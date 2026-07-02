#!/usr/bin/env python3
"""Batch-annotate business documentation screenshots from CALLOUTS registry."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from annotate_business_screenshot import BadgeCallout, _parse_badge, annotate  # noqa: E402
from business_screenshot_callouts import CALLOUTS_BY_FILE  # noqa: E402

SCREENSHOT_ROOT = REPO / "docs/business-documentation/_screenshots/v2"


def main() -> int:
    ok = 0
    skipped = 0
    results: list[dict] = []

    for filename, markers in sorted(CALLOUTS_BY_FILE.items()):
        input_path = SCREENSHOT_ROOT / filename
        if not input_path.exists():
            print(f"SKIP missing: {filename}")
            skipped += 1
            continue

        output_path = SCREENSHOT_ROOT / filename.replace(".png", "-annotated.png")
        callouts = [_parse_badge(m) for m in markers]
        annotate(input_path, output_path, callouts)
        ok += 1
        results.append({"file": filename, "output": output_path.name, "badges": len(callouts)})
        print(f"OK {filename} -> {output_path.name} ({len(callouts)} badges)")

    summary = {"annotated": ok, "skipped": skipped, "results": results}
    print(json.dumps(summary, indent=2))
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
