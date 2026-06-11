#!/usr/bin/env python3
"""
E2E: Forecasting page → Refresh Data popup → Q4 reporting dates.

Local (auth bypass via TEST_USER_EMAIL in main.py when DEBUG=true):
  DEBUG=true TEST_USER_EMAIL=mohdsaeedafri@coresight.com \\
    streamlit run app/main.py --server.port 8000

  python scripts/forecasting_refresh_e2e_playwright.py

STG (OIDC login):
  BASE_URL=https://marketdata-stg.coresight.com \\
  LOGIN_USERNAME=... LOGIN_PASSWORD=... \\
  python scripts/forecasting_refresh_e2e_playwright.py

Screenshots: scripts/_playwright_forecast_out/
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
OUT = Path(__file__).resolve().parent / "_playwright_forecast_out"
START_URL = f"{BASE}/forecasting?ticker=M"
IS_LOCAL = "localhost" in BASE or "127.0.0.1" in BASE


def _ts() -> str:
    env = "local" if IS_LOCAL else "stg"
    return f"{env}_{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}"


def _login_if_needed(page) -> None:
    if IS_LOCAL:
        return
    username = os.getenv("LOGIN_USERNAME", "mohdsaeedafri@coresight.com")
    password = os.getenv("LOGIN_PASSWORD", "")
    if not password:
        print("STG test requires LOGIN_PASSWORD in env", file=sys.stderr)
        raise RuntimeError("missing LOGIN_PASSWORD")
    page.goto(BASE, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector("#csr-sso-btn", timeout=120_000)
    page.locator("#csr-sso-btn").click()
    page.wait_for_url(re.compile(r"stage.*coresight|openid|authorize", re.I), timeout=120_000)
    page.fill('input[name="username"], input[type="email"]', username)
    page.fill('input[name="password"], input[type="password"]', password)
    page.locator('button[type="submit"], input[type="submit"]').first.click()
    page.wait_for_url(re.compile(r"marketdata", re.I), timeout=180_000)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = _ts()
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900}, ignore_https_errors=True)
        page = context.new_page()
        page.set_default_timeout(180_000)

        def shot(name: str) -> Path:
            path = OUT / f"{stamp}_{name}.png"
            page.screenshot(path=str(path), full_page=True)
            print(f"screenshot {path}")
            return path

        _login_if_needed(page)

        if IS_LOCAL:
            page.goto(f"{BASE}/home", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

        page.goto(START_URL, wait_until="domcontentloaded")
        page.wait_for_selector('[data-testid="stMain"]', state="attached", timeout=180_000)
        page.wait_for_selector("text=Revenue", timeout=180_000)
        shot("01_page_loaded")

        refresh_btn = page.get_by_role("button", name=re.compile(r"Refresh Data", re.I))
        refresh_btn.wait_for(state="visible", timeout=180_000)
        shot("02_refresh_data_button_visible")

        refresh_btn.click()
        page.wait_for_selector('[data-testid="stDialog"]', state="visible", timeout=180_000)
        # Q4 bulk lookup can take 15–20s on first open (cold DB + cache miss)
        page.wait_for_selector("text=Reporting Date", timeout=180_000)
        page.wait_for_timeout(5000)
        shot("03_popup_opened")

        dialog = page.locator('[data-testid="stDialog"]')
        dialog_text = dialog.inner_text()
        dialog_upper = dialog_text.upper()
        if "REPORTING DATE" not in dialog_upper:
            errors.append("Missing header 'Reporting Date'")
        if "FISCAL ENDING" not in dialog_upper:
            errors.append("Missing header 'Fiscal Ending'")
        if "ANNUAL REPORTED ON" in dialog_upper:
            errors.append("Old header 'Annual Reported On' still present")
        if "FISCAL PERIOD" in dialog_upper and "FISCAL ENDING" not in dialog_upper:
            errors.append("Old header 'Fiscal Period' still present")
        shot("04_popup_headers")

        for tk in ["1913.HK", "2020.HK", "3382.T", "9983.T", "M"]:
            if tk not in dialog_text:
                errors.append(f"Ticker {tk} not found in popup")
        shot("05_popup_top_rows")

        if "1913.HK" in dialog_text:
            shot("06_row_1913_HK")
        m_row = dialog.locator("text=1913.HK").locator("xpath=ancestor::div[contains(@data-testid,'stVerticalBlock')][1]")
        page.keyboard.press("End")
        page.wait_for_timeout(1000)
        if "Macy" in dialog_text or dialog.get_by_text("Macy", exact=False).count():
            shot("07_row_M")
        elif dialog.get_by_text(re.compile(r"\\bM\\b")).count():
            shot("07_row_M")

        quarterly = dialog.get_by_text("Quarterly", exact=True)
        if quarterly.count():
            quarterly.first.click()
            page.wait_for_timeout(1500)
            if "Quarterly forecast refresh is not yet supported" not in dialog.inner_text():
                errors.append("Quarterly unsupported message missing")
            shot("08_quarterly_unsupported")

        if page.locator('[data-testid="stException"]').count():
            errors.append("Streamlit exception visible on page")
            shot("99_exception")

        browser.close()

    if errors:
        print("FAILURES:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
