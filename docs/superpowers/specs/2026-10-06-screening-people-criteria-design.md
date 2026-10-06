# Screening: Remove Key Devs from Companies, Build Out People Screening

**Date:** 2026-10-06
**Status:** Approved design, not yet implemented
**Scope:** `app/pages/screening.py`, `app/data/screening_config.py`, `app/data/people_service.py` (new), `app/utils/cache_manager.py`

---

## 1. Problem

Two unrelated asks against the same page:

1. **Company Screening** offers a "Key Developments by Category" criterion that does not
   belong there — Key Devs has its own `Screen For` mode.
2. **People Screening** is a stub. It exposes 5 of the 16 fields the data already
   carries, has no people-level filters at all (only the inherited company filters),
   and refetches the whole dataset from the DB on every criteria edit.

### 1.1 Measured baseline (STG DB, 2026-10-06)

| Measurement | Value |
|---|---|
| `coreiq_executives_compensation` | 14,007 rows · 103 tickers · comp years 2013–2026 |
| YF `companyOfficers` (latest row per ticker) | 516 officers · 75 tickers |
| Full SEC table fetch, warm pool | **4,328 ms** |
| YF latest-per-ticker fetch (0.5 MB payload) | **1,599 ms** |
| JSON parse of all officers | 4 ms |
| **Total cold People render today** | **≈ 5.9 s, repeated on every criteria edit** |

The fetch is `@st.cache_data(ttl=21600)` keyed on the **ticker tuple**
(`repository.py:14455`, `:14514`), so narrowing or widening any Industry / Geography /
Financial criterion changes the key and pays the full 5.9 s again. This matches the
repo's documented cost model: the DB is packet-per-row bound, so 14.5k rows is the
cost, not the 0.5 MB of bytes.

### 1.2 Fields the data has and the UI throws away

`_render_people_results()` (`screening.py:5983`) surfaces Company, Ticker, Executive
Name, Position, Year, and up to 5 money columns. Verified-populated and unused:

| Column | Table | Populated |
|---|---|---|
| `option_awards` | `coreiq_executives_compensation` | 6,246 / 14,007 |
| `non_equity_incentive` | same | 11,312 / 14,007 |
| `all_other_compensation` | same | 12,650 / 14,007 |
| `form` (`DEF 14A` / `PRE 14A`) | same | 14,007 / 14,007 |
| `year` (filing year, ≠ `compensation_year`) | same | 14,007 / 14,007 |
| `blob_name` (path to the source proxy) | same | 14,007 / 14,007 |
| `confidence_score` | same | 14,007 / 14,007 |
| `age`, `yearBorn` | YF payload | 541 / 916 officers |
| `exercisedValue`, `unexercisedValue` | YF payload | 916 / 916 officers |

**Bug found while measuring:** `get_yf_all_compensation` (`repository.py:14543`) does
`if total_pay is None: continue`. Only 313 of 916 officers have `totalPay`, so **603
officers (66%) are dropped entirely** even though they carry a name, title, age and
exercised/unexercised value. People Screening silently under-reports non-SEC companies.

### 1.3 Executive emails do not exist in this database

Every `%email%` column in the schema belongs to `coreiq_portal_users`,
`coreiq_user_roles`, `coreiq_page_access_control`, the alert-preference tables, or an
audit log — all **our own portal users**, never an executive. There is no exec contact
table and no email field on any people-bearing table.

**Decision:** ship the `Email` column through the schema, grid and Excel export,
populated by a `LEFT JOIN` on a `coreiq_people_contacts` table that does not exist
yet. It renders blank today. When the data team lands a source, no app change is
needed. Deriving `first.last@domain.com` from the IR website was considered and
**rejected** — fabricated contact data in a research portal is worse than an empty
column.

---

## 2. Part A — Remove Key Developments from Company Screening

### 2.1 Current structure

`_CRITERIA_OPTIONS` (`screening.py:596`) is one flat list of 4 entries, and
`_render_criteria_palette()` (`:3553`) renders all of them for all three modes
(`Companies`, `Key Devs`, `People`) — the palette is explicitly shared
(`:7454`).

### 2.2 Change

Make the mode→criteria mapping a single constant rather than branching at each call
site:

```python
_CRITERIA_OPTIONS = [
    ("Industry Classifications",     "industry",   True),
    ("Geographic Locations",         "geography",  True),
    ("Financial Information",        "financial",  True),
    ("Key Developments by Category", "keydevs",    True),
    ("People Attributes",            "people",     True),
]

# Which criteria each Screen For mode may add. One definition so a mode cannot drift.
_MODE_CRITERIA = {
    "Companies": ("industry", "geography", "financial"),
    "Key Devs":  ("industry", "geography", "financial", "keydevs"),
    "People":    ("industry", "geography", "financial", "people"),
}
```

Three call sites read it:

1. **`_render_criteria_palette()`** — renders only the current mode's entries.
   `st.columns(len(...))` already sizes off the list, so the row stays even.
2. **`_render_add_new_criteria_inline()` (`:2365`)** — `type_options` is currently the
   hardcoded list `["Industry", "Geography", "Financial", "Key Developments"]`. This is
   a **second** way to add a Key Devs criterion in Companies mode, reachable from any
   criterion's detail panel. It must derive from `_MODE_CRITERIA`, otherwise hiding the
   palette button only moves the problem.
3. **Saved-screening load (`:2668`)** — `st.session_state.scr_active_criteria =
   list(cr["criteria_list"])` replaces the stack wholesale with no mode check. Filter
   to the mode's allowed types and `st.warning` naming what was dropped.

### 2.3 Why the saved-screening path is the only real leak

Switching `Screen For` already calls `_reset_criteria()` (`:3546`), which empties
`scr_active_criteria`. So a stale `keydevs` criterion can reach Companies mode **only**
via a Saved Screening created while in Key Devs mode. Left unhandled, Companies results
would render a Key Developments column that the user cannot edit or remove, because no
palette button for it exists any more.

### 2.4 What is deliberately NOT removed

Credit Ratings and Store Counts are offered as checkboxes inside the Key Developments
form (`:4542`, `:4550`) **and** inside the Financial form (`:4037-4058`, as
`hidden`/`parent_idx` child criteria). Removing the Key Devs palette button from
Companies mode therefore orphans nothing — verified before designing this. The only
capability Companies mode loses is the Key Developments category filter itself, which
is what was asked for.

`_render_criterion_form()`'s dispatch (`:4833`) keeps routing `keydevs`,
`biz_segments`, `geo_segments` and `additional` to `_render_keydevs_form()` unchanged,
for Key Devs mode and for edit-prefill of segment criteria.

---

## 3. Part B — People data layer

### 3.1 New module

`app/data/people_service.py`. A new module rather than growing `screening_service.py`,
which is already 6,723 lines.

Public surface:

```python
get_people_universe() -> pd.DataFrame    # materialized, whole dataset
classify_role(title: str) -> str         # title text -> role bucket
apply_people_criterion(df, criterion) -> pd.DataFrame   # pure pandas, no DB, no st.*
build_people_summary(criterion) -> str   # criterion card label
```

### 3.2 Materialized frame schema

One row per (ticker, person, year). ~14.5k rows.

| Column | dtype | Source |
|---|---|---|
| `ticker` | category | both |
| `executive_name` | string | `executive_name` / officer `name` |
| `email` | string | `coreiq_people_contacts` LEFT JOIN — **NULL today** |
| `title` | string | `position` / officer `title` |
| `role` | category | derived — `classify_role(title)` |
| `is_former` | bool | derived — `\bformer\b` in title (1,446 SEC rows, 10.3%) |
| `year` | Int16 | `compensation_year` / `fiscalYear` |
| `salary` | float | SEC |
| `bonus` | float | SEC |
| `stock_awards` | float | SEC |
| `option_awards` | float | SEC |
| `non_equity_incentive` | float | SEC |
| `all_other_compensation` | float | SEC |
| `total_compensation` | float | SEC |
| `total_pay` | float | YF `totalPay` |
| `exercised_value` | float | YF `exercisedValue` |
| `unexercised_value` | float | YF `unexercisedValue` |
| `age` | Int8 | YF `age` |
| `year_born` | Int16 | YF `yearBorn` |
| `source` | category | `'SEC'` / `'YFinance'` |
| `filing_form` | category | SEC `form` |
| `filing_year` | Int16 | SEC `year` |
| `filing_blob` | string | SEC `blob_name` |
| `confidence` | Int16 | SEC `confidence_score` |

Company / Industry / Country are **not** stored here. They are joined at render time
from `scr_working_df`, so the people cache does not have to rebuild when a company is
reclassified.

### 3.3 Build function

Two queries, **no ticker `IN` list**:

```sql
-- 1. whole SEC table
SELECT ticker, executive_name, compensation_year, position, form, year,
       salary, bonus, stock_awards, option_awards, non_equity_incentive,
       all_other_compensation, total_compensation, blob_name, confidence_score
FROM coreiq_executives_compensation;

-- 2. latest YF overview row per ticker
SELECT ticker, payload_json FROM (
    SELECT ticker, payload_json,
           ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ingested_at DESC) AS rn
    FROM coreiq_yf_company_overview
) z WHERE rn = 1;
```

Dropping the ticker `IN` list is the repo's own documented 18× win, and it makes the
cache universal — one copy serves every criteria combination instead of one per ticker
tuple.

The YF loop **keeps officers with no `totalPay`** (fixing §1.2), writing NULL money and
retaining name / title / age / exercised values.

`optimize_frame_memory` + `audit_frame_numbers` are applied as `screening_service.py`
does for `screening_universe`.

### 3.4 Materialization and freshness

```python
_SOURCES = [
    {"table": "coreiq_executives_compensation",
     "signal": ("SUM(CRC32(CONCAT_WS('|', ticker, executive_name, compensation_year, "
                "position, salary, bonus, stock_awards, option_awards, "
                "non_equity_incentive, all_other_compensation, total_compensation)))")},
    {"table": "coreiq_yf_company_overview",
     "signal": "SUM(CRC32(CONCAT_WS('|', ticker, ingested_at)))"},
]

return materialized_or_build("people_universe", _build_people, _SOURCES,
                             clear=lambda: get_people_universe.clear())
```

Two scars this avoids on purpose, both already paid for in this repo:

- **The signal is a CRC32 checksum, not `COUNT(*)`.** Re-extracting a proxy filing is an
  `UPDATE` — the row count does not move, so a count-based signal would serve stale
  compensation indefinitely. Same reasoning and same shape as
  `screening_service.py:291`.
- **`_build_people` queries the DB directly and reads no `st.cache_data` function.**
  Reading a TTL'd snapshot inside a `build_fn` writes stale rows under a *fresh*
  signature, which is unrecoverable without deleting the `.pkl.gz` by hand.

### 3.5 Role buckets

`position` holds 2,048 distinct free-text values
(`'Senior Vice President, Chief Financial Officer'`, `'Former Chief Executive Officer
Sharon McCollam President and Chief Financial Officer'`), so it cannot be a dropdown.
`role` is a first-match-wins regex bucket; the raw text stays filterable via
`title_contains`.

Order is significant — `President and Chief Executive Officer` must bucket as CEO, so
CEO is tested before President.

| # | Bucket | Pattern (case-insensitive) |
|---|---|---|
| 1 | CEO | `chief\s+exec\|\bceo\b` |
| 2 | CFO | `chief\s+financ\|\bcfo\b` |
| 3 | COO | `chief\s+operat\|\bcoo\b` |
| 4 | CIO/CTO | `chief\s+(info\|tech\|digital\|data)\|\bcio\b\|\bcto\b` |
| 5 | General Counsel | `general\s+counsel\|chief\s+legal\|legal\s+affairs\|\bcounsel\b` |
| 6 | Chair | `\bchair` |
| 7 | President | `\bpresident\b` |
| 8 | Other C-Suite | `\bchief\b` |
| 9 | Investor Relations | `investor\s+relations` |
| 10 | Company Secretary | `\bsecretary\b` |
| 11 | Board / Exec Director | `executive\s+director\|executive\s+board\|corporate\s+director\|representative\s+director` |
| 12 | General Manager | `general\s+manager\|\bgm\b\|managing\s+director\|\bmd\b` |
| 13 | Executive Officer | `executive\s+officer` |
| 14 | EVP/SVP/VP | `\b(e\|s)?vp\b\|vice\s+president` |
| 15 | Head of Function | `\bhead\s+of\b\|\bdirector\b\|\bmanager\b\|\bcontroller\b\|\bofficer\b` |
| 16 | Other / Unknown | fallthrough; blank title → `Unknown` |

Validated against all 14,523 real title strings:

| Pool | Bucketed | `Other` | `Unknown` (blank title) | Bucketed, blanks excluded |
|---|---|---|---|---|
| SEC, 14,007 rows | 94.8% | 1.6% (226) | 3.6% (511) | **98.3%** |
| YF, 516 officers | 99.0% | 1.0% (5) | 0% | **99.0%** |

The SEC `Other` residue is junk extraction (`'Executive'`, a stray sentence
fragment), not a missing bucket. The buckets and the regexes live in
`screening_config.py` per that file's "all additions should go here" rule.

---

## 4. Part C — "People Attributes" criterion

`_render_people_form()` in `screening.py`, following `_render_keydevs_form()`'s shape
(`st.expander` → `st.form` → Add Criteria / Cancel → `_add_criterion()` → `st.rerun()`).

### 4.1 Criterion dict

```python
{
    "type":            "people",
    "roles":           ["CEO", "CFO"],        # [] = any
    "title_contains":  "merchandising",       # "" = any
    "name_contains":   "",                    # "" = any
    "include_former":  False,                 # False drops is_former rows
    "money_metric":    "total_compensation",  # None = no money filter
    "money_operator":  "Greater Than",        # from OPERATORS
    "money_value1":    5.0,                   # $mm, scaled by DB_SCALE
    "money_value2":    None,                  # Between only
    "years":           [2024, 2025, 2026],    # [] = all
    "age_min":         None,
    "age_max":         None,
    "sources":         ["SEC"],               # [] = both
    "filing_forms":    ["DEF 14A"],           # [] = all
    "display_col":     "Total Compensation",
    "summary":         "CEO, CFO · Total Compensation > $5mm · FY 2024-2026",
}
```

Reuses `OPERATORS`, `OPERATOR_SQL` and `DB_SCALE` from `screening_config.py`. Money
inputs are in `$mm` like every other financial criterion; `apply_people_criterion`
multiplies by `DB_SCALE` before comparing, because the people tables store raw dollars.

The 10 filterable money metrics: `salary`, `bonus`, `stock_awards`, `option_awards`,
`non_equity_incentive`, `all_other_compensation`, `total_compensation`, `total_pay`,
`exercised_value`, `unexercised_value`.

### 4.2 Filter semantics

`apply_people_criterion(df, criterion)` is pure pandas — no DB handle, no `st.*` call
(an `st.*` call inside a cached path fails only on cache *hits*, which this repo has
already been bitten by). Each field is an `AND`; values inside one multiselect are an
`OR`. Multiple `people` criteria stack as `AND`, matching how the other criteria types
compose.

A `NaN` money value never satisfies a money comparison, so a person with no
`option_awards` is excluded by an `option_awards > 0` filter rather than counted as
zero.

### 4.3 Criterion card

`_render_criteria_detail_panel()` (`:1865`) gets `"people": "People Attributes"` added
to its label map (`:1910`) and a `_render_people_detail()` branch alongside
`_render_keydevs_detail` (`:1940`). This is what makes a People criterion editable,
removable, and savable as a Saved Screening like every other criterion.

---

## 5. Part D — Results grid

`_render_people_results()` (`:5983`) rewritten. New flow:

1. Ticker universe from `scr_working_df` as today — Industry / Geography / Financial
   keep narrowing the company set, so "CFOs at US specialty-retail companies with
   revenue > $5bn" still works. No company criteria → the full people universe.
2. `get_people_universe()` — a disk read, no SQL.
3. `df[df["ticker"].isin(tickers)]`, then fold every `type == "people"` criterion
   through `apply_people_criterion`.
4. Join `Company`, `Industry`, `Country` from the working set.
5. Drop money columns that are entirely empty for the current result (preserves today's
   behaviour, so a SEC-only result shows no `Total Pay` column).
6. Format money as `$x,xxx,xxx`; blank, never `0`, for missing.
7. Sort Company → Year desc → Total desc, as today.

### 5.1 Column order

Company · Ticker · Executive Name · **Email** · Title · **Role** · Year · Industry ·
Country · Age · Salary · Bonus · Stock Awards · Option Awards · Non-Equity Incentive ·
All Other Comp · Total Compensation · Total Pay · Exercised Value · Unexercised Value ·
Source · Filing Form · Filing Year · **Source Filing** (blob path, plain text) ·
Confidence

`Executive Name` stays the pinned column.

### 5.2 Grid wiring

`_render_filterable_results_grid(...)` with:

- `pinned_column="Executive Name"`
- **No `link_columns`.** `filing_blob` is shown as plain text — the blob path is the
  audit trail from a number back to its proxy filing. It is deliberately *not* a
  clickable link: the grid's link renderer only emits an `<a>` when the value starts
  with `http` or `/` (`screening.py:6669`), the container is private, and
  `azure_blob.py` exposes only `download_blob_to_memory` — there is no SAS-URL helper.
  Making 14k paths clickable would mean either writing a SAS helper or streaming every
  proxy through `media_url`, both outside this scope. Logged as a follow-up in §8.3.
- **`filter_domains` carrying the COMPLETE domain** for `Role`, `Year`, `Source`,
  `Filing Form`, `Industry`, `Country`. Not optional: the grid paginates, its header
  filter otherwise offers only values present on the loaded page, and because the Excel
  export honours the filter model, a partial domain silently drops real rows from the
  download. This repo has already shipped that bug once (36 of 75 industries hidden).

The existing standalone "Filter by Year" selectbox above the grid is kept as a quick
filter — redundant with the criterion, but zero-cost now that filtering is in-memory.

### 5.3 Excel export

Unchanged path (`_render_excel_js_download` via `utils/media_url`), exporting the
filtered frame with all the new columns. Keeping `media_url` matters: base64-ing a
workbook into a single websocket message is what caused the screening
`RangeError`/"Connection error" this repo already fixed.

---

## 6. Part E — Performance

| Path | Today | Target |
|---|---|---|
| People cold render, disk copy present | 5.9 s | **< 0.5 s** (one pickle read) |
| Any criteria edit | 5.9 s | **< 50 ms**, 0 DB round-trips |
| First render after a deploy, no disk copy | 5.9 s | 5.9 s once, then cached |

`people_universe` is added to `start_background_warmup()`
(`cache_manager.py:729`) so the first visitor after a deploy does not pay the build.

### 6.1 Measurement protocol

Timings are read from `log_timing` lines in `server-logs/server-log.log`, not from
wall-clock impressions. Before any perf claim, check for orphaned
`chrome-headless` processes — a killed Playwright run leaves browsers reconnecting at
~34 reruns/min and fakes slowness.

---

## 7. Testing plan

### 7.1 Automated check

`app/data/test_people_service.py` — one file, asserts only, no framework:

1. `classify_role` returns the documented bucket for ~20 real strings taken from the DB,
   including the ordering traps (`'President and Chief Executive Officer'` → CEO,
   `'Senior Vice President, Chief Financial Officer'` → CFO, `'Former Chief Executive
   Officer Sharon McCollam President and Chief Financial Officer'` → CEO) and blank →
   `Unknown`.
2. `is_former` is True for `'Former Senior Vice President, Chief Financial Officer'`,
   False for `'Senior Vice President, Chief Financial Officer'`.
3. Money filter boundaries on a hand-built frame: `Greater Than` is strict,
   `Greater Than or Equal To` includes the boundary, `Between` is inclusive both ends,
   `NaN` never matches.
4. `DB_SCALE` is applied — a `money_value1` of `5.0` matches a row holding `5_000_001`
   and not one holding `4_999_999`.
5. Multiple `people` criteria stack as AND.
6. `apply_people_criterion` opens no DB connection (monkeypatch
   `db_manager.execute_query_readonly` to raise).

### 7.2 UI verification (required before claiming done)

Launch `bash .claude/dev/run_local.sh` → `http://localhost:8501`.

| # | Check | Evidence |
|---|---|---|
| 1 | Companies palette shows 3 buttons, no Key Developments | screenshot `/screening` |
| 2 | Key Devs palette still shows its 4 buttons and the mode works | screenshot |
| 3 | People palette shows Industry / Geography / Financial / People Attributes | screenshot |
| 4 | Detail-panel "Add New Criteria" dropdown offers no Key Developments in Companies mode | screenshot |
| 5 | Loading a Key-Devs-era Saved Screening in Companies mode drops the criterion and warns | screenshot |
| 6 | Credit Ratings / Store Counts still reachable from the Financial form | screenshot |
| 7 | People grid renders every column in §5.1; Email present and blank | screenshot |
| 8 | Each new filter changes the row count in the expected direction | row counts before/after |
| 9 | A YF-only company shows officers with no `totalPay` (regression from §1.2) | screenshot |
| 10 | Excel export contains every visible column and honours the filter | downloaded file |
| 11 | Source Filing column shows the blob path as text | screenshot |
| 12 | Criteria edit does not re-query the DB | `server-log.log` shows no people SQL on the second edit |

Every report of these results names the running URL and page path so the user can
re-test.

### 7.3 Data integrity probes

- `people_universe` row count == 14,007 SEC rows + 516 YF officers = **14,523**, asserted
  at build time with a warning on drift, so a silent drop is visible in the log.
- No duplicate (ticker, executive_name, year, source) rows — the YF `ROW_NUMBER`
  guard must hold, since `coreiq_yf_company_overview` carries 57–86 rows per ticker and
  dropping that guard multiplies every officer ~80×.
- Spot-check 3 tickers' totals against the source proxy filing.

---

## 8. Rollout

1. `people_service.py` + `screening_config.py` constants + the test file. No UI change
   yet, so nothing user-visible can break.
2. Part A (Key Devs removal) — small, independent, independently verifiable.
3. Part C + D (form, criterion card, results grid).
4. Warmup registration.
5. Full §7.2 pass with screenshots, then hand the user a copy-paste list of touched
   files grouped by type for the STG copy.

**No commits, no pushes, no deploys** — per this repo's hard rule, the user commits.

**No DB writes.** `coreiq_people_contacts` is specified here for the data team; this
work does not create it, and the `email` column tolerates its absence. Everything else
is read-only.

### 8.1 Deliberate ceilings

Marked with `ponytail:` comments at the relevant line:

- `email` ships empty until a source table exists. Upgrade: the data team lands
  `coreiq_people_contacts`, the LEFT JOIN starts returning values, no app change.
- `role` is a regex bucket, not a classifier. 98.3% / 99.0% of non-blank titles bucket
  cleanly on real data. Upgrade: an LLM pass if the residue ever matters.
- The frame rebuilds whole, with no incremental delta path. 14.5k rows makes a delta
  path not worth the complexity.

### 8.2 Known data limits to surface, not fix

- SEC compensation covers **103 of ~450** companies. Blank People results for a
  company are a coverage gap, not a bug.
- `confidence_score` and `extraction_notes` show the extraction was imperfect — 511 rows
  have no `position` at all (`'missing_position'`). Those bucket as `Unknown`.
- `compensation_year` is the proxy's own fiscal label and can differ from `filing_year`.
  Both are shown rather than reconciled.

### 8.3 Follow-ups, out of scope here

- **Clickable source filing.** Needs a SAS-URL helper in `azure_blob.py` (the container
  is private and only `download_blob_to_memory` exists). Worth doing once, because
  Company Filings would use it too.
- **`coreiq_people_contacts`** for the `Email` column — a data-team deliverable. The
  app side is ready for it on day one.
- **Non-SEC compensation coverage.** 103 of ~450 companies have proxy compensation and
  75 have YF officers. Widening that is an ingestion question, not an app one.

---

## 9. As shipped — corrections to this spec, found during implementation

The design above was written from probes of the source tables. Building it turned
up four things the probes had not shown. The sections above are left as written;
this section is what is actually true in the code.

### 9.1 The people frame is 6,727 rows, not 14,523

`coreiq_executives_compensation` holds 14,007 rows covering only **6,418 distinct
person-years**. A proxy's Summary Compensation Table restates the three prior fiscal
years, and `PRE 14A` / `DEF 14A` are the preliminary and final versions of the same
proxy, so one executive-year is stored up to **6 times** (CLX / Laura Stein / 2018,
all six carrying the identical $2,106,867).

**This was a live defect:** every one of those copies was rendered as its own grid
row, so People Screening showed each executive **2.2x on average**.

`_collapse_restatements()` keeps one row per (ticker, person, year, source). The
copies agree on money — they differ in 386 of 6,418 groups, and then only by rounding
($26,248,995 vs $26,248,997) — but disagree on `position` in 2,480 groups, because
later proxies abbreviate the title and eventually prefix it with "Former". The
contemporaneous filing therefore wins, which also keeps `is_former` true to the year
being reported: Angela Ahrendts is flagged Former for FY2019, the year she left, and
not for FY2014-2018.

### 9.2 One person, several spellings

Keying on the raw `executive_name` still left the same person as two rows:
`Mark D. Papermaster` / `Mark Papermaster`, `Scott D. Lipesky` / `Scott Lipesky` —
176 (ticker, year) groups, 363 rows, 5.7% of the SEC side.

The extractor also glues junk onto names: footnote markers (`Richard A. Galanti 8`,
`Fabrizio Freda ( 1 )`) and zero-width characters (`Jeffrey Davis ﻿`).

So the frame carries `clean_person_name()` (strips that junk) and `person_key()`
(first + last token, middle initials dropped) and dedups on the key.

**The guard that matters:** Apple pays several SVPs *identically* — Bruce Sewell, Dan
Riccio and Eddy Cue all show $22,807,544 for 2016. Equal pay is therefore NOT a
duplicate signal, and only name identity is used. A test asserts those three survive.

Where a name-variant pair disagrees on money it is always a truncated parse, never
two people — ACI 2020 filed Robert Dimond at $7,146,900 and "Robert B. Dimond" at
$6,900 — so the larger figure wins, on pay rounded to the nearest $1,000 so that
restatement rounding still ties and the filing-year preference keeps picking the
title.

Final: **6,727 rows** (6,225 SEC person-years + 502 YF officers), 2.9 MB.

### 9.3 YF officers dropped: 343, not 603

§1.2 counted officers across unordered rows. Against the latest row per ticker the
real figure is **516 officers, of which 173 have `totalPay`** — so the old
`continue` dropped **343**, and all of them are now kept.

### 9.4 `disabled=` cannot depend on another widget in the same `st.form`

The compensation inputs were specified as disabled until a metric is chosen. Widgets
inside an `st.form` do not rerun the script until submit, so that condition is
evaluated against the **previous** render: the user picks a metric and the Value box
stays greyed out, with no way to type a threshold. The E2E caught it. All four
compensation inputs are now always enabled and `_validate_people_values()` does the
job on submit.

### 9.5 Two other defects fixed on the way

- **`fillna("")` on a categorical column raises.** `role` / `source` / `filing_form`
  are categorical (that is what holds the frame at 2.9 MB); filling them without an
  `astype(object)` first threw `TypeError: Cannot setitem on a Categorical with a new
  category ()` and killed the whole results render.
- **A multi-line value inside the criterion-card template renders as a code block.**
  The template is run through `textwrap.dedent`; an injected value with its own
  newlines leaves mismatched indentation, dedent strips nothing, and Streamlit's
  markdown renders the still-indented first line as code — a literal
  `<details class='criterion-details'>` in a grey box. The People card builds its
  HTML on one line.
- The criterion card no longer prints a company match count for a People criterion.
  The company pipeline legitimately reports `with_data=0` for one (it filters people,
  not companies), and "0 of 558 companies have data" read as a failure.

### 9.6 Measured result

| | Before | After |
|---|---|---|
| People render, warm | ~5,900 ms, on every criteria edit | **15–38 ms** |
| Materialized disk read | — | **0.02 s** |
| Cold build (empty cache, once) | 5,900 ms | 5,545 ms, then warmed on boot |
| Rows shown per executive-year | up to 6 | 1 |
| Columns in the grid / export | 10 | **22** |

Verified on `http://localhost:8501/screening` against the STG DB:
CEO+CFO = 2,189 records / 156 companies; + Total Compensation > $10mm = **670 records
/ 89 companies**, matching an independent pandas computation over the same frame.
Companies and Key Devs modes were driven to results in the same session with no
regression.
