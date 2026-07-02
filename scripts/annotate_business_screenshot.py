#!/usr/bin/env python3
"""Add numbered badge callouts to business documentation screenshots.

Each callout draws:
  1. Red numbered circle badge (①②③) in the left margin
  2. Optional short label text beside the badge

NO arrows, NO curved lines, NO bounding boxes.

Usage:
  .venv/bin/python scripts/annotate_business_screenshot.py input.png -o output.png \\
      --badge "80,120,1,Email field" \\
      --badge "80,200,2,Password"

Badge format: badge_x,badge_y,number[,label]
  - badge_x,badge_y = badge center in left margin (clear of main content)
  - number          = callout number (1-15 → circled digits)
  - label           = optional short text beside badge

Legacy arrow format (target coords ignored): badge_x,badge_y,number,target_x,target_y[,label]
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
RED = (214, 46, 47, 255)
WHITE = (255, 255, 255, 255)
BADGE_TEXT = (255, 255, 255, 255)
LABEL_TEXT = (40, 40, 40, 255)
BADGE_RADIUS = 18


@dataclass(frozen=True)
class BadgeCallout:
    x: int
    y: int
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


def _draw_badge(
    draw: ImageDraw.ImageDraw,
    callout: BadgeCallout,
    badge_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    label_font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    badge_bbox = (
        callout.x - BADGE_RADIUS,
        callout.y - BADGE_RADIUS,
        callout.x + BADGE_RADIUS,
        callout.y + BADGE_RADIUS,
    )
    draw.ellipse(badge_bbox, fill=RED, outline=WHITE, width=2)

    text = _circled_label(callout.num)
    text_bbox = draw.textbbox((0, 0), text, font=badge_font)
    tw = text_bbox[2] - text_bbox[0]
    th = text_bbox[3] - text_bbox[1]
    draw.text(
        (callout.x - tw / 2, callout.y - th / 2 - 1),
        text,
        fill=BADGE_TEXT,
        font=badge_font,
    )

    if callout.label:
        label_x = callout.x + BADGE_RADIUS + 8
        label_y = callout.y - 8
        draw.text((label_x, label_y), callout.label, fill=LABEL_TEXT, font=label_font)


def annotate(
    input_path: Path,
    output_path: Path,
    callouts: list[BadgeCallout],
) -> None:
    base = Image.open(input_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    badge_font = _load_font(18, bold=True)
    label_font = _load_font(13)

    for callout in callouts:
        _draw_badge(draw, callout, badge_font, label_font)

    composed = Image.alpha_composite(base, overlay)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(output_path, format="PNG", optimize=True)


def _parse_badge(raw: str) -> BadgeCallout:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 3:
        raise argparse.ArgumentTypeError(
            f"Badge must be badge_x,badge_y,number[,label] — got {raw!r}"
        )
    # Legacy arrow format: bx,by,num,tx,ty[,label]
    if len(parts) >= 5 and parts[3].lstrip("-").isdigit() and parts[4].lstrip("-").isdigit():
        label = ",".join(parts[5:]) if len(parts) > 5 else ""
        return BadgeCallout(x=int(parts[0]), y=int(parts[1]), num=int(parts[2]), label=label)
    label = ",".join(parts[3:]) if len(parts) > 3 else ""
    return BadgeCallout(x=int(parts[0]), y=int(parts[1]), num=int(parts[2]), label=label)


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
        "--badge",
        action="append",
        type=_parse_badge,
        default=[],
        dest="badges",
        help='Badge as "badge_x,badge_y,number[,label]"',
    )
    parser.add_argument(
        "--arrow",
        action="append",
        type=_parse_badge,
        default=[],
        dest="legacy_arrows",
        help="Deprecated alias for --badge (target coords ignored)",
    )
    args = parser.parse_args()

    badges = args.badges + args.legacy_arrows
    if not badges:
        parser.error("At least one --badge is required")

    output = args.output or args.input.with_name(
        f"{args.input.stem}-annotated{args.input.suffix}"
    )
    annotate(args.input, output, badges)
    print(json.dumps({"input": str(args.input), "output": str(output), "count": len(badges)}))


if __name__ == "__main__":
    main()
