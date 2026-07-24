# Newsroom cold-load fix + full app spinner audit (2026-07-24)

Follow-up to `2026-07-24-screening-slow-landing-overlay-trap-design.md`.

## 1. Newsroom — deep investigation

### Symptom
Newsroom nav shows the loading overlay far longer than other pages; hit the
overlay's 15s cap on a cold load.

### Findings (measured, local → STG DB over VPN)
Newsroom's own `PageLoadTracker` steps and DB timings on a cold process:

| Step / query | Cold cost |
|---|--:|
| `get_news_date_range` (DATE_RANGE_FETCH) | 2,901 ms |
| `_get_sectors_impl` | 402 ms |
| `get_articles` (AV, 1000 rows) | 3,376 ms |
| `get_yf_articles` (chunk 1, 804 rows) | 2,879 ms |
| `get_yf_articles` (chunk 2, 1095 rows) | 3,753 ms |
| `FILTER_WIDGETS` step (worst observed, STG) | 5,453–6,026 ms |

**Root cause: a warmup gap, not a bug.** Every one of those queries is already
`@st.cache_data`-decorated (30 min – 12 h TTL), so newsroom is fast once warm.
But `_background_warmup_thread()` (cache_manager) warmed only the newsroom
**dropdowns** — `get_sectors`, `get_ticker_sector_map`, `get_news_date_range`
(Track 1) — and never the **articles**, which are the page's actual cost.
Streamlit caches die with the process, and STG/prod deploy ~6×/day, so the first
newsroom visitor after every deploy paid the full cold fetch.

Two supporting facts:
- `warm_caches()` in cache_manager is **dead code** — defined, never called.
- The "double `get_yf_articles`" is **by design**: `_split_into_chunks()` splits the
  range into full ISO weeks (Mon–Sun) so cache keys stay stable; a 7-day default
  window spans 2 ISO weeks → 2 AV + 2 YF calls.

### Fix — Track 1d: warm the newsroom default window at boot
`app/utils/cache_manager.py`, added after Track 1c (market_data).

Newsroom's first load uses `date_from = today-7`, `date_to = today`, then
`_fetch_chunked(..., sector=None, company_ticker=None, sort_ascending=False,
av_limit=1000, yf_limit=2000)`. Track 1d recomputes the same ISO-week boundaries
and calls the same two repository methods with the same args, so it populates the
exact `@st.cache_data` entries the first visitor asks for.

`newsroom.py` cannot be imported from the warm thread (it calls `main()` at import),
so the week math is recomputed. It mirrors `_split_into_chunks()` exactly. If that
logic ever drifts, the warm simply misses — a wasted background query, never a
broken page.

### Verification
Ran the exact warm queries standalone against the STG DB:

```
default window: 2026-07-17 -> 2026-07-24
ISO week chunks (newest-first): [(2026-07-20, 2026-07-26), (2026-07-13, 2026-07-19)]
  week 2026-07-20 -> 2026-07-26:  get_articles 1000 rows in 9883ms | get_yf_articles  804 rows in 1448ms
  week 2026-07-13 -> 2026-07-19:  get_articles 1000 rows in 5696ms | get_yf_articles 1095 rows in 1442ms
TOTAL warm cost moved off first visitor: 18471ms
```

The chunk count (2) and YF row counts (**804** and **1095**) match byte-for-byte the
values newsroom itself fetched in the earlier page trace → **identical cache keys
confirmed**.

### End-to-end verification of the real warm thread
`_background_warmup_thread()` was then invoked directly (it is gated in main.py on
`_auth_ready_for_bg = session_state.authenticated and auth_data`, which the LOCAL
harness does not set — so it is exercised directly rather than by disabling auth).

```
invoking _background_warmup_thread() ...
WARMUP_THREAD_COMPLETED in 58.8s
[CACHE_WARM_BG] Newsroom articles warmed for 2 ISO week(s)      <- Track 1d ran, no error
CACHED-REPLAY week 2026-07-20: get_articles 4.2ms | get_yf_articles 2.1ms
CACHED-REPLAY week 2026-07-13: get_articles 3.8ms | get_yf_articles 8.5ms
```

Replaying the **exact** calls the first newsroom visitor makes now costs
**18.6 ms total, versus 18,471 ms cold — a ~1,000× reduction.** No warmup errors, no
Python exceptions. The fix is verified end-to-end.

## 2. Full cold + incognito audit (fresh browser context, process-cold server)

All numbers are **local over VPN to the STG DB**, so absolute values are heavily
inflated by ~150–300 ms per DB round-trip. Prod (app and DB in the same datacenter)
is far lower. **Relative ranking is the signal.**

### Header-nav pages — client-side nav overlay (`#cs-ov`)
| Page | Overlay visible | Notes |
|---|--:|---|
| screening | 6,312 ms cold / **1,014–1,821 ms warm** | cold = one-time process DDL; was **15,012 ms every visit** before the fix |
| market_data | 3,469 ms | hands off to its own page loader |
| calendar | 3,648 ms | own loader |
| newsroom | **15,030 ms** | hit the 15s cap — Track 1d addresses this |
| earnings_calls | 6,581 ms | own loader |

### market_data — every tab (page loader `.cs-al-ov`, cleared server-side)
| Tab | Loader visible |
|---|--:|
| Income Statement | 443 ms |
| Ratios | 473 ms |
| Balance Sheet | 874 ms |
| Cash Flow | 1,239 ms |
| Estimates | 1,251 ms |
| Forecasting | 1,564 ms |
| **Key Stats** | **5,111 ms** |
| **Segment** | **6,444 ms** |
| Company Profile | n/a (already active) |
| Additional Data | **no loader observed**, mainLen 669 — see open items |

These tab loaders use `render_page_loader()` — an `st.empty()` placeholder cleared
when the Python render finishes — so they track **real** server render time. No false
trap here. Key Stats and Segment are genuinely the two slowest tabs cold.

### Full-reload pages — boot overlay (`#cs-boot-overlay`)
| Path | Boot overlay gone |
|---|--:|
| /logs | 288 ms |
| /forecasting | 5,834 ms |
| /company_filings | 12,167 ms |

`boot_overlay.py` uses a **widget-presence** check (`stMarkdownContainer`,
`stSelectbox`, `stDataFrame`, …), not a character count, so it was never affected by
the screening bug.

## 3. How much harm the spinner actually did

The 15s trap was **specific to screening** — every other page either renders >800
chars or hands off to its own loader.

- Screening content was ready in **~0.3–0.5 s on prod**, but the overlay held for
  **15.0 s on 100% of screening navigations** → **~14.5 s wasted per visit**, a
  ~30× perceived slowdown.
- Prod log (24-Jul, 01:43→18:27, ~16.7 h): **78** screening page-load sequences
  across **2** users → roughly **19 minutes** of pure dead spinner in one day, on a
  page that was never actually slow.
- STG log (7 days): **285** screening sequences across **6** users.

Every screening complaint your clients raised was this overlay, not the data.

## 4. market_data "Additional Data" tab — investigated, healthy

The sweep anomaly (no loader, 669 chars) was **not a defect**:

- The tab labelled **"Additional Data"** has the internal key **`ratings`**
  (`components/toolbar.py`). It *is* dispatched (`elif selected_tab == "ratings"`).
- The sweep used **AMZN**, which has no credit-rating/store data (ratings are
  retailer-only), so the tab correctly renders *"No extracted data available. Check
  official filings."* The 669/153-char readings were mid-render samples.

**Polling is bounded — verified in the UI.** The tab renders inside
`@st.fragment(run_every=1.5 if _ratings_incomplete else None)`, the pattern that
previously caused a rerun storm. Measured over a 35-second idle:

| Ticker | market_data reruns during 35 s idle | Verdict |
|---|--:|---|
| AMZN (no data) | **0** | poll stopped |
| M (has data) | **1** | poll stopped (the settle rerun) |

A runaway poll would be ~23. The existing sticky-settle fix
(`_ratings_settled_<ticker>`, bounded `_n_retry < 4`, and "poll only while a CORE
fetch is pending") holds.

Rendering confirmed by screenshot: **M** shows Ratings & Store Data (store counts
722/718/680/665 with YoY, Excel export); **AMZN** shows the correct empty state.

### One real defect found and fixed — loader label mismatch
`_MD_TAB_LABELS` mapped `"ratings" → "Loading Ratings"` and carried a dead
`"additional_data"` key that never matched any query param. So clicking the tab
named **Additional Data** showed a spinner saying **"Loading Ratings"**.
Fixed in `app/pages/market_data.py`: `"ratings" → "Loading Additional Data"`, dead
key removed. Verified in the UI — the loader now reads *"Loading Additional Data"*.

## 5. RAM — measured, no leak

Concern: does this work exhaust RAM? Measured on the real process (single PID).

- **Newsroom article caches cost ~+56.5 MB.** This is what Track 1d holds. It is
  **RAM-neutral in steady state** for any server that gets newsroom traffic — the
  same cache entries are populated by the first visitor anyway; the warm only moves
  *when*. The marginal cost applies only to a server where nobody opens newsroom.
- **Leak test** (3 full cycles over screening / market_data / newsroom / calendar /
  earnings_calls / ratings): RSS **221.5 → 307.0 → 319.2 → 318.7 MB** — plateaus.
- **Three further full sweeps** (8 pages incl. earnings_calls): RSS oscillated
  **215 → 429 → 215 → 323 MB**, i.e. memory is actively *reclaimed* (malloc_trim
  working). Bounded band, no monotonic growth.
- Full 8-page smoke test: every page rendered, **zero** JS page errors.

Conclusion: bounded, self-correcting. STG normally sits at 340–430 MB on a 4,794 MB
box. These changes do not threaten RAM. The genuine RAM risk remains the separate
edgartools/BeautifulSoup filing-object retention on prod (1.6–2.0 GB).

## 6. Open items (not fixed)
- **Prod RAM 1.6–2.0 GB** from edgartools/BeautifulSoup filing-object retention
  (company_filings / EDGAR warmup, not screening or newsroom). Its own task.
- **Network floor**: India ↔ centralus ~233 ms RTT, http/1.1, no HTTP/2/CDN. Only
  infra fixes this.
- **Prod RAM ~1.6–2.0 GB** from edgartools/BeautifulSoup filing objects (see prior spec).
- **Network floor**: India ↔ centralus ~233 ms RTT; only HTTP/2 + CDN/region fixes it.
