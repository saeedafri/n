"""Tier selection and degenerate-input handling for both forecasting engines.

These are the paths real data reaches rarely and therefore breaks loudly when it
does: a newly-listed company with one reported year, a series containing a
negative restatement, a company whose revenue never moves.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from utils.retailer_forecaster import RetailerForecaster, drop_partial_periods  # noqa: E402
from utils.retailer_quarterly_forecaster import RetailerQuarterlyForecaster  # noqa: E402


def _annual(values, start_year=2018):
    return pd.DataFrame({
        "year": list(range(start_year, start_year + len(values))),
        "sales": [float(v) for v in values],
    })


def _quarterly(n, start_year=2018, growth=1.02):
    rows, year, quarter = [], start_year, 1
    for i in range(n):
        rows.append({"year": year, "quarter": quarter, "sales": 100.0 * growth ** i})
        quarter = quarter + 1 if quarter < 4 else 1
        if quarter == 1:
            year += 1
    return pd.DataFrame(rows, columns=["year", "quarter", "sales"])


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n_years,expected_tier", [
    (0, "NO_DATA"), (1, "FLAT"), (2, "MINIMAL"), (4, "LIMITED"), (8, "FULL"),
])
def test_annual_tier_matches_history_length(n_years, expected_tier):
    engine = RetailerForecaster.from_dataframe(_annual([100 * 1.1 ** i for i in range(n_years)]))
    assert engine.tier == expected_tier


@pytest.mark.parametrize("n_quarters,expected_tier", [
    (0, "NO_DATA"), (2, "FLAT"), (6, "MINIMAL"), (12, "LIMITED"), (24, "FULL"),
])
def test_quarterly_tier_matches_history_length(n_quarters, expected_tier):
    engine = RetailerQuarterlyForecaster.from_dataframe(_quarterly(n_quarters))
    assert engine.tier == expected_tier


@pytest.mark.parametrize("n_years", [0, 1, 2, 3, 6, 12])
def test_annual_never_raises_and_always_yields_an_ensemble(n_years):
    engine = RetailerForecaster.from_dataframe(_annual([100 * 1.1 ** i for i in range(n_years)]))
    engine.backtest()
    forecast = engine.forecast(periods=5)
    if n_years == 0:
        assert forecast.empty
        return
    assert len(forecast) == 5
    assert "ensemble" in forecast.columns
    assert forecast["ensemble"].notna().all()
    # Scenario columns feed the dashboard and must survive every tier.
    for label in ("pessimistic", "baseline", "optimistic"):
        assert f"scenario_{label}" in forecast.columns


@pytest.mark.parametrize("n_quarters", [0, 1, 3, 5, 8, 20])
def test_quarterly_never_raises_and_always_yields_an_ensemble(n_quarters):
    engine = RetailerQuarterlyForecaster.from_dataframe(_quarterly(n_quarters))
    engine.backtest()
    forecast = engine.forecast(periods=20)
    if n_quarters == 0:
        assert forecast.empty
        return
    assert len(forecast) == 20
    assert "ensemble" in forecast.columns
    assert forecast["ensemble"].notna().all()


def test_single_year_carries_the_level_forward():
    engine = RetailerForecaster.from_dataframe(_annual([250.0]))
    engine.backtest()
    forecast = engine.forecast(periods=5)
    assert engine.tier == "FLAT"
    assert engine.best_method == "flat_carry"
    assert (forecast["ensemble"] == 250.0).all()
    assert engine.needs_review() is True


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------

def test_non_positive_revenue_is_dropped():
    engine = RetailerForecaster.from_dataframe(_annual([100, -50, 120, 130, 140]))
    assert (engine.clean_data["sales"] > 0).all()
    assert len(engine.clean_data) == 4


def test_all_zero_history_is_no_data():
    engine = RetailerForecaster.from_dataframe(_annual([0, 0, 0, 0, 0]))
    assert engine.tier == "NO_DATA"
    assert engine.forecast(periods=5).empty


def test_flat_history_is_plausible_and_unflagged():
    engine = RetailerForecaster.from_dataframe(_annual([100] * 8))
    engine.backtest()
    engine.forecast(periods=5)
    assert engine.tier == "FULL"
    assert engine.plausible is True
    assert engine.flag_reasons == []


def test_explosive_growth_is_flagged_implausible():
    engine = RetailerForecaster.from_dataframe(_annual([10 * 3 ** i for i in range(8)]))
    engine.backtest()
    engine.forecast(periods=5)
    assert engine.plausible is False
    assert engine.confidence == "flagged_implausible"
    assert any(r.startswith("implied_cagr") for r in engine.flag_reasons)


# ---------------------------------------------------------------------------
# Partial-period rule — the regression that truncated high-growth histories
# ---------------------------------------------------------------------------

def test_growth_ramp_keeps_its_early_years():
    """A company that grew 100x must keep its first years; they are real data,
    not stub periods. The old 10%-of-peak rule deleted them."""
    ramp = _annual([1, 10, 100, 400, 900, 1500])
    assert len(drop_partial_periods(ramp)) == 6


def test_interior_stub_period_is_dropped():
    stub = _annual([1000, 1100, 40, 1300, 1400])
    kept = drop_partial_periods(stub)
    assert list(kept["sales"]) == [1000, 1100, 1300, 1400]


def test_trailing_stub_period_is_dropped():
    stub = _annual([1000, 1100, 1200, 30])
    assert list(drop_partial_periods(stub)["sales"]) == [1000, 1100, 1200]
