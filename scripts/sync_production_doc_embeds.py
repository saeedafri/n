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
    all_ann = sorted(p.name for p in V2.glob("*-annotated.png"))
    still_missing = []
    for base, reason in STAGING_ONLY.items():
        ann = base.replace(".png", "-annotated.png")
        if ann not in all_ann:
            still_missing.append((ann, reason))

    text = f"""# Business documentation — screenshot inventory

Last updated: 2026-06-30 (rebuild — numbers-only badges)

## Status

- **Annotated screenshots:** {len(all_ann)} `-annotated.png` files in `_screenshots/v2/`
- **Annotation style:** Red numbered circle badges in left margin only (no arrows)
- **Production capture:** See `REBUILD-REPORT.md` for login test and capture source

Regenerate captures (production, when reachable):

```bash
MDP_EMAIL=... MDP_PASSWORD=... .venv/bin/python scripts/capture_production_screenshots.py
```

Local staging DB capture (when production DNS unreachable):

```bash
MDP_BASE_URL=http://localhost:8501 .venv/bin/python scripts/capture_production_screenshots.py --no-auth --skip-login-captures
```

Re-annotate all badges:

```bash
.venv/bin/python scripts/batch_annotate_business_screenshots.py
```

---

## Staging-only admin pages

| Annotated file | Notes |
|----------------|--------|
| `retailer-adding-annotated.png` | Capture on staging/local when prod redirects to Home |
| `company-filings-add-files-annotated.png` | Capture on staging/local when prod redirects to Home |

**Gap count:** {len(still_missing)} (0 when both admin PNGs exist in v2/)
"""
    GAPS.write_text(text, encoding="utf-8")


def main() -> None:
    entries = rebuild_manifest()
    rebuild_verification(entries)
    rebuild_gaps()
    print(json.dumps({"annotated": len(entries), "gaps": len(STAGING_ONLY)}, indent=2))


if __name__ == "__main__":
    main()
