#!/usr/bin/env python3
"""Add numbered badge + curved arrow callouts to business documentation screenshots.

Each callout draws:
  1. Numbered badge circle (①②③ or 1,2,3) in the margin
  2. Curved (quadratic bezier) arrow from badge to target point
  3. Optional short label text beside the badge

NO bounding boxes — arrows only.

Usage:
  .venv/bin/python scripts/annotate_business_screenshot.py input.png -o output.png \\
      --arrow "80,120,280,145,1,Email field" \\
      --arrow "80,200,280,225,2,Password"

Arrow format: badge_x,badge_y,target_x,target_y,number[,label]
  - badge_x,badge_y   = badge position (left/top margin, clear of content)
  - target_x,target_y = arrow tip lands ON the target control center
  - number            = callout number (1-15 → circled digits)
  - label             = optional short text beside badge
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
RED = (214, 46, 47, 255)
ARROW_COLOR = (214, 46, 47, 230)
WHITE = (255, 255, 255, 255)
BADGE_TEXT = (255, 255, 255, 255)
LABEL_TEXT = (40, 40, 40, 255)
BADGE_RADIUS = 18
ARROW_WIDTH = 2
BEZIER_SAMPLES = 32


@dataclass(frozen=True)
class ArrowCallout:
    sx: int
    sy: int
    tx: int
    ty: int
    num: int
    label: str = ""


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf"]
        if bold
        else ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"]
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _circled_label(num: int) -> str:
    if 1 <= num <= len(CIRCLED_NUMBERS):
        return CIRCLED_NUMBERS[num - 1]
    return str(num)


def _edge_point_from_center(
    cx: float,
    cy: float,
    tx: float,
    ty: float,
    radius: float,
) -> tuple[float, float]:
    dx, dy = tx - cx, ty - cy
    length = math.hypot(dx, dy)
    if length < 1:
        return cx, cy
    return cx + dx * radius / length, cy + dy * radius / length


def _quadratic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    samples: int = BEZIER_SAMPLES,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for i in range(samples + 1):
        t = i / samples
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        points.append((x, y))
    return points


def _control_point(
    start: tuple[float, float],
    end: tuple[float, float],
    curvature: float = 0.35,
) -> tuple[float, float]:
    mx = (start[0] + end[0]) / 2
    my = (start[1] + end[1]) / 2
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length
    offset = length * curvature
    return mx + nx * offset, my + ny * offset


def _draw_arrowhead(
    draw: ImageDraw.ImageDraw,
    tip: tuple[float, float],
    prev: tuple[float, float],
    color: tuple[int, int, int, int],
    size: float = 11,
) -> None:
    angle = math.atan2(tip[1] - prev[1], tip[0] - prev[0])
    left = (
        tip[0] - size * math.cos(angle - math.pi / 7),
        tip[1] - size * math.sin(angle - math.pi / 7),
    )
    right = (
        tip[0] - size * math.cos(angle + math.pi / 7),
        tip[1] - size * math.sin(angle + math.pi / 7),
    )
    draw.polygon([tip, left, right], fill=color)


def _draw_callout(
    draw: ImageDraw.ImageDraw,
    callout: ArrowCallout,
    badge_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    target = (float(callout.tx), float(callout.ty))
    badge_edge = _edge_point_from_center(
        callout.sx,
        callout.sy,
        target[0],
        target[1],
        BADGE_RADIUS + 2,
    )
    control = _control_point(badge_edge, target, curvature=0.38)
    curve = _quadratic_bezier(badge_edge, control, target)

    for i in range(len(curve) - 1):
        draw.line([curve[i], curve[i + 1]], fill=ARROW_COLOR, width=ARROW_WIDTH)
    _draw_arrowhead(draw, curve[-1], curve[-2], ARROW_COLOR)

    badge_bbox = (
        callout.sx - BADGE_RADIUS,
        callout.sy - BADGE_RADIUS,
        callout.sx + BADGE_RADIUS,
        callout.sy + BADGE_RADIUS,
    )
    draw.ellipse(badge_bbox, fill=RED, outline=WHITE, width=2)

    text = _circled_label(callout.num)
    text_bbox = draw.textbbox((0, 0), text, font=badge_font)
    tw = text_bbox[2] - text_bbox[0]
    th = text_bbox[3] - text_bbox[1]
    draw.text(
        (callout.sx - tw / 2, callout.sy - th / 2 - 1),
        text,
        fill=BADGE_TEXT,
        font=badge_font,
    )

    if callout.label:
        label_x = callout.sx + BADGE_RADIUS + 8
        label_y = callout.sy - 8
        pad_x, pad_y = 6, 3
        lb = draw.textbbox((label_x, label_y), callout.label, font=label_font)
        draw.rounded_rectangle(
            (
                lb[0] - pad_x,
                lb[1] - pad_y,
                lb[2] + pad_x,
                lb[3] + pad_y,
            ),
            radius=4,
            fill=(255, 255, 255, 220),
            outline=RED,
            width=1,
        )
        draw.text((label_x, label_y), callout.label, fill=LABEL_TEXT, font=label_font)


def annotate(
    input_path: Path,
    output_path: Path,
    callouts: list[ArrowCallout],
) -> None:
    base = Image.open(input_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    badge_font = _load_font(18, bold=True)
    label_font = _load_font(13)

    for callout in callouts:
        _draw_callout(draw, callout, badge_font, label_font)

    composed = Image.alpha_composite(base, overlay)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(output_path, format="PNG", optimize=True)


def _parse_arrow(raw: str) -> ArrowCallout:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 5:
        raise argparse.ArgumentTypeError(
            f"Arrow must be badge_x,badge_y,target_x,target_y,number[,label] — got {raw!r}"
        )
    label = ",".join(parts[5:]) if len(parts) > 5 else ""
    return ArrowCallout(
        sx=int(parts[0]),
        sy=int(parts[1]),
        tx=int(parts[2]),
        ty=int(parts[3]),
        num=int(parts[4]),
        label=label,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output PNG path (default: <input>-annotated.png)",
    )
    parser.add_argument(
        "--arrow",
        action="append",
        type=_parse_arrow,
        default=[],
        dest="arrows",
        help='Arrow as "badge_x,badge_y,target_x,target_y,number[,label]"',
    )
    args = parser.parse_args()

    if not args.arrows:
        parser.error("At least one --arrow is required")

    output = args.output or args.input.with_name(
        f"{args.input.stem}-annotated{args.input.suffix}"
    )
    annotate(args.input, output, args.arrows)
    print(json.dumps({"input": str(args.input), "output": str(output), "count": len(args.arrows)}))


if __name__ == "__main__":
    main()
