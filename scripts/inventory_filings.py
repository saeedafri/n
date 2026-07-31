"""List every 10-K we hold in Azure blob and cache the inventory.

The container holds ~500k objects, so this walk takes minutes; the result is
written to disk and reused. Blob layout is {TICKER}/{storage_year}/{doc_type}/filing.html.
"""

from __future__ import annotations

import json
import os
import sys

for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from azure.storage.blob import BlobServiceClient

OUT = "/tmp/sc_report/inventory.json"
DOC_TYPES = {"10-K"}


def main() -> None:
    service = BlobServiceClient(
        f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
        credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
    container = service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])

    filings = []
    scanned = 0
    for blob in container.list_blobs():
        scanned += 1
        parts = blob.name.split("/")
        if len(parts) == 4 and parts[3] == "filing.html" and parts[2] in DOC_TYPES:
            ticker, storage_year, doc_type, _ = parts
            if not storage_year.isdigit():
                continue
            filings.append({"ticker": ticker, "storage_year": int(storage_year),
                            "doc_type": doc_type, "blob": blob.name,
                            "size": blob.size})
        if scanned % 100_000 == 0:
            print(f"  scanned {scanned:,} objects, {len(filings):,} filings",
                  flush=True)

    filings.sort(key=lambda f: (f["ticker"], f["storage_year"]))
    json.dump(filings, open(OUT, "w"), indent=1)
    tickers = {f["ticker"] for f in filings}
    print(f"\nobjects scanned : {scanned:,}")
    print(f"10-K filings    : {len(filings):,}")
    print(f"tickers         : {len(tickers):,}")
    print(f"WROTE {OUT}")


if __name__ == "__main__":
    main()
