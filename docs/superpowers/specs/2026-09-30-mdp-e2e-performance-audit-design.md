# MDP end-to-end performance audit — root causes and fixes

Date: 2026-09-30 · Scope: every registered page, tab, filter and download.
Measured on a local server (`run_local.sh` settings, STG MySQL over VPN) at
`http://localhost:8511` — a separate port because another agent was restarting 8501.

## 1. How it was measured

- Playwright harness (scratch, not in the repo). No fixed sleeps: an action is done
  when Streamlit's own run-state flag (`stApp[data-test-script-state]`) is
  `notRunning`, no page loader is visible, and the DOM has been quiet for 600 ms;
  the reported time is the **last DOM change**, so the quiet window is never counted.
  A second metric, `content_ms`, ignores Streamlit's toolbar/status-widget fade-out
  (~150-200 ms that users do not perceive).
- Orphan browsers killed before every run. Cold = fresh process with
  `WARM_ON_BOOT=0`, first hit of each page (n=1). Warm = same process, n=3-5.
- Server time from the app's own `[CLICK->RENDER]`/`[TIMING]` log lines; code-level
  cause from a 10 ms stack sampler loaded via `PYTHONPATH` (`sitecustomize.py`,
  scratch only — no repo change).
- Downloads timed click → bytes on disk and validated (xlsx = zip, PDF = `%PDF`…`%%EOF`).

**Caveat — local DB latency overstates DB-heavy paths.** From this laptop the STG DB
is ~300 ms per round trip and ~2.1 s per new TLS connection. The STG app server
sits next to the DB, so per-query costs there are far smaller. Ratios hold; absolute
DB times do not. Users in India add ~0.23 s per round trip on top of server time
(~3 round trips per page load, 1 per interaction).

**Local-only confound (not the app):** another project injected a full-screen
`#sip-boot` splash into the shared Homebrew Streamlit `index.html` (adds 1.5-10 s
of fake "Loading…" locally). STG is unaffected; the harness strips it.

## 2. Root causes (ranked) and fixes

| # | Symptom | Proven cause | Fix | Risk |
|---|---|---|---|---|
| 1 | Filings **Download** never finished; the whole server froze (health check stopped answering) | SEC HTML has `<hr style="page-break-after:always">`; PyMuPDF `Story.place()` never advances past a forced break, so `generate_filing_pdf` emitted blank pages forever while holding the GIL (a 20 KB snippet made 43,120 pages in 26 s) | `utils/html_to_pdf.py`: strip `page-break-*`/`break-*` CSS (Story paginates itself) + a 3,000-page backstop that logs and stops | Low. Output is the same content; page boundaries come from Story |
| 2 | Forecasting 3.6-4.6 s on **every** render, and UPDATEs against STG on every view | `backfill_company_info` gated its once-per-process flag on `updated == 0`, but the driver rowcount counts *matched* rows; 8 tickers with `exchange=''` re-matched forever (400 identical UPDATEs per render). It also ignored `FORECAST_STORE_READONLY` | `data/forecast_refresh_service.py`: one pass per process; skip entirely when store writes are disabled | Low. Verified by read-only probe: the rewrites never changed data |
| 3 | Earnings Calls cold load 28-34 s | `_get_fiscal_year_end_map` ranked **every** `coreiq_yf_company_overview` snapshot with `ROW_NUMBER()` while carrying `payload_json` through the window (30 s for 75 rows); the page waited 10 s, gave up, then re-ran it serially | `data/repository.py`: latest-snapshot join via `idx_ticker_ingested` (identical map, verified 75/75) + persisted with `materialized_or_build` (`fiscal_year_end_map`, 465 tickers) | Low. Build raises on DB error so an empty/partial map is never persisted |
| 4 | Newsroom 4-5 s **warm**, 11-14 s cold | Server work was 159 ms. The page rendered all ~7,083 articles at once: 85,784 DOM nodes / 7.9 MB over the websocket | `pages/newsroom.py`: feed rendered in batches of 200 with "Show more articles" (same pattern the search panel already used), always ≥ the search panel's count so its scroll targets exist | Low-medium: long feeds need a click to extend |
| 5 | Newsroom cold 10.4 s in topic list; keyword search 7-10 s, returning **0 rows** | `get_yf_topic_keys` DISTINCT walks 571k index rows for 16 values. The YF title search picked `idx_yf_pub_nid_ticker` (no `title`), fetching every row in range; `MAX_EXECUTION_TIME(10000)` then returns an empty result silently | Topic keys persisted (daily refresh token, no table scan on read). `FORCE INDEX (idx_yf_time_title_icp)`: 3.1→0.9 s (8 d), 14.5→1.8 s (90 d) cold, identical rows | Low |
| 6 | Company Filings cold 17-47 s | Company dropdown waited for a parallel Azure Blob listing of the non-SEC tickers (one listing took 38 s) whose result could only add tickers already in the list | `pages/company_filings.py`: the scan runs in a background thread (it still warms the per-ticker blob cache) | Low. Dropdown contents identical |
| 7 | Segment tab cold 8.5-20.5 s per ticker | ~2k dimensioned + ~9k consolidated v5 rows per ticker (row-bound), cached only in process memory → every deploy re-paid it | `_fetch_all_db_rows` persisted per ticker (`segment_rows_<T>`), fresh-checked by that ticker's 10-K row count (index-covered); rows verified identical (10,815 for M) | Low. Failed builds raise, never persist "no data" |
| 8 | ~5.7 s of every 20 s of warm reruns | Every `read_materialized` ran a `COUNT(*)` freshness query — the screening universe is read on every rerun | `utils/materialize.py`: signature results reused for 60 s (`MAT_SIGNATURE_TTL_S`); writes always take a fresh one | Low: up to 60 s extra staleness on day-scale data |
| 9 | Screening Show Results ≈ 2.1-2.3 s warm | `time.sleep(0.6)` + an extra `st.rerun()` "to let the overlay paint" | Removed; the overlay is emitted before the compute in the same run | Low (verified results render) |
| 10 | Screening per-rerun DB calls | `_InMemoryWatchlistStore._db_ok` lives in the page script, so `SELECT 1` (3 round trips) ran every rerun; user watchlists cached 30 s | Reachability cached per process (`st.cache_resource`, failures not cached — uses the raising query); watchlist TTL 120 s (writes still clear it) | Low |
| 11 | Header/footer role checks on every render (~1.5 s per warm pass) | `get_user_role` uncached (a session cache was intended — set/delete popped a key nothing read) | 60 s process-wide memo, cleared immediately by `set_user_role`/`delete_user` | Low: role changes made elsewhere apply within 60 s |
| 12 | Earnings Calls 2.0-2.5 s warm | Transcript cached only 60 s; the transcript PDF (~0.7 s) was memoised per session, so every new visitor rebuilt it before render | Transcript TTL 1 h; PDF cached across sessions (`_cached_transcript_pdf`, 64 entries) | Low |
| 13 | Forecasting 2.0-2.5 s warm | `st.tabs` sent all 11 Plotly charts (869 KB) up front; 0.4-0.6 s browser long tasks | Lazy tabs (Streamlit 1.55 `on_change="rerun"` + `.open`): only the open tab's charts are built. Also removes the hidden-tab 700 px chart width | Low-medium: switching tabs is now a server round trip (0.3-0.65 s) |
| 14 | Logs page 3.2-3.6 s | `st.tabs` runs every tab body; the Segment Cache tab ran an uncached full-table aggregate | Cached 120 s; refreshed while a rebuild runs | Low |
| 15 | Screening first visit after boot | Segment cache table checks ran on the request path | Added to the existing boot prewarm thread in `main.py` | Low (same idempotent DDL, earlier) |
| 16 | Every new session ran Market Data / Home / Earnings Calls **twice** (+250-350 ms) | `streamlit_local_storage` can only deliver the saved state by sending it back from the browser after the page rendered, and Streamlit reruns the script to deliver a component value (proven: 2 `[RERUN]`s per load with it, 1 with it stubbed out) | `utils/local_storage_manager.py`: same API, new transport — saved state read from a cookie (`st.context.cookies`, available on the first run) and written by a plain inline script (cookie + localStorage mirror, nothing sent back). A one-time script copies an existing user's old localStorage value into the cookie. The script's element container is hidden, so a save made inside a widget callback does not push the header down | Low: parity verified — old and new code restore exactly the same preferences (Units/conversion are reset on a new session by design, on both) |
| 17 | Every browser on STG posted Streamlit usage telemetry | `.streamlit/` is gitignored, so STG never gets `gatherUsageStats=false` | `startup.sh`: `export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false` (verified: 0 requests to data.streamlit.io / fivetran across all pages) | None |
| 18 | Local dev: the root URL and `/login` always stopped on the login page | The dev bypass (`APP_ENV=LOCAL` + `DEBUG`) lives in `require_auth()`, which the login page never calls; its redirect only trusts a real `auth_session` cookie | `pages/login.py`: under the same bypass gate, establish the bypass session and go to Home (skipped after Logout) | None on STG/PROD — `APP_ENV` is never `LOCAL` there |
| 19 | Earnings Calls transcript search for one company with Year or Quarter = All: 19 s ('tariff'), 110 s ('inventory') on every fresh process | `search_transcripts_fulltext` used `MATCH … AGAINST` with `AND ticker = :t`. MySQL scores every transcript in the table that contains the word (2,056 for 'tariff', 14,479 for 'inventory') before the ticker filter, so the 500-row candidate cap never stops it early. All-company searches do stop early and were fast | `data/repository.py`: with one company, search by the ticker index + `LIKE` (that company's ~60 transcripts). The page keeps only transcripts whose text contains the keyword (case-insensitive substring), which is exactly `LIKE`, so nothing is lost; natural-language FULLTEXT had missed some ('tariff' at M Q2: 3 matches now, 2 before). 110 s → 2.8 s, 19 s → 1.2 s, the rest being the transcript text transfer | Low. All-company search unchanged |
| 20 | Three new persisted caches did not all follow "rebuild when the data changes" | `yf_topic_keys` used a daily token (`CURDATE()`), so it rebuilt every day whether or not news arrived; `fiscal_year_end_map` (AV side) and `segment_rows_<T>` used row counts, which miss an edited value or a re-ingest with the same number of rows | `data/repository.py`: topics → `MAX(id)` (a new article), with the count scoped to one row (`COUNT(*)` of 572k rows cost up to 2.9 s; `ingested_at` has no index and timed out); fiscal year end → `SUM(CRC32(ticker, fiscal_year_end))` on the covering index; segments → `MAX(id)` per ticker's 10-K rows (moves on a new filing or a re-ingest). Each check 0.26-1.0 s, at most once a minute, off the request path when stale | Low. An in-place UPDATE of a v5 value without a re-ingest is still not seen (a per-ticker checksum costs 8.3 s cold) |
| 21 | Screening grid: a column filter vanished as soon as its popup closed; the Key Devs export then ignored it | Closing the popup reruns the page. The rerun sent the selection back inside `columnDefs[].filterParams` and a JS literal, so st_aggrid saw changed gridOptions and called `api.updateGridOptions`, which re-creates the JsCode filter class; AG Grid drops a column's filter when its filter class changes (proven in the browser: context/params changes keep the filter, a new class clears it). Same on HEAD | `pages/screening.py`: gridOptions are now identical between reruns of the same results. The filter saves its model in the tab's `sessionStorage` when the popup closes; `onFirstDataRendered` re-applies it only for a grid that is really rebuilt and only if `context.dataSig` (hash of the rows) matches, so a stale filter never lands on new results | Low. Verified: kept after close (8 rows), restored after leaving and returning, cleared by Select All, not applied to new criteria; Key Devs export = filtered rows |

## 3. Results (localhost, ms)

Cold = fresh process, first hit (n=1). Warm = p50 (n=3).

| Page · action | Cold before | Cold after | Warm before | Warm after |
|---|--:|--:|--:|--:|
| Filings PDF download (click → file) | never finished | **1,288** | — | — |
| Earnings Calls load | 28,482 | **3,133** | 2,507 | **1,074** |
| Market Data · Segment (M) | 20,533 | **2,333** | 1,096 | 1,002 |
| Company Filings load | 17,631 | **3,205** | 1,512 | **897** |
| Newsroom load | 11,529 | 7,793 | 5,107 | **816** |
| Newsroom `?ticker=M` | 3,135 | **745** | 4,845 | **840** |
| Newsroom keyword search | 7,314 | **880** | 721 | **532** |
| Screening Show Results (industry / geo / financial) | 1,914 / 1,746 / 2,728 | **805 / 623 / 627** | 2,251 / 2,196 / 2,101 | **894 / 869 / 878** |
| Screening open form / add / remove | 707-1,978 | **234-583** | 818-1,946 | **421-788** |
| Screening geo cascade (each level) | 656-674 | **370-423** | 592-767 | **433-450** |
| Forecasting load / quarterly / ticker | 9,542 / 7,063 / 6,023 | 4,582 / 2,803 / 2,361 | ~2,030 / 1,467 / 1,411 | **1,336 / 840 / 818** |
| Logs load | 1,757 | 1,356 | 1,084 | 1,202 |
| Transcript PDF download | — | — | — | **48-85** |
| Clear Logs | — | — | — | **306** |

Server time for most warm interactions is now 10-300 ms; the rest of each number is
the browser (render + Streamlit's status fade) and, from India, the network.

## 4. Not fixed here (flagged)

- Units / conversion reset to defaults on a new session by design: `market_data.py`
  (`prev_ticker is None` branch) deletes them from saved state on first load. Old and new
  code behave identically.
- **Local `.streamlit/config.toml` makes pages run twice** when the app is started from
  the repo root (`startup.sh` locally). STG never reads it (gitignored), and the
  STG-faithful image runs each page once. If the file is ever un-ignored, drop
  `runOnSave`/`fileWatcherType`/`folderWatchList` from it first.
- **Materialized caches don't know the code version.** The freshness signature is data-only,
  so a deploy that changes an existing cache's build keeps serving the old generation until
  the data changes (seen locally: another agent's server wrote the shared cache dir). None of
  this change's caches are affected (all new names).
- Geo cascade City level offers nothing: no geographic segment member resolves to a city
  (98 members, verified). Data gap, same on HEAD.
- 3 pre-existing failures in `tests/test_screening_keydevs_columns.py` (fail on HEAD too).
- `/company_filings?ticker=M&doc_type=10-K` without `&year=` opens the newest filing
  (10-Q-Q2 / 2027), not the 10-K; with a year the link is honoured. Same on HEAD.
- Market Data Estimates and Forecasting tabs have no Excel button. Same on HEAD.
- Screening Companies / Financial Excel export every result; only the Key Devs export
  follows the grid's column filter. By design in the code, same on HEAD.
- Company dropdowns (Streamlit's own fuzzy match): typing a ticker such as `TGT`, `COST`
  or `HD` does not bring that company into the first ten suggestions; the company name
  does. `st.selectbox` 1.55 has no match-mode option. Same on HEAD.
- Retailers (admin) takes ~25 s per load and per search: the page opens its own new
  PyMySQL connection and reloads `coreiq_companies` on every rerun, no caching.
- Add Files (admin) fails locally: `azure-storage-file-datalake` is in requirements.txt
  but not installed in the local venv. Access Management cannot be exercised locally
  (the page never calls `require_auth`, so the dev bypass does not apply). Both unchanged.
- Remaining cold > 1.5 s: first request after process start (DB pool TLS setup — the
  boot warm on STG pays it), Market Data Profile / Key Stats (3.2-3.7 s), Calendar
  (4.0 s), Newsroom first load (7.8 s), Key Devs show results (2.8 s), a never-viewed
  ticker's Segment tab (built once, then persisted).

## 5. Pre-push verification (2026-09-30)

- **Regression — content parity.** Every screen captured on the committed code and on the
  new code with the same inputs and the same fresh caches: 41/41 identical (Market Data
  every tab × M/AMZN + quarterly, Screening results/geo cascade options/Key Devs,
  Calendar month/next/list, Earnings Calls M/WMT, Newsroom first 200 cards + search,
  Filings, Logs). Differences seen with mismatched caches were cache age, not code.
- **Redeploys, STG-faithful.** Image = git-tracked files + the full working-tree `app/`
  (incl. the gitignored admin pages and CA cert, as synced by hand), no `.streamlit/`,
  started with `startup.sh`. Two redeploys: empty persistent cache, then cache kept.
  Boot: health 0.5 s, `[BOOT][WARM_TRIGGER] OK` 1.6 s after start, written to the app's
  own log; boot warm idle ~60 s. Every page, tab, filter and download exercised cold and
  warm; filings PDF 1.7-2.0 s valid 71 pages; 0 forecast backfill writes.
- **Side-by-side A/B** (committed vs new, alternating per action, same machine load):
  new faster on 14 actions, equal on 3 (Earnings Calls, Logs, and Calendar once both
  servers held the same data — the committed-code server had cached 0 M&A events).
  Period toggle and Excel were verified in the redeploy suites (the A/B session state
  broke them on both servers alike).
- **Single run per page load** on STG's signed-in path: committed code 2 runs, new code 1.
- Telemetry: 0 requests to data.streamlit.io / fivetran on any page. No layout shift when
  a preference is saved (header stays at 27 px).
- Tests: 418 passed; 3 pre-existing failures in `test_screening_keydevs_columns.py`.
- **Final redeploys on the current tree (after the login fix and a parallel session's
  geo-filter change, 23:09).** Same image recipe, empty cache then cache kept; 109 actions
  each: every page, every Market Data tab and its Excel, every filter and change, Screening
  all criteria + both Excels, Calendar views/filters, Earnings Calls year/quarter change,
  transcript PDF, in-transcript search + CSV, cross-transcript search + CSV, Forecasting
  every tab + Excel, Filings doc type/year/company/search + PDF, Newsroom sort/category/
  sector/search/show more, Logs + download. All pass except the City level (no data) and
  one Quarterly Excel download on the first redeploy that never arrived (8/8 on re-test).
  Every file checked valid (xlsx = zip, pdf = %PDF…%%EOF). Boot health 1.1 s,
  WARM_TRIGGER 1.8-2.7 s, 0 backfill writes.
- Caveat: this laptop was heavily loaded by other apps during the runs (load 4-10);
  absolute times are noisy, the A/B ratios are what to trust.

## 6. Warm-up plan (Phase 4)

Only `/home` survives a deploy; `wwwroot` is replaced ~6×/day and `/tmp` is wiped.

| Where | What | Why |
|---|---|---|
| **Persisted to `/home/mdp-cache` (`materialize.py`)** — survives deploys, stale-while-revalidate | `fiscal_year_end_map`, `yf_topic_keys`, `segment_rows_<ticker>` (new); existing `screening_universe`, `calendar_events_full`, `earnings_transcript_tickers`, … | Built once, then every process start reads them in milliseconds; a background thread refreshes on source change |
| **Warm on boot** (`startup.sh` → `/_stcore/script-health-check` → `main.py`) | DB pool (6 TLS connections), table checks (now incl. the two segment cache tables), screening universes, calendar/earnings caches | Boot pays the one-time process costs before the first user |
| **Should be added to boot warm** | `segment_rows_<T>` for the top ~50 most-viewed tickers; `EarningsCallRepository.get_companies_with_earnings`; newsroom default 8-week window | These are the remaining cold multi-second paths; each is persisted after its first build, so the warm only matters for never-seen keys |
| **`st.cache_data` with long TTL** (per process) | Transcripts (1 h), transcript PDFs, role memo (60 s), watchlists (120 s) | Cheap to rebuild, change during the day |

Required App Setting (unchanged, still needed): `EDGAR_CACHE_DIR=/home/edgar_cache`.
`MDP_CACHE_DIR` can stay unset — `materialize.py` already prefers `/home/mdp-cache`.

## 7. Tests

`tests/test_filing_pdf_page_breaks.py`, `tests/test_forecast_backfill_once.py`,
`tests/test_materialize_signature_memo.py`, `tests/test_user_role_memo.py`,
`tests/test_transcript_search_single_company.py` — each fails on the old code.
Full suite: 421 passed, 3 pre-existing failures.

## 8. Rollout

Sync the files listed in the task hand-off to STG (including `startup.sh`); no App
Setting or DB change is required. After deploy, check the log for `[MAT][segment_rows_*] WROTE`,
`[MAT][fiscal_year_end_map] HIT`, and the absence of `backfill_company_info | … updated=`.
