#!/usr/bin/env python3
"""Build production merged business user guide PDF from markdown sources.

Pipeline: all docs → single branded HTML (cover + master TOC + section dividers)
→ Playwright PDF → clickable TOC annotations.

Usage:
    python scripts/merge_business_doc_pdf.py
    python scripts/merge_business_doc_pdf.py --validate-only
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fitz  # pymupdf
from playwright.sync_api import sync_playwright

from generate_business_doc_pdf import (  # noqa: E402
    AUTH_BYPASS_PATTERNS,
    DOC_DIR,
    STYLES_PATH,
    VALIDATION_DIR,
    _embed_screenshots_in_html,
    _extract_title,
    _is_blank_page,
    _logo_data_uri,
    _md_to_html,
    _style_callout_tables,
    save_validation_screenshots,
)
from generate_technical_doc_pdf import (  # noqa: E402
    DIAGRAM_MAX_HEIGHT_PX,
    _replace_mermaid_blocks,
    _wrap_content_sections,
)

REPO = Path(__file__).resolve().parents[1]
OUTPUT_PDF = DOC_DIR / "Market-Data-Portal-User-Guide-Complete.pdf"

MERGED_TITLE = "Market Data Portal — Complete User Guide"

MERGE_SEQUENCE: list[dict[str, str]] = [
    {
        "file": "README.md",
        "slug": "readme",
        "description": "Master index — getting started, audience, and links to every user guide section.",
    },
    {
        "file": "login.md",
        "slug": "login",
        "description": "Sign in with Coresight single sign-on (SSO) and resolve common access issues.",
    },
    {
        "file": "home.md",
        "slug": "home",
        "description": "Home dashboard — pick a company and jump into Market Data.",
    },
    {
        "file": "navigation-and-portal-overview.md",
        "slug": "navigation",
        "description": "Top navigation, every major page, and when to use each tool.",
    },
    {
        "file": "market-data.md",
        "slug": "market-data",
        "description": "Financial statements, key stats, estimates, segments, and company profile.",
    },
    {
        "file": "screening.md",
        "slug": "screening",
        "description": "Multi-criteria screener, watchlists, and saved criteria.",
    },
    {
        "file": "newsroom.md",
        "slug": "newsroom",
        "description": "News feed with sentiment, ticker filters, and sector browsing.",
    },
    {
        "file": "earnings-calls.md",
        "slug": "earnings-calls",
        "description": "Earnings transcript search, PDF download, and watchlist scope.",
    },
    {
        "file": "earnings-calendar.md",
        "slug": "earnings-calendar",
        "description": "Earnings calendar views, filters, and email alert preferences.",
    },
    {
        "file": "live-earnings-transcript.md",
        "slug": "live-earnings-transcript",
        "description": "Live earnings transcript display during active calls.",
    },
    {
        "file": "company-filings.md",
        "slug": "company-filings",
        "description": "Browse SEC filings, metric search, and PDF viewer.",
    },
    {
        "file": "company-filings-add-files.md",
        "slug": "company-filings-add-files",
        "description": "Admin workflow — upload non-SEC filings to the document library.",
    },
    {
        "file": "forecasting.md",
        "slug": "forecasting",
        "description": "Revenue forecasts, model comparison, scenarios, and refresh.",
    },
    {
        "file": "forecasting-admin.md",
        "slug": "forecasting-admin",
        "description": "Forecast administration and bulk refresh (admin users).",
    },
    {
        "file": "access-management.md",
        "slug": "access-management",
        "description": "Page-level access control for portal administrators.",
    },
    {
        "file": "retailer-adding.md",
        "slug": "retailer-adding",
        "description": "Add and maintain retail company master data.",
    },
    {
        "file": "logs.md",
        "slug": "logs",
        "description": "Server log viewer — IT administrators only.",
    },
]

MERGED_EXTRA_CSS = """
/* ── Merged user guide: section dividers & end markers ── */
.section-divider-page {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: calc(100vh - 1.7in);
  padding: 0.5in 0.25in;
  page-break-before: always;
  break-before: page;
  page-break-after: avoid;
  break-after: avoid;
  text-align: center;
  position: relative;
}

.divider-accent {
  width: 120pt;
  height: 5pt;
  background: var(--cs-red);
  margin: 0 auto 28pt;
  border-radius: 2pt;
}

.divider-part {
  font-size: 10pt;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--cs-gray-600);
  margin: 0 0 16pt;
}

.divider-title {
  font-size: 24pt;
  font-weight: 700;
  color: var(--cs-primary-dark);
  margin: 0 0 14pt;
  line-height: 1.25;
  max-width: 6.5in;
  margin-left: auto;
  margin-right: auto;
}

.divider-desc {
  font-size: 12pt;
  color: var(--cs-gray-600);
  max-width: 5.5in;
  margin: 0 auto;
  line-height: 1.55;
}

.divider-footer {
  position: absolute;
  bottom: 0.35in;
  left: 0;
  right: 0;
  font-size: 9pt;
  color: var(--cs-gray-600);
  font-style: italic;
}

.doc-section {
  page-break-before: auto;
}

.doc-section > .doc-content > h1:first-child {
  margin-top: 0;
}

.doc-end-marker {
  margin: 36pt 0 0;
  padding-bottom: 24pt;
  text-align: center;
  page-break-after: avoid;
  break-after: avoid;
}

.doc-end-rule {
  border: none;
  border-top: 0.75pt solid var(--cs-gray-200);
  margin: 0 auto 10pt;
  width: 40%;
}

.doc-end-marker p {
  font-size: 9.5pt;
  color: var(--cs-gray-600);
  font-style: italic;
  margin: 0;
}

.merged-toc .toc-doc-entry {
  font-weight: 600;
  font-size: 10pt;
  margin: 5pt 0;
}

.merged-toc .toc-doc-entry a {
  display: flex;
  align-items: baseline;
  gap: 0;
  color: var(--cs-primary-dark);
  text-decoration: none;
}

.merged-toc .toc-doc-entry .toc-entry-text {
  flex: 0 1 auto;
  padding-right: 6pt;
}

.merged-toc .toc-doc-entry .toc-leader {
  flex: 1 1 auto;
  border-bottom: 0.75pt dotted var(--cs-gray-600);
  margin: 0 6pt 2pt;
  min-width: 16pt;
  opacity: 0.55;
}

.merged-toc .toc-doc-entry .toc-page-num {
  flex: 0 0 auto;
  min-width: 18pt;
  text-align: right;
  font-size: 9.5pt;
  color: var(--cs-gray-600);
  font-variant-numeric: tabular-nums;
}

.doc-section > .doc-content {
  page-break-before: always;
  break-before: page;
}

.doc-section:first-of-type > .doc-content {
  page-break-before: auto;
  break-before: auto;
}

.merged-toc .toc-doc-entry .toc-part {
  color: var(--cs-gray-600);
  font-weight: 500;
  font-size: 9.5pt;
  margin-right: 8pt;
}

.merged-cover .cover-title {
  font-size: 22pt;
}

.merged-cover .cover-subtitle {
  font-size: 15pt;
}
"""


def _available_sequence() -> list[dict[str, str]]:
    return [spec for spec in MERGE_SEQUENCE if (DOC_DIR / spec["file"]).exists()]


def _build_master_cover(generated: str, section_count: int) -> str:
    return f"""
  <section class="cover-page merged-cover business-cover">
    <div class="cover-top">
      <img class="cover-logo" src="{_logo_data_uri()}" alt="Coresight Research">
      <h1 class="cover-title">{escape(MERGED_TITLE)}</h1>
      <p class="cover-subtitle">Market Data Portal — User Guide</p>
      <div class="cover-meta">
        <p><strong>Document version:</strong> 1.0</p>
        <p><strong>Generated:</strong> {escape(generated)}</p>
        <p><strong>Classification:</strong> Confidential — Internal Use Only</p>
        <p><strong>Sections:</strong> {section_count} guides</p>
      </div>
    </div>
    <footer class="cover-footer">
      <div class="confidential-banner">
        CONFIDENTIAL — Internal Use Only — Do Not Distribute
      </div>
      <p class="cover-footer-note">
        This document contains proprietary information belonging to Coresight Research.
        Unauthorized disclosure, copying, or distribution is strictly prohibited.
      </p>
    </footer>
  </section>
"""


def _build_master_toc(entries: list[tuple[int, str, str, str]]) -> str:
    items: list[str] = []
    for part, title, slug, _desc in entries:
        items.append(
            f'<li class="toc-doc-entry">'
            f'<a href="#doc-{escape(slug)}">'
            f'<span class="toc-part">Part {part}</span>'
            f'<span class="toc-entry-text">{escape(title)}</span>'
            f'<span class="toc-leader"></span>'
            f'<span class="toc-page-num"></span>'
            f"</a>"
            f"</li>"
        )
    return f"""
  <section class="toc-page merged-toc">
    <h2>Table of Contents</h2>
    <ul class="toc-list">{"".join(items)}</ul>
  </section>
"""


def _build_section_divider(part: int, total: int, title: str, description: str, slug: str) -> str:
    transition = (
        "The previous section is complete — the next chapter begins below."
        if part > 2
        else "Welcome — this chapter begins the guided walkthrough."
    )
    return f"""
  <section class="section-divider-page" id="doc-{escape(slug)}">
    <div class="divider-accent"></div>
    <p class="divider-part">Part {part} of {total}</p>
    <h2 class="divider-title">{escape(title)}</h2>
    <p class="divider-desc">{escape(description)}</p>
    <footer class="divider-footer">{escape(transition)}</footer>
  </section>
"""


def _build_doc_end(title: str) -> str:
    return f"""
    <div class="doc-end-marker">
      <hr class="doc-end-rule"/>
      <p>— End of {escape(title)} —</p>
    </div>
"""


def _doc_content_html(md_path: Path) -> str:
    md_text = md_path.read_text(encoding="utf-8")
    processed = _replace_mermaid_blocks(md_text, md_path.stem)
    html = _wrap_content_sections(_md_to_html(processed))
    html, _ = _embed_screenshots_in_html(html)
    return _style_callout_tables(html)


def _header_footer_templates() -> tuple[str, str]:
    logo_uri = _logo_data_uri()
    safe_title = escape(MERGED_TITLE)
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


def build_merged_html(sequence: list[dict[str, str]] | None = None) -> str:
    sequence = sequence or _available_sequence()
    if not sequence:
        raise RuntimeError(f"No markdown files found in {DOC_DIR}")

    generated = datetime.now(timezone.utc).strftime("%B %d, %Y")
    total = len(sequence)
    css = STYLES_PATH.read_text(encoding="utf-8") + MERGED_EXTRA_CSS

    toc_entries: list[tuple[int, str, str, str]] = []
    body_parts: list[str] = [_build_master_cover(generated, total)]

    for idx, spec in enumerate(sequence, start=1):
        md_path = DOC_DIR / spec["file"]
        title = _extract_title(md_path.read_text(encoding="utf-8"), md_path)
        toc_entries.append((idx, title, spec["slug"], spec["description"]))

    body_parts.append(_build_master_toc(toc_entries))

    for idx, spec in enumerate(sequence, start=1):
        md_path = DOC_DIR / spec["file"]
        title = toc_entries[idx - 1][1]
        slug = spec["slug"]
        content = _doc_content_html(md_path)

        if spec["file"] != "README.md":
            body_parts.append(
                _build_section_divider(idx, total, title, spec["description"], slug)
            )
            anchor = ""
        else:
            anchor = f' id="doc-{slug}"'

        body_parts.append(
            f'  <section class="doc-section"{anchor}>\n'
            f'    <main class="doc-content">\n{content}\n    </main>\n'
            f"{_build_doc_end(title)}\n"
            f"  </section>"
        )

    body = "\n".join(body_parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{escape(MERGED_TITLE)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">
  <style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def _find_section_pages(doc: fitz.Document, sequence: list[dict[str, str]]) -> dict[str, int]:
    """Map doc slug → 0-based page index by scanning divider and README markers."""
    total = len(sequence)
    slug_pages: dict[str, int] = {}
    for page_idx in range(len(doc)):
        text = doc[page_idx].get_text()
        text_lower = text.lower()
        for spec in sequence:
            slug = spec["slug"]
            if slug in slug_pages:
                continue
            part = next(i + 1 for i, s in enumerate(sequence) if s["slug"] == slug)
            if spec["file"] == "README.md":
                if "getting started" in text_lower or "business documentation" in text_lower:
                    slug_pages[slug] = page_idx
            elif f"part {part} of {total}" in text_lower and (
                "previous section is complete" in text_lower
                or "guided walkthrough" in text_lower
                or "continued from previous section" in text_lower
            ):
                slug_pages[slug] = page_idx
    return slug_pages


def _annotate_toc_links(pdf_path: Path, sequence: list[dict[str, str]] | None = None) -> int:
    """Add internal PDF link annotations from master TOC entries to section pages."""
    sequence = sequence or _available_sequence()
    doc = fitz.open(pdf_path)
    total = len(sequence)
    slug_pages = _find_section_pages(doc, sequence)
    if len(slug_pages) < total:
        missing = [s["slug"] for s in sequence if s["slug"] not in slug_pages]
        doc.close()
        raise RuntimeError(
            f"Could only locate {len(slug_pages)}/{total} section pages for TOC links; missing: {missing}"
        )

    toc_page_indices = [
        i for i in range(len(doc)) if "table of contents" in doc[i].get_text().lower()
    ]
    if not toc_page_indices:
        toc_page_indices = [1] if len(doc) > 1 else [0]

    links_added = 0

    for spec in sequence:
        slug = spec["slug"]
        target_page = slug_pages.get(slug)
        if target_page is None:
            continue
        md_path = DOC_DIR / spec["file"]
        title = _extract_title(md_path.read_text(encoding="utf-8"), md_path)
        part = next(i + 1 for i, s in enumerate(sequence) if s["slug"] == slug)
        search_terms = [title, f"Part {part} {title}", title[:50], title[:35]]
        hits: list = []
        toc_page = None
        for toc_page_idx in toc_page_indices:
            toc_page = doc[toc_page_idx]
            for term in search_terms:
                if not term:
                    continue
                hits = toc_page.search_for(term)
                if hits:
                    break
            if hits:
                break
        if not hits or toc_page is None:
            continue
        rect = hits[0]
        link_rect = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1)
        toc_page.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "page": target_page,
                "from": link_rect,
                "to": fitz.Point(0, 64),
            }
        )
        page_label = str(target_page + 1)
        toc_page.insert_text(
            fitz.Point(toc_page.rect.width - 72, rect.y0 + 9),
            page_label,
            fontsize=9,
            fontname="helv",
            color=(0.42, 0.45, 0.48),
        )
        links_added += 1

    doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()
    return links_added


def generate_merged_pdf(pdf_path: Path | None = None) -> Path:
    sequence = _available_sequence()
    if pdf_path is None:
        pdf_path = OUTPUT_PDF

    html = build_merged_html(sequence)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(html)
        html_path = Path(tmp.name)

    header, footer = _header_footer_templates()

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

    links = _annotate_toc_links(pdf_path, sequence)
    print(f"  Annotated {links} TOC links ({len(sequence)} sections)")

    return pdf_path


def validate_merged_pdf(pdf_path: Path, sequence: list[dict[str, str]] | None = None) -> dict[str, object]:
    sequence = sequence or _available_sequence()
    doc = fitz.open(pdf_path)
    try:
        full_text = "\n".join(page.get_text() for page in doc)
        link_count = 0
        resolved = 0
        image_count = 0
        blank: list[int] = []
        for i, page in enumerate(doc):
            image_count += len(page.get_images(full=True))
            if _is_blank_page(page):
                blank.append(i + 1)
            for link in page.get_links():
                if link.get("kind") == fitz.LINK_GOTO:
                    link_count += 1
                    dest_page = link.get("page")
                    if dest_page is not None and 0 <= dest_page < len(doc):
                        resolved += 1

        auth_hits = AUTH_BYPASS_PATTERNS.findall(full_text)
        screenshot_errors: list[str] = []
        if "Screenshot not found:" in full_text:
            screenshot_errors.append("missing screenshot file")
        if re.search(r"Unable to load[^\n]{0,60}\.(png|jpe?g|webp)", full_text, re.IGNORECASE):
            screenshot_errors.append("broken screenshot embed")

        return {
            "page_count": len(doc),
            "file_size": pdf_path.stat().st_size,
            "image_count": image_count,
            "toc_link_count": link_count,
            "toc_links_resolved": resolved,
            "toc_links_expected": len(sequence),
            "blank_pages": blank,
            "auth_bypass_hits": auth_hits,
            "auth_bypass_ok": len(auth_hits) == 0,
            "screenshot_errors": screenshot_errors,
            "screenshots_ok": len(screenshot_errors) == 0,
            "links_ok": link_count >= len(sequence) and resolved == link_count,
        }
    finally:
        doc.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge business documentation into one user guide PDF")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate existing merged PDF without regenerating",
    )
    parser.add_argument(
        "--also-regenerate-individual",
        action="store_true",
        help="Regenerate all individual business PDFs before merging",
    )
    args = parser.parse_args(argv)

    sequence = _available_sequence()
    if not sequence:
        print(f"ERROR: no markdown in {DOC_DIR}", file=sys.stderr)
        return 1

    print(f"Sections available: {len(sequence)}/{len(MERGE_SEQUENCE)}")

    if args.also_regenerate_individual:
        print("Regenerating individual PDFs first...")
        rc = __import__("generate_business_doc_pdf", fromlist=["main"]).main([])
        if rc != 0:
            return rc

    if not args.validate_only:
        print(f"Building merged PDF → {OUTPUT_PDF}")
        generate_merged_pdf()
        size = OUTPUT_PDF.stat().st_size
        print(f"  ✓ {OUTPUT_PDF.name} ({size:,} bytes)")

    if not OUTPUT_PDF.exists():
        print(f"ERROR: merged PDF not found at {OUTPUT_PDF}", file=sys.stderr)
        return 1

    result = validate_merged_pdf(OUTPUT_PDF, sequence)
    shots = save_validation_screenshots(OUTPUT_PDF, VALIDATION_DIR)
    # Sample individual guide pages for visual QA
    for spec in sequence[:3]:
        ind_pdf = DOC_DIR / Path(spec["file"]).with_suffix(".pdf").name
        if ind_pdf.exists():
            shots.extend(save_validation_screenshots(ind_pdf, VALIDATION_DIR))

    print("\nValidation:")
    print(f"  Pages: {result['page_count']}")
    print(f"  Size: {result['file_size']:,} bytes")
    print(f"  Embedded images: {result['image_count']}")
    print(f"  TOC links: {result['toc_link_count']} ({result['toc_links_resolved']} resolved)")
    print(f"  Blank pages: {result['blank_pages'] or 'none'}")
    print(f"  Auth bypass text: {'PASS' if result['auth_bypass_ok'] else 'FAIL — ' + str(result['auth_bypass_hits'])}")
    print(f"  Screenshot errors: {'none' if result['screenshots_ok'] else result['screenshot_errors']}")
    print(f"  TOC links OK: {'PASS' if result['links_ok'] else 'FAIL'}")
    print(f"  Screenshots: {', '.join(p.name for p in shots)}")

    ok = (
        bool(result["auth_bypass_ok"])
        and bool(result["links_ok"])
        and bool(result["screenshots_ok"])
        and not result["blank_pages"]
        and result["image_count"] > 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
