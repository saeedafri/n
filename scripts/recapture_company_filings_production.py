#!/usr/bin/env python3
"""Re-capture company-filings PNGs from production with WMT + clean viewer.

Picks the first document combo that loads without a broken embed placeholder.
Requires MDP_EMAIL and MDP_PASSWORD in the environment (never commit credentials).

Usage:
  MDP_EMAIL=mohdsaeedafri@coresight.com MDP_PASSWORD=... \\
    .venv/bin/python scripts/recapture_company_filings_production.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from capture_production_screenshots import (  # noqa: E402
    OUT,
    _annotate,
    _login,
    _merge_manifest,
    log,
)
from playwright.sync_api import sync_playwright

BROKEN = "Screenshot 2026-05-15"
BASE = os.environ.get("MDP_BASE_URL", "https://marketdata.coresight.com").rstrip("/")

MARKERS_02 = [
    "80,210,1,900,210,Filing header",
    "80,290,2,1380,175,Download button",
    "80,370,3,850,450,Document viewer",
]
MARKERS_03 = [
    "80,250,1,200,250,Metric search box",
    "80,400,2,200,400,Metric result cards",
    "80,550,3,850,450,Document body",
]
MARKERS_MAIN = [
    "80,200,1,620,155,Company dropdown",
    "80,320,2,850,450,Document viewer",
    "80,440,3,980,155,Document type filter",
]


def _pick(page, idx: int, needle: str) -> None:
    page.locator("[data-baseweb='select']").nth(idx).click()
    page.wait_for_timeout(700)
    page.locator("[role='option']").filter(has_text=needle).first.click()
    page.wait_for_timeout(5000)


def _wait_ok(page, max_s: int = 90) -> tuple[str, bool]:
    for _ in range(max_s // 2):
        text = page.inner_text("body")
        if BROKEN in text:
            return text, False
        if "Loading filing data" in text:
            page.wait_for_timeout(2000)
            continue
        if "Download" in text and "Filing not found" not in text and "Unable to load" not in text:
            if "Walmart" in text or "WMT" in text:
                return text, True
        page.wait_for_timeout(2000)
    return page.inner_text("body"), False


def _select_clean_filing(page) -> tuple[str, str] | None:
    page.goto(f"{BASE}/company_filings?ticker=WMT", wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(8000)
    combos = [
        ("10-K", "2025"),
        ("10-K", "2024"),
        ("10-K", "2023"),
        ("10-Q-Q3", "2025"),
        ("10-Q-Q2", "2025"),
        ("10-Q-Q1", "2025"),
    ]
    for doc, year in combos:
        try:
            if page.locator("[data-baseweb='select']").count() < 3:
                page.wait_for_timeout(5000)
            _pick(page, 0, "Walmart")
            _pick(page, 1, doc)
            _pick(page, 2, year)
            _, ok = _wait_ok(page)
            log(f"  try {doc}/{year}: {'OK' if ok else 'skip'}")
            if ok:
                return doc, year
        except Exception as exc:
            log(f"  try {doc}/{year} failed: {exc}")
    return None


def main() -> int:
    password = os.environ.get("MDP_PASSWORD", "").strip()
    email = os.environ.get("MDP_EMAIL", "").strip() or os.environ.get(
        "MDP_EMAIL_ALT", "mohdsaeedafri@coresight.com"
    )
    if not password:
        log("Set MDP_EMAIL and MDP_PASSWORD environment variables.")
        return 1

    manifest: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 1200})
        ok, who = _login(ctx, [email], password)
        if not ok:
            log(f"login failed: {who}")
            return 2
        page = ctx.new_page()
        chosen = _select_clean_filing(page)
        if not chosen:
            log("No clean WMT filing combo found")
            return 3
        log(f"Using {chosen[0]} / {chosen[1]}")

        out02 = OUT / "company-filings-02.png"
        page.screenshot(path=str(out02), full_page=True)
        _annotate(out02, OUT / "company-filings-02-annotated.png", MARKERS_02)

        for sel in ("input[placeholder*='eg' i]", "input[placeholder*='metric' i]"):
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill("revenue")
                page.wait_for_timeout(6000)
                break
        out03 = OUT / "company-filings-03.png"
        page.screenshot(path=str(out03), full_page=True)
        _annotate(out03, OUT / "company-filings-03-annotated.png", MARKERS_03)

        _select_clean_filing(page)
        out_main = OUT / "company-filings.png"
        page.screenshot(path=str(out_main), full_page=True)
        _annotate(out_main, OUT / "company-filings-annotated.png", MARKERS_MAIN)

        for fname, ann in (
            ("company-filings-02.png", "company-filings-02-annotated.png"),
            ("company-filings-03.png", "company-filings-03-annotated.png"),
            ("company-filings.png", "company-filings-annotated.png"),
        ):
            manifest.append(
                {
                    "page": "company-filings",
                    "file": fname,
                    "status": "ok",
                    "annotated": ann,
                    "source": "production",
                    "reason": f"ok {chosen}",
                }
            )
        page.close()
        ctx.close()
        browser.close()

    _merge_manifest(manifest)
    log("Done — re-run merge_business_doc_pdf.py --also-regenerate-individual")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
