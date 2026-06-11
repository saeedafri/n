#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════
  Script 1: Fetch Filing HTML
═══════════════════════════════════════════════════════════════════════

Downloads 10-K / 10-Q filing HTML from SEC EDGAR and saves to:
  data/filings/{TICKER}/{YEAR}/{DOC_TYPE}/filing.html

This script ONLY downloads the HTML — no JSON generation, no DB load.

Usage:
    python scripts/fetch_filing_html.py AAPL --years 2024
    python scripts/fetch_filing_html.py AAPL AMZN WMT --years 2024 2025
    python scripts/fetch_filing_html.py AAPL --years 2020 2021 2022 2023 2024 2025 --form 10-Q
    python scripts/fetch_filing_html.py --all --years 2024 2025
═══════════════════════════════════════════════════════════════════════
"""

import os
import sys
import argparse
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# ── Edgar cache ──
_EDGAR_CACHE = "/tmp/edgar_cache"
os.makedirs(_EDGAR_CACHE, exist_ok=True)
os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", _EDGAR_CACHE)
os.environ.setdefault("EDGAR_CACHE_DIR", _EDGAR_CACHE)

from scripts.export_xbrl_facts import (
    download_filing, download_all_filings, determine_quarter,
    set_identity, ensure_dir, log
)

OUTPUT_BASE = PROJECT_ROOT / "data" / "filings"


def fetch_html_10k(ticker: str, year: int) -> bool:
    """Download 10-K filing HTML for a single company-year."""
    out_dir = OUTPUT_BASE / ticker / str(year) / "10-K"
    html_path = out_dir / "filing.html"

    if html_path.exists():
        print(f"  ✅ {ticker}/{year}/10-K/filing.html — already exists, skipping")
        return True

    filing = download_filing(ticker, year, "10-K")
    if not filing:
        print(f"  ❌ {ticker}/{year}/10-K — no filing found on SEC EDGAR")
        return False

    try:
        html = filing.html()
        if html:
            ensure_dir(str(out_dir))
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  ✅ {ticker}/{year}/10-K/filing.html — saved ({len(html):,} bytes)")
            return True
        else:
            print(f"  ❌ {ticker}/{year}/10-K — filing has no HTML content")
            return False
    except Exception as e:
        print(f"  ❌ {ticker}/{year}/10-K — error: {e}")
        return False


def fetch_html_10q(ticker: str, year: int) -> bool:
    """Download 10-Q filing HTML for all quarters of a company-year."""
    filings = download_all_filings(ticker, year, "10-Q")
    if not filings:
        print(f"  ❌ {ticker}/{year}/10-Q — no filings found on SEC EDGAR")
        return False

    all_ok = True
    processed = []
    for filing in filings:
        quarter = determine_quarter(filing)
        dir_name = f"10-Q-{quarter}"  # e.g. "10-Q-Q1"

        if quarter in processed:
            continue
        processed.append(quarter)

        out_dir = OUTPUT_BASE / ticker / str(year) / dir_name
        html_path = out_dir / "filing.html"

        if html_path.exists():
            print(f"  ✅ {ticker}/{year}/{dir_name}/filing.html — already exists, skipping")
            continue

        try:
            html = filing.html()
            if html:
                ensure_dir(str(out_dir))
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  ✅ {ticker}/{year}/{dir_name}/filing.html — saved ({len(html):,} bytes)")
            else:
                print(f"  ❌ {ticker}/{year}/{dir_name} — filing has no HTML content")
                all_ok = False
        except Exception as e:
            print(f"  ❌ {ticker}/{year}/{dir_name} — error: {e}")
            all_ok = False

    return all_ok


def fetch_html_20f(ticker: str, year: int) -> bool:
    """Download 20-F filing HTML for foreign companies."""
    out_dir = OUTPUT_BASE / ticker / str(year) / "20-F"
    html_path = out_dir / "filing.html"

    if html_path.exists():
        print(f"  ✅ {ticker}/{year}/20-F/filing.html — already exists, skipping")
        return True

    filing = download_filing(ticker, year, "20-F")
    if not filing:
        print(f"  ❌ {ticker}/{year}/20-F — no filing found on SEC EDGAR")
        return False

    try:
        html = filing.html()
        if html:
            ensure_dir(str(out_dir))
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  ✅ {ticker}/{year}/20-F/filing.html — saved ({len(html):,} bytes)")
            return True
        else:
            print(f"  ❌ {ticker}/{year}/20-F — filing has no HTML content")
            return False
    except Exception as e:
        print(f"  ❌ {ticker}/{year}/20-F — error: {e}")
        return False


def _get_all_tickers():
    """Get all tickers from data/filings directory."""
    tickers = []
    for d in sorted(OUTPUT_BASE.iterdir()):
        if d.is_dir() and not d.name.startswith('.'):
            tickers.append(d.name)
    return tickers


def main():
    parser = argparse.ArgumentParser(
        description="Fetch filing HTML from SEC EDGAR (no JSON, no DB)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/fetch_filing_html.py AAPL --years 2024
  python scripts/fetch_filing_html.py AAPL AMZN WMT --years 2024 2025
  python scripts/fetch_filing_html.py AAPL --years 2020 2021 2022 2023 2024 2025 --form 10-Q
  python scripts/fetch_filing_html.py TSM --years 2024 --form 20-F    # Foreign company
  python scripts/fetch_filing_html.py --all --years 2024 2025
        """,
    )
    parser.add_argument("tickers", type=str, nargs="*", help="Company ticker(s) (e.g. AAPL AMZN WMT)")
    parser.add_argument("--years", type=int, nargs="+", required=True, help="Year(s) to fetch")
    parser.add_argument("--form", type=str, default="both",
                        help="Form type: 10-K, 10-Q, 20-F, or both (default: both)")
    parser.add_argument("--all", action="store_true",
                        help="Process ALL companies in data/filings/")
    args = parser.parse_args()

    if args.all:
        tickers = _get_all_tickers()
    elif args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        parser.error("Provide ticker(s) or use --all")
        return

    identity = os.getenv("EDGAR_IDENTITY", "Research Portal user@company.com")
    set_identity(identity)

    print(f"\n{'═' * 60}")
    print(f"  FETCH FILING HTML")
    print(f"  Tickers: {len(tickers)}  |  Form: {args.form}  |  Years: {args.years}")
    print(f"{'═' * 60}")

    success = 0
    failed = 0
    total_tasks = len(tickers) * len(args.years)
    current_task = 0
    
    for ticker in tickers:
        print(f"\n── {ticker} ──")
        for year in args.years:
            current_task += 1
            progress_pct = (current_task / total_tasks) * 100
            print(f"\n[PROGRESS] {current_task}/{total_tasks} ({progress_pct:.1f}%) - Processing {ticker}/{year}")
            if args.form.upper() in ("BOTH",):
                forms = ["10-K", "10-Q"]
            elif args.form.upper() in ("10-K",):
                forms = ["10-K"]
            elif args.form.upper() in ("10-Q", "10Q"):
                forms = ["10-Q"]
            elif args.form.upper() in ("20-F", "20F"):
                forms = ["20-F"]
            else:
                print(f"  ❌ Unknown form type: {args.form}")
                forms = []

            for form in forms:
                if form == "10-K":
                    ok = fetch_html_10k(ticker, year)
                elif form == "20-F":
                    ok = fetch_html_20f(ticker, year)
                else:
                    ok = fetch_html_10q(ticker, year)

                if ok:
                    success += 1
                else:
                    failed += 1

    print(f"\n{'═' * 60}")
    print(f"  Done: {success} succeeded, {failed} failed")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
