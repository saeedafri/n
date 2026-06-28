#!/usr/bin/env python3
"""Generate professional PDFs from docs/technical-documentation/*.md.

Pipeline: Markdown → pre-render Mermaid (mmdc) → branded HTML → Playwright PDF.

Usage:
    python scripts/generate_technical_doc_pdf.py              # all docs
    python scripts/generate_technical_doc_pdf.py screening.md # one file
    python scripts/generate_technical_doc_pdf.py --verify screening.pdf
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from html import escape, unescape
from pathlib import Path

import fitz  # pymupdf
import markdown
from markdown.extensions.tables import TableExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.sane_lists import SaneListExtension
from markdown.extensions.nl2br import Nl2BrExtension
from markdown.extensions.toc import TocExtension
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
DOC_DIR = REPO / "docs" / "technical-documentation"
ASSETS_DIR = DOC_DIR / "_assets"
LOGO_PATH = ASSETS_DIR / "coresight-logo.png"
TEMPLATE_PATH = DOC_DIR / "_pdf_template.html"
STYLES_PATH = DOC_DIR / "_pdf_styles.css"
MERMAID_CONFIG_PATH = DOC_DIR / "_mermaid_config.json"
MERMAID_CSS_PATH = DOC_DIR / "_mermaid_theme.css"
DIAGRAM_CACHE_DIR = DOC_DIR / "_diagram_cache"
DIAGRAM_MAX_HEIGHT_PX = 420

MERMAID_FENCE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
HEADING_ID_RE = re.compile(r'<h([123])\s+id="([^"]+)"[^>]*>(.*?)</h\1>', re.DOTALL)
TOC_PAGE_MARKER = "Table of Contents"
HEADER_MARGIN_PT = 48.0
FOOTER_MARGIN_PT = 60.0


def _is_content_page(page: fitz.Page) -> bool:
    """True when page has body paragraphs (not cover/TOC)."""
    text = page.get_text()
    if TOC_PAGE_MARKER in text:
        return False
    long_lines = [ln for ln in text.splitlines() if len(ln.strip()) > 90]
    return len(long_lines) >= 2

# All 22 source documents
ALL_DOCS = [
    "README.md",
    "00-app-bootstrap-and-auth.md",
    "01-shared-components.md",
    "02-core-infrastructure.md",
    "03-data-layer.md",
    "04-utils-and-caching.md",
    "login.md",
    "logout-bridge.md",
    "home.md",
    "market-data.md",
    "newsroom.md",
    "earnings-calls.md",
    "live-earnings-transcript.md",
    "earnings-calendar.md",
    "screening.md",
    "company-filings.md",
    "company-filings-add-files.md",
    "logs.md",
    "retailer-adding.md",
    "forecasting.md",
    "forecasting-admin.md",
    "access-management.md",
]

MMDC_BIN = shutil.which("mmdc")
if not MMDC_BIN:
    _npx = shutil.which("npx")
    MMDC_CMD = [_npx, "@mermaid-js/mermaid-cli"] if _npx else None
else:
    MMDC_CMD = [MMDC_BIN]


def _slugify(text: str, _separator: str = "-") -> str:
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")


def _logo_data_uri() -> str:
    if LOGO_PATH.exists():
        encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    # Fallback wordmark SVG
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="40">'
        '<text x="0" y="30" font-family="Inter,sans-serif" font-size="22" '
        'font-weight="700" fill="#D62E2F">CORESIGHT RESEARCH</text></svg>'
    )
    encoded = base64.b64encode(svg.encode()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _extract_title(md_text: str, md_path: Path) -> str:
    for line in md_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return md_path.stem.replace("-", " ").title()


def _extract_headings(md_text: str) -> list[tuple[int, str, str]]:
    headings: list[tuple[int, str, str]] = []
    for match in HEADING_RE.finditer(md_text):
        level = len(match.group(1))
        title = match.group(2).strip()
        anchor = _slugify(title)
        headings.append((level, title, anchor))
    return headings


def _extract_heading_ids_from_html(html: str) -> list[tuple[int, str, str]]:
    """Return (level, title, anchor) using ids assigned by TocExtension."""
    headings: list[tuple[int, str, str]] = []
    for match in HEADING_ID_RE.finditer(html):
        level = int(match.group(1))
        anchor = match.group(2)
        title = unescape(re.sub(r"<[^>]+>", "", match.group(3)).strip())
        if title:
            headings.append((level, title, anchor))
    return headings


def _toc_headings(headings: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    """TOC includes document title (H1) and section headings (H2)."""
    return [(level, title, anchor) for level, title, anchor in headings if level <= 2]


def _search_terms_for_title(title: str) -> list[str]:
    """Build progressively relaxed search strings for PDF text matching."""
    plain = unescape(title)
    terms: list[str] = [plain]
    collapsed = re.sub(r"\s+", " ", plain).strip()
    if collapsed not in terms:
        terms.append(collapsed)
    # PDF text extraction often inserts spaces inside parentheses from inline code.
    for variant in (
        collapsed.replace("(", "( ").replace(")", " )"),
        re.sub(r"\s*\(\s*", " (", collapsed),
    ):
        cleaned = re.sub(r"\s+", " ", variant).strip()
        if cleaned not in terms:
            terms.append(cleaned)
    section_prefix = re.match(r"^(\d+\.\s+\S+(?:\s+\S+){0,5})", collapsed)
    if section_prefix:
        terms.append(section_prefix.group(1).strip())
    if "(" in collapsed:
        before_paren = collapsed.split("(", 1)[0].strip()
        if len(before_paren) > 6 and before_paren not in terms:
            terms.append(before_paren)
    for size in (55, 40, 30):
        if len(collapsed) > size:
            prefix = collapsed[:size].rsplit(" ", 1)[0].strip()
            if len(prefix) > 8 and prefix not in terms:
                terms.append(prefix)
    terms.sort(key=len, reverse=True)
    return terms


def _page_has_heading_hit(page: fitz.Page, title: str) -> bool:
    for term in _search_terms_for_title(title):
        if _body_search_hits(page, term):
            return True
    return False


def _body_search_hits(page: fitz.Page, text: str) -> list[fitz.Rect]:
    """Search page text excluding Playwright header/footer bands."""
    page_h = page.rect.height
    return [
        rect
        for rect in page.search_for(text)
        if rect.y0 >= HEADER_MARGIN_PT and rect.y1 <= page_h - FOOTER_MARGIN_PT
    ]


def _find_content_start_page(doc: fitz.Document, headings: list[tuple[int, str, str]]) -> int:
    """First body page where the initial H2 section begins (after TOC)."""
    h2_titles = [title for level, title, _ in headings if level == 2]
    if not h2_titles:
        return min(2, max(len(doc) - 1, 1))
    first_h2 = h2_titles[0]
    for page_idx in range(1, len(doc)):
        page = doc[page_idx]
        if not _is_content_page(page):
            continue
        if _page_has_heading_hit(page, first_h2):
            return page_idx
    return 2


def _find_heading_pages(
    doc: fitz.Document,
    headings: list[tuple[int, str, str]],
) -> dict[str, int]:
    """Map heading anchor slug → 0-based PDF page index."""
    content_start = _find_content_start_page(doc, headings)
    slug_pages: dict[str, int] = {}
    min_next_page = content_start

    for level, title, anchor in _toc_headings(headings):
        start_page = 0 if level == 1 else min_next_page
        found: int | None = None
        for page_idx in range(start_page, len(doc)):
            if level >= 2 and page_idx < content_start:
                continue
            if _page_has_heading_hit(doc[page_idx], title):
                found = page_idx
                break
        if found is None and level == 1:
            found = 0
        if found is not None:
            slug_pages[anchor] = found
            if level >= 2:
                min_next_page = found
    return slug_pages


def _toc_search_hits(page: fitz.Page, title: str) -> list[fitz.Rect]:
    for term in _search_terms_for_title(title):
        hits = page.search_for(term)
        if hits:
            return hits
    return []


def _annotate_toc_links(pdf_path: Path, headings: list[tuple[int, str, str]]) -> int:
    """Add internal PDF link annotations from TOC entries to section pages."""
    doc = fitz.open(pdf_path)
    try:
        if len(doc) < 2:
            return 0

        slug_pages = _find_heading_pages(doc, headings)
        content_start = _find_content_start_page(doc, headings)
        toc_pages = list(range(1, content_start))
        links_added = 0

        for _level, title, anchor in _toc_headings(headings):
            target_page = slug_pages.get(anchor)
            if target_page is None:
                continue

            for toc_idx in toc_pages:
                toc_page = doc[toc_idx]
                hits = _toc_search_hits(toc_page, title)
                if not hits:
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
                break

        doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        return links_added
    finally:
        doc.close()


def validate_toc_links(pdf_path: Path, headings: list[tuple[int, str, str]]) -> dict[str, object]:
    """Validate internal TOC links resolve to real pages."""
    doc = fitz.open(pdf_path)
    try:
        expected = len(_toc_headings(headings))
        slug_pages = _find_heading_pages(doc, headings)
        link_count = 0
        resolved = 0
        for page in doc:
            for link in page.get_links():
                if link.get("kind") != fitz.LINK_GOTO:
                    continue
                link_count += 1
                dest_page = link.get("page")
                if dest_page is not None and 0 <= dest_page < len(doc):
                    resolved += 1
        return {
            "expected": expected,
            "mapped_headings": len(slug_pages),
            "link_count": link_count,
            "resolved": resolved,
            "ok": link_count >= expected and resolved == link_count and len(slug_pages) >= expected,
        }
    finally:
        doc.close()


def _caption_before_block(md_text: str, block_start: int) -> str:
    """Use preceding non-empty line as diagram caption when it looks descriptive."""
    prefix = md_text[:block_start].rstrip()
    lines = prefix.splitlines()
    for line in reversed(lines[-6:]):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped.startswith("```"):
            break
        if len(stripped) > 20 and not stripped.startswith("|"):
            clean = re.sub(r"[*_`]", "", stripped)
            return clean[:180]
    return "Architecture diagram"


def _sanitize_mermaid(src: str) -> str:
    """Fix Mermaid syntax that mmdc cannot parse (special chars, shape delimiters)."""
    lines: list[str] = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("classDef ") or stripped.startswith("class "):
            if lines and lines[-1].strip() and not lines[-1].strip().startswith("class"):
                lines.append("")
        lines.append(line)
    text = "\n".join(lines)

    def _quote_bracket_label(match: re.Match[str]) -> str:
        node_id, label = match.group(1), match.group(2)
        if label.startswith('"') and label.endswith('"'):
            return match.group(0)
        # Mermaid cylinder/database shape: NODE[(label)] — do not re-quote
        if label.startswith("(") and label.endswith(")"):
            return match.group(0)
        if re.search(r"[@/?{}]|^/|—", label):
            safe = label.replace('"', "'")
            return f'{node_id}["{safe}"]'
        return match.group(0)

    def _quote_diamond_label(match: re.Match[str]) -> str:
        node_id, label = match.group(1), match.group(2)
        if label.startswith('"') and label.endswith('"'):
            return match.group(0)
        if re.search(r"[@/?{}]|^/|—", label):
            safe = label.replace('"', "'")
            return f'{node_id}{{"{safe}"}}'
        return match.group(0)

    text = re.sub(r"(\b[A-Za-z0-9_]+)\[([^\]\"]+)\]", _quote_bracket_label, text)
    text = re.sub(r"(\b[A-Za-z0-9_]+)\{([^}\"]+)\}", _quote_diamond_label, text)
    return text


MERMAID_CLASS_DEFS = """
classDef ui fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
classDef db fill:#fff3e0,stroke:#ef6c00,color:#e65100
classDef external fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
classDef process fill:#e8f4fd,stroke:#0066CC,color:#1a365d
classDef error fill:#ffebee,stroke:#c62828,color:#b71c1c
""".strip()

_UI_HINTS = re.compile(
    r"\b(ui|page|streamlit|component|frontend|browser|portal|view|screen|widget)\b",
    re.IGNORECASE,
)
_DB_HINTS = re.compile(
    r"\b(db|database|mysql|sql|table|repository|store|cache|redis)\b",
    re.IGNORECASE,
)
_EXTERNAL_HINTS = re.compile(
    r"\b(api|external|oidc|azure|blob|smtp|idp|webhook|http|rest|oauth|s3)\b",
    re.IGNORECASE,
)
_ERROR_HINTS = re.compile(
    r"\b(error|fail|invalid|deny|reject|exception|timeout|retry)\b",
    re.IGNORECASE,
)
_NODE_BRACKET = re.compile(r"(\b[A-Za-z0-9_]+)\[([^\]]*)\]")
_NODE_DIAMOND = re.compile(r"(\b[A-Za-z0-9_]+)\{([^}]*)\}")
_NODE_ROUND = re.compile(r"(\b[A-Za-z0-9_]+)\(([^)]*)\)")


def _mermaid_node_class(node_id: str, label: str, shape: str) -> str:
    haystack = f"{node_id} {label}"
    if shape == "diamond" or _ERROR_HINTS.search(haystack):
        return "error"
    if _DB_HINTS.search(haystack):
        return "db"
    if _EXTERNAL_HINTS.search(haystack):
        return "external"
    if _UI_HINTS.search(haystack):
        return "ui"
    return "process"


def _enhance_mermaid_styling(src: str) -> str:
    """Inject multi-color classDefs and auto-assign node classes when absent."""
    text = src.strip()
    # classDef/class only valid on flowchart/graph diagrams — not sequence, gantt, etc.
    if re.search(
        r"^\s*(sequenceDiagram|gantt|pie|gitGraph|journey|C4Context|mindmap|timeline)\b",
        text,
        re.MULTILINE | re.IGNORECASE,
    ):
        return text
    if "classDef ui" not in text:
        text = f"{text}\n\n{MERMAID_CLASS_DEFS}"

    existing_class_nodes: set[str] = set()
    for match in re.finditer(r"^class\s+([^\n]+)$", text, re.MULTILINE):
        body = match.group(1).strip()
        if " " not in body:
            continue
        nodes_part, _cls = body.rsplit(" ", 1)
        existing_class_nodes.update(n.strip() for n in nodes_part.split(",") if n.strip())

    assigned: dict[str, str] = {}
    for match in _NODE_DIAMOND.finditer(text):
        node_id, label = match.group(1), match.group(2)
        assigned[node_id] = _mermaid_node_class(node_id, label, "diamond")
    for match in _NODE_BRACKET.finditer(text):
        node_id, label = match.group(1), match.group(2)
        assigned.setdefault(node_id, _mermaid_node_class(node_id, label, "rect"))
    for match in _NODE_ROUND.finditer(text):
        node_id, label = match.group(1), match.group(2)
        assigned.setdefault(node_id, _mermaid_node_class(node_id, label, "round"))

    if not assigned:
        return text

    by_class: dict[str, list[str]] = {}
    for node_id, cls in assigned.items():
        if node_id in existing_class_nodes:
            continue
        by_class.setdefault(cls, []).append(node_id)

    if by_class:
        class_lines = [
            f"class {','.join(nodes)} {cls}"
            for cls, nodes in by_class.items()
            if nodes
        ]
        text = f"{text}\n" + "\n".join(class_lines)
    return text


def _render_mermaid_svg(mermaid_src: str, cache_key: str) -> Path:
    DIAGRAM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIAGRAM_CACHE_DIR / f"{cache_key}.svg"
    if out_path.exists() and out_path.stat().st_size > 100:
        return out_path

    if not MMDC_CMD:
        raise RuntimeError("mermaid-cli not found — install via: npx @mermaid-js/mermaid-cli")

    styled_src = _enhance_mermaid_styling(_sanitize_mermaid(mermaid_src))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".mmd", delete=False) as tmp:
        tmp.write(styled_src)
        mmd_path = Path(tmp.name)

    try:
        cmd = [
            *MMDC_CMD,
            "-i", str(mmd_path),
            "-o", str(out_path),
            "-b", "transparent",
            "--scale", "2",
            "--width", "1200",
        ]
        if MERMAID_CONFIG_PATH.exists():
            cmd.extend(["-c", str(MERMAID_CONFIG_PATH)])
        if MERMAID_CSS_PATH.exists():
            cmd.extend(["-C", str(MERMAID_CSS_PATH)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(f"mmdc failed: {err[:500]}")
    finally:
        mmd_path.unlink(missing_ok=True)

    if not out_path.exists():
        raise RuntimeError(f"mmdc did not produce {out_path}")
    return out_path


def _svg_to_data_uri(svg_path: Path) -> str:
    content = svg_path.read_bytes()
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _replace_mermaid_blocks(md_text: str, doc_stem: str) -> str:
    diagram_idx = 0

    def replacer(match: re.Match[str]) -> str:
        nonlocal diagram_idx
        diagram_idx += 1
        src = match.group(1).strip()
        caption = _caption_before_block(md_text, match.start())
        cache_key = hashlib.sha256(f"{doc_stem}:{diagram_idx}:{src}".encode()).hexdigest()[:16]
        svg_path = _render_mermaid_svg(src, cache_key)
        data_uri = _svg_to_data_uri(svg_path)
        return (
            f'\n<div class="diagram-block">'
            f'<p class="diagram-caption">{escape(caption)}</p>'
            f'<figure class="diagram">'
            f'<img src="{data_uri}" alt="{escape(caption)}" class="diagram-image"/>'
            f"</figure>"
            f"</div>\n"
        )

    return MERMAID_FENCE.sub(replacer, md_text)


def _wrap_content_sections(html: str) -> str:
    """Wrap h2/h3-led blocks so headings stay with their opening content."""
    if not html.strip():
        return html

    parts = re.split(r"(?=<h[23][\s>])", html)
    wrapped: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if re.match(r"<h2[\s>]", part):
            wrapped.append(f'<div class="section-block section-h2">{part}</div>')
        elif re.match(r"<h3[\s>]", part):
            wrapped.append(f'<div class="section-block section-h3">{part}</div>')
        else:
            wrapped.append(part)
    return "".join(wrapped)


def _build_toc_html(headings: list[tuple[int, str, str]]) -> str:
    toc_entries = _toc_headings(headings)
    if not toc_entries:
        return "<p><em>No sections</em></p>"

    items: list[str] = []
    for level, title, anchor in toc_entries:
        cls = f"toc-h{level}"
        items.append(
            f'<li class="{cls}">'
            f'<a href="#{escape(anchor)}">'
            f'<span class="toc-entry-text">{escape(title)}</span>'
            f'<span class="toc-leader"></span>'
            f'<span class="toc-page-num"></span>'
            f"</a>"
            f"</li>"
        )
    return f'<ul class="toc-list">{"".join(items)}</ul>'


def _md_to_html(md_text: str) -> str:
    return markdown.markdown(
        md_text,
        extensions=[
            TableExtension(),
            FencedCodeExtension(),
            SaneListExtension(),
            Nl2BrExtension(),
            TocExtension(permalink=False, slugify=_slugify),
        ],
    )


def _build_html(md_path: Path) -> tuple[str, str, list[tuple[int, str, str]]]:
    md_text = md_path.read_text(encoding="utf-8")
    title = _extract_title(md_text, md_path)

    processed = _replace_mermaid_blocks(md_text, md_path.stem)
    content_html = _wrap_content_sections(_md_to_html(processed))
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
    return html, title, headings


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


def generate_pdf(md_path: Path, pdf_path: Path | None = None) -> tuple[Path, int, list[tuple[int, str, str]]]:
    if pdf_path is None:
        pdf_path = md_path.with_suffix(".pdf")

    html, title, headings = _build_html(md_path)

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
                    } else if (naturalHeight > maxHeight * 0.82 && block) {
                      block.classList.add('diagram-oversized');
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

    links_added = _annotate_toc_links(pdf_path, headings)
    return pdf_path, links_added, headings


MERMAID_FENCE_COUNT = re.compile(r"```mermaid\s*\n", re.MULTILINE)
STRICT_MERMAID_LEAK = re.compile(
    r"\bsubgraph\s+\w|\bclassDef\s+\w|\bparticipant\s+\w+\s+as\s|```mermaid",
    re.IGNORECASE,
)
# Playwright renders Mermaid SVGs as vector paths, not raster images. Header logos
# are the only raster images on most pages — require vector-heavy pages instead.
MIN_VECTOR_PATHS_FOR_DIAGRAM = 50


def _count_mermaid_blocks(md_path: Path) -> int:
    if not md_path.exists():
        return 0
    return len(MERMAID_FENCE_COUNT.findall(md_path.read_text(encoding="utf-8")))


def verify_pdf(pdf_path: Path, md_path: Path | None = None) -> dict[str, object]:
    """Validate PDF: rendered diagrams (vector paths), no raw Mermaid syntax leak."""
    import fitz  # pymupdf

    if md_path is None:
        md_path = pdf_path.with_suffix(".md")

    expected_mermaid = _count_mermaid_blocks(md_path)
    doc = fitz.open(pdf_path)
    try:
        image_count = 0
        diagram_pages = 0
        full_text: list[str] = []
        for page in doc:
            image_count += len(page.get_images(full=True))
            if len(page.get_drawings()) >= MIN_VECTOR_PATHS_FOR_DIAGRAM:
                diagram_pages += 1
            full_text.append(page.get_text())
        text = "\n".join(full_text)
        raw_mermaid_leaked = bool(STRICT_MERMAID_LEAK.search(text))
        has_diagrams = diagram_pages > 0 if expected_mermaid > 0 else True
        ok = has_diagrams and not raw_mermaid_leaked
        return {
            "ok": ok,
            "expected_mermaid": expected_mermaid,
            "image_count": image_count,
            "diagram_pages": diagram_pages,
            "raw_mermaid_leaked": raw_mermaid_leaked,
            "page_count": len(doc),
        }
    finally:
        doc.close()


def verify_pdf_has_diagrams(pdf_path: Path) -> bool:
    """Return True if PDF passes diagram verification (see verify_pdf)."""
    return bool(verify_pdf(pdf_path).get("ok"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate technical documentation PDFs")
    parser.add_argument(
        "targets",
        nargs="*",
        help="Markdown filenames or PDF paths to verify (default: all 21 docs)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify PDF contains rendered diagram images",
    )
    args = parser.parse_args(argv)

    if args.verify:
        targets = args.targets or ["screening.pdf"]
        ok = True
        for t in targets:
            pdf = DOC_DIR / t if not Path(t).is_absolute() else Path(t)
            if not pdf.exists():
                print(f"{pdf.name}: MISSING", file=sys.stderr)
                ok = False
                continue
            result = verify_pdf(pdf)
            size = pdf.stat().st_size
            if result["ok"]:
                status = (
                    f"OK (mermaid={result['expected_mermaid']}, "
                    f"diagram_pages={result['diagram_pages']}, "
                    f"images={result['image_count']})"
                )
            else:
                reasons = []
                if result["expected_mermaid"] and not result["diagram_pages"]:
                    reasons.append("no rendered diagram pages")
                if result["raw_mermaid_leaked"]:
                    reasons.append("raw mermaid syntax in body")
                status = "FAIL (" + "; ".join(reasons) + ")"
            print(f"{pdf.name}: {size:,} bytes — {status}")
            ok = ok and bool(result["ok"])
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
        md_files = [DOC_DIR / name for name in ALL_DOCS]

    missing = [p for p in md_files if not p.exists()]
    if missing:
        for p in missing:
            print(f"ERROR: missing {p}", file=sys.stderr)
        return 1

    print(f"Generating {len(md_files)} PDF(s) → {DOC_DIR}")
    print(f"Mermaid CLI: {' '.join(MMDC_CMD or ['NOT FOUND'])}")

    ok_count = 0
    for md_path in md_files:
        try:
            pdf_path, _toc_links, headings = generate_pdf(md_path)
            size = pdf_path.stat().st_size
            vresult = verify_pdf(pdf_path, md_path)
            toc_result = validate_toc_links(pdf_path, headings)
            diag_note = (
                f"diagrams OK ({vresult['diagram_pages']} pages)"
                if vresult["ok"]
                else "VERIFY FAILED"
            )
            toc_note = (
                f"TOC links {toc_result['link_count']}/{toc_result['expected']}"
                if toc_result["ok"]
                else f"TOC FAIL ({toc_result['link_count']}/{toc_result['expected']})"
            )
            print(
                f"  ✓ {pdf_path.name} ({size:,} bytes, {diag_note}, {toc_note})"
            )
            ok_count += 1
        except Exception as exc:
            print(f"  ✗ {md_path.name}: {exc}", file=sys.stderr)

    print(f"\nDone: {ok_count}/{len(md_files)} succeeded")
    return 0 if ok_count == len(md_files) else 1


if __name__ == "__main__":
    sys.exit(main())
