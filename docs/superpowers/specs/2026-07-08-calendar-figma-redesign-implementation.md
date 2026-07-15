# Calendar Figma Redesign — Implementation

**Date:** 2026-07-08  
**Page:** `app/pages/earnings_calendar.py`  
**Design source:** Figma Make export `Refine calendar page design.make` (v17)

## Design intent (from Make file + business color tokens)

1. **Event cards** — light tinted fill (50% opacity), 2.5px left accent bar, company + ticker stacked, quarter/type badge on the right.
2. **Event types** — Earnings (blue), IPO (green), M&A (purple), Delisted (red) with Figma hex tokens.
3. **Quarter badges** — unified slate `#E2E8F0` / `#45556C`; IPO shows "IPO", M&A shows "M&A", Delisted has no badge.
4. **Today indicator** — Coresight red `#D62E2F` top border on active day cell.
5. **Filters** — Figma filter card: dark **Filters** toggle, removable type/quarter chips, expanded 3-column panel (Event Type / Fiscal Quarter / Company search).
6. **Calendar toolbar** — custom row: `‹ Month Year ›` · stats · Email Alerts · legend dots · Monthly/Yearly/List segmented control (FullCalendar built-in toolbar hidden).
7. **Views** — Monthly / Yearly / List (List uses FullCalendar `listMonth` with matching tag CSS).
8. **Layout** — removed legacy top row (Email Alerts + Company selectbox + Watchlist + Month/Year/List). Watchlist moved into filter panel Company column.

## Color tokens

| Type | Card bg | Text | Accent | Badge (IPO/M&A) |
|------|---------|------|--------|-----------------|
| Earnings | `rgba(240,249,255,0.5)` | `#024A70` | `#0084D1` | Q1–Q4 slate |
| IPO | `rgba(236,253,245,0.5)` | `#004F3B` | `#009966` | `#D0FAE5` / `#004F3B` |
| M&A | `rgba(245,243,255,0.5)` | `#4D179A` | `#7F22FE` | `#EDE9FE` / `#4D179A` |
| Delisted | `rgba(254,242,242,0.5)` | `#82181A` | `#D62E2F` | none |

## Implementation approach

- **`_TYPE_STYLE` + `_fc_event_card()`** — single builder for all FullCalendar events; sets `classNames` for CSS-driven card layout (streamlit_calendar cannot pass JS `eventContent`).
- **CSS `::after` badges** — quarter/type label on `.fc-event-title` and `.fc-list-event-title` via `ec-badge-*` classes.
- **Filter state** — `ec_active_types` + `ec_active_quarters` (replaces combined `ec_active_kinds`); migrates legacy session on first load.
- **Layout** — `st.container(border=True, key="ec_filter_card")` + `ec_cal_card`; `_render_filter_panel()` + `_render_calendar_toolbar()`; `headerToolbar: false`, `firstDay: 1`.
- **Company filter** — `ec_company_search` text filter (replaces Company selectbox); watchlist remains in expanded Company column.
- **Performance** — no new DB calls; same prefetch + in-memory filter; earnings feed still windowed to visible month.

## Verification

- Playwright: `bash .claude/dev/run_local.sh` → wait for load → screenshot `/tmp/ec-figma-final.png`
- Visual: filter card w/ expanded 3-col panel + chips; toolbar stats/legend/segmented views; MON-first grid; light event cards w/ accent + badge; red today border.
- Automated checks: `rgba(240,249,255,0.5)` event bg, `#0084D1` accent, `MON` first header, no `All Companies` dropdown.
- Functional: filter toggle, list/year/month views, event click → detail panel.
