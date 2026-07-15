# OIDC login stuck on "Completing sign-in…" — root cause + fix

**Date:** 2026-07-06
**Author:** Claude (Opus 4.8), driven by Mohd Saeed Afri
**Reported by:** manager (shashankgupta@coresight.com) — stuck on the login page for a long time.
**Evidence:** `server-logs/server-logs-20260706_025840.log` (STG, marketdata-stg.coresight.com)
**Scope:** `app/pages/login.py` — OIDC authorization-code callback processing.

---

## 1. Symptom

User lands on `marketdata-stg.coresight.com/?code=…&state=…` after the IdP redirect and
is pinned forever on the branded **"Completing sign-in…"** spinner (screenshot #46). The
page never advances to `/home`. Reloading does not help.

Secondary observation from the reporter: server-log lines show `user=-` (empty) and "never
get updated".

## 2. Root cause (proven from logs — not a guess)

The OIDC **authorization code is exchanged more than once**. An OIDC auth code is
**single-use and short-lived**; the second exchange of the same code is rejected by the IdP
with **HTTP 400 `invalid_grant`**, so `_complete_oidc_login()` is never reached and the user
is stranded.

The duplicate exchange is produced by a **timeout inversion** in the callback state machine:

| Constant | Value | Where |
|---|---|---|
| token-exchange HTTP timeout | **20 s** | `login.py` `_exchange_code_for_tokens` (`requests.post(..., timeout=20)`) |
| "processing marker stale" window | **8 s** | `login.py` callback loop (`_processing_age > 8`) |

Because `8 s < 20 s`, the app declares the still-in-flight first exchange "stale" and
**re-POSTs the same code** while the first attempt could still be running (or had merely been
preempted by a rerun storm). Concretely, under STG's rapid rerun storm the first exchange's
script run is separated from its result (its HTTP result never lands in the file/session
cache the polling reruns read), so the 8-second timer fires and a second `else`-branch
exchange sends the consumed code → `invalid_grant`.

### Smoking-gun evidence

**Every** `invalid_grant` in the log is immediately preceded by a
`"processing marker stale — clearing and retrying"` line. 5 for 5:

```
R00330 stale → R00333 exchange 400 invalid_grant   (07:14)
R00447 stale → R00450 exchange 400 invalid_grant   (07:16)
R00513 stale → R00516 exchange 400 invalid_grant   (07:17)
R00615 stale → R00618 exchange 400 invalid_grant   (07:19)
R00681 stale → R00684 exchange 400 invalid_grant   (08:14)
```

The **one successful** login in the window (R00706, 08:19:01) had **no** stale-marker — it
exchanged cleanly on the first attempt (`status=200 → SUCCESS`) in a single run.

The stuck flow at 08:14:
- `R00627` (08:14:13): captured code → spinner → "processing callback" → `token exchange →`
  … and then **no HTTP result is ever logged for R00627** (the first exchange's run was
  lost/hung).
- `R00630…R00681` (08:14:14→22): ~3 reruns / 0.4 s alternating `page=main`↔`page=login`,
  each showing the spinner and `"callback already in progress"`, polling the (empty) caches.
- `R00681` (08:14:22, ≈8 s later): `processing marker stale`.
- `R00684` (08:14:23): re-exchange of the **same** code `a750c5b3e533…` →
  `OIDC_TOKEN_HTTP 510ms status=400` → `invalid_grant`.

All real token-exchange HTTP calls in the log complete in **~500 ms**; none are slow. So the
first exchange did not fail on its merits — it was **abandoned early** by the 8 s timer, and
the retry killed the code.

### Why the spinner stays forever (no auto-recovery)

On failure the old code left `__oidc_cb_captured` set, so the fixed-position white overlay
(`#csr-loading-overlay`, z-index 999999) kept covering the page; the `st.error` and the login
button underneath were hidden; and the failing `else` branch does **not** call `st.rerun()`,
so nothing ever moved the user off the spinner.

### Why `user=-` (empty)

On the login page the user identity is **unknown until the token exchange + userinfo
succeed** — `user=-` is a *symptom* of the failed exchange, not a separate bug. The log
confirms it populates the instant a login succeeds (`user=mohdsaeedafri@coresight.com` at
08:19:03, right after the 08:19:01 success). It "never updates" for the stuck user precisely
because his exchange keeps failing.

## 3. Fix (root, in `login.py`)

An authorization code can be exchanged **at most once** — a re-POST is never recoverable.
So: never re-POST a code, and never dead-end on the spinner.

1. **New constant `OIDC_PROCESSING_STALE_SECS = 25`** (env-overridable). It **must exceed the
   20 s HTTP timeout** so a first exchange that could still be running is never abandoned and
   re-issued. This kills the timeout inversion that caused every observed failure.

2. **New helper `_restart_oidc_signin(code, reason)`** — abandons a spent/failed code
   (marks it in `__oidc_denied_codes_dict`, clears all `__oidc_cb_*`/processing state) and
   `<meta refresh>`-redirects to a **clean** `/?auth_error=signin_retry`, dropping `?code&state`
   from the URL. Turns the permanent dead-spinner into a one-click, working retry (a fresh
   `/authorize` round-trip yields a brand-new code that exchanges cleanly).

3. **Call `_restart_oidc_signin` on every terminal callback failure** instead of falling
   through to the hidden-error spinner or (worse) re-POSTing:
   - stale processing marker (was: clear marker + `st.rerun()` → re-exchange),
   - `invalid/expired state`,
   - `_build_token_data` failure,
   - token-exchange failure (`invalid_grant` / network / expired).

4. **Friendly message** for `?auth_error=signin_retry` on the login page
   ("Your sign-in link expired before it could be completed. Please sign in again.").

### Why this can't loop

After a restart the URL is `/?auth_error=signin_retry` — **no `code`** — so there is no
capture and no exchange; the login page just shows the button + message. The next attempt is
**user-initiated** (a click), so there is no machine loop. Successes in the same config prove
the failure is transient, not a persistent misconfiguration.

## 3b. Defense-in-depth hardening (make it impossible, not just unlikely)

Five independent layers now guard the callback; a failure of any one is caught by another, and
**no path can re-POST a spent code or strand the user on the spinner**:

1. **Timeout invariant** — `OIDC_PROCESSING_STALE_SECS = 25` > 20 s HTTP timeout, so a still-in-flight
   exchange is never abandoned early.
2. **Stale → restart, never re-exchange** — the watchdog now restarts sign-in instead of clearing the
   marker and looping back into a re-POST.
3. **EXACTLY-ONCE hard guard** — a durable file marker `_oidc_att_<hash>` is written the instant
   *before* the network POST. Under the exchange lock, any run that finds the marker set but no cached
   token result treats the code as spent and **refuses to POST it again** (`_exchange_code_for_tokens`
   returns None → restart). Also pre-checked in the callback `else` branch for the cross-session case.
   This is the physical impossibility of a double-exchange — the exact defect that stranded shashank.
   It preserves recovery: attempted **with** a cached result completes via the cache (no re-POST).
4. **Restart on every terminal failure** — invalid state / build-token failure / exchange failure /
   already-attempted all route to `_restart_oidc_signin` → clean `/?auth_error=signin_retry`.
5. **Total-exception safety net** — the whole callback body is wrapped so any *unexpected* exception is
   logged and bounced to a clean sign-in; Streamlit's own control signals (rerun/stop) are re-raised by
   module/name check so the happy path still navigates. No unforeseen error can ever hang the spinner.

## 4. Verification

- **Root cause**: proven from STG logs (5/5 stale→invalid_grant correlation; the lone success
  had no stale-marker). ✓
- **Fix logic**: traced against all four failure branches and the happy path; no re-POST of a
  spent code remains; no new infinite loop (restart lands on a code-less URL). ✓
- **Static**: `python3 -m py_compile app/pages/login.py` clean; `OIDC_PROCESSING_STALE_SECS`
  (25) `>` HTTP timeout (20); all referenced symbols in scope. ✓
- **Exactly-once unit test**: the real helpers were extracted from `login.py` source and exercised —
  fresh code → not attempted; marked → attempted (durable); attempted + no result → SPENT (refuse
  re-POST); attempted + cached result → recover via cache; marker >300 s → ignored. ALL PASS. ✓
- **Local UI boot**: app starts, login module imports/renders, and the `signin_retry` recovery screen
  renders the friendly message — no error banner, after the full defense-in-depth edit. ✓
- **Cannot be exercised by the local harness**: the launcher runs `APP_ENV=LOCAL + DEBUG` and
  **bypasses OIDC** (`IS_OIDC_ENV` false), so the real code-exchange path does not execute
  locally. **Final confirmation is a real sign-in on STG after deploy** — expect: stuck code →
  automatic bounce to a clean login with the "expired, please sign in again" notice, and a
  fresh click completes cleanly.

## 5. Rollout / risk

- Read/auth-path only; no schema, no writes, no data touched.
- Behavioural change is strictly **safer**: the only new outcomes on failure are (a) waiting up
  to 25 s (was 8 s) before giving up on a genuinely-hung exchange, and (b) a clean-login
  redirect instead of a permanent spinner. The happy path (fast ~500 ms exchange) is unchanged
  and still completes in ~1 s.
- All thresholds are env-overridable (`OIDC_PROCESSING_STALE_SECS`).

## 5b. Verified against `server-logs-20260706_032341.log` — 4-agent adversarial review

A second log (shashankgupta stuck; mohdsaeedafri fine) was analysed by 4 independent agents,
each re-grepping the raw log (correlation / discriminator / skeptic-refuter / fix-efficacy).
All converged:

- **100% correlation (CONFIRMED, high):** 5 `invalid_grant` failures, each immediately
  preceded by exactly one `processing marker stale`; 3 successes (all mohd), zero stale
  markers. No counter-example.
- **Skeptic could not refute (CONFIRMED, high):** 8 alternative causes tested and rejected
  (PKCE / redirect_uri / client_secret — all identical config to mohd's successes; IdP outage;
  independent TTL expiry — re-POST fires only ~8 s after capture; clock skew; proxy; benign
  duplicate). Decisive count: **16 exchange POSTs fired but only 8 logged an HTTP result** —
  the 8 result-less POSTs are the first attempt of each failing flow, orphaned mid-run by the
  rerun storm. The blocking `requests.post` still reached the IdP and **consumed the one-time
  code**, so the watchdog's re-POST 8 s later gets `invalid_grant`.
- **Smoking gun (single code `b394b40ba34b`):**
  `R00276 07:14:07 "token exchange →" (no result logged) → R00330 07:14:15 "processing marker
  stale" → R00333 07:14:15 re-POST same code → status=400 invalid_grant 07:14:16`.

### Why shashank got stuck but mohd didn't (differentiator — session birth, NOT identity)

- **Shashank — in-place session expiry:** his `auth_session` cookie went `237 B → 0`
  (`source=missing`) at **07:14:00 while he was actively rendering market_data**. That drove
  the open tab through `require_auth`'s retry loop (0.15 s, 0.35 s) → `AUTH_REQUIRE_AUTH_EXHAUSTED`
  → auto-redirect to `/login` → IdP → back with `?code=`. The code landed at 07:14:07 into a
  browser **still settling from that forced machine-driven redirect**; the capturing run's
  exchange was preempted, and the `CALLBACK_SPINNER` overlay's own `sleep(0.2)+st.rerun()`
  loop stormed (session_rerun# 1→80 in ~10 s, single `auth_flow_id`, single tab) until the 8 s
  watchdog re-POSTed the spent code. **It is not two tabs and not websocket reconnects** — the
  storm is the callback's own polling loop, kicked off by a preempted first exchange.
- **Mohd — deliberate logout→login:** he clicked Log Out (`R00696` logout_bridge 08:18:34),
  landed on a calm login page, and his `?code=` returned into a fresh idle session; the exchange
  ran to `status=200 → login complete` in a single pass, no storm, no stale marker.
- **Implication:** this can hit *any* user whose session expires while they are actively using
  the app — it is a timing/trigger difference, not anything specific to shashank's account
  (no `OIDC_DENY`/access-denied lines for him; failures are pure `invalid_grant`).

### Residual gap flagged by the fix-efficacy auditor (honest caveat)

The fix reliably **unsticks the spinner** and converts the dead-end into a clean retry that
starts from a calm login page (mohd's known-good path). But it does **not** touch (a) the
rerun-storm trigger (in-place-expiry force-relogin) nor (b) the post-login cookie handoff.
Because shashank never reached a successful exchange in the log, we **cannot prove from these
logs** that his login will *stick* afterward — only that he will no longer be trapped on the
spinner. See §6 for the optional deeper hardening.

## 6. Follow-up (not required for the fix, worth noting)

The underlying **rerun storm** (≈3 reruns / 0.4 s alternating `page=main`↔`page=login` during
callback) is what separates the first exchange from its result and makes the race reachable.
It does not need to be solved to fix the stuck login (the fix is robust to it), but reducing it
would make sign-in snappier and is worth a separate investigation.
