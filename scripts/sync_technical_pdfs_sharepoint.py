#!/usr/bin/env python3
"""Copy technical (and optional business) doc PDFs into SharePoint-ready folder trees.

Sources are the generated PDF directories (_pdf/ or legacy PDFs/). Markdown sources
are never moved — only PDF copies land under sharepoint-pdfs/.

Usage:
    python scripts/sync_technical_pdfs_sharepoint.py
    python scripts/sync_technical_pdfs_sharepoint.py --business
    python scripts/sync_technical_pdfs_sharepoint.py --technical-only
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TECH_DOC_DIR = REPO / "docs" / "technical-documentation"
BUSINESS_DOC_DIR = REPO / "docs" / "business-documentation"

TECH_MERGED_NAME = "Market-Data-Portal-Technical-Documentation-Complete.pdf"
BUSINESS_MERGED_NAME = "Market-Data-Portal-User-Guide-Complete.pdf"

INFRASTRUCTURE_PDF_RE = re.compile(r"^\d{2}-.+\.pdf$", re.IGNORECASE)

# Individual PDFs that belong in technical pages/ (everything else in individual/ except infra + README).
TECH_PAGE_EXCLUDE = frozenset({"README.pdf"})

# Business QA / audit artifacts — not for SharePoint upload.
BUSINESS_SHAREPOINT_EXCLUDE = frozenset(
    {
        "FINAL-AUDIT.pdf",
        "QA-REPORT.pdf",
        "SCREENSHOT-GAPS.pdf",
        BUSINESS_MERGED_NAME,
    }
)


def _resolve_technical_pdf_roots(doc_dir: Path) -> tuple[Path, Path]:
    """Return (individual_dir, merged_pdf_path), preferring _pdf/ then PDFs/."""
    for base_name in ("_pdf", "PDFs"):
        base = doc_dir / base_name
        individual = base / "individual"
        merged = base / TECH_MERGED_NAME
        if individual.is_dir() or merged.is_file():
            return individual, merged
    return doc_dir / "_pdf" / "individual", doc_dir / "_pdf" / TECH_MERGED_NAME


def _copy_pdf(src: Path, dest: Path) -> bool:
    if not src.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def sync_technical_sharepoint(
    doc_dir: Path | None = None,
    dest_root: Path | None = None,
) -> dict[str, object]:
    """Copy technical PDFs into sharepoint-pdfs/ layout. Returns summary dict."""
    doc_dir = doc_dir or TECH_DOC_DIR
    dest_root = dest_root or (doc_dir / "sharepoint-pdfs")
    individual_src, merged_src = _resolve_technical_pdf_roots(doc_dir)

    infra_dest = dest_root / "00-infrastructure"
    pages_dest = dest_root / "pages"
    copied: list[str] = []
    missing: list[str] = []

    if individual_src.is_dir():
        for pdf in sorted(individual_src.glob("*.pdf")):
            name = pdf.name
            if name in TECH_PAGE_EXCLUDE:
                continue
            if INFRASTRUCTURE_PDF_RE.match(name):
                target = infra_dest / name
            else:
                target = pages_dest / name
            if _copy_pdf(pdf, target):
                copied.append(str(target.relative_to(dest_root)))
            else:
                missing.append(name)

    merged_dest = dest_root / TECH_MERGED_NAME
    if _copy_pdf(merged_src, merged_dest):
        copied.append(merged_dest.name)
    else:
        missing.append(TECH_MERGED_NAME)

    _write_technical_readme(dest_root, copied, missing)

    return {
        "dest": dest_root,
        "copied": copied,
        "missing": missing,
        "infra_count": len(list(infra_dest.glob("*.pdf"))) if infra_dest.is_dir() else 0,
        "pages_count": len(list(pages_dest.glob("*.pdf"))) if pages_dest.is_dir() else 0,
        "has_merged": merged_dest.is_file(),
    }


def sync_business_sharepoint(
    doc_dir: Path | None = None,
    dest_root: Path | None = None,
) -> dict[str, object] | None:
    """Mirror business user-guide PDFs when individual page PDFs exist."""
    doc_dir = doc_dir or BUSINESS_DOC_DIR
    page_pdfs = [
        p
        for p in sorted(doc_dir.glob("*.pdf"))
        if p.name not in BUSINESS_SHAREPOINT_EXCLUDE
    ]
    if not page_pdfs:
        return None

    dest_root = dest_root or (doc_dir / "sharepoint-pdfs")
    pages_dest = dest_root / "pages"
    copied: list[str] = []
    missing: list[str] = []

    for pdf in page_pdfs:
        target = pages_dest / pdf.name
        if _copy_pdf(pdf, target):
            copied.append(str(target.relative_to(dest_root)))
        else:
            missing.append(pdf.name)

    merged_src = doc_dir / BUSINESS_MERGED_NAME
    merged_dest = dest_root / BUSINESS_MERGED_NAME
    if _copy_pdf(merged_src, merged_dest):
        copied.append(merged_dest.name)
    else:
        missing.append(BUSINESS_MERGED_NAME)

    _write_business_readme(dest_root, copied, missing)

    return {
        "dest": dest_root,
        "copied": copied,
        "missing": missing,
        "pages_count": len(list(pages_dest.glob("*.pdf"))) if pages_dest.is_dir() else 0,
        "has_merged": merged_dest.is_file(),
    }


def _write_technical_readme(dest_root: Path, copied: list[str], missing: list[str]) -> None:
    readme = dest_root / "README.txt"
    dest_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "Market Data Portal — Technical Documentation (PDFs for SharePoint)",
        "=" * 72,
        "",
        "This folder contains PDF copies only. Markdown sources stay in the parent",
        "technical-documentation/ tree and are NOT required on SharePoint.",
        "",
        "Folder layout",
        "---------------",
        "  README.txt (this file)",
        f"  {TECH_MERGED_NAME}",
        "      Complete merged technical reference (all sections in one file).",
        "  00-infrastructure/",
        "      Shared platform docs: bootstrap/auth, components, core, data layer, utils.",
        "  pages/",
        "      One PDF per Streamlit route (login, market-data, screening, etc.).",
        "",
        "SharePoint upload",
        "-------------------",
        "1. Create a document library (or folder) for MDP technical documentation.",
        "2. Upload this entire sharepoint-pdfs/ folder preserving subfolders.",
        "3. Optionally pin the merged PDF at the library root for quick access.",
        "4. Do not upload markdown (.md), _templates/, or _diagram_cache/ — PDFs only.",
        "",
        "Regenerating",
        "------------",
        "  python scripts/generate_technical_doc_pdf.py",
        "  python scripts/merge_technical_doc_pdf.py",
        "  python scripts/sync_technical_pdfs_sharepoint.py",
        "",
        "The generate and merge scripts refresh this folder automatically after each run.",
        "",
    ]
    if missing:
        lines.extend(["Missing sources (not copied):", *[f"  - {m}" for m in missing], ""])
    lines.append(f"Last sync: {len(copied)} file(s) copied.")
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_business_readme(dest_root: Path, copied: list[str], missing: list[str]) -> None:
    readme = dest_root / "README.txt"
    dest_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "Market Data Portal — Business User Guide (PDFs for SharePoint)",
        "=" * 72,
        "",
        "PDF copies for end-user documentation. Markdown sources remain in",
        "business-documentation/ and are NOT required on SharePoint.",
        "",
        "Folder layout",
        "---------------",
        "  README.txt (this file)",
        f"  {BUSINESS_MERGED_NAME}",
        "      Complete merged user guide.",
        "  pages/",
        "      Individual section PDFs (login, home, market-data, etc.).",
        "",
        "Excluded from SharePoint mirror: FINAL-AUDIT.pdf, QA-REPORT.pdf, SCREENSHOT-GAPS.pdf",
        "(internal QA artifacts only).",
        "",
        "Regenerating",
        "------------",
        "  python scripts/generate_business_doc_pdf.py",
        "  python scripts/merge_business_doc_pdf.py",
        "  python scripts/sync_technical_pdfs_sharepoint.py --business",
        "",
    ]
    if missing:
        lines.extend(["Missing sources (not copied):", *[f"  - {m}" for m in missing], ""])
    lines.append(f"Last sync: {len(copied)} file(s) copied.")
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync technical/business PDFs into SharePoint-ready folders"
    )
    parser.add_argument(
        "--business",
        action="store_true",
        help="Also sync business-documentation sharepoint-pdfs/",
    )
    parser.add_argument(
        "--technical-only",
        action="store_true",
        help="Sync technical docs only (default when --business omitted)",
    )
    parser.add_argument(
        "--business-only",
        action="store_true",
        help="Sync business docs only",
    )
    args = parser.parse_args(argv)

    sync_tech = not args.business_only
    sync_bus = args.business or args.business_only

    rc = 0

    if sync_tech:
        result = sync_technical_sharepoint()
        dest = result["dest"]
        print(f"Technical → {dest}")
        print(f"  00-infrastructure/: {result['infra_count']} PDF(s)")
        print(f"  pages/: {result['pages_count']} PDF(s)")
        print(f"  merged: {'yes' if result['has_merged'] else 'MISSING'}")
        if result["missing"]:
            print(f"  missing: {', '.join(result['missing'])}", file=sys.stderr)
            rc = 1

    if sync_bus:
        bus = sync_business_sharepoint()
        if bus is None:
            print("Business: no individual PDFs found — skipped")
        else:
            print(f"Business → {bus['dest']}")
            print(f"  pages/: {bus['pages_count']} PDF(s)")
            print(f"  merged: {'yes' if bus['has_merged'] else 'MISSING'}")
            if bus["missing"]:
                print(f"  missing: {', '.join(bus['missing'])}", file=sys.stderr)
                rc = 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
