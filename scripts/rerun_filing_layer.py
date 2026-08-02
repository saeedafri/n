"""Re-run ONLY the filing parse (L2) across every filing, reusing L1 and L3.

The extractor changed; the XBRL facts and the model's answers did not. Re-reading
the filings costs nothing but time, so this recomputes l2_value in place and
leaves l1_value / l3_value exactly as the paid run produced them.

    .venv/bin/python scripts/rerun_filing_layer.py

No API calls. No spend.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from azure.storage.blob import BlobServiceClient

from data.store_count_extractor import (
    choose_best, extract_from_filing, filing_to_text, fiscal_year_of)
from data.store_count_sources import geography_from_filing, roll_up_continents

IN = "/tmp/sc_report/build_results.json"
OUT = "/tmp/sc_report/build_results.json"
BEFORE = "/tmp/sc_report/build_results_before_fix.json"

service = BlobServiceClient(
    f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
container = service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])


def main() -> None:
    rows = json.load(open(IN))
    if not os.path.exists(BEFORE):
        json.dump(rows, open(BEFORE, "w"), indent=1, default=str)
        print(f"kept a copy of the pre-fix results at {BEFORE}")

    changed = [0]
    started = time.time()

    def redo(row):
        blob = row.get("accession")
        if not blob or "/" not in str(blob):
            return row
        try:
            raw = container.get_blob_client(blob).download_blob().readall()
            text = filing_to_text(raw.decode("utf-8", "ignore"))
        except Exception:
            return row
        row["l2_before"] = row.get("l2_value")
        best = choose_best(extract_from_filing(text))
        row["l2_value"] = best.value if best else None
        row["evidence"] = (best.evidence[:400] if best else row.get("evidence", ""))
        row["source_rule"] = best.source_rule if best else row.get("source_rule")
        # The filing's own fiscal year, so a value is never filed under the
        # wrong period — the blob folder is the STORAGE year, which differs for
        # every December year-end company.
        stated = fiscal_year_of(text)
        if stated:
            row["filing_fiscal_year"] = stated[0]
            row["fye_date"] = stated[1]
        if row["l2_value"]:
            split = geography_from_filing(text, row["l2_value"])
            if split:
                row["by_country"] = split
                row["by_continent"] = roll_up_continents(split)
        if row["l2_value"] != row["l2_before"]:
            changed[0] += 1
        return row

    with ThreadPoolExecutor(max_workers=14) as pool:
        done = []
        for index, row in enumerate(pool.map(redo, rows), 1):
            done.append(row)
            if index % 500 == 0:
                print(f"  {index}/{len(rows)}  changed so far {changed[0]}", flush=True)

    json.dump(done, open(OUT, "w"), indent=1, default=str)

    produced_before = sum(1 for r in done if r.get("l2_before"))
    produced_now = sum(1 for r in done if r.get("l2_value"))
    year_moved = sum(1 for r in done if r.get("filing_fiscal_year")
                     and r["filing_fiscal_year"] != r["fiscal_year"])
    print(f"\nre-ran {len(done)} filings in {time.time() - started:.0f}s")
    print(f"  produced a value BEFORE : {produced_before}")
    print(f"  produced a value NOW    : {produced_now}")
    print(f"  values changed          : {changed[0]}")
    print(f"  filings whose stated fiscal year differs from the folder: {year_moved}")
    print(f"WROTE {OUT}")


if __name__ == "__main__":
    main()
