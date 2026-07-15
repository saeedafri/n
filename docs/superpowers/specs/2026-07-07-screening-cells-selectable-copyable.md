# Screening — Make Every Cell Selectable & Copyable

**Date:** 2026-07-07
**Page:** `app/pages/screening.py` — `_render_filterable_results_grid()` (shared by ALL screening grids), `requirements.txt`
**Requested by:** Analyst / business — long cell values (e.g. Key Developments summaries) can't be read or copied.

## Problem
On STG, screening result cells were not selectable — analysts couldn't read or copy the full value of long columns. The grid already set `enableCellTextSelection=True`, `ensureDomOrder=True`, `enableBrowserTooltips=True`, and a `tooltipValueGetter` — and these **work** on the locally-installed `streamlit-aggrid 1.2.1.post2`. But `streamlit-aggrid` was pinned only as `>=1.2.0` (unpinned), so STG could deploy an AG Grid build where `enableCellTextSelection` is no longer honored (AG Grid deprecated it in v32.2 in favor of `cellSelection`). Same class of unpinned-dependency drift as the Streamlit 1.55/1.58/1.59 filter bug.

## Fix (both parts)
1. **Version-proof cell selection** — pass `custom_css` to the `AgGrid(...)` call in
   `_render_filterable_results_grid` (the single helper used by Company, Key Devs,
   People, and Segment grids). It injects **into the grid iframe** and forces
   `user-select: text` + `cursor: text` on `.ag-cell`, `.ag-cell-value`, and inner
   `span`/`a`. This makes every cell selectable regardless of the AG Grid version —
   an analyst can drag-select and Cmd/Ctrl+C the **full** value of any column (even
   when visually truncated), not just Company/Key-Dev.
2. **Pin the dependency** — `streamlit-aggrid==1.2.1.post2` in `requirements.txt`
   (was `>=1.2.0`) so STG/prod install the same known-good build.

Seeing the full value: `enableBrowserTooltips=True` + `tooltipValueGetter` already
put the full value in each cell's `title`, shown on hover; columns are `resizable`.

## Verification (Playwright, real installed AG Grid)
Rendered the **real** `_render_filterable_results_grid` with a 220-char long-text
column and inspected the grid iframe:
- All sampled cells: `user-select: text`. ✅
- Long cell fully selectable: **220 of 220** chars selected (full value, though visually truncated). ✅
- Hover tooltip (`title`) present on cells → read full value without copying. ✅
- Isolated min-grid A/B (`enableCellTextSelection` only vs `custom_css`): both give
  `user-select:text` on 1.2.1.post2 → the `custom_css` is the deterministic guarantee
  for whatever version STG runs.

All four call sites (`screening.py:4779` Key Devs, `5285` People, `5444` Segment,
`6117` Company) share the patched helper → every cell in every screening grid.

## Rollout
Sync `app/pages/screening.py` + `requirements.txt` to STG-deploy and redeploy so STG
installs the pinned `streamlit-aggrid==1.2.1.post2` and serves the `custom_css`.
