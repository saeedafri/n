"""Business segment names, removals and filter tags, from the business team's workbook.

``business_segment_map.json`` is generated from "Business-Segment-Final-Mapping-STG.xlsx"
by ``scripts/build_business_segment_map.py``. For every business segment label the
screener has seen, the workbook rules one of three things:

  keep    the label is a line of business; spellings of one segment merge into
          one Segment Member ("Gourmet Food and Gift Baskets" and "Gourmet Foods
          & Gift Baskets" are one member)
  remove  not a segment at all (corporate, eliminations, guarantor columns,
          financial line items, single-segment totals)
  geo     a place, screened on the Geographical screen under its geo name

A label the workbook has not ruled on yet stays in Business under its own name,
with no filter tags, as the workbook asks — except a place the geographic
workbook already names, which the workbook moves to Geographical as a rule
("Exact match to an approved Geo mapping spelling").

The screener applies this when it writes its segment cache and again when it
reads it, so neither a cache written before a workbook change nor a filing that
arrives tomorrow can show a label the workbook did not allow. The Market Data
Segments tab is not affected: it shows segments as filed.

Pure lookups, no database: the screening page calls these on every rerun.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from data.segment_aliases import normalize_geo_key

MAP_PATH = Path(__file__).with_name("business_segment_map.json")

# The four filters above the Segment members list, outermost first. Product
# narrows within Product Category the way State narrows within Country.
FILTERS: Tuple[str, ...] = ("Sales Channel", "Revenue Type", "Product Category", "Product")


@functools.lru_cache(maxsize=1)
def _map() -> dict:
    try:
        return json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        from utils.server_logger import log_error
        log_error(f"[BUSINESS_SEGMENT_MAP] {MAP_PATH.name} unreadable, labels shown as filed: {exc}")
        return {"exact": {}, "loose": {}, "tags": {}}


def exact_key(label: str) -> str:
    return " ".join(str(label or "").split()).casefold()


def _ruling(label: str) -> Optional[list]:
    data = _map()
    return data["exact"].get(exact_key(label)) or data["loose"].get(normalize_geo_key(label))


def is_reviewed(label: str) -> bool:
    """True when the workbook rules on this label."""
    return _ruling(label) is not None


def is_kept_business_label(label: str) -> bool:
    """True when the workbook rules this label a line of business. The segment
    classifier asks before its place rules, which would read "Turkey" (poultry)
    as Türkiye and drop or rename it."""
    ruling = _ruling(label)
    return bool(ruling) and ruling[0] == "keep"


@functools.lru_cache(maxsize=8192)
def business_verdict(label: str) -> Tuple[str, str]:
    """('keep', member) | ('remove', '') | ('geo', place) for one business segment label."""
    ruling = _ruling(label)
    if ruling:
        return ruling[0], (ruling[1] if len(ruling) > 1 else "")
    from data.geo_hierarchy import names_a_place
    if names_a_place(label):
        return "geo", label
    return "keep", label


@functools.lru_cache(maxsize=1)
def _spellings_by_member() -> Dict[str, Tuple[str, ...]]:
    found: Dict[str, set] = {}
    for key, ruling in _map()["exact"].items():
        if ruling[0] == "keep":
            found.setdefault(exact_key(ruling[1]), set()).add(key)
    return {member: tuple(sorted(keys)) for member, keys in found.items()}


def member_spellings(member: str) -> List[str]:
    """Every label (case-folded) the workbook merges into `member`, itself included.

    A cache written before the workbook still holds Colgate's segment as "Pet
    Nutrition"; picking the member "Pet Nutrition (Hill's)" must still find it."""
    key = exact_key(member)
    return sorted({key, *_spellings_by_member().get(key, ())})


def split_business_members(
    members: Mapping[str, Mapping[int, Optional[float]]],
) -> Tuple[Dict[str, Dict[int, float]], Dict[str, Dict[int, float]]]:
    """One metric's ``{label: {year: value}}`` → (business, geography), per the workbook.

    Spellings merged into one member are combined per year: a figure filed under
    two spellings counts once, distinct lines are added — Target's "Other" and
    "Other revenue" are both member "Other", and the member is their sum.
    """
    parts: Tuple[dict, dict] = ({}, {})
    for label, by_year in members.items():
        action, name = business_verdict(label)
        if action == "remove":
            continue
        part = parts[0] if action == "keep" else parts[1]
        for year, value in by_year.items():
            if value is not None:
                part.setdefault(name, {}).setdefault(year, set()).add(float(value))
    return tuple(  # type: ignore[return-value]
        {name: {year: sum(values) for year, values in by_year.items()}
         for name, by_year in part.items()}
        for part in parts)


def member_tags(member: str) -> Dict[str, str]:
    """{filter: tag} for a Segment Member. Unreviewed members have none."""
    return _map()["tags"].get(exact_key(member)) or {}


def filter_members(
    members: Sequence[str],
    selections: Optional[Mapping[str, Iterable[str]]] = None,
) -> List[str]:
    """Members carrying a picked tag in every filter that has a pick. Order kept.

    A filter left on Any restricts nothing, so an untagged member ("Other",
    brand codes) shows only while every filter is Any.
    """
    picked = {name: set(values) for name, values in (selections or {}).items() if values}
    if not picked:
        return list(members)
    return [member for member in members
            if all(member_tags(member).get(name) in values for name, values in picked.items())]


def filter_options(
    members: Sequence[str],
    name: str,
    selections: Optional[Mapping[str, Iterable[str]]] = None,
) -> List[str]:
    """Tags to offer in filter `name`: those some member reaches under the picks in
    every OTHER filter, so no option comes back empty whichever filter is set
    first. Product under Product Category = Pet lists only the pet products."""
    others = {f: v for f, v in (selections or {}).items() if f != name}
    pool = filter_members(members, others)
    return sorted({member_tags(member).get(name) for member in pool} - {None, ""})
