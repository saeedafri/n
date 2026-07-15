"""
Reusable chart components using Plotly.
Consistent styling matching the design system.
"""
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import pandas as pd
from typing import List, Optional, Dict, Any
from datetime import date
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.models import MarketMetric
from components.styles import COLORS, TYPOGRAPHY
from utils.server_logger import log_structured_error, timed_render


# Chart configuration defaults
CHART_CONFIG = {
    "displayModeBar": True,
    "displaylogo": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}

CHART_LAYOUT = {
    "font": {"family": TYPOGRAPHY["font_family"]},
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "margin": {"l": 50, "r": 30, "t": 50, "b": 50},
}


def create_candlestick_chart(
    metrics: List[MarketMetric],
    title: str = "Price History",
    height: int = 500
) -> go.Figure:
    """
    Create an interactive candlestick chart.

    Args:
        metrics: List of market metrics with OHLC data
        title: Chart title
        height: Chart height in pixels

    Returns:
        Plotly Figure object
    """
    try:
        df = pd.DataFrame([
            {
                "date": m.date,
                "open": m.open_price,
                "high": m.high_price,
                "low": m.low_price,
                "close": m.close_price,
                "volume": m.volume,
            }
            for m in sorted(metrics, key=lambda x: x.date)
        ])

        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.7, 0.3],
            subplot_titles=(title, "Volume")
        )

        # Candlestick trace
        fig.add_trace(
            go.Candlestick(
                x=df["date"],
                open=df["open"],
                high=df["high"],
                low=df["low"],
                close=df["close"],
                name="OHLC",
                increasing_line_color=COLORS["chart_up"],
                decreasing_line_color=COLORS["chart_down"],
            ),
            row=1, col=1
        )

        # Volume bars
        colors = [
            COLORS["chart_up"] if df["close"].iloc[i] >= df["open"].iloc[i] else COLORS["chart_down"]
            for i in range(len(df))
        ]

        fig.add_trace(
            go.Bar(
                x=df["date"],
                y=df["volume"],
                name="Volume",
                marker_color=colors,
                opacity=0.7,
            ),
            row=2, col=1
        )

        # Layout
        fig.update_layout(
            **CHART_LAYOUT,
            height=height,
            showlegend=False,
            xaxis_rangeslider_visible=False,
            hovermode="x unified",
        )

        # Y-axis formatting
        fig.update_yaxes(title_text="Price ($)", row=1, col=1)
        fig.update_yaxes(title_text="Volume", row=2, col=1)
        fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
        fig.update_xaxes(rangeslider_visible=False, row=2, col=1)

        # Grid styling
        fig.update_xaxes(
            showgrid=True,
            gridwidth=1,
            gridcolor=COLORS["gray_200"],
        )
        fig.update_yaxes(
            showgrid=True,
            gridwidth=1,
            gridcolor=COLORS["gray_200"],
        )

        return fig
    except Exception as e:
        log_structured_error(e, page="charts", component="create_candlestick_chart", operation="building candlestick chart")
        return go.Figure()


def create_line_chart(
    data: pd.DataFrame,
    x_column: str,
    y_columns: List[str],
    title: str = "",
    height: int = 400,
    show_markers: bool = True
) -> go.Figure:
    """
    Create a multi-line chart.

    Args:
        data: DataFrame with data
        x_column: Column name for x-axis
        y_columns: List of column names for y-axes
        title: Chart title
        height: Chart height
        show_markers: Whether to show markers on lines

    Returns:
        Plotly Figure object
    """
    try:
        fig = go.Figure()

        colors = [
            COLORS["chart_primary"],
            COLORS["chart_secondary"],
            COLORS["chart_tertiary"],
            COLORS["chart_quaternary"],
        ]

        for i, col in enumerate(y_columns):
            fig.add_trace(go.Scatter(
                x=data[x_column],
                y=data[col],
                mode="lines+markers" if show_markers else "lines",
                name=col,
                line=dict(color=colors[i % len(colors)], width=2),
                marker=dict(size=6) if show_markers else None,
            ))

        fig.update_layout(
            **CHART_LAYOUT,
            title=title,
            height=height,
            hovermode="x unified",
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
            ),
        )

        fig.update_xaxes(
            showgrid=True,
            gridwidth=1,
            gridcolor=COLORS["gray_200"],
        )
        fig.update_yaxes(
            showgrid=True,
            gridwidth=1,
            gridcolor=COLORS["gray_200"],
        )

        return fig
    except Exception as e:
        log_structured_error(e, page="charts", component="create_line_chart", operation="building line chart")
        return go.Figure()


def create_bar_chart(
    data: pd.DataFrame,
    x_column: str,
    y_column: str,
    title: str = "",
    height: int = 400,
    horizontal: bool = False,
    color_column: Optional[str] = None
) -> go.Figure:
    """
    Create a bar chart.

    Args:
        data: DataFrame with data
        x_column: Column name for x-axis
        y_column: Column name for y-axis
        title: Chart title
        height: Chart height
        horizontal: Whether to create horizontal bars
        color_column: Optional column for color coding

    Returns:
        Plotly Figure object
    """
    try:
        if horizontal:
            fig = go.Figure(data=go.Bar(
                x=data[y_column],
                y=data[x_column],
                orientation="h",
                marker_color=COLORS["chart_primary"],
            ))
            fig.update_xaxes(title_text=y_column)
            fig.update_yaxes(title_text=x_column)
        else:
            colors = data[color_column].map({
                True: COLORS["chart_up"],
                False: COLORS["chart_down"],
            }) if color_column else COLORS["chart_primary"]

            fig = go.Figure(data=go.Bar(
                x=data[x_column],
                y=data[y_column],
                marker_color=colors,
            ))
            fig.update_xaxes(title_text=x_column)
            fig.update_yaxes(title_text=y_column)

        fig.update_layout(
            **CHART_LAYOUT,
            title=title,
            height=height,
        )

        return fig
    except Exception as e:
        log_structured_error(e, page="charts", component="create_bar_chart", operation="building bar chart")
        return go.Figure()


def create_pie_chart(
    data: pd.DataFrame,
    values_column: str,
    names_column: str,
    title: str = "",
    height: int = 400,
    hole: float = 0.4
) -> go.Figure:
    """
    Create a pie/donut chart.

    Args:
        data: DataFrame with data
        values_column: Column name for values
        names_column: Column name for segment names
        title: Chart title
        height: Chart height
        hole: Hole size (0 for pie, >0 for donut)

    Returns:
        Plotly Figure object
    """
    try:
        colors = [
            COLORS["chart_primary"],
            COLORS["chart_secondary"],
            COLORS["chart_tertiary"],
            COLORS["chart_quaternary"],
            COLORS["gray_400"],
            COLORS["gray_300"],
        ]

        fig = go.Figure(data=go.Pie(
            values=data[values_column],
            labels=data[names_column],
            hole=hole,
            marker_colors=colors,
            textinfo="label+percent",
            textposition="outside",
        ))

        fig.update_layout(
            **CHART_LAYOUT,
            title=title,
            height=height,
            showlegend=False,
        )

        return fig
    except Exception as e:
        log_structured_error(e, page="charts", component="create_pie_chart", operation="building pie chart")
        return go.Figure()


def create_area_chart(
    data: pd.DataFrame,
    x_column: str,
    y_column: str,
    title: str = "",
    height: int = 400,
    fill_color: Optional[str] = None
) -> go.Figure:
    """
    Create an area chart with gradient fill.

    Args:
        data: DataFrame with data
        x_column: Column name for x-axis
        y_column: Column name for y-axis
        title: Chart title
        height: Chart height
        fill_color: Custom fill color

    Returns:
        Plotly Figure object
    """
    try:
        fill_color = fill_color or COLORS["chart_primary"]

        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=data[x_column],
            y=data[y_column],
            fill="tozeroy",
            fillcolor=f"rgba(0, 102, 204, 0.1)",
            line=dict(color=fill_color, width=2),
            name=y_column,
        ))

        fig.update_layout(
            **CHART_LAYOUT,
            title=title,
            height=height,
            showlegend=False,
        )

        fig.update_xaxes(
            showgrid=True,
            gridwidth=1,
            gridcolor=COLORS["gray_200"],
        )
        fig.update_yaxes(
            showgrid=True,
            gridwidth=1,
            gridcolor=COLORS["gray_200"],
        )

        return fig
    except Exception as e:
        log_structured_error(e, page="charts", component="create_area_chart", operation="building area chart")
        return go.Figure()


def render_chart(
    fig: go.Figure,
    width: str = "stretch",
    key: Optional[str] = None
):
    """
    Render a Plotly chart in Streamlit with consistent configuration.

    Args:
        fig: Plotly Figure object
        width: "stretch" for full container width, "content" for fixed width
        key: Optional unique key for the chart
    """
    try:
        with timed_render("CHART_PLOTLY"):
            st.plotly_chart(
                fig,
                width=width,
                config=CHART_CONFIG,
                key=key,
            )
    except Exception as e:
        log_structured_error(e, page="charts", component="render_chart", operation="rendering plotly chart")
        st.error("Failed to render chart.")
