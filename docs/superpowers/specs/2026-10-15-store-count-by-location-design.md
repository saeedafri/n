# Store Count by geographic location; Additional Data Edit fix; "available data only"

**Date:** 2026-10-15
**Page:** `/screening` → Screen For: Companies → Company Information
**Files:** `app/pages/screening.py`, `app/data/screening_service.py`, `app/data/screening_config.py`,
`app/data/segment_aliases.py`, `app/data/repository.py`, `app/data/store_count_sources.py`,
`app/utils/cache_manager.py`, `tests/test_store_count_locations.py`, `tests/test_segment_member_values.py`

---

## 1. What was reported

1. **Edit opened the wrong form (critical).** Company Information → Additional Data → Add
   Criteria → Edit on the card opened **Key Developments by Category**. Companies mode no
   longer offers that criterion at all.
2. **Store Count by Category returned 0 companies.** The option is to be removed.
3. **Store Count needs geographic locations,** grouped the same way the Geographical
   Segments filter groups its members.
4. **No "N/A" for Business Segments, Geographical Segments or Additional Data.** Only
   companies that have the data are listed. Other statements (Income Statement, Balance
   Sheet, Cash Flow, Key Stats, Estimates…) keep showing N/A, because they support
   multi-year views.

## 2. Root causes

| # | Cause |
|---|---|
| 1 | `_start_edit_criterion` set `scr_active_form = criterion["type"]`, which is `"additional"`. `_render_criterion_form` then sent `keydevs`, `biz_segments`, `geo_segments` and `additional` all to `_render_keydevs_form` (a leftover from when that form carried the Additional Data checkboxes). |
| 3 | No screener path read store-count geography. The per-location counts exist only as XBRL facts (`us-gaap:NumberOfStores` and friends, on `srt:StatementGeographicalAxis`) in `coreiq_filing_metrics_v5`. `source='store_count'` rows are worldwide totals only. |
| 4 | `recompute_working_set` is annotate-only for every type except Industry/Geography: each criterion left-joins onto the full universe, so companies without data stay as N/A. |

## 3. Design

### 3.1 Edit routing
- An `additional` criterion is edited in the **Company Information** form
  (`scr_active_form = "financial"`). The form prefills Statement Type = Additional Data,
  Metric = Store Count / Credit Rating, and the saved locations.
- Legacy `biz_segments` / `geo_segments` criteria go to their own forms. `keydevs` goes
  to the Key Developments form, as before.
- Stale widget keys (`scr_fin_stmt_sel`, `scr_fin_metric_sel`, `scr_fin_segment_members`,
  `scr_store_locations`) are cleared when an edit starts, so the prefill wins.

### 3.2 Additional Data metrics
`Store Count`, `Credit Rating`. "Store Count by Category" is removed from the UI. Saved
screens that still carry `store_counts_by_category` keep running through the unchanged
service branch.

### 3.3 Store Count → Step 3 — Select Store Locations (optional)
- The same Region > Sub-region > Country > State > City cascade as Geographical Segments
  (`_render_geo_hierarchy_filters`, `geo_hierarchy.json`). Each option shows its company
  count and hierarchy path.
- Semantics:
  - Specific locations picked → exactly those.
  - Only cascade picked → every location the cascade keeps. This includes broader
    members, exactly like the segment cascade (`include_broader=True`).
  - Nothing picked → the existing worldwide Store Count (unchanged).
- Results: one column per location (`Store Count — <location>`) plus `Store Count As Of`.
  Only companies reporting at least one chosen location are listed. A chosen location a
  listed company does not report is blank, not N/A. Counts are whole numbers.

### 3.4 Data source and naming (no invented mapping)
- Facts: `concept IN XBRL_CONCEPTS`, `is_dimensioned = 1`, `doc_type = '10-K'`, axis
  matching `COUNTRY_AXIS` (`geograph|country|countries|region`).
- Location name, in order:
  1. A `country:XX` member is named by its ISO code (`segment_aliases.ISO2_COUNTRY_NAMES`,
     hoisted from the Stores by Country code) and then run through
     `canonicalize_geo_label`. COST's "CANADA" and "Canadian Operations" are one place,
     because the qname says so.
  2. Any other member: the filer's label through `canonicalize_geo_label`
     (business workbook `geo_label_map.json`).
  3. A label the workbook does not know is **kept exactly as filed**. It sits under
     "Unclassified" in the cascade, the same rule Geographical Segments follows.
- Values: per (ticker, location, as-of date) the largest fact is kept. A whole and a slice
  of it, or one qname under two labels, would otherwise double up. This is the same
  convention as `store_count_sources.xbrl_by_year`. Across dates, the newest is kept.
  Locations are **never summed or merged**: a country and its states are both listed as
  filed.
- Plausibility gate unchanged: `0 < count <= MAX_PLAUSIBLE_COUNT`.

### 3.5 Cache (speed)
- `concept` has no index. One ticker over `idx_v5_ticker_dim_doctype` takes ~3 s, and a
  25-ticker batch hits the 120 s read timeout. `execute_query_readonly` turns that timeout
  into `[]`, which would silently lose companies: a first draft lost 100 of 146 tickers
  exactly that way.
- Build: one query per store-count ticker, 4 in parallel, on a **raising** connection,
  ~258 s on a laptop over VPN. Materialized as `screening_store_locations_v1` via
  `materialized_or_build`, with signal `MAX(id) WHERE source='store_count'`.
- Never built on a click. Boot warm-up (`_warm_store_locations_screening`) starts it, and
  if a user opens the form before the first build lands, Step 3 says so and plain Store
  Count still works. After the first build, filtering is an in-memory dict lookup (the
  criterion applied in milliseconds in the E2E run).

### 3.6 "Available data only" (`shows_available_data_only`)
- Applies to Business Segments, Geographical Segments (`financial` with a segment
  statement), Additional Data (`additional`), and legacy `biz_segments` / `geo_segments`.
- After the merge, the universe narrows to rows with a value in any of the criterion's new
  columns. The card says "N companies matched".
- Not applied to:
  - the hidden Credit Ratings / Store Counts checkboxes riding on an Income Statement /
    Balance Sheet / Cash Flow criterion, which annotate that grid;
  - degraded (failed) criteria, which keep the universe rather than emptying the grid.

## 4. Data found (STG, 2026-10-15)
146 store-count tickers; 22 tag store counts on a geographic axis; 81 distinct locations.

**Flagged for the business/data team — kept as filed, not guessed:**
- **Non-places tagged on the geographic axis**, none of which the workbook excludes:
  - Class Action Lawsuit
  - Distribution center closures
  - Carquest conversions or consolidations to be completed
  - Chipotle
  - Carter's
  - Brand Portfolio
  - DXL Outlets, DXL Stores
  - Casual Male XL Retail and Outlet Stores
  - Rochester Clothing Stores
  - Company-operated, Licensed, Licensed Units, Franchise
  - Domestic franchise restaurants
  - Other

  They appear under "Unclassified".
- **Alternate spellings the workbook does not merge:** "U.S. Virgin Islands" (resolves to
  a hierarchy node) vs "United States Virgin Islands" (does not). They are kept separate.
- **Compound labels:** "Dominican Republic And Guatemala" and "Guatemala City, Guatemala
  and Bucaramanga, Colombia".
- **Stale per-location figures** are shown with their date (e.g. CMG Canada 2015-12-31,
  CRI 2016-01-02). The filer stopped tagging that location later.
- **Shared ticker LULU** (Lululemon / Lulu Retail): both rows receive Lululemon's store
  data. This is the existing ticker-keyed behaviour of all Additional Data.

## 5. Testing
- `tests/test_store_count_locations.py` (15 tests) covers:
  - ISO naming and workbook naming; unknown labels kept as filed
  - no merging or summing; whole beats slice; newest wins; non-geographic axes ignored
  - hierarchy grouping matches the segment cascade
  - location and group filtering, and pipeline narrowing
  - segments narrowed while Income Statement keeps N/A; the hidden checkbox exemption
  - the metric list
- `tests/test_segment_member_values.py`: the year-range pipeline test now expects the
  no-data company to be dropped (the rule reversed by this change).
- Real-UI E2E (Playwright, local STG DB): additional metrics; EMEA cascade (10 companies,
  13 location columns); Edit reopens Company Information with the saved selections;
  Canada (10); Credit Rating (40); plain Store Count (149); Geographical Segments (323 of
  558); Business Segments (369); Income Statement keeps 558 with N/A. Excel rows and
  columns equal the grid. DB spot check: COST UK 29 and Canada 110 as of 2025-08-31.

## 5a. Round 2 (same day): Coresight-100 testing, Edit/Cancel, warm-up

**Scope confirmed.** Store Count lives only in Company Screening (Screen For: Companies →
Company Information → Additional Data). Key Devs mode offers Industry, Country of
Incorporation and Key Developments by Category only. This was checked in the UI.

**Watchlist "Coresight-100"** (STG `coreiq_watchlists.id = 86`, owner
mohdsaeedafri@coresight.com) was created from `C100 2026.xlsx`:
- 99 of 100 companies, each matched by hand-checked exact name to its screening-universe
  row (ticker + company_name, the watchlist identity).
- Shared tickers are kept apart: TSCO = Tesco and Tractor Supply; JD = JD Sports and
  JD.com.
- **Not added:** Compagnie Financière Richemont. It is not in `coreiq_companies`, and
  ticker CFR there is Cullen/Frost Bankers.

**Watchlist scoping holds.** The pipeline narrows to the watchlist before any criterion,
and "available data only" narrows within it. For every criterion type, a run with the
watchlist equals the full-universe run intersected with the watchlist:

| Criterion (Coresight-100) | Expected (independent) | UI |
|---|---|---|
| Store Count (worldwide) | 34 | 34 |
| Store Count — Region EMEA | 5 | 5 |
| Store Count — Canada | 4 | 4 |
| Credit Rating | 10 | 10 |
| Geographical Segments | 46 | 46 |
| Business Segments | 56 | 56 |
| Income Statement | 99 | 99 |
| Income Statement + Store Count Canada | 4 | 4 |
| No watchlist, Store Count | 149 | 149 |

**Fixes found while testing the Edit cycle**
1. **Cancel deleted the criterion.** Edit takes the criterion off the stack while the form
   is open. Cancel, clicking the open palette button again, or switching to another
   palette form dropped it for good. `_cancel_criterion_form()` now puts it back at its
   original position; it is used by all 9 Cancel handlers and both palette paths.
2. **Store-location warm-up queued behind minutes of other warms.** It now has its own
   thread (like People). From a cold cache dir, STG built it 4 min 55 s after boot.
3. **Background build was invisible.** WARNING lines now record "first build started /
   written / failed", because a daemon thread otherwise dies silently.

Local-testing note: opening `/screening` directly skips `main.py`, so no warm-up runs.
Open `/` first. Azure's health-check runs `main.py`.

**Recorded E2E** (Playwright video, Coresight-100):
- Watchlist select → Store Count → Show Results → Edit → EMEA → Show → Edit → Cancel →
  Show → Edit → Canada → Show → Edit → Credit Rating → Show → Edit → Geographical
  Segments → Show → Edit → Business Segments → Show → Edit → Income Statement → Show →
  Edit → Cancel; then two criteria together; then Key Devs mode; then the watchlist off.
- 34/34 checks pass on testing and on market-data-stg.
- Every Edit reopened Company Information with the saved statement, metric and
  locations; Key Developments never appeared.
- Server run time per click: Show Results 0.4–0.7 s, Edit 0.3–0.5 s. Adding a segment or
  Income Statement criterion takes 1.0–2.3 s, behind the branded "Applying criteria…"
  spinner. No layout shift on Show or Edit; a single card shift when the form closes
  after Add.

## 5b. Round 3 (same day): fiscal year / year range for Store Count and Credit Rating

**Request.** Store counts and credit ratings change every year, so Additional Data must
offer a single year or a year range, like the financial statements do.

**Data (STG).** `coreiq_filing_metrics_v5` already holds every year:
- store counts 2016–2026 (146 companies, 761 rows; 50 companies have 7 years);
- credit ratings 2019–2026 (42 companies, 443 rows; S&P, Moody's, Fitch).

Until now the screener only read the newest year.

**Which year a value belongs to.** It is the fiscal year of the value's own date
(`period_instant`), using the rule the segment columns use
(`SegmentDataRepository._fiscal_year_of`: a year ending Jan 1–7 is the year before).
`report_fiscal_year` is the filing's label and is wrong for 100 of 761 store-count rows.
For example, UAA's 2020-12-31 count is filed under 2021, so keying on it would put the
count in the wrong column. A row with no date keeps `report_fiscal_year` (34 rows).

**Choosing one value per (company, year).** The rules match what Latest already shows:
- the newest date in the year wins;
- on the same date, the larger store count wins, as in the Latest MAX;
- for ratings, the first-filed rating wins (lowest id). This is exactly the rule behind
  today's Latest value, checked for all 42 companies (0 differences).

**Form.** Company Information → Additional Data:
- Store Count: Step 3 Locations (optional), then Step 4 — Select Fiscal Year.
- Credit Rating: Step 3 — Select Fiscal Year.
- Choices are Single year (Latest, or a year) or Year range (From / To).
- Only years that have data are offered.
- Edit prefills the saved choice; the stale keys `scr_add_year_*` are cleared on Edit.

**Results.**
- Latest: unchanged, one column.
- A year or a range: one column per year, `Store Count [FY 2024]` /
  `Credit Rating [FY 2025]`.
- With locations: one column per location per year, `Store Count — Canada [FY 2024]`.
  The "As Of" column is dropped there, because the year is in the heading.
- Only companies with a value in at least one chosen year (or location-year) are listed.
  A year one of them did not report is blank, never N/A.
- The card summary carries the years, e.g. "Additional Data: Store Count (FY 2022–FY 2025)
  — Canada". The details list the Fiscal Year.

**Speed.**
- History: one query, ~1,200 rows (6 s cold / 0.8 s warm). It is materialized as
  `screening_additional_history_v1` (signal `MAX(id)` over both sources) and warmed on
  boot on the `warm-store-locations` thread. Applying a criterion is a dict lookup.
- Store locations: the cache became `screening_store_locations_v2` (adds the per-year
  counts). The first boot after deploy rebuilds it in ~5 min; until then Step 3 shows
  its "being prepared" notice, and Store Count by year without locations still works.

**Tests.** `tests/test_store_count_locations.py` has 7 new tests, 22 in total:
- the year comes from the value's own date (UAA, early January, no date);
- the within-year rules;
- a range gives one column per year and lists only companies with data;
- a single year;
- Latest is unchanged;
- locations by year.

## 6. Rollout
- No DB writes, no DDL. The cache file lands in the materialize dir (`/home/mdp-cache` on
  App Service, persistent across deploys).
- First boot after deploy builds it in the background (~4–5 min); until then Step 3 shows
  a notice.
- Behaviour change to announce: Segments and Additional Data screens now list only
  companies with data, so their counts drop (e.g. Geographical Segments 558 → 323).
