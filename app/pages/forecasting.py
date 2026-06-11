"""
Revenue Estimates page.

DB-backed forecasting cockpit. The page reads annual actual revenue from
staging tables, runs the six-model forecasting engine in Python, and never
writes back to the database.
"""

from __future__ import annotations

from time import perf_counter, time_ns
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from components.loading import inject_red_spinner_css
from components.navigation import render_header, render_coresight_footer
from components.styles import hide_sidebar, render_styles, set_page_layout
from core.access_control import AccessControlManager
from core.access_control import UserRolesManager
from core.auth_manager import get_current_user, require_auth
from data.forecast_admin_service import sync_forecast_for_ticker, sync_all_eligible, clear_revenue_forecast_caches
from data.forecast_refresh_service import (
    ensure_forecast_columns,
    backfill_company_info,
    get_refresh_table_data,
    send_model_refresh_email,
)
from data.revenue_forecast_service import RevenueForecastService
from utils.constants import get_currency_symbol
from utils.server_logger import log_structured_error, log_timing


BRAND_RED = '#D62E2F'
INK = '#2D2A29'
MUTED = '#6B7280'
SURFACE = '#FFFFFF'
BORDER = '#ECECEE'


def _render_excel_js_download(excel_bytes: bytes, filename: str, label: str = "Excel", auto_click: bool = False) -> None:
    """1-click JS Blob download — same pattern as market_data.py."""
    try:
        import base64
        from streamlit.components.v1 import html as _sthtml
        b64 = base64.b64encode(excel_bytes).decode("ascii")
        safe_name = filename.replace("'", "\\'").replace('"', '\\"')
        safe_label = label.replace("'", "\\'").replace('"', '\\"')
        auto_trigger = "window.addEventListener('load', function(){ setTimeout(dl, 100); });" if auto_click else ""
        btn_html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&icon_names=table" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{display:flex;justify-content:flex-end;align-items:center;height:52px;background:transparent;
  font-family:'Inter','Roboto',Helvetica,Arial,sans-serif;padding:0 20px;}}
button{{background:transparent;border:1px solid #D62E2F;color:#D62E2F;border-radius:4px;
  padding:7px 12px;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap;
  transition:background 0.15s,color 0.15s;letter-spacing:0.01em;
  display:flex;align-items:center;gap:6px;}}
button:hover{{background:#D62E2F;color:#fff;}}
button:active{{opacity:0.85;}}
.material-symbols-outlined{{font-variation-settings:'FILL' 0,'wght' 400,'GRAD' 0,'opsz' 24;font-size:18px;}}
</style></head><body>
<button onclick="dl()"><span class="material-symbols-outlined">table</span>&nbsp;&nbsp;{safe_label}</button>
<script>
var _d="{b64}";
function dl(){{
  try{{
    var bin=atob(_d),n=bin.length,u8=new Uint8Array(n);
    for(var i=0;i<n;i++) u8[i]=bin.charCodeAt(i);
    var blob=new Blob([u8],{{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}});
    var url=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=url; a.download="{safe_name}";
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(function(){{URL.revokeObjectURL(url);}},200);
  }}catch(e){{console.error("Excel download failed:",e);}}
}}
{auto_trigger}
</script></body></html>"""
        _sthtml(btn_html, height=52, scrolling=False)
    except Exception as e:
        log_structured_error(e, page="forecasting", component="_render_excel_js_download", operation="render_excel_download_button")
GRID = '#EEF0F3'
PALETTE = [
    '#F75442',
    '#F8983B',
    '#F9D338',
    '#45C662',
    '#38C4B3',
    '#3BBECE',
    '#3FB9E5',
    '#3B7CFA',
    '#6A43EF',
    '#C82EDB',
    '#F74D58',
    '#FA988E',
    '#FBC189',
    '#FBE588',
]

MODEL_LABELS = {
    'linear': 'Linear Regression',
    'cagr': 'CAGR',
    'exp_smoothing': 'Exponential Smoothing',
    'holt': "Holt's Linear Trend",
    'ma_trend': 'Moving Average Trend',
    'weighted_avg': 'Weighted Average Growth',
    'ensemble': 'Ensemble',
    'scenario_pessimistic': 'Pessimistic Scenario',
    'scenario_baseline': 'Baseline Scenario',
    'scenario_optimistic': 'Optimistic Scenario',
}

MODEL_FULL_LABELS = {
    'linear': 'Linear Regression',
    'cagr': 'Compound Annual Growth Rate',
    'exp_smoothing': 'Exponential Smoothing',
    'holt': "Holt's Linear Trend",
    'ma_trend': 'Moving Average Trend',
    'weighted_avg': 'Weighted Average Growth',
    'ensemble': 'Ensemble',
}

# Maps forecaster display strings → MODEL_FULL_LABELS keys
_METHOD_DISPLAY_TO_KEY = {
    'Linear Regression': 'linear',
    'CAGR': 'cagr',
    'Exp Smoothing': 'exp_smoothing',
    "Holt's Linear": 'holt',
    'MA Trend': 'ma_trend',
    'Weighted Avg': 'weighted_avg',
    'Ensemble': 'ensemble',
}

MODEL_BUSINESS_OVERVIEW = {
    'linear': (
        'Draws a straight trend line through all historical revenue and extends it forward. '
        'Best for companies that have grown by a consistent dollar amount each year. '
        'The shaded band shows the uncertainty range — the further out the forecast, the wider it gets.'
    ),
    'cagr': (
        'Calculates the single annual growth rate that would take revenue from the first year on record to the most recent year, then applies that same rate every future year. '
        'This is the growth rate most analysts and executives quote in earnings calls and investor decks. '
        'It is sensitive to the start and end years — an unusually high or low year at either end will shift the number.'
    ),
    'exp_smoothing': (
        'Uses the full revenue history but gives far more weight to recent years. '
        'A one-off bad year from a decade ago barely influences the result, while last year matters most. '
        'Good for businesses where recent performance is a better guide than long-run history.'
    ),
    'holt': (
        "Tracks two things at once — where revenue stands right now, and how fast it is changing. "
        "Both update every year, so the model can recover from a dip and resume a trend without being permanently dragged down. "
        "Often the most resilient model for companies with bumpy histories."
    ),
    'ma_trend': (
        'Averages only the last 3 years of growth rates and projects that forward. '
        'Very responsive to recent momentum — if the company has been on a strong run, this model will reflect that clearly. '
        'Tends to be the most optimistic when recent growth has been strong.'
    ),
    'weighted_avg': (
        'Uses the full history of annual growth rates but gives recent years more influence than older ones. '
        'Sits between Moving Average Trend (last 3 years only) and a simple average (all years treated equally). '
        'A balanced view that respects both recent momentum and long-run history.'
    ),
    'ensemble': (
        'The Ensemble is the final blended forecast — it averages the top 3 models, not all 6. '
        'Why top 3? Because every model is tested against real hidden years (backtesting). '
        'Some models perform well for one company and poorly for another. '
        'Only the 3 that made the most accurate predictions historically are included — the weaker models are excluded so they do not drag the estimate in the wrong direction. '
        'The chart below shows all 6 individual model lines (faded) alongside the Ensemble result (bold black) so you can see how the blend compares to each approach.'
    ),
}

MODEL_DASHES = {
    'linear': 'solid',
    'cagr': 'dash',
    'exp_smoothing': 'dot',
    'holt': 'dashdot',
    'ma_trend': 'longdash',
    'weighted_avg': 'longdashdot',
    'ensemble': 'solid',
}

MODEL_NOTES = {
    'linear': """\
**What it believes:** Revenue changes by roughly the same dollar amount each year — not the same percentage, the same absolute dollar direction.

**Core formula:**
```
Revenue = intercept + slope × year
```
`slope` = how many billion dollars the line rises per year.
`intercept` = the fitted starting value of that line.

**How the fit is computed (Ordinary Least Squares):**
The algorithm finds the slope and intercept that minimize the total squared distance between the fitted line and every historical data point:
```
minimize  Σ (actual_i − (intercept + slope × year_i))²
```
This has a closed-form solution — no iteration or gradient descent required:
```
slope     = Σ((year - mean_year)(sales - mean_sales)) / Σ((year - mean_year)²)
intercept = mean_sales - slope × mean_year
```

**Forecasting from the fit:**
```
forecast_h = intercept + slope × future_year
```

**R² (R-squared):** How well the line explains the history.
- R² = 1.0 → perfect fit
- R² = 0.0 → the line explains nothing
- R² > 0.9 → very strong linear trend

**90% prediction interval:** The chart shades an uncertainty band:
```
se    = standard error of the regression
band  = ±1.645 × se × √(1 + 1/n + (future_year - mean_year)² / Σ(year - mean_year)²)
```

**Best for:** Stable businesses with steady absolute-dollar growth.
**Struggles with:** Companies that compound by percentage; businesses with regime changes or recent acceleration.""",

    'cagr': """\
**What it believes:** One single steady percentage growth rate connects the first historical revenue to the last, and that same rate continues into the future.

**CAGR formula:**
```
CAGR = (last_sales / base_sales)^(1 / n) − 1
```
Where:
- `last_sales` = latest cleaned annual revenue
- `base_sales` = earliest cleaned annual revenue
- `n` = number of year-to-year intervals between them

**Forecasting:**
```
forecast_h = last_sales × (1 + CAGR)^h
```
`h` = number of years ahead (1, 2, 3…)

**Worked example:**
Revenue: 2021→100, 2022→110, 2023→121
CAGR = (121/100)^(1/2) − 1 = 0.10 = **10%**
2024 forecast = 121 × 1.10 = **133.1**

**The model stat shown:** The CAGR percentage itself. A CAGR of 8% means: if every year had grown at exactly 8%, you would arrive at the same end point.

**Best for:** Finance audiences who prefer one clean summary number; long-run stable compounders.
**Struggles with:** Companies where the first or last year was unusual (COVID dip, acquisition spike).""",

    'exp_smoothing': """\
**What it believes:** Recent years matter more than old years, but we should not chase every short-term spike. A smoothed version of the series is a better base than the raw data.

**Parameter α (alpha) = 0.3 (fixed in code):**
30% weight to the newest actual value, 70% weight to the previous smoothed level.

**Smoothing update — applied one year at a time:**
```
L₁ = first actual sales
Lₜ = α × actualₜ  +  (1 − α) × Lₜ₋₁
```
After processing all historical years, one simple trend term is extracted:
```
trend = L_last − L_prev
```

**Forecasting:**
```
forecast_h = L_last + h × trend
```

**Worked toy example (α = 0.3):**
Actuals: 100, 140, 130
L₁ = 100
L₂ = 0.3 × 140 + 0.7 × 100 = 42 + 70 = **112**
L₃ = 0.3 × 130 + 0.7 × 112 = 39 + 78.4 = **117.4**
trend = 117.4 − 112 = **5.4**
2024 forecast = 117.4 + 1 × 5.4 = **122.8**

**Best for:** Noisy series where one bad year should not dominate the forecast.
**Struggles with:** Structural shifts where the most recent direction is very different from recent history.""",

    'holt': """\
**What it believes:** The business has both a current revenue level AND a current direction of change (trend), and both should be updated separately every year.

**Parameters (fixed in code):**
- `α = 0.3` — how fast the level reacts to a new actual
- `β = 0.1` — how fast the trend reacts when the level shifts

**Starting values:**
```
level₁ = first sales value
trend₁ = second sales − first sales
```

**Update equations — applied one year at a time:**
```
levelₜ  =  α × actualₜ  +  (1 − α) × (levelₜ₋₁ + trendₜ₋₁)
trendₜ  =  β × (levelₜ − levelₜ₋₁)  +  (1 − β) × trendₜ₋₁
```

**Forecasting:**
```
forecast_h  =  level_last  +  h × trend_last
```

**Worked toy example (α=0.3, β=0.1):**
Actuals: 2021→10, 2022→11, 2023→13, 2024→14
level₁=10, trend₁=1
Step 2: level₂ = 0.3×11 + 0.7×(10+1) = 3.3+7.7=**11.0**, trend₂ = 0.1×(11-10)+0.9×1=**0.9**
Step 3: level₃ = 0.3×13 + 0.7×(11+0.9) = 3.9+8.33=**12.23**, trend₃ = 0.1×(12.23-11)+0.9×0.9=**0.93**
2025 forecast = 12.23 + 1×0.93 = **13.16**

**Why it usually wins:** It handles series that change direction — a business that dips then recovers benefits from the trend term adjusting down then back up.

**Best for:** Short and changing annual series; businesses with visible momentum shifts.
**Struggles with:** Very noisy data; single-year outlier shocks.""",

    'ma_trend': """\
**What it believes:** The last few years of growth rate are the best predictor of the near future. Older history matters less.

**Parameter window = 3 (fixed in code):**
Only the 3 most recent year-over-year growth rates are averaged.

**Step 1 — compute growth rates:**
```
growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁
```

**Step 2 — average the recent window:**
```
avg_growth = mean(growth rates for the last 3 years)
```

**Step 3 — compound from the latest actual:**
```
forecast₁ = last_sales × (1 + avg_growth)
forecast₂ = forecast₁ × (1 + avg_growth)
...
```

**Worked example (ANF real data):**
Recent growth rates:
2024 → 15.8%,  2025 → 15.6%,  2026 → 6.4%
avg_growth = (15.8 + 15.6 + 6.4) / 3 = **12.6%**
Latest actual = 5.266B
2027 forecast = 5.266 × 1.126 = **5.93B** → rounded to **5.9B** on the page

**Best for:** Companies with a clear recent trend that should continue.
**Struggles with:** Companies where the last 3 years were noisy or exceptional.""",

    'weighted_avg': """\
**What it believes:** All historical growth rates matter, but more recent years should get more influence. It is a recency-biased version of averaging all history.

**Step 1 — compute growth rates (same as MA Trend):**
```
growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁
```

**Step 2 — assign linearly increasing weights:**
```
weights = [1, 2, 3, ..., n]
```
The oldest rate gets weight 1, the newest gets weight n.

**Step 3 — compute weighted average growth:**
```
weighted_growth = Σ(weightᵢ × growthᵢ) / Σ(weightᵢ)
```

**Step 4 — compound from the latest actual:**
```
forecast_h = last_sales × (1 + weighted_growth)^h
```

**Worked toy example:**
Growth rates: 2%, 4%, 8%, 12%
Weights: 1, 2, 3, 4
weighted_growth = (1×2 + 2×4 + 3×8 + 4×12) / (1+2+3+4) = (2+8+24+48) / 10 = **8.2%**
vs simple average = (2+4+8+12)/4 = **6.5%** — recency bias pulls it higher.

**Best for:** Companies with building momentum where recent acceleration is real.
**Struggles with:** Situations where the recent pickup is noise rather than signal.""",

    'ensemble': """\
**What it is:** Not a new model. The ensemble is the average of the top 3 individual models chosen by backtesting MAPE.

**Selection rule:**
```
top3 = three models with the lowest backtest MAPE
```

**Computation for each future year:**
```
ensemble_h = (model_a_h + model_b_h + model_c_h) / 3
```

**Why blend instead of just using the winner?**
Even the best model can be overfit to the holdout period. Blending reduces variance — one model pulling high is offset by another pulling low.

**Worked example (ANF):**
Top 3 models: MA Trend (MAPE 2.25%), Weighted Avg (12.87%), CAGR (13.07%)
Their 2027 forecasts: 5.9B, 5.5B, 5.4B
Ensemble = (5.9 + 5.5 + 5.4) / 3 = **5.6B**

**If no backtest runs** (too little history), the code defaults to:
```
top3 = ['holt', 'cagr', 'weighted_avg']
```""",

    'scenario_pessimistic': """\
**What it is:** A planning path, not a model output. It uses the 25th percentile of all historical year-over-year growth rates.

**Formula:**
```
g = percentile_25(all historical growth rates)
scenario_h = last_sales × (1 + g)^h
```

**Interpretation:** If the company's future growth looks like its weaker historical years, this is where revenue ends up. Use for downside planning.""",

    'scenario_baseline': """\
**What it is:** A planning path using the 50th percentile (median) of historical growth rates.

**Formula:**
```
g = percentile_50(all historical growth rates)
scenario_h = last_sales × (1 + g)^h
```

**Interpretation:** If the company reverts to its median historical behavior, this is the middle-case path. Compare it to the ensemble — if they are far apart, the ensemble is leaning strongly on recent momentum.""",

    'scenario_optimistic': """\
**What it is:** A planning path using the 75th percentile of historical growth rates.

**Formula:**
```
g = percentile_75(all historical growth rates)
scenario_h = last_sales × (1 + g)^h
```

**Interpretation:** If the company's future growth looks like its stronger historical years, this is where revenue ends up. Use for upside planning or stress-testing assumptions.""",
}

SCENARIO_COLORS = {
    'scenario_pessimistic': '#FA988E',
    'scenario_baseline': '#38C4B3',
    'scenario_optimistic': '#45C662',
}

# Display currency for this page. Populated at runtime from the service payload.
_EST_DISPLAY_CURRENCY = "USD"

require_auth(page="forecasting")

hide_sidebar()
render_styles()
set_page_layout(
    header_full_width=True,
    footer_full_width=True,
    body_padding='0 20px',
    max_content_width='1440px',
    remove_top_padding=True,
    footer_at_bottom=True,
)


def _fmt_billions(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return '—'
    symbol = get_currency_symbol(_EST_DISPLAY_CURRENCY)
    sign = '-' if value < 0 else ''
    return f'{sign}{symbol}{abs(float(value)):,.2f}B'


def _fmt_mm(value: Optional[float]) -> str:
    """Value in millions (billions × 1000), 2 decimal places."""
    if value is None or pd.isna(value):
        return '—'
    return f'{float(value) * 1000:,.2f}'


def _fmt_money(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return '—'
    symbol = get_currency_symbol(_EST_DISPLAY_CURRENCY)
    sign = '-' if value < 0 else ''
    value = abs(float(value))
    if value >= 1_000_000_000:
        return f'{sign}{symbol}{value / 1_000_000_000:,.2f}B'
    if value >= 1_000_000:
        return f'{sign}{symbol}{value / 1_000_000:,.2f}M'
    return f'{sign}{symbol}{value:,.2f}'


def _fmt_pct(value: Optional[float], decimals: int = 1, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return '—'
    value = float(value)
    if signed:
        return f'{value:+.{decimals}f}%'
    return f'{value:.{decimals}f}%'


def _fmt_int(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return '—'
    return f'{int(value):,}'


def _fmt_year(value: Any) -> str:
    if value is None or pd.isna(value):
        return '—'
    if hasattr(value, 'strftime'):
        return value.strftime('%b %d, %Y')
    try:
        return str(int(value))
    except Exception:
        return str(value)


def _fmt_ms(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return '—'
    return f'{float(value):.0f}ms'


def _normalize_query_value(value: Any) -> Optional[str]:
    if isinstance(value, list):
        return value[0] if value else None
    if value is None:
        return None
    return str(value)


def _model_label(key: str) -> str:
    return MODEL_LABELS.get(key, key.replace('_', ' ').title())


def _model_color(key: str, index: int = 0) -> str:
    if key == 'ensemble':
        return '#6A43EF'
    if key in SCENARIO_COLORS:
        return SCENARIO_COLORS[key]
    if key in MODEL_LABELS:
        known = ['linear', 'cagr', 'exp_smoothing', 'holt', 'ma_trend', 'weighted_avg']
        if key in known:
            return PALETTE[known.index(key) % len(PALETTE)]
    return PALETTE[index % len(PALETTE)]


def _model_dash(key: str) -> str:
    return MODEL_DASHES.get(key, 'solid')


_ALL_SENTINEL    = "⬇ ALL COMPANIES"
_SELECT_SENTINEL = "__SELECT__"

def _resolve_initial_ticker(companies: List[Dict[str, Any]]) -> Optional[str]:
    if not companies:
        return None
    available = {row['ticker'] for row in companies} | {_ALL_SENTINEL}
    for candidate in (
        _normalize_query_value(st.query_params.get('ticker')),
        st.session_state.get('estimates_ticker'),
        st.session_state.get('active_ticker'),
    ):
        # Skip sentinels — we only resolve real tickers or ALL_SENTINEL from query param
        if candidate and candidate not in (_SELECT_SENTINEL,) and candidate in available:
            return candidate
    return companies[0]['ticker']


def _company_frame(payload: Dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame(payload.get('actual_rows', []))
    if df.empty:
        return df
    df = df.copy()
    df['year'] = pd.to_datetime(df['period_date'], errors='coerce').dt.year
    df['sales_billions'] = pd.to_numeric(df['total_revenue_billions'], errors='coerce')
    df['is_outlier'] = df['year'].isin(set(payload.get('outlier_years', [])))
    df['growth_pct'] = df['sales_billions'].pct_change() * 100
    return df.sort_values('year').reset_index(drop=True)


def _training_frame(payload: Dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame(payload.get('historical_rows', []))
    if df.empty:
        return df
    df = df.copy()
    df['year'] = pd.to_numeric(df['year'], errors='coerce')
    df['sales_billions'] = pd.to_numeric(df['sales'], errors='coerce')
    if 'growth_pct' in df.columns:
        df['growth_pct'] = pd.to_numeric(df['growth_pct'], errors='coerce')
    else:
        df['growth_pct'] = pd.NA
    if 'is_outlier' not in df.columns:
        df['is_outlier'] = False
    return df.dropna(subset=['year', 'sales_billions']).sort_values('year').reset_index(drop=True)


def _forecast_frame(payload: Dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame(payload.get('forecast_rows', []))
    if df.empty:
        return df
    df = df.copy()
    df['year'] = pd.to_numeric(df['year'], errors='coerce')
    return df.dropna(subset=['year']).sort_values('year').reset_index(drop=True)


def _backtest_frame(payload: Dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame(payload.get('backtest_rows', []))
    if df.empty:
        return df
    df = df.copy().sort_values('mape')
    df['Rank'] = range(1, len(df) + 1)
    return df


def _available_model_keys(forecast_df: pd.DataFrame) -> List[str]:
    if forecast_df.empty:
        return []
    ordered: List[str] = []
    for key in ['linear', 'cagr', 'exp_smoothing', 'holt', 'ma_trend', 'weighted_avg', 'ensemble']:
        if key in forecast_df.columns:
            ordered.append(key)
    extras = [col for col in forecast_df.columns if col not in ordered and col != 'year' and not col.startswith('scenario_')]
    return ordered + extras


def _available_scenario_keys(forecast_df: pd.DataFrame) -> List[str]:
    if forecast_df.empty:
        return []
    ordered = [key for key in ['scenario_pessimistic', 'scenario_baseline', 'scenario_optimistic'] if key in forecast_df.columns]
    extras = [col for col in forecast_df.columns if col.startswith('scenario_') and col not in ordered]
    return ordered + extras


def _render_card(title: str, value: str, meta: str) -> None:
    st.markdown(
        f'''
        <div class="rev-card">
            <div class="rev-card-kicker">{title}</div>
            <div class="rev-card-value">{value}</div>
            <div class="rev-card-meta">{meta}</div>
        </div>
        ''',
        unsafe_allow_html=True,
    )


def _inject_tab_js(main_tab_label: str, sub_tab_label: str = '', nonce: int = 0) -> None:
    """Inject JS via an iframe to programmatically click Streamlit tab buttons."""
    main_safe = (main_tab_label or '').replace('\\', '\\\\').replace("'", "\\'")
    sub_safe = (sub_tab_label or '').replace('\\', '\\\\').replace("'", "\\'")
    # Nonce forces Streamlit to re-execute the iframe script on repeat clicks.
    js = f"""<script>(function __estNav_{nonce}__(){{
        function norm(s) {{
            return (s || '').replace(/\\s+/g, ' ').trim();
        }}
        function clickTabByLabel(label) {{
            if (!label) return false;
            var tabs = window.parent.document.querySelectorAll('button[role="tab"]');
            label = norm(label);
            var best = null;
            var bestTop = 1e9;
            for (var i = 0; i < tabs.length; i++) {{
                var t = tabs[i];
                if (norm(t.textContent) !== label) continue;
                // Prefer the top-most matching tab (main tabs are higher on page than nested tabs).
                var r = t.getBoundingClientRect();
                if (r.top < bestTop) {{
                    bestTop = r.top;
                    best = t;
                }}
            }}
            if (best) {{
                best.click();
                return true;
            }}
            return false;
        }}
        // Tabs can be re-rendered; retry a few times to make this reliable.
        var main = '{main_safe}';
        var sub = '{sub_safe}';
        var attempts = 0;
        var maxAttempts = 30;
        var timer = setInterval(function() {{
            attempts++;
            var mainOk = clickTabByLabel(main);
            var subOk = true;
            if (sub) {{
                // Only try sub-tab after main tab is selected (so nested tabs exist).
                var selectedMain = null;
                var selectedTabs = window.parent.document.querySelectorAll('button[role=\"tab\"][aria-selected=\"true\"]');
                // Choose the top-most selected tab as the main selection.
                var selTop = 1e9;
                for (var j = 0; j < selectedTabs.length; j++) {{
                    var rt = selectedTabs[j].getBoundingClientRect();
                    if (rt.top < selTop) {{ selTop = rt.top; selectedMain = selectedTabs[j]; }}
                }}
                var selText = selectedMain ? norm(selectedMain.textContent) : '';
                if (selText === norm(main)) {{
                    subOk = clickTabByLabel(sub);
                }} else {{
                    subOk = false;
                }}
            }}
            if ((mainOk && (!sub || subOk)) || attempts >= maxAttempts) {{
                clearInterval(timer);
            }}
        }}, 200)
    )();</script>"""
    st.components.v1.html(js, height=0, scrolling=False, key=f"est_nav_{nonce}")


@st.dialog('Model Details', width='large')
def _model_popup(
    model_key: str,
    payload: Dict[str, Any],
    training_df: 'pd.DataFrame',
    actual_df: 'pd.DataFrame',
    forecast_df: 'pd.DataFrame',
    model_keys: List[str],
) -> None:
    model_full_name = MODEL_FULL_LABELS.get(model_key, _model_label(model_key))
    st.markdown(f"### {model_full_name}")
    overview = MODEL_BUSINESS_OVERVIEW.get(model_key, '')
    if overview:
        st.markdown(f'<p style="color:#6B7280;font-size:14px;line-height:1.6;">{overview}</p>', unsafe_allow_html=True)
    if not forecast_df.empty and model_key in forecast_df.columns:
        st.plotly_chart(
            _build_model_chart(training_df, actual_df, forecast_df, payload.get('raw_forecasts', {}), model_key, model_keys),
            use_container_width=True,
            config={'displayModeBar': False},
        )
        summary = _model_summary_metrics(model_key, payload, forecast_df)
        c1, c2, c3 = st.columns(3)
        with c1:
            _render_card('Forecast start', summary.get('start_year', '—'), 'First projected year')
        with c2:
            _render_card('First forecast', summary.get('first_value', '—'), f"{summary.get('vs_last_actual', '—')} vs last actual")
        with c3:
            _render_card('Forecast end', summary.get('last_value', '—'), f"Ends in {summary.get('end_year', '—')}")


def _render_css() -> None:
    st.markdown(
        f'''
        <style>
        /* Match market_data page: 110px left/right breathing room */
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
        /* Hide Streamlit's auto-anchor link icons on all headings */
        h1 a[href], h2 a[href], h3 a[href], h4 a[href], h5 a[href], h6 a[href] {{
            display: none !important;
        }}
        .stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
        .stMarkdown h4 a, .stMarkdown h5 a, .stMarkdown h6 a {{
            display: none !important;
        }}
        .rev-shell {{ margin: 16px 0 24px; }}
        .rev-toolbar {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 18px;
            padding: 16px 18px 12px;
            margin-bottom: 16px;
            box-shadow: 0 10px 28px rgba(17, 24, 39, 0.05);
        }}
        .rev-toolbar-label {{
            color: {MUTED};
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 8px;
        }}
        div[data-testid="stSlider"] {{
            padding-top: 2px;
            padding-bottom: 0px;
        }}
        div[data-testid="stSlider"] > div {{
            padding-top: 0;
        }}
        /* hero is now a compact data-quality strip, no title (header has it) */
        .rev-hero {{
            background: linear-gradient(135deg, #ffffff 0%, #fcfcfd 60%, #f5f5f7 100%);
            border: 1px solid {BORDER};
            border-left: 5px solid {BRAND_RED};
            border-radius: 16px;
            padding: 14px 20px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 16px;
        }}
        .rev-hero-company {{
            font-size: 15px;
            font-weight: 700;
            color: {INK};
            white-space: nowrap;
        }}
        .rev-hero-divider {{
            width: 1px;
            height: 20px;
            background: {BORDER};
            flex-shrink: 0;
        }}
        .rev-chip-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .rev-chip {{
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
        .rev-card {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 18px;
            height: 148px;
            padding: 16px 16px 14px;
            box-shadow: 0 10px 28px rgba(17, 24, 39, 0.05);
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            transition: box-shadow 0.2s ease, background-color 0.2s ease, border-color 0.2s ease;
        }}
        .rev-card:hover {{
            background-color: rgba(214, 46, 47, 0.025);
            box-shadow: 0 14px 36px rgba(17, 24, 39, 0.10) !important;
            border-color: rgba(214, 46, 47, 0.3) !important;
        }}
        .rev-card:hover .rev-card-value {{
            color: {BRAND_RED};
        }}
        .rev-card:hover .rev-card-value-sm {{
            color: {BRAND_RED};
        }}
        .rev-card-hoverable {{
            cursor: pointer;
            position: relative;
            transition: box-shadow 0.15s ease, border-color 0.15s ease, background-color 0.15s ease;
        }}
        /* Invisible button overlay — pulls the button up to cover the card above it */
        .element-container:has(.rev-best-card-anchor) + .element-container {{
            margin-top: -148px;
            height: 148px;
        }}
        .element-container:has(.rev-best-card-anchor) + .element-container button {{
            height: 148px !important;
            width: 100% !important;
            opacity: 0 !important;
            background: transparent !important;
            border: none !important;
            cursor: pointer !important;
        }}
        .rev-card-kicker {{
            color: {MUTED};
            font-size: 11px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 10px;
        }}
        .rev-card-value {{
            color: {INK};
            font-size: 30px;
            font-weight: 800;
            line-height: 1;
            margin-bottom: 8px;
            transition: color 0.2s ease;
        }}
        .rev-card-meta {{
            color: {MUTED};
            font-size: 13px;
            line-height: 1.5;
        }}
        .rev-card-value-sm {{
            color: {INK};
            font-size: 18px;
            font-weight: 800;
            line-height: 1.25;
            margin-bottom: 6px;
            transition: color 0.2s ease;
        }}
        .rev-model-btn-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 8px;
            margin-bottom: 4px;
        }}
        div[data-testid="stButton"] > button.rev-model-btn {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 10px;
            color: {INK};
            font-size: 13px;
            font-weight: 600;
            padding: 8px 14px;
            cursor: pointer;
            transition: background 0.12s, border-color 0.12s;
        }}
        div[data-testid="stButton"] > button.rev-model-btn:hover {{
            background: rgba(214, 46, 47, 0.06);
            border-color: {BRAND_RED};
            color: {BRAND_RED};
        }}
        .rev-section-title {{
            color: {INK};
            font-size: 18px;
            font-weight: 800;
            margin: 0 0 8px;
        }}
        .rev-section-title + .rev-section-copy,
        h3.rev-section-title {{
            margin-top: 0;
        }}
        .rev-section-copy {{
            color: {MUTED};
            font-size: 13px;
            line-height: 1.6;
            margin: 0 0 14px;
        }}
        /* ── Company header — EXACT same structure as market_data page ─────── */
        .est-header-section {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0;
            padding: 24px 0;
        }}
        .est-header-left {{
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}
        .est-header-right {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 6px;
            padding-top: 4px;
        }}
        .est-page-title {{
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            font-size: 20px;
            color: {BRAND_RED};
            letter-spacing: 0.5px;
            text-transform: uppercase;
            margin: 0;
            padding: 0;
        }}
        .est-company-name-row {{
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            font-size: 20px;
            color: {INK};
            display: flex;
            align-items: center;
            gap: 4px;
        }}
        .est-dropdown-arrow {{
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .est-dropdown-arrow svg {{
            width: 16px;
            height: 16px;
            stroke: {INK};
        }}
        .est-horizon-label {{
            font-family: 'Montserrat', sans-serif;
            font-weight: 700;
            font-size: 11px;
            color: {MUTED};
            letter-spacing: 0.06em;
            text-transform: uppercase;
            margin: 0;
        }}
        /* Keep label close, but avoid overlapping the slider's value text */
        .est-horizon-label {{
            margin-bottom: 8px;
        }}
        /* Invisible selectbox overlay — position-based (no aria-label dependency) */
        /* Targets the element-container immediately after the one holding our header */
        .element-container:has(.est-header-section) + .element-container {{
            margin-top: -40px !important;
            position: relative;
            z-index: 10;
        }}
        .element-container:has(.est-header-section) + .element-container div[data-testid="stSelectbox"] {{
            opacity: 0 !important;
            width: 400px !important;
        }}
        .element-container:has(.est-header-section) + .element-container div[data-testid="stSelectbox"] > div {{
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
        }}
        .element-container:has(.est-header-section) + .element-container div[data-testid="stSelectbox"] label {{
            display: none !important;
        }}
        .element-container:has(.est-header-section) + .element-container div[data-testid="stSelectbox"] > div > div {{
            height: 30px;
            cursor: pointer;
        }}
        /* ── Best Forecasting Model card hover via button overlay ─────────── */
        /* The invisible button sits in the NEXT .element-container and intercepts
           all pointer events. Use forward-looking :has() on the PRECEDING container
           to apply hover styles when that overlay button is being hovered. */
        .element-container:has(+ .element-container button:hover) .rev-best-card-anchor .rev-card-hoverable {{
            box-shadow: 0 14px 36px rgba(214, 46, 47, 0.18) !important;
            border-color: {BRAND_RED} !important;
            background-color: rgba(214, 46, 47, 0.03) !important;
        }}
        .element-container:has(+ .element-container button:hover) .rev-best-card-anchor .rev-card-value-sm {{
            color: {BRAND_RED} !important;
        }}
        .element-container:has(+ .element-container button:hover) .rev-best-card-anchor .rev-card-kicker {{
            color: {BRAND_RED} !important;
            opacity: 0.8;
        }}
        /* ── Explore models button grid ───────────────────────────────────── */
        /* The explore models section is the ONLY horizontal block on this page
           with 6+ columns. Target it structurally, independent of DOM depth. */
        [data-testid="stHorizontalBlock"]:has([data-testid="column"]:nth-child(6)) button,
        [data-testid="stHorizontalBlock"]:has([data-testid="stColumn"]:nth-child(6)) button {{
            height: 80px !important;
            min-height: 80px !important;
            max-height: 80px !important;
            width: 100% !important;
            white-space: normal !important;
            word-break: break-word !important;
            line-height: 1.35 !important;
            padding: 8px 12px !important;
            text-align: center !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            background: {SURFACE} !important;
            border: 1.5px solid {BORDER} !important;
            border-radius: 12px !important;
            color: {INK} !important;
            font-size: 13px !important;
            font-weight: 600 !important;
            cursor: pointer !important;
            transition: border-color 0.15s ease, background-color 0.15s ease,
                        color 0.15s ease, box-shadow 0.15s ease !important;
        }}
        [data-testid="stHorizontalBlock"]:has([data-testid="column"]:nth-child(6)) button:hover,
        [data-testid="stHorizontalBlock"]:has([data-testid="stColumn"]:nth-child(6)) button:hover {{
            border-color: {BRAND_RED} !important;
            background-color: rgba(214, 46, 47, 0.05) !important;
            color: {BRAND_RED} !important;
            box-shadow: 0 4px 16px rgba(214, 46, 47, 0.12) !important;
        }}
        [data-testid="stHorizontalBlock"]:has([data-testid="column"]:nth-child(6)) button p,
        [data-testid="stHorizontalBlock"]:has([data-testid="stColumn"]:nth-child(6)) button p {{
            margin: 0 !important;
            line-height: 1.35 !important;
        }}
        .rev-panel {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 10px 28px rgba(17, 24, 39, 0.04);
        }}
        .rev-mini-note {{
            color: {MUTED};
            font-size: 12px;
            line-height: 1.55;
            margin-top: 8px;
        }}
        div[data-testid="stSelectbox"] label {{
            color: {INK} !important;
            font-size: 13px !important;
            font-weight: 700 !important;
        }}
        /* ── Desktop section breathing room ─────────────────────────────── */
        @media (min-width: 1024px) {{
            .rev-hero {{ margin-bottom: 28px; }}
            h3.rev-section-title {{ margin-top: 36px; }}
            .rev-panel {{ margin-bottom: 8px; }}
        }}
        /* ── Responsive: tablet ──────────────────────────────────────────── */
        @media (max-width: 1024px) {{
            .block-container {{
                padding-left: 40px !important;
                padding-right: 40px !important;
            }}
            .main .block-container {{
                padding-left: 40px !important;
                padding-right: 40px !important;
            }}
        }}
        /* ── Responsive: mobile ─────────────────────────────────────────── */
        @media (max-width: 768px) {{
            .block-container {{
                padding-left: 16px !important;
                padding-right: 16px !important;
            }}
            .main .block-container {{
                padding-left: 16px !important;
                padding-right: 16px !important;
            }}
            .rev-title {{ font-size: 26px; }}
            .rev-card-value {{ font-size: 24px; }}
            .rev-hero {{ padding: 18px 16px 14px; }}
            [data-testid="stHorizontalBlock"]:has([data-testid="column"]:nth-child(6)) button,
            [data-testid="stHorizontalBlock"]:has([data-testid="stColumn"]:nth-child(6)) button {{
                height: 64px !important;
                min-height: 64px !important;
                max-height: 64px !important;
                font-size: 12px !important;
            }}
        }}
        /* ── Excel download button — red-bordered like Key Stats ────────── */
        button[data-testid="baseButton-secondary"][kind="secondary"]:has(p) {{
        }}
        div[data-testid="stButton"]:has(button[key="dl_all_btn"]) button,
        div[data-testid="stButton"]:has(button[key="dl_single_btn"]) button {{
            background: transparent !important;
            border: 1px solid {BRAND_RED} !important;
            color: {BRAND_RED} !important;
            border-radius: 4px !important;
            font-size: 13px !important;
            font-weight: 500 !important;
            padding: 7px 12px !important;
            transition: background 0.15s, color 0.15s !important;
        }}
        div[data-testid="stButton"]:has(button[key="dl_all_btn"]) button:hover,
        div[data-testid="stButton"]:has(button[key="dl_single_btn"]) button:hover {{
            background: {BRAND_RED} !important;
            color: #fff !important;
        }}
        /* ── Refresh Data button — red fill, hover = white + red border ─── */
        button[data-testid="stBaseButton-primary"] {{
            background: {BRAND_RED} !important;
            color: #FFFFFF !important;
            border: 1.5px solid {BRAND_RED} !important;
            border-radius: 6px !important;
            font-size: 14px !important;
            font-weight: 600 !important;
            transition: background 0.15s, color 0.15s, border-color 0.15s !important;
        }}
        button[data-testid="stBaseButton-primary"]:hover {{
            background: #FFFFFF !important;
            color: {BRAND_RED} !important;
            border: 1.5px solid {BRAND_RED} !important;
        }}
        </style>
        ''',
        unsafe_allow_html=True,
    )


def _build_overview_chart(
    training_df: pd.DataFrame,
    actual_df: pd.DataFrame,
    forecast_df: pd.DataFrame,
    model_keys: List[str],
    scenario_keys: List[str],
) -> go.Figure:
    fig = go.Figure()

    if not training_df.empty:
        fig.add_trace(
            go.Scatter(
                x=training_df['year'],
                y=training_df['sales_billions'],
                mode='lines+markers',
                name='Training actuals',
                line=dict(color=INK, width=3),
                marker=dict(color=INK, size=7),
                hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
            )
        )

    if not actual_df.empty:
        outliers = actual_df[actual_df['is_outlier']]
        if not outliers.empty:
            fig.add_trace(
                go.Scatter(
                    x=outliers['year'],
                    y=outliers['sales_billions'],
                    mode='markers',
                    name='Outliers excluded',
                    marker=dict(color='#F59E0B', size=11, symbol='x'),
                    hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                )
            )

    if not forecast_df.empty:
        for idx, key in enumerate(model_keys):
            if key not in forecast_df.columns:
                continue
            series = forecast_df[['year', key]].dropna()
            if series.empty:
                continue
            color = _model_color(key, idx)
            fig.add_trace(
                go.Scatter(
                    x=series['year'],
                    y=series[key],
                    mode='lines+markers',
                    name=_model_label(key),
                    line=dict(color=color, width=3 if key == 'ensemble' else 2.4, dash=_model_dash(key)),
                    marker=dict(color=color, size=7 if key == 'ensemble' else 6),
                    hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                )
            )

        for idx, key in enumerate(scenario_keys):
            if key not in forecast_df.columns:
                continue
            series = forecast_df[['year', key]].dropna()
            if series.empty:
                continue
            fig.add_trace(
                go.Scatter(
                    x=series['year'],
                    y=series[key],
                    mode='lines',
                    name=_model_label(key),
                    line=dict(color=_model_color(key, idx), width=2, dash='dot'),
                    hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                )
            )

    if not training_df.empty:
        last_train_year = int(training_df['year'].max())
        fig.add_shape(
            type='line',
            x0=last_train_year,
            x1=last_train_year,
            y0=0,
            y1=1,
            xref='x',
            yref='paper',
            line=dict(color=MUTED, width=1.2, dash='dot'),
        )
        fig.add_annotation(
            x=last_train_year,
            y=1,
            xref='x',
            yref='paper',
            text='Forecast begins',
            showarrow=False,
            yshift=14,
            font=dict(size=11, color=MUTED),
            bgcolor='rgba(255,255,255,0.92)',
        )

    fig.update_layout(
        height=480,
        margin=dict(l=20, r=220, t=20, b=18),
        paper_bgcolor='white',
        plot_bgcolor='white',
        hovermode='x unified',
        legend=dict(
            orientation='v',
            x=1.02,
            y=1.0,
            xanchor='left',
            yanchor='top',
            bgcolor='rgba(255,255,255,0.95)',
            bordercolor=BORDER,
            borderwidth=1,
            font=dict(size=11),
        ),
        xaxis_title='Fiscal year',
        yaxis_title='Revenue (USD, billions)',
    )
    fig.update_xaxes(showgrid=False, tickmode='linear', dtick=1)
    fig.update_yaxes(showgrid=True, gridcolor=GRID)
    return fig


def _build_model_chart(
    training_df: pd.DataFrame,
    actual_df: pd.DataFrame,
    forecast_df: pd.DataFrame,
    raw_forecasts: Dict[str, Dict[str, Any]],
    model_key: str,
    model_keys: List[str],
) -> go.Figure:
    fig = go.Figure()

    if not training_df.empty:
        fig.add_trace(
            go.Scatter(
                x=training_df['year'],
                y=training_df['sales_billions'],
                mode='lines+markers',
                name='Training actuals',
                line=dict(color=INK, width=3),
                marker=dict(color=INK, size=7),
                hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
            )
        )

    if not actual_df.empty:
        outliers = actual_df[actual_df['is_outlier']]
        if not outliers.empty:
            fig.add_trace(
                go.Scatter(
                    x=outliers['year'],
                    y=outliers['sales_billions'],
                    mode='markers',
                    name='Outliers excluded',
                    marker=dict(color='#F59E0B', size=11, symbol='x'),
                    hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                )
            )

    if not forecast_df.empty and model_key in forecast_df.columns:
        series = forecast_df[['year', model_key]].dropna()
        if not series.empty:
            color = _model_color(model_key)
            fig.add_trace(
                go.Scatter(
                    x=series['year'],
                    y=series[model_key],
                    mode='lines+markers',
                    name=_model_label(model_key),
                    line=dict(color=color, width=3.5),
                    marker=dict(color=color, size=8),
                    hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                )
            )

            if model_key == 'linear':
                raw = raw_forecasts.get(model_key, {})
                lower = raw.get('ci_lower') or []
                upper = raw.get('ci_upper') or []
                years = raw.get('years') or series['year'].tolist()
                if lower and upper and len(lower) == len(upper) == len(years):
                    fig.add_trace(
                        go.Scatter(
                            x=years,
                            y=upper,
                            mode='lines',
                            line=dict(width=0),
                            hoverinfo='skip',
                            showlegend=False,
                        )
                    )
                    fig.add_trace(
                        go.Scatter(
                            x=years,
                            y=lower,
                            mode='lines',
                            line=dict(width=0),
                            fill='tonexty',
                            fillcolor='rgba(214, 46, 47, 0.12)',
                            name='90% interval',
                            hoverinfo='skip',
                        )
                    )

    if model_key == 'ensemble' and not forecast_df.empty:
        individual_keys = [k for k in model_keys if k != 'ensemble']
        for idx, key in enumerate(individual_keys):
            if key not in forecast_df.columns:
                continue
            series = forecast_df[['year', key]].dropna()
            if series.empty:
                continue
            color = _model_color(key, idx)
            fig.add_trace(
                go.Scatter(
                    x=series['year'],
                    y=series[key],
                    mode='lines',
                    name=_model_label(key),
                    line=dict(color=color, width=1.6, dash=_model_dash(key)),
                    opacity=0.55,
                    hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                )
            )
        if 'ensemble' in forecast_df.columns:
            ens = forecast_df[['year', 'ensemble']].dropna()
            if not ens.empty:
                fig.add_trace(
                    go.Scatter(
                        x=ens['year'],
                        y=ens['ensemble'],
                        mode='lines+markers',
                        name='Ensemble (blended)',
                        line=dict(color='#111827', width=4),
                        marker=dict(color='#111827', size=8),
                        hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                    )
                )
    # Individual model charts show only their own forecast — no ensemble overlay

    if not training_df.empty:
        last_train_year = int(training_df['year'].max())
        fig.add_shape(
            type='line',
            x0=last_train_year,
            x1=last_train_year,
            y0=0,
            y1=1,
            xref='x',
            yref='paper',
            line=dict(color=MUTED, width=1.2, dash='dot'),
        )

    fig.update_layout(
        height=400,
        margin=dict(l=20, r=200, t=18, b=18),
        paper_bgcolor='white',
        plot_bgcolor='white',
        hovermode='x unified',
        legend=dict(
            orientation='v',
            x=1.02,
            y=1.0,
            xanchor='left',
            yanchor='top',
            bgcolor='rgba(255,255,255,0.95)',
            bordercolor=BORDER,
            borderwidth=1,
            font=dict(size=11),
        ),
        xaxis_title='Fiscal year',
        yaxis_title='Revenue (USD, billions)',
    )
    fig.update_xaxes(showgrid=False, tickmode='linear', dtick=1)
    fig.update_yaxes(showgrid=True, gridcolor=GRID)
    return fig


def _build_backtest_chart(backtest_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if backtest_df.empty:
        return fig
    sorted_df = backtest_df.sort_values('mape').copy()
    colors = [_model_color(str(row['method']), idx) for idx, (_, row) in enumerate(sorted_df.iterrows())]
    fig.add_trace(
        go.Bar(
            x=sorted_df['display'],
            y=sorted_df['mape'],
            marker_color=colors,
            text=[f"{value:.1f}%" for value in sorted_df['mape']],
            textposition='outside',
            hovertemplate='%{x}<br>MAPE: %{y:.1f}%<extra></extra>',
            name='MAPE',
        )
    )
    fig.add_hline(y=float(sorted_df['mape'].mean()), line_color=MUTED, line_width=1.2, line_dash='dot')
    fig.update_layout(
        height=360,
        margin=dict(l=20, r=20, t=18, b=18),
        paper_bgcolor='white',
        plot_bgcolor='white',
        showlegend=False,
        xaxis_title='Model',
        yaxis_title='MAPE %',
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, ticksuffix='%')
    return fig


def _forecast_table(forecast_df: pd.DataFrame, model_keys: List[str], scenario_keys: List[str]) -> pd.DataFrame:
    if forecast_df.empty:
        return pd.DataFrame()
    display = pd.DataFrame({'Forecast year': forecast_df['year'].astype(int)})
    for key in model_keys:
        if key in forecast_df.columns:
            display[_model_label(key)] = forecast_df[key].apply(_fmt_billions)
    for key in scenario_keys:
        if key in forecast_df.columns:
            display[_model_label(key)] = forecast_df[key].apply(_fmt_billions)
    return display


def _model_summary_metrics(model_key: str, payload: Dict[str, Any], forecast_df: pd.DataFrame) -> Dict[str, str]:
    summary = payload.get('summary', {})
    raw = payload.get('raw_forecasts', {}).get(model_key, {})
    result: Dict[str, str] = {}
    if forecast_df.empty or model_key not in forecast_df.columns:
        return result
    series = forecast_df[['year', model_key]].dropna()
    if series.empty:
        return result
    last_actual = summary.get('latest_actual_revenue_billions')
    first_value = float(series.iloc[0][model_key])
    last_value = float(series.iloc[-1][model_key])
    result['start_year'] = _fmt_year(series.iloc[0]['year'])
    result['end_year'] = _fmt_year(series.iloc[-1]['year'])
    result['first_value'] = _fmt_billions(first_value)
    result['last_value'] = _fmt_billions(last_value)
    if last_actual not in (None, 0):
        result['vs_last_actual'] = _fmt_pct(((first_value - float(last_actual)) / abs(float(last_actual))) * 100, signed=True)
    if raw.get('r_squared') is not None:
        result['special'] = f"R² {float(raw['r_squared']):.3f}"
    elif raw.get('cagr') is not None:
        result['special'] = f"CAGR {_fmt_pct(float(raw['cagr']) * 100)}"
    else:
        result['special'] = '—'
    return result


def _scenario_chart(
    forecast_df: pd.DataFrame,
    training_df: 'pd.DataFrame | None' = None,
    actual_df: 'pd.DataFrame | None' = None,
) -> go.Figure:
    fig = go.Figure()
    if forecast_df.empty:
        return fig

    # ── Historical actual points ─────────────────────────────────────────────
    if training_df is not None and not training_df.empty:
        fig.add_trace(
            go.Scatter(
                x=training_df['year'],
                y=training_df['sales_billions'],
                mode='lines+markers',
                name='Historical',
                line=dict(color=INK, width=2, dash='dot'),
                marker=dict(color=INK, size=6),
                hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
            )
        )
    if actual_df is not None and not actual_df.empty:
        outliers = actual_df[actual_df['is_outlier']]
        if not outliers.empty:
            fig.add_trace(
                go.Scatter(
                    x=outliers['year'],
                    y=outliers['sales_billions'],
                    mode='markers',
                    name='Outliers',
                    marker=dict(color='#E53E3E', size=10, symbol='x'),
                    hovertemplate='%{x}<br>%{y:,.2f}B (outlier)<extra></extra>',
                )
            )

    # ── Forecast begins vertical line ────────────────────────────────────────
    if training_df is not None and not training_df.empty:
        _last_yr = int(training_df['year'].max())
        fig.add_vline(
            x=_last_yr,
            line_width=1,
            line_dash='dot',
            line_color='#888',
            annotation_text='Forecast begins',
            annotation_position='top right',
            annotation_font_size=11,
            annotation_font_color='#888',
        )

    # ── Scenarios ────────────────────────────────────────────────────────────
    for key in ['scenario_pessimistic', 'scenario_baseline', 'scenario_optimistic']:
        if key not in forecast_df.columns:
            continue
        series = forecast_df[['year', key]].dropna()
        if series.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=series['year'],
                y=series[key],
                mode='lines+markers',
                name=_model_label(key),
                line=dict(color=_model_color(key), width=3, dash='dash'),
                marker=dict(size=7, color=_model_color(key)),
                hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
            )
        )
    if 'ensemble' in forecast_df.columns:
        ensemble = forecast_df[['year', 'ensemble']].dropna()
        if not ensemble.empty:
            fig.add_trace(
                go.Scatter(
                    x=ensemble['year'],
                    y=ensemble['ensemble'],
                    mode='lines',
                    name='Ensemble',
                    line=dict(color='#111827', width=3),
                    hovertemplate='%{x}<br>%{y:,.2f}B<extra></extra>',
                )
            )
    fig.update_layout(
        height=400,
        margin=dict(l=20, r=160, t=18, b=18),
        paper_bgcolor='white',
        plot_bgcolor='white',
        hovermode='x unified',
        legend=dict(
            orientation='v',
            x=1.02,
            y=1.0,
            xanchor='left',
            yanchor='top',
            bgcolor='rgba(255,255,255,0.95)',
            bordercolor=BORDER,
            borderwidth=1,
            font=dict(size=11),
        ),
        xaxis_title='Fiscal year',
        yaxis_title='Revenue (USD, billions)',
    )
    fig.update_xaxes(showgrid=False, tickmode='linear', dtick=1)
    fig.update_yaxes(showgrid=True, gridcolor=GRID)
    return fig


def _render_historical_stats(payload: Dict[str, Any], training_df: pd.DataFrame) -> None:
    """Historical data summary + growth stats table — matching notebook DATA SUMMARY output."""
    summary = payload.get('summary', {})
    outlier_years = set(payload.get('outlier_years', []))

    # ── Growth stats cards ──────────────────────────────────────────────────
    st.markdown('<h3 class="rev-section-title">Historical growth statistics</h3>', unsafe_allow_html=True)
    st.markdown(
        '<p class="rev-section-copy">Key growth metrics computed on the cleaned, outlier-excluded series — matching the notebook DATA SUMMARY output.</p>',
        unsafe_allow_html=True,
    )
    stat_cols = st.columns(6)
    stats_data = [
        ('CAGR',              _fmt_pct(summary.get('cagr_pct')),                 'Compound annual growth rate'),
        ('Avg Growth',        _fmt_pct(summary.get('avg_annual_growth_pct')),     'Arithmetic mean of YoY rates'),
        ('Median Growth',     _fmt_pct(summary.get('median_annual_growth_pct')),  '50th percentile of YoY rates'),
        ('Growth Volatility', _fmt_pct(summary.get('growth_volatility_pct')),     'Std deviation of YoY rates'),
        ('Best Year',         _fmt_pct(summary.get('max_growth_pct')),            'Highest single-year growth'),
        ('Worst Year',        _fmt_pct(summary.get('min_growth_pct')),            'Lowest single-year growth'),
    ]
    for col, (title, value, meta) in zip(stat_cols, stats_data):
        with col:
            _render_card(title, value, meta)

    # ── Historical data table with outlier markers ───────────────────────────
    st.markdown('<h3 class="rev-section-title" style="margin-top:20px;">Historical data</h3>', unsafe_allow_html=True)
    if not training_df.empty:
        hist = training_df[['year', 'sales_billions', 'growth_pct', 'is_outlier']].copy()
        hist['Year'] = hist['year'].astype(int).astype(str)  # string = left-aligned, narrow column
        hist['Revenue ($B)'] = hist['sales_billions'].apply(lambda v: f'${float(v):,.2f}B' if pd.notna(v) else '—')
        hist['YoY Growth'] = hist['growth_pct'].apply(lambda v: f'{v:+.2f}%' if pd.notna(v) else '—')
        hist['Status'] = hist['is_outlier'].map({True: '⚠ Outlier — excluded from models', False: '✓ Normal'})
        st.dataframe(
            hist[['Year', 'Revenue ($B)', 'YoY Growth', 'Status']],
            use_container_width=True,
            hide_index=True,
            column_config={
                'Year': st.column_config.TextColumn('Year', width='small'),
                'Revenue ($B)': st.column_config.TextColumn('Revenue ($B)'),
                'YoY Growth': st.column_config.TextColumn('YoY Growth', width='small'),
                'Status': st.column_config.TextColumn('Status'),
            },
        )
    else:
        st.info('Historical data not available.')


def _render_implied_growth_table(forecast_df: pd.DataFrame, payload: Dict[str, Any]) -> None:
    """Implied YoY growth rates on the ensemble forecast — matching notebook IMPLIED GROWTH RATES output."""
    summary = payload.get('summary', {})
    last_actual = summary.get('latest_actual_revenue_billions')
    if forecast_df.empty or 'ensemble' not in forecast_df.columns or last_actual is None:
        return

    st.markdown('<h3 class="rev-section-title" style="margin-top:20px;">Implied growth on ensemble forecast</h3>', unsafe_allow_html=True)
    st.markdown(
        '<p class="rev-section-copy">The year-over-year growth rate implied by the ensemble forecast — starting from the last actual revenue. This is the notebook "IMPLIED GROWTH RATES" table.</p>',
        unsafe_allow_html=True,
    )

    ens = forecast_df[['year', 'ensemble']].dropna().copy()
    all_values = [float(last_actual)] + ens['ensemble'].tolist()
    yoy = [(all_values[i + 1] / all_values[i] - 1) * 100 for i in range(len(all_values) - 1)]

    implied = pd.DataFrame({
        'Year': ens['year'].astype(int).tolist(),
        'Ensemble Forecast ($B)': [f'${float(v):,.2f}B' for v in ens['ensemble'].tolist()],
        'Implied YoY Growth': [f'{g:+.2f}%' for g in yoy],
    })
    st.dataframe(implied, use_container_width=True, hide_index=True)


def _render_model_panel(
    model_key: str,
    payload: Dict[str, Any],
    training_df: pd.DataFrame,
    actual_df: pd.DataFrame,
    forecast_df: pd.DataFrame,
    model_keys: List[str],
) -> None:
    model_name = _model_label(model_key)
    model_full_name = MODEL_FULL_LABELS.get(model_key, model_name)
    summary = _model_summary_metrics(model_key, payload, forecast_df)
    if model_key not in forecast_df.columns:
        st.info(f'{model_name} is not available in this payload yet.')
        return
    overview = MODEL_BUSINESS_OVERVIEW.get(model_key, '')
    title_col, info_col = st.columns([14, 1])
    with title_col:
        st.markdown(f"<h3 class='rev-section-title'>{model_full_name}</h3>", unsafe_allow_html=True)
    with info_col:
        note = MODEL_NOTES.get(model_key, '')
        if overview or note:
            with st.popover('ℹ️'):
                st.markdown(f'<h4 style="margin:0 0 6px;color:#2D2A29;">{model_full_name}</h4>', unsafe_allow_html=True)
                if overview:
                    st.markdown(
                        f'<p style="color:#6B7280;font-size:13px;line-height:1.65;margin:0 0 12px;">{overview}</p>',
                        unsafe_allow_html=True,
                    )
                if note:
                    st.divider()
                    st.markdown(note)
    st.plotly_chart(
        _build_model_chart(training_df, actual_df, forecast_df, payload.get('raw_forecasts', {}), model_key, model_keys),
        use_container_width=True,
        config={'displayModeBar': False},
    )
    cols = st.columns(3)
    with cols[0]:
        _render_card('Forecast start', summary.get('start_year', '—'), 'First projected year')
    with cols[1]:
        _render_card('First forecast', summary.get('first_value', '—'), f"{summary.get('vs_last_actual', '—')} vs last actual")
    with cols[2]:
        _render_card('Forecast end', summary.get('last_value', '—'), f"Ends in {summary.get('end_year', '—')}")


def _render_data_source_panel(payload: Dict[str, Any]) -> None:
    summary = payload.get('summary', {})
    source_columns = summary.get('source_columns', [])
    st.markdown(
        '<div class="rev-panel">'
        '<p class="rev-section-copy" style="margin-bottom: 0;">'
        'The page is read-only. It never inserts or updates rows in staging. It reads actual revenue, runs the models in memory, and renders the results.'
        '</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.write('')
    left, right = st.columns(2)
    with left:
        st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
        st.markdown('<h3 class="rev-section-title">SEC actuals path</h3>', unsafe_allow_html=True)
        st.markdown(
            """
            - Table: `coreiq_av_financials_income_statement`
            - Columns used: `fiscal_date_ending`, `total_revenue`, `gross_profit`, `operating_income`, `net_income`, `ebitda`, `reported_currency`, `report_type`
            - Filter: `report_type = 'annual'`
            - Purpose: build the annual revenue series for SEC companies
            """,
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)
    with right:
        st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
        st.markdown('<h3 class="rev-section-title">YFinance actuals path</h3>', unsafe_allow_html=True)
        st.markdown(
            """
            - Table: `coreiq_yf_financials_income_statement`
            - Columns used: `period_end`, `frequency`, `line_item`, `value`
            - Filter: `frequency = 'annual'`
            - Pivot rule: keep `line_item = 'Total Revenue'` as the revenue series and pivot the row set into one annual line per company
            - Purpose: support non-SEC companies with the same forecasting engine
            """,
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)
    st.write('')
    st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
    st.markdown('<h3 class="rev-section-title">Company mapping and labels</h3>', unsafe_allow_html=True)
    st.markdown(
        """
        - `coreiq_companies` supplies the `ticker` and `source` split (`SEC` or `YFinance`)
        - `coreiq_av_companies_all` provides the human-readable company name when available
        - The page uses the source split to choose the correct actual table before forecasting starts
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="rev-mini-note">Timing logs: `FORECAST_GET_COMPANIES`, `FORECAST_LOAD_ACTUALS`, `FORECAST_SUMMARY_STATS`, `FORECAST_BACKTEST`, `FORECAST_FIT`, `FORECAST_TOTAL`.</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    if source_columns:
        st.write('')
        st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
        st.markdown('<h3 class="rev-section-title">Resolved source columns</h3>', unsafe_allow_html=True)
        st.code('\n'.join(f'- {col}' for col in source_columns), language='text')
        st.markdown('</div>', unsafe_allow_html=True)


def _render_documentation_tab(summary: Dict[str, Any]) -> None:
    st.markdown('<h3 class="rev-section-title">How the forecasting engine works — full reference</h3>', unsafe_allow_html=True)
    st.markdown(
        '<p class="rev-section-copy">This tab explains every concept, keyword, and formula used on this page from the ground up. No prior ML or statistics knowledge required.</p>',
        unsafe_allow_html=True,
    )

    with st.expander('🧩 Glossary — every term used on this page', expanded=False):
        st.markdown(
            """
**actuals** — Real historical revenue numbers from the database. Not estimated, not forecast. The source of truth that all models learn from.

**forecast** — A future revenue estimate produced by one model. It is a best guess based on a mathematical rule applied to the actuals. Not a guaranteed outcome.

**model** — One specific mathematical method or rule for projecting future revenue. This page runs six models simultaneously: Linear Regression, CAGR, Exponential Smoothing, Holt's Linear Trend, Moving Average Trend, Weighted Average Growth.

**training data** — The cleaned historical revenue series passed into each model. On this page: the annual revenue values after outlier removal.

**training** — The process of fitting a model's parameters to the historical data. For the models on this page, training is computed in closed form (no iterative optimization). Examples: Linear Regression computes slope/intercept once; CAGR computes a single percentage from first and last values.

**outlier** — A year whose year-over-year growth rate was unusually far from the historical pattern. Detected with z-scores (see below). Outlier years are kept visible in the UI but removed from model fitting.

**z-score** — A measure of how many standard deviations a value is from the average. `z = (value − mean) / std_dev`. Years with |z| > 2.0 are flagged as outliers.

**CAGR** — Compound Annual Growth Rate. One steady annual percentage that, if compounded every year, would take revenue from the first observed value to the last. Formula: `(last/first)^(1/n) − 1`.

**slope** — In Linear Regression, the dollar amount the fitted line rises per year. A slope of 0.3 means the model expects revenue to grow by $0.3B each year.

**intercept** — In Linear Regression, the starting value of the fitted line in the equation `Revenue = intercept + slope × year`.

**R²** — R-squared. How well the linear fit explains the historical data. R² = 1.0 means perfect fit; R² = 0.0 means the line explains nothing.

**alpha (α)** — A smoothing parameter (0 to 1). Used in Exponential Smoothing and Holt's method. Higher alpha = more weight to the newest data point; lower alpha = smoother, slower-reacting series.

**beta (β)** — A trend smoothing parameter used only in Holt's method. Controls how fast the trend component reacts to a change in level. Lower beta = smoother trend.

**level** — In Holt's method, the current estimated revenue position (like a moving baseline). Updated every year using alpha.

**trend** — In Holt's method, the current estimated direction and speed of change. Updated every year using beta.

**window** — In Moving Average Trend, the number of recent growth rates to average. Default: 3. So the model uses only the last 3 yearly growth rates.

**weight** — Importance assigned to a value. In Weighted Average Growth, weights are 1, 2, 3, …, n — so older growth rates get less influence and the newest gets the most.

**backtest** — A simulation of past forecasting. The engine hides the most recent 2 years, trains each model on earlier history, and checks how well each model predicted those hidden years.

**holdout** — The hidden years used in backtesting. Default: 2 years. If history is short (≤ 4 years), falls back to 1 holdout year.

**MAPE** — Mean Absolute Percentage Error. Average percentage miss across holdout years. Lower = better. `MAPE = mean(|predicted − actual| / actual) × 100`.

**Bias** — Whether a model tended to over- or under-predict. `Bias = mean(predicted − actual)`. Positive = over-predicted; negative = under-predicted.

**RMSE** — Root Mean Squared Error. Average miss with larger misses penalized more. `RMSE = sqrt(mean((predicted − actual)²))`.

**ensemble** — The average of the top 3 models by backtest MAPE. Blending reduces the risk of a single model being wrong in one direction.

**scenario** — A planning path derived from historical growth percentiles, not from model fitting. Pessimistic = 25th percentile growth; Baseline = 50th percentile; Optimistic = 75th percentile.

**percentile** — A position in a sorted list. 25th percentile = bottom quarter; 50th = middle; 75th = top quarter.

**standard deviation** — How spread out a set of values is. If annual growth rates are always close to each other, std dev is small. If they swing wildly, std dev is large.

**OLS** — Ordinary Least Squares. The algorithm used by Linear Regression to find the best-fit line by minimizing the sum of squared differences between the line and the data points.
            """,
        )

    with st.expander('📐 All formulas in one place', expanded=False):
        st.markdown(
            """
#### Outlier detection
```
growth_t   = (sales_t − sales_(t-1)) / sales_(t-1)
z_score_t  = (growth_t − mean_growth) / std_growth
outlier    = |z_score| > 2.0
```

#### Linear Regression (OLS)
```
slope      = Σ((year_i − mean_year)(sales_i − mean_sales)) / Σ((year_i − mean_year)²)
intercept  = mean_sales − slope × mean_year
forecast_h = intercept + slope × future_year
R²         = r_value²  (from scipy.stats.linregress)
```
90% prediction interval:
```
se    = regression standard error
band  = ±1.645 × se × √(1 + 1/n + (future_year − mean_year)² / Σ(year − mean_year)²)
```

#### CAGR
```
CAGR       = (last_sales / base_sales)^(1/n) − 1
forecast_h = last_sales × (1 + CAGR)^h
```

#### Exponential Smoothing (α = 0.3)
```
L_1  = first actual sales
L_t  = α × actual_t + (1−α) × L_(t-1)
trend = L_last − L_prev
forecast_h = L_last + h × trend
```

#### Holt's Linear Trend (α = 0.3, β = 0.1)
```
level_1  = first sales
trend_1  = second sales − first sales

level_t  = α × actual_t + (1−α) × (level_(t-1) + trend_(t-1))
trend_t  = β × (level_t − level_(t-1)) + (1−β) × trend_(t-1)

forecast_h = level_last + h × trend_last
```

#### Moving Average Trend (window = 3)
```
growth_t   = (sales_t − sales_(t-1)) / sales_(t-1)
avg_growth = mean(last_3_growth_rates)
forecast_1 = last_sales × (1 + avg_growth)
forecast_h = forecast_(h-1) × (1 + avg_growth)
```

#### Weighted Average Growth
```
growth_t        = (sales_t − sales_(t-1)) / sales_(t-1)
weights         = [1, 2, 3, ..., n]
weighted_growth = Σ(weight_i × growth_i) / Σ(weight_i)
forecast_h      = last_sales × (1 + weighted_growth)^h
```

#### Ensemble
```
top3       = three models with lowest backtest MAPE
ensemble_h = (model_a_h + model_b_h + model_c_h) / 3
```

#### Scenarios
```
g_pessimistic = percentile_25(all historical growth rates)
g_baseline    = percentile_50(all historical growth rates)
g_optimistic  = percentile_75(all historical growth rates)
scenario_h    = last_sales × (1 + g)^h
```

#### Backtesting metrics
```
MAPE = mean(|predicted − actual| / actual) × 100
Bias = mean(predicted − actual)
RMSE = sqrt(mean((predicted − actual)²))
```
            """,
        )

    with st.expander('🔬 How each model is trained from scratch', expanded=False):
        st.markdown(
            """
### What "training" means here

These are not deep learning models. They do not use gradient descent or neural networks. Each model computes its parameters in a **single closed-form pass** over the historical data.

---

### Linear Regression — trained in one matrix operation

**Input:** n years of (year, revenue) pairs
**Goal:** find slope and intercept that minimize total squared error

The algorithm computes the slope and intercept simultaneously using:
```
slope     = Σ((year_i − ȳear)(sales_i − s̄ales)) / Σ((year_i − ȳear)²)
intercept = mean(sales) − slope × mean(year)
```
This is O(n) — one pass through the data. No iteration, no randomness, no hyperparameter tuning. The solution is mathematically guaranteed to be the global minimum of the squared error objective.

**What the model learned:** How many dollars revenue grows per calendar year across the whole history.

---

### CAGR — trained by reading two numbers

**Input:** first revenue value, last revenue value, count of intervals
**Computation:** one formula

```
CAGR = (last / first)^(1/n) − 1
```

The model ignores every year in between. It only learns from the endpoints. That is why it is sensitive to start and end point selection.

---

### Exponential Smoothing — trained by a sequential loop

**Input:** ordered revenue series
**Parameters:** alpha = 0.3 (fixed, not learned from data)

The model reads the series **left to right**, updating one smoothed value at a time:
```
L_t = 0.3 × actual_t + 0.7 × L_(t-1)
```
Each new smoothed value is 30% the newest data and 70% the previous smoothed estimate. After the final year, the difference between the last two smoothed values becomes the trend used for projection.

**What the model learned:** A weighted summary of the recent revenue level, biased toward newer observations.

---

### Holt's Linear Trend — trained by two sequential loops running in parallel

**Input:** ordered revenue series
**Parameters:** alpha = 0.3 (level speed), beta = 0.1 (trend speed) — both fixed

Two values are updated simultaneously at each year:
```
level_t = 0.3 × actual_t + 0.7 × (level_(t-1) + trend_(t-1))
trend_t = 0.1 × (level_t − level_(t-1)) + 0.9 × trend_(t-1)
```
The level chases the most recent revenue. The trend chases the most recent direction change, but more slowly (beta=0.1 makes it react slowly to level shifts).

**What the model learned:** The current revenue baseline and the current speed/direction of change — both updated every year.

---

### Moving Average Trend — trained by looking at only the last 3 growth rates

**Input:** ordered revenue series → derived growth rate series
**Computation:** average the most recent 3

```
growth_t   = (sales_t − sales_(t-1)) / sales_(t-1)
avg_growth = (growth_(n) + growth_(n-1) + growth_(n-2)) / 3
```

**What the model learned:** The average momentum from the most recent 3 years. All older history is discarded.

---

### Weighted Average Growth — trained by reading all growth rates with recency bias

**Input:** full growth rate history
**Computation:** weighted average with linearly increasing weights

```
weights = [1, 2, 3, ..., n]  # n = total growth rates
weighted_growth = Σ(weight_i × growth_i) / Σ(weight_i)
```

**What the model learned:** A summary of all growth rates in history, but with the newest year counted n times more than the oldest. Unlike MA Trend it uses all history, but it still tilts toward recent data.

---

### Backtesting — how accuracy is measured

The engine runs a simulation:
1. Take all cleaned history
2. Hide the last 2 years (holdout)
3. Train each model on the remaining history
4. Ask each model to predict the 2 hidden years
5. Compare predictions to the real hidden values
6. Rank by MAPE (lowest = best)

This tells you not which model fits the training data best, but which model would have been most useful if you had run it 2 years ago.
            """,
        )

    with st.expander('📊 MAPE score guide — what each range means', expanded=False):
        st.markdown(
            """
### MAPE = Mean Absolute Percentage Error

**Formula:**
```
MAPE = mean(|predicted − actual| / actual) × 100
```

**In plain words:** On average, what percentage of the true revenue did the model miss?

---

### Interpretation thresholds for annual revenue forecasting

| MAPE | What it means | Practical implication |
|---|---|---|
| **< 5%** | Excellent | Model closely matched both holdout years. Safe to weight heavily in the ensemble. |
| **5–10%** | Good | Model is a solid performer for annual data. Worth including. |
| **10–15%** | Acceptable | Some error, but still useful. May be dragged by one bad year. |
| **15–25%** | Weak | Model missed by a meaningful margin. Check if the holdout years were unusual. |
| **> 25%** | Poor | Model should not be trusted on its own for this company. |

---

### Why MAPE can look high even for a good forecast

Annual revenue forecasting has only 2 holdout years by default. If one of those years was unusual (post-pandemic rebound, acquisition, regulatory change), MAPE will spike for every model — not because the model is wrong, but because the holdout period was abnormal.

That is why the page also shows:
- **Bias** — did the model miss consistently in one direction?
- **RMSE** — how large were the individual misses in dollar terms?

A model with MAPE 14% but Bias near 0 and RMSE of 0.3B is still useful.
A model with MAPE 14% and Bias +1.2B means it consistently over-predicted by $1.2B.

---

### Why the ensemble exists

Even the best model in backtesting can be wrong for the next 2 years. Blending the top 3 models reduces reliance on any single model. Historically, ensembles outperform individual model selection on out-of-sample data in most time-series forecasting benchmarks.
            """,
        )

    with st.expander('🔢 Step-by-step worked example you can verify by hand', expanded=False):
        cagr_pct = summary.get('cagr_pct')
        first_year = summary.get('first_year')
        last_year = summary.get('last_year')
        avg_growth = summary.get('avg_annual_growth_pct')
        volatility = summary.get('growth_volatility_pct')

        st.markdown(
            f"""
### Summary statistics for the selected company

| Stat | Value |
|---|---|
| First history year | {first_year or '—'} |
| Last history year | {last_year or '—'} |
| Historical CAGR | {f'{cagr_pct:.2f}%' if cagr_pct is not None else '—'} |
| Avg annual growth | {f'{avg_growth:.2f}%' if avg_growth is not None else '—'} |
| Growth volatility (std dev) | {f'{volatility:.2f}%' if volatility is not None else '—'} |

---

### How to verify any model number by hand

**CAGR check (simplest):**
1. Find "Latest actual revenue" on the hero card above
2. Find "Historical years" card → that tells you how many year-to-year intervals there are
3. Apply: `CAGR = (latest / first_year_revenue)^(1 / intervals) − 1`
4. Apply: `2027 forecast = latest × (1 + CAGR)^1`

**Moving Average Trend check (most transparent):**
1. Open the Data Prep tab → Clean training series
2. Look at the last 3 Growth % values in the table
3. Average them manually
4. Multiply the latest revenue by `(1 + avg_growth_rate)`
5. Compare to the MA Trend value in the forecast table on the Overview tab

**Ensemble check:**
1. Open the Backtest tab — note the top 3 models by MAPE rank
2. Find their 2027 (or next-year) forecast values in the forecast table
3. Average those 3 values
4. Compare to the Ensemble column — they should match

**Scenario check:**
1. Open Data Prep → Clean training series
2. Collect all Growth % values
3. Sort them
4. Take the 25th, 50th, 75th percentile growth rates
5. Apply each to the latest revenue: `forecast = latest × (1 + g_scenario)^1`
6. Compare to Pessimistic / Baseline / Optimistic in the forecast table
            """,
        )


def _render_method_notes() -> None:
    st.markdown(
        '<div class="rev-panel">'
        '<h3 class="rev-section-title">How the page works</h3>'
        '<p class="rev-section-copy">'
        'The page loads annual actual revenue from staging, converts it into a single year/sales series, removes growth outliers, runs six forecasting methods, backtests them, and then renders the model outputs plus ensemble/scenario views. '
        'The implementation lives in `app/data/revenue_forecast_service.py` and `app/utils/retailer_forecaster.py`.'
        '</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.write('')
    st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
    st.markdown('<h3 class="rev-section-title">Core steps</h3>', unsafe_allow_html=True)
    st.markdown(
        """
        1. Resolve company source from `coreiq_companies.source`.
        2. Read SEC actuals from `coreiq_av_financials_income_statement` or YF actuals from `coreiq_yf_financials_income_statement`.
        3. Normalize the actual series into `year` and `sales` in billions.
        4. Detect outliers using annual growth z-scores.
        5. Run the six forecasting methods in `RetailerForecaster`.
        6. Backtest each model against the holdout years.
        7. Build the ensemble from the best backtested models.
        8. Build pessimistic, baseline, and optimistic scenarios from historical growth percentiles.
        9. Render the charts and tables in Streamlit.
        """,
        unsafe_allow_html=True,
    )
    st.markdown('</div>', unsafe_allow_html=True)
    st.write('')
    st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
    st.markdown('<h3 class="rev-section-title">Future model readiness</h3>', unsafe_allow_html=True)
    st.markdown(
        """
        - The page renders model tabs from the runtime payload, so new model families will show up automatically once they are added to the engine.
        - The forecast table is column-driven, not hard-coded to today’s six models.
        - The UI can grow to more models without changing the data fetch layer.
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<p class="rev-mini-note">Full markdown documentation: `docs/revenue_estimates_forecasting.md`.</p>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)


def _mm(v) -> Optional[float]:
    """Convert billions float → millions float, NO rounding. None-safe."""
    if v is None or (hasattr(v, '__float__') and pd.isna(float(v))):
        return None
    return float(v) * 1000.0


def _build_forecast_excel_single(
    ticker: str,
    company_name: str,
    training_df: pd.DataFrame,
    actual_df: pd.DataFrame,
    forecast_df: pd.DataFrame,
    backtest_df: pd.DataFrame,
    model_keys: List[str],
    scenario_keys: List[str],
    payload: Dict[str, Any],
) -> bytes:
    """Build a fully-formatted Excel workbook for one ticker's model forecasts."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    # ── Palette ──────────────────────────────────────────────────────────────
    _GREEN      = "1B6B24"
    _GREEN_LIGHT = "E8F5EA"
    _BLUE       = "0055BB"
    _BLUE_LIGHT = "E6EEFA"
    _GREY_DARK  = "2D2A29"
    _GREY_MID   = "6B6B6B"
    _GREY_LIGHT = "F2F2F2"
    _WHITE      = "FFFFFF"
    _ACTUAL_BG  = "F9F9F9"
    _ACTUAL_HDR = "E0E0E0"
    _WARN       = "FFF3CD"
    _WARN_BORDER = "FFC107"

    def _hdr(ws, row, col, text, bg=_GREY_DARK, fg=_WHITE, bold=True, size=10, align="center", wrap=False):
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(name="Calibri", bold=bold, size=size, color=fg)
        c.fill = PatternFill("solid", fgColor=bg)
        c.alignment = Alignment(horizontal=align, vertical="center", wrap_text=wrap)
        return c

    def _val(ws, row, col, value, fmt="#,##0.00", align="right", bold=False, bg=None):
        c = ws.cell(row=row, column=col, value=value)
        c.font = Font(name="Calibri", size=10, bold=bold)
        if fmt:
            c.number_format = fmt
        c.alignment = Alignment(horizontal=align, vertical="center")
        if bg:
            c.fill = PatternFill("solid", fgColor=bg)
        return c

    def _thin():
        s = Side(style="thin", color="DDDDDD")
        return Border(left=s, right=s, top=s, bottom=s)

    def _set_col_widths(ws, widths):
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    summary = payload.get("summary", {})
    wb = Workbook()

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 1 — Historical & All Model Forecasts
    # ═══════════════════════════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "Historical & Forecasts"

    # Title block
    ws1.merge_cells("A1:B1")
    c = ws1.cell(row=1, column=1, value=f"Revenue Forecasting — {company_name} ({ticker})")
    c.font = Font(name="Calibri", bold=True, size=14, color=_GREY_DARK)
    ws1.merge_cells("A2:B2")
    ws1.cell(row=2, column=1, value="All values in USD Millions (mm)  ·  A = Actual  ·  F = Model Forecast").font = Font(name="Calibri", size=9, color=_GREY_MID, italic=True)
    ws1.row_dimensions[3].height = 6  # spacer

    # Build column list: Year, Type, Revenue(mm), then model cols, then scenario cols
    all_model_cols = [k for k in model_keys if k != "ensemble"] + (["ensemble"] if "ensemble" in model_keys else [])
    scen_cols = ["scenario_pessimistic", "scenario_baseline", "scenario_optimistic"]
    scen_cols = [k for k in scen_cols if k in (scenario_keys or [])]

    col_labels = ["Year", "Type", "Revenue (mm)"] + [MODEL_LABELS.get(k, k) for k in all_model_cols] + [MODEL_LABELS.get(k, k) for k in scen_cols]

    HDR_ROW = 4
    for ci, lbl in enumerate(col_labels, 1):
        is_ens = (ci == 3 + len(all_model_cols)) and "ensemble" in all_model_cols
        is_scen = ci > 3 + len(all_model_cols)
        bg = _GREEN if is_ens else (_BLUE if is_scen else _GREY_DARK)
        _hdr(ws1, HDR_ROW, ci, lbl, bg=bg, size=9, wrap=True)
    ws1.row_dimensions[HDR_ROW].height = 30
    ws1.freeze_panes = f"A{HDR_ROW + 1}"

    # ── Build combined year list: all actuals (including outliers) + forecast ──
    _src_df = actual_df if not actual_df.empty else training_df
    _train_yrs = set(training_df["year"].astype(int).tolist()) if not training_df.empty else set()
    _actual_rows_sorted = _src_df.sort_values("year") if not _src_df.empty else pd.DataFrame()
    _fcst_rows_sorted   = forecast_df.sort_values("year") if not forecast_df.empty else pd.DataFrame()

    r = HDR_ROW + 1
    # Historical rows — use actual_df so outliers are included
    for _, row in _actual_rows_sorted.iterrows():
        yr = int(row["year"])
        rev_mm = float(row["sales_billions"]) * 1000.0  # exact, no rounding
        is_out = bool(row.get("is_outlier", False))
        bg = _WARN if is_out else _ACTUAL_BG
        _val(ws1, r, 1, yr, fmt="0000", align="center", bg=bg)
        typ = "Actual ✓" + (" ⚠ Outlier" if is_out else "")
        _val(ws1, r, 2, typ, fmt="@", align="center", bg=bg)
        _val(ws1, r, 3, rev_mm, fmt="#,##0.0000000000", bg=bg)  # exact, no format rounding
        for ci in range(4, len(col_labels) + 1):
            _val(ws1, r, ci, None, fmt="#,##0.0000000000", bg=bg)
        r += 1

    # Forecast rows
    for _, row in _fcst_rows_sorted.iterrows():
        yr = int(row["year"])
        _val(ws1, r, 1, yr, fmt="0000", align="center", bg=_GREEN_LIGHT)
        _val(ws1, r, 2, "Forecast ▶", fmt="@", align="center", bg=_GREEN_LIGHT)
        _val(ws1, r, 3, None, fmt="#,##0.0000000000", bg=_GREEN_LIGHT)
        for ci, k in enumerate(all_model_cols, 4):
            v = row.get(k)
            val = _mm(v)
            is_ens = k == "ensemble"
            _val(ws1, r, ci, val, fmt="#,##0.0000000000", bold=is_ens, bg=_GREEN_LIGHT)
        for ci, k in enumerate(scen_cols, 4 + len(all_model_cols)):
            v = row.get(k)
            _val(ws1, r, ci, _mm(v), fmt="#,##0.0000000000", bg=_BLUE_LIGHT)
        r += 1

    # Apply borders & col widths
    for rr in range(HDR_ROW, ws1.max_row + 1):
        for cc in range(1, len(col_labels) + 1):
            ws1.cell(rr, cc).border = _thin()
    _set_col_widths(ws1, [8, 16, 16] + [18] * len(all_model_cols) + [20] * len(scen_cols))

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 2 — Backtest Results
    # ═══════════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet("Backtest Results")
    ws2.merge_cells("A1:F1")
    ws2.cell(1, 1, f"Backtest Results — {company_name} ({ticker})").font = Font(name="Calibri", bold=True, size=13, color=_GREY_DARK)
    ws2.merge_cells("A2:F2")
    ws2.cell(2, 1, "Engine holds out the last 2 years, trains on earlier history, compares predicted vs actual. Lowest MAPE = Best model.").font = Font(name="Calibri", size=9, italic=True, color=_GREY_MID)

    bt_headers = ["Rank", "Model Name", "MAPE (%)", "Bias ($B)", "RMSE ($B)", "Role"]
    for ci, h in enumerate(bt_headers, 1):
        _hdr(ws2, 4, ci, h, bg=_GREY_DARK, size=10)
    ws2.freeze_panes = "A5"

    if not backtest_df.empty:
        _bt_sorted = backtest_df.sort_values("mape").reset_index(drop=True)
        best_mk = _bt_sorted.iloc[0]["method"] if len(_bt_sorted) else ""
        # Top-3 by MAPE = ensemble members
        ensemble_mks = set(_bt_sorted.head(3)["method"].tolist())
        for ri, (_, row) in enumerate(_bt_sorted.iterrows(), 5):
            rank = ri - 4
            mk = row.get("method", "")
            model_name = MODEL_FULL_LABELS.get(mk, row.get("display", mk))
            mape = float(row["mape"]) if row.get("mape") is not None else None
            bias = float(row["bias"]) if row.get("bias") is not None else None
            rmse = float(row["rmse"]) if row.get("rmse") is not None else None
            is_best = mk == best_mk
            is_ens = mk in ensemble_mks and not is_best
            role = "⭐ Best Model" if is_best else ("✅ In Ensemble" if is_ens else "—")
            bg = _GREEN_LIGHT if is_best else (_BLUE_LIGHT if is_ens else None)
            _val(ws2, ri, 1, rank, fmt="0", align="center", bg=bg)
            _val(ws2, ri, 2, model_name, fmt="@", align="left", bold=is_best, bg=bg)
            _val(ws2, ri, 3, mape, fmt="0.0000000000", bg=bg)
            _val(ws2, ri, 4, bias, fmt="0.0000000000", bg=bg)
            _val(ws2, ri, 5, rmse, fmt="0.0000000000", bg=bg)
            _val(ws2, ri, 6, role, fmt="@", align="center", bg=bg)
    else:
        ws2.cell(5, 1, "No backtest data available.").font = Font(italic=True, color=_GREY_MID)

    for rr in range(4, ws2.max_row + 1):
        for cc in range(1, 7):
            ws2.cell(rr, cc).border = _thin()
    _set_col_widths(ws2, [8, 32, 12, 12, 12, 18])

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 3 — Scenario Analysis
    # ═══════════════════════════════════════════════════════════════════════
    ws3 = wb.create_sheet("Scenario Analysis")
    ws3.merge_cells("A1:E1")
    ws3.cell(1, 1, f"Scenario Analysis — {company_name} ({ticker})").font = Font(name="Calibri", bold=True, size=13, color=_GREY_DARK)
    ws3.merge_cells("A2:E2")
    ws3.cell(2, 1, "Pessimistic = 25th pct growth  ·  Baseline = 50th pct (median)  ·  Optimistic = 75th pct  ·  All values in USD Millions (mm)").font = Font(name="Calibri", size=9, italic=True, color=_GREY_MID)

    sc_hdrs = ["Year", "Ensemble (mm)", "Pessimistic (mm)", "Baseline (mm)", "Optimistic (mm)"]
    sc_cols_map = {"Ensemble (mm)": "ensemble", "Pessimistic (mm)": "scenario_pessimistic", "Baseline (mm)": "scenario_baseline", "Optimistic (mm)": "scenario_optimistic"}
    for ci, h in enumerate(sc_hdrs, 1):
        bg = _GREEN if h.startswith("Ensemble") else _BLUE
        _hdr(ws3, 4, ci, h, bg=bg, size=10)
    ws3.freeze_panes = "A5"

    if not forecast_df.empty:
        for ri, (_, row) in enumerate(forecast_df.iterrows(), 5):
            yr = int(row["year"])
            _val(ws3, ri, 1, yr, fmt="0000", align="center")
            for ci, (hdr, key) in enumerate(sc_cols_map.items(), 2):
                v = row.get(key)
                val = float(v) * 1000.0 if v is not None and not pd.isna(v) else None
                bg = _GREEN_LIGHT if key == "ensemble" else _BLUE_LIGHT
                _val(ws3, ri, ci, val, fmt="#,##0.0000000000", bg=bg)
    else:
        ws3.cell(5, 1, "No forecast data available.").font = Font(italic=True, color=_GREY_MID)

    for rr in range(4, ws3.max_row + 1):
        for cc in range(1, 6):
            ws3.cell(rr, cc).border = _thin()
    _set_col_widths(ws3, [8, 20, 20, 20, 20])

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 4 — Implied Growth Rates
    # ═══════════════════════════════════════════════════════════════════════
    ws4 = wb.create_sheet("Implied Growth Rates")
    ws4.merge_cells("A1:C1")
    ws4.cell(1, 1, f"Implied Growth Rates — {company_name} ({ticker})").font = Font(name="Calibri", bold=True, size=13, color=_GREY_DARK)
    ws4.merge_cells("A2:C2")
    ws4.cell(2, 1, "YoY growth implied by the ensemble forecast starting from the last actual revenue.").font = Font(name="Calibri", size=9, italic=True, color=_GREY_MID)

    for ci, h in enumerate(["Year", "Ensemble Forecast (mm)", "Implied YoY Growth (%)"], 1):
        _hdr(ws4, 4, ci, h, bg=_GREEN, size=10)
    ws4.freeze_panes = "A5"

    last_actual_b = summary.get("latest_actual_revenue_billions")
    if not forecast_df.empty and "ensemble" in forecast_df.columns and last_actual_b is not None:
        ens = forecast_df[["year", "ensemble"]].dropna()
        all_vals = [float(last_actual_b)] + ens["ensemble"].tolist()
        yoy = [(all_vals[i + 1] / all_vals[i] - 1) * 100 for i in range(len(all_vals) - 1)]
        for ri, (_, row) in enumerate(ens.iterrows(), 5):
            yr = int(row["year"])
            v = float(row["ensemble"]) * 1000.0
            g = yoy[ri - 5]
            _val(ws4, ri, 1, yr, fmt="0000", align="center")
            _val(ws4, ri, 2, v, fmt="#,##0.0000000000", bg=_GREEN_LIGHT)
            _val(ws4, ri, 3, g, fmt="0.0000000000", bg=_GREEN_LIGHT)
    else:
        ws4.cell(5, 1, "No ensemble data available.").font = Font(italic=True, color=_GREY_MID)

    for rr in range(4, ws4.max_row + 1):
        for cc in range(1, 4):
            ws4.cell(rr, cc).border = _thin()
    _set_col_widths(ws4, [8, 24, 24])

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 5 — Summary / Metadata
    # ═══════════════════════════════════════════════════════════════════════
    ws5 = wb.create_sheet("Summary")
    ws5.column_dimensions["A"].width = 36
    ws5.column_dimensions["B"].width = 32
    ws5.cell(1, 1, f"Forecast Summary — {company_name} ({ticker})").font = Font(name="Calibri", bold=True, size=14, color=_GREY_DARK)
    _hdr(ws5, 3, 1, "Metric", bg=_GREY_DARK, size=10)
    _hdr(ws5, 3, 2, "Value", bg=_GREY_DARK, size=10)
    meta_rows = [
        ("Company", company_name),
        ("Ticker", ticker),
        ("Data Source", payload.get("source_label", "—")),
        ("Reported Currency", payload.get("reported_currency", "USD")),
        ("Historical Years Used", str(summary.get("historical_rows", "—"))),
        ("Outlier Years Excluded", ", ".join(str(y) for y in payload.get("outlier_years", [])) or "None"),
        ("Latest Actual Revenue (mm)", f"{float(summary['latest_actual_revenue_billions']) * 1000.0:,.2f}" if summary.get("latest_actual_revenue_billions") else "—"),
        ("Latest Actual Date", str(summary.get("latest_actual_date", "—"))),
        ("Best Forecasting Model", MODEL_FULL_LABELS.get(payload.get("best_method", ""), payload.get("best_method", "—"))),
        ("Best Model MAPE (%)", f"{float(summary['best_mape']):.2f}" if summary.get("best_mape") else "—"),
        ("Forecast Horizon (years)", str(summary.get("forecast_periods", "—"))),
        ("Forecast End Year", str(summary.get("forecast_end_year", "—"))),
        ("Ensemble — Next Year (mm)", f"{float(summary['next_forecast_value_billions']) * 1000.0:,.2f}" if summary.get("next_forecast_value_billions") else "—"),
        ("Historical CAGR (%)", f"{float(summary['cagr_pct']):.2f}" if summary.get("cagr_pct") else "—"),
        ("Avg Annual Growth (%)", f"{float(summary['avg_annual_growth_pct']):.2f}" if summary.get("avg_annual_growth_pct") else "—"),
    ]
    for ri, (k, v) in enumerate(meta_rows, 4):
        bg = _ACTUAL_BG if ri % 2 == 0 else _WHITE
        ws5.cell(ri, 1, k).font = Font(name="Calibri", size=10, bold=True)
        ws5.cell(ri, 1).fill = PatternFill("solid", fgColor=bg)
        ws5.cell(ri, 1).border = _thin()
        ws5.cell(ri, 2, v).font = Font(name="Calibri", size=10)
        ws5.cell(ri, 2).fill = PatternFill("solid", fgColor=bg)
        ws5.cell(ri, 2).border = _thin()

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_forecast_excel_all(companies: List[Dict[str, Any]]) -> bytes:
    """Build a combined Excel for ALL tickers from coreiq_model_forecasts (no engine re-run)."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from core.database import db_manager

    _GREEN       = "1B6B24"
    _GREEN_LIGHT = "E8F5EA"
    _BLUE        = "0055BB"
    _BLUE_LIGHT  = "E6EEFA"
    _GREY_DARK   = "2D2A29"
    _GREY_MID    = "6B6B6B"
    _ACTUAL_BG   = "F0F4FF"
    _WHITE       = "FFFFFF"
    _HDR_ACTUAL  = "D0D8F0"

    def _hdr(ws, row, col, text, bg=_GREY_DARK, fg="FFFFFF", bold=True, size=9, wrap=True):
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(name="Calibri", bold=bold, size=size, color=fg)
        c.fill = PatternFill("solid", fgColor=bg)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=wrap)
        return c

    def _val(ws, row, col, value, fmt="#,##0.0000000000", align="right", bold=False, bg=None, fg=None):
        c = ws.cell(row=row, column=col, value=value)
        c.font = Font(name="Calibri", size=9, bold=bold, color=fg or _GREY_DARK)
        if fmt:
            c.number_format = fmt
        c.alignment = Alignment(horizontal=align, vertical="center")
        if bg:
            c.fill = PatternFill("solid", fgColor=bg)
        return c

    def _thin():
        s = Side(style="thin", color="DDDDDD")
        return Border(left=s, right=s, top=s, bottom=s)

    def _set_widths(ws, widths):
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def _autofit(ws, min_w=8, max_w=60, extra=2):
        """Auto-fit every column to its widest cell content."""
        col_widths: Dict[int, float] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                if cell.data_type == 'n':
                    # numeric — estimate display length via format
                    txt = str(cell.value)
                else:
                    txt = str(cell.value)
                needed = len(txt) + extra
                col_widths[cell.column] = max(col_widths.get(cell.column, min_w), needed)
        for col_idx, width in col_widths.items():
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_w, max(min_w, width))

    # ── Fetch all data from DB ────────────────────────────────────────────
    all_fcst = db_manager.execute_query_readonly("""
        SELECT f.ticker, c.name_coresight AS company_name,
               f.fiscal_year, f.model_key, f.value_millions,
               f.is_best_model, f.mape, f.last_actual_date
        FROM coreiq_model_forecasts f
        INNER JOIN coreiq_companies c
          ON (
                c.ticker COLLATE utf8mb4_unicode_ci = f.ticker COLLATE utf8mb4_unicode_ci
             OR (
                  c.source = 'YFinance'
                  AND c.exchange_acronym IS NOT NULL
                  AND TRIM(c.exchange_acronym) <> ''
                  AND f.ticker COLLATE utf8mb4_unicode_ci = CONCAT(c.ticker, '.', TRIM(c.exchange_acronym)) COLLATE utf8mb4_unicode_ci
                )
              )
        WHERE f.metric = 'total_revenue'
        ORDER BY c.name_coresight ASC, f.fiscal_year ASC, f.model_key ASC
    """, {})

    all_hist_sec = db_manager.execute_query_readonly("""
        SELECT ticker, YEAR(fiscal_date_ending) AS yr,
               total_revenue / 1000000.0 AS value_mm
        FROM coreiq_av_financials_income_statement
        WHERE report_type = 'annual' AND total_revenue IS NOT NULL
    """, {})

    all_hist_yf = db_manager.execute_query_readonly("""
        SELECT COALESCE(NULLIF(TRIM(yf_symbol), ''), TRIM(ticker)) AS fcst_ticker,
               YEAR(period_end) AS yr,
               MAX(value) / 1000000.0 AS value_mm
        FROM coreiq_yf_financials_income_statement
        WHERE frequency = 'annual' AND line_item = 'Total Revenue' AND value IS NOT NULL
        GROUP BY fcst_ticker, YEAR(period_end)
    """, {})

    # Build lookup structures
    company_names = {row["ticker"]: row.get("company_name", row["ticker"]) for row in all_fcst}
    # Historical: {ticker: {year: value_mm}}
    hist_map: Dict[str, Dict[int, float]] = {}
    for r in (all_hist_sec or []):
        t, y, v = r["ticker"], int(r["yr"]), (float(r["value_mm"]) if r["value_mm"] else None)
        if v is not None:
            hist_map.setdefault(t, {})[y] = v
    for r in (all_hist_yf or []):
        t, y, v = r["fcst_ticker"], int(r["yr"]), (float(r["value_mm"]) if r["value_mm"] else None)
        if v is None:
            continue
        # SEC wins when the same symbol+year exists; otherwise accumulate all YF years for `t`.
        # (Do not use `if t not in hist_map` — that only lets the first YF row in per ticker.)
        if t in hist_map and y in hist_map[t]:
            continue
        hist_map.setdefault(t, {})[y] = v

    # Forecast: {ticker: {year: {model_key: value_mm}}}
    fcst_map: Dict[str, Dict[int, Dict[str, float]]] = {}
    best_model: Dict[str, str] = {}
    mape_map: Dict[str, Dict[str, float]] = {}
    for r in (all_fcst or []):
        t, yr, mk, vm = r["ticker"], int(r["fiscal_year"]), r["model_key"], r.get("value_millions")
        fcst_map.setdefault(t, {}).setdefault(yr, {})[mk] = float(vm) if vm is not None else None
        if r.get("is_best_model") and mk not in ("scenario_pessimistic", "scenario_baseline", "scenario_optimistic"):
            best_model[t] = mk
        if r.get("mape") is not None and mk not in ("scenario_pessimistic", "scenario_baseline", "scenario_optimistic"):
            mape_map.setdefault(t, {})[mk] = float(r["mape"])

    tickers_ordered = list(dict.fromkeys(r["ticker"] for r in (all_fcst or [])))
    all_years_hist = sorted({y for hm in hist_map.values() for y in hm})
    all_years_fcst = sorted({yr for fm in fcst_map.values() for yr in fm})

    wb = Workbook()

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 1 — Ensemble Overview (wide pivot: company × year)
    # ═══════════════════════════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "Ensemble Overview"

    ws1.merge_cells(f"A1:{get_column_letter(2 + len(all_years_hist) + len(all_years_fcst))}1")
    ws1.cell(1, 1, "Revenue Forecasting — All Companies | Ensemble Model Forecast").font = Font(name="Calibri", bold=True, size=13, color=_GREY_DARK)
    ws1.merge_cells(f"A2:{get_column_letter(2 + len(all_years_hist) + len(all_years_fcst))}2")
    ws1.cell(2, 1, "Values in USD Millions (mm)  ·  Grey columns = Actual  ·  Green columns = Model Forecast  ·  2 decimal places").font = Font(name="Calibri", size=9, italic=True, color=_GREY_MID)

    # Header row
    _hdr(ws1, 4, 1, "Company", bg=_GREY_DARK, size=9)
    _hdr(ws1, 4, 2, "Ticker", bg=_GREY_DARK, size=9)
    for ci, yr in enumerate(all_years_hist, 3):
        _hdr(ws1, 4, ci, f"{yr}\n(Actual)", bg=_HDR_ACTUAL, fg=_GREY_DARK, size=8)
    for ci, yr in enumerate(all_years_fcst, 3 + len(all_years_hist)):
        _hdr(ws1, 4, ci, f"{yr}\n(Forecast)", bg=_GREEN, size=8)
    ws1.row_dimensions[4].height = 28
    ws1.freeze_panes = "C5"

    for ri, ticker in enumerate(tickers_ordered, 5):
        cname = company_names.get(ticker, ticker)
        _val(ws1, ri, 1, cname, fmt="@", align="left", bg=_WHITE, bold=False)
        _val(ws1, ri, 2, ticker, fmt="@", align="center", bg=_WHITE)
        for ci, yr in enumerate(all_years_hist, 3):
            v = hist_map.get(ticker, {}).get(yr)
            _val(ws1, ri, ci, float(v) if v is not None else None, bg=_ACTUAL_BG if ri % 2 == 0 else "EDF2FF")
        for ci, yr in enumerate(all_years_fcst, 3 + len(all_years_hist)):
            v = fcst_map.get(ticker, {}).get(yr, {}).get("ensemble")
            _val(ws1, ri, ci, float(v) if v is not None else None, bg=_GREEN_LIGHT if ri % 2 == 0 else "D4EDDA")

    for rr in range(4, ws1.max_row + 1):
        for cc in range(1, 3 + len(all_years_hist) + len(all_years_fcst)):
            ws1.cell(rr, cc).border = _thin()
    _autofit(ws1)

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 2 — All Models Long Format (full detail per ticker/year/model)
    # ═══════════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet("All Models Detail")
    ws2.merge_cells("A1:J1")
    ws2.cell(1, 1, "All Model Forecasts — Long Format | All values in USD Millions (mm)").font = Font(name="Calibri", bold=True, size=13, color=_GREY_DARK)
    ws2.merge_cells("A2:J2")
    ws2.cell(2, 1, "One row per Company × Year  ·  Type: A = Actual, F = Forecast").font = Font(name="Calibri", size=9, italic=True, color=_GREY_MID)

    model_order = ["linear", "cagr", "exp_smoothing", "holt", "ma_trend", "weighted_avg", "ensemble"]
    scen_order  = ["scenario_pessimistic", "scenario_baseline", "scenario_optimistic"]
    detail_hdrs = ["Company", "Ticker", "Year", "Type", "Actual Revenue (mm)"] + \
                  [MODEL_LABELS.get(k, k) for k in model_order] + \
                  [MODEL_LABELS.get(k, k) for k in scen_order]
    for ci, h in enumerate(detail_hdrs, 1):
        is_ens = h == "Ensemble"
        is_scen = "Scenario" in h
        bg = _GREEN if is_ens else (_BLUE if is_scen else _GREY_DARK)
        _hdr(ws2, 4, ci, h, bg=bg, size=8, wrap=True)
    ws2.row_dimensions[4].height = 28
    ws2.freeze_panes = "A5"

    ri = 5
    for ticker in tickers_ordered:
        cname = company_names.get(ticker, ticker)
        hist_yrs = sorted(hist_map.get(ticker, {}).keys())
        fcst_yrs = sorted(fcst_map.get(ticker, {}).keys())
        all_yrs  = sorted(set(hist_yrs) | set(fcst_yrs))
        for yr in all_yrs:
            is_actual = yr in hist_yrs
            typ = "A — Actual" if is_actual else "F — Forecast"
            bg  = _ACTUAL_BG if is_actual else _GREEN_LIGHT
            _val(ws2, ri, 1, cname, fmt="@", align="left", bg=bg)
            _val(ws2, ri, 2, ticker, fmt="@", align="center", bg=bg)
            _val(ws2, ri, 3, yr, fmt="0000", align="center", bg=bg)
            _val(ws2, ri, 4, typ, fmt="@", align="center", bg=bg)
            hist_v = hist_map.get(ticker, {}).get(yr)
            _val(ws2, ri, 5, float(hist_v) if hist_v is not None else None, bg=bg)
            for ci, mk in enumerate(model_order, 6):
                v = fcst_map.get(ticker, {}).get(yr, {}).get(mk)
                is_e = mk == "ensemble"
                _val(ws2, ri, ci, float(v) if v is not None else None, bold=is_e, bg=_GREEN_LIGHT if is_e else bg)
            for ci, mk in enumerate(scen_order, 6 + len(model_order)):
                v = fcst_map.get(ticker, {}).get(yr, {}).get(mk)
                _val(ws2, ri, ci, float(v) if v is not None else None, bg=_BLUE_LIGHT)
            for cc in range(1, len(detail_hdrs) + 1):
                ws2.cell(ri, cc).border = _thin()
            ri += 1

    _autofit(ws2)

    # ═══════════════════════════════════════════════════════════════════════
    # SHEET 3 — Backtest Summary (one row per ticker)
    # ═══════════════════════════════════════════════════════════════════════
    ws3 = wb.create_sheet("Backtest Summary")
    ws3.merge_cells("A1:J1")
    ws3.cell(1, 1, "Backtest Summary — All Companies").font = Font(name="Calibri", bold=True, size=13, color=_GREY_DARK)
    ws3.merge_cells("A2:J2")
    ws3.cell(2, 1, "MAPE (%) per model — lower is better  ·  ⭐ = Best model for that company").font = Font(name="Calibri", size=9, italic=True, color=_GREY_MID)

    bt_hdrs = ["Company", "Ticker", "Best Model"] + [MODEL_LABELS.get(k, k) + " MAPE%" for k in model_order if k != "ensemble"]
    for ci, h in enumerate(bt_hdrs, 1):
        _hdr(ws3, 4, ci, h, bg=_GREY_DARK, size=9)
    ws3.freeze_panes = "A5"

    non_ens_models = [k for k in model_order if k != "ensemble"]
    for ri2, ticker in enumerate(tickers_ordered, 5):
        cname = company_names.get(ticker, ticker)
        bm    = best_model.get(ticker, "")
        bm_label = MODEL_LABELS.get(bm, bm) if bm else "—"
        bg = _WHITE if ri2 % 2 == 0 else _ACTUAL_BG
        _val(ws3, ri2, 1, cname, fmt="@", align="left", bg=bg)
        _val(ws3, ri2, 2, ticker, fmt="@", align="center", bg=bg)
        _val(ws3, ri2, 3, f"⭐ {bm_label}", fmt="@", align="left", bold=True, bg=_GREEN_LIGHT)
        for ci, mk in enumerate(non_ens_models, 4):
            mape_v = mape_map.get(ticker, {}).get(mk)
            is_best_cell = mk == bm
            _val(ws3, ri2, ci, float(mape_v) if mape_v is not None else None,
                 fmt="0.0000000000", bold=is_best_cell,
                 bg=_GREEN_LIGHT if is_best_cell else bg)
        for cc in range(1, len(bt_hdrs) + 1):
            ws3.cell(ri2, cc).border = _thin()

    _autofit(ws3)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── Refresh Data dialog ──────────────────────────────────────────────────────

def _get_refresh_permissions(user_email: Optional[str]) -> tuple[bool, bool]:
    """Return (can_view_popup, can_run_models).

    Hierarchy:
      admin / super_user role   → view + run  (via check_access bypass)
      edit/delete_admin/allow   → view + run
      view_only/upload_only     → view only
      no ACL row / deny         → no access
    """
    if not user_email:
        return False, False
    try:
        can_edit = AccessControlManager.check_access("forecasting", user_email, required_permission="edit")
        can_view = AccessControlManager.check_access("forecasting", user_email, required_permission="view_only")
        return can_view, can_edit
    except Exception:
        return False, False


@st.dialog("Refresh Forecasting Models", width="large")
def _refresh_dialog(can_run: bool, user_email: str) -> None:
    from datetime import datetime, timezone as _tz

    # Higher specificity needed to beat the 6-col horizontal-block white rule (0,3,1)
    # Our rule: div + [stDialog] + [stHorizontalBlock] + button + [attr] = (0,3,2) → wins
    st.markdown("""
<style>
div[data-testid="stDialog"] [data-testid="stHorizontalBlock"] button[data-testid="stBaseButton-secondary"] {
    background-color: #B91C1C !important;
    color: #FFFFFF !important;
    border: none !important;
    padding: 2px 10px !important;
    font-size: 11px !important;
    font-weight: 600 !important;
    height: 28px !important;
    min-height: 28px !important;
    max-height: 28px !important;
    line-height: 24px !important;
    border-radius: 4px !important;
    box-shadow: none !important;
    width: auto !important;
    cursor: pointer !important;
    white-space: nowrap !important;
}
div[data-testid="stDialog"] [data-testid="stHorizontalBlock"] button[data-testid="stBaseButton-secondary"] > div {
    padding: 0 !important;
    min-height: unset !important;
    height: auto !important;
}
div[data-testid="stDialog"] [data-testid="stHorizontalBlock"] button[data-testid="stBaseButton-secondary"]:hover {
    background-color: #991B1B !important;
}
div[data-testid="stDialog"] div[data-testid="column"]:last-child {
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding-top: 0 !important;
    padding-bottom: 0 !important;
}
</style>
""", unsafe_allow_html=True)

    period_type = st.radio(
        "Period Type",
        options=["Annual", "Quarterly"],
        index=0,
        horizontal=True,
        key="refresh_dialog_period_type",
    )

    if period_type == "Quarterly":
        st.info("Quarterly forecast refresh is not yet supported.")
        return

    with st.spinner("Loading forecast data…"):
        table_rows = get_refresh_table_data(period_type.lower())

    if not table_rows:
        st.warning("No forecast data found. Run models first.")
        return

    # Column widths: ticker, company, exchange, annual reported on, fiscal period, last refresh, [action]
    _CW = [1, 2, 1, 1.4, 1.4, 1.8, 0.9] if can_run else [1, 2, 1, 1.4, 1.4, 1.8]
    _HDR_STYLE = "font-size:11px;font-weight:700;color:#6B7280;text-transform:uppercase;letter-spacing:.05em;"
    _CELL_STYLE = "font-size:13px;line-height:1.4;"

    # Header row
    hcols = st.columns(_CW)
    _hdrs = ["Ticker", "Company Name", "Exchange", "Annual Reported On", "Fiscal Period", "Last Refresh"]
    if can_run:
        _hdrs.append("Action")
    for hc, lbl in zip(hcols, _hdrs):
        hc.markdown(f'<span style="{_HDR_STYLE}">{lbl}</span>', unsafe_allow_html=True)
    st.markdown('<hr style="margin:4px 0 6px;border:none;border-top:2px solid #2D2A29;">', unsafe_allow_html=True)

    with st.container(height=480, border=False):
        for row in table_rows:
            ticker = row["ticker"]
            rcols = st.columns(_CW)
            rcols[0].markdown(f'<span style="{_CELL_STYLE}">**{ticker}**</span>', unsafe_allow_html=True)
            rcols[1].markdown(f'<span style="{_CELL_STYLE}">{row["company_name"] or "—"}</span>', unsafe_allow_html=True)
            rcols[2].markdown(f'<span style="{_CELL_STYLE}">{row["exchange"] or "—"}</span>', unsafe_allow_html=True)
            rcols[3].markdown(f'<span style="{_CELL_STYLE}">{row["annual_reported_on"]}</span>', unsafe_allow_html=True)
            rcols[4].markdown(f'<span style="{_CELL_STYLE}">{row["fiscal_period"]}</span>', unsafe_allow_html=True)
            last_refresh_ph = rcols[5].empty()
            last_refresh_ph.markdown(f'<span style="{_CELL_STYLE}">{row["last_refresh"]}</span>', unsafe_allow_html=True)

            if can_run:
                action_ph = rcols[6].empty()
                if action_ph.button("Refresh Now", key=f"refresh_now_{ticker}"):
                    action_ph.markdown('<span style="font-size:12px;color:#6B7280">Running…</span>', unsafe_allow_html=True)
                    with st.spinner(""):
                        result = sync_forecast_for_ticker(ticker, force=True)
                        clear_revenue_forecast_caches()
                        get_refresh_table_data.clear()
                    if result.get("status") == "updated":
                        now_utc = datetime.now(_tz.utc).strftime("%b %d, %Y %H:%M UTC")
                        last_refresh_ph.markdown(f'<span style="{_CELL_STYLE};color:#22c55e">{now_utc}</span>', unsafe_allow_html=True)
                        action_ph.markdown('<span style="font-size:12px;font-weight:600;color:#22c55e">✓ Done</span>', unsafe_allow_html=True)
                        st.toast(f"✓ {ticker} models updated")
                        send_model_refresh_email(triggered_by=user_email, results=[result])
                    elif result.get("status") == "up_to_date":
                        action_ph.markdown('<span style="font-size:12px;color:#3b82f6">Up to date</span>', unsafe_allow_html=True)
                    else:
                        action_ph.markdown(f'<span style="font-size:11px;color:#ef4444">{result.get("message","Error")[:50]}</span>', unsafe_allow_html=True)

            st.markdown('<hr style="margin:2px 0;border:none;border-top:1px solid #ECECEE;">', unsafe_allow_html=True)

    st.markdown("---")
    if can_run:
        if st.button("Refresh All", key="refresh_all_btn"):
            with st.spinner("Running all forecast models — this may take several minutes…"):
                results = sync_all_eligible(force=True)
                clear_revenue_forecast_caches()
                get_refresh_table_data.clear()
            updated = sum(1 for r in results if r.get("status") == "updated")
            errors  = sum(1 for r in results if r.get("status") == "error")
            st.success(f"Refresh All complete — {updated} updated, {errors} errors.")
            st.toast(f"✓ Refresh All done: {updated} updated")
            send_model_refresh_email(triggered_by=user_email, results=results)


def main() -> None:
    inject_red_spinner_css()
    page_start = perf_counter()

    # Lazy schema init — runs once per session
    if not st.session_state.get("_forecast_schema_inited"):
        _schema_t = perf_counter()
        try:
            ensure_forecast_columns()
            backfill_company_info()
        except Exception as _exc:
            log_structured_error(_exc, page="forecasting", component="main", operation="schema_init")
        st.session_state["_forecast_schema_inited"] = True
        log_timing("FORECASTING_schema_init", (perf_counter() - _schema_t) * 1000,
                   "once_per_session", level="INFO")

    user_email: Optional[str] = get_current_user()

    can_view_popup, can_run_models = _get_refresh_permissions(user_email)

    # Pre-warm dialog cache in background so popup opens instantly
    if can_view_popup:
        import threading as _threading
        _threading.Thread(target=get_refresh_table_data, args=("annual",), daemon=True).start()
        del _threading

    _companies_t = perf_counter()
    companies = RevenueForecastService.get_companies()
    log_timing("FORECASTING_get_companies", (perf_counter() - _companies_t) * 1000,
               f"count={len(companies)} cached={'yes' if (perf_counter()-_companies_t)*1000 < 50 else 'no'}",
               level="INFO")
    if not companies:
        render_header(full_width=True, current_page='forecasting')
        st.error('No revenue forecast coverage is available in staging right now.')
        render_coresight_footer()
        return

    initial_ticker = _resolve_initial_ticker(companies)
    if initial_ticker is None:
        render_header(full_width=True, current_page='forecasting')
        st.error('No supported ticker could be resolved for Revenue Estimates.')
        render_coresight_footer()
        return

    # Only init from query param / default on first visit — never overwrite a user selection
    _valid_tickers = {row['ticker'] for row in companies} | {_ALL_SENTINEL}
    _current = st.session_state.get('estimates_ticker')
    if _current not in _valid_tickers:
        st.session_state.estimates_ticker = initial_ticker

    _display_ticker = st.session_state.estimates_ticker
    if _display_ticker != _ALL_SENTINEL:
        st.session_state.active_ticker = _display_ticker
    render_header(full_width=True, current_page='forecasting', ticker=st.session_state.get('active_ticker', initial_ticker))
    _render_css()

    # Build dropdown: placeholder → All Companies → individual companies
    _select_entry = {'ticker': _SELECT_SENTINEL, 'label': '— Select Company —', 'name': ''}
    _all_entry    = {'ticker': _ALL_SENTINEL,    'label': 'All Companies',         'name': 'All Companies'}
    companies_with_all = [_select_entry, _all_entry] + list(companies)
    company_map = {row['ticker']: row for row in companies_with_all}

    def _on_ticker_change() -> None:
        selected = st.session_state.estimates_ticker
        if selected == _SELECT_SENTINEL:
            return
        st.session_state.active_ticker = selected if selected != _ALL_SENTINEL else st.session_state.get('active_ticker', initial_ticker)
        if selected not in (_ALL_SENTINEL, _SELECT_SENTINEL):
            st.query_params['ticker'] = selected

    st.markdown("<div class='rev-shell'>", unsafe_allow_html=True)

    _cur = st.session_state.estimates_ticker
    if _cur == _ALL_SENTINEL:
        _display_name = 'All Companies'
    elif _cur == _SELECT_SENTINEL:
        _display_name = company_map.get(st.session_state.get('active_ticker', initial_ticker), {}).get('name', '')
    else:
        _display_name = company_map.get(_cur, {}).get('name', _cur)
    _display_name_safe = _display_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace("'", '&#39;')

    _col_widths = [3.2, 1]
    _cols = st.columns(_col_widths, vertical_alignment="top")
    header_left = _cols[0]
    header_right = _cols[-1]

    with header_left:
        st.markdown(
            f'<div class="est-header-section">'
            f'  <div class="est-header-left">'
            f'    <div class="est-page-title">Revenue Forecasting</div>'
            f'    <div class="est-company-name-row">'
            f'      <span>{_display_name_safe}</span>'
            f'      <span class="est-dropdown-arrow">'
            f'        <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
            f'          <path d="M6 9L12 15L18 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            f'        </svg>'
            f'      </span>'
            f'    </div>'
            f'  </div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.selectbox(
            'Select Company',
            options=[row['ticker'] for row in companies_with_all],
            format_func=lambda t: company_map[t]['label'],
            key='estimates_ticker',
            on_change=_on_ticker_change,
            label_visibility='collapsed',
        )

    if can_view_popup:
        _btn_col, _ = st.columns([1, 4])
        with _btn_col:
            if st.button("Refresh Data", key="open_refresh_dialog", type="primary"):
                _refresh_dialog(can_run=can_run_models, user_email=user_email or "")

    selected_ticker = st.session_state.estimates_ticker
    # If placeholder selected, fall back to last valid company
    if selected_ticker == _SELECT_SENTINEL:
        selected_ticker = st.session_state.get('active_ticker', initial_ticker)

    # ── ALL COMPANIES — Download-only view ────────────────────────────────
    if selected_ticker == _ALL_SENTINEL:
        _all_key = '_est_all_xl_bytes'
        _ac1, _ac2 = st.columns([5, 1], vertical_alignment='center')
        with _ac1:
            st.markdown(
                '<p style="font-size:13px;color:#444;margin:24px 0 0;">'
                '<strong style="color:#1B6B24;">All Companies — Model Forecasting Data</strong> — '
                'Ensemble Overview, All Models Detail, Backtest Summary (3 sheets, all companies).</p>',
                unsafe_allow_html=True,
            )
        with _ac2:
            _all_bytes = st.session_state.get(_all_key)
            _just_built = st.session_state.pop('_est_all_xl_just_built', False)
            if _all_bytes is not None:
                # auto_click=True only on the rerun immediately after building — fires download automatically
                _render_excel_js_download(_all_bytes, 'All_Companies_Revenue_Forecasting.xlsx', 'Excel', auto_click=_just_built)
            else:
                if st.button('Excel', key='dl_all_btn', use_container_width=True):
                    with st.spinner('Building Excel for all companies…'):
                        try:
                            st.session_state[_all_key] = _build_forecast_excel_all(companies)
                            st.session_state['_est_all_xl_just_built'] = True
                            st.rerun()
                        except Exception as _exc:
                            log_structured_error(_exc, page='forecasting', component='main', operation='BUILD_ALL_EXCEL')
                            st.error(f'Could not generate Excel: {_exc}')
        render_coresight_footer()
        return

    # ── INDIVIDUAL TICKER view — slider only shown here ───────────────────
    with header_right:
        st.markdown('<div class="est-horizon-label">Forecast Horizon</div>', unsafe_allow_html=True)
        forecast_periods = st.slider(
            'Forecast years',
            min_value=1,
            max_value=5,
            value=st.session_state.get('estimates_forecast_periods', 5),
            step=1,
            key='estimates_forecast_periods',
            label_visibility='collapsed',
            format='%d yr',
        )

    dashboard_start = perf_counter()
    try:
        with st.spinner(f'Loading revenue forecasts for {selected_ticker}...'):
            payload = RevenueForecastService.get_company_dashboard(selected_ticker, periods=forecast_periods)
    except Exception as exc:
        log_structured_error(
            exc,
            page='forecasting',
            component='main',
            operation='LOAD_DASHBOARD',
            context={'ticker': selected_ticker},
        )
        st.error('The Revenue Estimates page could not load this ticker.')
        render_coresight_footer()
        return
    dashboard_elapsed = (perf_counter() - dashboard_start) * 1000
    log_timing(
        'ESTIMATES_PAGE_LOAD_DASHBOARD',
        dashboard_elapsed,
        f'ticker={selected_ticker} source={payload.get("source", "unknown")} models={len(payload.get("available_models", []))}',
    )

    summary = payload.get('summary', {})
    global _EST_DISPLAY_CURRENCY
    _EST_DISPLAY_CURRENCY = (
        payload.get("reported_currency")
        or payload.get("display_currency")
        or summary.get("reported_currency")
        or summary.get("display_currency")
        or "USD"
    )
    timings = payload.get('timings_ms', {})
    actual_df = _company_frame(payload)
    training_df = _training_frame(payload)
    forecast_df = _forecast_frame(payload)
    # Align with slider (payload already limited to forecast_periods)
    if not forecast_df.empty:
        forecast_df = forecast_df.head(forecast_periods).reset_index(drop=True)
    backtest_df = _backtest_frame(payload)
    raw_forecasts = payload.get('raw_forecasts', {})
    model_keys = _available_model_keys(forecast_df)
    scenario_keys = _available_scenario_keys(forecast_df)
    company_name = payload.get('company_name', selected_ticker)
    source_label = payload.get('source_label', 'Unknown')
    source_note = summary.get('source_note', 'Read-only staging data')
    outlier_years = payload.get('outlier_years', [])
    total_runtime = timings.get('total') or dashboard_elapsed

    # Build Excel bytes once per ticker+periods, cache in session_state for true 1-click download
    _sgl_key = f'_est_sgl_xl_{selected_ticker}_{forecast_periods}'
    if _sgl_key not in st.session_state:
        try:
            st.session_state[_sgl_key] = _build_forecast_excel_single(
                selected_ticker, company_name,
                training_df, actual_df, forecast_df,
                backtest_df, model_keys, scenario_keys, payload,
            )
        except Exception as _exc:
            log_structured_error(_exc, page='forecasting', component='main', operation='BUILD_SINGLE_EXCEL_EAGER')
            st.session_state[_sgl_key] = None

    render_start = perf_counter()

    chips_html = f'<span class="rev-chip">Historical years: {_fmt_int(summary.get("historical_rows"))}</span>'
    if outlier_years:
        chips_html += f'<span class="rev-chip">{_fmt_int(len(outlier_years))} outlier year(s) excluded</span>'
    st.markdown(
        f'<div class="rev-hero">'
        f'<span class="rev-hero-company">{company_name} ({selected_ticker})</span>'
        f'<span class="rev-hero-divider"></span>'
        f'<div class="rev-chip-row">{chips_html}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    metric_cols = st.columns(5)
    with metric_cols[0]:
        _render_card('Latest Actual Revenue', _fmt_billions(summary.get('latest_actual_revenue_billions')), f"Period ending {_fmt_year(summary.get('latest_actual_date'))}")
    with metric_cols[1]:
        _render_card('Ensemble — Next Year', _fmt_billions(summary.get('next_forecast_value_billions')), f"Forecast year {_fmt_year(summary.get('forecast_start_year'))}")
    with metric_cols[2]:
        best_mape = summary.get('best_mape')
        best_method_raw = payload.get('best_method') or summary.get('best_method_display', '')
        best_method_key = _METHOD_DISPLAY_TO_KEY.get(best_method_raw, best_method_raw)
        best_full_name = MODEL_FULL_LABELS.get(best_method_key) or summary.get('best_method_display') or _model_label(best_method_key or '—')
        # Render card with hoverable style — invisible button overlay sits on top
        st.markdown(
            f'<div class="rev-best-card-anchor">'
            f'<div class="rev-card rev-card-hoverable">'
            f'<div class="rev-card-kicker">Best Forecasting Model</div>'
            f'<div class="rev-card-value-sm">{best_full_name}</div>'
            f'<div class="rev-card-meta">MAPE: {_fmt_pct(best_mape)} · Click to open</div>'
            f'</div></div>',
            unsafe_allow_html=True,
        )
        if best_method_key and st.button('', key='goto_best_model_card_btn', use_container_width=True):
            # Reliable behavior: always open the model details dialog immediately.
            # (Tab-jumping via DOM JS is brittle across Streamlit rerenders.)
            _model_popup(best_method_key, payload, training_df, actual_df, forecast_df, model_keys)
    with metric_cols[3]:
        _render_card('Historical Years in Training', _fmt_int(summary.get('historical_rows')), f"Total rows loaded: {_fmt_int(summary.get('actual_rows'))}")
    with metric_cols[4]:
        _render_card('Forecast Horizon', f"{_fmt_int(summary.get('forecast_periods'))} years", f"Through {_fmt_year(summary.get('forecast_end_year'))}")

    tabs = st.tabs(['Overview', 'Models', 'Test'])

    with tabs[0]:
        st.markdown('<h3 class="rev-section-title">Forecast overview</h3>', unsafe_allow_html=True)
        st.markdown(
            '<p class="rev-section-copy">The chart shows the cleaned historical series that feeds the engine, then overlays every model output. The table below is the full forecast from all models.</p>',
            unsafe_allow_html=True,
        )
        st.plotly_chart(
            _build_overview_chart(training_df, actual_df, forecast_df, model_keys, scenario_keys),
            use_container_width=True,
            config={'displayModeBar': False},
        )
        forecast_table = _forecast_table(forecast_df, model_keys, scenario_keys)
        if not forecast_table.empty:
            st.dataframe(forecast_table, use_container_width=True, hide_index=True)
        else:
            st.info('No forecast rows are available for this ticker yet.')

        _render_implied_growth_table(forecast_df, payload)
        _render_historical_stats(payload, training_df)

        if model_keys:
            st.markdown('<h3 class="rev-section-title" style="margin-top:36px;">Explore individual models</h3>', unsafe_allow_html=True)
            st.markdown('<p class="rev-section-copy">Click any model to see its full methodology, chart, and forecast metrics.</p>', unsafe_allow_html=True)
            st.markdown('<div class="rev-explore-models">', unsafe_allow_html=True)
            popup_cols = st.columns(len(model_keys))
            for col, key in zip(popup_cols, model_keys):
                with col:
                    if st.button(MODEL_FULL_LABELS.get(key, _model_label(key)), key=f'popup_{key}', use_container_width=True):
                        _model_popup(key, payload, training_df, actual_df, forecast_df, model_keys)
            st.markdown('</div>', unsafe_allow_html=True)

    with tabs[1]:
        st.markdown('<h3 class="rev-section-title">Model comparison</h3>', unsafe_allow_html=True)
        st.markdown(
            '<p class="rev-section-copy">Each tab shows one forecasting approach — what it assumes, and where it thinks revenue is heading. Use these to understand why different models land at different numbers for the same company.</p>',
            unsafe_allow_html=True,
        )
        nested_labels = [_model_label(key) for key in model_keys]
        if scenario_keys:
            nested_labels.append('Scenarios')
        if not nested_labels:
            st.info('No model forecasts are available for this ticker yet.')
        else:
            model_tabs = st.tabs(nested_labels)
            for idx, key in enumerate(model_keys):
                with model_tabs[idx]:
                    _render_model_panel(key, payload, training_df, actual_df, forecast_df, model_keys)
            if scenario_keys:
                with model_tabs[-1]:
                    st.markdown('<h3 class="rev-section-title">Scenarios</h3>', unsafe_allow_html=True)
                    st.markdown(
                        '<p class="rev-section-copy">'
                        'Three revenue paths — Pessimistic, Baseline, Optimistic — based on the company\'s own historical growth distribution. '
                        'The bold black line is the Ensemble forecast for reference.'
                        '</p>',
                        unsafe_allow_html=True,
                    )
                    st.plotly_chart(_scenario_chart(forecast_df), use_container_width=True, config={'displayModeBar': False})

    with tabs[2]:
        st.markdown('<h3 class="rev-section-title">Backtesting</h3>', unsafe_allow_html=True)
        st.markdown(
            '<p class="rev-section-copy">The engine holds out the most recent 2 years, trains each model on the earlier history, then checks how closely each model predicted those hidden years. The lowest MAPE wins and becomes the best model. The top-3 become the ensemble.</p>',
            unsafe_allow_html=True,
        )
        if backtest_df.empty:
            st.info('Backtest results are not available for this ticker.')
        else:
            st.plotly_chart(_build_backtest_chart(backtest_df), use_container_width=True, config={'displayModeBar': False})
            display = backtest_df.copy()
            display['Method'] = display['display']
            display['MAPE'] = display['mape'].apply(lambda value: _fmt_pct(value))
            display['Bias'] = display['bias'].apply(lambda value: f'{float(value):+,.2f}B' if pd.notna(value) else '—')
            display['RMSE'] = display['rmse'].apply(lambda value: f'{float(value):,.2f}B' if pd.notna(value) else '—')
            st.dataframe(
                display[['Rank', 'Method', 'MAPE', 'Bias', 'RMSE']].sort_values('Rank'),
                use_container_width=True,
                hide_index=True,
            )

        # ── Scenario Analysis ──────────────────────────────────────────────
        if not forecast_df.empty and scenario_keys:
            st.markdown('<h3 class="rev-section-title" style="margin-top:36px;">Scenario Analysis</h3>', unsafe_allow_html=True)
            st.markdown(
                '<p class="rev-section-copy">Scenarios are derived from historical growth percentiles — not from model fitting. Pessimistic = 25th percentile growth; Baseline = 50th percentile (median); Optimistic = 75th percentile. The Ensemble line is overlaid for comparison.</p>',
                unsafe_allow_html=True,
            )
            st.plotly_chart(_scenario_chart(forecast_df, training_df, actual_df), use_container_width=True, config={'displayModeBar': False})

            # Exact-millions table: year | ensemble | pessimistic | baseline | optimistic
            _scen_cols = {'Year': forecast_df['year'].astype(int)}
            if 'ensemble' in forecast_df.columns:
                _scen_cols['Ensemble (mm)'] = forecast_df['ensemble'].apply(_fmt_mm)
            for _sk in scenario_keys:
                if _sk in forecast_df.columns:
                    _scen_cols[_model_label(_sk) + ' (mm)'] = forecast_df[_sk].apply(_fmt_mm)
            st.dataframe(pd.DataFrame(_scen_cols), use_container_width=True, hide_index=True)

        # ── Implied Growth Rates ───────────────────────────────────────────
        _summary = payload.get('summary', {})
        _last_actual_b = _summary.get('latest_actual_revenue_billions')
        if not forecast_df.empty and 'ensemble' in forecast_df.columns and _last_actual_b is not None:
            st.markdown('<h3 class="rev-section-title" style="margin-top:36px;">Implied Growth Rates</h3>', unsafe_allow_html=True)
            st.markdown(
                '<p class="rev-section-copy">Year-over-year growth implied by the ensemble forecast, starting from the last actual revenue.</p>',
                unsafe_allow_html=True,
            )
            _ens = forecast_df[['year', 'ensemble']].dropna().copy()
            _all_vals = [float(_last_actual_b)] + _ens['ensemble'].tolist()
            _yoy = [(_all_vals[i + 1] / _all_vals[i] - 1) * 100 for i in range(len(_all_vals) - 1)]
            _growth_df = pd.DataFrame({
                'Year': _ens['year'].astype(int).tolist(),
                'Forecast (mm)': [f'{float(v) * 1000:,.2f}' for v in _ens['ensemble'].tolist()],
                'YoY Growth %': [f'{g:.2f}%' for g in _yoy],
            })
            st.dataframe(_growth_df, use_container_width=True, hide_index=True)

        st.write('')
        bt_left, bt_right = st.columns(2)
        with bt_left:
            st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
            st.markdown('<h3 class="rev-section-title">How to read MAPE</h3>', unsafe_allow_html=True)
            st.markdown(
                """
**MAPE = Mean Absolute Percentage Error**

Formula:
```
MAPE = mean(|predicted − actual| / actual) × 100
```

**Interpretation thresholds (annual revenue forecasting):**

| MAPE range | Reading |
|---|---|
| < 5% | Excellent — model closely tracked the held-out years |
| 5% – 15% | Acceptable — reasonable for annual revenue data |
| 15% – 25% | Weak — model missed by a meaningful margin |
| > 25% | Poor — model should not be trusted for this company |

**Lower is always better.** The winning model becomes the top-ranked in the table above, and the top 3 by MAPE form the ensemble.
                """,
            )
            st.markdown('</div>', unsafe_allow_html=True)
        with bt_right:
            st.markdown('<div class="rev-panel">', unsafe_allow_html=True)
            st.markdown('<h3 class="rev-section-title">Bias and RMSE explained</h3>', unsafe_allow_html=True)
            st.markdown(
                """
**Bias** = mean(predicted − actual)

- Positive bias → model consistently over-predicted the holdout years
- Negative bias → model consistently under-predicted
- Bias close to 0 → model predictions were centered around the truth

**RMSE = Root Mean Squared Error**

Formula:
```
RMSE = sqrt(mean((predicted − actual)²))
```

- Punishes large misses more heavily than small ones
- Units are in the same currency as revenue (billions here)
- Use RMSE alongside MAPE — a model with low MAPE but high RMSE had one very large miss

**Example:** A model with MAPE 2.3% and RMSE 0.1B is excellent. A model with MAPE 10% and RMSE 2.7B missed by a consistent margin across both holdout years.
                """,
            )
            st.markdown('</div>', unsafe_allow_html=True)

    # Best-model navigation now uses the dialog popover for reliability.

    # ── Excel download — true 1-click JS button ───────────────────────────
    st.markdown('<hr style="margin:28px 0 12px;border:none;border-top:1px solid #e5e5e5;">', unsafe_allow_html=True)
    _dl_col1, _dl_col2 = st.columns([5, 1], vertical_alignment='center')
    with _dl_col1:
        st.markdown(
            f'<p style="font-size:13px;color:#444;margin:4px 0;">'
            f'<strong style="color:#1B6B24;">📥 Export {company_name} ({selected_ticker})</strong> — '
            f'Historical actuals, all 6 model forecasts, scenarios, backtest & implied growth rates.</p>',
            unsafe_allow_html=True,
        )
    with _dl_col2:
        _xl = st.session_state.get(_sgl_key)
        if _xl:
            _render_excel_js_download(_xl, f'{selected_ticker}_Revenue_Forecasting.xlsx', 'Excel')

    st.markdown('</div>', unsafe_allow_html=True)
    render_elapsed = (perf_counter() - render_start) * 1000
    log_timing(
        'ESTIMATES_PAGE_RENDER',
        render_elapsed,
        f'ticker={selected_ticker} models={len(model_keys)} scenarios={len(scenario_keys)}',
    )
    log_timing(
        'ESTIMATES_PAGE_TOTAL',
        (perf_counter() - page_start) * 1000,
        f'ticker={selected_ticker} dashboard_ms={dashboard_elapsed:.2f} render_ms={render_elapsed:.2f}',
    )
    render_coresight_footer()


try:
    main()
except Exception as exc:
    log_structured_error(exc, page='forecasting', component='main', operation='PAGE_RENDER')
    st.error('Revenue Estimates could not be rendered.')
