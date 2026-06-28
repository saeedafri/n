#!/usr/bin/env python3
"""Measure UI element centers in business documentation screenshots."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
SCREENSHOT_ROOT = REPO / "docs/business-documentation/_screenshots"

CORESIGHT_RED = (214, 46, 47)


def _near_color(rgb: tuple[int, int, int], target: tuple[int, int, int], tol: int = 35) -> bool:
    return all(abs(rgb[i] - target[i]) <= tol for i in range(3))


def find_red_regions(img: Image.Image, min_pixels: int = 500) -> list[dict]:
    target = CORESIGHT_RED
    w, h = img.size
    pixels = img.convert("RGB").load()
    mask = [
        [
            _near_color(pixels[x, y], target, 30) and pixels[x, y][0] > 150 and pixels[x, y][1] < 100
            for x in range(w)
        ]
        for y in range(h)
    ]
    visited = [[False] * w for _ in range(h)]
    regions: list[dict] = []

    def flood(sx: int, sy: int) -> tuple[int, int, int, int, int]:
        stack = [(sx, sy)]
        min_x = max_x = sx
        min_y = max_y = sy
        count = 0
        while stack:
            cx, cy = stack.pop()
            if cx < 0 or cy < 0 or cx >= w or cy >= h:
                continue
            if visited[cy][cx] or not mask[cy][cx]:
                continue
            visited[cy][cx] = True
            count += 1
            min_x, max_x = min(min_x, cx), max(max_x, cx)
            min_y, max_y = min(min_y, cy), max(max_y, cy)
            stack.extend([(cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)])
        return min_x, min_y, max_x, max_y, count

    for y in range(h):
        for x in range(w):
            if mask[y][x] and not visited[y][x]:
                x0, y0, x1, y1, count = flood(x, y)
                if count >= min_pixels:
                    regions.append(
                        {
                            "bbox": [x0, y0, x1, y1],
                            "center": [(x0 + x1) // 2, (y0 + y1) // 2],
                            "size": [x1 - x0, y1 - y0],
                        }
                    )
    regions.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]))
    return regions


def find_gray_buttons(img: Image.Image, y_min: int, y_max: int) -> list[dict]:
    """Find medium-gray rectangular buttons (View buttons etc)."""
    w, h = img.size
    px = img.convert("RGB").load()
    results: list[dict] = []
    for y in range(y_min, min(y_max, h)):
        gray_run = 0
        run_start = 0
        for x in range(0, w):
            r, g, b = px[x, y]
            is_gray_btn = 160 <= r <= 200 and 160 <= g <= 200 and 160 <= b <= 200 and abs(r - g) < 15
            if is_gray_btn:
                if gray_run == 0:
                    run_start = x
                gray_run += 1
            else:
                if gray_run > 150:
                    x0, x1 = run_start, x - 1
                    # find vertical extent
                    top, bot = y, y
                    while top > y_min and all(
                        160 <= px[(x0 + x1) // 2, top][c] <= 210 for c in range(3)
                    ):
                        top -= 1
                    while bot < y_max and all(
                        160 <= px[(x0 + x1) // 2, bot][c] <= 210 for c in range(3)
                    ):
                        bot += 1
                    if 30 < bot - top < 70:
                        results.append(
                            {
                                "bbox": [x0, top, x1, bot],
                                "center": [(x0 + x1) // 2, (top + bot) // 2],
                            }
                        )
                gray_run = 0
    # dedupe overlapping
    deduped: list[dict] = []
    for r in results:
        if not any(
            abs(r["center"][0] - d["center"][0]) < 30 and abs(r["center"][1] - d["center"][1]) < 20
            for d in deduped
        ):
            deduped.append(r)
    deduped.sort(key=lambda r: (r["center"][1], r["center"][0]))
    return deduped


def find_white_inputs(img: Image.Image, y_min: int, y_max: int) -> list[dict]:
    """Find white dropdown/input fields with light borders."""
    w, h = img.size
    px = img.convert("RGB").load()
    results: list[dict] = []
    for y in range(y_min, min(y_max, h)):
        white_run = 0
        run_start = 0
        for x in range(0, w):
            r, g, b = px[x, y]
            is_white = r > 240 and g > 240 and b > 240
            if is_white:
                if white_run == 0:
                    run_start = x
                white_run += 1
            else:
                if white_run > 200:
                    x0, x1 = run_start, x - 1
                    mid_x = (x0 + x1) // 2
                    top, bot = y, y
                    while top > y_min and px[mid_x, top][0] > 230:
                        top -= 1
                    while bot < y_max and px[mid_x, bot][0] > 230:
                        bot += 1
                    if 30 < bot - top < 60:
                        results.append(
                            {
                                "bbox": [x0, top, x1, bot],
                                "center": [mid_x, (top + bot) // 2],
                            }
                        )
                white_run = 0
    deduped: list[dict] = []
    for r in results:
        if not any(
            abs(r["center"][0] - d["center"][0]) < 50 and abs(r["center"][1] - d["center"][1]) < 25
            for d in deduped
        ):
            deduped.append(r)
    deduped.sort(key=lambda r: (r["center"][1], r["center"][0]))
    return deduped


def find_light_cards(img: Image.Image, y_min: int, y_max: int) -> list[dict]:
    """Find light grey card backgrounds."""
    w, h = img.size
    px = img.convert("RGB").load()
    # sample grid for card-colored regions
    cards: list[dict] = []
    step = 40
    for y in range(y_min, y_max, step):
        for x in range(200, w - 200, step):
            r, g, b = px[x, y]
            if 230 <= r <= 248 and 230 <= g <= 248 and 230 <= b <= 248:
                cards.append((x, y))
    if not cards:
        return []
    # cluster into left/right
    left = [c for c in cards if c[0] < w // 2]
    right = [c for c in cards if c[0] >= w // 2]
    results = []
    for cluster, name in [(left, "left_card"), (right, "right_card")]:
        if cluster:
            xs = [c[0] for c in cluster]
            ys = [c[1] for c in cluster]
            cx, cy = (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2
            results.append({"name": name, "center": [cx, cy], "bbox": [min(xs), min(ys), max(xs), max(ys)]})
    return results


def probe_image(path: Path) -> dict:
    img = Image.open(path)
    w, h = img.size
    result: dict = {"file": str(path.relative_to(SCREENSHOT_ROOT)), "size": [w, h]}
    result["red_regions"] = find_red_regions(img)
    # context-dependent scans
    if "login-02" in path.name:
        result["inputs"] = find_white_inputs(img, 250, 520)
    elif "home" in path.name or "landing" in path.name:
        result["inputs"] = find_white_inputs(img, 350, 700)
        result["gray_buttons"] = find_gray_buttons(img, 450, 750)
        result["cards"] = find_light_cards(img, 300, 750)
    elif "newsroom" in path.name:
        result["inputs"] = find_white_inputs(img, 150, 280)
    return result


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: probe_screenshot_coords.py <image.png> [--json]")
        raise SystemExit(1)
    path = Path(sys.argv[1])
    data = probe_image(path)
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2))
    else:
        print(f"{data['file']} {data['size']}")
        for key in ("red_regions", "inputs", "gray_buttons", "cards"):
            if key in data:
                print(f"  {key}:")
                for item in data[key]:
                    label = item.get("name", "")
                    print(f"    {label} center={item['center']} bbox={item.get('bbox')}")


if __name__ == "__main__":
    main()
