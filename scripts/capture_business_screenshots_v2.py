#!/usr/bin/env python3
"""Capture business-documentation screenshots (v2) with data validation.

Only saves screenshots when real content is present — skips error/empty/loading states.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "docs/business-documentation/_screenshots/v2"
ANNOTATE = REPO / "scripts/annotate_business_screenshot.py"
PY = REPO / ".venv/bin/python"
PORT = 8501
BASE = f"http://localhost:{PORT}"
TICKER = "WMT"

ERROR_NEEDLES = (
    "Unable to load",
    "Error loading",
    "Database connection",
    "Something went wrong",
    "Connection timed out",
    "timed out",
    "Traceback",
    "Access Denied",
    "You do not have permission",
    "No earnings call data available",
    "No news articles found",
    "No revenue forecast coverage",
    "No options to select",
    "Filing not found",
)

LOADING_NEEDLES = (
    "Loading financial data",
    "Loading earnings calendar",
    "Loading filing data",
    "Loading earnings call data",
)

VIEWPORT = {"width": 1500, "height": 1200}


@dataclass
class ShotSpec:
    page_id: str
    path: str
    filename: str
    success_needles: tuple[str, ...]
    error_extra: tuple[str, ...] = ()
    wait_ms: int = 3000
    full_page: bool = True
    click: str | None = None
    click_selector: str | None = None
    dialog: bool = False
    markers: list[str] = field(default_factory=list)
    annotated_name: str | None = None
    min_body_len: int = 200


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


def _prepare_page(page, spec: ShotSpec) -> None:
    if spec.page_id == "newsroom" and "ticker=" not in spec.path:
        from_input = page.locator('input[aria-label="From"]').first
        if from_input.count():
            from_input.fill("2025/01/01")
            from_input.press("Tab")
            page.wait_for_timeout(5000)


def _do_click(page, spec: ShotSpec) -> tuple[bool, str]:
    if spec.click_selector:
        try:
            page.wait_for_selector(spec.click_selector, timeout=60000)
            page.click(spec.click_selector)
            page.wait_for_timeout(2000)
            return True, "ok"
        except PWTimeout:
            return False, f"click selector not found: {spec.click_selector}"
    if spec.click:
        try:
            page.wait_for_selector(f"button:has-text('{spec.click}')", timeout=60000)
            page.click(f"button:has-text('{spec.click}')")
            page.wait_for_timeout(2000)
            return True, "ok"
        except PWTimeout:
            return False, f"click target not found: {spec.click}"
    return True, "ok"


def _capture_page(page, spec: ShotSpec, out_path: Path) -> tuple[bool, str]:
    url = f"{BASE}{spec.path}"
    last_reason = "max retries exceeded"
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(spec.wait_ms)
            _prepare_page(page, spec)
            if spec.click or spec.click_selector:
                ok_click, click_reason = _do_click(page, spec)
                if not ok_click:
                    return False, click_reason

            for _ in range(40):
                text = _body_text(page)
                reason = _page_ready(text, spec)
                if reason is None:
                    target = page.query_selector("div[role='dialog']") if spec.dialog else None
                    if target:
                        target.screenshot(path=str(out_path))
                    else:
                        page.screenshot(path=str(out_path), full_page=spec.full_page)
                    verify = _page_ready(_body_text(page), spec)
                    if verify is None:
                        return True, "ok"
                    last_reason = f"post-shot regression: {verify}"
                    if out_path.exists():
                        out_path.unlink()
                    break
                last_reason = reason
                if reason.startswith("error state:") and attempt < 3:
                    page.wait_for_timeout(4000)
                    break
                page.wait_for_timeout(3000)
            else:
                if attempt < 3:
                    time.sleep(3)
                    continue
                return False, last_reason
        except Exception as exc:
            last_reason = str(exc)
            if attempt < 3:
                time.sleep(3)
                continue
            return False, last_reason
    return False, last_reason


def _annotate(src: Path, dst: Path, markers: list[str]) -> None:
    if not markers:
        return
    cmd = [str(PY), str(ANNOTATE), str(src), "--output", str(dst)]
    for marker in markers:
        cmd.extend(["--arrow", marker])
    subprocess.run(cmd, check=True, cwd=REPO)


def _md_tab(tab: str, filename: str, needle: str, markers: list[str], wait_ms: int = 8000) -> ShotSpec:
    return ShotSpec(
        "market-data",
        f"/market_data?ticker={TICKER}&tab={tab}&period_type=Annual",
        filename,
        (needle,),
        wait_ms=wait_ms,
        markers=markers,
    )


def _specs() -> list[ShotSpec]:
    return [
        # HOME
        ShotSpec("home", "/home", "home-01.png", ("CORESIGHT MARKET DATA", "Company"),
                 wait_ms=5000,
                 markers=["80,40,1,600,37,Navigation",
                          "80,380,2,420,380,View by Company",
                          "1420,380,3,920,380,View by Sector",
                          "80,404,4,514,404,Company picker"]),
        ShotSpec("home", "/home", "home-02-company-selected.png",
                 ("Company",), wait_ms=5000,
                 markers=["80,404,1,514,404,Company dropdown",
                          "80,470,2,498,470,View button",
                          "1420,404,3,1014,404,Sector dropdown"]),
        ShotSpec("home", "/home", "home-03-landing.png",
                 ("CORESIGHT MARKET DATA",), wait_ms=5000,
                 markers=["80,380,1,420,380,Company card",
                          "1420,380,2,920,380,Sector card",
                          "80,404,3,514,404,Company picker"]),
        ShotSpec("home", "/home", "home-04-nav-highlight.png",
                 ("Market Data", "Screening"), wait_ms=5000,
                 markers=["80,40,1,600,37,Top navigation",
                          "300,40,2,400,37,Market Data",
                          "700,40,3,950,39,Screening",
                          "900,40,4,1058,37,News"]),
        ShotSpec("home", "/home", "home-05-dashboard.png",
                 ("Company", "Sector"), wait_ms=5000,
                 markers=["80,40,1,600,37,Navigation",
                          "80,380,2,420,380,View by Company",
                          "1420,380,3,920,380,View by Sector"]),
        # MARKET DATA — WMT all tabs
        _md_tab("company_profile", "market-data-company-profile.png", "Walmart",
                ["120,200,1,400,220,Company profile tab",
                 "500,180,2,700,220,Ticker selector",
                 "900,300,3,1000,350,Key metrics"]),
        _md_tab("income_statement", "market-data-income-statement.png", "Total Revenue",
                ["120,200,1,220,240,Income Statement tab",
                 "600,320,2,700,360,Revenue row",
                 "1100,200,3,1200,250,Period filters"]),
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
        # SCREENING
        ShotSpec("screening", "/screening", "screening-default.png",
                 ("Screen For", "Companies"), min_body_len=300, wait_ms=12000,
                 markers=["200,200,1,300,240,Screen For row",
                          "400,280,2,500,320,Add criteria palette",
                          "700,350,3,800,390,Show Results"]),
        ShotSpec("screening", "/screening", "screening-financial-form.png",
                 ("Step 1", "Statement Type"), click="Financial Information", wait_ms=12000,
                 markers=["300,300,1,400,340,Financial criteria form",
                          "600,350,2,700,400,Statement type step"]),
        ShotSpec("screening", "/screening", "screening-key-devs-mode.png",
                 ("Key Developments",), click_selector="text=Key Devs", wait_ms=12000,
                 markers=["200,200,1,280,240,Key Devs mode",
                          "400,280,2,500,320,Key developments criteria"]),
        # NEWSROOM
        ShotSpec("newsroom", "/newsroom", "newsroom-01.png",
                 ("Search News",), min_body_len=400, wait_ms=8000,
                 markers=["60,245,1,170,245,Search box",
                          "60,290,2,450,245,Date range",
                          "1420,245,3,950,245,Sort and filters"]),
        ShotSpec("newsroom", "/newsroom?ticker=WMT", "newsroom-02-filters.png",
                 ("Search News",), wait_ms=8000, min_body_len=400,
                 markers=["60,245,1,170,245,News search",
                          "700,180,2,800,220,Ticker filter"]),
        ShotSpec("newsroom", "/newsroom", "newsroom-03-articles.png",
                 ("News Results",), wait_ms=10000, min_body_len=600,
                 markers=["400,400,1,500,450,Article headline",
                          "900,400,2,1000,450,Sentiment indicator"]),
        # EARNINGS CALLS
        ShotSpec("earnings-calls", "/earnings_calls?ticker=WMT&year=2024&quarter=Q4",
                 "earnings-calls-01.png", ("Operator", "Walmart", "prepared"), wait_ms=10000,
                 markers=["200,200,1,300,240,Ticker selector",
                          "500,200,2,600,240,Year quarter picker",
                          "400,350,3,500,400,Transcript content"]),
        # EARNINGS CALENDAR
        ShotSpec("earnings-calendar", "/earnings_calendar", "earnings-calendar-01.png",
                 ("Earnings Calendar", "Company"), wait_ms=10000, min_body_len=400,
                 markers=["400,250,1,500,300,Calendar grid",
                          "200,180,2,300,220,Company filter"]),
        # COMPANY FILINGS
        ShotSpec("company-filings", "/company_filings?ticker=WMT", "company-filings.png",
                 ("10-K", "10-Q", "Filing", "Annual"), wait_ms=15000, min_body_len=500,
                 markers=["200,200,1,300,240,Ticker search",
                          "500,300,2,600,350,Filing documents table",
                          "900,200,3,1000,240,Document type filter"]),
        # FORECASTING
        ShotSpec("forecasting", f"/forecasting?ticker={TICKER}", "forecasting.png",
                 ("REVENUE FORECASTING", "Walmart"), wait_ms=12000,
                 markers=["200,200,1,350,240,Ticker selector",
                          "600,300,2,700,350,Forecast chart",
                          "900,200,3,1050,240,Refresh controls"]),
        ShotSpec("forecasting", f"/forecasting?ticker={TICKER}", "forecasting-quarterly.png",
                 ("Quarterly", "Quarter"), click_selector="text=Quarterly", wait_ms=12000,
                 markers=["400,200,1,500,240,Quarterly period toggle",
                          "600,350,2,700,400,Quarterly forecast table"]),
        ShotSpec("forecasting", f"/forecasting?ticker={TICKER}", "forecasting-refresh-dialog.png",
                 ("Refresh Forecasting", "Ticker"), click="Refresh Data", dialog=True, wait_ms=12000,
                 markers=["300,200,1,450,250,Refresh dialog title",
                          "600,350,2,700,400,Company refresh table"]),
        # ADMIN PAGES
        ShotSpec("access-management", "/access_management", "access-management.png",
                 ("Access Management", "All Users"), wait_ms=12000, min_body_len=500,
                 error_extra=("Access Denied",),
                 markers=["120,265,1,372,265,Page badge",
                          "120,315,2,500,315,Users and Roles tab",
                          "120,450,3,400,450,All Users table"]),
        ShotSpec("retailer-adding", "/retailer_adding", "retailer-adding.png",
                 ("Retailer Manager", "coreiq_companies"), wait_ms=12000,
                 markers=["120,200,1,350,240,Retailer Manager title",
                          "400,350,2,500,400,Editable retailer grid",
                          "900,200,3,1000,240,Bulk upload section"]),
        ShotSpec("company-filings-add-files", "/company_filings_add_files",
                 "company-filings-add-files.png",
                 ("File Manager", "Upload"), wait_ms=15000, min_body_len=300,
                 markers=["120,200,1,350,240,File Manager title",
                          "400,350,2,500,400,Upload form",
                          "800,350,3,900,400,Folder browser"]),
        # LIVE EARNINGS TRANSCRIPT
        ShotSpec("live-earnings-transcript", "/live_earnings_transcript",
                 "live-earnings-transcript-01.png",
                 ("Call Setup", "Transcript Stream"),
                 wait_ms=8000,
                 markers=["60,180,1,500,185,Session stats",
                          "60,285,2,384,285,Company ticker",
                          "1420,600,4,1114,600,Transcript stream"]),
        # LOGIN (no auth)
        ShotSpec("login", "/login", "login-01.png",
                 ("Sign in", "Coresight", "Market Data Portal"),
                 error_extra=("Authentication failed",), wait_ms=4000,
                 markers=["80,200,1,749,200,Portal title",
                          "80,368,2,749,368,Sign in",
                          "1200,38,4,1339,39,Contact Us"]),
        ShotSpec("login", "/login", "login-02-coresight-sso.png",
                 ("Sign in", "Coresight"), wait_ms=4000,
                 markers=["120,293,1,749,293,Email",
                          "120,336,2,749,336,Password",
                          "120,527,4,749,527,Log In"]),
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    ok_pages: set[str] = set()
    skipped_pages: set[str] = set()
    shot_count = 0
    annotated_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        login_page = browser.new_page(viewport=VIEWPORT)
        login_ctx = browser.new_context(viewport=VIEWPORT)
        auth_page = login_ctx.new_page()

        for spec in _specs():
            page = login_page if spec.page_id == "login" else auth_page
            out_path = OUT / spec.filename
            print(f"CAPTURE {spec.page_id}: {spec.path} -> {spec.filename}")

            ok, reason = _capture_page(page, spec, out_path)
            entry = {
                "page": spec.page_id,
                "file": spec.filename,
                "status": "ok" if ok else "skipped",
                "reason": reason,
            }
            manifest.append(entry)

            if ok:
                ok_pages.add(spec.page_id)
                shot_count += 1
                time.sleep(2)
                if spec.markers:
                    ann_name = spec.annotated_name or spec.filename.replace(".png", "-annotated.png")
                    ann_path = OUT / ann_name
                    try:
                        _annotate(out_path, ann_path, spec.markers)
                        annotated_count += 1
                        entry["annotated"] = ann_name
                    except Exception as exc:
                        entry["annotation_error"] = str(exc)
            else:
                skipped_pages.add(spec.page_id)
                if out_path.exists():
                    out_path.unlink()
                print(f"  SKIP: {reason}")

        login_page.close()
        auth_page.close()
        login_ctx.close()
        browser.close()

    manifest_path = OUT / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    print("\n=== SUMMARY ===")
    print(f"Screenshots OK: {shot_count}")
    print(f"Annotated: {annotated_count}")
    print(f"Pages OK: {sorted(ok_pages)}")
    print(f"Pages with skips: {sorted(skipped_pages - ok_pages)}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
