"""Store Count by geographic location, and the "available data only" rule.

Company Information -> Additional Data -> Store Count can be narrowed to the
locations a company's 10-K tags its store counts with. Those locations are named
and grouped exactly like Geographical Segments members (geo_label_map.json for
the name, geo_hierarchy.json for Region > Sub-region > Country > State > City),
and grouping never merges, drops or reassigns a count.

Segments and Additional Data list only companies that have the data; every other
statement keeps the whole universe and shows N/A.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

from data import screening_service as service  # noqa: E402
from data.geo_hierarchy import filter_members, path_for_label  # noqa: E402
from data.screening_config import ADDITIONAL_DATA_TYPES, get_metric_labels  # noqa: E402
from data.segment_aliases import canonicalize_geo_label  # noqa: E402


def fact(ticker, member, label, count, as_of, axis="srt:StatementGeographicalAxis"):
    return {"ticker": ticker, "member": member, "dimension_member_label": label,
            "dimension": axis, "full_dimension_label": axis,
            "numeric_value": count, "period_instant": as_of, "period_end": None}


# ── naming ───────────────────────────────────────────────────────────────────

def test_country_member_is_named_by_its_iso_code_not_the_filers_label():
    # COST files these labels; the qname is the identity.
    assert service.store_location_name("country:CA", "Canadian Operations") == "Canada"
    assert service.store_location_name("country:US", "United States Operations") == "United States"
    assert service.store_location_name("country:KR", "KOREA") == canonicalize_geo_label("South Korea")


def test_other_members_use_the_segment_workbook_name():
    assert service.store_location_name("stpr:TX", "Texas") == canonicalize_geo_label("Texas")


def test_a_label_the_workbook_does_not_know_is_kept_as_filed():
    label = "Zq Unmapped Store Territory"
    assert canonicalize_geo_label(label) == label
    assert service.store_location_name("acme:ZqMember", label) == label


# ── resolution: nothing lost, nothing merged ─────────────────────────────────

def test_every_location_survives_and_counts_are_never_summed():
    rows = [
        fact("COST", "country:US", "United States Operations", 552, "2020-08-30"),
        fact("COST", "country:CA", "Canadian Operations", 101, "2020-08-30"),
        fact("COST", "country:FR", "FRANCE", 1, "2020-08-30"),
        fact("ULTA", "country:US", "United States", 1505, "2026-01-31"),
        fact("ULTA", "stpr:CA", "California", 124, "2017-04-29"),
        fact("ULTA", "acme:Odd", "Zq Unmapped Store Territory", 3, "2026-01-31"),
    ]
    out = service.resolve_store_locations(rows)
    assert out["COST"] == {
        "United States": {"count": 552, "as_of": "2020-08-30", "years": {2020: 552}},
        "Canada": {"count": 101, "as_of": "2020-08-30", "years": {2020: 101}},
        "France": {"count": 1, "as_of": "2020-08-30", "years": {2020: 1}},
    }
    # A country and one of its states are both kept, as reported.
    assert out["ULTA"]["United States"]["count"] == 1505
    assert out["ULTA"][canonicalize_geo_label("California")]["count"] == 124
    assert out["ULTA"]["Zq Unmapped Store Territory"]["count"] == 3


def test_same_location_same_date_keeps_the_whole_not_the_slice():
    rows = [fact("COST", "country:US", "United States Operations", 552, "2020-08-30"),
            fact("COST", "country:US", "United States", 40, "2020-08-30")]
    assert service.resolve_store_locations(rows)["COST"]["United States"]["count"] == 552


def test_newest_date_wins_per_location():
    rows = [fact("ULTA", "country:US", "United States", 1505, "2026-01-31"),
            fact("ULTA", "country:US", "United States", 1400, "2024-02-03")]
    assert service.resolve_store_locations(rows)["ULTA"]["United States"] == {
        "count": 1505, "as_of": "2026-01-31", "years": {2026: 1505, 2024: 1400}}


def test_a_comparative_count_lands_in_its_own_fiscal_year():
    # A 10-K repeats last year's count; each date keeps its own year, and a
    # year ending in the first week of January belongs to the year before.
    rows = [fact("CRI", "country:CA", "Canada", 190, "2022-01-01"),
            fact("CRI", "country:CA", "Canada", 185, "2022-12-31"),
            fact("CRI", "country:CA", "Canada", 180, "2021-01-02")]
    assert service.resolve_store_locations(rows)["CRI"]["Canada"]["years"] == {
        2021: 190, 2022: 185, 2020: 180}


def test_non_geographic_axes_are_ignored():
    rows = [fact("MCD", "us-gaap:FranchisedUnitsMember", "Franchised restaurants:", 22570,
                 "2025-12-31", axis="mcd:RestaurantOwnershipTypeAxis")]
    assert service.resolve_store_locations(rows) == {}


# ── grouping matches Geographical Segments ───────────────────────────────────

def test_locations_land_in_the_segment_hierarchy_groups():
    assert path_for_label("Canada")["region"] == "AMER"
    assert path_for_label("France")["region"] == "EMEA"
    names = ["United States", "Canada", "France", canonicalize_geo_label("California")]
    emea = filter_members(names, {"region": ["EMEA"]}, True)
    assert emea == ["France"]
    us = filter_members(names, {"country": ["United States"]}, True)
    assert set(us) >= {"United States", canonicalize_geo_label("California")}
    assert "France" not in us


# ── filtering ────────────────────────────────────────────────────────────────

LOCATIONS = {
    "COST": {"United States": {"count": 552, "as_of": "2020-08-30", "years": {2019: 540, 2020: 552}},
             "France": {"count": 1, "as_of": "2020-08-30", "years": {2020: 1}}},
    "ULTA": {"United States": {"count": 1505, "as_of": "2026-01-31", "years": {2026: 1505}}},
}
HISTORY = {
    "store_counts": {"COST": {2024: 890, 2025: 914}, "ULTA": {2023: 1355}},
    "credit_ratings": {"KSS": {2025: "BBB-"}, "COST": {2025: "AA"}},
}
UNIVERSE = pd.DataFrame({"ticker": ["COST", "ULTA", "WMT"],
                         "company_name": ["Costco", "Ulta", "Walmart"]})


@pytest.fixture
def fake_data(monkeypatch):
    monkeypatch.setattr(service, "get_store_locations", lambda: LOCATIONS)
    monkeypatch.setattr(service, "get_additional_history", lambda: HISTORY)
    monkeypatch.setattr(service, "get_base_company_universe", lambda: UNIVERSE.copy())


def test_location_filter_keeps_only_companies_reporting_it(fake_data):
    crit = {"type": "additional", "data_type": "store_counts",
            "display_col": "Store Count", "locations": ["France"]}
    out, stats = service.apply_additional_criterion(crit, UNIVERSE)
    assert list(out["ticker"]) == ["COST"]
    assert out.loc[0, "Store Count — France"] == 1
    assert out.loc[0, "Store Count As Of"] == "2020-08-30"
    assert crit["location_cols"] == ["Store Count — France"]
    assert stats["with_data"] == 1


def test_group_selection_is_every_location_in_it(fake_data):
    crit = {"type": "additional", "data_type": "store_counts",
            "display_col": "Store Count", "locations": ["United States", "France"]}
    out, _ = service.apply_additional_criterion(crit, UNIVERSE)
    assert sorted(out["ticker"]) == ["COST", "ULTA"]
    assert out.set_index("ticker").loc["ULTA", "Store Count — United States"] == 1505
    assert pd.isna(out.set_index("ticker").loc["ULTA", "Store Count — France"])


def test_pipeline_drops_companies_without_store_location_data(fake_data):
    crit = {"type": "additional", "data_type": "store_counts",
            "display_col": "Store Count", "locations": ["United States"]}
    df, trace = service.recompute_working_set([crit])
    assert sorted(df["ticker"]) == ["COST", "ULTA"]          # WMT: no data → not listed
    assert trace[0]["with_data"] == trace[0]["rows_out"] == 2


def test_segments_show_available_data_only_but_income_statement_keeps_na(monkeypatch, fake_data):
    def partial(column):
        def run(crit, df):
            out = df.copy()
            out[column] = out["ticker"].map({"COST": 10.0})
            return out, {"type": "financial"}
        return run
    monkeypatch.setattr(service, "apply_geographical_segments_statement_criterion",
                        partial("Geo Rev"))
    monkeypatch.setattr(service, "apply_financial_criterion", partial("Revenue"))

    seg = {"type": "financial", "statement": "Geographical Segments", "display_col": "Geo Rev"}
    df, _ = service.recompute_working_set([seg])
    assert list(df["ticker"]) == ["COST"]

    inc = {"type": "financial", "statement": "Income Statement", "display_col": "Revenue"}
    df, _ = service.recompute_working_set([inc])
    assert sorted(df["ticker"]) == ["COST", "ULTA", "WMT"]   # N/A rows stay
    assert df.set_index("ticker")["Revenue"].isna().sum() == 2


def test_additional_checkbox_on_income_statement_does_not_shrink_it():
    assert not service.shows_available_data_only(
        {"type": "additional", "data_type": "store_counts", "hidden": True})
    assert service.shows_available_data_only({"type": "additional", "data_type": "credit_ratings"})
    assert service.shows_available_data_only(
        {"type": "financial", "statement": "Business Segments"})
    assert not service.shows_available_data_only(
        {"type": "financial", "statement": "Key Stats"})


# ── the dropdown ─────────────────────────────────────────────────────────────

def test_additional_data_offers_only_store_count_and_credit_rating():
    assert get_metric_labels("Additional Data") == ["Store Count", "Credit Rating"]
    assert set(ADDITIONAL_DATA_TYPES) == {"Store Count", "Credit Rating"}


def test_location_options_count_companies(fake_data):
    assert service.store_location_options() == [("United States", 2), ("France", 1)]


# ── fiscal years ─────────────────────────────────────────────────────────────

def history_row(id_, ticker, source, value, day, report_year=None):
    from datetime import date
    numeric = float(value) if source == "store_count" else 13.0
    return {"id": id_, "ticker": ticker, "source": source, "value": str(value),
            "numeric_value": numeric, "report_fiscal_year": report_year,
            "period_instant": date.fromisoformat(day) if day else None}


def test_history_years_come_from_the_values_own_date():
    rows = [
        # UAA files its 2020-12-31 count under report year 2021: it is FY 2020.
        history_row(1, "UAA", "store_count", 439, "2020-12-31", 2021),
        # A year ending in the first week of January is the year before.
        history_row(2, "AAP", "store_count", 4806, "2021-01-02", 2021),
        # No date: the filing's year stands.
        history_row(3, "ZZ", "store_count", 12, None, 2019),
    ]
    out = service.resolve_additional_history(rows)["store_counts"]
    assert out == {"UAA": {2020: 439}, "AAP": {2020: 4806}, "ZZ": {2019: 12}}


def test_history_keeps_the_latest_value_rules_within_a_year():
    rows = [
        history_row(1, "AAP", "store_count", 5086, "2022-12-31"),
        history_row(2, "AAP", "store_count", 5107, "2022-12-31"),   # same date: larger stands
        history_row(3, "KSS", "credit_rating", "BBB-", "2025-03-20"),
        history_row(4, "KSS", "credit_rating", "BB", "2025-03-20"),  # same filing: first-filed stands
        history_row(5, "KSS", "credit_rating", "", "2024-03-21"),    # empty rating ignored
    ]
    out = service.resolve_additional_history(rows)
    assert out["store_counts"] == {"AAP": {2022: 5107}}
    assert out["credit_ratings"] == {"KSS": {2025: "BBB-"}}


def test_store_count_year_range_is_one_column_per_year_and_data_only(fake_data):
    crit = {"type": "additional", "data_type": "store_counts", "display_col": "Store Count",
            "year_range": [2023, 2024, 2025]}
    df, trace = service.recompute_working_set([crit])
    assert crit["year_cols"] == ["Store Count [FY 2023]", "Store Count [FY 2024]",
                                 "Store Count [FY 2025]"]
    by = df.set_index("ticker")
    assert sorted(by.index) == ["COST", "ULTA"]                # WMT: no data → not listed
    assert by.loc["COST", "Store Count [FY 2025]"] == 914
    assert pd.isna(by.loc["COST", "Store Count [FY 2023]"])    # shown blank, company stays
    assert trace[0]["rows_out"] == 2


def test_single_year_keeps_only_companies_with_that_year(fake_data):
    crit = {"type": "additional", "data_type": "credit_ratings", "display_col": "Credit Rating",
            "year": 2025}
    df, _ = service.recompute_working_set([crit])
    assert df.set_index("ticker")["Credit Rating [FY 2025]"].to_dict() == {"COST": "AA"}


def test_latest_keeps_the_existing_single_column(monkeypatch, fake_data):
    monkeypatch.setattr(service, "_fetch_additional_tickers", lambda tickers, src: {"COST"})
    monkeypatch.setattr(service, "_fetch_additional_display_values",
                        lambda tickers, dt: {"COST": "939"})
    crit = {"type": "additional", "data_type": "store_counts", "display_col": "Store Count"}
    df, _ = service.recompute_working_set([crit])
    assert df.set_index("ticker")["Store Count"].to_dict() == {"COST": "939"}
    assert "year_cols" not in crit


def test_locations_by_year(fake_data):
    crit = {"type": "additional", "data_type": "store_counts", "display_col": "Store Count",
            "locations": ["United States", "France"], "year_range": [2019, 2020]}
    out, _ = service.apply_additional_criterion(crit, UNIVERSE)
    by = out.set_index("ticker")
    assert crit["location_cols"] == [
        "Store Count — United States [FY 2019]", "Store Count — United States [FY 2020]",
        "Store Count — France [FY 2019]", "Store Count — France [FY 2020]"]
    assert list(by.index) == ["COST"]                           # ULTA has no 2019-2020 count
    assert by.loc["COST", "Store Count — United States [FY 2019]"] == 540
    assert "Store Count As Of" not in out.columns               # the year is in the heading
