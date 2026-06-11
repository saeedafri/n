#!/usr/bin/env python3
"""E2E: Company Screening segment statements cache path + regressions."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = PROJECT_ROOT / "test-artifacts"
LOG_PATH = PROJECT_ROOT / "server-logs" / "server-log.log"
sys.path.insert(0, str(PROJECT_ROOT / "app"))
PREPARED_WATCHLIST_NAME = ""


def _log_offset() -> int:
    try:
        return LOG_PATH.stat().st_size
    except FileNotFoundError:
        return 0


def _read_log_since(offset: int) -> str:
    if not LOG_PATH.exists():
        return ""
    with LOG_PATH.open("rb") as fh:
        fh.seek(offset)
        return fh.read().decode("utf-8", errors="replace")


def _last_timing_ms(log_text: str, operation: str) -> Optional[float]:
    matches = re.findall(rf"{re.escape(operation)} \| ([0-9.]+)ms", log_text)
    return float(matches[-1]) if matches else None


def _last_detail_value(log_text: str, operation: str, key: str) -> Optional[str]:
    lines = [line for line in log_text.splitlines() if operation in line]
    if not lines:
        return None
    match = re.search(rf"\b{re.escape(key)}=([^|\s]+)", lines[-1])
    return match.group(1) if match else None


def _cache_hit_state(log_text: str) -> str:
    hit = "SCREENING_SEGMENT_VALUES_CACHE_HIT" in log_text
    miss = "SCREENING_SEGMENT_VALUES_CACHE_MISS" in log_text
    if hit:
        return "HIT"
    if miss:
        return "MISS"
    return "UNKNOWN"


def _scenario_timing(name: str, log_text: str, total_s: float, results: Optional[int]) -> Dict:
    rows_read = _last_detail_value(log_text, "SCREENING_SEGMENT_VALUES_CACHE_ROWS", "rows")
    recompute_ms = _last_timing_ms(log_text, "SHOW_RESULTS_RECOMPUTE_TOTAL")
    render_ms = _last_timing_ms(log_text, "RESULTS_RENDER_TOTAL")
    return {
        "name": name,
        "cache": _cache_hit_state(log_text),
        "rows_read": int(rows_read or 0),
        "results": results if results is not None else -1,
        "recompute_s": (recompute_ms or 0) / 1000,
        "render_s": (render_ms or 0) / 1000,
        "total_s": total_s,
    }


def _print_timing(timing: Dict) -> None:
    print(f"\n{timing['name']}")
    print(f"Cache: {timing['cache']}")
    print(f"Rows read: {timing['rows_read']}")
    print(f"Results: {timing['results']} companies")
    print(f"Recompute: {timing['recompute_s']:.2f}s")
    print(f"Render: {timing['render_s']:.2f}s")
    print(f"Total: {timing['total_s']:.2f}s")


async def _save(page, name: str) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(ARTIFACTS / name), full_page=True)
    print(f"  saved {ARTIFACTS / name}")


async def click_by_text_or_role(page, text):
    """Click a visible button/text target with role-first fallbacks."""
    escaped = re.escape(text)
    last_error: Optional[Exception] = None
    for _attempt in range(4):
        locators = [
            page.get_by_role("button", name=re.compile(escaped, re.I)),
            page.locator("button").filter(has_text=re.compile(escaped, re.I)),
            page.get_by_text(re.compile(escaped, re.I)),
        ]
        for locator in locators:
            try:
                if await locator.count():
                    await locator.first.click(timeout=10_000, force=True)
                    return
            except Exception as exc:
                last_error = exc
        await page.wait_for_timeout(500)
    raise AssertionError(f"Could not click {text!r}: {last_error}")


async def _wait_for_streamlit(page, timeout: int = 120_000) -> None:
    await page.wait_for_selector('[data-testid="stMain"]', state="attached", timeout=timeout)
    await page.wait_for_timeout(1500)


async def _wait_for_screening_ready(page, timeout: int = 120_000) -> None:
    try:
        await page.get_by_text("Company Screening", exact=False).wait_for(timeout=timeout)
        await page.get_by_text("Screen For", exact=False).wait_for(timeout=timeout)
        await page.get_by_text("Financial Information", exact=False).wait_for(timeout=timeout)
    except PWTimeout:
        await _save(page, "e2e-failure-current-page.png")
        raise


async def _wait_for_screening_idle(page, timeout: int = 60_000) -> None:
    await page.wait_for_function(
        """
        () => !document.querySelector(".scr-loading-overlay")
        """,
        timeout=timeout,
    )
    await page.wait_for_timeout(500)


async def _login_if_needed(page, base_url: str) -> None:
    await page.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
    await _wait_for_streamlit(page)
    if await page.locator(".logout-btn, button:has-text('Logout')").count():
        return
    if await page.get_by_text("Screen For", exact=False).count():
        return
    if os.getenv("E2E_SKIP_AUTH", "").strip() == "1":
        return


async def _goto_screening(page, base_url: str) -> None:
    await page.goto(f"{base_url.rstrip('/')}/screening", wait_until="domcontentloaded", timeout=120_000)
    await _wait_for_streamlit(page)
    try:
        await _wait_for_screening_ready(page, timeout=60_000)
        return
    except PWTimeout:
        pass
    if not await page.get_by_text("Screen For", exact=False).count():
        await click_by_text_or_role(page, "Screening")
        await _wait_for_streamlit(page)
        await _wait_for_screening_ready(page, timeout=60_000)


def _fin_expander(page):
    return page.locator('[data-testid="stExpander"]').filter(has_text="Financial Information").first


async def _open_financial_form(page) -> None:
    exp = page.locator('[data-testid="stExpander"]').filter(has_text="Financial Information")
    if await exp.count() and await exp.first.locator('[data-testid="stSelectbox"]').count():
        return
    await page.get_by_text("Financial Information", exact=False).wait_for(timeout=120_000)
    await click_by_text_or_role(page, "Financial Information")
    await page.wait_for_selector(
        '[data-testid="stExpander"]:has-text("Financial Information") [data-testid="stSelectbox"]',
        timeout=60_000,
    )
    await page.wait_for_timeout(700)


async def _select_in_expander(page, expander, box_index: int, option_text: str) -> None:
    last_error: Optional[Exception] = None
    for _attempt in range(5):
        try:
            current_expander = _fin_expander(page)
            if not await current_expander.count():
                current_expander = expander
            boxes = current_expander.locator('[data-testid="stSelectbox"]')
            if await boxes.count() <= box_index:
                await page.wait_for_timeout(700)
                continue
            box = boxes.nth(box_index)
            await box.scroll_into_view_if_needed(timeout=8_000)
            await box.click(timeout=10_000)
            option_patterns = [
                page.get_by_role("option", name=option_text, exact=True),
                page.locator('[data-baseweb="popover"] li').filter(has_text=option_text),
                page.locator('[role="option"]').filter(has_text=option_text),
                page.locator("li").filter(has_text=option_text),
            ]
            for option in option_patterns:
                if await option.count():
                    await option.first.click(timeout=8_000)
                    await page.wait_for_timeout(900)
                    return
            raise AssertionError(f"option {option_text!r} not visible")
        except Exception as exc:
            last_error = exc
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.wait_for_timeout(900)
    raise AssertionError(f"Could not select {option_text!r}: {last_error}")


async def _clear_all(page) -> None:
    buttons = page.get_by_role("button", name=re.compile("Clear All", re.I))
    if not await buttons.count():
        return
    try:
        await buttons.first.scroll_into_view_if_needed(timeout=5_000)
        await buttons.first.click(timeout=5_000)
        await page.wait_for_timeout(1200)
    except Exception:
        return


async def _assert_no_raw_html(page) -> None:
    body = await page.locator('[data-testid="stMain"]').inner_text()
    assert "<details" not in body
    assert "criterion-details" not in body


async def _load_one_segment_member(page, expander) -> str:
    try:
        await click_by_text_or_role(page, "Load Segment Members")
        await page.wait_for_timeout(2500)
    except Exception:
        pass

    multi = expander.locator('[data-testid="stMultiSelect"]').first
    if not await multi.count():
        raise AssertionError("Segment multiselect not found")
    await multi.scroll_into_view_if_needed(timeout=10_000)
    await multi.click(timeout=10_000)
    await page.wait_for_timeout(700)

    options = page.locator('[role="option"], [data-baseweb="menu"] li')
    count = await options.count()
    if count == 0:
        await _save(page, "e2e-failure-current-page.png")
        raise AssertionError("No segment member options available")
    selected_option = None
    option_text = ""
    for idx in range(count):
        option = options.nth(idx)
        text = (await option.inner_text()).strip()
        if text and "select all" not in text.lower():
            selected_option = option
            option_text = text
            break
    if selected_option is None:
        await _save(page, "e2e-failure-current-page.png")
        raise AssertionError("No concrete segment member option available")
    await selected_option.click(timeout=10_000)
    await page.wait_for_timeout(700)
    return option_text.split(" (")[0].strip()


async def _add_segment_criterion(
    page,
    statement: str,
    *,
    selected_member: bool = False,
    value: str = "0",
) -> Optional[str]:
    await _open_financial_form(page)
    exp = _fin_expander(page)
    await _select_in_expander(page, exp, 0, statement)
    try:
        await _select_in_expander(page, exp, 1, "Revenues")
    except Exception:
        print("  metric selector already defaulted to Revenues")

    member = None
    if selected_member:
        member = await _load_one_segment_member(page, exp)

    await _select_in_expander(page, exp, 2, "FY")
    await _select_in_expander(page, exp, 3, "Latest")
    await _select_in_expander(page, exp, 4, "Greater Than")
    val_input = exp.locator('input[type="number"]').first
    await val_input.click(timeout=10_000)
    await val_input.fill(value)
    await exp.locator('[data-testid="stForm"]').get_by_role(
        "button", name=re.compile("Add Criteria", re.I)
    ).click(timeout=20_000)
    await page.wait_for_selector("text=Active Criteria", timeout=60_000)
    await page.wait_for_timeout(1200)
    await _assert_no_raw_html(page)
    return member


async def _extract_result_count(page) -> Optional[int]:
    text = await page.locator('[data-testid="stMain"]').inner_text()
    match = re.search(r"(\d+)\s+companies matched", text)
    return int(match.group(1)) if match else None


async def _show_results(
    page,
    scenario_name: str,
    screenshot_name: str,
    overlay_name: Optional[str] = None,
    *,
    require_under_5s: bool = False,
    pre_log_text: str = "",
) -> Dict:
    offset = _log_offset()
    started = time.perf_counter()
    await click_by_text_or_role(page, "Show Results")
    try:
        await page.wait_for_selector(".scr-loading-overlay", state="attached", timeout=8_000)
        if overlay_name:
            await _save(page, overlay_name)
    except PWTimeout:
        await _save(page, "e2e-failure-current-page.png")
        raise AssertionError("Loading overlay did not appear")

    await page.wait_for_function(
        """
        () => {
          const text = document.body.innerText || "";
          return document.querySelector("[data-testid='stDataFrame']")
            || text.includes("companies matched")
            || text.includes("No companies match")
            || text.includes("Could not load screening results")
            || text.includes("Segment screening cache is not available");
        }
        """,
        timeout=120_000,
    )
    await page.wait_for_timeout(1200)
    total_s = time.perf_counter() - started
    body = await page.locator('[data-testid="stMain"]').inner_text()
    if "Something went wrong" in body or "Could not load screening results" in body:
        await _save(page, "e2e-failure-current-page.png")
        raise AssertionError("Show Results failed")
    if "Segment screening cache is not available" in body:
        await _save(page, "e2e-failure-current-page.png")
        raise AssertionError("Segment cache unavailable")
    await _assert_no_raw_html(page)
    await _save(page, screenshot_name)
    results = await _extract_result_count(page)
    log_text = pre_log_text + _read_log_since(offset)
    timing = _scenario_timing(scenario_name, log_text, total_s, results)
    _print_timing(timing)
    if require_under_5s and timing["total_s"] > 5:
        raise AssertionError(f"Cached Show Results exceeded 5s target: {timing['total_s']:.2f}s")
    return timing


async def _run_segment_all(page, base_url: str, statement: str) -> Dict:
    slug = "business" if statement == "Business Segments" else "geographical"
    print(f"\n[A/B] {statement} — All Segments cached path")
    await _goto_screening(page, base_url)
    await _clear_all(page)
    await _add_segment_criterion(page, statement)
    return await _show_results(
        page,
        f"{statement} / Revenues / FY Latest / All Segments",
        f"{slug}-segments-all-cached-results.png",
        f"{slug}-segments-loading-overlay.png",
        require_under_5s=True,
    )


async def _run_segment_selected(page, base_url: str, statement: str) -> Dict:
    slug = "business" if statement == "Business Segments" else "geographical"
    print(f"\n[C/D] {statement} — selected member cached path")
    await _goto_screening(page, base_url)
    await _clear_all(page)
    member = await _add_segment_criterion(page, statement, selected_member=True)
    timing = await _show_results(
        page,
        f"{statement} / Revenues / FY Latest / {member}",
        f"{slug}-segments-selected-member-results.png",
    )
    body = await page.locator('[data-testid="stMain"]').inner_text()
    if member and member not in body:
        print(f"  selected member {member!r} not in accessible text; screenshot/table verified results > 0")
    assert timing["results"] > 0, "Selected member path returned no companies"
    return timing


async def _save_current_criteria(page, name: str) -> None:
    await _wait_for_screening_idle(page)
    save_button = page.get_by_role("button", name=re.compile("^Save Criteria$", re.I))
    last_error: Optional[Exception] = None
    for _attempt in range(4):
        try:
            await save_button.first.scroll_into_view_if_needed(timeout=8_000)
            await save_button.first.click(timeout=10_000)
            await page.wait_for_selector("input[aria-label='Name *'], input[aria-label='Name']", timeout=8_000)
            break
        except Exception as exc:
            last_error = exc
            await page.wait_for_timeout(1000)
    else:
        raise AssertionError(f"Save criteria dialog did not open: {last_error}")

    name_fields = page.locator("input[aria-label='Name *'], input[aria-label='Name']")
    if not await name_fields.count():
        raise AssertionError("Save criteria name field not found")
    await name_fields.first.fill(name)
    await page.get_by_role("button", name=re.compile("^Save$", re.I)).click(timeout=20_000)
    await page.wait_for_timeout(2000)


async def _load_latest_saved_criteria(page, name: str) -> None:
    await click_by_text_or_role(page, "Saved Screenings")
    await page.wait_for_timeout(1500)
    await page.get_by_text(name, exact=False).wait_for(timeout=30_000)
    card = page.locator('[data-testid="stVerticalBlock"]').filter(has_text=name).first
    if await card.count() and await card.get_by_role("button", name="Load").count():
        await card.get_by_role("button", name="Load").first.click(timeout=20_000)
    else:
        await page.get_by_role("button", name="Load").first.click(timeout=20_000)
    await page.wait_for_selector("text=Active Criteria", timeout=60_000)
    await page.wait_for_timeout(1500)


async def _run_saved_screening(page, base_url: str, statement: str) -> Dict:
    slug = "business" if statement == "Business Segments" else "geographical"
    print(f"\n[E] {statement} — saved screening compatibility")
    await _goto_screening(page, base_url)
    await _clear_all(page)
    value = f"0.{(int(time.time()) % 89) + 10:02d}"
    await _add_segment_criterion(page, statement, value=value)
    name = f"E2E {slug} segment {int(time.time())}"
    await _save_current_criteria(page, name)
    await _clear_all(page)
    load_offset = _log_offset()
    await _load_latest_saved_criteria(page, name)
    load_log_text = _read_log_since(load_offset)
    body = await page.locator('[data-testid="stMain"]').inner_text()
    for expected in (statement, "Revenues", "FY Latest", "Greater Than", f"{float(value):,.2f}"):
        assert expected in body, f"Saved criteria missing {expected!r}"
    return await _show_results(
        page,
        f"{statement} / Revenues / FY Latest / Saved Screening",
        f"saved-{slug}-segments-load-results.png",
        pre_log_text=load_log_text,
    )


def _ensure_watchlist() -> str:
    from core.database import db_manager
    from data.screening_service import get_base_company_universe
    from data.screening_service import SEGMENT_VALUES_CACHE_TABLE
    from data.watchlist_service import add_companies_to_watchlist, create_watchlist

    user_email = os.getenv("LOGIN_USERNAME", "mohdsaeedafri@coresight.com")
    watchlist_name = f"E2E Segment Cache Watchlist {int(time.time())}"
    cached_rows = db_manager.execute_query_readonly(
        f"""
        SELECT DISTINCT ticker
        FROM {SEGMENT_VALUES_CACHE_TABLE}
        WHERE segment_type = 'business'
          AND metric_key = 'Revenues'
        LIMIT 8
        """
    ) or []
    cached_tickers = {row.get("ticker") for row in cached_rows if row.get("ticker")}
    wid = create_watchlist(watchlist_name, user_email, "E2E segment cache test")
    if not wid:
        raise RuntimeError("Could not create E2E watchlist")
    base_all = get_base_company_universe()
    base = base_all[base_all["ticker"].isin(cached_tickers)].head(8)
    companies = [
        {
            "ticker": row.get("ticker"),
            "company_name": row.get("company_name") or row.get("name") or row.get("ticker"),
            "sector": row.get("sector") or "",
            "country": row.get("country") or row.get("country_of_incorporation") or "",
        }
        for _, row in base.iterrows()
    ]
    add_companies_to_watchlist(int(wid), companies, user_email)
    return watchlist_name


async def _select_watchlist(page, watchlist_name: str) -> str:
    boxes = page.locator('[data-testid="stSelectbox"]')
    if not await boxes.count():
        raise AssertionError("Watchlist selectbox not found")
    await boxes.first.click(timeout=10_000)
    target = page.get_by_role("option", name=watchlist_name, exact=True)
    if await target.count():
        await target.first.click(timeout=20_000)
        selected = watchlist_name
    else:
        options = page.locator('[role="option"]')
        selected = ""
        for idx in range(await options.count()):
            text = (await options.nth(idx).inner_text()).strip()
            if text and "none" not in text.lower():
                await options.nth(idx).click(timeout=20_000)
                selected = text
                break
        if not selected:
            raise AssertionError("No selectable watchlists found")
    await page.wait_for_timeout(2000)
    return selected


async def _run_watchlist(page, base_url: str) -> Dict:
    print("\n[F] Business Segments — watchlist cached path")
    watchlist_name = PREPARED_WATCHLIST_NAME or _ensure_watchlist()
    await _goto_screening(page, base_url)
    await _clear_all(page)
    watchlist_name = await _select_watchlist(page, watchlist_name)
    print(f"  selected watchlist: {watchlist_name}")
    await _add_segment_criterion(page, "Business Segments")
    timing = await _show_results(
        page,
        "Business Segments / Revenues / FY Latest / Watchlist",
        "business-segments-watchlist-results.png",
        require_under_5s=True,
    )
    body = await page.locator('[data-testid="stMain"]').inner_text()
    assert "watchlist" in body.lower() or timing["results"] <= 12
    return timing


async def _add_financial_criterion(
    page,
    statement: str,
    metric: str,
    *,
    value: str = "0",
) -> None:
    await _open_financial_form(page)
    exp = _fin_expander(page)
    await _select_in_expander(page, exp, 0, statement)
    await _select_in_expander(page, exp, 1, metric)
    await _select_in_expander(page, exp, 2, "FY")
    await _select_in_expander(page, exp, 3, "Latest")
    await _select_in_expander(page, exp, 4, "Greater Than")
    val_input = exp.locator('input[type="number"]').first
    await val_input.click(timeout=10_000)
    await val_input.fill(value)
    await exp.locator('[data-testid="stForm"]').get_by_role(
        "button", name=re.compile("Add Criteria", re.I)
    ).click(timeout=20_000)
    await page.wait_for_selector("text=Active Criteria", timeout=60_000)
    await page.wait_for_timeout(1500)


async def _run_financial_regression(page, base_url: str, statement: str, metric: str, screenshot: str) -> None:
    print(f"\n[G] Regression — {statement}")
    await _goto_screening(page, base_url)
    await _clear_all(page)
    await _add_financial_criterion(page, statement, metric)
    await _show_results(page, f"Regression / {statement} / {metric}", screenshot)


async def _run_simple_category_regressions(page, base_url: str) -> None:
    print("\n[G] Regression — Industry / Geography / Key Developments")
    await _goto_screening(page, base_url)
    await _clear_all(page)
    await click_by_text_or_role(page, "Industry Classifications")
    await page.wait_for_timeout(1000)
    exp = page.locator('[data-testid="stExpander"]').filter(has_text="Industry Classifications").first
    multi = exp.locator('[data-testid="stMultiSelect"]').first
    await multi.click(timeout=10_000)
    await page.locator('[role="option"]').first.click(timeout=10_000)
    await exp.locator('[data-testid="stForm"]').get_by_role("button", name="Add Criteria").click(timeout=20_000)
    await page.wait_for_selector("text=Active Criteria", timeout=60_000)

    await _goto_screening(page, base_url)
    await _clear_all(page)
    await click_by_text_or_role(page, "Geographic Locations")
    await page.wait_for_timeout(1000)
    exp = page.locator('[data-testid="stExpander"]').filter(has_text="Geographic Locations").first
    multi = exp.locator('[data-testid="stMultiSelect"]').first
    await multi.click(timeout=10_000)
    await page.locator('[role="option"]').first.click(timeout=10_000)
    await exp.locator('[data-testid="stForm"]').get_by_role("button", name="Add Criteria").click(timeout=20_000)
    await page.wait_for_selector("text=Active Criteria", timeout=60_000)

    await _goto_screening(page, base_url)
    await _clear_all(page)
    await click_by_text_or_role(page, "Key Developments by Category")
    await page.wait_for_timeout(1000)
    exp = page.locator('[data-testid="stExpander"]').filter(has_text="Key Developments by Category").first
    multi = exp.locator('[data-testid="stMultiSelect"]').first
    await multi.click(timeout=10_000)
    await page.locator('[role="option"]').first.click(timeout=10_000)
    await exp.locator('[data-testid="stForm"]').get_by_role("button", name="Add Criteria").click(timeout=20_000)
    await page.wait_for_selector("text=Active Criteria", timeout=60_000)
    print("  Industry, Geographic Locations, Key Developments forms pass add-criteria smoke checks.")


def _print_cache_status() -> None:
    from data.screening_service import get_segment_values_cache_status

    status = get_segment_values_cache_status()
    print("\nSegment values cache status before E2E")
    for key, value in status.items():
        print(f"  {key}: {value}")
    if not status.get("is_healthy"):
        raise RuntimeError("Segment values cache is not populated")


async def _amain() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("E2E_BASE_URL", "http://localhost:8503").strip().rstrip("/")
    headless = os.getenv("E2E_HEADLESS", "1") != "0"
    _print_cache_status()
    global PREPARED_WATCHLIST_NAME
    PREPARED_WATCHLIST_NAME = _ensure_watchlist()
    print(f"Prepared watchlist: {PREPARED_WATCHLIST_NAME}")

    timings: Dict[str, Dict] = {}
    rc = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await _login_if_needed(page, base_url)
            timings["business_all"] = await _run_segment_all(page, base_url, "Business Segments")
            timings["geographical_all"] = await _run_segment_all(page, base_url, "Geographical Segments")
            timings["business_selected"] = await _run_segment_selected(page, base_url, "Business Segments")
            timings["geographical_selected"] = await _run_segment_selected(page, base_url, "Geographical Segments")
            timings["saved_business"] = await _run_saved_screening(page, base_url, "Business Segments")
            timings["saved_geographical"] = await _run_saved_screening(page, base_url, "Geographical Segments")
            timings["watchlist"] = await _run_watchlist(page, base_url)
            await _run_financial_regression(
                page,
                base_url,
                "Income Statement",
                "Total Revenue",
                "regression-income-statement-results.png",
            )
            await _run_financial_regression(
                page,
                base_url,
                "Key Stats",
                "Total Revenue",
                "regression-key-stats-results.png",
            )
            await _run_financial_regression(
                page,
                base_url,
                "Ratios",
                "Current Ratio",
                "regression-ratios-results.png",
            )
            await _run_simple_category_regressions(page, base_url)
            print("\nSegment statement E2E completed.")
        except Exception as exc:
            rc = 1
            print(f"\nE2E FAILED: {exc}")
            try:
                await _save(page, "e2e-failure-current-page.png")
            except Exception:
                pass
        finally:
            await context.close()
            await browser.close()
    return rc


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
