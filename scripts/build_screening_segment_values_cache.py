#!/usr/bin/env python3
"""
README
------
Build the fast Company Screening segment values cache:

    python3 scripts/build_screening_segment_values_cache.py

Run this script after segment ingestion / filing refresh so Screening stays fast.
Use --segment-type, --metric, --year, --latest-only, and --dry-run for targeted rebuilds.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

BatchEntries = Dict[Tuple[str, str], Tuple[int, float]]


def _rows_to_cache_entries(
    rows: list,
    segment_type: str,
    metric_key: str,
) -> BatchEntries:
    """Map (ticker, member) -> (report_fiscal_year, value_mm), keeping latest year."""
    from data.repository import SegmentDataRepository
    from data.screening_service import (
        _segment_member_raw,
        _segment_row_section,
        _segment_row_matches_metric,
    )

    want_geo = segment_type == "geographical"
    best: BatchEntries = {}
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        if _segment_row_section(row) != ("geo" if want_geo else "business"):
            continue
        if not _segment_row_matches_metric(row, metric_key):
            continue
        member = _segment_member_raw(row)
        if not member:
            continue
        year = SegmentDataRepository._get_row_year(row)
        if year is None:
            continue
        raw_val = row.get("numeric_value")
        if raw_val is None:
            continue
        key = (ticker, member)
        yr = int(year)
        val_mm = float(raw_val) / 1_000_000
        prev = best.get(key)
        if prev is None or yr >= prev[0]:
            best[key] = (yr, val_mm)
    return best


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build coreiq_screening_segment_values_cache for fast Screening segment criteria.",
    )
    parser.add_argument(
        "--segment-type",
        choices=("business", "geographical"),
        help="Rebuild only one segment type. Default: both.",
    )
    parser.add_argument(
        "--metric",
        help="Rebuild one metric label/key, e.g. Revenues. Default: all segment metrics.",
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Rebuild one fiscal year, e.g. 2024.",
    )
    parser.add_argument(
        "--latest-only",
        action="store_true",
        help="Rebuild latest fiscal year per ticker/member. This is the default when --year is omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and count cache rows without deleting or inserting.",
    )
    args = parser.parse_args()
    if args.year and args.latest_only:
        parser.error("--year and --latest-only are mutually exclusive")
    return args


def _target_segment_types(requested: str | None) -> List[Tuple[str, str]]:
    if requested == "business":
        return [("Business Segments", "business")]
    if requested == "geographical":
        return [("Geographical Segments", "geographical")]
    return [
        ("Business Segments", "business"),
        ("Geographical Segments", "geographical"),
    ]


def _target_metrics(stmt_name: str, requested_metric: str | None) -> List[dict]:
    from data.screening_config import STATEMENT_CONFIG

    metrics = list((STATEMENT_CONFIG.get(stmt_name) or {}).get("metrics") or [])
    if not requested_metric:
        return metrics

    needle = requested_metric.strip().lower()
    selected = [
        metric
        for metric in metrics
        if (metric.get("metric_key") or "").lower() == needle
        or (metric.get("label") or "").lower() == needle
    ]
    if not selected:
        valid = ", ".join(m.get("metric_key") or m.get("label", "") for m in metrics)
        raise SystemExit(f"Unknown metric {requested_metric!r} for {stmt_name}. Valid: {valid}")
    return selected


def _delete_existing_rows(
    segment_type: str,
    metric_key: str,
    year: int | None,
) -> int:
    from core.database import db_manager
    from data.screening_service import SEGMENT_VALUES_CACHE_TABLE

    year_clause = ""
    params = {"st": segment_type, "mk": metric_key}
    if year is not None:
        year_clause = " AND report_fiscal_year = :yr "
        params["yr"] = year

    return db_manager.execute_insert(
        f"""
        DELETE FROM {SEGMENT_VALUES_CACHE_TABLE}
        WHERE segment_type = :st
          AND metric_key = :mk
          {year_clause}
        """,
        params,
    )


def _upsert_entries(
    entries: BatchEntries,
    segment_type: str,
    metric_key: str,
    batch_size: int = 500,
) -> int:
    from core.database import db_manager
    from data.screening_service import SEGMENT_VALUES_CACHE_TABLE

    rows = list(entries.items())
    inserted = 0
    for offset in range(0, len(rows), batch_size):
        chunk = rows[offset : offset + batch_size]
        values_sql = []
        params = {}
        for idx, ((ticker, member), (year, val_mm)) in enumerate(chunk):
            values_sql.append(
                f"(:ticker{idx}, :st{idx}, :mk{idx}, :member{idx}, :yr{idx}, :val{idx})"
            )
            params[f"ticker{idx}"] = ticker
            params[f"st{idx}"] = segment_type
            params[f"mk{idx}"] = metric_key
            params[f"member{idx}"] = member
            params[f"yr{idx}"] = year
            params[f"val{idx}"] = val_mm

        db_manager.execute_insert(
            f"""
            INSERT INTO {SEGMENT_VALUES_CACHE_TABLE}
            (ticker, segment_type, metric_key, member_label, report_fiscal_year, value_mm)
            VALUES {", ".join(values_sql)}
            ON DUPLICATE KEY UPDATE
                value_mm = VALUES(value_mm),
                updated_at = CURRENT_TIMESTAMP
            """,
            params,
        )
        inserted += len(chunk)
    return inserted


def _print_cache_status() -> None:
    from data.screening_service import get_segment_values_cache_status

    status = get_segment_values_cache_status()
    print("\nFinal cache health status")
    for key in (
        "table_exists",
        "total_rows",
        "business_rows_count",
        "geographical_rows_count",
        "distinct_tickers",
        "distinct_metrics",
        "min_report_fiscal_year",
        "max_report_fiscal_year",
        "last_updated_timestamp",
        "is_healthy",
    ):
        print(f"  {key}: {status.get(key)}")


def main() -> int:
    """Full rebuild of the screening segment values cache.

    Delegates to ``screening_service.build_segment_values_cache`` so the offline
    build uses the EXACT same wrapper-unwrap / geo-routing classifier as the
    market-data Segments tab and the in-app background rebuild (single source of
    truth). The granular ``--segment-type/--metric/--year`` flags are no longer
    supported — the build is always a whole-table replace — and error out so a
    stale invocation can't silently do a partial (and now-incorrect) rebuild.
    """
    from data.screening_service import (
        build_segment_values_cache,
        ensure_segment_values_cache_table,
    )

    args = _parse_args()
    if args.segment_type or args.metric or args.year or args.latest_only:
        print(
            "Partial rebuild flags (--segment-type/--metric/--year/--latest-only) "
            "are no longer supported: the cache is now built as one atomic whole-"
            "table replace via the shared Segments classifier. Re-run with no flags."
        )
        return 2

    started = time.perf_counter()
    if not ensure_segment_values_cache_table():
        print("Failed to ensure segment values cache table.")
        return 1

    if args.dry_run:
        from data.screening_service import _segment_cache_universe

        print(f"Dry run: would rebuild {len(_segment_cache_universe())} tickers. No writes.")
        return 0

    def _cb(done: int, total: int) -> None:
        print(f"  processed {done}/{total} tickers", end="\r", flush=True)

    stats = build_segment_values_cache(progress_cb=_cb)
    print()
    print(f"Tickers: {stats['tickers']}")
    print(f"Cache rows written: {stats['rows']}")
    print(f"Total runtime: {(time.perf_counter() - started):.1f}s")
    _print_cache_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
