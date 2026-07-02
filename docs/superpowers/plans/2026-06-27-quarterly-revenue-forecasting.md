# Quarterly Revenue Forecasting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add quarterly revenue forecasting alongside the existing annual stack — standalone `/forecasting` page, `market_data` Forecasting tab, the Refresh dialog, and Key Stats forward (F-) columns — with zero annual regression.

**Architecture:** A parallel quarterly stack that mirrors each annual unit (engine → store → service → admin/refresh → repository → UI). Quarterly forecasts live in a dedicated `coreiq_model_forecasts_quarterly` table so the annual table, queries, and indexes are never touched. The data scientist's `RetailerQuarterlyForecaster` (seasonal decomposition + 7 models + ensemble + scenarios) is adapted to a dataframe-in/out engine matching `RetailerForecaster`'s conventions.

**Tech Stack:** Python 3, Streamlit, SQLAlchemy 2 (raw SQL via `db_manager`), MySQL (Azure), pandas/numpy/scipy, Plotly, openpyxl, Playwright (UI verification), pytest (engine unit tests).

## Global Constraints

- **NO git commits / pushes** anywhere, ever, unless the user types the exact words "please commit"/"please push". The "Checkpoint" step in each task means *run verification and stop for review* — it does **NOT** mean commit.
- **No regression to annual paths** — never edit the annual engine, `coreiq_model_forecasts`, or any annual query/branch. Quarterly is additive.
- **Logging:** import only from `utils.server_logger` (`log_structured_error`, `log_timing`, `log_info`). Standard-library `logging` is forbidden. Every `except` calls `log_structured_error(exc, page=..., component=..., operation=...)`.
- **DB access:** reads via `db_manager.execute_query_readonly` / `fetch_one` (1 RTT, AUTOCOMMIT); writes via `db_manager.execute_insert` / `execute_update`. Never add a separate SELECT for ACL.
- **Metric scope:** `total_revenue` only.
- **Quarterly horizon:** 20 quarters on the standalone page + Excel; **next 8 quarters** in the `market_data` Forecasting tab and Key Stats F- columns.
- **Models:** `linear, cagr, exp_smoothing, holt, ma_trend, weighted_avg, seasonal_naive` + `ensemble` + `scenario_{pessimistic,baseline,optimistic}`.
- **Human-readable code** — no notebook scaffolding, no `print`, no matplotlib. Match the surrounding file's naming and structure.
- **Verification harness:** `bash .claude/dev/run_local.sh` (port 8501, OIDC bypass, STG DB) + `.venv/bin/python .claude/dev/ui_test.py …`. STG probes use the `.env` `STG_DB_*` + `DigiCertGlobalRootG2.crt.pem` SSL CA (see Task 2 helper).

---

## File Structure

**Create:**
- `app/utils/retailer_quarterly_forecaster.py` — `RetailerQuarterlyForecaster` engine (pure, dataframe-in/out).
- `app/data/quarterly_forecast_store.py` — write-through cache for `coreiq_model_forecasts_quarterly` + `ensure_quarterly_forecast_table()`.
- `tests/test_retailer_quarterly_forecaster.py` — engine unit tests (pytest).
- `scripts/probe_quarterly_forecast.py` — reusable STG probe used across tasks 3–7.

**Modify:**
- `app/data/revenue_forecast_service.py` — add `get_quarterly_dashboard()` + quarterly fetch/normalize helpers.
- `app/data/forecast_admin_service.py` — add `sync_quarterly_forecast_for_ticker` / `sync_all_eligible_quarterly`.
- `app/data/forecast_refresh_service.py` — `_quarterly_report_dates_bulk()`, implement `get_refresh_table_data("quarterly")`, quarterly email.
- `app/data/repository.py` — `ModelForecastsRepository` quarterly methods; `KeyStatsRepository.get_key_stats_data` quarterly F- block.
- `app/pages/forecasting.py` — Annual/Quarterly period filter, quarterly rendering, refresh-dialog quarterly branch, `ensure_quarterly_forecast_table()` call.
- `app/pages/market_data.py` — `get_available_period_types("forecasting")`, Forecasting tab quarterly render, date-range branch.

---

## Task 1: Create the quarterly DB table + idempotent migration

**Files:**
- Create: `app/data/quarterly_forecast_store.py` (only `ensure_quarterly_forecast_table()` in this task)
- Create: `scripts/probe_quarterly_forecast.py`

**Interfaces:**
- Produces: `ensure_quarterly_forecast_table() -> None` — `CREATE TABLE IF NOT EXISTS coreiq_model_forecasts_quarterly` with the schema below. Idempotent.
- Produces: `scripts/probe_quarterly_forecast.py` exporting `connect()` returning a `pymysql` connection to STG (used by later tasks).

- [ ] **Step 1: Write the STG probe helper**

Create `scripts/probe_quarterly_forecast.py`:

```python
"""Reusable STG probe for quarterly-forecast development. Read-mostly.
Usage: .venv/bin/python scripts/probe_quarterly_forecast.py <check>
"""
import os, sys
import pymysql
from dotenv import dotenv_values


def connect():
    v = dotenv_values(".env")
    return pymysql.connect(
        host=v["STG_DB_HOST"], port=int(v.get("STG_DB_PORT") or 3306),
        user=v["STG_DB_USER"], password=v["STG_DB_PASSWORD"], db=v["STG_DB_NAME"],
        ssl={"ca": os.path.join(os.getcwd(), "DigiCertGlobalRootG2.crt.pem")},
        cursorclass=pymysql.cursors.DictCursor,
    )


def show_schema():
    with connect() as c, c.cursor() as cur:
        cur.execute("SHOW CREATE TABLE coreiq_model_forecasts_quarterly")
        print(cur.fetchone()["Create Table"])


if __name__ == "__main__":
    {"schema": show_schema}.get(sys.argv[1] if len(sys.argv) > 1 else "schema", show_schema)()
```

- [ ] **Step 2: Write `ensure_quarterly_forecast_table()`**

Create `app/data/quarterly_forecast_store.py` with the migration function:

```python
"""
quarterly_forecast_store.py
===========================
Write-through cache between the in-memory quarterly forecasting engine and the
staging DB table `coreiq_model_forecasts_quarterly`. Sibling of forecast_store.py,
keyed by (ticker, fiscal_year, fiscal_quarter, metric, model_key).
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from core.database import db_manager
from utils.server_logger import log_structured_error, log_info

_METRIC_TOTAL_REVENUE = "total_revenue"
_ENSEMBLE_KEY = "ensemble"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS coreiq_model_forecasts_quarterly (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    ticker           VARCHAR(20)  NOT NULL,
    fiscal_year      SMALLINT     NOT NULL,
    fiscal_quarter   TINYINT      NOT NULL,
    forecast_date    DATE         DEFAULT NULL,
    metric           VARCHAR(80)  NOT NULL DEFAULT 'total_revenue',
    model_key        VARCHAR(40)  NOT NULL,
    value_millions   DECIMAL(16,4) DEFAULT NULL,
    is_best_model    TINYINT(1)   NOT NULL DEFAULT 0,
    is_ensemble      TINYINT(1)   NOT NULL DEFAULT 0,
    mape             DECIMAL(8,4) DEFAULT NULL,
    periods_ahead    TINYINT UNSIGNED NOT NULL,
    computed_at      DATETIME     NOT NULL,
    last_actual_date DATE         DEFAULT NULL
        COMMENT 'Max quarter-end used when this forecast was computed',
    company_name     VARCHAR(255) DEFAULT NULL,
    exchange         VARCHAR(100) DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_tcqmm (ticker, fiscal_year, fiscal_quarter, metric, model_key),
    KEY idx_ticker_metric_period (ticker, metric, fiscal_year, fiscal_quarter),
    KEY idx_ticker_ensemble (ticker, is_ensemble, fiscal_year, fiscal_quarter),
    KEY idx_ticker_computed (ticker, computed_at),
    KEY idx_best_metric_ticker (metric, is_best_model, ticker),
    KEY idx_null_company (company_name(1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  ROW_FORMAT=COMPRESSED
  COMMENT='Model-generated quarterly revenue forecasts (20-quarter horizon)';
"""


def ensure_quarterly_forecast_table() -> None:
    """Create coreiq_model_forecasts_quarterly if it does not yet exist (idempotent)."""
    try:
        db_manager.execute_insert(_CREATE_TABLE_SQL, {})
        log_info("[quarterly_forecast_store] ensured coreiq_model_forecasts_quarterly")
    except Exception as exc:
        log_structured_error(
            exc, page="quarterly_forecast_store",
            component="ensure_quarterly_forecast_table", operation="CREATE_TABLE",
        )
```

- [ ] **Step 3: Run the migration against STG**

Run:
```bash
.venv/bin/python -c "import sys; sys.path.insert(0,'app'); \
from data.quarterly_forecast_store import ensure_quarterly_forecast_table as e; \
import os; os.environ.update(__import__('dotenv').dotenv_values('.env')); e()"
```
(If the app's `db_manager` needs the LOCAL→STG env wiring, instead run the table-create SQL directly through `scripts/probe_quarterly_forecast.py` by adding a one-off `create()` that executes `_CREATE_TABLE_SQL`.)

Expected: no error logged.

- [ ] **Step 4: Verify schema on STG**

Run: `.venv/bin/python scripts/probe_quarterly_forecast.py schema`
Expected: prints `CREATE TABLE coreiq_model_forecasts_quarterly …` including `uq_tcqmm (ticker, fiscal_year, fiscal_quarter, metric, model_key)` and the five secondary indexes.

- [ ] **Step 5: Checkpoint** — verification passed; stop for review. Do **not** commit.

---

## Task 2: Quarterly forecasting engine (`RetailerQuarterlyForecaster`)

**Files:**
- Create: `app/utils/retailer_quarterly_forecaster.py`
- Test: `tests/test_retailer_quarterly_forecaster.py`

**Interfaces:**
- Produces: `RetailerQuarterlyForecaster.from_dataframe(df: pd.DataFrame) -> RetailerQuarterlyForecaster` where `df` has columns `year, quarter, sales`.
- Produces: `.backtest(holdout_quarters: int = 4) -> pd.DataFrame` (columns `method, mape, bias, rmse`); sets `.best_method: str`.
- Produces: `.forecast(periods: int = 20) -> pd.DataFrame` with columns `fiscal_year, fiscal_quarter, label, <7 model keys>, ensemble, scenario_pessimistic, scenario_baseline, scenario_optimistic`.
- Produces: `.summary_stats() -> dict`, `.clean_data: pd.DataFrame`, `.outliers: list`, `.forecasts: dict`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_retailer_quarterly_forecaster.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import numpy as np
import pandas as pd
import pytest

from utils.retailer_quarterly_forecaster import RetailerQuarterlyForecaster


def _seasonal_series(n_quarters=24, base=100.0, growth=0.03,
                     seasonal=(0.9, 1.0, 1.05, 1.25)):
    rows = []
    val = base
    year, q = 2018, 1
    for i in range(n_quarters):
        rows.append({"year": year, "quarter": q, "sales": val * seasonal[q - 1]})
        val *= (1 + growth)
        q += 1
        if q > 4:
            q = 1
            year += 1
    return pd.DataFrame(rows)


def test_from_dataframe_builds_clean_quarters():
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    assert list(eng.clean_data.columns).count("qidx") == 1
    assert len(eng.clean_data) == 24
    assert eng.clean_data["quarter"].between(1, 4).all()


def test_forecast_returns_20_quarters_with_required_columns():
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    eng.backtest(holdout_quarters=4)
    fc = eng.forecast(periods=20)
    assert len(fc) == 20
    for col in ("fiscal_year", "fiscal_quarter", "label", "ensemble",
                "scenario_pessimistic", "scenario_baseline", "scenario_optimistic",
                "seasonal_naive"):
        assert col in fc.columns
    assert fc["fiscal_quarter"].between(1, 4).all()
    # Forecast continues after the last actual quarter
    assert fc["fiscal_year"].iloc[0] >= eng.clean_data["year"].iloc[-1]


def test_forecast_preserves_seasonality_direction():
    # Q4 (index 4) is the strongest quarter in the synthetic series;
    # the engine's seasonal indices should rank Q4 highest.
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    assert eng.seasonal_indices[3] == max(eng.seasonal_indices)


def test_backtest_picks_a_best_method():
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    bt = eng.backtest(holdout_quarters=4)
    assert not bt.empty
    assert eng.best_method in {"linear", "cagr", "exp_smoothing", "holt",
                               "ma_trend", "weighted_avg", "seasonal_naive"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_retailer_quarterly_forecaster.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'utils.retailer_quarterly_forecaster'`.

- [ ] **Step 3: Implement the engine**

Create `app/utils/retailer_quarterly_forecaster.py` by porting the notebook class
(`docs/CIQ_Quarterly_Estimates.ipynb.aspx`) to codebase conventions. Required changes:

- Class docstring describing annual→quarterly parity; drop the Excel/plot usage notes.
- Replace `__init__(file_path)` + `load_data` with the dataframe path only:

```python
class RetailerQuarterlyForecaster:
    """Quarterly revenue forecasting for a single retailer (seasonal)."""

    def _initialize_state(self) -> None:
        self.raw_data = None
        self.clean_data = None
        self.data_excl_outliers = None
        self.outliers = []
        self.seasonal_indices = np.ones(4)
        self.forecasts = {}
        self.backtest_results = None
        self.best_method = None
        self.final_forecast = None

    @classmethod
    def from_dataframe(cls, dataframe: pd.DataFrame) -> "RetailerQuarterlyForecaster":
        obj = cls.__new__(cls)
        obj._initialize_state()
        obj.raw_data = dataframe.copy()
        obj._prepare_data()
        return obj
```

- `_prepare_data()`: accept a frame that already has `year, quarter, sales`
  (built by the service). Compute `qidx = year*4 + quarter` and
  `label = f"Q{quarter} {year}"`, dedup per `qidx`, sort, then call
  `_compute_seasonal_indices()` and `_detect_outliers()` exactly as the notebook does.
- Keep `_compute_seasonal_indices`, `_deseasonalise`, `_reseasonalise`,
  `_detect_outliers`, `summary_stats`, the 7 `_forecast_*` methods, `backtest`,
  and `forecast` from the notebook **verbatim in logic**, but:
  - Remove every `print(...)`.
  - In `forecast()`, replace the single `quarter` output column with explicit
    `fiscal_year` + `fiscal_quarter` columns derived from `qidx`
    (`fiscal_year = qidx // 4` when `qidx % 4 != 0` else `qidx // 4 - 1`;
    `fiscal_quarter = qidx % 4 or 4`), and keep `label`. Drop `qidx` from the
    returned frame.
  - Default `forecast(periods=20)` and `backtest(holdout_quarters=4)`.
- Do not include `export_results`, `plot`, `run_full_analysis`, or `__main__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_retailer_quarterly_forecaster.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Checkpoint** — stop for review. Do **not** commit.

---

## Task 3: Quarterly store (write-through cache)

**Files:**
- Modify: `app/data/quarterly_forecast_store.py`
- Modify: `scripts/probe_quarterly_forecast.py` (add a `roundtrip` check)

**Interfaces:**
- Consumes: `ensure_quarterly_forecast_table` (Task 1), `RetailerQuarterlyForecaster.forecast` frame (Task 2).
- Produces:
  - `needs_update(ticker, last_actual_date: datetime.date, metric=...) -> bool`
  - `upsert_forecasts(ticker, forecast_df, model_keys, best_method_key, backtest_rows, metric='total_revenue', last_actual_date=None, company_name=None, exchange=None) -> int`
  - `get_all_model_forecasts(ticker, metric='total_revenue', max_periods=20) -> Dict[str, List[Dict]]`
  - `get_forecasts(ticker, metric='total_revenue', model_key='ensemble', max_periods=8) -> List[Dict]`

- [ ] **Step 1: Implement the store functions**

Mirror `app/data/forecast_store.py` exactly, with these deltas (write the full module):

- `upsert_forecasts`: the `forecast_df` carries `fiscal_year` + `fiscal_quarter`;
  build one row per `(model_key, fiscal_year, fiscal_quarter)`. `forecast_date`
  = the quarter-end date for that row (`forecast_df` provides `forecast_date`, or
  compute the last day of the quarter from `fiscal_year, fiscal_quarter`).
  `periods_ahead` = ordinal position within the forecast (1..20).
  INSERT columns add `fiscal_quarter`; `ON DUPLICATE KEY UPDATE` matches `uq_tcqmm`.
- `needs_update`: same `MAX(last_actual_date)` staleness query, table swapped. Omit
  the annual `_DEDUP_FIX_DATE` special case (new table — no legacy rows).
- `get_all_model_forecasts`: derived-table join keyed on `(fiscal_year, fiscal_quarter)`
  ordered ascending, `LIMIT :max_periods`.
- `get_forecasts`: `ORDER BY fiscal_year, fiscal_quarter ASC LIMIT :max_periods`.

- [ ] **Step 2: Add the roundtrip probe**

In `scripts/probe_quarterly_forecast.py` add a `roundtrip()` that: builds a tiny
synthetic 24-quarter frame, runs `RetailerQuarterlyForecaster`, calls
`upsert_forecasts(ticker="ZZTEST", …)`, reads back via `get_all_model_forecasts`,
prints the row count per model, then `DELETE FROM coreiq_model_forecasts_quarterly
WHERE ticker='ZZTEST'`.

- [ ] **Step 3: Run the roundtrip probe**

Run: `.venv/bin/python scripts/probe_quarterly_forecast.py roundtrip`
Expected: prints `ensemble: 20 rows` (and similar per model); ends with `cleaned ZZTEST`.

- [ ] **Step 4: Verify no orphan rows remain**

Run: `.venv/bin/python -c "from scripts.probe_quarterly_forecast import connect; c=connect(); cur=c.cursor(); cur.execute(\"SELECT COUNT(*) n FROM coreiq_model_forecasts_quarterly WHERE ticker='ZZTEST'\"); print(cur.fetchone())"`
Expected: `{'n': 0}`.

- [ ] **Step 5: Checkpoint** — stop for review. Do **not** commit.

---

## Task 4: Service — `get_quarterly_dashboard()`

**Files:**
- Modify: `app/data/revenue_forecast_service.py`

**Interfaces:**
- Consumes: store funcs (Task 3), `RetailerQuarterlyForecaster` (Task 2), existing `_to_date`, `_safe_float`, `_yf_reported_currency`, `get_company_source`.
- Produces: `RevenueForecastService.get_quarterly_dashboard(ticker: str, periods: int = 20) -> Dict[str, Any]` returning the same shape as `get_company_dashboard` plus per-row `fiscal_quarter` and `label` in `forecast_rows`, and `actual_rows` carrying `year, quarter, period_date` (quarter-end).
- Produces: staticmethods `_fetch_quarterly_rows(ticker, source)`, `_normalize_quarterly_rows(raw_rows, source, ...)`.

- [ ] **Step 1: Add quarterly fetch + normalize helpers**

In `RevenueForecastService`, add (mirroring `_fetch_actual_rows` / `_normalize_actual_rows`):

```python
@staticmethod
def _fetch_quarterly_rows(ticker: str, source: str) -> List[Dict[str, Any]]:
    if source == "SEC":
        base = ticker.split('.')[0] if '.' in ticker else ticker
        return db_manager.execute_query_readonly(
            f"""SELECT DISTINCT fiscal_date_ending AS period_date, total_revenue,
                       reported_currency
                FROM {SEC_SOURCE_TABLE}
                WHERE ticker = :ticker AND report_type = 'quarterly'
                  AND total_revenue IS NOT NULL
                ORDER BY fiscal_date_ending ASC""",
            {"ticker": base})
    col = "yf_symbol" if '.' in ticker else "ticker"
    return db_manager.execute_query_readonly(
        f"""SELECT period_end AS period_date,
                   MAX(CASE WHEN line_item='Total Revenue' THEN value END) AS total_revenue
            FROM {YF_SOURCE_TABLE}
            WHERE {col} = :ticker AND frequency = 'quarterly'
            GROUP BY period_end ORDER BY period_end ASC""",
        {"ticker": ticker})
```

`_normalize_quarterly_rows`: for each row derive `period_date` (`_to_date`),
`year = period_date.year`, `quarter = (period_date.month - 1)//3 + 1`,
`total_revenue_billions = revenue / 1e9`, `label = f"Q{quarter} {year}"`. Drop rows
with null revenue/date.

- [ ] **Step 2: Add `get_quarterly_dashboard`**

Mirror `get_company_dashboard`'s structure (load → `_raw_df` with
`year,quarter,sales` → dedup per `(year,quarter)` → drop sub-10%-of-peak →
`RetailerQuarterlyForecaster.from_dataframe` → DB fast-path via
`quarterly_forecast_store` → engine on miss → fire-and-forget upsert in a daemon
thread). Decorate with `@st.cache_data(ttl=86400*7, show_spinner=False)`. Build
`forecast_rows` from the engine frame (includes `fiscal_year, fiscal_quarter, label`).
The fast-path reconstructs the frame from `get_all_model_forecasts` keyed on
`(fiscal_year, fiscal_quarter)`.

- [ ] **Step 3: Probe on a real SEC ticker**

Add a `dashboard(ticker)` check to `scripts/probe_quarterly_forecast.py` that calls
`get_quarterly_dashboard` under the app's import path and prints the first 8
`forecast_rows` (`label`, `ensemble`).

Run: `.venv/bin/python scripts/probe_quarterly_forecast.py dashboard AAPL`
Expected: 20 forecast rows; labels like `Q3 2026, Q4 2026, …`; monotonic ensemble values in a sane revenue range (~$90B–$180B/quarter).

- [ ] **Step 4: Verify the fast-path served from store on second call**

Re-run the same probe. Expected: timings log shows `served_from_store=True` (or no upsert thread spawned) on the second call.

- [ ] **Step 5: Checkpoint** — stop for review. Do **not** commit.

---

## Task 5: Admin sync (Refresh Now / Refresh All backend)

**Files:**
- Modify: `app/data/forecast_admin_service.py`

**Interfaces:**
- Consumes: `get_quarterly_dashboard` helpers, `quarterly_forecast_store.{needs_update,upsert_forecasts,ensure_quarterly_forecast_table}`, `RetailerQuarterlyForecaster`.
- Produces:
  - `sync_quarterly_forecast_for_ticker(display_ticker, *, force=False, periods=20) -> Dict[str, Any]` (same result-dict shape: `ticker,status,message,rows_upserted,store_ticker,last_actual_date,company_name,exchange`).
  - `sync_all_eligible_quarterly(*, force=False, periods=20, progress_callback=None, exclude_tickers=None) -> List[Dict]`.

- [ ] **Step 1: Implement `sync_quarterly_forecast_for_ticker`**

Mirror `sync_forecast_for_ticker` but: call `ensure_quarterly_forecast_table()` once
at module entry; fetch via `_fetch_quarterly_rows` / `_normalize_quarterly_rows`;
require ≥ 8 quarters (status `skipped` otherwise); build the `year,quarter,sales`
frame; run `RetailerQuarterlyForecaster`; `backtest(holdout_quarters=4)`;
`forecast(periods=20)`; upsert via `quarterly_forecast_store.upsert_forecasts`.
`last_actual_date` = last quarter-end.

- [ ] **Step 2: Implement `sync_all_eligible_quarterly`**

Mirror `sync_all_eligible`, calling `sync_quarterly_forecast_for_ticker`.

- [ ] **Step 3: Probe — sync two tickers**

Add `sync(ticker)` to the probe (calls `sync_quarterly_forecast_for_ticker`).
Run: `.venv/bin/python scripts/probe_quarterly_forecast.py sync AAPL`
Then: `.venv/bin/python scripts/probe_quarterly_forecast.py sync COST`
Expected: `status=updated rows_upserted>0` for both.

- [ ] **Step 4: Verify rows landed**

Run a probe `SELECT model_key, COUNT(*) FROM coreiq_model_forecasts_quarterly WHERE ticker IN ('AAPL','COST') GROUP BY model_key`.
Expected: each of the 10 model keys present with 20 rows per ticker.

- [ ] **Step 5: Checkpoint** — stop for review. Do **not** commit.

---

## Task 6: Refresh service — per-quarter reporting dates + table data + email

**Files:**
- Modify: `app/data/forecast_refresh_service.py`

**Interfaces:**
- Consumes: `coreiq_model_forecasts_quarterly`, `coreiq_nasdaq_earnings_calendar`, `coreiq_yf_earnings_calendar`, `_fiscal_year_end_bulk`, `_yf_fqe_date_for_earnings_date`.
- Produces:
  - `_quarterly_report_dates_bulk() -> Dict[str, Dict[str, Any]]` — per ticker: `{date, fiscal_quarter_ending, source, fetched_at_utc, fqe_month, fye_month, fiscal_q}` for the **relevant** quarter (any of Q1–Q4).
  - `get_refresh_table_data("quarterly")` returns populated rows (was `[]`).

- [ ] **Step 1: Implement `_quarterly_report_dates_bulk()`**

Copy `_annual_q4_report_dates_bulk()` and **remove the Q4 filter** (`if fqe_m != fye_m: continue` and the YF `if fqe_date.month != fye_month: continue`). For each ticker keep all derivable fiscal-quarter rows, then select nearest upcoming `earnings_date >= today`, else most recent past. Carry the row's real `fiscal_quarter_ending` and compute `fiscal_q` via the documented formula (`fy_start=(fye%12)+1; months_into=(fqe-fy_start)%12+1; fiscal_q=(months_into+2)//3`). NASDAQ keys on `ticker`; YF keys on composite `yf_symbol`. Keep the 460-day staleness guard.

- [ ] **Step 2: Implement the `quarterly` branch of `get_refresh_table_data`**

Replace `if period_type.lower() == "quarterly": return []` with logic mirroring the
annual branch but reading `coreiq_model_forecasts_quarterly` for `MAX(computed_at)`
(last_refresh) and `MAX(last_actual_date)` (fiscal_period = quarter-end, formatted
`"%b %d"`), and using `_quarterly_report_dates_bulk()` for the Reporting Date.
Set `annual_reporting_quarter` debug field to the actual `fiscal_q` (`"Q{n}"`).
Keep the `@st.cache_data(ttl=120)` (the cache key already includes `period_type`).

- [ ] **Step 3: Make `send_model_refresh_email` quarterly-aware**

Add a `period_type: str = "annual"` param. When `"quarterly"`, use
`_quarterly_report_dates_bulk()` for `reporting_date`, label the column
"Reporting Date (Quarter)", and include the fiscal quarter in the row.

- [ ] **Step 4: Probe the reporting dates**

Add `qdates()` to the probe → prints `_quarterly_report_dates_bulk()` for AAPL, COST, WMT.
Run: `.venv/bin/python scripts/probe_quarterly_forecast.py qdates`
Expected: each ticker shows a near-term `date` with a matching `fiscal_quarter_ending`
(e.g. AAPL → `Jun/2026` or `Sep/2026`), and `fiscal_q` in 1..4 (not forced to 4).

- [ ] **Step 5: Probe the table data**

Add `qtable()` → prints first 5 rows of `get_refresh_table_data("quarterly")`.
Run: `.venv/bin/python scripts/probe_quarterly_forecast.py qtable`
Expected: AAPL/COST rows show non-`—` Reporting Date, a `Fiscal Period` like `Mar 31`/`Jun 30`, and a recent `last_refresh`.

- [ ] **Step 6: Checkpoint** — stop for review. Do **not** commit.

---

## Task 7: Repository — `ModelForecastsRepository` quarterly + Key Stats F- block

**Files:**
- Modify: `app/data/repository.py`

**Interfaces:**
- Produces on `ModelForecastsRepository`:
  - `get_quarterly_forecasts_data(ticker, max_quarters=8) -> Dict[str, Any]` — same shape as `get_forecasts_data` but `periods` are quarters (`{"label": "Q3 2026", "fiscal_year":…, "fiscal_quarter":…}`) and reads `coreiq_model_forecasts_quarterly`.
  - `get_quarterly_date_range(ticker)`, `get_quarterly_available_dates(ticker)`.
- Modifies: `KeyStatsRepository.get_key_stats_data` — adds a `period_type == 'quarterly'` forward F- block.

- [ ] **Step 1: Add quarterly methods to `ModelForecastsRepository`**

Mirror `get_forecasts_data` / `get_date_range` / `get_available_dates` but select
`fiscal_year, fiscal_quarter, model_key, value_millions, is_best_model, is_ensemble,
mape, last_actual_date` from the quarterly table, order by
`fiscal_year, fiscal_quarter`, build `periods` with `"Q{q} {y}"` labels, and limit
to the nearest `max_quarters` (default 8). Reuse `_resolve_forecast_ticker`,
`_MODEL_ORDER/_LABELS`, `_SCENARIO_ORDER/_LABELS`, and `_build_rows`.

- [ ] **Step 2: Add the quarterly F- block to `get_key_stats_data`**

Directly after the annual block (`if period_type == 'annual' and periods:` … at
~line 4259), add a sibling:

```python
# ── Forward F- model revenue forecast (quarterly, next 8 quarters) ──
if period_type == 'quarterly' and periods:
    _last_hist_date = _last_actual_hist_date
    _n_actual = len(results)
    _last_rev_mm = next(
        (l['values'][_n_actual - 1] for l in line_items
         if l['label'] == 'Total Revenue' and len(l['values']) >= _n_actual),
        None)
    if source == 'YFinance' and '.' not in ticker:
        _exch = (CompanyRepository.get_companies_map().get(ticker) or {}).get('exchange_acronym')
        _forecast_ticker = f"{ticker}.{_exch}" if _exch else ticker
    else:
        _forecast_ticker = ticker
    _fcst_rows = db_manager.execute_query_readonly(
        "SELECT fiscal_year, fiscal_quarter, forecast_date, value_millions "
        "FROM coreiq_model_forecasts_quarterly "
        "WHERE ticker = :ticker AND metric = 'total_revenue' AND model_key = 'ensemble' "
        "AND forecast_date > :last_date "
        "ORDER BY fiscal_year ASC, fiscal_quarter ASC LIMIT 8",
        {"ticker": _forecast_ticker, "last_date": _last_hist_date})
    _f_growth_rows = [l for l in line_items
                      if l['label'] == 'Growth Over Prior Year' and l.get('indent') == 1]
    _f_rev_growth_row = _f_growth_rows[0] if _f_growth_rows else None
    for _fr in _fcst_rows:
        _rvm = float(_fr['value_millions']) if _fr.get('value_millions') is not None else None
        _fdt = _fr.get('forecast_date') or _add_quarters(_last_hist_date, 1)
        periods.append(FiscalPeriod(
            date=_fdt, label=f"3 Months\n{_fdt.strftime('%b-%d-%Y')}", is_forecast=True))
        _rev_growth = ((_rvm - _last_rev_mm) / abs(_last_rev_mm) * 100) \
            if (_rvm is not None and _last_rev_mm not in (None, 0)) else None
        for _li in line_items:
            if _li['label'] == 'Total Revenue':
                _li['values'].append(_rvm)
            elif _li is _f_rev_growth_row:
                _li['values'].append(_rev_growth)
            else:
                _li['values'].append(None)
        if _rvm is not None:
            _last_rev_mm = _rvm
```

(`_add_quarters` is already defined earlier in this function.)

- [ ] **Step 3: Probe the repository methods**

Add `repo(ticker)` to the probe that imports `ModelForecastsRepository`, calls
`get_quarterly_forecasts_data('AAPL')`, prints `periods` labels + the ensemble row values.
Run: `.venv/bin/python scripts/probe_quarterly_forecast.py repo AAPL`
Expected: 8 quarter labels (`Q… 20…`), ensemble row populated.

- [ ] **Step 4: Checkpoint** — stop for review. Do **not** commit.

---

## Task 8: Standalone `/forecasting` page — period filter + quarterly rendering + refresh dialog

**Files:**
- Modify: `app/pages/forecasting.py`

**Interfaces:**
- Consumes: `get_quarterly_dashboard` (Task 4), `get_refresh_table_data("quarterly")` (Task 6), `sync_quarterly_forecast_for_ticker` / `sync_all_eligible_quarterly` (Task 5), `ensure_quarterly_forecast_table` (Task 1).

- [ ] **Step 1: Ensure the quarterly table at page load**

At `forecasting.py:2968` (next to `ensure_forecast_columns(); backfill_company_info()`),
import and call `ensure_quarterly_forecast_table()`.

- [ ] **Step 2: Add the Annual/Quarterly period filter**

Add a `st.radio`/segmented control (key `forecasting_period_type`, default `Annual`)
near the ticker selector. Branch the dashboard fetch: `Annual` →
`get_company_dashboard(ticker)`; `Quarterly` → `get_quarterly_dashboard(ticker)`.

- [ ] **Step 3: Make the renderers period-aware**

The chart/table/Excel builders currently key on `year`. Add a `period_type`/`label`
path so the x-axis and table headers use the quarterly `label` ("Q1 2026") and the
forecast frame's `fiscal_quarter`. The summary cards ("Forecast Horizon") read
"20 quarters" / "Through Q4 2030" when quarterly. Header card "Ensemble — Next" reads
the next-quarter ensemble. Keep the annual code path untouched — branch, don't replace.

- [ ] **Step 4: Wire the refresh dialog quarterly branch**

In `_refresh_dialog`, replace the `st.info("Quarterly forecast refresh is not yet
supported.")` block: when `period_type == "Quarterly"`, render the same table from
`get_refresh_table_data("quarterly")`, and route Refresh Now / Refresh All to
`sync_quarterly_forecast_for_ticker` / `sync_all_eligible_quarterly`. Column header
"Reporting Date" now shows the relevant quarter; "Fiscal Period" shows the quarter-end.

- [ ] **Step 5: UI verification (Playwright)**

Start: `bash .claude/dev/run_local.sh` (separate shell).
Run:
```bash
.venv/bin/python .claude/dev/ui_test.py --path "/forecasting?ticker=AAPL" --shot /tmp/fc_q.png --chars 2500
```
Then drive the period filter to Quarterly (click the radio label) and re-snapshot; and:
```bash
.venv/bin/python .claude/dev/ui_test.py --path "/forecasting?ticker=AAPL" \
   --click "Refresh Data" --wait-text "Reporting Date" --dialog --shot /tmp/fc_refresh.png
```
Expected: quarterly view shows "Q… 20…" labels and a 20-quarter horizon; refresh dialog Quarterly tab lists tickers with a per-quarter Reporting Date. Read both screenshots; show the user.

- [ ] **Step 6: Checkpoint** — stop for review. Do **not** commit.

---

## Task 9: `market_data` Forecasting tab — period type + quarterly render

**Files:**
- Modify: `app/pages/market_data.py`

**Interfaces:**
- Consumes: `ModelForecastsRepository.get_quarterly_forecasts_data / get_quarterly_date_range / get_quarterly_available_dates` (Task 7).

- [ ] **Step 1: Offer Quarterly in `get_available_period_types`**

At line ~266 replace the forecasting branch:

```python
if tab == "forecasting":
    types = ["Annual"]
    if ModelForecastsRepository.get_quarterly_date_range(ticker)[0] is not None:
        types.append("Quarterly")
    return types
```

- [ ] **Step 2: Period-aware date-range branch**

At line ~1896, when `_period_type_db == 'quarterly'` use the quarterly date-range /
available-dates methods; else keep the annual ones.

- [ ] **Step 3: Quarterly render in the Forecasting tab**

At line ~3861, branch on the active period type: quarterly →
`get_quarterly_forecasts_data(selected_ticker, max_quarters=8)`; render the same
HTML table but the column header `period-label` reads "Quarterly" and `period-date`
reads the quarter `label` ("Q3 2026"). The session-state cache key must include the
period type. The "Show More ↗" link appends `&period_type=Quarterly`.

- [ ] **Step 4: UI verification (Playwright)**

Run:
```bash
.venv/bin/python .claude/dev/ui_test.py \
   --path "/market_data?ticker=AAPL&tab=forecasting&period_type=Quarterly" \
   --shot /tmp/md_fc_q.png --chars 2500
```
Expected: forecasting tab shows 8 quarterly columns ("Q… 20…"), ensemble + scenario rows. Read the screenshot; show the user.

- [ ] **Step 5: Checkpoint** — stop for review. Do **not** commit.

---

## Task 10: Regression sweep + Key Stats quarterly verification

**Files:** none (verification only)

- [ ] **Step 1: Annual regression — standalone page**

Run:
```bash
.venv/bin/python .claude/dev/ui_test.py --path "/forecasting?ticker=AAPL" --shot /tmp/reg_fc_annual.png --chars 2000
```
Expected: annual view unchanged (year labels, 5-year horizon).

- [ ] **Step 2: Annual regression — tab + refresh annual**

Run:
```bash
.venv/bin/python .claude/dev/ui_test.py --path "/market_data?ticker=AAPL&tab=forecasting&period_type=Annual" --shot /tmp/reg_md_annual.png
.venv/bin/python .claude/dev/ui_test.py --path "/forecasting?ticker=AAPL" --click "Refresh Data" --wait-text "Reporting Date" --dialog --shot /tmp/reg_refresh_annual.png
```
Expected: annual tab + annual refresh table identical to before (Q4 reporting dates).

- [ ] **Step 3: Key Stats quarterly F- columns**

Run:
```bash
.venv/bin/python .claude/dev/ui_test.py --path "/market_data?ticker=AAPL&tab=key_stats&period_type=Quarterly" --shot /tmp/ks_q.png --chars 3000
```
Expected: forward F- columns present, labeled "3 Months", with quarter-end dates; Total Revenue populated for the forecast quarters.

- [ ] **Step 4: Engine unit tests still green**

Run: `.venv/bin/python -m pytest tests/test_retailer_quarterly_forecaster.py -v`
Expected: all pass.

- [ ] **Step 5: Final checkpoint** — present all screenshots + probe outputs to the user as proof. Do **not** commit (await "please commit").

---

## Self-Review

**Spec coverage:**
- §4.1 table → Task 1 ✓
- §4.2 engine → Task 2 ✓
- §4.3 store → Task 3 ✓
- §4.4 service → Task 4 ✓
- §4.5 admin + refresh + per-quarter reporting date → Tasks 5, 6 ✓
- §4.6 repository + Key Stats F- → Task 7 ✓
- §4.7 UI (standalone + embedded) → Tasks 8, 9 ✓
- §5 reporting-date semantics → Task 6 (dates), Task 7 (Key Stats labels) ✓
- §6 testing (probes + Playwright + regression) → Tasks 3–10 ✓
- §7 rollout (table auto-create + Refresh All backfill) → Task 1 (auto-create), Task 8 (Refresh All) ✓

**Placeholder scan:** no TBD/TODO; each code step carries concrete code or an exact mirror reference with the precise delta.

**Type consistency:** `forecast()` frame columns (`fiscal_year, fiscal_quarter, label, ensemble, scenario_*`) are produced in Task 2 and consumed identically in Tasks 3, 4, 7. `_quarterly_report_dates_bulk()` return dict keys match between Task 6 producer and its Task 6/email consumers. `get_quarterly_dashboard` return shape matches the annual consumer contract used by Task 8.
