"""
One-time script: audit duplicate years and rerun forecast engine for specified tickers.
Clears old rows in coreiq_model_forecasts and upserts fresh values.

Usage: python scripts/rerun_forecasts.py
"""
import os, sys, datetime

# ── Bootstrap app path ────────────────────────────────────────────────────────
_root = os.path.join(os.path.dirname(__file__), "..", "app")
os.chdir(_root)
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(os.path.join(_root, "..", ".env"))

# Stub out streamlit.cache_data so imports don't error
import types, unittest.mock
_st_stub = types.ModuleType("streamlit")
_st_stub.cache_data = lambda *a, **kw: (lambda f: f)
_st_stub.session_state = {}
sys.modules.setdefault("streamlit", _st_stub)

from core.database import db_manager
from utils.retailer_forecaster import RetailerForecaster
import pandas as pd

TICKERS_META = {
    "ELF": "SEC",
    "NXT": "YFinance",
    "RKT": "YFinance",
}

SEC_TABLE = "coreiq_av_financials_income_statement"
YF_TABLE  = "coreiq_yf_financials_income_statement"
METRIC    = "total_revenue"


# ─────────────────────────────────────────────────────────────────────────────
# 1. AUDIT — prove duplicates exist in DB (for Data Team report)
# ─────────────────────────────────────────────────────────────────────────────
def audit_duplicates():
    print("\n" + "=" * 72)
    print("STEP 1 — DUPLICATE YEAR AUDIT (DB proof for Data Team)")
    print("=" * 72)

    for ticker, source in TICKERS_META.items():
        if source == "SEC":
            q = """
                SELECT YEAR(fiscal_date_ending) AS yr,
                       COUNT(*) AS cnt,
                       GROUP_CONCAT(fiscal_date_ending ORDER BY fiscal_date_ending) AS dates,
                       GROUP_CONCAT(total_revenue    ORDER BY fiscal_date_ending) AS revenues
                FROM   coreiq_av_financials_income_statement
                WHERE  ticker = :t AND report_type = 'annual' AND total_revenue IS NOT NULL
                GROUP  BY yr
                HAVING cnt > 1
                ORDER  BY yr
            """
        else:
            q = """
                SELECT YEAR(period_end) AS yr,
                       COUNT(*) AS cnt,
                       GROUP_CONCAT(period_end ORDER BY period_end) AS dates,
                       GROUP_CONCAT(value      ORDER BY period_end) AS revenues
                FROM   coreiq_yf_financials_income_statement
                WHERE  ticker = :t AND frequency = 'annual'
                  AND  line_item = 'Total Revenue' AND value IS NOT NULL
                GROUP  BY yr
                HAVING cnt > 1
                ORDER  BY yr
            """
        rows = db_manager.execute_query_readonly(q, {"t": ticker})
        flag = " *** DUPLICATES FOUND — DATA TEAM ACTION NEEDED ***" if rows else ""
        print(f"\n[{ticker}] {source}{flag}")
        if rows:
            for r in rows:
                rev_list = [f"{float(x):,.0f}" for x in r["revenues"].split(",")]
                print(f"  Year {r['yr']}: {r['cnt']} rows")
                print(f"    dates   : {r['dates']}")
                print(f"    revenues: {', '.join(rev_list)}")
        else:
            print("  No duplicates — DB data is clean.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. FETCH ACTUALS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_actuals(ticker: str, source: str):
    if source == "SEC":
        rows = db_manager.execute_query_readonly(f"""
            SELECT fiscal_date_ending AS period_date,
                   YEAR(fiscal_date_ending) AS fiscal_year,
                   total_revenue / 1e9     AS total_revenue_billions
            FROM   {SEC_TABLE}
            WHERE  ticker = :t AND report_type = 'annual' AND total_revenue IS NOT NULL
            ORDER  BY fiscal_date_ending ASC
        """, {"t": ticker})
    else:
        rows = db_manager.execute_query_readonly(f"""
            SELECT period_end AS period_date,
                   YEAR(period_end) AS fiscal_year,
                   value / 1e9      AS total_revenue_billions
            FROM   {YF_TABLE}
            WHERE  ticker = :t AND frequency = 'annual'
              AND  line_item = 'Total Revenue' AND value IS NOT NULL
            ORDER  BY period_end ASC
        """, {"t": ticker})
    return [dict(r) for r in (rows or [])]


# ─────────────────────────────────────────────────────────────────────────────
# 3. BUILD MODEL DF (same dedup logic as fixed revenue_forecast_service.py)
# ─────────────────────────────────────────────────────────────────────────────
def build_model_df(actual_rows):
    raw = pd.DataFrame({
        "year":  [r["fiscal_year"] for r in actual_rows],
        "sales": [r["total_revenue_billions"] for r in actual_rows],
    }).dropna()
    # Keep max-revenue row per year — handles ELF fiscal-year transition (keeps full year),
    # NXT phantom Dec-31 rows (keeps Jan-31 full year), and RKT duplicate zeros.
    deduped = (
        raw
        .loc[raw.groupby("year")["sales"].transform("max") == raw["sales"]]
        .drop_duplicates(subset=["year"], keep="last")
        .sort_values("year")
        .reset_index(drop=True)
    )
    if not deduped.empty:
        threshold = deduped["sales"].max() * 0.10
        deduped = deduped[deduped["sales"] >= threshold].reset_index(drop=True)
    return deduped


# ─────────────────────────────────────────────────────────────────────────────
# 4. CLEAR OLD DB ROWS
# ─────────────────────────────────────────────────────────────────────────────
def clear_stored_forecasts(ticker: str):
    affected = db_manager.execute_insert(
        "DELETE FROM coreiq_model_forecasts WHERE ticker = :t AND metric = :m",
        {"t": ticker, "m": METRIC},
    )
    print(f"  Cleared {affected} old rows for {ticker}.")


# ─────────────────────────────────────────────────────────────────────────────
# 5. UPSERT NEW FORECASTS
# ─────────────────────────────────────────────────────────────────────────────
MODEL_KEYS = ["linear", "cagr", "exp_smoothing", "holt", "ma_trend", "weighted_avg", "ensemble"]

def upsert_new_forecasts(ticker: str, engine: RetailerForecaster, actual_rows, forecast_df, backtest_df):
    last_actual_date = None
    if actual_rows:
        d = actual_rows[-1]["period_date"]
        last_actual_date = d.date() if hasattr(d, "date") else d

    best_method = None
    if backtest_df is not None and not backtest_df.empty and "mape" in backtest_df.columns:
        valid = backtest_df.dropna(subset=["mape"])
        if not valid.empty:
            best_method = valid.loc[valid["mape"].idxmin(), "method"]

    mape_by_key = {}
    if backtest_df is not None and not backtest_df.empty:
        for _, row in backtest_df.iterrows():
            key = row.get("method") or row.get("method_key")
            if key and row.get("mape") is not None:
                mape_by_key[key] = float(row["mape"])

    computed_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    last_actual_str = last_actual_date.strftime("%Y-%m-%d") if last_actual_date else None

    rows = []
    year_col = "year"
    for model_key in MODEL_KEYS:
        if model_key not in forecast_df.columns:
            continue
        series = forecast_df[[year_col, model_key]].dropna()
        if series.empty:
            continue
        is_ensemble = 1 if model_key == "ensemble" else 0
        is_best     = 1 if model_key == best_method else 0
        mape        = mape_by_key.get(model_key)
        first_fy    = int(series[year_col].min())
        for _, r in series.iterrows():
            fy   = int(r[year_col])
            val  = float(r[model_key]) * 1000  # billions → millions
            rows.append({
                "ticker":           ticker,
                "fiscal_year":      fy,
                "metric":           METRIC,
                "model_key":        model_key,
                "value_millions":   round(val, 4),
                "is_best_model":    is_best,
                "is_ensemble":      is_ensemble,
                "mape":             round(mape, 4) if mape is not None else None,
                "periods_ahead":    fy - first_fy + 1,
                "computed_at":      computed_at,
                "last_actual_date": last_actual_str,
            })

    if not rows:
        print(f"  No rows to upsert for {ticker}.")
        return 0

    placeholders = ", ".join(
        f"(:ticker_{i}, :fiscal_year_{i}, :metric_{i}, :model_key_{i}, "
        f":value_millions_{i}, :is_best_model_{i}, :is_ensemble_{i}, "
        f":mape_{i}, :periods_ahead_{i}, :computed_at_{i}, :last_actual_date_{i})"
        for i in range(len(rows))
    )
    flat = {}
    for i, row in enumerate(rows):
        for k, v in row.items():
            flat[f"{k}_{i}"] = v

    sql = f"""
        INSERT INTO coreiq_model_forecasts
            (ticker, fiscal_year, metric, model_key,
             value_millions, is_best_model, is_ensemble,
             mape, periods_ahead, computed_at, last_actual_date)
        VALUES {placeholders}
        ON DUPLICATE KEY UPDATE
            value_millions    = VALUES(value_millions),
            is_best_model     = VALUES(is_best_model),
            is_ensemble       = VALUES(is_ensemble),
            mape              = VALUES(mape),
            periods_ahead     = VALUES(periods_ahead),
            computed_at       = VALUES(computed_at),
            last_actual_date  = VALUES(last_actual_date)
    """
    affected = db_manager.execute_insert(sql, flat)
    print(f"  Upserted {len(rows)} rows ({affected} DB rows affected).")
    return affected


# ─────────────────────────────────────────────────────────────────────────────
# 6. MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    audit_duplicates()

    print("\n" + "=" * 72)
    print("STEP 2 — RERUN ENGINE & UPDATE DB")
    print("=" * 72)

    for ticker, source in TICKERS_META.items():
        print(f"\n[{ticker}] fetching actuals from {source}…")
        actual_rows = fetch_actuals(ticker, source)
        if not actual_rows:
            print(f"  No actuals found — skipping.")
            continue

        model_df = build_model_df(actual_rows)
        print(f"  {len(actual_rows)} raw rows → {len(model_df)} deduplicated years: {sorted(model_df['year'].tolist())}")

        engine = RetailerForecaster.from_dataframe(model_df)
        backtest_df = engine.backtest(holdout_years=2)
        forecast_df = engine.forecast(periods=5)

        # Show ensemble forecast
        if "ensemble" in forecast_df.columns:
            ens_rows = forecast_df[["year", "ensemble"]].head(5)
            print("  Ensemble FY+1…5:")
            for _, r in ens_rows.iterrows():
                print(f"    FY{int(r['year'])}: {r['ensemble']*1000:>10,.1f} M")

        best = None
        if not backtest_df.empty and "mape" in backtest_df.columns:
            valid = backtest_df.dropna(subset=["mape"])
            if not valid.empty:
                best = valid.loc[valid["mape"].idxmin(), "method"]
        print(f"  Best model: {best}  | MAPE: {backtest_df[backtest_df['method']==best]['mape'].values[0] if best else 'N/A':.2f}%" if best else f"  Best model: {best}")

        print(f"  Clearing old DB rows…")
        clear_stored_forecasts(ticker)

        print(f"  Writing fresh forecasts to DB…")
        upsert_new_forecasts(ticker, engine, actual_rows, forecast_df, backtest_df)

    print("\n✓ Done.")


if __name__ == "__main__":
    main()
