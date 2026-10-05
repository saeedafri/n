"""Run their app and ours over a whole folder of datasets and diff every number.

The single-dataset comparison answers "is the port faithful on their fixture".
This answers "is it faithful on anything a sector analyst might upload" — across
magnitudes from a share percentage to a trillion, shapes from flat to collapsing,
series that cross zero, and every frequency.

For SARIMAX it also records WHICH macro drivers each dataset selected, so the
report can show the exog choice is genuinely data-driven rather than the same
list every time.

Both sides run in this process on the same libraries. Their app is executed
unmodified apart from the one documented `--compat` substitution that lets their
Granger scan run on statsmodels 0.15 at all.

Run:
    .venv/bin/python scripts/sweep_datasets_against_data_team.py \
        --datasets /tmp/sectors --out /tmp/sector_sweep.json [--with-sarimax]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "scripts"))
os.environ.setdefault("APP_ENV", "LOCAL")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from data import market_size_forecast_service as svc              # noqa: E402
from run_data_team_app_headless import capture                    # noqa: E402

KEY_FILE = Path("/Users/mohdsaeedafri/Downloads/Salesforecastingtool/"
                "Sales_forecast_tool –Updated/config.txt")


def worst(left, right) -> float:
    """Largest absolute difference, treating two NaNs in the same place as equal."""
    if left is None or right is None:
        return float("nan")
    a = np.asarray(left, dtype=float).ravel()
    b = np.asarray(right, dtype=float).ravel()
    if a.size != b.size:
        return float("inf")
    if a.size == 0:
        return 0.0
    both_nan = np.isnan(a) & np.isnan(b)
    diff = np.where(both_nan, 0.0, np.abs(a - b))
    return float(np.nanmax(diff)) if not np.all(np.isnan(diff)) else 0.0


def same_number(left, right) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    if isinstance(left, float) and isinstance(right, float):
        if np.isnan(left) and np.isnan(right):
            return True
    return abs(float(left) - float(right)) < 1e-9


def compare_one(path: Path, api_key: str, with_sarimax: bool) -> dict:
    record = {"dataset": path.stem, "checks": {}, "notes": []}

    frame = svc.read_workbook(path.open("rb"))
    date_col = svc.guess_date_column(frame)
    value_col = svc.guess_value_column(frame, date_col)
    if date_col is None or value_col is None:
        record["notes"].append("our loader refused the file")
        return record
    freq = svc.detect_frequency(pd.DatetimeIndex(
        svc.normalize_dates(frame[date_col]).dropna()))
    config = svc.FREQ_CONFIGS[freq]
    horizon = config["pred_default"]
    loaded = svc.load_series(frame, date_col, value_col, freq)
    sales = loaded.frame[svc.TARGET_COL].dropna()
    record.update({"freq": freq, "rows": int(len(sales)), "horizon": horizon,
                   "mean_level": float(sales.mean())})

    models = {"SARIMAX": with_sarimax, "SARIMA": True, "Prophet": True}
    theirs = capture(path, horizon, models, api_key if with_sarimax else "",
                     1, 10, freq, compat=True)
    record["their_crash"] = theirs.get("crash")

    diagnostics = svc.data_diagnostics(sales, freq, "auto-detect")
    record["seasonality_mode"] = diagnostics["seasonality_mode"]
    record["checks"]["seasonality_mode"] = (
        theirs.get("seasonality_mode") == diagnostics["seasonality_mode"])
    record["checks"]["seasonal_strength"] = same_number(
        theirs.get("seasonal_strength"), diagnostics["seasonal_strength"])
    record["checks"]["adf_p"] = same_number(theirs.get("adf_p"), diagnostics["adf_p"])

    # ── SARIMA ───────────────────────────────────────────────────────
    their_sarima = theirs.get("sarima")
    if their_sarima and their_sarima.get("mape") is not None:
        ours = svc.run_sarima(sales, freq, horizon)
        record["sarima_order"] = f"{ours['order']}x{ours['seasonal_order']}"
        record["their_sarima_order"] = (f"{their_sarima['order']}"
                                        f"x{their_sarima['seasonal_order']}")
        record["sarima_mape"] = round(float(ours["mape"]), 4)
        record["checks"]["sarima_order"] = (
            str(their_sarima["order"]) == str(ours["order"])
            and str(their_sarima["seasonal_order"]) == str(ours["seasonal_order"]))
        record["checks"]["sarima_aic"] = same_number(
            their_sarima["best_aic"], ours["best_aic"])
        record["checks"]["sarima_mape"] = same_number(their_sarima["mape"], ours["mape"])
        their_forecast = their_sarima.get("forecast")
        if their_forecast is not None and "mean" in their_forecast:
            gap = worst(their_forecast["mean"].values,
                        ours["forecast"]["mean"].values[:len(their_forecast)])
            record["sarima_forecast_gap"] = gap
            record["checks"]["sarima_forecast"] = gap < 1e-6
    else:
        record["notes"].append(f"their SARIMA unavailable ({theirs.get('crash')})")

    # ── Prophet ──────────────────────────────────────────────────────
    their_prophet = theirs.get("prophet")
    if their_prophet and their_prophet.get("mape") is not None:
        ours = svc.run_prophet(sales, freq, horizon, diagnostics["seasonality_mode"],
                               0.05, 10, run_cross_validation=True)
        record["prophet_mape"] = round(float(ours["mape"]), 4)
        record["checks"]["prophet_mape"] = same_number(their_prophet["mape"], ours["mape"])
        gap = worst(their_prophet["forecast"]["yhat"].values,
                    ours["forecast"]["mean"].values[:len(their_prophet["forecast"])])
        record["prophet_forecast_gap"] = gap
        record["checks"]["prophet_forecast"] = gap < 1e-6
        their_deltas = np.asarray(their_prophet["deltas"], dtype=float)
        our_deltas = np.asarray(
            ours["components_model"].params["delta"].mean(axis=0), dtype=float)
        record["checks"]["prophet_changepoints"] = worst(their_deltas, our_deltas) < 1e-9
    else:
        record["notes"].append("their Prophet unavailable")

    # ── SARIMAX, on their captured macro panel so only the code differs ──
    their_sarimax = theirs.get("sarimax")
    if with_sarimax and their_sarimax and theirs.get("fred_monthly"):
        their_frame = theirs["model_frame"]
        aligned = svc.align_fred(theirs["fred_monthly"], their_frame.index,
                                 config["resample"])
        model_frame = svc.build_model_frame(loaded.frame, freq, aligned,
                                            end_date=their_frame.index[-1])
        stationary, rounds, _ = svc.difference_to_stationary(model_frame, freq)
        max_lag = min(config["max_lag"], 12)
        granger = svc.granger_scan(stationary, max_lag)
        record["granger_hits"] = int(len(granger))
        record["their_granger_hits"] = int(len(their_sarimax["granger"]))
        record["checks"]["granger_labels"] = (
            set(their_sarimax["granger"]["xlabel"]) == set(granger["xlabel"]))
        if len(granger):
            ranked = list(granger.sort_values("ftest_stat", ascending=False)
                          .iloc[:10]["xlabel"])
            record["top_drivers"] = ranked[:3]
            lagged = svc.build_lagged_frame(model_frame, granger, sales.index[-1],
                                            horizon, freq)
            combos = svc.rank_exog_combinations(lagged, ranked, max_lag, 1, horizon, freq)
            record["combos_fitted"] = int(len(combos))
            their_combos = their_sarimax.get("combo_table")
            if their_combos is not None and len(their_combos) == len(combos):
                record["checks"]["sarimax_combo_rmse"] = (
                    worst(their_combos["rmse"], combos["rmse"]) < 1e-9)
            if len(combos):
                best = combos.iloc[0]
                record["chosen_driver"] = list(best["predvars"])[0]
                out = svc.run_sarimax(lagged, list(best["predvars"]), best["order"],
                                      best["seasonal_order"], sales.index[-1],
                                      horizon, max_lag, freq)
                record["sarimax_mape"] = round(float(out["mape"]), 4)
                record["checks"]["sarimax_mape"] = same_number(
                    their_sarimax["mape"], out["mape"])
                their_forecast = their_sarimax.get("forecast")
                if their_forecast is not None and "mean" in their_forecast:
                    gap = worst(their_forecast["mean"].values,
                                out["forecast"]["mean"].values[:len(their_forecast)])
                    record["sarimax_forecast_gap"] = gap
                    record["checks"]["sarimax_forecast"] = gap < 1e-6
        else:
            record["notes"].append("no Granger-significant drivers (both sides)")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--with-sarimax", action="store_true")
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    files = sorted(Path(args.datasets).glob("*.xlsx"))
    if args.only:
        wanted = tuple(p.strip() for p in args.only.split(",") if p.strip())
        files = [f for f in files if f.stem.startswith(wanted)]
    api_key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""

    results = []
    for path in files:
        print(f"--- {path.stem}", flush=True)
        try:
            record = compare_one(path, api_key, args.with_sarimax)
        except Exception as exc:
            record = {"dataset": path.stem, "checks": {},
                      "fatal": f"{type(exc).__name__}: {exc}",
                      "traceback": traceback.format_exc()[-1200:]}
        results.append(record)
        failed = [k for k, v in record.get("checks", {}).items() if not v]
        print(f"    {len(record.get('checks', {})) - len(failed)}"
              f"/{len(record.get('checks', {}))} identical"
              + (f"   MISMATCH: {failed}" if failed else "")
              + (f"   FATAL: {record['fatal']}" if record.get("fatal") else ""), flush=True)

    Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    total = sum(len(r.get("checks", {})) for r in results)
    bad = sum(1 for r in results for v in r.get("checks", {}).values() if not v)
    fatal = [r["dataset"] for r in results if r.get("fatal")]
    print(f"\n{len(results)} datasets · {total} comparisons · {bad} mismatches "
          f"· {len(fatal)} fatal")
    if fatal:
        print("fatal:", fatal)
    return 1 if (bad or fatal) else 0


if __name__ == "__main__":
    raise SystemExit(main())
