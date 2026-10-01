"""Sort a store-count XBRL member label into the level it belongs to.

The member label is whatever the filer typed on the geographic axis of their own
10-K, so one dimension carries countries, US states, cities, marketing regions,
brand names and — because filers mis-tag — the occasional "Class Action
Lawsuit". Nothing here guesses at a mapping; it only says what KIND of thing a
label is, so the data team can see the shape of the work.
"""
from __future__ import annotations

import re
from typing import Tuple

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "district of columbia", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin",
    "wyoming",
}

# Multi-country groupings a filer writes instead of naming each one.
REGIONS = {
    "europe", "north america", "south america", "asia", "east asia", "africa",
    "oceania", "americas", "latin america", "central america", "caribbean",
    "middle east", "emea", "apac", "international", "other international",
    "non-us", "domestic", "worldwide",
}

# Cities and sub-state areas seen on the geographic axis. Listed rather than
# pattern-matched: "Washington" is a state, "Washington D.C." is not, and only
# an explicit list keeps those apart.
CITIES = {
    "boston", "hollywood", "indianapolis", "las vegas", "las vegas nevada",
    "new york city", "washington d.c.", "atlantic city", "tampa",
    "gainesville", "gainesville florida", "fairfield california", "ledyard",
    "spanish fort alabama", "gulf shores alabama", "bogota, columbia",
    "greater los angeles area market", "the greater los angeles area",
    "sacramento area", "central florida", "orange country", "chalut",
}

# Labels that name no place at all — a brand, a franchise arrangement, a
# corporate action. They reached the geographic axis because the filer tagged
# them there.
NOT_A_PLACE = re.compile(
    r"(?i)franchis|licensed|company[- ]o(?:wn|perat)ed|concession|outlet|"
    r"retail stores|shop-in-shop|acquisition|acquired|closures|refranchising|"
    r"lawsuit|intangible|brand portfolio|casino|resort|gasoline|"
    r"distribution cent|units|concepts"
)

LEVELS = ("Country", "US state", "City / area", "Region (multi-country)",
          "Not a place", "Unclassified")


def classify(label: str, known_country) -> Tuple[str, str]:
    """(level, note) for one member label. `known_country` answers "is this a
    country we already recognise?" — passed in so this file holds no map."""
    text = " ".join(str(label).split())
    lowered = text.lower().strip(" .")

    if lowered in US_STATES:
        return "US state", ""
    if lowered in CITIES:
        return "City / area", ""
    if lowered in REGIONS:
        return "Region (multi-country)", "covers more than one country"
    if known_country(text):
        note = "spelled in caps by the filer" if text.isupper() else ""
        return "Country", note
    if NOT_A_PLACE.search(lowered):
        return "Not a place", "filer tagged this on the geographic axis"
    return "Unclassified", "please tell us what this is"
