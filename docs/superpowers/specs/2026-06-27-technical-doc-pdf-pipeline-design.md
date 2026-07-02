# Technical Documentation PDF Pipeline — Design Spec

**Date:** 2026-06-27  
**Status:** Implemented  
**Scope:** Rebuild all 21 `docs/technical-documentation/*.pdf` files to professional internal standard.

---

## Problem

Previous PDF generation used pandoc/XeLaTeX or plain `md-to-pdf` without Mermaid rendering. Resulting PDFs had:

- Mermaid blocks as raw code (critical failure for architecture docs)
- Default LaTeX typography (weak heading hierarchy, excessive whitespace)
- No Coresight branding, logo, or confidentiality notice
- Unacceptable quality for stakeholder distribution

---

## Solution Architecture

```
.md source
    │
    ├─► Extract & sanitize Mermaid blocks
    │       └─► @mermaid-js/mermaid-cli (mmdc) → SVG (cached in _diagram_cache/)
    │
    ├─► python-markdown → HTML body (tables, code, TOC anchors)
    │
    ├─► Inject into _pdf_template.html + _pdf_styles.css
    │       • Cover page (logo, title, date, CONFIDENTIAL banner)
    │       • Auto TOC from H1–H3
    │       • Branded typography & tables
    │
    └─► Playwright Chromium print → PDF
            • Header: logo + doc title
            • Footer: CONFIDENTIAL | Coresight Research | Page X of Y
```

**Script:** `scripts/generate_technical_doc_pdf.py`

---

## Brand Assets

| Asset | Source |
|-------|--------|
| Logo | `docs/technical-documentation/_assets/coresight-logo.png` (Coresight CDN, same as `navigation.py`) |
| Primary blue | `#0066CC` (`app/components/styles.py` COLORS.primary) |
| Heading dark | `#1a365d` |
| Confidential red | `#D62E2F` (Coresight brand red, `loading.py`) |
| Table header | `#E6F2FF` (COLORS.primary_light) |
| Font | Inter / Source Sans 3 (Google Fonts, loaded in template) |

---

## Mermaid Sanitization

Some source diagrams use node labels with `@`, `/`, `?`, or `{` that break mmdc parsing. The pipeline auto-quotes labels containing these characters before rendering (e.g. `{@coresight.com?}` → `{"@coresight.com?"}`).

---

## Documents Covered (21)

README, 00–04 infrastructure, 14 registered page docs, forecasting-admin. **Excluded:** earnings_calendar (per project rules).

---

## Verification

1. File size: PDFs with diagrams are 600KB–960KB (vs ~60–75KB text-only before).
2. `python scripts/generate_technical_doc_pdf.py --verify <file.pdf>` — checks embedded images via pymupdf.
3. Visual: `_sample-flowchart-page.png` — README architecture sequence diagram page.

---

## Rollout

Run `python scripts/generate_technical_doc_pdf.py` after any `.md` edit. Markdown remains source of truth; PDFs are distribution artifacts for the tech team.
