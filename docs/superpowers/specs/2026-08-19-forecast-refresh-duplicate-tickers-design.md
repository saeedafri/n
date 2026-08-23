# Refresh Forecasting Models — duplicate companies (ZBRA / ZBRA.NASDAQ)

**Date:** 2026-08-19
**Area:** `/forecasting` → “Refresh Data” dialog
**Status:** display fix shipped; 3 DB rows still need the data team

---

## 1. Symptom

The Refresh popup lists the same company twice:

| Ticker | Company | Exchange | Reporting Date | Last Refresh | Action |
|---|---|---|---|---|---|
| ZBRA | Zebra Technologies | NYSE | Feb 12, 2026 | **Jul 08, 2026 03:28** | Refresh Now |
| ZBRA.NASDAQ | Zebra Technologies | NASDAQ | — | **Aug 15, 2026 14:40** | On hold |

The data team had already cleaned `coreiq_companies`, so the report was “the DB is
fixed but the UI still shows duplicates”.

## 2. Why the DB fix did not clear it

The dialog never reads `coreiq_companies`. `get_refresh_table_data()` is:

```sql
SELECT ticker, MAX(computed_at), MAX(last_actual_date)
FROM coreiq_model_forecasts GROUP BY ticker
```

i.e. it lists **every ticker key the forecast engine has ever written**. Nothing
deletes a key that the engine stops producing, so the table is a graveyard.

Two independent causes stack:

**(a) Bogus composite keys.** The engine keys a company as `TICKER.exchange_acronym`
when the acronym is set. `exchange_acronym` is meant to hold a *Yahoo suffix*
(`L`, `DE`, `TO`, `HK`) and must be NULL for US listings. Three SEC rows still hold
an exchange **name**, so the engine writes `ZBRA.NASDAQ`, `ORCL.NYSE`, `CMRC.NASDAQ`.

**(b) Retired keys are never deleted.** When the data team cleared the acronym for
APP / HPQ / MANH / NOW / SHOP / TWLO, the engine started writing the plain key on
the 2026‑08‑15 run — and the Jul‑31 composite rows stayed behind forever.

Measured on STG 2026‑08‑19 (`coreiq_model_forecasts`, 407 distinct keys):

| Pair | Written 2026‑08‑15 (live) | Frozen (ghost) |
|---|---|---|
| APP / APP.NasdaqGS | APP | APP.NasdaqGS (Jul 31) |
| HPQ / HPQ.NYSE | HPQ | HPQ.NYSE (Jul 31) |
| MANH / MANH.NASDAQ | MANH | MANH.NASDAQ (Jul 31) |
| NOW / NOW.NYSE | NOW | NOW.NYSE (Jul 31) |
| SHOP / SHOP.NYSE / SHOP.TO | SHOP | SHOP.NYSE, SHOP.TO (Jul 31) |
| TWLO / TWLO.NYSE | TWLO | TWLO.NYSE (Jul 31) |
| **ORCL / ORCL.NYSE** | **ORCL.NYSE** | ORCL (Jul 08) |
| **ZBRA / ZBRA.NASDAQ** | **ZBRA.NASDAQ** | ZBRA (Jul 08) |
| **CMRC.NASDAQ / CMRC.NasdaqGS** | **CMRC.NASDAQ** | CMRC.NasdaqGS (Jul 31) |
| QRTEA | — | QRTEA (no company row at all) |

Genuine same-base pairs that must survive: `JD` (JD.com) / `JD.L` (JD Sports) and
`TSCO` (Tractor Supply) / `TSCO.L` (Tesco) — different companies, both refreshed
2026‑08‑15.

## 3. Fix

**Display side** (`app/data/forecast_refresh_service.py`) — one rule, no suffix
whitelist or blacklist: *a forecast row is live only if the current company universe
can still produce its key.*

- `live_forecast_tickers()` — `@st.cache_data(ttl=3600)`; reads
  `coreiq_companies` and builds `{ticker}` when `exchange_acronym` is empty,
  `{ticker}.{acronym}` when it is set. Returns an empty set on DB failure.
- `drop_retired_tickers(rows)` — filters the popup rows against that set; an empty
  set means “filter off”, so a failed query can never blank the dialog.
- Called in both `get_refresh_table_data()` (annual) and
  `_get_quarterly_refresh_table_data()`.

It self-heals: as the data team clears an acronym, the composite key stops being
producible and disappears from the popup without a code change.

Because `get_refresh_table_data()` is the single source for the dialog, the
“Refresh All” loop and `utils/forecast_auto_refresh.py` inherit the same filter —
the auto-refresh job stops burning runs on keys the engine can never update.

Not touched: the Excel export and email report already `INNER JOIN coreiq_companies`,
so ghost keys never reached them.

**DB side (data team — no app writes).** `exchange_acronym` must be a Yahoo suffix or
NULL. Three SEC rows still carry an exchange name:

```sql
UPDATE coreiq_companies
SET exchange_acronym = NULL
WHERE source = 'SEC'
  AND ticker IN ('ZBRA', 'ORCL', 'CMRC')
  AND UPPER(TRIM(exchange_acronym)) IN ('NASDAQ', 'NYSE');
```

Optional graveyard cleanup (11 annual + 2 quarterly keys):

```sql
DELETE FROM coreiq_model_forecasts
WHERE ticker IN ('APP.NasdaqGS','CMRC.NasdaqGS','HPQ.NYSE','MANH.NASDAQ','NOW.NYSE',
                 'QRTEA','SHOP.NYSE','SHOP.TO','TWLO.NYSE');
-- plus 'ORCL','ZBRA' from both tables AFTER the acronym UPDATE above,
-- so the plain keys become the live ones again.
```

Ordering matters: run the `UPDATE` first. Until it runs, the popup shows
`ZBRA.NASDAQ` / `ORCL.NYSE` / `CMRC.NASDAQ` — the *fresh* rows, but under a wrong
ticker, and “On hold” because the earnings-calendar lookup cannot resolve a
composite key for a US listing.

## 4. Verification (STG, 2026‑08‑19)

Filter dry-run against the live DB:

```
coreiq_model_forecasts:           407 → 396  (11 dropped)
coreiq_model_forecasts_quarterly: 355 → 353  ( 2 dropped)
remaining base/composite pairs:   JD.L, TSCO.L   (both correct)
```

Real UI (`bash .claude/dev/run_local.sh` + Playwright, dialog scrolled to Z):

```
ZBRA  -> ['ZBRA.NASDAQ']     (was ZBRA + ZBRA.NASDAQ)
ORCL  -> ['ORCL.NYSE']
CMRC  -> ['CMRC.NASDAQ']
SHOP  -> ['SHOP']
QRTEA -> []
TSCO  -> ['TSCO', 'TSCO.L']  (kept — different companies)
JD    -> ['JD', 'JD.L']      (kept — different companies)
```

Unit check on the pure filter: retired keys dropped, legitimate pairs kept, empty
live-set = pass-through.

## 5. Rollout

Code change only, no schema and no data writes. Cache TTL 1h on
`live_forecast_tickers()`; the popup's own cache is already invalidated explicitly by
every refresh path.
