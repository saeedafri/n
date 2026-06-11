"""
Backfill forecasts for all tickers.

Runs the full RetailerForecaster engine (backtest + forecast + scenarios)
for every eligible ticker and stores results in coreiq_model_forecasts.

Usage:
    cd /Users/mohdsaeedafri/Documents/Documents/Code-Base/Real/CapIQReplacement
    python scripts/backfill_forecasts.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from time import perf_counter
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from core.database import db_manager
from utils.retailer_forecaster import RetailerForecaster
from data.forecast_store import upsert_forecasts

SEC_TABLE = "coreiq_av_financials_income_statement"
YF_TABLE  = "coreiq_yf_financials_income_statement"
PERIODS   = 5


def _get_all_tickers() -> List[Dict[str, Any]]:
    rows = db_manager.execute_query_readonly(f"""
        WITH sec_stats AS (
            SELECT ticker, COUNT(DISTINCT fiscal_date_ending) AS row_count, MAX(fiscal_date_ending) AS max_period
            FROM {SEC_TABLE}
            WHERE report_type = 'annual' AND total_revenue IS NOT NULL
            GROUP BY ticker
        ),
        yf_stats AS (
            SELECT ticker, COUNT(DISTINCT period_end) AS row_count, MAX(period_end) AS max_period
            FROM (
                SELECT ticker, period_end
                FROM {YF_TABLE}
                WHERE frequency = 'annual' AND line_item = 'Total Revenue' AND value IS NOT NULL
            ) yf
            GROUP BY ticker
        )
        SELECT
            c.ticker,
            c.name_coresight AS company_name,
            c.source,
            COALESCE(sec_stats.row_count, yf_stats.row_count, 0) AS row_count,
            COALESCE(sec_stats.max_period, yf_stats.max_period) AS max_period
        FROM coreiq_companies c
        LEFT JOIN sec_stats ON sec_stats.ticker = c.ticker
        LEFT JOIN yf_stats  ON yf_stats.ticker  = c.ticker
        WHERE c.source IN ('SEC', 'YFinance')
          AND COALESCE(sec_stats.row_count, yf_stats.row_count, 0) >= 3
        ORDER BY company_name ASC
    """, {})
    return rows or []


def _fetch_actuals(ticker: str, source: str) -> List[Dict[str, Any]]:
    if source == "SEC":
        return db_manager.execute_query_readonly(f"""
            SELECT DISTINCT fiscal_date_ending AS period_date, total_revenue, reported_currency
            FROM {SEC_TABLE}
            WHERE ticker = :ticker AND report_type = 'annual'
            ORDER BY fiscal_date_ending ASC
        """, {"ticker": ticker})
    return db_manager.execute_query_readonly(f"""
        SELECT period_end AS period_date,
               MAX(CASE WHEN line_item = 'Total Revenue' THEN value END) AS total_revenue
        FROM {YF_TABLE}
        WHERE ticker = :ticker AND frequency = 'annual'
        GROUP BY period_end
        ORDER BY period_end ASC
    """, {"ticker": ticker})


def _to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if hasattr(v, "date"):
        return v.date()
    return v


def run_ticker(ticker: str, source: str, company_name: str) -> str:
    raw = _fetch_actuals(ticker, source)
    if not raw:
        return "NO_DATA"

    rows = []
    for r in raw:
        pd_val = _to_date(r.get("period_date"))
        rev    = r.get("total_revenue")
        if pd_val is None or rev is None:
            continue
        try:
            rev = float(rev)
        except (TypeError, ValueError):
            continue
        rows.append({"year": pd_val.year, "sales": rev / 1_000_000_000})

    if len(rows) < 3:
        return "INSUFFICIENT"

    df = pd.DataFrame(rows).drop_duplicates("year").sort_values("year").reset_index(drop=True)
    last_actual_date = _to_date(raw[-1].get("period_date"))

    engine = RetailerForecaster.from_dataframe(df)
    backtest_df = engine.backtest()
    forecast_df = engine.forecast(periods=PERIODS)

    available_models = [col for col in forecast_df.columns if col != "year"]
    best_key = engine.best_method or ""

    backtest_rows = backtest_df.to_dict("records") if not backtest_df.empty else []

    upsert_forecasts(
        ticker=ticker,
        forecast_df=forecast_df,
        model_keys=available_models,
        best_method_key=best_key,
        backtest_rows=backtest_rows,
        metric="total_revenue",
        last_actual_date=last_actual_date,
    )
    return f"OK ({len(available_models)} models, {PERIODS} yrs)"


def main():
    tickers = _get_all_tickers()
    total   = len(tickers)
    print(f"Found {total} eligible tickers\n")

    ok = skipped = errors = 0
    t_start = perf_counter()

    for i, row in enumerate(tickers, 1):
        ticker  = (row.get("ticker") or "").strip()
        source  = (row.get("source") or "").strip()
        name    = (row.get("company_name") or ticker).strip()
        if not ticker or source not in ("SEC", "YFinance"):
            skipped += 1
            continue

        t0 = perf_counter()
        try:
            status = run_ticker(ticker, source, name)
            elapsed = (perf_counter() - t0) * 1000
            if status.startswith("OK"):
                ok += 1
                print(f"[{i:>4}/{total}] ✓  {ticker:<10} {name[:40]:<40} {status}  ({elapsed:.0f}ms)")
            else:
                skipped += 1
                print(f"[{i:>4}/{total}] –  {ticker:<10} {name[:40]:<40} {status}")
        except Exception as e:
            errors += 1
            elapsed = (perf_counter() - t0) * 1000
            print(f"[{i:>4}/{total}] ✗  {ticker:<10} {name[:40]:<40} ERROR: {e}  ({elapsed:.0f}ms)")

    total_elapsed = (perf_counter() - t_start)
    print(f"\nDone in {total_elapsed:.1f}s — OK: {ok}  Skipped: {skipped}  Errors: {errors}")


if __name__ == "__main__":
    main()
