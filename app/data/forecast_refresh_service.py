"""
Forecast Refresh Service
========================
Data access and orchestration for the Refresh Forecasting Models popup.

Responsibilities:
  1. ensure_forecast_columns() — add company_name/exchange to coreiq_model_forecasts if absent
  2. backfill_company_info()   — fill company_name/exchange for rows that lack them
  3. get_refresh_table_data()  — assembled table rows for the popup (all tickers)
  4. send_model_refresh_email()— HTML notification email after model run
"""
from __future__ import annotations

import calendar
import os
import smtplib
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Set

import streamlit as st

from time import perf_counter

from core.database import db_manager
from utils.server_logger import log_info, log_structured_error, log_timing


# ─── Month-name lookup tables (shared by Q4 derivation helpers) ───────────────
_MONTH_ABB_TO_NUM: Dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAME_TO_NUM: Dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _fye_name_to_month_num(fye: Optional[str]) -> Optional[int]:
    """Convert fiscal_year_end string (any format) to month number 1-12, or None."""
    if not fye:
        return None
    s = fye.strip().lower()
    if s in _MONTH_NAME_TO_NUM:
        return _MONTH_NAME_TO_NUM[s]
    if s[:3] in _MONTH_ABB_TO_NUM:
        return _MONTH_ABB_TO_NUM[s[:3]]
    # ISO date "2024-12-31" — extract middle part
    if "-" in s:
        parts = s.split("-")
        try:
            m = int(parts[1]) if len(parts) >= 3 else int(parts[-1])
            if 1 <= m <= 12:
                return m
        except (ValueError, IndexError):
            pass
    try:
        m = int(s)
        if 1 <= m <= 12:
            return m
    except ValueError:
        pass
    return None


def _yf_fqe_month_for_earnings_date(earnings_date: date, fye_month: int) -> Optional[int]:
    """
    Return the fiscal quarter ending month for a YF earnings date, given FYE month.
    Convenience wrapper around _yf_fqe_date_for_earnings_date — returns month only.
    Used for Q4 detection: if returned month == fye_month → this is Q4.
    """
    fqe = _yf_fqe_date_for_earnings_date(earnings_date, fye_month)
    return fqe.month if fqe is not None else None


def _yf_fqe_date_for_earnings_date(earnings_date: date, fye_month: int) -> Optional[date]:
    """
    Return the last day of the fiscal quarter that this earnings date reports.

    Mirrors EarningsCalendarRepository._derive_yf_fiscal_quarter_ending() exactly,
    but returns a full date object (last day of the fiscal quarter end month/year)
    instead of the 'Mon/YYYY' string. Returning the full date allows grouping YF rows
    by fiscal period (ticker, fqe_date) before picking the latest ingested_at.
    """
    quarter_end_months = {
        ((fye_month - offset - 1) % 12) + 1
        for offset in (0, 3, 6, 9)
    }
    candidates = []
    for year in (earnings_date.year - 1, earnings_date.year):
        for month in quarter_end_months:
            last_day = calendar.monthrange(year, month)[1]
            quarter_end = date(year, month, last_day)
            if quarter_end <= earnings_date:
                candidates.append(quarter_end)
    if not candidates:
        return None
    return max(candidates)


# ─── Schema management ────────────────────────────────────────────────────────

_COLUMNS_READY = False


def ensure_forecast_columns() -> None:
    """
    Add company_name and exchange columns to coreiq_model_forecasts if absent.
    MySQL 8.0 does not support ALTER TABLE ADD COLUMN IF NOT EXISTS, so we
    check INFORMATION_SCHEMA first.

    Guarded by a process-level flag: the columns cannot vanish while the process
    is alive, so the INFORMATION_SCHEMA round trip is paid once per process
    rather than on every new session.
    """
    global _COLUMNS_READY
    if _COLUMNS_READY:
        return
    _cols_to_add = [
        ("company_name", "VARCHAR(255) DEFAULT NULL"),
        ("exchange",     "VARCHAR(100) DEFAULT NULL"),
    ]
    try:
        existing = db_manager.execute_query_readonly(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'coreiq_model_forecasts'
              AND TABLE_SCHEMA = DATABASE()
              AND COLUMN_NAME IN ('company_name', 'exchange')
            """,
            {},
        )
        existing_names = {r["COLUMN_NAME"] for r in (existing or [])}
        for col_name, col_def in _cols_to_add:
            if col_name not in existing_names:
                db_manager.execute_insert(
                    f"ALTER TABLE coreiq_model_forecasts ADD COLUMN {col_name} {col_def}",
                    {},
                )
                log_info(f"[forecast_refresh] Added column {col_name} to coreiq_model_forecasts")
        _COLUMNS_READY = True
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_refresh_service",
            component="ensure_forecast_columns",
            operation="ALTER_TABLE",
        )


# Process-level flag — set after confirming nothing needs backfilling.
# Avoids repeated DB checks on every new Streamlit session within the same process.
_BACKFILL_DONE = False


def backfill_company_info() -> int:
    """
    Fill company_name and exchange for rows in coreiq_model_forecasts that have
    NULL values. Matches composite tickers like 'TSCO.L' → ticker='TSCO', exchange_acronym='L'.
    Returns number of tickers updated.

    Uses idx_null_company prefix index for the fast-path NULL check (<350ms).
    Sets a process-level flag once confirmed clean to skip all DB work on subsequent calls.
    """
    global _BACKFILL_DONE
    _t0 = perf_counter()

    # Fast skip — once confirmed clean within this server process
    if _BACKFILL_DONE:
        log_timing("backfill_company_info", 0.0, "skipped=process_flag", level="INFO")
        return 0

    try:
        # Fast null check using idx_null_company prefix index (~300ms vs 6700ms full scan)
        needs_fill = db_manager.fetch_one(
            """
            SELECT ticker FROM coreiq_model_forecasts
            WHERE company_name IS NULL OR company_name = '' LIMIT 1
            """,
            {},
        )
        log_timing("backfill_null_check", (perf_counter() - _t0) * 1000,
                   f"needs_fill={bool(needs_fill)}", level="INFO")

        if not needs_fill:
            _BACKFILL_DONE = True
            return 0

        # Still need to fill — find ALL missing tickers
        missing = db_manager.execute_query_readonly(
            """
            SELECT DISTINCT ticker
            FROM coreiq_model_forecasts
            WHERE company_name IS NULL OR company_name = ''
               OR exchange IS NULL OR exchange = ''
            """,
            {},
        )
        if not missing:
            _BACKFILL_DONE = True
            return 0

        # Build lookup: composite ticker → {company_name, exchange}
        comp_rows = db_manager.execute_query_readonly(
            """
            SELECT ticker, exchange_acronym, name_coresight, exchange
            FROM coreiq_companies
            WHERE source IN ('SEC', 'YFinance')
            """,
            {},
        )
        lookup: Dict[str, Dict[str, str]] = {}
        for c in comp_rows:
            base = (c.get("ticker") or "").strip()
            acronym = (c.get("exchange_acronym") or "").strip()
            composite = f"{base}.{acronym}" if acronym else base
            info = {
                "company_name": (c.get("name_coresight") or "").strip(),
                "exchange": (c.get("exchange") or "").strip(),
            }
            lookup[composite] = info
            if base and base not in lookup:
                lookup[base] = info

        updated = 0
        for row in missing:
            ticker = (row.get("ticker") or "").strip()
            if not ticker:
                continue
            info = lookup.get(ticker) or lookup.get(ticker.split(".")[0]) or {}
            company_name = info.get("company_name") or ""
            exchange = info.get("exchange") or ""
            if not company_name and not exchange:
                continue
            n = db_manager.execute_update(
                """
                UPDATE coreiq_model_forecasts
                SET company_name = :company_name,
                    exchange     = :exchange
                WHERE ticker = :ticker
                  AND (company_name IS NULL OR company_name = ''
                       OR exchange IS NULL OR exchange = '')
                """,
                {"ticker": ticker, "company_name": company_name, "exchange": exchange},
            )
            updated += int(n or 0)
        if updated == 0:
            _BACKFILL_DONE = True
        log_timing("backfill_company_info", (perf_counter() - _t0) * 1000,
                   f"updated={updated} tickers_checked={len(missing)}", level="INFO")
        return updated
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_refresh_service",
            component="backfill_company_info",
            operation="BACKFILL",
        )
        return 0


# ─── Batch data lookups ───────────────────────────────────────────────────────

def _next_reporting_dates_bulk() -> Dict[str, Optional[date]]:
    """base_ticker → next earnings date (any fiscal quarter).

    WARNING: returns Q1/Q2/Q3/Q4 dates indiscriminately.
    DO NOT use this for forecasting refresh — annual forecasting requires Q4 only.
    Use _annual_q4_report_dates_bulk() for the Refresh popup and email.

    This function is retained only for non-forecasting callers that genuinely
    need the next upcoming quarterly earnings date regardless of fiscal quarter.
    """
    _t = perf_counter()
    try:
        rows = db_manager.execute_query_readonly(
            """
            SELECT ticker,
                   COALESCE(
                       MIN(CASE WHEN earnings_date >= CURDATE() THEN earnings_date END),
                       MAX(CASE WHEN earnings_date <  CURDATE() THEN earnings_date END)
                   ) AS next_date
            FROM (
                SELECT ticker, earnings_date FROM coreiq_nasdaq_earnings_calendar
                UNION ALL
                SELECT ticker, earnings_date FROM coreiq_yf_earnings_calendar
            ) combined
            GROUP BY ticker
            """,
            {},
        )
        result: Dict[str, Optional[date]] = {}
        for r in rows:
            tk = (r.get("ticker") or "").strip()
            dt = r.get("next_date")
            if tk and dt is not None:
                result[tk] = dt.date() if hasattr(dt, "date") else dt
        log_timing("_next_reporting_dates_bulk", (perf_counter() - _t) * 1000,
                   f"tickers={len(result)}", level="INFO")
        return result
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_next_reporting_dates_bulk", operation="SELECT")
        return {}


def _annual_q4_report_dates_bulk() -> Dict[str, Dict[str, Any]]:
    """
    Canonical Q4/full-year annual reporting date per ticker.

    Returns Dict[ticker, info_dict] where info_dict contains:
        date                  — selected earnings date (date object)
        fiscal_quarter_ending — e.g. "Dec/2025"
        source                — "nasdaq" or "yf"
        fetched_at_utc        — datetime when the calendar row was fetched (ingested_at for YF)
        fiscal_q              — always 4
        fqe_month             — fiscal quarter ending month number
        fye_month             — fiscal year end month number

    ANNUAL FORECASTING ONLY — Q1/Q2/Q3 dates are NEVER returned.
    Do NOT use _next_reporting_dates_bulk() for forecasting refresh.

    Q4 identification (matches EarningsCalendarRepository._correct_fiscal_q_year):
        fy_start_month = (fye_month % 12) + 1
        months_into_fy = (fqe_month - fy_start_month) % 12 + 1
        fiscal_q       = (months_into_fy + 2) // 3
        Q4 iff fiscal_q == 4, equivalently iff fqe_month == fye_month

    Selection rule per ticker:
        1. Prefer nearest upcoming Q4 earnings_date >= CURDATE()
        2. Fallback to most recent past Q4 earnings_date < CURDATE()
        3. No Q4 row → ticker absent from result (shown as "—")

    NASDAQ path (non-composite tickers):
        1. ROW_NUMBER() OVER (PARTITION BY ticker, fiscal_quarter_ending
                              ORDER BY fetched_at_utc DESC, earnings_date DESC, id DESC)
           → keeps the latest-fetched row per (ticker, fiscal period), consistent
           with EarningsCalendarRepository.get_calendar_events() dedup logic.
        2. Joins latest AV overview per ticker via ROW_NUMBER() OVER
           (PARTITION BY ticker ORDER BY fetched_at_utc DESC).
        3. Filters fqe_month == fye_month (= Q4).
        4. All matching Q4 rows returned to Python; Python picks upcoming or past.
        Tickers with '.' are excluded — handled by YF path.

    YF path (composite tickers such as TSCO.L, JD.L, ADS.DE):
        1. Queries coreiq_yf_earnings_calendar by exact composite ticker
           (never strips suffix — prevents borrowing a US base-ticker's date).
        2. Fetches (ticker, earnings_date, ingested_at).
        3. Python derives FQE date via _yf_fqe_date_for_earnings_date() using FYE
           from _fiscal_year_end_bulk() (itself using latest ingested_at per ticker).
        4. Groups by (ticker, fqe_date) and keeps the row with latest ingested_at
           within each fiscal period — mirrors the fetched_at_utc dedup of NASDAQ.
        5. Filters fqe_month == fye_month (Q4), then applies upcoming/past selection.
    """
    _t = perf_counter()
    _MONTH_NUM_TO_ABB = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }

    # ── NASDAQ path ───────────────────────────────────────────────────────────
    # SQL returns all latest-fetched Q4-candidate rows (one per fiscal period per
    # ticker).  Python does the upcoming/past selection so we keep full metadata.
    nasdaq_result: Dict[str, Dict[str, Any]] = {}
    try:
        nasdaq_rows = db_manager.execute_query_readonly(
            """
            SELECT
                lc.ticker,
                lc.earnings_date,
                lc.fiscal_quarter_ending,
                lc.fetched_at_utc,
                CASE SUBSTRING_INDEX(lc.fiscal_quarter_ending, '/', 1)
                    WHEN 'Jan' THEN 1  WHEN 'Feb' THEN 2  WHEN 'Mar' THEN 3
                    WHEN 'Apr' THEN 4  WHEN 'May' THEN 5  WHEN 'Jun' THEN 6
                    WHEN 'Jul' THEN 7  WHEN 'Aug' THEN 8  WHEN 'Sep' THEN 9
                    WHEN 'Oct' THEN 10 WHEN 'Nov' THEN 11 WHEN 'Dec' THEN 12
                    ELSE 0
                END AS fqe_month_num
            FROM (
                SELECT ticker, earnings_date, fiscal_quarter_ending, fetched_at_utc, id
                FROM (
                    SELECT
                        ticker, earnings_date, fiscal_quarter_ending, fetched_at_utc, id,
                        ROW_NUMBER() OVER (
                            PARTITION BY ticker, fiscal_quarter_ending
                            ORDER BY fetched_at_utc DESC, earnings_date DESC, id DESC
                        ) AS rn
                    FROM coreiq_nasdaq_earnings_calendar
                    WHERE fiscal_quarter_ending IS NOT NULL
                      AND earnings_date IS NOT NULL
                      AND ticker NOT LIKE '%.%'
                ) ranked
                WHERE rn = 1
            ) lc
            """,
            {},
        )

        # Group by ticker, collect Q4 rows, apply upcoming-first selection
        ticker_q4_candidates: Dict[str, list] = defaultdict(list)
        today = date.today()
        # Fiscal year end is resolved in Python from the FULL source chain, not by
        # an INNER JOIN on coreiq_av_company_overview alone. The join silently
        # discarded every company whose vendor overview lacked a fiscal_year_end —
        # 104 companies on STG, 39 of which had a perfectly good annual date in the
        # calendar. A company with no FYE from ANY source is a real data gap and is
        # counted in `_dropped_no_fye` below rather than vanishing unexplained.
        fye_names = _fiscal_year_end_bulk()
        fye_months = {tk: _fye_name_to_month_num(name) for tk, name in fye_names.items()}
        _dropped_no_fye: Set[str] = set()
        _dropped_no_q4: Set[str] = set()

        # Which quarter-end month counts as ANNUAL, decided per ticker.
        #
        # Normally it is simply the fiscal-year-end month: a quarter is the annual
        # one iff its end month equals the FYE month. That is what excludes interim,
        # half-year and Q1-Q3 rows — a half-yearly reporter's H1 quarter ends
        # mid-year and can never match, its full-year quarter always does.
        #
        # One documented exception. US retailers on a 52/53-week calendar end their
        # year on the Saturday nearest 31 January, so the two vendors disagree by a
        # month: coreiq_av_company_overview says "February" while the NASDAQ calendar
        # labels the same quarter "Jan/2025". Verified on STG — PVH, ZUMZ and VRA all
        # announce their `Jan/YYYY` quarter in March, and their annual accounts end
        # 2026-02-28. That IS the annual announcement, and we were discarding it.
        #
        # The tolerance is deliberately narrow so it can never grab a wrong quarter:
        #   * only used when NO quarter matches the FYE month exactly, and
        #   * only for a month exactly one away, and
        #   * only when exactly one such month exists for that ticker.
        # A December-FYE company always has an exact Dec quarter, so it never reaches
        # this branch; one holding only a June quarter is 6 months away and stays on
        # hold. Recovers 6 companies on STG, all genuine annual rows.
        months_by_ticker: Dict[str, Set[int]] = defaultdict(set)
        for r in nasdaq_rows:
            tk = (r.get("ticker") or "").strip()
            if r.get("fqe_month_num"):
                months_by_ticker[tk].add(int(r["fqe_month_num"]))

        annual_month: Dict[str, int] = {}
        for tk, months in months_by_ticker.items():
            fye_m = fye_months.get(tk) or 0
            if not fye_m:
                continue
            if fye_m in months:
                annual_month[tk] = fye_m
                continue
            adjacent = {m for m in months if min((m - fye_m) % 12, (fye_m - m) % 12) == 1}
            if len(adjacent) == 1:
                annual_month[tk] = adjacent.pop()

        for r in nasdaq_rows:
            tk = (r.get("ticker") or "").strip()
            fqe_m = r.get("fqe_month_num") or 0
            fye_m = fye_months.get(tk) or 0
            if not fye_m:
                _dropped_no_fye.add(tk)
                continue
            want = annual_month.get(tk)
            if not want or fqe_m != want or fqe_m == 0:
                _dropped_no_q4.add(tk)
                continue
            raw_dt = r.get("earnings_date")
            ed = raw_dt.date() if raw_dt and hasattr(raw_dt, "date") else raw_dt
            if ed is None:
                continue
            raw_fat = r.get("fetched_at_utc")
            _dropped_no_q4.discard(tk)
            ticker_q4_candidates[tk].append({
                "date": ed,
                "fiscal_quarter_ending": (r.get("fiscal_quarter_ending") or ""),
                "fetched_at_utc": raw_fat,
                "fqe_month": fqe_m,
                "fye_month": fye_m,
            })

        def _choose(candidates):
            upcoming = [c for c in candidates if c["date"] >= today]
            past     = [c for c in candidates if c["date"] <  today]
            return (min(upcoming, key=lambda c: c["date"]) if upcoming
                    else max(past, key=lambda c: c["date"]) if past
                    else None)

        for tk, candidates in ticker_q4_candidates.items():
            chosen = _choose(candidates)
            if chosen:
                nasdaq_result[tk] = {**chosen, "source": "nasdaq", "fiscal_q": 4}


        # Every company that failed a gate is counted and named, so "why is this
        # one On hold?" is answerable from the log instead of a DB investigation.
        log_timing(
            "_annual_q4.nasdaq", (perf_counter() - _t) * 1000,
            f"resolved={len(nasdaq_result)} "
            f"dropped_no_fiscal_year_end={len(_dropped_no_fye)} "
            f"dropped_no_annual_row={len(_dropped_no_q4)} "
            f"no_fye_sample={sorted(_dropped_no_fye)[:10]} "
            f"no_annual_sample={sorted(_dropped_no_q4)[:10]}",
            level="INFO",
        )
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_annual_q4_report_dates_bulk", operation="SELECT_NASDAQ")

    # ── YF path: composite tickers (exact ticker match only) ─────────────────
    # Safety: composite tickers (e.g. TSCO.L = Tesco UK) must not borrow the
    # NASDAQ calendar row for the base US ticker (TSCO = Tractor Supply Co.).
    # We query by exact composite ticker and apply latest-ingested_at dedup
    # per fiscal period before checking Q4 — mirrors NASDAQ's fetched_at_utc dedup.
    _t1 = perf_counter()
    yf_result: Dict[str, Dict[str, Any]] = {}
    try:
        # yf_earnings_calendar stores the STRIPPED base in `ticker` (e.g. 'TSCO',
        # 'JD', '1913') and the EXACT composite in `yf_symbol` (e.g. 'TSCO.L',
        # 'JD.L', '1913.HK').  Forecast tickers are composite, so we must match on
        # `yf_symbol` — keying on `ticker` returns nothing AND would collide base
        # symbols across companies (TSCO.L=Tesco vs TSCO=Tractor Supply).
        yf_cal_rows = db_manager.execute_query_readonly(
            """
            SELECT yf_symbol AS ticker, earnings_date, ingested_at
            FROM coreiq_yf_earnings_calendar
            WHERE yf_symbol LIKE '%.%'
              AND earnings_date IS NOT NULL
            """,
            {},
        )
        if yf_cal_rows:
            # FYE month for composite tickers.  The AV/YF *overview* tables omit
            # foreign listings, so the forecast's own fiscal-year-end
            # (coreiq_model_forecasts.last_actual_date) is the authoritative source;
            # _fiscal_year_end_bulk() is kept only as a fallback.
            fye_fc_rows = db_manager.execute_query_readonly(
                """
                SELECT ticker, MONTH(MAX(last_actual_date)) AS fye_month
                FROM coreiq_model_forecasts
                WHERE ticker LIKE '%.%' AND last_actual_date IS NOT NULL
                GROUP BY ticker
                """,
                {},
            )
            fye_by_symbol: Dict[str, int] = {
                (r.get("ticker") or "").strip(): r.get("fye_month")
                for r in (fye_fc_rows or [])
                if r.get("fye_month")
            }
            fye_bulk = _fiscal_year_end_bulk()  # {ticker: month_name_str}, cached fallback

            # Build (ticker, earnings_date, ingested_at) grouped by composite yf_symbol.
            ticker_raw: Dict[str, list] = defaultdict(list)
            for r in yf_cal_rows:
                tk = (r.get("ticker") or "").strip()
                raw_dt = r.get("earnings_date")
                ed = raw_dt.date() if raw_dt and hasattr(raw_dt, "date") else raw_dt
                ing = r.get("ingested_at")
                if tk and ed is not None:
                    ticker_raw[tk].append({"date": ed, "ingested_at": ing})

            today = date.today()
            for tk, rows_list in ticker_raw.items():
                # Composite-ticker FYE: forecast last_actual_date first, bulk fallback.
                fye_month = fye_by_symbol.get(tk) or _fye_name_to_month_num(fye_bulk.get(tk))
                if not fye_month:
                    continue

                # Derive FQE date for each earnings row, then dedup by
                # (fqe_date) keeping latest ingested_at — same idea as NASDAQ
                # fetched_at_utc dedup per (ticker, fiscal_quarter_ending).
                fqe_groups: Dict[date, Dict] = {}  # fqe_date → latest-ingested row
                for row in rows_list:
                    fqe_date = _yf_fqe_date_for_earnings_date(row["date"], fye_month)
                    if fqe_date is None:
                        continue
                    prev = fqe_groups.get(fqe_date)
                    if prev is None or (row["ingested_at"] or "") > (prev["ingested_at"] or ""):
                        fqe_groups[fqe_date] = row

                # Filter to Q4 (fqe_month == fye_month) and apply selection
                q4_upcoming: list = []
                q4_past: list = []
                for fqe_date, row in fqe_groups.items():
                    if fqe_date.month != fye_month:
                        continue  # not Q4
                    fqe_str = f"{_MONTH_NUM_TO_ABB[fqe_date.month]}/{fqe_date.year}"
                    info = {
                        "date": row["date"],
                        "fiscal_quarter_ending": fqe_str,
                        "fetched_at_utc": row["ingested_at"],
                        "fqe_month": fqe_date.month,
                        "fye_month": fye_month,
                        "source": "yf",
                        "fiscal_q": 4,
                    }
                    if row["date"] >= today:
                        q4_upcoming.append(info)
                    else:
                        q4_past.append(info)

                chosen = (min(q4_upcoming, key=lambda c: c["date"]) if q4_upcoming
                          else max(q4_past, key=lambda c: c["date"]) if q4_past
                          else None)
                if chosen:
                    yf_result[tk] = chosen

        log_timing("_annual_q4.yf", (perf_counter() - _t1) * 1000,
                   f"tickers={len(yf_result)}", level="INFO")
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_annual_q4_report_dates_bulk", operation="SELECT_YF")

    # NASDAQ takes priority where both paths produced a result
    result: Dict[str, Dict[str, Any]] = {**yf_result, **nasdaq_result}

    # Staleness guard: keep all upcoming Q4 dates, but drop a *past* Q4 date older
    # than ~15 months. Some foreign tickers only have old Q4-derivable calendar
    # rows (recent rows are interim/half-year), and showing a years-old date as the
    # "Reporting Date" misleads more than "—" does. 15 months gives buffer for late
    # annual reporters while still excluding clearly outdated dates.
    # NO staleness filter and NO projection: the Reporting Date column shows the
    # real annual announcement date held in the calendar table, whatever its age.
    # Product decision 2026-07-30 — an estimated or hidden date is worse than an
    # old but factual one, because the column must be traceable to a row in
    # coreiq_nasdaq_earnings_calendar / coreiq_yf_earnings_calendar.
    #
    # Selection above already prefers the nearest UPCOMING annual date and only
    # falls back to the most recent PAST one, so a past date here means the
    # calendar genuinely holds nothing newer for that company.
    _today = date.today()
    _past = sum(1 for info in result.values() if info["date"] < _today)
    log_timing("_annual_q4.TOTAL", (perf_counter() - _t) * 1000,
               f"nasdaq={len(nasdaq_result)} yf={len(yf_result)} "
               f"total={len(result)} upcoming={len(result) - _past} already_reported={_past}",
               level="INFO")
    return result


def _fiscal_q_from_months(fqe_month: int, fye_month: int) -> int:
    """Fiscal quarter (1-4) of a quarter-ending month given the fiscal-year-end month."""
    if not fqe_month or not fye_month:
        return 0
    fy_start = (fye_month % 12) + 1
    months_into_fy = (fqe_month - fy_start) % 12 + 1
    return (months_into_fy + 2) // 3


def _quarterly_report_dates_bulk() -> Dict[str, Dict[str, Any]]:
    """
    Reporting date for the *relevant* fiscal quarter per ticker — Q1/Q2/Q3/Q4 all
    eligible (unlike the annual helper, which is Q4/full-year only).

    Returns Dict[ticker, info_dict] with the same keys as _annual_q4_report_dates_bulk
    (date, fiscal_quarter_ending, source, fetched_at_utc, fqe_month, fye_month, fiscal_q),
    selecting the nearest upcoming earnings date of any quarter (>= today), else the
    most recent past one. NASDAQ keys on `ticker`; YF keys on the composite `yf_symbol`.
    """
    _t = perf_counter()
    _MONTH_NUM_TO_ABB = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    today = date.today()

    def _pick(candidates: list) -> Optional[Dict[str, Any]]:
        upcoming = [c for c in candidates if c["date"] >= today]
        past = [c for c in candidates if c["date"] < today]
        if upcoming:
            return min(upcoming, key=lambda c: c["date"])
        if past:
            return max(past, key=lambda c: c["date"])
        return None

    # ── NASDAQ path (non-composite tickers) ─────────────────────────────────────
    nasdaq_result: Dict[str, Dict[str, Any]] = {}
    try:
        nasdaq_rows = db_manager.execute_query_readonly(
            """
            SELECT lc.ticker, lc.earnings_date, lc.fiscal_quarter_ending, lc.fetched_at_utc,
                CASE SUBSTRING_INDEX(lc.fiscal_quarter_ending, '/', 1)
                    WHEN 'Jan' THEN 1  WHEN 'Feb' THEN 2  WHEN 'Mar' THEN 3
                    WHEN 'Apr' THEN 4  WHEN 'May' THEN 5  WHEN 'Jun' THEN 6
                    WHEN 'Jul' THEN 7  WHEN 'Aug' THEN 8  WHEN 'Sep' THEN 9
                    WHEN 'Oct' THEN 10 WHEN 'Nov' THEN 11 WHEN 'Dec' THEN 12
                    ELSE 0 END AS fqe_month_num,
                CASE LOWER(LEFT(TRIM(ov.fiscal_year_end), 3))
                    WHEN 'jan' THEN 1  WHEN 'feb' THEN 2  WHEN 'mar' THEN 3
                    WHEN 'apr' THEN 4  WHEN 'may' THEN 5  WHEN 'jun' THEN 6
                    WHEN 'jul' THEN 7  WHEN 'aug' THEN 8  WHEN 'sep' THEN 9
                    WHEN 'oct' THEN 10 WHEN 'nov' THEN 11 WHEN 'dec' THEN 12
                    ELSE -1 END AS fye_month_num
            FROM (
                SELECT ticker, earnings_date, fiscal_quarter_ending, fetched_at_utc, id
                FROM (
                    SELECT ticker, earnings_date, fiscal_quarter_ending, fetched_at_utc, id,
                        ROW_NUMBER() OVER (
                            PARTITION BY ticker, fiscal_quarter_ending
                            ORDER BY fetched_at_utc DESC, earnings_date DESC, id DESC
                        ) AS rn
                    FROM coreiq_nasdaq_earnings_calendar
                    WHERE fiscal_quarter_ending IS NOT NULL
                      AND earnings_date IS NOT NULL
                      AND ticker NOT LIKE '%.%'
                ) ranked
                WHERE rn = 1
            ) lc
            JOIN (
                SELECT ticker, fiscal_year_end FROM (
                    SELECT ticker, fiscal_year_end,
                        ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY fetched_at_utc DESC) AS rn
                    FROM coreiq_av_company_overview
                    WHERE ticker IS NOT NULL AND fiscal_year_end IS NOT NULL AND fiscal_year_end <> ''
                ) fye_ranked WHERE rn = 1
            ) ov ON ov.ticker = lc.ticker
            """,
            {},
        )
        ticker_candidates: Dict[str, list] = defaultdict(list)
        for r in nasdaq_rows:
            tk = (r.get("ticker") or "").strip()
            fqe_m = r.get("fqe_month_num") or 0
            fye_m = r.get("fye_month_num") or -1
            if fqe_m == 0:
                continue
            raw_dt = r.get("earnings_date")
            ed = raw_dt.date() if raw_dt and hasattr(raw_dt, "date") else raw_dt
            if ed is None:
                continue
            ticker_candidates[tk].append({
                "date": ed,
                "fiscal_quarter_ending": (r.get("fiscal_quarter_ending") or ""),
                "fetched_at_utc": r.get("fetched_at_utc"),
                "fqe_month": fqe_m,
                "fye_month": fye_m,
                "fiscal_q": _fiscal_q_from_months(fqe_m, fye_m),
            })
        for tk, candidates in ticker_candidates.items():
            chosen = _pick(candidates)
            if chosen:
                nasdaq_result[tk] = {**chosen, "source": "nasdaq"}
        log_timing("_quarterly_dates.nasdaq", (perf_counter() - _t) * 1000,
                   f"tickers={len(nasdaq_result)}", level="INFO")
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_quarterly_report_dates_bulk", operation="SELECT_NASDAQ")

    # ── YF path (composite tickers, exact yf_symbol match) ──────────────────────
    _t1 = perf_counter()
    yf_result: Dict[str, Dict[str, Any]] = {}
    try:
        yf_cal_rows = db_manager.execute_query_readonly(
            """
            SELECT yf_symbol AS ticker, earnings_date, ingested_at
            FROM coreiq_yf_earnings_calendar
            WHERE yf_symbol LIKE '%.%' AND earnings_date IS NOT NULL
            """,
            {},
        )
        if yf_cal_rows:
            fye_fc_rows = db_manager.execute_query_readonly(
                """
                SELECT ticker, MONTH(MAX(last_actual_date)) AS fye_month
                FROM coreiq_model_forecasts_quarterly
                WHERE ticker LIKE '%.%' AND last_actual_date IS NOT NULL
                GROUP BY ticker
                """,
                {},
            )
            fye_by_symbol: Dict[str, int] = {
                (r.get("ticker") or "").strip(): r.get("fye_month")
                for r in (fye_fc_rows or []) if r.get("fye_month")
            }
            fye_bulk = _fiscal_year_end_bulk()

            ticker_raw: Dict[str, list] = defaultdict(list)
            for r in yf_cal_rows:
                tk = (r.get("ticker") or "").strip()
                raw_dt = r.get("earnings_date")
                ed = raw_dt.date() if raw_dt and hasattr(raw_dt, "date") else raw_dt
                if tk and ed is not None:
                    ticker_raw[tk].append({"date": ed, "ingested_at": r.get("ingested_at")})

            for tk, rows_list in ticker_raw.items():
                fye_month = fye_by_symbol.get(tk) or _fye_name_to_month_num(fye_bulk.get(tk))
                if not fye_month:
                    continue
                fqe_groups: Dict[date, Dict] = {}
                for row in rows_list:
                    fqe_date = _yf_fqe_date_for_earnings_date(row["date"], fye_month)
                    if fqe_date is None:
                        continue
                    prev = fqe_groups.get(fqe_date)
                    if prev is None or (row["ingested_at"] or "") > (prev["ingested_at"] or ""):
                        fqe_groups[fqe_date] = row
                candidates = []
                for fqe_date, row in fqe_groups.items():
                    candidates.append({
                        "date": row["date"],
                        "fiscal_quarter_ending": f"{_MONTH_NUM_TO_ABB[fqe_date.month]}/{fqe_date.year}",
                        "fetched_at_utc": row["ingested_at"],
                        "fqe_month": fqe_date.month,
                        "fye_month": fye_month,
                        "fiscal_q": _fiscal_q_from_months(fqe_date.month, fye_month),
                        "source": "yf",
                    })
                chosen = _pick(candidates)
                if chosen:
                    yf_result[tk] = chosen
        log_timing("_quarterly_dates.yf", (perf_counter() - _t1) * 1000,
                   f"tickers={len(yf_result)}", level="INFO")
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_quarterly_report_dates_bulk", operation="SELECT_YF")

    result: Dict[str, Dict[str, Any]] = {**yf_result, **nasdaq_result}

    # Staleness guard: drop past dates older than ~15 months (quarterly reporters
    # that have gone quiet). Upcoming dates are always kept.
    _stale_cutoff = date.today() - timedelta(days=460)
    _before_guard = len(result)
    result = {tk: info for tk, info in result.items() if info["date"] >= _stale_cutoff}
    log_timing("_quarterly_dates.TOTAL", (perf_counter() - _t) * 1000,
               f"nasdaq={len(nasdaq_result)} yf={len(yf_result)} total={len(result)} "
               f"dropped_stale={_before_guard - len(result)}", level="INFO")
    return result


@st.cache_data(ttl=3600, show_spinner=False)
def _fiscal_year_end_bulk() -> Dict[str, Optional[str]]:
    """ticker → fiscal_year_end string (month name), one row per ticker using latest fetch.

    Sources, applied in order — each only fills tickers the previous ones missed:
      1. coreiq_av_company_overview — the vendor-declared FYE; deduped by
         fetched_at_utc DESC so a stale duplicate overview row cannot override it.
      2. coreiq_model_forecasts.last_actual_date — the fiscal period end our own
         engine last modelled. Authoritative and near-universal: it is already what
         the refresh dialog prints in its "Fiscal Period" column.
      3. coreiq_av_financials_income_statement.fiscal_date_ending (annual) — the
         period end of the newest published annual statement.
      4. coreiq_yf_company_overview payload_json $.info.lastFiscalYearEnd — Unix
         epoch → month name; deduped by ingested_at DESC.

    Sources 2 and 3 were added 2026-07-30. Source 1 alone resolved 221 tickers on
    STG while the full chain resolves 401, and the annual reporting date cannot be
    identified without an FYE — so 39 companies were being shown "On hold" while
    their announcement date sat in the calendar. See spec
    docs/superpowers/specs/2026-07-30-reporting-date-resolution-hardening-design.md
    """
    _t0 = perf_counter()
    result: Dict[str, Optional[str]] = {}

    # ── Source 1: Alpha Vantage — latest fetched_at_utc per ticker ───────────
    try:
        av_rows = db_manager.execute_query_readonly(
            """
            SELECT ticker, fiscal_year_end
            FROM (
                SELECT
                    ticker,
                    fiscal_year_end,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker
                        ORDER BY fetched_at_utc DESC
                    ) AS rn
                FROM coreiq_av_company_overview
                WHERE ticker IS NOT NULL
                  AND fiscal_year_end IS NOT NULL
                  AND fiscal_year_end <> ''
            ) x
            WHERE rn = 1
            """,
            {},
        )
        for r in av_rows or []:
            tk = (r.get("ticker") or "").strip()
            fye = (r.get("fiscal_year_end") or "").strip()
            if tk and fye:
                result[tk] = fye
        _t1 = perf_counter()
        log_timing("_fiscal_year_end_bulk_AV", (_t1 - _t0) * 1000,
                   f"tickers_av={len(result)}", level="INFO")
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_fiscal_year_end_bulk", operation="SELECT_AV")
        _t1 = perf_counter()

    # ── Source 2: our own modelled fiscal period end ─────────────────────────
    # coreiq_model_forecasts.last_actual_date is the period end of the newest
    # actuals the forecast engine consumed. Present for essentially every company
    # in the refresh dialog (the dialog is built from this table), so it closes
    # most of the gap left by a sparse AV overview.
    try:
        fc_rows = db_manager.execute_query_readonly(
            """
            SELECT ticker, MONTH(MAX(last_actual_date)) AS fye_month
            FROM coreiq_model_forecasts
            WHERE last_actual_date IS NOT NULL
            GROUP BY ticker
            """,
            {},
        )
        fc_added = 0
        for r in fc_rows or []:
            tk = (r.get("ticker") or "").strip()
            month_num = r.get("fye_month")
            if not tk or tk in result or not month_num:
                continue
            result[tk] = calendar.month_name[int(month_num)]
            fc_added += 1
        log_timing("_fiscal_year_end_bulk_FORECASTS", (perf_counter() - _t1) * 1000,
                   f"added={fc_added}", level="INFO")
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_fiscal_year_end_bulk", operation="SELECT_FORECASTS")

    # ── Source 3: newest published annual income statement ───────────────────
    try:
        is_rows = db_manager.execute_query_readonly(
            """
            SELECT ticker, MONTH(MAX(fiscal_date_ending)) AS fye_month
            FROM coreiq_av_financials_income_statement
            WHERE report_type = 'annual' AND fiscal_date_ending IS NOT NULL
            GROUP BY ticker
            """,
            {},
        )
        is_added = 0
        for r in is_rows or []:
            tk = (r.get("ticker") or "").strip()
            month_num = r.get("fye_month")
            if not tk or tk in result or not month_num:
                continue
            result[tk] = calendar.month_name[int(month_num)]
            is_added += 1
        log_timing("_fiscal_year_end_bulk_INCOME_STMT", (perf_counter() - _t1) * 1000,
                   f"added={is_added}", level="INFO")
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_fiscal_year_end_bulk", operation="SELECT_INCOME_STMT")

    # ── Source 4: YFinance — latest ingested_at per ticker ───────────────────
    # payload_json $.info.lastFiscalYearEnd is a Unix epoch timestamp.
    # Only fills tickers not already resolved by AV (composite / non-US tickers).
    try:
        yf_rows = db_manager.execute_query_readonly(
            """
            SELECT ticker,
                   CAST(JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.info.lastFiscalYearEnd'))
                        AS UNSIGNED) AS fye_epoch
            FROM (
                SELECT
                    ticker,
                    payload_json,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker
                        ORDER BY ingested_at DESC
                    ) AS rn
                FROM coreiq_yf_company_overview
                WHERE ticker IS NOT NULL
                  AND payload_json IS NOT NULL
            ) x
            WHERE rn = 1
              AND JSON_EXTRACT(payload_json, '$.info.lastFiscalYearEnd') IS NOT NULL
            """,
            {},
        )
        yf_added = 0
        for r in yf_rows or []:
            tk = (r.get("ticker") or "").strip()
            if not tk or tk in result:
                continue
            epoch = r.get("fye_epoch")
            if not epoch:
                continue
            try:
                month_name = datetime.fromtimestamp(int(epoch)).strftime("%B")
                result[tk] = month_name
                yf_added += 1
            except (OSError, ValueError, OverflowError):
                pass
        log_timing("_fiscal_year_end_bulk_YF", (perf_counter() - _t1) * 1000,
                   f"yf_rows_scanned={len(yf_rows or [])} yf_added={yf_added}", level="INFO")
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_fiscal_year_end_bulk", operation="SELECT_YF")

    log_timing("_fiscal_year_end_bulk_TOTAL", (perf_counter() - _t0) * 1000,
               f"total_tickers={len(result)}", level="INFO")
    return result


def _fmt_fiscal_year_end(raw: Optional[str]) -> str:
    """Format fiscal_year_end for display: 'December', 'Dec', '12', '2024-12-31' → 'December'."""
    if not raw:
        return "—"
    raw = raw.strip()
    lower = raw.lower()
    _FULL = {
        "january": "January", "february": "February", "march": "March",
        "april": "April", "may": "May", "june": "June",
        "july": "July", "august": "August", "september": "September",
        "october": "October", "november": "November", "december": "December",
    }
    _SHORT = {
        "jan": "January", "feb": "February", "mar": "March", "apr": "April",
        "may": "May", "jun": "June", "jul": "July", "aug": "August",
        "sep": "September", "oct": "October", "nov": "November", "dec": "December",
    }
    if lower in _FULL:
        return _FULL[lower]
    if lower[:3] in _SHORT:
        return _SHORT[lower[:3]]
    # ISO date "2024-12-31" — extract month
    if "-" in raw:
        parts = raw.split("-")
        try:
            m = int(parts[1]) if len(parts) >= 3 else int(parts[-1])
            if 1 <= m <= 12:
                return calendar.month_name[m]
        except (ValueError, IndexError):
            pass
    # Plain number
    try:
        m = int(raw)
        if 1 <= m <= 12:
            return calendar.month_name[m]
    except ValueError:
        pass
    return raw


# ─── Public API ───────────────────────────────────────────────────────────────

def _get_quarterly_refresh_table_data() -> List[Dict[str, Any]]:
    """Assemble Refresh popup rows for quarterly forecasts.

    Reads coreiq_model_forecasts_quarterly for last_refresh + the quarter-end the
    forecast used, and _quarterly_report_dates_bulk() for the relevant quarter's
    reporting date. Same row shape as the annual branch so the UI renders both.
    """
    _t0 = perf_counter()
    rows = db_manager.execute_query_readonly(
        """
        SELECT ticker,
               MAX(computed_at)      AS last_refresh,
               MAX(last_actual_date) AS fiscal_period_end
        FROM coreiq_model_forecasts_quarterly
        GROUP BY ticker
        ORDER BY ticker ASC
        """,
        {},
    )
    if not rows:
        return []

    meta_rows = db_manager.execute_query_readonly(
        """
        SELECT ticker, company_name, exchange
        FROM coreiq_model_forecasts_quarterly
        WHERE is_best_model = 1 AND metric = 'total_revenue'
        ORDER BY ticker ASC
        """,
        {},
    )
    meta: Dict[str, Dict[str, str]] = {}
    for r in (meta_rows or []):
        tk = (r.get("ticker") or "").strip()
        if tk and tk not in meta:
            meta[tk] = {
                "company_name": (r.get("company_name") or "").strip(),
                "exchange": (r.get("exchange") or "").strip(),
            }

    quarterly_map = _quarterly_report_dates_bulk()

    result: List[Dict[str, Any]] = []
    for row in rows:
        ticker = (row.get("ticker") or "").strip()
        if not ticker:
            continue
        base = ticker.split(".")[0] if "." in ticker else ticker
        m = meta.get(ticker, {})

        info = quarterly_map.get(ticker) if "." in ticker else quarterly_map.get(base)
        rep = info["date"] if info else None
        reported_str = rep.strftime("%b %d, %Y") if rep else "—"
        fiscal_q = info.get("fiscal_q") if info else None

        fp = row.get("fiscal_period_end")
        if fp and hasattr(fp, "strftime"):
            fiscal_period_str = fp.strftime("%b %d")
        else:
            fiscal_period_str = str(fp)[5:10] if fp else "—"

        lr = row.get("last_refresh")
        if lr and hasattr(lr, "strftime"):
            lr_str = lr.strftime("%b %d, %Y %H:%M UTC")
        else:
            lr_str = str(lr)[:16] if lr else "—"

        result.append({
            "ticker": ticker,
            "company_name": m.get("company_name", ""),
            "exchange": m.get("exchange", ""),
            "annual_reported_on": reported_str,   # generic "Reporting Date" column
            "fiscal_period": fiscal_period_str,
            "last_refresh": lr_str,
            "annual_reporting_quarter": f"Q{fiscal_q}" if fiscal_q else "",
            "annual_reporting_source": info.get("source", "") if info else "",
            "annual_fiscal_quarter_ending": info.get("fiscal_quarter_ending", "") if info else "",
        })

    log_timing("DIALOG_TOTAL_get_refresh_table_data_quarterly", (perf_counter() - _t0) * 1000,
               f"result_rows={len(result)}", level="INFO")
    return result


# The popup's contents change only when a refresh actually runs, and both sync
# paths call get_refresh_table_data.clear() explicitly. A 2-minute TTL therefore
# bought nothing and made the dialog re-run a ~4s (cold: ~24s) query for anyone
# who opened it more than two minutes after the last one — which is the normal
# case. Long TTL + explicit invalidation keeps the data exactly as fresh while
# making the dialog open instantly.
@st.cache_data(ttl=3600, show_spinner=False)
def get_refresh_table_data(period_type: str = "annual") -> List[Dict[str, Any]]:
    """
    Return assembled row data for the Refresh popup table.

    Each row dict:
        ticker, company_name, exchange,
        annual_reported_on (str — annual results announcement date),
        fiscal_period (str — annual fiscal period end the forecast used),
        last_refresh (str — formatted datetime)

    ``period_type`` — 'annual' (Q4/full-year reporting date) or 'quarterly'
    (the relevant fiscal quarter's reporting date).

    Performance: two narrow queries instead of one wide GROUP BY to exploit
    the idx_ticker_computed loose-index-scan for MAX(computed_at) and the
    idx_best_metric_ticker covering index for company metadata.
    """
    if period_type.lower() == "quarterly":
        return _get_quarterly_refresh_table_data()

    _t0 = perf_counter()

    # ── Q1: MAX(computed_at) per ticker — uses idx_ticker_computed loose index scan ──
    rows = db_manager.execute_query_readonly(
        """
        SELECT ticker,
               MAX(computed_at)      AS last_refresh,
               MAX(last_actual_date) AS fiscal_period_end
        FROM coreiq_model_forecasts
        GROUP BY ticker
        ORDER BY ticker ASC
        """,
        {},
    )
    _t1 = perf_counter()
    log_timing("DIALOG_Q1_ticker_max_computed_at", (_t1 - _t0) * 1000,
               f"rows={len(rows or [])}", level="INFO")

    if not rows:
        return []

    # ── Q2: company_name + exchange — uses idx_best_metric_ticker covering index ──
    meta_rows = db_manager.execute_query_readonly(
        """
        SELECT ticker, company_name, exchange
        FROM coreiq_model_forecasts
        WHERE is_best_model = 1 AND metric = 'total_revenue'
        ORDER BY ticker ASC
        """,
        {},
    )
    _t2 = perf_counter()
    log_timing("DIALOG_Q2_company_meta", (_t2 - _t1) * 1000,
               f"rows={len(meta_rows or [])}", level="INFO")

    # Build O(1) lookup for company meta (one entry per ticker)
    meta: Dict[str, Dict[str, str]] = {}
    for r in (meta_rows or []):
        tk = (r.get("ticker") or "").strip()
        if tk and tk not in meta:
            meta[tk] = {
                "company_name": (r.get("company_name") or "").strip(),
                "exchange": (r.get("exchange") or "").strip(),
            }

    # ── Q3: Q4/full-year annual reporting date (strictly fiscal_q == 4 only) ───
    # Annual forecasting is full-year only; never show a Q1/Q2/Q3 date here.
    annual_map = _annual_q4_report_dates_bulk()
    _t3 = perf_counter()
    log_timing("DIALOG_Q3_annual_q4_dates", (_t3 - _t2) * 1000,
               f"tickers={len(annual_map)}", level="INFO")

    result: List[Dict[str, Any]] = []
    for row in rows:
        ticker = (row.get("ticker") or "").strip()
        if not ticker:
            continue
        base = ticker.split(".")[0] if "." in ticker else ticker

        m = meta.get(ticker, {})

        # Annual Q4 reporting date — strict Q4/full-year only:
        #   - Composite tickers (TSCO.L = Tesco UK): exact lookup (YF path).
        #     Never strip suffix — TSCO = Tractor Supply Co. is a different company.
        #   - Non-composite tickers: base ticker lookup (NASDAQ path).
        # annual_map values are info dicts; missing key → "—".
        ann_info = annual_map.get(ticker) if "." in ticker else annual_map.get(base)
        ann = ann_info["date"] if ann_info else None
        # Verbatim from the calendar table — never estimated, never projected.
        annual_str = ann.strftime("%b %d, %Y") if ann else "—"

        # Fiscal period from coreiq_model_forecasts.last_actual_date.
        # Show month + day only (no year): the column denotes the fiscal-year-END
        # pattern (e.g. "Feb 28"), not a specific year — showing a year made it look
        # mismatched against the Reporting Date (which can be a later fiscal year).
        fp = row.get("fiscal_period_end")
        if fp and hasattr(fp, "strftime"):
            fiscal_period_str = fp.strftime("%b %d")
        else:
            fiscal_period_str = str(fp)[5:10] if fp else "—"

        lr = row.get("last_refresh")
        if lr and hasattr(lr, "strftime"):
            lr_str = lr.strftime("%b %d, %Y %H:%M UTC")
        else:
            lr_str = str(lr)[:16] if lr else "—"

        result.append({
            "ticker": ticker,
            "company_name": m.get("company_name", ""),
            "exchange": m.get("exchange", ""),
            "annual_reported_on": annual_str,
            "fiscal_period": fiscal_period_str,
            "last_refresh": lr_str,
            # ── debug / validation fields (not rendered in UI by default) ──
            "annual_reporting_quarter": "Q4" if ann_info else "",
            "annual_reporting_source": ann_info.get("source", "") if ann_info else "",
            "annual_fiscal_quarter_ending": ann_info.get("fiscal_quarter_ending", "") if ann_info else "",
            "annual_reporting_fetched_at_utc": (
                ann_info["fetched_at_utc"].strftime("%Y-%m-%d %H:%M UTC")
                if ann_info and ann_info.get("fetched_at_utc") and hasattr(ann_info["fetched_at_utc"], "strftime")
                else ""
            ),
        })

    log_timing("DIALOG_TOTAL_get_refresh_table_data", (perf_counter() - _t0) * 1000,
               f"result_rows={len(result)}", level="INFO")
    return result


def get_company_info_for_ticker(ticker: str) -> Dict[str, str]:
    """
    Look up company_name and exchange for a single ticker from coreiq_companies.
    Used when enriching new forecast results.
    """
    base = ticker.split(".")[0] if "." in ticker else ticker
    acronym = ticker.split(".")[-1] if "." in ticker else ""
    try:
        rows = db_manager.execute_query_readonly(
            """
            SELECT name_coresight, exchange
            FROM coreiq_companies
            WHERE ticker = :base
              AND (:acronym = '' OR COALESCE(exchange_acronym, '') = :acronym)
            LIMIT 1
            """,
            {"base": base, "acronym": acronym},
        )
        if rows:
            return {
                "company_name": (rows[0].get("name_coresight") or "").strip(),
                "exchange": (rows[0].get("exchange") or "").strip(),
            }
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_refresh_service",
            component="get_company_info_for_ticker",
            operation="SELECT",
            context={"ticker": ticker},
        )
    return {"company_name": "", "exchange": ""}


# ─── Email notification ───────────────────────────────────────────────────────

_FORECAST_CC = ["dataautomation@coresight.com"]


def _get_admin_recipients() -> List[str]:
    """Return all admin + super_user emails from the DB; fall back to hardcoded set."""
    from core.access_control import UserRolesManager
    fallback = list(UserRolesManager._FALLBACK_ADMINS)
    log_info(f"[forecast_email_test] hardcoded fallback admins: {fallback}")
    try:
        rows = db_manager.execute_query_readonly(
            "SELECT user_email, role FROM coreiq_user_roles WHERE role IN ('admin', 'super_user')",
            {},
        )
        log_info(f"[forecast_email_test] DB rows from coreiq_user_roles: {rows}")
        if rows:
            emails = [r["user_email"] for r in rows if r.get("user_email")]
            seen = {e.lower() for e in emails}
            for e in fallback:
                if e.lower() not in seen:
                    emails.append(e)
                    seen.add(e.lower())
            log_info(f"[forecast_email_test] merged admin+super_user list (DB + fallback): {emails}")
            return emails
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="_get_admin_recipients", operation="SELECT")
    log_info(f"[forecast_email_test] DB unavailable — using fallback only: {fallback}")
    return fallback


def send_model_refresh_email(
    *,
    triggered_by: str,
    results: List[Dict[str, Any]],
    recipients: Optional[List[str]] = None,
    period_type: str = "annual",
) -> bool:
    """
    Send a formatted HTML notification email when model refresh runs complete.
    ``results`` should be list of dicts from sync_forecast_for_ticker / sync_all_eligible
    (or their quarterly siblings), optionally enriched with company_name, exchange.
    ``period_type`` selects the reporting-date source: 'annual' (Q4 only) or
    'quarterly' (the relevant fiscal quarter). To: all admin + super_user accounts.
    Cc: dataautomation@coresight.com.
    """
    if not results:
        return False
    _is_quarterly = period_type.lower() == "quarterly"

    smtp_host = os.getenv("SMTP_SERVER", "smtp.office365.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    from_addr = os.getenv("FROM_EMAIL", "dataautomation@coresight.com")
    password = os.getenv("EMAIL_PASSWORD", "")
    if not password:
        log_info("[forecast_refresh] Skipping email: EMAIL_PASSWORD not set")
        return False

    if not recipients:
        # Explicit override wins (FORECAST_EMAIL_RECIPIENTS = comma-separated emails);
        # otherwise the admin + super_user list from the DB (+ hardcoded fallback).
        _override = os.getenv("FORECAST_EMAIL_RECIPIENTS", "").strip()
        if _override:
            recipients = [e.strip() for e in _override.split(",") if e.strip() and "@" in e]
        else:
            recipients = _get_admin_recipients()

    # Include the human who triggered the run. Guard on "@" so a non-email
    # trigger label (e.g. the "auto-scheduler" automated run) is never added as
    # a recipient — that would be an invalid SMTP address and fail the send.
    if triggered_by and "@" in triggered_by and triggered_by not in recipients:
        recipients = recipients + [triggered_by]

    cc_preview = [a for a in _FORECAST_CC if a not in recipients]

    # Always log addresses for audit; send is conditional on FORECAST_EMAIL_TEST_MODE
    log_info(f"[forecast_email] triggered_by: {triggered_by}")
    log_info(f"[forecast_email] TO  (admin+superuser+triggerer): {recipients}")
    log_info(f"[forecast_email] CC  (dataautomation): {cc_preview}")
    log_info(f"[forecast_email] FROM: {from_addr}")

    now_str = datetime.utcnow().strftime("%b %d, %Y %H:%M UTC")
    updated = [r for r in results if r.get("status") == "updated"]
    errors  = [r for r in results if r.get("status") == "error"]
    skipped = [r for r in results if r.get("status") not in ("updated", "error")]

    # Reporting date source depends on cadence: annual → Q4/full-year only;
    # quarterly → the relevant fiscal quarter (any of Q1–Q4).
    reporting_map = _quarterly_report_dates_bulk() if _is_quarterly else _annual_q4_report_dates_bulk()
    fiscal_map    = _fiscal_year_end_bulk()

    def _enrich(r: Dict[str, Any]) -> Dict[str, Any]:
        t    = r.get("ticker", "")
        base = t.split(".")[0] if "." in t else t
        # Composite: exact-ticker lookup (YF path); non-composite: base (NASDAQ path)
        ann_info = reporting_map.get(t) if "." in t else reporting_map.get(base)
        rep      = ann_info["date"] if ann_info else None
        fye      = fiscal_map.get(base)
        return {
            **r,
            "reporting_date": rep.strftime("%b %d, %Y") if rep else "—",
            "fiscal_ending_date": _fmt_fiscal_year_end(fye),
        }

    enriched = [_enrich(r) for r in results]

    def _tr(r: Dict[str, Any]) -> str:
        status = r.get("status", "")
        lr = r.get("computed_at") or now_str
        return (
            f'<tr>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;">{r.get("ticker","")}</td>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;">{r.get("company_name","")}</td>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;">{r.get("exchange","")}</td>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;">{r.get("reporting_date","—")}</td>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;">{r.get("fiscal_ending_date","—")}</td>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;">{lr}</td>'
            f'<td style="padding:6px 10px;border:1px solid #ddd;">{status.title()}</td>'
            f'</tr>'
        )

    rows_html = "".join(_tr(r) for r in enriched)

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;font-size:13px;color:#222;padding:20px;">
  <p><strong>Forecasting Model Refresh — {"Quarterly" if _is_quarterly else "Annual"}</strong></p>
  <p>Triggered by: {triggered_by}<br>Time: {now_str}<br>
  Updated: {len(updated)} &nbsp; Errors: {len(errors)} &nbsp; Skipped: {len(skipped)}</p>
  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#f0f0f0;">
        <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Ticker</th>
        <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Company Name</th>
        <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Exchange</th>
        <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">{"Reporting Date (Quarter)" if _is_quarterly else "Reporting Date (Q4)"}</th>
        <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Fiscal Ending</th>
        <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Last Refresh</th>
        <th style="padding:6px 10px;border:1px solid #ddd;text-align:left;">Status</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="color:#888;font-size:11px;margin-top:16px;">Coresight Research · Revenue Forecasting · Automated notification</p>
</body></html>"""

    # Prefer the rich tabular + charts report (per company: forecast table, model
    # backtest MAPE table, forecast+scenario chart, model-accuracy chart). The simple
    # summary table built above stays as the fallback if the rich build or its DB
    # reads fail — the notification is never lost.
    inline_images: List[Any] = []
    try:
        from data import forecast_email_report as _rep
        _period = "quarterly" if _is_quarterly else "annual"
        _details = _rep.build_details(results, _period)
        if _details:
            _chart_src: Dict[str, str] = {}
            for _det in _details:
                for _cid, _png in _rep.render_charts(_det):
                    inline_images.append((_cid, _png))
                    _chart_src[_cid] = f"cid:{_cid}"
            html_body = _rep.build_report_html(
                period_type=_period, triggered_by=triggered_by, now_str=now_str,
                details=_details, chart_src=_chart_src,
            )
    except Exception as _exc:
        log_structured_error(_exc, page="forecast_refresh_service",
                             component="send_model_refresh_email", operation="build_rich_html")
        inline_images = []

    # Test mode: enrichment always runs (so it can be verified in logs), but SMTP
    # send is skipped unless FORECAST_EMAIL_TEST_MODE is explicitly set to "0".
    # Default "1" preserves the previous behaviour of not sending live emails.
    if os.getenv("FORECAST_EMAIL_TEST_MODE", "1") == "1":
        log_info("[forecast_email] SMTP send skipped (FORECAST_EMAIL_TEST_MODE=1). "
                 "Set FORECAST_EMAIL_TEST_MODE=0 to enable live sends.")
        return True

    # Cc: default dataautomation@; FORECAST_EMAIL_CC overrides it (empty string = no Cc).
    _cc_env = os.getenv("FORECAST_EMAIL_CC")
    if _cc_env is None:
        cc = [a for a in _FORECAST_CC if a not in recipients]
    else:
        cc = [e.strip() for e in _cc_env.split(",")
              if e.strip() and "@" in e and e.strip() not in recipients]
    all_recipients = recipients + cc

    try:
        if inline_images:
            # multipart/related so the cid: chart images render inline (Outlook/Gmail).
            msg = MIMEMultipart("related")
            _alt = MIMEMultipart("alternative")
            _alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(_alt)
            for _cid, _png in inline_images:
                _img = MIMEImage(_png, _subtype="png")
                _img.add_header("Content-ID", f"<{_cid}>")
                _img.add_header("Content-Disposition", "inline", filename=f"{_cid}.png")
                msg.attach(_img)
        else:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        msg["Subject"] = f"Forecast Refresh — {len(updated)} Updated · {now_str}"
        msg["From"] = from_addr
        msg["To"] = ", ".join(recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, all_recipients, msg.as_string())
        log_info(f"[forecast_email] SENT to={recipients} cc={cc}")
        return True
    except Exception as exc:
        log_structured_error(
            exc,
            page="forecast_refresh_service",
            component="send_model_refresh_email",
            operation="smtp_send",
            context=f"to={recipients} cc={cc}",
        )
        return False


# ─── Diagnostic / validation ──────────────────────────────────────────────────

def validate_q4_reporting_dates() -> List[Dict[str, Any]]:
    """
    Diagnostic: verify every non-"—" popup reporting date is valid Q4/full-year.

    Run in staging/production to confirm _annual_q4_report_dates_bulk() output.

    Each result row contains:
        ticker, source, annual_reported_on, fiscal_quarter_ending,
        fqe_month, fye_month, fiscal_q, fetched_at_utc,
        ok_fiscal_q          — fiscal_q == 4  (failure a)
        ok_latest_fetch      — selected date came from latest fetched_at_utc row
                               for that (ticker, fiscal_quarter_ending)  (failure b)
        ok_no_composite_borrow — composite tickers did not borrow base ticker's date (failure c)
        ok_has_fye           — FYE was available when a date was shown  (failure d)

    Returns all rows; rows with any ok_* == False are failures.
    """
    annual_map = _annual_q4_report_dates_bulk()
    if not annual_map:
        log_info("[validate_q4] annual_map is empty — no tickers to validate")
        return []

    # ── Build ground-truth lookup: latest fetched_at_utc per (ticker, fqe) ──
    # Used to verify that the selected date came from the latest-fetched row.
    latest_fetch_map: Dict[tuple, Any] = {}  # (ticker, fqe_str) → fetched_at_utc
    try:
        probe_rows = db_manager.execute_query_readonly(
            """
            SELECT ticker, fiscal_quarter_ending, earnings_date, fetched_at_utc
            FROM (
                SELECT
                    ticker, fiscal_quarter_ending, earnings_date, fetched_at_utc,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker, fiscal_quarter_ending
                        ORDER BY fetched_at_utc DESC, earnings_date DESC, id DESC
                    ) AS rn
                FROM coreiq_nasdaq_earnings_calendar
                WHERE fiscal_quarter_ending IS NOT NULL
                  AND earnings_date IS NOT NULL
                  AND ticker NOT LIKE '%.%'
            ) x
            WHERE rn = 1
            """,
            {},
        )
        for r in probe_rows:
            tk = (r.get("ticker") or "").strip()
            fqe = (r.get("fiscal_quarter_ending") or "").strip()
            fat = r.get("fetched_at_utc")
            if tk and fqe:
                latest_fetch_map[(tk, fqe)] = fat
    except Exception as exc:
        log_structured_error(exc, page="forecast_refresh_service",
                             component="validate_q4_reporting_dates", operation="SELECT_PROBE")

    fye_bulk = _fiscal_year_end_bulk()
    validation: List[Dict[str, Any]] = []

    for tk, info in annual_map.items():
        sel_date  = info["date"]
        fqe_str   = info.get("fiscal_quarter_ending", "")
        source    = info.get("source", "")
        fqe_m     = info.get("fqe_month", 0)
        fye_m     = info.get("fye_month", 0)
        fetched   = info.get("fetched_at_utc")

        # (a) fiscal_q must be 4
        fy_start = (fye_m % 12) + 1 if fye_m else 0
        months_into = (fqe_m - fy_start) % 12 + 1 if fqe_m and fye_m else 0
        fiscal_q = (months_into + 2) // 3 if months_into else 0
        ok_fiscal_q = fiscal_q == 4

        # (b) for NASDAQ tickers, selected date must equal the latest-fetched row's date
        ok_latest_fetch = True
        if source == "nasdaq" and fqe_str:
            latest_fat = latest_fetch_map.get((tk, fqe_str))
            if latest_fat is not None and fetched is not None:
                # Both should be the same fetched_at_utc value (same row)
                ok_latest_fetch = (fetched == latest_fat)

        # (c) composite tickers must not have borrowed a NASDAQ row
        ok_no_composite_borrow = True
        if "." in tk and source == "nasdaq":
            # A composite ticker in NASDAQ path is a violation
            ok_no_composite_borrow = False

        # (d) FYE must have been available when a date was shown
        fye_str = fye_bulk.get(tk) or fye_bulk.get(tk.split(".")[0])
        ok_has_fye = bool(fye_str) if sel_date else True

        row = {
            "ticker": tk,
            "source": source,
            "annual_reported_on": sel_date.strftime("%b %d, %Y"),
            "fiscal_quarter_ending": fqe_str,
            "fqe_month": fqe_m,
            "fye_month": fye_m,
            "fiscal_q": fiscal_q,
            "fetched_at_utc": str(fetched) if fetched else "",
            "ok_fiscal_q": ok_fiscal_q,
            "ok_latest_fetch": ok_latest_fetch,
            "ok_no_composite_borrow": ok_no_composite_borrow,
            "ok_has_fye": ok_has_fye,
        }
        validation.append(row)

    failures = [r for r in validation if not all(
        r[k] for k in ("ok_fiscal_q", "ok_latest_fetch", "ok_no_composite_borrow", "ok_has_fye")
    )]
    if failures:
        log_info(f"[validate_q4] {len(failures)} FAILURE(s) detected: {failures}")
    else:
        log_info(f"[validate_q4] All {len(validation)} tickers validated ✓ "
                 f"(nasdaq={sum(1 for r in validation if r['source']=='nasdaq')} "
                 f"yf={sum(1 for r in validation if r['source']=='yf')})")

    return validation
