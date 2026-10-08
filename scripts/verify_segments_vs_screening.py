"""Every number the Segments tab shows must be what Screening filters and sorts on.

    .venv/bin/python scripts/verify_segments_vs_screening.py WMT MDLZ ...
    .venv/bin/python scripts/verify_segments_vs_screening.py --all --out parity.jsonl

For each company the tab's own annual tables are built, and so are the rows the
screener's segment cache would store for it (the same code the cache rebuild runs,
without writing anything). Every tab cell must appear in the screener rows with the
same type, metric, member, year and value. The screener may hold more: a segment
named after a place is also indexed as geography, so a geography filter finds it.
"""
import argparse
import contextlib
import io
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))


def compare(ticker):
    from data.repository import SegmentDataRepository as repo
    from data import screening_service as screening
    with contextlib.redirect_stdout(io.StringIO()):
        table = repo._build_segment_tables_from_db(ticker, date(2012, 1, 1), date(2026, 12, 31))
        entries, skipped = screening._segment_entries_for_tickers([ticker], raise_on_timeout=False)
    if skipped:
        return {"ticker": ticker, "error": "screening scan timed out"}
    stored = {(kind, metric, member, int(year)): value
              for _t, kind, metric, member, year, value in entries}
    counts, issues = Counter(), []
    for part, kind in (("business_segments", "business"), ("geo_segments", "geographical")):
        for metric, members in ((table or {}).get(part) or {}).items():
            for member, by_year in members.items():
                for year, value in by_year.items():
                    if value is None:
                        continue
                    other = stored.get((kind, metric, member, int(year)))
                    if other is None:
                        counts["tab_not_in_screening"] += 1
                        issues.append(["tab_not_in_screening", kind, metric, member, int(year), value])
                    elif abs(other - value) > max(0.0015, abs(value) * 1e-6):
                        counts["different_value"] += 1
                        issues.append(["different_value", kind, metric, member, int(year), value, other])
                    else:
                        counts["same"] += 1
    return {"ticker": ticker, "counts": dict(counts), "issues": issues}


def main():
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--out", default="-")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    from data import screening_service as screening
    tickers = screening._segment_cache_universe() if args.all else [t.upper() for t in args.tickers]
    out = sys.stdout if args.out == "-" else open(args.out, "w")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(args.jobs) as pool:
        for result in pool.map(compare, tickers):
            out.write(json.dumps(result, default=str) + "\n")
            out.flush()
            print(result["ticker"], result.get("error") or result["counts"], file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
