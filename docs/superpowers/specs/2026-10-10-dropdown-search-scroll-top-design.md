# Dropdown search: matches start at the top

**Date:** 2026-10-10
**Scope:** every `st.selectbox` / `st.multiselect` in MIP (about 100 call sites)
**Files:** `app/components/navigation.py` (`_inject_transition_js` driver)

## Problem

Open the company picker while Amazon is loaded and type `wal`. The list filters
correctly, but its scrollbar stays partway down. Walmart, the first match, sits
above the visible area, and the user has to scroll up to find it.

## Root cause

Streamlit 1.55 draws the options in a virtual list (react-window), marked
`ul[data-testid="stSelectboxVirtualDropdown"]`. Its first child `div` is the
scroll container.

1. On open, BaseWeb scrolls that container so the current choice is visible.
   For Amazon this was `scrollTop = 750`.
2. Typing filters the options, but react-window keeps the old `scrollTop`.
   After filtering it was clamped to 220, so the list showed
   "Sportsman's Warehouse…" first and Walmart was hidden above.

This is how the library behaves, not app logic, so every selectbox in the app
has the same problem.

## Fix

The page-transition driver already runs once in the parent document on every
page that renders `render_header`. It now also listens for `input` events (in
the capture phase) from inside any `[data-baseweb="select"]`. After two
animation frames, once React has re-rendered the filtered list, it sets
`scrollTop = 0` on each open virtual dropdown.

- No server rerun, no Python logic change, no new dependency, no visual change.
- Option order is unchanged. BaseWeb already highlights the first match, so
  pressing Enter picks the item the user sees at the top.
- Cost is O(1) per keystroke: one `querySelectorAll` on the open popover. The
  virtual list renders only about 10 rows, however many options there are.

## Rollout

Already-open browser tabs keep the old driver (it installs once per document),
so users need one full reload. The local dev server also needs a restart:
Streamlit did not hot-reload `navigation.py`.

## Testing

Run the Playwright probe against `/market_data?ticker=AMZN` with the company
picker:

| Step | Before | After |
|---|---|---|
| open | scrollTop 750 | scrollTop 750 (current choice still shown on open) |
| type `wal` | scrollTop 220, first visible = Sportsman's Warehouse | scrollTop 0, first visible = Walmart Inc. (WMT) |
| type `cos` | scrollTop 750, first visible = Conagra | scrollTop 0, first visible = Costco |

The app-wide sweep (every visible dropdown on each page: open, type, check the
top match, Escape) is recorded in the task report.
