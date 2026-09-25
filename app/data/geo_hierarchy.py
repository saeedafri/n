"""Where each geographic segment name sits: Region > Sub-region > Country > State > City.

Pure lookup over ``geo_hierarchy.json`` — no database, no Streamlit, no I/O after
the first call. The screener's cascading filters run on every rerun, so every
function here works off dicts built once and cached for the life of the process.

This layer only decides *grouping*. The name a user sees still comes from
``segment_aliases.canonicalize_geo_label``, so the business team keeps control of
wording and the data team keeps control of placement.

The guiding rule is that a filter never loses a company. A member the workbook
has no node for is still selectable (it lands under "Unclassified"), and a level
a node does not reach — the Nordics have no country — leaves that member visible
at every level it does reach.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from data.segment_aliases import canonicalize_geo_label, geo_match_key, normalize_geo_key

HIERARCHY_PATH = Path(__file__).with_name("geo_hierarchy.json")

# Outermost first. Mirrors _LEVELS in scripts/build_geo_hierarchy.py.
LEVELS: Tuple[str, ...] = ("region", "sub_region", "country", "state", "city")

LEVEL_TITLES: Dict[str, str] = {
    "region": "Region",
    "sub_region": "Sub-region",
    "country": "Country",
    "state": "State / Province",
    "city": "City",
}

# Members with no node yet are grouped under this rather than dropped, so a label
# that arrives tomorrow is still reachable before anyone updates the workbook.
UNCLASSIFIED = "Unclassified"


@lru_cache(maxsize=1)
def _hierarchy() -> Dict:
    with HIERARCHY_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=4096)
def node_for_label(label: str) -> Optional[Dict[str, str]]:
    """The hierarchy node a segment name belongs to, or None if it has none.

    Tries the name as given, then the canonical spelling, and finally the loose
    key, so both a raw filing label and the name the screener displays resolve to
    the same node.
    """
    if not label:
        return None
    data = _hierarchy()
    nodes, exact, loose = data["nodes"], data["exact"], data["loose"]

    for candidate in (label, canonicalize_geo_label(label)):
        node_name = exact.get(normalize_geo_key(candidate))
        if node_name:
            return nodes.get(node_name)
    for candidate in (label, canonicalize_geo_label(label)):
        node_name = loose.get(geo_match_key(candidate))
        if node_name:
            return nodes.get(node_name)
    return None


def path_for_label(label: str) -> Dict[str, str]:
    """{level: value} for a segment name. Missing levels come back empty."""
    node = node_for_label(label)
    if not node:
        return {level: "" for level in LEVELS}
    return {level: node.get(level, "") or "" for level in LEVELS}


def is_excluded(label: str) -> bool:
    """True when the workbook lists this label as not a place at all."""
    return normalize_geo_key(label) in _hierarchy()["excluded"]


def _index(members: Sequence[str]) -> List[Tuple[str, Dict[str, str]]]:
    return [(member, path_for_label(member)) for member in members]


def level_options(
    members: Sequence[str],
    level: str,
    selections: Optional[Dict[str, Iterable[str]]] = None,
) -> List[str]:
    """Values to offer at `level`, given what is already picked at wider levels.

    Only values that some member in `members` actually reaches are returned, so
    the dropdown never offers a place that would come back empty. Members that
    stop short of `level` contribute nothing here but are not filtered out — see
    filter_members.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    wider = LEVELS[: LEVELS.index(level)]
    selections = selections or {}

    values, has_unplaced = set(), False
    for member, path in _index(members):
        if not _matches(path, selections, wider):
            continue
        value = path.get(level, "")
        if value:
            values.add(value)
        elif level == "region" and not any(path.values()):
            has_unplaced = True

    ordered = sorted(values)
    if has_unplaced:
        ordered.append(UNCLASSIFIED)
    return ordered


def _matches(
    path: Dict[str, str],
    selections: Dict[str, Iterable[str]],
    levels: Iterable[str],
    include_broader: bool = True,
) -> bool:
    """Does one member's path satisfy every selection made at `levels`?

    A member whose path stops short of a filtered level is kept by default: a
    filer reporting only "Americas" has no country, and hiding it when someone
    picks United States would silently drop that company. Pass
    ``include_broader=False`` for a strict view that shows only members at or
    inside the chosen place.
    """
    for level in levels:
        chosen = [value for value in (selections.get(level) or []) if value]
        if not chosen:
            continue
        value = path.get(level, "")
        if value:
            if value not in chosen:
                return False
        elif UNCLASSIFIED in chosen:
            if any(path.values()):
                return False
        elif not any(path.values()):
            # No node at all — only reachable through the Unclassified option.
            return False
        elif not include_broader:
            return False
    return True


def filter_members(
    members: Sequence[str],
    selections: Optional[Dict[str, Iterable[str]]] = None,
    include_broader: bool = True,
) -> List[str]:
    """The members left after applying every level selection. Order is preserved."""
    selections = {
        level: [value for value in (selections or {}).get(level) or [] if value]
        for level in LEVELS
    }
    if not any(selections.values()):
        return list(members)
    return [
        member
        for member, path in _index(members)
        if _matches(path, selections, LEVELS, include_broader)
    ]


def describe_label(label: str) -> str:
    """Where a member sits, as a trail: 'AMER > North America (NORAM)'.

    The member's own name is dropped from the end — "United States — AMER >
    North America (NORAM) > United States" says it twice, and the picker shows
    this next to the name.
    """
    path = path_for_label(label)
    trail = [path[level] for level in LEVELS if path[level]]
    while trail and trail[-1].casefold() == label.strip().casefold():
        trail.pop()
    return " > ".join(trail)


def unplaced_members(members: Sequence[str]) -> List[str]:
    """Members with no hierarchy node — what to send the data team next."""
    return [member for member in members if node_for_label(member) is None]
