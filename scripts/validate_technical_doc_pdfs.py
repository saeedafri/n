#!/usr/bin/env python3
"""Validate technical documentation PDFs for production readiness."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import fitz  # pymupdf

REPO = Path(__file__).resolve().parents[1]
DOC_DIR = REPO / "docs" / "technical-documentation"
VALIDATION_TOC_DIR = DOC_DIR / "_validation-toc"
SCRIPTS_DIR = Path(__file__).resolve().parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generate_technical_doc_pdf import (  # noqa: E402
    _build_html,
    validate_toc_links,
)

ALL_PDFS = sorted(DOC_DIR.glob("*.pdf"))
ALL_MDS = sorted(DOC_DIR.glob("*.md"))
ALL_MDS = [p for p in ALL_MDS if not p.name.startswith("_")]

MIN_VECTOR_PATHS = 50
SPLIT_FRAC_THRESHOLD = 0.35
BLANK_CHAR_THRESHOLD = 100
TOC_SAMPLE_PDFS = ("README.pdf", "earnings-calendar.pdf", "screening.pdf")


def _page_text(page: fitz.Page) -> str:
    return page.get_text().strip()


def _has_images(page: fitz.Page) -> bool:
    return len(page.get_images(full=True)) > 0


def _drawing_count(page: fitz.Page) -> int:
    return len(page.get_drawings())


def _drawing_y_ranges(page: fitz.Page) -> list[tuple[float, float]]:
    """Return y-range (top, bottom) for each drawing path cluster."""
    drawings = page.get_drawings()
    if not drawings:
        return []
    ranges: list[tuple[float, float]] = []
    for d in drawings:
        rect = d.get("rect")
        if rect:
            ranges.append((rect.y0, rect.y1))
    return ranges


def detect_bad_splits(doc: fitz.Document, pdf_name: str) -> list[dict]:
    """Detect diagrams awkwardly split across consecutive pages."""
    bad: list[dict] = []
    page_height = doc[0].rect.height if len(doc) else 792

    for i in range(len(doc) - 1):
        p1 = doc[i]
        p2 = doc[i + 1]
        d1 = _drawing_count(p1)
        d2 = _drawing_count(p2)
        if d1 < MIN_VECTOR_PATHS or d2 < MIN_VECTOR_PATHS:
            continue

        ranges1 = _drawing_y_ranges(p1)
        ranges2 = _drawing_y_ranges(p2)
        if not ranges1 or not ranges2:
            continue

        bottom_y = max(r[1] for r in ranges1)
        top_y = min(r[0] for r in ranges2)

        near_bottom = bottom_y > page_height * 0.55
        near_top = top_y < page_height * 0.45
        if not (near_bottom and near_top):
            continue

        split_y = (bottom_y + top_y) / 2
        frac = split_y / page_height
        if frac < SPLIT_FRAC_THRESHOLD:
            bad.append({
                "pdf": pdf_name,
                "pages": f"{i + 1}-{i + 2}",
                "frac": round(frac, 2),
                "bottom_drawings": d1,
                "top_drawings": d2,
            })
    return bad


def detect_blank_pages(doc: fitz.Document, pdf_name: str) -> list[dict]:
    blank: list[dict] = []
    for i, page in enumerate(doc):
        text = _page_text(page)
        clean = re.sub(r"(CONFIDENTIAL|Coresight Research|Page \d+ of \d+)", "", text)
        clean = re.sub(r"\s+", "", clean)
        if len(clean) < BLANK_CHAR_THRESHOLD and not _has_images(page):
            if _drawing_count(page) < MIN_VECTOR_PATHS:
                blank.append({"pdf": pdf_name, "page": i + 1, "chars": len(clean)})
    return blank


def detect_heading_only_pages(doc: fitz.Document, pdf_name: str) -> list[dict]:
    """Pages with only a single H2-style heading and nothing else."""
    waste: list[dict] = []
    for i, page in enumerate(doc):
        text = _page_text(page)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        content_lines = [
            ln for ln in lines
            if not re.match(r"^(CONFIDENTIAL|Coresight|Page \d+ of \d+|Internal Use)", ln, re.I)
            and not re.match(r"^Coresight Research Portal", ln, re.I)
        ]
        if len(content_lines) == 1:
            if len(content_lines[0]) < 80 and not _has_images(page):
                if _drawing_count(page) < MIN_VECTOR_PATHS:
                    waste.append({"pdf": pdf_name, "page": i + 1, "text": content_lines[0]})
    return waste


def grep_capiq(text: str) -> bool:
    return "CapIQReplacement" in text or "capiqreplacement" in text.lower()


def validate_toc_samples() -> dict[str, object]:
    """Validate clickable TOC links for representative individual PDFs."""
    sample_results: dict[str, object] = {}
    screenshots: list[str] = []
    VALIDATION_TOC_DIR.mkdir(parents=True, exist_ok=True)

    for pdf_name in TOC_SAMPLE_PDFS:
        pdf_path = DOC_DIR / pdf_name
        md_path = DOC_DIR / f"{pdf_path.stem}.md"
        if not pdf_path.exists() or not md_path.exists():
            sample_results[pdf_name] = {"ok": False, "error": "missing file"}
            continue

        _, _, headings = _build_html(md_path)
        toc_result = validate_toc_links(pdf_path, headings)
        sample_results[pdf_name] = toc_result

        doc = fitz.open(pdf_path)
        try:
            toc_page_idx = 1 if len(doc) > 1 else 0
            pix = doc[toc_page_idx].get_pixmap(matrix=fitz.Matrix(2, 2))
            shot_path = VALIDATION_TOC_DIR / f"{pdf_path.stem}-toc.png"
            pix.save(str(shot_path))
            screenshots.append(shot_path.name)
        finally:
            doc.close()

    all_ok = all(bool(r.get("ok")) for r in sample_results.values() if isinstance(r, dict))
    return {
        "samples": sample_results,
        "screenshots": screenshots,
        "ok": all_ok,
    }


def validate_all() -> dict:
    results = {
        "blank_pages": [],
        "heading_only_pages": [],
        "bad_splits": [],
        "capiq_md": [],
        "capiq_pdf": [],
        "page_counts": {},
        "total_pages": 0,
        "toc_validation": validate_toc_samples(),
    }

    for md_path in ALL_MDS:
        text = md_path.read_text(encoding="utf-8")
        if grep_capiq(text):
            results["capiq_md"].append(md_path.name)

    for pdf_path in ALL_PDFS:
        if pdf_path.name.startswith("_"):
            continue
        if pdf_path.name == "Market-Data-Portal-Technical-Documentation-Complete.pdf":
            continue
        doc = fitz.open(pdf_path)
        try:
            results["page_counts"][pdf_path.name] = len(doc)
            results["total_pages"] += len(doc)

            full_text = "\n".join(page.get_text() for page in doc)
            if grep_capiq(full_text):
                results["capiq_pdf"].append(pdf_path.name)

            results["blank_pages"].extend(detect_blank_pages(doc, pdf_path.name))
            results["bad_splits"].extend(detect_bad_splits(doc, pdf_path.name))
            results["heading_only_pages"].extend(detect_heading_only_pages(doc, pdf_path.name))
        finally:
            doc.close()

    return results


def main() -> int:
    results = validate_all()
    print(json.dumps(results, indent=2))

    toc_ok = bool(results["toc_validation"]["ok"])
    print("\nTOC sample validation:", "PASS" if toc_ok else "FAIL")
    for pdf_name, sample in results["toc_validation"]["samples"].items():
        if not isinstance(sample, dict):
            continue
        status = "PASS" if sample.get("ok") else "FAIL"
        print(
            f"  {pdf_name}: {status} — "
            f"{sample.get('link_count', 0)} internal links "
            f"({sample.get('resolved', 0)} resolved, expected {sample.get('expected', 0)})"
        )

    fail = (
        len(results["blank_pages"]) > 0
        or len(results["bad_splits"]) > 0
        or len(results["capiq_md"]) > 0
        or len(results["capiq_pdf"]) > 0
        or len(results["heading_only_pages"]) > 0
        or not toc_ok
    )
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
