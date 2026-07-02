# Business documentation screenshot recapture — design note

**Date:** 2026-06-27  
**Scope:** Re-capture MDP business user guide screenshots against staging DB with admin test user.

## Problem

Prior `v2` captures failed with `Unable to load`, blank bodies, or **Access Denied** on admin pages because VPN/DB was unavailable and three pages had `require_auth()` commented out.

## Solution

1. Restart via `bash .claude/dev/run_local.sh` (LOCAL auth + STG DB).
2. Restore `require_auth()` on `access_management`, `company_filings_add_files`, and `forecasting`.
3. Expand `scripts/capture_business_screenshots_v2.py` — WMT ticker, all Market Data tabs, screening modes, admin pages, refresh dialog.
4. Annotate with `scripts/annotate_business_screenshot.py` (`--arrow` format).
5. Embed `_screenshots/v2/*-annotated.png` in markdown guides; regenerate PDFs.

## Testing

- Playwright capture script validates success needles and rejects error states.
- `merge_business_doc_pdf.py --also-regenerate-individual` — 222 pages, 0 screenshot errors.

## Rollout

Documentation-only; no production deployment. Re-run capture script when UI layout shifts materially.
