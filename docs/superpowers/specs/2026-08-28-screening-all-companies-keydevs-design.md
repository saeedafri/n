# Key-Dev Screening Beyond Coresight Coverage — Design

**Date:** 2026-08-28
**Page:** `/screening` (Key Devs mode, and the shared criteria pipeline)
**Request:** business teams want *Key Developments by Category* across **all**
companies with events, not only the ~450 in `coreiq_companies`; plus an Industry
column on the results card.

---

## 1. Root cause

Key-dev screening was hard-bounded to the Coresight coverage list. Every path
went through one universe:

```
get_base_company_universe()          app/data/screening_service.py
  └─ CompanyRepository.get_companies_rows()   → SELECT ... FROM coreiq_companies
       └─ st.session_state.scr_working_df
            └─ _tickers = tuple(df["ticker"].values)      app/pages/screening.py
                 └─ WHERE e.ticker IN (<450 tickers>)
```

applied identically by `apply_keydevs_criterion`, `get_keydevs_events_count`,
`get_keydevs_events_for_tickers` and `fetch_all_keydevs_events`.

### Measured on STG (2026-08-28)

| | events | distinct tickers |
|---|---|---|
| `coreiq_company_events` (2016-01-01 → 2026-08-27) | 194,129 | 4,327 |
| `coreiq_companies` | — | 447 (450 rows) |
| Reachable by screening | 182,634 (94.1%) | 426 |
| **Invisible** | **11,495 (5.9%)** | **3,901** |

The loss is not spread evenly — it is concentrated where the business asked:

| Category | Total | Screened | Missing |
|---|---|---|---|
| **M&A Activity** | 20,753 | 11,062 | **9,691 (46.7%)** |
| **Layoffs & Restructuring** | 2,320 | 1,258 | **1,062 (45.8%)** |
| Bankruptcy | 155 | 92 | 63 (40.6%) |
| all others | — | — | < 300 each |

### What the 3,901 extra tickers are

3,900 of 3,901 have **only news-sourced** events (`AV News —…`, `YF News —…`) —
companies named in a headline about someone else (JPM, GOOG, BLK, WFC, UNP).
2,580 have exactly one event. `related_tickers` exists but is 100% NULL, so
there is no counterparty linkage to exploit instead.

The single genuine coverage gap is **QVCGA** (438 events, 433 SEC EDGAR, 2016→2026):
a **ticker mismatch**, not a missing company — `coreiq_companies` carries QVC Group
as `QVCPQ` (a preferred ticker with 0 events). `IMKT.A` vs the real `IMKTA` is the
same bug. Both are data fixes owned by the data team, not app changes.

---

## 2. Design

### 2.1 Universe — `get_all_companies_universe()`

Union of the Coresight master (verbatim) and one row per extra event ticker,
with name/exchange/country from the fallback masters. `@st.cache_data(ttl=3600)`.

`sector` is left **empty** for the extra companies on purpose: it is the column
the Industry criterion filters on, and its dropdown is the curated 79-value
Coresight taxonomy. Pouring Alpha Vantage labels in would mix two taxonomies in
one filter. Consequence, by design: an Industry or Geography criterion narrows
the screen back to Coresight names. Financial / key-dev / segment criteria keep
annotating (N/A, never drop) — `FILTERING_CRITERION_TYPES` is unchanged.

### 2.2 Dropping the IN list — `ALL_TICKERS` sentinel

When the universe *is* every event ticker, `WHERE ticker IN (…4,327…)` is a
no-op costing a ~40 KB SQL literal (and a 4,327-tuple `@st.cache_data` key).
`_keydev_ticker_clause()` returns `""` for the `ALL_TICKERS` sentinel.

Measured, M&A all-history, 500-row page:

| | |
|---|---|
| 450-ticker IN list | **5,689 ms** |
| no ticker clause | **305 ms** |
| COUNT, no clause | **260 ms** |

The widened screen is ~18× faster than the narrow one it replaces.

The page sends the sentinel only when the toggle is on **and** no narrowing
criterion is active (`_narrowing_criteria()`); with an Industry/Geography
criterion the working set is genuinely smaller and the IN list is correct.

### 2.3 Industry column

`coreiq_company_events` has **no industry column** — confirmed from the DDL. The
label has always come from a JOIN. `_keydevs_records_from_rows` already computed
an `industry` string and then **dropped it on the floor**: it was never added to
the record dict, so the column never reached the grid. That is now fixed, and the
JOIN is widened:

```sql
LEFT JOIN coreiq_companies c          ON c.ticker  = e.ticker   -- curated
LEFT JOIN coreiq_av_companies_all av  ON av.symbol = e.ticker   -- fallback
LEFT JOIN coreiq_sec_companies_all sc ON sc.ticker = e.ticker   -- name only
```

`av.symbol` was verified unique (0 duplicate symbols), so the extra joins cannot
fan out event rows.

`_keydev_industry_label()`: Coresight value wins and is tagged `(Primary)`;
otherwise the AV value title-cased and **untagged**. AV is a different
(Yahoo/Morningstar) taxonomy carrying junk (`NONE`, `SHELL COMPANIES`, `NULLIF`-ed),
so the tag is what stops it reading as a curated Coresight classification. The AV
*industry* itself comes from the per-symbol overview fetch and is accurate even for
reused tickers — it is the *name* that needs care (§7).

Coverage on M&A all-history: **19,568/20,815 rows get a name (94%)**,
**18,987 (91%) get an industry**.

### 2.4 UI — superseded, see §9

Originally an opt-in checkbox. Replaced 01-Sep by a mode rule: **Key Devs always
screens every ticker with events; every other mode stays on the Coresight master.**

---

## 3. Files changed

| File | Change |
|---|---|
| `app/data/screening_service.py` | `ALL_TICKERS`, `_keydev_ticker_clause()`, `_KEYDEV_COMPANY_JOINS/COLS`, `_keydev_industry_label()`, `get_all_companies_universe()`, `Industry` in `_keydevs_records_from_rows`, `all_tickers=`/`all_companies=` params |
| `app/pages/screening.py` | `_render_coverage_toggle()`, `_all_companies_on()`, `_screening_universe()`, `_narrowing_criteria()`, sentinel in `_render_keydevs_results`, status-bar wording |
| `tests/test_screening_keydevs_columns.py` | +15 tests (43 total) |

No DB writes. No schema change.

---

## 4. Verification (evidence, not assertions)

Local server `http://localhost:8501/screening`, STG DB, 2026-08-28.

**Before** — Key Devs · M&A Activity · All History:
`Working set: 450 companies` · `409 of 450 companies have data` ·
**`11,062 key development events found`**

**After, toggle ON** — same criterion:
`Working set: 4351 companies` · `3740 of 4351 companies have data` ·
**`20,753 key development events found`** (matches the raw DB count exactly)

Industry column rendered on the results card, both taxonomies distinguishable:

| Company | Industry |
|---|---|
| Vince Holding Corp. (NASDAQ:VNCE) | Apparel and Footwear Retail **(Primary)** |
| HSBC HOLDINGS PLC (HBCYF) | Banks & Financial Services **(Primary)** |
| Bristol-Myers Squibb Company (NYSE:BMY) | Drug Manufacturers - General |
| Coinbase Global Inc - Class A (NASDAQ:COIN) | Financial Data & Stock Exchanges |
| EQT Corp (NYSE:EQT) | Oil & Gas E&P |

Timing from `server-logs/server-log.log`: pipeline 3.9 ms (cached universe),
event page 1,542 ms, total page load 2,613 ms.

Tests: `43 passed` (`tests/test_screening_keydevs_columns.py`).

---

## 5. Known limits / follow-ups

1. **AG Grid header filter domain.** The Industry column's distinct-value filter
   sees only the loaded 500 rows, not all 20,753 — the same pre-existing
   limitation every paginated column here has. Fix is `filter_values` with the
   full domain, as done for the industries grid.
2. **AV reference data is not authoritative.** ~1,247 M&A rows still show a bare
   ticker, and stale mappings (MRNA, COR) surface wrong names. Untagged labels
   signal this, but it is worth a data-team pass.
3. **Ticker mismatches** `QVCPQ → QVCGA` and `IMKT.A → IMKTA` in
   `coreiq_companies` recover 438 real SEC-sourced events. Data-team change.
4. **21 covered companies have zero events** — 14 non-US (Samsung, LVMH, Ocado,
   Sainsbury…) never ingested; 5 delisted 2024-25 (Foot Locker, Nordstrom,
   Skechers, SpartanNash, Squarespace); 2 are the ticker bugs above.
5. **Watchlists** still resolve against the Coresight universe. Out of scope here.

---

## 6. Round 2 — default-on, filtered export, and timings

### 6.1 Toggle now defaults ON

`scr_all_companies` is seeded `True` in `_init_state`. Screening opens on the full
4,351-company universe. Turning it off restores the Coresight-only screen exactly.

### 6.2 Excel now exports what the grid is filtered to

**The bug.** The Excel button was rendered ABOVE the grid and only ever received
`(_tickers, _cats, _window)` — the query, never the grid's state. So it re-fetched
the entire result set and the user's column filters were invisible to it: filter to
"Divestiture/Asset Sale", click Excel, get all 20,753 rows.

**The fix.** Header and Excel button are now rendered into `st.empty()` placeholders
that are filled in AFTER the grid runs, so both can see the live filter model.

The model is read from the grid state — verified live on STG:

```
gridState = {'version', 'columnPinning', 'columnVisibility', 'columnSizing',
             'columnOrder', 'filter', 'pagination'}
gridState['filter'] = {'filterModel': {'Key Developments by Type': {'values': [...]}}}
```

`grid_filter_model()` reads that (accepting the flat `filterModel` shape too) and
returns `{column: {allowed display values}}`; an unreadable entry is skipped rather
than guessed at, so a filter we cannot parse never silently narrows an export.

`apply_grid_filter_model()` then re-applies it **to the full fetched set**, not to
the loaded page. This matters: the grid holds 500 rows of 20,753, so exporting the
grid's own filtered rows would hand back a slice of 500 when the filter really
matches thousands. Filtering the complete set gives every matching row. The
comparison uses the same display strings the grid filtered on, so the two can never
disagree, and it runs AFTER the criterion columns are merged so Industry/Country are
filterable too.

The header states which columns are filtered and that Excel carries every matching
row, so there is no ambiguity about what the file will contain.

### 6.3 Speed

Profiled against STG over the VPN. The decisive fact: **a query costs one ~250 ms
Azure round-trip almost regardless of what it returns.**

| query | time |
|---|---|
| COUNT alone | 258 ms |
| 500-row page, all columns incl. `situation` TEXT | 294 ms |
| 500-row page, 3 narrow columns | 280 ms |
| the same page with a 450-ticker `IN` list | 5,689 ms |

Columns and bytes are nearly free; round-trips and IN-list size are not. Two changes
followed:

1. **Drop the ticker IN list** when the universe is everything (Round 1) — the single
   biggest win, 5,689 ms → 305 ms.
2. **Overlap the count and the first page.** They are independent, so the count runs
   on a worker thread (with `add_script_run_ctx`, or it would run uncached) while the
   page query runs on the main thread: 258 + 294 = 552 ms sequential → one round-trip.

Rejected: folding both into one `COUNT(*) OVER ()` query. Measured **2,190 ms** — the
window function forces MySQL to materialize all 20k rows instead of stopping at
LIMIT 500. It is recorded here so nobody "optimizes" into it later.

### 6.4 Measured end to end (UI, cold session, STG DB over VPN)

| step | time |
|---|---|
| page load (server already up) | 0.38 s |
| switch to Key Devs | 0.25 s |
| Add Criteria — M&A Activity, All History (cold) | 4.66 s |
| **Show Results → header** | **1.84 s** |
| **Show Results → grid painted** | **2.53 s** |
| warm repeat (Show Results → grid) | 0.90 s |
| first page load after a server restart (Streamlit boot) | 10.7 s |
| Excel — full 20,753-row export (on click, off the render path) | 40 s |
| Excel — same export, pages already cached | 1.0 s |

Run-to-run variance on the VPN (±0.5 s) is larger than the round-trip the
overlapping saves, so the parallel fetch is evidenced at the query level (552 ms
sequential → one ~294 ms round-trip), not in the end-to-end number.

Grid columns confirmed: Company Name(s), Key Developments By Date, Key Developments
by Type, **Industry**, Key Development Headline, Summary, Key Development Sources,
Source Reference.

### 6.5 The bug the filtered export exposed — incomplete filter domain

Verifying 6.2 end to end surfaced a second, pre-existing defect. Unchecking
"Deal News" produced a 7,892-row file, but the arithmetic did not close:

```
unfiltered export        20,753 rows
Deal News                12,321 rows
expected after filter     8,432 rows
actual                    7,892 rows      <- 540 rows missing
```

Cause: the header filter builds its checkbox list from the rows the grid has
LOADED — one 500-row page. Of the 10 real M&A subtypes only 8 appeared;
`M&A Cancellation` (508) and `Change in Control` (32) were never offerable, so
they were absent from the selected set and the inclusion filter dropped them.
Invisible while the filter was display-only; a silent data loss once the export
honours it.

Fix: `get_keydev_subtypes_by_category()` publishes the complete taxonomy — one
462 ms `SELECT DISTINCT event_category, event_subtype` over the whole table (129
pairs), `@st.cache_data(ttl=3600)`, so it costs nothing per screen. It is handed to
the grid as `filterParams.values` via the new `filter_domains` argument, and
`DistinctValuesFilter.buildValues()` unions it with the loaded values — a value can
never become unlistable because the domain missed it. When the lookup fails the
domain is empty (not partial) and the filter falls back to today's behaviour.

**Residual limit:** only `Key Developments by Type` has a published domain. The
other columns' filters still see the loaded page only, so filtering `Industry` or
`Company Name(s)` and exporting can still omit rows whose value is not on the
loaded page. `Industry` (~200 values) is the obvious next one; the free-text
columns (Headline, Summary) have ~20k distinct values and are search-box territory,
not checkbox territory.

---

## 7. Which Alpha Vantage name to trust

`coreiq_av_companies_all` stores two names per symbol and **neither is reliably the
current issuer**. The rule is decided by `listing_status`:

| `listing_status` | Meaning | Winner |
|---|---|---|
| `Delisted` (166 of our 2,636 tickers, 103 names differ) | the ticker was **reused** — the listing row describes the dead former holder | **`overview_name`** |
| `Active` (2,470 tickers, 1,262 differ) | live listing; the bulk feed is fresher and the overview snapshot can predate a rename | **`company_name`** |

Evidence both ways:

| Ticker | Status | `company_name` (listing) | `overview_name` | Correct |
|---|---|---|---|---|
| MRNA | Delisted | Marina Biotech Inc *(delisted 2018)* | Moderna Inc | overview |
| COR | Delisted | CoreSite Realty *(delisted 2021)* | Cencora Inc. | overview |
| AMTD | Delisted | TD Ameritrade Holding | AMTD IDEA Group | overview |
| NEM | Active | Newmont Corp | Newmont Goldcorp Corp *(renamed away 2020)* | listing |
| FCX | Active | Freeport-McMoRan Inc | Freeport-McMoran Copper & Gold *(2014)* | listing |
| ACNT | Active | Ascent Industries Company | Synalloy Corporation *(renamed 2022)* | listing |

Preferring `overview_name` unconditionally was implemented first and **regressed the
Active rows** — hence `_AV_COMPANY_NAME` is a `CASE`, not a flat `COALESCE`. Each
branch falls back to the other column, so no source is ever dropped, and both carry
the literal string `'null'` plus bare-ticker placeholders (`overview_name` = `ACR-P-D`)
which `NULLIF` strips.

### Reference-data caveat (for the data team, not the app)

`coreiq_av_companies_all` is a **single snapshot dated 2026-03-24** — every row shares
the same `last_seen_at_utc`, so it has never been refreshed. Only **6,543 of 21,975
rows carry an industry at all** (~30%), which is why 1,828 M&A rows still show a blank
Industry. Neither affects the 11,124 Coresight-curated rows. Making industry and names
trustworthy for non-covered companies is a refresh of that table, not an app change.

---

## 8. Screening by event TYPE (2026-09-01)

**Report:** "we are not getting all the companies in the M&A category and M&A Closing."

**Root cause — two independent things, only one of them a bug.**

### 8.1 The bug: a sparse subtype was unreachable

Screening could only ever select a **category**. A category is dominated by one
subtype, so a sparse one inside it could not be isolated:

| | events | distinct companies |
|---|---|---|
| `M&A Activity` (whole table) | 20,753 | 3,740 |
| of which `M&A Closing` | **1,119** | **515** |
| `M&A Closing` **present in the newest 500 rows the grid loads** | 25 | **9** |

So filtering the grid's *Key Developments by Type* column to `M&A Closing` showed
**9 companies out of 515**. The data was there; the page was the limit.

**A "Key Development Types" criterion was built for this and then removed at the
user's request (01-Sep)** — it worked (`518 of 4374 companies` · `1,119 events`, vs 9
companies before), but the team prefers to filter from the results grid, which the
Excel export already honours.

**What actually ships instead**, and why it is enough for the export:

* The **Excel export applies the grid's filter to the FULL result set** (§6.2), so
  filtering the grid to `M&A Closing` and downloading yields all **1,119 events /
  515 companies**.
* The Type filter's checkbox list is populated from the **whole table** (§6.5), so all
  10 subtypes are selectable — including `M&A Cancellation` and `Change in Control`,
  which the loaded page never offered and which were silently dropping 540 rows.

**Residual, accepted:** the on-screen grid still holds only the newest 500 rows, so
filtering it to `M&A Closing` *displays* ~9 companies while the download holds 515.
Company counts read off the screen will understate a sparse subtype; the file is
correct. Re-introducing the criterion is the fix if that ever bites.

### 8.2 Not a bug: the directory is brands, not securities

The 646-row `worldwide_restaurant_coffee_directory.xlsx` has **no ticker column**,
and matching its names against all three masters resolves only **48 entries
(44 tickers)**. The other **598 have no ticker anywhere** because they are private
or non-US-listed:

* 233 US private chains — Blue Bottle, Caribou, Au Bon Pain, Biggby, 7 Brew…
* Chick-fil-A, Subway, Pret a Manger, Nando's — private, no security
* Greggs (LSE), Jollibee (PSE), Yoshinoya (TSE), Café de Coral (HKEX) — **confirmed
  absent from `coreiq_companies`, `coreiq_av_companies_all` AND
  `coreiq_sec_companies_all`**; our reference data is US-listed only

Of the 44 tickers: 25 have any event, 24 have `M&A Activity`, 13 have `M&A Closing`.
The 19 with no events are delisted/acquired and correctly silent — BKW and THI
(merged into QSR, 2014), BWLD (2018), PNRA (2017), RT (2017), BOJA (2019),
CKR (2010), FRGI (2023).

**Coverage is not being dropped:** all 37 Coresight `Restaurants` / `Coffee &
Beverage` companies have M&A data and appear (36 of 37 have `M&A Activity`; only
LKNCY has none).

### 8.3 Coverage toggle was silently resetting itself

Caught by the consolidated regression, not by hand. Unticking the coverage box and
then changing **Screen For** turned it back ON — the status line went `464` →
`4374` on the mode switch alone, so an explicit opt-out was undone without a word.

Cause: the checkbox owned the persisted key (`key="scr_all_companies"`). Streamlit
garbage-collects a widget key on any run where the widget is not drawn, and
`_render_screen_for` calls `st.rerun()` *before* `_render_coverage_toggle()` is
reached — so the key vanished and `_init_state` re-seeded it to its `True` default.

Fix: the checkbox owns `scr_all_companies_widget`; its value is mirrored onto the
plain `scr_all_companies` key that `_all_companies_on()` reads. A plain session key
is never collected, and `value=prev` re-seeds the widget after a collection.

Verified: `Universe: 464` before and after a mode switch; `421 of 464 companies have
data`; `11,293 key development events found` — all matching the DB exactly.

### 8.4 Note on the numbers in this document

The ETL runs daily, so every count here is a snapshot. Between 28-Aug and 01-Sep:
`coreiq_companies` 450 → **464**, event tickers 4,327 → **4,349**, widened universe
4,351 → **4,374**, `M&A Activity` 20,753 → **21,034** (Coresight-only 11,062 →
**11,293**). The first regression run failed on seven hardcoded constants for exactly
this reason; it now derives its expectations from the DB at run time. **Do not
hardcode live counts in a test here.**

---

## 9. Coverage is decided by the mode, not a checkbox (01-Sep-2026)

The opt-in checkbox is gone. The rule is now:

| Screen For | Universe |
|---|---|
| **Key Devs** | **always** every ticker in `coreiq_company_events` (~4,374) |
| Companies / Equities / People / … | `coreiq_companies` only (464) |

`_all_companies_on()` is now simply `scr_screen_for == "Key Devs"`, and
`_render_coverage_toggle()` plus the `scr_all_companies` session default are deleted.

Rationale: key-dev screening is about the *events*, and bounding it to the Coresight
master hid 9,741 of the 21,034 M&A Activity events (46%). There is no reading under
which a user wants that, so it is not a choice. Company-shaped criteria still behave
as before — Industry and Geography filter on `sector` / `country`, columns only
Coresight rows carry, so either one narrows a Key Devs screen back to covered
companies (4,374 → 20 for "Food Retail").

This also **deletes** the §8.3 widget-key reset bug rather than fixing it: with no
widget, there is no key for Streamlit to garbage-collect.

Verified in the UI: `Companies → 464`; `Key Devs → 4,374 (all companies with key
developments)` within 1s and stable; criterion `3,761 of 4,374 companies have data`;
`21,034 key development events found`; switching back → `464`, no leakage.

Note on one earlier red check: a run reported the Key Devs caption still showing 464.
That was the test reading the DOM mid-rerun — `_render_screen_for` calls `st.rerun()`
*before* the status bar renders, so the previous run's caption is still on screen for
a moment. A settle probe reads the correct 4,374 at +1s and holds it through +12s.
