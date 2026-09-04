"""
Forecast auto-refresh email report — rich tabular + charted HTML.

Builds the notification email sent by ``forecast_refresh_service.send_model_refresh_email``
after the auto-scheduler refreshes one or more companies. For every updated ticker it
pulls the stored model detail from ``coreiq_model_forecasts`` and renders:

  * a run-summary table (all updated companies),
  * per company: a best-model forecast table (year → value),
  * per company: a model backtest table (every model tried + its MAPE),
  * per company: two PNG charts drawn with Pillow (no matplotlib dependency) —
      1. forecast trajectory with a baseline/optimistic/pessimistic scenario band,
      2. model-accuracy bars (MAPE per model, best highlighted).

Charts are returned as raw PNG bytes with a stable chart-id. The caller embeds them
either as ``cid:`` attachments (email — renders in Outlook/Gmail) or as base64
``data:`` URIs (browser preview). One HTML builder serves both so the preview is exact.
"""
from __future__ import annotations

import base64
import io
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from core.database import db_manager
from utils.server_logger import log_structured_error

# Brand-ish palette (Coresight red accent + neutral grays).
_ACCENT = (200, 16, 46)        # #C8102E — ensemble (final output) / primary line
_ACTUAL = (37, 55, 90)         # #25375A — historical actuals line (deep slate-blue)
_BEST_LINE = (120, 120, 120)   # best individual model — light reference line
_BASELINE = (90, 90, 90)       # baseline scenario line
_BAND = (200, 16, 46, 38)      # scenario band fill (RGBA, translucent)
_GRID = (228, 228, 228)
_AXIS = (150, 150, 150)
_TEXT = (34, 34, 34)
_MUTED = (120, 120, 120)
_BAR_BEST = (200, 16, 46)
_BAR_OTHER = (170, 178, 190)

_ANNUAL_TABLE = "coreiq_model_forecasts"
_QUARTERLY_TABLE = "coreiq_model_forecasts_quarterly"

# Friendly labels for the model_key values the engine stores.
_MODEL_LABEL = {
    "holt": "Holt (linear trend)",
    "linear": "Linear regression",
    "exp_smoothing": "Exponential smoothing",
    "cagr": "CAGR",
    "weighted_avg": "Weighted average",
    "ma_trend": "Moving-average trend",
    "seasonal_naive": "Seasonal naive",
    "flat_carry": "Flat carry-forward",
    "ensemble": "Ensemble",
    "scenario_baseline": "Scenario · baseline",
    "scenario_optimistic": "Scenario · optimistic",
    "scenario_pessimistic": "Scenario · pessimistic",
}


def _label(model_key: str) -> str:
    return _MODEL_LABEL.get(model_key, (model_key or "").replace("_", " ").title())


def _fmt_mm(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.1f}M"
    except (TypeError, ValueError):
        return "—"


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Period identity
#
# Every series in this report used to be keyed by fiscal_year alone. That is
# unique for the annual table but NOT for the quarterly one, where a year holds
# up to four rows — so dict(series) silently kept whichever quarter came last
# (14 of 20 points lost for BIRK) and the header could show one quarter's
# ensemble while the table below showed another's. Key on a sortable period id
# instead, and carry a label for display.
# ─────────────────────────────────────────────────────────────────────────────

def _period_key(row: Dict[str, Any], period_type: str) -> int:
    fy = int(row["fiscal_year"])
    if period_type == "quarterly":
        return fy * 4 + int(row.get("fiscal_quarter") or 0)
    return fy


def _period_label(key: int, period_type: str) -> str:
    if period_type == "quarterly":
        q = key % 4 or 4
        y = key // 4 if key % 4 else key // 4 - 1
        return f"Q{q} FY{y}"
    return f"FY{key}"


def _period_heading(period_type: str) -> str:
    return "Fiscal Quarter" if period_type == "quarterly" else "Fiscal Year"


def _change_heading(period_type: str) -> str:
    return "QoQ" if period_type == "quarterly" else "YoY"


def _fmt_forecast_mm(v: Optional[float]) -> str:
    """Format a MODEL forecast value.

    Unbounded extrapolators (holt, exp_smoothing) can run a declining series
    straight through zero — 12 annual and 93 quarterly rows are negative in the
    store today, worst -14,310. A negative revenue is not a forecast, it is the
    model breaking down, and printing "-$14,310.5M" in a push notification reads
    as a broken system. Show it as not-meaningful instead; the model still appears
    with its MAPE so the reader can see it was considered and rejected.
    """
    if v is None:
        return "—"
    try:
        if float(v) <= 0:
            return '<span title="Model extrapolated below zero — not a meaningful revenue forecast" style="color:#999;">n/m</span>'
    except (TypeError, ValueError):
        return "—"
    return _fmt_mm(v)


def fetch_actuals(ticker: str, period_type: str = "annual") -> Optional[List[Tuple[int, float]]]:
    """Historical actual revenue as [(fiscal_year, value_millions)], reported currency.

    Reuses the exact ingest path the forecaster feeds on so the units line up with
    the stored ``value_millions`` forecasts. Returns [(period_key, value)] where the
    key matches ``_period_key`` for the cadence — quarterly included, which is why
    the quarterly mail no longer reports "0 historical actual years".
    Imports are lazy to avoid any import cycle; any failure returns None (email never lost).
    """
    try:
        from data.source_router import get_company_source
        from data.revenue_forecast_service import (
            RevenueForecastService, _source_table, _yf_reported_currency,
        )
        source = get_company_source(ticker)
        if source not in ("SEC", "YFinance"):
            return None
        # Each cadence has its own fetch/normalise pair. Using the annual pair for a
        # quarterly report returned one row per YEAR, which then rendered as
        # "Q4 FY2019, Q4 FY2020, ..." — annual figures wearing quarterly labels.
        if period_type == "quarterly":
            raw = RevenueForecastService._fetch_quarterly_rows(ticker, source)
        else:
            raw = RevenueForecastService._fetch_actual_rows(ticker, source)
        if not raw:
            return None
        raw_dicts = [dict(r) for r in raw]
        if source == "SEC":
            reported = raw_dicts[0].get("reported_currency") or "USD"
        else:
            base = ticker.split(".")[0] if "." in ticker else ticker
            reported = _yf_reported_currency(base) or "USD"
        if period_type == "quarterly":
            rows = RevenueForecastService._normalize_quarterly_rows(
                raw_rows=raw_dicts, source=source, reported_currency=reported,
            )
        else:
            rows = RevenueForecastService._normalize_actual_rows(
                raw_rows=raw_dicts, source=source, source_table=_source_table(source),
                reported_currency=reported, display_currency=reported,
            )
        out = [(_period_key(r, period_type), float(r["total_revenue_billions"]) * 1000.0)
               for r in rows
               if r.get("fiscal_year") is not None and r.get("total_revenue_billions") is not None]
        out.sort()
        return out or None
    except Exception as exc:
        log_structured_error(exc, page="forecast_email_report",
                             component="fetch_actuals", operation="SELECT")
        return None


def fetch_ticker_detail(ticker: str, period_type: str = "annual") -> Optional[Dict[str, Any]]:
    """Pull the stored forecast detail for one ticker. Returns None if nothing stored."""
    table = _QUARTERLY_TABLE if period_type == "quarterly" else _ANNUAL_TABLE
    # Quarterly rows need the quarter to form a unique, ordered period key.
    _qcol = "fiscal_quarter, " if period_type == "quarterly" else ""
    _order = "fiscal_year, fiscal_quarter" if period_type == "quarterly" else "fiscal_year"
    try:
        best = db_manager.execute_query_readonly(
            f"SELECT fiscal_year, {_qcol}model_key, metric, value_millions, mape, periods_ahead, "
            f"       last_actual_date, company_name, exchange, computed_at "
            f"FROM {table} WHERE ticker = :t AND is_best_model = 1 ORDER BY {_order}",
            {"t": ticker},
        )
        if not best:
            return None
        metric = best[0].get("metric")
        # One row per model for the NEXT forecast period.
        #
        # The annual table is keyed by fiscal_year, so pinning the year is enough.
        # The quarterly table is keyed by (fiscal_year, fiscal_quarter), so pinning
        # only the year returns every model once per quarter in that year — the
        # duplicate "Exponential smoothing 13.0% $4.2M / 13.0% $10.0M" rows seen in
        # the quarterly mail (same MAPE, because MAPE is per model; different
        # values, because they were different quarters). Pin the full period key.
        if period_type == "quarterly":
            models = db_manager.execute_query_readonly(
                f"SELECT model_key, is_best_model, is_ensemble, mape, value_millions "
                f"FROM {table} "
                f"WHERE ticker = :t AND metric = :m "
                f"  AND (fiscal_year, fiscal_quarter) = ("
                f"       SELECT fiscal_year, fiscal_quarter FROM {table} "
                f"       WHERE ticker = :t AND metric = :m AND is_best_model = 1 "
                f"       ORDER BY fiscal_year, fiscal_quarter LIMIT 1) "
                f"ORDER BY (mape IS NULL), mape",
                {"t": ticker, "m": metric},
            )
        else:
            models = db_manager.execute_query_readonly(
                f"SELECT model_key, is_best_model, is_ensemble, mape, value_millions "
                f"FROM {table} "
                f"WHERE ticker = :t AND metric = :m "
                f"  AND fiscal_year = (SELECT MIN(fiscal_year) FROM {table} "
                f"                     WHERE ticker = :t AND metric = :m AND is_best_model = 1) "
                f"ORDER BY (mape IS NULL), mape",
                {"t": ticker, "m": metric},
            )
        scenarios = db_manager.execute_query_readonly(
            f"SELECT fiscal_year, {_qcol}model_key, value_millions FROM {table} "
            f"WHERE ticker = :t AND metric = :m "
            f"  AND model_key IN ('scenario_baseline','scenario_optimistic','scenario_pessimistic') "
            f"ORDER BY {_order}",
            {"t": ticker, "m": metric},
        )
        # Ensemble is the team's final output — surface its full per-year series
        # (already stored with model_key='ensemble') alongside the best model.
        ensemble = db_manager.execute_query_readonly(
            f"SELECT fiscal_year, {_qcol}value_millions FROM {table} "
            f"WHERE ticker = :t AND metric = :m AND model_key = 'ensemble' "
            f"ORDER BY {_order}",
            {"t": ticker, "m": metric},
        )
        return {
            "ticker": ticker,
            "metric": metric,
            "company_name": best[0].get("company_name") or "",
            "exchange": best[0].get("exchange") or "",
            "best_model": best[0].get("model_key") or "",
            "best_mape": best[0].get("mape"),
            "last_actual_date": best[0].get("last_actual_date"),
            "computed_at": best[0].get("computed_at"),
            "period_type": period_type,
            "series": [(_period_key(r, period_type), float(r["value_millions"]))
                       for r in best if r.get("value_millions") is not None],
            "ensemble_series": [(_period_key(r, period_type), float(r["value_millions"]))
                                for r in (ensemble or []) if r.get("value_millions") is not None],
            "actuals": fetch_actuals(ticker, period_type),
            "models": models or [],
            "scenarios": scenarios or [],
        }
    except Exception as exc:
        log_structured_error(exc, page="forecast_email_report",
                             component="fetch_ticker_detail", operation="SELECT")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Charts (Pillow — no matplotlib)
# ─────────────────────────────────────────────────────────────────────────────
def _font(size: int):
    from PIL import ImageFont
    for p in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


# Final chart pixel size, and the size the <img> is declared at: an image map's
# coordinates address the image's own pixel grid, so a CSS-scaled image would put
# every hotspot in the wrong place. 600px also fits Outlook's content column, so
# Outlook will not shrink the image and break the map.
CHART_W, CHART_H = 600, 260


def _nice_axis(lo: float, hi: float) -> Tuple[float, float]:
    """Axis bounds on round numbers, divisible by 4 so the gridlines read cleanly.

    The old code padded by 15% and split the raw range four ways, which produced
    axes labelled -1,343 / 1,758 / 4,859 / 7,960 / 11,061 — and a negative floor
    on a revenue chart where nothing is negative. Revenue starts at zero unless
    the data actually goes below it.
    """
    if hi <= lo:
        hi = lo + abs(lo or 1.0) * 0.1
    floor_at_zero = lo >= 0
    lo_p = 0.0 if floor_at_zero else lo - (hi - lo) * 0.12
    hi_p = hi + (hi - lo) * 0.12

    span = hi_p - lo_p or 1.0
    raw = span / 4.0
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    # A coarse ladder (1, 2, 2.5, 5, 10) overshot badly: data peaking at 9,300
    # jumped from a 2,500 step to 5,000 and drew an axis to 20,000. The extra
    # rungs keep the top tick close to the data.
    for mult in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 8, 10):
        step = mult * mag
        if step * 4 >= hi_p - lo_p:
            break
    lo_r = 0.0 if floor_at_zero else math.floor(lo_p / step) * step
    hi_r = lo_r + step * 4
    while hi_r < hi_p:                       # widen until the data fits
        step *= 2
        lo_r = 0.0 if floor_at_zero else math.floor(lo_p / step) * step
        hi_r = lo_r + step * 4
    return lo_r, hi_r


def _scenario_bands(scenarios: List[Dict[str, Any]], period_type: str = "annual") -> Dict[str, List[Tuple[int, float]]]:
    out: Dict[str, List[Tuple[int, float]]] = {"scenario_baseline": [], "scenario_optimistic": [], "scenario_pessimistic": []}
    for r in scenarios:
        k = r.get("model_key")
        if k in out and r.get("value_millions") is not None:
            out[k].append((_period_key(r, period_type), float(r["value_millions"])))
    for k in out:
        out[k].sort()
    return out


def draw_trajectory_png(
    detail: Dict[str, Any],
    hotspots: Optional[List[Dict[str, Any]]] = None,
) -> Optional[bytes]:
    """Historical actuals → ensemble forecast (the final output), with the best
    individual model as a light reference and the scenario band around the forecast.

    X-axis is one continuous timeline: historical fiscal years then forecast years.
    Forecast lines anchor to the last actual point so history and forecast connect.
    Degrades gracefully — if actuals are missing it draws forecast-only (old behaviour).

    Pass a list as ``hotspots`` to receive one dict per plotted point:
    ``{"left": pct, "top": pct, "title": "Q2 FY2026 — Ensemble $1,176.1M"}``.
    The HTML uses them to lay invisible tooltip targets over the image, so every
    value is readable on hover even where the chart only prints a few labels.
    Percentages, not pixels, because the image is displayed at 680px wide but
    drawn at 900 — pixel coordinates would not line up.
    """
    from PIL import Image, ImageDraw
    actuals = detail.get("actuals") or []            # [(fy, v)] historical
    ens     = detail.get("ensemble_series") or []    # [(fy, v)] ensemble forecast
    best    = detail.get("series") or []             # [(fy, v)] best individual model
    if not ens and not best:
        return None

    bands = _scenario_bands(detail.get("scenarios") or [], detail.get("period_type", "annual"))
    opt  = dict(bands.get("scenario_optimistic") or [])
    pes  = dict(bands.get("scenario_pessimistic") or [])
    base = dict(bands.get("scenario_baseline") or [])

    fc_years = sorted({y for y, _ in ens} | {y for y, _ in best})
    act_years = [y for y, _ in actuals]
    all_years = sorted(set(act_years) | set(fc_years))
    if len(all_years) < 2:
        return None
    idx = {y: i for i, y in enumerate(all_years)}

    ss = 2  # supersample for crisp downscale
    # Draw at the size the <img> is declared at. An image map addresses the
    # image's own pixel grid, so a CSS-scaled image puts every hotspot in the
    # wrong place — which is exactly what happened when this stayed at 900 while
    # the tag said 600.
    W, H = CHART_W * ss, CHART_H * ss
    ml, mr, mt, mb = 64 * ss, 42 * ss, 58 * ss, 46 * ss
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img, "RGBA")
    f_title, f_lab, f_val = _font(20 * ss), _font(14 * ss), _font(13 * ss)

    # Scale on what the reader is actually here for — the history, the ensemble
    # and the best model. The scenario band is context, and letting it drive the
    # axis destroys the chart: BIRK's optimistic scenario compounds to $8.3
    # TRILLION against real revenue of $169-731M, which pinned every real point
    # to a flat line on the floor under an axis running to 10,000,000. A band
    # that runs past the top is clipped instead, which also reads honestly as
    # "this scenario is off the chart".
    core_vals = [v for _, v in actuals] + [v for _, v in ens] + [v for _, v in best]
    band_vals = list(opt.values()) + list(pes.values()) + list(base.values())
    if not core_vals:
        core_vals = band_vals
    lo, hi = min(core_vals), max(core_vals)
    # Let the band widen the view a little, but never beyond 1.5x the core range.
    if band_vals:
        room = (hi - lo) * 1.5 if hi > lo else abs(hi or 1.0)
        hi = max(hi, min(max(band_vals), hi + room))
        lo = min(lo, max(min(band_vals), lo - room))
    vmin, vmax = _nice_axis(lo, hi)
    x0, x1, y0, y1 = ml, W - mr, mt, H - mb

    def px(y): return x0 + (x1 - x0) * (idx[y] / (len(all_years) - 1))
    def py(v): return y1 - (y1 - y0) * ((v - vmin) / (vmax - vmin))

    # ASCII only: when no TrueType font is found the Pillow fallback is a bitmap
    # font with no em-dash glyph, which printed a tofu box in the title.
    d.text((ml, 12 * ss), f"{detail['ticker']}  actuals + ensemble forecast ($M)",
           font=f_title, fill=_TEXT)

    # y gridlines + labels
    for g in range(5):
        gv = vmin + (vmax - vmin) * g / 4
        gy = py(gv)
        d.line([(x0, gy), (x1, gy)], fill=_GRID, width=1 * ss)
        d.text((10 * ss, gy - 8 * ss), f"{gv:,.0f}", font=f_val, fill=_MUTED)

    # X labels. A quarterly chart carries up to ~103 periods on the same 900px
    # canvas an annual chart uses for 12, so a fixed step of 2 smeared every label
    # into an unreadable ribbon. Derive the step from the MEASURED label width and
    # always keep the newest period, which is the one people look for.
    period_type = detail.get("period_type", "annual")
    labels = [_period_label(y, period_type) for y in all_years]
    label_w = max(d.textlength(lab, font=f_lab) for lab in labels)
    fits = max(2, int((x1 - x0) // (label_w + 14 * ss)))
    step = max(1, -(-len(all_years) // fits))          # ceil division
    for i, y in enumerate(all_years):
        # count back from the end so the last period is always drawn
        if (len(all_years) - 1 - i) % step:
            continue
        lab = labels[i]
        w = d.textlength(lab, font=f_lab)
        d.text((px(y) - w / 2, y1 + 10 * ss), lab, font=f_lab, fill=_TEXT)

    # faint divider between last actual and first forecast year
    if actuals and fc_years:
        bx = (px(act_years[-1]) + px(fc_years[0])) / 2
        for yy in range(int(y0), int(y1), 8 * ss):
            d.line([(bx, yy), (bx, yy + 4 * ss)], fill=(180, 180, 180), width=1 * ss)

    # scenario band (optimistic top ↔ pessimistic bottom) over forecast years
    if opt and pes and all(y in opt and y in pes for y in fc_years):
        def pyc(v):   # clip to the plot box — the band may run far off scale
            return min(max(py(v), y0), y1)
        top = [(px(y), pyc(opt[y])) for y in fc_years]
        bot = [(px(y), pyc(pes[y])) for y in fc_years]
        if actuals:  # anchor the band to the last actual so it starts at history
            ay = act_years[-1]; av = dict(actuals)[ay]
            top = [(px(ay), py(av))] + top
            bot = [(px(ay), py(av))] + bot
        d.polygon(top + bot[::-1], fill=_BAND)

    anchor = (px(act_years[-1]), py(dict(actuals)[act_years[-1]])) if actuals else None

    # historical actuals — solid slate line + markers
    # One tooltip target per PERIOD, not per point. 16px dots on the markers meant
    # the cursor missed them almost everywhere; a full-height column means hovering
    # anywhere over that period works, which is how the in-app chart behaves too.
    _spot_vals: Dict[int, Dict[str, float]] = {}

    def _record(key, kind, value):
        # A scenario row can carry a period the axis does not have (the axis is
        # built from actuals + ensemble + best model). It cannot be plotted, so
        # it cannot be hovered either — skip rather than blow up in px().
        if hotspots is not None and key in idx:
            _spot_vals.setdefault(key, {})[kind] = value

    apts = [(px(y), py(v)) for y, v in actuals]
    for (y, v) in actuals:
        _record(y, "Actual", v)
    for a, b in zip(apts, apts[1:]):
        d.line([a, b], fill=_ACTUAL, width=3 * ss)
    for cx, cy in apts:
        r = 4 * ss
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_ACTUAL)
    if apts:  # label the last actual so the handoff value is explicit
        lx, lv = act_years[-1], dict(actuals)[act_years[-1]]
        _t = f"{lv:,.0f}"
        _tw = d.textlength(_t, font=f_val)
        _tx, _ty = px(lx) - _tw / 2, py(lv) + 8 * ss
        d.rectangle([_tx - 3 * ss, _ty - 2 * ss, _tx + _tw + 3 * ss, _ty + 15 * ss],
                    fill=(255, 255, 255))
        d.text((_tx, _ty), _t, font=f_val, fill=_ACTUAL)

    # best individual model — light thin reference line (anchored to last actual)
    bpts = ([anchor] if anchor else []) + [(px(y), py(v)) for y, v in best]
    for a, b in zip(bpts, bpts[1:]):
        d.line([a, b], fill=_BEST_LINE, width=2 * ss)

    # ensemble forecast — the highlighted final output (anchored to last actual)
    epts_data = [(px(y), py(v), v) for y, v in ens]
    for (y, v) in ens:
        _record(y, "Ensemble", v)
    for y, v in best:
        _record(y, "Best model", v)
    for y, v in opt.items():
        _record(y, "Optimistic", v)
    for y, v in pes.items():
        _record(y, "Pessimistic", v)
    epts = ([anchor] if anchor else []) + [(x, y) for x, y, _ in epts_data]
    for a, b in zip(epts, epts[1:]):
        d.line([a, b], fill=_ACCENT, width=3 * ss)
    # Every point kept its own value label, so 20 forecast quarters overprinted
    # into "1,17.2,221,4741,6141,706". Draw the markers for all of them but label
    # only as many as fit — first and last always, evenly spaced in between.
    if epts_data:
        val_w = max(d.textlength(f"{v:,.0f}", font=f_val) for _, _, v in epts_data)
        span = abs(epts_data[-1][0] - epts_data[0][0]) or (x1 - x0)
        room = max(1, int(span // (val_w + 12 * ss)))
        # On a dense seasonal chart the forecast is squeezed into the last fifth
        # of the canvas and the line zigzags through wherever a label would sit.
        # Below ~4 slots, show just the two numbers a reader actually wants:
        # where the forecast starts and where it ends.
        lab_step = (len(epts_data) if room < 4
                    else max(1, -(-len(epts_data) // room)))
    else:
        lab_step = 1
    last_i = len(epts_data) - 1
    # Spacing alone is not enough on a seasonal series: the points zigzag, so an
    # evenly spaced label can still land on its neighbour. Track what has been
    # drawn and skip anything that would overlap.
    drawn_spans: List[Tuple[float, float]] = []
    for i, (cx, cy, v) in enumerate(epts_data):
        r = 5 * ss
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_ACCENT)
        if not (i == 0 or i == last_i or i % lab_step == 0):
            continue
        txt = f"{v:,.0f}"
        tw = d.textlength(txt, font=f_val)
        lx0, lx1 = cx - tw / 2, cx + tw / 2
        if any(lx0 < px1 + 6 * ss and lx1 > px0 - 6 * ss for px0, px1 in drawn_spans):
            continue
        ly0 = cy - 26 * ss
        # A seasonal line zigzags straight through wherever the label sits, so the
        # digits were struck through by the series itself. Knock out the background
        # first — the number has to win against its own chart.
        d.rectangle([lx0 - 3 * ss, ly0 - 2 * ss, lx1 + 3 * ss, ly0 + 15 * ss],
                    fill=(255, 255, 255))
        d.text((lx0, ly0), txt, font=f_val, fill=_ACCENT)
        drawn_spans.append((lx0, lx1))

    # legend — one row under the title so long model names never collide
    ly = 34 * ss

    def _key(x, color, text, swatch=False, width=3):
        if swatch:
            d.rectangle([x, ly + 2 * ss, x + 24 * ss, ly + 14 * ss], fill=_BAND)
        else:
            d.line([(x, ly + 8 * ss), (x + 24 * ss, ly + 8 * ss)], fill=color, width=width * ss)
        d.text((x + 30 * ss, ly), text, font=f_val, fill=_TEXT)

    _key(ml, _ACTUAL, "Actual")
    _key(ml + 120 * ss, _ACCENT, "Ensemble (final)")
    _key(ml + 270 * ss, _BEST_LINE, "Best model", width=2)
    _key(ml + 390 * ss, None, "Scenario range", swatch=True)

    if hotspots is not None and _spot_vals:
        period_type = detail.get("period_type", "annual")
        keys = sorted(_spot_vals)
        # Tile the columns off the MIDPOINTS between neighbouring periods. Taking
        # cx +/- half and rounding independently collapsed 97 of 292 columns to
        # zero width on a dense series (103 quarters across 600px is under 6px
        # each), leaving dead strips the cursor fell through.
        centres = [px(k) / ss for k in keys]
        edges = [max(0.0, centres[0] - (centres[1] - centres[0]) / 2
                     if len(centres) > 1 else centres[0] - 4)]
        for a, b in zip(centres, centres[1:]):
            edges.append((a + b) / 2)
        edges.append(min(float(CHART_W),
                         centres[-1] + (centres[-1] - centres[-2]) / 2
                         if len(centres) > 1 else centres[-1] + 4))
        top = int(y0 // ss)
        for i, key in enumerate(keys):
            x1_px = int(round(edges[i]))
            x2_px = max(x1_px + 1, int(round(edges[i + 1])))   # never zero-width
            parts = [f"{k}: ${v:,.1f}M" for k, v in _spot_vals[key].items()]
            hotspots.append({
                "x1": x1_px,
                "y1": top,
                "x2": min(x2_px, CHART_W),
                "y2": CHART_H,
                "title": _period_label(key, period_type) + "  " + "  ".join(parts),
            })

    img = img.resize((CHART_W, CHART_H), Image.LANCZOS)
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def draw_mape_png(detail: Dict[str, Any]) -> Optional[bytes]:
    """Horizontal bars of MAPE per model (lower = more accurate); best highlighted."""
    from PIL import Image, ImageDraw
    rows = [r for r in (detail.get("models") or []) if r.get("mape") is not None]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: float(r["mape"]))
    ss = 2
    rowh = 34
    W, H = 900 * ss, (60 + rowh * len(rows) + 20) * ss
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_title, f_lab = _font(20 * ss), _font(14 * ss)
    d.text((28 * ss, 14 * ss), f"{detail['ticker']}  model accuracy (backtest MAPE, lower is better)",
           font=f_title, fill=_TEXT)
    x0 = 240 * ss
    x1 = W - 90 * ss
    maxm = max(float(r["mape"]) for r in rows) or 1.0
    y = 56 * ss
    for r in rows:
        m = float(r["mape"])
        best = int(r.get("is_best_model") or 0) == 1
        bw = (x1 - x0) * (m / maxm)
        d.text((28 * ss, y + 6 * ss), _label(r["model_key"]) + ("  (best)" if best else ""),
               font=f_lab, fill=(_ACCENT if best else _TEXT))
        d.rectangle([x0, y, x0 + max(bw, 2 * ss), y + 20 * ss],
                    fill=(_BAR_BEST if best else _BAR_OTHER))
        d.text((x0 + max(bw, 2 * ss) + 8 * ss, y + 4 * ss), f"{m:.1f}%",
               font=f_lab, fill=(_ACCENT if best else _MUTED))
        y += rowh * ss
    img = img.resize((W // ss, H // ss), Image.LANCZOS)
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def render_charts(detail: Dict[str, Any]) -> List[Tuple[str, bytes]]:
    """Return [(chart_id, png_bytes)] for this ticker (skips charts with no data).

    Trajectory hotspots are stashed on ``detail`` for the HTML builder, which runs
    after this and has no other way to learn where the points landed.
    """
    out: List[Tuple[str, bytes]] = []
    tk = detail["ticker"].replace(".", "_")
    spots: List[Dict[str, Any]] = []
    traj = draw_trajectory_png(detail, hotspots=spots)
    if traj:
        detail["_traj_hotspots"] = spots
        out.append((f"traj_{tk}", traj))
    mape = draw_mape_png(detail)
    if mape:
        out.append((f"mape_{tk}", mape))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────
_TD = 'style="padding:6px 10px;border:1px solid #e2e2e2;"'
_TH = 'style="padding:7px 10px;border:1px solid #e2e2e2;text-align:left;background:#f6f6f6;font-weight:600;"'
# Emphasis cell for the ensemble (final-output) values.
_TD_ENS = 'style="padding:6px 10px;border:1px solid #e2e2e2;background:#fdeef0;color:#C8102E;font-weight:700;"'


def _trajectory_with_hotspots(src: str, spots: List[Dict[str, Any]],
                              map_name: str = "traj") -> str:
    """The trajectory image with a hover tooltip over every period.

    An HTML image map, not positioned <span>s. Spans were the wrong tool: Gmail
    and Outlook both strip `position:absolute`, so the tooltips only ever worked
    when the raw file was opened in a browser — in a real inbox it was a flat
    picture. `<map>`/`<area>` is one of the few interactive constructs email
    clients do keep, and `title` on an <area> is the native tooltip.

    Two things have to hold for the coordinates to land: the image is emitted at
    its intrinsic size with width/height attributes (no CSS scaling), and the
    areas are in that same pixel grid. Clients that drop image maps show a plain
    chart, and every value is in the table directly below it either way.
    """
    img = (f'<img src="{src}" alt="forecast trajectory" '
           f'width="{CHART_W}" height="{CHART_H}" '
           f'usemap="#{map_name}" border="0" '
           f'style="display:block;border:1px solid #eee;">')
    if not spots:
        return f'<div style="margin:10px 0;">{img}</div>'
    areas = "".join(
        f'<area shape="rect" '
        f'coords="{sp["x1"]},{sp["y1"]},{sp["x2"]},{sp["y2"]}" '
        f'title="{_html_attr(sp["title"])}" alt="{_html_attr(sp["title"])}" '
        f'nohref="nohref">'
        for sp in spots
    )
    return (f'<div style="margin:10px 0;">{img}'
            f'<map name="{map_name}" id="{map_name}">{areas}</map></div>')


def _html_attr(value: str) -> str:
    """Escape for an HTML attribute — titles carry $ , and company text."""
    return (str(value).replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _forecast_table(detail: Dict[str, Any]) -> str:
    """Ensemble forecast (final output) as the headline, best individual model alongside."""
    best = dict(detail.get("series") or [])
    ens  = dict(detail.get("ensemble_series") or [])
    model_lab = _label(detail.get("best_model", ""))
    ptype = detail.get("period_type", "annual")
    if not ens:
        # Fallback: no ensemble stored — keep the original best-only layout.
        head = (f'<tr><th {_TH}>{_period_heading(ptype)}</th><th {_TH}>Forecast</th>'
                f'<th {_TH}>Model</th><th {_TH}>Periods Ahead</th></tr>')
        body = "".join(
            f'<tr><td {_TD}>{_period_label(fy, ptype)}</td><td {_TD}><strong>{_fmt_mm(v)}</strong></td>'
            f'<td {_TD}>{model_lab}</td><td {_TD}>{i}</td></tr>'
            for i, (fy, v) in enumerate(detail.get("series") or [], start=1)
        )
        return f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 14px;">{head}{body}</table>'

    years = sorted(set(best) | set(ens))
    head = (f'<tr><th {_TH}>{_period_heading(ptype)}</th><th {_TH}>Ensemble Forecast</th>'
            f'<th {_TH}>Best-Model Forecast</th><th {_TH}>Best Model</th>'
            f'<th {_TH}>Periods Ahead</th></tr>')
    body = ""
    for i, fy in enumerate(years, start=1):
        body += (f'<tr><td {_TD}>{_period_label(fy, ptype)}</td>'
                 f'<td {_TD_ENS}>{_fmt_mm(ens.get(fy))}</td>'
                 f'<td {_TD}>{_fmt_mm(best.get(fy))}</td>'
                 f'<td {_TD}>{model_lab}</td><td {_TD}>{i}</td></tr>')
    caption = ('<div style="font-size:11px;color:#888;margin:2px 0 4px;">'
               'Ensemble = final output (mean of the top-performing models).</div>')
    return (caption + f'<table style="border-collapse:collapse;width:100%;font-size:13px;'
            f'margin:2px 0 14px;">{head}{body}</table>')


def _growth_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous) * 100.0


def _yoy_cell(pct: Optional[float], top: str = "") -> str:
    base = f"padding:6px 10px;border:1px solid #e2e2e2;white-space:nowrap;{top}"
    if pct is None:
        return f'<td style="{base}color:#999;">—</td>'
    arrow = "▲" if pct >= 0 else "▼"
    color = "#1E8449" if pct >= 0 else "#C0392B"
    return f'<td style="{base}color:{color};font-weight:600;">{arrow} {abs(pct):.1f}%</td>'


def _revenue_table(detail: Dict[str, Any]) -> str:
    """Full reported history and the forecast in one continuous, reviewable table.

    Historical actual years first, then forecast years, with a YoY column computed on
    the effective series (actual while reported, ensemble once forecast) — which is what
    makes the trend validatable at a glance.
    """
    actuals = detail.get("actuals") or []
    best = dict(detail.get("series") or [])
    ens  = dict(detail.get("ensemble_series") or [])
    ptype = detail.get("period_type", "annual")
    if not actuals and not ens:
        return _forecast_table(detail)          # legacy fallback

    act = dict(actuals)
    fc_years = sorted(set(best) | set(ens))
    years = [y for y, _ in actuals] + [y for y in fc_years if y not in act]
    first_fc = fc_years[0] if fc_years else None

    head = (f'<tr><th {_TH}>{_period_heading(ptype)}</th><th {_TH}>Type</th>'
            f'<th {_TH}>Actual Revenue</th><th {_TH}>Ensemble Forecast</th>'
            f'<th {_TH}>Best-Model Forecast</th><th {_TH}>{_change_heading(ptype)}</th></tr>')

    body, prev = "", None
    for y in years:
        is_fc = y not in act
        current = ens.get(y) if is_fc else act.get(y)
        yoy = _growth_pct(current, prev)
        prev = current
        top = "border-top:2px solid #C8102E;" if (actuals and y == first_fc) else ""
        td = f'style="padding:6px 10px;border:1px solid #e2e2e2;{top}"'
        ens_td = ('style="padding:6px 10px;border:1px solid #e2e2e2;background:#fdeef0;'
                  f'color:#C8102E;font-weight:700;{top}"')
        if is_fc:
            badge = ('<span style="background:#fdeef0;color:#C8102E;padding:1px 7px;'
                     'border-radius:3px;font-size:11px;font-weight:600;">Forecast</span>')
            cells = (f'<td {td}>—</td><td {ens_td}>{_fmt_forecast_mm(ens.get(y))}</td>'
                     f'<td {td}>{_fmt_forecast_mm(best.get(y))}</td>')
            rowbg = ' style="background:#fffbfb;"'
        else:
            badge = ('<span style="background:#eef2f7;color:#25375A;padding:1px 7px;'
                     'border-radius:3px;font-size:11px;font-weight:600;">Actual</span>')
            cells = (f'<td {td}><strong>{_fmt_mm(act.get(y))}</strong></td>'
                     f'<td {td}>—</td><td {td}>—</td>')
            rowbg = ""
        body += (f'<tr{rowbg}><td {td}><strong>{_period_label(y, ptype)}</strong></td><td {td}>{badge}</td>'
                 f'{cells}{_yoy_cell(yoy, top)}</tr>')

    n_hist = len(actuals)
    _unit = "quarter" if ptype == "quarterly" else "year"
    caption = ('<div style="font-size:11px;color:#888;margin:2px 0 5px;">'
               f'{n_hist} historical actual {_unit}{"" if n_hist == 1 else "s"} from reported '
               'financials, then the forecast · Ensemble = final output (mean of the '
               f'top-performing models) · {_change_heading(ptype)} compares each row to the one above.</div>')
    return (caption + '<table style="border-collapse:collapse;width:100%;font-size:13px;'
            f'margin:2px 0 16px;">{head}{body}</table>')


def _model_table(detail: Dict[str, Any]) -> str:
    head = (f'<tr><th {_TH}>Model</th><th {_TH}>Backtest MAPE</th>'
            f'<th {_TH}>Next-period value</th><th {_TH}>Selected</th></tr>')
    body = ""
    for r in detail.get("models") or []:
        best = int(r.get("is_best_model") or 0) == 1
        mape = r.get("mape")
        mape_s = "—" if mape is None else f"{float(mape):.1f}%"
        star = "✅ best" if best else ("ensemble" if int(r.get("is_ensemble") or 0) == 1 else "")
        row_style = ' style="background:#fdeef0;"' if best else ""
        body += (f'<tr{row_style}><td {_TD}>{_label(r["model_key"])}</td>'
                 f'<td {_TD}>{mape_s}</td><td {_TD}>{_fmt_forecast_mm(r.get("value_millions"))}</td>'
                 f'<td {_TD}>{star}</td></tr>')
    return f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 14px;">{head}{body}</table>'


def _fmt_date(v) -> str:
    try:
        return v.strftime("%b %d, %Y")
    except Exception:
        return str(v) if v else "—"


def build_report_html(
    *,
    period_type: str,
    triggered_by: str,
    now_str: str,
    details: List[Dict[str, Any]],
    chart_src: Dict[str, str],
) -> str:
    """Assemble the full HTML. ``chart_src`` maps chart_id → src (cid: or data:)."""
    cadence = "Quarterly" if period_type == "quarterly" else "Annual"
    # Summary table
    srows = ""
    for det in details:
        _ens = det.get("ensemble_series") or []
        _ens_next = _fmt_mm(_ens[0][1]) if _ens else "—"
        _acts = det.get("actuals") or []
        _last_actual = f"{_fmt_mm(_acts[-1][1])} · " if _acts else ""
        srows += (
            f'<tr><td {_TD}><strong>{det["ticker"]}</strong></td>'
            f'<td {_TD}>{det.get("company_name","")}</td>'
            f'<td {_TD}>{det.get("exchange","")}</td>'
            f'<td {_TD_ENS}>{_ens_next}</td>'
            f'<td {_TD}>{_label(det.get("best_model",""))}</td>'
            f'<td {_TD}>{"—" if det.get("best_mape") is None else f"{float(det["best_mape"]):.1f}%"}</td>'
            f'<td {_TD}>{_last_actual}{_fmt_date(det.get("last_actual_date"))}</td></tr>'
        )
    summary = (
        f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 20px;">'
        f'<tr><th {_TH}>Ticker</th><th {_TH}>Company</th><th {_TH}>Exchange</th>'
        f'<th {_TH}>Ensemble (next period)</th>'
        f'<th {_TH}>Best Model</th><th {_TH}>MAPE</th><th {_TH}>Last Actual</th></tr>{srows}</table>'
    )

    sections = ""
    for det in details:
        _ens = det.get("ensemble_series") or []
        _pt = det.get("period_type", "annual")
        _ens_meta = (f' &nbsp;·&nbsp; Ensemble ({_period_label(_ens[0][0], _pt)}): '
                     f'<strong style="color:#C8102E;">{_fmt_mm(_ens[0][1])}</strong>') if _ens else ""
        tk = det["ticker"].replace(".", "_")
        traj = chart_src.get(f"traj_{tk}")
        mape = chart_src.get(f"mape_{tk}")
        charts = ""
        if traj:
            charts += _trajectory_with_hotspots(
                traj, det.get("_traj_hotspots") or [], map_name=f"traj_{tk}")
        if mape:
            charts += f'<img src="{mape}" alt="model accuracy" style="width:100%;max-width:680px;display:block;margin:10px 0;border:1px solid #eee;">'
        sections += (
            f'<div style="margin:26px 0 8px;padding-top:14px;border-top:2px solid #C8102E;">'
            f'<div style="font-size:16px;font-weight:700;color:#222;">{det["ticker"]} '
            f'<span style="font-weight:400;color:#666;">· {det.get("company_name","")} '
            f'({det.get("exchange","")})</span></div>'
            f'<div style="font-size:12px;color:#888;margin:2px 0 10px;">Metric: {det.get("metric","")} · '
            f'Best model: <strong>{_label(det.get("best_model",""))}</strong> · '
            f'Backtest MAPE: {"—" if det.get("best_mape") is None else f"{float(det["best_mape"]):.1f}%"}'
            f'{_ens_meta}</div>'
            f'<div style="font-size:13px;font-weight:600;color:#444;margin:8px 0 2px;">'
            f'Revenue — historical actuals &amp; forecast</div>{_revenue_table(det)}'
            f'<div style="font-size:13px;font-weight:600;color:#444;margin:8px 0 2px;">Model backtest comparison</div>{_model_table(det)}'
            f'{charts}</div>'
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#222;margin:0;padding:22px;background:#ffffff;">
  <div style="max-width:720px;margin:0 auto;">
    <div style="font-size:20px;font-weight:700;color:#C8102E;">Forecasting Model Refresh — {cadence}</div>
    <div style="font-size:12px;color:#666;margin:4px 0 16px;">
      Triggered by: {triggered_by} &nbsp;·&nbsp; {now_str} &nbsp;·&nbsp;
      {len(details)} {"company" if len(details)==1 else "companies"} updated
    </div>
    <div style="font-size:13px;font-weight:600;color:#444;margin:8px 0 2px;">Run summary</div>
    {summary}
    {sections}
    <p style="color:#999;font-size:11px;margin-top:24px;border-top:1px solid #eee;padding-top:10px;">
      Coresight Research · Revenue Forecasting · Automated notification. Forecasts are model
      estimates for internal use.</p>
  </div>
</body></html>"""


def build_details(results: List[Dict[str, Any]], period_type: str) -> List[Dict[str, Any]]:
    """For every 'updated' result, fetch its stored detail (skips ones with no rows)."""
    details = []
    for r in results:
        if r.get("status") != "updated":
            continue
        det = fetch_ticker_detail(r.get("ticker", ""), period_type)
        if det:
            # carry through name/exchange from the result if detail lacked them
            det["company_name"] = det.get("company_name") or r.get("company_name") or ""
            det["exchange"] = det.get("exchange") or r.get("exchange") or ""
            details.append(det)
    return details


def png_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
