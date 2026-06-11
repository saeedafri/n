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
