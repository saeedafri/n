# Full-app performance & UX audit — 2026-07-18

**Method:** parallel per-page code audit (15 targets) + live-UI drive (Playwright, video recorded) + server-log timings. The audit surfaced **57 findings**; the verify pass hit a session rate-limit, so remaining findings are verified by hand as they're fixed.

## Measured page load (first load; cold cache + STG DB over VPN — the *structural* issue is what matters; STG in-region is faster)

| Page | Before | After |
|---|---|---|
| home | 4.8 ms | 4.8 ms (fixed earlier — scanner block) |
| company_filings | 663 ms | 663 ms + **flicker/multi-MB re-transmit removed** ✅ |
| market_data | 2,092 ms | pending |
| screening | 4,004 ms | pending |
| forecasting | 4,156 ms | pending |
| earnings_calendar | 8,184 ms | pending |
| **newsroom** | **10,011 ms** | **113 ms** ✅ |
| **earnings_calls** | **14,631 ms** | **0.29 s** (cache-warm) ✅ |

## FIXED & verified this session

1. **company_filings — SEC iframe re-transmit (flicker, CRITICAL).** `nonce` included `time.time_ns()`, so the multi-MB filing HTML was re-sent + the iframe re-mounted on *every* rerun (blank-flash). Keyed nonce on `(html_path, highlight_fact_id)` only → unchanged views skip the re-mount.
2. **newsroom — left-list render (10s).** Left panel built HTML + regex-highlight for *every* article (`_LEFT_RENDER_LIMIT = 2,000,000` = no cap). Capped to 250 (the "+N more" note already existed). **10,011 ms → 113 ms.**
3. **earnings_calls — transcript PDF (14.6s).** Full PDF regenerated on every rerun of the single-transcript view. Memoised per `(company, year, quarter, date_line)`, bounded to 8 (also caps the RAM leak). **14.63 s → 0.29 s.**
4. **logs — Download Logs eager read (CRITICAL).** `data=download_logs()` re-read ALL log segments into RAM every rerun (Logs auto-refreshes). Now cached on the log file's `(mtime, size)` (ttl 20s).
5. **logs console (secret CMD)** — spinner + audit logging + confirmed it never blocks the whole app (subprocess releases the GIL; only the caller's tab waits). *(earlier this session)*

6. **company_filings — pdf_b64 RAM leak.** Each filing's base64 PDF (1–24 MB) was cached in `session_state` forever as the user browsed. Now LRU-evicted to the last ~2.

## KEY INSIGHT — the app is fast when WARM (measured 2nd-load)
The first-load numbers above are **cold cache** (heavy CTE/forecast/16k-event DB queries, all already `@st.cache_data`'d). On the **warm** re-render they are fine:

| Page | Cold | **Warm (2nd load)** |
|---|---|---|
| market_data | 2,086 ms | **308 ms** |
| forecasting | 4,155 ms | **690 ms** |
| earnings_calendar | 8,184 ms | **0.04 s** |

So the only pages that were slow on *every* render — newsroom and earnings_calls — are the ones fixed above. market_data / forecasting / earnings_calendar cold cost is inherent DB query time (mitigated by caching + the main.py startup warmups; faster on STG in-region than over the dev VPN). The remaining wins are cold-start pre-warming (optional) and the items below.

**Regression check:** all edited pages (company_filings, newsroom, earnings_calls, logs) re-driven post-fix — render clean, 0 crashes, 0 JS page-errors.

## Prioritized REMAINING findings (top, by user impact)

**Client-facing, high value:**
- **market_data** — `company_profile` runs an uncached shares+price JOIN every rerun purely to prep the Excel download; ~10 Plotly figures rebuilt every rerun; company selectbox `format_func` is O(n²). Fix: lazy Excel, cache figures, precompute a name map.
- **forecasting** — Excel workbook built eagerly on the render path for every ticker/horizon. Fix: build only on button click.
- **earnings_calendar** — every month-nav / view-switch forces a guaranteed **second full render** (`datesSet → st.rerun`), and filter-pill toggles remount the whole calendar iframe (blank flash). Fix: pre-seed `ec_visible_start/end` to the range FullCalendar will report.
- **Google Fonts `@import`** re-injected on every rerun on several pages (newsroom/home/others) → render-blocking text flash. Fix: inject once / use cached `<link rel=preconnect>`.

**Admin / less-hot but real:**
- **access_management** — 5 sequential uncached Azure round-trips/render (~1.25s), `get_all_users()` called twice, a `SELECT 1` health check every rerun. Fix: cache (ttl 30) / fetch once.
- **retailer_adding** — `build_union_df()` full-scans two company tables eagerly for a download; `ensure_audit_table()` runs DDL + 3 INFORMATION_SCHEMA probes every rerun; `get_pk_and_insertable_columns()` re-queries schema every rerun. Fix: cache / guard once-per-process.
- **company_filings** — `pdf_b64` base64 blobs accumulate unbounded in `session_state`; pdf.js loaded from external CDN on the render path.

**RAM / lower:** several pages re-inject 300+ line CSS via `st.markdown` every rerun; per-session Excel/HTML byte blobs never evicted; a few `@st.cache_data` caches lack `max_entries`.

## Notes
- Full per-finding evidence (file:line) is in the workflow journal: `…/subagents/workflows/wf_2f804002-363/journal.jsonl`.
- Re-run the verify pass after the rate-limit resets to auto-confirm the remaining ~50 before fixing.
