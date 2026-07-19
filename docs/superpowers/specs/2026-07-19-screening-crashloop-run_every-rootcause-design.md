# STG Screening Crash-Loop — Root Cause & Fix (19-Jul-2026)

## Symptom (production, marketdata-stg / `/screening`)
- Repeated **503 "Connection failed"** on the screening page.
- Stuck **"Applying criteria…"** spinner; controls greyed; **page would not scroll**.
- App felt "restarted every time."

## Evidence (log `server-logs-20260719_084400.log`)
- **18 server boots today**, in rapid **crash-loop bursts**: 04:32–04:40 AM (5 boots / 8 min)
  and **02:08–02:13 PM (4 boots / 5 min)**. That cadence = crash loop, not deploys.
- **All 5 sampled restarts were preceded by `page=screening`** — every other page was fine.
- **NOT memory:** RSS at the 2 PM crashes was **260–289 MB**, `avail` **2.5 GB**. No OOM,
  no `Killed`/`SIGKILL`, no `MemoryError`. (The 3.7 GB peak in the log is the old 18-Jul
  ratings-sweep incident, already disabled — unrelated.)
- **NOT the DB pool:** `pool_checkedout=1` at the crash. No exhaustion.
- **NOT the background threads I added:** zero `KEYDEV_EXCEL_BUILD` and zero
  `segment_cache_auto_refresh` / `build_segment_values_cache` activity in the whole log —
  neither the Excel worker nor the segment scheduler ran.
- The render **hung 40–89 s with no log line, then the container restarted.** Classic
  "app stopped answering → Azure health-check restart," not a crash with a traceback.

## Root cause
The screening **results** render contained an Excel widget wrapped in
`@st.fragment(run_every=2)`. `run_every=2` makes the **client fire a rerun every 2 seconds,
forever**, for as long as the results are on screen.

On STG the users are in **India → centralus (~233 ms RTT)** and the screening page is heavy
(keydev matcher + count + paginated fetch + AgGrid). When a single rerun takes longer than
the 2 s tick, the reruns **queue faster than they drain**. The session's single ScriptRunner
backs up, the WebSocket stops responding, Azure's health probe fails, and the **container is
restarted → 503**. The user retries, hits the same results page, and the loop repeats — a
**self-inflicted crash loop**, and it is **screening-only because that was the only page with
an unconditional `run_every` fragment.**

(Two other `run_every` fragments exist — `market_data.py` and `earnings_calls.py` — but the
market-data one **self-stops** (`run_every=None` once ratings are complete) and both are
short-lived/bounded, so they don't loop.)

## Fix (this change, `app/pages/screening.py`)
1. **Removed** the `@st.fragment(run_every=2)` `_kd_excel_download_area`, its background
   worker `_kd_excel_job`, the `_KD_XL_JOBS` job dict / lock, and `_KD_XL_BTN_CSS`.
2. Excel is now a **plain, synchronous, single-click branded button** of the currently-shown
   events, built with the fast streaming builder and served by the existing proven
   `_render_excel_js_download` (base64 blob anchor). No fragment, no timer, no thread.
   ```python
   with _dl_col:
       _xl_bytes = _build_keydevs_excel_fast(events_df, len(criteria),
                                             title="Coresight Key Developments")
       if _xl_bytes:
           _render_excel_js_download(_xl_bytes, f"KeyDev_Screening_{ts}.xlsx", label="Excel")
   ```
3. Left an explicit **"never re-add run_every here"** comment at both sites.

### Trade-off
The Excel now exports the **shown page(s)** of events, not the full 140k universe. That was a
deliberate stability-over-feature call while the app was down. A full-export can return later
via a **safe** path (a one-shot button that builds synchronously on click and offers a
download — **no `run_every`, no perpetual polling**).

## Verification (local UI, staging DB, Playwright)
- `/screening` loads clean, **scrolls fully**, no crash. (`/tmp/kd_screening_2.png`)
- Key Devs → "Earnings & Guidance [Last 7 Days]" → Show Results → **"80 key development
  events found"**, full grid with data + column filters, **branded red "Excel" button**
  top-right. `HAS_APPLYING_STUCK=False`. (`/tmp/kd_results.png`, `/tmp/kd_excel_area.png`)
- `grep` confirms **no `run_every` remains** in `screening.py`; file compiles.

## Rollout
Deploy `app/pages/screening.py`. Watch the boot count in the next log pull — the
screening-preceded restart bursts should disappear. If any crash-loop persists after this,
re-pull logs and check for a *different* screening operation (it will no longer be
`run_every`).

---

## ROUND 2 — the PRIMARY crash driver (log `server-logs-20260719_091930.log`)

The `run_every` fragment above is a real instability risk, but a second, **bigger** cause
sits in the same crash windows. Evidence:

- The internal **60-second heartbeat thread stops firing** before each restart — pid 1889's
  last beat is **01:35:17 PM**, then a 10-minute gap (01:53→02:03), then reboot at 02:08:41.
  The process was **freezing**, not busy-looping (almost no log lines).
- **Not memory** (RSS ~400–508 MB; the 3,678 MB peak was 18-Jul `company_filings`, unrelated),
  **not the pool** (`checkedout=1`).
- Immediately before the freeze, **8 background DB queries ran back-to-back at 01:05–01:16 PM,
  each 60–121 SECONDS**, all `user=-` (background thread), all scanning
  **`coreiq_filing_metrics_v5`** (14.4 M rows). Same shape on 18-Jul: **7 scans 08:50–09:00 PM**
  right after the 08:43 boot.

### Root cause
`build_segment_values_cache()` (`screening_service.py:1725`) rebuilds the screening segment
cache by scanning `coreiq_filing_metrics_v5` in **chunks of 40 tickers** — **349 / 40 ≈ 9
chunks** (matches the 8 observed scans). The chunk query
(`WHERE is_dimensioned=1 AND doc_type='10-K' AND ticker IN (40) AND (<dimension LIKE clause>)`)
has **NO `MAX_EXECUTION_TIME`** and takes **60–121 s each** → a **~9-minute barrage** of
1–2-minute scans on the web process. Each holds a DB connection and deserialises a large row
set (CPU + RAM, holding the GIL), so the Streamlit worker + heartbeat thread starve → the app
goes unresponsive → **Azure health-check restarts the container (503)**. A restart leaves the
cache empty, so the next screening load **re-triggers the rebuild → crash loop.**

**Trigger:** applying a Business/Geographic **Segments** criterion (or a screening load) while
the segment cache is empty calls `rebuild_segment_values_cache_async()` (`screening_service.py:2987`),
which runs the build on a background thread. The manual "rebuild" button on the **logs** page
(`logs.py:715`) does the same.

**Why it's fine now:** the cache is populated (`total_rows > 0`), so the lazy trigger no longer
fires → no heavy scans → stable for 36+ min after 02:13 PM.

### Fix options (to decide with the user — not yet implemented)
- **A. Statement timeout + smaller chunks (app-side, safe):** add `MAX_EXECUTION_TIME` to the
  chunk query and shrink `ticker_chunk` so no scan runs > a few seconds; on timeout, skip that
  chunk and log it. Stops the freeze even if the query is slow.
- **B. Index on `filing_metrics_v5` (data-team, the real speed fix):** a composite index on
  `(ticker, is_dimensioned, doc_type)` (± `dimension`) turns each 60–121 s scan into ms. We
  can't write DDL (data team owns the table) — hand them the SQL, same as the v5 ticker+source
  index already requested.
- **C. Don't hammer the web process:** yield between chunks (small sleep), cap concurrency to 1,
  and prefer running the full rebuild **off-hours** only (the auto-refresh scheduler already
  exists for this) — never a 9-minute inline scan during business hours.
- **D. Don't auto-trigger from a user request:** if the cache is empty, render N/A + a
  "segment data refreshing" note instead of kicking off the 9-minute build from the user's
  screening click.
- **E. Restart-resilient build:** persist chunk progress so a restart doesn't rescan from zero.

**Recommended:** B (real fix) + A (guardrail so it can never freeze the app again) + C/D
(never run the heavy build on a user request during business hours).
