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
import os
from typing import Any, Dict, List, Optional, Tuple

from core.database import db_manager
from utils.server_logger import log_structured_error

# Brand-ish palette (Coresight red accent + neutral grays).
_ACCENT = (200, 16, 46)        # #C8102E — best model / primary line
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
def fetch_ticker_detail(ticker: str, period_type: str = "annual") -> Optional[Dict[str, Any]]:
    """Pull the stored forecast detail for one ticker. Returns None if nothing stored."""
    table = _QUARTERLY_TABLE if period_type == "quarterly" else _ANNUAL_TABLE
    try:
        best = db_manager.execute_query_readonly(
            f"SELECT fiscal_year, model_key, metric, value_millions, mape, periods_ahead, "
            f"       last_actual_date, company_name, exchange, computed_at "
            f"FROM {table} WHERE ticker = :t AND is_best_model = 1 ORDER BY fiscal_year",
            {"t": ticker},
        )
        if not best:
            return None
        metric = best[0].get("metric")
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
            f"SELECT fiscal_year, model_key, value_millions FROM {table} "
            f"WHERE ticker = :t AND metric = :m "
            f"  AND model_key IN ('scenario_baseline','scenario_optimistic','scenario_pessimistic') "
            f"ORDER BY fiscal_year",
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
            "series": [(int(r["fiscal_year"]), float(r["value_millions"]))
                       for r in best if r.get("value_millions") is not None],
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


def _scenario_bands(scenarios: List[Dict[str, Any]]) -> Dict[str, List[Tuple[int, float]]]:
    out: Dict[str, List[Tuple[int, float]]] = {"scenario_baseline": [], "scenario_optimistic": [], "scenario_pessimistic": []}
    for r in scenarios:
        k = r.get("model_key")
        if k in out and r.get("value_millions") is not None:
            out[k].append((int(r["fiscal_year"]), float(r["value_millions"])))
    for k in out:
        out[k].sort()
    return out


def draw_trajectory_png(detail: Dict[str, Any]) -> Optional[bytes]:
    """Forecast trajectory line with a scenario (optimistic↔pessimistic) band."""
    from PIL import Image, ImageDraw
    series = detail.get("series") or []
    if len(series) < 2:
        return None
    ss = 2  # supersample for crisp downscale
    W, H = 900 * ss, 340 * ss
    ml, mr, mt, mb = 92 * ss, 60 * ss, 44 * ss, 52 * ss
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img, "RGBA")
    f_title, f_lab, f_val = _font(20 * ss), _font(15 * ss), _font(14 * ss)

    bands = _scenario_bands(detail.get("scenarios") or [])
    years = [y for y, _ in series]
    opt = dict(bands.get("scenario_optimistic") or [])
    pes = dict(bands.get("scenario_pessimistic") or [])
    base = dict(bands.get("scenario_baseline") or [])

    all_vals = [v for _, v in series] + list(opt.values()) + list(pes.values()) + list(base.values())
    vmin, vmax = min(all_vals), max(all_vals)
    pad = (vmax - vmin) * 0.15 or vmax * 0.1 or 1.0
    vmin, vmax = vmin - pad, vmax + pad
    x0, x1, y0, y1 = ml, W - mr, mt, H - mb

    def px(i): return x0 + (x1 - x0) * (i / (len(years) - 1))
    def py(v): return y1 - (y1 - y0) * ((v - vmin) / (vmax - vmin))

    # title
    d.text((ml, 12 * ss), f"{detail['ticker']} — revenue forecast ($M)", font=f_title, fill=_TEXT)

    # y gridlines + labels
    for g in range(5):
        gv = vmin + (vmax - vmin) * g / 4
        gy = py(gv)
        d.line([(x0, gy), (x1, gy)], fill=_GRID, width=1 * ss)
        d.text((10 * ss, gy - 8 * ss), f"{gv:,.0f}", font=f_val, fill=_MUTED)
    # x labels (centered under each point so the last one never clips)
    for i, y in enumerate(years):
        lab = f"FY{y}"
        w = d.textlength(lab, font=f_lab)
        d.text((px(i) - w / 2, y1 + 10 * ss), lab, font=f_lab, fill=_TEXT)

    # scenario band (optimistic top ↔ pessimistic bottom)
    if opt and pes and all(y in opt and y in pes for y in years):
        top = [(px(i), py(opt[y])) for i, y in enumerate(years)]
        bot = [(px(i), py(pes[y])) for i, y in enumerate(years)]
        d.polygon(top + bot[::-1], fill=_BAND)
        # baseline dashed-ish line
        if base and all(y in base for y in years):
            bpts = [(px(i), py(base[y])) for i, y in enumerate(years)]
            for a, b in zip(bpts, bpts[1:]):
                d.line([a, b], fill=_BASELINE, width=2 * ss)

    # best-model line + markers + labels
    pts = [(px(i), py(v)) for i, (_, v) in enumerate(series)]
    for a, b in zip(pts, pts[1:]):
        d.line([a, b], fill=_ACCENT, width=3 * ss)
    for (cx, cy), (_, v) in zip(pts, series):
        r = 5 * ss
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_ACCENT)
        d.text((cx - 22 * ss, cy - 26 * ss), f"{v:,.0f}", font=f_val, fill=_ACCENT)

    # legend
    lx, ly = x1 - 250 * ss, 16 * ss
    d.line([(lx, ly + 8 * ss), (lx + 26 * ss, ly + 8 * ss)], fill=_ACCENT, width=3 * ss)
    d.text((lx + 32 * ss, ly), f"Best: {_label(detail['best_model'])}", font=f_val, fill=_TEXT)
    d.rectangle([lx, ly + 22 * ss, lx + 26 * ss, ly + 34 * ss], fill=_BAND)
    d.text((lx + 32 * ss, ly + 22 * ss), "Scenario range", font=f_val, fill=_MUTED)

    img = img.resize((W // ss, H // ss), Image.LANCZOS)
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
    d.text((28 * ss, 14 * ss), f"{detail['ticker']} — model accuracy (backtest MAPE, lower is better)",
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
    """Return [(chart_id, png_bytes)] for this ticker (skips charts with no data)."""
    out: List[Tuple[str, bytes]] = []
    tk = detail["ticker"].replace(".", "_")
    traj = draw_trajectory_png(detail)
    if traj:
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


def _forecast_table(detail: Dict[str, Any]) -> str:
    head = (f'<tr><th {_TH}>Fiscal Year</th><th {_TH}>Forecast</th>'
            f'<th {_TH}>Model</th><th {_TH}>Periods Ahead</th></tr>')
    body = ""
    for i, (fy, v) in enumerate(detail.get("series") or [], start=1):
        body += (f'<tr><td {_TD}>FY{fy}</td><td {_TD}><strong>{_fmt_mm(v)}</strong></td>'
                 f'<td {_TD}>{_label(detail["best_model"])}</td><td {_TD}>{i}</td></tr>')
    return f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 14px;">{head}{body}</table>'


def _model_table(detail: Dict[str, Any]) -> str:
    head = (f'<tr><th {_TH}>Model</th><th {_TH}>Backtest MAPE</th>'
            f'<th {_TH}>Next-yr value</th><th {_TH}>Selected</th></tr>')
    body = ""
    for r in detail.get("models") or []:
        best = int(r.get("is_best_model") or 0) == 1
        mape = r.get("mape")
        mape_s = "—" if mape is None else f"{float(mape):.1f}%"
        star = "✅ best" if best else ("ensemble" if int(r.get("is_ensemble") or 0) == 1 else "")
        row_style = ' style="background:#fdeef0;"' if best else ""
        body += (f'<tr{row_style}><td {_TD}>{_label(r["model_key"])}</td>'
                 f'<td {_TD}>{mape_s}</td><td {_TD}>{_fmt_mm(r.get("value_millions"))}</td>'
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
        srows += (
            f'<tr><td {_TD}><strong>{det["ticker"]}</strong></td>'
            f'<td {_TD}>{det.get("company_name","")}</td>'
            f'<td {_TD}>{det.get("exchange","")}</td>'
            f'<td {_TD}>{_label(det.get("best_model",""))}</td>'
            f'<td {_TD}>{"—" if det.get("best_mape") is None else f"{float(det["best_mape"]):.1f}%"}</td>'
            f'<td {_TD}>{_fmt_date(det.get("last_actual_date"))}</td></tr>'
        )
    summary = (
        f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 20px;">'
        f'<tr><th {_TH}>Ticker</th><th {_TH}>Company</th><th {_TH}>Exchange</th>'
        f'<th {_TH}>Best Model</th><th {_TH}>MAPE</th><th {_TH}>Last Actual</th></tr>{srows}</table>'
    )

    sections = ""
    for det in details:
        tk = det["ticker"].replace(".", "_")
        traj = chart_src.get(f"traj_{tk}")
        mape = chart_src.get(f"mape_{tk}")
        charts = ""
        if traj:
            charts += f'<img src="{traj}" alt="forecast trajectory" style="width:100%;max-width:680px;display:block;margin:10px 0;border:1px solid #eee;">'
        if mape:
            charts += f'<img src="{mape}" alt="model accuracy" style="width:100%;max-width:680px;display:block;margin:10px 0;border:1px solid #eee;">'
        sections += (
            f'<div style="margin:26px 0 8px;padding-top:14px;border-top:2px solid #C8102E;">'
            f'<div style="font-size:16px;font-weight:700;color:#222;">{det["ticker"]} '
            f'<span style="font-weight:400;color:#666;">· {det.get("company_name","")} '
            f'({det.get("exchange","")})</span></div>'
            f'<div style="font-size:12px;color:#888;margin:2px 0 10px;">Metric: {det.get("metric","")} · '
            f'Best model: <strong>{_label(det.get("best_model",""))}</strong> · '
            f'Backtest MAPE: {"—" if det.get("best_mape") is None else f"{float(det["best_mape"]):.1f}%"}</div>'
            f'<div style="font-size:13px;font-weight:600;color:#444;margin:8px 0 2px;">Forecast</div>{_forecast_table(det)}'
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
