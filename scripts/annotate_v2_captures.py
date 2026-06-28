#!/usr/bin/env python3
"""Annotate all OK v2 screenshots listed in MANIFEST.json."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
V2 = REPO / "docs/business-documentation/_screenshots/v2"
MANIFEST = V2 / "MANIFEST.json"
CAPTURE = REPO / "scripts/capture_business_screenshots_v2.py"
PY = REPO / ".venv/bin/python"


def _marker_map() -> dict[str, list[str]]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("cap", CAPTURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out: dict[str, list[str]] = {}
    for s in mod._specs():
        out[s.filename] = s.markers
    return out


def main() -> int:
    manifest = json.loads(MANIFEST.read_text())
    markers = _marker_map()
    ok = 0
    for entry in manifest:
        if entry.get("status") != "ok":
            continue
        src = V2 / entry["file"]
        if not src.exists():
            print("MISSING", entry["file"])
            continue
        dst = V2 / entry["file"].replace(".png", "-annotated.png")
        m = markers.get(entry["file"], [])
        if not m:
            print("NO MARKERS", entry["file"])
            continue
        cmd = [str(PY), str(REPO / "scripts/annotate_business_screenshot.py"), str(src), "-o", str(dst)]
        for raw in m:
            parts = [p.strip() for p in raw.split(",")]
            cx, cy, num, tx, ty = parts[0], parts[1], parts[2], parts[3], parts[4]
            label = ",".join(parts[5:]) if len(parts) > 5 else ""
            cmd.extend(["--arrow", f"{cx},{cy},{tx},{ty},{num},{label}"])
        subprocess.run(cmd, check=True, cwd=REPO)
        entry["annotated"] = dst.name
        entry.pop("annotation_error", None)
        ok += 1
        print("OK", dst.name)

    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Annotated {ok} screenshots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
