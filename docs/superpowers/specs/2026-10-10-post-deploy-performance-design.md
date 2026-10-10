# Post-deploy performance — background churn and slow freshness checks

Date: 2026-10-10
Scope: performance only. No new features, no DB writes, no restart needed after data changes.

## How it was measured

- Local app on the STG database (`.claude/dev/perf/deploy_sim.sh`: STG-faithful image,
  `startup.sh`, OIDC bypass), port 8531, driven by the perf suites in `.claude/dev/perf/`
  (every page, tab, filter, download, Excel export and search; first visit + repeats).
- Three scenarios: fresh deploy with an empty cache dir; redeploy reusing the cache dir;
  `PAGE_MATERIALIZE=0`.
- Evidence is the server log (`[TIMING]`, `[DB_SLOW]`, `[CLICK->RENDER]`, `[PERSIST]`) plus
  read-only probes against STG.
- Caveat: the laptop is ~270-300 ms per DB round trip from the STG database. STG's app server
  is in the database's region, so round-trip-bound pages (e.g. Access Management, ~4 round
  trips) are slower here than on STG. Page switches spend 1.2-2 s in the headless browser
  (Streamlit bootstrap) with 0.04-0.4 s of server time.

## Root causes found and fixed

### 1. Segment data-version check scanned every row of the company (repository.py)

`SegmentDataRepository.current_data_version` ran
`SELECT MAX(id), COUNT(*), MAX(data_insert_timestamp) FROM coreiq_filing_metrics_v5 WHERE ticker = ?`.
`data_insert_timestamp` is in no index, so the query read every row of the company
(~44k-56k rows): 6-7 s idle, 44.6 s (M) and 84.6 s (AMZN) during the boot warm-up. It ran on
the first Segments view of each company after a deploy, and on every company in the
screener's segment index refresh (`fresh=True`).

Evidence the term adds nothing: the column is `DEFAULT CURRENT_TIMESTAMP` with no
`ON UPDATE`, so it only changes on INSERT, and ids are auto-increment. The newest insert
time sat on the newest id for every ticker checked (TGT, KSS). `MAX(id)` + `COUNT(*)` is
index-only: 0.4-0.6 s.

Fix: version = `MAX(id):COUNT(*)`. The version string format changes, so each company's
stored segment tables rebuild once after this deploys (the same thing that happens after
every data load).

### 2. Freshness watcher treated a NULL write time as "changed" on every pass (freshness.py)

`information_schema.TABLES.UPDATE_TIME` is NULL for a table with no write since the MySQL
restart. On STG this includes `coreiq_companies` and `coreiq_av_companies_all`. `_loop`
put any NULL table in `moved` every 10 s, which:

- re-copied `coreiq_av_companies_all` into the news SQLite copy every ~10 s
  (1,085 slow copies across the three runs);
- made `persist.on_tables_moved` queue a rebuild of every recently used cached result
  reading `coreiq_companies` (queue depth 1,958; `is_rows`, `bs_rows`, `cf_rows` and
  `company_source` rebuilt in a loop, `changed=False` every time). This CPU and DB churn
  ran for the whole life of the process, slowing every user click.

`_check` had the same rule: every materialized cache reading a NULL-time table recomputed
its full signature every pass.

A write always sets UPDATE_TIME, so NULL → NULL is no write. The only partitioned table
(`coreiq_company_events`) reports write times, so NULL is reliable here.

`persist._current` had a third copy of the rule: a result was current only if every table
it read had a non-NULL write time. Every stored result reading `coreiq_companies`
(`company_source`, `is_rows`, `bs_rows`, `cf_rows`, `company_overview`,
`fiscal_year_end`, …) was therefore stale on every read, and each read queued a background
rebuild. A partial after-fix run with only the watcher fixed still showed ~150 rebuilds a
minute, all `changed=False`.

Fix: a table counts as moved only when its write time differs from the last pass. Caches
reading NULL-time tables are still re-verified by the existing `FRESHNESS_FALLBACK_S`
(300 s) signature pass in `_check`, as the module docstring describes. That pass must NOT
report NULL tables as moved to listeners: a first attempt did, and every 300 s it re-queued
all ~2,000 recent stored results (`PERSIST_RECENT_MAX`) without a check. They drain at
~150/min, so the queue never emptied (refills at boot+300 s and boot+600 s in the log).

A fourth path into the same burst: the watched set grows as stored results are read
(`tables_in_use()`), and a table watched for the first time had no earlier time, so
`live != seen.get(t)` reported it as moved. On a redeploy with a warm cache the warm-up reads
every company's statements, adding `coreiq_av_financials_income_statement`, and ~1,920
results reading it were rebuilt without a check (all `changed=False`; stored and live write
times were identical). Now only a table seen on the previous pass can count as moved; each
stored result still compares its own build times on read. In `persist._current`, NULL is compared like
any other value; a table whose time was unreadable at build time never matches.
Check: scratchpad `t_null.py` asserts the five cases (NULL→NULL current; written since;
MySQL restart; unknown build time; unreadable live time).

### 3. Segment row sweep ran with nothing to store (repository.py, materialize.py)

`build_segment_rows_on_disk` (boot Track 2b) queried every company's ~6.5k-12.5k rows. With
`PAGE_MATERIALIZE=0` or no writable cache dir, `write_materialized` drops the result, so
every boot paid the full sweep for nothing (388 slow queries in that scenario).

Fix: `materialize.storing()` (switch on and a writable dir); the sweep returns 0 when it is
false.

Once fixes 1-2 removed the DB contention, the sweep ran unthrottled: 5-27 companies a minute
for an hour on a fresh cache dir. Decoding ~10k rows per company held the app's one core
(warm-phase CPU 44% average vs 20%, 309 samples pinned near one core vs 24), and repeat page
loads with 0.03-0.26 s of server time took 1.9-2.5 s in the browser (0.9-1.3 s before). The
sweep now sleeps 3x each company's build time (~25% duty, the news copy's pattern). On STG
it only runs for companies missing from `/home/mdp-cache`: the first deploy, then new
companies.

### 4. Filings search with no fact match took ~3 s every time (company_filings.py)

When the fact search found nothing (`risk`, `zzqxvq`), the page ran its own copy of the AI
fallback: it opened a transaction on the main engine (BEGIN + query + COMMIT, plus a new
TLS connection when the pool was idle) to check `filing_llm_cache`, then returned nothing
because no `SECTION_CACHE.json` exists. `filing_llm_cache` is empty on STG.
`FilingMetricRepository.search_with_llm_fallback` already did this check on the read engine
and skipped when there is nothing to read, but no caller used it.

Fix: the page calls `search_with_llm_fallback`; the inline copy is removed. The "AI
extracted" note is still shown only when the AI returned results.

### 5. Ratios warm-up crashed for every company (cache_manager.py) — earlier in this programme

`_warm_ratios_screening` passed `None` dates, which raised a TypeError in the period filter
(472 errors per boot) and never filled the cache entry screening reads. It now uses
`_screening_date_window("FY", "Latest", None)`, the window a default Ratios criterion asks
for. Verified in the full app: 0 TypeErrors on boot.

### 6. Missing-blob errors now name the file (company_filings.py)

`_ensure_local_blob*` log `context=blob=<name>` so the recurring "The specified blob does
not exist" can be traced to a file.

## Found, not changed (with reason)

| Item | Why left |
|---|---|
| Calendar event click: full render (~0.4 s) before the selection is applied, then a rerun | Delicate page; would need the component value read before render. Saves ~0.4 s locally |
| Access Management: 4 sequential DB round trips, including a `SELECT 1` health check | Round-trip bound; ~1.1 s from the laptop, much less in-region on STG |
| Page switches 1.2-2 s in the browser | Header links do full page loads (new session). Changing to client-side navigation is an architecture change |
| Forecasting "Refresh Data" dialog: 0.46 s server, 2.3 s browser | Client-side (page charts re-sent on the rerun); needs a browser profile |
| M&A auto-enrichment: 0 of 40 enriched per tick, edgartools AttributeError on a filing with no primary HTML | Background; not a performance issue |
| `ALTER TABLE ... ADD COLUMN classifier_version` logged as an error on every check | Noise only |

## Data changes without a restart

All caches touched here follow the database on their own: segment tables key on
`MAX(id):COUNT(*)` per company, and the freshness watcher reacts to write-time changes
within ~10 s, with a 300 s signature pass for tables whose write time is NULL.

## Verification

1. Rebuild the image and rerun the three scenarios:
   `bash .claude/dev/perf/build_img.sh new <img>` then `deploy_full.sh <name> <cache> 8531 <img>`
   for each scenario (driver: scratchpad `drive2.sh`).
2. In each `logs/server-log.log` check:
   - `grep -c 'MAX(data_insert_timestamp) AS loaded'` = 0;
   - `symbol, sector FROM` copies: at most one per write to the table or per 300 s,
     not every 10 s;
   - `[PERSIST][` rebuild lines with `changed=False` no longer repeat every pass;
   - no `TypeError` from the ratios warm-up;
   - with `PAGE_MATERIALIZE=0`, no `SEGMENT_ROWS_DISK_WARM` sweep queries.
3. Filings search: open `/company_filings?ticker=M`, 10-K, search `zzqxvq`. Server
   `FILINGS_LOAD` should be ~0.1-0.4 s (was ~3 s).
4. Compare per-action timings with `qa_report.py` (before: `full_qa_*`, after: `full_qa2_*`).

## Results

Same actions, same harness, before (`full_qa_*`, 2026-10-09/10 night) vs after (`full_qa3_*`,
2026-10-10 afternoon, all fixes). Laptop on the STG DB, so absolute numbers include ~270 ms
per DB round trip and headless-browser time.

| Scenario | First visits < 1 s | First-visit total | Repeats < 1 s | Repeat total |
|---|---|---|---|---|
| 1 Fresh deploy, empty cache | 352 → 353 of 503 | 1,168 s → 578 s | 213 → 201 of 281 | 239 s → 238 s |
| 2 Redeploy, cache reused | 428 → 423 of 503 | 670 s → 530 s | 251 → 246 of 281 | 165 s → 168 s |
| 3 `PAGE_MATERIALIZE=0` | 401 → 413 of 503 | 587 s → 391 s | 223 → 235 of 281 | 188 s → 180 s |

Background health (whole scenario):

| | Before (1 / 2 / 3) | After (1 / 2 / 3) |
|---|---|---|
| Stored-result rebuilds (`[PERSIST][`) | 10,006 / 9,883 / 10,683 | 1 / 2 / 1 |
| DB connection drops | 8 / 0 / 0 | 0 / 0 / 0 |
| Segment-sweep slow queries | 137 / 269 / 388 | 315 / 90 / 0 |
| Leftover downloads | 0 | 0 |

Largest single gains: Segments tab AMZN first view 109 s → 2.2 s (fresh) and 85.4 s → 1.3 s
(redeploy); Segments M URL 45.1 s → 0.7 s (redeploy); Screening Key Devs results 180 s →
2.8 s; Company Filings load 10.6 s → 0.8 s (redeploy); filings search with no fact match
~3.5 s → 0.8-1.2 s; Screening server pipeline 373 ms → 131 ms average.

Not yet meeting 1 s (open):

- Newsroom "From 1 year ago + search": 5-7 s first time — a full scan of a year of articles
  in the local SQLite copy. Needs a full-text index on the copy (larger change).
- Page switches 1.2-2 s, Screening grid actions 1-2 s, Forecasting Refresh dialog 2.3 s:
  browser-side (server time 0.04-0.5 s).
- Access Management ~1.1 s: four DB round trips from the laptop; in-region on STG.
- A few first-visit regressions within noise of a cold cache (e.g. Calendar List "next"
  0.65 s → 3.3 s once) — not repeated in the repeat runs.

Errors after the fixes are all known and non-performance: missing `filing.html` for
M 2024/2025 10-Q-Q1 in Azure (data side — `filing.meta.json` points to a file that is not
there), the `classifier_version` ALTER noise, and M&A auto-enrichment (edgartools
AttributeError on filings with no primary HTML; more of them as new events arrive).

The third scenario's first attempt was stopped at 15:09:44 by another session restarting
Streamlit on this machine; it was rerun in full (`full_qa3killed_nomat` kept, not used).

## Rollout

No App Settings, schema or data changes. Deploy as normal. The first Segments view per
company rebuilds its stored tables once (new version format), in the background where the
boot warm-up reaches it first.
