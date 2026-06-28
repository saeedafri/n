#!/usr/bin/env python3
"""Copy user-provided business doc screenshots into v2, annotate, write MANIFEST."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V2 = REPO / "docs/business-documentation/_screenshots/v2"
ANNOTATE = REPO / "scripts/annotate_business_screenshot.py"
PY = sys.executable

# badge_x,badge_y,num,target_x,target_y,label  (capture-script marker format)
USER_SOURCES: dict[str, str] = {
    # Home
    "home-01.png": "user-01.png",
    "home-02-company-selected.png": "user-02.png",
    "home-04-nav-highlight.png": "user-01.png",
    # Newsroom
    "newsroom-01.png": "user-03.png",
    "newsroom-02-filters.png": "user-05.png",
    "newsroom-03-articles.png": "user-09.png",
    "newsroom-04-sort.png": "user-04.png",
    "newsroom-05-category.png": "user-06.png",
    "newsroom-06-watchlist.png": "user-07.png",
    "newsroom-07-date-picker.png": "user-08.png",
    # Screening — main builder
    "screening-01-default.png": "user-10.png",
    "screening-05-watchlists-dropdown.png": "user-11.png",
    "screening-04-watchlist.png": "user-12.png",
    "screening-watchlist-dialog-new.png": "user-13.png",
    "screening-watchlist-dialog-companies.png": "user-14.png",
    "screening-watchlist-dialog-sectors.png": "user-15.png",
    "screening-industry-form.png": "user-16.png",
    "screening-03-industry.png": "user-17.png",
    "screening-geographic.png": "user-18.png",
    "screening-geographic-countries.png": "user-19.png",
    "screening-02-financial.png": "user-20.png",
    "screening-financial-statement-type.png": "user-21.png",
    "screening-financial-statement-dropdown.png": "user-22.png",
    "screening-financial-metric.png": "user-23.png",
    "screening-financial-period-type.png": "user-24.png",
    "screening-financial-year-range.png": "user-25.png",
    "screening-financial-year.png": "user-26.png",
    "screening-financial-operator.png": "user-27.png",
    "screening-financial-value.png": "user-28.png",
    "screening-key-devs-mode.png": "user-29.png",
    "screening-key-devs-categories.png": "user-30.png",
}

CALLOUTS: dict[str, list[str]] = {
    "home-01.png": [
        "80,78,1,1180,83,Navigation",
        "80,745,2,820,745,View by Company",
        "80,745,3,1820,745,View by Sector",
        "80,792,4,1000,792,Company picker",
    ],
    "home-02-company-selected.png": [
        "80,792,1,1000,650,Company dropdown",
        "80,860,2,980,860,View button",
        "80,792,3,1980,792,Sector dropdown",
        "80,860,4,1980,860,Sector View",
    ],
    "home-04-nav-highlight.png": [
        "80,78,1,1180,83,Top navigation",
        "80,130,2,620,83,Market Data Dashboard",
        "80,182,3,1520,83,Screening",
        "80,234,4,1920,83,News",
        "80,286,5,2760,83,Logout",
    ],
    "newsroom-01.png": [
        "80,300,1,400,345,Search box",
        "80,300,2,780,345,Date range",
        "80,300,3,1500,345,Sort and filters",
        "80,650,4,280,750,Search News panel",
    ],
    "newsroom-02-filters.png": [
        "80,300,1,400,345,Search box",
        "80,300,2,780,345,From / To dates",
        "80,300,3,1300,345,Sector dropdown",
        "80,300,4,1700,345,Category dropdown",
        "80,300,5,2100,345,Watchlist dropdown",
    ],
    "newsroom-03-articles.png": [
        "80,300,1,400,345,Search box",
        "80,650,2,280,750,Search News panel",
        "80,900,3,1200,950,Article headline",
        "80,900,4,1200,1100,Sentiment area",
    ],
    "newsroom-04-sort.png": [
        "80,300,1,400,345,Search box",
        "80,380,2,1130,420,Sort dropdown",
        "80,460,3,1500,345,Sector dropdown",
        "80,650,4,1200,950,Article feed",
    ],
    "newsroom-05-category.png": [
        "80,300,1,400,345,Search box",
        "80,380,2,1720,480,Category dropdown",
        "80,460,3,1300,345,Sector dropdown",
        "80,650,4,1200,950,Article feed",
    ],
    "newsroom-06-watchlist.png": [
        "80,300,1,400,345,Search box",
        "80,380,2,2120,480,Watchlist dropdown",
        "80,460,3,1700,345,Category dropdown",
        "80,650,4,1200,950,Article feed",
    ],
    "newsroom-07-date-picker.png": [
        "80,300,1,780,345,From date",
        "80,500,2,800,720,Calendar picker",
        "80,380,3,1050,345,To date",
        "80,650,4,1200,950,Article feed",
    ],
    "screening-01-default.png": [
        "80,300,1,350,343,Screen For row",
        "80,420,2,700,420,Watchlists dropdown",
        "80,900,3,350,1350,Show Results",
    ],
    "screening-05-watchlists-dropdown.png": [
        "80,300,1,350,343,Screen For row",
        "80,420,2,700,400,Watchlists dropdown",
        "80,420,3,1100,420,Edit / Manage",
    ],
    "screening-04-watchlist.png": [
        "80,200,1,700,263,Watchlist selector",
        "80,350,2,700,400,Create New",
        "80,500,3,700,550,Company search",
        "80,700,4,700,900,Companies table",
    ],
    "screening-watchlist-dialog-new.png": [
        "80,250,1,700,320,Name field",
        "80,400,2,500,520,Search Companies",
        "80,400,3,1200,520,Search Sectors",
        "80,700,4,1800,780,Create Watchlist",
    ],
    "screening-watchlist-dialog-companies.png": [
        "80,250,1,700,320,Name field",
        "80,400,2,500,480,Company dropdown",
        "80,400,3,1200,520,Search Sectors",
        "80,700,4,1800,780,Create Watchlist",
    ],
    "screening-watchlist-dialog-sectors.png": [
        "80,250,1,700,320,Name field",
        "80,400,2,500,520,Search Companies",
        "80,400,3,1200,480,Sector dropdown",
        "80,700,4,1800,780,Create Watchlist",
    ],
    "screening-industry-form.png": [
        "80,400,1,350,430,Industry tab",
        "80,550,2,700,600,Select industries",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-03-industry.png": [
        "80,400,1,350,430,Industry tab",
        "80,550,2,700,600,Select industries",
    ],
    "screening-geographic.png": [
        "80,400,1,700,430,Geographic tab",
        "80,550,2,700,600,Select countries",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-geographic-countries.png": [
        "80,400,1,700,430,Geographic tab",
        "80,550,2,700,650,Country dropdown",
    ],
    "screening-02-financial.png": [
        "80,350,1,700,450,Financial form",
        "80,500,2,700,550,Select Metric",
    ],
    "screening-financial-statement-type.png": [
        "80,200,1,1100,200,Financial tab",
        "80,350,2,700,400,Statement type",
        "80,500,3,700,550,Year dropdown",
    ],
    "screening-financial-statement-dropdown.png": [
        "80,200,1,1100,200,Financial tab",
        "80,350,2,700,500,Cash Flow option",
    ],
    "screening-financial-metric.png": [
        "80,200,1,1100,200,Financial tab",
        "80,350,2,700,500,Select Metric",
        "80,500,3,700,400,Statement type",
    ],
    "screening-financial-period-type.png": [
        "80,300,1,500,400,Period Type",
        "80,450,2,700,450,Statement type",
        "80,450,3,700,550,Metric",
    ],
    "screening-financial-year-range.png": [
        "80,300,1,500,400,Period Type",
        "80,450,2,500,550,From year",
        "80,450,3,900,550,To year",
        "80,700,4,350,900,Add Criteria",
    ],
    "screening-financial-year.png": [
        "80,300,1,500,400,Period Type",
        "80,450,2,500,500,Year dropdown",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-financial-operator.png": [
        "80,300,1,500,400,Operator",
        "80,450,2,500,550,Between option",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-financial-value.png": [
        "80,300,1,500,400,Operator",
        "80,450,2,500,500,Value ($mm)",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-key-devs-mode.png": [
        "80,400,1,2100,484,Key Devs tab",
        "80,550,2,700,650,Select Categories",
    ],
    "screening-key-devs-categories.png": [
        "80,400,1,2100,430,Key Devs tab",
        "80,550,2,700,650,Category dropdown",
        "80,700,3,350,900,Add Criteria",
    ],
}


def _to_arrow(raw: str) -> str:
    parts = [p.strip() for p in raw.split(",")]
    bx, by, num, tx, ty = parts[0], parts[1], parts[2], parts[3], parts[4]
    label = ",".join(parts[5:]) if len(parts) > 5 else ""
    return f"{bx},{by},{tx},{ty},{num},{label}"


def annotate(src: Path, dst: Path, markers: list[str]) -> None:
    cmd = [PY, str(ANNOTATE), str(src), "-o", str(dst)]
    for marker in markers:
        cmd.extend(["--arrow", _to_arrow(marker)])
    subprocess.run(cmd, check=True, cwd=REPO)


def main() -> int:
    V2.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    callout_doc: list[str] = ["# v2 screenshot callouts (user-provided batch)\n"]

    for dest_name, src_name in USER_SOURCES.items():
        src = V2 / src_name
        if not src.exists():
            print(f"MISSING source {src_name}")
            continue
        dest = V2 / dest_name
        shutil.copy2(src, dest)
        markers = CALLOUTS[dest_name]
        ann_name = dest_name.replace(".png", "-annotated.png")
        ann_path = V2 / ann_name
        annotate(dest, ann_path, markers)
        manifest.append(
            {
                "page": dest_name.split("-")[0],
                "file": dest_name,
                "source": src_name,
                "status": "ok",
                "annotated": ann_name,
                "reason": "user-provided",
            }
        )
        callout_doc.append(f"\n## `{ann_name}`\n")
        for m in markers:
            callout_doc.append(f"- `{m}`\n")
        print(f"OK {src_name} -> {dest_name} -> {ann_name}")

    (V2 / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (V2 / "CALLOUTS.md").write_text("".join(callout_doc), encoding="utf-8")

    verification = ["# v2 annotation verification\n", "| File | Annotated | Source |\n", "|------|-----------|--------|\n"]
    for entry in manifest:
        verification.append(
            f"| {entry['file']} | YES | {entry['source']} |\n"
        )
    (V2 / "VERIFICATION.md").write_text("".join(verification), encoding="utf-8")
    print(json.dumps({"processed": len(manifest)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
