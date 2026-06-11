"""
Full forecast rebuild — run once after source-mapping + forecast_date fixes.

Rules:
  • SEC companies (exchange_acronym = None)  → coreiq_av_financials_income_statement
      stored ticker = original ticker (e.g. "TSCO", "JD")
  • YFinance companies (exchange_acronym present) → match yf_symbol in YF table
      stored ticker = ticker.exchange_acronym (e.g. "TSCO.L", "JD.L")
  • YFinance companies (no exchange_acronym)  → coreiq_yf_financials_income_statement
      stored ticker = original ticker (e.g. "WOW", "BN")

  • forecast_date = last_actual_date with year incremented per period
    (same month/day as fiscal year-end — so WOW Jun-30 → 2026-06-30, 2027-06-30 …)

Usage:
    cd /path/to/CapIQReplacement
    python3 scripts/rebuild_all_forecasts.py
"""
import os, sys, datetime, traceback

os.chdir(os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(os.path.join("..", ".env"))

import types as _types
_st = _types.ModuleType("streamlit")
_st.cache_data = lambda *a, **kw: (lambda f: f)
_st.session_state = {}
sys.modules.setdefault("streamlit", _st)

from core.database import db_manager
from utils.retailer_forecaster import RetailerForecaster
import pandas as pd

METRIC     = "total_revenue"
PERIODS    = 5
MODEL_KEYS = ["linear", "cagr", "exp_smoothing", "holt", "ma_trend", "weighted_avg", "ensemble"]
MIN_YEARS  = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def composite_ticker(ticker: str, exchange_acronym) -> str:
    return f"{ticker}.{exchange_acronym}" if exchange_acronym else ticker


def forecast_date_for(last_actual_date: datetime.date, periods_ahead: int) -> datetime.date:
    """Same month/day as last actual, year += periods_ahead."""
    try:
        return last_actual_date.replace(year=last_actual_date.year + periods_ahead)
    except ValueError:                          # Feb-29 in non-leap year
        return last_actual_date.replace(year=last_actual_date.year + periods_ahead, day=28)


def fetch_sec(ticker: str):
    rows = db_manager.execute_query_readonly("""
        SELECT fiscal_date_ending AS period_date,
               YEAR(fiscal_date_ending) AS fiscal_year,
               total_revenue / 1e9 AS total_revenue_billions
        FROM   coreiq_av_financials_income_statement
        WHERE  ticker = :t AND report_type = 'annual'
          AND  total_revenue IS NOT NULL AND total_revenue > 0
        ORDER  BY fiscal_date_ending ASC
    """, {"t": ticker})
    return [dict(r) for r in (rows or [])]


def fetch_yf(ticker: str, exchange_acronym=None):
    """Use yf_symbol if exchange_acronym given, else plain ticker match."""
    if exchange_acronym:
        sym = f"{ticker}.{exchange_acronym}"
        rows = db_manager.execute_query_readonly("""
            SELECT period_end AS period_date,
                   YEAR(period_end) AS fiscal_year,
                   value / 1e9 AS total_revenue_billions
            FROM   coreiq_yf_financials_income_statement
            WHERE  yf_symbol = :sym AND frequency = 'annual'
              AND  line_item = 'Total Revenue'
              AND  value IS NOT NULL AND value > 0
            ORDER  BY period_end ASC
        """, {"sym": sym})
    else:
        rows = db_manager.execute_query_readonly("""
            SELECT period_end AS period_date,
                   YEAR(period_end) AS fiscal_year,
                   value / 1e9 AS total_revenue_billions
            FROM   coreiq_yf_financials_income_statement
            WHERE  ticker = :t AND frequency = 'annual'
              AND  line_item = 'Total Revenue'
              AND  value IS NOT NULL AND value > 0
            ORDER  BY period_end ASC
        """, {"t": ticker})
    return [dict(r) for r in (rows or [])]


def build_model_df(actual_rows) -> pd.DataFrame:
    raw = pd.DataFrame({
        "year":  [r["fiscal_year"] for r in actual_rows],
        "sales": [r["total_revenue_billions"] for r in actual_rows],
    }).dropna()
    if raw.empty:
        return raw
    deduped = (
        raw
        .loc[raw.groupby("year")["sales"].transform("max") == raw["sales"]]
        .drop_duplicates(subset=["year"], keep="last")
        .sort_values("year")
        .reset_index(drop=True)
    )
    if not deduped.empty:
        deduped = deduped[deduped["sales"] >= deduped["sales"].max() * 0.10].reset_index(drop=True)
    return deduped


def upsert(cticker: str, forecast_df: pd.DataFrame, backtest_df: pd.DataFrame,
           actual_rows: list) -> int:
    last_actual = actual_rows[-1]["period_date"] if actual_rows else None
    if last_actual and hasattr(last_actual, "date"):
        last_actual = last_actual.date()

    mape_map, best = {}, None
    if backtest_df is not None and not backtest_df.empty and "mape" in backtest_df.columns:
        v = backtest_df.dropna(subset=["mape"])
        if not v.empty:
            best = v.loc[v["mape"].idxmin(), "method"]
        for _, row in backtest_df.iterrows():
            k = row.get("method")
            if k and row.get("mape") is not None:
                mape_map[k] = float(row["mape"])

    computed_at  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    last_act_str = last_actual.strftime("%Y-%m-%d") if last_actual else None

    rows = []
    for mk in MODEL_KEYS:
        if mk not in forecast_df.columns:
            continue
        series = forecast_df[["year", mk]].dropna()
        if series.empty:
            continue
        first_fy = int(series["year"].min())
        mape     = mape_map.get(mk)

        for _, r in series.iterrows():
            fy  = int(r["year"])
            val = float(r[mk]) * 1000          # billions → millions
            if abs(val) > 999_999_999_999.9999:
                val = None
            periods_ahead = fy - first_fy + 1
            # forecast_date: same month/day as last actual, year incremented
            fdate_str = None
            if last_actual:
                fdate_str = forecast_date_for(last_actual, periods_ahead).strftime("%Y-%m-%d")

            rows.append({
                "ticker":           cticker,
                "fiscal_year":      fy,
                "forecast_date":    fdate_str,
                "metric":           METRIC,
                "model_key":        mk,
                "value_millions":   round(val, 4) if val is not None else None,
                "is_best_model":    1 if mk == best else 0,
                "is_ensemble":      1 if mk == "ensemble" else 0,
                "mape":             round(mape, 4) if mape is not None else None,
                "periods_ahead":    periods_ahead,
                "computed_at":      computed_at,
                "last_actual_date": last_act_str,
            })

    if not rows:
        return 0

    ph = ", ".join(
        f"(:ticker_{i},:fiscal_year_{i},:forecast_date_{i},:metric_{i},:model_key_{i},"
        f":value_millions_{i},:is_best_model_{i},:is_ensemble_{i},:mape_{i},"
        f":periods_ahead_{i},:computed_at_{i},:last_actual_date_{i})"
        for i in range(len(rows))
    )
    flat = {f"{k}_{i}": v for i, row in enumerate(rows) for k, v in row.items()}

    sql = f"""
        INSERT INTO coreiq_model_forecasts
            (ticker, fiscal_year, forecast_date, metric, model_key,
             value_millions, is_best_model, is_ensemble, mape,
             periods_ahead, computed_at, last_actual_date)
        VALUES {ph}
        ON DUPLICATE KEY UPDATE
            forecast_date     = VALUES(forecast_date),
            value_millions    = VALUES(value_millions),
            is_best_model     = VALUES(is_best_model),
            is_ensemble       = VALUES(is_ensemble),
            mape              = VALUES(mape),
            periods_ahead     = VALUES(periods_ahead),
            computed_at       = VALUES(computed_at),
            last_actual_date  = VALUES(last_actual_date)
    """
    return db_manager.execute_insert(sql, flat)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    companies = db_manager.execute_query_readonly("""
        SELECT ticker, name_coresight AS name, source, exchange_acronym
        FROM   coreiq_companies
        WHERE  source IN ('SEC', 'YFinance')
        ORDER  BY ticker ASC, source ASC
    """, {})
    total = len(companies)
    sec_cnt = sum(1 for c in companies if c["source"] == "SEC")
    yf_cnt  = sum(1 for c in companies if c["source"] == "YFinance")
    print(f"Companies: {total}  (SEC={sec_cnt}  YF={yf_cnt})")

    # TRUNCATE
    from sqlalchemy import text as _text
    print("Deleting all rows from coreiq_model_forecasts …", end=" ", flush=True)
    eng = db_manager._read_engine or db_manager._engine
    with eng.connect() as conn:
        conn.execute(_text("DELETE FROM coreiq_model_forecasts"))
    print("done.")
    print()

    skipped, failed, success = [], [], []

    for idx, company in enumerate(companies, 1):
        ticker     = company["ticker"]
        source     = company["source"]
        exch       = company.get("exchange_acronym") or None
        cticker    = composite_ticker(ticker, exch)
        name       = company.get("name", ticker)

        prefix = f"[{idx:3d}/{total}] {cticker:12s} ({source:9s})"

        try:
            if source == "SEC":
                actual_rows = fetch_sec(ticker)
            else:
                actual_rows = fetch_yf(ticker, exch)

            if not actual_rows:
                print(f"{prefix}  SKIP — no revenue data")
                skipped.append((cticker, "no data"))
                continue

            model_df = build_model_df(actual_rows)

            if len(model_df) < MIN_YEARS:
                print(f"{prefix}  SKIP — only {len(model_df)} clean years (need {MIN_YEARS})")
                skipped.append((cticker, f"only {len(model_df)} years"))
                continue

            engine_obj  = RetailerForecaster.from_dataframe(model_df)
            backtest_df = engine_obj.backtest(holdout_years=2)
            forecast_df = engine_obj.forecast(periods=PERIODS)
            affected    = upsert(cticker, forecast_df, backtest_df, actual_rows)

            last_actual = actual_rows[-1]["period_date"]
            if hasattr(last_actual, "date"):
                last_actual = last_actual.date()
            fdate_1 = forecast_date_for(last_actual, 1).strftime("%Y-%m-%d") if last_actual else "?"

            best = None
            if backtest_df is not None and not backtest_df.empty and "mape" in backtest_df.columns:
                v = backtest_df.dropna(subset=["mape"])
                if not v.empty:
                    best = v.loc[v["mape"].idxmin(), "method"]

            ens_1y = None
            if "ensemble" in forecast_df.columns:
                ev = forecast_df["ensemble"].dropna()
                if not ev.empty:
                    ens_1y = ev.iloc[0] * 1000

            ens_s = f"  ens_FY+1={ens_1y:>10,.1f}M" if ens_1y else ""
            print(f"{prefix}  OK  yrs={len(model_df)}  rows={affected:3d}  best={str(best):12s}  next={fdate_1}{ens_s}")
            success.append(cticker)

        except Exception as exc:
            print(f"{prefix}  ERROR — {exc}")
            traceback.print_exc()
            failed.append((cticker, str(exc)[:80]))

    print()
    print("=" * 72)
    print(f"DONE.  Success={len(success)}  Skipped={len(skipped)}  Failed={len(failed)}")
    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for t, r in skipped:
            print(f"  {t}: {r}")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for t, r in failed:
            print(f"  {t}: {r}")

    cnt = db_manager.execute_query_readonly("SELECT COUNT(*) AS c FROM coreiq_model_forecasts", {})
    print(f"\nTotal rows in coreiq_model_forecasts: {cnt[0]['c']}")


if __name__ == "__main__":
    main()
