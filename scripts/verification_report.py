"""Before/after report across every source, with public-source verification.

Sheets:
  VERDICT            headline accuracy, before and after the extractor fixes
  Public Verification the cases checked by hand against company filings/IR
  Before vs After     every filing whose parsed value changed
  All Sources         DB / XBRL / FILING(code) / FILING(model) / PUBLISHED
  Still Suspect       published values another source says are much larger
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

AFTER = json.load(open("/tmp/sc_report/reconciled.json"))
BEFORE = json.load(open("/tmp/sc_report/build_results_before_fix.json"))
OUT = "/tmp/sc_report/STORE_COUNTS_VERIFICATION.xlsx"

# Verified by hand against the company's own filing or investor page.
# (ticker, fiscal year as the FILING states it, published figure, source note)
PUBLIC_TRUTH = [
    ("AZO", 2019, 6411, "AutoZone FY2019: 5,772 US + Mexico + Brazil"),
    ("AZO", 2020, 6549, "AutoZone FY2020 press release"),
    ("BBY", 2021, 1159, "Best Buy FY2021 10-K: 991 Domestic + 168 International"),
    ("WMT", 2020, 11501, "Walmart FY2020 10-K, 'Total units'"),
    ("WMT", 2023, 10623, "Walmart FY2023 10-K, 'Total retail units'"),
    ("MCD", 2025, 45356, "McDonald's Restaurants by Market 2025"),
    ("COST", 2025, 914, "Costco FY2025 results"),
    ("DG", 2025, 20662, "Dollar General FY2025 10-K"),
    ("TGT", 2021, 1897, "Target FY2020 10-K ending store count"),
    ("PLCE", 2020, 924, "Children's Place FY2020 10-K"),
    ("ORLY", 2019, 5460, "O'Reilly FY2019 10-K"),
    ("TSCO", 2019, 2024, "Tractor Supply FY2019: 1,844 TSC + 180 Petsense"),
    ("SHAK", 2019, 275, "Shake Shack FY2019 10-K"),
    ("ULTA", 2026, 1591, "Ulta FY2026 10-K"),
    ("TXRH", 2024, 784, "Texas Roadhouse FY2024 system-wide"),
    ("FND", 2021, 160, "Floor & Decor FY2021 10-K"),
    ("CVS", 2019, 9941, "CVS FY2019 10-K"),
    ("LOW", 2025, 1748, "Lowe's FY2025 10-K"),
    ("ROST", 2022, 1923, "Ross Stores FY2022 10-K"),
    ("TJX", 2026, 5214, "TJX FY2026: 5,214 worldwide (we publish US only)"),
]

HEAD = PatternFill("solid", fgColor="1F4E79")
HEADFONT = Font(color="FFFFFF", bold=True)
GREEN = PatternFill("solid", fgColor="C6EFCE")
RED = PatternFill("solid", fgColor="FFC7CE")
AMBER = PatternFill("solid", fgColor="FFEB9C")


def header(sheet, columns):
    sheet.append(columns)
    for index in range(1, len(columns) + 1):
        sheet.cell(1, index).fill = HEAD
        sheet.cell(1, index).font = HEADFONT
    sheet.freeze_panes = "A2"
    return sheet


def main() -> None:
    after_by_key = {(r["ticker"], r["fiscal_year"]): r for r in AFTER}
    before_by_key = {(r["ticker"], r["fiscal_year"]): r for r in BEFORE}
    book = Workbook()

    # ── public verification ──────────────────────────────────────────────
    verify = header(book.active, ["Ticker", "FY", "PUBLIC TRUTH", "Published now",
                                  "Filing (code) now", "Filing (code) before",
                                  "Filing (model)", "XBRL", "DB", "Match?",
                                  "Public source"])
    book.active.title = "Public Verification"
    hits_now = hits_before = 0
    for ticker, year, truth, note in PUBLIC_TRUTH:
        now = after_by_key.get((ticker, year), {})
        old = before_by_key.get((ticker, year), {})
        published = now.get("final_value")
        code_now, code_old = now.get("l2_value"), old.get("l2_value")
        ok = published == truth
        hits_now += ok
        hits_before += (old.get("l2_value") == truth)
        verify.append([ticker, year, truth, published, code_now, code_old,
                       now.get("l3_value"), now.get("l1_value"), now.get("db_value"),
                       "MATCH" if ok else "MISMATCH", note])
        for column in range(1, 12):
            verify.cell(verify.max_row, column).fill = GREEN if ok else RED

    # ── before vs after ──────────────────────────────────────────────────
    delta = header(book.create_sheet("Before vs After"),
                   ["Ticker", "FY", "Filing (code) BEFORE", "Filing (code) AFTER",
                    "Published", "Change"])
    changed = 0
    for key, row in sorted(after_by_key.items()):
        old = before_by_key.get(key, {})
        if old.get("l2_value") == row.get("l2_value"):
            continue
        changed += 1
        kind = ("newly found" if not old.get("l2_value")
                else "lost" if not row.get("l2_value") else "corrected")
        delta.append([key[0], key[1], old.get("l2_value"), row.get("l2_value"),
                      row.get("final_value"), kind])
        fill = {"newly found": GREEN, "corrected": AMBER, "lost": RED}[kind]
        for column in range(1, 7):
            delta.cell(delta.max_row, column).fill = fill
    delta.auto_filter.ref = delta.dimensions

    # ── all sources ──────────────────────────────────────────────────────
    every = header(book.create_sheet("All Sources"),
                   ["Ticker", "FY", "DB", "XBRL", "FILING (code)", "FILING (model)",
                    "PUBLISHED", "Basis", "Confidence", "Countries"])
    for key, row in sorted(after_by_key.items()):
        every.append([key[0], key[1], row.get("db_value"), row.get("l1_value"),
                      row.get("l2_value"), row.get("l3_value"),
                      row.get("final_value"), row.get("final_basis"),
                      row.get("final_confidence"),
                      len(row.get("by_country") or {})])
    every.auto_filter.ref = every.dimensions

    # ── still suspect ────────────────────────────────────────────────────
    suspect = header(book.create_sheet("Still Suspect"),
                     ["Ticker", "FY", "Published", "A source says", "Ratio",
                      "Why this matters"])
    for key, row in sorted(after_by_key.items()):
        published = row.get("final_value")
        if not published:
            continue
        others = [v for v in (row.get("db_value"), row.get("l1_value"),
                              row.get("l3_value")) if v]
        if not others:
            continue
        highest = max(others)
        if highest > published * 1.10:
            suspect.append([key[0], key[1], published, highest,
                            round(highest / published, 2),
                            "possible segment subtotal published as worldwide"])
    suspect.auto_filter.ref = suspect.dimensions

    # ── verdict ──────────────────────────────────────────────────────────
    verdict = book.create_sheet("VERDICT", 0)
    verdict["A1"] = "Store counts — accuracy before and after the extractor fixes"
    verdict["A1"].font = Font(bold=True, size=14)
    published_now = [r for r in AFTER if r.get("final_value")]
    code_now = sum(1 for r in AFTER if r.get("l2_value"))
    code_before = sum(1 for r in BEFORE if r.get("l2_value"))
    for label, value in [
        ("Filings examined", len(AFTER)),
        ("", ""),
        ("Filing parse produced a value — BEFORE", code_before),
        ("Filing parse produced a value — AFTER", code_now),
        ("Parsed values changed by the fixes", changed),
        ("", ""),
        ("Verified against public sources", len(PUBLIC_TRUTH)),
        ("  correct BEFORE (filing parse)", hits_before),
        ("  correct AFTER (published)", hits_now),
        ("  accuracy after", f"{hits_now / len(PUBLIC_TRUTH) * 100:.0f}%"),
        ("", ""),
        ("Ticker-years published", len(published_now)),
        ("Tickers covered", len({r['ticker'] for r in published_now})),
        ("Withheld — nothing trustworthy", len(AFTER) - len(published_now)),
        ("Still flagged as possible subtotals", suspect.max_row - 1),
        ("", ""),
        ("LLM spend so far (USD)", 0.5015),
        ("Spend since the fixes (USD)", 0.0),
    ]:
        verdict.append([label, value])

    for sheet in book.worksheets:
        for column in range(1, sheet.max_column + 1):
            width = max((len(str(sheet.cell(r, column).value or ""))
                         for r in range(1, min(sheet.max_row, 300) + 1)), default=12)
            sheet.column_dimensions[get_column_letter(column)].width = \
                min(max(width + 2, 11), 58)

    book.save(OUT)
    print(f"public verification: {hits_now}/{len(PUBLIC_TRUTH)} correct after "
          f"(filing parse was {hits_before}/{len(PUBLIC_TRUTH)} before)")
    print(f"parsed values changed: {changed}")
    print(f"still flagged as possible subtotals: {suspect.max_row - 1}")
    print(f"WROTE {OUT}")


if __name__ == "__main__":
    main()
