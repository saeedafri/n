"""Run the filing parser over ONE batch of filings and flag what looks wrong.

Small batches on purpose: fix what a batch reveals before moving to the next,
rather than reprocessing 4,394 filings to discover the same defect late.

    .venv/bin/python scripts/batch_check.py 1        # filings 1-100
    .venv/bin/python scripts/batch_check.py 2 100    # filings 101-200

Free — reads filings from Azure blob, no model calls.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from azure.storage.blob import BlobServiceClient

import pymysql

from data.store_count_extractor import (
    NON_RETAIL_TICKERS, choose_best, extract_from_filing, filing_to_text,
    fiscal_year_of, industry_allows_store_count)


def industry_by_ticker():
    connection = pymysql.connect(
        host=os.environ["STG_DB_HOST"], port=int(os.environ.get("STG_DB_PORT", 3306)),
        user=os.environ["STG_DB_USER"], password=os.environ["STG_DB_PASSWORD"],
        database=os.environ["STG_DB_NAME"], ssl={"ssl": {}},
        cursorclass=pymysql.cursors.DictCursor, read_timeout=120)
    with connection.cursor() as cursor:
        cursor.execute("SELECT ticker, primary_industry_coresight AS industry "
                       "FROM coreiq_companies WHERE ticker IS NOT NULL")
        found = {r["ticker"].upper(): r["industry"] for r in cursor.fetchall()}
    connection.close()
    return found

BATCH = int(sys.argv[1]) if len(sys.argv) > 1 else 1
SIZE = int(sys.argv[2]) if len(sys.argv) > 2 else 100

service = BlobServiceClient(
    f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
container = service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])


def main() -> None:
    inventory = json.load(open("/tmp/sc_report/inventory.json"))
    industries = industry_by_ticker()
    before = len({f["ticker"] for f in inventory})
    inventory = [f for f in inventory
                 if f["ticker"].upper() not in NON_RETAIL_TICKERS
                 and industry_allows_store_count(industries.get(f["ticker"].upper()),
                                                 allow_unclassified=True)]
    after = len({f["ticker"] for f in inventory})
    print(f"industry gate: {before} tickers -> {after} that operate stores")
    start = (BATCH - 1) * SIZE
    batch = inventory[start:start + SIZE]
    print(f"batch {BATCH}: filings {start + 1}-{start + len(batch)} "
          f"of {len(inventory)}  ({len({f['ticker'] for f in batch})} tickers)\n")

    def run(filing):
        try:
            raw = container.get_blob_client(filing["blob"]).download_blob().readall()
            text = filing_to_text(raw.decode("utf-8", "ignore"))
        except Exception as exc:
            return {**filing, "error": type(exc).__name__}
        stated = fiscal_year_of(text)
        best = choose_best(extract_from_filing(text))
        return {**filing,
                "fy": stated[0] if stated else None,
                "value": best.value if best else None,
                "rule": best.source_rule if best else "none",
                "evidence": (best.evidence[:150] if best else "")}

    results = list(ThreadPoolExecutor(max_workers=12).map(run, batch))
    json.dump(results, open(f"/tmp/sc_report/batch_{BATCH}.json", "w"),
              indent=1, default=str)

    found = [r for r in results if r.get("value")]
    print(f"produced a value : {len(found)}/{len(results)}")
    print(f"no value         : {len(results) - len(found)}")

    # ── anomaly flags: what a human should look at ───────────────────────
    per_ticker = defaultdict(list)
    for row in found:
        per_ticker[row["ticker"]].append(row)

    print("\n── flagged for review ──")
    flags = 0
    for ticker, rows in sorted(per_ticker.items()):
        values = [r["value"] for r in rows]
        if len(values) >= 3:
            middle = statistics.median(values)
            for row in rows:
                if middle and abs(row["value"] - middle) / middle > 0.5:
                    flags += 1
                    print(f"  OUTLIER  {ticker:6} FY{row['fy']} = {row['value']:,} "
                          f"vs series median {middle:,.0f}   [{row['rule']}]")
        for row in rows:
            evidence = (row["evidence"] or "").lower()
            if row["value"] % 100 == 0 and row["value"] >= 1000:
                flags += 1
                print(f"  ROUND    {ticker:6} FY{row['fy']} = {row['value']:,} "
                      f"— suspiciously round   [{row['rule']}]")
            elif any(word in evidence[:70] for word in
                     ("domestic", "u.s.", "international", "company-operated",
                      "segment", "franchis")):
                flags += 1
                print(f"  SUBSET?  {ticker:6} FY{row['fy']} = {row['value']:,} "
                      f"— {row['evidence'][:70]}")
    if not flags:
        print("  none")

    print(f"\n{len(found)} values, {flags} flagged")
    print(f"WROTE /tmp/sc_report/batch_{BATCH}.json")

    # Compact per-ticker series so a human can eyeball continuity.
    print("\n── series by ticker ──")
    for ticker, rows in sorted(per_ticker.items()):
        series = {r["fy"]: r["value"] for r in sorted(rows, key=lambda r: r["fy"] or 0)}
        print(f"  {ticker:6} {series}")


if __name__ == "__main__":
    main()
