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

from pathlib import Path

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


# ── downloads ──────────────────────────────────────────────────────────

def test_download_buttons_fetch_a_blob_instead_of_linking_to_media(monkeypatch):
    """The markup must not hand the browser a /media anchor to follow.

    An anchor is what a desktop download manager hooks, and what the
    end-of-run orphan sweep can delete mid-fetch.
    """
    import json
    import streamlit.components.v1 as components
    from utils import media_url

    captured = {}
    monkeypatch.setattr(media_url, "serve_bytes",
                        lambda data, mimetype, filename, page="", **kw: f"/media/abc.{filename.rsplit('.', 1)[-1]}")
    monkeypatch.setattr(media_url, "absolute_app_url", lambda path: "https://host" + path)
    monkeypatch.setattr(components, "html",
                        lambda markup, **kw: captured.setdefault("markup", markup))

    ok = media_url.render_download_buttons([
        ("Download CSV", b"a,b\n1,2\n", "text/csv", 'weird "name".csv'),
        ("Download Excel", b"PK\x03\x04", "application/vnd.ms-excel", "x.xlsx"),
    ], page="test")

    markup = captured["markup"]
    assert ok is True
    assert "createObjectURL" in markup and "saveBlob(FILES[i].url" in markup
    assert '<a href="/media' not in markup and "href=\"https://host/media" not in markup
    assert markup.count("onclick=\"dl(") == 2
    # The quoted filename survives as JSON, not as a broken JS string literal.
    assert json.dumps('weird "name".csv') in markup
    assert 'weird "name".csv</button>' not in markup  # label is HTML-escaped


def test_download_buttons_report_failure_when_nothing_can_be_served(monkeypatch):
    from utils import media_url
    monkeypatch.setattr(media_url, "serve_bytes", lambda *a, **kw: None)
    monkeypatch.setattr(media_url, "report_oversized_embed", lambda *a, **kw: None)
    assert media_url.render_download_buttons(
        [("CSV", b"x", "text/csv", "x.csv")], page="test") is False


# ── ingestion: real-world date labels ───────────────────────────────────
# Every case below came from a file the data team actually tried to upload,
# or from the format table in the spec. Before this, five of the six monthly
# formats parsed and were then silently zeroed by the period-end reindex.

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _two_column_frame(dates):
    return pd.DataFrame({"Date": dates,
                         "Value": np.arange(len(dates), dtype=float) + 100.0})


@pytest.mark.parametrize("label,dates", [
    ("iso-month-end",   [f"2020-{m:02d}-{d}" for m, d in
                         [(1,31),(2,29),(3,31),(4,30),(5,31),(6,30),
                          (7,31),(8,31),(9,30),(10,31),(11,30),(12,31)]]),
    ("iso-month-start", [f"2020-{m:02d}-01" for m in range(1, 13)]),
    ("year-month-dash", [f"2020-{m:02d}" for m in range(1, 13)]),
    ("month-name-year", [f"{m} 2020" for m in MONTH_NAMES]),
    ("month-name-dash", [f"{m}-2020" for m in MONTH_NAMES]),
    ("numeric-slash",   [f"{m:02d}/2020" for m in range(1, 13)]),
])
def test_every_monthly_label_survives_cleaning(label, dates):
    """Parsing is not enough: the value must still be there after load_series."""
    loaded = engine.load_series(_two_column_frame(dates), "Date", "Value", "Monthly")
    usable = loaded.frame[engine.TARGET_COL].dropna()
    assert len(usable) == 12, f"{label}: {len(usable)}/12 survived cleaning"


@pytest.mark.parametrize("label,dates", [
    ("straight-apostrophe", ["Dec '22", "Jan '23", "Feb '23"]),
    ("curly-no-space",      ["Dec’22", "Jan’23", "Feb’23"]),
    ("curly-with-space",    ["Dec ’22", "Jan ’23", "Feb ’23"]),
    ("apostrophe-trailing", ["Dec' 22", "Jan' 23", "Feb' 23"]),
    ("hyphen-two-digit",    ["Dec-22", "Jan-23", "Feb-23"]),
    ("bare-two-digit",      ["Dec 22", "Jan 23", "Feb 23"]),
])
def test_short_year_month_labels_parse(label, dates):
    got = engine.normalize_dates(pd.Series(dates))
    assert got.notna().all(), f"{label}: {got.isna().sum()} of {len(dates)} failed"
    assert [d.year for d in got] == [2022, 2023, 2023]
    assert [d.month for d in got] == [12, 1, 2]


def test_statistical_month_notation_parses():
    got = engine.normalize_dates(pd.Series(["2020M01", "2020M02", "2020M12"]))
    assert got.notna().all()
    assert [d.month for d in got] == [1, 2, 12]


def test_a_numeric_column_is_still_refused_as_dates():
    """The guard that stops a sales column winning date detection must survive."""
    got = engine.normalize_dates(pd.Series([90000.0, 91000.0, 92000.0]))
    assert got.isna().all()


def test_period_end_files_are_untouched_by_the_snap():
    """A file that already worked must produce byte-identical dates."""
    dates = ["2020-01-31", "2020-02-29", "2020-03-31"]
    loaded = engine.load_series(_two_column_frame(dates), "Date", "Value", "Monthly")
    assert [str(d.date()) for d in loaded.frame.index] == dates


def test_unreadable_cells_are_reported_not_just_counted():
    frame = _two_column_frame(["2020-01-31", "not a date", "2020-03-31", "???"])
    loaded = engine.load_series(frame, "Date", "Value", "Monthly")
    assert loaded.dropped_rows == 2
    assert "not a date" in loaded.unreadable_dates
    assert "???" in loaded.unreadable_dates


# ── ingestion: values an analyst formatted for reading ──────────────────

@pytest.mark.parametrize("label,raw,expected", [
    ("thousands",  ["1,234", "5,678"],      [1234.0, 5678.0]),
    ("currency",   ["$1,234", "$5,678"],    [1234.0, 5678.0]),
    ("percent",    ["12.5%", "-0.4%"],      [12.5, -0.4]),
    ("accounting", ["(87)", "1,200"],       [-87.0, 1200.0]),
    ("spaced",     ["1 234", "5 678"],      [1234.0, 5678.0]),
    ("nbsp",       ["1 234", "5 678"], [1234.0, 5678.0]),
])
def test_formatted_numbers_are_recovered(label, raw, expected):
    got = engine.to_numeric_values(pd.Series(raw))
    assert list(got) == expected, label


def test_plain_numbers_are_left_alone():
    got = engine.to_numeric_values(pd.Series([1234.0, 5678.0, 9012.0]))
    assert list(got) == [1234.0, 5678.0, 9012.0]


def test_a_gap_does_not_crash_sarima():
    """A file with a missing period lost its index freq and SARIMAX raised
    'No supported index is available'. The data team's file has four."""
    idx = pd.date_range("2016-01-31", periods=60, freq="ME")
    values = 1000 + np.arange(60) * 5.0
    series = pd.Series(values, index=idx).drop(idx[13]).drop(idx[25])
    assert series.index.freq is None
    restored = engine.ensure_regular_index(series, "Monthly")
    assert restored.index.freq is not None
    assert restored.isna().sum() == 2
    result = engine.run_sarima(series, "Monthly", 3)
    assert result["forecast"]["mean"].notna().all()


def test_a_gapless_series_is_unchanged_by_the_index_repair():
    idx = pd.date_range("2016-01-31", periods=24, freq="ME")
    series = pd.Series(np.arange(24, dtype=float), index=idx)
    restored = engine.ensure_regular_index(series, "Monthly")
    assert len(restored) == 24
    assert restored.notna().all()
    assert list(restored.values) == list(series.values)


# ── ingestion: the day-vs-year ambiguity in `Mon NN` ────────────────────
# `Jan 01, Feb 02, Mar 03` is days of the month. Read as two-digit years it
# becomes 2001-01, 2002-02, 2003-03 -- steps of thirteen months, which no real
# timeline has. Found by driving the page, not by the earlier tests.

MONTHS_3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def test_bare_month_day_labels_are_not_read_as_years():
    labels = [f"{MONTHS_3[i]} {i + 1:02d}" for i in range(12)]
    got = engine.normalize_dates(pd.Series(labels))
    years = {d.year for d in got.dropna()}
    assert not (years and min(years) > 1990 and len(years) == 12), (
        f"day-of-month labels were read as 12 separate years: {sorted(years)}")


def test_a_repeating_day_cycle_is_not_read_as_years():
    labels = [f"{MONTHS_3[i % 12]} {(i % 28) + 1:02d}" for i in range(30)]
    got = engine.normalize_dates(pd.Series(labels))
    years = {d.year for d in got.dropna()}
    assert len(years) <= 2, f"repeating day cycle produced {len(years)} years"


@pytest.mark.parametrize("labels", [
    ["Dec '22", "Jan '23", "Feb '23"],
    ["Dec’22", "Jan’23", "Feb’23"],
    ["Dec-22", "Jan-23", "Feb-23"],
    ["Dec 22", "Jan 23", "Feb 23"],
])
def test_real_short_year_series_still_read_as_years(labels):
    got = engine.normalize_dates(pd.Series(labels)).dropna()
    assert [d.year for d in got] == [2022, 2023, 2023]


def test_an_annual_series_on_one_month_reads_as_years():
    got = engine.normalize_dates(pd.Series(["Dec 20", "Dec 21", "Dec 22", "Dec 23"])).dropna()
    assert [d.year for d in got] == [2020, 2021, 2022, 2023]


# ── value columns an analyst formatted for reading ──────────────────

@pytest.mark.parametrize("values, note", [
    (["1,234", "2,345", "3,456"], "thousands separators"),
    (["$1,234", "$2,345", "$3,456"], "currency symbols"),
    (["(87)", "1,234", "2,345"], "accounting negatives"),
    (["1 234", "2 345", "3 456"], "space separators"),
])
def test_a_formatted_value_column_is_still_detected(values, note):
    """Detection scored with plain to_numeric refused files the parser could read."""
    frame = pd.DataFrame({
        "Date": ["2020-01-31", "2020-02-29", "2020-03-31"],
        "Sales": values,
    })
    assert engine.guess_value_column(frame, "Date") == "Sales", note


def test_a_text_column_is_not_mistaken_for_values():
    frame = pd.DataFrame({
        "Date": ["2020-01-31", "2020-02-29", "2020-03-31"],
        "Region": ["National", "National", "National"],
    })
    assert engine.guess_value_column(frame, "Date") is None


# ── gapped series reach every model, not just SARIMA ────────────────

def _gapped_frame(periods=96, missing=(13, 25, 37)):
    """A monthly model frame with blank target periods and complete exog."""
    index = pd.date_range("2017-01-31", periods=periods, freq="ME")
    rng = np.random.default_rng(11)
    sales = pd.Series(np.linspace(100.0, 260.0, periods)
                      * (1 + 0.09 * np.sin(np.arange(periods) / 12 * 2 * np.pi)),
                      index=index)
    sales.iloc[list(missing)] = np.nan
    driver = pd.Series(np.linspace(40.0, 90.0, periods)
                       + rng.normal(0, 1.5, periods), index=index)
    return pd.DataFrame({engine.TARGET_COL: sales, "driver (2m ago)": driver})


def test_blank_periods_do_not_disable_the_combination_search():
    frame = _gapped_frame()
    ranked = engine.rank_exog_combinations(frame, ["driver (2m ago)"], 3, 1, 6, "Monthly")
    assert len(ranked) == 1, "every combination failed to fit on a gapped series"
    assert np.isfinite(ranked.iloc[0]["rmse"])


def test_blank_periods_do_not_disable_sarimax():
    frame = _gapped_frame()
    last_actual = frame[engine.TARGET_COL].last_valid_index()
    outcome = engine.run_sarimax(frame, ["driver (2m ago)"], (1, 1, 1), (1, 1, 1, 12),
                                 last_actual, 6, 3, "Monthly")
    assert len(outcome["forecast"]) == 6
    assert np.isfinite(outcome["mape"])
    # the walk-forward table must still be labelled with real dates
    assert outcome["walkforward"].index.dtype.kind == "M"
    assert outcome["wf_predicted"].index.dtype.kind == "M"


def test_a_gapless_frame_keeps_its_dated_index_through_sarimax():
    """The repair must be inert when there is nothing to repair."""
    frame = _gapped_frame(missing=())
    before = frame[["driver (2m ago)"]].copy()
    engine.run_sarimax(frame, ["driver (2m ago)"], (1, 1, 1), (0, 1, 0, 12),
                       frame.index[-1], 4, 3, "Monthly")
    pd.testing.assert_frame_equal(before, frame[["driver (2m ago)"]])
    assert engine.positional_if_irregular(frame) is frame


# ── a series containing zeros makes MAPE undefined, not the page broken ──

def test_mape_is_undefined_when_the_series_contains_a_zero():
    """Their formula divides by each actual; a true zero gives infinity."""
    actual = np.array([100.0, 0.0, 120.0])
    predicted = np.array([101.0, 2.0, 119.0])
    mape, rmse = engine._accuracy(actual, predicted)
    assert not np.isfinite(mape), "a zero actual must still produce a non-finite MAPE"
    assert np.isfinite(rmse), "RMSE stays usable and is what the page ranks on"


def test_undefined_mape_reads_as_not_available():
    from pages import market_size_forecasting as page
    assert page._percent(float("inf")) == "n/a"
    assert page._percent(float("nan")) == "n/a"
    assert page._percent(None) == "n/a"
    assert page._percent(1.3817) == "1.38%"
    assert page._percent(98.6, 1) == "98.6%"


def test_the_best_model_falls_back_to_rmse_when_every_mape_is_undefined():
    from pages import market_size_forecasting as page
    results = {
        "SARIMAX": {"mape": float("inf"), "rmse": 9100.0},
        "SARIMA": {"mape": float("inf"), "rmse": 4200.0},
        "Prophet": {"mape": float("inf"), "rmse": 6300.0},
    }
    assert not page._mape_is_usable(results)
    assert min(results, key=lambda m: page._ranking_metric(results[m])) == "SARIMA"


def test_a_usable_mape_still_decides_the_best_model():
    from pages import market_size_forecasting as page
    results = {
        "SARIMA": {"mape": 2.4, "rmse": 100.0},      # worse MAPE, better RMSE
        "Prophet": {"mape": 1.1, "rmse": 900.0},
    }
    assert page._mape_is_usable(results)
    assert min(results, key=lambda m: page._ranking_metric(results[m])) == "Prophet"


# ── a minority of unreadable labels must not discard the readable ones ──

def test_a_few_unreadable_labels_do_not_void_the_whole_column():
    """China NBS files label Jan+Feb as one row; 35 of 39 read fine.

    The share threshold used to be a cliff at 0.9: at 0.897 the month-year
    reader was skipped and the fallback turned every row into NaT, so the page
    reported "no readable date column" about a column it could almost all read.
    """
    labels = []
    for year in (23, 24, 25):
        labels.append(f"Jan/Feb '{year}")              # unreadable, 1 in 11
        for month in MONTHS_3[2:]:
            labels.append(f"{month} '{year}")
    got = engine.normalize_dates(pd.Series(labels))
    assert got.notna().sum() == len(labels) - 3, "readable labels were discarded"
    assert got.isna().sum() == 3


def test_the_whole_column_is_still_refused_when_little_of_it_reads():
    """Below the bar the reader must not claim a column it cannot read."""
    labels = ["Jan '23", "Feb '23"] + ["not a date"] * 10
    got = engine.normalize_dates(pd.Series(labels))
    assert got.notna().sum() <= 2


def test_a_mostly_iso_column_is_not_hijacked_by_the_month_year_reader():
    """Whichever reader finds more dates wins; the majority is never sacrificed.

    A file mixing two date styles is malformed — the format guide asks for one
    style throughout — so the two stragglers are reported by name rather than
    silently costing the ten good rows.
    """
    labels = [f"2024-{m:02d}-28" for m in range(1, 11)] + ["Jan '24", "Feb '24"]
    got = engine.normalize_dates(pd.Series(labels))
    assert got.notna().sum() == 10, "the ISO dates must survive"
    assert [l for l, p in zip(labels, got) if pd.isna(p)] == ["Jan '24", "Feb '24"]


def test_a_combined_two_month_label_is_explained_not_just_listed():
    reasons = engine.explain_unreadable_dates(["Jan/Feb '23", "Jan/Feb '24"])
    assert reasons, "no explanation produced"
    joined = " ".join(reasons).lower()
    assert "two months" in joined
    assert "jan/feb '23" in joined
    # it must say what to DO, not only what is wrong
    assert "split" in joined


def test_unreadable_reasons_are_grouped_by_kind():
    reasons = engine.explain_unreadable_dates(["Jan-Feb 2023", "90000", "who knows"])
    joined = " ".join(reasons).lower()
    assert "two months" in joined and "plain numbers" in joined
    assert "not a recognised date" in joined


def test_china_retail_shaped_file_loads_end_to_end():
    """The real failing file's shape: combined Januaries, apostrophe years."""
    rows = []
    for year in (23, 24, 25):
        rows.append((f"Jan/Feb '{year}", 6800.0))
        for index, month in enumerate(MONTHS_3[2:], start=3):
            rows.append((f"{month} '{year}", 3400.0 + index * 20))
    frame = pd.DataFrame(rows, columns=["Date", "Value (RMB bn)"])

    date_col = engine.guess_date_column(frame)
    value_col = engine.guess_value_column(frame, date_col)
    assert date_col == "Date", "the date column must be found"
    assert value_col == "Value (RMB bn)"

    loaded = engine.load_series(frame, date_col, value_col, "Monthly")
    series = loaded.frame[engine.TARGET_COL]
    assert series.notna().sum() == 30, "the 30 readable months must survive"
    assert loaded.dropped_rows == 3
    assert all("Jan/Feb" in label for label in loaded.unreadable_dates)


# ── a file is checked BEFORE anything is forecast from it ──────────

def _china_shaped_frame():
    """Combined Jan/Feb rows, as China's NBS publishes them."""
    rows = []
    for year in (23, 24, 25):
        rows.append((f"Jan/Feb '{year}", 6800.0))
        for index, month in enumerate(MONTHS_3[2:], start=3):
            rows.append((f"{month} '{year}", 3400.0 + index * 20))
    return pd.DataFrame(rows, columns=["Date", "Value (RMB bn)"])


def test_a_file_with_unreadable_rows_is_blocked_not_forecast():
    frame = _china_shaped_frame()
    check = engine.validate_upload(frame, "Date", "Value (RMB bn)", "Monthly")
    assert check["ok"] is False, "a file with unreadable dates must not be runnable"
    assert check["blocking"], "the problem must be reported"
    joined = " ".join(p["detail"] for p in check["blocking"]).lower()
    assert "two months" in joined and "split" in joined


def test_a_clean_file_passes_the_gate():
    frame = pd.DataFrame({
        "Date": pd.date_range("2020-01-31", periods=36, freq="ME"),
        "Sales": np.linspace(100.0, 260.0, 36),
    })
    check = engine.validate_upload(frame, "Date", "Sales", "Monthly")
    assert check["ok"] is True, check["blocking"]
    assert check["usable_rows"] == 36


def test_too_few_rows_is_blocking():
    frame = pd.DataFrame({
        "Date": pd.date_range("2020-01-31", periods=3, freq="ME"),
        "Sales": [1.0, 2.0, 3.0],
    })
    check = engine.validate_upload(frame, "Date", "Sales", "Monthly")
    assert check["ok"] is False
    assert any("usable period" in p["title"] for p in check["blocking"])


def test_a_text_only_value_column_is_blocking():
    frame = pd.DataFrame({
        "Date": pd.date_range("2020-01-31", periods=30, freq="ME"),
        "Sales": ["n/a"] * 30,
    })
    check = engine.validate_upload(frame, "Date", "Sales", "Monthly")
    assert check["ok"] is False
    assert any("no numbers" in p["title"] for p in check["blocking"])


def test_genuine_gaps_are_advisory_not_blocking():
    """Missing periods are a property of the data, not a mistake in the file."""
    dates = list(pd.date_range("2020-01-31", periods=36, freq="ME"))
    values = list(np.linspace(100.0, 260.0, 36))
    for drop in (20, 14, 8):
        dates.pop(drop)
        values.pop(drop)
    check = engine.validate_upload(pd.DataFrame({"Date": dates, "Sales": values}),
                                   "Date", "Sales", "Monthly")
    assert check["ok"] is True, "a gap must not block the run"
    assert any("no value" in a["title"] for a in check["advisories"])


def test_a_short_series_warns_about_sarimax_without_blocking():
    frame = pd.DataFrame({
        "Date": pd.date_range("2023-01-31", periods=12, freq="ME"),
        "Sales": np.linspace(50.0, 90.0, 12),
    })
    check = engine.validate_upload(frame, "Date", "Sales", "Monthly")
    assert check["ok"] is True
    assert any("SARIMAX" in a["title"] for a in check["advisories"])


# ── AI Forecast Assistant: the context it is given ─────────────────

def _real_outcome(periods=48, horizon=3):
    """A genuine run, so the context is built from real model output."""
    index = pd.date_range("2020-01-31", periods=periods, freq="ME")
    rng = np.random.default_rng(5)
    values = (np.linspace(200.0, 420.0, periods)
              * (1 + 0.10 * np.sin(np.arange(periods) / 12 * 2 * np.pi))
              * rng.normal(1.0, 0.01, periods))
    series = pd.Series(values, index=index, name=engine.TARGET_COL)
    frame = series.to_frame()
    diagnostics = engine.data_diagnostics(series, "Monthly", "auto-detect")
    sarima = engine.run_sarima(series, "Monthly", horizon)
    return {
        "loaded": engine.LoadedSeries(frame=frame),
        "series": series,
        "diagnostics": diagnostics,
        "freq_name": "Monthly",
        "horizon": horizon,
        "settings": {"date_col": "Date", "value_col": "Sales"},
        "results": {"SARIMA": sarima},
        "notes": [],
    }


def test_the_assistant_is_told_to_say_run_first_when_there_is_no_run():
    from components import forecast_assistant as fa
    for empty in (None, {}, {"results": {}}):
        assert "NO_RESULTS" in fa.build_context(empty)


def test_the_context_carries_the_numbers_the_page_shows():
    from components import forecast_assistant as fa
    outcome = _real_outcome()
    context = fa.build_context(outcome)
    sarima = outcome["results"]["SARIMA"]
    diagnostics = outcome["diagnostics"]

    assert f"{sarima['mape']:.2f}%" in context, "MAPE must be quotable"
    assert f"{sarima['rmse']:,.0f}" in context, "RMSE must be quotable"
    assert str(sarima["order"]) in context, "the chosen order must be quotable"
    assert f"{sarima['best_aic']:.2f}" in context, "AIC must be quotable"
    assert f"{diagnostics['adf_p']:.4f}" in context
    assert f"{diagnostics['kpss_p']:.4f}" in context
    # each test on its own line with its verdict spelled out, so a small
    # model cannot pair the wrong p-value with the wrong PASS/FAIL
    assert "Ljung-Box at lag" in context
    assert "Shapiro-Wilk: p=" in context
    assert "Durbin-Watson: " in context
    assert "Residual verdict: " in context
    for line in context.splitlines():
        if "-> PASS" in line or "-> FAIL" in line:
            assert line.count("->") == 1, f"two verdicts on one line: {line}"
    assert "BEST=SARIMA" in context


def test_every_forecast_period_reaches_the_context():
    from components import forecast_assistant as fa
    outcome = _real_outcome(horizon=6)
    context = fa.build_context(outcome)
    forecast = outcome["results"]["SARIMA"]["forecast"]
    assert context.count("    FC ") == len(forecast) == 6
    for when, row in forecast.iterrows():
        assert f"FC {pd.Timestamp(when):%Y-%m}: {row['mean']:,.0f}" in context


def test_walk_forward_rows_reach_the_context():
    from components import forecast_assistant as fa
    outcome = _real_outcome()
    context = fa.build_context(outcome)
    expected = len(outcome["results"]["SARIMA"]["wf_actual"])
    assert context.count("    WF ") == expected


def test_a_zero_series_reports_mape_as_unavailable_not_as_a_number():
    """MAPE is infinite with a zero in the series; the model must not quote it."""
    from components import forecast_assistant as fa
    outcome = _real_outcome()
    outcome["results"]["SARIMA"]["mape"] = float("inf")
    context = fa.build_context(outcome)
    assert "inf" not in context.lower().split("ingestion")[0].replace("information", "")
    assert "n/a (series contains zero)" in context
    assert "BEST=SARIMA" in context and "RMSE" in context


def test_ingestion_problems_are_mentioned_so_the_model_can_explain_them():
    from components import forecast_assistant as fa
    outcome = _real_outcome()
    outcome["loaded"] = engine.LoadedSeries(
        frame=outcome["loaded"].frame, duplicates_merged=2, snapped_to_period_end=48)
    context = fa.build_context(outcome)
    assert "2 duplicate period(s) summed" in context
    assert "48 date(s) snapped to period end" in context


def test_ollama_is_the_only_backend(monkeypatch):
    """The portal's OpenAI key is for filing extraction and must stay unused."""
    from components import forecast_assistant as fa
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-be-touched")

    monkeypatch.setattr(fa, "_ollama_models", lambda: ["llama3.1:8b"])
    assert fa.available_models() == ["llama3.1:8b"]

    monkeypatch.setattr(fa, "_ollama_models", lambda: [])
    assert fa.available_models() == [], "no OpenAI fallback may exist"

    source = Path(fa.__file__).read_text()
    assert "chat.completions" not in source
    assert "from openai import" not in source
    assert 'getenv("OPENAI_API_KEY"' not in source


def test_a_backend_failure_never_raises_into_the_page(monkeypatch):
    from components import forecast_assistant as fa

    def boom(messages, model):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(fa, "_stream_ollama", boom)
    text = "".join(fa.stream_reply([{"role": "user", "content": "hi"}], "llama3.1:8b"))
    assert "could not reach Ollama" in text
    assert "forecast above is unaffected" in text


def test_the_configured_model_is_offered_first(monkeypatch):
    from components import forecast_assistant as fa
    monkeypatch.setattr(fa, "_ollama_models", lambda: ["mistral", "llama3.1:8b", "phi3"])
    monkeypatch.setattr(fa, "OLLAMA_MODEL", "llama3.1:8b")
    assert fa.available_models()[0] == "llama3.1:8b"
    assert sorted(fa.available_models()) == ["llama3.1:8b", "mistral", "phi3"]


def test_an_absent_configured_model_does_not_hide_the_others(monkeypatch):
    from components import forecast_assistant as fa
    monkeypatch.setattr(fa, "_ollama_models", lambda: ["mistral"])
    monkeypatch.setattr(fa, "OLLAMA_MODEL", "llama3.1:8b")
    assert fa.available_models() == ["mistral"]


def test_each_residual_test_carries_its_own_verdict():
    """A small model mislabelled a PASS when four tests shared one line."""
    from components import forecast_assistant as fa
    outcome = _real_outcome()
    context = fa.build_context(outcome)
    checks = engine.residual_diagnostics(outcome["results"]["SARIMA"]["residuals"])

    expected = "PASS" if checks["lb_short"] > 0.05 else "FAIL"
    line = next(l for l in context.splitlines()
                if l.strip().startswith(f"Ljung-Box at lag {checks['lags'][0]}:"))
    assert f"p={checks['lb_short']:.4f}" in line
    assert f"-> {expected}" in line, f"verdict contradicts the p-value: {line}"

    verdict = next(l for l in context.splitlines() if "Residual verdict:" in l)
    if checks["failures"]:
        assert all(f in verdict for f in checks["failures"])
    else:
        assert "all four tests PASS" in verdict


# ── period-on-period growth table ──────────────────────────────────

def _growth_history():
    """Three years of monthly data with known, easy arithmetic."""
    index = pd.date_range("2022-01-31", periods=36, freq="ME")
    return pd.Series(np.arange(36, dtype=float) * 100 + 1000, index=index)


def test_growth_table_reports_month_on_month_and_year_on_year():
    history = _growth_history()
    table = engine.growth_table(history, "Monthly")

    assert list(table["Period"][:2]) == ["Jan 2022", "Feb 2022"]
    assert set(table["Type"]) == {"Actual"}
    assert len(table) == len(history)

    # Feb 2022: 1100 from 1000 — +100, +10%.
    february = table.iloc[1]
    assert february["Previous month"] == 1000
    assert february["MoM change"] == 100
    assert february["MoM growth %"] == 10.0
    assert pd.isna(february["YoY growth %"]), "no month a year before Feb 2022"

    # Jan 2023: 2200 from 1000 twelve months earlier — +1200, +120%.
    january = table[table["Period"] == "Jan 2023"].iloc[0]
    assert january["Same month last year"] == 1000
    assert january["YoY change"] == 1200
    assert january["YoY growth %"] == 120.0


def test_growth_table_has_nothing_to_compare_on_the_first_row():
    table = engine.growth_table(_growth_history(), "Monthly")
    first = table.iloc[0]
    assert pd.isna(first["Previous month"])
    assert pd.isna(first["MoM change"])
    assert pd.isna(first["MoM growth %"])
    assert first["Sales"] == 1000


def test_forecast_rows_continue_from_the_last_actual_period():
    history = _growth_history()
    forecast = pd.Series([4600.0, 4700.0],
                         index=pd.date_range("2025-01-31", periods=2, freq="ME"))
    table = engine.growth_table(history, "Monthly", forecast)

    assert list(table["Type"][-2:]) == ["Forecast", "Forecast"]
    assert len(table) == len(history) + 2

    handover = table.iloc[len(history)]
    assert handover["Period"] == "Jan 2025"
    assert handover["Previous month"] == history.iloc[-1], \
        "the first forecast month must be measured against the last actual one"
    assert handover["Same month last year"] == history.loc["2024-01-31"]


def test_a_forecast_overlapping_history_does_not_duplicate_periods():
    history = _growth_history()
    overlapping = pd.Series(
        [99.0, 4600.0], index=pd.to_datetime(["2024-12-31", "2025-01-31"]))
    table = engine.growth_table(history, "Monthly", overlapping)
    assert len(table) == len(history) + 1
    assert table["Period"].is_unique
    assert table.iloc[-2]["Sales"] == history.iloc[-1], "actuals win over a forecast"


def test_growth_from_zero_is_blank_not_infinite():
    index = pd.date_range("2024-01-31", periods=3, freq="ME")
    table = engine.growth_table(pd.Series([0.0, 500.0, 750.0], index=index), "Monthly")
    assert pd.isna(table.iloc[1]["MoM growth %"]), "nothing grows from zero"
    assert table.iloc[1]["MoM change"] == 500, "the absolute change is still reported"
    assert table.iloc[2]["MoM growth %"] == 50.0


def test_a_blank_period_blanks_its_growth_but_keeps_the_row():
    frame = monthly_series(30)
    frame.loc[5, "Sales"] = np.nan
    loaded = engine.load_series(frame, "Date", "Sales", "Monthly")
    table = engine.growth_table(loaded.frame[engine.TARGET_COL], "Monthly")

    assert len(table) == 30
    assert pd.isna(table.iloc[5]["Sales"])
    assert pd.isna(table.iloc[5]["MoM growth %"])
    assert pd.isna(table.iloc[6]["MoM growth %"]), "the month after a blank has no base"
    assert pd.notna(table.iloc[7]["MoM growth %"])


def test_growth_percent_keeps_its_direction_when_a_series_goes_negative():
    index = pd.date_range("2024-01-31", periods=3, freq="ME")
    table = engine.growth_table(pd.Series([-200.0, -100.0, -300.0], index=index),
                                "Monthly")
    assert table.iloc[1]["MoM growth %"] == 50.0, "a loss halving is growth"
    assert table.iloc[2]["MoM growth %"] == -200.0


@pytest.mark.parametrize("freq_name,freq,prefix,noun,first", [
    ("Weekly", "W", "WoW", "week", "2024-01-07"),
    ("Monthly", "ME", "MoM", "month", "Jan 2024"),
    ("Quarterly", "QE", "QoQ", "quarter", "Q1 2024"),
])
def test_each_frequency_names_its_own_periods_and_growth(freq_name, freq, prefix,
                                                         noun, first):
    index = pd.date_range("2024-01-01", periods=8, freq=freq)
    table = engine.growth_table(
        pd.Series(np.arange(8, dtype=float) + 1, index=index), freq_name)
    assert table["Period"].iloc[0] == first
    assert f"{prefix} growth %" in table.columns
    assert f"Previous {noun}" in table.columns
    assert f"Same {noun} last year" in table.columns


def test_annual_data_gets_one_growth_block_not_two():
    """A year against the previous year is already year-on-year."""
    index = pd.date_range("2018-12-31", periods=6, freq="YE")
    table = engine.growth_table(pd.Series(np.arange(6, dtype=float) + 1, index=index),
                               "Annual")
    assert table["Period"].iloc[0] == "2018"
    assert list(table.columns) == ["Period", "Type", "Sales", "Previous year",
                                   "YoY change", "YoY growth %"]
    assert table.iloc[1]["YoY growth %"] == 100.0


def test_the_year_on_year_shift_follows_the_frequency():
    """Quarterly YoY is four periods back, not twelve."""
    index = pd.date_range("2022-03-31", periods=8, freq="QE")
    values = pd.Series([100.0, 110, 120, 130, 200, 220, 240, 260], index=index)
    table = engine.growth_table(values, "Quarterly")
    assert table.iloc[4]["Same quarter last year"] == 100
    assert table.iloc[4]["YoY growth %"] == 100.0


def test_the_growth_table_survives_a_round_trip_through_the_download():
    """What the Download CSV button hands over is what the table shows."""
    import io
    table = engine.growth_table(_growth_history(), "Monthly")
    restored = pd.read_csv(io.StringIO(table.to_csv(index=False)))
    assert list(restored.columns) == list(table.columns)
    pd.testing.assert_frame_equal(restored, table, check_dtype=False)
