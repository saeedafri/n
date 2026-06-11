#!/usr/bin/env python3
"""E2E Playwright tests for Company Screening Key Stats / Ratios enhancement."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = PROJECT_ROOT / "test-artifacts"
sys.path.insert(0, str(PROJECT_ROOT / "app"))


def _save(page, name: str) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(ARTIFACTS / name), full_page=True)
    print(f"  saved {ARTIFACTS / name}")


def _login_if_needed(page, base_url: str) -> None:
    username = os.getenv("LOGIN_USERNAME", "mohdsaeedafri@coresight.com")
    password = os.getenv("LOGIN_PASSWORD", "Welcome@123")

    page.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(2000)
    if page.locator(".logout-btn, button:has-text('Logout')").count():
        return
    if page.get_by_text("Screen For", exact=False).count():
        return
    if os.getenv("E2E_SKIP_AUTH", "").strip() == "1":
        return

    if page.locator("#csr-sso-btn").count():
        try:
            page.locator("#csr-sso-btn").click(force=True, timeout=30_000)
        except Exception:
            page.evaluate(
                "document.getElementById('csr-sso-btn') && document.getElementById('csr-sso-btn').click()"
            )
        try:
            page.wait_for_url("**stage3.coresight.com/**", timeout=60_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)

    for sel in ("input[name='log']", "input[type='email']", "#user_login"):
        if page.locator(sel).count():
            page.locator(sel).first.fill(username)
            break
    for sel in ("input[name='pwd']", "input[type='password']", "#user_pass"):
        if page.locator(sel).count():
            page.locator(sel).first.fill(password)
            break
    for sel in ("#wp-submit", "button[type='submit']", "input[type='submit']"):
        if page.locator(sel).count():
            page.locator(sel).first.click()
            break

    page.wait_for_selector(".logout-btn, button:has-text('Logout')", timeout=180_000)
    page.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector(".logout-btn, button:has-text('Logout')", timeout=120_000)


def _goto_screening(page, base_url: str) -> None:
    page.goto(f"{base_url.rstrip('/')}/screening", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(3000)
    if page.get_by_text("Screen For", exact=False).count() == 0:
        for sel in ("a:has-text('Screening')", "button:has-text('Screening')"):
            if page.locator(sel).count():
                page.locator(sel).first.click()
                page.wait_for_timeout(3000)
                break


def _fin_expander(page):
    page.wait_for_selector('[data-testid="stMain"]', state="attached", timeout=120_000)
    return page.locator('[data-testid="stExpander"]').filter(has_text="Financial Information").first


def _select_in_expander(page, expander, box_index: int, option_text: str) -> None:
    boxes = expander.locator('[data-testid="stSelectbox"]')
    boxes.nth(box_index).click()
    try:
        page.get_by_role("option", name=option_text, exact=True).click(timeout=5_000)
    except PWTimeout:
        try:
            page.locator('[data-baseweb="popover"] li', has_text=option_text).first.click(timeout=5_000)
        except PWTimeout:
            page.locator("li", has_text=option_text).first.click(timeout=5_000)
    page.wait_for_timeout(600)


def _assert_no_additional_data(expander) -> None:
    body = expander.inner_text()
    assert "Additional Data" not in body, "Additional Data should be hidden for Key Stats/Ratios"
    assert "Credit Ratings" not in body, "Credit Ratings should be hidden for Key Stats/Ratios"
    assert "Store Counts" not in body, "Store Counts should be hidden for Key Stats/Ratios"


def _assert_additional_data_visible(expander) -> None:
    body = expander.inner_text()
    assert "Additional Data" in body, "Additional Data should remain for normal statements"
    assert "Credit Ratings" in body
    assert "Store Counts" in body


def _assert_no_inner_form_border(expander) -> None:
    """Financial form must not render Streamlit's bordered form container."""
    forms = expander.locator('[data-testid="stForm"]')
    assert forms.count() > 0, "expected financial st.form inside expander"
    border_px = forms.first.evaluate(
        """(el) => {
            const s = window.getComputedStyle(el);
            const w = ['Top', 'Right', 'Bottom', 'Left'].map(
                (side) => parseFloat(s['border' + side + 'Width'] || '0')
            );
            return Math.max(...w);
        }"""
    )
    assert border_px == 0, f"financial form should be borderless, border={border_px}px"


def _open_financial_form(page) -> None:
    exp = page.locator('[data-testid="stExpander"]').filter(has_text="Financial Information")
    if exp.count() and exp.first.locator('[data-testid="stSelectbox"]').count():
        return
    page.get_by_role("button", name="Financial Information", exact=True).click(timeout=30_000)
    page.wait_for_selector(
        '[data-testid="stExpander"]:has-text("Financial Information") [data-testid="stSelectbox"]',
        timeout=60_000,
    )
    page.wait_for_timeout(1000)


def _add_financial_criterion(
    page,
    statement: str,
    metric: str,
    period_type: str = "FY",
    year: str = "2024",
    operator: str = "Greater Than",
    value: float = 1000.0,
) -> None:
    _open_financial_form(page)
    exp = _fin_expander(page)
    _select_in_expander(page, exp, 0, statement)
    _select_in_expander(page, exp, 1, metric)
    _select_in_expander(page, exp, 2, period_type)
    boxes = exp.locator('[data-testid="stSelectbox"]')
    boxes.nth(3).click()
    try:
        page.get_by_role("option", name=year, exact=True).click(timeout=3_000)
    except PWTimeout:
        page.locator('[data-baseweb="popover"] li', has_text="Latest").first.click(timeout=5_000)
    page.wait_for_timeout(400)
    _select_in_expander(page, exp, 4, operator)
    val_input = exp.locator('input[type="number"]').first
    val_input.click()
    val_input.fill(str(value))
    exp.get_by_role("button", name="Add Criteria").click(force=True)
    page.wait_for_timeout(2000)
    page.wait_for_selector(f"text={statement} /", timeout=120_000)
    page.wait_for_timeout(3000)


def _show_results(page) -> None:
    page.get_by_role("button", name="Show Results").click()
    page.wait_for_timeout(6000)


def _assert_compact_columns(page) -> None:
    """Results must be Company Name + metric columns only."""
    headers = page.locator('[data-testid="stDataFrame"] th').all_inner_texts()
    headers = [h.strip() for h in headers if h.strip()]
    forbidden = {"Ticker", "Exchange", "Sector", "Country"}
    assert headers, f"expected dataframe headers, got {headers!r}"
    assert headers[0] == "Company Name", f"first col must be Company Name, got {headers!r}"
    assert not forbidden.intersection(headers), f"forbidden cols in table: {headers!r}"
    for h in headers[1:]:
        assert "(" in h and "[" in h, f"metric header should include unit/period: {h!r}"


def test_financial_no_inner_border_all_statements(page, base_url: str) -> None:
    print("\n[0] All statement types — borderless form")
    cases = [
        ("Income Statement", "income-statement-no-inner-border.png", True),
        ("Balance Sheet", "balance-sheet-no-inner-border.png", True),
        ("Cash Flow", "cash-flow-no-inner-border.png", True),
        ("Key Stats", "key-stats-no-inner-border.png", False),
        ("Ratios", "ratios-no-inner-border.png", False),
    ]
    _goto_screening(page, base_url)
    _open_financial_form(page)
    exp = _fin_expander(page)
    for statement, screenshot, expect_additional in cases:
        _select_in_expander(page, exp, 0, statement)
        page.wait_for_timeout(500)
        _assert_no_inner_form_border(exp)
        if expect_additional:
            _assert_additional_data_visible(exp)
        else:
            _assert_no_additional_data(exp)
        _save(page, screenshot)
        print(f"  {statement}: borderless OK, additional={expect_additional}")


def test_key_stats_form(page, base_url: str) -> None:
    print("\n[A] Key Stats form + results")
    _goto_screening(page, base_url)
    _open_financial_form(page)
    exp = _fin_expander(page)
    _select_in_expander(page, exp, 0, "Key Stats")
    _select_in_expander(page, exp, 1, "Growth Over Prior Year")
    body = exp.inner_text()
    assert "Step 3 — Set Period Type" in body
    assert "Step 4 — Set Operator" in body
    assert "Step 5 — Set Period Type" not in body
    assert "Geographical Location" not in body
    _assert_no_additional_data(exp)
    _assert_no_inner_form_border(exp)
    _save(page, "key-stats-no-additional-data.png")
    _save(page, "screening-key-stats-form.png")

    _select_in_expander(page, exp, 2, "FY")
    boxes = exp.locator('[data-testid="stSelectbox"]')
    boxes.nth(3).click()
    try:
        page.locator('[data-baseweb="popover"] li', has_text="Latest").first.click(timeout=3_000)
    except PWTimeout:
        page.get_by_role("option", name="2024", exact=True).click(timeout=5_000)
    page.wait_for_timeout(400)
    val_input = exp.locator('input[type="number"]').first
    val_input.click()
    val_input.fill("0")
    exp.get_by_role("button", name="Add Criteria").click(force=True)
    page.wait_for_timeout(2000)
    page.wait_for_selector("text=Growth Over Prior Year", timeout=120_000)
    page.wait_for_timeout(3000)
    t0 = time.perf_counter()
    _show_results(page)
    show_ms = (time.perf_counter() - t0) * 1000
    print(f"  Show Results wait {show_ms:.0f}ms")
    page.wait_for_selector('[data-testid="stDataFrame"]', timeout=120_000)
    _assert_compact_columns(page)
    _save(page, "screening-key-stats-results.png")


def test_ratios_form_ui(page, base_url: str) -> None:
    print("\n[B1] Ratios form UI (no Additional Data)")
    _goto_screening(page, base_url)
    _open_financial_form(page)
    exp = _fin_expander(page)
    _select_in_expander(page, exp, 0, "Ratios")
    _select_in_expander(page, exp, 1, "Current Ratio")
    _assert_no_additional_data(exp)
    _assert_no_inner_form_border(exp)
    _save(page, "ratios-no-additional-data.png")


def test_ratios_form(page, base_url: str) -> None:
    print("\n[B2] Ratios form + results")
    _goto_screening(page, base_url)
    _add_financial_criterion(page, "Ratios", "Current Ratio", year="2024", value=1.0)
    _show_results(page)
    page.wait_for_selector('[data-testid="stDataFrame"]', timeout=120_000)
    _assert_compact_columns(page)
    _save(page, "screening-ratios-form.png")
    _save(page, "screening-ratios-results.png")


def test_income_statement_regression(page, base_url: str) -> None:
    print("\n[C] Income Statement regression")
    _goto_screening(page, base_url)
    _open_financial_form(page)
    exp = _fin_expander(page)
    _select_in_expander(page, exp, 0, "Income Statement")
    body = exp.inner_text()
    assert "Geographical Location" in body
    assert "Step 5 — Set Period Type" in body
    _assert_additional_data_visible(exp)
    _assert_no_inner_form_border(exp)
    _save(page, "income-statement-additional-data-still-visible.png")
    _save(page, "screening-income-statement-regression.png")


def test_market_data_value_match(page, base_url: str) -> None:
    print("\n[D] Market Data vs Screening value match (Macy's)")
    ticker = "M"
    page.goto(
        f"{base_url.rstrip('/')}/market_data?ticker={ticker}&tab=key_stats&period_type=Annual",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_selector('[data-testid="stMain"]', state="attached", timeout=120_000)
    page.wait_for_timeout(8000)
    _save(page, "marketdata-key-stats-reference.png")

    _goto_screening(page, base_url)
    page.get_by_role("button", name="Clear All").click(force=True)
    page.wait_for_timeout(2000)
    _add_financial_criterion(page, "Key Stats", "Total Revenue", year="2024", value=1)
    _show_results(page)
    _save(page, "screening-key-stats-value-match.png")

    page.goto(
        f"{base_url.rstrip('/')}/market_data?ticker={ticker}&tab=ratios&period_type=Annual",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_selector('[data-testid="stMain"]', state="attached", timeout=120_000)
    page.wait_for_timeout(8000)
    _save(page, "marketdata-ratios-reference.png")

    _goto_screening(page, base_url)
    page.get_by_role("button", name="Clear All").click(force=True)
    page.wait_for_timeout(3000)
    _add_financial_criterion(page, "Ratios", "Current Ratio", year="2024", value=0.5)
    _show_results(page)
    _save(page, "screening-ratios-value-match.png")


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("E2E_BASE_URL", "http://localhost:8502").strip().rstrip("/")
    headless = os.getenv("E2E_HEADLESS", "1") != "0"

    rc = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        try:
            _login_if_needed(page, base_url)
            test_financial_no_inner_border_all_statements(page, base_url)
            test_key_stats_form(page, base_url)
            test_ratios_form_ui(page, base_url)
            test_ratios_form(page, base_url)
            test_income_statement_regression(page, base_url)
            test_market_data_value_match(page, base_url)
            print("\nAll screening E2E steps completed.")
        except Exception as exc:
            rc = 1
            print(f"\nE2E FAILED: {exc}")
            _save(page, "screening-e2e-failure.png")
        finally:
            context.close()
            browser.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
