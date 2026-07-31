"""Package the local cache and upload it to Azure blob for STG to restore.

The extraction has already been paid for once. STG should never repeat it, and
the container has no way to reach a developer's laptop — but it already talks
to Azure Blob with credentials it holds. So: tar the cache, put it in the same
container under a known name, and let the app pull it down on first boot
(see store_count_cache.restore_from_blob).

    .venv/bin/python scripts/package_store_counts.py

Nothing else is needed on the server. If you would rather place it by hand over
the Debian shell, the script prints a curl command using a short-lived SAS URL.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import sys
import tarfile

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from azure.storage.blob import (BlobSasPermissions, BlobServiceClient,
                                generate_blob_sas)

from data import store_count_cache as cache

BLOB_NAME = "_coreiq_cache/store_counts.tar.gz"
SNAPSHOT_BLOB = "_coreiq_cache/store_counts_snapshot.json"


def main() -> None:
    source = cache.cache_dir()
    files = sorted(source.glob("*.json"))
    if not files:
        print(f"nothing to package — {source} is empty")
        return

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for path in files:
            bundle.add(path, arcname=path.name)
    payload = archive.getvalue()

    account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    key = os.environ["AZURE_STORAGE_ACCOUNT_KEY"]
    container_name = os.environ["AZURE_BLOB_CONTAINER"]
    service = BlobServiceClient(f"https://{account}.blob.core.windows.net",
                                credential=key)
    container = service.get_container_client(container_name)
    container.get_blob_client(BLOB_NAME).upload_blob(payload, overwrite=True)

    # The snapshot is what the app actually restores and reads: one file
    # instead of 1,544, which on the /home SMB share is one ~29 ms write
    # rather than 45 seconds of them.
    cache.write_snapshot()
    snapshot_bytes = cache.snapshot_path().read_bytes()
    container.get_blob_client(SNAPSHOT_BLOB).upload_blob(snapshot_bytes,
                                                         overwrite=True)

    sas = generate_blob_sas(
        account_name=account, container_name=container_name, blob_name=SNAPSHOT_BLOB,
        account_key=key, permission=BlobSasPermissions(read=True),
        expiry=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7))
    url = f"https://{account}.blob.core.windows.net/{container_name}/{SNAPSHOT_BLOB}?{sas}"

    print(f"packaged {len(files):,} entries — archive {len(payload) / 1024:.0f} KB, "
          f"snapshot {len(snapshot_bytes) / 1024:.0f} KB")
    print(f"uploaded  {container_name}/{BLOB_NAME}")
    print(f"uploaded  {container_name}/{SNAPSHOT_BLOB}   <- what the app reads")
    print("\nThe app restores this automatically on first boot. To place it by")
    print("hand from the Debian shell instead, run:\n")
    print(f"  mkdir -p /home/filing_extract/store_counts && \\\n"
          f"  curl -sSL '{url}' \\\n"
          f"    -o /home/filing_extract/store_counts/store_counts_snapshot.json && \\\n"
          f"  wc -c /home/filing_extract/store_counts/store_counts_snapshot.json")
    print("\n(SAS link is read-only and expires in 7 days.)")


if __name__ == "__main__":
    main()
