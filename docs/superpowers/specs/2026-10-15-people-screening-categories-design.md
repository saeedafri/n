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

## 8. Round 3 (2026-10-15 evening): STG failure, form changes, five Key Devs categories

### 8.1 Root cause of "Management changes could not be loaded" (STG, 18:23–18:27)

From the STG server log:

- `management_changes_v5` was absent on `/home/mdp-cache`, so the first People run built it live.
- The build ran `_filing_units_by_accession`, the one-shot (ticker, accession) → (storage_year, filing_unit) lookup over `coreiq_filing_metrics_v5`. It hit the 120 s read timeout twice (18:25:08, 18:27:08), and the Round 2 change made that raise.
- So the user waited 4 minutes and got the error.

Measured on STG:

| Lookup | Time |
|---|---|
| Full lookup | 100–120 s |
| 60-ticker chunks | 40–57 s each (≈7 min for all) |
| One ticker + accession | ~1 s warm, 5.5 s cold |

The bulk lookup cannot be made to fit, so it was removed.

**Fix:** the frame no longer carries `filing_year` / `filing_unit` (now `management_changes_v6`).
- An 8-K's Source Link is `/company_filings?ticker=T&doc_type=8-K&accession=A`.
- `company_filings.py` resolves the accession with `utils/filing_units.find_filing_by_accession` when the link is opened. That is one ticker's range of `idx_v5_filings_dropdown`, cached 6 h, and uses a raising read so a timeout is never cached as "not found".
- The events read in the build also raises now, instead of materializing an empty frame on a timeout.

No index or DDL is needed any more. The index in §7 is withdrawn.

### 8.2 Form

- **Category:** a dropdown (`st.selectbox`), not a radio.
- **Title contains / Name contains:** removed from every category. Old saved criteria that carry them are still honoured.
- **Role / Title:** kept for Management Changes, Executive Changes / Movements and Management Remuneration. Not shown for event categories, which have no person.
- **Management Remuneration:** "Compensation years" is two dropdowns, *Compensation year from* / *to*. "Earliest" / "Latest" mean open-ended. They are stored as the `years` list, so the pivot and filter are unchanged. The card reads "FY 2022–2024".

### 8.3 Key Devs items moved into People (manager's list)

`PEOPLE_EVENT_CATEGORIES` in `screening_config.py`:

| Dropdown value | event_category / subtypes | Rows (STG) | Default period |
|---|---|---|---|
| Executive Compensation (Proxy Filings) | Proxy/Governance / Executive Compensation (DEF 14A) | 3,954 | Last 30 Days |
| Shareholder Votes | Governance / Shareholder Vote | 3,808 | Last 30 Days |
| Activist Communications | Investor Activism / Activist Communication | 201 | Last 90 Days |
| Director Nominations | Governance / Director Nomination | 22 | All History |
| Layoffs & Restructuring | Layoffs & Restructuring / Headcount Reduction, Restructuring, Exit/Disposal Costs | 2,617 | Last 30 Days |

- Each category has its own frame, `people_events_<category>_v1` (`people_service.get_people_events`).
  - The frame is materialized because the read is 5–35 s cold: it is row-bound, and `idx_cat_date` is used.
  - Its signal is MAX(event_id) for that event_category.
  - The boot warm-up builds all five.
- The grid shows Company, Ticker, Date, Type, Headline, Source and Source Link. The Excel export has the same columns, minus the link.
- Key Devs still offers these subtypes; removing them there is a separate decision.

### 8.4 /logs page

Causes and fixes:

| Cause | Fix |
|---|---|
| `[PERSIST][…] rebuilt … changed=False` logged at WARNING: 44,048 of the day's lines | Logged only when changed or over 5 s (~560/day) |
| `get_log_stats` counted lines by reading the whole live log (≤25 MB on the /home SMB share) on every render | Estimated from size |
| `get_log_content` read the FIRST 500 KB, so the "recent" view showed the oldest lines | Now reads the tail |
| Download built 80 MB of plain text, read every segment, then cut it | Reads newest-first up to the cap and is gzipped (`.log.gz`): 80 MB → 3 MB in 0.1 s |

**Left:** `_ensure_segment_refresh_state_table` logs a "Duplicate column classifier_version" ERROR every 15 min. It is harmless, and in `screening_service.py`, which another session is editing.

## 9. Round 4 (2026-10-15 night): four categories out, proxy pay in, directors in Management Changes

The user's decisions on the round-3 categories:

| Category | Decision | Done |
|---|---|---|
| Shareholder Votes | Not needed | Removed |
| Activist Communications | Not needed | Removed |
| Executive Compensation (Proxy Filings) | Its pay belongs in Management Remuneration | Removed as a category. The proxies' Summary Compensation Tables now feed Management Remuneration (below) |
| Director Nominations | Treat as management changes, with names | Removed as a category (see below). Directors are a Role in Management Changes |
| Layoffs & Restructuring | Keep | Kept, re-tested |

The People dropdown is now: Management Changes, Executive Changes / Movements, Management Remuneration, Layoffs & Restructuring.

### 9.1 Why proxy pay is not in XBRL

The SEC requires XBRL tags on only one compensation table, Pay versus Performance (Reg S-K Item 402(v), in proxies since 2023). It tags the CEO's total and "compensation actually paid", and the AVERAGE of the other named executives, for five years.

The Summary Compensation Table is plain HTML: salary, bonus, stock, options, non-equity incentive, pension, other and total, for each executive and year.

edgartools 5.16 (pinned in requirements.txt) shows exactly this split:
- `filing.obj()` on a DEF 14A returns a `ProxyStatement`. It has the headline figures only.
- Example, WMT 2026: CEO Doug McMillon $29,240,930 total and $36,488,421 actually paid; other-executive average $25,904,488.
- `has_individual_executive_data` is False, and `named_executives` is empty for WMT, PFGC and TEAM.

So the free route to detail is the HTML table. edgartools' `to_dataframe()` mangles these tables: headers land in body rows, spanned columns are duplicated, and cells come back as tuples. `proxy_pay` uses edgartools' parsed cells (text, colspan, rowspan) and lays them out on a grid itself.

### 9.2 `app/data/proxy_pay.py`

**Proxy list**
- Source: `coreiq_company_events` rows with subtype "Executive Compensation (DEF 14A)": 3,954 proxies for 411 tickers. `source_ref` is the sec.gov viewer URL.
- The newest 3 proxies per company are read. That is up to 5 fiscal years, since each proxy restates three.
- The order is round-robin: every company's newest proxy first, then the second, then the third.

**Parse**
- Candidate `<table>`s are cut out of the raw HTML by regex: text must contain salary, total, a 20xx year, and stock/option awards or bonus. Only those are parsed, not the whole 150–500-table proxy.
- The first candidate that yields named executives wins.
- Layouts handled:
  - Name and title on separate rows (WMT).
  - Name and title in one cell (PFGC).
  - Name column not first (JCI, APH).
  - A stray year row above the data (GOOGL).
  - Three years stacked in one cell (MNST).
  - Oldest-first tables.

**Fetch**
- Plain `requests` with an SEC User-Agent, 0.2 s between requests.
- Not edgartools' downloader: it caches every /Archives document on disk forever (221 MB for 65 proxies, about 4 GB for the set).

**Store**
- `(rows, done_accessions)` in the materialize cache dir as `proxy_pay_v1`.
- Progress is saved every 25 proxies.
- A proxy with no readable table is recorded as done, so it is never refetched.
- A network failure is not recorded, so it is retried on the next run.

**Never on a request**
- `read_proxy_pay()` returns the disk copy, or an empty frame.
- `refresh_proxy_pay()` runs at the end of `_warm_people_screening` (the boot warm-up thread). On a warm cache it costs one events query plus any new proxies.

**Merge** (`people_service.add_proxy_pay`, applied inside `get_people_universe`)
- Proxy rows are added only for company-years that `coreiq_executives_compensation` lacks.
- The data team's table stays the source where it has data.
- The added rows go through the same restatement collapse (`_collapse_restatements`) and column types (`_finish_frame`).
- After a refresh that read anything, the warm-up clears `get_people_universe`.

**Accuracy** (two random samples of 30 proxies each, latest proxy)
- Parsed: 28/30 and 27/30. The failures are MELI's two-table layout, LOW, VFC, LEVI, CTHR (a 2017 proxy) and LAD.
- Totals against the data team's table where both have the person-year: 135/139 and 97/102 equal.
- Every disagreement checked against the filing text was the data team's row that was wrong:
  - ACI: Sankaran's 2024 total is $15,240,511 in the filing; the table has $7,882,248.
  - DLTR: Richard McNeely's pay is filed under Jeffrey Davis.
  - CFR: Jimmy Stead carries Coolidge Rhodes's numbers.
- These are data-team items; the app still prefers their table where it has the company-year.

### 9.3 Directors in Management Changes

- "Director Nomination" is 8-K Item 5.08: it sets the deadline for shareholder director nominations. 22 events, and none names a person (SMCI, PRTS, PMNT, CTHR …). It is not a director change.
- Named director changes are 8-K Item 5.02(d) (new director elected) and 5.02(b) (director retires, resigns or declines re-election). These are already extracted: `management_changes_v6` has 3,083 board events, every one with a person.
  - 1,098 Appointment, 551 Director Election, 649 Resignation, 356 Not Standing for Re-election, 272 Retirement, …
  - Examples: NKE, Alexandre Arnault, 2026-09-16; ACI, Margaret Whitman, 2026-09-09.
- **Root cause of "directors missing":** the Role / Title multiselect offered only the title buckets (`PEOPLE_ROLES`). management_changes tags directors `role = "Board"`, a value no bucket produces, so picking any role dropped every director.
- **Fix:** `PEOPLE_ROLE_BOARD = "Board"` is offered first, as "Director (Board)", for Management Changes and Executive Moves. Remuneration does not offer it: proxies list executives, not directors.

### 9.4 Files

- `app/data/proxy_pay.py` (new)
- `app/data/test_proxy_pay.py` (new)
- `app/data/people_service.py`: `_finish_frame`, `_proxy_records`, `add_proxy_pay`
- `app/data/screening_config.py`: the categories, `PEOPLE_ROLE_BOARD`
- `app/pages/screening.py`: role options
- `app/utils/cache_manager.py`: refresh after warm

### 9.5 Full run and two more fixes found in testing

**Full extraction**
- 1,221 proxies read. 1,070 of them yield a Summary Compensation Table.
- 13,323 executive-year rows across 388 companies.
- Cold run: about 6 minutes locally with 4 downloads in flight. Warm runs read new proxies only.
- After the merge:
  - SEC pay covers 400 companies, up from 103.
  - The People pay universe covers 455 companies, up from 160.
  - Management Remuneration for FY 2024–2026 over the default 540-company set: 2,871 executives across 441 companies, up from 747 across 104.

**Parser fixes from the full run** (frame `proxy_pay_v2`). The full run surfaced 120 title-like "names". The causes:
- "Name, Title" in one cell (SEZL).
- A year cell shared by a name row and a title row (TPR).
- A name cell spanning past its figures (PFGC, PLAY).
- Executives listed by title only (MSS). These are dropped.
- After the fixes: zero title-like names.
- A parse error on one proxy is now recorded for that proxy and no longer stops the run.

**Name spelling**
- Proxy names are mapped to the spelling already on file for the same company and person. All-caps names are title-cased ("SCOTT E. MCPHERSON" → "Scott E. McPherson").
- Without this, TPR listed "JOANNE CREVOISERAT" (2026) and "Joanne Crevoiserat" (2024–25) as two people.

**Executive Moves bug (from round 2, frame `management_changes_v7`)**
- The executive-news rows joined in round 2 had `moved_from` = NaN. The Moves filter `astype(str) != ""` read "nan" as a move, so 400 news appointments were listed as moves (round 3's "472").
- The build now fills `moved_from` / `moved_from_title` with "".
- Moves are now 89 for all roles (93 in the frame before de-duplication and scope), of which 40 are directors.

**Verified in the UI** (Playwright, testing :8504 and STG checkout :8505, identical results). Every count equals an independent pandas recount using the app's period rule (`date >= today - N days`), and every Excel row count equals the screen.

| Screen | Result |
|---|---|
| Category options | Exactly the 4 categories |
| Management Changes, Director (Board), 90 days | 64 / 49 companies; every row role Board with a named person |
| Management Changes, Director (Board) + CEO, 30 days | 22 / 18 |
| Management Changes, any role, 7 days | 4 / 4 |
| Executive Moves, Director (Board) | 40 |
| Executive Moves, all roles | 89 |
| Management Remuneration, FY 2024–2026 | 2,871 / 441 |
| Layoffs & Restructuring, 30 days | 60 / 30 |
| Layoffs & Restructuring, 365 days | 755 / 176 |

No Title contains or Name contains field appears anywhere.

**Data-team items** (their table is still preferred wherever it has the company-year):
- ACI 2024: Sankaran and McCollam both carry $7,882,248. The filing says $15,240,511 for Sankaran.
- DLTR: McNeely's pay is filed under Jeffrey Davis.
- CFR: Stead carries Rhodes's numbers.

## 10. Round 5 (2026-10-16): Key Devs parity, watchlist scope, "Other" role, speed

### 10.1 Key Devs "Management Changes" vs People — what differs and why

Key Devs lists one row per stored EVENT (each 8-K, 6-K and news article). People
lists one row per person CHANGE: an 8-K naming three directors is three rows, and
six articles about one CEO appointment (GIS, 2026-09-30) are one row with the other
outlets under Also Reported By. Raw counts therefore never match; coverage was
measured per event instead (last 30 days, 152 Key Devs events):

| Outcome | Events |
|---|---|
| Listed in People (same event) | 34 |
| Same change, another source (folded into the People row) | 68 |
| News that is not a people change (insider trades, earnings previews, lawsuits, buybacks) | 21 |
| 6-K stored as cover page only (data team: text cut at 2,000 chars) | 14 |
| News about another company's appointment filed under a former employer | ~8 |
| 8-K with no named change in the stored text (pay-only, or text cut to exhibit boilerplate) | 4 |

Root causes fixed:

1. **Extractor recall (8-K 5.02).** Real wording the regexes missed: bare "depart"
   (KMB 2026-10-01, Russell Torres), "is/will be leaving", "ceased serving",
   "suspended from", "their respective retirements", "not to seek re-election"
   (no "for"), "transition to the role of", "became a member", accented names
   (María Fernanda Mejía), "Ms. Sarah G. McCoy" (honorific on the full name), and
   board seats with no title ("elected Joaquin Duato as an independent member of the
   Board", PEP 2026-09-17). Possessives ("Mr. Vestberg’s Verizon Board") and
   "… Companies" no longer read as people, which also replaced wrong-person rows
   ("Lowe's Companies" → Angela Braly). Measured on all 9,051 stored filings:
   filings with a named change 5,758 → 5,971; +346 events, −25 (most of the 25 are
   the wrong-person rows replaced). Sampled gains ~90% correct.
2. **News for non-Coresight companies.** Company names came from coreiq_companies
   (540), so every article about any other ticker failed "is this about the
   company" and was dropped. Names now come from the all-companies universe
   (~4,800), Coresight spelling first.
3. **Appointments filed under the former employer.** "Five Below Appoints Former
   Ulta Beauty CFO Scott Settersten to Board" is stored under ULTA. When the
   headline's subject is another company with a unique short name, the change is
   filed under that company (FIVE).
4. **Period date.** People filtered on the date stated in the text ("On October 1,
   …"), days before the filing; Key Devs filters on the filing/article date. People
   now filters on the disclosure date too (new "Disclosed" column; "Announced" kept).

Frame `management_changes_v8`: 10,346 rows, 477 companies (v7: 10,054 / 442).

### 10.2 Universe: 558 → 4,793

People screened `get_base_company_universe()` (the Coresight list). It now uses the
same universe as Key Devs (`_all_companies_on()` covers People), and with no
watchlist and no Industry/Geography criterion the scope is `None` = every ticker,
so a company added to the data tomorrow appears without a universe rebuild.

### 10.3 Watchlist scope

* **Key Devs** ignored the watchlist whenever no Industry/Geography criterion was
  present: the `ALL_TICKERS` shortcut dropped the ticker list. It now applies only
  when no watchlist is active (`_watchlist_on()`).
* **Empty watchlist / failed member read** returned the whole universe
  (`if not members: return df`, and truthiness checks on `[]`). An active watchlist
  with no members now scopes to zero companies, in `filter_universe_to_members`,
  `recompute_working_set` and every Show Results path.
* **People** applies one scope (`_in_scope`) to all four sections. Executive Moves
  keeps a move when either company is in the watchlist (a watchlist company losing
  an executive is its news too).

### 10.4 Role "Other"

`people_service.role_bucket`: any role the dropdown does not list (blank, the
retired "Unknown", a bucket added later) is Other — in the filters and in the Role
column. Every role selected = no role selected, by construction.

### 10.5 Speed

Key Devs: adding a criterion ran `SELECT DISTINCT ticker … WHERE event_category IN
(…)` to mark companies "has an event". Undated, MySQL's loose index scan over 35
date partitions took 33 s (45 s with the category index forced). Key Devs mode never
shows that marker, so the query is gone from that path.

Key Devs, unbounded screen (no Industry/Geography/watchlist): the first page is the
newest 8 events per industry, picked with `ROW_NUMBER() OVER (PARTITION BY industry)`
over every matching event joined to three company tables. DB-side alone (ids only,
no text) that took 25 s for Management Changes / All History and 37.5 s for M&A.
New `keydev_event_index_v1`: (event_id, event_date, ticker, event_category) for all
217k events, 0.9 MB on disk, signal MAX(event_id), built in the boot warm-up
(`_warm_keydevs_screening`) or in a background thread on first use — never on a
click. `pick_per_industry` does the pick in pandas (ms) and only the ~540 picked
rows are fetched by primary key (`event_id IN (...)` + a date range for partition
pruning). Until the index exists the SQL pick is used. The in-memory pick returned
the same events as SQL except shared-ticker duplicates (SQL listed LULU's news under
both Lululemon and "Lulu Retail Holdings"); the window boundary uses UTC, like
`CURDATE()`. Local (VPN) Show Results for Management Changes / All History: 51.7 s
→ 13.3 s; the remaining time is summary text over the VPN, which STG does not pay.

People Show Results (all in-process, materialized frames): 239–658 ms locally,
including Management Changes All History (8,002 rows).

### 10.7 Verification (Playwright, localhost:8504 testing and :8505 STG checkout)

| Check | Result |
|---|---|
| Key Devs Mgmt Changes 7 / 30 days / All History | 14 / 156 / 10,713 (= DB COUNT) |
| People Mgmt Changes 7 / 30 days / All History | 4 / 45 (35 cos) / 8,002 (475 cos); Excel rows equal; equal to pandas recount |
| People universe | 4,793 companies (was 558) |
| Roles, Mgmt Changes 365 d: empty / all 17 / Other | 966 / 966 / 94 |
| Roles, Remuneration: empty / all 16 / Other | 3,932 / 3,932 / 345 executives |
| Watchlist Coresight-100 (99 cos): Mgmt Changes / Remuneration / Layoffs | 1,515 / 1,133 / 375 rows, 0 tickers outside the watchlist |
| Watchlist, Key Devs All History | 2,226 events, 0 outside (before: every company) |
| Watchlist, Companies | 99 matched, Excel 99, 0 outside |

### 10.6 Files

`app/data/management_changes.py`, `app/data/people_service.py`,
`app/data/screening_service.py`, `app/pages/screening.py`, `app/utils/cache_manager.py`, tests in
`app/data/test_management_changes.py`, `app/data/test_people_service.py`,
`tests/test_screening_keydevs_columns.py`.
