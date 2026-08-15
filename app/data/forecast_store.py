"""
forecast_store.py
=================
Write-through cache between the in-memory forecasting engine and the
staging DB table `coreiq_model_forecasts`.

Responsibilities:
  1. needs_update()    — cheap staleness check (1 RTT). Returns True only when
                         new actuals exist that haven't been forecasted yet.
  2. upsert_forecasts()— multi-row INSERT … ON DUPLICATE KEY UPDATE (1 RTT).
                         Called by revenue_forecast_service in a background thread
                         only when needs_update() returns True.
  3. get_forecasts()   — called by KeyStatsRepository for F- columns.
  4. get_all_model_forecasts() — all models grouped by key (future use).
"""
from __future__ import annotations

import datetime
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from core.database import db_manager
from utils.server_logger import log_structured_error, log_error

def _writes_disabled() -> bool:
    """True when this process must not write to the forecast store.

    A developer running the app locally points at the staging database. Every
    page view that computes a fresh forecast fires a background upsert, so
    simply browsing /forecasting from a laptop rewrites staging rows. Set
    FORECAST_STORE_READONLY=1 (run_local.sh does) to keep local runs read-only.
    """
    return os.getenv("FORECAST_STORE_READONLY", "").strip().lower() in ("1", "true", "yes", "on")


_METRIC_TOTAL_REVENUE = "total_revenue"
_ENSEMBLE_KEY = "ensemble"

# Any forecast computed before this date used keep="first" dedup (wrong).
# Force recompute for all rows older than this date.
# Any forecast computed before this date came from an earlier revision of the
# forecasting engine and is not comparable with what the engine produces now.
# Bump it whenever the model changes, or stored rows keep being served until the
# company happens to report again.
_MODEL_REVISION_DATE = datetime.date(2026, 8, 15)


# ---------------------------------------------------------------------------
# Staleness check — called inside the background thread, zero main-thread cost
# ---------------------------------------------------------------------------

def needs_update(
    ticker: str,
    last_actual_date: datetime.date,
    metric: str = _METRIC_TOTAL_REVENUE,
) -> bool:
    """
    Return True only if the stored forecast was computed from older actuals
    than `last_actual_date`.

    Logic:
      - Query MAX(last_actual_date) already stored for this ticker/metric.
      - If stored date == last_actual_date → data unchanged, skip upsert.
      - If stored date is older or missing → new actuals arrived, re-forecast.

    Cost: 1 read RTT (~250ms Azure). Saved cost: entire forecast engine run
    + upsert RTT on every cold-cache Estimates page load.
    """
    try:
        rows = db_manager.execute_query_readonly(
            """
            SELECT MAX(last_actual_date) AS stored_date,
                   MAX(computed_at)      AS computed_at
            FROM   coreiq_model_forecasts
            WHERE  ticker  = :ticker
              AND  metric  = :metric
            """,
            {"ticker": ticker, "metric": metric},
        )
        row = rows[0] if rows else None
        stored = row["stored_date"] if row else None
        computed_at = row["computed_at"] if row else None
        if stored and hasattr(stored, "date"):
            stored = stored.date()
        if stored != last_actual_date:
            return True
        # Recompute anything produced by an earlier revision of the engine.
        if computed_at:
            computed_date = computed_at.date() if hasattr(computed_at, "date") else computed_at
            if isinstance(computed_date, str):
                computed_date = datetime.date.fromisoformat(computed_date[:10])
            if computed_date < _MODEL_REVISION_DATE:
                return True
        return False
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_store",
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
    Upsert forecast rows for all models into coreiq_model_forecasts.

    Single multi-row INSERT … ON DUPLICATE KEY UPDATE — one RTT regardless
    of how many rows.

    `last_actual_date`: max fiscal_date_ending from the income-statement table
    at the time of this compute run. Stored in each row so needs_update() can
    detect future data arrivals without re-running the engine.

    Returns rows affected (inserts + updates × 2 per MySQL convention).
    """
    if _writes_disabled():
        return 0
    if forecast_df is None or forecast_df.empty:
        return 0

    # Build MAPE lookup: backtest rows use "method_key" (Python key like "ma_trend")
    mape_by_key: Dict[str, Optional[float]] = {}
    for row in (backtest_rows or []):
        mk = row.get("method_key") or row.get("key")
        if mk:
            mape_by_key[mk] = row.get("mape")

    computed_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    last_actual_str = last_actual_date.strftime("%Y-%m-%d") if last_actual_date else None

    rows: List[Dict[str, Any]] = []
    year_col = "year"

    for model_key in model_keys:
        if model_key not in forecast_df.columns:
            continue
        series = forecast_df[[year_col, model_key]].dropna()
        if series.empty:
            continue

        is_ensemble = 1 if model_key == _ENSEMBLE_KEY else 0
        is_best = 1 if model_key == best_method_key else 0
        mape = mape_by_key.get(model_key)
        first_forecast_year = int(series[year_col].min())

        for _, r in series.iterrows():
            fy = int(r[year_col])
            val_b = r[model_key]
            val_mm = float(val_b) * 1000 if val_b is not None else None
            periods_ahead = fy - first_forecast_year + 1
            # Compute forecast_date: same month/day as last_actual_date, year incremented
            forecast_date_str = None
            if last_actual_date:
                try:
                    fd = last_actual_date.replace(year=last_actual_date.year + periods_ahead)
                except ValueError:
                    fd = last_actual_date.replace(year=last_actual_date.year + periods_ahead, day=28)
                forecast_date_str = fd.strftime("%Y-%m-%d")
            rows.append({
                "ticker":           ticker,
                "fiscal_year":      fy,
                "forecast_date":    forecast_date_str,
                "metric":           metric,
                "model_key":        model_key,
                "value_millions":   round(val_mm, 4) if val_mm is not None else None,
                "is_best_model":    is_best,
                "is_ensemble":      is_ensemble,
                "mape":             round(float(mape), 4) if mape is not None else None,
                "periods_ahead":    periods_ahead,
                "computed_at":      computed_at,
                "last_actual_date": last_actual_str,
                "company_name":     company_name,
                "exchange":         exchange,
            })

    if not rows:
        return 0

    placeholders = ", ".join(
        f"(:ticker_{i}, :fiscal_year_{i}, :forecast_date_{i}, :metric_{i}, :model_key_{i}, "
        f":value_millions_{i}, :is_best_model_{i}, :is_ensemble_{i}, "
        f":mape_{i}, :periods_ahead_{i}, :computed_at_{i}, :last_actual_date_{i}, "
        f":company_name_{i}, :exchange_{i})"
        for i in range(len(rows))
    )

    flat_params: Dict[str, Any] = {}
    for i, row in enumerate(rows):
        for k, v in row.items():
            flat_params[f"{k}_{i}"] = v

    upsert_sql = f"""
        INSERT INTO coreiq_model_forecasts
            (ticker, fiscal_year, forecast_date, metric, model_key,
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
        affected = db_manager.execute_insert(upsert_sql, flat_params)
        # Drop this ticker's rows that the current computation did not write.
        # Every row the forecast owns was just stamped with `computed_at`, so an
        # older stamp means the row is left over from a previous run whose
        # horizon reached further out (e.g. a 2030-31 tail from a forecast made
        # when the last actual was older). Those rows are stale model output and
        # would resurface if the horizon ever widened.
        db_manager.execute_delete(
            """
            DELETE FROM coreiq_model_forecasts
            WHERE  ticker = :ticker AND metric = :metric AND computed_at < :computed_at
            """,
            {"ticker": ticker, "metric": metric, "computed_at": computed_at},
        )
        return affected
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_store",
            component="upsert_forecasts",
            operation="UPSERT",
            context={"ticker": ticker, "rows": len(rows)},
        )
        return 0


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def get_forecasts(
    ticker: str,
    metric: str = _METRIC_TOTAL_REVENUE,
    model_key: str = _ENSEMBLE_KEY,
    max_periods: int = 5,
) -> List[Dict[str, Any]]:
    """
    Fetch up to `max_periods` forecast rows for Key Stats F- columns.
    Returns list ordered by fiscal_year ASC.
    """
    query = """
        SELECT fiscal_year, value_millions, is_best_model, mape,
               periods_ahead, computed_at, last_actual_date
        FROM   coreiq_model_forecasts
        WHERE  ticker    = :ticker
          AND  metric    = :metric
          AND  model_key = :model_key
        ORDER BY fiscal_year ASC
        LIMIT  :max_periods
    """
    try:
        rows = db_manager.execute_query_readonly(query, {
            "ticker":      ticker,
            "metric":      metric,
            "model_key":   model_key,
            "max_periods": max_periods,
        })
        return [dict(r) for r in (rows or [])]
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_store",
            component="get_forecasts",
            operation="SELECT",
            context={"ticker": ticker},
        )
        return []


def get_all_model_forecasts(
    ticker: str,
    metric: str = _METRIC_TOTAL_REVENUE,
    max_periods: int = 5,
) -> Dict[str, List[Dict]]:
    """Return forecasts for every model keyed by model_key."""
    # MySQL doesn't allow LIMIT inside IN subqueries — use a derived table join instead
    query = """
        SELECT f.model_key, f.fiscal_year, f.value_millions, f.is_best_model,
               f.is_ensemble, f.mape, f.periods_ahead, f.last_actual_date
        FROM   coreiq_model_forecasts f
        INNER JOIN (
            SELECT DISTINCT fiscal_year
            FROM   coreiq_model_forecasts
            WHERE  ticker = :ticker AND metric = :metric
            ORDER  BY fiscal_year ASC
            LIMIT  :max_periods
        ) fy ON f.fiscal_year = fy.fiscal_year
        WHERE  f.ticker = :ticker
          AND  f.metric = :metric
        ORDER BY f.model_key, f.fiscal_year ASC
    """
    try:
        rows = db_manager.execute_query_readonly(query, {
            "ticker":      ticker,
            "metric":      metric,
            "max_periods": max_periods,
        })
        result: Dict[str, List[Dict]] = {}
        for r in (rows or []):
            mk = r["model_key"]
            result.setdefault(mk, []).append(dict(r))
        return result
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_store",
            component="get_all_model_forecasts",
            operation="SELECT",
            context={"ticker": ticker},
        )
        return {}


def prune_stale_years(
    ticker: str,
    last_actual_date: datetime.date,
    metric: str = _METRIC_TOTAL_REVENUE,
) -> int:
    """
    Delete annual forecast rows that have aged into the past for this ticker.

    A fresh forecast only ever covers fiscal years AFTER `last_actual_date`, so any
    stored row whose `forecast_date <= last_actual_date` is a projection for a year
    that has since been reported — stale, and safe to drop. Mirrors the quarterly
    `prune_stale_quarters`; without it the annual table accumulates a dead forecast
    row for every year that becomes actual (shown in the UI as a phantom "forecast"
    column for a past year). Called after each successful annual upsert.
    """
    if last_actual_date is None:
        return 0
    try:
        return db_manager.execute_delete(
            """
            DELETE FROM coreiq_model_forecasts
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
            exc, page="forecast_store", component="prune_stale_years",
            operation="DELETE", context={"ticker": ticker},
        )
        return 0


def delete_forecasts_beyond_horizon(max_periods_ahead: int = 5) -> int:
    """
    Remove rows from coreiq_model_forecasts where periods_ahead exceeds the
    supported forecast horizon (default 5 years).
    """
    try:
        return db_manager.execute_insert(
            "DELETE FROM coreiq_model_forecasts WHERE periods_ahead > :max_ahead",
            {"max_ahead": int(max_periods_ahead)},
        )
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_store",
            component="delete_forecasts_beyond_horizon",
            operation="DELETE",
            context={"max_periods_ahead": max_periods_ahead},
        )
        return 0
