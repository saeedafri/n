import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from utils.retailer_quarterly_forecaster import RetailerQuarterlyForecaster  # noqa: E402


def _seasonal_series(n_quarters=24, base=100.0, growth=0.03,
                     seasonal=(0.9, 1.0, 1.05, 1.25)):
    rows, val, year, q = [], base, 2018, 1
    for _ in range(n_quarters):
        rows.append({"year": year, "quarter": q, "sales": val * seasonal[q - 1]})
        val *= (1 + growth)
        q = q + 1 if q < 4 else 1
        if q == 1:
            year += 1
    return pd.DataFrame(rows)


def test_from_dataframe_builds_clean_quarters():
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    assert "qidx" in eng.clean_data.columns
    assert len(eng.clean_data) == 24
    assert eng.clean_data["quarter"].between(1, 4).all()


def test_forecast_returns_20_quarters_with_required_columns():
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    eng.backtest(holdout_quarters=4)
    fc = eng.forecast(periods=20)
    assert len(fc) == 20
    for col in ("fiscal_year", "fiscal_quarter", "label", "ensemble",
                "scenario_pessimistic", "scenario_baseline", "scenario_optimistic",
                "seasonal_naive"):
        assert col in fc.columns, f"missing column {col}"
    assert fc["fiscal_quarter"].between(1, 4).all()
    # Forecast continues after the last actual quarter
    assert fc["fiscal_year"].iloc[0] >= int(eng.clean_data["year"].iloc[-1])


def test_forecast_preserves_seasonality_direction():
    # Q4 (index 3) is the strongest quarter in the synthetic series;
    # the engine's seasonal indices should rank Q4 highest.
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    assert eng.seasonal_indices[3] == max(eng.seasonal_indices)


def test_backtest_picks_a_best_method():
    eng = RetailerQuarterlyForecaster.from_dataframe(_seasonal_series())
    bt = eng.backtest(holdout_quarters=4)
    assert not bt.empty
    assert eng.best_method in {"linear", "cagr", "exp_smoothing", "holt",
                               "ma_trend", "weighted_avg", "seasonal_naive"}
