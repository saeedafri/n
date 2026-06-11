"""
Navigation components for the application.
"""
import base64
import os
import re
import streamlit as st
from typing import List, Dict, Callable, Optional
from enum import Enum
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from components.styles import COLORS, TYPOGRAPHY, SPACING, BORDER_RADIUS
import streamlit as st

# Import auth functions
from core.auth_manager import (
    is_authenticated,
    get_current_user,
    logout,
    get_auth_manager,
    _auth_request_meta,
)
from data.forecast_admin_service import is_forecast_admin
from utils.server_logger import log_error, log_exception, log_structured_error, log_critical, log_timing

# Lazy import to avoid circular dependency — called only inside render_coresight_footer
def _can_access_mgmt(user_email: str) -> bool:
    """True for admin or super_user roles. Falls back to hardcoded list if DB is down."""
    _FALLBACK = {
        "mohdsaeedafri@coresight.com",
        "philipmoore@coresight.com",
        "shashankgupta@coresight.com",
    }
    if not user_email:
        return False
    try:
        from core.access_control import UserRolesManager
        return UserRolesManager.is_admin_or_super_user(user_email)
    except Exception:
        return user_email.strip().lower() in {e.lower() for e in _FALLBACK}
import streamlit.components.v1 as _st_components


def _footer_social_icon_data_uri(viewbox: str, path_d: str) -> str:
    """White glyph on transparent; use as <img src> because st.html() strips nested <svg>."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">'
        f'<path fill="#ffffff" d="{path_d}"/></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


_FOOTER_SOCIAL_FB = _footer_social_icon_data_uri(
    "0 0 320 512",
    "M279.14 288l14.22-92.66h-88.91v-60.13c0-25.35 12.42-50.06 52.24-50.06h40.42V6.26S260.43 0 225.36 0c-73.22 0-121.08 44.38-121.08 124.72v70.62H22.89V288h81.39v224h100.17V288z",
)
_FOOTER_SOCIAL_TW = _footer_social_icon_data_uri(
    "0 0 512 512",
    "M459.37 151.716c.325 4.548.325 9.097.325 13.645 0 138.72-105.583 298.558-298.558 298.558-59.452 0-114.68-17.219-161.137-47.106 8.447.974 16.568 1.299 25.34 1.299 49.055 0 94.213-16.568 130.274-44.832-46.132-.975-84.792-31.188-98.112-72.772 6.498.974 12.995 1.624 19.818 1.624 9.421 0 18.843-1.3 27.614-3.573-48.081-9.747-84.143-51.98-84.143-102.985v-1.299c13.969 7.797 30.214 12.67 47.431 13.319-28.264-18.843-46.781-51.005-46.781-87.391 0-19.492 5.197-37.36 14.294-52.954 51.655 63.675 129.3 105.258 216.365 109.807-1.624-7.797-2.599-15.918-2.599-24.04 0-57.828 46.782-104.934 104.934-104.934 30.213 0 57.502 12.67 76.67 33.137 23.715-4.548 46.456-13.32 66.599-25.34-7.798 24.366-24.366 44.833-46.132 57.827 21.117-2.273 41.584-8.122 60.426-16.243-14.292 20.791-32.161 39.308-52.628 54.253z",
)
_FOOTER_SOCIAL_WC = _footer_social_icon_data_uri(
    "0 0 576 512",
    "M385.2 167.6c6.4 0 12.6.3 18.8 1.1C387.4 90.3 303.3 32 207.7 32 100.5 32 13 104.8 13 197.4c0 53.4 29.3 97.5 77.9 131.6l-19.3 58.6 68-34.1c24.4 4.8 43.8 9.7 68.2 9.7 6.2 0 12.1-.3 18.3-.8-4-12.9-6.2-26.6-6.2-40.8-.1-84.9 72.9-154 165.3-154zm-104.5-52.9c14.5 0 24.2 9.7 24.2 24.4 0 14.5-9.7 24.2-24.2 24.2-14.8 0-29.3-9.7-29.3-24.2.1-14.7 14.6-24.4 29.3-24.4zm-136.4 48.6c-14.5 0-29.3-9.7-29.3-24.2 0-14.8 14.8-24.4 29.3-24.4 14.8 0 24.4 9.7 24.4 24.4 0 14.6-9.6 24.2-24.4 24.2zM563 319.4c0-77.9-77.9-141.3-165.4-141.3-92.7 0-165.4 63.4-165.4 141.3S305 460.7 397.6 460.7c19.3 0 38.9-5.1 58.6-9.9l53.4 29.3-14.8-48.6C534 402.1 563 363.2 563 319.4zm-219.1-24.5c-9.7 0-19.3-9.7-19.3-19.6 0-9.7 9.7-19.3 19.3-19.3 14.8 0 24.4 9.7 24.4 19.3 0 10-9.7 19.6-24.4 19.6zm107.1 0c-9.7 0-19.3-9.7-19.3-19.6 0-9.7 9.7-19.3 19.3-19.3 14.5 0 24.4 9.7 24.4 19.3.1 10-9.9 19.6-24.4 19.6z",
)
_FOOTER_SOCIAL_LI = _footer_social_icon_data_uri(
    "0 0 448 512",
    "M100.28 448H7.4V148.9h92.88zM53.79 108.1C24.09 108.1 0 83.5 0 53.8a53.79 53.79 0 0 1 107.58 0c0 29.7-24.1 54.3-53.79 54.3zM447.9 448h-92.68V302.4c0-34.7-.7-79.2-48.29-79.2-48.29 0-55.69 37.7-55.69 76.7V448h-92.78V148.9h89.08v40.8h1.3c12.4-23.5 42.69-48.3 87.88-48.3 94 0 111.28 61.9 111.28 142.3V448z",
)


def _inject_transition_js() -> None:
    """
    Branded page-transition overlay for the Coresight portal.

    How it works
    ────────────
    Runs inside a components.v1.html iframe that can reach window.parent.
    st.html() renders nav links DIRECTLY in the parent document (not iframe),
    so our JS can find and intercept them via window.parent.document.

    Three-layer defence against blank/white screens:

    Layer 1 — Click interceptor (primary, fires BEFORE navigation):
      Attaches to all .coresight-header-nav anchors and .logout-btn in the
      parent document.  On click: preventDefault → show overlay → 65ms later
      navigate.  The 65ms gap lets the browser paint the overlay before the
      page unloads.  Result: user sees branded overlay WITH logo+spinner
      instead of any blank.

    Layer 2 — Per-render content check (fires on every page LOAD):
      render_header() is always the first Streamlit element on every page.
      After rendering, _inject_transition_js() checks: does stMain have any
      real content yet?  If not, show overlay immediately.  MutationObserver
      auto-hides it the moment content arrives.  Covers the new-page load
      period after navigation completes.

    Layer 3 — URL-change poller (60 ms, secondary fallback):
      Catches any pushState navigation that Layer 1 missed.
      Skips /login to ensure the login form is never covered.

    Critical constraints
    ────────────────────
    • Overlay is ALWAYS pointer-events:none — NEVER blocks user input.
    • index.html has body{background:#f2f2f2} to kill the 0 ms white flash.
    • 8-second safety timeout prevents overlay from getting stuck.
    • All DOM setup is idempotent (guarded by window.parent.__csTI flag).
    """
    script_html = """<script>
(function(){
  var doc = window.parent.document;
  var win = window.parent;

  /* ── Immediate: body stays grey even if CSS hasn't applied yet ────────── */
  if (doc.body) {
    doc.body.style.background = '#f2f2f2';
    doc.documentElement.style.background = '#f2f2f2';
  }

  /* ═══════════════════════════════════════════════════════════════════════
     ONE-TIME SETUP — persists across Streamlit page changes.
     (Streamlit uses full page reload for navigation, so this runs fresh
      each load.  The guard prevents double-init within the same load.)
     ═══════════════════════════════════════════════════════════════════════ */
  if (!win.__csTI) {
    win.__csTI = true;

    /* ── CSS ─────────────────────────────────────────────────────────────── */
    var el = doc.createElement('style');
    el.id  = 'cs-styles';
    el.textContent =
      /* Overlay — pointer-events:none means it NEVER blocks clicks */
      '#cs-ov{position:fixed;inset:0;z-index:99998;background:#f2f2f2;' +
        'display:flex;align-items:center;justify-content:center;' +
        'opacity:0;pointer-events:none;transition:opacity 0.2s ease;}' +
      '#cs-ov.on{opacity:1;}' +
      /* White card with logo, spinner, shimmer */
      '.cs-c{display:flex;flex-direction:column;align-items:center;gap:18px;' +
        'padding:32px 44px;background:#fff;border-radius:14px;' +
        'box-shadow:0 6px 40px rgba(0,0,0,0.10);}' +
      '.cs-c img{width:144px;height:auto;display:block;}' +
      '.cs-r{width:32px;height:32px;border-radius:50%;' +
        'border:3px solid rgba(214,46,47,0.12);border-top-color:#d62e2f;' +
        'animation:cs-sp 0.7s linear infinite;}' +
      '@keyframes cs-sp{to{transform:rotate(360deg)}}' +
      '.cs-s{width:108px;height:2px;border-radius:1px;' +
        'background:linear-gradient(90deg,#ebebeb 25%,#d62e2f 50%,#ebebeb 75%);' +
        'background-size:200% 100%;animation:cs-sh 1.6s ease infinite;}' +
      '@keyframes cs-sh{0%{background-position:200% 0}100%{background-position:-200% 0}}' +
      /* YouTube-style red progress bar at top of viewport */
      '#cs-pb{position:fixed;top:0;left:0;height:3px;z-index:99999;' +
        'width:0%;opacity:0;pointer-events:none;' +
        'background:linear-gradient(90deg,#d62e2f,#ff7575);}' +
      /* Page content fade-in animation (new page appears smoothly) */
      '.main .block-container{animation:cs-fi 0.32s ease both;}' +
      '@keyframes cs-fi{from{opacity:0;transform:translateY(6px)}' +
                        'to{opacity:1;transform:none}}';
    doc.head.appendChild(el);

    /* ── Overlay + progress bar DOM elements ─────────────────────────────── */
    var ov = doc.createElement('div'); ov.id = 'cs-ov';
    ov.innerHTML =
      '<div class="cs-c">' +
        '<img src="https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net' +
             '/wp-content/uploads/2023/12/coresight-logo-1.png" alt="Coresight">' +
        '<div class="cs-r"></div>' +
        '<div class="cs-s"></div>' +
      '</div>';
    doc.body.appendChild(ov);

    var pb = doc.createElement('div'); pb.id = 'cs-pb';
    doc.body.appendChild(pb);

    var _st = null, _bt = null;   /* safety timer, bar timer */

    /* Show the overlay + start progress bar sweep */
    win.__csShow = function() {
      if (ov.classList.contains('on')) return;
      ov.classList.add('on');
      pb.style.cssText = 'position:fixed;top:0;left:0;height:3px;z-index:99999;' +
        'pointer-events:none;background:linear-gradient(90deg,#d62e2f,#ff7575);' +
        'width:0%;opacity:1;transition:none;';
      clearTimeout(_bt);
      _bt = setTimeout(function(){
        pb.style.transition = 'width 3s cubic-bezier(0.05,0.5,0.9,1)';
        pb.style.width = '78%';
      }, 35);
      clearTimeout(_st);
      _st = setTimeout(function(){ win.__csHide(); }, 8000); /* safety */
    };

    /* Hide overlay + complete progress bar */
    win.__csHide = function() {
      if (!ov.classList.contains('on')) return;
      clearTimeout(_st); clearTimeout(_bt);
      ov.classList.remove('on');
      pb.style.transition = 'width 0.14s ease';
      pb.style.width = '100%';
      setTimeout(function(){
        pb.style.transition = 'opacity 0.22s ease';
        pb.style.opacity = '0';
        setTimeout(function(){
          pb.style.cssText = 'position:fixed;top:0;left:0;height:3px;z-index:99999;' +
            'pointer-events:none;background:linear-gradient(90deg,#d62e2f,#ff7575);' +
            'width:0%;opacity:0;transition:none;';
        }, 260);
      }, 150);
    };

    /* Selectors that mean "page has real content" */
    var READY =
      '[data-testid="stMarkdownContainer"],[data-testid="stDataFrame"],' +
      '[data-testid="stSelectbox"],[data-testid="stTable"],[data-testid="stMetric"],' +
      '[data-testid="stTabs"],[data-testid="stImage"],[data-testid="stForm"],' +
      '[data-testid="stTextInput"],[data-testid="stMultiSelect"],' +
      '[data-testid="stSpinner"],[data-testid="element-container"]';

    /* MutationObserver: auto-hide overlay when content appears in stMain */
    var mo = new MutationObserver(function(){
      if (!ov.classList.contains('on')) return;
      var main = doc.querySelector('[data-testid="stMain"]');
      if (main && main.querySelector(READY)) {
        setTimeout(function(){ win.__csHide(); }, 55);
      }
    });
    mo.observe(doc.querySelector('[data-testid="stAppViewContainer"]') || doc.body,
               {childList:true, subtree:true});

    /* ── Layer 3: URL-change poller (fallback for any pushState nav) ───────
       IMPORTANT: Streamlit updates ONLY the query string when filters sync
       (e.g. period_type=Quarterly). That must NOT show the full-page transition
       overlay — it looks like an infinite loop / stuck load. Only react when
       the path (multipage route) or hash changes. */
    var _lhPath = win.location.pathname + win.location.hash;
    setInterval(function(){
      var curPath = win.location.pathname + win.location.hash;
      if (curPath === _lhPath) return;
      _lhPath = curPath;
      if (win.location.href.indexOf('/login') !== -1) return; /* never overlay login */
      win.__csShow();
    }, 60);
  }

  /* ═══════════════════════════════════════════════════════════════════════
     LAYER 1 — Click interceptors (runs EVERY render so they survive Streamlit
     re-runs that regenerate the nav DOM).

     st.html() renders directly in the parent document (NOT in a sandboxed
     iframe), so window.parent.document.querySelectorAll finds these links.

     Mechanism:
       1. preventDefault stops the browser from navigating immediately.
       2. overlay shows (browser paint happens synchronously in next frame).
       3. After 65 ms the browser has painted the overlay → navigate.
     ═══════════════════════════════════════════════════════════════════════ */
  (function attachClicks(){
    function intercept(el) {
      if (el.__csI) return;   /* already intercepted this element */
      el.__csI = true;
      el.addEventListener('click', function() {
        /* Skip active page — no transition needed */
        if (el.classList && el.classList.contains('active')) return;
        /* Just show overlay — do NOT preventDefault.
           The iframe sandbox blocks win.location.href, so let the
           browser handle the native <a> navigation while we show
           the loading overlay. */
        win.__csShow && win.__csShow();
      });
    }

    /* Nav links */
    doc.querySelectorAll('.coresight-header-nav a').forEach(function(a){
      intercept(a);
    });
    /* Logout button */
    var lo = doc.querySelector('.logout-btn');
    if (lo) intercept(lo);
  })();

  /* ═══════════════════════════════════════════════════════════════════════
     LAYER 2 — Per-render content check.
     After render_header() renders (every page), if stMain has no content
     yet, show overlay immediately.  Covers the loading period after nav.
     ═══════════════════════════════════════════════════════════════════════ */
  (function perRenderCheck(){
    if (win.location.href.indexOf('/login') !== -1) return;
    var o2 = doc.getElementById('cs-ov');
    if (!o2 || o2.classList.contains('on')) return;
    var main = doc.querySelector('[data-testid="stMain"]');
    var R2 =
      '[data-testid="stMarkdownContainer"],[data-testid="stDataFrame"],' +
      '[data-testid="stSelectbox"],[data-testid="stTabs"],[data-testid="stForm"],' +
      '[data-testid="stTextInput"],[data-testid="stSpinner"],[data-testid="element-container"]';
    if (!main || !main.querySelector(R2)) {
      win.__csShow && win.__csShow();
    }
  })();

  /* ── Collapse injector iframe to zero layout space ───────────────────── */
  var f = window.frameElement;
  if (f) {
    f.style.cssText =
      'height:0!important;max-height:0!important;min-height:0!important;' +
      'border:none!important;display:block!important;overflow:hidden!important;' +
      'margin:0!important;padding:0!important;visibility:hidden!important;';
  }
})();
</script>"""
    try:
        _st_components.html(script_html, height=0)
    except Exception:
        pass


def render_header(full_width: bool = True, current_page: str = "market_data",ticker: str = "M"):
    """
    Render Coresight header based on Figma design - EXACT MATCH.

    Figma Reference: Header (Node ID: 20895:206587)
    - Container: 1440x80px, background #f2f2f2
    - Logo: 132x60px at x=390 (106px from left edge)
    - Nav: Frame at x=568 with 48px gaps between items
    - Nav items: Roboto 18px weight 500
    - Nav text color: #2d2a29

    Parameters:
    -----------
    full_width : bool
        If True, header takes full width
    current_page : str
        Current page identifier for active nav highlighting
    """
    # STEP 1: Page type detection
    is_market_data = current_page == "market_data"
    is_newsroom = current_page == "newsroom"
    is_company_profile = current_page == "company_profile"
    is_earnings_calls = current_page == "earnings_calls"
    is_earnings_calendar = current_page == "earnings_calendar"
    is_estimates = current_page in ("estimates", "forecasting")
    is_screening = current_page == "screening"

    # STEP 2: Ticker resolution (session > query param > argument)
    session_ticker = st.session_state.get("active_ticker")
    query_ticker = st.query_params.get("ticker", ticker)
    actual_ticker = session_ticker or query_ticker or ticker
    # Sanitize ticker to prevent XSS — allow only alphanumerics, dots, dashes
    if actual_ticker:
        actual_ticker = re.sub(r"[^A-Za-z0-9.\-]", "", actual_ticker)[:20]

    # Track the current active page so pages can detect navigation events
    st.session_state["_active_page"] = current_page

    _ec_href = f"/earnings_calendar?ticker={actual_ticker}"
    log_timing(
        "NAV_LINK_EARNINGS_CALENDAR",
        0,
        f"{_auth_request_meta()} target_page=earnings_calendar href={_ec_href!r} "
        f"url_type=same_host_relative current_page={current_page}",
        level="WARNING",
    )

    # STEP 3: Build header HTML
    header_html = '''<style>
    /* Header full-width wrapper - background #f2f2f2 */
    .coresight-header-exact {
      background-color: #f2f2f2;
      width: 100vw;
      margin-left: calc(-50vw + 50%);
      margin-right: calc(-50vw + 50%);
      box-sizing: border-box;
      position: sticky;
      top: 0;
      z-index: 1000;
    }

    /* Header container - 1440px max width, centered, flex layout */
    .coresight-header-container {
      max-width: 1440px;
      margin: 0 auto;
      height: 80px;
      display: flex;
      align-items: center;
      gap: 46px; /* Gap from logo to nav: 568 - 390 - 132 = 46px */
      padding-left: 106px; /* Logo at x=390 which is 106px from left edge of 1440px container */
    }

    /* Logo - 132x60px */
    .coresight-header-logo {
      flex-shrink: 0;
      width: 132px;
      height: 60px;
    }
    .coresight-header-logo img {
      width: 132px;
      height: 60px;
      object-fit: contain;
      display: block;
    }

    /* Navigation - horizontal layout with 48px gaps */
    .coresight-header-nav {
      display: flex;
      align-items: center;
      gap: 48px;
      height: 26px;
    }

    /* Nav links - Roboto 18px weight 500, color #2d2a29 */
    .coresight-header-nav a {
      font-family: 'Roboto', sans-serif;
      font-size: 18px;
      font-weight: 500;
      color: #2d2a29;
      text-decoration: none;
      white-space: nowrap;
      transition: color 0.2s ease;
      line-height: 26px;
    }
    .coresight-header-nav a:hover {
      color: #d62e2f;
    }
    .coresight-header-nav a.active {
      color: #d62e2f;
    }

    /* Logout button - styled as nav link with red color */
    .logout-btn {
    font-family: 'Roboto', sans-serif;
    font-size: 16px;
    font-weight: 500;
    color: #d62e2f;
    text-decoration: none;
    white-space: nowrap;
    line-height: 1;
    padding: 8px 18px;
    border-radius: 6px;
    border: 1px solid #d62e2f;
    background-color: white !important;
    transition: all 0.18s ease;
    margin-right: 40px;
    cursor: pointer;
  }

  .logout-btn:hover {
    background-color: #d62e2f !important;
    color: #ffffff;
    border-color: transparent;
    box-shadow: 0 2px 6px rgba(214, 46, 47, 0.25);
  }

    /* Remove default Streamlit toolbar — correct selector targets the actual element */
    header[data-testid="stHeader"] {
      display: none !important;
      height: 0 !important;
      min-height: 0 !important;
      margin: 0 !important;
      padding: 0 !important;
      overflow: hidden !important;
    }
    .main > div:first-child {
      padding-top: 0 !important;
    }

    /* Prevent horizontal scroll */
    html, body {
      overflow-x: hidden !important;
      max-width: 100% !important;
    }
    .stApp {
      overflow-x: hidden !important;
    }

    /* Responsive adjustments */
    @media (max-width: 1024px) {
      .coresight-header-container {
        padding-left: 20px;
        gap: 30px;
      }
      .coresight-header-nav {
        gap: 24px;
      }
      .coresight-header-nav a {
        font-size: 14px;
        font-weight: 500;
      }
    }

    @media (max-width: 768px) {
      .coresight-header-nav {
        display: none;
      }
    }

    </style>

    <div class="coresight-header-exact">
      <div class="coresight-header-container">
        <!-- Logo: 132x60 at x=390 (106px from left edge) -->
        <div class="coresight-header-logo">
          <a href="https://coresight.com/">
            <img src="https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net/wp-content/uploads/2023/12/coresight-logo-1.png"
                alt="Coresight Research" width="132" height="60">
          </a>
        </div>

        <!-- Navigation: at x=568, 48px gaps between items, Roboto 18px weight 500 -->
        <nav class="coresight-header-nav">
          <a href="/market_data?ticker=''' + actual_ticker + '''" target="_self" class=''' + ('"active"' if is_market_data else '""') + '''>Market Data Dashboard</a>
          <a href="/earnings_calls?ticker=''' + actual_ticker + '''" target="_self" class=''' + ('"active"' if is_earnings_calls else '""') + '''>Earnings Calls</a>
          <a href="/earnings_calendar?ticker=''' + actual_ticker + '''" target="_self" class=''' + ('"active"' if is_earnings_calendar else '""') + '''>Earnings Calendar</a>
          <a href="/screening?ticker=''' + actual_ticker + '''" target="_self" class=''' + ('"active"' if is_screening else '""') + '''>Screening</a>
          <a href="/newsroom?ticker=''' + actual_ticker + '''" target="_self" class=''' + ('"active"' if is_newsroom else '""') + '''>News</a>
        </nav>

        <!-- Spacer to push logout to right -->
        <div style="flex: 1;"></div>
        <a href="?action=logout" class="logout-btn">Logout</a>
        <!-- Logout placeholder - Streamlit button will be injected here -->
        <div id="logout-container" style="margin-right: 40px;"></div>
      </div>

    </div>'''

    # STEP 4: Render header HTML.
    # st.html() with unsafe_allow_javascript=True renders with full HTML support
    # and allows scripts to execute, enabling proper navigation.
    try:
        st.html(header_html, unsafe_allow_javascript=True)
    except TypeError:
        # Fallback for older Streamlit without unsafe_allow_javascript
        try:
            st.html(header_html)
        except AttributeError:
            st.markdown(header_html, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="navigation", component="render_header", operation="RENDER_HTML")
        st.markdown(header_html, unsafe_allow_html=True)

    # STEP 5: Inject transition JS into parent document via components iframe
    _inject_transition_js()


_RETAILER_ALLOWLIST = {
    "shashankgupta@coresight.com",
    "RisthaVilinaDsa@coresight.com",
    "PhilipMoore@coresight.com",
    "dataautomation@coresight.com",
    "mohdsaeedafri@coresight.com",
}

# Admin IAM panel access - restricted to 4 users
_ADMIN_IAM_ALLOWLIST = {
    "shashankgupta@coresight.com",
    "RisthaVilinaDsa@coresight.com",
    "PhilipMoore@coresight.com",
    "mohdsaeedafri@coresight.com",
}


def render_coresight_footer(full_width: bool = True, stick_to_bottom: bool = True):
    """
    Render Coresight footer based on Figma design - EXACT MATCH.

    Figma Reference: Advisory/Footer (Node ID: 20881:205174)
    - Background: #f2f2f2
    - Layout: 4 columns with 152px gap, horizontal
    - Column Order: Logo/Socials | LEARN MORE | GET IN TOUCH | QUICK LINKS
    - Link spacing: 16px vertical
    - Terms/Privacy gap: 25px horizontal
    - Copyright: centered with separator line above
    """
    # Staging-only links
    is_staging = os.getenv("APP_ENV", "").strip().lower() == "staging"
    # `auth_data.user_email` is the primary source, but can be absent depending on login/bootstrap timing.
    # Fall back to the auth manager so allowlisted links are reliably shown to the right users.
    user_email = (st.session_state.get("auth_data") or {}).get("user_email", "") or (get_current_user() or "")

    staging_links_html = ""
    if is_staging:
        staging_links_html += '\n          <a href="/company_filings_add_files" target="_self">Azure File Storage</a>'
        if user_email.lower() in {e.lower() for e in _RETAILER_ALLOWLIST}:
            staging_links_html += '\n          <a href="/retailer_adding" target="_self">Add Retailers</a>'

    am_link_html = ""
    if _can_access_mgmt(user_email):
        am_link_html = '\n          <a href="/access_management" target="_self">Access Management</a>'

    # STEP 1: Build style configuration
    if full_width:
        outer_style = "width: 100vw; margin-left: calc(-50vw + 50%); margin-right: calc(-50vw + 50%); box-sizing: border-box;"
    else:
        outer_style = ""

    # Bottom margin styles
    if stick_to_bottom:
        bottom_style = "margin-bottom: -100px !important; padding-bottom: 0 !important;"
    else:
        bottom_style = ""

    footer_html = f'''<style>
.coresight-footer-exact {{
  background-color: #f2f2f2;
  {outer_style}
  {bottom_style}
}}
.coresight-footer-container {{
  max-width: 1350px;
  margin: 0 auto;
  padding: 72px 20px 40px;
}}
/* Main content wrapper - 1249px width, horizontal layout with 152px gaps */
.coresight-footer-main {{
  display: flex;
  flex-direction: row;
  gap: 152px;
  margin-bottom: 40px;
  align-items: flex-start;
}}
/* Column 1: Logo section - 247px width */
.footer-col-logo {{
  width: 247px;
  display: flex;
  flex-direction: column;
  gap: 28px;
}}
.footer-logo-img {{
  width: 247px;
  height: 112px;
}}
/* Social icons - 36px circles with 12px gap */
.footer-socials-row {{
  display: flex;
  flex-direction: row;
  gap: 12px;
}}
.footer-social-icon {{
  width: 36px;
  height: 36px;
  background-color: #d62e2f;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}}
.footer-social-icon img {{
  width: 17px;
  height: 17px;
  display: block;
}}
/* Legal links - 25px gap */
.footer-legal-row {{
  display: flex;
  flex-direction: row;
  gap: 25px;
}}
.footer-legal-row a {{
  font-family: 'Roboto', sans-serif;
  font-size: 16px;
  color: #333333;
  text-decoration: none;
}}
.footer-legal-row a:hover {{
  color: #d62e2f;
}}
/* Columns 2-4: Navigation columns */
.footer-col-nav {{
  display: flex;
  flex-direction: column;
  gap: 0;
}}
/* Headers - Montserrat 25px, uppercase */
.footer-nav-header {{
  font-family: 'Montserrat', sans-serif;
  font-size: 25px;
  font-weight: 600;
  color: #333333;
  text-transform: uppercase;
  margin: 0 0 30px 0;
  line-height: 1.2;
}}
/* Links - vertical layout with 16px gap */
.footer-nav-links {{
  display: flex;
  flex-direction: column;
  gap: 16px;
}}
.footer-nav-links a {{
  font-family: 'Roboto', sans-serif;
  font-size: 16px;
  color: #333333;
  text-decoration: none;
  line-height: 22px;
}}
.footer-nav-links a:hover {{
  color: #d62e2f;
}}
/* Separator line */
.footer-separator-line {{
  width: 100%;
  height: 1px;
  background-color: #cbcaca;
  margin: 0 0 20px 0;
}}
/* Copyright - centered */
.footer-copyright-centered {{
  text-align: center;
  padding: 0;
}}
.footer-copyright-centered p {{
  font-family: 'Montserrat', sans-serif;
  font-size: 14px;
  color: #454545;
  margin: 0;
}}
/* Responsive */
@media (max-width: 1200px) {{
  .coresight-footer-main {{
    gap: 60px;
  }}
}}
@media (max-width: 900px) {{
  .coresight-footer-main {{
    flex-wrap: wrap;
    gap: 40px;
  }}
  .footer-col-logo {{
    width: 100%;
  }}
}}
@media (max-width: 640px) {{
  .coresight-footer-main {{
    flex-direction: column;
    gap: 30px;
  }}
  .footer-nav-header {{
    font-size: 20px;
  }}
}}
.main .block-container {{
  padding-bottom: 0 !important;
}}
</style>

<div class="coresight-footer-exact">
  <div class="coresight-footer-container">
    <!-- Main Footer Content - 4 Columns -->
    <div class="coresight-footer-main">

      <!-- Column 1: Logo & Socials -->
      <div class="footer-col-logo">
        <img src="https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net/wp-content/uploads/2023/12/coresight-logo.png"
             alt="Coresight Research" class="footer-logo-img" width="247" height="112">

        <div class="footer-socials-row">
          <a href="https://www.facebook.com/coresightresearch" target="_blank" rel="noopener noreferrer" class="footer-social-icon" aria-label="Facebook">
            <img src="{_FOOTER_SOCIAL_FB}" width="17" height="17" alt="" decoding="async" />
          </a>
          <a href="https://twitter.com/coresightnews" target="_blank" rel="noopener noreferrer" class="footer-social-icon" aria-label="Twitter">
            <img src="{_FOOTER_SOCIAL_TW}" width="17" height="17" alt="" decoding="async" />
          </a>
          <a href="#" target="_blank" rel="noopener noreferrer" class="footer-social-icon" aria-label="WeChat">
            <img src="{_FOOTER_SOCIAL_WC}" width="17" height="17" alt="" decoding="async" />
          </a>
          <a href="https://www.linkedin.com/company/coresight-research/" target="_blank" rel="noopener noreferrer" class="footer-social-icon" aria-label="LinkedIn">
            <img src="{_FOOTER_SOCIAL_LI}" width="17" height="17" alt="" decoding="async" />
          </a>
        </div>

        <div class="footer-legal-row">
          <a href="https://coresight.com/terms-of-service/" target="_blank">Terms of Use</a>
          <a href="https://coresight.com/privacy-policy/" target="_blank">Privacy Policy</a>
        </div>
      </div>

      <!-- Column 2: LEARN MORE -->
      <div class="footer-col-nav">
        <h3 class="footer-nav-header">Learn<br>More</h3>
        <div class="footer-nav-links">
          <a href="https://coresight.com/subscriptions/" target="_blank">Research Subscriptions</a>
          <a href="https://coresight.com/events/" target="_blank">Our Events</a>
          <a href="https://coresight.com/about-us/" target="_blank">About Us</a>
        </div>
      </div>

      <!-- Column 3: GET IN TOUCH -->
      <div class="footer-col-nav">
        <h3 class="footer-nav-header">Get In<br>Touch</h3>
        <div class="footer-nav-links">
          <a href="https://coresight.com/become-a-client/" target="_blank">Become a Client</a>
          <a href="https://coresight.com/about/contact/" target="_blank">Contact Us</a>
        </div>
      </div>

      <!-- Column 4: QUICK LINKS -->
      <div class="footer-col-nav">
        <h3 class="footer-nav-header">Quick<br>Links</h3>
        <div class="footer-nav-links">
          <a href="https://coresight.com/research/" target="_blank">Research Portal</a>
          <a href="https://coresight.com/retailistic-podcast/" target="_blank">The Retaili$tic Podcast</a>
          <a href="https://coresight.com/coresight-ai-council/" target="_blank">AI Council</a>{staging_links_html}{am_link_html}
        </div>
      </div>

    </div>

    <!-- Separator Line -->
    <div class="footer-separator-line"></div>

    <!-- Copyright - Centered -->
    <div class="footer-copyright-centered">
      <p>2026 Coresight Research. All rights reserved.</p>
    </div>
  </div>
</div>'''

    # STEP 2: Render footer HTML via st.html() (supports SVGs properly).
    try:
        st.html(footer_html, unsafe_allow_javascript=True)
    except TypeError:
        try:
            st.html(footer_html)
        except AttributeError:
            st.markdown(footer_html, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="navigation", component="render_coresight_footer", operation="RENDER_HTML")
        st.markdown(footer_html, unsafe_allow_html=True)


def _escape_html_for_display(text: str) -> str:
    """
    Escape HTML special characters for safe display in UI.

    This function handles apostrophes and other special characters
    to ensure proper rendering in HTML contexts.

    Parameters:
    -----------
    text : str
        The text to escape

    Returns:
    --------
    str
        HTML-escaped text safe for display
    """
    if not text:
        return text
    # Escape HTML special characters
    replacements = [
        ("&", "&amp;"),   # Must be first to avoid double-escaping
        ('"', "&quot;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("'", "&#39;"),   # Apostrophe/single quote
    ]
    for char, entity in replacements:
        text = text.replace(char, entity)
    return text


def render_company_header(company_name: str, ticker: str, exchange: str = "NYSE"):
    """
    Render reusable company header section with dropdown - matches Figma exactly.

    This is a centralized component that can be used on any page that needs
    the company header with:
    - "CORESIGHT MARKET DATA" red title
    - Company name with dropdown
    - Company Documents button

    Parameters:
    -----------
    company_name : str
        The company display name (e.g. "Macy's Inc")
    ticker : str
        The stock ticker symbol (e.g. "M")
    exchange : str, optional
        The stock exchange (default: "NYSE")

    Usage:
    ------
    from components.navigation import render_company_header
    from data.repository import CompanyRepository

    # Get company data
    companies = CompanyRepository.get_companies()

    # Render header
    render_company_header(
        company_name=company.name,
        ticker=company.ticker,
        exchange=company.exchange or "NYSE"
    )
    """
    from data.repository import CompanyRepository

    # CSS for the company header component
    header_css = """
    <style>
    /* Company Header Section - reusable component */
    .company-header-section {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        margin-bottom: 24px;
        padding: 24px 0;
    }

    .company-header-left {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }

    .section-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 700;
        font-size: 20px;
        color: #d62e2f;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        margin: 0;
        padding: 0;
    }

    .company-name-dropdown {
        font-family: 'Montserrat', sans-serif;
        font-weight: 700;
        font-size: 20px;
        color: #2d2a29;
        display: flex;
        align-items: center;
        gap: 4px;
    }

    .dropdown-arrow {
        width: 24px;
        height: 24px;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    .dropdown-arrow svg {
        width: 16px;
        height: 16px;
        stroke: #2d2a29;
    }

    .company-documents-btn {
        background-color: #d62e2f;
        color: #ffffff !important;
        font-family: 'Montserrat', sans-serif;
        font-weight: 700;
        font-size: 14px;
        padding: 10px 26px;
        border-radius: 4px;
        border: none;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        transition: background-color 0.2s ease;
        text-decoration: none !important;
    }
    .company-documents-btn .text {
      display: flex;
      flex-direction: column;
      line-height: 1.1;    /* tighter two-line look */
    }

    .company-documents-btn .text span {
      margin: 0;
    }

    .company-documents-btn:hover {
        background-color: #b52627;
    }

    .company-documents-btn svg {
        width: 20px;
        height: 20px;
    }

    /* Streamlit Selectbox Styling - positioned over the company name */
    div[data-testid="stSelectbox"]:has(> div > div > input[aria-label*="Select Company"]) {
        margin-top: -40px !important;
        margin-bottom: 20px !important;
        width: 400px !important;
        opacity: 0;
    }

    div[data-testid="stSelectbox"]:has(> div > div > input[aria-label*="Select Company"]) > div {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }

    div[data-testid="stSelectbox"]:has(> div > div > input[aria-label*="Select Company"]) label {
        display: none !important;
    }

    div[data-testid="stSelectbox"]:has(> div > div > input[aria-label*="Select Company"]) > div > div {
        height: 30px;
        cursor: pointer;
    }
    </style>
    """

    # HTML for the header display (company name + button)
    # Escape company name for safe HTML display (handles apostrophes like Macy's)
    display_company_name = _escape_html_for_display(company_name)
    header_html = f'''
    {header_css}
    <div class="company-header-section">
        <div class="company-header-left">
            <div class="section-title">CORESIGHT MARKET DATA</div>
            <div class="company-name-dropdown">
                <span>{display_company_name} ({exchange}:{ticker})</span>
                <span class="dropdown-arrow">
                    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M6 9L12 15L18 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                </span>
            </div>
        </div>
        <a href="/company_filings?ticker={ticker}" target="_blank" rel="noopener noreferrer" class="company-documents-btn">
            <span class="text">
              <span>Company</span>
              <span>Documents</span>
            </span>
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M7 1H3C2.46957 1 1.96086 1.21071 1.58579 1.58579C1.21071 1.96086 1 2.46957 1 3V15C1 15.5304 1.21071 16.0391 1.58579 16.4142C1.96086 16.7893 2.46957 17 3 17H15C15.5304 17 16.0391 16.7893 16.4142 16.4142C16.7893 16.0391 17 15.5304 17 15V11M9 9L17 1M17 1V6M17 1H12" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
        </a>
    </div>
    '''

    # Render the HTML header
    try:
        st.markdown(header_html, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="navigation", component="render_company_header", operation="RENDER_HTML")

    # Get all companies for the dropdown
    try:
        companies = CompanyRepository.get_companies()
    except Exception as e:
        log_structured_error(e, page="navigation", component="render_company_header", operation="GET_COMPANIES")
        companies = []

    # FIX: Add placeholder as first option so real companies start at index 1
    # This fixes the first-item-click issue on initial load
    placeholder = "─ Select Company ─"
    company_options = {placeholder: ""}  # Empty value for placeholder
    company_options.update({f"{c['name']} ({c['ticker']})": c['ticker'].strip() for c in companies})

    # Escape company name for dropdown display (handles apostrophes like Macy's)
    display_company_name = _escape_html_for_display(company_name)
    current_display = f"{display_company_name} ({ticker})"
    option_list = list(company_options.keys())

    # Set current value in session state
    if "company_selector_header" not in st.session_state:
        st.session_state.company_selector_header = current_display

    # Hidden Streamlit selectbox for functionality
    def on_company_change():
        selected = st.session_state.company_selector_header
        # Skip if placeholder selected
        if selected == placeholder:
            return
        selected_ticker = company_options[selected]
        # Update URL with new ticker - this automatically triggers a rerun
        st.query_params["ticker"] = selected_ticker
        # Also update active_ticker so the cross-page sync logic doesn't
        # override the new selection with the stale old ticker
        st.session_state.active_ticker = selected_ticker

    # Render selectbox
    try:
        st.selectbox(
            "Select Company",
            options=option_list,
            key="company_selector_header",
            on_change=on_company_change,
            label_visibility="collapsed"
        )
    except Exception as e:
        log_structured_error(e, page="navigation", component="render_company_header", operation="RENDER_SELECTBOX")


class Page(Enum):
    """Application pages."""
    MARKET_DATA = "market_data"
    NEWSROOM = "newsroom"


PAGE_CONFIG = {
    Page.MARKET_DATA: {
        "label": "Market Data",
        "icon": "📈",
        "description": "Real-time market data and analytics"
    },
    Page.NEWSROOM: {
        "label": "Newsroom",
        "icon": "📰",
        "description": "Latest news and market updates"
    },
}


def render_logo():
    """Render application logo."""
    st.markdown(f"""
    <div style="
        padding: {SPACING['space_6']} {SPACING['space_4']};
        margin-bottom: {SPACING['space_4']};
        border-bottom: 1px solid {COLORS['gray_200']};
    ">
        <div style="
            font-size: {TYPOGRAPHY['text_xl']};
            font-weight: {TYPOGRAPHY['font_bold']};
            color: {COLORS['primary']};
            display: flex;
            align-items: center;
            gap: 12px;
        ">
            <span style="font-size: 1.5rem;">📊</span>
            <span>Research Portal</span>
        </div>
        <div style="
            font-size: {TYPOGRAPHY['text_sm']};
            color: {COLORS['gray_500']};
            margin-top: 4px;
        ">Market Intelligence Platform</div>
    </div>
    """, unsafe_allow_html=True)



def render_user_state():
    """Render current user selections from local storage."""
    st.markdown(f"""
    <div style="
        font-size: {TYPOGRAPHY['text_xs']};
        font-weight: {TYPOGRAPHY['font_semibold']};
        color: {COLORS['gray_500']};
        text-transform: uppercase;
        letter-spacing: 0.1em;
        padding: 0 {SPACING['space_4']};
        margin-bottom: {SPACING['space_3']};
    ">Current Selection</div>
    """, unsafe_allow_html=True)

    # Get values from session state (synced from local storage)
    ticker = st.session_state.get("sec_filing", "-")
    start_date = st.session_state.get("start_date", "-")
    end_date = st.session_state.get("end_date", "-")

    st.markdown(f"""
    <div style="
        background: {COLORS['gray_100']};
        border-radius: {BORDER_RADIUS['rounded_lg']};
        padding: {SPACING['space_4']};
        margin: 0 {SPACING['space_2']};
    ">
        <div style="margin-bottom: {SPACING['space_3']};">
            <div style="
                font-size: {TYPOGRAPHY['text_xs']};
                color: {COLORS['gray_500']};
                margin-bottom: 2px;
            ">Ticker/Filing</div>
            <div style="
                font-size: {TYPOGRAPHY['text_sm']};
                font-weight: {TYPOGRAPHY['font_semibold']};
                color: {COLORS['gray_900']};
            ">{ticker}</div>
        </div>
        <div>
            <div style="
                font-size: {TYPOGRAPHY['text_xs']};
                color: {COLORS['gray_500']};
                margin-bottom: 2px;
            ">Date Range</div>
            <div style="
                font-size: {TYPOGRAPHY['text_sm']};
                color: {COLORS['gray_700']};
            ">{start_date} to {end_date}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_divider():
    """Render a divider line."""
    st.markdown(f"""
    <div style="
        height: 1px;
        background: {COLORS['gray_200']};
        margin: {SPACING['space_6']} {SPACING['space_4']};
    "></div>
    """, unsafe_allow_html=True)


def render_footer():
    """Render sidebar footer."""
    st.markdown(f"""
    <div style="
        padding: 0 {SPACING['space_4']};
        text-align: center;
    ">
        <div style="
            font-size: {TYPOGRAPHY['text_xs']};
            color: {COLORS['gray_500']};
            margin-bottom: 4px;
        ">Research Portal v1.0</div>
        <div style="
            font-size: {TYPOGRAPHY['text_xs']};
            color: {COLORS['gray_400']};
        ">© 2024 Market Intelligence</div>
    </div>
    """, unsafe_allow_html=True)


def render_top_bar():
    """Render top navigation bar with actions."""
    col1, col2, col3 = st.columns([6, 2, 2])

    with col2:
        st.button("🔔 Notifications", width="stretch")

    with col3:
        st.button("👤 Profile", width="stretch")


def get_page_title(page: Page) -> str:
    """Get the title for a page."""
    config = PAGE_CONFIG[page]
    return f"{config['icon']} {config['label']}"
