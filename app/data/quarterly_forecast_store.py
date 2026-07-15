"""
quarterly_forecast_store.py
===========================
Write-through cache between the in-memory quarterly forecasting engine and the
staging DB table `coreiq_model_forecasts_quarterly`.

Sibling of `forecast_store.py` (annual). Everything here is keyed by
(ticker, fiscal_year, fiscal_quarter, metric, model_key) so the annual table and
its queries are never touched.

Responsibilities:
  1. ensure_quarterly_forecast_table() — create the table if absent (idempotent).
  2. needs_update()       — cheap staleness check (1 RTT) against the latest quarter.
  3. upsert_forecasts()   — one multi-row INSERT … ON DUPLICATE KEY UPDATE (1 RTT).
  4. get_all_model_forecasts() — every model's rows for the next N quarters.
  5. get_forecasts()      — ensemble rows for Key Stats forward (F-) columns.
"""
from __future__ import annotations

import calendar
import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from core.database import db_manager
from utils.server_logger import log_structured_error, log_info

_METRIC_TOTAL_REVENUE = "total_revenue"
_ENSEMBLE_KEY = "ensemble"

# Revenue series fed to the engine is in billions; the store keeps millions.
_BILLIONS_TO_MILLIONS = 1000

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
  COMMENT='Model-generated quarterly revenue forecasts (20-quarter horizon)'
"""


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

def ensure_quarterly_forecast_table() -> None:
    """Create coreiq_model_forecasts_quarterly if it does not yet exist (idempotent)."""
    try:
        db_manager.execute_insert(_CREATE_TABLE_SQL, {})
        log_info("[quarterly_forecast_store] ensured coreiq_model_forecasts_quarterly")
    except Exception as exc:
        log_structured_error(
            exc,
            page="quarterly_forecast_store",
            component="ensure_quarterly_forecast_table",
            operation="CREATE_TABLE",
        )


def _quarter_end_date(fiscal_year: int, fiscal_quarter: int) -> Optional[datetime.date]:
    """Last calendar day of the given fiscal quarter (Q1→Mar, Q2→Jun, Q3→Sep, Q4→Dec).

    Calendar-quarter fallback only — used when there is no last-actual date to
    project a real cadence from. Prefer `_add_months_eom(last_actual, 3*n)`.
    """
    if fiscal_quarter not in (1, 2, 3, 4):
        return None
    month = fiscal_quarter * 3
    last_day = calendar.monthrange(fiscal_year, month)[1]
    return datetime.date(fiscal_year, month, last_day)


def _add_months_eom(d: datetime.date, n: int) -> datetime.date:
    """`d` advanced by `n` months, snapped to the last day of the resulting month.

    Used to project real quarter-end dates from the last actual quarter so the
    forecast follows each company's true fiscal cadence (e.g. Apr 30 → Jul 31 →
    Oct 31 → Jan 31 for a January fiscal year), not a generic calendar-quarter map.
    """
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return datetime.date(y, m, calendar.monthrange(y, m)[1])


# ---------------------------------------------------------------------------
# Staleness check — runs inside the background thread, zero main-thread cost
# ---------------------------------------------------------------------------

def needs_update(
    ticker: str,
    last_actual_date: datetime.date,
    metric: str = _METRIC_TOTAL_REVENUE,
) -> bool:
    """
    Return True only if the stored quarterly forecast was computed from an older
    last actual quarter than `last_actual_date`.
    """
    try:
        rows = db_manager.execute_query_readonly(
            """
            SELECT MAX(last_actual_date) AS stored_date
            FROM   coreiq_model_forecasts_quarterly
            WHERE  ticker = :ticker
              AND  metric = :metric
            """,
            {"ticker": ticker, "metric": metric},
        )
        stored = rows[0]["stored_date"] if rows else None
        if stored and hasattr(stored, "date"):
            stored = stored.date()
        return stored != last_actual_date
    except Exception as exc:
        log_structured_error(
            exc,
            page="quarterly_forecast_store",
            component="needs_update",
            operation="SELECT",
            context={"ticker": ticker},
        )
        return True  # on error, allow upsert to proceed


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def upsert_forecasts(
    ticker: str,
    forecast_df: pd.DataFrame,
    model_keys: List[str],
    best_method_key: str,
    backtest_rows: List[Dict],
    metric: str = _METRIC_TOTAL_REVENUE,
    last_actual_date: Optional[datetime.date] = None,
    company_name: Optional[str] = None,
    exchange: Optional[str] = None,
) -> int:
    """
    Upsert quarterly forecast rows for all models in a single multi-row
    INSERT … ON DUPLICATE KEY UPDATE (one RTT).

    `forecast_df` carries `fiscal_year`, `fiscal_quarter`, and one column per model.
    """
    if forecast_df is None or forecast_df.empty:
        return 0
    if "fiscal_year" not in forecast_df.columns or "fiscal_quarter" not in forecast_df.columns:
        return 0

    mape_by_key: Dict[str, Optional[float]] = {}
    for row in (backtest_rows or []):
        mk = row.get("method_key") or row.get("method")
        if mk:
            mape_by_key[mk] = row.get("mape")

    computed_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    last_actual_str = last_actual_date.strftime("%Y-%m-%d") if last_actual_date else None

    # Map each forecast (fiscal_year, fiscal_quarter) to its REAL projected quarter-end
    # date by stepping +3 months (end-of-month) from the last actual quarter. Built once
    # from the full frame so every model shares the same, correct dates. Falls back to the
    # calendar-quarter map only when there is no last-actual date.
    _period_dates: Dict[tuple, Optional[str]] = {}
    try:
        _uniq = sorted({(int(a), int(b)) for a, b in
                        zip(forecast_df["fiscal_year"], forecast_df["fiscal_quarter"])})
        for _idx, _fyq in enumerate(_uniq, start=1):
            if last_actual_date is not None:
                _d = _add_months_eom(last_actual_date, 3 * _idx)
            else:
                _d = _quarter_end_date(_fyq[0], _fyq[1])
            _period_dates[_fyq] = _d.strftime("%Y-%m-%d") if _d else None
    except Exception:
        _period_dates = {}

    rows: List[Dict[str, Any]] = []
    for model_key in model_keys:
        if model_key not in forecast_df.columns:
            continue
        series = forecast_df[["fiscal_year", "fiscal_quarter", model_key]].dropna()
        if series.empty:
            continue

        is_ensemble = 1 if model_key == _ENSEMBLE_KEY else 0
        is_best = 1 if model_key == best_method_key else 0
        mape = mape_by_key.get(model_key)

        for ordinal, (_, r) in enumerate(series.iterrows(), start=1):
            fy = int(r["fiscal_year"])
            fq = int(r["fiscal_quarter"])
            val_b = r[model_key]
            val_mm = float(val_b) * _BILLIONS_TO_MILLIONS if val_b is not None else None
            _fdate = _period_dates.get((fy, fq))
            if _fdate is None:
                _qend = _quarter_end_date(fy, fq)
                _fdate = _qend.strftime("%Y-%m-%d") if _qend else None
            rows.append({
                "ticker":           ticker,
                "fiscal_year":      fy,
                "fiscal_quarter":   fq,
                "forecast_date":    _fdate,
                "metric":           metric,
                "model_key":        model_key,
                "value_millions":   round(val_mm, 4) if val_mm is not None else None,
                "is_best_model":    is_best,
                "is_ensemble":      is_ensemble,
                "mape":             round(float(mape), 4) if mape is not None else None,
                "periods_ahead":    ordinal,
                "computed_at":      computed_at,
                "last_actual_date": last_actual_str,
                "company_name":     company_name,
                "exchange":         exchange,
            })

    if not rows:
        return 0

    placeholders = ", ".join(
        f"(:ticker_{i}, :fiscal_year_{i}, :fiscal_quarter_{i}, :forecast_date_{i}, "
        f":metric_{i}, :model_key_{i}, :value_millions_{i}, :is_best_model_{i}, "
        f":is_ensemble_{i}, :mape_{i}, :periods_ahead_{i}, :computed_at_{i}, "
        f":last_actual_date_{i}, :company_name_{i}, :exchange_{i})"
        for i in range(len(rows))
    )

    flat_params: Dict[str, Any] = {}
    for i, row in enumerate(rows):
        for k, v in row.items():
            flat_params[f"{k}_{i}"] = v

    upsert_sql = f"""
        INSERT INTO coreiq_model_forecasts_quarterly
            (ticker, fiscal_year, fiscal_quarter, forecast_date, metric, model_key,
             value_millions, is_best_model, is_ensemble,
             mape, periods_ahead, computed_at, last_actual_date,
             company_name, exchange)
        VALUES {placeholders}
        ON DUPLICATE KEY UPDATE
            forecast_date     = VALUES(forecast_date),
            value_millions    = VALUES(value_millions),
            is_best_model     = VALUES(is_best_model),
            is_ensemble       = VALUES(is_ensemble),
            mape              = VALUES(mape),
            periods_ahead     = VALUES(periods_ahead),
            computed_at       = VALUES(computed_at),
            last_actual_date  = VALUES(last_actual_date),
            company_name      = COALESCE(VALUES(company_name), company_name),
            exchange          = COALESCE(VALUES(exchange), exchange)
    """

    try:
        return db_manager.execute_insert(upsert_sql, flat_params)
    except Exception as exc:
        log_structured_error(
            exc,
            page="quarterly_forecast_store",
            component="upsert_forecasts",
            operation="UPSERT",
            context={"ticker": ticker, "rows": len(rows)},
        )
        return 0


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def get_all_model_forecasts(
    ticker: str,
    metric: str = _METRIC_TOTAL_REVENUE,
    max_periods: int = 20,
) -> Dict[str, List[Dict]]:
    """Return forecasts for every model keyed by model_key, for the nearest N quarters."""
    query = """
        SELECT f.model_key, f.fiscal_year, f.fiscal_quarter, f.value_millions,
               f.is_best_model, f.is_ensemble, f.mape, f.forecast_date, f.last_actual_date
        FROM   coreiq_model_forecasts_quarterly f
        INNER JOIN (
            SELECT DISTINCT fiscal_year, fiscal_quarter
            FROM   coreiq_model_forecasts_quarterly
            WHERE  ticker = :ticker AND metric = :metric
            ORDER  BY fiscal_year ASC, fiscal_quarter ASC
            LIMIT  :max_periods
        ) fq ON f.fiscal_year = fq.fiscal_year AND f.fiscal_quarter = fq.fiscal_quarter
        WHERE  f.ticker = :ticker
          AND  f.metric = :metric
        ORDER BY f.model_key, f.fiscal_year ASC, f.fiscal_quarter ASC
    """
    try:
        rows = db_manager.execute_query_readonly(query, {
            "ticker": ticker, "metric": metric, "max_periods": max_periods,
        })
        result: Dict[str, List[Dict]] = {}
        for r in (rows or []):
            result.setdefault(r["model_key"], []).append(dict(r))
        return result
    except Exception as exc:
        log_structured_error(
            exc,
            page="quarterly_forecast_store",
            component="get_all_model_forecasts",
            operation="SELECT",
            context={"ticker": ticker},
        )
        return {}


def get_forecasts(
    ticker: str,
    metric: str = _METRIC_TOTAL_REVENUE,
    model_key: str = _ENSEMBLE_KEY,
    max_periods: int = 8,
) -> List[Dict[str, Any]]:
    """Fetch up to `max_periods` quarterly forecast rows for one model (Key Stats F- columns)."""
    query = """
        SELECT fiscal_year, fiscal_quarter, forecast_date, value_millions,
               is_best_model, mape, periods_ahead, computed_at, last_actual_date
        FROM   coreiq_model_forecasts_quarterly
        WHERE  ticker    = :ticker
          AND  metric    = :metric
          AND  model_key = :model_key
        ORDER BY fiscal_year ASC, fiscal_quarter ASC
        LIMIT  :max_periods
    """
    try:
        rows = db_manager.execute_query_readonly(query, {
            "ticker": ticker, "metric": metric,
            "model_key": model_key, "max_periods": max_periods,
        })
        return [dict(r) for r in (rows or [])]
    except Exception as exc:
        log_structured_error(
            exc,
            page="quarterly_forecast_store",
            component="get_forecasts",
            operation="SELECT",
            context={"ticker": ticker},
        )
        return []


def delete_forecasts_beyond_horizon(max_periods_ahead: int = 20) -> int:
    """Remove rows whose periods_ahead exceeds the supported quarterly horizon."""
    try:
        return db_manager.execute_insert(
            "DELETE FROM coreiq_model_forecasts_quarterly WHERE periods_ahead > :max_ahead",
            {"max_ahead": int(max_periods_ahead)},
        )
    except Exception as exc:
        log_structured_error(
            exc,
            page="quarterly_forecast_store",
            component="delete_forecasts_beyond_horizon",
            operation="DELETE",
            context={"max_periods_ahead": max_periods_ahead},
        )
        return 0


def prune_stale_quarters(
    ticker: str,
    last_actual_date: datetime.date,
    metric: str = _METRIC_TOTAL_REVENUE,
) -> int:
    """
    Delete forecast rows that have aged into the past for this ticker.

    A fresh forecast only ever covers quarters AFTER `last_actual_date`, so any
    stored row whose `forecast_date <= last_actual_date` is a projection for a
    quarter that has since been reported — stale, and safe to drop. Keeps the
    table from accumulating one dead row per ticker per elapsed quarter without
    affecting the live forward window. Called after each successful upsert.
    """
    if last_actual_date is None:
        return 0
    try:
        return db_manager.execute_delete(
            """
            DELETE FROM coreiq_model_forecasts_quarterly
            WHERE ticker = :ticker
              AND metric = :metric
              AND forecast_date IS NOT NULL
              AND forecast_date <= :last_actual_date
            """,
            {"ticker": ticker, "metric": metric,
             "last_actual_date": last_actual_date.strftime("%Y-%m-%d")},
        )
    except Exception as exc:
        log_structured_error(
            exc,
            page="quarterly_forecast_store",
            component="prune_stale_quarters",
            operation="DELETE",
            context={"ticker": ticker},
        )
        return 0
