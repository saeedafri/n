"""
Forecast admin: bulk sync of `coreiq_model_forecasts` for eligible companies.

Uses the same staleness rule as `forecast_store.needs_update` — the engine and
DB upsert run only when latest annual actuals are newer than stored forecasts
(or the row predates the dedup fix), so unchanged data does not trigger work.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from data.forecast_store import needs_update, upsert_forecasts
from data.forecast_refresh_service import get_company_info_for_ticker
from data.revenue_forecast_service import (
    RevenueForecastService,
    _source_table,
    _to_date,
    _yf_reported_currency,
)
from data.source_router import get_company_source
from utils.retailer_forecaster import RetailerForecaster, drop_partial_periods
from data.forecast_store import _writes_disabled as forecast_writes_disabled
from utils.retailer_quarterly_forecaster import RetailerQuarterlyForecaster
from utils.constants import yahoo_symbol
from utils.server_logger import log_structured_error

# Quarterly forecast frame columns not persisted as model rows (parity with annual store).
_QUARTERLY_NON_MODEL_COLS = ("fiscal_year", "fiscal_quarter", "label", "ci_lower", "ci_upper")

# Approved operators only (case-insensitive email match). Keep this list to exactly four accounts.
FORECAST_ADMIN_EMAILS = frozenset(
    e.strip().lower()
    for e in (
        "mohdsaeedafri@coresight.com",
        "ShashankGupta@coresight.com",
        "NidhishaMohandas@coresight.com",
        "PhilipMoore@coresight.com",
    )
)


def is_forecast_admin(email: Optional[str]) -> bool:
    if not email:
        return False
    return email.strip().lower() in FORECAST_ADMIN_EMAILS


def _forecast_store_ticker(display_ticker: str, source: str) -> str:
    """Match `coreiq_model_forecasts.ticker` (composite YF symbols when applicable)."""
    if source == "YFinance" and "." not in display_ticker:
        from data.repository import CompanyRepository as _CR

        base = display_ticker.split(".")[0] if "." in display_ticker else display_ticker
        exch = (_CR.get_companies_map().get(base) or {}).get("exchange_acronym")
        return yahoo_symbol(base, exch)
    return display_ticker


def sync_forecast_for_ticker(
    display_ticker: str,
    *,
    force: bool = False,
    periods: int = 5,
) -> Dict[str, Any]:
    """
    Run the six-model + ensemble + scenario engine and upsert into
    `coreiq_model_forecasts` when `needs_update` says stored rows are stale.

    Returns a dict with keys: ticker, status, message, rows_upserted, store_ticker (optional).
    status: updated | up_to_date | skipped | no_data | error
    """
    out: Dict[str, Any] = {
        "ticker": display_ticker,
        "status": "error",
        "message": "",
        "rows_upserted": 0,
    }
    try:
        source = get_company_source(display_ticker)
        if source not in ("SEC", "YFinance"):
            out["status"] = "skipped"
            out["message"] = f"Unsupported source: {source}"
            return out

        raw_rows = RevenueForecastService._fetch_actual_rows(display_ticker, source)
        if not raw_rows:
            out["status"] = "no_data"
            out["message"] = "No annual revenue rows in DB."
            return out

        raw_dicts = [dict(r) for r in raw_rows]
        source_table = _source_table(source)
        if source == "SEC":
            reported_currency = (raw_dicts[0].get("reported_currency") or "USD") if raw_dicts else "USD"
        else:
            base = display_ticker.split(".")[0] if "." in display_ticker else display_ticker
            reported_currency = _yf_reported_currency(base)

        actual_rows = RevenueForecastService._normalize_actual_rows(
            raw_rows=raw_dicts,
            source=source,
            source_table=source_table,
            reported_currency=reported_currency or "USD",
            display_currency=reported_currency or "USD",
        )
        if len(actual_rows) < 3:
            out["status"] = "skipped"
            out["message"] = f"Need ≥3 annual revenue points; got {len(actual_rows)}."
            return out

        _raw_df = pd.DataFrame(
            {
                "year": [row["fiscal_year"] for row in actual_rows],
                "sales": [row["total_revenue_billions"] for row in actual_rows],
            }
        ).dropna()
        if _raw_df.empty:
            out["status"] = "no_data"
            out["message"] = "No usable revenue series after normalization."
            return out

        _deduped = (
            _raw_df.loc[_raw_df.groupby("year")["sales"].transform("max") == _raw_df["sales"]]
            .drop_duplicates(subset=["year"], keep="last")
            .sort_values("year")
            .reset_index(drop=True)
        )
        if not _deduped.empty:
            # Same stub-period rule the engines apply — kept identical so the
            # service never hands the engine a differently-truncated history.
            _deduped = drop_partial_periods(_deduped)
        if len(_deduped) < 3:
            out["status"] = "skipped"
            out["message"] = f"After cleaning, only {len(_deduped)} years (need ≥3)."
            return out

        last_actual_date = _to_date(actual_rows[-1]["period_date"])
        if last_actual_date is None:
            out["status"] = "error"
            out["message"] = "Could not read last actual period date."
            return out

        store_ticker = _forecast_store_ticker(display_ticker, source)
        out["store_ticker"] = store_ticker
        out["last_actual_date"] = str(last_actual_date)

        company_info = get_company_info_for_ticker(store_ticker)
        out["company_name"] = company_info.get("company_name", "")
        out["exchange"] = company_info.get("exchange", "")

        if not force and not needs_update(store_ticker, last_actual_date):
            out["status"] = "up_to_date"
            out["message"] = "Forecasts already match latest actuals — skipped (no DB write)."
            return out

        engine = RetailerForecaster.from_dataframe(_deduped)
        backtest_df = engine.backtest(holdout_years=2)
        forecast_df = engine.forecast(periods=periods)
        best_method_key = (engine.best_method or "") or ""

        backtest_rows: List[Dict[str, Any]] = []
        if backtest_df is not None and not backtest_df.empty:
            for _, row in backtest_df.iterrows():
                mk = row.get("method")
                if mk is None:
                    continue
                backtest_rows.append(
                    {
                        "method_key": mk,
                        "method": mk,
                        "mape": row.get("mape"),
                    }
                )

        available_models = [c for c in forecast_df.columns if c != "year"]
        rows_affected = upsert_forecasts(
            ticker=store_ticker,
            forecast_df=forecast_df,
            model_keys=available_models,
            best_method_key=best_method_key,
            backtest_rows=backtest_rows,
            metric="total_revenue",
            last_actual_date=last_actual_date,
            company_name=company_info.get("company_name") or None,
            exchange=company_info.get("exchange") or None,
        )
        # Drop annual forecast rows that have aged into the past (years now reported),
        # mirroring the quarterly prune — prevents phantom past-year forecast columns.
        from data.forecast_store import prune_stale_years
        rows_pruned = prune_stale_years(store_ticker, last_actual_date)
        out["status"] = "updated"
        out["message"] = ("Store writes are disabled (FORECAST_STORE_READONLY) — nothing was saved."
                          if forecast_writes_disabled() else "Upserted model rows.")
        out["rows_upserted"] = int(rows_affected or 0)
        out["rows_pruned"] = int(rows_pruned or 0)
        return out
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_admin_service",
            component="sync_forecast_for_ticker",
            operation="SYNC",
            context={"ticker": display_ticker},
        )
        out["status"] = "error"
        out["message"] = str(exc)[:500]
        return out


def sync_all_eligible(
    *,
    force: bool = False,
    periods: int = 5,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    exclude_tickers: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Run `sync_forecast_for_ticker` for every company returned by `get_companies`.

    `exclude_tickers` (matched case-insensitively) are skipped — used to hold back
    companies whose annual reporting date has not yet been identified.
    """
    try:
        companies = RevenueForecastService.get_companies()
    except Exception:
        companies = []

    _excl = {str(t).strip().upper() for t in (exclude_tickers or set())}
    results: List[Dict[str, Any]] = []
    total = len(companies)
    for i, row in enumerate(companies):
        disp = (row.get("ticker") or "").strip()
        if not disp:
            continue
        if disp.upper() in _excl:
            continue
        if progress_callback:
            try:
                progress_callback(i + 1, total, disp)
            except Exception:
                pass
        res = sync_forecast_for_ticker(disp, force=force, periods=periods)
        res["company_name"] = row.get("name") or disp
        results.append(res)
    return results


def clear_revenue_forecast_caches() -> None:
    """Invalidate Streamlit caches so Estimates / Market Data pick up new DB rows."""
    try:
        RevenueForecastService.get_company_dashboard.clear()
        RevenueForecastService.get_quarterly_dashboard.clear()
        RevenueForecastService.get_companies.clear()
    except Exception:
        pass


# ===========================================================================
# QUARTERLY  — siblings of the annual sync functions. Run the seasonal engine
# and upsert into coreiq_model_forecasts_quarterly. Annual path untouched.
# ===========================================================================

def sync_quarterly_forecast_for_ticker(
    display_ticker: str,
    *,
    force: bool = False,
    periods: int = 20,
) -> Dict[str, Any]:
    """Run the quarterly seasonal engine and upsert into
    coreiq_model_forecasts_quarterly when needs_update says stored rows are stale.

    Returns the same result-dict shape as sync_forecast_for_ticker.
    status: updated | up_to_date | skipped | no_data | error
    """
    from data.quarterly_forecast_store import (
        ensure_quarterly_forecast_table, needs_update as q_needs_update,
        upsert_forecasts as q_upsert_forecasts, prune_stale_quarters as q_prune_stale,
    )

    out: Dict[str, Any] = {
        "ticker": display_ticker, "status": "error", "message": "", "rows_upserted": 0,
        "period_type": "quarterly",
    }
    try:
        source = get_company_source(display_ticker)
        if source not in ("SEC", "YFinance"):
            out["status"] = "skipped"
            out["message"] = f"Unsupported source: {source}"
            return out

        reported_currency = "USD"
        if source == "YFinance":
            base = display_ticker.split(".")[0] if "." in display_ticker else display_ticker
            reported_currency = _yf_reported_currency(base) or "USD"

        raw_rows = RevenueForecastService._fetch_quarterly_rows(display_ticker, source)
        if not raw_rows:
            out["status"] = "no_data"
            out["message"] = "No quarterly revenue rows in DB."
            return out

        actual_rows = RevenueForecastService._normalize_quarterly_rows(
            raw_rows=[dict(r) for r in raw_rows], source=source,
            reported_currency=reported_currency,
        )

        _raw_df = pd.DataFrame({
            "year": [r["fiscal_year"] for r in actual_rows],
            "quarter": [r["fiscal_quarter"] for r in actual_rows],
            "sales": [r["total_revenue_billions"] for r in actual_rows],
        }).dropna()
        _deduped = (
            _raw_df.loc[_raw_df.groupby(["year", "quarter"])["sales"].transform("max") == _raw_df["sales"]]
            .drop_duplicates(subset=["year", "quarter"], keep="last")
            .sort_values(["year", "quarter"])
            .reset_index(drop=True)
        )
        if not _deduped.empty:
            # Same stub-period rule the engines apply — kept identical so the
            # service never hands the engine a differently-truncated history.
            _deduped = drop_partial_periods(_deduped)
        if len(_deduped) < 8:
            out["status"] = "skipped"
            out["message"] = f"Need >=8 quarters; got {len(_deduped)}."
            return out

        last_actual_date = _to_date(actual_rows[-1]["period_date"])
        if last_actual_date is None:
            out["status"] = "error"
            out["message"] = "Could not read last actual quarter date."
            return out

        store_ticker = _forecast_store_ticker(display_ticker, source)
        out["store_ticker"] = store_ticker
        out["last_actual_date"] = str(last_actual_date)

        company_info = get_company_info_for_ticker(store_ticker)
        out["company_name"] = company_info.get("company_name", "")
        out["exchange"] = company_info.get("exchange", "")

        ensure_quarterly_forecast_table()
        if not force and not q_needs_update(store_ticker, last_actual_date):
            out["status"] = "up_to_date"
            out["message"] = "Quarterly forecasts already match latest actuals — skipped."
            return out

        engine = RetailerQuarterlyForecaster.from_dataframe(_deduped)
        backtest_df = engine.backtest(holdout_quarters=4)
        forecast_df = engine.forecast(periods=periods)
        best_method_key = engine.best_method or ""

        backtest_rows: List[Dict[str, Any]] = []
        if backtest_df is not None and not backtest_df.empty:
            for _, row in backtest_df.iterrows():
                mk = row.get("method")
                if mk is not None:
                    backtest_rows.append({"method_key": mk, "method": mk, "mape": row.get("mape")})

        available_models = [c for c in forecast_df.columns if c not in _QUARTERLY_NON_MODEL_COLS]
        rows_affected = q_upsert_forecasts(
            ticker=store_ticker, forecast_df=forecast_df, model_keys=available_models,
            best_method_key=best_method_key, backtest_rows=backtest_rows,
            metric="total_revenue", last_actual_date=last_actual_date,
            company_name=company_info.get("company_name") or None,
            exchange=company_info.get("exchange") or None,
        )
        # Drop forecast rows that have aged into the past (quarters now reported).
        rows_pruned = q_prune_stale(store_ticker, last_actual_date)
        out["status"] = "updated"
        out["message"] = ("Store writes are disabled (FORECAST_STORE_READONLY) — nothing was saved."
                          if forecast_writes_disabled() else "Upserted quarterly model rows.")
        out["rows_upserted"] = int(rows_affected or 0)
        out["rows_pruned"] = int(rows_pruned or 0)
        return out
    except Exception as exc:
        log_structured_error(
            exc, page="forecast_admin_service",
            component="sync_quarterly_forecast_for_ticker", operation="SYNC_QUARTERLY",
            context={"ticker": display_ticker},
        )
        out["status"] = "error"
        out["message"] = str(exc)[:500]
        return out


def sync_all_eligible_quarterly(
    *,
    force: bool = False,
    periods: int = 20,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    exclude_tickers: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Run sync_quarterly_forecast_for_ticker for every company from get_companies."""
    try:
        companies = RevenueForecastService.get_companies()
    except Exception:
        companies = []

    _excl = {str(t).strip().upper() for t in (exclude_tickers or set())}
    results: List[Dict[str, Any]] = []
    total = len(companies)
    for i, row in enumerate(companies):
        disp = (row.get("ticker") or "").strip()
        if not disp or disp.upper() in _excl:
            continue
        if progress_callback:
            try:
                progress_callback(i + 1, total, disp)
            except Exception:
                pass
        res = sync_quarterly_forecast_for_ticker(disp, force=force, periods=periods)
        res["company_name"] = row.get("name") or disp
        results.append(res)
    return results
