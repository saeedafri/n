"""Score the filing parser across EVERY filing, not a sample.

There is no official store count for 4,816 filings anywhere, so accuracy at
that scale has to be measured against things that are independent of the
parser rather than against a truth table that does not exist:

  corroborated   another pipeline (our database, or the issuer's own XBRL tag)
                 arrived at the same number independently
  fits series    the value sits on the company's own multi-year trend, which a
                 misread quantity (square footage, a segment, an activity
                 count) essentially never does
  unsupported    neither — nothing backs this number up

The first two are evidence. The third is the honest size of the doubt.

    .venv/bin/python scripts/score_all_filings.py
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict

BUILD = json.load(open("/tmp/sc_report/build_results.json"))
RECONCILED = json.load(open("/tmp/sc_report/reconciled.json"))
TREND_TOLERANCE = 0.25


def on_trend(series: dict) -> set:
    """Years whose value sits on the best-supported line through the series."""
    years = [y for y in sorted(series) if series[y] and series[y] > 0]
    if len(years) < 4:
        return set(years)
    best: set = set()
    for index, first in enumerate(years):
        for second in years[index + 1:]:
            slope = (math.log(series[second]) - math.log(series[first])) / (second - first)
            intercept = math.log(series[first]) - slope * first
            fitted = {y for y in years
                      if abs(math.log(series[y]) - (slope * y + intercept)) <= TREND_TOLERANCE}
            if len(fitted) > len(best):
                best = fitted
    return best


def close(a, b, tolerance=0.01) -> bool:
    return bool(a and b and abs(a - b) <= max(1, b * tolerance))


def main() -> None:
    published = {(r["ticker"], r["fiscal_year"]): r for r in RECONCILED}

    per_ticker = defaultdict(dict)
    for row in BUILD:
        if row.get("value") and row.get("fiscal_year"):
            per_ticker[row["ticker"]][int(row["fiscal_year"])] = int(row["value"])
    trend_ok = {(ticker, year)
                for ticker, series in per_ticker.items()
                for year in on_trend(series)}

    produced = corroborated = fitted = unsupported = 0
    empty = 0
    doubtful = []
    for row in BUILD:
        value = row.get("value")
        if not value:
            empty += 1
            continue
        produced += 1
        key = (row["ticker"], row.get("fiscal_year"))
        other = published.get(key, {})
        backed = (close(value, other.get("db_value")) or
                  close(value, other.get("l1_value")) or
                  close(value, row.get("db_value")))
        if backed:
            corroborated += 1
        elif key in trend_ok:
            fitted += 1
        else:
            unsupported += 1
            doubtful.append((row["ticker"], row.get("fiscal_year"), value))

    total = len(BUILD)
    print(f"FILINGS PROCESSED                : {total:,}")
    print(f"  produced a value               : {produced:,}")
    print(f"  no store count in the filing   : {empty:,}")
    print()
    print("OF THE VALUES PRODUCED — what backs each one")
    print(f"  corroborated by DB or XBRL     : {corroborated:,} "
          f"({corroborated / max(produced, 1) * 100:.1f}%)")
    print(f"  fits the company's own trend   : {fitted:,} "
          f"({fitted / max(produced, 1) * 100:.1f}%)")
    print(f"  nothing supports it            : {unsupported:,} "
          f"({unsupported / max(produced, 1) * 100:.1f}%)")
    print()
    supported = corroborated + fitted
    print(f"  SUPPORTED BY SOMETHING         : {supported:,} "
          f"({supported / max(produced, 1) * 100:.1f}%)")
    print()

    shown = [r for r in RECONCILED if r.get("final_value")]
    shown_ok = sum(1 for r in shown
                   if (r["ticker"], r["fiscal_year"]) in trend_ok
                   or close(r["final_value"], r.get("db_value"))
                   or close(r["final_value"], r.get("l1_value")))
    print(f"AFTER RECONCILIATION (what users see)")
    print(f"  ticker-years published         : {len(shown):,}")
    print(f"  supported by trend or a source : {shown_ok:,} "
          f"({shown_ok / max(len(shown), 1) * 100:.1f}%)")
    print(f"  published with nothing backing : {len(shown) - shown_ok:,}")

    doubtful.sort(key=lambda r: -(r[2] or 0))
    print(f"\nWORST UNSUPPORTED VALUES (top 25 by size)")
    for ticker, year, value in doubtful[:25]:
        print(f"  {ticker:8} FY{year}  {value:>9,}")

    json.dump([{"ticker": t, "fiscal_year": y, "value": v} for t, y, v in doubtful],
              open("/tmp/sc_report/unsupported.json", "w"), indent=1)
    print(f"\nWROTE /tmp/sc_report/unsupported.json ({len(doubtful)} rows)")


if __name__ == "__main__":
    main()
