#!/usr/bin/env python3
"""Retry production captures that failed in the main run."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from capture_production_screenshots import (  # noqa: E402
    OUT,
    ShotSpec,
    _annotate,
    _capture_page,
    _login,
    _merge_manifest,
    log,
)
from playwright.sync_api import sync_playwright

RETRIES = [
    ShotSpec(
        "earnings-calendar", "/earnings_calendar", "earnings-calendar-02-events.png",
        ("Show types", "Q1", "M&A Completion"), wait_ms=12000, min_body_len=400,
        markers=["200,220,1,300,260,Legend chips",
                 "500,350,2,600,400,Day event markers",
                 "900,300,3,1000,350,Event colors"],
    ),
    ShotSpec(
        "forecasting", "/forecasting?ticker=WMT", "forecasting-02.png",
        ("Quarterly", "Quarter"), click_selector='[data-testid="stRadio"] label:has-text("Quarterly")',
        wait_ms=12000,
        markers=["400,200,1,500,240,Quarterly period toggle",
                 "600,350,2,700,400,Quarterly forecast table"],
    ),
    ShotSpec(
        "forecasting", "/forecasting?ticker=WMT", "forecasting-03.png",
        ("Scenario", "Baseline", "Quarterly"),
        click_selector='[data-testid="stRadio"] label:has-text("Quarterly")',
        wait_ms=12000,
        markers=["400,250,1,550,300,Scenario summary",
                 "700,350,2,800,400,Pessimistic band",
                 "900,350,3,1000,400,Optimistic band"],
    ),
]


def main() -> int:
    password = os.environ.get("MDP_PASSWORD", "")
    email = os.environ.get("MDP_EMAIL_ALT", "mohdsaeedafri@coresight.com")
    if not password:
        log("Set MDP_PASSWORD")
        return 1
    manifest = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1500, "height": 1200})
        ok, who = _login(ctx, [email], password)
        if not ok:
            log(f"login failed: {who}")
            return 2
        page = ctx.new_page()
        for spec in RETRIES:
            out = OUT / spec.filename
            log(f"RETRY {spec.filename}")
            ok, reason = _capture_page(page, spec, out, "WMT")
            entry = {"page": spec.page_id, "file": spec.filename, "status": "ok" if ok else "skipped", "reason": reason, "source": "production-retry"}
            if ok:
                ann = spec.filename.replace(".png", "-annotated.png")
                _annotate(out, OUT / ann, spec.markers)
                entry["annotated"] = ann
                log(f"  OK")
            else:
                if out.exists():
                    out.unlink()
                log(f"  SKIP: {reason}")
            manifest.append(entry)
        page.close()
        ctx.close()
        browser.close()
    _merge_manifest(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
