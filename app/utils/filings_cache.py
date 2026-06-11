"""
Filings Metadata Cache - Production-grade persistent cache for Azure blob metadata.
==================================================================================

This module provides a persistent, thread-safe cache for Azure Blob Storage metadata
using DiskCache (SQLite-backed). Features:

- ETag-based validation for cache freshness
- Incremental updates (no full re-scan needed)
- Thread-safe and process-safe operations
- Automatic expiry (24 hours)
- Tag-based eviction
- Progress tracking for background scans

Usage:
    from utils.filings_cache import get_filings_cache

    cache = get_filings_cache()
    data = cache.get_company_filings("AAPL")
    if data and cache.is_cache_valid("AAPL", current_etag):
        return data
    else:
        # Fetch from Azure
        cache.set_company_filings("AAPL", new_data, new_etag)

Author: Coresight Research
"""
import os
import json
import hashlib
import threading
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path

# Import server logger for error tracking only
try:
    from utils.server_logger import log_error
except ImportError:
    log_error = logging.error

logger = logging.getLogger(__name__)

# Try to import diskcache, but provide fallback if not available
try:
    from diskcache import Cache
    DISKCACHE_AVAILABLE = True
except ImportError:
    DISKCACHE_AVAILABLE = False

# Cache directory configuration
DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "filings_metadata_cache"
)

# Ensure cache directory exists
os.makedirs(DEFAULT_CACHE_DIR, exist_ok=True)


class InMemoryFallbackCache:
    """
    In-memory fallback when diskcache is not available.
    Provides same interface but no persistence.
    """

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._tags: Dict[str, str] = {}
        self._lock = threading.RLock()

    def get(self, key: str, default=None, tag=False):
        with self._lock:
            if key not in self._data:
                return (default, None) if tag else default
            value = self._data.get(key)
            if tag:
                return (value, self._tags.get(key))
            return value

    def set(self, key: str, value: Any, tag: Optional[str] = None, expire: Optional[int] = None):
        with self._lock:
            self._data[key] = value
            if tag is not None:
                self._tags[key] = tag
            return True

    def clear(self):
        with self._lock:
            self._data.clear()
            self._tags.clear()

    def close(self):
        pass


class FilingsMetadataCache:
    """
    Persistent cache for Azure blob metadata with ETag validation.

    This cache stores the filing metadata structure for each company:
    {ticker: {year: {doc_type: blob_name_or_quarter_dict}}}

    Features:
    - ETag-based cache invalidation (check if Azure data changed)
    - Incremental updates (update one company at a time)
    - Thread-safe concurrent access
    - Automatic 24-hour expiry
    - Scan progress tracking

    Args:
        cache_dir: Directory for cache files (default: data/filings_metadata_cache)
    """

    # Default cache expiry: 24 hours
    DEFAULT_EXPIRY = 86400  # seconds

    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._lock = threading.RLock()
        self._initialized = False
        self._cache = None

        self._init_cache()

    def _init_cache(self):
        """Initialize the underlying cache."""
        try:
            if DISKCACHE_AVAILABLE:
                # Use diskcache with tag index for ETag tracking
                self._cache = Cache(self.cache_dir, tag_index=True)
            else:
                self._cache = InMemoryFallbackCache()

            self._initialized = True

        except Exception as e:
            log_error(f"[CACHE] Failed to initialize cache: {e}")
            self._cache = InMemoryFallbackCache()
            self._initialized = True

    def _get_key(self, ticker: str) -> str:
        """Generate cache key for a ticker."""
        return f"filings:v1:{ticker.upper()}"

    def get_company_filings(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Get cached filings data for a company.

        Args:
            ticker: Company ticker symbol

        Returns:
            Dict with keys: 'data', 'etag', 'cached_at' or None if not cached
        """
        if not self._initialized:
            return None

        key = self._get_key(ticker)

        try:
            result = self._cache.get(key, tag=True, default=None)

            if result is None:
                return None

            data, etag = result

            # Get timestamp from separate key
            timestamp_key = f"{key}:timestamp"
            cached_at = self._cache.get(timestamp_key)

            return {
                'data': data,
                'etag': etag or "",  # ETag stored as tag
                'cached_at': cached_at
            }

        except Exception as e:
            log_error(f"[CACHE] Error reading cache for {ticker}: {e}")
            return None

    def set_company_filings(
        self,
        ticker: str,
        data: Dict,
        etag: str,
        expire: int = None
    ) -> bool:
        """
        Cache filings data with ETag for validation.

        Args:
            ticker: Company ticker symbol
            data: Filings metadata dict
            etag: ETag for cache validation
            expire: Expiry time in seconds (default: 24 hours)

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized:
            return False

        key = self._get_key(ticker)
        expire = expire or self.DEFAULT_EXPIRY

        try:
            with self._lock:
                # Store data with ETag as tag
                self._cache.set(key, data, tag=etag, expire=expire)

                # Store timestamp separately
                timestamp_key = f"{key}:timestamp"
                self._cache.set(timestamp_key, datetime.now().isoformat(), expire=expire)

            return True

        except Exception as e:
            log_error(f"[CACHE] Error writing cache for {ticker}: {e}")
            return False

    def is_cache_valid(self, ticker: str, current_etag: str) -> bool:
        """
        Check if cached data is still valid using ETag comparison.

        Args:
            ticker: Company ticker symbol
            current_etag: Current ETag from Azure

        Returns:
            True if cache is valid (ETags match), False otherwise
        """
        cached = self.get_company_filings(ticker)

        if not cached:
            return False

        cached_etag = cached.get('etag', '')

        # Normalize ETags (remove quotes if present)
        cached_etag = cached_etag.strip('"')
        current_etag = current_etag.strip('"')

        is_valid = cached_etag == current_etag

        return is_valid

    def invalidate_company(self, ticker: str) -> bool:
        """
        Remove a company's data from cache.

        Args:
            ticker: Company ticker symbol

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized:
            return False

        key = self._get_key(ticker)

        try:
            with self._lock:
                # diskcache doesn't have direct delete, use set with None and expire immediately
                self._cache.set(key, None, expire=1)
                self._cache.set(f"{key}:timestamp", None, expire=1)

            return True

        except Exception as e:
            log_error(f"[CACHE] Error invalidating cache for {ticker}: {e}")
            return False

    def get_scan_status(self) -> Dict[str, Any]:
        """
        Get background scan progress.

        Returns:
            Dict with keys:
            - in_progress: bool
            - completed_companies: List[str]
            - total_companies: int
            - last_scan: str (ISO timestamp)
            - error: str (if any)
        """
        if not self._initialized:
            return {
                'in_progress': False,
                'completed_companies': [],
                'total_companies': 0,
                'last_scan': None,
                'error': 'Cache not initialized'
            }

        try:
            status = self._cache.get('__scan_status__', default={
                'in_progress': False,
                'completed_companies': [],
                'total_companies': 0,
                'last_scan': None,
                'error': None
            })
            return status

        except Exception as e:
            return {
                'in_progress': False,
                'completed_companies': [],
                'total_companies': 0,
                'last_scan': None,
                'error': str(e)
            }

    def update_scan_status(self, **kwargs) -> bool:
        """
        Update scan progress.

        Args:
            **kwargs: Any of the scan status fields to update

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized:
            return False

        try:
            status = self.get_scan_status()
            status.update(kwargs)
            self._cache.set('__scan_status__', status)
            return True

        except Exception as e:
            log_error(f"[CACHE] Error updating scan status: {e}")
            return False

    def get_cached_companies(self) -> List[str]:
        """
        Get list of companies currently in cache.

        Returns:
            List of ticker symbols
        """
        if not self._initialized or not DISKCACHE_AVAILABLE:
            return []

        try:
            companies = []
            for key in self._cache.iterkeys():
                if key.startswith('filings:v1:') and not key.endswith(':timestamp'):
                    ticker = key.replace('filings:v1:', '')
                    companies.append(ticker)
            return companies

        except Exception as e:
            log_error(f"[CACHE] Error listing cached companies: {e}")
            return []

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dict with cache statistics
        """
        stats = {
            'initialized': self._initialized,
            'diskcache_available': DISKCACHE_AVAILABLE,
            'cache_dir': self.cache_dir,
            'cached_companies': 0,
            'scan_status': self.get_scan_status()
        }

        try:
            stats['cached_companies'] = len(self.get_cached_companies())

            if DISKCACHE_AVAILABLE and hasattr(self._cache, 'volume'):
                stats['volume_bytes'] = self._cache.volume()
        except Exception as e:
            log_error(f"[CACHE] Error computing cache stats: {e}")

        return stats

    def clear(self) -> bool:
        """
        Clear all cached data. Use with caution!

        Returns:
            True if successful, False otherwise
        """
        if not self._initialized:
            return False

        try:
            self._cache.clear()
            return True

        except Exception as e:
            log_error(f"[CACHE] Error clearing cache: {e}")
            return False

    def close(self):
        """Close cache connection."""
        if self._cache and hasattr(self._cache, 'close'):
            try:
                self._cache.close()
            except Exception as e:
                log_error(f"[CACHE] Error closing cache: {e}")


# Global singleton instance
_cache_instance: Optional[FilingsMetadataCache] = None
_cache_lock = threading.Lock()


def get_filings_cache() -> FilingsMetadataCache:
    """
    Get singleton cache instance.

    This function returns a thread-safe singleton instance of the cache.
    Safe to call from multiple threads.

    Returns:
        FilingsMetadataCache instance
    """
    global _cache_instance

    if _cache_instance is None:
        with _cache_lock:
            if _cache_instance is None:
                _cache_instance = FilingsMetadataCache()

    return _cache_instance


def generate_etag_from_blobs(blobs: List[Any]) -> str:
    """
    Generate aggregate ETag from list of blobs.

    This creates a hash of all blob ETags to detect any changes.

    Args:
        blobs: List of blob objects with etag attribute

    Returns:
        MD5 hash of sorted ETags
    """
    try:
        etags = []
        for b in blobs:
            etag = getattr(b, 'etag', None)
            if etag:
                # Normalize ETag (remove quotes)
                etag = str(etag).strip('"')
                etags.append(etag)

        if not etags:
            # Fallback to hash of names and last modified
            names = sorted([getattr(b, 'name', '') for b in blobs])
            last_modified = [str(getattr(b, 'last_modified', '')) for b in blobs]
            content = ','.join(names + last_modified)
        else:
            etags = sorted(etags)
            content = ','.join(etags)

        return hashlib.md5(content.encode()).hexdigest()

    except Exception:
        # Fallback to timestamp-based ETag
        return datetime.now().strftime('%Y%m%d%H%M%S')


def invalidate_company_cache(ticker: str) -> bool:
    """
    Utility function to invalidate a company's cache.

    Args:
        ticker: Company ticker symbol

    Returns:
        True if successful, False otherwise
    """
    return get_filings_cache().invalidate_company(ticker)


def get_cache_stats() -> Dict[str, Any]:
    """
    Utility function to get cache statistics.

    Returns:
        Dict with cache statistics
    """
    return get_filings_cache().get_cache_stats()


# Cleanup on module exit
import atexit

@atexit.register
def _cleanup_cache():
    """Cleanup cache on application exit."""
    global _cache_instance
    if _cache_instance is not None:
        _cache_instance.close()
        _cache_instance = None
