"""Write reconciled values into the permanent cache the app reads.

Only values the reconciliation actually stands behind are published. A row that
was kept only because our own extraction could not be verified is written with
its DB value and a review flag, so the portal never shows a number the pipeline
does not believe.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import pymysql

from data import store_count_cache as cache
from data.store_count_extractor import industry_allows_store_count
from data.store_count_sources import roll_up_continents


def industry_by_ticker():
    """Coresight's own classification, used as the last gate before publishing.

    The gate existed in the extractor but nothing on the publish path applied
    it, so companies that do not operate stores were reaching the portal:
    Apple with 475, PepsiCo climbing to 11,736, Qualcomm 7,502, Coty 40,407 and
    Cullen/Frost — a bank — with 26,477. Every one of those numbers is real and
    in the filing; none of them is a store count.
    """
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


INDUSTRIES = industry_by_ticker()

rows = json.load(open("/tmp/sc_report/reconciled.json"))
written = 0
removed = 0
blocked = set()
for row in rows:
    ticker = row["ticker"].upper()
    # A company we hold no classification for is allowed through — 190 tickers
    # with filings are absent from coreiq_companies and several of them are
    # real retailers (Grocery Outlet, Lovesac, Arhaus). Silence is not a
    # verdict. A classification that says "not a store operator" is.
    if ticker in INDUSTRIES and not industry_allows_store_count(INDUSTRIES[ticker]):
        blocked.add(ticker)
        stale = cache.cache_dir() / f"{row['accession'].replace('/', '_')}::{row['fiscal_year']}.json"
        if stale.is_file():
            stale.unlink()
            removed += 1
        continue
    if not row.get("final_value"):
        # A year the reconciliation rejected must LOSE its cache entry, not keep
        # the previous run's answer. Skipping it silently left WMT FY2016 on
        # 6,588 after that value had been judged out of range.
        stale = cache.cache_dir() / f"{row['accession'].replace('/', '_')}::{row['fiscal_year']}.json"
        if stale.is_file():
            stale.unlink()
            removed += 1
        continue
    by_country = row.get("by_country") or {}
    # A split that does not add up to the published total describes a different
    # scope and must not be shown beside it. AutoZone's total is its US fleet
    # while its XBRL geography is worldwide, so the two together read as
    # "North America 5,780 of 5,297" — 109% of the total. Better no split.
    if by_country:
        spread = sum(by_country.values())
        if abs(spread - row["final_value"]) / max(row["final_value"], 1) > 0.02:
            by_country = {}
    cache.write(f"{row['accession']}::{row['fiscal_year']}", {
        "ticker": row["ticker"],
        "fiscal_year": row["fiscal_year"],
        "value": row["final_value"],
        "confidence": row["final_confidence"],
        "basis": row["final_basis"],
        "evidence": (row.get("evidence") or "")[:400],
        "by_country": by_country,
        "by_continent": roll_up_continents(by_country),
        "db_value": row.get("db_value"),
        "needs_review": row["final_confidence"] == "low",
    })
    written += 1

# Sweep the whole cache directory, not just the tickers reconciliation had an
# opinion about. Deleting only while iterating reconciled.json left every
# blocked company that had NO reconciled row this pass holding its old files —
# Apple on 475, Amazon on 14,005, Adobe on 2,975 and ADM on 64,093 all survived
# the gate that was supposed to remove them and went straight into the snapshot.
swept = 0
for path in cache.cache_dir().glob("*.json"):
    if path.name.startswith("_") or path.name == cache.SNAPSHOT_NAME:
        continue
    try:
        entry = json.loads(path.read_text())
    except Exception:
        continue
    owner = str(entry.get("ticker", "")).upper()
    if owner in INDUSTRIES and not industry_allows_store_count(INDUSTRIES[owner]):
        path.unlink()
        swept += 1
        blocked.add(owner)

index = cache.rebuild_index()
snapshot_entries = cache.write_snapshot()
print(f"wrote {written} entries, removed {removed} stale, at {cache.cache_dir()}")
print(f"index covers {len(index)} tickers")
print(f"snapshot: {snapshot_entries} ticker-years, "
      f"{cache.snapshot_path().stat().st_size / 1024:.0f} KB — the file the app reads")
print(f"swept {swept} stale files from blocked tickers")
print(f"blocked {len(blocked)} tickers whose industry does not operate stores: "
      f"{', '.join(sorted(blocked)[:14])}{' ...' if len(blocked) > 14 else ''}")
