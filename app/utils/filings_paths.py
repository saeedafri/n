"""Persistent on-disk root for the filings caches (blob files + metadata).

WHY THIS EXISTS
---------------
The filing/transcript files (PDF + HTML) and their metadata used to be cached
under ``<repo>/data`` — i.e. ``/home/site/wwwroot/data`` on Azure App Service.
wwwroot is REPLACED on every deploy (STG: ~6/day) and is not guaranteed to
survive a container restart, so the entire download cache was thrown away
constantly and the background scanner re-downloaded ~44k blobs from Azure Blob
Storage all over again — while users paid live per-file downloads until it
caught up.

``/home`` (outside wwwroot) is the SAME Azure Files SMB share but is NOT
touched by deploys, so anything cached there persists across restarts AND
deploys. This mirrors ``repository.edgar_cache_dir`` — persistence is the
DEFAULT on Azure, not opt-in, because a developer with only container access
can't change control-plane App Settings.

Resolution order:
  1. FILINGS_CACHE_DIR env var, if set (explicit App Setting override).
  2. AUTO on Azure App Service: /home/filings_cache (persistent SMB share).
     Detected via the built-in WEBSITE_SITE_NAME env var + a writable /home.
  3. Local/other: <repo>/data.
"""
import os

_LOGGED = False


def filings_data_root():
    """Return the root dir that holds filings_blob_cache/ + filings_metadata_cache/.

    Every reader and writer of the filings cache must resolve its paths from
    here so the scanner writes and the page reads land in the same place.
    """
    override = os.getenv("FILINGS_CACHE_DIR", "").strip()
    if override:
        return _announce(override, source="FILINGS_CACHE_DIR")

    if os.getenv("WEBSITE_SITE_NAME"):
        try:
            if os.path.isdir("/home") and os.access("/home", os.W_OK):
                return _announce("/home/filings_cache", source="azure_home_persistent")
        except Exception:
            pass

    repo_data = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data",
    )
    return _announce(repo_data, source="repo_data_ephemeral")


def _announce(path, source):
    """Log the resolved root exactly once so STG shows persistent vs ephemeral."""
    global _LOGGED
    if not _LOGGED:
        _LOGGED = True
        persistent = source in ("FILINGS_CACHE_DIR", "azure_home_persistent")
        try:
            from utils.server_logger import log_info
            log_info(
                f"[FILINGS_CACHE_ROOT] path={path} source={source} "
                f"persistent={'yes' if persistent else 'NO-wiped-on-deploy'}"
            )
        except Exception:
            pass
    return path
