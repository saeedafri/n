"""
Filing Unit utilities for multi-filing documents.

These document types have multiple filings per year, stored in sub-folders:
  {ticker}/{year}/8-K/filing.html          (main filing)
  {ticker}/{year}/8-K/filing_94/filing.html  (filing unit 94)
  {ticker}/{year}/8-K/filing_95/filing.html  (filing unit 95)
  ...

The filing_unit column in coreiq_filing_metrics_v2 table stores these values.
"""
import os
import re
from typing import List, Optional, Tuple, Set
from pathlib import Path
import streamlit as st

# Document types that support filing units.
# DEF14A / DEFA14A can have multiple filings per year as filing_N folders,
# same behavior as 8-K / 6-K.
FILING_UNIT_DOC_TYPES = {"8-K", "6-K", "DEF14A", "DEFA14A", "DEF 14A", "PRE14A"}


def _get_blob_cache_base_path() -> str:
    """Get the base path for filings blob cache."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "data",
        "filings_blob_cache",
    )


def _get_filing_units_from_filesystem(ticker: str, year: str, doc_type: str) -> List[str]:
    """
    Get available filing units from filesystem (filings_blob_cache folder).
    This is a fallback when DB doesn't have the data.
    Only returns numbered filings (no "main").
    """
    base_path = _get_blob_cache_base_path()
    doc_path = os.path.join(base_path, ticker, year, doc_type)

    if not os.path.exists(doc_path):
        return []

    filing_units = []

    # Only check for sub-folders with filing_unit pattern (filing_XXX)
    # Skip "main" filing in root folder
    try:
        for item in os.listdir(doc_path):
            item_path = os.path.join(doc_path, item)
            if os.path.isdir(item_path) and re.match(r'^filing_\d+$', item):
                # Verify there's a filing.html inside
                if os.path.exists(os.path.join(item_path, "filing.html")):
                    filing_units.append(item)
    except (OSError, PermissionError):
        pass

    return filing_units


def _parse_filing_number(filing_unit: str) -> int:
    """Extract numeric value from filing_unit for sorting."""
    if filing_unit == "main":
        return 0
    match = re.match(r'^filing_(\d+)$', filing_unit)
    if match:
        return int(match.group(1))
    return 999999  # Unknown format goes last


def _sort_filing_units(filing_units: List[str]) -> List[str]:
    """Sort filing units: main first, then by number ascending."""
    return sorted(filing_units, key=_parse_filing_number)


def _sort_filing_units_desc(filing_units: List[str]) -> List[str]:
    """Sort filing units by number descending (newest/highest first), main last."""
    return sorted(filing_units, key=_parse_filing_number, reverse=True)


@st.cache_data(ttl=300, show_spinner=False)
def _get_filing_units_from_db(ticker: str, year: str, doc_type: str) -> Optional[List[str]]:
    """
    Get distinct filing units from database for a ticker/year/doc_type combination.

    Uses the idx_ticker_year_doc covering index for fast lookup.
    Returns None if no data found (caller should use filesystem fallback).

    Args:
        ticker: Company ticker symbol
        year: Fiscal year
        doc_type: Document type (e.g., '8-K', '6-K')

    Returns:
        List of filing unit names or None if no data
    """
    if not year or str(year).strip() == "":
        return None

    try:
        from core.database import db_manager

        # Use storage_year for 8-K/6-K (report_fiscal_year is NULL for those)
        rows = db_manager.execute_query_readonly("""
            SELECT DISTINCT filing_unit
            FROM coreiq_filing_metrics_v2
            WHERE ticker = :ticker
              AND storage_year = :year
              AND doc_type = :doc_type
              AND filing_unit IS NOT NULL
              AND filing_unit != ''
        """, {"ticker": ticker, "year": int(year), "doc_type": doc_type})

        if not rows:
            return None

        filing_units = [row["filing_unit"] for row in rows]

        # Filter out "main" - only show numbered filings from DB
        filing_units = [fu for fu in filing_units if fu != "main"]

        return filing_units if filing_units else None

    except Exception as e:
        # Log error but don't crash - fallback to filesystem
        import logging
        logging.getLogger(__name__).warning(f"DB filing_units query failed: {e}")
        return None


def get_filing_units(ticker: str, year: str, doc_type: str, use_cache: bool = True) -> List[str]:
    """
    Get available filing units for a ticker/year/doc_type combination.

    For 8-K and 6-K documents, multiple filings exist per year in sub-folders.
    This function checks the filesystem first (complete data), then
    falls back to database if needed.

    Args:
        ticker: Company ticker symbol
        year: Fiscal year
        doc_type: Document type (e.g., '8-K', '6-K')
        use_cache: Whether to use Streamlit cache for DB query (ignored, kept for compatibility)

    Returns:
        List of filing unit names (e.g., ['filing_1', 'filing_2', ...])
        Returns empty list if doc_type doesn't support filing units or no units found.
    """
    if doc_type not in FILING_UNIT_DOC_TYPES:
        return []

    if not year or str(year).strip() == "":
        return []

    # Use filesystem first - it has complete data (all filing units)
    filing_units = _get_filing_units_from_filesystem(ticker, year, doc_type)

    # Fallback to DB only if filesystem returns nothing
    if not filing_units:
        if use_cache:
            filing_units = _get_filing_units_from_db(ticker, year, doc_type)
        else:
            # Direct DB query without cache
            filing_units = _get_filing_units_from_db.__wrapped__(ticker, year, doc_type)

        if filing_units is None:
            filing_units = []

    # Sort by number ascending
    return _sort_filing_units(filing_units)


def get_default_filing_unit(filing_units: List[str]) -> str:
    """
    Get the default filing unit to select.

    Returns the highest filing number (most recent) as default.
    If only "main" exists, returns "main".

    Args:
        filing_units: List of available filing units

    Returns:
        The default filing unit (highest number, or "main" if that's all)
    """
    if not filing_units:
        return "main"

    # Sort descending (highest number first) and pick first
    sorted_desc = _sort_filing_units_desc(filing_units)
    return sorted_desc[0]


def get_filing_unit_display_name(filing_unit: str) -> str:
    """
    Get a user-friendly display name for a filing unit.

    Args:
        filing_unit: Filing unit name (e.g., 'filing_94')

    Returns:
        Display name (e.g., '94')
    """
    if filing_unit == "main":
        return "Main"  # Fallback, should not appear anymore

    match = re.match(r'^filing_(\d+)$', filing_unit)
    if match:
        # Return just the number (e.g., "94" instead of "Filing #94")
        return match.group(1)

    return filing_unit


def _sanitize_path_component(value: str) -> str:
    """Sanitize a path component to prevent directory traversal."""
    # Strip path separators and parent-dir references
    sanitized = value.replace("/", "").replace("\\", "").replace("..", "").strip()
    if not sanitized:
        raise ValueError(f"Invalid path component after sanitization: {value!r}")
    return sanitized


def build_filing_blob_path(ticker: str, year: str, doc_type: str, filing_unit: Optional[str] = None) -> Optional[str]:
    """
    Build the Azure blob path for a filing document.

    For multi-filing documents, includes the filing_unit in the path if specified.

    Args:
        ticker: Company ticker symbol
        year: Fiscal year
        doc_type: Document type (e.g., '8-K', '6-K', '10-K')
        filing_unit: Optional filing unit (e.g., 'main', 'filing_94').
                    For multi-filing doc types, defaults to 'main' if not specified.
                    Ignored for other doc types.

    Returns:
        Blob path string or None if invalid
    """
    # Sanitize all path components to prevent directory traversal
    try:
        safe_ticker = _sanitize_path_component(ticker)
        safe_year = _sanitize_path_component(year)
        safe_doc_type = _sanitize_path_component(doc_type)
    except ValueError:
        return None

    if doc_type not in FILING_UNIT_DOC_TYPES:
        # Standard path for non-filing-unit docs
        return f"{safe_ticker}/{safe_year}/{safe_doc_type}/filing.html"

    # For multi-filing doc types, include filing_unit in path
    if not filing_unit or filing_unit == "main":
        return f"{safe_ticker}/{safe_year}/{safe_doc_type}/filing.html"
    else:
        try:
            safe_unit = _sanitize_path_component(filing_unit)
        except ValueError:
            return None
        # filing_unit is like 'filing_94', path becomes: ticker/year/8-K/filing_94/filing.html
        return f"{safe_ticker}/{safe_year}/{safe_doc_type}/{safe_unit}/filing.html"


def get_local_filing_path(ticker: str, year: str, doc_type: str, filing_unit: Optional[str] = None) -> str:
    """
    Get the local cache path for a filing document.

    Args:
        ticker: Company ticker symbol
        year: Fiscal year
        doc_type: Document type
        filing_unit: Optional filing unit for 8-K/6-K documents

    Returns:
        Local file system path
    """
    base_path = _get_blob_cache_base_path()
    blob_path = build_filing_blob_path(ticker, year, doc_type, filing_unit)

    if blob_path:
        full_path = os.path.join(base_path, blob_path.replace("/", os.sep))
        # Verify the resolved path stays within the base cache directory
        if not os.path.realpath(full_path).startswith(os.path.realpath(base_path)):
            return os.path.join(base_path, "invalid", "filing.html")
        return full_path

    # Fallback — still sanitize
    try:
        safe_ticker = _sanitize_path_component(ticker)
        safe_year = _sanitize_path_component(year)
        safe_doc_type = _sanitize_path_component(doc_type)
    except ValueError:
        return os.path.join(base_path, "invalid", "filing.html")

    if doc_type in FILING_UNIT_DOC_TYPES and filing_unit and filing_unit != "main":
        try:
            safe_unit = _sanitize_path_component(filing_unit)
        except ValueError:
            return os.path.join(base_path, "invalid", "filing.html")
        return os.path.join(base_path, safe_ticker, safe_year, safe_doc_type, safe_unit, "filing.html")
    return os.path.join(base_path, safe_ticker, safe_year, safe_doc_type, "filing.html")


def has_filing_units(ticker: str, year: str, doc_type: str) -> bool:
    """
    Check if a ticker/year/doc_type combination has filing units.

    Args:
        ticker: Company ticker symbol
        year: Fiscal year
        doc_type: Document type

    Returns:
        True if filing units exist for this combination
    """
    if doc_type not in FILING_UNIT_DOC_TYPES:
        return False

    units = get_filing_units(ticker, year, doc_type)
    return len(units) > 0


def get_filing_unit_for_display(ticker: str, year: str, doc_type: str) -> Optional[str]:
    """
    Get filing unit info for document header display.

    Args:
        ticker: Company ticker symbol
        year: Fiscal year
        doc_type: Document type

    Returns:
        Filing unit display string or None if not applicable
    """
    if doc_type not in FILING_UNIT_DOC_TYPES:
        return None

    # Get filing units and return default (highest number)
    units = get_filing_units(ticker, year, doc_type)
    if not units:
        return None

    default_unit = get_default_filing_unit(units)
    if default_unit == "main":
        return None  # Don't show for main filing

    return get_filing_unit_display_name(default_unit)
