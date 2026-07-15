"""
Homepage - Coresight Research
"""
import streamlit as st
from components.styles import hide_sidebar, render_styles
from components.navigation import render_header, render_coresight_footer
from core.auth_manager import require_auth
from data.repository import CompanyRepository
from utils.local_storage import set_marketdata_tab
from utils.local_storage_manager import set_persistent_state, save_market_data_state
from utils.server_logger import log_structured_error, log_error, error_boundary, new_rerun_id, PageLoadTracker, log_render_complete
import time as _perf_time

new_rerun_id("home")

# require_auth(page="home")
hide_sidebar()

# =============================================================================
# START BACKGROUND FILINGS SCAN (INTELLIGENT - NON-BLOCKING)
# Only scans if cache is insufficient. Skips redundant work for production.
# =============================================================================
import time as _perf_time

# Intelligent BG_SCAN - only runs if needed
try:
    from utils.background_scanner import init_background_scanner, get_scan_progress, get_cache_audit, ensure_cache_purge

    # ONE-TIME PURGE: Must run BEFORE cache audit — regardless of scan decision.
    # If cache is "healthy" but stale (pre-ixbrl update), purge must still clear it.
    ensure_cache_purge()

    # Check cache status after purge (purge clears it → audit will say SCAN needed)
    _audit_start = _perf_time.time()
    _audit = get_cache_audit()
    _audit_end = _perf_time.time()

    if _audit.get('should_scan'):
        _init_result = init_background_scanner(auto_start=True)

    pass  # Background scan logic executed

except Exception as e:
    log_structured_error(e, page="home", component="bg_scan_init", operation="BACKGROUND_SCAN_INIT")

@st.cache_data(ttl=300)
def _load_companies():
    """Fetch companies from database, returns list of (ticker, name) tuples."""
    try:
        rows = CompanyRepository.get_companies()
        return [(r['ticker'], r['name']) for r in rows]
    except Exception as _exc:
        log_structured_error(_exc, page="home", component="_load_companies", operation="DB_FETCH")
        return []

try:
    COMPANIES = _load_companies()
except Exception as _exc:
    log_structured_error(_exc, page="home", component="module_init", operation="LOAD_COMPANIES")
    COMPANIES = []
SECTORS = ["Apparel & Footwear", "Department Stores", "Discount Stores", "Luxury Goods"]

def main():
  _page_start = _perf_time.perf_counter()
  _tracker = PageLoadTracker("home")
  try:
    import time as _time
    _t0_page = _time.perf_counter()
    if 'home_company' not in st.session_state:
        st.session_state.home_company = None
    if 'home_sector' not in st.session_state:
        st.session_state.home_sector = SECTORS[0]

    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&family=Montserrat:wght@400;500;600;700&display=swap');

    .block-container { padding: 0 !important; max-width: 100% !important; }
    .appview-container .main .block-container { padding-top: 0 !important; }
    [data-testid="stSidebar"] { display: none !important; }

    .main-title {
        font-family: 'Montserrat', sans-serif !important;
        font-weight: 700 !important;
        font-size: 39px !important;
        color: #D62E2F !important;
        text-align: center !important;
        letter-spacing: -0.5px !important;
        margin: 96px 0 32px 0 !important;
    }

    /* Card wrapper - gray background for columns */
    [data-testid="stColumn"]:nth-of-type(2) > div,
    [data-testid="stColumn"]:nth-of-type(3) > div {
        background-color: #EBEBEB !important;
        border-radius: 8px !important;
        padding: 8px 16px 28px 16px !important;
        width: 357px !important;
        margin: 0 auto !important;
    }

    /* Streamlit Selectbox Styling - White background */
    div[data-testid="stSelectbox"] {
        margin-bottom: 8px !important;
    }

    div[data-testid="stSelectbox"] > label { display: none !important; }

    /* Force white background on selectbox */
    div[data-testid="stSelectbox"] > div,
    div[data-testid="stSelectbox"] > div > div,
    div[data-testid="stSelectbox"] div[data-baseweb="select"],
    div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
        background-color: #FFFFFF !important;
    }

    /* The actual input/control */
    div[data-testid="stSelectbox"] div[data-baseweb="select"] {
        border: 1px solid #e0e0e0 !important;
        border-radius: 8px !important;
        min-height: 40px !important;
        height: 40px !important;
        padding-bottom: 40px !important;
    }

    /* Hover and focus states */
    div[data-testid="stSelectbox"] div[data-baseweb="select"]:hover,
    div[data-testid="stSelectbox"] div[data-baseweb="select"]:focus-within {
        border-color: #D62E2F !important;
        box-shadow: 0 0 0 1px #D62E2F !important;
    }

    /* Text styling */
    div[data-testid="stSelectbox"] span {
        font-family: 'Roboto', sans-serif !important;
        font-weight: 400 !important;
        font-size: 14px !important;
        color: #000000 !important;
    }

    /* Dropdown icon */
    div[data-testid="stSelectbox"] svg { color: #666666 !important; }

    /* View button - red filled style for col1 (View by Company) */
    [data-testid="stColumn"]:nth-of-type(2) div[data-testid="stButton"] > button {
        background-color: #D62E2F !important;
        color: white !important;
        width: 100% !important;
        height: 41px !important;
        font-family: 'Montserrat', sans-serif !important;
        font-weight: 700 !important;
        font-size: 16px !important;
        border-radius: 8px !important;
        border: none !important;
    }
    [data-testid="stColumn"]:nth-of-type(2) div[data-testid="stButton"] > button:hover {
        background-color: #B71C1C !important;
    }
    </style>
    """, unsafe_allow_html=True)

    render_styles()
    render_header(full_width=True, current_page="home")

    # Main title
    st.markdown('<h1 class="main-title">CORESIGHT MARKET DATA</h1>', unsafe_allow_html=True)

    # Two cards side by side
    col_spacer1, col1, col2, col_spacer2 = st.columns([1, 2, 2, 1])

    # Card 1: View by Company
    with col1:
        st.markdown('<p style="font-family: Roboto, sans-serif; font-weight: 600; font-size: 18px; color: #2D2A29; text-align: center; margin: 0 0 8px 0;">View by Company</p>', unsafe_allow_html=True)

        company = st.selectbox("Company", options=[c[0] for c in COMPANIES],
            format_func=lambda x: next((c[1] for c in COMPANIES if c[0] == x), x),
            index=None,
            placeholder="Select a Company",
            key="company_select", label_visibility="collapsed")
        st.session_state.home_company = company
        # Write to shared active_ticker for cross-page synchronization
        if company:
            st.session_state.active_ticker = company
        # No need to save ticker to local storage - it will be passed via query params
        save_market_data_state()
        if company:
            if st.button("View  ↗", key="view_company_btn", width="stretch"):
                st.session_state.selected_tab_market_data = "company_profile"
                set_marketdata_tab("company_profile")
                st.switch_page("pages/market_data.py")
        else:
            st.markdown('''
                <div style="background-color: #cccccc; color: white; font-family: Montserrat, sans-serif; font-weight: 700; font-size: 16px; border-radius: 8px; padding: 8px 16px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; width: 100%; height: 41px; box-sizing: border-box; cursor: not-allowed;">
                    View
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2">
                        <path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>
                        <path d="M14 4h6v6"/>
                        <path d="M21 3 12 12"/>
                    </svg>
                </div>
            ''', unsafe_allow_html=True)

    # Card 2: View by Sector (Coming Soon - disabled)
    with col2:
        st.markdown('<p style="font-family: Roboto, sans-serif; font-weight: 600; font-size: 18px; color: #2D2A29; text-align: center; margin: 0 0 8px 0;">View by Sector</p>', unsafe_allow_html=True)

        st.selectbox("Sector", options=SECTORS,
            index=None,
            placeholder="Select a Sector",
            disabled=True,
            key="sector_select", label_visibility="collapsed")

        st.markdown('''
            <style>
            .sector-btn-wrapper { position: relative; width: 100%; display: inline-block; }
            .sector-coming-soon-tip {
                visibility: hidden;
                background: rgba(50,50,50,0.85);
                color: #fff;
                text-align: center;
                padding: 5px 12px;
                border-radius: 4px;
                position: absolute;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%);
                font-size: 13px;
                font-family: Roboto, sans-serif;
                white-space: nowrap;
                pointer-events: none;
                z-index: 999;
            }
            .sector-btn-wrapper:hover .sector-coming-soon-tip { visibility: visible; }
            </style>
            <div class="sector-btn-wrapper">
                <div style="background-color: #cccccc; color: white; font-family: Montserrat, sans-serif; font-weight: 700; font-size: 16px; border-radius: 8px; padding: 8px 16px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; width: 100%; height: 41px; box-sizing: border-box; cursor: not-allowed;">
                    View
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2">
                        <path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>
                        <path d="M14 4h6v6"/>
                        <path d="M21 3 12 12"/>
                    </svg>
                </div>
                <span class="sector-coming-soon-tip">Coming Soon</span>
            </div>
        ''', unsafe_allow_html=True)

    st.markdown("<div style='height: 100px;'></div>", unsafe_allow_html=True)
    render_coresight_footer(full_width=True, stick_to_bottom=True)
    _tracker.finish()
    log_render_complete("home", _perf_time.perf_counter() - _page_start)
  except Exception as _exc:
    log_structured_error(_exc, page="home", component="main", operation="PAGE_RENDER")
    st.error("An unexpected error occurred. Please refresh the page.")
    st.stop()

try:
    main()
except Exception as _exc:
    log_structured_error(_exc, page="home", component="main_call", operation="PAGE_RENDER")
    st.error("Something went wrong. Please try again.")

if __name__ == "__main__":
    pass
