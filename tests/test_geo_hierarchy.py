"""The geographic hierarchy behind the screener's cascading segment filters.

The promise these tests defend is that a filter narrows what you see and never
loses a company: every segment name the screener can show has a place in the
hierarchy, every level offers only values that lead somewhere, and clearing the
filters gives back exactly what you started with.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "scripts"))

from data.geo_hierarchy import (  # noqa: E402
    LEVELS,
    LEVEL_TITLES,
    UNCLASSIFIED,
    describe_label,
    filter_members,
    is_excluded,
    level_options,
    node_for_label,
    path_for_label,
    unplaced_members,
)

HIERARCHY = json.loads((REPO / "app" / "data" / "geo_hierarchy.json").read_text())

# The names the screener showed on the last cache rebuild. Kept literal so a
# regression in the label layer or the workbook shows up as a test failure
# rather than as an empty dropdown in front of a user.
LIVE_MEMBERS = [
    "Africa", "Africa/Eurasia", "Americas", "Argentina", "Asia",
    "Asia Pacific (APAC)", "Asia Pacific & Latin America (APLA)",
    "Asia, Middle East & Africa (AMEA)", "Australia", "Belgium", "Brazil",
    "CEE (Central & Eastern Europe)", "California", "Canada", "Central",
    "China", "Colombia", "Commonwealth of Independent States (CIS)",
    "Developed Markets", "Domestic", "EMEA And Asia Pacific", "East",
    "Eastern Europe", "Egypt", "Emerging Markets", "Europe",
    "Europe, Asia & Middle East (EAME)", "Europe, Middle East and Africa (EMEA)",
    "Foreign", "France", "Germany", "Germany and Italy",
    "Germany, Italy, Japan, and Australia", "Germany, Italy, and Japan",
    "Greater China", "Hong Kong", "Hungary", "India", "Indonesia",
    "International", "Ireland", "Italy", "Japan", "Kazakhstan",
    "Latin America", "Latin America & Asia Pacific (LAAP)",
    "Latin America and the Caribbean", "Luxembourg", "Malaysia", "Mexico",
    "Mid East", "Mid-Atlantic", "Middle East", "Middle East and Africa",
    "Midwest", "Mountain", "Netherlands", "New Zealand", "Non-US", "North",
    "North America", "North East", "Northwest", "Norway", "Other", "Pacific",
    "Philippines", "Rest of World (RoW)", "Russia", "SSEA, CIS & MEA",
    "Saudi Arabia", "Singapore", "South", "South Africa", "South America",
    "South Central", "South East", "South Korea", "Southeast Asia",
    "Southwest", "Spain", "Sweden", "Switzerland", "Taiwan", "Thailand",
    "Ukraine", "United Arab Emirates", "United Kingdom", "United States",
    "Venezuela", "Vietnam", "West", "West Coast", "Western Europe",
]


# ── every option has a home ──────────────────────────────────────────────

def test_every_live_member_has_a_node():
    assert unplaced_members(LIVE_MEMBERS) == []


def test_generated_file_matches_the_workbook():
    """Catches a hand-edited JSON or a workbook nobody regenerated from."""
    from build_geo_hierarchy import build, WORKBOOK

    if not WORKBOOK.exists():                      # pragma: no cover
        pytest.skip("workbook not present in this checkout")
    rebuilt = build(WORKBOOK)
    assert rebuilt["nodes"] == HIERARCHY["nodes"]
    assert rebuilt["exact"] == HIERARCHY["exact"]
    assert rebuilt["tree"] == HIERARCHY["tree"]


def test_label_map_only_points_at_defined_nodes():
    for node_name in set(HIERARCHY["exact"].values()):
        assert node_name in HIERARCHY["nodes"], node_name


# ── the cascade ──────────────────────────────────────────────────────────

def test_regions_cover_the_live_members():
    assert level_options(LIVE_MEMBERS, "region") == [
        "AMER", "APAC", "EMEA", "Market grouping", "Multi-region", "Non-regional",
    ]


def test_sub_regions_narrow_to_the_chosen_region():
    assert level_options(LIVE_MEMBERS, "sub_region", {"region": ["AMER"]}) == [
        "Latin America (LATAM)", "North America (NORAM)",
    ]


def test_countries_narrow_to_the_chosen_sub_region():
    countries = level_options(
        LIVE_MEMBERS, "country",
        {"region": ["AMER"], "sub_region": ["North America (NORAM)"]},
    )
    assert countries == ["Canada", "United States"]


def test_states_narrow_to_the_chosen_country():
    states = level_options(
        LIVE_MEMBERS, "state",
        {"region": ["AMER"], "sub_region": ["North America (NORAM)"],
         "country": ["United States"]},
    )
    assert states == ["California"]


def test_a_level_only_offers_values_that_lead_somewhere():
    """No dropdown entry may come back with zero members."""
    for region in level_options(LIVE_MEMBERS, "region"):
        for sub in level_options(LIVE_MEMBERS, "sub_region", {"region": [region]}):
            picked = {"region": [region], "sub_region": [sub]}
            assert filter_members(LIVE_MEMBERS, picked, include_broader=False)


def test_deeper_levels_are_empty_when_nothing_reaches_them():
    assert level_options(LIVE_MEMBERS, "city") == []


# ── nothing is ever lost ─────────────────────────────────────────────────

def test_no_selection_returns_every_member():
    assert filter_members(LIVE_MEMBERS, {}) == LIVE_MEMBERS
    assert filter_members(LIVE_MEMBERS, {level: [] for level in LEVELS}) == LIVE_MEMBERS


def test_broader_members_are_kept_by_default():
    """A filer reporting only 'Americas' survives a filter on United States."""
    kept = filter_members(
        LIVE_MEMBERS,
        {"region": ["AMER"], "sub_region": ["North America (NORAM)"],
         "country": ["United States"]},
    )
    assert "Americas" in kept
    assert "United States" in kept
    assert "Mountain" in kept          # a US region sits inside the country


def test_strict_mode_drops_only_the_broader_ones():
    picked = {"region": ["AMER"], "sub_region": ["North America (NORAM)"],
              "country": ["United States"]}
    strict = filter_members(LIVE_MEMBERS, picked, include_broader=False)
    assert "United States" in strict
    assert "Mountain" in strict
    assert "Americas" not in strict
    assert set(strict) <= set(filter_members(LIVE_MEMBERS, picked))


def test_filtering_never_invents_a_member():
    picked = {"region": ["EMEA"]}
    assert set(filter_members(LIVE_MEMBERS, picked)) <= set(LIVE_MEMBERS)


def test_order_is_preserved():
    kept = filter_members(LIVE_MEMBERS, {"region": ["APAC"]})
    assert kept == [m for m in LIVE_MEMBERS if m in set(kept)]


def test_an_unknown_label_is_reachable_not_dropped():
    """Tomorrow's label has no node yet — it must still be selectable."""
    members = LIVE_MEMBERS + ["Nordic Cluster Zeta"]
    assert unplaced_members(members) == ["Nordic Cluster Zeta"]
    assert UNCLASSIFIED in level_options(members, "region")
    assert filter_members(members, {}) == members
    assert filter_members(members, {"region": [UNCLASSIFIED]}) == ["Nordic Cluster Zeta"]
    assert "Nordic Cluster Zeta" not in filter_members(members, {"region": ["EMEA"]})


# ── placement of the awkward cases ───────────────────────────────────────

@pytest.mark.parametrize("label, expected", [
    # The member's own name is trimmed off the end of its own trail.
    ("United States", "AMER > North America (NORAM)"),
    ("Mid-Atlantic", "AMER > North America (NORAM) > United States"),
    ("Mountain", "AMER > North America (NORAM) > United States"),
    ("Greater China", "APAC > East Asia"),
    ("International", "Non-regional"),
    ("EMEA And Asia Pacific", "Multi-region"),
])
def test_paths_for_the_labels_that_caused_trouble(label, expected):
    assert describe_label(label) == expected


def test_a_member_is_not_described_by_its_own_name():
    for label in ("United States", "Japan", "Germany", "Brazil"):
        assert label not in describe_label(label).split(" > ")


def test_us_regions_are_not_mistaken_for_countries():
    """'Mountain' is a Toll Brothers US region, not Montana."""
    for label in ("Mountain", "North", "South", "Midwest", "Southwest"):
        assert path_for_label(label)["country"] == "United States"
        assert path_for_label(label)["state"] == ""


def test_a_raw_filing_label_resolves_like_its_display_name():
    for raw, shown in [("U.S.", "United States"), ("EMEA", "Europe, Middle East and Africa (EMEA)")]:
        assert node_for_label(raw) == node_for_label(shown)


def test_non_places_are_flagged_as_excluded():
    assert is_excluded("Corporate Segment")
    assert not is_excluded("United States")


# ── speed ────────────────────────────────────────────────────────────────

def test_cascade_is_fast_enough_for_a_rerun():
    """The cascade runs on every Streamlit rerun, so it has to stay trivial."""
    import time

    members = LIVE_MEMBERS * 50          # 4,500 members ~ far past any real load
    node_for_label.cache_clear()
    started = time.perf_counter()
    for level in LEVELS:
        level_options(members, level, {"region": ["AMER"]})
    filter_members(members, {"region": ["AMER"], "country": ["United States"]})
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 250, f"cascade took {elapsed_ms:.0f} ms"


def test_every_level_has_a_title():
    assert set(LEVEL_TITLES) == set(LEVELS)
