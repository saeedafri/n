"""Business Segments in Screening follow the business team's workbook.

Business-Segment-Final-Mapping-STG.xlsx (via app/data/business_segment_map.json)
rules every business label: keep it (merged into its Segment Member), remove it,
or move it to Geographical. These pin the workbook's own rows and the gate that
no label it removed or moved can reach the business index.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from data.business_segment_map import (  # noqa: E402
    FILTERS, _map, business_verdict, filter_members, filter_options,
    is_reviewed, member_tags, split_business_members,
)


@pytest.mark.parametrize("label, verdict", [
    # spellings merge into one member (FLWS)
    ("Gourmet Food and Gift Baskets", ("keep", "Gourmet Foods & Gift Baskets")),
    ("BloonNet Wire Service", ("keep", "BloomNet Wire Service")),
    ("Other revenue", ("keep", "Other")),
    # not segments
    ("Corporate", ("remove", "")),
    ("Reportable", ("remove", "")),
    ("Eliminations and Other", ("remove", "")),
    ("Continuing Operations", ("remove", "")),
    # places
    ("U.S.", ("geo", "United States")),
    ("North America", ("geo", "North America")),
    # the workbook keeps these though the place test says otherwise
    ("Turkey (poultry)", ("keep", "Turkey (poultry)")),
    ("International Franchise", ("keep", "International Franchise")),
    ("Other", ("keep", "Other")),
    # a geo placeholder note is not a name: the label keeps its own
    ("Asia Middle East Africa", ("geo", "Asia Middle East Africa")),
])
def test_workbook_rows(label, verdict):
    assert business_verdict(label) == verdict


def test_case_and_spacing_do_not_matter():
    assert business_verdict("  gourmet food AND gift   baskets ") == business_verdict(
        "Gourmet Food and Gift Baskets")


def test_footnote_variants_the_workbook_rules_differently_stay_apart():
    """"Beverage Member" is kept, "Beverage Member (2)" removed: a loose key that
    drops footnote markers must not let one ruling overwrite the other."""
    assert business_verdict("Beverage Member") == ("keep", "Beverages")
    assert business_verdict("Beverage Member (2)") == ("remove", "")


def test_every_member_is_its_own_verdict():
    """A rebuilt cache holds member names and is mapped again on read."""
    members = {v[1] for v in _map()["exact"].values() if v[0] == "keep"}
    assert len(members) > 3000
    assert [m for m in members if business_verdict(m) != ("keep", m)] == []


def test_a_label_the_workbook_has_not_seen_stays_in_business_untagged():
    label = "Quantum Pet Robotics"
    assert not is_reviewed(label)
    assert business_verdict(label) == ("keep", label)
    assert member_tags(label) == {}


def test_an_unreviewed_approved_geo_spelling_moves_to_geography():
    """The workbook's own rule: "Exact match to an approved Geo mapping spelling"."""
    assert business_verdict("Europe, Middle East and Africa (EMEA)")[0] == "geo"


def test_split_merges_spellings_and_routes_the_rest():
    business, places = split_business_members({
        "Other": {2022: 684.0}, "Other revenue": {2022: 1394.0},      # Target: two lines
        "Gourmet Food and Gift Baskets": {2019: 670.0},
        "Gourmet Foods & Gift Baskets": {2019: 670.0, 2024: 768.5},  # same fact, two spellings
        "Corporate": {2022: 1.0},
        "U.S.": {2022: 5.0},
        "Retail": {2022: None},
    })
    assert business == {"Other": {2022: 2078.0},
                         "Gourmet Foods & Gift Baskets": {2019: 670.0, 2024: 768.5}}
    assert places == {"United States": {2022: 5.0}}


def test_no_removed_or_moved_label_reaches_the_business_index():
    """Gate over every workbook ruling, through the real cache writer."""
    from data.screening_service import _segment_cache_entries

    exact = _map()["exact"]
    biz = {"Revenues": {key: {2024: float(i + 1)} for i, key in enumerate(exact)}}
    business = {m for _t, kind, _m, m, _y, _v in _segment_cache_entries("T", biz, {})
                if kind == "business"}
    allowed = {v[1] for v in exact.values() if v[0] == "keep"}
    assert business == allowed


def test_filters_cascade_and_combine():
    members = ["Pet Food", "Pet Supplies", "Other", "Retail", "Quantum Pet Robotics"]
    assert all(member_tags(m) for m in ("Pet Food", "Retail"))
    # Any everywhere: nothing is hidden, untagged members included
    assert filter_members(members, {}) == members
    pet = filter_members(members, {"Segment": ["Pet"]})
    assert "Pet Food" in pet and "Other" not in pet and "Quantum Pet Robotics" not in pet
    # Product only offers what the picked category reaches
    products = filter_options(members, "Product", {"Segment": ["Pet"]})
    assert products and all(p.startswith("Pet") for p in products)
    # AND across filters
    both = filter_members(members, {"Sales Channel": ["Retail & Stores"],
                                    "Segment": ["Pet"]})
    assert "Retail" not in both


def test_filter_names_match_the_workbook_columns():
    assert FILTERS == ("Sales Channel", "Revenue Type", "Segment", "Product")
    used = {name for tags in _map()["tags"].values() for name in tags}
    assert used == set(FILTERS)


def test_dropdown_options_follow_the_workbook_on_a_stale_cache():
    from data.screening_service import _business_workbook_options

    raw = [("Corporate", 40), ("Retail", 38), ("U.S.", 9),
           ("Gourmet Food and Gift Baskets", 1), ("Gourmet Foods & Gift Baskets", 1)]
    assert _business_workbook_options(raw) == [("Retail", 38), ("Gourmet Foods & Gift Baskets", 2)]


def test_a_new_workbook_rebuilds_the_screening_cache():
    import inspect
    from data import screening_service as svc

    source = inspect.getsource(svc._segment_classifier_version)
    assert "business_segment_map" in source and "MAP_PATH" in source


@pytest.mark.parametrize("label", ["Turkey", "International Franchise", "U.S. - Federal", "Global"])
def test_the_classifier_keeps_what_the_workbook_keeps(label):
    """The place rules dropped or renamed these before screening ever saw them:
    "Turkey" read as Türkiye, "International Franchise" as "International"."""
    from data.repository import SegmentDataRepository as Segments

    row = {"dimension": "us-gaap:ProductOrServiceAxis", "dimension_member_label": label,
           "full_dimension_label": f"Product and Service: {label}"}
    assert Segments._classify_member(row, set()) == ("business", label)


def test_filters_narrow_each_other_whichever_is_set_first():
    """Filters 1-3 are independent in the workbook, so picking Sales Channel after
    Segment must only offer channels some pet segment carries — an
    ordered cascade let it offer every channel and then drop the Pet pick."""
    members = sorted({v[1] for v in _map()["exact"].values() if v[0] == "keep"})
    assert filter_options(members, "Segment", {"Product": ["Pet Food & Treats"]}) == ["Pet"]
    channels = filter_options(members, "Sales Channel", {"Segment": ["Pet"]})
    assert channels and all(
        filter_members(members, {"Sales Channel": [c], "Segment": ["Pet"]}) for c in channels)
    assert len(channels) < len(filter_options(members, "Sales Channel"))


def test_picking_a_member_finds_every_spelling_it_merges():
    """Colgate files "Pet Nutrition"; the member is "Pet Nutrition (Hill's)". A
    cache not rebuilt yet still holds the filed spelling, so the query must too."""
    from data.business_segment_map import member_spellings

    assert "pet nutrition" in member_spellings("Pet Nutrition (Hill's)")
    assert {"gourmet food and gift baskets", "gourmet foods & gift baskets"} <= set(
        member_spellings("Gourmet Foods & Gift Baskets"))
    assert member_spellings("Quantum Pet Robotics") == ["quantum pet robotics"]


def _screen(monkeypatch, criterion, rows):
    import pandas as pd
    from data import screening_service as svc

    monkeypatch.setattr(svc, "_segment_values_cache_scope_status", lambda *a, **k: {"available": True})
    monkeypatch.setattr(svc, "read_segment_values_cache", lambda *a, **k: rows)
    base = {"metric_info": {"metric_key": "Revenues", "label": "Revenues", "unit": "$mm"},
            "operator": "Greater Than", "value1": 0, "year": "Latest", "display_col": "Biz"}
    out, _stats = svc.apply_business_segments_statement_criterion(
        {**base, **criterion}, pd.DataFrame({"ticker": ["DPZ", "CL", "AMZN"]}))
    return {r.ticker: r["Biz__raw"] for _, r in out.iterrows()}


ROWS = [
    {"ticker": "DPZ", "member_label": "International Franchise", "value_mm": 338.7, "report_fiscal_year": 2025},
    {"ticker": "DPZ", "member_label": "Supply chain", "value_mm": 2989.5, "report_fiscal_year": 2025},
    {"ticker": "CL", "member_label": "Pet Nutrition (Hill's)", "value_mm": 4613.0, "report_fiscal_year": 2025},
    {"ticker": "AMZN", "member_label": "Aws", "value_mm": 128725.0, "report_fiscal_year": 2025},
]


def test_saved_filters_decide_the_screen(monkeypatch):
    """The criterion keeps the filters, not a frozen member list: Sales Channel =
    Franchise keeps DPZ's franchise segment only and drops the companies with none."""
    got = _screen(monkeypatch, {"segment_filters": {"Sales Channel": ["Franchise"]}}, ROWS)
    assert got == {"DPZ": {"International Franchise": 338.7}}


def test_a_filter_added_on_edit_narrows_the_screen(monkeypatch):
    """Edit used to restore a pre-expanded member list, so a filter added there
    changed nothing. Filters AND together at screen time."""
    got = _screen(monkeypatch, {"segment_filters": {"Segment": ["Pet"]}}, ROWS)
    assert got == {"CL": {"Pet Nutrition (Hill's)": 4613.0}}
    got = _screen(monkeypatch, {"segment_filters": {"Segment": ["Pet"], "Sales Channel": ["Franchise"]}}, ROWS)
    assert got == {}


def test_no_filters_keeps_every_segment(monkeypatch):
    got = _screen(monkeypatch, {}, ROWS)
    assert set(got) == {"DPZ", "CL", "AMZN"} and len(got["DPZ"]) == 2


def _pivot_frame():
    import pandas as pd
    cols = ["Revenues ($mm) — FY 2022–2024 [FY 2022]", "Revenues ($mm) — FY 2022–2024 [FY 2023]",
            "Revenues ($mm) — FY 2022–2024 [FY 2024]"]
    df = pd.DataFrame({"ticker": ["ACI", "AMZN"], "company_name": ["Albertsons", "Amazon"],
                       "exchange": ["NYSE", "NASDAQ"]})
    raws = {"ACI": [{"United States": 71887.0}, {"United States": 77649.7}, {"United States": 79237.7}],
            "AMZN": [{"Germany": 33598.0, "Japan": 24396.0}, {"Germany": 37588.0, "Japan": 26002.0},
                     {"Germany": 40856.0}]}
    for i, col in enumerate(cols):
        df[f"{col}__raw"] = df["ticker"].map(lambda t, i=i: raws[t][i])
    crit = {"display_col": "Revenues ($mm) — FY 2022–2024", "year_range": [2022, 2023, 2024],
            "year_cols": cols, "metric_info": {"label": "Revenues"}, "segment_type": "geographical"}
    return df, crit


def test_year_range_is_one_row_per_segment_with_a_column_per_year():
    """User, 19 Nov: a year range listed every year as its own row; the years
    must be side-by-side columns."""
    from pages.screening import _build_segment_pivot

    df, crit = _pivot_frame()
    pivot, val_cols = _build_segment_pivot(df, [crit], "geographical")
    assert val_cols == ["Geographic Revenues FY 2022 ($mm)", "Geographic Revenues FY 2023 ($mm)",
                        "Geographic Revenues FY 2024 ($mm)"]
    assert len(pivot) == 3                                     # ACI US, AMZN Germany, AMZN Japan
    rows = {r["Geographic Segment"]: r for _, r in pivot.iterrows()}
    assert [rows["United States"][c] for c in val_cols] == [71887.0, 77649.7, 79237.7]
    assert rows["Japan"]["Geographic Revenues FY 2024 ($mm)"] != rows["Japan"]["Geographic Revenues FY 2024 ($mm)"]  # NaN: not filed
    assert set(pivot["Fiscal Year"]) == {"FY 2022–2024"}


def test_single_year_keeps_one_value_column():
    import pandas as pd
    from pages.screening import _build_segment_pivot

    df = pd.DataFrame({"ticker": ["ACI"], "company_name": ["Albertsons"], "exchange": ["NYSE"],
                       "Biz__raw": [{"Fresh": 25442.7, "Pharmacy": 4000.0}], "Biz__year": ["2024"]})
    pivot, val_cols = _build_segment_pivot(
        df, [{"display_col": "Biz", "year": "2024", "metric_info": {"label": "Revenues"}}], "business")
    assert val_cols == ["Business Revenues ($mm)"]
    assert list(pivot["Fiscal Year"]) == ["2024", "2024"]
    assert {"Sales Channel", "Revenue Type", "Segment", "Product"} <= set(pivot.columns)


def _geo_screen(monkeypatch, criterion, rows):
    import pandas as pd
    from data import screening_service as svc

    monkeypatch.setattr(svc, "_segment_values_cache_scope_status", lambda *a, **k: {"available": True})
    monkeypatch.setattr(svc, "read_segment_values_cache", lambda *a, **k: rows)
    monkeypatch.setattr(svc, "read_segment_member_labels_raw", lambda *a, **k: [])
    base = {"metric_info": {"metric_key": "Revenues", "label": "Revenues", "unit": "$mm"},
            "operator": "Greater Than", "value1": 0, "year": "Latest", "display_col": "Geo"}
    out, _stats = svc.apply_geographical_segments_statement_criterion(
        {**base, **criterion}, pd.DataFrame({"ticker": ["AMZN", "ANF", "ACI"]}))
    return {r.ticker: r["Geo__raw"] for _, r in out.iterrows()}


GEO_ROWS = [
    {"ticker": "AMZN", "member_label": "Germany", "value_mm": 45900.0, "report_fiscal_year": 2025},
    {"ticker": "AMZN", "member_label": "United States", "value_mm": 489657.0, "report_fiscal_year": 2025},
    {"ticker": "AMZN", "member_label": "Japan", "value_mm": 30688.0, "report_fiscal_year": 2025},
    {"ticker": "ANF", "member_label": "Europe, Middle East and Africa (EMEA)", "value_mm": 818.1, "report_fiscal_year": 2025},
    {"ticker": "ANF", "member_label": "Americas", "value_mm": 4000.0, "report_fiscal_year": 2025},
    {"ticker": "ACI", "member_label": "United States", "value_mm": 83172.5, "report_fiscal_year": 2025},
]


def test_a_country_pick_filters_the_geographic_screen(monkeypatch):
    """User, 19 Nov: Country picked, results not filtered. The pick only narrowed
    the member list; with no member picked the screen took every segment."""
    got = _geo_screen(monkeypatch, {"segment_filters": {"country": ["Germany"]}}, GEO_ROWS)
    assert got == {"AMZN": {"Germany": 45900.0}}     # Germany only — not EMEA above it


def test_a_region_pick_filters_the_geographic_screen(monkeypatch):
    got = _geo_screen(monkeypatch, {"segment_filters": {"region": ["AMER"]}}, GEO_ROWS)
    assert got == {"AMZN": {"United States": 489657.0}, "ANF": {"Americas": 4000.0},
                   "ACI": {"United States": 83172.5}}     # the region and everything below it


def test_broader_means_a_place_that_contains_the_pick():
    from data.geo_hierarchy import filter_members

    members = ["Germany", "Europe", "EMEA", "Americas", "North America", "Asia Pacific (APAC)",
               "Rest of World (RoW)", "Worldwide", "United States", "U.K. and Ireland",
               "Western Europe", "Middle East and Africa"]
    # Sharing a region is not containing: U.K. and Ireland, Western Europe and
    # Middle East and Africa are beside Germany in the hierarchy, not above it.
    assert filter_members(members, {"country": ["Germany"]}) == ["Germany", "Europe", "EMEA"]
    assert filter_members(members, {"country": ["United States"]}) == ["Americas", "North America", "United States"]


@pytest.mark.parametrize("pick, want", [
    ({"country": ["United Kingdom"]}, ["United Kingdom"]),
    ({"country": ["United States"]}, ["United States", "West", "California", "Texas"]),
    ({"country": ["United States"], "state": ["Texas"]}, ["Texas"]),
    ({"sub_region": ["Europe"]}, ["Europe", "United Kingdom", "Western Europe"]),
    ({"region": ["EMEA"]}, ["Europe, Middle East and Africa (EMEA)", "Europe", "United Kingdom",
                            "EMEA Region", "Western Europe", "Middle East and Africa"]),
])
def test_a_geographic_pick_filters_downward(pick, want):
    """User, 19 Nov: filters work downward — a pick is that place and everything
    below it (United States with Texas, California, West), never EMEA, Europe or
    North America above it."""
    from data.geo_hierarchy import filter_members

    members = ["Europe, Middle East and Africa (EMEA)", "Europe", "United Kingdom",
               "EMEA Region", "United States", "Americas", "North America", "West", "California",
               "Texas", "Western Europe", "Middle East and Africa"]
    assert filter_members(members, pick, include_broader=False) == want


def test_a_town_named_business_segment_is_not_moved_to_geography():
    """Axe Compute (US) files segments "Birmingham" and "Pittsburgh"; the place rule
    moved Birmingham to Geographical as Birmingham, UK. Towns stay in Business."""
    assert business_verdict("Birmingham") == ("keep", "Birmingham")
    assert business_verdict("Pittsburgh") == ("keep", "Pittsburgh")
    assert business_verdict("Europe, Middle East and Africa (EMEA)")[0] == "geo"


@pytest.mark.parametrize("member, tags", [
    ("Non-apparel", {"Segment": "Apparel, Footwear & Accessories"}),          # not Apparel (general)
    ("Non-Foods", {}),                                                        # not Food & Grocery
    ("Non-marketplace", {}),
    ("Non-furniture", {"Segment": "Home & Hardlines"}),
    ("Non-Automotive Investments", {"Revenue Type": "Financial Services"}),
    # a negated word that is not the tag stays tagged
    ("Nonalcoholic beverage", {"Segment": "Beverages, Alcohol & Tobacco", "Product": "Beverages (general)"}),
    ("Non-perishables", {"Segment": "Food & Grocery", "Product": "Pantry & Shelf-stable"}),
    ("Apparel", {"Segment": "Apparel, Footwear & Accessories", "Product": "Apparel (general)"}),
])
def test_a_tag_never_names_what_the_label_says_it_is_not(member, tags):
    """User, 19 Nov: Torrid's "Non-apparel" was tagged Product = Apparel (general) —
    the workbook tagged by keyword and kept the word the label negates."""
    assert member_tags(member) == tags
