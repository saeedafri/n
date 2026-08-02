"""Run the store-count ladder over filings and cache the results.

    L1 XBRL already in our DB  ->  L2 the filing itself  ->  L3 gpt-4o-mini

L3 is reached only when both free layers come up empty or disagree, and the run
aborts the moment spend would exceed --budget. Nothing is ever silently
truncated: what was skipped, and why, is printed at the end.

    .venv/bin/python scripts/build_store_counts.py --budget 0.50 --ground-truth
    .venv/bin/python scripts/build_store_counts.py --budget 2.00 --all

Cached by SEC accession, so a filing is only ever paid for once.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
os.environ.setdefault("APP_ENV", "staging")

import pymysql
from azure.storage.blob import BlobServiceClient
from openai import OpenAI

from data import store_count_cache as cache
from data.store_count_extractor import (
    NON_RETAIL_TICKERS, StoreCount, choose_best, extract_from_filing,
    filing_to_text, fiscal_year_of, looks_plausible, value_supported_by,
)
from data.store_count_sources import (
    ask_llm, geography_from_filing, llm_call_cost, llm_windows,
    roll_up_continents, xbrl_by_year, xbrl_rows_for,
)


class Budget:
    """Hard spend ceiling. Reserves before a call so parallel workers cannot
    race past the limit, and refunds the difference once actual usage is known."""

    def __init__(self, ceiling: float):
        self.ceiling = ceiling
        self.spent = 0.0
        self.calls = 0
        self.refused = 0
        self._lock = threading.Lock()

    ESTIMATE = 0.0004  # a windowed call, rounded up

    def reserve(self) -> bool:
        with self._lock:
            if self.spent + self.ESTIMATE > self.ceiling:
                self.refused += 1
                return False
            self.spent += self.ESTIMATE
            self.calls += 1
            return True

    def settle(self, actual: float) -> None:
        with self._lock:
            self.spent += actual - self.ESTIMATE


def db_connect():
    return pymysql.connect(
        host=os.environ["STG_DB_HOST"], port=int(os.environ.get("STG_DB_PORT", 3306)),
        user=os.environ["STG_DB_USER"], password=os.environ["STG_DB_PASSWORD"],
        database=os.environ["STG_DB_NAME"], ssl={"ssl": {}},
        cursorclass=pymysql.cursors.DictCursor, read_timeout=300)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=float, default=0.50,
                        help="hard USD ceiling for LLM spend")
    parser.add_argument("--ground-truth", action="store_true",
                        help="only filings that already have a store_count row")
    parser.add_argument("--all", action="store_true", help="every 10-K we hold")
    parser.add_argument("--tickers", default="", help="comma separated subset")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--no-llm", action="store_true", help="free layers only")
    args = parser.parse_args()

    budget = Budget(args.budget)
    blob_service = BlobServiceClient(
        f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
        credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
    container = blob_service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    connection = db_connect()
    cursor = connection.cursor()

    wanted = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    if args.all:
        # Every 10-K in the archive, not only the ones the old pipeline
        # happened to touch. The fiscal year comes from the filing's own cover
        # page, so companies with no DB row at all are handled identically.
        inventory = json.load(open("/tmp/sc_report/inventory.json"))
        if wanted:
            inventory = [f for f in inventory if f["ticker"].upper() in wanted]
        targets = [{"ticker": f["ticker"], "fy": None, "db_value": None,
                    "archive_blob": f["blob"], "filing_accession": f["blob"],
                    "period_end": None, "filing_date": None}
                   for f in inventory]
        cursor.execute("""SELECT ticker, report_fiscal_year AS fy,
                                 numeric_value AS db_value
                          FROM coreiq_filing_metrics_v5
                          WHERE source = 'store_count'""")
        known = {(r["ticker"], int(r["fy"])): int(r["db_value"])
                 for r in cursor.fetchall() if r["db_value"]}
    else:
        where = ["source = 'store_count'", "archive_blob IS NOT NULL"]
        params: List[Any] = []
        if wanted:
            where.append("ticker IN (" + ",".join(["%s"] * len(wanted)) + ")")
            params += wanted
        cursor.execute(f"""
            SELECT ticker, report_fiscal_year AS fy, numeric_value AS db_value,
                   archive_blob, filing_accession, period_end, filing_date
            FROM coreiq_filing_metrics_v5
            WHERE {' AND '.join(where)}
            ORDER BY ticker, report_fiscal_year
        """, params)
        targets = cursor.fetchall()
        known = {}

    # Prefetch the XBRL layer per ticker. Done in parallel with its own
    # connections and a progress line: sequentially this is 500+ round trips to
    # Azure MySQL and the run looks hung for half an hour with nothing on stdout.
    all_tickers = sorted({row["ticker"] for row in targets})
    xbrl: Dict[str, Dict[int, StoreCount]] = {}
    xbrl_lock = threading.Lock()
    done = [0]

    def load_xbrl(ticker: str) -> None:
        own = db_connect()
        try:
            with own.cursor() as own_cursor:
                found = xbrl_by_year(xbrl_rows_for(own_cursor, ticker))
        except Exception:
            found = {}
        finally:
            own.close()
        with xbrl_lock:
            xbrl[ticker] = found
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"  xbrl prefetch {done[0]}/{len(all_tickers)}", flush=True)

    with ThreadPoolExecutor(max_workers=8) as prefetch_pool:
        list(prefetch_pool.map(load_xbrl, all_tickers))
    connection.close()

    print(f"targets: {len(targets)} filings, {len(xbrl)} tickers")
    print(f"budget:  ${budget.ceiling:.2f}\n")

    skipped_non_retail: List[str] = []
    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def process(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ticker = row["ticker"]
        accession = row.get("filing_accession") or row["archive_blob"]

        # Non-retail tickers are still RECORDED, as a tombstone carrying no
        # value. Returning early left 319 filings with no cache entry at all,
        # so every later pass treated them as new work and re-read them
        # forever. "We looked and there is nothing here" is a result.
        if ticker.upper() in NON_RETAIL_TICKERS:
            with lock:
                skipped_non_retail.append(ticker)
            year = int(row["fy"]) if row.get("fy") else None
            record = {"ticker": ticker, "fiscal_year": year, "value": None,
                      "accession": f"{accession}::{year}", "db_value": None,
                      "l1_value": None, "l2_value": None, "l3_value": None,
                      "confidence": "none", "by_country": {}, "by_continent": {},
                      "basis": "not a store operator", "needs_review": False}
            if year is not None:
                cache.write(record["accession"], record)
            return record

        year = int(row["fy"]) if row.get("fy") else None
        if year is not None:
            cached = cache.read(f"{accession}::{year}")
            if cached:
                return cached

        text = ""
        try:
            raw = container.get_blob_client(row["archive_blob"]).download_blob().readall()
            text = filing_to_text(raw.decode("utf-8", "ignore"))
        except Exception:
            text = ""

        fye = None
        if year is None:
            stated = fiscal_year_of(text) if text else None
            if stated:
                year, fye = stated
            else:
                # The filing does not spell out "fiscal year ended", which is
                # why 91 filings — IBM, Kodak, PVH, Yum among them — were
                # dropped entirely rather than reported as empty. The blob
                # folder names the year the filing was archived under, which is
                # good enough to place it on a timeline and far better than
                # pretending the filing does not exist.
                folder = str(row["archive_blob"]).split("/")
                year = next((int(part) for part in folder
                             if part.isdigit() and 1990 < int(part) < 2100), None)
                if year is None:
                    return None
            cached = cache.read(f"{accession}::{year}")
            if cached:
                return cached

        record: Dict[str, Any] = {
            "ticker": ticker, "fiscal_year": year, "accession": accession,
            "fye_date": fye or str(row.get("period_end") or ""),
            "db_value": (int(row["db_value"]) if row.get("db_value")
                         else known.get((ticker, year))),
            "l1_value": None, "l2_value": None, "l3_value": None,
            "value": None, "source_rule": "none", "confidence": "none",
            "evidence": "", "by_country": {}, "by_continent": {},
            "llm_cost": 0.0,
        }

        found = xbrl.get(ticker, {}).get(year)
        if found and found.value:
            record.update(l1_value=found.value, value=found.value,
                          source_rule=found.source_rule, confidence="high",
                          evidence=found.evidence, by_country=found.by_geography)

        if text:
            best = choose_best(extract_from_filing(text))
            if best and best.value:
                record["l2_value"] = best.value
                if record["value"] is None:
                    record.update(value=best.value, source_rule=best.source_rule,
                                  confidence="medium", evidence=best.evidence)

            # Country split, only when it reconciles to the chosen total.
            if record["value"] and not record["by_country"]:
                record["by_country"] = geography_from_filing(text, record["value"])

        # L3 only when the free layers produced nothing, or produced two
        # different answers and need a casting vote.
        needs_llm = (record["value"] is None
                     or (record["l1_value"] and record["l2_value"]
                         and record["l1_value"] != record["l2_value"]))
        if needs_llm and text and not args.no_llm:
            if budget.reserve():
                try:
                    parsed, prompt_tokens, completion_tokens = ask_llm(
                        openai_client, ticker, year,
                        record.get("fye_date") or "", llm_windows(text))
                    spend = llm_call_cost(prompt_tokens, completion_tokens)
                    budget.settle(spend)
                    record["llm_cost"] = spend
                    # A self-declared subset is refused outright. The model is
                    # asked to state its scope precisely so this is checkable
                    # rather than a matter of trust.
                    if parsed and parsed.get("scope") not in (None, "worldwide"):
                        record["evidence"] += (
                            f" | llm reported {parsed.get('scope')} scope, refused")
                        parsed = None
                    if parsed and parsed.get("found") and parsed.get("value"):
                        proposed = int(parsed["value"])
                        quote = str(parsed.get("quote") or "")
                        # The model must point at text that carries its number.
                        if value_supported_by(proposed, quote) or str(proposed) in text \
                                or f"{proposed:,}" in text:
                            record["l3_value"] = proposed
                            if record["value"] is None:
                                record.update(value=proposed, source_rule="L3_llm",
                                              confidence="medium", evidence=quote[:400])
                        else:
                            record["evidence"] += " | llm value unsupported, rejected"
                except Exception as exc:
                    budget.settle(0.0)
                    record["evidence"] += f" | llm error {type(exc).__name__}"

        # Two independent sources agreeing is the strongest signal available.
        agree = [v for v in (record["l1_value"], record["l2_value"], record["l3_value"])
                 if v is not None]
        if len(agree) >= 2 and len(set(agree)) == 1:
            record["confidence"] = "high"

        if record["by_country"]:
            record["by_continent"] = roll_up_continents(record["by_country"])

        if record["value"]:
            cache.write(f"{accession}::{year}", record)
        return record

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index, outcome in enumerate(pool.map(process, targets), 1):
            if outcome:
                results.append(outcome)
            if index % 100 == 0:
                print(f"  {index}/{len(targets)}  spend ${budget.spent:.4f}", flush=True)

    cache.rebuild_index()

    # Write BEFORE reporting. An hour of parsing was lost to a KeyError in the
    # summary block below, because the results only reached disk after it.
    with open("/tmp/sc_report/build_results.json", "w") as handle:
        json.dump(results, handle, indent=1, default=str)
    print("wrote /tmp/sc_report/build_results.json")

    got = [r for r in results if r["value"]]
    matched = [r for r in got if r["db_value"] and r["value"] == r["db_value"]]
    print(f"\nprocessed {len(results)} filings in {time.time() - started:.0f}s")
    print(f"  produced a value       : {len(got)}")
    print(f"  agrees with DB         : {len(matched)}")
    for layer in ("l1_value", "l2_value", "l3_value"):
        print(f"  {layer:22} : {sum(1 for r in got if r.get(layer))}")
    print(f"  with country breakdown : {sum(1 for r in got if r.get('by_country'))}")
    print(f"  skipped non-retail     : {len(skipped_non_retail)}")
    print(f"\nLLM calls {budget.calls}   spend ${budget.spent:.4f} of ${budget.ceiling:.2f}")
    if budget.refused:
        print(f"  !! {budget.refused} calls REFUSED — budget ceiling reached, "
              f"those rows have no L3 opinion")


if __name__ == "__main__":
    main()
