# DEPRECATED: This file is unused as of 2025-07-10.
# All active market data logic lives in app/pages/market_data.py.
# Confirmed unused via grep — no imports from this module exist in app/.
# DO NOT DELETE until confirmed safe by code owner. See docs/streamlit_audit/ for audit notes.
"""
Market Data Page - PIXEL PERFECT FIGMA MATCH
============================================
Based on detailed wireframe analysis
"""
import logging
import streamlit as st
from datetime import date
from typing import Optional, List

logger = logging.getLogger(__name__)

from components.styles import hide_sidebar, render_styles, set_page_layout, COLORS
from components.navigation import render_header, render_coresight_footer
from components.toolbar import render_tabs
from components.companyProfile import render_company_profile_content, get_company_css, get_profile_rows_for_excel
from components.navigation import render_company_header
from core.auth_manager import require_auth
require_auth()

hide_sidebar()
from data.repository import CompanyOverviewRepository, CompanyRepository, IncomeStatementRepository, BalanceSheetRepository, KeyStatsRepository,ForexRepository
from data.models import IncomeStatementData, Company, BalanceSheetData
from utils.local_storage import (
    get_marketdata_company, set_marketdata_company,
    get_marketdata_tab, set_marketdata_tab,
    get_marketdata_date_range, set_marketdata_date_range
)
from utils.local_storage_manager import sync_market_data_state, save_market_data_state, get_persistent_state, set_persistent_state


def format_value(value: Optional[float], conversion_rate: float = 1.0, units_scale: float = 1.0) -> str:
    """Format value with comma separator, currency conversion, and units scaling.

    units_scale: 1.0 = Millions (default), 0.001 = Billions, 1000.0 = Thousands
    """
    if value is None:
        return "-"
    converted = value * conversion_rate * units_scale
    return f"{converted:,.2f}"


def is_bold_row(label: str) -> bool:
    """Check if row should be bold (subtotal rows) per Figma."""
    bold_labels = {"Total Revenue", "Gross Profit", "Other Operating Exp., Total",
                   "Operating Income", "Net Interest Exp."}
    return label in bold_labels


def has_underline(label: str) -> bool:
    """Check if row should have 2px dark grey underline per Figma."""
    underline_labels = {"Total Revenue", "Gross Profit", "Other Operating Exp., Total",
                        "Operating Income", "Net Interest Exp."}
    return label in underline_labels


def has_grey_separator(label: str) -> bool:
    """Check if row should have 4px grey separator line below it per Figma."""
    separator_labels = {"Total Revenue", "Gross Profit", "Operating Income"}
    return label in separator_labels


def get_indent_level(label: str) -> int:
    """Get indentation level based on row type - 0=normal (12px), 1=indented (24px)."""
    # Level 1: SUBTOTAL/SUMMARY rows - INDENTED (24px left padding)
    level_1 = {"Total Revenue", "Gross Profit", "Other Operating Exp., Total",
               "Operating Income", "Net Interest Exp."}

    # Level 0: Regular line items - NOT INDENTED (12px left padding)
    # Revenue, Other Revenue, Cost Of Goods Sold, Selling General & Admin Exp.,
    # R&D Exp., Depreciation & Amort., Other Operating Expense/(Income),
    # Interest Expense, Interest and Invest. Income

    if label in level_1:
        return 1  # Subtotals are indented
    else:
        return 0  # Everything else is NOT indented


@st.cache_data(ttl=300, show_spinner=False)
def _get_company_reported_currency(ticker: str) -> str:
    """Get the company's reported currency (not trading currency).

    SEC companies: reads `reported_currency` from income statement rows.
    YF companies: reads `financialCurrency` or `currency` from coreiq_yf_company_overview.
    Falls back to "USD" if nothing found.
    """
    return IncomeStatementRepository.get_reported_currency(ticker, date.today())


def get_conversion_rate(from_currency: str, to_currency: str) -> float:
    """Get conversion rate between currencies from the forex table."""

    if from_currency == to_currency:
        return 1.0

    return ForexRepository.get_conversion_rate(from_currency, to_currency)


def is_balance_sheet_bold_row(label: str) -> bool:
    """Check if balance sheet row should be bold (subtotal/total rows)."""
    bold_labels = {
        "Total Assets", "Total Liabilities", "Total Shareholder Equity",
        "Total Current Assets", "Total Non-Current Assets",
        "Total Current Liabilities", "Total Non-Current Liabilities"
    }
    return label.strip() in bold_labels


def has_balance_sheet_grey_separator(label: str) -> bool:
    """Check if row should have grey separator after it (major totals)."""
    separator_after = {
        "Total Assets", "Total Liabilities", "Total Shareholder Equity"
    }
    return label.strip() in separator_after


def get_balance_sheet_indent_level(label: str) -> int:
    """Get indentation level for balance sheet rows.
    0 = no indent (line items like Cash, Inventory)
    1 = one indent (subtotals like Total Current Assets)
    2 = two indents (major totals like Total Assets)
    """
    stripped = label.strip()
    # Major totals - most indented
    if stripped in {"Total Assets", "Total Liabilities", "Total Shareholder Equity"}:
        return 2
    # Subtotals - one indent
    elif stripped.startswith("Total "):
        return 1
    # Line items - no indent
    else:
        return 0


def render_balance_sheet(ticker: str, start_date: date, end_date: date, conversion_rate: float, reported_currency: str, sort_ascending: bool = True, historical_rate_map: dict = None, units_scale: float = 1.0, units_label: str = "Millions"):
    """Render the balance sheet table."""
    try:
        data = BalanceSheetRepository.get_balance_sheet_data(ticker, start_date, end_date)

        # Apply sorting based on user selection
        if not sort_ascending:
            # Reverse the periods and corresponding values
            data.periods = list(reversed(data.periods))
            for item in data.line_items:
                item.values = list(reversed(item.values))

        if data.periods and data.line_items:
            # Build table HTML
            html = '<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>'

            # Header row
            html += f'<tr class="row-grey-separator"><th>For Fiscal Period Ending<span class="header-subtext">{units_label} of {st.session_state.get("target_currency", "USD")}, except per share items.</span></th>'
            for period in data.periods:
                lines = period.label.split('\n')
                if len(lines) >= 2:
                    period_text = lines[0]
                    date_text = lines[1]
                else:
                    period_text = ""
                    date_text = period.label

                html += f'<th class="data-col"><span class="period-label">{period_text}</span><span class="period-date">{date_text}</span></th>'
            html += '</tr></thead><tbody>'

            # Data rows with currency conversion applied
            for i, item in enumerate(data.line_items):
                indent = get_balance_sheet_indent_level(item.label)
                is_bold = is_balance_sheet_bold_row(item.label)
                has_grey_sep = has_balance_sheet_grey_separator(item.label)

                # Check if NEXT row is a total/subtotal - if so, add underline to THIS row
                next_item = data.line_items[i + 1] if i + 1 < len(data.line_items) else None
                needs_underline = next_item and is_balance_sheet_bold_row(next_item.label)

                # Build row classes
                row_classes = []
                if is_bold:
                    row_classes.append("row-bold")
                if needs_underline:
                    row_classes.append("row-underline-black")
                if has_grey_sep:
                    row_classes.append("row-grey-separator")

                row_class_str = ' '.join(row_classes) if row_classes else ''

                html += f'<tr class="{row_class_str}">'

                # First column - label with proper indentation
                # indent-0: no indent (line items)
                # indent-1: one indent (subtotals like Total Current Assets)
                # indent-2: two indents (major totals like Total Assets)
                display_label = item.label.strip()
                html += f'<td class="indent-{indent}">{display_label}</td>'

                # Data columns with converted values
                for col_idx, val in enumerate(item.values):
                    # Use per-column rate from historical_rate_map if available
                    if historical_rate_map and col_idx < len(data.periods):
                        period_date = data.periods[col_idx].date
                        col_rate = historical_rate_map.get(period_date, conversion_rate)
                    else:
                        col_rate = conversion_rate
                    formatted = format_value(val, col_rate, units_scale)
                    html += f'<td class="data-cell">{formatted}</td>'

                html += '</tr>'

            html += '</tbody></table></div></div>'
            st.html(html)




        else:
            st.info("No balance sheet data available for the selected date range")

    except Exception as e:
        st.error(f"Error loading balance sheet: {e}")


def get_cash_flow_indent_level(label: str) -> int:
    """Get indentation level for cash flow rows.
    0 = no indent (line items)
    1 = one indent (section totals like Operating Cash Flow)
    2 = two indents (major totals like Net Change in Cash)
    """
    stripped = label.strip()
    # Major totals - most indented
    if stripped in {"Net Change in Cash", "Cash at End of Period"}:
        return 2
    # Section totals - one indent
    elif stripped in {"Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow"}:
        return 1
    # Line items - no indent
    else:
        return 0


def is_cash_flow_bold_row(label: str) -> bool:
    """Check if row should be bold (totals and subtotals)."""
    bold_labels = {
        "Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow",
        "Net Change in Cash", "Cash at Beginning of Period", "Cash at End of Period"
    }
    return label.strip() in bold_labels


def has_cash_flow_grey_separator(label: str) -> bool:
    """Check if row should have grey separator after it."""
    grey_after = {
        "Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow",
        "Cash at End of Period"
    }
    return label.strip() in grey_after


def render_cash_flow(ticker: str, start_date: date, end_date: date, conversion_rate: float, reported_currency: str, sort_ascending: bool = True, historical_rate_map: dict = None, units_scale: float = 1.0, units_label: str = "Millions"):
    """Render the cash flow statement table."""
    try:
        from data.repository import CashFlowRepository

        data = CashFlowRepository.get_cash_flow_data(ticker, start_date, end_date)

        # Apply sorting based on user selection
        if not sort_ascending:
            # Reverse the periods and corresponding values
            data.periods = list(reversed(data.periods))
            for item in data.line_items:
                item.values = list(reversed(item.values))

        if data.periods and data.line_items:
            # Build table HTML
            html = '<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>'

            # Header row
            html += f'<tr class="row-grey-separator"><th>For Fiscal Period Ending<span class="header-subtext">{units_label} of {st.session_state.get("target_currency", "USD")}, except per share items.</span></th>'
            for period in data.periods:
                lines = period.label.split('\n')
                if len(lines) >= 2:
                    period_text = lines[0]
                    date_text = lines[1]
                else:
                    period_text = ""
                    date_text = period.label

                html += f'<th class="data-col"><span class="period-label">{period_text}</span><span class="period-date">{date_text}</span></th>'
            html += '</tr></thead><tbody>'

            # Data rows with currency conversion applied
            for i, item in enumerate(data.line_items):
                indent = get_cash_flow_indent_level(item.label)
                is_bold = is_cash_flow_bold_row(item.label)
                has_grey_sep = has_cash_flow_grey_separator(item.label)

                # Check if NEXT row is a total/subtotal - if so, add underline to THIS row
                next_item = data.line_items[i + 1] if i + 1 < len(data.line_items) else None
                needs_underline = next_item and is_cash_flow_bold_row(next_item.label)

                # Build row classes
                row_classes = []
                if is_bold:
                    row_classes.append("row-bold")
                if needs_underline:
                    row_classes.append("row-underline-black")
                if has_grey_sep:
                    row_classes.append("row-grey-separator")

                row_class_str = ' '.join(row_classes) if row_classes else ''

                html += f'<tr class="{row_class_str}">'

                # First column - label with proper indentation
                display_label = item.label.strip()
                html += f'<td class="indent-{indent}">{display_label}</td>'

                # Data columns with converted values
                for col_idx, val in enumerate(item.values):
                    # Use per-column rate from historical_rate_map if available
                    if historical_rate_map and col_idx < len(data.periods):
                        period_date = data.periods[col_idx].date
                        col_rate = historical_rate_map.get(period_date, conversion_rate)
                    else:
                        col_rate = conversion_rate
                    formatted = format_value(val, col_rate, units_scale)
                    html += f'<td class="data-cell">{formatted}</td>'

                html += '</tr>'

            html += '</tbody></table></div></div>'
            st.html(html)




        else:
            st.info("No cash flow data available for the selected date range")

    except Exception as e:
        st.error(f"Error loading cash flow statement: {e}")


def render_stock_quote(ticker: str, currency: str = "USD") -> None:
    """Render Stock Quote and Chart table matching Figma design node-id=20660-223833.

    Layout (Figma):
    ┌──────────────────────────────────────────────────────┐
    │  Stock Quote and Chart (Currency: USD)  [bold 16px]  │
    ├────────────────────────┬─────────────────────────────┤
    │  Left col (4 sub-cols) │  Right col (chart area)     │
    │  7 rows × [label|val | label|val]                    │
    └────────────────────────┴─────────────────────────────┘
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from data.repository import StockQuoteRepository

    quote   = StockQuoteRepository.get_latest_quote(ticker)
    overview = StockQuoteRepository.get_overview_data(ticker)
    history  = StockQuoteRepository.get_price_history(ticker, days=1825)  # 60 months

    # ── helper formatters ─────────────────────────────────
    def _fmt_price(v):
        if v is None:
            return "-"
        return f"{v:,.2f}"

    def _fmt_mm(v):
        if v is None:
            return "-"
        return f"{v:,.1f}"

    def _fmt_pct(v):
        if v is None:
            return "-"
        return f"{v:.2f}%"

    def _fmt_change(v):
        if v is None:
            return "-"
        sign = "+" if v > 0 else ""
        return f"{sign}{v:,.2f}"

    def _fmt_pct_change(v):
        if v is None:
            return "-"
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.2f}%"

    def _fmt_pe(v):
        if v is None:
            return "-"
        return f"{v:.2f}x"

    # ── extract values ────────────────────────────────────
    if quote:
        last       = _fmt_price(quote.get("close"))
        open_      = _fmt_price(quote.get("open"))
        prev_close = _fmt_price(quote.get("close"))      # same day prev_close ≈ close
        change     = _fmt_change(quote.get("change_on_day"))
        change_pct = _fmt_pct_change(quote.get("change_percent"))
        day_hl     = f"{_fmt_price(quote.get('high'))} / {_fmt_price(quote.get('low'))}"
    else:
        last = open_ = prev_close = change = change_pct = day_hl = "-"

    if overview:
        market_cap   = _fmt_mm(overview.get("market_cap_mm"))
        shares_out   = _fmt_mm(overview.get("shares_outstanding_mm"))
        div_yield    = _fmt_pct(overview.get("dividend_yield"))
        diluted_eps  = _fmt_price(overview.get("diluted_eps"))
        pe           = _fmt_pe(overview.get("pe_ratio"))
        w52h         = _fmt_price(overview.get("week_52_high"))
        w52l         = _fmt_price(overview.get("week_52_low"))
        week_52_hl   = f"{w52h} / {w52l}"
    else:
        market_cap = shares_out = div_yield = diluted_eps = pe = week_52_hl = "-"

    # Float % and Shares Sold Short — not in DB
    float_pct  = "-"
    short_mm   = "-"

    # ── 7 rows (left_label, left_val, right_label, right_val) ──
    rows = [
        ("Last (Delayed)",     last,       "Market Cap (mm)",              market_cap),
        ("Open",               open_,      "Shares Out. (mm)",             shares_out),
        ("Previous Close",     prev_close, "Float %",                      float_pct),
        ("Change on Day",      change,     "Shares Sold Short (mm)",       short_mm),
        ("Change % on Day",    change_pct, "Dividend Yield %",             div_yield),
        ("Day High/Low",       day_hl,     "Diluted EPS Excl. Extra Items", diluted_eps),
        ("52 wk High/Low",     week_52_hl, "P/Diluted EPS Before Extra",   pe),
    ]

    # ── CSS ───────────────────────────────────────────────
    css = """
    <style>
    .sq-header {
        font-family: 'Roboto', sans-serif;
        background: #F2F2F2;
        border: 1px solid #CFCFCF;
        border-bottom: none;
        border-radius: 12px 12px 0 0;
        padding: 4px 12px;
        font-weight: 700;
        font-size: 16px;
        color: #000000;
        height: 32px;
        display: flex;
        align-items: center;
        margin-bottom: 0;
        box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.06), -2px 0 6px rgba(0, 0, 0, 0.04), 2px 0 6px rgba(0, 0, 0, 0.04);
    }
    .sq-table-box {
        border: 1px solid #CBCACA;
        border-top: none;
        border-radius: 0 0 12px 12px;
        box-shadow: 0 2px 16px rgba(0, 0, 0, 0.10), 0 1px 4px rgba(0, 0, 0, 0.06);
        overflow: hidden;
    }
    .sq-table {
        width: 100%;
        border-collapse: collapse;
        table-layout: fixed;
        font-family: 'Roboto', sans-serif;
    }
    .sq-table tr {
        height: 24px;
    }
    .sq-table td {
        height: 24px;
        padding: 0 12px;
        vertical-align: middle;
        font-size: 14px;
        overflow: hidden;
        white-space: nowrap;
    }
    .sq-lbl {
        background: #F9F9F9;
        color: #4F4F4F;
        width: 33%;
    }
    .sq-val {
        background: #FFFFFF;
        color: #000000;
        text-align: right;
        width: 17%;
        border-right: 1px solid #CFCFCF;
        vertical-align: bottom;
        padding-bottom: 2px;
    }
    .sq-val:last-child {
        border-right: none;
    }
    </style>
    """
    st.html(css)

    # ── Section header ────────────────────────────────────
    st.html(f'<div class="sq-header">Stock Quote and Chart (Currency: {currency})</div>')

    # ── Body: table left (63%) | chart right (37%) ────────
    col_tbl, col_chart = st.columns([63, 37])

    with col_tbl:
        tbl_rows = ""
        for ll, lv, rl, rv in rows:
            tbl_rows += (
                f'<tr>'
                f'<td class="sq-lbl">{ll}</td>'
                f'<td class="sq-val">{lv}</td>'
                f'<td class="sq-lbl">{rl}</td>'
                f'<td class="sq-val">{rv}</td>'
                f'</tr>'
            )
        st.html(f'<div class="sq-table-box"><table class="sq-table">{tbl_rows}</table></div>')

    with col_chart:
        if history:
            dates   = [h["date"] for h in history]
            closes  = [h["close"] for h in history]
            volumes = [h.get("volume") or 0 for h in history]

            fig = make_subplots(
                rows=2, cols=1,
                row_heights=[0.72, 0.28],
                shared_xaxes=True,
                vertical_spacing=0.0,
            )

            # ── Price area with fill ──
            fig.add_trace(
                go.Scatter(
                    x=dates, y=closes,
                    mode="lines",
                    fill="tozeroy",
                    fillcolor="rgba(214,46,47,0.07)",
                    line=dict(color="#D62E2F", width=2),
                    name="Price",
                    showlegend=False,
                    hovertemplate="<b>%{x|%b %d, %Y}</b><br>Close: <b>$%{y:,.2f}</b><extra></extra>",
                ),
                row=1, col=1,
            )

            # ── Volume bars — red tint, transparent ──
            fig.add_trace(
                go.Bar(
                    x=dates, y=volumes,
                    marker_color="rgba(214,46,47,0.20)",
                    name="Volume",
                    showlegend=False,
                    hovertemplate="<b>%{x|%b %d, %Y}</b><br>Vol: <b>%{y:,.0f}</b><extra></extra>",
                ),
                row=2, col=1,
            )

            fig.update_layout(
                title=dict(
                    text="Stock Price",
                    font=dict(size=11, color="#2D2A29", family="Roboto"),
                    x=0.5, xanchor="center",
                    y=0.98, yanchor="top",
                ),
                margin=dict(l=10, r=10, t=26, b=6),
                plot_bgcolor="#FFFFFF",
                paper_bgcolor="#FCFCFC",
                height=215,
                font=dict(family="Roboto", size=9, color="#888"),
                hovermode="x unified",
                hoverlabel=dict(
                    bgcolor="white",
                    bordercolor="#D62E2F",
                    font=dict(size=10, color="#2D2A29"),
                ),
                bargap=0.1,
            )

            # Price row — no axis labels, subtle grid only
            fig.update_xaxes(showgrid=False, showticklabels=False,
                             showline=False, zeroline=False, row=1, col=1)
            fig.update_yaxes(
                showgrid=True, gridcolor="#F0F0F0", gridwidth=1,
                showticklabels=False,
                showline=False, zeroline=False,
                row=1, col=1,
            )

            # Volume row — no axis labels
            fig.update_xaxes(
                showgrid=False, showticklabels=False,
                showline=False, zeroline=False, ticks="",
                row=2, col=1,
            )
            fig.update_yaxes(
                showgrid=False, showticklabels=False,
                zeroline=False, showline=False,
                row=2, col=1,
            )

            st.plotly_chart(
                fig,
                width="stretch",
                config={"displayModeBar": False},
            )
        else:
            st.caption("No price history available")


def render_page():
    """Main render function - PIXEL PERFECT FIGMA MATCH."""
    import time as _time
    _perf_logger = logging.getLogger("market_data.perf")
    _page_start = _time.perf_counter()

    # Initialize local storage manager and sync state
    storage_manager = sync_market_data_state()

    # Resolve ticker: Check if we're initializing (no query params) or if query params exist
    query_ticker = st.query_params.get("ticker")
    stored_ticker = get_marketdata_company()
    # Cross-page sync: another page may have set active_ticker
    cross_page_ticker = st.session_state.get("active_ticker")

    # Priority: cross-page ticker (if different from URL = user changed on another page)
    #           > URL ticker > stored ticker
    if cross_page_ticker and query_ticker and cross_page_ticker != query_ticker:
        # User changed company on another page THEN clicked nav link (which has old ticker)
        # active_ticker is more recent — use it
        selected_ticker = cross_page_ticker
    elif query_ticker:
        selected_ticker = query_ticker
    elif cross_page_ticker:
        selected_ticker = cross_page_ticker
    elif stored_ticker:
        selected_ticker = stored_ticker
    else:
        selected_ticker = "M"  # fallback

    # Always update query params to reflect current state (for bookmarking/navigation)
    # BUT: Don't set ticker=ALL in URL - remove param if ALL is selected
    if selected_ticker and selected_ticker != "ALL":
        st.query_params["ticker"] = selected_ticker
    elif "ticker" in st.query_params and st.query_params["ticker"] == "ALL":
        del st.query_params["ticker"]

    # Save the selected ticker to local storage to persist across sessions
    set_marketdata_company(selected_ticker)

    # Detect ticker change — clear date/sort widget keys so Streamlit doesn't
    # raise "widget created with default value but also set via Session State API"
    prev_ticker = st.session_state.get("_prev_ticker_market_data")
    # On page refresh, prev_ticker is None (fresh session) but ticker hasn't truly changed.
    # Only wipe date query params when the user ACTUALLY switched to a different company.
    _ticker_truly_changed = (prev_ticker is not None and prev_ticker != selected_ticker)
    if prev_ticker != selected_ticker:
        # Clear shared filter keys so they re-initialise for the new ticker
        for _key in (
            "start_dt_shared", "end_dt_shared", "sort_order_select_shared",
            "date_range_market_data_shared", "sort_order_market_data_shared",
            "_mdlc_date_range", "_mdlc_sort", "_mdlc_conv",
            "_mdlc_from_curr", "_mdlc_to_curr", "_mdlc_units",
        ):
            if _key in st.session_state:
                del st.session_state[_key]
        # Only clear date query params on a real company switch — NOT on page refresh.
        # On refresh, prev_ticker is None (empty session) even though ticker didn't change.
        if _ticker_truly_changed:
            for _qp in ("start_dt", "end_dt", "to_curr"):
                if _qp in st.query_params:
                    del st.query_params[_qp]
        # Also clear legacy per-tab keys (backward compat for any cached session)
        for _tab in ["income_statement", "balance_sheet", "cash_flow", "key_stats"]:
            for _prefix in ("start_dt_", "end_dt_", "sort_order_select_"):
                _key = f"{_prefix}{_tab}"
                if _key in st.session_state:
                    del st.session_state[_key]
            _dr_key = f"date_range_market_data_{_tab}"
            if _dr_key in st.session_state:
                del st.session_state[_dr_key]
        # Reset FROM and TO currency so they re-initialise to the new ticker's reported currency
        for _curr_key in ("from_currency", "currency_from_unified",
                          "target_currency", "currency_to_unified"):
            if _curr_key in st.session_state:
                del st.session_state[_curr_key]
        st.session_state["_prev_ticker_market_data"] = selected_ticker

    # Store the selected ticker in session state for persistence
    st.session_state.selected_ticker_market_data = selected_ticker
    # Write to shared active_ticker for cross-page synchronization
    st.session_state.active_ticker = selected_ticker

    # Handle ALL selection - use a default ticker for company data fetch
    # ALL is not a real ticker, it's a filter option for showing all companies
    _company_fetch_ticker = selected_ticker if selected_ticker and selected_ticker != "ALL" else "M"
    company = CompanyOverviewRepository.get_company_overview(_company_fetch_ticker)

    if not company:
        st.error(f"Company data not found for ticker: {_company_fetch_ticker}")
        st.stop()

    # Get the company's reported currency early — used as default for both currency dropdowns
    _early_reported_currency = _get_company_reported_currency(_company_fetch_ticker)

    # Resolve tab: Check if we're initializing (no query params) or if query params exist
    query_tab = st.query_params.get("tab")
    stored_tab = get_marketdata_tab()

    # If no query tab, use stored tab, otherwise use query tab
    selected_tab = stored_tab if not query_tab else query_tab

    # Validate tab selection
    valid_tabs = ["income_statement", "balance_sheet", "cash_flow", "key_stats", "company_profile"]
    if selected_tab not in valid_tabs:
        selected_tab = "company_profile"

    # Always update query params to reflect current state (for bookmarking/navigation)
    if not query_tab and stored_tab and stored_tab in valid_tabs:
        st.query_params["tab"] = stored_tab

    # Store the selected tab in session state for persistence
    st.session_state.selected_tab_market_data = selected_tab

    # Save to local storage for persistence across sessions
    set_marketdata_tab(selected_tab)

    # Shared filter state — same across all financial tabs so switching tabs preserves filters
    tab_state_prefix = f"{selected_tab}_"
    date_range_key = "date_range_market_data_shared"
    sort_order_key = "sort_order_market_data_shared"

    # Initialize defaults
    min_date = max_date = start_date = end_date = None
    available_dates = []
    reported_currency = "USD"
    conversion_rate = 1.0

    # ── Filter initialization: query_params > session_state > localStorage > default ──
    # st.query_params is synchronous (part of URL) so it survives page refresh
    # immediately — no React lifecycle delay unlike streamlit-local-storage.
    if 'target_currency' not in st.session_state:
        # Default to company's reported currency (not USD) unless URL param overrides it
        _qp_to_curr = st.query_params.get("to_curr")
        st.session_state.target_currency = _qp_to_curr if _qp_to_curr else _early_reported_currency

    if 'conversion_mode' not in st.session_state:
        _qp_conv = st.query_params.get("conv_mode", "Today's Spot Rate")
        st.session_state.conversion_mode = _qp_conv if _qp_conv in ("Today's Spot Rate", "Historical") else "Today's Spot Rate"

    if 'units' not in st.session_state:
        _qp_units = st.query_params.get("units_sel", "Millions (mm)")
        st.session_state.units = _qp_units if _qp_units in ("Millions (mm)", "Billions (bn)", "Thousands (k)") else "Millions (mm)"

    # Initialize sort order
    if sort_order_key not in st.session_state:
        _qp_sort = st.query_params.get("sort_ord")
        st.session_state[sort_order_key] = _qp_sort if _qp_sort in ("Earliest", "Latest") else get_persistent_state(sort_order_key, 'Earliest')

    # Get dates based on selected tab (skip for company_profile)
    if selected_tab != "company_profile":
        if selected_tab == "balance_sheet":
            min_date, max_date = BalanceSheetRepository.get_date_range(selected_ticker)
            available_dates = BalanceSheetRepository.get_available_dates(selected_ticker)
        elif selected_tab == "cash_flow":
            from data.repository import CashFlowRepository
            min_date, max_date = CashFlowRepository.get_date_range(selected_ticker)
            available_dates = CashFlowRepository.get_available_dates(selected_ticker)
        elif selected_tab == "key_stats":
            min_date, max_date = KeyStatsRepository.get_date_range(selected_ticker)
            available_dates = KeyStatsRepository.get_available_dates(selected_ticker)
        else:
            min_date, max_date = IncomeStatementRepository.get_date_range(selected_ticker)
            available_dates = IncomeStatementRepository.get_available_dates(selected_ticker)


    # ==================== GLOBAL CSS - PIXEL PERFECT FIGMA SPECS ====================
    st.html("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:ital,wght@0,400;0,600;0,700;1,400&display=swap');

    :root {
        /* Figma Colors - Exact Match */
        --white: #FFFFFF;
        --light-grey: #F9F9F9;
        --black: #000000;
        --dark-grey: #4F4F4F;
        --border-light: #CFCFCF;
        --border-medium: #C1CFCF;

        /* Accent Colors */
        --primary-red: #D62E2F;

        /* Typography - Figma Specs */
        --font-family: 'Roboto', sans-serif;
        --font-size-base: 16px;
        --font-size-small: 12px;
        --line-height-base: 19px;
        --line-height-compact: 100%;

        /* Font Weights */
        --font-weight-regular: 400;
        --font-weight-semibold: 600;
        --font-weight-bold: 700;

        /* Spacing */
        --padding-normal: 12px;
        --padding-indented: 24px;
        --padding-top: 4px;
        --gap-small: 2px;
        --gap-medium: 6px;

        /* Dimensions */
        --header-height: 48px;
        --row-height: 24px;
        --border-width: 1px;
        --underline-width: 2px;

        /* Column Widths */
        --first-column-width: 400px;
        --date-column-width: 164px;
    }

    /* Hide Streamlit elements */
    #MainMenu, footer, header, .stDeployButton {display: none !important;}

    /* Main content container - 110px left/right padding per Figma */
    .block-container {
        padding-left: 110px !important;
        padding-right: 110px !important;
        padding-top: 0 !important; /* Remove default top padding */
        padding-bottom: 0 !important; /* Remove default bottom padding */
        max-width: 1440px !important;
        margin: 0 auto !important;
    }

    /* Ensure main container has proper padding */
    .main .block-container {
        padding-left: 110px !important;
        padding-right: 110px !important;
    }

    /* Hide duplicate Streamlit button tabs (we use styled HTML tabs instead) */
    div[data-testid="stElementContainer"].st-key-tabbtn_income_statement,
    div[data-testid="stElementContainer"].st-key-tabbtn_key_stats,
    div[data-testid="stElementContainer"].st-key-tabbtn_company_profile {
        display: none !important;
    }

    /* Page title - aligned to 110px left margin */
    .page-title {
        font-family: var(--font-family);
        font-weight: var(--font-weight-bold);
        font-size: var(--font-size-small);
        letter-spacing: 0.5px;
        text-transform: uppercase;
        color: var(--primary-red);
        margin: 32px 0 4px 0;
        padding-left: 0;
    }

    /* Company selector */
    .company-selector {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 24px;
        padding-left: 0;
    }

    .company-name {
        font-family: var(--font-family);
        font-weight: var(--font-weight-semibold);
        font-size: 22px;
        color: var(--black);
    }

    .dropdown-chevron {
        font-size: var(--font-size-small);
        color: var(--dark-grey);
    }

    /* ==================== NAVIGATION TABS - FIGMA EXACT SPECS ==================== */
    .tabs-container {
        display: flex;
        gap: 48px;  /* Figma spec: 48px gap between tabs */
        border-bottom: var(--border-width) solid var(--border-light);
        margin-bottom: 30px;
        padding-top: 8px;  /* Align with Figma 80px header height */
    }

    .tab {
        font-family: var(--font-family);
        font-size: var(--font-size-base);  /* 16px Body 1 */
        font-weight: 500;  /* Medium weight from Figma */
        line-height: 26px;  /* Figma height spec */
        padding: 12px 0;
        cursor: pointer;
        border-bottom: 3px solid transparent;
        margin-bottom: -1px;
        transition: all 0.2s ease;
        white-space: nowrap;
    }

    .tab-active {
        color: var(--primary-red);  /* #D62E2F */
        font-weight: 500;  /* Medium */
        border-bottom-color: var(--primary-red);
    }

    .tab-inactive {
        color: rgba(0, 0, 0, 0.6);  /* 60% opacity for inactive */
        font-weight: 400;  /* Regular */
    }

    .tab-inactive:hover {
        color: rgba(0, 0, 0, 0.8);  /* Slight hover effect */
    }

    /* Filter row - Figma Design Match */
    .filter-label {
        font-family: var(--font-family);
        font-size: 15px;
        font-weight: 600;
        color: #4F4F4F;
        margin-bottom: -7px;
        margin-top: 0;
        line-height: normal;
        display: block;
        padding-top: 12px;

    }

    /* Streamlit selectbox styling to match Figma */
    div[data-testid="stSelectbox"] {
        margin-top: 0 !important;

    }

    /* Override the selectbox container */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] {
        background-color: #F2F2F2 !important;
        border-radius: 4px !important;
        border: none !important;
        height: 36px !important;
        min-height: 36px !important;
    }

    /* Override the inner control */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] > div {
        background-color: #F2F2F2 !important;
        border-radius: 4px !important;
        border: none !important;
        min-height: 36px !important;
        height: 36px !important;
        padding: 0 10px !important;
    }

    /* Override the input text - prevent truncation */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] input {
        font-family: 'Roboto', sans-serif !important;
        font-size: 14px !important;
        color: #000000 !important;
        text-overflow: clip !important;
        overflow: visible !important;
    }

    /* Override the value container - ensure full text is shown */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] > div > div:nth-child(2) {
        padding: 0 !important;
        text-overflow: clip !important;
        overflow: visible !important;
        white-space: nowrap !important;
    }

    /* Ensure selectbox value text is not truncated */
    div[data-testid="stSelectbox"] [data-baseweb="select"] span {
        text-overflow: clip !important;
        overflow: visible !important;
    }

    /* Override the dropdown indicator */
    div[data-testid="stSelectbox"] > div[data-baseweb="select"] svg {
        color: #4F4F4F !important;
    }

    /* Streamlit selectbox styling */
    div[data-testid="stSelectbox"] > div > div {
        background-color: var(--white) !important;
        border: var(--border-width) solid var(--border-light) !important;
        border-radius: 6px !important;
    }

    div[data-testid="stSelectbox"] > div > div > div {
        padding: auto !important;
        font-family: var(--font-family) !important;
        font-size: 14px !important;
        color: var(--black) !important;

    }

    /* ==================== TABLE STYLING - PIXEL PERFECT FROM FIGMA ==================== */

    .table-container {
        border: var(--border-width) solid var(--border-light);
        border-radius: 12px;
        overflow: hidden;
        background: var(--white);
        margin-bottom: 30px;
        box-shadow: 0 2px 16px rgba(0, 0, 0, 0.10), 0 1px 4px rgba(0, 0, 0, 0.06);
    }

    .table-scroll {
        max-height: 600px;
        overflow-x: auto;
        overflow-y: auto;
    }

    .data-table {
        width: 100%;
        border-collapse: separate;
        border-spacing: 0;
        font-family: var(--font-family);
        font-size: var(--font-size-base);
    }

    /* ========== HEADER ROW - 48px height, grey background ========== */
    .data-table thead {
        background-color: var(--light-grey);
        position: sticky;
        top: 0;
        z-index: 10;
    }

    .data-table th {
        height: var(--header-height);
        padding: var(--padding-top) var(--padding-normal);
        text-align: left;
        font-weight: var(--font-weight-bold);
        font-size: var(--font-size-base);
        line-height: var(--line-height-base);
        color: var(--black);
        border-bottom: var(--border-width) solid var(--border-light);
        background-color: var(--light-grey);
        vertical-align: top;
    }

    /* STICKY FIRST COLUMN */
    .data-table th:first-child,
    .data-table td:first-child {
        position: sticky;
        left: 0;
        z-index: 5;
        background-color: var(--light-grey);
    }

    .data-table th:first-child {
        min-width: var(--first-column-width);
        max-width: var(--first-column-width);
    }

    .data-table td:first-child {
        min-width: var(--first-column-width);
        max-width: var(--first-column-width);
    }

    /* Date column headers - right aligned */
    .data-table th.data-col {
        text-align: right;
        min-width: var(--date-column-width);
        padding: var(--padding-top) var(--padding-top) var(--padding-top) var(--padding-normal);
    }

    /* Header subtext */
    .header-subtext {
        display: block;
        font-size: var(--font-size-small);
        font-weight: var(--font-weight-regular);
        font-style: italic;
        color: var(--dark-grey);
        margin-top: var(--gap-small);
        line-height: var(--line-height-compact);
    }

    /* Period labels in header */
    .period-label {
        display: block;
        font-size: var(--font-size-base);
        font-weight: var(--font-weight-bold);
        color: var(--black);
        line-height: var(--line-height-compact);
        text-align: right;
    }

    .period-date {
        display: block;
        font-size: var(--font-size-base);
        font-weight: var(--font-weight-bold);
        color: var(--black);
        line-height: var(--line-height-compact);
        text-align: right;
        margin-top: var(--gap-small);
    }

    /* ========== DATA ROWS - 24px height ========== */
    .data-table tbody tr {
        height: var(--row-height);
    }

    .data-table td {
        height: var(--row-height);
        padding: var(--padding-top) var(--padding-normal);
        border-bottom: var(--border-width) solid var(--border-light);
        vertical-align: bottom;
        font-size: var(--font-size-base);
        line-height: var(--line-height-base);
    }

    /* First column - label column */
    .data-table td:first-child {
        text-align: left;
        background-color: var(--light-grey);
        color: var(--black);
        font-weight: var(--font-weight-regular);
        vertical-align: middle;
    }

    /* Data cells - numbers */
    .data-table td.data-cell {
        text-align: right;
        font-variant-numeric: tabular-nums;
        background-color: var(--white);
        color: var(--black);
        font-weight: var(--font-weight-regular);
        padding: var(--padding-top) 8px var(--padding-top) var(--padding-normal);
    }

    /* ========== ROW INDENTATION ========== */
    /* Reversed: Line items = no indent, Totals = indented */
    .indent-0 {
        padding-left: 8px !important;
    }

    .indent-1 {
        padding-left: 40px !important;
    }

    .indent-2 {
        padding-left: 80px !important;
    }

    /* ========== BOLD ROWS (Subtotals) ========== */
    .row-bold td:first-child {
        font-weight: var(--font-weight-bold) !important;
        color: var(--dark-grey) !important;
    }

    .row-bold td.data-cell {
        font-weight: var(--font-weight-semibold) !important;
        color: var(--dark-grey) !important;
    }

    /* ========== UNDERLINES - 2px thick, dark grey ========== */
    .row-underline-black td.data-cell {
        position: relative;
    }

    .row-underline-black td.data-cell::after {
        content: '';
        position: absolute;
        bottom: 0;
        left: 5%;
        right: 5%;
        height: var(--underline-width);
        background-color: var(--dark-grey);
    }

    /* ========== INDENTED ROW BACKGROUNDS ========== */
    .row-indent-grey {
        background-color: var(--light-grey) !important;
    }

    /* ========== GREY SEPARATOR - 4px thick ========== */
    .row-grey-separator td {
        border-bottom: 4px solid #9CA3AF !important;
    }

    /* ========== CURRENCY CONVERSION - LEFT SIDE ONLY ========== */
    .currency-section {
        margin-top: 30px;
        max-width: 500px;
    }

    .currency-label {
        font-family: var(--font-family);
        font-size: var(--font-size-base);
        font-weight: var(--font-weight-bold);
        color: var(--dark-grey);
    }

    .currency-row {
        display: flex;
        align-items: center;
        gap: 16px;
    }

    .currency-box {
        background: var(--white);
        border: var(--border-width) solid var(--border-light);
        border-radius: 6px;
        padding: 6px 10px;
        font-family: var(--font-family);
        font-size: 14px;
        color: var(--black);
        min-width: 120px;
    }

    .currency-arrow {
        color: var(--dark-grey);
        font-size: 18px;
        font-weight: 300;
    }
    </style>
    """)
    # Inject company profile CSS for consistent styling across all tabs
    st.markdown(get_company_css(), unsafe_allow_html=True)
    # Excel download icon button styling
    st.markdown("""
    <style>
    div[data-testid="stDownloadButton"] button {
        background: #FFFFFF !important;
        border: 1.5px solid #D0D0D0 !important;
        color: #555555 !important;
        border-radius: 8px !important;
        padding: 0 !important;
        font-size: 17px !important;
        line-height: 1 !important;
        cursor: pointer !important;
        min-width: 38px !important;
        height: 36px !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        transition: border-color 0.15s, color 0.15s, background 0.15s !important;
    }
    div[data-testid="stDownloadButton"] button:hover {
        border-color: #D62E2F !important;
        color: #D62E2F !important;
        background: #FFF5F5 !important;
    }
    div[data-testid="stDownloadButton"] { margin-top: 6px; }
    </style>
    """, unsafe_allow_html=True)

    # ==================== TITLE SECTION ====================

    render_company_header(
        company_name=company.name,
        ticker=company.ticker,
        exchange=company.exchange or "NYSE"
    )
    selected_tab = render_tabs(selected_tab)
    if not min_date or not max_date:
        if selected_tab != "company_profile":  # Only show message if it's a financial tab
            st.info("No financial data available for this company on the selected tab.")
    else:
        # Compute default start = last 5 years from max_date, clamped to min_date
        try:
            _default_start = max_date.replace(year=max_date.year - 5)
        except ValueError:  # Feb 29 in non-leap year
            _default_start = max_date.replace(year=max_date.year - 5, day=28)
        _default_start = max(_default_start, min_date)

        # ── Date range resolution: session_state > query_params > localStorage > default ──
        if date_range_key in st.session_state:
            stored_start, stored_end = st.session_state[date_range_key]
            start_date = date.fromisoformat(stored_start) if stored_start else _default_start
            end_date = date.fromisoformat(stored_end) if stored_end else max_date
        else:
            # Try query params first (synchronous, survives page refresh)
            _qp_start = st.query_params.get("start_dt")
            _qp_end   = st.query_params.get("end_dt")
            if _qp_start and _qp_end:
                try:
                    start_date = max(min(date.fromisoformat(_qp_start), max_date), min_date)
                    end_date   = max(min(date.fromisoformat(_qp_end),   max_date), min_date)
                except ValueError:
                    start_date, end_date = _default_start, max_date
            else:
                # Fall back to legacy localStorage helper → default
                stored_start, stored_end = get_marketdata_date_range()
                start_date = date.fromisoformat(stored_start) if stored_start else _default_start
                end_date   = date.fromisoformat(stored_end)   if stored_end   else max_date
            st.session_state[date_range_key] = (start_date.isoformat(), end_date.isoformat())

        # Always write back so downstream code can read it
        st.session_state[date_range_key] = (start_date.isoformat(), end_date.isoformat())

            # Get currency from database based on tab
        if selected_tab == "balance_sheet":
                reported_currency = BalanceSheetRepository.get_reported_currency(
                    selected_ticker, end_date
                ) or "USD"
        elif selected_tab == "cash_flow":
                from data.repository import CashFlowRepository
                reported_currency = CashFlowRepository.get_reported_currency(
                    selected_ticker, end_date
                ) or "USD"
        elif selected_tab == "key_stats":
                reported_currency = KeyStatsRepository.get_reported_currency(
                    selected_ticker, end_date
                ) or "USD"
        else:
                reported_currency = IncomeStatementRepository.get_reported_currency(
                    selected_ticker, end_date
                ) or "USD"

        # Initialize FROM currency — default to reported_currency for the current ticker
        if 'from_currency' not in st.session_state:
            st.session_state.from_currency = reported_currency

        # Get conversion rate using the selected FROM currency
        conversion_rate = get_conversion_rate(st.session_state.from_currency, st.session_state.target_currency)

        # Compute per-date historical rates if Historical mode is selected
        historical_rate_map = None
        if st.session_state.conversion_mode == "Historical" and available_dates:
            fiscal_dates_for_rates = []
            for d in available_dates:
                if start_date <= d <= end_date:
                    fiscal_dates_for_rates.append(d)

            if fiscal_dates_for_rates:
                historical_rate_map = ForexRepository.get_conversion_rates_bulk(
                    st.session_state.from_currency,
                    st.session_state.target_currency,
                    tuple(fiscal_dates_for_rates)
                )

    # ==================== FILTER ROW - DATES & SORT ====================

    # Only show filters for financial tabs with valid date data
    if selected_tab not in ["company_profile"] and start_date and end_date and available_dates:
        date_options = [d.strftime("%B %Y") for d in available_dates]
        date_values = {d.strftime("%B %Y"): d for d in available_dates}

        curr_start = start_date.strftime("%B %Y")
        start_idx = date_options.index(curr_start) if curr_start in date_options else 0

        curr_end = end_date.strftime("%B %Y")
        end_idx = date_options.index(curr_end) if curr_end in date_options else len(date_options) - 1

        # ── Widget-state persistence fix ─────────────────────────────────────
        # Problem: streamlit-local-storage fires on Render 2 (React mounts late).
        # Render 1 sets widget state to defaults ("Jan 2020").
        # Render 2 loads real stored dates ("Jan 2008") into session_state —
        # but widget state is stale ("Jan 2020") → date_changed fires →
        # OVERWRITES localStorage with wrong defaults.
        #
        # Fix: track "last confirmed" authoritative values. If stored ≠ last
        # confirmed, localStorage gave us fresh data → force-sync ALL widget
        # states from stored before rendering.  During normal user interaction
        # the stored value only changes AFTER date_changed/sort_changed fires
        # (next rerun), so the widget interaction render is never interrupted.
        # ─────────────────────────────────────────────────────────────────────
        _sw_key = "start_dt_shared"
        _ew_key = "end_dt_shared"
        _so_key = "sort_order_select_shared"

        # "last confirmed" tracking keys (set at end of this block)
        _LC_DATE  = "_mdlc_date_range"
        _LC_SORT  = "_mdlc_sort"
        _LC_CONV  = "_mdlc_conv"
        _LC_FROM  = "_mdlc_from_curr"
        _LC_TO    = "_mdlc_to_curr"
        _LC_UNITS = "_mdlc_units"

        # Detect if authoritative session-state values changed since last render
        _date_sync  = (st.session_state.get(date_range_key)          != st.session_state.get(_LC_DATE)
                       or _sw_key not in st.session_state
                       or st.session_state[_sw_key] not in date_options)
        _sort_sync  = (st.session_state.get(sort_order_key)          != st.session_state.get(_LC_SORT)
                       or _so_key not in st.session_state)
        _conv_sync  = (st.session_state.get("conversion_mode")       != st.session_state.get(_LC_CONV)
                       or "conversion_mode_select" not in st.session_state)
        _from_sync  = (st.session_state.get("from_currency")         != st.session_state.get(_LC_FROM)
                       or "currency_from_unified" not in st.session_state)
        _to_sync    = (st.session_state.get("target_currency")       != st.session_state.get(_LC_TO)
                       or "currency_to_unified" not in st.session_state)
        _units_sync = (st.session_state.get("units")                 != st.session_state.get(_LC_UNITS)
                       or "units_select" not in st.session_state)

        # Force-sync stale widget states from authoritative values
        if _date_sync:
            st.session_state[_sw_key] = date_options[start_idx] if date_options else None
            st.session_state[_ew_key] = date_options[end_idx]   if date_options else None
        if _sort_sync:
            st.session_state[_so_key] = st.session_state[sort_order_key]
        if _conv_sync:
            st.session_state["conversion_mode_select"] = st.session_state.conversion_mode
        if _from_sync:
            st.session_state["currency_from_unified"]  = st.session_state.from_currency
        if _to_sync:
            st.session_state["currency_to_unified"]    = st.session_state.target_currency
        if _units_sync:
            st.session_state["units_select"]           = st.session_state.units

        # Commit "last confirmed" snapshot — used on next render to detect changes
        st.session_state[_LC_DATE]  = st.session_state.get(date_range_key)
        st.session_state[_LC_SORT]  = st.session_state.get(sort_order_key)
        st.session_state[_LC_CONV]  = st.session_state.get("conversion_mode")
        st.session_state[_LC_FROM]  = st.session_state.get("from_currency")
        st.session_state[_LC_TO]    = st.session_state.get("target_currency")
        st.session_state[_LC_UNITS] = st.session_state.get("units")

        space,space, f1, f2, f3, f4, f5, f_arrow, f6, f7 = st.columns([0.4, 0.3, 1.5, 1.5, 1.1, 1.6, 1.0, 0.1, 1.0, 1.5], gap="small", vertical_alignment="center", width="stretch" )


        with f1:
            st.html('<div class="filter-label">Start Date</div>')
            new_start_label = st.selectbox(
                "Start",
                options=date_options,
                label_visibility="collapsed",
                key=_sw_key
            )
            new_start_date = date_values.get(new_start_label, start_date)

        with f2:
            st.html('<div class="filter-label">End Date</div>')
            new_end_label = st.selectbox(
                "End",
                options=date_options,
                label_visibility="collapsed",
                key=_ew_key
            )
            new_end_date = date_values.get(new_end_label, end_date)

        with f3:
            st.html('<div class="filter-label">Sort</div>')
            new_sort_order = st.selectbox(
                "Sort",
                options=["Earliest", "Latest"],
                label_visibility="collapsed",
                key=_so_key
            )

        with f4:
            st.html('<div class="filter-label">Conversion</div>')
            conversion_modes = ["Today's Spot Rate", "Historical"]
            new_conversion_mode = st.selectbox(
                "Conversion",
                options=conversion_modes,
                label_visibility="collapsed",
                key="conversion_mode_select"
            )
            if new_conversion_mode != st.session_state.conversion_mode:
                st.session_state.conversion_mode = new_conversion_mode
                st.query_params["conv_mode"] = new_conversion_mode
                save_market_data_state()
                st.rerun()

        # Safety: ensure from_currency is always set
        if 'from_currency' not in st.session_state:
            st.session_state.from_currency = reported_currency

        _fc = st.session_state.get("from_currency", _early_reported_currency)
        _tc = st.session_state.get("target_currency", _early_reported_currency)

        # "Currency" dropdown: currencies that have a direct mapping TO the current to_currency
        # e.g. if to_currency=EUR, show currencies where X→EUR exists in forex table
        _from_options = ForexRepository.get_from_currencies(_tc)

        # "To Currency" dropdown: currencies that from_currency can directly convert TO
        # e.g. if from_currency=EUR, show currencies where EUR→X exists in forex table
        _to_options = ForexRepository.get_to_currencies(_fc)

        with f5:
            st.html('<div class="filter-label">Currency</div>')
            new_from = st.selectbox(
                "From Currency",
                options=_from_options,
                label_visibility="collapsed",
                key="currency_from_unified"
            )
            if new_from != st.session_state.from_currency:
                st.session_state.from_currency = new_from
                save_market_data_state()
                st.rerun()

        with f_arrow:
            st.html('<div class="filter-label">&nbsp;</div>')
            st.html('<div style="display:flex;align-items:center;justify-content:center;height:36px;font-size:16px;color:#4F4F4F;padding-top:4px;">➜</div>')

        with f6:
            st.html('<div class="filter-label">To Currency</div>')
            target = st.selectbox(
                "To Currency",
                options=_to_options,
                label_visibility="collapsed",
                key="currency_to_unified"
            )
            if target != st.session_state.target_currency:
                st.session_state.target_currency = target
                st.query_params["to_curr"] = target
                save_market_data_state()
                st.rerun()
        with f7:
            st.html('<div class="filter-label">Units</div>')
            unit_options = ["Millions (mm)", "Billions (bn)", "Thousands (k)"]
            new_units = st.selectbox(
                "Units",
                options=unit_options,
                label_visibility="collapsed",
                key="units_select"
            )
            if new_units != st.session_state.units:
                st.session_state.units = new_units
                st.query_params["units_sel"] = new_units
                save_market_data_state()
                st.rerun()
        # Update states if changed
        date_changed = new_start_date != start_date or new_end_date != end_date
        sort_changed = new_sort_order != st.session_state[sort_order_key]

        if date_changed or sort_changed:
            if new_start_date > new_end_date:
                st.error("Start date must be before end date")
            else:
                st.session_state[date_range_key] = (new_start_date.isoformat(), new_end_date.isoformat())
                st.session_state[sort_order_key] = new_sort_order
                # Persist to URL query params — synchronous, survives page refresh
                st.query_params["start_dt"] = new_start_date.isoformat()
                st.query_params["end_dt"]   = new_end_date.isoformat()
                st.query_params["sort_ord"] = new_sort_order
                save_market_data_state()
                st.rerun()

    # ==================== TABLE WITH CURRENCY CONVERSION ====================
    # Apply sort order to data
    sort_ascending = st.session_state[sort_order_key] == "Earliest"

    # Compute units scale factor (data is stored in millions)
    _units_map = {"Millions (mm)": 1.0, "Billions (bn)": 0.001, "Thousands (k)": 1000.0}
    units_scale = _units_map.get(st.session_state.get("units", "Millions (mm)"), 1.0)
    _units_label_map = {"Millions (mm)": "Millions", "Billions (bn)": "Billions", "Thousands (k)": "Thousands"}
    units_label = _units_label_map.get(st.session_state.get("units", "Millions (mm)"), "Millions")

    if selected_tab == "company_profile":
        render_company_profile_content(company)
        # Excel download button — bottom-right, icon style
        try:
            from utils.excel_export import export_company_profile_excel
            _profile_rows = get_profile_rows_for_excel(company)
            _profile_xl = export_company_profile_excel(
                company_name=company.name or "",
                ticker=company.ticker or "",
                rows=_profile_rows,
                description=company.company_description or "",
            )
            _, _dl_col = st.columns([11, 0.5])
            with _dl_col:
                st.download_button(
                    label="⬇",
                    data=_profile_xl,
                    file_name=f"{company.ticker}_Company_Profile.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    help="Download Company Profile as Excel",
                    key="dl_excel_company_profile",
                )
        except Exception:
            pass
    elif not start_date or not end_date:
        pass  # No data available message already shown above
    elif selected_tab == "balance_sheet":
        render_balance_sheet(selected_ticker, start_date, end_date, conversion_rate, reported_currency, sort_ascending, historical_rate_map, units_scale, units_label)
        # Excel download
        try:
            from utils.excel_export import export_financial_excel
            _bs_data = BalanceSheetRepository.get_balance_sheet_data(selected_ticker, start_date, end_date)
            if not sort_ascending:
                _bs_data.periods = list(reversed(_bs_data.periods))
                for _it in _bs_data.line_items:
                    _it.values = list(reversed(_it.values))
            if _bs_data.periods and _bs_data.line_items:
                _bs_rows = []
                for _it in _bs_data.line_items:
                    _rates = [historical_rate_map.get(_bs_data.periods[ci].date, conversion_rate) if historical_rate_map else conversion_rate for ci in range(len(_bs_data.periods))]
                    _bs_rows.append({"label": _it.label.strip(), "values": [v * _rates[ci] * units_scale if v is not None else None for ci, v in enumerate(_it.values)], "is_bold": is_balance_sheet_bold_row(_it.label), "indent": get_balance_sheet_indent_level(_it.label), "is_percent": False, "is_text": False, "has_separator": has_balance_sheet_grey_separator(_it.label), "is_estimated": [False] * len(_it.values)})
                _bs_xl = export_financial_excel("Balance Sheet", company.name or "", selected_ticker, [p.label for p in _bs_data.periods], _bs_rows, units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
                _, _dl_col = st.columns([11, 0.5])
                with _dl_col:
                    st.download_button(label="⬇", data=_bs_xl, file_name=f"{selected_ticker}_Balance_Sheet.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", help="Download Balance Sheet as Excel", key="dl_excel_balance_sheet")
        except Exception:
            pass
    elif selected_tab == "cash_flow":
        render_cash_flow(selected_ticker, start_date, end_date, conversion_rate, reported_currency, sort_ascending, historical_rate_map, units_scale, units_label)
        # Excel download
        try:
            from utils.excel_export import export_financial_excel
            from data.repository import CashFlowRepository
            _cf_data = CashFlowRepository.get_cash_flow_data(selected_ticker, start_date, end_date)
            if not sort_ascending:
                _cf_data.periods = list(reversed(_cf_data.periods))
                for _it in _cf_data.line_items:
                    _it.values = list(reversed(_it.values))
            if _cf_data.periods and _cf_data.line_items:
                _cf_rows = []
                for _it in _cf_data.line_items:
                    _rates = [historical_rate_map.get(_cf_data.periods[ci].date, conversion_rate) if historical_rate_map else conversion_rate for ci in range(len(_cf_data.periods))]
                    _cf_rows.append({"label": _it.label.strip(), "values": [v * _rates[ci] * units_scale if v is not None else None for ci, v in enumerate(_it.values)], "is_bold": is_cash_flow_bold_row(_it.label), "indent": get_cash_flow_indent_level(_it.label), "is_percent": False, "is_text": False, "has_separator": has_cash_flow_grey_separator(_it.label), "is_estimated": [False] * len(_it.values)})
                _cf_xl = export_financial_excel("Cash Flow", company.name or "", selected_ticker, [p.label for p in _cf_data.periods], _cf_rows, units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
                _, _dl_col = st.columns([11, 0.5])
                with _dl_col:
                    st.download_button(label="⬇", data=_cf_xl, file_name=f"{selected_ticker}_Cash_Flow.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", help="Download Cash Flow as Excel", key="dl_excel_cash_flow")
        except Exception:
            pass
    elif selected_tab == "income_statement":
        try:
            data = IncomeStatementRepository.get_income_statement_data(
                selected_ticker, start_date, end_date
            )

            # Apply sorting based on user selection
            if not sort_ascending:
                # Reverse the periods and corresponding values
                data.periods = list(reversed(data.periods))
                for item in data.line_items:
                    item.values = list(reversed(item.values))

            if data.periods and data.line_items:
                # Build table HTML
                html = '<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>'

                # Header row - with grey separator
                html += f'<tr class="row-grey-separator"><th>For Fiscal Period Ending<span class="header-subtext">{units_label} of {st.session_state.get("target_currency", "USD")}, except per share items.</span></th>'
                for period in data.periods:
                    lines = period.label.split('\n')
                    if len(lines) >= 2:
                        period_text = lines[0]
                        date_text = lines[1]
                    else:
                        period_text = ""
                        date_text = period.label

                    html += f'<th class="data-col"><span class="period-label">{period_text}</span><span class="period-date">{date_text}</span></th>'
                html += '</tr></thead><tbody>'

                # Data rows with currency conversion applied
                prev_item_label = None
                for i, item in enumerate(data.line_items):
                    indent = get_indent_level(item.label)
                    is_bold = is_bold_row(item.label)
                    needs_grey_sep = has_grey_separator(item.label)

                    # Check if NEXT row needs underline, if so add it to THIS row
                    next_item = data.line_items[i + 1] if i + 1 < len(data.line_items) else None
                    needs_underline = has_underline(next_item.label) if next_item else False

                    # Build row classes
                    row_classes = []
                    if is_bold:
                        row_classes.append("row-bold")
                    if needs_underline:
                        row_classes.append("row-underline-black")
                    if needs_grey_sep:
                        row_classes.append("row-grey-separator")

                    row_class_str = ' '.join(row_classes) if row_classes else ''

                    html += f'<tr class="{row_class_str}">'

                    # First column - label with proper indentation
                    html += f'<td class="indent-{indent}">{item.label}</td>'

                    # Data columns with converted values
                    for col_idx, val in enumerate(item.values):
                        # Use per-column rate from historical_rate_map if available
                        if historical_rate_map and col_idx < len(data.periods):
                            period_date = data.periods[col_idx].date
                            col_rate = historical_rate_map.get(period_date, conversion_rate)
                        else:
                            col_rate = conversion_rate
                        formatted = format_value(val, col_rate, units_scale)
                        html += f'<td class="data-cell">{formatted}</td>'

                    html += '</tr>'

                html += '</tbody></table></div></div>'
                st.html(html)

                # Excel download
                try:
                    from utils.excel_export import export_financial_excel as _xl_fn
                    _is_rows = []
                    for _it in data.line_items:
                        _rates = [historical_rate_map.get(data.periods[ci].date, conversion_rate) if historical_rate_map else conversion_rate for ci in range(len(data.periods))]
                        _is_rows.append({"label": _it.label, "values": [v * _rates[ci] * units_scale if v is not None else None for ci, v in enumerate(_it.values)], "is_bold": is_bold_row(_it.label), "indent": get_indent_level(_it.label), "is_percent": False, "is_text": False, "has_separator": has_grey_separator(_it.label), "is_estimated": [False] * len(_it.values)})
                    _is_xl = _xl_fn("Income Statement", company.name or "", selected_ticker, [p.label for p in data.periods], _is_rows, units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
                    _, _dl_col = st.columns([11, 0.5])
                    with _dl_col:
                        st.download_button(label="⬇", data=_is_xl, file_name=f"{selected_ticker}_Income_Statement.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", help="Download Income Statement as Excel", key="dl_excel_income_statement")
                except Exception:
                    pass

            else:
                st.info("No data available")

        except Exception as e:
            st.error(f"Error: {e}")
    elif selected_tab == "key_stats":
        try:
            data = KeyStatsRepository.get_key_stats_data(selected_ticker, start_date, end_date)

            # Apply sorting based on user selection
            if not sort_ascending:
                data["periods"] = list(reversed(data["periods"]))
                for item in data["line_items"]:
                    item["values"] = list(reversed(item["values"]))

            if data["periods"] and data["line_items"]:
                # Build table HTML - SAME STRUCTURE AS INCOME STATEMENT
                # Inject CSS for A/E badges and estimated column
                est_css = """
                <style>
                .actual-badge {
                    font-size: 9px; font-weight: 700; color: #888888;
                    vertical-align: super; margin-left: 1px; letter-spacing: 0;
                }
                .estimate-badge {
                    font-size: 9px; font-weight: 700; color: #0066CC;
                    vertical-align: super; margin-left: 1px; letter-spacing: 0;
                }
                th.est-col {
                    background: rgba(0, 102, 204, 0.04) !important;
                    border-left: 2px solid rgba(0, 102, 204, 0.18) !important;
                }
                td.est-cell {
                    background: rgba(0, 102, 204, 0.03);
                    border-left: 2px solid rgba(0, 102, 204, 0.18);
                    color: #0066CC;
                    font-style: italic;
                }
                td.est-cell-dash {
                    background: rgba(0, 102, 204, 0.03);
                    border-left: 2px solid rgba(0, 102, 204, 0.18);
                    color: #AAAAAA;
                    font-style: italic;
                }
                </style>
                """
                html = est_css + '<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>'

                # Header row - with grey separator
                html += f'<tr class="row-grey-separator"><th>For Fiscal Period Ending<span class="header-subtext">{units_label} of {st.session_state.get("target_currency", "USD")}, except per share items.</span></th>'
                for period in data["periods"]:
                    lines = period.label.split('\n')
                    if len(lines) >= 2:
                        period_text = lines[0]
                        date_text = lines[1]
                    else:
                        period_text = ""
                        date_text = period.label

                    if period.is_estimated:
                        html += (
                            f'<th class="data-col est-col">'
                            f'<span class="period-label">{period_text}</span>'
                            f'<span class="period-date">{date_text}'
                            f'<sup class="estimate-badge">E</sup></span></th>'
                        )
                    else:
                        html += (
                            f'<th class="data-col">'
                            f'<span class="period-label">{period_text}</span>'
                            f'<span class="period-date">{date_text}'
                            f'<sup class="actual-badge">A</sup></span></th>'
                        )
                html += '</tr></thead><tbody>'

                # Data rows with currency conversion applied
                for i, item in enumerate(data["line_items"]):
                    label = item["label"]
                    values = item["values"]
                    is_bold = item.get("is_bold", False)
                    indent = item.get("indent", 0)
                    is_percent = item.get("is_percent", False)
                    is_text = item.get("is_text", False)
                    has_grey_sep = item.get("has_grey_sep", False)

                    # Skip empty label rows but add separator
                    if not label:
                        html += f'<tr><td colspan="{len(data["periods"]) + 1}">&nbsp;</td></tr>'
                        continue

                    # Build row classes - same as income statement
                    row_classes = []
                    if is_bold:
                        row_classes.append("row-bold")

                    # Add grey separator for specific rows
                    if has_grey_sep:
                        row_classes.append("row-grey-separator")

                    row_class_str = ' '.join(row_classes) if row_classes else ''

                    html += f'<tr class="{row_class_str}">'

                    # First column - label with proper indentation
                    html += f'<td class="indent-{min(indent, 2)}">{label}</td>'

                    # Data columns with converted values
                    for col_idx, val in enumerate(values):
                        col_is_estimated = (
                            col_idx < len(data["periods"])
                            and data["periods"][col_idx].is_estimated
                        )

                        # Determine per-column rate (estimated col uses current rate, not historical)
                        if not col_is_estimated and historical_rate_map and col_idx < len(data["periods"]):
                            period_date = data["periods"][col_idx].date
                            col_rate = historical_rate_map.get(period_date, conversion_rate)
                        else:
                            col_rate = conversion_rate

                        if is_text:
                            formatted = str(val) if val is not None else "-"
                        elif is_percent:
                            formatted = f"{val:.2f}%" if val is not None else "-"
                        elif label == "Diluted EPS Excl. Extra Items":
                            formatted = f"{val:.2f}" if val is not None else "-"
                        else:
                            formatted = format_value(val, col_rate, units_scale)

                        if col_is_estimated:
                            cell_class = "data-cell est-cell" if val is not None else "data-cell est-cell-dash"
                        else:
                            cell_class = "data-cell"
                        html += f'<td class="{cell_class}">{formatted}</td>'

                    html += '</tr>'

                html += '</tbody></table></div></div>'
                st.html(html)

                # Excel download
                try:
                    from utils.excel_export import export_financial_excel as _xl_fn
                    _ks_rows = []
                    for _item in data["line_items"]:
                        _lbl = _item["label"]
                        if not _lbl:
                            _ks_rows.append({"label": "", "values": [], "is_bold": False, "indent": 0, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": []})
                            continue
                        _is_pct  = _item.get("is_percent", False)
                        _is_txt  = _item.get("is_text", False)
                        _is_eps  = (_lbl == "Diluted EPS Excl. Extra Items")
                        _vals, _est = [], []
                        for _ci, _v in enumerate(_item["values"]):
                            _col_est = _ci < len(data["periods"]) and data["periods"][_ci].is_estimated
                            _est.append(_col_est)
                            if _v is None:
                                _vals.append(None)
                            elif _is_txt:
                                _vals.append(_v)
                            elif _is_pct:
                                _vals.append(_v)  # raw %, will be divided by 100 in export
                            elif _is_eps:
                                _vals.append(_v)
                            else:
                                _cr = conversion_rate if _col_est else (historical_rate_map.get(data["periods"][_ci].date, conversion_rate) if historical_rate_map and _ci < len(data["periods"]) else conversion_rate)
                                _vals.append(_v * _cr * units_scale)
                        _ks_rows.append({"label": _lbl, "values": _vals, "is_bold": _item.get("is_bold", False), "indent": _item.get("indent", 0), "is_percent": _is_pct, "is_text": _is_txt, "has_separator": _item.get("has_grey_sep", False), "is_estimated": _est})
                    _ks_xl = _xl_fn("Key Stats", company.name or "", selected_ticker, [p.label for p in data["periods"]], _ks_rows, units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
                    _, _dl_col = st.columns([11, 0.5])
                    with _dl_col:
                        st.download_button(label="⬇", data=_ks_xl, file_name=f"{selected_ticker}_Key_Stats.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", help="Download Key Stats as Excel", key="dl_excel_key_stats")
                except Exception:
                    pass

            else:
                st.info("No key stats data available for the selected date range")

            # ── Stock Quote and Chart table (always shown below key stats) ──
            render_stock_quote(selected_ticker, currency=st.session_state.get("target_currency", "USD"))

        except Exception as e:
            st.error(f"Error loading key stats: {e}")
    # company_profile tab is handled above in the table section

    _page_elapsed = (_time.perf_counter() - _page_start) * 1000


def main():
    """Market data page entry point."""
    render_styles()

    set_page_layout(
        header_full_width=True,
        footer_full_width=True,
        body_padding="0 20px",
        max_content_width="1350px",
        remove_top_padding=True,
        footer_at_bottom=True
    )

    # Get ticker from query params or use default for header
    import streamlit as st
    ticker_for_header = st.query_params.get("ticker", "M")
    render_header(full_width=True, current_page="market_data", ticker=ticker_for_header)
    render_page()
    render_coresight_footer(full_width=True, stick_to_bottom=True)


main()
