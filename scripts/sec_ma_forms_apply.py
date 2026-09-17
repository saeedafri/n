#!/usr/bin/env python3
"""Match swept SEC form deals onto coreiq_company_events and write the overlay.

Join key is the CIK, not the company name: the ticker is resolved to a CIK via
SEC's own company_tickers.json (free, official, ~10k symbols), and a deal matches
an event when the event's company is one of the two parties AND the filing date
is within --days of the event date. Name matching is what produces the current
mis-attribution, so it is deliberately not used.

Writes app/data/ma_event_overrides_secforms.json — FILL-ONLY: it never replaces
a value the event already has, and never blanks one. No DB writes.
"""
import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from utils.ma_8k_extract import sanitize_ma_name  # noqa: E402

UA = "Coresight Research mohdsaeedafri@coresight.com"
DEALS = os.path.join(os.path.dirname(__file__), "..", "output", "sec_ma_deals.json")
OVERLAY = os.path.join(os.path.dirname(__file__), "..", "app", "data",
                       "ma_event_overrides_secforms.json")


def ticker_to_cik():
    """SEC's official symbol -> CIK map (free, no key)."""
    req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = json.load(resp)
    return {row["ticker"].upper(): str(row["cik_str"]) for row in raw.values()}


def load_events(since):
    # db_manager wraps the SQL in SQLAlchemy text(): bind params are :name,
    # NOT pyformat — a %(name)s silently binds nothing and returns no rows.
    from core.database import db_manager
    return db_manager.execute_query_readonly("""
        SELECT event_id, ticker, event_date, ma_acquirer, ma_target,
               ma_transaction_value
        FROM coreiq_company_events
        WHERE is_active = 1 AND event_category = 'M&A Activity'
          AND event_date >= :since
    """, {"since": since})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30,
                    help="max gap between the filing date and the event date")
    ap.add_argument("--since", default="2025-01-01")
    args = ap.parse_args()

    with open(DEALS, encoding="utf-8") as fh:
        deals = json.load(fh)["deals"]
    print(f"[apply] {len(deals)} swept SEC deals")

    by_cik = defaultdict(list)
    for d in deals:
        if not d.get("filing_date"):
            continue
        for cik in (d.get("acquirer_cik"), d.get("target_cik")):
            if cik:
                by_cik[cik].append(d)
    print(f"[apply] indexed under {len(by_cik)} CIKs")

    symbols = ticker_to_cik()
    print(f"[apply] SEC ticker->CIK map: {len(symbols)} symbols")

    events = load_events(args.since)
    print(f"[apply] {len(events)} M&A events since {args.since}")

    overlay, stats = {}, defaultdict(int)
    for ev in events:
        cik = symbols.get((ev["ticker"] or "").upper())
        if not cik:
            stats["no_cik_for_ticker"] += 1
            continue
        stats["ticker_resolved"] += 1
        candidates = by_cik.get(cik)
        if not candidates:
            stats["no_sec_deal_for_company"] += 1
            continue
        ev_date = ev["event_date"]
        if not isinstance(ev_date, date):
            continue
        best, best_gap = None, None
        for d in candidates:
            try:
                fdate = date.fromisoformat(d["filing_date"][:10])
            except Exception:
                continue
            gap = abs((fdate - ev_date).days)
            if gap <= args.days and (best_gap is None or gap < best_gap):
                best, best_gap = d, gap
        if not best:
            stats["no_deal_in_date_window"] += 1
            continue
        stats["matched"] += 1

        entry = {}
        if not sanitize_ma_name(ev["ma_acquirer"]):
            entry["acquirer"] = best["acquirer"]
            stats["filled_acquirer"] += 1
        if not sanitize_ma_name(ev["ma_target"]):
            entry["target"] = best["target"]
            stats["filled_target"] += 1
        if best.get("value_usd_m") is not None and not (ev["ma_transaction_value"] or "").strip():
            entry["transaction_value_usd_m"] = round(best["value_usd_m"], 2)
            stats["filled_value"] += 1
        if entry:
            entry.update(source=f"SEC {best['form']}", accession=best["accession"],
                         acquirer_cik=best.get("acquirer_cik"),
                         target_cik=best.get("target_cik"), date_gap_days=best_gap)
            overlay[str(ev["event_id"])] = entry

    with open(OVERLAY, "w", encoding="utf-8") as fh:
        json.dump({"source": "SEC merger form types (header parties + fee exhibit)",
                   "events": overlay}, fh, ensure_ascii=False, indent=1)
    print(f"\n[apply] overlay rows: {len(overlay)} -> {OVERLAY}")
    for k in sorted(stats):
        print(f"    {k:28} {stats[k]}")


if __name__ == "__main__":
    main()
