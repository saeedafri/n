"""
Cache Manager - Centralized cache control for Research Portal
===============================================================

Provides:
- Manual cache clear functions for emergency updates
- Cache statistics and monitoring
- Automatic cache warming for static data
- BACKGROUND cache warming for heavy queries

Usage:
    from utils.cache_manager import clear_news_cache, get_cache_stats

    # Clear cache when new company added
    clear_news_cache()

    # Get cache statistics
    stats = get_cache_stats()

    # Start background warming on app startup
    start_background_warmup()

Date: March 8, 2026
Author: AI Assistant
"""
import streamlit as st
import time
import threading
from typing import Dict, Any, Optional
from datetime import datetime

# Background warmup state
_background_warmup_started = False
_background_warmup_lock = threading.Lock()

# Import server logger (error only)
try:
    from utils.server_logger import log_structured_error, log_error
except ImportError:
    import logging
    log_error = logging.error
    log_structured_error = None


def clear_news_cache() -> Dict[str, Any]:
    """
    Clear all news-related caches.

    Call this when:
    - New company added to database
    - Sector assignments changed
    - Emergency data refresh needed

    Returns:
        Dict with status and cleared cache names
    """
    cleared = []
    errors = []

    try:
        # Import here to avoid circular imports
        from data.repository import NewsRepository

        # Clear cached methods
        if hasattr(NewsRepository.get_sectors, 'clear'):
            NewsRepository.get_sectors.clear()
            cleared.append('get_sectors')


        if hasattr(NewsRepository.get_companies, 'clear'):
            NewsRepository.get_companies.clear()
            cleared.append('get_companies')


        if hasattr(NewsRepository.get_news_date_range, 'clear'):
            NewsRepository.get_news_date_range.clear()
            cleared.append('get_news_date_range')


        # Note: get_articles is NOT cleared intentionally (short 5-min TTL)
        # It will auto-expire naturally



        return {
            'success': True,
            'cleared': cleared,
            'errors': errors,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        log_error(f"[CACHE_CLEAR] Error clearing news cache: {e}")
        return {
            'success': False,
            'cleared': cleared,
            'errors': [str(e)],
            'timestamp': datetime.now().isoformat()
        }


def clear_company_cache() -> Dict[str, Any]:
    """
    Clear company-related caches.

    Call this when:
    - New company added
    - Company metadata updated
    """
    cleared = []
    errors = []

    try:
        from data.repository import CompanyRepository, CompanyOverviewRepository

        if hasattr(CompanyRepository.get_companies, 'clear'):
            CompanyRepository.get_companies.clear()
            cleared.append('CompanyRepository.get_companies')


        if hasattr(CompanyOverviewRepository.get_company_overview, 'clear'):
            CompanyOverviewRepository.get_company_overview.clear()
            cleared.append('CompanyOverviewRepository.get_company_overview')


        return {
            'success': True,
            'cleared': cleared,
            'errors': errors,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        log_error(f"[CACHE_CLEAR] Error clearing company cache: {e}")
        return {
            'success': False,
            'cleared': cleared,
            'errors': [str(e)],
            'timestamp': datetime.now().isoformat()
        }


def clear_all_caches() -> Dict[str, Any]:
    """
    Clear ALL application caches.

    WARNING: This will cause temporary performance degradation
    as all data needs to be re-fetched from database.

    Use only for:
    - Major database updates
    - Emergency data consistency issues
    - Application upgrades
    """
    try:
        results = {
            'news_cache': clear_news_cache(),
            'company_cache': clear_company_cache(),
            'timestamp': datetime.now().isoformat()
        }

        total_cleared = (
            len(results['news_cache'].get('cleared', [])) +
            len(results['company_cache'].get('cleared', []))
        )

        return results
    except Exception as exc:
        log_structured_error(exc, page="cache_manager", component="clear_all_caches",
                             operation="clear_all", context="emergency full cache clear")
        return {
            'success': False,
            'timestamp': datetime.now().isoformat()
        }


def get_cache_stats() -> Dict[str, Any]:
    """
    Get cache statistics.

    Returns:
        Dict with cache status and TTL information
    """
    try:
        # Streamlit doesn't provide introspection into cache contents
        # but we can document the expected behavior

        return {
            'cached_functions': {
                'NewsRepository.get_sectors': {
                    'ttl_seconds': 3600,
                    'ttl_human': '1 hour',
                    'description': 'Sector dropdown options'
                },
                'NewsRepository.get_companies': {
                    'ttl_seconds': 3600,
                    'ttl_human': '1 hour',
                    'description': 'Company dropdown options'
                },
                'NewsRepository.get_news_date_range': {
                    'ttl_seconds': 1800,
                    'ttl_human': '30 minutes',
                    'description': 'Min/max news dates'
                },
                'NewsRepository.get_articles': {
                    'ttl_seconds': 300,
                    'ttl_human': '5 minutes',
                    'description': 'News articles (short TTL for freshness)'
                },
                'CompanyRepository.get_companies': {
                    'ttl_seconds': 600,
                    'ttl_human': '10 minutes',
                    'description': 'All companies with data'
                },
                'CompanyOverviewRepository.get_company_overview': {
                    'ttl_seconds': 600,
                    'ttl_human': '10 minutes',
                    'description': 'Company profile data'
                }
            },
            'manual_clear_functions': [
                'clear_news_cache()',
                'clear_company_cache()',
                'clear_all_caches()'
            ],
            'auto_clear_triggers': [
                'TTL expiry (automatic)',
                'App restart',
                'Manual clear via this module'
            ]
        }
    except Exception as exc:
        log_structured_error(exc, page="cache_manager", component="get_cache_stats",
                             operation="get_stats", context="stats lookup")
        return {}


def warm_caches(include_earnings: bool = True) -> Dict[str, Any]:
    """
    Warm up caches by pre-fetching static data.

    Call this on app startup to ensure first user gets fast response.

    Args:
        include_earnings: If True, also warm earnings calls caches
                         (can be slow, so optional for foreground warming)
    """
    start_time = time.perf_counter()
    warmed = []
    errors = []

    try:
        from data.repository import NewsRepository, CompanyRepository
        from datetime import date, timedelta



        # Warm news date range
        try:
            date_range = NewsRepository.get_news_date_range()
            warmed.append(f"news_date_range: {date_range.get('min_date')} to {date_range.get('max_date')}")
        except Exception as e:
            errors.append(f"news_date_range: {e}")

        # Warm sectors (with default date range)
        try:
            sectors = NewsRepository.get_sectors()
            warmed.append(f"sectors: {len(sectors)} sectors")
        except Exception as e:
            errors.append(f"sectors: {e}")

        # Warm YF topic keys — pre-populates 6h cache so newsroom category dropdown
        # costs 0ms on every page load instead of ~500ms+ on first visit.
        try:
            from utils.constants import merge_yf_topics
            yf_topics = NewsRepository.get_yf_topic_keys()
            merge_yf_topics(yf_topics)
            warmed.append(f"yf_topic_keys: {len(yf_topics)} topics")
        except Exception as e:
            errors.append(f"yf_topic_keys: {e}")

        # Warm ticker → sector map (for newsroom client-side filtering)
        try:
            tsmap = NewsRepository.get_ticker_sector_map()
            warmed.append(f"ticker_sector_map: {len(tsmap)} tickers")
        except Exception as e:
            errors.append(f"ticker_sector_map: {e}")

        # Warm companies list
        try:
            companies = NewsRepository.get_companies()
            warmed.append(f"companies: {len(companies)} companies")
        except Exception as e:
            errors.append(f"companies: {e}")

        # Warm main company list
        try:
            all_companies = CompanyRepository.get_companies()
            warmed.append(f"all_companies: {len(all_companies)} companies")
        except Exception as e:
            errors.append(f"all_companies: {e}")

        # Warm earnings companies (THIS IS THE BIG ONE - 23s query)
        if include_earnings:
            try:
                from data.repository import EarningsCallRepository

                _earnings_start = time.perf_counter()
                earnings_companies = EarningsCallRepository.get_companies_with_earnings()
                _earnings_elapsed = (time.perf_counter() - _earnings_start) * 1000
                warmed.append(f"earnings_companies: {len(earnings_companies)} companies ({_earnings_elapsed:.0f}ms)")

            except Exception as e:
                errors.append(f"earnings_companies: {e}")

        elapsed_ms = (time.perf_counter() - start_time) * 1000



        return {
            'success': len(errors) == 0,
            'warmed': warmed,
            'errors': errors,
            'elapsed_ms': elapsed_ms,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        log_error(f"[CACHE_WARM] Cache warm-up failed: {e}")
        return {
            'success': False,
            'warmed': warmed,
            'errors': [str(e)],
            'elapsed_ms': (time.perf_counter() - start_time) * 1000,
            'timestamp': datetime.now().isoformat()
        }


def _background_warmup_thread():
    """
    Background thread function for cache warming.

    Two-track strategy:
    - Sectors / companies / date_range: call @st.cache_data functions directly.
      These are fast (1-3s cold), short lock window, cache population is the goal.
    - AV + YF articles: run raw DB queries ONLY in PARALLEL — no @st.cache_data.
      Goal is to warm the MySQL buffer pool so the first user's query runs in
      ~3s (warm) instead of ~33s (cold). @st.cache_data populates on the first
      real user request. Raw queries hold no @st.cache_data lock → zero blocking.
      2026-03-27: AV + YF warm queries now fire in parallel (was sequential).
    """
    try:
        time.sleep(0.5)  # Minimal delay to let pg.run() start (engines are initialized)
        from datetime import date, timedelta
        _warm_date_to   = date.today()
        _warm_date_from = _warm_date_to - timedelta(days=7)

        # ── Track 0: Earnings Calendar warmup (FIRST — it's the slowest page) ──
        # The calendar page runs three independent slow queries on a cold cache
        # (events UNION ~8.8s, fiscal-year-end map ~7.1s, tickers ~6.6s). Warm
        # them up front so EC is cached within ~15s of boot — before real users
        # navigate to it. Run SEQUENTIALLY (not a parallel pool): the page itself
        # already parallelizes these four queries, so a concurrent warmup would
        # double DB load (thundering herd) for any visit during the warmup window
        # and run SLOWER than no warmup at all. Sequential keeps DB pressure low.
        try:
            from data.repository import EarningsCalendarRepository
            for _warm_fn in (
                EarningsCalendarRepository.get_available_tickers,
                EarningsCalendarRepository._get_fiscal_year_end_map,
                EarningsCalendarRepository.get_calendar_events,  # also warms IR + companies map
            ):
                try:
                    _warm_fn()
                except Exception:
                    pass
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Earnings calendar warmup error: {e}")

        # ── Track 1: populate @st.cache_data for static dropdowns (PARALLEL) ──
        # All 4 calls fire concurrently. Total = max(individual) ≈ 2.7s
        # instead of sum ≈ 3.7s when sequential.
        try:
            from data.repository import NewsRepository, CompanyRepository
            from concurrent.futures import ThreadPoolExecutor as _T1Pool

            def _safe_call(fn):
                try:
                    fn()
                except Exception:
                    pass

            with _T1Pool(max_workers=4) as _t1:
                _t1_futs = [
                    _t1.submit(_safe_call, NewsRepository.get_sectors),
                    _t1.submit(_safe_call, NewsRepository.get_ticker_sector_map),
                    _t1.submit(_safe_call, NewsRepository.get_news_date_range),
                    _t1.submit(_safe_call, CompanyRepository.get_companies_rows),
                ]
                for _f in _t1_futs:
                    _f.result(timeout=30)
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Dropdown warmup error: {e}")

        # ── Track 2: raw DB buffer-pool warmup for AV + YF (PARALLEL) ─────────
        # Does NOT call @st.cache_data decorated functions — no lock contention.
        # Warms covering-index pages so first user query is fast.
        # 2026-03-27: Made AV + YF warm-up queries run in parallel (was sequential).
        try:
            from core.database import db_manager
            from concurrent.futures import ThreadPoolExecutor
            params = {'date_from': _warm_date_from, 'date_to': _warm_date_to}

            def _warm_av():
                # Warm AV: touch idx_av_time_id covering index pages
                db_manager.execute_query_readonly(
                    """
                    SELECT n.id
                    FROM coreiq_av_market_news_sentiment n USE INDEX (idx_av_time_id)
                    WHERE n.time_published_utc >= :date_from
                      AND n.time_published_utc < :date_to
                    ORDER BY n.time_published_utc DESC
                    LIMIT 2000
                    """,
                    params,
                )

            def _warm_yf():
                # Warm YF: touch idx_yf_pub_nid_ticker covering index pages
                db_manager.execute_query_readonly(
                    """
                    SELECT y.news_id, y.ticker
                    FROM coreiq_yf_market_news_sentiment y
                         USE INDEX (idx_yf_pub_nid_ticker)
                    WHERE y.published_at >= :date_from
                      AND y.published_at < :date_to
                    ORDER BY y.published_at DESC
                    LIMIT 60000
                    """,
                    params,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                av_f = pool.submit(_warm_av)
                yf_f = pool.submit(_warm_yf)
                try:
                    av_f.result()
                except Exception:
                    pass
                try:
                    yf_f.result()
                except Exception:
                    pass

        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Buffer-pool warmup error: {e}")

        # ── Earnings (parallelized SEC + non-SEC warmup) ─────────────────────
        try:
            from data.repository import EarningsCallRepository
            from concurrent.futures import ThreadPoolExecutor as _ETP

            def _warm_sec_companies():
                EarningsCallRepository.get_companies_with_earnings()

            def _warm_non_sec_companies():
                EarningsCallRepository.get_non_sec_transcript_companies()

            with _ETP(max_workers=2) as _ep:
                _ef1 = _ep.submit(_warm_sec_companies)
                _ef2 = _ep.submit(_warm_non_sec_companies)
                try:
                    _ef1.result(timeout=60)
                except Exception:
                    pass
                try:
                    _ef2.result(timeout=60)
                except Exception:
                    pass
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Earnings warmup error: {e}")

    except Exception as exc:
        log_structured_error(exc, page="cache_manager", component="_background_warmup_thread",
                             operation="background_warmup", context="background thread fatal error")


def start_background_warmup() -> bool:
    """
    Start background cache warming in a separate thread.

    Call this ONCE during app startup (main.py). The heavy queries
    (like earnings companies) will load in background while user
    interacts with other pages.

    Returns:
        True if thread started, False if already running

    Example (in main.py):
        from utils.cache_manager import start_background_warmup
        start_background_warmup()  # Non-blocking
    """
    global _background_warmup_started

    try:
        with _background_warmup_lock:
            if _background_warmup_started:

                return False

            _background_warmup_started = True

            thread = threading.Thread(
                target=_background_warmup_thread,
                name="CacheWarmup",
                daemon=True,
            )
            # NOTE: do NOT add_script_run_ctx here.
            # The heavy warmup queries (AV/YF) bypass @st.cache_data intentionally.
            # Propagating session context would cause @st.cache_data lock contention
            # and block the first real user request for up to 33s on cold DB.
            thread.start()


            return True
    except Exception as exc:
        log_structured_error(exc, page="cache_manager", component="start_background_warmup",
                             operation="start_thread", context="background warmup thread start")
        return False


# ============================================================================
# STREAMLIT UI COMPONENTS
# ============================================================================

def render_cache_controls():
    """
    Render cache control buttons for admin/debug use.

    Usage in Streamlit page:
        from utils.cache_manager import render_cache_controls
        render_cache_controls()
    """
    try:
        st.subheader("Cache Management")

        col1, col2, col3 = st.columns(3)

        with col1:
            if st.button("🗑️ Clear News Cache", help="Clear sectors, companies, date range"):
                with st.spinner("Clearing..."):
                    result = clear_news_cache()
                    if result['success']:
                        st.success(f"Cleared: {', '.join(result['cleared'])}")
                    else:
                        st.error(f"Error: {result['errors']}")

        with col2:
            if st.button("🗑️ Clear Company Cache", help="Clear company lists and profiles"):
                with st.spinner("Clearing..."):
                    result = clear_company_cache()
                    if result['success']:
                        st.success(f"Cleared: {', '.join(result['cleared'])}")
                    else:
                        st.error(f"Error: {result['errors']}")

        with col3:
            if st.button("⚠️ Clear ALL Caches", help="Emergency: Clear everything"):
                st.warning("This will slow down the app temporarily!")
                if st.checkbox("Confirm clear all caches"):
                    with st.spinner("Clearing all caches..."):
                        result = clear_all_caches()
                        st.success("All caches cleared!")

        # Show cache stats
        with st.expander("📊 Cache Information"):
            stats = get_cache_stats()

            st.write("**Cached Functions:**")
            for func_name, info in stats['cached_functions'].items():
                st.write(f"- `{func_name}`: {info['ttl_human']} - {info['description']}")

            st.write("**Manual Clear:**")
            for func in stats['manual_clear_functions']:
                st.write(f"- `{func}`")
    except Exception as exc:
        log_structured_error(exc, page="cache_manager", component="render_cache_controls",
                             operation="render_ui", context="admin cache control UI render")
        return


if __name__ == "__main__":
    # Test cache manager
    print("Cache Manager Test")
    print("==================")
    print("\nCache Stats:")
    print(get_cache_stats())
