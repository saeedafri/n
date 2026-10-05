"""SARIMAX stage-by-stage: their app vs our port, on one shared FRED panel.

SARIMAX is the only model whose inputs are not purely the uploaded file: it
pulls 32 FRED series live. Two runs minutes apart can therefore differ because
FRED revised a number, which has nothing to do with the port. So this script
takes the macro panel THEIR run captured and feeds that same panel into our
pipeline. After that the only variable left is the code.

It then compares every stage in order -- model frame, differencing, Granger
scan, exog ranking, combination search, chosen order, walk-forward, forecast --
because a single end-number match can hide two compensating differences.

Prerequisite (writes the pickle, including their FRED panel):
    .venv/bin/python scripts/run_data_team_app_headless.py \
        --excel .../TestingDataset.xlsx --models SARIMAX --fred-key <key> \
        --compat --out /tmp/theirs_sx.pkl

Run:
    .venv/bin/python scripts/compare_sarimax_to_data_team.py --theirs /tmp/theirs_sx.pkl
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))
os.environ.setdefault("APP_ENV", "LOCAL")

import numpy as np
import pandas as pd

from data import market_size_forecast_service as svc   # noqa: E402

HANDOVER = Path("/Users/mohdsaeedafri/Downloads/Salesforecastingtool")


def worst(left, right) -> float:
    left_array = np.asarray(left, dtype=float).ravel()
    right_array = np.asarray(right, dtype=float).ravel()
    if left_array.size != right_array.size:
        return float("inf")
    return float(np.nanmax(np.abs(left_array - right_array))) if left_array.size else 0.0


def line(label: str, theirs, ours, delta=None) -> None:
    extra = "" if delta is None else f"   max|delta|={delta:.3e}"
    print(f"  {label:<26} theirs={theirs}   ours={ours}{extra}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theirs", required=True)
    parser.add_argument("--excel", default=str(HANDOVER / "TestingDataset.xlsx"))
    parser.add_argument("--freq", default="Monthly")
    parser.add_argument("--horizon", type=int, default=60)
    parser.add_argument("--max-lag", type=int, default=12)
    parser.add_argument("--max-exog", type=int, default=10)
    parser.add_argument("--max-combo", type=int, default=1)
    args = parser.parse_args()

    captured = pickle.loads(Path(args.theirs).read_bytes())
    theirs = captured["sarimax"]
    their_panel = captured["fred_monthly"]
    their_frame = captured["model_frame"]
    if not their_panel:
        print("their run captured no FRED panel — rerun the headless script")
        return 2

    frame = svc.read_workbook(Path(args.excel).open("rb"))
    date_col = svc.guess_date_column(frame)
    value_col = svc.guess_value_column(frame, date_col)
    loaded = svc.load_series(frame, date_col, value_col, args.freq)
    sales = loaded.frame[svc.TARGET_COL].dropna()
    last_actual = sales.index[-1]

    # Their panel, their index, our code from here on.
    # third argument is the resample METHOD, not the frequency name
    aligned = svc.align_fred(their_panel, their_frame.index,
                             svc.FREQ_CONFIGS[args.freq]['resample'])
    model_frame = svc.build_model_frame(loaded.frame, args.freq, aligned,
                                        end_date=their_frame.index[-1])

    checks = []
    print("=" * 94)
    print("SARIMAX STAGE-BY-STAGE: their app (compat-patched) vs our port")
    print("=" * 94)
    print(f"input {Path(args.excel).name}  freq={args.freq}  horizon={args.horizon}  "
          f"max_lag={args.max_lag}  max_exog={args.max_exog}  max_combo={args.max_combo}")
    print(f"FRED panel: their captured pull, {len(their_panel)} series, shared by both sides")
    print()

    print("[1. model frame]")
    shared = [c for c in their_frame.columns if c in model_frame.columns]
    line("columns", len(their_frame.columns), len(model_frame.columns))
    line("rows", len(their_frame), len(model_frame))
    frame_gap = worst(their_frame[shared].values, model_frame[shared].values)
    line("shared cells", f"{len(shared)} cols", f"{len(shared)} cols", frame_gap)
    checks.append(("model frame", frame_gap == 0.0 and
                   set(their_frame.columns) == set(model_frame.columns)))
    print()

    print("[2. differencing to stationarity]")
    stationary, rounds, capped = svc.difference_to_stationary(model_frame, args.freq)
    line("rounds", theirs["diffs"], f"{rounds} (hit cap: {capped})")
    checks.append(("differencing rounds", theirs["diffs"] == rounds))
    print()

    print("[3. Granger scan]")
    granger = svc.granger_scan(stationary, args.max_lag)
    their_granger = theirs["granger"]
    line("significant variables", len(their_granger), len(granger))
    theirs_sorted = their_granger.sort_values('ftest_stat', ascending=False).reset_index(drop=True)
    ours_sorted = granger.sort_values('ftest_stat', ascending=False).reset_index(drop=True)
    same_labels = set(their_granger['xlabel']) == set(granger['xlabel'])
    same_rank = list(theirs_sorted['xlabel']) == list(ours_sorted['xlabel'])
    print(f"  {'label set identical':<26} {same_labels}")
    print(f"  {'ranking identical':<26} {same_rank}")
    f_gap = p_gap = float("inf")
    if len(theirs_sorted) == len(ours_sorted):
        f_gap = worst(theirs_sorted['ftest_stat'], ours_sorted['ftest_stat'])
        p_gap = worst(theirs_sorted['ftest_pval'], ours_sorted['ftest_pval'])
        print(f"  {'F-statistic':<26} max|delta|={f_gap:.3e}")
        print(f"  {'p-value':<26} max|delta|={p_gap:.3e}")
        print(f"  {'winning lag identical':<26} "
              f"{list(theirs_sorted['lag']) == list(ours_sorted['lag'])}")
    checks.append(("granger labels", same_labels))
    checks.append(("granger ranking", same_rank))
    checks.append(("granger F-stats", f_gap == 0.0))
    print()

    print("[4. exog ranking + lagged frame]")
    ranked = granger.sort_values('ftest_stat', ascending=False).iloc[:args.max_exog]['xlabel'].tolist()
    line(f"top-{args.max_exog} list identical", list(theirs["exog_ftest"]) == ranked, "")
    lagged = svc.build_lagged_frame(model_frame, granger, last_actual, args.horizon, args.freq)
    line("lagged frame rows", "-", len(lagged))
    checks.append(("exog ranking", list(theirs["exog_ftest"]) == ranked))
    print()

    print("[5. combination search]")
    combos = svc.rank_exog_combinations(lagged, ranked, args.max_lag, args.max_combo,
                                        args.horizon, args.freq)
    their_combos = theirs["combo_table"]
    line("combinations scored", len(their_combos), len(combos))
    rmse_gap = mape_gap = float("inf")
    if len(their_combos) == len(combos):
        rmse_gap = worst(their_combos['rmse'], combos['rmse'])
        mape_gap = worst(their_combos['mape'], combos['mape'])
        print(f"  {'ranked RMSE column':<26} max|delta|={rmse_gap:.3e}")
        print(f"  {'ranked MAPE column':<26} max|delta|={mape_gap:.3e}")
        same_sequence = ([list(v) for v in their_combos['predvars']]
                         == [list(v) for v in combos['predvars']])
        print(f"  {'ranking identical':<26} {same_sequence}")
        checks.append(("combo ranking", same_sequence))
    checks.append(("combo RMSE", rmse_gap == 0.0))
    print()

    print("[6. chosen model, walk-forward, forecast]")
    best = combos.iloc[0]
    outcome = svc.run_sarimax(lagged, list(best['predvars']), best['order'],
                              best['seasonal_order'], last_actual, args.horizon,
                              args.max_lag, args.freq)
    line("predvars", list(their_combos.iloc[0]['predvars']), list(best['predvars']))
    line("order", f"{theirs['order']}x{theirs['seasonal_order']}",
         f"{outcome['order']}x{outcome['seasonal_order']}")
    print(f"  {'walk-forward MAPE':<26} theirs={theirs['mape']:.8f}   "
          f"ours={outcome['mape']:.8f}   d={abs(theirs['mape'] - outcome['mape']):.3e}")
    print(f"  {'walk-forward RMSE':<26} theirs={theirs['rmse']:.6f}   "
          f"ours={outcome['rmse']:.6f}   d={abs(theirs['rmse'] - outcome['rmse']):.3e}")
    checks.append(("chosen order", str(theirs['order']) == str(outcome['order'])
                   and str(theirs['seasonal_order']) == str(outcome['seasonal_order'])))
    checks.append(("walk-forward MAPE", abs(theirs['mape'] - outcome['mape']) < 1e-9))
    checks.append(("walk-forward RMSE", abs(theirs['rmse'] - outcome['rmse']) < 1e-9))

    their_forecast = theirs["forecast"]
    if their_forecast is not None and 'mean' in their_forecast:
        their_mean = their_forecast['mean'].values
        our_mean = outcome['forecast']['mean'].values[:len(their_mean)]
        forecast_gap = worst(their_mean, our_mean)
        print(f"  {'forecast rows':<26} theirs={len(their_mean)}   ours={len(outcome['forecast'])}")
        print(f"  {'forecast mean':<26} max|delta|={forecast_gap:.3e}  "
              f"({forecast_gap / float(np.mean(np.abs(their_mean))) * 100:.6f}% of level)")
        lower_gap = worst(their_forecast['mean_ci_lower'].values,
                          outcome['forecast']['mean_ci_lower'].values[:len(their_mean)])
        upper_gap = worst(their_forecast['mean_ci_upper'].values,
                          outcome['forecast']['mean_ci_upper'].values[:len(their_mean)])
        print(f"  {'forecast CI lower':<26} max|delta|={lower_gap:.3e}")
        print(f"  {'forecast CI upper':<26} max|delta|={upper_gap:.3e}")
        checks.append(("forecast mean", forecast_gap < 1e-6))
        checks.append(("forecast CI", lower_gap < 1e-6 and upper_gap < 1e-6))
    print()

    print("=" * 94)
    for label, ok in checks:
        print(f"  {label:<26} {'IDENTICAL' if ok else 'DIFFERS'}")
    everything = all(ok for _, ok in checks)
    print()
    print(f"VERDICT: SARIMAX {'PORT IS FAITHFUL' if everything else 'DIFFERS FROM SOURCE'}")
    return 0 if everything else 1


if __name__ == "__main__":
    raise SystemExit(main())
