"""
Screening Service
=================
Data-access layer for the Coresight Screening feature.

Responsibilities:
- Fetch option lists (industries, countries)
- Build base company universe DataFrame
- Apply each criterion type (industry, geography, financial) to a working DataFrame
- Handle SEC vs YFinance source routing for financial data
- Return production-ready DataFrames and debug metadata

This module NEVER touches Streamlit UI. All caching uses @st.cache_data.
All DB access goes through db_manager from core.database.
All logging via server_logger only.

Assumptions / Schema Notes:
- coreiq_companies.source = 'SEC' | 'YFinance'
- coreiq_companies.primary_industry_coresight = industry label
- coreiq_companies.country_of_incorporation = country label (may not exist in all envs)
- SEC financial tables: direct snake_case columns + fiscal_date_ending + report_type
- YF financial tables: ticker, period_end, frequency, line_item, value (long format)
- DB stores raw dollar values; UI shows / user enters in $mm (divide/multiply by 1,000,000)
"""

import json
import os
import re
import time
import pandas as pd
import streamlit as st
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from core.database import db_manager
from data.screening_config import (
    STATEMENT_CONFIG,
    DB_SCALE,
    KEYDEV_CATEGORIES,
    KEYDEV_CATEGORY_ALIASES,
    KEYDEV_CATEGORY_REVERSE_ALIASES,
    TABULAR_MARKET_DATA_STMTS,
    SEGMENT_STATEMENT_TYPES,
)
from data.repository import (
    CompanyRepository,
    KeyStatsRepository,
    RatiosRepository,
    SegmentDataRepository,
    _format_company_name,
    _get_fiscal_year_end_cached,
)
from data.models import get_fiscal_quarter

try:
    from utils.server_logger import (
        log_error, log_exception, log_info, log_timing, log_db_timing,
        timed_operation,
    )
except ImportError:
    import logging
    _lg = logging.getLogger(__name__)
    log_error = _lg.error
    log_info = _lg.info
    def log_timing(*a, **kw): pass
    def log_db_timing(*a, **kw): pass
    from contextlib import contextmanager as _cm
    @_cm
    def timed_operation(*a, **kw): yield


# =============================================================================
# OPTION LISTS  (cached 1 hour — stable, rarely changes)
# =============================================================================

def get_all_industries() -> List[str]:
    """Return sorted list of distinct non-empty industries from companies_map.

    Uses the 1-hour cached companies_map — zero DB overhead.
    """
    return CompanyRepository.get_all_sectors()


@st.cache_data(ttl=1800, show_spinner=False)
def _get_segment_names_cached(ticker_sample: tuple) -> List[str]:
    """Single DB query for segment names. Sampled to first 50 tickers for speed.

    One call serves both geo and biz option lists — avoids two identical queries.
    MAX_EXECUTION_TIME(3000) + 5s Python thread timeout ensures the form never
    blocks even if v4 has no ticker index.
    """
    if not ticker_sample:
        return []
    ticker_sql = _build_ticker_in_list(list(ticker_sample))

    result: List[List[str]] = []  # mutable container for thread result

    def _run():
        try:
            rows = db_manager.execute_query_readonly(f"""
                SELECT /*+ MAX_EXECUTION_TIME(3000) */ DISTINCT dimension_member_label
                FROM coreiq_filing_metrics_v4
                WHERE ticker IN ({ticker_sql})
                  AND is_dimensioned = 1
                  AND dimension_member_label IS NOT NULL
                  AND dimension_member_label != ''
                ORDER BY dimension_member_label
                LIMIT 500
            """)
            result.append([r["dimension_member_label"] for r in (rows or [])])
        except Exception as exc:
            log_error(f"[SCREENING] _get_segment_names_cached failed: {exc}")
            result.append([])

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_run)
        try:
            fut.result(timeout=5)  # 5-second Python-level ceiling
        except Exception:
            log_error("[SCREENING] _get_segment_names_cached timed out (5s)")
            return []

    return result[0] if result else []


_GEO_SEGMENT_PRESETS: List[str] = sorted([
    # Major geographic regions / blocs
    "Americas", "Americas Retail", "Americas Wholesale", "Americas Segment",
    "North America", "North America Segment",
    "International", "International Segment",
    "Europe", "Europe Segment", "Europe, Middle East and Africa", "EMEA",
    "Asia", "Asia Segment", "Asia Pacific", "Asia-Pacific",
    "Rest of Asia Pacific", "APAC Segment",
    "Greater China", "China",
    "Japan",
    "Latin America",
    "Middle East",
    "Africa",
    # Country-level segments
    "United States", "Canada", "Mexico",
    "United Kingdom", "Germany", "France", "Italy", "Spain",
    "Australia", "South Korea", "India", "Brazil",
    # Retail-specific geo segments
    "Domestic", "Foreign",
    "Walmart U.S.", "Walmart International", "Sam's Club",
    "International Retail Stores and E-commerce Operations",
    "Wholly-owned Retail Stores", "Wholly-owned Retail Store and E-commerce Operations",
])


def get_all_geo_segment_names(ticker_tuple: tuple = ()) -> List[str]:
    """Return hardcoded geographic segment labels drawn from coreiq_filing_metrics_v4.

    These are the actual dimension_member_label values on geo axes (StatementGeographicalAxis
    etc.) for revenue metrics — the same values shown in the market data Segments tab.
    No DB call at render time; loads instantly.
    """
    return _GEO_SEGMENT_PRESETS


_BIZ_SEGMENT_PRESETS: List[str] = sorted([
    # Geographic / Regional segments
    "Americas", "Americas Retail", "Americas Wholesale", "Americas Segment",
    "Asia", "Asia Segment", "Asia-Related", "APAC Segment",
    "Europe", "Europe Segment", "Europe, Middle East and Africa", "Greater China",
    "International", "International Segment", "Japan",
    "North America", "North America Segment", "Rest of Asia Pacific",
    "United States", "Walmart U.S.", "Walmart International", "Sam's Club",
    # Brand / Product segments (Retail & Apparel)
    "NIKE Brand", "Jordan Brand", "Converse",
    "Abercrombie", "Abercrombie Kids", "Hollister", "Gilly Hicks",
    "Aerie", "Coach", "Coach Brand", "Calvin Klein", "Tommy Hilfiger",
    "Kate Spade", "Versace", "Michael Kors",
    "Old Navy", "Gap", "Banana Republic", "Athleta",
    "Free People", "Anthropologie Group", "Urban Outfitters",
    "PVH Heritage Brands", "VF International",
    # Product category segments
    "Apparel", "Footwear", "Equipment", "Accessories",
    "Hardlines", "Softlines", "Electronics and other general merchandise",
    "General merchandise", "Grocery", "Foods", "Fresh Foods", "Non-Foods",
    "Sundries", "Food and Sundries",
    "Beauty", "Health and wellness", "Pharmacy",
    "Pet Supplies", "Home furnishings & décor",
    "Technology, office and entertainment",
    # Channel / Sales type segments
    "Wholesale", "Direct-to-Consumer Operations",
    "Company-operated stores", "Retail Stores",
    "Online stores", "Physical stores",
    "Net product sales", "Net service sales", "Services",
    "Subscription services", "Advertising revenue", "Media",
    # Digital / Tech
    "Amazon Web Services", "AWS",
    "iPhone", "Mac", "iPad", "Wearables, Home and Accessories",
    "Products", "Product", "Membership", "Membership fees",
])


def get_all_biz_segment_names(ticker_tuple: tuple = ()) -> List[str]:
    """Return hardcoded business segment labels drawn from the DB across the Coresight universe.

    These are the same segment names that appear in the market data Segments tab.
    No DB call at render time — instant, never blocks the form.
    """
    return _BIZ_SEGMENT_PRESETS


@st.cache_data(ttl=3600, show_spinner=False)
def get_all_countries() -> List[str]:
    """Return sorted list of distinct non-null country_of_incorporation values.

    Falls back gracefully if column doesn't exist in the current schema.
    """
    query = """
        SELECT DISTINCT country_of_incorporation
        FROM coreiq_companies
        WHERE country_of_incorporation IS NOT NULL
          AND country_of_incorporation != ''
        ORDER BY country_of_incorporation
    """
    try:
        rows    = db_manager.execute_query_readonly(query)
        countries = [r["country_of_incorporation"] for r in rows]
        return countries

    except Exception:
        return []


# =============================================================================
# BASE UNIVERSE
# =============================================================================

def get_base_company_universe() -> pd.DataFrame:
    """Return DataFrame of ALL companies as the base screening universe.

    Uses get_companies_rows() (1-hour cached) instead of get_companies_map() so
    that companies sharing the same ticker on different exchanges (e.g. JD Sports
    on LSE and JD.com on NASDAQ both use ticker 'JD') are each included as a
    separate row.  The unique identity is ticker + name_coresight.

    Returns:
        DataFrame with columns: ticker, company_name, sector, exchange
    """
    company_rows = CompanyRepository.get_companies_rows()

    rows = []
    for c in company_rows:
        raw_name = c.get("name_coresight") or ""
        rows.append({
            "ticker":       c["ticker"],
            "company_name": _format_company_name(raw_name),
            "sector":       c.get("primary_industry_coresight") or "",
            "exchange":     c.get("exchange") or "",
            "country":      c.get("country_of_incorporation") or "",
        })
    df = pd.DataFrame(rows, columns=["ticker", "company_name", "sector", "exchange", "country"])
    return df


# =============================================================================
# CRITERION APPLICATIONS
# =============================================================================

def apply_industry_criterion(
    industries: List[str],
    working_df: pd.DataFrame,
    display_col: str = "Industry",
) -> Tuple[pd.DataFrame, Dict]:
    """Annotate (non-filtering) each company with its sector.

    Keeps ALL companies. The annotation column shows the company's sector when it
    matches the selected industries, else N/A. Zero DB overhead.
    """
    rows_in = len(working_df)
    out = working_df.copy()

    if not industries:
        out[display_col] = out["sector"] if "sector" in out.columns else None
        return out, {
            "type": "industry", "rows_in": rows_in, "rows_out": rows_in,
            "with_data": rows_in, "elapsed_ms": 0,
        }

    industry_set = set(industries)
    if "sector" in out.columns:
        out[display_col] = out["sector"].apply(
            lambda s: s if s in industry_set else None
        )
        with_data = int(out[display_col].notna().sum())
    else:
        out[display_col] = None
        with_data = 0

    return out, {
        "type":       "industry",
        "industries": industries,
        "rows_in":    rows_in,
        "rows_out":   rows_in,   # non-filtering: never drops
        "with_data":  with_data,
        "elapsed_ms": 0,
    }


def apply_geography_criterion(
    countries: List[str],
    working_df: pd.DataFrame,
    display_col: str = "Country",
) -> Tuple[pd.DataFrame, Dict]:
    """Annotate (non-filtering) each company with its country.

    Keeps ALL companies. The annotation column shows the company's country when it
    matches the selected countries, else N/A. Uses the in-memory 'country' column.
    """
    t_start = time.perf_counter()
    rows_in = len(working_df)
    out = working_df.copy()

    if "country" not in out.columns:
        out[display_col] = None
        return out, {
            "type": "geography", "rows_in": rows_in, "rows_out": rows_in,
            "with_data": 0, "elapsed_ms": 0,
        }

    if not countries:
        out[display_col] = out["country"]
        return out, {
            "type": "geography", "rows_in": rows_in, "rows_out": rows_in,
            "with_data": rows_in, "elapsed_ms": 0,
        }

    country_set = set(countries)
    out[display_col] = out["country"].apply(lambda c: c if c in country_set else None)
    with_data = int(out[display_col].notna().sum())
    elapsed  = (time.perf_counter() - t_start) * 1000
    return out, {
        "type":      "geography",
        "countries": countries,
        "rows_in":   rows_in,
        "rows_out":  rows_in,   # non-filtering: never drops
        "with_data": with_data,
        "source":    "in_memory",
        "elapsed_ms": elapsed,
    }


def apply_financial_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Apply a financial metric criterion to working_df.

    Companies without data for the metric are excluded from results.
    Supports both Annual and Quarterly period types.
    """
    t_total = time.perf_counter()
    rows_in = len(working_df)

    stmt        = criterion["statement"]
    metric_info = criterion["metric_info"]
    operator    = criterion["operator"]
    value1      = float(criterion["value1"])
    value2      = float(criterion.get("value2") or 0.0)
    timeframe   = criterion.get("timeframe", "Latest")
    display_col = criterion.get("display_col", f"{metric_info['label']} ($mm)")
    period_type = criterion.get("period_type", "FY")
    quarter     = criterion.get("quarter")        # "Q1"..."Q4" or None
    year_sel    = criterion.get("year", "Latest")  # "Latest" or int year

    cfg = STATEMENT_CONFIG.get(stmt)
    if not cfg:
        log_error(f"[SCREENING] Unknown statement type: {stmt}")
        return working_df.copy(), {
            "type": "financial", "error": f"Unknown statement: {stmt}",
            "rows_in": rows_in, "rows_out": rows_in,
        }

    tickers = list(working_df["ticker"].values)
    if not tickers:
        return pd.DataFrame(columns=list(working_df.columns) + [display_col]), {
            "type": "financial", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0
        }

    log_info(f"[TIMING] SCREENING_FIN_START | stmt={stmt} metric={metric_info['label']} "
             f"op={operator} period={period_type} tickers={len(tickers)}")

    # Determine DB period value: "annual" for FY, "quarterly" for CQ/FQ
    period_value = "quarterly" if period_type in ("CQ", "FQ") else "annual"

    # Parse year
    year = None
    if year_sel and year_sel != "Latest":
        try:
            year = int(year_sel)
        except (ValueError, TypeError):
            year = None

    # Parse quarter number (1-4) for CQ/FQ filtering
    quarter_num = None
    if period_type in ("CQ", "FQ") and quarter:
        try:
            quarter_num = int(quarter.replace("Q", ""))
        except (ValueError, TypeError):
            quarter_num = None

    # ── Phase 1: source split (in-memory) ──
    t_split = time.perf_counter()
    companies_map = CompanyRepository.get_companies_map()
    sec_tickers  = [t for t in tickers if companies_map.get(t, {}).get("source") == "SEC"]
    yf_tickers   = [t for t in tickers if companies_map.get(t, {}).get("source") == "YFinance"]
    unk_tickers  = [t for t in tickers
                    if t not in sec_tickers and t not in yf_tickers]
    ms_split = (time.perf_counter() - t_split) * 1000
    log_timing("SCREENING_FIN_SOURCE_SPLIT", ms_split,
               f"sec={len(sec_tickers)} yf={len(yf_tickers)} unk={len(unk_tickers)}")

    # ── Phase 2+3: SEC and YF queries — run PARALLEL ──
    raw_values: Dict[str, Optional[float]] = {}

    need_sec = bool(sec_tickers and metric_info.get("sec_col"))
    need_yf  = bool(yf_tickers  and metric_info.get("yf_item"))

    ms_sec_query = 0.0
    ms_yf_query = 0.0
    ms_fq_post = 0.0

    if period_type == "FQ" and quarter_num:
        # FQ: fetch all quarterly rows with dates, then post-filter by fiscal quarter
        t_parallel = time.perf_counter()
        futures = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            if need_sec:
                futures["sec"] = pool.submit(
                    _query_sec_quarterly_with_dates,
                    sec_tickers, cfg, metric_info["sec_col"],
                    year,
                )
            if need_yf:
                futures["yf"] = pool.submit(
                    _query_yf_quarterly_with_dates,
                    yf_tickers, cfg, metric_info["yf_item"],
                    year,
                )

        # Collect all (ticker, date, value) rows
        all_rows = []
        if "sec" in futures:
            try:
                all_rows.extend(futures["sec"].result())
            except Exception:
                pass
        if "yf" in futures:
            try:
                all_rows.extend(futures["yf"].result())
            except Exception:
                pass
        ms_parallel = (time.perf_counter() - t_parallel) * 1000
        log_timing("SCREENING_FIN_FQ_DB_PARALLEL", ms_parallel,
                   f"rows_fetched={len(all_rows)}")

        # Post-filter by fiscal quarter: look up each company's fiscal_year_end
        # and compute which FQ each row belongs to
        t_fq_post = time.perf_counter()
        from data.models import parse_fiscal_year_end
        # Group rows by ticker, keep only the one matching the desired FQ
        ticker_candidates: Dict[str, list] = {}
        for ticker, dt, val in all_rows:
            ticker_candidates.setdefault(ticker, []).append((dt, val))

        for ticker, rows_list in ticker_candidates.items():
            fy_end_str = _get_fiscal_year_end_cached(ticker)
            fy_end_month = parse_fiscal_year_end(fy_end_str) if fy_end_str else None
            # Filter rows where fiscal quarter matches
            for dt, val in sorted(rows_list, key=lambda x: x[0], reverse=True):
                if fy_end_month:
                    fq = get_fiscal_quarter(dt.month, fy_end_month)
                else:
                    # No fiscal_year_end data — fall back to calendar quarter
                    fq = (dt.month - 1) // 3 + 1
                if fq == quarter_num:
                    raw_values[ticker] = val
                    break  # take the latest matching row
        ms_fq_post = (time.perf_counter() - t_fq_post) * 1000
        log_timing("SCREENING_FIN_FQ_POST_FILTER", ms_fq_post,
                   f"candidates={len(ticker_candidates)} matched={len(raw_values)}")
    else:
        # FY or CQ: use existing SQL-level filtering
        t_parallel = time.perf_counter()
        futures = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            if need_sec:
                futures["sec"] = pool.submit(
                    _query_sec_latest,
                    sec_tickers, cfg, metric_info["sec_col"],
                    f"{stmt}/{metric_info['label']}",
                    year, period_value, quarter_num,
                )
            if need_yf:
                futures["yf"] = pool.submit(
                    _query_yf_latest,
                    yf_tickers, cfg, metric_info["yf_item"],
                    f"{stmt}/{metric_info['label']}",
                    year, period_value, quarter_num,
                )

        if "sec" in futures:
            try:
                t_sec = time.perf_counter()
                sec_vals = futures["sec"].result()
                ms_sec_query = (time.perf_counter() - t_sec) * 1000
                raw_values.update(sec_vals)
                log_timing("SCREENING_FIN_SEC_RESULT", ms_sec_query,
                           f"tickers_queried={len(sec_tickers)} values_returned={len(sec_vals)}")
            except Exception as exc:
                log_error(f"[SCREENING] SEC query failed: {exc}")

        if "yf" in futures:
            try:
                t_yf = time.perf_counter()
                yf_vals = futures["yf"].result()
                ms_yf_query = (time.perf_counter() - t_yf) * 1000
                raw_values.update(yf_vals)
                log_timing("SCREENING_FIN_YF_RESULT", ms_yf_query,
                           f"tickers_queried={len(yf_tickers)} values_returned={len(yf_vals)}")
            except Exception as exc:
                log_error(f"[SCREENING] YF query failed: {exc}")

        ms_parallel = (time.perf_counter() - t_parallel) * 1000
        log_timing("SCREENING_FIN_DB_PARALLEL", ms_parallel,
                   f"raw_values_total={len(raw_values)}")

    # ── Phase 4: operator filter (pure Python) ──
    t_op = time.perf_counter()
    threshold1_raw = value1 * DB_SCALE
    threshold2_raw = value2 * DB_SCALE
    passing: Dict[str, float] = {}

    for ticker, raw_val in raw_values.items():
        if raw_val is None:
            continue
        try:
            raw_float = float(raw_val)
        except (TypeError, ValueError):
            continue
        if _apply_operator(raw_float, operator, threshold1_raw, threshold2_raw):
            passing[ticker] = raw_float / DB_SCALE   # → $mm for display
    ms_op = (time.perf_counter() - t_op) * 1000
    log_timing("SCREENING_FIN_OPERATOR_FILTER", ms_op,
               f"raw_values={len(raw_values)} passing={len(passing)} op={operator}")

    # ── Phase 5: DataFrame join ──
    t_join = time.perf_counter()
    filtered = working_df[working_df["ticker"].isin(passing.keys())].copy()
    filtered[display_col] = filtered["ticker"].map(passing)
    rows_out = len(filtered)
    ms_join = (time.perf_counter() - t_join) * 1000
    log_timing("SCREENING_FIN_DF_JOIN", ms_join,
               f"rows_in={rows_in} rows_out={rows_out}")

    ms_total = (time.perf_counter() - t_total) * 1000
    log_timing("SCREENING_FIN_TOTAL", ms_total,
               f"stmt={stmt} metric={metric_info['label']} rows_in={rows_in} "
               f"rows_out={rows_out} sec={len(sec_tickers)} yf={len(yf_tickers)}")

    return filtered, {
        "type":        "financial",
        "statement":   stmt,
        "metric":      metric_info["label"],
        "operator":    operator,
        "value1":      value1,
        "value2":      value2,
        "timeframe":   timeframe,
        "period_type": period_type,
        "rows_in":     rows_in,
        "rows_out":    rows_out,
        "sec_tickers": len(sec_tickers),
        "yf_tickers":  len(yf_tickers),
        "raw_values":  len(raw_values),
        "passing":     len(passing),
        "elapsed_ms":  ms_total,
        "phase_ms": {
            "source_split":      ms_split,
            "sec_query":         ms_sec_query,
            "yf_query":          ms_yf_query,
            "operator_filter":   ms_op,
            "df_join":           ms_join,
        },
    }


# =============================================================================
# TABULAR MARKET DATA CRITERION  (Key Stats / Ratios — period-based, tab-equivalent)
# =============================================================================

_TABULAR_MAX_WORKERS = 12


def _screening_period_to_repo(period_type: str) -> str:
    return "quarterly" if period_type in ("CQ", "FQ") else "annual"


def _screening_date_window(
    period_type: str,
    year_sel,
    quarter: Optional[str],
) -> Tuple[date, date]:
    """Wide fetch window so repository can return all periods for index resolution."""
    today = date.today()
    if year_sel and year_sel != "Latest":
        try:
            y = int(year_sel)
        except (ValueError, TypeError):
            y = today.year
        if period_type in ("CQ", "FQ") and quarter:
            qn = int(str(quarter).replace("Q", ""))
            start_m = (qn - 1) * 3 + 1
            end_m = qn * 3
            start = date(y, start_m, 1)
            if end_m in (1, 3, 5, 7, 8, 10, 12):
                end_d = 31
            elif end_m in (4, 6, 9, 11):
                end_d = 30
            else:
                end_d = 29
            end = date(y, end_m, end_d)
            return date(y - 2, 1, 1), date(y + 1, 12, 31)
        return date(y - 1, 1, 1), date(y + 1, 12, 31)
    return date(today.year - 4, 1, 1), today


def _resolve_tabular_period_index(
    periods: list,
    period_type: str,
    year_sel,
    quarter: Optional[str],
    ticker: str,
) -> Optional[int]:
    """Pick period column index matching screening FY/CQ/FQ + year selection."""
    if not periods:
        return None

    candidates = [
        (i, p) for i, p in enumerate(periods)
        if not getattr(p, "is_forecast", False)
    ]
    if not candidates:
        candidates = list(enumerate(periods))

    def _latest_idx(matching):
        if not matching:
            return None
        return max(matching, key=lambda x: x[1].date)[0]

    if year_sel == "Latest" or year_sel is None:
        if period_type in ("CQ", "FQ") and quarter:
            qn = int(str(quarter).replace("Q", ""))
            if period_type == "CQ":
                matching = [(i, p) for i, p in candidates if p.calendar_quarter == qn]
            else:
                matching = [(i, p) for i, p in candidates if p.fiscal_quarter == qn]
            return _latest_idx(matching)
        return _latest_idx(candidates)

    try:
        year = int(year_sel)
    except (ValueError, TypeError):
        return _latest_idx(candidates)

    if period_type in ("CQ", "FQ") and quarter:
        qn = int(str(quarter).replace("Q", ""))
        if period_type == "CQ":
            matching = [
                (i, p) for i, p in candidates
                if p.calendar_quarter == qn and p.date.year == year
            ]
        else:
            matching = [
                (i, p) for i, p in candidates
                if p.fiscal_quarter == qn and p.date.year == year
            ]
        if matching:
            return _latest_idx(matching)
        matching = [(i, p) for i, p in candidates if p.fiscal_quarter == qn]
        return _latest_idx(matching)

    matching = [(i, p) for i, p in candidates if p.date.year == year]
    return _latest_idx(matching)


def _format_tabular_display_value(val: Optional[float], unit: str) -> Optional[float]:
    """Normalize display numeric for results column (already in tab units)."""
    if val is None:
        return None
    try:
        return round(float(val), 4)
    except (TypeError, ValueError):
        return None


_KEY_STATS_SEC_COL = {
    "total_revenue": "total_revenue",
    "gross_profit": "gross_profit",
    "ebitda": "ebitda",
    "ebit": "ebit",
    "cont_ops_income": "net_income_from_continuing_operations",
    "net_income": "net_income",
}

_KEY_STATS_YF_ITEM = {
    "total_revenue": "Total Revenue",
    "gross_profit": "Gross Profit",
    "ebitda": "EBITDA",
    "ebit": "EBIT",
    "cont_ops_income": "Net Income From Continuing Operations",
    "net_income": "Net Income",
}

_KEY_STATS_MARGIN = {
    "gross_profit_margin": ("gross_profit", "total_revenue"),
    "ebitda_margin": ("ebitda", "total_revenue"),
    "ebit_margin": ("ebit", "total_revenue"),
    "cont_ops_margin": ("net_income_from_continuing_operations", "total_revenue"),
    "net_income_margin": ("net_income", "total_revenue"),
}

_KEY_STATS_GROWTH_COL = {
    "revenue_growth_yoy": ("total_revenue", "Total Revenue"),
}


def _to_millions(val: Optional[float]) -> Optional[float]:
    if val is None:
        return None
    return float(val) / DB_SCALE


def _bulk_margin_values(
    numerator: Dict[str, float],
    denominator: Dict[str, float],
    as_percent: bool = True,
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for ticker, num_raw in numerator.items():
        den_raw = denominator.get(ticker)
        if num_raw is None or den_raw is None or den_raw == 0:
            continue
        num = num_raw / DB_SCALE
        den = den_raw / DB_SCALE
        if den == 0:
            continue
        pct = (num / den) * 100 if as_percent else (num / den)
        out[ticker] = round(pct, 4)
    return out


def _bulk_key_stats_growth(
    sec_tickers: List[str],
    yf_tickers: List[str],
    sec_col: str,
    yf_item: str,
    year: Optional[int],
    period_value: str,
    quarter_num: Optional[int],
) -> Dict[str, float]:
    """Bulk YoY / sequential growth (%) for revenue-like columns."""
    result: Dict[str, float] = {}
    is_cfg = STATEMENT_CONFIG["Income Statement"]

    def _growth_from_pairs(pairs: Dict[str, list]) -> None:
        for ticker, rows in pairs.items():
            if len(rows) < 2:
                continue
            rows_sorted = sorted(rows, key=lambda x: x[0])
            curr_dt, curr_val = rows_sorted[-1]
            prev_dt, prev_val = rows_sorted[-2]
            if period_value == "quarterly" and quarter_num:
                # YoY: match same quarter prior calendar year when possible
                try:
                    target = curr_dt.replace(year=curr_dt.year - 1)
                except ValueError:
                    target = curr_dt.replace(year=curr_dt.year - 1, day=28)
                prior = next((v for d, v in rows if d == target), None)
                if prior is not None and curr_val is not None and prior != 0:
                    result[ticker] = round(((curr_val - prior) / abs(prior)) * 100, 4)
                    continue
            if curr_val is not None and prev_val is not None and prev_val != 0:
                result[ticker] = round(((curr_val - prev_val) / abs(prev_val)) * 100, 4)

    if sec_tickers and sec_col:
        ticker_sql = _build_ticker_in_list(sec_tickers)
        date_clause = ""
        if year:
            date_clause = f"AND fiscal_date_ending BETWEEN '{year - 1}-01-01' AND '{year}-12-31'"
        query = f"""
            SELECT ticker, fiscal_date_ending, {sec_col} AS metric_value
            FROM {is_cfg['sec_table']}
            WHERE ticker IN ({ticker_sql})
              AND report_type = '{period_value}'
              AND {sec_col} IS NOT NULL
              {date_clause}
            ORDER BY ticker, fiscal_date_ending
        """
        rows = db_manager.execute_query_readonly(query) or []
        pairs: Dict[str, list] = {}
        for r in rows:
            pairs.setdefault(r["ticker"], []).append((r["fiscal_date_ending"], float(r["metric_value"])))
        if year:
            filtered: Dict[str, list] = {}
            for t, rs in pairs.items():
                yr_rows = [x for x in rs if x[0].year == year]
                prior_rows = [x for x in rs if x[0].year == year - 1]
                if yr_rows and prior_rows:
                    filtered[t] = [prior_rows[-1], yr_rows[-1]]
            pairs = filtered
        else:
            pairs = {t: rs[-2:] for t, rs in pairs.items() if len(rs) >= 2}
        _growth_from_pairs(pairs)

    if yf_tickers and yf_item:
        ticker_sql = _build_ticker_in_list(yf_tickers)
        safe_item = yf_item.replace("'", "''")
        date_clause = ""
        if year:
            date_clause = f"AND period_end BETWEEN '{year - 1}-01-01' AND '{year}-12-31'"
        query = f"""
            SELECT ticker, period_end AS fiscal_date_ending, value AS metric_value
            FROM {is_cfg['yf_table']}
            WHERE ticker IN ({ticker_sql})
              AND frequency = '{period_value}'
              AND line_item = '{safe_item}'
              AND value IS NOT NULL
              {date_clause}
            ORDER BY ticker, period_end
        """
        rows = db_manager.execute_query_readonly(query) or []
        pairs = {}
        for r in rows:
            pairs.setdefault(r["ticker"], []).append((r["fiscal_date_ending"], float(r["metric_value"])))
        if year:
            filtered = {}
            for t, rs in pairs.items():
                yr_rows = [x for x in rs if x[0].year == year]
                prior_rows = [x for x in rs if x[0].year == year - 1]
                if yr_rows and prior_rows:
                    filtered[t] = [prior_rows[-1], yr_rows[-1]]
            pairs = filtered
        else:
            pairs = {t: rs[-2:] for t, rs in pairs.items() if len(rs) >= 2}
        _growth_from_pairs(pairs)

    return result


def _try_bulk_tabular_values(
    tickers: List[str],
    statement: str,
    metric_key: str,
    period_type: str,
    year_sel,
    quarter: Optional[str],
) -> Optional[Dict[str, float]]:
    """Bulk SQL path for Key Stats (and simple Ratios later). Returns None if unsupported."""
    if statement != "Key Stats":
        return None

    period_value = _screening_period_to_repo(period_type)
    quarter_num = None
    if period_type in ("CQ", "FQ") and quarter:
        try:
            quarter_num = int(str(quarter).replace("Q", ""))
        except (ValueError, TypeError):
            quarter_num = None

    year = None
    if year_sel and year_sel != "Latest":
        try:
            year = int(year_sel)
        except (ValueError, TypeError):
            year = None

    companies_map = CompanyRepository.get_companies_map()
    sec_tickers = [t for t in tickers if companies_map.get(t, {}).get("source") == "SEC"]
    yf_tickers = [t for t in tickers if companies_map.get(t, {}).get("source") == "YFinance"]
    is_cfg = STATEMENT_CONFIG["Income Statement"]

    if metric_key in _KEY_STATS_GROWTH_COL:
        sec_col, yf_item = _KEY_STATS_GROWTH_COL[metric_key]
        return _bulk_key_stats_growth(
            sec_tickers, yf_tickers, sec_col, yf_item, year, period_value, quarter_num,
        )

    if metric_key in _KEY_STATS_MARGIN:
        num_col, den_col = _KEY_STATS_MARGIN[metric_key]
        num_sec = _query_sec_latest(
            sec_tickers, is_cfg, num_col, year=year, period_value=period_value, quarter_num=quarter_num,
        )
        den_sec = _query_sec_latest(
            sec_tickers, is_cfg, den_col, year=year, period_value=period_value, quarter_num=quarter_num,
        )
        num_yf = _query_yf_latest(
            yf_tickers, is_cfg, _KEY_STATS_YF_ITEM.get(num_col, num_col),
            year=year, period_value=period_value, quarter_num=quarter_num,
        )
        den_yf = _query_yf_latest(
            yf_tickers, is_cfg, _KEY_STATS_YF_ITEM.get(den_col, den_col),
            year=year, period_value=period_value, quarter_num=quarter_num,
        )
        num_all = {**num_sec, **num_yf}
        den_all = {**den_sec, **den_yf}
        return _bulk_margin_values(num_all, den_all, as_percent=True)

    if metric_key in _KEY_STATS_SEC_COL:
        sec_col = _KEY_STATS_SEC_COL[metric_key]
        yf_item = _KEY_STATS_YF_ITEM[metric_key]
        raw: Dict[str, float] = {}
        sec_vals = _query_sec_latest(
            sec_tickers, is_cfg, sec_col, year=year, period_value=period_value, quarter_num=quarter_num,
        )
        yf_vals = _query_yf_latest(
            yf_tickers, is_cfg, yf_item, year=year, period_value=period_value, quarter_num=quarter_num,
        )
        for t, v in {**sec_vals, **yf_vals}.items():
            if v is not None:
                raw[t] = round(v / DB_SCALE, 4)
        return raw

    return None


def _fetch_ticker_tabular_metric(
    ticker: str,
    statement: str,
    metric_key: str,
    period_type: str,
    year_sel,
    quarter: Optional[str],
) -> Tuple[Optional[float], Optional[date], Optional[str]]:
    """Fetch one tab-equivalent metric value for a ticker. Returns (value, period_date, period_label)."""
    t0 = time.perf_counter()
    repo_pt = _screening_period_to_repo(period_type)
    start_date, end_date = _screening_date_window(period_type, year_sel, quarter)

    try:
        if statement == "Key Stats":
            data = KeyStatsRepository.get_key_stats_data(ticker, start_date, end_date, repo_pt)
            extract = KeyStatsRepository.extract_metric_value
        elif statement == "Ratios":
            data = RatiosRepository.get_ratios_data(ticker, start_date, end_date, repo_pt)
            extract = RatiosRepository.extract_metric_value
        else:
            return None, None, None
    except Exception as exc:
        log_error(f"[SCREENING] tabular fetch failed ticker={ticker} stmt={statement}: {exc}")
        return None, None, None

    periods = data.get("periods") or []
    idx = _resolve_tabular_period_index(periods, period_type, year_sel, quarter, ticker)
    if idx is None:
        return None, None, None

    val = extract(data, metric_key, idx)
    period = periods[idx]
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if elapsed_ms > 500:
        log_timing(
            "SCREENING_TABULAR_SLOW_TICKER",
            elapsed_ms,
            f"ticker={ticker} stmt={statement} metric_key={metric_key} "
            f"period={period_type} year={year_sel}",
        )
    return val, period.date, getattr(period, "label", None)


def _tabular_thresholds(unit: str, value1: float, value2: float) -> Tuple[float, float]:
    """Compare using user-entered units (already $mm / % / x / $ per tab)."""
    return value1, value2


def apply_tabular_market_data_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Apply Key Stats or Ratios criterion using tab-equivalent repository data."""
    t_total = time.perf_counter()
    rows_in = len(working_df)

    stmt = criterion.get("statement")
    metric_info = criterion["metric_info"]
    metric_key = metric_info.get("metric_key")
    metric_label = metric_info.get("label", metric_key or "")
    operator = criterion["operator"]
    value1 = float(criterion["value1"])
    value2 = float(criterion.get("value2") or 0.0)
    display_col = criterion.get("display_col", metric_label)
    unit = metric_info.get("unit", "x")
    period_type = criterion.get("period_type", "FY")
    quarter = criterion.get("quarter")
    year_sel = criterion.get("year", "Latest")
    threshold1, threshold2 = _tabular_thresholds(unit, value1, value2)

    tickers = list(working_df["ticker"].values)
    if not tickers or not metric_key:
        return pd.DataFrame(columns=list(working_df.columns) + [display_col]), {
            "type": "financial", "rows_in": rows_in, "rows_out": 0, "elapsed_ms": 0,
        }

    log_info(
        f"[TIMING] SCREENING_TABULAR_START | stmt={stmt} metric={metric_label} "
        f"key={metric_key} period={period_type} year={year_sel} tickers={len(tickers)}"
    )

    passing: Dict[str, float] = {}
    missing = 0
    matched_periods: Dict[str, str] = {}
    raw_values: Dict[str, float] = {}
    used_bulk = False
    repo_calls = 0
    per_ticker_ms: List[Tuple[str, float]] = []
    ms_pool = 0.0

    t_bulk = time.perf_counter()
    bulk_vals = _try_bulk_tabular_values(
        tickers, stmt, metric_key, period_type, year_sel, quarter,
    )
    ms_bulk = (time.perf_counter() - t_bulk) * 1000

    if bulk_vals is not None:
        used_bulk = True
        raw_values = {t: v for t, v in bulk_vals.items() if v is not None}
        log_timing(
            "SCREENING_TABULAR_BULK_FETCH",
            ms_bulk,
            f"stmt={stmt} metric={metric_label} tickers={len(tickers)} values={len(raw_values)}",
        )
    else:
        def _process_one(ticker: str):
            t_one = time.perf_counter()
            val, pdate, plabel = _fetch_ticker_tabular_metric(
                ticker, stmt, metric_key, period_type, year_sel, quarter,
            )
            return ticker, val, pdate, plabel, (time.perf_counter() - t_one) * 1000

        t_pool = time.perf_counter()
        with ThreadPoolExecutor(max_workers=_TABULAR_MAX_WORKERS) as pool:
            futures = {pool.submit(_process_one, t): t for t in tickers}
            for fut in as_completed(futures):
                repo_calls += 1
                try:
                    ticker, val, pdate, plabel, elapsed_one = fut.result()
                except Exception as exc:
                    log_error(f"[SCREENING] tabular worker failed: {exc}")
                    continue
                per_ticker_ms.append((ticker, elapsed_one))
                if val is None:
                    missing += 1
                    continue
                try:
                    raw_values[ticker] = float(val)
                except (TypeError, ValueError):
                    missing += 1
                    continue
                if pdate:
                    matched_periods[ticker] = str(pdate)
        ms_pool = (time.perf_counter() - t_pool) * 1000
        avg_ms = (sum(ms for _, ms in per_ticker_ms) / len(per_ticker_ms)) if per_ticker_ms else 0
        slowest = sorted(per_ticker_ms, key=lambda x: x[1], reverse=True)[:10]
        slowest_str = ",".join(f"{t}:{ms:.0f}ms" for t, ms in slowest[:5])
        log_timing(
            "SCREENING_TABULAR_TICKER_FETCH_SUMMARY",
            ms_pool,
            f"stmt={stmt} metric={metric_label} tickers={len(tickers)} "
            f"repo_calls={repo_calls} avg_ms={avg_ms:.1f} missing={missing} "
            f"slowest={slowest_str}",
        )

    for ticker, fv in raw_values.items():
        if _apply_operator(fv, operator, threshold1, threshold2):
            disp = _format_tabular_display_value(fv, unit)
            if disp is not None:
                passing[ticker] = disp

    missing = len(tickers) - len(raw_values)
    if used_bulk:
        ms_pool = ms_bulk
    filtered = working_df[working_df["ticker"].isin(passing.keys())].copy()
    filtered[display_col] = filtered["ticker"].map(passing)
    rows_out = len(filtered)
    ms_total = (time.perf_counter() - t_total) * 1000

    log_timing(
        "SCREENING_TABULAR_TOTAL",
        ms_total,
        f"stmt={stmt} metric={metric_label} tickers={len(tickers)} "
        f"passing={len(passing)} missing={missing} rows_in={rows_in} rows_out={rows_out} "
        f"pool_ms={ms_pool:.0f} bulk={used_bulk} repo_calls={repo_calls}",
    )

    return filtered, {
        "type":           "financial",
        "statement":      stmt,
        "metric":         metric_label,
        "metric_key":     metric_key,
        "operator":       operator,
        "period_type":    period_type,
        "year":           year_sel,
        "rows_in":        rows_in,
        "rows_out":       rows_out,
        "missing":        missing,
        "passing":        len(passing),
        "elapsed_ms":     ms_total,
        "pool_ms":        ms_pool,
        "matched_periods": len(matched_periods),
    }


def apply_key_stats_criterion(criterion: Dict, working_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
    criterion = {**criterion, "statement": "Key Stats"}
    return apply_tabular_market_data_criterion(criterion, working_df)


def apply_ratios_criterion(criterion: Dict, working_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict]:
    criterion = {**criterion, "statement": "Ratios"}
    return apply_tabular_market_data_criterion(criterion, working_df)


# =============================================================================
# SEGMENT STATEMENT CRITERIA  (Business / Geographical Segments)
# =============================================================================

_SEGMENT_BULK_CHUNK = 20
_SEGMENT_VALUES_CACHE_CHUNK = 500
_SEGMENT_OPTIONS_LIMIT = 500
_SEGMENT_OPTIONS_UI_TIMEOUT_S = 3.0
_SEGMENT_STATEMENT_DB_TIMEOUT_MS = 15000
ENABLE_SEGMENT_REPOSITORY_FALLBACK = False
ENABLE_SEGMENT_LIVE_SQL_FALLBACK = False
if os.environ.get("ENABLE_SEGMENT_LIVE_SQL_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on"):
    ENABLE_SEGMENT_LIVE_SQL_FALLBACK = True
SEGMENT_MEMBER_CACHE_TABLE = "coreiq_screening_segment_member_cache"
SEGMENT_VALUES_CACHE_TABLE = "coreiq_screening_segment_values_cache"
SEGMENT_CACHE_UNAVAILABLE_MESSAGE = (
    "Segment screening cache is not available. Please build the segment values cache "
    "or run this in slow fallback mode."
)

# ─── Preset segment member labels (hardcoded from real v4 dimension_member_label
#     values, same as used in the market data Segments tab). These are inserted
#     into SEGMENT_MEMBER_CACHE_TABLE on startup so segment member dropdowns load
#     in <5ms even before the full values cache is built.
_BIZ_MEMBER_PRESETS: List[Tuple[str, str, int]] = [
    # (segment_type, member_label, approx_company_count)
    ("business", "AWS",                         1),
    ("business", "Abercrombie",                 1),
    ("business", "Accessories",                 3),
    ("business", "Advertising",                 2),
    ("business", "Apparel",                     8),
    ("business", "Asia",                        2),
    ("business", "APAC Segment",                2),
    ("business", "Calvin Klein",                1),
    ("business", "Coach",                       1),
    ("business", "Converse",                    1),
    ("business", "Digital Advertising",         1),
    ("business", "Direct-to-Consumer",          4),
    ("business", "E-Commerce",                  3),
    ("business", "Equipment",                   4),
    ("business", "Footwear",                    6),
    ("business", "Google Cloud",                1),
    ("business", "Google Services",             1),
    ("business", "Hardware",                    2),
    ("business", "Hollister",                   1),
    ("business", "Kate Spade",                  1),
    ("business", "Menswear",                    2),
    ("business", "Microsoft Cloud",             1),
    ("business", "NIKE Brand",                  1),
    ("business", "Online stores",               1),
    ("business", "Other Bets",                  1),
    ("business", "Other businesses",            3),
    ("business", "Physical stores",             1),
    ("business", "Productivity and Business Processes", 1),
    ("business", "Sam's Club",                  1),
    ("business", "Services",                    3),
    ("business", "Software",                    2),
    ("business", "Stuart Weitzman",             1),
    ("business", "Tapestry",                    1),
    ("business", "Third-party seller services", 1),
    ("business", "Tommy Hilfiger",              1),
    ("business", "Walmart U.S.",                1),
    ("business", "Walmart International",       1),
    ("business", "Wholesale",                   4),
    ("business", "Womenswear",                  2),
]
_GEO_MEMBER_PRESETS: List[Tuple[str, str, int]] = [
    ("geographical", "Africa",                              3),
    ("geographical", "Americas",                            8),
    ("geographical", "Americas Retail",                     1),
    ("geographical", "APAC Segment",                        3),
    ("geographical", "Asia",                                8),
    ("geographical", "Asia Pacific",                        5),
    ("geographical", "Asia-Pacific",                        4),
    ("geographical", "Australia",                           2),
    ("geographical", "Canada",                              4),
    ("geographical", "China",                               4),
    ("geographical", "Domestic",                            6),
    ("geographical", "EMEA",                                5),
    ("geographical", "Europe",                              12),
    ("geographical", "Europe, Middle East and Africa",      4),
    ("geographical", "Foreign",                             5),
    ("geographical", "France",                              2),
    ("geographical", "Germany",                             2),
    ("geographical", "Greater China",                       3),
    ("geographical", "India",                               2),
    ("geographical", "International",                       15),
    ("geographical", "International Retail Stores and E-commerce Operations", 1),
    ("geographical", "Japan",                               5),
    ("geographical", "Latin America",                       4),
    ("geographical", "Middle East",                         2),
    ("geographical", "North America",                       12),
    ("geographical", "Other",                               6),
    ("geographical", "Rest of Asia Pacific",                3),
    ("geographical", "Rest of World",                       5),
    ("geographical", "Sam's Club",                          1),
    ("geographical", "South Korea",                         1),
    ("geographical", "United Kingdom",                      4),
    ("geographical", "United States",                       10),
    ("geographical", "Walmart U.S.",                        1),
    ("geographical", "Walmart International",               1),
]


def populate_segment_member_cache_from_presets() -> int:
    """Upsert the hardcoded preset segment member labels into the member cache.

    Called on startup so the 'Load Segment Members' dropdown works instantly
    even before the full values cache is built.  Returns the number of rows
    written.
    """
    try:
        ensure_segment_member_cache_table()
        all_presets = _BIZ_MEMBER_PRESETS + _GEO_MEMBER_PRESETS
        rows_written = 0
        batch: List[tuple] = []
        for seg_type, label, cnt in all_presets:
            normalized = label.lower().strip()
            batch.append((seg_type, label, normalized, cnt))

        if not batch:
            return 0

        # Individual upserts (small set — ~75 rows, run once on startup)
        upsert_sql = f"""
            INSERT INTO {SEGMENT_MEMBER_CACHE_TABLE}
                (segment_type, member_label, member_label_normalized, company_count)
            VALUES (:seg_type, :label, :norm, :cnt)
            ON DUPLICATE KEY UPDATE
                company_count = GREATEST(company_count, VALUES(company_count)),
                updated_at    = CURRENT_TIMESTAMP
        """
        rows_written = 0
        for seg_type, label, normalized, cnt in batch:
            db_manager.execute_insert(upsert_sql, {
                "seg_type": seg_type,
                "label": label,
                "norm": normalized,
                "cnt": cnt,
            })
            rows_written += 1
        log_info(f"[SCREENING] segment member cache: upserted {rows_written} preset labels")
        return rows_written
    except Exception as exc:
        log_error(f"[SCREENING] populate_segment_member_cache_from_presets failed: {exc}")
        return 0


# Background job state (module-level, survives across Streamlit reruns in same process)
import threading as _threading
_segment_cache_build_lock  = _threading.Lock()
_segment_cache_build_state = {"running": False, "progress": 0, "total": 0, "last_error": ""}


def get_segment_cache_build_state() -> dict:
    """Return a snapshot of the background cache-build state."""
    with _segment_cache_build_lock:
        return dict(_segment_cache_build_state)


def rebuild_segment_values_cache_async() -> bool:
    """Start the segment values cache build in a background thread.

    Uses a server-side MySQL INSERT...SELECT that runs entirely within the DB
    engine — no Python row iteration.  Returns False if already running.
    """
    with _segment_cache_build_lock:
        if _segment_cache_build_state["running"]:
            return False
        _segment_cache_build_state.update(running=True, progress=0, total=2, last_error="")

    def _run():
        try:
            from utils.constants import SEGMENT_GEO_AXES, SEGMENT_BUSINESS_AXES, SEGMENT_PRODUCT_AXES

            ensure_segment_values_cache_table()

            geo_axes_expr = " OR ".join(
                f"dimension LIKE '{ax}'" for ax in SEGMENT_GEO_AXES
            )
            biz_axes_expr = " OR ".join(
                f"dimension LIKE '{ax}'" for ax in SEGMENT_BUSINESS_AXES + SEGMENT_PRODUCT_AXES
            )

            # Step 1: business segments
            log_info("[SCREENING] rebuild_segment_cache: building business segments...")
            with _segment_cache_build_lock:
                _segment_cache_build_state["progress"] = 1

            biz_sql = f"""
                REPLACE INTO {SEGMENT_VALUES_CACHE_TABLE}
                    (ticker, segment_type, metric_key, member_label,
                     report_fiscal_year, value_mm)
                SELECT
                    ticker,
                    'business'  AS segment_type,
                    CASE
                        WHEN LOWER(original_label) REGEXP 'revenue|sales'          THEN 'Revenues'
                        WHEN LOWER(original_label) REGEXP 'operating.*(profit|income)' THEN 'Operating Profit Before Tax'
                        WHEN LOWER(original_label) REGEXP 'depreciation|amortization' THEN 'Depreciation & Amortization'
                        WHEN LOWER(original_label) REGEXP 'capital.expend|capex'    THEN 'Capital Expenditure'
                        WHEN LOWER(original_label) = 'assets'                      THEN 'Assets'
                        ELSE NULL
                    END                AS metric_key,
                    dimension_member_label AS member_label,
                    report_fiscal_year,
                    SUM(numeric_value) / 1000000 AS value_mm
                FROM coreiq_filing_metrics_v4
                WHERE is_dimensioned = 1
                  AND doc_type       = '10-K'
                  AND numeric_value  > 0
                  AND dimension_member_label IS NOT NULL
                  AND ({biz_axes_expr})
                GROUP BY ticker, metric_key, dimension_member_label, report_fiscal_year
                HAVING metric_key IS NOT NULL
            """
            db_manager.execute_insert(biz_sql)
            log_info("[SCREENING] rebuild_segment_cache: business segments done")

            # Step 2: geographical segments
            log_info("[SCREENING] rebuild_segment_cache: building geographical segments...")
            with _segment_cache_build_lock:
                _segment_cache_build_state["progress"] = 2

            geo_sql = f"""
                REPLACE INTO {SEGMENT_VALUES_CACHE_TABLE}
                    (ticker, segment_type, metric_key, member_label,
                     report_fiscal_year, value_mm)
                SELECT
                    ticker,
                    'geographical' AS segment_type,
                    CASE
                        WHEN LOWER(original_label) REGEXP 'revenue|sales'          THEN 'Revenues'
                        WHEN LOWER(original_label) REGEXP 'operating.*(profit|income)' THEN 'Operating Profit Before Tax'
                        WHEN LOWER(original_label) REGEXP 'depreciation|amortization' THEN 'Depreciation & Amortization'
                        WHEN LOWER(original_label) REGEXP 'capital.expend|capex'    THEN 'Capital Expenditure'
                        WHEN LOWER(original_label) = 'assets'                      THEN 'Assets'
                        ELSE NULL
                    END                AS metric_key,
                    dimension_member_label AS member_label,
                    report_fiscal_year,
                    SUM(numeric_value) / 1000000 AS value_mm
                FROM coreiq_filing_metrics_v4
                WHERE is_dimensioned = 1
                  AND doc_type       = '10-K'
                  AND numeric_value  > 0
                  AND dimension_member_label IS NOT NULL
                  AND ({geo_axes_expr})
                GROUP BY ticker, metric_key, dimension_member_label, report_fiscal_year
                HAVING metric_key IS NOT NULL
            """
            db_manager.execute_insert(geo_sql)
            log_info("[SCREENING] rebuild_segment_cache: geographical segments done")

            # Step 3: refresh member cache from values cache
            member_sql = f"""
                REPLACE INTO {SEGMENT_MEMBER_CACHE_TABLE}
                    (segment_type, member_label, member_label_normalized, company_count)
                SELECT
                    segment_type,
                    member_label,
                    LOWER(TRIM(member_label)) AS member_label_normalized,
                    COUNT(DISTINCT ticker)    AS company_count
                FROM {SEGMENT_VALUES_CACHE_TABLE}
                WHERE metric_key = 'Revenues'
                GROUP BY segment_type, member_label
            """
            db_manager.execute_insert(member_sql)
            log_info("[SCREENING] rebuild_segment_cache: member cache refreshed")

            with _segment_cache_build_lock:
                _segment_cache_build_state.update(running=False, progress=2, total=2, last_error="")

        except Exception as exc:
            err = str(exc)
            log_error(f"[SCREENING] rebuild_segment_cache FAILED: {err}")
            with _segment_cache_build_lock:
                _segment_cache_build_state.update(running=False, last_error=err)

    t = _threading.Thread(target=_run, daemon=True, name="seg-cache-rebuild")
    t.start()
    return True


def _segment_statement_type(criterion: Dict) -> str:
    """Return 'business' or 'geographical' from criterion."""
    stype = criterion.get("segment_type")
    if stype in ("business", "geographical"):
        return stype
    stmt = criterion.get("statement", "")
    if stmt == "Geographical Segments":
        return "geographical"
    return "business"


def _segment_row_section(row: Dict) -> str:
    """Classify a dimensioned row as 'business' or 'geo' (Segment tab rules)."""
    fdl = row.get("full_dimension_label") or ""
    heading = SegmentDataRepository._get_heading(fdl)
    return SegmentDataRepository._classify_heading(heading)


def _segment_member_raw(row: Dict) -> Optional[str]:
    member_raw = (row.get("dimension_member_label") or "").strip()
    if not member_raw:
        return None
    from utils.constants import SEGMENT_SKIP_MEMBERS
    if member_raw.lower().strip() in SEGMENT_SKIP_MEMBERS:
        return None
    member = SegmentDataRepository._title_case_member(member_raw)
    return member or None


def _segment_row_matches_metric(row: Dict, metric_key: str) -> bool:
    from utils.constants import SEGMENT_METRIC_GROUPS
    cfg = SEGMENT_METRIC_GROUPS.get(metric_key)
    if not cfg:
        return False
    if not SegmentDataRepository._is_monetary_row(row):
        return False
    orig_label = row.get("original_label") or ""
    return SegmentDataRepository._matches_metric(orig_label, cfg)


def _segment_axes_clause(segment_type: str, table_alias: str = "f") -> Tuple[str, Dict]:
    from utils.constants import (
        SEGMENT_BUSINESS_AXES,
        SEGMENT_GEO_AXES,
        SEGMENT_PRODUCT_AXES,
    )
    if segment_type == "geographical":
        axes = SEGMENT_GEO_AXES
    else:
        axes = SEGMENT_BUSINESS_AXES + SEGMENT_PRODUCT_AXES
    clause, params = SegmentDataRepository._build_dim_clause(axes)
    if table_alias:
        clause = clause.replace("dimension ", f"{table_alias}.dimension ")
    return clause, params


def _segment_year_filter_sql(year_sel: str) -> Tuple[str, Dict]:
    """SQL fragment + params for explicit fiscal year (not Latest)."""
    if year_sel and year_sel != "Latest":
        try:
            yr = int(year_sel)
            return " AND f.report_fiscal_year = :seg_year ", {"seg_year": yr}
        except (TypeError, ValueError):
            pass
    return "", {}


def _metric_label_sql(metric_key: str) -> Tuple[str, Dict]:
    """Build SQL include/exclude clauses for original_label from SEGMENT_METRIC_GROUPS."""
    from utils.constants import SEGMENT_METRIC_GROUPS

    cfg = SEGMENT_METRIC_GROUPS.get(metric_key) or SEGMENT_METRIC_GROUPS.get("Revenues", {})
    params: Dict = {}
    include_parts = []
    for i, kw in enumerate(cfg.get("db_include", ["revenue"])[:8]):
        key = f"inc{i}"
        params[key] = f"%{kw.lower()}%"
        include_parts.append(f"LOWER(f.original_label) LIKE :{key}")
    exclude_parts = []
    for i, kw in enumerate(cfg.get("db_exclude", [])[:12]):
        key = f"exc{i}"
        params[key] = f"%{kw.lower()}%"
        exclude_parts.append(f"LOWER(f.original_label) NOT LIKE :{key}")
    include_clause = "(" + " OR ".join(include_parts) + ")" if include_parts else "1=1"
    exclude_clause = " AND ".join(exclude_parts) if exclude_parts else "1=1"
    return include_clause, exclude_clause, params


def _selected_segments_sql(selected_segments: List[str]) -> Tuple[str, Dict]:
    if not selected_segments:
        return "", {}
    safe = []
    params: Dict = {}
    for i, seg in enumerate(selected_segments):
        if not seg:
            continue
        key = f"segm{i}"
        params[key] = seg.lower().strip()
        safe.append(f"LOWER(TRIM(f.dimension_member_label)) = :{key}")
    if not safe:
        return "", {}
    return " AND (" + " OR ".join(safe) + ") ", params


def ensure_segment_member_cache_table() -> bool:
    """Create cache table if missing (idempotent)."""
    ddl = f"""
        CREATE TABLE IF NOT EXISTS {SEGMENT_MEMBER_CACHE_TABLE} (
            segment_type VARCHAR(32) NOT NULL,
            member_label VARCHAR(512) NOT NULL,
            member_label_normalized VARCHAR(512) NOT NULL,
            company_count INT NOT NULL DEFAULT 0,
            latest_report_fiscal_year INT NULL,
            updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (segment_type, member_label_normalized)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    try:
        db_manager.execute_insert(ddl)
        return True
    except Exception as exc:
        log_error(f"[SCREENING] ensure_segment_member_cache_table failed: {exc}")
        return False


@st.cache_data(ttl=300, show_spinner=False)
def read_segment_member_options_cache(segment_type: str) -> List[Tuple[str, int]]:
    """Fast read of precomputed segment member options (<300ms target)."""
    t0 = time.perf_counter()
    sql = f"""
        SELECT member_label, company_count
        FROM {SEGMENT_MEMBER_CACHE_TABLE}
        WHERE segment_type = :segment_type
        ORDER BY company_count DESC, member_label ASC
        LIMIT {_SEGMENT_OPTIONS_LIMIT}
    """
    try:
        rows = db_manager.execute_query_readonly(sql, {"segment_type": segment_type}) or []
    except Exception:
        rows = []
    out = [(r["member_label"], int(r["company_count"] or 0)) for r in rows if r.get("member_label")]
    source = "member_cache"
    if not out:
        try:
            rows = db_manager.execute_query_readonly(
                f"""
                SELECT member_label, COUNT(DISTINCT ticker) AS company_count
                FROM {SEGMENT_VALUES_CACHE_TABLE}
                WHERE segment_type = :segment_type
                GROUP BY member_label
                ORDER BY company_count DESC, member_label ASC
                LIMIT {_SEGMENT_OPTIONS_LIMIT}
                """,
                {"segment_type": segment_type},
            ) or []
            out = [
                (r["member_label"], int(r["company_count"] or 0))
                for r in rows
                if r.get("member_label")
            ]
            source = "values_cache"
        except Exception as exc:
            log_error(f"[SCREENING] segment member values-cache fallback failed: {exc}")
    ms = (time.perf_counter() - t0) * 1000
    log_timing(
        "SCREENING_SEGMENT_OPTIONS_CACHE_HIT" if out else "SCREENING_SEGMENT_OPTIONS_CACHE_MISS",
        ms,
        f"type={segment_type} members={len(out)} source={source}",
    )
    return out


def ensure_segment_values_cache_table() -> bool:
    """Create screening-ready segment value cache (idempotent)."""
    ddl = f"""
        CREATE TABLE IF NOT EXISTS {SEGMENT_VALUES_CACHE_TABLE} (
            ticker VARCHAR(32) NOT NULL,
            segment_type VARCHAR(32) NOT NULL,
            metric_key VARCHAR(128) NOT NULL,
            member_label VARCHAR(512) NOT NULL,
            report_fiscal_year INT NOT NULL,
            value_mm DECIMAL(24, 6) NOT NULL,
            updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, segment_type, metric_key, member_label, report_fiscal_year),
            KEY idx_seg_metric_year_ticker (segment_type, metric_key, report_fiscal_year, ticker),
            KEY idx_seg_metric_year_member (segment_type, metric_key, report_fiscal_year, member_label),
            KEY idx_ticker_seg_metric_year (ticker, segment_type, metric_key, report_fiscal_year)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    try:
        db_manager.execute_insert(ddl)
        return True
    except Exception as exc:
        log_error(f"[SCREENING] ensure_segment_values_cache_table failed: {exc}")
        return False


@st.cache_data(ttl=60, show_spinner=False)
def get_segment_values_cache_status() -> dict:
    """Return health metadata for the screening segment values cache."""
    t0 = time.perf_counter()
    status = {
        "table_exists": False,
        "total_rows": 0,
        "business_rows_count": 0,
        "geographical_rows_count": 0,
        "distinct_tickers": 0,
        "distinct_metrics": 0,
        "min_report_fiscal_year": None,
        "max_report_fiscal_year": None,
        "last_updated_timestamp": None,
        "is_healthy": False,
    }
    try:
        exists_rows = db_manager.execute_query_readonly(
            """
            SELECT COUNT(*) AS table_count
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
            """,
            {"table_name": SEGMENT_VALUES_CACHE_TABLE},
        ) or []
        status["table_exists"] = bool(exists_rows and int(exists_rows[0].get("table_count") or 0))
        if not status["table_exists"]:
            return status

        rows = db_manager.execute_query_readonly(
            f"""
            SELECT
                COUNT(*) AS total_rows,
                SUM(CASE WHEN segment_type = 'business' THEN 1 ELSE 0 END) AS business_rows_count,
                SUM(CASE WHEN segment_type = 'geographical' THEN 1 ELSE 0 END) AS geographical_rows_count,
                COUNT(DISTINCT ticker) AS distinct_tickers,
                COUNT(DISTINCT metric_key) AS distinct_metrics,
                MIN(report_fiscal_year) AS min_report_fiscal_year,
                MAX(report_fiscal_year) AS max_report_fiscal_year,
                MAX(updated_at) AS last_updated_timestamp
            FROM {SEGMENT_VALUES_CACHE_TABLE}
            """
        ) or []
        if rows:
            row = rows[0]
            for key in (
                "total_rows",
                "business_rows_count",
                "geographical_rows_count",
                "distinct_tickers",
                "distinct_metrics",
            ):
                status[key] = int(row.get(key) or 0)
            status["min_report_fiscal_year"] = row.get("min_report_fiscal_year")
            status["max_report_fiscal_year"] = row.get("max_report_fiscal_year")
            updated = row.get("last_updated_timestamp")
            status["last_updated_timestamp"] = str(updated) if updated is not None else None
            status["is_healthy"] = bool(status["total_rows"] > 0)
        return status
    except Exception as exc:
        log_error(f"[SCREENING] get_segment_values_cache_status failed: {exc}")
        return status
    finally:
        log_timing(
            "SCREENING_SEGMENT_VALUES_CACHE_STATUS",
            (time.perf_counter() - t0) * 1000,
            f"table_exists={status['table_exists']} rows={status['total_rows']}",
        )


@st.cache_data(ttl=120, show_spinner=False)
def _segment_values_cache_has_rows(segment_type: str, metric_key: str) -> bool:
    try:
        rows = db_manager.execute_query_readonly(
            f"""
            SELECT 1 AS ok FROM {SEGMENT_VALUES_CACHE_TABLE}
            WHERE segment_type = :st AND metric_key = :mk
            LIMIT 1
            """,
            {"st": segment_type, "mk": metric_key},
        ) or []
        return bool(rows)
    except Exception:
        return False


def _segment_values_cache_scope_status(
    tickers: List[str],
    segment_type: str,
    metric_key: str,
    year_sel: str,
) -> Dict:
    """Check whether the values cache can answer this segment criterion."""
    t0 = time.perf_counter()
    out = {
        "available": False,
        "rows": 0,
        "distinct_tickers": 0,
        "min_report_fiscal_year": None,
        "max_report_fiscal_year": None,
        "elapsed_ms": 0.0,
    }
    if not tickers:
        return out

    year_clause = ""
    year_params: Dict = {}
    if year_sel and year_sel != "Latest":
        try:
            year_clause = " AND report_fiscal_year = :seg_year "
            year_params = {"seg_year": int(year_sel)}
        except (TypeError, ValueError):
            pass

    try:
        for i in range(0, len(tickers), _SEGMENT_VALUES_CACHE_CHUNK):
            chunk = tickers[i : i + _SEGMENT_VALUES_CACHE_CHUNK]
            ticker_sql = _build_ticker_in_list(chunk)
            rows = db_manager.execute_query_readonly(
                f"""
                SELECT
                    COUNT(*) AS row_count,
                    COUNT(DISTINCT ticker) AS ticker_count,
                    MIN(report_fiscal_year) AS min_report_fiscal_year,
                    MAX(report_fiscal_year) AS max_report_fiscal_year
                FROM {SEGMENT_VALUES_CACHE_TABLE}
                WHERE segment_type = :st
                  AND metric_key = :mk
                  AND ticker IN ({ticker_sql})
                  {year_clause}
                """,
                {"st": segment_type, "mk": metric_key, **year_params},
            ) or []
            if not rows:
                continue
            row = rows[0]
            row_count = int(row.get("row_count") or 0)
            if row_count <= 0:
                continue
            out["rows"] += row_count
            out["distinct_tickers"] += int(row.get("ticker_count") or 0)
            min_year = row.get("min_report_fiscal_year")
            max_year = row.get("max_report_fiscal_year")
            if min_year is not None:
                out["min_report_fiscal_year"] = (
                    min_year
                    if out["min_report_fiscal_year"] is None
                    else min(out["min_report_fiscal_year"], min_year)
                )
            if max_year is not None:
                out["max_report_fiscal_year"] = (
                    max_year
                    if out["max_report_fiscal_year"] is None
                    else max(out["max_report_fiscal_year"], max_year)
                )

        out["available"] = out["rows"] > 0
        return out
    except Exception as exc:
        log_error(f"[SCREENING] segment values cache scope check failed: {exc}")
        return out
    finally:
        out["elapsed_ms"] = (time.perf_counter() - t0) * 1000
        log_timing(
            "SCREENING_SEGMENT_VALUES_CACHE_HIT" if out["available"] else "SCREENING_SEGMENT_VALUES_CACHE_MISS",
            out["elapsed_ms"],
            f"type={segment_type} metric={metric_key} year={year_sel} "
            f"tickers={len(tickers)} matching_tickers={out['distinct_tickers']} "
            f"scope_rows={out['rows']}",
        )


def read_segment_values_cache(
    tickers: List[str],
    segment_type: str,
    metric_key: str,
    year_sel: str,
    selected_segments: Optional[List[str]] = None,
) -> List[Dict]:
    """Read precomputed latest segment values for screening (fast path)."""
    if not tickers:
        return []

    t0 = time.perf_counter()
    all_rows: List[Dict] = []
    year_clause = ""
    year_params: Dict = {}
    if year_sel and year_sel != "Latest":
        try:
            year_clause = " AND report_fiscal_year = :seg_year "
            year_params = {"seg_year": int(year_sel)}
        except (TypeError, ValueError):
            pass

    member_clause = ""
    member_params: Dict = {}
    if selected_segments:
        parts = []
        for i, seg in enumerate(selected_segments):
            if not seg:
                continue
            key = f"cm{i}"
            member_params[key] = seg.lower().strip()
            parts.append(f"LOWER(TRIM(member_label)) = :{key}")
        if parts:
            member_clause = " AND (" + " OR ".join(parts) + ") "

    for i in range(0, len(tickers), _SEGMENT_VALUES_CACHE_CHUNK):
        chunk = tickers[i : i + _SEGMENT_VALUES_CACHE_CHUNK]
        ticker_sql = _build_ticker_in_list(chunk)
        if year_clause:
            sql = f"""
                SELECT ticker, member_label, value_mm, report_fiscal_year
                FROM {SEGMENT_VALUES_CACHE_TABLE}
                WHERE segment_type = :st
                  AND metric_key = :mk
                  AND ticker IN ({ticker_sql})
                  {year_clause}
                  {member_clause}
            """
        else:
            sql = f"""
                SELECT c.ticker, c.member_label, c.value_mm, c.report_fiscal_year
                FROM {SEGMENT_VALUES_CACHE_TABLE} c
                INNER JOIN (
                    SELECT ticker, member_label, MAX(report_fiscal_year) AS report_fiscal_year
                    FROM {SEGMENT_VALUES_CACHE_TABLE}
                    WHERE segment_type = :st
                      AND metric_key = :mk
                      AND ticker IN ({ticker_sql})
                      {member_clause}
                    GROUP BY ticker, member_label
                ) latest ON latest.ticker = c.ticker
                    AND latest.member_label = c.member_label
                    AND latest.report_fiscal_year = c.report_fiscal_year
                WHERE c.segment_type = :st
                  AND c.metric_key = :mk
                  AND c.ticker IN ({ticker_sql})
            """
        params = {"st": segment_type, "mk": metric_key, **year_params, **member_params}
        try:
            rows = db_manager.execute_query_readonly(sql, params) or []
            all_rows.extend(rows)
        except Exception as exc:
            log_error(f"[SCREENING] read_segment_values_cache chunk failed: {exc}")
            return []

    ms = (time.perf_counter() - t0) * 1000
    log_timing(
        "SCREENING_SEGMENT_VALUES_CACHE_ROWS",
        ms,
        f"type={segment_type} metric={metric_key} year={year_sel} "
        f"tickers={len(tickers)} rows={len(all_rows)} "
        f"selected_members={len(selected_segments or [])}",
    )
    return all_rows


def _build_ticker_segment_values_from_cache(
    rows: List[Dict],
) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for row in rows:
        ticker = row.get("ticker")
        member = row.get("member_label")
        val = row.get("value_mm")
        if not ticker or not member or val is None:
            continue
        out.setdefault(ticker, {})[member] = float(val)
    return out


def _normalize_segment_options_rows(
    rows: List[Dict],
    segment_type: str,
) -> List[Tuple[str, int]]:
    """Normalize aggregated SQL rows to (label, company_count), applying skip rules."""
    want_geo = segment_type == "geographical"
    counts: Dict[str, int] = {}
    for row in rows:
        raw = (row.get("dimension_member_label") or "").strip()
        if not raw:
            continue
        member = _segment_member_raw({"dimension_member_label": raw})
        if not member:
            continue
        fdl = row.get("full_dimension_label") or ""
        if fdl:
            section = _segment_row_section({"full_dimension_label": fdl})
            if want_geo and section != "geo":
                continue
            if not want_geo and section != "business":
                continue
        cnt = int(row.get("company_count") or row.get("cnt") or 0)
        counts[member] = counts.get(member, 0) + cnt
    return sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))[:_SEGMENT_OPTIONS_LIMIT]


def _fetch_segment_options_aggregated_sql(
    tickers: List[str],
    segment_type: str,
    year_sel: str,
    timeout_ms: int = 3000,
) -> List[Dict]:
    """DB-side aggregation for member dropdown (no row-level metric values)."""
    if not tickers:
        return []
    dim_clause, dim_params = _segment_axes_clause(segment_type)
    year_clause, year_params = _segment_year_filter_sql(year_sel)
    include_clause, exclude_clause, metric_params = _metric_label_sql("Revenues")

    all_rows: List[Dict] = []
    hint = f"/*+ MAX_EXECUTION_TIME({timeout_ms}) */"
    for i in range(0, len(tickers), _SEGMENT_BULK_CHUNK):
        chunk = tickers[i : i + _SEGMENT_BULK_CHUNK]
        ticker_sql = _build_ticker_in_list(chunk)
        sql = f"""
            SELECT {hint}
                f.dimension_member_label,
                MAX(f.full_dimension_label) AS full_dimension_label,
                COUNT(DISTINCT f.ticker) AS company_count
            FROM coreiq_filing_metrics_v4 f
            WHERE f.ticker IN ({ticker_sql})
              AND f.is_dimensioned = 1
              AND f.numeric_value IS NOT NULL
              AND f.doc_type = '10-K'
              AND f.dimension_member_label IS NOT NULL
              AND TRIM(f.dimension_member_label) != ''
              AND ({dim_clause})
              AND ({include_clause})
              AND ({exclude_clause})
              {year_clause}
            GROUP BY f.dimension_member_label
            ORDER BY company_count DESC, f.dimension_member_label ASC
            LIMIT {_SEGMENT_OPTIONS_LIMIT}
        """
        try:
            rows = db_manager.execute_query_readonly(
                sql, {**dim_params, **year_params, **metric_params},
            ) or []
            all_rows.extend(rows)
        except Exception as exc:
            log_error(f"[SCREENING] segment options aggregated SQL failed: {exc}")
    return all_rows


def _bulk_fetch_segment_v4_rows(
    tickers: List[str],
    segment_type: str,
    year_sel: str,
    metric_key: str,
    selected_segments: Optional[List[str]] = None,
) -> List[Dict]:
    """Bulk-fetch segment rows with metric/year/member filters pushed into SQL."""
    if not tickers:
        return []
    dim_clause, dim_params = _segment_axes_clause(segment_type)
    year_clause, year_params = _segment_year_filter_sql(year_sel)
    include_clause, exclude_clause, metric_params = _metric_label_sql(metric_key)
    member_clause, member_params = _selected_segments_sql(selected_segments or [])
    use_latest = not year_clause
    hint = f"/*+ MAX_EXECUTION_TIME({_SEGMENT_STATEMENT_DB_TIMEOUT_MS}) */"

    def _fetch_chunk(chunk: List[str]) -> List[Dict]:
        ticker_sql = _build_ticker_in_list(chunk)
        latest_join = ""
        if use_latest:
            latest_join = f"""
            INNER JOIN (
                SELECT ticker, MAX(report_fiscal_year) AS latest_year
                FROM coreiq_filing_metrics_v4
                WHERE ticker IN ({ticker_sql})
                  AND is_dimensioned = 1
                  AND numeric_value IS NOT NULL
                  AND doc_type = '10-K'
                GROUP BY ticker
            ) latest ON latest.ticker = f.ticker
                AND latest.latest_year = f.report_fiscal_year
            """
        sql = f"""
            SELECT {hint}
                f.ticker, f.original_label, f.numeric_value, f.unit_ref,
                f.report_fiscal_year, f.dimension_member_label, f.full_dimension_label,
                f.dimension, f.period_type, f.period_start, f.period_end,
                f.period_instant, f.filing_date
            FROM coreiq_filing_metrics_v4 f
            {latest_join}
            WHERE f.ticker IN ({ticker_sql})
              AND f.is_dimensioned = 1
              AND f.numeric_value IS NOT NULL
              AND f.doc_type = '10-K'
              AND ({dim_clause})
              AND ({include_clause})
              AND ({exclude_clause})
              {year_clause}
              {member_clause}
            ORDER BY f.ticker ASC, f.report_fiscal_year DESC,
                     f.full_dimension_label ASC, f.original_label ASC, f.filing_date DESC
        """
        params = {**dim_params, **year_params, **metric_params, **member_params}
        try:
            return db_manager.execute_query_readonly(sql, params) or []
        except Exception as exc:
            log_error(
                f"[SCREENING] _bulk_fetch_segment_v4_rows failed chunk_size={len(chunk)}: {exc}"
            )
            raise RuntimeError(
                f"Segment data query failed for {len(chunk)} tickers: {exc}"
            ) from exc

    chunks = [tickers[i : i + _SEGMENT_BULK_CHUNK] for i in range(0, len(tickers), _SEGMENT_BULK_CHUNK)]
    all_rows: List[Dict] = []
    if len(chunks) == 1:
        all_rows.extend(_fetch_chunk(chunks[0]))
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
            for fut in as_completed({pool.submit(_fetch_chunk, c): c for c in chunks}):
                all_rows.extend(fut.result())
    return all_rows


def _resolve_segment_target_year(rows: List[Dict], year_sel: str) -> Optional[int]:
    if year_sel and year_sel != "Latest":
        try:
            return int(year_sel)
        except (TypeError, ValueError):
            return None
    years = []
    for r in rows:
        y = SegmentDataRepository._get_row_year(r)
        if y is not None:
            years.append(int(y))
    return max(years) if years else None


def _build_ticker_segment_values(
    rows: List[Dict],
    segment_type: str,
    metric_key: str,
    year_sel: str,
) -> Dict[str, Dict[str, float]]:
    """Return {ticker: {member_label: value_mm}} — SQL pre-filters metric/year."""
    want_geo = segment_type == "geographical"
    out: Dict[str, Dict[str, float]] = {}
    for row in rows:
        ticker = row.get("ticker")
        if not ticker:
            continue
        if _segment_row_section(row) != ("geo" if want_geo else "business"):
            continue
        if not SegmentDataRepository._is_monetary_row(row):
            continue
        member = _segment_member_raw(row)
        if not member:
            continue
        members = out.setdefault(ticker, {})
        if member in members:
            continue
        raw_val = row.get("numeric_value")
        if raw_val is None:
            continue
        members[member] = float(raw_val) / 1_000_000
    return {t: m for t, m in out.items() if m}


def _format_segment_value_mm(val: float) -> str:
    return f"{val:,.2f}"


def _format_segment_cell(
    member_values: Dict[str, float],
    selected_segments: List[str],
    all_segments_mode: bool,
) -> str:
    if not member_values:
        return "-"
    selected = [s for s in selected_segments if s in member_values]
    if selected:
        ordered = list(selected)
        remaining = sorted(
            [m for m in member_values if m not in set(ordered)],
            key=lambda m: (-member_values[m], m.lower()),
        )
        ordered.extend(remaining)
    elif all_segments_mode:
        ordered = sorted(member_values.keys(), key=lambda m: (-member_values[m], m.lower()))
    else:
        ordered = sorted(member_values.keys(), key=lambda m: m.lower())
    parts = [f"{m}: {_format_segment_value_mm(member_values[m])}" for m in ordered]
    return "; ".join(parts) if parts else "-"


def _ticker_passes_segment_filter(
    member_values: Dict[str, float],
    selected_segments: List[str],
    operator: str,
    threshold1: float,
    threshold2: float,
) -> bool:
    if not member_values:
        return False
    if selected_segments:
        candidates = [member_values[m] for m in selected_segments if m in member_values]
    else:
        candidates = list(member_values.values())
    if not candidates:
        return False
    return any(_apply_operator(v, operator, threshold1, threshold2) for v in candidates)


def get_segment_member_options(
    segment_type: str,
    ticker_tuple: tuple,
    period_type: str = "FY",
    year: str = "Latest",
    *,
    use_watchlist_exact: bool = True,
) -> List[Tuple[str, int]]:
    """Load segment member options (explicit UI action only — not for render path)."""
    t0 = time.perf_counter()
    tickers = list(ticker_tuple) if use_watchlist_exact else []
    cached = read_segment_member_options_cache(segment_type)
    if cached and not use_watchlist_exact:
        log_timing(
            "SCREENING_SEGMENT_OPTIONS_TOTAL",
            (time.perf_counter() - t0) * 1000,
            f"type={segment_type} source=global_cache members={len(cached)}",
        )
        return cached

    if not tickers:
        tickers = list(ticker_tuple)
    if not tickers:
        return cached or []

    result_holder: List[List[Tuple[str, int]]] = []
    err_holder: List[str] = []

    def _run():
        try:
            rows = _fetch_segment_options_aggregated_sql(
                tickers, segment_type, year, timeout_ms=3000,
            )
            result_holder.append(_normalize_segment_options_rows(rows, segment_type))
        except Exception as exc:
            err_holder.append(str(exc))

    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=1) as ex:
        fut = ex.submit(_run)
        try:
            fut.result(timeout=_SEGMENT_OPTIONS_UI_TIMEOUT_S)
        except Exception:
            log_timing(
                "SCREENING_SEGMENT_OPTIONS_TIMEOUT",
                (time.perf_counter() - t0) * 1000,
                f"type={segment_type} tickers={len(tickers)} year={year}",
            )
            return cached or []

    options = result_holder[0] if result_holder else (cached or [])
    ms = (time.perf_counter() - t0) * 1000
    log_timing(
        "SCREENING_SEGMENT_OPTIONS_DB_FETCH",
        ms,
        f"type={segment_type} tickers={len(tickers)} members={len(options)} "
        f"year={year} period={period_type} err={err_holder[0] if err_holder else ''}",
    )
    log_timing(
        "SCREENING_SEGMENT_OPTIONS_TOTAL",
        ms,
        f"type={segment_type} members={len(options)}",
    )
    return options


def apply_segment_statement_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Filter working set using Business/Geographical Segments statement criteria."""
    t_total = time.perf_counter()
    rows_in = len(working_df)

    stmt = criterion.get("statement", "")
    segment_type = _segment_statement_type(criterion)
    metric_info = criterion.get("metric_info") or {}
    metric_key = metric_info.get("metric_key") or criterion.get("metric_label", "Revenues")
    metric_label = metric_info.get("label", metric_key)
    operator = criterion.get("operator", "Greater Than")
    value1 = float(criterion.get("value1") or 0)
    value2 = float(criterion.get("value2") or 0)
    display_col = criterion.get("display_col", metric_label)
    year_sel = criterion.get("year", "Latest")
    selected_segments = list(criterion.get("selected_segments") or [])
    unit = metric_info.get("unit", "$mm")
    threshold1, threshold2 = value1, value2
    if unit == "$mm":
        pass  # user enters $mm; values stored in $mm
    elif unit == "%":
        threshold1, threshold2 = value1 / 100.0, value2 / 100.0

    tickers = list(working_df["ticker"].values)
    if not tickers:
        return working_df.copy(), {
            "type": "financial", "statement": stmt, "rows_in": rows_in,
            "rows_out": 0, "elapsed_ms": 0,
        }

    log_info(
        f"[TIMING] SCREENING_SEGMENT_STATEMENT_START | stmt={stmt} "
        f"metric={metric_label} type={segment_type} year={year_sel} "
        f"tickers={len(tickers)} selected_members={len(selected_segments)}"
    )

    t_db = time.perf_counter()
    rows: List[Dict] = []
    data_source = "values_cache"
    cache_scope = _segment_values_cache_scope_status(
        tickers, segment_type, metric_key, year_sel,
    )
    if cache_scope.get("available"):
        rows = read_segment_values_cache(
            tickers, segment_type, metric_key, year_sel, selected_segments,
        )
    elif ENABLE_SEGMENT_LIVE_SQL_FALLBACK:
        data_source = "v4_live_sql_fallback"
        log_timing(
            "SCREENING_SEGMENT_LIVE_SQL_FALLBACK_ENABLED",
            (time.perf_counter() - t_db) * 1000,
            f"stmt={stmt} metric={metric_key} type={segment_type} tickers={len(tickers)}",
        )
        rows = _bulk_fetch_segment_v4_rows(
            tickers,
            segment_type,
            year_sel,
            metric_key,
            selected_segments=selected_segments,
        )
    else:
        # NON-FILTERING GRACEFUL DEGRADE — the values cache cannot answer this
        # scope (fresh deployment with an empty cache, or a metric/year the cache
        # doesn't cover). NEVER crash the results page: return every company with
        # no segment value so it renders as N/A and the full universe is
        # preserved, exactly like every other criterion. If the cache is
        # completely empty (fresh deploy), kick off a one-time background build so
        # subsequent screens get real values automatically; a populated-but-scope-
        # missing cache means the data simply isn't in v4 — degrade silently.
        data_source = "cache_unavailable"
        rows = []
        try:
            overall = get_segment_values_cache_status()
            if int(overall.get("total_rows", 0) or 0) == 0:
                rebuild_segment_values_cache_async()  # no-op if already running
        except Exception as _bx:
            log_error(f"[SCREENING] auto segment cache rebuild check failed: {_bx}")
        log_timing(
            "SCREENING_SEGMENT_CACHE_UNAVAILABLE_DEGRADE",
            (time.perf_counter() - t_db) * 1000,
            f"stmt={stmt} metric={metric_key} type={segment_type} "
            f"tickers={len(tickers)} -> N/A for all (universe preserved)",
        )
    ms_db = (time.perf_counter() - t_db) * 1000
    log_timing(
        "SEGMENT_SQL_TOTAL",
        ms_db,
        f"source={data_source} stmt={stmt} tickers={len(tickers)} rows={len(rows)} "
        f"metric={metric_key} selected_members={len(selected_segments)} year={year_sel}",
    )
    log_timing(
        "SEGMENT_ROWS_FETCHED",
        ms_db,
        f"rows={len(rows)} source={data_source}",
    )

    t_build = time.perf_counter()
    if data_source == "values_cache":
        ticker_values = _build_ticker_segment_values_from_cache(rows)
    else:
        ticker_values = _build_ticker_segment_values(rows, segment_type, metric_key, year_sel)
    ms_build = (time.perf_counter() - t_build) * 1000
    log_timing(
        "SEGMENT_BUILD_VALUES",
        ms_build,
        f"tickers_with_data={len(ticker_values)} source={data_source}",
    )

    fallback_count = 0
    missing_tickers = [t for t in tickers if t not in ticker_values]
    if ENABLE_SEGMENT_REPOSITORY_FALLBACK and missing_tickers and len(missing_tickers) <= 25:
        from datetime import date as _date
        end = _date.today()
        start = _date(end.year - 8, 1, 1)
        for t in missing_tickers:
            try:
                data = SegmentDataRepository.get_segment_data(
                    t, start, end, period_type="annual",
                )
                seg_key = "geo_segments" if segment_type == "geographical" else "business_segments"
                seg_tables = data.get(seg_key) or {}
                metric_map = seg_tables.get(metric_key) or {}
                if not metric_map:
                    continue
                target_year = None
                if year_sel and year_sel != "Latest":
                    target_year = int(year_sel)
                else:
                    years = data.get("years") or []
                    target_year = max(years) if years else None
                if target_year is None:
                    continue
                members: Dict[str, float] = {}
                for member, year_vals in metric_map.items():
                    v = year_vals.get(target_year)
                    if v is not None:
                        members[member] = float(v)
                if members:
                    ticker_values[t] = members
                    fallback_count += 1
            except Exception as exc:
                log_error(f"[SCREENING] segment fallback failed ticker={t}: {exc}")

    t_filter = time.perf_counter()
    passing_display: Dict[str, str] = {}
    companies_with_data = 0
    for ticker in tickers:
        mv = ticker_values.get(ticker)
        if not mv:
            continue
        companies_with_data += 1
        if _ticker_passes_segment_filter(mv, selected_segments, operator, threshold1, threshold2):
            passing_display[ticker] = _format_segment_cell(
                mv, selected_segments, all_segments_mode=not selected_segments,
            )

    filtered = working_df[working_df["ticker"].isin(passing_display.keys())].copy()
    filtered[display_col] = filtered["ticker"].map(passing_display)
    # Store raw {segment: value_mm} dict for expanded table rendering in UI
    filtered[f"{display_col}__raw"] = filtered["ticker"].map(
        lambda tk: {k: round(v, 2) for k, v in (ticker_values.get(tk) or {}).items()}
    )
    # Store the actual fiscal year used
    filtered[f"{display_col}__year"] = str(year_sel)
    rows_out = len(filtered)
    ms_filter = (time.perf_counter() - t_filter) * 1000
    ms_total = (time.perf_counter() - t_total) * 1000

    log_timing(
        "SEGMENT_FILTER_TOTAL",
        ms_filter,
        f"passing={len(passing_display)} with_data={companies_with_data} rows_out={rows_out}",
    )
    log_timing(
        "SCREENING_SEGMENT_STATEMENT_TOTAL",
        ms_total,
        f"stmt={stmt} metric={metric_label} tickers={len(tickers)} "
        f"passing={len(passing_display)} fallback={fallback_count} rows_in={rows_in} rows_out={rows_out} "
        f"source={data_source} cache_scope_rows={cache_scope.get('rows', 0)}",
    )

    return filtered, {
        "type":              "financial",
        "statement":         stmt,
        "segment_type":      segment_type,
        "metric":            metric_label,
        "rows_in":           rows_in,
        "rows_out":          rows_out,
        "passing":           len(passing_display),
        "with_segment_data": companies_with_data,
        "fallback_count":    fallback_count,
        "elapsed_ms":        ms_total,
        "db_ms":             ms_db,
        "data_source":       data_source,
        "cache_status":      (
            "HIT" if cache_scope.get("available")
            else ("UNAVAILABLE_DEGRADED" if data_source == "cache_unavailable" else "MISS")
        ),
        "cache_rows":        len(rows) if data_source == "values_cache" else 0,
        "cache_scope_rows":  cache_scope.get("rows", 0),
    }


def apply_business_segments_statement_criterion(
    criterion: Dict, working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    criterion = {**criterion, "statement": "Business Segments", "segment_type": "business"}
    return apply_segment_statement_criterion(criterion, working_df)


def apply_geographical_segments_statement_criterion(
    criterion: Dict, working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    criterion = {**criterion, "statement": "Geographical Segments", "segment_type": "geographical"}
    return apply_segment_statement_criterion(criterion, working_df)


# =============================================================================
# LEGACY OVERVIEW SNAPSHOT CRITERION (retained for reference; not used for Key Stats/Ratios)
# =============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_overview_values(ticker_tuple: tuple, metric_info: dict) -> Dict[str, Optional[float]]:
    """Fetch Key Stats / Ratio metric values from company overview tables.

    Uses coreiq_av_company_overview for SEC companies and
    coreiq_yf_company_overview (payload_json) for YF companies.
    Returns {ticker: float_value}.
    """
    tickers = list(ticker_tuple)
    if not tickers:
        return {}

    companies_map = CompanyRepository.get_companies_map()
    sec_tickers = [t for t in tickers if companies_map.get(t, {}).get("source") == "SEC"]
    yf_tickers  = [t for t in tickers if companies_map.get(t, {}).get("source") == "YFinance"]

    results: Dict[str, Optional[float]] = {}

    # ── SEC: direct columns or raw_json JSON extract ──
    if sec_tickers:
        sec_sql = _build_ticker_in_list(sec_tickers)
        sec_col  = metric_info.get("sec_col")
        json_key = metric_info.get("sec_json_key")

        if sec_col:
            try:
                rows = db_manager.execute_query_readonly(f"""
                    SELECT ticker, {sec_col} AS val
                    FROM coreiq_av_company_overview
                    WHERE ticker IN ({sec_sql})
                      AND {sec_col} IS NOT NULL
                """)
                for r in (rows or []):
                    try:
                        results[r["ticker"]] = float(r["val"])
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                log_error(f"[SCREENING] overview SEC col failed: {exc}")
        elif json_key:
            try:
                rows = db_manager.execute_query_readonly(f"""
                    SELECT ticker, JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.{json_key}')) AS val
                    FROM coreiq_av_company_overview
                    WHERE ticker IN ({sec_sql})
                """)
                for r in (rows or []):
                    try:
                        v = r.get("val")
                        if v and v not in ("None", "null", "NULL", ""):
                            results[r["ticker"]] = float(v)
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                log_error(f"[SCREENING] overview SEC json failed: {exc}")

    # ── YF: payload_json field ──
    if yf_tickers:
        yf_sql   = _build_ticker_in_list(yf_tickers)
        yf_item  = metric_info.get("yf_item")
        if yf_item:
            try:
                rows = db_manager.execute_query_readonly(f"""
                    SELECT ticker,
                           JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.info.{yf_item}')) AS val
                    FROM coreiq_yf_company_overview
                    WHERE ticker IN ({yf_sql})
                """)
                for r in (rows or []):
                    try:
                        v = r.get("val")
                        if v and v not in ("None", "null", "NULL", ""):
                            results[r["ticker"]] = float(v)
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                log_error(f"[SCREENING] overview YF json failed: {exc}")

    return results


def apply_overview_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Apply a Key Stats / Ratios criterion to working_df.

    Fetches values from company overview tables (point-in-time snapshots).
    Companies without data for the metric are excluded (hard filter).
    """
    t_total = time.perf_counter()
    rows_in = len(working_df)

    metric_info  = criterion["metric_info"]
    operator     = criterion["operator"]
    value1       = float(criterion["value1"])
    value2       = float(criterion.get("value2") or 0.0)
    display_col  = criterion.get("display_col", metric_info["label"])
    unit         = metric_info.get("unit", "x")

    tickers = list(working_df["ticker"].values)
    if not tickers:
        return pd.DataFrame(columns=list(working_df.columns) + [display_col]), {
            "type": "financial", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0,
        }

    ticker_tuple = tuple(sorted(tickers))
    raw_values   = _fetch_overview_values(ticker_tuple, metric_info)

    # Scale: market cap stored as raw dollars → convert to $mm
    if metric_info.get("label") == "Market Cap":
        raw_values = {t: v / DB_SCALE for t, v in raw_values.items() if v is not None}
    # Percentages: some fields store as fraction (0.05 = 5%) → multiply by 100
    if unit == "%":
        raw_values = {t: v * 100 if abs(v) < 50 else v
                      for t, v in raw_values.items() if v is not None}

    # Operator filter
    passing: Dict[str, float] = {}
    for ticker, val in raw_values.items():
        if val is None:
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        if _apply_operator(fv, operator, value1, value2):
            passing[ticker] = round(fv, 4)

    filtered = working_df[working_df["ticker"].isin(passing.keys())].copy()
    filtered[display_col] = filtered["ticker"].map(passing)
    rows_out = len(filtered)
    ms_total = (time.perf_counter() - t_total) * 1000

    return filtered, {
        "type":       "financial",
        "statement":  criterion.get("statement"),
        "metric":     metric_info["label"],
        "rows_in":    rows_in,
        "rows_out":   rows_out,
        "elapsed_ms": ms_total,
    }


# =============================================================================
# ESTIMATES CRITERION  (analyst consensus — coreiq_*_earnings_estimates)
# =============================================================================

@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_estimates_bulk(
    ticker_tuple: tuple,
    av_col: str,
    yf_type: str,
    yf_metric: str,
    period_type: str,
) -> Dict[str, float]:
    """Bulk-fetch one estimate metric for all tickers (cached in memory).

    AV companies: wide table, latest estimate_date per ticker for the horizon.
    YF companies: long table, latest run_id, the (estimate_type, metric, period_label).
    Returns {ticker: value} (raw — caller scales).
    """
    tickers = list(ticker_tuple)
    if not tickers:
        return {}

    companies_map = CompanyRepository.get_companies_map()
    sec_tickers = [t for t in tickers if companies_map.get(t, {}).get("source") == "SEC"]
    yf_tickers  = [t for t in tickers if companies_map.get(t, {}).get("source") == "YFinance"]

    is_quarter = period_type in ("CQ", "FQ")
    av_horizon = "next fiscal quarter" if is_quarter else "next fiscal year"
    yf_period  = "+1q" if is_quarter else "+1y"

    out: Dict[str, float] = {}

    # ── AV (SEC) — latest estimate_date per ticker for the horizon ──
    if sec_tickers and av_col:
        sec_sql = _build_ticker_in_list(sec_tickers)
        try:
            rows = db_manager.execute_query_readonly(f"""
                SELECT e.ticker, MAX(e.{av_col}) AS val
                FROM coreiq_av_financials_earnings_estimates e
                INNER JOIN (
                    SELECT ticker, MAX(estimate_date) AS md
                    FROM coreiq_av_financials_earnings_estimates
                    WHERE ticker IN ({sec_sql})
                      AND LOWER(horizon) = '{av_horizon}'
                    GROUP BY ticker
                ) latest ON e.ticker = latest.ticker AND e.estimate_date = latest.md
                WHERE LOWER(e.horizon) = '{av_horizon}'
                  AND e.{av_col} IS NOT NULL
                GROUP BY e.ticker
            """)
            for r in (rows or []):
                try:
                    out[r["ticker"]] = float(r["val"])
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            log_error(f"[SCREENING] estimates AV bulk failed: {exc}")

    # ── YF — latest run_id per ticker, long format ──
    if yf_tickers and yf_type and yf_metric:
        yf_sql = _build_ticker_in_list(yf_tickers)
        try:
            rows = db_manager.execute_query_readonly(f"""
                SELECT e.ticker, e.value AS val
                FROM coreiq_yf_financials_earnings_estimates e
                INNER JOIN (
                    SELECT ticker, MAX(run_id) AS rid
                    FROM coreiq_yf_financials_earnings_estimates
                    WHERE ticker IN ({yf_sql})
                    GROUP BY ticker
                ) latest ON e.ticker = latest.ticker AND e.run_id = latest.rid
                WHERE e.estimate_type = '{yf_type}'
                  AND e.metric        = '{yf_metric}'
                  AND e.period_label  = '{yf_period}'
                  AND e.value IS NOT NULL
            """)
            for r in (rows or []):
                try:
                    out[r["ticker"]] = float(r["val"])
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            log_error(f"[SCREENING] estimates YF bulk failed: {exc}")

    return out


def apply_estimates_criterion(
    criterion: Dict, working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Annotate working_df with an analyst-estimate metric (non-filtering)."""
    t_total = time.perf_counter()
    rows_in = len(working_df)
    mi = criterion["metric_info"]
    display_col = criterion.get("display_col", mi["label"])
    unit = mi.get("unit", "$mm")
    period_type = criterion.get("period_type", "FY")
    year_str = criterion.get("year", "Latest")

    tickers = list(working_df["ticker"].values)
    if not tickers:
        out = working_df.copy(); out[display_col] = None
        return out, {"type": "financial", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0}

    # Analyst estimates are a relative horizon (current / next fiscal period) — the
    # source tables carry no absolute target year beyond "next fiscal year". Honour
    # the selected year only up to the next fiscal year; anything further out has no
    # estimate data, so show N/A (non-filtering) rather than mislabel next-FY data.
    _cur_fy = date.today().year
    try:
        _yr = None if (not year_str or year_str == "Latest") else int(year_str)
    except (TypeError, ValueError):
        _yr = None
    if _yr is not None and _yr > _cur_fy + 1:
        out = working_df.copy(); out[display_col] = None
        return out, {
            "type": "financial", "statement": "Estimates", "metric": mi["label"],
            "rows_in": rows_in, "rows_out": rows_in, "with_data": 0,
            "no_estimate_for_year": _yr,
            "elapsed_ms": (time.perf_counter() - t_total) * 1000,
        }

    vals = _fetch_estimates_bulk(
        tuple(sorted(tickers)),
        mi.get("av_col", ""), mi.get("yf_type", ""), mi.get("yf_metric", ""),
        period_type,
    )
    if mi.get("scale_mm"):
        vals = {t: v / DB_SCALE for t, v in vals.items() if v is not None}

    out = working_df.copy()
    out[display_col] = out["ticker"].map(lambda t: round(vals[t], 4) if t in vals else None)
    with_data = int(out[display_col].notna().sum())
    return out, {
        "type": "financial", "statement": "Estimates", "metric": mi["label"],
        "rows_in": rows_in, "rows_out": rows_in, "with_data": with_data,
        "elapsed_ms": (time.perf_counter() - t_total) * 1000,
    }


# =============================================================================
# FORECASTING CRITERION  (revenue model forecasts — coreiq_model_forecasts)
# =============================================================================

@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_forecast_bulk(
    ticker_tuple: tuple,
    model_key: str,
    year: Optional[int],
) -> Dict[str, float]:
    """Bulk-fetch revenue forecast (value_millions) for all tickers (cached).

    coreiq_model_forecasts stores YF tickers as composite (e.g. 'ADS.DE'); we
    normalise to the base ticker so the working-set ticker matches.
    year=None → each ticker's nearest (min) forecast fiscal_year.
    Returns {base_ticker: value_mm}.
    """
    tickers = list(ticker_tuple)
    if not tickers or not model_key:
        return {}

    # Build a set of candidate DB tickers: base + composite forms
    companies_map = CompanyRepository.get_companies_map()
    db_to_base: Dict[str, str] = {}
    for t in tickers:
        db_to_base[t] = t
        info = companies_map.get(t, {})
        if info.get("source") == "YFinance":
            exch = info.get("exchange_acronym")
            if exch:
                db_to_base[f"{t}.{exch}"] = t
        # also map any composite already containing '.'
    db_tickers = list(db_to_base.keys())
    db_sql = _build_ticker_in_list(db_tickers)

    try:
        if year:
            rows = db_manager.execute_query_readonly(f"""
                SELECT ticker, value_millions AS val
                FROM coreiq_model_forecasts
                WHERE ticker IN ({db_sql})
                  AND metric = 'total_revenue'
                  AND model_key = '{model_key}'
                  AND fiscal_year = {int(year)}
                  AND value_millions IS NOT NULL
            """)
            out: Dict[str, float] = {}
            for r in (rows or []):
                base = db_to_base.get(r["ticker"], r["ticker"])
                try:
                    out[base] = float(r["val"])
                except (TypeError, ValueError):
                    pass
            return out
        else:
            # Nearest (min) forecast year per ticker
            rows = db_manager.execute_query_readonly(f"""
                SELECT f.ticker, f.value_millions AS val
                FROM coreiq_model_forecasts f
                INNER JOIN (
                    SELECT ticker, MIN(fiscal_year) AS fy
                    FROM coreiq_model_forecasts
                    WHERE ticker IN ({db_sql})
                      AND metric = 'total_revenue'
                      AND model_key = '{model_key}'
                    GROUP BY ticker
                ) nearest ON f.ticker = nearest.ticker AND f.fiscal_year = nearest.fy
                WHERE f.metric = 'total_revenue'
                  AND f.model_key = '{model_key}'
                  AND f.value_millions IS NOT NULL
            """)
            out = {}
            for r in (rows or []):
                base = db_to_base.get(r["ticker"], r["ticker"])
                try:
                    out[base] = float(r["val"])
                except (TypeError, ValueError):
                    pass
            return out
    except Exception as exc:
        log_error(f"[SCREENING] forecast bulk failed: {exc}")
        return {}


def apply_forecast_criterion(
    criterion: Dict, working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Annotate working_df with a revenue forecast metric (non-filtering)."""
    t_total = time.perf_counter()
    rows_in = len(working_df)
    mi = criterion["metric_info"]
    display_col = criterion.get("display_col", mi["label"])
    year_str = criterion.get("year", "Latest")
    year = None if (not year_str or year_str == "Latest") else int(year_str)

    tickers = list(working_df["ticker"].values)
    if not tickers:
        out = working_df.copy(); out[display_col] = None
        return out, {"type": "financial", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0}

    vals = _fetch_forecast_bulk(tuple(sorted(tickers)), mi.get("model_key", ""), year)
    out = working_df.copy()
    out[display_col] = out["ticker"].map(lambda t: round(vals[t], 4) if t in vals else None)
    with_data = int(out[display_col].notna().sum())
    return out, {
        "type": "financial", "statement": "Forecasting", "metric": mi["label"],
        "rows_in": rows_in, "rows_out": rows_in, "with_data": with_data,
        "elapsed_ms": (time.perf_counter() - t_total) * 1000,
    }


# =============================================================================
# KEY DEVELOPMENTS CRITERION
# =============================================================================

_KEYDEV_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_keydev_iso_date(value) -> Optional[str]:
    """Return YYYY-MM-DD or None (guards SQL date literals)."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        s = value.isoformat()
    else:
        s = str(value).strip()[:10]
    return s if _KEYDEV_ISO_DATE.match(s) else None


def _build_keydev_date_clause(criterion: Dict) -> str:
    """Build SQL AND-clause for event_date from a keydevs criterion dict."""
    mode = criterion.get("date_filter_mode", "timeframe")
    if mode == "date_range":
        start = _safe_keydev_iso_date(criterion.get("start_date"))
        end = _safe_keydev_iso_date(criterion.get("end_date"))
        parts = []
        if start:
            parts.append(f"AND event_date >= '{start}'")
        if end:
            parts.append(f"AND event_date <= '{end}'")
        return " ".join(parts)

    days = criterion.get("days")
    if days is not None:
        return f"AND event_date >= DATE_SUB(CURDATE(), INTERVAL {int(days)} DAY)"
    return ""


def resolve_keydevs_event_window(keydev_criteria: List[Dict]) -> Dict:
    """Merge date filters across keydev criteria for the results events table.

    Timeframe criteria use the widest (max) day window. Date-range criteria use
    the earliest start and latest end. Mixed criteria expand to cover both.
    """
    if not keydev_criteria:
        return {"days": 365}

    starts: List[str] = []
    ends: List[str] = []
    max_days = 0
    has_all_history = False

    for kc in keydev_criteria:
        mode = kc.get("date_filter_mode", "timeframe")
        if mode == "date_range":
            s = _safe_keydev_iso_date(kc.get("start_date"))
            e = _safe_keydev_iso_date(kc.get("end_date"))
            if s:
                starts.append(s)
            if e:
                ends.append(e)
        else:
            days = kc.get("days")
            if days is None:
                has_all_history = True
            else:
                max_days = max(max_days, int(days))

    if has_all_history and not starts and not ends:
        return {"days": None}

    today = date.today()
    if starts or ends:
        start = min(starts) if starts else None
        end = max(ends) if ends else None
        if max_days and not has_all_history:
            tf_start = (today - timedelta(days=max_days)).isoformat()
            if start is None or tf_start < start:
                start = tf_start
            if end is None:
                end = today.isoformat()
        return {
            "days": None,
            "start_date": start,
            "end_date": end or today.isoformat(),
        }

    if has_all_history:
        return {"days": None}
    return {"days": max_days or None}


def apply_keydevs_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Filter working_df to companies that have key development events
    matching the selected categories and timeframe.

    Reads from coreiq_company_events (canonical events table).
    Optionally attaches a CIQ-style result column with the latest event.
    """
    rows_in = len(working_df)

    categories = criterion.get("categories", [])    # list of exact DB event_category values
    show_headline = criterion.get("show_headline", False)

    # Simple exact match - categories are already exact DB values from KEYDEV_CATEGORIES_5MAIN
    query_cats = categories

    if not categories:
        return working_df.copy(), {
            "type": "keydevs", "rows_in": rows_in, "rows_out": rows_in,
            "elapsed_ms": 0,
        }

    tickers = list(working_df["ticker"].values)
    if not tickers:
        return working_df.copy(), {
            "type": "keydevs", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0,
        }

    ticker_sql = _build_ticker_in_list(tickers)
    cat_sql = ", ".join(f"'{c}'" for c in query_cats)

    date_clause = _build_keydev_date_clause(criterion)

    # Step 1+2 merged: single ROW_NUMBER() query replaces filter + headline queries
    _MAX_EVENTS_PER_COMPANY = 10

    if show_headline:
        # Single ROW_NUMBER() query replaces 2 separate queries
        # Benchmark: 419ms vs 4,404ms combined on Azure
        combined_query = f"""
            SELECT ticker, event_date, event_subtype, event_category, headline
            FROM (
                SELECT ticker, event_date, event_subtype, event_category, headline,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker
                           ORDER BY event_date DESC, event_id DESC
                       ) AS rn
                FROM coreiq_company_events
                WHERE ticker IN ({ticker_sql})
                  AND event_category IN ({cat_sql})
                  {date_clause}
            ) ranked
            WHERE rn <= {_MAX_EVENTS_PER_COMPANY}
        """
    else:
        combined_query = f"""
            SELECT DISTINCT ticker
            FROM coreiq_company_events
            WHERE ticker IN ({ticker_sql})
              AND event_category IN ({cat_sql})
              {date_clause}
        """

    try:
        combined_rows = db_manager.execute_query_readonly(combined_query)
    except Exception as exc:
        log_error(f"[SCREENING] keydevs combined query failed: {exc}")
        combined_rows = []

    # Derive matched tickers and event map from single result
    matched_tickers = {r["ticker"] for r in combined_rows}
    event_map: Dict[str, str] = {}

    if show_headline and combined_rows:
        from collections import defaultdict
        ticker_events: Dict[str, list] = defaultdict(list)
        for r in combined_rows:
            ticker_events[r["ticker"]].append(r)

        for t, events in ticker_events.items():
            lines = []
            for r in events:
                ev_date = r.get("event_date")
                subtype = r.get("event_subtype") or KEYDEV_CATEGORIES.get(
                    r.get("event_category"), "Key Development"
                )
                headline = (r.get("headline") or "")[:120]
                if ev_date:
                    date_str = f"{ev_date.month}/{ev_date.day}/{ev_date.year}"
                else:
                    date_str = "—"
                line1 = f"{date_str}  ({subtype})"
                entry = f"{line1}\n{headline}" if headline else line1
                lines.append(entry)
            event_map[t] = "\n\n".join(lines)

    # Step 3: filter working_df
    filtered = working_df[working_df["ticker"].isin(matched_tickers)].copy()

    if show_headline:
        display_col = criterion.get("display_col", "Key Developments")
        filtered[display_col] = filtered["ticker"].map(
            lambda t: event_map.get(t, "")
        )

    rows_out = len(filtered)

    return filtered, {
        "type":        "keydevs",
        "rows_in":     rows_in,
        "rows_out":    rows_out,
        "matched":     len(matched_tickers),
        "elapsed_ms":  0,
        "phase_ms": {
            "db_filter":  0,
            "headline":   0,
            "df_join":    0,
        },
    }


# Human-readable source display names (CIQ-style)
_SOURCE_DISPLAY = {
    "8k":           "SEC Form 8-K",
    "edgartools":   "SEC Form 8-K",
    "earnings_cal": "Nasdaq Earnings Calendar",
    "estimates":    "Alpha Vantage Estimates",
    # "news" intentionally absent — handled by _resolve_source_display() below
}

# Whitelist of recognized professional wires/publications for news events.
# Only these appear by their own name. Everything else → "Other".
_KNOWN_NEWS_SOURCES = {
    "Business Wire", "Businesswire", "BusinessWire",
    "PR Newswire", "PRNewswire", "PR-Newswire",
    "GlobeNewswire", "Globe Newswire", "Globenewswire",
    "Reuters", "Bloomberg", "Associated Press", "AP News",
    "Dow Jones Newswires", "The Wall Street Journal",
    "Financial Times", "CNBC", "MarketWatch",
    "Seeking Alpha", "Benzinga", "Zacks",
    "TheStreet", "Motley Fool", "Investopedia",
    "S&P Global", "Morningstar",
    "Yahoo Finance", "Yahoo! Finance",
    "The Motley Fool", "MotleyFool",
    "CNBC.com", "Fox Business", "NBC News",
    "The New York Times", "New York Times",
    "Washington Post", "The Washington Post",
    "Forbes", "Fortune", "Fast Company",
    "TechCrunch", "Barron's", "Barrons",
    "Investor's Business Daily", "IBD",
    "Nasdaq.com", "StockAnalysis",
    "Simply Wall St", "The Street",
    "Proactive Investors", "GuruFocus",
    "Globe and Mail", "Financial Post",
}

# Pre-compute lowercase lookup for case-insensitive matching
_KNOWN_NEWS_SOURCES_LOWER = {s.lower(): s for s in _KNOWN_NEWS_SOURCES}


def _resolve_source_display(source_raw: str, source_detail: str) -> str:
    """Return display string for a source — full detail when available."""
    detail = (source_detail or "").strip()
    if detail and detail not in ("sec_edgar", "nasdaq", "alpha_vantage"):
        # Already has rich detail (new format from ETL) — show it directly
        return detail[:200]
    # Fallback to friendly name mapping
    mapped = _SOURCE_DISPLAY.get(source_raw)
    if mapped:
        return mapped
    if source_raw == "news":
        canonical = _KNOWN_NEWS_SOURCES_LOWER.get(detail.lower())
        return canonical if canonical else "Other"
    return source_raw or "—"





@st.cache_data(ttl=300, show_spinner=False)
def get_keydevs_events_for_tickers(
    tickers: tuple,
    categories: tuple,
    days: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """Return event-per-row DataFrame for Key Devs mode.

    Columns returned:
      Key Developments By Date, Key Developments by Type, Company Name(s),
      Key Development Headline, Summary,
      Key Development Sources, Source Reference
    """
    if not tickers or not categories:
        return pd.DataFrame()

    # Simple exact match - categories are exact DB event_category values
    query_cats = list(categories)

    ticker_sql = _build_ticker_in_list(tickers)
    cat_sql = ", ".join(f"'{c}'" for c in query_cats)
    date_clause = _build_keydev_date_clause({
        "date_filter_mode": "date_range" if (start_date or end_date) else "timeframe",
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
    })

    query = f"""
        SELECT
            e.event_date,
            e.event_subtype,
            e.event_category,
            e.ticker,
            c.name_coresight AS company_name,
            c.exchange_acronym,
            e.headline,
            e.situation,
            e.source,
            e.source_ref,
            e.source_detail,
            c.primary_industry_coresight AS industry
        FROM coreiq_company_events e
        LEFT JOIN coreiq_companies c ON e.ticker = c.ticker
        WHERE e.ticker IN ({ticker_sql})
          AND e.event_category IN ({cat_sql})
          {date_clause}
        ORDER BY e.event_date DESC, e.event_id DESC
        LIMIT 2000
    """
    try:
        rows = db_manager.execute_query_readonly(query)
    except Exception as exc:
        log_error(f"[SCREENING] get_keydevs_events_for_tickers failed: {exc}")
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    records = []
    for r in rows:
        ev_date = r.get("event_date")
        date_str = ev_date.strftime("%b-%d-%Y") if ev_date else "—"
        subtype = r.get("event_subtype") or KEYDEV_CATEGORIES.get(
            r.get("event_category"), "Key Development"
        )
        cname = r.get("company_name") or r.get("ticker", "")
        exch = r.get("exchange_acronym") or ""
        ticker = r.get("ticker", "")
        company_display = f"{cname} ({exch}:{ticker})" if exch else f"{cname} ({ticker})"
        source_raw = r.get("source", "")
        source_display = _resolve_source_display(source_raw, r.get("source_detail") or "")
        situation = r.get("situation") or "—"
        industry = r.get("industry") or ""
        if industry:
            industry = f"{industry} (Primary)"

        source_ref = r.get("source_ref") or ""
        records.append({
            "Key Developments By Date": date_str,
            "Key Developments by Type": subtype,
            "Company Name(s)": company_display,
            "Key Development Headline": r.get("headline") or "",
            "Summary": situation,
            "Key Development Sources": source_display,
            "Source Reference": source_ref,
        })

    return pd.DataFrame(records)


# =============================================================================
# SEGMENTS CRITERION  (Business Segments / Geographic Segments)
# =============================================================================

# ── Fast income-statement helpers ────────────────────────────────────────────
# Business/Geographic segments reuse the income-statement tables (fast, small)
# ── Segment v4 revenue helper ────────────────────────────────────────────────
# Used ONLY when specific geo/biz segment labels are selected (Steps 4/5).
# Accepts slower v4 query because the user explicitly chose specific segments.

@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_segment_revenue_v4(
    ticker_tuple: tuple,
    segment_labels_tuple: tuple,
    year: Optional[int],
    use_geo_axes: bool = False,
) -> Dict[str, float]:
    """Query coreiq_filing_metrics_v4 for segment revenue by selected dimension_member_labels.

    Fast approach: no subquery JOIN.
    - Specific year: single WHERE filter.
    - Latest: query last 3 years, pick latest year per ticker in Python.
    Returns {ticker: revenue_mm}.
    """
    from utils.constants import SEGMENT_GEO_AXES, SEGMENT_BUSINESS_AXES, SEGMENT_PRODUCT_AXES
    from datetime import date as _date

    tickers = list(ticker_tuple)
    labels = [l for l in segment_labels_tuple if l]
    if not tickers or not labels:
        return {}

    ticker_sql   = _build_ticker_in_list(tickers)
    safe_labels  = [lbl.replace("'", "''") for lbl in labels]
    label_in_sql = ", ".join(f"'{l}'" for l in safe_labels)

    axes = SEGMENT_GEO_AXES if use_geo_axes else (SEGMENT_BUSINESS_AXES + SEGMENT_PRODUCT_AXES)
    axis_or = " OR ".join(f"dimension LIKE '{ax}'" for ax in axes)

    # MAX_EXECUTION_TIME hint: 30s ceiling. Large working sets can exceed this on v4
    # (no index on dimension_member_label). If it times out we return {} and the
    # caller falls back to the IS-table total-revenue path.
    try:
        if year:
            query = f"""
                SELECT /*+ MAX_EXECUTION_TIME(30000) */
                    ticker, SUM(numeric_value) / {DB_SCALE} AS revenue_mm
                FROM coreiq_filing_metrics_v4
                WHERE ticker IN ({ticker_sql})
                  AND is_dimensioned = 1
                  AND doc_type = '10-K'
                  AND numeric_value > 0
                  AND dimension_member_label IN ({label_in_sql})
                  AND ({axis_or})
                  AND report_fiscal_year = {year}
                GROUP BY ticker
            """
            rows = db_manager.execute_query_readonly(query)
            return {r["ticker"]: float(r["revenue_mm"])
                    for r in (rows or []) if r.get("revenue_mm") is not None}
        else:
            min_year = _date.today().year - 3
            query = f"""
                SELECT /*+ MAX_EXECUTION_TIME(30000) */
                    ticker, report_fiscal_year,
                    SUM(numeric_value) / {DB_SCALE} AS revenue_mm
                FROM coreiq_filing_metrics_v4
                WHERE ticker IN ({ticker_sql})
                  AND is_dimensioned = 1
                  AND doc_type = '10-K'
                  AND numeric_value > 0
                  AND dimension_member_label IN ({label_in_sql})
                  AND ({axis_or})
                  AND report_fiscal_year >= {min_year}
                GROUP BY ticker, report_fiscal_year
                ORDER BY ticker, report_fiscal_year DESC
            """
            rows = db_manager.execute_query_readonly(query)
            latest: Dict[str, float] = {}
            for r in (rows or []):
                t = r["ticker"]
                if t not in latest and r.get("revenue_mm") is not None:
                    latest[t] = float(r["revenue_mm"])
            return latest

    except Exception as exc:
        log_error(f"[SCREENING] _fetch_segment_revenue_v4 failed (may be timeout for large set): {exc}")
        return {}


# ── Fast IS-table revenue path ────────────────────────────────────────────────
# instead of coreiq_filing_metrics_v4 (7.75M rows, no suitable composite index
# for ticker+is_dimensioned+doc_type — full scans take 90-430 seconds on Azure).
# Revenue from IS tables == sum of all segments, which is what the column shows.

@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_revenue_from_is(ticker_tuple: tuple) -> Dict[str, float]:
    """Return {ticker: latest_annual_total_revenue_mm} from income-statement tables.

    Uses the same fast SEC + YF parallel path as apply_financial_criterion.
    Returns in $mm (raw DB values / DB_SCALE).
    """
    tickers = list(ticker_tuple)
    if not tickers:
        return {}

    companies_map = CompanyRepository.get_companies_map()
    sec_tickers = [t for t in tickers if companies_map.get(t, {}).get("source") == "SEC"]
    yf_tickers  = [t for t in tickers if companies_map.get(t, {}).get("source") == "YFinance"]

    cfg = STATEMENT_CONFIG.get("Income Statement", {})
    results: Dict[str, float] = {}

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            if sec_tickers and cfg.get("sec_table"):
                futures["sec"] = pool.submit(
                    _query_sec_latest, sec_tickers, cfg, "total_revenue",
                    "total_revenue", None, "annual",
                )
            if yf_tickers and cfg.get("yf_table"):
                futures["yf"] = pool.submit(
                    _query_yf_latest, yf_tickers, cfg, "Total Revenue",
                    "Total Revenue", None, "annual",
                )

        for key, fut in futures.items():
            try:
                raw = fut.result()
                for tk, val in raw.items():
                    if val is not None:
                        # DB stores raw dollars; divide by DB_SCALE → $mm
                        results[tk] = float(val) / DB_SCALE
            except Exception as exc:
                log_error(f"[SCREENING] _fetch_revenue_from_is {key} failed: {exc}")
    except Exception as exc:
        log_error(f"[SCREENING] _fetch_revenue_from_is failed: {exc}")

    return results


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_additional_tickers(ticker_tuple: tuple, source_val: str) -> set:
    """Return tickers that have credit_rating or store_count rows (fast via idx_v2_source)."""
    ticker_sql = _build_ticker_in_list(list(ticker_tuple))
    try:
        rows = db_manager.execute_query_readonly(f"""
            SELECT DISTINCT ticker
            FROM coreiq_filing_metrics_v4
            WHERE ticker IN ({ticker_sql})
              AND source = '{source_val}'
        """)
        return {r["ticker"] for r in (rows or [])}
    except Exception as exc:
        log_error(f"[SCREENING] _fetch_additional_tickers failed: {exc}")
        return set()


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_additional_display_values(ticker_tuple: tuple, data_type: str) -> Dict[str, str]:
    """Return {ticker: display_value} for credit ratings or store counts."""
    ticker_sql = _build_ticker_in_list(list(ticker_tuple))
    try:
        if data_type == "credit_ratings":
            rows = db_manager.execute_query_readonly(f"""
                SELECT fmv.ticker, fmv.value AS rating_val
                FROM coreiq_filing_metrics_v4 fmv
                INNER JOIN (
                    SELECT ticker, MAX(report_fiscal_year) AS yr
                    FROM coreiq_filing_metrics_v4
                    WHERE ticker IN ({ticker_sql})
                      AND source = 'credit_rating'
                    GROUP BY ticker
                ) ly ON fmv.ticker = ly.ticker AND fmv.report_fiscal_year = ly.yr
                WHERE fmv.source = 'credit_rating'
            """)
            seen: set = set()
            result: Dict[str, str] = {}
            for r in (rows or []):
                tk = r["ticker"]
                if tk not in seen and r.get("rating_val"):
                    result[tk] = str(r["rating_val"])
                    seen.add(tk)
            return result
        else:
            rows = db_manager.execute_query_readonly(f"""
                SELECT fmv.ticker, SUM(fmv.numeric_value) AS store_total
                FROM coreiq_filing_metrics_v4 fmv
                INNER JOIN (
                    SELECT ticker, MAX(report_fiscal_year) AS yr
                    FROM coreiq_filing_metrics_v4
                    WHERE ticker IN ({ticker_sql})
                      AND source = 'store_count'
                    GROUP BY ticker
                ) ly ON fmv.ticker = ly.ticker AND fmv.report_fiscal_year = ly.yr
                WHERE fmv.source = 'store_count'
                GROUP BY fmv.ticker
            """)
            return {r["ticker"]: str(int(r["store_total"]))
                    for r in (rows or []) if r.get("store_total") is not None}
    except Exception as exc:
        log_error(f"[SCREENING] _fetch_additional_display_values failed: {exc}")
        return {}


# ── Criterion functions ──────────────────────────────────────────────────────

def apply_biz_segments_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Add business segment revenue column to working_df. N/A for companies without data.

    Does NOT filter companies — all companies stay in working_df.
    Only filters when filter_enabled=True AND the company HAS data that fails the condition.
    Fast path (IS tables): when no specific segments selected.
    Slow path (v4):        when specific segment labels are selected.
    """
    t_start = time.perf_counter()
    rows_in = len(working_df)

    display_col    = criterion.get("display_col")
    filter_enabled = criterion.get("filter_enabled", False)
    operator       = criterion.get("operator", "Greater Than")
    value1         = float(criterion.get("value1", 0) or 0)
    value2         = float(criterion.get("value2", 0) or 0)
    segments       = criterion.get("segments", [])
    year_str       = criterion.get("year", "Latest")
    year           = None if (not year_str or year_str == "Latest") else int(year_str)

    tickers = list(working_df["ticker"].values)
    if not tickers:
        return working_df.copy(), {"type": "biz_segments", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0}

    ticker_tuple = tuple(sorted(tickers))

    if segments:
        seg_tuple     = tuple(sorted(segments))
        metric_values = _fetch_segment_revenue_v4(ticker_tuple, seg_tuple, year, use_geo_axes=False)
        # Fall back to IS tables if v4 timed out (empty result for large working sets)
        if not metric_values:
            metric_values = _fetch_revenue_from_is(ticker_tuple)
    else:
        metric_values = _fetch_revenue_from_is(ticker_tuple)

    # Keep ALL companies (N/A for those without data); only filter if condition fails on existing data
    result = working_df.copy()

    if filter_enabled and metric_values:
        keep = []
        for _, row in result.iterrows():
            tk = row["ticker"]
            if tk in metric_values:
                keep.append(_apply_operator(metric_values[tk], operator, value1, value2))
            else:
                keep.append(True)  # no data → keep (show N/A)
        result = result[keep].reset_index(drop=True)

    if display_col:
        result[display_col] = result["ticker"].map(
            lambda tk: round(metric_values[tk], 2) if tk in metric_values else None
        )

    elapsed = (time.perf_counter() - t_start) * 1000
    return result.reset_index(drop=True), {
        "type": "biz_segments", "metric": "Revenues", "segments": segments,
        "rows_in": rows_in, "rows_out": len(result), "elapsed_ms": elapsed,
    }


def apply_geo_segments_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Add geographic segment revenue column to working_df. N/A for companies without data.

    Does NOT filter companies — all companies stay in working_df.
    Only filters when filter_enabled=True AND the company HAS data that fails the condition.
    Fast path (IS tables): when no specific countries selected.
    Slow path (v4):        when specific country/region labels are selected.
    """
    t_start = time.perf_counter()
    rows_in = len(working_df)

    display_col    = criterion.get("display_col")
    filter_enabled = criterion.get("filter_enabled", False)
    operator       = criterion.get("operator", "Greater Than")
    value1         = float(criterion.get("value1", 0) or 0)
    value2         = float(criterion.get("value2", 0) or 0)
    countries      = criterion.get("countries", [])
    year_str       = criterion.get("year", "Latest")
    year           = None if (not year_str or year_str == "Latest") else int(year_str)

    tickers = list(working_df["ticker"].values)
    if not tickers:
        return working_df.copy(), {"type": "geo_segments", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0}

    ticker_tuple = tuple(sorted(tickers))

    if countries:
        seg_tuple     = tuple(sorted(countries))
        metric_values = _fetch_segment_revenue_v4(ticker_tuple, seg_tuple, year, use_geo_axes=True)
        # Fall back to IS tables if v4 timed out (empty result for large working sets)
        if not metric_values:
            metric_values = _fetch_revenue_from_is(ticker_tuple)
    else:
        metric_values = _fetch_revenue_from_is(ticker_tuple)

    # Keep ALL companies (N/A for those without data); only filter if condition fails on existing data
    result = working_df.copy()

    if filter_enabled and metric_values:
        keep = []
        for _, row in result.iterrows():
            tk = row["ticker"]
            if tk in metric_values:
                keep.append(_apply_operator(metric_values[tk], operator, value1, value2))
            else:
                keep.append(True)  # no data → keep (show N/A)
        result = result[keep].reset_index(drop=True)

    if display_col:
        result[display_col] = result["ticker"].map(
            lambda tk: round(metric_values[tk], 2) if tk in metric_values else None
        )

    elapsed = (time.perf_counter() - t_start) * 1000
    return result.reset_index(drop=True), {
        "type": "geo_segments", "metric": "Revenues", "countries": countries,
        "rows_in": rows_in, "rows_out": len(result), "elapsed_ms": elapsed,
    }


def apply_additional_criterion(
    criterion: Dict,
    working_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict]:
    """Filter working_df to companies with Additional Data. Uses cached helpers."""
    t_start = time.perf_counter()
    rows_in = len(working_df)

    data_type   = criterion.get("data_type", "credit_ratings")
    display_col = criterion.get("display_col")

    tickers = list(working_df["ticker"].values)
    if not tickers:
        return working_df.copy(), {"type": "additional", "rows_in": 0, "rows_out": 0, "elapsed_ms": 0}

    ticker_tuple = tuple(sorted(tickers))
    source_val   = "credit_rating" if data_type == "credit_ratings" else "store_count"

    matched_tickers = _fetch_additional_tickers(ticker_tuple, source_val)

    if not matched_tickers:
        elapsed = (time.perf_counter() - t_start) * 1000
        return working_df.iloc[0:0].copy(), {
            "type": "additional", "rows_in": rows_in, "rows_out": 0, "elapsed_ms": elapsed
        }

    filtered = working_df[working_df["ticker"].isin(matched_tickers)].copy()

    if display_col:
        seg_tuple      = tuple(sorted(matched_tickers))
        display_values = _fetch_additional_display_values(seg_tuple, data_type)
        filtered[display_col] = filtered["ticker"].map(lambda tk: display_values.get(tk))

    elapsed = (time.perf_counter() - t_start) * 1000
    return filtered.reset_index(drop=True), {
        "type": "additional", "data_type": data_type,
        "rows_in": rows_in, "rows_out": len(filtered), "elapsed_ms": elapsed,
    }


def build_biz_segments_summary(metric: str, filter_enabled: bool, operator: str = "", value1: float = 0, value2: float = 0) -> str:
    """Build human-readable summary for a business segments criterion."""
    if not filter_enabled:
        return f"Business Segments: Has {metric} data"
    v1_fmt = f"{value1:,.0f}"
    if operator == "Between":
        v2_fmt = f"{value2:,.0f}"
        return f"Business Segments: {metric} between ${v1_fmt}mm – ${v2_fmt}mm"
    op_map = {"Greater Than": ">", "Less Than": "<", "Equals": "=",
              "Greater Than or Equal To": "≥", "Less Than or Equal To": "≤"}
    op_sym = op_map.get(operator, operator)
    return f"Business Segments: {metric} {op_sym} ${v1_fmt}mm"


def build_geo_segments_summary(metric: str, filter_enabled: bool, operator: str = "", value1: float = 0, value2: float = 0) -> str:
    """Build human-readable summary for a geographic segments criterion."""
    if not filter_enabled:
        return f"Geographic Segments: Has {metric} data"
    v1_fmt = f"{value1:,.0f}"
    if operator == "Between":
        v2_fmt = f"{value2:,.0f}"
        return f"Geographic Segments: {metric} between ${v1_fmt}mm – ${v2_fmt}mm"
    op_map = {"Greater Than": ">", "Less Than": "<", "Equals": "=",
              "Greater Than or Equal To": "≥", "Less Than or Equal To": "≤"}
    op_sym = op_map.get(operator, operator)
    return f"Geographic Segments: {metric} {op_sym} ${v1_fmt}mm"


def build_additional_summary(data_type: str) -> str:
    """Build human-readable summary for an additional data criterion."""
    labels = {"credit_ratings": "Credit Ratings", "store_counts": "Store Counts"}
    return f"Additional Data: {labels.get(data_type, data_type)}"


# =============================================================================
# PROGRESSIVE PIPELINE
# =============================================================================

def recompute_working_set(
    active_criteria: List[Dict],
    criterion_cache: Optional[Dict] = None,
    allowed_tickers: Optional[set] = None,
    allowed_members: Optional[set] = None,
) -> Tuple[pd.DataFrame, List[Dict]]:
    """Re-apply all criteria in order from the base universe.

    Always starts from scratch to guarantee correctness when criteria
    are added, removed, or edited mid-stack.

    Args:
        active_criteria:  Current ordered list of criterion dicts.
        criterion_cache:  Optional mutable dict (from session state) used to
            cache per-criterion results.  Key = (_criterion_fingerprint, frozenset
            of input tickers).  Pass the same dict across calls so cache hits
            accumulate across reruns — saves all DB round-trips for unchanged
            criteria at unchanged positions.
        allowed_tickers:  Optional set of tickers to restrict the base universe
            (used by the Watchlist feature).  When provided only companies whose
            ticker is in this set are considered.  None means full universe.
    """
    t_pipeline = time.perf_counter()

    # ── Load base universe ──
    t_base = time.perf_counter()
    working_df  = get_base_company_universe()
    ms_base = (time.perf_counter() - t_base) * 1000
    log_timing("SCREENING_BASE_UNIVERSE_LOAD", ms_base,
               f"rows={len(working_df)}")

    # ── Watchlist filter (applied before any criteria) ──
    # Prefer composite (ticker, company_name) membership — ticker alone wrongly
    # over-includes a shared-ticker sibling (TSCO=Tractor Supply/Tesco) that is
    # not actually in the watchlist, and collapses the company count.
    if allowed_members:
        before_wl = len(working_df)
        working_df = filter_universe_to_members(working_df, allowed_members)
        log_info(f"[TIMING] SCREENING_WATCHLIST_FILTER | "
                 f"before={before_wl} after={len(working_df)} "
                 f"watchlist_members={len(allowed_members)}")
    elif allowed_tickers:
        before_wl = len(working_df)
        working_df = working_df[working_df["ticker"].isin(allowed_tickers)].reset_index(drop=True)
        log_info(f"[TIMING] SCREENING_WATCHLIST_FILTER | "
                 f"before={before_wl} after={len(working_df)} "
                 f"watchlist_size={len(allowed_tickers)}")

    debug_trace: List[Dict] = []
    log_info(f"[TIMING] SCREENING_PIPELINE_START | criteria={len(active_criteria)} "
             f"base_universe={len(working_df)}"
             f"{' watchlist_filter=' + str(len(allowed_tickers)) if allowed_tickers else ''}")

    # NON-FILTERING MODE: criteria never drop companies. The full universe
    # (`full_df`, 253 companies) is preserved; each criterion is run against it
    # and the value column(s) it produces are left-merged back so companies
    # without data simply show N/A. The "matched" count always equals the
    # universe size; each criterion's trace records how many had actual data.
    full_df = working_df.copy()
    universe_tickers = frozenset(full_df["ticker"].values)

    def _run_criterion(crit, ctype_, src_df):
        """Dispatch a single criterion against src_df (full universe)."""
        if ctype_ == "industry":
            return apply_industry_criterion(crit.get("industries", []), src_df)
        if ctype_ == "geography":
            return apply_geography_criterion(crit.get("countries", []), src_df)
        if ctype_ == "financial":
            stmt_name = crit.get("statement")
            if stmt_name in SEGMENT_STATEMENT_TYPES:
                if stmt_name == "Business Segments":
                    return apply_business_segments_statement_criterion(crit, src_df)
                if stmt_name == "Geographical Segments":
                    return apply_geographical_segments_statement_criterion(crit, src_df)
                return apply_segment_statement_criterion(crit, src_df)
            if stmt_name in TABULAR_MARKET_DATA_STMTS:
                if stmt_name == "Key Stats":
                    return apply_key_stats_criterion(crit, src_df)
                if stmt_name == "Ratios":
                    return apply_ratios_criterion(crit, src_df)
                return apply_tabular_market_data_criterion(crit, src_df)
            if stmt_name == "Estimates":
                return apply_estimates_criterion(crit, src_df)
            if stmt_name == "Forecasting":
                return apply_forecast_criterion(crit, src_df)
            return apply_financial_criterion(crit, src_df)
        if ctype_ == "keydevs":
            return apply_keydevs_criterion(crit, src_df)
        if ctype_ == "biz_segments":
            return apply_biz_segments_criterion(crit, src_df)
        if ctype_ == "geo_segments":
            return apply_geo_segments_criterion(crit, src_df)
        if ctype_ == "additional":
            return apply_additional_criterion(crit, src_df)
        return src_df, {}

    for i, criterion in enumerate(active_criteria):
        ctype  = criterion.get("type", "?")
        t_crit = time.perf_counter()

        # Criterion-level cache (keyed on criterion + constant universe)
        cache_key = None
        result_df = None
        dbg = None
        if criterion_cache is not None:
            cache_key = (_criterion_fingerprint(criterion), universe_tickers)
            if cache_key in criterion_cache:
                result_df, cached_dbg = criterion_cache[cache_key]
                dbg = dict(cached_dbg)
                dbg["cache_hit"] = True

        # FULLY NON-FILTERING: every criterion type — including industry &
        # geography — only ANNOTATES the full universe and never drops a company.
        if result_df is None:
            run_src = full_df.copy()
            # SAFETY NET — a single malformed/failing criterion must NEVER break
            # the whole screen. On any error, degrade that criterion to a no-op:
            # the universe is preserved and its column simply shows N/A, exactly
            # like missing data. This is the centralized "never breaks" guarantee.
            try:
                result_df, dbg = _run_criterion(criterion, ctype, run_src)
            except Exception as crit_exc:
                log_error(
                    f"[SCREENING] criterion idx={i} type={ctype} "
                    f"stmt={criterion.get('statement')} failed; degrading to N/A: {crit_exc}"
                )
                result_df = run_src
                dbg = {"type": ctype, "error": str(crit_exc), "degraded": True}
            if criterion_cache is not None and cache_key is not None:
                # don't cache transient errors — only cache real results
                if not dbg.get("degraded"):
                    criterion_cache[cache_key] = (result_df.copy(), dict(dbg))

        # Merge NEW value columns onto the full universe; missing → NaN (N/A)
        with_data = 0
        new_cols = [c for c in result_df.columns
                    if c not in full_df.columns and c != "ticker"]
        if new_cols and "ticker" in result_df.columns:
            merge_src = result_df[["ticker"] + new_cols].drop_duplicates(subset=["ticker"])
            full_df = full_df.merge(merge_src, on="ticker", how="left")
            primary = criterion.get("display_col")
            # Industry/geography default annotation column names
            if not primary:
                primary = {"industry": "Industry", "geography": "Country"}.get(ctype)
            if primary and primary in full_df.columns:
                with_data = int(full_df[primary].notna().sum())

        dbg = dict(dbg or {})
        dbg["criterion_idx"]     = i
        dbg["criterion_summary"] = criterion.get("summary", "")
        dbg["rows_out"]   = len(full_df)      # always the full universe size
        dbg["with_data"]  = with_data
        ms_crit = (time.perf_counter() - t_crit) * 1000
        dbg["elapsed_ms"] = ms_crit
        debug_trace.append(dbg)

        log_timing("SCREENING_CRITERION_APPLIED", ms_crit,
                   f"idx={i} type={ctype} universe={len(full_df)} "
                   f"with_data={with_data} (non-filtering)")

    working_df = full_df
    ms_pipeline = (time.perf_counter() - t_pipeline) * 1000
    log_timing("SCREENING_PIPELINE_TOTAL", ms_pipeline,
               f"criteria={len(active_criteria)} rows_out={len(working_df)}")

    return working_df, debug_trace


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _parse_timeframe_year(timeframe: str) -> Optional[int]:
    """Extract calendar year from 'FY YYYY'. Returns None for 'Latest'."""
    if not timeframe or timeframe == "Latest":
        return None
    try:
        return int(timeframe.strip().split()[-1])
    except (ValueError, IndexError):
        return None


def _criterion_fingerprint(criterion: Dict) -> str:
    """Stable string key for a criterion dict used for cache lookup.

    Excludes ephemeral / derived fields (summary, display_col) so that
    two identical criteria always produce the same fingerprint.
    """
    skip = {"summary", "display_col"}
    safe = {k: v for k, v in criterion.items() if k not in skip}
    return json.dumps(safe, sort_keys=True, default=str)


def filter_universe_to_members(
    df: pd.DataFrame,
    members,
) -> pd.DataFrame:
    """Restrict the universe to a watchlist's exact companies.

    members: iterable of (ticker, company_name) pairs from the watchlist.

    Why not a plain `ticker.isin(...)`: JD / LULU / TSCO are each shared by two
    distinct companies on different exchanges (e.g. TSCO = Tractor Supply AND
    Tesco PLC). Filtering by ticker alone both (a) over-includes the sibling that
    is NOT in the watchlist and (b) collapses the count.

    Matching rule (robust to historical company_name casing drift in the
    watchlist table):
      • normal ticker (unique in the universe) → match by ticker alone;
      • shared ticker → disambiguate by case-insensitive company_name.
    """
    if df is None or len(df) == 0 or not members:
        return df
    members = list(members)
    wl_tickers = {t for t, _ in members}
    tcount = df["ticker"].value_counts()
    shared = {t for t in wl_tickers if int(tcount.get(t, 0)) > 1}
    allowed_named = {
        (t, (n or "").strip().casefold())
        for t, n in members if t in shared
    }

    def _keep(ticker, name) -> bool:
        if ticker not in wl_tickers:
            return False
        if ticker in shared:
            return (ticker, (name or "").strip().casefold()) in allowed_named
        return True

    mask = [
        _keep(t, n)
        for t, n in zip(df["ticker"].values, df["company_name"].values)
    ]
    return df[pd.Series(mask, index=df.index)].reset_index(drop=True)


def criteria_stack_fingerprint(
    active_criteria: List[Dict],
    allowed_tickers: Optional[set] = None,
    allowed_members: Optional[set] = None,
) -> str:
    """Fingerprint for the full visible criteria stack + watchlist scope.

    Prefers allowed_members (composite identity) so adding a ticker-sibling
    company to a watchlist busts the cache; falls back to allowed_tickers.
    """
    visible = [c for c in active_criteria if not c.get("hidden")]
    parts = [_criterion_fingerprint(c) for c in visible]
    if allowed_members:
        wl_key = sorted((str(t), str(n)) for t, n in allowed_members)
    elif allowed_tickers:
        wl_key = sorted(allowed_tickers)
    else:
        wl_key = None
    return json.dumps({"criteria": parts, "watchlist": wl_key}, sort_keys=True, default=str)


def _build_ticker_in_list(tickers: List[str]) -> str:
    """Build a safe SQL IN clause value list from a list of tickers.

    Uses whitelist sanitization: only alphanumeric chars, dots, dashes,
    and forward slashes are allowed (covering all valid ticker formats).
    """
    import re as _re
    safe = []
    for t in tickers:
        if not t:
            continue
        # Whitelist: keep only valid ticker characters (A-Z, 0-9, ., -, /)
        cleaned = _re.sub(r"[^A-Za-z0-9.\-/]", "", t.strip())
        if cleaned and len(cleaned) <= 20:  # Tickers are max ~10 chars
            safe.append(cleaned)
    if not safe:
        return "''"
    return ", ".join(f"'{t}'" for t in safe)


def _apply_operator(
    raw_value: float,
    operator: str,
    threshold1: float,
    threshold2: float,
) -> bool:
    """Apply comparison operator. Returns True if raw_value passes."""
    if operator == "Greater Than":
        return raw_value > threshold1
    elif operator == "Less Than":
        return raw_value < threshold1
    elif operator == "Equals":
        return abs(raw_value - threshold1) < 1.0   # $1 tolerance
    elif operator == "Greater Than or Equal To":
        return raw_value >= threshold1
    elif operator == "Less Than or Equal To":
        return raw_value <= threshold1
    elif operator == "Between":
        lo, hi = min(threshold1, threshold2), max(threshold1, threshold2)
        return lo <= raw_value <= hi
    return False


def _query_sec_latest(
    tickers: List[str],
    cfg: Dict,
    col: str,
    label: str = "",
    year: Optional[int] = None,
    period_value: str = "annual",
    quarter_num: Optional[int] = None,
) -> Dict[str, Optional[float]]:
    """Query value of `col` for each SEC ticker.

    period_value = "annual" | "quarterly"
    quarter_num  = 1..4 (only used when period_value == "quarterly")
    year=None    → MAX(date) subquery (latest period per ticker)
    year=YYYY    → date range scan for that year
    """
    if not tickers:
        return {}

    t_sec_start = time.perf_counter()
    table      = cfg["sec_table"]
    date_col   = cfg["sec_date_col"]
    period_col = cfg["sec_period_col"]
    ticker_sql = _build_ticker_in_list(tickers)

    # Build date range clause for quarterly + specific year + specific quarter
    date_clause = ""
    if year and quarter_num and period_value == "quarterly":
        q_start_month = (quarter_num - 1) * 3 + 1
        q_end_month = quarter_num * 3
        start_date = f"{year}-{q_start_month:02d}-01"
        # End of quarter: last day of the quarter month
        if q_end_month in (1, 3, 5, 7, 8, 10, 12):
            end_day = 31
        elif q_end_month in (4, 6, 9, 11):
            end_day = 30
        else:
            end_day = 29  # Feb — safe upper bound
        end_date = f"{year}-{q_end_month:02d}-{end_day}"
        date_clause = f"AND {date_col} BETWEEN '{start_date}' AND '{end_date}'"
    elif year:
        date_clause = f"AND {date_col} BETWEEN '{year}-01-01' AND '{year}-12-31'"

    if year and date_clause:
        query = f"""
            SELECT ticker, {col} AS metric_value
            FROM {table}
            WHERE ticker IN ({ticker_sql})
              AND {period_col} = '{period_value}'
              {date_clause}
              AND {col} IS NOT NULL
        """
    elif period_value == "quarterly" and quarter_num and not year:
        # Latest quarter with specific quarter number: use ROW_NUMBER + QUARTER filter
        query = f"""
            SELECT ticker, {col} AS metric_value
            FROM (
                SELECT ticker, {col},
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY {date_col} DESC) AS rn
                FROM {table}
                WHERE ticker IN ({ticker_sql})
                  AND {period_col} = '{period_value}'
                  AND QUARTER({date_col}) = {quarter_num}
                  AND {col} IS NOT NULL
            ) ranked
            WHERE rn = 1
        """
    else:
        # ROW_NUMBER() window function — latest period per ticker
        query = f"""
            SELECT ticker, {col} AS metric_value
            FROM (
                SELECT ticker, {col},
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY {date_col} DESC) AS rn
                FROM {table}
                WHERE ticker IN ({ticker_sql})
                  AND {period_col} = '{period_value}'
                  AND {col} IS NOT NULL
            ) ranked
            WHERE rn = 1
        """

    rows = db_manager.execute_query_readonly(query)
    t_sec_end = time.perf_counter()
    log_db_timing("SEC_SCREENING_QUERY", cfg["sec_table"],
                  (t_sec_end - t_sec_start) * 1000,
                  rows=len(rows), ticker=f"{len(tickers)}_tickers")

    # ── Parse results ──
    result: Dict[str, Optional[float]] = {}
    for r in rows:
        ticker = r.get("ticker")
        val    = r.get("metric_value")
        if ticker and val is not None:
            try:
                result[ticker] = float(val)
            except (TypeError, ValueError):
                pass

    return result


def _query_yf_latest(
    tickers: List[str],
    cfg: Dict,
    line_item: str,
    label: str = "",
    year: Optional[int] = None,
    period_value: str = "annual",
    quarter_num: Optional[int] = None,
) -> Dict[str, Optional[float]]:
    """Query value of `line_item` for each YF ticker.

    YF tables are long-format: (ticker, period_end, frequency, line_item, value).
    period_value = "annual" | "quarterly"
    quarter_num  = 1..4 (only used when period_value == "quarterly")
    year=None    → MAX(date) subquery (latest period per ticker)
    year=YYYY    → date range scan for that year
    """
    if not tickers:
        return {}

    t_yf_start = time.perf_counter()
    table      = cfg["yf_table"]
    date_col   = cfg["yf_date_col"]
    period_col = cfg["yf_period_col"]
    ticker_sql = _build_ticker_in_list(tickers)
    safe_item  = line_item.replace("'", "''")

    # Build date range clause for quarterly + specific year + specific quarter
    date_clause = ""
    if year and quarter_num and period_value == "quarterly":
        q_start_month = (quarter_num - 1) * 3 + 1
        q_end_month = quarter_num * 3
        start_date = f"{year}-{q_start_month:02d}-01"
        if q_end_month in (1, 3, 5, 7, 8, 10, 12):
            end_day = 31
        elif q_end_month in (4, 6, 9, 11):
            end_day = 30
        else:
            end_day = 29
        end_date = f"{year}-{q_end_month:02d}-{end_day}"
        date_clause = f"AND {date_col} BETWEEN '{start_date}' AND '{end_date}'"
    elif year:
        date_clause = f"AND {date_col} BETWEEN '{year}-01-01' AND '{year}-12-31'"

    if year and date_clause:
        query = f"""
            SELECT ticker, value AS metric_value
            FROM {table}
            WHERE ticker IN ({ticker_sql})
              AND {period_col} = '{period_value}'
              AND line_item = '{safe_item}'
              {date_clause}
              AND value IS NOT NULL
        """
    elif period_value == "quarterly" and quarter_num and not year:
        # Latest quarter with specific quarter number
        query = f"""
            SELECT ticker, value AS metric_value
            FROM (
                SELECT ticker, value,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY {date_col} DESC) AS rn
                FROM {table}
                WHERE ticker IN ({ticker_sql})
                  AND {period_col} = '{period_value}'
                  AND line_item = '{safe_item}'
                  AND QUARTER({date_col}) = {quarter_num}
                  AND value IS NOT NULL
            ) ranked
            WHERE rn = 1
        """
    else:
        # ROW_NUMBER() window function — latest period per ticker
        query = f"""
            SELECT ticker, value AS metric_value
            FROM (
                SELECT ticker, value,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY {date_col} DESC) AS rn
                FROM {table}
                WHERE ticker IN ({ticker_sql})
                  AND {period_col} = '{period_value}'
                  AND line_item = '{safe_item}'
                  AND value IS NOT NULL
            ) ranked
            WHERE rn = 1
        """

    rows = db_manager.execute_query_readonly(query)
    t_yf_end = time.perf_counter()
    log_db_timing("YF_SCREENING_QUERY", cfg["yf_table"],
                  (t_yf_end - t_yf_start) * 1000,
                  rows=len(rows), ticker=f"{len(tickers)}_tickers")

    # ── Parse results ──
    result: Dict[str, Optional[float]] = {}
    for r in rows:
        ticker = r.get("ticker")
        val    = r.get("metric_value")
        if ticker and val is not None:
            try:
                result[ticker] = float(val)
            except (TypeError, ValueError):
                pass

    return result


def _query_sec_quarterly_with_dates(
    tickers: List[str],
    cfg: Dict,
    col: str,
    year: Optional[int] = None,
) -> List[tuple]:
    """Query ALL quarterly rows with dates for FQ post-filtering.

    Returns list of (ticker, date, value) tuples.
    """
    if not tickers:
        return []

    t_sq_start = time.perf_counter()
    table      = cfg["sec_table"]
    date_col   = cfg["sec_date_col"]
    period_col = cfg["sec_period_col"]
    ticker_sql = _build_ticker_in_list(tickers)

    date_clause = ""
    if year:
        date_clause = f"AND {date_col} BETWEEN '{year}-01-01' AND '{year}-12-31'"

    query = f"""
        SELECT ticker, {date_col} AS fiscal_date, {col} AS metric_value
        FROM {table}
        WHERE ticker IN ({ticker_sql})
          AND {period_col} = 'quarterly'
          {date_clause}
          AND {col} IS NOT NULL
        ORDER BY {date_col} DESC
    """

    rows = db_manager.execute_query_readonly(query)
    t_sq_end = time.perf_counter()
    log_db_timing("SEC_QUARTERLY_SCREENING", cfg["sec_table"],
                  (t_sq_end - t_sq_start) * 1000,
                  rows=len(rows), ticker=f"{len(tickers)}_tickers")
    result = []
    for r in rows:
        ticker = r.get("ticker")
        dt = r.get("fiscal_date")
        val = r.get("metric_value")
        if ticker and dt and val is not None:
            try:
                result.append((ticker, dt, float(val)))
            except (TypeError, ValueError):
                pass
    return result


def _query_yf_quarterly_with_dates(
    tickers: List[str],
    cfg: Dict,
    line_item: str,
    year: Optional[int] = None,
) -> List[tuple]:
    """Query ALL quarterly rows with dates for FQ post-filtering.

    Returns list of (ticker, date, value) tuples.
    """
    if not tickers:
        return []

    t_yq_start = time.perf_counter()
    table      = cfg["yf_table"]
    date_col   = cfg["yf_date_col"]
    period_col = cfg["yf_period_col"]
    ticker_sql = _build_ticker_in_list(tickers)
    safe_item  = line_item.replace("'", "''")

    date_clause = ""
    if year:
        date_clause = f"AND {date_col} BETWEEN '{year}-01-01' AND '{year}-12-31'"

    query = f"""
        SELECT ticker, {date_col} AS fiscal_date, value AS metric_value
        FROM {table}
        WHERE ticker IN ({ticker_sql})
          AND {period_col} = 'quarterly'
          AND line_item = '{safe_item}'
          {date_clause}
          AND value IS NOT NULL
        ORDER BY {date_col} DESC
    """

    rows = db_manager.execute_query_readonly(query)
    t_yq_end = time.perf_counter()
    log_db_timing("YF_QUARTERLY_SCREENING", cfg["yf_table"],
                  (t_yq_end - t_yq_start) * 1000,
                  rows=len(rows), ticker=f"{len(tickers)}_tickers")
    result = []
    for r in rows:
        ticker = r.get("ticker")
        dt = r.get("fiscal_date")
        val = r.get("metric_value")
        if ticker and dt and val is not None:
            try:
                result.append((ticker, dt, float(val)))
            except (TypeError, ValueError):
                pass
    return result


# =============================================================================
# CRITERION SUMMARY HELPERS  (for UI display)
# =============================================================================

def build_industry_summary(industries: List[str]) -> str:
    if not industries:
        return "Industry Classifications: (none)"
    if len(industries) <= 3:
        return f"Industry Classifications: {', '.join(industries)}"
    return f"Industry Classifications: {len(industries)} selected"


def build_geography_summary(countries: List[str]) -> str:
    if not countries:
        return "Geographic Locations: (none)"
    if len(countries) <= 3:
        return f"Geographic Locations: {', '.join(countries)}"
    return f"Geographic Locations: {len(countries)} countries"


def build_financial_summary(
    statement: str,
    metric_label: str,
    operator: str,
    value1: float,
    value2: float,
    timeframe: str,
    unit: str = "$mm",
) -> str:
    if unit == "$mm":
        fmt = lambda v: f"{v:,.2f}"
        unit_suffix = "($mm)"
    elif unit == "%":
        fmt = lambda v: f"{v:,.1f}%"
        unit_suffix = ""
    else:
        fmt = lambda v: f"{v:,.2f}"
        unit_suffix = f"({unit})" if unit else ""

    if operator == "Between":
        if unit_suffix:
            val_str = f"{operator} {fmt(value1)} – {fmt(value2)} {unit_suffix}"
        else:
            val_str = f"{operator} {fmt(value1)} – {fmt(value2)}"
    else:
        if unit_suffix:
            val_str = f"{operator} {fmt(value1)} {unit_suffix}"
        else:
            val_str = f"{operator} {fmt(value1)}"
    return f"{statement} / {metric_label}: {val_str} [{timeframe}]"


def build_keydevs_summary(
    category_labels: List[str],
    timeframe_label: str,
    date_filter_mode: str = "timeframe",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """Concise summary: show count of categories selected."""
    if date_filter_mode == "date_range" and start_date and end_date:
        period = f"{start_date} to {end_date}"
    else:
        period = timeframe_label
    if not category_labels:
        return "Key Developments by Category: (none)"
    n = len(category_labels)
    if n == 1:
        return f"Key Developments by Category: {category_labels[0]} [{period}]"
    return f"Key Developments: {n} categories selected [{period}]"


def keydevs_period_display_label(criterion: Dict) -> str:
    """Human-readable period label for UI (timeframe preset or explicit dates)."""
    if criterion.get("date_filter_mode") == "date_range":
        start = _safe_keydev_iso_date(criterion.get("start_date"))
        end = _safe_keydev_iso_date(criterion.get("end_date"))
        if start and end:
            return f"{start} to {end}"
    return criterion.get("timeframe_label", "")
