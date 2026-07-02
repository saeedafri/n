# MDP Cold Matrix (trustworthy filter/tab numbers) + STG DB-Waste Audit

**Date:** 2026-07-03
**Author:** performance audit (Opus) — measurement + investigation only, **no app-code changes**
**Depends on:** `2026-07-02-mdp-sip-parity-audit-and-coldload.md`
**Harness:** new `.claude/dev/cold_matrix.py` (fresh Streamlit process per page → cold full-load →
then each tab/filter as a first-time cold-data interaction, with per-interaction server-log
correlation). STG probe: `scratchpad/stg_waste_probe.py` (read-only `information_schema` + windowed COUNTs).

> "Zero numbers loss" guarantee kept: the only files touched are the **test harness** and **docs**.
> No repository, page, SQL, or render code was modified. App output is byte-identical to before.

---

## 0. What was wrong with the old harness (and what I fixed)

The user was right not to trust the old filter numbers. Root causes found by inspecting the live DOM:

| Page | Old harness bug | Fix in `cold_matrix.py` |
|---|---|---|
| market_data | Clicked **"Income Statement"** — the *already-active* tab → no-op → bogus **53 ms** | Click each *other* tab (Balance Sheet/Cash Flow/Key Stats/Segment/Estimates); wait for tab-specific content |
| newsroom | Opened Sector selectbox before it rendered → `[role=listbox]` never visible | Wait for real content, then `stSelectbox` filtered by label "Sector" |
| earnings_calendar | `.fc-next-button` searched in main page — it lives in the **`streamlit_calendar` iframe** | `frame_locator("iframe[title='streamlit_calendar.calendar']").locator(".fc-next-button")` |
| screening | "United States" geo option flaky | Use the e2e path: Financial Information → value 0 → Add Criteria → Show Results; wait ag-grid |
| forecasting | Picked option text "All Companies" (not always present) | Pick by index; wait for hero/forecast content |
| **all** | `_start()` only waited for the raw socket → `ERR_CONNECTION_RESET` | Poll `/_stcore/health` until 200, + 3× goto retry |
| **all** | `[DATA_VOLUME]` regex expected `table=X rows=N`; real format is `table=X \| rows=N` → parsed rows as 0 | Regex fixed to pipe-delimited |

All 8 pages now measure load **and** every tab/filter cleanly (validated per-page).

---

## 1. Trustworthy Cold Matrix (2026-07-03, fresh server per page)

`ms` = wall-clock to data-ready. `reruns`/`db` from the server-log window for that interaction.
Interactions marked *(cold-data)* run on a warm page shell but fetch that data for the first time —
i.e. the real "user already on the page, switches tab/company" latency.

| Page · interaction | ms | <1s | reruns | DB (ms / calls) | Note |
|---|---:|:--:|:--:|---|---|
| **home** · cold load | 11,547 | ❌ | 2 | 12,629 / 3 | `get_companies` full-scan + BG_SCAN |
| **market_data** · cold load | 8,939 | ❌ | 2 | 14,663 / 10 | 10 queries (parallel) for default AMZN income |
| market_data · tab Balance Sheet *(cold-data)* | 2,564 | ❌ | 1 | 307 / 4 | pivot query fast; rerun + render bound |
| market_data · tab Cash Flow *(cold-data)* | 2,547 | ❌ | 1 | 299 / 4 | " |
| market_data · tab Key Stats *(cold-data)* | 2,544 | ❌ | 1 | 1,495 / 1 | single 1.5s stats query |
| market_data · tab **Segment** *(cold-data)* | 6,117 | ❌ | 1 | **0 / 0** | **uninstrumented DB + HTML build** (see §4) |
| market_data · tab Estimates *(cold-data)* | 2,535 | ❌ | 1 | 0 / 0 | cached/uninstrumented |
| **newsroom** · cold load | 7,149 | ❌ | 1 | 0 / 0 | (fetch logged under AV/YF ops, see §3) |
| newsroom · **filter Sector** *(cold-data)* | 10,026 | ❌ | 1 | **41,268 / 13** | week-chunk fan-out; 41s cumulative DB |
| **earnings_calls** · cold load | **81,972** | ❌ | 2 | **89,859 / 13** | cold-buffer + MAT miss (unstable — see §2) |
| earnings_calls · company change *(cold-data)* | 2,878 | ❌ | 1 | 3,848 / 7 | warm shell |
| **earnings_calendar** · cold load | 25,376 | ❌ | 1 | 45,977 / 5 | 3 heavy calls + missing `coresight_ma…` table |
| earnings_calendar · month next *(cold-data)* | 2,659 | ✅-ish | **0** | **0 / 0** | **client-side** within fetched window (no server hit) |
| earnings_calendar · company filter *(cold-data)* | 2,601 | ❌ | 1 | 0 / 1 | |
| **screening** · cold load | 11,788 | ❌ | 1 | 0 / 0 | segment-options cache build |
| screening · **Show Results** *(cold-data)* | **128,477** | ❌ | 5 | **2,255 / 3** | **rerun storm**: 2 min wall, 2.3s DB (see §2) |
| **company_filings** · cold load | 28,187 | ❌ | 1 | 30,317 / 3 | cold buffer on companies + filings |
| company_filings · company change *(cold-data)* | 2,876 | ❌ | 0 | 0 / 0 | companies cached |
| company_filings · doc-type filter *(cold-data)* | 3,851 | ❌ | 1 | 0 / 0 | v5 prefetch (indexed) |
| **forecasting** · cold load | 10,516 | ❌ | 1 | 0 / 0 | uninstrumented DB (see §4) |
| forecasting · company change *(cold-data)* | 2,660 | ❌ | 1 | 0 / 0 | |

**Cold page loads: 0/8 < 1s** (7.1s → 82s). **Cold-data interactions: 0/19 < 1s** except the
client-side calendar month-nav. Even the *warm-shell* tab/company switches sit at **2.5–6s** — so the
SLA gap is not only first-load; **every data-touching interaction misses the 1s target.**

---

## 2. Two findings the numbers make undeniable

### 2a. Cold latency is not just high — it's *unstable*
`earnings_calls` cold load measured **7.6s** (2026-07-02 run) and **82.0s** (2026-07-03 run) on the
same code. The 82s run shows `DB 89,859ms / 13 calls` — the cold Azure buffer pool + a materialize
**miss** on `earnings_transcript_tickers` (the DISTINCT-ticker query the db-opt spec claims is fixed
fired cold here). **Implication:** the fix works only when the `[MAT]` file already exists; a fresh
process/deploy before warmup still eats the full penalty. Warm-on-boot ordering + a guaranteed MAT
write pre-first-request is required, not just the MAT capability.

### 2b. screening Show Results = rerun storm, quantified
Wall **128,477 ms** with **DB only 2,255 ms** and **5 reruns** in the window. 98% of the wall time is
**not** the database — it is Streamlit re-executing the page (the `time.sleep(0.6)` paint-deferral +
repeated `st.rerun()` in `_render_results`; screening has **71** `st.rerun()` calls total). This is the
single highest-value fix and it is pure transport/rerun (SIP Batch-4 pattern), **no data change**.

---

## 3. STG DB-Waste Audit — "fetching everything, showing less"

### 3a. Real table sizes (STG, `information_schema`)

| Table | Rows | Size | Role |
|---|---:|---:|---|
| `coreiq_filing_metrics_v5` | **14,398,269** | **23.3 GB** | filings metrics — the *old* dropdown scanned `DISTINCT ticker` here (337 distinct) → now uses `coreiq_companies` ✅ |
| `coreiq_av_market_news_sentiment` | **1,447,393** | **7.1 GB** | newsroom AV source |
| `coreiq_av_time_series_daily` | 1,450,528 | 663 MB | daily bars (AMZN alone = **6,699** rows) |
| `coreiq_yf_market_news_sentiment` | 373,005 | 1.5 GB | newsroom YF source |
| `coreiq_av_earnings_call_transcripts` | 20,020 | 1.36 GB | fat transcript text |
| `coreiq_av_financials_income_statement` | 24,177 | 42 MB | market_data pivots |
| `coreiq_companies` | **349** | **~0 MB** | company master |

### 3b. The waste, per hot path (fetched vs returned vs displayed)

| Path | Fetched from DB | Returned to page | Rendered | Actually viewed | Verdict |
|---|---|---|---|---|---|
| **newsroom AV (per chunk)** | **4,000 rows** (`[DATA_VOLUME] rows=4000 op=SELECT_NOKEY`, on the **7 GB** table) | **1,000** (dedupe, `rows=1000 op=get_articles`) | up to `_PAGE_SIZE=1000` cards | ~a dozen above fold | **row over-fetch 4×; render over-build ~20×** |
| newsroom YF (per chunk) | 2,364 rows | subset | " | " | same shape |
| newsroom **whole default window** | multiple chunks × (4000 AV + 2364 YF) — DB has **5,466 AV in 7 days, 36,719 in 30 days** | ≤1000/chunk | ≤1000 | dozens | multi-chunk × 2 sources × 6 workers |
| `get_companies_rows` | 349 rows × 8 cols (~0 MB) | 349 | dropdown (2 cols) | dropdown | **NOT waste** — cols used by screening geo/industry; cost is the cold **full-scan latency**, not volume |
| market_data financial pivots | 20–40 periods, 12–18 line items | same | full grid | full grid | **NOT waste** — all shown |
| `get_latest_quote` | `LIMIT 1` (reads a `raw_json` blob) | 1 | 1 | 1 | bounded ✅ |
| earnings_calendar | now 42-day windowed (~197 events) | same | calendar | calendar | already fixed ✅ |

**Column waste:** minimal everywhere — `get_companies_rows` fetches `country_of_incorporation` unused
by the *map* build but needed by *screening*; news selects 9 cols, all rendered. **The waste is ROWS,
concentrated in newsroom** (4,000 → 1,000 → ~dozen, against a 7 GB table, ×chunks ×2 sources).

### 3c. Newsroom over-fetch knobs (code)
- `av_limit=1000, yf_limit=2000` per chunk, `_OVERFLOW_MULTIPLIER=2` → **4,000 AV** raw/chunk (dedup ratio measured **1.41×**, so ~1.5× overflow would suffice — the extra 0.5× is pure waste).
- `_CHUNK_DAYS=7`, `_MAX_WORKERS=6` on a 2-core Azure box (SIP found workers should equal cores = **2**; 6 makes each query slower and storms connections).
- `_PAGE_SIZE=1000` renders up to 1,000 news-card HTML strings for a viewport that shows ~a dozen.

**Safe fixes (byte-identical):** overflow ×2 → ×1.5; initial fetch = current week only, lazy-load older
on scroll; push the dedupe key into SQL (`GROUP BY normalized_title`) so raw≈returned; workers 6 → 2;
progressive card render. None change which articles are shown, only how many rows are moved/built.

---

## 4. Observability blind spots discovered during measurement

| Blind spot | Evidence | Fix |
|---|---|---|
| **Segment tab DB uninstrumented** | tab_segment = 6.1s but `DB 0/0`; `get_segment_data`/`get_available_dates`/`get_date_range` not wrapped in `log_db_timing` | wrap those repo methods → their cost becomes visible on STG |
| **Forecasting DB uninstrumented** | cold 10.5s, `DB 0/0`; `RevenueForecastService` reads not timed | wrap forecast reads in `log_db_timing` |
| **Newsroom fetch magnitude only partly visible** | `[DATA_VOLUME]` fires for AV/YF (rows=4000/2364) but the *chunk fan-out total* isn't summarized | add a `NEWSROOM_CHUNK_FETCH` summary line (raw_total, returned_total, displayed) |
| **`[MEM_OPT]` inert** (from prior doc) | 0 lines — only frame wrapped is 349-row universe < 1000 floor | apply `optimize_frame_memory` to the **news article frame** (thousands of rows) instead |
| **`[RERUN_TRIGGER]` dead** (from prior doc) | screening 71 `st.rerun()`, 5 reruns in Show Results window, still 0 triggers | fix `_patch_st_rerun_once`; without it STG can't attribute the storm to a line |

---

## 5. Refined fix priority (unchanged order, sharpened by evidence)

1. **P0 — screening Show Results rerun storm** — 128s→~2s. Remove `sleep(0.6)` + collapse `st.rerun()` in `_render_results`. DB is already 2.3s. *(SIP Batch-4)*
2. **P0 — earnings_calls / earnings_calendar / company_filings cold** — guarantee `[MAT]` write + warm-on-boot **before first request**; guard the missing `coresight_ma…` table. Kills the 25–82s cold cliffs.
3. **P0 — materialize + share the `coreiq_companies` map** (349 rows, ~0 MB, but full-scan latency dominates home/nav/filings cold).
4. **P1 — newsroom row over-fetch**: overflow ×2→×1.5, current-week-first + lazy older, SQL-side dedupe, workers 6→2, progressive render. Against a 7 GB table this is the biggest DB-load win.
5. **P1 — market_data**: suppress QP-mutation rerun; warm default-ticker pivots at boot (tabs 2.5s→ms).
6. **P2 — observability**: instrument segment/forecasting DB, fix `[RERUN_TRIGGER]`, add newsroom `[CLICK->RENDER]`, move `[MEM_OPT]` to the news frame, add chunk-fetch summary.

---

## 6. For the STG push (what to grep in the real logs)

After you deploy and drive the pages on STG, these confirm the diagnosis on real hardware
(2-core, glibc — where `[MALLOC_TRIM]`/`[RAM]` also start firing, unlike macOS):

```bash
LOG=/home/LogFiles/mdp/server-log.log
grep -a "\[DATA_VOLUME\].*news_sentiment" "$LOG"        # expect rows=4000 AV / 2364 YF per chunk
grep -a "AV_ARTICLES_DEDUPE" "$LOG"                     # raw=… unique=… dupes=… (measure real dedup ratio)
grep -a "NEWSROOM_CHUNK_FETCH" "$LOG"                   # chunk count × per-chunk rows
grep -a "SHOW_RESULTS_RERUN_TOTAL" "$LOG"               # page_rerun# on Show Results (the storm)
grep -a "\[MAT\]\[earnings_transcript_tickers\]" "$LOG" # HIT vs MISS on first request (2b)
grep -a "\[CLICK->RENDER\].*SLOW" "$LOG"
grep -a "1146.*coresight_ma" "$LOG"                     # missing-table errors (should be 0 after guard)
grep -a "\[RERUN_TRIGGER\]" "$LOG"                      # currently empty — must be non-empty after fix
```

**Artifacts:** `server-logs/cold_matrix.json`, `server-logs/cold_matrix_shots/`,
`server-logs/cold_matrix_run.log`.
