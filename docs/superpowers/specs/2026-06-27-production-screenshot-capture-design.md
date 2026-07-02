# Production MDP screenshot capture — design note

**Date:** 2026-06-27  
**Scope:** Fill `SCREENSHOT-GAPS.md` from live https://marketdata.coresight.com/

## Architecture

- `scripts/capture_production_screenshots.py` — Playwright headless, OIDC via coresight.com login, `auth_session` cookie poll, per-shot validation (reject "Unable to load", loading skeletons, access denied).
- Credentials only via `MDP_EMAIL` / `MDP_PASSWORD` env vars (never committed).
- Post-process: `scripts/annotate_business_screenshot.py --arrow`, `scripts/sync_production_doc_embeds.py`, `scripts/merge_business_doc_pdf.py --also-regenerate-individual`.

## Login flow (production)

1. `/` shows MDP welcome + `#csr-sso-btn`.
2. Redirect to `coresight.com/login` → email/password + math security check → OIDC callback `/?code=...`.
3. Wait for `auth_session` cookie before navigating to `/home`.

Primary email spelling failed IdP; alternate `mohdsaeedafri@coresight.com` succeeded.

## Staging-only pages (not capturable on prod)

`retailer_adding.py` and `company_filings_add_files.py` guard `config.env not in (LOCAL, STAGING)` → redirect to Home on production.

## Testing

- Automated capture + OCR-style body needle checks.
- PDF merge validation: 218 pages, 0 blank, 0 missing screenshots (merged guide).
