# Calendar "+N more" popover — clipped, unscrollable, and mis-positioned

**Date:** 2026-08-23
**Page:** `/calendar` (`app/pages/calendar.py`)
**Status:** Fixed, verified end-to-end in the local UI against the STG DB

---

## Symptoms

1. On a busy day (4 Aug 2026 `+21 more`, 5 Aug 2026 `+33 more`) the popover ran off
   the bottom of the calendar and simply **stopped** — no scrollbar, page scroll
   did not help, everything past roughly the 20th company was unreachable.
2. After the first fix, popovers on the **lower weeks** (25–27 Aug) opened at the
   *top of the month* instead of next to the day that was clicked.

Both come from the same place: the component iframe cannot scroll, and
FullCalendar assumes it can.

## Root cause

Three facts, in the shipped `streamlit_calendar` bundle
(`frontend/build/static/js/main.a153ecd0.js`, FullCalendar v6):

1. **No height cap.** `.fc-popover { position:absolute; z-index:9999 }` and
   `.fc-popover-body { min-width:220px; padding:10px }` — no `max-height`, no
   `overflow`. A 36-event day renders a ~1,230 px tall popover.

2. **`Popover.updateSize()` clamps left and right, but only *floors* the top —
   it never clamps the bottom:**

   ```js
   s = alignGridTop ? … : alignmentRect.top;      // desired top
   l = isRtl ? rect.right - width : rect.left;    // desired left
   s = Math.max(s, 10);                           // ← top floor only
   l = Math.min(l, documentElement.clientWidth - 10 - width);
   l = Math.max(l, 10);                           // ← left/right clamped
   applyStyle(rootEl, { top: s - origin.top, left: l - origin.left });
   ```

   That is deliberate: on an ordinary page you scroll the document down to read
   the rest.

3. **The iframe cannot scroll.** `streamlit_calendar` calls
   `Streamlit.setFrameHeight()` once, in a `useEffect(…, [])` at mount, so the
   iframe is pinned to the grid height (`options.height = _month_h = 33 + weeks ×
   150`). The popover is portalled into `.fc-view-harness` *inside* that iframe,
   so anything past its bottom edge is clipped by the iframe element and no
   gesture can reach it.

Measured before the fix (headless Chromium 1500×950, August 2026, iframe 931 px):

| popover | top | bottom | inside iframe? |
|---|---|---|---|
| `+33 more` (week 1) | 103 | **1336** | no |
| `+10 more` (last week, 27 Aug) | 678 | **1168** | no |

## Fix — two parts

### 1. Height cap + scrollable body (CSS, `_get_calendar_css()`)

```css
.fc-popover {
    display:flex !important; flex-direction:column !important;
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

`min-height:0` is required or the flex item refuses to shrink below its content
height. `overscroll-behavior:contain` stops the wheel from chaining into the
Streamlit page when the list bottoms out. `100vh` inside the iframe is the
iframe's own height, so the cap adapts to 4-, 5- and 6-week months.
`560px` is a readability ceiling, not a limit — the list scrolls past it.

### 2. Position clamp (JS, `_keep_popover_in_view()`)

CSS cannot fix the position: it cannot read the inline `top` FullCalendar writes,
so the only CSS-only option is pinning every popover to a fixed edge — which is
what made lower-week popovers jump to the top of the month. The clamp has to be
computed.

No JS can be injected into the component's own bundle, so the driver is installed
in the **parent** document (`components.html` + `window.parent.document`, the same
idiom already used in `navigation.py`, `loading.py`, `auth_manager.py` and five
other files) and reaches into the calendar iframe, which is same-origin — both are
served off the Streamlit host.

```js
var lowest  = viewportBottom - GAP - height - hostTop;  // keep the bottom edge in
var highest = GAP - hostTop;                            // keep the top edge in
var top = Math.max(highest, Math.min(current, lowest));
```

`Math.min(current, …)` is the important half: **FullCalendar's own position is
kept whenever the popover fits**, so a day in week 1 opens at week 1. The popover
slides up only by as much as it takes, and still overlaps its own day cell.

Three details that matter:

- **The observer is keyed on the iframe's `document`, not the iframe element.**
  An iframe polled before its `src` has loaded hands back the `about:blank`
  document; marking the element there left the real document unwatched, which
  made the clamp fire only sometimes. Every fresh document is a new object, so
  it gets its own observer. (This was a real flake — caught by running the sweep
  three times.)
- **A `style` mutation observer on the popover re-clamps** when FullCalendar
  repositions it (window resize). Our own write is recognised by the recorded
  value and ignored, so there is no feedback loop.
- **`window.frameElement` is collapsed to `height:0`** before the install guard —
  Streamlit otherwise reserves ~2 px per rerun for the script-only iframe.

If the injection were ever blocked, the CSS half still applies: the popover stays
capped and scrollable, and only the position falls back to FullCalendar's native
behaviour. Degradation, not breakage.

## Not changed

- `dayMaxEvents: 3` — the number of chips per day cell is unchanged.
- Yearly view returns early and renders a custom HTML grid (`_render_year_grid`);
  its `.ec-ym-more` is a static `+N` label, not a popover.
- List view has no popover.

## Verification (local UI, STG DB)

Server: `bash .claude/dev/run_local.sh` → **http://localhost:8501/calendar**
(LOCAL mode, OIDC bypass as `mohdsaeedafri@coresight.com`, STG DB).
Playwright drives the real page and reaches into the component iframe.

**Every `+N more` link in three consecutive months, opened one by one:**

| month | links | natural position kept | slid up to fit | failures |
|---|---|---|---|---|
| August 2026 (6 weeks) | 14 | 11 | 3 | **0** |
| July 2026 (5 weeks) | 8 | 1 | 7 | **0** |
| September 2026 | 7 | 7 | 0 | **0** |

A "failure" is any popover that is not fully inside the iframe, whose list end is
not reachable by scrolling, or that no longer overlaps the day cell it belongs to.
`PAGE_ERRORS none` throughout.

**The two cases from the report:**

| day | events | popover top → bottom | day cell | behaviour |
|---|---|---|---|---|
| 4 Aug (week 1) | 24 | 103 → 663 | top 103 | opens exactly at its row |
| 27 Aug (last week) | 13 | 431 → 921 | 678–860 | slides up 247 px, still over its own column |

Iframe viewport is 931 px, so 921 is the bottom edge minus the 10 px gap.

**Content completeness** (`+33 more`, 5 Aug): 36 events in the popover
(3 chips + 33 hidden), body `scrollHeight 1233 / clientHeight 528`, scrolled to the
end → last row `ZoomInfo Technologies Inc. / GTM` fully visible.

**Regressions checked:** clicking an event inside a popover still opens the detail
panel — `Autodesk, Inc. / ADSK` on 27 Aug (a slid-up popover) and `Aritzia / ATZ`
after two month navigations both rendered the right-hand card. ✕ closes the
popover. Monthly / Yearly / List all render with no error banner. The script-only
iframe measures 0 px.

**Repeatability:** the full August sweep was run three times back-to-back with
identical results after the document-keyed observer fix (before it: 2 of 3 runs
had the last week unclamped).

## Rollout

`app/pages/calendar.py` only — CSS block plus one helper and its call site. No DB,
no schema, no config, no dependency change, nothing to set on the App Service.
