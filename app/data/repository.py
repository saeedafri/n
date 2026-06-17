"""
Data repository for fetching market data from database.
"""
from typing import List, Optional, Tuple, Dict, Any
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import calendar
import json
import os
import time
import logging
import functools
import re
import unicodedata

import streamlit as st

from core.database import db_manager
from utils.server_logger import log_error, log_structured_error
from utils.constants import get_exchange_code


# =============================================================================
# CANONICAL TITLE NORMALIZATION — used for AV dedupe and any title comparison
# =============================================================================
def normalize_title_for_dedupe(title: Optional[str]) -> str:
    """Canonical title normalizer for deduplication.

    Ensures Python dedupe key matches SQL LOWER(TRIM(title)) semantics and
    also handles Unicode edge cases that raw .strip().lower() misses:
      - None/empty safety
      - NFKC Unicode normalization (collapses ligatures, ™→TM, etc.)
      - Replace non-breaking spaces (\\u00a0) with regular space
      - Remove zero-width chars (\\u200b, \\u200c, \\u200d, \\ufeff, etc.)
      - Collapse all internal whitespace runs to a single space
      - Strip leading/trailing whitespace
      - Lowercase

    This is the SINGLE source of truth for title-based grouping in AV dedupe.
    """
    if not title:
        return ''
    s = unicodedata.normalize('NFKC', title)
    # Replace non-breaking space with regular space
    s = s.replace('\u00a0', ' ')
    # Remove zero-width chars and BOM-like invisibles
    s = re.sub(r'[\u200b\u200c\u200d\ufeff\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069]', '', s)
    # Collapse all whitespace (tabs, newlines, multiple spaces) to single space
    s = re.sub(r'\s+', ' ', s)
    return s.strip().lower()

def _format_company_name(name: str) -> str:
    """Return company name exactly as stored in name_coresight column — no formatting."""
    return name or ''

_MONTH_NUM_TO_NAME = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

_MONTH_TEXT_TO_NUM = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _month_name_from_value(value: Any) -> Optional[str]:
    """Normalize a month-like value to a full month name."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        month_num = int(value)
        return _MONTH_NUM_TO_NAME.get(month_num)

    text = str(value).strip()
    if not text:
        return None

    lower = text.lower()
    if lower in _MONTH_TEXT_TO_NUM:
        return _MONTH_NUM_TO_NAME[_MONTH_TEXT_TO_NUM[lower]]

    try:
        month_num = int(float(text))
        if 1 <= month_num <= 12:
            return _MONTH_NUM_TO_NAME[month_num]
    except (TypeError, ValueError):
        pass

    iso_match = re.match(r"\d{4}-(\d{2})-\d{2}", text)
    if iso_match:
        return _MONTH_NUM_TO_NAME.get(int(iso_match.group(1)))

    us_match = re.match(r"(\d{1,2})[/-]\d{1,2}[/-]\d{4}", text)
    if us_match:
        return _MONTH_NUM_TO_NAME.get(int(us_match.group(1)))

    return None


def _month_name_from_epoch(value: Any) -> Optional[str]:
    """Convert a Unix epoch timestamp to its UTC month name."""
    if value is None:
        return None

    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None

    # YF fiscal dates are seconds, but handle millisecond payloads defensively.
    if epoch > 10_000_000_000:
        epoch = epoch / 1000

    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        return _MONTH_NUM_TO_NAME.get(dt.month)
    except (OSError, OverflowError, ValueError):
        return None


def _extract_yf_fiscal_year_end(payload_json: Any) -> Optional[str]:
    """Return a YFinance fiscal year end month name from payload_json.

    YF overview payloads usually store fiscal year end dates as Unix epoch
    seconds under info.lastFiscalYearEnd / info.nextFiscalYearEnd rather than a
    fiscalEndMonth field. Return the month name so existing fiscal-quarter logic
    can consume it exactly like coreiq_av_company_overview.fiscal_year_end.
    """
    if not payload_json:
        return None

    if isinstance(payload_json, str):
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            return None
    elif isinstance(payload_json, dict):
        payload = payload_json
    else:
        return None

    info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
    containers = [info]
    if info is not payload:
        containers.append(payload)

    month_keys = (
        "fiscalEndMonth", "fiscalendmonth", "fiscal_end_month",
        "fiscalYearEnd", "fiscal_year_end",
    )
    for container in containers:
        for key in month_keys:
            month_name = _month_name_from_value(container.get(key))
            if month_name:
                return month_name

    for container in containers:
        for key in ("lastFiscalYearEnd", "nextFiscalYearEnd"):
            month_name = _month_name_from_epoch(container.get(key))
            if month_name:
                return month_name

    return None
from data.models import (
    Company, IncomeStatementLineItem, FiscalPeriod, IncomeStatementData,
    NewsArticle, TickerSentiment, CompanyOverview, EarningsCall,
    BalanceSheetLineItem, BalanceSheetData,
    CashFlowLineItem, CashFlowData,
    FilingMetricResult
)

# Import server logger for error logging
try:
    from utils.server_logger import log_error, log_exception, log_structured_error
    SERVER_LOGGER_AVAILABLE = True
except ImportError:
    SERVER_LOGGER_AVAILABLE = False
    log_error = print
    def log_structured_error(exc, page="", component="", operation="", context=""):
        print(f"[ERROR] page={page} component={component} operation={operation}: {exc}")

logger = logging.getLogger(__name__)

def _log_query_time(func):
    """Decorator to log DB query execution time with detailed metrics.

    Uses server_logger.log_db_timing for consistent [TIMING] format.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        func_name = func.__qualname__

        # Extract ticker from args if present (first arg after self/static)
        ticker = args[0] if args and isinstance(args[0], str) else 'N/A'

        try:
            result = func(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000

            # Log result size if it's a list
            result_size = len(result) if isinstance(result, (list, tuple, dict)) else 1

            # Use server_logger for consistent timing format
            try:
                from utils.server_logger import log_db_timing
                log_db_timing(func_name, "repository", elapsed_ms, rows=result_size, ticker=str(ticker))
            except Exception:
                pass  # Never fail a query because of logging

            return result
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            log_error(f"[DB_ERROR] {func_name} ticker={ticker} failed after {elapsed_ms:.1f}ms: {e}")
            raise
    return wrapper

class CompanyRepository:
    """Repository for coreiq_companies table."""

    @staticmethod
    @_log_query_time
    def get_all_sources() -> List[str]:
        """Get distinct data sources."""
        try:
            companies_map = CompanyRepository.get_companies_map()
            sources = {c.get('source') for c in companies_map.values() if c.get('source')}
            return sorted(list(sources))
        except Exception as exc:
            log_structured_error(exc, page="repository", component="CompanyRepository", operation="get_all_sources")
            return []

    @staticmethod
    @_log_query_time
    def get_companies_by_source() -> List[Company]:
        """Get all companies for a data source."""
        try:
            companies_map = CompanyRepository.get_companies_map()
            results = [c for c in companies_map.values() if c.get('source') == 'SEC']
            results.sort(key=lambda x: x.get('name_coresight') or '')

            return [Company(
                ticker=row['ticker'],
                name=row['name'],
                name_coresight=row['name_coresight'],
                exchange=row['exchange'],
            ) for row in results]
        except Exception as exc:
            log_structured_error(exc, page="repository", component="CompanyRepository", operation="get_companies_by_source")
            return []

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_company_by_ticker(ticker: str) -> Optional[Company]:
        """Get single company by ticker (cached 10 min)."""
        companies_map = CompanyRepository.get_companies_map()
        row = companies_map.get(ticker)
        if not row:
            return None

        return Company(
            ticker=row['ticker'],
            name=row['name'],
            name_coresight=row['name_coresight'],
            exchange=row['exchange'],
        )

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_companies() -> List[Dict[str, str]]:
        """Get all companies from cached map.

        PERFORMANCE FIX: Removed complex subqueries checking multiple tables.
        Now simply returns all companies from the cached map.

        Returns both SEC and YFinance companies.
        For YFinance companies with exchange_acronym, only the composite ticker
        (e.g. 'ADS.DE') is shown — the base-ticker duplicate entry is skipped.
        """
        companies_map = CompanyRepository.get_companies_map()

        seen = set()
        companies = []
        for row in companies_map.values():
            ticker = row['ticker']
            # Skip base-ticker duplicate when a composite entry exists in the map.
            # Composite tickers contain '.'; base duplicates don't.
            exch = row.get('exchange_acronym')
            if exch and '.' not in ticker:
                # This is the base entry (e.g. 'ADS') — composite ('ADS.DE') will be emitted separately
                continue
            if ticker in seen:
                continue
            seen.add(ticker)
            # Dropdown label: ONLY `coreiq_companies.name_coresight` (never vendor `name`).
            display_name = (row.get('name_coresight') or '').strip() or ticker
            companies.append({'ticker': ticker, 'name': display_name})

        companies.sort(key=lambda x: x['name'].lower())
        return companies

    # ========================================================================
    # CACHED COMPANIES MAP - Load once, use everywhere
    # ========================================================================

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)  # 1 hour cache
    def get_companies_map() -> Dict[str, Dict[str, Any]]:
        """Get all companies as a dictionary keyed by ticker (cached 1 hour).

        Derived from get_companies_rows() so both caches share a single DB query.
        When get_companies_rows() is already cached (e.g. after screening page loads),
        this call is zero-cost (pure in-memory dict build, <1ms).

        Returns:
            Dict mapping ticker -> {
                'ticker': str,
                'name': str,
                'name_coresight': str,
                'exchange': str,
                'source': str,
                'primary_industry_coresight': str
            }
        """
        companies_map = {}
        for row in CompanyRepository.get_companies_rows():
            ticker = row.get('ticker')
            if not ticker:
                continue
            exchange_acronym = row.get('exchange_acronym') or None
            composite = f"{ticker}.{exchange_acronym}" if exchange_acronym else None
            base_entry = {
                'ticker': ticker,
                'name': row.get('name', ''),
                'name_coresight': row.get('name_coresight', ''),
                'exchange': row.get('exchange', ''),
                'source': row.get('source', ''),
                'exchange_acronym': exchange_acronym,
                'primary_industry_coresight': row.get('primary_industry_coresight', ''),
            }
            if composite:
                # Composite key (e.g. 'ADS.DE') gets its own entry with composite as ticker
                companies_map[composite] = {**base_entry, 'ticker': composite}
                # Base key ('ADS') only if not claimed by a non-composite company
                if ticker not in companies_map:
                    companies_map[ticker] = base_entry
            else:
                companies_map[ticker] = base_entry
        return companies_map

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)  # 1 hour cache
    def get_companies_rows() -> List[Dict[str, Any]]:
        """Get ALL company rows as a flat list — no deduplication by ticker.

        Unlike get_companies_map() which keys by ticker (losing duplicate tickers),
        this returns every row from coreiq_companies as a separate dict entry.
        Use this for universe-building (e.g. screening) where a ticker collision
        (two real companies sharing the same ticker on different exchanges) must
        not silently drop one company.

        The unique identity here is the combination of ticker + name_coresight.

        Returns:
            List of dicts, each with the same keys as get_companies_map() values:
            ticker, name, name_coresight, exchange, source, primary_industry_coresight
        """
        query = """
            SELECT ticker,
                   name,
                   name_coresight,
                   exchange,
                   source,
                   exchange_acronym,
                   primary_industry_coresight,
                   country_of_incorporation
            FROM coreiq_companies;
        """
        results = db_manager.execute_query_readonly(query)
        rows: List[Dict[str, Any]] = []
        for row in results:
            ticker = row['ticker']
            if not ticker:
                continue
            raw_exchange = row.get('exchange')
            normalized_exchange = get_exchange_code(raw_exchange) if raw_exchange else ''
            exchange_acronym = (row.get('exchange_acronym') or '').strip() or None
            rows.append({
                'ticker': ticker,
                'name': row['name'] or '',
                'name_coresight': row['name_coresight'] or '',
                'exchange': normalized_exchange,
                'source': row['source'] or '',
                'exchange_acronym': exchange_acronym,
                'primary_industry_coresight': row['primary_industry_coresight'] or '',
                'country_of_incorporation': row.get('country_of_incorporation') or '',
            })
        return rows

    @staticmethod
    def get_company_name(ticker: str) -> str:
        """Get company name by ticker (uses cached map).

        FAST - No DB query, uses in-memory cache.
        """
        companies_map = CompanyRepository.get_companies_map()
        company = companies_map.get(ticker)
        if company:
            return _format_company_name(company['name_coresight'])
        return ticker

    @staticmethod
    def get_company_sector(ticker: str) -> Optional[str]:
        """Get company sector by ticker (uses cached map).

        FAST - No DB query, uses in-memory cache.
        """
        companies_map = CompanyRepository.get_companies_map()
        company = companies_map.get(ticker)
        if company:
            return company['primary_industry_coresight'] or None
        return None

    @staticmethod
    def get_all_sectors() -> List[str]:
        """Get all unique sectors from companies (uses cached map).

        FAST - No DB query, uses in-memory cache.
        """
        companies_map = CompanyRepository.get_companies_map()
        sectors = set()
        for company in companies_map.values():
            sector = company.get('primary_industry_coresight')
            if sector and sector.strip():
                sectors.add(sector)
        return sorted(list(sectors))

# =============================================================================
# CACHED FISCAL YEAR END LOOKUP - Avoids repeated DB calls
# =============================================================================

@st.cache_data(ttl=1800, show_spinner=False)  # 30 min cache
def _get_fiscal_year_end_cached(ticker: str) -> Optional[str]:
    """Get fiscal_year_end for a ticker (cached 30 min) - ZERO LAG optimization.

    This function is called by all financial repositories to avoid N+1 queries.
    Uses covering index: idx_overview_ticker_fy_covering (ticker, fetched_at_utc, fiscal_year_end)

    Index Anatomy:
    - Column 1: ticker (for WHERE filtering)
    - Column 2: fetched_at_utc DESC (for ORDER BY)
    - Column 3: fiscal_year_end (for SELECT - no table lookup!)

    Result: "Using index" in EXPLAIN (fully covering)
    """
    import time as _time
    _start = _time.perf_counter()
    _cache_status = "UNKNOWN"
    _source = "UNKNOWN"
    _result = None

    try:
        from data.source_router import get_company_source
        from utils.server_logger import log_error

        _source = get_company_source(ticker)

        if _source == 'YFinance':
            query = """
                SELECT payload_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
            _ov_ticker = ticker.split('.')[0] if '.' in ticker else ticker
            results = db_manager.execute_query_readonly(query, {"ticker": _ov_ticker})
            if results:
                _result = _extract_yf_fiscal_year_end(results[0].get("payload_json"))
                _cache_status = "YF_DB_HIT" if _result else "YF_NO_FY"
            else:
                _cache_status = "YF_DB_MISS_NO_DATA"
            return _result

        # SEC company - fast COVERING INDEX lookup
        # Query is 100% satisfied by index (no table row access)
        _query_start = _time.perf_counter()
        query = """
            SELECT fiscal_year_end
            FROM coreiq_av_company_overview
            WHERE ticker = :ticker
            ORDER BY fetched_at_utc DESC
            LIMIT 1
        """
        results = db_manager.execute_query_readonly(query, {"ticker": ticker})
        _query_time = (_time.perf_counter() - _query_start) * 1000

        if results:
            _result = results[0].get('fiscal_year_end')
            _cache_status = "DB_HIT"
        else:
            _cache_status = "DB_MISS_NO_DATA"

    except Exception as e:
        _cache_status = f"ERROR: {str(e)[:50]}"
        log_error(f"[FQ_CACHE] ERROR ticker={ticker} error={e}")
        pass

    _total_time = (_time.perf_counter() - _start) * 1000

    return _result

class IncomeStatementRepository:
    """Repository for coreiq_av_financials_income_statement table."""

    # Mapping of UI labels to database columns
    LINE_ITEMS = [
        ("Revenue", "total_revenue", False),
        ("Other Revenue", None, True),  # Will calculate or show "-"
        ("Total Revenue", "total_revenue", False),
        ("Cost Of Goods Sold", "cost_of_revenue", False),
        ("Gross Profit", "gross_profit", False),
        ("Selling General & Admin Exp.", "selling_general_and_administrative", False),
        ("R&D Exp.", "research_and_development", False),
        ("Depreciation & Amort.", "depreciation_and_amortization", False),
        ("Other Operating Expense/(Income)", "other_non_operating_income", False),
        ("Other Operating Exp., Total", "operating_expenses", False),
        ("Operating Income", "operating_income", False),
        ("Interest Expense", "interest_expense", False),
        ("Interest and Invest. Income", "interest_income", False),
        ("Net Interest Exp.", "net_interest_income", False),
        ("Net Income", "net_income", False),
        ("EBITDA", "ebitda", False),
    ]

    # ── Cached single-query: fetches ALL rows for a ticker in one go ──
    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_all_annual_rows(ticker: str, period_type: str = "annual") -> List[Dict[str, Any]]:
        """Fetch all income statement rows for a ticker (cached 5 min).

        This single query replaces get_date_range, get_available_dates,
        get_reported_currency, AND get_income_statement_data — cutting
        4 network round-trips down to 1 (or 0 on cache hit).

        Supports both SEC and YFinance companies.

        Args:
            ticker: Company ticker symbol
            period_type: 'annual' or 'quarterly' (default: 'annual')
        """
        t0 = time.perf_counter()

        # Map period_type to database values
        # SEC uses: 'annual', 'quarterly' in report_type column
        # YF uses: 'annual', 'quarterly' in frequency column
        period_value = period_type.lower()

        # Detect company source
        from data.source_router import get_company_source
        source = get_company_source(ticker)
        # Composite tickers (e.g. 'ADS.DE') use yf_symbol; plain YF tickers use ticker column
        _yf_is_composite = '.' in ticker
        _yf_base = ticker.split('.')[0] if _yf_is_composite else ticker

        if source == 'YFinance':
            # YF companies: query YF table with pivot
            # Get currency from YF company overview first (overview table uses base ticker)
            currency_query = """
                SELECT payload_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
            currency_result = db_manager.execute_query_readonly(currency_query, {"ticker": _yf_base})
            yf_currency = 'USD'
            if currency_result and currency_result[0].get('payload_json'):
                try:
                    payload = json.loads(currency_result[0]['payload_json'])
                    # Support both nested {"info": {...}} and flat {"currency": ...} structures
                    info = payload.get('info', payload)
                    yf_currency = info.get('financialCurrency') or info.get('currency') or 'USD'
                except:
                    yf_currency = 'USD'

            _yf_col = "yf_symbol" if _yf_is_composite else "ticker"
            query = f"""
                SELECT
                    period_end as fiscal_date_ending,
                    MAX(CASE WHEN line_item = 'Total Revenue' THEN value END) as total_revenue,
                    MAX(CASE WHEN line_item = 'Cost Of Revenue' THEN value END) as cost_of_revenue,
                    MAX(CASE WHEN line_item = 'Gross Profit' THEN value END) as gross_profit,
                    MAX(CASE WHEN line_item = 'Selling General And Administration' THEN value END) as selling_general_and_administrative,
                    MAX(CASE WHEN line_item = 'Research Development' THEN value END) as research_and_development,
                    MAX(CASE WHEN line_item = 'Reconciled Depreciation' THEN value END) as depreciation_and_amortization,
                    MAX(CASE WHEN line_item = 'Operating Income' THEN value END) as operating_income,
                    MAX(CASE WHEN line_item = 'Interest Expense' THEN value END) as interest_expense,
                    MAX(CASE WHEN line_item = 'Interest Income' THEN value END) as interest_income,
                    MAX(CASE WHEN line_item = 'Net Income' THEN value END) as net_income,
                    MAX(CASE WHEN line_item = 'Operating Expense' THEN value END) as operating_expenses,
                    MAX(CASE WHEN line_item = 'Total Other Income Expense Net' THEN value END) as other_non_operating_income,
                    MAX(CASE WHEN line_item = 'Net Interest Income' THEN value END) as net_interest_income,
                    MAX(CASE WHEN line_item = 'EBITDA' THEN value END) as ebitda,
                    MAX(CASE WHEN line_item = 'Basic EPS' THEN value END) as basic_eps,
                    MAX(CASE WHEN line_item = 'Diluted EPS' THEN value END) as diluted_eps,
                    '{yf_currency}' as reported_currency
                FROM coreiq_yf_financials_income_statement
                WHERE {_yf_col} = :ticker
                  AND frequency = '{period_value}'
                GROUP BY period_end
                ORDER BY period_end ASC
            """
        else:
            # SEC companies (default): query SEC table
            query = f"""
                SELECT DISTINCT fiscal_date_ending, total_revenue, cost_of_revenue,
                       gross_profit, selling_general_and_administrative,
                       research_and_development, depreciation_and_amortization,
                       operating_income, interest_expense, interest_income,
                       net_income, reported_currency, operating_expenses,
                       other_non_operating_income, net_interest_income,
                       ebitda, ebit
                FROM coreiq_av_financials_income_statement
                WHERE ticker = :ticker
                  AND report_type = '{period_value}'
                ORDER BY fiscal_date_ending ASC
            """

        _q_ticker = ticker if source == 'YFinance' else ticker
        results = db_manager.execute_query_readonly(query, {"ticker": _q_ticker})
        elapsed = (time.perf_counter() - t0) * 1000
        return results

    @staticmethod
    @_log_query_time
    def get_date_range(ticker: str, period_type: str = "annual") -> Tuple[Optional[date], Optional[date]]:
        """Get min and max fiscal dates for a ticker (from cache)."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        if not rows:
            return None, None
        return rows[0]['fiscal_date_ending'], rows[-1]['fiscal_date_ending']

    @staticmethod
    @_log_query_time
    def get_available_dates(ticker: str, period_type: str = "annual") -> List[date]:
        """Get all available fiscal dates for dropdown (from cache)."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        return [row['fiscal_date_ending'] for row in rows]

    @staticmethod
    @_log_query_time
    def get_income_statement_data(
        ticker: str,
        start_date: date,
        end_date: date,
        period_type: str = "annual"
    ) -> IncomeStatementData:
        """Get income statement data for date range (from cache)."""
        import time as _time
        _start = _time.perf_counter()

        # Fetch company info (cached separately)
        _t0 = _time.perf_counter()
        company = CompanyRepository.get_company_by_ticker(ticker)
        _company_time = (_time.perf_counter() - _t0) * 1000
        if not company:
            raise ValueError(f"Company not found: {ticker}")

        # Filter cached rows by date range — NO extra DB call
        _t0 = _time.perf_counter()
        all_rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        results = [
            row for row in all_rows
            if start_date <= row['fiscal_date_ending'] <= end_date
        ]
        _fetch_time = (_time.perf_counter() - _t0) * 1000

        if not results:
            return IncomeStatementData(
                company=company,
                periods=[],
                line_items=[]
            )

        # Fetch fiscal_year_end for SEC companies (CACHED - ZERO LAG)
        _t0 = _time.perf_counter()
        fiscal_year_end = _get_fiscal_year_end_cached(ticker)
        _fy_time = (_time.perf_counter() - _t0) * 1000

        # Create periods from results (with FQ for SEC companies)
        _t0 = _time.perf_counter()
        periods = [
            FiscalPeriod.from_date(row['fiscal_date_ending'], period_type, fiscal_year_end)
            for row in results
        ]
        _period_time = (_time.perf_counter() - _t0) * 1000
        # Log first period as sample
        # Build line items
        _t0 = _time.perf_counter()
        line_items = []
        for label, column, is_calc in IncomeStatementRepository.LINE_ITEMS:
            values = []
            for row in results:
                if column and row.get(column) is not None:
                    # Convert to millions
                    values.append(float(row[column]) / 1_000_000)
                else:
                    values.append(None)

            line_items.append(IncomeStatementLineItem(
                label=label,
                key=column or label,
                values=values,
                is_calculated=is_calc
            ))
        _line_item_time = (_time.perf_counter() - _t0) * 1000

        _total_time = (_time.perf_counter() - _start) * 1000

        from utils.server_logger import log_warning as _slw
        _slw("[TIMING] IS_get_data | %.1fms | ticker=%s company=%.1f fetch=%.1f fy=%.1f periods=%.1f line_items=%.1f rows=%d" % (
            _total_time, ticker, _company_time, _fetch_time, _fy_time, _period_time, _line_item_time, len(results)))

        return IncomeStatementData(
            company=company,
            periods=periods,
            line_items=line_items
        )

    @staticmethod
    @_log_query_time
    def get_reported_currency(ticker: str, fiscal_date: date, period_type: str = "annual") -> str:
        """Get the reported currency for a specific fiscal period (from cache)."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)

        def _is_valid_currency(curr):
            """Check if currency is valid (not None, not 'None', not empty)."""
            if not curr:
                return False
            if isinstance(curr, str):
                return curr.strip().upper() not in ('', 'NONE', 'NULL')
            return True

        # Find the row closest to the requested date
        for row in reversed(rows):
            if row['fiscal_date_ending'] <= fiscal_date and _is_valid_currency(row.get('reported_currency')):
                return row['reported_currency']
        # Fallback: use first row with currency or USD
        for row in rows:
            if _is_valid_currency(row.get('reported_currency')):
                return row['reported_currency']
        return "USD"

class NewsRepository:
    """Repository for coreiq_av_market_news_sentiment table.

    PERFORMANCE OPTIMIZATION (March 8, 2026):
    - All reference data methods use @st.cache_data for 1-hour TTL
    - News articles cached for 5 minutes (frequent updates)
    - Cache can be cleared manually for emergency updates
    - Database indexes: idx_ticker_time, idx_query_ticker_time, idx_time
    """

    @staticmethod
    def _parse_ticker_sentiment(ticker_sentiment_json: str) -> List[TickerSentiment]:
        """Parse ticker_sentiment JSON string into list of TickerSentiment objects."""
        if not ticker_sentiment_json:
            return []
        try:
            data = json.loads(ticker_sentiment_json)
            return [
                TickerSentiment(
                    ticker=ts.get('ticker', ''),
                    relevance_score=ts.get('relevance_score', '0'),
                    ticker_sentiment_label=ts.get('ticker_sentiment_label', 'Neutral'),
                    ticker_sentiment_score=ts.get('ticker_sentiment_score', '0')
                )
                for ts in data
            ]
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _dedupe_and_merge_articles(
        articles: List['NewsArticle'],
        limit: int,
    ) -> Tuple[List['NewsArticle'], int, int, int]:
        """Deduplicate articles by TITLE only and merge tagged companies.

        Business Rule:
        - Group key = normalize_title_for_dedupe(title) — NEVER URL
        - First article per group becomes the representative
        - ticker_sentiment from duplicate rows is merged into the representative:
          * deduplicate by ticker
          * keep highest relevance_score per ticker
          * stable sort: relevance desc, ticker asc
        - Returns exactly min(limit, unique_count) articles in original sort order
        - This function operates on a SINGLE source (AV only or YF only)
        - NEVER compares across sources

        Returns:
            (unique_articles, raw_count, duplicates_collapsed, merged_tickers_count)
        """
        if not articles:
            return [], 0, 0, 0

        raw_count = len(articles)
        seen: Dict[str, int] = {}           # normalised title -> index in unique list
        unique: List['NewsArticle'] = []
        merged_tickers_total = 0
        duplicates_collapsed = 0
        normalized_title_changed_count = 0

        for article in articles:
            group_key = normalize_title_for_dedupe(article.title)
            # Track titles where canonical normalization differs from simple strip().lower()
            simple_key = (article.title or '').strip().lower()
            if group_key != simple_key:
                normalized_title_changed_count += 1
            if not group_key:
                # Articles with empty title are kept as-is
                unique.append(article)
                continue

            if group_key in seen:
                # Merge tagged companies from this duplicate into the representative
                duplicates_collapsed += 1
                rep = unique[seen[group_key]]
                existing_tickers = {ts.ticker for ts in rep.ticker_sentiment}
                for ts in article.ticker_sentiment:
                    if ts.ticker not in existing_tickers:
                        rep.ticker_sentiment.append(ts)
                        existing_tickers.add(ts.ticker)
                        merged_tickers_total += 1
                    else:
                        # Same ticker — keep highest relevance score
                        for i, existing_ts in enumerate(rep.ticker_sentiment):
                            if existing_ts.ticker == ts.ticker:
                                try:
                                    if float(ts.relevance_score) > float(existing_ts.relevance_score):
                                        rep.ticker_sentiment[i] = ts
                                except (ValueError, TypeError):
                                    pass
                                break
            else:
                seen[group_key] = len(unique)
                unique.append(article)

        # Stable sort each article's tagged companies: relevance desc, ticker asc
        for article in unique:
            article.ticker_sentiment.sort(
                key=lambda ts: (-float(ts.relevance_score or '0'), ts.ticker)
            )

        # Trim to requested limit (overflow-fetch may have returned extra)
        result = unique[:limit]

        # Diagnostic logging for dedupe transparency
        _dedupe_logger = logging.getLogger(__name__)
        _dedupe_logger.info(
            f"[NEWSROOM_TIMING] dedupe_summary | "
            f"raw_count={raw_count} unique_count={len(result)} "
            f"duplicates_collapsed={duplicates_collapsed} "
            f"normalized_title_changed={normalized_title_changed_count} "
            f"merged_tickers={merged_tickers_total}"
        )

        return result, raw_count, duplicates_collapsed, merged_tickers_total

    @staticmethod
    def _parse_topics(topics_json: str) -> List[Dict[str, str]]:
        """Parse topics JSON string into list of topic dictionaries."""
        if not topics_json:
            return []
        try:
            return json.loads(topics_json)
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    @st.cache_data(ttl=43200, show_spinner=False)
    @_log_query_time
    def get_articles(
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        sector: Optional[str] = None,
        company_ticker: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        sort_ascending: bool = False
    ) -> List[NewsArticle]:
        """
        Get news articles with optional filtering and deduplication.

        DEDUPLICATION STRATEGY (March 2026):

        The DB has ~1.43x duplication ratio: the same article from Alpha Vantage
        is stored once per tagged ticker (e.g. an article tagged with 5 tickers
        produces 5 rows with the same url/title but different ticker column).
        ticker_sentiment_json is identical 99.84% of the time; for the 0.16%
        edge case, tagged companies are merged.

        Overflow-fetch approach:
          1. SQL fetches limit × 2 raw rows (same indexed query, same EXPLAIN plan)
          2. Python _dedupe_and_merge_articles() collapses duplicates in O(N)
          3. Returns exactly min(limit, unique_count) deduplicated articles
          4. Merges ticker_sentiment from all duplicate rows (union by ticker,
             highest relevance wins per ticker)
          5. No GROUP BY in SQL — avoids 10-20× penalty with FULLTEXT

        PATH A — Keyword search (FULLTEXT or LIKE on title, date-bounded).
        PATH B — No keyword (direct date/sector/ticker filter).
        Both paths select only the 8 columns needed for rendering.

        Args:
            date_from: Start date filter
            date_to: End date filter
            sector: Filter by company sector (primary_industry_coresight)
            company_ticker: Filter by specific company ticker
            keyword: Keyword to search in title (full date range, no clamp)
            limit: Maximum number of unique articles to return per page
            offset: Pagination offset (raw row position)
        """
        import logging
        import os
        from utils.server_logger import log_timing, log_db_timing, log_info, log_warning
        _repo_logger = logging.getLogger(__name__)
        _RCA_ENABLED = os.environ.get('NEWSROOM_RCA_LOGGING', '0') == '1'
        _t_start = time.perf_counter()

        # Generate unique search ID for correlating logs
        _search_id = f"AV_{int(_t_start * 1000000)}"

        date_sort = "ASC" if sort_ascending else "DESC"
        has_keyword = keyword and keyword.strip()
        clean_keyword = keyword.strip() if has_keyword else None

        # ── FIX #2: Min-char guard ──────────────────────────────────────────
        # FULLTEXT innodb_ft_min_token_size defaults to 3 on MySQL 8.
        # Keywords shorter than 3 chars match zero rows — skip the round-trip.
        if clean_keyword and len(clean_keyword) < 3:
            return []

        # Calculate days for logging
        _days = (date_to - date_from).days if date_from and date_to else 0

        _OVERFLOW_MULTIPLIER = 2
        fetch_limit = limit * _OVERFLOW_MULTIPLIER

        # ── Shared WHERE conditions (date / ticker) ──────────────────────────
        inner_where = "WHERE 1=1"
        params: dict = {}

        if date_from:
            inner_where += " AND n.time_published_utc >= :date_from"
            params['date_from'] = date_from
        if date_to:
            inner_where += " AND n.time_published_utc < :date_to_excl"
            params['date_to_excl'] = date_to + timedelta(days=1)
        if company_ticker:
            inner_where += " AND n.ticker = :company_ticker"
            params['company_ticker'] = company_ticker

        # Sector filter using coreiq_av_companies_all.sector
        # 'Unknown Sector' = YF-only bucket → return no AV articles
        # 'None' = AV articles whose ticker has no/null sector in AV table
        # anything else = INNER JOIN on matching sector (UPPER for case safety)
        sector_join = ""
        if sector == "Unknown Sector":
            inner_where += " AND 1=0"
        elif sector == "None":
            inner_where += (
                " AND NOT EXISTS ("
                "SELECT 1 FROM coreiq_av_companies_all av"
                " WHERE av.symbol = n.ticker"
                " AND av.sector IS NOT NULL AND av.sector != ''"
                " AND UPPER(av.sector) != 'NONE'"
                ")"
            )
        elif sector:
            sector_join = (
                "INNER JOIN ("
                "  SELECT DISTINCT symbol FROM coreiq_av_companies_all"
                "  WHERE UPPER(sector) = UPPER(:sector)"
                ") av ON av.symbol = n.ticker"
            )
            params['sector'] = sector

        if has_keyword:
            # ── Feature flag: USE_AV_FULLTEXT ──────────────────────────────
            # Default=1: MATCH(title) AGAINST(... IN BOOLEAN MODE) via ft_av_title
            # Set to 0 to fall back to LIKE '%word%' baseline path
            # Accepted 2026-03-20 after AV RCA proved FULLTEXT 93x faster at DB level.
            _use_av_ft = os.environ.get('USE_AV_FULLTEXT', '1') == '1'

            words = [w for w in clean_keyword.split() if w]

            if _use_av_ft:
                # FULLTEXT path (Experiment): MATCH AGAINST in BOOLEAN MODE.
                # Requires ft_av_title FULLTEXT index on title column.
                # Each word prefixed with '+' for AND semantics.
                ft_expr = ' '.join(f'+{w}' for w in words)
                inner_where += " AND MATCH(n.title) AGAINST(:av_ft_kw IN BOOLEAN MODE)"
                params['av_ft_kw'] = ft_expr
            else:
                # LIKE path (baseline): original row-by-row title scanning.
                # MAX_EXECUTION_TIME(60000) for cold buffer pool on Azure Flex.
                for i, word in enumerate(words):
                    key = f'kw_{i}'
                    inner_where += f" AND n.title LIKE :{key}"
                    params[key] = f'%{word}%'

            _hint = "" if _use_av_ft else "/*+ MAX_EXECUTION_TIME(60000) */ "
            query = f"""
                SELECT {_hint}n.id, n.title, n.summary, n.url,
                       n.source_name AS source, n.source_domain,
                       n.time_published_utc AS time_published,
                       n.ticker_sentiment_json,
                       n.topics_json
                FROM coreiq_av_market_news_sentiment n
                {sector_join}
                {inner_where}
                ORDER BY n.time_published_utc {date_sort}
                LIMIT :fetch_limit OFFSET :offset
            """
            params['fetch_limit'] = fetch_limit
            params['offset'] = offset

            _t = time.perf_counter()
            _db_start = _t
            try:
                results = db_manager.execute_query_readonly(query, params)
                _ms = (time.perf_counter() - _t) * 1000
                _db_time = (time.perf_counter() - _db_start) * 1000
                log_db_timing("SELECT_KEYWORD", "coreiq_av_market_news_sentiment", _db_time, len(results))

            except Exception as exc:
                _ms = (time.perf_counter() - _t) * 1000
                _db_time = (time.perf_counter() - _db_start) * 1000
                if has_keyword:
                    log_warning(f"[KEYWORD_SEARCH] AV_ERROR | search_id={_search_id} | "
                               f"db_time={_db_time:.1f}ms | error={type(exc).__name__}")
                results = []

        else:
            # ── PATH B: No keyword — deferred row lookup ──────────────────────
            # Inner subquery: covering-index scan on idx_av_pub_id_covering
            #   (time_published_utc, id) → returns 2000 IDs with zero off-page reads.
            # Outer query: PK-based point lookups for wide columns (summary, JSON).
            #   PK lookups hit buffer pool sequentially → ~3-5× faster than a
            #   straight range scan that fetches off-page LONGTEXT/JSON per row.
            # Business logic: identical rows, identical columns — no data change.
            #
            # PERF FIX (2026-04-02): When date range >= 3 days and no sector/ticker
            # filter, split into 2 parallel half-range queries. Each half fetches
            # ~half the rows concurrently → wall clock ≈ max(half1, half2) ≈ 1.75s
            # instead of sequential 3.5s.

            _can_parallel = (
                date_from and date_to
                and (date_to - date_from).days >= 3
                and not company_ticker
                and not sector
            )

            def _run_av_half(p_from, p_to_excl, half_limit, half_offset):
                """Run one half-range AV deferred-join query."""
                _hw = "WHERE 1=1 AND n.time_published_utc >= :date_from AND n.time_published_utc < :date_to_excl"
                _hp = {
                    'date_from': p_from,
                    'date_to_excl': p_to_excl,
                    'fetch_limit': half_limit,
                    'offset': half_offset,
                }
                _hq = f"""
                    SELECT outer_n.id, outer_n.title, outer_n.summary, outer_n.url,
                           outer_n.source_name AS source, outer_n.source_domain,
                           outer_n.time_published_utc AS time_published,
                           outer_n.ticker_sentiment_json,
                           outer_n.topics_json
                    FROM coreiq_av_market_news_sentiment outer_n
                    INNER JOIN (
                        SELECT n.id
                        FROM coreiq_av_market_news_sentiment n USE INDEX (idx_av_time_id)
                        {_hw}
                        ORDER BY n.time_published_utc {date_sort}
                        LIMIT :fetch_limit OFFSET :offset
                    ) id_page ON outer_n.id = id_page.id
                    ORDER BY outer_n.time_published_utc {date_sort}
                """
                return db_manager.execute_query_readonly(_hq, _hp)

            _t = time.perf_counter()

            if _can_parallel:
                # Split date range at midpoint, fire both halves concurrently
                from concurrent.futures import ThreadPoolExecutor as _AVPool
                _mid = date_from + (date_to - date_from) / 2
                _mid_excl = _mid + timedelta(days=1)  # date_to is inclusive
                _date_to_excl = date_to + timedelta(days=1)

                with _AVPool(max_workers=2) as _avp:
                    _f1 = _avp.submit(_run_av_half, date_from, _mid_excl, fetch_limit, offset)
                    _f2 = _avp.submit(_run_av_half, _mid_excl, _date_to_excl, fetch_limit, offset)
                    _r1 = _f1.result(timeout=30)
                    _r2 = _f2.result(timeout=30)

                results = _r1 + _r2
            else:
                # Original single-query path (keyword, sector, ticker, or short range)
                query = f"""
                    SELECT outer_n.id, outer_n.title, outer_n.summary, outer_n.url,
                           outer_n.source_name AS source, outer_n.source_domain,
                           outer_n.time_published_utc AS time_published,
                           outer_n.ticker_sentiment_json,
                           outer_n.topics_json
                    FROM coreiq_av_market_news_sentiment outer_n
                    INNER JOIN (
                        SELECT n.id
                        FROM coreiq_av_market_news_sentiment n USE INDEX (idx_av_time_id)
                        {sector_join}
                        {inner_where}
                        ORDER BY n.time_published_utc {date_sort}
                        LIMIT :fetch_limit OFFSET :offset
                    ) id_page ON outer_n.id = id_page.id
                    ORDER BY outer_n.time_published_utc {date_sort}
                """
                params['fetch_limit'] = fetch_limit
                params['offset'] = offset
                results = db_manager.execute_query_readonly(query, params)

            _ms = (time.perf_counter() - _t) * 1000
            log_db_timing("SELECT_NOKEY", "coreiq_av_market_news_sentiment", _ms, len(results))

        # Build articles list with timing
        _build_start = time.perf_counter()
        articles = []
        for row in results:
            article = NewsArticle(
                id=row['id'],
                title=row['title'] or '',
                summary=row['summary'] or '',
                url=row['url'] or '',
                source=row['source'] or '',
                source_domain=row['source_domain'] or '',
                time_published=row['time_published'],
                time_published_raw='',
                overall_sentiment_score=0.0,
                overall_sentiment_label='Neutral',
                banner_image=None,
                ticker_sentiment=NewsRepository._parse_ticker_sentiment(
                    row['ticker_sentiment_json'] or '[]'
                ),
                topics=NewsRepository._parse_topics(row.get('topics_json') or ''),
                category_within_source=''
            )
            articles.append(article)
        _build_time = (time.perf_counter() - _build_start) * 1000
        log_timing("AV_ARTICLES_BUILD", _build_time, f"rows={len(articles)}")

        # ── Deduplicate + merge tagged companies (repository layer) ───────
        # Overflow-fetch strategy: same SQL, 2x rows, O(N) Python dedupe.
        # Eliminates duplicate article cards while preserving all tagged companies.
        _merge_start = time.perf_counter()
        articles, _raw_count, _dupes_collapsed, _merged_tickers = (
            NewsRepository._dedupe_and_merge_articles(articles, limit)
        )
        _merge_time = (time.perf_counter() - _merge_start) * 1000
        log_timing("AV_ARTICLES_DEDUPE", _merge_time, f"raw={_raw_count} unique={len(articles)} dupes={_dupes_collapsed}")

        _total_ms = (time.perf_counter() - _t_start) * 1000
        log_timing("AV_ARTICLES_TOTAL", _total_ms, f"rows={len(articles)} has_kw={bool(has_keyword)}")

        # =============================================================================
        # DEDUPE + PERFORMANCE LOGGING (always, not just keyword)
        # =============================================================================
        _keyword_mode = 'N/A'
        if has_keyword:
            _keyword_mode = 'FULLTEXT' if ('_use_av_ft' in dir() and _use_av_ft) else 'LIKE'
            # Alert on slow searches (>2s total or >1s DB time)
            if _total_ms > 2000:
                log_warning(f"[KEYWORD_SEARCH_SLOW] AV_SLOW_TOTAL | search_id={_search_id} | "
                           f"keyword='{clean_keyword}' | total={_total_ms:.1f}ms | "
                           f"db={_ms:.1f}ms | build={_build_time:.1f}ms | mode={_keyword_mode}")
            if _ms > 1000:
                log_warning(f"[KEYWORD_SEARCH_SLOW] AV_SLOW_DB | search_id={_search_id} | "
                           f"keyword='{clean_keyword}' | db_time={_ms:.1f}ms | mode={_keyword_mode}")

        if _RCA_ENABLED:
            _db_ms_str = f"{_ms:.1f}" if '_ms' in locals() else "N/A"
            _repo_logger.info(
                f"[NEWSROOM_QUERY] AV_END "
                f"strategy=overflow_fetch_dedupe "
                f"has_keyword={has_keyword} "
                f"av_keyword_mode={_keyword_mode} "
                f"raw_rows_fetched={_raw_count} "
                f"unique_articles={len(articles)} "
                f"duplicates_collapsed={_dupes_collapsed} "
                f"merged_tickers={_merged_tickers} "
                f"fetch_limit={fetch_limit} requested_limit={limit} offset={offset} "
                f"filters={{sector={sector or 'NONE'}, ticker={company_ticker or 'NONE'}, "
                f"keyword={clean_keyword or 'NONE'}, sort={date_sort}}} "
                f"path={'KEYWORD' if has_keyword else 'NO_KEYWORD'} "
                f"total_ms={_total_ms:.1f} db_ms={_db_ms_str} "
                f"build_ms={_build_time:.1f} merge_ms={_merge_time:.1f}"
            )

        return articles

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_articles_metadata_only(
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        sector: Optional[str] = None,
        company_ticker: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        sort_ascending: bool = False
    ) -> List[NewsArticle]:
        """
        Fast AV fetch — omits TEXT/JSON columns (summary, ticker_sentiment_json).

        PERFORMANCE: InnoDB stores TEXT and JSON off-page. Cold reads for these
        columns cost ~2ms per row. Omitting them avoids that overhead entirely:
          summary (TEXT)              → ~2ms/row cold → eliminated
          ticker_sentiment_json (JSON) → ~2ms/row cold → eliminated
        Result: ~3-5× faster cold read (VARCHAR row lookups ~0.2ms vs ~2ms).

        Returns NewsArticle objects with summary='' and ticker_sentiment=[].
        Intended for fast initial display; use get_articles() for full content.
        Same overflow-fetch + dedupe strategy as get_articles().
        """
        import os
        date_sort = "ASC" if sort_ascending else "DESC"
        has_keyword = keyword and keyword.strip()
        clean_keyword = keyword.strip() if has_keyword else None

        if clean_keyword and len(clean_keyword) < 3:
            return []

        _OVERFLOW_MULTIPLIER = 2
        fetch_limit = limit * _OVERFLOW_MULTIPLIER

        inner_where = "WHERE 1=1"
        params: dict = {}

        if date_from:
            inner_where += " AND n.time_published_utc >= :date_from"
            params['date_from'] = date_from
        if date_to:
            inner_where += " AND n.time_published_utc < :date_to_excl"
            params['date_to_excl'] = date_to + timedelta(days=1)
        if company_ticker:
            inner_where += " AND n.ticker = :company_ticker"
            params['company_ticker'] = company_ticker

        sector_join = ""
        if sector == "Unknown Sector":
            inner_where += " AND 1=0"
        elif sector == "None":
            inner_where += (
                " AND NOT EXISTS ("
                "SELECT 1 FROM coreiq_av_companies_all av"
                " WHERE av.symbol = n.ticker"
                " AND av.sector IS NOT NULL AND av.sector != ''"
                " AND UPPER(av.sector) != 'NONE'"
                ")"
            )
        elif sector:
            sector_join = (
                "INNER JOIN ("
                "  SELECT DISTINCT symbol FROM coreiq_av_companies_all"
                "  WHERE UPPER(sector) = UPPER(:sector)"
                ") av ON av.symbol = n.ticker"
            )
            params['sector'] = sector

        if has_keyword:
            _use_av_ft = os.environ.get('USE_AV_FULLTEXT', '1') == '1'
            words = [w for w in clean_keyword.split() if w]
            if _use_av_ft:
                ft_expr = ' '.join(f'+{w}' for w in words)
                inner_where += " AND MATCH(n.title) AGAINST(:av_ft_kw IN BOOLEAN MODE)"
                params['av_ft_kw'] = ft_expr
            else:
                for i, word in enumerate(words):
                    key = f'kw_{i}'
                    inner_where += f" AND n.title LIKE :{key}"
                    params[key] = f'%{word}%'

        # SELECT excludes summary (TEXT) and ticker_sentiment_json (JSON) to avoid
        # off-page InnoDB row lookups — the dominant cold-read bottleneck.
        query = f"""
            SELECT n.id, n.title, n.url,
                   n.source_name AS source, n.source_domain,
                   n.time_published_utc AS time_published
            FROM coreiq_av_market_news_sentiment n
            {sector_join}
            {inner_where}
            ORDER BY n.time_published_utc {date_sort}
            LIMIT :fetch_limit OFFSET :offset
        """
        params['fetch_limit'] = fetch_limit
        params['offset'] = offset

        results = db_manager.execute_query_readonly(query, params)

        articles = []
        for row in results:
            article = NewsArticle(
                id=row['id'],
                title=row['title'] or '',
                summary='',                          # omitted — fetched in get_articles()
                url=row['url'] or '',
                source=row['source'] or '',
                source_domain=row['source_domain'] or '',
                time_published=row['time_published'],
                time_published_raw='',
                overall_sentiment_score=0.0,
                overall_sentiment_label='Neutral',
                banner_image=None,
                ticker_sentiment=[],                 # omitted — fetched in get_articles()
                topics=[],
                category_within_source=''
            )
            articles.append(article)

        articles, _, _, _ = NewsRepository._dedupe_and_merge_articles(articles, limit)
        return articles

    @staticmethod
    @st.cache_data(ttl=1800, show_spinner=False)  # 30 min cache for date range
    @_log_query_time
    def get_news_date_range() -> Dict[str, Any]:
        """Get unified min/max date across both AV and YF news tables (cached 30 min).

        PERF FIX (March 2026): 4 queries fire in parallel via ThreadPoolExecutor.
        Each resolves via an index extremum lookup (O(1)) — no full scans.
        - AV: idx_av_time_id (time_published_utc DESC, id) → O(1) MAX/MIN
        - YF: idx_yf_pub_newsid (published_at DESC) → O(1) MAX
              idx_yf_published_at (published_at ASC)  → O(1) MIN
        Final min/max chosen Python-side by comparing both tables.
        """
        from utils.server_logger import log_timing, log_db_timing
        _t = time.perf_counter()

        av_min_query = "SELECT MIN(time_published_utc) AS ts FROM coreiq_av_market_news_sentiment"
        av_max_query = "SELECT MAX(time_published_utc) AS ts FROM coreiq_av_market_news_sentiment"
        yf_min_query = "SELECT MIN(published_at) AS ts FROM coreiq_yf_market_news_sentiment"
        yf_max_query = "SELECT MAX(published_at) AS ts FROM coreiq_yf_market_news_sentiment"

        # All 4 queries fire simultaneously — AV and YF are independent tables
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            av_min_fut = pool.submit(db_manager.execute_query_readonly, av_min_query)
            av_max_fut = pool.submit(db_manager.execute_query_readonly, av_max_query)
            yf_min_fut = pool.submit(db_manager.execute_query_readonly, yf_min_query)
            yf_max_fut = pool.submit(db_manager.execute_query_readonly, yf_max_query)
            av_min_r = av_min_fut.result()
            av_max_r = av_max_fut.result()
            yf_min_r = yf_min_fut.result()
            yf_max_r = yf_max_fut.result()

        _ms = (time.perf_counter() - _t) * 1000

        def _to_dt(rows):
            v = rows[0]['ts'] if rows and rows[0]['ts'] else None
            return v

        av_min_ts = _to_dt(av_min_r)
        av_max_ts = _to_dt(av_max_r)
        yf_min_ts = _to_dt(yf_min_r)
        yf_max_ts = _to_dt(yf_max_r)

        # Pick the overall earliest min and latest max across both tables
        candidates_min = [ts for ts in (av_min_ts, yf_min_ts) if ts is not None]
        candidates_max = [ts for ts in (av_max_ts, yf_max_ts) if ts is not None]

        if candidates_min and candidates_max:
            min_ts = min(candidates_min)
            max_ts = max(candidates_max)
            return {
                'min_date': min_ts.date() if hasattr(min_ts, 'date') else min_ts,
                'max_date': max_ts.date() if hasattr(max_ts, 'date') else max_ts,
            }
        return {'min_date': date.today() - timedelta(days=30), 'max_date': date.today()}

    @staticmethod
    @_log_query_time
    def _get_news_tickers_impl(
        date_from: Optional[date] = None,
        date_to: Optional[date] = None
    ) -> List[str]:
        """Internal implementation: Get distinct tickers from news articles within a date range.

        CHANGED (March 8, 2026):
        - Uses 'ticker' column directly from news table (not JSON_TABLE)
        - 'ticker' column represents the primary ticker the article was fetched for
        - This is sufficient for dropdown filtering
        - Indexed column: idx_ticker_time on (ticker, time_published_utc)
        """
        query = """
            SELECT DISTINCT ticker
            FROM coreiq_av_market_news_sentiment
            WHERE 1=1
        """
        params = {}
        if date_from:
            query += " AND time_published_utc >= :date_from"
            params['date_from'] = date_from
        if date_to:
            query += " AND time_published_utc < :date_to_excl"
            params['date_to_excl'] = date_to + timedelta(days=1)

        query += " ORDER BY ticker"

        results = db_manager.execute_query_readonly(query, params)
        return [row['ticker'] for row in results if row['ticker']]

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)  # 1 hour cache
    def get_news_tickers(
        date_from: Optional[date] = None,
        date_to: Optional[date] = None
    ) -> List[str]:
        """Get distinct tickers from news articles within a date range (cached 1 hour)."""
        return NewsRepository._get_news_tickers_impl(date_from, date_to)

    @staticmethod
    @_log_query_time
    def _get_all_news_tickers_impl() -> List[str]:
        """Get ALL distinct tickers from both AV and YF news tables (no date filter).

        Used to build date-independent sector and company dropdowns that include
        every ticker that has ever appeared in either AV or YF news, regardless of
        the currently selected date range.

        Both tables have a ticker index so DISTINCT queries are index-only scans.
        """
        query = """
            SELECT DISTINCT ticker FROM coreiq_av_market_news_sentiment
            WHERE ticker IS NOT NULL
            UNION
            SELECT DISTINCT ticker FROM coreiq_yf_market_news_sentiment
            WHERE ticker IS NOT NULL
            ORDER BY ticker
        """
        results = db_manager.execute_query_readonly(query, {})
        return [row['ticker'] for row in results if row['ticker']]

    @staticmethod
    @st.cache_data(ttl=21600, show_spinner=False)  # 6-hour cache — date-independent
    def get_all_news_tickers() -> List[str]:
        """Get ALL distinct tickers from AV + YF news tables (no date filter, 6-hour cache)."""
        return NewsRepository._get_all_news_tickers_impl()

    @staticmethod
    @_log_query_time
    def _get_sectors_impl() -> List[str]:
        """Get all distinct sectors from coreiq_av_companies_all.sector, title-cased.
        'None' bucket = AV articles whose ticker has no/null sector.
        'Unknown Sector' bucket = all YF articles (no sector data).
        """
        query = """
            SELECT DISTINCT sector
            FROM coreiq_av_companies_all
            WHERE sector IS NOT NULL AND sector != ''
              AND UPPER(sector) != 'NONE'
            ORDER BY sector
        """
        results = db_manager.execute_query_readonly(query, {})
        seen: set = set()
        sectors: List[str] = []
        for row in results:
            s = row['sector']
            if s:
                titled = s.title()
                if titled not in seen:
                    seen.add(titled)
                    sectors.append(titled)
        sectors.sort()
        sectors.append('None')            # AV articles with null/missing sector
        sectors.append('Unknown Sector')  # All YF articles
        return sectors

    @staticmethod
    @st.cache_data(ttl=21600, show_spinner=False)  # 6-hour cache — date-independent
    def get_sectors() -> List[str]:
        """Get all distinct sectors from AV + YF news (date-independent, 6-hour cache)."""
        return NewsRepository._get_sectors_impl()

    @staticmethod
    @_log_query_time
    def _get_ticker_sector_map_impl() -> Dict[str, str]:
        """Build ticker → sector map from coreiq_av_companies_all.

        Uses idx_av_companies_sector_symbol_name covering index for zero
        off-page reads.  Title-cases sectors to match get_sectors() output.
        Tickers with NULL/empty/'None' sector are mapped to 'None'.
        """
        query = """
            SELECT symbol, sector
            FROM coreiq_av_companies_all
        """
        results = db_manager.execute_query_readonly(query, {})
        ticker_map: Dict[str, str] = {}
        for row in results:
            sym = row['symbol']
            if not sym:
                continue
            raw = row['sector']
            if raw and raw.strip() and raw.strip().upper() != 'NONE':
                ticker_map[sym] = raw.strip().title()
            else:
                ticker_map[sym] = 'None'
        return ticker_map

    @staticmethod
    @st.cache_data(ttl=21600, show_spinner=False)  # 6-hour cache — same as get_sectors
    def get_ticker_sector_map() -> Dict[str, str]:
        """Cached ticker → sector map (title-cased, matching get_sectors dropdown)."""
        return NewsRepository._get_ticker_sector_map_impl()

    @staticmethod
    @_log_query_time
    def _get_companies_impl() -> List[Dict[str, str]]:
        """Get all companies from coreiq_av_companies_all master list (active stocks only).

        PERFORMANCE: Removed ORDER BY company_name from SQL.
        - SQL filesort of 21K rows is ~50ms saved on server.
        - Python sort below is ~5ms and produces identical result.
        - MySQL now does a simple covering index scan (Using index, no filesort)
          via idx_av_companies_dropdown(listing_status, asset_type, company_name, symbol).
        - The company_name prefix index (idx_av_company_name_sort) was also created
          but MySQL cannot use it as covering for SELECT due to prefix constraints.
        """
        query = """
            SELECT
                symbol AS ticker,
                company_name
            FROM coreiq_av_companies_all
        """
        results = db_manager.execute_query_readonly(query, {})
        companies = []
        for row in results:
            ticker = row['ticker']
            display_name = row['company_name'] or ticker
            companies.append({
                'ticker': ticker,
                'name': _format_company_name(display_name)
            })
        # Sort in Python — saves server-side filesort; identical final ordering
        companies.sort(key=lambda x: x['name'].lower())
        return companies

    @staticmethod
    @st.cache_data(ttl=21600, show_spinner=False)  # 6-hour cache — date-independent
    def get_companies() -> List[Dict[str, str]]:
        """Get all companies from AV + YF news (date-independent, 6-hour cache)."""
        return NewsRepository._get_companies_impl()

    @staticmethod
    @_log_query_time
    def get_company_name_by_ticker(ticker: str) -> Optional[str]:
        """Get company display name by ticker."""
        query = """
            SELECT name_coresight as display_name
            FROM coreiq_companies
            WHERE ticker = :ticker
            LIMIT 1
        """
        results = db_manager.execute_query_readonly(query, {'ticker': ticker})
        if results:
            return _format_company_name(results[0]['display_name'])
        return ticker  # Return ticker if company not found

    @staticmethod
    @st.cache_data(ttl=21600, show_spinner=False)  # 6-hour cache matches get_sectors
    def get_yf_topic_keys() -> List[str]:
        """Return all distinct YF topic keys (primary_topic_v1 + v2) for dropdown population.

        Called once per process at newsroom startup to feed merge_yf_topics().
        Any new category added to the YF table is automatically picked up here.
        6-hour cache — topics change rarely; UNION of two covering-index scans is ~1-2ms.
        """
        import time as _t
        _t0 = _t.perf_counter()
        try:
            from utils.server_logger import log_db_timing as _ldt
            # USE INDEX forces a covering index scan instead of a full table scan.
            # idx_yf_pub_topic_v1 (published_at, primary_topic_v1) covers v1 reads,
            # idx_yf_pub_topic_v2 (published_at, primary_topic_v2) covers v2 reads.
            # MySQL reads only the index pages (~1/10th the data of a table scan).
            # Without leading-column index on topic alone, DISTINCT still scans all
            # index entries — but index pages are much smaller than row pages.
            # Estimated: ~500ms index scan vs ~5000ms table scan (10× improvement).
            sql = """
                SELECT DISTINCT primary_topic_v1 AS topic
                FROM coreiq_yf_market_news_sentiment
                     USE INDEX (idx_yf_pub_topic_v1)
                WHERE primary_topic_v1 IS NOT NULL AND primary_topic_v1 != ''
                UNION
                SELECT DISTINCT primary_topic_v2
                FROM coreiq_yf_market_news_sentiment
                     USE INDEX (idx_yf_pub_topic_v2)
                WHERE primary_topic_v2 IS NOT NULL AND primary_topic_v2 != ''
            """
            rows = db_manager.execute_query_readonly(sql, {})
            result = [r['topic'] for r in rows if r.get('topic')]
            _ldt("get_yf_topic_keys", "coreiq_yf_market_news_sentiment", (_t.perf_counter() - _t0) * 1000, len(result))
            return result
        except Exception as exc:
            try:
                from utils.server_logger import log_structured_error as _lse
                _lse(exc, page="repository", component="NewsRepository", operation="get_yf_topic_keys")
            except Exception:
                pass
            return []

    @staticmethod
    @st.cache_data(ttl=43200, show_spinner=False)
    @_log_query_time
    def get_yf_articles(
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        limit: int = 2000,
        offset: int = 0,
        sort_ascending: bool = False,
        # Legacy params kept for call-site compat — ignored
        sector: Optional[str] = None,
        company_ticker: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[NewsArticle]:
        """
        Fetch YF news articles, deduplicated by news_id with merged tickers.

        PERF FIX (2026-04-02): Replaced two-phase query (60K covering scan + 2K
        detail fetch = 15.8s) with single combined GROUP BY query (~1.5-2s).
        MySQL server-side GROUP BY on the covering index collapses ~60K rows into
        ~2K unique news_ids, GROUP_CONCAT collects tickers, and a single JOIN
        fetches detail columns via 2K PK lookups. No Python dedup needed.

        - Sector filtering: handled by caller using cached companies_map
        - Keyword filtering: handled by caller on cached articles
        - Company ticker filtering: removed (company dropdown disabled)
        """
        from utils.server_logger import log_timing, log_db_timing, log_info, log_warning
        _t_start = time.perf_counter()

        if not date_from:
            date_from = date.today() - timedelta(days=30)

        date_sort = "ASC" if sort_ascending else "DESC"
        params: dict = {}

        if date_from:
            params['date_from'] = date_from
        if date_to:
            params['date_to_excl'] = date_to + timedelta(days=1)
        params['limit'] = limit
        params['offset'] = offset

        # ── Build WHERE ────────────────────────────────────────────────────────
        yf_where_parts = []
        if date_from:
            yf_where_parts.append("published_at >= :date_from")
        if date_to:
            yf_where_parts.append("published_at < :date_to_excl")
        yf_where = " AND ".join(yf_where_parts) if yf_where_parts else "1=1"

        # ── COMBINED QUERY (2026-04-02 perf fix) ─────────────────────────────
        # Single query replaces Phase A (60K covering scan) + Phase B (2K detail
        # fetch). Server-side GROUP BY on idx_yf_pub_nid_ticker collapses rows,
        # GROUP_CONCAT collects tickers, JOIN on MIN(id) fetches details.
        # Result: ~2K rows transferred instead of 62K. No Python dedup needed.
        #
        # PERF FIX (2026-04-02 v2): Parallel date-range split for cold buffer pool.
        # When date range >= 3 days, split into 2 parallel half-range queries.
        # Each half scans ~30K covering-index rows → GROUP BY → ~1K groups.
        # Wall clock = max(half1, half2) ≈ 7s instead of sequential 14s.

        _t_db = time.perf_counter()

        _can_yf_parallel = (
            date_from and date_to
            and (date_to - date_from).days >= 3
        )

        def _run_yf_half(p_from, p_to_excl, half_limit, half_offset):
            """Run one half-range YF combined query."""
            _hw = "published_at >= :date_from AND published_at < :date_to_excl"
            _hp = {
                'date_from': p_from,
                'date_to_excl': p_to_excl,
                'limit': half_limit,
                'offset': half_offset,
            }
            _hq = f"""
                SELECT y.id, d.news_id, d.published_at, d.tickers,
                       y.title, y.publisher, y.link,
                       y.primary_topic_v1, y.primary_topic_v2
                FROM coreiq_yf_market_news_sentiment y
                INNER JOIN (
                    SELECT news_id,
                           MAX(published_at) AS published_at,
                           MIN(id) AS min_id,
                           GROUP_CONCAT(DISTINCT ticker SEPARATOR ';;') AS tickers
                    FROM coreiq_yf_market_news_sentiment
                         USE INDEX (idx_yf_pub_nid_ticker)
                    WHERE {_hw}
                    GROUP BY news_id
                    ORDER BY MAX(published_at) {date_sort}
                    LIMIT :limit OFFSET :offset
                ) d ON y.id = d.min_id
                ORDER BY d.published_at {date_sort}
            """
            return db_manager.execute_query_readonly(_hq, _hp)

        try:
            _t_q = time.perf_counter()

            if _can_yf_parallel:
                from concurrent.futures import ThreadPoolExecutor as _YFPool
                _mid = date_from + (date_to - date_from) / 2
                _mid_excl = _mid + timedelta(days=1)
                _date_to_excl = date_to + timedelta(days=1)

                with _YFPool(max_workers=2) as _yfp:
                    _yf1 = _yfp.submit(_run_yf_half, date_from, _mid_excl, limit, offset)
                    _yf2 = _yfp.submit(_run_yf_half, _mid_excl, _date_to_excl, limit, offset)
                    _yr1 = _yf1.result(timeout=60)
                    _yr2 = _yf2.result(timeout=60)

                results = _yr1 + _yr2
            else:
                query = f"""
                    SELECT y.id, d.news_id, d.published_at, d.tickers,
                           y.title, y.publisher, y.link,
                           y.primary_topic_v1, y.primary_topic_v2
                    FROM coreiq_yf_market_news_sentiment y
                    INNER JOIN (
                        SELECT news_id,
                               MAX(published_at) AS published_at,
                               MIN(id) AS min_id,
                               GROUP_CONCAT(DISTINCT ticker SEPARATOR ';;') AS tickers
                        FROM coreiq_yf_market_news_sentiment
                             USE INDEX (idx_yf_pub_nid_ticker)
                        WHERE {yf_where}
                        GROUP BY news_id
                        ORDER BY MAX(published_at) {date_sort}
                        LIMIT :limit OFFSET :offset
                    ) d ON y.id = d.min_id
                    ORDER BY d.published_at {date_sort}
                """
                results = db_manager.execute_query_readonly(query, params)

            _q_ms = (time.perf_counter() - _t_q) * 1000
            log_db_timing("SELECT_YF_COMBINED", "coreiq_yf_market_news_sentiment", _q_ms, len(results))

        except Exception as exc:
            _db_ms = (time.perf_counter() - _t_db) * 1000
            log_warning(f"[TIMING] YF_QUERY_ERROR | {_db_ms:.1f}ms | "
                        f"{type(exc).__name__}: {str(exc)[:120]}")
            return []

        _db_ms = (time.perf_counter() - _t_db) * 1000
        _t_build = time.perf_counter()
        articles: List[NewsArticle] = []
        for row in results:
            title_val = row.get('title') or ''
            if not title_val:
                continue

            # Parse tickers from GROUP_CONCAT string
            ts_list: List[TickerSentiment] = []
            tickers_raw = row.get('tickers') or ''
            if tickers_raw:
                for _tk in tickers_raw.split(';;'):
                    _tk = _tk.strip()
                    if _tk:
                        ts_list.append(TickerSentiment(
                            ticker=_tk,
                            relevance_score='1.0',
                            ticker_sentiment_label='',
                            ticker_sentiment_score='0.0',
                            display_name='',
                        ))

            # Build topics list from primary_topic_v1/v2 (same format as AV)
            _yf_topics = []
            _t1 = row.get('primary_topic_v1') or ''
            _t2 = row.get('primary_topic_v2') or ''
            if _t1:
                _yf_topics.append({'topic': _t1})
            if _t2 and _t2 != _t1:
                _yf_topics.append({'topic': _t2})

            article = NewsArticle(
                id=row['id'],
                title=title_val,
                summary=title_val,
                url=row.get('link') or '',
                source=row.get('publisher') or '',
                source_domain='',
                time_published=row.get('published_at'),
                time_published_raw='',
                overall_sentiment_score=0.0,
                overall_sentiment_label='',
                banner_image=None,
                ticker_sentiment=ts_list,
                topics=_yf_topics,
                category_within_source='',
                source_tag='yf',
            )
            articles.append(article)
        _build_ms = (time.perf_counter() - _t_build) * 1000
        log_timing("YF_ARTICLES_BUILD", _build_ms, f"rows={len(articles)}")

        _total_ms = (time.perf_counter() - _t_start) * 1000
        log_timing("YF_ARTICLES_TOTAL", _total_ms, f"rows={len(articles)} db={_db_ms:.1f}ms")

        if _total_ms > 2000:
            log_warning(f"[TIMING] YF_ARTICLES_SLOW | {_total_ms:.1f}ms | "
                       f"db={_db_ms:.1f}ms build={_build_ms:.1f}ms rows={len(articles)}")

        return articles

    @staticmethod
    def refresh_yf_article_head() -> dict:
        """Removed — no longer needed. Optimization uses existing tables only."""
        raise NotImplementedError(
            "refresh_yf_article_head has been removed. "
            "YF queries now use the raw table directly with an optimized IN-subquery strategy."
        )


class CompanyOverviewRepository:
    """Repository for AV and YF company overview tables."""

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_company_overview(ticker: str) -> Optional[CompanyOverview]:
        """
        Get company overview by ticker (cached 10 min).
        Supports both SEC and YFinance companies.

        Args:
            ticker: Company ticker symbol

        Returns:
            CompanyOverview object or None if not found
        """
        from data.source_router import get_company_source

        source = get_company_source(ticker)
        # coreiq_yf_company_overview stores base ticker (e.g. 'ADS' not 'ADS.DE')
        _ov_ticker = ticker.split('.')[0] if ('.' in ticker and source == 'YFinance') else ticker

        if source == 'YFinance':
            # Query YF company overview table - data is in payload_json
            query = """
                SELECT
                    ticker,
                    company_name as name,
                    payload_json as raw_json,
                    ingested_at as fetched_at_utc
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
        else:
            # SEC companies (default): query SEC table
            query = """
                SELECT
                    ticker,
                    name,
                    exchange,
                    currency,
                    country,
                    sector,
                    industry,
                    company_description,
                    official_site,
                    fiscal_year_end,
                    cik,
                    market_capitalization,
                    pe_ratio,
                    eps,
                    dividend_yield,
                    analyst_target_price,
                    raw_json,
                    fetched_at_utc
                FROM coreiq_av_company_overview
                WHERE ticker = :ticker
                ORDER BY fetched_at_utc DESC
                LIMIT 1
            """

        results = db_manager.execute_query_readonly(query, {"ticker": _ov_ticker})

        if not results:
            return None

        row = results[0]

        # Parse raw_json for additional fields
        raw_json_data = {}
        if row.get('raw_json'):
            try:
                raw_json_data = json.loads(row['raw_json'])
            except json.JSONDecodeError:
                pass

        # Helper function to safely get float from various formats
        def safe_float(value, default=None):
            if value is None:
                return default
            try:
                return float(value)
            except (ValueError, TypeError):
                return default

        # Helper function to safely get int
        def safe_int(value, default=None):
            if value is None:
                return default
            try:
                return int(float(value))
            except (ValueError, TypeError):
                return default

        # Fetch primary_industry_coresight AND exchange from CACHED companies map (FAST - no DB query!)
        companies_map = CompanyRepository.get_companies_map()
        company_info = companies_map.get(ticker, {})
        primary_industry_coresight = company_info.get('primary_industry_coresight')

        # Fast Exchange retrieval from cached map, no JSON parsing or re-lookup needed
        cached_exchange = company_info.get('exchange')

        # Canonical display name for Market Data header + profile + dropdown:
        # ONLY `coreiq_companies.name_coresight` (never AV/YF vendor `name` columns).
        _coresight_name = (str(company_info.get("name_coresight") or "").strip()) or (
            str(ticker or "").strip()
        )

        # YFinance: always take this branch (payload may be empty). Never fall through to the SEC
        # row shape — YF overview rows only have ticker / company_name / payload_json / ingested_at.
        if source == "YFinance":
            info: Dict[str, Any] = raw_json_data.get("info", {}) if raw_json_data else {}
            fiscal_year_end = _extract_yf_fiscal_year_end(raw_json_data) if raw_json_data else None
            return CompanyOverview(
                ticker=row["ticker"] or ticker,
                name=_coresight_name,
                exchange=cached_exchange,
                currency=info.get("currency"),
                country=info.get("country"),
                sector=info.get("sector"),
                industry=info.get("industry"),
                primary_industry_coresight=primary_industry_coresight,
                company_description=info.get("longBusinessSummary"),
                official_site=info.get("website"),
                fiscal_year_end=fiscal_year_end,
                cik=None,
                market_capitalization=safe_int(info.get("marketCap")),
                pe_ratio=safe_float(info.get("trailingPE")),
                eps=safe_float(info.get("trailingEps")),
                dividend_yield=safe_float(info.get("dividendYield")),
                analyst_target_price=None,
                fetched_at_utc=row["fetched_at_utc"],
                address=info.get("address1"),
                revenue_ttm=safe_float(info.get("totalRevenue")),
                ebitda=safe_float(info.get("ebitda")),
                profit_margin=safe_float(info.get("profitMargins")),
                shares_outstanding=safe_int(info.get("sharesOutstanding")),
                week_52_high=safe_float(info.get("fiftyTwoWeekHigh")),
                week_52_low=safe_float(info.get("fiftyTwoWeekLow")),
            )

        # SEC companies (default)
        return CompanyOverview(
            ticker=row['ticker'] or ticker,
            name=_coresight_name,
            exchange=cached_exchange,
            currency=row['currency'],
            country=row['country'],
            sector=row['sector'],
            industry=row['industry'],
            primary_industry_coresight=primary_industry_coresight,
            company_description=row['company_description'],
            official_site=row['official_site'],
            fiscal_year_end=row['fiscal_year_end'],
            cik=row['cik'],
            market_capitalization=safe_int(row['market_capitalization']),
            pe_ratio=safe_float(row['pe_ratio']),
            eps=safe_float(row['eps']),
            dividend_yield=safe_float(row['dividend_yield']),
            analyst_target_price=safe_float(row['analyst_target_price']),
            fetched_at_utc=row['fetched_at_utc'],
            # From raw_json
            address=raw_json_data.get('Address'),
            revenue_ttm=safe_float(raw_json_data.get('RevenueTTM')),
            ebitda=safe_float(raw_json_data.get('EBITDA')),
            profit_margin=safe_float(raw_json_data.get('ProfitMargin')),
            shares_outstanding=safe_int(raw_json_data.get('SharesOutstanding')),
            week_52_high=safe_float(raw_json_data.get('52WeekHigh')),
            week_52_low=safe_float(raw_json_data.get('52WeekLow')),
            dividend_per_share=safe_float(raw_json_data.get('DividendPerShare')),
            latest_quarter=raw_json_data.get('LatestQuarter'),
            # Placeholder fields - not in database
            employees="N/A",
            year_founded="N/A",
            professionals_profiled="N/A",
            coverage_summary="N/A",
            coverage_list="N/A",
            relationships="N/A",
            projects="N/A",
            activity_logs="N/A"
        )

    @staticmethod
    def company_exists(ticker: str) -> bool:
        """Check if company overview exists for ticker."""
        query = """
            SELECT 1
            FROM coreiq_av_company_overview
            WHERE ticker = :ticker
            LIMIT 1
        """
        results = db_manager.execute_query_readonly(query, {"ticker": ticker})
        return len(results) > 0

class EarningsCallRepository:
    """Repository for coreiq_av_earnings_call_transcripts table."""

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_companies_with_earnings() -> List[Dict[str, str]]:
        """Get only companies that have earnings call transcripts (cached 10 min).

        PERFORMANCE OPTIMIZATION:
        1. Get distinct tickers from transcripts table (fast, no JOIN)
        2. Use CACHED companies map for name lookup (no DB query!)

        Returns:
            List of dicts with 'ticker' and 'name' keys.
        """
        from utils.server_logger import log_db_timing
        import time
        total_start = time.perf_counter()

        # Step 1: Get tickers from transcripts table (fast, indexed)
        step_start = time.perf_counter()
        query = """
            SELECT DISTINCT ticker
            FROM coreiq_av_earnings_call_transcripts
            WHERE has_transcript = 1
            ORDER BY ticker
        """
        results = db_manager.execute_query_readonly(query)
        tickers = [row['ticker'] for row in results if row['ticker']]
        step1_ms = (time.perf_counter() - step_start) * 1000
        log_db_timing("get_companies_with_earnings.distinct_tickers", "coreiq_av_earnings_call_transcripts", step1_ms, rows=len(tickers))

        # Step 2: Use CACHED companies map for name lookup (FAST!)
        step_start = time.perf_counter()
        companies_map = CompanyRepository.get_companies_map()
        step2_ms = (time.perf_counter() - step_start) * 1000
        log_db_timing("get_companies_with_earnings.companies_map", "coreiq_companies", step2_ms, rows=len(companies_map))

        # Step 3: Build company list
        step_start = time.perf_counter()
        companies = []

        for ticker in tickers:
            company = companies_map.get(ticker)
            if company:
                display_name = company['name_coresight'] or ticker
                companies.append({
                    'ticker': ticker,
                    'name': _format_company_name(display_name)
                })
            else:
                # Fallback: ticker not in companies table
                companies.append({'ticker': ticker, 'name': ticker})

        # Sort by name
        companies.sort(key=lambda x: x['name'].lower())
        step3_ms = (time.perf_counter() - step_start) * 1000

        total_ms = (time.perf_counter() - total_start) * 1000
        log_db_timing("get_companies_with_earnings.TOTAL", "coreiq_av_earnings_call_transcripts", total_ms, rows=len(companies))

        return companies

    @staticmethod
    def _parse_quarter_param(quarter) -> Optional[int]:
        """Convert quarter param (int, str like 'Q1', or 'Q1') to integer 1-4."""
        if quarter is None:
            return None
        if isinstance(quarter, int):
            return quarter
        # Handle string like "Q1", "Q2", etc.
        q_str = str(quarter).strip().upper()
        if q_str.startswith('Q') and len(q_str) == 2 and q_str[1].isdigit():
            return int(q_str[1])
        # Try direct int parse
        try:
            return int(q_str)
        except (ValueError, TypeError):
            return None

    @staticmethod
    @st.cache_data(ttl=60, show_spinner=False)  # Reduced from 300 to 60 seconds for fresher data
    @_log_query_time
    def get_earnings_calls(
        ticker: Optional[str] = None,
        year: Optional[int] = None,
        quarter=None,
        has_transcript_only: bool = True,
        limit: int = 100
    ) -> List[EarningsCall]:
        """Get earnings calls with optional filtering (cached 1 min for recent data freshness).

        Args:
            ticker: Filter by company ticker
            year: Filter by year
            quarter: Filter by quarter — accepts int (1-4) or str ('Q1'-'Q4')
            has_transcript_only: Only return calls with transcripts
            limit: Maximum number of results

        Returns:
            List of EarningsCall objects
        """
        from utils.server_logger import log_db_timing
        import time
        total_start = time.perf_counter()
        q_int = EarningsCallRepository._parse_quarter_param(quarter)

        # Build query
        query = """
            SELECT
                id,
                source,
                ticker,
                quarter,
                year,
                q,
                transcript_text,
                has_transcript,
                title,
                event_datetime_utc,
                fetched_at_utc
            FROM coreiq_av_earnings_call_transcripts
            WHERE 1=1
        """
        params = {}

        if ticker:
            query += " AND ticker = :ticker"
            params['ticker'] = ticker

        if year:
            params['year'] = int(year) if isinstance(year, str) else year
            query += " AND year = :year"

        if q_int:
            query += " AND q = :quarter"
            params['quarter'] = q_int

        if has_transcript_only:
            query += " AND has_transcript = 1"

        query += " ORDER BY year DESC, q DESC"
        query += " LIMIT :limit"
        params['limit'] = limit

        # Execute query
        query_start = time.perf_counter()
        results = db_manager.execute_query_readonly(query, params)
        query_ms = (time.perf_counter() - query_start) * 1000
        log_db_timing("get_earnings_calls.query", "coreiq_av_earnings_call_transcripts", query_ms, rows=len(results), ticker=str(ticker or ''))

        # Build objects
        build_start = time.perf_counter()
        earnings_calls = []
        for row in results:
            earnings_calls.append(EarningsCall(
                id=row['id'],
                source=row['source'],
                ticker=row['ticker'],
                quarter=row['quarter'],
                year=row['year'],
                q=row['q'],
                transcript_text=row['transcript_text'],
                has_transcript=bool(row['has_transcript']),
                title=row['title'],
                event_datetime_utc=row['event_datetime_utc'],
                fetched_at_utc=row['fetched_at_utc']
            ))
        build_ms = (time.perf_counter() - build_start) * 1000

        total_ms = (time.perf_counter() - total_start) * 1000
        log_db_timing("get_earnings_calls.TOTAL", "coreiq_av_earnings_call_transcripts", total_ms, rows=len(earnings_calls), ticker=str(ticker or ''))

        return earnings_calls

    @staticmethod
    @_log_query_time
    def get_earnings_call_by_id(earnings_id: int) -> Optional[EarningsCall]:
        """Get a single earnings call by ID."""
        query = """
            SELECT
                id,
                source,
                ticker,
                quarter,
                year,
                q,
                transcript_text,
                has_transcript,
                title,
                event_datetime_utc,
                fetched_at_utc
            FROM coreiq_av_earnings_call_transcripts
            WHERE id = :id
            LIMIT 1
        """
        results = db_manager.execute_query_readonly(query, {"id": earnings_id})

        if not results:
            return None

        row = results[0]
        return EarningsCall(
            id=row['id'],
            source=row['source'],
            ticker=row['ticker'],
            quarter=row['quarter'],
            year=row['year'],
            q=row['q'],
            transcript_text=row['transcript_text'],
            has_transcript=bool(row['has_transcript']),
            title=row['title'],
            event_datetime_utc=row['event_datetime_utc'],
            fetched_at_utc=row['fetched_at_utc']
        )

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_available_years(ticker: str) -> List[int]:
        """Get distinct years that have transcripts for a given company (cached 10 min).

        Args:
            ticker: Company ticker (required)

        Returns:
            List of years (descending order)
        """
        from utils.server_logger import log_db_timing
        import time
        start = time.perf_counter()

        query = """
            SELECT DISTINCT year
            FROM coreiq_av_earnings_call_transcripts
            WHERE has_transcript = 1
              AND ticker = :ticker
            ORDER BY year DESC
        """
        results = db_manager.execute_query_readonly(query, {'ticker': ticker})
        years = [row['year'] for row in results]
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_db_timing("get_available_years", "coreiq_av_earnings_call_transcripts", elapsed_ms, rows=len(years), ticker=ticker)

        return years

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_available_quarters(ticker: str, year) -> List[str]:
        """Get distinct quarters that have transcripts for a company+year (cached 10 min).

        Args:
            ticker: Company ticker (required)
            year: Year to filter by (required, accepts str or int)

        Returns:
            List of quarter strings like ['Q1', 'Q2', 'Q3', 'Q4'] (ascending)
        """
        from utils.server_logger import log_db_timing
        import time
        start = time.perf_counter()

        year_int = int(year) if isinstance(year, str) else year
        query = """
            SELECT DISTINCT q
            FROM coreiq_av_earnings_call_transcripts
            WHERE has_transcript = 1
              AND ticker = :ticker
              AND year = :year
            ORDER BY q
        """
        results = db_manager.execute_query_readonly(query, {'ticker': ticker, 'year': year_int})
        quarters = [f"Q{row['q']}" for row in results]
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_db_timing("get_available_quarters", "coreiq_av_earnings_call_transcripts", elapsed_ms, rows=len(quarters), ticker=ticker)

        return quarters

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_years_and_quarters(ticker: str) -> Dict[int, List[str]]:
        """Get all year→quarters combos for a ticker in ONE query (saves 1 round-trip).

        Returns:
            Dict mapping year (int) to list of quarter strings ['Q1','Q2',...]
            Years sorted descending, quarters sorted ascending.
        """
        from utils.server_logger import log_db_timing
        import time
        start = time.perf_counter()

        query = """
            SELECT DISTINCT year, q
            FROM coreiq_av_earnings_call_transcripts
            WHERE has_transcript = 1
              AND ticker = :ticker
            ORDER BY year DESC, q ASC
        """
        results = db_manager.execute_query_readonly(query, {'ticker': ticker})
        mapping: Dict[int, List[str]] = {}
        for row in results:
            yr = row['year']
            if yr not in mapping:
                mapping[yr] = []
            mapping[yr].append(f"Q{row['q']}")

        elapsed_ms = (time.perf_counter() - start) * 1000
        log_db_timing("get_years_and_quarters", "coreiq_av_earnings_call_transcripts", elapsed_ms, rows=len(results), ticker=ticker)
        return mapping

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_all_available_years() -> List[int]:
        """Get all distinct years that have transcripts across all companies (cached 10 min)."""
        from utils.server_logger import log_db_timing
        import time
        start = time.perf_counter()

        query = """
            SELECT DISTINCT year
            FROM coreiq_av_earnings_call_transcripts
            WHERE has_transcript = 1
            ORDER BY year DESC
        """
        results = db_manager.execute_query_readonly(query, {})
        years = [row['year'] for row in results]
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_db_timing("get_all_available_years", "coreiq_av_earnings_call_transcripts", elapsed_ms, rows=len(years))

        return years

    @staticmethod
    @st.cache_data(ttl=120, show_spinner=False)
    @_log_query_time
    def search_transcripts_fulltext(
        keyword: str,
        ticker: Optional[str] = None,
        year: Optional[int] = None,
        quarter: Optional[str] = None,
        limit: int = 50,
        allowed_tickers: Optional[Tuple[str, ...]] = None,
        offset: int = 0,
    ) -> List[Dict]:
        """
        Cross-transcript keyword search using MySQL FULLTEXT index.

        Uses the existing idx_transcript_fulltext_search FULLTEXT index for
        sub-20ms keyword lookup across all 2,170+ transcripts. Falls back to
        LIKE for very short keywords (<3 chars) or stopwords.

        Returns raw dicts with: id, ticker, year, quarter, q, transcript_text
        """
        from utils.server_logger import log_db_timing
        import time
        total_start = time.perf_counter()

        keyword = keyword.strip()
        if not keyword:
            return []

        scoped_tickers = tuple(
            dict.fromkeys(
                str(t).strip().upper()
                for t in (allowed_tickers or ())
                if str(t).strip()
            )
        )
        if allowed_tickers is not None and not scoped_tickers:
            return []

        params: Dict = {}
        used_fallback = False

        # FULLTEXT boolean mode with prefix wildcard (3+ chars)
        if len(keyword) >= 3:
            # Build boolean-mode query: single word → +word*, multi-word → +"w1" +"w2"
            words = keyword.split()
            if len(words) == 1:
                ft_kw = f'+{words[0]}*'
            else:
                ft_kw = ' '.join(f'+"{w}"' for w in words if w)
            base_query = """
                SELECT id, ticker, year, quarter, q, transcript_text
                FROM coreiq_av_earnings_call_transcripts
                WHERE MATCH(transcript_text) AGAINST (:kw IN BOOLEAN MODE)
                  AND has_transcript = 1
            """
            params['kw'] = ft_kw
        else:
            # Short keyword: LIKE fallback (no index, but rare)
            base_query = """
                SELECT id, ticker, year, quarter, q, transcript_text
                FROM coreiq_av_earnings_call_transcripts
                WHERE transcript_text LIKE :kw
                  AND has_transcript = 1
            """
            params['kw'] = f'%{keyword}%'

        if ticker and ticker != 'ALL':
            clean_ticker = str(ticker).strip().upper()
            if scoped_tickers and clean_ticker not in scoped_tickers:
                return []
            base_query += " AND ticker = :ticker"
            params['ticker'] = clean_ticker
        elif scoped_tickers:
            wl_keys = []
            for idx, scoped_ticker in enumerate(scoped_tickers):
                key = f"wl_ticker_{idx}"
                wl_keys.append(f":{key}")
                params[key] = scoped_ticker
            base_query += f" AND ticker IN ({', '.join(wl_keys)})"

        if year and str(year) != 'ALL':
            base_query += " AND year = :year"
            params['year'] = int(year) if isinstance(year, str) else year

        if quarter and quarter != 'ALL':
            q_int = EarningsCallRepository._parse_quarter_param(quarter)
            if q_int:
                base_query += " AND q = :quarter_q"
                params['quarter_q'] = q_int

        base_query += " ORDER BY year DESC, q DESC, id DESC LIMIT :limit OFFSET :offset"
        params['limit'] = limit
        params['offset'] = offset

        # Execute main query
        query_start = time.perf_counter()
        results = db_manager.execute_query_readonly(base_query, params)
        query_ms = (time.perf_counter() - query_start) * 1000
        log_db_timing("search_transcripts_fulltext.main_query", "coreiq_av_earnings_call_transcripts", query_ms, rows=len(results), ticker=str(ticker or ''))

        # FULLTEXT stopword fallback: if no results and keyword >= 3 chars, try LIKE
        if not results and len(keyword) >= 3:
            used_fallback = True
            fallback = """
                SELECT id, ticker, year, quarter, q, transcript_text
                FROM coreiq_av_earnings_call_transcripts
                WHERE transcript_text LIKE :kw AND has_transcript = 1
            """
            fb_params: Dict = {'kw': f'%{keyword}%'}
            if ticker and ticker != 'ALL':
                clean_ticker = str(ticker).strip().upper()
                if scoped_tickers and clean_ticker not in scoped_tickers:
                    return []
                fallback += " AND ticker = :ticker"
                fb_params['ticker'] = clean_ticker
            elif scoped_tickers:
                wl_keys = []
                for idx, scoped_ticker in enumerate(scoped_tickers):
                    key = f"fb_wl_ticker_{idx}"
                    wl_keys.append(f":{key}")
                    fb_params[key] = scoped_ticker
                fallback += f" AND ticker IN ({', '.join(wl_keys)})"
            if year and str(year) != 'ALL':
                fallback += " AND year = :year"
                fb_params['year'] = int(year) if isinstance(year, str) else year
            if quarter and quarter != 'ALL':
                q_int = EarningsCallRepository._parse_quarter_param(quarter)
                if q_int:
                    fallback += " AND q = :quarter_q"
                    fb_params['quarter_q'] = q_int
            fallback += " ORDER BY year DESC, q DESC, id DESC LIMIT :limit OFFSET :offset"
            fb_params['limit'] = limit
            fb_params['offset'] = offset

            fb_start = time.perf_counter()
            results = db_manager.execute_query_readonly(fallback, fb_params)
            fb_ms = (time.perf_counter() - fb_start) * 1000
            log_db_timing("search_transcripts_fulltext.LIKE_fallback", "coreiq_av_earnings_call_transcripts", fb_ms, rows=len(results), ticker=str(ticker or ''))

        total_ms = (time.perf_counter() - total_start) * 1000
        log_db_timing("search_transcripts_fulltext.TOTAL", "coreiq_av_earnings_call_transcripts", total_ms, rows=len(results), ticker=str(ticker or ''))

        return [dict(r) for r in results]

    # ------------------------------------------------------------------
    # NON-SEC Transcript PDFs  (from coreiq_filing_metrics_v4)
    # ------------------------------------------------------------------
    _TRANSCRIPT_DOC_TYPES = ('transcript-Q1', 'transcript-Q2', 'transcript-Q3', 'transcript-Q4')

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    def get_non_sec_transcript_companies() -> List[Dict[str, str]]:
        """Get companies that have transcript PDFs in coreiq_filing_metrics_v4 (cached 10 min).

        Uses company_name directly from coreiq_filing_metrics_v4 (ticker+company_name is
        the unique composite — ticker alone is NOT unique across sources).
        """
        from utils.server_logger import log_db_timing
        import time as _t
        _start = _t.perf_counter()

        query = """
            SELECT DISTINCT ticker, company_name FROM coreiq_filing_metrics_v4
            WHERE doc_type IN ('transcript-Q1','transcript-Q2','transcript-Q3','transcript-Q4')
            ORDER BY company_name
        """
        results = db_manager.execute_query_readonly(query)
        companies = []
        _seen_tickers = set()
        for row in results:
            ticker = row.get('ticker')
            cname = row.get('company_name') or ticker
            if ticker and ticker not in _seen_tickers:
                _seen_tickers.add(ticker)
                companies.append({'ticker': ticker, 'name': _format_company_name(cname)})
        companies.sort(key=lambda x: x['name'].lower())

        _elapsed_ms = (_t.perf_counter() - _start) * 1000
        log_db_timing("get_non_sec_transcript_companies", "coreiq_filing_metrics_v4", _elapsed_ms, rows=len(companies))
        return companies

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    def get_non_sec_transcript_years_and_quarters(ticker: str) -> Dict[int, List[str]]:
        """Get all year->quarters for NON-SEC transcripts in ONE query.

        Returns dict mapping year (int) to list of quarter strings ['Q1','Q2',...].
        """
        from utils.server_logger import log_db_timing
        import time as _t
        _start = _t.perf_counter()

        query = """
            SELECT DISTINCT report_fiscal_year, doc_type FROM coreiq_filing_metrics_v4
            WHERE ticker = :ticker
              AND doc_type IN ('transcript-Q1','transcript-Q2','transcript-Q3','transcript-Q4')
            ORDER BY report_fiscal_year DESC, doc_type ASC
        """
        results = db_manager.execute_query_readonly(query, {'ticker': ticker})
        mapping: Dict[int, List[str]] = {}
        for row in results:
            yr = row.get('report_fiscal_year')
            dt = row.get('doc_type', '')
            if yr and dt:
                mapping.setdefault(yr, []).append(dt.replace('transcript-', ''))

        _elapsed_ms = (_t.perf_counter() - _start) * 1000
        log_db_timing("get_non_sec_transcript_years_and_quarters", "coreiq_filing_metrics_v4", _elapsed_ms, rows=len(results), ticker=ticker)
        return mapping

class BalanceSheetRepository:
    """Repository for coreiq_av_financials_balance_sheet table.

    Uses raw_json column for data extraction as per manager requirements.
    """

    # Mapping of UI labels to raw_json keys
    # Organized by section: Assets, Liabilities, Shareholders' Equity
    # IMPORTANT: Totals come AFTER their components (at the bottom)
    LINE_ITEMS = [
        # ASSETS - Current
        ("Cash & Cash Equivalents", "cashAndCashEquivalentsAtCarryingValue", False, "assets"),
        ("Cash & Short Term Investments", "cashAndShortTermInvestments", False, "assets"),
        ("Inventory", "inventory", False, "assets"),
        ("Current Net Receivables", "currentNetReceivables", False, "assets"),
        ("Other Current Assets", "otherCurrentAssets", False, "assets"),
        ("Total Current Assets", "totalCurrentAssets", False, "assets"),

        # ASSETS - Non-Current
        ("Property Plant & Equipment", "propertyPlantEquipment", False, "assets"),
        ("Intangible Assets", "intangibleAssets", False, "assets"),
        ("Intangible Assets Excl. Goodwill", "intangibleAssetsExcludingGoodwill", False, "assets"),
        ("Goodwill", "goodwill", False, "assets"),
        ("Long Term Investments", "longTermInvestments", False, "assets"),
        ("Other Non-Current Assets", "otherNonCurrentAssets", False, "assets"),
        ("Total Non-Current Assets", "totalNonCurrentAssets", False, "assets"),

        # ASSETS - Total
        ("Total Assets", "totalAssets", False, "assets"),

        # LIABILITIES - Current
        ("Current Accounts Payable", "currentAccountsPayable", False, "liabilities"),
        ("Deferred Revenue", "deferredRevenue", False, "liabilities"),
        ("Current Debt", "currentDebt", False, "liabilities"),
        ("Short Term Debt", "shortTermDebt", False, "liabilities"),
        ("Other Current Liabilities", "otherCurrentLiabilities", False, "liabilities"),
        ("Total Current Liabilities", "totalCurrentLiabilities", False, "liabilities"),

        # LIABILITIES - Non-Current
        ("Long Term Debt", "longTermDebt", False, "liabilities"),
        ("Long Term Debt Noncurrent", "longTermDebtNoncurrent", False, "liabilities"),
        ("Capital Lease Obligations", "capitalLeaseObligations", False, "liabilities"),
        ("Other Non-Current Liabilities", "otherNonCurrentLiabilities", False, "liabilities"),
        ("Total Non-Current Liabilities", "totalNonCurrentLiabilities", False, "liabilities"),

        # LIABILITIES - Total
        ("Total Liabilities", "totalLiabilities", False, "liabilities"),

        # SHAREHOLDERS' EQUITY - Components
        ("Common Stock", "commonStock", False, "equity"),
        ("Retained Earnings", "retainedEarnings", False, "equity"),
        ("Treasury Stock", "treasuryStock", False, "equity"),

        # SHAREHOLDERS' EQUITY - Total
        ("Total Shareholder Equity", "totalShareholderEquity", False, "equity"),
    ]

    # ── Cached single-query: fetches ALL rows for a ticker in one go ──
    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_all_annual_rows(ticker: str, period_type: str = "annual") -> List[Dict[str, Any]]:
        """Fetch all balance sheet rows for a ticker (cached 5 min).

        This single query replaces get_date_range, get_available_dates,
        get_reported_currency, AND get_balance_sheet_data — cutting
        4 network round-trips down to 1 (or 0 on cache hit).

        Supports both SEC and YFinance companies.

        Args:
            ticker: Company ticker symbol
            period_type: 'annual' or 'quarterly' (default: 'annual')
        """
        t0 = time.perf_counter()

        # Map period_type to database values
        period_value = period_type.lower()

        # Detect company source
        from data.source_router import get_company_source
        source = get_company_source(ticker)
        # Composite tickers (e.g. 'ADS.DE') use yf_symbol; plain YF tickers use ticker column
        _yf_is_composite = '.' in ticker
        _yf_base = ticker.split('.')[0] if _yf_is_composite else ticker

        if source == 'YFinance':
            # YF companies: query YF table with pivot, convert to SEC-style raw_json format
            # Get currency from YF company overview first (overview table uses base ticker)
            currency_query = """
                SELECT payload_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
            currency_result = db_manager.execute_query_readonly(currency_query, {"ticker": _yf_base})
            yf_currency = 'USD'
            if currency_result and currency_result[0].get('payload_json'):
                try:
                    payload = json.loads(currency_result[0]['payload_json'])
                    # Support both nested {"info": {...}} and flat {"currency": ...} structures
                    info = payload.get('info', payload)
                    yf_currency = info.get('financialCurrency') or info.get('currency') or 'USD'
                except:
                    yf_currency = 'USD'

            _yf_col = "yf_symbol" if _yf_is_composite else "ticker"
            query = f"""
                SELECT
                    period_end as fiscal_date_ending,
                    MAX(CASE WHEN line_item = 'Cash And Cash Equivalents' THEN value END) as cashAndCashEquivalentsAtCarryingValue,
                    MAX(CASE WHEN line_item IN ('Cash And Short Term Investments', 'Cash Cash Equivalents And Short Term Investments') THEN value END) as cashAndShortTermInvestments,
                    MAX(CASE WHEN line_item = 'Inventory' THEN value END) as inventory,
                    MAX(CASE WHEN line_item IN ('Net Receivables', 'Accounts Receivable') THEN value END) as currentNetReceivables,
                    MAX(CASE WHEN line_item = 'Other Current Assets' THEN value END) as otherCurrentAssets,
                    MAX(CASE WHEN line_item IN ('Total Current Assets', 'Current Assets') THEN value END) as totalCurrentAssets,
                    MAX(CASE WHEN line_item = 'Net PPE' THEN value END) as propertyPlantEquipment,
                    MAX(CASE WHEN line_item = 'Intangible Assets' THEN value END) as intangibleAssets,
                    MAX(CASE WHEN line_item = 'Goodwill' THEN value END) as goodwill,
                    MAX(CASE WHEN line_item = 'Investments And Advances' THEN value END) as longTermInvestments,
                    MAX(CASE WHEN line_item = 'Other Non Current Assets' THEN value END) as otherNonCurrentAssets,
                    MAX(CASE WHEN line_item = 'Total Non Current Assets' THEN value END) as totalNonCurrentAssets,
                    MAX(CASE WHEN line_item = 'Total Assets' THEN value END) as totalAssets,
                    MAX(CASE WHEN line_item = 'Payables And Accrued Expenses' THEN value END) as currentAccountsPayable,
                    MAX(CASE WHEN line_item = 'Current Debt' THEN value END) as currentDebt,
                    MAX(CASE WHEN line_item = 'Other Current Liabilities' THEN value END) as otherCurrentLiabilities,
                    MAX(CASE WHEN line_item IN ('Total Current Liabilities', 'Current Liabilities') THEN value END) as totalCurrentLiabilities,
                    MAX(CASE WHEN line_item = 'Long Term Debt' THEN value END) as longTermDebtNoncurrent,
                    MAX(CASE WHEN line_item = 'Other Non Current Liabilities' THEN value END) as otherNonCurrentLiabilities,
                    MAX(CASE WHEN line_item = 'Total Non Current Liabilities' THEN value END) as totalNonCurrentLiabilities,
                    MAX(CASE WHEN line_item = 'Total Liabilities Net Minority Interest' THEN value END) as totalLiabilities,
                    MAX(CASE WHEN line_item = 'Common Stock' THEN value END) as commonStock,
                    MAX(CASE WHEN line_item = 'Retained Earnings' THEN value END) as retainedEarnings,
                    MAX(CASE WHEN line_item = 'Stockholders Equity' THEN value END) as totalShareholderEquity,
                    '{yf_currency}' as reported_currency
                FROM coreiq_yf_financials_balance_sheet
                WHERE {_yf_col} = :ticker
                  AND frequency = '{period_value}'
                GROUP BY period_end
                ORDER BY period_end ASC
            """
            rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
            # Convert to SEC-style format with raw_json
            results = []
            for row in rows:
                # Build raw_json dict from columns
                raw_data = {
                    'cashAndCashEquivalentsAtCarryingValue': row.get('cashAndCashEquivalentsAtCarryingValue'),
                    'cashAndShortTermInvestments': row.get('cashAndShortTermInvestments'),
                    'inventory': row.get('inventory'),
                    'currentNetReceivables': row.get('currentNetReceivables'),
                    'otherCurrentAssets': row.get('otherCurrentAssets'),
                    'totalCurrentAssets': row.get('totalCurrentAssets'),
                    'propertyPlantEquipment': row.get('propertyPlantEquipment'),
                    'intangibleAssets': row.get('intangibleAssets'),
                    'goodwill': row.get('goodwill'),
                    'longTermInvestments': row.get('longTermInvestments'),
                    'otherNonCurrentAssets': row.get('otherNonCurrentAssets'),
                    'totalNonCurrentAssets': row.get('totalNonCurrentAssets'),
                    'totalAssets': row.get('totalAssets'),
                    'currentAccountsPayable': row.get('currentAccountsPayable'),
                    'currentDebt': row.get('currentDebt'),
                    'otherCurrentLiabilities': row.get('otherCurrentLiabilities'),
                    'totalCurrentLiabilities': row.get('totalCurrentLiabilities'),
                    'longTermDebtNoncurrent': row.get('longTermDebtNoncurrent'),
                    'otherNonCurrentLiabilities': row.get('otherNonCurrentLiabilities'),
                    'totalNonCurrentLiabilities': row.get('totalNonCurrentLiabilities'),
                    'totalLiabilities': row.get('totalLiabilities'),
                    'commonStock': row.get('commonStock'),
                    'retainedEarnings': row.get('retainedEarnings'),
                    'totalShareholderEquity': row.get('totalShareholderEquity'),
                }
                # Remove None values
                raw_data = {k: v for k, v in raw_data.items() if v is not None}
                results.append({
                    'fiscal_date_ending': row['fiscal_date_ending'],
                    'raw_json': json.dumps(raw_data) if raw_data else '{}',
                    'reported_currency': row['reported_currency']
                })
        else:
            # SEC companies (default): query SEC table using COLUMNS instead of raw_json
            # PERFORMANCE FIX: Fetch individual columns and build raw_json format in Python
            # This avoids JSON parsing overhead and leverages column indexes
            query = f"""
                SELECT DISTINCT
                    fiscal_date_ending,
                    reported_currency,
                    -- Assets - Current
                    cash_and_cash_equivalents_at_carrying_value,
                    cash_and_short_term_investments,
                    inventory,
                    current_net_receivables,
                    other_current_assets,
                    total_current_assets,
                    short_term_investments,
                    -- Assets - Non-Current
                    property_plant_equipment,
                    accumulated_depreciation_amortization_ppe,
                    intangible_assets,
                    intangible_assets_excluding_goodwill,
                    goodwill,
                    investments,
                    long_term_investments,
                    other_non_current_assets,
                    total_non_current_assets,
                    -- Assets - Total
                    total_assets,
                    -- Liabilities - Current
                    current_accounts_payable,
                    deferred_revenue,
                    current_debt,
                    current_long_term_debt,
                    short_term_debt,
                    other_current_liabilities,
                    total_current_liabilities,
                    -- Liabilities - Non-Current
                    long_term_debt,
                    long_term_debt_noncurrent,
                    capital_lease_obligations,
                    other_non_current_liabilities,
                    total_non_current_liabilities,
                    short_long_term_debt_total,
                    -- Liabilities - Total
                    total_liabilities,
                    -- Equity
                    common_stock,
                    common_stock_shares_outstanding,
                    retained_earnings,
                    treasury_stock,
                    total_shareholder_equity
                FROM coreiq_av_financials_balance_sheet
                WHERE ticker = :ticker
                  AND report_type = '{period_value}'
                ORDER BY fiscal_date_ending ASC
            """
            rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
            # Build SEC-style results with raw_json format from columns
            results = []
            for row in rows:
                # Parse raw_json for any missing fields
                raw_json_data = {}
                if row.get('raw_json'):
                    try:
                        raw_json_data = json.loads(row['raw_json'])
                    except:
                        pass

                # Build raw_json dict from columns (camelCase keys for consistency)
                raw_data = {
                    'cashAndCashEquivalentsAtCarryingValue': row.get('cash_and_cash_equivalents_at_carrying_value'),
                    'cashAndShortTermInvestments': row.get('cash_and_short_term_investments'),
                    'shortTermInvestments': row.get('short_term_investments'),
                    'inventory': row.get('inventory'),
                    'currentNetReceivables': row.get('current_net_receivables'),
                    'otherCurrentAssets': row.get('other_current_assets'),
                    'totalCurrentAssets': row.get('total_current_assets'),
                    'propertyPlantEquipment': row.get('property_plant_equipment'),
                    'accumulatedDepreciationAmortizationPPE': row.get('accumulated_depreciation_amortization_ppe'),
                    'intangibleAssets': row.get('intangible_assets'),
                    'intangibleAssetsExcludingGoodwill': row.get('intangible_assets_excluding_goodwill'),
                    'goodwill': row.get('goodwill'),
                    'investments': row.get('investments'),
                    'longTermInvestments': row.get('long_term_investments'),
                    'otherNonCurrentAssets': row.get('other_non_current_assets'),
                    'totalNonCurrentAssets': row.get('total_non_current_assets'),
                    'totalAssets': row.get('total_assets'),
                    'currentAccountsPayable': row.get('current_accounts_payable'),
                    'deferredRevenue': row.get('deferred_revenue'),
                    'currentDebt': row.get('current_debt'),
                    'currentLongTermDebt': row.get('current_long_term_debt'),
                    'shortTermDebt': row.get('short_term_debt'),
                    'otherCurrentLiabilities': row.get('other_current_liabilities'),
                    'totalCurrentLiabilities': row.get('total_current_liabilities'),
                    'longTermDebt': row.get('long_term_debt'),
                    'longTermDebtNoncurrent': row.get('long_term_debt_noncurrent'),
                    'capitalLeaseObligations': row.get('capital_lease_obligations'),
                    'otherNonCurrentLiabilities': row.get('other_non_current_liabilities'),
                    'totalNonCurrentLiabilities': row.get('total_non_current_liabilities'),
                    'shortLongTermDebtTotal': row.get('short_long_term_debt_total'),
                    'totalLiabilities': row.get('total_liabilities'),
                    'commonStock': row.get('common_stock'),
                    'commonStockSharesOutstanding': row.get('common_stock_shares_outstanding'),
                    'retainedEarnings': row.get('retained_earnings'),
                    'treasuryStock': row.get('treasury_stock'),
                    'totalShareholderEquity': row.get('total_shareholder_equity'),
                }
                # Remove None values
                raw_data = {k: v for k, v in raw_data.items() if v is not None}
                results.append({
                    'fiscal_date_ending': row['fiscal_date_ending'],
                    'raw_json': json.dumps(raw_data) if raw_data else '{}',
                    'reported_currency': row['reported_currency'] or 'USD'
                })

        elapsed = (time.perf_counter() - t0) * 1000
        # Production-grade logging
        return results

    @staticmethod
    @_log_query_time
    def get_date_range(ticker: str, period_type: str = "annual") -> Tuple[Optional[date], Optional[date]]:
        """Get min and max fiscal dates for a ticker (from cache)."""
        rows = BalanceSheetRepository._fetch_all_annual_rows(ticker, period_type)
        if not rows:
            return None, None
        return rows[0]['fiscal_date_ending'], rows[-1]['fiscal_date_ending']

    @staticmethod
    @_log_query_time
    def get_available_dates(ticker: str, period_type: str = "annual") -> List[date]:
        """Get all available fiscal dates for dropdown (from cache)."""
        rows = BalanceSheetRepository._fetch_all_annual_rows(ticker, period_type)
        return [row['fiscal_date_ending'] for row in rows]

    @staticmethod
    def _parse_raw_json(raw_json: Any) -> Dict[str, Any]:
        """Parse raw_json field from database."""
        if raw_json is None:
            return {}
        if isinstance(raw_json, str):
            try:
                return json.loads(raw_json)
            except json.JSONDecodeError:
                return {}
        if isinstance(raw_json, dict):
            return raw_json
        return {}

    @staticmethod
    def _get_nested_value(data: Dict[str, Any], key: str) -> Optional[float]:
        """Get value from nested dict structure."""
        if not data:
            return None
        if key in data:
            val = data[key]
            if val is not None and val != "None":
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return None
        return None

    @staticmethod
    @_log_query_time
    def get_balance_sheet_data(
        ticker: str,
        start_date: date,
        end_date: date,
        period_type: str = "annual"
    ) -> BalanceSheetData:
        """Get balance sheet data for date range using raw_json (from cache)."""
        import time as _time
        _start = _time.perf_counter()
        company = CompanyRepository.get_company_by_ticker(ticker)
        _company_time = (_time.perf_counter() - _start) * 1000
        if not company:
            raise ValueError(f"Company not found: {ticker}")

        # Filter cached rows by date range — NO extra DB call
        _t0 = _time.perf_counter()
        all_rows = BalanceSheetRepository._fetch_all_annual_rows(ticker, period_type)
        results = [
            row for row in all_rows
            if start_date <= row['fiscal_date_ending'] <= end_date
        ]
        _fetch_time = (_time.perf_counter() - _t0) * 1000

        if not results:
            return BalanceSheetData(
                company=company,
                periods=[],
                line_items=[]
            )

        # Fetch fiscal_year_end for SEC companies (CACHED - ZERO LAG)
        _t0 = _time.perf_counter()
        fiscal_year_end = _get_fiscal_year_end_cached(ticker)
        _fy_time = (_time.perf_counter() - _t0) * 1000

        _t0 = _time.perf_counter()
        periods = [
            FiscalPeriod.from_date(row['fiscal_date_ending'], period_type, fiscal_year_end)
            for row in results
        ]
        _period_time = (_time.perf_counter() - _t0) * 1000

        _t0 = _time.perf_counter()
        json_data_list = [
            BalanceSheetRepository._parse_raw_json(row['raw_json'])
            for row in results
        ]

        line_items = []
        for label, json_key, is_calc, section in BalanceSheetRepository.LINE_ITEMS:
            values = []
            for json_data in json_data_list:
                val = BalanceSheetRepository._get_nested_value(json_data, json_key)
                if val is not None:
                    values.append(val / 1_000_000)
                else:
                    values.append(None)

            if any(v is not None for v in values):
                line_items.append(BalanceSheetLineItem(
                    label=label,
                    key=json_key,
                    values=values,
                    is_calculated=is_calc,
                    section=section
                ))
        _line_item_time = (_time.perf_counter() - _t0) * 1000

        _total_time = (_time.perf_counter() - _start) * 1000
        from utils.server_logger import log_warning as _slw
        _slw("[TIMING] BS_get_data | %.1fms | ticker=%s company=%.1f fetch=%.1f fy=%.1f periods=%.1f line_items=%.1f rows=%d" % (
            _total_time, ticker, _company_time, _fetch_time, _fy_time, _period_time, _line_item_time, len(results)))

        return BalanceSheetData(
            company=company,
            periods=periods,
            line_items=line_items
        )

    @staticmethod
    @_log_query_time
    def get_reported_currency(ticker: str, fiscal_date: date, period_type: str = "annual") -> str:
        """Get the reported currency for a specific fiscal period (from cache)."""
        rows = BalanceSheetRepository._fetch_all_annual_rows(ticker, period_type)
        for row in reversed(rows):
            if row['fiscal_date_ending'] <= fiscal_date and row.get('reported_currency'):
                return row['reported_currency']
        for row in rows:
            if row.get('reported_currency'):
                return row['reported_currency']
        return "USD"

class CashFlowRepository:
    """Repository for coreiq_av_financials_cash_flow table.

    Uses raw_json column for data extraction.
    """

    # Mapping of UI labels to raw_json keys
    # Organized by section: Operating, Investing, Financing
    # IMPORTANT: Totals come AFTER their components (at the bottom)
    LINE_ITEMS = [
        # OPERATING ACTIVITIES
        ("Net Income", "netIncome", False, "operating"),
        ("Depreciation & Amortization", "depreciationDepletionAndAmortization", False, "operating"),
        ("Deferred Tax", "deferredIncomeTax", False, "operating"),
        ("Stock-Based Compensation", "stockBasedCompensation", False, "operating"),
        ("Change in Working Capital", "changeInWorkingCapital", False, "operating"),
        ("Accounts Receivable", "changeInReceivables", False, "operating"),
        ("Inventory", "changeInInventory", False, "operating"),
        ("Accounts Payable", "changeInPayables", False, "operating"),
        ("Other Operating Activities", "changeInOtherOperatingAssets", False, "operating"),
        ("Operating Cash Flow", "operatingCashflow", False, "operating"),

        # INVESTING ACTIVITIES
        ("Capital Expenditures", "capitalExpenditures", False, "investing"),
        ("Acquisitions", "acquisitions", False, "investing"),
        ("Purchases of Investments", "purchaseOfInvestment", False, "investing"),
        ("Sales/Maturities of Investments", "saleOfInvestment", False, "investing"),
        ("Other Investing Activities", "otherCashflowFromInvestment", False, "investing"),
        ("Investing Cash Flow", "cashflowFromInvestment", False, "investing"),

        # FINANCING ACTIVITIES
        ("Debt Repayment", "debtRepayment", False, "financing"),
        ("Common Stock Issued", "commonStockIssued", False, "financing"),
        ("Common Stock Repurchased", "commonStockRepurchased", False, "financing"),
        ("Dividends Paid", "dividendsPaid", False, "financing"),
        ("Other Financing Activities", "otherCashflowFromFinancing", False, "financing"),
        ("Financing Cash Flow", "cashflowFromFinancing", False, "financing"),

        # SUMMARY
        ("Effect of Forex Changes", "exchangeRateChanges", False, "summary"),
        ("Net Change in Cash", "netChangeInCash", False, "summary"),
        ("Cash at Beginning of Period", "cashAtBeginningOfPeriod", False, "summary"),
        ("Cash at End of Period", "cashAtEndOfPeriod", False, "summary"),
    ]

    # ── Cached single-query: fetches ALL rows for a ticker in one go ──
    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_all_annual_rows(ticker: str, period_type: str = "annual") -> List[Dict[str, Any]]:
        """Fetch all cash flow rows for a ticker (cached 5 min).

        This single query replaces get_date_range, get_available_dates,
        get_reported_currency, AND get_cash_flow_data — cutting
        4 network round-trips down to 1 (or 0 on cache hit).

        Supports both SEC and YFinance companies.

        Args:
            ticker: Company ticker symbol
            period_type: 'annual' or 'quarterly' (default: 'annual')
        """
        t0 = time.perf_counter()

        # Map period_type to database values
        period_value = period_type.lower()

        # Detect company source
        from data.source_router import get_company_source
        source = get_company_source(ticker)
        # Composite tickers (e.g. 'ADS.DE') use yf_symbol; plain YF tickers use ticker column
        _yf_is_composite = '.' in ticker
        _yf_base = ticker.split('.')[0] if _yf_is_composite else ticker

        if source == 'YFinance':
            # YF companies: query YF table with pivot, convert to SEC-style raw_json format
            # Get currency from YF company overview first (overview table uses base ticker)
            currency_query = """
                SELECT payload_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
            currency_result = db_manager.execute_query_readonly(currency_query, {"ticker": _yf_base})
            yf_currency = 'USD'
            if currency_result and currency_result[0].get('payload_json'):
                try:
                    payload = json.loads(currency_result[0]['payload_json'])
                    # Support both nested {"info": {...}} and flat {"currency": ...} structures
                    info = payload.get('info', payload)
                    yf_currency = info.get('financialCurrency') or info.get('currency') or 'USD'
                except:
                    yf_currency = 'USD'

            _yf_col = "yf_symbol" if _yf_is_composite else "ticker"
            query = f"""
                SELECT
                    period_end as fiscal_date_ending,
                    MAX(CASE WHEN line_item = 'Net Income' THEN value END) as netIncome,
                    MAX(CASE WHEN line_item = 'Depreciation Amortization Depletion' THEN value END) as depreciationDepletionAndAmortization,
                    MAX(CASE WHEN line_item = 'Deferred Tax' THEN value END) as deferredIncomeTax,
                    MAX(CASE WHEN line_item = 'Stock Based Compensation' THEN value END) as stockBasedCompensation,
                    MAX(CASE WHEN line_item = 'Change In Working Capital' THEN value END) as changeInWorkingCapital,
                    MAX(CASE WHEN line_item = 'Change In Receivables' THEN value END) as changeInReceivables,
                    MAX(CASE WHEN line_item = 'Change In Inventory' THEN value END) as changeInInventory,
                    MAX(CASE WHEN line_item = 'Change In Payables And Accrued Expense' THEN value END) as changeInPayables,
                    MAX(CASE WHEN line_item = 'Change In Other Current Assets' THEN value END) as changeInOtherOperatingAssets,
                    MAX(CASE WHEN line_item = 'Operating Cash Flow' THEN value END) as operatingCashflow,
                    MAX(CASE WHEN line_item = 'Capital Expenditure' THEN value END) as capitalExpenditures,
                    MAX(CASE WHEN line_item = 'Net Business Purchase And Sale' THEN value END) as acquisitions,
                    MAX(CASE WHEN line_item = 'Net Investment Purchase And Sale' THEN value END) as purchaseOfInvestment,
                    MAX(CASE WHEN line_item = 'Net Investment Purchase And Sale' THEN value END) as saleOfInvestment,
                    MAX(CASE WHEN line_item = 'Other Investing Activities' THEN value END) as otherCashflowFromInvestment,
                    MAX(CASE WHEN line_item = 'Investing Cash Flow' THEN value END) as cashflowFromInvestment,
                    MAX(CASE WHEN line_item = 'Net Long Term Debt Issuance' THEN value END) as debtRepayment,
                    MAX(CASE WHEN line_item = 'Net Common Stock Issuance' THEN value END) as commonStockIssued,
                    MAX(CASE WHEN line_item = 'Net Common Stock Issuance' THEN value END) as commonStockRepurchased,
                    MAX(CASE WHEN line_item = 'Cash Dividends Paid' THEN value END) as dividendsPaid,
                    MAX(CASE WHEN line_item = 'Other Financing Activities' THEN value END) as otherCashflowFromFinancing,
                    MAX(CASE WHEN line_item = 'Financing Cash Flow' THEN value END) as cashflowFromFinancing,
                    MAX(CASE WHEN line_item = 'Foreign Exchange Cash Flow Adjustments' THEN value END) as exchangeRateChanges,
                    MAX(CASE WHEN line_item = 'Changes In Cash' THEN value END) as netChangeInCash,
                    '{yf_currency}' as reported_currency
                FROM coreiq_yf_financials_cash_flow
                WHERE {_yf_col} = :ticker
                  AND frequency = '{period_value}'
                GROUP BY period_end
                ORDER BY period_end ASC
            """
            rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
            # Convert to SEC-style format with raw_json
            results = []
            for row in rows:
                # Build raw_json dict from columns
                raw_data = {
                    'netIncome': row.get('netIncome'),
                    'depreciationDepletionAndAmortization': row.get('depreciationDepletionAndAmortization'),
                    'deferredIncomeTax': row.get('deferredIncomeTax'),
                    'stockBasedCompensation': row.get('stockBasedCompensation'),
                    'changeInWorkingCapital': row.get('changeInWorkingCapital'),
                    'changeInReceivables': row.get('changeInReceivables'),
                    'changeInInventory': row.get('changeInInventory'),
                    'changeInPayables': row.get('changeInPayables'),
                    'changeInOtherOperatingAssets': row.get('changeInOtherOperatingAssets'),
                    'operatingCashflow': row.get('operatingCashflow'),
                    'capitalExpenditures': row.get('capitalExpenditures'),
                    'acquisitions': row.get('acquisitions'),
                    'purchaseOfInvestment': row.get('purchaseOfInvestment'),
                    'saleOfInvestment': row.get('saleOfInvestment'),
                    'otherCashflowFromInvestment': row.get('otherCashflowFromInvestment'),
                    'cashflowFromInvestment': row.get('cashflowFromInvestment'),
                    'debtRepayment': row.get('debtRepayment'),
                    'commonStockIssued': row.get('commonStockIssued'),
                    'commonStockRepurchased': row.get('commonStockRepurchased'),
                    'dividendsPaid': row.get('dividendsPaid'),
                    'otherCashflowFromFinancing': row.get('otherCashflowFromFinancing'),
                    'cashflowFromFinancing': row.get('cashflowFromFinancing'),
                    'exchangeRateChanges': row.get('exchangeRateChanges'),
                    'netChangeInCash': row.get('netChangeInCash'),
                }
                # Remove None values
                raw_data = {k: v for k, v in raw_data.items() if v is not None}
                results.append({
                    'fiscal_date_ending': row['fiscal_date_ending'],
                    'raw_json': json.dumps(raw_data) if raw_data else '{}',
                    'reported_currency': row['reported_currency']
                })
        else:
            # SEC companies (default): query SEC table using COLUMNS instead of raw_json
            # PERFORMANCE FIX: Fetch individual columns and build raw_json format in Python
            # NOTE: Only querying columns that exist in the database schema
            query = f"""
                SELECT DISTINCT
                    fiscal_date_ending,
                    reported_currency,
                    -- Operating Activities (all 11 fields)
                    net_income,
                    profit_loss,
                    depreciation_depletion_and_amortization,
                    change_in_receivables,
                    change_in_inventory,
                    change_in_operating_assets,
                    change_in_operating_liabilities,
                    payments_for_operating_activities,
                    proceeds_from_operating_activities,
                    operating_cashflow,
                    -- Investing Activities (3 fields)
                    capital_expenditures,
                    cashflow_from_investment,
                    cashflow_from_financing,
                    -- Financing Activities (11 fields)
                    dividend_payout,
                    dividend_payout_common_stock,
                    dividend_payout_preferred_stock,
                    payments_for_repurchase_of_common_stock,
                    payments_for_repurchase_of_equity,
                    payments_for_repurchase_of_preferred_stock,
                    proceeds_from_issuance_of_common_stock,
                    proceeds_from_issuance_of_preferred_stock,
                    proceeds_from_repurchase_of_equity,
                    proceeds_from_repayments_of_short_term_debt,
                    proceeds_from_sale_of_treasury_stock,
                    proceeds_from_lt_debt_and_capital_net,
                    -- Summary (2 fields)
                    change_in_cash_and_cash_equivalents,
                    change_in_exchange_rate
                FROM coreiq_av_financials_cash_flow
                WHERE ticker = :ticker
                  AND report_type = '{period_value}'
                ORDER BY fiscal_date_ending ASC
            """
            rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
            # Build SEC-style results with raw_json format from columns
            results = []
            for row in rows:
                # Build raw_json dict from columns (all 29 fields mapped)
                raw_data = {
                    'netIncome': row.get('net_income'),
                    'profitLoss': row.get('profit_loss'),
                    'depreciationDepletionAndAmortization': row.get('depreciation_depletion_and_amortization'),
                    'changeInReceivables': row.get('change_in_receivables'),
                    'changeInInventory': row.get('change_in_inventory'),
                    'changeInOperatingAssets': row.get('change_in_operating_assets'),
                    'changeInOperatingLiabilities': row.get('change_in_operating_liabilities'),
                    'paymentsForOperatingActivities': row.get('payments_for_operating_activities'),
                    'proceedsFromOperatingActivities': row.get('proceeds_from_operating_activities'),
                    'operatingCashflow': row.get('operating_cashflow'),
                    'capitalExpenditures': row.get('capital_expenditures'),
                    'cashflowFromInvestment': row.get('cashflow_from_investment'),
                    'cashflowFromFinancing': row.get('cashflow_from_financing'),
                    'dividendPayout': row.get('dividend_payout'),
                    'dividendPayoutCommonStock': row.get('dividend_payout_common_stock'),
                    'dividendPayoutPreferredStock': row.get('dividend_payout_preferred_stock'),
                    'paymentsForRepurchaseOfCommonStock': row.get('payments_for_repurchase_of_common_stock'),
                    'paymentsForRepurchaseOfEquity': row.get('payments_for_repurchase_of_equity'),
                    'paymentsForRepurchaseOfPreferredStock': row.get('payments_for_repurchase_of_preferred_stock'),
                    'proceedsFromIssuanceOfCommonStock': row.get('proceeds_from_issuance_of_common_stock'),
                    'proceedsFromIssuanceOfPreferredStock': row.get('proceeds_from_issuance_of_preferred_stock'),
                    'proceedsFromRepurchaseOfEquity': row.get('proceeds_from_repurchase_of_equity'),
                    'proceedsFromRepaymentsOfShortTermDebt': row.get('proceeds_from_repayments_of_short_term_debt'),
                    'proceedsFromSaleOfTreasuryStock': row.get('proceeds_from_sale_of_treasury_stock'),
                    'proceedsFromIssuanceOfLongTermDebtAndCapitalSecuritiesNet': row.get('proceeds_from_lt_debt_and_capital_net'),
                    'changeInCashAndCashEquivalents': row.get('change_in_cash_and_cash_equivalents'),
                    'changeInExchangeRate': row.get('change_in_exchange_rate'),
                }
                # Remove None values
                raw_data = {k: v for k, v in raw_data.items() if v is not None}
                results.append({
                    'fiscal_date_ending': row['fiscal_date_ending'],
                    'raw_json': json.dumps(raw_data) if raw_data else '{}',
                    'reported_currency': row['reported_currency'] or 'USD'
                })

        elapsed = (time.perf_counter() - t0) * 1000
        # Production-grade logging
        return results

    @staticmethod
    @_log_query_time
    def get_date_range(ticker: str, period_type: str = "annual") -> Tuple[Optional[date], Optional[date]]:
        """Get min and max fiscal dates for a ticker (from cache)."""
        rows = CashFlowRepository._fetch_all_annual_rows(ticker, period_type)
        if not rows:
            return None, None
        return rows[0]['fiscal_date_ending'], rows[-1]['fiscal_date_ending']

    @staticmethod
    @_log_query_time
    def get_available_dates(ticker: str, period_type: str = "annual") -> List[date]:
        """Get all available fiscal dates for dropdown (from cache)."""
        rows = CashFlowRepository._fetch_all_annual_rows(ticker, period_type)
        return [row['fiscal_date_ending'] for row in rows]

    @staticmethod
    def _parse_raw_json(raw_json: Any) -> Dict[str, Any]:
        """Parse raw_json field from database."""
        if raw_json is None:
            return {}
        if isinstance(raw_json, str):
            try:
                return json.loads(raw_json)
            except json.JSONDecodeError:
                return {}
        if isinstance(raw_json, dict):
            return raw_json
        return {}

    @staticmethod
    def _get_nested_value(data: Dict[str, Any], key: str) -> Optional[float]:
        """Get value from nested dict structure."""
        if not data:
            return None
        if key in data:
            val = data[key]
            if val is not None and val != "None":
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return None
        return None

    @staticmethod
    @_log_query_time
    def get_cash_flow_data(
        ticker: str,
        start_date: date,
        end_date: date,
        period_type: str = "annual"
    ) -> CashFlowData:
        """Get cash flow data for date range using raw_json (from cache)."""
        import time as _time
        _start = _time.perf_counter()
        company = CompanyRepository.get_company_by_ticker(ticker)
        _company_time = (_time.perf_counter() - _start) * 1000
        if not company:
            raise ValueError(f"Company not found: {ticker}")

        # Filter cached rows by date range — NO extra DB call
        _t0 = _time.perf_counter()
        all_rows = CashFlowRepository._fetch_all_annual_rows(ticker, period_type)
        results = [
            row for row in all_rows
            if start_date <= row['fiscal_date_ending'] <= end_date
        ]
        _fetch_time = (_time.perf_counter() - _t0) * 1000

        if not results:
            return CashFlowData(
                company=company,
                periods=[],
                line_items=[]
            )

        # Fetch fiscal_year_end for SEC companies (CACHED - ZERO LAG)
        _t0 = _time.perf_counter()
        fiscal_year_end = _get_fiscal_year_end_cached(ticker)
        _fy_time = (_time.perf_counter() - _t0) * 1000

        _t0 = _time.perf_counter()
        periods = [
            FiscalPeriod.from_date(row['fiscal_date_ending'], period_type, fiscal_year_end)
            for row in results
        ]
        _period_time = (_time.perf_counter() - _t0) * 1000

        _t0 = _time.perf_counter()
        json_data_list = [
            CashFlowRepository._parse_raw_json(row['raw_json'])
            for row in results
        ]

        line_items = []
        for label, json_key, is_calc, section in CashFlowRepository.LINE_ITEMS:
            values = []
            for json_data in json_data_list:
                val = CashFlowRepository._get_nested_value(json_data, json_key)
                if val is not None:
                    values.append(val / 1_000_000)
                else:
                    values.append(None)

            if any(v is not None for v in values):
                line_items.append(CashFlowLineItem(
                    label=label,
                    key=json_key,
                    values=values,
                    is_calculated=is_calc,
                    section=section
                ))
        _line_item_time = (_time.perf_counter() - _t0) * 1000

        _total_time = (_time.perf_counter() - _start) * 1000
        from utils.server_logger import log_warning as _slw
        _slw("[TIMING] CF_get_data | %.1fms | ticker=%s company=%.1f fetch=%.1f fy=%.1f periods=%.1f line_items=%.1f rows=%d" % (
            _total_time, ticker, _company_time, _fetch_time, _fy_time, _period_time, _line_item_time, len(results)))

        return CashFlowData(
            company=company,
            periods=periods,
            line_items=line_items
        )

    @staticmethod
    @_log_query_time
    def get_reported_currency(ticker: str, fiscal_date: date, period_type: str = "annual") -> str:
        """Get the reported currency for a specific fiscal period (from cache)."""
        rows = CashFlowRepository._fetch_all_annual_rows(ticker, period_type)
        for row in reversed(rows):
            if row['fiscal_date_ending'] <= fiscal_date and row.get('reported_currency'):
                return row['reported_currency']
        for row in rows:
            if row.get('reported_currency'):
                return row['reported_currency']
        return "USD"

class KeyStatsRepository:
    """Repository for Key Stats data combining multiple tables.

    Combines data from:
    - coreiq_av_financials_income_statement (revenue, ebitda, ebit, net income)
    - coreiq_av_financials_balance_sheet (cash, debt, equity for TEV calculations)
    - coreiq_av_company_overview (market cap, share price, eps)
    """

    @staticmethod
    def get_date_range(ticker: str, period_type: str = "annual") -> Tuple[Optional[date], Optional[date]]:
        """Get min and max fiscal dates — reuses IncomeStatement cache (0 extra DB queries)."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        if not rows:
            return None, None
        dates = [r['fiscal_date_ending'] for r in rows if r.get('fiscal_date_ending')]
        if not dates:
            return None, None
        return min(dates), max(dates)

    @staticmethod
    def get_available_dates(ticker: str, period_type: str = "annual") -> List[date]:
        """Get all available fiscal dates — reuses IncomeStatement cache (0 extra DB queries)."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        return sorted([r['fiscal_date_ending'] for r in rows if r.get('fiscal_date_ending')])

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    @_log_query_time
    def get_key_stats_data(
        ticker: str,
        start_date: date,
        end_date: date,
        period_type: str = "annual"
    ) -> Dict[str, Any]:
        """Get key stats data for date range (cached 5 min).

        Returns a dictionary with:
        - periods: List of FiscalPeriod
        - line_items: List of dict with label and values
        - reported_currency: str

        Supports both SEC and YFinance companies.

        Args:
            ticker: Company ticker symbol
            start_date: Start date for filtering
            end_date: End date for filtering
            period_type: 'annual' or 'quarterly' (default: 'annual')
        """
        import time as _time
        _start = _time.perf_counter()
        from data.models import FiscalPeriod
        from data.source_router import get_company_source

        source = get_company_source(ticker)
        # Composite tickers (e.g. 'ADS.DE') use yf_symbol; plain YF tickers use ticker column
        _yf_is_composite = '.' in ticker
        _yf_base_ticker = ticker.split('.')[0] if _yf_is_composite else ticker
        _yf_col = "yf_symbol" if _yf_is_composite else "ticker"

        # Map period_type to database values
        period_value = period_type.lower()

        if source == 'YFinance':
            # YF companies: Query YF tables with pivot
            # Get currency from YF company overview first (overview table uses base ticker)
            currency_query = """
                SELECT payload_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
            currency_result = db_manager.execute_query_readonly(currency_query, {"ticker": _yf_base_ticker})
            yf_currency = 'USD'
            if currency_result and currency_result[0].get('payload_json'):
                try:
                    payload = json.loads(currency_result[0]['payload_json'])
                    # Support both nested {"info": {...}} and flat {"currency": ...} structures
                    info = payload.get('info', payload)
                    yf_currency = info.get('financialCurrency') or info.get('currency') or 'USD'
                except:
                    yf_currency = 'USD'

            query = f"""
                SELECT
                    period_end as fiscal_date_ending,
                    MAX(CASE WHEN line_item = 'Total Revenue' THEN value END) as total_revenue,
                    MAX(CASE WHEN line_item = 'Gross Profit' THEN value END) as gross_profit,
                    MAX(CASE WHEN line_item = 'EBITDA' THEN value END) as ebitda,
                    MAX(CASE WHEN line_item = 'EBIT' THEN value END) as ebit,
                    MAX(CASE WHEN line_item = 'Net Income From Continuing Operations' THEN value END) as net_income_from_continuing_operations,
                    MAX(CASE WHEN line_item = 'Net Income' THEN value END) as net_income,
                    '{yf_currency}' as reported_currency,
                    '{{}}' as raw_json
                FROM coreiq_yf_financials_income_statement
                WHERE {_yf_col} = :ticker
                  AND frequency = '{period_value}'
                  AND period_end BETWEEN :start_date AND :end_date
                GROUP BY period_end
                ORDER BY period_end ASC
            """
        else:
            # SEC companies: Query SEC tables
            query = f"""
                SELECT DISTINCT
                    i.fiscal_date_ending,
                    i.total_revenue,
                    i.gross_profit,
                    i.ebitda,
                    i.ebit,
                    i.net_income_from_continuing_operations,
                    i.net_income,
                    i.reported_currency,
                    i.raw_json
                FROM coreiq_av_financials_income_statement i
                WHERE i.ticker = :ticker
                  AND i.fiscal_date_ending BETWEEN :start_date AND :end_date
                  AND i.report_type = '{period_value}'
                ORDER BY i.fiscal_date_ending ASC
            """

        # For YF: financial tables use full ticker (via yf_symbol or ticker col as per _yf_col above)
        # SEC: use plain ticker
        _query_ticker = ticker if source == 'YFinance' else ticker
        results = db_manager.execute_query_readonly(query, {
            "ticker": _query_ticker,
            "start_date": start_date,
            "end_date": end_date
        })

        if not results:
            return {"periods": [], "line_items": [], "reported_currency": "USD"}

        # Get company overview for latest EPS and Market Cap
        if source == 'YFinance':
            # YF: Get from YF company overview (data is in payload_json; overview uses base ticker)
            overview_query = """
                SELECT
                    payload_json as overview_raw_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
        else:
            # SEC: Get from SEC company overview
            overview_query = """
                SELECT
                    market_capitalization,
                    eps,
                    pe_ratio,
                    raw_json as overview_raw_json
                FROM coreiq_av_company_overview
                WHERE ticker = :ticker
                ORDER BY fetched_at_utc DESC
                LIMIT 1
            """

        _ov_q_ticker = _yf_base_ticker if source == 'YFinance' else ticker
        overview_results = db_manager.execute_query_readonly(overview_query, {"ticker": _ov_q_ticker})
        overview = overview_results[0] if overview_results else {}

        # Parse overview raw_json
        overview_raw = {}
        if overview and overview.get('overview_raw_json'):
            try:
                overview_raw = json.loads(overview['overview_raw_json'])
                # YF: payload_json has nested 'info' structure
                if source == 'YFinance' and 'info' in overview_raw:
                    overview_raw = overview_raw['info']
            except (json.JSONDecodeError, TypeError):
                overview_raw = {}

        # Get shares outstanding from overview
        shares_outstanding = None
        if overview_raw.get('SharesOutstanding'):
            try:
                shares_outstanding = float(overview_raw['SharesOutstanding'])
            except (ValueError, TypeError):
                shares_outstanding = None
        elif source == 'YFinance' and overview_raw.get('sharesOutstanding'):
            # YF uses camelCase
            try:
                shares_outstanding = float(overview_raw['sharesOutstanding'])
            except (ValueError, TypeError):
                shares_outstanding = None

        # Get latest balance sheet for TEV calculation
        if source == 'YFinance':
            # YF: Get from YF balance sheet (use yf_symbol for composite tickers)
            bs_query = f"""
                SELECT
                    MAX(CASE WHEN line_item = 'Cash And Cash Equivalents' THEN value END) as cashAndCashEquivalentsAtCarryingValue,
                    MAX(CASE WHEN line_item = 'Total Debt' THEN value END) as shortLongTermDebtTotal,
                    MAX(CASE WHEN line_item = 'Stockholders Equity' THEN value END) as totalShareholderEquity,
                    period_end as fiscal_date_ending
                FROM coreiq_yf_financials_balance_sheet
                WHERE {_yf_col} = :ticker
                  AND frequency = 'annual'
                GROUP BY period_end
                ORDER BY period_end DESC
                LIMIT 1
            """
        else:
            # SEC: Get from SEC balance sheet
            bs_query = """
                SELECT
                    raw_json as bs_raw_json,
                    fiscal_date_ending
                FROM coreiq_av_financials_balance_sheet
                WHERE ticker = :ticker
              AND report_type = 'annual'
            ORDER BY fiscal_date_ending DESC
            LIMIT 1
        """
        _bs_q_ticker = ticker if source == 'YFinance' else ticker
        bs_results = db_manager.execute_query_readonly(bs_query, {"ticker": _bs_q_ticker})
        latest_bs = bs_results[0] if bs_results else {}

        if source == 'YFinance':
            # YF: Direct column values
            cash_and_st_investments = None
            if latest_bs.get('cashAndCashEquivalentsAtCarryingValue'):
                try:
                    cash_and_st_investments = float(latest_bs['cashAndCashEquivalentsAtCarryingValue'])
                except (ValueError, TypeError):
                    cash_and_st_investments = None

            total_debt = None
            if latest_bs.get('shortLongTermDebtTotal'):
                try:
                    total_debt = float(latest_bs['shortLongTermDebtTotal'])
                except (ValueError, TypeError):
                    total_debt = None

            total_shareholder_equity = None
            if latest_bs.get('totalShareholderEquity'):
                try:
                    total_shareholder_equity = float(latest_bs['totalShareholderEquity'])
                except (ValueError, TypeError):
                    total_shareholder_equity = None
        else:
            # SEC: Parse balance sheet raw_json
            bs_raw = {}
            if latest_bs and latest_bs.get('bs_raw_json'):
                try:
                    bs_raw = json.loads(latest_bs['bs_raw_json'])
                except (json.JSONDecodeError, TypeError):
                    bs_raw = {}

            # Extract cash and debt from balance sheet
            cash_and_st_investments = None
            if bs_raw.get('cashAndShortTermInvestments'):
                try:
                    cash_and_st_investments = float(bs_raw['cashAndShortTermInvestments'])
                except (ValueError, TypeError):
                    cash_and_st_investments = None

            total_debt = None
            if bs_raw.get('shortLongTermDebtTotal'):
                try:
                    total_debt = float(bs_raw['shortLongTermDebtTotal'])
                except (ValueError, TypeError):
                    total_debt = None

            total_shareholder_equity = None
            if bs_raw.get('totalShareholderEquity'):
                try:
                    total_shareholder_equity = float(bs_raw['totalShareholderEquity'])
                except (ValueError, TypeError):
                    total_shareholder_equity = None

        # Get market cap from overview (in millions for consistency)
        market_cap = None
        if source == 'YFinance':
            # YF: From payload_json
            if overview_raw.get('marketCap'):
                try:
                    market_cap = float(overview_raw['marketCap'])
                except (ValueError, TypeError):
                    market_cap = None
        elif overview and overview.get('market_capitalization'):
            # SEC: From direct column
            try:
                market_cap = float(overview['market_capitalization'])
            except (ValueError, TypeError):
                market_cap = None

        # Create periods (with FQ for SEC companies - CACHED)
        _t0 = _time.perf_counter()
        fiscal_year_end = _get_fiscal_year_end_cached(ticker)
        _fy_time = (_time.perf_counter() - _t0) * 1000

        periods = [FiscalPeriod.from_date(row['fiscal_date_ending'], period_type, fiscal_year_end) for row in results]

        # Build line items
        line_items = []

        # Helper to safely get float value
        def safe_float_val(val):
            if val is None or val == 'None':
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        # Helper to convert to millions
        def to_millions(val):
            if val is None:
                return None
            return val / 1_000_000

        # Get reported currency from first row
        reported_currency = results[0]['reported_currency'] if results else 'USD'

        # 1. Total Revenue
        total_revenue_vals = [to_millions(safe_float_val(row['total_revenue'])) for row in results]
        line_items.append({"label": "Total Revenue", "metric_key": "total_revenue", "values": total_revenue_vals, "is_bold": True, "indent": 0})

        # 2. Growth Over Prior Year (calculated)
        # Quarterly: compare same calendar quarter of prior year (YoY); Annual: sequential
        growth_vals = []
        if period_type.lower() == "quarterly":
            _rev_by_date = {row['fiscal_date_ending']: safe_float_val(row['total_revenue']) for row in results}
            for row in results:
                curr = safe_float_val(row['total_revenue'])
                dt = row['fiscal_date_ending']
                try:
                    prior_dt = dt.replace(year=dt.year - 1)
                except ValueError:
                    prior_dt = dt.replace(year=dt.year - 1, day=28)
                prev = _rev_by_date.get(prior_dt)
                if curr is not None and prev is not None and prev != 0:
                    growth_vals.append(((curr - prev) / abs(prev)) * 100)
                else:
                    growth_vals.append(None)
        else:
            for i, row in enumerate(results):
                curr = safe_float_val(row['total_revenue'])
                if i > 0:
                    prev = safe_float_val(results[i-1]['total_revenue'])
                    if curr is not None and prev is not None and prev != 0:
                        growth_vals.append(((curr - prev) / abs(prev)) * 100)
                    else:
                        growth_vals.append(None)
                else:
                    growth_vals.append(None)
        line_items.append({"label": "Growth Over Prior Year", "metric_key": "revenue_growth_yoy", "values": growth_vals, "is_bold": False, "indent": 1, "is_percent": True})

        # 3. Gross Profit
        gross_profit_vals = [to_millions(safe_float_val(row['gross_profit'])) for row in results]
        line_items.append({"label": "Gross Profit", "metric_key": "gross_profit", "values": gross_profit_vals, "is_bold": True, "indent": 0})

        # 5. Margin % (calculated from raw_json for accuracy)
        gp_margin_vals = []
        for row in results:
            raw = json.loads(row['raw_json']) if row['raw_json'] else {}
            gp = safe_float_val(row['gross_profit'])
            tr = safe_float_val(row['total_revenue'])
            if gp is not None and tr is not None and tr != 0:
                gp_margin_vals.append((gp / tr) * 100)
            else:
                gp_margin_vals.append(None)
        line_items.append({"label": "Margin %", "metric_key": "gross_profit_margin", "values": gp_margin_vals, "is_bold": False, "indent": 1, "is_percent": True})

        # 4. EBITDA
        ebitda_vals = [to_millions(safe_float_val(row['ebitda'])) for row in results]
        line_items.append({"label": "EBITDA", "metric_key": "ebitda", "values": ebitda_vals, "is_bold": True, "indent": 0})

        # 8. EBITDA Margin %
        ebitda_margin_vals = []
        for row in results:
            ebitda = safe_float_val(row['ebitda'])
            tr = safe_float_val(row['total_revenue'])
            if ebitda is not None and tr is not None and tr != 0:
                ebitda_margin_vals.append((ebitda / tr) * 100)
            else:
                ebitda_margin_vals.append(None)
        line_items.append({"label": "Margin %", "metric_key": "ebitda_margin", "values": ebitda_margin_vals, "is_bold": False, "indent": 1, "is_percent": True})

        # 5. EBIT
        ebit_vals = [to_millions(safe_float_val(row['ebit'])) for row in results]
        line_items.append({"label": "EBIT", "metric_key": "ebit", "values": ebit_vals, "is_bold": True, "indent": 0})

        # 11. EBIT Margin %
        ebit_margin_vals = []
        for row in results:
            ebit = safe_float_val(row['ebit'])
            tr = safe_float_val(row['total_revenue'])
            if ebit is not None and tr is not None and tr != 0:
                ebit_margin_vals.append((ebit / tr) * 100)
            else:
                ebit_margin_vals.append(None)
        line_items.append({"label": "Margin %", "metric_key": "ebit_margin", "values": ebit_margin_vals, "is_bold": False, "indent": 1, "is_percent": True})

        # 6. Earnings from Cont. Ops
        cont_ops_vals = [to_millions(safe_float_val(row['net_income_from_continuing_operations'])) for row in results]
        line_items.append({"label": "Earnings from Cont. Ops.", "metric_key": "cont_ops_income", "values": cont_ops_vals, "is_bold": True, "indent": 0})

        # 14. Cont Ops Margin %
        cont_ops_margin_vals = []
        for row in results:
            cont_ops = safe_float_val(row['net_income_from_continuing_operations'])
            tr = safe_float_val(row['total_revenue'])
            if cont_ops is not None and tr is not None and tr != 0:
                cont_ops_margin_vals.append((cont_ops / tr) * 100)
            else:
                cont_ops_margin_vals.append(None)
        line_items.append({"label": "Margin %", "metric_key": "cont_ops_margin", "values": cont_ops_margin_vals, "is_bold": False, "indent": 1, "is_percent": True})

        # 7. Net Income
        net_income_vals = [to_millions(safe_float_val(row['net_income'])) for row in results]
        line_items.append({"label": "Net Income", "metric_key": "net_income", "values": net_income_vals, "is_bold": True, "indent": 0})

        # 17. Net Income Margin %
        ni_margin_vals = []
        for row in results:
            ni = safe_float_val(row['net_income'])
            tr = safe_float_val(row['total_revenue'])
            if ni is not None and tr is not None and tr != 0:
                ni_margin_vals.append((ni / tr) * 100)
            else:
                ni_margin_vals.append(None)
        line_items.append({"label": "Margin %", "metric_key": "net_income_margin", "values": ni_margin_vals, "is_bold": False, "indent": 1, "is_percent": True})

        # 8. Diluted EPS - from raw_json
        eps_vals = []
        for row in results:
            raw = json.loads(row['raw_json']) if row['raw_json'] else {}
            # Try to get diluted EPS from raw_json if available
            eps = safe_float_val(raw.get('dilutedEPS') or raw.get('dilutedEps'))
            if eps is None and row['net_income'] and shares_outstanding:
                # Calculate if not available
                ni = safe_float_val(row['net_income'])
                if ni is not None and shares_outstanding > 0:
                    eps = ni / shares_outstanding
            eps_vals.append(eps)
        line_items.append({"label": "Diluted EPS Excl. Extra Items", "metric_key": "diluted_eps_excl_extra_items", "values": eps_vals, "is_bold": True, "indent": 0})

        # 9. EPS Growth Over Prior Year
        # Quarterly: compare same calendar quarter of prior year; Annual: sequential
        eps_growth_vals = []
        if period_type.lower() == "quarterly":
            _eps_by_date = {}
            for row, eps in zip(results, eps_vals):
                _eps_by_date[row['fiscal_date_ending']] = eps
            for row, eps in zip(results, eps_vals):
                dt = row['fiscal_date_ending']
                try:
                    prior_dt = dt.replace(year=dt.year - 1)
                except ValueError:
                    prior_dt = dt.replace(year=dt.year - 1, day=28)
                prev_eps = _eps_by_date.get(prior_dt)
                if eps is not None and prev_eps is not None and prev_eps != 0:
                    eps_growth_vals.append(((eps - prev_eps) / abs(prev_eps)) * 100)
                else:
                    eps_growth_vals.append(None)
        else:
            for i, eps in enumerate(eps_vals):
                if i > 0 and eps is not None and eps_vals[i-1] is not None and eps_vals[i-1] != 0:
                    eps_growth_vals.append(((eps - eps_vals[i-1]) / abs(eps_vals[i-1])) * 100)
                else:
                    eps_growth_vals.append(None)
        line_items.append({"label": "Growth Over Prior Year", "metric_key": "eps_growth_yoy", "values": eps_growth_vals, "is_bold": False, "indent": 1, "is_percent": True, "has_grey_sep": True})

        # ── Forward E- analyst estimates ────────────────────────────────────
        _last_actual_hist_date = periods[-1].date if periods else None  # capture before E- appends
        if source != 'YFinance' and periods:
            _last_hist_date = _last_actual_hist_date
            _last_rev_mm = total_revenue_vals[-1] if total_revenue_vals else None
            _last_eps = eps_vals[-1] if eps_vals else None

            _hor_clause = (
                "LOWER(horizon) IN ('fiscal year', 'next fiscal year')"
                if period_type == 'annual'
                else "LOWER(horizon) IN ('fiscal quarter', 'next fiscal quarter')"
            )
            _est_rows = db_manager.execute_query_readonly(
                f"SELECT estimate_date, eps_est_avg, rev_est_avg "
                f"FROM coreiq_av_financials_earnings_estimates "
                f"WHERE ticker = :ticker AND estimate_date > :last_date AND {_hor_clause} "
                f"ORDER BY estimate_date ASC",
                {"ticker": ticker, "last_date": _last_hist_date},
            )
            for _er in _est_rows:
                if _er.get('eps_est_avg') is None and _er.get('rev_est_avg') is None:
                    continue
                _edt = _er['estimate_date']
                if hasattr(_edt, 'date'): _edt = _edt.date()
                _rev_mm = (float(_er['rev_est_avg']) / 1_000_000) if _er.get('rev_est_avg') is not None else None
                _eps_e  = float(_er['eps_est_avg']) if _er.get('eps_est_avg') is not None else None

                _fp_e = FiscalPeriod.from_date(_edt, period_type, fiscal_year_end)
                periods.append(FiscalPeriod(date=_fp_e.date, label=_fp_e.label, is_estimated=True,
                                            calendar_quarter=_fp_e.calendar_quarter, fiscal_quarter=_fp_e.fiscal_quarter))

                _rev_growth = ((_rev_mm - _last_rev_mm) / abs(_last_rev_mm) * 100) if (_rev_mm is not None and _last_rev_mm not in (None, 0)) else None
                _eps_growth = ((_eps_e  - _last_eps)  / abs(_last_eps)  * 100) if (_eps_e  is not None and _last_eps  not in (None, 0)) else None

                # Identify the two "Growth Over Prior Year" rows (revenue = first occurrence, EPS = last)
                _growth_rows = [l for l in line_items if l['label'] == 'Growth Over Prior Year' and l.get('indent') == 1]
                _rev_growth_row = _growth_rows[0] if len(_growth_rows) >= 1 else None
                _eps_growth_row = _growth_rows[-1] if len(_growth_rows) >= 2 else None

                for _li in line_items:
                    _llbl = _li['label']
                    if _llbl == 'Total Revenue':
                        _li['values'].append(_rev_mm)
                    elif _llbl == 'Diluted EPS Excl. Extra Items':
                        _li['values'].append(_eps_e)
                    elif _li is _rev_growth_row:
                        _li['values'].append(_rev_growth)
                    elif _li is _eps_growth_row:
                        _li['values'].append(_eps_growth)
                    else:
                        _li['values'].append(None)

                if _rev_mm is not None: _last_rev_mm = _rev_mm
                if _eps_e  is not None: _last_eps   = _eps_e

        # ── Forward E- analyst estimates (YFinance companies) ───────────────
        elif source == 'YFinance' and periods:
            import calendar as _cal
            _last_rev_mm = total_revenue_vals[-1] if total_revenue_vals else None
            _last_eps    = eps_vals[-1]           if eps_vals           else None

            # Helper: add n quarters to a date (keeps fiscal month/day pattern)
            def _add_quarters(d, n):
                m = d.month + n * 3
                y = d.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                day = min(d.day, _cal.monthrange(y, m)[1])
                return date(y, m, day)

            if period_type == 'annual':
                # '0y' = current fiscal year being estimated, '+1y' = following year
                _pl_map = [('0y', 1), ('+1y', 2)]  # period_label → year offset from last actual
                _period_len_label = "12 Months"
            else:
                # quarterly: '0q' = +1 quarter, '+1q' = +2 quarters
                _pl_map = [('0q', 1), ('+1q', 2)]
                _period_len_label = "3 Months"

            # Fetch avg revenue and EPS estimates for relevant period labels
            _pl_keys = [p[0] for p in _pl_map]
            _pl_in   = ", ".join(f"'{p}'" for p in _pl_keys)
            _yf_est_rows = db_manager.execute_query_readonly(
                f"SELECT estimate_type, period_label, value "
                f"FROM coreiq_yf_financials_earnings_estimates "
                f"WHERE ticker = :t "
                f"  AND estimate_type IN ('revenue_estimate', 'earnings_estimate') "
                f"  AND metric = 'avg' "
                f"  AND period_label IN ({_pl_in}) "
                f"ORDER BY estimate_type, period_label",
                {"t": _yf_base_ticker},
            )
            # Build lookup: (estimate_type, period_label) → value
            _yf_est_map = {}
            for _yr in _yf_est_rows:
                _yf_est_map[(_yr['estimate_type'], _yr['period_label'])] = _yr['value']

            _growth_rows = [l for l in line_items if l['label'] == 'Growth Over Prior Year' and l.get('indent') == 1]
            _rev_growth_row = _growth_rows[0]  if len(_growth_rows) >= 1 else None
            _eps_growth_row = _growth_rows[-1] if len(_growth_rows) >= 2 else None

            for _pl, _offset in _pl_map:
                _rev_raw = _yf_est_map.get(('revenue_estimate', _pl))
                _eps_e   = _yf_est_map.get(('earnings_estimate', _pl))

                # Skip if no useful data
                _rev_mm = (float(_rev_raw) / 1_000_000) if (_rev_raw is not None and _rev_raw != 0) else None
                _eps_e  = float(_eps_e) if (_eps_e is not None and _eps_e != 0) else None
                if _rev_mm is None and _eps_e is None:
                    continue

                # Compute fiscal period end date from last actual
                if period_type == 'annual':
                    try:
                        _edt = _last_actual_hist_date.replace(year=_last_actual_hist_date.year + _offset)
                    except ValueError:
                        _edt = _last_actual_hist_date.replace(year=_last_actual_hist_date.year + _offset, day=28)
                else:
                    _edt = _add_quarters(_last_actual_hist_date, _offset)

                _fp_e = FiscalPeriod.from_date(_edt, period_type, fiscal_year_end)
                periods.append(FiscalPeriod(date=_fp_e.date, label=_fp_e.label, is_estimated=True,
                                            calendar_quarter=_fp_e.calendar_quarter, fiscal_quarter=_fp_e.fiscal_quarter))

                _rev_growth = ((_rev_mm - _last_rev_mm) / abs(_last_rev_mm) * 100) if (_rev_mm is not None and _last_rev_mm not in (None, 0)) else None
                _eps_growth = ((_eps_e  - _last_eps)   / abs(_last_eps)  * 100) if (_eps_e  is not None and _last_eps  not in (None, 0)) else None

                for _li in line_items:
                    _llbl = _li['label']
                    if _llbl == 'Total Revenue':
                        _li['values'].append(_rev_mm)
                    elif _llbl == 'Diluted EPS Excl. Extra Items':
                        _li['values'].append(_eps_e)
                    elif _li is _rev_growth_row:
                        _li['values'].append(_rev_growth)
                    elif _li is _eps_growth_row:
                        _li['values'].append(_eps_growth)
                    else:
                        _li['values'].append(None)

                if _rev_mm is not None: _last_rev_mm = _rev_mm
                if _eps_e  is not None: _last_eps    = _eps_e

        # ── Forward F- model revenue forecast (annual only, next 5 years) ──
        if period_type == 'annual' and periods:
            _last_hist_date = _last_actual_hist_date  # use original last actual, not E- appended date
            _n_actual = len(results)  # use results (historical-only); total_revenue_vals is mutated by E-estimate appends
            _last_rev_mm = next(
                (l['values'][_n_actual - 1] for l in line_items if l['label'] == 'Total Revenue' and len(l['values']) >= _n_actual),
                None
            )
            # Build composite forecast ticker: coreiq_model_forecasts stores 'ADS.DE' not 'ADS'.
            # If ticker is already composite (has '.') use as-is; otherwise look up exchange_acronym.
            if source == 'YFinance' and '.' not in ticker:
                _exch = (CompanyRepository.get_companies_map().get(ticker) or {}).get('exchange_acronym')
                _forecast_ticker = f"{ticker}.{_exch}" if _exch else ticker
            else:
                _forecast_ticker = ticker

            _fcst_rows = db_manager.execute_query_readonly(
                "SELECT fiscal_year, forecast_date, value_millions FROM coreiq_model_forecasts "
                "WHERE ticker = :ticker AND metric = 'total_revenue' AND model_key = 'ensemble' "
                "AND fiscal_year > YEAR(:last_date) "
                "ORDER BY fiscal_year ASC LIMIT 5",
                {"ticker": _forecast_ticker, "last_date": _last_hist_date},
            )
            _f_growth_rows = [l for l in line_items if l['label'] == 'Growth Over Prior Year' and l.get('indent') == 1]
            _f_rev_growth_row = _f_growth_rows[0] if _f_growth_rows else None
            for _fr in _fcst_rows:
                _fy  = int(_fr['fiscal_year'])
                _rvm = float(_fr['value_millions']) if _fr.get('value_millions') is not None else None
                # Use stored fiscal period end date (same month/day as actuals, year incremented)
                _fdt = _fr.get('forecast_date') or date(_fy, 12, 31)

                periods.append(FiscalPeriod(
                    date=_fdt,
                    label=f"12 Months\n{_fdt.strftime('%b-%d-%Y')}",
                    is_forecast=True,
                ))
                _rev_growth = ((_rvm - _last_rev_mm) / abs(_last_rev_mm) * 100) if (_rvm is not None and _last_rev_mm not in (None, 0)) else None

                for _li in line_items:
                    if _li['label'] == 'Total Revenue':
                        _li['values'].append(_rvm)
                    elif _li is _f_rev_growth_row:
                        _li['values'].append(_rev_growth)
                    else:
                        _li['values'].append(None)

                if _rvm is not None: _last_rev_mm = _rvm

        _total_time = (_time.perf_counter() - _start) * 1000
        from utils.server_logger import log_warning as _slw
        _slw("[TIMING] KS_get_data | %.1fms | ticker=%s source=%s periods=%d line_items=%d fy=%.1f" % (
            _total_time, ticker, source, len(periods), len(line_items), _fy_time))

        return {
            "periods": periods,
            "line_items": line_items,
            "reported_currency": reported_currency,
            # Additional data for capitalization section
            "market_cap": to_millions(market_cap),
            "cash": to_millions(cash_and_st_investments),
            "total_debt": to_millions(total_debt),
            "total_equity": to_millions(total_shareholder_equity),
            "latest_eps": overview.get('eps'),
            "latest_pe": overview.get('pe_ratio')
        }

    @staticmethod
    def extract_metric_value(data: Dict[str, Any], metric_key: str, period_index: int) -> Optional[float]:
        """Return tab-equivalent metric value for a period index from get_key_stats_data output."""
        if period_index is None or period_index < 0:
            return None
        for item in data.get("line_items") or []:
            if item.get("metric_key") == metric_key:
                values = item.get("values") or []
                if period_index < len(values):
                    return values[period_index]
        return None

    @staticmethod
    @_log_query_time
    def get_estimated_data(ticker: str, end_date: date) -> List[Dict[str, Any]]:
        """Fetch ALL annual analyst estimates whose estimate_date > end_date.

        Only fiscal-year horizons (not quarterly) — returns list, may be empty.
        Each dict: estimate_date (date), rev_avg (float|None in USD), eps_avg (float|None).
        """
        query = """
            SELECT estimate_date, eps_est_avg, rev_est_avg
            FROM coreiq_av_financials_earnings_estimates
            WHERE ticker = :ticker
              AND estimate_date > :end_date
              -- Include all fiscal YEAR horizons, excluding historical + quarterly.
              AND LOWER(horizon) LIKE '%year%'
              AND LOWER(horizon) NOT IN ('historical fiscal year', 'fiscal quarter', 'next fiscal quarter')
            ORDER BY estimate_date ASC
        """
        results = db_manager.execute_query_readonly(query, {"ticker": ticker, "end_date": end_date})
        estimates = []
        for row in results:
            if row["eps_est_avg"] is None and row["rev_est_avg"] is None:
                continue
            est_date = row["estimate_date"]
            if hasattr(est_date, "date"):
                est_date = est_date.date()
            estimates.append({
                "estimate_date": est_date,
                "eps_avg": float(row["eps_est_avg"]) if row["eps_est_avg"] is not None else None,
                "rev_avg": float(row["rev_est_avg"]) if row["rev_est_avg"] is not None else None,
            })
        return estimates

    @staticmethod
    @_log_query_time
    def get_quarterly_estimated_data(ticker: str, end_date: date) -> List[Dict[str, Any]]:
        """Fetch quarterly analyst estimates whose estimate_date > end_date.

        Uses 'fiscal quarter' and 'next fiscal quarter' horizons.
        Each dict: estimate_date (date), rev_avg (float|None in USD), eps_avg (float|None).
        """
        query = """
            SELECT estimate_date, eps_est_avg, rev_est_avg
            FROM coreiq_av_financials_earnings_estimates
            WHERE ticker = :ticker
              AND estimate_date > :end_date
              AND horizon IN ('fiscal quarter', 'next fiscal quarter')
            ORDER BY estimate_date ASC
        """
        results = db_manager.execute_query_readonly(query, {"ticker": ticker, "end_date": end_date})
        estimates = []
        for row in results:
            if row["eps_est_avg"] is None and row["rev_est_avg"] is None:
                continue
            est_date = row["estimate_date"]
            if hasattr(est_date, "date"):
                est_date = est_date.date()
            estimates.append({
                "estimate_date": est_date,
                "eps_avg": float(row["eps_est_avg"]) if row["eps_est_avg"] is not None else None,
                "rev_avg": float(row["rev_est_avg"]) if row["rev_est_avg"] is not None else None,
            })
        return estimates

    @staticmethod
    @_log_query_time
    def get_reported_currency(ticker: str, fiscal_date: date, period_type: str = "annual") -> str:
        """Get reported currency — reuses IncomeStatement cache (0 extra DB queries)."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        for row in rows:
            if row.get('fiscal_date_ending') == fiscal_date and row.get('reported_currency'):
                return row['reported_currency']
        return "USD"

class AnalystEstimatesRepository:
    """Analyst consensus estimates from AV (coreiq_av_financials_earnings_estimates)
    and YF (coreiq_yf_financials_earnings_estimates)."""

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_date_range(ticker: str, period_type: str = "annual") -> Tuple[Optional[date], Optional[date]]:
        """Min/max estimate_date for AV companies; (None, None) for YF."""
        from data.source_router import get_company_source
        source = get_company_source(ticker)
        if source == 'YFinance':
            return None, None
        horizon_clause = "LOWER(horizon) LIKE '%year%'" if period_type in ("annual", "Annual") else "LOWER(horizon) LIKE '%quarter%'"
        query = f"""
            SELECT MIN(estimate_date) AS min_date, MAX(estimate_date) AS max_date
            FROM coreiq_av_financials_earnings_estimates
            WHERE ticker = :ticker AND {horizon_clause}
        """
        rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
        if rows and rows[0]["min_date"] and rows[0]["max_date"]:
            mn, mx = rows[0]["min_date"], rows[0]["max_date"]
            if hasattr(mn, "date"): mn = mn.date()
            if hasattr(mx, "date"): mx = mx.date()
            return mn, mx
        return None, None

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_available_dates(ticker: str, period_type: str = "annual") -> List[date]:
        """Distinct estimate_dates for AV; [] for YF."""
        from data.source_router import get_company_source
        source = get_company_source(ticker)
        if source == 'YFinance':
            return []
        horizon_clause = "LOWER(horizon) LIKE '%year%'" if period_type in ("annual", "Annual") else "LOWER(horizon) LIKE '%quarter%'"
        query = f"""
            SELECT DISTINCT estimate_date
            FROM coreiq_av_financials_earnings_estimates
            WHERE ticker = :ticker AND {horizon_clause}
            ORDER BY estimate_date ASC
        """
        rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
        dates = []
        for r in rows:
            dt = r["estimate_date"]
            if hasattr(dt, "date"): dt = dt.date()
            if dt: dates.append(dt)
        return dates

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_estimates_data(ticker: str, start_date, end_date, period_type: str = "annual") -> Dict[str, Any]:
        """Return structured estimates for the Estimates tab.

        Dict keys: periods, sections, reported_currency, source.
        """
        from data.source_router import get_company_source
        source = get_company_source(ticker)
        if source == 'YFinance':
            return AnalystEstimatesRepository._get_yf_estimates(ticker, period_type)
        return AnalystEstimatesRepository._get_av_estimates(ticker, start_date, end_date, period_type)

    # ── private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _get_av_estimates(ticker: str, start_date, end_date, period_type: str) -> Dict[str, Any]:
        horizon_clause = "LOWER(horizon) LIKE '%year%'" if period_type in ("annual", "Annual") else "LOWER(horizon) LIKE '%quarter%'"
        period_label_mm = "12 Months" if period_type in ("annual", "Annual") else "3 Months"

        params: Dict[str, Any] = {"ticker": ticker}
        date_clause = ""
        if start_date is not None and end_date is not None:
            date_clause = "AND estimate_date BETWEEN :start_date AND :end_date"
            params["start_date"] = start_date
            params["end_date"] = end_date

        query = f"""
            SELECT estimate_date, horizon,
                   eps_est_avg, eps_est_high, eps_est_low, eps_est_analyst_count,
                   eps_est_avg_7d_ago, eps_est_avg_30d_ago, eps_est_avg_60d_ago, eps_est_avg_90d_ago,
                   eps_rev_up_7d, eps_rev_down_7d, eps_rev_up_30d, eps_rev_down_30d,
                   rev_est_avg, rev_est_high, rev_est_low, rev_est_analyst_count,
                   fetched_at_utc
            FROM coreiq_av_financials_earnings_estimates
            WHERE ticker = :ticker
              {date_clause}
              AND {horizon_clause}
            ORDER BY estimate_date ASC, fetched_at_utc DESC
        """
        rows = db_manager.execute_query_readonly(query, params)
        if not rows:
            return {"periods": [], "sections": [], "reported_currency": "USD", "source": "AV"}

        # Deduplicate: one row per estimate_date (latest fetched_at_utc)
        seen: Dict[date, Any] = {}
        ordered: List[date] = []
        for row in rows:
            dt = row["estimate_date"]
            if hasattr(dt, "date"): dt = dt.date()
            if dt not in seen:
                seen[dt] = row
                ordered.append(dt)

        _HORIZON_SHORT = {
            "next fiscal year": "Next FY",
            "fiscal year": "Fiscal Year",
            "historical fiscal year": "Historical FY",
            "next fiscal quarter": "Next FQ",
            "fiscal quarter": "Fiscal Quarter",
            "historical fiscal quarter": "Historical FQ",
        }

        periods = []
        for dt in ordered:
            periods.append({
                "date": dt,
                "label": f"{period_label_mm}\n{dt.strftime('%b-%d-%Y')}",
            })

        def gv(row, col):
            v = row.get(col)
            return float(v) if v is not None else None

        def rev_mm(row, col):
            v = gv(row, col)
            return v / 1_000_000 if v is not None else None

        def build_rows(row_defs):
            out = []
            for rd in row_defs:
                vals = []
                for dt in ordered:
                    r = seen[dt]
                    fn = rd.get("fn")
                    vals.append(fn(r) if fn else gv(r, rd["col"]))
                out.append({
                    "label": rd["label"], "values": vals,
                    "is_header": False, "is_bold": False, "has_grey_sep": False,
                    "is_currency": rd.get("is_currency", False),
                    "is_eps": rd.get("is_eps", False),
                    "is_count": rd.get("is_count", False),
                    "is_percent": rd.get("is_percent", False),
                    "indent": 1,
                })
            return out

        def section_header(title):
            return {"label": title, "values": [None] * len(ordered),
                    "is_header": True, "is_bold": True, "has_grey_sep": True,
                    "is_currency": False, "is_eps": False, "is_count": False,
                    "is_percent": False, "indent": 0}

        rev_rows = build_rows([
            {"label": "Average",          "fn": lambda r: rev_mm(r, "rev_est_avg"), "is_currency": True},
            {"label": "High",             "fn": lambda r: rev_mm(r, "rev_est_high"), "is_currency": True},
            {"label": "Low",              "fn": lambda r: rev_mm(r, "rev_est_low"),  "is_currency": True},
            {"label": "No. of Analysts",  "col": "rev_est_analyst_count", "is_count": True},
        ])
        eps_rows = build_rows([
            {"label": "Average",          "col": "eps_est_avg",          "is_eps": True},
            {"label": "High",             "col": "eps_est_high",         "is_eps": True},
            {"label": "Low",              "col": "eps_est_low",          "is_eps": True},
            {"label": "No. of Analysts",  "col": "eps_est_analyst_count","is_count": True},
        ])
        trend_rows = build_rows([
            {"label": "Current",    "col": "eps_est_avg",       "is_eps": True},
            {"label": "7 Days Ago", "col": "eps_est_avg_7d_ago","is_eps": True},
            {"label": "30 Days Ago","col": "eps_est_avg_30d_ago","is_eps": True},
            {"label": "60 Days Ago","col": "eps_est_avg_60d_ago","is_eps": True},
            {"label": "90 Days Ago","col": "eps_est_avg_90d_ago","is_eps": True},
        ])
        rev_updown = build_rows([
            {"label": "Up (Last 7 Days)",   "col": "eps_rev_up_7d",   "is_count": True},
            {"label": "Down (Last 7 Days)", "col": "eps_rev_down_7d", "is_count": True},
            {"label": "Up (Last 30 Days)",  "col": "eps_rev_up_30d",  "is_count": True},
            {"label": "Down (Last 30 Days)","col": "eps_rev_down_30d","is_count": True},
        ])

        sections = [
            [section_header("Revenue Estimates")] + rev_rows,
            [section_header("EPS Estimates")] + eps_rows,
            [section_header("EPS Trend")] + trend_rows,
            [section_header("EPS Revisions")] + rev_updown,
        ]
        return {"periods": periods, "sections": sections, "reported_currency": "USD", "source": "AV"}

    @staticmethod
    def _get_yf_estimates(ticker: str, period_type: str) -> Dict[str, Any]:
        import calendar as _cal
        from data.models import FiscalPeriod
        # earnings estimates table uses base ticker (no yf_symbol column)
        _est_ticker = ticker.split('.')[0] if '.' in ticker else ticker
        _yf_is_composite = '.' in ticker
        _yf_col = "yf_symbol" if _yf_is_composite else "ticker"

        if period_type in ("annual", "Annual"):
            period_labels = ["0y", "+1y", "LTG"]
            _fallback_display = {"0y": "Current Year", "+1y": "Next Year", "LTG": "Long-Term Growth"}
            period_in = "('0y', '+1y', 'LTG')"
            _pl_offsets = {"0y": 1, "+1y": 2}
        else:
            period_labels = ["0q", "+1q"]
            _fallback_display = {"0q": "Current Quarter", "+1q": "Next Quarter"}
            period_in = "('0q', '+1q')"
            _pl_offsets = {"0q": 1, "+1q": 2}

        run_row = db_manager.execute_query_readonly(
            "SELECT run_id FROM coreiq_yf_financials_earnings_estimates WHERE ticker = :ticker ORDER BY ingested_at DESC LIMIT 1",
            {"ticker": _est_ticker}
        )
        if not run_row:
            return {"periods": [], "sections": [], "reported_currency": "USD", "source": "YF"}
        latest_run = run_row[0]["run_id"]

        rows = db_manager.execute_query_readonly(f"""
            SELECT estimate_type, period_label, metric, value
            FROM coreiq_yf_financials_earnings_estimates
            WHERE ticker = :ticker AND run_id = :run_id AND period_label IN {period_in}
            ORDER BY estimate_type, period_label, metric
        """, {"ticker": _est_ticker, "run_id": latest_run})

        data: Dict[str, Dict] = {}
        for r in rows:
            et, pl, m, v = r["estimate_type"], r["period_label"], r["metric"], r["value"]
            data.setdefault(et, {}).setdefault(pl, {})[m] = v

        # Compute date-based column labels (same logic as Key Stats estimate columns)
        _last_actual_date = None
        try:
            _freq = 'annual' if period_type in ("annual", "Annual") else 'quarterly'
            _act_rows = db_manager.execute_query_readonly(
                f"SELECT MAX(period_end) AS last_date FROM coreiq_yf_financials_income_statement "
                f"WHERE {_yf_col} = :t AND frequency = :freq",
                {"t": ticker, "freq": _freq}
            )
            if _act_rows and _act_rows[0].get("last_date"):
                _last_actual_date = _act_rows[0]["last_date"]
                if hasattr(_last_actual_date, "date"):
                    _last_actual_date = _last_actual_date.date()
        except Exception:
            pass

        _fiscal_ye = _get_fiscal_year_end_cached(ticker)

        def _add_quarters(d, n):
            m = d.month + n * 3
            y = d.year + (m - 1) // 12
            m = (m - 1) % 12 + 1
            day = min(d.day, _cal.monthrange(y, m)[1])
            return date(y, m, day)

        def _label_for(pl: str) -> str:
            if pl == "LTG":
                return "Long-Term Growth"
            if _last_actual_date is None:
                return _fallback_display.get(pl, pl)
            offset = _pl_offsets.get(pl, 1)
            try:
                if period_type in ("annual", "Annual"):
                    try:
                        _edt = _last_actual_date.replace(year=_last_actual_date.year + offset)
                    except ValueError:
                        _edt = _last_actual_date.replace(year=_last_actual_date.year + offset, day=28)
                else:
                    _edt = _add_quarters(_last_actual_date, offset)
                _fp = FiscalPeriod.from_date(_edt, period_type, _fiscal_ye)
                return _fp.label
            except Exception:
                return _fallback_display.get(pl, pl)

        available_pls = [pl for pl in period_labels if pl in _fallback_display]
        periods = [{"date": None, "label": _label_for(pl)} for pl in available_pls]

        def gv(et, pl, metric):
            v = data.get(et, {}).get(pl, {}).get(metric)
            return float(v) if v is not None else None

        def build_rows_yf(et, row_defs):
            out = []
            for rd in row_defs:
                vals = [gv(et, pl, rd["metric"]) for pl in available_pls]
                if rd.get("multiply_1e6"):
                    vals = [v / 1_000_000 if v is not None else None for v in vals]
                if rd.get("multiply_100"):
                    vals = [v * 100 if v is not None else None for v in vals]
                out.append({
                    "label": rd["label"], "values": vals,
                    "is_header": False, "is_bold": False, "has_grey_sep": False,
                    "is_currency": rd.get("is_currency", False),
                    "is_eps": rd.get("is_eps", False),
                    "is_count": rd.get("is_count", False),
                    "is_percent": rd.get("is_percent", False),
                    "indent": 1,
                })
            return out

        def sec_hdr(title):
            return {"label": title, "values": [None] * len(available_pls),
                    "is_header": True, "is_bold": True, "has_grey_sep": True,
                    "is_currency": False, "is_eps": False, "is_count": False,
                    "is_percent": False, "indent": 0}

        rev_rows = build_rows_yf("revenue_estimate", [
            {"label": "Average",          "metric": "avg",             "multiply_1e6": True, "is_currency": True},
            {"label": "High",             "metric": "high",            "multiply_1e6": True, "is_currency": True},
            {"label": "Low",              "metric": "low",             "multiply_1e6": True, "is_currency": True},
            {"label": "No. of Analysts",  "metric": "numberOfAnalysts","is_count": True},
            {"label": "Year-Ago Revenue", "metric": "yearAgoRevenue",  "multiply_1e6": True, "is_currency": True},
            {"label": "Revenue Growth",   "metric": "growth",          "multiply_100": True, "is_percent": True},
        ])
        eps_rows = build_rows_yf("earnings_estimate", [
            {"label": "Average",          "metric": "avg",             "is_eps": True},
            {"label": "High",             "metric": "high",            "is_eps": True},
            {"label": "Low",             "metric": "low",             "is_eps": True},
            {"label": "No. of Analysts",  "metric": "numberOfAnalysts","is_count": True},
            {"label": "Year-Ago EPS",     "metric": "yearAgoEps",      "is_eps": True},
            {"label": "EPS Growth",       "metric": "growth",          "multiply_100": True, "is_percent": True},
        ])
        trend_rows = build_rows_yf("eps_trend", [
            {"label": "Current",    "metric": "current",   "is_eps": True},
            {"label": "7 Days Ago", "metric": "7daysAgo",  "is_eps": True},
            {"label": "30 Days Ago","metric": "30daysAgo", "is_eps": True},
            {"label": "60 Days Ago","metric": "60daysAgo", "is_eps": True},
            {"label": "90 Days Ago","metric": "90daysAgo", "is_eps": True},
        ])
        rev_rows2 = build_rows_yf("eps_revisions", [
            {"label": "Up (Last 7 Days)",   "metric": "upLast7days",   "is_count": True},
            {"label": "Down (Last 7 Days)", "metric": "downLast7Days", "is_count": True},
            {"label": "Up (Last 30 Days)",  "metric": "upLast30days",  "is_count": True},
            {"label": "Down (Last 30 Days)","metric": "downLast30days","is_count": True},
        ])
        growth_rows = build_rows_yf("growth_estimates", [
            {"label": "Stock Growth", "metric": "stockTrend", "multiply_100": True, "is_percent": True},
            {"label": "Index Growth", "metric": "indexTrend", "multiply_100": True, "is_percent": True},
        ])

        sections = [
            [sec_hdr("Revenue Estimate")] + rev_rows,
            [sec_hdr("EPS Estimate")] + eps_rows,
            [sec_hdr("EPS Trend")] + trend_rows,
            [sec_hdr("EPS Revisions")] + rev_rows2,
            [sec_hdr("Growth Estimates")] + growth_rows,
        ]
        return {"periods": periods, "sections": sections, "reported_currency": "USD", "source": "YF"}


class ModelForecastsRepository:
    """Revenue model forecasts from coreiq_model_forecasts."""

    _MODEL_LABELS = {
        "ensemble":          "Ensemble (Best-3 Blend)",
        "linear":            "Linear Regression",
        "cagr":              "CAGR",
        "exp_smoothing":     "Exponential Smoothing",
        "holt":              "Holt's Linear Trend",
        "ma_trend":          "Moving Average Trend",
        "weighted_avg":      "Weighted Average Growth",
    }
    _MODEL_ORDER = ["ensemble", "linear", "cagr", "exp_smoothing", "holt", "ma_trend", "weighted_avg"]

    _SCENARIO_LABELS = {
        "scenario_pessimistic": "Pessimistic",
        "scenario_baseline":    "Baseline",
        "scenario_optimistic":  "Optimistic",
    }
    _SCENARIO_ORDER = ["scenario_pessimistic", "scenario_baseline", "scenario_optimistic"]

    @staticmethod
    def _resolve_forecast_ticker(ticker: str) -> str:
        """Resolve composite ticker for YF companies (DB stores 'ADS.DE', URL sends 'ADS')."""
        if '.' in ticker:
            return ticker
        _company = CompanyRepository.get_companies_map().get(ticker) or {}
        if _company.get('source') == 'YFinance':
            _exch = _company.get('exchange_acronym')
            if _exch:
                return f"{ticker}.{_exch}"
        return ticker

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_date_range(ticker: str) -> Tuple[Optional[date], Optional[date]]:
        ticker = ModelForecastsRepository._resolve_forecast_ticker(ticker)
        rows = db_manager.execute_query_readonly(
            "SELECT MIN(fiscal_year) AS mn, MAX(fiscal_year) AS mx FROM coreiq_model_forecasts WHERE ticker = :ticker",
            {"ticker": ticker}
        )
        if rows and rows[0]["mn"] and rows[0]["mx"]:
            return date(int(rows[0]["mn"]), 1, 1), date(int(rows[0]["mx"]), 12, 31)
        return None, None

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_available_dates(ticker: str) -> List[date]:
        ticker = ModelForecastsRepository._resolve_forecast_ticker(ticker)
        rows = db_manager.execute_query_readonly(
            "SELECT DISTINCT fiscal_year FROM coreiq_model_forecasts WHERE ticker = :ticker ORDER BY fiscal_year ASC",
            {"ticker": ticker}
        )
        return [date(int(r["fiscal_year"]), 1, 1) for r in rows]

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_forecasts_data(ticker: str, start_year: Optional[int] = None, end_year: Optional[int] = None) -> Dict[str, Any]:
        """Return forecast data; sections has one section (Revenue Forecast) with one row per model."""
        ticker = ModelForecastsRepository._resolve_forecast_ticker(ticker)
        params: Dict[str, Any] = {"ticker": ticker}
        fy_filter = ""
        if start_year:
            fy_filter += " AND fiscal_year >= :start_fy"
            params["start_fy"] = start_year
        if end_year:
            fy_filter += " AND fiscal_year <= :end_fy"
            params["end_fy"] = end_year

        rows = db_manager.execute_query_readonly(f"""
            SELECT fiscal_year, model_key, value_millions, is_best_model, is_ensemble, mape, last_actual_date
            FROM coreiq_model_forecasts
            WHERE ticker = :ticker AND metric = 'total_revenue' {fy_filter}
            ORDER BY fiscal_year ASC, model_key ASC
        """, params)

        if not rows:
            return {"periods": [], "sections": [], "reported_currency": "USD", "last_actual_date": None, "historical_rows": []}

        fiscal_years = sorted({int(r["fiscal_year"]) for r in rows})
        by_fy: Dict[int, Dict[str, Any]] = {}
        last_actual_date = None
        for r in rows:
            fy = int(r["fiscal_year"])
            mk = r["model_key"]
            by_fy.setdefault(fy, {})[mk] = r
            if r.get("last_actual_date") and last_actual_date is None:
                last_actual_date = r["last_actual_date"]
                if hasattr(last_actual_date, "date"): last_actual_date = last_actual_date.date()

        periods = [{"date": date(fy, 1, 1), "label": str(fy), "fiscal_year": fy} for fy in fiscal_years]

        available_models = [mk for mk in ModelForecastsRepository._MODEL_ORDER
                            if any(by_fy.get(fy, {}).get(mk) for fy in fiscal_years)]
        available_scenarios = [mk for mk in ModelForecastsRepository._SCENARIO_ORDER
                               if any(by_fy.get(fy, {}).get(mk) for fy in fiscal_years)]

        def _build_rows(keys, labels, ensemble_key="ensemble"):
            rows_out = []
            for mk in keys:
                lbl = labels.get(mk, mk)
                vals = []
                for fy in fiscal_years:
                    rd = by_fy.get(fy, {}).get(mk)
                    vals.append(float(rd["value_millions"]) if rd and rd.get("value_millions") is not None else None)
                rows_out.append({
                    "label": lbl, "values": vals,
                    "is_header": False, "is_bold": (mk == ensemble_key), "has_grey_sep": False,
                    "is_currency": True, "is_eps": False, "is_count": False, "is_percent": False,
                    "model_key": mk, "indent": 0 if mk == ensemble_key else 1,
                })
            return rows_out

        model_rows = _build_rows(available_models, ModelForecastsRepository._MODEL_LABELS)
        scenario_rows = _build_rows(available_scenarios, ModelForecastsRepository._SCENARIO_LABELS, ensemble_key="")

        section_hdr = {"label": "Total Revenue Forecast", "values": [None] * len(fiscal_years),
                       "is_header": True, "is_bold": True, "has_grey_sep": True,
                       "is_currency": False, "is_eps": False, "is_count": False, "is_percent": False, "indent": 0}
        scenario_hdr = {"label": "Scenario Analysis", "values": [None] * len(fiscal_years),
                        "is_header": True, "is_bold": True, "has_grey_sep": True,
                        "is_currency": False, "is_eps": False, "is_count": False, "is_percent": False, "indent": 0}

        sections = [[section_hdr] + model_rows]
        if scenario_rows:
            sections.append([scenario_hdr] + scenario_rows)

        # Fetch historical revenue for chart (millions)
        hist_rows: List[Dict[str, Any]] = []
        try:
            _sec = db_manager.execute_query_readonly("""
                SELECT YEAR(fiscal_date_ending) AS yr, total_revenue
                FROM coreiq_av_financials_income_statement
                WHERE ticker = :ticker AND report_type = 'annual' AND total_revenue IS NOT NULL
                ORDER BY fiscal_date_ending ASC
            """, {"ticker": ticker})
            if _sec:
                hist_rows = [{"year": int(r["yr"]), "value_millions": float(r["total_revenue"]) / 1_000_000} for r in _sec if r.get("yr") and r.get("total_revenue") is not None]
            else:
                _yf = db_manager.execute_query_readonly("""
                    SELECT YEAR(period_end) AS yr,
                           MAX(CASE WHEN line_item = 'Total Revenue' THEN value END) AS total_revenue
                    FROM coreiq_yf_financials_income_statement
                    WHERE ticker = :ticker AND frequency = 'annual'
                    GROUP BY period_end
                    ORDER BY period_end ASC
                """, {"ticker": ticker})
                hist_rows = [{"year": int(r["yr"]), "value_millions": float(r["total_revenue"]) / 1_000_000} for r in _yf if r.get("yr") and r.get("total_revenue") is not None]
        except Exception:
            pass

        return {
            "periods": periods,
            "sections": sections,
            "reported_currency": "USD",
            "last_actual_date": last_actual_date,
            "historical_rows": hist_rows,
            "model_keys": available_models,
            "scenario_keys": available_scenarios,
            "by_fy": by_fy,
        }


class RatiosRepository:
    """Repository for derived financial ratios.

    Design goals:
    - Additive only: no changes to existing statement repositories.
    - Minimal round-trips: reuse existing cached statement fetches.
    - Safe math: returns None when numerator/denominator inputs are missing.
    """

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_date_range(ticker: str, period_type: str = "annual") -> Tuple[Optional[date], Optional[date]]:
        """Get min and max fiscal dates from cached income rows."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        if not rows:
            return None, None
        dates = [r['fiscal_date_ending'] for r in rows if r.get('fiscal_date_ending')]
        if not dates:
            return None, None
        return min(dates), max(dates)

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_available_dates(ticker: str, period_type: str = "annual") -> List[date]:
        """Get all fiscal dates from cached income rows."""
        rows = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        return sorted([r['fiscal_date_ending'] for r in rows if r.get('fiscal_date_ending')])

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    @_log_query_time
    def get_ratios_data(
        ticker: str,
        start_date: date,
        end_date: date,
        period_type: str = "annual"
    ) -> Dict[str, Any]:
        """Compute ratio payload for Market Data Ratios tab.

        Output mirrors Key Stats shape:
            {
                "periods": [...],
                "line_items": [...],
                "reported_currency": "USD",
                "notes": [...]
            }
        """
        import time as _time
        _start = _time.perf_counter()
        from data.models import FiscalPeriod

        def _safe_float(value: Any) -> Optional[float]:
            if value is None or value == "None":
                return None
            try:
                return float(value)
            except (ValueError, TypeError):
                return None

        def _parse_json(raw_json: Any) -> Dict[str, Any]:
            if raw_json is None:
                return {}
            if isinstance(raw_json, dict):
                return raw_json
            if isinstance(raw_json, str):
                try:
                    parsed = json.loads(raw_json)
                    return parsed if isinstance(parsed, dict) else {}
                except (json.JSONDecodeError, TypeError):
                    return {}
            return {}

        def _first_numeric(source: Any, *keys: str) -> Optional[float]:
            if source is None:
                return None
            for key in keys:
                if isinstance(source, dict) and key in source:
                    val = _safe_float(source.get(key))
                    if val is not None:
                        return val
            return None

        def _safe_div(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
            if numerator is None or denominator is None or denominator == 0:
                return None
            return numerator / denominator

        def _avg_pair(current: Optional[float], prior: Optional[float]) -> Optional[float]:
            if current is None or prior is None:
                return None
            return (current + prior) / 2.0

        def _sum_optional(values: List[Optional[float]]) -> Optional[float]:
            present = [v for v in values if v is not None]
            if not present:
                return None
            return sum(present)

        # Reuse cached base datasets (no per-ratio querying)
        import time as _t
        _t0_fetch = _t.perf_counter()
        income_rows_all = IncomeStatementRepository._fetch_all_annual_rows(ticker, period_type)
        balance_rows_all = BalanceSheetRepository._fetch_all_annual_rows(ticker, period_type)
        cash_rows_all = CashFlowRepository._fetch_all_annual_rows(ticker, period_type)
        _fetch_ms = (_t.perf_counter() - _t0_fetch) * 1000

        _t0_compute = _t.perf_counter()

        income_rows = [
            row for row in income_rows_all
            if row.get('fiscal_date_ending') and start_date <= row['fiscal_date_ending'] <= end_date
        ]

        if not income_rows:
            return {
                "periods": [],
                "line_items": [],
                "reported_currency": "USD",
                "notes": ["No income statement periods found for selected range."],
            }

        fiscal_year_end = _get_fiscal_year_end_cached(ticker)
        periods = [
            FiscalPeriod.from_date(row['fiscal_date_ending'], period_type, fiscal_year_end)
            for row in income_rows
        ]

        income_by_date: Dict[date, Dict[str, Any]] = {
            row['fiscal_date_ending']: row for row in income_rows_all if row.get('fiscal_date_ending')
        }
        balance_by_date: Dict[date, Dict[str, Any]] = {
            row['fiscal_date_ending']: _parse_json(row.get('raw_json'))
            for row in balance_rows_all if row.get('fiscal_date_ending')
        }
        cash_by_date: Dict[date, Dict[str, Any]] = {
            row['fiscal_date_ending']: _parse_json(row.get('raw_json'))
            for row in cash_rows_all if row.get('fiscal_date_ending')
        }

        all_dates_sorted = sorted(
            set(income_by_date.keys())
            | set(balance_by_date.keys())
            | set(cash_by_date.keys())
        )
        prev_date_map: Dict[date, Optional[date]] = {}
        prev_date: Optional[date] = None
        for dt in all_dates_sorted:
            prev_date_map[dt] = prev_date
            prev_date = dt

        overview = CompanyOverviewRepository.get_company_overview(ticker)
        market_cap = _safe_float(getattr(overview, 'market_capitalization', None)) if overview else None
        dividend_yield = _safe_float(getattr(overview, 'dividend_yield', None)) if overview else None
        if dividend_yield is not None and dividend_yield <= 1:
            dividend_yield = dividend_yield * 100

        metric_values: Dict[str, List[Optional[float]]] = {
            "Current Ratio": [],
            "Quick Ratio": [],
            "Cash Ratio": [],
            "Net Debt / EBITDA": [],
            "Net Debt / (EBITDA - Capex)": [],
            "Debt / Equity": [],
            "Debt / Assets": [],
            "Equity Ratio": [],
            "Inventory Turnover": [],
            "Receivables Turnover": [],
            "Asset Turnover": [],
            "Gross Margin %": [],
            "EBITDA Margin %": [],
            "EBIT Margin %": [],
            "Net Margin %": [],
            "ROA %": [],
            "ROE %": [],
            "P/E": [],
            "Price / Book": [],
            "Dividend Yield %": [],
            "EV / Revenue": [],
            "EV / EBITDA": [],
        }

        for period in periods:
            period_date = period.date
            income_row = income_by_date.get(period_date, {})
            balance_row = balance_by_date.get(period_date, {})
            cash_row = cash_by_date.get(period_date, {})

            prior_date = prev_date_map.get(period_date)
            prior_balance_row = balance_by_date.get(prior_date, {}) if prior_date else {}

            revenue = _first_numeric(income_row, 'total_revenue', 'revenue')
            gross_profit = _first_numeric(income_row, 'gross_profit')
            ebitda = _first_numeric(income_row, 'ebitda')
            if ebitda is None:
                operating_income = _first_numeric(income_row, 'operating_income')
                depreciation_amort = _first_numeric(income_row, 'depreciation_and_amortization')
                if operating_income is not None and depreciation_amort is not None:
                    ebitda = operating_income + depreciation_amort
            ebit = _first_numeric(income_row, 'ebit', 'operating_income')
            net_income = _first_numeric(income_row, 'net_income', 'net_income_from_continuing_operations')
            cogs = _first_numeric(income_row, 'cost_of_revenue')

            current_assets = _first_numeric(balance_row, 'totalCurrentAssets', 'total_current_assets')
            current_liabilities = _first_numeric(balance_row, 'totalCurrentLiabilities', 'total_current_liabilities')
            inventory = _first_numeric(balance_row, 'inventory')
            receivables = _first_numeric(balance_row, 'currentNetReceivables', 'current_net_receivables')
            total_assets = _first_numeric(balance_row, 'totalAssets', 'total_assets')
            total_equity = _first_numeric(balance_row, 'totalShareholderEquity', 'total_shareholder_equity')

            short_debt = _first_numeric(balance_row, 'shortTermDebt', 'currentDebt', 'currentLongTermDebt', 'short_term_debt', 'current_debt')
            long_debt = _first_numeric(balance_row, 'longTermDebt', 'longTermDebtNoncurrent', 'long_term_debt', 'long_term_debt_noncurrent')
            total_debt = _first_numeric(balance_row, 'shortLongTermDebtTotal', 'totalDebt', 'total_debt')
            if total_debt is None:
                total_debt = _sum_optional([short_debt, long_debt])

            cash_and_st = _first_numeric(
                balance_row,
                'cashAndShortTermInvestments',
                'cashAndCashEquivalentsAtCarryingValue',
                'cash_and_short_term_investments',
                'cash_and_cash_equivalents_at_carrying_value',
            )
            # AV's cashAndShortTermInvestments often equals cashAndCashEquivalents
            # and does NOT include shortTermInvestments — add it if present
            st_investments = _first_numeric(
                balance_row,
                'shortTermInvestments',
                'short_term_investments',
            )
            if cash_and_st is not None and st_investments is not None:
                cash_and_st = cash_and_st + st_investments

            capex = _first_numeric(cash_row, 'capitalExpenditures', 'capital_expenditures')
            capex_mag = abs(capex) if capex is not None else None

            prior_inventory = _first_numeric(prior_balance_row, 'inventory') if prior_balance_row else None
            prior_receivables = _first_numeric(prior_balance_row, 'currentNetReceivables', 'current_net_receivables') if prior_balance_row else None
            prior_assets = _first_numeric(prior_balance_row, 'totalAssets', 'total_assets') if prior_balance_row else None
            prior_equity = _first_numeric(prior_balance_row, 'totalShareholderEquity', 'total_shareholder_equity') if prior_balance_row else None

            avg_inventory = _avg_pair(inventory, prior_inventory)
            avg_receivables = _avg_pair(receivables, prior_receivables)
            avg_assets = _avg_pair(total_assets, prior_assets)
            avg_equity = _avg_pair(total_equity, prior_equity)

            net_debt = None
            if total_debt is not None and cash_and_st is not None:
                net_debt = total_debt - cash_and_st

            metric_values["Current Ratio"].append(_safe_div(current_assets, current_liabilities))

            quick_ratio = None
            if current_assets is not None and inventory is not None and current_liabilities is not None:
                quick_ratio = _safe_div(current_assets - inventory, current_liabilities)
            metric_values["Quick Ratio"].append(quick_ratio)

            metric_values["Cash Ratio"].append(_safe_div(cash_and_st, current_liabilities))
            metric_values["Net Debt / EBITDA"].append(_safe_div(net_debt, ebitda))

            nd_over_ebitda_capex = None
            if net_debt is not None and ebitda is not None and capex_mag is not None:
                nd_over_ebitda_capex = _safe_div(net_debt, ebitda - capex_mag)
            metric_values["Net Debt / (EBITDA - Capex)"].append(nd_over_ebitda_capex)

            metric_values["Debt / Equity"].append(_safe_div(total_debt, total_equity))
            metric_values["Debt / Assets"].append(_safe_div(total_debt, total_assets))
            metric_values["Equity Ratio"].append(_safe_div(total_equity, total_assets))

            metric_values["Inventory Turnover"].append(_safe_div(cogs, avg_inventory))
            metric_values["Receivables Turnover"].append(_safe_div(revenue, avg_receivables))
            metric_values["Asset Turnover"].append(_safe_div(revenue, avg_assets))

            gp_margin = _safe_div(gross_profit, revenue)
            metric_values["Gross Margin %"].append(gp_margin * 100 if gp_margin is not None else None)

            ebitda_margin = _safe_div(ebitda, revenue)
            metric_values["EBITDA Margin %"].append(ebitda_margin * 100 if ebitda_margin is not None else None)

            ebit_margin = _safe_div(ebit, revenue)
            metric_values["EBIT Margin %"].append(ebit_margin * 100 if ebit_margin is not None else None)

            net_margin = _safe_div(net_income, revenue)
            metric_values["Net Margin %"].append(net_margin * 100 if net_margin is not None else None)

            roa = _safe_div(net_income, avg_assets)
            metric_values["ROA %"].append(roa * 100 if roa is not None else None)

            roe = _safe_div(net_income, avg_equity)
            metric_values["ROE %"].append(roe * 100 if roe is not None else None)

            p_over_e = _safe_div(market_cap, net_income)
            metric_values["P/E"].append(p_over_e)
            metric_values["Price / Book"].append(_safe_div(market_cap, total_equity))
            metric_values["Dividend Yield %"].append(dividend_yield)

            enterprise_value = None
            if market_cap is not None and total_debt is not None and cash_and_st is not None:
                enterprise_value = market_cap + total_debt - cash_and_st

            metric_values["EV / Revenue"].append(_safe_div(enterprise_value, revenue))
            metric_values["EV / EBITDA"].append(_safe_div(enterprise_value, ebitda))

        reported_currency = income_rows[0].get('reported_currency') if income_rows else "USD"
        if not reported_currency:
            reported_currency = "USD"
        _compute_ms = (_t.perf_counter() - _t0_compute) * 1000

        notes = [
            "Valuation metrics use latest available market cap for all displayed periods (Price/Book, EV/Revenue, EV/EBITDA vary only by the financial-statement denominator).",
            "P/E is computed as Market Cap / Net Income; shown as '-' when net income is zero or unavailable.",
            "Dividend Yield uses the latest company overview value and is constant across all displayed periods.",
            "Enterprise Value is simplified as Market Cap + Total Debt - Cash.",
            "Average-balance ratios require prior comparable period; otherwise shown as missing.",
            "Capex uses absolute magnitude for EBITDA-Capex denominator when capex is stored as outflow.",
        ]

        _RATIO_LINE_SPECS = [
            ("Liquidity", None, True),
            ("Current Ratio", "current_ratio", False),
            ("Quick Ratio", "quick_ratio", False),
            ("Cash Ratio", "cash_ratio", False),
            ("Leverage", None, True),
            ("Net Debt / EBITDA", "net_debt_ebitda", False),
            ("Net Debt / (EBITDA - Capex)", "net_debt_ebitda_capex", False),
            ("Debt / Equity", "debt_equity", False),
            ("Debt / Assets", "debt_assets", False),
            ("Equity Ratio", "equity_ratio", False),
            ("Efficiency", None, True),
            ("Inventory Turnover", "inventory_turnover", False),
            ("Receivables Turnover", "receivables_turnover", False),
            ("Asset Turnover", "asset_turnover", False),
            ("Profitability", None, True),
            ("Gross Margin %", "gross_margin_pct", False),
            ("EBITDA Margin %", "ebitda_margin_pct", False),
            ("EBIT Margin %", "ebit_margin_pct", False),
            ("Net Margin %", "net_margin_pct", False),
            ("ROA %", "roa_pct", False),
            ("ROE %", "roe_pct", False),
            ("Valuation", None, True),
            ("P/E", "pe", False),
            ("Price / Book", "price_book", False),
            ("Dividend Yield %", "dividend_yield_pct", False),
            ("EV / Revenue", "ev_revenue", False),
            ("EV / EBITDA", "ev_ebitda", False),
        ]
        line_items: List[Dict[str, Any]] = []
        for label, metric_key, is_header in _RATIO_LINE_SPECS:
            if is_header:
                line_items.append({
                    "label": label,
                    "values": [None] * len(periods),
                    "is_section_header": True,
                    "is_bold": True,
                    "indent": 0,
                })
            else:
                is_pct = label.endswith("%")
                line_items.append({
                    "label": label,
                    "metric_key": metric_key,
                    "values": metric_values[label],
                    "is_percent": is_pct,
                    "is_bold": False,
                    "indent": 1,
                })

        _total_time = (_time.perf_counter() - _start) * 1000
        from utils.server_logger import log_warning as _slw
        _slw("[TIMING] RATIOS_get_data | %.1fms | ticker=%s periods=%d line_items=%d" % (
            _total_time, ticker, len(periods), len(line_items)))

        return {
            "periods": periods,
            "line_items": line_items,
            "reported_currency": reported_currency,
            "notes": notes,
        }

    @staticmethod
    def extract_metric_value(data: Dict[str, Any], metric_key: str, period_index: int) -> Optional[float]:
        """Return tab-equivalent ratio value for a period index from get_ratios_data output."""
        if period_index is None or period_index < 0:
            return None
        for item in data.get("line_items") or []:
            if item.get("is_section_header"):
                continue
            if item.get("metric_key") == metric_key:
                values = item.get("values") or []
                if period_index < len(values):
                    return values[period_index]
        return None

class ForexRepository:
    """Repository for currency conversion rates from coreiq_av_forex_daily table."""

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    @_log_query_time
    def get_conversion_rate(from_currency: str, to_currency: str, as_of_date: Optional[date] = None) -> float:
        """
        Get conversion rate between two currencies (cached 5 min).

        Args:
            from_currency: Source currency code (e.g., 'USD')
            to_currency: Target currency code (e.g., 'EUR')
            as_of_date: Date for the rate (defaults to most recent)

        Returns:
            Conversion rate as float (1.0 if same currency or not found)
        """
        if from_currency == to_currency:
            return 1.0

        # Query the forex table for the most recent rate
        if as_of_date:
            query = """
                SELECT close
                FROM coreiq_av_forex_daily
                WHERE from_currency = :from_currency
                  AND to_currency = :to_currency
                  AND day_date <= :as_of_date
                ORDER BY day_date DESC
                LIMIT 1
            """
            params = {
                "from_currency": from_currency,
                "to_currency": to_currency,
                "as_of_date": as_of_date
            }
        else:
            query = """
                SELECT close
                FROM coreiq_av_forex_daily
                WHERE from_currency = :from_currency
                  AND to_currency = :to_currency
                ORDER BY day_date DESC
                LIMIT 1
            """
            params = {
                "from_currency": from_currency,
                "to_currency": to_currency
            }

        results = db_manager.execute_query_readonly(query, params)

        if results and results[0].get('close'):
            return float(results[0]['close'])

        # No direct mapping found — return 1.0 (no conversion)
        return 1.0

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_to_currencies(from_currency: str) -> List[str]:
        """Currencies that from_currency can be directly converted TO (no reverse/bridge).
        The from_currency itself is always included first (= no conversion, rate 1.0).
        """
        query = """
            SELECT DISTINCT to_currency
            FROM coreiq_av_forex_daily
            WHERE from_currency = :fc
            ORDER BY to_currency
        """
        results = db_manager.execute_query_readonly(query, {"fc": from_currency})
        others = [r['to_currency'] for r in results if r.get('to_currency') and r['to_currency'] != from_currency]
        return [from_currency] + others

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_from_currencies(to_currency: str) -> List[str]:
        """Currencies that can be directly converted TO to_currency (no reverse/bridge).
        The to_currency itself is always included first (= no conversion, rate 1.0).
        """
        query = """
            SELECT DISTINCT from_currency
            FROM coreiq_av_forex_daily
            WHERE to_currency = :tc
            ORDER BY from_currency
        """
        results = db_manager.execute_query_readonly(query, {"tc": to_currency})
        others = [r['from_currency'] for r in results if r.get('from_currency') and r['from_currency'] != to_currency]
        return [to_currency] + others

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    @_log_query_time
    def get_conversion_rates_bulk(
        from_currency: str,
        to_currency: str,
        as_of_dates: tuple,
    ) -> Dict[date, float]:
        """
        Get historical conversion rates for multiple dates (cached 5 min).

        For each as_of_date, finds the closest rate on or before that date.
        Returns a dict mapping each requested date to its conversion rate.

        Args:
            from_currency: Source currency code (e.g., 'USD')
            to_currency:   Target currency code (e.g., 'EUR')
            as_of_dates:   Tuple of dates (must be a tuple for cache key hashing)

        Returns:
            Dict mapping each date to its conversion rate (1.0 if same currency or not found)
        """
        if from_currency == to_currency:
            return {d: 1.0 for d in as_of_dates}

        if not as_of_dates:
            return {}

        # Build a query that fetches the closest rate on or before each date
        # We use a lateral-join style approach via a subquery for each date
        rate_map: Dict[date, float] = {}

        # Direct lookup only — no reverse/bridge fallback
        min_date = min(as_of_dates)
        max_date = max(as_of_dates)

        query = """
            SELECT day_date, close
            FROM coreiq_av_forex_daily
            WHERE from_currency = :from_currency
              AND to_currency = :to_currency
              AND day_date <= :max_date
            ORDER BY day_date DESC
        """
        results = db_manager.execute_query_readonly(query, {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "max_date": max_date
        })

        if not results:
            return {d: 1.0 for d in as_of_dates}

        # Results sorted DESC — for each target date find the closest rate on or before it
        for target_date in as_of_dates:
            found_rate = None
            for row in results:
                row_date = row['day_date']
                if hasattr(row_date, 'date'):
                    row_date = row_date.date()
                if row_date <= target_date:
                    try:
                        found_rate = float(row['close'])
                    except (ValueError, TypeError):
                        found_rate = 1.0
                    break
            rate_map[target_date] = found_rate if found_rate is not None else 1.0

        return rate_map

class StockQuoteRepository:
    """Repository for Stock Quote table — fetches latest price data + company overview."""

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    @_log_query_time
    def get_latest_quote(ticker: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the most recent day's bar data (cached 5 min).

        SEC companies: coreiq_av_time_series_daily
        Non-SEC (YF) companies: coreiq_yf_time_series_daily

        Returns a dict with keys:
            open, high, low, close, volume, day_date,
            change_on_day, change_percent_on_day
        or None if no data found.
        """
        import time
        _start = time.time()

        from data.source_router import get_company_source

        def _f(val: Any) -> Optional[float]:
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        _t0 = time.time()
        source = get_company_source(ticker) or "SEC"
        _source_time = (time.time() - _t0) * 1000

        if source == "YFinance":
            # Non-SEC: Use coreiq_yf_time_series_daily
            # coreiq_yf_time_series_daily stores base ticker (e.g. 'ATD' not 'ATD.TO')
            _yf_ts_ticker = ticker.split('.')[0] if '.' in ticker else ticker
            table = "coreiq_yf_time_series_daily"
            query = """
                SELECT raw_json, day_date
                FROM coreiq_yf_time_series_daily
                WHERE ticker = :ticker
                  AND raw_json IS NOT NULL
                ORDER BY day_date DESC
                LIMIT 1
            """
            results = db_manager.execute_query_readonly(query, {"ticker": _yf_ts_ticker})
            if not results:
                return None

            row = results[0]
            bar = {}
            if row.get("raw_json"):
                try:
                    raw = json.loads(row["raw_json"])
                    bar = raw.get("bar", {})
                except (json.JSONDecodeError, TypeError):
                    pass

            # YF uses CamelCase keys
            open_p  = _f(bar.get("Open"))
            high_p  = _f(bar.get("High"))
            low_p   = _f(bar.get("Low"))
            close_p = _f(bar.get("Close"))
            volume  = _f(bar.get("Volume"))
        else:
            # SEC: Use coreiq_av_time_series_daily
            table = "coreiq_av_time_series_daily"
            query = """
                SELECT raw_json, day_date
                FROM coreiq_av_time_series_daily
                WHERE ticker = :ticker
                  AND raw_json IS NOT NULL
                ORDER BY day_date DESC
                LIMIT 1
            """
            results = db_manager.execute_query_readonly(query, {"ticker": ticker})
            if not results:
                return None

            row = results[0]
            bar = {}
            if row.get("raw_json"):
                try:
                    raw = json.loads(row["raw_json"])
                    bar = raw.get("bar", {})
                except (json.JSONDecodeError, TypeError):
                    pass

            # AV uses numbered keys
            open_p  = _f(bar.get("1. open"))
            high_p  = _f(bar.get("2. high"))
            low_p   = _f(bar.get("3. low"))
            close_p = _f(bar.get("4. close"))
            volume  = _f(bar.get("5. volume"))

        change_on_day = None
        change_pct    = None
        if open_p is not None and close_p is not None and open_p != 0:
            change_on_day = close_p - open_p
            change_pct    = (change_on_day / open_p) * 100.0

        _total = (time.time() - _start) * 1000
        if _total > 1000:
            pass  # logging removed
        else:
            pass  # logging removed

        return {
            "open":              open_p,
            "high":              high_p,
            "low":               low_p,
            "close":             close_p,
            "volume":            int(volume) if volume is not None else None,
            "day_date":          row.get("day_date"),
            "change_on_day":     change_on_day,
            "change_percent":    change_pct,
        }

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    @_log_query_time
    def get_price_history(ticker: str, days: int = 365) -> List[Dict[str, Any]]:
        """
        Fetch daily close prices for charting (last `days` calendar days, cached 5 min).

        SEC companies: coreiq_av_time_series_daily
        Non-SEC (YF) companies: coreiq_yf_time_series_daily

        Returns list of dicts sorted ASC by date:
            [{"date": "2025-01-30", "close": 204.79, "volume": 12345678}, ...]
        """
        import time
        _start = time.time()

        from data.source_router import get_company_source

        _t0 = time.time()
        source = get_company_source(ticker) or "SEC"
        _source_time = (time.time() - _t0) * 1000

        history = []

        if source == "YFinance":
            # Non-SEC: Use coreiq_yf_time_series_daily
            # coreiq_yf_time_series_daily stores base ticker (e.g. 'ATD' not 'ATD.TO')
            _yf_ts_ticker = ticker.split('.')[0] if '.' in ticker else ticker
            table = "coreiq_yf_time_series_daily"
            columns = "day_date, raw_json"
            query = """
                SELECT day_date, raw_json
                FROM coreiq_yf_time_series_daily
                WHERE ticker = :ticker
                  AND day_date >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                  AND raw_json IS NOT NULL
                ORDER BY day_date ASC
            """
            results = db_manager.execute_query_readonly(query, {"ticker": _yf_ts_ticker, "days": days})
            for row in results:
                if row.get("raw_json"):
                    try:
                        bar = json.loads(row["raw_json"]).get("bar", {})
                        close_val = float(bar.get("Close", 0)) or None
                        volume_val = int(float(bar.get("Volume", 0))) or None

                        if close_val is not None:
                            day = row["day_date"]
                            history.append({
                                "date":   str(day) if not isinstance(day, str) else day,
                                "close":  close_val,
                                "volume": volume_val,
                            })
                    except (ValueError, TypeError, json.JSONDecodeError):
                        pass
        else:
            # SEC: Use coreiq_av_time_series_daily
            table = "coreiq_av_time_series_daily"
            columns = "day_date, close, raw_json"
            query = """
                SELECT day_date, close, raw_json
                FROM coreiq_av_time_series_daily
                WHERE ticker = :ticker
                  AND day_date >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                  AND raw_json IS NOT NULL
                ORDER BY day_date ASC
            """
            results = db_manager.execute_query_readonly(query, {"ticker": ticker, "days": days})
            for row in results:
                close_val = None
                volume_val = None
                # Prefer the dedicated column for close; fall back to raw_json
                if row.get("close") is not None:
                    try:
                        close_val = float(row["close"])
                    except (ValueError, TypeError):
                        pass
                # Always parse raw_json for volume (dedicated volume column may not exist)
                if row.get("raw_json"):
                    try:
                        bar = json.loads(row["raw_json"]).get("bar", {})
                        if close_val is None:
                            close_val = float(bar.get("4. close", 0)) or None
                        volume_val = int(float(bar.get("5. volume", 0))) or None
                    except (ValueError, TypeError, json.JSONDecodeError):
                        pass
                if close_val is not None:
                    day = row["day_date"]
                    history.append({
                        "date":   str(day) if not isinstance(day, str) else day,
                        "close":  close_val,
                        "volume": volume_val,
                    })

        _total = (time.time() - _start) * 1000
        if _total > 3000:
            log_error(f"[STOCK_REPO] [CRITICAL] 🚨 get_price_history SLOW: {_total:.1f}ms, records={len(history)}")
        elif _total > 1000:
            pass  # logging removed
        else:
            pass  # logging removed

        return history

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_shares_with_price(ticker: str) -> Dict[str, Any]:
        """
        Fetch shares outstanding history joined with exact-match close price.

        SEC: report_date = day_date in coreiq_av_time_series_daily
             Returns basic + diluted shares per report_date.
        Non-SEC: asof_date = day_date in coreiq_yf_time_series_daily
             Returns shares per asof_date.

        Returns:
            {
                "is_sec": bool,
                "rows": [
                    # SEC rows:
                    {"date": "2024-12-31", "close": 219.39,
                     "shares_basic": 10551000000, "shares_diluted": 10769000000,
                     "mktcap_basic": ..., "mktcap_diluted": ...}
                    # Non-SEC rows:
                    {"date": "2026-04-01", "close": 1889.5,
                     "shares": 703513211, "mktcap": ...}
                ]
            }
        """
        from data.source_router import get_company_source

        def _f(val):
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        source = get_company_source(ticker) or "SEC"
        is_sec = (source != "YFinance")
        rows = []

        if is_sec:
            results = db_manager.execute_query_readonly("""
                SELECT s.report_date, s.shares_outstanding_basic, s.shares_outstanding_diluted, ts.close
                FROM coreiq_av_shares_outstanding s
                INNER JOIN coreiq_av_time_series_daily ts
                    ON ts.ticker = s.ticker AND ts.day_date = s.report_date
                WHERE s.ticker = :ticker
                ORDER BY s.report_date DESC
            """, {"ticker": ticker})

            for row in results:
                close = _f(row.get("close"))
                basic = _f(row.get("shares_outstanding_basic"))
                diluted = _f(row.get("shares_outstanding_diluted"))
                rows.append({
                    "date": str(row["report_date"]),
                    "close": close,
                    "shares_basic": basic,
                    "shares_diluted": diluted,
                    "mktcap_basic": (close * basic) if close and basic else None,
                    "mktcap_diluted": (close * diluted) if close and diluted else None,
                })
        else:
            results = db_manager.execute_query_readonly("""
                SELECT s.asof_date, s.shares_outstanding, ts.close
                FROM coreiq_yf_shares_outstanding s
                INNER JOIN coreiq_yf_time_series_daily ts
                    ON ts.ticker = s.ticker AND ts.day_date = s.asof_date
                WHERE s.ticker = :ticker
                ORDER BY s.asof_date DESC
            """, {"ticker": ticker})

            for row in results:
                close = _f(row.get("close"))
                shares = _f(row.get("shares_outstanding"))
                rows.append({
                    "date": str(row["asof_date"]),
                    "close": close,
                    "shares": shares,
                    "mktcap": (close * shares) if close and shares else None,
                })

        return {"is_sec": is_sec, "rows": rows}

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_overview_data(ticker: str) -> Optional[Dict[str, Any]]:
        """
        Fetch company overview fields needed for the Stock Quote table (cached 10 min).

        Market Cap = latest close price × latest shares outstanding.
        Shares outstanding sourced from:
          - SEC companies: coreiq_av_shares_outstanding (basic)
          - YF  companies: coreiq_yf_shares_outstanding

        Returns a dict with keys:
            market_cap_mm, shares_outstanding_mm, dividend_yield,
            diluted_eps, pe_ratio, week_52_high, week_52_low
        or None if no data found.
        """
        import time
        _start = time.time()

        from data.source_router import get_company_source

        def _f(val: Any) -> Optional[float]:
            if val is None or val == "None":
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        _t0 = time.time()
        source = get_company_source(ticker) or "SEC"
        _source_time = (time.time() - _t0) * 1000

        # ── AV overview (always queried — has EPS, PE, dividend, 52-week) ──
        av_results = db_manager.execute_query_readonly("""
            SELECT market_capitalization, dividend_yield, eps, pe_ratio, raw_json
            FROM coreiq_av_company_overview
            WHERE ticker = :ticker
            ORDER BY fetched_at_utc DESC
            LIMIT 1
        """, {"ticker": ticker})
        av_row = av_results[0] if av_results else {}
        av_raw: Dict[str, Any] = {}
        if av_row.get("raw_json"):
            try:
                av_raw = json.loads(av_row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        # ── YF overview (for YF companies — EPS, PE, dividend, 52-week) ──
        yf_info: Dict[str, Any] = {}
        if source == "YFinance":
            yf_results = db_manager.execute_query_readonly("""
                SELECT payload_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """, {"ticker": ticker})
            if yf_results and yf_results[0].get("payload_json"):
                try:
                    payload = json.loads(yf_results[0]["payload_json"])
                    yf_info = payload.get("info", payload)
                except (json.JSONDecodeError, TypeError):
                    pass

        # ── Shares outstanding from dedicated tables ──
        shares_raw: Optional[float] = None
        if source == "YFinance":
            sh = db_manager.execute_query_readonly("""
                SELECT shares_outstanding
                FROM coreiq_yf_shares_outstanding
                WHERE ticker = :ticker
                ORDER BY asof_date DESC
                LIMIT 1
            """, {"ticker": ticker})
            if sh:
                shares_raw = _f(sh[0].get("shares_outstanding"))
            if shares_raw is None:
                shares_raw = _f(yf_info.get("sharesOutstanding"))
        else:
            sh = db_manager.execute_query_readonly("""
                SELECT shares_outstanding_basic
                FROM coreiq_av_shares_outstanding
                WHERE ticker = :ticker
                ORDER BY report_date DESC
                LIMIT 1
            """, {"ticker": ticker})
            if sh:
                shares_raw = _f(sh[0].get("shares_outstanding_basic"))
            if shares_raw is None:
                shares_raw = _f(av_raw.get("SharesOutstanding"))

        # ── Latest close price from time series ──
        latest_close: Optional[float] = None
        if source == "YFinance":
            # Non-SEC: Use coreiq_yf_time_series_daily
            # coreiq_yf_time_series_daily stores base ticker (e.g. 'ATD' not 'ATD.TO')
            _yf_ts_ticker = ticker.split('.')[0] if '.' in ticker else ticker
            close_results = db_manager.execute_query_readonly("""
                SELECT raw_json
                FROM coreiq_yf_time_series_daily
                WHERE ticker = :ticker
                ORDER BY day_date DESC
                LIMIT 1
            """, {"ticker": _yf_ts_ticker})
            if close_results and close_results[0].get("raw_json"):
                try:
                    bar = json.loads(close_results[0]["raw_json"]).get("bar", {})
                    latest_close = _f(bar.get("Close"))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
        else:
            # SEC: Use coreiq_av_time_series_daily
            close_results = db_manager.execute_query_readonly("""
                SELECT close, raw_json
                FROM coreiq_av_time_series_daily
                WHERE ticker = :ticker
                ORDER BY day_date DESC
                LIMIT 1
            """, {"ticker": ticker})
            if close_results:
                cr = close_results[0]
                latest_close = _f(cr.get("close"))
                if latest_close is None and cr.get("raw_json"):
                    try:
                        bar = json.loads(cr["raw_json"]).get("bar", {})
                        latest_close = _f(bar.get("4. close"))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass

        # ── Market Cap = close × shares ──
        market_cap_mm: Optional[float] = None
        if latest_close is not None and shares_raw is not None and shares_raw > 0:
            market_cap_mm = (latest_close * shares_raw) / 1_000_000
        shares_mm = (shares_raw / 1_000_000) if shares_raw is not None else None

        # ── Other fields: prefer YF info for YF companies, else AV ──
        if source == "YFinance" and yf_info:
            dividend_yield = _f(yf_info.get("dividendYield"))
            if dividend_yield is not None:
                dividend_yield *= 100  # YF gives decimal (0.012 → 1.2%)
            diluted_eps  = _f(yf_info.get("trailingEps"))
            pe           = _f(yf_info.get("trailingPE"))
            week_52_high = _f(yf_info.get("fiftyTwoWeekHigh"))
            week_52_low  = _f(yf_info.get("fiftyTwoWeekLow"))
        else:
            dividend_yield = _f(av_row.get("dividend_yield"))
            diluted_eps    = _f(av_row.get("eps"))
            pe             = _f(av_row.get("pe_ratio"))
            week_52_high   = _f(av_raw.get("52WeekHigh"))
            week_52_low    = _f(av_raw.get("52WeekLow"))

        if not av_row and not yf_info and shares_raw is None:
            return None

        _total = (time.time() - _start) * 1000
        if _total > 2000:
            log_error(f"[STOCK_REPO] [CRITICAL] 🚨 get_overview_data SLOW: {_total:.1f}ms")
        elif _total > 1000:
            pass  # logging removed
        else:
            pass  # logging removed

        return {
            "market_cap_mm":         market_cap_mm,
            "shares_outstanding_mm": shares_mm,
            "shares_outstanding":    shares_raw,      # raw share count (for Excel market cap calc)
            "dividend_yield":        dividend_yield,
            "diluted_eps":           diluted_eps,
            "pe_ratio":              pe,
            "week_52_high":          week_52_high,
            "week_52_low":           week_52_low,
        }

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_market_cap_chart_data(ticker: str, months: int = 12) -> List[Dict[str, Any]]:
        """
        Fetch Market Cap chart data aggregated by period.

        For SEC companies: Quarterly aggregation (last 12 months = 4 quarters)
        For Non-SEC (YF) companies: Monthly aggregation (last 12 months)

        Returns list of dicts sorted ASC by period end date:
            [{"period": "Q1 2025", "date": "2025-03-31", "close": 150.25, "volume": 1234567,
              "shares_outstanding": 1000000000, "market_cap": 150250000000}, ...]
        """
        from data.source_router import get_company_source
        from datetime import datetime, timedelta
        from collections import defaultdict

        def _f(val: Any) -> Optional[float]:
            if val is None or val == "None":
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        source = get_company_source(ticker) or "SEC"
        is_sec = (source != "YFinance")

        # Calculate date range (last N months)
        end_date = datetime.now().date()
        start_date = (datetime.now() - timedelta(days=30*months)).date()

        # ── Fetch Price History (Daily) ──
        price_data = []
        if is_sec:
            # SEC companies: coreiq_av_time_series_daily
            results = db_manager.execute_query_readonly("""
                SELECT day_date, close, raw_json
                FROM coreiq_av_time_series_daily
                WHERE ticker = :ticker
                  AND day_date >= :start_date
                  AND day_date <= :end_date
                ORDER BY day_date ASC
            """, {"ticker": ticker, "start_date": start_date, "end_date": end_date})

            for row in results:
                close_val = _f(row.get("close"))
                volume_val = 0

                # Parse raw_json for bar data
                if row.get("raw_json"):
                    try:
                        bar_data = json.loads(row["raw_json"]).get("bar", {})
                        if close_val is None:
                            close_val = _f(bar_data.get("4. close"))
                        volume_val = int(float(bar_data.get("5. volume", 0))) or 0
                    except (json.JSONDecodeError, TypeError):
                        pass

                if close_val:
                    price_data.append({
                        "date": str(row["day_date"]),
                        "close": close_val,
                        "volume": volume_val
                    })
        else:
            # Non-SEC companies: coreiq_yf_time_series_daily
            results = db_manager.execute_query_readonly("""
                SELECT day_date, raw_json
                FROM coreiq_yf_time_series_daily
                WHERE ticker = :ticker
                  AND day_date >= :start_date
                  AND day_date <= :end_date
                ORDER BY day_date ASC
            """, {"ticker": ticker, "start_date": start_date, "end_date": end_date})

            for row in results:
                if row.get("raw_json"):
                    try:
                        bar_data = json.loads(row["raw_json"]).get("bar", {})
                        close_val = _f(bar_data.get("Close"))
                        volume_val = int(float(bar_data.get("Volume", 0))) or 0

                        if close_val:
                            price_data.append({
                                "date": str(row["day_date"]),
                                "close": close_val,
                                "volume": volume_val
                            })
                    except (json.JSONDecodeError, TypeError):
                        pass

        # ── Fetch Shares Outstanding History ──
        shares_history = []
        if is_sec:
            # SEC: coreiq_av_shares_outstanding (quarterly)
            results = db_manager.execute_query_readonly("""
                SELECT report_date, shares_outstanding_basic, raw_json
                FROM coreiq_av_shares_outstanding
                WHERE ticker = :ticker
                ORDER BY report_date ASC
            """, {"ticker": ticker})

            for row in results:
                shares = None
                if row.get("raw_json"):
                    try:
                        raw_data = json.loads(row["raw_json"])
                        # Structure: {"row": {"shares_outstanding_basic": "...", ...}}
                        row_data = raw_data.get("row", raw_data)
                        shares = _f(row_data.get("shares_outstanding_basic") or
                                   row_data.get("SharesOutstanding"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                if shares is None:
                    shares = _f(row.get("shares_outstanding_basic"))
                if shares:
                    shares_history.append({
                        "date": str(row["report_date"]),
                        "shares_outstanding": shares
                    })
        else:
            # Non-SEC: coreiq_yf_shares_outstanding
            results = db_manager.execute_query_readonly("""
                SELECT asof_date, shares_outstanding, raw_json
                FROM coreiq_yf_shares_outstanding
                WHERE ticker = :ticker
                ORDER BY asof_date ASC
            """, {"ticker": ticker})

            for row in results:
                shares = None
                if row.get("raw_json"):
                    try:
                        raw_data = json.loads(row["raw_json"])
                        shares = _f(raw_data.get("sharesOutstanding") or
                                   raw_data.get("shares_outstanding"))
                    except (json.JSONDecodeError, TypeError):
                        pass
                if shares is None:
                    shares = _f(row.get("shares_outstanding"))
                if shares:
                    shares_history.append({
                        "date": str(row["asof_date"]),
                        "shares_outstanding": shares
                    })

        # ── Aggregate by Period ──
        def get_period_key(date_str):
            """Get period key for grouping."""
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if is_sec:
                # Quarterly: Q1 (Jan-Mar), Q2 (Apr-Jun), Q3 (Jul-Sep), Q4 (Oct-Dec)
                quarter = (dt.month - 1) // 3 + 1
                quarter_months = {
                    1: "Jan-Mar",
                    2: "Apr-Jun",
                    3: "Jul-Sep",
                    4: "Oct-Dec"
                }
                month_range = quarter_months.get(quarter, "")
                return f"Q{quarter} {dt.year} ({month_range})", dt.year, quarter
            else:
                # Monthly
                return f"{dt.strftime('%b %Y')}", dt.year, dt.month

        # Group prices by period
        period_prices = defaultdict(list)
        for p in price_data:
            period_key, year, period_num = get_period_key(p["date"])
            period_prices[period_key].append({
                **p,
                "year": year,
                "period_num": period_num
            })

        # For each period, pick the LAST trading day
        # Sort periods chronologically by tracking the max date in each period
        period_info = []
        for period_key, prices in period_prices.items():
            if not prices:
                continue
            # Sort by date and pick last
            prices_sorted = sorted(prices, key=lambda x: x["date"])
            last_price = prices_sorted[-1]
            period_info.append({
                "period_key": period_key,
                "prices": prices,
                "last_date": last_price["date"]  # For chronological sorting
            })

        # Sort by last_date (chronological)
        period_info_sorted = sorted(period_info, key=lambda x: x["last_date"])

        chart_data = []
        for pinfo in period_info_sorted:
            period_key = pinfo["period_key"]
            prices = pinfo["prices"]
            # Re-get last price
            prices_sorted = sorted(prices, key=lambda x: x["date"])
            last_price = prices_sorted[-1]

            # Find shares outstanding for this date
            price_date = datetime.strptime(last_price["date"], "%Y-%m-%d").date()
            shares_for_date = None

            # Find most recent shares outstanding <= price_date
            for sh in reversed(shares_history):
                sh_date = datetime.strptime(sh["date"], "%Y-%m-%d").date()
                if sh_date <= price_date:
                    shares_for_date = sh["shares_outstanding"]
                    break

            # If no shares found, try to find the next available
            if shares_for_date is None:
                for sh in shares_history:
                    sh_date = datetime.strptime(sh["date"], "%Y-%m-%d").date()
                    if sh_date >= price_date:
                        shares_for_date = sh["shares_outstanding"]
                        break

            # Calculate market cap
            market_cap = None
            if last_price["close"] and shares_for_date:
                market_cap = last_price["close"] * shares_for_date

            chart_data.append({
                "period": period_key,
                "date": last_price["date"],
                "close": last_price["close"],
                "volume": last_price["volume"],
                "shares_outstanding": shares_for_date,
                "market_cap": market_cap
            })

        return chart_data

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    @_log_query_time
    def get_stock_price_chart_data(ticker: str, months: int = 12) -> List[Dict[str, Any]]:
        """
        Fetch Stock Price chart data - simple daily close prices.

        For SEC companies: coreiq_av_time_series_daily
        For Non-SEC (YF) companies: coreiq_yf_time_series_daily

        Returns list of dicts sorted ASC by date:
            [{"date": "2025-03-31", "close": 150.25}, ...]
        """
        from data.source_router import get_company_source
        from datetime import datetime, timedelta

        def _f(val: Any) -> Optional[float]:
            if val is None or val == "None":
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        source = get_company_source(ticker) or "SEC"
        is_sec = (source != "YFinance")

        # Calculate date range (last N months)
        end_date = datetime.now().date()
        start_date = (datetime.now() - timedelta(days=30*months)).date()

        # ── Fetch Price History (Daily) ──
        price_data = []
        if is_sec:
            # SEC companies: coreiq_av_time_series_daily
            results = db_manager.execute_query_readonly("""
                SELECT day_date, close, raw_json
                FROM coreiq_av_time_series_daily
                WHERE ticker = :ticker
                  AND day_date >= :start_date
                  AND day_date <= :end_date
                ORDER BY day_date ASC
            """, {"ticker": ticker, "start_date": start_date, "end_date": end_date})

            for row in results:
                close_val = _f(row.get("close"))

                # Parse raw_json for bar data
                if close_val is None and row.get("raw_json"):
                    try:
                        bar_data = json.loads(row["raw_json"]).get("bar", {})
                        close_val = _f(bar_data.get("4. close"))
                    except (json.JSONDecodeError, TypeError):
                        pass

                if close_val:
                    price_data.append({
                        "date": str(row["day_date"]),
                        "close": close_val
                    })
        else:
            # Non-SEC companies: coreiq_yf_time_series_daily
            results = db_manager.execute_query_readonly("""
                SELECT day_date, raw_json
                FROM coreiq_yf_time_series_daily
                WHERE ticker = :ticker
                  AND day_date >= :start_date
                  AND day_date <= :end_date
                ORDER BY day_date ASC
            """, {"ticker": ticker, "start_date": start_date, "end_date": end_date})

            for row in results:
                if row.get("raw_json"):
                    try:
                        bar_data = json.loads(row["raw_json"]).get("bar", {})
                        close_val = _f(bar_data.get("Close"))

                        if close_val:
                            price_data.append({
                                "date": str(row["day_date"]),
                                "close": close_val
                            })
                    except (json.JSONDecodeError, TypeError):
                        pass

        return price_data

class FilingMetricRepository:
    """Repository for filing_metrics table — SEC filing metric search."""

    # Synonym map: user-typed term → list of DB label variants.
    # All keys and values must be lowercase. The original query is always
    # searched as well — these are ADDITIONAL terms, not replacements.
    SYNONYM_MAP: Dict[str, List[str]] = {
        # Income Statement
        "revenue":                   ["net sales", "total revenue", "revenues", "total net revenue"],
        "total revenue":             ["net sales", "revenue", "total net revenue"],
        "net revenue":               ["net sales", "revenue"],
        "sales":                     ["net sales", "revenue", "total revenue"],
        "gross profit":              ["gross margin", "gross income"],
        "gross margin":              ["gross profit", "gross income"],
        "cost of goods sold":        ["cost of sales", "cost of revenue", "cost of products"],
        "cogs":                      ["cost of sales", "cost of revenue", "cost of goods sold"],
        "cost of revenue":           ["cost of sales", "cost of goods sold"],
        "selling general":           ["selling, general and administrative", "sg&a", "sga"],
        "sg&a":                      ["selling, general and administrative", "selling general"],
        "sga":                       ["selling, general and administrative", "selling general and admin"],
        "operating expenses":        ["total operating expenses", "operating expense"],
        "operating income":          ["income from operations", "operating profit", "ebit"],
        "operating profit":          ["operating income", "income from operations"],
        "ebit":                      ["operating income", "operating profit", "income from operations"],
        "interest expense":          ["other income", "interest and other income", "net interest expense"],
        "interest income":           ["other income", "interest and other income", "investment income"],
        "net interest":              ["other income", "interest expense", "interest income"],
        "other income":              ["other income/(expense), net", "other income/expense"],
        "r&d":                       ["research and development", "research & development"],
        "research and development":  ["r&d", "research & development"],
        "depreciation":              ["depreciation and amortization", "depreciation & amortization", "d&a"],
        "amortization":              ["depreciation and amortization", "depreciation & amortization"],
        "d&a":                       ["depreciation and amortization", "depreciation"],
        "depreciation amortization": ["depreciation and amortization"],
        "net income":                ["net income (loss)", "net earnings", "profit after tax", "earnings"],
        "earnings":                  ["net income", "net earnings"],
        "diluted eps":               ["diluted (in dollars per share)", "earnings per share diluted"],
        "eps":                       ["diluted (in dollars per share)", "basic (in dollars per share)", "earnings per share"],
        "basic eps":                 ["basic (in dollars per share)", "earnings per share basic"],
        "stock based comp":          ["share-based compensation expense", "stock-based compensation"],
        "stock compensation":        ["share-based compensation expense", "stock-based compensation"],
        "share based compensation":  ["share-based compensation expense"],
        "advertising":               ["advertising expense", "advertising costs"],
        "income tax":                ["provision for income taxes", "income tax expense"],
        "tax expense":               ["provision for income taxes", "income tax expense"],

        # Balance Sheet — Assets
        "cash":                      ["cash and cash equivalents", "cash & cash equivalents"],
        "cash equivalents":          ["cash and cash equivalents"],
        "short term investments":    ["marketable securities", "short-term investments"],
        "marketable securities":     ["short-term investments", "short term investments"],
        "accounts receivable":       ["accounts receivable, net", "trade receivables"],
        "receivables":               ["accounts receivable, net", "vendor non-trade receivables"],
        "inventory":                 ["inventories"],
        "inventories":               ["inventory"],
        "prepaid":                   ["prepaid expenses", "other current assets"],
        "current assets":            ["total current assets"],
        "total current assets":      ["current assets"],
        "ppe":                       ["property, plant and equipment, net", "property plant equipment"],
        "property plant equipment":  ["property, plant and equipment, net", "gross property, plant and equipment"],
        "net ppe":                   ["property, plant and equipment, net"],
        "gross ppe":                 ["gross property, plant and equipment"],
        "accumulated depreciation":  ["accumulated depreciation"],
        "goodwill":                  ["goodwill and intangible assets", "intangible assets"],
        "intangibles":               ["intangible assets", "goodwill"],
        "total assets":              ["assets, total"],

        # Balance Sheet — Liabilities
        "accounts payable":          ["accounts payable", "trade payables"],
        "current liabilities":       ["total current liabilities"],
        "long term debt":            ["term debt", "total term debt", "long-term debt"],
        "long-term debt":            ["term debt", "total term debt"],
        "term debt":                 ["long-term debt", "long term debt", "total term debt"],
        "total debt":                ["term debt", "total term debt", "long-term debt"],
        "debt to equity":            ["debt-to-equity", "total debt-to-equity", "lt debt-to-equity"],
        "debt equity":               ["debt-to-equity", "total debt-to-equity"],
        "deferred revenue":          ["deferred revenue", "unearned revenue"],
        "unearned revenue":          ["deferred revenue"],
        "total liabilities":         ["liabilities, total"],
        "operating lease":           ["operating lease liabilities, current", "operating lease liabilities, non-current"],
        "finance lease":             ["finance lease liabilities, current", "finance lease liabilities, non-current"],
        "commercial paper":          ["commercial paper"],

        # Balance Sheet — Equity
        "retained earnings":         ["accumulated deficit", "retained deficit"],
        "accumulated deficit":       ["retained earnings"],
        "shareholders equity":       ["total shareholders' equity", "stockholders equity"],
        "stockholders equity":       ["total shareholders' equity", "shareholders equity"],
        "total equity":              ["total shareholders' equity", "shareholders equity"],
        "book value":                ["total shareholders' equity", "book value of equity"],

        # Cash Flow
        "cash from operations":      ["cash generated by operating activities", "operating cash flow", "cash from operating"],
        "operating cash flow":       ["cash generated by operating activities", "cash from operations"],
        "capex":                     ["payments for acquisition of property, plant and equipment", "capital expenditure", "capital expenditures"],
        "capital expenditure":       ["payments for acquisition of property, plant and equipment", "capex"],
        "capital expenditures":      ["payments for acquisition of property, plant and equipment", "capex"],
        "cash from investing":       ["cash generated by/(used in) investing activities"],
        "investing activities":      ["cash generated by/(used in) investing activities"],
        "cash from financing":       ["cash used in financing activities"],
        "financing activities":      ["cash used in financing activities"],
        "dividends":                 ["payments for dividends and dividend equivalents", "dividends paid"],
        "dividends paid":            ["payments for dividends and dividend equivalents"],
        "buyback":                   ["common stock repurchased", "repurchases of common stock", "share repurchase"],
        "share repurchase":          ["common stock repurchased", "repurchases of common stock"],
        "stock repurchase":          ["common stock repurchased", "repurchases of common stock"],
        "debt issuance":             ["proceeds from issuance of term debt, net"],
        "debt repayment":            ["repayments of term debt", "repayment of debt"],

        # Calculated / Derived (will exist after Phase 6.3)
        "ebitda":                    ["ebitda", "earnings before interest tax depreciation"],
        "net debt":                  ["net debt"],
        "enterprise value":          ["enterprise value", "tev"],
        "gross margin %":            ["gross margin percentage", "gross margin %"],
        "operating margin":          ["operating margin %", "operating income margin"],
        "net margin":                ["net income margin", "net margin %", "profit margin"],

        # Shares
        "shares outstanding":        ["common stock, shares outstanding (in shares)", "entity common stock, shares outstanding"],
        "diluted shares":            ["diluted (in shares)", "weighted average diluted shares"],
        "basic shares":              ["basic (in shares)", "weighted average basic shares"],

        # Segments (search by dimension_label via original_label="Net sales")
        # AAPL segments
        "iphone":                    ["iphone revenue", "iphone net sales"],
        "iphone revenue":            ["iphone", "net sales"],
        "mac":                       ["mac revenue", "mac net sales"],
        "ipad":                      ["ipad revenue", "ipad net sales"],
        "services":                  ["services revenue", "services net sales"],
        "wearables":                 ["wearables, home and accessories", "wearables revenue"],
        "americas":                  ["americas revenue", "americas net sales"],
        "europe":                    ["europe revenue", "europe net sales"],
        "china":                     ["greater china", "china revenue"],
        "greater china":             ["china", "greater china revenue"],

        # AMZN segments
        "aws":                       ["amazon web services"],
        "north america":             ["north america revenue", "north america net sales"],
        "international":             ["international revenue", "international net sales"],
        "total net sales":           ["net sales", "total revenue", "revenue"],

        # M (Macy's) segments
        "macy's":                    ["macys", "macy's first"],
        "bloomingdale":              ["bloomingdale's", "bloomingdales"],
        "net sales":                 ["total net sales", "total revenue", "revenue"],

        # Store Counts (extracted by extract_store_counts.py, source='store_count')
        "store":                     ["store count", "number of stores", "store locations"],
        "stores":                    ["store count", "number of stores", "store locations"],
        "store count":               ["store", "stores", "number of stores", "store locations"],
        "number of stores":          ["store count", "stores", "store locations"],
        "store locations":           ["store count", "stores", "number of stores"],
        "locations":                 ["store count", "store locations", "number of stores"],
        "warehouse":                 ["store count", "warehouses", "membership warehouses"],
        "warehouses":                ["store count", "warehouse", "membership warehouses"],
        "supermarket":               ["store count", "supermarkets"],
        "supermarkets":              ["store count", "supermarket"],

        # Credit Ratings (extracted by extract_credit_ratings.py, source='credit_rating')
        "credit rating":             ["credit ratings", "debt rating", "bond rating", "s&p rating"],
        "credit ratings":            ["credit rating", "debt rating", "bond rating"],
        "debt rating":               ["credit rating", "bond rating", "credit ratings"],
        "bond rating":               ["credit rating", "debt rating", "credit ratings"],
        "investment grade":          ["credit rating", "bbb", "baa"],
        "s&p rating":                ["credit rating", "credit ratings", "debt rating"],
        "moody's rating":            ["credit rating", "credit ratings", "debt rating"],
        "fitch rating":              ["credit rating", "credit ratings"],
        "rating":                    ["credit rating", "credit ratings", "debt rating"],
    }

    @staticmethod
    def _rows_to_results(rows: list) -> "List[FilingMetricResult]":
        """Convert raw DB rows to FilingMetricResult objects."""
        return [
            FilingMetricResult(
                original_label=row["original_label"] or "",
                numeric_value=row["numeric_value"],
                unit_ref=row["unit_ref"],
                report_fiscal_year=row["report_fiscal_year"],
                is_dimensioned=bool(row["is_dimensioned"]),
                dimension_label=row["dimension_label"],
                statement_type=row["statement_type"],
                ixbrl_id=row["ixbrl_id"],
                standard_concept=row["standard_concept"],
                concept=row["concept"],
                balance=row["balance"],
                period_type=row["period_type"],
                period_start=str(row["period_start"]) if row.get("period_start") else None,
                period_end=str(row["period_end"]) if row.get("period_end") else None,
                period_instant=str(row["period_instant"]) if row.get("period_instant") else None,
                value=row["value"],
                source=row.get("source"),
                calculation_note=row.get("llm_query") if row.get("source") == "calculated" else None,
                full_dimension_label=row.get("full_dimension_label"),
                dimension=row.get("dimension"),
                llm_query=row.get("llm_query") if row.get("source") in ("store_count", "credit_rating") else None,
            )
            for row in rows
        ]

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_header_metadata(
        ticker: str,
        effective_year: int,
        doc_type: str,
        filing_unit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return date and fiscal-period metadata for a filing document header.

        `effective_year` is the same value used by the filings Year filter:
        COALESCE(storage_year, report_fiscal_year). The storage year is the
        document bucket/source of truth; report fiscal fields only enrich labels.
        """
        params: Dict[str, Any] = {
            "ticker": ticker,
            "year": effective_year,
            "doc_type": doc_type,
        }

        filing_unit_clause = ""
        if filing_unit:
            if filing_unit == "main":
                filing_unit_clause = "AND (filing_unit IS NULL OR filing_unit = '' OR filing_unit = 'main')"
            else:
                filing_unit_clause = "AND filing_unit = :filing_unit"
                params["filing_unit"] = filing_unit

        query = f"""
            SELECT
                filing_date,
                filing_date_sec,
                fiscal_year,
                fiscal_quarter,
                period_type,
                period_start,
                period_end,
                period_instant,
                COALESCE(fiscal_year, storage_year, report_fiscal_year) AS effective_year,
                report_fiscal_year,
                storage_year
            FROM coreiq_filing_metrics_v4
            WHERE ticker = :ticker
              AND COALESCE(fiscal_year, storage_year, report_fiscal_year) = :year
              AND doc_type = :doc_type
              {filing_unit_clause}
            ORDER BY
                CASE WHEN filing_date_sec IS NULL THEN 1 ELSE 0 END ASC,
                filing_date_sec DESC
            LIMIT 5000
        """

        try:
            rows = db_manager.execute_query_readonly(query, params) or []
            if not rows and filing_unit:
                params.pop("filing_unit", None)
                rows = db_manager.execute_query_readonly(
                    query.replace(filing_unit_clause, ""),
                    params,
                ) or []
            if not rows:
                return {}

            def _to_date(value) -> Optional[date]:
                if not value:
                    return None
                if isinstance(value, datetime):
                    return value.date()
                if isinstance(value, date):
                    return value
                try:
                    return date.fromisoformat(str(value)[:10])
                except (TypeError, ValueError):
                    return None

            # Prefer filing_date_sec (authoritative EDGAR date, never contaminated).
            # Fall back to filing_date for rows pre-dating v4 migration.
            sec_dates = [_to_date(row.get("filing_date_sec")) for row in rows]
            sec_dates = [d for d in sec_dates if d]
            filing_date = max(sec_dates) if sec_dates else None

            if filing_date is None:
                filing_dates = [_to_date(row.get("filing_date")) for row in rows]
                filing_dates = [d for d in filing_dates if d]
                filing_date = max(filing_dates) if filing_dates else None

            selected_storage_year = int(effective_year)
            report_years = [
                int(row["report_fiscal_year"])
                for row in rows
                if row.get("report_fiscal_year") is not None
            ]
            if report_years:
                report_fiscal_year = max(
                    set(report_years),
                    key=lambda year: (report_years.count(year), year),
                )
            else:
                report_fiscal_year = selected_storage_year

            # Metric rows can contain future obligation/maturity dates. Those
            # are real facts, but they are not the filing's report period. Pick
            # the dominant current-period date, preferring duration period_end
            # rows and excluding dates after the filing date/storage bucket.
            cutoff_date = filing_date + timedelta(days=7) if filing_date else date(selected_storage_year + 1, 12, 31)
            earliest_year = min(report_fiscal_year, selected_storage_year) - 2
            period_scores: Dict[date, int] = {}
            instant_scores: Dict[date, int] = {}

            for row in rows:
                row_report_year = row.get("report_fiscal_year")
                try:
                    row_report_year = int(row_report_year) if row_report_year is not None else None
                except (TypeError, ValueError):
                    row_report_year = None

                row_weight = 2 if row_report_year == report_fiscal_year else 1
                row_period_end = _to_date(row.get("period_end"))
                if row_period_end and earliest_year <= row_period_end.year and row_period_end <= cutoff_date:
                    period_scores[row_period_end] = period_scores.get(row_period_end, 0) + row_weight

                row_instant = _to_date(row.get("period_instant"))
                if row_instant and earliest_year <= row_instant.year and row_instant <= cutoff_date:
                    instant_scores[row_instant] = instant_scores.get(row_instant, 0) + row_weight

            selected_period_scores = period_scores or instant_scores
            period_end = (
                max(selected_period_scores, key=lambda d: (selected_period_scores[d], d))
                if selected_period_scores
                else None
            )

            annual_doc_types = {"10-K", "20-F", "40-F", "annual-report", "AR"}
            quarterly_doc_types = {"10-Q-Q1", "10-Q-Q2", "10-Q-Q3"} | {
                f"interim-report-Q{i}" for i in range(1, 6)
            }

            fiscal_q = None
            fiscal_year = None
            fiscal_period_label = ""

            # ── Fiscal year / quarter from v4 columns ────────────────────────
            # fiscal_year and fiscal_quarter are stored authoritatively in v4.
            # Prefer these over any recomputed values.  Rows migrated from v2
            # may have NULL fiscal_quarter — fall back to doc_type parsing.
            db_fiscal_year_values = [
                int(row["fiscal_year"])
                for row in rows
                if row.get("fiscal_year") is not None
            ]
            db_fiscal_year = (
                max(set(db_fiscal_year_values), key=lambda y: (db_fiscal_year_values.count(y), y))
                if db_fiscal_year_values else None
            )

            db_fiscal_quarters = [
                row["fiscal_quarter"]
                for row in rows
                if row.get("fiscal_quarter")
            ]
            db_fiscal_quarter = (
                max(set(db_fiscal_quarters), key=db_fiscal_quarters.count)
                if db_fiscal_quarters else None
            )

            if doc_type in annual_doc_types:
                fiscal_year = db_fiscal_year or selected_storage_year
                fiscal_period_label = f"FY{fiscal_year}"
            elif doc_type in quarterly_doc_types:
                fiscal_year = db_fiscal_year or selected_storage_year
                # fiscal_quarter stored as "Q1"/"Q2"/"Q3" — extract the number.
                if db_fiscal_quarter and db_fiscal_quarter.startswith("Q"):
                    try:
                        fiscal_q = int(db_fiscal_quarter[1:])
                    except ValueError:
                        fiscal_q = None
                else:
                    # Fallback for pre-v4 rows: parse from doc_type.
                    _q_match = re.search(r"Q([1-5])", doc_type or "", re.IGNORECASE)
                    fiscal_q = int(_q_match.group(1)) if _q_match else None

                if fiscal_q is not None and fiscal_year is not None:
                    fiscal_period_label = f"Q{fiscal_q} FY{fiscal_year}"

            # Rows migrated from v2 have filing_date_sec = DATE(filing_date), which
            # may still be contaminated (comparison-period rows carry wrong dates).
            # Only run the contamination check when filing_date came from the legacy
            # filing_date column (sec_dates was empty) — v4 rows with a real EDGAR
            # date in filing_date_sec don't need this.
            _used_legacy_date = bool(filing_date and not sec_dates)
            if (
                _used_legacy_date
                and doc_type in quarterly_doc_types
                and filing_date is not None
                and period_end is not None
                and (
                    (filing_date.year >= selected_storage_year
                     and (filing_date - period_end).days > 180)
                    or filing_date < period_end
                )
            ):
                try:
                    _cal_rows = db_manager.execute_query_readonly(
                        """
                        SELECT earnings_date
                        FROM coreiq_nasdaq_earnings_calendar
                        WHERE ticker = :ticker
                          AND earnings_date BETWEEN :start AND :end
                        ORDER BY earnings_date ASC
                        LIMIT 1
                        """,
                        {
                            "ticker": ticker,
                            "start": period_end.isoformat(),
                            "end": (period_end + timedelta(days=120)).isoformat(),
                        },
                    )
                    filing_date = _to_date(_cal_rows[0]["earnings_date"]) if _cal_rows else None
                except Exception:
                    filing_date = None

            # Last resort: earnings calendar lookup when date still unknown.
            if filing_date is None and fiscal_q and fiscal_year and fiscal_q <= 4:
                try:
                    filing_date = EarningsCalendarRepository.get_earnings_date(
                        ticker,
                        int(fiscal_q),
                        int(fiscal_year),
                    )
                except Exception:
                    filing_date = None

            return {
                "filing_date": filing_date.isoformat() if hasattr(filing_date, "isoformat") else filing_date,
                "period_end": period_end.isoformat() if hasattr(period_end, "isoformat") else period_end,
                "report_fiscal_year": report_fiscal_year,
                "storage_year": selected_storage_year,
                "fiscal_q": fiscal_q,
                "fiscal_year": fiscal_year,
                "fiscal_period_label": fiscal_period_label,
            }
        except Exception as exc:
            log_structured_error(
                exc,
                page="repository",
                component="FilingMetricRepository",
                operation="get_header_metadata",
                context=f"ticker={ticker} effective_year={effective_year} doc_type={doc_type} filing_unit={filing_unit}",
            )
            return {}

    # ── Label priority for Python-side sorting (mirrors SQL ORDER BY CASE) ──
    _LABEL_PRIORITY: Dict[str, int] = {
        "net sales": 0, "total net sales": 0, "revenue": 0,
        "net revenue": 0, "revenues": 0, "total revenue": 0,
        "operating income": 1, "operating income (loss)": 1,
        "net income": 2, "net income (loss)": 2, "net earnings": 2,
        "total assets": 3, "cash and cash equivalents": 3,
        "ebitda": 4, "free cash flow": 4,
        "long-term debt": 5, "total debt": 5,
        "net debt": 6,
    }

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def _prefetch_filing_metrics(ticker: str, report_fiscal_year: int, doc_type: str) -> List[Dict[str, Any]]:
        """
        Fetch ALL numeric, non-TextBlock rows for one filing into memory (cached 5 min).

        Why: Every DB round-trip to Azure takes ~250ms (network RTT dominates).
        By pulling the full filing row-set ONCE with the fast BTREE index on
        (ticker, storage_year, doc_type) and caching it, all subsequent searches
        on the same filing run in Python — O(rows) in microseconds with zero
        additional DB hits.

        Returns raw dicts so they are cache-serialisable (no dataclass instances).
        """
        t0 = time.perf_counter()
        sql = f"""
            SELECT {FilingMetricRepository._SELECT_COLS_RAW}
            FROM coreiq_filing_metrics_v4
            WHERE ticker = :ticker
              AND COALESCE(fiscal_year, storage_year, report_fiscal_year) = :year
              AND doc_type = :doc_type
              AND numeric_value IS NOT NULL
              AND (standard_concept IS NULL OR standard_concept NOT LIKE '%Text Block')
        """
        rows = db_manager.execute_query_readonly(sql, {"ticker": ticker, "year": report_fiscal_year, "doc_type": doc_type})
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return rows

    @staticmethod
    def _python_search(rows: List[Dict[str, Any]], patterns: List[str], limit: int) -> List[FilingMetricResult]:
        """
        Filter pre-fetched filing rows in Python, dedup, and sort — zero DB I/O.

        Replicates the SQL logic:
          • WHERE: any pattern (stripped of %) is a substring of original_label,
                   standard_concept, or dimension_label (case-insensitive)
          • ROW_NUMBER: keep the latest period per (label, dimension, dimension_label)
          • ORDER BY: priority label CASE, then dimensioned/dim_label/len/alpha
        """
        terms = [p.strip("%").lower() for p in patterns]

        # ── Filter ───────────────────────────────────────────────────────────
        matched: List[Dict[str, Any]] = []
        for row in rows:
            label = (row.get("original_label") or "").lower()
            concept = (row.get("standard_concept") or "").lower()
            dim = (row.get("dimension_label") or "").lower()
            for term in terms:
                if term and (term in label or term in concept or term in dim):
                    matched.append(row)
                    break

        # ── Dedup: keep latest period per (label, dimension, dimension_label) ─
        best: Dict[tuple, Dict[str, Any]] = {}
        for row in matched:
            key = (
                row.get("original_label") or "",
                row.get("dimension") or "",
                row.get("dimension_label") or "",
            )
            period = str(row.get("period_end") or row.get("period_instant") or "")
            existing = best.get(key)
            if existing is None:
                best[key] = row
            else:
                existing_period = str(existing.get("period_end") or existing.get("period_instant") or "")
                if period > existing_period:
                    best[key] = row

        deduped = list(best.values())

        # ── Sort ─────────────────────────────────────────────────────────────
        priority = FilingMetricRepository._LABEL_PRIORITY

        def _sort_key(r: Dict[str, Any]) -> tuple:
            lbl = (r.get("original_label") or "").lower()
            return (
                priority.get(lbl, 10),
                1 if r.get("is_dimensioned") else 0,
                r.get("dimension_label") or "",
                len(r.get("original_label") or ""),
                r.get("original_label") or "",
            )

        deduped.sort(key=_sort_key)
        return FilingMetricRepository._rows_to_results(deduped[:limit])

    @staticmethod
    def _expand_query(query: str) -> List[str]:
        """Return list of LIKE patterns: original query + all synonym expansions."""
        q = query.strip().lower()
        patterns = [f"%{q}%"]
        for synonym in FilingMetricRepository.SYNONYM_MAP.get(q, []):
            patterns.append(f"%{synonym.lower()}%")
        return patterns

    @staticmethod
    def _build_fulltext_query(patterns: List[str]) -> str:
        """
        Convert LIKE patterns to a MySQL FULLTEXT boolean mode query string.
        Each '%term%' becomes 'term*' (prefix wildcard for word-boundary matching).
        Multi-word terms use phrase search "like this"*.
        Returns empty string if no usable terms (all too short or special-chars only).
        """
        terms = []
        for p in patterns:
            term = p.strip('%').strip()
            if len(term) < 3:
                continue
            # FULLTEXT boolean mode: phrase-match multi-word, prefix-match single word
            if ' ' in term:
                terms.append(f'"{term}"')
            else:
                terms.append(f'{term}*')
        # de-duplicate while preserving order
        seen: set = set()
        unique = []
        for t in terms:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return ' '.join(unique)

    # ── Shared ORDER BY / SELECT blocks ──────────────────────────────────────
    # Columns from the raw table (storage_year is the selected document bucket)
    _SELECT_COLS_RAW = """original_label, numeric_value, unit_ref,
                   COALESCE(fiscal_year, storage_year, report_fiscal_year) AS report_fiscal_year,
                   is_dimensioned, dimension_label, statement_type, ixbrl_id,
                   standard_concept, concept, balance, period_type,
                   period_start, period_end, period_instant,
                   value, source, llm_query, dimension, full_dimension_label"""

    # Columns from a subquery (report_fiscal_year already resolved by inner query)
    _SELECT_COLS = """original_label, numeric_value, unit_ref, report_fiscal_year,
                   is_dimensioned, dimension_label, statement_type, ixbrl_id,
                   standard_concept, concept, balance, period_type,
                   period_start, period_end, period_instant,
                   value, source, llm_query, dimension, full_dimension_label"""

    _ORDER_BY = """ORDER BY CASE LOWER(original_label)
                       WHEN 'net sales'               THEN 0
                       WHEN 'total net sales'          THEN 0
                       WHEN 'revenue'                  THEN 0
                       WHEN 'net revenue'               THEN 0
                       WHEN 'revenues'                 THEN 0
                       WHEN 'total revenue'             THEN 0
                       WHEN 'operating income'          THEN 1
                       WHEN 'operating income (loss)'   THEN 1
                       WHEN 'net income'                THEN 2
                       WHEN 'net income (loss)'         THEN 2
                       WHEN 'net earnings'              THEN 2
                       WHEN 'total assets'              THEN 3
                       WHEN 'cash and cash equivalents' THEN 3
                       WHEN 'ebitda'                    THEN 4
                       WHEN 'free cash flow'            THEN 4
                       WHEN 'long-term debt'            THEN 5
                       WHEN 'total debt'                THEN 5
                       WHEN 'net debt'                  THEN 6
                       ELSE 10
                     END ASC,
                     is_dimensioned ASC,
                     dimension_label ASC,
                     CHAR_LENGTH(original_label) ASC, original_label ASC"""

    @staticmethod
    def search(
        ticker: str,
        report_fiscal_year: int,
        doc_type: str,
        query: str,
        limit: int = 100,
    ) -> List[FilingMetricResult]:
        """
        Search filing metrics by original_label OR standard_concept OR dimension_label.

        Strategy (fast path first):
          1. Pre-fetch ALL numeric rows for this filing into Streamlit cache on
             first call (one ~250ms DB round-trip to Azure, then cached 5 min).
          2. Filter, dedup, and sort entirely in Python — 0ms for all subsequent
             searches on the same filing (different query terms, synonyms, etc.).
          3. DB fallback: if the cache is cold and Python search returns nothing,
             run the original FULLTEXT / LIKE query against the DB.
        """
        patterns = FilingMetricRepository._expand_query(query)

        # ── Fast path: Python search over cached prefetch ─────────────────────
        try:
            all_rows = FilingMetricRepository._prefetch_filing_metrics(ticker, report_fiscal_year, doc_type)
            if all_rows is not None:
                if not all_rows:
                    # Prefetch confirmed this filing has zero metrics in DB — skip
                    # the slow FULLTEXT/LIKE fallback (saves ~20s Azure round-trip).
                    return []
                results = FilingMetricRepository._python_search(all_rows, patterns, limit)
                if results:
                    return results
                # Filing has rows but this query matched nothing — try DB FULLTEXT
        except Exception as exc:
            pass  # logging removed

        # ── DB fallback: FULLTEXT then LIKE ───────────────────────────────────
        ft_query = FilingMetricRepository._build_fulltext_query(patterns)
        base_params: Dict[str, Any] = {
            "ticker": ticker,
            "year": report_fiscal_year,
            "doc_type": doc_type,
            "limit": limit,
        }

        if ft_query:
            ft_params = {**base_params, "ft_query": ft_query}
            ft_sql = f"""
                SELECT {FilingMetricRepository._SELECT_COLS}
                FROM (
                    SELECT {FilingMetricRepository._SELECT_COLS_RAW},
                           ROW_NUMBER() OVER (
                               PARTITION BY original_label,
                                            COALESCE(dimension, ''),
                                            COALESCE(dimension_label, '')
                               ORDER BY COALESCE(period_end, period_instant) DESC
                           ) AS rn
                    FROM coreiq_filing_metrics_v4
                    WHERE ticker = :ticker
                      AND COALESCE(fiscal_year, storage_year, report_fiscal_year) = :year
                      AND doc_type = :doc_type
                      AND MATCH(original_label, standard_concept, dimension_label)
                          AGAINST (:ft_query IN BOOLEAN MODE)
                      AND (standard_concept IS NULL OR standard_concept NOT LIKE '%Text Block')
                      AND numeric_value IS NOT NULL
                ) deduped
                WHERE rn = 1
                {FilingMetricRepository._ORDER_BY}
            """
            results = db_manager.execute_query_readonly(ft_sql, ft_params)
            if results:
                return FilingMetricRepository._rows_to_results(results)

        or_clauses = []
        like_params: Dict[str, Any] = {**base_params}
        for i, pattern in enumerate(patterns):
            k = f"q{i}"
            or_clauses.append(
                f"(LOWER(original_label) LIKE :{k} OR LOWER(standard_concept) LIKE :{k} OR LOWER(dimension_label) LIKE :{k})"
            )
            like_params[k] = pattern

        where_synonyms = " OR ".join(or_clauses)
        like_sql = f"""
            SELECT {FilingMetricRepository._SELECT_COLS}
            FROM (
                SELECT {FilingMetricRepository._SELECT_COLS_RAW},
                       ROW_NUMBER() OVER (
                           PARTITION BY original_label,
                                        COALESCE(dimension, ''),
                                        COALESCE(dimension_label, '')
                           ORDER BY COALESCE(period_end, period_instant) DESC
                       ) AS rn
                FROM coreiq_filing_metrics_v4
                WHERE ticker = :ticker
                  AND COALESCE(fiscal_year, storage_year, report_fiscal_year) = :year
                  AND doc_type = :doc_type
                  AND ({where_synonyms})
                  AND (standard_concept IS NULL OR standard_concept NOT LIKE '%Text Block')
                  AND numeric_value IS NOT NULL
            ) deduped
            WHERE rn = 1
            {FilingMetricRepository._ORDER_BY}
        """
        results = db_manager.execute_query_readonly(like_sql, like_params)
        return FilingMetricRepository._rows_to_results(results)

    @staticmethod
    def search_with_llm_fallback(
        ticker: str,
        report_fiscal_year: int,
        doc_type: str,
        query: str,
        limit: int = 20,
    ) -> tuple:
        """
        Search filing metrics; if DB returns no results, attempt LLM extraction.

        Returns:
            (results: List[FilingMetricResult], used_llm: bool)
            used_llm=True means LLM was called (show spinner before calling this)
        """
        db_results = FilingMetricRepository.search(
            ticker=ticker,
            report_fiscal_year=report_fiscal_year,
            doc_type=doc_type,
            query=query,
            limit=limit,
        )

        if db_results:
            return db_results, False

        # DB miss — try LLM extraction
        api_key = __import__("os").getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return [], False

        try:
            from core.llm_extractor import LLMExtractor
            from core.database import db_manager as _dbm

            engine = _dbm._engine
            if engine is None:
                return [], False

            with engine.begin() as conn:
                llm_results = LLMExtractor.extract(
                    conn=conn,
                    ticker=ticker,
                    report_fiscal_year=report_fiscal_year,
                    doc_type=doc_type,
                    query=query,
                )
            return llm_results, True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[LLM fallback] error: {e}")
            return [], False

# ---------------------------------------------------------------------------
# Helper SQL fragment: derive fiscal quarter (1-4) and year from
# fiscal_quarter_ending column which stores values like "Nov/2009".
# ---------------------------------------------------------------------------
_FQE_Q = """CASE SUBSTRING_INDEX(ec.fiscal_quarter_ending, '/', 1)
    WHEN 'Jan' THEN 1 WHEN 'Feb' THEN 1 WHEN 'Mar' THEN 1
    WHEN 'Apr' THEN 2 WHEN 'May' THEN 2 WHEN 'Jun' THEN 2
    WHEN 'Jul' THEN 3 WHEN 'Aug' THEN 3 WHEN 'Sep' THEN 3
    WHEN 'Oct' THEN 4 WHEN 'Nov' THEN 4 WHEN 'Dec' THEN 4
    ELSE NULL END"""

_FQE_YEAR = "CAST(SUBSTRING_INDEX(ec.fiscal_quarter_ending, '/', -1) AS UNSIGNED)"


class AVFinancialsEarningsRepository:
    """Alpha Vantage earnings history in `coreiq_av_financials_earnings`.

    Supports a bulk JSON payload (`quarterlyEarnings` / `quarterlyReports`) or
    normalized rows with `fiscal_date_ending` + `reported_date`. Used to resolve
    the actual announcement date (`reportedDate`) for a fiscal quarter end
    (`fiscalDateEnding`) before falling back to the NASDAQ/YF calendar tables.
    """

    @staticmethod
    def _parse_av_date(val: Any) -> Optional[date]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        s = str(val).strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)
    def get_earnings_date_pair_for_fiscal_quarter_ends(
        ticker: str,
        fiscal_quarter_end_iso_tuple: Tuple[str, ...],
    ) -> Optional[Tuple[date, date]]:
        """Return (report_date, fiscal_period_end_date) when AV history matches a candidate quarter end."""
        if not ticker or not fiscal_quarter_end_iso_tuple:
            return None
        try:
            end_set = frozenset(date.fromisoformat(s) for s in fiscal_quarter_end_iso_tuple)
        except ValueError:
            return None

        date_list = sorted(end_set)
        placeholders = ", ".join(f":fd{i}" for i in range(len(date_list)))
        params: Dict[str, Any] = {"ticker": ticker}
        for i, d in enumerate(date_list):
            params[f"fd{i}"] = d.isoformat()

        norm_rows = db_manager.execute_query_readonly(
            f"""
            SELECT reported_date, fiscal_date_ending
            FROM coreiq_av_financials_earnings
            WHERE ticker = :ticker
              AND fiscal_date_ending IN ({placeholders})
              AND (
                    LOWER(COALESCE(report_type, '')) IN ('quarterly', 'q')
                    OR COALESCE(report_type, '') = ''
                  )
            ORDER BY ingested_at DESC
            LIMIT 1
            """,
            params,
        )
        if norm_rows:
            rd_raw = norm_rows[0].get("reported_date")
            if rd_raw is not None:
                if hasattr(rd_raw, "date"):
                    rd_raw = rd_raw.date()
            fde_raw = norm_rows[0].get("fiscal_date_ending")
            rd = AVFinancialsEarningsRepository._parse_av_date(rd_raw)
            fde = AVFinancialsEarningsRepository._parse_av_date(fde_raw)
            if rd and fde and fde in end_set:
                return (rd, fde)

        blob_rows = db_manager.execute_query_readonly(
            """
            SELECT payload_json
            FROM coreiq_av_financials_earnings
            WHERE ticker = :ticker
              AND payload_json IS NOT NULL
              AND payload_json != ''
            ORDER BY ingested_at DESC
            LIMIT 1
            """,
            {"ticker": ticker},
        )
        if not blob_rows:
            return None
        raw = blob_rows[0].get("payload_json")
        if not raw:
            return None
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None

        quarterly: Optional[List[Any]] = None
        for key in ("quarterlyEarnings", "quarterly_earnings", "quarterlyReports"):
            block = payload.get(key)
            if isinstance(block, list):
                quarterly = block
                break
        if not quarterly:
            return None

        for entry in quarterly:
            if not isinstance(entry, dict):
                continue
            fde = AVFinancialsEarningsRepository._parse_av_date(
                entry.get("fiscalDateEnding") or entry.get("fiscal_date_ending")
            )
            rd = AVFinancialsEarningsRepository._parse_av_date(
                entry.get("reportedDate") or entry.get("reported_date")
            )
            if fde and rd and fde in end_set:
                return (rd, fde)
        return None


class EarningsCalendarRepository:
    """
    Repository for coreiq_nasdaq_earnings_calendar table.

    fiscal_quarter_ending is stored as 'Mon/YYYY' (e.g. 'Sep/2025').
    Quarter is derived: Jan-Mar=Q1, Apr-Jun=Q2, Jul-Sep=Q3, Oct-Dec=Q4.
    Transcripts are joined via (ticker, derived q, derived year).
    """

    @staticmethod
    def _pick_ir_url_from_company_ir_row(row: Dict[str, Any]) -> Optional[str]:
        """IR / webcast link: use `ir_website_url` only (single column for all companies)."""
        if not row:
            return None
        lower_map = {str(k).lower(): v for k, v in row.items()}
        v = lower_map.get("ir_website_url")
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @staticmethod
    def _ir_row_confidence(row: Dict[str, Any]) -> float:
        """Best-effort score for choosing one IR row when ticker-only fallback is needed."""
        if not row:
            return 0.0
        lower_map = {str(k).lower(): v for k, v in row.items()}
        raw_score = lower_map.get("confidence_score") or lower_map.get("confidence") or 0
        try:
            score = float(raw_score or 0)
        except (TypeError, ValueError):
            score = 0.0
        if lower_map.get("last_error"):
            score -= 0.25
        if lower_map.get("ir_website_url") or lower_map.get("investor_relations_url"):
            score += 0.25
        return score

    @staticmethod
    @staticmethod
    def _normalize_ir_company_source(val: Optional[Any]) -> Optional[str]:
        """Canonical `company_source` on IR rows (case-insensitive)."""
        if val is None:
            return None
        s = str(val).strip()
        if not s:
            return None
        sl = s.lower().replace(" ", "").replace("_", "")
        if sl == "sec":
            return "SEC"
        if sl in ("yfinance", "yahoofinance", "yf"):
            return "YFinance"
        return None

    @staticmethod
    def _calendar_data_source_to_ir_company_source(data_source: Optional[str]) -> Optional[str]:
        """NASDAQ calendar rows -> SEC; YF rows -> YFinance."""
        if data_source == "nasdaq":
            return "SEC"
        if data_source == "yf":
            return "YFinance"
        return None

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)
    def _build_ir_website_lookup() -> Dict[Tuple[str, str], str]:
        """
        (ticker_upper, company_source) -> IR URL.

        IR `company_source` must normalize to SEC (NASDAQ/Nasdaq calendar lineage)
        or YFinance (Yahoo Finance calendar lineage). Rows without `company_source`
        are skipped.

        Tries `coreiq_ir_websites` then `company_ir_websites` (same as before).
        """
        rows: List[Dict[str, Any]] = []
        ir_table_errors: List[Tuple[str, Exception]] = []
        for table_name in ("coreiq_ir_websites", "company_ir_websites"):
            try:
                table_rows = db_manager.execute_query_readonly(
                    f"""
                    SELECT ticker, company_source, ir_website_url,
                           confidence_score, last_error
                    FROM {table_name}
                    WHERE ticker IS NOT NULL
                      AND TRIM(ticker) != ''
                    """,
                    {},
                )
                rows.extend(table_rows)
            except Exception as exc:
                ir_table_errors.append((table_name, exc))

        if not rows:
            for table_name, exc in ir_table_errors:
                log_structured_error(exc, page="repository", component="EarningsCalendarRepository",
                                     operation="_build_ir_website_lookup",
                                     context=f"IR table lookup skipped for {table_name}")

        best: Dict[Tuple[str, str], Tuple[float, str]] = {}
        for r in rows:
            t_raw = r.get("ticker")
            if t_raw is None:
                continue
            t_key = str(t_raw).strip().upper()
            if not t_key:
                continue
            lower_map = {str(k).lower(): v for k, v in r.items()}
            src = EarningsCalendarRepository._normalize_ir_company_source(
                lower_map.get("company_source")
            )
            if not src:
                continue
            url = EarningsCalendarRepository._pick_ir_url_from_company_ir_row(r)
            if not url:
                continue
            pair_key = (t_key, src)
            score = EarningsCalendarRepository._ir_row_confidence(r)
            current = best.get(pair_key)
            if current is None or score >= current[0]:
                best[pair_key] = (score, url)

        return {key: url for key, (_score, url) in best.items()}

    @staticmethod
    def _resolve_ir_website_url(
        lookup: Dict[Tuple[str, str], str],
        ticker: str,
        calendar_data_source: Optional[str],
    ) -> Optional[str]:
        """IR URL for this calendar row using ticker + calendar source (SEC vs YFinance)."""
        t_key = str(ticker).strip().upper()
        if not t_key:
            return None
        src = EarningsCalendarRepository._calendar_data_source_to_ir_company_source(
            calendar_data_source
        )
        if not src:
            return None
        return lookup.get((t_key, src))

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_calendar_events(
        tickers: Optional[tuple] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return earnings events for calendar rendering — one row per
        (ticker, fiscal_quarter_ending).

        Dedup rule across the combined NASDAQ + YF dataset:
        1. Keep the row with the LATEST fetched_at_utc for each quarter
           (the freshest fetch — corrects stale earnings_date when the source
           re-published a revised date for the same fiscal quarter).
        2. Tie-break: prefer nasdaq over yf, then latest earnings_date, then id.

        Returns list of dicts with keys:
            id, ticker, company_name, earnings_date, report_time,
            fiscal_q, report_fiscal_year, fiscal_quarter_ending,
            eps_actual, eps_forecast, surprise_pct, market_cap,
            num_estimates, beat_miss, ir_website_url (from IR tables `ir_website_url` column only)
        """
        params: Dict[str, Any] = {}

        # Build WHERE clauses for both sub-queries
        ticker_clause_nasdaq = ""
        ticker_clause_yf = ""
        date_clause_nasdaq = ""
        date_clause_yf = ""

        if tickers:
            in_clause = ", ".join(f":t{i}" for i in range(len(tickers)))
            for i, t in enumerate(tickers):
                params[f"t{i}"] = t
            ticker_clause_nasdaq = f"AND ec.ticker IN ({in_clause})"
            ticker_clause_yf = f"AND yf.ticker IN ({in_clause})"

        if start_date:
            params["start_date"] = start_date.isoformat()
            date_clause_nasdaq = "AND ec.earnings_date >= :start_date"
            date_clause_yf = "AND yf.earnings_date >= :start_date"

        if end_date:
            params["end_date"] = end_date.isoformat()
            date_clause_nasdaq = date_clause_nasdaq + " AND ec.earnings_date <= :end_date"
            date_clause_yf = date_clause_yf + " AND yf.earnings_date <= :end_date"

        # UNION query combining NASDAQ and YF earnings calendar data, deduped:
        # one row per (ticker, fiscal_quarter_ending), keeping the latest
        # earnings_date with deterministic tie-break (nasdaq > yf, then highest id).
        query = f"""
            SELECT id, ticker, calendar_company_name, earnings_date, report_time,
                   fiscal_quarter_ending, fiscal_q, report_fiscal_year,
                   eps_actual, eps_forecast, surprise_pct, market_cap,
                   num_estimates, data_source
            FROM (
                SELECT
                    id, ticker, calendar_company_name, earnings_date, report_time,
                    fiscal_quarter_ending, fiscal_q, report_fiscal_year,
                    eps_actual, eps_forecast, surprise_pct, market_cap,
                    num_estimates, data_source,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker, fiscal_quarter_ending
                        ORDER BY fetched_at_utc DESC,
                                 CASE data_source WHEN 'nasdaq' THEN 0 ELSE 1 END ASC,
                                 earnings_date DESC,
                                 id DESC
                    ) AS rn
                FROM (
                    SELECT
                        ec.id,
                        ec.ticker,
                        ec.company_name                    AS calendar_company_name,
                        ec.earnings_date,
                        ec.report_time,
                        ec.fiscal_quarter_ending,
                        {_FQE_Q}                           AS fiscal_q,
                        {_FQE_YEAR}                        AS report_fiscal_year,
                        ec.eps_actual,
                        ec.eps_forecast,
                        ec.surprise_pct,
                        ec.market_cap,
                        ec.num_estimates,
                        ec.fetched_at_utc                  AS fetched_at_utc,
                        'nasdaq'                           AS data_source
                    FROM coreiq_nasdaq_earnings_calendar ec
                    WHERE ec.fiscal_quarter_ending IS NOT NULL
                      {ticker_clause_nasdaq}
                      {date_clause_nasdaq}

                    UNION ALL

                    SELECT
                        yf.id + 10000000                   AS id,
                        yf.ticker,
                        yf.company_name                    AS calendar_company_name,
                        yf.earnings_date,
                        'time-not-supplied'                AS report_time,
                        CONCAT(DATE_FORMAT(yf.earnings_date, '%b'), '/', YEAR(yf.earnings_date))
                                                           AS fiscal_quarter_ending,
                        QUARTER(yf.earnings_date)          AS fiscal_q,
                        YEAR(yf.earnings_date)             AS report_fiscal_year,
                        yf.reported_eps                    AS eps_actual,
                        yf.eps_estimate                    AS eps_forecast,
                        yf.surprise_pct,
                        NULL                               AS market_cap,
                        NULL                               AS num_estimates,
                        yf.ingested_at                     AS fetched_at_utc,
                        'yf'                               AS data_source
                    FROM coreiq_yf_earnings_calendar yf
                    WHERE yf.ticker IS NOT NULL
                      {ticker_clause_yf}
                      {date_clause_yf}
                ) combined
            ) ranked
            WHERE rn = 1
        """
        # RAISES on timeout/error — @st.cache_data will not cache error states
        rows = db_manager.execute_query_readonly_raising(query, params)
        if not rows:
            try:
                from utils.server_logger import log_warning as _lw_opt
                _lw_opt(
                    f"[EC_OPT] DB_EMPTY_VALIDATED | get_calendar_events"
                    f" | tickers={tickers} start={start_date} end={end_date}"
                )
            except Exception:
                pass
        ir_lookup = EarningsCalendarRepository._build_ir_website_lookup()

        # Load cached companies map for name lookup (FAST - no DB query!)
        companies_map = CompanyRepository.get_companies_map()

        # Load fiscal year end months per ticker from AV/YF overview.
        # Used to correctly compute fiscal quarter number and fiscal year for
        # companies whose fiscal year does NOT end in December.
        # e.g. M and LULU have FYE = January: "Jan/2026" = Q4 of FY2025, not Q1 2026.
        fye_map = EarningsCalendarRepository._get_fiscal_year_end_map()

        _MONTH_NAME_TO_NUM = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        _MONTH_ABB_TO_NUM = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        _MONTH_NUM_TO_ABB = {
            1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
            7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
        }

        def _derive_yf_fiscal_quarter_ending(earnings_date_value, fye_month: int) -> Optional[str]:
            """
            YF calendar rows do not store fiscal_quarter_ending, so the SQL
            initially uses the earnings month as a placeholder. Replace that
            with the latest fiscal quarter-end month before the earnings date.
            """
            if not fye_month:
                return None

            try:
                if isinstance(earnings_date_value, datetime):
                    earnings_dt = earnings_date_value.date()
                elif isinstance(earnings_date_value, date):
                    earnings_dt = earnings_date_value
                else:
                    earnings_dt = date.fromisoformat(str(earnings_date_value)[:10])
            except (TypeError, ValueError):
                return None

            quarter_end_months = {
                ((fye_month - offset - 1) % 12) + 1
                for offset in (0, 3, 6, 9)
            }
            candidates = []
            for year in (earnings_dt.year - 1, earnings_dt.year):
                for month in quarter_end_months:
                    last_day = calendar.monthrange(year, month)[1]
                    quarter_end = date(year, month, last_day)
                    if quarter_end <= earnings_dt:
                        candidates.append(quarter_end)

            if not candidates:
                return None

            quarter_end = max(candidates)
            return f"{_MONTH_NUM_TO_ABB[quarter_end.month]}/{quarter_end.year}"

        def _correct_fiscal_q_year(ticker: str, raw_q, raw_year, fiscal_quarter_ending: str):
            """
            Re-derive fiscal quarter (1-4) and fiscal year using the company's
            actual fiscal year end month from company overview data.

            Falls back to the raw SQL-derived values for tickers without FYE data
            (preserves existing behaviour for standard Dec FY companies).
            """
            fye_name = fye_map.get(ticker)
            if not fye_name:
                # No FYE data — keep what the SQL computed
                return (int(raw_q) if raw_q else None,
                        int(raw_year) if raw_year else None)

            fye_month = _MONTH_NAME_TO_NUM.get(fye_name.lower())
            if not fye_month:
                return (int(raw_q) if raw_q else None,
                        int(raw_year) if raw_year else None)

            # Parse fqe_month from fiscal_quarter_ending (format: 'Mon/YYYY', e.g. 'Jan/2026')
            if not fiscal_quarter_ending or "/" not in fiscal_quarter_ending:
                return (int(raw_q) if raw_q else None,
                        int(raw_year) if raw_year else None)
            try:
                fqe_parts = fiscal_quarter_ending.split("/")
                fqe_month = _MONTH_ABB_TO_NUM.get(fqe_parts[0].strip().lower())
                fqe_year  = int(fqe_parts[1].strip())
            except (IndexError, ValueError):
                return (int(raw_q) if raw_q else None,
                        int(raw_year) if raw_year else None)

            if not fqe_month:
                return (int(raw_q) if raw_q else None,
                        int(raw_year) if raw_year else None)

            # Fiscal year start = month after fiscal year end
            fy_start_month = (fye_month % 12) + 1

            if fye_month == 12:
                # December FYE: label = calendar year (Jan–Dec)
                fiscal_year = fqe_year
            else:
                # All other FYE months (including January): label = end year
                # e.g. MSFT (June FYE): FY2026 = Jul 2025 – Jun 2026
                # e.g. WDAY (January FYE): FY2026 = Feb 2025 – Jan 2026
                if fqe_month >= fy_start_month:
                    fiscal_year = fqe_year + 1
                else:
                    fiscal_year = fqe_year

            # Fiscal quarter: how many months into the fiscal year does this quarter end?
            months_into_fy = (fqe_month - fy_start_month) % 12 + 1
            fiscal_q = (months_into_fy + 2) // 3  # ceil(months_into_fy / 3)

            return (fiscal_q, fiscal_year)

        def _parse_event_date(value) -> Optional[date]:
            try:
                if isinstance(value, datetime):
                    return value.date()
                if isinstance(value, date):
                    return value
                return date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                return None

        def _dedupe_close_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """
            After fiscal-period correction, YF and NASDAQ can represent the same
            event under different raw quarter-end labels. If the same ticker and
            fiscal period has multiple events within 30 days, keep the latest.
            """
            grouped: Dict[Tuple[Any, Any, Any], List[Dict[str, Any]]] = {}
            passthrough: List[Dict[str, Any]] = []

            for event in events:
                key = (
                    event.get("ticker"),
                    event.get("fiscal_q"),
                    event.get("report_fiscal_year"),
                )
                event_date = _parse_event_date(event.get("earnings_date"))
                if not all(key) or not event_date:
                    passthrough.append(event)
                    continue
                grouped.setdefault(key, []).append(event)

            deduped: List[Dict[str, Any]] = []
            for group_events in grouped.values():
                kept: List[Dict[str, Any]] = []
                for event in sorted(
                    group_events,
                    key=lambda e: _parse_event_date(e.get("earnings_date")) or date.min,
                    reverse=True,
                ):
                    event_date = _parse_event_date(event.get("earnings_date"))
                    if event_date and any(
                        abs((event_date - kept_date).days) <= 30
                        for kept_event in kept
                        for kept_date in [_parse_event_date(kept_event.get("earnings_date"))]
                        if kept_date
                    ):
                        continue
                    kept.append(event)
                deduped.extend(kept)

            return sorted(
                deduped + passthrough,
                key=lambda e: (_parse_event_date(e.get("earnings_date")) or date.min, str(e.get("ticker") or "")),
            )

        results = []
        for row in rows:
            eps_a = float(row["eps_actual"]) if row["eps_actual"] is not None else None
            eps_f = float(row["eps_forecast"]) if row["eps_forecast"] is not None else None

            if eps_a is not None and eps_f is not None:
                diff = eps_a - eps_f
                if abs(diff) < 0.005:
                    beat_miss = "met"
                elif diff > 0:
                    beat_miss = "beat"
                else:
                    beat_miss = "miss"
            else:
                beat_miss = "no_data"

            # Use cached company name if available, fallback to calendar's company_name or ticker
            ticker = row["ticker"]
            company_info = companies_map.get(ticker, {})
            company_name = (
                company_info.get('name_coresight')
                or company_info.get('name')
                or row["calendar_company_name"]
                or ticker
            )

            # Correct fiscal quarter and year using company-specific FYE
            fiscal_quarter_ending = row["fiscal_quarter_ending"] or ""
            if row.get("data_source") == "yf":
                fye_name = fye_map.get(ticker)
                fye_month = _MONTH_NAME_TO_NUM.get(str(fye_name).lower()) if fye_name else None
                derived_fqe = _derive_yf_fiscal_quarter_ending(row.get("earnings_date"), fye_month) if fye_month else None
                if derived_fqe:
                    fiscal_quarter_ending = derived_fqe

            fiscal_q, report_fiscal_year = _correct_fiscal_q_year(
                ticker,
                row["fiscal_q"],
                row["report_fiscal_year"],
                fiscal_quarter_ending,
            )
            ir_website_url = EarningsCalendarRepository._resolve_ir_website_url(
                ir_lookup, ticker, row.get("data_source")
            )

            results.append({
                "id": row["id"],
                "ticker": ticker,
                "company_name": company_name,
                "earnings_date": row["earnings_date"].isoformat() if row["earnings_date"] else None,
                "report_time": row["report_time"] or "time-not-supplied",
                "fiscal_quarter_ending": fiscal_quarter_ending,
                "fiscal_q": fiscal_q,
                "report_fiscal_year": report_fiscal_year,
                "eps_actual": eps_a,
                "eps_forecast": eps_f,
                "surprise_pct": float(row["surprise_pct"]) if row["surprise_pct"] is not None else None,
                "market_cap": int(row["market_cap"]) if row["market_cap"] is not None else None,
                "num_estimates": int(row["num_estimates"]) if row["num_estimates"] is not None else None,
                "beat_miss": beat_miss,
                "ir_website_url": ir_website_url,
            })
        return _dedupe_close_events(results)

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)
    def _get_fiscal_year_end_map() -> Dict[str, str]:
        """
        Return {ticker: fiscal_year_end_month_name} from AV and YF overviews.
        e.g. {'M': 'January', 'LULU': 'January', 'AAPL': 'September'}
        Used to correctly label fiscal quarters for non-December FY companies.
        Cached for 1 hour — fiscal year ends rarely change.
        """
        try:
            av_rows = db_manager.execute_query_readonly(
                """
                SELECT ticker, fiscal_year_end
                FROM coreiq_av_company_overview
                WHERE ticker IS NOT NULL
                  AND fiscal_year_end IS NOT NULL
                  AND fiscal_year_end != ''
                """,
                {},
            )
            fye_map = {
                r["ticker"]: r["fiscal_year_end"]
                for r in av_rows
                if r.get("ticker") and r.get("fiscal_year_end")
            }

            try:
                yf_rows = db_manager.execute_query_readonly(
                    """
                    SELECT ticker, payload_json
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
                          AND payload_json != ''
                    ) ranked
                    WHERE rn = 1
                    """,
                    {},
                )
                for row in yf_rows:
                    ticker = row.get("ticker")
                    fiscal_year_end = _extract_yf_fiscal_year_end(row.get("payload_json"))
                    if ticker and fiscal_year_end:
                        fye_map.setdefault(ticker, fiscal_year_end)
            except Exception as yf_exc:
                log_structured_error(yf_exc, page="repository", component="EarningsCalendarRepository",
                                     operation="_get_fiscal_year_end_map_yf",
                                     context="YF fiscal year end map fetch failed; using AV-only map")

            return fye_map
        except Exception as exc:
            log_structured_error(exc, page="repository", component="EarningsCalendarRepository",
                                 operation="_get_fiscal_year_end_map",
                                 context="fiscal year end map fetch failed — falling back to calendar quarters")
            return {}

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_available_tickers() -> List[Dict[str, str]]:
        """Return distinct tickers + company names from both calendar tables.

        PERFORMANCE: Uses cached companies_map for name lookup — zero extra DB join.
        Two light index-only scans on both calendar tables (UNION deduplicates).
        """
        import time as _t
        _t0 = _t.perf_counter()

        # Fast UNION — both tables have idx_ticker (index-only scans)
        query = """
            SELECT DISTINCT ticker, company_name
            FROM coreiq_nasdaq_earnings_calendar
            WHERE fiscal_quarter_ending IS NOT NULL
              AND ticker IS NOT NULL

            UNION ALL

            SELECT DISTINCT ticker, company_name
            FROM coreiq_yf_earnings_calendar
            WHERE ticker IS NOT NULL
        """
        # RAISES on timeout/error — prevents @st.cache_data from caching []
        rows = db_manager.execute_query_readonly_raising(query, {})
        _db_ms = (_t.perf_counter() - _t0) * 1000

        # Use cached companies_map for name enrichment (FAST — no DB query)
        companies_map = CompanyRepository.get_companies_map()

        seen: Dict[str, Dict[str, str]] = {}
        for row in rows:
            ticker = row["ticker"]
            if not ticker or ticker in seen:
                continue
            company_info = companies_map.get(ticker, {})
            name = (
                company_info.get("name_coresight")
                or company_info.get("name")
                or row["company_name"]
                or ticker
            )
            seen[ticker] = {"ticker": ticker, "name": name}

        result = sorted(seen.values(), key=lambda x: x["name"])
        _total_ms = (_t.perf_counter() - _t0) * 1000
        return result

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_date_range() -> tuple:
        """Return (min_date, max_date) of earnings_date across both calendar tables."""
        # RAISES on timeout/error — prevents @st.cache_data from caching (None, None)
        row = db_manager.execute_query_readonly_raising(
            """
            SELECT
                LEAST(
                    (SELECT MIN(earnings_date) FROM coreiq_nasdaq_earnings_calendar),
                    (SELECT MIN(earnings_date) FROM coreiq_yf_earnings_calendar)
                ) AS mn,
                GREATEST(
                    (SELECT MAX(earnings_date) FROM coreiq_nasdaq_earnings_calendar),
                    (SELECT MAX(earnings_date) FROM coreiq_yf_earnings_calendar)
                ) AS mx
            """,
            {}
        )
        if row and row[0]["mn"]:
            return row[0]["mn"], row[0]["mx"]
        return None, None

    @staticmethod
    def _fqe_strings_to_unique_end_dates(fqe_strings: List[str]) -> Tuple[date, ...]:
        month_abb_to_num = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        out: List[date] = []
        for fqe in fqe_strings:
            if not fqe or "/" not in fqe:
                continue
            try:
                mt, yr_text = fqe.split("/", 1)
                m = month_abb_to_num.get(mt.strip().lower())
                yr = int(yr_text.strip())
                if not m:
                    continue
                ld = calendar.monthrange(yr, m)[1]
                out.append(date(yr, m, ld))
            except (ValueError, IndexError):
                continue
        return tuple(sorted(set(out)))

    @staticmethod
    def _fiscal_period_end_date_for_nasdaq_announcement(
        ticker: str,
        announcement_date: Any,
    ) -> Optional[date]:
        """Period-close date from NASDAQ calendar row matching this announcement date, if any."""
        if not ticker or not announcement_date:
            return None
        ed_str = announcement_date.isoformat() if hasattr(announcement_date, "isoformat") else str(announcement_date)[:10]
        rows = db_manager.execute_query_readonly(
            """
            SELECT fiscal_quarter_ending
            FROM coreiq_nasdaq_earnings_calendar
            WHERE ticker = :ticker
              AND earnings_date = :ed
              AND fiscal_quarter_ending IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            {"ticker": ticker, "ed": ed_str},
        )
        if not rows or not rows[0].get("fiscal_quarter_ending"):
            return None
        parsed = EarningsCalendarRepository._fqe_strings_to_unique_end_dates([rows[0]["fiscal_quarter_ending"]])
        return parsed[0] if parsed else None

    @staticmethod
    def _candidate_fiscal_quarter_end_dates_for_transcript(
        fiscal_q: int,
        report_fiscal_year: int,
        fye_month: int,
        transcript_rows: Optional[List[Dict[str, Any]]],
    ) -> Tuple[date, ...]:
        """Calendar quarter-end dates (last day of month) matching transcript fiscal_q/FY labels."""
        _MONTH_NUM_TO_ABB = {
            1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
            7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
        }

        if fye_month <= 2:
            fy_start_month = (fye_month % 12) + 1
            raw_offset = fy_start_month - 1 + fiscal_q * 3 - 1
            fqe_m = (raw_offset % 12) + 1
            yr_bump = raw_offset // 12
            m_abb = _MONTH_NUM_TO_ABB[fqe_m]
            fqe_str_a = f"{m_abb}/{report_fiscal_year - 1 + yr_bump}"
            fqe_str_b = f"{m_abb}/{report_fiscal_year + yr_bump}"
            base_month = (fiscal_q - 1) * 3 + 1
            calendar_fqe_month = base_month + (fye_month - base_month) % 3
            calendar_fqe = f"{_MONTH_NUM_TO_ABB[calendar_fqe_month]}/{report_fiscal_year}"
            fqe_strings: List[str] = [fqe_str_a, fqe_str_b, calendar_fqe]
        elif fye_month == 3:
            fy_start_month = 10
            raw_offset = fy_start_month - 1 + fiscal_q * 3 - 1
            fqe_m = (raw_offset % 12) + 1
            fqe_y = (report_fiscal_year - 1) + (raw_offset // 12)
            fqe_strings = [f"{_MONTH_NUM_TO_ABB[fqe_m]}/{fqe_y}"]
        elif fye_month <= 8:
            base = (fiscal_q - 1) * 3 + 1
            fqe_m = base + (fye_month - base) % 3
            fqe_strings = [f"{_MONTH_NUM_TO_ABB[fqe_m]}/{report_fiscal_year}"]
        elif fye_month <= 11:
            fy_start_month = fye_month + 1
            raw_offset = fy_start_month - 1 + fiscal_q * 3 - 1
            fqe_m = (raw_offset % 12) + 1
            fqe_y = (report_fiscal_year - 1) + (raw_offset // 12)
            fqe_strings = [f"{_MONTH_NUM_TO_ABB[fqe_m]}/{fqe_y}"]
        else:
            raw_offset = fiscal_q * 3 - 1
            fqe_m = (raw_offset % 12) + 1
            fqe_y = report_fiscal_year + (raw_offset // 12)
            fqe_strings = [f"{_MONTH_NUM_TO_ABB[fqe_m]}/{fqe_y}"]

        if transcript_rows:
            base_month = (fiscal_q - 1) * 3 + 1
            calendar_fqe_month = base_month + (fye_month - base_month) % 3
            calendar_fqe = f"{_MONTH_NUM_TO_ABB[calendar_fqe_month]}/{report_fiscal_year}"

            standard_fy_start_month = (fye_month % 12) + 1
            standard_raw_offset = standard_fy_start_month - 1 + fiscal_q * 3 - 1
            standard_fqe_month = (standard_raw_offset % 12) + 1
            standard_fye_year = (report_fiscal_year - 1) + (standard_raw_offset // 12)
            standard_fqe = f"{_MONTH_NUM_TO_ABB[standard_fqe_month]}/{standard_fye_year}"

            fqe_strings.extend([calendar_fqe, standard_fqe])

        deduped = list(dict.fromkeys(fqe_strings))
        return EarningsCalendarRepository._fqe_strings_to_unique_end_dates(deduped)

    @staticmethod
    def _resolve_calendar_earnings_announcement_date(
        ticker: str,
        fiscal_q: int,
        report_fiscal_year: int,
        fye_month: int,
        transcript_rows: Optional[List[Dict[str, Any]]],
        _MONTH_NUM_TO_ABB: Dict[int, str],
    ) -> "Optional[date]":
        """NASDAQ/YF earnings announcement date only (no `coreiq_av_financials_earnings`)."""
        # Derive fiscal_quarter_ending (e.g. "Mar/2026") from fiscal_q + report_fiscal_year + FYE.
        #
        # AV uses different quarter-labeling conventions depending on FYE month:
        #
        #   Jan/Feb FYE — fiscal year sequential quarters, but the fiscal year LABEL
        #     differs by company: some use "FY starts" labeling, others "FY ends".
        #     Rather than hardcoding per-company, we compute BOTH candidate fqe strings
        #     (they share the same quarter-end month, differ only in year by 1) and use
        #     the transcript's has_transcript flag to pick the right one at query time:
        #       has_transcript=1 → real call already happened → MAX past NASDAQ date
        #       has_transcript=0 → upcoming placeholder      → MIN future NASDAQ date
        #
        #   Mar–Aug FYE — AV uses CALENDAR QUARTER convention.
        #     year = calendar year, q maps to Jan-Mar / Apr-Jun / Jul-Sep / Oct-Dec.
        #     fqe_month = base + (fye_month - base) % 3  where base = (q-1)*3 + 1
        #
        #   Sep–Nov FYE — fiscal year sequential, FY label = year FY ends.
        #
        #   Dec FYE — fiscal year = calendar year.

        if fye_month <= 2:
            # Jan/Feb FYE: quarter-end month is fixed; only the year is ambiguous.
            fy_start_month = (fye_month % 12) + 1      # 2 for Jan FYE, 3 for Feb FYE
            raw_offset = fy_start_month - 1 + fiscal_q * 3 - 1
            fqe_month = (raw_offset % 12) + 1
            yr_bump   = raw_offset // 12
            m_abb     = _MONTH_NUM_TO_ABB[fqe_month]

            # Two candidates differing by 1 year
            fqe_str_a = f"{m_abb}/{report_fiscal_year - 1 + yr_bump}"  # "FY ends" convention
            fqe_str_b = f"{m_abb}/{report_fiscal_year     + yr_bump}"  # "FY starts" convention
            base_month = (fiscal_q - 1) * 3 + 1
            calendar_fqe_month = base_month + (fye_month - base_month) % 3
            calendar_fqe = f"{_MONTH_NUM_TO_ABB[calendar_fqe_month]}/{report_fiscal_year}"
            # For each candidate fqe_str, derive which fiscal year it belongs to.
            # Only keep candidates whose derived fiscal year matches report_fiscal_year,
            # so that for Jan/Feb FYE companies we don't accidentally pick up the
            # NEXT year's earnings call (e.g. WMT 2026Q1 should resolve to May 2025,
            # not May 2026 which belongs to Q1 FY2027).
            def _derived_fy(fqe_str: str) -> int:
                try:
                    m_part, y_part = fqe_str.split("/")
                    m_num = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                             "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}[m_part]
                    y_num = int(y_part)
                    return y_num + 1 if m_num >= fy_start_month else y_num
                except Exception:
                    return 0

            # Exclude calendar_fqe for Jan/Feb FYE — for these companies the
            # calendar quarter-end month equals the FYE month itself (e.g. "Jan/2026"
            # for WMT Q1), which maps to Q4 not Q1 and produces wrong date matches.
            all_candidates = list(dict.fromkeys([fqe_str_a, fqe_str_b]))
            filtered = [c for c in all_candidates if _derived_fy(c) == report_fiscal_year]
            candidate_fqes = filtered if filtered else all_candidates
            fqe_params = {
                f"candidate_{idx}": candidate
                for idx, candidate in enumerate(candidate_fqes)
            }
            fqe_clause = ", ".join(f":candidate_{idx}" for idx in range(len(candidate_fqes)))

            has_real = bool(transcript_rows and transcript_rows[0]["has_transcript"])

            today_str = date.today().isoformat()
            # YF fallback window: use the later candidate's quarter-end date
            fqe_year_w = report_fiscal_year + yr_bump
            last_day_w = calendar.monthrange(fqe_year_w, fqe_month)[1]
            fqe_window_start = (date(fqe_year_w, fqe_month, last_day_w) - timedelta(days=15)).isoformat()
            fqe_window_end   = (date(fqe_year_w, fqe_month, last_day_w) + timedelta(days=90)).isoformat()

            if has_real:
                # Real transcript: most recently completed call for this quarter-month
                query = f"""
                    SELECT earnings_date FROM (
                        SELECT MAX(ec.earnings_date) AS earnings_date, 1 AS priority
                        FROM coreiq_nasdaq_earnings_calendar ec
                        WHERE ec.ticker = :ticker
                          AND ec.fiscal_quarter_ending IN ({fqe_clause})
                          AND ec.earnings_date <= :today
                        UNION ALL
                        SELECT yf.earnings_date, 2 AS priority
                        FROM coreiq_yf_earnings_calendar yf
                        WHERE yf.ticker = :ticker
                          AND yf.earnings_date BETWEEN :fqe_window_start AND :fqe_window_end
                          AND yf.earnings_date <= :today
                    ) combined
                    WHERE earnings_date IS NOT NULL
                    ORDER BY priority ASC, earnings_date DESC
                    LIMIT 1
                """
            else:
                # Placeholder: nearest upcoming scheduled call
                query = f"""
                    SELECT earnings_date FROM (
                        SELECT MIN(ec.earnings_date) AS earnings_date, 1 AS priority
                        FROM coreiq_nasdaq_earnings_calendar ec
                        WHERE ec.ticker = :ticker
                          AND ec.fiscal_quarter_ending IN ({fqe_clause})
                          AND ec.earnings_date > :today
                        UNION ALL
                        SELECT yf.earnings_date, 2 AS priority
                        FROM coreiq_yf_earnings_calendar yf
                        WHERE yf.ticker = :ticker
                          AND yf.earnings_date BETWEEN :fqe_window_start AND :fqe_window_end
                          AND yf.earnings_date > :today
                    ) combined
                    WHERE earnings_date IS NOT NULL
                    ORDER BY priority ASC, earnings_date ASC
                    LIMIT 1
                """

            rows = db_manager.execute_query_readonly(query, {
                "ticker": ticker,
                "today": today_str,
                "fqe_window_start": fqe_window_start,
                "fqe_window_end": fqe_window_end,
                **fqe_params,
            })
            if rows and rows[0]["earnings_date"]:
                return rows[0]["earnings_date"]
            return None

        elif fye_month == 3:
            # March FYE: AV uses the same quarterly structure as September FYE.
            # The first earnings release of each calendar year is in Feb (for the
            # Oct-Dec quarter), so AV labels Oct-Dec = Q1 of that calendar year.
            # This is identical to Sep FYE convention: fy_start = Oct, FY label = year FY ends.
            # Results: Q1→Dec/(Y-1), Q2→Mar/Y, Q3→Jun/Y, Q4→Sep/Y
            fy_start_month = 10
            raw_offset = fy_start_month - 1 + fiscal_q * 3 - 1
            fqe_month  = (raw_offset % 12) + 1
            fqe_year   = (report_fiscal_year - 1) + (raw_offset // 12)

        elif fye_month <= 8:
            # Apr–Aug FYE: AV uses calendar quarter convention.
            # year = calendar year, q = 1..4 maps to Jan-Mar/Apr-Jun/Jul-Sep/Oct-Dec.
            base      = (fiscal_q - 1) * 3 + 1
            fqe_month = base + (fye_month - base) % 3
            fqe_year  = report_fiscal_year

        elif fye_month <= 11:
            # Sep–Nov FYE: fiscal year sequential, FY label = year FY ends.
            fy_start_month = fye_month + 1
            raw_offset = fy_start_month - 1 + fiscal_q * 3 - 1
            fqe_month  = (raw_offset % 12) + 1
            fqe_year   = (report_fiscal_year - 1) + (raw_offset // 12)

        else:
            # Dec FYE: fiscal year = calendar year.
            raw_offset = fiscal_q * 3 - 1
            fqe_month  = (raw_offset % 12) + 1
            fqe_year   = report_fiscal_year + (raw_offset // 12)

        fqe_str = f"{_MONTH_NUM_TO_ABB[fqe_month]}/{fqe_year}"

        if transcript_rows:
            # If the transcript exists but had no usable event-date match, compare
            # the local convention candidate with calendar-quarter and standard
            # fiscal-year-end candidates. This covers mixed AV labeling such as
            # AAPL 2026 Q1 -> Mar/2026 and BABA 2026 Q4 -> Mar/2026.
            base_month = (fiscal_q - 1) * 3 + 1
            calendar_fqe_month = base_month + (fye_month - base_month) % 3
            calendar_fqe = f"{_MONTH_NUM_TO_ABB[calendar_fqe_month]}/{report_fiscal_year}"

            standard_fy_start_month = (fye_month % 12) + 1
            standard_raw_offset = standard_fy_start_month - 1 + fiscal_q * 3 - 1
            standard_fqe_month = (standard_raw_offset % 12) + 1
            standard_fye_year = (report_fiscal_year - 1) + (standard_raw_offset // 12)
            standard_fqe = f"{_MONTH_NUM_TO_ABB[standard_fqe_month]}/{standard_fye_year}"

            candidate_fqes = list(dict.fromkeys([fqe_str, calendar_fqe, standard_fqe]))

            if len(candidate_fqes) > 1:
                fqe_params = {
                    f"candidate_{idx}": candidate
                    for idx, candidate in enumerate(candidate_fqes)
                }
                fqe_clause = ", ".join(f":candidate_{idx}" for idx in range(len(candidate_fqes)))
                has_real = bool(transcript_rows[0]["has_transcript"])
                today_str = date.today().isoformat()

                if has_real:
                    candidate_query = f"""
                        SELECT MAX(ec.earnings_date) AS earnings_date
                        FROM coreiq_nasdaq_earnings_calendar ec
                        WHERE ec.ticker = :ticker
                          AND ec.fiscal_quarter_ending IN ({fqe_clause})
                          AND ec.earnings_date <= :today
                    """
                else:
                    candidate_query = f"""
                        SELECT MIN(ec.earnings_date) AS earnings_date
                        FROM coreiq_nasdaq_earnings_calendar ec
                        WHERE ec.ticker = :ticker
                          AND ec.fiscal_quarter_ending IN ({fqe_clause})
                          AND ec.earnings_date > :today
                    """

                rows = db_manager.execute_query_readonly(
                    candidate_query,
                    {"ticker": ticker, "today": today_str, **fqe_params},
                )
                if rows and rows[0]["earnings_date"]:
                    return rows[0]["earnings_date"]

        last_day = calendar.monthrange(fqe_year, fqe_month)[1]
        fqe_window_start = (date(fqe_year, fqe_month, last_day) - timedelta(days=15)).isoformat()
        fqe_window_end   = (date(fqe_year, fqe_month, last_day) + timedelta(days=90)).isoformat()

        query = """
            SELECT earnings_date FROM (
                SELECT MAX(ec.earnings_date) AS earnings_date, 1 AS priority
                FROM coreiq_nasdaq_earnings_calendar ec
                WHERE ec.ticker = :ticker
                  AND ec.fiscal_quarter_ending = :fqe_str

                UNION ALL

                SELECT yf.earnings_date, 2 AS priority
                FROM coreiq_yf_earnings_calendar yf
                WHERE yf.ticker = :ticker
                  AND yf.earnings_date BETWEEN :fqe_window_start AND :fqe_window_end
            ) combined
            WHERE earnings_date IS NOT NULL
            ORDER BY priority ASC, earnings_date DESC
            LIMIT 1
        """
        rows = db_manager.execute_query_readonly(query, {
            "ticker": ticker,
            "fqe_str": fqe_str,
            "fqe_window_start": fqe_window_start,
            "fqe_window_end": fqe_window_end,
        })
        if rows and rows[0]["earnings_date"]:
            return rows[0]["earnings_date"]
        return None

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_earnings_event_dates(
        ticker: str,
        fiscal_q: int,
        report_fiscal_year: int,
    ) -> Optional[Dict[str, Any]]:
        """Return announcement (`report_date`) and fiscal quarter-end when known (`fiscal_period_end_date`)."""
        _MONTH_NUM_TO_ABB = {
            1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
            7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
        }
        _MONTH_NAME_TO_NUM = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }

        try:
            fye_map = EarningsCalendarRepository._get_fiscal_year_end_map()
        except Exception:
            fye_map = {}
        fye_name = fye_map.get(ticker, "December")
        fye_month = _MONTH_NAME_TO_NUM.get(fye_name.lower(), 12)

        transcript_rows = db_manager.execute_query_readonly(
            """
            SELECT has_transcript
            FROM coreiq_av_earnings_call_transcripts
            WHERE ticker = :ticker
              AND year = :year
              AND q = :q
            ORDER BY has_transcript DESC
            LIMIT 1
            """,
            {"ticker": ticker, "year": report_fiscal_year, "q": fiscal_q},
        )

        cand_dates = EarningsCalendarRepository._candidate_fiscal_quarter_end_dates_for_transcript(
            fiscal_q,
            report_fiscal_year,
            fye_month,
            transcript_rows,
        )
        if cand_dates:
            iso_key = tuple(sorted({d.isoformat() for d in cand_dates}))
            av_pair = AVFinancialsEarningsRepository.get_earnings_date_pair_for_fiscal_quarter_ends(
                ticker, iso_key
            )
            if av_pair:
                rd, fde = av_pair
                return {"report_date": rd, "fiscal_period_end_date": fde}

        cal_dt = EarningsCalendarRepository._resolve_calendar_earnings_announcement_date(
            ticker,
            fiscal_q,
            report_fiscal_year,
            fye_month,
            transcript_rows,
            _MONTH_NUM_TO_ABB,
        )
        if not cal_dt:
            return None
        fpe = EarningsCalendarRepository._fiscal_period_end_date_for_nasdaq_announcement(ticker, cal_dt)
        return {"report_date": cal_dt, "fiscal_period_end_date": fpe}

    @staticmethod
    @_log_query_time
    def get_earnings_date(ticker: str, fiscal_q: int, report_fiscal_year: int) -> "Optional[date]":
        """Backward-compatible: announcement date only (same as `report_date` from `get_earnings_event_dates`)."""
        ev = EarningsCalendarRepository.get_earnings_event_dates(ticker, fiscal_q, report_fiscal_year)
        return ev["report_date"] if ev else None

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_earnings_display_period(
        ticker: str,
        earnings_date,
        source_q: Optional[int] = None,
        source_year: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the fiscal period label for a matched earnings calendar date.

        The earnings call transcript table uses Alpha Vantage's raw year/q labels.
        For display, use the NASDAQ fiscal_quarter_ending plus the company's
        fiscal year end, matching the earnings calendar page.
        """
        if not ticker or not earnings_date:
            return None

        if hasattr(earnings_date, "isoformat"):
            earnings_date_str = earnings_date.isoformat()
        else:
            earnings_date_str = str(earnings_date)[:10]

        rows = db_manager.execute_query_readonly(
            """
            SELECT fiscal_quarter_ending
            FROM coreiq_nasdaq_earnings_calendar
            WHERE ticker = :ticker
              AND earnings_date = :earnings_date
              AND fiscal_quarter_ending IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            {"ticker": ticker, "earnings_date": earnings_date_str},
        )
        if not rows or not rows[0].get("fiscal_quarter_ending"):
            if source_q is not None and source_year is not None:
                return {
                    "earnings_date": earnings_date_str,
                    "fiscal_quarter_ending": None,
                    "fiscal_q": int(source_q),
                    "report_fiscal_year": int(source_year),
                }
            return None

        fqe = rows[0]["fiscal_quarter_ending"]
        if "/" not in fqe:
            return None

        month_name_to_num = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        month_abb_to_num = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }

        try:
            fqe_month_text, fqe_year_text = fqe.split("/", 1)
            fqe_month = month_abb_to_num.get(fqe_month_text.strip().lower())
            fqe_year = int(fqe_year_text.strip())
        except (AttributeError, ValueError):
            return None

        if not fqe_month:
            return None

        try:
            fye_map = EarningsCalendarRepository._get_fiscal_year_end_map()
        except Exception:
            fye_map = {}

        fye_name = fye_map.get(ticker, "December")
        fye_month = month_name_to_num.get(str(fye_name).lower(), 12)
        fy_start_month = (fye_month % 12) + 1

        if fye_month == 12:
            fiscal_year = fqe_year
        elif fqe_month >= fy_start_month:
            fiscal_year = fqe_year + 1
        else:
            fiscal_year = fqe_year

        months_into_fy = (fqe_month - fy_start_month) % 12 + 1
        fiscal_q = (months_into_fy + 2) // 3
        if source_q == fiscal_q and source_year:
            fiscal_year = int(source_year)

        return {
            "earnings_date": earnings_date_str,
            "fiscal_quarter_ending": fqe,
            "fiscal_q": fiscal_q,
            "report_fiscal_year": fiscal_year,
        }

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    @_log_query_time
    def get_transcript_for_calendar_event(ticker: str, earnings_date) -> Optional[Dict[str, Any]]:
        """Return the raw transcript year/q that maps to a calendar earnings date.

        Calendar rows are labeled by fiscal_quarter_ending/FYE, while transcript
        rows use the raw Alpha Vantage year/q keys. Use the same date resolver as
        the earnings call page to bridge those labels.
        """
        if not ticker or not earnings_date:
            return None

        target_date = earnings_date.isoformat() if hasattr(earnings_date, "isoformat") else str(earnings_date)[:10]
        rows = db_manager.execute_query_readonly(
            """
            SELECT year, q, quarter
            FROM coreiq_av_earnings_call_transcripts
            WHERE ticker = :ticker
              AND has_transcript = 1
            ORDER BY year DESC, q DESC
            LIMIT 80
            """,
            {"ticker": ticker},
        )

        for row in rows:
            year = row.get("year")
            q = row.get("q")
            if not year or not q:
                continue

            mapped_date = EarningsCalendarRepository.get_earnings_date(ticker, int(q), int(year))
            mapped_date_str = (
                mapped_date.isoformat()
                if hasattr(mapped_date, "isoformat")
                else str(mapped_date)[:10]
                if mapped_date
                else ""
            )
            if mapped_date_str == target_date:
                return {
                    "ticker": ticker,
                    "year": int(year),
                    "q": int(q),
                    "quarter": row.get("quarter") or f"Q{int(q)}",
                }

        return None

    # ------------------------------------------------------------------
    # Earnings email alert preferences (MySQL: coreiq_earnings_alert_preferences_history)
    # ------------------------------------------------------------------

    _ALERT_PREF_SESSION_KEY = "_earnings_alert_preferences_v1"
    _ALERT_PREF_TS_KEY = "_earnings_alert_preferences_ts_v1"
    _ALERT_PREF_TTL = 120  # seconds

    @staticmethod
    def get_earnings_alert_preferences(user_email: str) -> Optional[Dict[str, Any]]:
        """Return saved alert prefs for this user, or None.

        Uses a 120s session-state TTL cache to avoid a MySQL round-trip on every
        Streamlit rerun. Cache is invalidated by upsert_earnings_alert_preferences.
        Falls back to stale cache if DB is unavailable.
        """
        if not user_email:
            return None
        import time as _t_prefs
        key = str(user_email).strip().lower()
        sess_key = EarningsCalendarRepository._ALERT_PREF_SESSION_KEY
        ts_key = EarningsCalendarRepository._ALERT_PREF_TS_KEY
        ttl = EarningsCalendarRepository._ALERT_PREF_TTL

        # Fast path: check session-state TTL cache before hitting DB
        try:
            cache = st.session_state.get(sess_key, {})
            ts_cache = st.session_state.get(ts_key, {})
            cached_at = ts_cache.get(key, 0)
            if key in cache and isinstance(cache[key], dict) and (_t_prefs.time() - cached_at) < ttl:
                return dict(cache[key])
        except Exception:
            pass

        try:
            from data.earnings_alert_store import load_preference

            row = load_preference(key)
            if isinstance(row, dict):
                if sess_key not in st.session_state or not isinstance(st.session_state[sess_key], dict):
                    st.session_state[sess_key] = {}
                if ts_key not in st.session_state or not isinstance(st.session_state[ts_key], dict):
                    st.session_state[ts_key] = {}
                st.session_state[sess_key][key] = dict(row)
                st.session_state[ts_key][key] = _t_prefs.time()
                return dict(row)
        except Exception as e:
            log_structured_error(
                e,
                page="repository",
                component="EarningsCalendarRepository",
                operation="get_earnings_alert_preferences_sqlite",
                context=f"user_email={user_email}",
            )
        try:
            store = st.session_state.get(EarningsCalendarRepository._ALERT_PREF_SESSION_KEY)
            if not isinstance(store, dict):
                return None
            sess_row = store.get(key)
            if not isinstance(sess_row, dict):
                return None
            return dict(sess_row)
        except Exception as e:
            log_structured_error(
                e,
                page="repository",
                component="EarningsCalendarRepository",
                operation="get_earnings_alert_preferences",
                context=f"user_email={user_email}",
            )
            return None

    @staticmethod
    def upsert_earnings_alert_preferences(
        user_email: str,
        enabled: bool,
        days_before: int,
        selection_mode: str,
        tickers: List[str],
        sectors: Optional[List[str]] = None,
    ) -> bool:
        """Persist alert prefs (SQLite + session). Returns True on success."""
        if not user_email:
            return False
        key = str(user_email).strip().lower()
        sec = list(sectors) if sectors is not None else []
        payload = {
            "enabled": bool(enabled),
            "days_before": int(days_before),
            "selection_mode": str(selection_mode),
            "tickers": list(tickers or []),
            "sectors": sec,
        }
        try:
            from data.earnings_alert_store import save_preference

            if not save_preference(
                user_email=key,
                enabled=payload["enabled"],
                days_before=payload["days_before"],
                selection_mode=payload["selection_mode"],
                tickers=payload["tickers"],
                sectors=payload["sectors"],
            ):
                return False
        except Exception as e:
            log_structured_error(
                e,
                page="repository",
                component="EarningsCalendarRepository",
                operation="upsert_earnings_alert_preferences_sqlite",
                context=f"user_email={user_email}",
            )
            return False
        try:
            sk = EarningsCalendarRepository._ALERT_PREF_SESSION_KEY
            ts_key = EarningsCalendarRepository._ALERT_PREF_TS_KEY
            if sk not in st.session_state or not isinstance(st.session_state[sk], dict):
                st.session_state[sk] = {}
            st.session_state[sk][key] = payload
            # Invalidate TTL cache so next read fetches fresh data
            try:
                if ts_key in st.session_state and isinstance(st.session_state[ts_key], dict):
                    st.session_state[ts_key].pop(key, None)
            except Exception:
                pass
            return True
        except Exception as e:
            log_structured_error(
                e,
                page="repository",
                component="EarningsCalendarRepository",
                operation="upsert_earnings_alert_preferences",
                context=f"user_email={user_email}",
            )
            return False


# =============================================================================
# SEGMENT DATA REPOSITORY — Business & Geographic segments (CapIQ-style)
# =============================================================================
class SegmentDataRepository:
    """Repository for segment data from DB (coreiq_filing_metrics_v4) with
    edgartools fallback.

    Returns two separate tables matching CapitalIQ format:
      1. Business Segments — Revenue, Operating Profit, Assets, D&A, CapEx by segment
      2. Geographic Segments — Revenue by country/region

    DB-first: parallel year queries from coreiq_filing_metrics_v4.
    If DB has no segment data, fall back to edgartools XBRL parsing.
    """

    from utils.constants import (
        SEGMENT_ALL_AXES, SEGMENT_HEADING_MAP, SEGMENT_SKIP_MEMBERS,
        SEGMENT_METRIC_GROUPS, SEGMENT_PERCENT_UNIT_REFS,
        SEGMENT_NON_USD_CURRENCIES, SEGMENT_NON_MONETARY_UNITS,
        EDGAR_BUSINESS_AXES, EDGAR_GEO_AXES,
    )

    # ── SQL templates ──────────────────────────────────────────────────────
    @staticmethod
    def _build_dim_clause(axes: list) -> tuple:
        """Build SQL OR clause and params dict for a list of LIKE patterns."""
        parts = []
        params = {}
        for i, pattern in enumerate(axes):
            key = f"ax{i}"
            parts.append(f"dimension LIKE :{key}")
            params[key] = pattern
        return " OR ".join(parts), params

    _YEAR_SQL_TPL = """
        SELECT DISTINCT report_fiscal_year
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND is_dimensioned = 1
          AND numeric_value IS NOT NULL
          AND doc_type = '10-K'
          AND ({dim_clause})
        ORDER BY report_fiscal_year ASC
    """

    _ROW_SQL_TPL = """
        SELECT original_label, numeric_value, unit_ref, report_fiscal_year,
               dimension_label, dimension_member_label, concept,
               period_type, period_start, period_end, period_instant,
               dimension, full_dimension_label, filing_date
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND is_dimensioned = 1
          AND numeric_value IS NOT NULL
          AND doc_type = '10-K'
          AND report_fiscal_year = :year
          AND ({dim_clause})
        ORDER BY full_dimension_label ASC, original_label ASC
    """

    # Quarterly: distinct 3-month period_end dates in date range
    _QUARTER_PERIODS_SQL_TPL = """
        SELECT DISTINCT period_end
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND is_dimensioned = 1
          AND numeric_value IS NOT NULL
          AND doc_type IN ('10-Q-Q1', '10-Q-Q2', '10-Q-Q3')
          AND period_start IS NOT NULL AND period_end IS NOT NULL
          AND DATEDIFF(period_end, period_start) BETWEEN 80 AND 100
          AND period_end BETWEEN :start_date AND :end_date
          AND ({dim_clause})
        ORDER BY period_end ASC
    """

    # Quarterly: all rows for a specific period_end (3-month only)
    _QUARTER_ROW_SQL_TPL = """
        SELECT original_label, numeric_value, unit_ref,
               dimension_label, dimension_member_label, concept,
               period_type, period_start, period_end,
               dimension, full_dimension_label, filing_date
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND is_dimensioned = 1
          AND numeric_value IS NOT NULL
          AND doc_type IN ('10-Q-Q1', '10-Q-Q2', '10-Q-Q3')
          AND period_start IS NOT NULL AND period_end IS NOT NULL
          AND DATEDIFF(period_end, period_start) BETWEEN 80 AND 100
          AND period_end = :period_end
          AND ({dim_clause})
        ORDER BY full_dimension_label ASC, original_label ASC
    """

    # Non-dimensioned (consolidated) rows — used as exact totals per metric per period
    _QUARTER_TOTAL_SQL_TPL = """
        SELECT original_label, numeric_value, unit_ref, period_end
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND is_dimensioned = 0
          AND numeric_value IS NOT NULL
          AND doc_type IN ('10-Q-Q1', '10-Q-Q2', '10-Q-Q3')
          AND period_start IS NOT NULL AND period_end IS NOT NULL
          AND DATEDIFF(period_end, period_start) BETWEEN 80 AND 100
          AND period_end = :period_end
        ORDER BY original_label ASC
    """

    # ── Utility helpers ────────────────────────────────────────────────────

    @staticmethod
    def _is_monetary_row(row) -> bool:
        """Return True if the row is a USD monetary value (not percent/count)."""
        from utils.constants import (
            SEGMENT_PERCENT_UNIT_REFS, SEGMENT_NON_USD_CURRENCIES,
            SEGMENT_NON_MONETARY_UNITS,
        )
        uref = (row.get('unit_ref') or '').strip().lower()
        if 'usd' in uref and 'pershare' not in uref.replace('_', ''):
            return True
        if uref in SEGMENT_PERCENT_UNIT_REFS:
            return False
        if 'pure' in uref or 'xbrli' in uref:
            return False
        for cur in SEGMENT_NON_USD_CURRENCIES:
            if cur in uref:
                return False
        for nm in SEGMENT_NON_MONETARY_UNITS:
            if nm in uref:
                return False
        val = row.get('numeric_value')
        if val is not None and abs(val) >= 1000:
            return True
        return False

    @staticmethod
    def _matches_metric(label: str, metric_cfg: dict) -> bool:
        """Return True if label matches the include keywords but not exclude."""
        low = label.lower()
        if not any(kw in low for kw in metric_cfg["db_include"]):
            return False
        if any(kw in low for kw in metric_cfg["db_exclude"]):
            return False
        return True

    @staticmethod
    def _normalize_heading(raw_heading: str) -> str:
        """Normalize heading to Business / Product and Service / Geographical."""
        from utils.constants import SEGMENT_HEADING_MAP
        key = raw_heading.lower().strip()
        return SEGMENT_HEADING_MAP.get(key, raw_heading.title())

    @staticmethod
    def _title_case_member(member: str) -> str:
        if not member:
            return member
        # Normalize unicode quotes → ASCII
        member = member.replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"')
        if member != member.upper() and member != member.lower():
            return member
        return member.title()

    @staticmethod
    def _get_heading(fdl: str) -> str:
        if not fdl:
            return ""
        idx = fdl.find(":")
        raw = fdl[:idx].strip() if idx != -1 else fdl.strip()
        return SegmentDataRepository._normalize_heading(raw)

    @staticmethod
    def _get_row_year(row):
        pt = (row.get('period_type') or '').lower()
        if pt == 'duration' and row.get('period_end'):
            pe = row['period_end']
            ps = row.get('period_start')
            rfy = row.get('report_fiscal_year')
            if ps and hasattr(pe, 'year') and hasattr(ps, 'year'):
                span = (pe - ps).days
                if span >= 300:
                    pe_year = pe.year if hasattr(pe, 'year') else int(str(pe)[:4])
                    if rfy is not None and pe_year < rfy:
                        return pe_year
                elif span <= 7:
                    # Zero/near-zero duration rows are XBRL noise (e.g. point-in-time
                    # disclosure dates tagged as durations). They don't represent an
                    # annual reporting period — return None to exclude them from the
                    # segment year set rather than polluting it via the rfy fallback.
                    return None
        elif pt == 'instant' and row.get('period_instant'):
            pi = row['period_instant']
            return pi.year if hasattr(pi, 'year') else int(str(pi)[:4])
        return row.get('report_fiscal_year')

    # ── DB fetchers ────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_db_years(ticker: str) -> List[int]:
        from utils.constants import SEGMENT_ALL_AXES
        clause, params = SegmentDataRepository._build_dim_clause(SEGMENT_ALL_AXES)
        sql = SegmentDataRepository._YEAR_SQL_TPL.format(dim_clause=clause)
        rows = db_manager.execute_query_readonly(sql, {"ticker": ticker, **params})
        return sorted(r['report_fiscal_year'] for r in rows if r.get('report_fiscal_year'))

    @staticmethod
    def _fetch_db_year_rows(ticker: str, year: int) -> List[Dict[str, Any]]:
        from utils.constants import SEGMENT_ALL_AXES
        clause, params = SegmentDataRepository._build_dim_clause(SEGMENT_ALL_AXES)
        sql = SegmentDataRepository._ROW_SQL_TPL.format(dim_clause=clause)
        dim_rows = db_manager.execute_query_readonly(sql, {"ticker": ticker, "year": year, **params}) or []
        ndim_sql = """
            SELECT original_label, numeric_value, unit_ref, report_fiscal_year,
                   NULL AS dimension_label, NULL AS dimension_member_label, NULL AS concept,
                   period_type, period_start, period_end, period_instant,
                   NULL AS dimension, NULL AS full_dimension_label, filing_date
            FROM coreiq_filing_metrics_v4
            WHERE ticker = :ticker
              AND is_dimensioned = 0
              AND numeric_value IS NOT NULL
              AND doc_type = '10-K'
              AND report_fiscal_year = :year
            ORDER BY original_label ASC
        """
        ndim_rows = db_manager.execute_query_readonly(ndim_sql, {"ticker": ticker, "year": year}) or []
        for r in ndim_rows:
            r['_is_ndim'] = True
        return dim_rows + ndim_rows

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_all_db_rows(ticker: str) -> List[Dict[str, Any]]:
        """Fetch ALL dimensioned rows for a ticker (years in parallel)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        years = SegmentDataRepository._fetch_db_years(ticker)
        if not years:
            return []
        all_rows: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(len(years), 6)) as pool:
            futs = {pool.submit(SegmentDataRepository._fetch_db_year_rows, ticker, yr): yr for yr in years}
            for fut in as_completed(futs):
                rows = fut.result()
                if rows:
                    all_rows.extend(rows)
        def _fd_neg(r) -> int:
            """Return negative integer of filing_date YYYYMMDD for descending sort."""
            fd = r.get('filing_date')
            if fd is None:
                return 0
            s = fd.isoformat() if hasattr(fd, 'isoformat') else str(fd)[:10]
            try:
                return -int(s.replace('-', '')[:8])
            except (ValueError, TypeError):
                return 0

        # Sort by (year, fdl, label) asc then filing_date DESC within each group.
        # "First value wins" in the classification loop will therefore pick the most
        # recent filing's value — reclassified prior-year rows from a newer 10-K win.
        # Use `or` instead of get(key, default): ndim rows have NULL columns present
        # in the dict as None, so dict.get(key, default) returns None not the default.
        all_rows.sort(key=lambda r: (
            r.get('report_fiscal_year') or 0,
            r.get('full_dimension_label') or '',
            r.get('original_label') or '',
            _fd_neg(r),
        ))
        return all_rows

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_all_db_rows_quarterly(ticker: str, start_date: date, end_date: date) -> List[Dict[str, Any]]:
        """Fetch all 3-month dimensioned rows from 10-Q filings for a date range."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from utils.constants import SEGMENT_ALL_AXES
        clause, params = SegmentDataRepository._build_dim_clause(SEGMENT_ALL_AXES)

        # Get distinct 3-month period_ends in range
        periods_sql = SegmentDataRepository._QUARTER_PERIODS_SQL_TPL.format(dim_clause=clause)
        period_rows = db_manager.execute_query_readonly(
            periods_sql, {"ticker": ticker, "start_date": start_date, "end_date": end_date, **params}
        )
        period_ends = []
        for r in (period_rows or []):
            pe = r.get('period_end')
            if pe:
                pe = pe.date() if hasattr(pe, 'date') else pe
                period_ends.append(pe)

        if not period_ends:
            return []

        all_rows: List[Dict[str, Any]] = []
        row_sql = SegmentDataRepository._QUARTER_ROW_SQL_TPL.format(dim_clause=clause)
        total_sql = SegmentDataRepository._QUARTER_TOTAL_SQL_TPL

        def _fetch_period(pe):
            dim_rows = db_manager.execute_query_readonly(row_sql, {"ticker": ticker, "period_end": pe, **params}) or []
            ndim_rows = db_manager.execute_query_readonly(total_sql, {"ticker": ticker, "period_end": pe}) or []
            # Tag non-dimensioned rows so _build_segment_tables_quarterly can use them as totals
            for r in ndim_rows:
                r['_is_ndim'] = True
            return dim_rows + ndim_rows

        with ThreadPoolExecutor(max_workers=min(len(period_ends), 6)) as pool:
            futs = {pool.submit(_fetch_period, pe): pe for pe in period_ends}
            for fut in as_completed(futs):
                rows = fut.result()
                if rows:
                    all_rows.extend(rows)

        all_rows.sort(key=lambda r: (
            r.get('period_end') or date.min,
            r.get('full_dimension_label') or '',
            r.get('original_label') or '',
        ))
        return all_rows

    # ── DB → structured output ─────────────────────────────────────────────

    @staticmethod
    def _classify_heading(heading: str) -> str:
        """Return 'business' or 'geo' for a normalised heading."""
        if heading == "Geographical":
            return "geo"
        return "business"  # Business, Product and Service, etc.

    @staticmethod
    def _build_segment_tables_from_db(ticker: str, start_date: date, end_date: date) -> Dict[str, Any]:
        """Process DB rows into CapIQ-style Business + Geographic tables."""
        from utils.constants import SEGMENT_METRIC_GROUPS, SEGMENT_SKIP_MEMBERS

        all_rows = SegmentDataRepository._fetch_all_db_rows(ticker)
        if not all_rows:
            return None  # Signal to fall back to edgartools

        start_year, end_year = start_date.year, end_date.year
        _get_ey = SegmentDataRepository._get_row_year
        filtered = [r for r in all_rows if _get_ey(r) is not None and start_year <= _get_ey(r) <= end_year]

        if not filtered:
            return None

        years = sorted(set(_get_ey(r) for r in filtered if _get_ey(r) is not None))

        # Build period_dates
        period_dates: Dict[int, date] = {}
        for row in filtered:
            yr = _get_ey(row)
            pe = row.get('period_end')
            if yr and pe and hasattr(pe, 'year'):
                if yr not in period_dates or pe > period_dates[yr]:
                    period_dates[yr] = pe

        # Classify each row into business or geo, and match to a metric group
        biz_data: Dict[str, Dict[str, Dict[int, Optional[float]]]] = {}
        geo_data: Dict[str, Dict[str, Dict[int, Optional[float]]]] = {}
        metric_totals: Dict[str, Dict[int, Optional[float]]] = {}

        for row in filtered:
            if not SegmentDataRepository._is_monetary_row(row):
                continue
            orig_label = row.get('original_label') or ''
            row_year = SegmentDataRepository._get_row_year(row)
            raw_val = row.get('numeric_value')
            if raw_val is None or row_year not in years:
                continue
            scaled = raw_val / 1_000_000

            # Non-dimensioned rows → metric_totals (exact consolidated totals)
            if row.get('_is_ndim'):
                for metric_name, cfg in SEGMENT_METRIC_GROUPS.items():
                    if SegmentDataRepository._matches_metric(orig_label, cfg):
                        if metric_name not in metric_totals:
                            metric_totals[metric_name] = {y: None for y in years}
                        if metric_totals[metric_name].get(row_year) is None:
                            metric_totals[metric_name][row_year] = scaled
                        break
                continue

            # Dimensioned rows → segment members
            fdl = row.get('full_dimension_label') or ''
            member_raw = row.get('dimension_member_label') or ''
            if member_raw.lower().strip() in SEGMENT_SKIP_MEMBERS:
                continue
            member = SegmentDataRepository._title_case_member(member_raw)
            if not member:
                continue
            heading = SegmentDataRepository._get_heading(fdl)
            section = SegmentDataRepository._classify_heading(heading)

            for metric_name, cfg in SEGMENT_METRIC_GROUPS.items():
                if SegmentDataRepository._matches_metric(orig_label, cfg):
                    target = geo_data if section == "geo" else biz_data
                    if metric_name not in target:
                        target[metric_name] = {}
                    if member not in target[metric_name]:
                        target[metric_name][member] = {y: None for y in years}
                    if target[metric_name][member][row_year] is None:
                        target[metric_name][member][row_year] = scaled
                    break

        # If we found absolutely nothing useful, signal fallback
        if not biz_data and not geo_data:
            return None

        # ── US-only geo fallback ──────────────────────────────────────────────
        # When a SEC company has business segments but zero geographic segments,
        # it MIGHT be US-only.  Guard: if filing_metrics_v2 has ANY non-US member
        # label on a geographic axis (e.g. Canada, UK, Japan), the company is
        # international — skip the fallback to avoid showing a misleading single
        # "United States = total revenue" row.
        if biz_data and not geo_data:
            try:
                from utils.constants import SEGMENT_ALL_AXES
                # Quick check: does this ticker have any geo-axis member that is
                # clearly non-US?  We only need one row to know.
                # Detect international ops: look for ANY non-US, non-ambiguous
                # country/region as a geographic dimension member.  This catches
                # cases like Costco (geo axis has CANADA/AUSTRALIA for inventory
                # and store counts — no revenue — but clearly international).
                # We exclude generic/ambiguous labels like "International", "Foreign",
                # and pension/pool labels that appear in US-only companies.
                _intl_check_sql = """
                    SELECT COUNT(*) as cnt
                    FROM coreiq_filing_metrics_v4
                    WHERE ticker = :ticker
                      AND is_dimensioned = 1
                      AND doc_type = '10-K'
                      AND dimension_member_label IS NOT NULL
                      AND LOWER(dimension_member_label) NOT IN (
                          'united states', 'u.s.', 'us', 'domestic',
                          'united states operations', 'us operations',
                          'united states pooled funds',
                          'international', 'foreign',
                          'all other', 'other', 'other international'
                      )
                      AND LOWER(dimension_member_label) NOT LIKE '%pooled%'
                      AND LOWER(dimension_member_label) NOT LIKE '%pension%'
                      AND LOWER(dimension_member_label) NOT LIKE '%benefit%'
                      AND (dimension LIKE '%Geograph%' OR dimension LIKE '%Geographic%'
                           OR dimension LIKE '%StatementGeograph%')
                    LIMIT 1
                """
                _intl_rows = db_manager.execute_query_readonly(
                    _intl_check_sql, {"ticker": ticker}
                )
                _is_international = bool(_intl_rows and _intl_rows[0].get('cnt', 0) > 0)

                if not _is_international:
                    _us_wide_start = start_date.replace(year=max(start_date.year - 1, 1900))
                    try:
                        _us_wide_end = end_date.replace(year=end_date.year + 1)
                    except ValueError:
                        _us_wide_end = end_date
                    _rev_sql = """
                        SELECT fiscal_date_ending, total_revenue
                        FROM coreiq_av_financials_income_statement
                        WHERE ticker = :ticker
                          AND report_type = 'annual'
                          AND fiscal_date_ending BETWEEN :start_date AND :end_date
                        ORDER BY fiscal_date_ending ASC
                    """
                    _rev_rows = db_manager.execute_query_readonly(
                        _rev_sql,
                        {"ticker": ticker, "start_date": _us_wide_start, "end_date": _us_wide_end},
                    )
                    _us_rev: Dict[int, Optional[float]] = {y: None for y in years}
                    for _rr in (_rev_rows or []):
                        _fe = _rr.get('fiscal_date_ending')
                        _rv = _rr.get('total_revenue')
                        if _fe is None or _rv is None:
                            continue
                        _fe_yr = _fe.year if hasattr(_fe, 'year') else int(str(_fe)[:4])
                        if _fe_yr in _us_rev and _us_rev[_fe_yr] is None:
                            _us_rev[_fe_yr] = float(_rv) / 1_000_000
                            continue
                        # Proximity match via period_dates (handles AV date normalisation)
                        _best_yr2 = None
                        _min_days2 = 60
                        for _yr2, _pd2 in period_dates.items():
                            if _yr2 not in _us_rev or _us_rev[_yr2] is not None:
                                continue
                            try:
                                _d2 = abs((_fe - _pd2).days) if (hasattr(_fe, 'year') and hasattr(_pd2, 'year')) else 9999
                                if _d2 < _min_days2:
                                    _min_days2 = _d2
                                    _best_yr2 = _yr2
                            except Exception:
                                pass
                        if _best_yr2 is not None:
                            _us_rev[_best_yr2] = float(_rv) / 1_000_000
                    if any(v is not None for v in _us_rev.values()):
                        geo_data["Revenues"] = {"United States": _us_rev}
                else:
                    pass  # international company — skip US-only geo fallback
            except Exception as _e:
                pass  # Non-fatal — just skip the geo fallback

        # ── Fetch Total Revenue from income statement (authoritative total, no summing) ──
        # Used as the "Total" row for the Revenues metric in both business and geo tables.
        # Query with 1-year wider window on both sides: Alpha Vantage sometimes normalises
        # off-calendar fiscal year-end dates to Dec 31 of the prior calendar year
        # (e.g. Lowe's FY2020 ending 2021-01-29 → stored as 2020-12-31), so a plain
        # BETWEEN start_date..end_date misses the oldest fiscal year.
        revenue_totals: Dict[int, Optional[float]] = {y: None for y in years}
        try:
            _tot_wide_start = start_date.replace(year=max(start_date.year - 1, 1900))
            try:
                _tot_wide_end = end_date.replace(year=end_date.year + 1)
            except ValueError:
                _tot_wide_end = end_date
            _tot_sql = """
                SELECT fiscal_date_ending, total_revenue
                FROM coreiq_av_financials_income_statement
                WHERE ticker = :ticker
                  AND report_type = 'annual'
                  AND fiscal_date_ending BETWEEN :start_date AND :end_date
                ORDER BY fiscal_date_ending ASC
            """
            _tot_rows = db_manager.execute_query_readonly(
                _tot_sql, {"ticker": ticker, "start_date": _tot_wide_start, "end_date": _tot_wide_end}
            )
            for _tr in (_tot_rows or []):
                _fe = _tr.get('fiscal_date_ending')
                _rv = _tr.get('total_revenue')
                if _fe is None or _rv is None:
                    continue
                _fe_yr = _fe.year if hasattr(_fe, 'year') else int(str(_fe)[:4])
                # Fast path: direct year key match (calendar-year companies)
                if _fe_yr in revenue_totals and revenue_totals[_fe_yr] is None:
                    revenue_totals[_fe_yr] = float(_rv) / 1_000_000
                    continue
                # Proximity match via period_dates: handles off-calendar FY companies
                # where AV normalises fiscal_date_ending to Dec 31 of prior year.
                _best_yr = None
                _min_days = 60
                for _yr, _pd in period_dates.items():
                    if _yr not in revenue_totals or revenue_totals[_yr] is not None:
                        continue
                    try:
                        _delta = abs((_fe - _pd).days) if (hasattr(_fe, 'year') and hasattr(_pd, 'year')) else 9999
                        if _delta < _min_days:
                            _min_days = _delta
                            _best_yr = _yr
                    except Exception:
                        pass
                if _best_yr is not None:
                    revenue_totals[_best_yr] = float(_rv) / 1_000_000
        except Exception:
            pass  # Non-fatal

        # IS revenue takes precedence over XBRL non-dim; merge into metric_totals
        for _yr, _rv in revenue_totals.items():
            if _rv is not None:
                if "Revenues" not in metric_totals:
                    metric_totals["Revenues"] = {y: None for y in years}
                metric_totals["Revenues"][_yr] = _rv

        return {
            "years": years,
            "period_dates": period_dates,
            "business_segments": biz_data,
            "geo_segments": geo_data,
            "revenue_totals": revenue_totals,
            "metric_totals": metric_totals,
            "source": "db",
        }

    @staticmethod
    def _build_segment_tables_quarterly(ticker: str, start_date: date, end_date: date) -> Optional[Dict[str, Any]]:
        """Process 10-Q rows into CapIQ-style quarterly segment tables.

        Uses YYYYMMDD integer keys so the rendering layer works unchanged.
        period_dates maps {yyyymmdd_int: period_end date}.
        """
        from utils.constants import SEGMENT_METRIC_GROUPS, SEGMENT_SKIP_MEMBERS

        all_rows = SegmentDataRepository._fetch_all_db_rows_quarterly(ticker, start_date, end_date)
        if not all_rows:
            return None

        # Collect distinct period_ends and build integer keys
        _pe_set = set()
        for r in all_rows:
            pe = r.get('period_end')
            if pe:
                pe = pe.date() if hasattr(pe, 'date') else pe
                _pe_set.add(pe)

        if not _pe_set:
            return None

        # YYYYMMDD integer key for each period_end
        def _pe_key(d) -> int:
            return d.year * 10000 + d.month * 100 + d.day

        sorted_pes = sorted(_pe_set)
        period_keys = [_pe_key(pe) for pe in sorted_pes]
        period_dates: Dict[int, date] = {_pe_key(pe): pe for pe in sorted_pes}
        key_set = set(period_keys)

        biz_data: Dict[str, Dict[str, Dict[int, Optional[float]]]] = {}
        geo_data: Dict[str, Dict[str, Dict[int, Optional[float]]]] = {}
        # Exact filed totals per metric per period (from non-dimensioned rows)
        metric_totals: Dict[str, Dict[int, Optional[float]]] = {}

        for row in all_rows:
            if not SegmentDataRepository._is_monetary_row(row):
                continue

            orig_label = row.get('original_label') or ''
            pe = row.get('period_end')
            if pe is None:
                continue
            pe = pe.date() if hasattr(pe, 'date') else pe
            pkey = _pe_key(pe)
            if pkey not in key_set:
                continue
            raw_val = row.get('numeric_value')
            if raw_val is None:
                continue
            scaled = raw_val / 1_000_000

            # Non-dimensioned rows → metric_totals (exact consolidated values)
            if row.get('_is_ndim'):
                for metric_name, cfg in SEGMENT_METRIC_GROUPS.items():
                    if SegmentDataRepository._matches_metric(orig_label, cfg):
                        if metric_name not in metric_totals:
                            metric_totals[metric_name] = {k: None for k in period_keys}
                        if metric_totals[metric_name].get(pkey) is None:
                            metric_totals[metric_name][pkey] = scaled
                        break
                continue

            # Dimensioned rows → segment members
            fdl = row.get('full_dimension_label') or ''
            member_raw = row.get('dimension_member_label') or ''
            if member_raw.lower().strip() in SEGMENT_SKIP_MEMBERS:
                continue
            member = SegmentDataRepository._title_case_member(member_raw)
            if not member:
                continue
            heading = SegmentDataRepository._get_heading(fdl)
            section = SegmentDataRepository._classify_heading(heading)

            for metric_name, cfg in SEGMENT_METRIC_GROUPS.items():
                if SegmentDataRepository._matches_metric(orig_label, cfg):
                    target = geo_data if section == "geo" else biz_data
                    if metric_name not in target:
                        target[metric_name] = {}
                    if member not in target[metric_name]:
                        target[metric_name][member] = {k: None for k in period_keys}
                    if target[metric_name][member].get(pkey) is None:
                        target[metric_name][member][pkey] = scaled
                    break

        if not biz_data and not geo_data:
            return None

        # Authoritative quarterly revenue totals from income statement
        revenue_totals: Dict[int, Optional[float]] = {k: None for k in period_keys}
        try:
            _q_rev_sql = """
                SELECT fiscal_date_ending, total_revenue
                FROM coreiq_av_financials_income_statement
                WHERE ticker = :ticker
                  AND report_type = 'quarterly'
                  AND fiscal_date_ending BETWEEN :start_date AND :end_date
                ORDER BY fiscal_date_ending ASC
            """
            _q_rev_rows = db_manager.execute_query_readonly(
                _q_rev_sql, {"ticker": ticker, "start_date": start_date, "end_date": end_date}
            )
            for _qr in (_q_rev_rows or []):
                _fe = _qr.get('fiscal_date_ending')
                _rv = _qr.get('total_revenue')
                if _fe is None or _rv is None:
                    continue
                _fe = _fe.date() if hasattr(_fe, 'date') else _fe
                _pk = _pe_key(_fe)
                if _pk in revenue_totals and revenue_totals[_pk] is None:
                    revenue_totals[_pk] = float(_rv) / 1_000_000
                    continue
                # Proximity match: within 5 days
                _best_k = None
                _min_d = 5
                for _k, _pd in period_dates.items():
                    if revenue_totals.get(_k) is not None:
                        continue
                    try:
                        _d = abs((_fe - _pd).days)
                        if _d < _min_d:
                            _min_d = _d
                            _best_k = _k
                    except Exception:
                        pass
                if _best_k is not None:
                    revenue_totals[_best_k] = float(_rv) / 1_000_000
        except Exception:
            pass

        # Income-statement revenue takes precedence over XBRL non-dim revenue
        # (IS is deduplicated and more reliable); merge into metric_totals
        for _pk, _rv in revenue_totals.items():
            if _rv is not None:
                if "Revenues" not in metric_totals:
                    metric_totals["Revenues"] = {k: None for k in period_keys}
                metric_totals["Revenues"][_pk] = _rv

        return {
            "years": period_keys,
            "period_dates": period_dates,
            "business_segments": biz_data,
            "geo_segments": geo_data,
            "revenue_totals": revenue_totals,
            "metric_totals": metric_totals,
            "source": "db_quarterly",
        }

    # ── edgartools fallback ────────────────────────────────────────────────

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    def _fetch_from_edgartools(ticker: str) -> Dict[str, Any]:
        """Fall back to edgartools when DB has no segment data.

        Fetches the latest 6 10-K filings and extracts segment data via XBRL.
        Returns same structure as _build_segment_tables_from_db.
        """
        import sys
        from pathlib import Path
        _edgartools_path = str(Path(__file__).resolve().parent.parent.parent / "edgartools")
        if _edgartools_path not in sys.path:
            sys.path.insert(0, _edgartools_path)
        from edgar import Company
        from utils.constants import (
            EDGAR_BUSINESS_AXES, EDGAR_GEO_AXES, EDGAR_PRODUCT_AXES,
            SEGMENT_METRIC_GROUPS, SEGMENT_SKIP_MEMBERS,
        )
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            company = Company(ticker)
        except Exception:
            return None

        filings = company.get_filings(form='10-K')
        if not filings:
            return None

        # Take last 6 filings
        filing_list = list(filings[:6])
        if not filing_list:
            return None

        biz_data: Dict[str, Dict[str, Dict[int, Optional[float]]]] = {}
        geo_data: Dict[str, Dict[str, Dict[int, Optional[float]]]] = {}
        all_years = set()
        period_dates: Dict[int, date] = {}

        def _process_filing(filing_obj):
            """Extract segment facts from a single filing."""
            local_biz = []
            local_geo = []
            try:
                xbrl = filing_obj.xbrl()
                if not xbrl:
                    return local_biz, local_geo

                # Business segment facts
                for axis in EDGAR_BUSINESS_AXES:
                    try:
                        facts = xbrl.query().by_dimension(axis).with_dimensions().execute()
                        for f in facts:
                            f['_section'] = 'business'
                            local_biz.append(f)
                    except Exception:
                        pass

                # Product/service segment facts — classified as business
                for axis in EDGAR_PRODUCT_AXES:
                    try:
                        facts = xbrl.query().by_dimension(axis).with_dimensions().execute()
                        for f in facts:
                            f['_section'] = 'business'
                            local_biz.append(f)
                    except Exception:
                        pass

                # Geographic segment facts
                for axis in EDGAR_GEO_AXES:
                    try:
                        facts = xbrl.query().by_dimension(axis).with_dimensions().execute()
                        for f in facts:
                            f['_section'] = 'geo'
                            local_geo.append(f)
                    except Exception:
                        pass

            except Exception:
                pass
            return local_biz, local_geo

        # Parallel filing processing
        all_facts_biz = []
        all_facts_geo = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(_process_filing, f): f for f in filing_list}
            for fut in as_completed(futs):
                biz_facts, geo_facts = fut.result()
                all_facts_biz.extend(biz_facts)
                all_facts_geo.extend(geo_facts)

        def _process_facts(facts_list, target_dict):
            for fact in facts_list:
                member_raw = fact.get('dimension_member_label') or ''
                if member_raw.lower().strip() in SEGMENT_SKIP_MEMBERS:
                    continue
                member = SegmentDataRepository._title_case_member(member_raw)
                if not member:
                    continue
                fy = fact.get('fiscal_year')
                if not fy:
                    continue
                all_years.add(fy)
                val = fact.get('numeric_value')
                if val is None:
                    continue

                # Build period_dates from period_end
                pe = fact.get('period_end') or fact.get('period_instant')
                if pe and fy:
                    try:
                        from datetime import date as dt_date
                        if isinstance(pe, str):
                            pe = dt_date.fromisoformat(pe)
                        if fy not in period_dates or pe > period_dates[fy]:
                            period_dates[fy] = pe
                    except Exception:
                        pass

                scaled = val / 1_000_000
                orig_label = fact.get('original_label') or ''

                for metric_name, cfg in SEGMENT_METRIC_GROUPS.items():
                    # Match by concept or label
                    concept = fact.get('concept') or ''
                    concept_short = concept.split(':')[-1] if ':' in concept else concept
                    matched = False
                    for ec in cfg["edgar_concepts"]:
                        if ec.lower() == concept_short.lower():
                            matched = True
                            break
                    if not matched:
                        low_label = orig_label.lower()
                        if any(kw in low_label for kw in cfg["edgar_labels"]):
                            if not any(kw in low_label for kw in cfg.get("db_exclude", [])):
                                matched = True
                    if matched:
                        if metric_name not in target_dict:
                            target_dict[metric_name] = {}
                        if member not in target_dict[metric_name]:
                            target_dict[metric_name][member] = {}
                        if fy not in target_dict[metric_name][member] or target_dict[metric_name][member][fy] is None:
                            target_dict[metric_name][member][fy] = scaled
                        break

        _process_facts(all_facts_biz, biz_data)
        _process_facts(all_facts_geo, geo_data)

        if not biz_data and not geo_data:
            return None

        years = sorted(all_years)
        # Backfill year keys
        for data_dict in (biz_data, geo_data):
            for metric in data_dict.values():
                for member_vals in metric.values():
                    for y in years:
                        if y not in member_vals:
                            member_vals[y] = None

        return {
            "years": years,
            "period_dates": period_dates,
            "business_segments": biz_data,
            "geo_segments": geo_data,
            "source": "edgartools",
        }

    # ── Public API ─────────────────────────────────────────────────────────

    @staticmethod
    def get_date_range(ticker: str, period_type: str = "annual") -> Tuple[Optional[date], Optional[date]]:
        if period_type.lower() == "quarterly":
            # Use a wide window to find the full quarterly date range
            _wide = date(2010, 1, 1)
            _wide_end = date(2030, 12, 31)
            rows = SegmentDataRepository._fetch_all_db_rows_quarterly(ticker, _wide, _wide_end)
            if not rows:
                return None, None
            pes = sorted(set(
                (r['period_end'].date() if hasattr(r['period_end'], 'date') else r['period_end'])
                for r in rows if r.get('period_end')
            ))
            if not pes:
                return None, None
            return pes[0], pes[-1]
        rows = SegmentDataRepository._fetch_all_db_rows(ticker)
        if not rows:
            return None, None
        years = sorted(set(r['report_fiscal_year'] for r in rows if r.get('report_fiscal_year')))
        if not years:
            return None, None
        return date(years[0], 1, 31), date(years[-1], 12, 31)

    @staticmethod
    def get_available_dates(ticker: str, period_type: str = "annual") -> List[date]:
        if period_type.lower() == "quarterly":
            _wide = date(2010, 1, 1)
            _wide_end = date(2030, 12, 31)
            rows = SegmentDataRepository._fetch_all_db_rows_quarterly(ticker, _wide, _wide_end)
            return sorted(set(
                (r['period_end'].date() if hasattr(r['period_end'], 'date') else r['period_end'])
                for r in rows if r.get('period_end')
            ))
        rows = SegmentDataRepository._fetch_all_db_rows(ticker)
        years = sorted(set(r['report_fiscal_year'] for r in rows if r.get('report_fiscal_year')))
        return [date(y, 12, 31) for y in years]

    @staticmethod
    def has_quarterly_segment_data(ticker: str) -> bool:
        """Quick check: does this ticker have any quarterly segment rows in the DB?"""
        from utils.constants import SEGMENT_ALL_AXES
        clause, params = SegmentDataRepository._build_dim_clause(SEGMENT_ALL_AXES)
        sql = f"""
            SELECT COUNT(*) as cnt
            FROM coreiq_filing_metrics_v4
            WHERE ticker = :ticker
              AND is_dimensioned = 1
              AND numeric_value IS NOT NULL
              AND doc_type IN ('10-Q-Q1', '10-Q-Q2', '10-Q-Q3')
              AND period_start IS NOT NULL AND period_end IS NOT NULL
              AND DATEDIFF(period_end, period_start) BETWEEN 80 AND 100
              AND ({clause})
            LIMIT 1
        """
        r = db_manager.execute_query_readonly(sql, {"ticker": ticker, **params})
        return bool(r and r[0].get('cnt', 0) > 0)

    @staticmethod
    def get_segment_data(ticker: str, start_date: date, end_date: date, period_type: str = "annual") -> Dict[str, Any]:
        """Get segment data in CapIQ format — two tables: Business + Geographic.

        Returns:
            {
                "years": [2020, 2021, ...],
                "period_dates": {2020: date, ...},
                "business_segments": {
                    "Revenues": {"North America": {2020: 236282.0, ...}, ...},
                    "Operating Profit Before Tax": {...},
                    "Assets": {...},
                    "Depreciation & Amortization": {...},
                    "Capital Expenditure": {...},
                },
                "geo_segments": {
                    "Revenues": {"United States": {2020: 263520.0, ...}, ...},
                },
                "source": "db" | "edgartools",
            }
        Values are in Millions (raw XBRL dollars / 1_000_000).
        """
        # Quarterly path — DB only, no edgartools fallback
        if period_type.lower() == "quarterly":
            result = SegmentDataRepository._build_segment_tables_quarterly(ticker, start_date, end_date)
            if result is not None:
                return result
            return {
                "years": [], "period_dates": {},
                "business_segments": {}, "geo_segments": {},
                "revenue_totals": {}, "source": "none",
            }

        # Annual: Tier 1 — DB
        result = SegmentDataRepository._build_segment_tables_from_db(ticker, start_date, end_date)
        if result is not None:
            return result

        # Annual: Tier 2 — Fall back to edgartools
        edgar_result = SegmentDataRepository._fetch_from_edgartools(ticker)
        if edgar_result is not None:
            # Filter years to date range
            start_yr, end_yr = start_date.year, end_date.year
            years = [y for y in edgar_result["years"] if start_yr <= y <= end_yr]
            if years:
                edgar_result["years"] = years
                # Fetch authoritative revenue totals from income statement for edgartools path
                period_dates = edgar_result.get("period_dates", {})
                revenue_totals: Dict[int, Optional[float]] = {y: None for y in years}
                try:
                    _e_wide_start = start_date.replace(year=max(start_date.year - 1, 1900))
                    try:
                        _e_wide_end = end_date.replace(year=end_date.year + 1)
                    except ValueError:
                        _e_wide_end = end_date
                    _e_tot_rows = db_manager.execute_query_readonly(
                        """SELECT fiscal_date_ending, total_revenue
                           FROM coreiq_av_financials_income_statement
                           WHERE ticker = :ticker AND report_type = 'annual'
                             AND fiscal_date_ending BETWEEN :start_date AND :end_date
                           ORDER BY fiscal_date_ending ASC""",
                        {"ticker": ticker, "start_date": _e_wide_start, "end_date": _e_wide_end},
                    )
                    for _etr in (_e_tot_rows or []):
                        _efe = _etr.get('fiscal_date_ending')
                        _erv = _etr.get('total_revenue')
                        if _efe is None or _erv is None:
                            continue
                        _efe_yr = _efe.year if hasattr(_efe, 'year') else int(str(_efe)[:4])
                        if _efe_yr in revenue_totals and revenue_totals[_efe_yr] is None:
                            revenue_totals[_efe_yr] = float(_erv) / 1_000_000
                            continue
                        _e_best_yr = None
                        _e_min = 60
                        for _eyr, _epd in period_dates.items():
                            if _eyr not in revenue_totals or revenue_totals[_eyr] is not None:
                                continue
                            try:
                                _ed = abs((_efe - _epd).days) if (hasattr(_efe, 'year') and hasattr(_epd, 'year')) else 9999
                                if _ed < _e_min:
                                    _e_min = _ed
                                    _e_best_yr = _eyr
                            except Exception:
                                pass
                        if _e_best_yr is not None:
                            revenue_totals[_e_best_yr] = float(_erv) / 1_000_000
                except Exception:
                    pass
                edgar_result["revenue_totals"] = revenue_totals
                return edgar_result

        # Nothing found
        return {
            "years": [],
            "period_dates": {},
            "business_segments": {},
            "geo_segments": {},
            "source": "none",
        }


# =============================================================================
# RATINGS — process-level cache + background preload (survives across reruns)
# =============================================================================
import threading as _ratings_threading

_edgar_ratings_cache: Dict[str, Dict] = {}        # ticker → result dict
_edgar_ratings_futures: Dict[str, Any] = {}        # ticker → Future
_edgar_ratings_lock = _ratings_threading.Lock()
_edgar_ratings_executor: Any = None                # lazy-init ThreadPoolExecutor


def _get_edgar_executor():
    """Lazy-init a single background executor for preload tasks."""
    global _edgar_ratings_executor
    if _edgar_ratings_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _edgar_ratings_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edgar_preload")
    return _edgar_ratings_executor


def preload_edgartools_ratings(ticker: str) -> None:
    """Fire-and-forget background preload of edgartools credit ratings.

    Call this as soon as the ticker is known (any tab). If edgartools work
    is needed it runs in a daemon thread so the result is ready by the time
    the user clicks the Ratings tab.  No-op if already cached / in-flight.
    """
    with _edgar_ratings_lock:
        if ticker in _edgar_ratings_cache or ticker in _edgar_ratings_futures:
            return  # already done / in-progress

    # Always preload edgartools credit ratings — used to supplement DB data for missing years

    def _worker():
        try:
            result = RatingsDataRepository._edgartools_credit_ratings_impl(ticker)
            with _edgar_ratings_lock:
                _edgar_ratings_cache[ticker] = result
        except Exception:
            with _edgar_ratings_lock:
                _edgar_ratings_cache[ticker] = {}  # cache empty on error
        finally:
            with _edgar_ratings_lock:
                _edgar_ratings_futures.pop(ticker, None)

    with _edgar_ratings_lock:
        # Double-check after acquiring lock
        if ticker not in _edgar_ratings_cache and ticker not in _edgar_ratings_futures:
            future = _get_edgar_executor().submit(_worker)
            _edgar_ratings_futures[ticker] = future


# =============================================================================
# RATINGS & STORE COUNT REPOSITORY — credit ratings + store counts
# =============================================================================
class RatingsDataRepository:
    """Repository for credit ratings and store counts from coreiq_filing_metrics_v4.

    Queries source IN ('credit_rating', 'store_count') with parallel year-fetches for speed.
    Falls back to edgartools real-time extraction when DB has no credit_rating data.
    """

    # ── Regex constants for edgartools fallback ──
    _SP_RE = r'(?:AAA|AA[\+\-]?|A[\+\-]?|BBB[\+\-]?|BB[\+\-]?|B[\+\-]?|CCC[\+\-]?|CC|C|D)'
    _MOODYS_RE = r'(?:Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca|C)'
    _OUTLOOK_RE = r'(?:[Ss]table|[Nn]egative|[Pp]ositive|[Ww]atch)'
    _MOODYS_TO_SP = {
        "Aaa": "AAA", "Aa1": "AA+", "Aa2": "AA", "Aa3": "AA-",
        "A1": "A+", "A2": "A", "A3": "A-",
        "Baa1": "BBB+", "Baa2": "BBB", "Baa3": "BBB-",
        "Ba1": "BB+", "Ba2": "BB", "Ba3": "BB-",
        "B1": "B+", "B2": "B", "B3": "B-",
        "Caa1": "CCC+", "Caa2": "CCC", "Caa3": "CCC-",
        "Ca": "CC", "C": "C",
    }

    _YEAR_QUERY = """
        SELECT DISTINCT report_fiscal_year
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND source IN ('credit_rating', 'store_count')
        ORDER BY report_fiscal_year ASC
    """

    _ROW_QUERY = """
        SELECT ticker, company_name, report_fiscal_year, doc_type,
               concept, value, unit_ref, numeric_value,
               period_type, period_start, period_end, period_instant,
               label, original_label, standard_concept,
               statement_type, source, dimension, member,
               dimension_label, dimension_member_label, full_dimension_label,
               llm_query, filing_date
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND source IN ('credit_rating', 'store_count')
          AND report_fiscal_year = :year
        ORDER BY source ASC, dimension ASC, report_fiscal_year ASC
    """

    @staticmethod
    def _fetch_available_years(ticker: str) -> List[int]:
        rows = db_manager.execute_query_readonly(
            RatingsDataRepository._YEAR_QUERY, {"ticker": ticker},
        )
        return sorted(r['report_fiscal_year'] for r in rows if r.get('report_fiscal_year'))

    @staticmethod
    def _fetch_year_rows(ticker: str, year: int) -> List[Dict[str, Any]]:
        return db_manager.execute_query_readonly(
            RatingsDataRepository._ROW_QUERY, {"ticker": ticker, "year": year},
        )

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch_all_rows(ticker: str) -> List[Dict[str, Any]]:
        """Fetch ALL credit_rating + store_count rows, years in parallel."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        years = RatingsDataRepository._fetch_available_years(ticker)
        if not years:
            return []

        all_rows: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(len(years), 6)) as pool:
            futures = {
                pool.submit(RatingsDataRepository._fetch_year_rows, ticker, yr): yr
                for yr in years
            }
            for future in as_completed(futures):
                rows = future.result()
                if rows:
                    all_rows.extend(rows)

        all_rows.sort(key=lambda r: (r.get('source', ''), r.get('report_fiscal_year', 0)))
        return all_rows

    # ─────────────────────────────────────────────────────────────────────
    #  EdgarTools fallback — regex credit rating extraction from 10-K text
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_ratings_from_text(text: str) -> List[Dict[str, Any]]:
        """Apply regex patterns to 10-K section text to extract credit ratings.

        Agency names matched case-insensitively; rating tokens matched case-sensitively
        to avoid false positives (e.g., lowercase "c" or "a" in prose).

        Returns list of dicts: [{"agency": "S&P", "rating": "BBB+", "outlook": "Stable"}, ...]
        """
        import re as _re

        # Rating tokens — CASE-SENSITIVE (Moody's uses mixed case like "Baa2")
        SP = RatingsDataRepository._SP_RE
        MY = RatingsDataRepository._MOODYS_RE
        OL = RatingsDataRepository._OUTLOOK_RE
        # Require 2+ chars or word boundary for short S&P tokens to avoid "a", "c", "d" false positives
        SP_SAFE = r'(?:AAA|AA[\+\-]?|A[\+\-]|BBB[\+\-]?|BB[\+\-]?|B[\+\-]|CCC[\+\-]?|CC|D)'  # excludes bare "A", "B", "C"
        SP_WORDBOUNDED = rf'\b({SP})\b'  # full set but with word boundaries

        candidates: List[Dict[str, Any]] = []
        seen: set = set()

        def _add(agency: str, rating: str, match_obj):
            if agency in seen:
                return
            # Validate rating is actually a proper rating (case-sensitive check)
            if not _re.match(rf'^{SP}$', rating) and not _re.match(rf'^{MY}$', rating):
                return
            # Reject single-letter lowercase matches (false positives)
            if len(rating) == 1 and rating.islower():
                return
            seen.add(agency)
            # Find outlook near the match
            ctx_start = max(0, match_obj.start() - 50)
            ctx_end = min(len(text), match_obj.end() + 150)
            ctx = text[ctx_start:ctx_end]
            outlook_m = _re.search(OL, ctx)  # case-sensitive for outlook
            outlook = outlook_m.group().capitalize() if outlook_m else None
            candidates.append({"agency": agency, "rating": rating, "outlook": outlook})

        # ── Phase 1: Agency-anchored patterns ──
        # Rating tokens are CASE-SENSITIVE (no re.IGNORECASE on capture groups).
        # Agency names use inline case alternatives to stay case-flexible.

        # S&P  — (?!\d) prevents matching commercial paper (e.g. A-1+) as A-
        for m in _re.finditer(
            rf'(?:S&P|Standard\s*[&\s]+Poor)[^\n.]{{0,120}}?(?:rat(?:es?|ed|ing)|assign|affirm)[^\n.]{{0,80}}?({SP})(?!\d)',
            text
        ):
            _add("S&P", m.group(1), m)
        if "S&P" not in seen:
            for m in _re.finditer(rf'(?:S&P|Standard[^A-Za-z]{{0,15}}Poor)[^\n]{{0,40}}?({SP_SAFE})(?!\d)', text):
                _add("S&P", m.group(1), m)

        # Moody's — no IGNORECASE; inline [Mm] for agency, rating stays case-sensitive
        for m in _re.finditer(
            rf"[Mm]oody'?s[^\n.]{{0,120}}?(?:rat(?:es?|ed|ing)|assign|affirm)[^\n.]{{0,80}}?({MY})",
            text
        ):
            _add("Moody's", m.group(1), m)
        if "Moody's" not in seen:
            for m in _re.finditer(rf"[Mm]oody'?s[^\n]{{0,40}}?({MY})", text):
                _add("Moody's", m.group(1), m)

        # Fitch — no IGNORECASE; inline [Ff] for agency; (?!\d) excludes F1+/F2
        for m in _re.finditer(
            rf'[Ff]itch[^\n.]{{0,120}}?(?:rat(?:es?|ed|ing)|assign|affirm)[^\n.]{{0,80}}?({SP})(?!\d)',
            text
        ):
            _add("Fitch", m.group(1), m)
        if "Fitch" not in seen:
            for m in _re.finditer(rf'[Ff]itch[^\n]{{0,40}}?({SP_SAFE})(?!\d)', text):
                _add("Fitch", m.group(1), m)

        # ── Phase 2a: Compressed table — "Moody'sS&P\nFitch\nLong-term debtA2AA\n..." ──
        table_m = _re.search(
            rf"[Mm]oody.{{0,6}}S&P.{{0,20}}[Ff]itch.{{0,80}}(?:[Ll]ong.term|[Ss]enior)[^\n]{{0,20}}?({MY})({SP})",
            text,
        )
        if table_m:
            if "Moody's" not in seen:
                _add("Moody's", table_m.group(1), table_m)
            if "S&P" not in seen:
                _add("S&P", table_m.group(2), table_m)
            rest = text[table_m.end():table_m.end() + 30]
            fitch_m = _re.match(rf'\s*({SP_SAFE})', rest)
            if fitch_m and "Fitch" not in seen:
                _add("Fitch", fitch_m.group(1), table_m)

        # ── Phase 2b: 2-col table — "Debt RatingsS&PMoody's\n...\nSenior DebtBBB+Baa1" ──
        table_m2 = _re.search(
            rf"(?:[Dd]ebt|[Cc]redit)\s*[Rr]atings\s*S&P[\s\S]{{0,15}}?[Mm]oody[\s\S]{{0,200}}?"
            rf"(?:[Ss]enior|[Ll]ong.term)[^\n]{{0,20}}?({SP_SAFE})(?!\d)({MY})",
            text,
        )
        if table_m2:
            if "S&P" not in seen:
                _add("S&P", table_m2.group(1), table_m2)
            if "Moody's" not in seen:
                _add("Moody's", table_m2.group(2), table_m2)
            # Check for Fitch as 3rd column
            rest2 = text[table_m2.end():table_m2.end() + 30]
            fitch_m2 = _re.match(rf'\s*({SP_SAFE})(?!\d)', rest2)
            if fitch_m2 and "Fitch" not in seen:
                _add("Fitch", fitch_m2.group(1), table_m2)
            # Check for Outlook row after the table
            outlook_block = text[table_m2.end():table_m2.end() + 200]
            ol_m = _re.search(r'[Oo]utlook\s*(Stable|Negative|Positive)', outlook_block)
            if ol_m:
                outlook_val = ol_m.group(1).capitalize()
                for c in candidates:
                    if c.get('outlook') is None:
                        c['outlook'] = outlook_val

        # ── Phase 3: Generic "credit rating" patterns (fallback) ──
        if not candidates:
            for m in _re.finditer(rf'credit\s+rating[^\n.]{{0,60}}\b({SP_SAFE})\b', text, _re.IGNORECASE):
                _add("S&P", m.group(1), m)
                break

        return candidates

    @staticmethod
    def _edgartools_credit_ratings_impl(ticker: str) -> Dict[int, List[Dict[str, Any]]]:
        """Core implementation — no Streamlit dependency.

        Processes up to 6 most-recent 10-K filings in parallel (6 workers).
        Returns {fiscal_year: [{"agency","rating","outlook"}, ...], "_filing_dates": {...}}
        """
        import sys as _sys
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _process_filing(filing):
            try:
                tenk = filing.obj()
                fy = filing.filing_date.year if filing.filing_date else None
                if not fy:
                    return None
                for item_key in ["Item 7", "Item 1A", "Item 1", "Item 8"]:
                    try:
                        section = tenk[item_key]
                        section_text = str(section) if section else ""
                        if len(section_text) < 200:
                            continue
                        extracted = RatingsDataRepository._extract_ratings_from_text(section_text)
                        if extracted:
                            return (fy, extracted, filing.filing_date)
                    except Exception:
                        continue
                return None
            except Exception:
                return None

        try:
            if 'edgar' not in _sys.modules:
                _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "edgartools"))
            from edgar import Company as EdgarCompany

            company = EdgarCompany(ticker)
            filings = company.get_filings(form="10-K")
            if not filings or len(filings) == 0:
                return {}
            filing_list = list(filings)[:6]
            results: Dict[int, List[Dict[str, Any]]] = {}
            filing_dates: Dict[int, Any] = {}

            with ThreadPoolExecutor(max_workers=min(len(filing_list), 6)) as pool:
                futures = {pool.submit(_process_filing, f): f for f in filing_list}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        fy, extracted, fd = result
                        results[fy] = extracted
                        filing_dates[fy] = fd

            if results:
                results["_filing_dates"] = filing_dates  # type: ignore[assignment]

            return results

        except Exception:
            return {}

    @staticmethod
    @st.cache_data(ttl=3600, show_spinner=False)
    def _edgartools_credit_ratings(ticker: str) -> Dict[int, List[Dict[str, Any]]]:
        """Wrapper with st.cache_data.  Checks process-level preload cache first."""
        # Check process-level cache (populated by background preload)
        with _edgar_ratings_lock:
            if ticker in _edgar_ratings_cache:
                return _edgar_ratings_cache[ticker]
            future = _edgar_ratings_futures.get(ticker)

        # If preload is in-flight, wait for it (up to 120s)
        if future is not None:
            try:
                future.result(timeout=120)
            except Exception:
                pass
            with _edgar_ratings_lock:
                if ticker in _edgar_ratings_cache:
                    return _edgar_ratings_cache[ticker]

        # No preload — compute directly
        result = RatingsDataRepository._edgartools_credit_ratings_impl(ticker)
        with _edgar_ratings_lock:
            _edgar_ratings_cache[ticker] = result
        return result

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    def _edgartools_stores_by_country(ticker: str) -> Dict[str, Any]:
        """Fetch stores-by-country data from edgartools XBRL for up to 6 recent 10-Ks.

        Uses us-gaap:NumberOfStores (and related concepts) dimensioned by
        srt:StatementGeographicalAxis to get per-country store counts per year.

        Returns:
            {
                "years": [2023, 2024, ...],
                "period_dates": {2023: date(...), ...},
                "countries": {
                    "United States": {2023: 629, 2024: 614, ...},
                    "Canada":         {2023: 108, ...},
                    ...
                }
            }
            or {} on failure / no data.
        """
        import sys
        from pathlib import Path
        _edgartools_path = str(Path(__file__).resolve().parent.parent.parent / "edgartools")
        if _edgartools_path not in sys.path:
            sys.path.insert(0, _edgartools_path)

        _STORE_CONCEPTS = {
            'us-gaap:numberofstores',
            'us-gaap:numberofrestaurants',
            'us-gaap:numberofunitsoperated',
            'us-gaap:numberofoperatinglocations',
        }

        try:
            from edgar import Company
            from concurrent.futures import ThreadPoolExecutor, as_completed

            company = Company(ticker)
            filings = company.get_filings(form='10-K')
            if not filings:
                return {}

            filing_list = list(filings[:6])
            countries: Dict[str, Dict[int, int]] = {}
            period_dates: Dict[int, 'date'] = {}

            def _process(filing_obj):
                local: list = []
                try:
                    xbrl = filing_obj.xbrl()
                    if not xbrl:
                        return local
                    # Use by_dimension() to fetch only geo-dimensioned facts — much faster
                    # than fetching all facts and filtering in Python.
                    facts = xbrl.query().by_dimension("StatementGeographicalAxis").with_dimensions().execute()
                    for f in (facts or []):
                        concept = (f.get('concept') or '').lower()
                        if concept not in _STORE_CONCEPTS:
                            continue
                        country_label = f.get('dimension_member_label') or f.get('label') or ''
                        country_label = country_label.title()
                        if not country_label:
                            continue
                        # Normalize US variants to a single canonical label
                        _cl_lower = country_label.lower()
                        if _cl_lower.startswith('united states') or _cl_lower in (
                            'u.s.', 'us', 'u.s. operations', 'us operations', 'domestic',
                            'united states of america'
                        ):
                            country_label = 'United States'
                        fy = f.get('fiscal_year')
                        val = f.get('numeric_value')
                        if fy and val is not None:
                            pe = f.get('period_instant') or f.get('period_end')
                            local.append((country_label, int(fy), int(val), pe))
                except Exception:
                    pass
                return local

            with ThreadPoolExecutor(max_workers=3) as pool:
                futs = [pool.submit(_process, f) for f in filing_list]
                for fut in as_completed(futs):
                    for country_label, fy, val, pe in (fut.result() or []):
                        if country_label not in countries:
                            countries[country_label] = {}
                        # Keep the first (most recent filing's) value per year
                        if fy not in countries[country_label]:
                            countries[country_label][fy] = val
                        if pe and fy not in period_dates:
                            try:
                                from datetime import date as _dt
                                if isinstance(pe, str):
                                    pe = _dt.fromisoformat(pe)
                                period_dates[fy] = pe
                            except Exception:
                                pass

            if not countries:
                return {}

            all_years = sorted(set(y for vals in countries.values() for y in vals))
            return {
                "years": all_years,
                "period_dates": period_dates,
                "countries": countries,
            }
        except Exception:
            return {}

    # ─────────────────────────────────────────────────────────────────────
    #  Square Footage — DB-first + edgartools fallback
    # ─────────────────────────────────────────────────────────────────────

    _SQFT_DB_QUERY = """
        SELECT ticker, concept, label, numeric_value, unit_ref,
               report_fiscal_year, fiscal_period, doc_type, is_dimensioned,
               dimension_member_label, full_dimension_label
        FROM coreiq_filing_metrics_v4
        WHERE ticker = :ticker
          AND (concept IN (
                  'us-gaap:AreaOfRealEstateProperty',
                  'us-gaap:NetRentableArea',
                  'us-gaap:AreaOfLand',
                  'us-gaap:NumberOfRealEstateProperties',
                  'us-gaap:GrossLeasableArea',
                  'us-gaap:LandSubjectToGroundLeases'
               )
               OR unit_ref IN ('sqft', 'SqFt', 'acre', 'acres'))
          AND numeric_value IS NOT NULL
          AND report_fiscal_year IS NOT NULL
        ORDER BY concept, report_fiscal_year ASC
    """

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    def _fetch_sqft_from_db(ticker: str) -> List[Dict[str, Any]]:
        """Fetch square footage XBRL facts from coreiq_filing_metrics_v4."""
        raw = db_manager.execute_query_readonly(
            RatingsDataRepository._SQFT_DB_QUERY, {"ticker": ticker},
        )
        # Normalise column names to match the dict keys expected downstream
        results = []
        for r in raw:
            concept = r.get('concept', '')
            tag = concept.split(':')[-1] if ':' in concept else concept
            results.append({
                'ticker': r.get('ticker'),
                'tag': tag,
                'value': r.get('numeric_value'),
                'unit': r.get('unit_ref', ''),
                'fy': r.get('report_fiscal_year'),
                'fp': r.get('fiscal_period', ''),
                'form': r.get('doc_type', ''),
                'label': r.get('label', ''),
                'is_dimensioned': r.get('is_dimensioned', 0),
                'dimension_member_label': r.get('dimension_member_label', ''),
                'full_dimension_label': r.get('full_dimension_label', ''),
            })
        return results

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    def _fetch_sqft_from_edgartools(ticker: str) -> List[Dict[str, Any]]:
        """Fetch square footage data from SEC EDGAR via edgartools as fallback.

        Looks for AreaOfRealEstateProperty, NetRentableArea, AreaOfLand,
        NumberOfRealEstateProperties, GrossLeasableArea concepts.
        """
        import sys
        from pathlib import Path
        _edgartools_path = str(Path(__file__).resolve().parent.parent.parent / "edgartools")
        if _edgartools_path not in sys.path:
            sys.path.insert(0, _edgartools_path)

        _SQFT_CONCEPTS = {
            'us-gaap:AreaOfRealEstateProperty',
            'us-gaap:NetRentableArea',
            'us-gaap:AreaOfLand',
            'us-gaap:NumberOfRealEstateProperties',
            'us-gaap:GrossLeasableArea',
            'us-gaap:LandSubjectToGroundLeases',
        }

        try:
            from edgar import Company
            company = Company(ticker)
            facts = company.get_facts()
            df = facts.to_dataframe()

            # Filter to square footage concepts
            mask = df['concept'].isin(_SQFT_CONCEPTS)
            sqft_df = df[mask]

            if sqft_df.empty:
                return []

            results = []
            for _, row in sqft_df.iterrows():
                fy = row.get('fiscal_year')
                if fy is None:
                    continue
                results.append({
                    'ticker': ticker,
                    'tag': row.get('concept', '').replace('us-gaap:', ''),
                    'value': row.get('numeric_value'),
                    'unit': row.get('unit', ''),
                    'fy': int(fy),
                    'fp': row.get('fiscal_period', ''),
                    'form': '',
                    'end_date': row.get('period_end'),
                    'label': row.get('label', ''),
                })
            return results
        except Exception:
            return []

    @staticmethod
    @st.cache_data(ttl=600, show_spinner=False)
    def get_square_footage_data(ticker: str, start_year: int, end_year: int) -> Dict[str, Any]:
        """Get square footage / property area data organized by metric and year.

        DB-first from coreiq_filing_metrics_v4, with edgartools
        fallback via SEC EDGAR company facts API.

        Returns:
            {
                "years": [2020, 2021, ...],
                "metrics": [
                    {"metric": "Area of Real Estate Property", "unit": "sqft",
                     "values": {2020: 700000, 2021: 750000, ...}},
                    ...
                ],
                "has_data": bool,
                "_source": "db" | "edgartools" | "none",
            }
        """
        # ── 1. Try DB first ──
        db_rows = RatingsDataRepository._fetch_sqft_from_db(ticker)

        # Filter to the requested year range; prefer annual (FY/10-K), fall back to quarterly
        annual_rows = [
            r for r in db_rows
            if r.get('fy') and start_year <= r['fy'] <= end_year
            and (r.get('fp') in (None, '', 'FY') or r.get('form') == '10-K')
        ]
        # If no annual, include quarterly data as fallback
        if not annual_rows:
            annual_rows = [
                r for r in db_rows
                if r.get('fy') and start_year <= r['fy'] <= end_year
            ]

        source = "none"
        rows_to_use = annual_rows

        # ── 2. If no DB data, try edgartools ──
        if not rows_to_use:
            edgar_rows = RatingsDataRepository._fetch_sqft_from_edgartools(ticker)
            edgar_annual = [
                r for r in edgar_rows
                if r.get('fy') and start_year <= r['fy'] <= end_year
                and r.get('fp', '') in (None, '', 'FY')
            ]
            if not edgar_annual:
                edgar_annual = [
                    r for r in edgar_rows
                    if r.get('fy') and start_year <= r['fy'] <= end_year
                ]
            if edgar_annual:
                rows_to_use = edgar_annual
                source = "edgartools"
        else:
            source = "db"

        if not rows_to_use:
            return {"years": [], "metrics": [], "has_data": False, "_source": "none"}

        # ── 3. Group by tag/metric ──
        _TAG_LABELS = {
            'AreaOfRealEstateProperty': 'Area of Real Estate Property',
            'NetRentableArea': 'Net Rentable Area',
            'AreaOfLand': 'Area of Land',
            'NumberOfRealEstateProperties': 'Number of Real Estate Properties',
            'GrossLeasableArea': 'Gross Leasable Area',
            'LandSubjectToGroundLeases': 'Land Subject to Ground Leases',
        }
        _TAG_UNITS = {
            'AreaOfRealEstateProperty': 'sq ft',
            'NetRentableArea': 'sq ft',
            'AreaOfLand': 'acres',
            'GrossLeasableArea': 'sq ft',
            'NumberOfRealEstateProperties': 'properties',
            'LandSubjectToGroundLeases': 'sq ft',
        }
        # Normalise raw unit_ref values from coreiq_filing_metrics_v4
        _UNIT_NORMALIZE = {
            'sqft': 'sq ft', 'sqf': 'sq ft', 'u_sqft': 'sq ft',
            'acre': 'acres', 'u_acre': 'acres',
            'store': 'properties', 'stores': 'properties',
            'number_of_store': 'properties',
        }

        metrics_map: Dict[str, Dict[str, Any]] = {}
        all_years: set = set()

        for row in rows_to_use:
            tag = row.get('tag', '')
            fy = row['fy']
            val = row.get('value')
            if val is None:
                continue

            # For dimensioned data, use tag+member_label as key to keep separate metrics
            label_text = row.get('label', '') or ''
            dim_member = row.get('dimension_member_label', '') or ''
            is_dim = row.get('is_dimensioned', 0)
            group_key = f"{tag}|{dim_member or label_text}" if is_dim else tag

            if group_key not in metrics_map:
                raw_unit = (row.get('unit') or '').lower()
                norm_unit = _UNIT_NORMALIZE.get(raw_unit)
                if norm_unit:
                    display_unit = norm_unit
                elif raw_unit.startswith('unit') or not raw_unit:
                    # Opaque XBRL unit IDs (Unit15, Unit_Standard_sqft_*, etc.)
                    # – fall back to the concept's known unit
                    display_unit = _TAG_UNITS.get(tag, raw_unit if raw_unit else 'units')
                else:
                    display_unit = _TAG_UNITS.get(tag, raw_unit)
                # Use dimension member label for dimensioned data, or DB label
                if is_dim and dim_member:
                    display_label = dim_member
                elif is_dim and label_text:
                    display_label = label_text
                else:
                    display_label = _TAG_LABELS.get(tag, label_text or tag)
                metrics_map[group_key] = {
                    "metric": display_label,
                    "unit": display_unit,
                    "values": {},
                }
            # Keep the latest value per year (prefer 10-K)
            if fy not in metrics_map[group_key]["values"]:
                metrics_map[group_key]["values"][fy] = val
            all_years.add(fy)

        years = sorted(all_years)
        metrics = sorted(metrics_map.values(), key=lambda x: x["metric"])

        return {
            "years": years,
            "metrics": metrics,
            "has_data": len(metrics) > 0,
            "_source": source,
        }

    @staticmethod
    def get_date_range(ticker: str) -> Tuple[Optional[date], Optional[date]]:
        rows = RatingsDataRepository._fetch_all_rows(ticker)
        if not rows:
            edgar_data = RatingsDataRepository._edgartools_credit_ratings(ticker)
            edgar_years = [k for k in edgar_data if isinstance(k, int)]
            if edgar_years:
                return date(min(edgar_years), 1, 31), date(max(edgar_years), 12, 31)
            return None, None
        years = sorted(set(r['report_fiscal_year'] for r in rows if r.get('report_fiscal_year')))
        if not years:
            return None, None
        return date(years[0], 1, 31), date(years[-1], 12, 31)

    @staticmethod
    def get_available_dates(ticker: str) -> List[date]:
        rows = RatingsDataRepository._fetch_all_rows(ticker)
        years = sorted(set(r['report_fiscal_year'] for r in rows if r.get('report_fiscal_year')))
        return [date(y, 12, 31) for y in years]

    @staticmethod
    def get_ratings_data(ticker: str, start_date: date, end_date: date) -> Dict[str, Any]:
        """Get credit ratings + store counts organized by year.

        DB-first with edgartools fallback for credit_rating data.

        Returns dict with:
            - years: list of fiscal years in range
            - period_dates: {year: filing_date}
            - credit_ratings: list of dicts with per-year rating values
            - store_counts: list of dicts with per-year count values
            - has_credit_ratings: bool
            - has_store_counts: bool
        """
        start_year = start_date.year
        end_year = end_date.year

        # ── Phase 1: DB + credit ratings in parallel (both lightweight/cached) ─
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as _pool1:
            _f_db = _pool1.submit(RatingsDataRepository._fetch_all_rows, ticker)
            _f_cr = _pool1.submit(RatingsDataRepository._edgartools_credit_ratings, ticker)
            try:
                all_rows = _f_db.result()
            except Exception:
                all_rows = []
            _edgar_data_raw: Dict[str, Any] = {}
            try:
                _edgar_data_raw = _f_cr.result() or {}
            except Exception:
                pass

        filtered = [r for r in all_rows if r.get('report_fiscal_year') and start_year <= r['report_fiscal_year'] <= end_year]

        db_has_cr = any(r.get('source') == 'credit_rating' for r in filtered)

        # ── Process edgartools credit ratings ─────────────────────────────────
        edgar_cr_map: Dict[str, Dict[str, Any]] = {}
        edgar_years: set = set()
        edgar_period_dates: Dict[int, date] = {}
        try:
            edgar_data = _edgar_data_raw
            edgar_filing_dates = edgar_data.pop("_filing_dates", {}) if isinstance(edgar_data.get("_filing_dates"), dict) else {}
            for fy, ratings_list in edgar_data.items():
                if not isinstance(fy, int) or fy < start_year or fy > end_year:
                    continue
                edgar_years.add(fy)
                fd = edgar_filing_dates.get(fy)
                if fd and hasattr(fd, 'year'):
                    edgar_period_dates[fy] = fd
                for r in ratings_list:
                    agency = r.get("agency", "Unknown")
                    if agency not in edgar_cr_map:
                        edgar_cr_map[agency] = {
                            "agency": agency,
                            "label": f"Credit Rating ({agency})",
                            "values": {},
                            "outlooks": {},
                            "details": {},
                        }
                    edgar_cr_map[agency]["values"][fy] = r.get("rating")
                    edgar_cr_map[agency]["outlooks"][fy] = r.get("outlook")
        except Exception:
            pass

        # Merge years from DB + edgartools
        db_years = sorted(set(r['report_fiscal_year'] for r in filtered if r.get('report_fiscal_year')))
        all_years = sorted(set(db_years) | edgar_years)

        if not all_years:
            return {"years": [], "period_dates": {}, "credit_ratings": [], "store_counts": [],
                    "has_credit_ratings": False, "has_store_counts": False}

        years = all_years

        # Build period_dates from DB filing_date or period_instant
        period_dates: Dict[int, date] = {}
        for row in filtered:
            yr = row.get('report_fiscal_year')
            fd = row.get('filing_date') or row.get('period_instant')
            if yr and fd and hasattr(fd, 'year'):
                if yr not in period_dates or fd > period_dates[yr]:
                    period_dates[yr] = fd
        # Merge edgartools filing dates for years not in DB
        for fy, fd in edgar_period_dates.items():
            if fy not in period_dates:
                period_dates[fy] = fd

        # Separate credit ratings and store counts from DB
        cr_rows = [r for r in filtered if r.get('source') == 'credit_rating']
        sc_rows = [r for r in filtered if r.get('source') == 'store_count']

        # Credit ratings: group by agency (dimension) — DB rows
        cr_map: Dict[str, Dict[str, Any]] = {}  # key = agency
        for row in cr_rows:
            agency = row.get('dimension') or 'Unknown'
            if agency not in cr_map:
                cr_map[agency] = {
                    "agency": agency,
                    "label": f"Credit Rating ({agency})",
                    "values": {y: None for y in years},
                    "outlooks": {y: None for y in years},
                    "details": {y: None for y in years},
                }
            yr = row['report_fiscal_year']
            if yr in cr_map[agency]["values"]:
                _val = row.get('value')
                _member = row.get('member')
                cr_map[agency]["values"][yr] = _val if _val and str(_val).lower() != 'none' else None
                cr_map[agency]["outlooks"][yr] = _member if _member and str(_member).lower() != 'none' else None
                # Parse llm_query JSON for extra detail
                lq = row.get('llm_query')
                if lq:
                    try:
                        import json
                        cr_map[agency]["details"][yr] = json.loads(lq) if isinstance(lq, str) else lq
                    except Exception:
                        pass

        # Merge edgartools credit ratings — DB data takes priority; edgartools fills gaps
        if edgar_cr_map:
            for agency, edgar_entry in edgar_cr_map.items():
                if agency not in cr_map:
                    cr_map[agency] = {
                        "agency": agency,
                        "label": edgar_entry["label"],
                        "values": {y: edgar_entry["values"].get(y) for y in years},
                        "outlooks": {y: edgar_entry["outlooks"].get(y) for y in years},
                        "details": {y: None for y in years},
                    }
                else:
                    # Fill years missing from DB with edgartools data
                    for yr in years:
                        if cr_map[agency]["values"].get(yr) is None and edgar_entry["values"].get(yr) is not None:
                            cr_map[agency]["values"][yr] = edgar_entry["values"][yr]
                        if cr_map[agency]["outlooks"].get(yr) is None and edgar_entry["outlooks"].get(yr) is not None:
                            cr_map[agency]["outlooks"][yr] = edgar_entry["outlooks"][yr]

        # Store counts: group by dimension (store type)
        sc_map: Dict[str, Dict[str, Any]] = {}  # key = store_type
        for row in sc_rows:
            store_type = row.get('dimension') or 'Total'
            if store_type not in sc_map:
                sc_map[store_type] = {
                    "store_type": store_type,
                    "label": f"Store Count ({store_type.title()})",
                    "values": {y: None for y in years},
                }
            yr = row['report_fiscal_year']
            if yr in sc_map[store_type]["values"]:
                sc_map[store_type]["values"][yr] = row.get('numeric_value')

        # Sort agencies and store types alphabetically
        credit_ratings = sorted(cr_map.values(), key=lambda x: x["agency"])
        store_counts = sorted(sc_map.values(), key=lambda x: x["store_type"])

        # ── Phase 2: stores-by-country + square footage in parallel ──────────
        _stores_by_country_raw: Dict[str, Any] = {}
        _sqft_raw: Dict[str, Any] = {"years": [], "metrics": [], "has_data": False, "_source": "none"}
        with ThreadPoolExecutor(max_workers=2) as _pool2:
            _f_sbc  = _pool2.submit(RatingsDataRepository._edgartools_stores_by_country, ticker)
            _f_sqft = _pool2.submit(RatingsDataRepository.get_square_footage_data, ticker, 2000, end_year)
            try:
                _stores_by_country_raw = _f_sbc.result() or {}
            except Exception:
                pass
            try:
                _sqft_raw = _f_sqft.result() or _sqft_raw
            except Exception:
                pass

        stores_by_country_data: Dict[str, Any] = _stores_by_country_raw

        sbc_years_all = stores_by_country_data.get("years", [])
        sbc_years = [y for y in sbc_years_all if start_year <= y <= end_year]
        sbc_countries = stores_by_country_data.get("countries", {})
        sbc_period_dates = stores_by_country_data.get("period_dates", {})

        for _fy, _fd in sbc_period_dates.items():
            if _fy not in period_dates and _fd:
                period_dates[_fy] = _fd

        sqft_data: Dict[str, Any] = _sqft_raw

        _source = "db" if db_has_cr else ("edgartools" if edgar_cr_map else "none")

        return {
            "years": years,
            "period_dates": period_dates,
            "credit_ratings": credit_ratings,
            "store_counts": store_counts,
            "stores_by_country": {"years": sbc_years, "countries": sbc_countries},
            "square_footage": sqft_data,
            "has_credit_ratings": len(credit_ratings) > 0,
            "has_store_counts": len(store_counts) > 0,
            "has_stores_by_country": bool(sbc_years and sbc_countries),
            "has_square_footage": sqft_data.get("has_data", False),
            "_source": _source,
        }


# =============================================================================
# EXECUTIVE COMPENSATION REPOSITORY
# =============================================================================

class ExecutiveCompensationRepository:
    """Fetch executive compensation data from SEC and YF sources.

    SEC source  → coreiq_executives_compensation (column-level, multi-year)
    YF source   → coreiq_yf_company_overview.payload_json → info.companyOfficers
                  (only totalPay available, single snapshot / latest)
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _to_float(val: Any) -> Optional[float]:
        """Safely cast a raw DB value (str / int / None) to float."""
        if val is None or val == '':
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _fmt_currency(val: Optional[float]) -> Optional[str]:
        """Format a numeric dollar value with commas, e.g. 3000000 → '$3,000,000'."""
        if val is None:
            return None
        return f"${val:,.0f}"

    # ── SEC company: latest year only (Company Profile tab) ──────────────────

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_sec_latest_compensation(ticker: str) -> List[Dict[str, Any]]:
        """Return all executives for the latest compensation_year for a SEC ticker.

        Returns a list of dicts, each with keys:
            executive_name, position, compensation_year,
            salary, bonus, stock_awards, total_compensation
        Values are already formatted strings (None for missing/empty).
        """
        try:
            query = """
                SELECT executive_name, position, compensation_year,
                       salary, bonus, stock_awards, total_compensation
                FROM coreiq_executives_compensation
                WHERE ticker = :ticker
                  AND compensation_year = (
                      SELECT MAX(compensation_year)
                      FROM coreiq_executives_compensation
                      WHERE ticker = :ticker
                  )
                ORDER BY
                    CASE WHEN total_compensation IS NOT NULL AND total_compensation != ''
                         THEN CAST(total_compensation AS UNSIGNED)
                         ELSE 0
                    END DESC
            """
            rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
            result = []
            for r in rows:
                result.append({
                    "executive_name":   r.get("executive_name") or None,
                    "position":         r.get("position") or None,
                    "compensation_year": r.get("compensation_year") or None,
                    "salary":           ExecutiveCompensationRepository._fmt_currency(
                                            ExecutiveCompensationRepository._to_float(r.get("salary"))),
                    "bonus":            ExecutiveCompensationRepository._fmt_currency(
                                            ExecutiveCompensationRepository._to_float(r.get("bonus"))),
                    "stock_awards":     ExecutiveCompensationRepository._fmt_currency(
                                            ExecutiveCompensationRepository._to_float(r.get("stock_awards"))),
                    "total_compensation": ExecutiveCompensationRepository._fmt_currency(
                                            ExecutiveCompensationRepository._to_float(r.get("total_compensation"))),
                })
            return result
        except Exception as e:
            try:
                from utils.server_logger import log_structured_error
                log_structured_error(e, page="repository",
                                     component="ExecutiveCompensationRepository",
                                     operation="get_sec_latest_compensation")
            except Exception:
                pass
            return []

    # ── YF company: officers from payload_json (Company Profile tab) ─────────

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_yf_latest_compensation(ticker: str) -> List[Dict[str, Any]]:
        """Return companyOfficers from the YF payload for a NON-SEC ticker.

        Returns a list of dicts.  Only keys present in the data are included
        (dynamic — no salary / bonus / stock_awards for YF; only total_pay).
        Each dict: executive_name, position, compensation_year, total_pay
        (values are formatted strings or None when absent).
        """
        try:
            import json as _json
            query = """
                SELECT payload_json
                FROM coreiq_yf_company_overview
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """
            rows = db_manager.execute_query_readonly(query, {"ticker": ticker})
            if not rows:
                return []
            payload = rows[0].get("payload_json")
            if not payload:
                return []
            data = _json.loads(payload)
            officers = data.get("info", {}).get("companyOfficers", [])
            result = []
            for o in officers:
                total_pay = ExecutiveCompensationRepository._to_float(o.get("totalPay"))
                # Skip officers with no pay data — nothing useful to display
                if total_pay is None:
                    continue
                _fy = o.get("fiscalYear")
                _year = str(int(_fy)) if _fy is not None and str(_fy) not in ("", "0", "None") else None
                result.append({
                    "executive_name":    o.get("name") or None,
                    "position":          o.get("title") or None,
                    "compensation_year": _year,
                    "total_pay":         ExecutiveCompensationRepository._fmt_currency(total_pay),
                })
            return result
        except Exception as e:
            try:
                from utils.server_logger import log_structured_error
                log_structured_error(e, page="repository",
                                     component="ExecutiveCompensationRepository",
                                     operation="get_yf_latest_compensation")
            except Exception:
                pass
            return []

    # ── ALL-YEARS data for SEC tickers (People Screening tab) ────────────────

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_sec_all_compensation(tickers: Tuple[str, ...]) -> List[Dict[str, Any]]:
        """Return ALL historical compensation rows for a set of SEC tickers.

        Args:
            tickers: Tuple of ticker strings (must be tuple for st.cache_data key).

        Returns:
            List of dicts with keys:
                ticker, executive_name, position, compensation_year,
                salary, bonus, stock_awards, total_compensation  (raw floats)
        """
        if not tickers:
            return []
        try:
            placeholders = ", ".join([f":t{i}" for i in range(len(tickers))])
            params = {f"t{i}": t for i, t in enumerate(tickers)}
            query = f"""
                SELECT ticker, executive_name, position, compensation_year,
                       salary, bonus, stock_awards, total_compensation
                FROM coreiq_executives_compensation
                WHERE ticker IN ({placeholders})
                ORDER BY ticker, compensation_year DESC,
                         CAST(COALESCE(NULLIF(total_compensation,''),'0') AS UNSIGNED) DESC
            """
            rows = db_manager.execute_query_readonly(query, params)
            result = []
            _ff = ExecutiveCompensationRepository._to_float
            for r in rows:
                salary  = _ff(r.get("salary"))
                bonus   = _ff(r.get("bonus"))
                stock   = _ff(r.get("stock_awards"))
                total   = _ff(r.get("total_compensation"))
                # Skip rows with absolutely no money data
                if salary is None and bonus is None and stock is None and total is None:
                    continue
                result.append({
                    "ticker":             r.get("ticker"),
                    "executive_name":     r.get("executive_name") or "",
                    "position":           r.get("position") or "",
                    "compensation_year":  r.get("compensation_year") or "",
                    "salary":             salary,
                    "bonus":              bonus,
                    "stock_awards":       stock,
                    "total_compensation": total,
                })
            return result
        except Exception as e:
            try:
                from utils.server_logger import log_structured_error
                log_structured_error(e, page="repository",
                                     component="ExecutiveCompensationRepository",
                                     operation="get_sec_all_compensation")
            except Exception:
                pass
            return []

    # ── YF compensation for bulk tickers (People Screening tab) ─────────────

    @staticmethod
    @st.cache_data(ttl=300, show_spinner=False)
    def get_yf_all_compensation(tickers: Tuple[str, ...]) -> List[Dict[str, Any]]:
        """Return companyOfficers for all YF tickers in bulk.

        Returns a list of dicts with keys:
            ticker, executive_name, position, compensation_year, total_pay (float)
        """
        if not tickers:
            return []
        try:
            import json as _json
            placeholders = ", ".join([f":t{i}" for i in range(len(tickers))])
            params = {f"t{i}": t for i, t in enumerate(tickers)}
            # Use ROW_NUMBER to pick ONLY the latest ingested_at row per ticker.
            # Without this, coreiq_yf_company_overview returns 57-86 rows per
            # ticker (one per daily ETL run) → every officer appears ~80 times.
            query = f"""
                SELECT ticker, payload_json
                FROM (
                    SELECT ticker, payload_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker
                               ORDER BY ingested_at DESC
                           ) AS _rn
                    FROM coreiq_yf_company_overview
                    WHERE ticker IN ({placeholders})
                ) _ranked
                WHERE _rn = 1
            """
            rows = db_manager.execute_query_readonly(query, params)
            result = []
            _ff = ExecutiveCompensationRepository._to_float
            for r in rows:
                tk = r.get("ticker", "")
                payload = r.get("payload_json")
                if not payload:
                    continue
                try:
                    data = _json.loads(payload)
                    officers = data.get("info", {}).get("companyOfficers", [])
                    for o in officers:
                        total_pay = _ff(o.get("totalPay"))
                        # Skip officers with no pay data
                        if total_pay is None:
                            continue
                        _fy = o.get("fiscalYear")
                        _year = str(int(_fy)) if _fy is not None and str(_fy) not in ("", "0", "None") else ""
                        result.append({
                            "ticker":            tk,
                            "executive_name":    o.get("name") or "",
                            "position":          o.get("title") or "",
                            "compensation_year": _year,
                            "total_pay":         total_pay,
                        })
                except Exception:
                    continue
            return result
        except Exception as e:
            try:
                from utils.server_logger import log_structured_error
                log_structured_error(e, page="repository",
                                     component="ExecutiveCompensationRepository",
                                     operation="get_yf_all_compensation")
            except Exception:
                pass
            return []


# =============================================================================
# EARNINGS CALLS CACHE WARMUP — one-shot per process lifetime
# =============================================================================
_ec_warmup_triggered: bool = False


def warmup_ec_caches() -> None:
    """Pre-warm st.cache_data for EC company lists. One-shot per process.

    Runs get_non_sec_transcript_companies() and get_companies_with_earnings()
    in a daemon background thread so the first real user session never pays
    the cold-cache penalty. The thread is safe to spawn from main.py on every
    Streamlit rerender because the _ec_warmup_triggered guard ensures the
    actual DB calls happen only once.
    """
    global _ec_warmup_triggered
    if _ec_warmup_triggered:
        return
    _ec_warmup_triggered = True

    import threading as _th

    def _run():
        try:
            EarningsCallRepository.get_non_sec_transcript_companies()
        except Exception:
            pass
        try:
            EarningsCallRepository.get_companies_with_earnings()
        except Exception:
            pass

    _th.Thread(target=_run, daemon=True, name="ec-cache-warmup").start()
