"""
Ticker Utility Functions
========================
Centralized ticker validation and fallback handling for all pages.
Ensures graceful fallback to default ticker (M) when invalid tickers are provided.
"""
import streamlit as st
from typing import Optional, Tuple
from data.repository import CompanyRepository
from utils.server_logger import log_structured_error, log_error

# Default fallback ticker - AMZN (Amazon) has complete data in all tables:
# - 206 income statement records
# - 103 balance sheet records
# - 103 cash flow records
# - 6,613 stock price records
# - 80 earnings call transcripts (most among candidates)
# - 39,995 filing metrics
# - All SEC filings available
DEFAULT_FALLBACK_TICKER = "AMZN"


def validate_and_get_ticker(
    url_ticker: Optional[str],
    session_ticker: Optional[str],
    page_name: str = "unknown"
) -> Tuple[str, bool]:
    """
    Validate ticker from URL or session and return valid ticker with fallback.

    Args:
        url_ticker: Ticker from URL query params (may be invalid/non-existent)
        session_ticker: Ticker from session state
        page_name: Name of the page for logging

    Returns:
        Tuple of (valid_ticker, was_fallback_used)
        - valid_ticker: The validated ticker (or fallback if invalid)
        - was_fallback_used: True if fallback was applied, False otherwise
    """
    # Priority: URL ticker > Session ticker > Default fallback
    candidate = url_ticker or session_ticker or DEFAULT_FALLBACK_TICKER

    # If no ticker provided, use default
    if not candidate:
        return DEFAULT_FALLBACK_TICKER, True

    # Check if ticker exists in database
    try:
        company = CompanyRepository.get_company_by_ticker(candidate)
        if company:
            # Valid ticker found
            return company.ticker or candidate, False
    except Exception as e:
        log_error(f"[{page_name}] Error checking ticker {candidate}: {e}")

    # Ticker not found in DB - fallback to default

    # Update URL to reflect fallback (optional - keeps URL in sync)
    try:
        if url_ticker:
            st.query_params["ticker"] = DEFAULT_FALLBACK_TICKER
    except Exception:
        pass  # Don't fail if URL update fails

    return DEFAULT_FALLBACK_TICKER, True


def get_ticker_from_source(
    page_name: str = "unknown",
    use_url: bool = True,
    use_session: bool = True
) -> str:
    """
    Get valid ticker from URL and/or session with automatic fallback.

    This is the main function pages should use.

    Args:
        page_name: Name of the page for logging
        use_url: Whether to check URL query params
        use_session: Whether to check session state

    Returns:
        Valid ticker string (guaranteed to exist in DB)
    """
    try:
        url_ticker = st.query_params.get("ticker") if use_url else None
        session_ticker = st.session_state.get("active_ticker") if use_session else None

        valid_ticker, was_fallback = validate_and_get_ticker(
            url_ticker=url_ticker,
            session_ticker=session_ticker,
            page_name=page_name
        )

        # Update session state with valid ticker
        if valid_ticker != session_ticker:
            st.session_state["active_ticker"] = valid_ticker

        return valid_ticker
    except Exception as e:
        log_structured_error(e, page="ticker_utils", component="get_ticker_from_source", operation="resolving_ticker")
        return DEFAULT_FALLBACK_TICKER


def check_ticker_exists(ticker: str) -> bool:
    """
    Check if a ticker exists in the database.

    Args:
        ticker: Ticker symbol to check

    Returns:
        True if ticker exists, False otherwise
    """
    if not ticker:
        return False
    try:
        company = CompanyRepository.get_company_by_ticker(ticker)
        return company is not None
    except Exception as e:
        log_error(f"[check_ticker_exists] Error checking {ticker}: {e}")
        return False
