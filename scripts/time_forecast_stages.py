"""Time every stage of a Run Analysis click, cold and warm.

End-to-end seconds hide where they go. This reports each stage separately so a
slow click can be attributed, and runs the whole thing twice so the second pass
shows what the caches actually save.

Run:
    .venv/bin/python scripts/time_forecast_stages.py --excel <file> [--freq Monthly]
        [--horizon 60] [--models SARIMAX,SARIMA,Prophet] [--no-cv]
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))
os.environ.setdefault("APP_ENV", "LOCAL")

import pandas as pd

from data import market_size_forecast_service as svc   # noqa: E402

KEY_FILE = Path("/Users/mohdsaeedafri/Downloads/Salesforecastingtool/"
                "Sales_forecast_tool –Updated/config.txt")


class Timings(dict):
    @contextmanager
    def stage(self, label: str):
        started = perf_counter()
        try:
            yield
        finally:
            self[label] = self.get(label, 0.0) + (perf_counter() - started) * 1000


def one_pass(excel: Path, freq: str | None, horizon: int | None, models: set,
             api_key: str, cross_validate: bool) -> tuple:
    timings = Timings()

    with timings.stage("read workbook"):
        handle = excel.open("rb")
        frame = svc.read_workbook(handle)

    with timings.stage("detect columns + frequency"):
        date_col = svc.guess_date_column(frame)
        value_col = svc.guess_value_column(frame, date_col)
        detected = svc.detect_frequency(pd.DatetimeIndex(
            svc.normalize_dates(frame[date_col]).dropna()))
    freq = freq or detected
    horizon = horizon or svc.FREQ_CONFIGS[freq]['pred_default']

    with timings.stage("load series"):
        loaded = svc.load_series(frame, date_col, value_col, freq)
    sales = loaded.frame[svc.TARGET_COL].dropna()

    with timings.stage("diagnostics"):
        diagnostics = svc.data_diagnostics(sales, freq, 'auto-detect')

    if "SARIMA" in models:
        with timings.stage("SARIMA (grid + fit + forecast)"):
            svc.run_sarima(sales, freq, horizon)

    if "Prophet" in models and svc.prophet_available():
        label = "Prophet (fit + forecast" + (" + CV)" if cross_validate else ", CV deferred)")
        with timings.stage(label):
            svc.run_prophet(sales, freq, horizon, diagnostics['seasonality_mode'],
                            0.05, 10, run_cross_validation=cross_validate)

    if "SARIMAX" in models and api_key:
        with timings.stage("FRED pull (32 series)"):
            monthly, _ = svc.fetch_fred_monthly(api_key)
        with timings.stage("align FRED + model frame"):
            aligned = svc.align_fred(monthly, loaded.frame.index,
                                     svc.FREQ_CONFIGS[freq]['resample'])
            model_frame = svc.build_model_frame(loaded.frame, freq, aligned)
        with timings.stage("difference to stationary"):
            stationary, _, _ = svc.difference_to_stationary(model_frame, freq)
        max_lag = min(svc.FREQ_CONFIGS[freq]['max_lag'], 12)
        with timings.stage("Granger scan"):
            granger = svc.granger_scan(stationary, max_lag)
        if len(granger):
            with timings.stage("build lagged frame"):
                lagged = svc.build_lagged_frame(model_frame, granger, sales.index[-1],
                                                horizon, freq)
            ranked = granger.sort_values('ftest_stat', ascending=False
                                         ).iloc[:10]['xlabel'].tolist()
            with timings.stage("combination search"):
                combos = svc.rank_exog_combinations(lagged, ranked, max_lag, 1, horizon, freq)
            if len(combos):
                best = combos.iloc[0]
                with timings.stage("SARIMAX (validate + forecast)"):
                    svc.run_sarimax(lagged, list(best['predvars']), best['order'],
                                    best['seasonal_order'], sales.index[-1],
                                    horizon, max_lag, freq)
    return timings, freq, horizon, len(sales)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--excel", required=True)
    parser.add_argument("--freq", default=None)
    parser.add_argument("--horizon", type=int, default=None)
    parser.add_argument("--models", default="SARIMAX,SARIMA,Prophet")
    parser.add_argument("--no-cv", action="store_true",
                        help="skip Prophet cross-validation, as the page now does")
    args = parser.parse_args()

    models = {name.strip() for name in args.models.split(",") if name.strip()}
    api_key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""
    excel = Path(args.excel)

    print("=" * 84)
    print(f"STAGE TIMINGS  {excel.name}  models={sorted(models)}  "
          f"cv={'off' if args.no_cv else 'on'}")
    print("=" * 84)

    results = []
    for label in ("cold", "warm"):
        timings, freq, horizon, rows = one_pass(excel, args.freq, args.horizon, models,
                                                api_key, not args.no_cv)
        results.append((label, timings))
        if label == "cold":
            print(f"freq={freq}  horizon={horizon}  rows={rows}")
            print()

    cold = dict(results[0][1])
    warm = dict(results[1][1])
    order = list(results[0][1])
    print(f"{'stage':<40} {'cold ms':>10} {'warm ms':>10}")
    print("-" * 84)
    for stage in order:
        print(f"{stage:<40} {cold.get(stage, 0):>10.0f} {warm.get(stage, 0):>10.0f}")
    print("-" * 84)
    print(f"{'TOTAL':<40} {sum(cold.values()):>10.0f} {sum(warm.values()):>10.0f}")
    print()
    print(f"cold {sum(cold.values()) / 1000:.2f}s   warm {sum(warm.values()) / 1000:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
