"""
Test: Annual → Quarterly period type switch on Key Stats tab.
"""
import os, sys, time
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS    = PROJECT_ROOT / "server-logs" / "e2e-artifacts" / "period_switch_test"
LOG_PATH     = PROJECT_ROOT / "server-logs" / "server-log.log"
ARTIFACTS.mkdir(parents=True, exist_ok=True)

BASE_URL = "http://localhost:8000"
TARGET   = f"{BASE_URL}/market_data?ticker=AAPL&tab=key_stats&period_type=Annual"

def ss(name, page):
    path = str(ARTIFACTS / f"{name}.png")
    try:
        page.screenshot(path=path, full_page=False, timeout=10000)
        print(f"  📸  {name}.png")
    except Exception as e:
        print(f"  ⚠️  screenshot {name} failed: {e}")

def tail_logs(n=30):
    if not LOG_PATH.exists():
        return ""
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])

def wait_for_streamlit(page, timeout_ms=45000):
    """Wait for Streamlit to finish a render cycle."""
    # Streamlit shows a running indicator — wait for it to disappear
    # Try multiple known selectors across Streamlit versions
    RUNNING_SELECTORS = [
        '[data-testid="stSpinner"]',
        '[data-testid="stStatusWidget"]',
        '.stSpinner',
        '[class*="StatusWidget"]',
    ]
    # First check which selectors are currently present
    page.wait_for_timeout(300)
    found = None
    for sel in RUNNING_SELECTORS:
        if page.locator(sel).count() > 0:
            found = sel
            print(f"  → spinner selector: {sel!r}")
            break

    if found:
        try:
            page.wait_for_selector(found, state="detached", timeout=timeout_ms)
            return True
        except PWTimeout:
            return False
    else:
        # No spinner — page might already be done or uses different mechanism
        # Fall back to network idle + fixed wait
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        return True

def main():
    load_dotenv(PROJECT_ROOT / ".env")
    username = os.getenv("LOGIN_USERNAME", "mohdsaeedafri@coresight.com")
    password = os.getenv("LOGIN_PASSWORD", "Welcome@123")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=0)
        ctx  = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        page.set_viewport_size({"width": 1440, "height": 900})

        # ── Load the page ────────────────────────────────────────────────────
        print(f"\n→ {TARGET}\n")
        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        # Handle OIDC login if needed
        if page.locator("#csr-sso-btn").count() > 0:
            print("  → doing OIDC login…")
            page.locator("#csr-sso-btn").click(force=True)
            page.wait_for_timeout(2000)
            try:
                iframe = page.locator("iframe").first
                oidc_url = iframe.get_attribute("src")
                if oidc_url:
                    page.goto(oidc_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            for sel in ["input[name='log']", "input[type='email']", "#user_login"]:
                try:
                    if page.locator(sel).count() > 0:
                        page.locator(sel).first.fill(username); break
                except Exception:
                    pass
            for sel in ["input[name='pwd']", "input[type='password']", "#user_pass"]:
                try:
                    if page.locator(sel).count() > 0:
                        page.locator(sel).first.fill(password); break
                except Exception:
                    pass
            for sel in ["#wp-submit", "button[type='submit']", "input[type='submit']"]:
                try:
                    if page.locator(sel).count() > 0:
                        page.click(sel); break
                except Exception:
                    pass
            page.wait_for_selector(".logout-btn, button:has-text('Logout')", timeout=120000)
            print("  ✅  Logged in")
            page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)

        # ── Wait for Annual to render ────────────────────────────────────────
        print("  → waiting for Annual to load…")
        t_annual = time.perf_counter()
        wait_for_streamlit(page, timeout_ms=45000)
        page.wait_for_timeout(1500)
        ss("01_annual_loaded", page)
        print(f"  ✅  Annual loaded ({(time.perf_counter()-t_annual)*1000:.0f}ms)")

        # Print all data-testid attributes on page for debugging
        testids = page.evaluate("""
            () => [...document.querySelectorAll('[data-testid]')]
                    .map(el => el.getAttribute('data-testid'))
                    .filter((v, i, a) => a.indexOf(v) === i)
        """)
        spinner_ids = [t for t in testids if 'spin' in t.lower() or 'status' in t.lower() or 'running' in t.lower()]
        print(f"  → spinner-related testids: {spinner_ids}")

        # ── Find Period Type selectbox ────────────────────────────────────────
        stselects = page.locator('[data-baseweb="select"]')
        count = stselects.count()
        print(f"\n  → {count} Streamlit selectboxes:")
        period_idx = None
        for i in range(count):
            try:
                txt = stselects.nth(i).inner_text(timeout=2000).strip()
                first_line = txt.split("\n")[0].strip()
                print(f"     [{i}] {first_line!r}")
                if first_line == "Annual":
                    period_idx = i
            except Exception as e:
                print(f"     [{i}] error: {e}")

        if period_idx is None:
            ss("ZZ_no_period_select", page)
            print("  ❌  Period Type selectbox not found")
            browser.close()
            return False

        # ── Click Quarterly ───────────────────────────────────────────────────
        print(f"\n  → Clicking select[{period_idx}] to open Period Type dropdown…")
        t0 = time.perf_counter()
        stselects.nth(period_idx).click()
        page.wait_for_timeout(600)
        ss("02_dropdown_open", page)

        # Find and click Quarterly option
        q_opt = page.locator('[data-baseweb="menu"] li').filter(has_text="Quarterly")
        if q_opt.count() == 0:
            q_opt = page.locator('[role="option"]').filter(has_text="Quarterly")
        if q_opt.count() == 0:
            q_opt = page.locator('li').filter(has_text="Quarterly")
        print(f"  → Quarterly option count: {q_opt.count()}")

        if q_opt.count() == 0:
            ss("ZZ_no_quarterly_opt", page)
            print("  ❌  Quarterly option not visible in dropdown")
            browser.close()
            return False

        q_opt.first.click()
        ss("03_clicked_quarterly", page)
        print(f"  → Quarterly clicked — waiting for render (up to 45s)…")

        # Poll for running indicator (any selector that exists)
        page.wait_for_timeout(300)
        running_sel = None
        for sel in [
            '[data-testid="stSpinner"]',
            '[data-testid="stStatusWidget"]',
            '.stSpinner',
        ]:
            if page.locator(sel).count() > 0:
                running_sel = sel
                print(f"  → spinner detected: {sel!r}")
                break

        if running_sel:
            try:
                page.wait_for_selector(running_sel, state="detached", timeout=45000)
                elapsed = (time.perf_counter() - t0) * 1000
                print(f"  → spinner gone — {elapsed:.0f}ms")
            except PWTimeout:
                elapsed = (time.perf_counter() - t0) * 1000
                ss("ZZ_infinite_spinner", page)
                print(f"\n  ❌  SPINNER STILL RUNNING at {elapsed:.0f}ms")
                print("\n--- LAST 30 LOG LINES ---")
                print(tail_logs(30))
                browser.close()
                return False
        else:
            print("  → no spinner found — page may have rendered immediately")

        page.wait_for_timeout(2000)
        ss("04_quarterly_loaded", page)

        body = page.locator("body").inner_text()
        elapsed = (time.perf_counter() - t0) * 1000
        if "No extracted data" in body or "No key stats data" in body:
            ss("ZZ_empty_state", page)
            print(f"  ❌  Empty state after Quarterly switch ({elapsed:.0f}ms)")
            browser.close()
            return False

        print(f"  ✅  Quarterly loaded OK — {elapsed:.0f}ms")

        # ── Extend end date to show future estimate columns ───────────────────
        # End Date selectbox is index 3 (after Period Type, Start Date)
        print("\n  → Extending end date to show estimate columns…")
        stselects = page.locator('[data-baseweb="select"]')
        count = stselects.count()
        print(f"  → {count} selectboxes now (Quarterly mode):")
        for i in range(count):
            try:
                txt = stselects.nth(i).inner_text(timeout=2000).strip().split("\n")[0].strip()
                print(f"     [{i}] {txt!r}")
            except Exception:
                pass

        # Find End Date selectbox (should contain a date like "September 20...")
        end_date_idx = None
        for i in range(count):
            try:
                txt = stselects.nth(i).inner_text(timeout=2000).strip().split("\n")[0].strip()
                if "2025" in txt or "2024" in txt:
                    end_date_idx = i
            except Exception:
                pass

        if end_date_idx is not None:
            print(f"  → opening End Date selectbox [{end_date_idx}]…")
            stselects.nth(end_date_idx).click()
            page.wait_for_timeout(600)
            ss("04b_end_date_dropdown", page)

            # Pick the last option (most recent/future date available)
            opts = page.locator('[data-baseweb="menu"] li')
            if opts.count() == 0:
                opts = page.locator('[role="option"]')
            total_opts = opts.count()
            print(f"  → {total_opts} date options available")
            if total_opts > 0:
                last_opt_text = opts.last.inner_text(timeout=2000).strip()
                print(f"  → picking last option: {last_opt_text!r}")
                opts.last.click()
                wait_for_streamlit(page, 30000)
                page.wait_for_timeout(2000)
                ss("04c_extended_end_date", page)
                # Scroll table container to rightmost to show estimate columns
                page.evaluate("""
                    () => {
                        document.querySelectorAll('div').forEach(el => {
                            if (el.scrollWidth > el.clientWidth + 50) el.scrollLeft = el.scrollWidth;
                        });
                    }
                """)
                page.wait_for_timeout(800)
                ss("04d_quarterly_with_estimates", page)
                print("  ✅  Extended end date — screenshot taken")

        # ── Switch back Annual ────────────────────────────────────────────────
        print("\n  → Switching back to Annual…")
        stselects = page.locator('[data-baseweb="select"]')
        for i in range(stselects.count()):
            try:
                txt = stselects.nth(i).inner_text(timeout=2000).strip().split("\n")[0].strip()
                if txt == "Quarterly":
                    t1 = time.perf_counter()
                    stselects.nth(i).click()
                    page.wait_for_timeout(500)
                    ann = page.locator('[data-baseweb="menu"] li').filter(has_text="Annual")
                    if ann.count() == 0:
                        ann = page.locator('[role="option"]').filter(has_text="Annual")
                    if ann.count() > 0:
                        ann.first.click()
                    wait_for_streamlit(page, 30000)
                    page.wait_for_timeout(1500)
                    print(f"  ✅  Back to Annual — {(time.perf_counter()-t1)*1000:.0f}ms")
                    break
            except Exception:
                pass

        ss("05_back_to_annual", page)

        print("\n--- LAST 30 LOG LINES ---")
        print(tail_logs(30))
        browser.close()
        return True


if __name__ == "__main__":
    ok = main()
    print(f"\n{'='*50}")
    print(f"RESULT: {'✅ PASS' if ok else '❌ FAIL'}")
    print(f"Screenshots: {ARTIFACTS}")
    print(f"{'='*50}")
    sys.exit(0 if ok else 1)
