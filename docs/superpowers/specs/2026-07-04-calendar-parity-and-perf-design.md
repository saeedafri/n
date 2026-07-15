# Calendar data-parity + performance, graph speed, and "all pages slow" diagnosis

**Date:** 2026-07-04
**Author:** Claude (Opus 4.8), driven by Mohd Saeed Afri
**Scope:** `earnings_calendar` page (rename → "Calendar", data-mismatch, render speed),
`StockQuoteRepository.get_price_history` (chart speed), and an evidence-based
diagnosis of the "all pages slow" report.

---

## 1. The critical incident: STG showed 462 events, prod shows 16,052

The testing team reported STG's calendar badge read **"462 events · 250 companies ·
263 M&A completions"** while production reads **"16,052 events · 330 companies"**.
This looked like data loss ("what have you done??").

### Root cause (proven, not guessed)

A prior perf change had **date-windowed the events fetch** to the visible FullCalendar
range:

```python
# app/pages/earnings_calendar.py  _fetch_all_events (before)
EarningsCalendarRepository.get_calendar_events(
    tickers=None, start_date=_vis_start, end_date=_vis_end_inclusive)   # 42-day window
```

and the badge counted **only the in-view rows**:

```python
_n_total = len(events) + len(ma_events)   # events = windowed → ~199 + M&A → 462
```

So the DB was **fully intact** — only the *display scope* had shrunk. Read-only STG
probes (`get_calendar_events(tickers=None)`):

| Query | Events | Companies |
|---|---|---|
| Full (prod behaviour) | **16,052** | **330** ← matches prod exactly |
| Windowed to July 2026 | 199 | 199 |
| Windowed to full-year 2026 | 543 | 320 |

Raw tables present: `coreiq_nasdaq_earnings_calendar` 15,732 + `coreiq_yf_earnings_calendar`
528 rows, events spanning **2005–2026 (21 years)**. **No data was deleted.**

A second, independent cause of the number mismatch: the STG badge folds M&A into the
totals (`len(events) + len(ma_events)`, union of tickers), whereas **production's badge
is earnings-only**: `app/pages/earnings_calendar.py` (prod) line ~1653:
```python
{len(events):,} events · {len(set(e['ticker'] for e in events))} companies
```

---

## 2. The fix — full set for the count, windowed set for the render

Production fetches **all** events and lets FullCalendar page client-side; that is why its
badge is the full 16,052. But the user's explicit constraint is *"don't fetch everything
and load my RAM."* We reconcile both:

- **Count from the full set** → badge matches prod (16,052 · 330 · 263).
- **Render only the visible window** → FullCalendar never receives 16k events.

### 2a. `EarningsCalendarRepository.get_calendar_events_full()` (new)

Disk-materialized full deduped set (same pattern as `get_ma_completion_events`):

- Freshness **signature = row counts of the two calendar tables** (small, indexed → the
  check is instant and only moves on ingest). No full-count-trap (those tables are not the
  12.5M-row filing table).
- Materializes a **`DataFrame(dtype=object)`**, not the raw list: compact fingerprint
  (rows/cols/hash, ~425-byte meta) *and* exact value preservation. `dtype=object` keeps
  `None` as `None` and `int` as `int`, so `to_dict("records")` cannot re-introduce the
  `NaN`/`float` coercion that once crashed the M&A overlay. **Verified: 0 value mismatches,
  0 NaN leaks, 0 type drift vs the direct query.**
- Cold build ≈ 3.6 s (the dedup `ROW_NUMBER() OVER(...)` UNION); every subsequent read is a
  **~40 ms disk hit** (`[MAT][calendar_events_full] HIT rows=16,052 read=0.04s`).

### 2b. Page rewiring (`app/pages/earnings_calendar.py`)

- `_fetch_all_events()` → `get_calendar_events_full()` (no date args).
- New `_window_events(evs, vstart, vend)` filters to the visible range in Python (~ms);
  handles both the earnings feed (ISO-string dates) and the M&A feed (date objects).
- Badge counts the **full** filtered set (earnings-only, matching prod); the render is
  windowed right after the badge.
- Specific-company selection filters the full prefetched set in memory (`ticker IN …`) —
  identical result to the old per-ticker SQL, no extra round-trip.
- Badge changed to earnings-only:
  ```python
  _n_total    = len(events)                          # 16,052 — matches prod
  _n_companies = len({e['ticker'] for e in events})  # 330    — matches prod
  # M&A stays an ADDITIVE suffix, never folded into the totals
  ```

### 2c. Warmup (`app/utils/cache_manager.py`)

Track-0 now warms `get_calendar_events_full` + `get_ma_completion_events`, so the first
page load *and every month/year navigation* are cache hits (no dedup SQL, no per-nav 700 ms).

### Measured result (local UI)

| | Before | After |
|---|---|---|
| Badge | 462 · 250 · 263 | **16,051 · 330 · 263** (matches prod) |
| Warm page load | 0.6–3.8 s | **1.0 s** (grid rendered) |
| `EC_PAGE_FETCH_ALL_EVENTS` | 700–3577 ms SQL | **0.00 ms** (materialize+cache hit) |
| Company filter (FLWS) | — | **72 · 1 · 2** (correctly reduced) |

Month view opens on the current month (July 2026); Year Wise renders the year grid; both
keep the full badge — identical to production semantics.

---

## 3. Rename "Earnings Calendar" → "Calendar"

Display label only — the URL slug `earnings_calendar` is unchanged (no broken
links/bookmarks). Edits: `app/main.py` page registration, `app/components/navigation.py`
nav link, `st.set_page_config(page_title=…)`, and the error string. The boot splash already
maps `/earnings_calendar` → "Loading Calendar".

---

## 4. The chart ("graph taking too much time")

`StockQuoteRepository.get_price_history(ticker, days=1825)` (SEC path) selected the big
`raw_json` blob for **all ~1,248 rows** and ran `json.loads` on each just to read `volume`
— even though the main page chart only plots `date`+`close`. The table has a **covering
index `(ticker, day_date, close, volume)`**, so we now read the columns directly:

```sql
SELECT day_date, close, volume
FROM coreiq_av_time_series_daily
WHERE ticker = :ticker AND day_date >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
  AND close IS NOT NULL
ORDER BY day_date ASC
```

Verified on STG: `close`/`volume` columns equal the raw_json values **exactly** (0 drift),
**0** in-range rows have a NULL close (nothing dropped), and `volume` is preserved for the
secondary volume chart in `app/utils/market_data.py`. Pure covering-index scan — no blob
transfer, no 1,248 `json.loads`. Plotly build itself is ~20 ms (never the bottleneck).
(The YF path also has close/volume columns but is left on raw_json pending the same
parity check — the SEC path covers the US retailers.)

---

## 5. "All pages have become so slow" — diagnosis

Timing evidence (`server-logs/server-log.log`) separates two very different things:

- **Warm requests are fast**: market_data tab switches **30–45 ms**, calendar page
  **~30 ms** after warm.
- **Cold first-load is the pain**: the first query on a *fresh DB connection* from the
  local box to Azure MySQL costs **~7 s** (`SELECT 1` = 7303 ms cold, 320 ms warm) — an
  SSL-handshake / cold-connection cost. This is a **local-testing artifact** (home network →
  Azure); on STG the app and DB are co-located in Azure, so it is ~200 ms.

The genuinely STG-relevant server-side costs were the calendar dedup SQL (3.6 s) and the
chart's raw_json blob fetch — **both fixed above**. Remaining cold-load cost is empty
`st.cache_data` on the first view of each page/ticker, mitigated by the boot warmup.

---

## 6. Testing

- **DB integrity probe** (`verify_full_fix.py`): full set = 16,052 · 330; materialize
  MISS→WRITE→HIT; 0 value/type/NaN drift vs the direct query; July window = 198.
- **UI (Playwright, local STG DB)**: badge 16,051 · 330 · 263; grid renders in 1.0 s;
  "Calendar" in nav, "Earnings Calendar" gone; company filter → 72 · 1 · 2; year view
  renders; Macy's chart renders with correct values (Mkt Cap $6.12B, P/E 9.61).
- **Chart parity probe**: covering-index close/volume == raw_json (0 mismatches), volume
  present on 1,248/1,248 rows.

## 7. Rollout / risk

- All changes are read-path only; no schema, no writes. No data touched.
- `get_calendar_events` (windowed) is retained for the alert dispatcher / forecast service
  callers — only the page and warmup switched to the full+materialized variant.
- Materialized artifact self-heals: a signature change (ingest) triggers a rebuild.
