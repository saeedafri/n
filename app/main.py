# app/main.py
"""
Coresight Research Portal - Main Entry Point (STG VERSION)
Uses ONLY server_logger - no standard library logging
"""

import sys
import os

# glibc malloc arenas — must be set before first heap allocation
os.environ.setdefault("MALLOC_ARENA_MAX", "2")

# Optional Python allocation tracing for the memory forensics report. Started
# as early as possible (before heavy imports) so [MEM_REPORT] can name the exact
# file:line holding RAM. Off unless MEM_TRACEMALLOC=1 (has ~memory+CPU overhead).
if os.environ.get("MEM_TRACEMALLOC", "0").strip().lower() in ("1", "true", "yes", "on"):
    try:
        import tracemalloc as _tm
        if not _tm.is_tracing():
            _tm.start(int(os.environ.get("MEM_TRACEMALLOC_FRAMES", "1") or "1"))
    except Exception:
        pass

from pathlib import Path

# =============================================================================
# CRITICAL: Disable Python bytecode cache — set BOTH the runtime flag AND
# the environment variable so every subprocess/worker inherits it too.
# =============================================================================
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

# =============================================================================
# CRITICAL: Delete ALL __pycache__ on every startup.
# Runs each time Streamlit re-executes this script (page load / file change).
# Fast no-op once dont_write_bytecode is in effect (nothing to find).
# =============================================================================
try:
    if os.environ.get("APP_PYCACHE_CLEANED") != "1":
        import time as _time
        _pycache_start = _time.perf_counter()
        import shutil as _shutil
        for _pycache_dir in list(Path(__file__).parent.rglob('__pycache__')):
            _shutil.rmtree(_pycache_dir, ignore_errors=True)
        os.environ["APP_PYCACHE_CLEANED"] = "1"
        os.environ["APP_PYCACHE_CLEANUP_MS"] = f"{(_time.perf_counter() - _pycache_start) * 1000:.2f}"
        del _shutil
        del _time
except Exception:
    pass

import traceback
from datetime import datetime
from time import perf_counter

# =============================================================================
# CRITICAL: Add app to path FIRST
# =============================================================================
_APP_DIR = Path(__file__).parent
sys.path.insert(0, str(_APP_DIR))

# =============================================================================
# Patch Streamlit's static index.html so the grey boot skeleton never shows and
# a branded Coresight loader covers the full-reload window on every page change.
# Must run at process start (before the server hands index.html to the browser).
# Best-effort, idempotent — see core/boot_overlay.py.
# =============================================================================
try:
    from core.boot_overlay import patch_streamlit_index_html
    patch_streamlit_index_html()
except Exception:
    pass

# =============================================================================
# ONLY USE SERVER_LOGGER - NO STANDARD LIBRARY LOGGING
# =============================================================================
try:
    from utils.server_logger import (
        log_error, log_exception, log_timing, new_rerun_id
    )
except Exception as e:
    # Last resort - print to stderr
    print(f"FATAL: Cannot import server_logger: {e}", file=sys.stderr, flush=True)
    raise

if os.environ.get("APP_PYCACHE_CLEANUP_MS"):
    try:
        log_timing("MAIN_PYCACHE_CLEANUP", float(os.environ.pop("APP_PYCACHE_CLEANUP_MS")))
    except Exception:
        pass

# =============================================================================
# STREAMLIT IMPORT & CONFIG
# =============================================================================
try:
    import streamlit as st
except Exception as e:
    print(f"FATAL: Cannot import Streamlit: {e}", file=sys.stderr, flush=True)
    raise

def _maybe_clear_auth_cookies_once() -> None:
    """Optional one-time cookie reset (per-browser) to recover from stale/garbled cookies.

    Enabled via env: AUTH_CLEAR_COOKIES_ONCE=1
    Uses localStorage marker so it runs once per browser profile, not per rerun.
    """
    try:
        if os.getenv("AUTH_CLEAR_COOKIES_ONCE", "0").strip() != "1":
            return

        import streamlit.components.v1 as _components_v1
        from core.auth_manager import COOKIE_NAME, LOGOUT_BRIDGE_COOKIE_NAME, get_current_domain

        domain = get_current_domain()
        cookie_domain = "" if domain == "localhost" else domain

        # JS: if marker absent, clear cookies + set marker, then reload.
        js = f"""
<script>
(function() {{
  try {{
    var k = "__csr_cookie_reset_v1";
    if (window.localStorage && window.localStorage.getItem(k) === "1") {{
      return;
    }}
    function expire(name) {{
      var base = name + "=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax";
      document.cookie = base;
      {"document.cookie = base + '; domain=" + json.dumps(cookie_domain) + "';" if cookie_domain else ""}
    }}
    expire({json.dumps(COOKIE_NAME)});
    expire({json.dumps(LOGOUT_BRIDGE_COOKIE_NAME)});
    if (window.localStorage) window.localStorage.setItem(k, "1");
    window.location.reload();
  }} catch (e) {{}}
}})();
</script>
"""
        _components_v1.html(js, height=0)
        log_timing("MAIN_COOKIE_RESET_ONCE", 0, f"enabled=True domain={cookie_domain!r}")
    except Exception:
        pass

_maybe_clear_auth_cookies_once()


try:
    st.set_page_config(
        page_title="Market Data Portal",
        page_icon="https://coresight.com/wp-content/uploads/2019/03/cropped-CoreSightTransparent_Logo_favico-32x32.png",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
except Exception as e:
    print(f"FATAL: Cannot set page config: {e}", file=sys.stderr, flush=True)
    raise

# =============================================================================
# EARLY: suppress Streamlit's grey skeleton placeholders.
# Injected here (top of every page's run, into the PARENT document) so it is in
# effect before page content streams in — unlike navigation.py's CSS, which
# rides in on a late components.html iframe and so misses the early flash. The
# branded nav overlay + fade-in cover the load; the grey bars only add jank.
# =============================================================================
try:
    st.markdown(
        "<style>"
        "[data-testid='stSkeleton'],[data-testid='stAppSkeleton']{display:none!important;}"
        "</style>",
        unsafe_allow_html=True,
    )
except Exception:
    pass

# =============================================================================
# SSL SETUP
# =============================================================================
try:
    _ssl_start = perf_counter()
    from core.ssl_setup import ensure_ca_cert
    ensure_ca_cert()
    log_timing("MAIN_SSL_SETUP", (perf_counter() - _ssl_start) * 1000)
except Exception as e:
    log_exception("ERROR in SSL setup")

# =============================================================================
# PAGE REGISTRATION - CRITICAL SECTION
# =============================================================================

_PAGES_CONFIG = [
    ("pages/login.py", "Login", "login", True),
    ("pages/logout_bridge.py", "Logout", "logout_bridge", False),
    ("pages/home.py", "Home", "home", False),
    ("pages/market_data.py", "Market Data", "market_data", False),
    ("pages/newsroom.py", "Newsroom", "newsroom", False),
    ("pages/earnings_calls.py", "Earnings Calls", "earnings_calls", False),
    ("pages/live_earnings_transcript.py", "Live Transcript", "live_earnings_transcript", False),
    ("pages/earnings_calendar.py", "Calendar", "earnings_calendar", False),
    ("pages/screening.py", "Screening", "screening", False),
    ("pages/company_filings.py", "Company Filings", "company_filings", False),
    ("pages/company_filings_add_files.py", "Add Files", "company_filings_add_files", False),
    ("pages/logs.py", "Logs", "logs", False),
    ("pages/retailer_adding.py", "Retailers", "retailer_adding", False),
    ("pages/forecasting.py", "Forecasting", "forecasting", False),
    ("pages/access_management.py", "Access Management", "access_management", False),
]

_pages_dir = _APP_DIR / "pages"
_missing_pages = []
for page_file, page_title, url_path, is_default in _PAGES_CONFIG:
    full_path = _APP_DIR / page_file
    exists = full_path.exists()
    if not exists:
        _missing_pages.append((page_title, full_path))
        log_error(f"MISSING page file: {page_file}")

if _missing_pages:
    log_error("FATAL: Missing page files detected!")
    for title, path in _missing_pages:
        log_error(f"  - {title}: {path}")
    raise FileNotFoundError(f"Missing {len(_missing_pages)} page files. Check logs.")

try:
    _page_reg_start = perf_counter()
    _st_pages = []
    for page_file, page_title, url_path, is_default in _PAGES_CONFIG:
        try:
            page = st.Page(
                page_file,
                title=page_title,
                url_path=url_path,
                default=is_default
            )
            _st_pages.append(page)
        except Exception as e:
            log_exception(f"Failed to register page: {page_title}")
            raise

    pg = st.navigation(_st_pages, position="hidden")
    log_timing("MAIN_PAGE_REGISTRATION", (perf_counter() - _page_reg_start) * 1000, f"page_count={len(_st_pages)}")
except Exception as e:
    log_exception("FATAL: Cannot register navigation")
    raise

# =============================================================================
# CENTRAL AUTH BOOTSTRAP
# Hydrate session_state from cookie BEFORE any backend startup work or logout
# routing. This keeps the login page light and makes OIDC logout handoff
# deterministic.
# =============================================================================
try:
    new_rerun_id("main")
    from components.base_page import _patch_st_rerun_once
    _patch_st_rerun_once()
except Exception:
    pass

params = st.query_params

try:
    from core.auth_manager import (
        get_auth_manager as _get_auth_mgr,
        get_auth_flow_id as _get_auth_flow_id,
        log_auth_cookie_server_presence as _log_auth_cookie_server,
        _auth_request_meta as _auth_request_meta,
        COOKIE_NAME as _AUTH_COOKIE_NAME,
    )

    _qp = dict(params)
    _qp_keys = ",".join(sorted(_qp.keys()))
    _login_handoff = _qp.get("login_handoff") == "1"
    _handoff_flow_param = (_qp.get("auth_flow_id") or "").strip()
    if _handoff_flow_param and not _get_auth_flow_id():
        try:
            st.session_state["auth_flow_id"] = _handoff_flow_param
        except Exception:
            pass

    _host_hdr = ""
    _referer_hdr = ""
    _origin_hdr = ""
    try:
        _h = st.context.headers
        _host_hdr = (_h.get("host") or "").split(",", 1)[0].strip()
        _referer_hdr = (_h.get("referer") or "").split(",", 1)[0].strip()
        _origin_hdr = (_h.get("origin") or "").split(",", 1)[0].strip()
    except Exception:
        pass

    _cookie_present_http = False
    _cookie_bytes = 0
    try:
        _raw_cookie = st.context.cookies.get(_AUTH_COOKIE_NAME)
        if _raw_cookie:
            _cookie_present_http = True
            _cookie_bytes = len(_raw_cookie.encode("utf-8")) if isinstance(_raw_cookie, str) else 0
    except Exception:
        pass

    _has_session_state = bool(
        st.session_state.get("authenticated") and st.session_state.get("auth_data")
    )
    _routing_target = ""
    try:
        _routing_target = getattr(pg, "url_path", "") or ""
    except Exception:
        pass

    log_timing(
        "MAIN_AUTH_BOOTSTRAP_ENTRY",
        0,
        f"{_auth_request_meta()} host={_host_hdr!r} referer={_referer_hdr!r} origin={_origin_hdr!r} "
        f"qp_keys=[{_qp_keys}] login_handoff={_login_handoff} "
        f"cookie_http_present={_cookie_present_http} cookie_bytes={_cookie_bytes} "
        f"session_state_auth={_has_session_state} routing_target={_routing_target or '?'}",
        level="WARNING",
    )

    if _login_handoff:
        _browser_ok_param = (_qp.get("browser_cookie_ok") or "").strip()
        if _cookie_present_http:
            _log_auth_cookie_server(page="main", context="login_handoff_success")
            log_timing(
                "AUTH_LOGIN_HANDOFF_SUCCESS",
                0,
                f"{_auth_request_meta()} browser_cookie_ok={_browser_ok_param or '1'}",
                level="WARNING",
            )
            # Clean the handoff params from the URL CLIENT-SIDE (history.replaceState)
            # instead of mutating st.query_params. Mutating st.query_params schedules
            # a full app RERUN, which re-rendered the landing page a second time during
            # sign-in ("Loading Home" twice). replaceState strips the params with no
            # rerun; the next natural rerun reads the already-clean URL, so the handoff
            # is not re-processed. Same end state, one fewer home render.
            try:
                import streamlit.components.v1 as _handoff_comp
                _handoff_comp.html(
                    "<script>try{var w=window.parent,u=new URL(w.location.href);"
                    "['login_handoff','auth_flow_id','browser_cookie_ok']"
                    ".forEach(function(k){u.searchParams.delete(k);});"
                    "w.history.replaceState({},'',u.pathname+(u.search||'')+(u.hash||''));"
                    "}catch(e){}</script>",
                    height=0,
                )
            except Exception:
                pass
        else:
            _log_auth_cookie_server(page="main", context="login_handoff_failed")
            log_timing(
                "AUTH_LOGIN_HANDOFF_FAILED",
                0,
                f"{_auth_request_meta()} browser_cookie_ok={_browser_ok_param or '0'}",
                level="WARNING",
            )
            for _sk in ("auth_data", "authenticated"):
                if _sk in st.session_state:
                    del st.session_state[_sk]
            st.session_state["_auth_login_error"] = (
                "Sign-in cookie could not be saved. Please try again."
            )
            st.switch_page("pages/login.py")
            st.stop()

    _auth_boot_start = perf_counter()
    _get_auth_mgr().is_authenticated()  # side-effect: populates session_state if cookie present
    log_timing(
        "MAIN_AUTH_BOOTSTRAP",
        (perf_counter() - _auth_boot_start) * 1000,
        f"{_auth_request_meta()} authenticated={bool(st.session_state.get('authenticated'))} "
        f"routing_target={_routing_target or '?'}",
        level="WARNING",
    )
except Exception as _e:
    pass

# =============================================================================
# AUTHENTICATION & LOGOUT HANDLING
# =============================================================================
if params.get("action") == "logout":
    try:
        _logout_route_start = perf_counter()
        from core.auth_environment import is_oidc_enabled

        if is_oidc_enabled():
            from core.auth_manager import stash_logout_context as _stash_logout_context

            _bridge_ready = _stash_logout_context(st.session_state.get("auth_data"))
            log_timing(
                "MAIN_LOGOUT_ROUTE",
                (perf_counter() - _logout_route_start) * 1000,
                f"oidc=True bridge_ready={_bridge_ready} authenticated={bool(st.session_state.get('authenticated'))}",
            )
            st.switch_page("pages/logout_bridge.py")
            st.stop()
        else:
            for _k in ["auth_data", "authenticated"]:
                if _k in st.session_state:
                    del st.session_state[_k]
            st.session_state["_auth_invalidated"] = True
            log_timing(
                "MAIN_LOGOUT_ROUTE",
                (perf_counter() - _logout_route_start) * 1000,
                "oidc=False bridge_ready=False",
            )
            st.switch_page("pages/login.py")
            st.stop()
    except Exception as e:
        log_exception("ERROR during logout routing")
        raise

_auth_ready_for_bg = bool(st.session_state.get("authenticated") and st.session_state.get("auth_data"))

# =============================================================================
# BACKGROUND DB WARMUP (unauthenticated-only, one-shot per process)
# Keeps the login page fast while pre-connecting before the OIDC callback lands.
# =============================================================================
if not _auth_ready_for_bg and os.environ.get("APP_DB_WARMUP_STARTED") != "1":
    try:
        import threading as _db_threading

        def _warm_db_for_login():
            _warm_start = perf_counter()
            try:
                from core.database import init_database as _init_database
                _init_database()
                log_timing("MAIN_DB_WARMUP_BG", (perf_counter() - _warm_start) * 1000, "status=ok")
            except Exception:
                log_exception("ERROR in background DB warmup")

        os.environ["APP_DB_WARMUP_STARTED"] = "1"
        _db_thread_start = perf_counter()
        _db_threading.Thread(target=_warm_db_for_login, daemon=True).start()
        log_timing("MAIN_DB_WARMUP_THREAD", (perf_counter() - _db_thread_start) * 1000)
        del _db_threading
    except Exception:
        pass

# =============================================================================
# DATABASE INITIALIZATION + WARMUPS (authenticated-only)
# Avoids paying Azure DB setup on unauthenticated login and logout requests.
# Login callback still works because auth_manager/login_user lazily opens DB.
# =============================================================================
if _auth_ready_for_bg:
    try:
        _db_init_start = perf_counter()
        from core.database import init_database
        init_database()
        log_timing("MAIN_DB_INIT", (perf_counter() - _db_init_start) * 1000)
    except Exception as e:
        log_exception("ERROR in database initialization")

    try:
        if os.environ.get("APP_SCREENING_PREWARM_STARTED") == "1":
            raise RuntimeError("screening prewarm already started")
        import threading as _threading

        def _prewarm_screening_tables():
            try:
                from data.watchlist_service import ensure_tables as _wl_ensure
                from data.saved_criteria_service import ensure_tables as _sc_ensure
                from data.portal_users_service import ensure_tables as _pu_ensure
                from data.earnings_alert_service import ensure_tables as _ea_ensure
                _wl_ensure()
                _sc_ensure()
                _pu_ensure()
                _ea_ensure()
            except Exception:
                log_exception("ERROR in background screening-tables prewarm")

        os.environ["APP_SCREENING_PREWARM_STARTED"] = "1"
        _prewarm_start = perf_counter()
        _threading.Thread(target=_prewarm_screening_tables, daemon=True).start()
        log_timing("MAIN_SCREENING_PREWARM_THREAD", (perf_counter() - _prewarm_start) * 1000)
        del _threading
    except RuntimeError:
        pass
    except Exception:
        pass

    try:
        if os.environ.get("APP_AV_FT_WARMED") == "1":
            raise RuntimeError("av ft already warmed")
        _ft_start = perf_counter()
        from core.database import warmup_av_fulltext
        warmup_av_fulltext()
        os.environ["APP_AV_FT_WARMED"] = "1"
        log_timing("MAIN_AV_FT_WARMUP", (perf_counter() - _ft_start) * 1000)
    except RuntimeError:
        pass
    except Exception:
        pass

    if os.getenv("ENABLE_EC_WARMUP", "1").strip() != "0" and os.environ.get("APP_EC_WARMED") != "1":
        try:
            _ec_warm_start = perf_counter()
            from data.repository import warmup_ec_caches
            warmup_ec_caches()
            os.environ["APP_EC_WARMED"] = "1"
            log_timing("MAIN_EC_WARMUP", (perf_counter() - _ec_warm_start) * 1000)
        except Exception:
            pass

    # =========================================================================
    # COMPANY-FILINGS PREFETCH WARMUP (authenticated-only, BACKGROUND)
    # The /company_filings filter prefetch runs a DISTINCT+filesort over every
    # metric row of a ticker on coreiq_filing_metrics_v5 (~75k rows for a big
    # filer like AMZN). On a COLD buffer pool (right after a process restart)
    # that first query is ~11s → a 12s "CRITICAL" page load; once the pages are
    # in MySQL's buffer pool it drops to ~0.5s. Warm the shared ticker index +
    # the common heavy filers' pages in the BACKGROUND (never blocks the first
    # render — unlike the FT/EC warmups above) so the first real filings load is
    # already warm. Same query as company_filings._prefetch_ticker_filter_data.
    # =========================================================================
    if (os.getenv("ENABLE_FILINGS_WARMUP", "1").strip() != "0"
            and os.environ.get("APP_FILINGS_PREFETCH_WARMED") != "1"):
        try:
            import threading as _fm_threading

            def _warm_filings_prefetch():
                _w_start = perf_counter()
                _ok = 0
                try:
                    from core.database import db_manager as _fm_db
                    # Warm only the single largest filer by default. Its DISTINCT
                    # scan pulls the shared coreiq_filing_metrics_v5 index into
                    # MySQL's buffer pool, which is what makes EVERY ticker's
                    # later prefetch fast — so warming 8 filers serially (~183s)
                    # just saturated the 5-conn read pool while real users waited.
                    # IT can override via the FILINGS_WARMUP_TICKERS App Setting.
                    _tickers = [t.strip().upper() for t in os.getenv(
                        "FILINGS_WARMUP_TICKERS",
                        "AMZN",
                    ).split(",") if t.strip()]
                    _q = (
                        "SELECT DISTINCT doc_type, "
                        "COALESCE(storage_year, report_fiscal_year, fiscal_year) AS bucket_year, "
                        "COALESCE(fiscal_year, storage_year, report_fiscal_year) AS display_year "
                        "FROM coreiq_filing_metrics_v5 WHERE ticker = :ticker "
                        "ORDER BY doc_type, bucket_year DESC"
                    )
                    for _tk in _tickers:
                        try:
                            _fm_db.execute_query_readonly(_q, {"ticker": _tk})
                            _ok += 1
                        except Exception:
                            pass
                    log_timing(
                        "MAIN_FILINGS_PREFETCH_WARMUP",
                        (perf_counter() - _w_start) * 1000,
                        f"warmed={_ok}/{len(_tickers)}",
                    )
                except Exception:
                    pass

            os.environ["APP_FILINGS_PREFETCH_WARMED"] = "1"
            _fm_thr_start = perf_counter()
            _fm_threading.Thread(target=_warm_filings_prefetch, daemon=True).start()
            log_timing("MAIN_FILINGS_PREFETCH_WARMUP_THREAD", (perf_counter() - _fm_thr_start) * 1000)
            del _fm_threading
        except Exception:
            pass

# =============================================================================
# BACKGROUND CACHE WARMUP (authenticated-only)
# =============================================================================
_ENABLE_BG_WARMUP = os.getenv("ENABLE_BG_WARMUP", "1").strip() != "0"
if _ENABLE_BG_WARMUP and _auth_ready_for_bg and os.environ.get("APP_BG_WARMUP_STARTED") != "1":
    try:
        _warmup_start = perf_counter()
        from utils.cache_manager import start_background_warmup
        start_background_warmup()
        os.environ["APP_BG_WARMUP_STARTED"] = "1"
        log_timing("MAIN_BG_WARMUP_INIT", (perf_counter() - _warmup_start) * 1000)
    except Exception:
        pass

# =============================================================================
# FILING CACHE PURGE + BACKGROUND SCANNER (authenticated-only)
# =============================================================================
_ENABLE_BG_SCANNER = os.getenv("ENABLE_BG_SCANNER", "1").strip() != "0"
if _ENABLE_BG_SCANNER and _auth_ready_for_bg and os.environ.get("APP_BG_SCANNER_STARTED") != "1":
    try:
        import threading as _scanner_threading

        # Run OFF the render thread. ensure_cache_purge()/init_background_scanner()
        # call _audit_file_cache() → an os.walk() of the filings blob cache, which
        # on STG lives on the /home Azure SMB share and PERSISTS across restarts
        # (~44k files). That walk took 52s SYNCHRONOUSLY here (STG log 17-Jul:
        # MAIN_BG_SCANNER_INIT=52393ms) → the first authenticated page load after
        # every restart was blank for ~52s. Every other warmup in this file is
        # already threaded; this one wasn't. The env flag is set BEFORE the thread
        # so a second concurrent request never spawns a duplicate.
        def _init_bg_scanner():
            _scanner_start = perf_counter()
            try:
                from utils.background_scanner import ensure_cache_purge, init_background_scanner
                ensure_cache_purge()
                init_background_scanner(auto_start=True)
                log_timing("MAIN_BG_SCANNER_INIT", (perf_counter() - _scanner_start) * 1000)
            except Exception:
                log_exception("ERROR in background scanner init")

        os.environ["APP_BG_SCANNER_STARTED"] = "1"
        _scanner_threading.Thread(target=_init_bg_scanner, daemon=True, name="bg-scanner-init").start()
        del _scanner_threading
    except Exception:
        pass

# =============================================================================
# NON-SEC FILING CACHE WARM-UP (authenticated-only)
# =============================================================================
if _auth_ready_for_bg and os.environ.get("APP_NON_SEC_WARMUP_STARTED") != "1":
    try:
        import threading as _nonsec_threading

        def _warm_non_sec_cache():
            try:
                from utils.non_sec_blob_fallback import (
                    get_non_sec_tickers_from_db,
                    warm_non_sec_cache_at_startup,
                )
                tickers_with_names = get_non_sec_tickers_from_db()
                if tickers_with_names:
                    tickers = [t for t, _ in tickers_with_names]
                    warm_non_sec_cache_at_startup(tickers, max_workers=32)
            except Exception:
                log_exception("ERROR in background non-SEC cache warmup")

        _nonsec_start = perf_counter()
        os.environ["APP_NON_SEC_WARMUP_STARTED"] = "1"
        _nonsec_threading.Thread(target=_warm_non_sec_cache, daemon=True).start()
        log_timing("MAIN_NON_SEC_WARMUP_THREAD", (perf_counter() - _nonsec_start) * 1000)
        del _nonsec_threading
    except Exception:
        pass

# =============================================================================
# EARNINGS ALERT DISPATCH (authenticated — MySQL coreiq_earnings_alert_* + SMTP)
# =============================================================================
if _auth_ready_for_bg and os.getenv("EARNINGS_ALERT_DISPATCH", "1").strip() != "0":
    try:
        import threading as _ea_tick_threading

        from utils.earnings_alert_dispatcher import run_earnings_alert_dispatch_tick

        # Run OFF the render thread (daemon). This tick loops enabled prefs ×
        # lookback days calling get_calendar_events(); on a cold DB buffer pool the
        # first calendar query (EC_QUERY_FYE_MAP) took ~10.4s and this ran
        # SYNCHRONOUSLY in the main bootstrap BEFORE pg.run() — so the home page's
        # first paint waited the full ~10.4s (STG log 16-Jul). The 120s min-interval
        # guard inside the tick keeps the per-request kick cheap; the bg dispatch
        # thread below still owns the steady-state loop.
        _ea_tick_threading.Thread(
            target=run_earnings_alert_dispatch_tick, daemon=True
        ).start()
        del _ea_tick_threading
    except Exception:
        pass

    if os.environ.get("APP_EARNINGS_ALERT_DISPATCH_THREAD_STARTED") != "1":
        if os.getenv("EARNINGS_ALERT_DISPATCH_THREAD", "1").strip() != "0":
            try:
                import threading as _earnings_alert_threading

                from utils.earnings_alert_dispatcher import start_background_dispatch_thread

                os.environ["APP_EARNINGS_ALERT_DISPATCH_THREAD_STARTED"] = "1"
                start_background_dispatch_thread()
                del _earnings_alert_threading
            except Exception:
                pass

# =============================================================================
# FORECAST AUTO-REFRESH (twice-daily, reporting-date driven; ON by default).
# One daemon thread; never blocks page render. Exactly-once across instances via
# an atomic DB claim. Disable with ENABLE_FORECAST_AUTO_REFRESH=0.
# Spec: docs/superpowers/specs/2026-07-07-forecast-auto-refresh-design.md
# =============================================================================
if (_auth_ready_for_bg
        and os.getenv("ENABLE_FORECAST_AUTO_REFRESH", "1").strip() not in ("0", "", "false", "False")
        and os.environ.get("APP_FORECAST_AUTO_REFRESH_STARTED") != "1"):
    try:
        from utils.forecast_auto_refresh import start_forecast_auto_refresh

        os.environ["APP_FORECAST_AUTO_REFRESH_STARTED"] = "1"
        start_forecast_auto_refresh()
    except Exception:
        pass

# =============================================================================
# M&A OVERLAY AUTO-ENRICH (6-hourly; ON by default). Fixes acquirer/target for
# M&A rows the nightly ETL inserts with regex garbage, via edgartools in a
# daemon thread — no DB writes, never blocks page render. Disable with
# ENABLE_MA_OVERRIDES_AUTO=0.
# Spec: docs/superpowers/specs/2026-07-15-calendar-ma-acquirer-target-repair-design.md
# =============================================================================
if (_auth_ready_for_bg
        and os.getenv("ENABLE_MA_OVERRIDES_AUTO", "1").strip() not in ("0", "", "false", "False")
        and os.environ.get("APP_MA_OVERRIDES_AUTO_STARTED") != "1"):
    try:
        from utils.ma_overrides_auto import start_ma_overrides_auto

        os.environ["APP_MA_OVERRIDES_AUTO_STARTED"] = "1"
        start_ma_overrides_auto()
    except Exception:
        pass

# =============================================================================
# SEGMENT-CACHE AUTO-REFRESH (staleness-driven; ON by default). One daemon
# thread rebuilds the screening segment values/member caches only when the
# source coreiq_filing_metrics_v5 grew (cheap MAX(id) gate — the lazy trigger
# only fired on an empty table, so on a persistent DB table it never refreshed
# and drifted 24 days stale). Exactly-once across instances via an atomic DB
# claim; RAM-safe (~10MB build). Disable with ENABLE_SEGMENT_CACHE_AUTO_REFRESH=0.
# Spec: docs/superpowers/specs/2026-07-18-log-investigation-and-segment-cache-automation-design.md
# =============================================================================
if (_auth_ready_for_bg
        and os.getenv("ENABLE_SEGMENT_CACHE_AUTO_REFRESH", "1").strip() not in ("0", "", "false", "False")
        and os.environ.get("APP_SEG_CACHE_REFRESH_STARTED") != "1"):
    try:
        from utils.segment_cache_auto_refresh import start_segment_cache_auto_refresh

        os.environ["APP_SEG_CACHE_REFRESH_STARTED"] = "1"
        start_segment_cache_auto_refresh()
    except Exception:
        pass

# =============================================================================
# STARTUP COMPLETE - RUN APP
# =============================================================================
_page_obs_start = None
_page_obs_key = ""
try:
    from components.base_page import bootstrap_page_observability, finish_page_observability
    try:
        _page_obs_key = getattr(pg, "url_path", "") or "main"
    except Exception:
        _page_obs_key = "main"
    _page_obs_start = bootstrap_page_observability(_page_obs_key)
except Exception:
    pass

try:
    # Per-run queue instrumentation for EVERY page (market_data has its own
    # MD_RUN_START). Streamlit runs a session's scripts sequentially: a click
    # during a run waits for it to finish, then starts a fresh run. A gap
    # <100ms since the previous run ended means this run served an interaction
    # that sat QUEUED — the "app is frozen" the user feels, invisible in
    # per-phase timings. Also stamps process uptime so cold-start runs (the
    # first ~30s after a deploy) are identifiable in the log.
    _run_now = perf_counter()
    _prev_end = st.session_state.get("_app_prev_run_end")
    _gap_ms = (_run_now - _prev_end) * 1000 if _prev_end else -1.0
    log_timing(
        "APP_RUN_START", 0,
        f"page={_page_obs_key or '?'} gap_since_prev_run_end_ms={_gap_ms:.0f}"
        f"{' interaction_QUEUED_behind_previous_run' if 0 <= _gap_ms < 100 else ''}",
        level="WARNING",
    )
    pg.run()
    st.session_state["_app_prev_run_end"] = perf_counter()
    try:
        from core.perf_panel import render_perf_panel_if_requested
        render_perf_panel_if_requested()  # ?perf=1 → browser-side breakdown
    except Exception:
        pass
except Exception as e:
    log_exception("FATAL: Error running page")
    raise
finally:
    try:
        if _page_obs_start is not None and _page_obs_key:
            finish_page_observability(_page_obs_key, _page_obs_start)
    except Exception:
        pass
