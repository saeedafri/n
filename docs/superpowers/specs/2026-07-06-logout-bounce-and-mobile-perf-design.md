# Logout double-bounce + mobile page-slowness investigation

**Date:** 2026-07-06
**Author:** Claude (Opus 4.8), driven by Mohd Saeed Afri
**Evidence:** `server-logs/logout-issue.mov` (12s), `server-logs/full-recording.mov` (5min),
`server-logs/server-logs-20260706_044329.log`
**Scope:** `app/components/navigation.py` (logout link) — plus diagnosis of perceived page slowness.

---

## 1. Logout double-bounce — ROOT CAUSE + FIX

**Symptom (from `logout-issue.mov`, 24 frames @2fps):** click Logout → brief "Signing out" →
**lands back on the source page** showing "Loading Market Data…" for ~3-4 s → then
`/logout_bridge` ("Logging out") → then `/` login.

Frame trace (Macy's market_data session):
```
t0.0  /market_data?ticker=M&tab=company_profile   (Logout button visible)
t2.5  /market_data?action=logout                  "Loading Market Data" spinner  ← the bounce
t6.0  /logout_bridge                               "Logging out"
t8.0  /                                            "Loading Login"
t10   login page
```

**Root cause (proven):** the Logout link was
`app/components/navigation.py:526  <a href="?action=logout">`. `?action=logout` is a
**relative** query string, so the browser navigates to `<current-path>?action=logout` — e.g.
`/market_data?action=logout`. The URL *path* stays `/market_data`, so:
- the branded boot-overlay reads the path and shows **"Loading Market Data"** (wrong label), and
- Streamlit loads the **market_data page** (re-running the source page) until `main.py`'s
  `action=logout` handler (`main.py:368`) stashes the id_token and `st.switch_page`es to the
  logout bridge.

That source-page re-render is the visible "lands back on the page I clicked from".

**Fix:** make the link target the bridge **path** directly:
```html
<a href="/logout_bridge?action=logout" class="logout-btn">Logout</a>
```
- URL path is `/logout_bridge` from the click → overlay shows "Signing out", source page never renders.
- `action=logout` is **kept**, so `main.py` still runs `stash_logout_context(auth_data)` → the OIDC
  `end_session` (id_token_hint) logout is **not** downgraded to a local-only logout.
- `st.switch_page` drops the query param on the follow-up rerun, so there is no re-trigger loop.

**Verified (local UI):** navigating to `/logout_bridge?action=logout` routes **straight to
`page=logout_bridge`** (server log R00004) and runs the bridge's clear+redirect — **no market_data
render**. `navigation.py` parses clean.

> **Precision note (corrected after 4-agent verification):** the source page BODY does **not**
> re-render during logout — `main.py:368` intercepts `action=logout` before `pg.run()`. What the
> user sees as "Loading Market Data" is the **boot-overlay label** (driven by the URL *path*) held
> during the full browser reload + `main.py` reroute. The absolute-path fix still cures it because
> the URL path is `/logout_bridge` from request one, so the overlay reads "Signing out". Verified:
> every pre-fix logout in the log carries `qp_keys=[action]` with `routing_target=<source page>`
> (company_filings / home / newsroom / earnings_calls), never `logout_bridge`.

---

## 2. "Why are pages slow — is it my mobile network?"

**Verified answer: BOTH, and it depends on the page.** (Corrects my looser first read — several
heavy pages are genuinely slow on the SERVER, not just network.)

- **Light pages are fast server-side** — home `PAGE_LOAD` median **37 ms** (worst 223 ms), `logs`
  median 0.02 s. For these the lag is the app's **full-reload navigation** (`477`
  `MAIN_PAGE_REGISTRATION` in the session — nearly every nav is a full browser reload), 3–6 reruns
  per view, and the branded loader held across the Streamlit websocket reconnect — all multiplied by
  mobile round-trip latency (21 % of rerun gaps > 500 ms; 123 gaps > 3 s).
- **Heavy pages are genuinely slow on the SERVER** (measured before bytes leave the box):
  - `earnings_calls` median **2.93 s**, worst **14.54 s** — of which **13.3 s is CPU** Python
    transcript rendering (`RENDER_CONTENT`), only 0.4 s DB.
  - `earnings_calendar` 1.76 s / **7.44 s** — `get_earnings_event_dates` **10.4 s**,
    `get_ipo_events` 5.4 s, `get_ma_completion_events` 3.3 s.
  - `newsroom` 1.56 s / **8.38 s** — `get_articles` up to **8.2 s**, `get_yf_articles` up to 4.2 s.
  - `company_filings` cold **11.70 s** (`[PERFORMANCE_ALERT] … CRITICAL`).
  - `market_data` usually 0.34 s, occasional 10 s outliers.

So: mobile network is a real multiplier on the full-reload navigation (felt most on otherwise-fast
pages like home), **and** several heavy pages spend seconds in DB/CPU on STG regardless of network.

## 3. "Why is home slow?"

Home is **not** slow on the server (median 37 ms, zero DB on its render path). The 2.26 s / 3.25 s /
4.26 s `SLOW` outliers are full-reload-after-idle / cold Azure connection warmup — the SLOW number is
click→paint wall time (reload + websocket reconnect + reruns + loader), not home compute.

## 4. Other issues found in the recording (verified)

- **The "HTTP ERROR 500" frame = Azure cold-start / container recycle, NOT app code.** The log has a
  single **~24.5-minute total-silence gap (09:34:05 → 09:58:32, no heartbeats)** ending in a clean
  cold boot (`[BOOT] server_logger up`, R-counter reset, `MAIN_AUTH_BOOTSTRAP 744 ms`), immediately
  followed by the user re-authenticating. Azure App Service suspended the idle container; the return
  request hit the down/cold backend and the platform proxy served the 500. Proof it's not app code:
  **zero** Python tracebacks in 14,493 lines, no OOM (min free RAM 1.3 GB), clean boot after. *Honest
  caveat:* the literal proxy 500 line lives in Azure's front-end logs, not this app log — inferred
  from the gap + boot + re-auth. **Fix: enable Always-On / a warmup ping** so the idle container isn't
  recycled.
- **`earnings_calls` "future failed for M" is a real bug amplifier** (`earnings_calls.py:2412/2420`):
  the prefetch future for `get_earnings_event_dates` times out at 10 s (empty-message
  `TimeoutError`), is logged, then the code **re-runs the same 10.4 s query synchronously** — so
  Macy's earnings page pays the slow query **twice** (→ 14.5 s). Fix: index/scope the query and, on
  timeout, degrade gracefully instead of re-executing serially.
- **STG runs on the default `SECRET_KEY`** (`config.py:249` warning at boot) — session-signing
  hardening gap. Set the `SECRET_KEY` env var on STG.

## 5. Rollout / risk

- **Logout fix (applied):** one-line href change, read-path only; OIDC `end_session` preserved
  (`action=logout` retained → `main.py` still stashes the id_token); `switch_page` drops the query
  param so there is no loop. Verified locally (routes straight to `page=logout_bridge`). Log is
  pre-fix, so **confirm with one STG logout smoke test after deploy** (expect `routing_target=
  logout_bridge` on request one).
- **Separate pre-existing logout gap (NOT caused by this change):** when the id_token temp file is
  missing (`stash source=NOT_FOUND`, e.g. R00697), logout downgrades to local-only and the **IdP
  session is not destroyed**. Track and fix separately.
- **Slowness / 500 / earnings_calls double-query / SECRET_KEY are DIAGNOSIS only** — no code changed
  for them yet. Each needs its own scoped change + verification.
