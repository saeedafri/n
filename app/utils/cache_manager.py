"""
Cache Manager - Centralized cache control for Research Portal
"""
import builtins
import os
import streamlit as st
import time
import threading
from typing import Dict, Any, Optional
from datetime import datetime

# Process-global warm-once guard — survives Streamlit script reloads within one worker.
if not hasattr(builtins, "_mdp_warmup_started"):
    builtins._mdp_warmup_started = False

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

        # ── Track 0: Calendar warmup (RUNS FIRST — it's the slowest page and the
        # most latency-sensitive for clients). Warm BEFORE the earnings-calls
        # company lists below so a user who opens /calendar right after a deploy
        # is a cache HIT, not a ~25s cold DB fan-out (get_available_tickers +
        # get_ma_completion_events are the cold long-poles). Sequential to avoid a
        # cold-connection-pool storm against Azure MySQL.
        try:
            from data.repository import EarningsCalendarRepository

            for _warm_fn, _warm_args in (
                (EarningsCalendarRepository.get_available_tickers, ()),
                (EarningsCalendarRepository._get_fiscal_year_end_map, ()),
                # Warm the FULL deduped set (disk-materialized) — the calendar page
                # now counts this for its badge and windows it in Python for render,
                # so warming it means both the first load AND every month/year
                # navigation are cache hits (no ~3.6s dedup SQL).
                (EarningsCalendarRepository.get_calendar_events_full, ()),
                (EarningsCalendarRepository.get_ma_completion_events, ()),
            ):
                try:
                    _warm_fn(*_warm_args)
                except Exception:
                    pass
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Earnings calendar warmup error: {e}")

        # ── Track -1: Earnings calls company lists (~23s cold without warmup) ──
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
                    _ef1.result(timeout=90)
                except Exception:
                    pass
                try:
                    _ef2.result(timeout=90)
                except Exception:
                    pass
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Earnings calls warmup error: {e}")

        _warm_date_to   = date.today()
        _warm_date_from = _warm_date_to - timedelta(days=7)

        # ── Track 1: populate @st.cache_data for static dropdowns (SEQUENTIAL) ──
        # Sequential to avoid connection storms on Azure MySQL cold start.
        try:
            from data.repository import NewsRepository, CompanyRepository
            from data.revenue_forecast_service import RevenueForecastService

            for _warm_fn in (
                NewsRepository.get_sectors,
                NewsRepository.get_ticker_sector_map,
                NewsRepository.get_news_date_range,
                CompanyRepository.get_companies_rows,
                RevenueForecastService.get_companies,
            ):
                try:
                    _warm_fn()
                except Exception:
                    pass
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Dropdown warmup error: {e}")

        # ── Track 1b: Company Filings dropdown (SEC master + NON-SEC blob scan) ──
        try:
            from pages.company_filings import _load_companies_cached

            _load_companies_cached()
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Filings companies warmup error: {e}")

        # ── Track 1c: Market Data DEFAULT ticker (AMZN) slow tabs ──────────────
        # STG 03-Jul: the Segment (date_range ~4.6s) and Ratings (~10s) tabs pay
        # cold Azure-buffer latency on first touch per ticker. AMZN is the default
        # landing ticker, so warming its two heaviest fetches makes the common
        # market_data landing fast; other tickers still warm lazily on first view.
        try:
            from data.repository import SegmentDataRepository, RatingsDataRepository
            try:
                # Segment tab: AMZN is the default landing ticker (has segments).
                # Underlies get_date_range / get_available_dates / get_segment_data.
                SegmentDataRepository._fetch_all_db_rows("AMZN")
            except Exception:
                pass
            try:
                # Ratings tab: warm a flagship RETAILER that has credit ratings
                # (AMZN has none — ratings are retailer-only). M (Macy's) hit ~10s
                # cold on STG 03-Jul; warming it makes that common view fast.
                _r1, _r2 = RatingsDataRepository.get_date_range("M")
                if _r1 and _r2:
                    RatingsDataRepository.get_ratings_data("M", _r1, _r2)
            except Exception:
                pass
        except Exception as e:
            log_error(f"[CACHE_WARM_BG] Market data warmup error: {e}")

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

        # ── Track 3: Additional Data (ratings) full-universe warm sweep ───────
        # STG evidence (15-Jul): the first open of the Additional Data tab per
        # ticker burns 2-4.3s in bounded waits on cold EDGAR fetches, while a
        # warm render is ~1ms. Every EDGAR layer persists on disk for 24h
        # (edgar_cache/store_totals, stores_by_country, credit_ratings, sqft),
        # so with App Service Always On this throttled walk over the whole
        # company universe — repeated every 12h — keeps every ticker warm for
        # every user. First sweep may take hours on a fresh cache dir (cold
        # credit-rating extraction is 30-60s/ticker); later sweeps are mostly
        # cache validations and finish in minutes. Runs LAST: it must never
        # delay the page-critical tracks above.
        # DISABLED BY DEFAULT (18-Jul): the per-ticker edgartools XBRL parse it does
        # is the confirmed RAM driver — STG log 18-Jul shows RSS spiking to 3.7GB
        # (avail→95MB, swap engaged) during this sweep, amplified by it running in
        # BOTH gunicorn workers. Credit ratings still load on-demand when a user
        # opens a filing. Re-enable explicitly with RATINGS_WARM_SWEEP=1.
        if os.getenv("RATINGS_WARM_SWEEP", "0").strip().lower() in ("1", "true", "yes", "on"):
            try:
                from datetime import date as _sweep_date
                from data.repository import CompanyRepository, RatingsDataRepository
                from utils.server_logger import log_timing

                _SWEEP_INTERVAL_S = 12 * 3600
                _SWEEP_DELAY_S = 2.0   # gentle on SEC EDGAR and the DB
                # Stay out of the post-boot window: STG restarts often
                # (21 in the 9 days to 16-Jul), and right after a restart
                # interactive traffic is already paying cold caches — the
                # sweep competing for DB I/O then makes first clicks worse.
                time.sleep(300)
                while True:
                    _sweep_t0 = time.time()
                    log_timing("RATINGS_warm_sweep_start", 0,
                               details=f"pid={os.getpid()}")
                    # Buffer-pool warm for the tab's bottleneck query: no
                    # (ticker, source) composite index exists on the 12.5M-row
                    # coreiq_filing_metrics_v5, so MySQL serves the per-ticker
                    # fetch from the source-only index + ticker post-filter —
                    # ~1,020 scattered row pages, ~5.7s each on a cold Azure
                    # buffer. One query touching those rows' off-index columns
                    # pulls every page hot, making all per-ticker fetches ~0.3s.
                    try:
                        from core.database import db_manager as _dbm
                        _dbm.execute_query_readonly(
                            """
                            SELECT COALESCE(SUM(LENGTH(value)), 0),
                                   COALESCE(SUM(LENGTH(llm_query)), 0)
                            FROM coreiq_filing_metrics_v5
                            WHERE source IN ('credit_rating', 'store_count')
                            """,
                            {},
                        )
                    except Exception:
                        pass
                    try:
                        _rows = CompanyRepository.get_companies_rows() or []
                        _sweep_tickers = sorted({str(r.get("ticker") or "").upper()
                                                 for r in _rows if r.get("ticker")})
                    except Exception:
                        _sweep_tickers = []
                    _warmed = 0
                    for _tkr in _sweep_tickers:
                        try:
                            from data.repository import SegmentDataRepository
                            RatingsDataRepository._fetch_all_rows(_tkr)
                            RatingsDataRepository._edgartools_store_totals(_tkr)
                            RatingsDataRepository._edgartools_stores_by_country(_tkr)
                            RatingsDataRepository._edgartools_credit_ratings(_tkr)
                            # DB-first like the render; only warms the EDGAR disk
                            # layer for tickers whose sqft data isn't in the DB.
                            RatingsDataRepository.get_square_footage_data(
                                _tkr, 2000, _sweep_date.today().year)
                            SegmentDataRepository._fye_month(_tkr)  # header dates
                            # Geographies for the "Store Count (...)" header.
                            # Built here (5-14s each) so no render ever pays it.
                            from data.repository import (
                                _build_geo_segment_members, _geo_members_cache_path,
                                _write_cache_atomic)
                            _write_cache_atomic(
                                _geo_members_cache_path(_tkr),
                                {"cached_at": time.time(),
                                 "members": _build_geo_segment_members(_tkr)})
                            _warmed += 1
                        except Exception:
                            pass
                        time.sleep(_SWEEP_DELAY_S)
                    log_timing("RATINGS_warm_sweep", (time.time() - _sweep_t0) * 1000,
                               details=f"tickers={_warmed}/{len(_sweep_tickers)}")
                    time.sleep(max(60.0, _SWEEP_INTERVAL_S - (time.time() - _sweep_t0)))
            except Exception as e:
                log_error(f"[CACHE_WARM_BG] Ratings warm sweep error: {e}")

    except Exception as exc:
        log_structured_error(exc, page="cache_manager", component="_background_warmup_thread",
                             operation="background_warmup", context="background thread fatal error")


def start_background_warmup() -> bool:
    """Start background cache warming in a separate thread (once per process)."""
    global _background_warmup_started

    if os.getenv("WARM_ON_BOOT", "1").strip().lower() in ("0", "false", "no", "off"):
        return False

    try:
        with _background_warmup_lock:
            if _background_warmup_started or getattr(builtins, "_mdp_warmup_started", False):
                return False

            _background_warmup_started = True
            builtins._mdp_warmup_started = True

            # Explicit restart marker: fires exactly once per process, so
            # counting these lines in the server log == counting restarts
            # (STG restarted 21× in the 9 days to 16-Jul — each one wipes
            # every in-process cache and re-triggers the cold-start window).
            try:
                from utils.server_logger import log_timing as _boot_log
                _boot_log("APP_PROCESS_BOOT", 0, details=f"pid={os.getpid()}")
            except Exception:
                pass

            thread = threading.Thread(
                target=_background_warmup_thread,
                name="CacheWarmup",
                daemon=True,
            )
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
