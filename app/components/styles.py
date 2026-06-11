"""
Global CSS styles and design tokens.
Pixel-perfect styling matching Figma design specifications.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.server_logger import log_structured_error

# ============================================================================
# DESIGN TOKENS
# ============================================================================

# Color Palette
COLORS = {
    # Primary
    "primary": "#0066CC",
    "primary_hover": "#0052A3",
    "primary_light": "#E6F2FF",

    # Secondary
    "secondary": "#6C757D",
    "secondary_hover": "#5A6268",

    # Semantic Colors
    "success": "#28A745",
    "success_light": "#D4EDDA",
    "warning": "#FFC107",
    "warning_light": "#FFF3CD",
    "danger": "#DC3545",
    "danger_light": "#F8D7DA",
    "info": "#17A2B8",
    "info_light": "#D1ECF1",

    # Neutral Scale
    "white": "#FFFFFF",
    "gray_50": "#F8F9FA",
    "gray_100": "#F1F3F5",
    "gray_200": "#E9ECEF",
    "gray_300": "#DEE2E6",
    "gray_400": "#CED4DA",
    "gray_500": "#ADB5BD",
    "gray_600": "#6C757D",
    "gray_700": "#495057",
    "gray_800": "#343A40",
    "gray_900": "#212529",
    "black": "#000000",

    # Chart Colors
    "chart_primary": "#0066CC",
    "chart_secondary": "#00C49F",
    "chart_tertiary": "#FFBB28",
    "chart_quaternary": "#FF8042",
    "chart_up": "#28A745",
    "chart_down": "#DC3545",
}

# Typography
TYPOGRAPHY = {
    "font_family": "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    "font_family_mono": "'SF Mono', Monaco, 'Cascadia Code', monospace",

    # Font Sizes
    "text_xs": "0.75rem",      # 12px
    "text_sm": "0.875rem",     # 14px
    "text_base": "1rem",       # 16px
    "text_lg": "1.125rem",     # 18px
    "text_xl": "1.25rem",      # 20px
    "text_2xl": "1.5rem",      # 24px
    "text_3xl": "1.875rem",    # 30px
    "text_4xl": "2.25rem",     # 36px

    # Font Weights
    "font_normal": "400",
    "font_medium": "500",
    "font_semibold": "600",
    "font_bold": "700",

    # Line Heights
    "leading_tight": "1.25",
    "leading_snug": "1.375",
    "leading_normal": "1.5",
    "leading_relaxed": "1.625",
}

# Spacing
SPACING = {
    "space_0": "0",
    "space_1": "0.25rem",   # 4px
    "space_2": "0.5rem",    # 8px
    "space_3": "0.75rem",   # 12px
    "space_4": "1rem",      # 16px
    "space_5": "1.25rem",   # 20px
    "space_6": "1.5rem",    # 24px
    "space_8": "2rem",      # 32px
    "space_10": "2.5rem",   # 40px
    "space_12": "3rem",     # 48px
    "space_16": "4rem",     # 64px
}

# Border Radius
BORDER_RADIUS = {
    "rounded_none": "0",
    "rounded_sm": "0.125rem",   # 2px
    "rounded": "0.25rem",       # 4px
    "rounded_md": "0.375rem",   # 6px
    "rounded_lg": "0.5rem",     # 8px
    "rounded_xl": "0.75rem",    # 12px
    "rounded_2xl": "1rem",      # 16px
    "rounded_full": "9999px",
}

# Shadows
SHADOWS = {
    "shadow_sm": "0 1px 2px 0 rgba(0, 0, 0, 0.05)",
    "shadow": "0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px 0 rgba(0, 0, 0, 0.06)",
    "shadow_md": "0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)",
    "shadow_lg": "0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05)",
    "shadow_xl": "0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)",
    # Coresight card shadow — matches main website
    "shadow_card": "0 2px 16px rgba(0, 0, 0, 0.10), 0 1px 4px rgba(0, 0, 0, 0.06)",
}

# ============================================================================
# GLOBAL CSS
# ============================================================================

def get_global_css() -> str:
    """Return global CSS styles — minimal, matching screenshot design."""
    try:
        return f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    /* Root Variables */
    :root {{
        --primary: {COLORS["primary"]};
        --primary-hover: {COLORS["primary_hover"]};
        --primary-light: {COLORS["primary_light"]};
        --success: {COLORS["success"]};
        --danger: {COLORS["danger"]};
        --warning: {COLORS["warning"]};
        --gray-50: {COLORS["gray_50"]};
        --gray-100: {COLORS["gray_100"]};
        --gray-200: {COLORS["gray_200"]};
        --gray-300: {COLORS["gray_300"]};
        --gray-600: {COLORS["gray_600"]};
        --gray-700: {COLORS["gray_700"]};
        --gray-800: {COLORS["gray_800"]};
        --gray-900: {COLORS["gray_900"]};
    }}

    /* Global Reset — white background per screenshot */
    .stApp {{
        font-family: {TYPOGRAPHY["font_family"]};
        background-color: {COLORS["white"]};
    }}

    /* Hide Streamlit Branding */
    #MainMenu {{visibility: hidden;}}
    footer {{visibility: hidden;}}
    /* Remove stHeader from layout entirely (display:none removes its ~58px height;
       visibility:hidden alone would preserve the height and cause blank-space above
       the custom nav on rerun). */
    header[data-testid="stHeader"] {{
        display: none !important;
        height: 0 !important;
        min-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        visibility: hidden !important;
    }}
    header {{visibility: hidden;}}

    /* Custom Scrollbar */
    ::-webkit-scrollbar {{
        width: 8px;
        height: 8px;
    }}

    ::-webkit-scrollbar-track {{
        background: {COLORS["gray_100"]};
    }}

    ::-webkit-scrollbar-thumb {{
        background: {COLORS["gray_400"]};
        border-radius: 4px;
    }}

    ::-webkit-scrollbar-thumb:hover {{
        background: {COLORS["gray_500"]};
    }}

    /* Typography */
    h1, h2, h3, h4, h5, h6 {{
        font-family: {TYPOGRAPHY["font_family"]};
        font-weight: {TYPOGRAPHY["font_semibold"]};
        color: {COLORS["gray_900"]};
        margin-bottom: 1rem;
    }}
    /* Hide Streamlit auto-anchor link icons on all headings */
    h1 a[href], h2 a[href], h3 a[href], h4 a[href], h5 a[href], h6 a[href],
    .stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
    .stMarkdown h4 a, .stMarkdown h5 a, .stMarkdown h6 a {{
        display: none !important;
    }}
    [data-testid="stSidebarCollapsedControl"] {{
        visibility: hidden;
        display: none !important;
    }}

    /* Button Styling */
    .stButton > button {{
        font-family: {TYPOGRAPHY["font_family"]};
        font-weight: {TYPOGRAPHY["font_medium"]};
        border-radius: {BORDER_RADIUS["rounded_lg"]};
        transition: all 0.2s ease;
    }}

    .stButton > button[kind="primary"] {{
        background-color: {COLORS["primary"]};
        border: none;
    }}

    .stButton > button[kind="primary"]:hover {{
        background-color: {COLORS["primary_hover"]};
        transform: translateY(-1px);
        box-shadow: {SHADOWS["shadow_md"]};
    }}

    /* Input Styling */
    .stTextInput > div > div > input,
    .stSelectbox > div > div > select,
    .stDateInput > div > div > input {{
        border-radius: {BORDER_RADIUS["rounded_lg"]};
        border: 1px solid {COLORS["gray_300"]};
        font-family: {TYPOGRAPHY["font_family"]};
    }}

    .stTextInput > div > div > input:focus,
    .stSelectbox > div > div > select:focus,
    .stDateInput > div > div > input:focus {{
        border-color: {COLORS["primary"]};
        box-shadow: 0 0 0 3px rgba(0, 102, 204, 0.1);
    }}

    /* ── Card / Chart Shadows (Coresight website style) ── */

    /* Plotly charts */
    [data-testid="stPlotlyChart"] {{
        border-radius: {BORDER_RADIUS["rounded_xl"]} !important;
        box-shadow: {SHADOWS["shadow_card"]} !important;
        overflow: hidden;
        background: #fff;
    }}

    /* Metric / KPI cards */
    [data-testid="metric-container"] {{
        border-radius: {BORDER_RADIUS["rounded_xl"]} !important;
        box-shadow: {SHADOWS["shadow_card"]} !important;
        background: #fff !important;
        padding: 16px 20px !important;
    }}

    /* st.container(border=True) */
    [data-testid="stVerticalBlockBorderWrapper"] {{
        border-radius: {BORDER_RADIUS["rounded_xl"]} !important;
        box-shadow: {SHADOWS["shadow_card"]} !important;
        border: none !important;
        background: #fff;
        overflow: hidden;
    }}

    /* DataFrames */
    .stDataFrame {{
        border-radius: {BORDER_RADIUS["rounded_xl"]};
        box-shadow: {SHADOWS["shadow_card"]};
        overflow: hidden;
    }}

    /* HTML tables injected via st.markdown (table-container class) */
    .table-container {{
        border-radius: {BORDER_RADIUS["rounded_xl"]} !important;
        box-shadow: {SHADOWS["shadow_card"]} !important;
        overflow: hidden;
    }}

    /* Tabs Styling */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 0;
        border-bottom: 1px solid {COLORS["gray_200"]};
    }}

    .stTabs [data-baseweb="tab"] {{
        padding: 1rem 1.5rem;
        font-weight: {TYPOGRAPHY["font_medium"]};
    }}

    .stTabs [aria-selected="true"] {{
        color: {COLORS["primary"]} !important;
        border-bottom-color: {COLORS["primary"]} !important;
    }}

    /* ═══════════════════════════════════════════════════════════════════════
       SMOOTH CONTENT RENDER — eliminates jank when Streamlit elements pop in
       ═══════════════════════════════════════════════════════════════════════

       Root cause: Streamlit renders elements one-by-one top-to-bottom via
       WebSocket. Each element appears instantaneously causing layout shifts,
       pop-ins and jumps.

       Fix: Every element type gets a fade-in + subtle lift animation.
       CSS nth-child stagger adds 30 ms delay per sibling so elements glide
       in sequentially rather than all jumping at once.
       GPU-composited properties (opacity + transform) ensure zero layout
       thrash — no width/height changes, no reflow.
    ═══════════════════════════════════════════════════════════════════════ */

    /* Core keyframes — composited-only (no reflow)
       IMPORTANT: All "to" states use transform:none (not translateY(0)/scale(1)).
       Any non-none CSS transform creates a new containing block for
       position:fixed children — which breaks Streamlit dropdown positioning. */
    @keyframes cs-rise {{
        from {{ opacity: 0; transform: translateY(10px); }}
        to   {{ opacity: 1; transform: none;             }}
    }}
    @keyframes cs-fade {{
        from {{ opacity: 0; }}
        to   {{ opacity: 1; }}
    }}
    @keyframes cs-scale-in {{
        from {{ opacity: 0; transform: scale(0.97); }}
        to   {{ opacity: 1; transform: none;        }}
    }}

    /* ── Stagger mixin (applied via nth-child) ───────────────────────── */
    /* Every direct child of a vertical block gets a sequential delay     */
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(1)  {{ animation-delay: 0ms;   }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(2)  {{ animation-delay: 30ms;  }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(3)  {{ animation-delay: 60ms;  }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(4)  {{ animation-delay: 90ms;  }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(5)  {{ animation-delay: 120ms; }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(6)  {{ animation-delay: 150ms; }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(7)  {{ animation-delay: 180ms; }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(8)  {{ animation-delay: 200ms; }}
    [data-testid="stVerticalBlock"] > [data-testid="element-container"]:nth-child(n+9) {{ animation-delay: 220ms; }}

    /* ── Element animations ──────────────────────────────────────────── */

    /* Generic content blocks: rise + fade */
    [data-testid="element-container"] {{
        animation: cs-rise 0.38s cubic-bezier(0.22, 1, 0.36, 1) both;
        will-change: opacity, transform;
    }}

    /* Metric / KPI cards: scale-in for tactile pop */
    [data-testid="metric-container"] {{
        animation: cs-scale-in 0.32s cubic-bezier(0.34, 1.56, 0.64, 1) both !important;
        will-change: opacity, transform;
    }}

    /* DataFrames and tables: fade only (no Y-shift, avoids height jump) */
    [data-testid="stDataFrame"],
    [data-testid="stTable"],
    .stDataFrame {{
        animation: cs-fade 0.4s ease both;
        will-change: opacity;
    }}

    /* Charts: rise gently */
    [data-testid="stPlotlyChart"],
    [data-testid="stVegaLiteChart"],
    [data-testid="stArrowVegaLiteChart"] {{
        animation: cs-rise 0.45s cubic-bezier(0.22, 1, 0.36, 1) both;
        will-change: opacity, transform;
    }}

    /* Tabs: fade in — no shift so active tab underline doesn't jump */
    [data-testid="stTabs"] {{
        animation: cs-fade 0.3s ease both;
    }}

    /* Columns: each column rises independently */
    [data-testid="stHorizontalBlock"] > div {{
        animation: cs-rise 0.38s cubic-bezier(0.22, 1, 0.36, 1) both;
        will-change: opacity, transform;
    }}
    [data-testid="stHorizontalBlock"] > div:nth-child(2) {{ animation-delay: 40ms;  }}
    [data-testid="stHorizontalBlock"] > div:nth-child(3) {{ animation-delay: 80ms;  }}
    [data-testid="stHorizontalBlock"] > div:nth-child(4) {{ animation-delay: 120ms; }}
    [data-testid="stHorizontalBlock"] > div:nth-child(5) {{ animation-delay: 160ms; }}
    [data-testid="stHorizontalBlock"] > div:nth-child(6) {{ animation-delay: 200ms; }}

    /* Bordered containers (news cards, panels): scale-in with shadow */
    [data-testid="stVerticalBlockBorderWrapper"] {{
        animation: cs-scale-in 0.35s cubic-bezier(0.22, 1, 0.36, 1) both;
        will-change: opacity, transform;
    }}

    /* Selectboxes / inputs: smooth fade so dropdowns don't flash */
    [data-testid="stSelectbox"],
    [data-testid="stTextInput"],
    [data-testid="stDateInput"],
    [data-testid="stMultiSelect"],
    [data-testid="stNumberInput"],
    [data-testid="stSlider"] {{
        animation: cs-fade 0.28s ease both;
    }}

    /* Markdown text blocks: rise */
    [data-testid="stMarkdownContainer"] {{
        animation: cs-rise 0.32s ease both;
        will-change: opacity, transform;
    }}

    /* Expanders: fade */
    [data-testid="stExpander"] {{
        animation: cs-fade 0.3s ease both;
    }}

    /* Spinners: don't animate spinners themselves — they already have motion */
    [data-testid="stSpinner"] {{
        animation: none !important;
    }}

    /* ── Smooth height transitions for dynamic content ───────────────── */
    /* When Streamlit adds/removes elements (e.g. show-results panel),   */
    /* transitioning max-height prevents jump shifts.                    */
    [data-testid="stVerticalBlock"] {{
        transition: height 0.25s ease;
    }}

    /* ── Chart loading shimmer placeholder ───────────────────────────── */
    /* Before Plotly initialises, the container is empty and collapses.  */
    /* min-height reserves space so surrounding content doesn't shift.   */
    [data-testid="stPlotlyChart"]:empty {{
        min-height: 300px;
        background: linear-gradient(90deg, #f0f0f0 25%, #e8e8e8 50%, #f0f0f0 75%);
        background-size: 200% 100%;
        animation: cs-shim-chart 1.4s ease infinite;
        border-radius: 12px;
    }}
    @keyframes cs-shim-chart {{
        0%   {{ background-position: 200% 0; }}
        100% {{ background-position: -200% 0; }}
    }}

    /* ── Spinner → content swap: prevent layout jump ─────────────────── */
    /* Streamlit replaces a spinner with the actual content causing a     */
    /* height change. Transition the parent so the swap is smooth.       */
    [data-testid="stSpinner"] + * {{
        animation: cs-rise 0.3s ease both;
    }}

    /* ── Tab panel swap: fade between tab contents ───────────────────── */
    [data-testid="stTabsContent"] > div {{
        animation: cs-fade 0.25s ease both;
    }}

    /* ── Dataframe inner cells: no layout animation (causes scroll jank) */
    .dvn-scroller,
    .dvn-scroller * {{
        animation: none !important;
        transition: none !important;
    }}

    /* ── Selectbox dropdown menu: NO animation ───────────────────────── */
    /* Streamlit positions popovers via JS transform: translate3d(X,Y,0). */
    /* Any CSS animation on the popover overrides that transform and       */
    /* places the dropdown at (0,0) — top-left corner.  Leave as-is.     */

    /* ── Reduce motion for accessibility ─────────────────────────────── */
    @media (prefers-reduced-motion: reduce) {{
        *, *::before, *::after {{
            animation-duration: 0.01ms !important;
            transition-duration: 0.01ms !important;
        }}
    }}
    </style>
    """
    except Exception as e:
        log_structured_error(e, page="", component="styles", operation="get_global_css")
        return ""


def render_styles():
    """Render global CSS styles in Streamlit."""
    try:
        import streamlit as st
        # Cache the CSS string at module level to avoid regenerating 9KB of CSS
        # on every Streamlit rerun. The CSS is constant (derived from static dicts).
        if '_cached_global_css' not in st.session_state:
            st.session_state['_cached_global_css'] = get_global_css()
        st.markdown(st.session_state['_cached_global_css'], unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="", component="styles", operation="render_styles")
        return


def set_page_layout(
    header_full_width: bool = True,
    footer_full_width: bool = True,
    body_padding: str = "0 20px",
    max_content_width: str = "1350px",
    remove_top_padding: bool = True,
    footer_at_bottom: bool = True
):
    """
    Set adjustable page layout parameters.

    This function allows you to customize the page layout padding and width.
    Call this in your page entry point after hide_sidebar().

    LOCATION: app/components/styles.py

    Parameters:
    -----------
    header_full_width : bool
        If True, header takes full width with no side padding
    footer_full_width : bool
        If True, footer takes full width with no side padding
    body_padding : str
        CSS padding for body content (e.g., "0 20px", "20px", "0")
        Default: "0 20px" (0 top/bottom, 20px left/right)
    max_content_width : str
        Maximum width for content (e.g., "1350px", "100%", "1200px")
        Default: "1350px"
    remove_top_padding : bool
        If True, removes the default top padding from Streamlit
    footer_at_bottom : bool
        If True, footer sticks to bottom of page

    Examples:
    ---------
    # Full width with minimal padding
    set_page_layout(body_padding="0 10px", max_content_width="100%")

    # Centered content with more padding
    set_page_layout(body_padding="20px 40px", max_content_width="1200px")

    # Default behavior
    set_page_layout()  # Uses all defaults
    """
    try:
        import streamlit as st

        css_parts = []

        # Remove top padding and margins
        _pad_lr = body_padding.split()[1] if len(body_padding.split()) > 1 else body_padding
        if remove_top_padding:
            css_parts.append(f"""
            /* Remove default Streamlit top padding */
            .stApp {{
                padding-top: 0 !important;
                margin-top: 0 !important;
            }}
            .main .block-container {{
                padding-top: 0 !important;
                margin-top: 0 !important;
                max-width: {max_content_width} !important;
                padding-left: {_pad_lr} !important;
                padding-right: {_pad_lr} !important;
            }}
            /* Remove padding from main container */
            div[data-testid="stAppViewContainer"] {{
                padding: 0 !important;
            }}
            """)

        # Body content padding
        css_parts.append(f"""
        /* Body content padding - ADJUSTABLE via body_padding parameter */
        .main .block-container > div {{
            padding: {body_padding} !important;
        }}
        """)

        # Footer at bottom
        if footer_at_bottom:
            css_parts.append("""
            /* Footer at bottom - no space below */
            .site-footer {
                margin-bottom: 0 !important;
                padding-bottom: 0 !important;
            }
            """)

        st.markdown("<style>" + "\n".join(css_parts) + "</style>", unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="", component="styles", operation="set_page_layout")
        return


def _build_sidebar_css():
    """Build the sidebar-hiding CSS string (called once, then cached)."""
    return """<style>
    /* Collapse st.markdown wrappers that ONLY contain a <style> tag (no other content) */
    div[data-testid="stElementContainer"]:has(> div[data-testid="stMarkdown"] style:only-child) {
        display: none !important;
        height: 0 !important;
        min-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        line-height: 0 !important;
    }

    /* Hide sidebar immediately on page load - prevents skeleton flash */
    [data-testid="stSidebar"],
    section[data-testid="stSidebar"],
    .stSidebar {
        display: none !important;
        visibility: hidden !important;
        width: 0 !important;
        min-width: 0 !important;
        max-width: 0 !important;
        opacity: 0 !important;
    }
    /* Also hide the sidebar collapse button */
    [data-testid="collapsedControl"] {
        display: none !important;
    }

    /* Remove spacing from sidebar iframe containers only */
    [data-testid="stSidebar"] iframe {
        display: block;
        margin: 0 !important;
        padding: 0 !important;
        height: 0 !important;
        min-height: 0 !important;
    }
    /* Remove stHeader from layout so it cannot create blank space above custom nav */
    header[data-testid="stHeader"] {
        display: none !important;
        height: 0 !important;
        min-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        visibility: hidden !important;
    }
    /* Ensure no top padding on main container */
    .stApp {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    .stAppViewContainer {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    .stMain {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    .block-container {
        padding-top: 0 !important;
        margin-top: 0 !important;
        padding-bottom: 0 !important;
        max-width: 100% !important;
    }


    /* Override Emotion link styles with higher specificity */
    html body .coresight-header-container a,
    html body .coresight-header-container a:link,
    html body .coresight-header-container a:visited,
    html body .coresight-header-container a:active,
    html body [class*="coresight-header"] a,
    html body [class*="coresight-header"] a:link,
    html body [class*="coresight-header"] a:visited,
    html body [class*="coresight-header"] a:active {
        color: inherit !important;
        text-decoration: none !important;
    }
    /* Override any emotion cache link styles for nav links */
    html body [class*="st-emotion-cache"] a.coresight-nav-link {
        color: inherit !important;
        text-decoration: none !important;
    }
    /* Contact Us button - WHITE text with high specificity */
    html body .coresight-contact-btn,
    html body [class*="st-emotion-cache"] a.coresight-contact-btn,
    html body .coresight-header-container a.coresight-contact-btn,
    html body .coresight-header-container a.coresight-contact-btn:link,
    html body .coresight-header-container a.coresight-contact-btn:visited {
        color: #fff !important;
        text-decoration: none !important;
    }
    html body .coresight-contact-btn:hover,
    html body [class*="st-emotion-cache"] a.coresight-contact-btn:hover,
    html body .coresight-header-container a.coresight-contact-btn:hover {
        color: #d62e2f !important;
    }
    /* Remove Emotion style tags from head via CSS (backup) */
    </style>"""


def hide_sidebar():
    """
    Immediately hide the Streamlit sidebar to prevent skeleton flash.

    IMPORTANT: Must be called immediately after st.set_page_config()
    to prevent sidebar skeleton from showing during page load.

    Uses both CSS and JavaScript for maximum compatibility during navigation.
    Also removes top spacing from Streamlit's default elements.

    LOCATION: app/components/styles.py
    """
    try:
        import streamlit as st
        from streamlit.components.v1 import html

        # Cache the sidebar CSS string in session state (constant, no need to rebuild)
        if '_cached_sidebar_css' not in st.session_state:
            st.session_state['_cached_sidebar_css'] = _build_sidebar_css()
        css_styles = st.session_state['_cached_sidebar_css']

        st.markdown(css_styles, unsafe_allow_html=True)

        # JavaScript approach - runs immediately to hide elements, remove Emotion styles, and remove spacing
    except Exception as e:
        log_structured_error(e, page="", component="styles", operation="hide_sidebar")
        return
