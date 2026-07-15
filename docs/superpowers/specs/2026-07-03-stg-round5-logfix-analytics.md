# STG Round-5 — Log Investigation + Analytics Full-Capture

**Date:** 2026-07-03
**Input:** `server-logs-20260703_160606.log` (3,356 lines) +
`user-analytics-20260703_160611.jsonl` (650 events).

---

## 1. Log investigation — round-3 fixes CONFIRMED live on STG
- **0** query timeouts (3024/OperationalError) — the newsroom deferred-join holds.
- **0** M&A `AttributeError` — the NaN→None fix holds.
- **0** streamlit_calendar component errors in this window (was intermittent).
- Newsroom `revenue` wide (2012→2026): **av=1000, yf=898** (was 0/0). Fixed on STG.
- Only **11 ERROR lines** total (was 707) — the log-level fix holds.

## 2. Real issues found & fixed
### 2.1 More misclassified ERROR-level diagnostics (log noise)
- `background_scanner.py`: `[BG_SCAN][INIT/AUDIT/SKIP]` were `log_error` → demoted to
  `log_info`. Kept the REAL ones (`=== SCAN WORKER CRASHED ===`, `Initialization failed`)
  at ERROR.
- `company_filings.py`: the `[AZURE_DL]` *success* download log → `log_info`; the 0-byte
  case → `log_warning`. **Verified:** 0 BG_SCAN/AZURE_DL at ERROR now.

### 2.2 Ratings tab 54s hang (CRMT)
- **Log:** `TAB_Ratings page_total=54416ms | ratings_render=53603ms` for CRMT.
- **Root cause:** `get_ratings_data` ran the edgartools (live SEC EDGAR) fallback with an
  **unbounded** `future.result()` (and a `with ThreadPoolExecutor` that JOINS the slow
  thread on exit). CRMT isn't in the DB ratings cache → cold live fetch = 53s.
- **Fix (`repository.py`):** bound the waits (`result(timeout=20/15)`) and
  `shutdown(wait=False)` so the UI renders DB-only after ≤~15s instead of hanging 53s; the
  background thread finishes and `@st.cache_data` has it warm for the next render.

### 2.3 Screening "Could not load results" (log_warning NameError)
- Present in this log (08:49:52) — this log predates the round-4 fix (`log_warning` import)
  reaching STG. Already fixed in code.

## 3. Analytics — full filter capture (was PARTIAL)
- **Finding:** all 650 events were `page_view`; only URL-encoded filters were captured.
  Gaps: screening criteria (0), newsroom keyword/sector (0), earnings_calls
  year/quarter (0), market_data currency/units (0), calendar kinds (0).
- **Fix:** new `log_filters_if_changed(page, **filters)` in `server_logger.py` — emits a
  `filter_change` event when a page's session-state filter set changes (deduped,
  non-blocking). Wired into every page at the point its filters are known:
  - newsroom → keyword, date_from/to, sort, sector, category
  - earnings_calls → company, year, quarter, keyword
  - screening → screen_for, criteria_count, criteria[{type, summary}], watchlist
  - market_data → currency, to_currency, units (complements the URL keys)
  - earnings_calendar → company, watchlist, active_kinds
- **Verified (Playwright + analytics log):**
  - newsroom → `{keyword: "inflation", date_from, date_to, sort}` ✓ (was invisible)
  - earnings_calls → `{company: AMZN, year: 2026, quarter: Q1}` ✓
  - earnings_calendar → `{active_kinds: [Q1..Q4, ma]}` ✓
  - screening → `{screen_for, criteria_count: 1, criteria: [{type: keydevs, summary: …}]}` ✓
  - market_data → currency/units captured when set.

## 3b. Multi-user attribution — every event names its user (CRITICAL for many users)
- **Problem:** 61/650 events had an EMPTY `user`, and `session` was only the per-rerun
  counter — so with many concurrent MDP users you couldn't tell whose event was whose.
- **Fix (`server_logger.py`):**
  - `_read_auth_identity()` resolves `(user_email, session_id)` from PER-REQUEST sources
    only — this session's `auth_data`, then this request's `auth_session` cookie (present
    even before auth_data re-hydrates → fills the empty `main`/`logs` events). Both are
    per-user, so an event is correctly attributed or left blank — **NEVER attributed to
    the WRONG user**. Deliberately NO process-global cache (that would leak one user's id
    to another user's background thread).
  - `log_user_event` now stamps EVERY event with `user` (resolved if the caller omitted
    it) and `user_session` = the stable auth session id (groups one user's activity,
    distinguishes concurrent users). `session` stays the per-rerun correlation id.
- **Verified (unit test, mocked session/cookie):** auth_data present → email+session_id;
  no auth_data but cookie present → email+session_id from cookie; anonymous → blank (never
  wrong). Full record: `{"user":"alice@coresight.com","user_session":"S-abcdef12345678",…}`.
- Note: can't be tested via the LOCAL UI because `require_auth()` is commented out on the
  pages locally (so the debug bypass never sets auth_data) — but STG runs require_auth, so
  auth_data/cookie are populated there (589/650 events already had the user pre-fix).

## 4. Files
`utils/server_logger.py`, `utils/background_scanner.py`, `pages/company_filings.py`,
`data/repository.py`, `pages/newsroom.py`, `pages/earnings_calls.py`,
`pages/screening.py`, `pages/market_data.py`, `pages/earnings_calendar.py`.
