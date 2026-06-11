"""
Company Profile Page - Coresight Research
=========================================
Company overview page with profile information.
"""
# from attr import asdict  # Not used - using dataclasses instead
import streamlit as st
from typing import Optional

# from components.styles import hide_sidebar, set_page_layout
# hide_sidebar()

from components.styles import render_styles, COLORS, TYPOGRAPHY, SPACING
from components.navigation import render_header, render_coresight_footer, render_company_header
from components.toolbar import render_tabs
from data.models import CompanyOverview
from data.repository import CompanyOverviewRepository
from core.database import init_database
from utils.constants import (
    CURRENCY_NAMES,
    CURRENCY_SYMBOLS,
    EXCHANGE_NAMES,
    COUNTRY_NAMES,
    get_currency_symbol,
    format_currency_full,
)
from utils.server_logger import log_structured_error


def get_company_css() -> str:
    """Get custom CSS for company profile page - matches Figma exactly."""
    return """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&family=Montserrat:wght@400;500;600;700&display=swap');

    /* Page Container */
    .company-profile-container {
        max-width: 1238px;
        margin: 0 auto;
        padding: 0 0;
        font-family: 'Roboto', sans-serif;
    }

    /* Streamlit Selectbox Styling - scoped to company selector only */
    div.stSelectbox:has(input[aria-label*="Select Company"]) {
        margin-top: -85px !important;
        margin-bottom: 20px !important;
        width: 600px !important;
    }

    div.stSelectbox:has(input[aria-label*="Select Company"]) > div {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
    }

    div.stSelectbox:has(input[aria-label*="Select Company"]) label {
        display: none !important;
    }

    /* Hide the actual selectbox but keep it clickable */
    div.stSelectbox:has(input[aria-label*="Select Company"]) > div > div {
        opacity: 0;
        height: 30px;
        cursor: pointer;
    }

    /* Tab Navigation */
    .tab-navigation {
        display: flex;
        border-bottom: 1px solid #CBCACA;
        margin-bottom: 24px;
        gap: 80px;
    }

    .tab-item {
        font-family: 'Montserrat', sans-serif;
        font-weight: 600;
        font-size: 16px;
        padding: 12px 0;
        cursor: pointer;
        color: #323232;
        border-bottom: 3px solid transparent;
        margin-bottom: -2px;
        transition: all 0.2s ease;
    }

    .tab-item:hover {
        color: #d62e2f;
    }

    .tab-item.active {
        color: #d62e2f;
        border-bottom-color: #d62e2f;
    }

    /* Info Table Container */
    .info-table-container {
        border: 1px solid #CBCACA;
        border-radius: 12px;
        overflow: hidden;
        background: #FFFFFF;
        margin-bottom: 32px;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05);
    }

    .info-table {
        width: 100%;
        border-collapse: collapse;
    }

    .info-table-row {
        display: flex;
        border-bottom: 1px solid #CBCACA;
    }

    .info-table-row:last-child {
        border-bottom: none;
    }

    /* Left column (labels) - Website:, Number of Employees, etc */
    .info-label {
        width: 22%;
        padding: 14px 20px;
        background: #F8F9FA;
        font-family: 'Roboto', sans-serif;
        font-weight: 700;
        font-size: 14px;
        color: #323232;
        border-right: 1px solid #CBCACA;
        display: flex;
        align-items: center;
    }

    /* Middle column (values) - macys.com, 1,556,999, etc */
    .info-value {
        width: 28%;
        padding: 14px 20px;
        font-family: 'Roboto', sans-serif;
        font-weight: 400;
        font-size: 14px;
        color: #323232;
        border-right: 1px solid #CBCACA;
        display: flex;
        align-items: center;
    }

    /* Right column labels (Coverage Summary:, Coverage List:, etc) */
    .info-label-right {
        width: 22%;
        padding: 14px 20px;
        background: #F8F9FA;
        font-family: 'Roboto', sans-serif;
        font-weight: 700;
        font-size: 14px;
        color: #323232;
        border-right: 1px solid #CBCACA;
        display: flex;
        align-items: center;
    }

    /* Right column values - No, No, No, etc */
    .info-value-right {
        width: 28%;
        padding: 14px 20px;
        font-family: 'Roboto', sans-serif;
        font-weight: 400;
        font-size: 14px;
        color: #323232;
        display: flex;
        align-items: center;
    }

    /* Business Description Section */
    .business-description-section {
        margin-top: 32px;
    }

    .section-header {
        font-family: 'Montserrat', sans-serif;
        font-weight: 700;
        font-size: 18px;
        color: #323232;
        margin-bottom: 16px;
        padding-bottom: 8px;
        border-bottom: 1px solid #CBCACA;
    }

    .business-description-text {
        font-family: 'Roboto', sans-serif;
        font-weight: 400;
        font-size: 15px;
        line-height: 1.6;
        color: #4F4F4F;
    }

    /* Link styling */
    .website-link {
        color: #0066CC;
        text-decoration: none;
    }

    .website-link:hover {
        text-decoration: underline;
    }

    /* No data available styling */
    .no-data {
        color: #888888;
        font-style: italic;
    }
    </style>
    """

from dataclasses import asdict, is_dataclass

def object_to_dict(obj):
    """
    Safely convert any object (dataclass, attrs, pydantic, or normal class)
    into a dictionary.
    """
    try:
        # dataclass
        if is_dataclass(obj):
            return asdict(obj)

        # attrs class
        if hasattr(obj, "__attrs_attrs__"):
            return {attr.name: getattr(obj, attr.name) for attr in obj.__attrs_attrs__}

        # pydantic
        if hasattr(obj, "model_dump"):
            return obj.model_dump()

        # normal class
        if hasattr(obj, "__dict__"):
            return vars(obj)

        raise TypeError("Unsupported object type")
    except Exception as e:
        log_structured_error(e, page="company_profile", component="object_to_dict", operation="converting object to dict")
        return {}

def company_to_label_value(company):
    try:
        data = object_to_dict(company)

        return {
            key.replace("_", " ").title(): value
            for key, value in data.items()
        }
    except Exception as e:
        log_structured_error(e, page="company_profile", component="company_to_label_value", operation="converting company to label-value pairs")
        return {}

def format_market_cap(value, currency_symbol: str = ""):
    """Format market cap dynamically in Billions or Millions with currency symbol."""
    if value in (None, "", "N/A"):
        return "N/A"

    try:
        clean = str(value).replace(",", "").replace("$", "").strip().lower()

        # Handle already formatted values
        if clean.endswith("b"):
            num = float(clean[:-1])
            return f"{currency_symbol} {num:.2f} (in Billions)"

        if clean.endswith("m"):
            num = float(clean[:-1])
            return f"{currency_symbol} {num:.2f} (in Millions)"

        # Raw numeric value
        num = float(clean)

        if num >= 1_000_000_000:
            return f"{currency_symbol} {num / 1_000_000_000:.2f} (in Billions)"
        else:
            return f"{currency_symbol} {num / 1_000_000:.2f} (in Millions)"

    except Exception as e:
        log_structured_error(e, page="company_profile", component="format_market_cap", operation="formatting market cap value")
        return str(value)


def to_title_case(value: Optional[str]) -> str:
    """Format string to Title Case - first letter of each word capitalized."""
    if not value or value in ("N/A", "", None):
        return "N/A"
    # Convert to lowercase first, then title case for consistent formatting
    return str(value).lower().title()

def render_info_table(company: CompanyOverview) -> str:
    """Render the company info table matching Figma wireframe exactly."""
    try:
        # Hiding Sector and Industry as per manager's request
        HIDDEN_FIELDS = {"Company Description", "Fetched At Utc", "Cik", "Address", "Analyst Target Price", "Sector", "Industry"}

        LABEL_MAP = {
            "Eps":           "EPS",
            "Pe Ratio":      "P/E Ratio",
            "Ebitda":        "EBITDA",
            "Revenue Ttm":   "Revenue (TTM)",
            "Week 52 High":  "52-Week High",
            "Week 52 Low":   "52-Week Low",
            "Primary Industry Coresight": "Coresight Sector",
        }

        data_items = list(company_to_label_value(company).items())
        data_items = [
            (LABEL_MAP.get(k, k), v) for k, v in data_items
            if v not in (None, "", "N/A") and k not in HIDDEN_FIELDS
        ]
        def format_website(value):
            if not value or value == "N/A":
                return '<span class="no-data">N/A</span>'
            # Clean URL for display
            display = str(value).replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")
            return f'<a href="{value}" target="_blank" class="website-link">{display}</a>'

        MARKET_CAP_FIELDS = {"Market Capitalization", "Revenue (TTM)", "EBITDA"}
        SHARES_FIELDS = {"Shares Outstanding"}  # No currency symbol for shares
        CURRENCY_PREFIX_FIELDS = {"EPS", "52-Week High", "52-Week Low"}  # Need currency prefix
        TITLE_CASE_FIELDS = {"Coresight Sector"}  # Fields that need Title Case formatting

        # Extract currency symbol first for market cap formatting
        currency_symbol = ""
        for k, v in data_items:
            if k == "Currency" and v:
                currency_symbol = get_currency_symbol(v)
                break

        formatted_items = []
        for k, v in data_items:
            if k in MARKET_CAP_FIELDS:
                v = format_market_cap(v, currency_symbol)
            elif k in SHARES_FIELDS:
                v = format_market_cap(v, "")  # No currency for shares
            elif k in CURRENCY_PREFIX_FIELDS:
                if v not in (None, "", "N/A"):
                    v = f"{currency_symbol} {v}"
            elif k in TITLE_CASE_FIELDS:
                v = to_title_case(v)
            elif k == "Currency" and v:
                # Format as 'Full Name (CODE) (Symbol)' e.g., 'Hong Kong Dollar (HKD) ($)'
                v = format_currency_full(v)
            elif k == "Exchange" and v:
                # Format as 'Full Name (Short Code)' e.g., 'New York Stock Exchange (NYSE)'
                exchange_full = EXCHANGE_NAMES.get(v.upper(), v)
                v = f"{exchange_full} ({v})"
            elif k == "Country" and v:
                # Format as 'Full Name (Short Code)' e.g., 'United States (USA)'
                country_full = COUNTRY_NAMES.get(v.upper(), v)
                v = f"{country_full} ({v})"
            formatted_items.append((k, v))

        data_items = formatted_items

        for idx in range(len(data_items)):
            item = data_items[idx]
            if item[0] == "Official Site":
                # Format value as anchor tag
                formatted_value = format_website(item[1])
                data_items.pop(idx)
                data_items.insert(0, ("Official Site", formatted_value))
                break
        rows = []
        for i in range(0, len(data_items), 2):
            left_item = data_items[i]
            left_label = left_item[0]
            left_value = left_item[1] if len(left_item) > 1 else ""

            # Check if there's a right column
            if i + 1 < len(data_items):
                right_item = data_items[i + 1]
                right_label = right_item[0]
                right_value = right_item[1] if len(right_item) > 1 else ""
            else:
                right_label, right_value = "", ""

            rows.append((left_label, left_value, right_label, right_value))


        table_html = '<div class="info-table-container"><div class="info-table">'

        for label_left, value_left, label_right, value_right in rows:
            table_html += f'<div class="info-table-row">'
            table_html += f'<div class="info-label">{label_left}</div>'
            table_html += f'<div class="info-value">{value_left}</div>'
            table_html += f'<div class="info-label-right">{label_right}</div>'
            table_html += f'<div class="info-value-right">{value_right}</div>'
            table_html += '</div>'

        table_html += '</div></div>'

        return table_html
    except Exception as e:
        log_structured_error(e, page="company_profile", component="render_info_table", operation="rendering company info table")
        return ""


# def render_tabs(active_tab: str = "profile") -> str:
#     """Render the tab navigation - matches Figma exactly.
#     Order: Company Profile | Key Stats | Income Statement | Balance Sheet
#     """
#     tabs = [
#         ("profile", "Company Profile"),
#         ("stats", "Key Stats"),
#         ("income", "Income Statement"),
#         ("balance", "Balance Sheet")
#     ]

#     tabs_html = '<div class="tab-navigation">'
#     for tab_id, tab_label in tabs:
#         active_class = "active" if tab_id == active_tab else ""
#         tabs_html += f'<div class="tab-item {active_class}">{tab_label}</div>'
#     tabs_html += '</div>'

#     return tabs_html


def render_business_description(description: Optional[str]) -> str:
    """Render the business description section."""
    try:
        if not description:
            description = "No business description available."

        html = f"""
        <div class="business-description-section">
            <div class="section-header">Business Description</div>
            <div class="business-description-text">
                {description}
            </div>
        </div>
        """

        return html
    except Exception as e:
        log_structured_error(e, page="company_profile", component="render_business_description", operation="rendering business description")
        return ""


def render_reference_table(company: CompanyOverview) -> str:
    """Render the reference table in expander style with Sector and Industry."""
    try:
        sector = company.sector or "N/A"
        industry = company.industry or "N/A"

        html = f"""
        <div class="info-table-container" style="margin-top: 16px;">
            <div class="info-table">
                <div class="info-table-row">
                    <div class="info-label" style="width: 22%;">Sector</div>
                    <div class="info-value" style="width: 28%; border-right: 1px solid #CBCACA;">{sector}</div>
                    <div class="info-label-right" style="width: 22%;">Industry</div>
                    <div class="info-value-right" style="width: 28%;">{industry}</div>
                </div>
            </div>
        </div>
        """
        return html
    except Exception as e:
        log_structured_error(e, page="company_profile", component="render_reference_table", operation="rendering reference table")
        return ""


def get_profile_rows_for_excel(company) -> list:
    """Return [(label, plain_value), ...] for Excel export (same logic as render_info_table)."""
    try:
        HIDDEN_FIELDS = {"Company Description", "Fetched At Utc", "Cik", "Address",
                         "Analyst Target Price", "Sector", "Industry"}
        LABEL_MAP = {
            "Eps": "EPS", "Pe Ratio": "P/E Ratio", "Ebitda": "EBITDA",
            "Revenue Ttm": "Revenue (TTM)", "Week 52 High": "52-Week High",
            "Week 52 Low": "52-Week Low", "Primary Industry Coresight": "Coresight Sector",
        }
        MARKET_CAP_FIELDS = {"Market Capitalization", "Revenue (TTM)", "EBITDA"}
        SHARES_FIELDS = {"Shares Outstanding"}
        CURRENCY_PREFIX_FIELDS = {"EPS", "52-Week High", "52-Week Low"}
        TITLE_CASE_FIELDS = {"Coresight Sector"}

        raw_items = company_to_label_value(company)

        # Extract currency symbol for market cap formatting
        currency_symbol = ""
        for k, v in raw_items.items():
            if k == "Currency" and v:
                currency_symbol = get_currency_symbol(v)
                break

        data_items = [
            (LABEL_MAP.get(k, k), v)
            for k, v in raw_items.items()
            if v not in (None, "", "N/A") and k not in HIDDEN_FIELDS
        ]
        result = []
        for k, v in data_items:
            if k in MARKET_CAP_FIELDS:
                v = format_market_cap(v, currency_symbol)
            elif k in SHARES_FIELDS:
                v = format_market_cap(v, "")
            elif k in CURRENCY_PREFIX_FIELDS:
                if v not in (None, "", "N/A"):
                    v = f"{currency_symbol} {v}"
            elif k in TITLE_CASE_FIELDS:
                v = to_title_case(v)
            result.append((k, str(v) if v is not None else ""))

        # Move Official Site to first position (plain URL, no HTML)
        for idx, (k, v) in enumerate(result):
            if k == "Official Site":
                result.pop(idx)
                result.insert(0, (k, v))
                break
        return result
    except Exception as e:
        log_structured_error(e, page="company_profile", component="get_profile_rows_for_excel", operation="preparing profile rows for Excel export")
        return []


def render_company_profile_content(company):
    """Render company profile content only (info table + description) - no header."""
    try:
        # Page content container
        st.markdown('<div class="company-profile-container">', unsafe_allow_html=True)

        # Info table
        st.markdown(render_info_table(company), unsafe_allow_html=True)

        # Business description
        st.markdown(render_business_description(company.company_description), unsafe_allow_html=True)

        # Add space before expander
        st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)

        # Reference Table Expander - collapsed by default, contains Sector and Industry
        with st.expander("**For Reference**", expanded=False):
            st.markdown(render_reference_table(company), unsafe_allow_html=True)

        # Close container
        st.markdown('</div>', unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="company_profile", component="render_company_profile_content", operation="rendering company profile content")
        st.error("Failed to load company profile content.")


def render_company_profile(ticker: str = "M"):
    """Render full company profile (header + content) - for standalone page use."""
    try:
        # Handle ALL selection - use default ticker
        _fetch_ticker = ticker if ticker and ticker != "ALL" else "M"
        company = CompanyOverviewRepository.get_company_overview(_fetch_ticker)

        if not company:
            st.error(f"Company data not found for ticker: {_fetch_ticker}")
            st.stop()

        st.markdown(get_company_css(), unsafe_allow_html=True)

        st.markdown('<div class="company-profile-container">', unsafe_allow_html=True)

        render_company_header(
            company_name=company.name,
            ticker=company.ticker,
            exchange=company.exchange or "NYSE"
        )

        # Info table
        st.markdown(render_info_table(company), unsafe_allow_html=True)

        # Business description
        st.markdown(render_business_description(company.company_description), unsafe_allow_html=True)

        # Close container
        st.markdown('</div>', unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="company_profile", component="render_company_profile", operation="rendering full company profile")
        st.error("Failed to load company profile.")


# def main():
#     """Company profile page entry point (standalone)."""
#     # Initialize
#     init_database()

#     # Render global styles
#     render_styles()

#     # Set layout
#     set_page_layout(
#         header_full_width=True,
#         footer_full_width=True,
#         body_padding="0 20px",
#         max_content_width="1350px",
#         remove_top_padding=True,
#         footer_at_bottom=True
#     )

#     # Render Header
#     # render_header(full_width=True, current_page="company_profile")

#     # Get ticker from URL query params or default to M (Macy's)
#     query_params = st.query_params
#     ticker = query_params.get("ticker", "M")

#     # Render content
#     render_company_profile(ticker)

#     # Render Footer
#     # render_coresight_footer(full_width=True, stick_to_bottom=True)


# main()

# if __name__ == "__main__":
#     pass
