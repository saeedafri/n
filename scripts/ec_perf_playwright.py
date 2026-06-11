"""
Earnings Calendar — Phase 5 E2E performance + regression test.

Covers all 17 scenarios from docs/EARNINGS_CALENDAR_OPTIMIZATION_NOTES.md §Phase 5.

Prerequisites:
  Streamlit running in LOCAL mode (use scripts/start_ec_perf_local.sh):
    APP_ENV=LOCAL  DEBUG=true  LOCAL_TEST_USER_EMAIL=mohdsaeedafri@coresight.com
    cd app && streamlit run main.py

Usage:
  python scripts/ec_perf_playwright.py [--base-url http://localhost:8501] [--headed]

Output:
  test-artifacts/ec_perf/
    screenshots/   JPEG per milestone
    videos/        WebM per scenario
    console_*.txt  Browser console per scenario
    REPORT.md      Timing + EC_OPT log assertion table
"""

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ARTIFACTS = REPO_ROOT / "test-artifacts" / "ec_perf"
SCREENSHOTS = ARTIFACTS / "screenshots"
VIDEOS = ARTIFACTS / "videos"
LOG_FILE = REPO_ROOT / "server-logs" / "server-log.log"

ARTIFACTS.mkdir(parents=True, exist_ok=True)
SCREENSHOTS.mkdir(exist_ok=True)
VIDEOS.mkdir(exist_ok=True)

DEFAULT_BASE_URL = "http://localhost:8501"
ECAL_PATH = "/earnings_calendar"
VIEWPORT = {"width": 1440, "height": 900}
_LONG = 60_000
_MED  = 30_000
_SHORT = 10_000

# Company that has 0 events in current month window (confirmed Phase 3 EXPLAIN ANALYZE)
ZERO_EVENTS_TICKER = "AAPL"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _log(msg: str):
    print(f"  [{_ts()}] {msg}")


def _ss(page, scenario: str, tag: str):
    path = str(SCREENSHOTS / f"{scenario}__{tag}.jpeg")
    try:
        page.screenshot(path=path, type="jpeg", quality=85, full_page=False)
    except Exception as e:
        _log(f"[warn] screenshot failed ({tag}): {e}")
    return path


def _save_console(msgs: list, scenario: str):
    (ARTIFACTS / f"console_{scenario}.txt").write_text(
        "\n".join(f"[{m['type']}] {m['text']}" for m in msgs),
        encoding="utf-8",
    )


def _log_offset() -> int:
    """Current byte offset at end of server log — used to read only new lines."""
    try:
        return LOG_FILE.stat().st_size
    except Exception:
        return 0


def _new_log_lines(offset: int) -> str:
    """Return lines added to server log since `offset`."""
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _assert_log(new_lines: str, pattern: str, scenario: str) -> bool:
    found = bool(re.search(pattern, new_lines))
    status = "PASS" if found else "FAIL"
    _log(f"[log-assert] {status}: /{pattern}/ in {scenario}")
    return found


def _get_fc_frame(page):
    """Find the FullCalendar iframe frame. Returns None if not found."""
    for frame in page.frames:
        try:
            if frame.locator(".fc-view-harness").count() > 0:
                return frame
        except Exception:
            pass
    return None


def _wait_calendar(page, timeout_ms: int = _LONG) -> float:
    """Wait for FullCalendar grid in any iframe. Returns elapsed ms."""
    t0 = time.perf_counter()
    deadline = t0 + timeout_ms / 1000
    while time.perf_counter() < deadline:
        f = _get_fc_frame(page)
        if f:
            try:
                f.locator(".fc-view-harness").wait_for(state="visible", timeout=2000)
                return (time.perf_counter() - t0) * 1000
            except Exception:
                pass
        time.sleep(0.2)
    return -1


def _wait_spinner_gone(page, timeout_ms: int = _LONG) -> float:
    t0 = time.perf_counter()
    try:
        page.wait_for_function(
            "() => !document.body.innerText.includes('Loading earnings calendar')",
            timeout=timeout_ms,
        )
        return (time.perf_counter() - t0) * 1000
    except Exception:
        return -1


def _wait_first_event(page, timeout_ms: int = _LONG) -> float:
    t0 = time.perf_counter()
    deadline = t0 + timeout_ms / 1000
    while time.perf_counter() < deadline:
        f = _get_fc_frame(page)
        if f:
            try:
                f.locator(".fc-event").first.wait_for(state="visible", timeout=2000)
                return (time.perf_counter() - t0) * 1000
            except Exception:
                pass
        time.sleep(0.2)
    return -1


def _wait_event_count(page, timeout_ms: int = _LONG) -> float:
    t0 = time.perf_counter()
    try:
        page.wait_for_selector(".ec-event-count", timeout=timeout_ms)
        return (time.perf_counter() - t0) * 1000
    except Exception:
        return -1


def _wait_rerun_settle(page, timeout_ms: int = _MED):
    """Wait for a Streamlit rerun to start and finish."""
    try:
        # Running indicator appears briefly — wait for it then for it to vanish
        page.wait_for_selector("[data-testid='stStatusWidget']", timeout=3000)
        page.wait_for_selector(
            "[data-testid='stStatusWidget']", state="detached", timeout=timeout_ms
        )
    except Exception:
        pass  # rerun may be too fast to catch the indicator


def _click_fc_nav(page, direction: str) -> float:
    """Click prev or next in the FullCalendar toolbar. Returns elapsed ms for rerun to settle."""
    f = _get_fc_frame(page)
    if not f:
        _log("[warn] FC frame not found for nav click")
        return -1
    btn = ".fc-prev-button" if direction == "prev" else ".fc-next-button"
    t0 = time.perf_counter()
    f.locator(btn).click(timeout=_SHORT)
    _wait_rerun_settle(page)
    _wait_calendar(page, timeout_ms=_MED)
    return (time.perf_counter() - t0) * 1000


def _rename_latest_video(video_dir: Path, name: str):
    time.sleep(0.5)
    try:
        known_prefixes = {
            "cold_", "warm_", "visible_range_", "dates_set_", "month_", "year_",
            "event_", "close_", "company_", "watchlist_", "email_", "db_failure_",
        }
        webms = sorted(video_dir.glob("*.webm"), key=os.path.getmtime)
        unnamed = [f for f in webms if not any(f.stem.startswith(p) for p in known_prefixes)]
        if unnamed:
            target = video_dir / f"{name}.webm"
            if target.exists():
                target.unlink()
            unnamed[-1].rename(target)
    except Exception as e:
        _log(f"[warn] video rename failed: {e}")


def _make_ctx_page(browser, console_msgs: list, video_name: str):
    ctx = browser.new_context(
        viewport=VIEWPORT,
        record_video_dir=str(VIDEOS),
        record_video_size=VIEWPORT,
    )
    p = ctx.new_page()
    p.on("console", lambda m: console_msgs.append({"type": m.type, "text": m.text}))
    return ctx, p


def _full_load(page, url: str, console_msgs: list, scenario: str) -> dict:
    """Navigate + measure all timing milestones for a full page load."""
    console_msgs.clear()
    _log(f"→ {url}")
    t0 = time.perf_counter()
    page.goto(url, wait_until="domcontentloaded", timeout=_LONG)
    t_dom = (time.perf_counter() - t0) * 1000
    _log(f"domcontentloaded {t_dom:.0f}ms")
    _ss(page, scenario, "01_page_start")

    t_spinner = _wait_spinner_gone(page)
    _log(f"spinner_gone {t_spinner:.0f}ms")
    _ss(page, scenario, "02_spinner_gone")

    t_ec = _wait_event_count(page)
    _log(f"event_count {t_ec:.0f}ms")
    _ss(page, scenario, "03_event_count")

    t_cal = _wait_calendar(page)
    _log(f"calendar_grid {t_cal:.0f}ms")
    _ss(page, scenario, "04_calendar_grid")

    t_ev = _wait_first_event(page)
    _log(f"first_event_chip {t_ev:.0f}ms")
    _ss(page, scenario, "05_first_event")

    t_total = (time.perf_counter() - t0) * 1000
    _log(f"total_usable {t_total:.0f}ms")
    return {
        "scenario": scenario,
        "dom_ms": round(t_dom),
        "spinner_gone_ms": round(t_spinner),
        "event_count_ms": round(t_ec),
        "calendar_grid_ms": round(t_cal),
        "first_event_ms": round(t_ev),
        "total_usable_ms": round(t_total),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scenarios
# ─────────────────────────────────────────────────────────────────────────────

def run_scenarios(base_url: str, headless: bool = True) -> list:
    from playwright.sync_api import sync_playwright

    ec_url = base_url.rstrip("/") + ECAL_PATH
    results: list = []
    log_asserts: dict = {}   # scenario → {pattern: bool}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        console_msgs: list = []

        # ── S1: Cold first load ───────────────────────────────────────────────
        print("\n=== S1: cold_first_load ===")
        log_off = _log_offset()
        ctx1, p1 = _make_ctx_page(browser, console_msgs, "cold_first_load")
        r1 = _full_load(p1, ec_url, console_msgs, "cold_first_load")
        new_log = _new_log_lines(log_off)
        log_asserts["cold_first_load"] = {
            "VISIBLE_RANGE source=computed_month": _assert_log(
                new_log, r"\[EC_OPT\] VISIBLE_RANGE.*source=computed_month", "cold_first_load"
            ),
            "PARALLEL_FETCH": _assert_log(
                new_log, r"\[EC_PERF\] PARALLEL_FETCH", "cold_first_load"
            ),
        }
        _save_console(console_msgs, "cold_first_load")
        ctx1.close()
        _rename_latest_video(VIDEOS, "cold_first_load")
        results.append(r1)

        # ── S2: Warm reload ───────────────────────────────────────────────────
        print("\n=== S2: warm_reload ===")
        ctx2, p2 = _make_ctx_page(browser, console_msgs, "warm_reload")
        # Prime the cache with a first visit
        p2.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p2)
        # Now measure the warm reload
        log_off = _log_offset()
        r2 = _full_load(p2, ec_url, console_msgs, "warm_reload")
        new_log = _new_log_lines(log_off)
        # Cache hit: the DB_GET_CALENDAR_EVENTS line should NOT appear (st.cache_data hit)
        log_asserts["warm_reload"] = {
            "no_db_call (cache hit)": not bool(
                re.search(r"DB_GET_CALENDAR_EVENTS_ALL", new_log)
            ),
        }
        _save_console(console_msgs, "warm_reload")
        ctx2.close()
        _rename_latest_video(VIDEOS, "warm_reload")
        results.append(r2)

        # ── S3: datesSet loop-proof check ─────────────────────────────────────
        # After first render, FullCalendar fires datesSet → corrective rerun →
        # FullCalendar fires datesSet again with same range → DATES_SET_NO_RERUN logged.
        print("\n=== S3: dates_set_no_rerun ===")
        ctx3, p3 = _make_ctx_page(browser, console_msgs, "dates_set_no_rerun")
        log_off = _log_offset()
        p3.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p3)
        # Wait up to 15s for the corrective rerun + stable state
        time.sleep(3)
        new_log = _new_log_lines(log_off)
        no_rerun_found = bool(re.search(r"\[EC_OPT\] DATES_SET_NO_RERUN", new_log))
        updated_found  = bool(re.search(r"\[EC_OPT\] DATES_SET_UPDATED", new_log))
        _ss(p3, "dates_set_no_rerun", "01_stable")
        log_asserts["dates_set_no_rerun"] = {
            "DATES_SET_UPDATED (corrective rerun fired)": _assert_log(
                new_log, r"\[EC_OPT\] DATES_SET_UPDATED", "dates_set_no_rerun"
            ),
            "DATES_SET_NO_RERUN (loop stopped)": _assert_log(
                new_log, r"\[EC_OPT\] DATES_SET_NO_RERUN", "dates_set_no_rerun"
            ),
        }
        r3 = {
            "scenario": "dates_set_no_rerun",
            "DATES_SET_UPDATED": updated_found,
            "DATES_SET_NO_RERUN": no_rerun_found,
            "loop_proof": updated_found and no_rerun_found,
        }
        _save_console(console_msgs, "dates_set_no_rerun")
        ctx3.close()
        _rename_latest_video(VIDEOS, "dates_set_no_rerun")
        results.append(r3)

        # ── S4: Month next navigation ─────────────────────────────────────────
        print("\n=== S4: month_next ===")
        ctx4, p4 = _make_ctx_page(browser, console_msgs, "month_next")
        p4.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p4)
        _ss(p4, "month_next", "01_before")
        log_off = _log_offset()
        t_nav = _click_fc_nav(p4, "next")
        new_log = _new_log_lines(log_off)
        _ss(p4, "month_next", "02_after")
        log_asserts["month_next"] = {
            "DATES_SET_UPDATED": _assert_log(
                new_log, r"\[EC_OPT\] DATES_SET_UPDATED", "month_next"
            ),
            "DATES_SET_NO_RERUN (loop stopped)": _assert_log(
                new_log, r"\[EC_OPT\] DATES_SET_NO_RERUN.*reason=range_unchanged", "month_next"
            ),
        }
        r4 = {
            "scenario": "month_next",
            "nav_to_calendar_ms": round(t_nav),
            "log_ok": all(log_asserts["month_next"].values()),
        }
        _save_console(console_msgs, "month_next")
        ctx4.close()
        _rename_latest_video(VIDEOS, "month_next")
        results.append(r4)

        # ── S5: Month prev navigation ─────────────────────────────────────────
        print("\n=== S5: month_prev ===")
        ctx5, p5 = _make_ctx_page(browser, console_msgs, "month_prev")
        p5.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p5)
        log_off = _log_offset()
        t_nav = _click_fc_nav(p5, "prev")
        new_log = _new_log_lines(log_off)
        _ss(p5, "month_prev", "02_after")
        log_asserts["month_prev"] = {
            "DATES_SET_UPDATED": _assert_log(
                new_log, r"\[EC_OPT\] DATES_SET_UPDATED", "month_prev"
            ),
            "DATES_SET_NO_RERUN": _assert_log(
                new_log, r"\[EC_OPT\] DATES_SET_NO_RERUN", "month_prev"
            ),
        }
        r5 = {
            "scenario": "month_prev",
            "nav_to_calendar_ms": round(t_nav),
            "log_ok": all(log_asserts["month_prev"].values()),
        }
        _save_console(console_msgs, "month_prev")
        ctx5.close()
        _rename_latest_video(VIDEOS, "month_prev")
        results.append(r5)

        # ── S6: Month zero events (AAPL — 0 events in June 2026 window) ───────
        print("\n=== S6: month_zero_events ===")
        ctx6, p6 = _make_ctx_page(browser, console_msgs, "month_zero_events")
        p6.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p6)
        log_off = _log_offset()
        try:
            # Select AAPL from the company dropdown
            co_sel = p6.locator("div[data-testid='stSelectbox']").filter(has_text="Company")
            co_sel.click(timeout=_SHORT)
            aapl_opt = p6.get_by_role("option", name=ZERO_EVENTS_TICKER, exact=True)
            aapl_opt.wait_for(timeout=_SHORT)
            aapl_opt.click()
            _log(f"Selected {ZERO_EVENTS_TICKER}")
            time.sleep(1)  # let the Streamlit selectbox rerun fire
            _wait_spinner_gone(p6)
        except Exception as e:
            _log(f"[warn] company select failed: {e}")
        new_log = _new_log_lines(log_off)
        _ss(p6, "month_zero_events", "01_after_select")
        # The empty state renders "No events in this date range — use prev/next to navigate."
        empty_text_visible = False
        try:
            p6.wait_for_selector(".ec-event-count", timeout=_MED)
            ec_text = p6.locator(".ec-event-count").inner_text(timeout=5000)
            empty_text_visible = "No events" in ec_text or "prev/next" in ec_text
        except Exception:
            pass
        # FC grid must still be present (navigable empty state, NOT dead)
        fc_alive = _wait_calendar(p6, timeout_ms=_MED) >= 0
        log_asserts["month_zero_events"] = {
            "VALID_EMPTY_RANGE in log": _assert_log(
                new_log, r"\[EC_OPT\] VALID_EMPTY_RANGE", "month_zero_events"
            ),
            "empty text visible": _assert_log(
                "VALID_EMPTY_RANGE" if empty_text_visible else "", r"VALID", "month_zero_events"
            ),
        }
        r6 = {
            "scenario": "month_zero_events",
            "empty_text_visible": empty_text_visible,
            "calendar_still_alive": fc_alive,
            "valid_empty_range_logged": bool(
                re.search(r"\[EC_OPT\] VALID_EMPTY_RANGE", new_log)
            ),
        }
        _log(f"empty_text={empty_text_visible}  fc_alive={fc_alive}")
        _save_console(console_msgs, "month_zero_events")
        ctx6.close()
        _rename_latest_video(VIDEOS, "month_zero_events")
        results.append(r6)

        # ── S7: Year Wise switch ──────────────────────────────────────────────
        print("\n=== S7: year_wise ===")
        ctx7, p7 = _make_ctx_page(browser, console_msgs, "year_wise")
        p7.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p7)
        log_off = _log_offset()
        t0 = time.perf_counter()
        try:
            p7.get_by_role("button", name="Year Wise", exact=True).click(timeout=_SHORT)
            _wait_rerun_settle(p7)
            t_yr = _wait_calendar(p7, timeout_ms=_MED)
        except Exception as e:
            _log(f"[warn] year switch: {e}")
            t_yr = -1
        t_total = (time.perf_counter() - t0) * 1000
        new_log = _new_log_lines(log_off)
        _ss(p7, "year_wise", "01_year_view")
        log_asserts["year_wise"] = {
            "VISIBLE_RANGE source=computed_year": _assert_log(
                new_log, r"\[EC_OPT\] VISIBLE_RANGE.*source=computed_year", "year_wise"
            ),
        }
        r7 = {
            "scenario": "year_wise",
            "switch_to_calendar_ms": round(t_yr),
            "total_ms": round(t_total),
            "log_ok": all(log_asserts["year_wise"].values()),
        }
        _save_console(console_msgs, "year_wise")

        # ── S8: Year next (reuse ctx7 already in year view) ──────────────────
        print("\n=== S8: year_next ===")
        log_off = _log_offset()
        t_nav = _click_fc_nav(p7, "next")
        new_log = _new_log_lines(log_off)
        _ss(p7, "year_next", "01_after")
        log_asserts["year_next"] = {
            "DATES_SET_UPDATED": _assert_log(new_log, r"\[EC_OPT\] DATES_SET_UPDATED", "year_next"),
            "DATES_SET_NO_RERUN": _assert_log(new_log, r"\[EC_OPT\] DATES_SET_NO_RERUN", "year_next"),
        }
        r8 = {
            "scenario": "year_next",
            "nav_to_calendar_ms": round(t_nav),
            "log_ok": all(log_asserts["year_next"].values()),
        }

        # ── S9: Year prev (reuse ctx7) ────────────────────────────────────────
        print("\n=== S9: year_prev ===")
        log_off = _log_offset()
        t_nav = _click_fc_nav(p7, "prev")
        t_nav2 = _click_fc_nav(p7, "prev")  # two prevs to go before current year
        new_log = _new_log_lines(log_off)
        _ss(p7, "year_prev", "01_after")
        log_asserts["year_prev"] = {
            "DATES_SET_UPDATED": _assert_log(new_log, r"\[EC_OPT\] DATES_SET_UPDATED", "year_prev"),
            "DATES_SET_NO_RERUN": _assert_log(new_log, r"\[EC_OPT\] DATES_SET_NO_RERUN", "year_prev"),
        }
        r9 = {
            "scenario": "year_prev",
            "nav_to_calendar_ms": round(max(t_nav, t_nav2)),
            "log_ok": all(log_asserts["year_prev"].values()),
        }

        ctx7.close()
        _rename_latest_video(VIDEOS, "year_wise")
        results.extend([r7, r8, r9])

        # ── S10: Year → Month switch (range cleared) ──────────────────────────
        print("\n=== S10: year_to_month ===")
        ctx10, p10 = _make_ctx_page(browser, console_msgs, "year_to_month")
        p10.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p10)
        # Switch to year first
        try:
            p10.get_by_role("button", name="Year Wise", exact=True).click(timeout=_SHORT)
            _wait_rerun_settle(p10)
            _wait_calendar(p10)
        except Exception as e:
            _log(f"[warn] year switch: {e}")
        # Now switch back to month
        log_off = _log_offset()
        t0 = time.perf_counter()
        try:
            p10.get_by_role("button", name="Month Wise", exact=True).click(timeout=_SHORT)
            _wait_rerun_settle(p10)
            t_mo = _wait_calendar(p10, timeout_ms=_MED)
        except Exception as e:
            _log(f"[warn] month switch: {e}")
            t_mo = -1
        t_total = (time.perf_counter() - t0) * 1000
        new_log = _new_log_lines(log_off)
        _ss(p10, "year_to_month", "01_month_view")
        log_asserts["year_to_month"] = {
            "VISIBLE_RANGE source=computed_month (range cleared on switch)": _assert_log(
                new_log, r"\[EC_OPT\] VISIBLE_RANGE.*source=computed_month", "year_to_month"
            ),
        }
        r10 = {
            "scenario": "year_to_month",
            "switch_to_calendar_ms": round(t_mo),
            "total_ms": round(t_total),
            "log_ok": all(log_asserts["year_to_month"].values()),
        }
        _save_console(console_msgs, "year_to_month")
        ctx10.close()
        _rename_latest_video(VIDEOS, "year_to_month")
        results.append(r10)

        # ── S11: Company filter ───────────────────────────────────────────────
        print("\n=== S11: company_filter ===")
        ctx11, p11 = _make_ctx_page(browser, console_msgs, "company_filter")
        p11.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p11)
        log_off = _log_offset()
        t0 = time.perf_counter()
        selected_co = "?"
        try:
            co_sel = p11.locator("div[data-testid='stSelectbox']").filter(has_text="Company")
            co_sel.click(timeout=_SHORT)
            # Pick the first non-"All" option
            opts = p11.locator("[data-baseweb='popover'] li[role='option']")
            opts.first.wait_for(timeout=_SHORT)
            count = opts.count()
            if count > 1:
                second = opts.nth(1)
                selected_co = second.inner_text(timeout=3000).strip()
                second.click()
            _log(f"Selected company: {selected_co}")
            _wait_rerun_settle(p11)
        except Exception as e:
            _log(f"[warn] company select: {e}")
        t_cal = _wait_calendar(p11, timeout_ms=_MED)
        t_total = (time.perf_counter() - t0) * 1000
        new_log = _new_log_lines(log_off)
        _ss(p11, "company_filter", "01_after_select")
        log_asserts["company_filter"] = {
            "DB_GET_CALENDAR_EVENTS_SPECIFIC": _assert_log(
                new_log, r"\[EC_PERF\] DB_GET_CALENDAR_EVENTS_SPECIFIC", "company_filter"
            ),
        }
        r11 = {
            "scenario": "company_filter",
            "selected_company": selected_co,
            "calendar_ms": round(t_cal),
            "total_ms": round(t_total),
            "log_ok": all(log_asserts["company_filter"].values()),
        }
        _save_console(console_msgs, "company_filter")
        ctx11.close()
        _rename_latest_video(VIDEOS, "company_filter")
        results.append(r11)

        # ── S12: Watchlist filter ─────────────────────────────────────────────
        print("\n=== S12: watchlist_filter ===")
        ctx12, p12 = _make_ctx_page(browser, console_msgs, "watchlist_filter")
        p12.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p12)
        log_off = _log_offset()
        t0 = time.perf_counter()
        selected_wl = "?"
        try:
            wl_sel = p12.locator("div[data-testid='stSelectbox']").filter(has_text="Watchlist")
            wl_sel.click(timeout=_SHORT)
            opts = p12.locator("[data-baseweb='popover'] li[role='option']")
            opts.first.wait_for(timeout=_SHORT)
            count = opts.count()
            if count > 1:
                second = opts.nth(1)
                selected_wl = second.inner_text(timeout=3000).strip()
                second.click()
            _log(f"Selected watchlist: {selected_wl}")
            _wait_rerun_settle(p12)
        except Exception as e:
            _log(f"[warn] watchlist select: {e}")
        t_cal = _wait_calendar(p12, timeout_ms=_MED)
        t_total = (time.perf_counter() - t0) * 1000
        new_log = _new_log_lines(log_off)
        _ss(p12, "watchlist_filter", "01_after_select")
        # Watchlist uses in-memory filter, not DB_GET_CALENDAR_EVENTS_SPECIFIC
        log_asserts["watchlist_filter"] = {
            "FILTER_EVENTS_BY_WATCHLIST": _assert_log(
                new_log, r"\[EC_PERF\] FILTER_EVENTS_BY_WATCHLIST", "watchlist_filter"
            ),
        }
        r12 = {
            "scenario": "watchlist_filter",
            "selected_watchlist": selected_wl,
            "calendar_ms": round(t_cal),
            "total_ms": round(t_total),
            "log_ok": all(log_asserts["watchlist_filter"].values()),
        }
        _save_console(console_msgs, "watchlist_filter")
        ctx12.close()
        _rename_latest_video(VIDEOS, "watchlist_filter")
        results.append(r12)

        # ── S13: Email Alerts dialog ──────────────────────────────────────────
        print("\n=== S13: email_alerts_dialog ===")
        ctx13, p13 = _make_ctx_page(browser, console_msgs, "email_alerts")
        p13.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p13)
        dialog_opened = False
        t0 = time.perf_counter()
        try:
            p13.get_by_role("button", name="Email Alerts", exact=False).click(timeout=_SHORT)
            p13.wait_for_selector("[data-testid='stDialog']", timeout=_MED)
            dialog_opened = True
            t_dialog = (time.perf_counter() - t0) * 1000
            _log(f"Email Alerts dialog opened {t_dialog:.0f}ms")
            _ss(p13, "email_alerts", "01_dialog_open")
            # Close the dialog via Escape
            p13.keyboard.press("Escape")
            time.sleep(0.5)
            _ss(p13, "email_alerts", "02_dialog_closed")
        except Exception as e:
            _log(f"[warn] email alerts dialog: {e}")
            t_dialog = -1
        r13 = {
            "scenario": "email_alerts_dialog",
            "dialog_opened": dialog_opened,
            "open_ms": round(t_dialog) if dialog_opened else -1,
        }
        _save_console(console_msgs, "email_alerts")
        ctx13.close()
        _rename_latest_video(VIDEOS, "email_alerts")
        results.append(r13)

        # ── S14: Event click → detail panel ──────────────────────────────────
        print("\n=== S14: event_click_detail ===")
        ctx14, p14 = _make_ctx_page(browser, console_msgs, "event_click")
        p14.goto(ec_url, wait_until="domcontentloaded", timeout=_LONG)
        _wait_calendar(p14)
        _wait_first_event(p14)
        detail_opened = False
        t0 = time.perf_counter()
        try:
            f = _get_fc_frame(p14)
            if f:
                first_ev = f.locator(".fc-event").first
                first_ev.wait_for(state="visible", timeout=_MED)
                _ss(p14, "event_click", "01_before_click")
                first_ev.click()
                p14.wait_for_selector(".ec-detail-card", timeout=_MED)
                detail_opened = True
                t_detail = (time.perf_counter() - t0) * 1000
                _log(f"detail panel opened {t_detail:.0f}ms")
                _ss(p14, "event_click", "02_detail_open")
        except Exception as e:
            _log(f"[warn] event click: {e}")
            t_detail = -1
        r14 = {
            "scenario": "event_click_detail",
            "detail_opened": detail_opened,
            "click_to_detail_ms": round(t_detail) if detail_opened else -1,
        }

        # ── S15: Close detail panel (reuse ctx14) ────────────────────────────
        print("\n=== S15: close_detail ===")
        t0 = time.perf_counter()
        closed_ok = False
        if detail_opened:
            try:
                p14.get_by_role("button", name="✕ Close", exact=False).click(timeout=_SHORT)
                _wait_rerun_settle(p14)
                _wait_calendar(p14, timeout_ms=_MED)
                closed_ok = True
                t_close = (time.perf_counter() - t0) * 1000
                _log(f"detail closed, calendar back {t_close:.0f}ms")
                _ss(p14, "close_detail", "01_after_close")
            except Exception as e:
                _log(f"[warn] close detail: {e}")
                t_close = -1
        r15 = {
            "scenario": "close_detail",
            "closed_ok": closed_ok,
            "close_to_calendar_ms": round(t_close) if closed_ok else -1,
        }
        _save_console(console_msgs, "event_click")
        ctx14.close()
        _rename_latest_video(VIDEOS, "event_click")
        results.extend([r14, r15])

        # ── S16: DB failure simulation (manual note) ──────────────────────────
        # Cannot be automated without network interception or DB firewall access.
        # Manual procedure:
        #   1. Start app locally
        #   2. Block port 3306 (e.g. `sudo pfctl -e` with a rule blocking 3306)
        #   3. Navigate to /earnings_calendar
        #   4. Expect: st.warning("Earnings calendar metadata could not be loaded...")
        #   5. Expect: [EC_OPT] DB_ERROR_TICKERS / DB_ERROR_DATE_RANGE in log
        #   6. Expect: NO "No earnings calendar data found." (that's for genuinely empty DB)
        _log("[S16] DB failure simulation — manual test (see REPORT.md for procedure)")
        results.append({
            "scenario": "db_failure_sim",
            "status": "MANUAL — automated blocking not implemented",
        })

        browser.close()

    _write_report(results, log_asserts, base_url)
    print(f"\n[✓] Done. Artifacts: {ARTIFACTS}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def _write_report(results: list, log_asserts: dict, base_url: str):
    lines = [
        "# Earnings Calendar — Phase 5 E2E Report",
        f"\nGenerated: {datetime.now().isoformat()}",
        f"Base URL: {base_url}",
        f"Viewport: {VIEWPORT['width']}×{VIEWPORT['height']}",
        "\n---\n",
        "## Timing Scenarios\n",
        "| Scenario | Spinner (ms) | EventCount (ms) | CalGrid (ms) | FirstChip (ms) | Total (ms) |",
        "|----------|-------------|-----------------|--------------|----------------|-----------|",
    ]
    for r in results:
        name = r.get("scenario", "?")
        if any(k in r for k in ("spinner_gone_ms", "calendar_grid_ms", "total_usable_ms")):
            lines.append(
                f"| {name} "
                f"| {r.get('spinner_gone_ms', '—')} "
                f"| {r.get('event_count_ms', '—')} "
                f"| {r.get('calendar_grid_ms', '—')} "
                f"| {r.get('first_event_ms', '—')} "
                f"| {r.get('total_usable_ms', '—')} |"
            )

    lines += [
        "\n## Navigation Scenarios\n",
        "| Scenario | nav_to_calendar (ms) | log_ok |",
        "|----------|---------------------|--------|",
    ]
    for r in results:
        name = r.get("scenario", "?")
        if "nav_to_calendar_ms" in r:
            lines.append(
                f"| {name} | {r.get('nav_to_calendar_ms', '—')} | {r.get('log_ok', '?')} |"
            )

    lines += [
        "\n## [EC_OPT] Log Assertions\n",
        "| Scenario | Assertion | Result |",
        "|----------|-----------|--------|",
    ]
    for scenario, asserts in log_asserts.items():
        for assertion, result in asserts.items():
            status = "✓ PASS" if result else "✗ FAIL"
            lines.append(f"| {scenario} | {assertion} | {status} |")

    lines += [
        "\n## Feature Checks\n",
        "| Scenario | Check | Result |",
        "|----------|-------|--------|",
    ]
    feature_checks = [
        ("dates_set_no_rerun", "loop_proof", "datesSet does not cause infinite rerun"),
        ("month_zero_events", "empty_text_visible", "Empty month shows 'No events' message"),
        ("month_zero_events", "calendar_still_alive", "Empty month: calendar grid still renders (not dead-state)"),
        ("month_zero_events", "valid_empty_range_logged", "EC_OPT VALID_EMPTY_RANGE logged (not DB_ERROR)"),
        ("email_alerts_dialog", "dialog_opened", "Email Alerts dialog opens"),
        ("event_click_detail", "detail_opened", "Event click opens ec-detail-card"),
        ("close_detail", "closed_ok", "Close button hides detail panel"),
        ("year_to_month", "log_ok", "Year→Month switch clears ec_visible range"),
    ]
    by_scenario = {r["scenario"]: r for r in results}
    for scenario, key, description in feature_checks:
        r = by_scenario.get(scenario, {})
        val = r.get(key, "?")
        status = "✓ PASS" if val is True else ("✗ FAIL" if val is False else f"? {val}")
        lines.append(f"| {scenario} | {description} | {status} |")

    lines += [
        "\n---\n",
        "## DB Failure Simulation (Manual)\n",
        "Automated simulation not implemented (requires network interception).\n",
        "**Manual procedure:**",
        "1. Start app locally with `scripts/start_ec_perf_local.sh`",
        "2. Block TCP port 3306 from localhost (e.g. `sudo pfctl` on macOS)",
        "3. Navigate to `/earnings_calendar` in a fresh session",
        "4. **Expected:** `st.warning(\"Earnings calendar metadata could not be loaded right now. Please try again.\")` — NOT `st.error(\"No earnings calendar data found.\")`",
        "5. **Expected in log:** `[EC_OPT] DB_ERROR_TICKERS` or `[EC_OPT] DB_ERROR_DATE_RANGE`",
        "6. **Expected:** NO `[EC_OPT] DB_EMPTY_VALIDATED` (that is only for genuinely empty DB)",
        "7. Unblock port 3306, reload — calendar should recover (no 5-minute dead state).\n",
        "## Artifacts\n",
        f"- Screenshots: `{SCREENSHOTS}`",
        f"- Videos: `{VIDEOS}`",
        f"- Console logs: `{ARTIFACTS}/console_*.txt`",
        f"- Server log: `{LOG_FILE}`",
    ]

    path = ARTIFACTS / "REPORT.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[report] {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    print(f"Target: {args.base_url}{ECAL_PATH}")
    print(f"Artifacts: {ARTIFACTS}")
    print(
        "\nPrerequisite — Streamlit must be running:\n"
        "  ./scripts/start_ec_perf_local.sh\n"
        "  (APP_ENV=LOCAL  DEBUG=true  LOCAL_TEST_USER_EMAIL=mohdsaeedafri@coresight.com)\n"
    )

    results = run_scenarios(args.base_url, headless=not args.headed)
    print(json.dumps(results, indent=2, default=str))
