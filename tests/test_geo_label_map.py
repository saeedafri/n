"""Geographic segment label mapping — the guarantees the screener depends on.

The screener's Step 3 dropdown listed one place many times because filers spell
places differently. These tests pin the two things that can silently break:
the business team's names must be reachable from every spelling they listed, and
normalisation must never merge two places the business team kept apart.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

from data.segment_aliases import (  # noqa: E402
    build_geo_label_groups,
    canonicalize_geo_label,
    geo_iso_code,
    geo_match_key,
    is_geo_excluded_label,
    normalize_geo_key,
)

MAP = json.loads((REPO / "app" / "data" / "geo_label_map.json").read_text())


def test_map_is_generated_and_populated():
    assert MAP["_source"] == "Countries Mapping.xlsx"
    assert MAP["_counts"]["workbook_labels"] > 400
    assert MAP["_counts"]["canonicals"] > 200


def test_no_normalized_key_claims_two_canonicals():
    """The safety property: normalisation may merge spellings, never places."""
    by_key = {}
    for key, canonical in MAP["exact"].items():
        assert by_key.setdefault(key, canonical) == canonical, key
    # Loose keys are generated only where a single canonical claims them; an
    # ambiguous one is dropped, and the generator records which.
    assert set(MAP["_ambiguous_loose_keys"]) & set(MAP["loose"]) == set()


@pytest.mark.parametrize("raw,expected", [
    # The duplicates from the reported dropdown.
    ("EMEA", "Europe, Middle East and Africa (EMEA)"),
    ("Europe, Middle East and Africa", "Europe, Middle East and Africa (EMEA)"),
    ('Europe, Middle East and Africa ("EMEA")', "Europe, Middle East and Africa (EMEA)"),
    ("Europe/Middle East/Africa", "Europe, Middle East and Africa (EMEA)"),
    ("Europe, the Middle East\xa0& Africa", "Europe, Middle East and Africa (EMEA)"),
    ("APAC", "Asia Pacific (APAC)"),
    ("Asia-Pacific", "Asia Pacific (APAC)"),
    ("Asia/Pacific", "Asia Pacific (APAC)"),
    ("Asia Pacific (“APAC”)", "Asia Pacific (APAC)"),
    # Case, punctuation and XBRL residue.
    ("UNITED STATES", "United States"),
    ("U.S.", "United States"),
    ("United States [Member}", "United States"),
    ("United States:", "United States"),
    ("pf0:US", "United States"),
    ("Korea, Republic Of", "South Korea"),
    ("Rest Of The World", "Rest of World (RoW)"),
    ("Türkiye", "Türkiye"),
    ("Turkey", "Türkiye"),
])
def test_business_names_reachable_from_every_spelling(raw, expected):
    assert canonicalize_geo_label(raw) == expected


@pytest.mark.parametrize("raw", [
    # Qualified regions are their own segments — merging them into the bare
    # region would silently combine revenue the filer split on purpose.
    "Americas (excluding United States)",
    "Asia Pacific (excluding China and Japan)",
    "Asia Pacific (including Oceania)",
    "Europe (Excluding United Kingdom)",
    "Mainland China (excluding Hong Kong)",
    "China (including Hong Kong)",
])
def test_qualified_regions_are_never_merged_into_the_bare_region(raw):
    assert canonicalize_geo_label(raw) == raw


@pytest.mark.parametrize("raw", [
    "srt_SegmentGeographicalDomain",   # XBRL axis domain, a scraping artifact
    "AllOtherGeographiesMember",       # unparsed XBRL element name
    "Corporate Segment",               # business segment
    "Traditional Homebuilding",        # product line
    "Site in Plano, Texas",            # facility description
    "U.S. Stock Funds",                # investment category
    "U.S. federal",                    # tax jurisdiction
    "U.S. pensions",                   # pension disclosure
    "U.S. plans",                      # pension disclosure (legacy list)
    "Walmart International",           # business segment in the geo cache
])
def test_non_places_are_hidden(raw):
    assert is_geo_excluded_label(raw)
    assert raw not in build_geo_label_groups([raw, "Japan"])


def test_unnamed_labels_survive_untouched():
    """A label the workbook never saw keeps its own name — nothing is dropped."""
    for raw in ("Midwest", "Far East", "Emerging Markets", "Eastern Mediterranean"):
        assert canonicalize_geo_label(raw) == raw
        assert raw in build_geo_label_groups([raw])


def test_spelling_drift_collapses_without_a_workbook_entry():
    """New spellings of an unnamed segment still land on one option."""
    groups = build_geo_label_groups([
        "United States and Canada", "United States & Canada",
        'United States and Canada ("US&CAN")',
    ])
    assert list(groups) == ["United States and Canada"]
    assert len(groups["United States and Canada"]) == 3


def test_group_expansion_covers_every_raw_spelling():
    """Selecting one option must match every spelling of it in SQL."""
    raw_labels = ["United States", "U.S.", "UNITED STATES", "USA",
                  "United States [Member}", "Japan"]
    groups = build_geo_label_groups(raw_labels)
    assert set(groups["United States"]) == {
        "U.S.", "UNITED STATES", "USA", "United States", "United States [Member}"}
    assert groups["Japan"] == ["Japan"]


def test_canonical_is_selectable_even_when_no_filer_spells_it_that_way():
    groups = build_geo_label_groups(["EMEA"])
    assert "Europe, Middle East and Africa (EMEA)" in groups
    assert "EMEA" in groups["Europe, Middle East and Africa (EMEA)"]


def test_keys_are_stable_under_invisible_characters():
    # PriceSmart embeds bidi marks; they split one member into two options.
    assert normalize_geo_key("Central ‎American ‎Operations") == \
        "central american operations"
    # Non-breaking space vs plain space must produce one key, not two.
    assert normalize_geo_key("Europe, the Middle East\xa0& Africa") == \
        normalize_geo_key("Europe, the Middle East & Africa")
    assert normalize_geo_key("Asia–Pacific") == "asia-pacific"
    assert geo_match_key("Asia Pacific, Other") == geo_match_key("Asia Pacific and other")


def test_iso_codes_carried_through():
    assert geo_iso_code("United States") == "USA"
    assert geo_iso_code("Türkiye") == "TUR"
    assert geo_iso_code("Not A Country") == ""


def test_empty_and_junk_input_is_safe():
    for value in ("", "   ", ":", "-"):
        assert canonicalize_geo_label(value) == value
        assert not is_geo_excluded_label(value)
    assert build_geo_label_groups(["", None, "Japan"]) == {"Japan": ["Japan"]}


def test_renamed_segment_keeps_the_newest_year_not_the_biggest_value():
    """Uber renamed "United States And Canada" to ...("US&CAN"), so each spelling
    has its own latest year in the cache (2023 and 2025). Collapsing them must
    keep the newer year — picking the larger value would show a stale number for
    any segment that shrank."""
    from data.screening_service import _build_ticker_segment_values_from_cache

    resolve = lambda label: "United States and Canada"  # noqa: E731
    rows = [
        {"ticker": "UBER", "member_label": "United States And Canada",
         "report_fiscal_year": 2023, "value_mm": 20_436.0},
        {"ticker": "UBER", "member_label": 'United States and Canada ("US&CAN")',
         "report_fiscal_year": 2025, "value_mm": 26_469.0},
    ]
    assert _build_ticker_segment_values_from_cache(rows, resolve) == {
        "UBER": {"United States and Canada": 26_469.0}}

    # Same rows, newer year first, and a shrinking segment: still the newer year.
    shrinking = [
        {"ticker": "X", "member_label": "Europe", "report_fiscal_year": 2025,
         "value_mm": 100.0},
        {"ticker": "X", "member_label": "Europe:", "report_fiscal_year": 2019,
         "value_mm": 900.0},
    ]
    assert _build_ticker_segment_values_from_cache(
        shrinking, lambda label: "Europe") == {"X": {"Europe": 100.0}}


def test_case_variants_group_even_when_the_member_cache_hides_one():
    """MySQL's case-insensitive GROUP BY leaves the member cache holding one
    arbitrary spelling per place, so grouping must work from the fetched labels
    too — otherwise a value gets a name the dropdown never offered."""
    groups = build_geo_label_groups([
        "United States and Canada",        # the spelling the member cache kept
        "United States And Canada",        # the spelling the values cache holds
    ])
    assert list(groups) == ["United States and Canada"]


def test_named_vs_unnamed_is_distinguishable():
    """A canonical resolves to itself, so canonicalize() cannot tell "named" from
    "not in the workbook" — the business-team report depends on this check."""
    from data.segment_aliases import is_geo_named_label

    assert is_geo_named_label("United States")   # a canonical is named
    assert is_geo_named_label("EMEA")            # a spelling of one is named
    assert not is_geo_named_label("Midwest")     # the workbook never saw it
    assert not is_geo_named_label("Other countries")


def test_watchlist_scoped_options_collapse_the_same_way():
    """The watchlist-scoped dropdown builds its options from live SQL rows rather
    than the member cache. It must collapse identically, or narrowing to a
    watchlist would bring the duplicate options back."""
    from data.screening_service import _normalize_segment_options_rows

    rows = [
        {"dimension_member_label": m, "full_dimension_label": f"Geographical: {m}",
         "company_count": c}
        for m, c in [
            ("EMEA", 5), ("Europe, Middle East and Africa", 3),
            ("Europe/Middle East/Africa", 1),
            ("United States", 9), ("U.S.", 2), ("UNITED STATES", 1),
            ("Corporate Segment", 4),          # not a place
            ("srt_SegmentGeographicalDomain", 1),  # scraper artifact
            ("Japan", 2),
        ]
    ]
    options = dict(_normalize_segment_options_rows(rows, "geographical"))
    assert set(options) == {
        "Europe, Middle East and Africa (EMEA)", "United States", "Japan"}
    assert options["Europe, Middle East and Africa (EMEA)"] == 9   # 5 + 3 + 1
    assert options["United States"] == 12                          # 9 + 2 + 1

    # Business segments have no collapse step and must keep every raw label.
    biz = [
        {"dimension_member_label": m, "full_dimension_label": f"Business Segments: {m}",
         "company_count": 1}
        for m in ("Retail", "Wholesale", "Corporate Segment")
    ]
    assert set(dict(_normalize_segment_options_rows(biz, "business"))) == {
        "Retail", "Wholesale", "Corporate Segment"}


def test_non_places_are_dropped_by_the_shared_classifier():
    """The Segments tab and the screening cache are both built from
    _classify_member, so a label the business team ruled is not a place must be
    dropped there — otherwise the tab shows "Site in Plano, Texas" as a
    geography while the screener hides it."""
    from data.repository import SegmentDataRepository

    def geo_row(member):
        return {"dimension": "srt:StatementGeographicalAxis",
                "dimension_label": member, "dimension_member_label": member,
                "full_dimension_label": f"Geographical: {member}"}

    for member in ("Site in Plano, Texas", "U.S. federal", "U.S. pensions",
                   "City Living", "Irwindale Brewery",
                   "srt_SegmentGeographicalDomain"):
        assert SegmentDataRepository._classify_member(geo_row(member), set()) is None, member

    # Real places still classify, under the business team's name.
    assert SegmentDataRepository._classify_member(geo_row("EMEA"), set()) == \
        ("geo", "Europe, Middle East and Africa (EMEA)")
    assert SegmentDataRepository._classify_member(geo_row("U.S."), set()) == \
        ("geo", "United States")
    # A business segment named the same as a removed geo label is untouched:
    # the drop only applies to the geographic axis.
    biz = {"dimension": "us-gaap:StatementBusinessSegmentsAxis",
           "dimension_label": "City Living", "dimension_member_label": "City Living",
           "full_dimension_label": "Business Segments: City Living"}
    assert SegmentDataRepository._classify_member(biz, set()) == ("business", "City Living")


def test_every_canonical_name_is_a_fixed_point():
    """canonicalize(canonicalize(x)) == canonicalize(x).

    The rebuilt cache stores canonical names, and the read path canonicalises
    again. A canonical that renames to something else would make the stored and
    displayed names disagree, and repeated rebuilds could oscillate. That is how
    an invented "Latin America & Caribbean (LACC)" was caught: its loose key was
    already the workbook's "Latin America and the Caribbean".
    """
    canonicals = set(MAP["exact"].values()) | set(MAP["loose"].values())
    drifting = {c: canonicalize_geo_label(c) for c in canonicals
                if canonicalize_geo_label(c) != c}
    assert not drifting, f"canonical names that rename again: {drifting}"


def test_display_map_cannot_leak_a_name_across_sections():
    """Only geographic members get the business team's names, so the display-map
    that picks a spelling must be keyed per section. Philip Morris tags
    "Middle East & Africa" on a business axis and "Middle East and Africa" on the
    geographic one; a shared key let the business spelling win and undo the
    canonicalisation."""
    from datetime import date
    from data.repository import SegmentDataRepository

    def row(member, axis, filed):
        return {"dimension": axis, "dimension_label": member,
                "dimension_member_label": member,
                "full_dimension_label":
                    ("Geographical: " if "Geographical" in axis else "Business Segments: ") + member,
                "filing_date": date.fromisoformat(filed)}

    rows = [
        row("Middle East & Africa", "us-gaap:StatementBusinessSegmentsAxis", "2025-02-10"),
        row("Middle East And Africa", "us-gaap:StatementBusinessSegmentsAxis", "2024-02-10"),
        row("Middle East and Africa", "srt:StatementGeographicalAxis", "2023-02-10"),
    ]
    display = SegmentDataRepository._member_display_map(rows, set())
    key = SegmentDataRepository._member_key("Middle East and Africa")
    assert display[("geo", key)] == "Middle East and Africa"
    assert display[("business", key)] == "Middle East & Africa"


@pytest.mark.parametrize("raw,expected", [
    ("Europe Segment", "Europe"),          # XBRL element-name suffix
    ("China Mainland Segment", "China Mainland"),
    ("International segment", "International"),
    ("Europe:", "Europe"),
    ("North America:", "North America"),
    # A trailing stop is left alone: it is stray in "Japan." but the
    # abbreviation itself in "Outside U.S." and "Ops.", with no reliable rule
    # between them, so ranking (not stripping) handles it.
    ("Japan.", "Japan."),
    ("Outside U.S.", "Outside U.S."),
    ("U.S.", "U.S."),
    ("Eastern Mediterranean Ops.", "Eastern Mediterranean Ops."),
    ("Segments", "Segments"),              # never strip a name to nothing
    ("Rest of World (RoW)", "Rest of World (RoW)"),
])
def test_element_name_artifacts_are_stripped_per_section(raw, expected):
    """Ranking only drops a trailing "Segment"/stop when the same section also
    filed a clean spelling. A segment filed only as "Europe Segment" used to be
    cleaned by accident, by borrowing the geographic axis's spelling — the leak
    the section-keyed map now blocks. Stripping makes it section-local."""
    from data.repository import SegmentDataRepository

    assert SegmentDataRepository._strip_member_artifact(raw) == expected
