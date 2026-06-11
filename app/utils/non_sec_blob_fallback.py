"""
NON-SEC Filing Fallback — local-first, event-driven.

DATA FLOW
---------
1. Background scanner downloads all NON-SEC PDFs into filings_blob_cache/.
2. This module reads that local folder (filesystem, <50ms for all tickers).
3. Results are cached in data/non_sec_blob_cache.json (24-hour TTL).
4. On server startup main.py calls warm_non_sec_cache_at_startup() which
   pre-populates the cache so the first page load is instant.
5. When the background scanner downloads new files it calls
   invalidate_non_sec_cache(ticker) which sets scanned_at=0, triggering a
   fresh local scan on the next access — no Azure call needed.
6. Azure Blob is only used as a fallback for tickers that have no local files
   yet (e.g. brand-new company not yet downloaded by the scanner).

CACHE TTL: 24 hours for local scans (files only change when scanner runs).
"""
import os
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Any, Tuple

_logger = logging.getLogger(__name__)

try:
    from utils.server_logger import log_structured_error, log_error
except ImportError:
    def log_error(msg, *a, **kw): _logger.error(msg)
    log_structured_error = None

# Doc types that appear in NON-SEC filing folders
NON_SEC_FALLBACK_DOC_TYPES = frozenset({
    "annual-report",
    "half-yearly",
    "interim-report-Q1",
    "interim-report-Q2",
    "interim-report-Q3",
    "interim-report-Q4",
})

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_FILE = os.path.join(_APP_ROOT, "data", "non_sec_blob_cache.json")
_LOCAL_BLOB_CACHE = os.path.join(_APP_ROOT, "data", "filings_blob_cache")

# 24-hour TTL for locally-sourced data (only changes when scanner runs)
_LOCAL_CACHE_TTL = 86_400
# 5-minute TTL for Azure-sourced data
_AZURE_CACHE_TTL = 300

# In-process memory cache so repeated calls within the same Streamlit run
# don't re-read the JSON file from disk.
_MEM_CACHE: Optional[Dict] = None
_MEM_CACHE_TS: float = 0.0
_MEM_CACHE_TTL: float = 5.0   # re-read disk at most every 5 seconds


# ─────────────────────────────────────────────────────────────────────────────
# JSON cache helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_cache() -> Dict:
    global _MEM_CACHE, _MEM_CACHE_TS
    now = time.time()
    if _MEM_CACHE is not None and (now - _MEM_CACHE_TS) < _MEM_CACHE_TTL:
        return _MEM_CACHE
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, "r") as f:
                _MEM_CACHE = json.load(f)
                _MEM_CACHE_TS = now
                return _MEM_CACHE
    except Exception:
        pass
    _MEM_CACHE = {}
    _MEM_CACHE_TS = now
    return _MEM_CACHE


def _save_cache(cache: Dict) -> None:
    global _MEM_CACHE, _MEM_CACHE_TS
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w") as f:
            json.dump(cache, f)
        _MEM_CACHE = cache
        _MEM_CACHE_TS = time.time()
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="non_sec_blob_fallback",
                                 component="_save_cache", operation="write_cache")


# ─────────────────────────────────────────────────────────────────────────────
# LOCAL filesystem scan  (primary — fast, no network)
# ─────────────────────────────────────────────────────────────────────────────

def _scan_ticker_local(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Scan data/filings_blob_cache/{ticker}/ for annual/interim PDF files.

    Returns the same shape as _prefetch_ticker_filter_data():
      {doc_types, all_years, years_by_doc_type, source, scanned_at}
    Returns None if the ticker folder doesn't exist or has no matching files.
    """
    ticker_dir = os.path.join(_LOCAL_BLOB_CACHE, ticker)
    if not os.path.isdir(ticker_dir):
        return None

    doc_types_seen: List[str] = []
    all_years: set = set()
    years_by_doc_type: Dict[str, List[str]] = {}

    try:
        for year in os.listdir(ticker_dir):
            year_dir = os.path.join(ticker_dir, year)
            if not os.path.isdir(year_dir) or year in ("unknown_year", ""):
                continue
            for doc_type in os.listdir(year_dir):
                if doc_type not in NON_SEC_FALLBACK_DOC_TYPES:
                    continue
                doc_dir = os.path.join(year_dir, doc_type)
                if not os.path.isdir(doc_dir):
                    continue
                # Verify at least one PDF exists
                has_pdf = any(
                    f.endswith(".pdf") or f.endswith(".html")
                    for f in os.listdir(doc_dir)
                )
                if not has_pdf:
                    continue
                all_years.add(year)
                if doc_type not in doc_types_seen:
                    doc_types_seen.append(doc_type)
                years_by_doc_type.setdefault(doc_type, [])
                if year not in years_by_doc_type[doc_type]:
                    years_by_doc_type[doc_type].append(year)
    except OSError:
        pass

    if not doc_types_seen:
        return None

    all_years_sorted = sorted(all_years, reverse=True)
    for dt in years_by_doc_type:
        years_by_doc_type[dt] = sorted(years_by_doc_type[dt], reverse=True)

    return {
        "doc_types": doc_types_seen,
        "all_years": all_years_sorted,
        "years_by_doc_type": years_by_doc_type,
        "source": "local",
        "scanned_at": time.time(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# AZURE BLOB scan  (fallback — used only when local files not yet downloaded)
# ─────────────────────────────────────────────────────────────────────────────

def _scan_ticker_azure(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Prefix-scan Azure Blob for annual/interim blobs for one ticker.
    Used only when the local filings_blob_cache has no data for this ticker.
    """
    try:
        from utils.azure_blob import get_container_client
        container = get_container_client()
        if not container:
            return None

        doc_types_seen: List[str] = []
        all_years: set = set()
        years_by_doc_type: Dict[str, List[str]] = {}
        newest_ts = None

        for blob in container.list_blobs(name_starts_with=f"{ticker}/"):
            parts = blob.name.split("/")
            if len(parts) < 3:
                continue
            year, doc_type = parts[1], parts[2]
            filename = parts[-1]
            if not (filename.endswith(".pdf") or filename.endswith(".html")):
                continue
            if doc_type not in NON_SEC_FALLBACK_DOC_TYPES:
                continue
            if year in ("unknown_year", ""):
                continue

            lm = getattr(blob, "last_modified", None)
            if lm is not None and (newest_ts is None or lm > newest_ts):
                newest_ts = lm

            all_years.add(year)
            if doc_type not in doc_types_seen:
                doc_types_seen.append(doc_type)
            years_by_doc_type.setdefault(doc_type, [])
            if year not in years_by_doc_type[doc_type]:
                years_by_doc_type[doc_type].append(year)

        if not doc_types_seen:
            return None

        all_years_sorted = sorted(all_years, reverse=True)
        for dt in years_by_doc_type:
            years_by_doc_type[dt] = sorted(years_by_doc_type[dt], reverse=True)

        return {
            "doc_types": doc_types_seen,
            "all_years": all_years_sorted,
            "years_by_doc_type": years_by_doc_type,
            "watermark": newest_ts.isoformat() if newest_ts else None,
            "source": "azure",
            "scanned_at": time.time(),
        }

    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="non_sec_blob_fallback",
                                 component="_scan_ticker_azure",
                                 operation=f"azure_scan_{ticker}")
        return None


def _scan_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Local-first scan: try filesystem, fall back to Azure if nothing found."""
    result = _scan_ticker_local(ticker)
    if result:
        return result
    return _scan_ticker_azure(ticker)


# ─────────────────────────────────────────────────────────────────────────────
# Public per-ticker API
# ─────────────────────────────────────────────────────────────────────────────

def get_non_sec_blob_data(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Return NON-SEC filing data for a ticker.

    Cache policy:
    - Local-sourced entries: 24-hour TTL (files only change when scanner runs)
    - Azure-sourced entries: 5-minute TTL
    - invalidate_non_sec_cache(ticker) sets scanned_at=0 to force refresh
    """
    cache = _load_cache()
    entry = cache.get(ticker)

    if entry:
        ttl = _LOCAL_CACHE_TTL if entry.get("source") == "local" else _AZURE_CACHE_TTL
        if (time.time() - entry.get("scanned_at", 0)) < ttl:
            return entry

    result = _scan_ticker(ticker)
    if result is None:
        return entry  # stale fallback — fail-open

    cache[ticker] = result
    _save_cache(cache)
    return result


def invalidate_non_sec_cache(ticker: str) -> None:
    """
    Expire the cache entry for a ticker (set scanned_at=0).
    Called by background_scanner after downloading new NON-SEC files.
    Next access will re-scan from local filesystem (fast).
    """
    global _MEM_CACHE
    try:
        cache = _load_cache()
        if ticker in cache:
            cache[ticker]["scanned_at"] = 0
            _save_cache(cache)
        _MEM_CACHE = None  # force re-read on next access
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Startup warm-up  (called once from main.py background thread)
# ─────────────────────────────────────────────────────────────────────────────

def warm_non_sec_cache_at_startup(tickers: List[str], max_workers: int = 32) -> Dict[str, Any]:
    """
    Scan filings_blob_cache/ in parallel for all NON-SEC tickers.
    Called from main.py background thread at server startup.

    - Skips tickers whose cache entry is still fresh (local TTL = 24h).
    - For tickers with stale/missing cache: does a fast local filesystem scan.
    - For tickers with NO local files at all: does NOT hit Azure (too slow at
      startup). Azure fallback happens lazily on first page load for that ticker.
    - Writes all results to disk cache in one batch.

    Returns: dict of ticker → data for all tickers that have filing files.
    """
    if not tickers:
        return {}

    cache = _load_cache()
    now = time.time()

    # Separate fresh (skip) vs stale/missing (re-scan)
    stale = []
    for t in tickers:
        entry = cache.get(t)
        if entry:
            ttl = _LOCAL_CACHE_TTL if entry.get("source") == "local" else _AZURE_CACHE_TTL
            if (now - entry.get("scanned_at", 0)) < ttl:
                continue  # still fresh
        stale.append(t)

    if stale:
        def _scan_one(ticker: str):
            return ticker, _scan_ticker_local(ticker)

        updated = {}
        _EMPTY_MARKER_TTL = 3600  # re-check no-data tickers hourly

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_scan_one, t): t for t in stale}
            for future in as_completed(futures):
                try:
                    ticker, result = future.result(timeout=10)
                    if result is not None:
                        updated[ticker] = result
                        cache[ticker] = result
                    else:
                        # No local files — write empty marker so scan_all won't
                        # fall through to Azure on every page load for this ticker
                        marker = {
                            "doc_types": [], "all_years": [], "years_by_doc_type": {},
                            "source": "local", "scanned_at": time.time(),
                        }
                        cache[ticker] = marker
                        updated[ticker] = marker
                except Exception:
                    pass

        if updated:
            _save_cache(cache)

    # Return everything that has data
    return {t: cache[t] for t in tickers if cache.get(t) and cache[t].get("doc_types")}


# ─────────────────────────────────────────────────────────────────────────────
# Parallel scan used by _load_companies_from_db()
# ─────────────────────────────────────────────────────────────────────────────

def scan_all_non_sec_tickers_parallel(
    tickers: List[str],
    max_workers: int = 32,
) -> Dict[str, Dict[str, Any]]:
    """
    Return filing data for all given tickers, using cache + local scan.

    Reads from cache first (0ms for warm cache).
    For stale/missing entries: parallel local filesystem scan (all 36 tickers
    in <500ms, vs 15s for Azure).
    For tickers with no local files: falls back to Azure scan.

    Called by _load_companies_from_db() which is wrapped in @st.cache_data(ttl=300).
    """
    if not tickers:
        return {}

    cache = _load_cache()
    now = time.time()

    fresh: Dict[str, Any] = {}
    to_scan: List[str] = []

    for t in tickers:
        entry = cache.get(t)
        if entry:
            ttl = _LOCAL_CACHE_TTL if entry.get("source") == "local" else _AZURE_CACHE_TTL
            if (now - entry.get("scanned_at", 0)) < ttl:
                if entry.get("doc_types"):
                    fresh[t] = entry
                continue
        to_scan.append(t)

    if to_scan:
        def _scan_one_local(ticker: str):
            return ticker, _scan_ticker_local(ticker)

        azure_fallback: List[str] = []
        updated: Dict[str, Any] = {}

        # Phase 1: fast local filesystem scan
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_scan_one_local, t): t for t in to_scan}
            for future in as_completed(futures):
                try:
                    ticker, result = future.result(timeout=10)
                    if result:
                        updated[ticker] = result
                        cache[ticker] = result
                        fresh[ticker] = result
                    else:
                        azure_fallback.append(ticker)
                except Exception:
                    pass

        # Phase 2: Azure fallback only for tickers with no local files
        if azure_fallback:
            def _scan_one_azure(ticker: str):
                return ticker, _scan_ticker_azure(ticker)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_scan_one_azure, t): t for t in azure_fallback}
                for future in as_completed(futures):
                    try:
                        ticker, result = future.result(timeout=30)
                        if result:
                            updated[ticker] = result
                            cache[ticker] = result
                            fresh[ticker] = result
                    except Exception:
                        pass

        if updated:
            _save_cache(cache)

    return fresh


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_non_sec_source_company(ticker: str) -> bool:
    """Return True if coreiq_companies.source != 'SEC' for this ticker."""
    try:
        from core.database import db_manager
        rows = db_manager.execute_query_readonly(
            "SELECT source FROM coreiq_companies WHERE ticker = :ticker LIMIT 1",
            {"ticker": ticker},
        )
        if rows:
            return rows[0]["source"] != "SEC"
        return False
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="non_sec_blob_fallback",
                                 component="is_non_sec_source_company",
                                 operation="DB_CHECK")
        return False


def get_non_sec_tickers_from_db() -> List[Tuple[str, str]]:
    """
    Return [(ticker, display_name), ...] for all NON-SEC companies in coreiq_companies.
    """
    try:
        from core.database import db_manager
        rows = db_manager.execute_query_readonly(
            "SELECT ticker, name_coresight, name FROM coreiq_companies"
            " WHERE source != :s ORDER BY ticker",
            {"s": "SEC"},
        )
        return [
            (r["ticker"], r.get("name_coresight") or r.get("name") or r["ticker"])
            for r in rows if r.get("ticker")
        ]
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="non_sec_blob_fallback",
                                 component="get_non_sec_tickers_from_db",
                                 operation="DB_FETCH")
        return []
