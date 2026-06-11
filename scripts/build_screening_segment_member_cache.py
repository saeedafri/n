#!/usr/bin/env python3
"""Precompute segment member options for Company Screening (fast UI dropdown)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


def main() -> int:
    from data.screening_service import (
        SEGMENT_MEMBER_CACHE_TABLE,
        ensure_segment_member_cache_table,
        get_base_company_universe,
        _fetch_segment_options_aggregated_sql,
        _normalize_segment_options_rows,
    )
    from core.database import db_manager

    t0 = time.perf_counter()
    if not ensure_segment_member_cache_table():
        print("Failed to ensure cache table exists.")
        return 1

    tickers = list(get_base_company_universe()["ticker"].dropna().unique())
    print(f"Universe tickers: {len(tickers)}")

    for segment_type in ("business", "geographical"):
        t_seg = time.perf_counter()
        rows = _fetch_segment_options_aggregated_sql(
            tickers, segment_type, "Latest", timeout_ms=120_000,
        )
        options = _normalize_segment_options_rows(rows, segment_type)
        print(f"  {segment_type}: {len(options)} members from {len(rows)} agg rows "
              f"in {(time.perf_counter() - t_seg):.1f}s")

        db_manager.execute_insert(
            f"DELETE FROM {SEGMENT_MEMBER_CACHE_TABLE} WHERE segment_type = :st",
            {"st": segment_type},
        )
        for label, cnt in options:
            norm = label.lower().strip()
            db_manager.execute_insert(
                f"""
                INSERT INTO {SEGMENT_MEMBER_CACHE_TABLE}
                (segment_type, member_label, member_label_normalized, company_count)
                VALUES (:st, :label, :norm, :cnt)
                ON DUPLICATE KEY UPDATE
                    member_label = VALUES(member_label),
                    company_count = VALUES(company_count),
                    updated_at = CURRENT_TIMESTAMP
                """,
                {"st": segment_type, "label": label, "norm": norm, "cnt": cnt},
            )

    print(f"Done in {(time.perf_counter() - t0):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
