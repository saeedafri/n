"""Score the FREE layers (L1 XBRL + L2 filing parse) before any LLM spend.

Ground truth: the 596 store_count rows in coreiq_filing_metrics_v5 that pass the
source-sentence gate AND were confirmed present in their own filing, plus the 35
rows confirmed ABSENT from their filing (which a good extractor must NOT
reproduce).

Usage:  .venv/bin/python scripts/score_store_counts.py [sample_size]
Cost:   $0.00
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
os.environ.setdefault("APP_ENV", "staging")

import pymysql
from azure.storage.blob import BlobServiceClient

from data.store_count_extractor import choose_best, extract_from_filing
from data.store_count_sources import load_filing_text, xbrl_by_year, xbrl_rows_for

SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 0


def db_connect():
    return pymysql.connect(
        host=os.environ["STG_DB_HOST"], port=int(os.environ.get("STG_DB_PORT", 3306)),
        user=os.environ["STG_DB_USER"], password=os.environ["STG_DB_PASSWORD"],
        database=os.environ["STG_DB_NAME"], ssl={"ssl": {}},
        cursorclass=pymysql.cursors.DictCursor, read_timeout=300)


blob_service = BlobServiceClient(
    f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
container = blob_service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])

# Rows whose value is NOT in its own filing — measured earlier. A correct
# extractor should disagree with these.
KNOWN_BAD = {
    ("AAP", 2023), ("ARKO", 2025), ("ARW", 2022), ("BLD", 2023), ("BLD", 2025),
    ("COLM", 2023), ("KSS", 2020), ("KSS", 2021), ("LEVI", 2021), ("LEVI", 2023),
    ("LULU", 2022), ("LULU", 2023), ("O", 2023), ("PAG", 2019), ("PAG", 2024),
    ("RCKY", 2025), ("RH", 2024), ("RH", 2026), ("SAH", 2022), ("SAH", 2023),
    ("SHW", 2020), ("SHW", 2021), ("SHW", 2023), ("SHW", 2024), ("TJX", 2022),
    ("TJX", 2023), ("TJX", 2024), ("TJX", 2025), ("TJX", 2026), ("UPBD", 2019),
    ("UPBD", 2022), ("VRA", 2025), ("WMT", 2020), ("WMT", 2022), ("WMT", 2023),
}


def main() -> None:
    connection = db_connect()
    cursor = connection.cursor()
    cursor.execute("""
        SELECT ticker, report_fiscal_year AS fy, numeric_value, archive_blob,
               filing_accession, llm_query
        FROM coreiq_filing_metrics_v5
        WHERE source = 'store_count' AND archive_blob IS NOT NULL
        ORDER BY ticker, report_fiscal_year
    """)
    rows = cursor.fetchall()

    xbrl_cache: dict = {}
    for ticker in sorted({r["ticker"] for r in rows}):
        xbrl_cache[ticker] = xbrl_by_year(xbrl_rows_for(cursor, ticker))
    connection.close()

    truth = [r for r in rows if (r["ticker"], r["fy"]) not in KNOWN_BAD]
    bad = [r for r in rows if (r["ticker"], r["fy"]) in KNOWN_BAD]
    if SAMPLE:
        truth = truth[:SAMPLE]

    print(f"ground truth rows: {len(truth)}   known-bad rows: {len(bad)}")
    print(f"tickers with L1 xbrl: {sum(1 for v in xbrl_cache.values() if v)}"
          f" / {len(xbrl_cache)}\n")

    def evaluate(row):
        ticker, year = row["ticker"], int(row["fy"])
        expected = int(row["numeric_value"])
        l1 = xbrl_cache.get(ticker, {}).get(year)
        l1_value = l1.value if l1 else None
        l2_value, rule = None, "none"
        try:
            text = load_filing_text(container, row["archive_blob"])
            best = choose_best(extract_from_filing(text))
            if best:
                l2_value, rule = best.value, best.source_rule
        except Exception as exc:
            rule = f"error:{type(exc).__name__}"
        return ticker, year, expected, l1_value, l2_value, rule

    results = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for i, outcome in enumerate(pool.map(evaluate, truth), 1):
            results.append(outcome)
            if i % 50 == 0:
                print(f"  {i}/{len(truth)}", flush=True)

    l1_hit = sum(1 for *_, exp, v1, _, _ in
                 ((r[0], r[1], r[2], r[3], r[4], r[5]) for r in results) if v1 == exp)
    l1_any = sum(1 for r in results if r[3] is not None)
    l2_hit = sum(1 for r in results if r[4] == r[2])
    l2_any = sum(1 for r in results if r[4] is not None)
    either = sum(1 for r in results if r[3] == r[2] or r[4] == r[2])
    neither = [r for r in results if r[3] != r[2] and r[4] != r[2]]

    total = len(results)
    print(f"\n{'':22} {'produced':>9} {'exact match':>12}")
    print(f"{'L1 xbrl (our DB)':22} {l1_any:>9} {l1_hit:>12}  ({l1_hit/total*100:.0f}%)")
    print(f"{'L2 filing parse':22} {l2_any:>9} {l2_hit:>12}  ({l2_hit/total*100:.0f}%)")
    print(f"{'L1 or L2 correct':22} {'':>9} {either:>12}  ({either/total*100:.0f}%)")
    print(f"{'-> residue for LLM':22} {'':>9} {len(neither):>12}  ({len(neither)/total*100:.0f}%)")

    print("\nfirst 25 residue rows (what the LLM would be asked):")
    for ticker, year, expected, v1, v2, rule in neither[:25]:
        print(f"   {ticker:6} FY{year}  expected={expected:>7,}  L1={v1}  L2={v2} ({rule})")

    print("\n--- known-bad rows: does the extractor avoid reproducing them? ---")
    avoided = 0
    for row in bad:
        ticker, year = row["ticker"], int(row["fy"])
        wrong = int(row["numeric_value"])
        l1 = xbrl_cache.get(ticker, {}).get(year)
        try:
            text = load_filing_text(container, row["archive_blob"])
            best = choose_best(extract_from_filing(text))
            got = best.value if best else None
        except Exception:
            got = None
        l1_value = l1.value if l1 else None
        picked = l1_value if l1_value is not None else got
        ok = picked != wrong
        avoided += ok
        print(f"   {ticker:6} FY{year} db_wrong={wrong:>7,} extractor={picked}"
              f"  {'OK' if ok else 'REPRODUCED BAD VALUE'}")
    print(f"\navoided {avoided}/{len(bad)} known-bad values")


if __name__ == "__main__":
    main()
