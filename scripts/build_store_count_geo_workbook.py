#!/usr/bin/env python3
"""Everything MDP holds about WHERE a store is, as one workbook for the UI team.

They asked for the geographic filters the Geographical Segments criterion now
has — Region > Sub-region > Country > State > City — on Store Count too. Store
count geography is a different pipeline with a thinner and messier base, and
this workbook is the honest picture of it so they can decide what to build.

The staging database is the source of truth. The disk cache under --sbc is only
one machine's copy of what the Additional Data tab last rendered, so it is
reported separately and never as the headline number.

    .venv/bin/python scripts/build_store_count_geo_workbook.py \
        --facts /tmp/sc_facts.json --out ~/Downloads/MDP_Store_Count_Geography.xlsx

--facts is the JSON from the per-ticker sweep of coreiq_filing_metrics_v5 (that
table is 14.6M rows with no index on `concept`, so a WHERE concept IN (...) is a
full scan that never returns; the sweep walks the indexed `ticker` instead).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "scripts"))

from data.repository import CONTINENT_ORDER, COUNTRY_TO_CONTINENT, continent_of  # noqa: E402
from data.store_count_extractor import XBRL_CONCEPTS  # noqa: E402
from data.store_count_sources import CONTINENT_BY_COUNTRY, normalise_country  # noqa: E402
from store_count_geo_labels import LEVELS, classify  # noqa: E402

HEADER = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
ASK = PatternFill("solid", fgColor="FFF2CC")      # columns the data team fills in
TITLE = Font(bold=True, size=12, color="1F3864")


def sheet(workbook, title, columns, rows, ask_from=None, widths=None):
    ws = workbook.create_sheet(title)
    ws.append(columns)
    for index in range(1, len(columns) + 1):
        cell = ws.cell(row=1, column=index)
        cell.fill, cell.font = HEADER, HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append(row)
    if ask_from is not None:
        for column in range(ask_from, len(columns) + 1):
            for row in range(2, ws.max_row + 1):
                ws.cell(row=row, column=column).fill = ASK
    for index, column in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(index)].width = \
            (widths or {}).get(column, max(12, min(46, len(str(column)) + 6)))
    ws.freeze_panes = "A2"
    return ws


def prose(workbook, title, lines):
    ws = workbook.create_sheet(title)
    for text, bold in lines:
        cell = ws.cell(row=ws.max_row + 1, column=1, value=text)
        if bold:
            cell.font = TITLE
    ws.column_dimensions["A"].width = 100
    return ws


def alias_key(name: str) -> str:
    """Two spellings of one place share this key.

    The data holds both "U.S. Virgin Islands" and "United States Virgin
    Islands" from different filers, which a dropdown would show as two rows
    that never add up. Surfacing that is half the point of this workbook.
    """
    text = name.lower().replace(".", "").replace(",", "").replace("&", "and")
    text = text.replace("u s ", "united states ").replace("us ", "united states ")
    if text.startswith("trinidad"):
        text = "trinidad and tobago"
    return " ".join(text.split())


def read_sbc(directory: Path):
    """What the Additional Data tab last rendered, per the disk cache."""
    found = defaultdict(lambda: {"tickers": set(), "years": set()})
    with_data, without = [], []
    if not directory.exists():
        return found, with_data, without
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        countries = (payload.get("data", payload) or {}).get("countries") or {}
        (with_data if countries else without).append(path.stem.upper())
        for name, per_year in countries.items():
            found[name]["tickers"].add(path.stem.upper())
            found[name]["years"].update(str(year) for year in per_year)
    return found, with_data, without


def read_facts(path: Path):
    """Every store-count fact from the sweep, and the geographic subset."""
    if not path or not path.exists():
        return [], [], Counter(), {}, {}
    facts = json.loads(path.read_text())
    geographic = [
        fact for fact in facts
        if str(fact.get("is_dimensioned")) in ("1", "True")
        and any(word in str(fact.get("full_dimension_label") or fact.get("dimension") or "").lower()
                for word in ("geograph", "countr", "region"))
    ]
    seen, tickers, years = Counter(), defaultdict(set), defaultdict(set)
    for fact in geographic:
        label = str(fact.get("dimension_member_label") or fact.get("member") or "").strip()
        if not label:
            continue
        seen[label] += 1
        tickers[label].add(fact.get("ticker"))
        year = fact.get("report_fiscal_year") or fact.get("fiscal_year")
        if year:
            years[label].add(str(year))
    return facts, geographic, seen, tickers, years


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=Path, default=None)
    parser.add_argument("--sbc", type=Path,
                        default=REPO / "data" / "edgar_cache" / "stores_by_country")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    cached, tickers_with, tickers_without = read_sbc(args.sbc)
    facts, geographic, seen, member_tickers, member_years = read_facts(args.facts)

    known = lambda name: (normalise_country(name) in COUNTRY_TO_CONTINENT
                          or normalise_country(name) in CONTINENT_BY_COUNTRY)
    member_level = {label: classify(label, known) for label in seen}
    levels = Counter(level for level, _ in member_level.values())
    geo_tickers = {f["ticker"] for f in geographic}
    all_tickers = {f["ticker"] for f in facts}

    aliases = defaultdict(list)
    for label in seen:
        aliases[alias_key(label)].append(label)
    duplicates = {key: names for key, names in aliases.items() if len(names) > 1}

    workbook = Workbook()
    workbook.remove(workbook.active)

    prose(workbook, "Read me", [
        ("MDP — Store Count geography", True),
        ("", False),
        ("What this is", True),
        ("Every geographic label MDP holds a store count against, read from the", False),
        ("staging database — not from any one machine's cache.", False),
        ("Prepared because the UI team asked for the Geographical Segments filters", False),
        ("(Region > Sub-region > Country > State > City) on Store Count as well.", False),
        ("", False),
        ("Where store counts come from", True),
        ("SEC 10-K filings. Not a vendor feed, not a table anyone maintains by hand.", False),
        ("Four XBRL concepts: " + ", ".join(XBRL_CONCEPTS), False),
        ("A geographic number exists only when the filer tagged one of those concepts", False),
        ("against a geographic axis in their own 10-K. Most filers publish one fleet", False),
        ("total and nothing else, so most companies have no geographic split at all.", False),
        ("", False),
        ("What MDP groups them into today", True),
        ("Country, then one of seven display buckets:", False),
        ("  " + " / ".join(CONTINENT_ORDER), False),
        ("There is no Region, no Sub-region, no State and no City filter today —", False),
        ("although State and City data partly exists. See 'What the labels are'.", False),
        ("", False),
        ("What we need from you", True),
        ("The yellow columns on 'Every label as filed': which level each label belongs", False),
        ("to, and what it maps to — using the same vocabulary as the geographic", False),
        ("hierarchy workbook, so both screens can share one hierarchy.", False),
        ("", False),
        ("Please read 'Our recommendation' and 'Coverage' first.", False),
    ])

    prose(workbook, "Our recommendation", [
        ("What we suggest building, and why", True),
        ("", False),
        ("1. Country, then State. Not five levels.", True),
        ("   Store counts carry Country and US State well, City for only a handful of", False),
        ("   filers, and very little Region. A five-level cascade copied from", False),
        ("   Geographical Segments would show three dropdowns that are empty nearly", False),
        ("   every time. Two levels that are always populated beat five that are not.", False),
        ("", False),
        ("2. Keep the seven display buckets as the top level.", True),
        ("   " + " / ".join(CONTINENT_ORDER), False),
        ("   They already exist, users already see them on the Additional Data tab, and", False),
        ("   they need no new data. Renaming them to match the segment Regions (AMER,", False),
        ("   APAC, EMEA) is a data-team decision, not a UI one.", False),
        ("", False),
        ("3. Carry over the 'include broader' rule from segments.", True),
        ("   Filers tag 'Europe' and 'Latin America' as if they were countries. Without", False),
        ("   that rule, filtering to Spain silently drops a company that only reported", False),
        ("   Europe — the same trap the segments filter already solves.", False),
        ("", False),
        ("4. Fix these before shipping any filter.", True),
        (f"   a. {len(duplicates)} places are spelled more than one way and would each become", False),
        ("      two or three rows in a dropdown that never add up. Mostly caps versus", False),
        ("      title case — 'UNITED STATES' vs 'United States' vs 'United states' —", False),
        ("      but also 'U.S. Virgin Islands' vs 'United States Virgin Islands'.", False),
        ("      Every one is marked in the 'Our note' column.", False),
        ("   c. Filer typos are in the data: 'Tennesse', 'Orange Country' (County),", False),
        ("      'Columbia' (Colombia). They need a decision, not a guess from us.", False),
        ("   d. Non-places sit on the geographic axis: 'Franchise', 'Class Action", False),
        ("      Lawsuit', 'Refranchising (gain) loss'. Filer tagging errors — they", False),
        ("      should never reach a dropdown.", False),
        ("", False),
        ("5. Set expectations on coverage.", True),
        (f"   Only {len(geo_tickers)} of {len(all_tickers)} companies that report a store count split it", False),
        ("   by geography at all. A geographic filter will look empty for most of the", False),
        ("   universe, and that is the source data, not a bug. See Coverage.", False),
        ("", False),
        ("6. Share one hierarchy with Geographical Segments.", True),
        ("   If both screens use the same Region and Sub-region vocabulary, a user who", False),
        ("   learns one already knows the other, and the data team maintains one", False),
        ("   mapping instead of two. That is what the yellow columns are for.", False),
    ])

    sheet(workbook, "Coverage",
          ["Measure", "Count", "What it means"],
          [
              ["FROM THE STAGING DATABASE", "", "coreiq_filing_metrics_v5, swept ticker by ticker"],
              ["Companies with any store-count fact", len(all_tickers),
               "Filed one of the four XBRL store-count concepts"],
              ["...that split it by geography", len(geo_tickers),
               "Tagged against a geographic axis — the only ones a filter can reach"],
              ["Store-count facts held", len(facts), "All years, all companies"],
              ["...of those, on a geographic axis", len(geographic), ""],
              ["Distinct geographic labels", len(seen),
               "Exactly as the filers wrote them — see 'Every label as filed'"],
              ["", "", ""],
              ["WHAT THE TAB RENDERS TODAY", "",
               f"Disk cache '{args.sbc.name}' — one machine's copy, not the whole picture"],
              ["Companies looked up", len(tickers_with) + len(tickers_without), ""],
              ["...with a country breakdown", len(tickers_with), ", ".join(sorted(tickers_with))],
              ["...with none", len(tickers_without), "One fleet total only"],
              ["Clean country names shown", len(cached),
               "After mapping the filer's label to an ISO country"],
              ["", "", ""],
              ["THE GROUPING IN CODE", "", ""],
              ["Display buckets", len(CONTINENT_ORDER), " / ".join(CONTINENT_ORDER)],
              ["Countries in the built-in map", len(COUNTRY_TO_CONTINENT),
               "app/data/repository.py — anything outside it shows as 'Other'"],
          ],
          widths={"Measure": 40, "Count": 10, "What it means": 88})

    sheet(workbook, "What the labels are",
          ["Kind of label", "Distinct labels", "Facts", "What it means for a filter"],
          [[level, levels.get(level, 0),
            sum(count for label, count in seen.items() if member_level[label][0] == level),
            meaning]
           for level, meaning in [
               ("Country", "Usable as a Country filter straight away"),
               ("US state", "State-level data EXISTS — a State filter is possible"),
               ("City / area", "City data exists, but for only a handful of filers"),
               ("Region (multi-country)", "Spans countries; needs the 'include broader' rule"),
               ("Not a place", "A brand or franchise term the filer put on the geographic axis"),
               ("Unclassified", "We cannot tell — please decide"),
           ]],
          widths={"Kind of label": 26, "Distinct labels": 16, "Facts": 10,
                  "What it means for a filter": 66})

    # Every label, nothing hidden — this is the sheet they fill in.
    rows = []
    for label, count in seen.most_common():
        level, note = member_level[label]
        twins = [other for other in aliases[alias_key(label)] if other != label]
        if twins:
            note = (note + "; " if note else "") + f"SAME PLACE as {', '.join(sorted(twins))}"
        rows.append([label, level, count, len(member_tickers[label]),
                     ", ".join(sorted(t for t in member_tickers[label] if t)),
                     ", ".join(sorted(member_years[label])), "", "", "", note])
    sheet(workbook, "Every label as filed",
          ["Member label exactly as filed", "What it looks like to us", "Facts",
           "Companies", "Which companies", "Years",
           "Level it belongs to (please fill)", "Maps to (please fill)",
           "Region (please fill)", "Our note"],
          rows, ask_from=7,
          widths={"Member label exactly as filed": 54, "What it looks like to us": 22,
                  "Which companies": 34, "Years": 26,
                  "Level it belongs to (please fill)": 26, "Maps to (please fill)": 26,
                  "Region (please fill)": 22, "Our note": 46})

    # What the tab renders today, kept separate and clearly labelled.
    cache_aliases = defaultdict(list)
    for name in cached:
        cache_aliases[alias_key(name)].append(name)
    sheet(workbook, "Countries the tab shows",
          ["Country as shown", "Display bucket", "In the continent map?",
           "Companies", "Which companies", "Years", "Our note"],
          [[name, continent_of(name),
            "yes" if name in COUNTRY_TO_CONTINENT else "NO — falls into 'Other'",
            len(cached[name]["tickers"]), ", ".join(sorted(cached[name]["tickers"])),
            ", ".join(sorted(cached[name]["years"])),
            ("SAME PLACE as " + ", ".join(sorted(o for o in cache_aliases[alias_key(name)] if o != name)))
            if len(cache_aliases[alias_key(name)]) > 1 else ""]
           for name in sorted(cached)],
          widths={"Country as shown": 32, "Display bucket": 22, "In the continent map?": 22,
                  "Which companies": 36, "Years": 28, "Our note": 46})

    sheet(workbook, "Grouping today",
          ["Country", "Display bucket", "Does MDP hold store counts for it?"],
          [[country, bucket, "yes" if country in cached else "not yet"]
           for country, bucket in sorted(COUNTRY_TO_CONTINENT.items(),
                                         key=lambda kv: (CONTINENT_ORDER.index(kv[1]), kv[0]))],
          widths={"Country": 32, "Display bucket": 24,
                  "Does MDP hold store counts for it?": 34})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(args.out)
    print(f"wrote {args.out}")
    print(f"  database    {len(facts)} facts / {len(all_tickers)} companies; "
          f"{len(geographic)} geo facts / {len(geo_tickers)} companies")
    print(f"  labels      {len(seen)} distinct")
    for level in LEVELS:
        if levels.get(level):
            print(f"    {level:24} {levels[level]:>4}")
    print(f"  tab cache   {len(tickers_with)} with a split / {len(tickers_without)} without, "
          f"{len(cached)} clean countries")
    print(f"  duplicates  {len(duplicates)} places spelled more than one way")


if __name__ == "__main__":
    main()
