"""
Loading Components - Coresight-branded loading spinners and progress indicators.
================================================================================

This module provides custom loading components that match Coresight's brand colors
(D62E2F - Coresight Red). Includes:

- Red spinner CSS injection
- Loading overlay with progress
- Inline loading indicators
- Skeleton placeholders

Usage:
    from components.loading import (
        inject_red_spinner_css,
        show_loading_overlay,
        show_inline_loading,
        hide_loading_overlay
    )

    # Apply red spinner globally
    inject_red_spinner_css()

    # Show overlay with message
    show_loading_overlay("Loading filings from Azure...")

    # Show inline spinner
    show_inline_loading("Fetching data...")

Author: Coresight Research
"""
import streamlit as st
from typing import Optional

from utils.server_logger import log_structured_error


def inject_red_spinner_css():
    """
    Inject custom CSS to make Streamlit spinners use Coresight Red.

    Call this once at the top of your page to apply the styling globally.
    """
    try:
        st.markdown("""
            <style>
            /* ===================================================================
               CORESIGHT RED SPINNER OVERRIDES
               =================================================================== */

            /* Main spinner circle - make it red */
            .stSpinner > div > div {
                border-top-color: #D62E2F !important;
                border-left-color: #D62E2F !important;
                border-right-color: transparent !important;
                border-bottom-color: transparent !important;
            }

            /* Spinner text color */
            .stSpinner > div > div > div {
                color: #D62E2F !important;
                font-weight: 500 !important;
            }

            /* Alternative selectors for different Streamlit versions */
            div[data-testid="stSpinner"] > div > div {
                border-top-color: #D62E2F !important;
                border-left-color: #D62E2F !important;
            }

            /* Loading text */
            .loading-text-red {
                color: #D62E2F;
                font-weight: 600;
                animation: pulse 1.5s infinite;
            }

            @keyframes pulse {
                0% { opacity: 0.6; }
                50% { opacity: 1; }
                100% { opacity: 0.6; }
            }

            /* ===================================================================
               LOADING OVERLAY STYLES
               =================================================================== */

            .cs-loading-overlay {
                position: fixed;
                top: 0;
                left: 0;
                width: 100vw;
                height: 100vh;
                background: rgba(255, 255, 255, 0.95);
                z-index: 9999;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 24px;
                backdrop-filter: blur(4px);
            }

            .cs-loading-spinner {
                width: 56px;
                height: 56px;
                border: 4px solid #f3f3f3;
                border-top: 4px solid #D62E2F;
                border-radius: 50%;
                animation: cs-spin 1s linear infinite;
            }

            @keyframes cs-spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }

            .cs-loading-text {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                font-size: 18px;
                font-weight: 600;
                color: #2D2A29;
                text-align: center;
            }

            .cs-loading-subtext {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                font-size: 14px;
                color: #888;
                text-align: center;
            }

            .cs-loading-progress {
                width: 300px;
                height: 4px;
                background: #f0f0f0;
                border-radius: 2px;
                overflow: hidden;
                margin-top: 8px;
            }

            .cs-loading-progress-bar {
                height: 100%;
                background: linear-gradient(90deg, #D62E2F 0%, #FF6B6B 100%);
                border-radius: 2px;
                transition: width 0.3s ease;
            }

            /* ===================================================================
               INLINE LOADING STYLES
               =================================================================== */

            .cs-inline-loading {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 16px 20px;
                background: #fff;
                border: 1px solid #E5E5E5;
                border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.06);
            }

            .cs-inline-spinner {
                width: 20px;
                height: 20px;
                border: 2px solid #f3f3f3;
                border-top: 2px solid #D62E2F;
                border-radius: 50%;
                animation: cs-spin 0.8s linear infinite;
                flex-shrink: 0;
            }

            .cs-inline-text {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                font-size: 14px;
                color: #323232;
            }

            /* ===================================================================
               SKELETON LOADING PLACEHOLDERS
               =================================================================== */

            .cs-skeleton {
                background: linear-gradient(90deg, #f0f0f0 25%, #e0e0e0 50%, #f0f0f0 75%);
                background-size: 200% 100%;
                animation: cs-skeleton-shimmer 1.5s infinite;
                border-radius: 4px;
            }

            @keyframes cs-skeleton-shimmer {
                0% { background-position: 200% 0; }
                100% { background-position: -200% 0; }
            }

            .cs-skeleton-text {
                height: 16px;
                margin-bottom: 8px;
            }

            .cs-skeleton-title {
                height: 24px;
                width: 60%;
                margin-bottom: 16px;
            }

            .cs-skeleton-card {
                height: 100px;
                margin-bottom: 12px;
                border-radius: 8px;
            }

            /* ===================================================================
               PROGRESS BAR STYLES
               =================================================================== */

            .cs-progress-container {
                width: 100%;
                max-width: 400px;
                margin: 0 auto;
            }

            .cs-progress-bar {
                height: 6px;
                background: #f0f0f0;
                border-radius: 3px;
                overflow: hidden;
            }

            .cs-progress-fill {
                height: 100%;
                background: linear-gradient(90deg, #D62E2F 0%, #FF6B6B 100%);
                border-radius: 3px;
                transition: width 0.3s ease;
            }

            .cs-progress-text {
                text-align: center;
                margin-top: 8px;
                font-family: 'Inter', sans-serif;
                font-size: 13px;
                color: #666;
            }
            </style>
        """, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="loading", component="inject_red_spinner_css", operation="inject CSS")


def show_loading_overlay(
    message: str = "Loading...",
    submessage: Optional[str] = None,
    show_progress: bool = False,
    progress_percent: float = 0
):
    """
    Show a full-screen loading overlay with Coresight branding.

    Args:
        message: Main loading message
        submessage: Optional secondary message
        show_progress: Whether to show progress bar
        progress_percent: Progress percentage (0-100)
    """
    try:
        progress_html = ""
        if show_progress:
            progress_html = f"""
                <div class="cs-loading-progress">
                    <div class="cs-loading-progress-bar" style="width: {progress_percent}%"></div>
                </div>
                <div class="cs-loading-subtext">{progress_percent:.0f}% complete</div>
            """

        subtext_html = f"""
            <div class="cs-loading-subtext">{submessage}</div>
        """ if submessage else ""

        html = f"""
            <div class="cs-loading-overlay" id="cs-loading-overlay">
                <div class="cs-loading-spinner"></div>
                <div class="cs-loading-text">{message}</div>
                {subtext_html}
                {progress_html}
            </div>
        """

        # Use a placeholder to show the overlay
        st.markdown(html, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="loading", component="show_loading_overlay", operation="render overlay")


def hide_loading_overlay():
    """
    Hide the loading overlay.

    Note: In Streamlit, this is done by rerunning the script without the overlay.
    This function is a placeholder for API compatibility.
    """
    try:
        # In Streamlit, the overlay disappears on next rerun
        pass
    except Exception as e:
        log_structured_error(e, page="loading", component="hide_loading_overlay", operation="hide overlay")


def show_inline_loading(message: str = "Loading..."):
    """
    Show an inline loading indicator.

    Args:
        message: Loading message to display
    """
    try:
        html = f"""
            <div class="cs-inline-loading">
                <div class="cs-inline-spinner"></div>
                <div class="cs-inline-text">{message}</div>
            </div>
        """
        st.markdown(html, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="loading", component="show_inline_loading", operation="render inline loading")


def show_skeleton_card():
    """Show a skeleton card placeholder."""
    try:
        st.markdown('<div class="cs-skeleton cs-skeleton-card"></div>', unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="loading", component="show_skeleton_card", operation="render skeleton card")


def show_skeleton_text(lines: int = 3):
    """
    Show skeleton text placeholders.

    Args:
        lines: Number of lines to show
    """
    try:
        for _ in range(lines):
            st.markdown('<div class="cs-skeleton cs-skeleton-text"></div>', unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="loading", component="show_skeleton_text", operation="render skeleton text")


def show_progress_bar(
    label: str = "Loading...",
    percent: float = 0,
    show_percent: bool = True
):
    """
    Show a styled progress bar.

    Args:
        label: Label text
        percent: Progress percentage (0-100)
        show_percent: Whether to show percentage text
    """
    try:
        percent = max(0, min(100, percent))

        percent_text = f"{percent:.0f}%" if show_percent else ""

        html = f"""
            <div class="cs-progress-container">
                <div class="cs-progress-bar">
                    <div class="cs-progress-fill" style="width: {percent}%"></div>
                </div>
                <div class="cs-progress-text">{label} {percent_text}</div>
            </div>
        """
        st.markdown(html, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="loading", component="show_progress_bar", operation="render progress bar")


def show_red_spinner(text: str = "Loading..."):
    """
    Show a Streamlit spinner with Coresight red color.

    This is a wrapper around st.spinner() that ensures the CSS is injected first.

    Args:
        text: Spinner text

    Returns:
        Streamlit spinner context manager
    """
    try:
        inject_red_spinner_css()
        return st.spinner(text)
    except Exception as e:
        log_structured_error(e, page="loading", component="show_red_spinner", operation="show spinner")
        return st.spinner(text)


# Coresight logo — same asset as the boot splash (core/boot_overlay.py), proven to
# load behind the STG proxy. onerror hides it so the card still shows spinner+label.
_CS_LOGO_URL = ("https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net"
                "/wp-content/uploads/2023/12/coresight-logo-1.png")

_BRANDED_LOADER_CSS = """
<style>
/* ── CENTERING ROOT-CAUSE FIX ─────────────────────────────────────────────────
   styles.py puts `will-change: opacity, transform` + a translateY entry animation
   on every element-container/stMarkdownContainer. Per CSS spec, will-change:transform
   (or any live transform) turns that ancestor into the CONTAINING BLOCK for
   position:fixed descendants — so this overlay was positioned against a 0-height
   box at the top of the content (STG bug: card at top, not centered; measured
   cardCenter y=146 vs viewport y=450). Neutralise those properties on any wrapper
   that contains the overlay so position:fixed means the VIEWPORT again. */
[data-testid="stMarkdownContainer"]:has(.cs-al-ov),
[data-testid="element-container"]:has(.cs-al-ov),
[data-testid="stElementContainer"]:has(.cs-al-ov),
[data-testid="stVerticalBlock"] > div:has(.cs-al-ov),
[data-testid="stHorizontalBlock"] > div:has(.cs-al-ov){
  animation:none!important;will-change:auto!important;
  transform:none!important;filter:none!important;}

/* ── ONE SPINNER ONLY: while the branded overlay is up, hide EVERY other
   loading indicator — the boot splash card, st.spinner, and inline page/article
   spinners — so the user never sees two spinners at once. The boot overlay and
   this in-app overlay are pixel-identical, so hiding boot the instant this one
   mounts makes the boot→in-app handoff read as ONE continuous spinner. Rules
   deactivate the moment the overlay leaves the DOM. */
body:has(.cs-al-ov) #cs-boot-overlay,
body:has(.cs-al-ov) #cs-ov{display:none!important;}
body:has(.cs-al-ov) [data-testid="stSpinner"],
body:has(.cs-al-ov) .cs-inline-loading,
body:has(.cs-al-ov) .cs-page-subspinner{display:none!important;}

/* Overlay: full viewport, translucent (the page stays visible + keeps loading
   behind it), and CLICK-BLOCKING (16-Jul, per business): while a tab loads,
   nothing behind may be clicked — clicks during a load only queue behind the
   running script and made the app feel broken (users opened dropdowns behind
   the spinner). The 22s pure-CSS failsafe also flips pointer-events off, so
   the overlay can never trap the user even if the remover JS fails. */
.cs-al-ov{position:fixed!important;inset:0!important;z-index:2147483000;
  background:rgba(244,244,244,.62);
  -webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);
  display:flex;align-items:center;justify-content:center;
  pointer-events:auto;cursor:wait;touch-action:none;
  animation:cs-al-in .18s ease both, cs-al-failsafe .4s ease 22s forwards;}
@keyframes cs-al-in{from{opacity:0}to{opacity:1}}
@keyframes cs-al-failsafe{to{opacity:0;visibility:hidden;pointer-events:none;}}
/* Card: IDENTICAL to the boot-splash card (logo + ring + label + shimmer) so a
   boot→in-app handoff reads as ONE spinner whose text changes. */
.cs-al-card{display:flex;flex-direction:column;align-items:center;gap:16px;
  padding:30px 44px;background:#fff;border-radius:14px;
  box-shadow:0 6px 40px rgba(0,0,0,.12);pointer-events:none;}
.cs-al-card img{width:132px;height:auto;display:block;}
.cs-al-ring{width:32px;height:32px;border-radius:50%;
  border:3px solid rgba(214,46,47,.14);border-top-color:#d62e2f;animation:cs-al-spin .7s linear infinite;}
@keyframes cs-al-spin{to{transform:rotate(360deg)}}
.cs-al-txt{font-family:Montserrat,'Source Sans Pro',system-ui,sans-serif;font-size:14px;
  font-weight:600;color:#555;letter-spacing:.01em;text-align:center;}
.cs-al-sh{width:108px;height:2px;border-radius:1px;
  background:linear-gradient(90deg,#ebebeb 25%,#d62e2f 50%,#ebebeb 75%);
  background-size:200% 100%;animation:cs-al-shm 1.6s ease infinite;}
@keyframes cs-al-shm{0%{background-position:200% 0}100%{background-position:-200% 0}}
</style>
"""


def _branded_overlay_html(label: str, oid: str = "cs-app-loader") -> str:
    """The branded Coresight loader card (logo + red spinner + dynamic label)."""
    return (
        _BRANDED_LOADER_CSS
        + f'<div id="{oid}" class="cs-al-ov"><div class="cs-al-card">'
        + f'<img src="{_CS_LOGO_URL}" alt="Coresight" referrerpolicy="no-referrer" '
        + 'onerror="this.style.display=\'none\'">'
        + '<div class="cs-al-ring"></div>'
        + f'<div class="cs-al-txt">{label}</div>'
        + '<div class="cs-al-sh"></div></div></div>'
    )


def render_page_loader(label: str = "Loading", overlay: bool = True, placeholder=None):
    """Branded Coresight loader — a centered white card with the Coresight logo, a red
    spinner and a DYNAMIC {label} (e.g. "Loading Income Statement"). Shown via st.empty()
    so it is visible DURING the server compute and cleared when the Python render
    finishes — correct for pages whose content is server-rendered HTML/tables
    (market_data, earnings_calls).

    For pages whose heavy content paints CLIENT-SIDE in an iframe AFTER Python returns
    (AgGrid results grid, streamlit_calendar) use render_sticky_loader() instead, so the
    overlay stays up until the grid actually appears (never vanishes onto a blank area).

    `overlay` is kept for backwards-compat (the card is always a centered overlay now).
    Returns the st.empty() placeholder; call .empty() on it when done.
    """
    try:
        holder = placeholder if placeholder is not None else st.empty()
        holder.markdown(_branded_overlay_html(label), unsafe_allow_html=True)
        return holder
    except Exception as e:
        log_structured_error(e, page="loading", component="render_page_loader", operation="render page loader")
        return placeholder


_STICKY_REMOVER_JS = """
<script>
(function(){
  try{
    var doc = window.parent.document;
    var ov = doc.getElementById('cs-sticky-loader');
    if(!ov) return;
    var main = doc.querySelector('[data-testid="stMain"]') || doc.body;
    // Hide ONLY once the heavy content has actually PAINTED — never on a "DOM settled"
    // heuristic, because during the server-compute wait the DOM is idle (looks settled)
    // while the grid/calendar has not rendered yet. "Painted" = the AgGrid / calendar
    // iframe has real height, OR a "No results" alert has appeared (nothing to render).
    function ready(){
      var f = main.querySelector('iframe[title*="agGrid"],iframe[title*="aggrid"],iframe[title*="calendar"]');
      if(f && f.clientHeight > 60) return true;
      var t = main.querySelector('[data-testid="stDataFrame"],[data-testid="stTable"]');
      if(t && t.clientHeight > 60) return true;
      var a = main.querySelector('[data-testid="stAlert"],[data-testid="stAlertContainer"],[data-testid="stException"]');
      if(a) return true;  // "No key development events found" / error → nothing to wait for
      return false;
    }
    var done=false;
    function hide(){ if(done) return; done=true;
      clearInterval(iv);
      ov.style.transition='opacity .3s ease'; ov.style.opacity='0';
      setTimeout(function(){ if(ov.parentNode) ov.parentNode.removeChild(ov); }, 320);
    }
    var iv = setInterval(function(){ if(ready()) hide(); }, 150);
    // hard cap: never trap the user behind the overlay (also covers the rare
    // no-iframe / no-alert page whose content is plain HTML).
    setTimeout(hide, 20000);
  }catch(e){
    try{ var o=window.parent.document.getElementById('cs-sticky-loader');
         if(o&&o.parentNode) o.parentNode.removeChild(o);}catch(_){}
  }
})();
</script>
"""


class _NoOpHolder:
    """Stand-in for an st.empty() placeholder whose .empty() is a no-op — the sticky
    overlay is removed by JS after the grid paints, so callers that still call
    `.empty()` (legacy inline-loader clears) must NOT tear it down early."""
    def empty(self, *a, **k):
        return None
    def markdown(self, *a, **k):
        return None


def render_sticky_loader(label: str = "Loading"):
    """Branded overlay that PERSISTS until the page's heavy content has actually painted
    client-side (AgGrid results grid / streamlit_calendar), then fades itself out via JS.
    Use on pages whose results render in an iframe AFTER Python returns, so the spinner
    never disappears onto a blank area (the screening "2000 events found + blank grid"
    and the earnings-calendar cases). Call ONCE, early in the render — the JS self-removes,
    so NO .empty() is needed. Returns a no-op holder so legacy `.empty()` calls are safe.
    """
    try:
        st.markdown(_branded_overlay_html(label, oid="cs-sticky-loader"), unsafe_allow_html=True)
        from streamlit.components.v1 import html as _sthtml
        _sthtml(_STICKY_REMOVER_JS, height=0)
    except Exception as e:
        log_structured_error(e, page="loading", component="render_sticky_loader", operation="render sticky loader")
    return _NoOpHolder()


def render_scan_progress_overlay(progress: dict):
    """
    Render a loading overlay specifically for background scan progress.

    Args:
        progress: Dict from get_scan_progress() with keys:
                 - completed, total, percent, eta_seconds
    """
    try:
        if not progress.get('is_running'):
            return

        completed = progress.get('completed', 0)
        total = progress.get('total', 0)
        percent = progress.get('percent', 0)
        eta = progress.get('eta_seconds')

        # Format ETA
        eta_text = ""
        if eta:
            if eta < 60:
                eta_text = f"~{eta}s remaining"
            elif eta < 3600:
                eta_text = f"~{eta//60}m remaining"
            else:
                eta_text = f"~{eta//3600}h {(eta%3600)//60}m remaining"

        submessage = f"Loaded {completed} of {total} companies"
        if eta_text:
            submessage += f" • {eta_text}"

        show_loading_overlay(
            message="Loading filings from Azure...",
            submessage=submessage,
            show_progress=True,
            progress_percent=percent
        )
    except Exception as e:
        log_structured_error(e, page="loading", component="render_scan_progress_overlay", operation="render scan progress overlay")
