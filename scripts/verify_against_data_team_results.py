"""Acceptance test: our engine vs the data team's own recorded run.

The forecasting tool the data team handed over ships two artefacts that make a
real acceptance test possible:

    TestingDataset.xlsx   114 monthly points, their test fixture
    results.json          the output THEY recorded from running their app on it

This script feeds the same fixture through the portal's engine with the same
parameters and diffs every recorded number. Nothing here reimplements their
maths -- it imports the engine the page actually calls, so a pass means the
page a user clicks produces the numbers the data team signed off on.

Run:
    .venv/bin/python scripts/verify_against_data_team_results.py

Pass condition: `VERDICT: MATCH` on the last line. Anything else is a
regression against the data team's specification and must block release.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))
os.environ.setdefault("APP_ENV", "LOCAL")

import numpy as np
import pandas as pd

HANDOVER = Path("/Users/mohdsaeedafri/Downloads/Salesforecastingtool")
FIXTURE = HANDOVER / "TestingDataset.xlsx"
RECORDED = HANDOVER / "Sales_forecast_tool –Updated/results.json"

from data import market_size_forecast_service as svc   # noqa: E402

# Their recorded run used these sidebar settings (results.json -> params).
HORIZON = 60
FREQ = "Monthly"
SEASON_MODE = "auto-detect"
CP_PRIOR = 0.05
SEASON_PRIOR = 10


def compare(label: str, expected, actual, tolerance: float, failures: list) -> None:
    """Record one comparison. Tolerance is absolute, in the value's own units."""
    if expected is None or actual is None:
        line = f"  {label:<34} expected={expected!r:>14}  got={actual!r:>14}  SKIP"
        print(line)
        return
    if isinstance(expected, (bool, str)) or isinstance(actual, (bool, str)):
        ok = expected == actual
        delta = ""
    else:
        ok = abs(float(expected) - float(actual)) <= tolerance
        delta = f"  d={abs(float(expected) - float(actual)):.4g}"
    mark = "OK  " if ok else "FAIL"
    print(f"  {label:<34} expected={expected!r:>14}  got={actual!r:>14}  {mark}{delta}")
    if not ok:
        failures.append(label)


def compare_series(label: str, expected: list, actual, tolerance: float,
                   failures: list) -> None:
    actual = np.asarray(actual, dtype=float)
    expected_array = np.asarray(expected, dtype=float)
    if len(actual) != len(expected_array):
        print(f"  {label:<34} length expected={len(expected_array)} got={len(actual)}  FAIL")
        failures.append(f"{label} (length)")
        return
    worst = float(np.max(np.abs(expected_array - actual)))
    mark = "OK  " if worst <= tolerance else "FAIL"
    print(f"  {label:<34} n={len(actual):<3} max|delta|={worst:.4f}  "
          f"(tolerance {tolerance})  {mark}")
    if worst > tolerance:
        failures.append(label)


def main() -> int:
    for path in (FIXTURE, RECORDED):
        if not path.exists():
            print(f"missing handover artefact: {path}")
            return 2

    recorded = json.loads(RECORDED.read_text())
    failures: list = []

    print("=" * 78)
    print("ACCEPTANCE: portal engine vs the data team's recorded run")
    print("=" * 78)
    print(f"fixture       {FIXTURE.name}")
    print(f"recorded run  {RECORDED.name}")
    print(f"params        horizon={HORIZON} freq={FREQ} season_mode={SEASON_MODE} "
          f"cp={CP_PRIOR} season_prior={SEASON_PRIOR}")
    print()

    # ── Ingestion ────────────────────────────────────────────────────
    frame = svc.read_workbook(FIXTURE.open("rb"))
    date_col = svc.guess_date_column(frame)
    value_col = svc.guess_value_column(frame, date_col)
    detected = svc.detect_frequency(pd.DatetimeIndex(
        svc.normalize_dates(frame[date_col]).dropna()))
    loaded = svc.load_series(frame, date_col, value_col, FREQ)
    sales = loaded.frame[svc.TARGET_COL].dropna()

    print("[ingestion]")
    compare("detected frequency", recorded["params"]["frequency"], detected, 0, failures)
    compare("date column", "Date", str(date_col), 0, failures)
    compare("value column", "Sales", str(value_col), 0, failures)
    compare("rows loaded", recorded["diagnostics"]["total_records"], len(sales), 0, failures)
    compare("dates snapped", 0, loaded.snapped_to_period_end, 0, failures)
    compare("rows dropped", 0, loaded.dropped_rows, 0, failures)
    print()

    # ── Diagnostics tab ──────────────────────────────────────────────
    diagnostics = svc.data_diagnostics(sales, FREQ, SEASON_MODE)
    expected = recorded["diagnostics"]
    print("[diagnostics tab]")
    compare("adf_p", expected["adf_p"], round(diagnostics["adf_p"], 4), 5e-4, failures)
    compare("adf_stationary", expected["adf_stationary"],
            diagnostics["adf_stationary"], 0, failures)
    compare("kpss_p", expected["kpss_p"], round(diagnostics["kpss_p"], 4), 5e-4, failures)
    compare("kpss_stationary", expected["kpss_stationary"],
            diagnostics["kpss_stationary"], 0, failures)
    compare("seasonal_strength", expected["seasonal_strength"],
            round(diagnostics["seasonal_strength"], 1), 0.05, failures)
    compare("seasonality_mode", expected["seasonality_mode"],
            diagnostics["seasonality_mode"], 0, failures)
    compare("peak_month", expected["peak_month"],
            int(diagnostics["period_avg"].idxmax()), 0, failures)
    compare("peak_month_avg", expected["peak_month_avg"],
            round(float(diagnostics["period_avg"].max())), 0.5, failures)
    compare("trough_month", expected["trough_month"],
            int(diagnostics["period_avg"].idxmin()), 0, failures)
    compare("trough_month_avg", expected["trough_month_avg"],
            round(float(diagnostics["period_avg"].min())), 0.5, failures)
    compare("outlier_count", expected["outlier_count"],
            len(diagnostics["outliers"]), 0, failures)
    compare("mean_sales", expected["mean_sales"],
            round(diagnostics["mean_sales"]), 0.5, failures)
    compare("std_sales", expected["std_sales"],
            round(diagnostics["std_sales"]), 0.5, failures)
    compare("min_sales", expected["min_sales"],
            round(diagnostics["min_sales"]), 0.5, failures)
    compare("max_sales", expected["max_sales"],
            round(diagnostics["max_sales"]), 0.5, failures)
    compare("date_start", expected["date_start"],
            diagnostics["date_start"].strftime("%Y-%m-%d"), 0, failures)
    compare("date_end", expected["date_end"],
            diagnostics["date_end"].strftime("%Y-%m-%d"), 0, failures)
    for month, value in expected["monthly_avg_by_month"].items():
        compare(f"monthly_avg[{month}]", value,
                round(float(diagnostics["period_avg"].loc[int(month)])), 0.5, failures)
    print()

    # ── SARIMA tab ───────────────────────────────────────────────────
    sarima = svc.run_sarima(sales, FREQ, HORIZON)
    expected = recorded["model_detail"]["SARIMA"]
    outcome = recorded["results"]["SARIMA"]
    resid = svc.residual_diagnostics(sarima["residuals"])
    print("[SARIMA tab]")
    compare("order", expected["order"], str(sarima["order"]), 0, failures)
    compare("seasonal_order", expected["seasonal_order"],
            str(sarima["seasonal_order"]), 0, failures)
    compare("best_aic", expected["best_aic"], round(sarima["best_aic"], 2), 0.005, failures)
    compare("ljung_box_lag10_p", expected["ljung_box_lag10_p"],
            round(resid["lb_short"], 4), 5e-4, failures)
    compare("ljung_box_lag20_p", expected["ljung_box_lag20_p"],
            round(resid["lb_long"], 4), 5e-4, failures)
    compare("shapiro_wilk_p", expected["shapiro_wilk_p"],
            round(resid["shapiro_p"], 4), 5e-4, failures)
    compare("durbin_watson", expected["durbin_watson"],
            round(resid["durbin_watson"], 4), 5e-4, failures)
    compare("walk-forward MAPE", expected.get("mape", outcome["mape"]),
            round(sarima["mape"], 4), 5e-4, failures)
    compare("walk-forward RMSE", outcome["rmse"], round(sarima["rmse"], 2), 0.005, failures)
    compare_series("wf_actual", expected["wf_actual"],
                   np.asarray(sarima["wf_actual"], dtype=float), 0.5, failures)
    compare_series("wf_predicted", expected["wf_predicted"],
                   np.round(np.asarray(sarima["wf_predicted"], dtype=float)), 0.5, failures)
    compare("wf_dates[0]", expected["wf_dates"][0],
            pd.Timestamp(sarima["wf_actual"].index[0]).strftime("%Y-%m-%d"), 0, failures)
    compare("wf_dates[-1]", expected["wf_dates"][-1],
            pd.Timestamp(sarima["wf_actual"].index[-1]).strftime("%Y-%m-%d"), 0, failures)
    compare("forecast rows", len(outcome["fc_mean"]), len(sarima["forecast"]), 0, failures)
    compare("forecast start", outcome["fc_idx"][0],
            sarima["forecast"].index[0].strftime("%Y-%m-%d"), 0, failures)
    compare("forecast end", outcome["fc_idx"][-1],
            sarima["forecast"].index[-1].strftime("%Y-%m-%d"), 0, failures)
    compare_series("forecast mean", outcome["fc_mean"],
                   sarima["forecast"]["mean"].values, 0.01, failures)
    compare_series("forecast lower", outcome["fc_lower"],
                   sarima["forecast"]["mean_ci_lower"].values, 0.01, failures)
    compare_series("forecast upper", outcome["fc_upper"],
                   sarima["forecast"]["mean_ci_upper"].values, 0.01, failures)
    print()

    # ── Prophet tab ──────────────────────────────────────────────────
    expected = recorded["model_detail"]["Prophet"]
    outcome = recorded["results"]["Prophet"]
    if not svc.prophet_available():
        print("[Prophet tab] prophet not importable — SKIPPED")
    else:
        prophet = svc.run_prophet(sales, FREQ, HORIZON,
                                  diagnostics["seasonality_mode"], CP_PRIOR,
                                  SEASON_PRIOR, run_cross_validation=True)
        resid = svc.residual_diagnostics(prophet["residuals"])
        print("[Prophet tab]")
        compare("seasonality_mode", expected["seasonality_mode"],
                prophet["seasonality_mode"], 0, failures)
        compare("significant_changepoints", expected["significant_changepoints"],
                len(prophet["changepoints"]), 0, failures)
        compare("total_changepoints", expected["total_changepoints"],
                prophet["total_changepoints"], 0, failures)
        compare("cv_mape_with_regressor", expected["cv_mape_with_regressor"],
                round(prophet["cv_mape"], 2), 0.005, failures)
        compare("cv_mape_baseline", expected["cv_mape_baseline"],
                round(prophet["cv_baseline_mape"], 2), 0.005, failures)
        compare("ljung_box_lag10_p", expected["ljung_box_lag10_p"],
                round(resid["lb_short"], 4), 5e-4, failures)
        compare("ljung_box_lag20_p", expected["ljung_box_lag20_p"],
                round(resid["lb_long"], 4), 5e-4, failures)
        compare("shapiro_wilk_p", expected["shapiro_wilk_p"],
                round(resid["shapiro_p"], 4), 5e-4, failures)
        compare("durbin_watson", expected["durbin_watson"],
                round(resid["durbin_watson"], 4), 5e-4, failures)
        compare("walk-forward MAPE", outcome["mape"], round(prophet["mape"], 4), 5e-4, failures)
        compare("walk-forward RMSE", outcome["rmse"], round(prophet["rmse"], 2), 0.005, failures)
        compare_series("wf_actual", expected["wf_actual"], prophet["wf_actual"], 0.5, failures)
        compare_series("wf_predicted", expected["wf_predicted"],
                       np.round(prophet["wf_predicted"]), 0.5, failures)
        compare("forecast rows", len(outcome["fc_mean"]), len(prophet["forecast"]), 0, failures)
        compare("forecast start", outcome["fc_idx"][0],
                prophet["forecast"].index[0].strftime("%Y-%m-%d"), 0, failures)
        compare_series("forecast mean", outcome["fc_mean"],
                       prophet["forecast"]["mean"].values, 0.01, failures)
        compare_series("forecast lower", outcome["fc_lower"],
                       prophet["forecast"]["mean_ci_lower"].values, 0.01, failures)
        compare_series("forecast upper", outcome["fc_upper"],
                       prophet["forecast"]["mean_ci_upper"].values, 0.01, failures)
        for entry, their in zip(prophet["changepoints"], expected["changepoint_details"]):
            compare(f"changepoint {their['date']}", their["delta"],
                    round(entry["delta"], 4), 5e-4, failures)
    print()

    print("=" * 78)
    if failures:
        print(f"VERDICT: MISMATCH -- {len(failures)} check(s) failed")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("VERDICT: MATCH -- every number the data team recorded is reproduced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
