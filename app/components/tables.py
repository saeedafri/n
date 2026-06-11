"""
Reusable table components with consistent styling.
"""
import streamlit as st
import pandas as pd
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime, date
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from components.styles import COLORS, TYPOGRAPHY, SPACING, BORDER_RADIUS
from utils.server_logger import log_structured_error


def format_currency(value: float, decimals: int = 2) -> str:
    """Format number as currency."""
    try:
        if value is None:
            return "-"
        return f"${value:,.{decimals}f}"
    except Exception as e:
        log_structured_error(e, page="tables", component="format_currency", operation="format value")
        return "-"


def format_percentage(value: float, decimals: int = 2) -> str:
    """Format number as percentage with color."""
    try:
        if value is None:
            return "-"
        sign = "+" if value > 0 else ""
        color = COLORS["success"] if value >= 0 else COLORS["danger"]
        return f"<span style='color: {color}; font-weight: {TYPOGRAPHY['font_semibold']};'>{sign}{value:.{decimals}f}%</span>"
    except Exception as e:
        log_structured_error(e, page="tables", component="format_percentage", operation="format value")
        return "-"


def format_number(value: float, decimals: int = 0) -> str:
    """Format number with thousand separators."""
    try:
        if value is None:
            return "-"
        return f"{value:,.{decimals}f}"
    except Exception as e:
        log_structured_error(e, page="tables", component="format_number", operation="format value")
        return "-"


def format_volume(value: int) -> str:
    """Format volume in millions/thousands."""
    try:
        if value is None or (isinstance(value, float) and value != value):
            return "-"
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B"
        elif value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        elif value >= 1_000:
            return f"{value / 1_000:.2f}K"
        return str(value)
    except Exception as e:
        log_structured_error(e, page="tables", component="format_volume", operation="format value")
        return "-"


def format_market_cap(value: float) -> str:
    """Format market cap in billions/millions."""
    try:
        if value is None or (isinstance(value, float) and value != value):
            return "-"
        if value >= 1_000_000_000_000:
            return f"${value / 1_000_000_000_000:.2f}T"
        elif value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        elif value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        return format_currency(value)
    except Exception as e:
        log_structured_error(e, page="tables", component="format_market_cap", operation="format value")
        return "-"


def format_datetime(dt: datetime, format_str: str = "%b %d, %Y %H:%M") -> str:
    """Format datetime object."""
    try:
        if dt is None:
            return "-"
        return dt.strftime(format_str)
    except Exception as e:
        log_structured_error(e, page="tables", component="format_datetime", operation="format value")
        return "-"


def format_date(d: date, format_str: str = "%b %d, %Y") -> str:
    """Format date object."""
    try:
        if d is None:
            return "-"
        return d.strftime(format_str)
    except Exception as e:
        log_structured_error(e, page="tables", component="format_date", operation="format value")
        return "-"


def create_data_table(
    data: pd.DataFrame,
    columns_config: Optional[Dict[str, Dict[str, Any]]] = None,
    height: int = 400,
    width: str = "stretch",
    key: Optional[str] = None,
    selection_mode: str = "none",
    on_select: Optional[Callable] = None,
    hide_index: bool = True
) -> Any:
    """
    Create a styled data table with custom column configuration.

    Args:
        data: DataFrame to display
        columns_config: Dict mapping column names to config dicts
        height: Table height in pixels
        width: "stretch" for full width, "content" for fixed width
        key: Unique key for the table
        selection_mode: "none", "single", or "multi"
        on_select: Callback when selection changes
        hide_index: Whether to hide the index column

    Returns:
        Streamlit data editor object
    """
    try:
        # Default column configurations
        default_config = {
            "width": "auto",
            "help": None,
        }

        # Work on a copy to avoid mutating caller's DataFrame
        data = data.copy()

        # Apply custom configurations
        if columns_config:
            for col_name, config in columns_config.items():
                if col_name in data.columns:
                    # Apply formatting based on column type
                    if config.get("type") == "currency":
                        data[col_name] = data[col_name].apply(
                            lambda x: format_currency(x, config.get("decimals", 2))
                        )
                    elif config.get("type") == "percentage":
                        data[col_name] = data[col_name].apply(
                            lambda x: format_percentage(x, config.get("decimals", 2))
                        )
                    elif config.get("type") == "volume":
                        data[col_name] = data[col_name].apply(format_volume)
                    elif config.get("type") == "market_cap":
                        data[col_name] = data[col_name].apply(format_market_cap)
                    elif config.get("type") == "datetime":
                        data[col_name] = data[col_name].apply(
                            lambda x: format_datetime(x, config.get("format", "%b %d, %Y %H:%M"))
                        )
                    elif config.get("type") == "date":
                        data[col_name] = data[col_name].apply(
                            lambda x: format_date(x, config.get("format", "%b %d, %Y"))
                        )

        # Configure column display
        column_config = {}
        if columns_config:
            for col_name, config in columns_config.items():
                if col_name in data.columns:
                    column_config[col_name] = st.column_config.Column(
                        config.get("label", col_name),
                        help=config.get("help"),
                        width=config.get("width", "auto"),
                    )

        # Render the table
        dataframe_kwargs = {
            "data": data,
            "column_config": column_config if column_config else None,
            "width": width,
            "height": height,
            "hide_index": hide_index,
            "key": key,
            "selection_mode": selection_mode,
        }

        # Only add on_select if it's provided
        if on_select is not None:
            dataframe_kwargs["on_select"] = on_select

        result = st.dataframe(**dataframe_kwargs)

        return result
    except Exception as e:
        log_structured_error(e, page="tables", component="create_data_table", operation="render data table")
        st.error("Failed to render table.")
        return None


def create_sec_filings_table(
    filings_data: List[Dict[str, Any]],
    key: Optional[str] = None,
    on_select: Optional[Callable] = None
) -> Any:
    """
    Create a specialized table for SEC filings.

    Args:
        filings_data: List of filing dictionaries
        key: Unique key for the table
        on_select: Callback when row is selected

    Returns:
        Streamlit data editor object
    """
    try:
        df = pd.DataFrame(filings_data)

        # Define columns to show
        display_columns = [
            "filing_date", "company_name", "ticker", "form_type",
            "accession_number", "items"
        ]

        # Filter to available columns
        available_columns = [c for c in display_columns if c in df.columns]
        df = df[available_columns]

        columns_config = {
            "filing_date": {
                "label": "Filing Date",
                "type": "date",
                "width": "120",
            },
            "company_name": {
                "label": "Company",
                "width": "medium",
            },
            "ticker": {
                "label": "Ticker",
                "width": "small",
            },
            "form_type": {
                "label": "Form Type",
                "width": "small",
            },
            "accession_number": {
                "label": "Accession #",
                "width": "medium",
            },
            "items": {
                "label": "Items",
                "width": "medium",
                "help": "8-K items reported",
            },
        }

        return create_data_table(
            df,
            columns_config=columns_config,
            height=400,
            key=key,
            selection_mode="single",
            on_select=on_select,
        )
    except Exception as e:
        log_structured_error(e, page="tables", component="create_sec_filings_table", operation="render SEC filings table")
        st.error("Failed to render filings table.")
        return None


def create_market_data_table(
    market_data: List[Dict[str, Any]],
    key: Optional[str] = None
) -> Any:
    """
    Create a specialized table for market data.

    Args:
        market_data: List of market data dictionaries
        key: Unique key for the table

    Returns:
        Streamlit data editor object
    """
    try:
        df = pd.DataFrame(market_data)

        columns_config = {
            "ticker": {
                "label": "Ticker",
                "width": "small",
            },
            "company_name": {
                "label": "Company",
                "width": "medium",
            },
            "current_price": {
                "label": "Price",
                "type": "currency",
                "decimals": 2,
                "width": "small",
            },
            "price_change": {
                "label": "Change",
                "type": "currency",
                "decimals": 2,
                "width": "small",
            },
            "price_change_percent": {
                "label": "% Change",
                "type": "percentage",
                "decimals": 2,
                "width": "small",
            },
            "market_cap": {
                "label": "Market Cap",
                "type": "market_cap",
                "width": "small",
            },
            "pe_ratio": {
                "label": "P/E Ratio",
                "type": "number",
                "decimals": 2,
                "width": "small",
            },
            "avg_volume": {
                "label": "Avg Volume",
                "type": "volume",
                "width": "small",
            },
        }

        return create_data_table(
            df,
            columns_config=columns_config,
            height=500,
            key=key,
        )
    except Exception as e:
        log_structured_error(e, page="tables", component="create_market_data_table", operation="render market data table")
        st.error("Failed to render market data table.")
        return None


def create_news_table(
    articles: List[Dict[str, Any]],
    key: Optional[str] = None
) -> Any:
    """
    Create a specialized table for news articles.

    Args:
        articles: List of article dictionaries
        key: Unique key for the table

    Returns:
        Streamlit data editor object
    """
    try:
        df = pd.DataFrame(articles)

        columns_config = {
            "published_at": {
                "label": "Published",
                "type": "datetime",
                "format": "%b %d, %H:%M",
                "width": "small",
            },
            "headline": {
                "label": "Headline",
                "width": "large",
            },
            "source": {
                "label": "Source",
                "width": "small",
            },
            "category": {
                "label": "Category",
                "width": "small",
            },
            "related_tickers": {
                "label": "Tickers",
                "width": "medium",
            },
            "views": {
                "label": "Views",
                "type": "number",
                "decimals": 0,
                "width": "small",
            },
        }

        return create_data_table(
            df,
            columns_config=columns_config,
            height=500,
            key=key,
        )
    except Exception as e:
        log_structured_error(e, page="tables", component="create_news_table", operation="render news table")
        st.error("Failed to render news table.")
        return None


def render_sortable_header(
    columns: List[Dict[str, Any]],
    sort_column: str,
    sort_order: str,
    on_sort: Callable[[str], None]
):
    """
    Render a sortable table header.

    Args:
        columns: List of column definitions with 'key' and 'label'
        sort_column: Currently sorted column
        sort_order: Current sort order ('asc' or 'desc')
        on_sort: Callback when header is clicked
    """
    try:
        cols = st.columns([c.get("width", 1) for c in columns])

        for i, col in enumerate(columns):
            with cols[i]:
                is_sorted = sort_column == col["key"]
                sort_icon = "↓" if sort_order == "desc" else "↑" if is_sorted else ""

                button_style = "font-weight: 600;" if is_sorted else ""

                if st.button(
                    f"{col['label']} {sort_icon}",
                    key=f"sort_{col['key']}",
                    width="stretch",
                ):
                    on_sort(col["key"])
    except Exception as e:
        log_structured_error(e, page="tables", component="render_sortable_header", operation="render sortable header")
        st.error("Failed to render table header.")
