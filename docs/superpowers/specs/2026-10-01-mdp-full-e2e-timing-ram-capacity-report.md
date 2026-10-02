# MDP full end-to-end timing, RAM and capacity report

**Date:** 2026-10-01
**Measured on:** a copy of the app built exactly like an STG deploy, run on a MacBook
(10 cores, 16 GB) against the **STG database** over VPN. STG data was only read — no row
was written (the committed code's forecast backfill was switched off in its test copy,
see §7).
**Test server:** `http://localhost:8531` (STG-style deploys), `http://localhost:8501` (dev).
**Harness:** `.claude/dev/perf/` (gitignored). Raw results: `full_*`, `data_*`, `load/`,
`abwatch_*`, `profrun/` in that folder.

---

## 1. What was run

| # | Scenario | Code | Cache at start | Result |
|---|---|---|---|---|
| 1 | Redeploy without cache | now | empty `/home/mdp-cache` + empty EDGAR cache | all suites complete |
| 2 | Redeploy with cache | now | kept from #1 | all suites complete |
| 3 | Redeploy without cache | before (committed `HEAD`) | empty | complete; PDF step skipped (freezes the server) |
| 4 | Redeploy with cache | before | kept from #3 | complete |
| 5 | Data changes: none / rows added / rows edited | now | copy of #1, signatures altered | complete |
| 6 | Load: 1, 5, 10, 20, 30 concurrent users + 30 idle tabs | now | warm | complete, 0 errors |
| 7 | Redeploy without cache (repeat) | now | empty | complete |
| 8 | Redeploy with cache (repeat) | now | kept from #7 | complete |
| 9 | File watcher on vs off, 10 users | now | warm | complete |

Each redeploy ran: a cold pass of every page (first hit after boot), the filings PDF,
3 warm repeats of every page/tab/filter/download, then the gap, extra and interaction
suites (2 repeats each). In total **308 distinct actions** per deploy:

- every page (incl. the admin pages), every Market Data tab by link and by click,
  every Forecasting tab and model tab, Logs tabs;
- every filter and filter change: period, units, sort, start/end date, conversion mode,
  currency, company switch, Screening industry/geography/financial/Key Devs criteria and
  every financial-form field, Calendar month/year/list views, filter panel, chips, company
  filter, Newsroom sort/category/sector/watchlist, Earnings Calls year/quarter/company,
  Filings doc type/year/company;
- every search: Newsroom, Filings, transcript (in one transcript and across all), the
  type-to-search in every dropdown, and the Excel-style search inside every screening grid
  column filter;
- every download, each opened and checked: 11 Market Data Excels, Screening companies +
  Key Devs Excel, Forecasting Excel, filings PDF, transcript PDF, 2 transcript CSVs, Logs;
- every chart hover (Market Data stock chart, every Forecasting chart incl. both Test-tab
  charts) — there are no maps in the app;
- navigation: every header link, page-to-page switching, browser Back/Forward, every
  footer link (internal clicked, external checked reachable), tab switching round trips;
- the loader/spinner time of every action (how long a loader was on screen).

Timing rules: no fixed sleeps; an action ends when Streamlit is idle, no loader is
visible and the page has been quiet for 600 ms; the number reported is the last change on
screen, so the quiet window is never counted. Server time is read from the app's own log.

## 2. Headline answers

1. **Every action works.** 308 actions × 6 deploys: all pass except the Screening
   geography **City** level (no member resolves to a city — data, same before), the
   **Add Files** admin page locally (its package `azure-storage-file-datalake` is in
   requirements.txt but not installed in the local venv) and one test-tool click flake.
2. **Warm (what a user sees after the first visit): 0.3–1.5 s** for page loads, tab
   clicks, filters and downloads — see §3. The server's own work is 10–300 ms; the rest
   is the browser starting Streamlit (≈1 s per full page load, see §5).
3. **Cold after a deploy is much better than before** where it hurt most: Calendar
   17.1 → 7.4 s, Company Filings 15.9 → 7.7 s, Newsroom 9.5 → 3.7 s, Screening Show
   Results 5.0 → 1.7 s; the filings PDF went from **freezing the server** to 2.0 s.
4. **Capacity is limited by CPU, not RAM.** One Streamlit process uses one CPU core
   (Python's lock); each full page load costs ~1.5–2 CPU-seconds of server time. From 5
   concurrent users the core is at 97–99 % and waits grow linearly (§6).
   **Estimate for STG today: about 10–15 people actively clicking at the same time with a
   30 % buffer; ~30–60 people logged in with normal think time.** RAM peaks at 1.1–1.7 GB
   of the 4.8 GB available.
5. **Data changes never block a user.** A changed table is detected on the next read; the
   previous copy is served at once and rebuilt in the background in 0.6–7.8 s. Two
   freshness gaps remain (§8).

## 3. Before vs now — main actions

Before = committed code (`HEAD`), now = working tree. Cold = first hit after a deploy
(single sample per deploy; now = average of 2 deploys). Warm = median of all repeats.

| Page · action | Before: 1st load, no cache | Now: 1st load, no cache | Before: warm, cache | Now: warm, cache |
|---|--:|--:|--:|--:|
| Market Data · Income Statement (link) | 1.4 s | 1.6 s | 955 ms | 990 ms |
| Market Data · Segment tab | 9.5 s | 7.8 s | 1.1 s | 996 ms |
| Market Data · Company Profile | 3.3 s | 3.2 s | 1.4 s | 1.3 s |
| Market Data · click Balance Sheet | 1.5 s | 1.0 s | 365 ms | 362 ms |
| Market Data · Annual → Quarterly | 1.0 s | 1.1 s | 407 ms | 394 ms |
| Market Data · Excel | 1.7 s | 1.1 s | 362 ms | 324 ms |
| Screening · page load | 1.7 s | 2.0 s | 1.2 s | 1.3 s |
| Screening · Show Results (industry) | 5.0 s | 1.7 s | 1.2 s | 1.1 s |
| Screening · Show Results (financial) | 1.6 s | 992 ms | 980 ms | 851 ms |
| Screening · Show Results (Key Devs) | 3.9 s | 2.9 s | 1.0 s | 988 ms |
| Screening · Key Devs full Excel | 2.5 s | 3.4 s | 4.1 s | 2.6 s |
| Calendar · load | 17.1 s | 7.4 s | 1.5 s | 1.5 s |
| Calendar · next month | 489 ms | 859 ms | 365 ms | 380 ms |
| Earnings Calls · load | 3.4 s | 3.8 s | 1.1 s | 1.1 s |
| Earnings Calls · transcript search | 601 ms | 568 ms | 261 ms | 282 ms |
| Earnings Calls · transcript PDF | — | — | 70 ms | 48 ms |
| Earnings Calls · one company, Year = All, "inventory" | 110 s cold | 1.9 s | — | ~1 s |
| Newsroom · load | 9.5 s | 3.7 s | 1.2 s | 1.2 s |
| Newsroom · search | 1.8 s | 3.0 s | 444 ms | 452 ms |
| Company Filings · load | 15.9 s | 7.7 s | 1.3 s | 1.4 s |
| Company Filings · PDF download | **server froze** | 2.0 s | — | — |
| Forecasting · load ¹ | 3.7 s (+2.9–3.5 s) | 3.7 s | 2.2 s (+2.9–3.5 s) | 2.0 s |
| Forecasting · Annual → Quarterly ¹ | 2.9 s | 2.4 s | 1.2 s | 1.3 s |
| Logs · load | 3.4 s | 3.0 s | 1.8 s | 1.4 s |
| Header link → another page | — | — | 1.8 s | 1.9 s |

¹ The committed code also re-wrote 400 forecast rows on **every** Forecasting render
(2.9–3.5 s each, measured before the test copy was guarded). The "before" column
excludes that, so the real before was ~3 s slower.

Single cold samples swing ±2 s with the laptop's VPN round trip (300 ms per query, 2.1 s
per new database connection) — compare the big ratios, not small differences. Every
action's numbers (all 6 deploys, cold and warm, average and max, loader time) are in
Appendix A.

## 4. Everything over 1 second — the bottleneck for each

Warm, now. "Server" = the app's own run time; the rest is the browser.

| Action | Warm | Cold | Why |
|---|--:|--:|---|
| Retailers (admin) load / search | 24 s / 21 s | — | Page opens its own new PyMySQL connection and reloads all of `coreiq_companies` on every rerun, uncached. Not changed here (admin page). |
| Footer: external links | 20 s | — | Test-only: fetching 14 coresight.com pages. Not a user wait. |
| Forecasting load / ticker | 2.0–2.3 s | 3.6–3.9 s | Server 280 ms, but the dashboard builds every model chart (`ESTIMATES_PAGE_LOAD_DASHBOARD` 1.4 s cold); the first run per ticker also fits the models. |
| Key Devs full Excel | 2.6 s | 3.4–4.2 s | Writes every matching event (~145k universe, keyset-paginated) to one workbook. |
| Company Filings (any) | 1.4–1.9 s | 4.6–7.9 s | `FILINGS_LOAD` cold 2–6 s: header metadata + fetching the filing from Azure Blob; the filing HTML is then sent as **one ~1 MB message**. |
| Earnings Calls load | 1.1–1.8 s | 3.8–4.5 s | `get_years_and_quarters` 0.4–0.7 s + transcript fetch on cold. |
| Calendar load | 1.5 s | 7.4 s (17 s before) | Cold: IPO events build (`EC_PAGE_FETCH_ALL_IPO` 5.8 s), now persisted. |
| Market Data Segment | 1.0 s | 7.8 s | Cold per ticker: ~11k rows from `filing_metrics_v5` (row-bound), then persisted per ticker. |
| Market Data Profile / Key Stats | 1.3–1.4 s | 3.2 s | Tab build 1.5–2.3 s cold (company overview + prices). |
| Newsroom load | 1.2 s | 3.7 s | Cold: topic keys + 8-week article window; sends 254 KB. |
| Logs load | 1.4 s | 3.0 s | Segment-cache status (1.3 s, now cached 120 s). |
| Home (cold) | — | 3.8 s | First request after boot pays the news query (`get_articles` 3.5 s) and new database connections. |
| Header link / page switch | 1.4–1.9 s | — | Header links are plain links: every click is a **full page reload and a new server session** (≈1 s of browser start-up + ~105 KB re-sent). |
| Every full page load | ~1 s | — | Browser side: Streamlit's front end starts, then draws; ~105 KB of base64 logos (header 19 KB, footer 67 KB, boot script 19 KB) are re-sent every time and cannot be cached. |

Anything not in this table is under 1 s warm (tab clicks, filters, period/units/sort,
dropdown search 15–50 ms, grid column-filter search 3–6 ms and apply 60–110 ms, chart
hover 47–66 ms, downloads 40–700 ms).

## 5. Why a warm page still takes ~1 s

Measured on one Market Data load: HTML in 6 ms, Streamlit's JavaScript started by
~70 ms, the script run starts at ~210 ms, the server finishes it in **10–30 ms**, and the
page is complete at ~1,000 ms. The gap is the browser receiving and drawing what the
server sent: 99 websocket messages / 204 KB, of which ~105 KB are logo images embedded as
base64 text in the header, footer and boot script — sent again on every page and every
header click because they are part of the page, not files the browser can cache. Over
the India ↔ Central US link (≈233 ms per round trip) that weight matters more than here.

## 6. Capacity — how many users STG can take

### Measured (warm server, each user runs Home → Market Data → tab → Screening →
Newsroom → Calendar → Earnings Calls → Filings → Forecasting, 3 s think time, rotating 8 tickers)

| Users at once | Median wait per step | p95 | Errors | Server CPU | Page steps per second |
|--:|--:|--:|--:|--:|--:|
| 1 | 1.6 s | 2.1 s | 0 | 32 % of one core | 0.19 |
| 5 | 6.3 s | 12.3 s | 0 | 99 % | 0.48 |
| 10 | 16.1 s | 27 s | 0 | 97 % | 0.51 |
| 20 | 37.8 s | 68 s | 0 | 99 % | 0.46 |
| 30 | 61.8 s | 88 s | 0 | 97 % | 0.49 |

Throughput is flat at ~0.5 full page loads per second from 5 users up: the single Python
process is pinned at one core and every extra user just queues. Server CPU per page step:
**1.7–2.2 s** (background work included; ~16 % of a core is background when idle).

### Where the CPU goes (whole-process profile, 10 users)

| Share | What |
|--:|---|
| 32 % | Parsing SEC 10-K text (edgartools `TenK.sections`) for credit ratings — background threads, triggered by Market Data visits and the 12-hourly ratings sweep |
| ~15 % | Azure Blob listing / TLS handshakes |
| 11 % | Page scripts themselves |
| 10 % | Streamlit's file watcher re-scanning every loaded module after each run — **removed** (§9) |
| ~6 % | Web server plumbing |

### Estimate for STG

STG is one App Service instance (P0v3: 2 vCPU, 4.8 GB). Streamlit is one Python process,
so it uses ~1 of the 2 vCPUs. With the file watcher off, a page action costs ~1.5
CPU-seconds on this laptop; Azure vCPUs are typically slower per core than this Apple
chip, so assume 1.5–2.5 s on STG. Keeping 30 % headroom leaves ~0.55 core for users.

- **Actively clicking at the same time** (one action every few seconds): **~10–15 users**
  before waits go above a few seconds.
- **Logged in, working normally** (one action every 20–30 s): **~30–60 users**.
- **RAM is not the limit:** 0.46–0.52 GB after boot, peaks 1.0–1.7 GB during the tests,
  ~1.2 GB under 30 users; 30 idle tabs added almost nothing. 4.8 GB leaves room for ~3×.

How to raise it, biggest first: (1) run a second instance (App Service scale-out to 2 with
session affinity, or 2 Streamlit processes behind the proxy) — doubles it and uses the
second vCPU; (2) stop parsing 10-Ks on the web process (move the ratings/geo sweep to a
scheduled job that writes to the DB or `/home`, and skip tickers whose disk cache is
fresh); (3) make header links switch pages inside the app instead of reloading; (4) serve
the logos as files.

**Verify on STG:** the app already logs exact Linux RAM on every page run —
`[CLICK->RENDER] … rss=…MB avail=…MB` in the Logs page (macOS prints `?`). For CPU,
App Service → Diagnose → CPU usage during a busy hour.

## 7. RAM (server process, resident memory)

| Scenario | After boot | Peak | End of run |
|---|--:|--:|--:|
| Before · no cache | 463 MB | 1,700 MB | 650 MB |
| Before · cache | 524 MB | 1,025 MB | 743 MB |
| Now · no cache (2 deploys) | 480–1,072 MB | 1,548–1,695 MB | 1,030–1,481 MB |
| Now · cache (2 deploys) | 504–513 MB | 1,073–1,141 MB | 727–880 MB |
| Load test, 30 users | — | 1,170 MB | — |

macOS can compress memory under pressure, so its numbers can read low; the STG log line
above is exact. Streamlit itself is ~230 MB at boot; the rest is caches and pandas frames.

## 8. Data changes (simulated — STG never written)

A copy of the persisted cache had its saved signatures altered so the app believed the
source tables had changed, then the app was redeployed on it.

| Change | What the app did | First page load after deploy |
|---|---|---|
| None | 13/13 caches loaded from disk | normal (e.g. Calendar 2.3 s, Home 7.4 s cold) |
| Rows added (any table) | 13/13 noticed; previous copy served at once; rebuilt in the background in 0.6–7.8 s each, 0 failures | same as no change (Calendar 3.7 s, Home 9.8 s) |
| 1 row / 10 rows / old rows edited in place | 8 caches noticed (checksum / max-id signals) and rebuilt in 1.1–5.7 s; **5 did not**: `calendar_events_full`, `delisted_events`, `earnings_transcript_tickers`, `ipo_events`, `non_sec_transcript_companies` — they only compare row counts | same as no change |

One row vs ten rows makes no difference: the signature moves once and the whole cache is
rebuilt in the background either way.

Two freshness gaps to know about (not changed here):
1. The 5 count-only caches above keep serving old values after an in-place edit until a
   row is added or removed (fix: a checksum signal, as `screening_universe` uses).
2. On a running server each persisted cache sits behind an in-memory cache of 1 h (Key
   Devs, all-companies universe) or 6 h (calendar, IPO, M&A, delisted, transcripts,
   topics, fiscal year ends, segment rows). A change reaches users after that, or on the
   next deploy — and on a deploy the first visitor receives the previous copy, which then
   stays in memory for up to 6 h.

## 9. Changes made in this round

| Change | File | Measured |
|---|---|---|
| Streamlit file watcher off on the server (`STREAMLIT_SERVER_FILE_WATCHER_TYPE=none`) — STG ran the developer default because `.streamlit/` is gitignored | `startup.sh` | 10 users: CPU per page 1.85 → 1.48 s (−20 %), median wait 11.9 → 9.2 s (−22 %), steps +13 % |
| Screening grid: column filter no longer vanishes when its popup closes; restored after leaving and returning; Key Devs Excel follows it | `app/pages/screening.py` | 8 rows kept after close (was back to 558); Excel 4 rows = grid |
| One-company transcript search uses the ticker index instead of FULLTEXT | `app/data/repository.py` | "inventory" 110 s → 1.9 s, "tariff" 19 s → 1.4 s |
| Three persisted caches rebuild when data changes, not on a timer / row count | `app/data/repository.py` | checks 0.26–1.0 s |

All earlier changes of this audit: `2026-09-30-mdp-e2e-performance-audit-design.md` §2
(21 root causes).

## 10. Every speed fix so far

| When | Fix | Before → after |
|---|---|---|
| Jul | Screening Show Results: removed `sleep(0.6)` + 3 chained reruns | 23–128 s → ~2 s |
| Jul | Key Devs criterion: window query dragged headline TEXT through a temp table | 37.8 s → 2.0 s |
| Jul | Segment rows: 21 round trips → 3 | AMZN 14.2 → 4.3 s, COST 7.4 → 4.7 s |
| Jul | Newsroom chunk loading | 758 chunks / 92.9 s → 1 wave |
| Jul | Newsroom left list rendered every article | 10.0 s → 0.11 s |
| Jul | Transcript PDF rebuilt on every rerun | 14.6 s → 0.29 s |
| Jul | Calendar per-company events | 27.8–36 s → 1.2 s; click → popup 1.1 s → 0.3 s |
| Jul | Screening navigation overlay stuck 15 s | 15 s on every visit → 0 |
| Jul | Persisted caches on `/home` (`materialize.py`) | universe read 0.07 s |
| Sep 30 | Filings PDF infinite loop froze the server | never finished → 1.3–2.0 s |
| Sep 30 | Forecast backfill re-wrote 400 rows per render | 3.6–4.6 s per render → 0 |
| Sep 30 | Fiscal-year-end map, Earnings Calls cold | 28.5 s → 3.1 s |
| Sep 30 | Newsroom rendered all ~7,000 articles | 5.1 s warm → 0.8 s |
| Sep 30 | Newsroom topic list + title search index | 10 s / 7–10 s (0 rows) → persisted / 0.9 s |
| Sep 30 | Filings dropdown waited on an Azure listing | 17–47 s → 3.2 s |
| Sep 30 | Segment rows persisted per ticker | 8.5–20.5 s → 2.3 s cold |
| Sep 30 | Cache freshness query on every read | −5.7 s per 20 s of warm reruns |
| Sep 30 | Watchlist / role lookups per rerun | −1.5 s per warm pass |
| Sep 30 | Forecasting sent all 11 charts | 2.0–2.5 s → 0.8–1.3 s |
| Sep 30 | Every page ran twice on a new session | 2 runs → 1 |
| Sep 30 | Streamlit telemetry from every browser | on → off |
| Oct 1 | §9 above | |

## 11. Recommended next (not done — need your go-ahead)

1. **Second instance or second process** — the only change that lifts the one-core ceiling.
2. **Move 10-K parsing off the web process** — ratings/geo sweep as a scheduled job; and
   `_build_geo_segment_members` is rebuilt for all ~450 tickers every sweep even when
   fresh (5–14 s each).
3. **Header links switch pages in the app** (`st.switch_page`/`st.page_link`) instead of
   a full reload — saves ~1 s and a new server session per click.
4. **Logos as cacheable files** (`app/static` + `STREAMLIT_SERVER_ENABLE_STATIC_SERVING=true`
   in `startup.sh`, or the existing CDN) — −105 KB on every page.
5. **Checksum signals** for the 5 count-only caches (§8).
6. **Retailers page** — cache the table and reuse the app's connection pool (24 s → <1 s).
7. Filings: send the filing HTML as a file the browser fetches instead of one 1 MB message.

## 12. Notes on the test itself

- The Mac rebooted at 08:55 mid-run; the harness was rebuilt from the session transcript
  and the whole programme re-run from 09:22 to 14:11 under `caffeinate`.
- The committed code wrote to STG during the first attempt (its forecast backfill ignores
  the read-only setting): 9 batches of 400 rows. A read-only check confirmed every value
  written was identical to what was stored (0 changes). The re-run's test copy had the
  backfill disabled; 0 writes.
- The committed code's filings PDF froze the test server (the PyMuPDF page-break loop
  fixed on Sep 30); that step was skipped for "before".
- Load-test waits include this laptop running up to 30 browsers plus the server; the
  server-side numbers (CPU per step, CPU %) are the basis for the STG estimate.

---

## 13. Review: data safety, sessions, caching, memory (2026-10-01, second pass)

### 13.1 Is anything changed besides speed?
Re-checked on the final code, committed vs now, both on fresh caches against the same STG
data: **41/41 screens identical**, Forecasting identical (annual + quarterly, M and WMT),
transcript search returns the same results ("tariff" 2, "inventory" 13). 0 database writes.
Data-safety checks on each change that caches or rewrites a query:

| Change | Could it show wrong or stale data? | Verdict |
|---|---|---|
| Segment rows persisted per ticker | Freshness check watches 10-K rows; the cached queries read only 10-K rows | safe |
| Fiscal-year-end map | 75/75 identical; a failed build raises, never stores a partial map | safe |
| One-company transcript search (LIKE) | `%`/`_` in a keyword only widen the database candidates; the page keeps exact substring matches | safe; can find more than before, never fewer |
| Filings blob scan in background | The scan only covers tickers already in the dropdown | safe |
| Watchlists cached 2 min | Every watchlist write clears the cache | safe |
| Transcripts cached 1 h (was 60 s) | A newly ingested transcript appears up to 1 h later on a running server | freshness only |
| Roles memo 60 s | A role removed elsewhere stays effective up to 60 s | freshness only |
| Cache freshness check reused 60 s | A data change is noticed up to 60 s later | freshness only |
| Grid filter, Newsroom paging, lazy Forecasting tabs, PDF page breaks | Display/interaction only; values unchanged | safe |

### 13.2 Are users' sessions separate?
- `st.session_state`, widgets, filters, criteria: **per browser session** (verified: user B never
  sees user A's period, units or Screening criteria).
- Page files run in a fresh module every run (Streamlit 1.55 `exec(code, ModuleType("__main__"))`),
  so page-level variables cannot leak.
- Preference cookie (`sip_app_state`) and login cookie: per browser.
- **Leak found and fixed:** `utils/local_storage.py` kept one process-wide dict that `get()` read
  before the session, so the last ticker any user picked became every new visitor's default
  (reproduced: user-b and an anonymous visitor opened on user-a's ticker; same on the committed code).
  Fixed by removing the dict; test `tests/test_local_storage_no_cross_session.py` fails on the old code.
- Per-user caches are keyed by email (saved criteria, watchlists, portal users) — no cross-user data.

### 13.3 Security findings (all in the committed code; not changed — need a decision)
1. `require_auth()` is commented out on Home, Market Data, Screening, Newsroom, Earnings Calls and
   Forecasting; Calendar has none; `main.py` never redirects a signed-out visitor. A visitor with no
   cookie gets those pages with data (verified on a local STG-like server).
2. The `auth_session` cookie is unsigned JSON `{session_id, user_email}` and is trusted as-is (only a
   logout blacklist is checked) — any email, including an admin's, can be claimed by setting it.
3. `/logs` checks sign-in only when `ENFORCE_PAGE_AUTH=1`, and its admin check lets an empty user
   through; its third tab runs any shell command on the server. Check that App Setting on STG today.
Fix direction: sign the cookie (HMAC with a server secret) or look the session up server-side; call
`require_auth()` centrally in `main.py` for every page except login; deny `/logs` when no user.

### 13.4 Caching — shared or per user, and memory
- `st.cache_data` (148 functions) and `st.cache_resource` (10) are **shared by all users** in the one
  server process: one copy per distinct argument (e.g. per ticker). 10 users on the same company share
  one copy — that is what makes a single instance work. Only 3 caches are per user (keyed by email).
- 139 of them are bounded only by time (1–6 h), not by count. **Measured: +22 MB of RAM per new company
  opened** (4 Market Data tabs), 364 MB → 3.2 GB after ~35 companies on this Mac. At that rate ~200
  distinct companies within the 6 h window would exceed STG's 4.8 GB. The number of users matters less
  than the number of different companies/filters they open.
- Already in place (same as the modular repo): `MALLOC_ARENA_MAX=2`, background `malloc_trim` after
  page runs and in the heartbeat, categorical encoding of large cached frames, persisted `/home` caches.
- Modular repo (`Code-Base/Modular-Code`) differs: 20 of its 68 `st.cache_data` have `max_entries`
  (mostly 1–2: one whole dataset shared, filtered per session in memory) and its `.streamlit/config.toml`
  is committed so production gets its settings.
- Best fit for MDP (single instance): keep shared `st.cache_data`, add `max_entries` (~50–100) to the
  per-ticker caches so RAM is capped; evicted tickers reload from the `/home` disk cache in ~0.1 s or
  from the DB; move 10-K parsing off the web process. A shared external cache (Redis) is only needed
  when running more than one instance.

### 13.5 Rebuilds — every change rebuilds the whole cache, once
| Run | Built from DB | Rebuilt in background | Disk hits |
|---|--:|--:|--:|
| Deploy, no cache | 14 (each once, on first use) | 0 | ~250 |
| Redeploy with cache, data unchanged | 0 | 0 | ~260 |
| Redeploy, rows added | 0 | 14 changed caches, each once | 54 |
| Redeploy, rows edited | 0 | 9 changed caches, each once (5 count-only caches did not notice) | 59 |
Nothing rebuilds repeatedly. A rebuild re-runs that cache's whole query (0.6–7.8 s, in the background;
users get the previous copy meanwhile). One changed row or ten makes no difference. Per-ticker caches
(segment rows) already rebuild only the ticker that changed. Row-level incremental updates (append new
ids) would save at most ~8 s of background work per change but risk silently missing edits/deletes;
not recommended unless a source table has a reliable `updated_at`.

## Appendix C — first load per scenario, every page and tab (ms)

| Page · action | Deploy, no cache | Redeploy with cache, data unchanged | Redeploy with cache, rows added | Redeploy with cache, rows edited | After background rebuild | Warm (repeat visit) |
|---|--:|--:|--:|--:|--:|--:|
| home · load | 3.8 s | 3.2 s | 9.8 s | 9.1 s | 1.4 s | 1.1 s |
| home · select company | 310 ms | 271 ms | 156 ms | 152 ms | 608 ms | 387 ms |
| screening · load | 2.0 s | 1.5 s | 1.3 s | 1.3 s | 2.5 s | 1.3 s |
| screening · open Industry form | 585 ms | 320 ms | 269 ms | 279 ms | 1.4 s | 542 ms |
| screening · Add industry criterion | 578 ms | 417 ms | 278 ms | 467 ms | 1.4 s | 594 ms |
| screening · Show Results (industry) | 1.7 s | 963 ms | 894 ms | 856 ms | 1.9 s | 1.1 s |
| screening · Excel companies (industry) | 43 ms | 43 ms | 43 ms | 50 ms | 46 ms | 34 ms |
| screening · Remove criterion | 582 ms | 367 ms | 266 ms | 289 ms | 1.1 s | 488 ms |
| screening · open Geography form | 774 ms | 602 ms | 558 ms | 754 ms | 1.1 s | 530 ms |
| screening · Add geography criterion | 887 ms | 350 ms | 652 ms | 443 ms | 1.3 s | 537 ms |
| screening · Show Results (geography) | 1.0 s | 691 ms | 689 ms | 858 ms | 1.8 s | 986 ms |
| screening · open Financial form | 790 ms | 530 ms | 658 ms | 605 ms | 1.1 s | 458 ms |
| screening · Add financial criterion (default metric) | 808 ms | 650 ms | 706 ms | 892 ms | 1.7 s | 736 ms |
| screening · Show Results (financial) | 992 ms | 812 ms | 5.1 s | 5.4 s | 1.9 s | 851 ms |
| screening · Statement → Geographical Segments | 516 ms | 2.3 s | 3.3 s | 3.8 s | 950 ms | 424 ms |
| screening · geo region pick 1st | 282 ms | 412 ms | 445 ms | 447 ms | 630 ms | 386 ms |
| screening · geo sub_region pick 1st | 706 ms | 446 ms | 469 ms | 412 ms | 645 ms | 280 ms |
| screening · geo country pick 1st | 2.8 s | 480 ms | 362 ms | 399 ms | 648 ms | 298 ms |
| screening · geo state pick 1st | 906 ms | 324 ms | 596 ms | 362 ms | 613 ms | 262 ms |
| screening · Screen For → Key Devs | 792 ms | 466 ms | 652 ms | 674 ms | 1.0 s | 459 ms |
| screening · open Key Devs form | 1.0 s | 603 ms | 894 ms | 837 ms | 1.3 s | 541 ms |
| screening · Add key devs criterion | 1.4 s | 856 ms | 1.0 s | 975 ms | 1.7 s | 772 ms |
| screening · Show Results (key devs) | 2.9 s | 2.7 s | 3.3 s | 2.8 s | 1.5 s | 988 ms |
| screening · Excel Key Devs (full export) | 3.4 s | 3.2 s | 2.9 s | 2.6 s | 2.6 s | 2.6 s |
| market_data · URL load tab=company_profile (M) | 3.2 s | 3.0 s | 2.8 s | 2.9 s | 2.5 s | 1.3 s |
| market_data · URL load tab=key_stats (M) | 3.1 s | 2.7 s | 3.6 s | 2.9 s | 1.4 s | 902 ms |
| market_data · URL load tab=income_statement (M) | 1.6 s | 1.3 s | 1.4 s | 1.5 s | 1.7 s | 990 ms |
| market_data · URL load tab=balance_sheet (M) | 2.0 s | 2.2 s | 1.6 s | 1.8 s | 1.5 s | 963 ms |
| market_data · URL load tab=cash_flow (M) | 1.6 s | 1.3 s | 1.8 s | 1.6 s | 1.8 s | 949 ms |
| market_data · URL load tab=ratios (M) | 1.7 s | 1.1 s | 2.0 s | 1.9 s | 1.5 s | 940 ms |
| market_data · URL load tab=estimates (M) | 2.1 s | 2.7 s | 1.8 s | 2.0 s | 1.7 s | 944 ms |
| market_data · URL load tab=forecasting (M) | 2.3 s | 2.4 s | 2.5 s | 2.4 s | 1.5 s | 960 ms |
| market_data · URL load tab=segment_data (M) | 7.8 s | 2.6 s | 2.8 s | 2.7 s | 1.8 s | 996 ms |
| market_data · URL load tab=ratings (M) | 1.3 s | 974 ms | 1.4 s | 1.2 s | 1.6 s | 917 ms |
| market_data · click tab Key Stats (AMZN) | 3.5 s | 2.3 s | 3.2 s | 3.3 s | 848 ms | 284 ms |
| market_data · click tab Income Statement (AMZN) | 1.5 s | 593 ms | 947 ms | 836 ms | 612 ms | 356 ms |
| market_data · click tab Balance Sheet (AMZN) | 1.0 s | 1.1 s | 1.0 s | 1.1 s | 632 ms | 362 ms |
| market_data · click tab Cash Flow (AMZN) | 1.2 s | 967 ms | 1.1 s | 1.8 s | 646 ms | 353 ms |
| market_data · click tab Ratios (AMZN) | 974 ms | 619 ms | 790 ms | 1.3 s | 654 ms | 334 ms |
| market_data · click tab Estimates (AMZN) | 1.3 s | 1.2 s | 1.3 s | 1.6 s | 628 ms | 344 ms |
| market_data · click tab Forecasting (AMZN) | 2.6 s | 2.0 s | 2.0 s | 1.8 s | 634 ms | 336 ms |
| market_data · click tab Segment (AMZN) | 1.3 s | 1.1 s | 1.3 s | 1.7 s | 682 ms | 372 ms |
| market_data · click tab Additional Data (AMZN) | 950 ms | 671 ms | 711 ms | 1.5 s | 658 ms | 333 ms |
| market_data · period Annual→Quarterly (IS) | 1.1 s | 990 ms | 1.3 s | 1.0 s | 608 ms | 394 ms |
| market_data · ticker switch M→WMT (IS) | 1.7 s | 1.6 s | 2.2 s | 1.7 s | 946 ms | 553 ms |
| market_data · Excel income_statement (M) | 1.1 s | 739 ms | 1.0 s | 625 ms | 940 ms | 318 ms |
| market_data · Excel balance_sheet (M) | 392 ms | 264 ms | 396 ms | 395 ms | 608 ms | 332 ms |
| market_data · Excel segment_data (M) | 609 ms | 548 ms | 880 ms | 524 ms | 626 ms | 336 ms |
| market_data · Excel ratings (M) | 510 ms | 375 ms | 458 ms | 498 ms | 476 ms | 282 ms |
| calendar · load | 7.4 s | 2.0 s | 3.7 s | 3.0 s | 2.5 s | 1.5 s |
| calendar · next month | 859 ms | 580 ms | 491 ms | 956 ms | 642 ms | 380 ms |
| calendar · prev month | 613 ms | 798 ms | 517 ms | 588 ms | 656 ms | 397 ms |
| calendar · view Yearly | 502 ms | 367 ms | 522 ms | 913 ms | 1.3 s | 380 ms |
| calendar · view List | 502 ms | 393 ms | 552 ms | 499 ms | 655 ms | 373 ms |
| calendar · open Filters panel | 1.4 s | 337 ms | 681 ms | 1.2 s | 666 ms | 372 ms |
| calendar · company filter M | 556 ms | 388 ms | 600 ms | 653 ms | 890 ms | 382 ms |
| calendar · URL ?ticker=WMT | 2.2 s | 1.5 s | 1.9 s | 1.9 s | 1.9 s | 1.5 s |
| earnings_calls · load | 3.8 s | 3.1 s | 3.5 s | 3.3 s | 2.0 s | 1.1 s |
| earnings_calls · URL ticker=M | 3.9 s | 2.7 s | 3.1 s | 3.5 s | 1.7 s | 1.1 s |
| earnings_calls · company change →WMT | 2.4 s | 2.2 s | 2.2 s | 3.0 s | 738 ms | 526 ms |
| earnings_calls · TF-IDF search 'tariff' | 568 ms | 325 ms | 414 ms | 646 ms | 514 ms | 282 ms |
| newsroom · load | 3.7 s | 1.6 s | 2.0 s | 1.7 s | 1.8 s | 1.2 s |
| newsroom · URL ticker=M | 2.2 s | 1.2 s | 1.5 s | 2.7 s | 1.7 s | 1.3 s |
| newsroom · sector filter (1st option) | 1.0 s | 664 ms | 986 ms | 770 ms | 722 ms | 496 ms |
| newsroom · search 'tariff' | 3.0 s | 1.8 s | 1.3 s | 1.8 s | 598 ms | 452 ms |
| company_filings · load | 7.7 s | 7.0 s | 4.6 s | 4.0 s | 2.0 s | 1.4 s |
| company_filings · URL M 10-K | 4.6 s | 4.0 s | 3.2 s | 3.7 s | 1.8 s | 1.4 s |
| company_filings · URL M 10-Q-Q1 | 3.4 s | 3.1 s | 2.2 s | 2.0 s | 1.7 s | 1.4 s |
| forecasting · load | 3.7 s | 4.0 s | 3.8 s | 3.5 s | 2.5 s | 2.0 s |
| forecasting · URL ticker=M annual | 3.4 s | 2.7 s | 3.0 s | 3.3 s | 2.2 s | 2.0 s |
| forecasting · Annual→Quarterly (M) | 2.4 s | 2.4 s | 2.3 s | 2.3 s | 1.4 s | 1.3 s |
| forecasting · ticker change →WMT | 2.1 s | 1.9 s | 2.1 s | 2.1 s | 1.3 s | 1.2 s |
| logs · load | 3.0 s | 2.6 s | 3.1 s | 3.6 s | 3.1 s | 1.4 s |
| logs · Download Logs | 54 ms | 54 ms | 47 ms | 47 ms | 60 ms | 74 ms |

## Appendix A — every action, every deploy (ms)

| Page · action | Before · no cache cold | Before · no cache warm | Before · cache cold | Before · cache warm | Now · no cache #1 cold | Now · no cache #1 warm | Now · cache #1 cold | Now · cache #1 warm | Now · no cache #2 cold | Now · no cache #2 warm | Now · cache #2 cold | Now · cache #2 warm | Now warm n/avg/max | Loader (now) | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| home · load | 765 | 1,710 | 1,354 | 880 | 900 | 1,524 | 736 | 928 | 6,620 | 1,686 | 1,624 | 1,252 | 12/1,418/2,266 | 0 | PASS |
| home · select company | 232 | 585 | 363 | 368 | 190 | 1,028 | 265 | 386 | 429 | 567 | 387 | 388 | 12/645/1,368 | 0 | PASS |
| screening · load | 1,663 | 1,514 | 1,518 | 1,228 | 1,669 | 1,564 | 1,905 | 1,244 | 2,354 | 1,991 | 1,405 | 1,451 | 12/1,688/2,744 | 0 | PASS |
| screening · open Industry form | 658 | 1,163 | 704 | 648 | 300 | 1,023 | 378 | 564 | 870 | 1,107 | 329 | 531 | 12/837/1,241 | 0 | PASS |
| screening · Add industry criterion | 665 | 988 | 325 | 591 | 489 | 1,031 | 310 | 546 | 668 | 1,082 | 670 | 642 | 12/892/1,422 | 0 | PASS |
| screening · Show Results (industry) | 4,995 | 2,642 | 1,103 | 1,210 | 1,421 | 2,240 | 1,063 | 1,178 | 1,959 | 2,528 | 900 | 1,057 | 12/1,736/2,692 | 0 | PASS |
| screening · Excel companies (industry) | 38 | 54 | 64 | 42 | 47 | 35 | 47 | 40 | 39 | 42 | 41 | 31 | 12/36/45 | 0 | PASS |
| screening · Remove criterion | 1,151 | 1,125 | 311 | 495 | 271 | 956 | 531 | 488 | 894 | 1,181 | 303 | 487 | 4/778/1,181 | 0 | PASS |
| screening · open Geography form | 2,014 | 1,107 | 650 | 490 | 615 | 1,248 | 641 | 532 | 934 | 1,139 | 615 | 527 | 12/889/1,756 | 0 | PASS |
| screening · Add geography criterion | 808 | 1,142 | 330 | 503 | 449 | 1,003 | 326 | 520 | 1,325 | 1,729 | 307 | 554 | 12/971/1,784 | 0 | PASS |
| screening · Show Results (geography) | 1,317 | 1,835 | 927 | 1,178 | 612 | 1,912 | 740 | 1,034 | 1,443 | 1,921 | 678 | 968 | 12/1,488/2,476 | 0 | PASS |
| screening · open Financial form | 1,158 | 1,096 | 446 | 436 | 486 | 1,128 | 367 | 451 | 1,095 | 1,140 | 458 | 467 | 12/808/1,185 | 0 | PASS |
| screening · Add financial criterion (default metric) | 1,057 | 1,439 | 669 | 726 | 603 | 1,415 | 645 | 744 | 1,012 | 1,711 | 687 | 729 | 12/1,216/2,010 | 0 | PASS |
| screening · Show Results (financial) | 1,585 | 1,933 | 674 | 980 | 648 | 1,880 | 701 | 821 | 1,337 | 1,874 | 748 | 872 | 12/1,447/1,950 | 0 | PASS |
| screening · Statement → Geographical Segments | 552 | 729 | 781 | 311 | 358 | 605 | 349 | 236 | 674 | 651 | 386 | 598 | 12/655/1,373 | 0 | PASS |
| screening · geo region pick 1st | 359 | 641 | 254 | 332 | 195 | 628 | 218 | 510 | 370 | 642 | 554 | 302 | 12/532/858 | 0 | PASS |
| screening · geo sub_region pick 1st | 554 | 621 | 366 | 311 | 778 | 628 | 737 | 268 | 634 | 644 | 196 | 287 | 12/537/1,342 | 0 | PASS |
| screening · geo country pick 1st | 515 | 644 | 213 | 329 | 5,206 | 636 | 200 | 252 | 474 | 631 | 229 | 358 | 12/556/1,111 | 0 | PASS |
| screening · geo state pick 1st | 361 | 614 | 222 | 316 | 303 | 846 | 201 | 256 | 1,510 | 618 | 235 | 345 | 12/504/861 | 0 | PASS |
| screening · geo city pick 1st | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | 0/—/— | 0 | FAIL×16: Locator.wait_for: Timeout 15000ms exceeded.
Call log:
  - wa |
| screening · Screen For → Key Devs | 956 | 1,089 | 309 | 498 | 647 | 1,095 | 350 | 394 | 936 | 1,051 | 397 | 524 | 12/808/1,593 | 0 | PASS |
| screening · open Key Devs form | 796 | 1,112 | 359 | 537 | 885 | 1,084 | 362 | 489 | 1,148 | 1,100 | 588 | 595 | 12/852/1,321 | 0 | PASS |
| screening · Add key devs criterion | 1,007 | 1,458 | 798 | 862 | 1,326 | 1,438 | 755 | 740 | 1,545 | 1,492 | 813 | 774 | 12/1,152/1,848 | 0 | PASS |
| screening · Show Results (key devs) | 3,930 | 1,327 | 2,515 | 1,014 | 2,907 | 1,533 | 2,694 | 916 | 2,812 | 1,297 | 2,600 | 991 | 12/1,255/1,935 | 1,278 | PASS |
| screening · Excel Key Devs (full export) | 2,508 | 2,943 | 3,420 | 4,062 | 3,878 | 2,600 | 2,655 | 2,408 | 2,902 | 2,872 | 4,244 | 2,786 | 4/2,666/2,872 | 0 | PASS |
| market_data · URL load tab=company_profile (M) | 3,331 | 1,879 | 2,577 | 1,376 | 3,248 | 1,875 | 2,570 | 1,220 | 3,179 | 1,906 | 3,206 | 1,311 | 12/1,801/2,961 | 0 | PASS |
| market_data · URL load tab=key_stats (M) | 3,954 | 1,552 | 2,802 | 918 | 3,177 | 1,502 | 2,817 | 830 | 3,035 | 1,538 | 2,477 | 965 | 12/1,280/1,984 | 0 | PASS |
| market_data · URL load tab=income_statement (M) | 1,374 | 1,598 | 960 | 955 | 1,352 | 1,524 | 903 | 942 | 1,760 | 1,550 | 892 | 1,004 | 12/1,267/1,663 | 0 | PASS |
| market_data · URL load tab=balance_sheet (M) | 1,637 | 1,669 | 1,359 | 943 | 1,851 | 1,508 | 1,582 | 958 | 2,085 | 1,522 | 1,679 | 966 | 12/1,303/1,523 | 0 | PASS |
| market_data · URL load tab=cash_flow (M) | 1,662 | 1,580 | 1,244 | 922 | 1,622 | 1,514 | 1,196 | 934 | 1,606 | 1,659 | 1,169 | 970 | 12/1,343/2,009 | 0 | PASS |
| market_data · URL load tab=ratios (M) | 1,667 | 1,663 | 1,518 | 946 | 1,537 | 1,510 | 921 | 921 | 1,836 | 1,505 | 1,073 | 953 | 12/1,304/2,018 | 0 | PASS |
| market_data · URL load tab=estimates (M) | 2,289 | 1,544 | 1,412 | 926 | 1,931 | 1,515 | 1,668 | 938 | 2,318 | 1,540 | 1,425 | 1,080 | 12/1,327/1,968 | 0 | PASS |
| market_data · URL load tab=forecasting (M) | 2,349 | 1,567 | 2,156 | 938 | 2,132 | 1,527 | 1,812 | 960 | 2,380 | 1,536 | 2,524 | 961 | 12/1,351/2,018 | 0 | PASS |
| market_data · URL load tab=segment_data (M) | 9,472 | 1,563 | 3,006 | 1,090 | 8,130 | 1,590 | 2,953 | 986 | 7,433 | 1,577 | 2,180 | 1,004 | 12/1,353/1,841 | 0 | PASS |
| market_data · URL load tab=ratings (M) | 1,128 | 1,486 | 741 | 852 | 1,111 | 1,558 | 698 | 915 | 1,493 | 1,474 | 751 | 919 | 12/1,268/1,873 | 0 | PASS |
| market_data · click tab Key Stats (AMZN) | 3,238 | 555 | 2,141 | 276 | 3,441 | 524 | 2,127 | 283 | 3,489 | 523 | 2,060 | 284 | 12/443/930 | 0 | PASS |
| market_data · click tab Income Statement (AMZN) | 790 | 641 | 493 | 350 | 1,211 | 604 | 490 | 327 | 1,760 | 642 | 507 | 367 | 12/483/653 | 0 | PASS |
| market_data · click tab Balance Sheet (AMZN) | 1,484 | 614 | 758 | 365 | 1,031 | 644 | 777 | 371 | 1,056 | 700 | 756 | 355 | 12/571/1,191 | 0 | PASS |
| market_data · click tab Cash Flow (AMZN) | 1,349 | 605 | 773 | 340 | 1,422 | 669 | 1,045 | 342 | 1,033 | 637 | 740 | 364 | 12/553/1,233 | 0 | PASS |
| market_data · click tab Ratios (AMZN) | 1,576 | 656 | 574 | 308 | 960 | 633 | 498 | 335 | 989 | 644 | 492 | 332 | 12/518/826 | 0 | PASS |
| market_data · click tab Estimates (AMZN) | 1,201 | 636 | 992 | 335 | 1,225 | 608 | 1,022 | 381 | 1,377 | 610 | 986 | 331 | 12/555/1,285 | 0 | PASS |
| market_data · click tab Forecasting (AMZN) | 2,539 | 652 | 1,722 | 328 | 3,135 | 614 | 1,740 | 322 | 2,044 | 612 | 1,982 | 347 | 12/512/791 | 0 | PASS |
| market_data · click tab Segment (AMZN) | 2,080 | 691 | 851 | 363 | 1,501 | 654 | 1,098 | 393 | 1,066 | 636 | 1,138 | 361 | 12/528/705 | 0 | PASS |
| market_data · click tab Additional Data (AMZN) | 681 | 588 | 502 | 305 | 683 | 602 | 558 | 305 | 1,216 | 613 | 473 | 354 | 12/524/1,092 | 0 | PASS |
| market_data · period Annual→Quarterly (IS) | 1,027 | 684 | 841 | 407 | 1,017 | 654 | 838 | 387 | 1,157 | 705 | 1,032 | 414 | 12/539/708 | 0 | PASS |
| market_data · ticker switch M→WMT (IS) | 1,766 | 834 | 1,504 | 540 | 1,807 | 841 | 1,507 | 541 | 1,684 | 842 | 1,560 | 565 | 12/694/886 | 0 | PASS |
| market_data · Excel income_statement (M) | 1,688 | 562 | 191 | 362 | 747 | 612 | 184 | 329 | 1,544 | 630 | 229 | 318 | 16/510/1,246 | 0 | PASS |
| market_data · Excel balance_sheet (M) | 431 | 627 | 252 | 338 | 396 | 612 | 199 | 368 | 389 | 804 | 199 | 344 | 16/545/1,128 | 0 | PASS |
| market_data · Excel segment_data (M) | 470 | 634 | 229 | 328 | 492 | 607 | 284 | 341 | 726 | 588 | 168 | 340 | 16/475/711 | 0 | PASS |
| market_data · Excel ratings (M) | 825 | 507 | 204 | 288 | 441 | 505 | 209 | 312 | 579 | 538 | 201 | 267 | 16/410/594 | 0 | PASS |
| calendar · load | 17,124 | 2,060 | 2,072 | 1,479 | 5,324 | 2,109 | 1,558 | 1,534 | 9,433 | 2,179 | 2,059 | 1,486 | 12/1,802/2,402 | 1,596 | PASS |
| calendar · next month | 489 | 688 | 256 | 365 | 575 | 665 | 691 | 411 | 1,143 | 658 | 270 | 379 | 12/514/679 | 0 | PASS |
| calendar · prev month | 685 | 694 | 255 | 368 | 736 | 661 | 299 | 409 | 490 | 666 | 252 | 385 | 12/550/736 | 0 | PASS |
| calendar · view Yearly | 458 | 1,245 | 261 | 346 | 539 | 633 | 321 | 410 | 465 | 661 | 260 | 356 | 12/518/694 | 0 | PASS |
| calendar · view List | 711 | 660 | 296 | 396 | 459 | 658 | 296 | 386 | 545 | 666 | 285 | 365 | 12/568/1,280 | 0 | PASS |
| calendar · open Filters panel | 451 | 641 | 597 | 353 | 1,059 | 631 | 271 | 374 | 1,691 | 677 | 262 | 371 | 12/563/1,038 | 0 | PASS |
| calendar · company filter M | 545 | 682 | 429 | 401 | 596 | 697 | 398 | 397 | 517 | 689 | 300 | 360 | 12/587/758 | 0 | PASS |
| calendar · URL ?ticker=WMT | 2,204 | 1,706 | 1,291 | 1,469 | 2,424 | 2,178 | 1,267 | 1,362 | 1,910 | 1,774 | 1,292 | 1,497 | 12/1,782/2,888 | 1,563 | PASS |
| earnings_calls · load | 3,448 | 1,771 | 2,842 | 1,106 | 3,114 | 1,768 | 3,070 | 1,151 | 4,488 | 2,040 | 2,843 | 1,143 | 12/1,673/2,349 | 0 | PASS |
| earnings_calls · URL ticker=M | 3,239 | 1,741 | 2,506 | 1,194 | 3,604 | 1,763 | 2,389 | 1,155 | 4,222 | 2,227 | 2,573 | 1,141 | 12/1,684/3,043 | 0 | PASS |
| earnings_calls · company change →WMT | 2,238 | 728 | 2,086 | 503 | 2,271 | 776 | 2,118 | 527 | 2,468 | 1,003 | 2,034 | 523 | 12/716/1,014 | 0 | PASS |
| earnings_calls · TF-IDF search 'tariff' | 601 | 491 | 213 | 261 | 451 | 473 | 203 | 277 | 685 | 794 | 355 | 287 | 12/519/1,604 | 0 | PASS |
| newsroom · load | 9,548 | 1,798 | 1,318 | 1,228 | 3,720 | 1,773 | 1,134 | 1,256 | 3,752 | 4,135 | 1,322 | 1,206 | 12/2,220/5,043 | 0 | PASS |
| newsroom · URL ticker=M | 2,076 | 1,885 | 1,491 | 1,265 | 2,199 | 1,844 | 993 | 1,286 | 2,188 | 4,189 | 993 | 1,265 | 12/2,107/4,731 | 0 | PASS |
| newsroom · sector filter (1st option) | 1,060 | 810 | 524 | 497 | 950 | 750 | 537 | 514 | 1,148 | 1,089 | 599 | 465 | 12/759/1,290 | 0 | PASS |
| newsroom · search 'tariff' | 1,823 | 731 | 1,288 | 444 | 4,179 | 706 | 2,577 | 488 | 1,901 | 1,204 | 1,281 | 435 | 12/743/1,921 | 0 | PASS |
| company_filings · load | 15,862 | 1,994 | 6,462 | 1,323 | 7,915 | 1,933 | 7,180 | 1,393 | 7,501 | 2,901 | 6,964 | 1,400 | 12/2,102/4,370 | 0 | PASS |
| company_filings · URL M 10-K | 4,654 | 1,962 | 4,001 | 1,329 | 4,319 | 1,962 | 3,897 | 1,495 | 4,794 | 2,732 | 4,063 | 1,359 | 12/1,931/3,242 | 0 | PASS |
| company_filings · URL M 10-Q-Q1 | 3,305 | 1,970 | 2,466 | 1,337 | 3,426 | 1,951 | 2,525 | 1,399 | 3,337 | 3,858 | 2,420 | 1,368 | 12/2,277/4,805 | 0 | PASS |
| forecasting · load | 3,720 | 2,682 | 3,347 | 2,199 | 3,467 | 2,581 | 3,625 | 1,991 | 3,887 | 2,860 | 3,657 | 1,991 | 12/2,504/4,577 | 0 | PASS |
| forecasting · URL ticker=M annual | 2,930 | 2,621 | 2,426 | 2,004 | 3,563 | 2,594 | 2,448 | 2,012 | 3,242 | 3,151 | 2,481 | 2,113 | 12/2,567/5,006 | 0 | PASS |
| forecasting · Annual→Quarterly (M) | 2,924 | 1,537 | 2,055 | 1,210 | 2,399 | 1,520 | 2,324 | 1,225 | 2,359 | 2,479 | 2,548 | 1,400 | 12/1,615/2,685 | 0 | PASS |
| forecasting · ticker change →WMT | 1,921 | 1,435 | 1,956 | 1,013 | 2,244 | 1,423 | 1,807 | 1,086 | 1,913 | 1,389 | 1,764 | 1,626 | 11/1,384/1,880 | 0 | FAIL×1: TimeoutError: Locator.click: Timeout 30000ms exceeded.
Call  |
| logs · load | 3,399 | 1,832 | 2,234 | 1,840 | 3,048 | 1,851 | 2,116 | 1,198 | 2,990 | 3,116 | 2,904 | 1,445 | 12/2,054/3,241 | 0 | PASS |
| logs · Download Logs | 51 | 75 | 42 | 78 | 60 | 73 | 66 | 70 | 47 | 82 | 47 | 78 | 12/77/105 | 0 | PASS |
| earnings_calls · year change 2026→2025 | — | 1,336 | — | 1,325 | — | 1,261 | — | 1,120 | — | 1,460 | — | 1,557 | 8/1,350/2,267 | 0 | PASS |
| earnings_calls · quarter change →Q1 | — | 1,155 | — | 1,298 | — | 1,417 | — | 1,113 | — | 2,100 | — | 1,548 | 8/1,544/3,337 | 0 | PASS |
| earnings_calls · transcript PDF download | — | 48 | — | 70 | — | 58 | — | 48 | — | 75 | — | 58 | 8/60/102 | 0 | PASS |
| earnings_calls · search in transcript 'sales' | — | 494 | — | 290 | — | 484 | — | 272 | — | 1,030 | — | 415 | 8/550/1,378 | 0 | PASS |
| earnings_calls · transcript search CSV download | — | 38 | — | 37 | — | 39 | — | 47 | — | 34 | — | 48 | 4/42/48 | 0 | PASS |
| earnings_calls · clear search | — | 1,063 | — | 633 | — | 1,126 | — | 537 | — | 620 | — | 609 | 4/723/1,126 | 0 | PASS |
| earnings_calls · cross-transcript search 'tariff' (Year=All) | — | 1,377 | — | 966 | — | 1,230 | — | 1,180 | — | 1,356 | — | 935 | 8/1,176/2,055 | 0 | PASS |
| earnings_calls · cross-company CSV prepare+download | — | 17,416 | — | 2,367 | — | 3,873 | — | 27,974 | — | 3,369 | — | 2,367 | 4/9,396/27,974 | 0 | PASS |
| market_data · Excel company_profile (M) | — | 648 | — | 280 | — | 546 | — | 283 | — | 988 | — | 298 | 4/529/988 | 0 | PASS |
| market_data · Excel key_stats (M) | — | 649 | — | 895 | — | 593 | — | 342 | — | 648 | — | 332 | 4/479/648 | 0 | PASS |
| market_data · Excel cash_flow (M) | — | 631 | — | 349 | — | 625 | — | 365 | — | 588 | — | 344 | 4/480/625 | 0 | PASS |
| market_data · Excel ratios (M) | — | 659 | — | 332 | — | 650 | — | 332 | — | 630 | — | 919 | 4/633/919 | 0 | PASS |
| market_data · Excel estimates (M) | — | 0 | — | 0 | — | 0 | — | 0 | — | 0 | — | 0 | 4/0/0 | 0 | PASS |
| market_data · Excel forecasting (M) | — | 0 | — | 0 | — | 0 | — | 0 | — | 0 | — | 0 | 4/0/0 | 0 | PASS |
| market_data · units change (IS) | — | 620 | — | 389 | — | 640 | — | 342 | — | 622 | — | 694 | 8/574/996 | 0 | PASS |
| market_data · sort order change (IS) | — | 618 | — | 320 | — | 624 | — | 347 | — | 657 | — | 341 | 8/492/703 | 0 | PASS |
| market_data · Quarterly Excel (IS) | — | 634 | — | 319 | — | 606 | — | 324 | — | 607 | — | 352 | 4/472/607 | 0 | PASS |
| forecasting · main tab Models | — | 792 | — | 486 | — | 825 | — | 506 | — | 1,141 | — | 566 | 4/760/1,141 | 0 | PASS |
| forecasting · main tab Test | — | 1,341 | — | 939 | — | 1,324 | — | 959 | — | 1,322 | — | 957 | 4/1,140/1,324 | 0 | PASS |
| forecasting · main tab Overview | — | 847 | — | 1,211 | — | 828 | — | 591 | — | 840 | — | 603 | 4/716/840 | 0 | PASS |
| forecasting · model tab CAGR | — | 740 | — | 365 | — | 666 | — | 379 | — | 653 | — | 382 | 4/520/666 | 0 | PASS |
| forecasting · model tab Ensemble | — | 736 | — | 382 | — | 720 | — | 428 | — | 696 | — | 724 | 4/642/724 | 0 | PASS |
| forecasting · model tab Scenarios | — | 718 | — | 391 | — | 717 | — | 425 | — | 648 | — | 433 | 4/556/717 | 0 | PASS |
| forecasting · Annual→Quarterly radio | — | 1,798 | — | 958 | — | 1,200 | — | 1,244 | — | 1,379 | — | 1,182 | 8/1,251/1,502 | 0 | PASS |
| forecasting · Excel download (M) | — | 58 | — | 55 | — | 49 | — | 47 | — | 55 | — | 46 | 4/49/55 | 0 | PASS |
| company_filings · doc type change →10-Q-Q1 | — | 624 | — | 340 | — | 788 | — | 366 | — | 624 | — | 400 | 8/544/958 | 0 | PASS |
| company_filings · year change (10-K →2024) | — | 1,440 | — | 1,130 | — | 1,502 | — | 1,255 | — | 1,439 | — | 1,450 | 8/1,412/2,394 | 0 | PASS |
| company_filings · search 'Revenue' | — | 1,434 | — | 1,051 | — | 1,587 | — | 2,529 | — | 1,297 | — | 1,072 | 8/1,621/4,128 | 0 | PASS |
| company_filings · company change →WMT | — | 4,312 | — | 1,928 | — | 4,706 | — | 4,981 | — | 2,074 | — | 1,650 | 8/3,352/9,385 | 0 | PASS |
| newsroom · sort change | — | 692 | — | 494 | — | 642 | — | 484 | — | 769 | — | 730 | 8/656/871 | 0 | PASS |
| newsroom · category change | — | 701 | — | 404 | — | 688 | — | 398 | — | 697 | — | 404 | 8/547/704 | 0 | PASS |
| newsroom · show more articles | — | 1,422 | — | 844 | — | 1,379 | — | 809 | — | 1,548 | — | 832 | 8/1,142/1,575 | 0 | PASS |
| newsroom · search 'tariff' then clear | — | 818 | — | 466 | — | 708 | — | 749 | — | 771 | — | 444 | 8/668/1,080 | 0 | PASS |
| home · type-to-search 'Macy' (company_select) | — | 22 | — | 21 | — | 24 | — | 21 | — | 28 | — | 25 | 8/24/33 | 0 | PASS |
| market_data · type-to-search 'WMT' (company_selector_header) | — | 33 | — | 42 | — | 43 | — | 29 | — | 49 | — | 42 | 8/41/51 | 0 | PASS |
| earnings_calls · type-to-search 'Target' (ec_company_select) | — | 28 | — | 28 | — | 28 | — | 28 | — | 41 | — | 29 | 8/32/47 | 0 | PASS |
| company_filings · type-to-search 'Costco' (cf_company_select) | — | 50 | — | 52 | — | 51 | — | 50 | — | 52 | — | 53 | 8/52/56 | 0 | PASS |
| forecasting · type-to-search 'Home Depot' (estimates_ticker) | — | 40 | — | 39 | — | 42 | — | 42 | — | 46 | — | 48 | 8/44/49 | 0 | PASS |
| newsroom · type-to-search 'Con' (news_sector_select) | — | 16 | — | 19 | — | 14 | — | 18 | — | 18 | — | 18 | 8/17/19 | 0 | PASS |
| calendar · company filter type-to-search 'Mac' | — | 21 | — | 21 | — | 25 | — | 24 | — | 25 | — | 28 | 8/25/31 | 0 | PASS |
| market_data · start date change (3rd) | — | 812 | — | 610 | — | 915 | — | 620 | — | 1,810 | — | 734 | 8/1,020/2,743 | 0 | PASS |
| market_data · end date change (2nd) | — | 1,052 | — | 758 | — | 1,364 | — | 746 | — | 1,560 | — | 750 | 8/1,105/1,647 | 0 | PASS |
| market_data · conversion mode change (2nd) | — | 866 | — | 546 | — | 822 | — | 522 | — | 1,226 | — | 554 | 8/781/1,609 | 0 | PASS |
| market_data · currency to → EUR | — | 1,176 | — | 742 | — | 1,031 | — | 756 | — | 1,168 | — | 717 | 8/918/1,261 | 0 | PASS |
| calendar · remove chip type_ipo | — | 948 | — | 546 | — | 828 | — | 502 | — | 1,132 | — | 517 | 8/745/1,153 | 0 | PASS |
| calendar · remove chip type_ma | — | 796 | — | 578 | — | 932 | — | 846 | — | 1,126 | — | 546 | 8/862/1,213 | 0 | PASS |
| calendar · remove chip q_Q1 | — | 763 | — | 562 | — | 701 | — | 548 | — | 752 | — | 539 | 8/635/791 | 0 | PASS |
| calendar · Email Alerts open (no save) | — | 748 | — | 530 | — | 697 | — | 575 | — | 776 | — | 635 | 8/671/846 | 0 | PASS |
| calendar · Email Alerts company search 'Mac' | — | 22 | — | 22 | — | 23 | — | 24 | — | 28 | — | 24 | 8/25/30 | 0 | PASS |
| newsroom · watchlist filter | — | 0 | — | 0 | — | 0 | — | 0 | — | 0 | — | 0 | 4/0/0 | 0 | PASS |
| screening · watchlist bar select | — | 807 | — | 310 | — | 550 | — | 349 | — | 533 | — | 390 | 4/456/550 | 0 | PASS |
| screening · industry type-to-search 'Ret' | — | 28 | — | 20 | — | 16 | — | 19 | — | 22 | — | 17 | 8/18/24 | 0 | PASS |
| screening · geography type-to-search 'Uni' | — | 18 | — | 16 | — | 14 | — | 20 | — | 17 | — | 15 | 8/16/25 | 0 | PASS |
| screening · key devs category type-to-search 'M' | — | 12 | — | 10 | — | 12 | — | 10 | — | 8 | — | 10 | 8/10/13 | 0 | PASS |
| screening · financial · statement → Balance Sheet | — | 616 | — | 362 | — | 810 | — | 377 | — | 631 | — | 355 | 4/543/810 | 0 | PASS |
| screening · financial · metric type-to-search 'Net' | — | 21 | — | 15 | — | 19 | — | 15 | — | 19 | — | 19 | 4/18/19 | 0 | PASS |
| screening · financial · metric change (2nd) | — | 647 | — | 420 | — | 654 | — | 405 | — | 651 | — | 376 | 4/522/654 | 0 | PASS |
| screening · financial · period type change (2nd) | — | 652 | — | 390 | — | 649 | — | 912 | — | 690 | — | 372 | 4/656/912 | 0 | PASS |
| screening · financial · year mode → Year range | — | 676 | — | 396 | — | 689 | — | 399 | — | 672 | — | 378 | 4/534/689 | 0 | PASS |
| screening · financial · operator change (2nd) | — | 694 | — | 350 | — | 594 | — | 848 | — | 676 | — | 341 | 4/615/848 | 0 | PASS |
| screening · financial · value typed | — | 5 | — | 4 | — | 5 | — | 7 | — | 7 | — | 4 | 4/6/7 | 0 | PASS |
| screening · financial · Credit Ratings | — | 266 | — | 260 | — | 258 | — | 253 | — | 267 | — | 270 | 4/262/270 | 0 | PASS |
| screening · financial · Store Counts | — | 251 | — | 257 | — | 255 | — | 266 | — | 262 | — | 253 | 4/259/266 | 0 | PASS |
| screening · key devs · timeframe change (3rd) | — | 55 | — | 48 | — | 53 | — | 56 | — | 58 | — | 51 | 4/54/58 | 0 | PASS |
| screening · key devs · period → Date range | — | 689 | — | 406 | — | 651 | — | 397 | — | 815 | — | 363 | 4/556/815 | 0 | PASS |
| screening · Screen For → Equities | — | 1,150 | — | 555 | — | 1,168 | — | 588 | — | 1,151 | — | 602 | 4/877/1,168 | 0 | PASS |
| screening · Screen For → Fixed Income | — | 1,103 | — | 560 | — | 1,143 | — | 578 | — | 1,130 | — | 578 | 4/857/1,143 | 0 | PASS |
| screening · Screen For → People | — | 1,223 | — | 555 | — | 1,108 | — | 574 | — | 1,224 | — | 557 | 4/866/1,224 | 0 | PASS |
| screening · Screen For → Transactions | — | 1,139 | — | 563 | — | 1,131 | — | 563 | — | 1,211 | — | 588 | 4/873/1,211 | 0 | PASS |
| screening · Screen For → Projects/Portfolios | — | 1,003 | — | 580 | — | 1,157 | — | 563 | — | 1,053 | — | 562 | 4/834/1,157 | 0 | PASS |
| screening · Saved Screenings open | — | 1,641 | — | 1,139 | — | 1,572 | — | 1,225 | — | 1,732 | — | 1,212 | 4/1,435/1,732 | 0 | PASS |
| screening · grid filter open · Company Name | — | 50 | — | 54 | — | 60 | — | 53 | — | 63 | — | 56 | 16/57/71 | 0 | PASS |
| screening · grid filter search '1-8' · Company Name | — | 5 | — | 5 | — | 5 | — | 5 | — | 6 | — | 5 | 16/5/6 | 0 | PASS |
| screening · grid filter apply · Company Name | — | 68 | — | 66 | — | 66 | — | 70 | — | 70 | — | 70 | 16/69/76 | 0 | PASS |
| screening · grid filter close (1 rerun) · Company Name | — | 686 | — | 338 | — | 630 | — | 326 | — | 852 | — | 328 | 16/533/1,005 | 0 | PASS |
| screening · grid filter open · Industry | — | 36 | — | 47 | — | 51 | — | 43 | — | 44 | — | 41 | 12/44/58 | 0 | PASS |
| screening · grid filter search 'Acc' · Industry | — | 4 | — | 4 | — | 4 | — | 4 | — | 4 | — | 4 | 8/4/4 | 0 | PASS |
| screening · grid filter apply · Industry | — | 99 | — | 98 | — | 100 | — | 102 | — | 99 | — | 97 | 12/92/116 | 0 | PASS |
| screening · grid filter close (1 rerun) · Industry | — | 756 | — | 397 | — | 639 | — | 336 | — | 716 | — | 313 | 12/717/1,627 | 0 | PASS |
| screening · Excel after column filter | — | 37 | — | 39 | — | 43 | — | 41 | — | 40 | — | 51 | 20/813/4,975 | 0 | PASS |
| screening · grid filter open · Total Revenue [FY Latest] | — | 55 | — | 56 | — | 60 | — | 58 | — | 52 | — | 65 | 8/59/66 | 0 | PASS |
| screening · grid filter search '1,0' · Total Revenue [FY Latest] | — | 6 | — | 5 | — | 5 | — | 4 | — | 6 | — | 6 | 8/5/6 | 0 | PASS |
| screening · grid filter apply · Total Revenue [FY Latest] | — | 71 | — | 74 | — | 68 | — | 74 | — | 69 | — | 68 | 8/70/75 | 0 | PASS |
| screening · grid filter close (1 rerun) · Total Revenue [FY Latest] | — | 633 | — | 334 | — | 667 | — | 314 | — | 792 | — | 346 | 8/530/932 | 0 | PASS |
| screening · grid filter open · Company Name(s) | — | 51 | — | 46 | — | 49 | — | 54 | — | 52 | — | 42 | 4/49/54 | 0 | PASS |
| screening · grid filter search 'Aca' · Company Name(s) | — | 5 | — | 6 | — | 5 | — | 3 | — | 4 | — | 4 | 4/4/5 | 0 | PASS |
| screening · grid filter apply · Company Name(s) | — | 69 | — | 71 | — | 69 | — | 75 | — | 73 | — | 78 | 4/74/78 | 0 | PASS |
| screening · grid filter close (1 rerun) · Company Name(s) | — | 1,608 | — | 864 | — | 1,620 | — | 815 | — | 1,924 | — | 888 | 4/1,312/1,924 | 0 | PASS |
| screening · grid filter open · Key Developments By Date | — | 47 | — | 48 | — | 48 | — | 51 | — | 41 | — | 46 | 4/46/51 | 0 | PASS |
| screening · grid filter search 'Sep' · Key Developments By Date | — | 3 | — | 3 | — | 3 | — | 4 | — | 4 | — | 3 | 4/4/4 | 0 | PASS |
| screening · grid filter apply · Key Developments By Date | — | 80 | — | 80 | — | 109 | — | 77 | — | 77 | — | 77 | 4/85/109 | 0 | PASS |
| screening · grid filter close (1 rerun) · Key Developments By Date | — | 1,499 | — | 868 | — | 1,497 | — | 1,158 | — | 1,376 | — | 855 | 4/1,222/1,497 | 0 | PASS |
| screening · grid filter open · Key Developments by Type | — | 43 | — | 41 | — | 45 | — | 50 | — | 50 | — | 50 | 4/49/50 | 0 | PASS |
| screening · grid filter search '6-K' · Key Developments by Type | — | 5 | — | 3 | — | 6 | — | 5 | — | 5 | — | 4 | 4/5/6 | 0 | PASS |
| screening · grid filter apply · Key Developments by Type | — | 73 | — | 65 | — | 69 | — | 72 | — | 71 | — | 64 | 4/69/72 | 0 | PASS |
| screening · grid filter close (1 rerun) · Key Developments by Type | — | 1,477 | — | 942 | — | 1,470 | — | 906 | — | 1,563 | — | 814 | 4/1,188/1,563 | 0 | PASS |
| screening · grid filter search 'a' · Industry | — | 4 | — | 4 | — | 5 | — | 4 | — | 7 | — | 4 | 4/5/7 | 0 | PASS |
| retailer_adding · load | — | 24,404 | — | 26,630 | — | 24,769 | — | 24,172 | — | 24,972 | — | 23,912 | 8/24,456/26,388 | 0 | PASS |
| market_size_forecasting · load | — | 1,737 | — | 1,232 | — | 1,873 | — | 1,228 | — | 3,520 | — | 1,208 | 8/1,957/4,186 | 0 | PASS |
| live_earnings_transcript · load | — | 1,710 | — | 1,160 | — | 1,787 | — | 1,143 | — | 3,390 | — | 1,207 | 8/1,882/3,581 | 0 | PASS |
| access_management · load | — | 1,786 | — | 1,192 | — | 1,704 | — | 1,249 | — | 2,612 | — | 1,275 | 8/1,710/2,714 | 0 | PASS |
| company_filings_add_files · load | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | 0/—/— | 0 | FAIL×8: RuntimeError: stException: ModuleNotFoundError: No module na |
| retailer_adding · search 'Macy' | — | 22,353 | — | 22,497 | — | 21,250 | — | 21,163 | — | 22,408 | — | 19,231 | 4/21,013/22,408 | 0 | PASS |
| navigation · header → Market Data Dashboard (from Home) | — | 1,372 | — | 1,388 | — | 1,813 | — | 1,347 | — | 1,588 | — | 1,454 | 8/1,550/1,817 | 2,859 | PASS |
| navigation · header → Earnings Calls (from Home) | — | 1,566 | — | 1,266 | — | 1,592 | — | 1,268 | — | 1,624 | — | 1,278 | 8/1,440/1,679 | 0 | PASS |
| navigation · header → Calendar (from Home) | — | 1,811 | — | 1,554 | — | 1,755 | — | 1,498 | — | 2,026 | — | 1,550 | 8/1,707/2,042 | 2,916 | PASS |
| navigation · header → Screening (from Home) | — | 2,050 | — | 1,848 | — | 2,064 | — | 1,913 | — | 2,049 | — | 1,842 | 8/1,967/2,340 | 2,949 | PASS |
| navigation · header → News (from Home) | — | 2,076 | — | 1,593 | — | 2,018 | — | 1,616 | — | 2,090 | — | 1,688 | 8/1,853/2,147 | 0 | PASS |
| navigation · page switch → Earnings Calls | — | 1,631 | — | 1,272 | — | 1,538 | — | 1,278 | — | 1,736 | — | 1,322 | 8/1,469/1,787 | 0 | PASS |
| navigation · page switch → Calendar | — | 1,958 | — | 1,548 | — | 2,188 | — | 1,598 | — | 2,408 | — | 1,546 | 8/1,935/2,712 | 4,626 | PASS |
| navigation · page switch → Screening | — | 1,796 | — | 1,482 | — | 1,756 | — | 1,484 | — | 1,897 | — | 1,599 | 8/1,684/1,904 | 0 | PASS |
| navigation · page switch → News | — | 2,005 | — | 1,589 | — | 1,994 | — | 1,638 | — | 2,180 | — | 1,699 | 8/1,878/2,317 | 0 | PASS |
| navigation · page switch → Market Data Dashboard | — | 1,950 | — | 1,440 | — | 1,910 | — | 1,490 | — | 2,004 | — | 1,427 | 8/1,708/2,204 | 10,894 | PASS |
| navigation · browser Back | — | 6 | — | 4 | — | 6 | — | 5 | — | 6 | — | 4 | 8/5/8 | 0 | PASS |
| navigation · browser Forward | — | 1,905 | — | 1,235 | — | 879 | — | 1,392 | — | 2,384 | — | 1,341 | 8/1,499/2,808 | 0 | PASS |
| footer · Market Size Forecasting link | — | 2,618 | — | 1,988 | — | 2,652 | — | 2,109 | — | 3,662 | — | 2,108 | 4/2,633/3,662 | 0 | PASS |
| footer · all external links reachable (header logo + footer) | — | 21,510 | — | 25,962 | — | 20,249 | — | 19,934 | — | 21,754 | — | 21,269 | 4/20,802/21,754 | 0 | PASS |
| market_data · tab switch IS→BS→IS (round trip) | — | 700 | — | 425 | — | 722 | — | 432 | — | 732 | — | 444 | 8/583/742 | 0 | PASS |
| market_data · tab switch Profile→Segment→Profile | — | 1,064 | — | 734 | — | 1,050 | — | 734 | — | 1,097 | — | 816 | 8/924/1,108 | 0 | PASS |
| market_data · stock chart hover (Profile) | — | 47 | — | 55 | — | 47 | — | 47 | — | 54 | — | 47 | 8/49/63 | 0 | PASS |
| forecasting · Overview chart hover | — | 66 | — | 65 | — | 74 | — | 65 | — | 66 | — | 66 | 8/68/82 | 0 | PASS |
| forecasting · Models → Linear Regression: render + hover | — | 63 | — | 63 | — | 63 | — | 63 | — | 63 | — | 63 | 4/63/63 | 0 | PASS |
| forecasting · Models → CAGR: render + hover | — | 63 | — | 64 | — | 63 | — | 46 | — | 46 | — | 64 | 4/55/64 | 0 | PASS |
| forecasting · Models → Exponential Smoothing: render + hover | — | 62 | — | 60 | — | 63 | — | 46 | — | 46 | — | 63 | 4/54/63 | 0 | PASS |
| forecasting · Models → Holt's Linear Trend: render + hover | — | 44 | — | 46 | — | 63 | — | 63 | — | 46 | — | 62 | 4/58/63 | 0 | PASS |
| forecasting · Models → Moving Average Trend: render + hover | — | 46 | — | 63 | — | 63 | — | 63 | — | 47 | — | 63 | 4/59/63 | 0 | PASS |
| forecasting · Models → Weighted Average Growth: render + hover | — | 63 | — | 47 | — | 63 | — | 46 | — | 63 | — | 62 | 4/58/63 | 0 | PASS |
| forecasting · Models → Ensemble: render + hover | — | 47 | — | 46 | — | 63 | — | 62 | — | 63 | — | 63 | 4/63/63 | 0 | PASS |
| forecasting · Models → Scenarios: render + hover | — | 1,329 | — | 1,347 | — | 1,364 | — | 1,344 | — | 1,347 | — | 1,313 | 4/1,342/1,364 | 0 | PASS |
| forecasting · Test tab: MAPE bar chart hover | — | 4,641 | — | 4,615 | — | 4,632 | — | 4,599 | — | 4,615 | — | 4,631 | 4/4,619/4,632 | 0 | PASS |
| forecasting · Test tab: backtest line chart hover | — | 49 | — | 48 | — | 48 | — | 48 | — | 48 | — | 50 | 4/48/50 | 0 | PASS |
| forecasting · tab switch Overview→Models→Overview | — | 991 | — | 726 | — | 859 | — | 728 | — | 972 | — | 699 | 8/814/982 | 0 | PASS |
| logs · tab → Segment Cache | — | 52 | — | 59 | — | 51 | — | 50 | — | 54 | — | 55 | 8/52/63 | 0 | PASS |
| logs · tab → Live Server Logs | — | 52 | — | 46 | — | 56 | — | 50 | — | 52 | — | 52 | 8/52/59 | 0 | PASS |
| company_filings · PDF download M 10-K (cap 180s) | — | — | — | — | 1,994 | — | 1,025 | — | 2,069 | — | 1,090 | — | 0/—/— | 0 | PASS |

| Page · action | Warm p50 | Worst cold | Server p50 | Largest server steps (ms) |
|---|--:|--:|--:|---|
| retailer_adding · load | 24,338 | — | 22,995 |  |
| retailer_adding · search 'Macy' | 21,206 | — | 45,280 |  |
| footer · all external links reachable (header logo + footer) | 20,759 | — | 20 |  |
| forecasting · Test tab: MAPE bar chart hover | 4,623 | — | 345 |  |
| earnings_calls · cross-company CSV prepare+download | 3,621 | — | 650 | EC_PAGE_TOTAL 757 |
| screening · Excel Key Devs (full export) | 2,693 | 4,244 | — |  |
| footer · Market Size Forecasting link | 2,380 | — | 785 |  |
| forecasting · URL ticker=M annual | 2,347 | 3,563 | 270 | ESTIMATES_PAGE_TOTAL 1,619, ESTIMATES_PAGE_LOAD_DASHBOARD 1,434, FORECAST_TOTAL 1,037 |
| forecasting · load | 2,340 | 3,887 | 280 | ESTIMATES_PAGE_TOTAL 1,784, ESTIMATES_PAGE_LOAD_DASHBOARD 1,420, FORECAST_TOTAL 986 |
| navigation · header → Screening (from Home) | 2,056 | — | 370 |  |
| company_filings · URL M 10-K | 1,956 | 4,794 | 210 | FILINGS_LOAD 1,923, FILINGS_PREFETCH 1,126, FL_header_metadata 1,084 |
| company_filings · URL M 10-Q-Q1 | 1,897 | 3,426 | 235 | FILINGS_LOAD 2,341, FL_header_metadata 766 |
| company_filings · load | 1,870 | 7,915 | 445 | FILINGS_LOAD 5,044, FL_header_metadata 1,255, DB_load_companies_from_db 601 |
| navigation · page switch → News | 1,838 | — | — |  |
| navigation · header → News (from Home) | 1,832 | — | 260 |  |
| navigation · page switch → Calendar | 1,828 | — | — |  |
| company_filings · company change →WMT | 1,812 | — | 1,045 | FILINGS_LOAD 1,982, FL_header_metadata 890 |
| logs · load | 1,802 | 3,048 | 565 | SCREENING_SEGMENT_VALUES_CACHE_STATUS 1,345 |
| market_data · URL load tab=company_profile (M) | 1,801 | 3,248 | 720 | TAB_Company_Profile 1,547, PAGE_SUMMARY 1,547 |
| earnings_calls · load | 1,768 | 4,488 | 650 | EC_PAGE_TOTAL 2,599, DB_EarningsCallRepository.get_years_and_quarters 533, DB_get_years_and_quarters 533 |
| earnings_calls · URL ticker=M | 1,755 | 4,222 | 655 | EC_PAGE_TOTAL 2,512, DB_EarningsCallRepository.get_years_and_quarters 677, DB_get_years_and_quarters 664 |
| newsroom · load | 1,692 | 3,752 | 280 | DB_get_yf_topic_keys 1,485 |
| calendar · load | 1,676 | 9,433 | 225 | EC_PAGE_RENDER_TOTAL 5,966, EC_PAGE_PARALLEL_TOTAL 5,919, EC_PAGE_FETCH_ALL_IPO 5,757 |
| navigation · header → Calendar (from Home) | 1,658 | — | 200 |  |
| navigation · page switch → Market Data Dashboard | 1,650 | — | — |  |
| navigation · page switch → Screening | 1,646 | — | — |  |
| screening · Show Results (industry) | 1,641 | 1,959 | — |  |
| calendar · URL ?ticker=WMT | 1,604 | 2,424 | 160 |  |
| newsroom · URL ticker=M | 1,598 | 2,199 | 145 |  |
| screening · Show Results (financial) | 1,588 | 1,337 | — |  |
| market_data · URL load tab=segment_data (M) | 1,554 | 8,130 | 200 | TAB_Segments 7,370, PAGE_SUMMARY 7,370 |
| access_management · load | 1,523 | — | 545 | ACCESS_MGMT_db_health 922 |
| screening · load | 1,514 | 2,354 | 700 | SCREENING_SEGMENT_OPTIONS_CACHE_HIT 408 |
| market_size_forecasting · load | 1,507 | — | 325 |  |
| live_earnings_transcript · load | 1,504 | — | 90 |  |
| screening · Show Results (geography) | 1,498 | 1,443 | — |  |
| navigation · header → Market Data Dashboard (from Home) | 1,496 | — | 60 |  |
| market_data · URL load tab=cash_flow (M) | 1,486 | 1,622 | 150 | TAB_Cash_Flow 781, PAGE_SUMMARY 781 |
| market_data · URL load tab=forecasting (M) | 1,472 | 2,524 | 150 | TAB_Forecasting 1,563, PAGE_SUMMARY 1,563 |
| forecasting · Annual→Quarterly (M) | 1,452 | 2,548 | 670 | ESTIMATES_PAGE_TOTAL 1,425, ESTIMATES_PAGE_LOAD_DASHBOARD 1,226, FORECAST_QUARTERLY_TOTAL 1,225 |
| company_filings · year change (10-K →2024) | 1,450 | — | 3,470 | FILINGS_LOAD 6,220, FL_header_metadata 1,382 |
| market_data · URL load tab=ratios (M) | 1,439 | 1,836 | 150 | TAB_Ratios 497, PAGE_SUMMARY 497 |
| market_data · URL load tab=balance_sheet (M) | 1,436 | 2,085 | 115 | TAB_Balance_Sheet 888, PAGE_SUMMARY 888 |
| navigation · page switch → Earnings Calls | 1,433 | — | — |  |
| navigation · header → Earnings Calls (from Home) | 1,432 | — | 400 | EC_PAGE_TOTAL 433 |
| market_data · URL load tab=estimates (M) | 1,426 | 2,318 | 115 | TAB_Estimates 1,193, PAGE_SUMMARY 1,193 |
| earnings_calls · quarter change →Q1 | 1,417 | — | 1,375 | EC_PAGE_TOTAL 2,037 |
| market_data · URL load tab=ratings (M) | 1,414 | 1,493 | 120 | MAIN_PAGE_REGISTRATION 325 |
| market_data · URL load tab=key_stats (M) | 1,410 | 3,177 | 75 | TAB_Key_Statistics 2,309, PAGE_SUMMARY 2,309 |
| home · load | 1,402 | 6,620 | 20 | DB_NewsRepository.get_articles 3,550, AV_ARTICLES_TOTAL 3,549, DB_SELECT_NOKEY 3,520 |
| screening · Saved Screenings open | 1,398 | — | 760 |  |
| navigation · browser Forward | 1,393 | — | — |  |
| forecasting · ticker change →WMT | 1,389 | 2,244 | 540 | ESTIMATES_PAGE_TOTAL 1,018, ESTIMATES_PAGE_LOAD_DASHBOARD 938, FORECAST_TOTAL 937 |
| company_filings · search 'Revenue' | 1,353 | — | 900 | FILINGS_LOAD 3,837 |
| forecasting · Models → Scenarios: render + hover | 1,346 | — | 475 |  |
| earnings_calls · year change 2026→2025 | 1,280 | — | 1,460 | EC_PAGE_TOTAL 1,552 |
| screening · grid filter close (1 rerun) · Key Developments By Date | 1,267 | — | — |  |
| forecasting · Annual→Quarterly radio | 1,258 | — | 560 | ESTIMATES_PAGE_TOTAL 360 |
| screening · grid filter close (1 rerun) · Company Name(s) | 1,254 | — | — |  |
| screening · Show Results (key devs) | 1,252 | 2,907 | — |  |
| market_data · URL load tab=income_statement (M) | 1,249 | 1,760 | 135 | TAB_Income_Statement 497, PAGE_SUMMARY 497 |
| screening · Add financial criterion (default metric) | 1,238 | 1,012 | — |  |
| screening · grid filter close (1 rerun) · Key Developments by Type | 1,188 | — | — |  |
| forecasting · main tab Test | 1,140 | — | 145 |  |
| earnings_calls · cross-transcript search 'tariff' (Year=All) | 1,124 | — | 1,405 | EC_PAGE_TOTAL 1,825, DB_EarningsCallRepository.search_transcripts_fulltext 1,814, DB_search_transcripts_fulltext.TOTAL 1,814 |
| newsroom · show more articles | 1,095 | — | 165 |  |
| screening · Add key devs criterion | 1,044 | 1,545 | — |  |
| screening · open Key Devs form | 964 | 1,148 | — |  |
| screening · Add geography criterion | 959 | 1,325 | — |  |
| screening · open Financial form | 884 | 1,095 | — |  |
| newsroom · sector filter (1st option) | 704 | 1,148 | 170 |  |
| earnings_calls · company change →WMT | 686 | 2,468 | 675 | EC_PAGE_TOTAL 1,439 |
| market_data · ticker switch M→WMT (IS) | 660 | 1,807 | 235 | TAB_Income_Statement 1,197, PAGE_SUMMARY 1,197 |
| screening · geo country pick 1st | 622 | 5,206 | — |  |
| market_data · click tab Estimates (AMZN) | 602 | 1,377 | 85 | TAB_Estimates 842, PAGE_SUMMARY 842 |
| market_data · click tab Segment (AMZN) | 580 | 1,501 | 155 | TAB_Segments 929, PAGE_SUMMARY 929 |
| newsroom · search 'tariff' | 580 | 4,179 | 255 | NEWSROOM_KEYWORD_SEARCH 3,431 |
| market_data · click tab Forecasting (AMZN) | 577 | 3,135 | 140 | TAB_Forecasting 1,489, PAGE_SUMMARY 1,489 |
| market_data · click tab Additional Data (AMZN) | 576 | 1,216 | 10 | TAB_Ratings 463, PAGE_SUMMARY 463 |
| screening · geo state pick 1st | 558 | 1,510 | — |  |
| calendar · open Filters panel | 544 | 1,691 | 265 | EC_PAGE_RENDER_TOTAL 389 |
| market_data · click tab Balance Sheet (AMZN) | 540 | 1,056 | 120 | TAB_Balance_Sheet 582, PAGE_SUMMARY 582 |
| market_data · period Annual→Quarterly (IS) | 534 | 1,157 | 295 | TAB_Income_Statement 601, PAGE_SUMMARY 601 |
| market_data · click tab Cash Flow (AMZN) | 527 | 1,422 | 135 | TAB_Cash_Flow 722, PAGE_SUMMARY 722 |
| market_data · click tab Income Statement (AMZN) | 484 | 1,760 | 95 | TAB_Income_Statement 384, PAGE_SUMMARY 384 |
| calendar · next month | 476 | 1,143 | 105 |  |
| market_data · Excel income_statement (M) | 462 | 1,544 | 85 |  |
| market_data · click tab Key Stats (AMZN) | 406 | 3,489 | 10 | TAB_Key_Statistics 2,241, PAGE_SUMMARY 2,241 |
| company_filings · PDF download M 10-K (cap 180s) | — | 2,069 | 440 |  |


## Appendix B — data changes, load, RAM by phase

### Data changes (simulated on a copy of the cache; STG data only read)

| Page · action | No change: 1st load after deploy | No change: after rebuild | Rows added (every source): 1st load after deploy | Rows added (every source): after rebuild | Rows edited in place: 1st load after deploy | Rows edited in place: after rebuild |
|---|--:|--:|--:|--:|--:|--:|
| home · load | 7,377 | 1,480 | 9,787 | 1,410 | 9,125 | 1,489 |
| home · select company | 161 | 571 | 156 | 570 | 152 | 645 |
| screening · load | 1,331 | 1,944 | 1,342 | 2,280 | 1,340 | 2,804 |
| screening · open Industry form | 252 | 1,050 | 269 | 1,630 | 279 | 1,121 |
| screening · Add industry criterion | 272 | 1,078 | 278 | 1,124 | 467 | 1,698 |
| screening · Show Results (industry) | 925 | 1,789 | 894 | 1,874 | 856 | 1,864 |
| screening · Excel companies (industry) | 41 | 50 | 43 | 45 | 50 | 48 |
| screening · Remove criterion | 268 | 1,014 | 266 | 1,061 | 289 | 1,121 |
| screening · open Geography form | 551 | 1,028 | 558 | 1,132 | 754 | 1,111 |
| screening · Add geography criterion | 417 | 1,065 | 652 | 1,672 | 443 | 976 |
| screening · Show Results (geography) | 656 | 1,831 | 689 | 1,822 | 858 | 1,863 |
| screening · open Financial form | 765 | 1,951 | 658 | 1,110 | 605 | 1,170 |
| screening · Add financial criterion (default metric) | 619 | 1,386 | 706 | 1,441 | 892 | 1,944 |
| screening · Show Results (financial) | 988 | 1,822 | 5,084 | 1,869 | 5,382 | 1,896 |
| screening · Statement → Geographical Segments | 6,192 | 813 | 3,326 | 908 | 3,827 | 992 |
| screening · geo region pick 1st | 463 | 571 | 445 | 623 | 447 | 637 |
| screening · geo sub_region pick 1st | 405 | 590 | 469 | 653 | 412 | 637 |
| screening · geo country pick 1st | 1,012 | 1,079 | 362 | 651 | 399 | 644 |
| screening · geo state pick 1st | 536 | 483 | 596 | 582 | 362 | 644 |
| screening · Screen For → Key Devs | 652 | 889 | 652 | 1,005 | 674 | 1,013 |
| screening · open Key Devs form | 858 | 1,011 | 894 | 1,095 | 837 | 1,412 |
| screening · Add key devs criterion | 999 | 1,264 | 1,042 | 1,885 | 975 | 1,463 |
| screening · Show Results (key devs) | 2,903 | 1,717 | 3,279 | 1,554 | 2,800 | 1,538 |
| screening · Excel Key Devs (full export) | 2,698 | 192 | 2,892 | 2,523 | 2,609 | 2,713 |
| market_data · URL load tab=company_profile (M) | 3,277 | 2,095 | 2,804 | 2,784 | 2,885 | 2,264 |
| market_data · URL load tab=key_stats (M) | 2,938 | 1,285 | 3,607 | 1,420 | 2,933 | 1,426 |
| market_data · URL load tab=income_statement (M) | 2,044 | 2,019 | 1,422 | 1,491 | 1,520 | 1,947 |
| market_data · URL load tab=balance_sheet (M) | 3,410 | 1,625 | 1,621 | 1,486 | 1,839 | 1,517 |
| market_data · URL load tab=cash_flow (M) | 1,641 | 1,328 | 1,849 | 1,993 | 1,628 | 1,533 |
| market_data · URL load tab=ratios (M) | 1,356 | 1,324 | 2,026 | 1,475 | 1,857 | 1,504 |
| market_data · URL load tab=estimates (M) | 4,888 | 1,365 | 1,829 | 1,524 | 2,026 | 1,806 |
| market_data · URL load tab=forecasting (M) | 2,718 | 2,233 | 2,509 | 1,492 | 2,425 | 1,489 |
| market_data · URL load tab=segment_data (M) | 2,655 | 1,305 | 2,788 | 2,064 | 2,742 | 1,529 |
| market_data · URL load tab=ratings (M) | 1,472 | 1,629 | 1,449 | 1,590 | 1,192 | 1,557 |
| market_data · click tab Key Stats (AMZN) | 2,608 | 1,078 | 3,210 | 1,170 | 3,254 | 527 |
| market_data · click tab Income Statement (AMZN) | 783 | 482 | 947 | 558 | 836 | 666 |
| market_data · click tab Balance Sheet (AMZN) | 1,845 | 824 | 1,024 | 622 | 1,131 | 642 |
| market_data · click tab Cash Flow (AMZN) | 1,115 | 479 | 1,088 | 641 | 1,765 | 650 |
| market_data · click tab Ratios (AMZN) | 866 | 511 | 790 | 657 | 1,342 | 652 |
| market_data · click tab Estimates (AMZN) | 1,464 | 534 | 1,252 | 677 | 1,567 | 580 |
| market_data · click tab Forecasting (AMZN) | 2,315 | 559 | 1,967 | 630 | 1,767 | 637 |
| market_data · click tab Segment (AMZN) | 1,174 | 1,257 | 1,266 | 684 | 1,655 | 680 |
| market_data · click tab Additional Data (AMZN) | 981 | 488 | 711 | 675 | 1,536 | 642 |
| market_data · period Annual→Quarterly (IS) | 1,099 | 584 | 1,252 | 658 | 1,036 | 557 |
| market_data · ticker switch M→WMT (IS) | 1,836 | 1,313 | 2,232 | 1,077 | 1,748 | 815 |
| market_data · Excel income_statement (M) | 1,805 | 572 | 1,015 | 628 | 625 | 1,251 |
| market_data · Excel balance_sheet (M) | 395 | 776 | 396 | 601 | 395 | 615 |
| market_data · Excel segment_data (M) | 1,192 | 1,126 | 880 | 627 | 524 | 626 |
| market_data · Excel ratings (M) | 714 | 409 | 458 | 479 | 498 | 473 |
| calendar · load | 2,338 | 1,821 | 3,669 | 2,198 | 3,046 | 2,744 |
| calendar · next month | 780 | 691 | 491 | 646 | 956 | 637 |
| calendar · prev month | 1,842 | 569 | 517 | 656 | 588 | 656 |
| calendar · view Yearly | 521 | 450 | 522 | 1,287 | 913 | 1,247 |
| calendar · view List | 597 | 507 | 552 | 651 | 499 | 659 |
| calendar · open Filters panel | 478 | 571 | 681 | 650 | 1,173 | 682 |
| calendar · company filter M | 467 | 617 | 600 | 1,061 | 653 | 718 |
| calendar · URL ?ticker=WMT | 1,885 | 2,199 | 1,912 | 1,689 | 1,899 | 2,063 |
| earnings_calls · load | 3,426 | 1,616 | 3,511 | 1,788 | 3,278 | 2,300 |
| earnings_calls · URL ticker=M | 2,997 | 1,715 | 3,053 | 1,741 | 3,533 | 1,756 |
| earnings_calls · company change →WMT | 2,537 | 680 | 2,239 | 734 | 3,030 | 741 |
| earnings_calls · TF-IDF search 'tariff' | 417 | 424 | 414 | 514 | 646 | 515 |
| newsroom · load | 2,241 | 1,705 | 1,985 | 1,787 | 1,720 | 1,828 |
| newsroom · URL ticker=M | 1,736 | 1,733 | 1,510 | 1,727 | 2,655 | 1,765 |
| newsroom · sector filter (1st option) | 857 | 1,056 | 986 | 636 | 770 | 808 |
| newsroom · search 'tariff' | 1,623 | 562 | 1,316 | 557 | 1,772 | 639 |
| company_filings · load | 6,937 | 1,913 | 4,633 | 1,966 | 4,006 | 1,995 |
| company_filings · URL M 10-K | 4,100 | 1,424 | 3,156 | 1,576 | 3,700 | 2,036 |
| company_filings · URL M 10-Q-Q1 | 4,364 | 1,890 | 2,181 | 1,666 | 1,980 | 1,675 |
| forecasting · load | 4,650 | 2,583 | 3,789 | 2,459 | 3,475 | 2,463 |
| forecasting · URL ticker=M annual | 3,044 | 2,097 | 3,024 | 2,955 | 3,293 | 1,535 |
| forecasting · Annual→Quarterly (M) | 2,327 | 1,159 | 2,288 | 1,373 | 2,272 | 1,372 |
| forecasting · ticker change →WMT | 2,099 | 1,212 | 2,059 | 1,309 | 2,082 | 1,309 |
| logs · load | 2,644 | 2,648 | 3,096 | 3,288 | 3,567 | 2,928 |
| logs · Download Logs | 49 | 61 | 47 | 64 | 47 | 56 |

**No change** — caches served stale then rebuilt in the background: 0; rebuild times: none; fresh hits: 13

**Rows added (every source)** — caches served stale then rebuilt in the background: 13; rebuild times: screening_universe 1.1s, non_sec_transcript_companies 0.6s, earnings_transcript_tickers 1.0s, fiscal_year_end_map 2.2s, ma_completion_events 1.7s, calendar_events_full 3.8s, screening_all_companies_universe 7.8s, keydev_subtype_taxonomy 6.9s, segment_rows_AMZN 4.6s, segment_rows_M 4.9s, delisted_events 1.0s, ipo_events 6.5s, yf_topic_keys 1.2s; fresh hits: 1

**Rows edited in place** — caches served stale then rebuilt in the background: 8; rebuild times: screening_universe 1.2s, fiscal_year_end_map 2.1s, ma_completion_events 1.7s, screening_all_companies_universe 4.9s, keydev_subtype_taxonomy 5.7s, segment_rows_AMZN 4.6s, segment_rows_M 4.6s, yf_topic_keys 1.1s; fresh hits: 6
  Edits NOT detected (count-only signal): calendar_events_full, delisted_events, earnings_transcript_tickers, ipo_events, non_sec_transcript_companies

### Concurrent users (warm server, 3 s think time, realistic journey)

| Users | Steps done | Errors | p50 ms | p95 ms | max ms | Server CPU % avg / max | RSS MB avg / max |
|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | 47 | 0 | 1,563 | 2,124 | 2,147 | 25 / 97 | 843 / 1,345 |
| 5 | 121 | 0 | 6,303 | 12,257 | 14,214 | 73 / 492 | 934 / 1,144 |
| 10 | 132 | 0 | 16,076 | 26,998 | 30,347 | 69 / 108 | 921 / 1,005 |
| 20 | 128 | 0 | 37,845 | 68,062 | 75,370 | 70 / 343 | 1,014 / 1,167 |
| 30 | 137 | 0 | 61,804 | 88,202 | 93,190 | 73 / 105 | 897 / 1,135 |

Per step at 10 users (p50 / p95 ms): home 13,931/19,930; market_data IS 15,378/18,036; market_data → Balance Sheet 0/0; screening 15,902/21,410; newsroom 15,740/21,582; calendar 19,058/30,347; earnings_calls 21,256/27,128; company_filings 22,691/29,023; forecasting 25,684/28,158

Per step at 30 users (p50 / p95 ms): home 49,838/82,631; market_data IS 81,626/89,825; market_data → Balance Sheet 0/0; screening 80,804/88,929; newsroom 50,806/63,408

### RAM by phase (server process RSS, MB)

| Phase | samples | min | median | max |
|---|--:|--:|--:|--:|
| boot | 2 | 116 | 174 | 231 |
| idle_boot | 85 | 233 | 474 | 529 |
| prime | 239 | 507 | 1,179 | 1,361 |
| load_1 | 238 | 725 | 808 | 1,345 |
| cool_1 | 42 | 809 | 823 | 946 |
| load_5 | 236 | 796 | 931 | 1,144 |
| cool_5 | 42 | 939 | 951 | 958 |
| load_10 | 243 | 818 | 922 | 1,005 |
| cool_10 | 43 | 856 | 887 | 894 |
| load_20 | 261 | 718 | 1,046 | 1,167 |
| cool_20 | 42 | 262 | 443 | 1,140 |
| load_30 | 259 | 255 | 861 | 1,135 |
| cool_30 | 43 | 1,103 | 1,129 | 1,170 |
| idle_sessions_30 | 223 | 106 | 733 | 1,170 |
| after_close | 170 | 604 | 672 | 956 |
