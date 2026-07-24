"""Source detection for SEC vs YFinance companies."""
import streamlit as st
from typing import Literal, Optional
from core.database import db_manager
from utils.server_logger import log_structured_error, log_error


@st.cache_data(ttl=21600, show_spinner=False)
def get_company_source(ticker: str) -> Optional[Literal['SEC', 'YFinance']]:
    """
    Detect data source for a ticker.

    Handles composite tickers (e.g. 'TSCO.L' → exchange_acronym='L') so that
    duplicate base tickers resolve to the correct company/source.

    Returns:
        'SEC' for SEC-listed companies
        'YFinance' for YFinance companies
        None if not found
    """
    try:
        if '.' in ticker:
            base, acr = ticker.rsplit('.', 1)
            results = db_manager.execute_query_readonly(
                "SELECT source FROM coreiq_companies WHERE ticker = :t AND exchange_acronym = :a LIMIT 1",
                {'t': base.strip(), 'a': acr.strip()},
            )
        else:
            results = db_manager.execute_query_readonly(
                "SELECT source FROM coreiq_companies WHERE ticker = :t AND (exchange_acronym IS NULL OR exchange_acronym = '') LIMIT 1",
                {'t': ticker.strip()},
            )
        if results:
            return results[0]['source']
        # Fallback: plain ticker match (handles non-duplicate tickers without exchange_acronym quirk)
        results = db_manager.execute_query_readonly(
            "SELECT source FROM coreiq_companies WHERE ticker = TRIM(:ticker) LIMIT 1",
            {'ticker': ticker.split('.')[0] if '.' in ticker else ticker},
        )
        return results[0]['source'] if results else None
    except Exception as e:
        log_structured_error(e, page="source_router", component="get_company_source", operation="querying_company_source")
        return None


def is_sec_company(ticker: str) -> bool:
    """Check if company is SEC-listed."""
    try:
        return get_company_source(ticker) == 'SEC'
    except Exception as e:
        log_structured_error(e, page="source_router", component="is_sec_company", operation="checking_sec_status")
        return False


def is_yf_company(ticker: str) -> bool:
    """Check if company is YFinance (NON-SEC)."""
    try:
        return get_company_source(ticker) == 'YFinance'
    except Exception as e:
        log_structured_error(e, page="source_router", component="is_yf_company", operation="checking_yf_status")
        return False
