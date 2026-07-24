# Screening Key Devs — Excel-only-500 + removeChild error fixes

**Date:** 2026-07-24 · **Page:** `app/pages/screening.py` (Key Devs by Category) · **No DB writes.**

## Bug 1 — Excel export only had 500 rows (UI showed 7,885)
Root cause: the results grid is capped at `_KD_PAGE = 500` (keyset pagination), and the Excel
button built the workbook from that **same 500-row `events_df`** (`_build_keydevs_excel_fast(events_df, …)`).
So a 7,885-event result exported only 500.

Fix: the Excel is now built from the **full** matching set. Replaced the eager 500-row build with
a **lazy `@st.fragment` button** that, on click, runs one bounded query
`get_keydevs_events_for_tickers(..., limit=50000)` and streams it through the existing
`_build_keydevs_excel_fast` (already written for 100k+ rows), auto-downloading via
`_render_excel_js_download(..., auto_download=True)`.
- Built **only on click** (not every rerun) → no perf hit, no huge workbook per render.
- Plain `@st.fragment` (NO `run_every`) → safe; the 19-Jul crash-loop was `run_every=2`, not fragments.
- Capped at 50,000 rows to bound RAM/time on very broad "All History" queries; a caption notes if capped.

Verified (staging DB): `limit=500 → 500` rows, `limit=5000 → 5000` rows — the export now returns the
full set instead of 500.

## Bug 2 — `NotFoundError: Failed to execute 'removeChild' on 'Node'`
Streamlit's React reconciler threw this on the screening results page. Root cause: the sticky
loader (`render_sticky_loader("Loading Key Developments")`) is rendered via `st.markdown`, so its
overlay lives **inside Streamlit's React tree**. Its remover JS then called
`ov.parentNode.removeChild(ov)` — deleting a node React still tracks. On the next rerun React's own
removeChild throws because the child is already gone. (The boot/nav overlays don't hit this — they
`appendChild` to `document.body`, outside React's root.)

Fix (`app/components/loading.py` `_STICKY_REMOVER_JS`): **hide instead of remove** — set
`opacity/visibility:hidden` + `pointer-events:none` (already click-through) and leave the node in
place so React stays the sole owner and unmounts it normally. Visually identical.

Verified: screening page renders with no removeChild error, no Python error, no server errors.
(The reconciler race isn't deterministically reproducible, but the fix removes its mechanism —
external DOM removal of a React-managed node.)
