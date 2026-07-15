# Company Screening — Key Developments criterion always shows event DETAILS

**Date:** 2026-07-06
**Author:** Claude (Opus 4.8), driven by Mohd Saeed Afri
**Scope:** `app/data/screening_service.py::apply_keydevs_criterion` + the two key-dev
add-criterion forms in `app/pages/screening.py`.
**Type:** Regression / UX fix. Non-filtering mode UNCHANGED. No schema / no writes.

---

## 1. Symptom

Screen For = Companies + a *Key Developments by Category* criterion → the results
column ("Key Developments by Category - [Last N Days]") showed only a bare **`✓ Matched`**
(or N/A) marker instead of the actual key-development details. The user asked for the
development details (date · type · headline), not a match flag.

## 2. Root cause (proven, STG DB)

A **default mismatch** between the UI and the service, plus the marker introduced by the
2026-07-04 #44 patch:

- Both add-criterion forms default the "Show latest event in results" checkbox to **True**,
  so a *freshly* added criterion carried `show_headline=True` → details rendered (verified
  in the UI: real dated headlines, zero markers).
- `apply_keydevs_criterion` defaulted `criterion.get("show_headline", **False**)`. So any
  criterion that reached the service **without an explicit `show_headline`** — i.e. a
  **saved / loaded criterion** (Saved Screenings), an edit-in-place, or a legacy dict —
  fell into the `show_headline=False` branch, which the #44 patch renders as `"✓ Matched"`.

That is the user's screenshot: a criterion whose `show_headline` was missing/False → the
useless marker instead of the events.

Probe (`recompute_working_set`, STG), before the fix:
```
show_headline MISSING → column = "✓ Matched" (75 cells)   ← the bug
show_headline=False   → column = "✓ Matched"
show_headline=True    → column = real event details
```

## 3. Decision

A Key Developments criterion exists to surface the developments. The `✓ Matched` /
"compact" mode is not wanted. **Always show the event details; retire the marker and its
checkbox.**

## 4. Fix

`apply_keydevs_criterion` (`screening_service.py`):
- Dropped the `show_headline` read and the `if show_headline / else` branching.
- **Always** runs the detail query (narrow `ROW_NUMBER()` over `event_id/event_date`, then
  PK join for the headline TEXT of the ≤10 latest events/company — the fast ~2.7s shape).
- **Always** builds the `event_map` and populates `display_col` with the details
  (`date · (type)` + headline, ≤10/company). Removed the `"✓ Matched"` branch entirely.

`screening.py` (both key-dev forms — `_inline_add_keydevs` and `_render_keydevs_form`):
- Removed the now-meaningless **"Show latest event in results"** checkbox.
- Criterion is stamped `show_headline: True` (kept for dict-shape stability; the service no
  longer reads it, so saved/legacy criteria missing the field also render details).

**Non-filtering mode is unchanged** — the key-dev criterion still annotates the full 349
universe (matched companies show their events; the rest N/A). Only the *content* of the
result column changed (details, never a marker).

## 5. Verification (evidence)

Pipeline (`recompute_working_set`, STG) — details for ALL entry paths, zero markers:
```
show_headline MISSING → 75 detail cells, 0 '✓ Matched'   (the saved/loaded bug — FIXED)
show_headline=False   → 75 detail cells, 0 '✓ Matched'
show_headline=True    → 75 detail cells, 0 '✓ Matched'
   sample: "7/4/2026  (Earnings News) / Stuart Investment Advisors Inc. Acquires Shares of 6,782 Amazon.com…"
```

Real UI (local Playwright, STG DB): add *Key Developments = Earnings & Guidance [Last 7
Days]* → the form no longer shows the "Show latest event" checkbox; the results grid's
key-dev column shows real dated headlines (AMD / Amazon / Arista …), **0 `✓ Matched`**;
"349 companies matched" (non-filtering) preserved. `py_compile` clean; no screening errors
in the server log.

## 6. Risk

- Read-path only; no schema, no writes.
- The detail query now always runs for a key-dev criterion (~2.7s worst-case, previously
  only when the box was ticked — which was the default anyway).
- Supersedes the `✓ Matched` half of the 2026-07-04 #44 change; the "always attach a
  results column" half (never a blank grid) is kept.
