"""Criterion columns must reach the Key Devs results grid.

Key Devs mode renders one row per EVENT, built only from coreiq_company_events.
The criterion annotations (Industry, Country, financial metrics, segments) are
computed onto `scr_working_df` per COMPANY and were never carried across, so a
user who added "Industry Classifications: Coffee & Beverage" saw no Industry
column at all — while Companies mode and People mode both showed it.

The join is on the bare ticker, which is NOT unique: JD, LULU and TSCO each name
two different companies in coreiq_companies (JD.com vs JD Sports, Lululemon vs
Lulu Retail, Tractor Supply vs Tesco). A plain merge would multiply every event
row for those three. These tests pin that, plus the value semantics.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

from data.screening_service import (  # noqa: E402
    apply_geography_criterion,
    apply_industry_criterion,
    keep_working_set_companies,
    merge_company_columns,
)


COMPANIES = pd.DataFrame([
    {"ticker": "SBUX", "company_name": "Starbucks",
     "sector": "Coffee & Beverage", "country": "United States"},
    {"ticker": "WMT", "company_name": "Walmart Inc.",
     "sector": "Broadline Retail", "country": "United States"},
    {"ticker": "TSCO", "company_name": "Tesco PLC",
     "sector": "Food Retail", "country": "United Kingdom"},
])


def test_industry_criterion_filters_to_the_selected_industries():
    """The form promises "Filter companies to those in selected industries".
    It used to keep all 431 companies and only annotate them, so picking
    "Coffee & Beverage" still reported "431 companies matched"."""
    out, meta = apply_industry_criterion(["Coffee & Beverage"], COMPANIES)

    assert list(out["ticker"]) == ["SBUX"]
    assert list(out["Industry"]) == ["Coffee & Beverage"]
    assert meta["rows_in"] == 3 and meta["rows_out"] == 1


def test_industry_criterion_accepts_several_industries():
    out, _ = apply_industry_criterion(
        ["Coffee & Beverage", "Food Retail"], COMPANIES)
    assert sorted(out["ticker"]) == ["SBUX", "TSCO"]


def test_empty_industry_selection_is_not_a_filter():
    out, meta = apply_industry_criterion([], COMPANIES)
    assert len(out) == 3 and meta["rows_out"] == 3


def test_geography_criterion_filters_to_the_selected_countries():
    out, meta = apply_geography_criterion(["United Kingdom"], COMPANIES)

    assert list(out["ticker"]) == ["TSCO"]
    assert list(out["Country"]) == ["United Kingdom"]
    assert meta["rows_in"] == 3 and meta["rows_out"] == 1


def test_shared_ticker_rows_outside_the_working_set_are_dropped():
    """The events query joins companies on the bare ticker, so a TSCO event comes
    back as BOTH Tractor Supply and Tesco. Screening for Food Retail must not
    still list Tractor Supply."""
    grid = pd.DataFrame([
        {"Company Name(s)": "Tractor Supply Company (TSCO)", "Ticker": "TSCO",
         "_CompanyName": "Tractor Supply Company"},
        {"Company Name(s)": "Tesco PLC (L:TSCO)", "Ticker": "TSCO",
         "_CompanyName": "Tesco PLC"},
    ])
    working = universe([
        {"ticker": "TSCO", "company_name": "Tesco PLC", "Industry": "Food Retail"},
    ])

    out = keep_working_set_companies(grid, working)

    assert list(out["_CompanyName"]) == ["Tesco PLC"]


def test_unshared_tickers_are_never_dropped_even_if_the_name_differs():
    """A ticker that appears under one company name only is left alone, so a
    name that fails to parse can never empty the grid."""
    grid = pd.DataFrame([
        {"Company Name(s)": "Starbucks (SBUX)", "Ticker": "SBUX",
         "_CompanyName": "Starbucks Corporation"},
        {"Company Name(s)": "Starbucks (SBUX)", "Ticker": "SBUX",
         "_CompanyName": "Starbucks Corporation"},
    ])
    working = universe([
        {"ticker": "SBUX", "company_name": "Starbucks", "Industry": "Coffee & Beverage"},
    ])

    assert len(keep_working_set_companies(grid, working)) == 2


def events(*tickers):
    """One key-dev grid row per ticker, in the shape the grid actually holds —
    note the DATE column comes first, not the company column."""
    return pd.DataFrame([
        {"Key Developments By Date": "Aug-20-2026",
         "Company Name(s)": f"Co {t} ({t})",
         "Key Development Headline": f"news {t}",
         "Ticker": t}
        for t in tickers
    ])


ANCHOR = "Company Name(s)"


def universe(rows):
    return pd.DataFrame(rows)


def test_criterion_column_lands_on_matching_event_rows():
    grid = events("SBUX", "WMT")
    working = universe([
        {"ticker": "SBUX", "Industry": "Coffee & Beverage"},
        {"ticker": "WMT", "Industry": None},
    ])

    out = merge_company_columns(grid, working, ["Industry"])

    assert list(out["Industry"]) == ["Coffee & Beverage", "N/A"]
    assert len(out) == 2


def test_duplicate_tickers_do_not_multiply_event_rows():
    """TSCO is two companies. One event row must stay one event row."""
    grid = events("TSCO", "SBUX")
    working = universe([
        {"ticker": "TSCO", "Industry": None},                  # Tractor Supply
        {"ticker": "TSCO", "Industry": "Food Retail"},          # Tesco PLC
        {"ticker": "SBUX", "Industry": "Coffee & Beverage"},
    ])

    out = merge_company_columns(grid, working, ["Industry"])

    assert len(out) == 2, f"event rows multiplied: {len(out)}"
    # The company that actually matched the criterion wins over the blank row.
    assert out.loc[out["Ticker"] == "TSCO", "Industry"].iloc[0] == "Food Retail"


def test_columns_land_directly_behind_the_company_column():
    """The events frame starts with the DATE column, so the criterion columns
    must be anchored on the company column explicitly — otherwise they sort in
    front of the company name."""
    grid = events("SBUX")
    working = universe([{"ticker": "SBUX", "Industry": "Coffee & Beverage"}])

    out = merge_company_columns(grid, working, ["Industry"], after=ANCHOR)

    assert list(out.columns)[:2] == [ANCHOR, "Industry"], list(out.columns)
    assert "Key Developments By Date" in out.columns


def test_multiple_criterion_columns_keep_their_order():
    grid = events("SBUX")
    working = universe([
        {"ticker": "SBUX", "Industry": "Coffee & Beverage",
         "Country": "United States", "Revenue FY2025": 36176.0},
    ])

    out = merge_company_columns(
        grid, working, ["Industry", "Country", "Revenue FY2025"], after=ANCHOR)

    assert list(out.columns)[:4] == [
        ANCHOR, "Industry", "Country", "Revenue FY2025"]


def test_no_event_column_is_lost_when_columns_are_reordered():
    grid = events("SBUX", "WMT")
    working = universe([{"ticker": "SBUX", "Industry": "Coffee & Beverage"}])

    out = merge_company_columns(grid, working, ["Industry"], after=ANCHOR)

    assert set(grid.columns).issubset(set(out.columns))
    assert len(out.columns) == len(grid.columns) + 1


def test_ticker_absent_from_universe_gets_na_not_a_dropped_row():
    grid = events("SBUX", "GHOST")
    working = universe([{"ticker": "SBUX", "Industry": "Coffee & Beverage"}])

    out = merge_company_columns(grid, working, ["Industry"])

    assert len(out) == 2
    assert out.loc[out["Ticker"] == "GHOST", "Industry"].iloc[0] == "N/A"


def test_no_columns_requested_returns_the_grid_untouched():
    grid = events("SBUX")
    out = merge_company_columns(grid, universe([{"ticker": "SBUX"}]), [])
    assert list(out.columns) == list(grid.columns)


def test_missing_ticker_column_is_not_fatal():
    """The grid parses Ticker from the display label; if that ever fails the
    results must still render, just without the criterion columns."""
    grid = events("SBUX").drop(columns=["Ticker"])
    working = universe([{"ticker": "SBUX", "Industry": "Coffee & Beverage"}])

    out = merge_company_columns(grid, working, ["Industry"])

    assert "Industry" not in out.columns
    assert len(out) == 1


def test_same_ticker_two_companies_each_get_their_own_attributes():
    """coreiq_company_events is keyed on the bare ticker, so its company JOIN
    already emits one row per candidate company. Tractor Supply must not inherit
    Tesco's industry just because both are TSCO."""
    grid = pd.DataFrame([
        {"Key Developments By Date": "Aug-19-2026", "Company Name(s)": "Tractor Supply Company (TSCO)",
         "Ticker": "TSCO", "_CompanyName": "Tractor Supply Company"},
        {"Key Developments By Date": "Aug-19-2026", "Company Name(s)": "Tesco PLC (L:TSCO)",
         "Ticker": "TSCO", "_CompanyName": "Tesco PLC"},
    ])
    working = universe([
        {"ticker": "TSCO", "company_name": "Tractor Supply Company",
         "Industry": None, "Country": "United States"},
        {"ticker": "TSCO", "company_name": "Tesco PLC",
         "Industry": "Food Retail", "Country": "United Kingdom"},
    ])

    out = merge_company_columns(grid, working, ["Industry", "Country"], after=ANCHOR)

    tractor = out[out["_CompanyName"] == "Tractor Supply Company"].iloc[0]
    tesco = out[out["_CompanyName"] == "Tesco PLC"].iloc[0]
    assert tractor["Country"] == "United States"
    assert tractor["Industry"] == "N/A"          # did not match the criterion
    assert tesco["Country"] == "United Kingdom"
    assert tesco["Industry"] == "Food Retail"


def test_unknown_company_name_falls_back_to_the_ticker_match():
    grid = pd.DataFrame([
        {"Key Developments By Date": "Aug-19-2026", "Company Name(s)": "Starbucks (SBUX)",
         "Ticker": "SBUX", "_CompanyName": "Starbucks Corporation"},   # label differs
    ])
    working = universe([
        {"ticker": "SBUX", "company_name": "Starbucks", "Industry": "Coffee & Beverage"},
    ])

    out = merge_company_columns(grid, working, ["Industry"], after=ANCHOR)

    assert out["Industry"].iloc[0] == "Coffee & Beverage"


def test_pipeline_narrows_the_universe_not_just_the_column(monkeypatch):
    """The criterion function filtering is not enough on its own: the pipeline
    used to merge the new column onto the UNTOUCHED universe and report
    `rows_out = len(full_df)`, which is what printed "431 companies matched"
    under a one-company industry."""
    import data.screening_service as svc

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: COMPANIES.copy())

    working, trace = svc.recompute_working_set(
        [{"type": "industry", "industries": ["Coffee & Beverage"]}])

    assert list(working["ticker"]) == ["SBUX"]
    assert trace[-1]["rows_out"] == 1
    assert list(working["Industry"]) == ["Coffee & Beverage"]


def test_pipeline_keeps_the_right_company_for_a_shared_ticker(monkeypatch):
    import data.screening_service as svc

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: pd.DataFrame([
        {"ticker": "TSCO", "company_name": "Tractor Supply Company",
         "sector": "Home and Home Improvement", "country": "United States"},
        {"ticker": "TSCO", "company_name": "Tesco PLC",
         "sector": "Food Retail", "country": "United Kingdom"},
    ]))

    working, trace = svc.recompute_working_set(
        [{"type": "industry", "industries": ["Food Retail"]}])

    assert list(working["company_name"]) == ["Tesco PLC"]
    assert trace[-1]["rows_out"] == 1


def test_pipeline_reports_zero_when_nothing_matches(monkeypatch):
    import data.screening_service as svc

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: COMPANIES.copy())

    working, trace = svc.recompute_working_set(
        [{"type": "industry", "industries": ["Nonexistent Industry"]}])

    assert len(working) == 0
    assert trace[-1]["rows_out"] == 0


def test_financial_criteria_never_drop_a_company(monkeypatch):
    """Business rule: a company that does not report the metric must stay in the
    results showing N/A, not disappear. Only industry and geography narrow."""
    import data.screening_service as svc

    assert "financial" not in svc.FILTERING_CRITERION_TYPES
    assert "keydevs" not in svc.FILTERING_CRITERION_TYPES
    assert svc.FILTERING_CRITERION_TYPES == {"industry", "geography"}

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: COMPANIES.copy())
    # A financial criterion that resolves for exactly one company.
    monkeypatch.setattr(svc, "apply_financial_criterion",
                        lambda crit, df, **kw: (
                            df.assign(**{crit["display_col"]: [1.0, None, None]}),
                            {"type": "financial"}))

    working, trace = svc.recompute_working_set([{
        "type": "financial", "display_col": "Revenue FY2025",
        "metric_info": {}, "operator": ">", "value1": 0,
    }])

    assert len(working) == 3, "a non-reporting company was dropped"
    assert trace[-1]["rows_out"] == 3
    assert working["Revenue FY2025"].isna().sum() == 2   # shown as N/A, still listed


def test_industry_filter_and_financial_annotation_compose(monkeypatch):
    """Both scenarios together: industry narrows, financial only annotates."""
    import data.screening_service as svc

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: COMPANIES.copy())
    monkeypatch.setattr(svc, "apply_financial_criterion",
                        lambda crit, df, **kw: (
                            df.assign(**{crit["display_col"]: [None] * len(df)}),
                            {"type": "financial"}))

    working, trace = svc.recompute_working_set([
        {"type": "industry", "industries": ["Coffee & Beverage", "Food Retail"]},
        {"type": "financial", "display_col": "Revenue FY2025",
         "metric_info": {}, "operator": ">", "value1": 0},
    ])

    assert sorted(working["ticker"]) == ["SBUX", "TSCO"]   # industry narrowed
    assert trace[-1]["rows_out"] == 2                      # financial dropped nobody
    assert working["Revenue FY2025"].isna().all()          # and shows N/A


def test_a_failed_event_query_raises_instead_of_looking_empty(monkeypatch):
    """A timed-out query used to come back as an empty DataFrame, so the page
    printed "No key development events found" for a screen that really has
    10,708 events. Failure and emptiness must be distinguishable."""
    import data.screening_service as svc

    def boom(*a, **kw):
        raise RuntimeError("(2013, 'Lost connection ... read operation timed out')")

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", boom)

    with pytest.raises(svc.KeydevsQueryError):
        svc.get_keydevs_events_for_tickers(("SBUX",), ("M&A Activity",), limit=10)
    with pytest.raises(svc.KeydevsQueryError):
        svc.get_keydevs_events_count(("SBUX",), ("M&A Activity",))


def test_genuinely_empty_result_is_still_empty_not_an_error(monkeypatch):
    import data.screening_service as svc

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", lambda *a, **kw: [])

    df, cursor = svc.get_keydevs_events_for_tickers(
        ("SBUX",), ("M&A Activity",), limit=10)
    assert df.empty and cursor is None
    assert svc.get_keydevs_events_count(("SBUX",), ("M&A Activity",)) == 0


def test_same_event_is_not_listed_twice_when_both_companies_are_in_scope():
    """With no criterion to disambiguate, both TSCO companies are legitimately in
    the working set, so the working-set filter cannot drop either — the event would
    still appear twice. 10,708 events must not export as 10,773 rows."""
    grid = pd.DataFrame([
        {"_event_id": 419136, "Company Name(s)": "Tractor Supply Company (TSCO)",
         "Ticker": "TSCO", "_CompanyName": "Tractor Supply Company"},
        {"_event_id": 419136, "Company Name(s)": "Tesco PLC (L:TSCO)",
         "Ticker": "TSCO", "_CompanyName": "Tesco PLC"},
        {"_event_id": 500001, "Company Name(s)": "Starbucks (SBUX)",
         "Ticker": "SBUX", "_CompanyName": "Starbucks"},
    ])
    working = universe([
        {"ticker": "TSCO", "company_name": "Tractor Supply Company"},
        {"ticker": "TSCO", "company_name": "Tesco PLC"},
        {"ticker": "SBUX", "company_name": "Starbucks"},
    ])

    out = keep_working_set_companies(grid, working)

    assert len(out) == 2, f"event listed twice: {list(out['_event_id'])}"
    assert sorted(out["_event_id"]) == [419136, 500001]
    # first in the query's newest-first order wins
    assert out.loc[out["_event_id"] == 419136, "_CompanyName"].iloc[0] == "Tractor Supply Company"


def test_dedupe_does_not_touch_distinct_events():
    grid = pd.DataFrame([
        {"_event_id": i, "Company Name(s)": "Starbucks (SBUX)",
         "Ticker": "SBUX", "_CompanyName": "Starbucks"} for i in range(5)
    ])
    working = universe([{"ticker": "SBUX", "company_name": "Starbucks"}])
    assert len(keep_working_set_companies(grid, working)) == 5


def test_keydev_membership_path_skips_the_detail_fetch(monkeypatch):
    """Key Devs mode never shows the per-company detail column, so it must not
    pay to build it. Profiled: the detail query is one network packet per row
    (3,461 packets for 3,453 rows); membership needs 388."""
    import data.screening_service as svc
    seen = {}

    def fake_query(q, *a, **kw):
        seen["sql"] = " ".join(q.split())
        return [{"ticker": "SBUX"}]

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", fake_query)

    out, dbg = svc.apply_keydevs_criterion(
        {"type": "keydevs", "categories": ["M&A Activity"],
         "display_col": "Key Developments"},
        COMPANIES, detail_column=False)

    assert "SELECT DISTINCT ticker" in seen["sql"]
    assert "headline" not in seen["sql"].lower()
    assert "ROW_NUMBER" not in seen["sql"]
    assert dbg["with_data"] == 1
    assert dbg["rows_out"] == 3            # still non-filtering
    assert list(out["Key Developments"]) == ["Yes", None, None]


def test_keydev_detail_path_still_fetches_headlines(monkeypatch):
    """Companies mode DOES display the detail column — don't break it."""
    import data.screening_service as svc
    seen = {}
    monkeypatch.setattr(svc.db_manager, "execute_query_readonly",
                        lambda q, *a, **kw: (seen.__setitem__("sql", " ".join(q.split())), [])[1])

    svc.apply_keydevs_criterion(
        {"type": "keydevs", "categories": ["M&A Activity"],
         "display_col": "Key Developments"},
        COMPANIES, detail_column=True)

    assert "headline" in seen["sql"].lower()
    assert "ROW_NUMBER" in seen["sql"]


def test_criterion_cache_key_separates_the_two_keydev_shapes(monkeypatch):
    """A mode switch must not serve a membership result to Companies mode."""
    import data.screening_service as svc
    monkeypatch.setattr(svc, "get_base_company_universe", lambda: COMPANIES.copy())
    monkeypatch.setattr(svc.db_manager, "execute_query_readonly",
                        lambda q, *a, **kw: [{"ticker": "SBUX"}])
    crit = [{"type": "keydevs", "categories": ["M&A Activity"],
             "display_col": "Key Developments"}]
    cache = {}
    svc.recompute_working_set(crit, criterion_cache=cache, keydev_details=False)
    svc.recompute_working_set(crit, criterion_cache=cache, keydev_details=True)
    assert len(cache) == 2, "both shapes must cache separately"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
