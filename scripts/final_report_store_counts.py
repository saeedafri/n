"""The full comparison report: what each source contributed, and where.

Answers, per ticker and per year:
  * what the portal shows today (DB store_count rows)
  * what the XBRL layer says (us-gaap facts already in v5)
  * what the official 10-K says (our parse of the filing in Azure blob)
  * what the LLM said when asked
  * what we finally publish, and on what grounds
  * the country and continent split where the filing discloses one
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
from data.store_count_sources import continent_of, roll_up_continents  # noqa: E402

ROWS = json.load(open("/tmp/sc_report/reconciled.json"))
OUT = "/tmp/sc_report/STORE_COUNTS_FINAL_REPORT.xlsx"

GREEN = PatternFill("solid", fgColor="C6EFCE")
RED = PatternFill("solid", fgColor="FFC7CE")
AMBER = PatternFill("solid", fgColor="FFEB9C")
BLUE = PatternFill("solid", fgColor="DDEBF7")
HEAD = PatternFill("solid", fgColor="1F4E79")
HEADFONT = Font(color="FFFFFF", bold=True)


def header(sheet, columns):
    sheet.append(columns)
    for index in range(1, len(columns) + 1):
        sheet.cell(1, index).fill = HEAD
        sheet.cell(1, index).font = HEADFONT
    sheet.freeze_panes = "A2"
    return sheet


def autosize(book):
    for sheet in book.worksheets:
        for column in range(1, sheet.max_column + 1):
            width = max((len(str(sheet.cell(r, column).value or ""))
                         for r in range(1, min(sheet.max_row, 400) + 1)), default=12)
            sheet.column_dimensions[get_column_letter(column)].width = \
                min(max(width + 2, 11), 58)


def main() -> None:
    book = Workbook()
    published = [r for r in ROWS if r.get("final_value")]

    # ── 1. Source scoreboard ─────────────────────────────────────────────
    board = header(book.active, ["Source", "Produced a value", "Was published",
                                 "Win rate", "Agreed with final", "Notes"])
    book.active.title = "Source Scoreboard"

    def contributed(key):
        return [r for r in ROWS if r.get(key)]

    def agreed(key):
        return [r for r in published if r.get(key) == r["final_value"]]

    for key, label, note in (
        ("db_value", "DB (portal today)", "the store_count rows the tab shows now"),
        ("l1_value", "XBRL (us-gaap in v5)", "issuer-tagged, free, already in our DB"),
        ("l2_value", "OFFICIAL FILING (10-K)", "source of truth — parsed from Azure blob"),
        ("l3_value", "LLM (gpt-4o-mini)", "only asked when free layers failed"),
    ):
        produced = contributed(key)
        matched = agreed(key)
        board.append([label, len(produced), len(matched),
                      f"{len(matched) / max(len(produced), 1) * 100:.0f}%",
                      len(matched), note])
        if key == "l2_value":
            for column in range(1, 7):
                board.cell(board.max_row, column).fill = GREEN

    # ── 2. Per ticker per year ───────────────────────────────────────────
    grid = header(book.create_sheet("Per Ticker Per Year"),
                  ["Ticker", "FY", "DB today", "XBRL", "FILING", "LLM",
                   "PUBLISHED", "Basis", "Confidence", "Change vs DB",
                   "Countries", "Evidence"])
    for row in sorted(ROWS, key=lambda r: (r["ticker"], r["fiscal_year"])):
        final, db = row.get("final_value"), row.get("db_value")
        if final and db and final != db:
            change = "CORRECTED"
        elif final and not db:
            change = "NEW"
        elif final and final == db:
            change = "confirms DB"
        elif not final and db:
            change = "withheld (was shown)"
        else:
            change = "no data"
        grid.append([row["ticker"], row["fiscal_year"], db, row.get("l1_value"),
                     row.get("l2_value"), row.get("l3_value"), final,
                     row.get("final_basis"), row.get("final_confidence"), change,
                     len(row.get("by_country") or {}),
                     (row.get("evidence") or "")[:180]])
        fill = {"CORRECTED": AMBER, "NEW": GREEN, "confirms DB": BLUE,
                "withheld (was shown)": RED}.get(change)
        if fill:
            for column in range(1, 13):
                grid.cell(grid.max_row, column).fill = fill
    grid.auto_filter.ref = grid.dimensions

    # ── 3. Coverage by ticker ────────────────────────────────────────────
    cover = header(book.create_sheet("Coverage By Ticker"),
                   ["Ticker", "Years published", "First year", "Last year",
                    "Years DB had", "Years GAINED", "Corrected", "Withheld",
                    "From filing", "From XBRL", "From LLM", "Country years"])
    per_ticker = defaultdict(list)
    for row in ROWS:
        per_ticker[row["ticker"]].append(row)
    for ticker, entries in sorted(per_ticker.items()):
        done = [e for e in entries if e.get("final_value")]
        if not done:
            continue
        years = sorted(e["fiscal_year"] for e in done)
        had = {e["fiscal_year"] for e in entries if e.get("db_value")}
        cover.append([
            ticker, len(done), years[0], years[-1], len(had),
            len([e for e in done if e["fiscal_year"] not in had]),
            len([e for e in done if e.get("db_value")
                 and e["final_value"] != e["db_value"]]),
            len([e for e in entries if not e.get("final_value") and e.get("db_value")]),
            len([e for e in done if str(e.get("final_basis", "")).startswith("filing")]),
            len([e for e in done if e.get("l1_value") == e["final_value"]]),
            len([e for e in done if e.get("l3_value") == e["final_value"]]),
            len([e for e in done if e.get("by_country")]),
        ])
    cover.auto_filter.ref = cover.dimensions

    # ── 4/5. Geography ───────────────────────────────────────────────────
    country = header(book.create_sheet("By Country"),
                     ["Ticker", "FY", "Worldwide total", "Country", "Stores",
                      "Continent", "% of total"])
    continent = header(book.create_sheet("By Continent"),
                       ["Ticker", "FY", "Worldwide total", "Continent", "Stores",
                        "% of total"])
    for row in sorted(published, key=lambda r: (r["ticker"], r["fiscal_year"])):
        split = row.get("by_country") or {}
        if not split:
            continue
        total = row["final_value"]
        for name, count in sorted(split.items(), key=lambda kv: -kv[1]):
            country.append([row["ticker"], row["fiscal_year"], total, name, count,
                            continent_of(name), round(count / total * 100, 1)])
        for name, count in sorted(roll_up_continents(split).items(),
                                  key=lambda kv: -kv[1]):
            continent.append([row["ticker"], row["fiscal_year"], total, name,
                              count, round(count / total * 100, 1)])
    country.auto_filter.ref = country.dimensions
    continent.auto_filter.ref = continent.dimensions

    # ── 6. Needs review ──────────────────────────────────────────────────
    review = header(book.create_sheet("Needs Review"),
                    ["Ticker", "FY", "DB today", "Our value", "Basis", "Why"])
    for row in ROWS:
        if row.get("final_confidence") == "low" or (
                not row.get("final_value") and row.get("db_value")):
            review.append([row["ticker"], row["fiscal_year"], row.get("db_value"),
                           row.get("final_value"), row.get("final_basis"),
                           row.get("review_note") or row.get("final_basis")])
    review.auto_filter.ref = review.dimensions

    # ── 7. Summary, first ────────────────────────────────────────────────
    summary = book.create_sheet("SUMMARY", 0)
    summary["A1"] = "Store Counts — full extraction across every 10-K we hold"
    summary["A1"].font = Font(bold=True, size=14)
    spend = sum(r.get("llm_cost", 0) or 0 for r in ROWS)
    tickers_out = {r["ticker"] for r in published}
    db_tickers = {r["ticker"] for r in ROWS if r.get("db_value")}
    basis = Counter(str(r.get("final_basis", "")).split(" (")[0] for r in published)
    for label, value in [
        ("Filings examined", len(ROWS)),
        ("Ticker-years PUBLISHED", len(published)),
        ("Tickers with data now", len(tickers_out)),
        ("Tickers with data before", len(db_tickers)),
        ("Tickers GAINED", len(tickers_out - db_tickers)),
        ("", ""),
        ("Values confirming the DB", sum(1 for r in published
                                         if r["final_value"] == r.get("db_value"))),
        ("Values CORRECTING the DB", sum(1 for r in published if r.get("db_value")
                                         and r["final_value"] != r["db_value"])),
        ("Values NEW (DB had none)", sum(1 for r in published if not r.get("db_value"))),
        ("Withheld — nothing trustworthy", sum(1 for r in ROWS
                                               if not r.get("final_value"))),
        ("", ""),
        ("Confidence high", sum(1 for r in published
                                if r["final_confidence"] == "high")),
        ("Confidence medium", sum(1 for r in published
                                  if r["final_confidence"] == "medium")),
        ("Confidence low (kept DB, flagged)", sum(1 for r in published
                                                  if r["final_confidence"] == "low")),
        ("", ""),
        ("Ticker-years with country split", sum(1 for r in published
                                                if r.get("by_country"))),
        ("", ""),
        ("TOTAL LLM SPEND (USD)", round(spend, 4)),
        ("Cost per filing (USD)", round(spend / max(len(ROWS), 1), 6)),
    ]:
        summary.append([label, value])
    summary.append([])
    summary.append(["Basis for published values", ""])
    for name, count in basis.most_common():
        summary.append([f"   {name}", count])

    autosize(book)
    book.save(OUT)

    print(f"published {len(published)} ticker-years across {len(tickers_out)} tickers")
    print(f"gained {len(tickers_out - db_tickers)} tickers that had no store data")
    print(f"spend ${spend:.4f}")
    print(f"WROTE {OUT}")


if __name__ == "__main__":
    main()
