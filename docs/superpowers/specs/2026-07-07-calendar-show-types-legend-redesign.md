# Calendar "Show Types" Legend — Business-Requested Redesign

**Date:** 2026-07-07
**Page:** `app/pages/earnings_calendar.py` — `_render_event_type_legend()` + legend CSS
**Requested by:** Business team

## Requests (verbatim intent)
1. Make the type chips the **same size**.
2. **Right-align** the chip group to the page.
3. Move the **"Show types"** label to vertical **center** (was sitting at the bottom).
4. Add on-page **caption** clarifying what clicking does — clicking a type **unselects** it; **everything is selected by default**.
5. Rename **"M&A Completion" → "M&A"** (keep **"Delisted"**).
6. Center the **text inside** each chip; keep all icons the **same size**.

## Implementation
- **Labels** (`_LEGEND_ITEMS`): `ma` label `M&A Completion` → `M&A`. (Only the display label; the internal kind key `ma` is unchanged, so all filtering/feeds are untouched.)
- **Equal-width, right-aligned chips**: column ratios changed from the old uneven
  `[0.9,0.5,0.5,0.5,0.5,1.6,0.6,1.0,1.4]` (M&A wide, trailing empty column pushed chips left) to
  `[1.6, 1.9] + [1.0]*7` — a label column, a **spacer** column that pushes the chip
  group flush-right, then **7 equal 1.0 columns**. Each `st.button(width="stretch")` fills
  its equal column → identical chip size (verified 123px each, 0px spread).
- **Vertically-centered label + caption**: label wrapped in `.ec-legend-labelwrap`
  (flex column, `justify-content:center`) with `vertical_alignment="center"` on the row.
  Added `.ec-legend-caption` = "Click to unselect · all selected by default".
- **Centered chip text**: chip button CSS set to `display:flex; align-items:center;
  justify-content:center; text-align:center`.
- **Per-chip clarification** (bonus): each chip gets a `help=` hover tooltip
  (`_LEGEND_HELP`) naming what it filters (e.g. "Delisted (went-private) dates").

## Verification (Playwright, real UI, local STG DB)
- All 7 chips exactly **123px** wide (spread 0px) → equal size. ✅
- M&A label renders `M&A`; Delisted renders `Delisted`. ✅
- Chip group flush-right (right edge aligns with the "Year Wise" toolbar above). ✅
- Caption "Click to unselect · all selected by default" present under "SHOW TYPES";
  label text sits at chip vertical-center. ✅
- **Functional**: clicking `Delisted` flips it primary→secondary (unselected, outlined),
  and the summary line drops "6 delisted" → the filter actually applies. ✅
- Hover tooltip shows the per-chip description. ✅

## Rollout
Change is in `app/pages/earnings_calendar.py` only. Sync to the STG-deploy repo and
redeploy STG. No DB/schema/behavioral change to the calendar feeds.
