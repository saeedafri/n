#!/usr/bin/env python3
"""Re-extract M&A acquirer/target/value from source 8-K filings (edgartools).

The nightly key-developments enrichment populated coreiq_company_events.ma_*
with naive regex captures over raw 8-K text — producing values like
ma_acquirer='reference' (from "by reference") and sentence-fragment targets.
This script repairs the 262 calendar-surfaced 'M&A Closing' rows:

  harvest         — pull rows from STG, fetch each source 8-K via edgartools,
                    extract Item 1.01/2.01 section text → JSONL chunks for LLM pass
  emit-overrides  — read extraction-result JSONL, validate, write the app's
                    read-only overlay app/data/ma_event_overrides.json
  apply           — DATA TEAM ONLY: validate + UPDATE the DB rows in place.
                    The app team does not write to the DB; hand this script and
                    the extraction JSONL to the data team.

The LLM extraction pass between harvest and emit-overrides is run externally
(Claude subagents over the harvest chunks); this keeps the script
key-independent.

Usage:
  python scripts/enrich_ma_events_v2.py harvest --out DIR [--chunks N]
  python scripts/enrich_ma_events_v2.py emit-overrides --results GLOB --out FILE
  python scripts/enrich_ma_events_v2.py apply --results GLOB [--dry-run]
"""
import argparse
import glob as globlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import pymysql

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from utils.ma_8k_extract import extract_item_sections, sanitize_ma_name  # noqa: E402

CANON_DEAL_TYPES = {
    "merger": "merger", "acquisition": "acquisition",
    "asset purchase": "asset purchase", "asset sale": "asset purchase",
    "divestiture": "divestiture", "spin-off": "spin-off",
    "spinoff": "spin-off", "other": "other",
}


def _load_env():
    root = os.path.join(os.path.dirname(__file__), "..")
    env_path = os.path.join(root, ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _conn():
    return pymysql.connect(
        host=os.environ["STG_DB_HOST"], port=int(os.environ.get("STG_DB_PORT", 3306)),
        user=os.environ["STG_DB_USER"], password=os.environ["STG_DB_PASSWORD"],
        database=os.environ["STG_DB_NAME"], ssl={"ssl": {}}, autocommit=True,
        cursorclass=pymysql.cursors.DictCursor)


CALENDAR_WHERE = """
    event_category = 'M&A Activity'
    AND event_subtype = 'M&A Closing'
    AND is_active = 1
    AND ma_is_closed = 1
    AND (ma_deal_type IS NULL OR ma_deal_type <> 'spin-off')
    AND event_date IS NOT NULL
"""


def _accession_from_url(url):
    m = re.search(r"/(\d{18})/", url or "")
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"


_extract_item_sections = extract_item_sections


def cmd_harvest(args):
    from edgar import set_identity, find
    set_identity("Coresight Research mohdsaeedafri@coresight.com")

    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT event_id, ticker, event_date, headline, source, source_ref,
               ma_acquirer, ma_target, ma_deal_type, ma_transaction_value_usd_m,
               ma_announce_date, ma_close_date
        FROM coreiq_company_events
        WHERE {CALENDAR_WHERE}
        ORDER BY event_id
    """)
    rows = cur.fetchall()
    conn.close()
    print(f"[harvest] {len(rows)} calendar M&A rows")

    os.makedirs(args.out, exist_ok=True)

    def fetch(row):
        acc = _accession_from_url(row["source_ref"])
        rec = {
            "event_id": row["event_id"],
            "ticker": row["ticker"],
            "event_date": str(row["event_date"]),
            "accession": acc,
            "existing": {
                "ma_acquirer": row["ma_acquirer"],
                "ma_target": row["ma_target"],
                "ma_deal_type": row["ma_deal_type"],
                "ma_transaction_value_usd_m":
                    float(row["ma_transaction_value_usd_m"])
                    if row["ma_transaction_value_usd_m"] is not None else None,
                "ma_announce_date": str(row["ma_announce_date"]) if row["ma_announce_date"] else None,
                "ma_close_date": str(row["ma_close_date"]) if row["ma_close_date"] else None,
            },
        }
        if not acc:
            rec["error"] = "no_accession_in_source_ref"
            return rec
        for attempt in range(3):
            try:
                f = find(acc)
                rec["filer"] = str(getattr(f, "company", "") or "")
                rec["filing_date"] = str(getattr(f, "filing_date", "") or "")
                rec["item_text"] = _extract_item_sections(f.text())
                return rec
            except Exception as e:
                if attempt == 2:
                    rec["error"] = f"{type(e).__name__}: {str(e)[:150]}"
                else:
                    time.sleep(2 * (attempt + 1))
        return rec

    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch, r): r["event_id"] for r in rows}
        done = 0
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 25 == 0:
                print(f"[harvest] {done}/{len(rows)}")

    results.sort(key=lambda r: r["event_id"])
    ok = [r for r in results if "item_text" in r]
    bad = [r for r in results if "item_text" not in r]
    print(f"[harvest] fetched={len(ok)} failed={len(bad)}")
    for r in bad:
        print(f"  SKIP event_id={r['event_id']} {r['ticker']}: {r.get('error')}")

    n = max(1, args.chunks)
    per = (len(ok) + n - 1) // n
    for i in range(n):
        chunk = ok[i * per:(i + 1) * per]
        if not chunk:
            continue
        path = os.path.join(args.out, f"ma_chunk_{i:02d}.jsonl")
        with open(path, "w") as f:
            for rec in chunk:
                f.write(json.dumps(rec) + "\n")
        print(f"[harvest] wrote {path} ({len(chunk)} records)")
    skip_path = os.path.join(args.out, "ma_skipped.jsonl")
    with open(skip_path, "w") as f:
        for rec in bad:
            f.write(json.dumps(rec) + "\n")
    print(f"[harvest] wrote {skip_path} ({len(bad)} records)")


_clean_name = sanitize_ma_name


def _clean_date(val):
    if not val or not isinstance(val, str):
        return None
    try:
        d = datetime.strptime(val[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    if not (date(1990, 1, 1) <= d <= date(2030, 12, 31)):
        return None
    return d.isoformat()


def _clean_value_musd(val):
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not (0 < f < 10_000_000):
        return None
    return round(f, 2)


def _load_extractions(results_glob):
    paths = sorted(globlib.glob(results_glob))
    if not paths:
        print(f"no result files match {results_glob}")
        sys.exit(1)
    extracted = {}
    for p in paths:
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                extracted[int(rec["event_id"])] = rec
            except (ValueError, KeyError) as e:
                print(f"bad line in {p}: {e}")
    print(f"{len(extracted)} extraction records from {len(paths)} files")
    return extracted


def _validated_fields(rec):
    """Validate one extraction record → overlay/update field dict + confidence."""
    try:
        conf = float(rec.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    acquirer = _clean_name(rec.get("acquirer"))
    target = _clean_name(rec.get("target"))
    if conf < 0.3:
        acquirer = target = None
    return {
        "acquirer": acquirer,
        "target": target,
        "deal_type": CANON_DEAL_TYPES.get(str(rec.get("deal_type") or "").strip().lower()),
        "transaction_value_usd_m": _clean_value_musd(rec.get("transaction_value_usd_m")),
        "announce_date": _clean_date(rec.get("announce_date")),
        "close_date": _clean_date(rec.get("close_date")),
    }, round(conf, 4)


def cmd_emit_overrides(args):
    extracted = _load_extractions(args.results)
    events = {}
    stats = {"named_both": 0, "low_conf": 0}
    for event_id, rec in sorted(extracted.items()):
        fields, conf = _validated_fields(rec)
        fields["confidence"] = conf
        events[str(event_id)] = fields
        if fields["acquirer"] and fields["target"]:
            stats["named_both"] += 1
        if conf < 0.5:
            stats["low_conf"] += 1
    payload = {
        "_comment": ("Read-only display overlay for coreiq_company_events ma_* garbage; "
                     "re-extracted from source 8-K filings via edgartools + LLM. "
                     "Generated by scripts/enrich_ma_events_v2.py emit-overrides. "
                     "The DB itself is repaired by the data team (see 'apply')."),
        "generated_from": args.results,
        "events": events,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)
    print(f"[emit-overrides] wrote {args.out}: {len(events)} events, "
          f"{stats['named_both']} with both names, {stats['low_conf']} low-confidence")


def cmd_apply(args):
    print("[apply] DATA-TEAM ONLY: this writes to the DB. App-side display uses "
          "emit-overrides instead.")
    extracted = _load_extractions(args.results)

    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT event_id, ticker, ma_acquirer, ma_target, ma_deal_type,
               ma_transaction_value_usd_m, ma_announce_date, ma_close_date
        FROM coreiq_company_events WHERE {CALENDAR_WHERE}
    """)
    db_rows = {r["event_id"]: r for r in cur.fetchall()}

    updated = skipped = nulled = 0
    for event_id, rec in sorted(extracted.items()):
        row = db_rows.get(event_id)
        if row is None:
            print(f"  MISS event_id={event_id} not in calendar set anymore")
            continue
        fields, conf = _validated_fields(rec)
        acquirer, target = fields["acquirer"], fields["target"]
        deal_type = fields["deal_type"]
        value_m = fields["transaction_value_usd_m"]
        ann, close = fields["announce_date"], fields["close_date"]

        # keep existing value/dates when the LLM found nothing (they may be fine)
        if value_m is None and row["ma_transaction_value_usd_m"] is not None:
            value_m = float(row["ma_transaction_value_usd_m"])
        if ann is None and row["ma_announce_date"]:
            ann = str(row["ma_announce_date"])
        if close is None and row["ma_close_date"]:
            close = str(row["ma_close_date"])
        if deal_type is None:
            deal_type = row["ma_deal_type"]

        old_a, old_t = row["ma_acquirer"], row["ma_target"]
        if acquirer is None and target is None and _clean_name(old_a) is None and _clean_name(old_t) is None:
            nulled += 1  # garbage out, nothing in — still clear the garbage
        changes = {
            "ma_acquirer": acquirer, "ma_target": target, "ma_deal_type": deal_type,
            "ma_transaction_value_usd_m": value_m, "ma_announce_date": ann,
            "ma_close_date": close, "ma_extraction_confidence": round(conf, 4),
        }
        print(f"  {'DRY ' if args.dry_run else ''}event_id={event_id} {row['ticker']}: "
              f"acquirer={old_a!r}→{acquirer!r} target={old_t!r}→{target!r} "
              f"type={deal_type!r} val={value_m} ann={ann} close={close} conf={conf}")
        if args.dry_run:
            continue
        cur.execute("""
            UPDATE coreiq_company_events
            SET ma_acquirer=%s, ma_target=%s, ma_deal_type=%s,
                ma_transaction_value_usd_m=%s, ma_announce_date=%s,
                ma_close_date=%s, ma_extraction_confidence=%s, updated_at=NOW()
            WHERE event_id=%s
        """, (changes["ma_acquirer"], changes["ma_target"], changes["ma_deal_type"],
              changes["ma_transaction_value_usd_m"], changes["ma_announce_date"],
              changes["ma_close_date"], changes["ma_extraction_confidence"], event_id))
        updated += 1
    conn.close()
    print(f"[apply] updated={updated} dry_run={args.dry_run} "
          f"missing_extractions={len(db_rows) - len(extracted)}")


def main():
    _load_env()
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("--out", required=True)
    h.add_argument("--chunks", type=int, default=9)
    e = sub.add_parser("emit-overrides")
    e.add_argument("--results", required=True, help="glob of extraction JSONL files")
    e.add_argument("--out", required=True, help="path of ma_event_overrides.json")
    a = sub.add_parser("apply", help="DATA TEAM ONLY — writes to the DB")
    a.add_argument("--results", required=True, help="glob of extraction JSONL files")
    a.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.cmd == "harvest":
        cmd_harvest(args)
    elif args.cmd == "emit-overrides":
        cmd_emit_overrides(args)
    else:
        cmd_apply(args)


if __name__ == "__main__":
    main()
