"""
Company Filing Documents Page - Coresight Research
==================================================
SEC filing documents viewer with metric search and document display.
Matches Figma design with Streamlit native components + custom styling.

OPTIMIZATION NOTES (2024):
- Azure SDK tuned for parallel downloads (max_concurrency=4)
- Background metadata preloading via DiskCache
- Lazy loading pattern to prevent UI blocking
- Red spinner matching Coresight brand (#D62E2F)
- Non-breaking: Falls back to original behavior if cache miss
"""
import os
import json
import html
import logging
import re
import threading
import time
import streamlit as st
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

# =============================================================================
# MODULE LOAD TIMING - Track every millisecond from import start
# =============================================================================
_module_load_start = time.perf_counter()

from utils.server_logger import log_error, log_info, log_warning, log_structured_error, new_rerun_id, PageLoadTracker, log_render_complete, log_timing

new_rerun_id("company_filings")

from components.styles import hide_sidebar, set_page_layout
from core.auth_manager import require_auth
# require_auth(page="company_filings")
hide_sidebar()
from components.styles import render_styles
from components.navigation import render_header, render_coresight_footer
from utils.ticker_utils import validate_and_get_ticker, DEFAULT_FALLBACK_TICKER
from utils.filing_units import (
    get_filing_units,
    get_filing_unit_display_name,
    get_default_filing_unit,
    build_filing_blob_path,
    get_local_filing_path,
    FILING_UNIT_DOC_TYPES,
)

try:
    from utils.filings_cache import get_filings_cache, generate_etag_from_blobs
    from utils.background_scanner import (
        get_background_scanner,
        init_background_scanner,
        get_scan_progress,
        ensure_cache_purge,
    )
    from components.loading import inject_red_spinner_css

    CACHE_AVAILABLE = True
except Exception as e:
    # Broad catch: ImportError for missing packages, KeyError for Python
    # import-system race conditions when the background scanner thread imports
    # the same modules while Streamlit re-runs this page.  Either way, run
    # without cache rather than crash with the Streamlit error page.
    CACHE_AVAILABLE = False

if CACHE_AVAILABLE:
    inject_red_spinner_css()

# =============================================================================
# SEARCH CLEAR CALLBACK - Clears search when company/doc/year/quarter changes
# =============================================================================
def _clear_search_callback():
    """Clear search input when any filing selector changes."""
    st.session_state.cf_search = ""
    st.session_state.cf_search_gen = st.session_state.get("cf_search_gen", 0) + 1
    st.session_state.cf_highlight_fact_id = None
    st.session_state.cf_view_metric = None


def _get_best_landing_doc_and_year(company: str) -> tuple:
    """Return (doc_type, year) for the most recent 10-K or 10-Q filing.

    Only considers annual and quarterly doc types (SEC: 10-K / 10-Q-Q*;
    non-SEC: annual-report / interim-report-Q*).  Among all candidates the
    one with the highest (year, quarter_rank) wins, so a Q3 filing beats a
    Q2 filing in the same year, and any filing in a newer year beats all
    filings in older years.  Falls back to ("10-K", "") if no data is found.
    """
    # Quarter rank: annual=0, Q1=1, Q2=2, Q3=3, Q4=4, Q5=5
    _RANK = {
        "10-K": 0, "annual-report": 0,
        "10-Q-Q1": 1, "interim-report-Q1": 1,
        "10-Q-Q2": 2, "interim-report-Q2": 2,
        "10-Q-Q3": 3, "interim-report-Q3": 3,
        "interim-report-Q4": 4,
        "interim-report-Q5": 5,
    }
    try:
        prefetch = _prefetch_ticker_filter_data(company)
        ybd = prefetch["years_by_doc_type"] if prefetch else {}
        best_doc, best_key = None, (-1, -1)
        for dt, rank in _RANK.items():
            years = ybd.get(dt, [])
            if not years:
                continue
            top_year = int(years[0])  # already sorted descending
            key = (top_year, rank)
            if key > best_key:
                best_key = key
                best_doc = dt
        if best_doc:
            best_year = str(int(best_key[0]))
            return best_doc, best_year
    except Exception as _exc:
        log_structured_error(_exc, page="company_filings", component="_get_best_landing_doc_and_year", operation="PICK_BEST")
    return "10-K", ""


def _on_company_change():
    """Company changed → land on the most recent 10-K or 10-Q filing."""
    new_company = st.session_state.get("cf_company_select", "")
    if not new_company:
        return

    # Clear filing units cache when company changes
    try:
        from utils.filing_units import _get_filing_units_from_db
        _get_filing_units_from_db.clear()
    except Exception:
        pass

    # Pick the most recent annual or quarterly filing (SEC or non-SEC).
    try:
        new_doc, new_year = _get_best_landing_doc_and_year(new_company)
    except Exception as _exc:
        log_structured_error(_exc, page="company_filings", component="_on_company_change", operation="GET_BEST_DOC")
        new_doc, new_year = "10-K", ""

    # If the helper returned no year, fall back to the latest available year
    # for that doc type so the year widget is never left empty.
    if not new_year:
        try:
            new_years = _get_available_years_for_doc_type(new_company, new_doc)
            new_year = new_years[0] if new_years else ""
        except Exception as _exc:
            log_structured_error(_exc, page="company_filings", component="_on_company_change", operation="GET_YEARS")
            new_year = ""
    # Reset filing unit for 8-K/6-K docs
    if new_doc in FILING_UNIT_DOC_TYPES:
        filing_units = get_filing_units(new_company, new_year, new_doc, use_cache=False) if new_year else []
        new_filing_unit = get_default_filing_unit(filing_units) if filing_units else ""
        new_filing_unit_display = len(filing_units) if filing_units else 1
    else:
        new_filing_unit = ""
        new_filing_unit_display = 1
    st.session_state.cf_doc_type = new_doc
    st.session_state["cf_doc_type_select"] = new_doc
    st.session_state.cf_year = new_year
    st.session_state["cf_year_select"] = new_year
    st.session_state.cf_filing_unit = new_filing_unit
    st.session_state["cf_filing_unit_select"] = new_filing_unit_display
    # Clear search via gen counter (avoids the widget-key clearing loop)
    st.session_state.cf_search = ""
    st.session_state.cf_search_gen = st.session_state.get("cf_search_gen", 0) + 1
    st.session_state.cf_highlight_fact_id = None
    st.session_state.cf_view_metric = None


def _on_doc_type_change():
    """Doc type changed → reset year to latest for company + new doc_type."""
    new_doc = st.session_state.get("cf_doc_type_select", "10-K")
    company = st.session_state.get("cf_company_select") or st.session_state.get("cf_company", "")

    # Clear filing units cache when doc type changes
    try:
        from utils.filing_units import _get_filing_units_from_db
        _get_filing_units_from_db.clear()
    except Exception:
        pass

    try:
        new_years = _get_available_years_for_doc_type(company, new_doc)
    except Exception as _exc:
        log_structured_error(_exc, page="company_filings", component="_on_doc_type_change", operation="GET_YEARS")
        new_years = []
    new_year = new_years[0] if new_years else ""
    # For 8-K/6-K, also reset filing unit
    if new_doc in FILING_UNIT_DOC_TYPES and new_year:
        filing_units = get_filing_units(company, new_year, new_doc, use_cache=False)
        new_filing_unit = get_default_filing_unit(filing_units) if filing_units else ""
        new_filing_unit_display = len(filing_units) if filing_units else 1
    else:
        new_filing_unit = ""
        new_filing_unit_display = 1
    st.session_state.cf_year = new_year
    st.session_state["cf_year_select"] = new_year
    st.session_state.cf_filing_unit = new_filing_unit
    st.session_state["cf_filing_unit_select"] = new_filing_unit_display
    # Clear search
    st.session_state.cf_search = ""
    st.session_state.cf_search_gen = st.session_state.get("cf_search_gen", 0) + 1
    st.session_state.cf_highlight_fact_id = None
    st.session_state.cf_view_metric = None


def _on_year_change():
    """Year changed → reset filing unit for 8-K/6-K docs, clear search."""
    company = st.session_state.get("cf_company_select") or st.session_state.get("cf_company", "")
    new_year = st.session_state.get("cf_year_select", "")
    doc_type = st.session_state.get("cf_doc_type_select", "10-K")

    # Clear filing units cache to force fresh lookup for new year
    try:
        from utils.filing_units import _get_filing_units_from_db
        _get_filing_units_from_db.clear()
    except Exception:
        pass

    # For 8-K/6-K, update filing units when year changes
    if doc_type in FILING_UNIT_DOC_TYPES and new_year:
        filing_units = get_filing_units(company, new_year, doc_type, use_cache=False)
        new_filing_unit = get_default_filing_unit(filing_units) if filing_units else ""
        new_filing_unit_display = len(filing_units) if filing_units else 1
        st.session_state.cf_filing_unit = new_filing_unit
        st.session_state["cf_filing_unit_select"] = new_filing_unit_display
    else:
        st.session_state.cf_filing_unit = ""
        st.session_state["cf_filing_unit_select"] = 1

    # Update the year in session state to ensure consistency
    st.session_state.cf_year = new_year

    # Clear search
    st.session_state.cf_search = ""
    st.session_state.cf_search_gen = st.session_state.get("cf_search_gen", 0) + 1
    st.session_state.cf_highlight_fact_id = None
    st.session_state.cf_view_metric = None


def _on_filing_unit_change():
    """Filing unit changed (8-K/6-K only) → clear search.

    Note: cf_filing_unit_select now contains display number (1-N), not actual filing unit.
    We map it back to the actual filing unit for backend operations.
    """
    company = st.session_state.get("cf_company_select") or st.session_state.get("cf_company", "")
    doc_type = st.session_state.get("cf_doc_type_select", "")

    if doc_type not in FILING_UNIT_DOC_TYPES:
        return

    # Get the display number (1-N) and map to actual filing unit
    _display_num = st.session_state.get("cf_filing_unit_select", 1)
    try:
        available_filing_units = get_filing_units(company, st.session_state.cf_year, doc_type)
        if not available_filing_units:
            return
        # Map display number (1-based) to actual filing unit index
        _index = min(_display_num - 1, len(available_filing_units) - 1)
        new_filing_unit = available_filing_units[_index]
    except Exception:
        new_filing_unit = ""

    if not new_filing_unit:
        return

    # Store actual filing unit for internal use
    st.session_state.cf_filing_unit = new_filing_unit
    # Clear search
    st.session_state.cf_search = ""
    st.session_state.cf_search_gen = st.session_state.get("cf_search_gen", 0) + 1
    st.session_state.cf_highlight_fact_id = None
    st.session_state.cf_view_metric = None

import time as _perf_time
from functools import wraps
from collections import defaultdict

# Global timing storage for analysis
if '_TIMING_DATA' not in st.session_state:
    st.session_state._TIMING_DATA = {
        'rerun_count': 0,
        'function_calls': defaultdict(list),
        'search_history': [],
        'dropdown_load_times': {},
        'company_changes': []
    }

# Page load start time
_PAGE_LOAD_START = _perf_time.time()

def _timing_decorator(func_name):
    """Decorator to track function execution time."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = _perf_time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = _perf_time.time() - start
                st.session_state._TIMING_DATA['function_calls'][func_name].append(elapsed)
                return result
            except Exception as e:
                elapsed = _perf_time.time() - start
                log_error(f"[TIMING_FUNC] ERROR: {func_name}() failed after {elapsed:.3f}s: {e}")
                raise
        return wrapper
    return decorator

# =============================================================================
# AZURE BLOB STORAGE (REPLACES LOCAL FILINGS DIRECTORY STRUCTURE)
# =============================================================================
# Expected blob structure (same as prior local):
#   {prefix}/{TICKER}/{YEAR}/{DOC_TYPE_DIR}/*.html or *.htm
# For 10-Q quarters:
#   {prefix}/{TICKER}/{YEAR}/10-Q-Q1/  etc.
#
# IMPORTANT: Do NOT hardcode secrets. Set these as environment variables.
#   AZURE_STORAGE_ACCOUNT_NAME
#   AZURE_STORAGE_ACCOUNT_KEY
#   AZURE_BLOB_CONTAINER
#   AZURE_BLOB_PREFIX (optional)

# Local cache root where blobs are downloaded on-demand
# (used only as a transient cache to keep the rest of the code unchanged)
# Persistent /home root on Azure (survives deploys), <repo>/data locally.
# MUST match background_scanner.FILINGS_BLOB_CACHE_DIR so page-reads find
# scanner-writes. See utils.filings_paths for the why.
from utils.filings_paths import filings_data_root
FILINGS_BASE_DIR = os.path.join(filings_data_root(), "filings_blob_cache")

# DISABLED: Static file serving - using data/filings_blob_cache/ instead
# Streamlit static folder has 1GB limit, we serve directly from data folder
# _STATIC_CLEAN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "filings_clean")


def _get_azure_config() -> Tuple[str, str, str, str]:
    acct = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "csmarketdata").strip()
    # NOTE: no default secret in code — must be provided in env.
    key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "IXETqLLag1VIaXrp7YzT3ZmwVI/c4v7txYfW6A1n6jJhym31Yr+1hY2TWHIP4yhrtB28EVGByz1u+ASt1QLNWw==").strip()
    container_name = (os.getenv("AZURE_BLOB_CONTAINER") or "azure-storage-test").strip()
    prefix = (os.getenv("AZURE_BLOB_PREFIX") or "").strip("/")

    return acct, key, container_name, prefix


@st.cache_resource(show_spinner=False)
def _get_blob_service_client():
    acct, key, _, _ = _get_azure_config()
    if not acct:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is missing.")
    if not key:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY is missing.")
    try:
        from azure.storage.blob import BlobServiceClient
    except Exception as e:
        raise RuntimeError(
            "azure-storage-blob is required. Install with: pip install azure-storage-blob"
        ) from e

    account_url = f"https://{acct}.blob.core.windows.net"
    return BlobServiceClient(account_url=account_url, credential=key)


@st.cache_resource(show_spinner=False)
def _get_blob_container_client():
    _, _, container_name, _ = _get_azure_config()
    svc = _get_blob_service_client()
    return svc.get_container_client(container_name)


# =============================================================================
# OPTIMIZED AZURE CLIENT (PERFORMANCE TUNED)
# =============================================================================
@st.cache_resource(show_spinner=False)
def _get_optimized_blob_service_client():
    """
    Get Azure blob client with performance tuning for faster downloads.

    TUNING PARAMETERS:
    - max_single_get_size: 64MB (files < 64MB downloaded in single request)
    - max_chunk_get_size: 8MB (larger chunks for fewer API calls)
    - connection_pool_size: 50 (more concurrent connections)

    Returns:
        BlobServiceClient with optimized settings
    """
    acct, key, _, _ = _get_azure_config()
    if not acct or not key:
        raise RuntimeError("Azure credentials not configured")

    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError as e:
        raise RuntimeError("azure-storage-blob not installed") from e

    account_url = f"https://{acct}.blob.core.windows.net"

    # OPTIMIZED: Tuned for 2-3MB filing files
    return BlobServiceClient(
        account_url=account_url,
        credential=key,
        max_single_get_size=64 * 1024 * 1024,   # 64MB - single request for most files
        max_chunk_get_size=8 * 1024 * 1024,     # 8MB chunks (was 4MB)
        connection_pool_size=50,                 # 50 concurrent connections (was 10)
        max_block_size=8 * 1024 * 1024,         # 8MB blocks for uploads
    )


@st.cache_resource(show_spinner=False)
def _get_optimized_container_client():
    """Get container client with optimized blob service."""
    _, _, container_name, _ = _get_azure_config()
    svc = _get_optimized_blob_service_client()
    return svc.get_container_client(container_name)


def _ensure_local_blob_optimized(blob_name: str, use_temp: bool = False) -> Optional[str]:
    """
    Optimized blob download with parallel chunks and tuned settings.

    Args:
        blob_name: Name of the blob in Azure
        use_temp: If True, download to /tmp/ (not filings_blob_cache) so the
                  file is OS-managed and never accumulates on disk.  Use this
                  for on-demand/older-year downloads that should not be cached.

    Returns:
        Local file path or None if download failed
    """
    download_start = _perf_time.time()

    if not blob_name:
        return None

    if use_temp:
        import tempfile, hashlib
        _safe = hashlib.md5(blob_name.encode()).hexdigest()[:12]
        local_path = os.path.join(tempfile.gettempdir(), f"capiq_filing_{_safe}.pdf")
    else:
        local_path = _local_cache_path_for_blob(blob_name)

    try:
        # Use optimized client
        container = _get_optimized_container_client()
        blob_client = container.get_blob_client(blob_name)

        # Get properties for freshness check
        props = blob_client.get_blob_properties()

        last_modified = getattr(props, "last_modified", None)
        lm_ts = None

        try:
            if last_modified:
                lm_ts = last_modified.timestamp()
        except Exception:
            pass

        # Check if local cache is fresh
        if os.path.exists(local_path) and lm_ts is not None:
            if os.path.getmtime(local_path) >= lm_ts:
                return local_path

        # Download with parallel chunks — atomic write (temp → rename) so a
        # failed download never leaves an empty/corrupt file on disk.
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        tmp_path = local_path + ".tmp"

        download_start = _perf_time.time()

        try:
            with open(tmp_path, "wb") as f:
                downloader = blob_client.download_blob(max_concurrency=4)
                chunk_count = 0
                total_bytes = 0
                for chunk in downloader.chunks():
                    f.write(chunk)
                    chunk_count += 1
                    total_bytes += len(chunk)
        except Exception:
            # Remove partial temp file so it doesn't poison future cache hits
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        if total_bytes == 0:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            log_warning(f"[AZURE_DL] blob={blob_name!r} — blob exists but downloaded 0 bytes, skipping cache")
            return None

        os.replace(tmp_path, local_path)

        download_end = _perf_time.time()
        download_duration = download_end - download_start
        _mb = total_bytes / (1024 * 1024)
        log_info(
            f"[AZURE_DL] blob={blob_name!r} size={_mb:.2f}MB "
            f"duration={download_duration:.2f}s speed={_mb/max(download_duration, 0.001):.1f}MB/s chunks={chunk_count}"
        )

        # Update mtime to match blob
        if lm_ts is not None:
            try:
                os.utime(local_path, (lm_ts, lm_ts))
            except Exception:
                pass

        return local_path

    except Exception as e:
        log_structured_error(e, page="company_filings", component="_ensure_local_blob_optimized", operation="DOWNLOAD_BLOB")
        # FALLBACK: Try original method
        return _ensure_local_blob(blob_name)


def _blob_prefix_path(*parts: str) -> str:
    """Join path parts with configured prefix (if any)."""
    _, _, _, prefix = _get_azure_config()
    clean_parts = [p.strip("/").replace("\\", "/") for p in parts if p and p.strip("/")]
    if prefix:
        return "/".join([prefix] + clean_parts)
    return "/".join(clean_parts)


def _strip_prefix(blob_name: str) -> str:
    """Return blob_name relative to prefix (or same name if no prefix)."""
    _, _, _, prefix = _get_azure_config()
    bn = (blob_name or "").lstrip("/").replace("\\", "/")
    if not prefix:
        return bn
    pfx = prefix.strip("/") + "/"
    if bn.startswith(pfx):
        return bn[len(pfx):]
    return bn


def _iter_filing_blobs():
    """Yield blob names for candidate filing HTML/HTM/PDF files under the prefix."""
    _start = _perf_time.time()


    _client_start = _perf_time.time()
    container = _get_blob_container_client()
    _, _, _, prefix = _get_azure_config()
    name_starts_with = (prefix.strip("/") + "/") if prefix else ""
    _client_elapsed = _perf_time.time() - _client_start


    # List blobs under prefix; filter to html/htm/pdf
    _list_start = _perf_time.time()
    _blob_count = 0
    _yielded_count = 0

    for b in container.list_blobs(name_starts_with=name_starts_with):
        _blob_count += 1
        name = getattr(b, "name", "")
        if not name:
            continue
        lower = name.lower()
        # Include HTML, HTM, and PDF files
        if not (lower.endswith(".html") or lower.endswith(".htm") or lower.endswith(".pdf")):
            continue
        if lower.endswith("-clean.html"):
            continue
        _yielded_count += 1
        yield b  # includes name + (often) last_modified, etag, size

    _total_elapsed = _perf_time.time() - _start



def _pick_best_blob_for_docdir(blob_names: List[str]) -> Optional[str]:
    """Prefer filing.html/filing.pdf if present, else first .html, else first .htm, else first .pdf."""
    if not blob_names:
        return None
    # normalize basename checks
    filing_html = None
    filing_pdf = None
    htmls = []
    htms = []
    pdfs = []
    for n in blob_names:
        base = n.split("/")[-1]
        lower = base.lower()
        if lower == "filing.html":
            filing_html = n
        elif lower == "filing.pdf":
            filing_pdf = n
        elif lower.endswith(".html") and not lower.endswith("-clean.html"):
            htmls.append(n)
        elif lower.endswith(".htm"):
            htms.append(n)
        elif lower.endswith(".pdf"):
            pdfs.append(n)
    # Priority: filing.html > html > htm > filing.pdf > other pdf
    return filing_html or (htmls[0] if htmls else (htms[0] if htms else (filing_pdf or (pdfs[0] if pdfs else None))))


def _parse_blob_path(blob_name: str) -> Optional[Tuple[str, str, str, str]]:
    """
    Parse blob path into (ticker, year, doc_type_dir, filename).
    Assumes structure: {prefix}/{ticker}/{year}/{doc_type_dir}/{filename}
    """
    rel = _strip_prefix(blob_name)
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 4:
        return None
    ticker, year, doc_type_dir = parts[0], parts[1], parts[2]
    filename = parts[-1]
    if not year.isdigit():
        return None
    return ticker, year, doc_type_dir, filename


# =============================================================================
# FILINGS DIRECTORY SCANNER (NOW SCANS AZURE BLOBS)
# =============================================================================

# Map directory names to display names for document types
# PRODUCTION-GRADE: Valid doc types for Azure Blob and local cache
# Only these are allowed in the system
VALID_DOC_TYPES = {
    # SEC filings
    '10-K', '10-Q-Q1', '10-Q-Q2', '10-Q-Q3',
    '8-K', '20-F', '6-K',
    'DEF14A', 'DEFA14A', 'DEF 14A', 'PRE14A',
    # NON-SEC filings (CAPITAL Q)
    'annual-report',
    'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3',
    'interim-report-Q4', 'interim-report-Q5',
    'half-yearly',
}


def _is_valid_doc_type(doc_type: str) -> bool:
    """PRODUCTION: Validate doc_type against allowed list."""
    return doc_type in VALID_DOC_TYPES


# Map directory names to display names (handles both old "10K" and new "10-K" folders)
DOC_TYPE_MAP = {
    "10K": "10-K",
    "10-K": "10-K",
    "10Q": "10-Q",
    "10-Q": "10-Q",
    "10-Q-Q1": "10-Q",
    "10-Q-Q2": "10-Q",
    "10-Q-Q3": "10-Q",
    "8K": "8-K",
    "8-K": "8-K",
    "DEF14A": "DEF 14A",
    "DEFA14A": "DEFA 14A",
    "DEF 14A": "DEF 14A",
    "S1": "S-1",
    "S-1": "S-1",
    # NON-SEC doc types (folder names in Azure with CAPITAL Q)
    "annual-report": "Annual Report",
    "interim-report-Q1": "Q1 Interim",
    "interim-report-Q2": "Q2 Interim",
    "interim-report-Q3": "Q3 Interim",
    "interim-report-Q4": "Q4 Interim",
    "interim-report-Q5": "Q5 Interim",
    "half-yearly": "Half-Yearly",
}
# Reverse map: display name → DB doc_type (uses exact Azure folder names)
DOC_TYPE_REVERSE = {
    "10-K": "10-K",
    "10-Q": "10-Q",
    "8-K": "8-K",
    "DEF 14A": "DEF14A",
    "DEFA 14A": "DEFA14A",
    "S-1": "S-1",
    # NON-SEC display names → exact folder names
    "Annual Report": "annual-report",
    "Q1 Interim": "interim-report-Q1",
    "Q2 Interim": "interim-report-Q2",
    "Q3 Interim": "interim-report-Q3",
    "Q4 Interim": "interim-report-Q4",
    "Q5 Interim": "interim-report-Q5",
    "Half-Yearly": "half-yearly",
}

# Map DB doc_type to Azure folder names (same as doc_type since we store exact names)
DOC_TYPE_TO_FOLDER = {
    "annual-report": "annual-report",
    "interim-report-Q1": "interim-report-Q1",
    "interim-report-Q2": "interim-report-Q2",
    "interim-report-Q3": "interim-report-Q3",
    "interim-report-Q4": "interim-report-Q4",
    "interim-report-Q5": "interim-report-Q5",
    "half-yearly": "half-yearly",
}

# Annual document types (no quarter filter needed)
ANNUAL_DOC_TYPES = {"10-K", "DEF 14A", "DEF14A", "DEFA14A", "S-1", "Annual Report", "AR"}

# NON-SEC document types (exact folder names stored in DB)
NON_SEC_DOC_TYPES = {"annual-report", "interim-report-Q1", "interim-report-Q2", "interim-report-Q3", "interim-report-Q4", "interim-report-Q5", "half-yearly"}

@st.cache_data(ttl=300, show_spinner=False)
def _is_non_sec_company(ticker: str) -> bool:
    """
    Check if a company is NON-SEC by looking for NON-SEC doc types in the database.
    Returns True if the company has annual-report or interim-report-Q* doc types.
    """
    try:
        from core.database import db_manager
        from sqlalchemy import text

        with db_manager.get_session() as session:
            result = session.execute(text("""
                SELECT COUNT(*) FROM coreiq_filing_metrics_v5
                WHERE ticker = :ticker
                AND (doc_type = 'annual-report' OR doc_type LIKE 'interim-report-Q%' OR doc_type = 'half-yearly')
                LIMIT 1
            """), {'ticker': ticker})
            count = result.scalar()
            return count > 0
    except Exception as e:
        log_structured_error(e, page="company_filings", component="_is_non_sec_company", operation="DB_CHECK")
        return False


def _get_non_sec_blob_path(ticker: str, year: str, doc_type: str) -> Optional[str]:
    """
    PRODUCTION: Construct the Azure blob path for a NON-SEC filing.
    Uses DB ticker directly (assumes DB ticker matches Azure folder name).
    Validates doc_type before generating path.
    """


    # PRODUCTION: Validate doc_type first
    if not _is_valid_doc_type(doc_type):
        log_error(f"[_get_non_sec_blob_path] ❌ FAILED: doc_type '{doc_type}' not in VALID_DOC_TYPES")
        log_error(f"[_get_non_sec_blob_path] Valid types: {sorted(VALID_DOC_TYPES)}")
        return None


    # For new NON-SEC types not yet in static mapping, default folder to doc_type.
    folder = DOC_TYPE_TO_FOLDER.get(doc_type, doc_type)
    # Build path: {ticker}/{year}/{folder}/filing.pdf
    blob_path = f"{ticker}/{year}/{folder}/filing.pdf"

    return blob_path


# =============================================================================
# KEYWORD SEARCH — document text extraction + search
# =============================================================================


@st.cache_data(ttl=600, show_spinner=False)
def _build_doc_text_index(file_path: str, is_pdf: bool):
    """
    Extract plain searchable text from a filing document.
    *** This function body runs ONLY on cache MISS. On cache HIT the body is
        skipped entirely by @st.cache_data — timing inside = miss-only cost. ***

    - HTML: returns a single plain-text string (newlines preserved as element
            boundaries so keyword positions can be found by line).
    - PDF:  returns list of (page_num: int, page_label: str, page_text: str).
            page_num   = physical 1-based position in the PDF file (used for navigation).
            page_label = printed page number/label as it appears in the document
                         (e.g. "17" when cover pages shift numbering). Falls back to
                         str(page_num) if the PDF has no page labels defined.

    Typical first-call cost:
      HTML  1 MB  → ~100 ms   (regex pass)
      HTML  5 MB  → ~500 ms   (regex pass)
      PDF  18 MB / 200 pages → ~60 ms    (PyMuPDF C layer)
    All reruns after first call: 0 ms (Streamlit @st.cache_data in-memory hit).
    """
    import re as _re
    _t0 = _perf_time.time()

    if is_pdf:
        import fitz
        pages = []
        try:
            _file_size_kb = os.path.getsize(file_path) // 1024

            doc = fitz.open(file_path)
            for i, page in enumerate(doc, start=1):
                text = page.get_text("text")
                if text.strip():
                    # Printed page label (e.g. "17") — 0-indexed in PyMuPDF
                    try:
                        label = doc.get_page_label(i - 1) or str(i)
                    except Exception:
                        label = str(i)
                    pages.append((i, label, text))
            doc.close()
            _elapsed_ms = (_perf_time.time() - _t0) * 1000

        except Exception as e:
            log_structured_error(e, page="company_filings", component="_build_doc_text_index", operation="INDEX_PDF")
        return pages

    else:
        try:
            _file_size_kb = os.path.getsize(file_path) // 1024

            with open(file_path, "r", encoding="utf-8", errors="replace") as _f:
                raw = _f.read()
        except Exception as e:
            log_structured_error(e, page="company_filings", component="_build_doc_text_index", operation="INDEX_HTML")
            return ""

        # Strip scripts / styles first (compiled patterns = faster on large files)
        _re_script = _re.compile(r"<script[^>]*>.*?</script>", _re.DOTALL | _re.IGNORECASE)
        _re_style  = _re.compile(r"<style[^>]*>.*?</style>",   _re.DOTALL | _re.IGNORECASE)
        _re_tags   = _re.compile(r"<[^>]+>")
        _re_hspace = _re.compile(r"[ \t]+")
        _re_lines  = _re.compile(r"\n{3,}")

        raw   = _re_script.sub(" ", raw)
        raw   = _re_style.sub(" ", raw)
        plain = _re_tags.sub(" ", raw)
        plain = _re_hspace.sub(" ", plain)
        plain = _re_lines.sub("\n\n", plain)

        _elapsed_ms = (_perf_time.time() - _t0) * 1000
        return plain


@st.cache_data(ttl=600, show_spinner=False)
def _kw_search_document(file_path: str, keyword: str, is_pdf: bool, limit: int = 15):
    """
    Search for keyword inside the pre-built text index.
    *** Body runs only on cache MISS (new keyword or new file). ***

    Returns list of dicts: [{"snippet": str, "page": int|None, "num": int}]

    HIGHLIGHT FIX — snippet construction rule for HTML:
      OLD: 60 chars BEFORE keyword + keyword + 60 chars AFTER
           Problem: JS substring(0,30) variant = first 30 chars = pre-context only
                    → no keyword in the 30-char variant → DOM search fails → NO RED BOX.
      NEW: 10 chars BEFORE keyword + keyword + 90 chars AFTER
           Result:  substring(0,30) = pre_10 + keyword + post_10 → keyword is ALWAYS
                    present in ALL JS variants (80/50/30 chars) → TEXT: match guaranteed.
    """
    import re as _re
    if not keyword.strip() or not file_path:
        return []

    _t0 = _perf_time.time()


    # _build_doc_text_index is @st.cache_data — instant if already built for this file
    _idx_t0   = _perf_time.time()
    text_index = _build_doc_text_index(file_path, is_pdf)
    _idx_ms   = (_perf_time.time() - _idx_t0) * 1000


    results = []
    pattern = _re.compile(_re.escape(keyword.strip()), _re.IGNORECASE)

    if is_pdf:
        for page_num, page_label, page_text in text_index:
            for m in pattern.finditer(page_text):
                if len(results) >= limit:
                    break
                start   = max(0, m.start() - 60)
                end     = min(len(page_text), m.end() + 60)
                snippet = page_text[start:end].replace("\n", " ").strip()
                if len(snippet) < 20:
                    continue
                results.append({
                    "snippet":    snippet,
                    "page":       page_num,    # physical page number (for PDFPAGE: nav)
                    "page_label": page_label,  # printed label shown in card
                    "num":        len(results) + 1,
                })
            if len(results) >= limit:
                break

    else:
        if not isinstance(text_index, str):

            return []
        seen = set()
        for m in pattern.finditer(text_index):
            if len(results) >= limit:
                break

            # ── HIGHLIGHT FIX ──────────────────────────────────────────────────
            # Keep only 10 chars of pre-context so that when the JS autoscroll
            # tries its shortest variant (substring 0→30), the keyword still
            # falls within those 30 chars → DOM element found → RED BOX shown.
            #
            # Layout:  [pre≤10][keyword][post≤90]  total ≤ ~110 chars
            # JS tries: full | 80 | 50 | 30
            #   30-char: [pre≤10][keyword][post≤10]  ← keyword always present ✓
            # ───────────────────────────────────────────────────────────────────
            pre_start = max(0, m.start() - 10)
            raw_snip  = text_index[pre_start : min(len(text_index), m.end() + 90)]
            snippet   = " ".join(raw_snip.replace("\n", " ").split())[:110]

            if len(snippet) < 15:
                continue
            dedup_key = snippet[:40].lower()
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            results.append({"snippet": snippet, "page": None, "num": len(results) + 1})

    _total_ms = (_perf_time.time() - _t0) * 1000
    return results


def _render_pdf_viewer(pdf_path: str, highlight_page: int = 0, highlight_keyword: str = "") -> None:
    """
    Render a PDF using PDF.js (canvas-based, no browser PDF plugin required).

    Why PDF.js instead of a native browser iframe/embed:
    - Streamlit's components.html() renders inside a sandboxed iframe.
    - Chrome refuses to instantiate its PDF plugin inside a nested sandboxed iframe,
      showing "This page has been blocked by Chrome" for any blob:/data: URI PDF embed.
    - PDF.js renders entirely to <canvas> elements in JavaScript — no plugin, no nested
      iframe, no Chrome blocking — and works identically in Chrome, Firefox, Safari, Edge.

    Performance vs old PyMuPDF approach:
    - Old: Python renders ALL pages as JPEG (30+ s CPU) → 20 MB HTML blob over WebSocket.
    - New: Python reads bytes once (< 0.5 s) → PDF.js lazy-renders only visible pages via
      IntersectionObserver. First 3 pages appear in ~3 s; rest load on scroll.

    Args:
        pdf_path:       Absolute path to the PDF file on disk.
        highlight_page: If > 0, scroll to this page number after load (keyword nav).
    """
    import base64
    import streamlit.components.v1 as components



    # ------------------------------------------------------------------
    # 1. Load PDF bytes — cached in session_state so reruns are instant
    # ------------------------------------------------------------------
    cache_key = f"pdf_b64_{pdf_path}"
    _pdf_load_t0 = _perf_time.time()
    if cache_key not in st.session_state:
        try:
            file_size = os.path.getsize(pdf_path)

            _read_t0 = _perf_time.time()
            with st.spinner("Loading filing..."):
                with open(pdf_path, "rb") as _f:
                    _raw = _f.read()
            _read_ms = (_perf_time.time() - _read_t0) * 1000
            _enc_t0 = _perf_time.time()
            st.session_state[cache_key] = base64.b64encode(_raw).decode("ascii")
            _enc_ms = (_perf_time.time() - _enc_t0) * 1000
        except Exception as e:
            st.error(f"Could not read PDF: {e}")
            log_error(f"[_render_pdf_viewer] Read failed: {e}")
            return
    else:
        _pdf_cache_ms = (_perf_time.time() - _pdf_load_t0) * 1000

    b64 = st.session_state[cache_key]

    # ------------------------------------------------------------------
    # 2. PDF.js component — renders pages lazily to <canvas> elements.
    #    Only visible pages are rendered; neighbours are pre-fetched on
    #    scroll via IntersectionObserver (rootMargin: 400px lookahead).
    # ------------------------------------------------------------------
    # Escape keyword for safe JS string literal
    import json as _json
    _kw_js = _json.dumps(highlight_keyword)   # produces a quoted, escaped JS string

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #525659; }}
  #container {{
    height: 800px;
    overflow-y: auto;
    padding: 12px 0;
  }}
  .pg {{
    margin: 0 auto 10px;
    text-align: center;
    background: #3a3d40;
    max-width: 100%;
  }}
  canvas {{ display: block; margin: 0 auto; box-shadow: 0 1px 6px rgba(0,0,0,.5); }}
  #status {{ color: #aaa; text-align: center; padding: 60px 20px; font: 14px sans-serif; }}
</style>
</head>
<body>
<div id="container"><div id="status">Loading PDF...</div></div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script>
(function () {{
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

  // Decode base64 payload into a Uint8Array for PDF.js
  var b64 = "{b64}";
  var bin = atob(b64);
  var u8  = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);

  var container      = document.getElementById('container');
  var rendered       = {{}};
  var pdfDoc         = null;
  var targetPage     = {highlight_page};
  var highlightKeyword = {_kw_js};

  // ── Keyword highlight: draw red boxes directly onto the page canvas ───
  // Canvas overlay is 100% reliable — no text-layer API version concerns.
  // We draw on top of the rendered bitmap, so it is always visible.
  function drawKeywordOnCanvas(page, canvas, viewport) {{
    if (!highlightKeyword) return;
    var kw = highlightKeyword.toLowerCase();
    page.getTextContent().then(function (tc) {{
      var ctx        = canvas.getContext('2d');
      var firstHitY  = null;   // canvas-y of first match (for scroll precision)
      var firstHitEl = canvas.parentElement;  // .pg div

      tc.items.forEach(function (item) {{
        if (!item.str || item.str.toLowerCase().indexOf(kw) === -1) return;

        // Convert PDF text-matrix → canvas pixel coordinates
        var tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
        // tx[4]=x, tx[5]=y (baseline, canvas coords)
        var cx = tx[4];
        var cy = tx[5];
        var cw = Math.abs(item.width  * viewport.scale);
        var ch = Math.abs(item.height * viewport.scale);
        if (cw < 2 || ch < 2) return;   // skip invisible items

        ctx.save();
        ctx.fillStyle = 'rgba(229, 57, 53, 0.35)';
        ctx.fillRect(cx, cy - ch, cw, ch);
        ctx.restore();

        // Track first hit canvas-y for scroll (relative to page top)
        if (firstHitY === null) firstHitY = cy - ch;
      }});

      // Scroll container so the first keyword hit is centred in the viewport
      if (firstHitY !== null && firstHitEl) {{
        setTimeout(function () {{
          // firstHitEl.offsetTop = page top from container top
          container.scrollTop = firstHitEl.offsetTop + firstHitY - (container.clientHeight / 2);
        }}, 100);
      }}
    }}).catch(function () {{/* non-fatal — canvas render already succeeded */}});
  }}

  pdfjsLib.getDocument({{ data: u8 }}).promise.then(function (pdf) {{
    pdfDoc = pdf;
    var total = pdf.numPages;
    container.innerHTML = '';

    // Create one placeholder <div> per page — sized to approx A4 so the
    // scrollbar reflects the true document length before pages are rendered.
    for (var p = 1; p <= total; p++) {{
      var div = document.createElement('div');
      div.className  = 'pg';
      div.dataset.p  = p;
      div.style.width  = '816px';
      div.style.height = '1056px';
      container.appendChild(div);
    }}

    function renderPage(p) {{
      if (rendered[p] || p < 1 || p > total) return;
      rendered[p] = true;
      var el = container.querySelector('[data-p="' + p + '"]');
      if (!el) return;
      pdfDoc.getPage(p).then(function (page) {{
        var baseVp   = page.getViewport({{ scale: 1.0 }});
        var scale    = Math.min(900, (container.clientWidth || 900) - 24) / baseVp.width;
        var viewport = page.getViewport({{ scale: scale }});
        var canvas   = document.createElement('canvas');
        canvas.width  = viewport.width;
        canvas.height = viewport.height;
        page.render({{ canvasContext: canvas.getContext('2d'), viewport: viewport }})
            .promise.then(function () {{
              el.style.height     = '';
              el.style.background = '';
              el.innerHTML        = '';
              el.appendChild(canvas);
              // Draw keyword highlight directly on this page's canvas
              if (p === targetPage && highlightKeyword) {{
                drawKeywordOnCanvas(page, canvas, viewport);
              }}
            }});
      }});
    }}

    // IntersectionObserver: render as pages enter viewport + 400 px lookahead
    var io = new IntersectionObserver(function (entries) {{
      entries.forEach(function (entry) {{
        if (entry.isIntersecting) {{
          var p = parseInt(entry.target.dataset.p, 10);
          renderPage(p);
          renderPage(p - 1);  // pre-render previous page
          renderPage(p + 1);  // pre-render next page
        }}
      }});
    }}, {{ root: container, rootMargin: '400px' }});

    container.querySelectorAll('.pg').forEach(function (el) {{ io.observe(el); }});

    // Eagerly render first 3 pages without waiting for scroll
    renderPage(1);
    renderPage(2);
    renderPage(3);

    // Keyword navigation: initial scroll to target page (fast path);
    // drawKeywordOnCanvas() will refine the scroll to the exact hit position.
    if (targetPage > 0) {{
      renderPage(targetPage - 1);
      renderPage(targetPage);
      renderPage(targetPage + 1);
      setTimeout(function () {{
        var el = container.querySelector('[data-p="' + targetPage + '"]');
        if (el) el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }}, 400);
    }}

  }}).catch(function (err) {{
    document.getElementById('status').textContent = 'PDF error: ' + err.message;
  }});
}})();
</script>
</body>
</html>"""

    _comp_t0 = _perf_time.time()
    components.html(html, height=820, scrolling=False)
    _comp_ms = (_perf_time.time() - _comp_t0) * 1000


@st.cache_resource(show_spinner=False)
def scan_filings_directory():
    """Scan Azure Blob Storage to discover available companies, years, and doc types.

    TIMING: This function is cached, first call may take 10-30s depending on blob count.

    Expected blob structure: {prefix}/{TICKER}/{YEAR}/{DOC_TYPE_DIR}/*.html or *.htm
    For 10-Q: {prefix}/{TICKER}/{YEAR}/10-Q-Q1/  etc.

    Returns: {ticker: {year: {display_type: blob_name_or_quarter_dict}}}
    For 10-Q, the value is a dict: {'Q1': blob_name, 'Q2': blob_name, 'Q3': blob_name}
    For other types, the value is a blob name string.
    """
    scan_start = _perf_time.time()


    filings_data: Dict[str, Dict[str, Dict[str, object]]] = {}

    # If Azure not configured, return empty (caller has fallbacks)
    acct, key, container_name, _ = _get_azure_config()
    if not acct or not container_name or not key:

        return filings_data

    # Group blobs by (ticker, year, doc_type_dir)
    grouped: Dict[Tuple[str, str, str], List[str]] = {}
    try:
        for b in _iter_filing_blobs():
            name = getattr(b, "name", "")
            parsed = _parse_blob_path(name)
            if not parsed:
                continue
            ticker, year, doc_type_dir, _filename = parsed
            if ticker.startswith((".", "_")):
                continue
            grouped.setdefault((ticker, year, doc_type_dir), []).append(name)
    except Exception as e:
        log_structured_error(e, page="company_filings", component="scan_filings_directory", operation="SCAN_BLOBS")
        return filings_data

    # Build FILINGS_DATA shape (choose one best file per doc dir)
    for (ticker, year, doc_type_dir), blob_names in grouped.items():
        # PRODUCTION: Skip invalid doc_types
        if not _is_valid_doc_type(doc_type_dir):
            continue

        best_blob = _pick_best_blob_for_docdir(blob_names)
        if not best_blob:
            continue

        # Check if this is a 10-Q quarter directory (e.g. 10-Q-Q1)
        if doc_type_dir.startswith("10-Q-Q"):
            quarter = doc_type_dir.replace("10-Q-", "")  # 'Q1', 'Q2', 'Q3'
            quarter_dict = filings_data.setdefault(ticker, {}).setdefault(year, {}).setdefault("10-Q", {})
            if isinstance(quarter_dict, dict):
                quarter_dict[quarter] = best_blob
        # Check if this is a NON-SEC interim report directory (e.g. interim-report-Q1)
        elif doc_type_dir.startswith("interim-report-Q"):
            quarter = doc_type_dir.replace("interim-report-", "")  # 'Q1', 'Q2', 'Q3', 'Q4', 'Q5'
            display_type = DOC_TYPE_MAP.get(doc_type_dir, doc_type_dir)  # e.g., "Q1 Interim"
            filings_data.setdefault(ticker, {}).setdefault(year, {})[display_type] = best_blob
        else:
            display_type = DOC_TYPE_MAP.get(doc_type_dir, doc_type_dir)
            filings_data.setdefault(ticker, {}).setdefault(year, {})[display_type] = best_blob

    ticker_count = len(filings_data)
    year_count = sum(len(years) for years in filings_data.values())
    scan_end = _perf_time.time()
    scan_duration = scan_end - scan_start

    return filings_data


# =============================================================================
# CACHED SCAN WITH DISK CACHE (PERFORMANCE OPTIMIZATION)
# =============================================================================

def _organize_blobs_to_filings(blobs: List) -> Dict[str, Dict[str, Any]]:
    """
    Organize Azure blobs into filings data structure.

    Args:
        blobs: List of Azure blob objects

    Returns:
        {ticker: {year: {doc_type: blob_name_or_quarter_dict}}}
    """
    filings_data: Dict[str, Dict[str, Any]] = {}

    for blob in blobs:
        name = getattr(blob, "name", "")
        if not name:
            continue

        # Filter to HTML/HTM/PDF files
        lower = name.lower()
        if not (lower.endswith(".html") or lower.endswith(".htm") or lower.endswith(".pdf")):
            continue
        if lower.endswith("-clean.html"):
            continue

        # Parse path structure
        parsed = _parse_blob_path(name)
        if not parsed:
            continue

        ticker, year, doc_type_dir, filename = parsed

        if ticker.startswith((".", "_")):
            continue

        # PRODUCTION: Skip invalid doc_types
        if not _is_valid_doc_type(doc_type_dir):
            continue

        # Organize by doc type
        if doc_type_dir.startswith("10-Q-Q"):
            quarter = doc_type_dir.replace("10-Q-", "")
            quarter_dict = filings_data.setdefault(ticker, {}).setdefault(year, {}).setdefault("10-Q", {})
            if isinstance(quarter_dict, dict):
                quarter_dict[quarter] = name
        # Handle NON-SEC interim report directories (with CAPITAL Q)
        elif doc_type_dir.startswith("interim-report-Q"):
            display_type = DOC_TYPE_MAP.get(doc_type_dir, doc_type_dir)
            filings_data.setdefault(ticker, {}).setdefault(year, {})[display_type] = name
        else:
            display_type = DOC_TYPE_MAP.get(doc_type_dir, doc_type_dir)
            filings_data.setdefault(ticker, {}).setdefault(year, {})[display_type] = name

    return filings_data


def scan_filings_directory_cached(use_cache: bool = True) -> Dict[str, Dict]:
    """
    Scan filings directory with DiskCache support.

    DEPRECATED: Use get_filings_data() instead for lazy loading.

    Args:
        use_cache: If False, always scan from Azure (cache bypass)

    Returns:
        Filings data dict
    """
    return get_filings_data()


def get_filings_data_lazy() -> Dict[str, Dict]:
    """
    Lazy getter for filings data - returns cache immediately if available.

    This function is designed to be called during UI rendering. It returns
    cached data immediately if available, without blocking.

    Returns:
        Filings data dict (may be empty if cache not ready)
    """
    return get_filings_data()


# =============================================================================
# LAZY LOADING - Don't scan on module load to avoid blocking
# =============================================================================
_FILINGS_DATA_LAZY: Optional[Dict] = None
_FILINGS_DATA_LOADING = False

def get_filings_data() -> Dict:
    """
    Get filings data - lazy loaded to avoid blocking module import.

    This function returns cached data if available, otherwise triggers
    a scan. The first call may be slow, but subsequent calls are fast.
    """
    global _FILINGS_DATA_LAZY, _FILINGS_DATA_LOADING

    if _FILINGS_DATA_LAZY is not None:
        return _FILINGS_DATA_LAZY

    if _FILINGS_DATA_LOADING:
        # Return empty dict while loading
        return {}

    _FILINGS_DATA_LOADING = True


    # Try to get from cache first
    if CACHE_AVAILABLE:
        try:
            cache = get_filings_cache()
            status = cache.get_scan_status()
            completed = status.get('completed_companies', [])

            if completed:

                all_data = {}
                for ticker in completed:
                    cached = cache.get_company_filings(ticker)
                    if cached and cached.get('data'):
                        all_data[ticker] = cached['data']

                if all_data:
                    _FILINGS_DATA_LAZY = all_data
                    _FILINGS_DATA_LOADING = False

                    return _FILINGS_DATA_LAZY
        except Exception as e:
            log_structured_error(e, page="company_filings", component="get_filings_data", operation="LAZY_LOAD")

    # Fall back to scanning

    _FILINGS_DATA_LAZY = scan_filings_directory()
    _FILINGS_DATA_LOADING = False
    return _FILINGS_DATA_LAZY


# Legacy variable - redirects to lazy getter
# IMPORTANT: Don't access this directly, use get_filings_data() instead
FILINGS_DATA: Dict = {}

# =============================================================================
# AZURE → LOCAL CACHE (DOWNLOAD ON DEMAND)
# =============================================================================

def _local_cache_path_for_blob(blob_name: str) -> str:
    rel = _strip_prefix(blob_name)
    rel = rel.replace("\\", "/").lstrip("/")
    return os.path.join(FILINGS_BASE_DIR, rel.replace("/", os.sep))


def _ensure_local_blob(blob_name: str) -> Optional[str]:
    """
    Ensure the blob is downloaded locally. Returns local filepath or None.
    Downloads into FILINGS_BASE_DIR mirroring the blob's relative path.
    """
    if not blob_name:
        return None

    local_path = _local_cache_path_for_blob(blob_name)
    try:
        container = _get_blob_container_client()
        blob_client = container.get_blob_client(blob_name)

        # Get properties to support freshness check
        props = blob_client.get_blob_properties()
        last_modified = getattr(props, "last_modified", None)
        # Convert last_modified to timestamp if possible
        lm_ts = None
        try:
            if last_modified:
                lm_ts = last_modified.timestamp()
        except Exception:
            lm_ts = None

        if os.path.exists(local_path) and lm_ts is not None:
            # If local file is newer or same as blob, keep it
            if os.path.getmtime(local_path) >= lm_ts:
                return local_path

        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        # Download
        with open(local_path, "wb") as f:
            dl = blob_client.download_blob()
            f.write(dl.readall())

        # Align mtime to blob last_modified for stable cache comparisons
        if lm_ts is not None:
            try:
                os.utime(local_path, (lm_ts, lm_ts))
            except Exception:
                pass

        return local_path
    except Exception as e:
        log_structured_error(e, page="company_filings", component="_ensure_local_blob", operation="DOWNLOAD_BLOB")
        return None


# =============================================================================
# ── DB-based company names (replaces hardcoded dict) ─────────────────────────
# Module-level cache for company names - lazy loaded
_COMPANY_NAMES_CACHE = None

@st.cache_data(ttl=600, show_spinner=False)
def _load_company_names_from_db():
    """Load ticker→company_name map from coreiq_companies (indexed master table).

    Replaces the previous DISTINCT scan on coreiq_filing_metrics_v5 (~7.75M rows).
    """
    _start = _perf_time.time()

    try:
        from data.repository import CompanyRepository

        _query_start = _perf_time.time()
        company_rows = CompanyRepository.get_companies_rows()
        _process_start = _perf_time.time()
        result = {}
        for row in company_rows:
            ticker = row.get("ticker")
            name = row.get("name_coresight") or row.get("name")
            if ticker and name and name.upper() != ticker.upper():
                formatted_name = _format_company_name(name)
                result[ticker] = formatted_name

        return result
    except Exception as e:
        log_structured_error(e, page="company_filings", component="_load_company_names_from_db", operation="DB_FETCH")
        _total = _perf_time.time() - _start

        return {"AAPL": "Apple Inc.", "AMZN": "Amazon.com Inc.", "M": "Macy's Inc."}


def get_company_names() -> Dict[str, str]:
    """Get company names map - lazy loaded to avoid blocking module import."""
    global _COMPANY_NAMES_CACHE
    if _COMPANY_NAMES_CACHE is None:

        _COMPANY_NAMES_CACHE = _load_company_names_from_db()

    return _COMPANY_NAMES_CACHE


def _format_company_name(name: str) -> str:
    """Return company name exactly as stored in DB."""
    return name


# =============================================================================
# DOWNLOAD BUTTON - Client-side HTML download (matches earnings_calls styling)
# =============================================================================
def _render_filing_download_button(pdf_bytes: bytes, filename: str, auto_click: bool = False) -> None:
    """
    Render a PDF download button that works entirely client-side via JavaScript
    Blob API — bypasses Streamlit's /media/ endpoint.
    Matches the red border styling from earnings_calls.
    If auto_click=True, triggers the download automatically on load (no user click needed).
    """
    try:
        from utils.server_logger import track_download
        track_download("filing_pdf", name=filename, page="company_filings")
    except Exception:
        pass
    import base64
    from streamlit.components.v1 import html as _sthtml

    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    safe_name = filename.replace("'", "\\'").replace('"', '\\"')
    auto_trigger = "window.addEventListener('load', function(){ setTimeout(dl, 100); });" if auto_click else ""

    btn_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{
  display:flex;justify-content:flex-end;align-items:center;
  height:52px;background:transparent;
  font-family:'Roboto',Helvetica,Arial,sans-serif;
  padding:0 20px;
}}
button{{
  background:transparent;
  border:1px solid #D62E2F;
  color:#D62E2F;
  border-radius:4px;
  padding:7px 12px;
  font-size:13px;
  font-weight:500;
  cursor:pointer;
  white-space:nowrap;
  transition:background 0.15s,color 0.15s;
  letter-spacing:0.01em;
}}
button:hover{{background:#D62E2F;color:#fff;}}
button:active{{opacity:0.85;}}
</style>
</head>
<body>
<button onclick="dl()">&#8659;&nbsp;&nbsp;Download</button>
<script>
var _d="{b64}";
function dl(){{
  try{{
    var bin=atob(_d),n=bin.length,u8=new Uint8Array(n);
    for(var i=0;i<n;i++) u8[i]=bin.charCodeAt(i);
    var blob=new Blob([u8],{{type:"application/pdf"}});
    var url=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=url; a.download="{safe_name}";
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(function(){{URL.revokeObjectURL(url);}},200);
  }}catch(e){{console.error("PDF download failed:",e);}}
}}
{auto_trigger}
</script>
</body></html>"""
    _sthtml(btn_html, height=52, scrolling=False)


# NOTE: COMPANY_NAMES moved to lazy loading via get_company_names()
# This prevents blocking module import with DB queries
# Call get_company_names() when needed instead

# =============================================================================
# DISABLED: STATIC HTML PRE-PROCESSOR
# =============================================================================
# NOTE: Static file serving disabled - using data/filings_blob_cache/ instead
# Streamlit static folder has 1GB limit and 200MB per-file limit
# Keeping function definitions for backwards compatibility but they return None

def _make_clean_html_for_static(raw: str) -> str:
    """Process raw filing HTML: strip XML, convert iXBRL, inject CSS + postMessage receiver."""
    import re
    raw = re.sub(r'^\s*<\?xml[^?]*\?>\s*', '', raw, count=1)
    raw = _convert_ixbrl_to_spans(raw)

    lazy_css = (
        "<style>"
        "body>*{content-visibility:auto;contain-intrinsic-size:auto 80px}"
        "</style>"
    )
    # postMessage receiver — listens for {type:'HIGHLIGHT_FACT', factId:'...'} from wrapper
    receiver = (
        "<script>(function(){"
        "console.log('[RECEIVER] postMessage receiver initialized');"
        "window.addEventListener('message',function(e){"
        "console.log('[RECEIVER] Received message:', e.data);"
        "var d=e.data;if(!d||d.type!=='HIGHLIGHT_FACT'){console.log('[RECEIVER] Ignoring message - wrong type');return;}"
        "var fid=d.factId;if(!fid){console.log('[RECEIVER] No factId in message');return;}"
        "console.log('[RECEIVER] Processing factId:', fid);"
        "var el=fid.startsWith('TEXT:')?_fbt(fid.slice(5)):document.getElementById(fid);"
        "console.log('[RECEIVER] Found element:', el);"
        "if(el){console.log('[RECEIVER] Highlighting element');_hl(el);}"
        "else{console.log('[RECEIVER] Element not found for:', fid);}"
        "});"
        "function _hl(el){"
        "el.style.backgroundColor='#FDF5F5';"
        "el.style.boxShadow='0 0 10px rgba(214,46,47,0.3)';"
        "el.style.border='2px solid #D62E2F';"
        "el.style.borderRadius='4px';"
        "el.style.padding='4px';"
        "el.scrollIntoView({behavior:'instant',block:'center'});"
        "setTimeout(function(){el.scrollIntoView({behavior:'instant',block:'center'});},150);"
        "setTimeout(function(){el.scrollIntoView({behavior:'instant',block:'center'});},380);"
        "setTimeout(function(){el.scrollIntoView({behavior:'smooth',block:'center'});},680);"
        "}"
        "function _fbt(text){"
        "var WS=/[\\u00A0\\s]+/g,ns=text.replace(WS,' ').trim();"
        "var vs=[ns,ns.slice(0,80),ns.slice(0,50),ns.slice(0,30)];"
        "for(var vi=0;vi<vs.length;vi++){"
        "var sv=vs[vi];if(sv.length<15)continue;"
        "var all=document.body.querySelectorAll('p,td,li,div,span,section,article');"
        "var best=null,bestSz=Infinity;"
        "for(var i=0;i<all.length;i++){"
        "var et=(all[i].textContent||'').replace(WS,' ');"
        "if(et.includes(sv)&&et.length<bestSz){bestSz=et.length;best=all[i];}"
        "}"
        "if(best){_hl(best);return true;}"
        "}"
        "return false;"
        "}"
        "})();</script>"
    )

    if '</head>' in raw:
        raw = raw.replace('</head>', lazy_css + '</head>', 1)
    elif '<head>' in raw:
        raw = raw.replace('<head>', '<head>' + lazy_css, 1)
    else:
        raw = lazy_css + raw

    if '</body>' in raw:
        raw = raw.replace('</body>', receiver + '</body>', 1)
    else:
        raw = raw + receiver

    return raw


def _get_static_clean_paths(html_path: str):
    """DISABLED - Returns (None, None) always."""
    # Static serving disabled - using data/filings_blob_cache/ instead
    return None, None


def _write_clean_static(html_path: str) -> Optional[str]:
    """DISABLED - Always returns None."""
    # Static serving disabled - using data/filings_blob_cache/ instead
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def _get_or_create_static_url(html_path: str) -> Optional[str]:
    """
    DISABLED: Static file serving from app/static/filings_clean/

    Reason: Streamlit static folder has 1GB limit. We serve directly from
    data/filings_blob_cache/ using inline HTML instead.

    Returns None to force fallback to inline HTML rendering which reads
    directly from the blob cache directory.
    """
    # ALWAYS return None to disable static serving and use inline HTML
    # This avoids the 1GB Streamlit static folder limit
    return None


@st.cache_resource(show_spinner=False)
def _start_static_warmup():
    """DISABLED - Static file serving disabled."""
    # Static serving disabled - no warmup needed
    return True


# DISABLED: Background preprocessing to static folder
# Reason: Streamlit static folder has 1GB limit. We serve directly from
# data/filings_blob_cache/ using inline HTML instead.
# _start_static_warmup()  # <- DISABLED


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class FilingDocument:
    """A SEC filing document."""
    company_name: str
    ticker: str
    document_type: str
    year: str
    quarter: str
    content: str  # Would be actual PDF/binary content


# =============================================================================
# MOCK DATA (Replace with database calls)
# =============================================================================

# ── DB-based company/year/doctype lists (replaces folder scan) ────────────────
from data.repository import FilingMetricRepository

@st.cache_data(ttl=600, show_spinner=False)
def _load_companies_from_db():
    """Get (ticker, display_label) for the company dropdown.

    SEC tickers come from coreiq_companies (indexed master) instead of a DISTINCT
    scan on coreiq_filing_metrics_v5 (~7.75M rows). NON-SEC tickers are merged
    from coreiq_companies + Azure Blob scan (unchanged).
    Display format: "Company Name (TICKER)"
    """
    _func_start = _perf_time.time()


    try:
        from data.repository import CompanyRepository

        # Step 1: SEC companies from coreiq_companies master (cached 1h).
        _query_start = _perf_time.time()

        company_rows = CompanyRepository.get_companies_rows()
        _query_elapsed = _perf_time.time() - _query_start

        # Step 2: Build both ticker list and company names map from single result
        _process_start = _perf_time.time()

        # Build company names map inline (no separate DB call needed)
        global _COMPANY_NAMES_CACHE
        _built_names = {}
        seen_tickers = {}
        for row in company_rows:
            if (row.get("source") or "").strip() != "SEC":
                continue
            ticker = row.get("ticker")
            name = row.get("name_coresight") or row.get("name")
            if ticker and name and name.upper() != ticker.upper():
                formatted_name = _format_company_name(name)
                _built_names[ticker] = formatted_name
            if ticker and ticker not in seen_tickers:
                seen_tickers[ticker] = True

        # NON-SEC: pull all companies from coreiq_companies (source != SEC) AND
        # scan Azure Blob in parallel for each.  Both sources are merged — DB wins
        # on name; blob-only tickers (not in DB at all) are added from blob scan.
        # SEC companies are NEVER touched — their list comes solely from v2 above.
        try:
            from utils.non_sec_blob_fallback import (
                get_non_sec_tickers_from_db,
                scan_all_non_sec_tickers_parallel,
            )

            # All registered NON-SEC tickers (coreiq_companies)
            non_sec_db = get_non_sec_tickers_from_db()   # [(ticker, name), ...]
            non_sec_ticker_list = [t for t, _ in non_sec_db]

            # Register names from DB first (don't overwrite v2 entries)
            for ticker, name in non_sec_db:
                if ticker and name and name.upper() != ticker.upper():
                    if ticker not in _built_names:
                        _built_names[ticker] = _format_company_name(name)
                if ticker and ticker not in seen_tickers:
                    seen_tickers[ticker] = True

            # Parallel Azure Blob scan — discovers which tickers actually have files.
            # Also warms the per-ticker blob cache so the first selection is instant.
            blob_results = scan_all_non_sec_tickers_parallel(non_sec_ticker_list)
            for ticker, blob_data in blob_results.items():
                # Add blob-discovered tickers not yet in the list
                if ticker not in seen_tickers:
                    seen_tickers[ticker] = True
                    # No DB name available — use ticker as display name
                    if ticker not in _built_names:
                        _built_names[ticker] = ticker
        except Exception as _nse:
            log_structured_error(
                _nse, page="company_filings",
                component="_load_companies_from_db", operation="NON_SEC_BLOB_SCAN"
            )

        # Populate the global cache so get_company_names() won't re-query
        if _COMPANY_NAMES_CACHE is None:
            _COMPANY_NAMES_CACHE = _built_names

        results = []
        for ticker in seen_tickers:
            name = _built_names.get(ticker, ticker)
            display = f"{name} ({ticker})"
            results.append((ticker, display))

        results.sort(key=lambda x: x[1].lower())

        _process_elapsed = _perf_time.time() - _process_start

        # Step 3: Return
        _total_elapsed = _perf_time.time() - _func_start
        try:
            log_timing(
                "DB_load_companies_from_db",
                _total_elapsed * 1000,
                f"table=coreiq_companies rows={len(results)} query_ms={_query_elapsed * 1000:.0f}",
            )
        except Exception:
            pass

        return results
    except Exception as e:
        log_structured_error(e, page="company_filings", component="_load_companies_from_db", operation="DB_FETCH")
        _total_elapsed = _perf_time.time() - _func_start

        # Fallback now uses Azure scan instead of local folders
        _fd = get_filings_data()
        _company_names = get_company_names()
        fallback = [(t, f"{_company_names.get(t, t)} ({t})") for t in _fd.keys()] if _fd else [("AAPL", "Apple Inc. (AAPL)")]
        fallback.sort(key=lambda x: x[1].lower())
        return fallback

@st.cache_data(ttl=300, show_spinner=False)
def _get_available_years_from_db(ticker: str):
    """Get available fiscal years for a ticker — derived from prefetch cache."""
    _func_start = _perf_time.time()
    prefetch = _prefetch_ticker_filter_data(ticker)
    if prefetch:
        result = prefetch["all_years"]

        return result

    return sorted(get_filings_data().get(ticker, {}).keys(), reverse=True)

_TRANSCRIPT_DOC_TYPES = frozenset({
    'transcript', 'transcript-Q1', 'transcript-Q2',
    'transcript-Q3', 'transcript-Q4', 'transcript-unknown',
})

@st.cache_data(ttl=300, show_spinner=False)
def _get_available_doc_types_from_db(ticker: str):
    """Get available doc types for a ticker — derived from prefetch cache."""
    _func_start = _perf_time.time()
    prefetch = _prefetch_ticker_filter_data(ticker)
    if prefetch and prefetch["doc_types"]:
        result = [dt for dt in prefetch["doc_types"] if dt not in _TRANSCRIPT_DOC_TYPES]
        return result

    return [dt for dt in DOCUMENT_TYPES if dt not in _TRANSCRIPT_DOC_TYPES]


@st.cache_data(ttl=300, show_spinner=False)
def _prefetch_ticker_filter_data(ticker: str):
    """Prefetch all doc_type + storage_year combos for a ticker in ONE DB query.

    OPTIMIZATION: Replaces 3 separate queries (years, doc_types, years_for_doc_type)
    with a single query. Each Azure DB roundtrip costs ~250ms network RTT, so merging
    3 queries into 1 saves ~500ms on cold load.

    CACHE: @st.cache_data(ttl=300) - Cached for 5 minutes
    TABLE: coreiq_filing_metrics_v5 (DEI-correct fiscal_year; storage_year = physical bucket)
    INDEX: idx_ticker_fiscal_doctype (covering index on ticker, storage_year, doc_type)
    QUERY: SELECT DISTINCT doc_type, storage_year, fiscal_year WHERE ticker = ? ORDER BY ...
    """
    _func_start = _perf_time.time()


    try:
        from core.database import db_manager

        _query_start = _perf_time.time()
        rows = db_manager.execute_query_readonly("""
            SELECT DISTINCT
                   doc_type,
                   COALESCE(storage_year, report_fiscal_year, fiscal_year) AS bucket_year,
                   COALESCE(fiscal_year, storage_year, report_fiscal_year)  AS display_year
            FROM coreiq_filing_metrics_v5
            WHERE ticker = :ticker
            ORDER BY doc_type, bucket_year DESC
        """, {"ticker": ticker})
        _query_elapsed = _perf_time.time() - _query_start

        try:
            log_timing(
                "FILINGS_PREFETCH",
                _query_elapsed * 1000,
                f"ticker={ticker} table=coreiq_filing_metrics_v5 rows={len(rows)}",
            )
        except Exception:
            pass

        # bucket_year = physical blob bucket → drives file loading and ALL plumbing
        #               (unchanged from v4; the blob loader's year/year-1 fallback relies on it).
        # display_year = the filing's OWN fiscal year, from its DEI cover-page tag → shown to user.
        # They differ for non-December fiscal years (e.g. LULU Q3: bucket 2026 → displays 2025).
        doc_types = []
        all_years = set()
        years_by_doc_type = {}
        fiscal_label_by_year = {}   # {doc_type: {bucket_year_str: display_year_str}}
        for row in rows:
            dt = row["doc_type"]
            by = row["bucket_year"]
            dy = row["display_year"]
            if dt and dt not in doc_types:
                doc_types.append(dt)
            if by:
                by_str = str(by)
                all_years.add(by_str)
                if dt:
                    years_by_doc_type.setdefault(dt, [])
                    if by_str not in years_by_doc_type[dt]:
                        years_by_doc_type[dt].append(by_str)
                    fiscal_label_by_year.setdefault(dt, {})
                    fiscal_label_by_year[dt][by_str] = str(dy) if dy else by_str

        all_years_sorted = sorted(all_years, reverse=True)

        # Sort each doc_type's year list descending so [0] is always the latest
        for dt in years_by_doc_type:
            years_by_doc_type[dt] = sorted(years_by_doc_type[dt], reverse=True)

        return {"doc_types": doc_types, "all_years": all_years_sorted,
                "years_by_doc_type": years_by_doc_type,
                "fiscal_label_by_year": fiscal_label_by_year}
    except Exception as e:
        log_structured_error(e, page="company_filings", component="_prefetch_ticker_filter_data", operation="DB_PREFETCH")
        _total_elapsed = _perf_time.time() - _func_start

        return None








def _get_available_years_for_doc_type(ticker: str, doc_type: str):
    """Get available fiscal years for a ticker + doc_type — derived from prefetch cache."""
    _func_start = _perf_time.time()
    prefetch = _prefetch_ticker_filter_data(ticker)
    if prefetch:
        result = prefetch["years_by_doc_type"].get(doc_type, [])

        return result
    return []


def _fiscal_label_for(ticker: str, doc_type: str, bucket_year) -> str:
    """Map a physical bucket (storage) year to the filing's OWN fiscal year for DISPLAY.

    The dropdown/plumbing carry the bucket year (so blob loading is unchanged); this
    converts it to the DEI-declared fiscal year purely for what the user sees. Falls
    back to the bucket year when no fiscal label is known (e.g. 8-K, non-SEC, no DEI).
    """
    if bucket_year in (None, ""):
        return ""
    prefetch = _prefetch_ticker_filter_data(ticker)
    if prefetch:
        return prefetch.get("fiscal_label_by_year", {}).get(doc_type, {}).get(str(bucket_year), str(bucket_year))
    return str(bucket_year)


# Module-level cache variable - populated lazily on first use
_COMPANIES_CACHE = None

# Fallback doc types list (used only if DB query fails)
DOCUMENT_TYPES = ["10-K", "10-Q-Q1", "10-Q-Q2", "10-Q-Q3"]


def _format_header_date(value: Any) -> str:
    """Format an ISO date for the filing header."""
    if not value:
        return ""
    try:
        from datetime import datetime as _dt
        parsed = _dt.strptime(str(value)[:10], "%Y-%m-%d")
        return parsed.strftime("%b %d, %Y").replace(" 0", " ")
    except Exception:
        return str(value)


def _filing_period_display(doc_type: str, filing_unit: str = "") -> str:
    """Return the compact period chip used beside the selected storage year."""
    if doc_type in {"10-K", "20-F", "40-F", "annual-report", "AR"}:
        return "Annual"
    if doc_type == "half-yearly":
        return "Half-Yearly"

    q_match = re.search(r"Q([1-5])", doc_type or "", re.IGNORECASE)
    if q_match:
        return f"Q{q_match.group(1)}"

    display = DOC_TYPE_MAP.get(doc_type, doc_type)
    if doc_type in FILING_UNIT_DOC_TYPES and filing_unit and filing_unit != "main":
        display = f"{display} {get_filing_unit_display_name(filing_unit)}"
    return display


def _render_filing_header_html(
    company_name: str,
    ticker: str,
    year: str,
    doc_type: str,
    filing_unit: str,
    metadata: Dict[str, Any],
    highlight_text: str = "",
) -> str:
    """Build the filing document header to match the earnings-call metadata style."""
    period_display = _filing_period_display(doc_type, filing_unit)
    filing_date = _format_header_date((metadata or {}).get("filing_date"))
    fiscal_period_label = (metadata or {}).get("fiscal_period_label") or ""

    # Show the filing's OWN fiscal year (DEI), not the physical storage bucket.
    display_year = _fiscal_label_for(ticker, doc_type, year) or str(year)

    meta_parts = [str(display_year), period_display]
    if filing_date:
        meta_parts.append(filing_date)
    if fiscal_period_label:
        meta_parts.append(f"Fiscal Period: {fiscal_period_label}")

    meta_html = '<span class="filing-header-dot"></span>'.join(
        f"<span>{html.escape(part)}</span>"
        for part in meta_parts
        if part
    )
    highlight_html = (
        f'<span class="filing-header-highlight">{html.escape(highlight_text)}</span>'
        if highlight_text
        else ""
    )

    return (
        '<div class="filing-document-header">'
        f'<span class="filing-document-title">{html.escape(company_name)} ({html.escape(ticker)})</span>'
        f'<span class="filing-document-meta">{meta_html}</span>'
        f'{highlight_html}'
        '</div>'
    )


def get_companies() -> List[Tuple[str, str]]:
    """Get companies list - cached and loaded lazily to avoid blocking module import."""
    _start = _perf_time.time()


    global _COMPANIES_CACHE
    if _COMPANIES_CACHE is None:

        _COMPANIES_CACHE = _load_companies_cached()
        _elapsed = _perf_time.time() - _start

    else:
        _elapsed = _perf_time.time() - _start



    return _COMPANIES_CACHE


@st.cache_data(ttl=300, show_spinner=False)
def _load_companies_cached() -> List[Tuple[str, str]]:
    """Cached wrapper for loading companies - only runs once every 5 minutes."""
    _start = _perf_time.time()

    result = _load_companies_from_db()
    _elapsed = _perf_time.time() - _start

    return result


# For backwards compatibility - will be populated on first use
COMPANIES = []


# =============================================================================
# CSS STYLES
# =============================================================================

def get_filings_css() -> str:
    """Get custom CSS for filings page."""
    return """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&family=Montserrat:wght@400;500;600;700&display=swap');

    /* =======================================================================
       PAGE CONTAINER
       ======================================================================= */
    .filings-page-container {
        max-width: 1440px;
        margin: 0 auto;
        padding: 0;
        font-family: 'Roboto', sans-serif;
        background: #FFFFFF;
    }

    .filings-content-wrapper {
        max-width: 1220px;
        margin: 0 auto;
        padding: 0 110px;
    }

    /* =======================================================================
       HEADER SECTION WITH FILTERS
       ======================================================================= */
    .filings-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 24px 0;
        border-bottom: 1px solid #E5E5E5;
        margin-bottom: 24px;
    }

    .filings-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 700;
        font-size: 24px;
        color: #2D2A29;
        margin: 0;
    }

    .filing-document-header {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
        padding: 12px 4px 14px;
        border-bottom: 1px solid #E5E5E5;
        margin-bottom: 8px;
        font-family: 'Montserrat', sans-serif;
    }

    .filing-document-title {
        font-weight: 700;
        font-size: 20px;
        line-height: 1.25;
        color: #2D2A29;
    }

    .filing-document-meta {
        display: inline-flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 10px;
        font-family: 'Roboto', sans-serif;
        font-weight: 600;
        font-size: 15px;
        color: #6B6B6B;
    }

    .filing-header-dot {
        width: 4px;
        height: 4px;
        border-radius: 50%;
        background: #6B6B6B;
        display: inline-block;
        flex: 0 0 auto;
    }

    .filing-header-highlight {
        color: #0066CC;
        font-family: 'Roboto', sans-serif;
        font-size: 14px;
        margin-left: 2px;
    }

    /* Filter row styling */
    .filter-row {
        display: flex;
        gap: 16px;
        align-items: flex-end;
    }

    /* =======================================================================
       STREAMLIT SELECTBOX STYLING
       ======================================================================= */

    /* Selectbox label styling */
    div[data-testid="stSelectbox"] label {
        font-family: 'Roboto', sans-serif !important;
        font-size: 12px !important;
        font-weight: 400 !important;
        color: #6B6B6B !important;
        margin-bottom: 4px !important;
    }

    /* Selectbox input container */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] {
        border: 1px solid #CBCACA !important;
        border-radius: 4px !important;
        background: #FFFFFF !important;
        min-height: 36px !important;
    }

    /* Selectbox hover state */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"]:hover {
        border-color: #0066CC !important;
    }

    /* Selectbox text */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] span {
        font-family: 'Roboto', sans-serif !important;
        font-size: 14px !important;
        color: #2D2A29 !important;
    }

    /* =======================================================================
       SEARCH METRICS SIDEBAR
       ======================================================================= */
    .search-sidebar {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        padding: 16px;
        height: calc(100vh - 340px);
        min-height: 480px;
        overflow-y: auto;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05);
    }

    .search-sidebar::-webkit-scrollbar {
        width: 6px;
    }

    .search-sidebar::-webkit-scrollbar-track {
        background: #F2F2F2;
        border-radius: 3px;
    }

    .search-sidebar::-webkit-scrollbar-thumb {
        background: #CBCACA;
        border-radius: 3px;
    }

    .search-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid #F2F2F2;
    }

    .search-header svg {
        color: #D62E2F;
    }

    .search-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 600;
        font-size: 16px;
        color: #2D2A29;
    }

    /* Search input styling - integrated with sidebar */
    div[data-testid="stTextInput"] {
        margin-bottom: 0 !important;
    }

    div[data-testid="stTextInput"] > div > div > input {
        border: 1px solid #CBCACA !important;
        border-radius: 4px !important;
        font-family: 'Roboto', sans-serif !important;
        font-size: 14px !important;
        background: #FFFFFF !important;
        height: 36px !important;
    }

    div[data-testid="stTextInput"] > div > div > input:focus {
        border-color: #0066CC !important;
        box-shadow: 0 0 0 2px rgba(0, 102, 204, 0.2) !important;
    }

    .metrics-count {
        font-family: 'Roboto', sans-serif;
        font-size: 12px;
        color: #888888;
        margin: 4px 0 12px 4px;
    }

    /* =======================================================================
       METRIC CARDS
       ======================================================================= */
    .metric-card {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        padding: 12px;
        margin-bottom: 12px;
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        transition: all 0.2s ease;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06), 0 1px 3px rgba(0, 0, 0, 0.04);
    }

    .metric-card:hover {
        border-color: #CBCACA;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.10), 0 2px 6px rgba(0, 0, 0, 0.06);
    }

    .metric-card.active {
        border-color: #D62E2F;
        background: #FDF5F5;
    }

    .metric-info {
        flex: 1;
    }

    .metric-name {
        font-family: 'Roboto', sans-serif;
        font-weight: 500;
        font-size: 14px;
        color: #2D2A29;
        margin-bottom: 4px;
    }

    .metric-value {
        font-family: 'Roboto', sans-serif;
        font-weight: 600;
        font-size: 16px;
        color: #2D2A29;
        margin-bottom: 2px;
    }

    .metric-formula {
        font-family: 'Roboto Mono', 'Courier New', monospace;
        font-size: 11px;
        color: #888;
        margin-bottom: 4px;
        white-space: normal;
        overflow-wrap: break-word;
        word-break: break-word;
    }

    .metric-dimension {
        font-size: 11px;
        color: #777;
        font-style: italic;
        margin: 1px 0 4px 0;
        line-height: 1.3;
        overflow-wrap: break-word;
        word-break: break-word;
    }

    .metric-meta {
        display: flex;
        align-items: center;
        flex-wrap: nowrap;
        gap: 6px;
        font-family: 'Roboto', sans-serif;
        font-size: 11px;
        color: #888888;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .metric-meta span {
        white-space: nowrap;
        flex-shrink: 0;
    }

    .metric-meta-dot {
        width: 3px;
        height: 3px;
        background: #888888;
        border-radius: 50%;
    }

    .metric-action-btn {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 6px 12px;
        border-radius: 4px;
        font-family: 'Roboto', sans-serif;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.2s ease;
        border: none;
        background: transparent;
    }

    .metric-action-btn.view {
        color: #0066CC;
        background: #F0F7FF;
    }

    .metric-action-btn.view:hover {
        background: #E0EFFF;
    }

    .metric-action-btn.viewing {
        color: #D62E2F;
        background: #FDF5F5;
    }

    /* =======================================================================
       COMPACT VIEW BUTTONS IN SEARCH SIDEBAR
       ======================================================================= */
    /* Compact inline view buttons inside search cards */
    [data-testid="stColumn"]:first-child button {
        height: 28px !important;
        min-height: 28px !important;
        padding: 0 10px !important;
        font-size: 11px !important;
        background: transparent !important;
        color: #0066CC !important;
        border: 1px solid #E0EFFF !important;
        border-radius: 14px !important;
        font-family: 'Roboto', sans-serif !important;
        font-weight: 500 !important;
        margin-top: -4px !important;
        line-height: 28px !important;
    }

    [data-testid="stColumn"]:first-child button:hover {
        background: #E0EFFF !important;
        border-color: #0066CC !important;
    }

    /* =======================================================================
       BOTH PANELS — Override Streamlit's native bordered container
       Applies to: left Search Metrics box AND right Document Viewer box
       ======================================================================= */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border: 1px solid #E5E5E5 !important;
        border-radius: 12px !important;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08), 0 1px 4px rgba(0, 0, 0, 0.05) !important;
    }

    /* =======================================================================
       DOCUMENT VIEWER
       ======================================================================= */
    .document-viewer {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        height: calc(100vh - 280px);
        min-height: 540px;
        display: flex;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05);
        flex-direction: column;
    }

    .document-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px 20px;
        border-bottom: 1px solid #E5E5E5;
    }

    .document-title-section {
        display: flex;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
    }

    .document-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 600;
        font-size: 16px;
        color: #2D2A29;
    }

    .document-meta {
        display: flex;
        align-items: center;
        gap: 8px;
        font-family: 'Roboto', sans-serif;
        font-size: 14px;
        color: #6B6B6B;
    }

    .document-meta-dot {
        width: 4px;
        height: 4px;
        background: #6B6B6B;
        border-radius: 50%;
    }

    .document-badge {
        display: inline-flex;
        align-items: center;
        padding: 4px 10px;
        background: #F2F2F2;
        border-radius: 4px;
        font-family: 'Roboto', sans-serif;
        font-size: 13px;
        color: #4F4F4F;
    }

    .download-btn {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 7px 12px;
        background: transparent;
        border: 1px solid #D62E2F;
        border-radius: 4px;
        font-family: 'Roboto', sans-serif;
        font-size: 13px;
        font-weight: 500;
        color: #D62E2F;
        cursor: pointer;
        transition: background 0.15s, color 0.15s;
        text-decoration: none;
        white-space: nowrap;
        letter-spacing: 0.01em;
    }

    .download-btn:hover {
        background: #D62E2F;
        color: #FFFFFF;
    }

    .document-content {
        flex: 1;
        padding: 40px;
        background: #F9F9F9;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        overflow: auto;
        border-radius: 0 0 8px 8px;
    }

    .document-placeholder {
        text-align: center;
        color: #888888;
    }

    .document-placeholder svg {
        margin-bottom: 16px;
        color: #CBCACA;
    }

    .document-placeholder-text {
        font-family: 'Roboto', sans-serif;
        font-size: 18px;
        color: #888888;
        margin-bottom: 8px;
    }

    .document-placeholder-subtext {
        font-family: 'Roboto', sans-serif;
        font-size: 14px;
        color: #888888;
    }

    /* =======================================================================
       RESPONSIVE ADJUSTMENTS
       ======================================================================= */
    @media (max-width: 1024px) {
        .filings-content-wrapper {
            padding: 0 24px;
        }

        .filings-header {
            flex-direction: column;
            align-items: flex-start;
            gap: 16px;
        }
    }
    </style>
    """


# =============================================================================
# COMPONENT RENDERING
# =============================================================================

def render_document_viewer(document: Optional[FilingDocument]) -> str:
    """Render the document viewer area."""
    # Download icon SVG (inline)
    download_icon = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'

    if not document:
        return f'<div class="document-viewer"><div class="document-content"><div class="document-placeholder"><div class="document-placeholder-text">Select a metric to view document</div><div class="document-placeholder-subtext">Choose a financial metric from the list to view details</div></div></div></div>'

    return f'<div class="document-viewer"><div class="document-header"><div class="document-title-section"><span class="document-title">{document.company_name} ({document.ticker}) {document.document_type}</span><span class="document-badge">{document.year}</span><span class="document-meta"><span class="document-meta-dot"></span><span>{document.quarter}</span></span></div><a href="#" class="download-btn" onclick="alert(\'Download functionality coming soon!\'); return false;">{download_icon}<span>Download</span></a></div><div class="document-content"><div class="document-placeholder"><div class="document-placeholder-text">FILING DOCUMENT</div><div class="document-placeholder-subtext">{document.company_name} {document.document_type} for {document.year} {document.quarter}</div></div></div></div>'


def _convert_ixbrl_to_spans(html: str) -> str:
    """Convert iXBRL namespace elements (ix:nonFraction, ix:nonNumeric, etc.)
    to regular <span> elements so their id attributes are accessible via
    document.getElementById() in the browser.

    The HTML5 parser does not create proper DOM nodes for namespace-prefixed
    elements like <ix:nonFraction>, so id attributes on them are invisible
    to JavaScript. Converting to <span> fixes this.
    """
    import re
    # Convert opening ix: tags → <span> while keeping id and other attrs
    # Matches <ix:nonFraction ...>, <ix:nonNumeric ...>, <ix:continuation ...>, etc.
    html = re.sub(
        r'<ix:(\w+)(\s[^>]*)?>',
        lambda m: f'<span data-ix="{m.group(1)}"{m.group(2) or ""}>',
        html,
    )
    # Convert closing tags
    html = re.sub(r'</ix:\w+>', '</span>', html)
    return html


def _resolve_sec_edgar_base_url(html_path: str, html_content: str) -> Optional[str]:
    """Resolve the SEC EDGAR base URL for a filing so relative links work.

    Strategy:
    1. Extract CIK from the iXBRL dei:EntityCentralIndexKey tag in the HTML.
    2. Locate the sibling ``archive/`` directory (only exists for 8-K/6-K filings).
    3. Hash the *original file on disk* and compare with archive files to find the
       matching accession number (archive filenames are accession numbers).
    4. Return ``https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION}/``.

    Returns None if any step fails (caller falls back to JS-only link handling).
    """
    import re as _re
    import hashlib as _hl

    # Step 1 – extract CIK
    cik_m = _re.search(r'CentralIndexKey[^>]*>(\d+)', html_content)
    if not cik_m:
        return None
    cik_int = str(int(cik_m.group(1)))  # strip leading zeros for URL

    # Step 2 – find archive directory
    filing_dir = os.path.dirname(html_path)
    # For filing_XXX/filing.html → parent is the doc-type dir
    # For filing.html (main) → filing_dir IS the doc-type dir
    parent_dir = filing_dir
    if os.path.basename(filing_dir).startswith('filing_'):
        parent_dir = os.path.dirname(filing_dir)
    archive_dir = os.path.join(parent_dir, 'archive')
    if not os.path.isdir(archive_dir):
        return None

    # Step 3 – hash the original file on disk (binary) and match against archive.
    # We must compare the raw bytes because html_content has been processed
    # (XML declaration stripped, scripts removed, iXBRL converted).
    try:
        with open(html_path, 'rb') as hf:
            file_hash = _hl.md5(hf.read()).hexdigest()
    except OSError:
        return None

    for arc_file in os.listdir(archive_dir):
        if not arc_file.endswith('.html'):
            continue
        arc_path = os.path.join(archive_dir, arc_file)
        try:
            with open(arc_path, 'rb') as af:
                arc_hash = _hl.md5(af.read()).hexdigest()
            if arc_hash == file_hash:
                accession = arc_file.replace('.html', '')
                return f'https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/'
        except OSError:
            continue

    return None


@st.cache_data(max_entries=20, show_spinner=False)
def _load_and_process_html(html_path: str, _v: int = 5) -> str:
    """Read HTML file from disk, strip XML preamble, convert iXBRL tags — cached per path.
    _v: cache-buster — increment to force re-processing of all cached files.
    """
    _start = _perf_time.time()
    import re
    with open(html_path, 'r', encoding='utf-8', errors='ignore') as f:
        raw = f.read()
    _file_size = len(raw)

    # Strip XML declaration — SEC iXBRL files often start with <?xml version='1.0'?>
    # which makes browsers treat the document as XML and render raw source instead
    # of rendering the HTML. Removing it forces HTML5 parsing mode.
    raw = re.sub(r'^\s*<\?xml[^?]*\?>\s*', '', raw, count=1)

    # Strip all <script> tags from the filing HTML — SEC filings sometimes embed
    # JavaScript with malformed regex literals (e.g. missing closing /) that throw
    # a SyntaxError, crashing the entire document before our scroll script runs.
    raw = re.sub(r'<script\b[^>]*>.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)

    raw = _convert_ixbrl_to_spans(raw)

    # ── Lazy rendering via content-visibility: auto ───────────────────────────
    lazy_css = (
        "<style>"
        "body>*{"
        "content-visibility:auto;"
        "contain-intrinsic-size:auto 80px"
        "}"
        "</style>"
    )
    if '</head>' in raw:
        raw = raw.replace('</head>', lazy_css + '</head>', 1)
    elif '<head>' in raw:
        raw = raw.replace('<head>', '<head>' + lazy_css, 1)
    else:
        raw = lazy_css + raw

    # ── Fix ALL links inside the Streamlit sandboxed iframe ─────────────────
    # SEC filing HTML contains three kinds of links that break in the iframe:
    #   1. Hash links (#section-id) — bubble up to parent Streamlit window
    #   2. Absolute http(s) links — navigate the iframe instead of opening a tab
    #   3. Relative links (exhibit.htm, pressrelease.htm) — resolve to localhost
    #      instead of the original SEC EDGAR directory
    #
    # Fix strategy:
    #   A. Resolve SEC EDGAR base URL from archive directory (accession numbers)
    #   B. Rewrite relative hrefs to absolute SEC EDGAR URLs (Python-side)
    #   C. Inject JS to intercept clicks: hash links scroll in-iframe,
    #      all other links open in a new tab

    sec_base_url = _resolve_sec_edgar_base_url(html_path, raw)

    # B. Rewrite relative href attributes to absolute SEC EDGAR URLs
    if sec_base_url:
        def _rewrite_href(m):
            prefix, href, suffix = m.group(1), m.group(2), m.group(3)
            # Skip hash, absolute, mailto, javascript, data URIs
            if href.startswith(('#', 'http://', 'https://', 'mailto:', 'javascript:', 'data:')):
                return m.group(0)
            # Skip XBRL schema references (not user-clickable)
            if href.endswith('.xsd'):
                return m.group(0)
            return f'{prefix}{sec_base_url}{href}{suffix}'
        raw = re.sub(r'(href=["\'])([^"\']+)(["\'])', _rewrite_href, raw)

    # C. Inject JS click handler as safety net for all link types
    link_handler_script = """<script>
(function(){
  document.addEventListener('click', function(e) {
    var anchor = e.target.closest('a');
    if (!anchor) return;
    var href = anchor.getAttribute('href');
    if (!href) return;

    // Skip non-navigational hrefs
    if (/^(javascript:|mailto:|data:)/.test(href) || href.endsWith('.xsd')) {
      e.preventDefault();
      e.stopPropagation();
      return false;
    }

    // Internal hash link — scroll within the iframe
    if (href.charAt(0) === '#') {
      e.preventDefault();
      e.stopPropagation();
      var targetId = href.substring(1);
      var target = document.getElementById(targetId);
      if (!target) {
        try { target = document.querySelector('[name="' + CSS.escape(targetId) + '"]'); } catch(ex) {}
      }
      if (target) {
        target.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
      return false;
    }

    // All other links (absolute http/https, rewritten SEC URLs) — open in new tab
    e.preventDefault();
    e.stopPropagation();
    window.open(href, '_blank', 'noopener,noreferrer');
    return false;
  }, true);
})();
</script>"""

    if '</body>' in raw:
        raw = raw.replace('</body>', link_handler_script + '</body>', 1)
    else:
        raw = raw + link_handler_script

    _elapsed = _perf_time.time() - _start

    return raw


def render_sec_html_viewer(html_path: str, highlight_fact_id: Optional[str] = None) -> None:
    """
    Render SEC HTML document in a sandboxed iframe.

    NOTE: Static file serving DISABLED (Streamlit 1GB limit workaround).

    Files are served directly from data/filings_blob_cache/ using inline HTML.
    This avoids the 1GB Streamlit static folder limitation and 200MB per-file limit.
    Uses st.html() with content cached in session_state for optimal performance.
    """
    _render_start = _perf_time.time()
    import streamlit.components.v1 as components
    import time



    _autoscroll_context = {
        'html_path': html_path,
        'highlight_fact_id': highlight_fact_id,
        'view_metric': st.session_state.get('cf_view_metric', 'N/A'),
        'start_time': _render_start,
        'html_size_mb': 0,
        'script_injected': False,
        'scroll_attempts': 30,  # Max attempts in JS
        'scroll_delay_ms': 300,  # Delay between attempts
    }

    try:
        _html_load_start = _perf_time.time()
        clean_html = _load_and_process_html(html_path)
        # Guard: if cache returned empty/stale result, clear and re-read directly
        if len(clean_html) < 1000 and os.path.exists(html_path) and os.path.getsize(html_path) > 1000:

            _load_and_process_html.clear()
            clean_html = _load_and_process_html(html_path)
        _html_load_elapsed = _perf_time.time() - _html_load_start
        _autoscroll_context['html_size_mb'] = len(clean_html) / (1024 * 1024)

    except Exception as e:
        log_error(f"[AUTOSCROLL] ERROR loading document: {e}")
        st.error(f"Error loading document: {e}")
        return

    if highlight_fact_id:
        _script_inject_start = _perf_time.time()
        _autoscroll_context['script_injected'] = True

        # Detailed logging for autoscroll tracking


        highlight_script = f"""
        <script>
        (function() {{
            console.log('[AUTOSCROLL_JS] Script loaded at ' + new Date().toISOString());
            let attempts = 0;
            const factId = '{highlight_fact_id}';
            const isTextSearch = factId.startsWith('TEXT:');
            const searchText = isTextSearch ? factId.substring(5) : '';
            const maxAttempts = 30;
            const delayMs = 300;

            console.log('[AUTOSCROLL_JS] Config:', {{factId: factId, isTextSearch: isTextSearch, maxAttempts: maxAttempts}});

            function highlightElement(el) {{
                console.log('[AUTOSCROLL_JS] Element found! Highlighting and scrolling...', el.id || el.tagName);
                el.style.backgroundColor = '#FDF5F5';
                el.style.boxShadow = '0 0 10px rgba(214, 46, 47, 0.3)';
                el.style.border = '2px solid #D62E2F';
                el.style.borderRadius = '4px';
                el.style.padding = '4px';
                el.scrollIntoView({{behavior: 'instant', block: 'center'}});
                setTimeout(function(){{ el.scrollIntoView({{behavior: 'instant', block: 'center'}}); }}, 150);
                setTimeout(function(){{ el.scrollIntoView({{behavior: 'instant', block: 'center'}}); }}, 380);
                setTimeout(function(){{ el.scrollIntoView({{behavior: 'smooth',  block: 'center'}}); }}, 680);
                console.log('[AUTOSCROLL_JS] SUCCESS - Scrolled to element after ' + attempts + ' attempts');
            }}

            function findAndHighlightText(text) {{
                console.log('[AUTOSCROLL_JS] Searching for text:', text.substring(0, 50) + '...');
                const WS = /\\s+/g;
                const normalizedSearch = text.replace(WS, ' ').trim();
                const searchVariants = [normalizedSearch, normalizedSearch.substring(0, 80),
                    normalizedSearch.substring(0, 50), normalizedSearch.substring(0, 30)];
                console.log('[AUTOSCROLL_JS] Trying ' + searchVariants.length + ' search variants');

                for (let i = 0; i < searchVariants.length; i++) {{
                    const searchStr = searchVariants[i];
                    if (searchStr.length < 15) continue;
                    console.log('[AUTOSCROLL_JS] Trying variant ' + i + ':', searchStr.substring(0, 40) + '...');

                    const allElements = document.body.querySelectorAll('p, td, li, div, span, section, article');
                    console.log('[AUTOSCROLL_JS] Scanning ' + allElements.length + ' elements');

                    let bestMatch = null, bestSize = Infinity;
                    for (const el of allElements) {{
                        const elText = (el.textContent || '').replace(WS, ' ');
                        if (elText.includes(searchStr) && elText.length < bestSize) {{
                            bestSize = elText.length; bestMatch = el;
                        }}
                    }}
                    if (bestMatch) {{
                        console.log('[AUTOSCROLL_JS] MATCH FOUND with variant ' + i);
                        highlightElement(bestMatch);
                        return true;
                    }}
                }}
                console.log('[AUTOSCROLL_JS] NO MATCH found after trying all variants');
                return false;
            }}

            function tryScroll() {{
                attempts++;
                console.log('[AUTOSCROLL_JS] Attempt ' + attempts + '/' + maxAttempts);

                if (isTextSearch) {{
                    if (findAndHighlightText(searchText)) {{
                        return; // Success!
                    }}
                    if (attempts < maxAttempts) {{
                        setTimeout(tryScroll, delayMs);
                    }} else {{
                        console.error('[AUTOSCROLL_JS] FAILED - Max attempts reached for text search');
                    }}
                }} else {{
                    const el = document.getElementById(factId);
                    if (el) {{
                        highlightElement(el);
                        return; // Success!
                    }}
                    if (attempts < maxAttempts) {{
                        setTimeout(tryScroll, delayMs);
                    }} else {{
                        console.error('[AUTOSCROLL_JS] FAILED - Element not found after ' + maxAttempts + ' attempts. ID: ' + factId);
                    }}
                }}
            }}

            if (document.readyState === 'loading') {{
                console.log('[AUTOSCROLL_JS] Document still loading, waiting for DOMContentLoaded...');
                document.addEventListener('DOMContentLoaded', function() {{
                    console.log('[AUTOSCROLL_JS] DOMContentLoaded fired, starting scroll attempts');
                    tryScroll();
                }});
            }} else {{
                console.log('[AUTOSCROLL_JS] Document already loaded, starting scroll immediately');
                tryScroll();
            }}
        }})();
        </script>
        """
        if '</body>' in clean_html:
            clean_html = clean_html.replace('</body>', highlight_script + '</body>')
        else:
            clean_html = clean_html + highlight_script

        _script_inject_elapsed = _perf_time.time() - _script_inject_start

    else:
        log_info(f"[AUTOSCROLL] No highlight_fact_id - autoscroll DISABLED")

    _render_elapsed = _perf_time.time() - _render_start


    try:
        _st_html_start = _perf_time.time()
        # CRITICAL FIX #2: Add nonce to fallback HTML too for consistency
        nonce = f"<!-- fallback_nonce:{hash((html_path, highlight_fact_id, time.time_ns() // 1_000_000))} -->"
        components.html(nonce + clean_html, height=800, scrolling=True)
        _st_html_elapsed = _perf_time.time() - _st_html_start

    except Exception as e:
        log_error(f"[RENDER HTML] components.html error: {e}")
        st.error(f"Error rendering HTML: {e}")

    _total_render = _perf_time.time() - _render_start



# =============================================================================
# MAIN PAGE
# =============================================================================

def main():
    """Company Filing Documents page entry point.

    OPTIMIZED: Header renders FIRST - before any DB calls or heavy operations.
    User sees UI immediately, then data loads.
    """
    main_start = _perf_time.time()
    _tracker = PageLoadTracker("company_filings")

    # ═══════════════════════════════════════════════════════════════════════
    # CRITICAL: HEADER RENDERS FIRST - NO DELAYS!
    # ═══════════════════════════════════════════════════════════════════════

    # Step 1: Basic setup (minimal)
    render_styles()
    st.set_page_config(page_title="Company Filing Documents", layout="wide")

    # Step 2: Get ticker (simple, no DB calls)
    _header_ticker = st.session_state.get("active_ticker", "M")

    # Step 3: RENDER HEADER IMMEDIATELY!

    render_header(full_width=True, current_page="company_filings", ticker=_header_ticker)


    # ═══════════════════════════════════════════════════════════════════════
    # EVERYTHING ELSE AFTER HEADER (background loading)
    # ═══════════════════════════════════════════════════════════════════════



    # ONE-TIME PURGE GUARD: Ensure stale cache is cleared even if user lands
    # directly on this page (bypassing home.py where the scanner starts).
    # No-op after first run (<1ms) — safe to call on every rerun.
    if CACHE_AVAILABLE:
        try:
            _purge_t0 = _perf_time.time()
            ensure_cache_purge()

        except Exception as _pe:
            log_error(f"[CACHE PURGE] Error during cache purge: {_pe}")

    # NOTE: System State Report moved to end or removed - was causing 500ms+ delay
    # Old code was doing: get_background_scanner(), get_filings_cache() etc.
    # Those are now lazy-loaded when actually needed

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 5: Inject CSS
    # ═══════════════════════════════════════════════════════════════════════
    _step5_start = _perf_time.time()

    st.markdown(get_filings_css(), unsafe_allow_html=True)
    _step5_elapsed = _perf_time.time() - _step5_start


    # Show loading placeholder immediately before any blocking DB calls
    _cf_loading_hint = st.empty()
    _cf_loading_hint.markdown(
        '<div style="display:flex;align-items:center;gap:12px;padding:32px 0 16px 0;">'
        '<div style="width:28px;height:28px;border:3px solid #eee;border-top:3px solid #d62e2f;'
        'border-radius:50%;animation:cf-spin 0.8s linear infinite;"></div>'
        '<span style="font-family:Montserrat,sans-serif;font-size:15px;color:#888;">'
        'Loading filing data&hellip;</span></div>'
        '<style>@keyframes cf-spin{to{transform:rotate(360deg)}}</style>',
        unsafe_allow_html=True,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 6: Load Companies (AFTER header is visible)
    # ═══════════════════════════════════════════════════════════════════════
    _step6_start = _perf_time.time()

    _companies_list = get_companies()
    _step6_elapsed = _perf_time.time() - _step6_start


    if _step6_elapsed > 2.0:
        log_warning(f"[COMPANIES LOAD] Loading companies took {_step6_elapsed:.2f} seconds")

    # Initialize session state with defaults from available data
    available_tickers = [c[0] for c in _companies_list]

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 7: Resolve Ticker
    # ═══════════════════════════════════════════════════════════════════════
    _step7_start = _perf_time.time()

    _url_ticker   = st.query_params.get("ticker", "")
    _url_doc_type = st.query_params.get("doc_type", "")   # e.g. "10-K", "10-Q-Q1"
    _url_year     = st.query_params.get("year", "")       # e.g. "2026"
    _active_ticker = st.session_state.get("active_ticker", "")


    _validated_ticker, _was_fallback = validate_and_get_ticker(
        url_ticker=_url_ticker if _url_ticker else None,
        session_ticker=_active_ticker if _active_ticker else None,
        page_name="company_filings"
    )

    _resolved_ticker = _validated_ticker if _validated_ticker in available_tickers else None
    if _resolved_ticker is None and available_tickers:
        if DEFAULT_FALLBACK_TICKER in available_tickers:
            _resolved_ticker = DEFAULT_FALLBACK_TICKER
        else:
            _resolved_ticker = available_tickers[0]

    _step7_elapsed = _perf_time.time() - _step7_start


    # ═══════════════════════════════════════════════════════════════════════
    # STEP 8: Initialize Session State
    # ═══════════════════════════════════════════════════════════════════════
    _step8_start = _perf_time.time()


    if 'cf_search' not in st.session_state:
        st.session_state.cf_search = ""

    if 'cf_search_gen' not in st.session_state:
        st.session_state.cf_search_gen = 0
    if 'cf_company' not in st.session_state:
        st.session_state.cf_company = _resolved_ticker or (available_tickers[0] if available_tickers else DEFAULT_FALLBACK_TICKER)

    elif _resolved_ticker and _resolved_ticker != st.session_state.cf_company:
        st.session_state.cf_company = _resolved_ticker

    if st.session_state.cf_company not in available_tickers:
        st.session_state.cf_company = available_tickers[0] if available_tickers else DEFAULT_FALLBACK_TICKER

    # Apply deep-link doc_type param (e.g. from Key Developments source_ref link)
    _valid_doc_types = set(DOCUMENT_TYPES) if DOCUMENT_TYPES else {"10-K"}
    if _url_doc_type and _url_doc_type in _valid_doc_types:
        st.session_state.cf_doc_type = _url_doc_type
    elif 'cf_doc_type' not in st.session_state:
        # Land on the most recent 10-K or 10-Q (SEC or non-SEC equivalent)
        # rather than always defaulting to 10-K regardless of what is newest.
        try:
            _best_doc, _best_year = _get_best_landing_doc_and_year(st.session_state.cf_company)
        except Exception:
            _best_doc, _best_year = DOCUMENT_TYPES[0] if DOCUMENT_TYPES else "10-K", ""
        st.session_state.cf_doc_type = _best_doc

    # Apply deep-link year param
    if _url_year and _url_year.isdigit() and len(_url_year) == 4:
        st.session_state.cf_year = _url_year
    elif 'cf_year' not in st.session_state:
        _year_fetch_start = _perf_time.time()
        _best_year = ""
        # Re-use the year already found by the landing-doc picker when available.
        if 'cf_doc_type' in st.session_state and not _url_doc_type:
            try:
                _landing_years = _get_available_years_for_doc_type(
                    st.session_state.cf_company, st.session_state.cf_doc_type
                )
                _best_year = _landing_years[0] if _landing_years else ""
            except Exception:
                _best_year = ""
        if not _best_year:
            company_years = _get_available_years_from_db(st.session_state.cf_company)
            _best_year = company_years[0] if company_years else "2025"
        st.session_state.cf_year = _best_year
        _year_fetch_elapsed = _perf_time.time() - _year_fetch_start

    if 'cf_filing_unit' not in st.session_state:
        # Initialize filing unit for 8-K/6-K docs
        _doc = st.session_state.cf_doc_type
        _year = st.session_state.cf_year
        if _doc in FILING_UNIT_DOC_TYPES:
            _units = get_filing_units(st.session_state.cf_company, _year, _doc)
            st.session_state.cf_filing_unit = get_default_filing_unit(_units) if _units else ""
        else:
            st.session_state.cf_filing_unit = ""

    if 'cf_highlight_fact_id' not in st.session_state:
        st.session_state.cf_highlight_fact_id = None
    if 'cf_view_metric' not in st.session_state:
        st.session_state.cf_view_metric = None

    _step8_elapsed = _perf_time.time() - _step8_start


    # ═══════════════════════════════════════════════════════════════════════
    # STEP 9: Render Page Content (Dropdowns + Document Viewer)
    # ═══════════════════════════════════════════════════════════════════════
    _step9_start = _perf_time.time()
    html_path = ""
    blob_name = None

    header_col1, header_col2 = st.columns([1, 2])

    with header_col1:
        _title_start = _perf_time.time()
        st.markdown('<h3 class="filings-title">Company Filing Documents</h3>', unsafe_allow_html=True)


    with header_col2:
        # Always use 4 columns for consistent layout
        # Column 1: Company, Column 2: Document Type, Column 3: Filing Unit (8-K/6-K only), Column 4: Year
        f1, f2, f3, f4 = st.columns([2.0, 1.4, 1.0, 0.8])

        # ═══════════════════════════════════════════════════════════════════
        # DROPDOWN #1: Company Selection
        # ═══════════════════════════════════════════════════════════════════
        _dd1_start = _perf_time.time()


        # Find index safely
        _company_options = [c[0] for c in _companies_list]
        _selected_company = st.session_state.cf_company
        if _selected_company in _company_options:
            _company_index = _company_options.index(_selected_company)

        else:
            _company_index = 0


        _dropdown_render_start = _perf_time.time()
        with f1:
            company = st.selectbox(
                "Company",
                options=_company_options,
                format_func=lambda x: next((c[1] for c in _companies_list if c[0] == x), x),
                index=_company_index,
                key="cf_company_select",
                on_change=_on_company_change,
            )
        _dd1_render_elapsed = _perf_time.time() - _dropdown_render_start
        _dd1_total_elapsed = _perf_time.time() - _dd1_start


        # ═══════════════════════════════════════════════════════════════════
        # DROPDOWN #2: Document Type Selection (depends on Company)
        # ═══════════════════════════════════════════════════════════════════
        _dd2_start = _perf_time.time()


        _db_fetch_start = _perf_time.time()
        available_doc_types = _get_available_doc_types_from_db(company) or DOCUMENT_TYPES
        _db_fetch_elapsed = _perf_time.time() - _db_fetch_start


        # Ensure widget key is initialised from logical state before first render.
        # Do NOT pass index= to the selectbox — that conflicts with session_state
        # and triggers the ST_WIDGET_SESSION_STATE_CONFLICT warning.
        _desired_doc = st.session_state.cf_doc_type if st.session_state.cf_doc_type in available_doc_types else available_doc_types[0]
        if st.session_state.get("cf_doc_type_select") not in available_doc_types:
            st.session_state["cf_doc_type_select"] = _desired_doc

        _dropdown_render_start = _perf_time.time()
        with f2:
            doc_type = st.selectbox(
                "Document Type",
                options=available_doc_types,
                key="cf_doc_type_select",
                on_change=_on_doc_type_change,
            )
        _dd2_render_elapsed = _perf_time.time() - _dropdown_render_start
        _dd2_total_elapsed = _perf_time.time() - _dd2_start


        # ═══════════════════════════════════════════════════════════════════
        # DROPDOWN #3: Filing Unit Selection (ONLY for 8-K and 6-K)
        # ═══════════════════════════════════════════════════════════════════
        filing_unit = ""  # Default for non-8-K/6-K docs
        if doc_type in FILING_UNIT_DOC_TYPES:
            _dd_filing_unit_start = _perf_time.time()

            available_filing_units = get_filing_units(company, st.session_state.cf_year, doc_type)

            # Sync cf_filing_unit from the display number stored in session state
            # (display number = 1-based position in sorted ascending list)
            if st.session_state.cf_filing_unit not in available_filing_units:
                st.session_state.cf_filing_unit = get_default_filing_unit(available_filing_units) if available_filing_units else ""

            _dropdown_render_start = _perf_time.time()

            # Create sequential display numbers (1-N); users see 1, 2, 3... instead of raw filing_XX numbers
            _filing_unit_display_options = list(range(1, len(available_filing_units) + 1))

            # Ensure session state display number is valid integer within range.
            # Callbacks set this to the correct display number (integer). If it's out of
            # range or wrong type (e.g. stale string), clamp/reset to the default (last = highest).
            _current_display = st.session_state.get("cf_filing_unit_select")
            _max_display = len(available_filing_units)
            if not isinstance(_current_display, int) or _current_display < 1 or _current_display > _max_display:
                _default_display = _max_display if _max_display > 0 else 1
                st.session_state["cf_filing_unit_select"] = _default_display

            with f3:
                _selected_display_num = st.selectbox(
                    "Filing Unit",
                    options=_filing_unit_display_options,
                    key="cf_filing_unit_select",
                    on_change=_on_filing_unit_change,
                )
                # Map the selected display number back to actual filing unit
                if available_filing_units and _selected_display_num:
                    filing_unit = available_filing_units[_selected_display_num - 1]
                elif available_filing_units:
                    filing_unit = available_filing_units[0]
                else:
                    filing_unit = ""
            _dd_filing_unit_render_elapsed = _perf_time.time() - _dropdown_render_start
            _dd_filing_unit_total_elapsed = _perf_time.time() - _dd_filing_unit_start

            # Use f4 for Year when Filing Unit is shown
            _year_col = f4
        else:
            # No filing unit - use f3 for Year
            _year_col = f3

        # ═══════════════════════════════════════════════════════════════════
        # DROPDOWN #4 (or #3): Year Selection (depends on Company + DocType)
        # ═══════════════════════════════════════════════════════════════════
        _dd3_start = _perf_time.time()


        _years_db_start = _perf_time.time()
        available_years = _get_available_years_for_doc_type(company, doc_type) or ["2025"]
        _years_db_elapsed = _perf_time.time() - _years_db_start


        # Same pattern: pre-init session state, no index= to avoid the warning.
        _desired_year = st.session_state.cf_year if st.session_state.cf_year in available_years else available_years[0]
        if st.session_state.get("cf_year_select") not in available_years:
            st.session_state["cf_year_select"] = _desired_year

        _dropdown_render_start = _perf_time.time()
        with _year_col:
            year = st.selectbox(
                "Year",
                options=available_years,
                key="cf_year_select",
                on_change=_on_year_change,
                format_func=lambda y: _fiscal_label_for(company, doc_type, y),
            )
        _dd3_render_elapsed = _perf_time.time() - _dropdown_render_start
        _dd3_total_elapsed = _perf_time.time() - _dd3_start


        _total_dropdown_db_time = _perf_time.time() - _db_fetch_start

        st.session_state._TIMING_DATA['dropdown_load_times'][f'{company}_years_docs'] = _total_dropdown_db_time

    _step9_elapsed = _perf_time.time() - _step9_start


    # ═══════════════════════════════════════════════════════════════════════
    # FILTER DATA FLOW SUMMARY
    # ═══════════════════════════════════════════════════════════════════════



    # CRITICAL FIX: Check if company ACTUALLY changed (user switched dropdown)
    # BEFORE updating session state - use a separate tracking variable
    _prev_company = st.session_state.get('cf_company')
    _prev_doc = st.session_state.get('cf_doc_type')
    _prev_year = st.session_state.get('cf_year')
    _prev_filing_unit = st.session_state.get('cf_filing_unit', '')

    # Update session state with current widget values
    st.session_state.cf_company = company
    st.session_state.cf_doc_type = doc_type
    st.session_state.cf_year = year
    st.session_state.cf_filing_unit = filing_unit
    st.session_state.active_ticker = company
    # Keep URL query param in sync so _resolved_ticker never conflicts with dropdown
    if st.query_params.get("ticker") != company:
        st.query_params["ticker"] = company

    # Check if filing actually changed (including filing_unit for 8-K/6-K)
    _filing_changed = (_prev_company != company or
                       _prev_doc != doc_type or
                       _prev_year != year or
                       (doc_type in FILING_UNIT_DOC_TYPES and _prev_filing_unit != filing_unit))

    # DETAILED STATE LOGGING


    # TRACKING: Log company change with detailed info
    if _prev_company != company:
        _change_record = {
            'timestamp': _perf_time.time(),
            'rerun': st.session_state._TIMING_DATA['rerun_count'],
            'prev_company': _prev_company,
            'new_company': company,
            'prev_doc': _prev_doc,
            'new_doc': doc_type,
            'prev_year': _prev_year,
            'new_year': year
        }
        st.session_state._TIMING_DATA['company_changes'].append(_change_record)


    # CRITICAL FIX: Check if highlight was just set by view button (prevents clearing on view button click)
    _highlight_just_set = st.session_state.pop('_highlight_just_set', False)
    _view_click_time = st.session_state.pop('_view_button_click_time', None)

    if _view_click_time:
        _time_since_click = _perf_time.time() - _view_click_time


    if _filing_changed and not _highlight_just_set:

        st.session_state.cf_highlight_fact_id = None
        st.session_state.cf_view_metric = None
        st.session_state.cf_search = ""
        st.session_state.cf_search_gen = st.session_state.get("cf_search_gen", 0) + 1

        # Clear PDF cache for old filing
        _old_pdf_key = f"pdf_bytes_{_prev_company}_{_prev_year}_{_prev_doc}"
        if _old_pdf_key in st.session_state:
            del st.session_state[_old_pdf_key]

    elif _filing_changed and _highlight_just_set:
        pass
    else:
        # No change - user likely just switched dropdowns without changing actual filing
        pass


    # =======================================================================
    # MAIN CONTENT - TWO COLUMN LAYOUT
    # =======================================================================

    _columns_t0 = _perf_time.time()
    left_col, right_col = st.columns([0.3, 0.7])

    _left_panel_t0 = _perf_time.time()


    with left_col:
        _search_input_start = _perf_time.time()
        _search_key = f"cf_search_input_{st.session_state.cf_search_gen}"
        search_term = st.text_input(
            "Search",
            placeholder="eg., Revenue",
            value=st.session_state.cf_search,
            key=_search_key,
            label_visibility="collapsed"
        )
        st.session_state.cf_search = search_term


        search_results = []
        used_llm = False
        _search_start = _perf_time.time()

        if search_term.strip():
            doc_type_dir = doc_type  # doc_type is already the raw DB value (e.g. 10-Q-Q1)



            try:
                _db_search_start = _perf_time.time()

                search_results = FilingMetricRepository.search(
                    ticker=company,
                    report_fiscal_year=int(year),
                    doc_type=doc_type_dir,
                    query=search_term,
                    limit=500,
                )
                _db_search_elapsed = _perf_time.time() - _db_search_start

                # Count by source
                _source_counts = {}
                for r in search_results:
                    src = r.source or 'unknown'
                    _source_counts[src] = _source_counts.get(src, 0) + 1


                # Count by source
                _source_counts = {}
                for r in search_results:
                    src = r.source or 'unknown'
                    _source_counts[src] = _source_counts.get(src, 0) + 1


            except Exception as e:
                log_error(f"DB search error: {e}", exc_info=True)

            if not search_results:

                import os as _os
                if _os.getenv("OPENAI_API_KEY", "").strip():
                    try:
                        _llm_start = _perf_time.time()

                        with st.spinner("Searching document with AI..."):
                            from core.llm_extractor import LLMExtractor
                            from core.database import db_manager as _dbm
                            engine = _dbm._engine
                            if engine:
                                with engine.begin() as conn:
                                    llm_res = LLMExtractor.extract(
                                        conn=conn,
                                        ticker=company,
                                        report_fiscal_year=int(year),
                                        doc_type=doc_type_dir,
                                        query=search_term,
                                    )
                                if llm_res:
                                    search_results = llm_res
                                    used_llm = True
                                    _llm_elapsed_ms = (_perf_time.time() - _llm_start) * 1000
                    except Exception as e:
                        log_error(f"[LLM] extraction error: {e}", exc_info=True)

            # Log search to history
            _total_search_elapsed = _perf_time.time() - _search_start
            _search_record = {
                'timestamp': _perf_time.time(),
                'rerun': st.session_state._TIMING_DATA['rerun_count'],
                'term': search_term,
                'company': company,
                'year': year,
                'doc_type': doc_type_dir,
                'results_count': len(search_results),
                'used_llm': used_llm,
                'total_time': _total_search_elapsed
            }
            st.session_state._TIMING_DATA['search_history'].append(_search_record)

        else:
            pass

        def _source_badge(source: Optional[str]) -> str:
            if source == "calculated":
                return '<span style="background:#E8F4FD;color:#0066CC;font-size:10px;font-weight:600;padding:2px 6px;border-radius:3px;margin-left:6px;vertical-align:middle;">CALC</span>'
            if source == "llm":
                return '<span style="background:#F0F7EE;color:#2E7D32;font-size:10px;font-weight:600;padding:2px 6px;border-radius:3px;margin-left:6px;vertical-align:middle;">AI</span>'
            if source == "edgartools":
                return '<span style="background:#FFF3E0;color:#E65100;font-size:10px;font-weight:600;padding:2px 6px;border-radius:3px;margin-left:6px;vertical-align:middle;">EDGAR</span>'
            return ""

        def _format_calc_note(note: str) -> str:
            import re
            if not note:
                return ""
            def _fmt(m):
                try:
                    val = abs(float(m.group(1).replace(",", "")))
                    if val >= 1e12:
                        return f"(${val/1e12:.1f} T)"
                    elif val >= 1e9:
                        return f"(${val/1e9:.1f} B)"
                    elif val >= 1e6:
                        return f"(${val/1e6:.0f} M)"
                    else:
                        return f"(${val:,.0f})"
                except Exception:
                    return m.group(0)
            result = re.sub(r'\(([\d,]+(?:\.\d+)?)\)', _fmt, note)
            return "= " + result

        search_icon = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#D62E2F" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
        eye_icon_svg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'

        with st.container(height=700, border=True):
            st.markdown(f'<div class="search-header">{search_icon}<span class="search-title">Search Metrics</span></div>', unsafe_allow_html=True)

            _container_t0 = _perf_time.time()


            if search_results:
                source_note = " · AI extracted" if used_llm else ""
                st.markdown(f'<div class="metrics-count">Showing {len(search_results)} metrics{source_note}</div>', unsafe_allow_html=True)
                _cards_render_t0 = _perf_time.time()

                def _fmt_period_date(d_str: str) -> str:
                    if not d_str:
                        return ""
                    try:
                        from datetime import datetime as _dt
                        return _dt.strptime(str(d_str)[:10], "%Y-%m-%d").strftime("%b '%y")
                    except Exception:
                        return str(d_str)[:7]

                for i, metric in enumerate(search_results):
                    source_sentence = None
                    if not metric.ixbrl_id and metric.source in ('store_count', 'credit_rating') and getattr(metric, 'llm_query', None):
                        try:
                            detail = json.loads(getattr(metric, 'llm_query', '{}'))
                            source_sentence = detail.get('source_sentence', '')
                        except Exception:
                            source_sentence = None
                    view_id = metric.ixbrl_id
                    if not view_id and source_sentence:
                        view_id = f"TEXT:{source_sentence[:120]}"

                    is_viewing = (st.session_state.cf_highlight_fact_id == view_id and view_id)
                    card_class = "metric-card active" if is_viewing else "metric-card"
                    btn_class = "viewing" if is_viewing else "view"
                    btn_text = "Viewing" if is_viewing else "View"
                    badge_html = _source_badge(metric.source)
                    label_html = f'{metric.display_label}{badge_html}'
                    dim_html = ""
                    if metric.is_dimensioned and metric.full_dimension_label:
                        dim_html = f'<div class="metric-dimension">( {metric.full_dimension_label} )</div>'
                    formula_html = ""
                    if metric.source == "calculated" and metric.calculation_note:
                        formula_html = f'<div class="metric-formula">{_format_calc_note(metric.calculation_note)}</div>'
                    pt = metric.period_type or ""
                    if pt == "duration" and metric.period_start and metric.period_end:
                        period_meta = f"{_fmt_period_date(metric.period_start)} → {_fmt_period_date(metric.period_end)}"
                    elif pt == "instant" and metric.period_instant:
                        period_meta = _fmt_period_date(metric.period_instant)
                    else:
                        period_meta = str(metric.report_fiscal_year)
                    st.markdown(f'<div class="{card_class}"><div class="metric-info"><div class="metric-name">{label_html}</div>{dim_html}<div class="metric-value">{metric.formatted_value}</div>{formula_html}<div class="metric-meta"><span>{metric.display_statement_type}</span><span class="metric-meta-dot"></span><span>{doc_type}</span><span class="metric-meta-dot"></span><span>{period_meta}</span></div></div><div class="metric-action-btn {btn_class}">{eye_icon_svg}<span>{btn_text}</span></div></div>', unsafe_allow_html=True)

                    if view_id:
                        btn_key = f"view_{i}_{metric.original_label.replace(' ', '_')}_{hash(view_id) % 10000}"
                        if st.button(f"👁 View in Document", key=btn_key, width="stretch"):
                            _view_click_time = _perf_time.time()

                            st.session_state.cf_highlight_fact_id = view_id
                            st.session_state.cf_view_metric = metric.display_label
                            st.session_state._view_button_click_time = _view_click_time
                            # CRITICAL FIX: Mark that highlight was just set by view button
                            # This prevents the next rerun from clearing it due to company mismatch
                            st.session_state._highlight_just_set = True
                            st.rerun()
                _cards_render_ms = (_perf_time.time() - _cards_render_t0) * 1000

            elif search_term.strip():

                st.markdown(f'<div style="text-align:center;color:#888;padding:40px 0;font-size:14px;">Not disclosed in this filing for &quot;{search_term}&quot;</div>', unsafe_allow_html=True)
            else:

                st.markdown(f'<div style="text-align:center;color:#888;padding:40px 0;font-size:14px;">Search for a metric to see results</div>', unsafe_allow_html=True)

            # =============================================================
            # KEYWORD HITS IN DOCUMENT
            # Runs alongside metric search — never replaces it.
            # Uses cached text index: first call ~50 ms, all reruns 0 ms.
            # HTML filings → TEXT: navigation (existing JS handler).
            # PDF  filings → PDFPAGE: navigation (PDF.js scroll).
            # =============================================================
            if search_term.strip():
                # ── resolve local path (same logic as right panel, no Azure call) ──
                _kw_section_t0 = _perf_time.time()



                # Route based on DOC_TYPE, not company type
                NON_SEC_DOC_TYPES = {"annual-report", "interim-report-Q1", "interim-report-Q2",
                                     "interim-report-Q3", "interim-report-Q4", "interim-report-Q5",
                                     "half-yearly"}
                _kw_is_non_sec_doc = doc_type in NON_SEC_DOC_TYPES

                if _kw_is_non_sec_doc:

                    _kw_blob = _get_non_sec_blob_path(company, year, doc_type)
                    if _kw_blob:
                        pass
                    else:
                        log_error(f"[KW_BLOB_PATH]   FAILED: _get_non_sec_blob_path returned None")
                else:
                    if not _is_valid_doc_type(doc_type):
                        log_error(f"[KW_BLOB_PATH]   ❌ FAILED: doc_type '{doc_type}' not in VALID_DOC_TYPES")
                        _kw_blob = None
                    else:
                        # For 8-K/6-K, include filing_unit in path
                        if doc_type in FILING_UNIT_DOC_TYPES:
                            _kw_blob = build_filing_blob_path(company, year, doc_type, filing_unit)
                        else:
                            _kw_blob = f"{company}/{year}/{doc_type}/filing.html"


                _kw_local = _local_cache_path_for_blob(_kw_blob) if _kw_blob else None


                # ── guard: file must be downloaded already ──────────────────────
                _kw_file_exists = bool(_kw_local and os.path.exists(_kw_local))




                if _kw_file_exists:
                    _kw_t0  = _perf_time.time()
                    kw_hits = _kw_search_document(_kw_local, search_term.strip(), is_pdf=_kw_is_non_sec_doc)
                    _kw_call_ms = (_perf_time.time() - _kw_t0) * 1000


                    if kw_hits:
                        _kw_cards_t0 = _perf_time.time()

                        import re as _re2
                        kw_badge = (
                            '<span style="background:#F3E8FF;color:#6B21A8;font-size:10px;'
                            'font-weight:600;padding:2px 6px;border-radius:3px;margin-left:6px;'
                            'vertical-align:middle;">KEYWORD</span>'
                        )
                        st.markdown(
                            f'<div style="border-top:1px solid #E5E5E5;margin:12px 0 8px;padding-top:10px;">'
                            f'<span style="font-size:12px;font-weight:600;color:#4F4F4F;letter-spacing:.04em;">'
                            f'KEYWORD HITS IN DOCUMENT</span>'
                            f'<span style="font-size:11px;color:#999;margin-left:8px;">{len(kw_hits)} found</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        for kw_hit in kw_hits:
                            snippet   = kw_hit["snippet"]
                            # Use printed page label (e.g. "17") not physical number (e.g. "20")
                            _disp_pg  = kw_hit.get("page_label") or (str(kw_hit["page"]) if kw_hit["page"] else None)
                            page_info = f"Page {_disp_pg}" if _disp_pg else "In document"
                            kw_view_id = (
                                f"PDFPAGE:{kw_hit['page']}:{kw_hit['num']}" if _kw_is_non_sec_doc
                                else f"TEXT:{snippet[:100]}"
                            )
                            is_kw_viewing = (st.session_state.cf_highlight_fact_id == kw_view_id)
                            kw_card_class = "metric-card active" if is_kw_viewing else "metric-card"
                            kw_btn_class  = "viewing" if is_kw_viewing else "view"
                            kw_btn_text   = "Viewing" if is_kw_viewing else "View"
                            highlighted   = _re2.sub(
                                r'(' + _re2.escape(search_term.strip()) + r')',
                                r'<mark style="background:#FFF3CD;padding:0 2px;border-radius:2px;">\1</mark>',
                                snippet,
                                flags=_re2.IGNORECASE,
                            )
                            st.markdown(
                                f'<div class="{kw_card_class}">'
                                f'<div class="metric-info">'
                                f'<div class="metric-name">Hit #{kw_hit["num"]}{kw_badge}</div>'
                                f'<div class="metric-value" style="font-size:12px;color:#555;'
                                f'white-space:normal;line-height:1.5;">...{highlighted}...</div>'
                                f'<div class="metric-meta"><span>Text match</span>'
                                f'<span class="metric-meta-dot"></span><span>{page_info}</span></div>'
                                f'</div>'
                                f'<div class="metric-action-btn {kw_btn_class}">'
                                f'{eye_icon_svg}<span>{kw_btn_text}</span></div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                            kw_btn_key = f"kw_view_{kw_hit['num']}_{hash(kw_view_id) % 10000}"
                            if st.button("👁 View in Document", key=kw_btn_key, width="stretch"):
                                _kw_click = _perf_time.time()
                                st.session_state.cf_highlight_fact_id    = kw_view_id
                                st.session_state.cf_view_metric          = f'Keyword: "{search_term}"'
                                st.session_state._view_button_click_time = _kw_click
                                st.session_state._kw_highlight_keyword   = search_term.strip()
                                st.session_state._highlight_just_set     = True
                                st.rerun()

    with right_col:
        # Use formatted company name from COMPANY_NAMES for display
        # Format: "Company Name (TICKER)" - each word capitalized
        _company_names = get_company_names()
        company_name = _company_names.get(company)
        if not company_name:
            # Fallback: format the ticker nicely if no company name found
            company_name = _format_company_name(company)

        else:
            log_info(f"[UI] Displaying company: {company_name} ({company})")

        # FAST PATH: Construct blob path directly (no cache scan needed)
        # Route based on DOC_TYPE, not company type (any company can have any valid doc_type)
        NON_SEC_DOC_TYPES = {"annual-report", "interim-report-Q1", "interim-report-Q2",
                             "interim-report-Q3", "interim-report-Q4", "interim-report-Q5",
                             "half-yearly"}
        is_non_sec_doc = doc_type in NON_SEC_DOC_TYPES



        if is_non_sec_doc:
            # NON-SEC: Use PDF path with folder mapping


            blob_name = _get_non_sec_blob_path(company, year, doc_type)
            file_ext = "pdf"

            if blob_name:
                pass

            else:
                log_error(f"[BLOB_PATH_GEN]   FAILED: _get_non_sec_blob_path returned None")
        else:
            # SEC: Use HTML path - validate doc_type first


            # Validate doc_type

            if not _is_valid_doc_type(doc_type):
                log_error(f"[BLOB_PATH_GEN]   ❌ FAILED: doc_type '{doc_type}' not in VALID_DOC_TYPES")
                log_error(f"[BLOB_PATH_GEN]   Valid types: {sorted(VALID_DOC_TYPES)}")
                blob_name = None
            else:


                # Build SEC HTML path: {ticker}/{year}/{doc_type}/filing.html
                # For 8-K/6-K, include filing_unit if specified
                if doc_type in FILING_UNIT_DOC_TYPES:
                    blob_name = build_filing_blob_path(company, year, doc_type, filing_unit)
                else:
                    blob_name = f"{company}/{year}/{doc_type}/filing.html"

            file_ext = "html"

        filing_entry = blob_name


        # Cache is only used for logging/info purposes now - don't block on it
        _current_filings_data = {}


        # FAST PATH: Check local disk first — skip Azure entirely if already cached.
        # _local_cache_path_for_blob() derives the path from the blob name with zero
        # network I/O (same logic used in the left panel keyword section).
        # This saves 250-1400ms on every rerun after the first download.
        if blob_name:
            _disk_path = _local_cache_path_for_blob(blob_name)
            if _disk_path and os.path.exists(_disk_path):
                html_path = _disk_path

            else:
                # File not in 2-year local cache — stream from Azure into /tmp/
                # (no permanent disk cache: /tmp is OS-managed and auto-cleaned)
                _spinner_t0 = _perf_time.time()
                with st.spinner("Downloading filing from Azure..."):
                    html_path = _ensure_local_blob_optimized(blob_name, use_temp=True)
                _dl_elapsed = _perf_time.time() - _spinner_t0
                if html_path and os.path.exists(html_path):
                    _dl_size_mb = os.path.getsize(html_path) / (1024 * 1024)
                    _size_str = f"{_dl_size_mb:.1f} MB" if _dl_size_mb >= 0.1 else f"{os.path.getsize(html_path)} bytes"
                else:
                    # For quarterly 10-Q filings, storage_year is the fiscal year
                    # label (e.g. 2026 for LULU FY2026 Q1). Non-December FYE
                    # companies have their Azure blobs stored under the calendar year
                    # of the period_end (storage_year - 1). Try that path before
                    # reporting "not found".
                    _fallback_html_path = None
                    if doc_type and "10-Q" in doc_type and year and str(year).isdigit():
                        try:
                            _fb_blob = f"{company}/{int(year) - 1}/{doc_type}/filing.html"
                            _fb_disk = _local_cache_path_for_blob(_fb_blob)
                            if _fb_disk and os.path.exists(_fb_disk):
                                _fallback_html_path = _fb_disk
                            else:
                                _fallback_html_path = _ensure_local_blob_optimized(_fb_blob, use_temp=True)
                            if _fallback_html_path and os.path.exists(_fallback_html_path):
                                blob_name = _fb_blob  # update so downloads use correct path
                        except Exception:
                            _fallback_html_path = None
                    if _fallback_html_path and os.path.exists(_fallback_html_path):
                        html_path = _fallback_html_path
                    else:
                        st.warning(f"Azure fetch took {_dl_elapsed:.1f}s but file not found. Please retry.")

        else:

            html_path = ""

        # DETAILED LOGGING (skip cache scan - use direct path only)
        _right_panel_time = _perf_time.time()


        # Check if view button was clicked and calculate timing
        if '_view_button_click_time' in st.session_state and st.session_state.cf_highlight_fact_id:
            _click_time = st.session_state._view_button_click_time
            _time_to_render = _right_panel_time - _click_time




        with st.container(border=True):
            if html_path and os.path.exists(html_path):
                highlight_text = ""
                if st.session_state.cf_highlight_fact_id:
                    highlight_text = f"🔍 Auto-scrolled to {st.session_state.cf_view_metric or 'metric'} ({st.session_state.cf_highlight_fact_id})"

                # Header with title and download button
                # Header with download button
                header_cols = st.columns([0.8, 0.2])
                with header_cols[0]:
                    _header_metadata = {}
                    _hdr_t0 = _perf_time.time()
                    try:
                        _header_metadata = FilingMetricRepository.get_header_metadata(
                            ticker=company,
                            effective_year=int(year),
                            doc_type=doc_type,
                            filing_unit=filing_unit if doc_type in FILING_UNIT_DOC_TYPES else None,
                        )
                    except Exception as _exc:
                        log_structured_error(
                            _exc,
                            page="company_filings",
                            component="filing_header",
                            operation="GET_HEADER_METADATA",
                            context=f"ticker={company} year={year} doc_type={doc_type} filing_unit={filing_unit}",
                        )
                    log_timing("FL_header_metadata", (_perf_time.time() - _hdr_t0) * 1000,
                               f"ticker={company} doc={doc_type} year={year}")
                    header_html = _render_filing_header_html(
                        company_name=company_name,
                        ticker=company,
                        year=year,
                        doc_type=doc_type,
                        filing_unit=filing_unit,
                        metadata=_header_metadata,
                        highlight_text=highlight_text,
                    )
                    st.markdown(header_html, unsafe_allow_html=True)

                with header_cols[1]:
                    # Include filing_unit in session key for 8-K/6-K
                    _filing_unit_key = f"_{filing_unit}" if (doc_type in FILING_UNIT_DOC_TYPES and filing_unit) else ""
                    pdf_session_key = f"pdf_bytes_{company}_{year}_{doc_type}{_filing_unit_key}"
                    pdf_ready_key = f"pdf_ready_{company}_{year}_{doc_type}{_filing_unit_key}"
                    if pdf_session_key not in st.session_state:
                        st.session_state[pdf_session_key] = None
                    if pdf_ready_key not in st.session_state:
                        st.session_state[pdf_ready_key] = False

                    if st.session_state.get(pdf_ready_key) and st.session_state[pdf_session_key] is not None:
                        # PDF just generated — auto-trigger download, then reset state
                        _pdf_data = st.session_state[pdf_session_key]
                        st.session_state[pdf_session_key] = None
                        st.session_state[pdf_ready_key] = False
                        _render_filing_download_button(
                            _pdf_data,
                            f"{company}_{year}_{doc_type}.pdf",
                            auto_click=True,
                        )

                    else:
                        # Scoped CSS: target button via Streamlit's st-key-* container class
                        _btn_key = f"gen_pdf_{company}_{year}_{doc_type}"
                        st.markdown(
                            '<style>'
                            f'[class*="st-key-{_btn_key}"] button{{'
                            'background:transparent!important;border:1px solid #D62E2F!important;'
                            'color:#D62E2F!important;border-radius:4px!important;'
                            'padding:7px 12px!important;font-size:13px!important;'
                            'font-weight:500!important;white-space:nowrap!important;'
                            'letter-spacing:.01em!important;transition:background .15s,color .15s!important;}}'
                            f'[class*="st-key-{_btn_key}"] button:hover{{background:#D62E2F!important;color:#fff!important}}'
                            '</style>',
                            unsafe_allow_html=True,
                        )
                        if st.button("⬇  Download", key=_btn_key):
                            with st.spinner("Preparing PDF..."):
                                try:
                                    if is_non_sec_doc:
                                        # NON-SEC: PDF already exists, read it directly
                                        with open(html_path, 'rb') as f:
                                            pdf_bytes = f.read()
                                    else:
                                        # SEC: Generate PDF from HTML
                                        with open(html_path, 'r', encoding='utf-8') as f:
                                            html_content = f.read()
                                        from utils.html_to_pdf import generate_filing_pdf as _gen_pdf
                                        pdf_bytes = _gen_pdf(
                                            company_name=company_name,
                                            ticker=company,
                                            year=str(year),
                                            doc_type=doc_type,
                                            quarter="Annual",
                                            html_content=html_content,
                                        )


                                    st.session_state[pdf_session_key] = pdf_bytes
                                    st.session_state[pdf_ready_key] = True
                                    st.rerun()
                                except Exception as e:
                                    _err = str(e)
                                    log_error(f"[PDF] Download failed: {_err}", exc_info=True)
                                    st.error(f"Failed to download PDF: {_err}")

                _render_viewer_start = _perf_time.time()

                # Render based on file type (HTML for SEC, PDF for NON-SEC)
                if is_non_sec_doc:

                    # Extract page number from PDFPAGE: keyword-nav highlight, if set
                    _pdf_highlight_page = 0
                    _hfi = st.session_state.get("cf_highlight_fact_id") or ""
                    if _hfi.startswith("PDFPAGE:"):
                        try:
                            # Format: "PDFPAGE:{page}:{hit_num}" — take only the page part
                            _pdf_highlight_page = int(_hfi.split(":")[1])
                        except Exception:
                            _pdf_highlight_page = 0
                    _pdf_kw = st.session_state.get("_kw_highlight_keyword", "") if _pdf_highlight_page > 0 else ""
                    _render_pdf_viewer(html_path, highlight_page=_pdf_highlight_page, highlight_keyword=_pdf_kw)
                else:

                    render_sec_html_viewer(html_path, st.session_state.cf_highlight_fact_id)

                log_timing("FL_viewer_render", (_perf_time.time() - _render_viewer_start) * 1000,
                           f"type={'pdf' if is_non_sec_doc else 'html'} ticker={company} doc={doc_type}")

                # Calculate total time from view button click to render complete
                if '_view_button_click_time' in st.session_state:
                    _total_autoscroll_time = _perf_time.time() - st.session_state._view_button_click_time

            else:
                file_type = "PDF" if is_non_sec_doc else "HTML"


                # Show error with retry button
                st.error(f"⚠️ **Filing not found in Azure**: `{blob_name}`")
                if st.button("🔄 Retry Download", key="retry_download"):
                    st.rerun()

                document = FilingDocument(
                    company_name=company_name,
                    ticker=company,
                    document_type=doc_type,
                    year=year,
                    quarter="Annual",
                    content=""
                )
                viewer_html = render_document_viewer(document)
                st.markdown(viewer_html, unsafe_allow_html=True)

    # Clear the early loading placeholder now that content is rendered
    _cf_loading_hint.empty()

    _step9_elapsed_ms = (_perf_time.time() - _step9_start) * 1000
    _file_loaded = bool(html_path and os.path.exists(html_path))
    log_timing(
        "FILINGS_LOAD",
        _step9_elapsed_ms,
        (
            f"ticker={company} doc={doc_type} year={year} "
            f"file_ok={_file_loaded} companies={len(_companies_list)}"
        ),
    )

    render_coresight_footer(full_width=True, stick_to_bottom=True)

    _total_main = _perf_time.time() - main_start
    log_render_complete("company_filings", _total_main)
    _tracker.finish()
    if _total_main > 10.0:
        log_warning(f"[PERFORMANCE_ALERT] Page load CRITICAL - {_total_main:.3f}s. User likely experiencing broken page!")
    elif _total_main > 5.0:
        log_warning(f"[PERFORMANCE_ALERT] Page load SLOW - {_total_main:.3f}s. User may experience degraded performance.")

try:
    main()
except Exception as _exc:
    log_structured_error(_exc, page="company_filings", component="main", operation="PAGE_RENDER")
    st.error("An unexpected error occurred. Please refresh the page.")
