"""Filing PDF generation must finish.

SEC filings mark page boundaries with `<hr style="page-break-after:always">`.
PyMuPDF's Story never advances past a forced page break, so the page loop in
generate_filing_pdf emitted blank pages forever (a 700 KB 10-Q ran 15+ minutes
and pinned the server's GIL, freezing every session). These tests fail by
timing out or by producing an absurd page count.
"""

import sys
import time
from pathlib import Path

import fitz
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from utils.html_to_pdf import generate_filing_pdf  # noqa: E402

PAGE_BREAK_HTML = (
    "<div><p>Cover page</p></div>"
    '<hr style="page-break-after:always"/>'
    "<div><p>Part I</p></div>"
    '<div style="break-before: page">Part II</div>'
)


def page_count(pdf: bytes) -> int:
    return fitz.open(stream=pdf, filetype="pdf").page_count


def test_forced_page_breaks_do_not_loop():
    started = time.perf_counter()
    pdf = generate_filing_pdf("Macy's", "M", "2026", "10-Q", "Q1", PAGE_BREAK_HTML)
    assert time.perf_counter() - started < 5
    assert pdf.startswith(b"%PDF")
    assert 1 <= page_count(pdf) <= 3


REAL_FILING = REPO / "data" / "filings_blob_cache" / "M" / "2027" / "10-Q-Q1" / "filing.html"


@pytest.mark.skipif(not REAL_FILING.exists(), reason="cached Macy's 10-Q not present")
def test_real_10q_renders_in_seconds():
    html = REAL_FILING.read_text(encoding="utf-8", errors="replace")
    started = time.perf_counter()
    pdf = generate_filing_pdf("Macy's", "M", "2027", "10-Q", "Q1", html)
    assert time.perf_counter() - started < 30
    assert 10 <= page_count(pdf) <= 200
