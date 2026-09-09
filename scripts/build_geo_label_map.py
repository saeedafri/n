#!/usr/bin/env python3
"""Regenerate app/data/geo_label_map.json from the business team's mapping workbook.

The workbook ("Countries Mapping.xlsx") is the business team's answer to
"what should each raw XBRL geographic segment label be called?".  It has four
sheets; three of them drive the app:

  Mapping  MDP Label -> Mapping Name.  The Mapping Name column is filled only on
           the first row of each group (Excel-style visual grouping), so it is
           forward-filled: every following blank row belongs to the canonical
           above it.
  remove   Labels that are not geographies at all (business segments, facility
           descriptions, tax categories, scraper artifacts).  Action "Remove"
           hides them from the screener; "Decode -> keep" leaves them visible.
  codes    Canonical Name -> ISO 3166 alpha-3.  Carried through for reference.

  states   Canonical Country + ISO for labels that roll UP to a country
           (California -> United States).  NOT used: it contradicts the Mapping
           sheet on purpose ("North" is United States there, North America in
           Mapping) and applying it would collapse every US state into one
           option, which the Mapping sheet deliberately does not do.

Usage
    .venv/bin/python scripts/build_geo_label_map.py                    # regenerate
    .venv/bin/python scripts/build_geo_label_map.py --report out.xlsx  # unmapped report

The report mode lists every geographic member label live in the screening member
cache that the workbook does not name, newest coverage first, so the business
team can extend the workbook as new filings arrive.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

DEFAULT_WORKBOOK = Path.home() / "Downloads" / "Countries Mapping.xlsx"
OUTPUT = REPO / "app" / "data" / "geo_label_map.json"

# Labels the workbook does not name, added here with the business team's own
# reasoning applied one step further.  Every entry says why, so the business
# team can audit or overrule it in the next workbook revision.
MANUAL_CANONICALS: Dict[str, Tuple[str, str]] = {
    # remove sheet, "Decode -> keep": the acronym is opaque in the dropdown and
    # the sheet spells out what it means.  Only the unambiguous ones are here —
    # "Mci" ("needs confirmation") and "JAPA" ("likely") are left untouched.
    "Apj": ("Asia Pacific & Japan (APJ)", "remove sheet: decode -> keep"),
    "Apjc": ("Asia Pacific, Japan & China (APJC)", "remove sheet: decode -> keep"),
    "Apla": ("Asia Pacific & Latin America (APLA)", "remove sheet: decode -> keep"),
    "Eame": ("Europe, Asia & Middle East (EAME)", "remove sheet: decode -> keep"),
    "Laap": ("Latin America & Asia Pacific (LAAP)", "remove sheet: decode -> keep"),
    # LACC decodes to Latin America & Caribbean, which the workbook already
    # names — reuse its name rather than coining a near-duplicate. A new name
    # here would not survive canonicalisation: its loose key is the workbook
    # entry's, so canonicalize() would rename it straight back and the stored
    # and displayed names would disagree (tests/test_geo_label_map.py pins this).
    "Lacc": ("Latin America and the Caribbean", "remove sheet: decode -> keep"),
    "LACC Geographic Region": ("Latin America and the Caribbean",
                               "same segment as Lacc, axis suffix only"),
    "Cis": ("Commonwealth of Independent States (CIS)", "remove sheet: decode -> keep"),
    "Almea": ("Asia, Middle East & Africa (AMEA)", "remove sheet: decode -> keep"),
    "Amea": ("Asia, Middle East & Africa (AMEA)", "remove sheet: decode -> keep"),
    # Namespace-prefix artifact, exactly like the workbook's own "pf0:US".
    "pf0:CA": ("Canada", "same artifact shape as workbook's pf0:US -> United States"),
}

# Not geographies.  Extends the remove sheet's own rulings to variants of the
# same disclosures that the workbook did not list individually.
MANUAL_REMOVED: Dict[str, str] = {
    # Pension/benefit disclosures filed on the geographic axis.  The workbook's
    # remove sheet removes tax categories for the same reason; these are the
    # pension equivalents (HON, MDLZ, DBD file them on the geo axis).
    "Pension Benefits - U.S.": "pension disclosure, not geographic revenue",
    "U.S. Pension Benefits": "pension disclosure, not geographic revenue",
    "U.S. pensions": "pension disclosure, not geographic revenue",
    # Texas Instruments facility descriptions.  The remove sheet removes four of
    # these by name; these are the same disclosure for other sites.
    "Semiconductor manufacturing facilities in Hiji, Japan, and Houston, Texas":
        "facility description (remove sheet removes the Houston/Plano variants)",
    "Semiconductor manufacturing facility in Nice, France":
        "facility description (remove sheet removes the Houston/Plano variants)",
    "Site in Nice, France":
        "facility description (remove sheet removes the Houston/Plano variants)",
    "Factories in Houston, Texas, and Hiji, Japan":
        "facility description (remove sheet removes the Houston/Plano variants)",
}


def load_workbook_sheets(path: Path):
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)

    # Mapping — forward-fill the canonical down each visual group.
    mapping: Dict[str, str] = {}
    canonical = None
    for row in wb["Mapping"].iter_rows(min_row=2, values_only=True):
        raw, name = row[0], row[1]
        if raw is None:
            continue
        if name:
            canonical = str(name).strip()
        if canonical:
            mapping[str(raw).strip()] = canonical

    removed: Dict[str, str] = {}
    decoded: List[Tuple[str, str]] = []
    for row in wb["remove"].iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        label = str(row[0]).strip()
        action = str(row[2] or "").strip().lower()
        reason = str(row[3] or "").strip()
        if action.startswith("remove"):
            removed[label] = reason
        else:
            decoded.append((label, reason))

    iso_codes: Dict[str, str] = {}
    for row in wb["codes"].iter_rows(min_row=2, values_only=True):
        if row[0] and row[1]:
            iso_codes[str(row[0]).strip()] = str(row[1]).strip()

    return mapping, removed, decoded, iso_codes


def build(path: Path) -> dict:
    from data.segment_aliases import geo_match_key, normalize_geo_key

    mapping, removed, decoded, iso_codes = load_workbook_sheets(path)

    for label, (canonical, _why) in MANUAL_CANONICALS.items():
        mapping[label] = canonical
    removed.update(MANUAL_REMOVED)

    # The remove sheet wins where both sheets name the same label: it carries a
    # specific per-filer reason ("tax category, Micron"), while the Mapping
    # sheet's entry is a bulk alphabetical assignment.
    removed_keys = {normalize_geo_key(label) for label in removed}
    conflicts = sorted(
        label for label in mapping if normalize_geo_key(label) in removed_keys
    )

    exact: Dict[str, str] = {}
    for raw, canonical in mapping.items():
        key = normalize_geo_key(raw)
        if key and key not in removed_keys:
            exact[key] = canonical

    # Loose keys resolve spelling drift the workbook never saw ("Asia Pacific,
    # Other" for "Asia Pacific and other").  A loose key that two different
    # canonicals claim is ambiguous and dropped rather than guessed.
    loose_claims: Dict[str, set] = defaultdict(set)
    for raw, canonical in mapping.items():
        if normalize_geo_key(raw) in removed_keys:
            continue
        loose_claims[geo_match_key(raw)].add(canonical)
    # A loose key that is also an exact key stays: "U.S. total" loosens to "us",
    # which is a workbook label in its own right, and dropping the entry would
    # leave the label unresolved.
    loose = {
        key: next(iter(names))
        for key, names in sorted(loose_claims.items())
        if key and len(names) == 1
    }
    ambiguous = sorted(key for key, names in loose_claims.items() if len(names) > 1)

    return {
        "_source": path.name,
        "_generated": date.today().isoformat(),
        "_generator": "scripts/build_geo_label_map.py",
        "_note": (
            "Generated file — do not hand-edit. Business team owns "
            f"{path.name}; rerun the generator after they revise it."
        ),
        "_counts": {
            "workbook_labels": len(mapping),
            "canonicals": len(set(mapping.values())),
            "exact_keys": len(exact),
            "loose_keys": len(loose),
            "removed": len(removed),
        },
        "_conflicts_remove_wins": conflicts,
        "_ambiguous_loose_keys": ambiguous,
        "_decode_keep_untouched": [label for label, _ in decoded
                                   if label not in MANUAL_CANONICALS],
        "exact": exact,
        "loose": loose,
        "removed": sorted({normalize_geo_key(label) for label in removed} - {""}),
        "removed_reasons": {normalize_geo_key(k): v for k, v in removed.items()},
        "iso_codes": iso_codes,
        "manual_additions": {
            "canonicals": {k: {"canonical": v[0], "reason": v[1]}
                           for k, v in MANUAL_CANONICALS.items()},
            "removed": MANUAL_REMOVED,
        },
    }


def write_report(destination: Path) -> None:
    import openpyxl

    from core.database import db_manager
    from data.segment_aliases import (build_geo_label_groups, is_geo_excluded_label,
                                      is_geo_named_label)

    rows = db_manager.execute_query_readonly(
        "SELECT member_label, company_count FROM coreiq_screening_segment_member_cache "
        "WHERE segment_type = 'geographical' ORDER BY company_count DESC, member_label"
    ) or []
    counts = {r["member_label"]: int(r["company_count"] or 0)
              for r in rows if r.get("member_label")}

    # One row per dropdown option the workbook does not name, largest first, with
    # every raw spelling that currently lands on it so the business team can see
    # what they are naming.
    unmapped = [
        (display, sum(counts.get(m, 0) for m in members),
         "; ".join(m for m in members if m != display))
        for display, members in build_geo_label_groups(counts).items()
        if not is_geo_named_label(display) and not is_geo_excluded_label(display)
    ]
    unmapped.sort(key=lambda row: (-row[1], row[0].lower()))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Unmapped"
    ws.append(["MDP Label", "Mapping Name (please fill)", "Companies",
               "Other spellings already grouped here"])
    for label, count, variants in unmapped:
        ws.append([label, None, count, variants])
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["D"].width = 70
    wb.save(destination)
    print(f"{len(unmapped)} unnamed dropdown options -> {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument("--report", type=Path,
                        help="write an xlsx of live labels the workbook does not name")
    args = parser.parse_args()

    if args.report:
        write_report(args.report)
        return 0

    if not args.workbook.exists():
        print(f"workbook not found: {args.workbook}", file=sys.stderr)
        return 1

    payload = build(args.workbook)
    args.out.write_text(json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=False))
    counts = payload["_counts"]
    try:
        shown = args.out.relative_to(REPO)
    except ValueError:
        shown = args.out          # --out may point outside the repo
    print(f"{shown}: {counts['workbook_labels']} workbook labels -> "
          f"{counts['canonicals']} canonicals "
          f"({counts['exact_keys']} exact + {counts['loose_keys']} loose keys), "
          f"{counts['removed']} removed")
    if payload["_conflicts_remove_wins"]:
        print("  remove sheet overrides Mapping for:", payload["_conflicts_remove_wins"])
    if payload["_ambiguous_loose_keys"]:
        print("  ambiguous loose keys dropped:", payload["_ambiguous_loose_keys"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
