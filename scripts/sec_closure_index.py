#!/usr/bin/env python3
"""Build a local SEC deal-closure index for every company that has M&A events.

Closure is read from SEC structure, never from prose:
  * 8-K with Item 2.01  — "Completion of Acquisition or Disposition of Assets"
  * Form 25 / 25-NSE    — the exchange delists a target (only happens post-close)
  * Form 15 (15-12B/G)  — the target deregisters (only happens post-close)

`items` is a DECLARED field on data.sec.gov/submissions, so no filing is ever
downloaded: one JSON per company. Verified 98.2% against opening the filings.

Output: app/data/sec_closure_index.json — the app reads this local file and never
calls SEC at render time. No DB writes.

Usage:
  python scripts/sec_closure_index.py                 # all tickers with M&A events
  python scripts/sec_closure_index.py --limit 200     # smoke test
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

UA = "Coresight Research mohdsaeedafri@coresight.com"
OUT = os.path.join(os.path.dirname(__file__), "..", "app", "data", "sec_closure_index.json")
DELIST_FORMS = ("25", "25-NSE", "15-12B", "15-12G", "15F-12B", "15F-12G")


def get_json(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except Exception:
            if attempt == tries - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


CIK_MAP = os.path.join(os.path.dirname(__file__), "..", "app", "data", "sec_ticker_cik.json")


def ticker_to_cik():
    """Bulk file PLUS the resolved map. SEC's company_tickers.json holds only
    ~10.4k symbols and omits major US filers (AvalonBay, Equity Residential,
    Denny's...), so scripts/sec_ticker_cik_map.py resolves the rest via EDGAR's
    own lookup and caches them here."""
    raw = get_json("https://www.sec.gov/files/company_tickers.json") or {}
    mapping = {r["ticker"].upper(): int(r["cik_str"]) for r in raw.values()}
    if os.path.exists(CIK_MAP):
        with open(CIK_MAP, "r", encoding="utf-8") as fh:
            for ticker, cik in (json.load(fh).get("tickers") or {}).items():
                if cik:
                    mapping[ticker.upper()] = int(cik)
    return mapping


def tickers_with_ma():
    from core.database import db_manager
    rows = db_manager.execute_query_readonly("""
        SELECT DISTINCT ticker FROM coreiq_company_events
        WHERE is_active = 1 AND event_category = 'M&A Activity' AND ticker IS NOT NULL
    """)
    return [r["ticker"] for r in rows]


def closure_for(cik, max_pages=3):
    """Every Item 2.01 8-K and every delist/deregister filing for one company."""
    data = get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if not data:
        return None
    blocks = [data["filings"]["recent"]]
    for extra in (data["filings"].get("files") or [])[:max_pages]:
        page = get_json("https://data.sec.gov/submissions/" + extra["name"])
        if page:
            blocks.append(page)
        time.sleep(0.1)

    item201, delistings = [], []
    for b in blocks:
        forms = b.get("form", [])
        dates = b.get("filingDate", [])
        items = b.get("items", [])
        accs = b.get("accessionNumber", [])
        for i, form in enumerate(forms):
            date = dates[i] if i < len(dates) else None
            if form == "8-K" and i < len(items) and "2.01" in (items[i] or ""):
                item201.append({"date": date, "accession": accs[i] if i < len(accs) else None,
                                "items": items[i]})
            elif form in DELIST_FORMS:
                delistings.append({"date": date, "form": form})
    return {
        "cik": cik,
        "exchanges": data.get("exchanges") or [],
        "item201": sorted(item201, key=lambda x: x["date"] or "", reverse=True)[:40],
        "delistings": sorted(delistings, key=lambda x: x["date"] or "", reverse=True)[:6],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    symbols = ticker_to_cik()
    print(f"[closure] SEC ticker->CIK map: {len(symbols)} symbols", flush=True)
    tickers = tickers_with_ma()
    print(f"[closure] tickers with M&A events: {len(tickers)}", flush=True)

    wanted = {}
    unresolved = []
    for t in tickers:
        cik = symbols.get((t or "").upper())
        (wanted.setdefault(cik, []).append(t) if cik else unresolved.append(t))
    # resume-friendly: keep whatever a previous sweep already indexed
    if os.path.exists(OUT):
        try:
            with open(OUT, "r", encoding="utf-8") as fh:
                prior = json.load(fh).get("companies", {}) or {}
        except Exception:
            prior = {}
    else:
        prior = {}
    print(f"[closure] resolved to {len(wanted)} distinct CIKs; "
          f"{len(unresolved)} tickers have no SEC CIK", flush=True)

    ciks = list(wanted)
    if args.limit:
        ciks = ciks[: args.limit]

    index, done, failed = dict(prior), 0, 0
    ciks = [c for c in ciks if not all(t in prior for t in wanted[c])]
    print(f'[closure] {len(ciks)} companies still to fetch', flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(closure_for, c): c for c in ciks}
        for fut in as_completed(futs):
            cik = futs[fut]
            res = fut.result()
            done += 1
            if res is None:
                failed += 1
            else:
                for t in wanted[cik]:
                    index[t] = res
            if done % 250 == 0:
                print(f"  {done}/{len(ciks)} companies ({failed} failed)", flush=True)
                with open(OUT, "w", encoding="utf-8") as fh:
                    json.dump({"companies": index}, fh)

    with_201 = sum(1 for v in index.values() if v["item201"])
    with_delist = sum(1 for v in index.values() if v["delistings"])
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"source": "SEC submissions API (declared 8-K items + delist forms)",
                   "companies": index}, fh)
    print(f"\n[closure] {len(index)} tickers indexed, {failed} companies failed")
    print(f"[closure] with >=1 Item 2.01 8-K : {with_201}")
    print(f"[closure] with delist/deregister : {with_delist}")
    print(f"[closure] -> {OUT}")


if __name__ == "__main__":
    main()
