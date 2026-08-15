"""Parity against the data-science MDP Forecasting System reference output.

The data scientist shipped two reference reports produced by their own scripts.
These tests replay the same input spreadsheets through the app's engines and
require the tier, selected method, backtest MAPE and every forecast value to
match the reference to the cent.

One documented divergence: Coty Inc. reports revenue of -1098.0 for Q4 2020.
The reference script models that negative figure; the app drops it as invalid
(revenue cannot be negative), which shifts Coty's detected structural break by
one quarter and lowers its backtest error from 6.8% to 4.4%. Coty is therefore
excluded from the quarterly value comparison and asserted separately.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from utils.retailer_forecaster import RetailerForecaster  # noqa: E402
from utils.retailer_quarterly_forecaster import RetailerQuarterlyForecaster  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "mdp_forecasting")
TARGET_END_YEAR = 2031
REPORT_START_YEAR = 2026
TARGET_END_QIDX = TARGET_END_YEAR * 4 + 4
REPORT_START_QIDX = REPORT_START_YEAR * 4 + 1

NEGATIVE_REVENUE_COMPANY = "Coty Inc. (NYSE:COTY)"

pytestmark = pytest.mark.skipif(
    not os.path.isdir(FIXTURES), reason="MDP reference fixtures not present"
)


def _fixture(name):
    return os.path.join(FIXTURES, name)


# ---------------------------------------------------------------------------
# Loaders mirroring the reference scripts' input parsing
# ---------------------------------------------------------------------------

def _load_annual():
    df = pd.read_excel(_fixture("Screening_Results_Annual.xlsx"))
    df = df.drop_duplicates(subset=[df.columns[0]]).reset_index(drop=True)
    company_col = df.columns[0]
    year_cols = {}
    for column in df.columns[1:]:
        digits = "".join(ch for ch in str(column) if ch.isdigit())
        for i in range(len(digits) - 3):
            candidate = digits[i:i + 4]
            if 1990 <= int(candidate) <= 2100:
                year_cols[column] = int(candidate)
    rows = []
    for _, row in df.iterrows():
        for column, year in year_cols.items():
            value = _numeric(row[column])
            if value is not None:
                rows.append({"company": row[company_col], "year": year, "sales": value})
    return pd.DataFrame(rows), df[company_col].tolist()


def _parse_quarter(value):
    text = str(value).strip()
    match = re.search(r"[Qq](\d)\D*(\d{4})", text)
    if match:
        return int(match.group(2)), int(match.group(1))
    match = re.search(r"(\d{4})\D*[Qq](\d)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    try:
        parsed = pd.to_datetime(value, errors="raise")
        return parsed.year, (parsed.month - 1) // 3 + 1
    except Exception:
        return None


def _numeric(value):
    if pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if value in ("", "-", "NA", "N/A"):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_quarterly():
    path = _fixture("Screening_Results_Quarterly.xlsx")
    preview = pd.read_excel(path, header=None, nrows=10)
    header_row = 0
    for i in range(len(preview)):
        first = str(preview.iloc[i].iloc[0]).strip().lower()
        if first in ("company", "company name", "company/ticker") or (
            "compan" in first and len(first) < 20
        ):
            if any(_parse_quarter(v) for v in preview.iloc[i].iloc[1:]):
                header_row = i
                break
    df = pd.read_excel(path, header=header_row).dropna(how="all")
    df = df.drop_duplicates(subset=[df.columns[0]]).reset_index(drop=True)
    company_col = df.columns[0]

    quarter_cols = {}
    for column in df.columns[1:]:
        parsed = _parse_quarter(column)
        if parsed:
            quarter_cols[column] = parsed
    rows = []
    for _, row in df.iterrows():
        for column, (year, quarter) in quarter_cols.items():
            value = _numeric(row[column])
            if value is not None:
                rows.append({"company": row[company_col], "year": year,
                             "quarter": quarter, "sales": value})
    return pd.DataFrame(rows), df[company_col].tolist()


def _reference(name):
    return pd.read_excel(_fixture(name), sheet_name="Summary").set_index("Company")


def _quarter_label(qidx):
    quarter = qidx % 4
    if quarter == 0:
        return f"Q4 {qidx // 4 - 1}"
    return f"Q{quarter} {qidx // 4}"


# ---------------------------------------------------------------------------
# Annual
# ---------------------------------------------------------------------------

def test_annual_matches_reference_report():
    long_df, companies = _load_annual()
    reference = _reference("forecast_report_annual.xlsx")

    compared = 0
    for company in companies:
        history = long_df[long_df["company"] == company].sort_values("year")
        expected = reference.loc[company]
        if history.empty:
            assert expected["Tier"] == "NO_DATA", company
            continue

        engine = RetailerForecaster.from_dataframe(
            history[["year", "sales"]].reset_index(drop=True)
        )
        assert engine.tier == expected["Tier"], company

        fit_last_year = int(engine.data_excluding_outliers["year"].iloc[-1])
        periods = TARGET_END_YEAR - fit_last_year
        if periods <= 0:
            continue

        engine.backtest()
        forecast = engine.forecast(periods=periods)

        assert engine.best_method == expected["Best Method"], company
        if pd.notna(expected["Backtest MAPE %"]):
            assert engine.mape == pytest.approx(expected["Backtest MAPE %"], abs=0.06), company

        ensemble = dict(zip(forecast["year"].astype(int), forecast["ensemble"]))
        last_actual_year = int(engine.clean_data["year"].iloc[-1])
        for year in range(REPORT_START_YEAR, TARGET_END_YEAR + 1):
            if year <= last_actual_year:
                continue
            expected_value = expected[year] if year in expected.index else expected[str(year)]
            if pd.isna(expected_value):
                continue
            # One cent of slack: the reference rounds the ensemble at a
            # different point in the arithmetic, which can move the last digit.
            assert round(float(ensemble[year]), 2) == pytest.approx(
                float(expected_value), abs=0.02
            ), f"{company} {year}"
        compared += 1

    assert compared > 300, f"only compared {compared} companies"


# ---------------------------------------------------------------------------
# Quarterly
# ---------------------------------------------------------------------------

def test_quarterly_matches_reference_report():
    long_df, companies = _load_quarterly()
    reference = _reference("forecast_report_quarterly.xlsx")

    compared = 0
    for company in companies:
        history = long_df[long_df["company"] == company].sort_values(["year", "quarter"])
        expected = reference.loc[company]
        if history.empty:
            assert expected["Tier"] == "NO_DATA", company
            continue

        engine = RetailerQuarterlyForecaster.from_dataframe(
            history[["year", "quarter", "sales"]].reset_index(drop=True)
        )
        assert engine.tier == expected["Tier"], company
        if company == NEGATIVE_REVENUE_COMPANY:
            continue

        fit_last_qidx = int(engine.data_excl_outliers["qidx"].iloc[-1])
        periods = TARGET_END_QIDX - fit_last_qidx
        if periods <= 0:
            continue

        engine.backtest()
        forecast = engine.forecast(periods=periods)

        assert engine.best_method == expected["Best Method"], company
        if pd.notna(expected["Backtest MAPE %"]):
            assert engine.mape == pytest.approx(expected["Backtest MAPE %"], abs=0.06), company

        keys = forecast["fiscal_year"].astype(int) * 4 + forecast["fiscal_quarter"].astype(int)
        ensemble = dict(zip(keys, forecast["ensemble"]))
        last_actual_qidx = int(engine.clean_data["qidx"].iloc[-1])
        for qidx in range(REPORT_START_QIDX, TARGET_END_QIDX + 1):
            if qidx <= last_actual_qidx:
                continue
            label = _quarter_label(qidx)
            if label not in expected.index or pd.isna(expected[label]):
                continue
            assert round(float(ensemble[qidx]), 2) == pytest.approx(
                float(expected[label]), abs=0.02
            ), f"{company} {label}"
        compared += 1

    assert compared > 60, f"only compared {compared} companies"


def test_negative_revenue_period_is_dropped_not_modelled():
    """Coty reports -1098.0 for Q4 2020. Revenue cannot be negative, so the
    period is discarded rather than fed into growth rates and seasonal factors.
    """
    long_df, _ = _load_quarterly()
    history = long_df[long_df["company"] == NEGATIVE_REVENUE_COMPANY].sort_values(
        ["year", "quarter"]
    )
    assert (history["sales"] < 0).sum() == 1, "fixture no longer contains the negative period"

    engine = RetailerQuarterlyForecaster.from_dataframe(
        history[["year", "quarter", "sales"]].reset_index(drop=True)
    )
    assert (engine.clean_data["sales"] > 0).all()
    assert len(engine.clean_data) == len(history) - 1

    engine.backtest()
    # The reference script, which keeps the negative period, scores 6.8% here.
    assert engine.mape < 6.8


# ---------------------------------------------------------------------------
# Internals: the reference report also publishes the walk-forward detail and the
# seasonal indices. Matching Summary proves the outputs agree; matching these
# proves the engines got there the same way, rather than coincidentally landing
# on the same numbers.
# ---------------------------------------------------------------------------

def test_annual_backtest_detail_matches_reference():
    long_df, companies = _load_annual()
    detail = pd.read_excel(_fixture("forecast_report_annual.xlsx"),
                           sheet_name="Backtest Detail")
    expected_by_company = {c: g for c, g in detail.groupby("Company")}

    compared = 0
    for company in companies:
        history = long_df[long_df["company"] == company].sort_values("year")
        if history.empty or company not in expected_by_company:
            continue
        engine = RetailerForecaster.from_dataframe(
            history[["year", "sales"]].reset_index(drop=True))
        engine.backtest()
        if not engine.best_method or engine.best_method not in engine.backtest_detail:
            continue

        mine = engine.backtest_detail[engine.best_method]
        rows = expected_by_company[company]
        assert len(mine) == len(rows), f"{company}: {len(mine)} origins vs {len(rows)}"
        for got, (_, want) in zip(mine, rows.iterrows()):
            assert got["predicting_year"] == int(str(want["Predicting"]).split()[0]), company
            assert got["predicted"] == pytest.approx(float(want["Predicted"]), abs=0.02), company
            assert got["actual"] == pytest.approx(float(want["Actual"]), abs=0.02), company
            assert got["error_pct"] == pytest.approx(float(want["Error %"]), abs=0.11), company
        compared += 1

    assert compared > 250, f"only compared {compared} companies"


def test_quarterly_seasonal_indices_match_reference():
    long_df, companies = _load_quarterly()
    seasonal = pd.read_excel(_fixture("forecast_report_quarterly.xlsx"),
                             sheet_name="Seasonal Indices").set_index("Company")

    compared = 0
    for company in companies:
        history = long_df[long_df["company"] == company].sort_values(["year", "quarter"])
        if history.empty or company not in seasonal.index:
            continue
        if company == NEGATIVE_REVENUE_COMPANY:
            continue
        engine = RetailerQuarterlyForecaster.from_dataframe(
            history[["year", "quarter", "sales"]].reset_index(drop=True))
        engine.backtest()
        engine.forecast(periods=8)

        want = seasonal.loc[company]
        for i, quarter in enumerate(("Q1", "Q2", "Q3", "Q4")):
            assert round(float(engine.seasonal_indices[i]), 3) == pytest.approx(
                float(want[quarter]), abs=0.002
            ), f"{company} {quarter}"
        compared += 1

    assert compared > 60, f"only compared {compared} companies"
