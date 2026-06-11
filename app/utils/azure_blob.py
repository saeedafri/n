"""
Azure Blob Storage utilities
"""
import os
from typing import Optional

try:
    from utils.server_logger import log_structured_error, log_error
except ImportError:
    log_error = print
    log_structured_error = None

# Azure config (same as company_filings.py)
def _get_azure_config():
    """Get Azure configuration from environment."""
    try:
        acct = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "csmarketdata").strip()
        key = os.getenv("AZURE_STORAGE_ACCOUNT_KEY", "").strip()
        container_name = (os.getenv("AZURE_BLOB_CONTAINER") or "azure-storage-test").strip()
        return acct, key, container_name
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="azure_blob", component="_get_azure_config", operation="reading_azure_config")
        else:
            log_error(f"[azure_blob] _get_azure_config failed: {e}")
        return "", "", ""


# ---------------------------------------------------------------------------
# Local blob cache — transcript PDFs (used by earnings_calls.py)
# Mirrors the layout of company_filings.py but defined here to avoid
# importing that page (which would trigger its module-level main() call).
# ---------------------------------------------------------------------------

# Cache root: {repo_root}/data/filings_blob_cache/
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILINGS_BLOB_CACHE_DIR = os.path.join(_APP_ROOT, "data", "filings_blob_cache")


def _get_blob_prefix() -> str:
    """Return the configured Azure blob prefix (empty string if not set)."""
    try:
        return (os.getenv("AZURE_BLOB_PREFIX") or "").strip("/")
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="azure_blob", component="_get_blob_prefix", operation="reading_blob_prefix")
        else:
            log_error(f"[azure_blob] _get_blob_prefix failed: {e}")
        return ""


def local_cache_path_for_blob(blob_name: str) -> str:
    """Return the local filesystem path where a blob is (or will be) cached."""
    try:
        prefix = _get_blob_prefix()
        rel = (blob_name or "").lstrip("/").replace("\\", "/")
        if prefix:
            pfx = prefix + "/"
            if rel.startswith(pfx):
                rel = rel[len(pfx):]
        return os.path.join(FILINGS_BLOB_CACHE_DIR, rel.replace("/", os.sep))
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="azure_blob", component="local_cache_path_for_blob", operation="resolving_local_cache_path")
        else:
            log_error(f"[azure_blob] local_cache_path_for_blob failed for {blob_name}: {e}")
        return ""


def ensure_local_blob_for_transcript(blob_name: str) -> Optional[str]:
    """
    Ensure a transcript PDF blob is downloaded and cached locally.

    Returns the local file path, or None if the download failed.
    Uses a freshness check: skips the download when the cached file is
    newer than (or same age as) the blob's last-modified timestamp.
    """
    if not blob_name:
        return None

    local_path = local_cache_path_for_blob(blob_name)

    try:
        container = get_container_client()
        if not container:
            # No Azure client — fall back to any stale cached copy
            return local_path if os.path.exists(local_path) else None

        blob_client = container.get_blob_client(blob_name)
        props = blob_client.get_blob_properties()
        last_modified = getattr(props, "last_modified", None)
        lm_ts = None
        try:
            if last_modified:
                lm_ts = last_modified.timestamp()
        except Exception:
            pass

        # Return cached file if it is already fresh
        if os.path.exists(local_path) and lm_ts is not None:
            if os.path.getmtime(local_path) >= lm_ts:
                return local_path

        # Download with parallel chunks
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            downloader = blob_client.download_blob(max_concurrency=4)
            for chunk in downloader.chunks():
                f.write(chunk)

        # Stamp mtime to match the blob so future freshness checks work
        if lm_ts is not None:
            try:
                os.utime(local_path, (lm_ts, lm_ts))
            except Exception:
                pass

        return local_path

    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="azure_blob", component="ensure_local_blob_for_transcript", operation="downloading_transcript_blob")
        else:
            log_error(f"[blob] ensure_local_blob_for_transcript failed for {blob_name}: {e}")
        # Serve stale cache rather than a hard failure
        return local_path if os.path.exists(local_path) else None


def get_container_client():
    """Get Azure container client."""
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        log_error("Azure SDK not available")
        return None

    acct, key, container_name = _get_azure_config()
    if not key:
        return None

    try:
        account_url = f"https://{acct}.blob.core.windows.net"
        blob_service = BlobServiceClient(account_url=account_url, credential=key)
        return blob_service.get_container_client(container_name)
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="azure_blob", component="get_container_client", operation="connecting_to_azure_blob")
        else:
            log_error(f"Azure connection failed: {e}")
        return None


def download_blob_to_memory(blob_name: str) -> bytes:
    """
    Download blob to memory (for PDF viewing).

    Args:
        blob_name: Path to blob in Azure

    Returns:
        Blob content as bytes
    """
    container = get_container_client()
    if not container:
        if log_structured_error:
            log_structured_error(
                Exception("Azure not configured or connection failed"),
                page="azure_blob",
                component="download_blob_to_memory",
                operation="acquiring_container_client",
            )
        else:
            log_error(f"[azure_blob] download_blob_to_memory: Azure not configured for {blob_name}")
        return b""

    try:
        blob_client = container.get_blob_client(blob_name)
        downloader = blob_client.download_blob()
        return downloader.readall()
    except Exception as e:
        if log_structured_error:
            log_structured_error(e, page="azure_blob", component="download_blob_to_memory", operation="downloading_blob_to_memory")
        else:
            log_error(f"Azure download failed for {blob_name}: {e}")
        return b""
