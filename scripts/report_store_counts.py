"""Compare every source for every ticker-year and write the Excel report.

Sources compared per (ticker, fiscal year):
    DB        the store_count row the portal shows today
    L1 xbrl   us-gaap facts already in coreiq_filing_metrics_v5
    L2 filing the 10-K HTML in Azure blob
    L3 llm    gpt-4o-mini over a windowed slice
    app now   what the Additional Data tab renders today (before/after run)

Where two independent sources agree and the DB differs, the DB is the odd one
out — those rows are listed separately as likely DB errors.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

RESULTS = "/tmp/sc_report/reconciled.json"
APP_NOW = "/tmp/sc_report/after_warm.json"
OUT = "/tmp/sc_report/store_count_extraction_report.xlsx"

RED = PatternFill("solid", fgColor="FFC7CE")
GREEN = PatternFill("solid", fgColor="C6EFCE")
AMBER = PatternFill("solid", fgColor="FFEB9C")
HEAD = PatternFill("solid", fgColor="1F4E79")
HEADFONT = Font(color="FFFFFF", bold=True)


def header(sheet, columns):
    sheet.append(columns)
    for column in range(1, len(columns) + 1):
        sheet.cell(1, column).fill = HEAD
        sheet.cell(1, column).font = HEADFONT
    sheet.freeze_panes = "A2"


def main() -> None:
    rows = json.load(open(RESULTS))
    app_now = {}
    if os.path.exists(APP_NOW):
        for ticker, payload in json.load(open(APP_NOW)).items():
            for entry in payload.get("rows", []):
                for year, value in entry.get("values", {}).items():
                    if value is not None:
                        app_now[(ticker, int(year))] = int(value)

    workbook = Workbook()

    # ── per ticker-year ──────────────────────────────────────────────────
    sheet = workbook.active
    sheet.title = "Per Ticker Per Year"
    header(sheet, ["Ticker", "FY", "DB (today)", "L1 xbrl", "L2 filing", "L3 llm",
                   "CHOSEN", "Rule", "Confidence", "Sources agreeing",
                   "App shows today", "Verdict", "Evidence"])

    tally = Counter()
    db_wrong, new_values = [], []

    for row in sorted(rows, key=lambda r: (r["ticker"], r["fiscal_year"])):
        ticker, year = row["ticker"], row["fiscal_year"]
        db, l1, l2, l3 = (row["db_value"], row["l1_value"],
                          row["l2_value"], row["l3_value"])
        chosen = row.get("final_value") or row["value"]
        independent = [v for v in (l1, l2, l3) if v is not None]
        agreeing = max(Counter(independent).values()) if independent else 0

        if chosen is None:
            verdict = "NOT FOUND"
        elif db is None:
            verdict = "NEW (db had nothing)"
            new_values.append((ticker, year, chosen))
        elif chosen == db:
            verdict = "confirms DB"
        elif agreeing >= 2:
            verdict = "DB LIKELY WRONG"
            db_wrong.append((ticker, year, db, chosen, agreeing, row["evidence"][:120]))
        else:
            verdict = "differs from DB (1 source)"
        tally[verdict] += 1

        sheet.append([ticker, year, db, l1, l2, l3, chosen, row.get("final_basis", row["source_rule"]),
                      row.get("final_confidence", row["confidence"]), agreeing, app_now.get((ticker, year)),
                      verdict, (row["evidence"] or "")[:200]])
        fill = {"confirms DB": GREEN, "DB LIKELY WRONG": RED,
                "NEW (db had nothing)": GREEN, "NOT FOUND": AMBER,
                "differs from DB (1 source)": AMBER}.get(verdict)
        if fill:
            for column in range(1, 14):
                sheet.cell(sheet.max_row, column).fill = fill
    sheet.auto_filter.ref = sheet.dimensions

    # ── likely DB errors ─────────────────────────────────────────────────
    errors = workbook.create_sheet("DB Likely Wrong")
    header(errors, ["Ticker", "FY", "DB says", "We say", "Independent sources agreeing",
                    "Evidence"])
    for record in sorted(db_wrong):
        errors.append(list(record))
    errors.auto_filter.ref = errors.dimensions

    # ── coverage by ticker ───────────────────────────────────────────────
    coverage = workbook.create_sheet("Coverage By Ticker")
    header(coverage, ["Ticker", "Years found", "Years DB had", "Years gained",
                      "Confirms DB", "DB likely wrong", "Country breakdown years"])
    per_ticker = defaultdict(list)
    for row in rows:
        per_ticker[row["ticker"]].append(row)
    for ticker, entries in sorted(per_ticker.items()):
        found = [e for e in entries if e["value"]]
        had_db = [e for e in entries if e["db_value"]]
        gained = [e for e in found if not e["db_value"]]
        confirms = [e for e in found if e["db_value"] and e["value"] == e["db_value"]]
        wrong = [e for e in found if e["db_value"] and e["value"] != e["db_value"]
                 and len({v for v in (e["l1_value"], e["l2_value"], e["l3_value"])
                          if v is not None}) == 1
                 and len([v for v in (e["l1_value"], e["l2_value"], e["l3_value"])
                          if v is not None]) >= 2]
        coverage.append([ticker, len(found), len(had_db), len(gained),
                         len(confirms), len(wrong),
                         sum(1 for e in found if e["by_country"])])
    coverage.auto_filter.ref = coverage.dimensions

    # ── geography ────────────────────────────────────────────────────────
    geography = workbook.create_sheet("By Country")
    header(geography, ["Ticker", "FY", "Total", "Country", "Stores", "Continent"])
    from data.store_count_sources import continent_of
    for row in sorted(rows, key=lambda r: (r["ticker"], r["fiscal_year"])):
        for country, count in sorted((row.get("by_country") or {}).items(),
                                     key=lambda kv: -kv[1]):
            geography.append([row["ticker"], row["fiscal_year"], row["value"],
                              country, count, continent_of(country)])
    geography.auto_filter.ref = geography.dimensions

    continents = workbook.create_sheet("By Continent")
    header(continents, ["Ticker", "FY", "Total", "Continent", "Stores"])
    for row in sorted(rows, key=lambda r: (r["ticker"], r["fiscal_year"])):
        # Recompute rather than trust the cached roll-up: entries written
        # before country-label normalisation put US stores under "Other".
        from data.store_count_sources import roll_up_continents
        fresh = roll_up_continents(row.get("by_country") or {})
        for continent, count in sorted(fresh.items(), key=lambda kv: -kv[1]):
            continents.append([row["ticker"], row["fiscal_year"], row["value"],
                               continent, count])
    continents.auto_filter.ref = continents.dimensions

    # ── summary, placed first ────────────────────────────────────────────
    summary = workbook.create_sheet("Summary", 0)
    summary["A1"] = "Store count extraction — all sources compared"
    summary["A1"].font = Font(bold=True, size=14)
    spend = sum(r.get("llm_cost", 0) for r in rows)
    produced = [r for r in rows if r["value"]]
    lines = [
        ("Filings processed", len(rows)),
        ("Produced a value", len(produced)),
        ("", ""),
        ("From L1 xbrl (our DB, free)", sum(1 for r in produced if r["l1_value"])),
        ("From L2 filing parse (free)", sum(1 for r in produced if r["l2_value"])),
        ("From L3 llm (paid)", sum(1 for r in produced if r["l3_value"])),
        ("", ""),
        ("Confirms the DB", tally["confirms DB"]),
        ("DB likely WRONG (2+ sources agree against it)", tally["DB LIKELY WRONG"]),
        ("New value, DB had nothing", tally["NEW (db had nothing)"]),
        ("Differs, only one source", tally["differs from DB (1 source)"]),
        ("Not found by any layer", tally["NOT FOUND"]),
        ("", ""),
        ("Years with a country breakdown", sum(1 for r in produced if r["by_country"])),
        ("", ""),
        ("LLM spend for this run (USD)", round(spend, 4)),
        ("Cost per filing (USD)", round(spend / max(len(rows), 1), 6)),
    ]
    for label, value in lines:
        summary.append([label, value])

    for page in workbook.worksheets:
        for column in range(1, page.max_column + 1):
            width = max((len(str(page.cell(r, column).value or ""))
                         for r in range(1, min(page.max_row, 300) + 1)), default=12)
            page.column_dimensions[get_column_letter(column)].width = min(max(width + 2, 11), 60)

    workbook.save(OUT)
    print(json.dumps(dict(tally), indent=1))
    print(f"spend ${spend:.4f}")
    print(f"WROTE {OUT}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(os.getcwd(), "app"))
    main()
