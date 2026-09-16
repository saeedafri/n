# Screening — Key Developments: Coresight industry display & filtering

**Date:** 2026-09-04
**Area:** `/screening` → Screen For = **Key Devs**
**Trigger:** The data team added `primary_industry_coresight` to
`coreiq_sec_companies_all` (10,338 rows) and `coreiq_non_sec_companies_all`
(9,828 rows), tagged from the same curated Coresight list already used in
`coreiq_companies`. Request: use it to display and filter industry/sector in
screening Key Developments.

---

## 1. What was actually broken

Two separate defects, both rooted in "only `coreiq_companies` carries a
Coresight industry".

### 1.1 The Industry column did not exist (display)

`_keydevs_records_from_rows` had the `"Industry"` key **commented out**, parked
behind an explicit data-team question recorded in the code:

> Waiting on: is the Alpha Vantage (Yahoo/Morningstar) taxonomy an acceptable
> source alongside the curated `primary_industry_coresight`?

It was parked because 8.7% of event rows came back blank — the only non-Coresight
source was `coreiq_av_companies_all.industry`, populated for ~6,543 of 21,975
symbols, in a *different* taxonomy (`BANKS - REGIONAL` vs Coresight's curated list).

### 1.2 The Industry criterion silently filtered on coverage, not industry (filter)

`get_all_companies_universe()` set `sector: ""` for every event-only ticker, by
design at the time. `apply_industry_criterion` filters on `sector`. So on a Key
Devs screen of 4,392 event tickers, only the ~439 in `coreiq_companies` could
ever match — the other 3,953 were dropped as *"has no sector"*, not as *"is not
Food Retail"*. The old code documented the symptom: **4,374 → 20 for "Food Retail"**.

### 1.3 (Found during this work) The materialized universe never invalidated

`get_base_company_universe()` uses `materialized_or_build("screening_universe", …)`
with `{"table": "coreiq_companies", "signal": None}`. A `None` signal makes the
freshness check `COUNT(*)`. `coreiq_companies` has **no `updated_at`**, and
re-classifying a company is an `UPDATE` — the row count never moves, so the
snapshot never rebuilt. It was serving **13 retired industry values**
(`Chips`, `Grocery`, `Footwear`, `Drug Stores`, `Hardware`, …) that the live table
had already replaced: NVDA/AMD/INTC/QCOM were still `Chips` against a live
`Semiconductors`. Those companies were unfindable under their current industry.

---

## 2. Data verification (staging, before writing code)

| Check | Result |
|---|---|
| Column exists, both tables | `varchar(150)`, indexed (`MUL`) |
| Tagged rows | SEC 10,338/10,338 · non-SEC 9,828/9,828 (100%) |
| Distinct values | SEC 67 · non-SEC 47 · `coreiq_companies` 69 |
| **Taxonomy drift** | **Zero** — every SEC/non-SEC value already exists in `coreiq_companies`, so the existing dropdown needs no new options |
| `coreiq_sec_companies_all.ticker` unique | **Yes** (0 duplicates) → safe to JOIN directly |
| `coreiq_non_sec_companies_all.ticker` unique | **NO — 77 duplicates, 66 with *conflicting* industries** (`NANO` = `Software and Services` *and* `Technology`) |
| Tickers in both masters | 507, of which 428 disagree on industry |

**Coverage gained** (4,392 distinct event tickers):

| Source | Event tickers labelled |
|---|---|
| `coreiq_companies` only (before) | 439 (10.0%) |
| + SEC master | 2,630 |
| + non-SEC master | 1,817 |
| **Any Coresight source (after)** | **4,218 (96.0%)** |
| Alpha Vantage (the rejected fallback) | 2,380 |

**Row-level**, M&A Activity all-history (21,346 joined rows):
21,067 labelled = **98.7%**; 279 blank (1.3%), of which AV would recover only 84.

That answers the parked question by making it moot: the blank rate falls
8.7% → 1.3% on curated sources alone, so AV is dropped entirely.

---

## 3. Design decisions

### 3.1 Precedence: `coreiq_companies` > SEC > non-SEC
Curated coverage list wins, then the SEC master, then non-SEC. Implemented as a
`COALESCE` in SQL and, in the universe builder, by concatenating `base` verbatim
and only synthesising rows for tickers `base` does not carry.

### 3.2 non-SEC must be de-duplicated before every JOIN
77 duplicate tickers would multiply event rows. All joins go through one shared
constant, `_NON_SEC_INDUSTRY_SUBQUERY` (`GROUP BY ticker`, `MIN(...)` for a
deterministic pick). **Verified zero added fan-out**: M&A all-history is
21,346 rows / 21,284 distinct events both with and without the new join. (The
62-row gap is the pre-existing, intentional `coreiq_companies` shared-ticker
fan-out — JD / LULU / TSCO — collapsed downstream on `_event_id`.)

### 3.3 Alpha Vantage dropped from the industry label
The column is filterable with the Coresight dropdown. Mixing a second taxonomy in
would give a filter whose options and whose data disagree. Cost: 84 rows (0.4%)
stay blank that AV could have labelled. Blank renders empty, never `None`/`nan`.

### 3.4 The `(Primary)` suffix is removed
It existed to mark which of two taxonomies a value came from. With one taxonomy it
would only make the grid filter offer `Healthcare (Primary)` while the criterion
dropdown offers `Healthcare` — two spellings of one value. Bare keeps the grid
filter, the criterion dropdown and the DB in exact agreement.

### 3.5 Filtering needed no new filter code
`apply_industry_criterion` is **unchanged**. Populating `sector` in the universe is
the entire fix — the criterion simply has a real column to work with now. This is
also why the fix applies to every mode at once rather than only Key Devs.

### 3.6 Grid header filter gets the full domain
`keydev_industry_domain()` publishes all 69 values, so the Excel-style header
filter is not limited to the industries that happen to be on the loaded page — the
Excel export honours the filter model, so a missing value would silently drop rows
the user never deselected. It reads the DB (a 3-table `UNION`, ~275 ms, cached 1 h)
rather than reusing `get_all_industries()` so an industry added to the SEC/non-SEC
masters later shows up on its own. Warmed on the same background thread as the
subtype domain, so it costs nothing on render.

### 3.7 Materialization signal changed to a checksum
`SUM(CRC32(CONCAT_WS('|', ticker, primary_industry_coresight,
country_of_incorporation, exchange, exchange_acronym, name_coresight)))`.
Moves whenever any materialized column does; 465 rows, ~40 ms. `SUM` rather than
`GROUP_CONCAT` because `group_concat_max_len` truncates at 1 KB and would silently
stop noticing changes past that point. Verified: flipping one ticker's industry
changes the signal while `COUNT(*)` stays 465.

---

## 4. Files changed

| File | Change |
|---|---|
| `app/data/screening_service.py` | `_NON_SEC_INDUSTRY_SUBQUERY` (new shared constant); `get_base_company_universe` checksum signal; `get_all_companies_universe` selects `sector`; `_KEYDEV_COMPANY_JOINS` + `_KEYDEV_COMPANY_COLS` industry COALESCE; `"Industry"` un-parked; `_keydev_industry_label` simplified; `keydev_industry_domain()` (new) |
| `app/pages/screening.py` | import + publish `Industry` filter domain; warm it alongside the subtype domain; corrected the `_all_companies_on` docstring |

No DB writes. No schema changes. No new dependencies.

---

## 5. Rollout / risk

- **Read-only.** Two added LEFT JOINs and one cached domain query.
- **Perf:** page query 279 ms → 316 ms (+37 ms) on M&A all-history LIMIT 100.
- **Blast radius:** `sector` is now populated for event-only tickers. It was
  previously `""`, so anything that depended on it being empty would change — the
  only consumer is `apply_industry_criterion`, which is the intended fix.
- **Geography is unchanged** and still narrows a Key Devs screen back to Coresight
  coverage: `country_of_incorporation` had no equivalent backfill. Flagged, not bundled.

---

## 6. Verification (staging DB, real UI)

Local server `bash .claude/dev/run_local.sh` → **http://localhost:8501/screening**,
STG DB, Playwright headless. AG Grid renders inside the `st_aggrid` component
**iframe**, so all grid assertions are made against that frame.

### Service layer — 21/21
Universe 4,418 rows, **96.1%** with a Coresight industry (was 465/4,418 = 10.5%).
Industry criterion widened vs Coresight-only: Healthcare 430 vs 10, Restaurants
47 vs 36, Food Retail 17 vs 12, Technology 24 vs 1. No duplicate
(ticker, company_name) rows. Event rows 97.3% labelled, no `(Primary)` suffix,
every rendered value inside the published domain, export one row per event after
the existing `_event_id` dedupe. `merge_company_columns` with an active Industry
criterion yields exactly one `Industry` column, zero `N/A` overwrites, positioned
directly after `Company Name(s)`.

### UI scenarios — 12/12
| # | Scenario | Result |
|---|---|---|
| S1 | Key Devs, M&A / 365d, no Industry criterion | `Industry` column present; 13,781 events; 8 distinct industries on page; 1/27 blank; no `(Primary)`; blanks render empty (never `None`/`nan`) |
| S2 | + Industry = Healthcare | **Working set 430 companies** (was ~10); 1,342 events; every row Healthcare; 20 distinct companies incl. **AN2 Therapeutics, Aurora Cannabis, Biomerica, Crinetics, Brookdale** — all outside Coresight coverage, i.e. exactly the companies the old build dropped |
| S3 | Multi-select Restaurants + Food Retail | 188 events; both industries present; no other industry leaked |
| S4a | Grid `Industry` header filter domain | Offers **70 values** (69 industries + `(Blanks)`) against only **8** present on the loaded page — the `filter_domains` publication works |
| S6 | Companies mode regression | Industry criterion still returns rows, column correct (12 × Food Retail) |

### Pre-existing defect found while testing — NOT caused by this change, NOT fixed here

Toggling **any** AG Grid header filter does not stick, and then empties the Excel
export:

1. `GridUpdateMode.FILTERING_CHANGED` (grid config, pre-existing) makes every
   filter toggle round-trip to Streamlit, which **remounts the component iframe**.
2. The remounted filter GUI is rebuilt with `selected = null` — the user's
   selection is visually lost and the grid stops filtering.
3. But the Python side keeps the stale model, so the export applies it:
   `[KEYDEVS_EXPORT] grid filter applied: 1814 → 0 rows (columns: ['Industry'])`
   → `full.empty` → `return b""` → a **0-byte .xlsx**.

**Proven pre-existing by a control test** on `Key Developments by Type`, a column
that predates this work: identical behaviour — deselect-all leaves the grid at 27
rows, `(Select All)` reads `True` again on re-open, and Excel downloads 0 bytes.
So this is the shared grid/export plumbing, not the Industry column.

Worth fixing separately (own ticket): persist the filter model across the remount
(`setModel` from the returned model on re-render), and make
`_build_full_keydevs_workbook` return the unfiltered set — or surface an explicit
"filter matches no rows" message — instead of an empty file.

---

## 7. Incremental + shuffled criteria testing — and a stale-cache bug it found

Re-tested by applying criteria **one at a time**, with a full timed Show Results after
**every single one**, in **all six orderings** of {Key Devs, Industry, Geography}, plus a
mutation pass (remove / re-add / swap mid-sequence).

### Order independence — 36/36
All six orders converge on the identical result (**19 events, 8 companies,
`['Healthcare']`**) despite very different intermediate states:

| Order | after 1st | after 2nd | after 3rd |
|---|---|---|---|
| KD → IND → GEO | 1,814 ev | 175 ev | **19 ev** |
| KD → GEO → IND | 1,814 ev | 305 ev | **19 ev** |
| IND → KD → GEO | 2,103 ev | 175 ev | **19 ev** |
| IND → GEO → KD | 2,103 ev | 676 ev | **19 ev** |
| GEO → KD → IND | 55,089 ev | 305 ev | **19 ev** |
| GEO → IND → KD | 55,089 ev | 676 ev | **19 ev** |

18 incremental Show Results: **296 ms – 3,544 ms**, median ≈ 500 ms, 13 of 18 under 700 ms.

### BUG FOUND AND FIXED — criterion cache ignored upstream filters

Removing a criterion did not widen the results back:

```
add Key Devs → Industry=Healthcare → Geography=US     working set 8
remove Industry                                       still 8   ← wrong, should be 396
re-add Industry=Energy                                0 rows    ← wrong, should be 3
```
The criteria panel correctly showed 2 criteria, yet still reported
*"Geographic Locations: United States — 8 companies matched"*.

**Root cause.** `full_df` narrows as each *filtering* criterion is applied, so a
criterion's result depends on which filters ran before it. But the criterion cache was
keyed on `_criterion_fingerprint(criterion)` + the **constant** `universe_tickers`, which
cannot distinguish those cases. Geography's 8-row result — computed while Industry was
upstream — was then served after Industry had been removed. Everything downstream
(the counts, the grid, the export) inherited that stale narrowing.

Proved it was the cache, not the pipeline: calling `recompute_working_set` directly with
`[KD, GEO]` returned the correct **396** every time and was order-independent.

**Fix** (`app/data/screening_service.py`): key the cache on the tickers of the frame the
criterion is about to run against, instead of the constant universe.

```python
cache_key = (_criterion_fingerprint(criterion),
             frozenset(full_df["ticker"].values),
             keydev_details if ctype == "keydevs" else None)
```
The key now changes exactly when the upstream filters change and stays identical when
they do not, so the cache still hits on repeat renders (verified: entry count unchanged
across an identical re-run).

**Scope note.** This is pre-existing code, untouched by the industry work, and the
pipeline itself was already correct. It is fixed here rather than filed separately
because it silently corrupts the Industry criterion this change delivers — a user who
adds Industry and then removes it would keep the narrowed result.

### Mutation pass after the fix — 20/20

| Step | Result | Painted |
|---|---|---|
| Key Devs only | 4,418 companies · 1,814 events · 8 industries | 2,112 ms |
| + Industry=Healthcare | 430 · 175 ev · Healthcare only | 1,319 ms |
| + Geography=US | 8 · 19 ev | 818 ms |
| **remove Industry** | **396 · 305 ev · 9 industries** (widens correctly) | 2,052 ms |
| re-add Industry=**Energy** | 3 · 9 ev · Energy only, no stale Healthcare | 548 ms |
| remove + re-add Key Devs (365d) | 3 · 91 ev · Energy survives | 1,078 ms |
| Clear All, rebuild GEO → IND → KD | 3 · 91 ev · **identical to the forward path** | 622 ms |

---

## 8. Follow-up fixes (2026-09-05)

### 8.1 FIXED — Excel export could download a 0-byte file

`_build_full_keydevs_workbook` did `return b""` whenever the grid's filter model
matched no rows. That downloads a **0-byte .xlsx**, which Excel refuses to open and
which is indistinguishable from a crash. It now keeps the (empty) filtered frame and
builds a valid header-only workbook instead.

**Verified in the UI:** the same flow that previously produced 0 bytes now downloads
**526,661 bytes**, opens cleanly, and carries the Industry column (483 rows).

### 8.2 FIXED — People mode: `Sector` → `Industry`, always shown

People Screening labelled the column `Sector` and only rendered it when an Industry
criterion happened to be active — while the value was already the curated
`primary_industry_coresight` from the working set. Now named `Industry` and shown
unconditionally, so one name means one thing in Key Devs, Companies and People.

**Verified in the UI (3/3):** with *only* a Geography criterion, the People grid shows
`[… Year, Industry, Country, Salary …]`, no `Sector` column, values populated
(`Apparel and Footwear Retail`), painted in 4.8 s across 396 companies.

### 8.3 FIXED (second attempt) — the header filter now survives being used

`GridUpdateMode.FILTERING_CHANGED` remounts the component on every toggle and the
remounted `DistinctValuesFilter` is rebuilt with `selected = null`.

**First attempt — `api.setFilterModel` in `onGridReady` — was measured as ineffective**
and discarded: AG Grid instantiates a column's filter lazily (only when its menu is
first opened), so at grid-ready there is no filter instance to drive.

**What works** is threading the selection through the column's own `filterParams`,
which `DistinctValuesFilter.buildValues()` reads whenever the filter *is* created —
so the lazy-instantiation timing problem disappears:

* JS — `buildValues()` seeds `this.selected` from `filterParams.preselected` instead
  of always `null`; `init()` now calls `syncSelAll()` so the restored selection is
  reflected in the tri-state `(Select All)` box on mount.
* Python — the preset is read from `st.session_state[key]`, the component's **incoming**
  value, which already holds the toggle that caused this rerun. Reading it there (rather
  than from the `AgGrid()` return, which only lands after the grid is built) restores the
  filter on the SAME run the user made it, not one run late.

**Verified in the UI — 5/5:**
`(Select All)` reads **unchecked after the remount**; ticking `Healthcare` leaves the
grid showing **Healthcare only (27/27 rows)**; the Excel export is **46,970 bytes** and
contains **164 rows, all Healthcare** — grid, funnel and download finally agree.

### 8.4 NOT FIXABLE IN CODE — master coverage gap (data team)

~101 real-company tickers with key-dev events exist in **neither** new master, so they
render a blank Industry cell (314 rows, **0.16%** of 200,441). They are not "untagged"
— they are absent. Notable:

| Ticker | Company | Event rows |
|---|---|---|
| IPG | Interpublic Group | 12 |
| HES | Hess Corporation | 9 |
| ANSS | Ansys | 7 |
| JNPR | Juniper Networks | 5 |
| **K** | **Kellanova** | 3 |
| **KLG** | **WK Kellogg** | 2 |
| CFB | Crossfirst Bankshares | 2 |

The other 74 affected tickers are preferred shares / warrants / units
(`TWO-P-B`, `ASB-P-E`, `CSTA-U`), which have no company industry by nature.

### 8.5 Data-loss proof

**200,441 events in `coreiq_company_events` → 200,441 through the new joins.** Zero
lost, zero duplicated. M&A alone: 21,341 → 21,341 (old joins) → 21,341 (new joins).

---

## 9. Section 9 — "Does a criterion search all companies, or only `coreiq_companies`?"

Raised 2026-09-11: the business team screened **Restaurants + Coffee & Beverage** and
got **49 companies**, which looked like a Coresight-only result.

### 9.1 Answer: 49 is the wide-universe number

| Universe | Industry = Restaurants + Coffee & Beverage |
|---|---|
| `coreiq_companies` only | **37** |
| Full event universe (what the app uses) | **49** |

The extra 12 are companies the Coresight master does not carry — `BRCB` Black Rock
Coffee Bar, `REBN` Reborn Coffee, `CHA` Chagee, `BTBD` BT Brands, plus HK/TW numeric
listings. Verified in the real UI: those names render in the results grid.

### 9.2 Why not more than 49

143 tickers across the three masters carry those two industries. Only **49 have any
row in `coreiq_company_events`**; the other 94 have no key development at all, so
there is nothing to screen in Key Devs mode. The ceiling is event coverage, not
Coresight coverage.

### 9.3 Per-criterion universe (measured)

| Criterion | Universe it searches | Coverage |
|---|---|---|
| **Industry** | full event universe (4,476) | 95.9% have an industry |
| **Key Developments** | full event universe (4,476) | 3,829 have M&A rows |
| **Geography** | full event universe (4,476) | **58.4%** have a country |
| **Financial Information** | `coreiq_companies` (510) | by design — financials only exist there |

Geography is **not** Coresight-only (an earlier comment in `_all_companies_on()` said
so; corrected). It filters the same wide universe, but `country` comes from the Alpha
Vantage listing and is blank for 42% of event tickers, so a Country criterion drops
those rows.

### 9.4 BUG FOUND — stale materialized universe (465 of 510 companies)

`get_base_company_universe()` was serving **465 rows while `coreiq_companies` held
510**. 45 companies were missing from every screen: Allbirds, On Holding, Gildan,
Hugo Boss, CarGurus, Chefs' Warehouse, Mission Produce, Baozun, Arhaus, Boqii…

**Root cause — two independent caches, the inner one poisoning the outer:**

1. `materialized_or_build` computes its freshness signature **live from the DB**.
2. `_build_universe()` read through `CompanyRepository.get_companies_rows()`, which
   carries its own `@st.cache_data(ttl=3600)` snapshot.
3. DB changed → signature moved → rebuild fired → the build re-read the **stale
   1-hour snapshot** → the old 465-row frame was written under the **new** signature.

The on-disk meta proved it — signature `COUNT(*) = 510`, fingerprint `rows = 465`:

```json
{"signature": [["coreiq_companies", "1151802226184", 510]],
 "fingerprint": {"rows": 465, ...}}
```

Because the signature then matched, the cache looked fresh **permanently**. A restart
does not clear it; only deleting the file or another signature change does.

**Fix** (`screening_service.py`, `_build_universe`): `materialized_or_build` only calls
the build when the DB signal has moved, so the inner snapshot is by definition stale at
that point — drop it first.

```python
try:
    CompanyRepository.get_companies_rows.clear()
except Exception:
    pass
company_rows = CompanyRepository.get_companies_rows()
```

`screening_universe` was the only materialized cache building through an
`@st.cache_data` layer; the other six build straight from `execute_query_readonly`.

**Visible effect of the fix**

| | before | after |
|---|---|---|
| Base universe rows | 465 | **510** |
| Key Devs universe | 4,442 | **4,476** |
| Industry dropdown options | 69 | **72** |
| Industries returning 0 companies | 3 | **0** |
| Companies reachable across all industries | 465 | **4,294** |

The three dead dropdown options were `Food & Beverage`, `Specialty Retail` and
`Apparel & Footwear` — offered in the form but unable to match any row, because the
only companies carrying them were among the 45 missing. `Food & Beverage` now
returns 18.

**STG/PROD note:** the poisoned `screening_universe.pkl.gz` / `.meta.json` must be
deleted once per environment. The code fix prevents recurrence but cannot heal a file
that already claims to be fresh.

### 9.5 Verification (real UI, both orderings)

| Step | Result | Time |
|---|---|---|
| Key Devs baseline universe | 4,476 | — |
| **A1** Industry first | 49 | 1,241 ms |
| **A2** + Key Devs M&A [All History] | 49 · *44 of 49 have data* | 2,054 ms |
| **A3** Show Results | Industry column populated, **0 blank of 500 rows**; only the 2 selected industries present | 3,046 ms |
| **B1** Key Devs first (reverse order) | 4,476 · 3,829 have data | 2,052 ms |
| **B2** + Industry | **49** — identical to A, order-independent | 1,240 ms |
| **C1** Industry = Food & Beverage | **18** (was 0 before the fix) | 1,504 ms |

---

## 10. Section 10 — Excel-style column filter: "so many refresh" and wrong rows

Reported 2026-09-11 against this exact flow: Key Devs → Key Developments by Category
= M&A Activity / All History → Show Results → Industry column funnel → (Select All)
to clear → tick Restaurants, tick Coffee & Beverage.

### 10.1 Measured before (Playwright, with video + server rerun counting)

| Action | Reruns | Grid remounted | Popup |
|---|---|---|---|
| Open the Industry funnel | 0 | no | open, 74 values |
| Click (Select All) to clear | **1** | **yes** | **destroyed — 0 labels** |
| Tick Restaurants | — | — | popup was already gone |

`st_aggrid` forwards every `filterChanged` to Streamlit, Streamlit reruns, and the
rerun REMOUNTS the component — taking the open popup with it. One checkbox = one full
page round-trip, and the user has to reopen the funnel after every single tick.

### 10.2 Fix — client-side filtering, one sync at the end

`AgGrid(should_grid_return=...)` is a JsCode gate st_aggrid calls before returning
anything. Three coordinated pieces:

1. **`_SUPPRESS_FILTER_RERUN`** — returns `false` for `filterChanged` unless an
   explicit flush flag is set. Ticking values is now pure client-side AG Grid work.
2. **`DistinctValuesFilter.afterGuiAttached/afterGuiDetached`** — tracks whether the
   popup is open, and on close compares a snapshot; if the selection changed it sets
   `window.__agFilterFlush` and fires `filterChangedCallback()` once.
3. **Restore via `api.setFilterModel()` in `onFirstDataRendered`** — see 10.3.

| Action | Reruns | Remounted | Popup |
|---|---|---|---|
| Open funnel | 0 | no | open |
| (Select All) clear | **0** | **no** | **open, 74 labels** |
| Tick Restaurants | **0** | **no** | open |
| Tick Coffee & Beverage | **0** | **no** | open |
| Close the popup | **1** | yes (harmless, popup already closed) | — |
| Reopen the funnel | — | — | ticks remembered: Coffee & Beverage, Restaurants |

### 10.3 BUG — the grid silently dropped the filter on remount

With the popup closed there is no filter instance, so nothing read
`filterParams.preselected` and the grid re-rendered **unfiltered** while Python still
reported the filtered count. Measured: banner said *"6 of 500 shown rows"* while the
grid displayed **13 unrelated industries** (E-commerce, Utilities, Energy…). This is
the "results are also wrong" report.

`filterParams.preselected` only restores what the *popup* shows. The grid itself needs
`api.setFilterModel()`, and it must run in **`onFirstDataRendered`**, not `onGridReady`
— at grid-ready there are no rows and the call was previously measured doing nothing.
`setFilterModel` also forces the filter to be created, which is what defeats AG Grid's
lazy instantiation. The `filterChanged` it raises is swallowed by the gate, so
restoring cannot loop. After the fix the grid shows `['Restaurants']` only.

### 10.4 BUG — Excel exported unfiltered when clicked from an open popup

Introduced by 10.2: closing the popup is what flushes the filter to Python, and that
flush travels over the websocket. Clicking **Excel** straight from an open popup raced
it — **21,548 rows downloaded when only 610 matched**.

Fixed the way Excel itself behaves: the first click outside an open dropdown only
closes it, it does not activate what was clicked. The parent-document `mousedown`
handler now calls `preventDefault()`/`stopPropagation()` and eats the following
`click` when a popup was open. Same pass moved the listener to one-per-parent-document
with the live api republished on each mount — the old per-iframe guard re-added a
listener on every remount and left each previous one holding a dead api.

Verified: click 1 closes the popup and no download starts; click 2 downloads
**610 rows = 593 Restaurants + 17 Coffee & Beverage**, matching the DB exactly. With
no popup open, ordinary clicks are untouched ("Load more events" 500 → 1,000 works).

### 10.5 Remaining limitation — the filter only sees the loaded page (disclosed)

The column filter is client-side, so it can only sift the rows the grid holds — the
newest 500 of 21,548. Filtering Industry to Restaurants + Coffee shows **6 rows when
610 events really match**. The Excel export is unaffected (it re-applies the model
server-side over the full set — proven at 610).

The banner used to read "6 of 500 shown rows", which is true but reads as "6 matches".
It now names the gap outright:

> ⚠ This filter only searched the 500 events loaded so far — the other 21,048 of
> 21,548 have not been checked. Use "Load more events" to widen it, or download Excel,
> which applies this filter to all 21,548.

Making the on-screen grid exact would mean loading every matching row client-side
(~43 paged queries ≈ 13 s for this screen, far worse for the 200k-event all-category
case) or pushing generic column filters into SQL. That is a performance/product
trade-off, not a bug — left for an explicit decision.

---

## 11. Section 11 — Per-industry first page for unbounded Key Devs screens

### 11.1 Problem

The first page was a flat "newest 500", which is whatever happens to be busy. On
M&A / All History that carried **36 of 69 industries**, so the Industry column filter
could only ever surface 6 of the 610 rows matching Restaurants + Coffee & Beverage —
and Coffee & Beverage had **zero** rows on the page. Telling the user to press "Load
more events" to find their industry is not a design.

### 11.2 Design

| Screen state | First page |
|---|---|
| **Unbounded** — no Industry / Geography criterion (`_tickers is ALL_TICKERS`) | newest **8 per industry** (~526 rows, every industry represented) |
| **Narrowed** — an Industry criterion is applied | newest **500 within that industry** (unchanged) |

Once an industry is chosen the universe is already narrow, so the plain newest-first
page over those tickers is the right answer and needs no stratification.

"Load more events" changes meaning in the unbounded case: the cursor is a **row-number
depth**, not a date, so each click takes the next slice of *every* industry rather than
drifting into whichever industry happens to own the older events.

### 11.3 Query shape — why this one

`ROW_NUMBER() OVER (PARTITION BY <industry> ORDER BY event_date DESC, event_id DESC)`,
run over a **lean subquery selecting only `event_id`**, with the display columns joined
on afterwards — all in a single statement.

Measured on M&A / All History, k=8:

| Shape | Time |
|---|---|
| Window over the FULL row (all display columns inside it) | **11,860 ms** |
| Window over lean ids, two round trips | 3,088 ms |
| UNION ALL of one indexed `LIMIT k` per industry (69 subqueries, 45 KB SQL) | 8,046 ms |
| Window over lean ids, **single statement** ← chosen | **2,193 ms** |

Selecting the wide columns inside the window makes MySQL materialise every matching
row with all its text. Resolving industry once per ticker in a derived table was also
tried and did **not** help (5,758 ms vs 5,889 ms on the all-category case) — the cost
is the 150k-row sort, not the joins.

Cold vs warm is the buffer pool on `coreiq_company_events`: window-ids 5,672 ms cold,
767 / 742 ms on the next two runs. `@st.cache_data(ttl=900)` covers repeat views.

### 11.4 Measured in the UI (click → grid painted)

| Step | Time | Result |
|---|---|---|
| Unbounded first view (cold) | **3,075 ms** | 526 rows, **68 industries** |
| Same screen again (cached) | **547 ms** | — |
| Load more (next slice per industry) | 4,555 ms | 991 rows |
| Industry criterion applied | 2,952 ms | 610 found · newest 500, Restaurants + Coffee only |

Filtering the Industry column on the new page now returns **16 rows (8 + 8)** with both
industries present, against 6 rows and Coffee & Beverage entirely absent before.

Timings are click→painted over VPN to STG, where ~250 ms of every round trip is network.

### 11.5 Scope

Only "every category + All History" remains slow (~5 s cold for the window), because
that scans ~150k events. Every realistic combination is under 2 s for the ids query:
all categories / 365 days (the no-criterion default) 1,787 ms, 3 categories / all
history 676 ms, M&A / all history 733 ms.

---

## 12. Section 12 — Selecting industries in the GRID must dig as deep as the criterion

Reported 2026-09-11: picking Restaurants + Coffee & Beverage in the results grid's
Industry column still showed only 20-30 rows. Section 11 wired the deep load to the
**criterion form** only; the user does not care which control they used.

### 12.1 Fix

The grid's Industry selection now narrows the fetch exactly like the criterion does.
It is readable before any fetch is decided because closing the filter popup flushes
the model to Streamlit (§10.2), so on that run the grid's incoming value already
carries it. `apply_industry_criterion` is imported and reused rather than
re-implemented, so the two paths cannot diverge.

| Industry selected in the grid | Result |
|---|---|
| Restaurants + Coffee & Beverage | **500 of 610**, all on-industry |
| Financial Services | **500 of 4,120** |
| Website Builder (only 9 events exist) | **9 of 9** — "i.e. all of them" |
| cleared | back to the per-industry page, 526 rows / 68 industries |

### 12.2 BUG — a cleared filter could not be cleared

`_incoming_grid_filter()` returned `{}` both when the component had not reported yet
and when it reported *no filter*. The call sites read
`incoming or persisted or {}`, so an empty (cleared) model fell through to the
previously persisted one and the old filter came straight back — the grid stayed stuck
on the last industry picked. It now returns `None` for "has not reported", which is
distinct from `{}` for "reported, no filter". Affects every screening grid, not just
Key Devs.

### 12.3 BUG — the match count was one interaction stale

The banner read *"16 matching rows out of the 500 loaded"* over a grid correctly
showing all 500 (verified: AG Grid container 14,500px ÷ 29px row height = 500 rows).
`_kd_filtered_rows` came from `_kd_resp.data`, and the component only returns a payload
when the gate allows it — so its rows described the page as it was when the popup last
closed. It is now recomputed server-side with `apply_grid_filter_model(events_df, …)`,
which cannot drift from what was rendered.

### 12.4 Verified

Five-case matrix (baseline → two industries → big industry → tiny industry → cleared)
all pass; Excel still exports **610 rows = 593 Restaurants + 17 Coffee & Beverage**;
the filter interaction is still **0 reruns while ticking, 1 on close**, popup intact,
ticks remembered on reopen.

Note for future tests: AG Grid's "(Select All)" is **tri-state**. From an indeterminate
state a click SELECTS ALL — it does not clear. A test that assumes one click clears
will silently end up with "everything except one" selected and report a false failure.
