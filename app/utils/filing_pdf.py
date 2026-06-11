"""Simple Filing PDF Generator - Clean text only"""
from io import BytesIO
from typing import Optional
import re
from html import unescape
from fpdf import FPDF
from utils.server_logger import log_structured_error, log_error


def _clean_text(text: str) -> str:
    """Clean text for PDF output."""
    if not text:
        return ""
    # Replace Unicode checkbox characters with ASCII equivalents (fpdf2 doesn't support them)
    text = text.replace("☒", "[X]").replace("☐", "[ ]")
    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    # Remove XBRL technical identifiers
    text = re.sub(r'\b(us-gaap|xbrli|iso|srt|anf|000\d+):\S+', '', text)
    text = re.sub(r'\b[0-9]{10,20}\b', '', text)
    # Clean whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Soft limit: truncate extremely large filings for fpdf2 memory safety
    _MAX_CHARS = 500_000  # ~125 pages — covers virtually all SEC filings
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + "\n\n[Content truncated — full filing available on SEC EDGAR]"
    return text


def _strip_html(html: str) -> str:
    """Simple HTML to text conversion."""
    if not html:
        return ""
    # Remove scripts/styles
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block elements with newlines
    html = re.sub(r'</(p|div|h[1-6]|br|tr|li)[^>]*>', '\n', html, flags=re.IGNORECASE)
    # Remove remaining tags
    html = re.sub(r'<[^>]+>', ' ', html)
    # Decode entities
    html = unescape(html)
    return _clean_text(html)


def generate_filing_pdf(
    company_name: str,
    ticker: str,
    year: str,
    doc_type: str,
    quarter: str = "",
    html_content: Optional[str] = None,
) -> bytes:
    """Generate simple PDF from HTML content."""
    try:
        pdf = FPDF(orientation="P", unit="mm", format="A4")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Title
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, f"{company_name} ({ticker})", ln=True)

        pdf.set_font("Helvetica", "", 12)
        meta = f"{year} | {doc_type}"
        if quarter and quarter != "Annual":
            meta += f" | {quarter}"
        pdf.cell(0, 8, meta, ln=True)
        pdf.ln(5)

        # Divider line
        pdf.set_draw_color(214, 46, 47)  # Coresight red
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(5)

        # Content
        if html_content:
            text = _strip_html(html_content)
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, text)

        buf = BytesIO()
        pdf.output(buf)
        return buf.getvalue()
    except Exception as e:
        log_structured_error(e, page="filing_pdf", component="generate_filing_pdf", operation="generating_filing_pdf")
        return b""
