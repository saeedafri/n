#!/usr/bin/env python3
"""Harvest M&A parties + deal value from SEC merger FORM TYPES (free, official).

The nightly ETL derives ma_acquirer/ma_target by regex over filing prose. SEC
publishes both parties as STRUCTURED header blocks on transaction forms, each
with a CIK, and the deal value in the structured filing-fee exhibit. This script
reads those and writes a read-only overlay the app merges — no DB writes.

  SC TO-T  third-party tender offer   FILED BY = bidder, SUBJECT COMPANY = target
  425      merger communication       FILER    = acquirer, SUBJECT COMPANY = target
  S-4      merger registration        co-registrants (weaker; kept for value only)

Usage:
  python scripts/sec_ma_forms_sweep.py --from 2025Q1 --to 2026Q3
  python scripts/sec_ma_forms_sweep.py --from 2026Q1 --to 2026Q3 --forms "SC TO-T,425"
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from utils.ma_8k_extract import sanitize_ma_name, parse_ma_amount  # noqa: E402

UA = "Coresight Research mohdsaeedafri@coresight.com"
HDR = {"User-Agent": UA}
OVERLAY = os.path.join(os.path.dirname(__file__), "..", "app", "data",
                       "ma_event_overrides_secforms.json")
DEALS_CACHE = os.path.join(os.path.dirname(__file__), "..", "output",
                           "sec_ma_deals.json")


def fetch(url, byte_limit=None):
    req = urllib.request.Request(url, headers=dict(HDR))
    if byte_limit:                      # headers live in the first few KB
        req.add_header("Range", f"bytes=0-{byte_limit}")
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "replace")


def party_blocks(sgml, label):
    """Every {label} block in the submission header, as (name, cik)."""
    out = []
    pattern = rf"{label}:\s*\n\s*(?:COMPANY|OWNER) DATA:(.*?)(?=\n[A-Z][A-Z /-]*:\s*\n|\Z)"
    for m in re.finditer(pattern, sgml, re.S):
        name = re.search(r"COMPANY CONFORMED NAME:\s*([^\n]+)", m.group(1))
        cik = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", m.group(1))
        if name:
            out.append((name.group(1).strip(), (cik.group(1).lstrip("0") if cik else "")))
    return out


_VALUE_TAG = re.compile(r"<ffd:(?:MaximumAggregate|Transaction)Value[^>]*>\s*([\d.]+)", re.I)
_VALUE_TXT = re.compile(r"Transaction\s+Valuation[^$]{0,200}?\$\s*([\d,]+(?:\.\d+)?)", re.I)


def deal_value_usd_m(sgml):
    """Deal value in USD millions from the structured fee exhibit / fee table."""
    m = _VALUE_TAG.search(sgml)
    if m:
        return float(m.group(1)) / 1e6
    flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", sgml).replace("&nbsp;", " "))
    m = _VALUE_TXT.search(flat)
    if m:
        return float(m.group(1).replace(",", "")) / 1e6
    return None


def quarters(start, end):
    ys, qs = int(start[:4]), int(start[-1])
    ye, qe = int(end[:4]), int(end[-1])
    while (ys, qs) <= (ye, qe):
        yield ys, qs
        qs += 1
        if qs > 4:
            ys, qs = ys + 1, 1


def harvest(forms, q_from, q_to, workers, value_for):
    from edgar import set_identity, get_filings
    set_identity(UA)
    deals = []
    for form in forms:
        for year, qtr in quarters(q_from, q_to):
            try:
                filings = get_filings(form=form, year=year, quarter=qtr)
            except Exception as exc:
                print(f"  [{form} {year}Q{qtr}] list failed: {type(exc).__name__}")
                continue
            if filings is None or not len(filings):
                continue
            print(f"  [{form} {year}Q{qtr}] {len(filings)} filings")

            def one(idx):
                f = filings[idx]
                acc = f.accession_no
                base = (f"https://www.sec.gov/Archives/edgar/data/{f.cik}/"
                        f"{acc.replace('-', '')}/{acc}.txt")
                try:
                    head = fetch(base, byte_limit=12000)
                except Exception:
                    return None
                filer = party_blocks(head, "FILED BY") or party_blocks(head, "FILER")
                subject = party_blocks(head, "SUBJECT COMPANY")
                if not filer or not subject:
                    return None
                acq_name, acq_cik = filer[0]
                tgt_name, tgt_cik = subject[0]
                if acq_cik and acq_cik == tgt_cik:
                    return None            # self-tender / target's own response
                value = None
                if form in value_for:
                    try:                   # value needs the whole submission
                        value = deal_value_usd_m(fetch(base))
                    except Exception:
                        value = None
                return {"form": form, "accession": acc,
                        "filing_date": str(getattr(f, "filing_date", "")),
                        "acquirer": acq_name, "acquirer_cik": acq_cik,
                        "target": tgt_name, "target_cik": tgt_cik,
                        "value_usd_m": value}

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(one, i) for i in range(len(filings))]
                for fut in as_completed(futs):
                    rec = fut.result()
                    if rec:
                        deals.append(rec)
            print(f"      running total: {len(deals)} deals with two distinct parties")
    return deals


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="q_from", default="2025Q1")
    ap.add_argument("--to", dest="q_to", default="2026Q3")
    ap.add_argument("--forms", default="SC TO-T,425")
    ap.add_argument("--value-forms", default="SC TO-T",
                    help="forms worth downloading in full for the fee exhibit")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    forms = [f.strip() for f in args.forms.split(",") if f.strip()]
    value_for = {f.strip() for f in args.value_forms.split(",") if f.strip()}
    print(f"[sweep] forms={forms} {args.q_from}..{args.q_to}")
    deals = harvest(forms, args.q_from, args.q_to, args.workers, value_for)

    os.makedirs(os.path.dirname(DEALS_CACHE), exist_ok=True)
    with open(DEALS_CACHE, "w", encoding="utf-8") as fh:
        json.dump({"deals": deals}, fh, indent=1)
    withval = sum(1 for d in deals if d["value_usd_m"] is not None)
    print(f"[sweep] {len(deals)} deals ({withval} with a value) -> {DEALS_CACHE}")


if __name__ == "__main__":
    main()
