# Earnings Calendar Optimization Notes

**Status:** Phase 1 + Phase 2 + Phase 3 complete. Phase 4 (st.fragment) + Phase 5 (Playwright) pending.
**Last updated:** 2026-06-11

---

## API / OpenAPI contract

No `API_MAPPING.md` / Swagger / OpenAPI changes required.
This is a Streamlit UI-only optimization. No public API contract changed.

---

## Files changed

| File | Change summary |
|------|---------------|
| `app/core/database.py` | Added `execute_query_readonly_raising()` — raises on error instead of returning `[]` |
| `app/data/repository.py` | `get_calendar_events`: `@staticmethod` + `@st.cache_data(ttl=300)` + `start_date`/`end_date` params + uses raising variant; `get_available_tickers` + `get_date_range`: use raising variant; alert preferences TTL cache; upsert invalidates cache; `_build_ir_website_lookup`: `SELECT *` → specific 5 columns (Phase 3) |
| `app/data/watchlist_service.py` | TTL cache on `get_user_watchlists`; `_invalidate_watchlist_cache()` called from 8 write functions / 10 invalidation call sites (including `grant_watchlist_access` and `revoke_watchlist_access`) |
| `app/pages/earnings_calendar.py` | Date-windowed fetch; toolbar parallelization; `datesSet` callback; empty-range vs DB-error distinction; `ThreadPoolExecutor` top-level import |

---

## Purpose of each change

### `database.py` — `execute_query_readonly_raising`

**Before:** `execute_query_readonly` catches ALL exceptions and returns `[]`. When `@st.cache_data` wraps a caller, a DB timeout silently caches an empty list. For 5 minutes (TTL), every session sees "No data" even after the DB recovers.

**After:** `execute_query_readonly_raising` raises on any DB error. `@st.cache_data` does not cache exceptions — only successful return values are stored. A timeout causes a cache miss on the next request, not a 5-minute blackout.

### `repository.py` — `get_calendar_events`

- `@staticmethod` required for `@st.cache_data` to key on `(tickers, start_date, end_date)` correctly on a class method.
- `@st.cache_data(ttl=300, show_spinner=False)` — shared across all sessions; keyed by `(tickers: Optional[Tuple], start_date: Optional[date], end_date: Optional[date])`.
- `start_date` / `end_date` params added — SQL uses `WHERE earnings_date BETWEEN :start_date AND :end_date` when provided. Month view passes a 42-day window; year view passes Jan 1–Dec 31.
- `[EC_OPT] DB_EMPTY_VALIDATED` logged when DB returns no rows for a valid query (real empty range, not an error).

### `repository.py` — alert preferences TTL cache

`get_earnings_alert_preferences` checks session-state TTL cache (120 s) before hitting DB. On success, stores `(timestamp, result)`. On DB failure, returns last known good value from cache if available (stale-on-error fallback).

`upsert_earnings_alert_preferences` invalidates the cache entry after a successful save.

### `watchlist_service.py` — TTL cache + invalidation

`get_user_watchlists` checks session-state TTL cache (120 s) keyed by lowercase user email. Stale-on-error fallback on DB failure.

Cache invalidated (key deleted) on all 8 write paths:

| Function | Who's cache is invalidated |
|----------|---------------------------|
| `create_watchlist` | `user_email` (owner) |
| `update_watchlist` | `user_email` (owner) |
| `add_companies_to_watchlist` | `user_email` (owner) |
| `remove_company_from_watchlist` | `user_email` (owner) |
| `replace_watchlist_companies` | `user_email` (owner) |
| `delete_watchlist` | `user_email` (owner) |
| `grant_watchlist_access` | `target_email` (grantee's visible list changed) + `granter_email` (safety flush) |
| `revoke_watchlist_access` | `target_email` (grantee's visible list changed) + `granter_email` (safety flush) |

Note: `grant_watchlist_access` has an early `if target_email == granter_email: return True` no-op at the top (creator always has full access). In that case, neither invalidation call is reached — intentional, no state changed.

**10 call sites total** — confirmed by `grep -n _invalidate_watchlist_cache app/data/watchlist_service.py`:

```
162: create_watchlist
342: update_watchlist
417: add_companies_to_watchlist
474: remove_company_from_watchlist
554: replace_watchlist_companies
593: delete_watchlist
650: grant_watchlist_access (target_email)
651: grant_watchlist_access (granter_email)
688: revoke_watchlist_access (target_email)
689: revoke_watchlist_access (granter_email)
```

### `earnings_calendar.py` — DB error sentinel pattern

All three parallel fetch functions now return `None` on exception and their normal value (`[]` / tuple / list) on success. This distinguishes DB errors from genuinely empty results:

| Fetch function | DB error return | Valid empty return | Valid data return |
|---------------|----------------|--------------------|-------------------|
| `_fetch_tickers` | `None` | `[]` | `[{...}, ...]` |
| `_fetch_date_range` | `None` | `(None, None)` | `(min_date, max_date)` |
| `_fetch_all_events` | `None` | `[]` | `[{...}, ...]` |

After the parallel fetch:
```python
_tickers_had_db_error    = _tickers_raw is None
_date_range_had_db_error = _dr_raw is None
_events_had_db_error     = _prefetched_events is None
```

**Early exit (no tickers / no max_date):**
- DB error → `st.warning("...could not be loaded...please try again")`
- Valid empty → `st.error("No earnings calendar data found.")`

**Empty events gate (`_filter_mode in ("all", "watchlist")`):**
- Both modes depend on `_prefetched_events`. If `_events_had_db_error` is true, `[]` is a DB artefact, not a real empty range.
- DB error: `st.warning(...)` + early return. `[EC_OPT] DB_ERROR_EMPTY_RESULT` logged.
- Valid empty: fall through to navigable empty FullCalendar. `[EC_OPT] VALID_EMPTY_RANGE` logged.
- Single-company mode: exception propagates to top-level `render_page()` try/except → "Earnings calendar is temporarily unavailable."

**Watchlist caption guard:**
The "Filtering by watchlist: N companies" caption is only shown when `not _events_had_db_error`. If the base events fetch failed, the caption would falsely show "0 companies found" when the DB was the problem.

### `earnings_calendar.py` — date windowing

`_vis_start` / `_vis_end_inclusive` computed before the parallel fetch:
- If `ec_visible_start`/`ec_visible_end` in session state (set by prior `datesSet` callback): use those.
- Else: compute from anchor date + view type:
  - **Month view**: 42-day FullCalendar Sunday-aligned grid (`_month_view_date_range`)
  - **Year view**: Jan 1 – Dec 31 (`_year_view_date_range`)

Both `_fetch_all_events` and single-company fetch pass `start_date=_vis_start, end_date=_vis_end_inclusive`.

### `earnings_calendar.py` — toolbar parallelization

Alert preferences load and watchlist load were sequential. Now they run in a `ThreadPoolExecutor(max_workers=2)` context — both are independent read-only calls.

### `earnings_calendar.py` — `datesSet` callback

FullCalendar fires `datesSet` when the visible date range changes (navigation, view switch, initial mount). Guards prevent rerun loops:
1. `datesSet` fires with new range → compare to stored `ec_visible_start`/`ec_visible_end`.
2. If different: update state + `st.rerun()` to re-fetch with the correct window.
3. After rerun: FullCalendar fires `datesSet` again → same range → **NO rerun** (`DATES_SET_NO_RERUN` logged).

### `earnings_calendar.py` — DB error vs valid empty range

`_fetch_all_events` returns `None` on exception (DB error) and `[]` on valid empty result. After the parallel fetch:
- `_events_had_db_error = _prefetched_events is None`
- `None` is normalized to `[]` for downstream list operations.

At the empty-events rendering gate:
- **DB error + unfiltered fetch** → `st.warning(...)` + early return. Calendar NOT dead-stated; user can reload.
- **Valid empty range** (no events in window, or filter yields zero) → `VALID_EMPTY_RANGE` logged, FullCalendar still renders with empty grid. User can navigate via prev/next/today.

---

## Before timing (from `[EC_PERF]` — pre-optimization Playwright run)

Source: `test-artifacts/ec_perf/` from prior session.

| Phase | Time |
|-------|------|
| Cold first load — spinner gone | ~22 274 ms |
| Cold first load — total usable | ~144 869 ms (calendar chip not found by Playwright) |
| Warm reload — spinner gone | ~8 ms (cached) |
| `DB_GET_CALENDAR_EVENTS_ALL` | ~17 800 ms |
| Toolbar (alert prefs + watchlists sequential) | ~4 500 ms healthy / up to ~20 000 ms degraded |

---

## After timing (from `[EC_PERF]` / `[EC_OPT]` — PENDING first run)

> Run `scripts/start_ec_perf_local.sh` + `python scripts/ec_perf_playwright.py` after deploying these changes, then paste `[EC_PERF]` lines here.

Expected improvements:
- `DB_GET_CALENDAR_EVENTS_ALL` (month view): 42-day window instead of all events → fewer rows scanned, smaller result set. Estimated: 500–2 000 ms vs 17 800 ms.
- `DB_GET_CALENDAR_EVENTS_ALL` (year view): Jan–Dec window → ~12× less data than all-time. Estimated: 2 000–5 000 ms.
- `LOAD_ALERT_PREFERENCES` + `LOAD_USER_WATCHLISTS` parallel: wall time ≈ max(a, b) instead of a+b. Expected ~40–50% reduction on degraded runs.

---

## Query date windows per scenario

| Scenario | `vis_start` | `vis_end_inclusive` | Source |
|----------|-------------|---------------------|--------|
| Month view, June 2026, first render | 2026-05-31 | 2026-07-11 | `computed_month` |
| Month view, June 2026, after datesSet | 2026-05-31 | 2026-07-11 | `session_datesSet` |
| Month view, July 2026 (after next) | 2026-06-28 | 2026-08-08 | `session_datesSet` |
| Year view, 2026, first render | 2026-01-01 | 2026-12-31 | `computed_year` |
| Year view, 2026, after datesSet | 2026-01-01 | 2026-12-31 | `session_datesSet` |
| Year view, 2025 (after prev) | 2025-01-01 | 2025-12-31 | `session_datesSet` |
| Single company (AAPL), month view | same as month | same as month | same as month |
| Watchlist filter, month view | same as month | same as month | same as month (prefetched, filtered in memory) |
| View switch Month→Year | cleared → `computed_year` | cleared → `computed_year` | `computed_year` (range cleared on switch) |
| View switch Year→Month | cleared → `computed_month` | cleared → `computed_month` | `computed_month` |

---

## Event row counts before/after (PENDING first run)

> Paste `[EC_PERF] DB_GET_CALENDAR_EVENTS_ALL | ... event_count=... company_count=...` lines here after first run.

Known baseline (pre-optimization): ~12 352 events / 242 companies (all-time fetch).

---

## FullCalendar payload size before/after

Pre-optimization `[EC_PERF] TO_FULLCALENDAR` lines showed `payload_kb=...` — to be compared post-optimization. With month windowing, expected payload < 100 KB vs multi-MB for full dataset.

---

## Cache keys and TTL

| Cache | Key | TTL | Where |
|-------|-----|-----|-------|
| `get_calendar_events` | `(tickers, start_date, end_date)` — all hashable | 300 s | `@st.cache_data` on `EarningsCalendarRepository.get_calendar_events` |
| `get_available_tickers` | `()` (no args) | 300 s (existing) | `@st.cache_data` on `EarningsCalendarRepository.get_available_tickers` |
| `get_date_range` | `()` | 300 s (existing) | `@st.cache_data` on `EarningsCalendarRepository.get_date_range` |
| Alert preferences | `(user_email,)` in session state key `_earnings_alert_preferences_v1` | 120 s | Session-state dict, per-user |
| User watchlists | `user_email.lower()` in session state key `_coreiq_wl_cache_v1` | 120 s | Session-state dict, per-user |

`@st.cache_data` is **shared across all browser sessions** for the same process. Session-state caches are **per browser session**.

---

## Cache invalidation behavior

### `@st.cache_data` invalidation

`get_calendar_events` is invalidated when the tuple `(tickers, start_date, end_date)` changes. Navigation to a new month/year = new `start_date`/`end_date` = new cache key = DB fetch. Prior window remains cached for the TTL.

No explicit invalidation needed for read-only calendar data. TTL=300 s is the expiry.

### Session-state TTL caches

Alert preferences and watchlist caches expire after 120 s (checked at read time via `time.time() - cached_at < 120`). Write operations explicitly delete the cache key to force the next read to re-fetch.

---

## datesSet rerun behavior (loop proof)

```
Render 1 (initial):
  ec_visible_start=None, ec_visible_end=None
  → vis_range computed: computed_month = (2026-05-31, 2026-07-11)
  → EC_OPT VISIBLE_RANGE | source=computed_month
  → DB fetch for 2026-05-31..2026-07-11
  → FullCalendar renders
  → datesSet fires: startStr=2026-05-31, endStr=2026-07-12
  → EC_OPT DATES_SET_UPDATED | old_start=None new_start=2026-05-31 new_end=2026-07-12
  → st.rerun()

Render 2 (corrective rerun from datesSet):
  ec_visible_start=2026-05-31, ec_visible_end=2026-07-12
  → vis_range from session: 2026-05-31 .. 2026-07-11 (exclusive→inclusive)
  → EC_OPT VISIBLE_RANGE | source=session_datesSet
  → DB fetch (hits @st.cache_data — same key as Render 1)
  → FullCalendar renders
  → datesSet fires: startStr=2026-05-31, endStr=2026-07-12
  → range == stored → EC_OPT DATES_SET_NO_RERUN | reason=range_unchanged
  ✓ Loop stops

Navigation (user clicks "next"):
  FullCalendar advances to July 2026 client-side
  → datesSet fires: startStr=2026-06-28, endStr=2026-08-09
  → EC_OPT DATES_SET_UPDATED | old=2026-05-31/2026-07-12 new=2026-06-28/2026-08-09
  → st.rerun()

Render 3 (new month fetch):
  → DB fetch for 2026-06-28..2026-08-08
  → datesSet fires: same range → DATES_SET_NO_RERUN
  ✓ Loop stops
```

---

## View switch reset behavior

When "Year Wise" is clicked and `ec_view` was `"calendar"`:
1. `ec_visible_start = None`, `ec_visible_end = None` set in session state.
2. `ec_view = "year"`.
3. On next render: `_vis_start` computed via `_year_view_date_range(_anchor)` → Jan 1, Dec 31 of anchor year.
4. No stale month range is reused in year view. ✓

When "Month Wise" is clicked and `ec_view` was `"year"`:
1. `ec_visible_start = None`, `ec_visible_end = None`.
2. `ec_view = "calendar"`.
3. On next render: `_vis_start` computed via `_month_view_date_range(_anchor)` → 42-day Sunday grid.
4. No stale year range is reused in month view. ✓

---

## Known limitations

1. **First render = one extra rerun.** On initial page load (no `ec_visible_start` in session), the server computes a range from the anchor date. FullCalendar may report a slightly different range via `datesSet` (if the exact grid boundary differs). One corrective rerun occurs. After that, stable.

2. **Navigation gap.** When a user navigates to a new month/year, FullCalendar client-side advances immediately (empty cells until rerun fetches the new window). The rerun takes ~2–4 s to fetch and re-render. This is a fundamental Streamlit limitation — no client-side event data prefetch.

3. **Cache eviction on deploy.** `@st.cache_data` is in-process. Restarting Streamlit clears all caches. First load after deploy will be a full DB fetch for the visible window.

4. **Shared cache across sessions.** `@st.cache_data` is shared. If User A loads June 2026 and User B loads June 2026 shortly after, User B gets the cached result. This is the intended behavior — read-only data.

5. **Watchlist session-state cache is per-browser-session.** Two browser tabs for the same user each have independent caches. If the user adds a company in Tab A, Tab B shows stale data until its 120 s TTL expires or a write invalidates it in that tab's session.

6. **Year view FullCalendar `datesSet`**. FullCalendar's `multiMonthYear` view may report a range slightly different from Jan 1 – Dec 31 (e.g., it may include trailing days from the prior/next year). The `datesSet` corrective rerun normalizes this on first render.

---

## No-regression checklist

- [ ] Earnings dates unchanged — no date mutation applied
- [ ] Fiscal quarter labels unchanged — calculation logic untouched
- [ ] Company names unchanged — no name normalization changed
- [ ] Watchlist matching logic unchanged — `_filter_events_by_watchlist` / `_watchlist_rows_to_filter_scope` untouched
- [ ] Email Alerts dialog unchanged — `_render_email_alert_preferences_inner` untouched; preferences save/load works
- [ ] Event detail panel unchanged — `_render_detail_panel` untouched
- [ ] Transcript / webcast links unchanged — detail panel links untouched
- [ ] URL ticker behavior unchanged — `validate_and_get_ticker` call and `?ticker=` param handling untouched
- [ ] Month Wise / Year Wise toggle behavior unchanged — view switch still clears selected event
- [ ] Watchlist dropdown unchanged — `_on_watchlist_change` callback untouched
- [ ] Company dropdown unchanged — `_on_company_change` callback untouched
- [ ] Styling unchanged — no CSS or layout modified
- [ ] Local auth bypass unchanged — `require_auth` call and env-flag gating untouched
- [ ] No infinite rerun loop introduced — `datesSet` guard proven in log trace above
- [ ] Calendar still renders for empty date windows — `if not events:` no longer dead-states the page
- [ ] DB errors do not cache empty results — `execute_query_readonly_raising` + `@st.cache_data` exception-skip

---

## Artifacts (to be populated after Phase 5 Playwright run)

```
test-artifacts/ec_perf/
├── videos/
│   ├── cold_first_load.webm
│   ├── warm_reload.webm
│   ├── month_prev.webm
│   ├── month_next.webm
│   ├── month_empty.webm
│   ├── year_wise.webm
│   ├── year_prev.webm
│   ├── year_next.webm
│   ├── year_empty.webm
│   ├── month_to_year.webm
│   ├── year_to_month.webm
│   ├── company_filter.webm
│   ├── watchlist_filter.webm
│   ├── email_alerts.webm
│   ├── event_click.webm
│   └── db_failure_sim.webm
├── screenshots/
│   └── (per scenario)
├── logs/
│   └── server-log-perf-run.log
└── REPORT.md
```

Server log path: `server-logs/server-log.log`

---

## Phase 3 — Index investigation (COMPLETE — 2026-06-11)

Full EXPLAIN ANALYZE output: `docs/phase3_index_analysis.md`

### Table sizes

| Table | Row count |
|-------|-----------|
| `coreiq_nasdaq_earnings_calendar` | 11,996 |
| `coreiq_yf_earnings_calendar` | 521 |
| `coreiq_av_earnings_call_transcripts` | 18,336 |
| `coreiq_ir_websites` | 290 |
| `company_ir_websites` | does not exist in STG |

### Actual DB execution times (from EXPLAIN ANALYZE)

| Query | Access type | DB time | Rows scanned |
|-------|-------------|---------|--------------|
| Month window — all companies | `idx_earnings_date` range | **68ms** | 74 |
| Year window — all companies | `idx_earnings_date` range (nasdaq) / full scan (yf, 521 rows) | **57ms** | 463 |
| Single-company month | `idx_ticker_earnings_date` composite range | **0.05ms** | 0 |
| Single-company year | `idx_ticker_earnings_date` composite range | **0.14ms** | 3 |
| Transcript lookup (AAPL) | `idx_has_transcript_ticker` | **13.6ms** | 72 |
| IR lookup (`SELECT *`) | Full table scan | **521ms** | 290 |

### Verdict: no new indexes needed

All calendar queries use existing indexes correctly:
- `idx_ticker_earnings_date (ticker, earnings_date)` on nasdaq — used for single-company + all-company windowed range
- `idx_earnings_date` on both tables — used for date-range scan when no ticker filter
- `idx_yf_ticker_date (ticker, earnings_date)` on yf — used for single-company

The yf year-window full scan (521 rows, 23ms) is correct optimizer behavior for a tiny table. No index benefit.

The original ~17.8s bottleneck was fetching all 11,996 rows with no date window before Phase 2. With date windowing, DB execution is 57–68ms — negligible.

### IR lookup `SELECT *` fix (applied)

`_build_ir_website_lookup` was doing `SELECT *`, pulling `raw_json_row` (JSON) and 18 other columns. Only 5 columns are consumed: `ticker`, `company_source`, `ir_website_url`, `confidence_score`, `last_error`.

Fixed in `repository.py`: `SELECT *` → `SELECT ticker, company_source, ir_website_url, confidence_score, last_error`

Expected: IR lookup drops from 521ms → ~30–50ms. Since this call is inside the `@st.cache_data`-wrapped `get_calendar_events()`, it only runs on cold load / TTL expiry.

### `company_ir_websites` not found

Table does not exist in STG. The loop in `_build_ir_website_lookup` catches the `(1146, "Table ... doesn't exist")` exception and appends to `ir_table_errors`. Since `coreiq_ir_websites` returns 290 rows, `rows` is non-empty and the errors are silently logged. No bug.

---

## Phase 5 — Test scenarios (pending)

| # | Scenario | Expected result |
|---|----------|----------------|
| 1 | Cold first load | Calendar renders; spinner gone < 5 s (month window) |
| 2 | Warm reload | Spinner gone < 1 s (Streamlit cache hit) |
| 3 | Initial Month Wise visible range | `[EC_OPT] VISIBLE_RANGE source=computed_month` in log |
| 4 | Month previous | `DATES_SET_UPDATED` → rerun → `DATES_SET_NO_RERUN` |
| 5 | Month next | Same as #4 |
| 6 | Month with zero events | Empty calendar grid renders; prev/next/today clickable |
| 7 | Year Wise | `computed_year` range; all 12 months visible |
| 8 | Year previous | `DATES_SET_UPDATED` for new year range |
| 9 | Year next | Same as #8 |
| 10 | Year with zero events | Empty calendar renders navigably |
| 11 | Switch Year → Month | `ec_visible_start/end` cleared; `computed_month` fetched |
| 12 | Company filter | Single-company windowed fetch; detail works |
| 13 | Watchlist filter | Watchlist prefetch + in-memory filter; caption shows count |
| 14 | Email Alerts dialog | Dialog opens; preferences save/load; badge updates |
| 15 | Event click detail | Detail panel appears; transcript link works |
| 16 | Close detail | Panel closes; calendar reinits on correct month |
| 17 | Simulated DB failure | `DB_ERROR_VISIBLE_RANGE` in log; warning shown; no "No data" dead state |
