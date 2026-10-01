#!/usr/bin/env python3
"""Extract staging business-segment review data without printing credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv


REPO = Path(__file__).resolve().parents[3]
load_dotenv(REPO / ".env")

os.environ["APP_ENV"] = "LOCAL"
os.environ["DEBUG"] = "true"
os.environ["DB_HOST"] = os.environ["STG_DB_HOST"]
os.environ["DB_PORT"] = os.environ.get("STG_DB_PORT", "3306")
os.environ["DB_NAME"] = os.environ["STG_DB_NAME"]
os.environ["DB_USER"] = os.environ["STG_DB_USER"]
os.environ["DB_PASSWORD"] = os.environ["STG_DB_PASSWORD"]
os.environ["ENABLE_SSL"] = "true"

import sys

sys.path.insert(0, str(REPO / "app"))

from core.database import db_manager
from data.repository import SegmentDataRepository
from data.segment_aliases import is_geo_named_label


members = db_manager.execute_query_readonly(
    """
    SELECT member_label, member_label_normalized, company_count, updated_at
    FROM coreiq_screening_segment_member_cache
    WHERE segment_type = 'business'
    ORDER BY company_count DESC, member_label ASC
    """
) or []

details = db_manager.execute_query_readonly(
    """
    SELECT
        v.member_label,
        COUNT(DISTINCT v.ticker) AS values_company_count,
        COUNT(*) AS cached_value_rows,
        MIN(v.report_fiscal_year) AS earliest_year,
        MAX(v.report_fiscal_year) AS latest_year,
        GROUP_CONCAT(DISTINCT v.metric_key ORDER BY v.metric_key SEPARATOR ' | ') AS metrics,
        SUBSTRING_INDEX(
            GROUP_CONCAT(
                DISTINCT CONCAT(v.ticker, ': ', COALESCE(c.name_coresight, v.ticker))
                ORDER BY v.ticker SEPARATOR ' | '
            ),
            ' | ', 8
        ) AS sample_companies
    FROM coreiq_screening_segment_values_cache v
    LEFT JOIN coreiq_companies c ON c.ticker = v.ticker
    WHERE v.segment_type = 'business'
    GROUP BY v.member_label
    """
) or []

geo_rows = db_manager.execute_query_readonly(
    """
    SELECT member_label, member_label_normalized, company_count
    FROM coreiq_screening_segment_member_cache
    WHERE segment_type = 'geographical'
    """
) or []

meta = db_manager.execute_query_readonly(
    """
    SELECT
        COUNT(*) AS total_value_rows,
        COUNT(DISTINCT ticker) AS distinct_tickers,
        COUNT(DISTINCT member_label) AS distinct_business_members,
        MIN(report_fiscal_year) AS earliest_year,
        MAX(report_fiscal_year) AS latest_year,
        MAX(updated_at) AS values_updated_at
    FROM coreiq_screening_segment_values_cache
    WHERE segment_type = 'business'
    """
) or []

detail_map = {row["member_label"]: row for row in details}
geo_by_norm = {row["member_label_normalized"]: row for row in geo_rows}

metric_columns = [
    "Revenues",
    "Operating Profit Before Tax",
    "Assets",
    "Depreciation & Amortization",
    "Capital Expenditure",
]

output_rows = []
member_labels = {row["member_label"] for row in members}
all_source_rows = [
    (rank, row)
    for rank, row in enumerate(members, start=1)
]
all_source_rows.extend(
    (None, {
        "member_label": label,
        "member_label_normalized": label.lower().strip(),
        "company_count": 0,
        "updated_at": "",
    })
    for label in sorted(set(detail_map) - member_labels, key=str.lower)
)

for rank, row in all_source_rows:
    label = row["member_label"]
    detail = detail_map.get(label, {})
    metrics = set((detail.get("metrics") or "").split(" | ")) - {""}
    geo_match = geo_by_norm.get(row["member_label_normalized"])
    geo_named = bool(is_geo_named_label(label))
    geo_heuristic = bool(SegmentDataRepository._looks_geographic(label))
    if geo_match:
        geo_reason = "Exact normalized label also exists in Geographical Segments"
    elif geo_named:
        geo_reason = "Recognized by the reviewed geographical label map"
    elif geo_heuristic:
        geo_reason = "Matches the repository's geographical-name heuristic"
    else:
        geo_reason = ""
    item = {
        "dropdown_rank": rank,
        "shown_in_current_dropdown": bool(rank is not None and rank <= 500),
        "source_coverage": (
            "Member and values caches"
            if label in detail_map and label in member_labels
            else "Values cache only"
            if label in detail_map
            else "Member cache only"
        ),
        "business_segment": label,
        "member_cache_company_count": int(row.get("company_count") or 0),
        "values_cache_company_count": int(detail.get("values_company_count") or 0),
        "cached_value_rows": int(detail.get("cached_value_rows") or 0),
        "earliest_fiscal_year": detail.get("earliest_year"),
        "latest_fiscal_year": detail.get("latest_year"),
        "sample_companies": detail.get("sample_companies") or "",
        "location_like": bool(geo_match or geo_named or geo_heuristic),
        "location_review_reason": geo_reason,
        "also_in_geo_cache_exact_label": bool(geo_match),
        "geo_cache_company_count": int((geo_match or {}).get("company_count") or 0),
        "cache_updated_at": str(row.get("updated_at") or ""),
    }
    for metric in metric_columns:
        item[f"has_{metric}"] = metric in metrics
    output_rows.append(item)

payload = {
    "source": "Staging database via local OIDC bypass",
    "portal": "http://localhost:8501/screening",
    "statement_type": "Business Segments",
    "dropdown_limit": 500,
    "metric_options": metric_columns,
    "meta": meta[0] if meta else {},
    "total_member_cache_rows": len(members),
    "total_distinct_business_labels": len(output_rows),
    "location_like_count": sum(1 for row in output_rows if row["location_like"]),
    "rows": output_rows,
}

out_path = Path(__file__).with_name("staging_business_segments.json")
out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

print(json.dumps({
    "output": str(out_path),
    "segments": len(output_rows),
    "dropdown_rows": min(500, len(output_rows)),
    "location_like": payload["location_like_count"],
    "meta": payload["meta"],
}, indent=2, default=str))
