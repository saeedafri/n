#!/usr/bin/env python3
"""Generate professional PDFs from docs/business-documentation/*.md.

Pipeline: Markdown → embed UI screenshots → optional Mermaid → branded HTML → Playwright PDF.

Usage:
    python scripts/generate_business_doc_pdf.py              # all docs
    python scripts/generate_business_doc_pdf.py login.md     # one file
    python scripts/generate_business_doc_pdf.py --verify login.pdf
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sys
import tempfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import fitz  # pymupdf
from playwright.sync_api import sync_playwright

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generate_technical_doc_pdf import (  # noqa: E402
    DIAGRAM_MAX_HEIGHT_PX,
    FOOTER_MARGIN_PT,
    HEADER_MARGIN_PT,
    TOC_PAGE_MARKER,
    _annotate_toc_links,
    _build_toc_html,
    _extract_heading_ids_from_html,
    _extract_title,
    _md_to_html,
    _replace_mermaid_blocks,
    _toc_headings,
    _wrap_content_sections,
    validate_toc_links,
)

REPO = Path(__file__).resolve().parents[1]
DOC_DIR = REPO / "docs" / "business-documentation"
ASSETS_DIR = DOC_DIR / "_assets"
LOGO_PATH = ASSETS_DIR / "coresight-logo.png"
TEMPLATE_PATH = DOC_DIR / "_pdf_template.html"
STYLES_PATH = DOC_DIR / "_pdf_styles.css"
SCREENSHOTS_DIR = DOC_DIR / "_screenshots"
VALIDATION_DIR = DOC_DIR / "_validation-business-v2"

# Preferred merge order (non-README page docs); README handled separately in merge script.
BUSINESS_DOC_ORDER = [
    "login.md",
    "home.md",
    "navigation-and-portal-overview.md",
    "market-data.md",
    "screening.md",
    "newsroom.md",
    "earnings-calls.md",
    "earnings-calendar.md",
    "live-earnings-transcript.md",
    "company-filings.md",
    "company-filings-add-files.md",
    "forecasting.md",
    "forecasting-admin.md",
    "access-management.md",
    "retailer-adding.md",
    "logs.md",
]

AUTH_BYPASS_PATTERNS = re.compile(
    r"APP_ENV=LOCAL|LOCAL_TEST_USER_EMAIL|run_local\.sh|"
    r"OIDC bypass|auth bypass|LOCAL\+DEBUG|LOCAL testing|Known dev pattern",
    re.IGNORECASE,
)

SCREENSHOT_IMG_RE = re.compile(
    r'<p>\s*<img\s+alt="([^"]*)"\s+src="([^"]+)"\s*/>\s*</p>',
    re.IGNORECASE,
)
SCREENSHOT_IMG_RE_ALT = re.compile(
    r'<p>\s*<img\s+src="([^"]+)"\s+alt="([^"]*)"\s*/>\s*</p>',
    re.IGNORECASE,
)
CALLOUT_CELL_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]|\*\*[①②③④⑤⑥⑦⑧⑨⑩]\*\*")


def _discover_md_files() -> list[Path]:
    """Return markdown sources in canonical order (README first when generating all)."""
    ordered: list[Path] = []
    readme = DOC_DIR / "README.md"
    if readme.exists():
        ordered.append(readme)
    for name in BUSINESS_DOC_ORDER:
        path = DOC_DIR / name
        if path.exists() and path not in ordered:
            ordered.append(path)
    for path in sorted(DOC_DIR.glob("*.md")):
        if path not in ordered:
            ordered.append(path)
    return ordered


def _logo_data_uri() -> str:
    if LOGO_PATH.exists():
        encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    tech_logo = REPO / "docs" / "technical-documentation" / "_assets" / "coresight-logo.png"
    if tech_logo.exists():
        encoded = base64.b64encode(tech_logo.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="40">'
        '<text x="0" y="30" font-family="Inter,sans-serif" font-size="22" '
        'font-weight="700" fill="#D62E2F">CORESIGHT RESEARCH</text></svg>'
    )
    encoded = base64.b64encode(svg.encode()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _resolve_screenshot_path(src: str) -> Path | None:
    """Resolve _screenshots/... or bare filename to an on-disk image."""
    src = src.strip()
    candidates: list[Path] = []
    if src.startswith("_screenshots/"):
        candidates.append(DOC_DIR / src)
    elif src.startswith("/"):
        candidates.append(Path(src))
    else:
        candidates.append(SCREENSHOTS_DIR / Path(src).name)
        candidates.append(DOC_DIR / src)

    for path in candidates:
        if path.exists():
            return path
    # Fallback: strip -annotated suffix or add it
    for path in list(candidates):
        stem = path.stem
        if stem.endswith("-annotated"):
            alt = path.with_name(stem.replace("-annotated", "") + path.suffix)
            if alt.exists():
                return alt
        else:
            alt = path.with_name(f"{stem}-annotated{path.suffix}")
            if alt.exists():
                return alt
    return None


def _image_to_data_uri(img_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(img_path))
    if not mime:
        mime = "image/png"
    encoded = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _style_callout_tables(html: str) -> str:
    """Add callout-table class to legend tables that follow screenshots."""

    def _is_callout_table(table_html: str, prefix: str) -> bool:
        if CALLOUT_CELL_RE.search(table_html):
            return True
        prefix_lower = prefix.lower()
        return "screenshot callout" in prefix_lower or "callout" in prefix_lower

    parts = re.split(r"(<table[\s>])", html)
    if len(parts) < 3:
        return html

    out: list[str] = [parts[0]]
    i = 1
    while i < len(parts):
        if not parts[i].startswith("<table"):
            out.append(parts[i])
            i += 1
            continue
        chunk = parts[i]
        i += 1
        while i < len(parts) and not parts[i].startswith("</table>"):
            chunk += parts[i]
            i += 1
        if i < len(parts):
            chunk += parts[i]
            i += 1
        prefix = "".join(out)[-1200:]
        if _is_callout_table(chunk, prefix):
            chunk = chunk.replace("<table>", '<table class="callout-table">', 1)
            if "<table " in chunk and 'class="callout-table"' not in chunk:
                chunk = re.sub(r"<table(\s)", r'<table class="callout-table"\1', chunk, count=1)
        out.append(chunk)
    return "".join(out)


def _embed_screenshots_in_html(html: str) -> tuple[str, int]:
    """Replace markdown-generated <img> tags with full-width embedded screenshot figures."""
    embedded = 0

    def _figure(caption: str, src: str) -> str:
        nonlocal embedded
        img_path = _resolve_screenshot_path(src)
        if img_path is None:
            print(f"    WARN: omitting missing screenshot {src}", file=sys.stderr)
            return ""
        data_uri = _image_to_data_uri(img_path)
        embedded += 1
        cap = escape(caption or img_path.stem.replace("-", " "))
        return (
            f'<figure class="screenshot">'
            f'<img src="{data_uri}" alt="{cap}" class="screenshot-image"/>'
            f"<figcaption>{cap}</figcaption>"
            f"</figure>"
        )

    def replacer_alt_first(match: re.Match[str]) -> str:
        src, caption = match.group(1), match.group(2)
        return _figure(caption, src)

    def replacer_caption_first(match: re.Match[str]) -> str:
        caption, src = match.group(1), match.group(2)
        return _figure(caption, src)

    html = SCREENSHOT_IMG_RE.sub(replacer_caption_first, html)
    html = SCREENSHOT_IMG_RE_ALT.sub(replacer_alt_first, html)
    return html, embedded


def _build_html(md_path: Path) -> tuple[str, str, list[tuple[int, str, str]], int]:
    md_text = md_path.read_text(encoding="utf-8")
    title = _extract_title(md_text, md_path)

    processed = _replace_mermaid_blocks(md_text, md_path.stem)
    content_html = _wrap_content_sections(_md_to_html(processed))
    content_html, screenshot_count = _embed_screenshots_in_html(content_html)
    content_html = _style_callout_tables(content_html)
    headings = _extract_heading_ids_from_html(content_html)
    toc_html = _build_toc_html(headings)

    css = STYLES_PATH.read_text(encoding="utf-8")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    generated = datetime.now(timezone.utc).strftime("%B %d, %Y")

    html = (
        template.replace("{{DOC_TITLE}}", escape(title))
        .replace("{{DOC_VERSION}}", "1.0")
        .replace("{{GENERATED_DATE}}", generated)
        .replace("{{LOGO_DATA_URI}}", _logo_data_uri())
        .replace("{{CSS_CONTENT}}", css)
        .replace("{{TOC_HTML}}", toc_html)
        .replace("{{CONTENT_HTML}}", content_html)
    )
    return html, title, headings, screenshot_count


def _header_footer_templates(title: str) -> tuple[str, str]:
    logo_uri = _logo_data_uri()
    safe_title = escape(title)
    header = f"""
<div style="width:100%; font-size:8pt; font-family:Inter,sans-serif; color:#495057;
            padding:0 0.5in; display:flex; align-items:center; justify-content:space-between;
            border-bottom:0.5pt solid #dee2e6; padding-bottom:4pt;">
  <img src="{logo_uri}" style="height:18px;" alt="Coresight"/>
  <span style="font-weight:600; color:#1a365d;">{safe_title}</span>
</div>
"""
    footer = """
<div style="width:100%; font-size:7.5pt; font-family:Inter,sans-serif; color:#6c757d;
            padding:0 0.5in; display:flex; justify-content:space-between; align-items:center;
            border-top:0.5pt solid #dee2e6; padding-top:4pt;">
  <span style="color:#D62E2F; font-weight:700;">CONFIDENTIAL</span>
  <span>Coresight Research — Internal Use Only</span>
  <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>
</div>
"""
    return header, footer


def _is_blank_page(page: fitz.Page) -> bool:
    text = page.get_text().strip()
    clean = re.sub(r"(CONFIDENTIAL|Coresight Research|Page \d+ of \d+)", "", text)
    clean = re.sub(r"\s+", "", clean)
    return (
        len(clean) < 100
        and len(page.get_images(full=True)) == 0
        and len(page.get_drawings()) < 50
    )


def verify_pdf(
    pdf_path: Path,
    md_path: Path | None = None,
    *,
    expected_screenshots: int | None = None,
) -> dict[str, object]:
    """Validate business PDF: screenshots, TOC, no auth bypass text, no blank pages."""
    if md_path is None:
        md_path = pdf_path.with_suffix(".md")

    doc = fitz.open(pdf_path)
    try:
        image_count = 0
        blank_pages: list[int] = []
        full_text: list[str] = []
        for i, page in enumerate(doc):
            image_count += len(page.get_images(full=True))
            full_text.append(page.get_text())
            if _is_blank_page(page):
                blank_pages.append(i + 1)

        text = "\n".join(full_text)
        auth_hits = AUTH_BYPASS_PATTERNS.findall(text)
        screenshot_errors: list[str] = []
        if "Screenshot not found:" in text:
            screenshot_errors.append("missing screenshot file")
        if re.search(r"Unable to load[^\n]{0,60}\.(png|jpe?g|webp)", text, re.IGNORECASE):
            screenshot_errors.append("broken screenshot embed")

        if expected_screenshots is None and md_path.exists():
            expected_screenshots = len(
                re.findall(r"!\[[^\]]*\]\(_screenshots/[^)]+\)", md_path.read_text(encoding="utf-8"))
            )
        expected_screenshots = expected_screenshots or 0

        screenshots_ok = image_count >= expected_screenshots if expected_screenshots > 0 else True

        return {
            "ok": screenshots_ok and not blank_pages and not auth_hits and not screenshot_errors,
            "expected_screenshots": expected_screenshots,
            "image_count": image_count,
            "blank_pages": blank_pages,
            "auth_bypass_hits": auth_hits,
            "screenshot_errors": screenshot_errors,
            "page_count": len(doc),
        }
    finally:
        doc.close()


def save_validation_screenshots(pdf_path: Path, out_dir: Path | None = None) -> list[Path]:
    """Save cover + TOC + first content sample screenshots for visual QA."""
    out_dir = out_dir or VALIDATION_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    shots: list[Path] = []
    try:
        targets = [(0, "cover.png"), (1, "toc.png"), (2, "content.png")]
        for page_idx, name in targets:
            if page_idx < len(doc):
                pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2, 2))
                out = out_dir / f"{pdf_path.stem}-{name}"
                pix.save(str(out))
                shots.append(out)
    finally:
        doc.close()
    return shots


def generate_pdf(md_path: Path, pdf_path: Path | None = None) -> tuple[Path, int, list[tuple[int, str, str]], int]:
    if pdf_path is None:
        pdf_path = md_path.with_suffix(".pdf")

    html, title, headings, screenshot_count = _build_html(md_path)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(html)
        html_path = Path(tmp.name)

    header, footer = _header_footer_templates(title)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(html_path.as_uri(), wait_until="networkidle")
            page.evaluate(
                """(maxHeight) => {
                  document.querySelectorAll('.diagram-block img, figure.diagram img').forEach((img) => {
                    const naturalHeight = img.naturalHeight || img.height || 0;
                    const block = img.closest('.diagram-block');
                    if (naturalHeight > maxHeight) {
                      const scale = maxHeight / naturalHeight;
                      img.style.maxHeight = `${maxHeight}px`;
                      img.style.width = `${Math.round((img.naturalWidth || img.width) * scale)}px`;
                      img.style.height = `${maxHeight}px`;
                      img.style.objectFit = 'contain';
                      if (block) block.classList.add('diagram-oversized');
                    }
                  });
                  document.querySelectorAll('figure.screenshot img').forEach((img) => {
                    const fig = img.closest('figure.screenshot');
                    if ((img.naturalHeight || img.height || 0) > 520 && fig) {
                      fig.classList.add('screenshot-oversized');
                    }
                  });
                }""",
                DIAGRAM_MAX_HEIGHT_PX,
            )
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                display_header_footer=True,
                header_template=header,
                footer_template=footer,
                margin={
                    "top": "0.9in",
                    "bottom": "0.75in",
                    "left": "0.75in",
                    "right": "0.75in",
                },
            )
            browser.close()
    finally:
        html_path.unlink(missing_ok=True)

    _annotate_toc_links(pdf_path, headings)
    return pdf_path, screenshot_count, headings, screenshot_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate business documentation PDFs")
    parser.add_argument(
        "targets",
        nargs="*",
        help="Markdown filenames or PDF paths to verify (default: all business docs)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify PDF screenshots and validation checks",
    )
    parser.add_argument(
        "--validation-shots",
        action="store_true",
        help="Save cover/TOC validation screenshots to _validation-business/",
    )
    args = parser.parse_args(argv)

    if args.verify:
        targets = args.targets or ["login.pdf"]
        ok = True
        for t in targets:
            pdf = DOC_DIR / t if not Path(t).is_absolute() else Path(t)
            if not pdf.exists():
                print(f"{pdf.name}: MISSING", file=sys.stderr)
                ok = False
                continue
            result = verify_pdf(pdf)
            md_path = pdf.with_suffix(".md")
            toc_result = {}
            if md_path.exists():
                from generate_technical_doc_pdf import _extract_heading_ids_from_html as _eh

                md_text = md_path.read_text(encoding="utf-8")
                processed = _replace_mermaid_blocks(md_text, md_path.stem)
                content_html = _embed_screenshots_in_html(_wrap_content_sections(_md_to_html(processed)))[0]
                headings = _extract_heading_ids_from_html(content_html)
                toc_result = validate_toc_links(pdf, headings)

            size = pdf.stat().st_size
            if result["ok"] and toc_result.get("ok", True):
                status = (
                    f"OK (screenshots={result['image_count']}/{result['expected_screenshots']}, "
                    f"pages={result['page_count']})"
                )
            else:
                reasons = []
                if result["expected_screenshots"] and result["image_count"] < result["expected_screenshots"]:
                    reasons.append("missing screenshots")
                if result["blank_pages"]:
                    reasons.append(f"blank pages {result['blank_pages']}")
                if result["screenshot_errors"]:
                    reasons.append(f"screenshot errors {result['screenshot_errors']}")
                if toc_result and not toc_result.get("ok"):
                    reasons.append("TOC links failed")
                status = "FAIL (" + "; ".join(reasons) + ")"
            print(f"{pdf.name}: {size:,} bytes — {status}")
            ok = ok and bool(result["ok"]) and toc_result.get("ok", True)
        return 0 if ok else 1

    if args.targets:
        md_files = []
        for t in args.targets:
            p = Path(t)
            if not p.suffix:
                p = DOC_DIR / f"{t}.md"
            elif p.suffix == ".pdf":
                p = DOC_DIR / f"{p.stem}.md"
            elif not p.is_absolute():
                p = DOC_DIR / p.name
            md_files.append(p)
    else:
        md_files = _discover_md_files()

    missing = [p for p in md_files if not p.exists()]
    if missing:
        for p in missing:
            print(f"ERROR: missing {p}", file=sys.stderr)
        return 1

    print(f"Generating {len(md_files)} business PDF(s) → {DOC_DIR}")

    ok_count = 0
    for md_path in md_files:
        try:
            pdf_path, _sc, headings, sc_count = generate_pdf(md_path)
            size = pdf_path.stat().st_size
            vresult = verify_pdf(pdf_path, md_path, expected_screenshots=sc_count)
            toc_result = validate_toc_links(pdf_path, headings)
            sc_note = (
                f"screenshots {vresult['image_count']}/{vresult['expected_screenshots']}"
                if vresult["expected_screenshots"]
                else "no screenshots"
            )
            toc_note = (
                f"TOC {toc_result['link_count']}/{toc_result['expected']}"
                if toc_result["ok"]
                else f"TOC FAIL ({toc_result['link_count']}/{toc_result['expected']})"
            )
            blank_note = f"blank={vresult['blank_pages']}" if vresult["blank_pages"] else "no blanks"
            print(f"  ✓ {pdf_path.name} ({size:,} bytes, {sc_note}, {toc_note}, {blank_note})")
            if args.validation_shots:
                shots = save_validation_screenshots(pdf_path)
                print(f"    validation: {', '.join(s.name for s in shots)}")
            ok_count += 1
        except Exception as exc:
            print(f"  ✗ {md_path.name}: {exc}", file=sys.stderr)

    print(f"\nDone: {ok_count}/{len(md_files)} succeeded")
    return 0 if ok_count == len(md_files) else 1


if __name__ == "__main__":
    sys.exit(main())
