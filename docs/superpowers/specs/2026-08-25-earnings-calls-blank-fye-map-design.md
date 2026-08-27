# Earnings Calls renders blank — `_get_fiscal_year_end_map` window-sort over 52 MB of JSON

**Date:** 2026-08-25
**Reporter evidence:** screenshot of `marketdata-stg.coresight.com/earnings_calls` (LULU / 2026 / Q1) showing filters but no transcript; `server-logs-20260825_131848.log`
**Status:** Fixed in `app/data/repository.py` (`EarningsCalendarRepository._get_fiscal_year_end_map`), verified locally against STG DB. Not deployed.

---

## 1. Symptom

`/earnings_calls` renders the header, the Search box and the Company / Year / Quarter
selectors, and then **nothing**. No left search panel, no right transcript panel, no
error. The page just never finishes.

The data was never missing. `coreiq_av_earnings_call_transcripts` has the LULU 2026 Q1
row with a 45,768-character transcript. The page was blocked mid-render.

## 2. Evidence trail

### 2.1 The page never completes

Every `earnings_calls` rerun in the log's tail starts but none ends:

```
06:47:44  [PAGE_LOAD] === earnings_calls | PAGE LOAD SEQUENCE START ===
06:47:44  STEP=FETCH_TRANSCRIPT | elapsed=0.01ms
06:47:54  ERROR  earnings_calls.py:2452 | [EC] Earnings event future failed for LULU:
          (no PAGE LOAD SEQUENCE END, no [CLICK->RENDER])
```

Exactly 10 s between the last step and the error — that is
`_edate_future.result(timeout=10)` in `app/pages/earnings_calls.py:2449` expiring.
`TimeoutError` stringifies to `""`, which is why the log message ends in a bare colon.

`st.columns()` is created before this block, so the two column shells exist but their
contents are produced *after* the blocking call — hence filters visible, body empty.

### 2.2 What the 10 s future was waiting on

Other sessions that survived long enough to log a total show the real cost:

| Time (25-Aug) | Ticker | `get_earnings_event_dates` | page total |
|---|---|---|---|
| 12:20:50 | ADBE | **70,943 ms** | 72.8 s |
| 12:55:14 | TSCO | **60,830 ms** | — |
| 12:55:14 | BKE  | **34,168 ms** + **37,241 ms** (both, same ticker) | 9.2 s |

Normal historical values for the same call: 12–360 ms.

### 2.3 The single query responsible

```
[DB_SLOW]       engine=read execute_ms=69673 | pool_checkedout=1 pool_size=5
[DB_READ_SPLIT] checkout_ms=0 exec+deserialize_ms=69675 rows=56
                sql=SELECT ticker, payload_json FROM ( SEL...
```

`checkout_ms=0` — not pool starvation. 69.7 s of pure server-side execution to return
**56 rows**. Occurrences on 25-Aug alone: 04:52 (73.4 s), 12:20 (69.7 s), 12:55 (72.6 s),
17:57 (84.5 s). The worst `exec+deserialize_ms` seen in the file is **117,038 ms**.

That SQL is the YF half of
`EarningsCalendarRepository._get_fiscal_year_end_map()` (`repository.py:8497`), the
first thing `get_earnings_event_dates()` calls.

### 2.4 Why it is slow — measured on STG

```
coreiq_yf_company_overview:
  rows            7,110
  distinct ticker    56          (~127 daily ETL snapshots per ticker)
  SUM(LENGTH(payload_json))  52.2 MB
  AVG(LENGTH(payload_json))   7.5 KB
indexes: PRIMARY(id), idx_ticker(ticker), idx_ticker_ingested(ticker, ingested_at), …
```

The query put `payload_json` **inside** the `ROW_NUMBER() OVER (PARTITION BY ticker
ORDER BY ingested_at DESC)` derived table. MySQL therefore materialises all 7,110 rows
*including* 52 MB of JSON into a temp table, sorts it, and then throws away 7,054 rows.
On Azure MySQL Flexible that temp table spills to disk — hence 70–117 s.

This is the same anti-pattern already documented in this repo for Key Devs
(`screening_service.py`, "Late row lookup" comment): *never carry a fat column through
a window sort.*

### 2.5 Why it hits at random times

`_get_fiscal_year_end_map` is `@st.cache_data(ttl=21600)` (6 h) and is warmed on boot
(`utils/cache_manager.py:386`). So it is cheap all day — until the TTL lapses, and then
**whichever user happens to load the page next eats the full 70–117 s**, with the page
blank the whole time. STG deploys ~6×/day reset the clock, which is why it looks
intermittent.

### 2.6 Secondary amplifier

On timeout, `earnings_calls.py:2453` falls back to calling
`EarningsCalendarRepository.get_earnings_event_dates(...)` **synchronously** — the same
work again. That is the BKE 34 s + 37 s pair in §2.2: one prefetch thread and one
foreground call both grinding the same query. Left as-is; with the query fixed the 10 s
timeout no longer fires.

## 3. Fix

`app/data/repository.py` → `_get_fiscal_year_end_map`, YF branch.

Late row lookup: rank on **narrow** columns only (`id`, partitioned by `ticker`,
ordered by `ingested_at` — served index-only from `idx_ticker_ingested`), then join back
by primary key to fetch `payload_json` for just the ~56 surviving rows.

```sql
SELECT o.ticker, o.payload_json
FROM (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ingested_at DESC) AS rn
        FROM coreiq_yf_company_overview
        WHERE ticker IS NOT NULL
    ) ranked
    WHERE rn = 1
) latest
JOIN coreiq_yf_company_overview o ON o.id = latest.id
WHERE o.payload_json IS NOT NULL AND o.payload_json != ''
```

No schema change, no new index, no DB write. Python-side extraction
(`_extract_yf_fiscal_year_end`) is untouched.

## 4. Verification

**Result equivalence** — old vs new run back to back against STG, both 56 rows,
`old == new` on the full `{ticker: payload_json}` dict: **identical**.
FYE resolves for 45/56 YF tickers (unchanged; the other 11 have no fiscal field in their
YF payload and fall back to `coreiq_av_company_overview.fiscal_year_end` via
`setdefault`, as before).

**Query timing** (Mac over VPN, so absolute numbers are inflated by the ~233 ms RTT):

| | cold | warm |
|---|---|---|
| old | 10.9 s | 9.6 s |
| new | 7.9 s | **1.8–2.0 s** |

**Same-log before/after**, both instances writing to `server-logs/server-log.log`:

```
06:57:50  old SQL   exec 11,627 ms  →  [EC] Earnings event future failed for LULU
          EC_PAGE_TOTAL 14,093.0 ms | company=LULU
06:58:51  new SQL   exec  2,153 ms  →  (no timeout)
          EC_PAGE_TOTAL  4,742.4 ms | company=LULU
```

**UI, fixed instance only** (stale pre-fix server killed first):

- Test server: `http://localhost:8502/earnings_calls` — `bash .claude/dev/run_local.sh`
  with `--server.port 8502`, `APP_ENV=LOCAL`, STG DB.
- `?ticker=LULU&year=2026&quarter=Q1` → content in **2.2 s**. Renders
  "Lululemon Athletica Inc. (LULU) · 2026 · Q1 · Report date: Jun 04, 2026 ·
  Quarter ended: Apr 30, 2026 · Fiscal Period: Q1 FY2026", the Download button, and the
  full Operator / Howard Tubin transcript. Screenshot: `ec_LULU.png`.
- `?ticker=ADBE&year=2026&quarter=Q2` → 4.1 s. `?ticker=TSCO&year=2026&quarter=Q2` → 3.1 s.
- `Earnings event future failed` occurrences after the fix: **0**.

Dates match the DB directly: `get_earnings_event_dates("LULU", 1, 2026)` →
`{report_date: 2026-06-04, fiscal_period_end_date: 2026-04-30}`.

## 5. Blast radius

`_get_fiscal_year_end_map` has 4 callers, all in `repository.py`
(`get_earnings_event_dates`, and lines 8170 / 9656 / 9724) plus the boot warmup. All
consume the same `{ticker: month_name}` dict, which is byte-identical before and after.
Pages affected: `earnings_calls`, `earnings_calendar`, `company_filings` (any fiscal
quarter labelling).

## 6. Rollout

Code-only, ship with the next STG deploy. No app setting, no migration, no backfill.

Post-deploy check: `grep 'get_earnings_event_dates' server-logs/server-log.log` — values
should stay in the hundreds of ms, and `Earnings event future failed` should not appear.

## 7. Follow-ups (not done, out of scope)

1. **Timeout fallback duplicates work.** `earnings_calls.py:2453` re-runs the identical
   call synchronously when the future times out. Prefer waiting on the future (the
   `@st.cache_data` entry it is populating) over re-issuing.
2. **`coreiq_yf_company_overview` retains every daily snapshot** — 7,110 rows / 52 MB for
   56 companies, growing daily. Every "latest per ticker" reader pays for that. A
   retention policy or a `latest`-flagged view is a data-team call
   (see memory: *No DB writes — data team owns DB*).
3. **Blank-page UX.** A 70 s block renders as an empty page with no message. A spinner or
   a degraded header (skip the announcement dates, still show the transcript) would fail
   visibly instead of silently.
