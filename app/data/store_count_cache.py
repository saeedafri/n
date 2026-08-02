"""Permanent store-count cache, keyed by SEC accession number.

A filed 10-K never changes, so an entry here can never go stale. A new fiscal
year arrives as a new accession; an amendment (10-K/A) is a different accession
and is picked up on its own. Nothing is ever deleted or refreshed on a timer —
the only thing that forces recomputation is a bump to EXTRACTOR_VERSION.

On the App Service this must live on /home (the persistent Azure share, ~499 GB
free, survives restart AND deploy). /tmp and the container filesystem are wiped
on every deploy, which is exactly the trap the EDGAR caches fell into.

    STORE_COUNT_CACHE_DIR=/home/filing_extract/store_counts
    WEBSITES_ENABLE_APP_SERVICE_STORAGE=true
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# Bump only when the extraction logic changes in a way that should invalidate
# previously written values. Entries written by an older version are re-run.
#
# 2: support-site and geography-noun vetoes, decimal and footnote-marker
#    rejection, 'opened' dropped as a fleet verb, third-party statistics
#    excluded, unnamed-total ceiling, roll-forward sanity, aspirational prose
#    demoted a tier, unclassified companies no longer treated as non-retail.
#    Leaving this at 1 meant every one of those fixes was masked by a cache
#    entry the old parser had written — TJX kept publishing a US-only 3,790
#    while the parser in front of it was returning the correct 5,214.
EXTRACTOR_VERSION = 5


def cache_dir() -> Path:
    """Where extractions live.

    Same resolution order as edgar_cache_dir() and filings_data_root(), so all
    three persist identically and a developer with only container access does
    not have to set an App Setting:

      1. STORE_COUNT_CACHE_DIR env var, if set (explicit App Setting override).
      2. AUTO on Azure App Service: /home/filing_extract/store_counts. /home is
         the persistent SMB share that survives restarts AND deploys, while
         wwwroot is replaced on every deploy. Detected via the built-in
         WEBSITE_SITE_NAME env var plus a writable /home — checking /home alone
         would match any Linux box and silently write outside the repo.
      3. Local/other: <repo>/data/store_counts.
    """
    override = os.getenv("STORE_COUNT_CACHE_DIR", "").strip()
    if override:
        path = Path(override)
    elif os.getenv("WEBSITE_SITE_NAME") and os.path.isdir("/home") \
            and os.access("/home", os.W_OK):
        path = Path("/home/filing_extract/store_counts")
    else:
        path = Path(__file__).resolve().parent.parent.parent / "data" / "store_counts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_for(accession: str) -> Path:
    return cache_dir() / f"{accession.replace('/', '_')}.json"


def read(accession: str) -> Optional[Dict[str, Any]]:
    """Cached extraction, or None when absent or written by an older version."""
    path = _file_for(accession)
    if not path.is_file():
        return None
    try:
        entry = json.loads(path.read_text())
    except Exception:
        return None
    if entry.get("extractor_version") != EXTRACTOR_VERSION:
        return None
    return entry


def write(accession: str, payload: Dict[str, Any]) -> None:
    """Write one extraction. Atomic — a half-written file would be read as
    valid JSON-or-nothing on the next boot otherwise."""
    entry = dict(payload)
    entry["accession"] = accession
    entry["extractor_version"] = EXTRACTOR_VERSION
    target = _file_for(accession)
    scratch = target.with_suffix(".json.tmp")
    scratch.write_text(json.dumps(entry, ensure_ascii=False, indent=1, default=str))
    scratch.replace(target)


def read_many(accessions: List[str]) -> Dict[str, Dict[str, Any]]:
    found = {}
    for accession in accessions:
        entry = read(accession)
        if entry:
            found[accession] = entry
    return found


def all_entries() -> Iterator[Dict[str, Any]]:
    for path in sorted(cache_dir().glob("*.json")):
        try:
            yield json.loads(path.read_text())
        except Exception:
            continue


SNAPSHOT_NAME = "store_counts_snapshot.json"

_snapshot: Optional[Dict[str, Any]] = None
_snapshot_stamp: float = 0.0
_snapshot_lock = __import__("threading").Lock()


def snapshot_path() -> Path:
    return cache_dir() / SNAPSHOT_NAME


def write_snapshot() -> int:
    """Collapse every per-accession entry into ONE file the app can read.

    The per-accession files stay the source of record — they carry provenance
    and make "extract once, ever" work. But they are the wrong shape to read
    from: /home is an SMB share where a page needing ten years of a ticker pays
    ten ~2.4 ms round trips, and unpacking 1,544 of them onto that share costs
    ~45 seconds at ~29 ms per write.

    The whole dataset is 21 KB of values. One file, parsed once per process,
    turns every subsequent lookup into a dict access.
    """
    combined: Dict[str, Dict[str, Any]] = {}
    for entry in all_entries():
        ticker = str(entry.get("ticker", "")).upper()
        value = entry.get("value")
        if not ticker or not value:
            continue
        combined.setdefault(ticker, {})[str(entry["fiscal_year"])] = {
            "value": int(value),
            "confidence": entry.get("confidence"),
            "basis": entry.get("basis"),
            "by_country": entry.get("by_country") or {},
            "by_continent": entry.get("by_continent") or {},
            "needs_review": bool(entry.get("needs_review")),
        }
    target = snapshot_path()
    scratch = target.with_suffix(".json.tmp")
    scratch.write_text(json.dumps(combined, separators=(",", ":")))
    scratch.replace(target)
    return sum(len(years) for years in combined.values())


def load_snapshot() -> Dict[str, Any]:
    """The snapshot, held in memory and re-read only when the file changes.

    mtime is checked rather than assumed immutable so the background refresh
    worker's writes are picked up without a restart.
    """
    global _snapshot, _snapshot_stamp
    path = snapshot_path()
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return _snapshot or {}
    if _snapshot is not None and stamp == _snapshot_stamp:
        return _snapshot
    with _snapshot_lock:
        try:
            loaded = json.loads(path.read_text())
        except Exception:
            return _snapshot or {}
        _snapshot = loaded
        _snapshot_stamp = stamp
        return _snapshot


def by_ticker(ticker: str) -> Dict[int, Dict[str, Any]]:
    """Every cached fiscal year for one ticker.

    Served from the in-memory snapshot: no disk I/O at all after the first
    call in a process, which is what keeps the Additional Data tab instant.
    The per-accession files are only consulted if the snapshot is missing.
    """
    fast = load_snapshot().get(ticker.upper())
    if fast:
        return {int(year): entry for year, entry in fast.items()
                if entry.get("value")}

    index_file = cache_dir() / "_index.json"
    if index_file.is_file():
        try:
            index = json.loads(index_file.read_text())
            entries = {}
            for accession in index.get(ticker.upper(), []):
                entry = read(accession)
                if entry and entry.get("value") is not None:
                    entries[int(entry["fiscal_year"])] = entry
            return entries
        except Exception:
            pass
    entries = {}
    for entry in all_entries():
        if str(entry.get("ticker", "")).upper() == ticker.upper() and entry.get("value"):
            entries[int(entry["fiscal_year"])] = entry
    return entries


CACHE_BUNDLE_BLOB = "_coreiq_cache/store_counts.tar.gz"
SNAPSHOT_BLOB = "_coreiq_cache/store_counts_snapshot.json"


def restore_from_blob() -> int:
    """Seed an empty cache from Azure blob.

    The extraction is paid for once and uploaded by
    scripts/package_store_counts.py. A fresh container would otherwise
    re-derive all of it — spending money and CPU to recompute values that
    cannot have changed, because a filed 10-K is immutable.

    Restores the SNAPSHOT, not the per-accession archive: it is one ~200 KB
    file and one write, against 1,544 writes at ~29 ms each on the /home SMB
    share — 45 seconds of a cold container's life to land the same data. The
    archive is only unpacked if the snapshot is missing, and is not needed to
    serve pages (see write_snapshot).

    Only runs when nothing is cached yet, so it can never overwrite newer work.
    """
    target = cache_dir()
    if snapshot_path().is_file() or any(target.glob("*.json")):
        return 0
    try:
        from utils.azure_blob import get_container_client
        container = get_container_client()
    except Exception:
        return 0

    try:
        payload = container.get_blob_client(SNAPSHOT_BLOB).download_blob().readall()
        combined = json.loads(payload)
        scratch = snapshot_path().with_suffix(".json.tmp")
        scratch.write_bytes(payload)
        scratch.replace(snapshot_path())
        return sum(len(years) for years in combined.values())
    except Exception:
        pass

    # Fallback: the full archive, if no snapshot was published.
    try:
        import io
        import tarfile
        payload = container.get_blob_client(CACHE_BUNDLE_BLOB).download_blob().readall()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as bundle:
            # Refuse absolute paths and traversal — this writes to /home.
            safe = [m for m in bundle.getmembers()
                    if m.isfile() and "/" not in m.name and not m.name.startswith("..")]
            bundle.extractall(target, members=safe)
        rebuild_index()
        write_snapshot()
        return len(list(target.glob("*.json")))
    except Exception:
        return 0


def rebuild_index() -> Dict[str, List[str]]:
    """Ticker -> [accession]. Keeps the runtime read to one small file plus the
    handful of entries a page actually needs."""
    index: Dict[str, List[str]] = {}
    for entry in all_entries():
        ticker = str(entry.get("ticker", "")).upper()
        if not ticker or not entry.get("accession"):
            continue
        index.setdefault(ticker, []).append(entry["accession"])
    (cache_dir() / "_index.json").write_text(json.dumps(index, indent=1))
    return index
