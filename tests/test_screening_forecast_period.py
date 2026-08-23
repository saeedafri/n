"""Screening's Forecasting criterion must read the store that matches its label.

The Forecasting statement offers FY / CQ / FQ in the same Period Type selector
as every other statement. Before this was wired up, all three routed to the
ANNUAL store, so a column headed "FQ3 2027" carried the full-year figure —
roughly 4x the quarter it claimed to show. These tests pin the routing with a
fake db_manager, so they run without a database.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import data.screening_service as screening_service  # noqa: E402

METRIC = {"label": "Revenue Forecast (Ensemble)", "model_key": "ensemble", "unit": "$mm"}

ANNUAL_VALUE = 22233.1
QUARTER_VALUES = {(2027, 1): 7372.2, (2027, 2): 4835.0,
                  (2027, 3): 4850.6, (2027, 4): 4668.0}


class FakeDB:
    """Answers from whichever table the SQL names, and records the queries."""

    def __init__(self):
        self.queries = []

    def execute_query_readonly(self, sql, params=None):
        flat = " ".join(sql.split())
        self.queries.append(flat)
        if "coreiq_model_forecasts_quarterly" in flat:
            if " AS y, " in flat:                      # range fetch
                return [{"ticker": "M", "y": y, "q": q, "val": v}
                        for (y, q), v in QUARTER_VALUES.items()]
            for (year, quarter), value in QUARTER_VALUES.items():
                # Match the whole clause: "= 2" is a substring of "= 2027".
                quarter_hit = (f"fiscal_quarter = {quarter}" in flat
                               or f"QUARTER(forecast_date) = {quarter}" in flat)
                year_hit = (f"fiscal_year = {year}" in flat
                            or f"YEAR(forecast_date) = {year}" in flat)
                if quarter_hit and year_hit:
                    return [{"ticker": "M", "val": value}]
            return []
        return [{"ticker": "M", "val": ANNUAL_VALUE}]

    @property
    def tables_hit(self):
        return {"quarterly" if "coreiq_model_forecasts_quarterly" in q else "annual"
                for q in self.queries}


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(screening_service, "db_manager", db)
    monkeypatch.setattr(screening_service.CompanyRepository, "get_companies_map",
                        staticmethod(lambda: {"M": {"source": "SEC"}}))
    # The bulk fetchers are @st.cache_data; call the undecorated functions so one
    # test's result can never be served to another.
    for name in ("_fetch_forecast_bulk", "_fetch_quarterly_forecast_bulk",
                 "_fetch_quarterly_forecast_range_bulk"):
        fn = getattr(screening_service, name)
        monkeypatch.setattr(screening_service, name, getattr(fn, "__wrapped__", fn))
    return db


def _run(criterion, fake_db):
    df = pd.DataFrame({"ticker": ["M"]})
    return screening_service.apply_forecast_criterion(criterion, df)


def test_fiscal_year_reads_the_annual_store(fake_db):
    out, stats = _run({"metric_info": METRIC, "year": "2027", "period_type": "FY",
                       "quarter": None, "display_col": "col"}, fake_db)
    assert out["col"].iloc[0] == pytest.approx(ANNUAL_VALUE)
    assert fake_db.tables_hit == {"annual"}


def test_fiscal_quarter_reads_the_quarterly_store(fake_db):
    """The defect: this used to return the annual figure under a quarter label."""
    out, stats = _run({"metric_info": METRIC, "year": "2027", "period_type": "FQ",
                       "quarter": "Q3", "display_col": "col"}, fake_db)
    assert out["col"].iloc[0] == pytest.approx(QUARTER_VALUES[(2027, 3)])
    assert out["col"].iloc[0] != pytest.approx(ANNUAL_VALUE)
    assert fake_db.tables_hit == {"quarterly"}


def test_each_quarter_returns_its_own_value(fake_db):
    for quarter, expected in ((1, 7372.2), (2, 4835.0), (4, 4668.0)):
        out, _ = _run({"metric_info": METRIC, "year": "2027", "period_type": "FQ",
                       "quarter": f"Q{quarter}", "display_col": "col"}, fake_db)
        assert out["col"].iloc[0] == pytest.approx(expected), quarter


def test_calendar_quarter_buckets_on_the_period_end_date(fake_db):
    _run({"metric_info": METRIC, "year": "2027", "period_type": "CQ",
          "quarter": "Q3", "display_col": "col"}, fake_db)
    sql = " ".join(fake_db.queries)
    assert "QUARTER(forecast_date)" in sql, "CQ must bucket by calendar quarter"
    assert "YEAR(forecast_date)" in sql
    assert "fiscal_quarter =" not in sql


def test_fiscal_quarter_matches_the_companys_own_fiscal_quarter(fake_db):
    _run({"metric_info": METRIC, "year": "2027", "period_type": "FQ",
          "quarter": "Q3", "display_col": "col"}, fake_db)
    sql = " ".join(fake_db.queries)
    assert "fiscal_quarter = 3" in sql
    assert "QUARTER(forecast_date)" not in sql


def test_quarter_range_produces_one_column_per_quarter(fake_db):
    criterion = {"metric_info": METRIC, "year": "Latest", "period_type": "FQ",
                 "quarter": None, "display_col": "col",
                 "quarter_range": {"from_q": 1, "from_y": 2027, "to_q": 4, "to_y": 2027}}
    out, stats = _run(criterion, fake_db)

    cols = stats["quarter_cols"]
    assert len(cols) == 4
    assert cols == criterion["quarter_cols"], "columns must be stamped for render + Excel"
    for quarter, col in enumerate(cols, start=1):
        assert f"FQ{quarter} 2027" in col
        assert out[col].iloc[0] == pytest.approx(QUARTER_VALUES[(2027, quarter)])


def test_latest_picks_the_nearest_matching_quarter(fake_db):
    _run({"metric_info": METRIC, "year": "Latest", "period_type": "FQ",
          "quarter": "Q3", "display_col": "col"}, fake_db)
    sql = " ".join(fake_db.queries)
    assert "MIN(periods_ahead)" in sql, "nearest quarter, not an arbitrary row"
