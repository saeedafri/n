#!/usr/bin/env python3
"""Batch-annotate business documentation screenshots from callouts_config.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from annotate_business_screenshot import ArrowCallout, annotate  # noqa: E402

CONFIG = REPO / "docs/business-documentation/_screenshots/v2/callouts_config.json"
SCREENSHOT_ROOT = REPO / "docs/business-documentation/_screenshots"


def _parse_callout(raw: dict) -> ArrowCallout:
    return ArrowCallout(
        sx=int(raw["sx"]),
        sy=int(raw["sy"]),
        tx=int(raw["tx"]),
        ty=int(raw["ty"]),
        num=int(raw["num"]),
        label=str(raw.get("label", "")),
    )


def main() -> int:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    ok = 0
    skipped = 0
    results: list[dict] = []

    for entry in data["images"]:
        rel = entry["file"]
        input_path = SCREENSHOT_ROOT / rel
        if not input_path.exists():
            print(f"SKIP missing: {rel}")
            skipped += 1
            continue
        if entry.get("skip"):
            print(f"SKIP flagged: {rel} ({entry.get('reason', '')})")
            skipped += 1
            continue

        output_rel = entry.get("output") or rel.replace(".png", "-annotated.png")
        output_path = SCREENSHOT_ROOT / output_rel
        callouts = [_parse_callout(c) for c in entry["callouts"]]
        annotate(input_path, output_path, callouts)
        ok += 1
        results.append({"file": rel, "output": output_rel, "callouts": len(callouts)})
        print(f"OK {rel} -> {output_rel} ({len(callouts)} arrows)")

    summary = {"annotated": ok, "skipped": skipped, "results": results}
    print(json.dumps(summary, indent=2))
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
