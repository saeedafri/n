"""
Market Size Forecasting.

Portal home for the research team's "Sales Forecast Intelligence" tool:
upload a spreadsheet of historical sales, and SARIMAX, SARIMA and Prophet each
forecast it with full validation and a residual diagnostic suite.

All modelling lives in `data/market_size_forecast_service`; this module is UI
only. Differences from the data team's standalone script are recorded in
docs/superpowers/specs/2026-08-28-market-size-forecasting-design.md.

Every model runs behind its own `@st.cache_data` keyed on exactly the inputs
that model depends on, so changing a Prophet prior refits Prophet alone and
leaves SARIMA and SARIMAX results in place.
"""

from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from utils.server_logger import new_rerun_id
new_rerun_id("market_size_forecasting")

from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from components.loading import inject_red_spinner_css, render_page_loader
from components.navigation import render_header, render_coresight_footer
from components.styles import hide_sidebar, render_styles, set_page_layout
from core.auth_manager import require_auth
from data import market_size_forecast_service as engine
from utils.server_logger import (
    PageLoadTracker, log_render_complete, log_structured_error, log_timing,
)

# Same palette as the Revenue Estimates page — these two are sibling tools.
BRAND_RED = "#D62E2F"
INK = "#2D2A29"
MUTED = "#6B7280"
SURFACE = "#FFFFFF"
BORDER = "#ECECEE"
GRID = "#EEF0F3"
SOFT = "#F9FAFB"

MODEL_COLORS = {"SARIMAX": "#D85A30", "SARIMA": "#1D9E75", "Prophet": "#7B5EA7"}
ACTUAL_COLOR = INK

# Every widget and cached artefact on this page is namespaced so a rerun on
# another page can never collide with it.
KEY = "msf_"

# ═══════════════════════════════════════════════════════════════════
#  STYLES
# ═══════════════════════════════════════════════════════════════════

def _page_css() -> None:
    st.markdown(
        f"""
<style>
/* Match the Revenue Estimates page: 110px left/right breathing room */
.block-container {{
    padding-left: 110px !important;
    padding-right: 110px !important;
    padding-top: 0 !important;
    padding-bottom: 0 !important;
    max-width: 1440px !important;
}}
.main .block-container {{
    padding-left: 110px !important;
    padding-right: 110px !important;
}}
h1 a[href], h2 a[href], h3 a[href], h4 a[href], h5 a[href], h6 a[href] {{ display: none !important; }}

@keyframes msf-rise {{
    from {{ opacity: 0; transform: translateY(10px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
}}

/* ── page header ───────────────────────────────────────────────── */
.msf-page-title {{
    color: {MUTED};
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin: 18px 0 4px;
}}
.msf-page-name {{
    color: {INK};
    font-size: 30px;
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.15;
    margin: 0 0 6px;
}}
.msf-page-copy {{
    color: {MUTED};
    font-size: 14px;
    line-height: 1.6;
    margin: 0 0 18px;
    max-width: 900px;
}}

/* ── toolbar + hero ────────────────────────────────────────────── */
.msf-toolbar-label {{
    color: {MUTED};
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin: 0 0 10px;
}}
.msf-hero {{
    background: linear-gradient(135deg, #ffffff 0%, #fcfcfd 60%, #f5f5f7 100%);
    border: 1px solid {BORDER};
    border-left: 5px solid {BRAND_RED};
    border-radius: 16px;
    padding: 14px 20px;
    margin: 4px 0 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    animation: msf-rise 0.34s cubic-bezier(0.22, 1, 0.36, 1) both;
}}
.msf-hero-lead {{
    font-size: 15px;
    font-weight: 700;
    color: {INK};
    white-space: nowrap;
}}
.msf-hero-divider {{
    width: 1px; height: 20px; background: {BORDER}; flex-shrink: 0;
}}
.msf-chip-row {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.msf-chip {{
    background: rgba(214, 46, 47, 0.07);
    border: 1px solid rgba(214, 46, 47, 0.15);
    border-radius: 999px;
    color: {INK};
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    font-weight: 700;
    padding: 5px 11px;
    letter-spacing: 0.01em;
}}
.msf-chip-quiet {{
    background: {SOFT};
    border-color: {BORDER};
    color: {MUTED};
}}

/* ── KPI cards ─────────────────────────────────────────────────── */
.msf-card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 18px;
    height: 132px;
    padding: 16px 18px 14px;
    box-shadow: 0 10px 28px rgba(17, 24, 39, 0.05);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: box-shadow 0.2s ease, background-color 0.2s ease, border-color 0.2s ease;
    animation: msf-rise 0.34s cubic-bezier(0.22, 1, 0.36, 1) both;
}}
.msf-card:hover {{
    background-color: rgba(214, 46, 47, 0.025);
    box-shadow: 0 14px 36px rgba(17, 24, 39, 0.10) !important;
    border-color: rgba(214, 46, 47, 0.3) !important;
}}
.msf-card:hover .msf-card-value {{ color: {BRAND_RED}; }}
.msf-card-kicker {{
    color: {MUTED};
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 10px;
}}
.msf-card-value {{
    color: {INK};
    font-size: 28px;
    font-weight: 800;
    line-height: 1.05;
    margin-bottom: 8px;
    transition: color 0.2s ease;
}}
.msf-card-value-sm {{ font-size: 19px; line-height: 1.2; }}
.msf-card-meta {{ color: {MUTED}; font-size: 13px; line-height: 1.45; }}
.msf-card-pass {{ color: #1B6B24; font-weight: 700; }}
.msf-card-fail {{ color: {BRAND_RED}; font-weight: 700; }}

/* ── sections ──────────────────────────────────────────────────── */
.msf-section-title {{
    color: {INK};
    font-size: 18px;
    font-weight: 800;
    margin: 26px 0 6px;
}}
.msf-section-copy {{
    color: {MUTED};
    font-size: 13px;
    line-height: 1.6;
    margin: 0 0 14px;
}}
.msf-spec {{
    background: {SOFT};
    border: 1px solid {BORDER};
    border-radius: 14px;
    padding: 16px 20px;
    margin: 4px 0 16px;
    font-size: 13.5px;
    line-height: 2;
}}
.msf-spec b {{
    color: {MUTED}; font-weight: 700; display: inline-block; width: 170px;
}}
.msf-drop {{
    border: 1.5px dashed #D8DADE;
    border-radius: 16px;
    padding: 30px 20px;
    text-align: center;
    color: {MUTED};
    background: {SOFT};
    margin: 6px 0 4px;
    font-size: 13.5px;
}}
.msf-drop b {{ display: block; color: {INK}; font-size: 15px; margin-bottom: 5px; font-weight: 700; }}

/* ── charts: reveal on paint, red hover crosshair ──────────────── */
div[data-testid="stPlotlyChart"] {{
    animation: msf-rise 0.42s cubic-bezier(0.22, 1, 0.36, 1) both;
    border: 1px solid {BORDER};
    border-radius: 16px;
    padding: 8px 6px 2px;
    background: {SURFACE};
    box-shadow: 0 10px 28px rgba(17, 24, 39, 0.04);
}}
/* ── section selector, styled as the portal's tabs ─────────────── */
button[data-testid="stBaseButton-segmented_control"],
button[data-testid="stBaseButton-segmented_controlActive"] {{
  border: none !important;
  background: transparent !important;
  border-radius: 0 !important;
  border-bottom: 2px solid transparent !important;
  color: {MUTED} !important;
  font-size: 14px !important;
  font-weight: 600 !important;
  padding: 9px 20px !important;
  box-shadow: none !important;
}}
button[data-testid="stBaseButton-segmented_control"]:hover {{
  color: {INK} !important;
  background: rgba(214,46,47,0.04) !important;
}}
button[data-testid="stBaseButton-segmented_controlActive"] {{
  color: {BRAND_RED} !important;
  border-bottom-color: {BRAND_RED} !important;
}}
div[data-testid="stDataFrame"] {{ animation: msf-rise 0.3s ease both; }}
</style>
""",
        unsafe_allow_html=True,
    )


def _section(title: str, subtitle: str = "") -> None:
    sub = f'<p class="msf-section-copy">{subtitle}</p>' if subtitle else ""
    st.markdown(f'<h3 class="msf-section-title">{title}</h3>{sub}', unsafe_allow_html=True)


def _card(kicker: str, value: str, meta: str = "", small: bool = False) -> None:
    size = " msf-card-value-sm" if small else ""
    st.markdown(
        f'<div class="msf-card">'
        f'<div class="msf-card-kicker">{kicker}</div>'
        f'<div class="msf-card-value{size}">{value}</div>'
        f'<div class="msf-card-meta">{meta}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _chips(*chips: str) -> str:
    return '<div class="msf-chip-row">' + "".join(chips) + "</div>"


def _chip(text: str, quiet: bool = False) -> str:
    return f'<span class="msf-chip{" msf-chip-quiet" if quiet else ""}">{text}</span>'


# ═══════════════════════════════════════════════════════════════════
#  CHARTS  — same conventions as the Revenue Estimates page
# ═══════════════════════════════════════════════════════════════════

CHART_CONFIG = {"displayModeBar": False, "responsive": True}


def _rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _style(fig: go.Figure, height: int = 420, yaxis_title: str = "",
           legend: str = "right", margin_right: int = 220) -> go.Figure:
    """House chart styling. Never sets an empty title — plotly.js draws the
    string "undefined" when `title` is passed as None."""
    if legend == "right":
        legend_cfg = dict(orientation="v", x=1.02, y=1.0, xanchor="left", yanchor="top",
                          bgcolor="rgba(255,255,255,0.95)", bordercolor=BORDER,
                          borderwidth=1, font=dict(size=11), title_text="")
        margin = dict(l=20, r=margin_right, t=20, b=18)
    else:
        legend_cfg = dict(orientation="h", x=0, y=1.02, xanchor="left", yanchor="bottom",
                          font=dict(size=11), title_text="")
        margin = dict(l=20, r=20, t=34, b=18)
    fig.update_layout(
        # autosize must be explicit: with only `height` set, plotly.js stops
        # auto-fitting width and bakes in its 700px default — which is what
        # every chart laid out inside a hidden tab was rendering at.
        autosize=True, height=height, margin=margin,
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        hovermode="x unified",
        legend=legend_cfg,
        font=dict(family="Inter, Roboto, Helvetica, Arial, sans-serif", size=12, color=MUTED),
        yaxis_title=yaxis_title,
        transition=dict(duration=350, easing="cubic-in-out"),
    )
    fig.update_xaxes(showgrid=False, linecolor=BORDER, ticks="outside", tickcolor=BORDER)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, zeroline=False, linecolor=BORDER)
    return fig


def _forecast_begins(fig: go.Figure, at) -> None:
    fig.add_shape(type="line", x0=at, x1=at, y0=0, y1=1, xref="x", yref="paper",
                  line=dict(color=MUTED, width=1.2, dash="dot"))
    fig.add_annotation(x=at, y=1, xref="x", yref="paper", text="Forecast begins",
                       showarrow=False, yshift=14, font=dict(size=11, color=MUTED),
                       bgcolor="rgba(255,255,255,0.92)")


def _forecast_chart(history: pd.Series, forecast: pd.DataFrame, color: str,
                    tail: int = 24) -> go.Figure:
    """History, forecast mean and the 95% band, split at the last actual."""
    recent = history.iloc[-tail:]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(forecast.index) + list(forecast.index)[::-1],
        y=list(forecast["mean_ci_upper"]) + list(forecast["mean_ci_lower"])[::-1],
        fill="toself", fillcolor=_rgba(color, 0.13), line=dict(width=0),
        mode="lines", hoverinfo="skip", name="95% confidence"))
    fig.add_trace(go.Scatter(
        x=recent.index, y=recent.values, mode="lines+markers", name="Actual",
        line=dict(color=ACTUAL_COLOR, width=3), marker=dict(color=ACTUAL_COLOR, size=6),
        hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=forecast.index, y=forecast["mean"], mode="lines+markers", name="Forecast",
        line=dict(color=color, width=3, dash="dash"), marker=dict(color=color, size=7),
        hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"))
    _forecast_begins(fig, recent.index[-1])
    return _style(fig, height=420, yaxis_title="Value")


def _history_chart(series: pd.Series) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=series.index, y=series.values, mode="lines", name="Actual",
        line=dict(color=BRAND_RED, width=2.4),
        fill="tozeroy", fillcolor=_rgba(BRAND_RED, 0.05),
        hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"))
    return _style(fig, height=300, yaxis_title="Value", legend="top", margin_right=20)


def _decomposition_chart(decomposition) -> go.Figure:
    from plotly.subplots import make_subplots
    parts = [("Observed", decomposition.observed, INK),
             ("Trend", decomposition.trend, BRAND_RED),
             ("Seasonal", decomposition.seasonal, "#0EA5E9"),
             ("Residual", decomposition.resid, "#9CA3AF")]
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.055,
                        subplot_titles=[p[0] for p in parts])
    for row, (name, series, color) in enumerate(parts, start=1):
        fig.add_trace(go.Scatter(x=series.index, y=series.values, mode="lines",
                                 line=dict(color=color, width=1.8), name=name,
                                 showlegend=False,
                                 hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"),
                      row=row, col=1)
    fig.update_annotations(font=dict(size=12, color=MUTED))
    fig = _style(fig, height=620, legend="top", margin_right=20)
    fig.update_layout(hovermode="x")
    return fig


def _residual_charts(resid) -> go.Figure:
    from plotly.subplots import make_subplots
    values = np.asarray(pd.Series(resid).dropna().values, dtype=float)
    fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.08,
                        subplot_titles=("Residuals over time", "Residual distribution"))
    fig.add_trace(go.Scatter(x=list(range(len(values))), y=values, mode="lines",
                             line=dict(color=ACTUAL_COLOR, width=1.4), showlegend=False,
                             hovertemplate="obs %{x}<br>%{y:,.0f}<extra></extra>"),
                  row=1, col=1)
    fig.add_hline(y=0, line=dict(color=BRAND_RED, width=1.2, dash="dash"), row=1, col=1)
    fig.add_trace(go.Histogram(x=values, nbinsx=28, marker=dict(color=BRAND_RED),
                               opacity=0.78, showlegend=False,
                               hovertemplate="%{x:,.0f}<br>%{y} obs<extra></extra>"),
                  row=1, col=2)
    fig.update_annotations(font=dict(size=12, color=MUTED))
    fig = _style(fig, height=320, legend="top", margin_right=20)
    fig.update_layout(hovermode="closest")
    return fig


TICK_FORMAT = {"Weekly": "%d %b %y", "Monthly": "%b %Y",
               "Quarterly": "%b %Y", "Annual": "%Y"}


def _pin_ticks(fig: go.Figure, index, freq_name: str) -> None:
    """Label the actual periods, not plotly's auto grid.

    Two month-end points otherwise get weekly tick labels (May 31, Jun 7, …),
    which reads as daily data.
    """
    try:
        values = pd.DatetimeIndex(pd.to_datetime(np.asarray(index)))
    except Exception:
        return
    if len(values) == 0 or len(values) > 26:
        fig.update_xaxes(tickformat=TICK_FORMAT.get(freq_name, "%b %Y"))
        return
    fig.update_xaxes(tickmode="array", tickvals=list(values),
                     ticktext=[v.strftime(TICK_FORMAT.get(freq_name, "%b %Y"))
                               for v in values])


def _walkforward_chart(actual, predicted, index, color: str,
                       freq_name: str = "Monthly") -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=index, y=np.asarray(actual, dtype=float),
                             mode="lines+markers", name="Actual",
                             line=dict(color=ACTUAL_COLOR, width=3),
                             marker=dict(size=7),
                             hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=index, y=np.asarray(predicted, dtype=float),
                             mode="lines+markers", name="Predicted",
                             line=dict(color=color, width=3, dash="dash"),
                             marker=dict(size=7),
                             hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"))
    fig = _style(fig, height=300, yaxis_title="Value", legend="top", margin_right=20)
    _pin_ticks(fig, index, freq_name)
    return fig


def _comparison_chart(results: Dict[str, Dict[str, Any]]) -> go.Figure:
    fig = go.Figure()
    history = next(iter(results.values()))["history"].iloc[-24:]
    fig.add_trace(go.Scatter(x=history.index, y=history.values, mode="lines+markers",
                             name="Actual", line=dict(color=ACTUAL_COLOR, width=3),
                             marker=dict(size=6),
                             hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"))
    for name, result in results.items():
        color = MODEL_COLORS.get(name, ACTUAL_COLOR)
        forecast = result["forecast"]
        fig.add_trace(go.Scatter(
            x=list(forecast.index) + list(forecast.index)[::-1],
            y=list(forecast["mean_ci_upper"]) + list(forecast["mean_ci_lower"])[::-1],
            fill="toself", fillcolor=_rgba(color, 0.08), line=dict(width=0),
            mode="lines", hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scatter(
            x=forecast.index, y=forecast["mean"], mode="lines+markers",
            name=f"{name} · MAPE {result['mape']:.2f}%",
            line=dict(color=color, width=3, dash="dash"), marker=dict(size=7),
            hovertemplate="%{x|%b %Y}<br>%{y:,.0f}<extra></extra>"))
    _forecast_begins(fig, history.index[-1])
    return _style(fig, height=460, yaxis_title="Value", margin_right=240)


# ═══════════════════════════════════════════════════════════════════
#  SMALL RENDER HELPERS
# ═══════════════════════════════════════════════════════════════════

def _table(df: pd.DataFrame, height: Optional[int] = None) -> None:
    st.dataframe(df, use_container_width=True,
                 height=height or min(420, 44 + len(df) * 35))


def _fmt_p(value) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "n/a"
    return f"{value:.4f}" if value >= 0.0001 else f"{value:.2e}"


def _verdict(ok: bool, good: str, bad: str) -> str:
    css = "msf-card-pass" if ok else "msf-card-fail"
    return f'<span class="{css}">{good if ok else bad}</span>'


def _to_xlsx(frame: pd.DataFrame) -> bytes:
    """One-sheet workbook in memory."""
    from io import BytesIO
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Forecast")
    return buffer.getvalue()


def _export_frame(forecast: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "Date": pd.to_datetime(forecast.index).strftime("%Y-%m-%d"),
        "Forecast": forecast["mean"].round(0).astype("int64"),
        "Lower_CI": forecast["mean_ci_lower"].round(0).astype("int64"),
        "Upper_CI": forecast["mean_ci_upper"].round(0).astype("int64"),
    })


def _export_buttons(frame: pd.DataFrame, stem: str) -> None:
    """CSV + Excel download for one forecast table.

    Forecast tables are a few kilobytes, so they ride the normal download
    button; the /media/ route exists for the big workbooks elsewhere.
    """
    csv_col, xlsx_col, _ = st.columns([1, 1, 4])
    csv_col.download_button(
        "Download CSV", frame.to_csv(index=False).encode("utf-8"),
        file_name=f"{stem}.csv", mime="text/csv",
        use_container_width=True, key=f"{KEY}csv_{stem}")
    try:
        xlsx_col.download_button(
            "Download Excel", _to_xlsx(frame),
            file_name=f"{stem}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True, key=f"{KEY}xlsx_{stem}")
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecasting._export_buttons",
                             operation="write_xlsx", context=stem)


# ═══════════════════════════════════════════════════════════════════
#  CACHED ENGINE WRAPPERS
#  Each is keyed on exactly what that stage depends on, so nudging a
#  Prophet prior does not refit SARIMA or re-pull FRED.
# ═══════════════════════════════════════════════════════════════════

CACHE_TTL = 3600


# The data science team's FRED key, shipped in config.txt inside the tool zip
# they distributed. Checked in deliberately so SARIMAX works without an app
# setting. It is a free, rate-limited key for public St. Louis Fed data — no
# personal data, no write access, no billing; the worst case is someone else
# consuming its request quota.
# Prefer the FRED_API_KEY app setting, which overrides this; once that is set on
# every environment, delete this constant.
FRED_KEY_FALLBACK = "c3784c667bb4cd3e9265152ff210bdd3"


def _fred_key() -> str:
    """The FRED key: what the user typed, then the app setting, then the default.

    The standalone tool read this from a config.txt next to the script. That
    file cannot survive here — wwwroot is replaced on every deploy.
    """
    return (st.session_state.get(f"{KEY}fred_key_input")
            or os.getenv("FRED_API_KEY", "")
            or FRED_KEY_FALLBACK).strip()


@st.cache_data(ttl=21600, show_spinner=False, max_entries=2)
def _fred_monthly(api_key: str):
    """FRED's monthly panel, cached six hours — none of it updates faster."""
    return engine.fetch_fred_monthly(api_key)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False, max_entries=8)
def _diagnostics(series: pd.Series, freq_name: str, season_mode: str):
    return engine.data_diagnostics(series, freq_name, season_mode)


def _through_disk(namespace: str, key_parts: tuple, compute):
    """Memory cache in front, disk cache behind.

    The in-process cache dies with the process, and STG redeploys several times
    a day — so a fitted model also lands on the persistent /home share. A second
    person opening the same series, or the same person after a deploy, gets it
    back instead of refitting.
    """
    key = hashlib.sha256(repr(key_parts).encode("utf-8")).hexdigest()[:24]
    cached = engine.disk_cache_get(namespace, key)
    if cached is not None:
        return cached
    value = compute()
    engine.disk_cache_put(namespace, key, value)
    return value


# NOTE: nothing below may call a Streamlit element. st.cache_data records and
# replays element calls, and replaying one that targets a block created outside
# the function raises — which is what a progress callback in here used to do.
@st.cache_data(ttl=CACHE_TTL, show_spinner=False, max_entries=8)
def _sarima(series: pd.Series, freq_name: str, horizon: int):
    return _through_disk(
        "sarima", (engine.series_fingerprint(series), freq_name, horizon),
        lambda: engine.run_sarima(series, freq_name, horizon))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False, max_entries=8)
def _prophet(series: pd.Series, freq_name: str, horizon: int, seasonality_mode: str,
             changepoint_prior: float, seasonality_prior: float):
    return _through_disk(
        "prophet", (engine.series_fingerprint(series), freq_name, horizon,
                    seasonality_mode, changepoint_prior, seasonality_prior),
        lambda: engine.run_prophet(series, freq_name, horizon, seasonality_mode,
                                   changepoint_prior, seasonality_prior))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False, max_entries=8)
def _granger(frame: pd.DataFrame, freq_name: str, max_lag: int):
    stationary, rounds, hit_cap = engine.difference_to_stationary(frame, freq_name)
    return engine.granger_scan(stationary, max_lag), rounds, hit_cap


@st.cache_data(ttl=CACHE_TTL, show_spinner=False, max_entries=8)
def _combinations(lagged: pd.DataFrame, candidates: List[str], max_lag: int,
                  max_combo: int, horizon: int, freq_name: str):
    return _through_disk(
        "combos", (engine.series_fingerprint(lagged[engine.TARGET_COL].dropna()),
                   tuple(candidates), max_lag, max_combo, horizon, freq_name),
        lambda: engine.rank_exog_combinations(lagged, candidates, max_lag, max_combo,
                                              horizon, freq_name))


@st.cache_data(ttl=CACHE_TTL, show_spinner=False, max_entries=16)
def _sarimax(lagged: pd.DataFrame, predvars: List[str], order, seasonal_order,
             last_actual, horizon: int, max_lag: int, freq_name: str):
    return engine.run_sarimax(lagged, predvars, order, seasonal_order, last_actual,
                              horizon, max_lag, freq_name)


# ═══════════════════════════════════════════════════════════════════
#  CONTROLS
# ═══════════════════════════════════════════════════════════════════

def _read_upload(upload) -> Optional[pd.DataFrame]:
    try:
        upload.seek(0)
        frame = engine.read_workbook(upload)
        upload.seek(0)
        return frame
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecasting._read_upload",
                             operation="read_workbook")
        st.error(f"Couldn't read that file: {exc}")
        return None


def _render_controls() -> Dict[str, Any]:
    """Upload plus every model setting, returned as one settings dict.

    The standalone tool put all of this in the sidebar; the portal hides the
    sidebar on every page, so it lives in the page body instead.
    """
    settings: Dict[str, Any] = {"ready": False}

    st.markdown('<div class="msf-toolbar-label">Sales history</div>', unsafe_allow_html=True)
    upload = st.file_uploader(
        "Sales history (Excel or CSV)", type=["xlsx", "xls", "csv"],
        key=f"{KEY}upload", label_visibility="collapsed",
        help=("One column of dates and one column of numeric values — names are "
              "detected automatically.\n\n"
              "Dates may be full (`2020-12-31`), month-year (`2020-01`, `Jan 2020`), "
              "year only (`2020`) or quarterly (`Q1 2020`, `2020Q1`). Keep one "
              "format throughout and avoid gaps."))
    if not upload:
        st.markdown(
            '<div class="msf-drop"><b>Upload a spreadsheet to begin</b>'
            'Monthly, weekly, quarterly or annual history — the columns and '
            'frequency are detected for you.</div>', unsafe_allow_html=True)
        return settings

    raw = _read_upload(upload)
    if raw is None or raw.empty:
        return settings

    guessed_date = engine.guess_date_column(raw)
    guessed_value = engine.guess_value_column(raw, guessed_date)
    if guessed_date is None or guessed_value is None:
        st.error(
            "Couldn't find both a date column and a numeric value column in this "
            f"file. Columns found: {list(raw.columns)}")
        return settings

    columns = list(raw.columns)
    col_date, col_value, col_freq = st.columns(3)

    date_options = [c for c in columns if c != guessed_value] or columns
    date_col = col_date.selectbox(
        "Date column", date_options,
        index=date_options.index(guessed_date) if guessed_date in date_options else 0,
        key=f"{KEY}date_col", help="Auto-detected — override if wrong")

    value_options = [c for c in columns if c != date_col] or columns
    value_col = col_value.selectbox(
        "Value column", value_options,
        index=value_options.index(guessed_value) if guessed_value in value_options else 0,
        key=f"{KEY}value_col", help="Auto-detected — override if wrong")

    try:
        parsed = engine.normalize_dates(raw[date_col]).dropna()
        detected_freq = engine.detect_frequency(pd.DatetimeIndex(parsed))
    except Exception:
        detected_freq = "Monthly"
    freq_name = col_freq.selectbox(
        "Frequency", engine.FREQ_NAMES,
        index=engine.FREQ_NAMES.index(detected_freq), key=f"{KEY}freq",
        help="Inferred from the spacing between your dates — override if wrong")

    config = engine.FREQ_CONFIGS[freq_name]

    st.markdown('<div class="msf-toolbar-label" style="margin-top:14px">'
                'Models &amp; parameters</div>', unsafe_allow_html=True)
    col_models, col_horizon, col_lag = st.columns([2, 1, 1])
    with col_models:
        run_sarimax = st.checkbox("SARIMAX  ·  FRED macro drivers", value=True,
                                  key=f"{KEY}m_sarimax")
        run_sarima = st.checkbox("SARIMA  ·  series only", value=True, key=f"{KEY}m_sarima")
        prophet_ready = engine.prophet_available()
        run_prophet = st.checkbox("Prophet  ·  trend and seasonality",
                                  value=prophet_ready, disabled=not prophet_ready,
                                  key=f"{KEY}m_prophet",
                                  help=None if prophet_ready
                                  else "Prophet is not installed in this environment.")
    horizon = col_horizon.slider(
        f"Forecast horizon ({config['unit']})", 1, config["max_horizon"],
        config["pred_default"], key=f"{KEY}horizon")
    max_lag = col_lag.slider("Max Granger lag", 2, config["max_lag"],
                             min(config["max_lag"], 12), key=f"{KEY}max_lag")

    if freq_name == "Weekly" and (run_sarima or run_sarimax):
        st.caption("Weekly data fits a 52-period seasonal model. Seasonal AR/MA "
                   "terms are only searched with at least 8 years of history — "
                   "below that the models keep seasonal differencing instead.")

    setting_left, setting_right = st.columns(2)
    with setting_left:
        with st.expander("SARIMAX settings"):
            max_exog = st.slider("Top N exogenous variables", 3, 10, 10, key=f"{KEY}max_exog")
            max_combo = st.slider("Max combination size", 1, 3, 1, key=f"{KEY}max_combo",
                                  help="1 = single variables (fast), 2 = pairs, 3 = triples (slow)")
            st.text_input(
                "FRED API key", type="password", key=f"{KEY}fred_key_input",
                help=("Only needed for SARIMAX. A shared key is built in; set the "
                      "FRED_API_KEY app setting to override it, or paste your own "
                      "here — free key at fred.stlouisfed.org."))
            if os.getenv("FRED_API_KEY", "").strip():
                st.caption("Using the FRED key configured for this environment.")
            elif FRED_KEY_FALLBACK:
                st.caption("Using the shared FRED key built into the app.")
    with setting_right:
        with st.expander("Prophet settings"):
            changepoint_prior = st.slider("Changepoint prior scale", 0.01, 0.5, 0.05,
                                          step=0.01, key=f"{KEY}cp_prior",
                                          help="Higher = more flexible trend")
            seasonality_prior = st.slider("Seasonality prior scale", 1, 20, 10,
                                          key=f"{KEY}s_prior",
                                          help="Higher = stronger seasonality")
            season_mode = st.selectbox("Seasonality mode",
                                       ["auto-detect", "additive", "multiplicative"],
                                       key=f"{KEY}season_mode")

    settings.update({
        "ready": True, "upload": upload, "raw": raw,
        "date_col": date_col, "value_col": value_col, "freq_name": freq_name,
        "detected_freq": detected_freq, "guessed_date": guessed_date,
        "guessed_value": guessed_value,
        "run_sarimax": run_sarimax, "run_sarima": run_sarima, "run_prophet": run_prophet,
        "horizon": horizon, "max_lag": max_lag, "max_exog": max_exog,
        "max_combo": max_combo, "changepoint_prior": changepoint_prior,
        "seasonality_prior": float(seasonality_prior), "season_mode": season_mode,
    })
    return settings


# ═══════════════════════════════════════════════════════════════════
#  ANALYSIS
# ═══════════════════════════════════════════════════════════════════

def _stage(bar, share: float, text: str) -> None:
    """Move the overall bar to the named stage."""
    bar.progress(min(max(share, 0.0), 1.0), text=text)


def _run_analysis(settings: Dict[str, Any], bar) -> Dict[str, Any]:
    """Run every selected model once and return everything the tabs render.

    Deliberately a single pass: Streamlit reruns the whole script on any widget
    change, so refitting per interaction would cost minutes. Each stage is
    cached independently, so a second run only redoes what actually changed.
    """
    started = perf_counter()
    freq_name = settings["freq_name"]
    horizon = settings["horizon"]
    config = engine.FREQ_CONFIGS[freq_name]
    stage_times: List[tuple] = []

    def timed(label, fn):
        mark = perf_counter()
        value = fn()
        stage_times.append((label, (perf_counter() - mark) * 1000))
        return value

    loaded = engine.load_series(settings["raw"], settings["date_col"],
                               settings["value_col"], freq_name)
    sales = loaded.frame
    series = sales[engine.TARGET_COL].dropna()
    if len(series) < 4:
        return {"error": f"Only {len(series)} usable rows after cleaning — too few to "
                         "model. Check the date and value columns."}
    last_actual = sales.index[-1]

    _stage(bar, 0.05, "Reading diagnostics")
    diagnostics = timed("diagnostics",
                        lambda: _diagnostics(series, freq_name, settings["season_mode"]))

    outcome: Dict[str, Any] = {
        "loaded": loaded, "series": series, "last_actual": last_actual,
        "diagnostics": diagnostics, "freq_name": freq_name, "horizon": horizon,
        "settings": settings, "results": {}, "notes": [], "fred_status": {},
    }

    selected = [m for m, on in (("SARIMAX", settings["run_sarimax"]),
                                ("SARIMA", settings["run_sarima"]),
                                ("Prophet", settings["run_prophet"])) if on]
    slice_size = 0.9 / max(len(selected), 1)
    cursor = 0.08

    # The FRED pull is 32 HTTPS round-trips to St. Louis and holds no GIL while
    # it waits. Start it now so it runs behind SARIMA and Prophet, and collect
    # it afterwards — on a cold cache that hides most of a ~8s wait.
    fred_pool = fred_future = None
    if settings["run_sarimax"] and len(series) >= engine.SARIMAX_MIN_OBS and _fred_key():
        _stage(bar, cursor, "Pulling FRED macro data")
        fred_pool = ThreadPoolExecutor(max_workers=1)
        api_key = _fred_key()
        script_ctx = get_script_run_ctx()

        def pull_fred():
            # st.cache_data needs the script context; attaching from inside the
            # worker avoids racing the thread's creation.
            add_script_run_ctx(threading.current_thread(), script_ctx)
            return _fred_monthly(api_key)

        fred_future = fred_pool.submit(pull_fred)

    # SARIMA and Prophet touch nothing in common, and Prophet spends its time
    # inside Stan, which releases the GIL — so Prophet rides along behind
    # SARIMA's grid search instead of queueing after it.
    if settings["run_sarima"] or settings["run_prophet"]:
        label = " and ".join(m for m in ("SARIMA", "Prophet")
                             if settings[f"run_{m.lower()}"])
        _stage(bar, cursor, f"Fitting {label}")
        script_ctx = get_script_run_ctx()

        def fit_sarima():
            add_script_run_ctx(threading.current_thread(), script_ctx)
            return _sarima(series, freq_name, horizon)

        def fit_prophet():
            add_script_run_ctx(threading.current_thread(), script_ctx)
            return _prophet(series, freq_name, horizon,
                            diagnostics["seasonality_mode"],
                            settings["changepoint_prior"],
                            settings["seasonality_prior"])

        jobs = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            if settings["run_sarima"]:
                jobs["SARIMA"] = ("sarima", pool.submit(fit_sarima))
            if settings["run_prophet"]:
                jobs["Prophet"] = ("prophet", pool.submit(fit_prophet))
            for model, (stage_name, future) in jobs.items():
                mark = perf_counter()
                try:
                    outcome["results"][model] = future.result()
                except Exception as exc:
                    log_structured_error(
                        exc, page="market_size_forecasting",
                        component="market_size_forecasting._run_analysis",
                        operation=f"run_{stage_name}")
                    outcome["notes"].append(
                        (model, f"{model} could not be fitted: {exc}"))
                stage_times.append((stage_name, (perf_counter() - mark) * 1000))
        cursor += slice_size * len(jobs)

    if settings["run_sarimax"]:
        _stage(bar, cursor, "SARIMAX")
        try:
            timed("sarimax", lambda: _run_sarimax_stage(
                outcome, sales, series, last_actual, settings, config, bar, cursor,
                slice_size, fred_future))
        finally:
            if fred_pool is not None:
                fred_pool.shutdown(wait=False)

    bar.progress(1.0, text="Done")
    total_ms = (perf_counter() - started) * 1000
    outcome["stage_times"] = stage_times
    outcome["total_ms"] = total_ms
    log_timing("MSF_RUN_ANALYSIS", total_ms,
               details=f"freq={freq_name} models={list(outcome['results'])} "
                       + " ".join(f"{k}={v:.0f}ms" for k, v in stage_times))
    return outcome


def _run_sarimax_stage(outcome: Dict[str, Any], sales: pd.DataFrame, series: pd.Series,
                       last_actual, settings: Dict[str, Any], config: Dict[str, Any],
                       bar, cursor: float, slice_size: float, fred_future=None) -> None:
    """FRED pull → stationarity → Granger → combination search → fit."""
    freq_name = settings["freq_name"]
    horizon = settings["horizon"]

    if len(series) < engine.SARIMAX_MIN_OBS:
        outcome["notes"].append((
            "SARIMAX",
            f"SARIMAX needs at least {engine.SARIMAX_MIN_OBS} data points "
            f"(this dataset has {len(series)}); it relies on macro variables and "
            "repeated differencing. SARIMA and Prophet still run."))
        return

    api_key = _fred_key()
    if not api_key:
        outcome["notes"].append((
            "SARIMAX",
            "SARIMAX needs a FRED API key. Add one under **SARIMAX settings**, or ask "
            "an admin to set the `FRED_API_KEY` app setting."))
        return

    _stage(bar, cursor, "SARIMAX — collecting FRED macro data")
    try:
        monthly, status = (fred_future.result() if fred_future is not None
                           else _fred_monthly(api_key))
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecasting._run_sarimax_stage",
                             operation="fetch_fred_monthly")
        outcome["notes"].append(("SARIMAX", f"FRED pull failed: {exc}"))
        return
    outcome["fred_status"] = status
    if not monthly:
        outcome["notes"].append((
            "SARIMAX",
            "No FRED variables could be downloaded — the API key is probably wrong or "
            "outbound access to api.stlouisfed.org is blocked. SARIMA and Prophet "
            "still run."))
        return

    frame = engine.build_model_frame(
        sales, freq_name,
        engine.align_fred(
            monthly,
            pd.date_range(sales.index[0], pd.Timestamp.today().date(), freq=config["freq"]),
            config["resample"]))

    _stage(bar, cursor + slice_size * 0.35, "SARIMAX — Granger causality")
    granger, rounds, hit_cap = _granger(frame, freq_name, settings["max_lag"])
    outcome["granger"] = granger
    outcome["sarimax_diffs"] = rounds
    outcome["sarimax_diff_capped"] = hit_cap
    if granger.empty:
        outcome["notes"].append((
            "SARIMAX",
            "No Granger-significant macro variables were found. Try raising **Max "
            "Granger lag**. SARIMA and Prophet still run."))
        return

    candidates = list(granger.sort_values("ftest_stat", ascending=False)
                      .iloc[:settings["max_exog"]]["xlabel"])
    lagged = engine.build_lagged_frame(frame, granger, last_actual, horizon, freq_name)

    _stage(bar, cursor + slice_size * 0.55, "SARIMAX — searching combinations")
    ranked = _combinations(lagged, candidates, settings["max_lag"],
                           settings["max_combo"], horizon, freq_name)
    if ranked.empty:
        outcome["notes"].append((
            "SARIMAX",
            "Every variable combination failed to fit. SARIMA and Prophet still run."))
        return

    outcome["sarimax_ranked"] = ranked
    outcome["sarimax_lagged"] = lagged
    outcome["sarimax_candidates"] = candidates
    _stage(bar, cursor + slice_size * 0.85, "SARIMAX — fitting the best combination")
    _fit_sarimax_rank(outcome, 0)


def _fit_sarimax_rank(outcome: Dict[str, Any], rank: int) -> None:
    """Fit SARIMAX at one rank of the combination table."""
    row = outcome["sarimax_ranked"].iloc[rank]
    try:
        outcome["results"]["SARIMAX"] = _sarimax(
            outcome["sarimax_lagged"], list(row["predvars"]), row["order"],
            row["seasonal_order"], outcome["last_actual"], outcome["horizon"],
            outcome["settings"]["max_lag"], outcome["freq_name"])
        outcome["sarimax_rank"] = rank
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecasting._fit_sarimax_rank",
                             operation="run_sarimax", context=f"rank={rank}")
        outcome["notes"].append(("SARIMAX", f"SARIMAX could not be fitted: {exc}"))


# ═══════════════════════════════════════════════════════════════════
#  TABS
# ═══════════════════════════════════════════════════════════════════

def _render_diagnostics_tab(outcome: Dict[str, Any]) -> None:
    diagnostics = outcome["diagnostics"]
    series = outcome["series"]
    config = engine.FREQ_CONFIGS[outcome["freq_name"]]

    _section("Pre-modelling diagnostics",
             "Stationarity, seasonality, outliers and decomposition — what the models "
             "are being asked to fit.")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _card("Records", f"{diagnostics['total_records']:,}",
              f"{diagnostics['date_start']:%b %Y} → {diagnostics['date_end']:%b %Y}")
    with c2:
        _card("Mean", f"{diagnostics['mean_sales']:,.0f}",
              f"Std dev {diagnostics['std_sales']:,.0f}")
    with c3:
        _card("Range", f"{diagnostics['min_sales']:,.0f}",
              f"up to {diagnostics['max_sales']:,.0f}", small=True)
    with c4:
        _card("Seasonal strength", f"{diagnostics['seasonal_strength']:.1f}%",
              f"Mode: {diagnostics['seasonality_mode']}")

    s1, s2, s3, s4 = st.columns(4)
    with s1:
        _card("ADF test", f"{diagnostics['adf_p']:.4f}",
              _verdict(diagnostics["adf_stationary"], "Stationary", "Non-stationary"))
    with s2:
        _card("KPSS test", f"{diagnostics['kpss_p']:.4f}",
              _verdict(diagnostics["kpss_stationary"], "Stationary", "Non-stationary"))
    period_avg = diagnostics["period_avg"]
    with s3:
        _card("Peak month", f"Month {period_avg.idxmax()}", f"Avg {period_avg.max():,.0f}")
    with s4:
        _card("Trough month", f"Month {period_avg.idxmin()}", f"Avg {period_avg.min():,.0f}")

    outliers = diagnostics["outliers"]
    if len(outliers):
        st.warning(f"{len(outliers)} outlier(s) detected (|z| > 3)")
        _table(outliers.rename("Value").to_frame())
    else:
        st.success("No outliers detected (|z| > 3)")

    loaded = outcome["loaded"]
    if loaded.duplicates_merged:
        st.info(f"{loaded.duplicates_merged} duplicate date(s) — rows sharing a period "
                "were summed so the series has one value per period.")
    if loaded.dropped_rows:
        st.info(f"{loaded.dropped_rows} row(s) had unreadable dates and were dropped.")

    _section("History", "The full uploaded series, after cleaning.")
    st.plotly_chart(_history_chart(series), config=CHART_CONFIG, key=f"{KEY}history")

    _section("Seasonal decomposition",
             "Trend, repeating seasonal shape and what is left over.")
    if diagnostics["decomposition"] is not None:
        st.plotly_chart(_decomposition_chart(diagnostics["decomposition"]),
                        config=CHART_CONFIG,
                        key=f"{KEY}decomp")
    elif config["decomp_period"] is None:
        st.info("Seasonal decomposition does not apply to annual data — there is no "
                "within-year cycle to separate out.")
    else:
        st.info("Not enough history for seasonal decomposition — it needs at least "
                f"two full cycles of {config['decomp_period']} periods.")

    status = outcome.get("fred_status") or {}
    if status:
        ok = [k for k, v in status.items() if v == "ok"]
        failed = {k: v for k, v in status.items() if v != "ok"}
        with st.expander(f"FRED data pull — {len(ok)}/{len(status)} variables loaded"):
            if failed:
                st.warning(f"{len(failed)} variable(s) failed")
                _table(pd.DataFrame({"Variable": list(failed),
                                     "Reason": list(failed.values())}))
            else:
                st.success("All FRED variables loaded")


def _render_residuals(resid, key: str):
    stats_ = engine.residual_diagnostics(resid)
    if stats_["too_short"]:
        st.warning(f"Only {stats_['n']} residuals — too few to run diagnostics. "
                   "A longer history gives reliable tests.")
        return stats_

    lags = stats_["lags"]
    failures = stats_["failures"]
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _card(f"Ljung-Box lag {lags[0]}", _fmt_p(stats_["lb_short"]),
              _verdict("lb_short" not in failures, "No autocorrelation", "Autocorrelated"))
    with c2:
        _card(f"Ljung-Box lag {lags[-1]}", _fmt_p(stats_["lb_long"]),
              _verdict("lb_long" not in failures, "No autocorrelation", "Autocorrelated"))
    with c3:
        _card("Shapiro-Wilk", _fmt_p(stats_["shapiro_p"]),
              _verdict("normality" not in failures, "Normal", "Non-normal"))
    with c4:
        dw = stats_["durbin_watson"]
        _card("Durbin-Watson", "n/a" if np.isnan(dw) else f"{dw:.4f}",
              _verdict("dw" not in failures, "Independent", "Serial correlation"))
    if lags != [10, 20]:
        st.caption(f"Short series ({stats_['n']} residuals) — Ljung-Box tested at lag "
                   f"{lags[0]}" + (f" and {lags[-1]}" if len(lags) > 1 else ""))

    st.plotly_chart(_residual_charts(resid), config=CHART_CONFIG,
                    key=f"{KEY}resid_{key}")
    return stats_


def _render_fix_suggestions(stats_: Dict[str, Any], model_name: str) -> None:
    """What failed, why it usually fails, and what to change."""
    failures = stats_.get("failures") or []
    if not failures:
        st.success("All residual diagnostics pass — the model is well specified.")
        return

    critical = [f for f in failures if f in ("lb_long", "dw")]
    with st.expander("What failed and what to try", expanded=True):
        if "lb_short" in failures and "lb_long" not in failures:
            st.warning("**Short-lag Ljung-Box fails, long lag passes** — mild "
                       "short-term autocorrelation only. Common, and usually has "
                       "little effect on forecast accuracy. Check MAPE first: under "
                       "3% this is generally acceptable.")
        if "lb_short" in failures and "lb_long" in failures:
            st.error("**Both Ljung-Box lags fail** — the model is missing a systematic "
                     "pattern and forecasts may be biased.")
        if "normality" in failures:
            st.warning("**Non-normal residuals** — usually COVID-period outliers "
                       "creating fat tails. Affects confidence-interval width more "
                       "than the point forecast.")
        if "dw" in failures:
            direction = "positive" if stats_["durbin_watson"] < 1.5 else "negative"
            st.error(f"**Durbin-Watson = {stats_['durbin_watson']:.4f}** — {direction} "
                     "serial correlation; consecutive residuals move together.")

        st.markdown("**Recommended changes**")
        if model_name == "SARIMAX":
            st.markdown(
                "- Step the **combination rank** up by one and compare — a different "
                "variable set often fits the short-term pattern better.\n"
                "- Raise **Max Granger lag** to admit longer lead-time relationships.\n"
                "- Raise **Max combination size** to 2 so pairs of variables are tried.")
        elif model_name == "SARIMA":
            st.markdown(
                "- Switch on **SARIMAX** — even one exogenous variable can absorb "
                "autocorrelation pure SARIMA cannot.\n"
                "- Non-normal residuals are expected here: without a COVID regressor "
                "SARIMA absorbs the 2020 shock into its residuals.")
        else:
            st.markdown(
                "- Raise **Changepoint prior scale** (0.05 → 0.10/0.15) for a more "
                "flexible trend.\n"
                "- Raise **Seasonality prior scale** (10 → 15/20) for stronger seasonality.\n"
                "- Flip **Seasonality mode** between additive and multiplicative.\n"
                "- Prophet failing at the short lag while the long lag passes, with "
                "MAPE under 2%, is a normal and acceptable result.")

        st.markdown("**Overall severity**")
        if not critical:
            st.success("Minor issues only — the model is usable. Apply a change if "
                       "MAPE is above 3%, otherwise proceed.")
        elif len(critical) == 1:
            st.warning("Moderate — at least one change is recommended before relying "
                       "on this forecast.")
        else:
            st.error("Multiple critical failures — review the specification or prefer "
                     "another model.")


def _render_accuracy(result: Dict[str, Any]) -> None:
    a, b, c = st.columns(3)
    with a:
        _card("Walk-forward MAPE", f"{result['mape']:.2f}%", "Mean absolute % error")
    with b:
        _card("RMSE", f"{result['rmse']:,.0f}", "In the series' own units")
    with c:
        _card("Accuracy", f"{100 - result['mape']:.1f}%", "1 − MAPE")


def _render_forecast_block(result: Dict[str, Any], outcome: Dict[str, Any],
                           color: str, stem: str) -> None:
    forecast = result["forecast"]
    st.plotly_chart(_forecast_chart(result["history"], forecast, color),
                    config=CHART_CONFIG, key=f"{KEY}fc_{stem}")
    table = forecast[["mean_ci_lower", "mean", "mean_ci_upper"]].round(0).reset_index()
    table.columns = ["Period", "Lower 95%", "Forecast", "Upper 95%"]
    table["Period"] = pd.to_datetime(table["Period"]).dt.strftime("%Y-%m-%d")
    _table(table)
    _export_buttons(_export_frame(forecast), stem)


def _render_walkforward(result: Dict[str, Any], color: str, key: str,
                        freq_name: str = "Monthly") -> None:
    _render_accuracy(result)
    actual = result["wf_actual"]
    predicted = result["wf_predicted"]
    # SARIMA/SARIMAX hand back a Series indexed by date; Prophet hands back a
    # separate `wf_dates` Series. Normalise both to a DatetimeIndex — a Series
    # has no .strftime, only .dt.strftime.
    raw_index = result.get("wf_dates")
    if raw_index is None:
        raw_index = getattr(actual, "index", None)
    index = (pd.DatetimeIndex(pd.to_datetime(np.asarray(raw_index)))
             if raw_index is not None else None)

    actual_values = np.asarray(actual, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    plot_index = index if index is not None else list(range(len(actual_values)))
    st.plotly_chart(_walkforward_chart(actual_values, predicted_values, plot_index,
                                       color, freq_name),
                    config=CHART_CONFIG, key=f"{KEY}wf_{key}")
    _table(pd.DataFrame({
        "Period": (index.strftime("%Y-%m-%d") if index is not None
                   else [str(i) for i in range(len(actual_values))]),
        "Actual": actual_values.round(0),
        "Predicted": predicted_values.round(0),
        "Error %": ((actual_values - predicted_values) / actual_values * 100).round(2),
    }))


def _render_sarimax_tab(outcome: Dict[str, Any]) -> None:
    for who, message in outcome["notes"]:
        if who == "SARIMAX":
            st.warning(message)
    if "SARIMAX" not in outcome["results"]:
        return

    if outcome["freq_name"] == "Weekly":
        st.info("FRED publishes monthly, so each macro value is forward-filled across "
                "the weeks of a month — the macro signal works at monthly resolution. "
                "SARIMA and Prophet may read week-level patterns better.")

    rounds = outcome.get("sarimax_diffs", 0)
    stationarity = (f"Stationary after {rounds} difference(s)"
                    if not outcome.get("sarimax_diff_capped")
                    else f"Differenced {rounds} times (the cap) — some macro series "
                         "never reach stationarity at this frequency, so treat the "
                         "rankings below as indicative")
    _section("Granger-significant variables",
             f"{stationarity}. Macro series whose past moves help predict this one.")
    granger = outcome["granger"]
    display = pd.DataFrame({
        "Variable (lag)": granger["xlabel"],
        "Lag": granger["lag"],
        "F statistic": granger["ftest_stat"].round(3),
        # Significant p-values here run to 1e-10; rounding shows them all as 0.
        "p-value": granger["ftest_pval"].apply(_fmt_p),
    })
    _table(display)

    ranked = outcome["sarimax_ranked"]
    _section(f"Combination search — {len(ranked)} fitted",
             "Every variable combination, ranked by error on a held-out window.")
    top = ranked[["predvars", "rmse", "mape"]].head(10).copy()
    top["predvars"] = top["predvars"].apply(lambda v: ", ".join(v))
    top.columns = ["Variables", "RMSE", "MAPE %"]
    _table(top.round({"RMSE": 0, "MAPE %": 2}))

    rank = st.number_input(
        "Combination rank (0 = lowest RMSE)", min_value=0,
        max_value=max(0, len(ranked) - 1), step=1, key=f"{KEY}combo_rank",
        help="Pick a different combination and the model is refitted below.")
    if rank != outcome.get("sarimax_rank"):
        with st.spinner("Refitting SARIMAX at the selected rank…"):
            _fit_sarimax_rank(outcome, int(rank))
        if "SARIMAX" not in outcome["results"]:
            return

    result = outcome["results"]["SARIMAX"]
    st.markdown(
        f'<div class="msf-spec">'
        f'<div><b>Variables</b>{", ".join(result["predvars"])}</div>'
        f'<div><b>Order</b>ARIMA{result["order"]}x{result["seasonal_order"]}</div>'
        f'<div><b>Real macro periods</b>{result["real_exog_periods"]} of '
        f'{outcome["horizon"]} — beyond that the exogenous terms fall back to a '
        f'neutral baseline</div></div>', unsafe_allow_html=True)

    _section("Walk-forward validation",
             "Accuracy on recent periods the model never saw during fitting.")
    _render_walkforward(result, MODEL_COLORS["SARIMAX"], "sarimax",
                        outcome["freq_name"])

    _section("Residual diagnostics", "Ljung-Box · Shapiro-Wilk · Durbin-Watson")
    stats_ = _render_residuals(result["residuals"], "sarimax")
    if stats_:
        _render_fix_suggestions(stats_, "SARIMAX")
    with st.expander("Full model summary"):
        st.text(result["summary"])

    _section("Forecast", f"Next {outcome['horizon']} "
                         f"{engine.FREQ_CONFIGS[outcome['freq_name']]['unit']}.")
    _render_forecast_block(result, outcome, MODEL_COLORS["SARIMAX"], "sarimax_forecast")


def _render_sarima_tab(outcome: Dict[str, Any]) -> None:
    for who, message in outcome["notes"]:
        if who == "SARIMA":
            st.error(message)
    if "SARIMA" not in outcome["results"]:
        return
    result = outcome["results"]["SARIMA"]

    _section("SARIMA",
             "Pure time series — no external data. The order is chosen automatically "
             "by grid search on AIC.")
    if result.get("seasonal_search") is False:
        cycles = result.get("cycles")
        st.info(
            f"Seasonal AR/MA terms were not searched: this series covers about "
            f"{cycles:.1f} seasonal cycles, and those terms need at least "
            f"{engine.MIN_CYCLES_FOR_SEASONAL_TERMS} to be estimated meaningfully. "
            "Seasonal differencing is still applied, so the annual pattern is "
            "still removed — and the fit is far quicker."
            if cycles else
            "Seasonal AR/MA terms were not searched — too few seasonal cycles.")
    st.markdown(
        f'<div class="msf-spec">'
        f'<div><b>Best order</b>ARIMA{result["order"]}x{result["seasonal_order"]}</div>'
        f'<div><b>AIC</b>{result["best_aic"]:,.2f}</div>'
        f'<div><b>Exogenous</b>None — history only</div></div>',
        unsafe_allow_html=True)

    _section("Walk-forward validation",
             "Accuracy on recent periods the model never saw during fitting.")
    _render_walkforward(result, MODEL_COLORS["SARIMA"], "sarima",
                        outcome["freq_name"])

    _section("Residual diagnostics", "Ljung-Box · Shapiro-Wilk · Durbin-Watson")
    stats_ = _render_residuals(result["residuals"], "sarima")
    if stats_:
        _render_fix_suggestions(stats_, "SARIMA")
    with st.expander("Full model summary"):
        st.text(result["summary"])

    _section("Forecast", f"Next {outcome['horizon']} "
                         f"{engine.FREQ_CONFIGS[outcome['freq_name']]['unit']}.")
    _render_forecast_block(result, outcome, MODEL_COLORS["SARIMA"], "sarima_forecast")


def _render_prophet_tab(outcome: Dict[str, Any]) -> None:
    for who, message in outcome["notes"]:
        if who == "Prophet":
            st.error(message)
    if "Prophet" not in outcome["results"]:
        return
    result = outcome["results"]["Prophet"]

    _section("Prophet",
             "Trend and seasonality with a COVID structural-break regressor. No FRED "
             "data is used here — only covid_shock (Mar–Jun 2020).")

    c1, c2, c3 = st.columns(3)
    with c1:
        _card("Seasonality mode", result["seasonality_mode"].title(), "Chosen from the data")
    with c2:
        _card("Changepoints", f"{len(result['changepoints'])}",
              f"{result['total_changepoints']} candidates detected")
    with c3:
        if result["cv_table"] is not None:
            improvement = result["cv_baseline_mape"] - result["cv_mape"]
            _card("Regressor value", f"{improvement:+.2f}pp",
                  _verdict(improvement > 0, "COVID regressor helps", "No improvement"))
        else:
            _card("Cross-validation", "Skipped", "Not enough history")

    if result["changepoints"]:
        _section("Changepoint analysis", "Where the underlying trend shifted.")
        _table(pd.DataFrame([
            {"Date": c["date"], "Delta": f"{c['delta']:+.4f}", "Direction": c["direction"]}
            for c in result["changepoints"]]))

    _section("Cross-validation", "Rolling-window accuracy across multiple cutpoints.")
    if result["cv_table"] is not None:
        a, b = st.columns(2)
        with a:
            _card("CV MAPE — with regressor", f"{result['cv_mape']:.2f}%",
                  "Rolling window across cutpoints")
        with b:
            _card("CV MAPE — baseline", f"{result['cv_baseline_mape']:.2f}%",
                  "Without covid_shock")
        _table(result["cv_table"][["horizon", "mape", "rmse", "mae"]]
               .round({"mape": 4, "rmse": 0, "mae": 0}).reset_index(drop=True))
    else:
        st.info("Cross-validation needs a longer history than this dataset provides, "
                "so it was skipped. Walk-forward validation below still ran.")

    _section("Walk-forward validation",
             "Accuracy on recent periods the model never saw during fitting.")
    _render_walkforward(result, MODEL_COLORS["Prophet"], "prophet",
                        outcome["freq_name"])

    _section("Residual diagnostics", "Ljung-Box · Shapiro-Wilk · Durbin-Watson")
    stats_ = _render_residuals(result["residuals"], "prophet")
    if stats_:
        _render_fix_suggestions(stats_, "Prophet")

    with st.expander("Trend and seasonality components"):
        try:
            import matplotlib
            matplotlib.use("Agg")            # headless container — no GUI backend
            import matplotlib.pyplot as plt
            model = result["components_model"]
            figure = model.plot_components(model.predict(
                model.history[["ds", "covid_shock"]]))
            st.pyplot(figure, use_container_width=True)
            plt.close(figure)
        except Exception as exc:
            log_structured_error(exc, page="market_size_forecasting",
                                 component="market_size_forecasting._render_prophet_tab",
                                 operation="plot_components")
            st.info("Component plot unavailable for this run.")

    _section("Forecast", f"Next {outcome['horizon']} "
                         f"{engine.FREQ_CONFIGS[outcome['freq_name']]['unit']}.")
    _render_forecast_block(result, outcome, MODEL_COLORS["Prophet"], "prophet_forecast")


def _render_comparison_tab(outcome: Dict[str, Any]) -> None:
    results = outcome["results"]
    config = engine.FREQ_CONFIGS[outcome["freq_name"]]

    _section("Model comparison",
             "Every model on identical terms — same held-out window, same error measure.")

    winner = min(results, key=lambda m: results[m]["mape"])
    cards = st.columns(len(results))
    for column, (name, result) in zip(cards, results.items()):
        with column:
            _card(name + ("  ·  BEST" if name == winner else ""),
                  f"{result['mape']:.2f}%",
                  f"RMSE {result['rmse']:,.0f} · {result['label']}")

    st.plotly_chart(_comparison_chart(results), config=CHART_CONFIG,
                    key=f"{KEY}compare")

    summary = pd.DataFrame({
        name: {
            "Walk-forward MAPE": f"{r['mape']:.2f}%",
            "Walk-forward RMSE": f"{r['rmse']:,.0f}",
            "Accuracy": f"{100 - r['mape']:.1f}%",
            "Uses FRED": "Yes" if name == "SARIMAX" else "No",
            "Exogenous": ("Granger-selected" if name == "SARIMAX"
                          else "None" if name == "SARIMA" else "covid_shock"),
            "Specification": r["label"],
        } for name, r in results.items()
    }).T
    _table(summary)

    _section("Period-by-period forecast",
             "Exact figures from each model, for reporting.")
    label_format = "%Y-%m-%d" if outcome["freq_name"] == "Weekly" else "%Y-%m"
    side_by_side = pd.DataFrame({
        name: pd.Series(result["forecast"]["mean"].round(0).values,
                        index=pd.to_datetime(result["forecast"].index).strftime(label_format))
        for name, result in results.items()
    })
    _table(side_by_side)

    every_model = pd.concat([
        _export_frame(result["forecast"]).assign(Model=name)[
            ["Date", "Model", "Forecast", "Lower_CI", "Upper_CI"]]
        for name, result in results.items()])
    _export_buttons(every_model, "market_size_forecast_all_models")

    spread = engine.forecast_spread(results)
    if spread < 5:
        st.success(f"Models agree to within **{spread:.1f}%** — confidence is high.")
    else:
        st.warning(f"Models diverge by up to **{spread:.1f}%** — worth investigating the "
                   "structural drivers before quoting a single number.")

    st.info(
        "**Reading this comparison**\n"
        "- Agreement within 5% → the midpoint is a reasonable working forecast\n"
        "- SARIMAX is driven by FRED macro signals — the one to use for scenarios\n"
        "- SARIMA is the stable baseline with no external dependencies\n"
        "- Prophet captures trend shifts and seasonality without macro data\n"
        f"- Quote a band over the next {outcome['horizon']} {config['unit']}, not a "
        "single number")


# ═══════════════════════════════════════════════════════════════════
#  PAGE
# ═══════════════════════════════════════════════════════════════════

def _hero(outcome: Dict[str, Any]) -> None:
    config = engine.FREQ_CONFIGS[outcome["freq_name"]]
    diagnostics = outcome["diagnostics"]
    chips = [
        _chip(f"{diagnostics['total_records']:,} periods"),
        _chip(f"{diagnostics['date_start']:%b %Y} → {diagnostics['date_end']:%b %Y}"),
        _chip(f"{outcome['freq_name']} · s={config['s']}", quiet=True),
        _chip(f"Horizon {outcome['horizon']} {config['unit']}", quiet=True),
    ]
    if outcome["results"]:
        best = min(outcome["results"], key=lambda m: outcome["results"][m]["mape"])
        chips.insert(0, _chip(f"Best: {best} · {outcome['results'][best]['mape']:.2f}% MAPE"))
    st.markdown(
        f'<div class="msf-hero">'
        f'<div class="msf-hero-lead">Forecast ready</div>'
        f'<div class="msf-hero-divider"></div>'
        f'{_chips(*chips)}</div>', unsafe_allow_html=True)


def _render_preview(settings: Dict[str, Any]) -> None:
    preview = settings["raw"]
    st.markdown(
        f'<div class="msf-hero">'
        f'<div class="msf-hero-lead">Ready to run</div>'
        f'<div class="msf-hero-divider"></div>'
        f'{_chips(_chip(f"{len(preview):,} rows"), _chip(f"Date: {settings["date_col"]}", quiet=True), _chip(f"Value: {settings["value_col"]}", quiet=True), _chip(f"{settings["freq_name"]} (auto: {settings["detected_freq"]})", quiet=True))}'
        f'</div>', unsafe_allow_html=True)
    _section("Data preview", "The last rows as read — check the columns look right.")
    _table(preview.tail(12))


def main() -> None:
    page_start = perf_counter()
    tracker = PageLoadTracker("market_size_forecasting")

    tracker.step_start("CHROME")
    hide_sidebar()
    render_styles()
    set_page_layout()
    inject_red_spinner_css()
    _page_css()
    require_auth(redirect_to="login", page="market_size_forecasting")
    render_header(full_width=True, current_page="market_size_forecasting")
    tracker.step_end("CHROME")

    st.markdown(
        '<div class="msf-page-title">Coresight Research · Analytics</div>'
        '<div class="msf-page-name">Market Size Forecasting</div>'
        '<p class="msf-page-copy">Upload a sales or market-size history and forecast it '
        'three ways — SARIMAX on FRED macro indicators, SARIMA on the series alone, and '
        'Prophet — each with walk-forward validation and a full residual diagnostic '
        'suite.</p>', unsafe_allow_html=True)

    tracker.step_start("CONTROLS")
    settings = _render_controls()
    tracker.step_end("CONTROLS")

    if not settings["ready"]:
        st.session_state.pop(f"{KEY}outcome", None)
        render_coresight_footer(full_width=True, stick_to_bottom=True)
        tracker.finish()
        return

    any_model = any((settings["run_sarimax"], settings["run_sarima"],
                     settings["run_prophet"]))
    if not any_model:
        st.warning("Select at least one model to run.")

    run_clicked = st.button("Run analysis", type="primary", key=f"{KEY}run",
                            disabled=not any_model)

    # Progress renders inside a container created on EVERY run, so the element
    # tree above the tabs never shifts — otherwise the tabs remount and the
    # first combination-rank change bounces the user back to Diagnostics.
    status_area = st.container()
    if run_clicked:
        tracker.step_start("ANALYSIS")
        loader = render_page_loader("Running forecast models")
        with status_area:
            bar = st.progress(0.0, text="Starting…")
            try:
                st.session_state[f"{KEY}outcome"] = _run_analysis(settings, bar)
            finally:
                bar.empty()
        loader.empty()
        tracker.step_end("ANALYSIS")

    outcome = st.session_state.get(f"{KEY}outcome")
    if not outcome:
        _render_preview(settings)
        render_coresight_footer(full_width=True, stick_to_bottom=True)
        tracker.finish()
        return

    if outcome.get("error"):
        st.error(outcome["error"])
        render_coresight_footer(full_width=True, stick_to_bottom=True)
        tracker.finish()
        return

    tracker.step_start("RENDER")
    _hero(outcome)

    ran = outcome["settings"]
    labels = ["Diagnostics"]
    if ran["run_sarimax"]:
        labels.append("SARIMAX")
    if ran["run_sarima"]:
        labels.append("SARIMA")
    if ran["run_prophet"]:
        labels.append("Prophet")
    if len(outcome["results"]) > 1:
        labels.append("Comparison")

    # A keyed selector rather than st.tabs. st.tabs keeps its selection only in
    # the browser, so any rerun that remounts it — a download button click does
    # exactly that — dropped the user back to Diagnostics and stayed there.
    # This also renders one section instead of all five: ~400ms of chart
    # building per rerun became ~100ms, and charts no longer lay out inside a
    # hidden container, which is what made plotly freeze them at 700px.
    if st.session_state.get(f"{KEY}section") not in labels:
        st.session_state[f"{KEY}section"] = labels[0]
    section = st.segmented_control(
        "Section", labels, key=f"{KEY}section", label_visibility="collapsed")
    section = section if section in labels else labels[0]

    renderers = {
        "Diagnostics": _render_diagnostics_tab,
        "SARIMAX": _render_sarimax_tab,
        "SARIMA": _render_sarima_tab,
        "Prophet": _render_prophet_tab,
        "Comparison": _render_comparison_tab,
    }
    renderers[section](outcome)
    tracker.step_end("RENDER")

    stage_times = outcome.get("stage_times") or []
    if stage_times:
        with st.expander(f"Timing — analysis {outcome.get('total_ms', 0):,.0f}ms", expanded=False):
            _table(pd.DataFrame(
                [{"Stage": label, "Milliseconds": round(ms)} for label, ms in stage_times]))
            st.caption("Each stage is cached on exactly its own inputs, so re-running "
                       "after changing one setting only refits what that setting "
                       "affects.")

    render_coresight_footer(full_width=True, stick_to_bottom=True)
    tracker.finish()
    log_render_complete("market_size_forecasting", perf_counter() - page_start)


main()
