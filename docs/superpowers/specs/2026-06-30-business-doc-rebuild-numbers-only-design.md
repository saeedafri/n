# Business documentation rebuild — numbers-only screenshots

**Date:** 2026-06-30  
**Status:** Implemented (pending production re-capture when DNS reachable)

## Problem

1. Duplicate PNG content — `home-04-nav-highlight.png` was copied from `user-01.png` (same as `home-01.png`); 32 hash-duplicate groups in v2/.
2. Arrow annotations had wrong landing coordinates on several pages.
3. Cross-guide embed reuse (same `-annotated.png` on multiple sections) confused client readers.

## Solution

### Annotation (numbers only)

- Rewrote `scripts/annotate_business_screenshot.py` — red circled badges ①②③ in left margin + optional label; **no arrows, boxes, or curves**.
- Central registry: `scripts/business_screenshot_callouts.py`
- Batch: `scripts/batch_annotate_business_screenshots.py`

### Capture

- Extended `scripts/capture_production_screenshots.py` with full home/screening/newsroom specs, nav clip shots, `--no-auth` for localhost LOCAL bypass.
- Unique embed filenames: `nav-portal-overview`, `nav-newsroom-layout`, `screening-nav-context`, `market-data-entry-from-home`.

### Docs

- Fixed cross-page duplicate embeds in `navigation-and-portal-overview.md`, `screening.md`, `home.md`, `market-data.md`.
- `SCREENSHOT-GAPS.md` updated; final status in `REBUILD-REPORT.md`.

## Verification

```bash
.venv/bin/python scripts/batch_annotate_business_screenshots.py
.venv/bin/python scripts/sync_production_doc_embeds.py
.venv/bin/python scripts/merge_business_doc_pdf.py --also-regenerate-individual
python scripts/sync_technical_pdfs_sharepoint.py --business
```

## Production login

Requires `marketdata.coresight.com` DNS + SSO. Credentials via shell env only (`MDP_EMAIL`, `MDP_PASSWORD`).
