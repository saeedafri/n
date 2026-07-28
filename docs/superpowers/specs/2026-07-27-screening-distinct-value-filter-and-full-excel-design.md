# Screening: Excel-style distinct-value column filter + full Key-Devs Excel export

**Date:** 2026-07-27
**Page:** `/screening` (`app/pages/screening.py`)
**Author:** (design)

## 1. Problem

The screening results tables (AG Grid via `streamlit-aggrid==1.2.1`) give per-column
**sort (ASC/DESC)** and a **text "contains" filter** in the column-menu funnel. They do
**not** offer Excel's *distinct-values checkbox dropdown* — a searchable list of the valid
values in that column with per-value checkboxes and a "(Select All)".

Second issue: the **Key Developments** grid paginates the on-screen rows at 500 with a
"Load more events" link, and its single **Excel** button exports the full matching set but
**hard-capped at 50,000 rows** (`_KD_EXPORT_CAP`). Users with >50k matching events only get
part of the data. Requirement: Excel must contain **everything**, fast, from the **same one
button** (no new/extra button, no UI change).

## 2. Constraints (from the user)

- **Free only** — no AG Grid Enterprise license. (Enterprise `agSetColumnFilter` is bundled
  but unlicensed use paints a red "License Key Not Found" watermark → not acceptable.)
- **No new buttons / no UI additions.** The distinct filter lives inside the existing
  column-menu funnel; the Excel button stays exactly one button in its current place.
- **All screening grids** get the filter (they share `_render_filterable_results_grid`).
- **Download truly everything** — remove the cap.
- **Lightning fast** — client-side filtering (no server rerun); streaming workbook build.

## 3. Approach

### 3.1 Distinct-value filter — custom AG Grid **Community** filter component

AG Grid v32/34 (confirmed in the bundled frontend) supports **custom filter components** in
Community. We register **one** custom filter class as the grid's `default_column` filter, so
every column in every screening grid inherits it. The component implements the `IFilterComp`
interface and renders, inside the existing funnel popup:

- a **search box** (subsumes the old "contains" behavior),
- a **"(Select All)"** checkbox (tri-state / indeterminate),
- a scrollable **checkbox list of the column's distinct values** (blanks shown as `(Blanks)`),
  sorted with natural/numeric-aware ordering.

Behavior:
- Distinct values are collected once in `init()` via `api.forEachLeafNode` over the loaded
  rows (≤ a few thousand → instant).
- **Auto-apply on toggle** (`filterChangedCallback()`), matching Excel's "Auto Apply" — so
  **no Apply button** is added. Filtering is 100% client-side.
- `getModel`/`setModel` persist selection across reruns; `isFilterActive` is true only when a
  strict subset is selected (so the funnel shows "active" exactly like Excel).

Delivered via `st_aggrid.JsCode` (comment-free, whitespace-safe — JsCode strips comments and
collapses whitespace). `allow_unsafe_jscode=True` is already set on every grid.

**Change point (single):** `_render_filterable_results_grid`
- `configure_default_column(...)`: replace `filter="agTextColumnFilter"` + the text
  `filterParams` with `filter=<DistinctValuesFilter JsCode>`. Keep `sortable`, `resizable`,
  `menuTabs` (funnel + sort menu unchanged), tooltip getter, etc.
- Remove the explicit `filter="agTextColumnFilter"` on the **pinned** column and **link**
  columns so they inherit the new default (no separate behavior).

Everything else in that function (selection checkbox, cell copy, autosize, CSS) is untouched.

### 3.2 Full Key-Devs Excel — remove the 50k cap (via keyset-paginated assembly)

**Change point (single):** `_render_keydevs_results`

> **Critical finding during verification (probe against STG):** a naive
> `limit = st.session_state["kd_total"]` (one giant `LIMIT = total` query) does **NOT**
> work. The read-only engine has `read_timeout = 120s` (`DB_READ_TIMEOUT`), and the full
> 378-company universe over all history is **~145k events** (all 3,998 tickers → ~156k).
> A single 100k+ row statement **exceeds 120s and returns an EMPTY DataFrame** → the
> workbook would be empty. The old 50k cap wasn't arbitrary — it kept the query under the
> timeout. So "no cap via one query" is *worse* than the cap (empty instead of 50k).

**Fix — assemble the full set with keyset pagination** (the same seek the grid already uses):
- Loop `get_keydevs_events_for_tickers(..., limit=10000, before_date/before_id=<cursor>)`,
  accumulating pages until `next_cursor is None` (or an empty page). Each 10k-row page is a
  fast, index-served seek that stays **well under** the 120s per-query timeout, so the
  workbook always holds **every** matching event — no cap.
- A defensive runaway guard (`_XL_RUNAWAY = 500000`, far above any real dataset — the whole
  table is ~156k) prevents an unbounded loop; hitting it is logged. Not a user-facing cap.
- Keep the existing **streaming** builder `_build_keydevs_excel_fast` (openpyxl `write_only`
  + `itertuples`) — handles 100k+ rows.
- Keep the current amortization: assembled + built **once per query** inside the existing
  sticky-loader block, cached in `st.session_state["kd_xl"]`; ordinary reruns
  (sort/select/load-more) never rebuild it. The client-side JS blob download stays instant.
- **No `st.fragment(run_every=...)` and no background thread** — that crash-looped STG.

The grid still shows 500 rows/page with "Load more" (fast on-screen). Only the workbook holds
the full set — exactly the requested behavior.

**Verified (UI + STG probe):** a real Key-Devs screen of 47,420 events built the full workbook
(~19 MB, all 47,420 rows) behind the loader with the single Excel button and no error; the old
path capped it at 50k / a single-query attempt returned empty.

**Note on scale/latency:** for very broad screens (tens/hundreds of thousands of events) the
eager assembly + workbook are genuinely large (a ~145k-row export is ~50 MB and its base64
blob is heavier still) and take real time to build even server-side. This is the literal
consequence of "download everything, no cap" for the broadest screens; typical targeted
screens (hundreds–few thousand events) build and download near-instantly.

Companies / People / Segment grids already render and export their **entire** result set
(no pagination, no cap) — verified — so no change needed there.

## 4. Data flow (unchanged except the two edits)

Show Results → recompute → `scr_working_df` → grid renderers → `_render_filterable_results_grid`
(now with distinct filter) → Excel via `_render_excel_js_download` (client-side blob).
Key Devs: query page (500) + count(total) + **full-set** workbook (uncapped) built once/loader.

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Custom JsCode filter mis-constructs → grid fails to render | Mandatory end-to-end UI test (Playwright) on Companies **and** Key Devs before "done"; keep the non-aggrid `st.dataframe` fallback path intact. |
| Very high-cardinality column (e.g. Summary) → long checkbox list | Scrollable list + search box; render is DOM-cheap for ≤ a few thousand distinct. |
| Uncapped export on pathological "All History" (100k+) adds seconds to first Key Devs load | Build is behind the existing sticky loader, cached per query; streaming builder is fast. Accepted per user ("truly everything, no cap"). |
| AG Grid version fragility (pin is 1.2.1 for a reason) | Filter uses only stable IFilterComp APIs; tested against the pinned build; no version bump. |

## 6. Testing plan (mandatory, real UI)

`bash .claude/dev/run_local.sh` → `http://localhost:8501`, drive with `.claude/dev/ui_test.py`.
1. Companies screen → open a column funnel → confirm search + (Select All) + distinct
   checkboxes; tick a subset → grid filters client-side; clear → all return.
2. Key Devs screen → same filter on its columns (Date, Type, Company, etc.).
3. Key Devs Excel → download → confirm row count == header total (not 500, not 50k) on a
   query known to exceed 500.
4. Sanity: sort ASC/DESC still works; cell copy/tooltip still works; no console errors.
Screenshots + dumped text as evidence.

## 7. Out of scope

- No Enterprise modules / license. No new buttons. No new pages. No DB writes.
- No changes to the download button's look or count (stays one branded button).
