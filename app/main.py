# app/main.py
"""
Coresight Research Portal - Main Entry Point (STG VERSION)
Uses ONLY server_logger - no standard library logging
"""

import sys
import os
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
    ("pages/earnings_calendar.py", "Earnings Calendar", "earnings_calendar", False),
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
            try:
                for _hk in ("login_handoff", "auth_flow_id", "browser_cookie_ok"):
                    if _hk in st.query_params:
                        del st.query_params[_hk]
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
                pass

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
        _scanner_start = perf_counter()
        from utils.background_scanner import ensure_cache_purge, init_background_scanner
        ensure_cache_purge()
        init_background_scanner(auto_start=True)
        os.environ["APP_BG_SCANNER_STARTED"] = "1"
        log_timing("MAIN_BG_SCANNER_INIT", (perf_counter() - _scanner_start) * 1000)
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
                pass

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
        from utils.earnings_alert_dispatcher import run_earnings_alert_dispatch_tick

        run_earnings_alert_dispatch_tick()
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
# STARTUP COMPLETE - RUN APP
# =============================================================================
try:
    pg.run()
except Exception as e:
    log_exception("FATAL: Error running page")
    raise
