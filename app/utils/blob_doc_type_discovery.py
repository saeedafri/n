"""
Blob Doc Type Discovery — detects new/unknown document types in Azure Blob.

Purpose:
  Employees may add new doc_types to Azure Blob (e.g., 'half-yearly') before the
  manager adds them to coreiq_filing_metrics_v2. This utility discovers those gaps.

How it works:
  1. Lists all blobs in Azure and extracts unique doc_type folder names.
  2. Compares against KNOWN_DOC_TYPES (what the app currently supports).
  3. Checks DB table coreiq_filing_metrics_v2 for any doc_types found in blob
     but not in the known list.
  4. Returns a report: unknown doc_types + which companies have them + file counts.

Performance:
  - Parallel workers list blobs per company prefix using ThreadPoolExecutor.
  - Results are cached in data/blob_doc_type_scan_state.json.
  - Re-scan only triggers when the newest blob's last_modified > watermark stored
    in the state file (i.e., only when new files were actually added to Azure).

Usage:
    from utils.blob_doc_type_discovery import discover_unknown_doc_types, get_last_scan_result

    # Check (re-scans only if new blobs added since last scan)
    result = discover_unknown_doc_types()
    print(result["unknown"])   # {doc_type: {"companies": [...], "count": N}}

    # Force a fresh scan regardless of watermark
    result = discover_unknown_doc_types(force=True)
"""

import os
import json
import time
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from utils.server_logger import log_error, log_structured_error
except ImportError:
    import logging
    log_error = logging.error
    def log_structured_error(exc, **kw):
        logging.error(f"[blob_doc_type_discovery] {exc}")

# ─── paths ────────────────────────────────────────────────────────────────────
# Persistent /home root on Azure (survives deploys), <repo>/data locally.
from utils.filings_paths import filings_data_root
_DATA_DIR = filings_data_root()
_STATE_FILE = os.path.join(_DATA_DIR, "blob_doc_type_scan_state.json")

# ─── doc types the application currently knows about ──────────────────────────
# Keep in sync with background_scanner.VALID_DOC_TYPES
KNOWN_DOC_TYPES: Set[str] = {
    # SEC filings
    "10-K", "10-Q-Q1", "10-Q-Q2", "10-Q-Q3",
    "8-K", "20-F", "6-K",
    "DEF14A", "DEFA14A", "DEF 14A",
    # NON-SEC
    "annual-report",
    "interim-report-Q1", "interim-report-Q2", "interim-report-Q3",
    "interim-report-Q4", "interim-report-Q5",
    "half-yearly",
    # Transcripts
    "transcript-Q1", "transcript-Q2", "transcript-Q3", "transcript-Q4",
    # Internal sub-folder names to ignore entirely
    "archive", "versions",
}

# ─── explicitly invalid / junk doc types — never reported as "unknown" ─────────
# These exist as Azure Blob folder names but are not valid document types.
# Adding them here suppresses them from the discovery report permanently.
IGNORED_DOC_TYPES: Set[str] = {
    # Old/invalid quarterly naming formats (correct format is 10-Q-Q1/Q2/Q3)
    "10-Q-Q4", "10-Q-1", "10-Q-2", "10-Q-3",
    # Non-financial document types (not part of the filings system)
    "esg-sustainability", "esg-sustainability-Q4",
    "presentation", "presentation-Q1", "presentation-Q2", "presentation-Q3", "presentation-Q4",
    "earnings-release", "earnings-release-Q2", "earnings-release-Q4",
    "agm-notice",
    "governance-remuneration", "governance-remuneration-Q4",
    "regulatory-filing",
    "transcript-unknown",
    "transcript",
    "interim-report-unknown",
    "other", "other-Q2", "other-Q3", "other-Q4",
}

# Blob path components that are not doc_type folders
_SKIP_SEGMENTS: Set[str] = {"archive", "versions"}

# Max parallel workers for listing blobs per company
_WORKERS = 20

_scan_lock = threading.Lock()


# ─── Azure helpers ─────────────────────────────────────────────────────────────

def _get_azure_config():
    acct = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "csmarketdata").strip()
    key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "").strip()
    container = (os.getenv("AZURE_BLOB_CONTAINER") or "azure-storage-test").strip()
    prefix = (os.getenv("AZURE_BLOB_PREFIX") or "").strip("/")
    return acct, key, container, prefix


def _get_container_client():
    try:
        from azure.storage.blob import BlobServiceClient
        acct, key, container, _ = _get_azure_config()
        if not key:
            return None
        svc = BlobServiceClient(
            account_url=f"https://{acct}.blob.core.windows.net",
            credential=key,
            connection_pool_size=50,
        )
        return svc.get_container_client(container)
    except Exception as e:
        log_structured_error(e, component="blob_doc_type_discovery", operation="get_container_client")
        return None


def _strip_prefix(blob_name: str, prefix: str) -> str:
    bn = (blob_name or "").lstrip("/").replace("\\", "/")
    if not prefix:
        return bn
    pfx = prefix.strip("/") + "/"
    return bn[len(pfx):] if bn.startswith(pfx) else bn


# ─── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> Dict[str, Any]:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log_structured_error(e, component="blob_doc_type_discovery", operation="save_state")


def get_last_scan_result() -> Optional[Dict[str, Any]]:
    """Return the cached result from the last scan, or None if never scanned."""
    state = _load_state()
    return state.get("last_result")


# ─── Core scan ─────────────────────────────────────────────────────────────────

def _list_company_blobs(container_client, company_prefix: str, prefix: str) -> List[Dict]:
    """List all blobs for a single company prefix. Returns list of {name, last_modified}."""
    try:
        blobs = []
        for b in container_client.list_blobs(name_starts_with=company_prefix):
            name = getattr(b, "name", "")
            lm = getattr(b, "last_modified", None)
            blobs.append({"name": name, "last_modified": lm})
        return blobs
    except Exception as e:
        log_structured_error(e, component="blob_doc_type_discovery",
                             operation=f"list_blobs:{company_prefix}")
        return []


def _extract_doc_type(rel_path: str) -> Optional[str]:
    """
    Extract doc_type from a relative blob path.
    Supported structures:
      4-level: {ticker}/{year}/{doc_type}/file
      5-level: {ticker}/{year}/{doc_type}/filing_N/file   (multi-filing)
      6-level: {ticker}/{year}/{doc_type}/filing_N/archive/file (skip)
    Returns None for ignored/invalid doc types.
    """
    parts = [p for p in rel_path.split("/") if p]
    if len(parts) < 4:
        return None
    doc_type = parts[2]
    if doc_type in _SKIP_SEGMENTS:
        return None
    if doc_type in IGNORED_DOC_TYPES:
        return None
    # Skip filing_N sub-folder names that bubble up to level 2
    if doc_type.startswith("filing_") and doc_type[8:].isdigit():
        return None
    return doc_type


def _discover_all_companies(container_client, prefix: str) -> List[str]:
    """List unique top-level company ticker prefixes from blob names."""
    companies: Set[str] = set()
    try:
        # Use delimiter to get virtual directories at top level
        from azure.storage.blob import BlobPrefix
        search_prefix = (prefix + "/") if prefix else ""
        for item in container_client.walk_blobs(name_starts_with=search_prefix, delimiter="/"):
            name = getattr(item, "name", "")
            # Remove leading prefix and trailing /
            rel = _strip_prefix(name, prefix).strip("/")
            if rel and not rel.startswith("_"):
                companies.add(rel)
    except Exception as e:
        log_structured_error(e, component="blob_doc_type_discovery",
                             operation="discover_companies")
    return sorted(companies)


def _check_db_for_doc_types(unknown_types: Set[str]) -> Dict[str, bool]:
    """Check which of the unknown doc_types exist in coreiq_filing_metrics_v2."""
    result = {dt: False for dt in unknown_types}
    if not unknown_types:
        return result
    try:
        from core.database import db_manager
        placeholders = ", ".join([f":dt{i}" for i in range(len(unknown_types))])
        params = {f"dt{i}": dt for i, dt in enumerate(unknown_types)}
        rows = db_manager.execute_query_readonly(
            f"SELECT DISTINCT doc_type FROM coreiq_filing_metrics_v2 WHERE doc_type IN ({placeholders})",
            params
        )
        for row in rows:
            dt = row["doc_type"]
            if dt in result:
                result[dt] = True
    except Exception as e:
        log_structured_error(e, component="blob_doc_type_discovery",
                             operation="check_db_for_doc_types")
    return result


def _needs_rescan(state: Dict[str, Any]) -> bool:
    """
    Returns True if a rescan is needed.

    We store the newest blob last_modified seen in the last scan (watermark).
    On subsequent calls, we do a cheap metadata-only scan of a small prefix sample
    to detect if any blob has a newer last_modified. If yes → rescan needed.

    Note: Full blob listing is expensive. The watermark check is fast because we
    just need to find ONE blob newer than the watermark to trigger a rescan.
    """
    watermark_str = state.get("newest_blob_ts")
    if not watermark_str:
        return True  # Never scanned

    try:
        watermark = datetime.fromisoformat(watermark_str)
        if watermark.tzinfo is None:
            watermark = watermark.replace(tzinfo=timezone.utc)

        container = _get_container_client()
        if not container:
            return False

        _, _, _, prefix = _get_azure_config()
        name_starts_with = (prefix.strip("/") + "/") if prefix else ""

        # Sample the first 2000 blobs — if any is newer, rescan
        count = 0
        for b in container.list_blobs(name_starts_with=name_starts_with):
            lm = getattr(b, "last_modified", None)
            if lm:
                if lm.tzinfo is None:
                    lm = lm.replace(tzinfo=timezone.utc)
                if lm > watermark:
                    return True
            count += 1
            if count >= 2000:
                break

        return False
    except Exception as e:
        log_structured_error(e, component="blob_doc_type_discovery",
                             operation="needs_rescan")
        return True  # If we can't check, assume rescan needed


def _run_full_scan() -> Dict[str, Any]:
    """
    Full scan: list all blobs, extract doc_types, compare with KNOWN_DOC_TYPES.
    Returns a result dict with unknown types, company mapping, and watermark.
    """
    t0 = time.time()
    container = _get_container_client()
    if not container:
        return {"error": "Azure connection failed", "unknown": {}}

    _, _, _, prefix = _get_azure_config()

    # Step 1: Discover company prefixes (fast walk with delimiter)
    companies = _discover_all_companies(container, prefix)

    # Step 2: List blobs for all companies in parallel
    doc_type_companies: Dict[str, Set[str]] = {}
    doc_type_counts: Dict[str, int] = {}
    newest_ts: Optional[datetime] = None

    def _process_company(ticker: str) -> Dict:
        company_prefix = (f"{prefix}/{ticker}/" if prefix else f"{ticker}/")
        blobs = _list_company_blobs(container, company_prefix, prefix)
        local_doc_types: Dict[str, int] = {}
        local_newest: Optional[datetime] = None
        for b in blobs:
            name = b["name"]
            lm = b["last_modified"]
            if lm:
                if lm.tzinfo is None:
                    lm = lm.replace(tzinfo=timezone.utc)
                if local_newest is None or lm > local_newest:
                    local_newest = lm

            rel = _strip_prefix(name, prefix)
            dt = _extract_doc_type(rel)
            if dt:
                local_doc_types[dt] = local_doc_types.get(dt, 0) + 1
        return {"ticker": ticker, "doc_types": local_doc_types, "newest_ts": local_newest}

    with ThreadPoolExecutor(max_workers=_WORKERS) as executor:
        futures = {executor.submit(_process_company, ticker): ticker for ticker in companies}
        for future in as_completed(futures):
            try:
                res = future.result(timeout=60)
                ticker = res["ticker"]
                for dt, cnt in res["doc_types"].items():
                    doc_type_companies.setdefault(dt, set()).add(ticker)
                    doc_type_counts[dt] = doc_type_counts.get(dt, 0) + cnt
                local_ts = res.get("newest_ts")
                if local_ts:
                    if newest_ts is None or local_ts > newest_ts:
                        newest_ts = local_ts
            except Exception as e:
                log_structured_error(e, component="blob_doc_type_discovery",
                                     operation="process_company_future")

    # Step 3: Find unknown doc_types (exclude known-invalid/junk entries)
    all_found = set(doc_type_companies.keys())
    unknown_types = all_found - KNOWN_DOC_TYPES - IGNORED_DOC_TYPES

    # Step 4: Check DB for unknown types
    in_db = _check_db_for_doc_types(unknown_types)

    unknown_report: Dict[str, Any] = {}
    for dt in sorted(unknown_types):
        unknown_report[dt] = {
            "companies": sorted(doc_type_companies.get(dt, set())),
            "blob_count": doc_type_counts.get(dt, 0),
            "in_db": in_db.get(dt, False),
        }

    duration = round(time.time() - t0, 1)
    result = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "companies_scanned": len(companies),
        "all_doc_types": sorted(all_found),
        "unknown": unknown_report,
        "scan_duration_s": duration,
    }
    return result, newest_ts


def discover_unknown_doc_types(force: bool = False) -> Dict[str, Any]:
    """
    Discover doc_types in Azure Blob that are not in KNOWN_DOC_TYPES.

    Uses a watermark (newest blob last_modified from last scan) to avoid
    full rescans when no new blobs were added.

    Args:
        force: If True, run a full scan regardless of watermark.

    Returns:
        Dict with keys:
          - "unknown": {doc_type: {"companies": [...], "blob_count": N, "in_db": bool}}
          - "scanned_at": ISO timestamp
          - "companies_scanned": int
          - "scan_duration_s": float (0 if served from cache)
          - "from_cache": bool
    """
    with _scan_lock:
        state = _load_state()

        if not force and not _needs_rescan(state):
            last = state.get("last_result", {})
            last["from_cache"] = True
            last["scan_duration_s"] = 0
            return last

        # Full scan
        try:
            result, newest_ts = _run_full_scan()
        except Exception as e:
            log_structured_error(e, component="blob_doc_type_discovery",
                                 operation="discover_unknown_doc_types")
            return {"error": str(e), "unknown": {}, "from_cache": False}

        # Save state
        state["last_result"] = result
        state["newest_blob_ts"] = newest_ts.isoformat() if newest_ts else None
        _save_state(state)

        result["from_cache"] = False
        return result


def format_discovery_report(result: Dict[str, Any]) -> str:
    """Format discovery result as a human-readable string for logging or display."""
    lines = []
    lines.append(f"=== Blob Doc Type Discovery Report ===")
    lines.append(f"Scanned at:   {result.get('scanned_at', 'unknown')}")
    lines.append(f"Companies:    {result.get('companies_scanned', '?')}")
    lines.append(f"Duration:     {result.get('scan_duration_s', 0):.1f}s")
    lines.append(f"From cache:   {result.get('from_cache', False)}")

    unknown = result.get("unknown", {})
    if not unknown:
        lines.append("\n✅ No unknown doc types found.")
    else:
        lines.append(f"\n⚠️  {len(unknown)} unknown doc type(s) found:")
        for dt, info in sorted(unknown.items()):
            in_db = "✅ in DB" if info.get("in_db") else "❌ NOT in DB"
            companies = ", ".join(info.get("companies", [])[:10])
            if len(info.get("companies", [])) > 10:
                companies += f" ... (+{len(info['companies']) - 10} more)"
            lines.append(
                f"  {dt!r:30s}  blobs={info['blob_count']:5d}  {in_db}  "
                f"companies: [{companies}]"
            )

    return "\n".join(lines)
