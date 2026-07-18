"""
Earnings Calls Page - Coresight Research
========================================
Earnings call transcripts page using native Streamlit components with custom styling.
"""
import streamlit as st
import re
import os
import time as _time
from io import BytesIO
from typing import List, Optional, Tuple, Dict
from concurrent.futures import ThreadPoolExecutor

from components.styles import hide_sidebar, set_page_layout, render_styles, COLORS, TYPOGRAPHY, SPACING
from core.auth_manager import require_auth, get_current_user
# require_auth(page="earnings_calls")
hide_sidebar()

from components.navigation import render_header, render_coresight_footer
from data.repository import EarningsCallRepository, EarningsCalendarRepository
from data.watchlist_service import get_user_watchlists, get_watchlist_companies
from core.database import init_database
from utils.local_storage_manager import load_earnings_calls_state, save_earnings_calls_state
from utils.ticker_utils import validate_and_get_ticker, DEFAULT_FALLBACK_TICKER
try:
    from utils.server_logger import log_error, log_info, log_warning, PageLoadTracker, log_db_timing, log_timing, new_rerun_id, set_page_context, log_structured_error, error_boundary, log_render_complete
except ImportError:
    def log_error(*a, **kw): pass  # type: ignore
    def log_info(*a, **kw): pass  # type: ignore
    def log_warning(*a, **kw): pass  # type: ignore
    def log_db_timing(*a, **kw): pass  # type: ignore
    def log_timing(*a, **kw): pass  # type: ignore
    def new_rerun_id(*a, **kw): return ''  # type: ignore
    def set_page_context(*a, **kw): pass  # type: ignore
    def log_structured_error(*a, **kw): return ''  # type: ignore
    def error_boundary(*a, **kw): pass  # type: ignore
    def log_render_complete(*a, **kw): pass  # type: ignore
    PageLoadTracker = None  # type: ignore

# =============================================================================
# CRITICAL: One-time cache cleanup to prevent stale code issues
# This runs ONCE per process and removes __pycache__ directories
# =============================================================================
_earnings_cleanup_results = None
if '_earnings_cleanup_done' not in st.session_state:
    try:
        from utils.cache_cleaner import ensure_fresh_code
        _cleanup_start = _time.perf_counter()
        _earnings_cleanup_results = ensure_fresh_code()
        _cleanup_elapsed = (_time.perf_counter() - _cleanup_start) * 1000
        st.session_state['_earnings_cleanup_done'] = True
        # Store in session state for debugging visibility
        st.session_state['_earnings_cleanup_time_ms'] = _cleanup_elapsed
        st.session_state['_earnings_cleanup_results'] = _earnings_cleanup_results
    except Exception as _e:
        # Never fail the page load due to cleanup issues
        log_structured_error(_e, page="earnings_calls", component="module_init", operation="CLEANUP")
        st.session_state['_earnings_cleanup_error'] = str(_e)
        pass

# =============================================================================
# CSS - Styled Streamlit Components
# =============================================================================

def get_earnings_css() -> str:
    """Get custom CSS for earnings calls page - styles native Streamlit components."""
    try:
        return """
    <style>

    /* =======================================================================
       HIDE STREAMLIT CHROME
       ======================================================================= */
    [data-testid="stHeaderActionElements"] {
        display: none !important;
        visibility: hidden !important;
    }
    /* Also hide stHeader if styles.py didn't already catch it */
    header[data-testid="stHeader"] {
        display: none !important;
        height: 0 !important;
        min-height: 0 !important;
        overflow: hidden !important;
    }

    /* =======================================================================
       FIX: REMOVE EXTRA TOP PADDING ABOVE NAV
       Streamlit adds default top padding to block-container; remove it so
       the nav bar starts at the top of the viewport.
       ======================================================================= */
    div.block-container > div[data-testid="stVerticalBlock"],
    [data-testid="block-container"] > div[data-testid="stVerticalBlock"] {
        padding-top: 0 !important;
    }

    /* =======================================================================
       FIX: Two-column layout — anchor both columns at the top so the left
       search panel doesn't vertically center when the right PDF viewer is
       taller. Streamlit defaults stHorizontalBlock to align-items:center.
       ======================================================================= */
    .stHorizontalBlock {
        align-items: flex-start !important;
    }
    .stColumn {
        align-items: flex-start !important;
    }

    /* =======================================================================
       SCROLLBARS — Left panel (Streamlit container) + Right panel (transcript)
       Matches company_filings.py scrollbar style exactly.
       ======================================================================= */

    /* Left search panel scrollbar — target inner scrollable div of st.container */
    [data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]::-webkit-scrollbar,
    [data-testid="stVerticalBlockBorderWrapper"] div::-webkit-scrollbar {
        width: 6px;
    }
    [data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]::-webkit-scrollbar-track,
    [data-testid="stVerticalBlockBorderWrapper"] div::-webkit-scrollbar-track {
        background: #F2F2F2;
        border-radius: 3px;
    }
    [data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]::-webkit-scrollbar-thumb,
    [data-testid="stVerticalBlockBorderWrapper"] div::-webkit-scrollbar-thumb {
        background: #CBCACA;
        border-radius: 3px;
    }

    /* Left panel border/shadow — matches company_filings style */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border: 1px solid #E5E5E5 !important;
        border-radius: 12px !important;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05) !important;
    }

    /* Right transcript body scrollbar */
    .transcript-body::-webkit-scrollbar {
        width: 6px;
    }
    .transcript-body::-webkit-scrollbar-track {
        background: #F2F2F2;
        border-radius: 3px;
    }
    .transcript-body::-webkit-scrollbar-thumb {
        background: #CBCACA;
        border-radius: 3px;
    }

    /* =======================================================================
       FILTER BAR - Styled Streamlit Selectboxes
       ======================================================================= */

    /* Target Streamlit selectboxes in the filter area */
    div[data-testid="stSelectbox"] {
        min-height: auto !important;
    }

    /* Style the selectbox labels (helper text) */
    div[data-testid="stSelectbox"] label {
        font-family: 'Roboto', sans-serif !important;
        font-size: 12px !important;
        font-weight: 400 !important;
        color: #6B6B6B !important;
        margin-bottom: 4px !important;
    }

    /* Style the selectbox input container */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] {
        border: 1px solid #CBCACA !important;
        border-radius: 4px !important;
        background: #FFFFFF !important;
        min-height: 36px !important;
    }

    /* Style the selectbox input value text */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] span {
        font-family: 'Roboto', sans-serif !important;
        font-size: 14px !important;
        color: #2D2A29 !important;
    }

    /* Hover state */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"]:hover {
        border-color: #0066CC !important;
    }

    /* Focus state */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"][aria-expanded="true"] {
        border-color: #0066CC !important;
        box-shadow: 0 0 0 2px rgba(0, 102, 204, 0.2) !important;
    }

    /* Dropdown menu styling */
    div[data-baseweb="popover"] div[data-baseweb="menu"] {
        border: 1px solid #E5E5E5 !important;
        border-radius: 4px !important;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1) !important;
    }

    /* Dropdown options */
    div[data-baseweb="popover"] div[data-baseweb="menu"] li {
        font-family: 'Roboto', sans-serif !important;
        font-size: 14px !important;
    }

    /* =======================================================================
       TRANSCRIPT CARD
       Fixed at 520px to match the left search panel height exactly.
       Uses flex so the body fills remaining space after the header.
       ======================================================================= */
    .transcript-card {
        width: 100%;
        height: 520px;
        display: flex;
        flex-direction: column;
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        overflow: hidden;
        margin-top: 0;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05);
    }

    /* Card Header */
    .transcript-card-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        padding: 16px 20px;
        border-bottom: 1px solid #E5E5E5;
        background: #FFFFFF;
        gap: 12px;
    }

    .transcript-title-section {
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 8px;
        flex: 1;
        min-width: 0;
        margin-right: 16px;
    }

    .transcript-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 700;
        font-size: 18px;
        color: #2D2A29;
        word-break: normal;
        overflow-wrap: break-word;
        flex-shrink: 0;
        width: 100%;
        line-height: 1.25;
    }

    .transcript-meta {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        row-gap: 6px;
        column-gap: 8px;
        font-family: 'Roboto', sans-serif;
        font-weight: 500;
        font-size: 14px;
        color: #6B6B6B;
        white-space: normal;
        flex-shrink: 0;
        width: 100%;
    }

    .meta-dot {
        width: 4px;
        height: 4px;
        background: #6B6B6B;
        border-radius: 50%;
    }

    /* Download Button */
    .download-btn {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 12px;
        background: transparent;
        border: 1px solid #D62E2F;
        border-radius: 4px;
        cursor: pointer;
        font-family: 'Roboto', sans-serif;
        font-size: 14px;
        color: #D62E2F;
        text-decoration: none;
        transition: all 0.2s ease;
        flex-shrink: 0;
        white-space: nowrap;
    }

    .download-btn:hover {
        background: #D62E2F;
        color: #FFFFFF;
    }

    /* =======================================================================
       TRANSCRIPT CARD — Split header (st.columns) + body (st.markdown)
       ======================================================================= */

    /* Header row: styled like original card header */
    .ec-hdr-title-section {
        padding: 16px 20px 14px 20px;
    }

    /* Parent stHorizontalBlock that wraps the header columns.
       :not(:has([data-testid="stHorizontalBlock"])) ensures we only target the INNER
       header row (hdr_left + hdr_right) and NOT the outer two-column layout block
       (left_col + right_col). Without this, :has() propagates to all ancestors that
       contain .ec-hdr-title-section, applying align-items:center to the outer block
       and visually pushing the left search panel down (worst for tall PDF viewers). */
    [data-testid="stHorizontalBlock"]:has(.ec-hdr-title-section):not(:has([data-testid="stHorizontalBlock"])) {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px 12px 0 0;
        box-shadow: 0 2px 12px rgba(0,0,0,0.07), 0 1px 3px rgba(0,0,0,0.05);
        align-items: center !important;
        margin-bottom: 0 !important;
        padding-bottom: 0 !important;
    }

    /* Left column: top-align so stacked title + meta read naturally */
    [data-testid="stHorizontalBlock"]:has(.ec-hdr-title-section):not(:has([data-testid="stHorizontalBlock"])) [data-testid="stColumn"]:first-child {
        align-items: flex-start !important;
    }
    [data-testid="stHorizontalBlock"]:has(.ec-hdr-title-section):not(:has([data-testid="stHorizontalBlock"])) [data-testid="stColumn"]:first-child > div {
        align-items: flex-start !important;
    }

    /* Remove default padding from columns inside the header, centre content vertically */
    [data-testid="stHorizontalBlock"]:has(.ec-hdr-title-section):not(:has([data-testid="stHorizontalBlock"])) [data-testid="stColumn"] {
        display: flex !important;
        align-items: center !important;
    }
    [data-testid="stHorizontalBlock"]:has(.ec-hdr-title-section):not(:has([data-testid="stHorizontalBlock"])) [data-testid="stColumn"] > div {
        padding: 0 !important;
        display: flex !important;
        align-items: center !important;
        width: 100% !important;
    }

    /* Right column — JS download iframe, right-aligned & vertically centred */
    [data-testid="stHorizontalBlock"]:has(.ec-hdr-title-section):not(:has([data-testid="stHorizontalBlock"])) iframe {
        display: block !important;
        background: transparent !important;
        border: none !important;
        overflow: hidden !important;
    }

    /* Body wrapper — picks up from where the header ends */
    .ec-transcript-body-wrap {
        height: calc(520px - 54px);
        display: flex;
        flex-direction: column;
        border: 1px solid #E5E5E5;
        border-top: none;
        border-radius: 0 0 12px 12px;
        overflow: hidden;
        box-shadow: 0 2px 12px rgba(0,0,0,0.07), 0 1px 3px rgba(0,0,0,0.05);
        margin-top: -6px;
    }

    /* =======================================================================
       TRANSCRIPT BODY
       flex: 1 so it fills whatever height remains after the card header.
       This ensures the total card height stays at exactly 520px.
       ======================================================================= */
    .transcript-body {
        flex: 1;
        max-height: none;
        overflow-y: auto;
        padding: 20px;
        background: #F9F9F9;
    }

    .transcript-content {
        padding: 20px;
        background: #FFFFFF;
        border-radius: 8px;
    }

    /* Speaker Section */
    .speaker-section {
        margin-bottom: 24px;
    }

    .speaker-section:last-child {
        margin-bottom: 0;
    }

    .speaker-name {
        font-family: 'Roboto', sans-serif;
        font-weight: 700;
        font-size: 16px;
        color: #D62E2F;
        margin-bottom: 8px;
    }

    .speaker-text {
        font-family: 'Roboto', sans-serif;
        font-weight: 400;
        font-size: 15px;
        color: #2D2A29;
        line-height: 1.7;
    }

    /* =======================================================================
       EMPTY STATE
       ======================================================================= */
    .empty-state {
        padding: 60px 40px;
        text-align: center;
        background: #F9F9F9;
        border-radius: 8px;
        margin-top: 32px;
    }

    .empty-state-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 600;
        font-size: 18px;
        color: #2D2A29;
        margin-bottom: 8px;
    }

    .empty-state-text {
        font-family: 'Roboto', sans-serif;
        font-size: 14px;
        color: #6B6B6B;
    }

    /* =======================================================================
       SCROLLBAR STYLING
       ======================================================================= */
    .transcript-body::-webkit-scrollbar {
        width: 8px;
    }

    .transcript-body::-webkit-scrollbar-track {
        background: #F2F2F2;
        border-radius: 4px;
    }

    .transcript-body::-webkit-scrollbar-thumb {
        background: #CBCACA;
        border-radius: 4px;
    }

    .transcript-body::-webkit-scrollbar-thumb:hover {
        background: #999999;
    }

    /* ===== KEYWORD HIGHLIGHT — BRAND RED THEME ===== */
    mark {
        background: rgba(214, 46, 47, 0.15);
        color: #D62E2F;
        padding: 2px 4px;
        border-radius: 2px;
        font-weight: 600;
    }
    .transcript-content mark {
        background: rgba(214, 46, 47, 0.15);
        color: #D62E2F;
        padding: 2px 4px;
        border-radius: 2px;
        font-weight: 600;
    }

    /* ===== TRANSCRIPT SEARCH SIDEBAR ===== */
    .transcript-search-sidebar {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        padding: 16px;
        height: calc(100vh - 340px);
        min-height: 480px;
        overflow-y: auto;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05);
    }
    .transcript-search-sidebar::-webkit-scrollbar {
        width: 6px;
    }
    .transcript-search-sidebar::-webkit-scrollbar-track {
        background: #F2F2F2;
        border-radius: 3px;
    }
    .transcript-search-sidebar::-webkit-scrollbar-thumb {
        background: #CBCACA;
        border-radius: 3px;
    }

    .transcript-search-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid #F2F2F2;
    }
    .transcript-search-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 600;
        font-size: 16px;
        color: #2D2A29;
    }

    .transcript-search-count {
        font-family: 'Roboto', sans-serif;
        font-size: 12px;
        color: #888888;
        margin: 4px 0 12px 4px;
    }

    .transcript-search-result-card {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        padding: 12px;
        margin-bottom: 10px;
        cursor: pointer;
        transition: all 0.2s ease;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06), 0 1px 3px rgba(0, 0, 0, 0.04);
    }
    .transcript-search-result-card:hover {
        border-color: #CBCACA;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }
    .transcript-search-speaker {
        font-family: 'Roboto', sans-serif;
        font-weight: 600;
        font-size: 13px;
        color: #D62E2F;
        margin-bottom: 4px;
    }
    .transcript-search-snippet {
        font-family: 'Roboto', sans-serif;
        font-size: 12px;
        color: #6B6B6B;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .transcript-search-placeholder {
        text-align: center;
        color: #888;
        padding: 40px 0;
        font-family: 'Roboto', sans-serif;
        font-size: 14px;
    }
    /* Relevance score badges */
    .relevance-badge {
        font-family: 'Roboto', sans-serif;
        font-size: 11px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 10px;
        white-space: nowrap;
    }
    .relevance-high {
        background: #D4EDDA;
        color: #155724;
    }
    .relevance-mid {
        background: #FFF3CD;
        color: #856404;
    }
    .relevance-low {
        background: #F0F0F0;
        color: #6B6B6B;
    }

    /* ===== SEARCH RESULT VIEW BUTTON & META ===== */
    .transcript-result-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-top: 8px;
        padding-top: 6px;
        border-top: 1px solid #F0F0F0;
    }
    .transcript-result-meta {
        font-family: 'Roboto', sans-serif;
        font-size: 11px;
        color: #999;
        font-weight: 500;
        letter-spacing: 0.3px;
    }
    .transcript-view-btn {
        font-family: 'Roboto', sans-serif;
        font-size: 11px;
        font-weight: 600;
        color: #D62E2F;
        text-decoration: none;
        border: 1px solid #D62E2F;
        border-radius: 4px;
        padding: 3px 10px;
        transition: all 0.15s;
        background: transparent;
        white-space: nowrap;
        display: inline-block;
    }
    .transcript-view-btn:hover {
        background: #D62E2F;
        color: #fff !important;
        text-decoration: none !important;
    }

    /* Highlight the targeted segment when navigated via anchor */
    .speaker-section:target {
        background: rgba(214, 46, 47, 0.06);
        border-radius: 6px;
        outline: 1px solid rgba(214, 46, 47, 0.2);
        padding: 8px;
        margin: -8px;
    }

    button[data-testid="stBaseButton-secondary"] {
        background: transparent !important;
        border: 1px solid #D62E2F !important;
        color: #D62E2F !important;
        border-radius: 4px !important;
        padding: 7px 12px !important;
        font-size: 13px !important;
        font-weight: 500 !important;
        transition: background 0.15s, color 0.15s !important;
    }

    button[data-testid="stBaseButton-secondary"]:hover {
        background: #D62E2F !important;
        color: #FFFFFF !important;
    }

    </style>
    """
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="get_earnings_css", operation="generating CSS")
        return ""


# =============================================================================
# TRANSCRIPT PARSING# =============================================================================

@st.cache_data(ttl=600, show_spinner=False)
def parse_transcript(transcript_text: str, cache_version: int = 3) -> List[Dict]:
    """
    Parse transcript text into speaker segments.

    Returns List[Dict] with keys 'speaker' and 'text' — plain dicts are
    picklable so st.cache_data works correctly across Streamlit reruns.
    cache_version: cache-buster — increment to force re-processing.
    """
    try:
        _t0 = _time.perf_counter()

        if not transcript_text:
            return []

        text_length = len(transcript_text)

        import html

        # Step 1: Unescape HTML entities
        _t1 = _time.perf_counter()
        text_unescaped = html.unescape(transcript_text)

        # Step 2: Strip HTML tags
        cleaned = re.sub(r'<[^>]+>', '', text_unescaped)
        cleaned = cleaned.strip()
        _t2 = _time.perf_counter()

        # Step 3: Split into paragraphs and parse speakers
        paragraphs = re.split(r'\n\s*\n', cleaned)

        segments = []
        current_speaker = None
        current_text = []

        # Pattern to detect speaker names (Name: or Name Title:)
        speaker_pattern = re.compile(r'^([A-Z][a-zA-Z\s\.]+(?:\s+[A-Z][a-zA-Z]+)*):\s*(.*)$')

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            match = speaker_pattern.match(para)

            if match:
                if current_speaker and current_text:
                    segments.append({'speaker': current_speaker, 'text': ' '.join(current_text)})
                current_speaker = match.group(1).strip()
                current_text = [match.group(2).strip()] if match.group(2) else []
            else:
                current_text.append(para)

        if current_speaker and current_text:
            segments.append({'speaker': current_speaker, 'text': ' '.join(current_text)})

        if not segments and cleaned:
            segments.append({'speaker': 'Transcript', 'text': cleaned})

        _t3 = _time.perf_counter()
        _total_ms = (_t3 - _t0) * 1000
        _clean_ms = (_t2 - _t1) * 1000
        _parse_ms = (_t3 - _t2) * 1000

        return segments
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="parse_transcript", operation="parsing transcript text")
        return []


def _highlight_keyword(text: str, keyword: str) -> str:
    """Wrap keyword matches with <mark> tags — highlights each word individually for multi-word queries."""
    try:
        if not keyword or not keyword.strip():
            return text
        # Split into individual words and highlight each
        words = keyword.strip().split()
        result = text
        for word in words:
            if word.strip():
                pattern = re.compile(re.escape(word.strip()), re.IGNORECASE)
                result = pattern.sub(lambda m: f'<mark>{m.group()}</mark>', result)
        return result
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_highlight_keyword", operation="highlighting keyword in text")
        return text


def render_speaker_section(segment: Dict, keyword: str = None, index: int = 0) -> str:
    """Render a single speaker section with optional keyword highlighting."""
    try:
        paragraphs = segment['text'].split('\n')
        processed = []
        for p in paragraphs:
            p = p.strip()
            if not p:
                continue
            if keyword:
                p = _highlight_keyword(p, keyword)
            processed.append(f'<p style="margin: 0 0 12px 0;">{p}</p>')
        paragraphs_html = ''.join(processed)

        return f"""
    <div class="speaker-section" id="seg-{index}">
        <div class="speaker-name">{segment['speaker']}</div>
        <div class="speaker-text">{paragraphs_html}</div>
    </div>
    """
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="render_speaker_section", operation="rendering speaker section")
        return ""


def _format_ec_display_date(value) -> str:
    """Format a date or ISO date string as 'Mon DD, YYYY' for transcript chrome."""
    if value is None:
        return ""
    try:
        from datetime import date as _date
        if isinstance(value, str):
            _d = _date.fromisoformat(str(value)[:10])
        elif hasattr(value, "strftime"):
            _d = value
        else:
            _d = _date.fromisoformat(str(value)[:10])
        return _d.strftime("%b %d, %Y")
    except Exception:
        return str(value)[:10]


def _ec_date_equals(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        from datetime import date as _date
        da = _date.fromisoformat(str(a)[:10]) if isinstance(a, str) else (
            a if isinstance(a, _date) else _date.fromisoformat(str(a)[:10])
        )
        db = _date.fromisoformat(str(b)[:10]) if isinstance(b, str) else (
            b if isinstance(b, _date) else _date.fromisoformat(str(b)[:10])
        )
        return da == db
    except Exception:
        return False


def render_transcript_card(
    company_name: str,
    ticker: str,
    year: str,
    quarter: str,
    transcript_text: str,
    keyword: str = None,
    earnings_date=None,
) -> str:
    """Render the transcript card with header and content."""
    try:
        # Parse transcript into speaker segments
        segments = parse_transcript(transcript_text)

        # Render speaker sections with optional keyword highlighting.
        # .strip() removes trailing whitespace/newlines from the last section —
        # without it, Python-Markdown sees a blank line before the template's
        # closing </div> tags and renders them as a code block in the UI.
        speaker_html = ''.join([
            render_speaker_section(s, keyword=keyword, index=i)
            for i, s in enumerate(segments)
        ]).strip()

        # Format the earnings announcement date
        if earnings_date:
            try:
                from datetime import date as _date
                if isinstance(earnings_date, str):
                    _d = _date.fromisoformat(earnings_date)
                else:
                    _d = earnings_date
                date_str = _d.strftime("%b %d, %Y")
            except Exception as _exc:
                log_structured_error(_exc, page="earnings_calls", component="render_transcript_card", operation="FORMAT_DATE")
                date_str = str(earnings_date)
            date_html = f'<span class="meta-dot"></span><span>{date_str}</span>'
        else:
            date_html = ""

        html = f"""
    <div class="ec-transcript-body-wrap">
        <div class="transcript-body">
            <div class="transcript-content">
                {speaker_html}
            </div>
        </div>
    </div>
    """
        return html
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="render_transcript_card", operation="rendering transcript card")
        return ""


def _render_transcript_header_html(
    company_name: str,
    ticker: str,
    year: str,
    quarter: str,
    report_date=None,
    fiscal_period_end_date=None,
    fiscal_period_label: str = "",
) -> str:
    """Return just the title+meta HTML for the transcript card header row."""
    try:
        date_parts: List[str] = []
        if report_date:
            date_parts.append(f"Report date: {_format_ec_display_date(report_date)}")
        if fiscal_period_end_date and not _ec_date_equals(fiscal_period_end_date, report_date):
            date_parts.append(f"Quarter ended: {_format_ec_display_date(fiscal_period_end_date)}")

        date_html = ""
        for chunk in date_parts:
            date_html += f'<span class="meta-dot"></span><span>{chunk}</span>'

        # Build meta HTML - avoid blank line when date_html is empty
        meta_content = f"{year}<span class=\"meta-dot\"></span>{quarter}"
        meta_content += date_html
        if fiscal_period_label:
            meta_content += f'<span class="meta-dot"></span><span>Fiscal Period: {fiscal_period_label}</span>'

        return f"""<div class="ec-hdr-title-section"><div class="transcript-title-section"><span class="transcript-title">{company_name} ({ticker})</span><span class="transcript-meta">{meta_content}</span></div></div>"""
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_render_transcript_header_html", operation="rendering transcript header")
        return ""


def render_empty_state() -> str:
    """Render empty state when no transcript is available."""
    try:
        return """
    <div class="empty-state">
        <div class="empty-state-title">Select a Company, Year, and Quarter</div>
        <div class="empty-state-text">Choose filters above to view earnings call transcripts</div>
    </div>
    """
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="render_empty_state", operation="rendering empty state")
        return ""


def _render_js_download_button(pdf_bytes: bytes, filename: str) -> None:
    """
    Render a PDF download button that works entirely client-side via JavaScript
    Blob API — bypasses Streamlit's /media/ endpoint (which fails on proxied
    deployments when Nginx doesn't forward that path).
    """
    try:
        from utils.server_logger import track_download
        track_download("transcript_pdf", name=filename, page="earnings_calls")
    except Exception:
        pass
    try:
        import base64
        from streamlit.components.v1 import html as _sthtml

        b64 = base64.b64encode(pdf_bytes).decode("ascii")
        # Escape the filename for safe use in JS string
        safe_name = filename.replace("'", "\\'").replace('"', '\\"')

        btn_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{
  display:flex;justify-content:flex-end;align-items:center;
  height:52px;background:transparent;
  font-family:'Roboto',Helvetica,Arial,sans-serif;
  padding:0 20px;
}}
button{{
  background:transparent;
  border:1px solid #D62E2F;
  color:#D62E2F;
  border-radius:4px;
  padding:7px 12px;
  font-size:13px;
  font-weight:500;
  cursor:pointer;
  white-space:nowrap;
  transition:background 0.15s,color 0.15s;
  letter-spacing:0.01em;
}}
button:hover{{background:#D62E2F;color:#fff;}}
button:active{{opacity:0.85;}}
</style>
</head>
<body>
<button onclick="dl()">&#8659;&nbsp;&nbsp;Download</button>
<script>
var _d="{b64}";
function dl(){{
  try{{
    var bin=atob(_d),n=bin.length,u8=new Uint8Array(n);
    for(var i=0;i<n;i++) u8[i]=bin.charCodeAt(i);
    var blob=new Blob([u8],{{type:"application/pdf"}});
    var url=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=url; a.download="{safe_name}";
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(function(){{URL.revokeObjectURL(url);}},200);
  }}catch(e){{console.error("PDF download failed:",e);}}
}}
</script>
</body></html>"""
        _sthtml(btn_html, height=52, scrolling=False)
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_render_js_download_button", operation="rendering download button")


def _render_excel_js_download(excel_bytes: bytes, filename: str, label: str = "Excel", auto_click: bool = False) -> None:
    """Client-side Excel download button via JS Blob API; matches market_data.py."""
    try:
        from utils.server_logger import track_download
        track_download("earnings_excel", name=filename, page="earnings_calls")
    except Exception:
        pass
    try:
        import base64
        from streamlit.components.v1 import html as _sthtml

        b64 = base64.b64encode(excel_bytes).decode("ascii")
        safe_name = filename.replace("'", "\\'").replace('"', '\\"')
        safe_label = label.replace("'", "\\'").replace('"', '\\"')
        auto_trigger = "window.addEventListener('load', function(){ setTimeout(dl, 100); });" if auto_click else ""

        btn_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&icon_names=table" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{
  display:flex;justify-content:flex-end;align-items:center;
  height:52px;background:transparent;
  font-family:'Inter','Roboto',Helvetica,Arial,sans-serif;
  padding:0 20px;
}}
button{{
  background:transparent;
  border:1px solid #D62E2F;
  color:#D62E2F;
  border-radius:4px;
  padding:7px 12px;
  font-size:13px;
  font-weight:500;
  cursor:pointer;
  white-space:nowrap;
  transition:background 0.15s,color 0.15s;
  letter-spacing:0.01em;
  display:flex;align-items:center;gap:6px;
}}
button:hover{{background:#D62E2F;color:#fff;}}
button:active{{opacity:0.85;}}
.material-symbols-outlined {{
  font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
  font-size:18px;
}}
</style>
</head>
<body>
<button onclick="dl()"><span class="material-symbols-outlined">table</span>&nbsp;&nbsp;{safe_label}</button>
<script>
var _d="{b64}";
function dl(){{
  try{{
    var bin=atob(_d),n=bin.length,u8=new Uint8Array(n);
    for(var i=0;i<n;i++) u8[i]=bin.charCodeAt(i);
    var blob=new Blob([u8],{{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}});
    var url=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=url; a.download="{safe_name}";
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(function(){{URL.revokeObjectURL(url);}},200);
  }}catch(e){{console.error("Excel download failed:",e);}}
}}
{auto_trigger}
</script>
</body></html>"""
        _sthtml(btn_html, height=52, scrolling=False)
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_render_excel_js_download", operation="render_excel_download_button")


def _build_keyword_results_excel(results: List[Dict], ticker_display: Dict[str, str]) -> bytes:
    """Build a branded one-sheet workbook for loaded keyword search results."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from datetime import datetime

        def _company_from_ticker(ticker: str) -> str:
            label = ticker_display.get(ticker, ticker) or ticker
            return label.split("(")[0].strip() if "(" in label else label

        wb = Workbook()
        ws = wb.active
        ws.title = "Keyword Results"

        red = "D62E2F"
        dark = "2D2A29"
        gray = "6B6B6B"
        light_gray = "E0E0E0"
        header_bg = "F0F0F0"
        alt_row = "F9F9F9"
        keyword_fill = "FFF3CD"

        thin = Border(
            top=Side(style="thin", color=light_gray),
            bottom=Side(style="thin", color=light_gray),
            left=Side(style="thin", color=light_gray),
            right=Side(style="thin", color=light_gray),
        )

        headers = ["Company", "Ticker", "Year", "Quarter", "Reporter Name", "Whole Paragraph"]
        last_col = len(headers)
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        title = ws.cell(row=1, column=1, value="Earnings Calls Keyword Search Results")
        title.font = Font(name="Inter", size=14, bold=True, color=red)
        title.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[1].height = 30

        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
        subtitle = ws.cell(
            row=2,
            column=1,
            value=f"{len(results)} result{'s' if len(results) != 1 else ''} | Generated {datetime.now().strftime('%B %d, %Y %I:%M %p')}",
        )
        subtitle.font = Font(name="Inter", size=10, color=gray)
        subtitle.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[2].height = 22

        start_row = 4
        header_font = Font(name="Inter", size=10, bold=True, color=dark)
        header_fill = PatternFill(start_color=header_bg, end_color=header_bg, fill_type="solid")
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=start_row, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin
        ws.row_dimensions[start_row].height = 28

        # Hoist shared style objects out of the per-cell loop. Re-instantiating
        # Font/Fill/Alignment for every cell dominates build time on large
        # exports; sharing one object reference per style keeps it O(1).
        body_font = Font(name="Inter", size=10, color=dark)
        alt_fill = PatternFill(start_color=alt_row, end_color=alt_row, fill_type="solid")
        snippet_fill = PatternFill(start_color=keyword_fill, end_color=keyword_fill, fill_type="solid")
        align_left_top = Alignment(horizontal="left", vertical="top", wrap_text=True)
        align_left_center = Alignment(horizontal="left", vertical="center", wrap_text=True)

        for row_idx, result in enumerate(results, start_row + 1):
            ticker = str(result.get("ticker") or "").strip()
            values = [
                _company_from_ticker(ticker),
                ticker,
                str(result.get("year") or ""),
                str(result.get("quarter") or ""),
                str(result.get("speaker") or ""),
                re.sub(r"\s+", " ", str(result.get("paragraph") or result.get("snippet") or "")).strip(),
            ]
            is_alt = (row_idx - start_row) % 2 == 0
            has_snippet = bool(result.get("snippet"))
            for col_idx, value in enumerate(values, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.font = body_font
                cell.border = thin
                if col_idx == 6:
                    cell.alignment = align_left_top
                    if has_snippet:
                        cell.fill = snippet_fill
                    elif is_alt:
                        cell.fill = alt_fill
                else:
                    cell.alignment = align_left_center
                    if is_alt:
                        cell.fill = alt_fill
            ws.row_dimensions[row_idx].height = 72

        widths = [34, 12, 10, 10, 24, 90]
        for col_idx, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        ws.freeze_panes = f"A{start_row + 1}"
        ws.auto_filter.ref = f"A{start_row}:{get_column_letter(last_col)}{start_row + len(results)}"

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_build_keyword_results_excel", operation="build_excel")
        return b""


def _build_keyword_results_csv(results: List[Dict], ticker_display: Dict[str, str]) -> bytes:
    """Build a CSV of keyword search results — SIP-style export.

    Same rows and columns as the old Excel body (Company, Ticker, Year, Quarter,
    Reporter Name, Whole Paragraph). CSV builds in milliseconds where the styled
    openpyxl workbook took minutes on 10k rows, and opens directly in Excel.
    UTF-8 BOM so Excel renders non-ASCII correctly.
    """
    try:
        import csv
        from io import StringIO

        def _company_from_ticker(ticker: str) -> str:
            label = ticker_display.get(ticker, ticker) or ticker
            return label.split("(")[0].strip() if "(" in label else label

        buf = StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Company", "Ticker", "Year", "Quarter", "Reporter Name", "Whole Paragraph"])
        for result in results:
            ticker = str(result.get("ticker") or "").strip()
            writer.writerow([
                _company_from_ticker(ticker),
                ticker,
                str(result.get("year") or ""),
                str(result.get("quarter") or ""),
                str(result.get("speaker") or ""),
                re.sub(r"\s+", " ", str(result.get("paragraph") or result.get("snippet") or "")).strip(),
            ])
        return buf.getvalue().encode("utf-8-sig")
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_build_keyword_results_csv", operation="build_csv")
        return b""


def _safe_excel_filename_keyword(keyword: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (keyword or "").strip()).strip("_")
    return cleaned[:40] or "keyword"


def _matching_paragraph_for_export(transcript_text: str, keyword: str) -> Tuple[str, str, int]:
    """Return speaker + paragraph containing keyword from a transcript/window."""
    try:
        import html

        kw_lower = (keyword or "").lower()
        cleaned = html.unescape(transcript_text or "")
        cleaned = re.sub(r"<[^>]+>", "", cleaned).strip()
        speaker_pattern = re.compile(r'^([A-Z][a-zA-Z\s\.]+(?:\s+[A-Z][a-zA-Z]+)*):\s*(.*)$')
        current_speaker = "Transcript"

        for idx, para in enumerate(re.split(r"\n\s*\n", cleaned)):
            para = re.sub(r"\s+", " ", para.strip())
            if not para:
                continue
            match = speaker_pattern.match(para)
            if match:
                current_speaker = match.group(1).strip()
                para_text = (match.group(2) or "").strip()
                if para_text and kw_lower in para_text.lower():
                    return current_speaker, para_text, idx
                continue
            if kw_lower in para.lower():
                return current_speaker, para, idx

        lower = cleaned.lower()
        idx = lower.find(kw_lower)
        if idx >= 0:
            start = max(0, idx - 800)
            end = min(len(cleaned), idx + len(keyword) + 1800)
            para = re.sub(r"\s+", " ", cleaned[start:end].strip())
            return current_speaker, para, 0
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_matching_paragraph_for_export", operation="extract_paragraph")
    return "Transcript", "", 0


# =============================================================================
# PDF VIEWER (self-contained — no import from company_filings)
# Highlights keyword on ALL pages when no specific page is targeted.
# =============================================================================

def _render_ec_pdf_viewer(pdf_path: str, highlight_keyword: str = "", target_page: int = 0) -> None:
    """
    Render a transcript PDF using PDF.js — highlights keyword on every page.

    Args:
        pdf_path:         Absolute path to the PDF file on disk.
        highlight_keyword: Keyword to highlight on every page (red boxes).
        target_page:      If > 0, scroll to this page on load (from View → cards).

    Deliberately separate from company_filings._render_pdf_viewer so that
    earnings_calls.py never imports from pages.company_filings (which would
    trigger that module's bare main() call and redirect to the filings page).
    """
    import base64
    import json as _json
    import streamlit.components.v1 as components

    cache_key = f"ec_pdf_b64_{pdf_path}"
    if cache_key not in st.session_state:
        try:
            with open(pdf_path, "rb") as _f:
                st.session_state[cache_key] = base64.b64encode(_f.read()).decode("ascii")
        except Exception as e:
            log_structured_error(e, page="earnings_calls", component="render_pdf_viewer", operation="READ_PDF")
            st.error(f"Could not read PDF: {e}")
            return

    b64 = st.session_state[cache_key]
    _kw_js = _json.dumps(highlight_keyword)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #525659; }}
  #container {{
    height: 800px;
    overflow-y: auto;
    padding: 12px 0;
  }}
  .pg {{
    margin: 0 auto 10px;
    text-align: center;
    background: #3a3d40;
    max-width: 100%;
  }}
  canvas {{ display: block; margin: 0 auto; box-shadow: 0 1px 6px rgba(0,0,0,.5); }}
  #status {{ color: #aaa; text-align: center; padding: 60px 20px; font: 14px sans-serif; }}
</style>
</head>
<body>
<div id="container"><div id="status">Loading PDF...</div></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
<script>
(function () {{
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

  var b64 = "{b64}";
  var bin = atob(b64);
  var u8  = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);

  var container        = document.getElementById('container');
  var rendered         = {{}};
  var pdfDoc           = null;
  var highlightKeyword = {_kw_js};
  var targetPage       = {target_page};

  // Keyword highlight: draw red boxes directly onto the page canvas.
  // Runs on EVERY rendered page (no targetPage restriction).
  function drawKeywordOnCanvas(page, canvas, viewport) {{
    if (!highlightKeyword) return;
    var kw = highlightKeyword.toLowerCase();
    page.getTextContent().then(function (tc) {{
      var ctx       = canvas.getContext('2d');
      var firstHitY = null;
      var firstHitEl = canvas.parentElement;

      tc.items.forEach(function (item) {{
        if (!item.str || item.str.toLowerCase().indexOf(kw) === -1) return;
        var tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
        var cx = tx[4];
        var cy = tx[5];
        var cw = Math.abs(item.width  * viewport.scale);
        var ch = Math.abs(item.height * viewport.scale);
        if (cw < 2 || ch < 2) return;
        ctx.save();
        ctx.fillStyle = 'rgba(229, 57, 53, 0.35)';
        ctx.fillRect(cx, cy - ch, cw, ch);
        ctx.restore();
        if (firstHitY === null) firstHitY = cy - ch;
      }});

      // On first page with a hit, scroll so the match is centred
      if (firstHitY !== null && firstHitEl && !container._scrolledToKeyword) {{
        container._scrolledToKeyword = true;
        setTimeout(function () {{
          container.scrollTop = firstHitEl.offsetTop + firstHitY - (container.clientHeight / 2);
        }}, 100);
      }}
    }}).catch(function () {{}});
  }}

  pdfjsLib.getDocument({{ data: u8 }}).promise.then(function (pdf) {{
    pdfDoc = pdf;
    var total = pdf.numPages;
    container.innerHTML = '';

    for (var p = 1; p <= total; p++) {{
      var div = document.createElement('div');
      div.className  = 'pg';
      div.dataset.p  = p;
      div.style.width  = '816px';
      div.style.height = '1056px';
      container.appendChild(div);
    }}

    function renderPage(p) {{
      if (rendered[p] || p < 1 || p > total) return;
      rendered[p] = true;
      var el = container.querySelector('[data-p="' + p + '"]');
      if (!el) return;
      pdfDoc.getPage(p).then(function (page) {{
        var baseVp   = page.getViewport({{ scale: 1.0 }});
        var scale    = Math.min(900, (container.clientWidth || 900) - 24) / baseVp.width;
        var viewport = page.getViewport({{ scale: scale }});
        var canvas   = document.createElement('canvas');
        canvas.width  = viewport.width;
        canvas.height = viewport.height;
        page.render({{ canvasContext: canvas.getContext('2d'), viewport: viewport }})
            .promise.then(function () {{
              el.style.height     = '';
              el.style.background = '';
              el.innerHTML        = '';
              el.appendChild(canvas);
              // Highlight keyword on EVERY rendered page
              if (highlightKeyword) {{
                drawKeywordOnCanvas(page, canvas, viewport);
              }}
            }});
      }});
    }}

    var io = new IntersectionObserver(function (entries) {{
      entries.forEach(function (entry) {{
        if (entry.isIntersecting) {{
          var p = parseInt(entry.target.dataset.p, 10);
          renderPage(p);
          renderPage(p - 1);
          renderPage(p + 1);
        }}
      }});
    }}, {{ root: container, rootMargin: '400px' }});

    container.querySelectorAll('.pg').forEach(function (el) {{ io.observe(el); }});
    renderPage(1);
    renderPage(2);
    renderPage(3);

    // Scroll to a specific page when navigating from a search result card
    if (targetPage > 0) {{
      renderPage(targetPage - 1);
      renderPage(targetPage);
      renderPage(targetPage + 1);
      setTimeout(function () {{
        var el = container.querySelector('[data-p="' + targetPage + '"]');
        if (el) el.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      }}, 400);
    }}

  }}).catch(function (err) {{
    document.getElementById('status').textContent = 'PDF error: ' + err.message;
  }});
}})();
</script>
</body>
</html>"""

    components.html(html, height=820, scrolling=False)


@st.cache_data(ttl=300, show_spinner=False)
def _extract_pdf_text_by_page(pdf_path: str) -> list:
    """
    Extract text from a PDF grouped by page using PyMuPDF.
    Returns [(page_num, text), ...] — page_num is 1-indexed.
    Result is cached 5 min so repeated searches on the same PDF are instant.
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        result = [(i + 1, page.get_text("text")) for i, page in enumerate(doc)]
        doc.close()
        return result
    except Exception as _exc:
        log_structured_error(_exc, page="earnings_calls", component="_extract_pdf_text_by_page", operation="EXTRACT_PDF_TEXT")
        return []


def _search_pdf_pages(pages: list, keyword: str) -> list:
    """
    Search extracted PDF pages for keyword.
    Returns [{'page': N, 'snippet': '...'}, ...] for every matching page.
    """
    try:
        kw_lower = keyword.lower()
        matches = []
        for page_num, text in pages:
            if kw_lower in text.lower():
                idx = text.lower().find(kw_lower)
                start = max(0, idx - 60)
                end   = min(len(text), idx + len(keyword) + 80)
                raw   = text[start:end].replace('\n', ' ').strip()
                snippet = ('...' if start > 0 else '') + raw + ('...' if end < len(text) else '')
                matches.append({'page': page_num, 'snippet': snippet})
        return matches
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_search_pdf_pages", operation="search_pdf_pages")
        return []


# =============================================================================
# MAIN PAGE
# =============================================================================

def get_years(company):
    return EarningsCallRepository.get_available_years(company)

def get_quarters(company, year):
    return EarningsCallRepository.get_available_quarters(company, year)


def _watchlist_rows_to_tickers(rows: List[Dict]) -> Tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(row.get("ticker", "")).strip().upper()
            for row in (rows or [])
            if str(row.get("ticker", "")).strip()
        )
    )


@st.cache_data(ttl=120, show_spinner=False)
def _get_cross_search_results(
    keyword: str,
    company: str,
    year: str,
    quarter: str,
    watchlist_id: Optional[int] = None,
    allowed_tickers: Optional[Tuple[str, ...]] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Dict], int]:
    """
    Cross-transcript search: FULLTEXT DB lookup (~15ms) + cached segment extraction.

    Returns (results, raw_count): one best-matching segment per transcript, plus the
    number of transcripts the DB returned (used to decide whether "Load more" applies).
    Only the first `limit` matching transcripts are parsed — pagination keeps the
    initial render fast (parse a page, not the whole result set). Per-transcript parses
    are cached, so loading more only parses the new transcripts.
    """

    # Step 1: FULLTEXT DB lookup
    raw_rows = EarningsCallRepository.search_transcripts_fulltext(
        keyword=keyword,
        ticker=company if company != 'ALL' else None,
        year=year if str(year) != 'ALL' else None,
        quarter=quarter if quarter != 'ALL' else None,
        limit=limit,
        offset=offset,
        allowed_tickers=allowed_tickers if watchlist_id is not None else None,
    )

    # Step 2: Parse transcripts and extract matching segments

    results = []
    kw_lower = keyword.lower()
    parsed_count = 0

    for row in raw_rows:
        transcript_text = row.get('transcript_text', '') or ''
        if not transcript_text:
            continue

        segments = parse_transcript(transcript_text)
        parsed_count += 1

        for i, seg in enumerate(segments):
            if kw_lower in seg['text'].lower():
                idx = seg['text'].lower().find(kw_lower)
                start = max(0, idx - 40)
                end = min(len(seg['text']), idx + len(keyword) + 40)
                snippet = seg['text'][start:end]
                if start > 0:
                    snippet = '...' + snippet
                if end < len(seg['text']):
                    snippet = snippet + '...'

                q_val = row.get('q')
                q_str = f"Q{q_val}" if q_val else ''
                results.append({
                    'ticker': row.get('ticker', ''),
                    'year': str(row.get('year', '')),
                    'quarter': q_str,
                    'speaker': seg['speaker'],
                    'snippet': snippet,
                    'paragraph': seg['text'],
                    'seg_index': i,
                })
                break  # first match per transcript only

    return results, len(raw_rows)


@st.cache_resource
def _get_ec_excel_executor() -> ThreadPoolExecutor:
    """Singleton background pool for heavy Excel builds (shared across reruns/sessions)."""
    return ThreadPoolExecutor(max_workers=2)


# ── Cross-transcript export job registry ─────────────────────────────────────
# PROCESS-LEVEL via st.cache_resource so an in-flight build SURVIVES filter
# changes, reruns, and page navigation. It must NOT be a plain module global:
# st.Page re-executes this file in a fresh module namespace on every script
# run, so a module-level dict is wiped each rerun — the ready bytes were lost
# before any run could deliver them (the "export never completes" bug seen on
# STG 03-Jul). Keyed by the export signature; a run_every fragment polls this.
import threading as _ec_threading


@st.cache_resource(show_spinner=False)
def _ec_excel_job_registry() -> dict:
    return {"jobs": {}, "lock": _ec_threading.Lock()}


_EC_EXCEL_REG = _ec_excel_job_registry()
_EC_EXCEL_JOBS: Dict[str, dict] = _EC_EXCEL_REG["jobs"]   # sig_key -> {future, bytes, downloaded, filename, error}
_EC_EXCEL_JOBS_LOCK = _EC_EXCEL_REG["lock"]
_EC_EXCEL_JOBS_MAX = 12


def _ec_excel_job_get(sig_key: str) -> Optional[dict]:
    with _EC_EXCEL_JOBS_LOCK:
        j = _EC_EXCEL_JOBS.get(sig_key)
        return dict(j) if j else None


def _ec_excel_jobs_snapshot() -> List[tuple]:
    """Return a list of (sig_key, job-copy) for safe iteration outside the lock."""
    with _EC_EXCEL_JOBS_LOCK:
        return [(k, dict(v)) for k, v in _EC_EXCEL_JOBS.items()]


def _ec_excel_job_start(sig_key: str, filename: str, submit) -> None:
    """Start a build for sig_key once. `submit` is a zero-arg callable returning a Future."""
    with _EC_EXCEL_JOBS_LOCK:
        existing = _EC_EXCEL_JOBS.get(sig_key)
        if existing and (existing.get("bytes") or existing.get("error") or
                         (existing.get("future") is not None and not existing["future"].done())):
            return  # already done / building — don't resubmit
        if len(_EC_EXCEL_JOBS) >= _EC_EXCEL_JOBS_MAX:
            for old in list(_EC_EXCEL_JOBS.keys())[: len(_EC_EXCEL_JOBS) - _EC_EXCEL_JOBS_MAX + 1]:
                _EC_EXCEL_JOBS.pop(old, None)
        _EC_EXCEL_JOBS[sig_key] = {
            "future": submit(), "bytes": None, "downloaded": False,
            "filename": filename, "error": False,
        }


def _ec_excel_jobs_collect() -> None:
    """Move any finished futures' results into the registry (called from the poller)."""
    with _EC_EXCEL_JOBS_LOCK:
        items = list(_EC_EXCEL_JOBS.items())
    for k, j in items:
        if j.get("bytes") is None and not j.get("error"):
            fut = j.get("future")
            if fut is not None and fut.done():
                try:
                    data = fut.result() or b""
                except Exception as exc:
                    log_structured_error(exc, page="earnings_calls",
                                         component="_ec_excel_jobs_collect", operation="collect_background_excel")
                    data = b""
                with _EC_EXCEL_JOBS_LOCK:
                    if k in _EC_EXCEL_JOBS:
                        if data:
                            _EC_EXCEL_JOBS[k]["bytes"] = data
                        else:
                            _EC_EXCEL_JOBS[k]["error"] = True


def _build_cross_excel_job(
    keyword: str,
    company: str,
    year: str,
    quarter: str,
    watchlist_id: Optional[int],
    allowed_tickers: Optional[Tuple[str, ...]],
    ticker_display: Dict[str, str],
) -> bytes:
    """Heavy export build run OFF the script thread — never touches st.session_state.

    Fetches all matching keyword windows and renders a CSV (SIP-style — the old
    styled openpyxl workbook took minutes for 10k rows; CSV builds in ms with the
    exact same rows/columns). Runs inside the background executor so the
    Streamlit session stays interactive while it works.
    """
    try:
        _t0 = _time.perf_counter()
        results = _compute_all_cross_search_results_for_excel(
            keyword, company, year, quarter, watchlist_id, allowed_tickers,
        )
        _t1 = _time.perf_counter()
        data = _build_keyword_results_csv(results, ticker_display)
        log_timing(
            "EC_EXPORT_JOB_TOTAL",
            (_time.perf_counter() - _t0) * 1000,
            details=(
                f"rows={len(results)} fetch_ms={(_t1 - _t0) * 1000:.0f} "
                f"csv_ms={(_time.perf_counter() - _t1) * 1000:.0f} bytes={len(data)}"
            ),
            level="WARNING",
        )
        return data
    except Exception as e:
        log_structured_error(e, page="earnings_calls", component="_build_cross_excel_job", operation="background_export")
        return b""


@st.cache_data(ttl=120, show_spinner=False)
def _get_all_cross_search_results_for_excel(
    keyword: str,
    company: str,
    year: str,
    quarter: str,
    watchlist_id: Optional[int] = None,
    allowed_tickers: Optional[Tuple[str, ...]] = None,
) -> List[Dict]:
    """Cached wrapper around the export computation (foreground callers)."""
    return _compute_all_cross_search_results_for_excel(
        keyword, company, year, quarter, watchlist_id, allowed_tickers,
    )


def _compute_all_cross_search_results_for_excel(
    keyword: str,
    company: str,
    year: str,
    quarter: str,
    watchlist_id: Optional[int] = None,
    allowed_tickers: Optional[Tuple[str, ...]] = None,
) -> List[Dict]:
    """Fetch every matching cross-transcript result for Excel export.

    NOT cached — safe to call from a background worker thread (no ScriptRunContext
    needed). The cached entry point above delegates here for foreground callers.
    """
    raw_rows = EarningsCallRepository.search_transcript_windows_for_export(
        keyword=keyword,
        ticker=company if company != 'ALL' else None,
        year=year if str(year) != 'ALL' else None,
        quarter=quarter if quarter != 'ALL' else None,
        allowed_tickers=allowed_tickers if watchlist_id is not None else None,
        limit=10000,
    )
    kw_lower = keyword.lower()
    results: List[Dict] = []

    for row in raw_rows:
        transcript_text = row.get("transcript_text", "") or ""
        if not transcript_text:
            continue

        speaker, paragraph, seg_index = _matching_paragraph_for_export(transcript_text, keyword)
        if not paragraph or kw_lower not in paragraph.lower():
            continue

        idx = paragraph.lower().find(kw_lower)
        start = max(0, idx - 40)
        end = min(len(paragraph), idx + len(keyword) + 40)
        snippet = paragraph[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(paragraph):
            snippet = snippet + "..."

        q_val = row.get("q")
        results.append(
            {
                "ticker": row.get("ticker", ""),
                "year": str(row.get("year", "")),
                "quarter": f"Q{q_val}" if q_val else "",
                "speaker": speaker,
                "snippet": snippet,
                "paragraph": paragraph,
                "seg_index": seg_index,
            }
        )

    return results


def render_cross_search_panel(company: str, year: str, quarter: str) -> str:
    """Right panel shown when any filter is ALL (cross-transcript search mode)."""
    parts = []
    parts.append('All Companies' if company == 'ALL' else company)
    parts.append('All Years' if str(year) == 'ALL' else str(year))
    parts.append('All Quarters' if quarter == 'ALL' else quarter)
    scope = ' &bull; '.join(parts)

    return f"""
    <div class="empty-state" style="margin-top: 32px;">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#D62E2F" stroke-width="1.5" style="margin-bottom: 16px; opacity: 0.7;">
            <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <div class="empty-state-title">Search Across Transcripts</div>
        <div class="empty-state-text" style="margin-top: 10px;">
            <span style="font-weight: 600; color: #2D2A29; font-size: 13px;">{scope}</span><br><br>
            Type a keyword on the left to search all matching transcripts.<br>
            Click <strong>View &#8594;</strong> on any result to open the full transcript.
        </div>
    </div>
    """

def render_earnings_calls(active_ticker: str = None):
    """Render earnings calls content (for unified entry point)."""

    # =============================================================================
    # EMERGENCY CACHE CLEAR - Check for URL parameter
    # Usage: /earnings_calls?clear_cache=1
    # This clears cache for ALL users on the server!
    # =============================================================================
    if st.query_params.get("clear_cache") == "1":
        st.cache_data.clear()
        st.success("🚨 Cache cleared for ALL users! (Remove ?clear_cache=1 from URL)")
        st.info("Hard refresh the page to see fresh data: Ctrl+Shift+R or Cmd+Shift+R")
        if st.button("Continue to Earnings Calls"):
            st.query_params.clear()
            st.rerun()
        st.stop()

    _t0_page_start = _time.perf_counter()

    # ── Performance tracking ──
    new_rerun_id('earnings_calls')
    _tracker = PageLoadTracker('earnings_calls') if PageLoadTracker else None

    # Show loading placeholder immediately so the user sees feedback
    # before any blocking DB calls (companies, years, transcript fetch).
    from components.loading import render_page_loader
    _ec_loading_hint = render_page_loader("Loading Earnings Calls")

    # Inject custom CSS
    st.markdown(get_earnings_css(), unsafe_allow_html=True)

    # (title is rendered inline in the filter header row below — matches Figma layout)

    # Get data for dropdowns - CRITICAL PATH START
    # Parallelize independent DB calls: SEC companies + non-SEC companies
    if _tracker: _tracker.step_start("FETCH_COMPANIES")
    _t0_companies = _time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as _company_pool:
        _sec_future = _company_pool.submit(EarningsCallRepository.get_companies_with_earnings)
        _non_sec_future = _company_pool.submit(EarningsCallRepository.get_non_sec_transcript_companies)
        try:
            companies = _sec_future.result(timeout=30)
        except Exception as _exc:
            log_structured_error(_exc, page="earnings_calls", component="fetch_sec_companies", operation="THREAD_RESULT")
            companies = []
        try:
            # non-SEC transcript companies are SUPPLEMENTARY (materialized, warm≈0ms,
            # cold≈6.4s). Bound the wait at 15s so a cold startup-race can't block the
            # whole page for 30s; on the rare miss we degrade to [] and it fills from the
            # warm cache on the next load. This is EXPECTED cold behaviour, not an error —
            # log it as a WARNING so it never pollutes the ERROR stream.
            non_sec_transcript_companies = _non_sec_future.result(timeout=15)
        except Exception as _exc:
            log_warning(
                f"[EC_NONSEC] non-SEC transcript companies unavailable this load "
                f"(cold materialize race, degrading to empty; warms next load): "
                f"{type(_exc).__name__}"
            )
            non_sec_transcript_companies = []
    _t1_companies = _time.perf_counter()
    _companies_time = (_t1_companies - _t0_companies) * 1000
    _companies_warm = _companies_time < 1000
    log_timing(
        "EC_FETCH_COMPANIES",
        _companies_time,
        f"sec={len(companies)} non_sec={len(non_sec_transcript_companies)} warm={_companies_warm}",
    )
    if _tracker: _tracker.step_end("FETCH_COMPANIES", f"sec={len(companies)} non_sec={len(non_sec_transcript_companies)} parallel_ms={_companies_time:.0f}")
    if _tracker: _tracker.step_start("MERGE_COMPANIES")
    _sec_tickers = {c['ticker'] for c in companies}
    _non_sec_only_tickers = set()  # tickers that ONLY have NON-SEC transcripts (not in SEC table)
    # Deduplicate by BOTH ticker AND normalized name to prevent duplicate entries
    _seen_tickers = set(_sec_tickers)  # all SEC tickers already seen
    _seen_names = {c['name'].strip().lower() for c in companies}  # dedupe by normalized name
    for c in non_sec_transcript_companies:
        _norm_name = c['name'].strip().lower()
        if c['ticker'] not in _seen_tickers and _norm_name not in _seen_names:
            _non_sec_only_tickers.add(c['ticker'])
            companies.append(c)  # add to unified list
            _seen_tickers.add(c['ticker'])
            _seen_names.add(_norm_name)
        elif c['ticker'] not in _sec_tickers:
            # Ticker not in SEC but already seen (duplicate ticker or name) — skip adding
            # Still track as non-sec ticker for source routing
            _non_sec_only_tickers.add(c['ticker'])
    companies.sort(key=lambda x: x['name'])
    log_timing("EC_MERGE_COMPANIES", 0, f"total={len(companies)} non_sec_only={len(_non_sec_only_tickers)}")
    if _tracker: _tracker.step_end("MERGE_COMPANIES", f"total={len(companies)} non_sec_only={len(_non_sec_only_tickers)}")

    company_options = [('ALL', 'All Companies')] + [(c['ticker'], f"{c['name']} ({c['ticker'].upper()})") for c in companies]

    if len(company_options) <= 1:
        st.error("No earnings call data available.")
        st.stop()

    tickers = [opt[0] for opt in company_options]

    # Ticker → "Company Name (Ticker)" lookup built from already-fetched company_options.
    # Used in result card meta — no extra DB call needed.
    ticker_display = {ticker: label for ticker, label in company_options if ticker != 'ALL'}

    # ---------------------------------
    # Restore persisted state from local storage (before initializing defaults)
    # ---------------------------------
    load_earnings_calls_state()

    # ---------------------------------
    # Handle incoming navigation from cross-search results OR earnings calendar.
    # Uses a nav_id to consume params ONCE per navigation, not on every rerun.
    # URL (cross-search): /earnings_calls?ticker=NKE&year=2024&quarter=Q2&highlight=revenue
    # URL (calendar):     /earnings_calls?ticker=NKE&year=2024&quarter=Q3&from=calendar
    # ---------------------------------
    _qp_ticker = st.query_params.get("ticker", "")
    _qp_year = st.query_params.get("year", "")
    _qp_quarter = st.query_params.get("quarter", "")
    _qp_highlight = st.query_params.get("highlight", "")
    _qp_from = st.query_params.get("from", "")
    # pdf_page: from NON-SEC "View →" cards — scroll PDF viewer to this page
    _qp_pdf_page = int(st.query_params.get("pdf_page", 0) or 0)
    # keyword: preserved in URL by View → links so the search input survives navigation
    _qp_keyword = st.query_params.get("keyword", "")
    if _qp_keyword and not st.session_state.get("ec_search"):
        st.session_state.ec_search = _qp_keyword
    _nav_id = f"nav_{_qp_ticker}_{_qp_year}_{_qp_quarter}_{_qp_highlight}_{_qp_from}"

    # Calendar redirect: consume ticker + year + quarter directly (no highlight needed)
    _is_calendar_nav = _qp_from == "calendar" and st.session_state.get("_ec_nav_id") != _nav_id

    if _is_calendar_nav:
        st.session_state._ec_nav_id = _nav_id

        if _qp_ticker and _qp_ticker in tickers:
            st.session_state.ec_company = _qp_ticker
            # CRITICAL: ALWAYS clear widget keys on calendar navigation to ensure
            # the selectbox renders with the correct company from URL params.
            # This prevents stale widget state from overriding the navigation.
            st.session_state.pop("ec_company_select", None)
            st.session_state.pop("ec_year_select", None)
            st.session_state.pop("ec_quarter_select", None)

        # Handle year/quarter with fallback to latest available if not provided or invalid
        # This ensures backward compatibility if calendar data is incomplete
        _resolved_year = _qp_year
        _resolved_quarter = _qp_quarter

        # If year not provided or invalid, get the latest available year for this ticker
        if not _resolved_year or _resolved_year == 'ALL':
            if _qp_ticker and _qp_ticker in tickers and _qp_ticker != 'ALL':
                available_years = get_years(_qp_ticker)
                if available_years:
                    _resolved_year = str(max(available_years))

        # If quarter not provided or invalid, get the latest available quarter for year
        if not _resolved_quarter or _resolved_quarter == 'ALL':
            if _qp_ticker and _qp_ticker in tickers and _qp_ticker != 'ALL' and _resolved_year and _resolved_year != 'ALL':
                available_quarters = get_quarters(_qp_ticker, _resolved_year)
                if available_quarters:
                    # Sort quarters (Q1, Q2, Q3, Q4) and pick the latest
                    _resolved_quarter = sorted(available_quarters)[-1]

        if _resolved_year and _resolved_year != 'ALL':
            st.session_state.ec_year = _resolved_year
            if st.session_state.get("ec_year_select") != _resolved_year:
                st.session_state.pop("ec_year_select", None)
        if _resolved_quarter and _resolved_quarter != 'ALL':
            st.session_state.ec_quarter = _resolved_quarter
            st.session_state._ec_quarter_user_set = True
            if st.session_state.get("ec_quarter_select") != _resolved_quarter:
                st.session_state.pop("ec_quarter_select", None)
    else:
        if _qp_from == "calendar":
            pass  # already processed this nav
        else:
            pass  # _qp_from is not 'calendar'

    if _qp_highlight and st.session_state.get("_ec_nav_id") != _nav_id:
        # Fresh cross-search navigation — consume params into session state
        st.session_state._ec_nav_id = _nav_id
        st.session_state.ec_search = _qp_highlight
        if _qp_ticker and _qp_ticker in tickers:
            st.session_state.ec_company = _qp_ticker
        if _qp_year and _qp_year != 'ALL':
            st.session_state.ec_year = _qp_year
        if _qp_quarter and _qp_quarter != 'ALL':
            st.session_state.ec_quarter = _qp_quarter

    # ---------------------------------
    # Initialize company state — respect cross-page active_ticker
    # ---------------------------------
    # Resolve the ticker to use: URL param > session active_ticker > default
    _resolved_ticker = active_ticker
    if (not _resolved_ticker or _resolved_ticker not in tickers) and st.session_state.get("active_ticker") in tickers:
        _resolved_ticker = st.session_state.active_ticker

    if "ec_company" not in st.session_state:
        if _resolved_ticker and _resolved_ticker in tickers:
            st.session_state.ec_company = _resolved_ticker
        else:
            st.session_state.ec_company = tickers[0]  # "ALL"
    # If URL or active_ticker provides a specific ticker, override current selection
    elif _resolved_ticker and _resolved_ticker in tickers and _resolved_ticker != st.session_state.ec_company:
        # Only override if the user just navigated here (URL ticker differs from current)
        _qp_ticker_raw = st.query_params.get("ticker", "")
        if _qp_ticker_raw and _qp_ticker_raw in tickers:
            st.session_state.ec_company = _qp_ticker_raw
    # Validate persisted company still exists in available tickers
    if st.session_state.ec_company not in tickers:
        st.session_state.ec_company = tickers[0]

    # Detect if current company is NON-SEC transcript-only
    _is_non_sec_transcript = (
        st.session_state.ec_company != 'ALL'
        and st.session_state.ec_company in _non_sec_only_tickers
    )

    # Get available years+quarters in ONE DB call (saves 1 round-trip)
    if _tracker: _tracker.step_start("FETCH_YEARS_QUARTERS")
    _yq_map = None  # year→quarters mapping from combined query
    if st.session_state.ec_company == 'ALL':
        _all_yrs = EarningsCallRepository.get_all_available_years()
        year_options = ['ALL'] + [str(y) for y in _all_yrs]
    elif _is_non_sec_transcript:
        _yq_map = EarningsCallRepository.get_non_sec_transcript_years_and_quarters(st.session_state.ec_company)
        available_years = sorted(_yq_map.keys(), reverse=True) if _yq_map else []
        year_options = ['ALL'] + ([str(y) for y in available_years] if available_years else ["2025", "2024"])
    else:
        _yq_map = EarningsCallRepository.get_years_and_quarters(st.session_state.ec_company)
        available_years = sorted(_yq_map.keys(), reverse=True) if _yq_map else []
        year_options = ['ALL'] + ([str(y) for y in available_years] if available_years else ["2025", "2024"])
    if _tracker: _tracker.step_end("FETCH_YEARS_QUARTERS", f"company={st.session_state.ec_company} years={len(year_options)-1}")

    # Company is the parent filter. The cascade to Year=ALL / Quarter=ALL fires once,
    # in on_company_change (the ACTION of selecting ALL) — NOT here on every render,
    # so the user can still narrow Year/Quarter while keeping Company on ALL.
    _ec_company_is_all = (st.session_state.ec_company == 'ALL')

    # If we've landed with a fresh URL ticker parameter (different from session state),
    # reset the year to the latest available for that ticker
    _fresh_ticker = (st.query_params.get("ticker") and st.query_params.get("ticker") != st.session_state.get("_ec_last_url_ticker"))

    if "ec_year" not in st.session_state or st.session_state.ec_year not in year_options or _fresh_ticker:
        # Default to LATEST real year. available_years is sorted reverse=True
        # (newest first), so year_options = ['ALL', <newest>, ..., <oldest>] and
        # the newest real year is index 1 — NOT [-1], which is the OLDEST and
        # made Macy's (M) land on 2007 instead of 2026 on fresh load (STG 03-Jul).
        st.session_state.ec_year = year_options[1] if len(year_options) > 1 else year_options[0]
        if _fresh_ticker:
            st.session_state._ec_last_url_ticker = st.query_params.get("ticker")

    # Derive quarters from the combined result (no extra DB call)
    if st.session_state.ec_company != 'ALL' and str(st.session_state.ec_year) != 'ALL' and _yq_map is not None:
        yr_key = int(st.session_state.ec_year) if str(st.session_state.ec_year).isdigit() else None
        available_quarters = _yq_map.get(yr_key, []) if yr_key else []
        quarter_options = ['ALL'] + (sorted(available_quarters) if available_quarters else ["Q1", "Q2", "Q3", "Q4"])
    elif st.session_state.ec_company != 'ALL' and str(st.session_state.ec_year) != 'ALL':
        # Fallback to original separate query if combined wasn't used
        available_quarters = get_quarters(st.session_state.ec_company, st.session_state.ec_year)
        quarter_options = ['ALL'] + (sorted(available_quarters) if available_quarters else ["Q1", "Q2", "Q3", "Q4"])
    else:
        quarter_options = ['ALL', 'Q1', 'Q2', 'Q3', 'Q4']

    # Default to latest available quarter (last in sorted list)
    # BUT: Preserve calendar navigation values - if from=calendar and we just
    # navigated here, trust the session state set by the calendar even if
    # _ec_quarter_user_set isn't True yet (it's set after the nav block runs)
    _qp_from = st.query_params.get("from", "")
    _just_from_calendar = _qp_from == "calendar" and st.session_state.get("_ec_nav_id") == f"nav_{_qp_ticker}_{_qp_year}_{_qp_quarter}__calendar"

    # If we've landed with a fresh URL ticker, reset quarter to latest
    if "ec_quarter" not in st.session_state or st.session_state.ec_quarter not in quarter_options or _fresh_ticker:
        # Default to LATEST quarter (last item in sorted list)
        st.session_state.ec_quarter = quarter_options[-1] if len(quarter_options) > 1 else quarter_options[0]
    elif not _ec_company_is_all:
        # Even if ec_quarter exists, default to latest on fresh page load
        # (user can still override via dropdown).
        # EXCEPTION 1: Don't override if we just navigated from calendar.
        # EXCEPTION 2: When Company is ALL we never auto-narrow the Quarter —
        # keep it at the user's choice (ALL by default) so cross-transcript
        # search stays broad after the parent-filter cascade.
        if st.session_state.get('_ec_quarter_user_set') != True and not _just_from_calendar:
            st.session_state.ec_quarter = quarter_options[-1] if len(quarter_options) > 1 else quarter_options[0]

    # Dynamic widget keys — include company+year so the key changes whenever the
    # selection changes.  A new key = brand-new Streamlit widget that reads
    # index= instead of restoring the old frontend value.  This is the only
    # reliable way to force the Year/Quarter dropdowns to display the correct
    # value after a company or year change (pop() loses the race against
    # Streamlit's frontend-state sync which runs after callbacks).
    _year_widget_key = f"ec_year_select_{st.session_state.ec_company}"
    _quarter_widget_key = f"ec_quarter_select_{st.session_state.ec_company}_{st.session_state.ec_year}"

    # ── PREFETCH: transcript + earnings event (report date + quarter-end) ────
    # After years/quarters returns, we know the default company/year/quarter.
    # Start transcript fetch and `get_earnings_event_dates` in parallel (~100–200ms overlap).
    _pf_company = st.session_state.ec_company
    _pf_year = st.session_state.ec_year
    _pf_quarter = st.session_state.ec_quarter
    _pf_is_cross = (_pf_company == 'ALL' or str(_pf_year) == 'ALL' or _pf_quarter == 'ALL')
    _pf_is_non_sec = (_pf_company in _non_sec_only_tickers)

    # Analytics: company/year/quarter/keyword are session-state (not URL), so the auto
    # page_view misses them. Capture the full earnings-calls filter state.
    try:
        from utils.server_logger import log_filters_if_changed
        log_filters_if_changed(
            "earnings_calls",
            company=_pf_company,
            year=str(_pf_year),
            quarter=str(_pf_quarter),
            keyword=st.session_state.get('ec_search') or None,
        )
    except Exception:
        pass
    _transcript_future = None
    _edate_future = None
    if not _pf_is_cross and not _pf_is_non_sec:
        _ec_prefetch_exec = ThreadPoolExecutor(max_workers=2)
        _transcript_future = _ec_prefetch_exec.submit(
            EarningsCallRepository.get_earnings_calls,
            ticker=_pf_company,
            year=_pf_year,
            quarter=_pf_quarter,
        )
        _pq_int = int(str(_pf_quarter).replace("Q", "")) if _pf_quarter and str(_pf_quarter) not in ("ALL", "") else None
        _pyr_int = int(_pf_year) if _pf_year and str(_pf_year) not in ("ALL", "") else None
        if _pq_int and _pyr_int:
            _edate_future = _ec_prefetch_exec.submit(
                EarningsCalendarRepository.get_earnings_event_dates, _pf_company, _pq_int, _pyr_int
            )

    # =======================================================================
    # HEADER WITH TITLE AND FILTERS
    # =======================================================================
    def on_company_change():
        ticker = st.session_state.ec_company_select
        # Update shared session state only — do NOT write st.query_params here.
        # Writing to query_params triggers Streamlit's navigation rerun path (a
        # stronger DOM teardown than a normal widget rerun) which causes the native
        # stHeader's preserved layout height to flash as a blank strip above the
        # custom nav.  URL deep-linking is handled on initial load via _qp_ticker;
        # the dropdown is treated as app state, not navigation state.
        st.session_state.ec_company = ticker
        if ticker != 'ALL':
            st.session_state.active_ticker = ticker

        if ticker == 'ALL':
            # Parent filter ALL → cascade Year and Quarter to ALL (cross-transcript
            # search). No need to fetch year/quarter options for a single company.
            st.session_state.ec_year = 'ALL'
            st.session_state.ec_quarter = 'ALL'
            st.session_state._ec_quarter_user_set = False
            return

        if ticker in _non_sec_only_tickers:
            _yq = EarningsCallRepository.get_non_sec_transcript_years_and_quarters(ticker)
            years = sorted(_yq.keys(), reverse=True) if _yq else []
            year_opts = ['ALL'] + ([str(y) for y in years] if years else ["2025", "2024"])
        else:
            _yq = EarningsCallRepository.get_years_and_quarters(ticker)
            years = sorted(_yq.keys(), reverse=True) if _yq else []
            year_opts = ['ALL'] + ([str(y) for y in years] if years else ["2025", "2024"])

        # Default to first real year when changing company
        st.session_state.ec_year = year_opts[1] if len(year_opts) > 1 else year_opts[0]

        if ticker != 'ALL' and str(st.session_state.ec_year) != 'ALL' and _yq:
            yr_key = int(st.session_state.ec_year) if str(st.session_state.ec_year).isdigit() else None
            quarters = _yq.get(yr_key, []) if yr_key else []
            q_opts = ['ALL'] + (sorted(quarters) if quarters else ["Q1", "Q2", "Q3", "Q4"])
        else:
            q_opts = ['ALL', 'Q1', 'Q2', 'Q3', 'Q4']

        st.session_state.ec_quarter = q_opts[-1] if len(q_opts) > 1 else q_opts[0]
        st.session_state._ec_quarter_user_set = False
        # No pop needed — dynamic widget keys (_year_widget_key / _quarter_widget_key)
        # change on the next render (they include company name), so Streamlit creates
        # fresh widgets that use index= instead of restoring stale frontend values.

        # NOTE: Do NOT call save_earnings_calls_state() here.
        # components.html() (used by save_to_local_storage_js) called inside a Streamlit
        # callback creates a stCustomComponentV1 iframe that Streamlit inserts at position
        # [0] in the DOM tree — BEFORE the custom header — causing the ~58px blank strip.
        # State is saved at the end of render_earnings_calls() instead.

    def on_year_change():
        ticker = st.session_state.get('ec_company_select', st.session_state.get('ec_company', 'ALL'))
        # Read from the dynamic key captured in the closure
        year = st.session_state.get(_year_widget_key, st.session_state.get('ec_year', 'ALL'))
        st.session_state.ec_year = year

        if ticker != 'ALL' and str(year) != 'ALL':
            if ticker in _non_sec_only_tickers:
                _yq = EarningsCallRepository.get_non_sec_transcript_years_and_quarters(ticker)
            else:
                _yq = EarningsCallRepository.get_years_and_quarters(ticker)
            yr_key = int(year) if str(year).isdigit() else None
            quarters = _yq.get(yr_key, []) if yr_key and _yq else []
            q_opts = ['ALL'] + (sorted(quarters) if quarters else ["Q1", "Q2", "Q3", "Q4"])
        else:
            q_opts = ['ALL', 'Q1', 'Q2', 'Q3', 'Q4']

        st.session_state.ec_quarter = q_opts[-1] if len(q_opts) > 1 else q_opts[0]
        st.session_state._ec_quarter_user_set = False
        # No pop needed — _quarter_widget_key changes (includes year) so Streamlit
        # creates a fresh widget using index= on next render.

        # NOTE: Do NOT call save_earnings_calls_state() here — see on_company_change comment.

    def on_quarter_change():
        # Read from the dynamic key captured in the closure
        quarter = st.session_state.get(_quarter_widget_key)
        if quarter is not None:
            st.session_state.ec_quarter = quarter
            st.session_state._ec_quarter_user_set = True
            ticker = st.session_state.get('ec_company', 'ALL')
            year = st.session_state.get('ec_year', 'ALL')
        # NOTE: Do NOT call save_earnings_calls_state() here — see on_company_change comment.

    if "ec_calls_active_watchlist_id" not in st.session_state:
        st.session_state.ec_calls_active_watchlist_id = None
    if "ec_calls_active_watchlist_name" not in st.session_state:
        st.session_state.ec_calls_active_watchlist_name = ""

    _ec_calls_user_email = get_current_user() or ""
    try:
        # All active watchlists are visible to every user (the service filters by
        # permission flags, not ownership), so load unconditionally. This also makes
        # the dropdown populate when auth is bypassed locally (empty email still
        # returns the full list) — fixes the "No watchlists found" case.
        _ec_calls_watchlists = get_user_watchlists(_ec_calls_user_email)
    except Exception as _ec_wl_exc:
        log_structured_error(_ec_wl_exc, page="earnings_calls", component="render_earnings_calls",
                             operation="load_watchlists", context=f"user={_ec_calls_user_email}")
        _ec_calls_watchlists = []

    _ec_calls_no_wl_label = "— No watchlist —"
    _ec_calls_wl_options = [_ec_calls_no_wl_label] + [
        f"{wl['name']} ({wl.get('company_count', 0)} companies)"
        for wl in _ec_calls_watchlists
    ]
    _ec_calls_wl_ids: List[Optional[int]] = [None] + [wl["id"] for wl in _ec_calls_watchlists]
    _ec_calls_wl_names: List[str] = [""] + [wl["name"] for wl in _ec_calls_watchlists]

    _ec_calls_cur_wl_id = st.session_state.ec_calls_active_watchlist_id
    if _ec_calls_cur_wl_id is not None and _ec_calls_cur_wl_id not in _ec_calls_wl_ids:
        st.session_state.ec_calls_active_watchlist_id = None
        st.session_state.ec_calls_active_watchlist_name = ""
        st.session_state.pop("ec_calls_watchlist_filter", None)
        _ec_calls_cur_wl_id = None
    _ec_calls_wl_default_idx = (
        _ec_calls_wl_ids.index(_ec_calls_cur_wl_id)
        if _ec_calls_cur_wl_id in _ec_calls_wl_ids
        else 0
    )

    def _on_ec_calls_watchlist_change():
        try:
            _sel = st.session_state.get("ec_calls_watchlist_filter", "")
            if not _sel or _sel == _ec_calls_no_wl_label or _sel not in _ec_calls_wl_options:
                st.session_state.ec_calls_active_watchlist_id = None
                st.session_state.ec_calls_active_watchlist_name = ""
            else:
                _idx = _ec_calls_wl_options.index(_sel)
                st.session_state.ec_calls_active_watchlist_id = _ec_calls_wl_ids[_idx]
                st.session_state.ec_calls_active_watchlist_name = _ec_calls_wl_names[_idx]
        except Exception as _wl_cb_exc:
            log_structured_error(_wl_cb_exc, page="earnings_calls", component="_on_ec_calls_watchlist_change",
                                 operation="watchlist_change_callback",
                                 context=f"label={st.session_state.get('ec_calls_watchlist_filter', '')}")

    # =======================================================================
    # PAGE TITLE — Same pattern as newsroom
    # =======================================================================
    st.markdown("""
    <div style="margin: 24px 0 8px 0;">
        <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 24px; color: #d62e2f; letter-spacing: 1px;">CORESIGHT MARKET DATA</div>
        <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 28px; color: #323232;">Earnings Calls</div>
    </div>
    """, unsafe_allow_html=True)

    # =======================================================================
    # WATCHLIST SCOPE (transcript search only) — a single selector placed ABOVE
    # the search box. Shown ONLY when Company, Year and Quarter are all "ALL"
    # (true cross-transcript search). Restricting to a watchlist focuses the
    # keyword search on just those companies (and runs faster).
    # =======================================================================
    _ec_pre_company = st.session_state.get("ec_company", "ALL")
    _ec_pre_year = str(st.session_state.get("ec_year", "ALL"))
    _ec_pre_quarter = st.session_state.get("ec_quarter", "ALL")
    _ec_show_watchlist = (
        _ec_pre_company == "ALL"
        and _ec_pre_year == "ALL"
        and _ec_pre_quarter == "ALL"
        and bool(_ec_calls_watchlists)
    )
    if _ec_show_watchlist:
        _wl_col, _wl_spacer = st.columns([3, 7], gap="small")
        with _wl_col:
            st.selectbox(
                "Watchlist",
                options=_ec_calls_wl_options,
                index=_ec_calls_wl_default_idx,
                key="ec_calls_watchlist_filter",
                on_change=_on_ec_calls_watchlist_change
            )

    # =======================================================================
    # FILTER ROW — Search | Company | Year | Quarter
    # =======================================================================
    search_col, company_col, year_col, quarter_col = st.columns([3, 5, 1, 1], gap="small")

    with search_col:
        search_term = st.text_input(
            "Search",
            placeholder="eg., revenue, AWS, guidance...",
            value=st.session_state.get('ec_search', ''),
            key="ec_search_input",
            help="Enter a keyword to search across all matching transcripts",
        )
        st.session_state.ec_search = search_term

    with company_col:
        company = st.selectbox(
            "Company",
            options=tickers,
            format_func=lambda x: next((opt[1] for opt in company_options if opt[0] == x), x),
            index=tickers.index(st.session_state.ec_company),
            key="ec_company_select",
            on_change=on_company_change,
        )

    with year_col:
        year = st.selectbox(
            "Year",
            options=year_options,
            index=year_options.index(st.session_state.ec_year),
            key=_year_widget_key,
            on_change=on_year_change,
        )

    with quarter_col:
        quarter = st.selectbox(
            "Quarter",
            options=quarter_options,
            index=quarter_options.index(st.session_state.ec_quarter),
            key=_quarter_widget_key,
            on_change=on_quarter_change,
        )

    _watchlist_scope_enabled = (
        company == 'ALL'
        and str(year) == 'ALL'
        and quarter == 'ALL'
    )

    _active_watchlist_id = st.session_state.ec_calls_active_watchlist_id
    _active_watchlist_name = st.session_state.ec_calls_active_watchlist_name
    _active_watchlist_tickers: Optional[Tuple[str, ...]] = None
    _active_watchlist_company_count = 0
    _applied_watchlist_id = _active_watchlist_id if _watchlist_scope_enabled else None
    if _applied_watchlist_id is not None:
        try:
            _active_watchlist_rows = get_watchlist_companies(_applied_watchlist_id)
        except Exception as _ec_wl_comp_exc:
            log_structured_error(_ec_wl_comp_exc, page="earnings_calls", component="render_earnings_calls",
                                 operation="get_watchlist_companies",
                                 context=f"watchlist_id={_applied_watchlist_id}")
            _active_watchlist_rows = []
        _active_watchlist_tickers = _watchlist_rows_to_tickers(_active_watchlist_rows)
        _active_watchlist_company_count = len(_active_watchlist_tickers)

    # =======================================================================
    # DETECT MODE: cross-transcript search vs single-transcript view
    # =======================================================================
    is_cross_search = (company == 'ALL' or str(year) == 'ALL' or quarter == 'ALL')
    _is_viewing_non_sec = (company in _non_sec_only_tickers and not is_cross_search)

    # =======================================================================
    # FETCH TRANSCRIPT DATA (single-transcript mode only)
    # =======================================================================
    earnings_calls = []
    _non_sec_pdf_path = None  # local path for NON-SEC transcript PDF
    _ns_blob_name = None  # blob name for NON-SEC transcript PDF (resolved lazily)
    if _tracker: _tracker.step_start("FETCH_TRANSCRIPT")
    if _is_viewing_non_sec:
        from utils.azure_blob import local_cache_path_for_blob as _local_cache_path_for_blob, ensure_local_blob_for_transcript as _ensure_local_blob_optimized
        # NON-SEC: Build blob path, check local cache immediately (sub-ms).
        # Azure download (if needed) is deferred to right-column rendering so
        # the left column's search panel renders without blocking.
        _ns_blob_name = f"{company}/{year}/transcript-{quarter}/transcript.pdf"
        _ns_local = _local_cache_path_for_blob(_ns_blob_name)
        if _ns_local and os.path.exists(_ns_local):
            _non_sec_pdf_path = _ns_local
        # else: _non_sec_pdf_path stays None → resolved inside right_col below
    elif not is_cross_search:
        # Use prefetched result if params match; fall back to serial otherwise
        if (_transcript_future is not None
            and company == _pf_company
            and str(year) == str(_pf_year)
            and quarter == _pf_quarter):
            try:
                earnings_calls = _transcript_future.result(timeout=30)
            except Exception as _pf_err:
                log_error(f"[EC] Prefetch future failed for {company} {year} {quarter}: {_pf_err}")
                earnings_calls = EarningsCallRepository.get_earnings_calls(
                    ticker=company, year=year, quarter=quarter
                )
        else:
            if _transcript_future is not None:
                _transcript_future.cancel()
            earnings_calls = EarningsCallRepository.get_earnings_calls(
                ticker=company,
                year=year,
                quarter=quarter
            )
    elif _transcript_future is not None:
        _transcript_future.cancel()
        if _edate_future is not None:
            _edate_future.cancel()

    # Get company display name
    if _tracker: _tracker.step_end("FETCH_TRANSCRIPT")
    company_display = next((opt[1] for opt in company_options if opt[0] == company), company)
    company_name = company_display.split('(')[0].strip() if '(' in company_display else company_display


    # =======================================================================
    # TWO-COLUMN LAYOUT: Search (Left) + Transcript (Right)
    # =======================================================================
    if _tracker: _tracker.step_start("RENDER_CONTENT")
    left_col, right_col = st.columns([0.3, 0.7])

    # ── Parse transcript for single-transcript search ──
    transcript_text = None
    segments = []
    earnings_event = None
    report_date = None
    fiscal_period_end_date = None
    fiscal_period_label = ""
    if not is_cross_search and earnings_calls and len(earnings_calls) > 0:
        transcript = earnings_calls[0]
        transcript_text = transcript.transcript_text
        if transcript_text:
            segments = parse_transcript(transcript_text)
        # Announcement + period-end from AV earnings history when possible; else NASDAQ/YF calendar (SEC tickers).
        if company not in _non_sec_only_tickers:
            try:
                _q_int = int(str(quarter).replace("Q", "")) if quarter and str(quarter) not in ("ALL", "") else None
                _yr_int = int(year) if year and str(year) not in ("ALL", "") else None
                if _q_int and _yr_int:
                    if (_edate_future is not None
                        and company == _pf_company
                        and str(year) == str(_pf_year)
                        and quarter == _pf_quarter):
                        try:
                            earnings_event = _edate_future.result(timeout=10)
                        except Exception as _ef_err:
                            log_error(f"[EC] Earnings event future failed for {company}: {_ef_err}")
                            earnings_event = EarningsCalendarRepository.get_earnings_event_dates(
                                company, _q_int, _yr_int
                            )
                    else:
                        earnings_event = EarningsCalendarRepository.get_earnings_event_dates(
                            company, _q_int, _yr_int
                        )
                    if earnings_event:
                        report_date = earnings_event.get("report_date")
                        fiscal_period_end_date = earnings_event.get("fiscal_period_end_date")
                    if report_date:
                        _display_period = EarningsCalendarRepository.get_earnings_display_period(
                            company,
                            report_date,
                            source_q=_q_int,
                            source_year=_yr_int,
                        )
                        if _display_period and _display_period.get("fiscal_q") and _display_period.get("report_fiscal_year"):
                            fiscal_period_label = (
                                f"Q{_display_period['fiscal_q']} "
                                f"FY{_display_period['report_fiscal_year']}"
                            )
            except Exception as _edate_err:
                log_error(f"[EC] Earnings date lookup failed for {company} {year} {quarter}: {_edate_err}")

    # active_keyword comes from the search input in the filter row above
    active_keyword = search_term.strip() if search_term and search_term.strip() else None

    # ── LEFT COLUMN: Search Results Panel ──
    with left_col:
        search_label = "Search All Transcripts" if is_cross_search else "Search Transcript"
        search_icon = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#D62E2F" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'

        # Cross-transcript pagination — a subtle "Show more" text link sits at the
        # BOTTOM of the results list, inside this box, so it only appears once the
        # user scrolls past the loaded items. Offset pagination fetches only the
        # NEXT page each time and appends it, so no match is ever skipped and
        # already-loaded transcripts are never re-fetched.
        _CROSS_PAGE = 15

        # Captured inside the box, consumed by the Excel export rendered BELOW it.
        cross_results = []
        _single_matches = []

        with st.container(border=True, height=520):
            st.markdown(f'<div class="transcript-search-header">{search_icon}<span class="transcript-search-title">{search_label}</span></div>', unsafe_allow_html=True)

            if is_cross_search:
                # ── CROSS-TRANSCRIPT SEARCH MODE ──
                if active_keyword:
                    # Reset accumulator on a new search (keyword/filters/watchlist);
                    # load the first page. Results are kept newest-first (the DB
                    # orders year DESC, quarter DESC, id DESC).
                    _cross_sig = (active_keyword, company, str(year), quarter, _applied_watchlist_id)
                    if st.session_state.get("ec_cross_sig") != _cross_sig:
                        _page, _raw = _get_cross_search_results(
                            active_keyword, company, str(year), quarter,
                            _applied_watchlist_id, _active_watchlist_tickers, _CROSS_PAGE, 0,
                        )
                        st.session_state.ec_cross_sig = _cross_sig
                        st.session_state.ec_cross_acc = _page
                        st.session_state.ec_cross_offset = _CROSS_PAGE
                        st.session_state.ec_cross_has_more = (_raw == _CROSS_PAGE)
                    cross_results = list(st.session_state.get("ec_cross_acc", []))
                    _cross_has_more = bool(st.session_state.get("ec_cross_has_more", False))
                    if _applied_watchlist_id is not None:
                        st.caption(
                            f"Filtering by watchlist: **{_active_watchlist_name}** — "
                            f"{_active_watchlist_company_count} compan"
                            f"{'y' if _active_watchlist_company_count == 1 else 'ies'}"
                        )
                    if cross_results:
                        _cross_count_html = (
                            f'Showing <b>{len(cross_results)}</b> results for "<b>{active_keyword}</b>" — newest first'
                            if _cross_has_more else
                            f'Found <b>{len(cross_results)}</b> match'
                            f'{"es" if len(cross_results) != 1 else ""} for "<b>{active_keyword}</b>" — newest first'
                        )
                        st.markdown(
                            f'<div class="transcript-search-count">{_cross_count_html}</div>',
                            unsafe_allow_html=True
                        )
                        # Excel export button/download is rendered BELOW this box
                        # (see "EXCEL EXPORT — below the box" block after the container).
                        for r in cross_results:
                            highlighted_snippet = _highlight_keyword(r['snippet'], active_keyword)
                            view_url = (
                                f"/earnings_calls?ticker={r['ticker']}"
                                f"&year={r['year']}&quarter={r['quarter']}"
                                f"&highlight={active_keyword}"
                            )
                            card_html = f'''
                            <div class="transcript-search-result-card">
                                <div class="transcript-search-speaker">{r['speaker']}</div>
                                <div class="transcript-search-snippet">{highlighted_snippet}</div>
                                <div class="transcript-result-footer">
                                    <span class="transcript-result-meta">{ticker_display.get(r['ticker'], r['ticker'])} &bull; {r['year']} &bull; {r['quarter']}</span>
                                    <a href="{view_url}" target="_self" class="transcript-view-btn">View &#8594;</a>
                                </div>
                            </div>
                            '''
                            st.markdown(card_html, unsafe_allow_html=True)
                        if _cross_has_more:
                            _sm_l, _sm_c, _sm_r = st.columns([1, 2, 1])
                            with _sm_c:
                                if st.button("Show more ↓", key="ec_cross_load_more",
                                             type="tertiary", width="stretch"):
                                    _off = int(st.session_state.get("ec_cross_offset", _CROSS_PAGE))
                                    _page, _raw = _get_cross_search_results(
                                        active_keyword, company, str(year), quarter,
                                        _applied_watchlist_id, _active_watchlist_tickers,
                                        _CROSS_PAGE, _off,
                                    )
                                    st.session_state.ec_cross_acc = (
                                        list(st.session_state.get("ec_cross_acc", [])) + _page
                                    )
                                    st.session_state.ec_cross_offset = _off + _CROSS_PAGE
                                    st.session_state.ec_cross_has_more = (_raw == _CROSS_PAGE)
                                    st.rerun()
                    else:
                        st.markdown(
                            f'<div class="transcript-search-placeholder">No matches found for "<b>{active_keyword}</b>"</div>',
                            unsafe_allow_html=True
                        )
                else:
                    st.markdown(
                        '<div class="transcript-search-placeholder">Enter a keyword to search across all matching transcripts</div>',
                        unsafe_allow_html=True
                    )

            elif _is_viewing_non_sec:
                # ── NON-SEC TRANSCRIPT PDF MODE ──
                if active_keyword and _non_sec_pdf_path and os.path.exists(_non_sec_pdf_path):
                    _pdf_pages = _extract_pdf_text_by_page(_non_sec_pdf_path)
                    _pdf_matches = _search_pdf_pages(_pdf_pages, active_keyword)
                    if _pdf_matches:
                        st.markdown(
                            f'<div class="transcript-search-count">Found <b>{len(_pdf_matches)}</b> '
                            f'page{"s" if len(_pdf_matches) != 1 else ""} with "<b>{active_keyword}</b>"</div>',
                            unsafe_allow_html=True,
                        )
                        _cards_html = []
                        for _pm in _pdf_matches[:30]:
                            _hs = _highlight_keyword(_pm['snippet'], active_keyword)
                            _view_url = (
                                f"/earnings_calls?ticker={company}&year={year}"
                                f"&quarter={quarter}&pdf_page={_pm['page']}"
                                f"&keyword={active_keyword}"
                            )
                            _cards_html.append(f'''
                            <div class="transcript-search-result-card">
                                <div class="transcript-search-speaker">Page {_pm['page']}</div>
                                <div class="transcript-search-snippet">{_hs}</div>
                                <div class="transcript-result-footer">
                                    <span class="transcript-result-meta">{ticker_display.get(company, company)} &bull; {year} &bull; {quarter}</span>
                                    <a href="{_view_url}" target="_self" class="transcript-view-btn">View &#8594;</a>
                                </div>
                            </div>
                            ''')
                        st.markdown(''.join(_cards_html), unsafe_allow_html=True)
                    else:
                        st.markdown(
                            f'<div class="transcript-search-placeholder">No matches found for "<b>{active_keyword}</b>"</div>',
                            unsafe_allow_html=True,
                        )
                elif active_keyword and not (_non_sec_pdf_path and os.path.exists(_non_sec_pdf_path)):
                    st.markdown(
                        '<div class="transcript-search-placeholder">PDF not yet loaded — results will appear once the document downloads.</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<div class="transcript-search-placeholder">'
                        'Type a keyword above to search within the PDF transcript'
                        '</div>',
                        unsafe_allow_html=True,
                    )

            elif active_keyword and segments:
                # ── SINGLE-TRANSCRIPT SEARCH MODE ──
                matches = []
                for i, seg in enumerate(segments):
                    if active_keyword.lower() in seg['text'].lower():
                        idx = seg['text'].lower().find(active_keyword.lower())
                        start = max(0, idx - 40)
                        end = min(len(seg['text']), idx + len(active_keyword) + 40)
                        snippet = seg['text'][start:end]
                        if start > 0:
                            snippet = '...' + snippet
                        if end < len(seg['text']):
                            snippet = snippet + '...'
                        matches.append({
                            'ticker': company,
                            'year': str(year),
                            'quarter': quarter,
                            'speaker': seg['speaker'],
                            'snippet': snippet,
                            'paragraph': seg['text'],
                            'index': i,
                        })

                if matches:
                    _single_matches = matches
                    st.markdown(f'<div class="transcript-search-count">Found {len(matches)} match{"es" if len(matches) != 1 else ""} for "<b>{active_keyword}</b>"</div>', unsafe_allow_html=True)
                    # Excel export is rendered BELOW this box (after the container).
                    for m in matches[:30]:
                        highlighted_snippet = _highlight_keyword(m['snippet'], active_keyword)
                        card_html = f'''
                        <div class="transcript-search-result-card">
                            <div class="transcript-search-speaker">{m['speaker']}</div>
                            <div class="transcript-search-snippet">{highlighted_snippet}</div>
                            <div class="transcript-result-footer">
                                <span class="transcript-result-meta">{ticker_display.get(company, company)} &bull; {year} &bull; {quarter}</span>
                                <a href="#seg-{m['index']}" class="transcript-view-btn">View &#8594;</a>
                            </div>
                        </div>
                        '''
                        st.markdown(card_html, unsafe_allow_html=True)
                else:
                    st.markdown(f'<div class="transcript-search-placeholder">No matches found for "<b>{active_keyword}</b>"</div>', unsafe_allow_html=True)
            elif active_keyword and not segments:
                st.markdown('<div class="transcript-search-placeholder">No transcript loaded to search</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="transcript-search-placeholder">Type a keyword above to search within the transcript</div>', unsafe_allow_html=True)

        # ===================================================================
        # CSV EXPORT — rendered BELOW the results box (not inside it).
        # Cross-transcript export is heavy (DB windows for every match), so it
        # runs in a BACKGROUND thread: clicking "CSV" submits the build to the
        # pool and the page stays interactive; when it finishes, a Download
        # button appears here. (SIP-style CSV replaced the styled Excel build,
        # which took minutes for 10k rows and whose auto-download iframe was
        # wiped by the 1.2s poller before it could fire — the "never
        # completes" bug seen on STG 03-Jul.)
        # ===================================================================
        if is_cross_search and active_keyword and cross_results:
            _xl_sig_key = repr((active_keyword, company, str(year), quarter, _applied_watchlist_id))
            _xl_filename = (
                f"Earnings_Calls_{_safe_excel_filename_keyword(active_keyword)}"
                "_Keyword_Results.csv"
            )

            _dl_l, _dl_r = st.columns([1, 1])
            with _dl_r:
                _job = _ec_excel_job_get(_xl_sig_key)
                log_timing(
                    "EC_EXPORT_STATE", 0,
                    f"sig={_xl_sig_key} job=" + (
                        "none" if _job is None else
                        ("ready" if _job.get("bytes") else ("error" if _job.get("error") else "building"))
                    ),
                    level="WARNING",
                )
                if _job is None:
                    # ONE click → start the background build + toast. No full-page
                    # rerun loop: the poller fragment below does isolated polling,
                    # so the rest of the page stays fully interactive.
                    if st.button(
                        "▦  CSV",
                        key=f"ec_cross_excel_{hash(_xl_sig_key)}",
                        width="stretch",
                        help="Build a CSV of ALL matching results — runs in the background; a download button appears when it's ready.",
                    ):
                        _kw, _co, _yr, _qt = active_keyword, company, str(year), quarter
                        _wl, _wt, _td = _applied_watchlist_id, _active_watchlist_tickers, dict(ticker_display)
                        _ec_excel_job_start(
                            _xl_sig_key, _xl_filename,
                            lambda: _get_ec_excel_executor().submit(
                                _build_cross_excel_job, _kw, _co, _yr, _qt, _wl, _wt, _td,
                            ),
                        )
                        st.toast("Preparing CSV in the background — a download button appears when it's ready.", icon="⏳")
                        # No st.rerun(): the button click already triggers one rerun,
                        # and the poller fragment below picks up the new job. Calling
                        # st.rerun() here would cancel the toast before it shows.
                elif _job.get("bytes"):
                    # Ready — served by Streamlit's media manager over plain HTTP
                    # (no giant base64 iframe, no fonts.googleapis dependency).
                    st.download_button(
                        "⬇  Download CSV",
                        data=_job["bytes"],
                        file_name=_job.get("filename") or _xl_filename,
                        mime="text/csv",
                        key=f"ec_cross_csv_dl_{hash(_xl_sig_key)}",
                        width="stretch",
                        type="primary",
                    )
                elif _job.get("error"):
                    st.markdown(
                        '<div class="transcript-search-count" style="color:#D62E2F;">'
                        'Could not build the CSV file — please try again.</div>',
                        unsafe_allow_html=True,
                    )

            # Poller fragment: reruns ONLY itself every ~1.2s (page stays
            # responsive — no full-page rerun while building). When a build
            # finishes it marks the job done and triggers ONE full rerun so the
            # static "Download CSV" button above replaces the spinner. No JS
            # auto-click: the old iframe was destroyed by the next fragment
            # rerun before its load event fired, losing the download.
            if any(not j.get("downloaded") for _, j in _ec_excel_jobs_snapshot()):
                @st.fragment(run_every="1.2s")
                def _ec_excel_poller():
                    _ec_excel_jobs_collect()
                    # status line for the CURRENT search's job
                    cur = _ec_excel_job_get(_xl_sig_key)
                    if cur and cur.get("bytes") is None and not cur.get("error"):
                        st.markdown(
                            '<div class="transcript-search-count">'
                            '<span style="display:inline-flex;align-items:center;gap:8px;">'
                            '<span style="width:14px;height:14px;border:2px solid #eee;'
                            'border-top:2px solid #d62e2f;border-radius:50%;'
                            'display:inline-block;animation:ec-spin 0.8s linear infinite;"></span>'
                            'Preparing CSV in the background — keep working; a '
                            'download button appears here when it\'s ready.</span></div>'
                            '<style>@keyframes ec-spin{to{transform:rotate(360deg)}}</style>',
                            unsafe_allow_html=True,
                        )
                    # mark ANY finished job delivered, then one full rerun to
                    # swap the spinner for the persistent download button
                    _any_ready = False
                    for k, j in _ec_excel_jobs_snapshot():
                        if (j.get("bytes") or j.get("error")) and not j.get("downloaded"):
                            _any_ready = True
                            with _EC_EXCEL_JOBS_LOCK:
                                if k in _EC_EXCEL_JOBS:
                                    _EC_EXCEL_JOBS[k]["downloaded"] = True
                    if _any_ready:
                        st.rerun(scope="app")
                _ec_excel_poller()

        elif (not is_cross_search) and active_keyword and _single_matches:
            # Single-transcript export is small/fast — build inline (cached by sig).
            _xl_sig = (active_keyword, company, str(year), quarter, len(_single_matches))
            if st.session_state.get("ec_single_excel_sig") != _xl_sig:
                st.session_state.ec_single_excel_sig = _xl_sig
                st.session_state.ec_single_excel_bytes = _build_keyword_results_csv(
                    _single_matches, ticker_display,
                )
            _xl_bytes = st.session_state.get("ec_single_excel_bytes")
            if _xl_bytes:
                _dl_l, _dl_r = st.columns([1, 1])
                with _dl_r:
                    st.download_button(
                        "▦  CSV",
                        data=_xl_bytes,
                        file_name=f"Earnings_Calls_{_safe_excel_filename_keyword(active_keyword)}_Keyword_Results.csv",
                        mime="text/csv",
                        key=f"ec_single_csv_dl_{hash(_xl_sig)}",
                        width="stretch",
                    )


    # ── RIGHT COLUMN: Transcript Content ──
    with right_col:
        if is_cross_search:
            card_html = render_cross_search_panel(company, str(year), quarter)
            st.markdown(card_html, unsafe_allow_html=True)
        elif _is_viewing_non_sec:
            # Lazy-resolve Azure download here (inside right_col) so left_col
            # renders without blocking when the PDF isn't cached yet.
            if _non_sec_pdf_path is None and _ns_blob_name:
                _non_sec_pdf_path = _ensure_local_blob_optimized(_ns_blob_name)

            if _non_sec_pdf_path:
                # NON-SEC Transcript PDF — render header + PDF.js viewer
                hdr_left, hdr_right = st.columns([5.5, 1.5])
                with hdr_left:
                    st.markdown(_render_transcript_header_html(
                        company_name=company_name,
                        ticker=company,
                        year=year,
                        quarter=quarter,
                    ), unsafe_allow_html=True)
                with hdr_right:
                    try:
                        with open(_non_sec_pdf_path, "rb") as _pf:
                            _ns_pdf_bytes = _pf.read()
                        _render_js_download_button(
                            _ns_pdf_bytes,
                            f"{company}_{year}_{quarter}_Transcript.pdf",
                        )
                    except Exception as _exc:
                        log_structured_error(_exc, page="earnings_calls", component="render_earnings_calls", operation="FORMAT_DATE")
                _render_ec_pdf_viewer(
                    _non_sec_pdf_path,
                    highlight_keyword=active_keyword or "",
                    target_page=_qp_pdf_page,
                )
            else:
                # NON-SEC but PDF not found
                st.markdown(
                    '<div class="empty-state">'
                    '<div class="empty-state-title">Transcript PDF Not Available</div>'
                    '<div class="empty-state-text">No transcript PDF found for this company, year, and quarter.</div>'
                    '</div>',
                    unsafe_allow_html=True,
                )
        elif transcript_text:
            # Generate PDF for download button
            _pdf_bytes = None
            _pdf_filename = f"{company}_{year}_{quarter}_Earnings_Transcript.pdf"
            try:
                from utils.transcript_pdf import generate_transcript_pdf
                _pdf_meta_parts: List[str] = []
                if report_date:
                    _pdf_meta_parts.append(f"Report {_format_ec_display_date(report_date)}")
                if fiscal_period_end_date and not _ec_date_equals(fiscal_period_end_date, report_date):
                    _pdf_meta_parts.append(f"QE {_format_ec_display_date(fiscal_period_end_date)}")
                _pdf_date_line = " · ".join(_pdf_meta_parts) if _pdf_meta_parts else ""
                # Memoize per transcript identity. This ran on EVERY rerun of the
                # single-transcript view (hot path) and regenerating the whole PDF
                # dominated the page load. Bounded to the last 8 transcripts so the
                # base64 PDF blobs can't accumulate unbounded in session_state.
                _pdf_cache = st.session_state.setdefault("_ec_pdf_cache", {})
                _pdf_ck = (company, str(year), str(quarter), _pdf_date_line)
                _pdf_bytes = _pdf_cache.get(_pdf_ck)
                if _pdf_bytes is None:
                    _pdf_bytes = generate_transcript_pdf(
                        company_name=company_name,
                        ticker=company,
                        year=str(year),
                        quarter=str(quarter),
                        segments=segments,
                        earnings_date=_pdf_date_line or None,
                    )
                    if len(_pdf_cache) > 8:
                        _pdf_cache.clear()
                    _pdf_cache[_pdf_ck] = _pdf_bytes
            except Exception as _pdf_err:
                log_error(f"[EC] PDF generation failed for {company} {year} {quarter}: {_pdf_err}")
                _pdf_bytes = None

            # Split card: header row (columns) + body HTML
            hdr_left, hdr_right = st.columns([5.5, 1.5])
            with hdr_left:
                st.markdown(_render_transcript_header_html(
                    company_name=company_name,
                    ticker=company,
                    year=year,
                    quarter=quarter,
                    report_date=report_date,
                    fiscal_period_end_date=fiscal_period_end_date,
                    fiscal_period_label=fiscal_period_label,
                ), unsafe_allow_html=True)
            with hdr_right:
                if _pdf_bytes:
                    _render_js_download_button(_pdf_bytes, _pdf_filename)

            card_html = render_transcript_card(
                company_name=company_name,
                ticker=company,
                year=year,
                quarter=quarter,
                transcript_text=transcript_text,
                keyword=active_keyword,
                earnings_date=report_date,
            )
            st.markdown(card_html, unsafe_allow_html=True)
        else:
            st.markdown(render_empty_state(), unsafe_allow_html=True)

    # (no wrapper divs to close — using newsroom's flat layout pattern)

    # Clear the early loading placeholder now that content is rendered
    _ec_loading_hint.empty()

    # Save state AFTER all widgets are rendered so that save_to_local_storage_js()
    # (which calls components.html()) places its stCustomComponentV1 iframe at the
    # BOTTOM of the DOM, not at position [0] where it would push the header down.
    save_earnings_calls_state()

    # Log total page render time
    if _tracker: _tracker.step_end("RENDER_CONTENT")
    _t1_page_end = _time.perf_counter()
    _total_page_time = (_t1_page_end - _t0_page_start) * 1000
    if _tracker: _tracker.finish()
    log_info(f"[TIMING] EC_PAGE_TOTAL | {_total_page_time:.1f}ms | company={company} year={year} quarter={quarter}")
    log_render_complete("earnings_calls", _t1_page_end - _t0_page_start)


def main():
    """Earnings calls page entry point (standalone)."""
    try:
        render_styles()
        st.set_page_config(page_title="Earnings Calls", layout="wide")
        set_page_layout(
            header_full_width=True,
            footer_full_width=True,
            body_padding="0 20px",
            max_content_width="1440px",
            remove_top_padding=True,
            footer_at_bottom=True,
        )

        # Render Header — resolve active_ticker for nav links
        # Check the widget key first (Streamlit updates widget session_state BEFORE rerun)
        _widget_company = st.session_state.get("ec_company_select", None)
        if _widget_company and _widget_company != "All":
            active_ticker = _widget_company
            st.session_state.active_ticker = active_ticker
        else:
            # Get ticker from URL or session, with DB validation and fallback
            _url_ticker = st.query_params.get("ticker")
            _session_ticker = st.session_state.get("active_ticker")
            active_ticker, _was_fallback = validate_and_get_ticker(
                url_ticker=_url_ticker,
                session_ticker=_session_ticker,
                page_name="earnings_calls"
            )
            if _was_fallback:
                # Update session state to reflect fallback (do NOT rewrite URL — that also
                # triggers the navigation rerun path and can produce the blank-space glitch)
                st.session_state.active_ticker = active_ticker
        # Render Header
        render_header(full_width=True, current_page="earnings_calls", ticker=active_ticker)

        # Render content
        render_earnings_calls(active_ticker)

        # Render Footer
        render_coresight_footer(full_width=True, stick_to_bottom=True)
    except Exception as _exc:
        log_structured_error(_exc, page="earnings_calls", component="main", operation="PAGE_RENDER")
        st.error("An unexpected error occurred. Please refresh the page.")
        st.stop()


main()
