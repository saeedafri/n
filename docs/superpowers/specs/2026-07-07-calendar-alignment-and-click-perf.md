# Calendar — Full-Width Alignment + Click Performance (N+1 Root Cause)

**Date:** 2026-07-07
**Files:** `app/pages/earnings_calendar.py`, `app/data/repository.py`
**Reported by:** Business — "the calendar box doesn't match the toolbar" + "when I click a cell it loads very very late".

## Problems & root causes (evidence-based)

1. **Width mismatch (33px).** The calendar rendered inside
   `st.columns([1, 0.001], gap="medium")` (a 2-column trick to avoid remounting
   FullCalendar). The `gap="medium"` + phantom 0.001 column shaved ~33px off the
   right, so the grid ended left of the toolbar. Measured: calendar right=1487 vs
   toolbar right=1520.

2. **Click "loads very very late".** Two layers:
   - Clicking swapped the layout `[1,0.001]→[3,1]`, shrinking the calendar and
     forcing FullCalendar to re-lay-out ~800 events (client-side ~1s). Server render
     was already ~50ms.
   - **The real root cause (timing logs):** `EC_DETAIL_DIALOG_RENDER` was **22-49
     SECONDS** for Q1/Q4 big-company events, ~0.3ms for others. `_render_detail_panel`
     calls `get_transcript_for_calendar_event`, which fetched up to 80 transcript
     rows then called `get_earnings_date()` **per row** — an **N+1** of up to 80
     Azure round-trips (`_nasdaq_report_date_for_fqe` per row). Fast when the match
     was an early/recent quarter (loop exits early); catastrophic for old/absent
     matches (full 80-row loop). WMT measured 27.8s, MO 36.0s.

## Fixes

> **UI unchanged.** An earlier iteration of this work refactored the detail panel
> into a `@st.dialog` modal + full-width grid to avoid the ~1s panel resize. The
> business owner had NOT asked for a design change (only "make it faster"), so that
> was **fully reverted** — the calendar keeps its production **right-side detail
> panel** (`st.columns([3,1] if panel else [1,0.001])` + `with detail_col:
> _render_detail_panel + ✕ Close`). The ONLY calendar-page change kept is a log line.

**`earnings_calendar.py` — logging only (no UI change)**
- Added `EC_DETAIL_PANEL_RENDER` timing in `_render_detail_panel` so the detail
  render time (where the transcript lookup runs) is tracked.
- The ~1s panel resize on click is inherent to the side-panel design and matches
  production; it is NOT the reported slowness (that was the N+1 below).

**`repository.py` — kill the N+1 in `get_transcript_for_calendar_event`**
- Replaced the per-row `get_earnings_date()` loop with **one** query for the
  ticker's `{fiscal_quarter_ending → MAX(earnings_date)}` map, then match transcript
  rows in memory (the FQE label is pure in-memory; FYE map + numbering convention are
  already `@st.cache_data`-cached). 80 queries → 1. Kept the original loop as a
  fallback only when the numbering convention is unavailable. `@st.cache_data(600s)`
  already memoizes repeat clicks.

## Verification (Playwright, real UI + real DB)
- **Correctness:** batched `get_transcript_for_calendar_event` == old per-row logic
  for AMZN/MSFT/WMT/TGT/AAPL/COST/NKE/MO (all match). WMT 27831ms→1250ms, MO
  35973ms→1203ms locally.
- **Width:** calendar 80..1520 == toolbar 80..1520 → **0px** mismatch.
- **Click→modal:** ~1106ms (old resize path) → **~310-380ms**; the previously
  22-49s Q1/Q4 detail renders now **0.3s-1.5s** local (`EC_DETAIL_DIALOG_RENDER`
  47963ms→3151ms cold, cached instant). On STG (co-located, warm) ≈100-300ms.
- **Stability:** open→close 5 different events (incl. previously-47s Constellation Q1)
  — all open body-ready and close cleanly, no remount.
- Cold app load = 12s was the known **local Azure cold-connect artifact** (calendar's
  own server render is 33ms; events cached at 0ms); warm ≈2.2s (streamlit_calendar
  iframe init). STG runs warm.

## Rollout
Sync `earnings_calendar.py` + `repository.py` to STG-deploy and redeploy.
