# MDP Database Access Optimization Design

**Date:** 2026-07-02  
**Scope:** CapIQReplacement (Coresight Market Data Portal)  
**Reference:** SIP `materialize.py`, `shared_data.py` parallel-fetch patterns

## Goals

1. Eliminate unbounded full-table fetches where filters can be pushed to SQL.
2. Use `execute_query_readonly` (AUTOCOMMIT, 1 RTT) for all read paths.
3. Cache expensive reference data with `@st.cache_data` and disk materialization.
4. Parallelize independent queries via `ThreadPoolExecutor` without connection storms.
5. Remove direct `pymysql` bypasses — route all DB I/O through `db_manager`.

## Patterns Applied

### Read engine (`app/core/database.py`)

| Method | RTTs (Azure) | Use |
|--------|--------------|-----|
| `execute_query_readonly` | 1 | All SELECT |
| `fetch_one` / `fetch_all` | 1 | Thin wrappers over readonly |
| `get_session()` | 3 | Multi-statement writes needing rollback |
| `execute_insert` / `execute_update` | 1 | Single-statement AUTOCOMMIT writes |

### Disk materialization (`app/utils/materialize.py`)

- Signature = `(MAX(signal), COUNT(*))` per source table in one UNION query.
- Atomic gzip-pickle write; stale signature → live rebuild.
- Toggle: `PAGE_MATERIALIZE=1` (default on).
- Cache dir: `MDP_CACHE_DIR` → `/home/mdp-cache` → `server-logs/agg-cache`.

**Applied to:**

| Frame | Source tables | Cold-start benefit |
|-------|---------------|-------------------|
| `screening_universe` | `coreiq_companies` | ~1 RTT vs full companies scan per cold boot |

### Memory optimization (`app/utils/mem_opt.py`)

- `optimize_frame_memory()` on screening universe (categorical encoding).
- `release_memory()` + `malloc_trim` after heavy builds.

### Parallel fetch (SIP `use_pure=True` pattern)

Already in place (do not stack concurrent page warms):

| Location | Parallel queries | Workers |
|----------|------------------|---------|
| `EarningsCalendarRepository.get_calendar_events` | events UNION + IR + FYE + companies map | 4 |
| `earnings_calendar.render_page` | tickers + date range + events + M&A | 4 |
| `NewsRepository.get_news_date_range` | AV/YF MIN/MAX | 4 |
| `screening_service` segment cache rebuild | per-segment-type shards | env-tunable |

Boot warmup (`cache_manager._background_warmup_thread`) runs **sequentially** to avoid connection storms.

### Caching layers

| Function | TTL | Notes |
|----------|-----|-------|
| `CompanyRepository.get_companies_rows` | 1h | Screening universe input |
| `get_base_company_universe` | materialized + mem_opt | Disk + RAM |
| `NewsRepository.get_sectors` | 6h | Date-independent dropdown |
| `EarningsCalendarRepository.get_calendar_events` | 5m | `@st.cache_data` |
| `retailer_adding.load_full_table` | 30m | SEC/NON_SEC union sources |
| `retailer_adding.build_union_df` | 30m | Bulk-upload reference frame |

## Before / After Query Patterns

### Screening base universe

**Before:**
```sql
SELECT ticker, name, ... FROM coreiq_companies;  -- every cold page load
```

**After:**
```python
materialized_or_build("screening_universe", _build_universe,
    [{"table": "coreiq_companies", "signal": None}])
# → disk hit ~1-2s on restart; signature invalidates on row change
```

### Retailer adding reads

**Before:**
```python
pymysql.connect(STG_DB_*) → cur.execute("SELECT * FROM coreiq_companies LIMIT %s")
```

**After:**
```python
db_manager.execute_query_readonly(
    "SELECT * FROM coreiq_companies LIMIT :limit", {"limit": limit})
```

### Retailer adding writes

**Before:**
```python
conn = pymysql.connect(...); cur.execute(...); conn.commit()
```

**After:**
```python
with db_manager.get_session() as session:
    session.execute(text("UPDATE ..."), params)  # auto-commit on exit
```

### Earnings calendar page load

**Before:** 4 sequential repository calls (~16s cold).

**After:** `ThreadPoolExecutor(max_workers=4)` in `render_page` + internal parallel in `get_calendar_events` → wall time ≈ slowest query (~8.8s).

### News date range

**Before:** 4 sequential MIN/MAX queries.

**After:** 4 parallel `execute_query_readonly` via `ThreadPoolExecutor(max_workers=4)`.

## Files Changed (2026-07-02)

| File | Change |
|------|--------|
| `app/utils/materialize.py` | New — SIP-style persistent frames |
| `app/utils/mem_opt.py` | New — categorical RAM shrink |
| `app/data/screening_service.py` | `materialized_or_build` on universe |
| `app/utils/cache_manager.py` | Sequential warmup, `WARM_ON_BOOT`, process guard |
| `app/pages/retailer_adding.py` | pymysql → `db_manager`, `@st.cache_data` on schema/union |
| `app/components/base_page.py` | Rerun tracing bootstrap |
| `app/utils/server_logger.py` | `[DB_QUERY]`, `[TIMING]`, `PageLoadTracker` |
| `app/pages/earnings_calendar.py` | Date-windowed `get_calendar_events`; `datesSet` nav reruns |
| `app/pages/company_filings.py` | SEC dropdown from `coreiq_companies` (no v5 DISTINCT) |
| `app/data/screening_service.py` | Segment universe: companies master only, fail-fast |
| `app/utils/cache_manager.py` | EC 42-day warmup; forecasting + filings dropdown warm |
| `app/data/repository.py` | `get_companies_with_earnings` + `get_non_sec_transcript_companies` disk materialization |
| `app/pages/earnings_calls.py` | `log_render_complete`, `EC_FETCH_COMPANIES` cold/warm timing |
| `app/main.py` | `MALLOC_ARENA_MAX=2` setdefault before any heap alloc |
| `app/utils/server_logger.py` | Production env defaults via `setdefault` + `[BOOT]` confirmation |

## Earnings Calls Optimization (2026-07-02 — HIGH priority RESOLVED)

### Problem

`EarningsCallRepository.get_companies_with_earnings()` ran `SELECT DISTINCT ticker FROM coreiq_av_earnings_call_transcripts WHERE has_transcript = 1` on every `@st.cache_data` cold miss (~23s on Azure cold buffer pool). Boot warmup ran earnings last (Track 4), so first user often paid full penalty.

### Fix

1. **Disk materialization** (`materialized_or_build`):
   - `earnings_transcript_tickers` — DISTINCT tickers DataFrame, signature from `coreiq_av_earnings_call_transcripts`
   - `non_sec_transcript_companies` — DISTINCT ticker+name from `coreiq_filing_metrics_v5` transcript doc types
2. **Warmup reorder** — earnings SEC+non-SEC company lists moved to Track -1 (first in `_background_warmup_thread`)
3. **Observability** — `EC_FETCH_COMPANIES` timing with `warm=True/False`; `log_render_complete("earnings_calls")`

### Before / After

| Metric | Before | After (local STG 2026-07-02) |
|--------|--------|------------------------------|
| Cold `DISTINCT ticker` query | ~23,000ms (Azure audit) | 312ms (live build) |
| Cold `get_companies_with_earnings.TOTAL` | ~23,000ms | 896ms (incl. mat write) |
| Warm `EC_FETCH_COMPANIES` (post-cache) | N/A | 0.26–0.66ms |
| `[CLICK->RENDER] earnings_calls` | N/A | 1.08s (incl. transcript render) |
| Disk mat on restart | N/A | `[MAT][earnings_transcript_tickers] HIT` ~1–2s expected |

## Local Benchmark Table (2026-07-02 Playwright run)

Server: `bash .claude/dev/run_local.sh` (LOCAL OIDC bypass, STG DB). Screenshots: `/tmp/rpt_*.png`.

| Page | Load time (`[CLICK->RENDER]` or PAGE_LOAD) | Reruns | RAM | Status | Notes |
|------|---------------------------------------------|--------|-----|--------|-------|
| `/home` | ~0.5s (est.; no CLICK->RENDER) | 3 | `?` (macOS) | OK | Dashboard + company selector rendered |
| `/market_data` | 3.42s SLOW | 4 | `?` | OK | AMZN profile; price_history 2.9s |
| `/newsroom` | ~3s (est.) | 2 | `?` | OK | Filters rendered; articles loading |
| `/earnings_calls` | **1.08s** | 4 | `?` | OK | 296 SEC + 10 non-SEC companies; AMZN Q4 2006 transcript |
| `/earnings_calendar` | **0.04s** warm | 6 | `?` | OK | 459 events, 42-day window cached |
| `/screening` | 2.63s SLOW | 5 | `?` | OK | Segment cache hit 312ms |
| `/company_filings` | 1.61s | 6 | `?` | OK | AMZN 10-Q-Q1 2026 from `coreiq_companies` |
| `/forecasting` | ~3s (est.; FORECASTING_get_companies 680ms) | 1 | `?` | OK | 337 companies; FLWS dashboard loaded |

**Boot defaults confirmed:**
```
[BOOT] ... MALLOC_ARENA_MAX=2 | defaults_applied=[MALLOC_ARENA_MAX=2, SERVER_HEARTBEAT_SECS=60, SERVER_RAM_CENSUS=1, SERVER_TRIM_ON_HEARTBEAT=1, PAGE_MATERIALIZE=1, MDP_CATEGORICAL=1, WARM_ON_BOOT=1, APP_TIMING=1]
```

**Earnings calls evidence:**
```
[MAT][earnings_transcript_tickers] MISS → live build
[TIMING] DB_get_companies_with_earnings.distinct_tickers | 312.44ms | rows=296
[MAT][earnings_transcript_tickers] WROTE rows=296
[TIMING] EC_FETCH_COMPANIES | 898.03ms | sec=296 non_sec=10 warm=True
[TIMING] EC_FETCH_COMPANIES | 0.26ms | sec=296 non_sec=10 warm=True
[CLICK->RENDER] page=earnings_calls render=1.08s
```

## Remaining CRITICAL Items (2026-07-02 audit — RESOLVED)

| Item | Was | Fix applied |
|------|-----|-------------|
| `earnings_calendar` prefetch | `get_calendar_events(tickers=None)` — all-time UNION | Date-windowed fetch: 42-day month grid or Jan–Dec year; `datesSet` reruns on nav |
| `company_filings._load_companies_from_db` | `DISTINCT ticker` on `coreiq_filing_metrics_v5` (~7.75M rows) | SEC tickers from `CompanyRepository.get_companies_rows()` (`coreiq_companies`); NON-SEC blob merge unchanged |
| `company_filings._load_company_names_from_db` | Same v5 DISTINCT scan | Same `coreiq_companies` master via `get_companies_rows()` |
| `screening_service._segment_cache_universe` | Fallback DISTINCT on `filing_metrics_v5` | `coreiq_companies` only; `RuntimeError` if empty (no v5 scan) |
| `cache_manager` boot warmup | EC `get_calendar_events()` unbounded | 42-day window args; added `RevenueForecastService.get_companies` + `_load_companies_cached` |

### Still open (lower priority)

| Item | Risk | Recommendation |
|------|------|----------------|
| `retailer_adding.fetch_existing_business_keys` | Unbounded SELECT on business key cols | Acceptable (admin-only, ~2k rows); could cache 5m post-write invalidate |
| `NewsRepository._get_ticker_sector_map_impl` | Full `coreiq_av_companies_all` scan | Already 6h cache; consider covering-index-only SELECT |
| YFinance long-format pivots | Per-ticker `_fetch_all_annual_rows` | Already `@st.cache_data` 5m; N+1 avoided via cache |
| Screening Ratios bulk path | Per-ticker `RatiosRepository.get_ratios_data` when bulk misses | `_try_bulk_tabular_values` exists; extend coverage for more ratio types |
| `earnings_calendar` IR lookup | `EC_QUERY_IR_LOOKUP` 3.5s cold (missing `company_ir_websites` table on STG) | STG schema gap; guard/fallback already in place |
| `home` / `newsroom` / `forecasting` | Missing `log_render_complete` | Add for full `[CLICK->RENDER]` coverage |

## Testing Plan

1. `bash .claude/dev/run_local.sh` — LOCAL OIDC bypass unchanged.
2. Playwright: `/market_data`, `/screening`, `/earnings_calendar`.
3. Grep `server-logs/server-log.log`:
   - `[MAT][screening_universe]` HIT/MISS/WROTE
   - `[TIMING]` / `[DB_QUERY]` latency drops on rerun
   - `[CLICK->RENDER]` page totals
4. Retailer adding: verify load/edit path uses `db_manager` (no pymysql import).

### Verified 2026-07-02 (post CRITICAL fixes)

| Page | Result |
|------|--------|
| `/earnings_calendar` | 459 events rendered (197 earnings + 262 M&A); `EC_OPT_VISIBLE_RANGE` = `2026-06-28..2026-08-08` |
| `/company_filings` | Dropdown loads `Amazon.com, Inc. (AMZN)` from `coreiq_companies` master |
| `/screening` | `SCREENING_SEGMENT_OPTIONS_CACHE_HIT` 315ms; page renders |

**Earnings calendar `[TIMING]` before/after:**

| Metric | Before (unbounded) | After (42-day window) |
|--------|-------------------|----------------------|
| `EC_PAGE_FETCH_ALL_EVENTS` events | 15,980 | 197 |
| Cold `EC_QUERY_SQL_UNION` | ~17,800ms (all-time) | 11,115ms / 197 rows (`dates=2026-06-28..2026-08-08`) |
| Warm `EC_PAGE_FETCH_ALL_EVENTS` | 0ms (cached 15,980) | 0ms (cached 197) |
| `EC_PAGE_PARALLEL_TOTAL` (warm) | ~25–48ms | 1.03ms |

## Environment Flags

| Flag | Default (code) | Purpose |
|------|----------------|---------|
| `MALLOC_ARENA_MAX` | 2 | glibc arena cap — set in `main.py` before heap alloc |
| `SERVER_HEARTBEAT_SECS` | 60 | Background RAM/uptime heartbeat interval |
| `SERVER_RAM_CENSUS` | 1 | Live DataFrame census every 3rd heartbeat |
| `SERVER_TRIM_ON_HEARTBEAT` | 1 | `malloc_trim` after heartbeat |
| `PAGE_MATERIALIZE` | 1 | Disk materialization master switch |
| `MDP_CACHE_DIR` | (auto) | Persistent materialization directory |
| `MDP_CATEGORICAL` | 1 | Frame memory optimization |
| `WARM_ON_BOOT` | 1 | Background cache warmup |
| `APP_TIMING` | 1 | Pass `[TIMING]` lines through log filter |
| `OPENING_WORKERS` / `CLOSING_WORKERS` | N/A (SIP only) | MDP uses EC_WORKERS via repo internals |

All defaults applied via `os.environ.setdefault()` in `app/utils/server_logger.py` (and `MALLOC_ARENA_MAX` in `app/main.py`). Explicit Azure App Settings override code defaults.

## Rollout

- No schema migrations required.
- Safe to deploy: all optimizations are read-path or cache-layer; writes unchanged semantically.
- Set `PAGE_MATERIALIZE=0` or `WARM_ON_BOOT=0` to disable if DB pressure spikes during rollout.

### STG Deployment Checklist

1. **Azure App Settings** (optional — code defaults now apply if unset):
   - `MALLOC_ARENA_MAX=2`
   - `SERVER_HEARTBEAT_SECS=60`, `SERVER_RAM_CENSUS=1`, `SERVER_TRIM_ON_HEARTBEAT=1`
   - `PAGE_MATERIALIZE=1`, `MDP_CATEGORICAL=1`, `WARM_ON_BOOT=1`, `APP_TIMING=1`
2. **Persistent storage**: mount `/home/mdp-cache` for materialization; `/home/LogFiles/mdp` for logs
3. **Deploy** app package; restart worker
4. **Verify boot log**: `[BOOT] ... defaults_applied=[...]` in `/home/LogFiles/mdp/server-log.log`
5. **Verify warmup**: `[MAT][earnings_transcript_tickers] WROTE` or `HIT` within 90s of boot
6. **Smoke test pages**: `/earnings_calls` companies dropdown <5s cold; <500ms warm
7. **Monitor**: `[CLICK->RENDER]` SLOW flags, `[HEARTBEAT]` RSS, `[STRUCTURED_ERROR]` rate
8. **Rollback**: `PAGE_MATERIALIZE=0` + `WARM_ON_BOOT=0` via App Settings (no code revert needed)
