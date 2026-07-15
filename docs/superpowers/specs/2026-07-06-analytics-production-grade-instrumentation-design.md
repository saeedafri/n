# Production-Grade User Analytics — Instrumentation Upgrade

**Date:** 2026-07-06
**Author:** engineering (via Claude)
**Status:** implemented
**Scope chosen by product owner:** *Core + top actions* (enriched envelope + auto error capture +
login/logout lifecycle + downloads/searches/screening-run wiring).

---

## 1. Problem

The current analytics feed (`server-logs/user_analytics.log`, JSONL, one non-blocking sink
`log_user_event`) records only **`page_view`** and **`filter_change`**. That is navigation-only
telemetry. From the 2026-07-06 export (1,595 events) we could reconstruct *where* users went but
**not**:

- **Actions** — downloads/exports, search queries (the text), screening runs (with result counts).
- **Reliability** — errors never reach the analytics feed (they only go to `server-log.log`).
- **Session lifecycle** — no explicit `login` / `login_failed` / `logout`; durations had to be
  estimated.
- **Segmentation** — no device/browser/OS, app version/build, environment, `event_id`,
  `schema_version`, or `prev_page` for funnels.

## 2. Goals / Non-goals

**Goals**
- Every event carries a rich, self-contained envelope (segmentable without joins).
- All exceptions already funnelled through `log_structured_error` also surface as `error`
  analytics events — **zero new call sites**.
- Explicit login/logout lifecycle.
- Capture the highest-value actions: downloads, searches (with query text + result count),
  screening runs.
- Reuse the existing sink; **no new infra, no new deps, non-blocking, never raises into render.**

**Non-goals (this pass)**
- Per-tab dwell timing, session heartbeats/`session_end`, watchlist/alert/admin action wiring,
  client-side JS RUM. (Deferred; helpers are generic enough to extend later.)

## 3. Design

### 3.1 Enriched envelope (central, automatic)
Added inside `log_user_event()` so **every** event (existing + new) gets it:

| Field | Source | Notes |
|---|---|---|
| `event_id` | `uuid4().hex[:16]` | dedup / idempotency |
| `schema` | constant `2` | schema version — safe evolution |
| `env` | `APP_ENV` | local/staging/production |
| `app_version` | `APP_VERSION`/`GIT_SHA`/`BUILD_ID` env, else `.git/HEAD` sha (read once) | correlate behavior ↔ release |
| `browser`,`os`,`device` | parsed from `st.context.headers['User-Agent']`, cached per session | desktop/mobile/tablet/bot |

`device`/`browser`/`os` parsing is a dependency-free heuristic in `_parse_user_agent`. Client
context is resolved once per session (`st.session_state['_analytics_client_ctx']`).

### 3.2 `prev_page` on `page_view`
`new_rerun_id()` now stamps `prev_page` (last *different* page) → direct funnel edges.

### 3.3 Auto error capture (zero new call sites)
`log_structured_error()` additionally emits an `error` event
(`error_type`, `message`, `origin`, `component`, `operation`, `ctx`). Process-level
rate-limit: same `origin` at most once / 5 s (avoids flood on repeated reruns). No recursion:
`log_user_event` swallows its own errors and never calls `log_structured_error`.

### 3.4 New event helpers (thin, best-effort wrappers over `log_user_event`)
- `track_login(user, method, success, reason)` → `login` / `login_failed`
- `track_logout(user, route)` → `logout`
- `track_download(kind, name, page, **props)` → `download`
- `track_search(page, query, results, kind, **props)` → `search`
- `track_action(action, page, **props)` → generic `action` (e.g. `screen_run`)

**Dedupe:** `_analytics_should_emit(ns, val)` — session-scoped "changed since last time" gate.
Suppresses re-renders of the same download/search/action across reruns, but re-emits when the
value genuinely changes. Off-thread (no session_state) → errs on emitting.

### 3.5 Wired call sites
| Event | File / choke point |
|---|---|
| `login` / `login_failed` | `auth_manager.AuthManager.login()` (success path + except) |
| `logout` | `auth_manager.AuthManager.logout()` (start, with route) |
| `download` (transcript PDF, Excel) | `earnings_calls._render_js_download_button`, `_render_excel_js_download` |
| `download` (filing PDF) | `company_filings._render_filing_download_button` |
| `search` (news keyword) | `newsroom` keyword-search caller (query + `len(articles)`) |
| `action=screen_run` | `screening._render_results` (matched count) |

## 4. Event schema (v2)

Common envelope on every line:
`ts, event, event_id, schema, env, app_version, user, user_session, session, page,
browser, os, device` + event-specific fields.

| event | key fields |
|---|---|
| `page_view` | `page_title, rerun, since_last_ms, filters, reason, prev_page` |
| `filter_change` | `filters, reason` |
| `login`/`login_failed` | `method, reason` |
| `logout` | `route` |
| `download` | `kind, name` |
| `search` | `kind, query, results` |
| `action` | `action, …props` (e.g. `screen_run` → `results`, `criteria_count`) |
| `error` | `error_type, message, origin, component, operation, ctx` |

Backward-compatible: existing fields unchanged; `session` still = per-rerun id, `user_session`
still = stable auth-session id. Consumers ignore unknown fields.

## 5. Testing plan

1. Unit-ish import/log smoke: import `server_logger`, call every new helper + force an error →
   assert JSONL lines with the new envelope keys land in a temp analytics log.
2. Real UI (mandatory): `bash .claude/dev/run_local.sh`; drive login → market_data tabs →
   newsroom keyword search → earnings_calls transcript download → screening run → logout via
   `.claude/dev/ui_test.py`; then read the tail of `user_analytics.log` and confirm the new event
   types + envelope appear, correctly attributed.
3. Regression: confirm existing `page_view`/`filter_change` still emit and the Jul-6 report script
   still parses (new keys are additive).

## 5b. Round 2 — deferred actions wired + report generator (2026-07-06)

**Deferred actions wired** (all at discrete button/click handlers that end in `st.rerun()` or a
single success — fire ONCE per user action, never in a render loop; each `try/except`, best-effort):

| Event | File / handler |
|---|---|
| `action=watchlist_create` | `screening.py` — Create Watchlist button success (`companies`, `sectors`) |
| `action=alert_prefs_save` | `earnings_calendar.py` — alert-prefs save `if ok:` (`enabled`, `days_before`, `mode`, counts, `mailed`) |
| `action=filing_upload` | `company_filings_add_files.py` — after upload `st.success` (`name`, `size_bytes`, `overwrite`) |
| `action=retailer_save` | `retailer_adding.py` — Save Changes, `if total>0` (`updated`, `inserted`) |

**Performance proof (the hard constraint):** micro-benchmark of `log_user_event` = **50 µs/event,
~20,000 events/sec**, handler is a `QueueHandler` → the file write happens on the QueueListener
background thread, so the request thread never blocks on I/O. Client context is cached per session
(parsed once). Rule enforced: **emit only at discrete click handlers, never per-row/per-cell**.
No wiring is in a page-load path, so page render time is unchanged.

**Report generator** `.claude/dev/analytics_report.py` (reusable):
`python .claude/dev/analytics_report.py <export.jsonl> <output_basename>` → xlsx (11 sheets) + md.
v2-aware: adds **Actions & Events / Downloads / Searches / Errors** sheets, per-user
download/search/error/action counts, device/browser/env/app_version columns. Backward-compatible:
on a v1 (navigation-only) file the new sheets render an explanatory note and existing sheets are
unchanged (`HAS_ENVELOPE` gate).

**Regression test (real UI):** launched local app; drove `/home`, `/market_data`, `/screening`,
`/earnings_calendar`, `/company_filings_add_files`, `/retailer_adding` — all rendered, **0
`STRUCTURED_ERROR`**, screening/home content markers present. Report verified on synthetic v2
(all sheets populate) and v1 (backward-compatible) samples.

## 6. Rollout / risk

- All paths best-effort (`try/except`, non-blocking queue handler) → cannot break a page render.
- No schema removals → existing dashboards/exports keep working.
- File size: envelope adds ~120 bytes/line; 50 MB rotation × 10 backups already configured.
- Env knobs unchanged; `APP_VERSION`/`GIT_SHA` optional (falls back to `.git` sha then `unknown`).
