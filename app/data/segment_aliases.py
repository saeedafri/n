"""
Geographical segment canonical alias definitions for Coresight Screening.

Maps raw XBRL dimension_member_label values to canonical display labels.
Only conservative, high-confidence US-only aliases are grouped here.

Investigation completed 2026-06-13.
DO NOT add "Domestic", "North America", "Americas", "U.S. and Canada", or any
multi-country label to GEO_CANONICAL_GROUPS["United States"] without a separate
investigation and explicit review.
"""

from typing import Dict, FrozenSet, List, Set

# Canonical "United States" group — confirmed exclusively US labels from v4 raw samples.
# All matching is case-insensitive (lower+strip applied before lookup).
_US_SAFE_ALIASES: FrozenSet[str] = frozenset([
    "United States",          # canonical display label; kept in dropdown
    "UNITED STATES",          # all-caps variant (PEP, AMD, QCOM, NVDA, etc.)
    "United states",          # lowercase-s variant (GIII, CAL)
    "U.S.",                   # abbreviation (AAPL, WMT, BBY, EL, etc.)
    "US",                     # no-dot variant (GIS FY2016)
    "U.S",                    # missing trailing dot (ELF FY2016)
    "U S",                    # space-separated (PMNT FY2025)
    "United State",           # typo (RGR)
    "In the U.S.",            # Home Depot current label
    "Inside the U.S.",        # Home Depot earlier label (FY2019-2022)
    "U.S. operations",        # IBM domestic operations
    "United States Operations",  # Costco FY2022-2024 label (→ "United States" FY2025)
    "United States:",         # trailing colon artifact (JOUT)
    "United States Federal",  # US federal segment (Latham Group / SWIM)
    "Domestic/United States", # explicit US label (single filer)
    "pf0:US",                 # XBRL namespace-prefix artifact resolving to US
])

# Canonical "Canada" — single-country Canada labels (filing drift collapses to one).
_CANADA_ALIASES: FrozenSet[str] = frozenset([
    "Canada", "Canadian Operations", "Canada Operations",
])

# Canonical "United Kingdom" — confirmed UK spelling variants.
_UK_ALIASES: FrozenSet[str] = frozenset([
    "United Kingdom", "U.K.", "UK", "U.K", "United kingdom",
])

# Canonical "North America" — only the unambiguous single-region spellings
# (NOT multi-region labels like "North America And Europe").
_NORTH_AMERICA_ALIASES: FrozenSet[str] = frozenset([
    "North America", "North American Region", "North america",
])

# Canonical "Non-US" — labels that all mean "revenue outside the United States".
_NON_US_ALIASES: FrozenSet[str] = frozenset([
    "Non-US", "Non-U.S.", "Non-United States", "Other Non-U.S.", "Other non-U.S.",
    "Other Non-United States", "Outside United States", "Outside the United States",
    "Outside of the United States", "Outside the U.S.", "Outside of the U.S.",
    "Non-U.S. operations",
])

# Canonical "Americas" — only the whole-continent spellings (NOT "Latin America",
# "South America", "North America", "Other Americas" — those are distinct regions).
_AMERICAS_ALIASES: FrozenSet[str] = frozenset([
    "Americas", "The Americas", "Total Americas",
])

# canonical_label → frozenset of all raw aliases (case variants included).
GEO_CANONICAL_GROUPS: Dict[str, FrozenSet[str]] = {
    "United States":  _US_SAFE_ALIASES,
    "Canada":         _CANADA_ALIASES,
    "United Kingdom": _UK_ALIASES,
    "North America":  _NORTH_AMERICA_ALIASES,
    "Non-US":         _NON_US_ALIASES,
    "Americas":       _AMERICAS_ALIASES,
}

# raw_label_lower → canonical_label (built from GEO_CANONICAL_GROUPS).
GEO_ALIAS_TO_CANONICAL: Dict[str, str] = {
    alias.lower().strip(): canon
    for canon, aliases in GEO_CANONICAL_GROUPS.items()
    for alias in aliases
}

# Raw labels to hide from the Geo Segment dropdown.
# These are non-canonical aliases (e.g. "U.S.", "UNITED STATES") that would
# otherwise appear as duplicate-looking options alongside "United States".
GEO_SUPPRESSED_DROPDOWN_LABELS: FrozenSet[str] = frozenset(
    alias
    for canon, aliases in GEO_CANONICAL_GROUPS.items()
    for alias in aliases
    if alias != canon  # keep the canonical itself; suppress all other raw variants
)

# Pension / retirement-plan labels that contaminate geo revenue via StatementGeographicalAxis.
# Companies HON, MDLZ, DBD file pension geographic breakdowns on the same geo axis as
# revenue segments.  These labels are NOT geographic revenue and must be excluded.
# Matched with .lower().strip() comparison.
GEO_PENSION_SKIP_LABELS: FrozenSet[str] = frozenset([
    "u.s. plans",
    "u.s. plan",
    "domestic plan",
    "domestic plans",
    "u.s. defined benefit plan",
    "u.s. defined benefit plans",
    "non-u.s. plans",
    "non-u.s. plan",
    "non-us plans",
    "non-us plan",
    "canadian salaried and hourly plans",
    "u.k. plans",
    "uk plans",
    "foreign plans",
    "foreign plan",
    "pension plans",
    "pension plan",
    "retirement plans",
    "retirement plan",
    "defined benefit plans",
    "defined benefit plan",
    "u.s. pension plans",
    "u.s. pension plan",
    "international plans",
    "international plan",
])


def canonicalize_geo_label(raw_label: str) -> str:
    """Return the canonical label for a raw geo member label.

    If the label is a known alias, returns its canonical.
    Otherwise returns the original label unchanged (preserving case).
    """
    return GEO_ALIAS_TO_CANONICAL.get(raw_label.lower().strip(), raw_label)


def expand_geo_canonical_segments(selected_segments: List[str]) -> List[str]:
    """Expand canonical geo segment labels to all raw aliases for DB querying.

    Example: ["United States", "Europe"] ->
             ["United States", "UNITED STATES", "United states", "U.S.", "US",
              "U.S", "U S", "United State", "In the U.S.", "Inside the U.S.",
              "U.S. operations", "United States:", "United States Federal", "Europe"]

    Non-canonical labels (raw aliases or unrelated labels) are returned unchanged.
    """
    result: List[str] = []
    seen: Set[str] = set()
    for seg in selected_segments:
        canon = canonicalize_geo_label(seg)
        if canon in GEO_CANONICAL_GROUPS:
            for alias in GEO_CANONICAL_GROUPS[canon]:
                if alias not in seen:
                    result.append(alias)
                    seen.add(alias)
        else:
            if seg not in seen:
                result.append(seg)
                seen.add(seg)
    return result


# Business segment labels incorrectly included in geo presets (investigation 2026-06-13).
# These have ZERO geographical revenue rows in coreiq_screening_segment_values_cache.
# They're present in the DB member cache due to old preset upserts; suppressed at
# runtime until the next full cache rebuild removes them permanently.
GEO_KNOWN_BIZ_LABELS: FrozenSet[str] = frozenset([
    "walmart u.s.",
    "walmart international",
    "sam's club",
])


def is_geo_pension_label(raw_label: str) -> bool:
    """Return True if the label is a known pension/plan contamination label."""
    return raw_label.lower().strip() in GEO_PENSION_SKIP_LABELS


def is_geo_known_biz_label(raw_label: str) -> bool:
    """Return True if the label is a known business segment incorrectly in geo presets."""
    return raw_label.lower().strip() in GEO_KNOWN_BIZ_LABELS
