# Quarterly Revenue Forecasting — Design Spec

**Date:** 2026-06-27
**Author:** Engineering (with Mohd Saeed Afri)
**Status:** Approved — proceeding to implementation plan
**Scope:** Standalone `/forecasting` page, `market_data` Forecasting tab, the
Refresh Forecasting Models dialog, and `market_data` Key Stats forward (F-) columns.

---

## 1. Goal

Add **quarterly** revenue forecasting alongside the existing **annual** forecasting,
built the same way the annual stack was built, with:

- A period-type filter (Annual / Quarterly) on the standalone `/forecasting` page
  and the `market_data` Forecasting tab.
- Quarterly support in the Refresh Forecasting Models dialog (Refresh Now /
  Refresh All), including a **per-quarter** reporting date (not Q4-only).
- Quarterly forward (F-) columns in the Key Stats tab.

Hard constraints from the request:

- **No regression** to the annual paths.
- Fastest possible data retrieval (mirror the annual DB fast-path + indexes).
- Human-readable, real-developer code — no notebook scaffolding, no AI-looking
  one-off helpers.
- Reporting date must use the **correct quarter** the forecast is based on.

---

## 2. Background — how Annual forecasting works today

| Layer | File | Responsibility |
|-------|------|----------------|
| Engine | `app/utils/retailer_forecaster.py` | `RetailerForecaster`: 6 models + ensemble + scenarios; `from_dataframe()` runs on DB actuals. |
| Service (standalone page) | `app/data/revenue_forecast_service.py` | `RevenueForecastService.get_company_dashboard()` — load annual actuals → DB fast-path via store → engine on miss → fire-and-forget upsert. |
| Store (write-through cache) | `app/data/forecast_store.py` | `needs_update / upsert_forecasts / get_all_model_forecasts / get_forecasts`, writes `coreiq_model_forecasts`. |
| Admin sync | `app/data/forecast_admin_service.py` | `sync_forecast_for_ticker / sync_all_eligible`. |
| Refresh popup data | `app/data/forecast_refresh_service.py` | `get_refresh_table_data(period_type)` (already has a `period_type` param that returns `[]` for quarterly), `_annual_q4_report_dates_bulk()` (Q4-only), email. |
| Repository (tab + Key Stats) | `app/data/repository.py` | `ModelForecastsRepository.get_forecasts_data()` (forecasting tab); `KeyStatsRepository.get_key_stats_data()` F- block (line ~4258, gated `period_type == 'annual'`). |
| UI — standalone | `app/pages/forecasting.py` | Overview / Models / Test tabs; refresh dialog already shows an Annual/Quarterly radio that says "not yet supported". |
| UI — embedded | `app/pages/market_data.py` | Forecasting tab; `get_available_period_types("forecasting")` returns `["Annual"]` only. |

**DB table `coreiq_model_forecasts`** is annual-only:
unique key `uq_tcmm (ticker, fiscal_year, metric, model_key)`, `forecast_date` =
fiscal-year-end date. No quarter dimension.

**Data scientist's quarterly engine** (`docs/CIQ_Quarterly_Estimates.ipynb.aspx`,
class `RetailerQuarterlyForecaster`): same shape as the annual engine plus seasonal
decomposition; 7 methods (`linear, cagr, exp_smoothing, holt, ma_trend,
weighted_avg, seasonal_naive`); 20-quarter (5-year) horizon; backtest holdout 4
quarters; ensemble = top-3 by MAPE; pessimistic/baseline/optimistic scenarios.
Currently loads from Excel and prints/plots.

### STG data availability (live probe, 2026-06-27)

- SEC quarterly revenue (`coreiq_av_financials_income_statement`,
  `report_type='quarterly'`): **271 tickers / 18,866 rows** — most companies have
  25–83 quarters. ✅
- YF quarterly (`coreiq_yf_financials_income_statement`, `frequency='quarterly'`):
  only **13 tickers** — sparse; quarterly will mostly serve SEC companies.
- `coreiq_nasdaq_earnings_calendar.fiscal_quarter_ending` carries per-quarter labels
  (e.g. `Jun/2026, Mar/2026, Dec/2025, Sep/2025`) → the correct per-quarter
  reporting date **is** derivable.

---

## 3. Design decisions (locked)

1. **Storage:** a **separate** table `coreiq_model_forecasts_quarterly` mirroring the
   annual schema + a `fiscal_quarter` column. The annual table, queries, and indexes
   are never touched → zero annual regression.
2. **Embedded tab horizon:** the `market_data` Forecasting tab shows the **next 8
   quarters (2 years)**; the standalone `/forecasting` page and Excel export show the
   full **20 quarters**.
3. **Surface area:** standalone page + Forecasting tab **and** the Key Stats forward
   (F-) columns get quarterly support.
4. **Models:** include `seasonal_naive` (the DS model's 7th method) — it is the
   seasonally-appropriate addition for quarterly.

---

## 4. Architecture — parallel quarterly stack

Every quarterly unit is a sibling of an existing annual unit, sharing low-level
helpers but never mutating the annual code path.

### 4.1 New table — `coreiq_model_forecasts_quarterly`

```
ticker            VARCHAR(20)   NOT NULL
fiscal_year       SMALLINT      NOT NULL
fiscal_quarter    TINYINT       NOT NULL          -- 1..4   (NEW vs annual)
forecast_date     DATE          NULL              -- quarter-END date
metric            VARCHAR(80)   NOT NULL DEFAULT 'total_revenue'
model_key         VARCHAR(40)   NOT NULL
value_millions    DECIMAL(16,4) NULL
is_best_model     TINYINT(1)    NOT NULL DEFAULT 0
is_ensemble       TINYINT(1)    NOT NULL DEFAULT 0
mape              DECIMAL(8,4)  NULL
periods_ahead     TINYINT UNSIGNED NOT NULL       -- 1..20 quarters ahead
computed_at       DATETIME      NOT NULL
last_actual_date  DATE          NULL              -- max quarter-end used at compute
company_name      VARCHAR(255)  NULL
exchange          VARCHAR(100)  NULL

UNIQUE KEY uq_tcqmm (ticker, fiscal_year, fiscal_quarter, metric, model_key)
KEY idx_ticker_metric_period (ticker, metric, fiscal_year, fiscal_quarter)
KEY idx_ticker_ensemble (ticker, is_ensemble, fiscal_year, fiscal_quarter)
KEY idx_ticker_computed (ticker, computed_at)
KEY idx_best_metric_ticker (metric, is_best_model, ticker)
KEY idx_null_company (company_name(1))
```

Created idempotently by `ensure_quarterly_forecast_table()` (`CREATE TABLE IF NOT
EXISTS`) called at warmup next to `ensure_forecast_columns()`.

### 4.2 New engine — `app/utils/retailer_quarterly_forecaster.py`

`RetailerQuarterlyForecaster`, adapted from the DS notebook to match
`RetailerForecaster` conventions:

- Add `from_dataframe(df)` accepting columns `year, quarter, sales` (mirrors the
  annual engine's classmethod constructor).
- Keep seasonal decomposition, the 7 forecast methods, `backtest(holdout_quarters=4)`,
  `forecast(periods=20)`, ensemble (top-3 by MAPE), and scenarios.
- Remove Excel loading, matplotlib plotting, and all `print()` — the engine is
  pure dataframe-in / dataframe-out so it composes with the service layer.
- `forecast()` returns a frame with: `fiscal_year, fiscal_quarter, label` plus one
  column per model + `ensemble` + `scenario_*`.

### 4.3 New store — `app/data/quarterly_forecast_store.py`

Sibling of `forecast_store.py`, keyed by year + quarter, writing the quarterly table:
`needs_update`, `upsert_forecasts`, `get_all_model_forecasts`, `get_forecasts`,
`delete_forecasts_beyond_horizon`. Same staleness rule (`last_actual_date` vs latest
quarter-end) and the same single multi-row `INSERT … ON DUPLICATE KEY UPDATE`.

### 4.4 Service — `revenue_forecast_service.py`

Add a parallel `get_quarterly_dashboard(ticker, periods=20)` (the annual
`get_company_dashboard` is left exactly as-is):

- `_fetch_quarterly_rows` — SEC `report_type='quarterly'` / YF
  `frequency='quarterly'` (composite-ticker aware, same as annual).
- `_normalize_quarterly_rows` — derive `year, quarter, qidx, label, period_date`
  (quarter-end), dedup per quarter, drop sub-10%-of-peak partial periods.
- Run the quarterly engine → DB fast-path via `quarterly_forecast_store` → on a
  full run, fire-and-forget upsert. Reuses `_to_date`, `_safe_float`,
  `_yf_reported_currency`.

### 4.5 Admin / Refresh

- `forecast_admin_service.py`: `sync_quarterly_forecast_for_ticker` /
  `sync_all_eligible_quarterly` (need ≥ ~8 quarters of data).
- `forecast_refresh_service.py`:
  - Implement the `get_refresh_table_data("quarterly")` branch (currently `[]`):
    `last_refresh` + `fiscal_period` from the quarterly table; reporting date from
    the new bulk helper below.
  - **`_quarterly_report_dates_bulk()`** — the per-quarter reporting date. For each
    ticker pick the nearest upcoming earnings date of **any** fiscal quarter
    (≥ today, else most recent past), carrying its real `fiscal_quarter_ending`
    (e.g. `"Mar/2026"`). NASDAQ path keys on `ticker`; YF path keys on the composite
    `yf_symbol` (never borrow a US base ticker's date). This is the same dedup logic
    as `_annual_q4_report_dates_bulk()` **without** the Q4 filter.
  - "Fiscal Period" column = the quarter-end the forecast actually used
    (quarterly table `last_actual_date`).
  - Quarterly-aware notification email (reporting date = the quarter's date and its
    fiscal quarter label).

### 4.6 Repository — `repository.py`

- `ModelForecastsRepository`: add `get_quarterly_forecasts_data`,
  `get_quarterly_date_range`, `get_quarterly_available_dates` reading the quarterly
  table (year + quarter), with `"Q{q} {year}"` period labels.
- `KeyStatsRepository.get_key_stats_data`: add a `period_type == 'quarterly'` F- block
  mirroring the annual block at ~line 4258 — read quarterly `ensemble`, append the
  **next 8 quarters** as forward periods labeled `"3 Months\n<quarter-end>"`,
  reusing the existing `_add_quarters` helper. The annual block is unchanged.

### 4.7 UI

- **`forecasting.py`** (standalone): add an Annual/Quarterly period filter. Quarterly
  view → `get_quarterly_dashboard`, 20-quarter horizon, `"Q1 2026"` labels across
  overview chart, model charts, tables, and the Excel export. Refresh dialog:
  replace "not yet supported" with the real quarterly table — Reporting Date = the
  quarter's date, Fiscal Period = the quarter-end used.
- **`market_data.py`** (embedded): `get_available_period_types("forecasting")` →
  `["Annual","Quarterly"]` when quarterly data exists; Forecasting tab renders the
  **next 8 quarters** with quarterly column headers; the date-range branch
  (line ~1896) is period-aware via the new repository methods.

---

## 5. Reporting-date semantics (explicit)

| Context | Annual (today) | Quarterly (new) |
|---------|----------------|-----------------|
| Refresh "Reporting Date" | next/last **Q4** earnings date (`_annual_q4_report_dates_bulk`) | next/last earnings date of **the relevant fiscal quarter** (`_quarterly_report_dates_bulk`) |
| Refresh "Fiscal Period" | annual fiscal year-end pattern (`Feb 28`) | the **quarter-end** the forecast used (`Mar 31`, `Jun 30`, …) |
| Forecast `forecast_date` | fiscal-year-end of the forecast year | **quarter-end** of the forecast quarter |
| Key Stats F- period label | `12 Months\n<fy-end>` | `3 Months\n<quarter-end>` |

---

## 6. Testing & verification

Per repo workflow standards — evidence, not assertions.

1. **STG probes** (read-only): confirm `get_quarterly_dashboard` produces sane
   20-quarter forecasts for SEC tickers (AAPL, COST, WMT) and at least one YF
   ticker if available; confirm `_quarterly_report_dates_bulk` returns the correct
   fiscal quarter per ticker.
2. **Table migration**: run `ensure_quarterly_forecast_table()` against STG; verify
   schema + indexes; seed a handful of tickers via `sync_quarterly_forecast_for_ticker`.
3. **Playwright UI** via `.claude/dev/run_local.sh` + `ui_test.py`:
   - `/forecasting` quarterly view (charts, tables, Excel).
   - `market_data` Forecasting tab → Quarterly → next 8 quarters.
   - Refresh dialog → Quarterly → Reporting Date shows the correct quarter; Refresh
     Now writes a row.
   - Key Stats → Quarterly → forward F- quarters present and labeled `3 Months`.
4. **Regression**: re-verify the annual paths (standalone page, tab, refresh, Key
   Stats F-) are byte-for-byte unchanged in behavior.

---

## 7. Rollout

1. Ship code (table auto-creates at warmup).
2. Backfill quarterly forecasts via **Refresh All (Quarterly)** in the dialog —
   same way the annual table was seeded.
3. Quarterly becomes selectable wherever quarterly forecasts exist.

---

## 8. Out of scope

- Metrics other than `total_revenue`.
- Changing the annual engine, annual table, or annual queries.
- Forecasting for companies with fewer than the minimum required quarters.
