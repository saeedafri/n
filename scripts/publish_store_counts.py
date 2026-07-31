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

from data import store_count_cache as cache
from data.store_count_sources import roll_up_continents

rows = json.load(open("/tmp/sc_report/reconciled.json"))
written = 0
removed = 0
for row in rows:
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

index = cache.rebuild_index()
snapshot_entries = cache.write_snapshot()
print(f"wrote {written} entries, removed {removed} stale, at {cache.cache_dir()}")
print(f"index covers {len(index)} tickers")
print(f"snapshot: {snapshot_entries} ticker-years, "
      f"{cache.snapshot_path().stat().st_size / 1024:.0f} KB — the file the app reads")
