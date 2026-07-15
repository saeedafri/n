"""
Read-only revenue forecasting service.

Loads annual revenue actuals from the staging database and runs the same
six-model forecasting engine that powers the CIQ Estimates notebook.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from time import perf_counter
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from core.database import db_manager
from data.source_router import get_company_source
from utils.retailer_forecaster import RetailerForecaster
from utils.retailer_quarterly_forecaster import RetailerQuarterlyForecaster
from utils.server_logger import log_structured_error, log_timing

# Quarterly forecast frame columns that are not persisted as model rows
# (mirrors the annual store, which keeps only models + ensemble + scenarios).
_QUARTERLY_NON_MODEL_COLS = ("fiscal_year", "fiscal_quarter", "label", "ci_lower", "ci_upper")
# The forecasting models (excludes scenarios) — used to shape the page-facing frame.
_QUARTERLY_MODEL_KEYS = ("linear", "cagr", "exp_smoothing", "holt",
                         "ma_trend", "weighted_avg", "seasonal_naive", "ensemble")


SEC_SOURCE_TABLE = "coreiq_av_financials_income_statement"
YF_SOURCE_TABLE = "coreiq_yf_financials_income_statement"

_MODEL_FULL_LABELS: Dict[str, str] = {
    "linear": "Linear Regression",
    "cagr": "Compound Annual Growth Rate",
    "exp_smoothing": "Exponential Smoothing",
    "holt": "Holt's Linear Trend",
    "ma_trend": "Moving Average Trend",
    "weighted_avg": "Weighted Average Growth",
    "ensemble": "Ensemble",
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if hasattr(value, "date"):
        return value.date()
    return value


def _source_label(source: Optional[str]) -> str:
    if source == "SEC":
        return "SEC"
    if source == "YFinance":
        return "YFinance"
    return "Unknown"


def _source_table(source: str) -> str:
    return SEC_SOURCE_TABLE if source == "SEC" else YF_SOURCE_TABLE


def _source_columns(source: str) -> List[str]:
    if source == "SEC":
        return [
            "fiscal_date_ending",
            "total_revenue",
            "gross_profit",
            "operating_income",
            "net_income",
            "ebitda",
            "reported_currency",
        ]
    return [
        "period_end",
        "frequency",
        "line_item",
        "value",
    ]


def _yf_reported_currency(ticker: str) -> str:
    query = """
        SELECT payload_json
        FROM coreiq_yf_company_overview
        WHERE ticker = :ticker
        ORDER BY ingested_at DESC
        LIMIT 1
    """
    rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
    if not rows:
        return "USD"

    payload_raw = rows[0].get("payload_json")
    if not payload_raw:
        return "USD"

    try:
        payload = json.loads(payload_raw)
        info = payload.get("info", payload) if isinstance(payload, dict) else {}
        if isinstance(info, dict):
            return info.get("financialCurrency") or info.get("currency") or "USD"
    except Exception:
        pass
    return "USD"


class RevenueForecastService:
    """Read-only data access helpers for the Revenue Estimates page."""

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)
    def get_companies() -> List[Dict[str, Any]]:
        start = perf_counter()
        query = f"""
            WITH sec_stats AS (
                SELECT ticker, COUNT(DISTINCT fiscal_date_ending) AS row_count, MAX(fiscal_date_ending) AS max_period
                FROM {SEC_SOURCE_TABLE}
                WHERE report_type = 'annual'
                  AND total_revenue IS NOT NULL
                GROUP BY ticker
            ),
            yf_stats AS (
                SELECT yf_symbol, COUNT(DISTINCT period_end) AS row_count, MAX(period_end) AS max_period
                FROM (
                    SELECT yf_symbol, period_end
                    FROM {YF_SOURCE_TABLE}
                    WHERE frequency = 'annual'
                      AND line_item = 'Total Revenue'
                      AND value IS NOT NULL
                ) yf
                GROUP BY yf_symbol
            )
            SELECT
                c.ticker,
                c.name_coresight AS company_name,
                c.source,
                c.exchange_acronym,
                COALESCE(sec_stats.row_count, yf_stats.row_count, 0) AS row_count,
                COALESCE(sec_stats.max_period, yf_stats.max_period) AS max_period
            FROM coreiq_companies c
            LEFT JOIN sec_stats
                ON sec_stats.ticker = c.ticker AND c.source = 'SEC'
            LEFT JOIN yf_stats
                ON yf_stats.yf_symbol = CASE
                    WHEN c.exchange_acronym IS NOT NULL AND c.exchange_acronym != ''
                    THEN CONCAT(c.ticker, '.', c.exchange_acronym)
                    ELSE c.ticker
                END AND c.source = 'YFinance'
            WHERE c.source IN ('SEC', 'YFinance')
              AND COALESCE(sec_stats.row_count, yf_stats.row_count, 0) >= 3
            ORDER BY company_name ASC
        """

        try:
            rows = db_manager.execute_query_readonly(query, {})
            companies: List[Dict[str, Any]] = []
            for row in rows:
                base_ticker = (row.get("ticker") or "").strip()
                if not base_ticker:
                    continue
                source = (row.get("source") or "").strip()
                exchange_acronym = (row.get("exchange_acronym") or "").strip() or None
                # Composite ticker: "TSCO.L" for Tesco, "TSCO" for Tractor Supply
                display_ticker = f"{base_ticker}.{exchange_acronym}" if exchange_acronym else base_ticker
                company_name = (row.get("company_name") or base_ticker).strip()
                companies.append(
                    {
                        "ticker": display_ticker,
                        "base_ticker": base_ticker,
                        "exchange_acronym": exchange_acronym,
                        "name": company_name,
                        "label": f"{company_name} ({display_ticker})",
                        "source": source,
                        "source_label": _source_label(source),
                        "source_table": _source_table(source),
                        "source_columns": _source_columns(source),
                        "row_count": int(row.get("row_count") or 0),
                        "max_period": _to_date(row.get("max_period")),
                    }
                )
            log_timing("FORECAST_GET_COMPANIES", (perf_counter() - start) * 1000, f"count={len(companies)}")
            return companies
        except Exception as exc:
            log_structured_error(
                exc,
                page="estimates",
                component="RevenueForecastService",
                operation="GET_COMPANIES",
            )
            return []

    @staticmethod
    def _fetch_actual_rows(ticker: str, source: str) -> List[Dict[str, Any]]:
        if source == "SEC":
            # Strip exchange suffix if present (e.g. "TSCO.L" → "TSCO")
            base = ticker.split('.')[0] if '.' in ticker else ticker
            query = f"""
                SELECT DISTINCT
                    fiscal_date_ending AS period_date,
                    total_revenue,
                    gross_profit,
                    operating_income,
                    net_income,
                    ebitda,
                    reported_currency
                FROM {SEC_SOURCE_TABLE}
                WHERE ticker = :ticker
                  AND report_type = 'annual'
                ORDER BY fiscal_date_ending ASC
            """
            return db_manager.execute_query_readonly(query, {"ticker": base})

        # YFinance: composite tickers (e.g. "TSCO.L") must match yf_symbol; plain tickers use ticker column
        if '.' in ticker:
            query = f"""
                SELECT
                    period_end AS period_date,
                    MAX(CASE WHEN line_item = 'Total Revenue' THEN value END) AS total_revenue,
                    MAX(CASE WHEN line_item = 'Gross Profit' THEN value END) AS gross_profit,
                    MAX(CASE WHEN line_item = 'Operating Income' THEN value END) AS operating_income,
                    MAX(CASE WHEN line_item = 'Net Income' THEN value END) AS net_income,
                    MAX(CASE WHEN line_item = 'EBITDA' THEN value END) AS ebitda
                FROM {YF_SOURCE_TABLE}
                WHERE yf_symbol = :ticker
                  AND frequency = 'annual'
                GROUP BY period_end
                ORDER BY period_end ASC
            """
        else:
            query = f"""
                SELECT
                    period_end AS period_date,
                    MAX(CASE WHEN line_item = 'Total Revenue' THEN value END) AS total_revenue,
                    MAX(CASE WHEN line_item = 'Gross Profit' THEN value END) AS gross_profit,
                    MAX(CASE WHEN line_item = 'Operating Income' THEN value END) AS operating_income,
                    MAX(CASE WHEN line_item = 'Net Income' THEN value END) AS net_income,
                    MAX(CASE WHEN line_item = 'EBITDA' THEN value END) AS ebitda
                FROM {YF_SOURCE_TABLE}
                WHERE ticker = :ticker
                  AND frequency = 'annual'
                GROUP BY period_end
                ORDER BY period_end ASC
            """
        return db_manager.execute_query_readonly(query, {"ticker": ticker})

    @staticmethod
    def _normalize_actual_rows(
        raw_rows: List[Dict[str, Any]],
        source: str,
        source_table: str,
        reported_currency: str,
        display_currency: str = "USD",
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        prev_revenue: Optional[float] = None
        source_columns = _source_columns(source)

        for row in raw_rows:
            period_date = _to_date(row.get("period_date"))
            revenue = _safe_float(row.get("total_revenue"))
            if period_date is None or revenue is None:
                continue

            growth_pct = None
            if prev_revenue not in (None, 0):
                growth_pct = ((revenue - prev_revenue) / abs(prev_revenue)) * 100

            gross_profit = _safe_float(row.get("gross_profit"))
            operating_income = _safe_float(row.get("operating_income"))
            net_income = _safe_float(row.get("net_income"))
            ebitda = _safe_float(row.get("ebitda"))

            normalized = {
                "period_date": period_date,
                "fiscal_year": period_date.year if period_date else None,
                "period_label": period_date.strftime("%b %Y") if period_date else "Unknown",
                "reported_currency": reported_currency,
                "display_currency": display_currency,
                "source": source,
                "source_label": _source_label(source),
                "source_table": source_table,
                "source_columns": source_columns,
                # Store both the raw reported values and the converted display values.
                "total_revenue": revenue,
                "total_revenue_display": revenue,
                "total_revenue_billions": revenue / 1_000_000_000,
                "gross_profit": gross_profit,
                "gross_profit_display": gross_profit,
                "gross_profit_billions": (gross_profit / 1_000_000_000) if gross_profit is not None else None,
                "operating_income": operating_income,
                "operating_income_display": operating_income,
                "operating_income_billions": (operating_income / 1_000_000_000) if operating_income is not None else None,
                "net_income": net_income,
                "net_income_display": net_income,
                "net_income_billions": (net_income / 1_000_000_000) if net_income is not None else None,
                "ebitda": ebitda,
                "ebitda_display": ebitda,
                "ebitda_billions": (ebitda / 1_000_000_000) if ebitda is not None else None,
                "growth_pct": growth_pct,
            }
            rows.append(normalized)
            prev_revenue = revenue

        return rows

    @staticmethod
    @st.cache_data(ttl=86400 * 7, show_spinner=False)  # 1-week TTL; engine version bumps via DB fast path
    def get_company_dashboard(ticker: str, periods: int = 5) -> Dict[str, Any]:
        start = perf_counter()
        source = get_company_source(ticker)
        source_candidates = [source] if source in ("SEC", "YFinance") else ["SEC", "YFinance"]
        company_map = {row["ticker"]: row for row in RevenueForecastService.get_companies()}
        company_name = company_map.get(ticker, {}).get("name", ticker)
        # For composite tickers (e.g. "TSCO.L"), currency lookup uses base ticker
        _base_ticker = ticker.split('.')[0] if '.' in ticker else ticker
        # Composite ticker for forecast store — coreiq_model_forecasts uses 'ADS.DE' not 'ADS'
        if source == 'YFinance' and '.' not in ticker:
            from data.repository import CompanyRepository as _CR
            _exch = (_CR.get_companies_map().get(ticker) or {}).get('exchange_acronym')
            _forecast_ticker = f"{ticker}.{_exch}" if _exch else ticker
        else:
            _forecast_ticker = ticker

        raw_rows: List[Dict[str, Any]] = []
        resolved_source = None
        reported_currency = "USD"
        display_currency = "USD"
        source_table = SEC_SOURCE_TABLE
        source_columns = _source_columns("SEC")

        load_start = perf_counter()
        for candidate in source_candidates:
            try:
                if candidate == "YFinance":
                    reported_currency = _yf_reported_currency(_base_ticker)
                candidate_rows = RevenueForecastService._fetch_actual_rows(ticker, candidate)
                if candidate_rows:
                    raw_rows = candidate_rows
                    resolved_source = candidate
                    source_table = _source_table(candidate)
                    source_columns = _source_columns(candidate)
                    if candidate == "SEC":
                        reported_currency = (
                            candidate_rows[0].get("reported_currency") or "USD"
                        )
                    break
            except Exception as exc:
                log_structured_error(
                    exc,
                    page="estimates",
                    component="RevenueForecastService",
                    operation="FETCH_ACTUAL_ROWS",
                    context={"ticker": ticker, "source": candidate},
                )

        if not raw_rows or resolved_source is None:
            log_timing(
                "FORECAST_LOAD_ACTUALS",
                (perf_counter() - load_start) * 1000,
                f"ticker={ticker} rows=0 source=missing",
            )
            return {
                "ticker": ticker,
                "company_name": company_name,
                "source": source or "unknown",
                "source_label": _source_label(source),
                "source_table": source_table,
                "source_columns": source_columns,
                "reported_currency": reported_currency,
                "display_currency": display_currency,
                "actual_rows": [],
                "historical_rows": [],
                "backtest_rows": [],
                "forecast_rows": [],
                "raw_forecasts": {},
                "summary": {},
                "best_method": None,
            }

        # Keep original currency/values (no FX conversion). If reported_currency is blank, treat as USD.
        if not reported_currency:
            reported_currency = "USD"
        display_currency = reported_currency

        actual_rows = RevenueForecastService._normalize_actual_rows(
            raw_rows=raw_rows,
            source=resolved_source,
            source_table=source_table,
            reported_currency=reported_currency,
            display_currency=display_currency,
        )
        load_elapsed = (perf_counter() - load_start) * 1000
        log_timing(
            "FORECAST_LOAD_ACTUALS",
            load_elapsed,
            f"ticker={ticker} rows={len(actual_rows)} source={resolved_source}",
        )

        _raw_df = pd.DataFrame(
            {
                "year": [row["fiscal_year"] for row in actual_rows],
                "sales": [row["total_revenue_billions"] for row in actual_rows],
            }
        ).dropna()
        _deduped = (
            _raw_df
            .loc[_raw_df.groupby("year")["sales"].transform("max") == _raw_df["sales"]]
            .drop_duplicates(subset=["year"], keep="last")
            .sort_values("year")
            .reset_index(drop=True)
        )
        if not _deduped.empty:
            _threshold = _deduped["sales"].max() * 0.10
            _deduped = _deduped[_deduped["sales"] >= _threshold].reset_index(drop=True)
        model_df = _deduped

        # ── Always run lightweight engine for summary stats + historical ───
        engine = RetailerForecaster.from_dataframe(model_df)

        summary_start = perf_counter()
        summary_stats = engine.summary_stats()
        historical = engine.clean_data.copy()
        historical["growth_pct"] = (historical["sales"].pct_change() * 100).round(1)
        historical["is_outlier"] = historical["year"].isin(engine.outliers)
        summary_elapsed = (perf_counter() - summary_start) * 1000
        log_timing(
            "FORECAST_SUMMARY_STATS",
            summary_elapsed,
            f"ticker={ticker} years={len(historical)} outliers={len(engine.outliers)}",
        )

        # ── DB fast path: skip backtest+forecast if stored data is current ──
        _last_actual_date = actual_rows[-1]["period_date"] if actual_rows else None
        _store_hit = False
        backtest_df: pd.DataFrame = pd.DataFrame()
        forecast_df: pd.DataFrame = pd.DataFrame()
        backtest_elapsed: float = 0.0
        forecast_elapsed: float = 0.0

        if _last_actual_date:
            try:
                from data.forecast_store import needs_update as _needs_update, get_all_model_forecasts as _get_all_mf
                if not _needs_update(_forecast_ticker, _last_actual_date):
                    stored_by_model = _get_all_mf(_forecast_ticker, max_periods=periods)
                    if stored_by_model:
                        # Reconstruct forecast_df from stored values
                        _years: set = set()
                        for _rows in stored_by_model.values():
                            for _r in _rows:
                                _years.add(int(_r["fiscal_year"]))
                        _sorted_years = sorted(_years)[:periods]
                        _fcast_dict: dict = {"year": _sorted_years}
                        for _mkey, _mrows in stored_by_model.items():
                            _by_yr = {int(_r["fiscal_year"]): (float(_r["value_millions"]) / 1000 if _r["value_millions"] is not None else None) for _r in _mrows}
                            _fcast_dict[_mkey] = [_by_yr.get(y) for y in _sorted_years]
                        forecast_df = pd.DataFrame(_fcast_dict)

                        # coreiq_model_forecasts often omits scenario_* rows; merge those series from the
                        # in-memory engine so the Estimates Test tab always gets scenario chart + table.
                        _scen_keys = ("scenario_pessimistic", "scenario_baseline", "scenario_optimistic")
                        _missing_scen = [k for k in _scen_keys if k not in forecast_df.columns]
                        if _missing_scen:
                            try:
                                _fc_full = engine.forecast(periods=periods)
                                _merge_cols = ["year"] + [k for k in _missing_scen if k in _fc_full.columns]
                                if len(_merge_cols) > 1:
                                    _mt = _fc_full[_merge_cols].copy()
                                    _mt["year"] = _mt["year"].astype(int)
                                    _bf = forecast_df.copy()
                                    _bf["year"] = _bf["year"].astype(int)
                                    forecast_df = _bf.merge(_mt, on="year", how="left")
                            except Exception:
                                pass

                        # Approximate backtest rows from stored mape values
                        _bt_rows = []
                        for _mkey, _mrows in stored_by_model.items():
                            if _mrows:
                                _mape_val = _mrows[0].get("mape")
                                _bt_rows.append({
                                    "method_key": _mkey,
                                    "method": _mkey,
                                    "display": _MODEL_FULL_LABELS.get(_mkey, _mkey.replace("_", " ").title()),
                                    "mape": float(_mape_val) if _mape_val is not None else None,
                                    "bias": None,
                                    "rmse": None,
                                })
                        if _bt_rows:
                            backtest_df = pd.DataFrame(_bt_rows).dropna(subset=["mape"]).sort_values("mape")
                        _store_hit = True
                        log_timing("FORECAST_DB_FAST_PATH", (perf_counter() - start) * 1000, f"ticker={ticker} served_from_store=True")
            except Exception:
                pass  # fall through to full engine run

        if not _store_hit:
            backtest_start = perf_counter()
            backtest_df = engine.backtest()
            backtest_elapsed = (perf_counter() - backtest_start) * 1000
            log_timing(
                "FORECAST_BACKTEST",
                backtest_elapsed,
                f"ticker={ticker} models={len(backtest_df)} best={engine.best_method or 'none'}",
            )

            forecast_start = perf_counter()
            forecast_df = engine.forecast(periods=periods)
            forecast_elapsed = (perf_counter() - forecast_start) * 1000
        # Store-fast-path fallback: if we served forecasts from store but do not have usable
        # backtest rows / best method, run a lightweight backtest in-memory (no DB writes).
        if _store_hit and (backtest_df.empty or engine.best_method is None):
            try:
                backtest_start = perf_counter()
                backtest_df = engine.backtest()
                backtest_elapsed = (perf_counter() - backtest_start) * 1000
                log_timing(
                    "FORECAST_BACKTEST_FALLBACK",
                    backtest_elapsed,
                    f"ticker={ticker} models={len(backtest_df)} best={engine.best_method or 'none'} store_hit=True",
                )
            except Exception:
                # If fallback fails, keep store output; UI will show missing best model gracefully.
                pass

            log_timing(
                "FORECAST_FIT",
                forecast_elapsed,
                f"ticker={ticker} years={len(forecast_df)}",
            )

        raw_forecasts: Dict[str, Dict[str, Any]] = {}
        if not _store_hit and hasattr(engine, "forecasts"):
            def _as_list(value: Any) -> List[Any]:
                if value is None:
                    return []
                if hasattr(value, "tolist"):
                    converted = value.tolist()
                    return converted if isinstance(converted, list) else [converted]
                if isinstance(value, list):
                    return value
                return [value]

            for key, payload in engine.forecasts.items():
                raw_forecasts[key] = {
                    "method": payload.get("method"),
                    "years": _as_list(payload.get("years")),
                    "forecast": _as_list(payload.get("forecast")),
                    "ci_lower": _as_list(payload.get("ci_lower")),
                    "ci_upper": _as_list(payload.get("ci_upper")),
                    "r_squared": payload.get("r_squared"),
                    "slope": payload.get("slope"),
                    "cagr": payload.get("cagr"),
                }

        backtest_rows = backtest_df.to_dict("records") if not backtest_df.empty else []
        forecast_rows = forecast_df.to_dict("records") if not forecast_df.empty else []
        historical_rows = historical.to_dict("records") if not historical.empty else []

        forecast_min_year = forecast_df["year"].iloc[0] if not forecast_df.empty else None
        forecast_max_year = forecast_df["year"].iloc[-1] if not forecast_df.empty else None
        next_forecast_value = forecast_df["ensemble"].iloc[0] if "ensemble" in forecast_df.columns and not forecast_df.empty else None

        best_method_display = None
        best_mape = None
        best_method_key: Optional[str] = engine.best_method  # set by backtest(); None on fast path
        if backtest_rows:
            _valid_rows = [row for row in backtest_rows if row.get("mape") is not None]
            if _valid_rows:
                best_row = min(_valid_rows, key=lambda row: row.get("mape", float("inf")))
                best_method_display = best_row.get("display")
                best_mape = best_row.get("mape")
                if best_method_key is None:
                    best_method_key = best_row.get("method_key") or best_row.get("method")

        available_models = [
            col for col in forecast_df.columns if col != "year"
        ]

        # Fire-and-forget: only upsert when we ran the full engine (not from store)
        if not _store_hit:
            _df_copy = forecast_df.copy()
            _bt_copy = list(backtest_rows)
            _models  = list(available_models)
            _best    = best_method_key or ""

            def _smart_upsert():
                try:
                    from data.forecast_store import upsert_forecasts as _do_upsert
                    _do_upsert(
                        ticker=_forecast_ticker,
                        forecast_df=_df_copy,
                        model_keys=_models,
                        best_method_key=_best,
                        backtest_rows=_bt_copy,
                        metric="total_revenue",
                        last_actual_date=_last_actual_date,
                    )
                except Exception:
                    pass

        if not _store_hit:
            try:
                threading.Thread(target=_smart_upsert, daemon=True, name=f"fcst_upsert_{ticker}").start()
            except Exception:
                pass

        total_elapsed = (perf_counter() - start) * 1000
        log_timing(
            "FORECAST_TOTAL",
            total_elapsed,
            f"ticker={ticker} source={resolved_source} models={len(available_models)} store_hit={_store_hit} forecast_years={len(forecast_rows)}",
        )

        latest_actual = actual_rows[-1] if actual_rows else {}

        return {
            "ticker": ticker,
            "company_name": company_name,
            "source": resolved_source,
            "source_label": _source_label(resolved_source),
            "source_table": source_table,
            "source_columns": source_columns,
            "reported_currency": reported_currency,
            "display_currency": display_currency,
            "actual_rows": actual_rows,
            "historical_rows": historical_rows,
            "backtest_rows": backtest_rows,
            "forecast_rows": forecast_rows,
            "raw_forecasts": raw_forecasts,
            "best_method": best_method_key,
            "best_method_display": best_method_display,
            "best_mape": best_mape,
            "available_models": available_models,
            "outlier_years": list(engine.outliers),
            "timings_ms": {
                "load_actuals": round(load_elapsed, 2),
                "summary_stats": round(summary_elapsed, 2),
                "backtest": round(backtest_elapsed, 2),
                "forecast": round(forecast_elapsed, 2),
                "total": round(total_elapsed, 2),
            },
            "summary": {
                "latest_actual_date": latest_actual.get("period_date"),
                "latest_actual_revenue": latest_actual.get("total_revenue"),
                "latest_actual_revenue_billions": latest_actual.get("total_revenue_billions"),
                "display_currency": display_currency,
                "forecast_start_year": forecast_min_year,
                "forecast_end_year": forecast_max_year,
                "next_forecast_value": next_forecast_value,
                "next_forecast_value_billions": next_forecast_value,
                "forecast_periods": len(forecast_rows),
                "historical_rows": len(historical_rows),
                "actual_rows": len(actual_rows),
                "outliers_excluded": len(engine.outliers),
                "cagr_pct": summary_stats.get("cagr_pct"),
                "avg_annual_growth_pct": summary_stats.get("avg_annual_growth_pct"),
                "median_annual_growth_pct": summary_stats.get("median_annual_growth_pct"),
                "growth_volatility_pct": summary_stats.get("growth_volatility_pct"),
                "min_growth_pct": summary_stats.get("min_growth_pct"),
                "max_growth_pct": summary_stats.get("max_growth_pct"),
                "total_growth_pct": summary_stats.get("total_growth_pct"),
                "first_year": summary_stats.get("first_year"),
                "last_year": summary_stats.get("last_year"),
                "best_mape": best_mape,
                "best_method_display": best_method_display,
                "source_note": (
                    "SEC actuals from coreiq_av_financials_income_statement"
                    if resolved_source == "SEC"
                    else "YFinance annual revenue pivoted from coreiq_yf_financials_income_statement"
                ),
                "source_table": source_table,
                "source_columns": source_columns,
                "latest_period_label": latest_actual.get("period_label"),
                "reported_currency": reported_currency,
            },
        }

    # =====================================================================
    # QUARTERLY  — sibling of get_company_dashboard, never touches the
    # annual code path. Reads quarterly actuals and runs the seasonal engine.
    # =====================================================================

    @staticmethod
    def _fetch_quarterly_rows(ticker: str, source: str) -> List[Dict[str, Any]]:
        if source == "SEC":
            base = ticker.split('.')[0] if '.' in ticker else ticker
            return db_manager.execute_query_readonly(
                f"""
                SELECT DISTINCT fiscal_date_ending AS period_date, total_revenue,
                       reported_currency
                FROM {SEC_SOURCE_TABLE}
                WHERE ticker = :ticker AND report_type = 'quarterly'
                  AND total_revenue IS NOT NULL
                ORDER BY fiscal_date_ending ASC
                """,
                {"ticker": base},
            )
        # YFinance: composite tickers match yf_symbol; plain tickers use the ticker column
        col = "yf_symbol" if '.' in ticker else "ticker"
        return db_manager.execute_query_readonly(
            f"""
            SELECT period_end AS period_date,
                   MAX(CASE WHEN line_item = 'Total Revenue' THEN value END) AS total_revenue
            FROM {YF_SOURCE_TABLE}
            WHERE {col} = :ticker AND frequency = 'quarterly'
            GROUP BY period_end
            ORDER BY period_end ASC
            """,
            {"ticker": ticker},
        )

    @staticmethod
    def _normalize_quarterly_rows(
        raw_rows: List[Dict[str, Any]],
        source: str,
        reported_currency: str,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        prev_revenue: Optional[float] = None
        for row in raw_rows:
            period_date = _to_date(row.get("period_date"))
            revenue = _safe_float(row.get("total_revenue"))
            if period_date is None or revenue is None:
                continue
            quarter = (period_date.month - 1) // 3 + 1
            growth_pct = None
            if prev_revenue not in (None, 0):
                growth_pct = ((revenue - prev_revenue) / abs(prev_revenue)) * 100
            rows.append({
                "period_date": period_date,
                "fiscal_year": period_date.year,
                "fiscal_quarter": quarter,
                "label": f"Q{quarter} {period_date.year}",
                "period_label": period_date.strftime("%b %Y"),
                "reported_currency": reported_currency,
                "source": source,
                "source_label": _source_label(source),
                "total_revenue": revenue,
                "total_revenue_billions": revenue / 1_000_000_000,
                "growth_pct": growth_pct,
            })
            prev_revenue = revenue
        return rows

    @staticmethod
    @st.cache_data(ttl=86400 * 7, show_spinner=False)
    def get_quarterly_dashboard(ticker: str, periods: int = 20) -> Dict[str, Any]:
        """Quarterly revenue forecast dashboard. Same shape as get_company_dashboard,
        with forecast rows carrying fiscal_quarter + a 'Qx YYYY' label."""
        start = perf_counter()
        source = get_company_source(ticker)
        source_candidates = [source] if source in ("SEC", "YFinance") else ["SEC", "YFinance"]
        company_map = {row["ticker"]: row for row in RevenueForecastService.get_companies()}
        company_name = company_map.get(ticker, {}).get("name", ticker)
        _base_ticker = ticker.split('.')[0] if '.' in ticker else ticker

        # Composite ticker for the quarterly store (e.g. 'ADS.DE' not 'ADS')
        if source == 'YFinance' and '.' not in ticker:
            from data.repository import CompanyRepository as _CR
            _exch = (_CR.get_companies_map().get(ticker) or {}).get('exchange_acronym')
            _forecast_ticker = f"{ticker}.{_exch}" if _exch else ticker
        else:
            _forecast_ticker = ticker

        empty = {
            "ticker": ticker, "company_name": company_name,
            "source": source or "unknown", "source_label": _source_label(source),
            "reported_currency": "USD", "display_currency": "USD",
            "period_type": "quarterly", "actual_rows": [], "historical_rows": [],
            "backtest_rows": [], "forecast_rows": [], "best_method": None, "summary": {},
        }

        raw_rows: List[Dict[str, Any]] = []
        resolved_source = None
        reported_currency = "USD"
        for candidate in source_candidates:
            try:
                if candidate == "YFinance":
                    reported_currency = _yf_reported_currency(_base_ticker)
                candidate_rows = RevenueForecastService._fetch_quarterly_rows(ticker, candidate)
                if candidate_rows:
                    raw_rows = candidate_rows
                    resolved_source = candidate
                    if candidate == "SEC":
                        reported_currency = candidate_rows[0].get("reported_currency") or "USD"
                    break
            except Exception as exc:
                log_structured_error(
                    exc, page="estimates", component="RevenueForecastService",
                    operation="FETCH_QUARTERLY_ROWS", context={"ticker": ticker, "source": candidate},
                )

        if not raw_rows or resolved_source is None:
            return empty
        if not reported_currency:
            reported_currency = "USD"

        actual_rows = RevenueForecastService._normalize_quarterly_rows(
            raw_rows=raw_rows, source=resolved_source, reported_currency=reported_currency,
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
            _threshold = _deduped["sales"].max() * 0.10
            _deduped = _deduped[_deduped["sales"] >= _threshold].reset_index(drop=True)
        if len(_deduped) < 8:
            empty["summary"] = {"actual_rows": len(actual_rows),
                                "note": "Need >=8 quarters of revenue for a quarterly forecast."}
            empty["actual_rows"] = actual_rows
            return empty

        engine = RetailerQuarterlyForecaster.from_dataframe(_deduped)
        summary_stats = engine.summary_stats()
        historical = engine.clean_data.copy()
        historical["growth_pct"] = (historical["sales"].pct_change() * 100).round(1)
        historical["is_outlier"] = historical["qidx"].isin(engine.outliers)

        _last_actual_date = actual_rows[-1]["period_date"] if actual_rows else None
        forecast_df: pd.DataFrame = pd.DataFrame()
        backtest_df: pd.DataFrame = pd.DataFrame()
        _store_hit = False

        # ── DB fast path: serve from the quarterly store when data is current ──
        if _last_actual_date:
            try:
                from data.quarterly_forecast_store import (
                    needs_update as _needs_update, get_all_model_forecasts as _get_all_mf,
                )
                if not _needs_update(_forecast_ticker, _last_actual_date):
                    stored = _get_all_mf(_forecast_ticker, max_periods=periods)
                    if stored:
                        _keys = sorted({(int(r["fiscal_year"]), int(r["fiscal_quarter"]))
                                        for rows in stored.values() for r in rows})[:periods]
                        fdict: Dict[str, Any] = {
                            "fiscal_year": [k[0] for k in _keys],
                            "fiscal_quarter": [k[1] for k in _keys],
                            "label": [f"Q{k[1]} {k[0]}" for k in _keys],
                        }
                        for mkey, mrows in stored.items():
                            by_key = {(int(r["fiscal_year"]), int(r["fiscal_quarter"])):
                                      (float(r["value_millions"]) / 1000 if r["value_millions"] is not None else None)
                                      for r in mrows}
                            fdict[mkey] = [by_key.get(k) for k in _keys]
                        forecast_df = pd.DataFrame(fdict)
                        _bt_rows = []
                        for mkey, mrows in stored.items():
                            if mrows and mrows[0].get("mape") is not None:
                                _bt_rows.append({"method_key": mkey, "method": mkey,
                                                 "mape": float(mrows[0]["mape"])})
                        if _bt_rows:
                            backtest_df = pd.DataFrame(_bt_rows).sort_values("mape")
                        _store_hit = True
            except Exception:
                pass  # fall through to a full engine run

        if not _store_hit:
            backtest_df = engine.backtest(holdout_quarters=4)
            forecast_df = engine.forecast(periods=periods)

        # ── Normalize the forecast frame to the Annual dashboard shape ───────
        # x-axis key 'year' = qidx (fiscal_year*4 + fiscal_quarter); 'period_label'
        # = 'Q# YYYY'. Only model + scenario columns remain besides those two, so the
        # page's Overview/Models/Test helpers see exactly the same structure as Annual.
        def _lst(v):
            if v is None:
                return []
            if hasattr(v, "tolist"):
                out = v.tolist()
                return out if isinstance(out, list) else [out]
            return v if isinstance(v, list) else [v]

        raw_forecasts: Dict[str, Any] = {}
        if not forecast_df.empty:
            fdf = forecast_df.copy()
            fdf["year"] = fdf["fiscal_year"].astype(int) * 4 + fdf["fiscal_quarter"].astype(int)
            fdf["period_label"] = [f"Q{int(q)} {int(y)}"
                                   for y, q in zip(fdf["fiscal_year"], fdf["fiscal_quarter"])]
            if not _store_hit and getattr(engine, "forecasts", None):
                for mkey, mp in engine.forecasts.items():
                    raw_forecasts[mkey] = {
                        "method": mkey,
                        "years": _lst(mp.get("qidx")),
                        "forecast": _lst(mp.get("forecast")),
                        "ci_lower": _lst(mp.get("ci_lower")),
                        "ci_upper": _lst(mp.get("ci_upper")),
                        "r_squared": mp.get("r_squared"),
                        "cagr": mp.get("qoq_growth"),
                    }
            _keep = ["year", "period_label"] + [c for c in fdf.columns
                     if c in _QUARTERLY_MODEL_KEYS or c.startswith("scenario_")]
            forecast_df = fdf[_keep].sort_values("year").reset_index(drop=True)

        # Historical rows keyed to qidx for the shared frame helpers
        historical_rows: List[Dict[str, Any]] = []
        for _, r in historical.iterrows():
            historical_rows.append({
                "year": int(r["qidx"]),
                "sales": float(r["sales"]) if pd.notna(r["sales"]) else None,
                "growth_pct": float(r["growth_pct"]) if pd.notna(r["growth_pct"]) else None,
                "is_outlier": bool(r["is_outlier"]),
                "period_label": r["label"],
                "cal_year": int(r["year"]),
            })

        # Actual rows carry qidx + quarter label so the overview/company frame align
        for r in actual_rows:
            r["year"] = int(r["fiscal_year"]) * 4 + int(r["fiscal_quarter"])
            r["qidx"] = r["year"]
            r["period_label"] = r["label"]

        # Enrich backtest rows with method_key + display (matches the Annual contract)
        backtest_rows: List[Dict[str, Any]] = []
        if not backtest_df.empty:
            _has_bias = "bias" in backtest_df.columns
            _has_rmse = "rmse" in backtest_df.columns
            for _, r in backtest_df.iterrows():
                mk = r.get("method")
                backtest_rows.append({
                    "method": mk,
                    "method_key": r.get("method_key", mk),
                    "display": _MODEL_FULL_LABELS.get(mk, str(mk).replace("_", " ").title()),
                    "mape": float(r["mape"]) if pd.notna(r.get("mape")) else None,
                    "bias": (float(r["bias"]) if _has_bias and pd.notna(r.get("bias")) else None),
                    "rmse": (float(r["rmse"]) if _has_rmse and pd.notna(r.get("rmse")) else None),
                })

        forecast_rows = forecast_df.to_dict("records") if not forecast_df.empty else []
        outlier_qidx = [int(x) for x in engine.outliers]

        best_method_key: Optional[str] = engine.best_method
        best_method_display = None
        best_mape = None
        if backtest_rows:
            _valid = [r for r in backtest_rows if r.get("mape") is not None]
            if _valid:
                _best_row = min(_valid, key=lambda r: r["mape"])
                best_method_display = _best_row.get("display")
                best_mape = _best_row.get("mape")
                if best_method_key is None:
                    best_method_key = _best_row.get("method_key") or _best_row.get("method")

        # Page uses only the models (no scenarios) for the Model tabs; the store
        # persists both models and scenarios.
        available_models = [c for c in forecast_df.columns
                            if c not in ("year", "period_label") and not c.startswith("scenario_")]
        store_model_keys = [c for c in forecast_df.columns if c not in ("year", "period_label")]

        # Fire-and-forget upsert only on a full engine run (mirrors annual path).
        # The store needs fiscal_year/fiscal_quarter — reconstruct them from qidx.
        if not _store_hit and forecast_rows:
            _df_copy = forecast_df.copy()
            _df_copy["fiscal_quarter"] = ((_df_copy["year"] - 1) % 4 + 1).astype(int)
            _df_copy["fiscal_year"] = ((_df_copy["year"] - _df_copy["fiscal_quarter"]) // 4).astype(int)
            _bt_copy = [{"method_key": r.get("method_key"), "mape": r.get("mape")} for r in backtest_rows]
            _models = list(store_model_keys)
            _best = best_method_key or ""

            def _smart_upsert():
                try:
                    from data.quarterly_forecast_store import (
                        ensure_quarterly_forecast_table, upsert_forecasts as _do_upsert,
                        prune_stale_quarters as _prune,
                    )
                    ensure_quarterly_forecast_table()
                    _do_upsert(
                        ticker=_forecast_ticker, forecast_df=_df_copy, model_keys=_models,
                        best_method_key=_best, backtest_rows=_bt_copy, metric="total_revenue",
                        last_actual_date=_last_actual_date,
                        company_name=company_name or None,
                    )
                    _prune(_forecast_ticker, _last_actual_date)
                except Exception:
                    pass

            try:
                threading.Thread(target=_smart_upsert, daemon=True,
                                 name=f"q_fcst_upsert_{ticker}").start()
            except Exception:
                pass

        # qidx → 'Q# YYYY' map for the chart x-axis (history + forecast)
        period_labels: Dict[int, str] = {}
        for _hr in historical_rows:
            period_labels[int(_hr["year"])] = _hr["period_label"]
        for _fr in forecast_rows:
            period_labels[int(_fr["year"])] = _fr["period_label"]

        next_q = forecast_rows[0] if forecast_rows else {}
        latest_actual = actual_rows[-1] if actual_rows else {}
        forecast_min = forecast_rows[0]["year"] if forecast_rows else None
        forecast_max = forecast_rows[-1]["year"] if forecast_rows else None
        log_timing("FORECAST_QUARTERLY_TOTAL", (perf_counter() - start) * 1000,
                   f"ticker={ticker} source={resolved_source} store_hit={_store_hit} "
                   f"quarters={len(forecast_rows)}")

        return {
            "ticker": ticker,
            "company_name": company_name,
            "source": resolved_source,
            "source_label": _source_label(resolved_source),
            "reported_currency": reported_currency,
            "display_currency": reported_currency,
            "period_type": "quarterly",
            "period_labels": period_labels,
            "actual_rows": actual_rows,
            "historical_rows": historical_rows,
            "backtest_rows": backtest_rows,
            "forecast_rows": forecast_rows,
            "raw_forecasts": raw_forecasts,
            "best_method": best_method_key,
            "best_method_display": best_method_display,
            "best_mape": best_mape,
            "available_models": available_models,
            "outlier_years": outlier_qidx,
            "summary": {
                "latest_actual_date": latest_actual.get("period_date"),
                "latest_actual_label": latest_actual.get("period_label"),
                "latest_actual_revenue_billions": latest_actual.get("total_revenue_billions"),
                "display_currency": reported_currency,
                "reported_currency": reported_currency,
                "forecast_start_year": forecast_min,
                "forecast_end_year": forecast_max,
                "forecast_start_label": next_q.get("period_label"),
                "forecast_end_label": forecast_rows[-1].get("period_label") if forecast_rows else None,
                "next_forecast_value": next_q.get("ensemble"),
                "next_forecast_value_billions": next_q.get("ensemble"),
                "forecast_periods": len(forecast_rows),
                "historical_rows": len(historical_rows),
                "actual_rows": len(actual_rows),
                "outliers_excluded": len(outlier_qidx),
                "best_mape": best_mape,
                "best_method_display": best_method_display,
                "cagr_pct": summary_stats.get("annual_cagr_pct"),
                "avg_annual_growth_pct": summary_stats.get("avg_qoq_growth_pct"),
                "median_annual_growth_pct": summary_stats.get("median_qoq_growth_pct"),
                "growth_volatility_pct": summary_stats.get("growth_volatility_pct"),
                "min_growth_pct": summary_stats.get("min_qoq_growth_pct"),
                "max_growth_pct": summary_stats.get("max_qoq_growth_pct"),
                "total_growth_pct": summary_stats.get("total_growth_pct"),
                "seasonal_indices": summary_stats.get("seasonal_indices"),
                "source_note": ("SEC quarterly actuals from coreiq_av_financials_income_statement"
                                if resolved_source == "SEC"
                                else "YFinance quarterly revenue pivoted from coreiq_yf_financials_income_statement"),
            },
        }


# Backward-compatible alias for older imports.
RevenueEstimatesService = RevenueForecastService
