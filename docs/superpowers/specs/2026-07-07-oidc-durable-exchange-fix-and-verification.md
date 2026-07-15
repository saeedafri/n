# OIDC durable token exchange — fix + end-to-end verification

**Date:** 2026-07-07
**File:** `app/pages/login.py`
**Status:** implemented + verified end-to-end via Playwright against stage3. Ready to deploy (STG + prod).

## Problem (reproduced end-to-end)

Login intermittently fails for users on unstable/slow connections. Reproduced deterministically
with Playwright driving a **real stage3 OIDC login** on prod-identical code:

- The callback fires the token exchange, but the exchange runs **inside the Streamlit UI run**.
- stage3's token endpoint is slow (~2s); during that window the "Completing sign-in…" spinner
  fires a **storm of reruns** that **abandons** the exchange run before its result is captured —
  yet the POST still reaches the IdP and **consumes the single-use code**.
- Prod's flow then hits its **8s "clearing and retrying"** → **re-POSTs the spent code** →
  **`invalid_grant`** → infinite spinner / error.

Same failure mode as the reported production case (Shashank): a stranded exchange + a re-POST of a
spent code. Trigger there is a dropped WebSocket; here it is a rerun-storm + slow IdP — same root
fragility. Confirmed on **prod-identical code**, so it is NOT a prod-vs-STG code difference.

## Fix — durable, reconnect-safe exchange (never re-POST)

The single-use exchange no longer lives in the fragile UI run:

- **`_background_exchange_worker`** — does the POST in a **daemon thread** that OUTLIVES the UI run;
  never calls `st.*`; writes tokens to the durable file+process cache; records terminal failures in
  `_oidc_bg_error`.
- **`_try_claim_exchange`** — **atomic file claim** (`O_CREAT|O_EXCL`) keyed by the code. Exactly one
  caller (across all sessions/reruns/processes) wins the claim and POSTs; everyone else polls. A
  stale claim (>25s, claimer died) is stolen so a broken attempt can still recover. This is what
  guarantees the code is **POSTed at most once** — no re-POST, so `invalid_grant` cannot occur.
- **`_ensure_background_exchange`** — `done` (cached) / `error` (terminal) / `inflight` (poll).
- **Callback** — unified poll: cache hit → `_build_token_data` → `_complete_oidc_login`; terminal
  error → `st.error`; else spinner + `st.rerun()` (bounded to 30s). The old `elif _processing …`
  "clearing and retrying" re-exchange is **removed**.

A dropped WebSocket / reload / rerun-storm now **resumes** (the reconnected run re-reads `?code` from
the URL and finds the tokens the daemon already cached) instead of stranding the spent code.

### An earlier iteration's bug (fixed)
First attempt gated exactly-once on an in-memory `inflight` set + a `_was_code_attempted` file
marker. Streamlit's per-run script context made the in-memory set invisible to the next rerun, so
`_was_code_attempted` tripped a **sticky** `already_attempted_no_result` error ~2s before the worker
cached → login failed. Replaced with the atomic **file claim** (process-safe) → no false positives.

## Verification (Playwright → real stage3, mohdsaeedafri@coresight.com)

`--host-resolver-rules=MAP 192.168.1.35 127.0.0.1` so the browser reaches the local app while
stage3 sees the registered redirect. Suite: `oidc_strict_suite.py`. PASS = `login complete` AND no
`invalid_grant` AND no `clearing and retrying` (verdict read from the server log).

| Scenario | Result |
|---|---|
| Control (normal login) | ✅ PASS → /home |
| Drop (offline mid-exchange) | ✅ PASS → /home |
| Reload (mid-spinner) | ✅ PASS → /home |
| Flaky (3× offline toggles) | ✅ PASS → /home |
| Repeat ×3 (consistency) | ✅✅✅ PASS → /home |

Strict audit over the run: **14 exchange POSTs, 14 distinct codes, 0 codes POSTed >1×** (exactly-once
proven); 7/7 logins completed; **0** `invalid_grant`, **0** `clearing and retrying`, **0** FAILED
after the fix went live.

## Local-testing config (CapIQReplacement only — do NOT ship these)
- `login.py`: `IDP_BASE_URL` / `OIDC_REDIRECT_URI` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` made
  env-overridable (backward-compatible: exact prod defaults when the env vars are unset).
- `.env`: points at stage3 + `OIDC_REDIRECT_URI=http://192.168.1.35:8000` (a stage3-registered
  redirect). Run real OIDC with `APP_ENV=staging` (not `run_local.sh`, which forces the bypass).

## Rollout
- Deploy the **durable-exchange** change to STG, watch `server-logs` for `bg token exchange SUCCESS`
  + `login complete` under a simulated drop, then prod (prod has the identical underlying issue).
- Decouple this from the env-overridable config block if you want prod's `login.py` to stay byte-
  identical except for the exchange logic.
- Independent infra win: raise the STG reverse-proxy WebSocket idle-timeout to reduce the drops in
  the first place.
