# MDP Performance Investigation Report

**Date:** 2026-07-02  
**Scope:** CapIQReplacement (Coresight Market Data Portal)  
**Methodology:** Playwright E2E (`server-logs/e2e_perf_audit.json`, 25 interactions × cold/warm) + server-log correlation + SQL/code audit  
**Targets:** cold &lt;1000ms, warm &lt;200ms  
**This document:** diagnosis + observability only — **no performance fixes implemented**

**Related specs:**
- `docs/superpowers/specs/2026-07-02-mdp-e2e-perf-audit.md`
- `docs/superpowers/specs/2026-07-02-mdp-sip-observability-design.md`
- `docs/superpowers/specs/2026-07-02-mdp-db-optimization-design.md`
- **STG ops card:** `docs/MDP-STG-PERF-DIAGNOSTICS.md`

---

## A. Executive Summary

### Gap vs targets

| Metric | Target | Actual (2026-07-02 audit) | Gap |
|--------|--------|---------------------------|-----|
| Cold interactions &lt;1s | 100% | **0/24 (0%)** | 24 failures |
| Warm interactions &lt;200ms | 100% | **0/24 (0%)** | 24 failures |
| Median cold | &lt;1000ms | **12,738ms** | **12.7×** |
| Median warm | &lt;200ms | **1,004ms** | **5×** |
| P95 cold | — | **22,031ms** | — |
| Best warm | — | **314ms** (company_filings doc filter, cache hit) | — |
| Worst warm | — | **10,175ms** (screening Show Results rerun storm) | — |

**Verdict:** MDP is **10–23× above** the 1s cold SLA and **5–50× above** the ms warm SLA. Every audited page fails cold target by at least **10×**.

### Top 15 bottlenecks (ranked by cold Playwright elapsed)

| Rank | Page / Interaction | Cold ms | Primary root cause |
|------|-------------------|---------|-------------------|
| 1 | screening / show_results_revenue_gt0 | 23,508 | Streamlit rerun storm (9 reruns); SQL only ~900ms |
| 2 | newsroom / filter_ticker | 22,031 | Week-chunk parallel AV+YF fetch; 55.8s cumulative DB |
| 3 | newsroom / cold_full_load | 19,000 | Same chunk engine + metadata queries before first card |
| 4 | earnings_calls / cold_full_load | 18,439 | Materialize MISS + cold connection + serial DB chain |
| 5 | newsroom / filter_sector | 17,829 | Week-chunk fetch (36.7s cumulative DB) |
| 6 | earnings_calls / company_change | 16,694 | earnings_transcript_tickers MISS + get_company_by_ticker |
| 7 | market_data / tab_segment | 15,997 | Segment DB + pivot/render; 2 reruns |
| 8 | company_filings / company_change | 15,915 | Full `coreiq_companies` scan for dropdown (~13s) |
| 9 | company_filings / doc_type_filter | 15,590 | Same companies load on cold |
| 10 | company_filings / cold_full_load | 15,426 | `get_companies()` → `get_companies_rows()` full scan |
| 11 | earnings_calendar / month_prev | 14,958 | 3 serial EC repo calls ~9–10s each |
| 12 | market_data / ticker_change | 12,738 | QP mutation → 3 reruns + financial fetch |
| 13 | earnings_calendar / month_next | 12,428 | Same EC triple-query pattern |
| 14 | forecasting / company_change | 12,322 | Companies dropdown + render (log window miss) |
| 15 | market_data / tab_keystats | 11,460 | Key stats pivot + 2 reruns |

**Cross-cutting multipliers:**
1. Azure STG RTT (~250–400ms) × serial query chains (5–15 queries → 1.25–6s before render)
2. Full-table `SELECT … FROM coreiq_companies` on cold `@st.cache_data` miss (~6–13s)
3. Newsroom week-chunk engine: 4–6 chunks × (AV 2000–4000 + YF 2000–2364 rows) per filter change
4. Streamlit rerun amplification (screening: 9×; market_data: 2–3× per load)
5. Narrow disk `[MAT]` coverage — most pages cold-miss cache on restart

---

## B. Per-Page Deep Dive

Audit source: `server-logs/e2e_perf_audit.json` (WARM_ON_BOOT=0, fresh incognito cold starts).

### home

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_full_load | 11,130 | 1,602 | 1 | `CompanyRepository.get_companies` → `get_companies_rows` (~6s audit) | `coreiq_companies` | ticker, name, name_coresight, exchange, source, exchange_acronym, primary_industry_coresight, country_of_incorporation (8) | ~389 | Dropdown labels only (2 fields) | Module-level `_load_companies()` on import + BG_SCAN cache audit | Materialize companies map; defer BG_SCAN; lazy dropdown |

**Code path:** `home.py:46-60` calls `CompanyRepository.get_companies()` at import time. SQL: `repository.py:383-392` — **full table scan**, no WHERE. Indexed: PK/cluster only if ticker indexed; effectively **full scan** on cold buffer pool.

---

### market_data (all tabs — AMZN default, audit 2026-07-02)

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_full_load_amzn | 10,113 | 657 | 2 | `IncomeStatementRepository._fetch_all_annual_rows` (~3–4s) | `coreiq_av_financials_income_statement` or YF long-format | 17–18 line items pivoted | ~20–40 periods | All columns rendered in table | 2 reruns from QP/tab mutation + cold financial pivot | Suppress QP rerun; warm boot |
| tab_income (Income Statement) | 10,110 | 812 | 2 | Same income pivot | `coreiq_av_financials_income_statement` | DISTINCT fiscal_date_ending + 17 cols | ~20–40 | Yes — all in grid | Cold cache miss + rerun | `@st.cache_data` warm at boot |
| tab_balance (Balance Sheet) | 10,512 | 826 | 2 | `BalanceSheetRepository._fetch_all_annual_rows` | `coreiq_av_financials_balance_sheet` | ~15 cols pivoted | ~20–40 | Yes | Same pattern as income | Parallel tab prefetch |
| tab_cashflow (Cash Flow) | 10,498 | 662 | 2 | Cash flow pivot | `coreiq_av_financials_cash_flow` | ~12 cols | ~20–40 | Yes | Same | Same |
| tab_keystats (Key Statistics) | 11,460 | 967 | 2 | `KeyStatsRepository` multi-query | `coreiq_av_company_overview`, time series | varies | ~1 overview + price rows | Mostly yes | Extra overview + price_history query (~2.9s local) | Single joined query |
| tab_segment (Segments) | 15,997 | 1,467 | 2 | `SegmentDataRepository.get_segment_data` | segment tables / XBRL-derived | segment name × metric × year | ~50–200 cells | Yes — rendered tables | Slowest tab; DB + HTML build | Segment materialization |
| tab_estimates (Estimates) | 10,461 | 817 | 2 | `AnalystEstimatesRepository` | AV/YF estimates tables | estimate fields × dates | ~20–60 | Yes | Isolated date state + 2 reruns | Cache date-range per ticker |
| ticker_change | 12,738 | 3,356 | 3 (cold) / 1 (warm) | Full re-fetch all tab data | multiple financial tables | full pivots | same as above | Yes | 3 reruns on ticker QP change | Debounce QP writes |

**Tab SQL reference (Income — SEC path):** `repository.py:631-642`
```sql
SELECT DISTINCT fiscal_date_ending, total_revenue, cost_of_revenue, gross_profit,
       selling_general_and_administrative, research_and_development,
       depreciation_and_amortization, operating_income, interest_expense,
       interest_income, net_income, reported_currency, operating_expenses,
       other_non_operating_income, net_interest_income, ebitda, ebit
FROM coreiq_av_financials_income_statement
WHERE ticker = :ticker AND report_type = :period
ORDER BY fiscal_date_ending ASC
```
**Index use:** ticker + report_type (typical composite index). **Data waste:** minimal — all columns map to line items.

**New logging (2026-07-02):** `[TIMING] TAB_{Income_Statement|Balance_Sheet|...}` + existing `PAGE_*` breakdown.

---

### newsroom

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_full_load | 19,000 | 1,001 | 1 | Metadata + 7-day chunk fetch | AV+YF news, companies | see below | 1000+2000/chunk | ~50 cards shown | Chunk engine + date_range 3.1s | Reduce initial window |
| filter_sector | 17,829 | 1,004 | 1 | `get_articles` 5.4s + 3.2s; `get_yf_articles` 4.5s + 3.2s | `coreiq_av_market_news_sentiment`, `coreiq_yf_market_news_sentiment` | AV: id,title,summary,url,source,source_domain,time_published,ticker_sentiment_json,topics_json (9) | AV raw 2000–4000 → dedupe 1000; YF 2000–2364 | UI shows ~50–100 cards; **60–80% rows wasted** | 4–6 week chunks × 2 sources in parallel | Push dedupe to SQL; fewer chunks |
| filter_ticker | 22,031 | 1,034 | 1 | `get_articles` 9.9s + 7.8s | same | same | same | same | Ticker filter still multi-chunk | Single-chunk for ticker filter |

**Chunk engine:** `newsroom.py:86-179` — splits date range into 7-day chunks; each chunk: AV limit 1000 (fetch 2000 raw), YF limit 2000.

**AV no-keyword SQL:** deferred join via `idx_av_time_id` — inner: id only; outer: 9 columns. **Dedupe:** Python `_dedupe_and_merge_articles` — audit showed raw=4000 → unique=1000 (dupes=1203).

**New logging:** `[TIMING] NEWSROOM_CHUNK_FETCH | chunks=N av_per_chunk=[...] trim_ratio_av=final/raw`

---

### earnings_calls

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_full_load | 18,439 | 1,338 | 2 | `get_company_by_ticker` 7835ms; `get_earnings_event_dates` 4792ms; `get_non_sec_transcript_companies` 2995ms | `coreiq_companies`, `coreiq_av_earnings_call_transcripts`, `coreiq_filing_metrics_v5` | ticker lookup + event dates + DISTINCT tickers | 1 + 1 + 10–296 | Yes | MAT MISS on transcript tickers; cold pool | Disk mat (partially done) |
| company_change | 16,694 | 3,102 | 2 | Same chain | same | same | same | Yes | MAT WROTE on 2nd path | Warm dropdown first |

---

### earnings_calendar

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_full_load | 11,421 | 828 | 1 | `get_ma_completion_events` 9587ms; `get_available_tickers` 9181ms; `get_date_range` 8913ms | EC calendar tables | event fields, tickers, min/max dates | 262 + 329 + 2 | Yes — calendar render | 3 heavy serial calls (~27s DB total) | Already parallelized in code; STG still slow |
| month_next | 12,428 | 792 | 1 | Same triple | same | same | same | Yes | Re-fetch all on nav | `@st.cache_data` window |
| month_prev | 14,958 | 788 | 1 | Same (slowest date_range 9764ms) | same | same | same | Yes | Cold buffer on month boundary | Index on date columns |
| company_filter | 11,435 | 3,100 | 1 | Same | same | same | same | Yes | Filter triggers full refetch | Client-side filter on cached events |

---

### screening

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_form_load | 10,153 | 10,147 | 1 | Segment cache build (~11.7s PAGE_LOAD) | `coreiq_companies` (universe) | universe cols | ~349 | Form + segment options | Segment cache cold on warm too | Mat universe HIT helps SQL only |
| show_results_revenue_gt0 | 23,508 | 10,175 | **9** / **8** | `SEC_SCREENING_QUERY` 402ms; `YF_SCREENING_QUERY` 512ms | `coreiq_av_financials_income_statement`, `coreiq_yf_financials_income_statement` | ticker + revenue cols | 293 + 37 | Yes — results grid | **Rerun storm** (`time.sleep(0.6)` + 3× `st.rerun`) | Remove paint deferral reruns |

**Show Results rerun chain:** `screening.py:5859-5921` — click → loading overlay → sleep 0.6s → rerun → recompute → rerun.

**New logging:** `[SHOW_RESULTS]`, `[TIMING] SHOW_RESULTS_RERUN_TOTAL | page_rerun#=N`

---

### company_filings

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_full_load | 15,426 | 344 | 1 | `get_companies()` / `get_companies_rows` **~13.2s** (93% of page) | `coreiq_companies` | 8 cols full scan | ~389 | Dropdown only | Uncached companies on cold | Shared mat with screening |
| company_change | 15,915 | 9,265 | 1 | Same companies reload | same | same | same | Dropdown | Warm miss on company list | `@st.cache_data` hit |
| doc_type_filter | 15,590 | 314 | 1 | Companies cached; prefetch v5 | `coreiq_filing_metrics_v5` | doc_type, storage_year, fiscal_year | ~10–50/ticker | Yes | Warm: companies cached | — |

**Prefetch SQL:** `company_filings.py:1843-1851` — `SELECT DISTINCT doc_type, storage_year, fiscal_year FROM coreiq_filing_metrics_v5 WHERE ticker = :ticker` — **indexed** (`idx_ticker_fiscal_doctype`).

**New logging:** `[TIMING] FILINGS_LOAD`, `FILINGS_PREFETCH`, `DB_load_companies_from_db`

---

### forecasting

| Interaction/Tab | Cold ms | Warm ms | Reruns | Dominant DB calls | Tables | Columns fetched | Row count | Data used? | Root cause | Possible fix (note only) |
|-----------------|---------|---------|--------|-------------------|--------|-----------------|-----------|------------|------------|--------------------------|
| cold_full_load | 11,096 | 1,969 | 1 | `RevenueForecastService.get_companies` ~680ms | forecast + companies | company list | ~337 | Dropdown + dashboard | Companies + chart render | Boot warm |
| company_change | 12,322 | 3,130 | 2 | Same + chart refetch | same | same | same | Yes | 2 reruns on company change | Cache forecast payload |

---

## C. Rerun Analysis

### Why Streamlit reruns happen

Every widget interaction re-executes the full page script. MDP also calls `st.rerun()` explicitly for:
- Query-param synchronization (market_data ticker/tab/period)
- Loading overlays (screening Show Results)
- Auth cookie retry (`auth_manager.py:953`)
- Filter callbacks (newsroom, earnings_calendar nav)

### `[RERUN]` tag (automatic)

Emitted by `new_rerun_id()` and `bootstrap_page_observability()` in `server_logger.py` / `base_page.py`:

```
[RERUN] page=screening | page_rerun#=3 | session_rerun#=12 | since_last=57ms
```

| Field | Meaning |
|-------|---------|
| `page_rerun#` | Reruns since this page first loaded in session |
| `session_rerun#` | Global reruns across all pages |
| `since_last` | ms since previous rerun on this page (`first-load` on initial) |

### `[RERUN_TRIGGER]` tag (monkey-patch)

`base_page.py:_patch_st_rerun_once()` wraps `st.rerun`:

```
[RERUN_TRIGGER] screening.py:5865:_render_results
```

**STG grep:**
```bash
grep -a "\[RERUN\]" /home/LogFiles/mdp/server-log.log | tail -30
grep -a "\[RERUN_TRIGGER\]" /home/LogFiles/mdp/server-log.log | tail -30
grep -a "\[RERUN\] page=screening" /home/LogFiles/mdp/server-log.log
grep -a "\[RERUN_TRIGGER\].*screening.py" /home/LogFiles/mdp/server-log.log
```

### Per-page rerun hotspots (audit)

| Page | Typical reruns (cold) | Trigger sources |
|------|----------------------|-----------------|
| screening Show Results | **9** | `screening.py:5865,5871,5890,5916` + widget callbacks |
| market_data | 2–3 | QP tab/ticker sync, `render_tabs` |
| earnings_calls | 2 | Company select + transcript load |
| home / newsroom / EC | 1 | Single pass (unless auth retry) |
| auth (all pages) | +0–4 | `auth_manager` cookie retry schedule |

### Auth retries

`require_auth()` may schedule reruns with delays `[1.0, 2.0, 3.0, 4.0]`s when cookie not yet mounted — logged as `[TIMING] AUTH_RETRY_SCHEDULED`.

---

## D. Data Fetch Audit

### D.1 `CompanyRepository.get_companies_rows()` — CRITICAL

| Attribute | Value |
|-----------|-------|
| Function | `repository.py:368-413` |
| SQL | `SELECT ticker,name,name_coresight,exchange,source,exchange_acronym,primary_industry_coresight,country_of_incorporation FROM coreiq_companies` |
| Tables | `coreiq_companies` |
| Rows | ~389 (audit: 389 in earnings_calls map build) |
| Index | Full table scan on cold Azure buffer pool |
| Used in UI | 2 fields per dropdown entry (ticker + name_coresight) |
| Waste | **~75%** of columns unused in dropdown paths |
| Callers | home, nav, company_filings, screening universe, earnings_calls map |

### D.2 Newsroom `NewsRepository.get_articles()` — CRITICAL

| Attribute | Value |
|-----------|-------|
| Function | `repository.py:899-1225` |
| Tables | `coreiq_av_market_news_sentiment` (+ optional `coreiq_av_companies_all` sector join) |
| Columns (outer) | id, title, summary, url, source, source_domain, time_published, ticker_sentiment_json, topics_json |
| Fetch strategy | limit×2 overflow → Python dedupe → trim to limit |
| Rows (audit filter_sector) | 2000–4000 raw → 1000 unique per chunk |
| Index | `idx_av_time_id`, `ft_av_title` (keyword path) |
| UI usage | ~50–100 cards; summary + sentiment JSON partially used |
| Waste | **50–80%** of fetched rows discarded by dedupe + pagination |

### D.3 Newsroom `get_yf_articles()`

| Attribute | Value |
|-----------|-------|
| Tables | `coreiq_yf_market_news_sentiment` |
| Rows (audit) | 2000–2364 per chunk |
| Index | time-based index (combined SELECT) |
| Waste | Similar — fetch 2000, display subset |

### D.4 Earnings calendar triple fetch

| Function | Table(s) | Rows | Used? |
|----------|----------|------|-------|
| `get_ma_completion_events` | M&A calendar | 262 | Yes — calendar events |
| `get_available_tickers` | earnings sources | 329 | Yes — filter dropdown |
| `get_date_range` | min/max aggregates | 2 | Yes — bounds |

All three run on **every** cold navigation despite `@st.cache_data(ttl=300)` miss on WARM_ON_BOOT=0 audit.

### D.5 Screening queries (fast SQL, slow reruns)

| Query | Table | Rows (audit) | Index |
|-------|-------|--------------|-------|
| `SEC_SCREENING_QUERY` | `coreiq_av_financials_income_statement` | 293 (310 tickers) | ticker IN (...) |
| `YF_SCREENING_QUERY` | `coreiq_yf_financials_income_statement` | 37 (39 tickers) | yf_symbol IN (...) |

SQL total ~900ms; wall clock 23.5s = **rerun amplification**.

### D.6 Market data financial pivots

| Repository | Table | Typical rows | All cols used? |
|------------|-------|--------------|----------------|
| `IncomeStatementRepository._fetch_all_annual_rows` | AV or YF income | 20–40 periods | Yes — grid |
| `BalanceSheetRepository` | balance sheet | 20–40 | Yes |
| `KeyStatsRepository` | overview + time series | 1 + ~500 daily | Partial — many stats unused on tab |
| `SegmentDataRepository.get_segment_data` | segment XBRL / DB | variable | Yes |

### D.7 Company filings v5 prefetch

| Function | SQL | Rows/ticker | Index |
|----------|-----|-------------|-------|
| `_prefetch_ticker_filter_data` | DISTINCT doc_type, storage_year, fiscal_year WHERE ticker=? | 10–50 | `idx_ticker_fiscal_doctype` |

---

## E. SIP Logging — MDP Mapping

Reference: Modular-Code `server_logger.py`, `sip_components/base_page.py`, SIP doc `04-logging-and-how-to-verify.md`.

| SIP tag | MDP equivalent | Status | Notes |
|---------|----------------|--------|-------|
| `[BOOT]` | `[BOOT] server_logger up \| pid=…` | **Implemented** | `server_logger.py:425-428`; writes `restarts.log` |
| `[HEARTBEAT]` | `[HEARTBEAT] pid=… uptime=… app_rss=…` | **Implemented** | 60s daemon; `SERVER_HEARTBEAT_SECS` |
| `[RAM]` | `[RAM] page=… app_rss=…` | **Implemented** | On page_view via `bootstrap_page_observability` |
| `[RAM_CENSUS]` | `[RAM_CENSUS] live_frames=N …` | **Implemented** | Every 3rd heartbeat |
| `[RERUN]` | `[RERUN] page=… page_rerun#=…` | **Implemented** | `new_rerun_id()` + `base_page.py` |
| `[RERUN_TRIGGER]` | `[RERUN_TRIGGER] file:line:func` | **Implemented** | `st.rerun` monkey-patch in `base_page.py` |
| `[CLICK->RENDER]` | `[CLICK->RENDER] page=… render=Xs` | **Partial** | All main pages except newsroom (no `log_render_complete` yet) |
| `[TIMING]` | `[TIMING] OP \| Xms \| details` | **Implemented** | DB, page, tab, screening, newsroom chunks |
| `[DB_QUERY]` | `[TIMING] DB_{op} \| Xms \| table=… rows=…` | **Implemented** | Via `log_db_timing()` + `_log_query_time` decorator |
| `[MAT]` | `[MAT][name] HIT\|MISS\|WROTE` | **Implemented** | `materialize.py`; narrow coverage |
| `[AUDIT]` | `[BG_SCAN]` / cache audit on home | **Partial** | home boot only; not standardized tag |
| `user_analytics.log` | `user_analytics.log` JSON lines | **Implemented** | `log_user_event("page_view", …)` |
| `[MEM_OPT]` | `mem_opt.optimize_frame_memory` logs | **Partial** | Screening universe only; no `[MEM_OPT]` tag yet |
| `[MEM_TRIM]` | `[MALLOC_TRIM]` | **Implemented** | Heartbeat + post-render trim |
| `[DATA_VOLUME]` | `[DATA_VOLUME] table=… rows=N cols=M` | **Implemented (2026-07-02)** | `log_data_volume()` when rows≥1000 |

### New tags added 2026-07-02 (this task)

| Tag | Location | Purpose |
|-----|----------|---------|
| `[TIMING] DB_get_companies_rows` | `repository.py:get_companies_rows` | Full companies scan timing |
| `[TIMING] DB_get_companies` | `repository.py:get_companies` | Dropdown derive count |
| `[TIMING] NEWSROOM_CHUNK_FETCH` | `newsroom.py:_fetch_chunked` | Chunk count, rows/chunk, trim ratio |
| `[SHOW_RESULTS]` | `screening.py:_render_results` | Show Results click + rerun# |
| `[TIMING] SHOW_RESULTS_RERUN_TOTAL` | `screening.py` finally block | Rerun count after load cycle |
| `[TIMING] TAB_{Name}` | `market_data.py:render_page` | Per-tab wall clock + PAGE_* breakdown |
| `[TIMING] FILINGS_LOAD` | `company_filings.py:main` | Full page content load |
| `[TIMING] FILINGS_PREFETCH` | `_prefetch_ticker_filter_data` | v5 DISTINCT per ticker |
| `[TIMING] DB_load_companies_from_db` | `_load_companies_from_db` | Filings dropdown companies |
| `[DATA_VOLUME]` | `server_logger.log_data_volume` | Auto from `log_db_timing` when rows≥1000 |

---

## F. STG Deployment & Grep Guide

**Full command reference:** `docs/MDP-STG-PERF-DIAGNOSTICS.md`

Quick copy-paste after deploy:

```bash
LOG=/home/LogFiles/mdp/server-log.log

grep -a "\[BOOT\]" "$LOG" | tail -5
grep -a "\[CLICK->RENDER\]" "$LOG" | tail -20
grep -a "\[CLICK->RENDER\].*SLOW" "$LOG" | tail -10
grep -a "\[RERUN\]" "$LOG" | tail -20
grep -a "\[RERUN_TRIGGER\]" "$LOG" | tail -20
grep -a "\[TIMING\] DB_" "$LOG" | tail -30
grep -a "DB_get_companies_rows" "$LOG" | tail -10
grep -a "NEWSROOM_CHUNK_FETCH" "$LOG" | tail -10
grep -a "\[TIMING\] TAB_" "$LOG" | tail -15
grep -a "FILINGS_LOAD\|SHOW_RESULTS" "$LOG" | tail -15
grep -a "\[DATA_VOLUME\]" "$LOG" | tail -10
grep -a "\[MAT\]" "$LOG" | tail -20
grep -a "\[HEARTBEAT\]" "$LOG" | tail -5
grep -a "\[STRUCTURED_ERROR\]" "$LOG" | tail -10
tail -10 /home/LogFiles/mdp/user_analytics.log
```

**Correlate one request:** pick `rerun_id` from any line → `grep -a "R00123" "$LOG"`

---

## G. Observability Gaps Still Missing for STG

| Gap | Priority | Recommendation |
|-----|----------|----------------|
| `newsroom` missing `log_render_complete` | HIGH | Add at end of `render_page()` |
| `[MEM_OPT]` tag on categorical encoding | MED | Log in `mem_opt.optimize_frame_memory()` |
| `[AUDIT]` standardized cache audit tag | MED | Replace ad-hoc home BG_SCAN logs |
| Per-query `[DB_QUERY]` at `db_manager` layer | MED | Optional wrapper with SQL fingerprint (SIP pattern) |
| Earnings calendar parallel sub-timing | MED | Log each of 4 parallel futures separately |
| Auth retry correlation | LOW | Include `auth_flow_id` on AUTH reruns |
| Azure blob download timing (filings) | MED | `[TIMING] BLOB_DOWNLOAD` on `_ensure_local_blob_optimized` |
| `[CLICK->RENDER]` tab= on market_data | LOW | Pass `tab=` to `log_render_complete` in main() |
| Playwright→log automatic correlation ID | LOW | Inject test run ID via env for audit reruns |

---

## Appendix: Audit Methodology

| Aspect | Implementation |
|--------|----------------|
| Cold | Kill :8501, `WARM_ON_BOOT=0`, fresh incognito |
| Warm | Same server, second navigation |
| Script | `.claude/dev/e2e_perf_audit.py` |
| Launcher | `.claude/dev/run_local.sh` (LOCAL OIDC bypass unchanged) |
| Raw JSON | `server-logs/e2e_perf_audit.json` |
| Screenshots | `server-logs/e2e_perf_screenshots/` |

**Document status:** Observability + diagnosis complete. Performance fixes tracked separately in db-optimization design spec.
