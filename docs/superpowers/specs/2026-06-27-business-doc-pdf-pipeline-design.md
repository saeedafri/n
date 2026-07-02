# Business Documentation PDF Pipeline — Design Spec

**Date:** 2026-06-27  
**Status:** Implemented  
**Scope:** Generate professional PDFs from `docs/business-documentation/*.md` for non-technical MDP users.

---

## Problem

Business user guides need the same distribution quality as technical documentation: Coresight branding, confidentiality notices, embedded UI screenshots, and clickable tables of contents — not raw markdown exports or unstyled HTML prints.

---

## Solution Architecture

```
.md source (plain language user guides)
    │
    ├─► Embed _screenshots/*.png as base64 figures (full width + captions)
    │
    ├─► Optional Mermaid blocks (simple user flows) → mmdc → SVG
    │
    ├─► python-markdown → HTML body (tables, blockquotes, TOC anchors)
    │
    ├─► Inject into _pdf_template.html + _pdf_styles.css
    │       • Cover: "Market Data Portal — User Guide"
    │       • Auto TOC from H1–H2
    │       • Screenshot figures + callout blockquotes
    │
    └─► Playwright Chromium print → PDF
            • Header: logo + doc title
            • Footer: CONFIDENTIAL | Coresight Research | Page X of Y
            • pymupdf TOC link annotations
```

**Scripts:**

| Script | Output |
|--------|--------|
| `scripts/generate_business_doc_pdf.py` | Individual `*.pdf` per markdown guide |
| `scripts/merge_business_doc_pdf.py` | `Market-Data-Portal-User-Guide-Complete.pdf` |

---

## Brand Assets

Reuses technical documentation assets:

| Asset | Path |
|-------|------|
| Logo | `docs/business-documentation/_assets/coresight-logo.png` |
| Mermaid config | `docs/business-documentation/_mermaid_config.json` |
| Styles | `docs/business-documentation/_pdf_styles.css` |
| Template | `docs/business-documentation/_pdf_template.html` |

Color palette matches `app/components/styles.py` and technical docs (`#0066CC`, `#1a365d`, `#D62E2F`).

---

## Screenshot Embedding

Markdown image syntax `![caption](_screenshots/foo.png)` is post-processed after `python-markdown`:

1. Resolve path under `docs/business-documentation/_screenshots/`
2. Fallback between `-annotated` and non-annotated variants
3. Embed as `<figure class="screenshot">` with base64 `data:` URI (full width)
4. Caption from alt text

---

## Merged User Guide

`merge_business_doc_pdf.py` builds a single HTML document:

1. Master cover — "Market Data Portal — Complete User Guide"
2. Clickable master TOC (Part N → section divider)
3. Section divider pages between each guide (except README inline start)
4. End-of-section markers
5. pymupdf internal links from master TOC to divider anchors

Merge sequence defined in `MERGE_SEQUENCE`; only existing `.md` files are included.

---

## Validation

`generate_business_doc_pdf.py --verify` and `merge_business_doc_pdf.py`:

| Check | Method |
|-------|--------|
| Screenshots render | pymupdf `page.get_images()` count ≥ expected refs in source |
| No blank pages | Text + image + vector drawing heuristics |
| TOC links work | pymupdf link count and resolved destinations |
| No auth bypass text | Regex filter for `LOCAL_TEST_USER_EMAIL`, `run_local.sh`, etc. |

Validation screenshots saved to `docs/business-documentation/_validation-business/` (cover + TOC).

---

## Rollout

```bash
python scripts/generate_business_doc_pdf.py
python scripts/merge_business_doc_pdf.py --also-regenerate-individual
```

Markdown remains source of truth; PDFs are distribution artifacts for business teams.

---

## Documents Covered

15 guides at initial rollout (market-data and screening pending from content agents). See `docs/business-documentation/README.md` for the live index.
