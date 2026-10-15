# People Screening — category first, then its filters (2026-10-15)

Source: meeting brief of 2026-10-15. Previous rounds: `2026-10-09-management-changes-8k-502-design.md` §1–12.

## 1. What was there

People mode (`pages/screening.py`, Screen For = People) offered Industry, Country of
Incorporation and one **People Attributes** form. That form held everything at once:
Role, Title / Name contains, "Compensation metrics to show", a second "Compensation
filter" metric picker with Min / Max, years, ages, and two checkboxes ("Show
management changes?", "Show executive moves?"). Show Results always built the
executive pay grid. The two boxes added the 8-K sections. Every section read every
People criterion.

Data per section:

| Section | Dataset | Loader |
|---|---|---|
| Management Changes | 8-K Item 5.02 events | `management_changes.get_management_changes()` (materialized `management_changes_v4`) |
| Executive Changes / Movements | the same events, `moved_from` set; pay before/after from the pay frame | same + `get_people_universe()` |
| Management Remuneration | proxy (SEC) + officer (YFinance) pay | `people_service.get_people_universe()` (materialized `people_universe_v4`) |

## 2. What changed

### 2.1 Category selector
A **Category** radio sits at the top of People Attributes. It is outside the
`st.form` for the same reason Key Devs' Period radio is: a widget inside a form
reports only on submit. Options and labels live in `screening_config.PEOPLE_CATEGORIES`:

- `management_changes` → **Management Changes**
- `executive_moves` → **Executive Changes / Movements**
- `remuneration` → **Management Remuneration**

Executive moves stay a separate category. The model already distinguishes them:
a move is a derived pairing (departure at A, then appointment at B naming A),
with its own columns (From / To company, pay change). It is not a row type of
management changes.

### 2.2 Filters per category

| Filter | Changes | Moves | Remuneration |
|---|---|---|---|
| Role / Title, Title contains, Name contains | ✓ | ✓ | ✓ |
| Period: Time frame (Key Devs presets) or Date range | ✓ (Announced date) | ✓ (Joined date) | — |
| Compensation metrics + Min / Max ($mm) | — | — | ✓ |
| Compensation years, Min / Max age | — | — | ✓ |

Industry and Country of Incorporation remain separate criteria. They narrow the
company scope for every category.

### 2.3 Hidden filters never reach a query
- `_people_criterion_from_widgets` stores only the chosen category's fields.
  A Min typed under Remuneration before switching to Management Changes is not
  saved (verified in the UI).
- `people_service.people_criteria_for(criteria, category)` routes criteria to
  sections. A section is filtered only by its own criteria, so a CEO pay filter
  does not narrow management changes (verified: 44 changes before and after
  adding it).
- **On a category switch:**
  - Role, title and name are kept: the same widgets are shown for every category.
  - The other category's specific inputs are kept in the browser but never saved.
  - Opening a criterion with Edit re-seeds the whole form from that criterion.

### 2.4 Only the needed data is loaded
`_render_people_results` renders only the categories that criteria name:

- A management-changes screen never calls `get_people_universe()`.
- A remuneration screen reads the events only through `_fill_age_from_filings`,
  which fills the Age column from ages stated in filings, as before.

If no People criterion exists, Show Results asks the user to pick a category
instead of building the pay grid.

Page open still loads no people data. The process-start warm-up in
`utils/cache_manager.py` is unchanged.

### 2.5 Date range
- **Reuse:** `_render_keydevs_period_radio` and `_collect_keydevs_date_fields`
  (new `default_timeframe` argument), and `_apply_keydevs_date_fields` for
  validation. A start date after the end date gives "Start date must be on or
  before end date."
- **Semantics:** `people_service.period_mask` follows the Key Devs contract.
  - N days means on or after today − N, the same as `event_date >= CURDATE() - INTERVAL N DAY`.
  - A date range is inclusive at both ends.
  - All History, or no period at all (pre-category criteria), keeps everything.
  - Dates are calendar days; filings carry no time of day. "Today" is the server's date.
- **Saved values:** the criterion stores the preset (`days`), not resolved dates,
  so a saved "Last 7 Days" stays rolling.
- **Defaults** (`PEOPLE_DEFAULT_TIMEFRAME`):
  - Management Changes: Last 7 Days, the everyday check named in the meeting.
  - Moves: All History. There are about 90 moves in ten years, so a week is
    almost always empty.
- **Empty state:** "none announced in this period … Latest on file: <date>".
  - On 2026-10-15 the newest 8-K 5.02 event is 2026-10-01, so Last 7 Days is
    empty. That is an ingestion lag in `coreiq_company_events`, not app logic.

### 2.6 Compensation: one control
Before, two pickers overlapped:
- "Compensation metrics to show"
- "Compensation filter" with its own Min / Max

Now there is one **Compensation metrics** multiselect next to Min / Max:
- The chosen metrics are shown in the results.
- Min / Max keeps executives whose value for any chosen metric is inside the range.
- The default pair is Total Compensation (US proxies) and Total Pay (non-US officer
  listings). A person only has one of the two, so "any" keeps both sources in the
  screen. A single metric behaves exactly as the old one-metric filter did.

Details:
- Units stay $mm × `DB_SCALE`.
- Bounds are inclusive, and a missing value never matches.
- New key: `money_metrics`.
- Pre-category criteria keep `money_metric` / `money_operator` and the old path in
  `_apply_money_filter`.

### 2.7 Store-count location grouping
**Before:**
- People Screening has no store-count filter.
- Screening's Store Counts (Companies mode → Company Information → Additional
  Data) shows only the latest total.
- The location grouping lives in Market Data → Additional Data → "Stores by Country"
  (screen and Excel), through `repository.group_countries_by_continent`.
- It used a hand-kept continent map that disagreed with the geographic-segment
  hierarchy: Mexico, Central America and the Caribbean under North America; the
  Gulf states and Türkiye under Asia.

**Now:**
- `repository.continent_of` returns the country's **Sub-region** from
  `geo_hierarchy.json` (the business's Countries Mapping workbook, the same source
  as the Geographical Segments Region / Sub-region filters). Countries with no
  node go under "Other".
- Group order: Americas, EMEA, APAC; North America first; then A–Z.
- Totals use `store_total_label`:
  - "Total <sub-region>" for each group
  - "Total <region node>" when the footprint stays inside one region, e.g. "Total AMER (Americas)"
  - "Total Worldwide" otherwise, including partial splits as before
- `COUNTRY_TO_CONTINENT` / `CONTINENT_ORDER` are gone.
- `scripts/build_store_count_geo_workbook.py` derives both from the hierarchy.

**Visible label changes:**

| Before | Now |
|---|---|
| "Total (North America)" | "Total North America (NORAM)" |
| Mexico under North America | Mexico under "Latin America (LATAM)" |
| "Australia & New Zealand" | "Oceania (ANZ)" (the workbook's name) |

**Not changed:** `store_count_sources.roll_up_continents` still writes a
`by_continent` field into the store-count cache files. Nothing reads or displays
it, and changing it means rebuilding those files.

### 2.8 Found while testing
- **Spinner cut off at 6 s.** `.cs-al-ov` carries a CSS failsafe that fades it at
  6 s; it was meant for the short page loader. It also hid the sticky loader, which
  has its own end-of-run script and a 600 s cap. A cold People screen showed no
  spinner from 6.5 s until its grid drew at 16 s. `#cs-sticky-loader.cs-al-ov` is
  now exempt (`components/loading.py`).
  - After the fix: the spinner stays up to 12 s on a cold pay load; on Key Devs it
    stays for the whole 20 s cold run and hides 0.25 s after it ends.
- **Silent empty section.** When the management-changes frame came back empty, the
  section returned without a word. It now says the data is not available right now.
- **Not fixed (flagged): Filing links can vanish.** `_filing_units_by_accession`
  runs a ~100 s DISTINCT over `coreiq_filing_metrics_v5`, against a 120 s read
  timeout. On 2026-10-15 13:28 it timed out. `execute_query_readonly` returns `[]`
  on error, so the frame was materialized with 0 of 9,365 events linked. The data
  links 9,013 when the query completes. The Filing column stays missing until
  `MAX(event_id)` changes. Fix options: page the query by ticker, or use
  `execute_query_readonly_raising` so a failed lookup is never materialized.

## 3. Files

| File | Change |
|---|---|
| `app/data/screening_config.py` | `PEOPLE_CATEGORIES`, category keys, `PEOPLE_DEFAULT_TIMEFRAME` |
| `app/data/people_service.py` | `people_criteria_for`, `period_mask`, `money_metrics` range, category summary |
| `app/pages/screening.py` | category radio, per-category widgets / validation / criterion, section routing, card details |
| `app/data/repository.py` | hierarchy-based grouping, `store_total_label`, `store_group_order` |
| `app/pages/market_data.py` | subtotal labels via `store_total_label` (screen + Excel) |
| `app/components/loading.py` | sticky loader exempt from the 6 s CSS failsafe |
| `scripts/build_store_count_geo_workbook.py` | imports the new grouping |
| `app/data/test_people_service.py`, `tests/test_store_count_grouping.py` | tests |

No DB schema change and no DB writes.

## 4. Testing

**Unit tests:**
- `.venv/bin/python app/data/test_people_service.py`: ALL PASS, including the new
  category-routing, period-mask, range-over-metrics and summary tests.
- `.venv/bin/python app/data/test_management_changes.py`: 14 passed.
- `.venv/bin/python -m pytest -q tests/test_store_count_grouping.py tests/test_geo_hierarchy.py`: 38 passed, 1 skipped.

**UI test** (Playwright, localhost:8501 testing and :8503 STG checkout; 558-company
working set). Each count was checked against an independent pandas calculation.

| Step | Result |
|---|---|
| Form, Management Changes | Category, Period, Role, Title, Name, Timeframe = Last 7 Days; no pay inputs |
| Form, Executive Changes / Movements | same inputs, Timeframe = All History |
| Form, Management Remuneration | Role, Title, Name, Compensation metrics, Min, Max, years, ages; no Period |
| Min 5 typed under Remuneration, then saved as Management Changes | card shows only "Management Changes · Last 7 Days" |
| Last 7 Days | 0 changes; message names 2026-10-01 |
| Edit → Last 30 Days | category kept; 25 changes; Excel 25; second click identical |
| Date range 30 Sep → 1 Sep | validation error |
| Date range 1–30 Sep | 44 changes |
| + Remuneration: CEO, Min 25 / Max 1 | "Min cannot be greater than Max." |
| + Remuneration: CEO, $1–20mm | 316 executives (Excel 316); changes still 44 |
| + Moves, All History | 88 moves (Excel 88); three sections; second click identical |
| Spinner | shown on every click, hidden after 0.8–1.2 s |
| Page errors | none |
| Market Data → COST → Additional Data | NORAM, LATAM, Europe, East Asia, Oceania (ANZ) subtotals, then "Total Worldwide"; Excel identical |

**Test-harness note:** typing a date into the masked date input sometimes drops
keystrokes or reverts. Key Devs' form behaves the same (shared widget code).
Picking from the calendar works every time.

## 5. Rollout
Applied to the testing repo and copied to market-data-stg:
- Four files copied; `repository.py` and `market_data.py` 3-way merged with no conflicts.
- STG has no copy of `scripts/build_store_count_geo_workbook.py`.

Nothing is committed. Saved screenings from before this change keep working
(`people_criteria_for` maps them to what they showed).

## 6. Open decisions
1. **Labels:** the meeting once said "revenues". This build uses "Management
   Remuneration" and "Executive Changes / Movements". Confirm the wording.
2. **Store-group names:** the workbook's "Oceania (ANZ)" replaced "Australia &
   New Zealand", which the business chose on 16 Jul. Confirm, or ask the data team
   to rename the node; one place changes both screens.
3. **8-K events lag:** the newest event is 2026-10-01, which leaves Last 7 Days
   empty. The ingestion schedule belongs to the data team.

## 7. Round 2 (2026-10-15 PM) — management changes from every source

**Question:** does People read management changes from all sources, as Key Devs does?

**Answer:** it did not.

- Key Devs selects `coreiq_company_events` by `event_category` only, so every source.
- People read only `source = 'SEC EDGAR 8-K' AND headline LIKE '%Item 5.02%'`.

Management Changes category, by source:

| Source | Events | In People before | Now |
|---|---|---|---|
| SEC 8-K (Item 5.02, incl. Governance "Board Director Change") | 6,457 + 2,594 board | yes | yes |
| SEC 6-K (35 foreign issuers) | 1,402 | no | yes: 149 events from 118 filings |
| Alpha Vantage news (721 outlets) | 2,784 | no | yes |
| Yahoo news | 70 | no | yes |

News: 557 events were read; after folding repeats, 400 remain as their own rows and 234 reports appear under "Also Reported By".

**Why not every row**
- **News:** about one item in five is a change at the tagged company. The feed also files:
  - insider trades
  - earnings previews
  - webcast notices
  - other companies' appointments (Lumenci under QCOM, Northern Tool under SNBR, Imperva under ORCL)
- **6-K:** the text is stored cut at 2,000 characters, mostly the cover page. 633 of the 1,402 are NONOF filings, mostly share-repurchase and transaction reports mis-tagged as management changes.
- **Data-team items:** store the full 6-K text, and tighten the news tagging.

**How news is read** (`management_changes.parse_news`)
- The headline patterns:
  - "<Company> names/appoints/taps <Person> as <Title>"
  - "<Person> appointed/named <Title> of <Company>"
  - "<Person> to step down / retire / exit"
- The headline's subject must be the ticker's own company.
- When the headline gives a role but no person ("Five Below names COO"), the summary is read with the 8-K extractor; the result is marked Low confidence.
- **Measured on samples:** about 28 of 30 correct in a random sample; 17 of 17 correct in the last 30 days.

**Duplicates** (`_fold_repeats`)
- Same ticker, person and change within 30 days count as one change.
- The filing row is kept, and other reports are listed in **Also Reported By**.
- Repeats between news outlets fold the same way.

**Grid changes**
- New **Source** column (filterable) and **Also Reported By** column.
- "Filing" became **Source Link**: our Company Filings page for an 8-K; sec.gov for a 6-K; the article for news.

**Frame:** `management_changes_v5`.

**Filing-unit lookup**
- The query (~100 s, 120 s read timeout) timed out twice today. `execute_query_readonly` returned `[]`, and the frame was saved with 0 links.
- It now uses `execute_query_readonly_raising`, retries once, and then fails the build rather than saving a link-less frame.
- Per-ticker batches were tried and took 559 s. The real fix is an index on `coreiq_filing_metrics_v5 (doc_type, ticker, filing_accession, storage_year, filing_unit)`. That needs your OK, as DDL.

**People items still in Key Devs**

| Key Devs subtype | Count | Recommendation |
|---|---|---|
| Executive Changes – CEO / CFO / COO / Other C-Suite / Other | 6,457 | now in People |
| Management Change (6-K) | 1,402 | now in People |
| Executive News | 2,854 | now in People (as read) |
| Board Director Change (8-K 5.02) | 2,594 | already in People |
| Director Nomination (8-K 5.08) | 22 | not people: shareholder-proposal deadlines |
| Executive Compensation (DEF 14A) | 3,954 | people: CEO pay (the Pay-versus-Performance option) |
| Shareholder Vote (5.07) | 3,808 | company; includes director elections, decision needed |
| Activist Communication | 201 | borderline (board-seat demands), decision needed |
| Bylaw Amendment, Rights Modification, Activist / Institutional Stakes | — | company / investor level; stay in Key Devs |

**Tests**
- `app/data/test_management_changes.py`: 16 passed, including `test_news_headlines` (real headlines, including the wrong-company rejections) and `test_news_repeats_fold_into_filing`.
- **UI** (localhost:8504 testing, localhost:8505 STG checkout), People → Management Changes:

| Filter | Changes | Companies | Notes |
|---|---|---|---|
| Last 7 Days | 4 | 4 | all news |
| Last 30 Days | 42 | 33 | Excel 42; Source column present |
| Last 30 Days, CFO | 2 | 2 | |
| All History | 7,590 | 438 | |

  All counts equal the independent pandas calculation. No page errors.
