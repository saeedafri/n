"""Geographic segment label canonicalisation for Coresight.

Raw XBRL ``dimension_member_label`` values are whatever each filer typed, so one
place arrives under many spellings — "EMEA", "Europe, Middle East and Africa",
'Europe, Middle East and Africa ("EMEA")', "Europe/Middle East/Africa" — and the
screener's Step 3 dropdown listed every one of them as a separate option.

Naming is a business decision, so the names come from the business team's
workbook ("Countries Mapping.xlsx"), generated into ``geo_label_map.json`` by
``scripts/build_geo_label_map.py``.  Nothing here invents a name: this module
only decides which raw label the workbook is talking about.

Two lookup keys do that, and both are pure text normalisation:

  ``normalize_geo_key``  case, unicode quotes, non-breaking and zero-width
                         spaces, dash variants, XBRL ``[Member]`` residue,
                         footnote markers ``(a)``/``(1)`` and trailing
                         punctuation.  Nothing that changes the words.
  ``geo_match_key``      additionally treats "&", "/" and "," as "and", drops
                         "the" and roll-up words, and drops a parenthesised
                         acronym that merely repeats the label ('Asia Pacific
                         ("APAC")').  Used only when the exact key misses, and
                         only for keys exactly one canonical claims, so a label
                         the workbook never saw still lands on the right name.

A parenthetical is dropped only when it is a short acronym.  "Americas
(excluding United States)" and "Asia Pacific (including Oceania)" are different
segments from "Americas" and "Asia Pacific", and dropping their qualifiers would
merge revenue the filer deliberately split.

Labels the workbook rules out (business segments, facility descriptions, tax and
pension categories, scraper artifacts like ``srt_SegmentGeographicalDomain``)
are hidden rather than renamed — they are not places.
"""

from __future__ import annotations

import functools
import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Tuple

_MAP_PATH = Path(__file__).with_name("geo_label_map.json")

# Invisible characters filers embed mid-label.  PriceSmart files "Central
# ‎American ‎Operations" with bidi marks, which read identically on
# screen but split one member into two options.
_INVISIBLE = "​‌‍‎‏﻿­"
_DASHES = "‐‑‒–—−"
_QUOTES = {"’": "'", "‘": "'", "“": '"', "”": '"'}

_MEMBER_RESIDUE = re.compile(r"\[member[\]\}]|\{member\}|\(member\)|member\]")
_FOOTNOTE = re.compile(r"\((?:[a-z]|\d{1,2})\)")
_PARENTHETICAL = re.compile(r"\(\s*[\"'“”]?([A-Za-z0-9&.']{1,10})[\"'“”]?\s*\)")
_ROLLUP_WORDS = re.compile(r"\b(?:total|subtotal)\b")


def normalize_geo_key(label: str) -> str:
    """Lookup key that unifies formatting without changing the words."""
    if not label:
        return ""
    text = unicodedata.normalize("NFKC", str(label))
    for source, target in _QUOTES.items():
        text = text.replace(source, target)
    for char in _INVISIBLE:
        text = text.replace(char, "")
    for dash in _DASHES:
        text = text.replace(dash, "-")
    text = text.lower()
    text = _MEMBER_RESIDUE.sub(" ", text)
    text = _FOOTNOTE.sub(" ", text)
    # Spacing around a dash or slash is filer whim: Ingredion files "Asia-
    # Pacific" and "Asia/ Pacific" for what it elsewhere calls "Asia-Pacific".
    text = re.sub(r"\s*([-/])\s*", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[-:,.\s]+", "", text)
    text = re.sub(r"[:,.\s]+$", "", text)
    return text


def _drop_acronym_parenthetical(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        inner = match.group(1).replace(".", "").replace("'", "")
        return " " if re.fullmatch(r"[a-z0-9&]{1,8}", inner) else match.group(0)

    return _PARENTHETICAL.sub(replace, text)


def geo_match_key(label: str) -> str:
    """Looser key: conjunction, article and roll-up wording folded together."""
    text = normalize_geo_key(label)
    if not text:
        return ""
    text = _drop_acronym_parenthetical(text)
    # A surviving parenthetical is a real qualifier ("(excluding United Kingdom)"),
    # so its words stay and only the brackets go — that merges "China (including
    # Hong Kong)" with "China Including Hong Kong" without ever merging a
    # qualified region into the bare one.
    text = text.replace("(", " ").replace(")", " ")
    text = text.replace("&", " and ").replace("/", " and ").replace(",", " and ")
    text = re.sub(r"\bthe\b", " ", text)
    text = _ROLLUP_WORDS.sub(" ", text)
    text = re.sub(r"[.'\"]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    while " and and " in f" {text} ":
        text = re.sub(r"\band\s+and\b", "and", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^and\s+|\s+and$", "", text)


@functools.lru_cache(maxsize=1)
def _label_map() -> dict:
    """Parse the generated workbook map once per process (~30 KB, no DB, no I/O
    after the first call — the screening page consults it on every rerun)."""
    try:
        return json.loads(_MAP_PATH.read_text())
    except Exception:
        return {"exact": {}, "loose": {}, "removed": [], "iso_codes": {}}


@functools.lru_cache(maxsize=1)
def _exact_map() -> Dict[str, str]:
    return dict(_label_map().get("exact") or {})


@functools.lru_cache(maxsize=1)
def _loose_map() -> Dict[str, str]:
    return dict(_label_map().get("loose") or {})


@functools.lru_cache(maxsize=1)
def _removed_keys() -> FrozenSet[str]:
    return frozenset(_label_map().get("removed") or ())


def canonicalize_geo_label(raw_label: str) -> str:
    """Business-approved display name for a raw geo member label.

    Returns the label unchanged when the workbook does not name it, so a segment
    is never dropped or renamed on a guess — new filings keep showing up under
    their own name until the business team maps them.
    """
    key = normalize_geo_key(raw_label)
    if not key:
        return raw_label
    canonical = _exact_map().get(key)
    if canonical:
        return canonical
    return _loose_map().get(geo_match_key(raw_label)) or raw_label


def is_geo_named_label(raw_label: str) -> bool:
    """True when the workbook names this label.

    A canonical name is itself named — "United States" resolves to itself, so
    comparing before and after canonicalisation cannot tell the two apart. The
    unmapped-labels report for the business team depends on this distinction.
    """
    return (
        normalize_geo_key(raw_label) in _exact_map()
        or geo_match_key(raw_label) in _loose_map()
    )


def is_geo_removed_label(raw_label: str) -> bool:
    """True for labels the business team ruled are not geographies."""
    return normalize_geo_key(raw_label) in _removed_keys()


def geo_removal_reason(raw_label: str) -> str:
    reasons = _label_map().get("removed_reasons") or {}
    return reasons.get(normalize_geo_key(raw_label), "")


def geo_iso_code(canonical_label: str) -> str:
    return (_label_map().get("iso_codes") or {}).get(canonical_label, "")


# raw normalized key → canonical.  Read by SegmentDataRepository._geo_name_set()
# to decide whether a member name denotes a geography at all.
GEO_ALIAS_TO_CANONICAL: Dict[str, str] = _exact_map()


# ── grouping ────────────────────────────────────────────────────────────────
# Display grouping needs the label universe, because two labels the workbook
# never named can still be the same segment ("Other Foreign" / "Other foreign
# (1)").  Callers pass the labels they hold — for screening, the member cache.

_TOTAL_PREFIX = re.compile(r"^(?:sub)?total\b")
_ALL_PREFIX = re.compile(r"^all\b")


def _display_rank(label: str) -> Tuple[int, int, int, int, str]:
    """Sort key picking the most readable spelling within one group.

    Fewest artifacts first, then no trailing punctuation, then least roll-up
    phrasing ("Subtotal all foreign countries" loses to "All foreign
    countries"), then the longest — spelled out beats abbreviated.

    A descriptive parenthetical is not an artifact: "China (including Hong
    Kong)" reads better than "China Including Hong Kong". Only a repeated
    acronym or a footnote marker counts against a label.
    """
    lowered = label.lower()
    artifacts = sum(label.count(char) for char in "[{}]&:")
    if _drop_acronym_parenthetical(lowered) != lowered or _FOOTNOTE.search(lowered):
        artifacts += 1
    trailing = 0 if label == label.rstrip(" .:,-") else 1
    rollup = 2 if _TOTAL_PREFIX.match(lowered) else (1 if _ALL_PREFIX.match(lowered) else 0)
    return (artifacts, trailing, rollup, -len(label), lowered)


def build_geo_label_groups(labels: Iterable[str]) -> Dict[str, List[str]]:
    """Group raw geo labels by the segment they name → {display: [raw, ...]}.

    Workbook-named labels group under their canonical name.  The rest group by
    ``geo_match_key`` so pure spelling drift still collapses, and the group shows
    its most readable spelling.  Removed labels are left out entirely.
    """
    grouped: Dict[str, List[str]] = {}
    unnamed: Dict[str, List[str]] = {}
    # Callers may hand over one entry per fetched row; normalising text is the
    # cost here, so each distinct label is normalised once.
    for label in dict.fromkeys(labels):
        if not label or is_geo_excluded_label(label):
            continue
        canonical = canonicalize_geo_label(label)
        if canonical != label or normalize_geo_key(label) in _exact_map():
            grouped.setdefault(canonical, []).append(label)
        else:
            unnamed.setdefault(geo_match_key(label) or normalize_geo_key(label), []).append(label)

    for members in unnamed.values():
        display = min(members, key=_display_rank)
        grouped.setdefault(display, []).extend(members)

    for display, members in grouped.items():
        # A canonical is a valid selection even when no filer spells it that way.
        if display not in members:
            members.append(display)
        grouped[display] = sorted(dict.fromkeys(members))
    return grouped


# ── legacy contamination lists ──────────────────────────────────────────────
# Superseded by the workbook's "remove" sheet for anything it names; kept because
# they cover pension wording no filing in the current universe uses yet, and
# dropping them would silently re-admit that contamination.
GEO_PENSION_SKIP_LABELS: FrozenSet[str] = frozenset([
    "u.s. plans", "u.s. plan", "domestic plan", "domestic plans",
    "u.s. defined benefit plan", "u.s. defined benefit plans",
    "non-u.s. plans", "non-u.s. plan", "non-us plans", "non-us plan",
    "canadian salaried and hourly plans", "u.k. plans", "uk plans",
    "foreign plans", "foreign plan", "pension plans", "pension plan",
    "retirement plans", "retirement plan", "defined benefit plans",
    "defined benefit plan", "u.s. pension plans", "u.s. pension plan",
    "international plans", "international plan",
])

# Business segments that reached the geo member cache through old preset upserts.
GEO_KNOWN_BIZ_LABELS: FrozenSet[str] = frozenset([
    "walmart u.s.", "walmart international", "sam's club",
])


def is_geo_pension_label(raw_label: str) -> bool:
    return normalize_geo_key(raw_label) in GEO_PENSION_SKIP_LABELS


def is_geo_known_biz_label(raw_label: str) -> bool:
    return normalize_geo_key(raw_label) in GEO_KNOWN_BIZ_LABELS


def is_geo_excluded_label(raw_label: str) -> bool:
    """True when a label must not appear as a geographic segment at all."""
    return (
        is_geo_removed_label(raw_label)
        or is_geo_pension_label(raw_label)
        or is_geo_known_biz_label(raw_label)
    )
