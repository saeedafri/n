# STG-Log-Driven Performance Fixes — Design & Evidence

**Date:** 2026-07-03
**Input evidence:** real STG log `logs/server-logs-20260703_022556.log` (29 min window, 07:27–07:56 IST, 9,771 lines) + read-only STG DB probes (indexes, EXPLAIN, timed query shapes, row-parity checks)
**Constraint honored:** ZERO numbers change — every fix is transport/shape-level; result sets proven identical by probe before code was touched.

---

## 0. What the STG log showed (measured, not guessed)

| # | Symptom (user-visible) | STG evidence | Root cause |
|---|------------------------|--------------|------------|
| 1 | Earnings-calls keyword Excel "never completes" | `DB_search_transcript_windows_for_export.TOTAL = 82,801ms rows=10000` (cold; 10.5s warm) + unmeasured openpyxl build + auto-download iframe race | (a) styled openpyxl workbook: 60k+ cells with `wrap_text` = minutes for 10k rows; (b) the 1.2s poller fragment **destroyed the auto-download iframe before its `load` event fired** (the iframe also blocked on fonts.googleapis.com); job then marked `downloaded=True` → spinner forever, no file |
| 2 | Earnings calendar slow render | `EC_PAGE_TOOLBAR_AND_FILTER = 4708ms + 4321ms` in back-to-back reruns (R00113/R00116); data fetch itself only 166ms parallel / calendar component 20ms | 3 sequential DB round-trips (alert prefs, watchlists, ticker validation) between the parallel fetch and the calendar, paid twice per first-load (component-mount double render), amplified by concurrent DB-pool pressure from #1/#3 |
| 3 | Newsroom wide date range | `NEWSROOM_CHUNK_FETCH 92,926ms chunks=758 workers=6 av_rows=378,580 yf_rows=86,507 trim=none` | full-history range fanned out one query per ISO week since ~2012, materialized 465k articles in session RAM, then rendered ALL of them |
| 4 | Screening Show Results slow | `SCREENING_CRITERION_APPLIED 37,770ms idx=0 type=keydevs` → `CLICK->RENDER 37.84s` | ROW_NUMBER() window query dragged the fat `headline` TEXT through a disk-spilling temp table for ~90k rows, keeping 3,036. Reproduced on STG: 47.9s cold / 4.0s warm |
| 5 | Silent errors | 3 × `[STRUCTURED_ERROR]` ProgrammingError/OperationalError | queries referenced `company_ir_websites` (doesn't exist; real: `coreiq_ir_websites`), `ingested_at` and `payload_json` on `coreiq_av_financials_earnings` (real: `fetched_at_utc`, `raw_json`) |
| 6 | market_data Segments tab slow | `TAB_Segments 8591ms (date_range_fetch=5193ms)`; also `DB_get_non_sec_transcript_companies 60,243ms` for 10 rows (MAT MISS live build) | segment fetch = 1 + 2×N-years queries (~21 round-trips for AMZN) through a 6-worker pool; non-SEC list already materialized to persistent `/home/mdp-cache` (60s was the one-time MISS build during boot, concurrent with warmup) |

**Indexing audit (user asked):** all hot tables are already well indexed — AV news chunk uses `idx_av_time_id` (range), keydevs uses `idx_ticker_cat`, non-SEC uses covering `idx_v2_doc_type_lookup`, transcripts export pass-2 uses PRIMARY. **No missing index was the root cause.** The DB is an Azure MySQL with `innodb_buffer_pool_size = 2GB` against ~30GB of hot tables — our own query storms (758-chunk fan-out, 10-worker export, boot warmup all concurrent) evict the buffer pool and every small query pays cold-read + pool-wait. Fixing the storms fixes the "everything is slow" feeling.

**"Multiple restarts why?" (SIP log `~/Downloads/server-logs-20260703_022505.log`):** SIP's box hit `min avail = 172MB` (max RSS 3,563MB) with the same 220311×21 frame held 6+ times; silent death 07:10→07:44 + consecutive-pid respawn = Linux OOM-killer/platform recycle, not a Python crash. **MDP in the same window was healthy** (avail ≈ 2GB, 1 boot). SIP fix is a separate repo — flagged, not touched.

---

## 1. Fixes (all in this repo; no data, no schema, no auth changes)

### 1.1 Earnings-calls export → CSV (SIP approach) — `app/pages/earnings_calls.py`
- **Deepest root cause (found during UI verification):** the job registry was a
  module-level dict, but `st.Page` re-executes the page file in a fresh module
  namespace on every script run — the registry (and the finished bytes) were
  **wiped on every rerun**, so no run could ever deliver the file. The old
  Excel flow could never complete on STG regardless of build speed. Fixed by
  moving the registry into `@st.cache_resource` (process-lifetime), proven by
  the `EC_EXPORT_STATE` log trail flipping `job=none → job=ready` across reruns.
- New `_build_keyword_results_csv()` — same 6 columns (`Company, Ticker, Year, Quarter, Reporter Name, Whole Paragraph`), same rows, UTF-8 BOM so Excel opens it; builds in ms where openpyxl took minutes.
- Background job unchanged (module-level registry survives reruns) but now builds CSV; logs **`EC_EXPORT_JOB_TOTAL | rows= fetch_ms= csv_ms= bytes=`**.
- Delivery: `st.download_button` served by Streamlit's media manager over HTTP — replaces the multi-MB base64 iframe + JS auto-click. The poller now marks the job done and issues ONE `st.rerun(scope="app")` so the spinner swaps to a persistent **Download CSV** button. No race, no external font dependency.
- Single-transcript export path converted to the same CSV + download_button.
- Button label "▦ Excel" → "▦ CSV"; spinner/toast text updated honestly.

### 1.2 Earnings calendar toolbar — `app/pages/earnings_calendar.py`
- The three toolbar lookups now ride the existing parallel prefetch pool (workers 4→7): `_fetch_alert_prefs_raw` (pure-DB `load_preference`, no session_state from worker), `_fetch_toolbar_watchlists` (`get_user_watchlists` is worker-safe), `_warm_candidate_company` (warms `get_company_by_ticker` st.cache_data so `validate_and_get_ticker` is a hit).
- Toolbar consumes the prefetched results; `_load_existing_alert_preferences` gained an optional `saved_row` param (sentinel-guarded; all other callers unchanged).
- New logs: **`EC_PAGE_FETCH_ALERT_PREFS`**, **`EC_PAGE_FETCH_WATCHLISTS`**.
- The component-mount double render still happens (streamlit_calendar behavior) but each render's toolbar cost collapsed (verified 12ms locally).

### 1.3 Newsroom wave loading — `app/pages/newsroom.py`
- Wide ranges load in **waves of 8 ISO weeks**, newest-first ("Earliest" sort → oldest-first), stopping once a page (≥1000 raw rows) is loaded; a centered **"Load older articles (N more sections)"** button fetches the next wave. Default 7-day range = 1 wave → byte-identical behavior.
- Prefetch futures fire for wave 0 only (was: the full range — the 92.9s bomb started before the page even rendered).
- Sort flip on a **partially** loaded range refetches from the other end (guard `NEWSROOM_SORT_FLIP_REFRESH`) — a fully loaded range keeps today's client-side re-merge.
- Count line stays truthful: "All N articles loaded" only when complete; else "N most recent articles loaded (D days selected)".
- New logs: **`NEWSROOM_WAVE_PLAN`** (waves, range, direction), **`NEWSROOM_WAVE_FETCH`** (per-wave rows + cumulative), **`NEWSROOM_LOAD_STATE`** (loaded_waves/has_more), **`NEWSROOM_LOAD_MORE`**.

### 1.4 Screening keydevs — `app/data/screening_service.py`
- Late row-lookup rewrite: ROW_NUMBER() windows over narrow `(event_id, event_date)` (index-only from `idx_ticker_cat`), then PK-join back for `headline` of only the surviving ≤10/ticker rows.
- **Proven on STG:** identical result sets (3,036 rows, multiset-compared), 47.9s cold / 4.1s warm → 2.7s worst case; end-to-end `apply_keydevs_criterion` smoke: 2.0s with headlines, 399ms without.

### 1.5 Broken SQL — `app/data/repository.py`
- `get_earnings_date_pair_for_fiscal_quarter_ends`: `ORDER BY ingested_at` → `fetched_at_utc`; `payload_json` → `raw_json` (columns verified against STG information_schema).
- `_build_ir_website_lookup`: dropped the phantom `company_ir_websites` fallback (error 1146 on every lookup).

### 1.6 Segment fetch one-shot — `app/data/repository.py`
- `_fetch_all_db_rows`: 1 + 2×N-years queries → **3 queries total** (dim + ndim with `report_fiscal_year IN (...)`); the existing deterministic final sort makes input order irrelevant.
- **Proven on STG (row multisets identical):** AMZN 9,493 rows 14.2s→4.3s · COST 7,261 7.4s→4.7s · AAPL 6,526 7.7s→3.8s · WMT 8,304 6.8s→4.2s (from local; on STG the win is larger — 21 RTTs → 3).
- New log: **`DB_SegmentDataRepository._fetch_all_db_rows | rows= ticker=`** (closes the segment DB blind spot).

---

### 1.7 Newsroom FULL-RANGE keyword search — `app/pages/newsroom.py` + `app/data/repository.py`
**Business requirement (03-Jul):** a keyword must search the ENTIRE selected date
range (not just loaded articles), fast, with low RAM.

- **Neither strategy alone survives the measurements** (all timed on STG):
  - FULLTEXT materializes a common token's whole doc list before the date
    filter: `'earnings'` over a **7-day** range = **124s**. Rare terms fly
    (`'walmart earnings'` over 11.5y = 1.1s).
  - A LIKE scan down the (time,title) ICP index early-exits for common terms
    (`'earnings'` 7d = **676ms**, 183× vs FT) but never exits for rare ones
    (`'walmart earnings'` on one recent year = 30s+, aborted).
- **Chosen: range-gated router (final).** The two primitives have opposite
  failure modes, so the route depends on range width AND term density:
  - **NARROW range (≤ `_KW_NARROW_MAX_DAYS` = 366d): LIKE only.** The ICP scan
    is window-bounded → fast for ANY term. FT is *never* used here: MySQL builds
    a prefix term's whole doc-list ignoring the date filter, so a 7-day
    `+tariff*` still scans 11 years (measured **20s** in the first UI run — the
    bug this gate fixes). This covers the default 7-day view and all common
    ranges.
  - **WIDE range (> 366d): 14-day LIKE density probe routes** — DENSE (≥10
    recent hits) or <3 chars → LIKE (early-exits at the limit) with an **FT
    fallback** if LIKE hits its 6s execution cap (a term dense-recently but
    sparse-over-the-full-range, esp. "Earliest" sort); SPARSE → FT (rare term =
    tiny doc-list = fast).
- **Why no slicing (a wrong turn I backed out):** slicing an FT query by date
  does NOT shrink its cost — MySQL builds the term's global doc-list before the
  date filter — so per-slice FT is not cheaper, and LIKE already early-exits.
  Single capped queries (LIKE 6s, FT 15s) are simpler and strictly better; AV+YF
  run in parallel; `LIMIT` bounds RAM to ~3k articles at any range width.
- **Correctness** (verified on STG *before* connectivity dropped, via the real
  `_keyword_search_serverside` over 7 archetypes × both sort directions): every
  returned title contains the keyword, ordering monotonic newest-/oldest-first,
  counts bounded. Log line:
  **`NEWSROOM_KEYWORD_SEARCH | kw= mode= probe_hits= range_days= av= yf= capped=`**
  (`mode` ∈ LIKE_NARROW / LIKE_WIDE / FT_WIDE).
- New repo methods `search_av_articles_by_title` / `search_yf_articles_by_title`
  (deferred-join / news_id-dedup with merged tickers; `st.cache_data` ttl=1800;
  `MAX_EXECUTION_TIME(30000)` guard; logs `SELECT_AV_TITLE_LIKE` /
  `SELECT_YF_TITLE_LIKE`) + a FULLTEXT keyword branch in `get_yf_articles`
  (`SELECT_YF_KEYWORD`); AV FT reuses `get_articles` PATH A (expression now
  '+word*' prefix terms). Page helper `_keyword_search_serverside` probes,
  routes, slices, runs AV+YF in parallel, caps at 1000 AV + 2000 YF (~3k
  articles max in RAM), logs
  **`NEWSROOM_KEYWORD_SEARCH | kw= mode= probe_hits= slices= av= yf= capped=`**.
- Keyword mode replaces the browse feed: wave loader is skipped while a search
  is displayed (no wasted background fetches); watchlist/category filters still
  apply client-side on the matches; count line reads
  "N matching articles in the selected range (D days searched)". Left-panel
  result click → scroll+highlight of the article card works unchanged (verified:
  `data-nws-to` → `article-{idx}`, highlight rgba flash observed via JS).

## 2. Verification (local app + staging DB, real UI via Playwright)

Suite: `scratchpad/verify_fixes.py` (screenshots in `scratchpad/shots/`).

| Check | Result |
|---|---|
| ecal renders; alert-prefs/watchlists prefetch logged; toolbar span | **PASS — 12ms** (STG was 4,300–4,700ms ×2) |
| ecal event counts unchanged | **PASS — "462 events"** (matches STG screenshot exactly) |
| newsroom default 7d: full load, waves=1, count line unchanged | **PASS — "All 4,834 articles loaded (8 days)"** |
| newsroom 2015→2026 range: 76 waves planned, 1 loaded, has_more=True | **PASS** (was 758 chunks / 92.9s / 465k rows) |
| newsroom truthful partial count + Load-older button + wave load-more | **PASS** |
| earnings_calls cross-search → CSV button → background build (`EC_EXPORT_JOB_TOTAL 2,121ms rows=14 fetch=2,117 csv=4 bytes=78,326`) → Download CSV appears → file downloads with correct header + 14 data rows | **PASS (9/9)** |
| earnings_calls export-state trail across reruns | **PASS — `job=none → job=ready` persists** (registry fix) |
| market_data segment tab renders; `DB_SegmentDataRepository._fetch_all_db_rows 4,785ms rows=9,493` (local incl. trans-ocean RTT; ~1-2s expected on STG) | **PASS** |
| keydevs service-level: 37.8s → 2.0s (headlines) / 0.4s (filter-only), identical rows | **PASS** |
| STG row-parity probes (keydevs, segment ×4 tickers) | **PASS — IDENTICAL=True everywhere** |
| newsroom keyword search — `_keyword_search_serverside` correctness (7 archetypes × both sorts): titles contain keyword, monotonic order, bounded counts | **PASS** (before STG dropped) |

### 2a. Keyword-search router — VERIFIED ✅ (real UI, `verify_kw_search.py` 12/12)
The keyword router was redesigned after the first UI run exposed the narrow-range
FT trap (`tariff` on the default 7-day view = **20s**). Final design (range-gate:
narrow→LIKE-only; wide→density-probe LIKE/FT; single capped queries; no slicing).
STG dropped mid-session (Azure err 2003) then recovered; re-verified end-to-end:

| Check | Result |
|---|---|
| default-range search (`earnings`/`ai`) | **`mode=LIKE_NARROW`, 1.6–2.3s** (was the 20s FT trap) |
| offline router unit tests (mode per range/density, FT-fallback) | **7/7 PASS** |
| helper correctness on STG (7 archetypes × both sorts) | titles match, order monotonic, counts bounded — **PASS** |
| wide-range `tariff` (2015→2026) | `mode=FT_WIDE`, 1,145 matches across years, 11s cold / 4.5s warm |
| full-range count line + older-than-loaded results + click→scroll-highlight | **PASS** (A2–A6) |
| sort-flip re-search · clear-keyword browse-restore · 2-char server-side | **PASS** (D1, E1, C1) |

Real-app numbers (server log): narrow `LIKE_NARROW` 1.6–2.3s; wide `FT_WIDE`
11s cold → 4.5s warm (cached 5 min). The 20s narrow-FT trap is eliminated.

## 3. What to grep on STG after you push

```
grep -E "EC_EXPORT_JOB_TOTAL" server-log.log                  # export: fetch vs csv split
grep -E "EC_PAGE_TOOLBAR_AND_FILTER|EC_PAGE_FETCH_ALERT" ...  # calendar: expect ~100-400ms
grep -E "NEWSROOM_WAVE_PLAN|NEWSROOM_LOAD_STATE" ...          # newsroom browse: waves + bounded loads
grep -E "NEWSROOM_KEYWORD_SEARCH" ...                         # search: mode=LIKE_NARROW sub-s on default; no 20s FT on narrow
grep -E "SELECT_AV_TITLE_LIKE|SELECT_YF_TITLE_LIKE" ...       # keyword LIKE path timings
grep -E "SCREENING_CRITERION_APPLIED" ...                     # keydevs: expect <3s worst case
grep -E "DB_SegmentDataRepository._fetch_all_db_rows" ...     # segment: expect ~1-2s
grep -c "STRUCTURED_ERROR" ...                                # expect 0 of the 3 fixed shapes
```

## 4. Explicitly NOT done (flagged, out of scope)
- SIP memory duplication / OOM restarts (different repo).
- Quarterly segment path fan-out (annual only; quarterly untouched).
- DB DDL of any kind (no new indexes needed — audit says plans are fine once the storms are gone).
- `_get_all_cross_search_results_for_excel` (dead cached wrapper) and `_render_excel_js_download` left in place, unused.
