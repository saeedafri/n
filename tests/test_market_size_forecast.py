"""Market Size Forecasting engine — the behaviours the page depends on.

The forecasting maths is a port of the research team's standalone tool, so
these tests pin the parts that had to change to run in the portal, plus the
input handling the handover document calls out as deliberate behaviour:
year-only and quarter date labels, duplicate periods, auto column detection,
short series degrading instead of crashing, and the seasonal period actually
following the data's frequency.

No network and no database: SARIMAX's FRED pull is exercised only through its
failure path, which is what a wrong API key produces.
"""

import os
import sys
import threading

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

from data import market_size_forecast_service as engine  # noqa: E402


def monthly_series(periods=60, start="2019-01-31"):
    """A seasonal, trending monthly series — the shape the tool is built for."""
    index = pd.date_range(start, periods=periods, freq="ME")
    trend = np.arange(periods) * 800.0
    season = 20000.0 * np.sin(np.arange(periods) / 12.0 * 2 * np.pi)
    return pd.DataFrame({"Date": index, "Sales": 200000.0 + trend + season})


# ── frequency detection ────────────────────────────────────────────

@pytest.mark.parametrize("freq,expected", [
    ("W", "Weekly"), ("ME", "Monthly"), ("QE", "Quarterly"), ("YE", "Annual"),
])
def test_detect_frequency_covers_every_supported_cadence(freq, expected):
    index = pd.date_range("2019-01-31", periods=24, freq=freq)
    assert engine.detect_frequency(index) == expected


def test_detect_frequency_defaults_to_monthly_for_a_single_date():
    assert engine.detect_frequency(pd.DatetimeIndex(["2020-01-31"])) == "Monthly"


# ── date normalisation ─────────────────────────────────────────────

def test_bare_years_become_december_not_1970():
    """The original silently read the integer 2020 as nanoseconds."""
    parsed = engine.normalize_dates(pd.Series([2019, 2020, 2021]))
    assert list(parsed.dt.year) == [2019, 2020, 2021]
    assert list(parsed.dt.month) == [12, 12, 12]


@pytest.mark.parametrize("label,year,month", [
    ("Q1 2020", 2020, 3), ("2020Q2", 2020, 6),
    ("2020-Q3", 2020, 9), ("Q4-2020", 2020, 12),
])
def test_quarter_labels_map_to_quarter_end(label, year, month):
    parsed = engine.normalize_dates(pd.Series([label] * 4))
    assert parsed.iloc[0].year == year and parsed.iloc[0].month == month


def test_unparseable_dates_become_nat_rather_than_raising():
    parsed = engine.normalize_dates(pd.Series(["2020-01-31", "not a date", None]))
    assert parsed.notna().tolist() == [True, False, False]


# ── column detection ───────────────────────────────────────────────

def test_columns_are_detected_by_name_hint():
    frame = pd.DataFrame({"Period": pd.date_range("2020-01-31", periods=12, freq="ME"),
                          "Revenue": range(12), "Notes": ["x"] * 12})
    assert engine.guess_date_column(frame) == "Period"
    assert engine.guess_value_column(frame, "Period") == "Revenue"


def test_columns_are_detected_by_content_when_names_are_unhelpful():
    frame = pd.DataFrame({"a": pd.date_range("2020-01-31", periods=12, freq="ME"),
                          "b": np.arange(12, dtype=float)})
    assert engine.guess_date_column(frame) == "a"
    assert engine.guess_value_column(frame, "a") == "b"


def test_a_frame_with_no_usable_value_column_reports_nothing():
    frame = pd.DataFrame({"Date": pd.date_range("2020-01-31", periods=12, freq="ME"),
                          "Comment": ["n/a"] * 12})
    assert engine.guess_value_column(frame, "Date") is None


# ── loading ────────────────────────────────────────────────────────

def test_duplicate_periods_are_summed_and_reported():
    frame = pd.DataFrame({
        "Date": ["2020-01-31", "2020-01-31", "2020-02-29"],
        "Sales": [10.0, 15.0, 20.0]})
    loaded = engine.load_series(frame, "Date", "Sales", "Monthly")
    assert loaded.duplicates_merged == 1
    assert loaded.frame.loc["2020-01-31", engine.TARGET_COL] == 25.0
    assert loaded.frame.index.is_unique


def test_rows_with_unreadable_dates_are_dropped_and_counted():
    frame = pd.DataFrame({"Date": ["2020-01-31", "rubbish", "2020-02-29"],
                          "Sales": [1.0, 2.0, 3.0]})
    loaded = engine.load_series(frame, "Date", "Sales", "Monthly")
    assert loaded.dropped_rows == 1
    assert len(loaded.frame.dropna()) == 2


def test_loading_leaves_a_gap_free_index_at_the_chosen_frequency():
    frame = pd.DataFrame({"Date": ["2020-01-31", "2020-04-30"], "Sales": [1.0, 2.0]})
    loaded = engine.load_series(frame, "Date", "Sales", "Monthly")
    assert len(loaded.frame) == 4          # Jan..Apr, the gap filled with NaN
    assert loaded.frame[engine.TARGET_COL].isna().sum() == 2


# ── COVID dummy ────────────────────────────────────────────────────

@pytest.mark.parametrize("rule,freq,expected_flagged", [
    ("monthly", "ME", 4),      # Mar, Apr, May, Jun 2020
    ("quarterly", "QE", 3),    # Q1-Q3 2020 — the quarterly rule spans the recovery
    ("annual", "YE", 1),       # 2020
])
def test_covid_dummy_follows_the_data_cadence(rule, freq, expected_flagged):
    index = pd.date_range("2019-01-31", "2021-12-31", freq=freq)
    assert engine.get_covid_mask(index, rule).sum() == expected_flagged


# ── seasonal order ─────────────────────────────────────────────────

def test_seasonal_order_uses_the_datas_own_period():
    """The original stored a hardcoded 12 while fitting at the real period."""
    assert engine._seasonal_order((1, 1, 1), 4) == (1, 1, 1, 4)
    assert engine._seasonal_order((1, 1, 1), 52) == (1, 1, 1, 52)


def test_annual_data_falls_back_to_a_non_seasonal_order():
    """statsmodels rejects a seasonal period of 1, which Annual carries."""
    assert engine._seasonal_order((1, 1, 1), 1) == (0, 0, 0, 0)


# ── validation window ──────────────────────────────────────────────

def test_validation_window_never_swallows_the_training_set():
    # A 5-year monthly horizon must not hold out 60 of 114 points.
    assert engine.validation_window(60, "Monthly", 114) == 12
    assert engine.validation_window(2, "Monthly", 114) == 2
    assert engine.validation_window(24, "Monthly", 15) == 5


def test_validation_window_is_never_zero():
    assert engine.validation_window(6, "Monthly", 3) == 1


# ── diagnostics ────────────────────────────────────────────────────

def test_data_diagnostics_reports_stationarity_and_seasonality():
    loaded = engine.load_series(monthly_series(), "Date", "Sales", "Monthly")
    series = loaded.frame[engine.TARGET_COL].dropna()
    result = engine.data_diagnostics(series, "Monthly")
    assert result["total_records"] == 60
    assert 0.0 <= result["adf_p"] <= 1.0
    assert result["seasonality_mode"] in ("additive", "multiplicative")
    assert result["decomposition"] is not None


def test_seasonality_mode_can_be_forced():
    loaded = engine.load_series(monthly_series(), "Date", "Sales", "Monthly")
    series = loaded.frame[engine.TARGET_COL].dropna()
    assert engine.data_diagnostics(series, "Monthly", "additive")["seasonality_mode"] == "additive"


def test_residual_diagnostics_scale_their_lags_to_a_short_series():
    stats = engine.residual_diagnostics(np.random.default_rng(0).normal(size=9))
    assert stats["too_short"] is False
    assert max(stats["lags"]) < 9


def test_residual_diagnostics_refuse_a_series_too_short_to_test():
    stats = engine.residual_diagnostics([1.0, 2.0, 3.0])
    assert stats["too_short"] is True
    assert stats["failures"] == []


# ── models ─────────────────────────────────────────────────────────

def test_sarima_forecasts_the_requested_horizon_on_its_own_frequency():
    loaded = engine.load_series(monthly_series(), "Date", "Sales", "Monthly")
    series = loaded.frame[engine.TARGET_COL].dropna()
    result = engine.run_sarima(series, "Monthly", horizon=6)
    assert len(result["forecast"]) == 6
    assert result["forecast"].index[0] > series.index[-1]
    assert (result["forecast"].index.freqstr or "ME").startswith("M")
    assert result["mape"] >= 0
    assert result["seasonal_order"][-1] == 12


def test_sarima_on_quarterly_data_keeps_the_quarterly_season():
    index = pd.date_range("2015-03-31", periods=40, freq="QE")
    frame = pd.DataFrame({
        "Date": index,
        "Sales": 100000 + np.arange(40) * 900.0
                 + 8000.0 * np.sin(np.arange(40) / 4.0 * 2 * np.pi)})
    loaded = engine.load_series(frame, "Date", "Sales", "Quarterly")
    result = engine.run_sarima(loaded.frame[engine.TARGET_COL].dropna(),
                               "Quarterly", horizon=4)
    # The bug this pins: a 12-period season silently applied to quarterly data.
    assert result["seasonal_order"][-1] in (0, 4)
    assert len(result["forecast"]) == 4


def test_forecast_spread_is_measured_against_each_periods_own_level():
    def result(values):
        return {"forecast": pd.DataFrame({"mean": values})}
    # Two forecasts 10% apart at every period — a first-period-anchored
    # measure would report a much larger number as the level rises.
    flat = result(np.array([100.0, 200.0, 400.0]))
    high = result(np.array([110.526, 221.05, 442.1]))
    assert engine.forecast_spread({"a": flat, "b": high}) == pytest.approx(10.0, abs=0.1)


def test_forecast_spread_of_a_single_model_is_zero():
    assert engine.forecast_spread({"only": {"forecast": pd.DataFrame({"mean": [1.0]})}}) == 0.0


# ── FRED failure path ──────────────────────────────────────────────

def test_a_rejected_fred_key_is_reported_per_variable_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))   # never read a real panel

    class Rejected:
        status_code = 400

        @staticmethod
        def json():
            return {"error_code": 400,
                    "error_message": "Bad Request. The value for variable api_key is not registered."}

    # The pull reuses one Session, so that is what has to be stubbed.
    monkeypatch.setattr(engine.requests.Session, "get",
                        lambda self, *a, **k: Rejected())
    monthly, status = engine.fetch_fred_monthly("wrong-key")
    assert monthly == {}
    assert len(status) == len(engine.FRED_VARS)
    assert all("api_key" in message for message in status.values())


def test_one_broken_variable_does_not_sink_the_whole_pull(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))   # never read a real panel
    seen = {"n": 0}
    good = {"observations": [{"date": "2020-01-01", "value": "1.0"},
                             {"date": "2020-02-01", "value": "2.0"}]}
    lock = threading.Lock()

    class Response:
        status_code = 200

        @staticmethod
        def json():
            with lock:
                seen["n"] += 1
                first = seen["n"] == 1
            if first:
                raise ValueError("malformed payload")
            return good

    monkeypatch.setattr(engine.requests.Session, "get",
                        lambda self, *a, **k: Response())
    monthly, status = engine.fetch_fred_monthly("key")
    assert len(monthly) == len(engine.FRED_VARS) - 1
    assert sum(1 for message in status.values() if message != "ok") == 1


def test_monthly_fred_is_averaged_down_to_quarterly():
    monthly = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                        index=pd.date_range("2020-01-31", periods=6, freq="ME"))
    target = pd.date_range("2020-03-31", periods=2, freq="QE")
    aligned = engine.resample_fred_to_freq(monthly, target, "mean")
    assert aligned.iloc[0] == pytest.approx(2.0)   # Jan-Mar
    assert aligned.iloc[1] == pytest.approx(5.0)   # Apr-Jun


def test_monthly_fred_is_forward_filled_up_to_weekly():
    monthly = pd.Series([10.0, 20.0],
                        index=pd.to_datetime(["2020-01-31", "2020-02-29"]))
    target = pd.date_range("2020-02-02", periods=4, freq="W")
    aligned = engine.resample_fred_to_freq(monthly, target, "ffill")
    assert aligned.notna().all()
    assert aligned.iloc[0] == 10.0


# ── differencing ───────────────────────────────────────────────────

def test_differencing_stops_at_the_cap_on_series_adf_never_accepts():
    """Forward-filled monthly FRED on a weekly index is a step function.

    The original loop had only a length guard and differenced 76 times on
    weekly data, handing the Granger scan noise.
    """
    index = pd.date_range("2022-01-02", periods=180, freq="W")
    frame = pd.DataFrame({
        engine.TARGET_COL: 50000 + np.arange(180) * 22.0,
        "step": np.repeat(np.arange(45, dtype=float), 4),
    }, index=index)
    _, rounds, hit_cap = engine.difference_to_stationary(frame, "Weekly")
    assert rounds <= engine.MAX_DIFFERENCING_ROUNDS
    assert hit_cap is True


def test_differencing_reports_convergence_when_it_actually_converges():
    rng = np.random.default_rng(3)
    index = pd.date_range("2019-01-31", periods=90, freq="ME")
    frame = pd.DataFrame({engine.TARGET_COL: rng.normal(0, 1, 90),
                          "noise": rng.normal(0, 1, 90)}, index=index)
    _, rounds, hit_cap = engine.difference_to_stationary(frame, "Monthly")
    assert rounds == 0          # already stationary
    assert hit_cap is False


def test_a_numeric_column_is_not_mistaken_for_dates():
    """pandas reads 90000 as nanoseconds since the epoch and lands in 1970.

    With unhelpful column names that made the sales column outscore the real
    date column, and detection then failed outright.
    """
    assert engine.normalize_dates(pd.Series([90000.0, 91500.0, 93000.0])).isna().all()

    frame = pd.DataFrame({
        "when": pd.date_range("2020-01-31", periods=24, freq="ME").strftime("%Y-%m-%d"),
        "how_much": np.arange(24, dtype=float) * 1000 + 90000})
    assert engine.guess_date_column(frame) == "when"
    assert engine.guess_value_column(frame, "when") == "how_much"


# ── disk cache ─────────────────────────────────────────────────────
# The in-process cache dies on every restart and STG redeploys several times a
# day, so fitted models and the FRED panel are also persisted. A cache that
# served a stale or mismatched answer would be far worse than a slow page —
# these pin the invalidation rules.

def test_series_fingerprint_is_stable_and_sensitive():
    series = monthly_series(24).set_index("Date")["Sales"]
    assert engine.series_fingerprint(series) == engine.series_fingerprint(series.copy())

    edited = series.copy()
    edited.iloc[-1] *= 1.0001
    assert engine.series_fingerprint(series) != engine.series_fingerprint(edited)

    shifted = series.copy()
    shifted.index = shifted.index + pd.Timedelta(days=1)
    assert engine.series_fingerprint(series) != engine.series_fingerprint(shifted)


def test_disk_cache_round_trips_and_misses_on_a_new_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))
    assert engine.disk_cache_get("sarima", "abc") is None
    engine.disk_cache_put("sarima", "abc", {"mape": 1.25})
    assert engine.disk_cache_get("sarima", "abc") == {"mape": 1.25}
    assert engine.disk_cache_get("sarima", "different") is None
    assert engine.disk_cache_get("prophet", "abc") is None      # namespaced


def test_disk_cache_ignores_an_entry_past_its_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))
    engine.disk_cache_put("sarima", "old", {"mape": 2.0})
    assert engine.disk_cache_get("sarima", "old", ttl_hours=0) is None


def test_a_corrupt_cache_entry_is_ignored_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))
    (tmp_path / "sarima-broken.pkl").write_bytes(b"not a pickle")
    assert engine.disk_cache_get("sarima", "broken") is None


def test_an_unwritable_cache_directory_degrades_quietly(monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", "/proc/definitely/not/writable")
    assert engine.disk_cache_get("sarima", "x") is None
    engine.disk_cache_put("sarima", "x", {"mape": 1.0})          # must not raise


def test_cached_results_are_bit_identical_to_a_fresh_fit(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))
    loaded = engine.load_series(monthly_series(48), "Date", "Sales", "Monthly")
    series = loaded.frame[engine.TARGET_COL].dropna()

    fresh = engine.run_sarima(series, "Monthly", horizon=3)
    engine.disk_cache_put("sarima", "k", fresh)
    restored = engine.disk_cache_get("sarima", "k")

    assert restored["order"] == fresh["order"]
    assert restored["seasonal_order"] == fresh["seasonal_order"]
    assert restored["mape"] == fresh["mape"]
    assert restored["rmse"] == fresh["rmse"]
    assert restored["best_aic"] == fresh["best_aic"]
    for column in ("mean", "mean_ci_lower", "mean_ci_upper"):
        assert np.array_equal(restored["forecast"][column].values,
                              fresh["forecast"][column].values)


def test_the_fred_panel_is_not_served_across_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))
    panel = {"Unemployment Rate": pd.Series(
        [4.0, 4.1], index=pd.date_range("2020-01-31", periods=2, freq="ME"))}
    engine.write_fred_cache("key-one", panel, {"Unemployment Rate": "ok"})

    assert engine.read_fred_cache("key-one") is not None
    assert engine.read_fred_cache("key-two") is None      # different entitlements


def test_the_result_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "MAX_CACHED_RESULTS", 5)
    for index in range(12):
        engine.disk_cache_put("sarima", f"key{index:02d}", {"n": index})
    assert len(list(tmp_path.glob("sarima-*.pkl"))) <= 5


def test_eviction_never_removes_the_fred_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("MSF_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "MAX_CACHED_RESULTS", 2)
    engine.write_fred_cache("key", {"x": pd.Series([1.0])}, {"x": "ok"})
    for index in range(6):
        engine.disk_cache_put("sarima", f"key{index}", {"n": index})
    assert engine.read_fred_cache("key") is not None


# ── seasonal search gating ─────────────────────────────────────────
# A seasonal AR/MA term at lag 52 is estimated from one point per annual cycle,
# and each such fit costs ~90x one without them. A 288-week series took 50s
# locally for the grid alone — minutes on STG. These pin the gate, and pin that
# monthly and quarterly are never affected.

def test_monthly_and_quarterly_always_search_seasonal_terms():
    assert engine.seasonal_terms_supported(112, 12) is True      # the validated case
    assert engine.seasonal_terms_supported(40, 4) is True
    assert engine.seasonal_terms_supported(24, 12) is True       # short, still searched


def test_weekly_needs_eight_years_before_searching_seasonal_terms():
    assert engine.seasonal_terms_supported(236, 52) is False     # ~4.5 cycles
    assert engine.seasonal_terms_supported(8 * 52, 52) is True
    assert engine.seasonal_terms_supported(8 * 52 - 1, 52) is False


def test_annual_never_searches_seasonal_terms():
    assert engine.seasonal_terms_supported(50, 1) is False


def test_monthly_grid_is_unchanged_by_the_gate():
    """The order the research team validated must not move."""
    loaded = engine.load_series(monthly_series(114), "Date", "Sales", "Monthly")
    series = loaded.frame[engine.TARGET_COL].dropna()
    result = engine.run_sarima(series, "Monthly", horizon=2)
    assert result["seasonal_search"] is True
    assert result["seasonal_order"][-1] == 12
    assert result["seasonal_order"][:3] != (0, 0, 0)   # seasonal terms still searched


def test_short_weekly_drops_seasonal_terms_but_keeps_differencing():
    index = pd.date_range("2021-01-03", periods=200, freq="W")
    values = (3.6e9 + np.arange(200) * 2.0e6
              + 2.0e8 * np.sin(np.arange(200) / 52 * 2 * np.pi))
    frame = pd.DataFrame({"Date": index, "Sales": values})
    loaded = engine.load_series(frame, "Date", "Sales", "Weekly")
    result = engine.run_sarima(loaded.frame[engine.TARGET_COL].dropna(),
                               "Weekly", horizon=12)
    assert result["seasonal_search"] is False
    P, D, Q, season = result["seasonal_order"]
    assert (P, Q) == (0, 0)        # no seasonal AR/MA searched
    assert D == 1 and season == 52  # seasonal differencing still applied
    assert len(result["forecast"]) == 12
