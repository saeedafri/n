# Calendar page — rename + cold-load performance investigation

**Date:** 2026-07-24
**Author:** Claude (Opus 4.8) + Mohd Saeed Afri
**Scope:** (1) rename "Earnings Calendar" → "Calendar" (heading + URL + file); (2) root-cause
the slow cold load. **No data touched** — investigation is read-only; all fixes are display/
config/cache only.

---

## Part 1 — Rename (DONE, verified in UI)

| Aspect | Before | After |
|--------|--------|-------|
| Heading (2nd line) | `Earnings Calendar` | `Calendar` |
| URL | `/earnings_calendar` | `/calendar` |
| Page file | `app/pages/earnings_calendar.py` | `app/pages/calendar.py` |
| Internal id (`current_page`, `_active_page`) | `earnings_calendar` | **unchanged** (invisible to users; keeps session-state comparisons intact) |

Files touched: `app/main.py` (registration path + `url_path`), `app/components/navigation.py`
(loading map key, 2 hrefs, hidden-nav page_link path), `app/core/boot_overlay.py` (loading map
key), `app/pages/calendar.py` (heading), `app/utils/ma_8k_extract.py` (comment).

**Shadowing check:** `calendar.py` collides in name with stdlib `calendar`, which
`repository.py`, forecast services, and the page itself import. Verified SAFE: Streamlit's
`modified_sys_path` only ever puts the **main script dir** (`app/`) on `sys.path`, never the
page dir (`app/pages/`), so `import calendar` everywhere still resolves to stdlib. Verified in
UI: `/calendar` renders, calendar loads (114 events in view), nav intact, no import errors.

---

## Part 2 — Cold-load performance (root cause)

### How it was measured (read-only)
- Fresh **incognito** Playwright context (cold cache/cookies), cold server (restarted →
  empty `st.cache_data`), navigate `/calendar`, record **webm video** + **HAR** + client phase
  milestones + resource waterfall. Server side: `APP_TIMING=1` → `server-log.log`.
- Artifacts: `calendar_cold_load.mp4`, `calendar_cold.har`, `coldload_report.json`.

### Client timeline (cold server + cold browser, local dev over VPN)
| Phase | ms |
|-------|----|
| boot splash visible | 202 |
| boot splash hidden | 1,615 |
| "Loading Calendar" sticky spinner visible | 1,771 |
| **sticky spinner removed** | **22,197** |
| calendar iframe painted | **26,998** |

→ **~27 s to interactive**, of which **~20 s is the branded "Loading…" spinner** (video:
identical spinner card at t=2 s and t=15 s). No blank screen — the overlay covers it; the
problem is *duration of the background data load*, not a paint glitch.

### Server timeline — the real bottleneck (cold cache)
| Timing | Cold | Warm |
|--------|------|------|
| `EC_PAGE_FETCH_TICKERS` (`get_available_tickers`) | **10,774 ms** | 0 ms |
| `EC_PAGE_FETCH_ALL_MA` (`get_ma_completion_events`) | **14,133 ms** | 0 ms |
| `EC_PAGE_PARALLEL_TOTAL` | **24,910 ms** | 20 ms |
| `PAGE_LOAD TOTAL` | **24,941 ms** | 43 ms |

The 9-worker `ThreadPoolExecutor` (calendar.py:3126) fires every query at a **cold connection
pool** at once; the two heavy uncached queries plus first-time TCP/SSL handshakes to Azure
MySQL serialize into ~25 s. Once warm, the whole parallel block is 20 ms.

> Absolute numbers are **VPN-inflated** (dev Mac → STG DB). On STG the DB is co-located
> (~1 ms), so cold is faster there — but the **structure is real**: `@st.cache_data(ttl=600)`
> means the expensive path recurs **every 10 minutes** and after **every deploy** (STG deploys
> ~6×/day → cold caches much of the day), so clients repeatedly hit a multi-second cold load.

### Front-end costs (STG-real, amplified by India ~233 ms RTT / http-1.1)
From the HAR — cross-origin, render-blocking, on the boot splash + every loader:
1. **Logo from external WordPress CDN** `…azurefd.net/…/coresight-logo*.png` — 2,152 ms +
   1,087 ms here. Used in `boot_overlay.py`, `loading.py` (`_CS_LOGO_URL`), `navigation.py`,
   login/logout/footer. Blocks the branded first paint.
2. **Google Fonts** `fonts.googleapis.com/css2` + `fonts.gstatic.com` woff2 (Montserrat/Inter/
   Roboto/Material Symbols) — 305–1,456 ms each, render-blocking. In `styles.py`,
   `navigation.py`, several pages.

### Ruled OUT (not the app / not STG)
- `webhooks.fivetran.com/…` POST ×3 — **not in the repo** (repo-wide grep = 0), no JS
  initiator → this Mac's corporate networking/MDM, not Coresight.
- `data.streamlit.io/metrics.json` — local-dev only; `gatherUsageStats=false` in root
  `.streamlit/config.toml` (loaded on STG, not locally since dev cwd is `app/`).
- `/calendar/_stcore/health` + `host-config` 404 → 200 — Streamlit framework double-probe;
  minor.

---

## Fix plan (proposed — no data writes)

**P0 — kill the cold DB long-pole (biggest win)**
1. Raise `get_available_tickers` / `get_ma_completion_events` / events caches from `ttl=600`
   to a long TTL (e.g. 6–24 h) — the ticker/M&A sets change slowly. Removes the every-10-min
   cold hit.
2. **Warm these caches on boot** in `cache_manager.start_background_warmup()` so the first
   real user after a deploy never pays cold. (Calendar data is already disk-materialized for
   events; extend to tickers + M&A.)
3. Optionally shrink `get_available_tickers` SQL (it does 2× `SELECT DISTINCT` + `UNION ALL`
   over both earnings tables) and confirm `idx_ticker` is used on both.

**P1 — remove render-blocking cross-origin assets (India-latency win)**
4. Serve the Coresight logo as a **local static asset or inline data-URI** (0 network) instead
   of the WordPress CDN — it's on the boot splash + every loader.
5. **Self-host the fonts** (bundle woff2 locally) or at minimum `preconnect` + `font-display:swap`.

**P2 — polish**
6. Sticky loader lingered ~800 ms after the calendar painted (removed 22.2 s vs iframe 27 s in
   cold run; ~300 ms warm) — tighten the "painted" check.

All P0/P1 are display/config/caching only — **no DB writes, no data risk**.

---

## Fixes applied (2026-07-24) — evidence-based

### DB update cadence (probed read-only, so TTL is not arbitrary)
| Source table | Rows | Update signal | Real cadence |
|--------------|------|---------------|--------------|
| `coreiq_nasdaq_earnings_calendar` | 15,815 | `updated_at_utc` (max 07-23 18:09) | **daily** batch (257 rows 07-23) |
| `coreiq_yf_earnings_calendar` | 566 | `ingested_at` (max 07-23 01:33) | **daily** (100 rows 07-23) |
| `coreiq_company_events` (M&A) | 150,893 | `updated_at` (max 07-23 00:18) | **daily**, high volume (1,263 on 07-22) |
| `coreiq_av_companies_all` (IPO/delisted) | 21,975 | `last_seen_at_utc` | **static since 2026-03-24** (one snapshot) |
| `coreiq_companies` (universe/map) | 353 | `data_inserted_at` (max 07-22) | **rarely** (4 rows 07-22) |

Fastest-moving data refreshes **once/day**; IPO/delisted unchanged for 4 months. So the caches
were re-fetching **10–120× more often than the data changes** (ttl 300/600).

**TTL raised 300/600 → 21600 (6h)** on `get_calendar_events_full`, `get_ma_completion_events`,
`get_available_tickers`, `get_ipo_events`, `get_delisted_events`, `_get_fiscal_year_end_map`
(repository.py). 6h refreshes ~4×/day — comfortably catches the daily batch, bounds staleness to
a few hours on once-a-day data, and STG's ~6×/day deploys wipe+re-warm even sooner.

### Warmup reordered (cache_manager.py)
Calendar warmup (Track 0) now runs **before** the ~23s earnings-calls warmup (was after), so a
user opening `/calendar` right after a deploy hits a warm cache instead of the cold DB fan-out.

### RAM safety (measured, not assumed)
- Cached calendar objects total **14.4 MB** (`events_full` 13.8 MB; tickers/M&A/IPO/delisted <1 MB).
- These are **already cached today** — longer TTL adds **no** copies/peak; it keeps the same 14 MB
  resident longer. It **reduces** churn: the ~100 MB pandas/JOIN build burst recurred every
  5–10 min at ttl=300; at 6h it recurs ~4×/day → **lower** average/peak RAM, not higher.
- 14 MB vs 4,794 MB box (observed peak 1.3–2.8 GB) = <0.3%. Prior RAM drivers (ratings sweep +
  duplicate worker) are unrelated. Warmup is sequential + one-time → no steady-state RAM added.

### Front-end: logo served locally (P1)
Coresight logo inlined as a base64 data-URI (`app/utils/brand_assets.py`) instead of the external
WordPress CDN (`…azurefd.net`). Applied to boot splash (`boot_overlay.py`, marker bumped v5→v6 to
force index.html re-patch), sticky loader (`loading.py`), top bar + in-app loader overlay + footer
(`navigation.py`), and login/logout templates (`login.py`). **Verified: 0 external CDN logo
requests** on `/calendar` (was 3–5). Header 18.5 KB inline, footer 63 KB.

### Fonts — NOT changed this round (deliberately)
A `<link rel=preconnect>` prepended to the global CSS block **broke** Streamlit's HTML-block
parsing (raw `<style>` text leaked onto the page) — reverted immediately. Full woff2 self-hosting
needs `enableStaticServing=true` + `app/static/` + STG-proxy testing. Deferred as a separate,
tested change (correct spot: the boot `<head>` injection). Existing setup already has partial
preconnect (navigation.py) + `display=swap` (market_data.py).

### Results (local dev, verified in UI)
- Warm cold-browser load (representative client hitting a live STG): **interactive ~0.8–2.0 s**,
  logo local, heading correct, calendar renders, no CSS leak.
- Server warm render **43 ms** (was 24.9 s cold); cold window now shorter (calendar-first warmup)
  and eliminated between deploys (6h TTL).

## STG "blank / spinner-gone / hung calendar" — hang fix (2026-07-24)
Symptom (STG, incognito): header + "Calendar" render, then a blank body with NO spinner — hung.

Root cause: the on-load parallel fetch (`calendar.py` render_page) read every future with
**unbounded `.result()`** inside a `with ThreadPoolExecutor(...)` block. Two hang vectors:
1. A cold/slow/hung `get_available_tickers` (or any fetch) blocks `.result()` **forever**.
2. `with` exit calls `shutdown(wait=True)`, which also **joins the fire-and-forget
   `_warm_candidate_company`** task — so even after all reads, a slow warm task blocks exit.
Either way the render never completes, so the sticky loader hits its 20s hard-cap and hides →
blank, spinner-gone, hung. (Pre-existing in deployed code; exposed by cold caches post-deploy.)

Fix: manual executor with an **18s shared wall-clock deadline** on every `.result()` (missed
fetch → its empty default) and **`shutdown(wait=False)`** so the warm task never blocks. If a
core fetch (tickers/date-range) misses the deadline, clear the loader and show
"Calendar is taking longer than usual to load. Please refresh." (logged `EC_PAGE_FETCH_INCOMPLETE`)
instead of hanging. Verified locally: calendar paints normally (114 events), no hang, no errors.
Combined with the TTL(6h)+warmup fixes above, cold loads are rare AND can no longer hang.

## Verification plan
- Re-run the cold-load recorder after each fix; compare `EC_PAGE_PARALLEL_TOTAL` and
  `sticky_removed`/`iframe_painted` marks.
- Target: cold `PAGE_LOAD TOTAL` from ~25 s → sub-second warm-cache path for all post-deploy
  users (via boot warmup); front-end blocking requests removed from the waterfall.
