# Refresh Forecasting Models — "Loading forecast data…" takes ~80s

**Date:** 2026-08-19
**Area:** `/forecasting` → Refresh Data popup
**Status:** fixed (code only, no DB writes, not deployed)

---

## 1. Symptom

The popup opens and sits on "Loading forecast data…" for over a minute.

## 2. Root cause — one query reading 50 MB of JSON to return 45 rows

`_fiscal_year_end_bulk()` Source 4 resolved each company's fiscal-year-end month from
`coreiq_yf_company_overview.payload_json`:

```sql
SELECT ticker, CAST(JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.info.lastFiscalYearEnd')) AS UNSIGNED)
FROM (
    SELECT ticker, payload_json,
           ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ingested_at DESC) AS rn
    FROM coreiq_yf_company_overview
    WHERE ticker IS NOT NULL AND payload_json IS NOT NULL
) x
WHERE rn = 1 AND JSON_EXTRACT(payload_json,'$.info.lastFiscalYearEnd') IS NOT NULL
```

`payload_json` is listed **inside the derived table**, so MySQL materialises the blob for
every row before ranking. The table is 6,834 rows / **50.3 MB** across only **56 distinct
tickers** — and the query returns **45 rows**. Measured on STG:

| | |
|---|---|
| cold buffer pool | **119,478 ms** |
| second cold run | 59,183 ms |
| warm | 1,086 ms |

`WHERE payload_json IS NOT NULL` in the subquery has the same effect on its own — any
predicate on the blob column forces the read.

Cold call tree of one popup open, before:

```
DIALOG_TOTAL                        ~82 s
├─ _annual_q4_report_dates_bulk     69,794 ms
│  ├─ _fiscal_year_end_bulk         59,485 ms   ← 85% of the whole thing
│  │  └─ Source 4 (YF payload_json) 59,183 ms
│  └─ nasdaq calendar window         8,700 ms
├─ DIALOG_Q1 (forecast MAX per tk)   600–8,600 ms
└─ DIALOG_Q2 (company meta)          1,372 ms
```

## 3. Why users kept paying it

`@st.cache_data(ttl=3600)` on all five caches in the module. The underlying data —
earnings calendars, vendor overviews, the nightly model run — changes at most daily, so
the 1-hour TTL bought no freshness and guaranteed that some user rebuilt the whole chain
every hour. STG also restarts ~6×/day (deploys plus Azure recycles), and each restart
wipes every in-process cache. `_warm_refresh_dialog()` exists in the boot warmup but runs
**last**, after seven other warm tasks, so a user clicking early after a restart still
pays full price.

## 4. Fix

**(a) Keep the blob out of every access path but the final projection.**

```sql
SELECT o.ticker,
       CAST(JSON_UNQUOTE(JSON_EXTRACT(o.payload_json,'$.info.lastFiscalYearEnd')) AS UNSIGNED) AS fye_epoch
FROM (
    SELECT ticker, MAX(ingested_at) AS latest_ingested_at
    FROM coreiq_yf_company_overview
    WHERE ticker IS NOT NULL
    GROUP BY ticker
) latest
JOIN coreiq_yf_company_overview o
  ON o.ticker = latest.ticker AND o.ingested_at = latest.latest_ingested_at
```

EXPLAIN: inner is `Using index for group-by` on `idx_ticker_ingested` (loose index scan,
no blob touched); outer is a `ref` lookup returning one row per ticker, so exactly 56 blobs
are read. **280 ms, cold or warm.**

Equivalence verified: both queries return the same 45 tickers with the same FYE month.
`coreiq_yf_company_overview` has zero NULL `payload_json` rows, so dropping that predicate
from the aggregate loses nothing; rows whose payload lacks the key come back as NULL and
were already skipped in Python.

**(b) TTL 1h → 6h** for all five caches, via one `_DIALOG_CACHE_TTL_S` constant. Freshness
after a model run does not depend on the TTL — every refresh path calls
`get_refresh_table_data.clear()` explicitly. The TTL only bounds vendor-side staleness,
where 6h is well inside the daily ingest cadence.

## 5. Result — measured in the real UI, cold process, `WARM_ON_BOOT=0`

| Stage | Before | After |
|---|---|---|
| `_fiscal_year_end_bulk` Source 4 | 59,183 ms | **865 ms** |
| `_fiscal_year_end_bulk` total | 59,485 ms | **1,127 ms** |
| `_annual_q4_report_dates_bulk` | 69,794 ms | **4,579 ms** |
| **DIALOG_TOTAL (first click)** | **~82 s** | **6.5 s** |

Output unchanged: 336 NASDAQ + 36 YF = 372 tickers resolved, 29 upcoming, same rows.

## 6. Left alone, deliberately

- **NASDAQ calendar window query — ~2.9 s, run once per cadence.** It scans
  `coreiq_nasdaq_earnings_calendar` (18,553 rows) and ships 18,338 after a
  `ROW_NUMBER()` dedup that removes only 215 rows. It could be cut to ~700 ms with a
  36-month `earnings_date` window (18,338 → 3,143 rows), but that is a **product change**:
  CHSCP's newest calendar row is 2023-04-05, so it would flip from showing a real date to
  "On hold". The 2026-07-30 decision is that a real old date beats a hidden one. Needs a
  product call before touching. Alternatively the selection could ship only the
  nearest-upcoming and most-recent-past row per (ticker, fqe_month) — semantically
  identical, but it rewrites the selection logic of two functions and was not worth the
  risk next to a 12× win already banked.
- **`_fiscal_year_end_bulk` Source 1** (AV overview window, 411 rows) shows 5.3 s on a
  fully cold buffer pool and ~260 ms otherwise. The shape is fine; that is cold I/O.
- **Warmup ordering.** `_warm_refresh_dialog` is still last in the boot warm list. With the
  chain now at 6.5 s cold and a 6-hour TTL, moving it is no longer worth the churn.
