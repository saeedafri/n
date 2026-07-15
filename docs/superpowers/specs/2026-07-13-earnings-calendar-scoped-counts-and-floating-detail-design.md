# Earnings Calendar — Period-Scoped Counts + Floating Detail Overlay

**Date:** 2026-07-13
**Page:** `app/pages/earnings_calendar.py`
**Author:** engineering (business-driven fixes)

## Problem (reported by business team, with screenshots)

1. **Toolbar counts are all-time, not the displayed month.** Under the "July 2026"
   header the toolbar read *16,707 events · 338 companies · 264 M&A* — the full
   deduped dataset across every date, mislabeled as if it were July 2026.
2. **Footer count is all-time, mislabeled "in 2026", plus useless Q1–Q4 badges.**
   The footer read *"Showing 16,707 of 16,708 events in 2026"* (again the all-time
   total) and rendered four static, non-interactive `Q1 Q2 Q3 Q4` chips on the right
   that did nothing.
3. **Opening an event split the toolbar onto two lines.** The month/nav/stats/legend/
   view-switcher toolbar wrapped when a detail card was open.
4. **Opening an event changed the calendar view.** The grid shrank/reflowed when the
   card opened; business wants the calendar to look identical whether or not a card
   is open.

## Root cause

All four are in the page's render path:

- **#1 / #2** — `_n_total`, `_n_companies`, `_n_ma` were computed from the *full
  filtered, pre-window* feeds (`events + ma_events + ipo_events + delisted_events`).
  This was a deliberate "true badge count that matches production" choice, but it
  counts every date, not the visible period. The footer printed the same all-time
  numbers with a hard-coded "in {year}" label and a decorative `Q1–Q4` span block.
- **#3 / #4** — On event open the layout switched to `st.columns([3, 1])`, shrinking
  the calendar column to 3/4 width. That reflow (a) resized the FullCalendar iframe
  and (b) left the single-row toolbar without room, so it wrapped to two lines.

## Fix

### Period-scoped counts (`render_page`, stats block)
Scope the already type/company/watchlist-filtered feeds to the **displayed period**
before counting — the strict calendar month for Monthly/List, the calendar year for
Yearly — using the existing `_window_events()` helper:

```python
_view_now = st.session_state.get("ec_view", "calendar")
_cnt_anchor = _parse_iso_date(st.session_state.get("_ec_current_date")) or date.today()
if _view_now == "year":
    _cnt_start, _cnt_end = _year_view_date_range(_cnt_anchor)
else:                       # strict month, NOT the 42-day grid
    _cnt_start = _cnt_anchor.replace(day=1)
    _cnt_end   = _cnt_anchor.replace(day=calendar.monthrange(y, m)[1])
_all_shown = (window each feed to [_cnt_start, _cnt_end])
_n_total / _n_companies / _n_ma = counts of the scoped set
_period_label = "July 2026" | "2026"
```

Counts are computed *before* the render-window narrows feeds to the FullCalendar grid,
so they honor the active filters yet reflect only the period on screen.

### Footer (`render_page` month/list; `_render_year_grid` year)
- Drop the all-time denominator; print only the scoped count.
- Drop the non-interactive `Q1–Q4` badges.
- Label with the real period.

```
Monthly / List :  Showing <b>115</b> events in July 2026
Yearly         :  Showing <b>584</b> events in 2026
```

### Detail panel — dedicated right column, full-width toolbar (issues #3, #4)

**Final design (supersedes an intermediate `position:fixed` overlay attempt).** The
business requirement, confirmed against the reference design, is: the detail card opens
**beside** the calendar in its own right-hand column — never floating on top of the
grid — while the toolbar (month nav, stats, legend, **Monthly/Yearly/List switcher**)
stays a single, intact row.

Layout inside `st.container(border=True, key="ec_cal_card")`:

```python
_render_calendar_toolbar(...)                 # FULL WIDTH → always one row
if _has_panel:
    _cal_body, _detail_body = st.columns([3, 1], gap="medium")
else:
    _cal_body, _detail_body = st.container(), None
with _cal_body:
    cal_result = st_calendar(...); footer
if _has_panel:
    with _detail_body:
        with st.container(key="ec_detail_col"):
            _render_detail_panel(...); "✕ Close" button
```

- **Toolbar is full-width, above the split** → it never wraps to two lines and the view
  switcher is never covered (fixes #3 and the "Monthly/Yearly/List going somewhere"
  report). At full width the toolbar row measures ~40px (single line).
- **Only the body splits 3:1** when a card is open → calendar shrinks to 3/4, the card
  occupies the right 1/4 with a real gap between them (measured: calendar right edge
  1144px, card left edge 1176px @1600 viewport → **zero overlap**). Closed → calendar is
  full width again.
- The intermediate overlay (`position:fixed`) floated the card ON TOP of the grid,
  covering the middle days and the switcher — the exact defect this replaces.

**Why `ec_cal_version` bumps on CLOSE (not open):** `st_calendar` keeps its last
`eventClick` in `cal_result` across a plain rerun. On close `ec_selected_event` becomes
`None`, so `current_id` becomes `""` and the stale `clicked_id != current_id` handler
would immediately RE-select the event and re-open the panel. Bumping `ec_cal_version`
remounts the calendar, flushing that component state so Close actually closes. Open does
NOT bump (no re-trigger, since `clicked_id == current_id`).

## Data flow (unchanged elsewhere)
Prefetch (full deduped set, disk-materialized) → watchlist/company/type filters →
**scoped count for toolbar+footer** → render-window to the FullCalendar grid → render.
Only the counting step changed; fetching, filtering, and rendering are untouched.

## Testing (real UI, `bash .claude/dev/run_local.sh` + Playwright)

| View | Toolbar | Footer | Verified |
|------|---------|--------|----------|
| Monthly | 115 events · 113 companies · 3 M&A | Showing 115 events in July 2026 | ✓ |
| Yearly  | 584 events · 325 companies · 13 M&A | Showing 584 events in 2026 | ✓ |
| List    | 115 events · 113 companies · 3 M&A | Showing 115 events in July 2026 | ✓ |

**Full automated QA sweep: 35/35 assertions PASS** (`scratchpad/ec_full_qa.py`):
- Month baseline, month nav (July→Aug 157 events→back), earnings-open, close,
  M&A-open, close, filter-toggle, year, list, list-open — all green.

Detail panel layout (measured with Playwright @1600 and @2000 viewports):
- Card renders in its own right column; **card left edge ≥ calendar right edge → zero
  overlap** (1600: cal_right=1144, card_x=1176; 2000: cal_right=1444, card_x=1476). ✓
- Calendar **shrinks on open** (1440→1064 @1600, 1840→1364 @2000) and **restores to
  full width on close** (→1440). ✓
- Toolbar row height **40px = single line** whether open or closed; view switcher stays
  visible and uncovered. ✓
- **Close clears the panel in ~1.06s** and reopen works. ✓
- Earnings / M&A panels each render their type-specific fields in the column. ✓
- No `Q1–Q4` badges anywhere; counts scoped in all three views. ✓

## Rollout / risk
- Pure UI-logic change in one page; no DB/schema/API change.
- `.ec-yf-q` / `.ec-yf-qs` CSS rules are now dead (harmless; left in place).
- Company-dropdown "N matching events" hint still uses the all-time `_n_pre_filter`
  (intentional — it answers "how many events match this company overall").
