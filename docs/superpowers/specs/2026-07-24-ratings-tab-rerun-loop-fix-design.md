# Ratings / "Additional Data" tab — rerun loop + no-auto-surface fix

**Date:** 2026-07-24
**Scope:** market_data.py Ratings tab (the tab is labeled **"Additional Data"** in the UI —
same tab holds credit ratings + store counts + geographic breakdown + square footage).
**No DB writes.**

## Symptom (reported)
- Changing company on the Ratings/Additional-Data tab → "loading loading, nothing" / infinite
  feel; excessive reruns; geographic/additional data only appears after a **manual refresh**.

## Root cause (from `server-logs-20260723_202815.log` + code)
The tab wraps `render_ratings_data` in `@st.fragment(run_every=1.5)` (market_data.py:4412) that
polls while `_ratings_incomplete_<ticker>` is True. That flag was driven by
`get_ratings_data` putting **`square_footage`** into `_pending_fetches` whenever the EDGAR
square-footage fetch missed a shared 4s window (repository.py:13061-13075) — and sqft is the
slowest EDGAR extraction, read last, so it landed in `_pending_fetches` on **nearly every
ticker** (log: `pending=square_footage` for M, NKE, TGT, KR, COST, LULU, ANF, ADBE…).

The killer was the give-up path (market_data.py:1019-1035 + 4417): after the retry budget
exhausted it **popped** the retry counter, so the very next rerun saw count=0 and **re-armed**
the 1.5s poll → it never stuck. Every incidental rerun re-armed polling for the perpetually-
pending sqft → the rerun storm. For a company with no store data (ADBE/Adobe) the data never
arrived → poll ran the budget and showed empty ("loading…nothing"). Late store/geographic data
only surfaced when the user forced a fresh rerun (manual refresh).

## Fix (root, market_data.py `render_ratings_data`)
1. **Sticky per-ticker settle flag** `_ratings_settled_<ticker>`: once a ticker's render has
   completed OR exhausted its bounded poll budget, set it — later incidental reruns NEVER
   re-arm the poll. Kills the storm.
2. **square_footage is retry-worthy only when the company has store data**
   (`store_counts` or `stores_by_country`). A ratings-only company (ADBE) no longer spins for a
   sqft figure that never comes → shows its data / "No extracted data" **instantly**.
3. **Poll the empty-state only while a CORE fetch is warming** (db_rows / credit_ratings /
   store_totals / stores_by_country) — sqft alone never triggers a spinner.
4. Auto-surface preserved: while a store-shaping fetch warms, the partial table renders and the
   fragment re-polls (bounded) to self-heal to the full payload — **no manual refresh**.

## Verification (incognito, staging DB, 5 companies)
| Ticker | partial-payload renders | Result |
|--------|------------------------|--------|
| ADBE (non-retailer) | **0** | instant "No extracted data", no poll |
| LULU / TGT / COST | 1 each | store + geographic ("Store Count (Americas, United States … +8 more)") auto-surfaced, settled |
| M | 2 | settled |

- `_ratings_poll_fragment` RERUN_TRIGGER count since fix: **0** (was firing repeatedly).
- Total full-page reruns: **2-3 per ticker** (was climbing to 50+/session under the storm).
- No exceptions. Process RSS ~82 MB, flat. Fewer reruns ⇒ **lower** RAM/CPU churn, not higher.

## "Instant" — cache TTL fix (FLWS follow-up)
The boot warmup sweep (cache_manager.py:559) pre-warms `_edgartools_store_totals`,
`_edgartools_stores_by_country`, `get_square_footage_data` for ALL 353 tracked tickers — but
those had **`ttl=600` (10 min)** while the sweep runs **once at boot**, so after 10 minutes every
ticker re-cold → the ratings tab was slow all day. This 10-K-derived data updates quarterly, so
**raised those 3 TTLs to 21600 (6h)** (repository.py). Now the boot warmup keeps them warm all
day (until the next deploy re-warms).

RAM: measured ~0.6 KB per ticker → **~0.2 MB** for all 353 tickers resident at 6h TTL — negligible.

FLWS verification (incognito, staging DB over VPN):
- cold first view **3.8s** (was 15.6s fresh-boot / infinite), warm repeat **1.3s** (Streamlit
  page-rerun overhead, no fetch wait). On STG (co-located DB, boot-warmed) both are faster.
- `cr=False sc=False` → shows "No extracted data" and **settles** — no loop.
- Whole session: **0** `_ratings_poll_fragment` re-arm triggers; retry counters never climb
  (5×retry=0, 1×retry=1); no exceptions.

## Video confirmation (stg-screen-recording-issues.mov, 3.5 min)
Watched frame-by-frame (once moved to Downloads — ~/Desktop is TCC-blocked). Confirms the
pre-fix bug on STG: changing company on "Additional Data" → the branded **"Loading Ratings"
overlay stays stuck** over stale/empty content (ADBE @t190 shows "No extracted data" WITH the
overlay still up; FLWS @t40 stuck on "Loading store & ratings data…"). That is the reported
"loading nothing / infinite loop." Post-fix verified: FLWS/ADBE/LULU render with
`stuck_loader=False` everywhere, 0 poll re-arm triggers.

## App-wide TTL audit (evidence-based)
DB cadence probed: `av_company_overview` + `time_series_daily` update **daily**
(`fetched_at_utc`), financials/filings **quarterly** — nothing intraday. So 5–10 min TTLs were
pointless. Raised **68 caches** this pass (market_data financials/statements/dates/estimates/
forecasts/ratios/company-metadata/currencies/earnings-dropdowns → 6h; prices/quotes/charts →
1h; segment/compensation/EDGAR/sqft/filing-metadata/company lists → 6h). Combined with the
calendar (6) + ratings EDGAR (3): **74 caches now at 6h**.

Left deliberately short (18) — genuinely volatile or sensitive, need freshness:
news (articles/keyword search), live cross-search results, screening segment/keydevs member
caches (known staleness sensitivity), `_cached_portal_users` (ACL/security), forecast-refresh
admin view, `get_earnings_calls`/`search_transcripts_fulltext` (catch just-posted transcripts),
and the large `_build_doc_text_index`/`_kw_search_document` (RAM).

RAM: measured payloads are small (ratings ~0.6 KB/ticker; financials tens of KB); prices kept at
1h to bound their larger payloads. Net effect: far fewer cold re-fetches ⇒ lower CPU/RAM churn.

## Sparse-ticker date-range fix (FLWS "Dec 2016 → Dec 2016")
Root cause: `RatingsDataRepository._all_known_years` counted a fiscal year from ANY DB row,
including FLWS's lone `fy2016 store_count value='0'` (a zero store count = junk). That single
year drove `get_date_range` → Dec-2016→Dec-2016, so the tab showed misleading date dropdowns
above a "No extracted data" message. (`_store_row_passes_source_check` did NOT catch it — an
empty subset sums to 0, so val=0 spuriously "passes".)

Fix: `_all_known_years` now only counts a year whose row carries data the tab renders —
store_count needs `numeric_value > 0`; credit_rating needs a non-empty value. Verified:
`get_date_range` → FLWS (None,None) → clean "No extracted data", no dropdowns; LULU (2012–2026),
TGT (2020–2026), WMT (2018–2026) **unchanged** (no regression). UI-confirmed for all four.

## Notes
- `_ratings_settled` only stops **polling**; a manual refresh still re-renders and picks up
  freshly-cached late fetches, so slow (>~6s cold) EDGAR data appears on the next view.
- The screen recording could not be opened (macOS TCC blocks `~/Desktop` reads); root cause and
  verification are from the Downloads log + live UI. Move the .mov to Downloads to review it.
