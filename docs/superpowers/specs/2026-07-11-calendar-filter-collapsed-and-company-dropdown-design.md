# Calendar — Collapsed Filters by Default + Restored Company Dropdown

**Date:** 2026-07-11
**Page:** `app/pages/earnings_calendar.py` (`/earnings_calendar`)
**Requested by:** Business team
**Type:** Minor UX enhancement (faithful restoration of prior-app behavior)

---

## Problem

Two business asks on the Calendar page:

1. **Filter panel opened by default.** The expanded 3-column panel (Event Type /
   Fiscal Quarter / Company) rendered on every fresh page load, pushing the
   calendar down. Business wants the panel **collapsed** by default — the user
   opens it only when they want to filter.
2. **Company filter was a free-text search box, not a picker.** The current
   Company control was a `st.text_input` ("Company or ticker…") that substring-
   matched event names/tickers. The business wanted the **previous calendar's
   company dropdown** back: click → a full, scrollable, type-to-search list of
   every company (e.g. *Apple Inc. (AAPL)*), pick one → calendar filters to it.

Reference (previously-working) implementation:
`/Users/mohdsaeedafri/All-Code-Base/market-prod/app/pages/earnings_calendar.py`
(a native `st.selectbox("Company", options=["All Companies"] + "Name (TICK)"…)`).

---

## Root cause

The current file had already been **migrated** from the selectbox to a text
search, but left the selectbox scaffolding half-wired:

- `_all_opt`, `ticker_map`, `all_labels`, `_default_idx`, and the
  `_on_company_change` callback were all still built (lines ~3057–3100).
- `all_labels` was even still passed into `_render_filter_panel`.
- Only the widget itself was a `st.text_input`, and event filtering used
  `ec_company_search` substring matching.

So restoring the dropdown was a matter of re-connecting the existing plumbing,
not new machinery.

---

## Design / changes (`app/pages/earnings_calendar.py`)

### A. Collapsed by default
- `_toggle_filter_panel` default flipped `True → False`.
- `_render_filter_panel` reads `ec_filters_open` default `False`.
- Session-state init (`render_page`) sets `ec_filters_open = False`.
- Fresh-navigation reset sets `ec_filters_open = False`.
- The collapsed state still shows the **active-filter chips** row (Earnings ×,
  Q1 ×, …) so the user sees what's applied without opening the panel.

### B. Company = native searchable dropdown (restored)
- In the Company expander, replaced `st.text_input(key="ec_company_search")`
  with `st.selectbox("Company", options=all_labels, index=default_idx,
  key="ec_company_filter", on_change=on_company_change,
  placeholder="Company or ticker…")`.
  - `all_labels = ["All Companies"] + ["<Name> (<TICK>)", …]` (built from
    `all_tickers_meta`, alphabetical).
  - Native selectbox is **client-side searchable** — clicking shows the whole
    list; typing narrows it instantly (no server round-trip until selection).
- `_render_filter_panel` signature gained `default_idx` and `on_company_change`;
  call site passes the existing `_default_idx` / `_on_company_change`.
- Hint text under the box: `"All companies selected"` when *All Companies*,
  else `"{n} matching events"`.

### C. Event filtering by selection (in-memory, 0 DB round-trips)
Replaced the substring `ec_company_search` filter with an **exact-ticker** match
derived from the selected label via `ticker_map`:

```python
_company_label  = st.session_state.get("ec_company_filter") or ""
_company_ticker = ticker_map.get(_company_label, "") if _company_label != _all_opt else ""
if _company_ticker:
    events = [e for e in events if str(e.get("ticker") or "").lower() == _company_ticker.lower()]
    # …same for ma/ipo/delisted feeds
```

The previous app scoped this at the DB (`get_calendar_events(tickers=…)`); here
events are already prefetched for all companies, so filtering is a pure in-memory
pass — **faster** (no extra query) and identical results.

### D. In-place calendar update on selection (performance-critical)
`_on_company_change` closes any open detail panel (`ec_selected_event = None`) but
**does NOT bump `ec_cal_version`**. Keeping the calendar `key` stable lets
`streamlit_calendar` swap the filtered `events` prop **in place** — no iframe
teardown, no component-bundle reload, no "Loading Calendar" flash. It keeps the
existing cross-page ticker sync (`?ticker=`, `active_ticker`).

> **Regression avoided:** an earlier revision bumped `ec_cal_version` on company
> change (mirroring the watchlist handler). That forced a full FullCalendar iframe
> **remount** on every selection — the server render is only ~260ms, but the
> remount added a multi-second component reload + loader flash, which read as
> "very very slow." Removing the bump restores the fast path the old free-text
> search already used (stable key → in-place event swap). The watchlist selector
> still bumps the version (unchanged / out of scope); if it feels slow too, the
> same one-line removal applies.

### E. Housekeeping
- `_reset_filters` resets `ec_company_filter = "All Companies"` (was clearing the
  removed search key). "Reset all" now appears when a specific company is chosen.
- Removed the dead `ec_company_search` session init and its orphaned CSS block.

---

## Performance

"Lightning fast" is preserved/improved:
- Dropdown open + type-to-search is **100% client-side** (Streamlit selectbox
  virtualizes the option list) — no rerun while browsing/typing.
- Selecting a company triggers **one** rerun; event filtering is an in-memory
  list comprehension over the already-prefetched feeds — **no new DB calls**
  (measured: all fetches 0.00ms/cached, `EC_PAGE_RENDER_TOTAL` ~260ms).
- The calendar updates **in place** (stable key) — no iframe remount, no
  "Loading Calendar" flash (see §D).

### F. Window ALL feeds to the visible range (round-2 perf fix)
Previously only the ~16k earnings feed was windowed; the M&A (264), IPO (338) and
delisted (6) overlay feeds were sent to FullCalendar **in full** on every render —
~**1,415 events** shipped to the iframe per view. Because all calendar navigation
is server-side (custom `‹`/`›` buttons + Month/Year/List toggles rerun and
re-resolve the window; `headerToolbar: False` disables native FullCalendar arrows),
the overlays never need to be off-window. Windowing all four feeds cuts the iframe
payload to ~**202 events** for a month view. Badge counts stay full-set (computed
pre-window). Verified overlays still render (M&A/IPO/delisted markers), and Month
nav / Year / List views all re-window correctly.

**Measured impact (warm server, STG data):**

| Interaction | Before round-2 | After |
|-------------|----------------|-------|
| Company select (median, warm session) | ~0.9s | **0.13s** (0.06s steady) |
| Full load, warm browser cache | ~2.2s | **~1.3s** |
| Full load, first-ever visit (bundle download) | ~5s | **~1.9s** |
| Events shipped to FullCalendar (month) | ~1,415 | **202** |

The residual full-load time is dominated by the `streamlit_calendar` component
bundle download + FullCalendar init (a fixed cost of the component, not our code);
a returning user with the bundle cached loads in ~1s.

### G. Blank gap under the calendar after filtering (round-3 fix)
**Symptom:** selecting a company left a large blank area between the calendar grid
and the "Showing … events" footer (All Companies looked fine).

**Root cause — a side effect of the in-place fast path (§D):** `streamlit_calendar`
sizes its iframe to the content on mount but does **not** call `setFrameHeight`
again on an in-place `events` change. With `height:"auto"`, a dense *All Companies*
month produced a tall iframe (e.g. 783px); filtering to one company shrank the grid
(e.g. to 405px) but the iframe kept 783px → ~378px blank. Same effect in List view,
worse (7001px → 92px ⇒ ~6.9k px blank).

**Fix, per view:**
- **Month/day-grid:** give it a **deterministic height** = `33 + weeks × 150`,
  where `weeks` = `_month_grid_weeks(anchor)` (the FullCalendar grid's week-row
  count for that month). Height now depends only on the calendar structure, never
  on event count, so the grid always exactly fills the iframe — no blank, no
  remount, still instant. `150px/row` matches the previous dense auto height
  (5-week month ≈ 783px, 6-week ≈ 933px) and fits `dayMaxEvents:3` with no clip.
- **List view:** height is intrinsically event-count-driven, so a deterministic
  height isn't possible. `_on_company_change` bumps `ec_cal_version` **only when
  `ec_view == "list"`**, remounting the list at the correct (tight) height. Only
  the filtered company's rows render, so the remount is cheap. Month view keeps the
  no-remount fast path.
- **Year view:** custom HTML grid (no iframe) — unaffected.

**Verified (STG data):** month blank gap = **0** across 8 consecutive months
(5- and 6-week), `scroll_overflow = 0` (no clipping) in every month; List filtered
iframe shrinks 7001→92px, blank = 0; Year filtered renders the 12-month grid with
no gap. Month select stayed **0.13s** median (unchanged by the height fix).

---

## Testing (verified in the real UI — local launcher + Playwright)

Launched `bash .claude/dev/run_local.sh` (LOCAL OIDC bypass, STG DB) and drove
`/earnings_calendar` headless:

| Check | Result |
|-------|--------|
| Panel state on load | **Collapsed** — no Event Type/Company sections; chips + calendar visible |
| Click "Filters" → expand | Event Type / Fiscal Quarter / **Company dropdown** render |
| Company control type | Native `stSelectbox` (combobox), value "All Companies" |
| Open dropdown | Lists all companies: *All Companies, 1-800-FLOWERS.COM (FLWS), ADT (ADT), ANTA Sports (2020), …* |
| Type "AAPL" | Narrows to **Apple Inc. (AAPL)** (client-side search) |
| Select Apple | Stats **16,707 events / 338 companies → 76 events / 1 company**; hint "76 matching events"; calendar shows only AAPL |
| Cross-page sync | URL → `?ticker=AAPL`, `active_ticker` set |
| Reset | "Reset all" appears when a company is selected |

Screenshots retained during verification: default (collapsed), expanded dropdown,
Apple-filtered.

---

## Rollout

Single-file change; no schema, config, or dependency changes. No new DB calls.
Backward-compatible session keys (`ec_company_filter`, `ec_filters_open` already
existed). Safe to ship with the normal deploy.
