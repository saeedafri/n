# Pause Quarterly Forecasting — Design Spec

**Date:** 2026-07-13
**Author:** Engineering (at Data team's request)
**Status:** Implemented
**Type:** Temporary feature pause (reversible)

---

## 1. Goal

The Data team asked to **pause Quarterly revenue forecasting "for now."**

Hard requirement: **Annual forecasting must be 100% unaffected** — its UI, its
services, and its background refresh continue exactly as before.

The pause must be **fully reversible** (flip one flag to restore) and must **not
delete any code, service, engine, or DB data**. This is a pause, not a removal.

## 2. Approach — single feature flag

Add one module-level constant:

```python
# app/utils/constants.py
QUARTERLY_FORECASTING_ENABLED = False  # paused per Data team 2026-07-13; flip to True to restore
```

Every quarterly-forecasting surface is gated behind this flag. When `False`, the
quarterly branch is simply never taken — the annual branch runs unchanged. To
restore, set it to `True`. No other change is required.

Rationale for a flag over commenting-out code: a single reversible switch, no
risk of missing a spot or leaving dangling references, and it reads cleanly in
review. (Considered: literal comment-out — rejected as messier to revert and
easier to get wrong.)

## 3. Surfaces (verified in code)

Quarterly revenue forecasting is exposed / executed in exactly three places.
Annual runs through entirely separate values, methods, and cadences in all three.

| # | Surface | Quarterly path | Annual path (untouched) |
|---|---------|----------------|-------------------------|
| A | `/forecasting` page — "Period" toggle (`forecasting.py` ~3207-3218) + `?period_type=Quarterly` deep-link | `RevenueForecastService.get_quarterly_dashboard` | `get_company_dashboard` |
| A2 | `/forecasting` admin "Refresh Data" dialog (`forecasting.py` ~2934-2945) | `sync_quarterly_forecast_for_ticker` / `sync_all_eligible_quarterly` | `sync_forecast_for_ticker` / `sync_all_eligible` |
| B | `/market_data` → Forecasting tab (`market_data.py` `get_available_period_types` ~268-275; render ~3906) | `ModelForecastsRepository.get_quarterly_forecasts_data` | `get_forecasts_data` |
| C | Background auto-refresh (`forecast_auto_refresh.py` `_run_cycle` ~300) | `("annual","quarterly")` loop → quarterly cadence | annual cadence |

The quarterly engine `RetailerQuarterlyForecaster` is reached **only** through
the admin sync (A2) and the quarterly dashboard (A). Gating those neutralizes the
engine with no edit to the engine itself.

`market_data.py` snaps each tab's effective period type to
`get_available_period_types(ticker, tab)` (line ~1855-1860): if that returns only
`["Annual"]` for the forecasting tab, `_period_type_db` is forced to `"annual"`,
so the quarterly date-range load (~1924) and quarterly render (~3906) are both
skipped automatically. That makes surface B airtight from a single choke point;
a defense-in-depth gate is still added at the render site.

Out of scope (confirmed unrelated to revenue forecasting): `screening.py`
Quarterly period type, `background_scanner.py` 10-Q filing directories,
market_data quarterly *statements/estimates/segment* (Income Statement, Balance
Sheet, etc.). These are not touched.

## 4. Changes

1. **`app/utils/constants.py`** — add `QUARTERLY_FORECASTING_ENABLED = False`.
2. **`app/pages/forecasting.py`**
   - Import the flag.
   - Period toggle: when flag off, skip the radio and force
     `forecasting_period_type = 'Annual'` (also neutralizes the deep-link).
   - Admin Refresh dialog: when flag off, skip the radio and force
     `period_type = "Annual"` → admin refresh runs annual only.
3. **`app/pages/market_data.py`**
   - Import the flag.
   - `get_available_period_types(..., "forecasting")`: when flag off, return
     `["Annual"]` only.
   - Render gate (~3906): `_is_quarterly_fcst = QUARTERLY_FORECASTING_ENABLED and _period_type_db == "quarterly"` (defense-in-depth).
4. **`app/utils/forecast_auto_refresh.py`**
   - Import the flag.
   - `_run_cycle`: iterate `("annual",)` when flag off instead of
     `("annual", "quarterly")`.

## 5. Annual-safety argument

- Annual is a distinct radio/selectbox value; the local variable drives every
  downstream branch, so forcing it to `'Annual'` fully determines behavior.
- Annual uses different service methods and repository calls than quarterly.
- In the background job, the annual and quarterly cadences are separate loop
  iterations; removing `"quarterly"` from the tuple leaves the annual iteration
  byte-for-byte identical.
- No annual code path reads `QUARTERLY_FORECASTING_ENABLED`.

## 6. Testing plan (evidence-based, per CLAUDE.md)

Launch `bash .claude/dev/run_local.sh` (local OIDC bypass, staging DB, no STG writes).

1. `/forecasting`: Period toggle is gone; Annual cards/charts render; visiting
   `/forecasting?period_type=Quarterly` still renders Annual (deep-link neutralized).
2. `/market_data` Forecasting tab: Period Type offers Annual only; Annual
   forecast table renders.
3. Background gate: import-level probe asserting `_run_cycle`'s period tuple is
   `("annual",)` when the flag is off (no scheduler run needed).
4. Restore check: with the flag flipped to `True` in a probe, confirm the
   quarterly branch/period returns.

## 7. Rollback / restore

Set `QUARTERLY_FORECASTING_ENABLED = True` in `app/utils/constants.py`. All four
surfaces return to prior behavior immediately. No data migration, no cleanup.
