"""
Reusable STG probe for quarterly-forecast development. Read-mostly; the only
writes are quarterly forecast upserts (real tickers) and a self-cleaning ZZTEST
roundtrip. Mirrors run_local.sh env wiring so app-context code talks to STG.

Usage:
    .venv/bin/python scripts/probe_quarterly_forecast.py <check> [arg]

Checks: create | schema | roundtrip | dashboard <TICKER> | sync <TICKER>
        | rows <TICKER...> | qdates | qtable | repo <TICKER>
"""
import os
import sys

from dotenv import dotenv_values

# ── Env wiring: route LOCAL config at the STG database (same as run_local.sh) ──
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = dotenv_values(os.path.join(_REPO, ".env"))
os.environ.setdefault("APP_ENV", "LOCAL")
os.environ.setdefault("DEBUG", "true")
os.environ["DB_HOST"] = _ENV.get("STG_DB_HOST", "")
os.environ["DB_PORT"] = _ENV.get("STG_DB_PORT", "3306")
os.environ["DB_NAME"] = _ENV.get("STG_DB_NAME", "")
os.environ["DB_USER"] = _ENV.get("STG_DB_USER", "")
os.environ["DB_PASSWORD"] = _ENV.get("STG_DB_PASSWORD", "")
os.environ["ENABLE_SSL"] = "true"
os.environ["SSL_CA"] = os.path.join(_REPO, "DigiCertGlobalRootG2.crt.pem")
sys.path.insert(0, os.path.join(_REPO, "app"))


def connect():
    """Direct pymysql connection to STG for raw schema/row inspection."""
    import pymysql
    return pymysql.connect(
        host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        db=os.environ["DB_NAME"], ssl={"ca": os.environ["SSL_CA"]},
        cursorclass=pymysql.cursors.DictCursor,
    )


# ── Checks ────────────────────────────────────────────────────────────────────

def create():
    """Run the real migration through the app's db_manager."""
    from data.quarterly_forecast_store import ensure_quarterly_forecast_table
    ensure_quarterly_forecast_table()
    print("ensure_quarterly_forecast_table() executed")


def schema():
    with connect() as c, c.cursor() as cur:
        cur.execute("SHOW CREATE TABLE coreiq_model_forecasts_quarterly")
        print(cur.fetchone()["Create Table"])


def _synthetic_frame(n_quarters=24, base=80.0, growth=0.025,
                     seasonal=(0.9, 1.0, 1.05, 1.25)):
    import pandas as pd
    rows, val, year, q = [], base, 2018, 1
    for _ in range(n_quarters):
        rows.append({"year": year, "quarter": q, "sales": val * seasonal[q - 1]})
        val *= (1 + growth)
        q = q + 1 if q < 4 else 1
        if q == 1:
            year += 1
    return pd.DataFrame(rows)


def roundtrip():
    from utils.retailer_quarterly_forecaster import RetailerQuarterlyForecaster
    from data.quarterly_forecast_store import (
        ensure_quarterly_forecast_table, upsert_forecasts, get_all_model_forecasts,
    )
    ensure_quarterly_forecast_table()
    eng = RetailerQuarterlyForecaster.from_dataframe(_synthetic_frame())
    bt = eng.backtest(holdout_quarters=4)
    fc = eng.forecast(periods=20)
    models = [c for c in fc.columns if c not in ("fiscal_year", "fiscal_quarter", "label")]
    bt_rows = [{"method_key": r["method"], "mape": r["mape"]} for _, r in bt.iterrows()]
    import datetime
    n = upsert_forecasts(
        ticker="ZZTEST", forecast_df=fc, model_keys=models,
        best_method_key=eng.best_method or "", backtest_rows=bt_rows,
        last_actual_date=datetime.date(2023, 12, 31),
        company_name="ZZ Test Co", exchange="TEST",
    )
    print(f"upsert affected={n}")
    stored = get_all_model_forecasts("ZZTEST", max_periods=20)
    for mk, rws in sorted(stored.items()):
        print(f"  {mk}: {len(rws)} rows")
    with connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM coreiq_model_forecasts_quarterly WHERE ticker='ZZTEST'")
        c.commit()
    print("cleaned ZZTEST")


def dashboard(ticker="AAPL"):
    from data.revenue_forecast_service import RevenueForecastService
    d = RevenueForecastService.get_quarterly_dashboard(ticker)
    fr = d.get("forecast_rows", [])
    print(f"ticker={ticker} forecast_rows={len(fr)} source={d.get('source')}")
    for r in fr[:8]:
        print(f"  {r.get('label')}: ensemble={r.get('ensemble')}")


def sync(ticker="AAPL"):
    from data.forecast_admin_service import sync_quarterly_forecast_for_ticker
    print(sync_quarterly_forecast_for_ticker(ticker, force=True))


def rows(*tickers):
    tickers = tickers or ("AAPL", "COST")
    placeholders = ",".join(["%s"] * len(tickers))
    with connect() as c, c.cursor() as cur:
        cur.execute(
            f"SELECT ticker, model_key, COUNT(*) n FROM coreiq_model_forecasts_quarterly "
            f"WHERE ticker IN ({placeholders}) GROUP BY ticker, model_key ORDER BY ticker, model_key",
            tickers)
        for r in cur.fetchall():
            print(r)


def qdates():
    from data.forecast_refresh_service import _quarterly_report_dates_bulk
    m = _quarterly_report_dates_bulk()
    for tk in ("AAPL", "COST", "WMT"):
        print(tk, m.get(tk))


def qtable():
    from data.forecast_refresh_service import get_refresh_table_data
    for r in get_refresh_table_data("quarterly")[:5]:
        print(r)


def repo(ticker="AAPL"):
    from data.repository import ModelForecastsRepository
    d = ModelForecastsRepository.get_quarterly_forecasts_data(ticker)
    print("periods:", [p.get("label") for p in d.get("periods", [])])
    for sec in d.get("sections", []):
        for row in sec:
            if row.get("model_key") == "ensemble":
                print("ensemble:", row.get("values"))


_CHECKS = {
    "create": create, "schema": schema, "roundtrip": roundtrip,
    "dashboard": dashboard, "sync": sync, "rows": rows,
    "qdates": qdates, "qtable": qtable, "repo": repo,
}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "schema"
    _CHECKS[name](*sys.argv[2:])
