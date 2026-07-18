# Screening (Key Developments) — Root-Cause Investigation & Fix Plan
**Date:** 2026-07-18
**Log:** `server-logs-20260718_152745.log` (STG, last boot 07:01 PM pid=1889)
**Reported by user (marketdata-stg.coresight.com/screening):** slow load (cold+warm), 56 s blank-no-spinner on filter apply, "All History" shows only Jun/Jul, "2000 events" looks capped, Additional Data makes 2 separate cards.

---

## Root causes (all proven with DB/log evidence)

### 1. "All History" shows only Jun/Jul — and "2000 events" is a hard cap  ⬅ #1 correctness bug
`get_keydevs_events_for_tickers` (`screening_service.py:3849-3850`):
```sql
... ORDER BY e.event_date DESC, e.event_id DESC
LIMIT 2000
```
- `_build_keydev_date_clause` correctly returns `""` for All History → the query IS unfiltered by date.
- But `ORDER BY event_date DESC LIMIT 2000` returns only the **2000 newest** rows.

**DB evidence (`coreiq_company_events`):** ~119,169 rows, range **2016-01-01 → 2026-07-17**, 3,913 tickers. Recent monthly volume: **Jul-2026 = 2,118**, Jun = 4,584, May = 6,729, Apr = 4,854.
→ The newest 2000 events barely cover **~17 days of July 2026**. "All History" silently truncates to ~2-3 weeks; the true match set is **tens of thousands** of events. "2000 events found" is the cap, not the count.

### 2. Filter apply = 56 s, blank, no spinner  ⬅ #1 performance bug
`[CLICK->RENDER] page=screening render=56.16s` (R00070). Breakdown from `SCREENING_CRITERION_APPLIED`:
| Criterion | Time |
|---|---|
| keydevs (idx 0) | 5,650 ms (cold; 4 ms warm) |
| additional — Credit Ratings (idx 1) | 11,663 ms |
| additional — Store Counts (idx 2) | **38,738 ms** |

Cause **A** — `_fetch_additional_display_values` (`screening_service.py:4083-4095`) aggregates over the **14.4 M-row `coreiq_filing_metrics_v5`**, and the **outer query has no ticker filter** (`WHERE fmv.source='store_count'` scans *every* store-count row table-wide; only the subquery filters tickers).
Cause **B** — the **RATINGS_WARM_SWEEP** (user=-) runs concurrently, issuing 55–121 s `coreiq_filing_metrics_v5` scans that saturate the read pool. (Already disabled by default in the testing repo — not yet on STG.)
Cause **C** — this runs on the **criterion-apply path** (`recompute_working_set`), which has **no sticky loader** (the loader exists only on the "Show Results" grid path, `screening.py:4682`). → 56 s blank screen.

### 3. Additional Data → two separate cards
Each Additional Data checkbox (Credit Ratings, Store Counts) becomes its **own criterion** (Active Criteria #2 and #3). Confusing UX **and** doubles the slow v5 queries.

### 4. Result render / RAM
Results render via **AgGrid** (row-virtualized client-side). At 2000 rows it copes; if the cap is lifted naively to tens of thousands, the JSON payload + client memory would blow up — so any cap change must be paired with **windowing/pagination**.

---

## Fix plan (proposed — pending sign-off; screening = core product, behavior-sensitive)

### A. "All History" + the 2000 cap  → **date-windowed pagination + honest count**
- Show the **true total** (`SELECT COUNT(*)` over the filtered set — one fast indexed count) e.g. "≈34,000 events — showing newest 500."
- Page back through history with **keyset pagination on `event_date`** ("Load older") — newest-first, low RAM (N rows in memory), all history reachable. Mirrors the newsroom load-more already in the app.
- Default page 500 (tune for AgGrid). Never silently truncate.

### B. Additional-data query speed  → **precompute + fix the query**
- Add the missing **outer ticker filter** to `_fetch_additional_display_values` (correctness + big speedup).
- Precompute latest credit_rating / store_count **per ticker into a small cache table** (like the segment cache), refreshed by the same scheduled job → screening reads become instant (49-181 ms) instead of 11-38 s.
- Keep the RATINGS sweep **off** (already done) so it stops competing.

### C. Loader on the criterion-apply path
- Show the sticky branded loader during `recompute_working_set` too (not just the results grid) → no blank screen.

### D. Two cards → one
- Merge Additional Data selections into a single criterion card (UX only; matching logic unchanged).

### Testing (mandatory — ALL screening types, behavior must not change)
Key Developments (by category + by date-range + timeframe), Additional Data (credit ratings, store counts, both), financial criteria, geo/industry filters, saved criteria, watchlist-from-results, Excel download. Verify counts, ordering, and matched-company sets vs. current behavior on a fixed criteria set before/after.

---

## IMPLEMENTED 18-Jul-2026

**A. All-History pagination + honest count — DONE**
- `screening_service.py`: new `get_keydevs_events_count()`; `get_keydevs_events_for_tickers()` now takes `limit`/`before_date`/`before_id`, returns `(df, next_cursor)`, keyset seek on `(event_date, event_id)`. `LIMIT 2000` removed.
- `screening.py`: results render paginates (500/page), shows honest total ("N found · showing newest M"), "Load older" pager accumulating in session, resets on criteria change.
- **Tested (real STG):** all-history count 9,604 for 8 tickers (was capped at 2000); paged rows == COUNT exactly (completeness); 506 rows == 506 distinct event_id (no dups); timeframe (30d) + date-range (2025) modes correct; 7 display columns unchanged.

**B. Additional-data speed — DONE (ticker filter + precompute cache)**
- `_fetch_additional_display_values`: added the missing outer `fmv.ticker IN (...)` filter. But UI testing showed the credit-rating fetch was still **~12s on a cold buffer** (optimizer picks `idx_v2_source` and reads every credit-rating row) — buffer-dependent, not "instant."
- **Fix:** new precompute cache `coreiq_screening_additional_cache` (data_type, ticker, display_value) — latest value per ticker (~150 rows), read in <1ms. `_fetch_additional_display_values` / `_fetch_additional_tickers` read the cache first, live fallback only until built. `build_additional_data_cache()` wired into `refresh_segment_cache_if_stale` (same v5 `MAX(id)` gate).
- **Tested (real STG + UI):** built 31 credit + 123 store tickers in 9s; cache read <10ms server-side; values verified (WMT=10,955, COST=914, TGT=1,995, HD=2,359 == direct v5). In the live UI: additional criterion apply **0.84ms / 0.78ms** (cache hits), **no DB_SLOW** — vs the old 11.7s + 38.7s.

**C. Loader on criterion-apply — DONE**
- `_trigger_recompute()` now renders the branded overlay for every apply/edit/remove path (was only on "Show Results") → no more 56s blank.

**D. Merge two Additional Data cards — DEFERRED (behavior-sensitive)**
- Each additional type is a separate criterion threaded through creation → recompute → card display → results columns. Merging changes the criteria model, which the user requires to stay unchanged. Left for a scoped follow-up (recommended: display-only grouping that keeps the two criteria intact). No correctness/perf impact.

**Verification limits:** data layer proven against live STG (read-only); app boots + `/screening` renders with all changes, zero errors. Full authenticated click-through of the pager not run (local UI harness renders unauthenticated).
