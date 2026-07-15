# Earnings Calendar — Figma "Refine calendar page design" pixel-perfect restyle

**Date:** 2026-07-09
**Scope:** `app/pages/earnings_calendar.py` only (calendar body + filter panel + toolbar). Header/nav untouched, per request. No changes to data feeds, date derivation, or interactivity logic.

## Source of truth

Business team shared a Figma Make prototype: `figma.com/make/AnP0AkohNCK6G9V02M5t3A` (Refine calendar page design). The design-context MCP was **blocked** (the `dataautomation@coresight.com` Figma seat is View-only; that API needs Edit). Instead the exact spec was extracted from the **live preview iframe** the Make URL renders (`*-figmaiframepreview.figma.site`) — the prototype is a **Tailwind** React app, so DOM class names gave exact hex/spacing. Screenshots + `getComputedStyle` confirmed each value.

## Color mapping (the key decision)

The swatch "Selection colors" the team supplied map onto Tailwind palettes. Category→color, confirmed from the live prototype's chip DOM:

| Category | Tailwind family | Left bar (2.5px) | Company text | Ticker text | Badge |
|----------|-----------------|------------------|--------------|-------------|-------|
| **Earnings** | sky | `#38BDF8` (sky-400) | `#0C4A6E` (sky-900) | `#0284C7` (sky-600) | quarter Q1–Q4 `bg #E2E8F0 / #475569` (slate-200/600) |
| **IPO** | emerald (**green**) | `#6EE7B7` (emerald-300) | `#064E3B` (emerald-900) | `#059669` (emerald-600) | `IPO` `bg #D1FAE5 / #064E3B` |
| **M&A** | violet (**purple**) | `#C4B5FD` (violet-300) | `#4C1D95` (violet-900) | `#7C3AED` (violet-600) | `M&A` `bg #EDE9FE / #4C1D95` |
| **Delisted** | red | `#D62E2F` (Coresight red) | `#7F1D1D` (red-900) | `#DC2626` (red-600) | *(none)* |

> **IMPORTANT — reversal from the old app.** The prior calendar used IPO=plum `#654F6F`, M&A=green `#61A575`. The new design **swaps the semantics**: **IPO = green, M&A = purple.** This matches the team's own text labels and the live prototype. Do not "correct" it back to convention.

Chip fills are the `/50` tints: `bg-{family}-50/50` (e.g. earnings `rgba(240,249,255,0.5)`). Left bars use the **pastel** 300/400 shade (not the saturated 500/600) except Delisted, whose bar is the strong red.

## What changed (all in `earnings_calendar.py`)

**`_get_calendar_css()` (FullCalendar iframe CSS):**
- Event chip left bars → pastel per table above (month + list views).
- Ticker line now category-600; company (`::first-line`) category-900 (was a single flat color).
- Quarter/IPO/M&A badge vertically centered (`top:50%; translateY(-50%)`), 9px, `2px 6px`, radius 4px; slate-200/600 for quarters, emerald/violet for IPO/M&A.
- Wider badges (M&A/IPO) get `padding-right:46px` on the title so long company names never underlap.
- Weekday header row `bg #F8FAFC`, `tracking-widest`.

**`_get_css()` (Streamlit chrome CSS):**
- Card wrappers `border-radius:16px` (rounded-2xl).
- Top removable chips: pastel `::before` dot + tinted bg (`{family}-50`) + colored text, no border; Q chips slate-200.
- Event-type pills: left-aligned (dot + label hug left — required overriding Streamlit's nested `button > div > span` flex-centering), pastel `::before` dot, category-900 text, `{family}-50/50` bg.
- Fiscal-quarter grid buttons: `ring` via inset box-shadow, slate-700 bold when active.
- Legend dots: pastel, `rounded-sm` 8px, 11px labels.
- Company search input: lucide magnifier (inline SVG data-URI) + `padding-left:32px`.

**Python (`_render_filter_panel`):** dropped the literal `●`/`○`/"hidden" text from pill & chip labels (the dots are now CSS `::before`), so the labels are just the category name / `Label ×`.

**Second pass (match-the-design review):**
- **Per-section collapse.** Each column header (Event Type / Fiscal Quarter / Company) is now a clickable header with a chevron (`⌄`) that collapses just that section — the design shows a chevron per section. State in `ec_sec_{type,quarter,company}` (default open); `_toggle_section()` is pure-UI (never remounts the calendar). Header rendered as a `st-key-ec_secbtn_*` button styled flat (uppercase slate label + right chevron); chevron is a CSS `::after` SVG rotated 180° when collapsed (`button[kind="secondary"]`). Body rendered only when open.
- **Column proportions** `st.columns([3, 1, 3])` — Fiscal Quarter is narrow (2×2 Q grid), Event Type + Company wide, per the design (~3:1:3).
- **Filters button** now shows a lucide **sliders-horizontal** icon (CSS `::before` SVG, white on the dark open state / slate on the collapsed state) instead of a `☰` char.
- **Toolbar stats** dropped the `·` separators (design uses plain spacing; `gap:16px`).

## Testing (Playwright, isolated `:8502`, fresh code)

- Computed-style probes confirm live values: chip bar `rgb(56,189,248)`, ticker `rgb(2,132,199)`, badge slate-200, legend dot pastel+2px radius, pill `::before` dots render (sky/emerald pastels).
- **Filter expand/collapse** — works.
- **Select/unselect** — deselecting the IPO pill removes the `IPO ×` chip and re-filters; re-selecting restores it. All 8 chips present on fresh load.
- **Event click → detail panel** — works (verified with an earnings event; M&A/IPO/Delisted panels unchanged).
- **List view** — 111 rows, grouped by date, consistent colors + tags (M&A row: pastel-purple bar `#C4B5FD` + violet `M&A` badge).
- Screenshots match Figma Image #7 for filter panel, month grid, list view.

## Rollout

Changes are CSS/label-only in one file; no schema, no data, no new deps. The running dev server caches the Python module in memory — a **restart or watcher-triggered rerun** is needed to pick up the new styling (a fresh browser session alone won't, if the process already imported the old module).

## Bug fix — "remove last filter snaps all back" (empty-set vs unset)

**Reported:** removing fiscal-quarter pills one by one; after the *last* removal, all four quarters re-appeared instead of staying empty.

**Root cause:** six read sites used the idiom `set(st.session_state.get("ec_active_quarters") or list(_ALL_QUARTERS))` (and the same for `ec_active_types`). An **empty list is falsy**, so `[] or list(_ALL_QUARTERS)` evaluates to *all quarters*. The moment the user deselected the last pill, `ec_active_quarters` became `[]` and every read site interpreted that as "all selected" — the pills snapped back and the filter no-op'd. Both the top removable-chip `×` path and the expanded-pill path went through the same buggy reads, and so did the actual event-filtering at render time.

**Fix:** one helper `_selected_set(key, all_values)` that distinguishes `None` (uninitialised → default to all) from `[]` (user cleared → stay empty). Selection state is always initialised to a list on page entry (lines ~2542/2585), so `None` genuinely means "unset", never "cleared". Applied at all six sites: the two toggles (`_toggle_event_type`, `_toggle_quarter`), the two pill/chip active-state reads in `_render_filter_panel`, and the two event-filter reads at render. The `or []` in the analytics `log_filters_if_changed` call is intentionally left (empty→empty is correct there).

**Behaviour now:** removing all four quarters leaves zero pills/chips selected and hides all earnings (the pre-existing "No events match the selected filters" empty state handles the fully-empty case); M&A/IPO/Delisted markers remain since quarters only gate earnings. Re-adding one pill or Reset all recovers. Same for removing all four event types.

**Verified (Playwright, `:8502`, fresh code):**
- Quarters via pills: `201→186→34→17→2` events, chips `Q1Q2Q3Q4→…→[]`, pills all deselected — **no snap-back**. Re-add Q2 → 154 events; Reset → all 4 back.
- Quarters via top chip `×`: same, cleanly to `[]`.
- Event types: remove all 4 → `[]` + empty-state message shown; re-add + reset recover.
- Server log clean of errors during the run.

## Round 2 — exact source obtained from the `.make` file + five layout fixes

The business team shared `Calander.make` (a Figma Make export). It is a **ZIP**: `ai_chat.json` (the Make generation thread) embeds the **complete generated React/Tailwind source** as `write_tool` calls (double-JSON-encoded). Decoding the last `src/app/App.tsx` write gives the authoritative spec — every Tailwind class, exact. **This beats the Figma MCP** (our seat is View-only; MCP/Dev-Mode needs Edit) and beats reverse-engineering the preview iframe. Extraction recipe: `unzip`, `json.load(ai_chat.json)["threads"][0]["messages"]`, walk `parts`, for each string with `"write_tool"` do `json.loads(s)["argsJson"]` → `json.loads(...)["file_text"]`, keep the last per `path`. Saved to scratchpad `make_extract/App.tsx`.

Five fixes vs the source (all in `earnings_calendar.py`):

1. **Chips stretched as filters were removed** (the loudest complaint). Cause: the removable-chip row used `st.columns(n_chips)` with each chip `width="stretch"`, so fewer chips ⇒ each grew. Design is `flex flex-wrap gap-1.5` with `inline-flex` chips = **natural width, left-packed**. Fix: render the whole top row in a **`st.container(horizontal=True)`** (Streamlit 1.55 supports it; children default to content width) and force `width:auto; flex:0 0 auto` on chip wrappers. Verified: chip widths are constant (Earnings 103 / IPO 69 / M&A 75 / Delisted 99 / Q 52 px) before and after removing any subset.
2. **Missing Filters count badge.** Design: `Filters` button shows `inactiveCount` in a small circle. Added a dark-circle `.ec-fbadge` sibling (`inactiveCount = removed types + removed quarters + (company search?1:0)`), only when >0. Reads as the "counter in black background" the team asked for.
3. **No column dividers / outer border.** Design: one `border rounded-xl divide-x divide-slate-100 shadow-sm` box. Two bugs: (a) `st.columns(gap="small")` spaced the columns so the divider looked detached — fixed by zeroing the horizontal-block `gap`; (b) **the divider CSS targeted `[data-testid="column"]` but Streamlit 1.55 renders `[data-testid="stColumn"]`** so `border-right` never applied. Fixed the selector (`stColumn`) + `#E5E9F0` 1px dividers + `shadow-sm` on the box. **General trap: column testid is `stColumn`, not `column`.**
4. **Toolbar "very bad".** Rebuilt with nested horizontal containers: nav arrows flank the title on the left; stats + Email Alerts + legend + segmented view switcher pushed right via `margin-left:auto` on `ec_toolbar_right`. Segmented switcher track keyed on `.st-key-ec_view_switch`.
5. **Email Alerts button.** Now a white pill with a lucide **bell** `::before` icon + "Email Alerts" (was a 🔔 emoji).

Regression after all five: 22/23 automated checks pass (the 1 fail is the known `fill()`-without-Enter company-search artifact); chips fixed-width confirmed; badge shows correct count; dividers computed `1px #E5E9F0`; toolbar right-group flush-right (0px gap); server log clean.

## Round 3 — authoritative values from the Figma REST API (pixel reconciliation)

Client shared the final design as a Figma **design** file: `figma.com/design/CiMfOW2lULkY8p613AuNkX/...?node-id=24139-47522`. The cloud Figma MCP was disconnected and the local Dev-Mode MCP (`127.0.0.1:3845`) was off, but `.env` has a `FIGMA_ACCESS_TOKEN` (had to be regenerated — the old one was expired). **The REST API is the definitive source**, better than MCP or the `.make` file:
- Render: `GET https://api.figma.com/v1/images/CiMfOW2lULkY8p613AuNkX?ids=24139:47522&format=png&scale=2` (URL node-id dash → API colon).
- Structure: `GET .../files/CiMfOW2lULkY8p613AuNkX/nodes?ids=24139:47522` → 690-node JSON with every fill/stroke/effect/cornerRadius/auto-layout/text-style. A walker script converted it to a readable spec (scratchpad `figma_design/spec.txt`).

The frame **is** the calendar page, so this was a precision pass against exact values. Key corrections (all in `earnings_calendar.py`):
- **Font: Roboto → Inter.** The whole app is Inter (`styles.py`); only the calendar CSS used Roboto. Swapped all 20 usages + the Google-Fonts `@import`, and added an Inter `@import` **inside** `_get_calendar_css` (the FullCalendar iframe is a separate document and doesn't inherit the parent's font).
- **Filters button** `#0F172A → #2D2A29` (brand near-black), radius 10; count badge 16px `#2D2A29`.
- **Chips**: padding `2px 6px 2px 8px`, exact text colors `#0069A8`/`#007A55`/`#7008E7`/`#C10007`/Q `#45556C`.
- **Panel**: radius 14, two-layer shadow `0 1 2 -1 / 0 1 3 0 rgba(0,0,0,.10)`, dividers `#F1F5F9`; section labels 11px `#62748E` ls .55, chevrons `#90A1B9`; event-type rows radius 10 h32 with exact fills + a right-aligned "hidden" hint on inactive rows; Q buttons radius 10 h36 ring `#CAD5E2` text `#4C4E56`; search radius 10, placeholder `#90A1B9`.
- **Toolbar**: arrows radius 10 icon `#62748E`; title `#2D2A29`; stats `#4C4E56`/`#90A1B9`; Email Alerts radius 10 `#45556C`; legend dots radius 6 `#62748E`; segmented switcher active `#2D2A29`, two-layer shadow.
- **Calendar events**: radius 8, 2px bar, and the **missing pastel background tint** ({type}-50 @ 50%) added per type; exact company/ticker colors (earnings `#024A70`/`#0084D1`, ipo `#004F3B`/`#009966`, ma `#4D179A`/`#7008E7`, delisted `#9F0712`/`#E7000B`); IPO badge `#D0FAE5`/`#004F3B`, M&A `#EDE9FE`/`#4D179A`, Q `#E2E8F0`/`#45556C`. Weekday header `#90A1B9` (Sat/Sun `#CAD5E2`); day numbers `#45556C` (other-month `#CAD5E2`, weekend `#90A1B9`); prev/next & weekend cell washes.
- Filter band flattened (no shadow) vs the calendar card (radius 16 + two-layer shadow) to match the design's two distinct container treatments.

Verified via a Figma-render-vs-implementation side-by-side and a 22/23 functional regression (the 1 fail = the known `fill()`-without-Enter search artifact); server log clean.

## Round 4 — Figma MCP connected + custom Yearly view (node 24139:48905)

**MCP finally usable via the Claude Code Figma plugin** (`plugin:figma`, official remote `https://mcp.figma.com/mcp`). One-time OAuth per session: `authenticate` → user opens the figma.com/oauth URL, approves, pastes the `localhost:3118/callback?...` URL back → `complete_authentication`; then `get_screenshot` / `get_design_context` / `get_metadata` / `get_variable_defs` work. (The local Dev-Mode server on 3845 was never reachable; the `figma_age` 449xx ports are internal IPC, not MCP.) `get_design_context` can blow the token limit — it's saved to a file; the REST API remains handy for exact fills.

**Yearly view rebuilt as a custom grid.** The design's Yearly (node `24139:48905`) is a **3×4 grid of mini-month cards**, not FullCalendar's multimonth. Added `_render_year_grid()` (pure HTML via `st.html`) and branched the render: for `_view == "year"` it renders toolbar + grid + footer and returns early (before the `st_calendar` block), and calls `_ecal_loading_hint.empty()` because no iframe paints to auto-clear the sticky loader.

Spec (from node JSON): card radius 14, border `#E2E8F0`; header (pad 10/12, bg `#F8FAFC`, border-bottom `#F1F5F9`) = month name Inter 700 12px ls .3 `#333333` + one 6px dot per event-type present (order earnings/ipo/ma/delisted, type-colored) + count badge `#E2E8F0`/`#62748E`. Body (pad 12): MTWTFSS row Inter 600 8px `#90A1B9` (Sat/Sun `#CAD5E2`); 7-col day grid, cell min-h 36 border `#F8FAFC`, day number Inter 600 9px `#62748E`; per-day event rows = 4px dot + ticker Inter 500 8px in the type's 400 shade (earnings `#38BDF8`, ipo `#6EE7B7`, ma `#C4B5FD`, delisted `#D62E2F`), capped at 2 then `+N` (Inter 400 7px `#90A1B9`). **Current month red**: card border `#D62E2F` + two-layer shadow, header bg `#D62E2F` white name, dots `#FFFFFF@0.70`, badge `#FFFFFF@0.20`; today cell red border + red number. Footer `Showing X of Y events in {year}` + Q1–Q4 badges (`#E2E8F0`/`#45556C`).

Real data is far denser than the mock (July ≈ 111 events vs 32) — the `+N` overflow absorbs it, month cards auto-grow. Verified: Yearly renders 12 cards with July highlighted + today red, view-switch Monthly↔Yearly↔List all pass (22/23 regression, the 1 fail = the known search-input harness artifact), server log clean. Side-by-side (design vs impl) captured.

## Test-harness gotcha (recorded so it isn't re-hit)

Playwright `button:has-text('List')` is a **case-insensitive substring** match and also matches **"De·list·ed"** (which precedes the List button in the DOM), silently deselecting Delisted instead of switching view. Click view buttons by their `st-key` (`[class*='st-key-ec_view_list'] button`).
