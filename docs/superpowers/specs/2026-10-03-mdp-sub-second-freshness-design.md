# MDP — sub-second pages with fast, lossless freshness

**Date:** 2026-10-03 · **Status:** implemented, uncommitted · **Scope:** speed only — no UI,
logic, data or display change. No database writes (the data team owns the DB).

## 1. Problem

After every STG deploy (~6×/day) the first viewer of every page and every company paid the
database: 2–14 s per company, 5–16 s for Newsroom feeds, 55–127 s for a filings search with no
hits, 21 s for a cross-company transcript CSV. Separately, most caches refreshed on 1–12 h
timers, so a one-row data change could stay invisible for hours.

Root causes, measured against the STG database (laptop: ~300 ms per round trip):

| Symptom | Root cause |
|---|---|
| Per-company first loads 2–14 s | results lived only in process memory; every deploy lost them |
| Newsroom feed / search 2–16 s | every query scanned the news tables over the network |
| 1y/5y Newsroom search empty | MySQL title scan hits `MAX_EXECUTION_TIME(10000)` → 0 rows |
| Filings no-hit search 55–127 s | prefetch returned 0 → fell back to FULLTEXT over 12.5 M rows |
| Cross-company CSV 21 s | single-ticker export used the all-company FULLTEXT path |
| Segment tab +2–5 s each render | `has_quarterly_segment_data` uncached COUNT over v5 |
| First signed-in visit +1.4–9 s | FULLTEXT/EC warm-ups ran on that user's request |
| Background services idle until sign-in | news copy, watcher, table checks were behind the sign-in gate; the boot warm runs with no user |
| Watchlists queried on every page load | cache was per session; every page load is a new session |
| Forecasting +90 ms each visit | Excel workbook rebuilt per session |
| Stale data up to 12 h | outer `st.cache_data` TTLs (e.g. news feed 43200 s) |

## 2. Architecture

```
            information_schema.TABLES.UPDATE_TIME  (one query, every 15 s)
                              │
                     utils/freshness.py  ── watcher thread (first pass at boot)
          ┌───────────────────┼─────────────────────────┐
          ▼                   ▼                         ▼
 materialized caches    utils/persist.py          data/news_mirror.py
 (rebuild if content    per-company results       SQLite copy of news +
  signature changed)    on /home, rebuilt when    transcripts; pulls new /
                        a table they read moved   changed rows, checksums blocks
          │                   │                         │
          └──── disk copy rebuilt FIRST, then the in-memory st.cache_data is cleared ────┘
```

### 2.1 Freshness watcher (`app/utils/freshness.py`)
* One query returns the last write time of every watched table:
  `SELECT TABLE_NAME, UPDATE_TIME FROM information_schema.TABLES …` after
  `SET SESSION information_schema_stats_expiry = 0` (MySQL caches those stats 24 h by default).
* Runs every `FRESHNESS_SECONDS` (10). First pass at boot, so stored results are verified before
  users arrive.
* Watched tables = sources of materialized caches + tables read by recently used persisted
  results + listener tables (news, watchlists).
* A materialized cache rebuilds only when a source moved AND its content signature changed
  (SIP: watermark + CRC32 SUM). Unchanged → its recorded times are updated, no rebuild.
* A rebuild requested while one is queued or running is run again afterwards (`_AGAIN`), because
  the running build may have read the data before the write.
* `UPDATE_TIME` is NULL after a MySQL restart → fallback signature pass every
  `FRESHNESS_FALLBACK_S` (300).

### 2.2 Persisted results (`app/utils/persist.py`)
`@persistent(name)` sits UNDER `@st.cache_data`: memory stays the fast path, disk is the miss path.
* While a result is built, every SQL statement is recorded (SQLAlchemy `before_cursor_execute`,
  and `news_mirror.run` for queries served locally); the `coreiq_*` tables read are stored with the
  result plus their write times.
* Key = (function's own source hash, `PERSIST_VERSION`, arguments). Hashing the function rather than
  the file keeps results across deploys that change other functions. `PERSIST_VERSION` bump = drop all.
* Current ⇔ no table it read was written since. Not current → served at once and rebuilt in the
  background (one worker thread). When the watcher has not reported a table yet (first seconds after
  boot) the copy is served and only *checked* in the background — no DB round trip on the page.
* `strict=True` (companies rows/map — other caches are built from them) rebuilds before answering.
* Never stored: empty/None/False results, or values rejected by `store_if` (a failed query in this
  codebase returns `[]`, which must not hide real data). A rebuild that returns nothing keeps the
  stored copy.
* After a rebuild that changed the value, the in-memory cache is cleared (`bind_ram_clear`).

Functions persisted: key stats, price history, latest quote, shares/price, quote overview, stock
price/market cap charts, company overview, reported currency, FX list, statements (IS/BS/CF rows),
ratios, estimates (+dates), model forecasts (+dates), segments (raw rows, quarterly rows, quarterly
check, international check, revenue rows), ratings rows, filings header + ticker filter, EC
years/quarters/display period/event dates/available tickers, companies rows/map/list/by-ticker,
fiscal year end, executive compensation, Market Data period types + shares/price, countries,
company source, non-SEC tickers, Key Devs events by industry, segment-cache status, forecast
dashboards. Display mappings read from repo files (geo label map) are deliberately NOT persisted.

### 2.3 Local news & transcript copy (`app/data/news_mirror.py`)
* SQLite file with exactly the columns the Newsroom / Earnings Calls read from
  `coreiq_av_market_news_sentiment`, `coreiq_yf_market_news_sentiment`,
  `coreiq_av_earnings_call_transcripts`, `coreiq_av_companies_all(symbol, sector)`, plus
  `norm_title` (NFKD, accents stripped, casefolded = `utf8mb4_0900_ai_ci` comparison).
* The repository's MySQL SQL runs unchanged; only MySQL-only syntax is translated (hints,
  `USE/FORCE INDEX`, `MATCH…AGAINST` → substring per word, `GROUP_CONCAT … SEPARATOR`,
  `title LIKE` → `norm_title LIKE`). Equal timestamps are ordered the way MySQL's
  `idx_av_time_id (time DESC, id ASC)` returns them, so a LIMIT edge cuts the same rows.
* Index `(time, norm_title)` lets keyword search test titles inside the index:
  5-year no-hit search 1.4 s → 0.18 s.
* Served only when complete; any local error falls back to MySQL for that query.
* **Location:** on Azure `/home` is SMB, where SQLite's WAL locking does not work and every page
  read is a network trip. Working copy: `/tmp/mdp-news` (local disk, `NEWS_MIRROR_DIR` overrides).
  `/home/mdp-cache/news_mirror.sqlite` is a snapshot (backup API, every 6 h) that survives deploys.
* **Change detection** (on every write the watcher reports for these tables):

| Change | Detected by | Visible after |
|---|---|---|
| New rows | ids above the copied maximum | one watcher cycle (≤ ~20 s) |
| Transcript re-fetched in place (~1,700/day) | `fetched_at_utc` ≥ copy's newest | one cycle |
| Edit to recent news (newest 2 blocks ≈ 13 days AV) | COUNT + CRC32 per 10k-id block | one cycle |
| Edit/delete of older rows | full block sweep, throttled to 25 % DB time | ≤ 6 h (`NEWS_MIRROR_RECONCILE_S`) |

* A sync requested during a running sync is queued, never dropped.
* After applying a change: rebuild disk caches built from the copy, then clear the Newsroom /
  Earnings Calls in-memory caches (`news_mirror.on_change`).
* **After a deploy:** restore the snapshot (seconds) → pull new rows, changed rows, newest 5 blocks →
  serve → throttled full sweep in the background. First copy ever: ~45 min in the background,
  MySQL serves meanwhile.

### 2.3-bis First copy: parallel, newest first, checkpointed (no DB change)
Measured cause of the 44-min first copy: MySQL reads its disk at ~4-15 MB/s cold
(`innodb_io_capacity=200`, 4 GB buffer pool < 4.8 GB copied); indexes cannot help (the copy
already reads in primary-key order). Code changes:
* Id chunks per table, newest first, several workers per table, all tables at once
  (AV 3, YF 1, transcripts 2 streams); 5,000-row reads during the first copy; each chunk
  retried 3x; completed chunks recorded in the file (resume).
* Progressive serving: at start, the lowest id published in the last 7/30/90/365 days is
  read (indexed); once every chunk from the top down to it is copied, news queries whose
  start date lies in that window (and by-id lookups of present rows) are served locally.
  Older ranges stay on MySQL until the copy completes. New rows + newest blocks are kept
  exact meanwhile.
* Checkpoint snapshot every 10 min during the first copy → a redeploy resumes.
* Snapshot gzip-compressed: 4.9 GB → 1.36 GB on /home.
* Optional seed: `NEWS_MIRROR_SEED_BLOB=<blob name>` fetches a prepared snapshot from the
  filings storage container when /home has none.

Measured (laptop over VPN, 300 ms RTT — STG same-region should be faster):

| Event | Before | Now |
|---|---|---|
| Yahoo news, last 90 days served locally | 44 min | 1.8 min |
| AV news, last 30 / 90 days served locally | 44 min | 2.6 / 2.9 min |
| Everything copied | 44 min | 21.7 min |
| Redeploy (restore + verify, all tables local) | — | 26 s (+ /home read on Azure) |

### 2.3a Transcript keyword search from the copy
* `tx_words` — an FTS5 word index (unicode61, accents removed, case-folded = the column's
  `_ai_ci` comparison) over the copied transcripts, kept in step by triggers.
* `transcript_like_ids(kw)`: a keyword of letters/digits can only occur inside one word, so
  "transcripts containing a word that contains kw" is exactly MySQL's `LIKE '%kw%'`
  (verified identical on 13 keywords). Used for the cross-company LIKE paths (keywords
  < 3 chars, and the zero-FULLTEXT-hit fallback): 6-14 s → 0.3-0.7 s. Keywords with other
  characters keep the MySQL query. The 500-candidate cap keeps MySQL's read order
  (`idx_has_transcript_year` → year, id; ticker list → ticker, id).
* CSV export windows: `SUBSTRING(text, GREATEST(1, LOCATE(needle, text) − 3000), 8000)` is cut
  locally when it can be reproduced exactly (ASCII text, or every non-ASCII character folds
  to exactly one character; soft hyphens / zero-width / ß / æ → MySQL). 0 mismatches on
  12,000 windows; 92 % answered locally. Exports 2-8× faster, identical output.

### 2.3b Tables written all day
`coreiq_filing_metrics_v5` is written every ~5 s and `coreiq_company_events` every ~30 s
by the ingest. Table-level freshness rebuilt every v5-based result on every cycle (711
background rebuilds in 70 min). Now: the watcher reads which tickers got new ids since its
last look (primary-key range), rebuilds only results whose arguments name one of them,
and records the others as still exact (only if they were exact at the previous write).
Edits/deletes leave no new id → all results of those tables are rebuilt every 30 min
(`PERSIST_SCOPED_FULL_S`). `non_sec_transcript_companies` got a content checksum signal
instead of count-only (it was rebuilt on every v5 write).

### 2.3c Round 2 (approved: Company Filings speed-only)
* Missing filing blob: once BOTH download paths got "blob does not exist", that answer is
  reused for 60 s process-wide (`_missing_blobs`) instead of ~6 Azure calls on every rerun;
  "Retry Download" clears it. Found files: unchanged (already served from the disk cache).
* Filing search with no metric match: the LLM-cache status ("not_found", or nothing cached and
  no SECTION_CACHE.json) is read on the read engine first; the BEGIN/COMMIT transaction
  (~0.8 s) only opens when extraction can actually run. Same return values.
  `_prefetch_filing_metrics` persisted (2 s on the first search per filing after a restart).
* Materialized caches: the first read in a process serves the disk copy immediately and checks
  its signature in a background thread (it previously blocked the first visitor ~3 s, then
  served the same copy). Calendar first visit after restart 4.0 s → 1.3 s.

### 2.3d Round 3: Market Data Segment and Additional Data tabs (STG log 03-Oct)
Evidence (STG, first view of ADT after the round-2 deploy): Segment 7.8 s (quarterly check
2.9 s, live segment-row build 2.75 s), Additional Data 14.2 s first render, then 4.5–6.6 s per
1.5 s poll. The Excel button is drawn after the table, so it was missing for the whole wait.
* Quarterly check (`has_quarterly_segment_data`): `SELECT 1 … LIMIT 1` instead of `COUNT(*)`
  (same answer; COUNT read every matching row first). ADT 0.74 → 0.28 s, AMZN 3.9 → 0.27 s.
* Segment rows: background warm-up track 2b writes `segment_rows_<ticker>` to the persistent
  cache dir for every company that has none (`build_segment_rows_on_disk`, disk only, nothing
  kept in RAM, sequential). Later boots skip tickers already on disk; the first read still
  checks the signature (MAX(id) of the ticker's 10-K rows) in the background. The first
  Segment view then reads disk instead of 3 live queries (M: 12 s build → 0.06 s read).
* Additional Data: one shared 0.4 s EDGAR wait per render (`_EDGAR_WAIT_S`), was 3 s + 3 s in
  the date range and 2 s + 4 s in the data fetch, repeated on every poll. Disk-cache hits take
  milliseconds and still land in the first render; cold lookups keep running in the background
  and the existing poll fills them in. The page builds the year list once (range = first/last
  available date, same `_fiscal_dates` rule) instead of twice. Laptop: render 6.5 s → 0.41 s.
* No data or logic change: 8 Segment/Additional Data workbooks (M, WMT, KSS, ADT, AMZN) are
  cell-identical between the old and new code. Tickers with no Additional Data (ADT, AMZN) show
  "No extracted data" and no Excel, as before.

### 2.4 Other fixes
* Filings search returns the prefetch answer even when empty (no FULLTEXT fallback); phrase match
  across punctuation; 0 lost results vs DB LIKE.
* Single-ticker transcript export uses `ticker = … AND LIKE`, never the all-company FULLTEXT.
* Watchlists: process-wide cache per user, cleared on any watchlist write and on external writes.
* Background services start once per process at boot, not at first sign-in.
* FULLTEXT / EC warm-ups run in a background thread.
* Forecasting workbook cached by content digest across sessions.
* Key Devs export pages prefetched (≤ 20,000 events); Retailers admin page pooled connections,
  and its rows cached across sessions (cleared by its own saves, Refresh, and outside writes).
* Newsroom: results of aggregates (`MIN/MAX(time) AS ts`) come back as datetimes from the copy,
  as from MySQL (a string here crashed the page — found by the full run, fixed, tested).

## 3. Data-loss and correctness evidence (all read-only)
* News copy vs MySQL: 0 differing blocks (AV 2.1 M, YF 575 k, transcripts 37.7 k rows).
* Feeds, sectors, ticker, offsets: identical ids AND order to MySQL (AV); YF identical set and time
  sequence — order among same-second stories differs (MySQL's order there comes from a temporary
  table and is not stable across writes).
* 60 keyword searches × ranges: 0 lost articles. Where MySQL timed out and returned 0, the copy's
  answer equals a month-by-month MySQL scan (café 182/182, blockchain 1381/1381, same order).
* Unit tests: 394 passed (layer tests: restore-then-verify, tie order, atomic block replace,
  in-place transcript pull, queued sync, rebuild-again, no-DB-on-boot read, store_if, watchlists).

## 4. Settings (no new required App Settings)
| Variable | Default | Meaning |
|---|---|---|
| `MDP_CACHE_DIR` | `/home/mdp-cache` | persisted results, materialized caches, news snapshot |
| `NEWS_MIRROR_DIR` | `/tmp/mdp-news` when the cache dir is on `/home` | working news copy |
| `FRESHNESS_SECONDS` | 10 | watcher interval |
| `NEWS_MIRROR_HOT_BLOCKS` | 2 | newest blocks checked each cycle |
| `NEWS_MIRROR_RECONCILE_S` | 21600 | full sweep interval |
| `NEWS_MIRROR_SWEEP_DUTY` | 0.25 | max share of time the sweep queries MySQL |
| `NEWS_MIRROR=0` / `PERSIST=0` / `FRESHNESS=0` | on | kill switches (back to today's behaviour) |
| `PERSIST_VERSION` | 1 | change to drop every persisted result |

Disk: `/tmp` needs ~10 GB (4.5 GB copy + a 4.5 GB snapshot temp every 6 h); container root has ~48 GB.

## 5. Testing
Harness `.claude/dev/perf/` (gitignored): every page, tab, filter, search, download and pop-up,
cold/warm/restart, with SQL tracing (`dbtrace/`), per-run script timing (`runtime/`), browser traces,
and a no-write data-change simulator (`fakechange/`: overrides the watcher's write times and tampers
local copies only — STG is never written). Page loads are timed with `PERF_LOGIN_COOKIE=1`, the
signed-in cookie path STG users take (without it every local load runs the script twice).

## 6. Rollout / rollback
Deploy as usual. First boot builds the news copy in the background (~45 min); everything works from
MySQL meanwhile. Rollback without a redeploy: `NEWS_MIRROR=0`, `PERSIST=0`, `FRESHNESS=0`.
