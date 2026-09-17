#!/usr/bin/env python3
"""Resolve every ticker that has M&A events to an SEC CIK.

SEC's static company_tickers.json holds only 10,422 symbols and is missing major
US filers (AvalonBay, Equity Residential, Denny's, Sleep Number...). EDGAR's own
company lookup resolves them, so this does bulk-file first and falls back to the
lookup per unresolved ticker. Result is cached to app/data/sec_ticker_cik.json so
it is paid once, not per run.
"""
import json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
UA = "Coresight Research mohdsaeedafri@coresight.com"
OUT = os.path.join(os.path.dirname(__file__), "..", "app", "data", "sec_ticker_cik.json")


def fetch(url, as_json=False, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r) if as_json else r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.2 * (i + 1))
    return None


def lookup_one(ticker):
    """EDGAR company lookup — authoritative where the bulk file is silent."""
    x = fetch("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&ticker="
              f"{urllib.parse.quote(ticker)}&type=8-K&dateb=&owner=include&count=1&output=atom")
    if not x:
        return ticker, None
    m = re.search(r"CIK=(\d+)", x) or re.search(r"<cik>(\d+)</cik>", x)
    return ticker, (int(m.group(1)) if m else None)


import urllib.parse  # noqa: E402  (used by lookup_one)


def main():
    from core.database import db_manager
    tickers = [r["ticker"] for r in db_manager.execute_query_readonly("""
        SELECT DISTINCT ticker FROM coreiq_company_events
        WHERE is_active=1 AND event_category='M&A Activity' AND ticker IS NOT NULL""")]
    print(f"[map] tickers with M&A events: {len(tickers)}", flush=True)

    bulk = fetch("https://www.sec.gov/files/company_tickers.json", as_json=True) or {}
    by_symbol = {r["ticker"].upper(): int(r["cik_str"]) for r in bulk.values()}
    print(f"[map] bulk file: {len(by_symbol)} symbols", flush=True)

    cache = {}
    if os.path.exists(OUT):
        cache = json.load(open(OUT)).get("tickers", {})
        print(f"[map] cached from earlier run: {len(cache)}", flush=True)

    resolved = {}
    todo = []
    for t in tickers:
        if t in cache:
            if cache[t]:
                resolved[t] = cache[t]
            continue
        cik = by_symbol.get((t or "").upper())
        if cik:
            resolved[t] = cik
        else:
            todo.append(t)
    print(f"[map] from bulk: {len(resolved)}   needing EDGAR lookup: {len(todo)}", flush=True)

    found = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(lookup_one, t) for t in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            t, cik = fut.result()
            cache[t] = cik
            if cik:
                resolved[t] = cik
                found += 1
            if i % 200 == 0:
                print(f"  looked up {i}/{len(todo)}, {found} resolved", flush=True)
                json.dump({"tickers": cache}, open(OUT, "w"))

    for t, c in resolved.items():
        cache.setdefault(t, c)
    json.dump({"tickers": cache}, open(OUT, "w"))
    print(f"\n[map] RESOLVED {len(resolved)} / {len(tickers)} tickers "
          f"({100*len(resolved)/len(tickers):.1f}%)")
    print(f"[map] still unresolved: {len(tickers) - len(resolved)}")
    print(f"[map] -> {OUT}")


if __name__ == "__main__":
    main()
