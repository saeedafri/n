# Calendar "+N more" popover — clipped and unscrollable

**Date:** 2026-08-23
**Page:** `/calendar` (`app/pages/calendar.py`)
**Status:** Fixed, verified in the local UI against the STG DB

---

## Symptom

On a busy day (e.g. 4 Aug 2026, `+21 more`; 5 Aug 2026, `+33 more`) clicking the
`+N more` chip opens the FullCalendar day popover, but the list runs off the
bottom of the calendar and simply **stops**. There is no scrollbar, the page
scroll does not reveal the rest, and the tail of the list (everything after
roughly the 20th company) is unreachable.

## Root cause

Two FullCalendar v6 defaults combine badly with a Streamlit component iframe.

1. **No height cap.** The bundled rule is
   `.fc-popover { position:absolute; z-index:9999 }` and
   `.fc-popover-body { min-width:220px; padding:10px }` — no `max-height`, no
   `overflow`. A 36-event day renders a ~1,230 px tall popover.

2. **FullCalendar only clamps the popover's TOP, never its bottom.**
   From `Popover.updateSize()` in the shipped bundle
   (`streamlit_calendar/frontend/build/static/js/main.a153ecd0.js`):

   ```js
   s = alignGridTop ? … : alignmentRect.top;      // desired top
   l = isRtl ? rect.right - width : rect.left;    // desired left
   s = Math.max(s, 10);                           // ← top clamp only
   l = Math.min(l, documentElement.clientWidth - 10 - width);
   l = Math.max(l, 10);                           // ← left/right clamped
   applyStyle(rootEl, { top: s - origin.top, left: l - origin.left });
   ```

   Horizontal overflow is constrained; vertical overflow is not, because on a
   normal page you just scroll the document down to see the rest.

3. **The component iframe cannot scroll.** `streamlit_calendar` calls
   `Streamlit.setFrameHeight()` once, in a `useEffect(…, [])` at mount, so the
   iframe is pinned to the height of the grid (`options.height = _month_h =
   33 + weeks × 150`). The popover is portalled into `.fc-view-harness` *inside*
   that iframe, so anything past the iframe's bottom edge is clipped by the
   iframe element and is unreachable by any scroll gesture.

Measured before the fix (headless Chromium, 1500×950, August 2026):

| Popover | top | bottom | iframe height | inside iframe? |
|---|---|---|---|---|
| `+33 more` (row 1) | 103 | 663 → **1336 uncapped** | 931 | **no** |
| `+10 more` (last row) | 678 | 1168 | 931 | **no** |

## Fix

CSS only, in `_get_calendar_css()` (`app/pages/calendar.py`) — no JS can be
injected into the component iframe, `custom_css` is the only lever.

```css
.fc-popover {
    display:flex !important; flex-direction:column !important;
    top:10px !important;                              /* beats FC's inline top */
    max-height:min(560px, calc(100vh - 80px)) !important;
    border-radius:8px !important; border-color:#E2E8F0 !important;
    box-shadow:0 8px 24px rgba(15,23,42,0.16) !important;
}
.fc-popover-header { flex:0 0 auto !important; }      /* date + ✕ stay pinned */
.fc-popover-body {
    flex:1 1 auto !important; min-height:0 !important;
    overflow-y:auto !important; overscroll-behavior:contain !important;
}
```

Three things are doing work here:

- **`max-height` + flex column + `overflow-y:auto` on the body** — the header
  (date + close) stays fixed, the event list scrolls. `min-height:0` is required
  or the flex item refuses to shrink below its content height.
- **`top:10px !important`** — an author `!important` declaration outranks the
  non-important inline `style="top:…"` FullCalendar writes, so the popover is
  pinned to the top of the grid regardless of which week was clicked. This is
  what fixes the bottom-row case, which the height cap alone does not.
- **`overscroll-behavior:contain`** — reaching the end of the list does not
  chain the wheel event into the outer Streamlit page.

Geometry is now provably inside the iframe for any month:
`bottom = harnessTop(≈33) + 10 + min(560, 100vh − 80) ≤ 100vh − 37`.
`100vh` inside the iframe is the iframe's own height, so this holds for 4-, 5-
and 6-week months alike.

`560px` is a readability cap, not a constraint — the list scrolls past it.

## Not changed

- `dayMaxEvents: 3` — the number of chips shown in a day cell is unchanged.
- The Yearly view returns early and renders a custom HTML grid
  (`_render_year_grid`); its `.ec-ym-more` is a static `+N` label, not a
  popover. Out of scope.
- List view has no popover.

## Verification (local UI, STG DB)

`bash .claude/dev/run_local.sh` → Playwright drives the real page, reaching into
the component iframe.

**Placement** — every case now fully inside the iframe:

| Case | link | top | bottom | iframe H | inside |
|---|---|---|---|---|---|
| topmost row | `+5 more` | 10 | 332 | 931 | ✅ |
| bottom-most row | `+10 more` | 10 | 500 | 931 | ✅ |
| largest day | `+33 more` | 10 | 570 | 931 | ✅ |

**Completeness and scroll** — `+33 more` on 5 Aug 2026:

```
EVENTS_IN_POPOVER  36            (3 chips shown + 33 hidden)
FIRST_3            a.k.a. Brands Holding Corp/AKA, Amplitude Inc./AMPL, AppLovin Corporation/APP
LAST_3             Weis Markets, Inc./WMK, Western Digital Corporation/WDC, ZoomInfo Technologies Inc./GTM
SCROLL_STATE       {"atBottom": true, "lastVisible": true,
                    "lastText": "ZoomInfo Technologies Inc. / GTM", "count": 36}
bodyScrollHeight   1233   bodyClientHeight 528   overflowY "auto"
```

**In the browser viewport**, with the page scrolled so the grid is in view:
`popTopInWindow 70, popBottomInWindow 630, windowH 950, fullyVisible true`.

**Regression** — clicking an event *inside* the popover still fires `eventClick`
and opens the right-hand detail panel:

```
CLICK_EVENT       a.k.a. Brands Holding Corp / AKA
DETAIL_PANEL_OPEN True
→ "a.k.a. Brands Holding Corp · AKA · Q2 2026 · Earnings Date 2026-08-05 · After Hours …"
```

Close (`✕`) still dismisses the popover.

## Rollout

Single-file CSS change in `app/pages/calendar.py`. No DB, no schema, no config,
no dependency change. Nothing to set on the App Service.
