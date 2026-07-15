"""
Background Filings Scanner - INTELLIGENT Smart Pre-fetch (ALL Available Years).
=============================================================================

PRODUCTION-GRADE Features:
- Intelligent cache detection - Skip if already cached
- Check before scan - No redundant operations
- Detailed audit logs for every decision
- Resource-efficient for 50-user environment

Logic:
1. Check if sufficient files already cached
2. If YES → Skip scan, log reason, exit fast
3. If NO → Scan metadata + Download files for ALL available years
4. Never waste resources on redundant work
5. Years are discovered dynamically from blob paths; falls back to last 5 years

Usage:
    from utils.background_scanner import get_background_scanner
    scanner = get_background_scanner()
    scanner.start()  # Intelligent - skips if cached

Author: Coresight Research
"""
import os
import shutil
import time
import threading
from datetime import datetime
from typing import Optional, Dict, List, Any, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import glob

# Import server logger
try:
    from utils.server_logger import log_error, log_info, log_exception, log_structured_error
except ImportError:
    import logging
    log_error = logging.error
    log_info = logging.info
    log_exception = logging.exception
    def log_structured_error(exc, page="", component="", operation=""):
        logging.error(f"[{component}] {operation}: {exc}")

# Import cache
from utils.filings_cache import get_filings_cache, generate_etag_from_blobs

# Pre-import DB dependencies
try:
    from data.repository import FilingMetricRepository as _FilingMetricRepository
    from core.database import db_manager as _db_manager
    _DB_AVAILABLE = True
except Exception:
    _FilingMetricRepository = None
    _db_manager = None
    _DB_AVAILABLE = False

# ============================================================================
# CONFIGURATION
# ============================================================================
PREFETCH_YEARS = 2

# File cache directory
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
FILINGS_BLOB_CACHE_DIR     = os.path.join(_DATA_DIR, "filings_blob_cache")
FILINGS_METADATA_CACHE_DIR = os.path.join(_DATA_DIR, "filings_metadata_cache")
os.makedirs(FILINGS_BLOB_CACHE_DIR, exist_ok=True)
os.makedirs(FILINGS_METADATA_CACHE_DIR, exist_ok=True)

# ONE-TIME CACHE PURGE: Marker file written after purge completes.
# If this file exists → purge already ran on this machine → never runs again.
# Bump the version string (e.g. "v2") if a future Azure refresh requires
# a new one-time purge cycle.
_CACHE_PURGE_MARKER = os.path.join(_DATA_DIR, ".cache_purge_v1_done")
_CONTAINER_WATERMARK_FILE = os.path.join(_DATA_DIR, ".blob_container_last_modified")

# INTELLIGENCE: Minimum files to consider cache as "valid"
# If we have more than this, skip scan
MIN_CACHED_FILES_THRESHOLD = 10000  # At least 100 files cached

# INTELLIGENCE: Cache validity duration (hours)
CACHE_VALIDITY_HOURS = 24000000000

# VALID DOC TYPES - Only these are allowed in the system
# SEC filings: 10-K, 10-Q-Q1/2/3, 8-K, 20-F, 6-K
# NON-SEC filings: annual-report, interim-report-Q1/2/3/4/5
VALID_DOC_TYPES = {
    # SEC filings
    '10-K', '10-Q-Q1', '10-Q-Q2', '10-Q-Q3',
    '8-K', '20-F', '6-K',
    'DEF14A', 'DEFA14A', 'DEF 14A',
    # NON-SEC filings (CAPITAL Q)
    'annual-report',
    'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3',
    'interim-report-Q4', 'interim-report-Q5',
    'half-yearly',
    # Transcripts (PDF only: transcript-Q1/transcript.pdf)
    'transcript-Q1', 'transcript-Q2', 'transcript-Q3', 'transcript-Q4',
}

# Doc types that contain multiple sub-filings (e.g., 8-K/filing_1/, 8-K/filing_2/)
MULTI_FILING_DOC_TYPES = {'8-K', '6-K', 'DEF14A', 'DEFA14A', 'DEF 14A'}

# Sub-folders to skip inside multi-filing doc types
SKIP_SUBFOLDERS = {'versions'}


def _extract_year_and_doctype(parts: list) -> tuple:
    """
    Extract (year, doc_type) from blob path parts, handling both structures:
      4-level: [TICKER, YEAR, DOC_TYPE, file]         → parts[-3]=YEAR, parts[-2]=DOC_TYPE
      5-level: [TICKER, YEAR, 8-K, filing_N, file]    → parts[-4]=YEAR, parts[-3]=DOC_TYPE

    Returns (year, doc_type) or (None, None) if path is not recognized.
    """
    if len(parts) < 4:
        return None, None

    # Try standard 4-level first
    doc_type = parts[-2]
    if doc_type in VALID_DOC_TYPES:
        year = parts[-3]
        return (year, doc_type) if (year and year.isdigit() and len(year) == 4) else (None, None)

    # Try 5-level (multi-filing): parts[-3] is the doc type, parts[-2] is sub-folder
    if len(parts) >= 5:
        doc_type = parts[-3]
        if doc_type in MULTI_FILING_DOC_TYPES:
            sub_folder = parts[-2]
            # Skip 'versions' and other non-filing sub-folders
            if sub_folder in SKIP_SUBFOLDERS:
                return None, None
            year = parts[-4]
            return (year, doc_type) if (year and year.isdigit() and len(year) == 4) else (None, None)

    return None, None


def _is_valid_doc_type(doc_type_dir: str) -> bool:
    """Check if doc_type is in the valid list."""
    try:
        if not doc_type_dir:
            return False
        return doc_type_dir in VALID_DOC_TYPES
    except Exception as e:
        log_structured_error(e, page="", component="_is_valid_doc_type", operation="check valid doc type")
        return False


def _get_azure_config() -> tuple:
    """Get Azure configuration from environment."""
    try:
        acct = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "csmarketdata").strip()
        key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "").strip()
        container_name = (os.getenv("AZURE_BLOB_CONTAINER") or "azure-storage-test").strip()
        prefix = (os.getenv("AZURE_BLOB_PREFIX") or "").strip("/")
        return acct, key, container_name, prefix
    except Exception as e:
        log_structured_error(e, page="", component="_get_azure_config", operation="get azure config")
        return "", "", "", ""


def _blob_prefix_path(*parts: str) -> str:
    """Join path parts with configured prefix."""
    try:
        _, _, _, prefix = _get_azure_config()
        clean_parts = [p.strip("/").replace("\\", "/") for p in parts if p and p.strip("/")]
        if prefix:
            return "/".join([prefix] + clean_parts)
        return "/".join(clean_parts)
    except Exception as e:
        log_structured_error(e, page="", component="_blob_prefix_path", operation="build blob prefix path")
        return ""


def _strip_prefix(blob_name: str) -> str:
    """Return blob_name relative to prefix."""
    try:
        _, _, _, prefix = _get_azure_config()
        bn = (blob_name or "").lstrip("/").replace("\\", "/")
        if not prefix:
            return bn
        pfx = prefix.strip("/") + "/"
        if bn.startswith(pfx):
            return bn[len(pfx):]
        return bn
    except Exception as e:
        log_structured_error(e, page="", component="_strip_prefix", operation="strip blob prefix")
        return blob_name or ""


def _local_cache_path_for_blob(blob_name: str) -> str:
    """Convert blob name to local cache path."""
    try:
        rel = _strip_prefix(blob_name)
        rel = rel.replace("\\", "/").lstrip("/")
        return os.path.join(FILINGS_BLOB_CACHE_DIR, rel.replace("/", os.sep))
    except Exception as e:
        log_structured_error(e, page="", component="_local_cache_path_for_blob", operation="resolve local cache path")
        return ""


def _get_container_last_modified_ts() -> Optional[float]:
    """Get Azure container last_modified timestamp (epoch seconds)."""
    try:
        container = _get_container_client()
        if not container:
            return None
        props = container.get_container_properties()
        lm = getattr(props, 'last_modified', None)
        if not lm:
            return None
        return float(lm.timestamp())
    except Exception as e:
        log_structured_error(e, page="", component="_get_container_last_modified_ts", operation="get container last modified")
        return None


def _read_container_watermark() -> Optional[float]:
    """Read persisted container watermark timestamp from disk."""
    try:
        if not os.path.exists(_CONTAINER_WATERMARK_FILE):
            return None
        with open(_CONTAINER_WATERMARK_FILE, "r", encoding="utf-8") as f:
            raw = (f.read() or "").strip()
        return float(raw) if raw else None
    except Exception as e:
        log_structured_error(e, page="", component="_read_container_watermark", operation="read container watermark")
        return None


def _write_container_watermark(ts: Optional[float]) -> None:
    """Persist container watermark timestamp to disk."""
    if ts is None:
        return
    try:
        with open(_CONTAINER_WATERMARK_FILE, "w", encoding="utf-8") as f:
            f.write(str(ts))
    except Exception as e:
        log_structured_error(e, page="", component="_write_container_watermark", operation="write container watermark")


# Try to import Azure SDK
try:
    from azure.storage.blob import ContainerClient
    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False


def _get_container_client():
    """Get Azure container client with optimized settings."""
    try:
        if not AZURE_AVAILABLE:
            return None

        acct, key, container_name, _ = _get_azure_config()
        if not key:
            return None

        from azure.storage.blob import BlobServiceClient
        account_url = f"https://{acct}.blob.core.windows.net"

        return BlobServiceClient(
            account_url=account_url,
            credential=key,
            connection_pool_size=50,
            max_single_get_size=64 * 1024 * 1024,
            max_chunk_get_size=8 * 1024 * 1024,
        ).get_container_client(container_name)
    except Exception as e:
        log_structured_error(e, page="", component="_get_container_client", operation="get azure container client")
        return None


def _get_current_year() -> int:
    """Get current year."""
    try:
        return datetime.now().year
    except Exception as e:
        log_structured_error(e, page="", component="_get_current_year", operation="get current year")
        return 2024


def _get_prefetch_years() -> Set[str]:
    """Fallback: get set of years to pre-fetch (last N years)."""
    try:
        current_year = _get_current_year()
        return {str(y) for y in range(current_year - PREFETCH_YEARS + 1, current_year + 1)}
    except Exception as e:
        log_structured_error(e, page="", component="_get_prefetch_years", operation="get prefetch years")
        return set()


def _discover_years_from_blobs(all_blobs_by_company: Dict[str, list]) -> Set[str]:
    """
    Return only the last PREFETCH_YEARS years from available blob paths.
    Older files are downloaded on-demand from Azure when a user opens them.
    Falls back to _get_prefetch_years() if no years found.
    """
    try:
        discovered = set()
        for _ticker, blobs in all_blobs_by_company.items():
            for blob in blobs:
                blob_name = getattr(blob, 'name', '')
                if not blob_name:
                    continue
                parts = [p for p in blob_name.split('/') if p]
                year, _ = _extract_year_and_doctype(parts)
                if year:
                    discovered.add(year)
        if discovered:
            # Keep only the most recent PREFETCH_YEARS years
            current_year = _get_current_year()
            cutoff = current_year - PREFETCH_YEARS
            return {y for y in discovered if int(y) > cutoff}
    except Exception as e:
        log_structured_error(e, page="", component="_discover_years_from_blobs", operation="discover years from blob paths")
    return _get_prefetch_years()


# ============================================================================
# ONE-TIME CACHE PURGE  (runs exactly once per client machine)
# ============================================================================
def _run_one_time_cache_purge() -> bool:
    """
    Delete all stale cached files from filings_blob_cache and
    filings_metadata_cache — runs EXACTLY ONCE per client machine.

    Protection mechanism: after purge, a marker file is written to
    data/.cache_purge_v1_done. Every subsequent app start finds this
    marker and skips instantly.  No UI, no spinner — fully silent.

    Returns True if purge ran this call, False if already done / skipped.
    """
    try:
        # Already done on this machine → skip instantly (normal path after first run)
        if os.path.exists(_CACHE_PURGE_MARKER):
            try:
                with open(_CACHE_PURGE_MARKER) as mf:
                    pass
            except Exception:
                pass
            return False

        purge_start = time.time()

        # ── Step 1: Measure what we are about to delete ───────────────────────
        blob_file_count = blob_dir_count = 0
        blob_size_bytes = 0
        if os.path.isdir(FILINGS_BLOB_CACHE_DIR):
            for root, dirs, files in os.walk(FILINGS_BLOB_CACHE_DIR):
                blob_dir_count  += len(dirs)
                blob_file_count += len(files)
                for f in files:
                    try:
                        blob_size_bytes += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        blob_size_mb = blob_size_bytes / (1024 * 1024)

        meta_files = []
        if os.path.isdir(FILINGS_METADATA_CACHE_DIR):
            meta_files = [f for f in os.listdir(FILINGS_METADATA_CACHE_DIR)
                          if f.endswith((".db", ".db-shm", ".db-wal"))]

        # ── Step 2: Delete blob cache contents ───────────────────────────────
        removed_items = 0
        failed_items  = 0
        if os.path.isdir(FILINGS_BLOB_CACHE_DIR):
            for entry in os.listdir(FILINGS_BLOB_CACHE_DIR):
                full = os.path.join(FILINGS_BLOB_CACHE_DIR, entry)
                try:
                    if os.path.isdir(full):
                        shutil.rmtree(full)
                    else:
                        os.remove(full)
                    removed_items += 1
                except Exception:
                    failed_items += 1

        # ── Step 3: Delete metadata SQLite files ─────────────────────────────
        removed_meta = 0
        failed_meta  = 0
        for fname in meta_files:
            try:
                os.remove(os.path.join(FILINGS_METADATA_CACHE_DIR, fname))
                removed_meta += 1
            except Exception:
                failed_meta += 1

        elapsed = time.time() - purge_start

        # ── Step 4: Write marker ──────────────────────────────────────────────
        marker_written = False
        try:
            with open(_CACHE_PURGE_MARKER, "w") as mf:
                mf.write(
                    f"purge_v1 completed at {datetime.now().isoformat()} | "
                    f"blob: {removed_items} items ({blob_size_mb:.1f} MB) removed | "
                    f"meta: {removed_meta} files removed | "
                    f"duration: {elapsed:.2f}s\n"
                )
            marker_written = True
        except Exception:
            pass

        return True
    except Exception as e:
        log_structured_error(e, page="", component="_run_one_time_cache_purge", operation="run one-time cache purge")
        return False


# ============================================================================
# INTELLIGENCE: Cache Audit Functions
# ============================================================================
def _audit_file_cache() -> Dict[str, Any]:
    """
    INTELLIGENT: Audit the file cache to determine if scan is needed.

    Returns:
        Dict with cache status, file counts, and recommendation
    """
    try:
        audit_start = time.time()

        # Check if directory exists and has content
        if not os.path.exists(FILINGS_BLOB_CACHE_DIR):
            return {
                'exists': False,
                'file_count': 0,
                'should_scan': True,
                'reason': 'Cache directory does not exist'
            }

        # Count HTML and PDF files in cache (support both SEC HTML and NON-SEC PDF)
        cached_files = []
        try:
            for root, dirs, files in os.walk(FILINGS_BLOB_CACHE_DIR):
                for file in files:
                    if file.endswith(('.html', '.htm', '.pdf')):
                        full_path = os.path.join(root, file)
                        cached_files.append(full_path)
        except Exception:
            pass

        file_count = len(cached_files)

        # Check recent files (last 24 hours)
        recent_files = 0
        now = time.time()
        for file_path in cached_files[:100]:  # Sample first 100 for speed
            try:
                mtime = os.path.getmtime(file_path)
                if (now - mtime) < (CACHE_VALIDITY_HOURS * 3600):
                    recent_files += 1
            except Exception:
                pass

        # Calculate total size
        total_size_bytes = 0
        for file_path in cached_files:
            try:
                total_size_bytes += os.path.getsize(file_path)
            except Exception:
                pass

        total_size_mb = total_size_bytes / (1024 * 1024)

        # Fast incremental gate: detect container updates without full blob scan.
        previous_watermark = _read_container_watermark()
        current_watermark = _get_container_last_modified_ts()
        has_new_blob_activity = (
            current_watermark is not None
            and (previous_watermark is None or current_watermark > previous_watermark)
        )

        # INTELLIGENCE: Decision making
        should_scan = False
        reason = ""

        if file_count < MIN_CACHED_FILES_THRESHOLD:
            should_scan = True
            reason = f"Insufficient files ({file_count} < {MIN_CACHED_FILES_THRESHOLD} threshold)"
        elif recent_files < 10:
            should_scan = True
            reason = f"Cache stale (only {recent_files} recent files)"
        elif has_new_blob_activity:
            should_scan = True
            reason = "Azure container changed since last completed scan"
        else:
            should_scan = False
            reason = f"Cache healthy ({file_count} files, {recent_files} recent)"

        audit_duration = time.time() - audit_start

        log_info(
            f"[BG_SCAN][AUDIT] file_count={file_count} recent_files={recent_files} "
            f"previous_watermark={previous_watermark} current_watermark={current_watermark} "
            f"has_new_blob_activity={has_new_blob_activity} "
            f"should_scan={should_scan} reason={reason!r} "
            f"audit_duration={audit_duration:.3f}s"
        )

        return {
            'exists': True,
            'file_count': file_count,
            'recent_files': recent_files,
            'total_size_mb': total_size_mb,
            'container_last_modified': current_watermark,
            'container_last_scan_watermark': previous_watermark,
            'has_new_blob_activity': has_new_blob_activity,
            'should_scan': should_scan,
            'reason': reason,
            'audit_duration': audit_duration
        }
    except Exception as e:
        log_structured_error(e, page="", component="_audit_file_cache", operation="audit file cache")
        return {
            'exists': False,
            'file_count': 0,
            'should_scan': True,
            'reason': f'Audit error: {e}'
        }


# ============================================================================
# TIMING TRACKER
# ============================================================================
class TimingTracker:
    """Track detailed timing for performance analysis."""

    def __init__(self):
        self._timings = {}
        self._lock = threading.Lock()

    def start(self, name: str):
        """Start timing a phase."""
        try:
            with self._lock:
                self._timings[name] = {
                    'start': time.time(),
                    'end': None,
                    'duration': None
                }
        except Exception as e:
            log_structured_error(e, page="", component="TimingTracker", operation="start timing phase")

    def end(self, name: str):
        """End timing a phase."""
        try:
            with self._lock:
                if name in self._timings:
                    self._timings[name]['end'] = time.time()
                    self._timings[name]['duration'] = (
                        self._timings[name]['end'] - self._timings[name]['start']
                    )
        except Exception as e:
            log_structured_error(e, page="", component="TimingTracker", operation="end timing phase")

    def get_report(self) -> Dict[str, Any]:
        """Get timing report."""
        try:
            with self._lock:
                return {
                    name: {
                        'duration': data.get('duration'),
                        'start': data.get('start'),
                        'end': data.get('end')
                    }
                    for name, data in self._timings.items()
                }
        except Exception as e:
            log_structured_error(e, page="", component="TimingTracker", operation="get timing report")
            return {}


# ============================================================================
# BACKGROUND SCANNER CLASS
# ============================================================================
class BackgroundFilingsScanner:
    """
    INTELLIGENT background scanner - Skips if already cached.
    """

    DEFAULT_METADATA_WORKERS = 4
    DEFAULT_DOWNLOAD_WORKERS = 8
    DEFAULT_BATCH_SIZE = 5

    def __init__(self,
                 max_metadata_workers: int = None,
                 max_download_workers: int = None,
                 batch_size: int = None):
        try:
            self.max_metadata_workers = max_metadata_workers or self.DEFAULT_METADATA_WORKERS
            self.max_download_workers = max_download_workers or self.DEFAULT_DOWNLOAD_WORKERS
            self.batch_size = batch_size or self.DEFAULT_BATCH_SIZE

            self._thread: Optional[threading.Thread] = None
            self._stop_event = threading.Event()
            self._pause_event = threading.Event()
            self._pause_event.set()

            self._cache = get_filings_cache()
            self._lock = threading.RLock()
            self._timing = TimingTracker()

            # Stats
            self._stats = {
                'started_at': None,
                'completed_at': None,
                'companies_scanned': 0,
                'companies_failed': 0,
                'blobs_found': 0,
                'errors': [],
                'files_total': 0,
                'files_downloaded': 0,
                'files_skipped': 0,
                'files_failed': 0,
                'bytes_downloaded': 0,
                'files_by_year': defaultdict(lambda: {'total': 0, 'downloaded': 0, 'skipped': 0, 'failed': 0}),
                # Intelligence stats
                'cache_audit_result': None,
                'scan_skipped': False,
                'skip_reason': None,
            }

            # Doc type mapping (SEC + NON-SEC with CAPITAL Q)
            self._doc_type_map = {
                "10K": "10-K", "10-K": "10-K",
                "10Q": "10-Q", "10-Q": "10-Q",
                "10-Q-Q1": "10-Q", "10-Q-Q2": "10-Q", "10-Q-Q3": "10-Q",
                "8K": "8-K", "8-K": "8-K",
                "DEF14A": "DEF 14A",
                "DEFA14A": "DEFA 14A",
                "DEF 14A": "DEF 14A",
                "S1": "S-1", "S-1": "S-1",
                "20F": "20-F", "20-F": "20-F",
                # NON-SEC types (CAPITAL Q)
                "annual-report": "Annual Report",
                "interim-report-Q1": "Q1 Interim",
                "interim-report-Q2": "Q2 Interim",
                "interim-report-Q3": "Q3 Interim",
                "interim-report-Q4": "Q4 Interim",
                "interim-report-Q5": "Q5 Interim",
                "half-yearly": "Half-Yearly",
                # Transcripts
                "transcript-Q1": "Q1 Transcript",
                "transcript-Q2": "Q2 Transcript",
                "transcript-Q3": "Q3 Transcript",
                "transcript-Q4": "Q4 Transcript",
            }
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="init scanner")

    def start(self, companies: List[str] = None, force_scan: bool = False) -> bool:
        """
        INTELLIGENT: Start background scan only if needed.

        Args:
            companies: List of tickers to scan
            force_scan: If True, skip cache check and scan anyway

        Returns:
            True if started successfully, False otherwise
        """
        try:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return True

                # INTELLIGENCE: Check cache before starting
                if not force_scan:
                    audit = _audit_file_cache()
                    self._stats['cache_audit_result'] = audit

                    if not audit['should_scan']:
                        # Cache is healthy, skip scan
                        self._stats['scan_skipped'] = True
                        self._stats['skip_reason'] = audit['reason']
                        log_info(f"[BG_SCAN][SKIP] Scan skipped — {audit['reason']}")
                        return True

                self._stop_event.clear()
                self._pause_event.set()

                # Reset stats
                self._stats = {
                    'started_at': datetime.now(),
                    'completed_at': None,
                    'companies_scanned': 0,
                    'companies_failed': 0,
                    'blobs_found': 0,
                    'errors': [],
                    'files_total': 0,
                    'files_downloaded': 0,
                    'files_skipped': 0,
                    'files_failed': 0,
                    'bytes_downloaded': 0,
                    'files_by_year': defaultdict(lambda: {'total': 0, 'downloaded': 0, 'skipped': 0, 'failed': 0}),
                    'cache_audit_result': self._stats.get('cache_audit_result'),
                    'scan_skipped': False,
                    'skip_reason': None,
                }

                self._thread = threading.Thread(
                    target=self._scan_worker,
                    args=(companies,),
                    daemon=True,
                    name="filings-bg-scanner"
                )
                self._thread.start()

                return True
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="start scanner")
            return False

    def stop(self, wait: bool = False, timeout: float = 10.0) -> bool:
        """Signal scanner to stop."""
        try:
            self._stop_event.set()
            if wait and self._thread and self._thread.is_alive():
                self._thread.join(timeout=timeout)
                return not self._thread.is_alive()
            return True
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="stop scanner")
            return False

    def pause(self):
        """Pause scanning."""
        try:
            self._pause_event.clear()
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="pause scanner")

    def resume(self):
        """Resume scanning."""
        try:
            self._pause_event.set()
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="resume scanner")

    def is_running(self) -> bool:
        """Check if scanner is running."""
        try:
            return self._thread is not None and self._thread.is_alive()
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="check is running")
            return False

    def is_paused(self) -> bool:
        """Check if scanner is paused."""
        try:
            return not self._pause_event.is_set()
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="check is paused")
            return False

    def get_progress(self) -> Dict[str, Any]:
        """Get current scan progress."""
        try:
            # If scan was skipped, return that status
            if self._stats.get('scan_skipped'):
                return {
                    'is_running': False,
                    'is_paused': False,
                    'scan_skipped': True,
                    'skip_reason': self._stats.get('skip_reason'),
                    'cached_files': self._stats.get('cache_audit_result', {}).get('file_count', 0),
                    'cached_size_mb': self._stats.get('cache_audit_result', {}).get('total_size_mb', 0),
                }

            status = self._cache.get_scan_status()

            completed = len(status.get('completed_companies', []))
            total = status.get('total_companies', 0)
            percent = (completed / total * 100) if total > 0 else 0

            files_total = self._stats['files_total']
            files_done = self._stats['files_downloaded'] + self._stats['files_skipped']

            eta = None
            if completed > 0 and self._stats['started_at']:
                elapsed = (datetime.now() - self._stats['started_at']).total_seconds()
                rate = completed / elapsed
                remaining = total - completed
                eta = int(remaining / rate) if rate > 0 else None

            return {
                'is_running': self.is_running(),
                'is_paused': self.is_paused(),
                'completed': completed,
                'total': total,
                'percent': round(percent, 1),
                'files_total': files_total,
                'files_downloaded': self._stats['files_downloaded'],
                'files_skipped': self._stats['files_skipped'],
                'files_failed': self._stats['files_failed'],
                'bytes_downloaded': self._stats['bytes_downloaded'],
                'bytes_downloaded_mb': round(self._stats['bytes_downloaded'] / (1024*1024), 2),
                'eta_seconds': eta,
                'prefetch_years': sorted(_get_prefetch_years()),
                'error': status.get('error')
            }
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="get scan progress")
            return {}

    def get_stats(self) -> Dict[str, Any]:
        """Get scanner statistics."""
        try:
            with self._lock:
                stats = dict(self._stats)
                stats['files_by_year'] = dict(stats['files_by_year'])
                return stats
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="get scanner stats")
            return {}

    def get_timing_report(self) -> Dict[str, Any]:
        """Get detailed timing report."""
        try:
            return self._timing.get_report()
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="get timing report")
            return {}

    # ========================================================================
    # MAIN WORKER
    # ========================================================================
    def _scan_worker(self, companies: List[str] = None):
        """Main worker - Phase 1: Metadata, Phase 2: File downloads."""
        overall_start = time.time()
        self._stats['started_at'] = datetime.now()

        try:
            # Get companies
            if companies is None:
                companies = self._load_companies_from_db()

            total = len(companies)
            self._cache.update_scan_status(
                in_progress=True,
                total_companies=total,
                completed_companies=[],
                last_scan=datetime.now().isoformat(),
                error=None
            )

            # =====================================================================
            # PHASE 1: METADATA SCAN
            # =====================================================================
            self._timing.start('phase1_metadata_scan')

            all_blobs_by_company: Dict[str, List] = {}
            completed: Set[str] = set()
            failed: Set[str] = set()

            with ThreadPoolExecutor(max_workers=self.max_metadata_workers) as executor:
                for i in range(0, total, self.batch_size):
                    if self._stop_event.is_set():
                        break

                    while not self._pause_event.is_set():
                        if self._stop_event.is_set():
                            break
                        time.sleep(0.5)

                    batch = companies[i:i + self.batch_size]

                    try:
                        futures = {
                            executor.submit(self._scan_single_company_metadata, ticker): ticker
                            for ticker in batch
                        }
                    except RuntimeError:
                        break

                    for future in as_completed(futures):
                        ticker = futures[future]

                        try:
                            result = future.result(timeout=30)

                            if result:
                                completed.add(ticker)
                                self._stats['companies_scanned'] += 1
                                self._stats['blobs_found'] += result.get('blob_count', 0)
                                all_blobs_by_company[ticker] = result.get('blobs', [])

                                cache_success = self._cache.set_company_filings(
                                    ticker, result['data'], result['etag']
                                )
                            else:
                                failed.add(ticker)
                                self._stats['companies_failed'] += 1

                        except Exception:
                            failed.add(ticker)
                            self._stats['companies_failed'] += 1

                    self._cache.update_scan_status(completed_companies=list(completed))

                    if not self._stop_event.is_set():
                        time.sleep(0.2)

            self._timing.end('phase1_metadata_scan')

            # =====================================================================
            # PHASE 2: FILE DOWNLOADS
            # =====================================================================
            if all_blobs_by_company and not self._stop_event.is_set():
                self._download_files_parallel(all_blobs_by_company)

            # Complete
            self._stats['completed_at'] = datetime.now()
            self._cache.update_scan_status(
                in_progress=False,
                completed_companies=list(completed)
            )

            # Save watermark after a successful scan cycle so future runs can
            # skip unless Azure container content changes.
            _write_container_watermark(_get_container_last_modified_ts())

            # Invalidate NON-SEC blob fallback cache for any tickers that had
            # annual-report / half-yearly / interim-report blobs discovered this
            # cycle, so the next UI request picks up the fresh data.
            _NON_SEC_FALLBACK_DOC_PREFIXES = ("annual-report", "half-yearly", "interim-report-Q")
            try:
                from utils.non_sec_blob_fallback import invalidate_non_sec_cache
                for _ticker, _blobs in all_blobs_by_company.items():
                    for _blob in _blobs:
                        _bname = getattr(_blob, "name", "")
                        _parts = [p for p in _bname.split("/") if p]
                        if len(_parts) >= 3:
                            _dt = _parts[2]
                            if any(_dt.startswith(pfx) for pfx in _NON_SEC_FALLBACK_DOC_PREFIXES):
                                invalidate_non_sec_cache(_ticker)
                                break
            except Exception:
                pass

            overall_duration = time.time() - overall_start

        except Exception as e:
            log_error("[BG_SCAN] === SCAN WORKER CRASHED ===")
            log_exception(f"[BG_SCAN] Worker error: {e}")
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="scan worker main loop")
            self._cache.update_scan_status(in_progress=False, error=str(e))
            self._stats['errors'].append(str(e))

    # ========================================================================
    # METADATA SCANNING
    # ========================================================================
    def _scan_single_company_metadata(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Scan metadata for one company - filters invalid doc_types."""
        try:
            container = _get_container_client()
            if not container:
                return None

            prefix = _blob_prefix_path(ticker) + "/"
            all_blobs = list(container.list_blobs(name_starts_with=prefix))

            if not all_blobs:
                return None

            # Support both SEC (HTML) and NON-SEC (PDF) filings
            # PRODUCTION: Only include valid doc_types
            filing_blobs = []
            for blob in all_blobs:
                name = getattr(blob, 'name', '')
                name_lower = name.lower()

                # Check file extension
                if not name_lower.endswith(('.html', '.pdf')):
                    continue
                if name_lower.endswith('-clean.html'):
                    continue

                # PRODUCTION: Validate doc_type from path
                # 4-level: {ticker}/{year}/{doc_type}/filing.{ext}
                # 5-level: {ticker}/{year}/{8-K|6-K}/filing_N/filing.{ext}
                parts = [p for p in name.split('/') if p]
                year, doc_type = _extract_year_and_doctype(parts)
                if not year or not doc_type:
                    continue
                # Transcripts: only allow PDF files
                if doc_type.startswith("transcript-Q") and not name_lower.endswith('.pdf'):
                    continue

                filing_blobs.append(blob)

            if not filing_blobs:
                return None

            data = self._organize_blobs(filing_blobs)
            etag = generate_etag_from_blobs(filing_blobs)

            return {
                'data': data,
                'etag': etag,
                'blob_count': len(filing_blobs),
                'blobs': filing_blobs
            }

        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation=f"scan company metadata for {ticker}")
            return None

    def _organize_blobs(self, blobs: List[Any]) -> Dict[str, Dict[str, Any]]:
        """Organize blobs into filing structure - only valid doc_types."""
        try:
            filings_data: Dict[str, Dict[str, Any]] = {}

            for blob in blobs:
                name = getattr(blob, 'name', '')
                if not name:
                    continue

                parts = [p for p in name.split('/') if p]
                if len(parts) < 4:
                    continue

                year, doc_type_dir = _extract_year_and_doctype(parts)

                if not year or not doc_type_dir:
                    continue

                if doc_type_dir and doc_type_dir.startswith("10-Q-Q"):
                    quarter = doc_type_dir.replace("10-Q-", "")
                    quarter_dict = filings_data.setdefault(year, {}).setdefault("10-Q", {})
                    if isinstance(quarter_dict, dict):
                        quarter_dict[quarter] = name
                elif doc_type_dir and doc_type_dir.startswith("interim-report-Q"):
                    # NON-SEC interim reports (Q1, Q2, Q3, Q4, Q5)
                    quarter = doc_type_dir.replace("interim-report-", "")  # Q1, Q2, Q3, Q4, Q5
                    display_type = self._doc_type_map.get(doc_type_dir, doc_type_dir)
                    filings_data.setdefault(year, {})[display_type] = name
                elif doc_type_dir and doc_type_dir.startswith("transcript-Q"):
                    # Transcripts (Q1, Q2, Q3, Q4) — PDF only
                    display_type = self._doc_type_map.get(doc_type_dir, doc_type_dir)
                    filings_data.setdefault(year, {})[display_type] = name
                elif doc_type_dir:
                    display_type = self._doc_type_map.get(doc_type_dir, doc_type_dir)
                    filings_data.setdefault(year, {})[display_type] = name

            return filings_data
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="organize blobs into filing structure")
            return {}

    # ========================================================================
    # FILE DOWNLOADS
    # ========================================================================
    def _download_files_parallel(self, all_blobs_by_company: Dict[str, List]):
        """Download files for ALL available years discovered from blob paths."""
        try:
            self._timing.start('phase2_file_downloads')

            # Discover all available years from blob paths; falls back to last N years
            prefetch_years = _discover_years_from_blobs(all_blobs_by_company)

            # Collect files to download
            download_tasks = []
            already_cached = 0

            for ticker, blobs in all_blobs_by_company.items():
                for blob in blobs:
                    blob_name = getattr(blob, 'name', '')
                    if not blob_name:
                        continue

                    parts = [p for p in blob_name.split('/') if p]
                    year, _ = _extract_year_and_doctype(parts)
                    if year and year in prefetch_years:
                        local_path = _local_cache_path_for_blob(blob_name)
                        if os.path.exists(local_path):
                            already_cached += 1
                        else:
                            download_tasks.append((ticker, blob, year))

            total_files = len(download_tasks)
            self._stats['files_total'] = total_files
            self._stats['files_skipped'] = already_cached

            if not download_tasks:
                self._timing.end('phase2_file_downloads')
                return

            # Download
            completed_count = 0
            failed_count = 0
            bytes_downloaded = 0

            self._timing.start('file_downloads_execution')

            with ThreadPoolExecutor(max_workers=self.max_download_workers) as executor:
                future_to_task = {
                    executor.submit(self._download_single_file, ticker, blob, year): (ticker, blob, year)
                    for ticker, blob, year in download_tasks
                }

                for future in as_completed(future_to_task):
                    if self._stop_event.is_set():
                        break

                    ticker, blob, year = future_to_task[future]

                    try:
                        result = future.result(timeout=60)

                        if isinstance(result, int) and result > 0:
                            completed_count += 1
                            bytes_downloaded += result
                            with self._lock:
                                self._stats['files_downloaded'] += 1
                                self._stats['bytes_downloaded'] += result
                                self._stats['files_by_year'][year]['downloaded'] += 1
                                self._stats['files_by_year'][year]['total'] += 1
                        else:
                            failed_count += 1
                            with self._lock:
                                self._stats['files_failed'] += 1
                                self._stats['files_by_year'][year]['failed'] += 1
                                self._stats['files_by_year'][year]['total'] += 1

                    except Exception:
                        failed_count += 1
                        with self._lock:
                            self._stats['files_failed'] += 1
                            self._stats['files_by_year'][year]['failed'] += 1
                            self._stats['files_by_year'][year]['total'] += 1

            self._timing.end('file_downloads_execution')
            self._timing.end('phase2_file_downloads')
        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="download files parallel")
            self._timing.end('phase2_file_downloads')

    def _download_single_file(self, ticker: str, blob, year: str) -> Any:
        """Download a single file. Returns bytes count or None."""
        try:
            blob_name = getattr(blob, 'name', '')
            if not blob_name:
                return None

            local_path = _local_cache_path_for_blob(blob_name)

            # Double-check if cached (race condition safety)
            if os.path.exists(local_path):
                try:
                    container = _get_container_client()
                    if container:
                        blob_client = container.get_blob_client(blob_name)
                        props = blob_client.get_blob_properties()
                        last_modified = getattr(props, 'last_modified', None)

                        if last_modified:
                            lm_ts = last_modified.timestamp()
                            if os.path.getmtime(local_path) >= lm_ts:
                                return False
                except Exception:
                    pass

            # Download
            container = _get_container_client()
            if not container:
                return None

            blob_client = container.get_blob_client(blob_name)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            total_bytes = 0

            with open(local_path, "wb") as f:
                downloader = blob_client.download_blob(max_concurrency=4)
                for chunk in downloader.chunks():
                    f.write(chunk)
                    total_bytes += len(chunk)

            # Update mtime
            try:
                props = blob_client.get_blob_properties()
                last_modified = getattr(props, 'last_modified', None)
                if last_modified:
                    os.utime(local_path, (last_modified.timestamp(), last_modified.timestamp()))
            except Exception:
                pass

            return total_bytes

        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation=f"download single file for {ticker}/{year}")
            return None

    # ========================================================================
    # DATABASE
    # ========================================================================
    def _load_companies_from_db(self) -> List[str]:
        """Load company tickers from database."""
        if not _DB_AVAILABLE or _db_manager is None:
            return []

        try:
            rows = _db_manager.execute_query_readonly("""
                SELECT DISTINCT ticker FROM coreiq_filing_metrics_v2
                WHERE ticker IS NOT NULL AND ticker != ''
                ORDER BY ticker
            """)

            tickers = [row["ticker"] for row in rows if row["ticker"]]
            return tickers

        except Exception as e:
            log_structured_error(e, page="", component="BackgroundFilingsScanner", operation="load companies from database")
            return []


# ================================================================================
# GLOBAL SINGLETON & API
# ================================================================================
_scanner_instance: Optional[BackgroundFilingsScanner] = None
_scanner_lock = threading.Lock()


def get_background_scanner() -> BackgroundFilingsScanner:
    """Get singleton scanner instance."""
    global _scanner_instance

    try:
        if _scanner_instance is None:
            with _scanner_lock:
                if _scanner_instance is None:
                    _scanner_instance = BackgroundFilingsScanner()

        return _scanner_instance
    except Exception as e:
        log_structured_error(e, page="", component="get_background_scanner", operation="get or create scanner singleton")
        return BackgroundFilingsScanner()


_initialized = False

_purge_lock = threading.Lock()

def ensure_cache_purge() -> bool:
    """
    Public entry point for the one-time cache purge.
    Safe to call from ANY page — completes in <1ms if purge already done.
    Thread-safe: lock prevents double-execution on first call.

    Call this at the top of every page that accesses filing documents so
    the purge is guaranteed to run regardless of which page the user lands on.
    """
    try:
        with _purge_lock:
            return _run_one_time_cache_purge()
    except Exception as e:
        log_structured_error(e, page="", component="ensure_cache_purge", operation="ensure one-time cache purge")
        return False


def init_background_scanner(auto_start: bool = True, force_scan: bool = False) -> bool:
    """
    Initialize background scanner for Streamlit.

    Args:
        auto_start: If True, starts scanning immediately
        force_scan: If True, skip cache check and scan anyway

    Returns:
        True if initialized successfully
    """
    global _initialized

    log_info(f"[BG_SCAN][INIT] called — _initialized={_initialized} auto_start={auto_start} force_scan={force_scan}")

    if _initialized and not force_scan:
        log_info("[BG_SCAN][INIT] already initialized — returning early")
        return True

    try:
        # ONE-TIME PURGE: silently clear stale caches on first-ever launch.
        # Runs before the scanner so fresh files are downloaded immediately after.
        _run_one_time_cache_purge()

        scanner = get_background_scanner()

        if auto_start:
            scanner.start(force_scan=force_scan)

        _initialized = True
        log_info("[BG_SCAN][INIT] initialization complete")
        return True

    except Exception as e:
        log_error(f"[BG_SCAN] Initialization failed: {e}")
        log_structured_error(e, page="", component="init_background_scanner", operation="initialize background scanner")
        return False


def get_scan_progress() -> Dict[str, Any]:
    """Get scan progress."""
    try:
        scanner = get_background_scanner()
        progress = scanner.get_progress()
        timing = scanner.get_timing_report()

        return {**progress, 'timing': timing}
    except Exception as e:
        log_structured_error(e, page="", component="get_scan_progress", operation="get scan progress")
        return {
            'is_running': False,
            'completed': 0,
            'total': 0,
            'scan_skipped': False,
            'error': str(e)
        }


def get_cache_audit() -> Dict[str, Any]:
    """Get cache audit report."""
    try:
        return _audit_file_cache()
    except Exception as e:
        log_structured_error(e, page="", component="get_cache_audit", operation="get cache audit report")
        return {}


def is_scan_complete() -> bool:
    """Check if scan is complete or skipped."""
    try:
        progress = get_scan_progress()
        if progress.get('scan_skipped'):
            return True
        return not progress['is_running'] or progress['percent'] >= 100
    except Exception as e:
        log_structured_error(e, page="", component="is_scan_complete", operation="check if scan is complete")
        return True
