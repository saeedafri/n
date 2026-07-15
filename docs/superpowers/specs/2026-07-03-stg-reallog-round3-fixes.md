# STG Real-Log Round-3 Fixes — Design & Evidence

**Date:** 2026-07-03
**Input:** real STG log `server-logs-20260703_124208.log` (6,455 lines) + 3 screenshots
(#8 garbled "For Reference" + slow chart, #9 the good "Loading earnings calendar…"
spinner, #10 screening keyDev grid with no spinner). Every root cause below was proven
against the real log or the live local UI (Playwright), not assumed.

---

## 1. Standard loading spinner on every page (was: inconsistent / hand-rolled)

- **Ask:** "That spinner [earnings calendar, #9] is good… please use it on every page,
  named per page (Loading Calendar, etc.)."
- **Before:** two hand-rolled inline spinners (earnings_calls, earnings_calendar) +
  a separate market_data overlay + `components/loading.py` helpers nobody standardized on.
- **Fix:** one canonical `render_page_loader(label, overlay=False)` in
  `app/components/loading.py` — 28px Coresight-red spinner + "{label}…", identical
  markup everywhere. `overlay=True` renders a fixed, centered card that occupies **zero
  layout space** (keeps the round-2 "no push-down on tab switch" fix); `overlay=False`
  is the inline top-of-page loader for fresh page loads.
- **Applied:** market_data (`Loading Market Data`, overlay), earnings_calendar
  (`Loading Calendar`), earnings_calls (`Loading Earnings Calls`), screening keyDev
  (`Loading key developments`, overlay). Verified in UI — see `md_cp.png` (branded
  overlay card renders exactly like #9).
- **Not touched:** home (fast landing — a loader would only flash), newsroom /
  company_filings / forecasting (already have their own in-flow loaders). They can adopt
  the one-line helper later with no risk.

## 2. Garbled "keyboard_arrow_right For Reference" (#8) — ROOT CAUSE FOUND

- **Symptom:** the `For Reference` expander shows raw ligature text next to the label.
- **Investigation (Playwright DOM dump on the live expander):**
  - Locally the expander renders a **clean `>` chevron** (`verify_reference_expander.png`)
    — so the bug is **not** in our markup.
  - The toggle icon is a Streamlit **Material Symbols font ligature**
    (`textContent = "keyboard_arrow_right"`, `font-family: 'Material Symbols Rounded'`).
  - `document.fonts.check("Material Symbols Rounded")` = **False**, and one icon span had
    fallen back to `font-family: Inter` → the ligature renders as literal text.
- **Root cause:** Streamlit serves that icon font from its bundled `/static/…` path.
  Behind the STG reverse proxy that path isn't forwarded, so the font never loads and the
  ligature text leaks. (Locally the static path works → no garble locally.)
- **Fix (two layers, `app/components/styles.py`):**
  1. Global `@import` of **Material Symbols Rounded from the Google CDN** (`display=block`
     so the fallback text is hidden until the glyph is ready). The CDN is already proven
     reachable from STG — the Excel button's Material Symbols Outlined loads fine.
  2. A targeted CSS rule forcing the expander toggle icon to
     `font-family: 'Material Symbols Rounded'` so a stray Inter override can't leak text.
- **Verify on STG:** open a company → Company Profile → the `For Reference` header shows a
  chevron, not "keyboard_arrow_right". (Can't repro locally — it's proxy-specific.)

## 3. Screening keyDev — no spinner on Add criteria / re-select (#10)

- **Root cause:** the financial results path already has the
  `_render_coresight_loading_overlay` flow, but the **keyDev path** just ran
  `get_keydevs_events_for_tickers()` synchronously with **no feedback**. Both that query
  and the Excel build are `@st.cache_data(show_spinner=False)`, so an actual recompute
  (Add criteria, changed company selection → cache miss) froze the page silently.
- **Fix:** wrapped the keyDev event fetch in the branded `render_page_loader(overlay=True)`
  (`app/pages/screening.py`). Cache hits clear it instantly; real recomputes now show the
  spinner.
- **"Selecting a company reruns the whole results":** Streamlit reruns the entire script on
  *any* widget change — that is fundamental and unavoidable. What we control: the
  `@st.cache_data` layer already prevents a DB re-hit when inputs are unchanged, and the
  new spinner makes a real recompute read as "loading" instead of "broken." Eliminating
  the grid *re-render* entirely would require moving the results into an `@st.fragment`
  (larger, riskier change) — flagged as a follow-up, not bundled here.

## 4. MDP "jerking / graph too slow" — root cause = cold Azure first-touch

- **Log evidence (worst loads):**
  - `TAB_Company_Profile 17116ms` (AMZN) — but sub-timings sum to ~1.6s
    (`cp_stock_quote=1548ms`, everything else ≈0). The **15s is unmeasured** = the
    synchronous Excel build (60-month price history + a JOIN on the 12.5M-row
    `coreiq_av_time_series_daily` + workbook construction), all **uncached on first touch**.
  - `TAB_Segments 13648ms` — `tab_setup=11245ms` (the common pre-tab setup, first-touch DB).
  - `TAB_Ratings 13753ms` — `ratings_render=10413ms` (cold ratings component).
  - Warm loads of the *same* tabs in the same log: `cp_total=1796–2874ms`,
    `tab_setup=1–17ms`. → The pain is **cold Azure buffer first-touch**, not per-render cost.
- **What this round does about it:**
  - The standardized overlay (#1) now covers the whole cold wait smoothly (no blank
    screen, no layout jump) — directly answers "jerking / spinner on top / screen goes down."
  - Real latency mitigation stays the background warmup (round 2 pre-warms AMZN
    segments + M ratings). Per-ticker cold latency for *other* tickers is inherent to Azure
    and only fully solved by warming the hot tables into the buffer pool — noted, not silently
    dropped.
- **Frame capture:** a Playwright tab-switch frame harness was run
  (`verify_mdp.py` → `frame_bs_*.png`); the transition now shows the fixed overlay, not a
  content jump. (Full per-tab video wasn't produced; the flicker *cause* — in-flow loader
  pushing content — was already removed in round 2 and is now the shared overlay.)

## 5. Files changed
- `app/components/loading.py` — new `render_page_loader()`.
- `app/components/styles.py` — Material Symbols Rounded `@import` + expander-icon font rule.
- `app/pages/market_data.py` — overlay loader via helper.
- `app/pages/earnings_calendar.py`, `app/pages/earnings_calls.py` — inline loader via helper.
- `app/pages/screening.py` — branded loader around keyDev event fetch.

## 5b. Deep log audit (server-logs-20260703_124208.log) — real bugs found & fixed

The user asked to check "everything — RAM and other things." The 707 ERROR-level
lines are mostly **misclassified diagnostics** (`[DATE_RESOLVE]`, `[RENDER_START]`,
`[DATE_SYNC]`, `[FILTER_CB]` logged via `log_error()` for visibility). Filtering those
out surfaced **3 genuine bugs**:

### 5b.1 M&A calendar crash — all 263 events dropped (round-2 regression, FIXED)
- **Log:** `AttributeError: 'float' object has no attribute 'title'` at
  `earnings_calendar.py:1370` × every render.
- **Cause:** round-2 made `get_ma_completion_events` return `df.to_dict("records")`,
  which turns NULL cells into `float('nan')`; `(nan or "M&A").title()` crashed, and the
  `except` returned `[]` → the entire M&A overlay vanished.
- **Fix:** normalise NaN/NaT → None at the root (repository, after `to_dict`) + a
  per-event guard in `_ma_to_fullcalendar` so one bad row can't drop the feed + NaN
  guard on `ma_value_usd_m` (JSON can't encode NaN).
- **Verified (STG):** `get_ma_completion_events → 263 rows, 0 NaN`,
  `_ma_to_fullcalendar → 263 events` ("TopBuild Corp. · Merger").

### 5b.2 Newsroom `revenue` STILL returned 0 on wide ranges (FIXED — the real cause)
- **Log:** `DB_SELECT_AV_TITLE_LIKE 6028ms rows=0` + `NEWSROOM_KEYWORD_SEARCH kw='revenue'
  … av=0 yf=0`. Round-2's plain-query fix did **not** hold on STG.
- **Root cause (EXPLAIN on STG):** the plan is fine (`type=range`, uses the time index)
  — `SELECT id` returns 1000 in **528ms**. The single query timed out only because it
  also materialised the **big `summary` + `ticker_sentiment_json` + `topics_json`
  columns** for ~2k rows (`hydrate 2000 = 7098ms`), blowing the 6s cap → **0 rows**. FT
  is useless here (0 rows in 8.5s even at 366d). Also: **93% of all 1.44M rows are in the
  last 366 days**, so narrowing the range doesn't help — the payload volume is the floor.
- **Fix (`repository.py`, both AV + YF):** **deferred join** — step 1 fetches only ids
  (fast, id-scan capped 10s so a cold first-query still completes, not empties); step 2
  hydrates the matched ids by PK (capped 20s so it *completes* rather than truncating to
  empty). YF `revenue` is genuinely **sparse** (×4 overflow=8000 timed out; even
  `GROUP BY news_id` timed out) → dropped YF overflow to ×1 (grouping already happens in
  Python).
- **Verified (STG, cold, full 14-yr range):** AV `revenue` → **1000 rows (~8–11s)**,
  YF `revenue` → **898 rows (~3s)** — both were 0. Newsroom `revenue` wide is populated.
- **Note:** AV wide keyword search is ~8s (hydrating 1–2k full articles is the honest
  floor); the loading spinner covers it. Empty is fixed; speed is bounded, not instant.

### 5b.3 RAM — no leak, but a high ceiling (MONITOR)
- rss over the 23-min session: min 84MB → **peak 1712MB** (avail floor 1301MB) → settles
  ~977MB. It **rises and releases** (GC lines "released 17MB"), so it's not a monotonic
  leak, but 1.7GB peak is high — driven by in-memory caches (segment cache, materialised
  DataFrames, keyword result sets). Worth watching if the container is <3GB; not a bug.

### 5b.4 Other (flagged, not changed — out of scope)
- **Log hygiene:** ~700 ERROR-level lines are debug traces logged via `log_error()`
  (`[DATE_RESOLVE]`, `[RENDER_START]`, …). They bury the real 3 errors. Recommend
  downgrading to `log_info`/debug-gated. (Left as-is — flag first, could be intentional.)
- **`fetch_non_sec_companies` TimeoutError(30s):** one **cold** occurrence before the
  round-2 materialised cache was warm; warm path is ~0.4s. Cold-only.
- **`Using default SECRET_KEY in staging`** config warning — set `SECRET_KEY` on STG.

## 6. Verify on STG after push
```
# spinner: every page shows the same branded loader with a per-page label
# #8 icon: Company Profile → "For Reference" shows a chevron, not keyboard_arrow_right
# #10: screening keyDev → Add criteria now shows a spinner during recompute
grep TAB_Company_Profile server-log.log   # cold once, then ~1.8s warm
grep tab_setup=                            # warm ≈ 1–17ms
```
