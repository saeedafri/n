"""A place is not a line of business.

Filers tag "United States", "EMEA" and "Asia" on business axes. Those are
geography filed in the wrong place, and the Business Segments list must not
offer them. One rule, `names_a_place`, decides it for the Segments tab and the
screener alike, so the two screens cannot drift apart.
"""
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

from data.geo_hierarchy import names_a_place  # noqa: E402


# The seven the business team named, plus the ones that sit with them in the
# live cache. Every one is a place, none is a line of business.
PLACES_FOUND_ON_A_BUSINESS_AXIS = [
    "International", "North America", "Europe", "Americas", "EMEA",
    "United States", "Asia", "Canada", "Latin America", "Asia Pacific",
    "U.S.", "Brazil", "Mexico", "United Kingdom", "APAC", "Japan", "China",
    "Greater China", "Rest of World", "Domestic", "Texas", "West", "East",
]

# Real business segments from the live cache. None of these may be dropped.
REAL_BUSINESS_SEGMENTS = [
    "Retail", "Wholesale", "Product", "Services", "Subscription", "Royalty",
    "Apparel", "Direct-to-Consumer", "Continuing Operations",
    "Discontinued Operations", "Corporate", "Other Products", "Other revenue",
]

# "Other" is in the hierarchy under a region, but as a segment name it is the
# revenue a filer did not break out. 141 companies use it. Reading it as
# geography would empty their business list.
CATCH_ALLS = ["Other", "All Other", "Unallocated", "Other (2)", "Other operations"]


@pytest.mark.parametrize("label", PLACES_FOUND_ON_A_BUSINESS_AXIS)
def test_a_place_is_recognised_as_one(label):
    assert names_a_place(label) is True


@pytest.mark.parametrize("label", REAL_BUSINESS_SEGMENTS)
def test_a_real_business_segment_survives(label):
    assert names_a_place(label) is False


@pytest.mark.parametrize("label", CATCH_ALLS)
def test_a_catch_all_is_not_mistaken_for_a_place(label):
    assert names_a_place(label) is False


def test_blank_and_junk_are_not_places():
    for label in ("", "   ", None):
        assert names_a_place(label) is False






def test_classify_member_sends_a_place_on_a_business_axis_nowhere():
    """The root fix. A fact is dropped from the Business section whatever axis
    carried it, so the screening cache and the Segments tab agree."""
    from data.repository import SegmentDataRepository

    def row(member, axis):
        return {"dimension_member_label": member, "full_dimension_label": axis,
                "dimension_label": ""}

    business_axis = "Business: Retail"      # heading before the colon, as filed
    assert SegmentDataRepository._classify_member(row("United States", business_axis), set()) is None
    assert SegmentDataRepository._classify_member(row("EMEA", business_axis), set()) is None

    kept = SegmentDataRepository._classify_member(row("Retail", business_axis), set())
    assert kept is not None and kept[0] == "business" and kept[1] == "Retail"


def test_a_place_on_the_GEOGRAPHIC_axis_is_untouched():
    """Only the business side changes. Geography keeps working exactly as it did."""
    from data.repository import SegmentDataRepository

    geo_axis = "Geographical: United States"
    section, member = SegmentDataRepository._classify_member(
        {"dimension_member_label": "United States", "full_dimension_label": geo_axis,
         "dimension_label": ""}, set())
    assert section == "geo"
    assert member == "United States"


def test_the_filter_is_fast_enough_for_a_render():
    """The dropdown reads at most 500 labels and reruns on every interaction, so
    this has to be microseconds once warm, not milliseconds."""
    labels = (PLACES_FOUND_ON_A_BUSINESS_AXIS + REAL_BUSINESS_SEGMENTS + CATCH_ALLS) * 12
    [names_a_place(label) for label in labels]          # warm the lookup cache
    started = time.perf_counter()
    for _ in range(20):
        [label for label in labels if not names_a_place(label)]
    per_pass_ms = (time.perf_counter() - started) / 20 * 1000
    assert per_pass_ms < 5, f"{len(labels)} labels took {per_pass_ms:.2f} ms"


# ── the other direction: geography must stay free of businesses ──────────────

# Every member the live geographical cache holds that is NOT a place. Measured,
# not assumed: 115 of its 116 members resolve to a real place, and this is the
# one that does not.
GEO_MEMBERS_THAT_ARE_NOT_PLACES = ["Other"]

# Real business segment names. None may ever reach the geographic list.
BUSINESSES_THAT_MUST_NEVER_BE_GEOGRAPHIC = [
    "Retail", "Wholesale", "Subscription", "Royalty", "Apparel",
    "Direct-to-Consumer", "Continuing Operations", "Footwear",
]


@pytest.mark.parametrize("label", BUSINESSES_THAT_MUST_NEVER_BE_GEOGRAPHIC)
def test_a_business_segment_is_never_read_as_a_place(label):
    """The separation has to hold both ways. If one of these ever resolved to a
    node, it would be dropped from the Business list AND offered as a geography."""
    assert names_a_place(label) is False


def test_the_geographic_list_holds_only_places():
    """'Other' is the single exception, and on the geographic axis it honestly
    means "other geographies" — the filer's catch-all for what it did not name."""
    from data.geo_hierarchy import names_a_place as is_place

    live_geo_members = [
        "United States", "Other", "International", "Asia Pacific (APAC)", "Europe",
        "Europe, Middle East and Africa (EMEA)", "Canada", "United Kingdom",
        "Americas", "China", "Non-US", "North America", "Foreign", "Asia", "Japan",
        "Germany", "Rest of World (RoW)", "Latin America", "Mexico", "Australia",
    ]
    strays = [m for m in live_geo_members
              if not is_place(m) and m not in GEO_MEMBERS_THAT_ARE_NOT_PLACES]
    assert strays == [], f"not places: {strays}"


def test_the_two_lists_cannot_overlap():
    """A label belongs to exactly one side. The only members allowed on both are
    the catch-alls, which name no segment at all."""
    places = set(PLACES_FOUND_ON_A_BUSINESS_AXIS)
    businesses = set(REAL_BUSINESS_SEGMENTS) | set(BUSINESSES_THAT_MUST_NEVER_BE_GEOGRAPHIC)
    assert places & businesses == set()
    for label in places:
        assert names_a_place(label) is True
    for label in businesses:
        assert names_a_place(label) is False


# ── no double counting ───────────────────────────────────────────────────────

def test_a_place_can_reach_only_one_section():
    """The anti-double-count invariant.

    Before this rule a filer reporting "United States" on BOTH a business and a
    geographic axis produced two members — 340 such rows existed across 40
    companies. Now the business copy is dropped, so a place is counted once.
    """
    from data.repository import SegmentDataRepository

    def classify(member, heading):
        return SegmentDataRepository._classify_member(
            {"dimension_member_label": member, "dimension_label": "",
             "full_dimension_label": f"{heading}: {member}"}, set())

    for place in ("United States", "EMEA", "Canada", "Europe", "Asia"):
        sections = {result[0] for result in
                    (classify(place, "Geographical"), classify(place, "Business"))
                    if result}
        assert sections == {"geo"}, f"{place} reached {sections}"


def test_a_business_segment_still_reaches_only_its_own_section():
    """The invariant holds the other way too — nothing was over-corrected."""
    from data.repository import SegmentDataRepository

    result = SegmentDataRepository._classify_member(
        {"dimension_member_label": "Wholesale", "dimension_label": "",
         "full_dimension_label": "Business: Wholesale"}, set())
    assert result == ("business", "Wholesale")


# ── the axis has the final say, not the filer's prose ────────────────────────

# Real headings from staging whose text does NOT contain the word the heading
# classifier looks for, on rows whose axis IS geographic. 108 of 8,914 geo-axis
# rows land this way. Before the axis check they were filed as Business; once
# places started being dropped from Business they would have vanished instead.
GEO_ROWS_WITH_A_MISLEADING_HEADING = [
    ("pf0:StatementGeographicalAxis", "pf0:StatementGeographicalAxis: CHINA", "CHINA"),
    ("pf0:StatementGeographicalAxis", "pf0:StatementGeographicalAxis: CANADA", "CANADA"),
    ("srt:StatementGeographicalAxis", "Geographic Area: Americas", "Americas"),
    ("srt:StatementGeographicalAxis", "Geographic Area: EMEA", "EMEA"),
    ("srt:StatementGeographicalAxis", "Geographic Areas Financial Data: Europe", "Europe"),
    ("srt:StatementGeographicalAxis",
     "Revenue From Customers Based In Different Geographic Regions", "Taiwan"),
    ("srt:StatementGeographicalAxis",
     "Revenue From Customers Based In Different Geographic Regions", "United States"),
]


@pytest.mark.parametrize("dimension, heading, member", GEO_ROWS_WITH_A_MISLEADING_HEADING)
def test_a_geographic_axis_wins_over_the_heading_text(dimension, heading, member):
    from data.repository import SegmentDataRepository

    result = SegmentDataRepository._classify_member(
        {"dimension": dimension, "full_dimension_label": heading,
         "dimension_member_label": member, "dimension_label": ""}, set())
    assert result is not None, f"{member!r} on {dimension!r} was dropped"
    assert result[0] == "geo"


def test_the_axis_check_does_not_rescue_a_business_axis():
    """Only a geographic axis overrides the heading. A place on a business axis
    is still geography filed in the wrong place, and still goes."""
    from data.repository import SegmentDataRepository

    assert SegmentDataRepository._classify_member(
        {"dimension": "us-gaap:StatementBusinessSegmentsAxis",
         "full_dimension_label": "Geographic Area: United States",
         "dimension_member_label": "United States", "dimension_label": ""}, set()) is None


# ── the inner axis of a multi-dimensional fact ───────────────────────────────

# J&J's 10-K files its regions as a ConsolidationItems wrapper whose SECOND axis
# is the geographic one; the geographic copy exists only in its 10-Qs, which the
# screening cache never reads. `dimension` names the wrapper's axis, so neither
# _is_geographic_axis nor geo_member_set could see the geography, the heading
# before the first colon reads "Consolidation Items", and the compound names are
# not in the workbook so names_a_place could not drop them either. All three
# missed and "Asia-Pacific, Africa" showed up in the Business Segments dropdown.
WRAPPER_ROWS_WHOSE_INNER_AXIS_IS_GEOGRAPHIC = [
    ("Consolidation Items: Operating Segments, Geographical: Asia-Pacific, Africa",
     "Asia-Pacific, Africa"),
    ("Consolidation Items: Operating Segments, Statement, Geographical: Asia-Pacific, Africa",
     "Asia-Pacific, Africa"),
    ("Consolidation Items: Operating Segments, Geographical: Europe", "Europe"),
    ("Consolidation Items: Operating Segments, Statement, Geographical: "
     "Western Hemisphere excluding U.S.", "Western Hemisphere excluding U.S."),
]

# The same wrapper shape, but the inner axis is the filer's own segment axis.
# PepsiCo's reportable segments and Mondelez's AMEA are named after places but
# declared as business segments, and they must stay in the Business list.
WRAPPER_ROWS_WHOSE_INNER_AXIS_IS_BUSINESS = [
    ("Consolidation Items: Operating Segments, Segments: Asia Pacific Foods (Segment)",
     "Asia Pacific Foods (Segment)"),
    ("Consolidation Items: Operating Segments, Segments: "
     "Asia Pacific, Australia and New Zealand, and China Region",
     "Asia Pacific, Australia and New Zealand, and China Region"),
    ("Consolidation Items: Operating Segments, Segments: AMEA", "AMEA"),
    ("Consolidation Items: Operating Segments, Segments: Data Center", "Data Center"),
]

# ConsolidationItems is a genuine segment axis, so a place recovered from it is
# kept as a segment (the McDonald's rule) unless the inner heading says geography.
SEGMENT_AXES = frozenset({"srt:ConsolidationItemsAxis", "us-gaap:ConsolidationItemsAxis"})


def _wrapper_row(full_dimension_label, inner):
    return {"dimension": "srt:ConsolidationItemsAxis",
            "dimension_member_label": "Operating Segments",
            "dimension_label": inner,
            "full_dimension_label": full_dimension_label}


@pytest.mark.parametrize("fdl, inner", WRAPPER_ROWS_WHOSE_INNER_AXIS_IS_GEOGRAPHIC)
def test_a_wrappers_inner_geographic_axis_routes_to_geo(fdl, inner):
    from data.repository import SegmentDataRepository

    result = SegmentDataRepository._classify_member(
        _wrapper_row(fdl, inner), set(), SEGMENT_AXES)
    assert result is not None, f"{inner!r} was dropped entirely"
    assert result[0] == "geo", f"{inner!r} landed in {result[0]}"


@pytest.mark.parametrize("fdl, inner", WRAPPER_ROWS_WHOSE_INNER_AXIS_IS_BUSINESS)
def test_a_wrappers_inner_segment_axis_stays_in_business(fdl, inner):
    """A geographically-named segment the filer declares on its segment axis is
    still a segment. Moving these would delete PepsiCo's real reportable segments."""
    from data.repository import SegmentDataRepository

    result = SegmentDataRepository._classify_member(
        _wrapper_row(fdl, inner), set(), SEGMENT_AXES)
    assert result is not None, f"{inner!r} was dropped entirely"
    assert result[0] == "business", f"{inner!r} landed in {result[0]}"


def test_the_inner_heading_is_read_from_the_right_axis():
    """The heading belongs to the member that follows it, not to any other part
    of the label — otherwise a fact with one geographic and one business axis
    would be read as geography whichever member it carries."""
    from data.repository import SegmentDataRepository as repo

    fdl = "Geographical: Europe, Segments: Beverages"
    assert repo._inner_heading_is_geographic({"full_dimension_label": fdl}, "Europe") is True
    assert repo._inner_heading_is_geographic({"full_dimension_label": fdl}, "Beverages") is False
    assert repo._inner_heading_is_geographic({"full_dimension_label": ""}, "Europe") is False
    assert repo._inner_heading_is_geographic({}, "") is False


def test_a_row_with_no_axis_is_not_treated_as_geographic():
    from data.repository import SegmentDataRepository

    assert SegmentDataRepository._is_geographic_axis({"dimension": ""}) is False
    assert SegmentDataRepository._is_geographic_axis({}) is False


def test_every_geographic_axis_is_recognised():
    """The probe checked one axis against live data (2,992 rows, all recognised).
    This covers the whole list, so adding a geographic axis to SEGMENT_GEO_AXES
    without teaching _is_geographic_axis about it fails here instead of silently
    dropping that axis's countries."""
    from data.repository import SegmentDataRepository
    from utils.constants import SEGMENT_GEO_AXES

    for pattern in SEGMENT_GEO_AXES:
        axis = pattern.strip("%")
        for spelling in (axis, f"srt:{axis}", f"us-gaap:{axis}", f"pf0:{axis}",
                         axis.lower(), axis.upper()):
            assert SegmentDataRepository._is_geographic_axis({"dimension": spelling}), spelling


def test_the_new_rule_cannot_fire_on_a_geographic_axis():
    """Why the probe found zero regressions, stated as a test: on a geographic
    axis the section is 'geo' before the business branch is reached, so a place
    there can never be dropped — whatever the heading prose says."""
    from data.repository import SegmentDataRepository
    from utils.constants import SEGMENT_GEO_AXES

    for pattern in SEGMENT_GEO_AXES:
        for heading in ("Geographical", "Geographic Area", "Pf0",
                        "Revenue From Customers Based In Different Geographic Regions"):
            result = SegmentDataRepository._classify_member(
                {"dimension": f"srt:{pattern.strip('%')}",
                 "full_dimension_label": f"{heading}: United States",
                 "dimension_member_label": "United States",
                 "dimension_label": ""}, set())
            assert result == ("geo", "United States"), (pattern, heading, result)
