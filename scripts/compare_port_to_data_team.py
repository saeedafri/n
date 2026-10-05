"""Three-way comparison: their app, our port, and their recorded run.

    THEIRS    the data team's app.py executed here, now, unmodified
              (scripts/run_data_team_app_headless.py writes the pickle)
    OURS      app/data/market_size_forecast_service.py -- what the portal runs
    RECORDED  results.json, the run they did on their own machine

THEIRS vs OURS answers the only question the data scientist is asking: does the
port compute what their code computes? Both sides run on the same libraries and
the same input, so any gap here is the port's fault.

OURS vs RECORDED is the same comparison across a library upgrade as well
(statsmodels 0.14 -> 0.15, prophet 1.1.5 -> 1.4.0), so a gap there is only
meaningful next to THEIRS vs RECORDED: if their own app no longer reproduces
their recorded numbers either, the upgrade moved them, not us.

Run:
    .venv/bin/python scripts/run_data_team_app_headless.py \
        --excel .../TestingDataset.xlsx --horizon 60 --out /tmp/theirs.pkl
    .venv/bin/python scripts/compare_port_to_data_team.py --theirs /tmp/theirs.pkl
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))
os.environ.setdefault("APP_ENV", "LOCAL")

import numpy as np
import pandas as pd

HANDOVER = Path("/Users/mohdsaeedafri/Downloads/Salesforecastingtool")
RECORDED = HANDOVER / "Sales_forecast_tool –Updated/results.json"

from data import market_size_forecast_service as svc   # noqa: E402


def gap(left, right) -> float:
    """Largest absolute difference between two scalars or two sequences."""
    if left is None or right is None:
        return float("nan")
    left_array = np.asarray(left, dtype=float).ravel()
    right_array = np.asarray(right, dtype=float).ravel()
    if left_array.size != right_array.size:
        return float("inf")
    return float(np.nanmax(np.abs(left_array - right_array))) if left_array.size else 0.0


def relative(absolute: float, scale) -> str:
    if not np.isfinite(absolute):
        return "n/a"
    magnitude = float(np.nanmean(np.abs(np.asarray(scale, dtype=float))))
    if magnitude == 0:
        return "n/a"
    return f"{absolute / magnitude * 100:.6f}%"


def row(label: str, theirs, ours, recorded, scale=None) -> dict:
    scale = scale if scale is not None else theirs
    return {
        "label": label,
        "theirs_vs_ours": gap(theirs, ours),
        "ours_vs_recorded": gap(ours, recorded),
        "theirs_vs_recorded": gap(theirs, recorded),
        "scale": scale,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theirs", required=True)
    parser.add_argument("--excel", default=str(HANDOVER / "TestingDataset.xlsx"))
    parser.add_argument("--horizon", type=int, default=60)
    parser.add_argument("--freq", default="Monthly")
    args = parser.parse_args()

    theirs = pickle.loads(Path(args.theirs).read_bytes())
    recorded = json.loads(RECORDED.read_text())

    frame = svc.read_workbook(Path(args.excel).open("rb"))
    date_col = svc.guess_date_column(frame)
    value_col = svc.guess_value_column(frame, date_col)
    loaded = svc.load_series(frame, date_col, value_col, args.freq)
    sales = loaded.frame[svc.TARGET_COL].dropna()

    print("=" * 96)
    print("THREE-WAY: their app (here, now) | our port | their recorded run")
    print("=" * 96)
    print(f"input    {Path(args.excel).name}   rows={len(sales)}  freq={args.freq}  "
          f"horizon={args.horizon}")
    print()

    # ── input series must be the same bytes on both sides ────────────
    their_values = np.asarray(theirs["sales_values"], dtype=float)
    print("[input series]")
    print(f"  rows           theirs={len(their_values)}  ours={len(sales)}  "
          f"recorded={recorded['diagnostics']['total_records']}")
    print(f"  max|delta|     {gap(their_values, sales.values):.6g}")
    print(f"  index equal    {list(theirs['sales_index']) == list(sales.index)}")
    print()

    diagnostics = svc.data_diagnostics(sales, args.freq, "auto-detect")
    print("[diagnostics]")
    for label, mine, their, rec in [
        ("adf_p", diagnostics["adf_p"], theirs["adf_p"], recorded["diagnostics"]["adf_p"]),
        ("kpss_p", diagnostics["kpss_p"], theirs["kpss_p"], recorded["diagnostics"]["kpss_p"]),
        ("seasonal_strength", diagnostics["seasonal_strength"],
         theirs["seasonal_strength"], recorded["diagnostics"]["seasonal_strength"]),
    ]:
        print(f"  {label:<18} theirs={their:.6f}  ours={mine:.6f}  recorded={rec}  "
              f"|theirs-ours|={abs(their - mine):.3g}")
    print(f"  {'seasonality_mode':<18} theirs={theirs['seasonality_mode']}  "
          f"ours={diagnostics['seasonality_mode']}  "
          f"recorded={recorded['diagnostics']['seasonality_mode']}")
    print()

    verdicts = []

    # ── SARIMA ───────────────────────────────────────────────────────
    if "sarima" in theirs:
        their_sarima = theirs["sarima"]
        ours_sarima = svc.run_sarima(sales, args.freq, args.horizon)
        rec_detail = recorded["model_detail"]["SARIMA"]
        rec_out = recorded["results"]["SARIMA"]

        print("[SARIMA]")
        print(f"  order            theirs={their_sarima['order']}  "
              f"ours={ours_sarima['order']}  recorded={rec_detail['order']}")
        print(f"  seasonal_order   theirs={their_sarima['seasonal_order']}  "
              f"ours={ours_sarima['seasonal_order']}  recorded={rec_detail['seasonal_order']}")
        print(f"  best_aic         theirs={their_sarima['best_aic']:.6f}  "
              f"ours={ours_sarima['best_aic']:.6f}  recorded={rec_detail['best_aic']}")
        print(f"  mape             theirs={their_sarima['mape']:.6f}  "
              f"ours={ours_sarima['mape']:.6f}  recorded={rec_out['mape']}")
        print(f"  rmse             theirs={their_sarima['rmse']:.4f}  "
              f"ours={ours_sarima['rmse']:.4f}  recorded={rec_out['rmse']}")

        their_fc = their_sarima["forecast"]["mean"].values
        our_fc = ours_sarima["forecast"]["mean"].values[:len(their_fc)]
        rec_fc = np.asarray(rec_out["fc_mean"], dtype=float)[:len(their_fc)]
        for label, left, right in [
            ("forecast  theirs-vs-ours", their_fc, our_fc),
            ("forecast  ours-vs-recorded", our_fc, rec_fc),
            ("forecast  theirs-vs-recorded", their_fc, rec_fc),
        ]:
            absolute = gap(left, right)
            print(f"  {label:<32} max|delta|={absolute:12.6f}   "
                  f"({relative(absolute, right)} of level)")
        wf_gap = gap(their_sarima["wf_predicted"], ours_sarima["wf_predicted"])
        print(f"  {'walk-forward theirs-vs-ours':<32} max|delta|={wf_gap:12.6f}")
        verdicts.append(("SARIMA order", str(their_sarima["order"]) == str(ours_sarima["order"])))
        verdicts.append(("SARIMA seasonal_order",
                         str(their_sarima["seasonal_order"]) == str(ours_sarima["seasonal_order"])))
        verdicts.append(("SARIMA AIC", abs(their_sarima["best_aic"] - ours_sarima["best_aic"]) < 1e-6))
        verdicts.append(("SARIMA forecast", gap(their_fc, our_fc) < 1e-6))
        verdicts.append(("SARIMA walk-forward", wf_gap < 1e-6))
        print()

    # ── Prophet ──────────────────────────────────────────────────────
    if "prophet" in theirs and svc.prophet_available():
        their_prophet = theirs["prophet"]
        ours_prophet = svc.run_prophet(sales, args.freq, args.horizon,
                                       diagnostics["seasonality_mode"], 0.05, 10,
                                       run_cross_validation=True)
        rec_detail = recorded["model_detail"]["Prophet"]
        rec_out = recorded["results"]["Prophet"]

        print("[Prophet]")
        print(f"  cv_mape          theirs={their_prophet['cv_mape']:.6f}  "
              f"ours={ours_prophet['cv_mape']:.6f}  "
              f"recorded={rec_detail['cv_mape_with_regressor']}")
        print(f"  cv_baseline      theirs={their_prophet['cv_baseline']:.6f}  "
              f"ours={ours_prophet['cv_baseline_mape']:.6f}  "
              f"recorded={rec_detail['cv_mape_baseline']}")
        print(f"  mape             theirs={their_prophet['mape']:.6f}  "
              f"ours={ours_prophet['mape']:.6f}  recorded={rec_out['mape']}")
        print(f"  rmse             theirs={their_prophet['rmse']:.4f}  "
              f"ours={ours_prophet['rmse']:.4f}  recorded={rec_out['rmse']}")

        their_fc = their_prophet["forecast"]["yhat"].values
        our_fc = ours_prophet["forecast"]["mean"].values[:len(their_fc)]
        rec_fc = np.asarray(rec_out["fc_mean"], dtype=float)[:len(their_fc)]
        for label, left, right in [
            ("forecast  theirs-vs-ours", their_fc, our_fc),
            ("forecast  ours-vs-recorded", our_fc, rec_fc),
            ("forecast  theirs-vs-recorded", their_fc, rec_fc),
        ]:
            absolute = gap(left, right)
            print(f"  {label:<32} max|delta|={absolute:12.6f}   "
                  f"({relative(absolute, right)} of level)")
        wf_gap = gap(their_prophet["wf_predicted"], ours_prophet["wf_predicted"])
        print(f"  {'walk-forward theirs-vs-ours':<32} max|delta|={wf_gap:12.6f}")

        their_deltas = np.asarray(their_prophet["deltas"], dtype=float)
        our_full = None   # recompute our per-changepoint deltas for comparison
        our_model = ours_prophet["components_model"]
        our_full = np.asarray(our_model.params["delta"].mean(axis=0), dtype=float)
        print(f"  {'changepoint deltas':<32} max|delta|={gap(their_deltas, our_full):12.6f}"
              f"   (n={len(our_full)})")
        print(f"  significant      theirs={int((np.abs(their_deltas) > 0.01).sum())}  "
              f"ours={len(ours_prophet['changepoints'])}  "
              f"recorded={rec_detail['significant_changepoints']}")
        verdicts.append(("Prophet cv_mape",
                         abs(their_prophet["cv_mape"] - ours_prophet["cv_mape"]) < 1e-9))
        verdicts.append(("Prophet forecast", gap(their_fc, our_fc) < 1e-6))
        verdicts.append(("Prophet walk-forward", wf_gap < 1e-6))
        verdicts.append(("Prophet changepoints", gap(their_deltas, our_full) < 1e-9))
        print()

    print("=" * 96)
    print("PORT FIDELITY (their app, run here, vs our engine, run here)")
    for label, ok in verdicts:
        print(f"  {label:<26} {'IDENTICAL' if ok else 'DIFFERS'}")
    everything = all(ok for _, ok in verdicts)
    print()
    print(f"VERDICT: {'PORT IS FAITHFUL' if everything else 'PORT DIFFERS FROM SOURCE'}")
    return 0 if everything else 1


if __name__ == "__main__":
    raise SystemExit(main())
