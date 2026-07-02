"""
Earnings Calendar Page - Coresight Research
============================================
Calendar (Month grid) + Year view of earnings announcements with EPS beat/miss
indicators and deep links to earnings call transcripts.
"""
from datetime import date, datetime
from typing import Optional, List, Dict, Any, Tuple
import html

import streamlit as st

from components.styles import hide_sidebar, render_styles
from components.navigation import render_header, render_coresight_footer
from core.auth_manager import require_auth, get_current_user, _auth_request_meta, log_auth_cookie_server_presence
from utils.server_logger import new_rerun_id, log_timing

new_rerun_id("earnings_calendar")
log_timing(
    "AUTH_PAGE_ENTRY",
    0,
    f"{_auth_request_meta()} page=earnings_calendar stage=before_require_auth",
    level="WARNING",
)
log_auth_cookie_server_presence(page="earnings_calendar", context="before_require_auth")
# _auth_data = require_auth(page="earnings_calendar")
# log_timing(
#     "AUTH_PAGE_ENTRY",
#     0,
#     f"{_auth_request_meta()} page=earnings_calendar stage=after_require_auth "
#     f"user={(_auth_data or {}).get('user_email', '?')}",
#     level="WARNING",
# )
hide_sidebar()

from data.repository import CompanyRepository, EarningsCalendarRepository
from data.watchlist_service import get_user_watchlists, get_watchlist_companies
from utils.ticker_utils import validate_and_get_ticker, DEFAULT_FALLBACK_TICKER
from utils.server_logger import log_error, log_exception, log_structured_error
from utils.earnings_alert_email import send_earnings_alert_preferences_confirmation

# ─────────────────────────────────────────────────────────────────────────────
# COMPANY COLOR PALETTE
# ─────────────────────────────────────────────────────────────────────────────
# 20 visually-distinct dark colors — all readable with white text.
# A deterministic hash on the ticker ensures the same company always gets
# the same color across sessions and views.
_COMPANY_COLORS = [
    "#1565C0",  # Blue
    "#7B1FA2",  # Purple
    "#00796B",  # Teal
    "#E65100",  # Deep Orange
    "#2E7D32",  # Dark Green
    "#880E4F",  # Dark Pink
    "#0D47A1",  # Dark Blue
    "#4A148C",  # Deep Purple
    "#006064",  # Dark Cyan
    "#BF360C",  # Dark Deep Orange
    "#1A237E",  # Indigo
    "#33691E",  # Light Green
    "#827717",  # Olive
    "#4E342E",  # Brown
    "#37474F",  # Blue Grey
    "#558B2F",  # Moss Green
    "#0277BD",  # Sky Blue
    "#AD1457",  # Rose
    "#6D4C41",  # Warm Brown
    "#00695C",  # Dark Teal
]


def _company_color(ticker: str) -> str:
    """Return a stable per-company color derived from the ticker string."""
    try:
        n = sum(ord(c) * (i + 1) for i, c in enumerate(ticker))
        return _COMPANY_COLORS[n % len(_COMPANY_COLORS)]
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_company_color",
                             operation="compute_color", context=f"ticker={ticker}")
        return _COMPANY_COLORS[0]


# ─────────────────────────────────────────────────────────────────────────────
# EVENT-TYPE COLOR LEGEND  (Coresight brand palette — from the official
# Color Palette & Style Guide). Earnings events are colored by fiscal quarter;
# completed M&A events use the Coresight accent green. Each entry is
# (background, text) chosen for readability of the small FullCalendar labels.
# ─────────────────────────────────────────────────────────────────────────────
_KIND_COLORS: Dict[str, Tuple[str, str]] = {
    "Q1": ("#005F8F", "#FFFFFF"),   # Secondary Dark Blue
    "Q2": ("#A3C0CE", "#2D2A29"),   # Secondary Light Blue (dark text for contrast)
    "Q3": ("#7F7F7F", "#FFFFFF"),   # Secondary Grey
    "Q4": ("#2D2A29", "#FFFFFF"),   # Primary Black
    "ma": ("#61A575", "#16341F"),   # Accent Green — M&A Activities
}

# Order + labels for the clickable legend filter (NOT a dropdown).
_LEGEND_ITEMS: List[Tuple[str, str]] = [
    ("Q1", "Q1"),
    ("Q2", "Q2"),
    ("Q3", "Q3"),
    ("Q4", "Q4"),
    ("ma", "M&A Completion"),
]
_ALL_KINDS = tuple(k for k, _ in _LEGEND_ITEMS)


def _kind_color(kind: str) -> Tuple[str, str]:
    """(background, text) for an event kind; falls back to a neutral grey."""
    return _KIND_COLORS.get(kind, ("#37474F", "#FFFFFF"))


def _earnings_kind(fiscal_q: Optional[Any]) -> Optional[str]:
    """Map a fiscal quarter (1-4) to a legend kind 'Q1'..'Q4'."""
    try:
        q = int(fiscal_q)
        return f"Q{q}" if 1 <= q <= 4 else None
    except (TypeError, ValueError):
        return None


def _fmt_ma_value(val: Optional[Any]) -> str:
    """Format M&A transaction value (stored in USD millions) → '$1.23B' / '$450.0M'."""
    try:
        if val is None:
            return "—"
        v = float(val)
        if v <= 0:
            return "—"
        if v >= 1_000_000:
            return f"${v / 1_000_000:.2f}T"
        if v >= 1_000:
            return f"${v / 1_000:.2f}B"
        return f"${v:,.1f}M"
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_fmt_ma_value",
                             operation="format_ma_value", context=f"val={val}")
        return "—"


def _toggle_event_kind(kind: str) -> None:
    """Legend chip on_click — toggle a kind in the active set and remount calendar."""
    try:
        cur = set(st.session_state.get("ec_active_kinds") or list(_ALL_KINDS))
        if kind in cur:
            cur.discard(kind)
        else:
            cur.add(kind)
        st.session_state.ec_active_kinds = [k for k in _ALL_KINDS if k in cur]
        # Clear any open detail panel and force the calendar component to remount so
        # it repaints with the filtered event set (cal key embeds ec_cal_version).
        st.session_state.ec_selected_event = None
        st.session_state.ec_cal_version = st.session_state.get("ec_cal_version", 0) + 1
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_toggle_event_kind",
                             operation="toggle_kind", context=f"kind={kind}")


def _render_event_type_legend() -> None:
    """Clickable color legend that filters the calendar by event type.

    Active chips are filled with the type's Coresight brand color; inactive chips
    are outlined/dimmed. Clicking toggles that type — and combines (AND) with the
    Company and Watchlist filters already applied to the event feeds.
    """
    try:
        active = set(st.session_state.get("ec_active_kinds") or list(_ALL_KINDS))
        cols = st.columns([1.05, 0.72, 0.72, 0.72, 0.72, 1.9, 3.45],
                          gap="small", vertical_alignment="center")
        with cols[0]:
            st.markdown('<div class="ec-legend-label">Show types</div>', unsafe_allow_html=True)
        for _i, (kind, label) in enumerate(_LEGEND_ITEMS):
            with cols[_i + 1]:
                st.button(
                    label,
                    key=f"ec_legend_{kind}",
                    type="primary" if kind in active else "secondary",
                    on_click=_toggle_event_kind,
                    args=(kind,),
                    width="stretch",
                )
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_event_type_legend",
                             operation="render_legend", context="legend render")


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

def _get_css() -> str:
    try:
        return """
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&family=Montserrat:wght@600;700&display=swap');

[data-testid="stHeaderActionElements"] { display:none !important; }
header[data-testid="stHeader"] { display:none !important; height:0 !important; overflow:hidden !important; }
div.block-container > div[data-testid="stVerticalBlock"] { padding-top:0 !important; }

.ec-filter-label {
    font-size:11px; font-weight:500; color:#6B6B6B;
    text-transform:uppercase; letter-spacing:0.5px; margin-bottom:3px;
}

/* ── Event count bar ── */
.ec-event-count { font-size:12px; color:#6B6B6B; text-align:right; padding:4px 0; }

/* ── No data ── */
.ec-no-data { text-align:center; color:#6B6B6B; padding:48px 0; font-size:15px; }

/* ── Calendar detail panel ── */
.ec-detail-card {
    background:#FFFFFF; border:1px solid #E5E5E5; border-radius:12px;
    padding:20px; box-shadow:0 2px 12px rgba(0,0,0,0.07);
}
.ec-detail-title {
    font-family:'Montserrat',sans-serif; font-weight:700;
    font-size:18px; color:#2D2A29; margin-bottom:4px;
}
.ec-detail-sub { font-size:13px; color:#6B6B6B; margin-bottom:16px; }
.ec-detail-row {
    display:flex; justify-content:space-between;
    padding:8px 0; border-bottom:1px solid #F2F2F2; font-size:13px;
}
.ec-detail-row:last-child { border-bottom:none; }
.ec-detail-key { color:#6B6B6B; }
.ec-detail-val { color:#2D2A29; font-weight:500; text-align:right; }

/* Email alerts — modal dialog (replaces expander; tight vertical rhythm) */
[data-testid="stDialogContent"] [data-testid="stVerticalBlock"] {
    gap:0.35rem !important;
}
[data-testid="stDialogContent"] [data-testid="stVerticalBlock"] > div {
    gap:0.35rem !important;
}
[data-testid="stDialogContent"] [data-testid="element-container"] {
    margin-bottom:0 !important;
}
[data-testid="stDialogContent"] [data-testid="stWidgetLabel"] {
    margin-bottom:0.1rem !important;
    min-height:0 !important;
    padding-bottom:0 !important;
}
[data-testid="stDialogContent"] [data-testid="stCaptionContainer"] {
    margin-top:-0.1rem !important;
    margin-bottom:0 !important;
}
[data-testid="stDialogContent"] .stButton { margin-top:0.15rem !important; }
/* Save (primary) — Coresight D6 red */
[data-testid="stDialogContent"] button[data-testid="baseButton-primary"],
[data-testid="stDialogContent"] .stButton > button[kind="primary"] {
    background-color: #D62E2F !important;
    border-color: #D62E2F !important;
    color: #FFFFFF !important;
}
[data-testid="stDialogContent"] button[data-testid="baseButton-primary"]:hover,
[data-testid="stDialogContent"] .stButton > button[kind="primary"]:hover {
    background-color: #b82526 !important;
    border-color: #b82526 !important;
    color: #FFFFFF !important;
}
[data-testid="stDialogContent"] div[data-testid="stRadio"] fieldset {
    margin:0 !important;
    padding:0.15rem 0 0 0 !important;
}
[data-testid="stDialogContent"] div[data-testid="stRadio"] [role="radiogroup"] {
    gap:0.5rem !important;
    min-height:0 !important;
}
[data-testid="stDialogContent"] [data-testid="stWidgetLabel"] p,
[data-testid="stDialogContent"] [data-testid="stWidgetLabel"] label {
    color:#000000 !important;
}
[data-testid="stDialogContent"] div[data-testid="stRadio"] label,
[data-testid="stDialogContent"] div[data-testid="stRadio"] span {
    color:#000000 !important;
}
[data-testid="stDialogContent"] [data-testid="stCheckbox"] label,
[data-testid="stDialogContent"] [data-testid="stCheckbox"] span {
    color:#000000 !important;
}
[data-testid="stDialogContent"] [data-testid="stNumberInput"] input {
    color:#000000 !important;
}
[data-testid="stDialogContent"] [data-testid="stCaption"] {
    color:#000000 !important;
}
/* Toolbar — Email Alerts (Coresight D6 primary; same family/sizing rhythm as Month/Year) */
.st-key-ec_open_email_alerts button[kind="primary"],
.st-key-ec_open_email_alerts button[data-testid="baseButton-primary"] {
    font-family:'Montserrat',sans-serif !important;
    font-weight:600 !important;
    font-size:13px !important;
    border-radius:8px !important;
    background-color:#D62E2F !important;
    border:2px solid #D62E2F !important;
    color:#FFFFFF !important;
    text-transform:capitalize !important;
    white-space:nowrap !important;
}
.st-key-ec_open_email_alerts button[kind="primary"]:hover,
.st-key-ec_open_email_alerts button[data-testid="baseButton-primary"]:hover {
    background-color:#FFFFFF !important;
    border:2px solid #D62E2F !important;
    color:#D62E2F !important;
}
/* Toolbar badge — top-aligned; small gap between Email Alerts and pill (avoid overlap) */
[data-testid="stHorizontalBlock"]:has(.st-key-ec_open_email_alerts),
[data-testid="stHorizontalBlock"]:has(.ec-toolbar-alerts-pill-host) {
    align-items:flex-start !important;
}
[data-testid="column"]:has(.ec-toolbar-alerts-pill-host) {
    flex:0 0 auto !important;
    width:auto !important;
    min-width:unset !important;
    padding-left:0 !important;
    margin-left:0 !important;
}
.ec-toolbar-alerts-pill-host {
    display:inline-flex;
    align-items:center;
    margin:0;
    padding-top:1px;
}
.ec-alerts-status-pill.ec-alerts-status-pill--toolbar {
    font-family:'Roboto',sans-serif !important;
    min-width:unset !important;
    min-height:unset !important;
    padding:3px 9px !important;
    font-size:10px !important;
    font-weight:700 !important;
    border-radius:8px !important;
    line-height:1.2 !important;
    letter-spacing:0.01em !important;
}
/* Email alert saved-state display */
.ec-alerts-status-wrap {
    display:flex;
    align-items:center;
    justify-content:flex-end;
    min-height:38px;
    margin-top:0.15rem;
    width:100%;
}
.ec-alerts-status-pill {
    display:inline-flex;
    align-items:center;
    justify-content:center;
    min-width:96px;
    min-height:32px;
    padding:6px 14px;
    border-radius:8px;
    font-family:'Roboto',sans-serif;
    font-size:12px;
    font-weight:700;
    line-height:1.2;
    border:1px solid #9E9E9E;
    color:#424242;
    background:#F4F5F7;
    text-align:center;
    white-space:nowrap;
}
.ec-alerts-status-pill--on {
    border-color:#2E7D32;
    color:#1B5E20;
    background:#E8F5E9;
}
.ec-alerts-lede {
    font-size:13px; color:#000000; margin:0 0 2px 0; line-height:1.35;
    font-family:'Roboto',sans-serif;
}
.ec-alerts-foot {
    font-size:11px; color:#000000; line-height:1.35; margin:4px 0 0 0;
    font-family:'Roboto',sans-serif;
}
.ec-alerts-hint {
    font-size:12px; color:#000000; line-height:1.35; margin:0;
    font-family:'Roboto',sans-serif;
}
.ec-alerts-section {
    font-family:'Montserrat',sans-serif;
    font-size:11px; font-weight:600; color:#000000;
    text-transform:uppercase; letter-spacing:0.05em;
    margin:10px 0 6px 0; padding-bottom:5px; border-bottom:1px solid #ECECEC;
}
.ec-alerts-callout {
    font-size:12px; color:#000000; line-height:1.45;
    background:#F4F5F7; border-radius:8px; padding:9px 12px; margin:6px 0 10px 0;
    border-left:4px solid #9E9E9E;
    font-family:'Roboto',sans-serif;
}
.ec-alerts-callout--on {
    background:#E8F5E9; border-left-color:#2E7D32; color:#000000;
}
.ec-alerts-callout strong,
.ec-alerts-callout--on strong {
    color:#000000;
}

div[data-testid="stSelectbox"] label { font-size:11px !important; font-weight:500 !important; color:#6B6B6B !important; }
[data-testid="stDialogContent"] div[data-testid="stSelectbox"] label {
    color:#000000 !important;
}

/* ── Clickable color legend (event-type filter) ── */
.ec-legend-label {
    font-size:11px; font-weight:600; color:#6B6B6B;
    text-transform:uppercase; letter-spacing:0.5px; margin:2px 0 0 0;
    font-family:'Montserrat',sans-serif; white-space:nowrap;
}
/* Legend chips: compact, pill-shaped, branded per event kind */
div[class*="st-key-ec_legend_"] button {
    font-family:'Roboto',sans-serif !important;
    font-weight:700 !important; font-size:12px !important;
    border-radius:14px !important; padding:3px 12px !important;
    min-height:30px !important; width:100% !important;
    white-space:nowrap !important; box-shadow:none !important;
    transition:opacity .12s ease, filter .12s ease !important;
}
/* Inactive (secondary) chips: outlined + dimmed so "off" reads clearly */
div[class*="st-key-ec_legend_"] button[kind="secondary"] {
    background:#FFFFFF !important; opacity:0.5 !important;
    filter:grayscale(35%) !important; font-weight:600 !important;
}
div[class*="st-key-ec_legend_"] button[kind="secondary"]:hover { opacity:0.8 !important; }

.st-key-ec_legend_Q1 button[kind="primary"]{ background:#005F8F !important; border-color:#005F8F !important; color:#FFFFFF !important; }
.st-key-ec_legend_Q1 button[kind="secondary"]{ border:2px solid #005F8F !important; color:#005F8F !important; }
.st-key-ec_legend_Q2 button[kind="primary"]{ background:#A3C0CE !important; border-color:#A3C0CE !important; color:#2D2A29 !important; }
.st-key-ec_legend_Q2 button[kind="secondary"]{ border:2px solid #A3C0CE !important; color:#5B7C8D !important; }
.st-key-ec_legend_Q3 button[kind="primary"]{ background:#7F7F7F !important; border-color:#7F7F7F !important; color:#FFFFFF !important; }
.st-key-ec_legend_Q3 button[kind="secondary"]{ border:2px solid #7F7F7F !important; color:#7F7F7F !important; }
.st-key-ec_legend_Q4 button[kind="primary"]{ background:#2D2A29 !important; border-color:#2D2A29 !important; color:#FFFFFF !important; }
.st-key-ec_legend_Q4 button[kind="secondary"]{ border:2px solid #2D2A29 !important; color:#2D2A29 !important; }
.st-key-ec_legend_ma button[kind="primary"]{ background:#61A575 !important; border-color:#61A575 !important; color:#16341F !important; }
.st-key-ec_legend_ma button[kind="secondary"]{ border:2px solid #61A575 !important; color:#3E7350 !important; }
</style>
"""
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_get_css",
                             operation="get_css", context="CSS generation")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_eps(val: Optional[float]) -> str:
    try:
        if val is None:
            return "—"
        return f"-${abs(val):.2f}" if val < 0 else f"${val:.2f}"
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_fmt_eps",
                             operation="format_eps", context=f"val={val}")
        return "—"


def _fmt_surprise(val: Optional[float]) -> str:
    try:
        if val is None:
            return "—"
        return f"+{val:.1f}%" if val >= 0 else f"{val:.1f}%"
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_fmt_surprise",
                             operation="format_surprise", context=f"val={val}")
        return "—"


def _coerce_http_url(raw: Optional[Any]) -> Optional[str]:
    """Accept http(s) URLs and bare hosts/paths; return usable href or None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return s
    if s.startswith("//"):
        return "https:" + s
    dangerous = ("javascript:", "data:", "vbscript:")
    if any(low.startswith(p) for p in dangerous):
        return None
    if "://" in s.split("/")[0]:
        return None
    return "https://" + s.lstrip("/")


def _fmt_date(ed) -> str:
    """Format a date value (date object or YYYY-MM-DD string) → 'Jan 15, 2025'."""
    if ed is None:
        return "—"
    try:
        if hasattr(ed, "strftime"):
            return ed.strftime("%b %d, %Y")
        return datetime.strptime(str(ed), "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_fmt_date", operation="FORMAT_DATE")
        return str(ed) if ed else "—"



def _fmt_report_time_display(val: Optional[Any]) -> str:
    """
    NASDAQ calendar rows may include announcement time; YF uses placeholders.
    Show the real value when present; otherwise 'Time Not Available'.
    """
    _NASDAQ_TIME_LABELS = {
        "time pre market": "Pre Market",
        "time after hours": "After Hours",
    }
    try:
        if val is None:
            return "Time Not Available"
        s = str(val).strip()
        if not s:
            return "Time Not Available"
        norm = " ".join(s.lower().replace("-", " ").replace("_", " ").split())
        if norm in (
            "time not supplied",
            "not supplied",
            "n/a",
            "na",
            "unknown",
            "tbd",
            "none",
            "null",
        ) or norm.startswith("time not "):
            return "Time Not Available"
        if norm in _NASDAQ_TIME_LABELS:
            return _NASDAQ_TIME_LABELS[norm]
        return html.escape(s)
    except Exception as exc:
        log_structured_error(
            exc,
            page="earnings_calendar",
            component="_fmt_report_time_display",
            operation="format_report_time",
            context=f"val={val}",
        )
        return "Time Not Available"


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL ALERT PREFERENCES
# ─────────────────────────────────────────────────────────────────────────────

_EC_ALERT_ALL_COMPANIES = "All companies"
_EC_ALERT_SPECIFIC_COMPANIES = "Specific companies"
_EC_ALERT_ALL_SECTORS = "All sectors"
_EC_ALERT_SPECIFIC_SECTORS = "Specific sectors"
_EC_ALERT_WATCHLIST = "Select Watchlist"
_EC_WATCHLIST_ID_PREFIX = "watchlist_id:"  # sentinel stored in tickers_json


def _get_signed_in_user_email() -> str:
    """
    Best-effort lookup for the authenticated user's email.
    Prefer AuthManager (`auth_data.user_email`), then legacy session keys.
    """
    try:
        cu = get_current_user()
        if cu:
            return str(cu).strip()

        auth = st.session_state.get("auth_data")
        if isinstance(auth, dict):
            for key in ("user_email", "email"):
                value = auth.get(key)
                if value:
                    return str(value).strip()

        candidate_keys = (
            "user_email",
            "email",
            "auth_email",
            "authenticated_email",
            "logged_in_email",
        )
        for key in candidate_keys:
            value = st.session_state.get(key)
            if value:
                return str(value).strip()

        user_obj = st.session_state.get("user") or st.session_state.get("auth_user")
        if isinstance(user_obj, dict):
            for key in ("email", "user_email", "mail"):
                value = user_obj.get(key)
                if value:
                    return str(value).strip()
        return ""
    except Exception as exc:
        log_structured_error(
            exc,
            page="earnings_calendar",
            component="_get_signed_in_user_email",
            operation="resolve_email",
            context="session_state email lookup",
        )
        return ""


def _get_company_label(ticker_meta: Dict[str, Any]) -> str:
    """Return display label like 'Apple Inc. (AAPL)' for subscription multiselect."""
    try:
        name = ticker_meta.get("name") or ticker_meta.get("company_name") or ticker_meta.get("ticker")
        ticker = ticker_meta.get("ticker", "")
        return f"{name} ({ticker})" if ticker else str(name)
    except Exception as exc:
        log_structured_error(
            exc,
            page="earnings_calendar",
            component="_get_company_label",
            operation="format_label",
            context=f"ticker_meta={ticker_meta}",
        )
        return "Unknown Company"


def _calendar_tickers_matching_sectors(
    all_tickers_meta: List[Dict[str, Any]],
    sectors: Tuple[str, ...],
) -> Tuple[str, ...]:
    """Tickers that appear on the earnings calendar and match coreiq_companies sectors."""
    if not sectors:
        return tuple()
    try:
        sector_set = {s.strip() for s in sectors if s and str(s).strip()}
        if not sector_set:
            return tuple()
        companies_map = CompanyRepository.get_companies_map()
        out: List[str] = []
        for m in all_tickers_meta:
            t = m.get("ticker")
            if not t:
                continue
            row = companies_map.get(t) or {}
            ind = (row.get("primary_industry_coresight") or "").strip()
            if ind in sector_set:
                out.append(t)
        return tuple(sorted(set(out)))
    except Exception as exc:
        log_structured_error(
            exc,
            page="earnings_calendar",
            component="_calendar_tickers_matching_sectors",
            operation="resolve_sectors",
            context=f"sectors={sectors}",
        )
        return tuple()


def _normalize_alert_selection_mode(saved: Dict[str, Any]) -> str:
    """Map DB / legacy selection_mode to `companies`, `sectors`, or `watchlist`."""
    mode = (saved.get("selection_mode") or "companies") or "companies"
    tickers = saved.get("tickers") or []
    sectors = saved.get("sectors") or []
    if mode in ("companies", "sectors", "watchlist"):
        return mode
    if mode == "all":
        return "companies"
    if mode == "selected":
        return "companies"
    if mode == "sector":
        return "sectors"
    if mode == "company_or_sector":
        if tickers and not sectors:
            return "companies"
        if sectors and not tickers:
            return "sectors"
        if tickers:
            return "companies"
        if sectors:
            return "sectors"
        return "companies"
    return "companies"


def _load_existing_alert_preferences(user_email: str) -> Dict[str, Any]:
    """
    Load saved preferences from the DB via EarningsCalendarRepository.

    Expected repository method:
        get_earnings_alert_preferences(user_email: str) -> dict | None

    Expected dict shape:
        {
            "enabled": True,
            "days_before": 3,
            "selection_mode": "companies" | "sectors",
            "tickers": [],  # empty = all calendar companies; else explicit subset
            "sectors": [],  # empty = all coreiq sectors; else primary_industry_coresight subset
        }
    """
    default_prefs = {
        "enabled": False,
        "days_before": 1,
        "selection_mode": "companies",
        "tickers": [],
        "sectors": [],
        "watchlist_id": None,
    }
    try:
        getter = getattr(EarningsCalendarRepository, "get_earnings_alert_preferences", None)
        if not callable(getter) or not user_email:
            return default_prefs
        saved = getter(user_email) or {}
        mode = _normalize_alert_selection_mode(saved)
        raw_tickers = list(saved.get("tickers") or [])
        watchlist_id: Optional[int] = None
        if mode == "watchlist":
            for t in raw_tickers:
                if str(t).startswith(_EC_WATCHLIST_ID_PREFIX):
                    try:
                        watchlist_id = int(str(t)[len(_EC_WATCHLIST_ID_PREFIX):])
                    except (ValueError, TypeError):
                        pass
                    break
        return {
            "enabled": bool(saved.get("enabled", default_prefs["enabled"])),
            "days_before": int(saved.get("days_before", default_prefs["days_before"])),
            "selection_mode": mode,
            "tickers": raw_tickers,
            "sectors": list(saved.get("sectors") or []),
            "watchlist_id": watchlist_id,
        }
    except Exception as exc:
        log_structured_error(
            exc,
            page="earnings_calendar",
            component="_load_existing_alert_preferences",
            operation="load_preferences",
            context=f"user_email={user_email}",
        )
        return default_prefs


def _save_alert_preferences(
    *,
    user_email: str,
    enabled: bool,
    days_before: int,
    selection_mode: str,
    tickers: Tuple[str, ...],
    sectors: Tuple[str, ...] = tuple(),
    watchlist_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Persist alert preferences in the DB via EarningsCalendarRepository.

    When selection_mode is "watchlist", watchlist_id is encoded as the sole
    entry in tickers: ["watchlist_id:<id>"].  The dispatcher resolves the
    actual tickers at send time via get_watchlist_tickers().
    """
    try:
        if not user_email:
            return False, "Could not find the signed-in user's email address."
        if days_before < 0 or days_before > 60:
            return False, "Please choose a reminder window between 0 and 60 days."
        if selection_mode not in {"companies", "sectors", "watchlist", "all", "company_or_sector", "selected", "sector"}:
            return False, "Please choose a valid scope."

        saver = getattr(EarningsCalendarRepository, "upsert_earnings_alert_preferences", None)
        if not callable(saver):
            return (
                False,
                "Database save method is missing: EarningsCalendarRepository.upsert_earnings_alert_preferences(...).",
            )

        # Encode watchlist_id into tickers_json as a sentinel token
        if selection_mode == "watchlist":
            if watchlist_id is None:
                return False, "Please select a watchlist."
            encoded_tickers = [f"{_EC_WATCHLIST_ID_PREFIX}{watchlist_id}"]
        else:
            encoded_tickers = list(tickers)

        payload = dict(
            user_email=user_email,
            enabled=enabled,
            days_before=int(days_before),
            selection_mode=selection_mode,
            tickers=encoded_tickers,
            sectors=list(sectors),
        )
        try:
            out = saver(**payload)
        except TypeError:
            payload.pop("sectors", None)
            out = saver(**payload)
        if out is False:
            return False, "Could not save preferences."
        return True, "Saved."
    except Exception as exc:
        log_structured_error(
            exc,
            page="earnings_calendar",
            component="_save_alert_preferences",
            operation="save_preferences",
            context=(
                f"user_email={user_email}; enabled={enabled}; days_before={days_before}; "
                f"selection_mode={selection_mode}; tickers={tickers}; sectors={sectors}; "
                f"watchlist_id={watchlist_id}"
            ),
        )
        return False, "Unable to save earnings alert preferences right now. Please try again."


def _earnings_alert_confirmation_lines(
    *,
    enabled: bool,
    days_before: int,
    selection_mode: str,
    tickers: Tuple[str, ...],
    sectors: Tuple[str, ...],
    watchlist_name: str = "",
) -> List[str]:
    """Plain-text lines for the confirmation email body."""
    lines = [
        f"Send earnings reminders: {'Yes' if enabled else 'No'}",
        f"Days before earnings date: {days_before}",
    ]
    if selection_mode == "watchlist":
        lines.append("Coverage: Watchlist")
        lines.append(
            f"Watchlist: {watchlist_name}" if watchlist_name else "Watchlist selected"
        )
    elif selection_mode == "sectors":
        lines.append("Coverage: Sectors (coreiq_companies)")
        lines.append(
            "Sector scope: All sectors"
            if not sectors
            else f"Sector scope: {len(sectors)} sector(s) selected"
        )
    else:
        lines.append("Coverage: Companies")
        lines.append(
            "Company scope: All companies on this calendar"
            if not tickers
            else f"Company scope: {len(tickers)} company(ies) selected"
        )
    return lines


def _ec_toolbar_alert_status_html(saved_alerts_on: bool) -> str:
    """Same pill classes/colors as the popup; compact sizing for the toolbar."""
    if saved_alerts_on:
        cls = "ec-alerts-status-pill ec-alerts-status-pill--on ec-alerts-status-pill--toolbar"
        label = "Alerts on"
    else:
        cls = "ec-alerts-status-pill ec-alerts-status-pill--toolbar"
        label = "Alerts off"
    return (
        f'<div class="ec-toolbar-alerts-pill-host"><span class="{cls}">'
        f"{html.escape(label)}</span></div>"
    )


def _ec_alert_status_callout_markup(saved_alerts_on: bool) -> str:
    """HTML for green/grey saved-state banner in the email alerts dialog."""
    if saved_alerts_on:
        return (
            '<div class="ec-alerts-callout ec-alerts-callout--on">'
            "<strong>Reminders are on</strong> in your saved settings. "
            "Change timing or coverage below, then click <strong>Save</strong> to update them. "
            "You will get an email every time you save your preferences.</div>"
        )
    return (
        '<div class="ec-alerts-callout">'
        "<strong>Reminders are off</strong> in your saved settings. "
        "You can still change options below and click <strong>Save</strong>. "
        "Reminder emails stay off until you turn reminders on and save. "
        "You will get an email every time you save your preferences.</div>"
    )


def _render_email_alert_preferences_inner(all_tickers_meta: List[Dict[str, Any]]) -> None:
    """Render subscription controls for earnings email reminders (dialog body)."""
    try:
        user_email = _get_signed_in_user_email()
        prefs = _load_existing_alert_preferences(user_email)

        if st.session_state.pop("ec_alert_save_success", False):
            _ec_alert_was_mailed = bool(st.session_state.pop("ec_alert_save_mailed", False))
            if _ec_alert_was_mailed:
                st.success("Saved. Check your email for a confirmation.")
            else:
                st.success("Saved.")
                st.caption(
                    "No confirmation email was sent. Set EMAIL_PASSWORD (and SMTP_SERVER / FROM_EMAIL "
                    "if needed) to enable confirmations."
                )
        display_dest = html.escape(user_email) if user_email else "your signed-in email"
        st.markdown(
            f'<p class="ec-alerts-lede">Reminders are sent to <strong>{display_dest}</strong>.</p>',
            unsafe_allow_html=True,
        )

        if not user_email:
            st.info(
                "Your account email could not be loaded. Refresh the page or sign in again to save preferences."
            )
        label_to_ticker = {_get_company_label(m): m.get("ticker") for m in all_tickers_meta if m.get("ticker")}
        ticker_to_label = {ticker: label for label, ticker in label_to_ticker.items()}
        sorted_labels = sorted(label_to_ticker.keys(), key=lambda x: x.lower())

        default_days = int(prefs.get("days_before", 1))
        if default_days < 0 or default_days > 60:
            default_days = 1

        existing_mode = prefs.get("selection_mode", "companies")
        if existing_mode not in ("companies", "sectors", "watchlist"):
            existing_mode = "companies"

        mode_options = ["Select Companies", "Select Sectors", "Select Watchlist"]
        if existing_mode == "sectors":
            default_mode_index = 1
        elif existing_mode == "watchlist":
            default_mode_index = 2
        else:
            default_mode_index = 0

        sector_choices = CompanyRepository.get_all_sectors()

        if "ec_alert_send_email" not in st.session_state:
            st.session_state["ec_alert_send_email"] = bool(prefs.get("enabled", False))

        saved_alerts_on = bool(prefs.get("enabled", False))
        _ec_chk_col, _ec_status_col = st.columns([4.0, 1.0], gap="small", vertical_alignment="center")
        with _ec_chk_col:
            want_reminders = st.checkbox(
                "Send me earnings reminders to this email",
                key="ec_alert_send_email",
            )
        with _ec_status_col:
            _ec_status_class = "ec-alerts-status-pill ec-alerts-status-pill--on" if saved_alerts_on else "ec-alerts-status-pill"
            _ec_status_text = "Alerts on" if saved_alerts_on else "Alerts off"
            st.html(
                f'<div class="ec-alerts-status-wrap"><span class="{_ec_status_class}">{_ec_status_text}</span></div>'
            )
        # Callout reflects last saved state; refreshed in-place after Save (no full-page rerun).
        _ec_callout_ph = st.empty()
        with _ec_callout_ph:
            st.markdown(_ec_alert_status_callout_markup(saved_alerts_on), unsafe_allow_html=True)

        st.markdown('<p class="ec-alerts-section">Timing</p>', unsafe_allow_html=True)
        days_before = st.number_input(
            "Days before earnings date",
            min_value=0,
            max_value=60,
            value=default_days,
            step=1,
            key="ec_alert_days_before",
            help="Reminder is scheduled for (earnings date − this many calendar days).",
        )

        st.markdown('<p class="ec-alerts-section">Coverage</p>', unsafe_allow_html=True)
        selected_mode_label = st.radio(
            "Choose coverage type",
            options=mode_options,
            index=default_mode_index,
            horizontal=True,
            key="ec_alert_company_scope",
            label_visibility="collapsed",
        )

        if selected_mode_label == "Select Sectors":
            selection_mode = "sectors"
        elif selected_mode_label == "Select Watchlist":
            selection_mode = "watchlist"
        else:
            selection_mode = "companies"

        selected_tickers: Tuple[str, ...] = tuple()
        selected_sectors: Tuple[str, ...] = tuple()
        selected_watchlist_id: Optional[int] = None
        selected_watchlist_name: str = ""

        n_cal = len({m.get("ticker") for m in all_tickers_meta if m.get("ticker")})

        if selection_mode == "watchlist":
            _scope_title = "Watchlist"
        elif selection_mode == "sectors":
            _scope_title = "Sectors"
        else:
            _scope_title = "Companies"
        st.markdown(
            f'<p class="ec-alerts-section">{html.escape(_scope_title)}</p>',
            unsafe_allow_html=True,
        )

        if selection_mode == "companies":
            saved_t = prefs.get("tickers") or []
            company_cov = st.radio(
                "Company list",
                options=[_EC_ALERT_ALL_COMPANIES, _EC_ALERT_SPECIFIC_COMPANIES],
                index=1 if saved_t else 0,
                horizontal=True,
                key="ec_alert_company_coverage",
                label_visibility="collapsed",
            )
            if company_cov == _EC_ALERT_ALL_COMPANIES:
                selected_tickers = tuple()
                st.markdown(
                    f'<p class="ec-alerts-hint"><strong>{html.escape(_EC_ALERT_ALL_COMPANIES)}</strong> — '
                    f"all <strong>{n_cal}</strong> names on this calendar.</p>",
                    unsafe_allow_html=True,
                )
            else:
                default_labels = sorted(
                    [ticker_to_label[t] for t in saved_t if t in ticker_to_label],
                    key=lambda x: x.lower(),
                )
                selected_labels = st.multiselect(
                    "Choose companies",
                    options=sorted_labels,
                    default=default_labels,
                    key="ec_alert_selected_companies",
                    placeholder="Search and add companies",
                )
                selected_tickers = tuple(
                    sorted({label_to_ticker[lb] for lb in selected_labels if lb in label_to_ticker})
                )
                if selected_tickers:
                    st.markdown(
                        f'<p class="ec-alerts-hint"><strong>{len(selected_tickers)}</strong> of {n_cal} '
                        "calendar companies.</p>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<p class="ec-alerts-hint">Pick one or more companies, or switch to '
                        f"<strong>{html.escape(_EC_ALERT_ALL_COMPANIES)}</strong>.</p>",
                        unsafe_allow_html=True,
                    )
        elif selection_mode == "sectors":
            if not sector_choices:
                st.warning("No sectors in coreiq_companies — use Select Companies instead.")
            saved_s = prefs.get("sectors") or []
            sector_cov = st.radio(
                "Sector list",
                options=[_EC_ALERT_ALL_SECTORS, _EC_ALERT_SPECIFIC_SECTORS],
                index=1 if saved_s else 0,
                horizontal=True,
                key="ec_alert_sector_coverage",
                label_visibility="collapsed",
            )
            if sector_cov == _EC_ALERT_ALL_SECTORS:
                selected_sectors = tuple()
                if sector_choices:
                    n_sec = len(sector_choices)
                    st.markdown(
                        f'<p class="ec-alerts-hint"><strong>{html.escape(_EC_ALERT_ALL_SECTORS)}</strong> — '
                        f"all <strong>{n_sec}</strong> sector{'s' if n_sec != 1 else ''} available.</p>",
                        unsafe_allow_html=True,
                    )
            else:
                default_secs = sorted([s for s in saved_s if s in sector_choices])
                picked = st.multiselect(
                    "Choose sectors",
                    options=sector_choices,
                    default=default_secs,
                    key="ec_alert_selected_sectors",
                    placeholder="Search and add sectors",
                )
                selected_sectors = tuple(sorted(picked))
                if selected_sectors:
                    mt = _calendar_tickers_matching_sectors(all_tickers_meta, selected_sectors)
                    st.markdown(
                        f'<p class="ec-alerts-hint"><strong>{len(selected_sectors)}</strong> sector(s) — '
                        f"<strong>{len(mt)}</strong> calendar compan"
                        f"{'y' if len(mt) == 1 else 'ies'} match.</p>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<p class="ec-alerts-hint">Pick one or more sectors, or switch to '
                        f"<strong>{html.escape(_EC_ALERT_ALL_SECTORS)}</strong>.</p>",
                        unsafe_allow_html=True,
                    )
        else:  # selection_mode == "watchlist"
            try:
                user_watchlists = get_user_watchlists(user_email) if user_email else []
            except Exception as _wl_exc:
                log_structured_error(
                    _wl_exc,
                    page="earnings_calendar",
                    component="_render_email_alert_preferences_inner",
                    operation="load_watchlists",
                    context=f"user_email={user_email}",
                )
                user_watchlists = []

            if not user_watchlists:
                st.warning(
                    "No watchlists found. Create a watchlist on the Screening page first, then come back here."
                )
            else:
                saved_wl_id = prefs.get("watchlist_id")
                wl_names = [w["name"] for w in user_watchlists]
                wl_ids = [w["id"] for w in user_watchlists]
                default_wl_index = 0
                if saved_wl_id is not None:
                    try:
                        default_wl_index = wl_ids.index(saved_wl_id)
                    except ValueError:
                        default_wl_index = 0
                wl_options = [
                    f"{w['name']} ({w.get('company_count', 0)} companies)"
                    for w in user_watchlists
                ]
                picked_wl_label = st.selectbox(
                    "Choose watchlist",
                    options=wl_options,
                    index=default_wl_index,
                    key="ec_alert_selected_watchlist",
                )
                picked_wl_idx = wl_options.index(picked_wl_label) if picked_wl_label in wl_options else 0
                selected_watchlist_id = int(wl_ids[picked_wl_idx])
                selected_watchlist_name = wl_names[picked_wl_idx]
                wl_count = user_watchlists[picked_wl_idx].get("company_count", 0)
                st.markdown(
                    f'<p class="ec-alerts-hint"><strong>{html.escape(selected_watchlist_name)}</strong> — '
                    f"<strong>{wl_count}</strong> compan{'y' if wl_count == 1 else 'ies'} in this watchlist.</p>",
                    unsafe_allow_html=True,
                )

        save_clicked = st.button(
            "Save",
            type="primary",
            width=200,
            key="ec_save_alert_preferences",
            disabled=bool(st.session_state.get("ec_alert_save_in_progress", False)),
        )
        if save_clicked:
            save_err: Optional[str] = None
            if want_reminders:
                if selection_mode == "companies":
                    if (
                        st.session_state.get("ec_alert_company_coverage") == _EC_ALERT_SPECIFIC_COMPANIES
                        and not selected_tickers
                    ):
                        save_err = "Choose at least one company, or select All companies."
                elif selection_mode == "sectors":
                    if (
                        st.session_state.get("ec_alert_sector_coverage") == _EC_ALERT_SPECIFIC_SECTORS
                        and not selected_sectors
                    ):
                        save_err = "Choose at least one sector, or select All sectors."
                elif selection_mode == "watchlist":
                    if selected_watchlist_id is None:
                        save_err = "Please select a watchlist."
            if save_err:
                st.error(save_err)
            else:
                st.session_state["ec_alert_save_in_progress"] = True
                with st.spinner("Saving earnings alert preferences…"):
                    ok, message = _save_alert_preferences(
                        user_email=user_email,
                        enabled=bool(want_reminders),
                        days_before=int(days_before),
                        selection_mode=selection_mode,
                        tickers=selected_tickers,
                        sectors=selected_sectors,
                        watchlist_id=selected_watchlist_id,
                    )
                    if ok:
                        confirm_lines = _earnings_alert_confirmation_lines(
                            enabled=bool(want_reminders),
                            days_before=int(days_before),
                            selection_mode=selection_mode,
                            tickers=selected_tickers,
                            sectors=selected_sectors,
                            watchlist_name=selected_watchlist_name,
                        )
                        mailed = send_earnings_alert_preferences_confirmation(
                            to_email=user_email,
                            enabled=bool(want_reminders),
                            body_lines=confirm_lines,
                        )
                        prefs["enabled"] = bool(want_reminders)
                        st.session_state["ec_alert_save_success"] = True
                        st.session_state["ec_alert_save_mailed"] = bool(mailed)
                        st.session_state["ec_alert_save_in_progress"] = False
                        st.rerun()
                    else:
                        st.session_state["ec_alert_save_in_progress"] = False
                        st.error(message)

        st.markdown(
            '<p class="ec-alerts-foot">One alert setup per account. Reminder emails are sent at 8:00 AM UTC. Updates take effect after you click Save.</p>',
            unsafe_allow_html=True,
        )
    except Exception as exc:
        st.session_state["ec_alert_save_in_progress"] = False
        log_structured_error(
            exc,
            page="earnings_calendar",
            component="_render_email_alert_preferences_inner",
            operation="render_alert_preferences",
            context="email alerts UI",
        )
        st.warning("Email alert preferences are temporarily unavailable.")


@st.dialog("Email alerts for upcoming earnings calls", width="large")
def _email_alerts_dialog(all_tickers_meta: List[Dict[str, Any]]) -> None:
    """Modal with earnings reminder preferences."""
    _render_email_alert_preferences_inner(all_tickers_meta)


def _fmt_market_cap(val: Optional[int]) -> str:
    """Format raw market cap (USD) → '$1.23T', '$450.2B', '$12.5M'."""
    try:
        if val is None:
            return "—"
        v = float(val)
        if v >= 1e12:
            return f"${v / 1e12:.2f}T"
        if v >= 1e9:
            return f"${v / 1e9:.1f}B"
        if v >= 1e6:
            return f"${v / 1e6:.1f}M"
        return f"${v:,.0f}"
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_fmt_market_cap",
                             operation="format_market_cap", context=f"val={val}")
        return "—"


def _to_iso(ed) -> str:
    """Convert date/string to ISO format string for FullCalendar."""
    try:
        if ed is None:
            return ""
        if hasattr(ed, "isoformat"):
            return ed.isoformat()
        return str(ed)
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_to_iso",
                             operation="to_iso", context=f"ed={ed}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# FULLCALENDAR EVENT CONVERSION
# ─────────────────────────────────────────────────────────────────────────────

def _to_fullcalendar(events: List[Dict]) -> List[Dict]:
    try:
        fc = []
        for e in events:
            if not e.get("earnings_date") or not e.get("fiscal_q"):
                continue
            fq = e["fiscal_q"]
            fy = e["report_fiscal_year"]
            company_name = e.get("company_name") or e["ticker"]
            label = f"{company_name} Q{fq}" if fq and fy else company_name
            # Color by fiscal quarter (Q1-Q4) so the clickable legend can filter
            # by type. Falls back to a neutral grey for out-of-range quarters.
            kind = _earnings_kind(fq)
            bg, txt = _kind_color(kind) if kind else ("#37474F", "#FFFFFF")
            start_str = _to_iso(e["earnings_date"])
            fc.append({
                "id":        str(e["id"]) if e.get("id") is not None else "",
                "title":     label,
                "start":     start_str,
                "end":       start_str,
                "color":     bg,
                "textColor": txt,
                "extendedProps": {
                    "kind":          kind or "earnings",
                    "ticker":        e["ticker"],
                    "company_name":  e["company_name"],
                    "fiscal_q":      fq,
                    "fiscal_year":   fy,
                    "fiscal_qe":     e.get("fiscal_quarter_ending", ""),
                    "eps_actual":    e.get("eps_actual"),
                    "eps_forecast":  e.get("eps_forecast"),
                    "surprise_pct":  e.get("surprise_pct"),
                    "market_cap":    e.get("market_cap"),
                    "num_estimates": e.get("num_estimates"),
                    "report_time":   e.get("report_time", "time-not-supplied"),
                    "beat_miss":     e.get("beat_miss", "no_data"),
                    "ir_website_url": e.get("ir_website_url"),
                },
            })
        return fc
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_to_fullcalendar",
                             operation="convert_events",
                             context=f"events_count={len(events) if events else 0}")
        return []


def _ma_to_fullcalendar(ma_events: List[Dict]) -> List[Dict]:
    """Convert completed-M&A events into FullCalendar events (Coresight red).

    Ids are prefixed 'ma_' so they never collide with earnings event ids in the
    click handler. The earnings feed is untouched — this only ADDS events.
    """
    try:
        bg, txt = _kind_color("ma")
        fc = []
        for e in ma_events:
            start_str = _to_iso(e.get("earnings_date"))
            if not start_str:
                continue
            company_name = e.get("company_name") or e.get("ticker") or ""
            deal = (e.get("ma_deal_type") or "M&A").title()
            label = f"{company_name} · {deal}"
            # DB numerics arrive as Decimal — cast so streamlit_calendar can JSON it.
            _raw_val = e.get("ma_value_usd_m")
            _val = float(_raw_val) if _raw_val is not None else None
            fc.append({
                "id":        f"ma_{e['id']}" if e.get("id") is not None else f"ma_{start_str}_{e.get('ticker','')}",
                "title":     label,
                "start":     start_str,
                "end":       start_str,
                "color":     bg,
                "textColor": txt,
                "extendedProps": {
                    "kind":             "ma",
                    "ticker":           e.get("ticker", ""),
                    "company_name":     company_name,
                    "ma_acquirer":      e.get("ma_acquirer"),
                    "ma_target":        e.get("ma_target"),
                    "ma_deal_type":     e.get("ma_deal_type"),
                    "ma_value_usd_m":   _val,
                    "ma_close_date":    _to_iso(e.get("ma_close_date")) if e.get("ma_close_date") else "",
                    "ma_announce_date": _to_iso(e.get("ma_announce_date")) if e.get("ma_announce_date") else "",
                    "source":           e.get("source"),
                    "source_ref":       e.get("source_ref"),
                    "headline":         e.get("headline"),
                },
            })
        return fc
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_ma_to_fullcalendar",
                             operation="convert_ma_events",
                             context=f"events_count={len(ma_events) if ma_events else 0}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# CALENDAR CLICK — DETAIL PANEL
# ─────────────────────────────────────────────────────────────────────────────

def _render_ma_detail_panel(event_data: Dict) -> None:
    """Detail card for a completed-M&A event (acquirer/target/value/source)."""
    try:
        ep       = event_data.get("extendedProps", {})
        ticker   = ep.get("ticker", "")
        company  = ep.get("company_name", ticker)
        acquirer = ep.get("ma_acquirer") or "—"
        target   = ep.get("ma_target") or "—"
        deal     = (ep.get("ma_deal_type") or "M&A").title()
        value    = _fmt_ma_value(ep.get("ma_value_usd_m"))
        close_d  = _fmt_date(ep.get("ma_close_date") or event_data.get("start", ""))
        ann_d    = _fmt_date(ep.get("ma_announce_date")) if ep.get("ma_announce_date") else "—"
        bg, _txt = _kind_color("ma")

        src_href = _coerce_http_url(ep.get("source_ref"))
        src_label = ep.get("source") or "Source"
        src_value = html.escape(str(src_label)) if src_label else "—"
        if src_href:
            safe_href = html.escape(src_href, quote=True)
            src_value = (
                f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer" '
                f'style="color:#0066CC;text-decoration:none;font-weight:600;">'
                f'{html.escape(str(src_label) or "View filing")}</a>'
            )

        st.html(f"""
        <div class="ec-detail-card">
            <div class="ec-detail-title">{html.escape(str(company))}</div>
            <div class="ec-detail-sub">
                <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                             background:{bg};margin-right:6px;vertical-align:middle;"></span>
                {html.escape(str(ticker))} &nbsp;·&nbsp; M&amp;A Completion ({html.escape(deal)})
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Completion Date</span>
                <span class="ec-detail-val">{close_d}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Acquirer</span>
                <span class="ec-detail-val">{html.escape(str(acquirer))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Target</span>
                <span class="ec-detail-val">{html.escape(str(target))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Deal Type</span>
                <span class="ec-detail-val">{html.escape(deal)}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Transaction Value</span>
                <span class="ec-detail-val">{value}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Announced</span>
                <span class="ec-detail-val">{ann_d}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Source</span>
                <span class="ec-detail-val">{src_value}</span>
            </div>
        </div>
        """)
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_ma_detail_panel",
                             operation="render_ma_detail", context="M&A detail panel render")
        return None


def _render_detail_panel(event_data: Dict) -> None:
    try:
        import time as _t
        _t0 = _t.perf_counter()

        ep          = event_data.get("extendedProps", {})
        if ep.get("kind") == "ma":
            _render_ma_detail_panel(event_data)
            return
        ticker      = ep.get("ticker", "")
        company     = ep.get("company_name", ticker)
        fiscal_q    = ep.get("fiscal_q")
        fiscal_year = ep.get("fiscal_year")
        fqe         = ep.get("fiscal_qe", "")
        start_date  = event_data.get("start", "")
        market_cap  = ep.get("market_cap")
        report_time_display = _fmt_report_time_display(ep.get("report_time"))

        quarter_label = f"Q{fiscal_q} {fiscal_year}" if fiscal_q and fiscal_year else fqe or "—"
        # Dot matches the event's quarter color (falls back to company color if no quarter)
        _ec_kind = _earnings_kind(fiscal_q)
        company_color = _kind_color(_ec_kind)[0] if _ec_kind else _company_color(ticker)
        transcript_info = EarningsCalendarRepository.get_transcript_for_calendar_event(ticker, start_date)
        transcript_value = "—"
        if transcript_info:
            transcript_year = transcript_info["year"]
            transcript_quarter = f"Q{transcript_info['q']}"
            transcript_url = (
                f"/earnings_calls?ticker={ticker}"
                f"&year={transcript_year}"
                f"&quarter={transcript_quarter}"
                f"&from=calendar"
            )
            transcript_value = (
                f'<a href="{transcript_url}" target="_self" '
                f'style="color:#0066CC;text-decoration:none;font-weight:600;">'
                f'View Transcript</a>'
            )

        ir_href = _coerce_http_url(ep.get("ir_website_url"))
        ir_value = "—"
        if ir_href:
            safe_href = html.escape(ir_href, quote=True)
            ir_value = (
                f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer" '
                f'style="color:#0066CC;text-decoration:none;font-weight:600;">'
                f'Webcast</a>'
            )

        st.html(f"""
        <div class="ec-detail-card">
            <div class="ec-detail-title">{company}</div>
            <div class="ec-detail-sub">
                <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                             background:{company_color};margin-right:6px;vertical-align:middle;"></span>
                {ticker} &nbsp;·&nbsp; {quarter_label}
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Earnings Date</span>
                <span class="ec-detail-val">{start_date}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Report time</span>
                <span class="ec-detail-val">{report_time_display}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Fiscal Quarter End</span>
                <span class="ec-detail-val">{fqe}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Market Cap</span>
                <span class="ec-detail-val">{_fmt_market_cap(market_cap)}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">EPS Actual</span>
                <span class="ec-detail-val">{_fmt_eps(ep.get("eps_actual"))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">EPS Forecast</span>
                <span class="ec-detail-val">{_fmt_eps(ep.get("eps_forecast"))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Surprise</span>
                <span class="ec-detail-val">{_fmt_surprise(ep.get("surprise_pct"))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Transcript</span>
                <span class="ec-detail-val">{transcript_value}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Webcast</span>
                <span class="ec-detail-val">{ir_value}</span>
            </div>
        </div>
        """)

        _panel_ms = (_t.perf_counter() - _t0) * 1000
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_detail_panel",
                             operation="render_detail", context="detail panel render")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# WATCHLIST FILTERING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

import re as _re


def _normalize_company_name(value: str) -> str:
    return _re.sub(r"\s+", " ", (value or "").strip().lower())


def _ticker_variants(raw: str) -> set:
    """Return exact uppercase ticker and base ticker before the first '.'."""
    t = (raw or "").strip().upper()
    if not t:
        return set()
    variants = {t}
    if "." in t:
        variants.add(t.split(".")[0])
    return variants


def _watchlist_rows_to_filter_scope(rows: List[Dict]) -> Dict:
    """Build a matching scope from watchlist company rows.

    Stores both exact and base ticker variants so that ADS.DE matches ADS.
    Uses (ticker_variant, normalized_name) pairs for per-company disambiguation.
    has_named_members is True when any row carries a company_name — enables
    name-based disambiguation even when the watchlist has only one entry for
    a ticker that is shared globally (JD, LULU, TSCO each map to two companies).
    """
    ticker_variants: set = set()
    ticker_name_pairs: set = set()
    has_named_members = False
    for row in rows:
        t_raw = (row.get("ticker") or "").strip()
        if not t_raw:
            continue
        variants = _ticker_variants(t_raw)
        ticker_variants.update(variants)
        name = _normalize_company_name(row.get("company_name") or "")
        if name:
            has_named_members = True
            for v in variants:
                ticker_name_pairs.add((v, name))
    return {
        "ticker_variants": ticker_variants,
        "ticker_name_pairs": ticker_name_pairs,
        "has_named_members": has_named_members,
    }


def _event_matches_watchlist(event: Dict, scope: Dict) -> bool:
    if not scope or not scope.get("ticker_variants"):
        return False
    event_variants = _ticker_variants(event.get("ticker") or "")
    matched_variants = event_variants & scope["ticker_variants"]
    if not matched_variants:
        return False
    # When both sides carry company names, require a name match to avoid
    # cross-contaminating shared tickers (JD/LULU/TSCO each map to two companies).
    event_name = _normalize_company_name(event.get("company_name") or "")
    if event_name and scope.get("has_named_members"):
        for v in matched_variants:
            if (v, event_name) in scope["ticker_name_pairs"]:
                return True
        return False
    return True


def _filter_events_by_watchlist(events: List[Dict], watchlist_rows: List[Dict]) -> List[Dict]:
    if not watchlist_rows:
        return []
    scope = _watchlist_rows_to_filter_scope(watchlist_rows)
    return [e for e in events if _event_matches_watchlist(e, scope)]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PAGE
# ─────────────────────────────────────────────────────────────────────────────

def render_page() -> None:
    try:
        import time as _time
        _t0_page = _time.perf_counter()

        # Render styles FIRST - before anything else to prevent layout flash
        render_styles()
        st.set_page_config(page_title="Earnings Calendar", layout="wide")
        st.html(_get_css())

        # Reset company dropdown and watchlist filter on fresh navigation to this page
        if st.session_state.get("_active_page") != "earnings_calendar":
            st.session_state.pop("ec_company_filter", None)
            st.session_state.pop("ec_watchlist_filter", None)
            st.session_state["ec_active_watchlist_id"] = None
            st.session_state["ec_active_watchlist_name"] = ""
            # Reset the event-type legend filter so all kinds show on fresh entry
            st.session_state["ec_active_kinds"] = list(_ALL_KINDS)

        render_header(current_page="earnings_calendar")

        # Show loading placeholder immediately before any blocking DB calls
        _ecal_loading_hint = st.empty()
        _ecal_loading_hint.markdown(
            '<div style="display:flex;align-items:center;gap:12px;padding:32px 0 16px 0;">'
            '<div style="width:28px;height:28px;border:3px solid #eee;border-top:3px solid #d62e2f;'
            'border-radius:50%;animation:ecal-spin 0.8s linear infinite;"></div>'
            '<span style="font-family:Montserrat,sans-serif;font-size:15px;color:#888;">'
            'Loading earnings calendar&hellip;</span></div>'
            '<style>@keyframes ecal-spin{to{transform:rotate(360deg)}}</style>',
            unsafe_allow_html=True,
        )

        # ── session state defaults ────────────────────────────────────────────────
        _t_session = _time.perf_counter()
        if "ec_view" not in st.session_state:
            st.session_state.ec_view = "calendar"   # "calendar" | "year"
        if "ec_selected_event" not in st.session_state:
            st.session_state.ec_selected_event = None
        # ec_cal_version is incremented on Close so the calendar component
        # reinitialises (new key) and clears its stale eventClick state,
        # preventing an infinite rerun loop.
        if "ec_cal_version" not in st.session_state:
            st.session_state.ec_cal_version = 0
        # _ec_current_date tracks the date we pass as initialDate on every render.
        # We update it on event click so FullCalendar stays on the event's month.
        if "_ec_current_date" not in st.session_state:
            st.session_state._ec_current_date = date.today().isoformat()
        if "ec_active_watchlist_id" not in st.session_state:
            st.session_state.ec_active_watchlist_id = None
        if "ec_active_watchlist_name" not in st.session_state:
            st.session_state.ec_active_watchlist_name = ""
        # Event-type legend filter — set of active kinds ("Q1".."Q4","ma")
        if "ec_active_kinds" not in st.session_state:
            st.session_state.ec_active_kinds = list(_ALL_KINDS)

        # ── load available tickers + date range + all events (parallel — all independent) ─
        from concurrent.futures import ThreadPoolExecutor

        def _fetch_tickers():
            try:
                t = _time.perf_counter()
                result = EarningsCalendarRepository.get_available_tickers()
                return result
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_tickers",
                                     operation="fetch_tickers", context="parallel fetch")
                return []

        def _fetch_date_range():
            try:
                t = _time.perf_counter()
                result = EarningsCalendarRepository.get_date_range()
                return result
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_date_range",
                                     operation="fetch_date_range", context="parallel fetch")
                return (None, None)

        def _fetch_all_events():
            try:
                t = _time.perf_counter()
                # Pre-fetch ALL events (no ticker filter) — used when user has "All Companies" selected
                result = EarningsCalendarRepository.get_calendar_events(tickers=None)
                return result
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_all_events",
                                     operation="fetch_all_events", context="parallel fetch")
                return []

        def _fetch_all_ma():
            try:
                # Completed M&A events (separate feed; earnings logic untouched).
                # Small result set (~230 rows) — always fetch all, filter in memory.
                return EarningsCalendarRepository.get_ma_completion_events()
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_all_ma",
                                     operation="fetch_all_ma", context="parallel fetch")
                return []

        _t_parallel = _time.perf_counter()
        # Single loading UX: custom `_ecal_loading_hint` above (don't stack `st.spinner` with same message).
        with ThreadPoolExecutor(max_workers=4) as _ec_exec:
            _ticker_future = _ec_exec.submit(_fetch_tickers)
            _dr_future = _ec_exec.submit(_fetch_date_range)
            _events_future = _ec_exec.submit(_fetch_all_events)
            _ma_future = _ec_exec.submit(_fetch_all_ma)
            _te = _time.perf_counter(); all_tickers_meta = _ticker_future.result()
            log_timing("EC_PAGE_FETCH_TICKERS", (_time.perf_counter() - _te) * 1000, level="WARNING")
            _te = _time.perf_counter(); (min_date, max_date) = _dr_future.result()
            log_timing("EC_PAGE_FETCH_DATE_RANGE", (_time.perf_counter() - _te) * 1000, level="WARNING")
            _te = _time.perf_counter(); _prefetched_events = _events_future.result()
            log_timing("EC_PAGE_FETCH_ALL_EVENTS", (_time.perf_counter() - _te) * 1000,
                       details=f"events={len(_prefetched_events) if _prefetched_events else 0}", level="WARNING")
            _te = _time.perf_counter(); _prefetched_ma_events = _ma_future.result()
            log_timing("EC_PAGE_FETCH_ALL_MA", (_time.perf_counter() - _te) * 1000,
                       details=f"ma_events={len(_prefetched_ma_events) if _prefetched_ma_events else 0}", level="WARNING")
        log_timing("EC_PAGE_PARALLEL_TOTAL", (_time.perf_counter() - _t_parallel) * 1000, level="WARNING")

        if not all_tickers_meta or not max_date:
            _ecal_loading_hint.empty()
            st.error("No earnings calendar data found.")
            return

        del min_date  # unused — year range dropdowns removed; FullCalendar arrows handle navigation

        # ── build company dropdown ─────────────────────────────────────────────────
        _t_dropdown = _time.perf_counter()
        _all_opt      = "All Companies"
        ticker_map    = {f"{m['name']} ({m['ticker']})": m["ticker"] for m in all_tickers_meta}
        all_labels    = [_all_opt] + list(ticker_map.keys())

        # ── Company synchronization ───────────────────────────────────────────────
        _t_ticker_resolve = _time.perf_counter()
        # Priority: ?ticker= URL param > active_ticker session state (cross-page sync)
        # With DB validation and fallback to default ticker if invalid
        _url_ticker    = st.query_params.get("ticker", "")
        _active_ticker = st.session_state.get("active_ticker", "")

        # Validate ticker with fallback to default
        _validated_ticker, _was_fallback = validate_and_get_ticker(
            url_ticker=_url_ticker if _url_ticker else None,
            session_ticker=_active_ticker if _active_ticker else None,
            page_name="earnings_calendar"
        )

        _incoming = _validated_ticker

        # If fallback was applied, update the URL
        if _was_fallback:
            st.query_params["ticker"] = _validated_ticker

        # Always default to "All Companies" regardless of URL param or session ticker
        _default_idx = 0

        # ── on_change callback for company selectbox ─────────────────────────────
        def _on_company_change():
            try:
                _label = st.session_state.get("ec_company_filter", "")
                if _label and _label != _all_opt:
                    # Extract ticker from "Company Name (TICK)" label format
                    if "(" in _label and _label.endswith(")"):
                        _t = _label.rsplit("(", 1)[1].rstrip(")")
                        st.query_params["ticker"]              = _t
                        st.session_state.active_ticker         = _t
                        st.session_state._ec_cal_synced_ticker = _t
                # "All Companies" → carry forward existing ?ticker= URL param (don't clear it)
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_on_company_change",
                                     operation="company_change_callback",
                                     context=f"label={st.session_state.get('ec_company_filter', '')}")
                return

        # ── toolbar setup: email badge + watchlist list (one email lookup shared by both) ────
        _toolbar_user_email = _get_signed_in_user_email()
        _ec_tb_alerts_on = bool(
            _load_existing_alert_preferences(_toolbar_user_email).get("enabled", False)
        )

        try:
            _toolbar_watchlists = get_user_watchlists(_toolbar_user_email) if _toolbar_user_email else []
        except Exception as _wl_toolbar_exc:
            log_structured_error(
                _wl_toolbar_exc, page="earnings_calendar", component="render_page",
                operation="load_toolbar_watchlists", context=f"user={_toolbar_user_email}",
            )
            _toolbar_watchlists = []

        _no_wl_label = "— No watchlist —"
        _wl_options = [_no_wl_label] + [
            f"{wl['name']} ({wl.get('company_count', 0)} companies)"
            for wl in _toolbar_watchlists
        ]
        _wl_ids: List[Optional[int]] = [None] + [wl["id"] for wl in _toolbar_watchlists]
        _wl_names: List[str] = [""] + [wl["name"] for wl in _toolbar_watchlists]

        # Validate active watchlist still exists; reset stale state if deleted/hidden
        _cur_wl_id = st.session_state.ec_active_watchlist_id
        if _cur_wl_id is not None and _cur_wl_id not in _wl_ids:
            st.session_state.ec_active_watchlist_id = None
            st.session_state.ec_active_watchlist_name = ""
            st.session_state.pop("ec_watchlist_filter", None)
            _cur_wl_id = None

        _wl_default_idx = _wl_ids.index(_cur_wl_id) if _cur_wl_id in _wl_ids else 0

        def _on_watchlist_change():
            try:
                _sel = st.session_state.get("ec_watchlist_filter", "")
                if not _sel or _sel == _no_wl_label or _sel not in _wl_options:
                    st.session_state.ec_active_watchlist_id = None
                    st.session_state.ec_active_watchlist_name = ""
                else:
                    _idx = _wl_options.index(_sel)
                    st.session_state.ec_active_watchlist_id = _wl_ids[_idx]
                    st.session_state.ec_active_watchlist_name = _wl_names[_idx]
                st.session_state.ec_selected_event = None
                st.session_state.ec_cal_version += 1
            except Exception as exc:
                log_structured_error(
                    exc, page="earnings_calendar", component="_on_watchlist_change",
                    operation="watchlist_change_callback",
                    context=f"label={st.session_state.get('ec_watchlist_filter', '')}",
                )

        # ── filter bar ─────────────────────────────────────────────────────────────
        col_alerts, f1, f2_wl, f_toggle = st.columns([3, 3, 3, 2], gap="small", vertical_alignment="bottom")
        with col_alerts:
            # Compact button left; remainder of first column keeps spacing before Company (matches old empty column)
            _ec_btn_col, _ec_alerts_pad = st.columns([2, 2.65], gap="small")
            with _ec_btn_col:
                _ec_mail_btn_c, _ec_mail_badge_c = st.columns(
                    [2.72, 0.62], gap="small", vertical_alignment="top"
                )
                with _ec_mail_btn_c:
                    if st.button(
                        "Email Alerts",
                        key="ec_open_email_alerts",
                        type="primary",
                        width="stretch",
                    ):
                        _email_alerts_dialog(all_tickers_meta)
                with _ec_mail_badge_c:
                    st.html(_ec_toolbar_alert_status_html(_ec_tb_alerts_on))

        with f1:
            selected_label = st.selectbox(
                "Company",
                options=all_labels,
                index=_default_idx,
                key="ec_company_filter",
                on_change=_on_company_change,
            )
            if selected_label == _all_opt:
                selected_tickers = tuple(m["ticker"] for m in all_tickers_meta)
            else:
                selected_tickers = (
                    (ticker_map[selected_label],)
                    if selected_label in ticker_map
                    else tuple(m["ticker"] for m in all_tickers_meta)
                )

        with f2_wl:
            if _toolbar_watchlists:
                st.selectbox(
                    "Watchlist",
                    options=_wl_options,
                    index=_wl_default_idx,
                    key="ec_watchlist_filter",
                    on_change=_on_watchlist_change,
                )
            else:
                st.selectbox(
                    "Watchlist",
                    options=[_no_wl_label],
                    index=0,
                    key="ec_watchlist_filter",
                    disabled=True,
                    help="No watchlists found. Create one on the Screening page.",
                )

        with f_toggle:
            c1, c2 = st.columns(2, gap="small")
            with c1:
                if st.button(
                    "Month Wise",
                    width="stretch",
                    type="primary" if st.session_state.ec_view == "calendar" else "secondary",
                ):
                    st.session_state.ec_view = "calendar"
                    st.session_state.ec_selected_event = None
            with c2:
                if st.button(
                    "Year Wise",
                    width="stretch",
                    type="primary" if st.session_state.ec_view == "year" else "secondary",
                ):
                    st.session_state.ec_view = "year"
                    st.session_state.ec_selected_event = None

        # ── load events (all dates; FullCalendar arrows handle navigation) ────────
        _t_events = _time.perf_counter()
        _active_wl_id = st.session_state.ec_active_watchlist_id
        if _active_wl_id is not None:
            # Watchlist mode: fetch member rows then filter prefetched feeds in memory
            try:
                _wl_company_rows = get_watchlist_companies(_active_wl_id)
            except Exception as _wl_filter_exc:
                log_structured_error(
                    _wl_filter_exc, page="earnings_calendar", component="render_page",
                    operation="get_watchlist_companies", context=f"watchlist_id={_active_wl_id}",
                )
                _wl_company_rows = []
            events = _filter_events_by_watchlist(_prefetched_events, _wl_company_rows)
            ma_events = _filter_events_by_watchlist(_prefetched_ma_events, _wl_company_rows)
            _wl_active_name = st.session_state.ec_active_watchlist_name
            _wl_cal_count = len({e["ticker"] for e in events} | {e["ticker"] for e in ma_events})
            st.caption(
                f"Filtering by watchlist: **{_wl_active_name}** — "
                f"{len(_wl_company_rows)} {'company' if len(_wl_company_rows) == 1 else 'companies'}, "
                f"{_wl_cal_count} {'company' if _wl_cal_count == 1 else 'companies'} "
                f"found on this calendar."
            )
        elif selected_label == _all_opt:
            # "All Companies" selected — use the pre-fetched results (0ms — already in cache)
            events = _prefetched_events
            ma_events = _prefetched_ma_events
        else:
            # Specific company selected — fetch filtered earnings subset (hits Streamlit
            # cache); M&A is a small in-memory feed, filter it by the same tickers.
            with st.spinner(""):
                events = EarningsCalendarRepository.get_calendar_events(tickers=selected_tickers)
            _sel_set = set(selected_tickers)
            ma_events = [e for e in _prefetched_ma_events if e.get("ticker") in _sel_set]

        # ── event-type color legend (clickable filter; combines AND with Company /
        #     Watchlist above). Rendered before the no-data guard so the user can
        #     always re-enable a hidden type. ──────────────────────────────────────
        _render_event_type_legend()
        _active_kinds = set(st.session_state.get("ec_active_kinds") or _ALL_KINDS)

        # Apply the legend filter to each feed (earnings → by quarter; M&A → "ma")
        events = [e for e in events if _earnings_kind(e.get("fiscal_q")) in _active_kinds]
        ma_events = ma_events if "ma" in _active_kinds else []

        if not events and not ma_events:
            _ecal_loading_hint.empty()
            st.html('<div class="ec-no-data">No events match the selected filters. '
                    'Click a color in the legend above to show more.</div>')
            render_coresight_footer()
            return

        # ── event count ───────────────────────────────────────────────────────────
        _n_total = len(events) + len(ma_events)
        _n_companies = len({e['ticker'] for e in events} | {e['ticker'] for e in ma_events})
        _ma_suffix = f" &nbsp;·&nbsp; {len(ma_events):,} M&amp;A completion{'' if len(ma_events) == 1 else 's'}" if ma_events else ""
        st.html(f"""
        <div class="ec-event-count">
            {_n_total:,} events &nbsp;·&nbsp; {_n_companies} companies{_ma_suffix}
        </div>
        """)

        # ─────────────────────────────────────────────────────────────────────────
        # FULLCALENDAR — Month (dayGridMonth) or Year (multiMonthYear) view
        # ─────────────────────────────────────────────────────────────────────────
        from streamlit_calendar import calendar as st_calendar

        # Time from end of parallel fetch to here = dropdown + toolbar (watchlists,
        # email prefs) + ticker resolve + watchlist filtering.
        log_timing("EC_PAGE_TOOLBAR_AND_FILTER", (_time.perf_counter() - _t_dropdown) * 1000,
                   details=f"events_in_view={len(events)}", level="WARNING")
        _t_fc_prep = _time.perf_counter()
        fc_events    = _to_fullcalendar(events) + _ma_to_fullcalendar(ma_events)
        log_timing("EC_PAGE_TO_FULLCALENDAR", (_time.perf_counter() - _t_fc_prep) * 1000,
                   details=f"events={len(fc_events)}", level="WARNING")
        # Normalize to ISO date strings (earnings feed yields str dates, the M&A
        # feed yields date objects) so max() never compares mixed types.
        _dated       = [d[:10] for d in (
            [_to_iso(e["earnings_date"]) for e in events if e.get("earnings_date")] +
            [_to_iso(e["earnings_date"]) for e in ma_events if e.get("earnings_date")]
        ) if d]
        initial_date = max(_dated) if _dated else date.today().isoformat()
        is_year_view = st.session_state.ec_view == "year"

        if is_year_view:
            cal_key = f"earnings_cal_year_{st.session_state.ec_cal_version}"
            cal_options = {
                "initialView":       "multiMonthYear",
                "editable":          False,
                "selectable":        False,
                "headerToolbar": {
                    "left":   "prev,next today",
                    "center": "title",
                    "right":  "",
                },
                "buttonText":        {"today": "Today"},
                "multiMonthMaxColumns": 3,
                "dayMaxEvents":      3,
                "moreLinkClick":     "popover",
                "eventDisplay":      "block",
                "height":            "auto",
            }
        else:
            cal_key = f"earnings_cal_month_{st.session_state.ec_cal_version}"
            cal_options = {
                "initialView":   "dayGridMonth",
                "editable":      False,
                "selectable":    False,
                "headerToolbar": {
                    "left":   "prev,next today",
                    "center": "title",
                    "right":  "dayGridMonth,listMonth",
                },
                "buttonText":    {"today": "Today", "month": "Month", "listMonth": "List"},
                "dayMaxEvents":  4,
                "moreLinkClick": "popover",
                "eventDisplay":  "block",
                "height":        680,
                "contentHeight": 660,
            }

        # Always pass the tracked current date so FullCalendar re-initialises on
        # the correct month.  We update _ec_current_date on event click so when
        # Streamlit rerenders, the calendar opens back on the event's month.
        cal_options["initialDate"] = st.session_state._ec_current_date

        calendar_css = """
            .fc { font-family:'Roboto',sans-serif !important; background:#FFFFFF !important; }

            /* ── Toolbar ── */
            .fc-toolbar-title { font-family:'Montserrat',sans-serif !important; font-weight:700 !important; font-size:18px !important; }
            .fc-button-primary { background:#D62E2F !important; border-color:#D62E2F !important; text-transform:capitalize !important; font-size:13px !important; }
            .fc-button-primary:not(:disabled):active,
            .fc-button-primary:not(:disabled).fc-button-active { background:#A82020 !important; border-color:#A82020 !important; }
            .fc-today-button { background:#6C757D !important; border-color:#6C757D !important; }

            /* ── Day grid ── */
            .fc-day-today { background:rgba(214,46,47,0.06) !important; }
            .fc-event { cursor:pointer !important; font-size:11px !important; font-weight:500 !important; border:none !important; padding:1px 5px !important; border-radius:3px !important; }
            .fc-daygrid-day-number { font-size:12px !important; color:#2D2A29 !important; text-decoration:none !important; }
            .fc-col-header-cell-cushion { font-size:12px !important; font-weight:600 !important; color:#4F4F4F !important; text-transform:uppercase; letter-spacing:0.4px; text-decoration:none !important; }

            /* ── List view ── */
            .fc-list-event-title { font-size:13px !important; }
            .fc-list-event-time { display:none !important; }

            /* ── multiMonthYear ── */
            .fc-multimonth-title { font-family:'Montserrat',sans-serif !important; font-weight:700 !important; font-size:13px !important; text-transform:uppercase; letter-spacing:0.5px; color:#2D2A29 !important; }
            .fc-multimonth-daygrid .fc-daygrid-day-number { font-size:11px !important; }
        """

        # Keep the column COUNT constant (2 columns always) so React never remounts
        # FullCalendar.  When no event is selected the detail column is collapsed to
        # a negligible width so the calendar fills the full row.
        _has_panel = bool(st.session_state.ec_selected_event)
        cal_col, detail_col = st.columns([3, 1] if _has_panel else [1, 0.001], gap="medium")

        with cal_col:
            _t_fc_render = _time.perf_counter()
            # Default streamlit-calendar callbacks include `eventsSet`, which FullCalendar
            # fires whenever events are painted (initial load, month nav, etc.). Each call
            # posts to Streamlit and retriggers a full script rerun — feels like constant
            # reloads. We only need clicks for the detail panel.
            cal_result = st_calendar(
                events=fc_events,
                options=cal_options,
                custom_css=calendar_css,
                key=cal_key,
                callbacks=["eventClick"],
            )
            log_timing("EC_PAGE_STCALENDAR_RENDER", (_time.perf_counter() - _t_fc_render) * 1000,
                       details=f"events={len(fc_events)} view={st.session_state.ec_view}", level="WARNING")
        log_timing("EC_PAGE_RENDER_TOTAL", (_time.perf_counter() - _t0_page) * 1000,
                   details=f"events={len(fc_events)}", level="WARNING")

        if cal_result and cal_result.get("eventClick"):
            clicked_event = cal_result["eventClick"]["event"]
            # Compare as strings — JSON may coerce ids to int on one run and str on another.
            clicked_id = str(clicked_event.get("id", "") or "")
            current_id = str((st.session_state.ec_selected_event or {}).get("id", "") or "")
            if clicked_id != current_id:
                st.session_state.ec_selected_event = clicked_event
                # Track the event's month so initialDate keeps FullCalendar here
                _event_start = clicked_event.get("start", "")
                if _event_start:
                    st.session_state._ec_current_date = _event_start[:10]
                st.rerun()
            # else: same event already selected — skip to prevent infinite rerun loop

        with detail_col:
            if st.session_state.ec_selected_event:
                st.html('<div style="height:60px;"></div>')
                _render_detail_panel(st.session_state.ec_selected_event)
                if st.button("✕ Close", key="ec_close_detail"):
                    st.session_state.ec_selected_event = None
                    st.session_state.ec_cal_version   += 1
                    st.rerun()

        st.html('<div style="height:32px;"></div>')

        # Clear the early loading placeholder now that content is rendered
        _ecal_loading_hint.empty()

        render_coresight_footer()

    except Exception as exc:
        try:
            _ecal_loading_hint.empty()
        except Exception:
            pass
        log_structured_error(exc, page="earnings_calendar", component="render_page",
                             operation="page_render", context="top-level render failed")
        st.error("Earnings calendar is temporarily unavailable. Please try again.")
        return


try:
    render_page()
except Exception as exc:
    log_structured_error(exc, page="earnings_calendar", component="top_level",
                         operation="run_render_page", context="top-level invocation")
    st.error("Something went wrong loading the Earnings Calendar.")
