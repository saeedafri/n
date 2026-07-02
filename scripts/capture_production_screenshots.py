#!/usr/bin/env python3
"""Capture business-documentation screenshots from production MDP.

Credentials via environment only (never written to repo):
  MDP_BASE_URL   default https://marketdata.coresight.com
  MDP_EMAIL      primary login email
  MDP_PASSWORD   login password
  MDP_EMAIL_ALT  alternate email if primary fails
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from business_screenshot_callouts import CALLOUTS_BY_FILE  # noqa: E402

OUT = REPO / "docs/business-documentation/_screenshots/v2"
ANNOTATE = REPO / "scripts/annotate_business_screenshot.py"
PY = REPO / ".venv/bin/python"
BASE = os.environ.get("MDP_BASE_URL", "https://marketdata.coresight.com").rstrip("/")

def _base_hostname() -> str:
    return re.sub(r"^https?://", "", BASE).split("/")[0].split(":")[0]


def _resolve_host_ip(hostname: str) -> str | None:
    dns = os.environ.get("MDP_DNS_SERVER", "8.8.8.8").strip() or "8.8.8.8"
    try:
        proc = subprocess.run(
            ["dig", f"@{dns}", "+short", hostname],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        for line in (proc.stdout or "").strip().splitlines():
            candidate = line.strip().rstrip(".")
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", candidate):
                return candidate
    except Exception:
        pass
    return None


def _launch_browser(playwright):
    hostname = _base_hostname()
    launch_kwargs: dict = {"headless": True}
    ip = _resolve_host_ip(hostname)
    if ip:
        launch_kwargs["args"] = [f"--host-resolver-rules=MAP {hostname} {ip}"]
        log(f"Chromium DNS MAP {hostname} -> {ip} (via dig @{os.environ.get('MDP_DNS_SERVER', '8.8.8.8')})")
    return playwright.chromium.launch(**launch_kwargs)

TICKERS = ("WMT", "TGT", "AMZN")

ERROR_NEEDLES = (
    "Unable to load",
    "Error loading",
    "Database connection",
    "Something went wrong",
    "Connection timed out",
    "Access Denied",
    "You do not have permission",
    "Authentication failed",
    "Filing not found",
)

LOADING_NEEDLES = (
    "Loading financial data",
    "Loading earnings calendar",
    "Loading filing data",
    "Loading earnings call data",
)

VIEWPORT = {"width": 1500, "height": 1200}


def log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class ShotSpec:
    page_id: str
    path: str
    filename: str
    success_needles: tuple[str, ...]
    error_extra: tuple[str, ...] = ()
    wait_ms: int = 5000
    full_page: bool = True
    click: str | None = None
    click_selector: str | None = None
    dialog: bool = False
    markers: list[str] = field(default_factory=list)
    min_body_len: int = 200
    prepare: str | None = None
    ticker_fallback: bool = False
    clip: dict[str, int] | None = None
    markers_file: str | None = None


def _markers_for(filename: str, fallback: list[str] | None = None) -> list[str]:
    return CALLOUTS_BY_FILE.get(filename, fallback or [])


def _body_text(page) -> str:
    return page.inner_text("body")


def _has_errors(text: str, extra: tuple[str, ...] = ()) -> str | None:
    for needle in ERROR_NEEDLES + extra:
        if needle.lower() in text.lower():
            return needle
    return None


def _is_loading(text: str) -> bool:
    return any(n.lower() in text.lower() for n in LOADING_NEEDLES)


def _has_success(text: str, needles: tuple[str, ...]) -> bool:
    return any(n.lower() in text.lower() for n in needles)


def _page_ready(text: str, spec: ShotSpec) -> str | None:
    if "Sign in with Coresight" in text and spec.page_id != "login":
        return "not authenticated"
    if len(text.strip()) < spec.min_body_len:
        return f"blank page (len={len(text.strip())})"
    err = _has_errors(text, spec.error_extra)
    if err:
        return f"error state: {err}"
    if _is_loading(text):
        return "still loading"
    if not _has_success(text, spec.success_needles):
        return f"success needles not found: {spec.success_needles}"
    return None


def _annotate(src: Path, dst: Path, markers: list[str]) -> None:
    if not markers:
        return
    cmd = [str(PY), str(ANNOTATE), str(src), "--output", str(dst)]
    for marker in markers:
        cmd.extend(["--badge", marker])
    subprocess.run(cmd, check=True, cwd=REPO)


def _solve_security(page) -> None:
    label = page.locator("label:has-text('equals')").first
    if not label.count():
        return
    m = re.search(r"(\d+)\s*\+\s*(\d+)", label.inner_text())
    if not m:
        return
    answer = str(int(m.group(1)) + int(m.group(2)))
    page.locator("input[name*='math' i]").first.fill(answer)


def _wait_auth_session(context, page, max_seconds: int = 90) -> bool:
    for _ in range(max_seconds // 2):
        if any(c["name"] == "auth_session" for c in context.cookies()):
            return True
        text = _body_text(page)
        if "CORESIGHT MARKET DATA" in text or "Market Data Dashboard" in text:
            return True
        page.wait_for_timeout(2000)
    return False


def _login(context, emails: list[str], password: str) -> tuple[bool, str]:
    page = context.new_page()
    page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(4000)
    text = _body_text(page)
    if "CORESIGHT MARKET DATA" in text and "Sign in with Coresight" not in text:
        page.close()
        return True, "already authenticated"

    for email in emails:
        try:
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(4000)
            body = _body_text(page)
            if "CORESIGHT MARKET DATA" in body and "Sign in with Coresight" not in body:
                page.close()
                return True, f"session active ({email})"
            sso = page.locator("#csr-sso-btn, button:has-text('Sign in with Coresight')")
            try:
                sso.first.wait_for(state="visible", timeout=30000)
            except PWTimeout:
                log(f"  SSO button not found for {email}")
                continue
            with page.expect_navigation(timeout=90000, wait_until="domcontentloaded"):
                sso.first.click()
            page.wait_for_timeout(2000)
            page.locator("#user_login").fill(email)
            page.locator("#user_pass").fill(password)
            _solve_security(page)
            with page.expect_navigation(timeout=120000, wait_until="domcontentloaded"):
                page.locator("#wp-submit").click()
            if not _wait_auth_session(context, page):
                log(f"  auth_session timeout for {email}")
                continue
            page.goto(f"{BASE}/home", wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)
            if "CORESIGHT MARKET DATA" in _body_text(page):
                page.close()
                return True, email
        except Exception as exc:
            log(f"  login error {email}: {exc}")
    page.close()
    return False, "all login attempts failed"


def _select_company_home(page, ticker: str) -> None:
    page.goto(f"{BASE}/home", wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(4000)
    selects = page.locator("[data-baseweb='select']")
    if selects.count() >= 1:
        selects.first.click()
        page.wait_for_timeout(500)
        page.keyboard.type(ticker)
        page.wait_for_timeout(2000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1000)
    page.locator("button:has-text('View')").first.click()
    page.wait_for_timeout(6000)


def _select_company_only(page, ticker: str) -> None:
    page.goto(f"{BASE}/home", wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(4000)
    selects = page.locator("[data-baseweb='select']")
    if selects.count() >= 1:
        selects.first.click()
        page.wait_for_timeout(500)
        page.keyboard.type(ticker)
        page.wait_for_timeout(2000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)


def _open_baseselect_by_label(page, label: str) -> bool:
    for sel in (
        f"[data-baseweb='select']:near(:text('{label}'))",
        f"div:has(> label:has-text('{label}')) [data-baseweb='select']",
        f"label:has-text('{label}')",
    ):
        loc = page.locator(sel).first
        if loc.count():
            loc.click(timeout=10000)
            page.wait_for_timeout(1500)
            return True
    return False


def _open_screening_watchlist_dropdown(page) -> bool:
    """Open the collapsed Watchlists bar selectbox (label is not a <label> element)."""
    title = page.locator("p.watchlist-bar-title")
    if not title.count():
        return False
    for sel in (
        "div:has(p.watchlist-bar-title) [data-baseweb='select']",
        "[data-testid='stHorizontalBlock']:has(p.watchlist-bar-title) [data-baseweb='select']",
        "[data-testid='column']:has(p.watchlist-bar-title) + [data-testid='column'] [data-baseweb='select']",
    ):
        loc = page.locator(sel).first
        if loc.count():
            loc.click(timeout=15000)
            page.wait_for_timeout(1500)
            return True
    return False


def _open_screening_financial(page) -> None:
    page.locator("button:has-text('Financial Information')").first.click(timeout=30000)
    page.wait_for_timeout(2000)


def _screening_financial_advance(page, steps: int) -> None:
    _open_screening_financial(page)
    for _ in range(steps):
        for sel in (
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "[data-testid='stButton'] button:has-text('Next')",
        ):
            btn = page.locator(sel)
            if btn.count():
                btn.first.click(timeout=10000)
                page.wait_for_timeout(1500)
                break


def _prepare(page, spec: ShotSpec, ticker: str) -> None:
    if spec.prepare == "home_select_view":
        _select_company_home(page, ticker)
    elif spec.prepare == "home_select_company":
        _select_company_only(page, ticker)
    elif spec.prepare == "newsroom_from_date":
        _open_baseselect_by_label(page, "From")
    elif spec.prepare == "earnings_search":
        box = page.locator("input").filter(has_text="").first
        for sel in (
            "input[placeholder*='Search']",
            "input[aria-label*='Search']",
            "[data-testid='stTextInput'] input",
        ):
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill("margin")
                page.wait_for_timeout(4000)
                break
    elif spec.prepare == "filings_metric":
        for sel in ("input[placeholder*='metric' i]", "input[placeholder*='Metric' i]"):
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill("revenue")
                page.wait_for_timeout(5000)
                break
    elif spec.prepare == "filings_open_doc":
        for sel in ("button:has-text('10-K')", "button:has-text('10-Q')", "div[data-testid='stButton'] button"):
            rows = page.locator("button:has-text('10-K'), button:has-text('10-Q')")
            if rows.count():
                rows.first.click(timeout=10000)
                page.wait_for_timeout(5000)
                break
    elif spec.prepare == "screening_watchlist_dropdown":
        if not _open_screening_watchlist_dropdown(page):
            raise RuntimeError("screening watchlist selectbox not found")
    elif spec.prepare == "screening_financial_step0":
        _open_screening_financial(page)
    elif spec.prepare == "screening_financial_step1":
        _screening_financial_advance(page, 1)
    elif spec.prepare == "screening_financial_step2":
        _screening_financial_advance(page, 2)
    elif spec.prepare == "screening_financial_step3":
        _screening_financial_advance(page, 3)
    elif spec.prepare == "screening_financial_step4":
        _screening_financial_advance(page, 4)
    elif spec.prepare == "forecasting_quarterly":
        for sel in (
            "label:has-text('Quarterly')",
            "div[data-baseweb='radio'] >> text=Quarterly",
            "[data-testid='stRadio'] label:has-text('Quarterly')",
        ):
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=10000)
                page.wait_for_timeout(4000)
                break
    elif spec.prepare == "earnings_calendar_legend":
        try:
            page.locator("text=Show types").first.wait_for(state="visible", timeout=8000)
            page.wait_for_timeout(2000)
        except Exception:
            pass


def _do_click(page, spec: ShotSpec) -> tuple[bool, str]:
    if spec.click_selector:
        try:
            page.locator(spec.click_selector).first.click(timeout=60000)
            page.wait_for_timeout(2500)
            return True, "ok"
        except PWTimeout:
            return False, f"click selector not found: {spec.click_selector}"
    if spec.click:
        try:
            page.locator(f"button:has-text('{spec.click}')").first.click(timeout=60000)
            page.wait_for_timeout(2500)
            return True, "ok"
        except PWTimeout:
            return False, f"click target not found: {spec.click}"
    return True, "ok"


def _capture_page(page, spec: ShotSpec, out_path: Path, ticker: str) -> tuple[bool, str]:
    paths = [spec.path]
    if spec.ticker_fallback and "{ticker}" in spec.path:
        paths = [spec.path.format(ticker=t) for t in TICKERS]

    last_reason = "max retries exceeded"
    for path in paths:
        for attempt in range(1, 4):
            try:
                page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=120000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(spec.wait_ms)
                if spec.prepare:
                    _prepare(page, spec, ticker if "{ticker}" not in path else path.split("ticker=")[1].split("&")[0])

                if spec.click or spec.click_selector:
                    ok_click, click_reason = _do_click(page, spec)
                    if not ok_click:
                        last_reason = click_reason
                        if attempt < 3:
                            continue
                        return False, click_reason

                for _ in range(25):
                    text = _body_text(page)
                    reason = _page_ready(text, spec)
                    if reason is None:
                        target = page.query_selector("div[role='dialog']") if spec.dialog else None
                        if target:
                            target.screenshot(path=str(out_path))
                        elif spec.clip:
                            page.screenshot(path=str(out_path), clip=spec.clip)
                        else:
                            page.screenshot(path=str(out_path), full_page=spec.full_page)
                        return True, "ok"
                    last_reason = reason
                    page.wait_for_timeout(3000)
            except Exception as exc:
                last_reason = str(exc)
            if attempt < 3:
                backoff = 10 if "ERR_NAME_NOT_RESOLVED" in last_reason else 2
                time.sleep(backoff)
    return False, last_reason


def _md_tab(tab: str, filename: str, needle: str, markers: list[str], wait_ms: int = 8000) -> ShotSpec:
    return ShotSpec(
        "market-data",
        f"/market_data?ticker={{ticker}}&tab={tab}&period_type=Annual",
        filename,
        (needle,),
        wait_ms=wait_ms,
        markers=markers,
        ticker_fallback=True,
    )


def _login_specs() -> list[ShotSpec]:
    return [
        ShotSpec(
            "login", "/", "login-01.png",
            ("Sign in", "Coresight", "Market Data Portal"),
            wait_ms=4000, min_body_len=100,
            markers=[
                "80,200,1,749,200,Portal title",
                "80,368,2,749,368,Sign in with Coresight",
                "1200,38,4,1339,39,Contact Us",
            ],
        ),
        ShotSpec(
            "login", "/", "login-03-footer.png",
            ("Contact Us", "Premium member"),
            wait_ms=4000, min_body_len=80,
            markers=[
                "80,700,1,400,750,Contact Us footer",
                "80,750,2,500,800,Privacy Policy",
                "80,800,3,600,850,Social icons",
            ],
        ),
    ]


def _gap_specs() -> list[ShotSpec]:
    t = TICKERS[0]
    nav_clip = {"x": 0, "y": 0, "width": 1500, "height": 220}
    return [
        # HOME — unique captures (not copies of user batch)
        ShotSpec(
            "home", "/home", "home-01.png",
            ("CORESIGHT MARKET DATA", "Company"),
            wait_ms=5000,
            markers=_markers_for("home-01.png"),
        ),
        ShotSpec(
            "home", "/home", "home-02-company-selected.png",
            ("Company",), wait_ms=5000,
            prepare="home_select_company",
            markers=_markers_for("home-02-company-selected.png"),
            ticker_fallback=True,
        ),
        ShotSpec(
            "home", "/home", "market-data-entry-from-home.png",
            ("Company", "View"), wait_ms=5000,
            prepare="home_select_company",
            markers=_markers_for("market-data-entry-from-home.png"),
            ticker_fallback=True,
        ),
        ShotSpec(
            "home", "/home", "home-04-nav-highlight.png",
            ("CORESIGHT MARKET DATA", "Market Data"),
            wait_ms=5000,
            clip=nav_clip,
            markers=_markers_for("home-04-nav-highlight.png"),
        ),
        ShotSpec(
            "screening", "/screening", "nav-portal-overview.png",
            ("Screen For", "Market Data"),
            wait_ms=8000, min_body_len=300,
            clip=nav_clip,
            markers=_markers_for("nav-portal-overview.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-nav-context.png",
            ("Screen For", "Companies"), wait_ms=12000, min_body_len=300,
            markers=_markers_for("screening-nav-context.png"),
        ),
        # NEWSROOM — full set
        ShotSpec(
            "newsroom", "/newsroom", "newsroom-01.png",
            ("Search News",), wait_ms=8000, min_body_len=400,
            markers=_markers_for("newsroom-01.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom", "nav-newsroom-layout.png",
            ("Search News", "News Results"), wait_ms=8000, min_body_len=400,
            markers=_markers_for("nav-newsroom-layout.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom?ticker=WMT", "newsroom-02-filters.png",
            ("Search News",), wait_ms=8000, min_body_len=400,
            markers=_markers_for("newsroom-02-filters.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom?ticker=WMT", "nav-newsroom-filters.png",
            ("Search News",), wait_ms=8000, min_body_len=400,
            markers=_markers_for("nav-newsroom-filters.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom", "newsroom-03-articles.png",
            ("News Results",), wait_ms=10000, min_body_len=600,
            markers=_markers_for("newsroom-03-articles.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom", "newsroom-04-sort.png",
            ("Search News",), wait_ms=8000,
            click_selector="label:has-text('Sort')",
            markers=_markers_for("newsroom-04-sort.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom", "newsroom-05-category.png",
            ("Search News",), wait_ms=8000,
            click_selector="label:has-text('Category')",
            markers=_markers_for("newsroom-05-category.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom", "newsroom-06-watchlist.png",
            ("Search News",), wait_ms=8000,
            click_selector="label:has-text('Watchlist')",
            markers=_markers_for("newsroom-06-watchlist.png"),
        ),
        ShotSpec(
            "newsroom", "/newsroom", "newsroom-07-date-picker.png",
            ("Search News",), wait_ms=8000,
            prepare="newsroom_from_date",
            markers=_markers_for("newsroom-07-date-picker.png"),
        ),
        # SCREENING — full builder set
        ShotSpec(
            "screening", "/screening", "screening-01-default.png",
            ("Screen For", "Companies"), wait_ms=12000, min_body_len=300,
            markers=_markers_for("screening-01-default.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-05-watchlists-dropdown.png",
            ("Screen For",), wait_ms=12000,
            prepare="screening_watchlist_dropdown",
            markers=_markers_for("screening-05-watchlists-dropdown.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-04-watchlist.png",
            ("Watchlist",), wait_ms=12000,
            click="Edit / Manage", dialog=True,
            markers=_markers_for("screening-04-watchlist.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-watchlist-dialog-new.png",
            ("New Watchlist", "Watchlist"), wait_ms=12000,
            click="Edit / Manage", dialog=True,
            markers=_markers_for("screening-watchlist-dialog-new.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-03-industry.png",
            ("Industry",), wait_ms=12000,
            click="Industry Classifications",
            markers=_markers_for("screening-03-industry.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-industry-form.png",
            ("Industry",), wait_ms=12000,
            click="Industry Classifications",
            markers=_markers_for("screening-industry-form.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-geographic.png",
            ("Geographic", "Countries"), wait_ms=12000,
            click="Geographic Locations",
            markers=_markers_for("screening-geographic.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-geographic-countries.png",
            ("Geographic",), wait_ms=12000,
            click="Geographic Locations",
            markers=_markers_for("screening-geographic-countries.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-02-financial.png",
            ("Statement Type", "Financial"), wait_ms=12000,
            prepare="screening_financial_step0",
            markers=_markers_for("screening-02-financial.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-financial-statement-type.png",
            ("Statement Type",), wait_ms=12000,
            prepare="screening_financial_step0",
            markers=_markers_for("screening-financial-statement-type.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-financial-metric.png",
            ("Select Metric", "Statement"), wait_ms=12000,
            prepare="screening_financial_step1",
            markers=_markers_for("screening-financial-metric.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-financial-period-type.png",
            ("Period Type",), wait_ms=12000,
            prepare="screening_financial_step2",
            markers=_markers_for("screening-financial-period-type.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-financial-year-range.png",
            ("Period Type", "Year"), wait_ms=12000,
            prepare="screening_financial_step3",
            markers=_markers_for("screening-financial-year-range.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-financial-year.png",
            ("Year",), wait_ms=12000,
            prepare="screening_financial_step3",
            markers=_markers_for("screening-financial-year.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-financial-operator.png",
            ("Operator",), wait_ms=12000,
            prepare="screening_financial_step4",
            markers=_markers_for("screening-financial-operator.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-financial-value.png",
            ("Value",), wait_ms=12000,
            prepare="screening_financial_step4",
            markers=_markers_for("screening-financial-value.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-key-devs-mode.png",
            ("Key Developments",), wait_ms=12000,
            click_selector="text=Key Devs",
            markers=_markers_for("screening-key-devs-mode.png"),
        ),
        ShotSpec(
            "screening", "/screening", "screening-key-devs-categories.png",
            ("Key Developments", "Categories"), wait_ms=12000,
            click_selector="text=Key Devs",
            markers=_markers_for("screening-key-devs-categories.png"),
        ),
        ShotSpec(
            "home", "/home", "home-03-landing.png",
            ("Market Data", "Income Statement"),
            prepare="home_select_view", wait_ms=10000,
            markers=[
                "80,78,1,1180,83,Top navigation",
                "80,200,2,400,220,Company header",
                "80,250,3,300,280,Company Profile tab",
            ],
            ticker_fallback=True,
        ),
        ShotSpec(
            "home", "/home", "home-05-dashboard.png",
            ("CORESIGHT MARKET DATA", "Company"),
            wait_ms=5000,
            markers=[
                "80,260,1,331,353,View by Company",
                "80,340,2,668,353,View by Sector",
                "80,420,3,331,343,Company picker",
            ],
        ),
        _md_tab("company_profile", "market-data-company-profile.png", "Walmart",
                ["80,200,1,255,248,Key Stats tab",
                 "80,320,2,512,480,Profile fields",
                 "80,440,3,512,680,Chart area"]),
        _md_tab("income_statement", "market-data-income-statement.png", "Total Revenue",
                ["120,200,1,220,240,Income Statement tab",
                 "600,320,2,700,360,Revenue row"]),
        _md_tab("balance_sheet", "market-data-balance-sheet.png", "Total Assets",
                ["120,200,1,220,240,Balance Sheet tab",
                 "600,320,2,700,360,Total Assets row"]),
        _md_tab("cash_flow", "market-data-cash-flow.png", "Operating",
                ["120,200,1,200,240,Cash Flow tab",
                 "600,320,2,700,360,Operating cash row"]),
        _md_tab("key_stats", "market-data-key-stats.png", "Revenue",
                ["120,200,1,200,240,Key Stats tab",
                 "500,300,2,600,340,Revenue metric"]),
        _md_tab("ratios", "market-data-ratios.png", "Gross Margin",
                ["120,200,1,180,240,Ratios tab",
                 "500,300,2,600,340,Gross Margin row"]),
        _md_tab("estimates", "market-data-estimates.png", "Estimate",
                ["120,200,1,200,240,Estimates tab",
                 "600,300,2,700,350,Analyst estimates"]),
        _md_tab("forecasting", "market-data-forecasting.png", "Forecast",
                ["120,200,1,220,240,Forecasting tab",
                 "600,300,2,700,350,Forecast table"]),
        _md_tab("segment_data", "market-data-segment.png", "Segment",
                ["120,200,1,180,240,Segment tab",
                 "500,300,2,600,350,Segment breakdown"]),
        ShotSpec(
            "earnings-calls", "/earnings_calls?ticker={ticker}&year=2024&quarter=Q4",
            "earnings-calls-01.png", ("Operator", "prepared"), wait_ms=12000,
            markers=["200,200,1,300,240,Ticker selector",
                     "500,200,2,600,240,Year quarter picker",
                     "400,350,3,500,400,Transcript content"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "earnings-calls", "/earnings_calls?ticker={ticker}&year=2024&quarter=Q4",
            "earnings-calls-02-list.png", ("Q4", "2024", "Download"), wait_ms=12000,
            markers=["200,200,1,300,240,Company filter",
                     "500,200,2,600,240,Year and quarter",
                     "400,500,3,500,550,Transcript list"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "earnings-calls", "/earnings_calls?ticker={ticker}&year=2024&quarter=Q4",
            "earnings-calls-03-search.png", ("Search", "View"), wait_ms=12000,
            prepare="earnings_search",
            markers=["200,250,1,350,300,Keyword search",
                     "400,400,2,500,450,Search results",
                     "900,350,3,1000,400,Transcript viewer"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "earnings-calendar", "/earnings_calendar", "earnings-calendar-01.png",
            ("Earnings Calendar", "Month"), wait_ms=12000, min_body_len=400,
            markers=["80,170,1,620,155,Company filter",
                     "80,250,2,827,145,Month Wise toggle",
                     "80,360,3,489,517,Calendar grid"],
        ),
        ShotSpec(
            "earnings-calendar", "/earnings_calendar", "earnings-calendar-02-events.png",
            ("Earnings Calendar", "Month Wise", "Show types"), wait_ms=12000, min_body_len=400,
            prepare="earnings_calendar_legend",
            markers=["200,220,1,300,260,Legend chips",
                     "500,350,2,600,400,Day event markers",
                     "900,300,3,1000,350,Event colors"],
        ),
        ShotSpec(
            "earnings-calendar", "/earnings_calendar", "earnings-calendar-03-month.png",
            ("Email Alerts", "Month Wise"), wait_ms=12000, min_body_len=400,
            markers=["200,150,1,350,180,Email Alerts",
                     "500,150,2,650,180,Month Wise toggle",
                     "400,300,3,500,350,Calendar grid"],
        ),
        ShotSpec(
            "forecasting", "/forecasting?ticker={ticker}", "forecasting-01.png",
            ("REVENUE FORECASTING",), wait_ms=12000,
            markers=["200,200,1,350,240,Ticker selector",
                     "600,300,2,700,350,Forecast chart",
                     "900,200,3,1050,240,Refresh controls"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "forecasting", "/forecasting?ticker={ticker}&period_type=Quarterly", "forecasting-02.png",
            ("Quarterly", "Quarter", "REVENUE FORECASTING"), wait_ms=12000,
            prepare="forecasting_quarterly",
            markers=["400,200,1,500,240,Quarterly period toggle",
                     "600,350,2,700,400,Quarterly forecast table"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "forecasting", "/forecasting?ticker={ticker}&period_type=Quarterly", "forecasting-03.png",
            ("Scenario", "Baseline"), wait_ms=12000,
            markers=["400,250,1,550,300,Scenario summary",
                     "700,350,2,800,400,Pessimistic band",
                     "900,350,3,1000,400,Optimistic band"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "forecasting", "/forecasting?ticker={ticker}", "forecasting-refresh-dialog.png",
            ("Refresh Forecasting", "Ticker"), click="Refresh Data", dialog=True, wait_ms=12000,
            markers=["300,200,1,450,250,Refresh dialog title",
                     "600,350,2,700,400,Company refresh table"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "company-filings", "/company_filings?ticker={ticker}", "company-filings.png",
            ("10-K", "10-Q", "Filing"), wait_ms=15000, min_body_len=500,
            markers=["200,200,1,300,240,Ticker search",
                     "500,300,2,600,350,Filing documents table",
                     "900,200,3,1000,240,Document type filter"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "company-filings", "/company_filings?ticker={ticker}", "company-filings-02.png",
            ("Download", "10-K"), wait_ms=15000, min_body_len=400,
            prepare="filings_open_doc",
            markers=["80,210,1,900,210,Filing header",
                     "80,290,2,1380,175,Download button",
                     "80,370,3,850,450,Document viewer"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "company-filings", "/company_filings?ticker={ticker}", "company-filings-03.png",
            ("Search", "metric"), wait_ms=15000, min_body_len=400,
            prepare="filings_metric",
            markers=["200,250,1,350,300,Metric search box",
                     "200,400,2,350,450,Metric result cards",
                     "700,300,3,900,350,Document body"],
            ticker_fallback=True,
        ),
        ShotSpec(
            "access-management", "/access_management", "access-management.png",
            ("Access Management", "All Users"), wait_ms=12000, min_body_len=500,
            error_extra=("Access Denied",),
            markers=["60,170,1,135,160,IAM badge",
                     "60,250,2,180,340,Users tab",
                     "60,330,3,1280,580,Add User",
                     "60,410,4,600,920,All Users table"],
        ),
        ShotSpec(
            "retailer-adding", "/retailer_adding", "retailer-adding.png",
            ("Retailer Manager",), wait_ms=12000,
            markers=["120,200,1,350,240,Retailer Manager title",
                     "400,350,2,500,400,Editable retailer grid",
                     "900,200,3,1000,240,Bulk upload section"],
        ),
        ShotSpec(
            "company-filings-add-files", "/company_filings_add_files",
            "company-filings-add-files.png",
            ("File Manager", "Upload"), wait_ms=15000, min_body_len=300,
            markers=["120,200,1,350,240,File Manager title",
                     "400,350,2,500,400,Upload form",
                     "800,350,3,900,400,Folder browser"],
        ),
        ShotSpec(
            "live-earnings-transcript", "/live_earnings_transcript",
            "live-earnings-transcript-01.png",
            ("Call Setup", "Transcript Stream"),
            wait_ms=8000,
            markers=["60,180,1,500,185,Session stats",
                     "60,285,2,384,285,Company ticker",
                     "1420,600,4,1114,600,Transcript stream"],
        ),
        ShotSpec(
            "live-earnings-transcript", "/live_earnings_transcript",
            "live-earnings-transcript-02.png",
            ("Pipeline", "Transcript Stream"),
            wait_ms=8000,
            markers=["60,400,1,400,450,Pipeline log",
                     "1420,600,2,1114,600,Empty stream",
                     "60,180,3,300,200,Session stats"],
        ),
        ShotSpec(
            "live-earnings-transcript", "/live_earnings_transcript",
            "live-earnings-transcript-03.png",
            ("Call Setup", "Company"),
            wait_ms=8000, full_page=False,
            markers=["60,285,1,384,285,Company ticker field",
                     "60,350,2,384,350,Year field",
                     "60,415,3,384,415,Quarter field"],
        ),
    ]


def _capture_login_sso(browser, manifest: list[dict]) -> None:
    ctx = browser.new_context(viewport=VIEWPORT)
    page = ctx.new_page()
    for spec in _login_specs():
        out_path = OUT / spec.filename
        log(f"CAPTURE login: {spec.filename}")
        ok, reason = _capture_page(page, spec, out_path, TICKERS[0])
        entry = {"page": "login", "file": spec.filename, "status": "ok" if ok else "skipped", "reason": reason, "source": "production"}
        if ok:
            ann = spec.filename.replace(".png", "-annotated.png")
            try:
                markers = spec.markers or _markers_for(spec.filename)
                _annotate(out_path, OUT / ann, markers)
                entry["annotated"] = ann
            except Exception as exc:
                entry["annotation_error"] = str(exc)
        elif out_path.exists():
            out_path.unlink()
        manifest.append(entry)
        log(f"  {'OK' if ok else 'SKIP'}: {reason}")

    page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(3000)
    try:
        sso = page.locator("#csr-sso-btn, button:has-text('Sign in with Coresight')")
        sso.first.wait_for(state="visible", timeout=30000)
        sso.first.click(timeout=30000)
        page.wait_for_timeout(5000)
        sso_path = OUT / "login-02-sso.png"
        page.screenshot(path=str(sso_path), full_page=True)
        markers = _markers_for("login-02-sso.png")
        ann = "login-02-sso-annotated.png"
        _annotate(sso_path, OUT / ann, markers)
        manifest.append({"page": "login", "file": "login-02-sso.png", "status": "ok", "annotated": ann, "source": "production", "reason": "ok"})
        log("CAPTURE login: login-02-sso.png OK")
    except Exception as exc:
        manifest.append({"page": "login", "file": "login-02-sso.png", "status": "skipped", "reason": str(exc), "source": "production"})
        log(f"SKIP login-02-sso: {exc}")
    ctx.close()


def _merge_manifest(new_entries: list[dict]) -> None:
    manifest_path = OUT / "MANIFEST.json"
    existing: list[dict] = []
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
    by_file = {e["file"]: e for e in existing}
    for entry in new_entries:
        if entry.get("status") == "ok":
            by_file[entry["file"]] = entry
    merged = sorted(by_file.values(), key=lambda e: e.get("file", ""))
    manifest_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


def _filter_specs(only: str | None) -> list[ShotSpec]:
    specs = _gap_specs()
    if not only:
        return specs
    allowed = {s.strip() for s in only.split(",") if s.strip()}
    return [s for s in specs if s.page_id in allowed]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Capture MDP business-documentation screenshots.")
    parser.add_argument(
        "--only",
        help="Comma-separated page_id values to capture (e.g. retailer-adding,company-filings-add-files)",
    )
    parser.add_argument(
        "--skip-login-captures",
        action="store_true",
        help="Skip login SSO screenshot captures (use with --only for gap pages)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Skip OIDC login (localhost LOCAL bypass — server must already authenticate)",
    )
    args = parser.parse_args()

    email = os.environ.get("MDP_EMAIL", "").strip()
    password = os.environ.get("MDP_PASSWORD", "").strip()
    alt = os.environ.get("MDP_EMAIL_ALT", "mohdsaeedafri@coresight.com").strip()
    emails = [e for e in dict.fromkeys((alt, email)) if e]
    if not password or not emails:
        if not args.no_auth:
            log("Set MDP_EMAIL and MDP_PASSWORD environment variables.")
            return 1

    source = "local" if "localhost" in BASE or "127.0.0.1" in BASE else ("staging" if "stg" in BASE else "production")
    specs = _filter_specs(args.only)
    if args.only and not specs:
        log(f"No specs matched --only={args.only!r}")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    ok_count = 0
    fail_count = 0

    with sync_playwright() as p:
        browser = _launch_browser(p)
        if not args.skip_login_captures:
            _capture_login_sso(browser, manifest)

        ctx = browser.new_context(viewport=VIEWPORT)
        if args.no_auth:
            ok, who = True, "local-bypass"
            log(f"Skipping OIDC login (--no-auth); using {BASE} LOCAL bypass")
        else:
            ok, who = _login(ctx, emails, password)
            if not ok:
                log(f"FATAL: {who}")
                browser.close()
                return 2
            log(f"Authenticated as {who} via {BASE}")

        page = ctx.new_page()
        for spec in specs:
            out_path = OUT / spec.filename
            log(f"CAPTURE {spec.page_id}: {spec.filename}")
            ok, reason = _capture_page(page, spec, out_path, TICKERS[0])
            entry = {"page": spec.page_id, "file": spec.filename, "status": "ok" if ok else "skipped", "reason": reason, "source": source}
            if ok:
                ok_count += 1
                ann = spec.filename.replace(".png", "-annotated.png")
                try:
                    markers = spec.markers or _markers_for(spec.filename)
                    _annotate(out_path, OUT / ann, markers)
                    entry["annotated"] = ann
                except Exception as exc:
                    entry["annotation_error"] = str(exc)
                time.sleep(1)
            else:
                fail_count += 1
                if out_path.exists():
                    out_path.unlink()
                log(f"  SKIP: {reason}")
            manifest.append(entry)

        page.close()
        ctx.close()
        browser.close()

    _merge_manifest(manifest)

    log(f"\n=== {source.upper()} CAPTURE SUMMARY ({BASE}) ===")
    log(f"OK: {ok_count}")
    log(f"Skipped: {fail_count}")
    for entry in manifest:
        if entry["status"] != "ok":
            log(f"  FAIL {entry['file']}: {entry.get('reason')}")
    return 0 if ok_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
