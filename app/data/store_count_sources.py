"""Where store counts come from: our own XBRL rows, the filing HTML, the LLM.

Split out from store_count_extractor so the parsing logic stays free of I/O and
can be unit-tested against fixed text.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from data.store_count_extractor import (
    MAX_PLAUSIBLE_COUNT, NON_RETAIL_DIMENSIONS, UNIT_RE, XBRL_CONCEPTS,
    StoreCount, filing_to_text, front_section, numbers_in,
)

# ── Geography ────────────────────────────────────────────────────────────────

# Continent per country, for the roll-up the Additional Data tab shows. Only
# countries that actually appear as XBRL geographic members are listed; anything
# unmapped is reported under "Other" rather than guessed at.
CONTINENT_BY_COUNTRY = {
    "United States": "North America", "US": "North America", "USA": "North America",
    "Canada": "North America", "Mexico": "North America", "Puerto Rico": "North America",
    "Costa Rica": "North America", "Panama": "North America", "Guatemala": "North America",
    "Honduras": "North America", "Dominican Republic": "North America",
    "Trinidad And Tobago": "North America", "Jamaica": "North America",
    "Brazil": "South America", "Chile": "South America", "Argentina": "South America",
    "Colombia": "South America", "Peru": "South America", "Ecuador": "South America",
    "United Kingdom": "Europe", "Ireland": "Europe", "France": "Europe",
    "Germany": "Europe", "Spain": "Europe", "Italy": "Europe", "Netherlands": "Europe",
    "Belgium": "Europe", "Switzerland": "Europe", "Austria": "Europe",
    "Sweden": "Europe", "Norway": "Europe", "Denmark": "Europe", "Finland": "Europe",
    "Poland": "Europe", "Portugal": "Europe", "Russia": "Europe", "Ukraine": "Europe",
    "China": "Asia", "Japan": "Asia", "South Korea": "Asia", "Korea": "Asia",
    "India": "Asia", "Singapore": "Asia", "Malaysia": "Asia", "Thailand": "Asia",
    "Indonesia": "Asia", "Philippines": "Asia", "Vietnam": "Asia",
    "Hong Kong": "Asia", "Taiwan": "Asia", "Israel": "Asia",
    "United Arab Emirates": "Asia", "Saudi Arabia": "Asia", "Qatar": "Asia",
    "Kuwait": "Asia", "Turkey": "Asia",
    "Australia": "Oceania", "New Zealand": "Oceania",
    "South Africa": "Africa", "Egypt": "Africa", "Morocco": "Africa", "Nigeria": "Africa",
}


def normalise_country(label: str) -> str:
    """Reduce an XBRL member label to a country name.

    Issuers write members as prose — AZO tags 'Stores in the United States
    Including Puerto Rico'. Taken literally that is not a country and rolls up
    to 'Other', which put 6,168 US stores outside North America.
    """
    text = " ".join(str(label).split())
    if text in CONTINENT_BY_COUNTRY:
        return text
    lowered = text.lower()
    # Longest name first so 'United States' is not shadowed by a shorter match.
    for country in sorted(CONTINENT_BY_COUNTRY, key=len, reverse=True):
        if len(country) < 4:
            continue
        if re.search(rf"\b{re.escape(country.lower())}\b", lowered):
            return country
    return text


def continent_of(country: str) -> str:
    return CONTINENT_BY_COUNTRY.get(normalise_country(country), "Other")


def roll_up_continents(by_country: Dict[str, int]) -> Dict[str, int]:
    totals: Dict[str, int] = {}
    for country, count in by_country.items():
        totals[continent_of(country)] = totals.get(continent_of(country), 0) + count
    return totals


_COUNTRY_AXIS = re.compile(r"(?i)geograph|country|countries|region")
_STATE_MEMBER = re.compile(r"(?i)^(?:stpr:|state\s+of\s)")


# ── L1: XBRL facts already in our database ───────────────────────────────────

_XBRL_QUERY = """
    SELECT ticker, report_fiscal_year, numeric_value, concept,
           is_dimensioned, dimension, member, dimension_member_label,
           full_dimension_label, period_instant, period_end, filing_accession
    FROM coreiq_filing_metrics_v5
    WHERE ticker = %s
      AND concept IN ({placeholders})
      AND numeric_value IS NOT NULL
      AND report_fiscal_year IS NOT NULL
"""


def xbrl_rows_for(cursor, ticker: str) -> List[Dict[str, Any]]:
    """Every XBRL store-count fact we hold for one ticker. Indexed on ticker,
    so this is a few milliseconds — no network, no EDGAR."""
    query = _XBRL_QUERY.format(
        placeholders=",".join(["%s"] * len(XBRL_CONCEPTS)))
    cursor.execute(query, (ticker, *XBRL_CONCEPTS))
    return list(cursor.fetchall())


def xbrl_by_year(rows: List[Dict[str, Any]]) -> Dict[int, StoreCount]:
    """Resolve XBRL facts into one total per fiscal year, plus geography.

    Preference, matching how issuers tag:
      1. the undimensioned fact — the consolidated total itself
      2. the sum of geographic members (countries are disjoint); state-level
         members are ignored when a country member is present, or the fleet
         would be counted twice
      3. the largest single-axis sum (banner or segment splits each total the
         company independently, so partial tagging under-counts)
    """
    per_year: Dict[int, StoreCount] = {}
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["report_fiscal_year"]), []).append(row)

    for year, facts in grouped.items():
        plain = [f for f in facts
                 if not f["is_dimensioned"] and 0 < f["numeric_value"] <= MAX_PLAUSIBLE_COUNT]
        geographic = [f for f in facts
                      if f["is_dimensioned"]
                      and _COUNTRY_AXIS.search(str(f.get("full_dimension_label") or
                                                   f.get("dimension") or ""))
                      and 0 < f["numeric_value"] <= MAX_PLAUSIBLE_COUNT]

        by_country: Dict[str, int] = {}
        for fact in geographic:
            label = str(fact.get("dimension_member_label") or fact.get("member") or "").strip()
            if not label or _STATE_MEMBER.search(label):
                continue
            by_country[label] = max(int(fact["numeric_value"]), by_country.get(label, 0))

        # Only two shapes are trustworthy. Summing an arbitrary axis is NOT:
        # AutoNation tags a count per franchise brand, and adding those gave 969
        # against a true fleet of 247 — the members overlap or describe
        # something other than sites. When neither shape is available we return
        # nothing and let L2 read the filing instead of inventing a total.
        value, rule = None, None
        if plain:
            value = int(max(f["numeric_value"] for f in plain))
            rule = "L1_xbrl"
        elif by_country:
            value = sum(by_country.values())
            rule = "L1_xbrl_geo_sum"

        if value is None:
            continue
        as_of = next((f["period_instant"] or f["period_end"] for f in facts
                      if f.get("period_instant") or f.get("period_end")), None)
        per_year[year] = StoreCount(
            ticker=str(facts[0]["ticker"]), fiscal_year=year, value=value,
            source_rule=rule, confidence="high",
            evidence=f"XBRL {facts[0]['concept']} ({len(facts)} facts)",
            fye_date=str(as_of) if as_of else None,
            accession=facts[0].get("filing_accession"),
            by_geography=by_country,
        )
    return per_year


# ── L2: the filing itself ────────────────────────────────────────────────────

def load_filing_text(container_client, blob_name: str) -> str:
    raw = container_client.get_blob_client(blob_name).download_blob().readall()
    return filing_to_text(raw.decode("utf-8", "ignore"))


_COUNTRY_ROW = re.compile(
    r"(?im)^\s*\|?\s*([A-Z][A-Za-z\.\s&'\-]{2,28})\s*\|\s*([\d,]{1,6})\s*(?:\||$)")


def geography_from_filing(text: str, total: Optional[int]) -> Dict[str, int]:
    """Country rows from the filing's store table.

    Only accepted when the rows reconcile to the worldwide total within 2% —
    a partial split displayed as if complete is worse than no split at all.
    """
    counts: Dict[str, int] = {}
    for match in _COUNTRY_ROW.finditer(front_section(text)):
        name = " ".join(match.group(1).split())
        if name.lower().startswith(("total", "item", "net", "gross", "fiscal")):
            continue
        if name not in CONTINENT_BY_COUNTRY:
            continue
        value = int(match.group(2).replace(",", ""))
        if 0 < value <= MAX_PLAUSIBLE_COUNT:
            counts[name] = max(value, counts.get(name, 0))
    if not counts or not total:
        return {}
    if abs(sum(counts.values()) - total) / max(total, 1) > 0.02:
        return {}
    return counts


# ── L3: the model, on a slice ────────────────────────────────────────────────

LLM_MODEL = "gpt-4o-mini"
LLM_SYSTEM = (
    "You read SEC 10-K filings and report the total number of retail units the "
    "company OPERATED at its fiscal year end. Return only JSON."
)
_WINDOW_CHARS = 1400


def llm_windows(text: str, limit: int = 3) -> str:
    """The few passages worth paying for.

    Keeps only places where a unit word, a totalling cue and a number appear
    together, densest first. Measured at ~1,381 tokens versus 106,492 for the
    whole filing.
    """
    body = front_section(text)
    cue = re.compile(r"(?i)\b(total|as of|we operated|we had|number of|at year end)\b")
    unit = re.compile(rf"(?i)\b{UNIT_RE}\b")
    spans: List[Tuple[int, int]] = []
    for match in unit.finditer(body):
        start, end = max(0, match.start() - 700), min(len(body), match.start() + 700)
        chunk = body[start:end]
        if cue.search(chunk) and re.search(r"\b\d[\d,]{1,6}\b", chunk):
            spans.append((start, end))
    merged: List[List[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    merged.sort(key=lambda s: -len(unit.findall(body[s[0]:s[1]])))
    return "\n---\n".join(body[s:min(e, s + _WINDOW_CHARS)] for s, e in merged[:limit])


def ask_llm(client, ticker: str, fiscal_year: int, fye_date: Optional[str],
            windows: str) -> Tuple[Optional[Dict[str, Any]], int, int]:
    """One call. Returns (parsed, prompt_tokens, completion_tokens)."""
    prompt = f"""Company: {ticker}. Fiscal year: {fiscal_year}.
{f"Fiscal year ends: {fye_date}." if fye_date else ""}

From the excerpts below, report the TOTAL number of retail units the company
operated worldwide as of its fiscal year end.

Rules:
- Count retail units only: stores, warehouses, clubs, restaurants, shacks,
  galleries, dealerships. NEVER distribution centres, offices, plants or
  franchise agreements.
- If the excerpt gives a breakdown that sums to a total, report the total.
- Use only a number that literally appears in the excerpts, or the exact sum of
  numbers that appear. Never estimate or round.
- If the excerpts do not state it, return found=false.

Excerpts:
---
{windows}
---

JSON: {{"found": true|false, "value": <int|null>,
"quote": "<the exact sentence or table row the number came from>",
"unit_word": "<stores|warehouses|...|null>"}}"""

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": LLM_SYSTEM},
                  {"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=220,
        response_format={"type": "json_object"},
    )
    usage = response.usage
    try:
        parsed = json.loads(response.choices[0].message.content)
    except Exception:
        parsed = None
    return parsed, usage.prompt_tokens, usage.completion_tokens


def llm_call_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """gpt-4o-mini list price, confirmed against the July invoice."""
    return prompt_tokens * 0.15 / 1e6 + completion_tokens * 0.60 / 1e6
