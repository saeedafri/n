# Key Devs screening freeze — root cause and fix

**Date:** 18-Sep-2026
**Reported:** page stuck after switching Companies → Key Devs; two spinner cards;
Excel button styled differently in Key Devs; "is it rebuilding on every restart?"
**Evidence:** `server-logs-20260918_074742.log`, user session 13:13:26 → 13:16:43 IST.

---

## 1. What actually happened

Two queries, neither of them materialized, each a full scan of the 211,290-row
partitioned `coreiq_company_events`:

| Time | Query | Duration | Rows |
|---|---|---|---|
| 13:13:48 → 13:14:22 | `SELECT DISTINCT e.ticker …` + 3 master joins (`get_all_companies_universe`) | **34.7s** | 4,481 |
| 13:14:44 → 13:16:43 | `SELECT DISTINCT event_category, event_subtype` (`get_keydev_subtypes_by_category`) | **119.2s** | 130 |

Log lines: `[DB_READ_SPLIT] … exec+deserialize_ms=34659` and `…=119176`, each
followed by `[PAGE_LOAD_SLOW] … TOTAL=34700.8ms` / `TOTAL=119298.3ms` and
`[CLICK->RENDER] render=119.30s | SLOW`.

`EXPLAIN` on the second: `type: ALL, possible_keys: NULL, key: NULL, Using
temporary`, all 34 date partitions. There is **no index on
`(event_category, event_subtype)`**.

### Answer to "is the materialized cache rebuilding every restart?"

No. The same log shows `[MAT][screening_universe] HIT rows=510 read=0.01s` — the
materialized caches behaved exactly as designed. **These two queries were never in
the materialize layer at all.** They were plain `@st.cache_data(ttl=3600)`, which is
per-worker and per-hour, so every app restart and every hourly expiry handed the
full 154s to whichever user clicked first.

### Aggravating factor

From 12:50 to 13:09 the segment-cache background refresh failed on **every** chunk
from 21 to 41 — 20 consecutive minutes of 3×20s timed-out scans against the 14.4M-row
`coreiq_filing_metrics_v5`, holding read-pool connections and leaving the DB buffer
pool cold right before the user's session. The loop had no circuit breaker; it would
have kept going to the last chunk. `raise_on_timeout` would have discarded the whole
build anyway, so those 20 chunks bought nothing but load.

---

## 2. Fixes

### 2.1 Materialize both queries — `app/data/screening_service.py`

`get_all_companies_universe()` → `materialized_or_build("screening_all_companies_universe", …)`
`get_keydev_subtypes_by_category()` → `materialized_or_build("keydev_subtype_taxonomy", …)`

Freshness signal (measured **0.33s warm**, 7.1s on a cold buffer pool):

| Table | Signal | Why |
|---|---|---|
| `coreiq_company_events` | `MAX(event_id)` | index max; a new ticker or event type only ever arrives as an INSERT |
| `coreiq_companies` | CRC32 of the materialized columns | re-classification is an UPDATE — invisible to `COUNT(*)` |
| `coreiq_sec_companies_all` | CRC32 of ticker/name/industry | same |
| `coreiq_non_sec_companies_all` | CRC32 of ticker/industry | same |

Behaviour: fresh → disk read; stale → **previous generation served instantly,
rebuild in a background thread**; nothing on disk → build once. So a data change
never makes a user wait, which is the stated requirement.

### 2.2 The 119s query walks event_id ranges instead of scanning once

The single DISTINCT sits **on** the 120s `DB_READ_TIMEOUT`: 119.176s on STG (passed)
and 124.7s from a dev box (failed). Whether the taxonomy loaded at all was a coin
flip. It now walks `event_id` in ranges of 25,000 — measured 16.4s / 27.1s / 20.7s
for the three populated ranges and 0.25s for each empty one — on a **raising**
connection, because most chunks are legitimately empty and a swallowed timeout would
look like a normal gap.

Result verified identical to the original query: **130 pairs across 23 categories**.

### 2.3 Never materialize a failed read

`db_manager.execute_query_readonly` swallows exceptions and returns `[]`. During
development this wrote an **empty dict to disk as the answer**, where it would have
sat until the signal next moved. Both builds now raise when the result is empty,
which is impossible for these tables. Nothing is written on a failure and the
previous generation stands.

### 2.4 Warm both at boot — `app/utils/cache_manager.py`

`_warm_keydevs_screening()` added to the background warmup list. Measured 5.75s in a
fresh process (handshake + two signature queries + two disk reads). The one case
where it is not cheap — an empty cache directory — is the 125s + 35s first build,
and it now happens on the warmup thread instead of a user's click.

### 2.5 Segment-cache circuit breaker — `app/data/screening_service.py`

Stops after 3 consecutive chunk failures and logs once with the chunk number and
skipped count, instead of grinding through every remaining chunk.

### 2.6 Two spinner cards — `app/pages/screening.py`

`render_sticky_loader("Loading Key Developments")` (the branded card in the
screenshot) already covers the Key Devs fetch block. A second
`_render_coresight_loading_overlay` had been added two lines later; both are
`position: fixed` and centred, so they stacked into a visible card-inside-a-card.
The second overlay is **deleted**. One loader per wait.

### 2.7 Excel button identical on every tab — `app/pages/screening.py`

`_render_excel_js_download` injected `_EXCEL_BTN_CSS` with `st.markdown` right before
its `st.container(key="xlbtn-…")`. That works in Companies and People (plain
`st.columns`), but the Key Devs button renders `with _dl_slot:` where
`_dl_slot = _dl_col.empty()` — and **`st.empty()` holds exactly one element**, so the
container replaced the `<style>`. The rules now live in `_SCREENING_CSS`, emitted
once per page, so every tab is identical by construction and the dead duplicate block
is gone.

Only Companies, Key Devs and People have results today; Equities, Fixed Income,
Transactions and Projects/Portfolios are "coming soon" placeholders with no grid and
therefore no export button yet. They will inherit this styling when they gain one.

### 2.8 New log lines

| Line | Where | Answers |
|---|---|---|
| `[BUILD] root=… newest_py=… files=… bytes=… APP_ENV=…` | `server_logger.py`, once at boot | "was this a deploy or a restart?" — `root` is a fresh `/tmp/<hash>` per deploy; `files`/`bytes` catch a hand-edited container |
| `[MAT][name] STALE → … \| changed: <table> sig a→b rows c→d` | `materialize.py` | "why is it rebuilding?" — names the table that moved |
| `[SCREEN_FOR] Companies -> Key Devs` | `screening.py` | which tab the user was on when it went slow |
| `[SCREENING] segment cache: giving up after N consecutive chunk failures at chunk X of Y` | `screening_service.py` | a stuck background refresh, once instead of 40 times |

`_total_rows` in `materialize.py` also counts dicts, so a healthy taxonomy HIT reads
`rows=130` instead of `rows=0`.

---

## 3. Measured result

| | Before | After |
|---|---|---|
| Subtype taxonomy | 119.2s (STG), 124.7s = timeout (dev) | **0.30s** disk hit |
| All-companies universe | 34.7s | **0.31s** disk hit |
| Cold build (background, once) | — | 125s + 35s, on the warmup thread |
| Add Criteria (UI, end to end) | — | 2.5s, **1** overlay card |
| Show Results (UI, end to end) | — | 7.6s, **1** overlay card |
| Excel button, Key Devs | unstyled grey | `rgb(214,46,47)`, 13px, right edge 1520 |
| Excel button, Companies | `rgb(214,46,47)`, 13px, right edge 1520 | unchanged |

UI verified with Playwright against `http://localhost:8501/screening` (staging DB):
268 key development events found, 32 industries, grid rendered, max 1 loading card on
both the Add Criteria and Show Results paths.

---

## 4. Outstanding — for the data team

The rebuild is 125s only because the scan is unindexed. One statement makes it
milliseconds:

```sql
ALTER TABLE coreiq_company_events
  ADD INDEX idx_cat_subtype (event_category, event_subtype);
```

Not applied here: this repo does not write to the DB.
