# MDP ↔ SIP Parity Audit + Fresh Cold-Load Evidence

**Date:** 2026-07-02
**Author:** performance audit (Opus) — evidence-based, no perf fixes applied
**Scope:** CapIQReplacement (Coresight Research Portal / MDP)
**Reference build:** SIP `Modular-Code` (`server_logger.py`, `sip_components/{base_page,mem_opt,materialize}.py`)
**Method:** SIP↔MDP source diff · real server-log tag-firing counts · fresh `cold_load_benchmark.py` (fresh Streamlit process + incognito per page, `WARM_ON_BOOT=0`) · one controlled Playwright probe

> Supersedes stale claims in the sibling specs where re-verified below. Where this doc and
> `2026-07-02-mdp-performance-investigation-report.md` disagree, **this doc's numbers are the
> freshly re-measured ones** and win.

---

## TL;DR

1. **Observability is ~90% at SIP parity** and the core tags fire. Three real gaps:
   **`[RERUN_TRIGGER]` is dead** (monkey-patch not capturing `st.rerun()`), **newsroom has no
   `[CLICK->RENDER]`**, and **`[MEM_OPT]` is inert** (only frame it wraps is below its 1000-row floor).
2. **Fresh cold load = 0/8 pages under 1s.** Median **~11.6s**, worst **30.0s** (earnings_calendar)
   and **27.2s** (company_filings). This is *worse* than the prior audit's numbers for several pages
   — the prior audit was not fully cold for those.
3. Current-code error rate is low (**45** structured errors *today*, not the 1,917 cumulative in the
   multi-day log). The one live error is a **missing `coresight_ma…` table on STG** that throws on
   every earnings_calendar load.
4. Fix order: kill the rerun-storm & full-scan cold paths (P0), then materialize the remaining cold
   frames + warm-on-boot (P1), then close the 3 observability gaps (can ship alongside).

---

## Deliverable 1 — Observability Gap Report (SIP vs MDP)

Legend: ✅ at parity & verified firing · ⚠️ present but degraded/inert/platform-limited · ❌ missing or dead.
Counts are from `server-logs/server-log.log` (multi-day) unless marked "today"/"probe".

| SIP capability | SIP source | MDP status | Evidence | Gap action |
|---|---|---|---|---|
| `[BOOT]` + `defaults_applied` | `server_logger` | ✅ | 85 lines; `defaults_applied=[MALLOC_ARENA_MAX=2, …]` present | — |
| `[HEARTBEAT]` + RAM/uptime | `server_logger` | ✅ | 164 lines | — |
| `[RAM_CENSUS]` | `server_logger` | ✅ | 25 lines | — |
| `[RAM]` (per-page `log_ram`) | `server_logger` | ⚠️ | **0** on macOS (`ram_snapshot_mb()` → None, early-returns) | Verify fires on Linux/STG (psutil/`/proc`) |
| `[RERUN]` + counters | `base_page`/`new_rerun_id` | ✅ | 280 lines; effective emitter is `server_logger.py:145 new_rerun_id` (single-emit, no double-count observed in probe) | — |
| `[RERUN_TRIGGER]` (st.rerun trace) | `base_page._patch_st_rerun_once` | ❌ **DEAD** | **0 ever**, incl. 20× `SHOW_RESULTS` on 02-Jul *after* patch went live (01:06 PM). screening calls `st.rerun()` 71× → should trace | **P-obs-1**: patch not capturing `st.rerun`; fix + boot-confirm line |
| `[CLICK->RENDER]` | `log_render_complete` | ⚠️ | Fires for market_data/screening/forecasting/EC/earnings_calls/company_filings/home. **newsroom=0, live_earnings_transcript=0** | **P-obs-2**: add to newsroom `render_page()` tail |
| `user_analytics.log` (`page_view`) | `log_user_event` | ✅ | file present; JSON-lines events | — |
| `[MAT]` HIT/MISS/STALE/WROTE | `materialize.py` | ✅ (⚠️ coverage) | 47 lines; only `screening_universe`, `earnings_transcript_tickers`, `non_sec_transcript_companies` | Widen coverage (see D5) |
| `[MEM_OPT]` categorical shrink | `mem_opt.py` | ⚠️ **INERT** | **0** — only applied frame is screening universe (~349 rows) < `min_rows=1000` | **P-obs-3**: lower floor or apply to a ≥1k-row frame (news/financials) |
| `[AUDIT]` frame-number census | `mem_opt.audit_frame_numbers` | ✅ | 153 lines | — |
| `[DATA_VOLUME]` (rows≥1000) | MDP addition | ✅ | 32 lines (beyond SIP) | — |
| `[TIMING] DB_*` | `log_db_timing` | ✅ | 1,203 lines | — |
| `[TIMING] TAB_*` per-tab | `market_data.py:4158` | ✅ | **probe-verified**: `TAB_Income_Statement` + `TAB_Balance_Sheet` fired. (Prior "never" was a stale-log artifact) | — |
| `PAGE_SUMMARY` | `market_data.py:4163` | ✅ | probe-verified (2×) | — |
| `[TIMING] FILINGS_LOAD/PREFETCH` | `company_filings.py:3970` | ✅ | fired 11:53 PM (cold filings load) | — |
| `[SHOW_RESULTS]` | `screening.py` | ✅ | 26 lines | — |
| `restarts.log` non-rotating | `server_logger` | ✅ | 6 KB ledger present | — |
| QueueHandler/Listener non-blocking | `server_logger` | ✅ | `QueueHandler`+`QueueListener`+`RotatingFileHandler(25MB×8)` | — |
| `[MALLOC_TRIM]` | heartbeat/nav | ⚠️ | **0** on macOS (glibc-only). Expect on Linux/STG | Verify on STG |
| `[STRUCTURED_ERROR]` | `log_structured_error` | ✅ | present on every registered page; **45 today**, 1,917 cumulative | Fix root errors (D6) |
| `/logs` live tail + slow-ops | `pages/logs.py` | ✅ | `get_log_content` + `analyze_slow_operations` present | — |
| Per-page `new_rerun_id` first line | all pages | ✅ | present in all 8 registered content pages | — |

**Materialize richness gap (LOW):** MDP `materialize.py` is a faithful but trimmed SIP port — it lacks
SIP's `engine_factory` param and the verbose STALE line (`stored=… vs live=…`, size/fingerprint detail).
Functionally equivalent (HIT/MISS/STALE/WROTE all present); only diagnostic detail is thinner.

---

## Deliverable 2 — Fresh Cold Benchmark (wall-clock, truly cold)

`cold_load_benchmark.py` — fresh Streamlit process + fresh incognito context **per page**,
`WARM_ON_BOOT=0`, STG DB, measured goto→data-ready. Run 2026-07-02 ~23:5x.

| Page | **Cold full-load (ms)** | <1s? | vs prior audit | Notes |
|------|------------------------:|:----:|---|---|
| newsroom | 7,364 | ❌ | 19,000 | prior audit was warmer / different ready predicate |
| earnings_calls | 7,639 | ❌ | 18,439 | materialize now helps; still cold Azure chain |
| market_data | 8,571 | ❌ | 10,113 | warm tab-switch (same server) = **53 ms** ✅ |
| screening | 11,662 | ❌ | 10,153 | form load; segment-cache build |
| forecasting | 11,618 | ❌ | 11,096 | companies dropdown + chart |
| home | 12,292 | ❌ | 11,130 | `get_companies()` full scan + BG_SCAN |
| company_filings | **27,214** | ❌ | 15,426 | **worse than documented** — not fully cold before |
| earnings_calendar | **30,015** | ❌ | 11,421 | **worst**; missing `coresight_ma…` table adds failed-query RTTs |

**Aggregate:** 0/8 < 1s. min **7,364** · median **~11,640** · max **30,015**.
**Warm signal:** the one clean warm measurement (market_data tab-switch, same process) = **53 ms** —
confirms the SLA thesis: *warm can be ms; cold is the whole problem.*

> ⚠️ Filter/interaction numbers in the JSON are unreliable — the harness selectors
> (`[role=listbox]`, `.fc-next-button`, "United States" option) time out on 6/8 pages. Cold
> page-load numbers are valid; **the filter harness needs fixing before filter-level cold numbers
> can be trusted** (see Fix Plan P2-obs).

---

## Deliverable 3 — Root-Cause Map (table / columns / rows / wasted-data per slow cold path)

| Cold path | Function / SQL | Table(s) | Cols | Rows | Used in UI | Waste | Root cause |
|---|---|---|---|---|---|---|---|
| home, company_filings, nav, screening universe | `CompanyRepository.get_companies_rows` (`repository.py:368`) — `SELECT ticker,name,name_coresight,exchange,source,exchange_acronym,primary_industry_coresight,country_of_incorporation FROM coreiq_companies` (no WHERE) | `coreiq_companies` | 8 | ~389 | 2 (ticker+name) | **~75% cols** | Full scan on cold buffer pool + `@st.cache_data` miss per restart |
| earnings_calendar cold (30s) | 3 heavy serial repo calls + **missing table** | EC calendar + `coresight_ma…` (MISSING on STG) | events/tickers/min-max | 197 windowed | yes | failed-query RTTs | `ProgrammingError 1146` on M&A table → ret/except overhead every load |
| newsroom load/filter | week-chunk fan-out `NewsRepository.get_articles` (`repository.py:899`) | `coreiq_av_market_news_sentiment`, `coreiq_yf_market_news_sentiment` | 9 outer | 2000–4000 raw→1000/chunk | ~50–100 cards | **50–80% rows** | limit×2 overflow + Python dedupe across 4–6 chunks × 2 sources |
| screening Show Results | 9-rerun storm; SQL only ~900 ms | AV/YF income | ticker+rev | 293+37 | yes | — | `time.sleep(0.6)` + repeated `st.rerun()` in `_render_results` |
| market_data each tab | `_fetch_all_annual_rows` pivots | AV/YF financials | 12–18 line items | 20–40 periods | yes (all) | minimal | 2 reruns/tab + cold pivot; **data waste minimal — this is transport/rerun bound** |
| market_data Key Stats | overview + daily price series | `coreiq_av_company_overview` + time series | many | 1 + ~500 daily | partial | some | extra overview+price query (~2.9s cold) |
| earnings_calls cold | `get_company_by_ticker` + event dates + non-SEC tickers | `coreiq_companies`, `coreiq_av_earnings_call_transcripts`, `coreiq_filing_metrics_v5` | lookup | 1+1+10–296 | yes | — | MAT MISS on first cold + serial Azure chain |
| forecasting cold | `RevenueForecastService.get_companies` (~680ms) + chart | forecast + companies | list | ~337 | yes | — | dropdown + chart render, no warm |

Cross-cutting multipliers (unchanged, re-confirmed): Azure STG RTT ~250–400ms × serial chains;
full-table `coreiq_companies` scan on cold miss; newsroom chunk fan-out; Streamlit rerun amplification;
narrow `[MAT]` coverage so most pages cold-miss on restart.

---

## Deliverable 4 — Rerun Forensics

**`st.rerun()` census (129 calls):**

| File | Calls | Hot path |
|---|---:|---|
| `pages/screening.py` | **71** | Show Results overlay + criteria edits (the storm) |
| `pages/login.py` | 8 | auth flow |
| `pages/live_earnings_transcript.py` | 7 | live polling |
| `pages/access_management.py` | 6 | admin CRUD |
| `utils/market_data.py` | 5 | currency/units/sort QP sync (each `st.rerun()`) |
| `pages/logs.py` | 4 | admin |
| `pages/earnings_calls.py` | 4 | company/transcript select |
| `pages/earnings_calendar.py` | 4 | month nav / filter |
| `pages/company_filings.py` | 4 | company/doc select |
| `core/auth_manager.py` + `components/auth_utils.py` | 4 | cookie retry |
| others (forecasting_admin, admin_iam, retailer_adding, …) | 12 | admin |

**Per-interaction rerun counts (from log + probe):**

| Interaction | Reruns (cold) | Source |
|---|---:|---|
| screening Show Results | **~9** | `_render_results`: sleep(0.6) → `st.rerun` → recompute → `st.rerun` |
| market_data tab / ticker | 2–3 | native widget rerun + QP-sync `st.rerun` in `utils/market_data.py` |
| earnings_calls | 2 | company select + transcript load |
| home / newsroom / EC | 1 | single pass unless auth retry |
| auth (any) | +0–4 | `require_auth` cookie retry `[1,2,3,4]s` |

**Forensics tooling is broken:** `[RERUN_TRIGGER]` — the tag that pins *which* `st.rerun()` line
fired — **never emits** (0 across the whole log incl. 20 post-patch Show Results on 02-Jul). So today
you can see *that* reruns happened (`[RERUN]`) but not *which code line* triggered them. **Fixing
P-obs-1 is a prerequisite for trustworthy rerun forensics on STG.** Probe confirmed a native
tab-click produces `[RERUN]` with no `[RERUN_TRIGGER]` (correct — native widget rerun), but the
explicit-`st.rerun()` screening path also produces none (incorrect — should trace).

---

## Deliverable 5 — Prioritized Fix Plan (impact-ordered, SIP-methodology)

All fixes are the three SIP-safe classes only: **transport/parallelism**, **algebraically identical
SQL**, **caching/materialization**. Byte-identical UI/data required (parity proof before/after).

### P0 — kill the two worst cold offenders (biggest wall-clock wins)

| # | Target | Change | Expected |
|---|---|---|---|
| P0-1 | **screening Show Results (rerun storm)** | Remove `time.sleep(0.6)` + collapse the 3× `st.rerun()` in `_render_results` to a single state-driven pass (SIP pattern: no paint-deferral reruns). SQL is already ~900ms. | 23.5s → ~1–2s |
| P0-2 | **earnings_calendar missing `coresight_ma…` table** | Guard/fallback the M&A query (feature-flag or `SHOW TABLES` probe) so a schema-absent STG returns `[]` fast instead of throwing per load; **and** create the table on STG. | removes failed-query RTTs + 30s→ windowed cost |
| P0-3 | **`get_companies_rows` full scan** | Disk-materialize the companies map (SIP `materialized_or_build`, signature on `coreiq_companies`) and share it across home/nav/company_filings/screening/earnings_calls; SELECT only the 2 columns dropdowns use in the dropdown path. | ~13s → ~1–2s on cold restart |

### P1 — materialize remaining cold frames + warm-on-boot

| # | Target | Change | Expected |
|---|---|---|---|
| P1-1 | **newsroom chunk fan-out** | Push dedupe to SQL (`GROUP BY`/`DISTINCT` on the merge key) so raw≈final; shrink initial window to 1 chunk, lazy-load older on scroll. | 19s → few s |
| P1-2 | **company_filings cold (27s)** | Already switched dropdown to `coreiq_companies`; add it to the shared materialized companies map (P0-3) + warm at boot. | 27s → ~2s |
| P1-3 | **market_data tabs** | Warm the default ticker's income/balance/cashflow pivots at boot; suppress the QP-mutation rerun (write QP without a second rerun — SIP QP-suppression). | 8–16s → ~1–2s |
| P1-4 | **home / forecasting / earnings_calls** | Ensure `WARM_ON_BOOT=1` sequential warmup covers each page's dropdown + first payload; verify `[MAT] HIT` on restart. | cold→warm on 2nd process life |

### P2 — observability parity (ship alongside; small)

| # | Target | Change |
|---|---|---|
| P-obs-1 | `[RERUN_TRIGGER]` dead | Verify `_patch_st_rerun_once` actually reassigns `st.rerun` on this Streamlit version (log a one-time `[BOOT] rerun_trace_installed=<bool>`); if the module attr won't stick, wrap at the call sites' import or hook inside `new_rerun_id`. Re-run screening Show Results probe → expect ≥1 `[RERUN_TRIGGER] screening.py:*`. |
| P-obs-2 | newsroom `[CLICK->RENDER]` | Add `log_render_complete("newsroom", …)` at `render_page()` tail (mirror the other 6 pages). |
| P-obs-3 | `[MEM_OPT]` inert | Lower `min_rows` for the screening frame **or** apply `optimize_frame_memory` to a genuinely large frame (news articles, financial long-format) so categorical shrink actually runs. |
| P-obs-4 | filter harness | Fix `cold_load_benchmark.py` selectors (`[role=listbox]`, `.fc-next-button`, MultiSelect "United States") so filter-level cold numbers become measurable — 6/8 filters currently error. |

---

## Deliverable 6 — STG Verification Plan (post-deploy grep card)

```bash
LOG=/home/LogFiles/mdp/server-log.log

# boot + defaults took effect
grep -a "\[BOOT\].*defaults_applied" "$LOG" | tail -3

# cold→warm proof on the P0/P1 targets
grep -a "\[MAT\]\[screening_universe\]"      "$LOG" | tail   # expect HIT within 90s of boot
grep -a "\[MAT\]\[earnings_transcript_tickers\]" "$LOG" | tail
grep -a "DB_get_companies_rows"              "$LOG" | tail   # ms should drop on 2nd hit / show MAT HIT
grep -a "\[CLICK->RENDER\].*SLOW"            "$LOG" | tail -20

# rerun storm fixed (screening)
grep -a "SHOW_RESULTS_RERUN_TOTAL"           "$LOG" | tail   # page_rerun# should be ~1, not 8–9
grep -a "\[RERUN_TRIGGER\].*screening"       "$LOG" | tail   # MUST be non-empty once P-obs-1 lands

# obs gaps closed
grep -a "rerun_trace_installed"              "$LOG" | tail   # =True
grep -a "\[CLICK->RENDER\] page=newsroom"    "$LOG" | tail   # non-empty
grep -a "\[MEM_OPT\]"                        "$LOG" | tail   # non-empty

# error floor (should be schema-clean)
grep -a "\[STRUCTURED_ERROR\]"               "$LOG" | grep -oE "component=[^ |]+" | sort | uniq -c | sort -rn | head
grep -a "1146.*coresight_ma"                 "$LOG" | tail   # MUST be empty after P0-2
```

**Acceptance targets after fixes:** screening Show Results `SHOW_RESULTS_RERUN_TOTAL page_rerun#≈1`;
`[MAT][…] HIT` on every restart for the shared companies map; `[CLICK->RENDER]` present for **all 8**
content pages incl. newsroom; `1146 coresight_ma` errors = 0; cold P0 targets < ~2s (path to <1s SLA
continues via P1 warm-on-boot + per-page pivot warmup).

---

## Appendix — What changed vs the prior specs (corrections)

| Prior claim | This audit's finding |
|---|---|
| `[MEM_OPT]` "partial / no tag yet" | Tag **exists** and works, but is **inert** (frame < 1000-row floor) |
| `[AUDIT]` "partial / ad-hoc" | `[AUDIT]` fires 153× via `audit_frame_numbers` — at parity |
| `[TIMING] TAB_` / `FILINGS_LOAD` added | **Verified firing** (probe + 11:53 PM log); prior whole-log "0" was a pre-addition/slice artifact |
| home/forecasting "missing `log_render_complete`" | Now **present** (2 each); only **newsroom** + live_earnings_transcript lack it |
| 1,917 structured errors | **Cumulative multi-day**; current-code today = **45**, dominated by missing `coresight_ma…` table |
| Cold ~11–23s (prior audit) | **Re-measured fully cold**: EC **30s**, company_filings **27s** — prior audit under-measured these |
| `[RERUN]` possible double-count | **Not observed** — single-emit from `new_rerun_id`; bootstrap's line did not double-fire in probe |
