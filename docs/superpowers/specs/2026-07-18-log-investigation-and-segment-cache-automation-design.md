# STG Log Investigation + Segment-Cache Automation — Design & Findings
**Date:** 2026-07-18
**Log analyzed:** `server-logs-20260718_134355.log` (17-Jul 22:42 → 18-Jul 19:13 IST, ~20.5h, 7,997 lines)
**Env analyzed:** `docs/envs` (live STG container env dump, 154 vars)
**DB probed:** STG `coreiq_filing_metrics_v5`, `coreiq_screening_segment_values_cache` (read-only, over VPN)

---

## 1. RAM — end-to-end verdict

| State | RSS | Notes |
|-------|-----|-------|
| Steady (users browsing) | **~540–660 MB** | HEALTHY. `[MALLOC_TRIM]` reclaims 36–90 MB every 60 s heartbeat. `MALLOC_ARENA_MAX=2` holds arena ~250 MB. |
| **RATINGS warm sweep** | **spike → 3,678 MB** | avail dropped to **95 MB**, **swap=65 MB** engaged → near-OOM. |

**Root cause of the spike (proven):** rerun `R00006` = `cache_manager.py:522 _background_warmup_thread → RATINGS_warm_sweep_start`. MEM_REPORT during the spike (03:07→03:13, 18-Jul):
- `FinancialFact=257,408 → 432,983`, plus `Fact`, `PresentationNode`, `TextNode`, `NavigableString`, `Tag`, `XMLAttributeDict` — **edgartools/BeautifulSoup XBRL parse objects**.
- `py_objects 2.28M → 3.52M`, `dict shallow 512 MB → 831 MB`.
- Driven by repeated `SELECT ... FROM coreiq_filing_metrics_v5 WHERE source IN ('credit_rating', …)` + per-ticker concept fetches, each parsed with edgartools.

**Amplifier (proven):** **two app processes** run concurrently — `pid=1889` (serves users) + `pid=1890` — and **both** ran their own RATINGS sweep (log shows sweep-start on each pid). Both heartbeat in the same window (6+6 beats 03:10–03:15). Cause: `PYTHON_ENABLE_GUNICORN_MULTIWORKERS=true`. Two copies × 3.7 GB potential = the OOM risk.

## 2. Bottlenecks (ranked, evidence-based)

1. **RATINGS_WARM_SWEEP** — 3.7 GB RAM driver (edgartools XBRL). Lever exists: `RATINGS_WARM_SWEEP=0` (`cache_manager.py:507`).
2. **Duplicate worker** — 2nd Streamlit process wastes baseline RAM + runs a 2nd sweep. Streamlit is single-process by design; a gunicorn worker cannot share sessions. Fix: `PYTHON_ENABLE_GUNICORN_MULTIWORKERS=false`.
3. **Cold-buffer DB scans on huge tables** (DB-side, not app code):
   - `coreiq_company_events` cache-signature `MAX/COUNT` → **14.8 s**.
   - v5 concept fetch (`numeric_value,…,dimension_member_label`) → **3–12 s** during sweep.
   - newsroom `SELECT DISTINCT primary_topic_v1` → **5.4 s** (page `FILTER_WIDGETS=5,378 ms`).
   - v5 `SUM(LENGTH(value))` credit-rating scans → 1.9–3.2 s.
4. **Segment cache 24 days stale** (see §4).
5. **`SECRET_KEY` unset** — the only 5 `ERROR` lines (`config.py:249`); session-security default. Not in `docs/envs`.

Warm user renders are FINE: home 0.01–0.03 s, market_data 0.03–0.2 s, company_filings 0.04–0.5 s warm (2–4.3 s cold), newsroom 5.6 s only on the topic-DISTINCT path.

## 3. New STG envs review (`docs/envs`)

**Correctly set (good):** `MALLOC_ARENA_MAX=2` ✓ (working — arena capped), `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, `EDGAR_CACHE_DIR=/home/edgar_cache`, `FORECAST_EMAIL_TEST_MODE=0`, `FORECAST_EMAIL_RECIPIENTS`(5), `FROM_EMAIL=dataautomation@…`, `PAGE_MATERIALIZE=1`, `SERVER_TRIM_ON_HEARTBEAT=1`, `SERVER_RAM_CENSUS=1`, `MPLBACKEND=Agg`. Forecast auto-refresh confirmed running on last boot.

**`APP_*_STARTED` / `WARM_ON_BOOT` flags** appear in the dump but have **no `APPSETTING_` twin** → they are **runtime-set** (the app sets them via `os.environ` after starting threads) — INNOCENT, working as intended.
> ⚠️ **Do NOT let IT add these as Azure App Settings.** They are skip-guards (`main.py:453/515/567/581/612/660/681/699`). As real App Settings pre-set to `1`, every guard trips on boot → scanner, warmups, screening prewarm, earnings-alert dispatch, **and forecast auto-refresh would all be disabled.**

**Problems:** `PYTHON_ENABLE_GUNICORN_MULTIWORKERS=true` (→ 2 processes), `SECRET_KEY` missing.

## 4. Segments / Screening cache

### What it is
- Source: **`coreiq_filing_metrics_v5`** — **14.4 M rows, ~23 GB** (13.4 GB data + 9.9 GB index). Timestamp col is `data_insert_timestamp` (**not indexed**).
- Screening reads two precomputed **DB** caches (fast, indexed):
  - `coreiq_screening_segment_values_cache` — **41,290 rows / 248 tickers / FY2010–2026**. Read 49–181 ms.
  - `coreiq_screening_segment_member_cache` — business 2,041 / geo 240 members. Dropdown read ~49 ms.
- Build: `build_segment_values_cache()` — replays the Segments-tab classifier over ~349 tickers in chunks of 40 (`WHERE is_dimensioned=1 AND doc_type='10-K'`, ticker-indexed), full-replace + member refresh. Runs in a daemon thread via `rebuild_segment_values_cache_async()` (lock-guarded).

### Why it's "slow" / stale
- **Reads are fast.** The **build** is what's heavy (~minutes: 349 tickers × indexed fetch + classify + replace).
- **Refresh is purely reactive:** `rebuild_segment_values_cache_async()` only fires when `total_rows == 0` (`screening_service.py:2828`). The cache is a **persistent DB table** → it never empties → **it never auto-refreshes.**
- **Evidence:** values cache `last_built = 2026-06-24`, member cache `2026-06-27` → **~24 days stale** today (18-Jul). Any 10-K filed since is missing from the screener. No cron/interval refresh exists anywhere.

### Data cadence
Segment data derives from **10-K annual filings** → new data per company **~once/year**, clustered around fiscal year-ends (heaviest Feb–Apr for Dec-FY US filers). Daily/weekly new-segment volume is near-zero most of the year, a handful of companies during peak. **Monthly rebuild is sufficient for correctness.**

### DB vs Persistent storage — which is faster?
The app already runs **both** and splits them correctly:
- **DB** (indexed) — best for **selective** reads (segment_type + metric + year + member). Measured 49–181 ms. Low RAM, shared across processes, survives deploy.
- **Persistent disk** (`materialize.py` → `/home/mdp-cache`, pickled+gzip DataFrames, freshness via cheap signature) — best for **whole-dataset** reads. Measured `screening_universe` HIT = **0.07 s**. Used for calendar/universe frames.

**Verdict for the segment cache:** keep it in **DB** (selective/indexed/low-RAM/shared). Persistent disk would force loading all 41 K rows into RAM to filter in pandas — worse for the low-RAM goal. **The fix is not storage; it's scheduling.**

### Automation design (IMPLEMENTED 18-Jul-2026)
Mirrors `forecast_auto_refresh.py`. Files:
- `app/utils/segment_cache_auto_refresh.py` — daemon thread `start_segment_cache_auto_refresh()`.
- `app/data/screening_service.py` — `refresh_segment_cache_if_stale()` + `coreiq_screening_segment_refresh_state` (single-row state table).
- `app/main.py` — start block guarded by `APP_SEG_CACHE_REFRESH_STARTED`, next to the other auto-refreshers.

1. **Trigger:** one daemon thread, one-per-process guard flag; poll `SEGMENT_CACHE_REFRESH_POLL_SEC` (default 6 h), first-delay `SEGMENT_CACHE_REFRESH_FIRST_DELAY_SEC` (default 300 s, off the cold-boot window). Disable with `ENABLE_SEGMENT_CACHE_AUTO_REFRESH=0`.
2. **Cheap freshness gate (O(1)):** `SELECT MAX(id) FROM coreiq_filing_metrics_v5` (PK seek; `MAX(id)=86,623,186` on 18-Jul). Persist `last_built_max_id` in the state table. Rebuild only when it grew (first run: `last=0` → one rebuild, then only on new ingest). `data_insert_timestamp` is unindexed → deliberately not used.
3. **Exactly-once across instances:** atomic conditional `UPDATE … SET building=1 WHERE building=0 OR building_since < NOW()-INTERVAL 3 HOUR` — rowcount 1 = winner. Plus the in-process `_segment_cache_build_lock`. A crashed build's claim self-releases after 3 h.
4. **Build = reused `build_segment_values_cache()`**, called synchronously in the daemon thread. **No classification change.**
5. **Zero-downtime publish:** the build's final write step now stages into `*_staging` tables (`CREATE … LIKE` live) and **atomically `RENAME`s** both live tables in one statement, then drops `*_old`. Readers see the whole old cache or the whole new one — never the ~20–60 s partial state the old in-place `DELETE`+`INSERT` exposed once the rebuild became periodic. Rows written are byte-for-byte identical (same columns/VALUES/ON DUPLICATE KEY dedup).
6. **RAM:** build holds ~41 K tuples (~10 MB) — SAFE (unlike the ratings sweep).
7. **Manual override:** existing admin button in `logs.py` unchanged (now also benefits from the atomic swap).

**Verification (real STG DB, no live cache touched):** all 5 refresh paths (built/fresh/in_process/claimed_elsewhere/stale-reclaim) ✓; daemon starts+fires ✓; end-to-end build+swap on 2 tickers into throwaway tables → complete table, staging/old cleaned, idempotent ✓; app boots, `/screening` + `/home` render, 0 ratings-sweep starts ✓.

## 5. Recommendations → hand to IT (App Settings)

| Setting | Action | Why |
|---------|--------|-----|
| `PYTHON_ENABLE_GUNICORN_MULTIWORKERS` | **`false`** | Kill the 2nd process + its duplicate ratings sweep. #1 RAM fix. |
| `RATINGS_WARM_SWEEP` | **`0`** (or keep + accept 3.7 GB spikes) | Disables the edgartools RAM driver. Decide if credit-ratings pre-warm is worth it. |
| `SECRET_KEY` | **set a random 32+ char value** | Clears the 5 ERRORs; real session security. |
| `APP_*_STARTED`, `WARM_ON_BOOT`, `*_WARMED` | **never add as App Settings** | They are runtime skip-guards; as App Settings they disable all background work incl. forecast email. |
| v5 `data_insert_timestamp` index | data-team, optional | Only if ingest-time analytics ever needed; not required by this design (we use `MAX(id)`). |

**Code change (this repo):** add the scheduled segment-refresh thread per §4. Everything else is config handed to IT.
