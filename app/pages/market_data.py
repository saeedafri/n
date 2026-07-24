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
from utils.server_logger import (
    log_structured_error,
    log_error,
    log_info,
    error_boundary,
    get_rerun_id,
    new_rerun_id,
    log_timing,
)

from components.styles import hide_sidebar, render_styles, set_page_layout, COLORS
from components.navigation import render_header, render_coresight_footer
from components.toolbar import render_tabs
from components.companyProfile import render_company_profile_content, get_company_css, get_profile_rows_for_excel, render_info_table, render_business_description, render_reference_table
from components.navigation import render_company_header
from core.auth_manager import require_auth
# require_auth(page="market_data")

hide_sidebar()

from data.repository import CompanyOverviewRepository, CompanyRepository, IncomeStatementRepository, BalanceSheetRepository, KeyStatsRepository, RatiosRepository, ForexRepository, AnalystEstimatesRepository, ModelForecastsRepository
from data.models import IncomeStatementData, Company, BalanceSheetData
from utils.local_storage import (
    get_marketdata_company, set_marketdata_company,
    get_marketdata_tab, set_marketdata_tab,
    get_marketdata_date_range, set_marketdata_date_range
)
from utils.local_storage_manager import sync_market_data_state, save_market_data_state, get_persistent_state, set_persistent_state
from utils.ticker_utils import validate_and_get_ticker, DEFAULT_FALLBACK_TICKER


from utils.constants import (
    CURRENCY_NAMES,
    CURRENCY_SYMBOLS,
    get_currency_symbol,
    format_currency_full as format_currency_display,
    QUARTERLY_FORECASTING_ENABLED,
)


def _trigger_excel_download(excel_bytes: bytes, filename: str) -> None:
    """INVISIBLE client-side download trigger — renders NO button and no layout space.

    Downloads `excel_bytes` via the Blob API, which bypasses Streamlit's /media/
    endpoint (that path is not forwarded on the proxied deployment). The visible
    control is the st.button in _lazy_excel_download; this component must never draw
    one of its own — a second, differently-styled "Excel" button appearing under the
    real one after a click was exactly the 17-Jul regression. height=0 + no body
    content keeps it invisible, and nothing here touches the network (no font).
    """
    try:
        import base64
        import time as _t
        from streamlit.components.v1 import html as _sthtml

        b64 = base64.b64encode(excel_bytes).decode("ascii")
        safe_name = filename.replace("'", "\\'").replace('"', '\\"')
        # Unique nonce per invocation: openpyxl serialises timestamps at 1-second
        # resolution, so two builds in the same second are byte-identical → identical
        # iframe srcdoc → Streamlit keeps the existing DOM node and the download script
        # never re-runs (a deliberate quick re-download would silently do nothing). The
        # nonce forces a fresh srcdoc so every click remounts and re-fires.
        _nonce = _t.time_ns()
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;height:0;overflow:hidden;">
<!-- dl-nonce {_nonce} -->
<script>
(function(){{
  try{{
    var bin=atob("{b64}"),n=bin.length,u8=new Uint8Array(n);
    for(var i=0;i<n;i++) u8[i]=bin.charCodeAt(i);
    var blob=new Blob([u8],{{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}});
    var url=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=url; a.download="{safe_name}";
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(function(){{URL.revokeObjectURL(url);}},1000);
  }}catch(e){{console.error("Excel download failed:",e);}}
}})();
</script>
</body></html>"""
        _sthtml(html, height=0, width=0, scrolling=False)
    except Exception as e:
        log_structured_error(e, page="market_data", component="_trigger_excel_download", operation="trigger_excel_download")


def _lazy_excel_download(widget_key: str, filename: str, build_fn, label: str = "Excel") -> None:
    """ONE right-aligned Excel button that builds the workbook only when clicked.

    Why a fragment: the eager flow rebuilt a full openpyxl workbook on EVERY tab
    render (measured 60-216ms avg, up to 1.6s — STG log 16-Jul) and re-fetched the
    tab's data to do it. Gating the build on a plain st.button was worse: the click
    triggered a FULL-page rerun, so the whole market_data page re-rendered and the
    app visibly froze (STG log 17-Jul: `MD_RUN_START tab=ratios gap=-1` twice
    back-to-back). @st.fragment scopes the click's rerun to THIS button alone — the
    page is untouched, nothing else re-renders, and only build_fn() runs. The file
    then downloads through an invisible trigger, so the user sees one button and
    gets one file. Bytes are built fresh per click (never memoised — the workbook
    depends on period/currency/sort/units; a stale cache would serve the wrong file).
    """
    @st.fragment
    def _excel_button():
        if st.button(label, key=f"_xlbtn_{widget_key}", help="Download this table as an Excel file"):
            try:
                xl = build_fn()
            except Exception as e:
                log_structured_error(e, page="market_data", component="_lazy_excel_download",
                                     operation=f"build_excel:{widget_key}")
                st.error("Could not build the Excel file. Please try again.")
                return
            if xl:
                _trigger_excel_download(xl, filename)
            else:
                # build_fn returns None when the on-click re-fetch yields no rows.
                # Never leave the click silent — that reads as a dead button.
                st.toast("No data available to export for this view.", icon="⚠️")

    _excel_button()


def _income_excel_rows(data, historical_rate_map, conversion_rate, units_scale):
    """Excel rows for the Income Statement tab (relocated from the inline render so
    the workbook is built only when the user clicks Excel — see _lazy_excel_download)."""
    rows = []
    for _it in data.line_items:
        _rates = [historical_rate_map.get(data.periods[ci].date, conversion_rate) if historical_rate_map else conversion_rate for ci in range(len(data.periods))]
        rows.append({"label": _it.label, "values": [v * _rates[ci] * units_scale if v is not None else None for ci, v in enumerate(_it.values)], "is_bold": is_bold_row(_it.label), "indent": get_indent_level(_it.label), "is_percent": False, "is_text": False, "has_separator": has_grey_separator(_it.label), "is_estimated": [False] * len(_it.values)})
    return rows


def _key_stats_excel_rows(data, historical_rate_map, conversion_rate, units_scale):
    """Excel rows for the Key Stats tab (relocated for lazy Excel build)."""
    rows = []
    for _item in data["line_items"]:
        _lbl = _item["label"]
        if not _lbl:
            rows.append({"label": "", "values": [], "is_bold": False, "indent": 0, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": []})
            continue
        _is_pct = _item.get("is_percent", False)
        _is_txt = _item.get("is_text", False)
        _is_eps = (_lbl == "Diluted EPS Excl. Extra Items")
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
        rows.append({"label": _lbl, "values": _vals, "is_bold": _item.get("is_bold", False), "indent": _item.get("indent", 0), "is_percent": _is_pct, "is_text": _is_txt, "has_separator": _item.get("has_grey_sep", False), "is_estimated": _est})
    return rows


def _ratios_excel_rows(data):
    """Excel rows for the Ratios tab (relocated for lazy Excel build)."""
    rows = []
    for _it in data["line_items"]:
        rows.append({
            "label": _it.get("label", ""),
            "values": _it.get("values", []),
            "is_bold": _it.get("is_bold", False) or _it.get("is_section_header", False),
            "indent": min(_it.get("indent", 0), 2),
            "is_percent": _it.get("is_percent", False),
            "is_text": _it.get("is_section_header", False),
            "has_separator": _it.get("is_section_header", False),
            "is_estimated": [False] * len(_it.get("values", [])),
        })
    return rows


def format_value(value: Optional[float], conversion_rate: float = 1.0, units_scale: float = 1.0) -> str:
    """Format value with comma separator, currency conversion, and units scaling.

    units_scale: 1.0 = Millions (default), 0.001 = Billions, 1000.0 = Thousands
    """
    try:
        if value is None:
            return "-"
        converted = value * conversion_rate * units_scale
        return f"{converted:,.2f}"
    except Exception as e:
        log_structured_error(e, page="market_data", component="format_value", operation="format_numeric_value")
        return "-"


def is_bold_row(label: str) -> bool:
    """Check if row should be bold (subtotal rows) per Figma."""
    try:
        bold_labels = {"Total Revenue", "Gross Profit", "Other Operating Exp., Total",
                       "Operating Income", "Net Interest Exp."}
        return label in bold_labels
    except Exception as e:
        log_structured_error(e, page="market_data", component="is_bold_row", operation="check_bold_row")
        return False


def has_underline(label: str) -> bool:
    """Check if row should have 2px dark grey underline per Figma."""
    try:
        underline_labels = {"Total Revenue", "Gross Profit", "Other Operating Exp., Total",
                            "Operating Income", "Net Interest Exp."}
        return label in underline_labels
    except Exception as e:
        log_structured_error(e, page="market_data", component="has_underline", operation="check_underline")
        return False


def has_grey_separator(label: str) -> bool:
    """Check if row should have 4px grey separator line below it per Figma."""
    try:
        separator_labels = {"Total Revenue", "Gross Profit", "Operating Income"}
        return label in separator_labels
    except Exception as e:
        log_structured_error(e, page="market_data", component="has_grey_separator", operation="check_grey_separator")
        return False


def get_indent_level(label: str) -> int:
    """Get indentation level based on row type - 0=normal (12px), 1=indented (24px)."""
    try:
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
    except Exception as e:
        log_structured_error(e, page="market_data", component="get_indent_level", operation="get_indent_level")
        return 0


@st.cache_data(ttl=21600, show_spinner=False)
def _get_company_reported_currency(ticker: str) -> str:
    """Get the company's reported currency (not trading currency).

    SEC companies: reads `reported_currency` from income statement rows.
    YF companies: reads `financialCurrency` or `currency` from coreiq_yf_company_overview.
    Falls back to "USD" if nothing found.
    """
    try:
        import time as _time
        _t0 = _time.perf_counter()
        result = IncomeStatementRepository.get_reported_currency(ticker, date.today())
        _elapsed = (_time.perf_counter() - _t0) * 1000
        return result
    except Exception as e:
        log_structured_error(e, page="market_data", component="_get_company_reported_currency", operation="fetch_reported_currency")
        return "USD"


def get_conversion_rate(from_currency: str, to_currency: str) -> float:
    """Get conversion rate between currencies from the forex table."""
    try:
        if from_currency == to_currency:
            return 1.0

        return ForexRepository.get_conversion_rate(from_currency, to_currency)
    except Exception as e:
        log_structured_error(e, page="market_data", component="get_conversion_rate", operation="fetch_conversion_rate")
        return 1.0


def is_balance_sheet_bold_row(label: str) -> bool:
    """Check if balance sheet row should be bold (subtotal/total rows)."""
    try:
        bold_labels = {
            "Total Assets", "Total Liabilities", "Total Shareholder Equity",
            "Total Current Assets", "Total Non-Current Assets",
            "Total Current Liabilities", "Total Non-Current Liabilities"
        }
        return label.strip() in bold_labels
    except Exception as e:
        log_structured_error(e, page="market_data", component="is_balance_sheet_bold_row", operation="check_bold_row")
        return False


def has_balance_sheet_grey_separator(label: str) -> bool:
    """Check if row should have grey separator after it (major totals)."""
    try:
        separator_after = {
            "Total Assets", "Total Liabilities", "Total Shareholder Equity"
        }
        return label.strip() in separator_after
    except Exception as e:
        log_structured_error(e, page="market_data", component="has_balance_sheet_grey_separator", operation="check_grey_separator")
        return False


@st.cache_data(ttl=21600, show_spinner=False)
def get_available_period_types(ticker: str, tab: str) -> list:
    """Get available period types (Annual/Quarterly) for a company based on data availability.

    Returns a list of period types that have data for this company.
    """
    from data.source_router import get_company_source
    from core.database import db_manager

    source = get_company_source(ticker)
    available_types = []

    # Ratings are always annual
    if tab == "ratings":
        return ["Annual"]

    # Forecasting: Annual always; Quarterly only when quarterly forecasts exist.
    # Quarterly forecasting paused (see constants.QUARTERLY_FORECASTING_ENABLED):
    # offer Annual only, which clamps the tab's effective period type to Annual.
    if tab == "forecasting":
        if not QUARTERLY_FORECASTING_ENABLED:
            return ["Annual"]
        try:
            from data.repository import ModelForecastsRepository
            if ModelForecastsRepository.get_quarterly_date_range(ticker)[0] is not None:
                return ["Annual", "Quarterly"]
        except Exception:
            pass
        return ["Annual"]

    # Estimates always have both (AV has both horizons; YF has quarterly and annual labels)
    if tab == "estimates":
        return ["Annual", "Quarterly"]

    # Segment data: show Quarterly option only if DB has 10-Q segment rows
    if tab == "segment_data":
        try:
            from data.repository import SegmentDataRepository
            if SegmentDataRepository.has_quarterly_segment_data(ticker):
                return ["Annual", "Quarterly"]
        except Exception:
            pass
        return ["Annual"]

    try:
        if source == 'YFinance':
            # Check YF tables
            if tab == "balance_sheet":
                table = "coreiq_yf_financials_balance_sheet"
            elif tab == "cash_flow":
                table = "coreiq_yf_financials_cash_flow"
            else:  # income_statement, key_stats
                table = "coreiq_yf_financials_income_statement"

            # Composite tickers (e.g. ADS.DE) are stored via yf_symbol; plain tickers use ticker column
            if '.' in ticker:
                query = f"""
                    SELECT DISTINCT frequency
                    FROM {table}
                    WHERE yf_symbol = :ticker
                      AND frequency IN ('annual', 'quarterly')
                """
            else:
                query = f"""
                    SELECT DISTINCT frequency
                    FROM {table}
                    WHERE ticker = :ticker
                      AND frequency IN ('annual', 'quarterly')
                """
        else:
            # Check SEC tables
            if tab == "balance_sheet":
                table = "coreiq_av_financials_balance_sheet"
            elif tab == "cash_flow":
                table = "coreiq_av_financials_cash_flow"
            else:  # income_statement, key_stats
                table = "coreiq_av_financials_income_statement"

            query = f"""
                SELECT DISTINCT report_type
                FROM {table}
                WHERE ticker = :ticker
                  AND report_type IN ('annual', 'quarterly')
            """

        results = db_manager.execute_query_readonly(query, {"ticker": ticker})
        db_values = [row[0] if isinstance(row, tuple) else row.get('frequency', row.get('report_type', ''))
                     for row in results]

        # Map DB values to UI labels
        if 'annual' in db_values:
            available_types.append("Annual")
        if 'quarterly' in db_values:
            available_types.append("Quarterly")

        # Default to Annual if nothing found
        if not available_types:
            available_types = ["Annual"]

    except Exception as e:
        log_structured_error(e, page="market_data", component="get_available_period_types", operation="query_period_types")
        available_types = ["Annual"]  # Safe fallback

    return available_types


def get_balance_sheet_indent_level(label: str) -> int:
    """Get indentation level for balance sheet rows.
    0 = no indent (line items like Cash, Inventory)
    1 = one indent (subtotals like Total Current Assets)
    2 = two indents (major totals like Total Assets)
    """
    try:
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
    except Exception as e:
        log_structured_error(e, page="market_data", component="get_balance_sheet_indent_level", operation="get_indent_level")
        return 0


def render_balance_sheet(ticker: str, start_date: date, end_date: date, conversion_rate: float, reported_currency: str, sort_ascending: bool = True, historical_rate_map: dict = None, units_scale: float = 1.0, units_label: str = "Millions", period_type: str = "annual"):
    """Render the balance sheet table."""
    import time as _time
    # Session-state short-circuit — reuse cached HTML when inputs unchanged
    _conv_mode = st.session_state.get('conversion_mode', 'spot')
    _bs_cache_key = f"_bs_html_{ticker}_{start_date}_{end_date}_{period_type}_{sort_ascending}_{conversion_rate:.6f}_{units_scale}_{_conv_mode}"
    _cached_html = st.session_state.get(_bs_cache_key)
    if _cached_html is not None:
        st.html(_cached_html)
        return
    _t0 = _time.perf_counter()
    try:
        data = BalanceSheetRepository.get_balance_sheet_data(ticker, start_date, end_date, period_type)
        _data_fetch_time = (_time.perf_counter() - _t0) * 1000

        # Apply sorting based on user selection
        if not sort_ascending:
            # Reverse the periods and corresponding values
            data.periods = list(reversed(data.periods))
            for item in data.line_items:
                item.values = list(reversed(item.values))

        if data.periods and data.line_items:
            _t_html = _time.perf_counter()
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
            _html_build_time = (_time.perf_counter() - _t_html) * 1000
            st.html(html)
            st.session_state[_bs_cache_key] = html

            _total_render = (_time.perf_counter() - _t0) * 1000
            from utils.server_logger import log_timing as _lt
            _lt("BS_data_fetch", _data_fetch_time, details=f"ticker={ticker}")
            _lt("BS_html_build", _html_build_time, details=f"ticker={ticker} periods={len(data.periods)} rows={len(data.line_items)}")
            _lt("BS_total_render", _total_render, details=f"ticker={ticker}")

        else:
            st.info("No balance sheet data available for the selected date range")

    except Exception as e:
        log_structured_error(e, page="market_data", component="render_balance_sheet", operation="render_balance_sheet_table")
        st.error("Something went wrong. Please try again.")


def get_cash_flow_indent_level(label: str) -> int:
    """Get indentation level for cash flow rows.
    0 = no indent (line items)
    1 = one indent (section totals like Operating Cash Flow)
    2 = two indents (major totals like Net Change in Cash)
    """
    try:
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
    except Exception as e:
        log_structured_error(e, page="market_data", component="get_cash_flow_indent_level", operation="get_indent_level")
        return 0


def is_cash_flow_bold_row(label: str) -> bool:
    """Check if row should be bold (totals and subtotals)."""
    try:
        bold_labels = {
            "Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow",
            "Net Change in Cash", "Cash at Beginning of Period", "Cash at End of Period"
        }
        return label.strip() in bold_labels
    except Exception as e:
        log_structured_error(e, page="market_data", component="is_cash_flow_bold_row", operation="check_bold_row")
        return False


def has_cash_flow_grey_separator(label: str) -> bool:
    """Check if row should have grey separator after it."""
    try:
        grey_after = {
            "Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow",
            "Cash at End of Period"
        }
        return label.strip() in grey_after
    except Exception as e:
        log_structured_error(e, page="market_data", component="has_cash_flow_grey_separator", operation="check_grey_separator")
        return False


def render_cash_flow(ticker: str, start_date: date, end_date: date, conversion_rate: float, reported_currency: str, sort_ascending: bool = True, historical_rate_map: dict = None, units_scale: float = 1.0, units_label: str = "Millions", period_type: str = "annual"):
    """Render the cash flow statement table."""
    import time as _time
    # Session-state short-circuit — reuse cached HTML when inputs unchanged
    _conv_mode = st.session_state.get('conversion_mode', 'spot')
    _cf_cache_key = f"_cf_html_{ticker}_{start_date}_{end_date}_{period_type}_{sort_ascending}_{conversion_rate:.6f}_{units_scale}_{_conv_mode}"
    _cached_html = st.session_state.get(_cf_cache_key)
    if _cached_html is not None:
        st.html(_cached_html)
        return
    _t0 = _time.perf_counter()
    try:
        from data.repository import CashFlowRepository

        data = CashFlowRepository.get_cash_flow_data(ticker, start_date, end_date, period_type)
        _data_fetch_time = (_time.perf_counter() - _t0) * 1000

        # Apply sorting based on user selection
        if not sort_ascending:
            # Reverse the periods and corresponding values
            data.periods = list(reversed(data.periods))
            for item in data.line_items:
                item.values = list(reversed(item.values))

        if data.periods and data.line_items:
            _t_html = _time.perf_counter()
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
            _html_build_time = (_time.perf_counter() - _t_html) * 1000
            st.html(html)
            st.session_state[_cf_cache_key] = html

            _total_render = (_time.perf_counter() - _t0) * 1000
            from utils.server_logger import log_timing as _lt
            _lt("CF_data_fetch", _data_fetch_time, details=f"ticker={ticker}")
            _lt("CF_html_build", _html_build_time, details=f"ticker={ticker} periods={len(data.periods)} rows={len(data.line_items)}")
            _lt("CF_total_render", _total_render, details=f"ticker={ticker}")

        else:
            st.info("No cash flow data available for the selected date range")

    except Exception as e:
        log_structured_error(e, page="market_data", component="render_cash_flow", operation="render_cash_flow_table")
        st.error("Something went wrong. Please try again.")


def render_segment_data(ticker: str, start_date: date, end_date: date, conversion_rate: float, reported_currency: str, sort_ascending: bool = True, historical_rate_map: dict = None, units_scale: float = 1.0, units_label: str = "Millions", period_type: str = "annual", fiscal_year_end: str = None):
    """Render CapIQ-style Business Segments + Geographic Segments tables.

    Each table has metric groups (Revenues, Operating Profit, Assets, D&A, CapEx).
    Under each metric: member rows + Total row.
    """
    import time as _time
    from html import escape as html_escape

    # Session-state short-circuit — reuse cached HTML when inputs unchanged
    _conv_mode = st.session_state.get('conversion_mode', 'spot')
    _seg_cache_key = f"_seg_html_{ticker}_{start_date}_{end_date}_{period_type}_{sort_ascending}_{conversion_rate:.6f}_{units_scale}_{_conv_mode}_{fiscal_year_end}"
    _cached_html = st.session_state.get(_seg_cache_key)
    if _cached_html is not None:
        st.html(_cached_html)
        return
    _t0 = _time.perf_counter()
    try:
        from data.repository import SegmentDataRepository
        from utils.server_logger import log_timing as _seg_log_timing

        _t_fetch = _time.perf_counter()
        data = SegmentDataRepository.get_segment_data(ticker, start_date, end_date, period_type)
        _fetch_ms = (_time.perf_counter() - _t_fetch) * 1000
        _seg_log_timing("SEGMENT_DB_FETCH", _fetch_ms, details=f"ticker={ticker} source={data.get('source','?')} years={len(data.get('years',[]))} biz_metrics={len(data.get('business_segments',{}))} geo_metrics={len(data.get('geo_segments',{}))}")

        years = data["years"]
        period_dates = data.get("period_dates", {})
        # Headers use FYE-normalised display dates (annual); falls back to raw
        # period_dates for the quarterly path, which already has correct dates.
        period_display_dates = data.get("period_display_dates") or period_dates
        biz = data.get("business_segments", {})
        geo = data.get("geo_segments", {})
        source = data.get("source", "none")
        revenue_totals = data.get("revenue_totals", {})
        metric_totals = data.get("metric_totals", {})

        if not sort_ascending:
            years = list(reversed(years))

        if not years or (not biz and not geo):
            st.info("No extracted data available. Check official filings.")
            return

        target_ccy = st.session_state.get("target_currency", "USD")
        _all_parts = []

        def _build_table(title: str, seg_dict: dict, rev_totals: dict = None, m_totals: dict = None) -> str:
            """Build one HTML table for a segment section (Business or Geographic)."""
            if not seg_dict:
                return ""

            from utils.constants import SEGMENT_METRIC_GROUPS

            parts = []
            # Section title
            parts.append(f'<h4 style="margin:18px 0 6px 0;font-size:15px;font-weight:700;color:#1E293B;">{html_escape(title)}</h4>')
            parts.append('<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>')

            # Header row
            _is_quarterly = period_type.lower() == "quarterly"
            parts.append(f'<tr class="row-grey-separator"><th>For Fiscal Period Ending<span class="header-subtext">{html_escape(units_label)} of {html_escape(target_ccy)}, except per share items.</span></th>')
            for yr in years:
                pd = period_display_dates.get(yr)
                if pd:
                    if _is_quarterly:
                        from data.models import FiscalPeriod as _FP
                        _fp = _FP.from_date(pd, "quarterly", fiscal_year_end)
                        _fp_parts = _fp.label.split("\n", 1)
                        _p_label = _fp_parts[0]
                        _p_date = _fp_parts[1] if len(_fp_parts) > 1 else pd.strftime("%b-%d-%Y")
                    else:
                        _p_label = "12 Months"
                        _p_date = pd.strftime("%b-%d-%Y")
                else:
                    _p_label = "10-Q" if _is_quarterly else "10-K"
                    _p_date = str(yr)
                parts.append(f'<th class="data-col"><span class="period-label">{_p_label}</span><span class="period-date">{_p_date}</span></th>')
            parts.append('</tr></thead><tbody>')

            # Currency / Filing Date / Exchange Rate rows
            parts.append(f'<tr><td style="font-weight:500;color:#6B7280;">Currency</td>')
            for _ in years:
                parts.append(f'<td class="data-cell" style="color:#6B7280;">{html_escape(target_ccy)}</td>')
            parts.append('</tr>')

            # Iterate metric groups in defined order
            for metric_name, cfg in SEGMENT_METRIC_GROUPS.items():
                display_label = cfg["display"]
                # Check if this metric exists in seg_dict
                if metric_name not in seg_dict:
                    continue
                members_data = seg_dict[metric_name]
                if not members_data:
                    continue

                # Metric heading row (bold)
                parts.append(f'<tr class="row-bold row-grey-separator"><td style="font-weight:700;padding-left:8px;">{html_escape(display_label)}</td>')
                for _ in years:
                    parts.append('<td class="data-cell"></td>')
                parts.append('</tr>')

                # Sort members alphabetically
                sorted_members = sorted(members_data.keys(), key=lambda m: m.lower())

                for member in sorted_members:
                    yr_vals = members_data[member]
                    parts.append(f'<tr><td class="indent-1" style="padding-left:24px;">{html_escape(member)}</td>')
                    for yr in years:
                        val = yr_vals.get(yr)
                        formatted = format_value(val, conversion_rate, units_scale)
                        parts.append(f'<td class="data-cell">{formatted}</td>')
                    parts.append('</tr>')

                # Total row — use exact filed (non-dimensioned) total, never sum members
                if len(sorted_members) > 1:
                    # Priority: metric_totals (from non-dim XBRL) > rev_totals (IS) for Revenues
                    if m_totals and metric_name in m_totals:
                        total_vals = {yr: m_totals[metric_name].get(yr) for yr in years}
                    elif metric_name == "Revenues" and rev_totals:
                        total_vals = {yr: rev_totals.get(yr) for yr in years}
                    else:
                        total_vals = {yr: None for yr in years}
                    has_total = any(v is not None for v in total_vals.values())

                    if has_total:
                        parts.append(f'<tr class="row-bold" style="border-top:1px solid #D1D5DB;"><td style="font-weight:600;padding-left:24px;">Total</td>')
                        for yr in years:
                            t_val = total_vals.get(yr)
                            formatted = format_value(t_val, conversion_rate, units_scale)
                            parts.append(f'<td class="data-cell" style="font-weight:600;">{formatted}</td>')
                        parts.append('</tr>')

            parts.append('</tbody></table></div></div>')
            return ''.join(parts)

        # Build Business Segments table
        _t_biz = _time.perf_counter()
        biz_html = _build_table("Business Segments", biz, rev_totals=revenue_totals, m_totals=metric_totals)
        _biz_ms = (_time.perf_counter() - _t_biz) * 1000
        # Build Geographic Segments table
        _t_geo = _time.perf_counter()
        geo_html = _build_table("Geographic Segments", geo, rev_totals=revenue_totals, m_totals=metric_totals)
        _geo_ms = (_time.perf_counter() - _t_geo) * 1000

        html = biz_html + geo_html

        if biz_html or geo_html:
            _t_render = _time.perf_counter()
            st.html(html)
            _render_ms = (_time.perf_counter() - _t_render) * 1000
            st.session_state[_seg_cache_key] = html
            _total_ms = (_time.perf_counter() - _t0) * 1000
            _seg_log_timing("SEGMENT_RENDER", _total_ms, details=f"ticker={ticker} fetch={_fetch_ms:.0f}ms biz_html={_biz_ms:.0f}ms geo_html={_geo_ms:.0f}ms st_html={_render_ms:.0f}ms")
        else:
            st.info("No extracted data available. Check official filings.")

    except Exception as e:
        log_structured_error(e, page="market_data", component="render_segment_data", operation="render_segment_data_table")
        st.error("Something went wrong. Please try again.")


def _clear_md_tab_loader():
    """Tear down the market_data branded tab overlay before an early st.rerun().

    render_page() only clears the overlay at its END; a rerun that fires first
    (the ratings auto-heal reruns below) would orphan it in the DOM until the CSS
    failsafe fades it. Clearing it here removes the lingering spinner immediately.
    """
    _ldr = st.session_state.pop("_md_tab_loader", None)
    if _ldr is not None:
        try:
            _ldr.empty()
        except Exception:
            pass


# CSS for the collapsible geography list in the Store Count header. Emitted once
# with the cell (a lone <style> in st.markdown is harmless and idempotent).
_STORE_COUNT_GEO_CSS = (
    "<style>"
    ".sc-geo{display:inline}"
    ".sc-geo>summary{display:inline;list-style:none;cursor:pointer}"
    ".sc-geo>summary::-webkit-details-marker{display:none}"
    ".sc-geo>summary::marker{content:''}"
    ".sc-geo>summary:focus{outline:none}"
    ".sc-geo .sc-geo-rest{display:none}"
    ".sc-geo[open] .sc-geo-rest{display:inline}"
    ".sc-geo[open] .sc-geo-ell{display:none}"
    ".sc-geo .sc-geo-less{display:none}"
    ".sc-geo[open] .sc-geo-less{display:inline}"
    ".sc-geo[open] .sc-geo-more{display:none}"
    ".sc-geo-link{color:#D62E2F;font-weight:600;font-size:12px;"
    "text-decoration:underline;text-underline-offset:2px;cursor:pointer;"
    "white-space:nowrap;margin-left:5px}"
    ".sc-geo-link:hover{color:#A81F20}"
    "</style>"
)


def _store_count_header_cell(members: list) -> str:
    """HTML for the 'Store Count (...)' header cell.

    0 geographies → the "Worldwide, if Applicable" fallback. 1-2 → shown inline.
    A longer list shows the first two, then a "read more" that expands the rest
    IN PLACE. It is a pure-CSS <details> disclosure — no JavaScript — because
    Streamlit injects this markup via innerHTML, where <script> never executes.
    """
    from html import escape as _esc
    from data.repository import STORE_COUNT_WORLDWIDE_LABEL
    if not members:
        return f'Store Count ({_esc(STORE_COUNT_WORLDWIDE_LABEL)})'
    if len(members) <= 2:
        return 'Store Count (' + ', '.join(_esc(m) for m in members) + ')'
    head = ', '.join(_esc(m) for m in members[:2])
    rest = ', '.join(_esc(m) for m in members[2:])
    n_more = len(members) - 2
    return (
        _STORE_COUNT_GEO_CSS +
        '<details class="sc-geo"><summary>'
        f'Store Count ({head}'
        '<span class="sc-geo-ell"> …</span>'
        f'<span class="sc-geo-rest">, {rest}</span>)'
        f'<span class="sc-geo-link sc-geo-more">+{n_more} more</span>'
        '<span class="sc-geo-link sc-geo-less">show less</span>'
        '</summary></details>'
    )


def render_ratings_data(ticker: str, start_date: date, end_date: date, sort_ascending: bool = True):
    """Render credit ratings + store counts table with same structure as segment data.

    Two sections:
        1. Credit Ratings — rows per agency (S&P, Moody's, Fitch) with rating + outlook per year
        2. Store Counts — rows per store type with numeric count per year

    DB-first with parallel edgartools fallback for tickers missing from DB.
    """
    import time as _time
    from html import escape as html_escape

    from utils.server_logger import log_timing as _rt_log

    # No session HTML pin here (removed 16-Jul): a render that missed a
    # bounded background fetch (e.g. Stores by Country during the warm sweep)
    # used to get cached and shown incomplete for the whole session. The data
    # fetch is layer-cached (st.cache_data + disk + warm sweep) and ~1ms warm,
    # so rebuilding per rerun is both cheap and self-healing.
    _t0 = _time.perf_counter()
    try:
        from data.repository import RatingsDataRepository

        _t_fetch = _time.perf_counter()
        data = RatingsDataRepository.get_ratings_data(ticker, start_date, end_date)
        _fetch_ms = (_time.perf_counter() - _t_fetch) * 1000
        # Stash for the Excel-export block — avoids a second full fetch per render
        st.session_state[f"_ratings_data_{ticker}_{start_date}_{end_date}"] = data
        _rt_log("RATINGS_data_fetch", _fetch_ms, details=f"ticker={ticker} years={len(data.get('years',[]))} cr={data.get('has_credit_ratings')} sc={data.get('has_store_counts')} source={data.get('_source','?')}")

        years = data["years"]
        credit_ratings = data["credit_ratings"]
        store_counts = data["store_counts"]
        stores_by_country = data.get("stores_by_country", {})
        sbc_years = stores_by_country.get("years", [])
        sbc_countries = stores_by_country.get("countries", {})
        sqft_data = data.get("square_footage", {})
        sqft_metrics = sqft_data.get("metrics", [])
        sqft_years = sqft_data.get("years", [])

        if not sort_ascending:
            years = list(reversed(years))
            sbc_years = list(reversed(sbc_years))
            sqft_years = list(reversed(sqft_years))

        # Merge sqft years into main years if they are not already present
        if sqft_years and not years:
            years = sqft_years if sort_ascending else list(reversed(sorted(sqft_years)))
        elif sqft_years:
            merged = sorted(set(years) | set(sqft_years))
            years = merged if sort_ascending else list(reversed(merged))

        _rat_inc_key = f"_ratings_incomplete_{ticker}"
        # Sticky per-ticker settle flag. Once a ticker's ratings render has either
        # completed OR exhausted its bounded poll budget, we set this so later
        # incidental reruns (sort toggle, any widget) NEVER re-arm the 1.5s poll.
        # This is the root fix for the rerun storm: previously the "give up" path
        # popped the retry counter, so the very next rerun saw count=0 and re-armed
        # polling for the perpetually-pending square_footage fetch — forever.
        _settled_key = f"_ratings_settled_{ticker}"
        _settled = st.session_state.get(_settled_key, False)
        if not years or (not credit_ratings and not store_counts and not sbc_countries and not sqft_metrics):
            # Data may still be warming in the background (bounded fetch waits).
            # Poll a bounded number of times via the CALLER's st.fragment timer —
            # NEVER _time.sleep() the session thread (Streamlit runs a session's
            # runs sequentially on one thread, so sleeping here froze tab clicks for
            # the whole session; STG "Additional Data → any tab is dead"). The
            # fragment re-runs this subtree without blocking the thread.
            # square_footage is the slowest EDGAR fetch AND is meaningless on its
            # own (no store counts to attach it to). If it is the ONLY thing still
            # pending — store_totals / stores_by_country already came back empty —
            # this company simply has no store data (a non-retailer like ADBE):
            # show "no data" INSTANTLY and never poll. Poll only while a CORE fetch
            # (db rows / credit ratings / store totals / stores-by-country) that
            # could still yield data is genuinely warming.
            _pending_now = data.get("_pending_fetches") or []
            _core_pending = [p for p in _pending_now
                             if p in ("db_rows", "credit_ratings", "store_totals", "stores_by_country")]
            _retry_key = f"_ratings_retry_{ticker}"
            _n_retry = st.session_state.get(_retry_key, 0)
            if _core_pending and _n_retry < 4 and not _settled:
                st.session_state[_retry_key] = _n_retry + 1
                st.session_state[_rat_inc_key] = True   # keep the fragment polling
                st.markdown(
                    '<div style="display:flex;align-items:center;gap:12px;padding:24px 0;">'
                    '<div style="width:22px;height:22px;border:3px solid #eee;border-top:3px solid #d62e2f;'
                    'border-radius:50%;animation:rd-spin 0.8s linear infinite;"></div>'
                    '<span style="font-family:Montserrat,sans-serif;font-size:15px;color:#888;">'
                    'Loading store &amp; ratings data&hellip;</span></div>'
                    '<style>@keyframes rd-spin{to{transform:rotate(360deg)}}</style>',
                    unsafe_allow_html=True)
                return
            st.session_state.pop(_retry_key, None)
            st.session_state[_settled_key] = True       # sticky: never re-arm the poll
            st.session_state[_rat_inc_key] = False      # give up → stop polling
            st.info("No extracted data available. Check official filings.")
            return
        st.session_state.pop(f"_ratings_retry_{ticker}", None)

        # Auto-heal partial renders (16-Jul): when a bounded background fetch
        # missed its deadline (first view of a ticker right after a server
        # restart / during the warm sweep), the payload arrives incomplete —
        # e.g. DB store types instead of the XBRL total, no Stores by Country.
        # The fetch keeps running, so retry-rerun up to twice instead of
        # showing a wrong-looking table the user must manually refresh away.
        _pending = data.get("_pending_fetches") or []
        # Retry-rerun ONLY for store-shaping fetches. The table renders fine
        # without credit ratings (most retailers have none), and on STG a cold
        # credit-ratings extraction runs 30-60s — retrying for it burned ~15s
        # of spinner per view for nothing (STG log 16-Jul:
        # pending=credit_ratings retry=2 ×14). CR fills in on later reruns.
        # square_footage is retry-worthy ONLY when the company actually has store
        # data to attach it to. A company with credit ratings but no stores (e.g.
        # ADBE/Adobe) must NOT spin/poll for a square-footage figure that will
        # never arrive — show its ratings instantly and settle.
        _has_store_data = bool(store_counts or sbc_countries)
        _retry_worthy = [
            p for p in _pending
            if p != "credit_ratings"
            and not (p == "square_footage" and not _has_store_data)
        ]
        _pending_key = f"_ratings_pending_retry_{ticker}"
        if _pending and not _settled:
            _n_pending = st.session_state.get(_pending_key, 0)
            _rt_log("RATINGS_partial_payload", 0,
                    details=f"ticker={ticker} pending={','.join(_pending)} retry={_n_pending} retry_worthy={bool(_retry_worthy)}")
            if _retry_worthy and _n_pending < 4:
                # A store-shaping fetch is still warming — the partial table renders
                # below and the caller's fragment re-polls to self-heal to the full
                # payload (store counts + geographic breakdown) WITHOUT a manual
                # refresh. Bounded so it can never loop.
                st.session_state[_pending_key] = _n_pending + 1
                st.session_state[_rat_inc_key] = True
            else:
                # Budget exhausted (or only credit_ratings left) → settle for good:
                # stop polling and never re-arm on later incidental reruns.
                st.session_state.pop(_pending_key, None)
                st.session_state[_settled_key] = True
                st.session_state[_rat_inc_key] = False
        else:
            # Complete (or already settled). Mark settled so later reruns never
            # re-arm the 1.5s poll (root fix for the rerun storm). A manual refresh
            # still re-renders and picks up freshly-cached late fetches.
            st.session_state.pop(_pending_key, None)
            st.session_state[_settled_key] = True
            st.session_state[_rat_inc_key] = False

        # When the country breakdown covers every year the worldwide series has,
        # the separate "Store Count" section would duplicate the Total (Worldwide)
        # row inside Stores by Country — show only the country section then.
        _sbc_total_row = stores_by_country.get("total_row", {}) if (sbc_years and sbc_countries) else {}
        _hide_sc_section = bool(_sbc_total_row) and all(
            (yr in _sbc_total_row) for sc in store_counts
            for yr, v in sc["values"].items() if v is not None
        )

        # Fiscal-year-end display dates (match Income Statement / Key Stats);
        # fall back to filing dates only when the FYE month is unknown.
        period_dates = data.get("period_display_dates") or data.get("period_dates", {})
        _parts = []
        _parts.append('<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>')

        # Header row — "For Fiscal Period Ending" with year columns
        _parts.append('<tr class="row-grey-separator"><th>Ratings &amp; Store Data<span class="header-subtext">Annual (10-K filings)</span></th>')
        for yr in years:
            pd = period_dates.get(yr)
            _p_date = pd.strftime("%b-%d-%Y") if pd and hasattr(pd, 'strftime') else str(yr)
            _parts.append(f'<th class="data-col"><span class="period-label">10-K</span><span class="period-date">{_p_date}</span></th>')
        _parts.append('</tr></thead><tbody>')

        # ── Credit Ratings Section ──
        if credit_ratings:
            _parts.append(f'<tr class="row-bold row-grey-separator"><td class="indent-0" style="font-weight:700;">Credit Ratings</td>')
            for _ in years:
                _parts.append('<td class="data-cell"></td>')
            _parts.append('</tr>')

            for cr in credit_ratings:
                agency = cr["agency"]
                # Rating row
                _parts.append(f'<tr><td class="indent-1" style="font-weight:500;">{html_escape(agency)} Rating</td>')
                for yr in years:
                    val = cr["values"].get(yr) or ''
                    _cell = html_escape(str(val)) if val and str(val).lower() != 'none' else "-"
                    _parts.append(f'<td class="data-cell" style="text-align:center;font-weight:600;">{_cell}</td>')
                _parts.append('</tr>')

                # Outlook row
                _parts.append(f'<tr><td class="indent-2" style="color:#4B5563;">Outlook</td>')
                for yr in years:
                    outlook = cr["outlooks"].get(yr) or ''
                    _has_val = outlook and str(outlook).lower() not in ('none', '')
                    _color = '#059669' if str(outlook).lower() == 'stable' else '#D97706' if str(outlook).lower() == 'positive' else '#DC2626' if str(outlook).lower() == 'negative' else '#6B7280'
                    _parts.append(f'<td class="data-cell" style="text-align:center;color:{_color};font-weight:500;">{html_escape(str(outlook)) if _has_val else "-"}</td>')
                _parts.append('</tr>')

                # Short-term rating row (if available in details)
                has_st = False
                for yr in years:
                    d = cr["details"].get(yr)
                    if d and d.get("short_term_rating"):
                        has_st = True
                        break
                if has_st:
                    _parts.append(f'<tr><td class="indent-2" style="color:#4B5563;">Short-Term</td>')
                    for yr in years:
                        d = cr["details"].get(yr)
                        st_val = d.get("short_term_rating", '') if d else ''
                        _parts.append(f'<td class="data-cell" style="text-align:center;">{html_escape(str(st_val)) if st_val else "-"}</td>')
                    _parts.append('</tr>')

        # ── Store Counts Section (hidden when Stores by Country covers it) ──
        if store_counts and not _hide_sc_section:
            # No per-country store breakdown for this company → name the
            # geographies it reports revenue for on the Segments tab instead of
            # a bare "Worldwide" (21-Jul-2026, per business). This is a disk
            # read; the 5-14s segment build only ever runs in the background.
            _t_geo = _time.perf_counter()
            from data.repository import geo_segment_members
            _sc_members = [] if (sbc_years and sbc_countries) else geo_segment_members(ticker)
            _rt_log("RATINGS_geo_label", (_time.perf_counter() - _t_geo) * 1000,
                    details=f"ticker={ticker} n={len(_sc_members)}")
            _parts.append(f'<tr class="row-bold row-grey-separator"><td class="indent-0" style="font-weight:700;">{_store_count_header_cell(_sc_members)}</td>')
            for _ in years:
                _parts.append('<td class="data-cell"></td>')
            _parts.append('</tr>')

            for sc in store_counts:
                store_type = sc["store_type"].title()
                _parts.append(f'<tr><td class="indent-1" style="font-weight:500;">{html_escape(store_type)}</td>')
                for yr in years:
                    val = sc["values"].get(yr)
                    if val is not None:
                        _parts.append(f'<td class="data-cell" style="text-align:center;font-weight:600;">{int(val):,}</td>')
                    else:
                        _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                _parts.append('</tr>')

                # YoY change row — always computed on chronological year order;
                # iterating display order inverts the % when Sort = Latest.
                _yoy_pct: Dict[int, float] = {}
                _prev_val = None
                for _cy in sorted(years):
                    _cv = sc["values"].get(_cy)
                    if _cv is not None and _prev_val is not None and _prev_val != 0:
                        _yoy_pct[_cy] = ((_cv - _prev_val) / _prev_val) * 100
                    _prev_val = _cv
                _parts.append(f'<tr><td class="indent-2" style="color:#4B5563;">YoY Change</td>')
                for yr in years:
                    pct = _yoy_pct.get(yr)
                    if pct is not None:
                        _color = '#059669' if pct > 0 else '#DC2626' if pct < 0 else '#6B7280'
                        _sign = '+' if pct > 0 else ''
                        _parts.append(f'<td class="data-cell" style="text-align:center;color:{_color};">{_sign}{pct:.1f}%</td>')
                    else:
                        _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                _parts.append('</tr>')

        # ── Stores by Country Section (edgartools XBRL) ──
        if sbc_years and sbc_countries:
            # Partial disclosure (16-Jul): the 10-K's split doesn't sum to the
            # worldwide total (CAL discloses only Brand Portfolio stores by
            # country) — label it and skip subtotals/total so nothing implies
            # the rows add up; the Store Count section above keeps the total.
            _sbc_partial = bool(stores_by_country.get("partial"))
            _sbc_header = ("Stores by Country (partial — as disclosed in 10-K; does not sum to total)"
                           if _sbc_partial else "Stores by Country")
            _parts.append(f'<tr class="row-bold row-grey-separator"><td class="indent-0" style="font-weight:700;">{html_escape(_sbc_header)}</td>')
            for yr in years:
                _parts.append('<td class="data-cell"></td>')
            _parts.append('</tr>')

            # Continent grouping (16-Jul-2026, per business), Excel-report
            # layout: each continent's countries first (indented under it),
            # then that continent's bold subtotal, and the grand total LAST.
            # Single continent → grand total labeled by the continent;
            # exactly North + South America → "Total (Americas)";
            # otherwise "Total (Worldwide)".
            from data.repository import group_countries_by_continent, stores_total_label
            _sbc_groups = group_countries_by_continent(sbc_countries)
            _multi_continent = len(_sbc_groups) > 1 and not _sbc_partial
            _country_indent = "indent-2" if _multi_continent else "indent-1"

            # Country rows render NORMAL weight so the bold continent/grand
            # totals stand out (16-Jul, per business).
            for _cont, _cont_countries, _cont_totals in _sbc_groups:
                for country in _cont_countries:
                    _parts.append(f'<tr><td class="{_country_indent}" style="font-weight:400;">{html_escape(country)}</td>')
                    for yr in years:
                        val = sbc_countries[country].get(yr)
                        if val is not None:
                            _parts.append(f'<td class="data-cell" style="text-align:center;font-weight:400;">{int(val):,}</td>')
                        else:
                            _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                    _parts.append('</tr>')
                if _multi_continent:
                    _parts.append(f'<tr class="row-total-strong"><td class="indent-1">Total ({html_escape(_cont)})</td>')
                    for yr in years:
                        _sv = _cont_totals.get(yr)
                        if _sv is not None:
                            _parts.append(f'<td class="data-cell" style="text-align:center;font-weight:700;">{int(_sv):,}</td>')
                        else:
                            _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                    _parts.append('</tr>')

            # Verified grand total as the LAST row (bold, left) + YoY beneath —
            # a year only renders country rows when they reconcile. Label comes
            # from stores_total_label: continent / Americas only when the listed
            # countries account for the total EXACTLY; partial splits (CMG)
            # honestly stay "Worldwide".
            sbc_total_row = stores_by_country.get("total_row", {})
            _total_label = stores_total_label(_sbc_groups, sbc_total_row)
            if sbc_total_row:
                _parts.append(f'<tr class="row-total-strong"><td class="indent-0">{html_escape(_total_label)}</td>')
                for yr in years:
                    _tv = sbc_total_row.get(yr)
                    if _tv is not None:
                        _parts.append(f'<td class="data-cell" style="text-align:center;font-weight:700;">{int(_tv):,}</td>')
                    else:
                        _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                _parts.append('</tr>')

                _yoy_pct = {}
                _prev_val = None
                for _cy in sorted(years):
                    _cv = sbc_total_row.get(_cy)
                    if _cv is not None and _prev_val is not None and _prev_val != 0:
                        _yoy_pct[_cy] = ((_cv - _prev_val) / _prev_val) * 100
                    _prev_val = _cv
                _parts.append(f'<tr><td class="indent-2" style="color:#4B5563;">YoY Change</td>')
                for yr in years:
                    pct = _yoy_pct.get(yr)
                    if pct is not None:
                        _color = '#059669' if pct > 0 else '#DC2626' if pct < 0 else '#6B7280'
                        _sign = '+' if pct > 0 else ''
                        _parts.append(f'<td class="data-cell" style="text-align:center;color:{_color};">{_sign}{pct:.1f}%</td>')
                    else:
                        _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                _parts.append('</tr>')

        # ── Square Footage / Property Area Section ──
        if sqft_metrics:
            _sqft_src = sqft_data.get("_source", "")
            _src_label = " (SEC EDGAR)" if _sqft_src == "edgartools" else ""
            _parts.append(f'<tr class="row-bold row-grey-separator"><td class="indent-0" style="font-weight:700;">Square Footage{html_escape(_src_label)}</td>')
            for _ in years:
                _parts.append('<td class="data-cell"></td>')
            _parts.append('</tr>')

            for metric in sqft_metrics:
                metric_name = metric["metric"]
                unit = metric.get("unit", "")
                unit_display = f" ({html_escape(unit)})" if unit else ""
                _parts.append(f'<tr><td class="indent-1" style="font-weight:500;">{html_escape(metric_name)}{unit_display}</td>')
                for yr in years:
                    val = metric["values"].get(yr)
                    if val is not None:
                        try:
                            _num = float(val)
                            _formatted = f"{int(_num):,}" if _num == int(_num) else f"{_num:,.1f}"
                        except (ValueError, TypeError):
                            _formatted = html_escape(str(val))
                        _parts.append(f'<td class="data-cell" style="text-align:center;font-weight:600;">{_formatted}</td>')
                    else:
                        _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                _parts.append('</tr>')

                # YoY change row for area metrics — chronological order (see store counts)
                _yoy_pct = {}
                _prev_val = None
                for _cy in sorted(years):
                    _v = metric["values"].get(_cy)
                    try:
                        _cv = float(_v) if _v is not None else None
                    except (ValueError, TypeError):
                        _cv = None
                    if _cv is not None and _prev_val is not None and _prev_val != 0:
                        _yoy_pct[_cy] = ((_cv - _prev_val) / _prev_val) * 100
                    _prev_val = _cv
                _parts.append(f'<tr><td class="indent-2" style="color:#4B5563;">YoY Change</td>')
                for yr in years:
                    pct = _yoy_pct.get(yr)
                    if pct is not None:
                        _color = '#059669' if pct > 0 else '#DC2626' if pct < 0 else '#6B7280'
                        _sign = '+' if pct > 0 else ''
                        _parts.append(f'<td class="data-cell" style="text-align:center;color:{_color};">{_sign}{pct:.1f}%</td>')
                    else:
                        _parts.append('<td class="data-cell" style="text-align:center;">-</td>')
                _parts.append('</tr>')

        _parts.append('</tbody></table></div></div>')
        _t_html = _time.perf_counter()
        html = ''.join(_parts)
        st.html(html)
        _html_ms = (_time.perf_counter() - _t_html) * 1000
        _total_ms = (_time.perf_counter() - _t0) * 1000
        _rt_log("RATINGS_html_render", _html_ms, details=f"ticker={ticker} html_len={len(html)}")
        _rt_log("RATINGS_total_render", _total_ms, details=f"ticker={ticker} fetch={_fetch_ms:.0f}ms html={_html_ms:.0f}ms")

    except Exception as e:
        log_structured_error(e, page="market_data", component="render_ratings_data", operation="render_ratings_table")
        st.error("Something went wrong loading ratings data. Please try again.")


def render_stock_quote(ticker: str, currency: str = "USD", company_overview=None) -> None:
    """Render Stock Quote and Chart table matching Figma design node-id=20660-223833.

    Args:
        ticker: Company ticker symbol
        currency: Currency code for display formatting
        company_overview: Pre-fetched CompanyOverview object. When provided, overview data
            is derived locally (avoiding 3 redundant DB calls for company_overview,
            shares_outstanding, and latest_close).

    Layout (Figma):
    ┌──────────────────────────────────────────────────────┐
    │  Stock Quote and Chart (Currency: USD)  [bold 16px]  │
    ├────────────────────────┬─────────────────────────────┤
    │  Left col (4 sub-cols) │  Right col (chart area)     │
    │  6 rows × [label|val | label|val]                    │
    └────────────────────────┴─────────────────────────────┘
    """
    import time as _time
    _t0 = _time.perf_counter()
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from data.repository import StockQuoteRepository

    # ── Fetch Data (parallelized — 2 independent calls) ────
    # When company_overview is provided, overview data is derived locally
    # from the already-fetched CompanyOverview + quote close price,
    # eliminating 3 redundant DB calls (company_overview, shares_outstanding, latest_close).
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from utils.server_logger import get_rerun_id, set_page_context, _rerun_id_var, _thread_local

    _t_parallel_start = _time.perf_counter()
    _results = {}

    # Capture parent thread context for propagation to worker threads
    _parent_rerun_id = get_rerun_id()
    _parent_page = "market_data"

    def _set_thread_context():
        """Propagate logging context from parent thread to worker thread."""
        try:
            _rerun_id_var.set(_parent_rerun_id)
            _thread_local.rerun_id = _parent_rerun_id
            set_page_context(_parent_page)
        except Exception as exc:
            log_structured_error(exc, page="market_data", component="render_stock_quote", operation="_set_thread_context")

    def _fetch_quote():
        try:
            _set_thread_context()
            t = _time.perf_counter()
            r = StockQuoteRepository.get_latest_quote(ticker)
            return 'quote', r, (_time.perf_counter() - t) * 1000
        except Exception as exc:
            log_structured_error(exc, page="market_data", component="render_stock_quote", operation="_fetch_quote")
            return 'quote', None, 0.0

    def _fetch_history():
        try:
            _set_thread_context()
            t = _time.perf_counter()
            r = StockQuoteRepository.get_price_history(ticker, days=1825)  # 60 months
            return 'history', r, (_time.perf_counter() - t) * 1000
        except Exception as exc:
            log_structured_error(exc, page="market_data", component="render_stock_quote", operation="_fetch_history")
            return 'history', None, 0.0

    if company_overview is not None:
        # Fast path: derive overview from pre-fetched CompanyOverview + quote close
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fn) for fn in (_fetch_quote, _fetch_history)]
            for f in as_completed(futures):
                key, data, ms = f.result()
                _results[key] = (data, ms)

        quote, _quote_time = _results['quote']
        history, _history_time = _results['history']

        # Build overview dict from CompanyOverview + latest close price
        _t_ov = _time.perf_counter()
        _close = quote.get("close") if quote else None
        _shares = getattr(company_overview, 'shares_outstanding', None)
        _mcap_mm = None
        if _close is not None and _shares is not None and _shares > 0:
            _mcap_mm = (_close * _shares) / 1_000_000
        _shares_mm = (_shares / 1_000_000) if _shares is not None else None
        _div_yield = getattr(company_overview, 'dividend_yield', None)
        overview = {
            "market_cap_mm":         _mcap_mm,
            "shares_outstanding_mm": _shares_mm,
            "shares_outstanding":    _shares,
            "dividend_yield":        _div_yield,
            "diluted_eps":           getattr(company_overview, 'eps', None),
            "pe_ratio":              getattr(company_overview, 'pe_ratio', None),
            "week_52_high":          getattr(company_overview, 'week_52_high', None),
            "week_52_low":           getattr(company_overview, 'week_52_low', None),
        }
        _overview_time = (_time.perf_counter() - _t_ov) * 1000
    else:
        # Fallback: fetch overview via DB (original 3-call path)
        def _fetch_overview():
            try:
                _set_thread_context()
                t = _time.perf_counter()
                r = StockQuoteRepository.get_overview_data(ticker)
                return 'overview', r, (_time.perf_counter() - t) * 1000
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_stock_quote", operation="_fetch_overview")
                return 'overview', None, 0.0

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(fn) for fn in (_fetch_quote, _fetch_overview, _fetch_history)]
            for f in as_completed(futures):
                key, data, ms = f.result()
                _results[key] = (data, ms)

        quote, _quote_time = _results['quote']
        overview, _overview_time = _results['overview']
        history, _history_time = _results['history']

    _parallel_total = (_time.perf_counter() - _t_parallel_start) * 1000

    # ── helper formatters ─────────────────────────────────
    # Get currency symbol for displaying with values
    _curr_sym = get_currency_symbol(currency)

    def _fmt_price(v):
        if v is None:
            return "-"
        return f"{_curr_sym} {v:,.2f}"

    def _fmt_mm(v):
        if v is None:
            return "-"
        return f"{v:,.1f}"

    def _fmt_mktcap_mm(v):
        """Format market cap from millions → SymbolX.XXXT / SymbolX.XXB / SymbolX.XXM"""
        if v is None:
            return "-"
        if v >= 1_000_000:          # >= 1T
            return f"{_curr_sym} {v / 1_000_000:.2f}T"
        if v >= 1_000:              # >= 1B
            return f"{_curr_sym} {v / 1_000:.2f}B"
        return f"{_curr_sym} {v:.2f}M"

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

    # ── extract values ────────────────────────────────────
    if quote:
        close_price  = _fmt_price(quote.get("close"))
        open_price   = _fmt_price(quote.get("open"))
        change       = _fmt_change(quote.get("change_on_day"))
        change_pct   = _fmt_pct_change(quote.get("change_percent"))
        day_hl       = f"{_fmt_price(quote.get('high'))} / {_fmt_price(quote.get('low'))}"
    else:
        close_price = open_price = change = change_pct = day_hl = "-"

    if overview:
        market_cap   = _fmt_mktcap_mm(overview.get("market_cap_mm"))
        shares_out   = _fmt_mm(overview.get("shares_outstanding_mm"))
        div_yield    = _fmt_pct(overview.get("dividend_yield"))
        w52h         = _fmt_price(overview.get("week_52_high"))
        w52l         = _fmt_price(overview.get("week_52_low"))
        week_52_hl   = f"{w52h} / {w52l}"
    else:
        market_cap = shares_out = div_yield = week_52_hl = "-"

    # ── 8 rows - Single column layout (label, value) for better readability ──
    rows = [
        ("Closing Price",       close_price),
        ("Opening Price",       open_price),
        ("Change on Day",       change),
        ("Change % on Day",     change_pct),
        ("Day High/Low",        day_hl),
        ("52 Week High/Low",      week_52_hl),
        ("Market Capitalization", market_cap),
        ("Shares Out. (mm)",    shares_out),
        ("Dividend Yield %",    div_yield),
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
        width: 50%;
    }
    .sq-val {
        background: #FFFFFF;
        color: #000000;
        text-align: right;
        width: 50%;
        vertical-align: bottom;
        padding-bottom: 2px;
    }
    </style>
    """
    st.html(css)

    # ── Section header ────────────────────────────────────
    _currency_display = format_currency_display(currency)
    st.html(f'<div class="sq-header">Stock Quote and Chart (Currency: {_currency_display})</div>')

    # ── Body: table left (35%) | chart right (65%) ────────
    # Balanced: Table readable, Chart spacious
    col_tbl, col_chart = st.columns([35, 65])

    with col_tbl:
        tbl_rows = ""
        for label, value in rows:
            tbl_rows += (
                f'<tr>'
                f'<td class="sq-lbl">{label}</td>'
                f'<td class="sq-val">{value}</td>'
                f'</tr>'
            )
        st.html(f'<div class="sq-table-box"><table class="sq-table">{tbl_rows}</table></div>')

    with col_chart:
        # ── Get Stock Price chart data ──
        # Reuse already-fetched price history (from parallel block) instead of
        # a separate get_stock_price_chart_data DB call — same table, same data
        chart_data = history

        if chart_data:
            dates  = [d["date"] for d in chart_data]
            closes = [d["close"] for d in chart_data]

            # ── Simple Price Chart ──
            fig = go.Figure()

            # Prepare hover text
            hover_texts = []
            for d, c in zip(dates, closes):
                txt = f"<b>Date:</b> {d}<br><b>Close:</b> {_curr_sym} {c:,.2f}"
                hover_texts.append(txt)

            fig.add_trace(
                go.Scatter(
                    x=dates, y=closes,
                    mode="lines",
                    fill="tozeroy",
                    fillcolor="rgba(214,46,47,0.07)",
                    line=dict(color="#D62E2F", width=2),
                    name="Price",
                    showlegend=False,
                    hovertemplate="%{customdata}<extra></extra>",
                    customdata=hover_texts,
                ),
            )

            fig.update_layout(
                title=dict(
                    text="Stock Price",
                    font=dict(size=11, color="#2D2A29", family="Roboto"),
                    x=0.5, xanchor="center",
                    y=0.98, yanchor="top",
                ),
                margin=dict(l=10, r=10, t=30, b=10),
                plot_bgcolor="#FFFFFF",
                paper_bgcolor="#FCFCFC",
                height=240,
                font=dict(family="Roboto", size=9, color="#888"),
                hovermode="x unified",
                hoverlabel=dict(
                    bgcolor="white",
                    bordercolor="#D62E2F",
                    font=dict(size=10, color="#2D2A29"),
                ),
            )

            # No axis labels, subtle grid only
            fig.update_xaxes(showgrid=False, showticklabels=False,
                             showline=False, zeroline=False)
            fig.update_yaxes(
                showgrid=True, gridcolor="#F0F0F0", gridwidth=1,
                showticklabels=False,
                showline=False, zeroline=False,
            )

            st.plotly_chart(
                fig,
                width="stretch",
                config={"displayModeBar": False},
                key=f"stock_price_chart_{ticker}",
            )
        else:
            st.caption("No price history available")

        _elapsed = (_time.perf_counter() - _t0) * 1000


def render_page():
    """Main render function - PIXEL PERFECT FIGMA MATCH."""
    import time as _time
    _perf_logger = logging.getLogger("market_data.perf")
    _page_start = _time.perf_counter()
    _timings = {}  # Track section timings

    # Seed / sync period_type from URL every run when URL carries a valid value.
    # Query string is authoritative for refresh + deep links; avoids stale session
    # vs URL after localStorage two-phase hydration.
    _early_period_key = "period_type_market_data_shared"
    _qp_period_early = st.query_params.get("period_type")
    if _qp_period_early in ("Annual", "Quarterly"):
        st.session_state[_early_period_key] = _qp_period_early
    elif _early_period_key not in st.session_state:
        st.session_state[_early_period_key] = "Annual"

    _rid = get_rerun_id()
    log_info(
        f"[MD_PHASE] rerun={_rid} phase=render_start "
        f"ticker={st.query_params.get('ticker')} tab={st.query_params.get('tab')} "
        f"qp_period={st.query_params.get('period_type')} ss_period={st.session_state.get(_early_period_key)} "
        f"run_counter={st.session_state.get('_md_run_counter', 0)}"
    )
    st.session_state["_md_run_counter"] = int(st.session_state.get("_md_run_counter", 0)) + 1
    if st.session_state["_md_run_counter"] > 80:
        log_info(
            f"[MD_WARN] rerun={_rid} high_run_counter={st.session_state['_md_run_counter']} "
            "possible Streamlit rerun storm — check query_params writes"
        )

    log_info(f"[RENDER_START] ticker={st.query_params.get('ticker')} tab={st.query_params.get('tab')} period={st.query_params.get('period_type')} ss_period={st.session_state.get('period_type_market_data_shared')}")

    # Loading indicator as a FIXED, centered OVERLAY \u2014 it floats IN FRONT of the
    # page instead of being inserted at the top of the content flow. The old
    # in-flow block pushed the whole page down while loading, then collapsed when
    # cleared, so every tab click jumped/flickered. A fixed overlay occupies zero
    # layout space, so the page underneath stays put and there is no jump.
    # Canonical Coresight loader (overlay = zero layout space, so an in-place tab
    # swap does not push existing content down \u2014 same visual as every other page).
    from components.loading import render_page_loader
    # Dynamic per-tab label so the loader reads e.g. "Loading Income Statement".
    _MD_TAB_LABELS = {
        "company_profile": "Loading Company Profile", "key_stats": "Loading Key Stats",
        "income_statement": "Loading Income Statement", "balance_sheet": "Loading Balance Sheet",
        "cash_flow": "Loading Cash Flow", "ratios": "Loading Ratios",
        "segment_data": "Loading Segment Data",
        # The tab whose key is "ratings" is labelled "Additional Data" in the tab bar
        # (components/toolbar.py), so the loader must say the same thing the user
        # clicked. There is no "additional_data" tab key — that entry was dead and
        # never matched a query param.
        "ratings": "Loading Additional Data",
        "estimates": "Loading Estimates", "forecasting": "Loading Forecasting",
    }
    _md_tab_qp = (st.query_params.get("tab") or "").strip().lower()
    _tab_loading_hint = render_page_loader(
        _MD_TAB_LABELS.get(_md_tab_qp, "Loading Market Data"), overlay=True)
    # Stash the placeholder so any code that reruns mid-render (e.g. the ratings
    # auto-heal reruns in render_ratings_data) can tear the overlay down FIRST —
    # otherwise the st.empty() is only cleared at the end of render_page(), and a
    # rerun orphans it in the DOM. It is click-through (pointer-events:none) so an
    # orphan is harmless, but clearing it up front removes the lingering spinner.
    st.session_state["_md_tab_loader"] = _tab_loading_hint
    # Style the lazy Excel buttons (native st.button, keyed `_xlbtn_*`) to match the
    # former red-outline control AND right-align them, as the old iframe button was
    # (its body had justify-content:flex-end). Injected once per full render; the
    # rules stay in the DOM across fragment-only reruns. Scoped by Streamlit's stable
    # st-key- wrapper class so it never leaks to other buttons.
    st.markdown(
        "<style>"
        '[class*="st-key-_xlbtn_"]{width:100%!important;display:flex!important;justify-content:flex-end!important;}'
        '[class*="st-key-_xlbtn_"] button{'
        "background:transparent!important;border:1px solid #D62E2F!important;"
        "color:#D62E2F!important;border-radius:4px!important;padding:6px 14px!important;"
        "font-size:13px!important;font-weight:500!important;min-height:0!important;"
        "width:auto!important;}"
        '[class*="st-key-_xlbtn_"] button:hover{background:#D62E2F!important;color:#fff!important;}'
        "</style>",
        unsafe_allow_html=True,
    )

    # Initialize local storage manager and sync state
    _t0 = _time.perf_counter()
    storage_manager = sync_market_data_state()
    _timings['local_storage_sync'] = (_time.perf_counter() - _t0) * 1000
    log_info(
        f"[MD_PHASE] rerun={get_rerun_id()} phase=after_local_storage_sync "
        f"ms={_timings['local_storage_sync']:.1f} qp={dict(st.query_params)}"
    )

    _t_ticker_start = _time.perf_counter()
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
        selected_ticker = DEFAULT_FALLBACK_TICKER  # fallback

    # Validate ticker exists in database - fallback to default if not found
    selected_ticker, was_fallback = validate_and_get_ticker(
        url_ticker=selected_ticker,
        session_ticker=None,  # Already handled above
        page_name="market_data"
    )

    # Only mutate query params when values actually change — each mutation triggers a rerun.
    if was_fallback and st.query_params.get("ticker") != selected_ticker:
        st.query_params["ticker"] = selected_ticker

    if selected_ticker and selected_ticker != "ALL":
        if st.query_params.get("ticker") != selected_ticker:
            st.query_params["ticker"] = selected_ticker
    else:
        if "ticker" in st.query_params:
            del st.query_params["ticker"]

    # Save the selected ticker to local storage to persist across sessions
    set_marketdata_company(selected_ticker)

    # Detect ticker change — clear date/sort widget keys so Streamlit doesn't
    # raise "widget created with default value but also set via Session State API"
    prev_ticker = st.session_state.get("_prev_ticker_market_data")

    # Also check localStorage to detect if user selected a DIFFERENT company from Home page
    # localStorage might have data from previous company (e.g., BBWI) while URL has new company (ACI)
    _stored_ticker = get_marketdata_company()
    _ticker_from_localstorage = _stored_ticker if _stored_ticker else prev_ticker

    # On page refresh, prev_ticker is None (fresh session) but ticker hasn't truly changed.
    # However, if localStorage has a DIFFERENT ticker than URL, user changed company from Home page.
    # Only wipe date query params when the user ACTUALLY switched to a different company.
    _ticker_truly_changed = (prev_ticker is not None and prev_ticker != selected_ticker) or \
                            (_ticker_from_localstorage is not None and _ticker_from_localstorage != selected_ticker)

    if prev_ticker != selected_ticker:
        # Clear shared filter keys so they re-initialise for the new ticker
        # CRITICAL: Also clear period_type so it resets to available type for new company
        # CRITICAL: Clear currency so new company's reported currency is used (1:1 default)
        for _key in (
            "start_dt_shared", "end_dt_shared", "sort_order_select_shared",
            "period_type_select_shared",  # Reset period type on company change
            "_mdlc_date_range", "_mdlc_sort", "_mdlc_period", "_mdlc_conv",
            "_mdlc_from_curr", "_mdlc_to_curr", "_mdlc_units",
            "from_currency", "target_currency",  # Reset currency to reported (1:1)
            "currency_from_unified", "currency_to_unified",  # Reset unified currency selectors
        ):
            if _key in st.session_state:
                del st.session_state[_key]

        # Also clear period_type from session state on real company switches — but NOT on
        # first page load (prev_ticker is None): URL/query_params period must survive.
        _period_key = "period_type_market_data_shared"
        if _period_key in st.session_state and prev_ticker is not None:
            del st.session_state[_period_key]

        # CRITICAL FIX: When prev_ticker is None (first load), ALWAYS use 5-year default
        # because localStorage might have old data from previous company
        if prev_ticker is None:
            # Clear any existing date range from session to force 5-year default
            if "date_range_market_data_shared" in st.session_state:
                del st.session_state["date_range_market_data_shared"]
            # Clear localStorage date keys too (Python local_storage)
            from utils.local_storage import StorageKey, local_storage
            local_storage.delete(StorageKey.MARKETDATA_DATE_START)
            local_storage.delete(StorageKey.MARKETDATA_DATE_END)

            # CRITICAL: Also clear from browser localStorage (local_storage_manager)
            # This prevents sync_market_data_state() from reloading old date_range on next run
            storage_manager.delete_keys("market_data", [
                "date_range_market_data",
                "date_range_market_data_shared",
                "sort_order_market_data_shared",
                "conversion_mode",
                "units"
            ])

            # Also clear from session_state
            for _filter_key in ("sort_order_select_shared", "conversion_mode", "units"):
                if _filter_key in st.session_state:
                    del st.session_state[_filter_key]

            # Verify deletion
            _verify_start, _verify_end = get_marketdata_date_range()
        elif _ticker_truly_changed:
            for _qp in ("start_dt", "end_dt", "to_curr"):
                if _qp in st.query_params:
                    del st.query_params[_qp]

            # IMPORTANT: When company changes, we want 5-year default instead of stored 20-year range
            # Clear the date_range_market_data_shared from session so it gets recomputed with 5-year default
            if "date_range_market_data_shared" in st.session_state:
                del st.session_state["date_range_market_data_shared"]

            # Also clear localStorage date range so refresh doesn't bring back old range
            from utils.local_storage import StorageKey, local_storage
            local_storage.delete(StorageKey.MARKETDATA_DATE_START)
            local_storage.delete(StorageKey.MARKETDATA_DATE_END)

            # CRITICAL: Also clear from browser localStorage (local_storage_manager)
            storage_manager.delete_keys("market_data", [
                "date_range_market_data",
                "date_range_market_data_shared",
                "sort_order_market_data_shared",
                "period_type_market_data_shared",  # Clear period type
                "conversion_mode",
                "units"
            ])

            # Also clear from session_state
            for _filter_key in ("sort_order_select_shared", "period_type_select_shared", "conversion_mode", "units"):
                if _filter_key in st.session_state:
                    del st.session_state[_filter_key]

            # CRITICAL: Reset period type to default (Annual) for new company
            # This ensures we don't get stuck with Quarterly for a company that only has Annual
            _period_key = "period_type_market_data_shared"
            st.session_state[_period_key] = "Annual"
            if st.query_params.get("period_type") != "Annual":
                st.query_params["period_type"] = "Annual"

            # Verify deletion
            _verify_start, _verify_end = get_marketdata_date_range()
        else:
            pass  # Same company detected - preserving filters for ticker

        # Clear isolated date keys for estimates/forecasting (company-specific)
        for _iso_tab in ("estimates", "forecasting"):
            _iso_key = f"_dr_isolated_{_iso_tab}"
            if _iso_key in st.session_state:
                del st.session_state[_iso_key]

        # Also clear legacy per-tab keys (backward compat for any cached session)
        for _tab in ["income_statement", "balance_sheet", "cash_flow", "key_stats", "ratios", "estimates", "forecasting"]:
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
    _timings['ticker_resolution'] = (_time.perf_counter() - _t_ticker_start) * 1000

    # ── Parallel fetch: company overview + reported currency (independent, both need only ticker) ──
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from utils.server_logger import set_page_context, _rerun_id_var, _thread_local

    _parent_rerun_id = get_rerun_id()
    _parent_page = "market_data"

    def _set_md_thread_ctx():
        try:
            _rerun_id_var.set(_parent_rerun_id)
            _thread_local.rerun_id = _parent_rerun_id
            set_page_context(_parent_page)
        except Exception as exc:
            log_structured_error(exc, page="market_data", component="render_page", operation="_set_md_thread_ctx")

    _t0 = _time.perf_counter()

    def _fetch_company():
        try:
            _set_md_thread_ctx()
            t = _time.perf_counter()
            r = CompanyOverviewRepository.get_company_overview(_company_fetch_ticker)
            return 'company', r, (_time.perf_counter() - t) * 1000
        except Exception as exc:
            log_structured_error(exc, page="market_data", component="render_page", operation="_fetch_company")
            return 'company', None, 0.0

    def _fetch_currency():
        try:
            _set_md_thread_ctx()
            t = _time.perf_counter()
            r = _get_company_reported_currency(_company_fetch_ticker)
            return 'currency', r, (_time.perf_counter() - t) * 1000
        except Exception as exc:
            log_structured_error(exc, page="market_data", component="render_page", operation="_fetch_currency")
            return 'currency', "USD", 0.0

    _md_results = {}
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(fn) for fn in (_fetch_company, _fetch_currency)]
            for f in as_completed(futures):
                key, data, ms = f.result()
                _md_results[key] = (data, ms)
    except Exception as exc:
        log_structured_error(exc, page="market_data", component="render_page", operation="parallel_fetch_company_currency")

    company, _company_time = _md_results.get('company', (None, 0.0))
    _early_reported_currency, _currency_time = _md_results.get('currency', ("USD", 0.0))
    _parallel_elapsed = (_time.perf_counter() - _t0) * 1000
    _timings['company_overview_fetch'] = _parallel_elapsed
    _t_tab_setup_start = _time.perf_counter()

    # Note: Ticker validation with fallback is now handled at the start of render_page()
    # This check is a safety net - should never trigger due to validate_and_get_ticker()
    if not company:
        # Force fallback to default and reload
        selected_ticker = DEFAULT_FALLBACK_TICKER
        if st.query_params.get("ticker") != selected_ticker:
            st.query_params["ticker"] = selected_ticker
        st.session_state["active_ticker"] = selected_ticker
        set_marketdata_company(selected_ticker)
        # Try fetching default company
        company = CompanyOverviewRepository.get_company_overview(selected_ticker)
        if not company:
            # Absolute fallback - show error only if even default ticker fails
            st.error(f"Unable to load company data. Please try again later.")
            st.stop()
        # Re-fetch currency for fallback ticker (rare path)
        _early_reported_currency = _get_company_reported_currency(selected_ticker)

    # Resolve tab: Check if we're initializing (no query params) or if query params exist
    query_tab = st.query_params.get("tab")
    stored_tab = get_marketdata_tab()

    # If no query tab, use stored tab, otherwise use query tab
    selected_tab = stored_tab if not query_tab else query_tab

    # Validate tab selection
    valid_tabs = ["income_statement", "balance_sheet", "cash_flow", "key_stats", "ratios", "estimates", "forecasting", "company_profile", "segment_data", "ratings"]
    if selected_tab not in valid_tabs:
        selected_tab = "company_profile"

    if not query_tab and stored_tab and stored_tab in valid_tabs:
        if st.query_params.get("tab") != stored_tab:
            st.query_params["tab"] = stored_tab

    # Store the selected tab in session state for persistence
    st.session_state.selected_tab_market_data = selected_tab

    # Analytics: the URL page_view captures tab/ticker/period/dates/sort, but NOT the
    # currency conversion target or units — capture those (session-state) too.
    try:
        from utils.server_logger import log_filters_if_changed
        log_filters_if_changed(
            "market_data",
            ticker=selected_ticker,
            tab=selected_tab,
            from_currency=st.session_state.get("from_currency") or None,
            to_currency=st.session_state.get("target_currency") or None,
            units=st.session_state.get("units_label") or st.session_state.get("units_sel") or None,
        )
    except Exception:
        pass

    # Save to local storage for persistence across sessions
    set_marketdata_tab(selected_tab)
    log_info(
        f"[MD_PHASE] rerun={get_rerun_id()} phase=after_tab_resolve "
        f"selected_tab={selected_tab} qp_tab={st.query_params.get('tab')}"
    )

    # ── Early non-blocking prefetch for edgartools credit ratings ──
    # Fire background preload immediately (any tab) so that by the time
    # the user clicks the Ratings tab, data is already cached.
    try:
        from data.repository import preload_edgartools_ratings, preload_store_totals
        preload_edgartools_ratings(selected_ticker)
        preload_store_totals(selected_ticker)
    except Exception:
        pass  # best-effort, never block page load

    # ── Early non-blocking prefetch for company_profile tab ──
    # Submit history+quote futures now so they run in background during
    # filter init + CSS + toolbar rendering (~200ms overlap savings).
    # Connections freed by company fetch are reused (no new SSL overhead).
    _pf_history_future = None
    _pf_quote_future = None
    _pf_executor = None
    _pf_submit_t = None
    if selected_tab == "company_profile":
        from data.repository import StockQuoteRepository as _SQR_early
        _pf_submit_t = _time.perf_counter()
        def _pf_hist():
            try:
                _set_md_thread_ctx()
                return _SQR_early.get_price_history(selected_ticker, days=1825)  # 60 months
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_pf_hist")
                return None
        def _pf_qt():
            try:
                _set_md_thread_ctx()
                return _SQR_early.get_latest_quote(selected_ticker)
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_pf_qt")
                return None
        _pf_executor = ThreadPoolExecutor(max_workers=2)
        _pf_history_future = _pf_executor.submit(_pf_hist)
        _pf_quote_future = _pf_executor.submit(_pf_qt)

    # Shared filter state — same across all financial tabs so switching tabs preserves filters
    tab_state_prefix = f"{selected_tab}_"
    date_range_key = "date_range_market_data_shared"
    sort_order_key = "sort_order_market_data_shared"
    period_type_key = "period_type_market_data_shared"

    # Estimates/Forecasting use isolated date state so their future/different dates
    # never overwrite the shared date range used by Key Stats, Income Statement, etc.
    # ALSO use isolated period_type keys so Estimates/Forecasting filters don't affect other tabs
    _EST_FCST_TABS = ("estimates", "forecasting")
    _active_period_type_key = (f"_period_type_isolated_{selected_tab}"
                               if selected_tab in _EST_FCST_TABS else period_type_key)
    # NOTE: _active_date_key will be defined after _period_type_db is calculated

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

    # Initialize conversion mode - priority: query_params > localStorage > default
    if 'conversion_mode' not in st.session_state:
        _qp_conv = st.query_params.get("conv_mode")
        if _qp_conv and _qp_conv in ("Today's Spot Rate", "Historical"):
            st.session_state.conversion_mode = _qp_conv
        else:
            # Try localStorage via get_persistent_state
            _stored_conv = get_persistent_state('conversion_mode', None)
            if _stored_conv and _stored_conv in ("Today's Spot Rate", "Historical"):
                st.session_state.conversion_mode = _stored_conv
            else:
                st.session_state.conversion_mode = "Today's Spot Rate"

    # Initialize units - priority: query_params > localStorage > default
    if 'units' not in st.session_state:
        _qp_units = st.query_params.get("units_sel")
        if _qp_units and _qp_units in ("Millions (mm)", "Billions (bn)", "Thousands (k)"):
            st.session_state.units = _qp_units
        else:
            # Try localStorage via get_persistent_state
            _stored_units = get_persistent_state('units', None)
            if _stored_units and _stored_units in ("Millions (mm)", "Billions (bn)", "Thousands (k)"):
                st.session_state.units = _stored_units
            else:
                st.session_state.units = "Millions (mm)"

    # Initialize sort order - priority: query_params > localStorage > default
    if sort_order_key not in st.session_state:
        _qp_sort = st.query_params.get("sort_ord")
        if _qp_sort and _qp_sort in ("Earliest", "Latest"):
            st.session_state[sort_order_key] = _qp_sort
        else:
            # Try localStorage - check both the shared key and legacy keys
            _stored_sort = get_persistent_state(sort_order_key, None)
            if _stored_sort and _stored_sort in ("Earliest", "Latest"):
                st.session_state[sort_order_key] = _stored_sort
            else:
                st.session_state[sort_order_key] = 'Earliest'

    # Initialize period type - priority: query_params > localStorage > default
    # Use isolated key for Estimates/Forecasting so they don't affect other tabs
    if _active_period_type_key not in st.session_state:
        _qp_period = st.query_params.get("period_type")
        if _qp_period and _qp_period in ("Annual", "Quarterly"):
            st.session_state[_active_period_type_key] = _qp_period
        else:
            # Try localStorage via get_persistent_state
            _stored_period = get_persistent_state(_active_period_type_key, None)
            if _stored_period and _stored_period in ("Annual", "Quarterly"):
                st.session_state[_active_period_type_key] = _stored_period
            else:
                st.session_state[_active_period_type_key] = 'Annual'

    # Convert UI period type to database value (use isolated key for Estimates/Forecasting)
    _period_type_sel_title = (st.session_state.get(_active_period_type_key) or 'Annual')
    # Availability-aware effective period type (per tab). Many foreign / YFinance
    # issuers report only Annual for some statements (e.g. no quarterly income
    # statement) while still having quarterly data for others (e.g. balance sheet).
    # Snap the selected type to one that actually has data for THIS tab so the grid
    # shows data and the Period Type filter still renders — instead of an empty page
    # with a hidden filter the user cannot change.
    if selected_tab == "company_profile":
        _available_period_types = ["Annual"]
        _period_type_eff_title = _period_type_sel_title
        _period_type_fell_back = False
    else:
        _t_apt = _time.perf_counter()
        _available_period_types = get_available_period_types(selected_ticker, selected_tab)
        _apt_ms = (_time.perf_counter() - _t_apt) * 1000
        if _apt_ms > 300:
            # Chief suspect inside the opaque tab_setup span: for segment_data
            # this can re-run the same 9,493-row _fetch_all_db_rows the date-range
            # and render also run — so segment fetches its rows 2-3× per render.
            # Aliased import: `log_timing` is a local later in render_page (line
            # ~4442), so the bare name is unbound here — use an alias like the
            # other in-function timing calls (_lt / _seg_log_timing / _rt_log).
            from utils.server_logger import log_timing as _apt_log
            _apt_log("PAGE_available_period_types", _apt_ms,
                     details=f"tab={selected_tab} ticker={selected_ticker} n={len(_available_period_types) if _available_period_types else 0}")
        if _period_type_sel_title in _available_period_types:
            _period_type_eff_title = _period_type_sel_title
        else:
            _period_type_eff_title = _available_period_types[0] if _available_period_types else "Annual"
        _period_type_fell_back = (_period_type_eff_title != _period_type_sel_title)
    _period_type_db = _period_type_eff_title.lower()

    # One-liner notice rendered on a tab when the selected period type is not
    # reported for this company and we fell back to an available one.
    _PERIOD_TAB_LABELS = {
        "income_statement": "income statement",
        "balance_sheet": "balance sheet",
        "cash_flow": "cash flow",
        "key_stats": "key stats",
        "ratios": "ratios",
        "segment_data": "segment data",
    }

    def _render_period_fallback_notice():
        if _period_type_fell_back and selected_tab in _PERIOD_TAB_LABELS:
            st.info(
                f"{_period_type_sel_title} {_PERIOD_TAB_LABELS[selected_tab]} data is not reported "
                f"for this company — showing {_period_type_eff_title} instead."
            )

    # Now define _active_date_key with period_type included so Quarterly/Annual have separate date ranges
    _active_date_key = (f"_dr_isolated_{selected_tab}_{_period_type_db}"
                        if selected_tab in _EST_FCST_TABS else date_range_key)

    # Get dates based on selected tab (skip for company_profile)
    _timings['tab_setup'] = (_time.perf_counter() - _t_tab_setup_start) * 1000
    _t0 = _time.perf_counter()
    # Safe defaults — prevents NameError if the try block below fails
    min_date = max_date = available_dates = None
    if selected_tab != "company_profile":
        try:
            if selected_tab == "balance_sheet":
                min_date, max_date = BalanceSheetRepository.get_date_range(selected_ticker, _period_type_db)
                available_dates = BalanceSheetRepository.get_available_dates(selected_ticker, _period_type_db)
            elif selected_tab == "cash_flow":
                from data.repository import CashFlowRepository
                min_date, max_date = CashFlowRepository.get_date_range(selected_ticker, _period_type_db)
                available_dates = CashFlowRepository.get_available_dates(selected_ticker, _period_type_db)
            elif selected_tab == "key_stats":
                min_date, max_date = KeyStatsRepository.get_date_range(selected_ticker, _period_type_db)
                available_dates = KeyStatsRepository.get_available_dates(selected_ticker, _period_type_db)
            elif selected_tab == "ratios":
                min_date, max_date = RatiosRepository.get_date_range(selected_ticker, _period_type_db)
                available_dates = RatiosRepository.get_available_dates(selected_ticker, _period_type_db)
            elif selected_tab == "segment_data":
                from data.repository import SegmentDataRepository
                min_date, max_date = SegmentDataRepository.get_date_range(selected_ticker, _period_type_db)
                available_dates = SegmentDataRepository.get_available_dates(selected_ticker, _period_type_db)
            elif selected_tab == "ratings":
                from data.repository import RatingsDataRepository
                min_date, max_date = RatingsDataRepository.get_date_range(selected_ticker)
                available_dates = RatingsDataRepository.get_available_dates(selected_ticker)
            elif selected_tab == "estimates":
                _est_min, _est_max = AnalystEstimatesRepository.get_date_range(selected_ticker, _period_type_db)
                _est_dates = AnalystEstimatesRepository.get_available_dates(selected_ticker, _period_type_db)
                if _est_min is None:
                    # YF company: synthetic single date so filters still render
                    from datetime import date as _d
                    _today = _d.today()
                    min_date, max_date, available_dates = _today, _today, [_today]
                else:
                    min_date, max_date, available_dates = _est_min, _est_max, _est_dates
            elif selected_tab == "forecasting":
                if _period_type_db == "quarterly":
                    min_date, max_date = ModelForecastsRepository.get_quarterly_date_range(selected_ticker)
                    available_dates = ModelForecastsRepository.get_quarterly_available_dates(selected_ticker)
                else:
                    min_date, max_date = ModelForecastsRepository.get_date_range(selected_ticker)
                    available_dates = ModelForecastsRepository.get_available_dates(selected_ticker)
            else:
                min_date, max_date = IncomeStatementRepository.get_date_range(selected_ticker, _period_type_db)
                available_dates = IncomeStatementRepository.get_available_dates(selected_ticker, _period_type_db)
        except Exception as exc:
            log_structured_error(exc, page="market_data", component="render_page", operation="fetch_date_range")

        _timings['date_range_fetch'] = (_time.perf_counter() - _t0) * 1000
        log_info(
            f"[MD_PHASE] rerun={get_rerun_id()} phase=after_date_range_fetch "
            f"tab={selected_tab} period={_period_type_db} min={min_date} max={max_date} "
            f"n_dates={len(available_dates) if available_dates else 0} "
            f"ms={_timings['date_range_fetch']:.1f}"
        )


    _t_css_start = _time.perf_counter()
    # ==================== GLOBAL CSS - PIXEL PERFECT FIGMA SPECS ====================
    st.html("""
    <style>

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

    /* ── Ghost-filter fix (#38) ──────────────────────────────────────────────
       On a slow rerun (changing Period Type triggers a multi-second cold data
       fetch), Streamlit keeps the PREVIOUS run's widgets mounted — marked
       data-stale="true" — next to the freshly-rendered ones, so a second "ghost"
       filter row appears until the run finishes. The fresh copy renders
       immediately (before the slow fetch), so hiding the STALE copy of any select
       control leaves exactly one visible filter row. Scoped to selectbox
       containers so data tables / notices keep their normal stale-then-update. */
    [data-testid="stElementContainer"][data-stale="true"]:has(div[data-testid="stSelectbox"]) {
        display: none !important;
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
        font-size: 12px !important;
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

    /* Strong totals (Stores by Country continent/grand totals): true bold,
       full black — must be unmistakably heavier than the country rows. */
    .row-total-strong td:first-child,
    .row-total-strong td.data-cell {
        font-weight: var(--font-weight-bold) !important;
        color: var(--black) !important;
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
    _timings['css_injection'] = (_time.perf_counter() - _t_css_start) * 1000

    # ==================== TITLE SECTION ====================
    _t_header_start = _time.perf_counter()
    render_company_header(
        company_name=company.name,
        ticker=company.ticker,
        exchange=company.exchange or "NYSE"
    )
    # Log tab change if any
    _prev_tab = st.session_state.get("_prev_rendered_tab")
    if _prev_tab != selected_tab:
        st.session_state["_prev_rendered_tab"] = selected_tab

    selected_tab = render_tabs(selected_tab)
    _timings['header_tabs_render'] = (_time.perf_counter() - _t_header_start) * 1000

    # After render_tabs, check if tab was changed via button click (query_params updated)
    _new_tab_from_ui = st.query_params.get("tab")
    if _new_tab_from_ui and _new_tab_from_ui != selected_tab:
        selected_tab = _new_tab_from_ui
        st.session_state["_prev_rendered_tab"] = selected_tab

    _t_filter_start = _time.perf_counter()
    if not min_date or not max_date:
        if selected_tab != "company_profile":  # Only show message if it's a financial tab
            st.info("No extracted data available. Check official filings.")
    else:
        # Compute default start = last 5 years from max_date, clamped to min_date
        try:
            _default_start = max_date.replace(year=max_date.year - 5)
        except ValueError:  # Feb 29 in non-leap year
            _default_start = max_date.replace(year=max_date.year - 5, day=28)
        _default_start = max(_default_start, min_date)

        # ── Date range resolution: session_state > query_params > localStorage > default ──
        # CRITICAL: We must distinguish between:
        # 1. Company change (truly_changed=True) -> use 5-year default
        # 2. User manual selection -> respect user's choice (even if 20 years)
        # 3. Tab switch/refresh -> preserve current selection

        log_info(f"[DATE_RESOLVE] START ticker={selected_ticker} tab={selected_tab} period={_period_type_db} min={min_date} max={max_date} default_start={_default_start} key_in_ss={_active_date_key in st.session_state}")

        if selected_tab in _EST_FCST_TABS:
            # Estimates / Forecasting: isolated date state — never read from or write to
            # the shared key. Each tab has its own key so the user's changes stay within
            # that tab and don't corrupt dates on Key Stats / Income Statement etc.
            if _active_date_key in st.session_state:
                _iso_s, _iso_e = st.session_state[_active_date_key]
                start_date = date.fromisoformat(_iso_s) if _iso_s else _default_start
                end_date   = date.fromisoformat(_iso_e) if _iso_e else max_date
                # Clamp to current tab's range in case company changed
                if start_date > max_date or start_date > end_date:
                    start_date, end_date = _default_start, max_date
                end_date = min(end_date, max_date)
            else:
                start_date, end_date = _default_start, max_date
            st.session_state[_active_date_key] = (start_date.isoformat(), end_date.isoformat())
            log_info(f"[DATE_RESOLVE] EST_FCST_ISOLATED start={start_date} end={end_date}")
        elif date_range_key in st.session_state:
            stored_start, stored_end = st.session_state[date_range_key]
            start_date = date.fromisoformat(stored_start) if stored_start else _default_start
            end_date = date.fromisoformat(stored_end) if stored_end else max_date
            log_info(f"[DATE_RESOLVE] FROM_SS start={start_date} end={end_date} ticker_changed={_ticker_truly_changed}")

            # Check if this is a company change with old wide range (from previous company)
            # Only reset to 5-year default if company TRULY changed and we haven't saved new range yet
            _loaded_span = (end_date - start_date).days / 365.25
            if _ticker_truly_changed and _loaded_span > 6:
                start_date, end_date = _default_start, max_date
                log_info(f"[DATE_RESOLVE] TICKER_CHANGE_RESET start={start_date} end={end_date}")
                st.session_state[date_range_key] = (start_date.isoformat(), end_date.isoformat())
                set_marketdata_date_range(start_date.isoformat(), end_date.isoformat())
            else:
                # Clamp end_date to max_date (guards against future dates leaked from Estimates tab)
                if end_date > max_date:
                    end_date = max_date
                    st.session_state[date_range_key] = (start_date.isoformat(), end_date.isoformat())
                    log_info(f"[DATE_RESOLVE] END_CLAMPED_TO_MAX end={end_date}")
                if start_date > max_date or start_date > end_date:
                    start_date, end_date = _default_start, max_date
                    log_info(f"[DATE_RESOLVE] START_BEYOND_MAX_RESET start={start_date} end={end_date}")
                    st.session_state[date_range_key] = (start_date.isoformat(), end_date.isoformat())
                else:
                    log_info(f"[DATE_RESOLVE] KEPT_AS_IS start={start_date} end={end_date}")
        else:
            # Try query params first (synchronous, survives page refresh)
            _qp_start = st.query_params.get("start_dt")
            _qp_end   = st.query_params.get("end_dt")
            if _qp_start and _qp_end:
                try:
                    start_date = max(min(date.fromisoformat(_qp_start), max_date), min_date)
                    end_date   = max(min(date.fromisoformat(_qp_end),   max_date), min_date)
                except ValueError as e:
                    start_date, end_date = _default_start, max_date
            else:
                # Fall back to legacy localStorage helper → default
                stored_start, stored_end = get_marketdata_date_range()
                if stored_start and stored_end:
                    stored_start_dt = date.fromisoformat(stored_start)
                    stored_end_dt = date.fromisoformat(stored_end)
                    years_span = (stored_end_dt - stored_start_dt).days / 365.25

                    if _ticker_truly_changed and years_span > 6:
                        start_date, end_date = _default_start, max_date
                    else:
                        if stored_start_dt > max_date:
                            start_date, end_date = _default_start, max_date
                        else:
                            start_date = max(stored_start_dt, min_date)
                            end_date   = min(stored_end_dt, max_date)
                else:
                    start_date, end_date = _default_start, max_date

            st.session_state[date_range_key] = (start_date.isoformat(), end_date.isoformat())

        # Always write back so downstream code can read it (only for non-est/fcst tabs)
        if selected_tab not in _EST_FCST_TABS:
            st.session_state[date_range_key] = (start_date.isoformat(), end_date.isoformat())

        # Get currency from database - use already fetched _early_reported_currency
        # Same currency applies to all tabs for the same company
        _period_type_db = _period_type_eff_title.lower()
        reported_currency = _early_reported_currency or "USD"

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
        if curr_start in date_options:
            start_idx = date_options.index(curr_start)
        else:
            _closest_s = min(available_dates, key=lambda d: abs((d - start_date).days))
            start_idx = date_options.index(_closest_s.strftime("%B %Y"))

        curr_end = end_date.strftime("%B %Y")
        if curr_end in date_options:
            end_idx = date_options.index(curr_end)
        else:
            _closest_e = min(available_dates, key=lambda d: abs((d - end_date).days))
            end_idx = date_options.index(_closest_e.strftime("%B %Y"))

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
        _pt_key = "period_type_select_shared"

        # "last confirmed" tracking keys (set at end of this block)
        _LC_DATE  = "_mdlc_date_range"
        _LC_SORT  = "_mdlc_sort"
        _LC_PERIOD = "_mdlc_period"
        _LC_CONV  = "_mdlc_conv"
        _LC_FROM  = "_mdlc_from_curr"
        _LC_TO    = "_mdlc_to_curr"
        _LC_UNITS = "_mdlc_units"

        # Detect if authoritative session-state values changed since last render
        _dr_now     = st.session_state.get(_active_date_key)
        _dr_lc      = st.session_state.get(_LC_DATE)
        _sw_in_ss   = _sw_key in st.session_state
        _sw_in_opts = (st.session_state.get(_sw_key) in date_options) if _sw_in_ss else False
        _date_sync  = (_dr_now != _dr_lc or not _sw_in_ss or not _sw_in_opts)
        log_info(f"[DATE_SYNC] ticker={selected_ticker} period={_period_type_db} "
                  f"date_sync={_date_sync} dr_now={_dr_now} dr_lc={_dr_lc} "
                  f"sw_val={st.session_state.get(_sw_key)!r} sw_in_opts={_sw_in_opts} "
                  f"start_idx={start_idx} end_idx={end_idx} n_opts={len(date_options)} "
                  f"start_date={start_date} end_date={end_date}")
        _sort_sync  = (st.session_state.get(sort_order_key)          != st.session_state.get(_LC_SORT)
                       or _so_key not in st.session_state)
        _period_sync = (st.session_state.get(_active_period_type_key) != st.session_state.get(_LC_PERIOD)
                       or _pt_key not in st.session_state)
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
            _new_sw = date_options[start_idx] if date_options else None
            _new_ew = date_options[end_idx]   if date_options else None
            log_info(f"[DATE_SYNC] WIDGET_UPDATE sw={_new_sw!r} ew={_new_ew!r}")
            st.session_state[_sw_key] = _new_sw
            st.session_state[_ew_key] = _new_ew
            # Keep authoritative date key in sync with what the widgets now show
            if _new_sw and _new_ew and _new_sw in date_values and _new_ew in date_values:
                st.session_state[_active_date_key] = (
                    date_values[_new_sw].isoformat(),
                    date_values[_new_ew].isoformat()
                )
        if _sort_sync:
            st.session_state[_so_key] = st.session_state[sort_order_key]
        if _period_sync:
            st.session_state[_pt_key] = st.session_state.get(_active_period_type_key, "Annual")
        if _conv_sync:
            st.session_state["conversion_mode_select"] = st.session_state.conversion_mode
        if _from_sync:
            st.session_state["currency_from_unified"]  = st.session_state.from_currency
        if _to_sync:
            st.session_state["currency_to_unified"]    = st.session_state.target_currency
        if _units_sync:
            st.session_state["units_select"]           = st.session_state.units

        # Commit "last confirmed" snapshot — used on next render to detect changes
        st.session_state[_LC_DATE]  = st.session_state.get(_active_date_key)
        st.session_state[_LC_SORT]  = st.session_state.get(sort_order_key)
        st.session_state[_LC_PERIOD] = st.session_state.get(_active_period_type_key, "Annual")
        st.session_state[_LC_CONV]  = st.session_state.get("conversion_mode")
        st.session_state[_LC_FROM]  = st.session_state.get("from_currency")
        st.session_state[_LC_TO]    = st.session_state.get("target_currency")
        st.session_state[_LC_UNITS] = st.session_state.get("units")

        # ── on_change callbacks — update session_state before the script reruns,
        #    so the single widget-triggered rerun fetches/renders with the new value.
        #    This eliminates the second st.rerun() that was previously needed.
        def _on_period_type_change():
            try:
                _new_pt = st.session_state.get(_pt_key)
                log_info(f"[FILTER_CB] period_type_change tab={selected_tab} ticker={selected_ticker} new_period={_new_pt}")
                st.session_state[_active_period_type_key] = _new_pt
                if st.query_params.get("period_type") != st.session_state[_active_period_type_key]:
                    st.query_params["period_type"] = st.session_state[_active_period_type_key]
                # Clear cached HTML so it rebuilds with new period type
                for _k in list(st.session_state.keys()):
                    if _k.startswith("_est_html_") or _k.startswith("_fcst_html_"):
                        del st.session_state[_k]
                save_market_data_state()
                log_info(f"[FILTER_CB] period_type_change DONE tab={selected_tab}")
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_on_period_type_change")

        def _on_conversion_mode_change():
            try:
                st.session_state.conversion_mode = st.session_state["conversion_mode_select"]
                if st.query_params.get("conv_mode") != st.session_state.conversion_mode:
                    st.query_params["conv_mode"] = st.session_state.conversion_mode
                save_market_data_state()
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_on_conversion_mode_change")

        def _clear_est_fcst_cache():
            for _k in list(st.session_state.keys()):
                if _k.startswith("_est_html_") or _k.startswith("_fcst_html_"):
                    del st.session_state[_k]

        def _on_from_currency_change():
            try:
                log_info(f"[FILTER_CB] from_currency_change tab={selected_tab} ticker={selected_ticker}")
                st.session_state.from_currency = st.session_state["currency_from_unified"]
                if selected_tab in ("estimates", "forecasting"):
                    _clear_est_fcst_cache()
                save_market_data_state()
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_on_from_currency_change")

        def _on_to_currency_change():
            try:
                log_info(f"[FILTER_CB] to_currency_change tab={selected_tab} ticker={selected_ticker}")
                st.session_state.target_currency = st.session_state["currency_to_unified"]
                if st.query_params.get("to_curr") != st.session_state.target_currency:
                    st.query_params["to_curr"] = st.session_state.target_currency
                if selected_tab in ("estimates", "forecasting"):
                    _clear_est_fcst_cache()
                save_market_data_state()
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_on_to_currency_change")

        def _on_units_change():
            try:
                log_info(f"[FILTER_CB] units_change tab={selected_tab} ticker={selected_ticker}")
                st.session_state.units = st.session_state["units_select"]
                if st.query_params.get("units_sel") != st.session_state.units:
                    st.query_params["units_sel"] = st.session_state.units
                if selected_tab in ("estimates", "forecasting"):
                    _clear_est_fcst_cache()
                save_market_data_state()
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_on_units_change")

        def _on_date_sort_change():
            try:
                _new_start = date_values.get(st.session_state.get(_sw_key), start_date)
                _new_end   = date_values.get(st.session_state.get(_ew_key), end_date)
                _new_sort  = st.session_state.get(_so_key, st.session_state.get(sort_order_key))
                log_info(f"[FILTER_CB] date_sort_change tab={selected_tab} ticker={selected_ticker} new_start={_new_start} new_end={_new_end} new_sort={_new_sort}")
                if _new_start and _new_end and _new_start > _new_end:
                    return  # Invalid range — leave state unchanged; error shown below
                if _new_start and _new_end:
                    # Estimates / Forecasting: save to isolated key only, never touch shared key or query params
                    st.session_state[_active_date_key] = (_new_start.isoformat(), _new_end.isoformat())
                    if selected_tab not in _EST_FCST_TABS:
                        _s = _new_start.isoformat()
                        _e = _new_end.isoformat()
                        if st.query_params.get("start_dt") != _s:
                            st.query_params["start_dt"] = _s
                        if st.query_params.get("end_dt") != _e:
                            st.query_params["end_dt"] = _e
                if _new_sort:
                    st.session_state[sort_order_key] = _new_sort
                    if st.query_params.get("sort_ord") != _new_sort:
                        st.query_params["sort_ord"] = _new_sort
                if selected_tab in _EST_FCST_TABS:
                    _clear_est_fcst_cache()
                    return  # Don't persist to localStorage for estimates/forecasting
                save_market_data_state()
            except Exception as exc:
                log_structured_error(exc, page="market_data", component="render_page", operation="_on_date_sort_change")

        # Ratios & Ratings tabs: hide Conversion/Currency/Units filters (text-based, not currency-convertible)
        _hide_currency_filters = (selected_tab in ("ratios", "ratings"))
        # Ratings: always Annual, hide Period Type filter.
        # Forecasting & Segment: show Period Type only when quarterly data is
        # available (checked via _available_period_types → single disabled option
        # when Annual-only, Annual/Quarterly dropdown when quarterly forecasts exist).
        _hide_period_type = (selected_tab in ("ratings",))

        if _hide_currency_filters:
            if _hide_period_type:
                f_space, f1, f2, f3 = st.columns([4, 1.5, 1.5, 1.3], gap="small", vertical_alignment="center", width="stretch")
                f_period = f4 = f5 = f_arrow = f6 = f7 = None
            else:
                f_space ,f_period, f1, f2, f3 = st.columns([4, 1.2, 1.5, 1.5, 1.3], gap="small", vertical_alignment="center", width="stretch")
                f4 = f5 = f_arrow = f6 = f7 = None
        else:
            if _hide_period_type:
                f1, f2, f3, f4, f5, f_arrow, f6, f7 = st.columns([1.4, 1.4, 1.1, 1.5, 1.0, 0.1, 1.0, 1.5], gap="small", vertical_alignment="center", width="stretch")
                f_period = None
            else:
                f_period, f1, f2, f3, f4, f5, f_arrow, f6, f7 = st.columns([1.1, 1.4, 1.4, 1.1, 1.5, 1.0, 0.1, 1.0, 1.5], gap="small", vertical_alignment="center", width="stretch")

        # Available period types computed earlier (availability-aware effective
        # period type) — reuse to avoid a duplicate DB round-trip.

        if f_period is not None:
            with f_period:
                st.html('<div class="filter-label">Period Type</div>')
                _pt_key = "period_type_select_shared"
                # The shared selection may be e.g. Quarterly while this tab only has
                # Annual data — snap the widget's stored value into the available
                # options so Streamlit doesn't raise and the box shows the real type.
                if st.session_state.get(_pt_key) not in _available_period_types:
                    st.session_state[_pt_key] = _period_type_eff_title
                # If only one period type available, show it disabled
                if len(_available_period_types) == 1:
                    st.selectbox(
                        "Period Type",
                        options=_available_period_types,
                        label_visibility="collapsed",
                        key=_pt_key,
                        disabled=True
                    )
                    new_period_type = _available_period_types[0]
                else:
                    new_period_type = st.selectbox(
                        "Period Type",
                        options=_available_period_types,
                        label_visibility="collapsed",
                        key=_pt_key,
                        on_change=_on_period_type_change,
                    )
                # Validate against available types
                if new_period_type not in _available_period_types:
                    new_period_type = _available_period_types[0] if _available_period_types else "Annual"
        else:
            new_period_type = "Annual"

        with f1:
            st.html('<div class="filter-label">Start Date</div>')
            new_start_label = st.selectbox(
                "Start",
                options=date_options,
                label_visibility="collapsed",
                key=_sw_key,
                on_change=_on_date_sort_change,
            )
            new_start_date = date_values.get(new_start_label, start_date)

        with f2:
            st.html('<div class="filter-label">End Date</div>')
            new_end_label = st.selectbox(
                "End",
                options=date_options,
                label_visibility="collapsed",
                key=_ew_key,
                on_change=_on_date_sort_change,
            )
            new_end_date = date_values.get(new_end_label, end_date)

        with f3:
            st.html('<div class="filter-label">Sort</div>')
            new_sort_order = st.selectbox(
                "Sort",
                options=["Earliest", "Latest"],
                label_visibility="collapsed",
                key=_so_key,
                on_change=_on_date_sort_change,
            )

        if not _hide_currency_filters:
            with f4:
                st.html('<div class="filter-label">Conversion</div>')
                conversion_modes = ["Today's Spot Rate", "Historical"]
                new_conversion_mode = st.selectbox(
                    "Conversion",
                    options=conversion_modes,
                    label_visibility="collapsed",
                    key="conversion_mode_select",
                    on_change=_on_conversion_mode_change,
                )

        # Safety: ensure from_currency is always set
        if 'from_currency' not in st.session_state:
            st.session_state.from_currency = reported_currency

        _fc = st.session_state.get("from_currency", _early_reported_currency)
        _tc = st.session_state.get("target_currency", _early_reported_currency)

        # SIMPLE LOGIC:
        # 1. "Currency" (From): Always show only the reported_currency
        _from_options = [_early_reported_currency] if _early_reported_currency else ["USD"]

        # 2. "To Currency": Get from forex table where from_currency = reported_currency
        # Include reported_currency as first option (1:1 conversion), then other currencies
        _t_toc = _time.perf_counter()
        _forex_currencies = ForexRepository.get_to_currencies(_early_reported_currency) if _early_reported_currency else []
        _toc_ms = (_time.perf_counter() - _t_toc) * 1000
        if _toc_ms > 300:
            # Dominant cost inside filter_area_render (the currency dropdown).
            # On balance_sheet this alone was ~1.3s on STG. Aliased import — see
            # PAGE_available_period_types note above (log_timing is a local here).
            from utils.server_logger import log_timing as _toc_log
            _toc_log("PAGE_forex_to_currencies", _toc_ms,
                     details=f"tab={selected_tab} from={_early_reported_currency} n={len(_forex_currencies)}")
        # Build list: reported_currency first, then other valid targets
        _to_options = [_early_reported_currency] if _early_reported_currency else ["USD"]
        # Add other currencies (excluding reported_currency to avoid duplicate)
        for c in _forex_currencies:
            if c != _early_reported_currency and c not in _to_options:
                _to_options.append(c)

        if not _hide_currency_filters:
            with f5:
                st.html('<div class="filter-label">Currency</div>')
                new_from = st.selectbox(
                    "From Currency",
                    options=_from_options,
                    label_visibility="collapsed",
                    key="currency_from_unified",
                    on_change=_on_from_currency_change,
                )

            with f_arrow:
                st.html('<div class="filter-label">&nbsp;</div>')
                st.html('<div style="display:flex;align-items:center;justify-content:center;height:36px;font-size:16px;color:#4F4F4F;padding-top:4px;">➜</div>')

            with f6:
                st.html('<div class="filter-label">To Currency</div>')
                target = st.selectbox(
                    "To Currency",
                    options=_to_options,
                    label_visibility="collapsed",
                    key="currency_to_unified",
                    on_change=_on_to_currency_change,
                )
            with f7:
                st.html('<div class="filter-label">Units</div>')
                unit_options = ["Millions (mm)", "Billions (bn)", "Thousands (k)"]
                new_units = st.selectbox(
                    "Units",
                    options=unit_options,
                    label_visibility="collapsed",
                    key="units_select",
                    on_change=_on_units_change,
                )

        # Validate date range (callbacks skip invalid ranges; show error here for UX)
        if new_start_date and new_end_date and new_start_date > new_end_date:
            st.error("Start date must be before end date")

    _timings['filter_area_render'] = (_time.perf_counter() - _t_filter_start) * 1000

    # ==================== TABLE WITH CURRENCY CONVERSION ====================
    # Apply sort order to data
    sort_ascending = st.session_state[sort_order_key] == "Earliest"

    # Compute units scale factor (data is stored in millions)
    _units_map = {"Millions (mm)": 1.0, "Billions (bn)": 0.001, "Thousands (k)": 1000.0}
    units_scale = _units_map.get(st.session_state.get("units", "Millions (mm)"), 1.0)
    _units_label_map = {"Millions (mm)": "Millions", "Billions (bn)": "Billions", "Thousands (k)": "Thousands"}
    units_label = _units_label_map.get(st.session_state.get("units", "Millions (mm)"), "Millions")

    if selected_tab == "company_profile":
        _t0_tab = _time.perf_counter()

        # ── Info table ──
        _t1_tab = _time.perf_counter()
        st.markdown('<div class="company-profile-container">', unsafe_allow_html=True)
        st.markdown(render_info_table(company), unsafe_allow_html=True)
        _t_info = (_time.perf_counter() - _t1_tab) * 1000

        # ── Business description ──
        _t2_tab = _time.perf_counter()
        st.markdown(render_business_description(company.company_description), unsafe_allow_html=True)
        _t_desc = (_time.perf_counter() - _t2_tab) * 1000

        # ── Executive Compensation ──
        _comp_rows_for_xl = None
        _comp_col_defs_for_xl = None
        try:
            from data.repository import ExecutiveCompensationRepository as _ECR
            from data.source_router import get_company_source as _gcs_cp

            _cp_source = _gcs_cp(selected_ticker)
            _is_sec_cp = (_cp_source != "YFinance")

            if _is_sec_cp:
                _comp_rows = _ECR.get_sec_latest_compensation(selected_ticker)
            else:
                _comp_rows = _ECR.get_yf_latest_compensation(selected_ticker)

            if _comp_rows:
                # Determine money keys to check for row filtering
                if _is_sec_cp:
                    _money_keys = ["salary", "bonus", "stock_awards", "total_compensation"]
                else:
                    _money_keys = ["total_pay"]

                # Filter out entire rows where ALL money values are None/empty
                _comp_rows = [
                    r for r in _comp_rows
                    if any(r.get(k) not in (None, "") for k in _money_keys)
                ]

                # Determine which columns actually have data (dynamic hiding)
                if _is_sec_cp:
                    _col_defs = [
                        ("Executive Name",    "executive_name"),
                        ("Position",          "position"),
                        ("Year",              "compensation_year"),
                        ("Salary",            "salary"),
                        ("Bonus",             "bonus"),
                        ("Stock Awards",      "stock_awards"),
                        ("Total Compensation","total_compensation"),
                    ]
                else:
                    _col_defs = [
                        ("Executive Name",    "executive_name"),
                        ("Position",          "position"),
                        ("Year",              "compensation_year"),
                        ("Total Pay",         "total_pay"),
                    ]

                # Drop money columns where every (remaining) row is None/empty
                _money_labels = {"Salary", "Bonus", "Stock Awards", "Total Compensation", "Total Pay"}
                _visible_cols = [
                    (label, key) for label, key in _col_defs
                    if label not in _money_labels  # always keep text cols
                    or any(r.get(key) not in (None, "") for r in _comp_rows)
                ]

                if _visible_cols:
                    _comp_rows_for_xl = _comp_rows
                    _comp_col_defs_for_xl = _visible_cols
                    _comp_year = _comp_rows[0].get("compensation_year", "") or ""
                    _comp_html = f"""
<div class="comp-section">
  <div class="comp-section-header">
    <span class="comp-section-title">Executive Compensation</span>
    {"<span class='comp-year-badge'>FY " + _comp_year + "</span>" if _comp_year else ""}
  </div>
  <div class="comp-table-wrap">
    <table class="comp-table">
      <thead>
        <tr>
"""
                    for label, _ in _visible_cols:
                        _align = "right" if label not in ("Executive Name", "Position") else "left"
                        _comp_html += f'          <th style="text-align:{_align}">{label}</th>\n'
                    _comp_html += "        </tr>\n      </thead>\n      <tbody>\n"

                    for _idx, row in enumerate(_comp_rows):
                        _row_cls = "comp-row-even" if _idx % 2 == 0 else "comp-row-odd"
                        _comp_html += f'        <tr class="{_row_cls}">\n'
                        for label, key in _visible_cols:
                            val = row.get(key)
                            # For non-money cols show empty string; for money cols
                            # the row-filter above guarantees at least one is non-None.
                            if val in (None, ""):
                                _disp = "" if label in ("Executive Name", "Position", "Year") else "—"
                            else:
                                _disp = val
                            _align = "right" if label not in ("Executive Name", "Position") else "left"
                            _cls   = "comp-td-money" if label not in ("Executive Name", "Position", "Year") else ""
                            _comp_html += f'          <td class="{_cls}" style="text-align:{_align}">{_disp}</td>\n'
                        _comp_html += "        </tr>\n"

                    _comp_html += "      </tbody>\n    </table>\n  </div>\n</div>"

                    _comp_css = """
<style>
.comp-section {
  margin: 20px 0 10px 0;
}
.comp-section-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.comp-section-title {
  font-size: 1rem;
  font-weight: 700;
  color: #212529;
  letter-spacing: 0.01em;
}
.comp-year-badge {
  display: inline-block;
  background: #fff3cd;
  color: #856404;
  font-size: 0.72rem;
  font-weight: 700;
  border-radius: 10px;
  padding: 2px 10px;
  border: 1px solid #ffc107;
}
.comp-table-wrap {
  overflow-x: auto;
  border-radius: 8px;
  border: 1px solid #e9ecef;
  box-shadow: 0 1px 4px rgba(0,0,0,0.05);
}
.comp-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
  background: #fff;
}
.comp-table thead tr {
  background: #f8f9fa;
  border-bottom: 2px solid #dee2e6;
}
.comp-table thead th {
  padding: 10px 14px;
  font-weight: 700;
  font-size: 0.78rem;
  color: #495057;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  white-space: nowrap;
}
.comp-table tbody tr {
  border-bottom: 1px solid #f1f3f4;
  transition: background 0.12s;
}
.comp-table tbody tr:hover {
  background: #f8f9fa !important;
}
.comp-row-even { background: #ffffff; }
.comp-row-odd  { background: #fafbfc; }
.comp-table td {
  padding: 9px 14px;
  color: #343a40;
  font-size: 0.84rem;
}
.comp-td-money {
  font-variant-numeric: tabular-nums;
  color: #1a7a4a;
  font-weight: 600;
}
</style>
"""
                    st.markdown(_comp_css + _comp_html, unsafe_allow_html=True)
        except Exception as _comp_exc:
            log_structured_error(_comp_exc, page="market_data", component="render_page",
                                 operation="render_executive_compensation")

        # ── Excel Download (Simple 2-tab version like Key Stats) ──
        _sq_currency = company.currency or "USD"

        try:
            from utils.excel_export import export_company_profile_simple_excel
            from data.repository import StockQuoteRepository

            # Collect early-submitted prefetch futures (submitted after company fetch).
            # Futures have been running during filter init + CSS + toolbar rendering.
            _t_prefetch = _time.perf_counter()

            if _pf_history_future is not None:
                _price_history = _pf_history_future.result()
                _pf_quote_future.result()  # cache priming only
                if _pf_executor:
                    _pf_executor.shutdown(wait=False)
            else:
                # Fallback: blocking prefetch (shouldn't happen on company_profile)
                from data.repository import StockQuoteRepository as _SQR_fb
                def _fb_hist():
                    _set_md_thread_ctx()
                    return _SQR_fb.get_price_history(selected_ticker, days=1825)  # 60 months
                def _fb_qt():
                    _set_md_thread_ctx()
                    return _SQR_fb.get_latest_quote(selected_ticker)
                with ThreadPoolExecutor(max_workers=2) as _fb_exec:
                    _fh = _fb_exec.submit(_fb_hist)
                    _fq = _fb_exec.submit(_fb_qt)
                    _price_history = _fh.result()
                    _fq.result()

            _prefetch_ms = (_time.perf_counter() - _t_prefetch) * 1000
            _early_overlap = (_time.perf_counter() - _pf_submit_t) * 1000 if _pf_submit_t else 0

            # Fetch shares + price data (exact date match) for Market Cap sheet
            # Inlined here (not via repository) so Streamlit hot-reload always picks it up
            _shares_price_data = None
            try:
                from data.source_router import get_company_source as _gcs
                from core.database import DatabaseManager as _DBM
                _db = _DBM()
                _src = _gcs(selected_ticker) or "SEC"
                _is_sec = (_src != "YFinance")

                def _ff(v):
                    if v is None: return None
                    try: return float(v)
                    except Exception: return None

                if _is_sec:
                    _sp_rows_raw = _db.execute_query_readonly("""
                        SELECT s.report_date, s.shares_outstanding_basic, s.shares_outstanding_diluted, ts.close
                        FROM coreiq_av_shares_outstanding s
                        INNER JOIN coreiq_av_time_series_daily ts
                            ON ts.ticker = s.ticker AND ts.day_date = s.report_date
                        WHERE s.ticker = :ticker
                        ORDER BY s.report_date DESC
                    """, {"ticker": selected_ticker})
                    _sp_rows = []
                    for _r in _sp_rows_raw:
                        _c = _ff(_r.get("close")); _b = _ff(_r.get("shares_outstanding_basic")); _d = _ff(_r.get("shares_outstanding_diluted"))
                        _sp_rows.append({"date": str(_r["report_date"]), "close": _c, "shares_basic": _b, "shares_diluted": _d,
                                         "mktcap_basic": (_c * _b) if _c and _b else None,
                                         "mktcap_diluted": (_c * _d) if _c and _d else None})
                else:
                    _sp_rows_raw = _db.execute_query_readonly("""
                        SELECT s.asof_date, s.shares_outstanding, ts.close
                        FROM coreiq_yf_shares_outstanding s
                        INNER JOIN coreiq_yf_time_series_daily ts
                            ON ts.ticker = s.ticker AND ts.day_date = s.asof_date
                        WHERE s.ticker = :ticker
                        ORDER BY s.asof_date DESC
                    """, {"ticker": selected_ticker})
                    _sp_rows = []
                    for _r in _sp_rows_raw:
                        _c = _ff(_r.get("close")); _sh = _ff(_r.get("shares_outstanding"))
                        _sp_rows.append({"date": str(_r["asof_date"]), "close": _c, "shares": _sh,
                                         "mktcap": (_c * _sh) if _c and _sh else None})

                if _sp_rows:
                    _shares_price_data = {"is_sec": _is_sec, "rows": _sp_rows}
            except Exception as _sp_e:
                log_structured_error(_sp_e, page="market_data", component="render_page", operation="get_shares_with_price")

            if _price_history:
                _lazy_excel_download(
                    f"cp_{selected_ticker}",
                    f"{selected_ticker}_Company_Profile.xlsx",
                    lambda: export_company_profile_simple_excel(
                        company_name=company.name or "",
                        ticker=selected_ticker,
                        profile_rows=get_profile_rows_for_excel(company),
                        price_history=_price_history,  # 60 months of data
                        currency=_sq_currency,
                        shares_price_data=_shares_price_data,
                        compensation_rows=_comp_rows_for_xl,
                        compensation_col_defs=_comp_col_defs_for_xl,
                    ),
                )
        except Exception as _xl_e:
            log_structured_error(_xl_e, page="market_data", component="render_page", operation="EXCEL_DOWNLOAD_BALANCE")

        st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)

        # ── Stock Quote and Chart ──
        _t_stock_start = _time.perf_counter()
        # Use company overview currency (from database) - NO FALLBACK to session state
        _stock_quote_currency = company.currency or "N/A"
        try:
            render_stock_quote(selected_ticker, currency=_stock_quote_currency, company_overview=company)
        except Exception as _sq_exc:
            log_structured_error(_sq_exc, page="market_data", component="render_page", operation="render_stock_quote")
            st.error("Could not load stock quote. Please try again.")
        _t_stock = (_time.perf_counter() - _t_stock_start) * 1000
        st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)

        # ── For Reference expander ──
        _t_ref = _time.perf_counter()
        with st.expander("**For Reference**", expanded=False):
            st.markdown(render_reference_table(company), unsafe_allow_html=True)
        _ref_time = (_time.perf_counter() - _t_ref) * 1000
        st.markdown('</div>', unsafe_allow_html=True)

        # Total Company Profile timing (WITHOUT Excel - which is background)
        _total_tab = (_time.perf_counter() - _t0_tab) * 1000
        _timings['cp_info_table'] = _t_info
        _timings['cp_description'] = _t_desc
        _timings['cp_stock_quote'] = _t_stock
        _timings['cp_reference'] = _ref_time
        _timings['cp_prefetch'] = _prefetch_ms
        _timings['cp_total'] = _total_tab
    elif not start_date or not end_date:
        pass  # No data available message already shown above
    elif selected_tab == "balance_sheet":
        _t0_tab = _time.perf_counter()
        _t0 = _time.perf_counter()
        _period_type_db = _period_type_eff_title.lower()
        _render_period_fallback_notice()
        render_balance_sheet(selected_ticker, start_date, end_date, conversion_rate, reported_currency, sort_ascending, historical_rate_map, units_scale, units_label, _period_type_db)
        _timings['balance_sheet_render'] = (_time.perf_counter() - _t0) * 1000
        # Excel download (built lazily — only when the user clicks Excel)
        _t_xl_bs = _time.perf_counter()
        def _build_bs_xl():
            from utils.excel_export import export_financial_excel
            _bs_data = BalanceSheetRepository.get_balance_sheet_data(selected_ticker, start_date, end_date, _period_type_db)
            if not sort_ascending:
                _bs_data.periods = list(reversed(_bs_data.periods))
                for _it in _bs_data.line_items:
                    _it.values = list(reversed(_it.values))
            if not (_bs_data.periods and _bs_data.line_items):
                return None
            _bs_rows = []
            for _it in _bs_data.line_items:
                _rates = [historical_rate_map.get(_bs_data.periods[ci].date, conversion_rate) if historical_rate_map else conversion_rate for ci in range(len(_bs_data.periods))]
                _bs_rows.append({"label": _it.label.strip(), "values": [v * _rates[ci] * units_scale if v is not None else None for ci, v in enumerate(_it.values)], "is_bold": is_balance_sheet_bold_row(_it.label), "indent": get_balance_sheet_indent_level(_it.label), "is_percent": False, "is_text": False, "has_separator": has_balance_sheet_grey_separator(_it.label), "is_estimated": [False] * len(_it.values)})
            return export_financial_excel("Balance Sheet", company.name or "", selected_ticker, [p.label for p in _bs_data.periods], _bs_rows, units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
        _lazy_excel_download(f"bs_{selected_ticker}", f"{selected_ticker}_Balance_Sheet.xlsx", _build_bs_xl)
        _timings['balance_sheet_excel'] = (_time.perf_counter() - _t_xl_bs) * 1000
        _timings['balance_sheet_total'] = (_time.perf_counter() - _t0_tab) * 1000
    elif selected_tab == "cash_flow":
        _t0_tab = _time.perf_counter()
        _t0 = _time.perf_counter()
        _period_type_db = _period_type_eff_title.lower()
        _render_period_fallback_notice()
        render_cash_flow(selected_ticker, start_date, end_date, conversion_rate, reported_currency, sort_ascending, historical_rate_map, units_scale, units_label, _period_type_db)
        _timings['cash_flow_render'] = (_time.perf_counter() - _t0) * 1000
        # Excel download (built lazily — only when the user clicks Excel)
        _t_xl_cf = _time.perf_counter()
        def _build_cf_xl():
            from utils.excel_export import export_financial_excel
            from data.repository import CashFlowRepository
            _cf_data = CashFlowRepository.get_cash_flow_data(selected_ticker, start_date, end_date, _period_type_db)
            if not sort_ascending:
                _cf_data.periods = list(reversed(_cf_data.periods))
                for _it in _cf_data.line_items:
                    _it.values = list(reversed(_it.values))
            if not (_cf_data.periods and _cf_data.line_items):
                return None
            _cf_rows = []
            for _it in _cf_data.line_items:
                _rates = [historical_rate_map.get(_cf_data.periods[ci].date, conversion_rate) if historical_rate_map else conversion_rate for ci in range(len(_cf_data.periods))]
                _cf_rows.append({"label": _it.label.strip(), "values": [v * _rates[ci] * units_scale if v is not None else None for ci, v in enumerate(_it.values)], "is_bold": is_cash_flow_bold_row(_it.label), "indent": get_cash_flow_indent_level(_it.label), "is_percent": False, "is_text": False, "has_separator": has_cash_flow_grey_separator(_it.label), "is_estimated": [False] * len(_it.values)})
            return export_financial_excel("Cash Flow", company.name or "", selected_ticker, [p.label for p in _cf_data.periods], _cf_rows, units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
        _lazy_excel_download(f"cf_{selected_ticker}", f"{selected_ticker}_Cash_Flow.xlsx", _build_cf_xl)
        _timings['cash_flow_excel'] = (_time.perf_counter() - _t_xl_cf) * 1000
        _timings['cash_flow_total'] = (_time.perf_counter() - _t0_tab) * 1000
    elif selected_tab == "income_statement":
        _t0_tab = _time.perf_counter()
        try:
            _period_type_db = _period_type_eff_title.lower()
            _render_period_fallback_notice()
            # Session-state short-circuit — reuse cached HTML when inputs unchanged
            _conv_mode = st.session_state.get('conversion_mode', 'spot')
            _is_ck = f"_is_html_{selected_ticker}_{start_date}_{end_date}_{_period_type_db}_{sort_ascending}_{conversion_rate:.6f}_{units_scale}_{_conv_mode}"
            def _build_is_xl():
                from utils.excel_export import export_financial_excel as _xl_fn
                _d = IncomeStatementRepository.get_income_statement_data(selected_ticker, start_date, end_date, _period_type_db)
                if not sort_ascending:
                    _d.periods = list(reversed(_d.periods))
                    for _it in _d.line_items:
                        _it.values = list(reversed(_it.values))
                if not (_d.periods and _d.line_items):
                    return None
                return _xl_fn("Income Statement", company.name or "", selected_ticker, [p.label for p in _d.periods], _income_excel_rows(_d, historical_rate_map, conversion_rate, units_scale), units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
            _is_cached = st.session_state.get(_is_ck)
            if _is_cached is not None:
                _timings['income_data_fetch'] = 0.0
                st.html(_is_cached)
                _lazy_excel_download(f"is_{selected_ticker}", f"{selected_ticker}_Income_Statement.xlsx", _build_is_xl)
            else:
                _t0 = _time.perf_counter()
                data = IncomeStatementRepository.get_income_statement_data(
                    selected_ticker, start_date, end_date, _period_type_db
                )
                _timings['income_data_fetch'] = (_time.perf_counter() - _t0) * 1000

                # Apply sorting based on user selection
                if not sort_ascending:
                    # Reverse the periods and corresponding values
                    data.periods = list(reversed(data.periods))
                    for item in data.line_items:
                        item.values = list(reversed(item.values))

                if data.periods and data.line_items:
                    _t_html = _time.perf_counter()
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
                    _timings['income_html_build'] = (_time.perf_counter() - _t_html) * 1000
                    st.html(html)
                    st.session_state[_is_ck] = html

                    # Excel download (built lazily — only when the user clicks Excel)
                    _t_xl_is = _time.perf_counter()
                    _lazy_excel_download(f"is_{selected_ticker}", f"{selected_ticker}_Income_Statement.xlsx", _build_is_xl)
                    _timings['income_excel'] = (_time.perf_counter() - _t_xl_is) * 1000

                else:
                    st.info("No data available")

        except Exception as e:
            log_structured_error(e, page="market_data", component="render_page", operation="render_income_statement")
            st.error("Something went wrong. Please try again.")
        _timings['income_total'] = (_time.perf_counter() - _t0_tab) * 1000
    elif selected_tab == "key_stats":
        _t0_tab = _time.perf_counter()
        try:
            _period_type_db = _period_type_eff_title.lower()
            _render_period_fallback_notice()
            # Session-state short-circuit — reuse cached HTML when inputs unchanged
            _conv_mode = st.session_state.get('conversion_mode', 'spot')
            _ks_ck = f"_ks_html_v2_{selected_ticker}_{start_date}_{end_date}_{_period_type_db}_{sort_ascending}_{conversion_rate:.6f}_{units_scale}_{_conv_mode}"
            def _build_ks_xl():
                from utils.excel_export import export_financial_excel as _xl_fn
                _d = KeyStatsRepository.get_key_stats_data(selected_ticker, start_date, end_date, _period_type_db)
                if not sort_ascending:
                    _d["periods"] = list(reversed(_d["periods"]))
                    for _it in _d["line_items"]:
                        _it["values"] = list(reversed(_it["values"]))
                if not (_d["periods"] and _d["line_items"]):
                    return None
                return _xl_fn("Key Stats", company.name or "", selected_ticker, [p.label for p in _d["periods"]], _key_stats_excel_rows(_d, historical_rate_map, conversion_rate, units_scale), units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
            _ks_cached = st.session_state.get(_ks_ck)
            if _ks_cached is not None:
                _timings['key_stats_fetch'] = 0.0
                st.html(_ks_cached)
                _lazy_excel_download(f"ks_{selected_ticker}", f"{selected_ticker}_Key_Stats.xlsx", _build_ks_xl)
            else:
                _t0 = _time.perf_counter()
                log_info(
                    f"[MD_PHASE] rerun={get_rerun_id()} phase=key_stats_fetch_start "
                    f"ticker={selected_ticker} period={_period_type_db} start={start_date} end={end_date}"
                )
                data = KeyStatsRepository.get_key_stats_data(selected_ticker, start_date, end_date, _period_type_db)
                _timings['key_stats_fetch'] = (_time.perf_counter() - _t0) * 1000
                log_info(
                    f"[MD_PHASE] rerun={get_rerun_id()} phase=key_stats_fetch_done "
                    f"ms={_timings['key_stats_fetch']:.1f} n_periods={len(data.get('periods', []))}"
                )

                # Apply sorting based on user selection
                if not sort_ascending:
                    data["periods"] = list(reversed(data["periods"]))
                    for item in data["line_items"]:
                        item["values"] = list(reversed(item["values"]))

                if data["periods"] and data["line_items"]:
                    _t_html = _time.perf_counter()
                    ks_css = """<style>
                    .actual-badge  { font-size:11px; font-weight:800; color:#666; vertical-align:super; margin-left:2px; }
                    th.actual-col  { background:rgba(100,100,100,0.07)!important; border-left:2px solid rgba(100,100,100,0.15)!important; }
                    td.actual-cell { background:rgba(100,100,100,0.03); border-left:2px solid rgba(100,100,100,0.10); }
                    .estimate-badge { font-size:11px; font-weight:800; color:#0055BB; vertical-align:super; margin-left:2px; }
                    th.est-col  { background:rgba(0,85,187,0.08)!important; border-left:2px solid rgba(0,85,187,0.25)!important; }
                    td.est-cell { background:rgba(0,85,187,0.04); border-left:2px solid rgba(0,85,187,0.12); color:#0055BB; }
                    td.est-cell-dash { background:rgba(0,85,187,0.04); border-left:2px solid rgba(0,85,187,0.12); color:#aaa; }
                    .forecast-badge { font-size:11px; font-weight:800; color:#1B6B24; vertical-align:super; margin-left:2px; }
                    th.fcst-col  { background:rgba(27,107,36,0.08)!important; border-left:2px solid rgba(27,107,36,0.25)!important; }
                    td.fcst-cell { background:rgba(27,107,36,0.04); border-left:2px solid rgba(27,107,36,0.12); color:#1B6B24; }
                    td.fcst-cell-dash { background:rgba(27,107,36,0.04); border-left:2px solid rgba(27,107,36,0.12); color:#aaa; }
                    </style>"""
                    html = ks_css + '<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>'

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

                        if getattr(period, 'is_forecast', False):
                            html += (
                                f'<th class="data-col fcst-col">'
                                f'<span class="period-label">{period_text}</span>'
                                f'<span class="period-date">{date_text}'
                                f'<sup class="forecast-badge">F</sup></span></th>'
                            )
                        elif period.is_estimated:
                            html += (
                                f'<th class="data-col est-col">'
                                f'<span class="period-label">{period_text}</span>'
                                f'<span class="period-date">{date_text}'
                                f'<sup class="estimate-badge">E</sup></span></th>'
                            )
                        else:
                            html += (
                                f'<th class="data-col actual-col">'
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
                            _period = data["periods"][col_idx] if col_idx < len(data["periods"]) else None
                            col_is_forecast = _period is not None and getattr(_period, 'is_forecast', False)
                            col_is_estimated = _period is not None and _period.is_estimated

                            # Determine per-column rate (estimated/forecast cols use current rate)
                            if not col_is_estimated and not col_is_forecast and historical_rate_map and _period is not None:
                                col_rate = historical_rate_map.get(_period.date, conversion_rate)
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

                            if col_is_forecast:
                                cell_class = "data-cell fcst-cell" if val is not None else "data-cell fcst-cell-dash"
                            elif col_is_estimated:
                                cell_class = "data-cell est-cell" if val is not None else "data-cell est-cell-dash"
                            else:
                                cell_class = "data-cell actual-cell"
                            html += f'<td class="{cell_class}">{formatted}</td>'

                        html += '</tr>'

                    html += '</tbody></table></div></div>'
                    _timings['key_stats_html_build'] = (_time.perf_counter() - _t_html) * 1000
                    st.html(html)
                    st.session_state[_ks_ck] = html

                    # Column type legend
                    _has_est  = any(p.is_estimated for p in data["periods"])
                    _has_fcst = any(getattr(p, 'is_forecast', False) for p in data["periods"])
                    _legend_parts = [
                        '<span style="background:rgba(100,100,100,0.09);border-radius:3px;padding:1px 5px;color:#555;font-weight:800;font-size:11px;">A</span>&nbsp;Actual'
                    ]
                    if _has_est:
                        _legend_parts.append(
                            '<span style="background:rgba(0,85,187,0.10);border-radius:3px;padding:1px 5px;color:#0055BB;font-weight:800;font-size:11px;">E</span>&nbsp;Estimated (Analyst Consensus)'
                        )
                    if _has_fcst:
                        _legend_parts.append(
                            '<span style="background:rgba(27,107,36,0.10);border-radius:3px;padding:1px 5px;color:#1B6B24;font-weight:800;font-size:11px;">F</span>&nbsp;Forecasted (Revenue Model)'
                        )
                    _legend_html = (
                        f'<p style="font-size:12px;color:#666;margin:6px 0 0 4px;line-height:1.8;">'
                        f'{"&nbsp;&nbsp;&nbsp;·&nbsp;&nbsp;&nbsp;".join(_legend_parts)}'
                        f'</p>'
                    )
                    if _has_fcst:
                        _legend_html += (
                            '<p style="font-size:11px;color:#1B6B24;background:rgba(27,107,36,0.05);'
                            'border-left:3px solid rgba(27,107,36,0.35);border-radius:0 4px 4px 0;'
                            'padding:5px 10px;margin:4px 0 0 4px;line-height:1.6;">'
                            '<strong>F — Ensemble model forecast</strong>: Revenue values are the blended output of the '
                            'top-3 backtested models (Moving Average Trend, Weighted Average Growth, CAGR). '
                            'Only Total Revenue and Growth are projected; other metrics are not modelled. '
                            f'<a href="/forecasting?ticker={selected_ticker}" target="_blank" rel="noopener noreferrer" '
                            'style="color:#1B6B24;font-weight:700;text-decoration:none;'
                            'border-bottom:1.5px solid rgba(27,107,36,0.4);'
                            'transition:border-color 0.15s;cursor:pointer;">'
                            'Show More ↗</a>'
                            '</p>'
                        )
                    st.html(_legend_html)

                    # Excel download (built lazily — only when the user clicks Excel)
                    _t_xl_ks = _time.perf_counter()
                    _lazy_excel_download(f"ks_{selected_ticker}", f"{selected_ticker}_Key_Stats.xlsx", _build_ks_xl)
                    _timings['key_stats_excel'] = (_time.perf_counter() - _t_xl_ks) * 1000

                else:
                    st.info("No key stats data available for the selected date range")


        except Exception as e:
            log_structured_error(e, page="market_data", component="render_page", operation="RENDER_KEY_STATS")
            st.error(f"Error loading key stats: {e}")
        _timings['key_stats_total'] = (_time.perf_counter() - _t0_tab) * 1000
    elif selected_tab == "ratios":
        _t0_tab = _time.perf_counter()
        try:
            _period_type_db = _period_type_eff_title.lower()
            _render_period_fallback_notice()

            def _build_ratios_xl():
                from utils.excel_export import export_financial_excel as _xl_fn
                _d = RatiosRepository.get_ratios_data(selected_ticker, start_date, end_date, _period_type_db)
                if not sort_ascending:
                    _d["periods"] = list(reversed(_d["periods"]))
                    for _it in _d["line_items"]:
                        _it["values"] = list(reversed(_it["values"]))
                if not (_d["periods"] and _d["line_items"]):
                    return None
                return _xl_fn("Ratios", company.name or "", selected_ticker, [p.label for p in _d["periods"]], _ratios_excel_rows(_d), "", st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))

            # Phase 5: Session-state short-circuit — reuse cached HTML when inputs unchanged
            _ratios_cache_key = f"_ratios_html_{selected_ticker}_{start_date}_{end_date}_{_period_type_db}_{sort_ascending}"
            _cached_html = st.session_state.get(_ratios_cache_key)
            _cached_notes = st.session_state.get(_ratios_cache_key + "_notes")

            if _cached_html is not None:
                _timings['ratios_fetch'] = 0.0
                _timings['ratios_compute'] = 0.0
                _t0_render = _time.perf_counter()
                st.html(_cached_html)
                if _cached_notes:
                    st.caption("Ratios assumptions: " + " | ".join(_cached_notes))
                _lazy_excel_download(f"ratios_{selected_ticker}", f"{selected_ticker}_Ratios.xlsx", _build_ratios_xl)
                _timings['ratios_render'] = (_time.perf_counter() - _t0_render) * 1000
                _timings['ratios_total'] = (_time.perf_counter() - _t0_tab) * 1000
            else:
                # Cold / first-render path
                _t0 = _time.perf_counter()
                data = RatiosRepository.get_ratios_data(selected_ticker, start_date, end_date, _period_type_db)
                _timings['ratios_fetch'] = (_time.perf_counter() - _t0) * 1000

                _t0_compute = _time.perf_counter()
                if not sort_ascending:
                    data["periods"] = list(reversed(data["periods"]))
                    for item in data["line_items"]:
                        item["values"] = list(reversed(item["values"]))
                _timings['ratios_compute'] = (_time.perf_counter() - _t0_compute) * 1000

                _t0_render = _time.perf_counter()
                if data["periods"] and data["line_items"]:
                    # Phase 6: Build HTML via list-append + join (avoid O(n²) concat)
                    _parts = ['<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>',
                              '<tr class="row-grey-separator"><th>For Fiscal Period Ending<span class="header-subtext">Ratios and percentages (no unit scaling or currency conversion).</span></th>']
                    for period in data["periods"]:
                        lines = period.label.split('\n')
                        if len(lines) >= 2:
                            period_text = lines[0]
                            date_text = lines[1]
                        else:
                            period_text = ""
                            date_text = period.label
                        _parts.append(f'<th class="data-col"><span class="period-label">{period_text}</span><span class="period-date">{date_text}</span></th>')
                    _parts.append('</tr></thead><tbody>')

                    for item in data["line_items"]:
                        label = item.get("label", "")
                        values = item.get("values", [])
                        is_section_header = item.get("is_section_header", False)
                        is_bold = item.get("is_bold", False)
                        indent = min(item.get("indent", 0), 2)
                        is_percent = item.get("is_percent", False)

                        row_class_str = "row-bold" if is_bold else ""

                        _parts.append(f'<tr class="{row_class_str}">')
                        _parts.append(f'<td class="indent-{indent}">{label}</td>')

                        for val in values:
                            if is_section_header:
                                formatted = ""
                            elif val is None:
                                formatted = "-"
                            elif is_percent:
                                formatted = f"{val:.2f}%"
                            else:
                                formatted = f"{val:.2f}"
                            _parts.append(f'<td class="data-cell">{formatted}</td>')

                        _parts.append('</tr>')

                    _parts.append('</tbody></table></div></div>')
                    html = ''.join(_parts)
                    st.html(html)

                    notes = data.get("notes", [])
                    if notes:
                        st.caption("Ratios assumptions: " + " | ".join(notes))

                    # Excel download (built lazily — only when the user clicks Excel)
                    _t_xl_rat_inner = _time.perf_counter()
                    _lazy_excel_download(f"ratios_{selected_ticker}", f"{selected_ticker}_Ratios.xlsx", _build_ratios_xl)
                    _timings['ratios_excel'] = (_time.perf_counter() - _t_xl_rat_inner) * 1000

                    # Store in session_state for instant reuse on next rerun
                    st.session_state[_ratios_cache_key] = html
                    st.session_state[_ratios_cache_key + "_notes"] = notes
                else:
                    st.info("No ratios data available for the selected date range")

                _timings['ratios_render'] = (_time.perf_counter() - _t0_render) * 1000
                _timings['ratios_total'] = (_time.perf_counter() - _t0_tab) * 1000
        except Exception as e:
            log_structured_error(e, page="market_data", component="render_page", operation="RENDER_RATIOS")
            st.error(f"Error loading ratios: {e}")
    elif selected_tab == "estimates":
        _t0_tab = _time.perf_counter()
        try:
            _est_period_type = (st.session_state.get(period_type_key) or 'Annual').lower()
            _est_ck = f"_est_html_{selected_ticker}_{start_date}_{end_date}_{_est_period_type}_{sort_ascending}_{conversion_rate:.6f}_{units_scale}"
            _est_cached = st.session_state.get(_est_ck)
            log_info(f"[EST_TAB] ticker={selected_ticker} period={_est_period_type} start={start_date} end={end_date} sort={sort_ascending} cached={'YES' if _est_cached else 'NO'}")
            if _est_cached is not None:
                st.html(_est_cached)
            else:
                _t0 = _time.perf_counter()
                log_info(f"[EST_FETCH] calling get_estimates_data ticker={selected_ticker} period={_est_period_type}")
                _est_data = AnalystEstimatesRepository.get_estimates_data(
                    selected_ticker, start_date, end_date, _est_period_type
                )
                _timings['estimates_fetch'] = (_time.perf_counter() - _t0) * 1000
                log_info(f"[EST_FETCH] done in {_timings['estimates_fetch']:.0f}ms periods={len(_est_data.get('periods', []))} sections={len(_est_data.get('sections', []))}")

                periods = _est_data.get("periods", [])
                sections = _est_data.get("sections", [])
                est_source = _est_data.get("source", "AV")

                if not sort_ascending:
                    periods = list(reversed(periods))
                    for section in sections:
                        for row in section:
                            if not row.get("is_header"):
                                row["values"] = list(reversed(row["values"]))

                if periods and sections:
                    _parts = ['<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>',
                              f'<tr class="row-grey-separator"><th>Analyst Consensus Estimates'
                              f'<span class="header-subtext">{units_label} of {st.session_state.get("target_currency","USD")}, except EPS and counts. Source: {"Alpha Vantage" if est_source=="AV" else "Yahoo Finance"}.</span></th>']

                    for p in periods:
                        lbl = p.get("label", "")
                        lines = lbl.split('\n')
                        top_line = lines[0] if lines else lbl
                        date_line = lines[1] if len(lines) > 1 else ""
                        _parts.append(
                            f'<th class="data-col">'
                            f'<span class="period-label">{top_line}</span>'
                            f'<span class="period-date">{date_line}</span>'
                            '</th>'
                        )
                    _parts.append('</tr></thead><tbody>')

                    def _fmt_est(val, is_currency, is_eps, is_count, is_percent, conv_rate, u_scale):
                        if val is None:
                            return "-"
                        if is_count:
                            return f"{int(val)}" if val == int(val) else f"{val:.1f}"
                        if is_eps:
                            return f"{val:.2f}"
                        if is_percent:
                            return f"{val:.1f}%"
                        if is_currency:
                            return format_value(val, conv_rate, u_scale)
                        return f"{val:.4f}"

                    for section in sections:
                        for row in section:
                            lbl = row.get("label", "")
                            vals = row.get("values", [])
                            is_hdr = row.get("is_header", False)
                            is_bold = row.get("is_bold", False)
                            has_sep = row.get("has_grey_sep", False)
                            indent = min(row.get("indent", 0), 2)
                            is_currency = row.get("is_currency", False)
                            is_eps = row.get("is_eps", False)
                            is_count = row.get("is_count", False)
                            is_percent = row.get("is_percent", False)

                            row_cls = []
                            if is_bold: row_cls.append("row-bold")
                            if has_sep: row_cls.append("row-grey-separator")
                            row_cls_str = " ".join(row_cls)

                            _parts.append(f'<tr class="{row_cls_str}">')
                            _parts.append(f'<td class="indent-{indent}">{lbl}</td>')

                            for val in vals:
                                if is_hdr:
                                    _parts.append('<td class="data-cell"></td>')
                                else:
                                    fmt = _fmt_est(val, is_currency, is_eps, is_count, is_percent, conversion_rate, units_scale)
                                    _parts.append(f'<td class="data-cell">{fmt}</td>')
                            _parts.append('</tr>')

                    _parts.append('</tbody></table></div></div>')
                    _est_html = ''.join(_parts)
                    st.html(_est_html)
                    st.session_state[_est_ck] = _est_html

                    st.html(
                        '<p style="font-size:12px;color:#4F4F4F;background:rgba(0,0,0,0.03);'
                        'border-left:3px solid rgba(0,0,0,0.15);border-radius:0 4px 4px 0;'
                        'padding:5px 10px;margin:6px 0 0 4px;">'
                        'Analyst consensus estimates. Revenue values are converted to the selected currency and unit scale. '
                        'EPS values are shown in the company\'s reported currency.</p>'
                    )
                else:
                    st.info("No analyst estimates available for the selected ticker and filters.")
        except Exception as e:
            log_structured_error(e, page="market_data", component="render_page", operation="RENDER_ESTIMATES")
            st.error(f"Error loading estimates: {e}")
        _timings['estimates_total'] = (_time.perf_counter() - _t0_tab) * 1000
    elif selected_tab == "forecasting":
        _t0_tab = _time.perf_counter()
        try:
            _t0 = _time.perf_counter()
            # Defense-in-depth: quarterly forecasting paused
            # (see constants.QUARTERLY_FORECASTING_ENABLED). The effective period
            # type is already clamped to Annual upstream, but gate the render too.
            _is_quarterly_fcst = QUARTERLY_FORECASTING_ENABLED and _period_type_db == "quarterly"
            if _is_quarterly_fcst:
                _fcst_data = ModelForecastsRepository.get_quarterly_forecasts_data(selected_ticker, max_quarters=8)
            else:
                _fcst_data = ModelForecastsRepository.get_forecasts_data(selected_ticker)
            _timings['forecasting_fetch'] = (_time.perf_counter() - _t0) * 1000
            _fcst_period_label = "Quarterly" if _is_quarterly_fcst else "Annual"

            _fcst_periods  = _fcst_data.get("periods", [])
            _fcst_sections = _fcst_data.get("sections", [])
            _last_actual   = _fcst_data.get("last_actual_date")

            if not sort_ascending:
                _fcst_periods = list(reversed(_fcst_periods))
                for _sec in _fcst_sections:
                    for _row in _sec:
                        if not _row.get("is_header"):
                            _row["values"] = list(reversed(_row["values"]))

            if _fcst_periods and _fcst_sections:
                # ── HTML table (with session-state cache) ─────────────────
                _fcst_ck = f"_fcst_html_v5_{selected_ticker}_{_period_type_db}_{sort_ascending}_{conversion_rate:.6f}_{units_scale}"
                _fcst_cached = st.session_state.get(_fcst_ck)
                if _fcst_cached is not None:
                    st.html(_fcst_cached)
                else:
                    _fcst_css = """<style>
                    .fcst-tab-col  { background:rgba(27,107,36,0.07)!important;
                                     border-left:2px solid rgba(27,107,36,0.20)!important; }
                    .fcst-tab-ens  { background:rgba(27,107,36,0.12)!important;
                                     border-left:2px solid rgba(27,107,36,0.35)!important;
                                     color:#1B6B24!important; font-weight:600!important; }
                    .fcst-tab-model{ background:rgba(27,107,36,0.04);
                                     border-left:2px solid rgba(27,107,36,0.15);
                                     color:#2D5A32; }
                    .fcst-tab-scen { background:rgba(37,99,235,0.04);
                                     border-left:2px solid rgba(37,99,235,0.20);
                                     color:#1E40AF; }
                    </style>"""

                    _parts = [_fcst_css,
                              '<div class="table-container"><div class="table-scroll"><table class="data-table"><thead>',
                              f'<tr class="row-grey-separator"><th>Revenue Model Forecasts'
                              f'<span class="header-subtext">{units_label} of {st.session_state.get("target_currency","USD")}.'
                              + (f' Last actual: {_last_actual.strftime("%b %d, %Y")}' if _last_actual else '')
                              + '</span></th>']
                    # Quarterly headers mirror the Key Stats format — fiscal + calendar
                    # quarter + period-end date — via the SAME FiscalPeriod.from_date()
                    # helper Key Stats uses (no new quarter math). Annual unchanged.
                    from data.models import FiscalPeriod as _FP_FCST
                    _fcst_fye = getattr(company, "fiscal_year_end", None) if company else None
                    for _p in _fcst_periods:
                        _top = _fcst_period_label
                        if _is_quarterly_fcst:
                            _pdt = _p.get("date")
                            _fp_lbl = _FP_FCST.from_date(_pdt, "quarterly", _fcst_fye).label if _pdt is not None else ""
                            if "\n" in _fp_lbl:
                                _top, _bot = _fp_lbl.split("\n", 1)
                            else:
                                _bot = _fp_lbl or str(_p.get("label", ""))
                        else:
                            # Use label (= forecast_date's year, authoritative) over the
                            # raw fiscal_year, which some companies store off by one.
                            _bot = str(_p.get("label", _p.get("fiscal_year", "")))
                        _parts.append(
                            f'<th class="data-col fcst-tab-col">'
                            f'<span class="period-label">{_top}</span>'
                            f'<span class="period-date">{_bot}</span></th>'
                        )
                    _parts.append('</tr></thead><tbody>')

                    _scenario_mkeys = set(ModelForecastsRepository._SCENARIO_ORDER)
                    for _sec in _fcst_sections:
                        for _row in _sec:
                            _lbl     = _row.get("label", "")
                            _vals    = _row.get("values", [])
                            _is_hdr  = _row.get("is_header", False)
                            _is_bold = _row.get("is_bold", False)
                            _has_sep = _row.get("has_grey_sep", False)
                            _indent  = min(_row.get("indent", 0), 2)
                            _mkey    = _row.get("model_key", "")
                            _is_ens  = (_mkey == "ensemble")
                            _is_scen = (_mkey in _scenario_mkeys)

                            _row_cls = []
                            if _is_bold: _row_cls.append("row-bold")
                            if _has_sep: _row_cls.append("row-grey-separator")
                            _parts.append(f'<tr class="{" ".join(_row_cls)}">')
                            _parts.append(f'<td class="indent-{_indent}">{_lbl}</td>')

                            for _v in _vals:
                                if _is_hdr:
                                    _parts.append('<td class="data-cell"></td>')
                                elif _v is None:
                                    if _is_ens:
                                        _cell_cls = "data-cell fcst-tab-ens"
                                    elif _is_scen:
                                        _cell_cls = "data-cell fcst-tab-scen"
                                    else:
                                        _cell_cls = "data-cell fcst-tab-model"
                                    _parts.append(f'<td class="{_cell_cls}">-</td>')
                                else:
                                    _fmt = format_value(_v, conversion_rate, units_scale)
                                    if _is_ens:
                                        _cell_cls = "data-cell fcst-tab-ens"
                                    elif _is_scen:
                                        _cell_cls = "data-cell fcst-tab-scen"
                                    else:
                                        _cell_cls = "data-cell fcst-tab-model"
                                    _parts.append(f'<td class="{_cell_cls}">{_fmt}</td>')
                            _parts.append('</tr>')

                    _parts.append('</tbody></table></div></div>')
                    _fcst_html = ''.join(_parts)
                    st.html(_fcst_html)
                    st.session_state[_fcst_ck] = _fcst_html

                st.html(
                    '<p style="font-size:12px;color:#1B6B24;background:rgba(27,107,36,0.05);'
                    'border-left:3px solid rgba(27,107,36,0.35);border-radius:0 4px 4px 0;'
                    'padding:5px 10px;margin:6px 0 0 4px;line-height:1.6;">'
                    '<strong>Ensemble</strong> blends the top-3 backtested models '
                    '(by lowest MAPE (Mean Absolute Percentage Error)). '
                    'Scenario band shows the pessimistic–optimistic range. Only Total Revenue is forecasted. '
                    f'<a href="/forecasting?ticker={selected_ticker}&period_type={_fcst_period_label}" target="_blank" rel="noopener noreferrer" '
                    'style="color:#1B6B24;font-weight:700;text-decoration:none;'
                    'border-bottom:1.5px solid rgba(27,107,36,0.4);'
                    'transition:border-color 0.15s;cursor:pointer;">'
                    'Show More ↗</a>'
                    '</p>'
                )
            else:
                st.info("No model forecasts available for this ticker. The forecasting engine requires at least 3 years of historical revenue.")
        except Exception as e:
            log_structured_error(e, page="market_data", component="render_page", operation="RENDER_FORECASTING")
            st.error(f"Error loading forecasts: {e}")
        _timings['forecasting_total'] = (_time.perf_counter() - _t0_tab) * 1000
    elif selected_tab == "segment_data":
        _t0_tab = _time.perf_counter()
        try:
            _t0 = _time.perf_counter()
            _seg_period_type = _period_type_eff_title.lower()
            _render_period_fallback_notice()
            _seg_fye = getattr(company, 'fiscal_year_end', None) if company else None
            render_segment_data(selected_ticker, start_date, end_date, conversion_rate, reported_currency, sort_ascending, historical_rate_map, units_scale, units_label, _seg_period_type, fiscal_year_end=_seg_fye)
            _timings['segment_data_render'] = (_time.perf_counter() - _t0) * 1000

            # Excel download (built lazily — only when the user clicks Excel)
            _t_xl_seg = _time.perf_counter()
            def _build_seg_xl():
                from utils.excel_export import export_financial_excel
                from data.repository import SegmentDataRepository
                from utils.constants import SEGMENT_METRIC_GROUPS
                _seg_data = SegmentDataRepository.get_segment_data(selected_ticker, start_date, end_date, _seg_period_type)
                _seg_years = _seg_data["years"]
                _seg_biz = _seg_data.get("business_segments", {})
                _seg_geo = _seg_data.get("geo_segments", {})
                _seg_period_dates = _seg_data.get("period_dates", {})
                _seg_period_display = _seg_data.get("period_display_dates") or _seg_period_dates
                if not sort_ascending:
                    _seg_years = list(reversed(_seg_years))
                if not (_seg_years and (_seg_biz or _seg_geo)):
                    return None
                _seg_rows = []

                def _xl_add_section(title, seg_dict):
                    if not seg_dict:
                        return
                    # Section title row
                    _seg_rows.append({"label": title, "values": [None] * len(_seg_years), "is_bold": True, "indent": 0, "is_percent": False, "is_text": True, "has_separator": True, "is_estimated": [False] * len(_seg_years)})
                    for metric_name, cfg in SEGMENT_METRIC_GROUPS.items():
                        if metric_name not in seg_dict:
                            continue
                        members_data = seg_dict[metric_name]
                        if not members_data:
                            continue
                        # Metric heading row
                        _seg_rows.append({"label": cfg["display"], "values": [None] * len(_seg_years), "is_bold": True, "indent": 0, "is_percent": False, "is_text": True, "has_separator": True, "is_estimated": [False] * len(_seg_years)})
                        totals = {yr: 0.0 for yr in _seg_years}
                        has_total = False
                        for member in sorted(members_data.keys(), key=lambda m: m.lower()):
                            yr_vals = members_data[member]
                            _vals = [yr_vals.get(yr) for yr in _seg_years]
                            _conv_vals = [v * conversion_rate * units_scale if v is not None else None for v in _vals]
                            _seg_rows.append({"label": member, "values": _conv_vals, "is_bold": False, "indent": 1, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": [False] * len(_seg_years)})
                            for yr in _seg_years:
                                v = yr_vals.get(yr)
                                if v is not None:
                                    totals[yr] += v
                                    has_total = True
                        if has_total and len(members_data) > 1:
                            _t_vals = [totals[yr] * conversion_rate * units_scale if totals[yr] != 0 else None for yr in _seg_years]
                            _seg_rows.append({"label": "Total", "values": _t_vals, "is_bold": True, "indent": 1, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": [False] * len(_seg_years)})

                _xl_add_section("Business Segments", _seg_biz)
                _xl_add_section("Geographic Segments", _seg_geo)

                # Format year headers
                _seg_col_headers = []
                for yr in _seg_years:
                    pd = _seg_period_display.get(yr)
                    if pd:
                        if _seg_period_type == "quarterly":
                            from data.models import FiscalPeriod as _FP2
                            _fp2 = _FP2.from_date(pd, "quarterly", _seg_fye)
                            _seg_col_headers.append(_fp2.label.replace("\n", " / "))
                        else:
                            _seg_col_headers.append(f"12 Months / {pd.strftime('%b-%d-%Y')}")
                    else:
                        _seg_col_headers.append(str(yr))
                return export_financial_excel("Segment Data", company.name or "", selected_ticker, _seg_col_headers, _seg_rows, units_label, st.session_state.get("target_currency", "USD"), start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
            _lazy_excel_download(f"seg_{selected_ticker}", f"{selected_ticker}_Segment_Data.xlsx", _build_seg_xl)
            _timings['segment_data_excel'] = (_time.perf_counter() - _t_xl_seg) * 1000

            _timings['segment_data_total'] = (_time.perf_counter() - _t0_tab) * 1000
        except Exception as e:
            log_structured_error(e, page="market_data", component="render_page", operation="RENDER_SEGMENT_DATA")
            st.error(f"Error loading segment data: {e}")
    elif selected_tab == "ratings":
        _t0_tab = _time.perf_counter()
        try:
            # Non-blocking auto-heal: render the ratings/store table inside a fragment
            # that re-runs ONLY this subtree every 1.5s while background fetches are
            # still warming (render_ratings_data sets `_ratings_incomplete_<ticker>`).
            # This replaced a `_time.sleep()`+`st.rerun()` loop that parked the
            # session's single script thread, freezing every tab click for seconds
            # (STG: "Additional Data → any tab is dead"). run_every is fixed at
            # decoration, so once the flag flips to complete we do ONE full rerun to
            # re-decorate with run_every=None and stop polling.
            _rat_inc_key = f"_ratings_incomplete_{selected_ticker}"
            _rat_incomplete = st.session_state.get(_rat_inc_key, True)

            @st.fragment(run_every=(1.5 if _rat_incomplete else None))
            def _ratings_poll_fragment():
                _t0 = _time.perf_counter()
                render_ratings_data(selected_ticker, start_date, end_date, sort_ascending)
                _timings['ratings_render'] = (_time.perf_counter() - _t0) * 1000
                if _rat_incomplete and not st.session_state.get(_rat_inc_key, True):
                    st.rerun(scope="app")

            _ratings_poll_fragment()

            # Excel download — OUTSIDE the polling fragment (it is itself an
            # st.fragment; nesting is avoided). Built lazily only on click.
            _t_xl_rat = _time.perf_counter()
            def _build_rat_xl():
                from utils.excel_export import export_financial_excel
                from data.repository import RatingsDataRepository
                _rat_data = st.session_state.get(f"_ratings_data_{selected_ticker}_{start_date}_{end_date}")
                if _rat_data is None:
                    _rat_data = RatingsDataRepository.get_ratings_data(selected_ticker, start_date, end_date)
                _rat_years = _rat_data["years"]
                if not sort_ascending:
                    _rat_years = list(reversed(_rat_years))
                if _rat_years and (_rat_data["credit_ratings"] or _rat_data["store_counts"]):
                    _rat_rows = []
                    # Credit ratings section
                    if _rat_data["credit_ratings"]:
                        _rat_rows.append({"label": "Credit Ratings", "values": [None] * len(_rat_years), "is_bold": True, "indent": 0, "is_percent": False, "is_text": True, "has_separator": True, "is_estimated": [False] * len(_rat_years)})
                        for cr in _rat_data["credit_ratings"]:
                            _rat_rows.append({"label": f"{cr['agency']} Rating", "values": [cr["values"].get(yr, '') or '' for yr in _rat_years], "is_bold": False, "indent": 1, "is_percent": False, "is_text": True, "has_separator": False, "is_estimated": [False] * len(_rat_years)})
                            _rat_rows.append({"label": "Outlook", "values": [cr["outlooks"].get(yr, '') or '' for yr in _rat_years], "is_bold": False, "indent": 2, "is_percent": False, "is_text": True, "has_separator": False, "is_estimated": [False] * len(_rat_years)})
                    # Store counts section (hidden when Stores by Country covers it)
                    _sbc_tot = (_rat_data.get("stores_by_country", {}) or {}).get("total_row", {})
                    _hide_sc = bool(_sbc_tot) and all(
                        (yr in _sbc_tot) for sc in _rat_data["store_counts"]
                        for yr, v in sc["values"].items() if v is not None)
                    if _rat_data["store_counts"] and not _hide_sc:
                        # Same geography parenthetical as the UI header.
                        from data.repository import store_count_geo_label
                        _xl_sbc = _rat_data.get("stores_by_country", {}) or {}
                        _xl_geo = store_count_geo_label(
                            selected_ticker,
                            bool(_xl_sbc.get("years") and _xl_sbc.get("countries")))
                        _rat_rows.append({"label": f"Store Count ({_xl_geo})", "values": [None] * len(_rat_years), "is_bold": True, "indent": 0, "is_percent": False, "is_text": True, "has_separator": True, "is_estimated": [False] * len(_rat_years)})
                        for sc in _rat_data["store_counts"]:
                            _vals = [int(sc["values"].get(yr)) if sc["values"].get(yr) is not None else None for yr in _rat_years]
                            _rat_rows.append({"label": sc["store_type"].title(), "values": _vals, "is_bold": False, "indent": 1, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": [False] * len(_rat_years)})
                    # Stores by Country section — continent-grouped like the UI:
                    # countries indented under their continent subtotal when
                    # multi-continent; grand total (continent / Americas /
                    # Worldwide via stores_total_label) last.
                    _sbc = _rat_data.get("stores_by_country", {}) or {}
                    if _sbc.get("countries"):
                        from data.repository import group_countries_by_continent, stores_total_label
                        _sbc_part = bool(_sbc.get("partial"))
                        _sbc_hdr = ("Stores by Country (partial — as disclosed in 10-K; does not sum to total)"
                                    if _sbc_part else "Stores by Country")
                        _rat_rows.append({"label": _sbc_hdr, "values": [None] * len(_rat_years), "is_bold": True, "indent": 0, "is_percent": False, "is_text": True, "has_separator": True, "is_estimated": [False] * len(_rat_years)})
                        _sbc_groups = group_countries_by_continent(_sbc["countries"])
                        _multi_cont = len(_sbc_groups) > 1 and not _sbc_part
                        _cn_indent = 2 if _multi_cont else 1
                        for _cont, _cont_countries, _cont_totals in _sbc_groups:
                            for _cn in _cont_countries:
                                _cv = _sbc["countries"][_cn]
                                _vals = [int(_cv.get(yr)) if _cv.get(yr) is not None else None for yr in _rat_years]
                                _rat_rows.append({"label": _cn, "values": _vals, "is_bold": False, "indent": _cn_indent, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": [False] * len(_rat_years)})
                            if _multi_cont:
                                _vals = [int(_cont_totals.get(yr)) if _cont_totals.get(yr) is not None else None for yr in _rat_years]
                                _rat_rows.append({"label": f"Total ({_cont})", "values": _vals, "is_bold": True, "indent": 1, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": [False] * len(_rat_years)})
                        _trow = _sbc.get("total_row", {})
                        if _trow:
                            _tlabel = stores_total_label(_sbc_groups, _trow)
                            _vals = [int(_trow.get(yr)) if _trow.get(yr) is not None else None for yr in _rat_years]
                            _rat_rows.append({"label": _tlabel, "values": _vals, "is_bold": True, "indent": 0, "is_percent": False, "is_text": False, "has_separator": False, "is_estimated": [False] * len(_rat_years)})
                    _rat_disp_dates = _rat_data.get("period_display_dates") or _rat_data.get("period_dates", {})
                    _rat_col_headers = [
                        _pd.strftime("%b-%d-%Y") if (_pd := _rat_disp_dates.get(yr)) and hasattr(_pd, "strftime") else str(yr)
                        for yr in _rat_years
                    ]
                    return export_financial_excel("Ratings & Store Data", company.name or "", selected_ticker, _rat_col_headers, _rat_rows, "", "USD", start_date.strftime("%b %Y"), end_date.strftime("%b %Y"))
                return None
            _lazy_excel_download(f"rat_{selected_ticker}", f"{selected_ticker}_Ratings.xlsx", _build_rat_xl)
            _timings['ratings_excel'] = (_time.perf_counter() - _t_xl_rat) * 1000

            _timings['ratings_total'] = (_time.perf_counter() - _t0_tab) * 1000
        except Exception as e:
            log_structured_error(e, page="market_data", component="render_page", operation="RENDER_RATINGS")
            st.error(f"Error loading ratings data: {e}")
    # company_profile tab is handled above in the table section

    # Clear the loading spinner now that tab content has rendered
    _tab_loading_hint.empty()
    st.session_state.pop("_md_tab_loader", None)
    log_info(
        f"[MD_PHASE] rerun={get_rerun_id()} phase=page_render_complete "
        f"ticker={selected_ticker} tab={selected_tab} "
        f"period_ss={st.session_state.get(period_type_key)} qp_period={st.query_params.get('period_type')}"
    )

    _page_elapsed = (_time.perf_counter() - _page_start) * 1000
    _timings['page_total'] = _page_elapsed

    # ── Log ALL timings to server-log ──
    from utils.server_logger import log_timing
    _ticker_ctx = selected_ticker if 'selected_ticker' in dir() else '?'
    _tab_ctx = selected_tab if 'selected_tab' in dir() else '?'
    for _tk, _tv in _timings.items():
        log_timing(f"PAGE_{_tk}", _tv, details=f"tab={_tab_ctx} ticker={_ticker_ctx}")
    # Single summary line
    _timing_summary = " | ".join(f"{k}={v:.0f}ms" for k, v in sorted(_timings.items(), key=lambda x: -x[1]))
    _TAB_TIMING_NAMES = {
        "income_statement": "Income_Statement",
        "balance_sheet": "Balance_Sheet",
        "cash_flow": "Cash_Flow",
        "key_stats": "Key_Statistics",
        "segment_data": "Segments",
        "estimates": "Estimates",
        "company_profile": "Company_Profile",
        "ratios": "Ratios",
        "forecasting": "Forecasting",
        "ratings": "Ratings",
    }
    _tab_timing_key = _TAB_TIMING_NAMES.get(_tab_ctx, _tab_ctx)
    log_timing(
        f"TAB_{_tab_timing_key}",
        _page_elapsed,
        details=f"ticker={_ticker_ctx} [{_timing_summary}]",
    )
    log_timing("PAGE_SUMMARY", _page_elapsed, details=f"tab={_tab_ctx} ticker={_ticker_ctx} [{_timing_summary}]")


def main():
    """Market data page entry point."""
    import time as _time
    from utils.server_logger import log_timing, log_exception, PageLoadTracker, log_render_complete

    new_rerun_id("market_data")
    _main_start = _time.perf_counter()
    # Queue-delay instrumentation (16-Jul): Streamlit processes a session's
    # script runs SEQUENTIALLY — a click during a run waits for it to finish,
    # then triggers a fresh full run. A run that starts <100ms after the
    # previous one ended almost certainly served a click that sat QUEUED the
    # whole previous run — that queue time is the "app is blocked" the user
    # feels, and it never shows up inside any per-phase timing.
    _md_now = _time.perf_counter()
    _md_prev_end = st.session_state.get("_md_prev_run_end")
    _md_gap_ms = (_md_now - _md_prev_end) * 1000 if _md_prev_end else -1.0
    _md_gap_note = (" interaction_QUEUED_behind_previous_run"
                    if 0 <= _md_gap_ms < 100 else "")
    # Memory at render START. STG slowdowns line up with low `avail` — under
    # memory pressure st.cache_data evicts entries, so "cached" tab data
    # re-hits the DB and slow queries thrash. Logging rss/avail at start (and
    # the delta at end) makes that correlation visible per render.
    _md_rss0 = _md_avail0 = None
    try:
        from utils.server_logger import ram_snapshot_mb as _ram_snap
        _md_rss0, _md_used0, _md_total0, _md_avail0 = _ram_snap()
    except Exception:
        _ram_snap = None
    _mem_note = (f" rss={_md_rss0:.0f}MB avail={_md_avail0:.0f}MB"
                 f"{' LOW_MEM' if (_md_avail0 is not None and _md_avail0 < 500) else ''}"
                 if _md_rss0 is not None else "")
    log_timing(
        "MD_RUN_START", 0,
        details=(f"tab={st.query_params.get('tab', '-')} "
                 f"ticker={st.query_params.get('ticker', '-')} "
                 f"gap_since_prev_run_end_ms={_md_gap_ms:.0f}{_md_gap_note}{_mem_note}"),
    )
    _tracker = PageLoadTracker("market_data")
    try:
        render_styles()

        set_page_layout(
            header_full_width=True,
            footer_full_width=True,
            body_padding="0 20px",
            max_content_width="1350px",
            remove_top_padding=True,
            footer_at_bottom=True
        )

        ticker_for_header = st.query_params.get("ticker", "M")
        render_header(full_width=True, current_page="market_data", ticker=ticker_for_header)
        _pre_render_elapsed = (_time.perf_counter() - _main_start) * 1000
        log_timing("MAIN_PRE_RENDER", _pre_render_elapsed, details=f"render_styles+set_page_layout+render_header")
        render_page()
        render_coresight_footer(full_width=True, stick_to_bottom=True)
        _main_total = (_time.perf_counter() - _main_start) * 1000
        _mem_end = ""
        if _ram_snap is not None and _md_rss0 is not None:
            try:
                _rss1, _u1, _t1, _avail1 = _ram_snap()
                _mem_end = (f" | mem rss={_rss1:.0f}MB (delta={_rss1 - _md_rss0:+.0f}MB) "
                            f"avail={_avail1:.0f}MB"
                            f"{' LOW_MEM' if (_avail1 is not None and _avail1 < 500) else ''}")
            except Exception:
                pass
        log_timing("MAIN_TOTAL", _main_total,
                   details=f"includes pre_render={_pre_render_elapsed:.0f}ms{_mem_end}")
        log_render_complete("market_data", _time.perf_counter() - _main_start)
        _tracker.finish()
        st.session_state["_md_prev_run_end"] = _time.perf_counter()
    except Exception as _main_exc:
        import traceback as _tb
        log_exception(f"[MAIN_CRASH] UNHANDLED ticker={st.query_params.get('ticker')} tab={st.query_params.get('tab')} period={st.query_params.get('period_type')} err={_main_exc}\n{_tb.format_exc()}")
        raise  # re-raise so Streamlit still shows the error


main()
