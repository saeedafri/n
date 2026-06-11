"""
enrich_from_edgartools.py
--------------------------
Pre-extracts and caches SEC section text from local filing.html files.

This is a preprocessing step for Phase 6.5 LLM extraction.  Instead of
re-parsing the large (~2 MB) iXBRL HTML on every search, we extract the
key text sections once and store them in SECTION_CACHE.json alongside the
filing.  The LLM extractor then reads from this lightweight cache.

Note: EdgarTools cannot parse XBRL statements from local iXBRL files
(has_xbrl=False — it needs the SEC taxonomy which isn't embedded locally).
However, get_sec_section() correctly extracts text by reading the HTML DOM.

Usage:
    python scripts/enrich_from_edgartools.py               # all filings
    python scripts/enrich_from_edgartools.py AAPL          # specific ticker
    python scripts/enrich_from_edgartools.py AAPL 2024 10K # specific filing
    python scripts/enrich_from_edgartools.py --force        # re-extract all

Run from project root:
    python scripts/enrich_from_edgartools.py
"""

import sys
import json
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Sections to extract from each filing, in priority order.
# 10-K sections:
# part_ii_item_8 = Financial Statements & Notes (primary financial data)
# part_ii_item_7 = MD&A (management discussion with key metric commentary)
# part_i_item_1  = Business Description (segment definitions, employee count)
# part_i_item_2  = Properties (store counts, lease info)
TARGET_SECTIONS_10K = [
    "part_ii_item_8",
    "part_ii_item_7",
    "part_i_item_1",
    "part_i_item_2",
    "part_ii_item_7a",
]

# 10-Q sections (different Part/Item numbering):
# part_i_item_1  = Financial Statements (quarterly)
# part_i_item_2  = MD&A (quarterly)
# part_i_item_3  = Quantitative/Qualitative Market Risk
# part_ii_item_1 = Legal Proceedings
# part_ii_item_1a = Risk Factors
TARGET_SECTIONS_10Q = [
    "part_i_item_1",
    "part_i_item_2",
    "part_i_item_3",
    "part_ii_item_1",
    "part_ii_item_1a",
]

# Default for backwards compatibility
TARGET_SECTIONS = TARGET_SECTIONS_10K


def get_target_sections(doc_type: str) -> list:
    """Return the appropriate section list based on document type."""
    if doc_type and "10-Q" in doc_type.upper().replace("10Q", "10-Q"):
        return TARGET_SECTIONS_10Q
    return TARGET_SECTIONS_10K

# Maximum chars to store per section (keeps cache files manageable)
MAX_SECTION_CHARS = 150_000


def discover_filings(ticker_filter=None, year_filter=None, doc_filter=None):
    """Discover all filing directories that contain filing.html."""
    filings_root = PROJECT_ROOT / "data" / "filings"
    found = []
    if not filings_root.exists():
        return found

    for ticker_dir in sorted(filings_root.iterdir()):
        if not ticker_dir.is_dir():
            continue
        ticker = ticker_dir.name
        if ticker_filter and ticker != ticker_filter.upper():
            continue

        for year_dir in sorted(ticker_dir.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            fiscal_year = int(year_dir.name)
            if year_filter and fiscal_year != year_filter:
                continue

            for doc_dir in sorted(year_dir.iterdir()):
                if not doc_dir.is_dir():
                    continue
                doc_type = doc_dir.name
                if doc_filter:
                    # Support matching "10-Q" against "10-Q-Q1", "10-Q-Q2" etc.
                    filter_upper = doc_filter.upper()
                    doc_upper = doc_type.upper()
                    if filter_upper != doc_upper and not doc_upper.startswith(filter_upper):
                        continue

                html_file = doc_dir / "filing.html"
                if html_file.exists():
                    found.append((ticker, fiscal_year, doc_type, html_file))

    return found


def extract_sections_from_html(html_path: Path, doc_type: str = "") -> dict:
    """
    Use EdgarTools HTMLParser to extract key SEC sections from a local filing.

    Returns: {section_key: text_content, ...}
    Falls back to extracting full text if sections aren't available.
    """
    try:
        from edgar.documents import HTMLParser
        parser = HTMLParser()
        doc = parser.parse_file(str(html_path))
    except Exception as e:
        print(f"    ERROR: Could not load EdgarTools HTMLParser: {e}")
        return {}

    sections = {}

    # Get available sections first
    available = []
    try:
        available = doc.get_available_sec_sections()
    except (AttributeError, Exception):
        pass  # older edgartools version — try anyway

    # Select appropriate section list based on doc type
    target_sections = get_target_sections(doc_type)

    # Extract each target section
    for section_key in target_sections:
        # If we know available sections and this one isn't listed, skip
        if available and section_key not in available:
            continue
        try:
            text = doc.get_sec_section(section_key)
            if text and len(text.strip()) > 200:
                truncated = text[:MAX_SECTION_CHARS]
                sections[section_key] = truncated
                print(f"    {section_key}: {len(text):,} chars"
                      + (f" (truncated to {MAX_SECTION_CHARS:,})" if len(text) > MAX_SECTION_CHARS else ""))
        except Exception:
            pass  # section not available in this filing

    # Fallback: if no sections found, extract full doc text
    if not sections:
        try:
            full_text = doc.text() if callable(doc.text) else doc.text
            if full_text and len(full_text.strip()) > 1000:
                truncated = full_text[:MAX_SECTION_CHARS]
                sections["full_text"] = truncated
                print(f"    full_text (fallback): {len(full_text):,} chars"
                      + (f" (truncated)" if len(full_text) > MAX_SECTION_CHARS else ""))
        except Exception as e:
            print(f"    ERROR extracting full text: {e}")

    return sections


def process_filing(ticker: str, fiscal_year: int, doc_type: str,
                   html_path: Path, force: bool = False):
    """Extract and cache section text for a single filing."""
    cache_path = html_path.parent / "SECTION_CACHE.json"

    if cache_path.exists() and not force:
        try:
            with open(cache_path) as f:
                existing = json.load(f)
            section_names = [k for k in existing if not k.startswith("_")]
            print(f"  [{ticker} {fiscal_year} {doc_type}] "
                  f"Already cached ({len(section_names)} sections) — use --force to re-extract")
            return
        except Exception:
            pass  # corrupted cache — re-extract

    print(f"  [{ticker} {fiscal_year} {doc_type}] "
          f"Extracting from {html_path.name}...")

    sections = extract_sections_from_html(html_path, doc_type=doc_type)

    if not sections:
        print(f"    WARNING: No section text extracted — skipping")
        return

    # Build cache file
    metadata = {
        "_metadata": {
            "ticker": ticker,
            "fiscal_year": fiscal_year,
            "doc_type": doc_type,
            "source_file": html_path.name,
            "sections_extracted": list(sections.keys()),
            "section_lengths": {k: len(v) for k, v in sections.items()},
        }
    }
    cache = {**metadata, **sections}

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    total_chars = sum(len(v) for v in sections.values())
    print(f"    Saved SECTION_CACHE.json "
          f"({total_chars:,} total chars across {len(sections)} section(s))")


def main():
    parser = argparse.ArgumentParser(
        description="Pre-extract SEC section text from local filing.html files"
    )
    parser.add_argument("ticker",      nargs="?", help="Ticker symbol (e.g. AAPL)")
    parser.add_argument("fiscal_year", nargs="?", type=int, help="Fiscal year (e.g. 2024)")
    parser.add_argument("doc_type",    nargs="?", help="Doc type (e.g. 10K)")
    parser.add_argument("--force", action="store_true",
                        help="Re-extract even if SECTION_CACHE.json already exists")
    args = parser.parse_args()

    filings = discover_filings(
        ticker_filter=args.ticker,
        year_filter=args.fiscal_year,
        doc_filter=args.doc_type,
    )

    if not filings:
        print("No filings with filing.html found.")
        return

    print(f"Processing {len(filings)} filing(s)...\n")
    for ticker, fiscal_year, doc_type, html_path in filings:
        process_filing(ticker, fiscal_year, doc_type, html_path, force=args.force)

    print("\nDone.")


if __name__ == "__main__":
    main()
