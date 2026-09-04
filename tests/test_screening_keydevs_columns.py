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


# ---------------------------------------------------------------------------
# All-companies coverage: screening past Coresight's 450
# ---------------------------------------------------------------------------
# coreiq_companies is the 450-row coverage list, but coreiq_company_events holds
# 4,327 tickers. Bounding the event queries by that list hid 9,691 of the 20,753
# M&A Activity events (46.7% of the category). These pin the widened path.


def test_all_tickers_sentinel_drops_the_ticker_clause():
    """The all-companies screen IS the whole event table, so naming every ticker
    is a no-op that costs a 40 KB SQL literal. Measured: 305 ms without the
    clause against 5,689 ms with it."""
    import data.screening_service as svc

    assert svc._keydev_ticker_clause(svc.ALL_TICKERS) == ""
    assert svc._keydev_ticker_clause(svc.ALL_TICKERS, "e") == ""


def test_named_tickers_still_get_an_in_list():
    import data.screening_service as svc

    clause = svc._keydev_ticker_clause(("SBUX", "WMT"), "e")
    assert clause.startswith("AND e.ticker IN (")
    assert "'SBUX'" in clause and "'WMT'" in clause


def test_event_count_query_omits_the_in_list_for_all_tickers(monkeypatch):
    import data.screening_service as svc
    seen = {}

    def fake(q, *a, **kw):
        seen["sql"] = " ".join(q.split())
        return [{"c": 20753}]

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", fake)

    total = svc.get_keydevs_events_count.__wrapped__(
        svc.ALL_TICKERS, ("M&A Activity",))

    assert total == 20753
    assert "ticker IN" not in seen["sql"]
    assert "event_category IN ('M&A Activity')" in seen["sql"]


def test_event_page_query_omits_the_in_list_for_all_tickers(monkeypatch):
    import data.screening_service as svc
    seen = {}

    def fake(q, *a, **kw):
        seen["sql"] = " ".join(q.split())
        return []

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", fake)
    svc.get_keydevs_events_for_tickers.__wrapped__(
        svc.ALL_TICKERS, ("M&A Activity",), limit=500)

    assert "e.ticker IN" not in seen["sql"]
    # ...but the fallback masters must still be joined, or every extra company
    # renders as a bare ticker with no name and no industry.
    assert "coreiq_av_companies_all" in seen["sql"]
    assert "coreiq_sec_companies_all" in seen["sql"]


def test_keydev_criterion_drops_the_in_list_when_all_tickers(monkeypatch):
    """With the toggle on, working_df already IS every event ticker."""
    import data.screening_service as svc
    seen = {}

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly",
                        lambda q, *a, **kw: (seen.__setitem__("sql", " ".join(q.split())),
                                             [{"ticker": "SBUX"}])[1])

    svc.apply_keydevs_criterion(
        {"type": "keydevs", "categories": ["M&A Activity"],
         "display_col": "Key Developments"},
        COMPANIES, detail_column=False, all_tickers=True)

    assert "ticker IN" not in seen["sql"]

    svc.apply_keydevs_criterion(
        {"type": "keydevs", "categories": ["M&A Activity"],
         "display_col": "Key Developments"},
        COMPANIES, detail_column=False, all_tickers=False)

    assert "ticker IN" in seen["sql"]


def test_industry_label_prefers_the_coresight_classification():
    """coreiq_company_events has no industry column, so the label is a JOIN.
    Coresight's curated value wins and is marked "(Primary)"."""
    from data.screening_service import _keydev_industry_label

    assert _keydev_industry_label(
        {"industry": "Apparel and Footwear Retail",
         "industry_fallback": "APPAREL RETAIL"}
    ) == "Apparel and Footwear Retail (Primary)"


def test_industry_label_falls_back_to_alpha_vantage_untagged():
    """The AV values are a different (Yahoo/Morningstar) taxonomy and carry stale
    ticker mappings, so they must never read as a Coresight classification."""
    from data.screening_service import _keydev_industry_label

    assert _keydev_industry_label(
        {"industry": None, "industry_fallback": "BANKS - REGIONAL"}) == "Banks - Regional"
    assert _keydev_industry_label(
        {"industry": None, "industry_fallback": "OIL & GAS E&P"}) == "Oil & Gas E&P"
    assert "(Primary)" not in _keydev_industry_label(
        {"industry": None, "industry_fallback": "BIOTECHNOLOGY"})


def test_industry_label_is_blank_when_nothing_is_known():
    from data.screening_service import _keydev_industry_label

    assert _keydev_industry_label({}) == ""
    assert _keydev_industry_label({"industry": "", "industry_fallback": ""}) == ""
    # AV writes the literal string 'NONE'; the query NULLIFs it, but be safe.
    assert _keydev_industry_label({"industry": None, "industry_fallback": None}) == ""


@pytest.mark.skip(reason="Industry column parked pending a data-team decision on "
                         "whether coreiq_av_companies_all.industry is an acceptable "
                         "source (8.7% blank). Un-skip together with the commented "
                         "line in _keydevs_records_from_rows.")
def test_results_rows_carry_an_industry_column():
    """The industry was computed and then thrown away — the column never reached
    the grid at all."""
    from data.screening_service import _keydevs_records_from_rows
    import datetime as dt

    rows = [{
        "event_id": 1, "event_date": dt.date(2026, 8, 27), "event_subtype": "Deal News",
        "event_category": "M&A Activity", "ticker": "BMY", "company_name": "Bristol-Myers Squibb",
        "exchange_acronym": "NYSE", "headline": "h", "situation": "s",
        "source": "AV News — Yahoo Finance", "source_ref": "", "source_detail": "",
        "industry": None, "industry_fallback": "DRUG MANUFACTURERS - GENERAL",
    }]

    rec = _keydevs_records_from_rows(rows)[0]

    assert rec["Industry"] == "Drug Manufacturers - General"
    assert rec["Company Name(s)"] == "Bristol-Myers Squibb (NYSE:BMY)"


def test_widened_universe_keeps_coresight_rows_and_adds_the_rest(monkeypatch):
    """The union: Coresight rows verbatim, plus one row per extra event ticker."""
    import data.screening_service as svc

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: pd.DataFrame([
        {"ticker": "SBUX", "company_name": "Starbucks", "sector": "Coffee & Beverage",
         "exchange": "NASDAQ", "country": "United States"},
    ]))
    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", lambda *a, **kw: [
        {"ticker": "SBUX", "company_name": "Starbucks Corp", "exchange": "NASDAQ",
         "country": "USA"},                                    # already covered
        {"ticker": "BMY", "company_name": "Bristol-Myers Squibb Company",
         "exchange": "NYSE", "country": "USA"},
        {"ticker": "GHOST", "company_name": None, "exchange": None, "country": None},
    ])

    out = svc.get_all_companies_universe.__wrapped__()

    assert sorted(out["ticker"]) == ["BMY", "GHOST", "SBUX"]
    # the Coresight row is untouched — not overwritten by the AV name
    sbux = out[out["ticker"] == "SBUX"].iloc[0]
    assert sbux["company_name"] == "Starbucks"
    assert sbux["sector"] == "Coffee & Beverage"
    # a ticker with no name anywhere still renders as itself, never blank
    assert out[out["ticker"] == "GHOST"].iloc[0]["company_name"] == "GHOST"


def test_extra_companies_have_no_coresight_sector(monkeypatch):
    """`sector` is what the Industry criterion filters on and its dropdown is the
    curated 79-value Coresight taxonomy. Pouring Alpha Vantage labels in would
    silently mix two taxonomies in one filter."""
    import data.screening_service as svc

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: pd.DataFrame([
        {"ticker": "SBUX", "company_name": "Starbucks", "sector": "Coffee & Beverage",
         "exchange": "NASDAQ", "country": "United States"},
    ]))
    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", lambda *a, **kw: [
        {"ticker": "BMY", "company_name": "Bristol-Myers Squibb Company",
         "exchange": "NYSE", "country": "USA"},
    ])

    out = svc.get_all_companies_universe.__wrapped__()

    assert out[out["ticker"] == "BMY"].iloc[0]["sector"] == ""


def test_widened_universe_degrades_to_coresight_when_the_query_fails(monkeypatch):
    """A failed lookup must not empty the screen."""
    import data.screening_service as svc

    base = pd.DataFrame([{"ticker": "SBUX", "company_name": "Starbucks",
                          "sector": "Coffee & Beverage", "exchange": "NASDAQ",
                          "country": "United States"}])
    monkeypatch.setattr(svc, "get_base_company_universe", lambda: base)

    def boom(*a, **kw):
        raise RuntimeError("read timeout")

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", boom)

    assert list(svc.get_all_companies_universe.__wrapped__()["ticker"]) == ["SBUX"]


def test_pipeline_uses_the_widened_universe_when_asked(monkeypatch):
    import data.screening_service as svc

    narrow = pd.DataFrame([{"ticker": "SBUX", "company_name": "Starbucks",
                            "sector": "Coffee & Beverage", "country": "United States"}])
    wide = pd.concat([narrow, pd.DataFrame([
        {"ticker": "BMY", "company_name": "Bristol-Myers Squibb",
         "sector": "", "country": "USA"}])], ignore_index=True)

    monkeypatch.setattr(svc, "get_base_company_universe", lambda: narrow.copy())
    monkeypatch.setattr(svc, "get_all_companies_universe", lambda: wide.copy())

    default, _ = svc.recompute_working_set([])
    widened, _ = svc.recompute_working_set([], all_companies=True)

    assert list(default["ticker"]) == ["SBUX"]
    assert sorted(widened["ticker"]) == ["BMY", "SBUX"]


def test_industry_criterion_still_narrows_back_to_coresight(monkeypatch):
    """"Enable everything, show what's there" must not mean an Industry criterion
    silently keeps companies that have no Coresight industry at all."""
    import data.screening_service as svc

    wide = pd.DataFrame([
        {"ticker": "SBUX", "company_name": "Starbucks",
         "sector": "Coffee & Beverage", "country": "United States"},
        {"ticker": "BMY", "company_name": "Bristol-Myers Squibb",
         "sector": "", "country": "USA"},
    ])
    monkeypatch.setattr(svc, "get_all_companies_universe", lambda: wide.copy())

    working, trace = svc.recompute_working_set(
        [{"type": "industry", "industries": ["Coffee & Beverage"]}],
        all_companies=True)

    assert list(working["ticker"]) == ["SBUX"]
    assert trace[-1]["rows_out"] == 1


def test_keydev_criterion_still_annotates_never_drops_in_wide_mode(monkeypatch):
    """The whole point of the widened screen: a company with no matching event
    stays listed showing N/A, exactly as in the narrow one."""
    import data.screening_service as svc

    wide = pd.DataFrame([
        {"ticker": "SBUX", "company_name": "Starbucks", "sector": "Coffee & Beverage",
         "country": "United States"},
        {"ticker": "BMY", "company_name": "Bristol-Myers Squibb", "sector": "",
         "country": "USA"},
    ])
    monkeypatch.setattr(svc, "get_all_companies_universe", lambda: wide.copy())
    monkeypatch.setattr(svc.db_manager, "execute_query_readonly",
                        lambda *a, **kw: [{"ticker": "BMY"}])

    working, trace = svc.recompute_working_set(
        [{"type": "keydevs", "categories": ["M&A Activity"],
          "display_col": "Key Developments"}],
        all_companies=True, keydev_details=False)

    assert len(working) == 2, "the widened screen dropped a company"
    assert trace[-1]["with_data"] == 1


# ---------------------------------------------------------------------------
# Excel export must honour the grid's column filters
# ---------------------------------------------------------------------------
# The Excel button used to be rendered ABOVE the grid and re-queried the whole
# result set, so filtering the grid and clicking Excel still downloaded every
# row. Verified live: grid_state carries
#   {"filter": {"filterModel": {"Key Developments by Type": {"values": [...]}}}}


def _grid_resp(state):
    """Stand-in for an AgGridReturn carrying just the grid state."""
    class _Resp:
        grid_state = state
        data = None
    return _Resp()


def test_filter_model_read_from_grid_state():
    from pages.screening import grid_filter_model

    model = grid_filter_model(_grid_resp({
        "filter": {"filterModel": {
            "Key Developments by Type": {"values": ["Deal News", "M&A Closing"]}}},
    }))

    assert model == {"Key Developments by Type": {"Deal News", "M&A Closing"}}


def test_filter_model_accepts_a_flat_filter_model():
    """AG Grid has moved this key between versions — accept either shape."""
    from pages.screening import grid_filter_model

    model = grid_filter_model(_grid_resp(
        {"filterModel": {"Industry": {"values": ["Oil & Gas E&P"]}}}))

    assert model == {"Industry": {"Oil & Gas E&P"}}


def test_no_filter_means_empty_model_not_none():
    """An unfiltered grid must be distinguishable from an unreadable one, or the
    export cannot tell "export everything" from "export nothing"."""
    from pages.screening import grid_filter_model

    assert grid_filter_model(_grid_resp({})) == {}
    assert grid_filter_model(_grid_resp({"filter": {"filterModel": {}}})) == {}
    assert grid_filter_model(None) == {}


def test_unreadable_filter_entries_are_skipped_not_guessed():
    from pages.screening import grid_filter_model

    model = grid_filter_model(_grid_resp({"filter": {"filterModel": {
        "Good": {"values": ["a"]},
        "Weird": {"type": "contains", "filter": "x"},   # not our filter's shape
    }}}))

    assert model == {"Good": {"a"}}


def test_export_filter_keeps_only_matching_rows():
    from pages.screening import apply_grid_filter_model

    df = pd.DataFrame({
        "Key Developments by Type": ["Deal News", "M&A Closing", "Deal News"],
        "Company Name(s)": ["A", "B", "C"],
    })

    out = apply_grid_filter_model(df, {"Key Developments by Type": {"Deal News"}})

    assert list(out["Company Name(s)"]) == ["A", "C"]


def test_export_filter_ands_across_columns():
    from pages.screening import apply_grid_filter_model

    df = pd.DataFrame({
        "Key Developments by Type": ["Deal News", "Deal News", "M&A Closing"],
        "Industry": ["Oil & Gas E&P", "Banks - Regional", "Oil & Gas E&P"],
        "Company Name(s)": ["A", "B", "C"],
    })

    out = apply_grid_filter_model(df, {
        "Key Developments by Type": {"Deal News"},
        "Industry": {"Oil & Gas E&P"},
    })

    assert list(out["Company Name(s)"]) == ["A"]


def test_export_filter_matches_blanks_the_way_the_grid_does():
    """The grid filters on the DISPLAY string and shows empty as '(Blanks)',
    whose model value is the empty string — NaN must match it."""
    from pages.screening import apply_grid_filter_model
    import numpy as np

    df = pd.DataFrame({
        "Industry": ["Oil & Gas E&P", None, np.nan],
        "Company Name(s)": ["A", "B", "C"],
    })

    out = apply_grid_filter_model(df, {"Industry": {""}})

    assert list(out["Company Name(s)"]) == ["B", "C"]


def test_export_filter_is_a_no_op_without_a_model():
    """No filter set → the export still carries the COMPLETE result set."""
    from pages.screening import apply_grid_filter_model

    df = pd.DataFrame({"Key Developments by Type": ["Deal News", "M&A Closing"]})

    assert len(apply_grid_filter_model(df, {})) == 2
    assert len(apply_grid_filter_model(df, None)) == 2


def test_export_filter_ignores_columns_the_export_does_not_have():
    """Hidden identity columns are dropped before the workbook is written; a
    filter naming one must not empty the file."""
    from pages.screening import apply_grid_filter_model

    df = pd.DataFrame({"Key Developments by Type": ["Deal News", "M&A Closing"]})

    out = apply_grid_filter_model(df, {"_event_id": {"1"}})

    assert len(out) == 2


def test_export_filter_applies_to_the_full_set_not_just_the_loaded_page():
    """The point of re-applying the model server-side: the grid holds 500 rows,
    the export holds all 20,753, and the filter must run over the latter."""
    from pages.screening import apply_grid_filter_model

    full = pd.DataFrame({
        "Key Developments by Type": ["Deal News"] * 8000 + ["M&A Closing"] * 12753,
    })

    out = apply_grid_filter_model(full, {"Key Developments by Type": {"Deal News"}})

    assert len(out) == 8000, "export filtered only the visible page"


def test_subtype_filter_domain_covers_every_category_value(monkeypatch):
    """The header filter used to offer only the values on the loaded 500-row page.
    On the M&A all-history screen that was 8 of 10 subtypes — 'M&A Cancellation'
    (508 events) and 'Change in Control' (32) could not be selected at all, and
    once the Excel export honoured the filter those 540 rows silently vanished
    from the download."""
    import data.screening_service as svc

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", lambda *a, **kw: [
        {"event_category": "M&A Activity", "event_subtype": "Deal News"},
        {"event_category": "M&A Activity", "event_subtype": "M&A Cancellation"},
        {"event_category": "M&A Activity", "event_subtype": "Change in Control"},
        {"event_category": "Legal", "event_subtype": "Litigation"},
    ])
    monkeypatch.setattr(svc, "get_keydev_subtypes_by_category",
                        svc.get_keydev_subtypes_by_category.__wrapped__)

    assert svc.keydev_subtype_domain(["M&A Activity"]) == [
        "Change in Control", "Deal News", "M&A Cancellation"]
    assert svc.keydev_subtype_domain(["M&A Activity", "Legal"]) == [
        "Change in Control", "Deal News", "Litigation", "M&A Cancellation"]
    assert svc.keydev_subtype_domain([]) == []


def test_subtype_domain_is_empty_not_partial_when_the_lookup_fails(monkeypatch):
    """A partial domain presented as complete is worse than none — the grid falls
    back to the loaded values, which is the old behaviour, not a wrong one."""
    import data.screening_service as svc

    def boom(*a, **kw):
        raise RuntimeError("read timeout")

    monkeypatch.setattr(svc.db_manager, "execute_query_readonly", boom)
    monkeypatch.setattr(svc, "get_keydev_subtypes_by_category",
                        svc.get_keydev_subtypes_by_category.__wrapped__)

    assert svc.keydev_subtype_domain(["M&A Activity"]) == []


def test_delisted_reused_tickers_take_the_overview_name():
    """A Delisted AV listing row means the ticker was REUSED: the listing feed still
    names the dead former holder (MRNA 'Marina Biotech', delisted 2018; COR
    'CoreSite Realty', 2021) and only the overview knows the current issuer."""
    import data.screening_service as svc

    sql = svc._AV_COMPANY_NAME
    assert "av.listing_status = 'Delisted'" in sql
    then_branch = sql[sql.index("THEN"):sql.index("ELSE")]
    assert then_branch.index("av.overview_name") < then_branch.index("av.company_name")


def test_active_tickers_take_the_listing_name():
    """For a live listing the bulk feed is the fresher of the two — the overview
    snapshot can predate a rename (NEM overview still says 'Newmont Goldcorp',
    renamed away in 2020; FCX 'Freeport-McMoran Copper & Gold', renamed 2014).
    Preferring the overview unconditionally regressed exactly these."""
    import data.screening_service as svc

    else_branch = svc._AV_COMPANY_NAME[svc._AV_COMPANY_NAME.index("ELSE"):]
    assert else_branch.index("av.company_name") < else_branch.index("av.overview_name")


def test_both_name_sources_are_still_used():
    """Each branch falls back to the other column — neither source is ever dropped,
    and the SEC master backs both up."""
    import data.screening_service as svc

    for branch in ("THEN", "ELSE"):
        i = svc._AV_COMPANY_NAME.index(branch)
        j = svc._AV_COMPANY_NAME.index("ELSE") if branch == "THEN" else len(svc._AV_COMPANY_NAME)
        assert "av.company_name" in svc._AV_COMPANY_NAME[i:j]
        assert "av.overview_name" in svc._AV_COMPANY_NAME[i:j]
    assert "sc.name" in svc._KEYDEV_COMPANY_COLS          # SEC master, last resort
    assert "c.name_coresight" in svc._KEYDEV_COMPANY_COLS  # curated, first


def test_placeholder_names_are_stripped_so_the_fallback_applies():
    """Both AV columns carry the literal string 'null' and bare-ticker placeholders
    (overview_name 'ACR-P-D'); those must not win over a real name."""
    import data.screening_service as svc

    assert svc._AV_COMPANY_NAME.count("'null'") == 4      # 2 columns x 2 branches
    assert svc._AV_COMPANY_NAME.count("e.ticker") == 4


def test_key_devs_mode_always_covers_every_event_ticker():
    """Key-dev screening is about the events, and bounding it to the ~464 Coresight
    companies hid 9,741 of the 21,034 M&A Activity events. It is the mode that
    decides, not an opt-in checkbox — an earlier checkbox version silently reset
    itself to ON whenever Screen For changed, because Streamlit garbage-collects a
    widget key on any run where the widget is not drawn."""
    import inspect, pages.screening as page

    src = inspect.getsource(page._all_companies_on)
    assert 'st.session_state.get("scr_screen_for") == "Key Devs"' in src
    assert "checkbox" not in src.lower()
    assert not hasattr(page, "_render_coverage_toggle"), "the opt-in checkbox must be gone"


def test_other_modes_stay_on_the_coresight_master():
    """"Other criteria only on the present companies" — every non-Key-Devs mode
    screens coreiq_companies, unchanged."""
    import inspect, pages.screening as page

    src = inspect.getsource(page._screening_universe)
    assert "get_all_companies_universe()" in src and "get_base_company_universe()" in src
    assert "_all_companies_on()" in src


def test_company_criteria_still_narrow_a_key_devs_screen(monkeypatch):
    """Industry/Geography describe a COMPANY, not an event, and only Coresight rows
    carry those columns — so they pull a widened Key Devs screen back to covered
    companies rather than silently keeping 3,900 nameless tickers."""
    import data.screening_service as svc

    wide = pd.DataFrame([
        {"ticker": "SBUX", "company_name": "Starbucks",
         "sector": "Coffee & Beverage", "country": "United States"},
        {"ticker": "BMY", "company_name": "Bristol-Myers Squibb",
         "sector": "", "country": "USA"},
    ])
    monkeypatch.setattr(svc, "get_all_companies_universe", lambda: wide.copy())

    working, trace = svc.recompute_working_set(
        [{"type": "industry", "industries": ["Coffee & Beverage"]}], all_companies=True)

    assert list(working["ticker"]) == ["SBUX"]
    assert trace[-1]["rows_out"] == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
