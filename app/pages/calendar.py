"""
Earnings Calendar Page - Coresight Research
============================================
Calendar (Month grid) + Year view of earnings announcements with EPS beat/miss
indicators and deep links to earnings call transcripts.
"""
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
import html
import re

import streamlit as st

from components.styles import hide_sidebar, render_styles
from components.navigation import render_header, render_coresight_footer
from core.auth_manager import require_auth, get_current_user, _auth_request_meta, log_auth_cookie_server_presence
from utils.server_logger import new_rerun_id, log_timing, PageLoadTracker, log_render_complete
import time as _time

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


def _month_view_date_range(anchor: date) -> Tuple[date, date]:
    """42-day Sunday-aligned FullCalendar month grid (vis_start .. vis_end inclusive)."""
    first_of_month = anchor.replace(day=1)
    days_back = (first_of_month.weekday() + 1) % 7
    vis_start = first_of_month - timedelta(days=days_back)
    vis_end = vis_start + timedelta(days=41)
    return vis_start, vis_end


def _year_view_date_range(anchor: date) -> Tuple[date, date]:
    """Jan 1 – Dec 31 for the anchor year."""
    return date(anchor.year, 1, 1), date(anchor.year, 12, 31)


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _resolve_visible_date_range() -> Tuple[date, date, str]:
    """Return (vis_start, vis_end_inclusive, source_tag) for DB prefetch."""
    anchor = _parse_iso_date(st.session_state.get("_ec_current_date")) or date.today()
    vis_start_raw = st.session_state.get("ec_visible_start")
    vis_end_excl_raw = st.session_state.get("ec_visible_end")

    if vis_start_raw and vis_end_excl_raw:
        vis_start = _parse_iso_date(vis_start_raw)
        vis_end_excl = _parse_iso_date(vis_end_excl_raw)
        if vis_start and vis_end_excl:
            vis_end_inclusive = vis_end_excl - timedelta(days=1)
            return vis_start, vis_end_inclusive, "session_datesSet"

    if st.session_state.get("ec_view") == "year":
        vis_start, vis_end = _year_view_date_range(anchor)
        return vis_start, vis_end, "computed_year"

    # Month + list views share the same visible window
    vis_start, vis_end = _month_view_date_range(anchor)
    return vis_start, vis_end, "computed_month"


def _month_grid_weeks(anchor: date) -> int:
    """Number of week-rows FullCalendar's dayGridMonth renders for `anchor`'s month
    (firstDay=Monday, fixedWeekCount=False): the grid starts on the Monday on/before
    the 1st and ends on the Sunday on/after the last day. Used to give the month view
    a DETERMINISTIC height — one that depends only on the calendar structure, never on
    how many events are shown — so an in-place company filter can't leave the
    streamlit_calendar iframe at a stale (too-tall) height with a blank gap below."""
    import calendar as _pycal
    first = anchor.replace(day=1)
    lead = first.weekday()  # Mon=0 … Sun=6 → cells before the 1st
    days = _pycal.monthrange(anchor.year, anchor.month)[1]
    return max(1, -(-(lead + days) // 7))  # ceil((lead + days) / 7)


def _window_events(evs: List[Dict], vstart: date, vend: date) -> List[Dict]:
    """Keep only events whose date falls in [vstart, vend] (inclusive).

    The page fetches the FULL deduped set once (for the true badge count that
    matches production) and windows it here in Python — ~ms — so the FullCalendar
    render only ever receives the visible month/year, never all 16k events.
    Earnings rows carry `earnings_date` as an ISO string; M&A rows carry a date
    object — handle both."""
    out: List[Dict] = []
    for e in evs:
        d = e.get("earnings_date")
        if isinstance(d, str):
            d = _parse_iso_date(d)
        elif isinstance(d, datetime):
            d = d.date()
        if d is not None and vstart <= d <= vend:
            out.append(e)
    return out


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
# EVENT-TYPE STYLE TOKENS  (Figma Make — Refine calendar page design v17)
# Light card fills, left accent bar, right-side quarter/type badge via CSS.
# ─────────────────────────────────────────────────────────────────────────────
_CORESIGHT_RED = "#D62E2F"

_TYPE_STYLE: Dict[str, Dict[str, str]] = {
    "earnings": {
        "card_bg": "rgba(240,249,255,0.5)",
        "text": "#024A70",
        "subtle": "#45556C",
        "accent": "#0084D1",
        "filter_bg": "#F0F9FF",
        "filter_text": "#024A70",
        "label": "Earnings",
        "chip_bg": "#EFF6FF",
        "chip_text": "#1D4ED8",
    },
    "ipo": {
        "card_bg": "rgba(236,253,245,0.5)",
        "text": "#004F3B",
        "subtle": "#009966",
        "accent": "#009966",
        "filter_bg": "#ECFDF5",
        "filter_text": "#004F3B",
        "label": "IPO",
        "chip_bg": "#ECFDF5",
        "chip_text": "#047857",
    },
    "ma": {
        "card_bg": "rgba(245,243,255,0.5)",
        "text": "#4D179A",
        "subtle": "#7F22FE",
        "accent": "#7F22FE",
        "filter_bg": "#F5F3FF",
        "filter_text": "#4D179A",
        "label": "M&A",
        "chip_bg": "#F5F3FF",
        "chip_text": "#6D28D9",
    },
    "delisted": {
        "card_bg": "rgba(254,242,242,0.5)",
        "text": "#82181A",
        "subtle": "#D62E2F",
        "accent": "#D62E2F",
        "filter_bg": "#FEF2F2",
        "filter_text": "#82181A",
        "label": "Delisted",
        "chip_bg": "#FEF2F2",
        "chip_text": "#B91C1C",
    },
}

# Per-type chip CSS class suffix (Figma activePill colors)
_TYPE_CHIP_CLASS: Dict[str, str] = {
    "earnings": "ec-chip-earnings",
    "ipo": "ec-chip-ipo",
    "ma": "ec-chip-ma",
    "delisted": "ec-chip-delisted",
}

_QUARTER_STYLE = {"bg": "#E2E8F0", "text": "#45556C"}

_EVENT_TYPES: List[Tuple[str, str]] = [
    ("earnings", "Earnings"),
    ("ipo", "IPO"),
    ("ma", "M&A"),
    ("delisted", "Delisted"),
]
_QUARTER_ITEMS: List[Tuple[str, str]] = [
    ("Q1", "Q1"), ("Q2", "Q2"), ("Q3", "Q3"), ("Q4", "Q4"),
]
_ALL_TYPES = tuple(k for k, _ in _EVENT_TYPES)
_ALL_QUARTERS = tuple(k for k, _ in _QUARTER_ITEMS)


def _selected_set(key: str, all_values) -> set:
    """Read a filter-selection list, distinguishing an explicit *empty* selection
    (``[]`` — the user deselected every pill) from an *uninitialised* one
    (``None`` — default to all). The old ``st.session_state.get(key) or list(all)``
    idiom treated ``[]`` as falsy, so removing the last pill silently re-selected
    everything. Selection state is always initialised to a list on page entry, so
    ``None`` here means genuinely-unset, never "user cleared it"."""
    stored = st.session_state.get(key)
    return set(all_values) if stored is None else set(stored)

_TYPE_HELP: Dict[str, str] = {
    "earnings": "Earnings announcement dates",
    "ipo": "IPO (first-listing) dates",
    "ma": "M&A completion dates",
    "delisted": "Delisted (went-private) dates",
}
_QUARTER_HELP: Dict[str, str] = {
    "Q1": "Q1 earnings only", "Q2": "Q2 earnings only",
    "Q3": "Q3 earnings only", "Q4": "Q4 earnings only",
}


def _kind_color(kind: str) -> Tuple[str, str]:
    """(accent, text) for detail-panel headers; maps legacy kind keys."""
    _map = {
        "Q1": "earnings", "Q2": "earnings", "Q3": "earnings", "Q4": "earnings",
        "ma": "ma", "ipo": "ipo", "delisted": "delisted",
    }
    et = _map.get(kind, kind)
    if et in _TYPE_STYLE:
        s = _TYPE_STYLE[et]
        return s["accent"], s["text"]
    return "#37474F", "#FFFFFF"


def _fc_class_names(event_type: str, quarter: Optional[str] = None) -> List[str]:
    """FullCalendar classNames driving card CSS (badge via ::after)."""
    classes = ["ec-card", f"ec-type-{event_type}"]
    if event_type == "delisted":
        classes.append("ec-no-badge")
    elif event_type == "ipo":
        classes.append("ec-badge-ipo")
    elif event_type == "ma":
        classes.append("ec-badge-ma")
    elif quarter in _ALL_QUARTERS:
        classes.append(f"ec-badge-{quarter}")
    return classes


def _fc_event_card(
    *,
    event_id: str,
    start: str,
    company_name: str,
    ticker: str,
    event_type: str,
    quarter: Optional[str],
    extended_props: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a FullCalendar event dict with Figma card styling."""
    style = _TYPE_STYLE.get(event_type, _TYPE_STYLE["earnings"])
    kind = quarter if event_type == "earnings" and quarter else event_type
    return {
        "id": event_id,
        "title": f"{company_name}\n{ticker or ''}".strip(),
        "start": start,
        "end": start,
        "backgroundColor": style["card_bg"],
        "borderColor": "transparent",
        "textColor": style["text"],
        "color": style["accent"],
        "classNames": _fc_class_names(event_type, quarter),
        "extendedProps": {
            **extended_props,
            "kind": kind,
            "event_type": event_type,
            "ticker": ticker,
            "company_name": company_name,
        },
    }


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


def _toggle_event_type(etype: str) -> None:
    """Filter pill — toggle an event type and remount calendar."""
    try:
        cur = _selected_set("ec_active_types", _ALL_TYPES)
        if etype in cur:
            cur.discard(etype)
        else:
            cur.add(etype)
        st.session_state.ec_active_types = [k for k in _ALL_TYPES if k in cur]
        # Quarters belong to Earnings (earnings HAS a quarter, not vice-versa).
        # Toggling Earnings off clears all quarters; toggling it back on restores them.
        if etype == "earnings":
            st.session_state.ec_active_quarters = (
                list(_ALL_QUARTERS) if "earnings" in cur else []
            )
        st.session_state.ec_selected_event = None
        st.session_state.ec_cal_version = st.session_state.get("ec_cal_version", 0) + 1
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_toggle_event_type",
                             operation="toggle_type", context=f"type={etype}")


def _toggle_quarter(q: str) -> None:
    """Filter pill — toggle a fiscal quarter (earnings only) and remount calendar."""
    try:
        cur = _selected_set("ec_active_quarters", _ALL_QUARTERS)
        if q in cur:
            cur.discard(q)
        else:
            cur.add(q)
        st.session_state.ec_active_quarters = [k for k in _ALL_QUARTERS if k in cur]
        st.session_state.ec_selected_event = None
        st.session_state.ec_cal_version = st.session_state.get("ec_cal_version", 0) + 1
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_toggle_quarter",
                             operation="toggle_quarter", context=f"quarter={q}")


def _toggle_filter_panel() -> None:
    st.session_state.ec_filters_open = not st.session_state.get("ec_filters_open", False)


def _toggle_section(name: str) -> None:
    """Collapse/expand one filter section (Event Type / Fiscal Quarter / Company).
    Pure UI — never remounts the calendar."""
    st.session_state[f"ec_sec_{name}"] = not st.session_state.get(f"ec_sec_{name}", True)


def _reset_filters() -> None:
    st.session_state.ec_active_types = list(_ALL_TYPES)
    st.session_state.ec_active_quarters = list(_ALL_QUARTERS)
    st.session_state.ec_company_filter = "All Companies"
    st.session_state.ec_selected_event = None
    st.session_state.ec_cal_version = st.session_state.get("ec_cal_version", 0) + 1


def _ec_shift_period(delta: int) -> None:
    """Prev/next month (or year in yearly view) for the custom toolbar."""
    try:
        d = _parse_iso_date(st.session_state.get("_ec_current_date")) or date.today()
        if st.session_state.get("ec_view") == "year":
            new = date(d.year + delta, d.month, min(d.day, 28))
        else:
            m, y = d.month + delta, d.year
            while m < 1:
                m += 12
                y -= 1
            while m > 12:
                m -= 12
                y += 1
            import calendar as _cal
            last = _cal.monthrange(y, m)[1]
            new = date(y, m, min(d.day, last))
        st.session_state._ec_current_date = new.isoformat()
        st.session_state.ec_visible_start = None
        st.session_state.ec_visible_end = None
        st.session_state.ec_selected_event = None
        st.session_state.ec_cal_version = st.session_state.get("ec_cal_version", 0) + 1
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_ec_shift_period",
                             operation="shift_period", context=f"delta={delta}")


def _set_ec_view(view: str) -> None:
    if st.session_state.get("ec_view") != view:
        st.session_state.ec_visible_start = None
        st.session_state.ec_visible_end = None
    st.session_state.ec_view = view
    st.session_state.ec_selected_event = None
    st.session_state.ec_cal_version = st.session_state.get("ec_cal_version", 0) + 1


def _render_filter_panel(
    all_labels: List[str],
    default_idx: int,
    on_company_change,
    wl_options: List[str],
    wl_default_idx: int,
    on_watchlist_change,
    n_matching: int,
) -> None:
    """Figma filter card: Filters btn + removable chips + 3-column expanded panel."""
    try:
        active_types = _selected_set("ec_active_types", _ALL_TYPES)
        active_q = _selected_set("ec_active_quarters", _ALL_QUARTERS)
        filters_open = st.session_state.get("ec_filters_open", False)
        _all_opt = all_labels[0] if all_labels else "All Companies"
        _company_selected = (st.session_state.get("ec_company_filter") or "") not in ("", _all_opt)
        # Filters button badge = number of filters currently SELECTED (active event
        # types + active quarters) — always shown so the count is visible at a glance.
        selected_count = len(active_types) + len(active_q)
        # (kept for the "Reset all" visibility + chip logic below)
        inactive = (
            (len(_ALL_TYPES) - len(active_types))
            + (len(_ALL_QUARTERS) - len(active_q))
            + (1 if _company_selected else 0)
        )

        with st.container(border=True, key="ec_filter_card"):
            # Single flex row (design: `flex items-center gap-3`) — chips are natural
            # width and left-packed, never stretched. Reset all is pushed right (ml-auto).
            with st.container(
                horizontal=True, vertical_alignment="center", gap="small", key="ec_filter_row"
            ):
                # Count badge lives INSIDE the button (design) — baked into the label
                # as a Streamlit badge, then restyled to a white circle via CSS.
                # Always shown: the number of filters currently selected.
                _flabel = f"Filters :gray-badge[{selected_count}]"
                st.button(
                    _flabel,
                    key="ec_filters_toggle",
                    type="primary" if filters_open else "secondary",
                    on_click=_toggle_filter_panel,
                )
                st.html('<div class="ec-vdiv"></div>')
                for etype, label in _EVENT_TYPES:
                    if etype in active_types:
                        st.button(
                            f"{label} ×",
                            key=f"ec_chip_type_{etype}",
                            on_click=_toggle_event_type,
                            args=(etype,),
                        )
                for q, ql in _QUARTER_ITEMS:
                    if q in active_q:
                        st.button(
                            f"{ql} ×",
                            key=f"ec_chip_q_{q}",
                            on_click=_toggle_quarter,
                            args=(q,),
                        )
                if inactive > 0:
                    st.button("Reset all", key="ec_reset_filters", on_click=_reset_filters)

            if filters_open:
                with st.container(border=True, key="ec_filter_expand"):
                    # Figma proportions: Event Type + Company wide, Fiscal Quarter narrow (~3:1:3)
                    # Each section is a native st.expander (HTML <details>) so
                    # collapse/expand is CLIENT-SIDE — instant, no page rerun.
                    exp = st.columns([3, 1, 3], gap="small")
                    with exp[0]:
                        with st.expander("Event Type", expanded=True):
                            for etype, label in _EVENT_TYPES:
                                on = etype in active_types
                                st.button(
                                    label,
                                    key=f"ec_exp_type_{etype}",
                                    type="primary" if on else "secondary",
                                    on_click=_toggle_event_type,
                                    args=(etype,),
                                    width="stretch",
                                )
                    with exp[1]:
                        with st.expander("Fiscal Quarter", expanded=True):
                            # 2×2 grid — buttons FILL the column (design node 24139:47638)
                            qr1c1, qr1c2 = st.columns(2, gap="small")
                            qr2c1, qr2c2 = st.columns(2, gap="small")
                            _qslots = [(qr1c1, "Q1"), (qr1c2, "Q2"), (qr2c1, "Q3"), (qr2c2, "Q4")]
                            for col, q in _qslots:
                                with col:
                                    st.button(
                                        q,
                                        key=f"ec_exp_q_{q}",
                                        type="primary" if q in active_q else "secondary",
                                        on_click=_toggle_quarter,
                                        args=(q,),
                                        width="stretch",
                                    )
                    with exp[2]:
                        with st.expander("Company", expanded=True):
                            # Native searchable dropdown: click to see EVERY company,
                            # type to narrow (client-side). Restores the previous
                            # calendar's company picker. All filtering stays in-memory.
                            st.selectbox(
                                "Company",
                                options=all_labels,
                                index=default_idx,
                                key="ec_company_filter",
                                on_change=on_company_change,
                                placeholder="Company or ticker...",
                                label_visibility="collapsed",
                            )
                            _sel = st.session_state.get("ec_company_filter") or ""
                            hint = (
                                f"{n_matching:,} matching events"
                                if _sel and _sel != _all_opt
                                else "All companies selected"
                            )
                            st.markdown(f'<div class="ec-filter-hint">{html.escape(hint)}</div>', unsafe_allow_html=True)
                            if wl_options and len(wl_options) > 1:
                                st.selectbox(
                                    "Watchlist",
                                    options=wl_options,
                                    index=wl_default_idx,
                                    key="ec_watchlist_filter",
                                    on_change=on_watchlist_change,
                                    label_visibility="collapsed",
                                )
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_filter_panel",
                             operation="render_filters", context="filter panel render")


def _render_calendar_toolbar(
    n_events: int,
    n_companies: int,
    n_ma: int,
    alerts_on: bool,
    open_email_dialog,
) -> None:
    """Figma calendar toolbar: month nav, stats, email alerts, legend, view switcher."""
    try:
        d = _parse_iso_date(st.session_state.get("_ec_current_date")) or date.today()
        _months = [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
        if st.session_state.get("ec_view") == "year":
            title = str(d.year)
        else:
            title = f"{_months[d.month - 1]} {d.year}"

        _view = st.session_state.get("ec_view", "calendar")
        with st.container(
            horizontal=True, vertical_alignment="center", gap="small", key="ec_toolbar_row"
        ):
            # Left: month nav (arrows flank the title)
            st.button("‹", key="ec_cal_prev", on_click=_ec_shift_period, args=(-1,))
            st.html(f'<div class="ec-cal-toolbar-title">{html.escape(title)}</div>')
            st.button("›", key="ec_cal_next", on_click=_ec_shift_period, args=(1,))
            # Right group (pushed to the far right via ml-auto)
            with st.container(
                horizontal=True, vertical_alignment="center", gap="medium", key="ec_toolbar_right"
            ):
                st.html(
                    f'<div class="ec-cal-stats">'
                    f'<span><b>{n_events:,}</b> <span class="ec-cal-stat-lbl">events</span></span>'
                    f'<span><b>{n_companies}</b> <span class="ec-cal-stat-lbl">companies</span></span>'
                    f'<span><b>{n_ma}</b> <span class="ec-cal-stat-lbl">M&amp;A</span></span>'
                    f'</div>'
                )
                if st.button("Email Alerts", key="ec_open_email_alerts"):
                    open_email_dialog()
                st.html('<div class="ec-vdiv"></div>')
                st.html(
                    '<div class="ec-cal-legend">'
                    '<span><i class="ec-dot ec-dot-earnings"></i>Earnings</span>'
                    '<span><i class="ec-dot ec-dot-ipo"></i>IPO</span>'
                    '<span><i class="ec-dot ec-dot-ma"></i>M&A</span>'
                    '<span><i class="ec-dot ec-dot-delisted"></i>Delisted</span>'
                    '</div>'
                )
                # Segmented view switcher (design: bg-slate-100 rounded-lg p-0.5)
                with st.container(
                    horizontal=True, vertical_alignment="center", gap="small", key="ec_view_switch"
                ):
                    for vid, vlabel in [("calendar", "Monthly"), ("year", "Yearly"), ("list", "List")]:
                        st.button(
                            vlabel,
                            key=f"ec_view_{vid}",
                            type="primary" if _view == vid else "secondary",
                            on_click=_set_ec_view,
                            args=(vid,),
                        )
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_calendar_toolbar",
                             operation="render_toolbar", context="calendar toolbar")


# ── Yearly view (custom Figma month-card grid) ───────────────────────────────
_YEAR_TYPE_COLOR = {"earnings": "#38BDF8", "ipo": "#6EE7B7", "ma": "#C4B5FD", "delisted": "#D62E2F"}
_YEAR_TYPE_ORDER = ("earnings", "ipo", "ma", "delisted")
_YEAR_MONTHS = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December")
_YEAR_DOW = ("M", "T", "W", "T", "F", "S", "S")


def _days_in_month(y: int, m: int) -> int:
    return (date(y + 1, 1, 1) - date(y, 12, 1)).days if m == 12 else (date(y, m + 1, 1) - date(y, m, 1)).days


def _render_year_grid(events, ma_events, ipo_events, delisted_events,
                      year: int, today: date, shown: int, total: int) -> str:
    """Custom yearly view — 3×4 grid of mini-month cards matching the Figma design
    (node 24139:48905): per-month type dots + count badge, per-day event tickers,
    current-month red highlight. Rendered as one HTML block (no FullCalendar)."""
    try:
        from collections import defaultdict
        by_day: Dict[Tuple[int, int], list] = defaultdict(list)
        m_types: Dict[int, set] = defaultdict(set)
        m_count: Dict[int, int] = defaultdict(int)
        for lst, typ in ((events, "earnings"), (ipo_events, "ipo"),
                         (ma_events, "ma"), (delisted_events, "delisted")):
            for e in lst or []:
                iso = _to_iso(e.get("earnings_date"))
                if not iso:
                    continue
                try:
                    dd = date.fromisoformat(str(iso)[:10])
                except (ValueError, TypeError):
                    continue
                if dd.year != year:
                    continue
                by_day[(dd.month, dd.day)].append((str(e.get("ticker") or ""), typ))
                m_types[dd.month].add(typ)
                m_count[dd.month] += 1
        prio = {t: i for i, t in enumerate(_YEAR_TYPE_ORDER)}
        cards = []
        for m in range(1, 13):
            is_cur = (m == today.month and year == today.year)
            dots = "".join(
                f'<span class="ec-ym-dot" style="background:{_YEAR_TYPE_COLOR[t]}"></span>'
                for t in _YEAR_TYPE_ORDER if t in m_types.get(m, ())
            )
            cnt = m_count.get(m, 0)
            badge = f'<span class="ec-ym-badge">{cnt:,}</span>' if cnt else ""
            head = (f'<div class="ec-ym-head"><span class="ec-ym-name">{_YEAR_MONTHS[m - 1]}</span>'
                    f'<span class="ec-ym-hr"><span class="ec-ym-dots">{dots}</span>{badge}</span></div>')
            wk = "".join(f"<span>{d}</span>" for d in _YEAR_DOW)
            offset = date(year, m, 1).weekday()          # Monday = 0
            ndays = _days_in_month(year, m)
            cells = ['<div class="ec-ym-day ec-ym-empty"></div>' for _ in range(offset)]
            for day in range(1, ndays + 1):
                evs = sorted(by_day.get((m, day), ()), key=lambda x: (prio.get(x[1], 9), x[0]))
                rows = ""
                for tk, typ in evs[:2]:
                    c = _YEAR_TYPE_COLOR[typ]
                    rows += (f'<div class="ec-ym-ev"><span class="ec-ym-evdot" style="background:{c}"></span>'
                             f'<span class="ec-ym-evtk" style="color:{c}">{html.escape(tk)}</span></div>')
                if len(evs) > 2:
                    rows += f'<div class="ec-ym-more">+{len(evs) - 2}</div>'
                evhtml = f'<div class="ec-ym-evs">{rows}</div>' if rows else ""
                is_today = is_cur and day == today.day
                cls = "ec-ym-day today" if is_today else "ec-ym-day"
                cells.append(f'<div class="{cls}"><span class="ec-ym-daynum">{day}</span>{evhtml}</div>')
            body = (f'<div class="ec-ym-body"><div class="ec-ym-wk">{wk}</div>'
                    f'<div class="ec-ym-days">{"".join(cells)}</div></div>')
            cards.append(f'<div class="ec-ym-card{" cur" if is_cur else ""}">{head}{body}</div>')
        del total  # denominator dropped — footer now shows the year-scoped count only
        footer = (f'<div class="ec-year-foot"><span>Showing <b>{shown:,}</b> '
                  f'events in {year}</span></div>')
        return f'<div class="ec-year-wrap"><div class="ec-year-grid">{"".join(cards)}</div>{footer}</div>'
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_year_grid",
                             operation="render_year", context=f"year={year}")
        return '<div class="ec-no-data">Unable to render the yearly view.</div>'


# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

def _get_calendar_css() -> str:
    """FullCalendar iframe CSS — Figma card events, today indicator, list view."""
    qbg, qtx = _QUARTER_STYLE["bg"], _QUARTER_STYLE["text"]
    return f"""
            .fc {{ font-family:'Inter',sans-serif !important; background:#FFFFFF !important; }}

            /* Hide FullCalendar built-in toolbar — custom Figma toolbar above iframe */
            .fc-header-toolbar {{ display:none !important; }}

            /* Monday-first week */
            .fc-col-header-cell-cushion {{
                font-size:10px !important; font-weight:600 !important; color:#90A1B9 !important;
                text-transform:uppercase; letter-spacing:1px; text-decoration:none !important;
            }}
            .fc-col-header-cell.fc-day-sat .fc-col-header-cell-cushion,
            .fc-col-header-cell.fc-day-sun .fc-col-header-cell-cushion {{ color:#CAD5E2 !important; }}
            .fc-theme-standard .fc-col-header-cell {{
                background:#F8FAFC !important; border-color:#E2E8F0 !important;
            }}
            .fc-col-header-cell .fc-scrollgrid-sync-inner {{ padding:3px 0 !important; }}

            /* ── Today — Coresight red top accent (Figma) ── */
            .fc-daygrid-day.fc-day-today {{
                background:rgba(214,46,47,0.04) !important;
                box-shadow:inset 0 2px 0 0 {_CORESIGHT_RED} !important;
            }}
            .fc-daygrid-day.fc-day-today .fc-daygrid-day-number {{
                color:{_CORESIGHT_RED} !important; font-weight:700 !important;
            }}
            .fc-multimonth .fc-daygrid-day.fc-day-today .fc-daygrid-day-number {{
                color:{_CORESIGHT_RED} !important; font-weight:700 !important;
            }}

            /* ── Day grid chrome ── */
            .fc-daygrid-day-number {{
                font-size:11px !important; font-weight:600 !important; color:#45556C !important;
                text-decoration:none !important; padding:4px 6px !important; float:none !important;
            }}
            .fc-daygrid-day-top {{
                justify-content:flex-start !important; flex-direction:row !important;
            }}
            .fc-day-other .fc-daygrid-day-frame {{
                background:rgba(248,250,252,0.4) !important;
            }}
            .fc-day-other .fc-daygrid-day-number {{ color:#CAD5E2 !important; }}
            /* Adjacent-month days show only the faded number — no events (design) */
            .fc-day-other .fc-daygrid-day-events,
            .fc-day-other .fc-daygrid-day-bottom {{ display:none !important; }}
            /* Weekend cells — faint slate wash + muted day number (design) */
            .fc-daygrid-day.fc-day-sat:not(.fc-day-other) .fc-daygrid-day-frame,
            .fc-daygrid-day.fc-day-sun:not(.fc-day-other) .fc-daygrid-day-frame {{
                background:rgba(248,250,252,0.6) !important;
            }}
            .fc-day-sat:not(.fc-day-other) .fc-daygrid-day-number,
            .fc-day-sun:not(.fc-day-other) .fc-daygrid-day-number {{ color:#90A1B9 !important; }}
            /* Outer grid border comes from the iframe (rounded card); drop the grid's
               own outer border to avoid a double line, keep it clipped to the radius. */
            .fc-scrollgrid {{ border:none !important; border-radius:16px !important; overflow:hidden !important; }}
            .fc {{ border-radius:16px !important; overflow:hidden !important; }}
            .fc-daygrid-day {{ border-color:#F1F5F9 !important; }}

            /* ── Event cards (month + year) ── */
            .fc-event.ec-card {{
                cursor:pointer !important; border:none !important;
                border-radius:8px !important; padding:0 !important;
                margin-bottom:2px !important; overflow:hidden !important;
                box-shadow:none !important; transition:filter .12s ease !important;
            }}
            /* Pastel background tint per type (design: fill {{type}}-50 @ 50%) */
            .fc-event.ec-type-earnings {{ background:rgba(240,249,255,0.5) !important; }}
            .fc-event.ec-type-ipo {{ background:rgba(236,253,245,0.5) !important; }}
            .fc-event.ec-type-ma {{ background:rgba(245,243,255,0.5) !important; }}
            .fc-event.ec-type-delisted {{ background:rgba(254,242,242,0.5) !important; }}
            .fc-event.ec-card:hover {{ filter:brightness(0.97) !important; }}
            .fc-event.ec-card .fc-event-main {{ padding:0 !important; }}
            .fc-event.ec-card .fc-event-title-container {{ padding:0 !important; }}
            .fc-event.ec-card .fc-event-title {{
                white-space:pre-line !important; font-size:9px !important;
                font-weight:400 !important; line-height:1.3 !important;
                padding:3px 34px 3px 6px !important; position:relative !important;
                min-height:28px !important;
            }}
            /* Wider badges (M&A / IPO) need extra right room so the company name never underlaps */
            .fc-event.ec-badge-ma .fc-event-title,
            .fc-event.ec-badge-ipo .fc-event-title {{ padding-right:46px !important; }}
            .fc-event.ec-card .fc-event-title::first-line {{
                font-weight:600 !important; font-size:10px !important;
            }}
            /* Ticker line = category-600, company (first line) = category-900 (Figma) */
            .fc-event.ec-type-earnings .fc-event-title {{ color:#0084D1 !important; }}
            .fc-event.ec-type-earnings .fc-event-title::first-line {{ color:#024A70 !important; }}
            .fc-event.ec-type-ipo .fc-event-title {{ color:#009966 !important; }}
            .fc-event.ec-type-ipo .fc-event-title::first-line {{ color:#004F3B !important; }}
            .fc-event.ec-type-ma .fc-event-title {{ color:#7008E7 !important; }}
            .fc-event.ec-type-ma .fc-event-title::first-line {{ color:#4D179A !important; }}
            .fc-event.ec-type-delisted .fc-event-title {{ color:#E7000B !important; }}
            .fc-event.ec-type-delisted .fc-event-title::first-line {{ color:#9F0712 !important; }}
            .fc-event.ec-type-earnings {{ border-left:2px solid #38BDF8 !important; }}
            .fc-event.ec-type-ipo {{ border-left:2px solid #6EE7B7 !important; }}
            .fc-event.ec-type-ma {{ border-left:2px solid #C4B5FD !important; }}
            .fc-event.ec-type-delisted {{ border-left:2px solid #D62E2F !important; }}
            .fc-event.ec-card .fc-event-title::after {{
                position:absolute; right:6px; top:50%; bottom:auto; transform:translateY(-50%);
                font-size:9px; font-weight:700; padding:2px 6px; border-radius:4px;
                background:{qbg}; color:{qtx}; line-height:1.1;
            }}
            .fc-event.ec-badge-Q1 .fc-event-title::after {{ content:"Q1"; }}
            .fc-event.ec-badge-Q2 .fc-event-title::after {{ content:"Q2"; }}
            .fc-event.ec-badge-Q3 .fc-event-title::after {{ content:"Q3"; }}
            .fc-event.ec-badge-Q4 .fc-event-title::after {{ content:"Q4"; }}
            .fc-event.ec-badge-ipo .fc-event-title::after {{
                content:"IPO"; background:#D0FAE5; color:#004F3B;
            }}
            .fc-event.ec-badge-ma .fc-event-title::after {{
                content:"M&A"; background:#EDE9FE; color:#4D179A;
            }}
            .fc-event.ec-no-badge .fc-event-title::after {{ display:none !important; }}

            /* ── List view rows ── */
            .fc-list-event.ec-card {{
                border-left:3px solid #0084D1 !important; margin-bottom:2px !important;
                border-radius:0 6px 6px 0 !important;
            }}
            .fc-list-event.ec-type-earnings {{ border-left-color:#38BDF8 !important; }}
            .fc-list-event.ec-type-ipo {{ border-left-color:#6EE7B7 !important; }}
            .fc-list-event.ec-type-ma {{ border-left-color:#C4B5FD !important; }}
            .fc-list-event.ec-type-delisted {{ border-left-color:#D62E2F !important; }}
            .fc-list-event-time {{ display:none !important; }}
            .fc-list-event-title {{
                white-space:pre-line !important; font-size:13px !important;
                font-weight:600 !important; position:relative !important;
                padding-right:40px !important;
            }}
            .fc-list-event.ec-card .fc-list-event-title::after {{
                position:absolute; right:8px; top:50%; transform:translateY(-50%);
                font-size:10px; font-weight:700; padding:2px 6px; border-radius:4px;
                background:{qbg}; color:{qtx};
            }}
            .fc-list-event.ec-badge-Q1 .fc-list-event-title::after {{ content:"Q1"; }}
            .fc-list-event.ec-badge-Q2 .fc-list-event-title::after {{ content:"Q2"; }}
            .fc-list-event.ec-badge-Q3 .fc-list-event-title::after {{ content:"Q3"; }}
            .fc-list-event.ec-badge-Q4 .fc-list-event-title::after {{ content:"Q4"; }}
            .fc-list-event.ec-badge-ipo .fc-list-event-title::after {{
                content:"IPO"; background:#D1FAE5; color:#064E3B;
            }}
            .fc-list-event.ec-badge-ma .fc-list-event-title::after {{
                content:"M&A"; background:#EDE9FE; color:#4C1D95;
            }}
            .fc-list-event.ec-no-badge .fc-list-event-title::after {{ display:none !important; }}
            .fc-list-event-dot {{ width:0 !important; border-width:0 !important; }}
            .fc-list-day-cushion {{
                font-family:'Montserrat',sans-serif !important; font-weight:600 !important;
                font-size:12px !important; color:#334155 !important;
                background:#F8FAFC !important;
            }}
            .fc-list-table {{ border-color:#E2E8F0 !important; }}

            /* ── multiMonthYear (year view) ── */
            .fc-multimonth-title {{
                font-family:'Montserrat',sans-serif !important; font-weight:700 !important;
                font-size:13px !important; text-transform:uppercase; letter-spacing:0.5px;
                color:#1E293B !important;
            }}
            .fc-multimonth-daygrid .fc-daygrid-day-number {{ font-size:11px !important; }}
            .fc-multimonth {{ border-color:#E2E8F0 !important; border-radius:8px !important; }}
        """


def _get_css() -> str:
    try:
        return """
<style>

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
/* Detail card lives in its OWN right-hand column beside the calendar (st.columns
   [3,1]); it is never a floating overlay and never sits on top of the grid. */
.st-key-ec_detail_col { padding-top:2px !important; }
.st-key-ec_detail_col .stButton { margin-top:10px !important; }
.ec-detail-card {
    background:#FFFFFF; border:1px solid #E5E5E5; border-radius:12px;
    padding:20px; box-shadow:0 8px 24px rgba(0,0,0,0.12);
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

/* Email alerts — modal dialog (Figma node 24291-205047: 528px card, slate palette) */
div[data-testid="stDialog"] div[role="dialog"] {
    width:602px !important;
    max-width:96vw !important;
    border-radius:16px !important;
}
/* Header: 20/700 slate-900 title + full-width divider under it */
div[data-testid="stDialog"] div[role="dialog"] > div:first-child {
    border-bottom:1px solid #E2E8F0 !important;
    padding-bottom:14px !important;
}
div[data-testid="stDialog"] div[role="dialog"] > div:first-child span,
div[data-testid="stDialog"] div[role="dialog"] > div:first-child div {
    font-family:'Inter',sans-serif !important;
    font-size:20px !important; font-weight:700 !important; color:#0F172B !important;
}
div[role="dialog"] [data-testid="stVerticalBlock"] {
    gap:0.75rem !important;
}
div[role="dialog"] [data-testid="stVerticalBlock"] > div {
    gap:0.75rem !important;
}
div[role="dialog"] [data-testid="stElementContainer"] {
    margin-bottom:0 !important;
}
div[role="dialog"] [data-testid="stWidgetLabel"] {
    margin-bottom:0.1rem !important;
    min-height:0 !important;
    padding-bottom:0 !important;
}
div[role="dialog"] [data-testid="stCaptionContainer"] {
    margin-top:-0.1rem !important;
    margin-bottom:0 !important;
}
div[role="dialog"] .stButton { margin-top:0.35rem !important; }
/* Save (primary) — Coresight D6 red, full-width (design) */
div[role="dialog"] button[data-testid="stBaseButton-primary"],
div[role="dialog"] .stButton > button[kind="primary"] {
    background-color: #D62E2F !important;
    border-color: #D62E2F !important;
    color: #FFFFFF !important;
}
div[role="dialog"] button[data-testid="stBaseButton-primary"]:hover,
div[role="dialog"] .stButton > button[kind="primary"]:hover {
    background-color: #b82526 !important;
    border-color: #b82526 !important;
    color: #FFFFFF !important;
}
.st-key-ec_save_alert_preferences,
.st-key-ec_save_alert_preferences > div,
.st-key-ec_save_alert_preferences button {
    width:100% !important;
}
.st-key-ec_save_alert_preferences button {
    min-height:46px !important; border-radius:10px !important;
    font-family:'Inter',sans-serif !important;
    font-size:16px !important; font-weight:600 !important;
}
div[role="dialog"] div[data-testid="stRadio"] fieldset {
    margin:0 !important;
    padding:0.15rem 0 0 0 !important;
}
div[role="dialog"] div[data-testid="stRadio"] [role="radiogroup"] {
    gap:1.4rem !important;
    min-height:0 !important;
}
div[role="dialog"] [data-testid="stWidgetLabel"] p,
div[role="dialog"] [data-testid="stWidgetLabel"] label {
    color:#0F172B !important;
}
div[role="dialog"] div[data-testid="stRadio"] label,
div[role="dialog"] div[data-testid="stRadio"] span {
    color:#0F172B !important;
}
div[role="dialog"] div[data-testid="stRadio"] [role="radiogroup"] p {
    font-size:15px !important; color:#0F172B !important;
    font-family:'Inter',sans-serif !important;
}
div[role="dialog"] [data-testid="stCheckbox"] label,
div[role="dialog"] [data-testid="stCheckbox"] span {
    color:#0F172B !important;
}
div[role="dialog"] [data-testid="stCheckbox"] p {
    font-size:15px !important; color:#0F172B !important;
    font-family:'Inter',sans-serif !important;
}
div[role="dialog"] [data-testid="stCaption"] {
    color:#62748E !important;
}
/* Days-before row — label left, compact segmented [− | 1 | +] stepper right (design) */
div[role="dialog"] [data-testid="stNumberInput"] {
    display:flex !important; flex-direction:row !important;
    align-items:center !important; justify-content:space-between !important;
    gap:12px !important;
}
div[role="dialog"] [data-testid="stNumberInput"] > [data-testid="stWidgetLabel"] {
    flex:0 1 auto !important; margin-bottom:0 !important;
}
div[role="dialog"] [data-testid="stNumberInput"] > [data-testid="stWidgetLabel"] p {
    font-size:15px !important; color:#0F172B !important;
    font-family:'Inter',sans-serif !important;
}
div[role="dialog"] [data-testid="stNumberInputContainer"] {
    flex:0 0 auto !important; width:140px !important; margin-left:auto !important;
    display:flex !important; align-items:stretch !important;
    border:1px solid #E2E8F0 !important; border-radius:10px !important;
    background:#FFFFFF !important; overflow:hidden !important; height:44px !important;
}
/* Step buttons live in a wrapper div — flatten it so `order` can split − and + */
div[role="dialog"] [data-testid="stNumberInputContainer"] > div:not([data-baseweb="input"]) {
    display:contents !important;
}
div[role="dialog"] [data-testid="stNumberInputContainer"] > div[data-baseweb="input"] {
    order:0 !important; flex:1 1 auto !important;
    border:none !important; background:#FFFFFF !important;
    border-radius:0 !important;
}
div[role="dialog"] [data-testid="stNumberInputContainer"] div[data-baseweb="base-input"] {
    border:none !important; background:#FFFFFF !important;
}
div[role="dialog"] [data-testid="stNumberInput"] input {
    color:#0F172B !important; text-align:center !important;
    font-size:15px !important; font-weight:600 !important;
    font-family:'Inter',sans-serif !important;
    background:#FFFFFF !important; padding:0 !important;
}
div[role="dialog"] [data-testid="stNumberInputContainer"] button {
    background:#FFFFFF !important; color:#62748E !important;
    border:none !important; border-radius:0 !important;
    width:42px !important; flex:0 0 42px !important; min-height:0 !important;
}
div[role="dialog"] [data-testid="stNumberInputContainer"] button:hover {
    background:#F8FAFC !important; color:#0F172B !important;
}
div[role="dialog"] [data-testid="stNumberInputContainer"] button[data-testid="stNumberInputStepDown"] {
    order:-1 !important; border-right:1px solid #E2E8F0 !important;
}
div[role="dialog"] [data-testid="stNumberInputContainer"] button[data-testid="stNumberInputStepUp"] {
    order:1 !important; border-left:1px solid #E2E8F0 !important;
}
/* Toolbar — Email Alerts in dialog only (calendar toolbar uses white outline btn) */

/* ── Figma: NO outer card around filter or calendar (design has none) ── */
/* NOTE: st.container(border=True) draws the border directly on the .st-key-* block. */
/* Calendar SECTION: no box. A FULL-WIDTH top line + a slight-grey band that bleeds
   edge-to-edge behind the toolbar + grid (design). The grid (iframe) stays centered. */
.st-key-ec_cal_card {
    border:none !important; border-radius:0 !important; box-shadow:none !important;
    background:transparent !important; padding:16px 0 24px 0 !important; position:relative !important;
}
.st-key-ec_cal_card::before {
    content:'' !important; position:absolute !important; top:0 !important;
    left:calc(-50vw + 50%) !important; width:100vw !important; height:100% !important;
    background:#F8FAFC !important; border-top:1px solid #E2E8F0 !important;
    z-index:0 !important; pointer-events:none !important;
}
.st-key-ec_cal_card > div { position:relative !important; z-index:1 !important; }
/* The month/list grid iframe = rounded bordered card with shadow (design grid r16). */
.st-key-ec_cal_card iframe {
    border:1px solid #E2E8F0 !important; border-radius:16px !important;
    box-shadow:0 1px 2px -1px rgba(0,0,0,0.10), 0 1px 3px 0 rgba(0,0,0,0.10) !important;
    background:#FFFFFF !important; overflow:hidden !important;
}
/* Filter area: flat, no bordered box — Filters button + chips sit on the page,
   only the inner 3-section panel (ec_filter_expand) is a box. */
.st-key-ec_filter_card {
    border:none !important; border-radius:0 !important; box-shadow:none !important;
    background:transparent !important; padding:0 !important; margin-bottom:14px !important;
}
.st-key-ec_filter_expand {
    border:1px solid #E2E8F0 !important; border-radius:14px !important;
    margin-top:10px !important; padding:0 !important; overflow:hidden !important;
    box-shadow:0 1px 2px -1px rgba(0,0,0,0.10), 0 1px 3px 0 rgba(0,0,0,0.10) !important;
    background:#FFFFFF !important;
}
/* Zero the inter-column gap so the section dividers sit flush (design: divide-x) */
.st-key-ec_filter_expand [data-testid="stHorizontalBlock"] { gap:0 !important; }
.st-key-ec_filter_expand [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    border-right:1px solid #F1F5F9 !important; padding:12px 14px 14px !important;
}
.st-key-ec_filter_expand [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child {
    border-right:none !important;
}
/* Nested columns inside a section expander (Fiscal Quarter Q1–Q4 grid) must NOT
   inherit the panel-column divider/padding — that was crushing the Q buttons. */
.st-key-ec_filter_expand [data-testid="stExpanderDetails"] [data-testid="stColumn"] {
    border-right:none !important; padding:0 !important;
}
.st-key-ec_filter_expand [data-testid="stExpanderDetails"] [data-testid="stHorizontalBlock"] {
    gap:8px !important; width:100% !important;
}
/* Make the Q grid fill the Fiscal Quarter column (no dead space on the right) */
.st-key-ec_filter_expand [data-testid="stExpanderDetails"],
.st-key-ec_filter_expand [data-testid="stExpanderDetails"] > [data-testid="stVerticalBlock"],
.st-key-ec_filter_expand [data-testid="stExpanderDetails"] [data-testid="stColumn"] > [data-testid="stVerticalBlock"] {
    width:100% !important;
}
/* Email alert saved-state display (design: subtle slate pill, soft emerald when on) */
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
    min-height:34px;
    padding:7px 16px;
    border-radius:10px;
    font-family:'Inter',sans-serif;
    font-size:13px;
    font-weight:500;
    line-height:1.2;
    border:1px solid #E2E8F0;
    color:#45556C;
    background:#F8FAFC;
    text-align:center;
    white-space:nowrap;
}
.ec-alerts-status-pill--on {
    border-color:#A7F3D0;
    color:#047857;
    background:#ECFDF5;
}
.ec-alerts-lede {
    font-size:14px; color:#62748E; margin:0 0 2px 0; line-height:1.45;
    font-family:'Inter',sans-serif;
}
.ec-alerts-lede strong { color:#0F172B; }
.ec-alerts-foot {
    font-size:13px; color:#90A1B9; line-height:1.5; margin:8px 0 0 0;
    font-family:'Inter',sans-serif;
}
.ec-alerts-hint {
    font-size:14px; color:#62748E; line-height:1.45; margin:0;
    font-family:'Inter',sans-serif;
}
.ec-alerts-hint strong { color:#0F172B; }
.ec-alerts-section {
    font-family:'Inter',sans-serif;
    font-size:12px; font-weight:600; color:#62748E;
    text-transform:uppercase; letter-spacing:0.07em;
    margin:14px 0 4px 0;
}
.ec-alerts-callout {
    font-size:14px; color:#314158; line-height:1.55;
    background:#F8FAFC; border:1px solid #E2E8F0;
    border-radius:12px; padding:14px 16px; margin:8px 0 10px 0;
    font-family:'Inter',sans-serif;
}
.ec-alerts-callout--on {
    background:#ECFDF5; border-color:#A7F3D0; color:#065F46;
}
.ec-alerts-callout strong { color:#0F172B; }
.ec-alerts-callout--on strong { color:#065F46; }

div[data-testid="stSelectbox"] label { font-size:11px !important; font-weight:500 !important; color:#6B6B6B !important; }
div[role="dialog"] div[data-testid="stSelectbox"] label {
    color:#0F172B !important;
}

/* ── Figma filter controls ── */
.ec-filter-section-label {
    font-family:'Inter',sans-serif; font-size:10px; font-weight:600;
    color:#64748B; text-transform:uppercase; letter-spacing:0.08em;
    margin-bottom:10px;
}
.ec-filter-hint {
    font-family:'Inter',sans-serif; font-size:10px; color:#90A1B9;
    line-height:1.5; margin-top:8px;
}

/* Filters toggle button */
.st-key-ec_filters_toggle button[kind="primary"] {
    background:#2D2A29 !important; border:1px solid #2D2A29 !important; color:#FFFFFF !important;
    font-family:'Inter',sans-serif !important; font-weight:500 !important; font-size:12px !important;
    border-radius:10px !important; min-height:30px !important;
}
.st-key-ec_filters_toggle button[kind="secondary"] {
    background:#FFFFFF !important; border:1px solid #E2E8F0 !important; color:#62748E !important;
    font-family:'Inter',sans-serif !important; font-weight:500 !important; font-size:12px !important;
    border-radius:10px !important; min-height:30px !important;
}

/* Removable chips (active filters row) — Figma: pastel dot + label + × */
/* Natural (content) width, left-packed — never stretch as filters are removed. */
div[class*="st-key-ec_chip_type_"],
div[class*="st-key-ec_chip_q_"] { width:auto !important; flex:0 0 auto !important; }
div[class*="st-key-ec_chip_type_"] button,
div[class*="st-key-ec_chip_q_"] button {
    font-family:'Inter',sans-serif !important; font-weight:500 !important; font-size:11px !important;
    line-height:16.5px !important;
    border-radius:999px !important; min-height:21px !important; padding:2px 6px 2px 8px !important;
    width:auto !important; min-width:0 !important; white-space:nowrap !important;
    border:1px solid transparent !important; box-shadow:none !important;
    display:inline-flex !important; align-items:center !important; justify-content:center !important; gap:4px !important;
}
div[class*="st-key-ec_chip_type_"] button::before {
    content:''; width:6px; height:6px; border-radius:50%; flex:0 0 auto;
}
.st-key-ec_chip_type_earnings button { background:#F0F9FF !important; color:#0069A8 !important; }
.st-key-ec_chip_type_earnings button::before { background:#38BDF8 !important; }
.st-key-ec_chip_type_ipo button { background:#ECFDF5 !important; color:#007A55 !important; }
.st-key-ec_chip_type_ipo button::before { background:#6EE7B7 !important; }
.st-key-ec_chip_type_ma button { background:#F5F3FF !important; color:#7008E7 !important; }
.st-key-ec_chip_type_ma button::before { background:#C4B5FD !important; }
.st-key-ec_chip_type_delisted button { background:#FEF2F2 !important; color:#C10007 !important; }
.st-key-ec_chip_type_delisted button::before { background:#D62E2F !important; }
div[class*="st-key-ec_chip_q_"] button {
    background:#E2E8F0 !important; color:#45556C !important; font-weight:600 !important;
}
.st-key-ec_reset_filters button {
    font-size:11px !important; color:#90A1B9 !important; background:transparent !important;
    border:none !important; box-shadow:none !important; font-weight:500 !important;
}

/* Expanded event-type rows (Figma: pastel dot + label, left-aligned) */
div[class*="st-key-ec_exp_type_"] button {
    justify-content:flex-start !important; text-align:left !important; border:none !important;
    border-radius:10px !important; display:flex !important; align-items:center !important; gap:10px !important;
    padding:8px 12px !important; min-height:32px !important;
    font-family:'Inter',sans-serif !important; font-size:12px !important; font-weight:500 !important;
}
div[class*="st-key-ec_exp_type_"] button::before {
    content:''; width:8px; height:8px; border-radius:50%; flex:0 0 auto;
}
.st-key-ec_exp_type_earnings button[kind="primary"] { background:rgba(240,249,255,0.5) !important; color:#024A70 !important; }
.st-key-ec_exp_type_earnings button[kind="primary"]::before { background:#38BDF8 !important; }
.st-key-ec_exp_type_ipo button[kind="primary"] { background:rgba(236,253,245,0.5) !important; color:#004F3B !important; }
.st-key-ec_exp_type_ipo button[kind="primary"]::before { background:#6EE7B7 !important; }
.st-key-ec_exp_type_ma button[kind="primary"] { background:rgba(245,243,255,0.5) !important; color:#4D179A !important; }
.st-key-ec_exp_type_ma button[kind="primary"]::before { background:#C4B5FD !important; }
.st-key-ec_exp_type_delisted button[kind="primary"] { background:rgba(254,242,242,0.5) !important; color:#9F0712 !important; }
.st-key-ec_exp_type_delisted button[kind="primary"]::before { background:#D62E2F !important; }
div[class*="st-key-ec_exp_type_"] button[kind="secondary"] {
    background:#F8FAFC !important; color:#90A1B9 !important;
}
div[class*="st-key-ec_exp_type_"] button[kind="secondary"]::before { background:#CBD5E1 !important; }
/* Inactive event-type rows show a right-aligned "hidden" hint (design) */
div[class*="st-key-ec_exp_type_"] button[kind="secondary"]::after {
    content:'hidden'; margin-left:auto !important; font-size:10px !important;
    font-weight:500 !important; color:#CAD5E2 !important; letter-spacing:0 !important;
}
/* Keep the label hugging the dot — Streamlit centers the label via nested flex wrappers */
div[class*="st-key-ec_exp_type_"] button > div { justify-content:flex-start !important; width:100% !important; }
div[class*="st-key-ec_exp_type_"] button > div > span { justify-content:flex-start !important; margin:0 !important; }
div[class*="st-key-ec_exp_type_"] button [data-testid="stMarkdownContainer"] { text-align:left !important; margin:0 !important; }
div[class*="st-key-ec_exp_type_"] button [data-testid="stMarkdownContainer"] p { text-align:left !important; }
/* Fiscal Quarter: 2×2 grid, buttons fill the column (design node 24139:47638) */
div[class*="st-key-ec_exp_q_"] button { width:100% !important; padding:0 !important; }
div[class*="st-key-ec_exp_q_"] button[kind="primary"] {
    background:#F1F5F9 !important; box-shadow:inset 0 0 0 1px #CAD5E2 !important; border:none !important;
    color:#4C4E56 !important; border-radius:10px !important; font-weight:700 !important;
    font-size:12px !important; min-height:36px !important;
}
div[class*="st-key-ec_exp_q_"] button[kind="secondary"] {
    background:#F8FAFC !important; box-shadow:none !important; border:none !important;
    color:#CAD5E2 !important; border-radius:10px !important; font-weight:700 !important;
    font-size:12px !important; min-height:36px !important;
}
/* Collapsible filter sections = native st.expander (<details>) — client-side, no rerun.
   Restyle the header to the Figma section header: flat, uppercase label + chevron right. */
.st-key-ec_filter_expand [data-testid="stExpander"] { border:none !important; box-shadow:none !important; }
.st-key-ec_filter_expand [data-testid="stExpander"] details {
    border:none !important; background:transparent !important; box-shadow:none !important;
}
.st-key-ec_filter_expand [data-testid="stExpander"] summary {
    padding:2px 2px !important; min-height:0 !important; list-style:none !important;
    position:relative !important; cursor:pointer !important;
    display:flex !important; align-items:center !important;
    background:transparent !important; border:none !important;   /* no grey box, no underline (design) */
}
.st-key-ec_filter_expand [data-testid="stExpander"] summary:hover { background:transparent !important; }
/* Remove Streamlit's separator line under the expander header */
.st-key-ec_filter_expand [data-testid="stExpander"] [data-testid="stExpanderDetails"] { border:none !important; }
.st-key-ec_filter_expand [data-testid="stExpander"] summary::-webkit-details-marker { display:none !important; }
/* Hide Streamlit's default (Material-font) chevron — swap for an inline SVG so the
   STG proxy stripping the icon font can never garble it. */
.st-key-ec_filter_expand [data-testid="stExpander"] summary svg,
.st-key-ec_filter_expand [data-testid="stExpander"] summary [data-testid="stIconMaterial"],
.st-key-ec_filter_expand [data-testid="stExpander"] summary [data-testid="stExpanderToggleIcon"] {
    display:none !important;
}
.st-key-ec_filter_expand [data-testid="stExpander"] summary p,
.st-key-ec_filter_expand [data-testid="stExpander"] summary span {
    font-family:'Inter',sans-serif !important; font-size:11px !important; font-weight:600 !important;
    color:#62748E !important; text-transform:uppercase !important; letter-spacing:0.55px !important;
    text-align:left !important; margin:0 !important;
}
.st-key-ec_filter_expand [data-testid="stExpander"] summary::after {
    content:''; position:absolute; right:2px; top:50%; transform:translateY(-50%);
    width:13px; height:13px;
    background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%2390A1B9' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") no-repeat center;
    background-size:13px 13px; transition:transform .15s ease;
}
.st-key-ec_filter_expand [data-testid="stExpander"] details[open] summary::after { transform:translateY(-50%) rotate(180deg); }
.st-key-ec_filter_expand [data-testid="stExpander"] [data-testid="stExpanderDetails"] { padding-top:8px !important; }

/* Filters toggle — sliders icon (Figma) instead of a hamburger char */
.st-key-ec_filters_toggle button { display:flex !important; align-items:center !important; justify-content:center !important; gap:7px !important; }
.st-key-ec_filters_toggle button::before {
    content:''; width:13px; height:13px; flex:0 0 auto; background-repeat:no-repeat; background-position:center; background-size:13px 13px;
}
.st-key-ec_filters_toggle button[kind="primary"]::before {
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%23FFFFFF' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cline x1='21' x2='14' y1='4' y2='4'/%3E%3Cline x1='10' x2='3' y1='4' y2='4'/%3E%3Cline x1='21' x2='12' y1='12' y2='12'/%3E%3Cline x1='8' x2='3' y1='12' y2='12'/%3E%3Cline x1='21' x2='16' y1='20' y2='20'/%3E%3Cline x1='12' x2='3' y1='20' y2='20'/%3E%3Cline x1='14' x2='14' y1='2' y2='6'/%3E%3Cline x1='8' x2='8' y1='10' y2='14'/%3E%3Cline x1='16' x2='16' y1='18' y2='22'/%3E%3C/svg%3E");
}
.st-key-ec_filters_toggle button[kind="secondary"]::before {
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%23475569' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cline x1='21' x2='14' y1='4' y2='4'/%3E%3Cline x1='10' x2='3' y1='4' y2='4'/%3E%3Cline x1='21' x2='12' y1='12' y2='12'/%3E%3Cline x1='8' x2='3' y1='12' y2='12'/%3E%3Cline x1='21' x2='16' y1='20' y2='20'/%3E%3Cline x1='12' x2='3' y1='20' y2='20'/%3E%3Cline x1='14' x2='14' y1='2' y2='6'/%3E%3Cline x1='8' x2='8' y1='10' y2='14'/%3E%3Cline x1='16' x2='16' y1='18' y2='22'/%3E%3C/svg%3E");
}
/* Chevron on the Filters button (rotates up when the panel is open) */
.st-key-ec_filters_toggle button::after {
    content:''; width:12px; height:12px; flex:0 0 auto; margin-left:1px;
    background-repeat:no-repeat; background-position:center; background-size:12px 12px;
    transition:transform .2s ease;
}
.st-key-ec_filters_toggle button[kind="primary"]::after {
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%23FFFFFF' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
    transform:rotate(180deg);
}
.st-key-ec_filters_toggle button[kind="secondary"]::after {
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%23475569' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
}
/* Count badge INSIDE the Filters button — white circle (design). The label carries a
   Streamlit badge (:gray-badge[N]) which we restyle to a circle. */
.st-key-ec_filters_toggle button .stMarkdownBadge {
    width:16px !important; min-width:16px !important; height:16px !important; padding:0 !important;
    border-radius:999px !important; display:inline-flex !important; align-items:center !important;
    justify-content:center !important; font-family:'Inter',sans-serif !important;
    font-size:9px !important; font-weight:700 !important; line-height:1 !important; margin:0 1px !important;
    vertical-align:middle !important;
}
.st-key-ec_filters_toggle button[kind="primary"] .stMarkdownBadge {
    background:#FFFFFF !important; color:#2D2A29 !important;
}
.st-key-ec_filters_toggle button[kind="secondary"] .stMarkdownBadge {
    background:#2D2A29 !important; color:#FFFFFF !important;
}

/* Top filter row: single left-packed flex line (design: flex items-center gap-3) */
.st-key-ec_filter_row { flex-wrap:wrap !important; row-gap:8px !important; }
/* Every item is content-width and left-packed — nothing grows to create gaps. */
.st-key-ec_filter_row > [data-testid="stElementContainer"] {
    width:auto !important; flex:0 0 auto !important;
}
/* Count badge — dark circle (design's inactiveCount) */
.ec-fbadge {
    display:inline-flex; align-items:center; justify-content:center;
    width:16px; height:16px; border-radius:999px;
    background:#2D2A29; color:#FFFFFF; font-family:'Inter',sans-serif;
    font-size:9px; font-weight:700; line-height:1;
}
/* Vertical divider between Filters button and the chips */
.ec-vdiv { width:1px; height:16px; background:#E2E8F0; }
.st-key-ec_filter_row [data-testid="stElementContainer"]:has(.ec-vdiv),
.st-key-ec_filter_row [data-testid="stElementContainer"]:has(.ec-fbadge) {
    display:flex !important; align-items:center !important;
}
/* Reset all pushed to the far right (design: ml-auto) */
.st-key-ec_reset_filters { margin-left:auto !important; }

/* ── Calendar toolbar (Figma: flex justify-between, nav left / rest right) ── */
.st-key-ec_toolbar_row { margin:0 0 14px !important; flex-wrap:nowrap !important; }
.st-key-ec_toolbar_row > [data-testid="stElementContainer"] { width:auto !important; flex:0 0 auto !important; }
/* Right cluster pushed to the far right (design: the left group + ml-auto on the rest) */
.st-key-ec_toolbar_right { margin-left:auto !important; width:auto !important; flex:0 0 auto !important; }
.st-key-ec_toolbar_right > [data-testid="stElementContainer"] { width:auto !important; flex:0 0 auto !important; }
.ec-cal-toolbar-title {
    font-family:'Inter',sans-serif; font-size:16px; font-weight:600; color:#2D2A29;
    text-align:center; line-height:24px; min-width:140px; white-space:nowrap;
}
.ec-cal-stats {
    font-family:'Inter',sans-serif; font-size:12px; color:#90A1B9;
    display:flex; align-items:baseline; gap:16px; flex-wrap:nowrap; white-space:nowrap;
}
.ec-cal-stats b { color:#4C4E56; font-weight:700; font-size:12px; }
.ec-cal-stat-lbl { color:#90A1B9; font-size:11px; font-weight:400; }
.ec-cal-legend {
    font-family:'Inter',sans-serif; font-size:11px; color:#62748E;
    display:flex; flex-wrap:nowrap; gap:12px; align-items:center; line-height:1.5;
}
.ec-cal-legend span { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
.ec-dot { display:inline-block; width:8px; height:8px; border-radius:6px; }
.ec-dot-earnings { background:#38BDF8; }
.ec-dot-ipo { background:#6EE7B7; }
.ec-dot-ma { background:#C4B5FD; }
.ec-dot-delisted { background:#D62E2F; }

.st-key-ec_cal_prev button, .st-key-ec_cal_next button {
    min-height:28px !important; width:28px !important; height:28px !important; padding:0 !important;
    border:1px solid #E2E8F0 !important; border-radius:10px !important;
    background:#FFFFFF !important; color:#62748E !important; font-size:15px !important;
    font-weight:400 !important;
}
/* Email Alerts — white pill with a lucide bell icon (design) */
.st-key-ec_open_email_alerts button {
    background:#FFFFFF !important; border:1px solid #E2E8F0 !important; color:#45556C !important;
    font-family:'Inter',sans-serif !important; font-size:11px !important; font-weight:500 !important;
    border-radius:10px !important; min-height:31px !important; padding:6px 10px !important;
    display:inline-flex !important; align-items:center !important; gap:6px !important;
}
.st-key-ec_open_email_alerts button:hover { border-color:#CBD5E1 !important; background:#F8FAFC !important; }
.st-key-ec_open_email_alerts button::before {
    content:''; width:11px; height:11px; flex:0 0 auto; background-repeat:no-repeat;
    background-position:center; background-size:11px 11px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='%2345556C' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M10.268 21a2 2 0 0 0 3.464 0'/%3E%3Cpath d='M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326'/%3E%3C/svg%3E");
}
/* Segmented view switcher — pill group (design: bg-slate-100 rounded-lg p-0.5) */
.st-key-ec_view_switch {
    background:#F1F5F9 !important; border-radius:10px !important;
    padding:2px !important; gap:2px !important; flex:0 0 auto !important; width:auto !important;
}
.st-key-ec_view_switch > [data-testid="stElementContainer"] { width:auto !important; flex:0 0 auto !important; }
div[class*="st-key-ec_view_"] button {
    font-family:'Inter',sans-serif !important; font-size:12px !important; font-weight:500 !important;
    border-radius:8px !important; min-height:24px !important; border:none !important;
    box-shadow:none !important; white-space:nowrap !important; padding:4px 12px !important; width:auto !important;
}
div[class*="st-key-ec_view_"] button[kind="primary"] {
    background:#FFFFFF !important; color:#2D2A29 !important; font-weight:500 !important;
    box-shadow:0 1px 2px -1px rgba(0,0,0,0.10), 0 1px 3px 0 rgba(0,0,0,0.10) !important;
}
div[class*="st-key-ec_view_"] button[kind="secondary"] {
    background:transparent !important; color:#62748E !important;
}

.ec-no-data {
    text-align:center; color:#64748B; padding:48px 0; font-size:15px;
    font-family:'Inter',sans-serif;
}

/* ── Yearly view — custom month-card grid (Figma node 24139:48905) ── */
.ec-year-wrap { margin-top:4px; }
.ec-year-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px 19px; }
.ec-ym-card { border:1px solid #E2E8F0; border-radius:14px; background:#FFFFFF; overflow:hidden; }
.ec-ym-card.cur {
    border-color:#D62E2F;
    box-shadow:0 1px 2px -1px rgba(0,0,0,0.10), 0 1px 3px 0 rgba(0,0,0,0.10);
}
.ec-ym-head {
    display:flex; align-items:center; justify-content:space-between;
    padding:10px 12px; background:#F8FAFC; border-bottom:1px solid #F1F5F9;
}
.ec-ym-card.cur .ec-ym-head { background:#D62E2F; border-bottom-color:#D62E2F; }
.ec-ym-name {
    font-family:'Inter',sans-serif; font-weight:700; font-size:12px; line-height:16px;
    letter-spacing:0.3px; color:#333333;
}
.ec-ym-card.cur .ec-ym-name { color:#FFFFFF; }
.ec-ym-hr { display:flex; align-items:center; gap:8px; }
.ec-ym-dots { display:flex; align-items:center; gap:4px; }
.ec-ym-dot { width:6px; height:6px; border-radius:50%; flex:0 0 auto; }
.ec-ym-card.cur .ec-ym-dot { background:rgba(255,255,255,0.70) !important; }
.ec-ym-badge {
    font-family:'Inter',sans-serif; font-weight:600; font-size:9px; line-height:13.5px;
    color:#62748E; background:#E2E8F0; border-radius:999px; padding:2px 6px; white-space:nowrap;
}
.ec-ym-card.cur .ec-ym-badge { background:rgba(255,255,255,0.20); color:#FFFFFF; }
.ec-ym-body { padding:12px; }
.ec-ym-wk { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); }
.ec-ym-wk span {
    font-family:'Inter',sans-serif; font-weight:600; font-size:8px; line-height:12px;
    color:#90A1B9; padding-left:2px;
}
.ec-ym-wk span:nth-child(6), .ec-ym-wk span:nth-child(7) { color:#CAD5E2; }
.ec-ym-days { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); margin-top:4px; }
.ec-ym-day { min-height:36px; border:1px solid #F8FAFC; padding:2px 1px; box-sizing:border-box; }
.ec-ym-day.today { border-color:#D62E2F; }
.ec-ym-daynum {
    display:block; font-family:'Inter',sans-serif; font-weight:600; font-size:9px;
    line-height:11.25px; color:#62748E; padding-left:2px;
}
.ec-ym-day.today .ec-ym-daynum { color:#D62E2F; }
.ec-ym-evs { margin-top:2px; display:flex; flex-direction:column; gap:1px; }
.ec-ym-ev { display:flex; align-items:center; gap:2px; padding-left:1px; }
.ec-ym-evdot { width:4px; height:4px; border-radius:50%; flex:0 0 auto; }
.ec-ym-evtk {
    font-family:'Inter',sans-serif; font-weight:500; font-size:8px; line-height:9px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.ec-ym-more {
    font-family:'Inter',sans-serif; font-weight:400; font-size:7px; line-height:9px;
    color:#90A1B9; padding-left:2px; margin-top:1px;
}
.ec-year-foot {
    display:flex; align-items:center; justify-content:space-between; margin-top:14px;
    font-family:'Inter',sans-serif; font-size:11px; color:#90A1B9;
}
.ec-year-foot b { color:#62748E; font-weight:600; }
.ec-yf-qs { display:flex; gap:6px; }
.ec-yf-q {
    font-family:'Inter',sans-serif; font-weight:700; font-size:10px; color:#45556C;
    background:#E2E8F0; border-radius:4px; padding:2px 8px;
}
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


_EC_PREFS_UNSET = object()  # sentinel: "no prefetched row supplied"


def _load_existing_alert_preferences(user_email: str, saved_row: Any = _EC_PREFS_UNSET) -> Dict[str, Any]:
    """
    Load saved preferences from the DB via EarningsCalendarRepository.

    `saved_row` — optional prefetched raw row (from the page's parallel fetch
    phase). Pass the row (or None for "no saved prefs") to skip the repository
    lookup; omit it entirely to fetch from the repository as before.

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
        if not user_email:
            return default_prefs
        if saved_row is not _EC_PREFS_UNSET:
            saved = dict(saved_row) if isinstance(saved_row, dict) else {}
        else:
            getter = getattr(EarningsCalendarRepository, "get_earnings_alert_preferences", None)
            if not callable(getter):
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
                        # Analytics: earnings-alert preferences saved (discrete click).
                        try:
                            from utils.server_logger import track_action
                            track_action("alert_prefs_save", page="earnings_calendar",
                                         enabled=bool(want_reminders),
                                         days_before=int(days_before),
                                         mode=selection_mode,
                                         tickers=len(selected_tickers or []),
                                         sectors=len(selected_sectors or []),
                                         mailed=bool(mailed))
                        except Exception:
                            pass
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
            kind = _earnings_kind(fq)
            if not kind:
                continue
            start_str = _to_iso(e["earnings_date"])
            fc.append(_fc_event_card(
                event_id=str(e["id"]) if e.get("id") is not None else "",
                start=start_str,
                company_name=company_name,
                ticker=e["ticker"],
                event_type="earnings",
                quarter=kind,
                extended_props={
                    "fiscal_q": fq,
                    "fiscal_year": fy,
                    "fiscal_qe": e.get("fiscal_quarter_ending", ""),
                    "eps_actual": e.get("eps_actual"),
                    "eps_forecast": e.get("eps_forecast"),
                    "surprise_pct": e.get("surprise_pct"),
                    "market_cap": e.get("market_cap"),
                    "num_estimates": e.get("num_estimates"),
                    "report_time": e.get("report_time", "time-not-supplied"),
                    "beat_miss": e.get("beat_miss", "no_data"),
                    "ir_website_url": e.get("ir_website_url"),
                },
            ))
        return fc
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_to_fullcalendar",
                             operation="convert_events",
                             context=f"events_count={len(events) if events else 0}")
        return []


# upstream ETL produced sentence fragments / 8-K boilerplate in
# ma_acquirer/ma_target ('reference', 'completed its previously'); names that
# fail validation render as '—' rather than misinforming
from utils.ma_8k_extract import sanitize_ma_name as _sanitize_ma_name


def _ma_to_fullcalendar(ma_events: List[Dict]) -> List[Dict]:
    """Convert completed-M&A events into FullCalendar card events."""
    try:
        fc = []
        for e in ma_events:
          try:
            start_str = _to_iso(e.get("earnings_date"))
            if not start_str:
                continue
            company_name = e.get("company_name") or e.get("ticker") or ""
            _dt = e.get("ma_deal_type")
            deal = (_dt if isinstance(_dt, str) and _dt else "M&A").title()
            _raw_val = e.get("ma_value_usd_m")
            try:
                _val = float(_raw_val) if _raw_val is not None else None
                if _val != _val:
                    _val = None
            except (TypeError, ValueError):
                _val = None
            fc.append(_fc_event_card(
                event_id=f"ma_{e['id']}" if e.get("id") is not None else f"ma_{start_str}_{e.get('ticker','')}",
                start=start_str,
                company_name=company_name,
                ticker=e.get("ticker", ""),
                event_type="ma",
                quarter=None,
                extended_props={
                    "ma_acquirer": _sanitize_ma_name(e.get("ma_acquirer")),
                    "ma_target": _sanitize_ma_name(e.get("ma_target")),
                    "ma_deal_type": e.get("ma_deal_type") or deal,
                    "ma_value_usd_m": _val,
                    "ma_close_date": _to_iso(e.get("ma_close_date")) if e.get("ma_close_date") else "",
                    "ma_announce_date": _to_iso(e.get("ma_announce_date")) if e.get("ma_announce_date") else "",
                    "source": e.get("source"),
                    "source_ref": e.get("source_ref"),
                    "headline": e.get("headline"),
                },
            ))
          except Exception as _ev_exc:
            log_structured_error(_ev_exc, page="earnings_calendar", component="_ma_to_fullcalendar",
                                 operation="convert_ma_event_row",
                                 context=f"ticker={e.get('ticker') if isinstance(e, dict) else '?'}")
            continue
        return fc
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_ma_to_fullcalendar",
                             operation="convert_ma_events",
                             context=f"events_count={len(ma_events) if ma_events else 0}")
        return []


def _ipo_to_fullcalendar(ipo_events: List[Dict]) -> List[Dict]:
    """Convert IPO (first-listing) events into FullCalendar card events."""
    try:
        fc = []
        for e in ipo_events:
          try:
            start_str = _to_iso(e.get("earnings_date"))
            if not start_str:
                continue
            company_name = e.get("company_name") or e.get("ticker") or ""
            fc.append(_fc_event_card(
                event_id=f"ipo_{e['id']}" if e.get("id") is not None else f"ipo_{start_str}_{e.get('ticker','')}",
                start=start_str,
                company_name=company_name,
                ticker=e.get("ticker", ""),
                event_type="ipo",
                quarter=None,
                extended_props={
                    "ipo_date": _to_iso(e.get("ipo_date")) if e.get("ipo_date") else start_str,
                    "exchange": e.get("exchange"),
                    "listing_status": e.get("listing_status"),
                    "ipo_source": e.get("ipo_source", "av"),
                },
            ))
          except Exception as _ev_exc:
            log_structured_error(_ev_exc, page="earnings_calendar", component="_ipo_to_fullcalendar",
                                 operation="convert_ipo_event_row",
                                 context=f"ticker={e.get('ticker') if isinstance(e, dict) else '?'}")
            continue
        return fc
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_ipo_to_fullcalendar",
                             operation="convert_ipo_events",
                             context=f"events_count={len(ipo_events) if ipo_events else 0}")
        return []


def _delisted_to_fullcalendar(delisted_events: List[Dict]) -> List[Dict]:
    """Convert delisted events into FullCalendar card events."""
    try:
        fc = []
        for e in delisted_events:
          try:
            start_str = _to_iso(e.get("earnings_date"))
            if not start_str:
                continue
            company_name = e.get("company_name") or e.get("ticker") or ""
            fc.append(_fc_event_card(
                event_id=f"del_{e['id']}" if e.get("id") is not None else f"del_{start_str}_{e.get('ticker','')}",
                start=start_str,
                company_name=company_name,
                ticker=e.get("ticker", ""),
                event_type="delisted",
                quarter=None,
                extended_props={
                    "delisting_date": _to_iso(e.get("delisting_date")) if e.get("delisting_date") else start_str,
                    "industry": e.get("industry"),
                    "exchange": e.get("exchange"),
                },
            ))
          except Exception as _ev_exc:
            log_structured_error(_ev_exc, page="earnings_calendar", component="_delisted_to_fullcalendar",
                                 operation="convert_delisted_event_row",
                                 context=f"ticker={e.get('ticker') if isinstance(e, dict) else '?'}")
            continue
        return fc
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_delisted_to_fullcalendar",
                             operation="convert_delisted_events",
                             context=f"events_count={len(delisted_events) if delisted_events else 0}")
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
        acquirer = _sanitize_ma_name(ep.get("ma_acquirer")) or "—"
        target   = _sanitize_ma_name(ep.get("ma_target")) or "—"
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


def _render_ipo_detail_panel(event_data: Dict) -> None:
    """Detail card for an IPO (first-listing) event."""
    try:
        ep       = event_data.get("extendedProps", {})
        ticker   = ep.get("ticker", "")
        company  = ep.get("company_name", ticker)
        ipo_date = _fmt_date(ep.get("ipo_date") or event_data.get("start", ""))
        exchange = ep.get("exchange") or "—"
        status   = ep.get("listing_status") or "—"
        bg, _txt = _kind_color("ipo")
        # Source label reflects the feed: exact AV listing date vs Yahoo first-trade
        # (the fallback for foreign names AV omits — approximate for very old listings).
        _src = ep.get("ipo_source", "av")
        src_label = "Yahoo Finance (first-trade date)" if _src == "yf" else "Alpha Vantage Listing"
        date_label = "First Trade Date" if _src == "yf" else "IPO Date"

        st.html(f"""
        <div class="ec-detail-card">
            <div class="ec-detail-title">{html.escape(str(company))}</div>
            <div class="ec-detail-sub">
                <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                             background:{bg};margin-right:6px;vertical-align:middle;"></span>
                {html.escape(str(ticker))} &nbsp;·&nbsp; IPO
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">{date_label}</span>
                <span class="ec-detail-val">{ipo_date}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Exchange</span>
                <span class="ec-detail-val">{html.escape(str(exchange))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Listing Status</span>
                <span class="ec-detail-val">{html.escape(str(status))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Source</span>
                <span class="ec-detail-val">{html.escape(src_label)}</span>
            </div>
        </div>
        """)
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_ipo_detail_panel",
                             operation="render_ipo_detail", context="IPO detail panel render")
        return None


def _render_delisted_detail_panel(event_data: Dict) -> None:
    """Detail card for a delisted (went private) event."""
    try:
        ep        = event_data.get("extendedProps", {})
        ticker    = ep.get("ticker", "")
        company   = ep.get("company_name", ticker)
        del_date  = _fmt_date(ep.get("delisting_date") or event_data.get("start", ""))
        industry  = ep.get("industry") or "—"
        exchange  = ep.get("exchange") or "—"
        bg, _txt  = _kind_color("delisted")

        st.html(f"""
        <div class="ec-detail-card">
            <div class="ec-detail-title">{html.escape(str(company))}</div>
            <div class="ec-detail-sub">
                <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
                             background:{bg};margin-right:6px;vertical-align:middle;"></span>
                {html.escape(str(ticker))} &nbsp;·&nbsp; Delisted (Went Private)
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Delisting Date</span>
                <span class="ec-detail-val">{del_date}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Status</span>
                <span class="ec-detail-val">Public → Private</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Industry</span>
                <span class="ec-detail-val">{html.escape(str(industry))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Last Exchange</span>
                <span class="ec-detail-val">{html.escape(str(exchange))}</span>
            </div>
            <div class="ec-detail-row">
                <span class="ec-detail-key">Source</span>
                <span class="ec-detail-val">Alpha Vantage Listing</span>
            </div>
        </div>
        """)
    except Exception as exc:
        log_structured_error(exc, page="earnings_calendar", component="_render_delisted_detail_panel",
                             operation="render_delisted_detail", context="Delisted detail panel render")
        return None


def _render_detail_panel(event_data: Dict) -> None:
    try:
        import time as _t
        _t0 = _t.perf_counter()

        ep          = event_data.get("extendedProps", {})
        if ep.get("kind") == "ma":
            _render_ma_detail_panel(event_data)
            return
        if ep.get("kind") == "ipo":
            _render_ipo_detail_panel(event_data)
            return
        if ep.get("kind") == "delisted":
            _render_delisted_detail_panel(event_data)
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
        # Track detail-panel render time (this is where the transcript lookup runs —
        # the batched get_transcript_for_calendar_event keeps it fast; previously the
        # per-row N+1 made this 22-49s for big companies).
        try:
            log_timing("EC_DETAIL_PANEL_RENDER", _panel_ms,
                       details=f"ticker={ticker} q={fiscal_q}", level="WARNING")
        except Exception:
            pass
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
    import time as _time
    _page_start = _time.perf_counter()
    _tracker = PageLoadTracker("earnings_calendar")
    try:
        _t0_page = _time.perf_counter()

        # Render styles FIRST - before anything else to prevent layout flash
        render_styles()
        st.set_page_config(page_title="Calendar", layout="wide")
        st.markdown(_get_css(), unsafe_allow_html=True)

        # Reset company dropdown and watchlist filter on fresh navigation to this page
        if st.session_state.get("_active_page") != "earnings_calendar":
            st.session_state.pop("ec_company_filter", None)
            st.session_state.pop("ec_watchlist_filter", None)
            st.session_state["ec_active_watchlist_id"] = None
            st.session_state["ec_active_watchlist_name"] = ""
            # Reset filters on fresh page entry — panel starts COLLAPSED (business ask).
            st.session_state["ec_active_types"] = list(_ALL_TYPES)
            st.session_state["ec_active_quarters"] = list(_ALL_QUARTERS)
            st.session_state["ec_filters_open"] = False
            st.session_state.pop("ec_active_kinds", None)

        render_header(current_page="earnings_calendar")

        # =======================================================================
        # PAGE TITLE — same two-line branded header as earnings_calls / newsroom /
        # screening ("CORESIGHT MARKET DATA" in red + page name in black).
        # =======================================================================
        st.markdown("""
        <div style="margin: 24px 0 8px 0;">
            <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 24px; color: #d62e2f; letter-spacing: 1px;">CORESIGHT MARKET DATA</div>
            <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 28px; color: #323232;">Calendar</div>
        </div>
        """, unsafe_allow_html=True)

        # STICKY branded loader — stays up until the streamlit_calendar iframe actually
        # paints (it renders client-side well after Python returns), so the user never
        # sees the filter bar over an empty calendar area. JS self-removes; the legacy
        # `.empty()` calls below are safe no-ops.
        from components.loading import render_sticky_loader
        _ecal_loading_hint = render_sticky_loader("Loading Calendar")

        # ── session state defaults ────────────────────────────────────────────────
        _t_session = _time.perf_counter()
        if "ec_view" not in st.session_state:
            st.session_state.ec_view = "calendar"   # "calendar" | "year" | "list"
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
        if "ec_active_types" not in st.session_state:
            # Migrate legacy combined kind filter if present
            _legacy = st.session_state.get("ec_active_kinds")
            if _legacy:
                st.session_state.ec_active_types = [
                    t for t in _ALL_TYPES
                    if t in _legacy or (t == "earnings" and any(q in _legacy for q in _ALL_QUARTERS))
                ] or list(_ALL_TYPES)
            else:
                st.session_state.ec_active_types = list(_ALL_TYPES)
        if "ec_active_quarters" not in st.session_state:
            _legacy = st.session_state.get("ec_active_kinds")
            if _legacy:
                st.session_state.ec_active_quarters = [q for q in _ALL_QUARTERS if q in _legacy] or list(_ALL_QUARTERS)
            else:
                st.session_state.ec_active_quarters = list(_ALL_QUARTERS)
        if "ec_filters_open" not in st.session_state:
            st.session_state.ec_filters_open = False
        if "ec_visible_start" not in st.session_state:
            st.session_state.ec_visible_start = None
        if "ec_visible_end" not in st.session_state:
            st.session_state.ec_visible_end = None

        # Analytics: calendar filters (company / watchlist / active event-type legend
        # kinds) are session-state, not URL — capture them for the analytics feed.
        try:
            from utils.server_logger import log_filters_if_changed
            log_filters_if_changed(
                "earnings_calendar",
                company=st.session_state.get("ec_company_filter") or None,
                watchlist=st.session_state.get("ec_active_watchlist_name") or None,
                active_types=list(st.session_state.get("ec_active_types") or []),
                active_quarters=list(st.session_state.get("ec_active_quarters") or []),
            )
        except Exception:
            pass

        _vis_start, _vis_end_inclusive, _vis_source = _resolve_visible_date_range()
        log_timing(
            "EC_OPT_VISIBLE_RANGE",
            0,
            f"source={_vis_source} start={_vis_start} end={_vis_end_inclusive}",
            level="WARNING",
        )

        # ── load available tickers + date range + all events (parallel — all independent) ─
        from concurrent.futures import ThreadPoolExecutor

        # Toolbar inputs (main-thread session reads) needed by the extra
        # parallel prefetches below. On STG (03-Jul) the toolbar's sequential
        # alert-prefs + watchlists + ticker-validation DB calls measured
        # 4.3-4.7s per render (EC_PAGE_TOOLBAR_AND_FILTER); prefetching them
        # here overlaps them with the events fetch.
        _toolbar_user_email = _get_signed_in_user_email()
        _tb_cand_ticker = (
            st.query_params.get("ticker", "")
            or st.session_state.get("active_ticker", "")
            or ""
        ).strip()

        def _fetch_alert_prefs_raw():
            # Pure-DB path (no st.session_state — worker thread has no ctx).
            try:
                if not _toolbar_user_email:
                    return None
                from data.earnings_alert_store import load_preference
                return load_preference(str(_toolbar_user_email).strip().lower())
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_alert_prefs_raw",
                                     operation="prefetch_alert_prefs", context=f"user={_toolbar_user_email}")
                return None

        def _fetch_toolbar_watchlists():
            # get_user_watchlists wraps all session_state access in try/except —
            # safe from a worker thread (cache skipped, DB result returned).
            try:
                return get_user_watchlists(_toolbar_user_email) if _toolbar_user_email else []
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_toolbar_watchlists",
                                     operation="prefetch_watchlists", context=f"user={_toolbar_user_email}")
                return []

        def _warm_candidate_company():
            # Warms CompanyRepository.get_company_by_ticker's st.cache_data so the
            # later validate_and_get_ticker() call is a cache hit (~0ms).
            try:
                if _tb_cand_ticker:
                    from data.repository import CompanyRepository
                    CompanyRepository.get_company_by_ticker(_tb_cand_ticker)
            except Exception:
                pass

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
                # FULL deduped set (ALL companies, ALL dates) — disk-materialized,
                # so this is a fast cache read, not the ~3.6s dedup SQL. The page
                # counts this for the true badge (matches prod 16,052/330) and
                # windows it in Python for the FullCalendar render.
                result = EarningsCalendarRepository.get_calendar_events_full()
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

        def _fetch_all_ipo():
            try:
                # IPO (first-listing) dates for calendar companies (separate feed;
                # ~311 rows, one per company; earnings logic untouched).
                return EarningsCalendarRepository.get_ipo_events()
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_all_ipo",
                                     operation="fetch_all_ipo", context="parallel fetch")
                return []

        def _fetch_all_delisted():
            try:
                # Delisted (went private) dates for firm-tracked companies (separate
                # feed; ~6 rows; earnings logic untouched).
                return EarningsCalendarRepository.get_delisted_events()
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_fetch_all_delisted",
                                     operation="fetch_all_delisted", context="parallel fetch")
                return []

        _t_parallel = _time.perf_counter()
        # Single loading UX: custom `_ecal_loading_hint` above (don't stack `st.spinner` with same message).
        with ThreadPoolExecutor(max_workers=9) as _ec_exec:
            _ticker_future = _ec_exec.submit(_fetch_tickers)
            _dr_future = _ec_exec.submit(_fetch_date_range)
            _events_future = _ec_exec.submit(_fetch_all_events)
            _ma_future = _ec_exec.submit(_fetch_all_ma)
            _ipo_future = _ec_exec.submit(_fetch_all_ipo)
            _delisted_future = _ec_exec.submit(_fetch_all_delisted)
            _prefs_future = _ec_exec.submit(_fetch_alert_prefs_raw)
            _wl_future = _ec_exec.submit(_fetch_toolbar_watchlists)
            _ec_exec.submit(_warm_candidate_company)
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
            _te = _time.perf_counter(); _prefetched_ipo_events = _ipo_future.result()
            log_timing("EC_PAGE_FETCH_ALL_IPO", (_time.perf_counter() - _te) * 1000,
                       details=f"ipo_events={len(_prefetched_ipo_events) if _prefetched_ipo_events else 0}", level="WARNING")
            _te = _time.perf_counter(); _prefetched_delisted_events = _delisted_future.result()
            log_timing("EC_PAGE_FETCH_ALL_DELISTED", (_time.perf_counter() - _te) * 1000,
                       details=f"delisted_events={len(_prefetched_delisted_events) if _prefetched_delisted_events else 0}", level="WARNING")
            _te = _time.perf_counter(); _prefetched_alert_prefs = _prefs_future.result()
            log_timing("EC_PAGE_FETCH_ALERT_PREFS", (_time.perf_counter() - _te) * 1000, level="WARNING")
            _te = _time.perf_counter(); _prefetched_watchlists = _wl_future.result()
            log_timing("EC_PAGE_FETCH_WATCHLISTS", (_time.perf_counter() - _te) * 1000,
                       details=f"n={len(_prefetched_watchlists or [])}", level="WARNING")
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
                st.session_state.ec_selected_event = None
                # Height handling by view:
                #  • Month view has a DETERMINISTIC height (weeks × row) that is
                #    independent of the event count, so the filtered set can swap IN
                #    PLACE (stable key → no iframe remount, no loader flash) and the
                #    grid still exactly fills the iframe — fast AND no blank gap.
                #  • List view height is intrinsically event-count-driven (a listMonth
                #    is as tall as its rows). streamlit_calendar does NOT shrink the
                #    iframe on an in-place event change, so a stable key would leave the
                #    tall "all companies" height with a big blank gap under a filtered
                #    (short) list. Bump the version there so the list REMOUNTS at the
                #    correct height. Only ~1 filtered company's rows render, so the
                #    remount is cheap.
                if st.session_state.get("ec_view") == "list":
                    st.session_state.ec_cal_version += 1
            except Exception as exc:
                log_structured_error(exc, page="earnings_calendar", component="_on_company_change",
                                     operation="company_change_callback",
                                     context=f"label={st.session_state.get('ec_company_filter', '')}")
                return

        # ── toolbar setup: email badge + watchlist list — both prefetched in the
        # parallel phase above (was 2 sequential DB round-trips per render) ────
        _ec_tb_alerts_on = bool(
            _load_existing_alert_preferences(
                _toolbar_user_email, saved_row=_prefetched_alert_prefs,
            ).get("enabled", False)
        )
        _toolbar_watchlists = _prefetched_watchlists or []

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

        # ── load & filter events (company/watchlist from filter panel) ────────────
        _t_events = _time.perf_counter()
        _active_wl_id = st.session_state.ec_active_watchlist_id
        if _active_wl_id is not None:
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
            ipo_events = _filter_events_by_watchlist(_prefetched_ipo_events, _wl_company_rows)
            delisted_events = _filter_events_by_watchlist(_prefetched_delisted_events, _wl_company_rows)
            _wl_active_name = st.session_state.ec_active_watchlist_name
            _wl_cal_count = len({e["ticker"] for e in events} | {e["ticker"] for e in ma_events}
                                | {e["ticker"] for e in ipo_events} | {e["ticker"] for e in delisted_events})
            st.caption(
                f"Filtering by watchlist: **{_wl_active_name}** — "
                f"{len(_wl_company_rows)} {'company' if len(_wl_company_rows) == 1 else 'companies'}, "
                f"{_wl_cal_count} {'company' if _wl_cal_count == 1 else 'companies'} "
                f"found on this calendar."
            )
        else:
            events = _prefetched_events
            ma_events = _prefetched_ma_events
            ipo_events = _prefetched_ipo_events
            delisted_events = _prefetched_delisted_events

        # Company dropdown → filter events to the selected ticker (in-memory, 0 DB
        # round-trips). "All Companies" (or unset) leaves the full set untouched.
        _company_label = st.session_state.get("ec_company_filter") or ""
        _company_ticker = (
            ticker_map.get(_company_label, "")
            if _company_label and _company_label != _all_opt
            else ""
        )
        if _company_ticker:
            _ct = _company_ticker.lower()
            def _company_match(e: Dict) -> bool:
                return str(e.get("ticker") or "").lower() == _ct
            events = [e for e in events if _company_match(e)]
            ma_events = [e for e in ma_events if _company_match(e)]
            ipo_events = [e for e in ipo_events if _company_match(e)]
            delisted_events = [e for e in delisted_events if _company_match(e)]

        _n_pre_filter = len(events) + len(ma_events) + len(ipo_events) + len(delisted_events)
        _render_filter_panel(
            all_labels=all_labels,
            default_idx=_default_idx,
            on_company_change=_on_company_change,
            wl_options=_wl_options,
            wl_default_idx=_wl_default_idx,
            on_watchlist_change=_on_watchlist_change,
            n_matching=_n_pre_filter,
        )
        _active_types = _selected_set("ec_active_types", _ALL_TYPES)
        _active_quarters = _selected_set("ec_active_quarters", _ALL_QUARTERS)

        if "earnings" not in _active_types:
            events = []
        else:
            events = [e for e in events if _earnings_kind(e.get("fiscal_q")) in _active_quarters]
        ma_events = ma_events if "ma" in _active_types else []
        ipo_events = ipo_events if "ipo" in _active_types else []
        delisted_events = delisted_events if "delisted" in _active_types else []

        if not events and not ma_events and not ipo_events and not delisted_events:
            _ecal_loading_hint.empty()
            st.html('<div class="ec-no-data">No events match the selected filters. '
                    'Use the filter pills above to show more event types or quarters.</div>')
            render_coresight_footer()
            return

        # ── stats for Figma toolbar (scoped to the DISPLAYED period) ────────────
        # Business requirement: the toolbar/footer counts must reflect ONLY the
        # period on screen — the month for Monthly/List, the year for Yearly — NOT
        # the full all-time deduped set. We scope the already type/company/watchlist-
        # filtered feeds to that period here (strict calendar month, not the 42-day
        # grid) before the render window narrows them to the FullCalendar view.
        # Count ALL active event types (earnings + IPO + M&A + delisted), not just
        # earnings — otherwise "N events" reads 0 when only Delisted/IPO/M&A are on.
        import calendar as _cal_period
        _view_now = st.session_state.get("ec_view", "calendar")
        _cnt_anchor = _parse_iso_date(st.session_state.get("_ec_current_date")) or date.today()
        if _view_now == "year":
            _cnt_start, _cnt_end = _year_view_date_range(_cnt_anchor)
        else:
            _cnt_start = _cnt_anchor.replace(day=1)
            _cnt_end = _cnt_anchor.replace(
                day=_cal_period.monthrange(_cnt_anchor.year, _cnt_anchor.month)[1]
            )
        _events_scoped   = _window_events(events, _cnt_start, _cnt_end)
        _ma_scoped       = _window_events(ma_events, _cnt_start, _cnt_end)
        _ipo_scoped      = _window_events(ipo_events, _cnt_start, _cnt_end)
        _delisted_scoped = _window_events(delisted_events, _cnt_start, _cnt_end)
        _all_shown = _events_scoped + _ma_scoped + _ipo_scoped + _delisted_scoped
        _n_total = len(_all_shown)
        _n_companies = len({e["ticker"] for e in _all_shown if e.get("ticker")})
        _n_ma = len(_ma_scoped)
        # Human-readable label for the footer ("July 2026" / "2026").
        _period_months = ["January", "February", "March", "April", "May", "June",
                          "July", "August", "September", "October", "November", "December"]
        _period_label = (str(_cnt_anchor.year) if _view_now == "year"
                         else f"{_period_months[_cnt_anchor.month - 1]} {_cnt_anchor.year}")

        # Window ALL feeds down to the visible month/year for rendering. Navigation
        # is 100% server-side here: the custom ‹ › toolbar buttons and the
        # Month/Year/List toggles each trigger a Streamlit rerun that re-resolves the
        # visible window and re-windows every feed (headerToolbar is False — there are
        # no client-only FullCalendar arrows that could bypass the server). So the
        # iframe never needs off-window markers. Windowing the overlays too (they were
        # previously sent in full) cuts the events shipped to FullCalendar from ~1,400
        # to a few hundred per view — the dominant client-side render cost. The badge
        # counts above are computed PRE-window, so they still reflect the full set.
        events          = _window_events(events, _vis_start, _vis_end_inclusive)
        ma_events       = _window_events(ma_events, _vis_start, _vis_end_inclusive)
        ipo_events      = _window_events(ipo_events, _vis_start, _vis_end_inclusive)
        delisted_events = _window_events(delisted_events, _vis_start, _vis_end_inclusive)

        # ─────────────────────────────────────────────────────────────────────────
        # FULLCALENDAR — Month (dayGridMonth) or Year (multiMonthYear) view
        # ─────────────────────────────────────────────────────────────────────────
        from streamlit_calendar import calendar as st_calendar

        # Time from end of parallel fetch to here = dropdown + toolbar (watchlists,
        # email prefs) + ticker resolve + watchlist filtering.
        log_timing("EC_PAGE_TOOLBAR_AND_FILTER", (_time.perf_counter() - _t_dropdown) * 1000,
                   details=f"events_in_view={len(events)}", level="WARNING")
        _t_fc_prep = _time.perf_counter()
        fc_events    = (_to_fullcalendar(events)
                        + _ma_to_fullcalendar(ma_events)
                        + _ipo_to_fullcalendar(ipo_events)
                        + _delisted_to_fullcalendar(delisted_events))
        log_timing("EC_PAGE_TO_FULLCALENDAR", (_time.perf_counter() - _t_fc_prep) * 1000,
                   details=f"events={len(fc_events)}", level="WARNING")
        # Normalize to ISO date strings (earnings feed yields str dates, the overlay
        # feeds yield date objects) so max() never compares mixed types.
        _dated       = [d[:10] for d in (
            [_to_iso(e["earnings_date"]) for e in events if e.get("earnings_date")] +
            [_to_iso(e["earnings_date"]) for e in ma_events if e.get("earnings_date")] +
            [_to_iso(e["earnings_date"]) for e in ipo_events if e.get("earnings_date")] +
            [_to_iso(e["earnings_date"]) for e in delisted_events if e.get("earnings_date")]
        ) if d]
        initial_date = max(_dated) if _dated else date.today().isoformat()
        _view = st.session_state.ec_view

        # ── Yearly view: custom Figma month-card grid (no FullCalendar) ──
        if _view == "year":
            _yr = (_parse_iso_date(st.session_state.get("_ec_current_date")) or date.today()).year
            _yr_shown = len(events) + len(ma_events) + len(ipo_events) + len(delisted_events)
            with st.container(border=True, key="ec_cal_card"):
                _render_calendar_toolbar(
                    n_events=_n_total, n_companies=_n_companies, n_ma=_n_ma,
                    alerts_on=_ec_tb_alerts_on,
                    open_email_dialog=lambda: _email_alerts_dialog(all_tickers_meta),
                )
                _ecal_loading_hint.empty()   # no iframe paints in the year view — clear the sticky loader
                st.html(_render_year_grid(events, ma_events, ipo_events, delisted_events,
                                          _yr, date.today(), _yr_shown, _n_pre_filter))
            log_timing("EC_PAGE_RENDER_TOTAL", (_time.perf_counter() - _t0_page) * 1000,
                       details=f"year_view events={_yr_shown}", level="WARNING")
            render_coresight_footer()
            return

        if _view == "year":
            cal_key = f"earnings_cal_year_{st.session_state.ec_cal_version}"
            cal_options = {
                "initialView":       "multiMonthYear",
                "editable":          False,
                "selectable":        False,
                "headerToolbar":     False,
                "firstDay":          1,
                "multiMonthMaxColumns": 3,
                "dayMaxEvents":      3,
                "moreLinkClick":     "popover",
                "eventDisplay":      "block",
                "height":            "auto",
            }
        elif _view == "list":
            cal_key = f"earnings_cal_list_{st.session_state.ec_cal_version}"
            cal_options = {
                "initialView":   "listMonth",
                "editable":      False,
                "selectable":    False,
                "headerToolbar": False,
                "firstDay":      1,
                "eventDisplay":  "block",
                "height":        "auto",
                "contentHeight": 680,
            }
        else:
            cal_key = f"earnings_cal_month_{st.session_state.ec_cal_version}"
            _anchor = _parse_iso_date(st.session_state.get("_ec_current_date")) or date.today()
            # DETERMINISTIC height = weekday-header + N week-rows × per-row budget,
            # independent of the event count. `height:"auto"` sized the grid to the
            # tallest rendered row, so a dense "All Companies" month produced a tall
            # iframe; filtering to one company (in place, no remount) shrank the grid
            # but the streamlit_calendar iframe kept the tall height → a large blank
            # gap under the calendar. A fixed per-month height keeps every render the
            # same height (rows stretch to fill), so no stale height, no gap — and no
            # remount, so it stays fast. 150px/row matches the previous dense auto
            # height (5-week July ≈ 783px) and comfortably fits dayMaxEvents=3.
            _month_h = 33 + _month_grid_weeks(_anchor) * 150
            cal_options = {
                "initialView":   "dayGridMonth",
                "editable":      False,
                "selectable":    False,
                "headerToolbar": False,
                "firstDay":      1,
                "dayMaxEvents":  3,
                "moreLinkClick": "popover",
                "eventDisplay":  "block",
                # Show ONLY the weeks this month spans (no padded 6th week / next month).
                "fixedWeekCount": False,
                "showNonCurrentDates": True,
                "height":        _month_h,
            }

        # Always pass the tracked current date so FullCalendar re-initialises on
        # the correct month.  We update _ec_current_date on event click so when
        # Streamlit rerenders, the calendar opens back on the event's month.
        cal_options["initialDate"] = st.session_state._ec_current_date

        calendar_css = _get_calendar_css()

        # Layout — the toolbar spans the FULL width above the grid, so it is always a
        # single row: the month nav, stats, legend and Monthly/Yearly/List switcher
        # never wrap onto a second line and are never covered. When a detail card is
        # open, only the BODY below the toolbar splits 3:1 — the calendar shrinks to
        # 3/4 and the detail card takes a dedicated 1/4 column ON THE RIGHT, beside the
        # grid, NEVER overlapping it (matches the reference design). Closed → the
        # calendar is full width. (A prior position:fixed overlay floated the card on
        # TOP of the grid, covering the middle days and the view switcher — fixed here.)
        _has_panel = bool(st.session_state.ec_selected_event)

        with st.container(border=True, key="ec_cal_card"):
            _render_calendar_toolbar(
                n_events=_n_total,
                n_companies=_n_companies,
                n_ma=_n_ma,
                alerts_on=_ec_tb_alerts_on,
                open_email_dialog=lambda: _email_alerts_dialog(all_tickers_meta),
            )
            # Calendar is FULL WIDTH when no card is selected (unchanged design); the
            # body splits 3:1 only while a detail card is open, so the card sits beside
            # a slightly-narrower grid.
            if _has_panel:
                _cal_body, _detail_body = st.columns([3, 1], gap="medium")
            else:
                _cal_body, _detail_body = st.container(), None

            with _cal_body:
                _t_fc_render = _time.perf_counter()
                cal_result = st_calendar(
                    events=fc_events,
                    options=cal_options,
                    custom_css=calendar_css,
                    key=cal_key,
                    callbacks=["eventClick", "datesSet"],
                )
                log_timing("EC_PAGE_STCALENDAR_RENDER", (_time.perf_counter() - _t_fc_render) * 1000,
                           details=f"events={len(fc_events)} view={st.session_state.ec_view}", level="WARNING")
                # Footer: count scoped to the displayed period only.
                st.html(
                    f'<div class="ec-year-foot"><span>Showing <b>{_n_total:,}</b> '
                    f'events in {html.escape(_period_label)}</span></div>'
                )

            if _has_panel and _detail_body is not None:
                with _detail_body:
                    with st.container(key="ec_detail_col"):
                        _render_detail_panel(st.session_state.ec_selected_event)
                        if st.button("✕ Close", key="ec_close_detail"):
                            st.session_state.ec_selected_event = None
                            st.session_state.ec_cal_version += 1
                            st.rerun()
        log_timing("EC_PAGE_RENDER_TOTAL", (_time.perf_counter() - _t0_page) * 1000,
                   details=f"events={len(fc_events)}", level="WARNING")

        if cal_result and cal_result.get("datesSet"):
            _ds = cal_result["datesSet"]
            _new_start = str(_ds.get("startStr", ""))[:10]
            _new_end_excl = str(_ds.get("endStr", ""))[:10]
            _old_start = st.session_state.get("ec_visible_start")
            _old_end = st.session_state.get("ec_visible_end")
            if _new_start and _new_end_excl and (
                _new_start != _old_start or _new_end_excl != _old_end
            ):
                st.session_state.ec_visible_start = _new_start
                st.session_state.ec_visible_end = _new_end_excl
                log_timing(
                    "EC_OPT_DATES_SET_UPDATED",
                    0,
                    f"old_start={_old_start} old_end={_old_end} "
                    f"new_start={_new_start} new_end={_new_end_excl}",
                    level="WARNING",
                )
                st.rerun()
            else:
                log_timing(
                    "EC_OPT_DATES_SET_NO_RERUN",
                    0,
                    "reason=range_unchanged",
                    level="WARNING",
                )

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

        st.html('<div style="height:32px;"></div>')

        # Clear the early loading placeholder now that content is rendered
        _ecal_loading_hint.empty()

        render_coresight_footer()
        _tracker.finish()
        log_render_complete("earnings_calendar", _time.perf_counter() - _page_start)

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
    st.error("Something went wrong loading the Calendar.")
