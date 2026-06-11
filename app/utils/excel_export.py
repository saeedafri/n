"""
Excel Export Utility
====================
Generates formatted Excel files for all Market Data tabs.
Uses openpyxl for fast, branded Excel output.
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import List, Tuple, Dict, Optional


from utils.constants import (
    CURRENCY_NAMES,
    CURRENCY_SYMBOLS,
    get_currency_symbol as _get_currency_symbol,
    format_currency_full as _format_currency_display,
)

from utils.server_logger import log_structured_error

# openpyxl styles imported at module level so the helper lambdas below work.
# If openpyxl is missing the ImportError surfaces when the export functions are
# called (not at module load time) because Workbook is still imported lazily.
try:
    from openpyxl.styles import Side, Border
except ImportError:
    Side = None   # type: ignore[assignment,misc]
    Border = None  # type: ignore[assignment,misc]

# ── Brand palette (openpyxl uses ARGB = "FF" + RRGGBB) ──────────────────────
_RED    = "FFD62E2F"
_DARK   = "FF2D2A29"
_GRAY   = "FF6B6B6B"
_LGRAY  = "FFE0E0E0"
_WHITE  = "FFFFFFFF"
_ALT    = "FFF9F9F9"
_HDR    = "FFF0F0F0"
_TOTAL  = "FFEEEEEE"

_THIN  = lambda: Side(style="thin",   color=_LGRAY[2:])
_MED   = lambda c="2D2A29": Side(style="medium", color=c)
_RED_S = lambda: Side(style="medium", color=_RED[2:])


def _cl(text) -> str:
    """Clean text: remove null bytes, strip HTML tags, trim whitespace."""
    try:
        if text is None:
            return ""
        s = str(text).replace("\x00", "").replace("\r", " ")
        s = re.sub(r"<[^>]+>", "", s).strip()
        return s
    except Exception as exc:
        log_structured_error(exc, page="excel_export", component="excel_export", operation="_cl")
        return ""


def _border(*sides) -> Border:
    """Build Border from keyword pairs: top=True/False, bottom, left, right."""
    try:
        return Border(
            top    = _THIN() if sides[0] else None,
            bottom = _THIN() if sides[1] else None,
            left   = _THIN() if sides[2] else None,
            right  = _THIN() if sides[3] else None,
        )
    except Exception as exc:
        log_structured_error(exc, page="excel_export", component="excel_export", operation="_border")
        return None


# ── Company Profile ───────────────────────────────────────────────────────────

def export_company_profile_excel(
    company_name: str,
    ticker: str,
    rows: List[Tuple[str, str]],    # [(label, plain_value), ...]
    description: str = "",
) -> bytes:
    """Formatted Excel for Company Profile tab."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter  # noqa: F401
        wb = Workbook()
        ws = wb.active
        ws.title = "Company Profile"
        ws.sheet_view.showGridLines = False

        # Column widths
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 36
        ws.column_dimensions["C"].width = 30
        ws.column_dimensions["D"].width = 36

        # ── Title ──────────────────────────────────────────────────────────────
        ws.merge_cells("A1:D1")
        c = ws["A1"]
        c.value = f"{_cl(company_name)}  ({_cl(ticker)})"
        c.font = Font(name="Calibri", size=16, bold=True, color=_DARK[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[1].height = 32

        # ── Subtitle ───────────────────────────────────────────────────────────
        ws.merge_cells("A2:D2")
        c = ws["A2"]
        c.value = "Company Profile"
        c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[2].height = 18
        for col in range(1, 5):
            ws.cell(2, col).border = Border(bottom=_RED_S())

        # ── Spacer ─────────────────────────────────────────────────────────────
        ws.row_dimensions[3].height = 6

        # ── Data rows (2 pairs per row: label|value|label|value) ───────────────
        lbl_font   = Font(name="Calibri", size=10, bold=True, color=_DARK[2:])
        val_font   = Font(name="Calibri", size=10, color=_DARK[2:])
        align      = Alignment(horizontal="left", vertical="center", indent=1)
        alt_fill   = PatternFill("solid", fgColor=_ALT[2:])
        sep_v      = Side(style="thin", color=_LGRAY[2:])
        bot        = Side(style="thin", color=_LGRAY[2:])

        start_row = 4
        pairs = [
            (rows[i], rows[i + 1] if i + 1 < len(rows) else ("", ""))
            for i in range(0, len(rows), 2)
        ]

        for i, ((lbl1, val1), (lbl2, val2)) in enumerate(pairs):
            r = start_row + i
            fill = alt_fill if i % 2 == 0 else None
            for col, val, fnt in [(1, lbl1, lbl_font), (2, val1, val_font),
                                   (3, lbl2, lbl_font), (4, val2, val_font)]:
                c = ws.cell(r, col, _cl(val))
                c.font = fnt
                c.alignment = align
                if fill:
                    c.fill = fill
            # Internal vertical separator + row bottom border
            ws.cell(r, 2).border = Border(right=sep_v, bottom=bot)
            ws.cell(r, 3).border = Border(left=sep_v,  bottom=bot)
            ws.cell(r, 1).border = Border(bottom=bot)
            ws.cell(r, 4).border = Border(bottom=bot)
            ws.row_dimensions[r].height = 22

        # ── Business description ───────────────────────────────────────────────
        if description:
            desc_row = start_row + len(pairs) + 2
            ws.row_dimensions[desc_row - 1].height = 10   # spacer
            ws.merge_cells(
                start_row=desc_row, start_column=1,
                end_row=desc_row,   end_column=4
            )
            c = ws.cell(desc_row, 1)
            c.value = "Business Description"
            c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            c.border = Border(bottom=_RED_S())
            ws.row_dimensions[desc_row].height = 20

            desc_text_row = desc_row + 1
            ws.merge_cells(
                start_row=desc_text_row, start_column=1,
                end_row=desc_text_row,   end_column=4
            )
            c = ws.cell(desc_text_row, 1)
            c.value = _cl(description)
            c.font = Font(name="Calibri", size=9, color=_DARK[2:])
            c.alignment = Alignment(
                horizontal="left", vertical="top",
                wrap_text=True, indent=1
            )
            # Rough height: ~15px per 120 chars
            desc_len = len(_cl(description))
            ws.row_dimensions[desc_text_row].height = max(60, min(300, (desc_len // 120 + 1) * 15))

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as exc:
        log_structured_error(exc, page="excel_export", component="excel_export", operation="export_company_profile_excel")
        return b""


# ── Stock Quote ───────────────────────────────────────────────────────────────

def export_stock_quote_excel(
    company_name: str,
    ticker: str,
    snapshot_rows: List[Tuple[str, str, str, str]],  # [(left_label, left_val, right_label, right_val), ...]
    history: List[Dict],                              # [{"date": "2025-01-30", "close": 204.79, "volume": 12345678}, ...]
    currency: str = "USD",
) -> bytes:
    """
    Formatted Excel for Stock Quote and Chart section.
    Sheet 1 — Stock Quote snapshot (current values, 2-column layout)
    Sheet 2 — Last 12 months daily price history (Date, Close, Volume)
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        wb = Workbook()

        # ── Sheet 1: Snapshot ─────────────────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "Stock Quote"
        ws1.sheet_view.showGridLines = False
        ws1.column_dimensions["A"].width = 28
        ws1.column_dimensions["B"].width = 18
        ws1.column_dimensions["C"].width = 28
        ws1.column_dimensions["D"].width = 18

        # Title
        ws1.merge_cells("A1:D1")
        c = ws1["A1"]
        c.value = f"{_cl(company_name)}  ({_cl(ticker)})"
        c.font = Font(name="Calibri", size=16, bold=True, color=_DARK[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws1.row_dimensions[1].height = 32

        # Subtitle
        ws1.merge_cells("A2:D2")
        c = ws1["A2"]
        c.value = f"Stock Quote and Chart (Currency: {_format_currency_display(currency)})"
        c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws1.row_dimensions[2].height = 18
        for col in range(1, 5):
            ws1.cell(2, col).border = Border(bottom=_RED_S())

        ws1.row_dimensions[3].height = 6  # spacer

        lbl_font = Font(name="Calibri", size=10, bold=True, color=_DARK[2:])
        val_font = Font(name="Calibri", size=10, color=_DARK[2:])
        align    = Alignment(horizontal="left", vertical="center", indent=1)
        val_align = Alignment(horizontal="right", vertical="center", indent=1)
        alt_fill = PatternFill("solid", fgColor=_ALT[2:])
        sep_v    = Side(style="thin", color=_LGRAY[2:])
        bot      = Side(style="thin", color=_LGRAY[2:])

        for i, (ll, lv, rl, rv) in enumerate(snapshot_rows):
            r = 4 + i
            fill = alt_fill if i % 2 == 0 else None
            for col, val, fnt, al in [
                (1, ll, lbl_font, align), (2, lv, val_font, val_align),
                (3, rl, lbl_font, align), (4, rv, val_font, val_align),
            ]:
                c = ws1.cell(r, col, _cl(val))
                c.font = fnt
                c.alignment = al
                if fill:
                    c.fill = fill
            ws1.cell(r, 2).border = Border(right=sep_v, bottom=bot)
            ws1.cell(r, 3).border = Border(left=sep_v, bottom=bot)
            ws1.cell(r, 1).border = Border(bottom=bot)
            ws1.cell(r, 4).border = Border(bottom=bot)
            ws1.row_dimensions[r].height = 22

        # ── Sheet 2: Price History ────────────────────────────────────────────────
        # Collapse to one row per month (latest trading day of each month)
        from collections import defaultdict as _dd
        _by_month: dict = _dd(list)
        for _bar in history:
            _by_month[_bar["date"][:7]].append(_bar)
        monthly_history = sorted(
            [max(bars, key=lambda b: b["date"]) for bars in _by_month.values()],
            key=lambda x: x["date"], reverse=True
        )

        def _fmt_vol(v) -> str:
            try:
                if v is None: return "-"
                v = int(v)
                if v >= 1_000_000_000: return f"{v/1e9:.2f}B"
                if v >= 1_000_000:     return f"{v/1e6:.2f}M"
                if v >= 1_000:         return f"{v/1e3:.1f}K"
                return str(v)
            except Exception as exc:
                log_structured_error(exc, page="excel_export", component="excel_export", operation="_fmt_vol")
                return "-"

        def _fmt_mktcap(val) -> str:
            try:
                if val is None: return "-"
                if val >= 1e12: return f"${val/1e12:.3f}T"
                if val >= 1e9: return f"${val/1e9:.3f}B"
                if val >= 1e6: return f"${val/1e6:.2f}M"
                if val >= 1e3: return f"${val/1e3:.1f}K"
                return f"${val:.2f}"
            except Exception as exc:
                log_structured_error(exc, page="excel_export", component="excel_export", operation="_fmt_mktcap")
                return "-"

        ws2 = wb.create_sheet("Price History (12M)")
        ws2.sheet_view.showGridLines = False
        ws2.column_dimensions["A"].width = 16
        ws2.column_dimensions["B"].width = 14
        ws2.column_dimensions["C"].width = 14
        ws2.column_dimensions["D"].width = 20

        # Title
        ws2.merge_cells("A1:D1")
        c = ws2["A1"]
        c.value = f"{_cl(company_name)}  ({_cl(ticker)})  — Last 12 Months (Month-End)"
        c.font = Font(name="Calibri", size=14, bold=True, color=_DARK[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws2.row_dimensions[1].height = 28

        ws2.merge_cells("A2:D2")
        c = ws2["A2"]
        c.value = f"Currency: {_format_currency_display(currency)}"
        c.font = Font(name="Calibri", size=9, color=_RED[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws2.row_dimensions[2].height = 16
        for col in range(1, 5):
            ws2.cell(2, col).border = Border(bottom=_RED_S())

        ws2.row_dimensions[3].height = 6  # spacer

        # Header row
        hdr_fill = PatternFill("solid", fgColor=_HDR[2:])
        hdr_font = Font(name="Calibri", size=10, bold=True, color=_DARK[2:])
        for col, label in [(1, "Date"), (2, "Close Price"), (3, "Volume"), (4, "Market Cap (Close × Vol)")]:
            c = ws2.cell(4, col, label)
            c.font = hdr_font
            c.fill = hdr_fill
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = Border(bottom=Side(style="medium", color=_DARK[2:]))
        ws2.row_dimensions[4].height = 20

        # Data rows — one per month, newest first
        date_font = Font(name="Calibri", size=9, color=_DARK[2:])
        num_font  = Font(name="Calibri", size=9, color=_DARK[2:])
        for i, bar in enumerate(monthly_history):
            r = 5 + i
            fill = alt_fill if i % 2 == 0 else None

            # Date
            c = ws2.cell(r, 1, _cl(bar.get("date", "")))
            c.font = date_font
            c.alignment = Alignment(horizontal="center", vertical="center")

            # Close price  →  $XX.XX
            close_val = bar.get("close")
            c = ws2.cell(r, 2)
            if close_val is not None:
                c.value = round(float(close_val), 2)
                c.number_format = '"$"#,##0.00'
            else:
                c.value = "-"
            c.font = num_font
            c.alignment = Alignment(horizontal="right", vertical="center")

            # Volume  →  X.XM / X.XK
            vol_val = bar.get("volume")
            c = ws2.cell(r, 3, _fmt_vol(vol_val))
            c.font = num_font
            c.alignment = Alignment(horizontal="right", vertical="center")

            # Market Cap = Close × Volume  →  $X.XB / $X.XM
            mktcap = float(close_val) * int(vol_val) if close_val is not None and vol_val is not None else None
            c = ws2.cell(r, 4, _fmt_mktcap(mktcap))
            c.font = num_font
            c.alignment = Alignment(horizontal="right", vertical="center")

            if fill:
                for col in range(1, 5):
                    ws2.cell(r, col).fill = fill

            ws2.row_dimensions[r].height = 16

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as exc:
        log_structured_error(exc, page="excel_export", component="excel_export", operation="export_stock_quote_excel")
        return b""


# ── Combined Company Full Export ─────────────────────────────────────────────

def export_company_full_excel(
    company_name: str,
    ticker: str,
    profile_rows: List[Tuple[str, str]],
    description: str = "",
    snapshot_rows: Optional[List[Tuple]] = None,  # Supports (label, value) or (ll, lv, rl, rv)
    history: Optional[List[Dict]] = None,
    currency: str = "USD",
    shares_outstanding: Optional[float] = None,   # raw share count for Market Cap calc
    chart_data: Optional[List[Dict]] = None,      # Aggregated data from get_market_cap_chart_data
) -> bytes:
    """
    Single Excel with 2 sheets:
      Sheet 1 — Company Profile
      Sheet 2 — Market Cap (Stock Quote snapshot + chart)
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        wb = Workbook()
        snapshot_rows = snapshot_rows or []
        history       = history or []

        lbl_font  = Font(name="Calibri", size=10, bold=True, color=_DARK[2:])
        val_font  = Font(name="Calibri", size=10, color=_DARK[2:])
        align     = Alignment(horizontal="left",  vertical="center", indent=1)
        val_align = Alignment(horizontal="right", vertical="center", indent=1)
        alt_fill  = PatternFill("solid", fgColor=_ALT[2:])
        sep_v     = Side(style="thin",   color=_LGRAY[2:])
        bot       = Side(style="thin",   color=_LGRAY[2:])

        # ── Sheet 1: Company Profile ──────────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "Company Profile"
        ws1.sheet_view.showGridLines = False
        ws1.column_dimensions["A"].width = 30
        ws1.column_dimensions["B"].width = 36
        ws1.column_dimensions["C"].width = 30
        ws1.column_dimensions["D"].width = 36

        ws1.merge_cells("A1:D1")
        c = ws1["A1"]
        c.value = f"{_cl(company_name)}  ({_cl(ticker)})"
        c.font = Font(name="Calibri", size=16, bold=True, color=_DARK[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws1.row_dimensions[1].height = 32

        ws1.merge_cells("A2:D2")
        c = ws1["A2"]
        c.value = "Company Profile"
        c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws1.row_dimensions[2].height = 18
        for col in range(1, 5):
            ws1.cell(2, col).border = Border(bottom=_RED_S())
        ws1.row_dimensions[3].height = 6

        pairs = [(profile_rows[i], profile_rows[i+1] if i+1 < len(profile_rows) else ("", ""))
                 for i in range(0, len(profile_rows), 2)]
        for i, ((lbl1, val1), (lbl2, val2)) in enumerate(pairs):
            r = 4 + i
            fill = alt_fill if i % 2 == 0 else None
            for col, val, fnt in [(1, lbl1, lbl_font), (2, val1, val_font),
                                   (3, lbl2, lbl_font), (4, val2, val_font)]:
                c = ws1.cell(r, col, _cl(val))
                c.font = fnt
                c.alignment = align
                if fill:
                    c.fill = fill
            ws1.cell(r, 2).border = Border(right=sep_v, bottom=bot)
            ws1.cell(r, 3).border = Border(left=sep_v,  bottom=bot)
            ws1.cell(r, 1).border = Border(bottom=bot)
            ws1.cell(r, 4).border = Border(bottom=bot)
            ws1.row_dimensions[r].height = 22

        if description:
            desc_row = 4 + len(pairs) + 2
            ws1.row_dimensions[desc_row - 1].height = 10
            ws1.merge_cells(start_row=desc_row, start_column=1, end_row=desc_row, end_column=4)
            c = ws1.cell(desc_row, 1, "Business Description")
            c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            c.border = Border(bottom=_RED_S())
            ws1.row_dimensions[desc_row].height = 20
            dtr = desc_row + 1
            ws1.merge_cells(start_row=dtr, start_column=1, end_row=dtr, end_column=4)
            c = ws1.cell(dtr, 1, _cl(description))
            c.font = Font(name="Calibri", size=9, color=_DARK[2:])
            c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True, indent=1)
            ws1.row_dimensions[dtr].height = max(60, min(300, (len(_cl(description)) // 120 + 1) * 15))

        # ── Sheet 2: Stock Quote ──────────────────────────────────────────────────
        ws2 = wb.create_sheet("Stock Quote")
        ws2.sheet_view.showGridLines = False
        ws2.column_dimensions["A"].width = 28
        ws2.column_dimensions["B"].width = 18
        ws2.column_dimensions["C"].width = 28
        ws2.column_dimensions["D"].width = 18

        ws2.merge_cells("A1:D1")
        c = ws2["A1"]
        c.value = f"{_cl(company_name)}  ({_cl(ticker)})"
        c.font = Font(name="Calibri", size=16, bold=True, color=_DARK[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws2.row_dimensions[1].height = 32

        ws2.merge_cells("A2:D2")
        c = ws2["A2"]
        c.value = f"Stock Quote  (Currency: {_format_currency_display(currency)})"
        c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws2.row_dimensions[2].height = 18
        for col in range(1, 5):
            ws2.cell(2, col).border = Border(bottom=_RED_S())
        ws2.row_dimensions[3].height = 6

        # Handle both old 4-tuple format and new 2-tuple format
        for i, row_data in enumerate(snapshot_rows):
            r = 4 + i
            fill = alt_fill if i % 2 == 0 else None

            if len(row_data) == 4:
                # Old format: (left_label, left_val, right_label, right_val)
                ll, lv, rl, rv = row_data
                for col, val, fnt, al in [
                    (1, ll, lbl_font, align), (2, lv, val_font, val_align),
                    (3, rl, lbl_font, align), (4, rv, val_font, val_align),
                ]:
                    c = ws2.cell(r, col, _cl(val))
                    c.font = fnt
                    c.alignment = al
                    if fill:
                        c.fill = fill
                ws2.cell(r, 2).border = Border(right=sep_v, bottom=bot)
                ws2.cell(r, 3).border = Border(left=sep_v,  bottom=bot)
                ws2.cell(r, 1).border = Border(bottom=bot)
                ws2.cell(r, 4).border = Border(bottom=bot)
            else:
                # New format: (label, value) - single column layout
                lbl, val = row_data
                # Column A: Label, Column B: Value (spans C and D visually via width)
                c1 = ws2.cell(r, 1, _cl(lbl))
                c1.font = lbl_font
                c1.alignment = align

                c2 = ws2.cell(r, 2, _cl(val))
                c2.font = val_font
                c2.alignment = val_align

                if fill:
                    c1.fill = fill
                    c2.fill = fill

                # Merge C and D for cleaner look with single column data
                ws2.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
                ws2.cell(r, 2).border = Border(right=sep_v, bottom=bot)
                ws2.cell(r, 1).border = Border(bottom=bot)
                ws2.cell(r, 3).border = Border(bottom=bot)

            ws2.row_dimensions[r].height = 22

        # ── Chart image (requires kaleido) ────────────────────────────────────────
        if history:
            try:
                import plotly.graph_objects as go
                from plotly.subplots import make_subplots
                from openpyxl.drawing.image import Image as _XLImg
                from io import BytesIO as _BIO

                dates   = [h["date"]  for h in history]
                closes  = [h["close"] for h in history]
                volumes = [h.get("volume") or 0 for h in history]

                fig = make_subplots(rows=2, cols=1, row_heights=[0.72, 0.28],
                                    shared_xaxes=True, vertical_spacing=0.0)
                fig.add_trace(go.Scatter(x=dates, y=closes, mode="lines",
                    fill="tozeroy", fillcolor="rgba(214,46,47,0.07)",
                    line=dict(color="#D62E2F", width=2), name="Price"), row=1, col=1)
                fig.add_trace(go.Bar(x=dates, y=volumes,
                    marker_color="rgba(214,46,47,0.35)", name="Volume"), row=2, col=1)

                # Determine chart title based on aggregation type
                if chart_data and len(chart_data) <= 6:
                    excel_chart_title = "Market Capitalization Over Quarterly"
                else:
                    excel_chart_title = "Market Capitalization Over Monthly"

                fig.update_layout(showlegend=False, plot_bgcolor="white", paper_bgcolor="white",
                    margin=dict(l=40, r=10, t=30, b=10), height=280, width=620,
                    title=dict(text=excel_chart_title, font=dict(size=11)))
                fig.update_xaxes(showgrid=False)
                fig.update_yaxes(showgrid=True, gridcolor="#f0f0f0")

                img_bytes = fig.to_image(format="png", scale=1.5)
                img_anchor_row = 4 + len(snapshot_rows) + 2
                ws2.row_dimensions[img_anchor_row - 1].height = 8  # spacer
                xl_img = _XLImg(_BIO(img_bytes))
                xl_img.anchor = f"A{img_anchor_row}"
                ws2.add_image(xl_img)
            except Exception:
                pass  # kaleido not installed or plotly error — chart is optional

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as exc:
        log_structured_error(exc, page="excel_export", component="excel_export", operation="export_company_full_excel")
        return b""


# ── Financial Tabs ────────────────────────────────────────────────────────────

_INDENT = ["", "  ", "    "]      # 0 / 1 / 2 levels

def export_financial_excel(
    tab_name: str,
    company_name: str,
    ticker: str,
    period_labels: List[str],        # raw labels with "\n", e.g. "12 Months\nFeb-28-2013"
    excel_rows: List[Dict],
    units_label: str = "Millions",
    target_currency: str = "USD",
    start_date_label: str = "",
    end_date_label: str = "",
) -> bytes:
    """
    Formatted Excel for Key Stats, Income Statement, Balance Sheet, Cash Flow.

    Each dict in excel_rows:
      label         : str
      values        : list[float|None]   (already converted: currency + units)
      is_bold       : bool
      indent        : int  0/1/2
      is_percent    : bool  (value is raw %, e.g. 25.26 → store as 0.2526)
      is_text       : bool
      has_separator : bool
      is_estimated  : list[bool]  (per-column, Key Stats only)
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        ws = wb.active
        ws.title = tab_name[:31]
        ws.sheet_view.showGridLines = False

        n_cols     = len(period_labels)
        lbl_col    = 1
        data_start = 2
        last_col   = data_start + n_cols - 1

        # Column widths
        ws.column_dimensions[get_column_letter(lbl_col)].width = 36
        for ci in range(data_start, last_col + 1):
            ws.column_dimensions[get_column_letter(ci)].width = 15

        # ── Row 1: Company + tab name ─────────────────────────────────────────
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        c = ws.cell(1, 1)
        c.value = f"{_cl(company_name)}  ({_cl(ticker)})  —  {tab_name}"
        c.font = Font(name="Calibri", size=14, bold=True, color=_DARK[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[1].height = 28

        # ── Row 2: Filter summary ─────────────────────────────────────────────
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
        c = ws.cell(2, 1)
        parts = [f"Currency: {_format_currency_display(target_currency)}", f"Units: {units_label}"]
        if start_date_label and end_date_label:
            parts.append(f"Period: {start_date_label} – {end_date_label}")
        c.value = "  |  ".join(parts)
        c.font = Font(name="Calibri", size=9, color=_GRAY[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[2].height = 16
        for ci in range(1, last_col + 1):
            ws.cell(2, ci).border = Border(bottom=_RED_S())

        # ── Row 3: Spacer ─────────────────────────────────────────────────────
        ws.row_dimensions[3].height = 4

        # ── Row 4: Column headers ─────────────────────────────────────────────
        hdr_row  = 4
        hdr_fill = PatternFill("solid", fgColor=_HDR[2:])
        hdr_bot  = Side(style="medium", color=_DARK[2:])

        c = ws.cell(hdr_row, lbl_col)
        c.value = f"For Fiscal Period Ending\n({units_label} of {target_currency}, except per share items)"
        c.font = Font(name="Calibri", size=9, bold=True, color=_DARK[2:])
        c.fill = hdr_fill
        c.alignment = Alignment(horizontal="left", vertical="center",
                                wrap_text=True, indent=1)
        c.border = Border(top=_THIN(), bottom=hdr_bot)
        ws.row_dimensions[hdr_row].height = 36

        for ci_off, plabel in enumerate(period_labels):
            ci = data_start + ci_off
            c = ws.cell(hdr_row, ci)
            c.value = _cl(plabel)
            c.font = Font(name="Calibri", size=9, bold=True, color=_DARK[2:])
            c.fill = hdr_fill
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = Border(top=_THIN(), bottom=hdr_bot, left=_THIN())

        # ── Data rows ─────────────────────────────────────────────────────────
        cur_row    = hdr_row + 1
        alt_fill   = PatternFill("solid", fgColor=_ALT[2:])
        bold_fill  = PatternFill("solid", fgColor=_TOTAL[2:])
        white_fill = PatternFill("solid", fgColor=_WHITE[2:])
        est_color  = "FF0066CC"

        alt_idx = 0
        for row in excel_rows:
            label      = row.get("label", "")
            values     = row.get("values", [])
            is_bold    = row.get("is_bold", False)
            indent     = min(row.get("indent", 0), 2)
            is_percent = row.get("is_percent", False)
            is_text    = row.get("is_text", False)
            has_sep    = row.get("has_separator", False)
            est_flags  = row.get("is_estimated", [False] * len(values))

            # Empty label = spacer row
            if not label:
                ws.row_dimensions[cur_row].height = 6
                cur_row += 1
                continue

            fill = bold_fill if is_bold else (alt_fill if alt_idx % 2 == 0 else white_fill)

            # Label cell
            c = ws.cell(cur_row, lbl_col)
            c.value = _INDENT[indent] + _cl(label)
            c.font = Font(name="Calibri", size=9, bold=is_bold, color=_DARK[2:])
            c.fill = fill
            c.alignment = Alignment(horizontal="left", vertical="center")

            # Value cells
            for ci_off, val in enumerate(values):
                ci = data_start + ci_off
                is_est = est_flags[ci_off] if ci_off < len(est_flags) else False
                color  = est_color if is_est else _DARK[2:]

                c = ws.cell(cur_row, ci)
                c.fill = fill
                c.alignment = Alignment(horizontal="right", vertical="center")
                c.border = Border(left=_THIN())

                if val is None:
                    c.value = "-"
                    c.font  = Font(name="Calibri", size=9, color="FFAAAAAA")
                elif is_text:
                    c.value = _cl(str(val))
                    c.font  = Font(name="Calibri", size=9, bold=is_bold, color=color)
                elif is_percent:
                    # Raw value is already "25.26" meaning 25.26% → store 0.2526
                    c.value          = val / 100.0
                    c.number_format  = "0.00%"
                    c.font           = Font(name="Calibri", size=9, bold=is_bold, color=color)
                else:
                    c.value          = round(val, 3)
                    c.number_format  = "#,##0.000"
                    c.font           = Font(name="Calibri", size=9, bold=is_bold, color=color)

            # Grey separator: thick bottom border on this row
            if has_sep:
                sep_bot = Side(style="medium", color="FFC0C0C0")
                for ci in range(lbl_col, last_col + 1):
                    existing = ws.cell(cur_row, ci).border
                    ws.cell(cur_row, ci).border = Border(
                        top=existing.top, left=existing.left, right=existing.right,
                        bottom=sep_bot,
                    )

            ws.row_dimensions[cur_row].height = 18
            cur_row += 1
            alt_idx += 1

        # ── Freeze panes: keep header visible while scrolling ─────────────────
        ws.freeze_panes = ws.cell(hdr_row + 1, data_start)

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as exc:
        log_structured_error(exc, page="excel_export", component="excel_export", operation="export_financial_excel")
        return b""


# ── Simplified Company Profile Export (2 tabs only) ───────────────────────────

def export_company_profile_simple_excel(
    company_name: str,
    ticker: str,
    profile_rows: List[Tuple[str, str]],
    price_history: List[Dict],  # [{"date": "2024-01-01", "close": 100.5, "volume": 1000000}, ...]
    currency: str = "USD",
    shares_price_data: Optional[Dict] = None,  # result of StockQuoteRepository.get_shares_with_price()
    compensation_rows: Optional[List[Dict]] = None,
    compensation_col_defs: Optional[List[Tuple[str, str]]] = None,
) -> bytes:
    """
    Simplified Excel export with 2 tabs:
      Sheet 1 — Company Profile (info table)
      Sheet 2 — Market Cap (shares × close, exact date match)
                SEC:     Date | Close | Shares Basic | Mkt Cap Basic | Shares Diluted | Mkt Cap Diluted
                Non-SEC: Date | Close | Shares | Market Cap
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = Workbook()

        lbl_font  = Font(name="Calibri", size=10, bold=True, color=_DARK[2:])
        val_font  = Font(name="Calibri", size=10, color=_DARK[2:])
        align     = Alignment(horizontal="left",  vertical="center", indent=1)
        val_align = Alignment(horizontal="right", vertical="center", indent=1)
        hdr_font  = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
        hdr_fill  = PatternFill("solid", fgColor=_HDR[2:])
        alt_fill  = PatternFill("solid", fgColor=_ALT[2:])
        bot       = Side(style="thin", color=_LGRAY[2:])

        # ── Sheet 1: Company Profile ──────────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "Company Profile"
        ws1.sheet_view.showGridLines = False
        ws1.column_dimensions["A"].width = 30
        ws1.column_dimensions["B"].width = 36
        ws1.column_dimensions["C"].width = 30
        ws1.column_dimensions["D"].width = 36

        # Header
        ws1.merge_cells("A1:D1")
        c = ws1["A1"]
        c.value = f"{_cl(company_name)}  ({_cl(ticker)})"
        c.font = Font(name="Calibri", size=16, bold=True, color=_DARK[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws1.row_dimensions[1].height = 32

        ws1.merge_cells("A2:D2")
        c = ws1["A2"]
        c.value = "Company Profile"
        c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws1.row_dimensions[2].height = 18
        for col in range(1, 5):
            ws1.cell(2, col).border = Border(bottom=_RED_S())
        ws1.row_dimensions[3].height = 6

        # Data rows (2-column layout)
        pairs = [(profile_rows[i], profile_rows[i+1] if i+1 < len(profile_rows) else ("", ""))
                 for i in range(0, len(profile_rows), 2)]
        for i, ((lbl1, val1), (lbl2, val2)) in enumerate(pairs):
            r = 4 + i
            fill = alt_fill if i % 2 == 0 else None
            for col, val, fnt in [(1, lbl1, lbl_font), (2, val1, val_font),
                                   (3, lbl2, lbl_font), (4, val2, val_font)]:
                c = ws1.cell(r, col, _cl(val))
                c.font = fnt
                c.alignment = align
                if fill:
                    c.fill = fill
            ws1.cell(r, 2).border = Border(right=Side(style="thin", color=_LGRAY[2:]), bottom=bot)
            ws1.cell(r, 3).border = Border(left=Side(style="thin", color=_LGRAY[2:]),  bottom=bot)
            ws1.cell(r, 1).border = Border(bottom=bot)
            ws1.cell(r, 4).border = Border(bottom=bot)
            ws1.row_dimensions[r].height = 22

        # ── Sheet 2: Market Cap (shares × exact close price) ──────────────────────
        if shares_price_data and shares_price_data.get("rows"):
            is_sec = shares_price_data.get("is_sec", True)
            sp_rows = shares_price_data["rows"]

            ws2 = wb.create_sheet("Market Cap")
            ws2.sheet_view.showGridLines = False

            def _fmt_shares(v):
                if v is None:
                    return "-"
                v = float(v)
                if v >= 1e9:  return f"{v/1e9:.3f}B"
                if v >= 1e6:  return f"{v/1e6:.3f}M"
                if v >= 1e3:  return f"{v/1e3:.1f}K"
                return f"{v:,.0f}"

            def _fmt_mc(v):
                if v is None:
                    return "-"
                v = float(v)
                if v >= 1e12: return f"${v/1e12:.3f}T"
                if v >= 1e9:  return f"${v/1e9:.3f}B"
                if v >= 1e6:  return f"${v/1e6:.2f}M"
                return f"${v:,.0f}"

            # Title
            if is_sec:
                col_count = 6
                ws2.merge_cells(f"A1:F1")
            else:
                col_count = 4
                ws2.merge_cells(f"A1:D1")
            c = ws2.cell(1, 1, f"{_cl(company_name)}  ({_cl(ticker)})")
            c.font = Font(name="Calibri", size=14, bold=True, color=_DARK[2:])
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            ws2.row_dimensions[1].height = 28

            if is_sec:
                ws2.merge_cells("A2:F2")
            else:
                ws2.merge_cells("A2:D2")
            c = ws2.cell(2, 1, "Market Cap — Shares Outstanding × Close Price (Exact Date Match)")
            c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            c.border = Border(bottom=_RED_S())
            ws2.row_dimensions[2].height = 20

            # Column headers
            if is_sec:
                headers = [
                    "Date", f"Close Price ({_cl(currency)})",
                    "Shares Outstanding (Basic)", "Market Cap (Basic)",
                    "Shares Outstanding (Diluted)", "Market Cap (Diluted)",
                ]
                col_widths = [16, 20, 26, 20, 26, 20]
            else:
                headers = [
                    "Date", f"Close Price ({_cl(currency)})",
                    "Shares Outstanding", "Market Cap",
                ]
                col_widths = [16, 20, 24, 20]

            for i, (w, h) in enumerate(zip(col_widths, headers), 1):
                ws2.column_dimensions[get_column_letter(i)].width = w
                c = ws2.cell(3, i, h)
                c.font = hdr_font
                c.fill = hdr_fill
                c.alignment = align if i == 1 else val_align
                c.border = Border(bottom=bot)
            ws2.row_dimensions[3].height = 22

            # Data rows
            for i, row_data in enumerate(sp_rows):
                r = 4 + i
                fill = alt_fill if i % 2 == 0 else None

                def _wr(col, val, fmt=None):
                    c = ws2.cell(r, col, val)
                    c.font = val_font
                    c.alignment = align if col == 1 else val_align
                    if fill:
                        c.fill = fill
                    if fmt:
                        c.number_format = fmt

                _wr(1, row_data.get("date", ""))
                close = row_data.get("close")
                _wr(2, round(float(close), 2) if close else "-", '"$"#,##0.00' if close else None)

                if is_sec:
                    _wr(3, _fmt_shares(row_data.get("shares_basic")))
                    _wr(4, _fmt_mc(row_data.get("mktcap_basic")))
                    _wr(5, _fmt_shares(row_data.get("shares_diluted")))
                    _wr(6, _fmt_mc(row_data.get("mktcap_diluted")))
                else:
                    _wr(3, _fmt_shares(row_data.get("shares")))
                    _wr(4, _fmt_mc(row_data.get("mktcap")))

                ws2.row_dimensions[r].height = 20

        # ── Sheet 3: Executive Compensation ──────────────────────────────────────
        if compensation_rows and compensation_col_defs:
            ws3 = wb.create_sheet("Executive Compensation")
            ws3.sheet_view.showGridLines = False

            ws3.merge_cells(f"A1:{get_column_letter(len(compensation_col_defs))}1")
            c = ws3.cell(1, 1, f"{_cl(company_name)}  ({_cl(ticker)})")
            c.font = Font(name="Calibri", size=14, bold=True, color=_DARK[2:])
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            ws3.row_dimensions[1].height = 28

            ws3.merge_cells(f"A2:{get_column_letter(len(compensation_col_defs))}2")
            c = ws3.cell(2, 1, "Executive Compensation")
            c.font = Font(name="Calibri", size=10, bold=True, color=_RED[2:])
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            c.border = Border(bottom=_RED_S())
            ws3.row_dimensions[2].height = 20

            _money_col_labels = {"Salary", "Bonus", "Stock Awards", "Total Compensation", "Total Pay"}
            for ci, (label, _) in enumerate(compensation_col_defs, 1):
                c = ws3.cell(3, ci, label)
                c.font = hdr_font
                c.fill = hdr_fill
                c.alignment = val_align if label in _money_col_labels else align
                c.border = Border(bottom=bot)
                ws3.column_dimensions[get_column_letter(ci)].width = 28 if label in ("Executive Name", "Position") else 18
            ws3.row_dimensions[3].height = 22

            for ri, row_data in enumerate(compensation_rows):
                r = 4 + ri
                row_fill = alt_fill if ri % 2 == 0 else None
                for ci, (label, key) in enumerate(compensation_col_defs, 1):
                    val = row_data.get(key)
                    disp = "" if val in (None, "") else val
                    c = ws3.cell(r, ci, disp)
                    c.font = val_font
                    c.alignment = val_align if label in _money_col_labels else align
                    if row_fill:
                        c.fill = row_fill
                ws3.row_dimensions[r].height = 20

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as exc:
        log_structured_error(exc, page="excel_export", component="excel_export", operation="export_company_profile_simple_excel")
        return b""
