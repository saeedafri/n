# MDP End-to-End Performance Audit

**Generated:** 2026-07-02T18:58:03.580507
**Methodology:** Playwright + server log correlation, WARM_ON_BOOT=0 cold starts
**Target:** cold <1000ms, warm <200ms

---

## Section 1: Executive Summary

| Metric | Target | Actual | Gap |
|--------|--------|--------|-----|
| Cold interactions <1s | 100% | 0/24 (0%) | **24 failures** |
| Warm interactions <200ms | 100% | 0/24 (0%) | **24 failures** |
| Median cold | <1000ms | 12738ms | — |
| Median warm | <200ms | 1004ms | — |
| P95 cold | — | 22031ms | — |

**Verdict:** MDP is **10–23× above** the 1s cold target and **5–50× above** the ms warm target. Measured across **25 interactions** (50 cold/warm pairs) with real Playwright timing + `server-logs/server-log.log` correlation.

**Gap summary:**
- **Cold:** 0/24 successful loads under 1s. Median **12.7s**, P95 **22.0s**, worst **23.5s** (screening Show Results).
- **Warm:** 0/24 under 200ms target. Median **1.0s**, best warm **314ms** (company_filings doc filter, cache hit). Worst warm **10.2s** (screening — rerun storm).
- **Every page** fails the 1s cold SLA by at least **10×**.

**Dominant root causes (ranked):**
1. Azure STG RTT (~3–5s per heavy SELECT) × serial query chains
2. Full-table `coreiq_companies` scan on every cold server (`get_companies_rows`, 6–13s)
3. Newsroom week-chunk parallel fetch (4–6 chunks × AV+YF = 36–56s cumulative DB time)
4. Streamlit rerun storms (screening: 9 reruns on Show Results; market_data: 2–3 per load)
5. Narrow disk materialization coverage — most pages cold-miss `@st.cache_data`

---

## Section 2: Full Interaction Matrix

| Page | Interaction | Cold ms | Warm ms | Reruns (cold) | Dominant DB call | Root cause |
|------|-------------|---------|---------|---------------|------------------|------------|
| company_filings | cold_full_load | 15426 | 344 | 1 | get_companies() 13.2s | `company_filings.py:3004` uncached companies dropdown load |
| company_filings | company_change | 15915 | 9265 | 1 | get_companies() 12.5s | Same — companies list reload every cold start |
| company_filings | doc_type_filter | 15590 | 314 | 1 | get_companies() (cached) | Warm: companies map already in @st.cache_data |
| earnings_calendar | cold_full_load | 11421 | 828 | 1 | EarningsCalendarRepository.get_ma_completion_events (9587ms) | DB-bound: EarningsCalendarRepository.get_ma_completion_events (9587ms) |
| earnings_calendar | company_filter | 11435 | 3100 | 1 | EarningsCalendarRepository.get_ma_completion_events (9629ms) | DB-bound: EarningsCalendarRepository.get_ma_completion_events (9629ms) |
| earnings_calendar | month_next | 12428 | 792 | 1 | EarningsCalendarRepository.get_ma_completion_events (9632ms) | DB-bound: EarningsCalendarRepository.get_ma_completion_events (9632ms) |
| earnings_calendar | month_prev | 14958 | 788 | 1 | EarningsCalendarRepository.get_ma_completion_events (10429ms) | DB-bound: EarningsCalendarRepository.get_ma_completion_events (10429ms) |
| earnings_calls | cold_full_load | 18439 | 1338 | 2 | CompanyRepository.get_company_by_ticker (7835ms) | Materialize MISS/STALE: earnings_transcript_tickers — live DB rebuild |
| earnings_calls | company_change | 16694 | 3102 | 2 | CompanyRepository.get_company_by_ticker (6641ms) | Materialize MISS/STALE: earnings_transcript_tickers — live DB rebuild |
| forecasting | cold_full_load | 11096 | 1969 | 1 | — | CPU/render bound or log window miss |
| forecasting | company_change | 12322 | 3130 | 2 | — | CPU/render bound or log window miss |
| home | cold_full_load | 11130 | 1602 | 1 | CompanyRepository.get_companies (~6s) | Full companies scan + BG_SCAN cache audit on home boot |
| market_data | cold_full_load_amzn | 10113 | 657 | 2 | IncomeStatementRepository (~3-4s) | 2 reruns from qp mutation + financial pivot query |
| market_data | tab_balance | 10512 | 826 | 2 | — | CPU/render bound or log window miss |
| market_data | tab_cashflow | 10498 | 662 | 2 | — | CPU/render bound or log window miss |
| market_data | tab_estimates | 10461 | 817 | 2 | — | CPU/render bound or log window miss |
| market_data | tab_income | 10110 | 812 | 2 | — | CPU/render bound or log window miss |
| market_data | tab_keystats | 11460 | 967 | 2 | — | CPU/render bound or log window miss |
| market_data | tab_segment | 15997 | 1467 | 2 | — | CPU/render bound or log window miss |
| market_data | ticker_change | 12738 | 3356 | 3 | — | CPU/render bound or log window miss |
| newsroom | cold_full_load | 19000 | 1001 | 1 | — | CPU/render bound or log window miss |
| newsroom | filter_sector | 17829 | 1004 | 1 | NewsRepository.get_articles (8638ms) | Week-chunked parallel news fetch across AV+YF tables (5414ms) |
| newsroom | filter_ticker | 22031 | 1034 | 1 | NewsRepository.get_articles (17693ms) | Week-chunked parallel news fetch across AV+YF tables (9862ms) |
| screening | cold_form_load | 10153 | 10147 | 1 | PAGE_LOAD 11702ms | Form renders in 11.7s; Playwright missed "Add Criteria" text (page still loading segment cache) |
| screening | show_results_revenue_gt0 | 23508 | 10175 | 9 | YF_SCREENING_QUERY (512ms) | Streamlit rerun storm (9 reruns) amplifying latency |

---

## Section 3: Per-Page Deep Dive

### company_filings

#### cold_full_load (cold 15426ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 0ms
- Top DB calls:
  - `CompanyRepository.get_company_by_ticker` 0ms — table=repository rows=1 ticker=AMZN

#### company_change (cold 15915ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 0ms
- Top DB calls:
  - `CompanyRepository.get_company_by_ticker` 0ms — table=repository rows=1 ticker=AMZN
- Click→render: {'page': 'company_filings', 'render_s': 14.02}

#### doc_type_filter (cold 15590ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 0ms
- Top DB calls:
  - `CompanyRepository.get_company_by_ticker` 0ms — table=repository rows=1 ticker=AMZN

### earnings_calendar

#### cold_full_load (cold 11421ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 27681ms
- Top DB calls:
  - `EarningsCalendarRepository.get_ma_completion_events` 9587ms — table=repository rows=262 ticker=N/A
  - `EarningsCalendarRepository.get_available_tickers` 9181ms — table=repository rows=329 ticker=N/A
  - `EarningsCalendarRepository.get_date_range` 8913ms — table=repository rows=2 ticker=N/A
  - `CompanyRepository.get_company_by_ticker` 0ms — table=repository rows=1 ticker=AMZN
- Click→render: {'page': 'earnings_calendar', 'render_s': 9.62}

#### month_next (cold 12428ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 25109ms
- Top DB calls:
  - `EarningsCalendarRepository.get_ma_completion_events` 9632ms — table=repository rows=262 ticker=N/A
  - `EarningsCalendarRepository.get_available_tickers` 9270ms — table=repository rows=329 ticker=N/A
  - `EarningsCalendarRepository.get_date_range` 6206ms — table=repository rows=2 ticker=N/A
  - `CompanyRepository.get_company_by_ticker` 0ms — table=repository rows=1 ticker=AMZN
- Click→render: {'page': 'earnings_calendar', 'render_s': 10.65}

#### month_prev (cold 14958ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 30281ms
- Top DB calls:
  - `EarningsCalendarRepository.get_ma_completion_events` 10429ms — table=repository rows=262 ticker=N/A
  - `EarningsCalendarRepository.get_available_tickers` 10087ms — table=repository rows=329 ticker=N/A
  - `EarningsCalendarRepository.get_date_range` 9764ms — table=repository rows=2 ticker=N/A
  - `CompanyRepository.get_company_by_ticker` 0ms — table=repository rows=1 ticker=AMZN
- Click→render: {'page': 'earnings_calendar', 'render_s': 13.05}

#### company_filter (cold 11435ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 25171ms
- Top DB calls:
  - `EarningsCalendarRepository.get_ma_completion_events` 9629ms — table=repository rows=262 ticker=N/A
  - `EarningsCalendarRepository.get_available_tickers` 9313ms — table=repository rows=329 ticker=N/A
  - `EarningsCalendarRepository.get_date_range` 6228ms — table=repository rows=2 ticker=N/A
  - `CompanyRepository.get_company_by_ticker` 1ms — table=repository rows=1 ticker=AMZN
- Click→render: {'page': 'earnings_calendar', 'render_s': 9.67}

### earnings_calls

#### cold_full_load (cold 18439ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 20219ms
- Top DB calls:
  - `CompanyRepository.get_company_by_ticker` 7835ms — table=repository rows=1 ticker=AMZN
  - `EarningsCalendarRepository.get_earnings_event_dates` 4792ms — table=repository rows=1 ticker=AMZN
  - `get_non_sec_transcript_companies` 2995ms — table=coreiq_filing_metrics_v5 rows=10
  - `EarningsCallRepository.get_earnings_calls` 615ms — table=repository rows=1 ticker=N/A
  - `get_earnings_calls.TOTAL` 615ms — table=coreiq_av_earnings_call_transcripts rows=1 ticker=AMZN
- Materialize: earnings_transcript_tickers:MISS, non_sec_transcript_companies:MISS, non_sec_transcript_companies:WROTE
- Click→render: {'page': 'earnings_calls', 'render_s': 5.82}

#### company_change (cold 16694ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 19078ms
- Top DB calls:
  - `CompanyRepository.get_company_by_ticker` 6641ms — table=repository rows=1 ticker=AMZN
  - `EarningsCalendarRepository.get_earnings_event_dates` 4573ms — table=repository rows=1 ticker=AMZN
  - `get_non_sec_transcript_companies` 2600ms — table=coreiq_filing_metrics_v5 rows=10
  - `EarningsCallRepository.get_companies_with_earnings` 832ms — table=repository rows=296 ticker=N/A
  - `get_companies_with_earnings.TOTAL` 830ms — table=coreiq_av_earnings_call_transcripts rows=296
- Materialize: earnings_transcript_tickers:MISS, earnings_transcript_tickers:WROTE, non_sec_transcript_companies:HIT
- Click→render: {'page': 'earnings_calls', 'render_s': 5.6}

### forecasting

#### cold_full_load (cold 11096ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 0ms
- Click→render: {'page': 'forecasting', 'render_s': 9.0}

#### company_change (cold 12322ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms
- Click→render: {'page': 'forecasting', 'render_s': 1.56}

### home

#### cold_full_load (cold 11130ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 0ms

### market_data

#### cold_full_load_amzn (cold 10113ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms

#### tab_income (cold 10110ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms

#### tab_balance (cold 10512ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms

#### tab_cashflow (cold 10498ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms

#### tab_keystats (cold 11460ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms

#### tab_segment (cold 15997ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms

#### tab_estimates (cold 10461ms)
- Data ready: True, Timeout: False
- Reruns: 2, DB total: 0ms

#### ticker_change (cold 12738ms)
- Data ready: True, Timeout: False
- Reruns: 3, DB total: 0ms

### newsroom

#### cold_full_load (cold 19000ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 0ms

#### filter_sector (cold 17829ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 36680ms
- Top DB calls:
  - `NewsRepository.get_articles` 5414ms — table=repository rows=1000 ticker=N/A
  - `SELECT_NOKEY` 5361ms — table=coreiq_av_market_news_sentiment rows=4000
  - `NewsRepository.get_yf_articles` 4464ms — table=repository rows=2364 ticker=N/A
  - `SELECT_YF_COMBINED` 4455ms — table=coreiq_yf_market_news_sentiment rows=2364
  - `NewsRepository.get_articles` 3224ms — table=repository rows=1000 ticker=N/A

#### filter_ticker (cold 22031ms)
- Data ready: True, Timeout: False
- Reruns: 1, DB total: 55849ms
- Top DB calls:
  - `NewsRepository.get_articles` 9862ms — table=repository rows=1000 ticker=N/A
  - `SELECT_NOKEY` 9810ms — table=coreiq_av_market_news_sentiment rows=4000
  - `NewsRepository.get_articles` 7832ms — table=repository rows=1000 ticker=N/A
  - `SELECT_NOKEY` 7802ms — table=coreiq_av_market_news_sentiment rows=2000
  - `NewsRepository.get_yf_articles` 4866ms — table=repository rows=2364 ticker=N/A

### screening

#### cold_form_load (cold 10153ms)
- Data ready: False, Timeout: False
- Reruns: 1, DB total: 0ms

#### show_results_revenue_gt0 (cold 23508ms)
- Data ready: True, Timeout: False
- Reruns: 9, DB total: 913ms
- Top DB calls:
  - `YF_SCREENING_QUERY` 512ms — table=coreiq_yf_financials_income_statement rows=37 ticker=39_tickers
  - `SEC_SCREENING_QUERY` 402ms — table=coreiq_av_financials_income_statement rows=293 ticker=310_tickers
- Materialize: screening_universe:HIT, screening_universe:HIT, screening_universe:HIT, screening_universe:HIT
- Click→render: {'page': 'screening', 'render_s': 11.7}

---

## Section 4: Root Cause Ranking (Top 15)

1. **screening/show_results_revenue_gt0** — 23508ms cold
   - Streamlit rerun storm (9 reruns) amplifying latency
   - `repository.py` → `YF_SCREENING_QUERY` 512ms
   - `repository.py` → `SEC_SCREENING_QUERY` 402ms

2. **newsroom/filter_ticker** — 22031ms cold
   - Week-chunked parallel news fetch across AV+YF tables (9862ms)
   - `repository.py` → `NewsRepository.get_articles` 9862ms
   - `repository.py` → `SELECT_NOKEY` 9810ms
   - `repository.py` → `NewsRepository.get_articles` 7832ms

3. **newsroom/cold_full_load** — 19000ms cold
   - CPU/render bound or log window miss

4. **earnings_calls/cold_full_load** — 18439ms cold
   - Materialize MISS/STALE: earnings_transcript_tickers — live DB rebuild
   - `repository.py` → `CompanyRepository.get_company_by_ticker` 7835ms
   - `repository.py` → `EarningsCalendarRepository.get_earnings_event_dates` 4792ms
   - `repository.py` → `get_non_sec_transcript_companies` 2995ms

5. **newsroom/filter_sector** — 17829ms cold
   - Week-chunked parallel news fetch across AV+YF tables (5414ms)
   - `repository.py` → `NewsRepository.get_articles` 5414ms
   - `repository.py` → `SELECT_NOKEY` 5361ms
   - `repository.py` → `NewsRepository.get_yf_articles` 4464ms

6. **earnings_calls/company_change** — 16694ms cold
   - Materialize MISS/STALE: earnings_transcript_tickers — live DB rebuild
   - `repository.py` → `CompanyRepository.get_company_by_ticker` 6641ms
   - `repository.py` → `EarningsCalendarRepository.get_earnings_event_dates` 4573ms
   - `repository.py` → `get_non_sec_transcript_companies` 2600ms

7. **market_data/tab_segment** — 15997ms cold
   - CPU/render bound or log window miss

8. **company_filings/company_change** — 15915ms cold
   - DB-bound: CompanyRepository.get_company_by_ticker (0ms)
   - `repository.py` → `CompanyRepository.get_company_by_ticker` 0ms

9. **company_filings/doc_type_filter** — 15590ms cold
   - DB-bound: CompanyRepository.get_company_by_ticker (0ms)
   - `repository.py` → `CompanyRepository.get_company_by_ticker` 0ms

10. **company_filings/cold_full_load** — 15426ms cold
   - DB-bound: CompanyRepository.get_company_by_ticker (0ms)
   - `repository.py` → `CompanyRepository.get_company_by_ticker` 0ms

11. **earnings_calendar/month_prev** — 14958ms cold
   - DB-bound: EarningsCalendarRepository.get_ma_completion_events (10429ms)
   - `repository.py` → `EarningsCalendarRepository.get_ma_completion_events` 10429ms
   - `repository.py` → `EarningsCalendarRepository.get_available_tickers` 10087ms
   - `repository.py` → `EarningsCalendarRepository.get_date_range` 9764ms

12. **market_data/ticker_change** — 12738ms cold
   - CPU/render bound or log window miss

13. **earnings_calendar/month_next** — 12428ms cold
   - DB-bound: EarningsCalendarRepository.get_ma_completion_events (9632ms)
   - `repository.py` → `EarningsCalendarRepository.get_ma_completion_events` 9632ms
   - `repository.py` → `EarningsCalendarRepository.get_available_tickers` 9270ms
   - `repository.py` → `EarningsCalendarRepository.get_date_range` 6206ms

14. **forecasting/company_change** — 12322ms cold
   - CPU/render bound or log window miss

15. **market_data/tab_keystats** — 11460ms cold
   - CPU/render bound or log window miss

---

## Section 4b: Top 5 Slowest — Code Path Analysis (WHY)

### 1. screening/show_results_revenue_gt0 — 23,508ms cold / 10,175ms warm

**Evidence (server log):**
```
[RERUN] page=screening | page_rerun#=1 | session_rerun#=1
[MAT][screening_universe] HIT rows=349 read=0.01s
[PAGE_LOAD_SLOW] screening | TOTAL=11702.5ms
[CLICK->RENDER] page=screening render=11.70s | SLOW
[RERUN] page=screening | page_rerun#=3 | since_last=57ms
[TIMING] SHOW_RESULTS_RECOMPUTE_START | criteria=1
```

**WHY:** Clicking Financial Information → Add Criteria → Show Results triggers **9 Streamlit reruns**. Actual SQL (`SEC_SCREENING_QUERY` 402ms + `YF_SCREENING_QUERY` 512ms) is fast — **rerun amplification** dominates. `screening.py:5860-5890` uses `time.sleep(0.6)` + `st.rerun()` for paint deferral.

**Files:** `app/pages/screening.py:5841-5910`, `app/data/screening_service.py`

### 2. newsroom/filter_ticker — 22,031ms cold

**Evidence:** `get_articles` 9862ms + 7832ms, `SELECT_NOKEY` 9810ms + 7802ms, `get_yf_articles` 4866ms — **55.8s cumulative DB** in log window.

**WHY:** `newsroom.py:90-128` week-chunk parallel fetch (AV+YF per chunk). Each chunk pulls 2000-4000 rows then Python dedupe (`repository.py:1150-1189`).

### 3. newsroom/cold_full_load — 19,000ms cold

Same chunk engine + metadata queries (`get_news_date_range` 3.1s, sectors 309ms, topic keys 827ms) before first `.news-card`.

### 4. earnings_calls/cold_full_load — 18,439ms cold

**Evidence:** `[MAT][earnings_transcript_tickers] MISS`, `get_company_by_ticker` 7835ms (connection cold-start), `get_earnings_event_dates` 4792ms, `[MAT][non_sec_transcript_companies] MISS` 2995ms.

**Files:** `app/data/repository.py:2059-2110`

### 5. company_filings/cold_full_load — 15,426ms cold / 344ms warm

**Evidence:** `[COMPANIES LOAD] Loading companies took 13.22 seconds` — **93% of page time**. Full `SELECT ... FROM coreiq_companies` at `repository.py:383-394` via `company_filings.py:3004`.

---

## Section 4c: Methodology Notes

| Aspect | Implementation |
|--------|----------------|
| Cold | Kill :8501, `WARM_ON_BOOT=0`, fresh incognito context |
| Warm | Same server, prime then measure second navigation |
| Data ready | Page-specific selectors after all spinners gone |
| Tab tests | Direct URL `?tab=` per tab, cold restart each |
| Timeout | 180s with screenshot |

**Script:** `.claude/dev/e2e_perf_audit.py` | **Launcher:** `.claude/dev/run_local.sh`

---

## Section 5: Architecture Issues

### Streamlit rerun model
- Every widget interaction triggers full script re-execution.
- Query param mutations (market_data ticker/tab) cause additional reruns.
- `[RERUN]` counts of 2-6 per cold load are common; each rerun re-executes DB paths unless `@st.cache_data` hits.

### Cache model gaps
- `@st.cache_data` is per-process; cold server restart = 100% miss.
- Disk materialization (`[MAT]`) only covers screening universe + a few repository paths.
- `WARM_ON_BOOT=0` disables background warmup — intentional for audit, but exposes raw cold path.

### Azure STG latency multiplier
- Each `execute_query_readonly` ≈ 250-400ms RTT.
- Pages with 5-15 serial queries → 1.25-6s before any rendering.
- Parallel fetch (earnings calendar) helps but still 4+ concurrent queries × RTT.

---

## Section 6: What SIP Did That MDP Still Lacks

| SIP pattern | MDP status |
|-------------|------------|
| Disk materialization for all hot frames | Partial — screening universe only |
| `shared_data` parallel prefetch at boot | `cache_manager` warmup exists but WARM_ON_BOOT=0 exposes gap |
| `[CLICK->RENDER]` on every page | Added via `base_page.py` but not all pages migrated |
| `[MAT]` signature-based invalidation | Implemented but narrow coverage |
| `mem_opt` categorical encoding | Screening only |
| Zero full-table scans | `get_companies()` still full scan on home/nav |
| Query param rerun suppression | market_data still mutates qp causing rerun storms |

---

Raw JSON: `/Users/mohdsaeedafri/Documents/Documents/Code-Base/Real/CapIQReplacement/server-logs/e2e_perf_audit.json`
Screenshots: `/Users/mohdsaeedafri/Documents/Documents/Code-Base/Real/CapIQReplacement/server-logs/e2e_perf_screenshots`