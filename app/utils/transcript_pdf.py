"""
Transcript PDF Generator
========================
Generates a professionally formatted A4 PDF for earnings call transcripts.
Uses fpdf2 (pure Python, no system dependencies).
"""
from __future__ import annotations
from io import BytesIO
from typing import List, Dict, Optional

from fpdf import FPDF

try:
    from utils.server_logger import log_structured_error, log_error
except ImportError:
    def log_structured_error(exc, **kw): pass  # type: ignore
    def log_error(msg, **kw): pass  # type: ignore

# ── Brand Colors ──────────────────────────────────────────────────────────────
_RED   = (214, 46, 47)    # #D62E2F  Coresight brand red
_DARK  = (45, 42, 41)     # #2D2A29  primary text
_GRAY  = (107, 107, 107)  # #6B6B6B  secondary text
_LGRAY = (220, 220, 220)  # light divider


def _clean(text: str) -> str:
    """Replace Unicode characters that Latin-1 core fonts cannot render."""
    try:
        _MAP = {
            "\u2018": "'",   "\u2019": "'",   # curly single quotes
            "\u201c": '"',   "\u201d": '"',   # curly double quotes
            "\u2013": "-",   "\u2014": "--",  # en/em dash
            "\u2026": "...",                   # ellipsis
            "\u00a0": " ",                     # non-breaking space
            "\u2022": "*",                     # bullet
            "\u00e9": "e",   "\u00e8": "e",
            "\u00e0": "a",   "\u00f3": "o",
            "\u00fc": "u",   "\u00e4": "a",
            "\u00f6": "o",   "\u00df": "ss",
        }
        for ch, rep in _MAP.items():
            text = text.replace(ch, rep)
        return text.encode("latin-1", errors="replace").decode("latin-1")
    except Exception as e:
        log_structured_error(e, page="transcript_pdf", component="_clean", operation="UNICODE_CLEAN")
        return text if isinstance(text, str) else str(text)


class _TranscriptPDF(FPDF):
    """Custom FPDF subclass with branded header/footer on every page."""

    def __init__(self, company_name: str, ticker: str, year: str,
                 quarter: str, date_str: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self._co   = company_name
        self._tkr  = ticker
        self._year = year
        self._qtr  = quarter
        self._date = date_str
        self.set_auto_page_break(auto=True, margin=22)
        self.set_margins(left=15, top=14, right=15)

    def _mcell(self, h: float, txt: str, **kwargs) -> None:
        """multi_cell wrapper that resets x to l_margin after (fpdf2 bug workaround)."""
        self.multi_cell(0, h, txt, **kwargs)
        self.set_x(self.l_margin)

    # ── Per-page header ────────────────────────────────────────────────────────
    def header(self):
        # Red bar at top
        self.set_fill_color(*_RED)
        self.rect(0, 0, 210, 7, "F")

        # "CORESIGHT RESEARCH" — left, white
        self.set_xy(15, 1)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(255, 255, 255)
        self.cell(90, 5, "CORESIGHT RESEARCH", align="L")

        # "CONFIDENTIAL" — right, light pink
        self.set_xy(105, 1)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(255, 200, 200)
        self.cell(90, 5, "CONFIDENTIAL", align="R")

        # Reset cursor to left margin below bar
        self.set_xy(self.l_margin, 10)

    # ── Per-page footer ────────────────────────────────────────────────────────
    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*_LGRAY)
        self.set_line_width(0.3)
        self.line(15, self.get_y(), 195, self.get_y())

        self.set_y(-12)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*_GRAY)
        label = f"{self._co} ({self._tkr})  |  {self._year} {self._qtr} Earnings Call"
        # Left label
        self.set_x(15)
        self.cell(130, 5, _clean(label), align="L")
        # Right: page number
        self.set_x(145)
        self.cell(50, 5, f"Page {self.page_no()}", align="R")


# ── Cover / title block (first page only) ─────────────────────────────────────

def _add_title_block(pdf: _TranscriptPDF) -> None:
    """Render company name, meta line, and divider on first page."""
    try:
        pdf.ln(4)

        # Company name — large bold dark
        pdf.set_font("Helvetica", "B", 20)
        pdf.set_text_color(*_DARK)
        pdf._mcell(10, _clean(f"{pdf._co} ({pdf._tkr})"))

        # Year • Quarter • Date
        pdf.set_font("Helvetica", "", 11)
        pdf.set_text_color(*_GRAY)
        meta_parts = [pdf._year, pdf._qtr]
        if pdf._date:
            meta_parts.append(pdf._date)
        pdf._mcell(7, "  |  ".join(meta_parts))

        pdf.ln(1)

        # "EARNINGS CALL TRANSCRIPT" label — red uppercase
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*_RED)
        pdf._mcell(6, "EARNINGS CALL TRANSCRIPT")

        pdf.ln(2)

        # Red divider line
        pdf.set_draw_color(*_RED)
        pdf.set_line_width(0.8)
        pdf.line(15, pdf.get_y(), 195, pdf.get_y())
        pdf.set_x(pdf.l_margin)
        pdf.ln(7)
    except Exception as e:
        log_structured_error(e, page="transcript_pdf", component="_add_title_block", operation="RENDER_TITLE")


# ── Speaker segments ──────────────────────────────────────────────────────────

def _add_segment(pdf: _TranscriptPDF, speaker: str, text: str) -> None:
    """Render one speaker block: name in red + body text."""
    try:
        pdf.set_x(pdf.l_margin)

        # Speaker name — bold red
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(*_RED)
        pdf._mcell(6, _clean(speaker))

        # Body text — dark, regular weight
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*_DARK)

        paras = [p.strip() for p in text.split("\n") if p.strip()]
        if not paras:
            paras = [text.strip()] if text.strip() else []

        for para in paras:
            pdf._mcell(5.5, _clean(para))
            pdf.ln(1.5)

        pdf.ln(4)   # gap between speakers
    except Exception as e:
        log_structured_error(e, page="transcript_pdf", component="_add_segment", operation="RENDER_SEGMENT",
                             context={"speaker": speaker[:50] if speaker else ""})


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_transcript_pdf(
    company_name: str,
    ticker: str,
    year: str,
    quarter: str,
    segments: List[Dict],
    earnings_date: Optional[str] = None,
) -> bytes:
    """
    Build a formatted A4 PDF from parsed transcript segments.

    Parameters
    ----------
    company_name  : str   e.g. "Abercrombie & Fitch Co."
    ticker        : str   e.g. "ANF"
    year          : str   e.g. "2025"
    quarter       : str   e.g. "Q3"
    segments      : list  [{"speaker": str, "text": str}, ...]
    earnings_date : str   formatted date string e.g. "Aug 27, 2025" (optional)

    Returns
    -------
    bytes  — raw PDF bytes ready for st.download_button()
    """
    try:
        date_str = earnings_date or ""
        pdf = _TranscriptPDF(company_name, ticker, str(year), str(quarter), date_str)
        pdf.add_page()

        _add_title_block(pdf)

        for seg in segments:
            speaker = seg.get("speaker", "")
            text    = seg.get("text", "")
            if speaker or text:
                _add_segment(pdf, speaker, text)

        buf = BytesIO()
        pdf.output(buf)
        return buf.getvalue()
    except Exception as e:
        log_structured_error(e, page="transcript_pdf", component="generate_transcript_pdf", operation="BUILD_PDF",
                             context={"company": company_name, "ticker": ticker, "year": year, "quarter": quarter})
        raise
