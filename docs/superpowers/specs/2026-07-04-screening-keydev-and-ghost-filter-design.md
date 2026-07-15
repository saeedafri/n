# Screening Key-Dev blank grid + market_data ghost filter row

**Date:** 2026-07-04
**Author:** Claude (Opus 4.8), driven by Mohd Saeed Afri
**Scope:** `screening.py` + `screening_service.py` (Key-Dev results column) and
`market_data.py` (duplicate/ghost filter row after a Period Type change).

---

## 1. "Where is the data" — Key-Dev screen returned a blank grid (#44)

**Symptom:** Screen For = Companies + a *Key Developments by Category* criterion →
"349 companies matched" but the results grid showed only the **Company Name** column;
everything else blank.

**Root cause (proven directly, no UI):** the key-dev results column was attached ONLY when
the *"Show latest event in results"* checkbox (`show_headline`) was on:
- `app/pages/screening.py` set `criterion["display_col"]` inside `if show_headline:` (two
  add-criterion forms).
- `app/data/screening_service.py::apply_keydevs_criterion` stamped `filtered[display_col]`
  inside `if show_headline:`.

So with the box off (or a saved criterion with it off) the criterion had **no display_col**
→ `_build_results_display_df` had no key-dev column to render → blank grid. Confirmed:
```
apply_keydevs_criterion, show_headline=False → df columns = [ticker, company_name]   (no col)
apply_keydevs_criterion, show_headline=True  → df has "Key Developments…" filled
```

**Fix:** always attach the column.
- Both screening.py forms now set `display_col` unconditionally.
- `apply_keydevs_criterion` always stamps it: `show_headline=True` → latest events
  (date · type · headline, ≤10/company; narrow ROW_NUMBER() + PK join for the fat headline
  text); `show_headline=False` → `"✓ Matched"` marker so the column is never blank.

**Verified end-to-end** through the real pipeline (`recompute_working_set` → grid column
logic) for the exact user scenario (23 categories, All History):
```
matched companies = 349   key-dev column present = YES   filled = 349/349   → shows in grid
```
(both headline modes). Smoke-tested screening/market_data/calendar afterward: all render, 0
page errors.

> Testing note: driving the Key-Dev form in Playwright is unreliable — the categories
> `st.multiselect` detaches from the DOM on each rerun. Verify the LOGIC via
> `apply_keydevs_criterion` / `recompute_working_set`, not click-through.

---

## 2. Ghost duplicate filter row after Period Type change (#38)

**Symptom:** changing Period Type (e.g. Annual↔Quarterly) shows a faint SECOND filter row
(unlabeled dropdowns) above the real one for several seconds.

**Root cause (reproduced + measured):** the selectbox count jumps **9 → 17** during the
change (the 8-box filter row mounted twice), then back to 9. It is **not** a code duplicate
(the row renders once) and **not** the `st.query_params` write in the callback (removing it
did not help — experimentally ruled out). It is **Streamlit keeping the previous run's
widgets mounted (marked `data-stale="true"`) while a slow rerun runs** — the Period Type
change triggers a multi-second cold data fetch (`KS_get_data` + date-range), and the stale
old filter row coexists with the freshly-built one until the run finishes.

DOM dump at the 17-box moment:
```
old/ghost filter row  → ancestor data-stale="true"
fresh filter row      → data-stale="false"
```

**Fix (CSS, in market_data.py's filter `<style>`):**
```css
[data-testid="stElementContainer"][data-stale="true"]:has(div[data-testid="stSelectbox"]) {
  display: none !important;
}
```
Hides the stale copy of any select control during a rerun. The fresh copy renders *before*
the slow fetch, so exactly one filter row is ever visible — and since the fresh row is
present throughout, there is no blink. Scoped to selectbox containers so data tables /
period-fallback notices keep their normal stale-then-update behaviour.

**Verified (9→17 gate):** counting VISIBLE selectboxes through Q→Annual / Annual→Q /
Q→Annual, `peak_visible` stayed **9** (DOM still holds 17; 8 are hidden). Mid-change
screenshot (taken while the stale copy is in the DOM) shows one clean filter row, correct
Annual data, company preserved.

Caveat: `:has()` requires a modern browser (fine on current Chrome/Safari/Firefox).

---

## 3. #39 (nav delay) / #40 (screening blank) — not code bugs

Both are **cold first-load latency**. #40 reproduced as a slow top-down load, not a crash
(it fills in; screening cold-load was ~21 s over the home→Azure link, far faster on STG).
No data loss. The local ~7 s first-query cost is the home→Azure SSL cold-connection, not an
STG problem.

## 4. Rollout / risk

- All read-path; no schema, no writes, no data touched.
- #44 changes only add a column (events or a marker); existing screens gain the missing
  column, none lose data.
- #38 is a pure CSS visibility rule scoped to market_data selectbox containers.
