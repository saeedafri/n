#!/usr/bin/env python3
"""Sync MANIFEST, VERIFICATION, SCREENSHOT-GAPS after production captures."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V2 = REPO / "docs/business-documentation/_screenshots/v2"
GAPS = REPO / "docs/business-documentation/SCREENSHOT-GAPS.md"

STAGING_ONLY = {
    "retailer-adding.png": "Page redirects to Home on production (staging/local guard)",
    "company-filings-add-files.png": "Page redirects to Home on production (staging/local guard)",
}

PRODUCTION_OK = sorted(
    p.name.replace("-annotated.png", ".png")
    for p in V2.glob("*-annotated.png")
    if p.name.replace("-annotated.png", ".png") not in STAGING_ONLY
)


def rebuild_manifest() -> list[dict]:
    existing: dict[str, dict] = {}
    manifest_path = V2 / "MANIFEST.json"
    if manifest_path.exists():
        for entry in json.loads(manifest_path.read_text()):
            existing[entry["file"]] = entry

    entries: list[dict] = []
    for ann in sorted(V2.glob("*-annotated.png")):
        base = ann.name.replace("-annotated.png", ".png")
        src = existing.get(base, {})
        entries.append(
            {
                "page": base.split("-")[0],
                "file": base,
                "source": src.get("source", "production" if (V2 / base).exists() else "user-provided"),
                "status": "ok",
                "annotated": ann.name,
                "reason": src.get("reason", "ok"),
            }
        )
    manifest_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return entries


def rebuild_verification(entries: list[dict]) -> None:
    lines = ["# v2 annotation verification\n", "| File | Annotated | Source |\n", "|------|-----------|--------|\n"]
    for e in entries:
        lines.append(f"| {e['file']} | YES | {e.get('source', '')} |\n")
    (V2 / "VERIFICATION.md").write_text("".join(lines), encoding="utf-8")


def rebuild_gaps() -> None:
    all_ann = {p.name for p in V2.glob("*-annotated.png")}
    still_missing = []
    for base, reason in STAGING_ONLY.items():
        ann = base.replace(".png", "-annotated.png")
        if ann not in all_ann:
            still_missing.append((ann, reason))

    prod_count = len([a for a in all_ann if not any(a.startswith(k.replace(".png", "")) for k in STAGING_ONLY)])
    text = f"""# Business documentation — screenshot inventory

Last updated: 2026-06-27 (production MDP capture run)

## Production capture (this run)

Automated via `scripts/capture_production_screenshots.py` against **https://marketdata.coresight.com/**.

- **Captured and annotated:** {len(all_ann)} total `-annotated.png` files in `_screenshots/v2/`
- **Production-authenticated pages:** login, home (gaps), market-data tabs, earnings calls/calendar, forecasting, company filings, access management, live earnings transcript
- **Login account used:** Coresight SSO (alternate spelling email; primary spelling rejected by IdP)

Regenerate captures: `MDP_EMAIL=... MDP_PASSWORD=... .venv/bin/python scripts/capture_production_screenshots.py`

---

## User-provided batch (prior run)

30 user PNGs → 31 annotated outputs (home, newsroom, screening). See git history for mapping.

---

## Remaining gaps

| Annotated file | Reason |
|----------------|--------|
| `retailer-adding-annotated.png` | {STAGING_ONLY['retailer-adding.png']} |
| `company-filings-add-files-annotated.png` | {STAGING_ONLY['company-filings-add-files.png']} |

**Gap count:** {len(still_missing)} annotated files still needed (capture on **staging** MDP or waive).

---

## Fixed this run

- Added `scripts/capture_production_screenshots.py` — OIDC login, validation, arrow annotation
- Filled **{len(all_ann) - 31}** new production screenshots (31 from prior user batch retained)
- Updated business guides with v2 embeds; regenerated merged + individual PDFs
"""
    GAPS.write_text(text, encoding="utf-8")


def main() -> None:
    entries = rebuild_manifest()
    rebuild_verification(entries)
    rebuild_gaps()
    print(json.dumps({"annotated": len(entries), "gaps": len(STAGING_ONLY)}, indent=2))


if __name__ == "__main__":
    main()
