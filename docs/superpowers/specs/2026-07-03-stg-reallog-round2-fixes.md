# STG Real-Log Round-2 Fixes — Design & Evidence

**Date:** 2026-07-03
**Input:** real STG logs `server-logs-20260703_062842.log` (23 min, 3,770 lines) +
`user-analytics-20260703_062843.jsonl` (342 events) + 6 user-reported issues with
screenshots. Every root cause below was reproduced against STG with read-only probes.

---

## 1. Correctness bugs (all fixed + UI-verified)

### 1.1 Newsroom keyword returns EMPTY for common terms (`revenue`)
- **Symptom:** `revenue` over 2012→2026 = "No matches"; worked only for narrow ranges.
- **Log:** `NEWSROOM_KEYWORD_SEARCH mode=LIKE_WIDE probe_hits=30 ... av=0 yf=0` in 6s
  (then cached empty → 11ms re-serve).
- **Root cause (probe):** `search_av_articles_by_title` used a deferred join +
  `USE INDEX (idx_time_title_icp)`. EXPLAIN on the wide range → **"Backward index
  scan"** over 723k rows that **timed out at the 6s cap returning 0 rows**. The
  plain optimizer-chosen plan returns 2000 rows in ~1.7–3.3s.
- **Fix:** `app/data/repository.py` — both `search_av/yf_articles_by_title` now use
  a **plain single query, no deferred join, no index hint**. Rare terms that still
  can't fill via LIKE fall through to the router's FT fallback.
- **Verified:** UI — `revenue` wide now shows **1000 articles** ("1000 articles
  matching 'revenue' in the selected date range"). STG query: wide 14y = 2000 rows
  1.7s. (`revenue` YF over 14y still times out → 0; AV dominates the feed.)

### 1.2 Earnings Calls lands on 2007, not the latest year
- **Symptom:** Macy's (M) fresh load → 2007 Q4.
- **Root cause:** `available_years = sorted(..., reverse=True)` (newest first), so
  `year_options = ['ALL', <newest>, …, <oldest>]`, but the default used
  `year_options[-1]` = **oldest**. (The comment even claimed [-1] was "latest".)
- **Fix:** `earnings_calls.py:1998` → `year_options[1]` (newest). Verified: M now
  lands on **year=2026**.

### 1.3 Screening Key-Dev checkbox in column 2, not column 1
- **Root cause:** `configure_selection(use_checkbox=True)` puts the checkbox on
  `columnDefs[0]` ("Key Developments By Date"), while "Company Name(s)" is pinned
  left — so the pinned name column sits leftmost but the checkbox stays on col 2.
- **Fix:** `screening.py` — after `build()`, strip the auto-placed checkbox and set
  `checkboxSelection`/`headerCheckboxSelection` on the pinned column.

## 2. Performance (materialization wins — verified)

### 2.1 `get_non_sec_transcript_companies` 63s → 460ms warm
- **Root cause (probe):** the build query is **404ms** (12 rows). The killer was the
  **materialize freshness signature** doing `COUNT(*)` over the whole 12.5M-row
  `coreiq_filing_metrics_v5` = **65s** cold — and the count changes every ingest, so
  the disk cache never hit and the query "rebuilt" every load. This blocked
  earnings_calls (`EC_FETCH_COMPANIES` 63s), home, and logs on landing.
- **Fix:** `materialize.py` `_live_signature` gained an optional per-source
  `"where"` that scopes the COUNT to the subset the cache depends on;
  `repository.py` scopes non_sec to `doc_type IN (transcripts)` = **106 rows,
  ~0.3s**, and stable. Verified: cold 7.5s (mostly one-time SSL connect) → **warm
  disk 460ms**; the disk cache now hits.

### 2.2 `get_ma_completion_events` 12.5s → 364ms warm
- **Root cause:** query scans ~18.7k 'M&A Activity' rows + filesort + join; cached
  only 5 min in memory → recurring cold rebuilds on `earnings_calendar`.
- **Fix:** disk-materialized (263 static rows) with a scoped freshness check
  (`event_subtype='M&A Closing' AND ma_is_closed=1`). Verified: cold 1.5s → **warm
  364ms**, 263 rows identical, DATE dtypes preserved (pickle round-trip).

### 2.3 Flicker: market_data loading pushed the page down
- **Root cause:** the per-render loading hint was an in-flow block at the top of the
  content that pushed the page down, then collapsed when cleared → a visible
  jump/flicker on every tab click.
- **Fix:** `market_data.py` — converted to a **fixed, centered overlay** (zero
  layout space); the page underneath stays put. Verified renders OK.

## 3. Analytics (investigated + enriched)
- **Was:** only `page_view` events (page name + coarse `since_last_ms`), gated to
  >1.2s gaps. No tab, no ticker, no filters, no search terms, no downloads; `user`
  often empty. → **We were NOT capturing detailed per-tab/per-filter behavior.**
- **Now:** `server_logger.new_rerun_id` enriches `page_view` with the full URL
  **query-param state** (tab, ticker, period, sector, date range, keyword, …) and
  emits on **every filter/tab change** (`reason=first-load|filter-change|dwell-gap`),
  so the feed records what each user views per tab/filter with timing.

## 4. CSV export + market_data warmup (this pass)
- **CSV export** (`search_transcript_windows_for_export`, was 84s): probe showed
  pass-2 window fetch is BOTH latency- and transfer-bound. Raised parallelism
  **10→14 workers** (safe, ~14% faster). Kept the window at **8000 (no
  truncation)** — a 6000 window was ~40% faster but trimmed ~some >5900-char
  speaker turns (data loss, which you vetoed). Pass-1 metadata (FULLTEXT scan,
  ~22s for a common word) is the other half and is inherent to FTS.
  **Open decision for you:** if ~5% of very long paragraphs may be trimmed,
  window→6000 gives ~40% more. Say the word.
- **market_data cold tabs** — added a warmup track: **AMZN segments**
  (`_fetch_all_db_rows`, ~4.6s cold) + **M (Macy's) ratings** (`get_ratings_data`,
  ~10s cold — AMZN has no ratings; M is the flagship retailer that hit 10s). The
  common landing + the ratings example are now pre-warmed. Per-ticker cold latency
  for *other* tickers is inherent (Azure buffer) — a full fix would warm the whole
  ratings/segment index into the buffer pool.
- **Literal frame-by-frame video** not captured; the flicker *cause* was fixed
  (fixed overlay). A screenshot-sequence harness can be added if wanted.

## 5. Verify on STG after push
```
grep NEWSROOM_KEYWORD_SEARCH ...        # revenue wide: av>0 (not 0); mode/timing
grep "company=M year="  ...             # earnings: year=2026 not 2007
grep DB_get_non_sec_transcript_companies # expect ~0.4s (was 63s)
grep get_ma_completion_events / MA      # expect ~0.4s warm (was 12.5s)
tail user_analytics.log                 # page_view now has filters={tab,ticker,...}
```
