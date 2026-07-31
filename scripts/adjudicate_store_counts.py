"""Settle every DB-vs-extractor disagreement against the filing itself.

For each row where our value differs from the DB's, re-read the source filing
and ask one factual question of each number: does it appear in this document?

    DB absent, ours present   -> we are right, the DB is wrong
    DB present, ours absent   -> we are wrong
    both present              -> ambiguous, a human should look
    neither                   -> both wrong

No model involved and no cost — this is string matching against the primary
source, which is why it can be trusted to grade the model's own output.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import pymysql
from azure.storage.blob import BlobServiceClient

from data.store_count_extractor import filing_to_text

RESULTS = "/tmp/sc_report/build_results.json"
OUT = "/tmp/sc_report/adjudicated.json"

blob_service = BlobServiceClient(
    f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
container = blob_service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])


def present(value, text: str) -> bool:
    if value is None:
        return False
    value = int(value)
    return bool(re.search(rf"(?<![\d,]){value:,}(?![\d,])", text)
                or re.search(rf"(?<![\d,]){value}(?![\d,])", text))


def main() -> None:
    rows = json.load(open(RESULTS))

    connection = pymysql.connect(
        host=os.environ["STG_DB_HOST"], port=int(os.environ.get("STG_DB_PORT", 3306)),
        user=os.environ["STG_DB_USER"], password=os.environ["STG_DB_PASSWORD"],
        database=os.environ["STG_DB_NAME"], ssl={"ssl": {}},
        cursorclass=pymysql.cursors.DictCursor, read_timeout=300)
    cursor = connection.cursor()
    cursor.execute("""SELECT ticker, report_fiscal_year AS fy, archive_blob
                      FROM coreiq_filing_metrics_v5
                      WHERE source='store_count' AND archive_blob IS NOT NULL""")
    blob_for = {(r["ticker"], int(r["fy"])): r["archive_blob"] for r in cursor.fetchall()}
    connection.close()

    disputed = [r for r in rows
                if r["value"] and r["db_value"] and r["value"] != r["db_value"]]
    print(f"disputed rows: {len(disputed)}")

    def judge(row):
        blob = blob_for.get((row["ticker"], row["fiscal_year"]))
        if not blob:
            row["verdict"] = "no filing"
            return row
        try:
            text = filing_to_text(
                container.get_blob_client(blob).download_blob().readall()
                .decode("utf-8", "ignore"))
        except Exception:
            row["verdict"] = "filing unreadable"
            return row
        ours = present(row["value"], text)
        theirs = present(row["db_value"], text)
        row["ours_in_filing"] = ours
        row["db_in_filing"] = theirs
        row["verdict"] = ("WE ARE RIGHT — DB value absent from filing" if ours and not theirs
                          else "WE ARE WRONG — our value absent" if theirs and not ours
                          else "both appear — needs a human" if ours and theirs
                          else "neither appears")
        return row

    judged = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for index, row in enumerate(pool.map(judge, disputed), 1):
            judged.append(row)
            if index % 100 == 0:
                print(f"  {index}/{len(disputed)}", flush=True)

    from collections import Counter
    tally = Counter(r["verdict"] for r in judged)
    print()
    for verdict, count in tally.most_common():
        print(f"  {verdict:48} {count}")

    json.dump(judged, open(OUT, "w"), indent=1, default=str)
    print(f"\nWROTE {OUT}")


if __name__ == "__main__":
    main()
