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
    pet = filter_members(members, {"Product Category": ["Pet"]})
    assert "Pet Food" in pet and "Other" not in pet and "Quantum Pet Robotics" not in pet
    # Product only offers what the picked category reaches
    products = filter_options(members, "Product", {"Product Category": ["Pet"]})
    assert products and all(p.startswith("Pet") for p in products)
    # AND across filters
    both = filter_members(members, {"Sales Channel": ["Retail & Stores"],
                                    "Product Category": ["Pet"]})
    assert "Retail" not in both


def test_filter_names_match_the_workbook_columns():
    assert FILTERS == ("Sales Channel", "Revenue Type", "Product Category", "Product")
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
    Product Category must only offer channels some pet segment carries — an
    ordered cascade let it offer every channel and then drop the Pet pick."""
    members = sorted({v[1] for v in _map()["exact"].values() if v[0] == "keep"})
    assert filter_options(members, "Product Category", {"Product": ["Pet Food & Treats"]}) == ["Pet"]
    channels = filter_options(members, "Sales Channel", {"Product Category": ["Pet"]})
    assert channels and all(
        filter_members(members, {"Sales Channel": [c], "Product Category": ["Pet"]}) for c in channels)
    assert len(channels) < len(filter_options(members, "Sales Channel"))


def test_picking_a_member_finds_every_spelling_it_merges():
    """Colgate files "Pet Nutrition"; the member is "Pet Nutrition (Hill's)". A
    cache not rebuilt yet still holds the filed spelling, so the query must too."""
    from data.business_segment_map import member_spellings

    assert "pet nutrition" in member_spellings("Pet Nutrition (Hill's)")
    assert {"gourmet food and gift baskets", "gourmet foods & gift baskets"} <= set(
        member_spellings("Gourmet Foods & Gift Baskets"))
    assert member_spellings("Quantum Pet Robotics") == ["quantum pet robotics"]
