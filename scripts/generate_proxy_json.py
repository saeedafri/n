#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════
  Script: Generate Proxy JSON (DEF 14A and all proxy form types)
═══════════════════════════════════════════════════════════════════════

Same as generate_and_load.py but:
  - Form type is a proxy form (DEF 14A, DEF 14A/A, DEFA14A, DEFM14A)
  - Only runs Step 1: generate JSON from XBRL
  - No DB calls. No cleanup.

Usage:
    python scripts/generate_proxy_json.py WMT
    python scripts/generate_proxy_json.py WMT --form "DEF 14A/A"
    python scripts/generate_proxy_json.py WMT AAPL MSFT
═══════════════════════════════════════════════════════════════════════
"""

import os
import sys
import argparse
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# ── Edgar cache ──
_EDGAR_CACHE = "/tmp/edgar_cache"
os.makedirs(_EDGAR_CACHE, exist_ok=True)
os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", _EDGAR_CACHE)
os.environ.setdefault("EDGAR_CACHE_DIR", _EDGAR_CACHE)

OUTPUT_BASE = PROJECT_ROOT / "data" / "filings"


# ══════════════════════════════════════════════════════════════════════
#  STEP 1: GENERATE JSON (same as generate_and_load.py)
# ══════════════════════════════════════════════════════════════════════

def generate_proxy_json(ticker: str, form: str) -> bool:
    """
    Fetch the latest proxy filing for the given form type and run
    export_xbrl_facts → generates FINAL_FACTS_FILTERED.json + FINANCIAL_RATIOS.json
    in data/filings/<TICKER>/proxy/<FORM>/
    """
    from scripts.export_xbrl_facts import Config, process_company_year, set_identity

    cfg = Config()
    cfg.TICKERS = [ticker]
    cfg.FORM    = form
    cfg.YEARS   = [None]          # proxy filings are not year-keyed — latest only
    cfg.SKIP_EXISTING = False

    identity = os.getenv("EDGAR_IDENTITY", cfg.SEC_IDENTITY)
    set_identity(identity)

    try:
        return process_company_year(ticker, None, cfg)
    except Exception as e:
        print(f"  ❌ JSON generation failed for {ticker} / {form}: {e}")
        traceback.print_exc(limit=3)
        return False


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Generate proxy filing JSON (Step 1 only — no DB, no cleanup)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/generate_proxy_json.py WMT
  python scripts/generate_proxy_json.py WMT --form "DEF 14A/A"
  python scripts/generate_proxy_json.py WMT AAPL MSFT
        """,
    )
    parser.add_argument("tickers", type=str, nargs="+", help="Company ticker(s)")
    parser.add_argument(
        "--form", type=str, default="DEF 14A",
        help='Proxy form type (default: "DEF 14A"). Options: DEF 14A | DEF 14A/A | DEFA14A | DEFM14A',
    )
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers]
    form    = args.form

    print(f"\n{'═' * 60}")
    print(f"  GENERATE PROXY JSON — Step 1 only, no DB")
    print(f"  Tickers : {tickers}")
    print(f"  Form    : {form}")
    print(f"  Output  : {OUTPUT_BASE}/<TICKER>/")
    print(f"{'═' * 60}")

    for ticker in tickers:
        print(f"\n  ⏳ {ticker} / {form} ...")
        ok = generate_proxy_json(ticker, form)
        if ok:
            print(f"  ✅ {ticker} — JSON generated")
        else:
            print(f"  ❌ {ticker} — failed (see above)")

    print(f"\n{'═' * 60}\n")


if __name__ == "__main__":
    main()
