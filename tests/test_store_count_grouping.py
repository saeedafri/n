"""Stores by Country groups countries the way Geographical Segments does.

Both read the business's Countries Mapping hierarchy (geo_hierarchy.json): a
store-count bucket is the country's Sub-region there, so the Additional Data tab
(on screen and in Excel, one function for both) and the segment Region /
Sub-region filters cannot put a country in two different places.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

from data.geo_hierarchy import path_for_label  # noqa: E402
from data.repository import (  # noqa: E402
    STORE_GROUP_OTHER, continent_of, group_countries_by_continent, stores_total_label,
)
from data.store_count_sources import CONTINENT_BY_COUNTRY  # noqa: E402


def test_every_known_country_uses_the_segment_sub_region():
    for country in CONTINENT_BY_COUNTRY:
        expected = path_for_label(country)["sub_region"] or STORE_GROUP_OTHER
        assert continent_of(country) == expected, country


def test_the_places_the_old_map_got_wrong():
    assert continent_of("Mexico") == "Latin America (LATAM)"
    assert continent_of("Puerto Rico") == "North America (NORAM)"
    assert continent_of("United Arab Emirates") == "Middle East"
    assert continent_of("Turkey") == "Europe"
    assert continent_of("Australia") == "Oceania (ANZ)"
    assert continent_of("Atlantis") == STORE_GROUP_OTHER


def test_groups_order_and_subtotals():
    countries = {"Japan": {2025: 5}, "Mexico": {2025: 20}, "Canada": {2025: 30},
                 "United States": {2025: 100}, "France": {2025: 7}, "Atlantis": {2025: 1}}
    grouped = group_countries_by_continent(countries)
    assert [g for g, _, _ in grouped] == [
        "North America (NORAM)", "Latin America (LATAM)", "Europe", "East Asia", STORE_GROUP_OTHER]
    assert grouped[0][1] == ["United States", "Canada"]
    assert grouped[0][2] == {2025: 130}


def test_total_labels():
    one = group_countries_by_continent({"United States": {2025: 10}, "Canada": {2025: 5}})
    assert stores_total_label(one, {2025: 15}) == "Total North America (NORAM)"
    americas = group_countries_by_continent({"United States": {2025: 10}, "Mexico": {2025: 5}})
    assert stores_total_label(americas, {2025: 15}) == "Total AMER (Americas)"
    world = group_countries_by_continent({"United States": {2025: 10}, "France": {2025: 5}})
    assert stores_total_label(world, {2025: 15}) == "Total Worldwide"
    assert stores_total_label(one, {2025: 99}) == "Total Worldwide", "partial split stays Worldwide"
