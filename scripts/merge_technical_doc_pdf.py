#!/usr/bin/env python3
"""Build production merged technical documentation PDF from markdown sources.

Pipeline: all docs → single branded HTML (cover + master TOC + section dividers)
→ Playwright PDF.

Usage:
    python scripts/merge_technical_doc_pdf.py
    python scripts/merge_technical_doc_pdf.py --validate-only
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

from generate_technical_doc_pdf import (  # noqa: E402
    DOC_DIR,
    STYLES_PATH,
    _extract_title,
    _logo_data_uri,
    _md_to_html,
    _replace_mermaid_blocks,
    _wrap_content_sections,
)
from sync_technical_pdfs_sharepoint import sync_technical_sharepoint  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OUTPUT_PDF = DOC_DIR / "_pdf" / "Market-Data-Portal-Technical-Documentation-Complete.pdf"
VALIDATION_DIR = DOC_DIR / "_validation-merged"

MERGED_TITLE = "Market Data Portal (MDP) — Complete Technical Documentation"

MERGE_SEQUENCE: list[dict[str, str]] = [
    {
        "file": "README.md",
        "slug": "readme",
        "description": "Master index — architecture overview, authentication summary, page registry, and warmup flags.",
    },
    {
        "file": "00-infrastructure/00-app-bootstrap-and-auth.md",
        "slug": "app-bootstrap-and-auth",
        "description": "Application entry point, OIDC bootstrap, cookie hydration, and per-page auth enforcement.",
    },
    {
        "file": "00-infrastructure/01-shared-components.md",
        "slug": "shared-components",
        "description": "Shared Streamlit UI — navigation, charts, tables, styles, and toolbar patterns.",
    },
    {
        "file": "00-infrastructure/02-core-infrastructure.md",
        "slug": "core-infrastructure",
        "description": "Core modules — configuration, database engines, auth manager, access control, and LLM extraction.",
    },
    {
        "file": "00-infrastructure/03-data-layer.md",
        "slug": "data-layer",
        "description": "Data models, repositories, screening services, and forecast pipelines.",
    },
    {
        "file": "00-infrastructure/04-utils-and-caching.md",
        "slug": "utils-and-caching",
        "description": "Logging, caching, Azure Blob, PDF generation, email dispatch, and background warmups.",
    },
    {
        "file": "pages/login.md",
        "slug": "login",
        "description": "OIDC PKCE login flow, token exchange, and cookie handoff to Home.",
    },
    {
        "file": "pages/logout-bridge.md",
        "slug": "logout-bridge",
        "description": "RP-initiated OIDC logout and IdP session termination.",
    },
    {
        "file": "pages/home.md",
        "slug": "home",
        "description": "Landing dashboard, company picker, and background filings scan initialization.",
    },
    {
        "file": "pages/market-data.md",
        "slug": "market-data",
        "description": "Financial statements, key stats, estimates, and cross-page ticker synchronization.",
    },
    {
        "file": "pages/newsroom.md",
        "slug": "newsroom",
        "description": "News feed with sentiment scoring, ticker filters, and sector browsing.",
    },
    {
        "file": "pages/earnings-calls.md",
        "slug": "earnings-calls",
        "description": "Earnings transcript browser with TF-IDF search and PDF download.",
    },
    {
        "file": "pages/live-earnings-transcript.md",
        "slug": "live-earnings-transcript",
        "description": "Live audio transcription pipeline and earnings call ingestion.",
    },
    {
        "file": "pages/earnings-calendar.md",
        "slug": "earnings-calendar",
        "description": "FullCalendar earnings schedule with watchlists and email alerts.",
    },
    {
        "file": "pages/screening.md",
        "slug": "screening",
        "description": "Multi-criteria financial screener with saved criteria and watchlists.",
    },
    {
        "file": "pages/company-filings.md",
        "slug": "company-filings",
        "description": "SEC filing browser with XBRL metrics and Azure blob PDF retrieval.",
    },
    {
        "file": "pages/company-filings-add-files.md",
        "slug": "company-filings-add-files",
        "description": "Admin upload tool for non-SEC filings to Azure Blob Storage.",
    },
    {
        "file": "pages/logs.md",
        "slug": "logs",
        "description": "Hidden operations surface — server log viewer and segment cache admin.",
    },
    {
        "file": "pages/retailer-adding.md",
        "slug": "retailer-adding",
        "description": "Retail company master data management with ACL gating and audit trail.",
    },
    {
        "file": "pages/forecasting.md",
        "slug": "forecasting",
        "description": "Revenue forecast viewer, refresh workflows, and quarterly estimates.",
    },
    {
        "file": "pages/access-management.md",
        "slug": "access-management",
        "description": "IAM console for system roles and page-level access control.",
    },
    {
        "file": "pages/forecasting-admin.md",
        "slug": "forecasting-admin",
        "description": "Unregistered appendix — forecast model administration and bulk refresh.",
    },
]

AUTH_BYPASS_PATTERNS = re.compile(
    r"APP_ENV=LOCAL|LOCAL_TEST_USER_EMAIL|run_local\.sh|"
    r"OIDC bypass|auth bypass|LOCAL\+DEBUG|LOCAL testing|Known dev pattern",
    re.IGNORECASE,
)

MERGED_EXTRA_CSS = """
/* ── Merged document: section dividers & end markers ── */
.section-divider-page {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: calc(100vh - 1.7in);
  padding: 0.5in 0.25in;
  page-break-before: always;
  break-before: page;
  page-break-after: always;
  break-after: page;
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
  page-break-after: always;
  break-after: page;
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
  font-size: 11pt;
  margin: 10pt 0;
}

.merged-toc .toc-doc-entry a {
  color: var(--cs-primary-dark);
  text-decoration: none;
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
"""


def _build_master_cover(generated: str) -> str:
    return f"""
  <section class="cover-page merged-cover">
    <div class="cover-top">
      <img class="cover-logo" src="{_logo_data_uri()}" alt="Coresight Research">
      <h1 class="cover-title">{escape(MERGED_TITLE)}</h1>
      <p class="cover-subtitle">Coresight Research Portal — Engineering Reference</p>
      <div class="cover-meta">
        <p><strong>Document version:</strong> 1.0</p>
        <p><strong>Generated:</strong> {escape(generated)}</p>
        <p><strong>Classification:</strong> Confidential — Internal Use Only</p>
        <p><strong>Sections:</strong> {len(MERGE_SEQUENCE)} documents</p>
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
            f'<span class="toc-part">Part {part}</span>'
            f'<a href="#doc-{escape(slug)}">{escape(title)}</a>'
            f"</li>"
        )
    return f"""
  <section class="toc-page merged-toc">
    <h2>Table of Contents</h2>
    <ul class="toc-list">{"".join(items)}</ul>
  </section>
"""


def _build_section_divider(part: int, total: int, title: str, description: str, slug: str) -> str:
    return f"""
  <section class="section-divider-page" id="doc-{escape(slug)}">
    <div class="divider-accent"></div>
    <p class="divider-part">Part {part} of {total}</p>
    <h2 class="divider-title">{escape(title)}</h2>
    <p class="divider-desc">{escape(description)}</p>
    <footer class="divider-footer">Continued from previous section →</footer>
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
    return _wrap_content_sections(_md_to_html(processed))


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


def build_merged_html() -> str:
    generated = datetime.now(timezone.utc).strftime("%B %d, %Y")
    total = len(MERGE_SEQUENCE)
    css = STYLES_PATH.read_text(encoding="utf-8") + MERGED_EXTRA_CSS

    toc_entries: list[tuple[int, str, str, str]] = []
    body_parts: list[str] = [_build_master_cover(generated)]

    for idx, spec in enumerate(MERGE_SEQUENCE, start=1):
        md_path = DOC_DIR / spec["file"]
        title = _extract_title(md_path.read_text(encoding="utf-8"), md_path)
        toc_entries.append((idx, title, spec["slug"], spec["description"]))

    body_parts.append(_build_master_toc(toc_entries))

    for idx, spec in enumerate(MERGE_SEQUENCE, start=1):
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


def _find_section_pages(doc: fitz.Document, total: int) -> dict[str, int]:
    """Map doc slug → 0-based page index by scanning divider and README markers."""
    slug_pages: dict[str, int] = {}
    for page_idx in range(len(doc)):
        text = doc[page_idx].get_text()
        text_lower = text.lower()
        for spec in MERGE_SEQUENCE:
            slug = spec["slug"]
            if slug in slug_pages:
                continue
            part = MERGE_SEQUENCE.index(spec) + 1
            if spec["file"] == "README.md":
                if "how to read these docs" in text_lower and "market data portal (mdp)" in text_lower:
                    slug_pages[slug] = page_idx
            elif f"part {part} of {total}" in text_lower and "continued from previous section" in text_lower:
                slug_pages[slug] = page_idx
    return slug_pages


def _annotate_toc_links(pdf_path: Path) -> int:
    """Add internal PDF link annotations from master TOC entries to section pages."""
    doc = fitz.open(pdf_path)
    total = len(MERGE_SEQUENCE)
    slug_pages = _find_section_pages(doc, total)
    if len(slug_pages) < total:
        missing = [s["slug"] for s in MERGE_SEQUENCE if s["slug"] not in slug_pages]
        doc.close()
        raise RuntimeError(
            f"Could only locate {len(slug_pages)}/{total} section pages for TOC links; missing: {missing}"
        )

    toc_page_idx = 1
    if toc_page_idx >= len(doc):
        doc.close()
        return 0

    toc_page = doc[toc_page_idx]
    links_added = 0

    for spec in MERGE_SEQUENCE:
        slug = spec["slug"]
        target_page = slug_pages.get(slug)
        if target_page is None:
            continue
        md_path = DOC_DIR / spec["file"]
        title = _extract_title(md_path.read_text(encoding="utf-8"), md_path)
        part = MERGE_SEQUENCE.index(spec) + 1
        # TOC lines: "Part N Title..."
        search_terms = [title, f"Part {part} {title}", title[:50], title[:35]]
        hits: list = []
        for term in search_terms:
            if not term:
                continue
            hits = toc_page.search_for(term)
            if hits:
                break
        if not hits:
            continue
        rect = hits[0]
        link_rect = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1)
        toc_page.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "page": target_page,
                "from": link_rect,
                "to": fitz.Point(0, 0),
            }
        )
        links_added += 1

    doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()
    return links_added


def generate_merged_pdf(pdf_path: Path | None = None) -> Path:
    if pdf_path is None:
        pdf_path = OUTPUT_PDF

    html = build_merged_html()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(html)
        html_path = Path(tmp.name)

    header, footer = _header_footer_templates()
    diagram_max = 420

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
                    } else if (naturalHeight > maxHeight * 0.82 && block) {
                      block.classList.add('diagram-oversized');
                    }
                  });
                }""",
                diagram_max,
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

    links = _annotate_toc_links(pdf_path)
    print(f"  Annotated {links} TOC links")

    return pdf_path


def validate_merged_pdf(pdf_path: Path) -> dict[str, object]:
    doc = fitz.open(pdf_path)
    try:
        full_text = "\n".join(page.get_text() for page in doc)
        link_count = 0
        resolved = 0
        for page in doc:
            for link in page.get_links():
                if link.get("kind") == fitz.LINK_GOTO:
                    link_count += 1
                    dest_page = link.get("page")
                    if dest_page is not None and 0 <= dest_page < len(doc):
                        resolved += 1

        blank = []
        for i, page in enumerate(doc):
            text = page.get_text().strip()
            clean = re.sub(r"(CONFIDENTIAL|Coresight Research|Page \d+ of \d+)", "", text)
            clean = re.sub(r"\s+", "", clean)
            if len(clean) < 100 and len(page.get_images(full=True)) == 0 and len(page.get_drawings()) < 50:
                blank.append(i + 1)

        auth_hits = AUTH_BYPASS_PATTERNS.findall(full_text)

        return {
            "page_count": len(doc),
            "file_size": pdf_path.stat().st_size,
            "toc_link_count": link_count,
            "toc_links_resolved": resolved,
            "toc_links_expected": len(MERGE_SEQUENCE),
            "blank_pages": blank,
            "auth_bypass_hits": auth_hits,
            "auth_bypass_ok": len(auth_hits) == 0,
            "links_ok": link_count >= len(MERGE_SEQUENCE) and resolved == link_count,
        }
    finally:
        doc.close()


def save_validation_screenshots(pdf_path: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    shots: list[Path] = []
    try:
        targets = [
            (0, "cover.png"),
            (1, "toc.png"),
        ]
        # First section divider is typically page 2 or 3 — scan for "Part 2 of"
        divider_page = 2
        for i in range(min(8, len(doc))):
            if "Part 2 of" in doc[i].get_text():
                divider_page = i
                break
        targets.append((divider_page, "section-divider.png"))

        for page_idx, name in targets:
            if page_idx < len(doc):
                pix = doc[page_idx].get_pixmap(matrix=fitz.Matrix(2, 2))
                out = out_dir / name
                pix.save(str(out))
                shots.append(out)
    finally:
        doc.close()
    return shots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge technical documentation into one PDF")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate existing merged PDF without regenerating",
    )
    parser.add_argument(
        "--also-regenerate-individual",
        action="store_true",
        help="Regenerate all 22 individual PDFs before merging",
    )
    args = parser.parse_args(argv)

    if args.also_regenerate_individual:
        print("Regenerating individual PDFs first...")
        rc = __import__("generate_technical_doc_pdf", fromlist=["main"]).main([])
        if rc != 0:
            return rc

    if not args.validate_only:
        print(f"Building merged PDF → {OUTPUT_PDF}")
        generate_merged_pdf()
        size = OUTPUT_PDF.stat().st_size
        print(f"  ✓ {OUTPUT_PDF.name} ({size:,} bytes)")
        sp = sync_technical_sharepoint()
        print(
            f"  SharePoint mirror → {sp['dest'].relative_to(REPO)} "
            f"({sp['infra_count']} infra + {sp['pages_count']} pages + merged)"
        )

    if not OUTPUT_PDF.exists():
        print(f"ERROR: merged PDF not found at {OUTPUT_PDF}", file=sys.stderr)
        return 1

    result = validate_merged_pdf(OUTPUT_PDF)
    shots = save_validation_screenshots(OUTPUT_PDF, VALIDATION_DIR)

    print("\nValidation:")
    print(f"  Pages: {result['page_count']}")
    print(f"  Size: {result['file_size']:,} bytes")
    print(f"  TOC links: {result['toc_link_count']} ({result['toc_links_resolved']} resolved)")
    print(f"  Blank pages: {result['blank_pages'] or 'none'}")
    print(f"  Auth bypass text: {'PASS' if result['auth_bypass_ok'] else 'FAIL — ' + str(result['auth_bypass_hits'])}")
    print(f"  TOC links OK: {'PASS' if result['links_ok'] else 'FAIL'}")
    print(f"  Screenshots: {', '.join(p.name for p in shots)}")

    ok = (
        bool(result["auth_bypass_ok"])
        and bool(result["links_ok"])
        and not result["blank_pages"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
