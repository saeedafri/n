#!/usr/bin/env python3
"""Turn the data team's geographic hierarchy workbook into app/data/geo_hierarchy.json.

The workbook (data/geo/MDP_Geographic_Hierarchy-stage1.xlsx) is the source of
truth for *where a place sits*: Region > Sub-region > Country > State/Province >
City. It is owned by the data team and will keep growing, so nothing here is
hand-edited — rerun this script after they send a new copy.

This sits on top of the existing label layer, it does not replace it:

    raw filing label  --segment_aliases.canonicalize_geo_label-->  display name
    display name      --this file's node_index-->                  hierarchy path

Keeping the two apart means the screener keeps showing exactly the names the
business team signed off on, and the hierarchy only decides how those names are
grouped in the cascading filters.

Usage:
    .venv/bin/python scripts/build_geo_hierarchy.py
    .venv/bin/python scripts/build_geo_hierarchy.py --check   # fail if stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from openpyxl import load_workbook

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from data.segment_aliases import (  # noqa: E402  (needs the path insert above)
    canonicalize_geo_label,
    geo_match_key,
    normalize_geo_key,
)

WORKBOOK = REPO / "data" / "geo" / "MDP_Geographic_Hierarchy-stage2.xlsx"
OUTPUT = REPO / "app" / "data" / "geo_hierarchy.json"

# The five levels the screener cascades through, outermost first. The workbook
# carries more levels than this (sub-region groups like NAFTA, components like
# the Nordics, sub-national regions like US Carolinas); they are not their own
# filter step but they still carry a Region/Sub-region/Country, so they sit
# under whichever of these five their path fills in.
LEVELS = ["region", "sub_region", "country", "state", "city"]

PATH_COLUMNS = {
    "region": "Region",
    "sub_region": "Sub-region",
    "country": "Country",
    "state": "State/Province",
    "city": "City",
}

# Labels the workbook has no node for yet. Every one spans places the workbook
# keeps apart, so its own Rule 4 ("labels spanning regions go to a Multi-region
# node, never to one of the regions they span") decides them. Two are ordinary
# places the workbook already has a node for under a different spelling.
# Anything added here should go back to the data team for the next revision.
PENDING_NODES: Dict[str, str] = {
    "Asia and Central America": "Americas and APAC",
    "California and France": "Americas and EMEA",
    "Ireland, Netherlands, and Singapore": "EMEA and APAC",
    "Latin America Europe And Other": "Americas and EMEA",
    "Saudi Arabia and Mexico": "Americas and EMEA",
    "Minneapolis, MN": "Minneapolis",
    "Western Europe Reporting Unit": "Western Europe",
}


def cell(value) -> str:
    return "" if value is None else str(value).strip()


def read_sheet(workbook, name: str) -> List[Dict[str, str]]:
    sheet = workbook[name]
    header = [cell(c) for c in next(sheet.iter_rows(max_row=1, values_only=True))]
    rows = []
    for raw in sheet.iter_rows(min_row=2, values_only=True):
        if not raw or not cell(raw[0]):
            continue
        rows.append({key: cell(val) for key, val in zip(header, raw)})
    return rows


def build_nodes(workbook) -> Dict[str, Dict]:
    """One entry per place, keyed by the node's own name."""
    nodes: Dict[str, Dict] = {}
    for row in read_sheet(workbook, "Hierarchy"):
        name = row["Standard Node"]
        path = {level: row.get(column, "") for level, column in PATH_COLUMNS.items()}
        nodes[name] = {
            "node": name,
            "level": row.get("Level", ""),
            "parent": row.get("Parent Node", ""),
            "iso3": row.get("ISO3", ""),
            "notes": row.get("Definition / Notes", ""),
            **path,
        }
    return nodes


def build_label_index(workbook, nodes: Dict[str, Dict]) -> Dict[str, Dict]:
    """Every spelling the data team recognises -> the node it belongs to.

    Keyed two ways so a label that drifts between filings still lands: the exact
    normalised key and the loose match key.

    Spellings arrive in tiers and a narrower tier is never overruled by a wider
    one. Within a tier a key claimed by two different nodes is dropped rather
    than guessed — "Europe, Middle East and Africa (EMEA)" is the old display
    name of both an EMEA label and an EMEA-and-APAC one, and picking whichever
    row came first filed 71 companies under Multi-region.
    """
    unknown_nodes: List[str] = []

    def tier(pairs: Iterable[Tuple[str, str]]) -> Tuple[Dict[str, set], Dict[str, set]]:
        exact: Dict[str, set] = {}
        loose: Dict[str, set] = {}
        for label, node in pairs:
            if not label:
                continue
            if node not in nodes:
                unknown_nodes.append(f"{label} -> {node}")
                continue
            exact.setdefault(normalize_geo_key(label), set()).add(node)
            loose.setdefault(geo_match_key(label), set()).add(node)
        return exact, loose

    label_rows = read_sheet(workbook, "Label Map")
    tiers = [
        # A node's own name is the strongest claim on a key.
        tier((name, name) for name in nodes),
        tier((row["MDP Label (raw)"], row["Standard Node"]) for row in label_rows),
        # The name the screener shows today, which is what the filters receive.
        tier((canonicalize_geo_label(row["MDP Label (raw)"]), row["Standard Node"])
             for row in label_rows),
        tier((row.get("Previous MDP Name", ""), row["Standard Node"]) for row in label_rows),
        tier(PENDING_NODES.items()),
    ]

    exact: Dict[str, str] = {}
    loose: Dict[str, str] = {}
    ambiguous: List[str] = []
    for tier_exact, tier_loose in tiers:
        for index, claims in ((exact, tier_exact), (loose, tier_loose)):
            for key, owners in claims.items():
                if key in index:
                    continue
                if len(owners) == 1:
                    index[key] = next(iter(owners))
                else:
                    ambiguous.append(f"{key} -> {sorted(owners)}")

    return {
        "exact": exact,
        "loose": loose,
        "unknown_nodes": sorted(set(unknown_nodes)),
        "ambiguous": sorted(set(ambiguous)),
    }


def build_excluded(workbook) -> Dict[str, str]:
    """Labels the workbook says are not places at all."""
    excluded = {}
    for row in read_sheet(workbook, "Excluded"):
        excluded[normalize_geo_key(row["MDP Label (raw)"])] = row.get("Reason", "")
    return excluded


def build_tree(nodes: Dict[str, Dict]) -> Dict[str, Dict[str, List[str]]]:
    """Parent value -> child values, one map per level step.

    This is what the UI cascades over: pick a region, read tree['region'][region]
    to get its sub-regions, and so on. Built from the path columns rather than
    Parent Node so that a node which skips a level (NAFTA has a region but no
    sub-region) still appears under the level it does fill in.
    """
    tree: Dict[str, Dict[str, set]] = {level: {} for level in LEVELS[:-1]}
    for node in nodes.values():
        for outer, inner in zip(LEVELS, LEVELS[1:]):
            parent, child = node.get(outer, ""), node.get(inner, "")
            if parent and child:
                tree[outer].setdefault(parent, set()).add(child)
    return {
        level: {parent: sorted(children) for parent, children in sorted(parents.items())}
        for level, parents in tree.items()
    }


def build(workbook_path: Path) -> Dict:
    workbook = load_workbook(workbook_path, data_only=True)
    nodes = build_nodes(workbook)
    labels = build_label_index(workbook, nodes)
    if labels["unknown_nodes"]:
        raise SystemExit(
            "Label Map points at nodes the Hierarchy sheet does not define:\n  "
            + "\n  ".join(labels["unknown_nodes"][:20])
        )
    return {
        "_ambiguous_keys": labels["ambiguous"],
        "_source": workbook_path.name,
        "_note": (
            "Generated file — do not hand-edit. The data team owns the workbook; "
            "rerun scripts/build_geo_hierarchy.py after they revise it."
        ),
        "_generator": "scripts/build_geo_hierarchy.py",
        "_levels": LEVELS,
        "_pending_nodes": PENDING_NODES,
        "nodes": nodes,
        "exact": labels["exact"],
        "loose": labels["loose"],
        "excluded": build_excluded(workbook),
        "tree": build_tree(nodes),
    }


def coverage_report(payload: Dict, labels: List[str]) -> Dict[str, List[str]]:
    """Which of `labels` the hierarchy can place. Used by the tests and --check."""
    placed, missing = [], []
    for label in labels:
        key = normalize_geo_key(label)
        if key in payload["excluded"] or key in payload["exact"]:
            placed.append(label)
        elif geo_match_key(label) in payload["loose"]:
            placed.append(label)
        elif normalize_geo_key(canonicalize_geo_label(label)) in payload["exact"]:
            placed.append(label)
        else:
            missing.append(label)
    return {"placed": placed, "missing": missing}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, default=WORKBOOK)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument(
        "--check", action="store_true",
        help="exit non-zero if the committed JSON is not what the workbook produces",
    )
    args = parser.parse_args()

    payload = build(args.workbook)
    rendered = json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True)

    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current.strip() != rendered.strip():
            raise SystemExit(f"{args.out} is stale — rerun {parser.prog}")
        print(f"{args.out.name} is up to date")
        return

    args.out.write_text(rendered + "\n", encoding="utf-8")
    levels = {}
    for node in payload["nodes"].values():
        levels[node["level"]] = levels.get(node["level"], 0) + 1
    try:
        shown = args.out.relative_to(REPO)
    except ValueError:
        shown = args.out
    print(f"wrote {shown}")
    print(f"  nodes          {len(payload['nodes'])}")
    print(f"  label keys     {len(payload['exact'])} exact / {len(payload['loose'])} loose")
    print(f"  excluded       {len(payload['excluded'])}")
    for level, count in sorted(levels.items(), key=lambda kv: -kv[1]):
        print(f"    {level:22} {count}")


if __name__ == "__main__":
    main()
