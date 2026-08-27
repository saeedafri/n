# Logout — "Invalid id_token_hint" root cause & fix

**Date:** 2026-08-25
**Reported:** 25-Aug-2026 ~5:20 PM IST, STG (`marketdata-stg.coresight.com`)
**Symptom:** clicking Logout lands on
`https://coresight.com/csr-idp/logout/?id_token_hint=eyJ0eXAi…` showing a bare
white card reading **"Invalid id_token_hint"**. User is never returned to the
portal login page.

---

## 1. Root cause

The app talked to **two different IdPs**: it minted the `id_token` at
`stage3.coresight.com` and then presented that token to `coresight.com`.

`login.py` selects the IdP host from the environment
(`APP_ENV`/`ENVIRONMENT`/`ENV`, with an `IDP_BASE_URL` override):

| env | IdP base |
|---|---|
| `staging` / `stg` | `https://stage3.coresight.com` |
| `production` / `prod` / default | `https://coresight.com` |

`logout_bridge.py` did **not**. It carried a hardcoded constant:

```python
IDP_BASE_URL = "https://coresight.com"   # app/pages/logout_bridge.py:51 (before fix)
```

RP-initiated logout requires the OP to verify the `id_token_hint` JWT — issuer,
`kid`, signature. A stage3-signed token has an issuer and signing key the
production IdP has never seen, so `coresight.com/csr-idp/logout/` rejects it
outright: **Invalid id_token_hint**.

### Evidence — `server-logs-20260825_115210.log`

```
05:20:07  login.py:1106         [LOGIN] OIDC mode | IDP=https://stage3.coresight.com/csr-idp/authorize
05:20:39  auth_manager.py:665   AUTH_STASH_LOGOUT_ID_TOKEN | source=temp_file session_id=a2727081
05:20:40  logout_bridge.py:383  [LOGOUT] Step 3 — JS clear+redirect to IdP | idp='https://coresight.com'
05:20:40  logout_bridge.py:419  dest='https://coresight.com/csr-idp/logout/?id_token_hint=eyJ0eXAiOiJKV1QiLCJraWQiOiIx'
```

Login → stage3. Logout → coresight.com. 33 seconds apart, so the token was
fresh — **expiry was not the cause**; the wrong host was.

This is why the failure looked intermittent: any logout that fell back to
*local-only* (no `id_token` recovered) redirected straight to the portal and
appeared to work. Only logouts that successfully recovered an `id_token` reached
the wrong IdP and showed the error.

---

## 2. Fix

One IdP origin, resolved in one place, used by both pages.

**`app/core/auth_environment.py`** — new `idp_base_url()`, the single source of
truth (this module is already the env gate for auth):

```python
def idp_base_url() -> str:
    override = os.getenv("IDP_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    env_values = {os.getenv(key, "").strip().lower() for key in ("APP_ENV", "ENVIRONMENT", "ENV")}
    if not (env_values & {"production", "prod"}) and (env_values & {"staging", "stg"}):
        return "https://stage3.coresight.com"
    return "https://coresight.com"
```

Precedence is byte-for-byte the rule `login.py` already used: explicit
`IDP_BASE_URL` wins; production beats staging when both appear; default is
production.

**`app/pages/logout_bridge.py`** — hardcoded constant replaced:

```python
from core.auth_environment import is_production_deploy, idp_base_url
IDP_BASE_URL = idp_base_url()
```

**`app/pages/login.py`** — its private `_DEF_IDP_BASE` branch deleted; now calls
the same helper, so the two can no longer drift. Client secret and redirect URI
still come from login's own env branch (unchanged).

Nothing else touched. No DB change, no schema change, no auth-behaviour change.

---

## 3. Data flow after the fix

```
login.py            logout_bridge.py
   │                       │
   └──── idp_base_url() ───┘        ← one function, one answer per process
              │
   staging → https://stage3.coresight.com
   prod    → https://coresight.com
              │
   authorize / token / userinfo     logout?id_token_hint=…
   (token minted here)              (token verified here — same host ✓)
```

---

## 4. Testing

**Unit self-check** (run with `IDP_BASE_URL` cleared, since `.env` sets it):

| `APP_ENV` | result |
|---|---|
| `staging` | `https://stage3.coresight.com` ✅ |
| `stg` | `https://stage3.coresight.com` ✅ |
| `production` | `https://coresight.com` ✅ |
| `prod` | `https://coresight.com` ✅ |
| *(unset)* | `https://coresight.com` ✅ |
| `APP_ENV=staging` + `ENVIRONMENT=production` | `https://coresight.com` (prod wins) ✅ |
| `IDP_BASE_URL=https://custom.example.com/` | `https://custom.example.com` ✅ |

**Live UI** — `bash .claude/dev/run_local.sh` → `http://localhost:8501`,
driven with `.claude/dev/ui_test.py`:

```
/logout_bridge  → logout_bridge.py:198 | IDP_BASE_URL='https://stage3.coresight.com'
/login          → login.py:1104        | IDP=https://stage3.coresight.com/csr-idp/authorize
```

Same host from both pages in the same process. Before the fix the first line
read `https://coresight.com`.

---

## 5. Rollout

Code-only change; takes effect on the next STG deploy. No App Setting is
required — `APP_ENV=staging` already resolves stage3. If STG *does* carry an
explicit `IDP_BASE_URL` app setting it keeps winning, and both pages now honour
it identically.

Production is unaffected: `APP_ENV=production` resolves `https://coresight.com`,
exactly what logout_bridge hardcoded before.

---

## 6. Separate issue found in the same logs (NOT fixed here)

At 05:20:05 the *first* logout attempt logged:

```
auth_manager.py:685  AUTH_STASH_LOGOUT_ID_TOKEN | source=NOT_FOUND session_id=99bae444
logout_bridge.py:330 [LOGOUT] Step 1 — id_token NOT found in any source — local-only logout
```

The `id_token` lives in `/tmp/_oidc_idtok_{hash20}.json`, keyed by session id.
`/tmp` is wiped on every App Service restart/deploy, and a multi-worker process
can land on a worker that never wrote the file. When it misses, logout is
**local-only**: portal cookies are cleared but the IdP session survives, so the
next sign-in is silently re-authenticated.

Fix direction (separate ticket): persist the id_token where it outlives a
deploy — `/home/...` (persistent Azure share) instead of `/tmp`, or the existing
`market_data_user_sessions` row.
