#!/usr/bin/env python3
"""
E2E smoke: Market Data → Key Stats → Period Type Annual → Quarterly, then refresh + repeat.

Requires:
  pip install playwright
  playwright install chromium

Usage (from repo root, with Streamlit already on port 8000):
  python scripts/md_filter_e2e_playwright.py
  BASE_URL=http://127.0.0.1:8000 python scripts/md_filter_e2e_playwright.py

Screenshots are written to scripts/_playwright_md_out/
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
OUT = Path(__file__).resolve().parent / "_playwright_md_out"
START_URL = (
    f"{BASE}/market_data?ticker=AAPL&tab=key_stats&period_type=Annual"
)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("Install Playwright: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = _ts()
    print(f"server_time_utc={stamp} base={BASE}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.set_default_timeout(120_000)

        def shot(name: str) -> None:
            path = OUT / f"{stamp}_{name}.png"
            page.screenshot(path=str(path), full_page=True)
            print(f"screenshot {path}")

        def wait_main_ready() -> None:
            page.wait_for_selector('[data-testid="stMain"]', state="attached", timeout=120_000)
            # Filter row uses st.selectbox — wait until visible (markdown alone is not enough).
            page.wait_for_selector(
                '[data-testid="stMain"] [data-testid="stSelectbox"]',
                state="visible",
                timeout=120_000,
            )

        # --- Load Annual ---
        page.goto(START_URL, wait_until="domcontentloaded")
        wait_main_ready()
        shot("01_after_annual_load")

        # Filter row contains "Start Date" / "End Date" — Period Type is the first selectbox in that row.
        filter_row = page.locator('[data-testid="stHorizontalBlock"]').filter(
            has_text="Start Date"
        ).filter(has_text="End Date").first
        filter_row.wait_for(state="visible", timeout=60_000)
        frow_boxes = filter_row.locator('[data-testid="stSelectbox"]')
        print(f"filter_row_selectbox_count={frow_boxes.count()}")
        if frow_boxes.count() < 1:
            shot("02_error_no_filter_row_selectbox")
            browser.close()
            return 1

        frow_boxes.nth(0).click()
        try:
            # Streamlit / BaseWeb: options are often role=option inside a portal menu
            page.get_by_role("option", name="Quarterly", exact=True).click(timeout=5_000)
        except PWTimeout:
            try:
                page.locator('[data-baseweb="popover"] li', has_text="Quarterly").first.click(timeout=10_000)
            except PWTimeout:
                try:
                    page.locator("li", has_text="Quarterly").first.click(timeout=10_000)
                except PWTimeout:
                    shot("02_error_no_quarterly_option")
                    browser.close()
                    return 1

        page.wait_for_timeout(1500)
        wait_main_ready()
        shot("03_after_quarterly_select")

        # --- Full refresh, then change period again ---
        page.reload(wait_until="domcontentloaded")
        wait_main_ready()
        shot("04_after_reload_still_quarterly_url")

        filter_row2 = page.locator('[data-testid="stHorizontalBlock"]').filter(
            has_text="Start Date"
        ).filter(has_text="End Date").first
        boxes2 = filter_row2.locator('[data-testid="stSelectbox"]')
        boxes2.nth(0).click()
        try:
            page.get_by_role("option", name="Annual", exact=True).click(timeout=5_000)
        except PWTimeout:
            page.locator('[data-baseweb="popover"] li', has_text="Annual").first.click(timeout=10_000)
        page.wait_for_timeout(1500)
        wait_main_ready()
        shot("05_after_annual_post_reload")

        boxes2.nth(0).click()
        try:
            page.get_by_role("option", name="Quarterly", exact=True).click(timeout=5_000)
        except PWTimeout:
            page.locator('[data-baseweb="popover"] li', has_text="Quarterly").first.click(timeout=10_000)
        page.wait_for_timeout(2000)
        wait_main_ready()
        shot("06_after_quarterly_second_time")

        browser.close()
    print("OK — see screenshots under", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
