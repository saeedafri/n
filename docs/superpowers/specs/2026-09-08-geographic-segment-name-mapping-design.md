# Geographic segment name mapping — design

**Date:** 2026-09-08
**Area:** Screening → Financial Information → Geographical Segments → Step 3;
Market Data → Segment tab (shares the same canonicalisation)
**Source of truth for names:** business team workbook `Countries Mapping.xlsx`
**Predecessor:** [2026-09-01 geographic segment name grouping investigation](2026-09-01-geographic-segment-name-grouping-investigation.md)

---

## 1. Problem

Step 3's "Segment members" dropdown listed **323 options for ~216 real places**.
One place arrived under many spellings because the option labels are the raw XBRL
`dimension_member_label` each filer typed:

| Place | Options the user saw |
|---|---|
| EMEA | `EMEA`, `Europe, Middle East and Africa`, `Europe, Middle East and Africa ("EMEA")`, `Europe, Middle East, and Africa`, `Europe, the Middle East and Africa`, `Europe/Middle East/Africa`, `Europe/Middle East and Africa`, `EMEA Geographic Region`, … (12 in all) |
| Asia Pacific | `Asia Pacific`, `APAC`, `Asia-Pacific`, `Asia- Pacific`, `Asia/Pacific`, `Asia and Pacific`, `Asia Pacific (APAC)`, `Asia Pacific ("APAC")`, `APAC Geographic Region` |
| United States | `United States`, `USA`, `U.S. segment`, `U.S. total`, `United States ("U.S.")`, `United States [Member}`, `United States Geographic Region`, `Puerto Rico` |
| South Korea | `Korea`, `South Korea`, `Korea, Republic Of`, `Republic of Korea` |

Selecting one spelling screened only the companies that used *that* spelling —
picking `EMEA` matched 49 companies when 79 report EMEA revenue.

The dropdown also offered things that are not places at all: business segments
(`Corporate Segment`, `Traditional Homebuilding`), facility descriptions
(`Site in Plano, Texas`), tax categories (`U.S. federal`, `U.S. Stock Funds`),
pension disclosures (`U.S. pensions`) and scraper artifacts
(`srt_SegmentGeographicalDomain`, `AllOtherGeographiesMember`).

## 2. Root cause

Three separate causes, all of which had to be fixed:

1. **No name mapping.** `app/data/segment_aliases.py` grouped 6 canonicals
   covering 41 of ~1,260 distinct raw names (3%). Everything else passed through
   verbatim.
2. **The member cache is a case-insensitive collapse.** `_refresh_segment_member_cache`
   does `GROUP BY segment_type, member_label` on MySQL's default `utf8mb4_*_ci`
   collation, so `United States And Canada` and `United States and Canada` merge
   into one group and MySQL returns **an arbitrary one of the two spellings**. The
   values cache keeps both verbatim. Anything that treated the member cache as
   the complete label universe therefore had a hole.
3. **"Latest" is computed per raw label.** `read_segment_values_cache` picks
   `MAX(report_fiscal_year)` grouped by `(ticker, member_label)`. A filer that
   renamed a segment leaves two "latest" rows — Uber's
   `United States And Canada` (latest 2023) and
   `United States and Canada ("US&CAN")` (latest 2025) — so collapsing them by
   larger value would show a two-year-old number for any segment that shrank.

Naming is a business decision, so cause 1 could not be fixed in code. The
business team supplied `Countries Mapping.xlsx`.

## 3. The workbook

Four sheets; three drive the app.

| Sheet | Rows | Used | Meaning |
|---|---|---|---|
| `Mapping` | 479 | **yes** | `MDP Label` → `Mapping Name`. The name column is filled only on the first row of each visual group, so it is **forward-filled**. 490 labels → 221 canonical names after the manual additions below. |
| `remove` | 49 | **yes** | 37 labels with action `Remove` (not geographies) and 12 with `Decode -> keep` (opaque acronyms plus their meaning). |
| `codes` | 98 | **yes** | Canonical name → ISO 3166 alpha-3. Carried through as reference metadata. |
| `states` | 316 | **no** | Canonical *country* + ISO for labels that roll **up** to a country (`California` → `United States`). |

### Why `states` is not applied

It contradicts `Mapping` deliberately and would over-collapse:

* `Mapping` keeps `California`, `Texas`, `Ohio` as their own options; `states`
  rolls all 50 into `United States`. Applying it would delete every US-state
  segment option.
* `North` is `North America` in `Mapping` and `United States` in `states`; same
  for `South`, `Central`, `East`, `West`, `Eastern Region`, `West Coast`.

`Mapping` is the sheet whose header asks "what should this be called", so it
wins. `states` is kept in the workbook as a country-rollup reference for a future
"group by country" feature; nothing reads it today.

### Conflicts and judgement calls, all business-directed

| Item | Decision | Why |
|---|---|---|
| `U.S. federal` / `U.S. Federal` | **Removed**, overriding `Mapping`'s → `United States` | Both sheets name it. `remove` carries a specific per-filer reason ("tax/government category, Micron"); `Mapping`'s entry is a bulk alphabetical assignment. Mapping a tax-jurisdiction row into US revenue would inflate MU's US revenue. Normalisation is case-insensitive, so the two spellings cannot be told apart. `United States Federal` (the full spelling, a genuine Latham/SWIM segment) is a different key and stays → `United States`. |
| `Puerto Rico`, `GUAM` → `United States` | Applied | `Mapping` says so. |
| `Bermuda`, `Cayman Islands`, `British Virgin Islands` → `United Kingdom` | Applied | `Mapping` says so. |
| `Aruba` → `Netherlands` | Applied | `Mapping` says so. |
| `North` → `North America`, `South` → `South America` | Applied | `Mapping` says so; note the `states` sheet disagrees (§ "Why `states` is not applied"). **Worth a second look with the business team** — for a US retailer an internal "North" region is probably domestic, not the continent. |
| `Southern` | Left as `Southern` | The workbook's own note reads "Please check which south is this". |
| `Mci`, `JAPA Geographic Region` | Left untouched | `remove` marks their decoding "needs confirmation" / "likely". |

### Manual additions (in `scripts/build_geo_label_map.py`, each with a reason)

Extensions of the business team's own rulings, recorded in the generated JSON
under `manual_additions` so they can be audited or overruled in the next revision:

* 10 acronym decodes the `remove` sheet marked `Decode -> keep` and spelled out:
  `Apj` → `Asia Pacific & Japan (APJ)`, `Apjc`, `Apla`, `Eame`, `Laap`, `Lacc`,
  `LACC Geographic Region`, `Cis`, `Almea`/`Amea` → `Asia, Middle East & Africa (AMEA)`.
* `pf0:CA` → `Canada` — the same namespace-prefix artifact shape as the
  workbook's own `pf0:US` → `United States`.
* 4 more Texas Instruments facility descriptions and 3 pension disclosures
  removed, matching rows the `remove` sheet already removes by name.

## 4. Design

```
Countries Mapping.xlsx  ──[scripts/build_geo_label_map.py]──▶  app/data/geo_label_map.json
                                                                        │
                                              app/data/segment_aliases.py (pure text, no DB)
                                                                        │
                        ┌───────────────────────────────────────────────┴───────────────┐
                        ▼                                                               ▼
      Screening Step 3 dropdown / filter / results                    Market Data → Segment tab
      (screening_service.py)                                          (repository._classify_member)
```

**No DB writes.** The mapping is a read-only overlay file applied at read time,
matching the `ma_event_overrides.json` / `company_display_overrides.json`
precedent. The existing segment caches are untouched, so the fix is live with no
rebuild — and when the scheduler next rebuilds them, `_classify_member` already
canonicalises at source, so nothing drifts.

### Two lookup keys

`normalize_geo_key` — formatting only, never the words:
case, unicode quotes (`“U.S.”` → `"U.S."`), non-breaking and zero-width/bidi
characters (PriceSmart files `Central ‎American ‎Operations`), dash variants,
spacing around dashes and slashes (`Asia- Pacific`), XBRL `[Member]`/`{Member}`
residue, footnote markers `(a)` `(1)`, leading/trailing punctuation.

`geo_match_key` — additionally folds `&`, `/` and `,` to "and", drops `the` and
`total`/`subtotal`, strips brackets around a surviving qualifier, and drops a
parenthesised **acronym that merely repeats the label**. Used only when the exact
key misses.

**A parenthetical is dropped only when it is a short acronym** (`("APAC")`,
`(U.S.)`, `(US&CAN)`). A qualifier keeps its words:
`Americas (excluding United States)`, `Asia Pacific (including Oceania)` and
`Europe (Excluding United Kingdom)` are different segments from the bare region,
and merging them would combine revenue the filer split on purpose. Only the
brackets go, which is what lets `China (including Hong Kong)` merge with
`China Including Hong Kong` while staying separate from `China`.

### Safety property

The generator computes, for every workbook label, which canonical each key
claims. **A key claimed by two different canonicals is dropped, not guessed**,
and recorded in `_ambiguous_loose_keys`. Currently zero keys are ambiguous.
`tests/test_geo_label_map.py` asserts this so a future workbook revision cannot
silently introduce a wrong merge.

### Grouping

`build_geo_label_groups(labels)` → `{display name: [raw spellings]}`.

* Workbook-named labels group under their canonical name.
* The rest group by `geo_match_key`, so pure spelling drift still collapses with
  no workbook entry — `Other Foreign` + `Other foreign (1)`,
  `United States and Canada` + `United States & Canada` +
  `United States and Canada ("US&CAN")`.
* A group with no business name shows its most readable spelling: fewest
  artifacts, no trailing punctuation, least roll-up phrasing
  (`All foreign countries` beats `Subtotal all foreign countries`), then the
  longest. A **descriptive** parenthetical is not an artifact, so
  `China (including Hong Kong)` wins over `China Including Hong Kong`.
* Removed labels are left out entirely.
* The canonical name is always a selectable member of its own group, even when no
  filer spells it that way.

### Where it is applied

| Concern | Function | Behaviour |
|---|---|---|
| Non-places, everywhere | `SegmentDataRepository._classify_member` | Dropped at the shared classifier, so the Segments tab and the screening cache cannot disagree |
| Dropdown options | `_collapse_geo_canonical_options` | Group, sum company counts, drop non-places |
| Watchlist-scoped options | `_normalize_segment_options_rows` | Same collapse (was missing before) |
| SQL member filter | `apply_segment_statement_criterion` | Selected display name → every raw spelling in its group |
| Result labels | `_geo_display_resolver` | Built from the member cache **plus the labels just fetched**, closing the cause-2 hole |
| "Latest" per place | `_build_ticker_segment_values_from_cache` | Newest fiscal year wins when two spellings collapse (cause 3) |
| Market Data Segment tab | `canonicalize_geo_label` via `_classify_member` | Same names, for free |

### Saved criteria

Criteria saved before this change hold a raw spelling (`"U.S."`, `"EMEA"`).
`canonicalize_geo_label` normalises them to the display name on load, so they
keep working and in fact now match *more* companies. Verified: selecting `EMEA`,
`Europe/Middle East/Africa` or the canonical name all return the same 66
companies; `U.S.` and `United States` both return 208.

## 5. Performance

The mapping is a ~40 KB JSON parsed once per process behind `lru_cache`; grouping
is pure dict work. **No new query is issued** — the raw member-cache read was
split into `read_segment_member_labels_raw` (`st.cache_data`, ttl 300) and shared
by the dropdown, the filter expansion and the result labelling.

A/B measured in the real UI, same machine, server restarted for each run, code
stashed for the baseline (STG DB over VPN):

| Step | Before | After |
|---|---|---|
| `/screening` first render (cold process) | 12,042 ms | 12,061 ms |
| `/screening` render (warm) | 835 ms | 847 ms |
| Step 3 render after picking Geographical Segments | 38 ms | 38–41 ms |
| Segment members dropdown open | 48 ms | 48–58 ms |

Identical within noise. The 12 s cold first render is the screening page's
existing boot cost (see the calendar/screening cold-load work) and is untouched
by this change.

Component timings:

| Step | Measured |
|---|---|
| Raw member-cache read (cold) | 258 ms — pure RTT, unchanged |
| Collapse 323 raw → 216 options | 12 ms cold, **5.6 ms** warm |
| Add criterion (465-company universe) | 1.4 s |
| Show Results | 9.0 s (grid render, unrelated to this change) |
| One geo criterion end to end | ~630 ms, unchanged |

One regression was found and fixed during testing: `_geo_display_resolver` was
built from the fetched rows verbatim, and an unfiltered fetch hands over ~19K
rows carrying only ~390 distinct labels. Normalising per row cost **700 ms** on
the default all-segments path. De-duplicating first brings it to **17 ms** at
50K rows, and `build_geo_label_groups` now de-duplicates its own input so no
caller can reintroduce it.

Two cost changes, both deliberate:

* The member-filter SQL now emits one `IN (...)` list instead of one `OR` per
  label. A place can expand to 12 spellings, which as ORs made the predicate long
  for no benefit.
* The **geo** raw read limit rose from 500 to 1,500 rows
  (`_SEGMENT_GEO_ROWS_LIMIT`). Collapsing happens after the read, so a 500-row
  cap would silently drop places as filings add spellings. Measured identical
  (258 ms at 500, 1,500 and 3,000 rows — the query is RTT-bound at 323 rows).
  **Business members stay capped at 500**: 3,322 rows and growing, with no
  collapse step, and this database is packet-per-row bound.

## 6. Handling new data

Two paths, deliberately:

1. **Automatic — new spelling of a known place.** A filing that introduces
   `Asia‑Pacific Region` or `EMEA:` lands on the right name with no workbook
   change, because the keys normalise formatting rather than enumerate spellings.
2. **Business decision — a genuinely new place.** It keeps its own name in the
   dropdown (never dropped, never guessed) until the business team names it. To
   collect those:

```bash
# what the workbook does not yet name, one row per dropdown option, largest first
.venv/bin/python scripts/build_geo_label_map.py --report output/geo_mapping/geo-labels-unmapped-$(date +%F).xlsx

# after the business team returns a revised workbook
.venv/bin/python scripts/build_geo_label_map.py --workbook ~/Downloads/Countries\ Mapping.xlsx
.venv/bin/python -m pytest tests/test_geo_label_map.py
```

Today's report: **146 unnamed options**, largest `Other countries` (25
companies), `Other International` (12), `All Other Countries` (11),
`Other Americas` (11) — saved at
`output/geo_mapping/geo-labels-unmapped-2026-09-08.xlsx`. It also lists the raw
spellings already grouped under each option, so the team can see what they are
naming.

Candidates in that report worth a business ruling, since they look like the same
thing but cannot be merged without one: `Others` vs `Other`;
`Other AP` / `Other APAC` / `Other Asia Pacific`;
`Mainland China` vs `China Mainland`; `Int'l` vs `International`;
`U.S. and Canada` vs `United States and Canada`.

## 7. Results

| | Before | After |
|---|---|---|
| Step 3 geo options | 323 | **216** |
| Duplicate-spelling groups collapsed | 6 (41 labels) | **33 (139 labels)** |
| Non-places offered as segments | 32 | **0** |
| Companies matched by "EMEA" | 49 | **79 in the dropdown, 66 with revenue > 0** |
| Opaque acronyms (`Apj`, `Lacc`, `Almea`) | shown as-is | decoded |

UI evidence (local server `http://localhost:8501`, page `/screening`, STG DB):

* Typing `emea` in Step 3 offers **one** `Europe, Middle East and Africa (EMEA)
  (79 companies)`; `united states` offers **one** `United States (224 companies)`;
  `korea` offers **one** `South Korea (16 companies)`; `rest of` offers **one**
  `Rest of World (RoW) (27 companies)`.
* `federal`, `corporate` and the facility descriptions return **0 options**.
* Selecting `United States` + `EMEA` and running the screen: 228 of 465 companies
  have data; the results grid's Geographic Segment column reads
  `United States` / `Europe, Middle East and Africa (EMEA)`.
* Market Data → `ADBE` → Segment tab reads `Asia Pacific (APAC)`,
  `Europe, Middle East and Africa (EMEA)`, `United States`.
* Uber's renamed segment shows one row, `United States and Canada: 26,469.00`
  (FY2025), not two rows or the FY2023 figure.

## 8. Testing

`tests/test_geo_label_map.py` — 47 tests, no DB:

* the safety property (no key claims two canonicals);
* every reported duplicate resolves to its business name;
* qualified regions are never merged into the bare region;
* non-places are hidden;
* labels the workbook never saw survive untouched;
* spelling drift collapses without a workbook entry;
* group expansion covers every raw spelling;
* newest year wins for a renamed segment;
* case variants group even when the member cache hides one;
* junk/empty input is safe.

`tests/test_segment_totals.py` — two expectations updated, both encoding
pre-mapping names (`Asia Pacific` → `Asia Pacific (APAC)`; `Outside U.S.` and
`Outside US` now both → `Non-US`, with a new assertion that an *unmapped* label
still keeps its full stop).

Full suite: **317 passed**. Two pre-existing failures in
`tests/test_screening_keydevs_columns.py` (`_keydev_industry_label`) are
unrelated to this change and fail on `main` as well — flagged separately.

### End-to-end matrix actually exercised (STG DB, local UI)

| Path | Result |
|---|---|
| Geo dropdown collapse | 323 raw → 216 options, zero duplicate labels |
| Geo filter, canonical name | `EMEA` → 66 companies (was 49) |
| Geo filter, saved raw alias | `EMEA`, `Europe/Middle East/Africa`, `U.S.` all match the canonical's set |
| Explicit year (2024 / 2023 / 2020) — separate SQL shape | 59 / 58 / 52 matches, canonical labels only |
| Metrics beyond Revenues | Assets 142, Operating Profit 10, CapEx 3 — canonical labels |
| All-segments mode (no selection) | 211 rows, **zero** labels outside the dropdown's vocabulary |
| Saved criterion holding a now-removed label | 0 matches, no exception; mixed selection unaffected |
| **Business Segments dropdown (shared code)** | 500 raw = 500 options, **no collapse**, cap unchanged; `Corporate/Other`, `Retail stores` still listed |
| Watchlist-scoped options (`_normalize_segment_options_rows`) | Collapses identically, counts summed; business keeps raw labels (unit test — the live v5 SQL times out and falls back, pre-existing) |
| Edit an existing criterion | Step 3 re-populates `United States (224 companies)`, `Asia Pacific (APAC) (72 companies)` |
| Excel export | `Screening_Results_*.xlsx`, 522 rows, 23.8 KB, 3 distinct labels: `United States`, `Europe, Middle East and Africa (EMEA)`, `N/A` |
| Market Data → Segment tab (ADBE) | `Asia Pacific (APAC)`, `Europe, Middle East and Africa (EMEA)`, `United States` |
| Segment tab, removals (TXN / CAT / ARKR / SFIX) | TXN 6 canonical rows and no facility descriptions; CAT no pension rows; ARKR geo gone, business table renders; SFIX shows the designed *"No extracted data available"* |
| **Quarterly** segment builder (shares `_classify_member`) | TXN 8, CAT 4, ADBE 4 canonical members, zero non-places |
| Market Data header geo-members string | TXN 8, CAT 7, ADBE 12 canonical; SFIX/ARKR 0, no error |
| Segment tab Excel export (TXN, CAT) | Downloads, 6.7 KB / 8.2 KB, zero non-places in either |
| Screening re-run **after** the classifier and fallback fixes | `emea` one option, `federal` zero, grid canonical, nothing leaked |
| Watchlist-scoped screening in the UI | Same collapsed options as the global dropdown |
| Renamed segment (UBER) | one row, FY2025 value 26,469 |
| Cold/warm A/B vs stashed baseline | identical within noise |

### Removals apply at the shared classifier

The first cut applied the renames at `_classify_member` (shared by the Segments
tab and the screening cache) but the **removals** only in the screening path.
That drifted: TXN's Segment tab still listed nine facility descriptions as
geographies while the screener hid them, contradicting
`_classify_segment_rows`'s own contract ("SINGLE SOURCE OF TRUTH … so the
screener can never drift from what the Segments tab shows"). The exclusion now
sits next to the canonicalisation in `_classify_member`, so both surfaces agree:

| Ticker | Segment tab geo members before → after |
|---|---|
| TXN | 17 → 8 |
| MU | 15 → 14 |
| CAT | 9 → 7 |
| TAP | 9 → 6 |
| TOL | 9 → 7 |
| ARKR / DIN / SFIX | 3 / 1 / 1 → 0 |

The drop applies only on the geographic axis — a *business* segment named
"City Living" still classifies as a business segment.

`_fetch_from_edgartools`, the fallback the tab uses when a ticker has no DB
segment rows, normalised case only — no names, no removals. It had always been
inconsistent with the DB path; making the DB path drop non-places made it
*reachable*, because SFIX's only geo row is a facility sentence, so the tab fell
back here and the raw label reappeared. It now applies the same two rules.

### Removal impact — every affected ticker

27 tickers have at least one geo member removed. Nothing removed survives, and
every removal traces to a business-team ruling:

| Ticker | geo before → after | Removed |
|---|---|---|
| TXN | 17 → 8 | 9 facility descriptions (Plano, Houston, Hiji, Nice, Santa Clara) |
| PPC | 17 → 13 | Export, Fresh, Prepared, Other Products |
| MU | 15 → 14 | U.S. federal (tax jurisdiction) |
| TAP | 9 → 6 | Corporate Segment, Irwindale Brewery, Montreal Brewery |
| CAT | 9 → 7 | U.S. Pension Benefits, U.S. pensions |
| TOL | 9 → 7 | City Living, Traditional Homebuilding |
| C | 10 → 8 | Corporate/Other, State and Municipalities |
| BC | 8 → 6 | Corporate/Other, Parts and Accessories |
| BF-B | 14 → 12 | Non-branded and bulk, Travel Retail |
| WMT | 8 → 7 | Walmart International (business segment) |
| XOM | 11 → 10 | Pension Benefits - U.S. |
| AVGO, AXP, CHSCP, GME, GPI, HAS, LE, LEVI, MOV, MSI, NWL, PEP, TSN | −1 each | one artifact / tax / product label each |

**Three tickers lose every geo member**, because a facility sentence was their
only geographic tagging:

| Ticker | Was | Now |
|---|---|---|
| ARKR | 3 × "Hard Rock Casino and Hotel, …, Florida" | no Geographic table; **Business** table still renders (6 members) |
| DIN | "Cincinnati, Ohio Market Area Restaurants" | no Geographic table; **Business** table still renders (30 members) |
| SFIX | "Bethlehem, Pennsylvania and Dallas, Texas" | no segment tables at all → the page's designed empty state, *"No extracted data available. Check official filings."* |

This is intended: `_build_table` returns `""` for an empty section and
`render_segment_data` falls back to that message, so the empty state is a
designed path, not a break — 103 of the 372 segment-reporting tickers already
had no geographic segments before this change. In screening these three now
return `N/A` for a geo criterion instead of a facility description's value.

### No member changed tables

`_looks_geographic` reads the alias key set, which grew from 41 to 352, so
member routing between the Business and Geographic tables was re-checked by
running the classifier with the map present and with it hidden:

| Ticker | Business members | Geographic |
|---|---|---|
| COST | 10 → 10 identical | 5 → 3 (spellings merged) |
| AMD | 10 → 10 identical | 9 → 9 |
| TJX | 4 → 4 identical | 4 → 4 |
| NKE | 14 → 14 identical | 6 → 6 |
| MU | 20 → 20 identical | 15 → 15 |
| CAT | 30 → 30 identical | 11 → 9 (spellings merged) |

Business membership is byte-identical everywhere: nothing migrated. Geographic
counts fall only where two spellings of one place merged.

### Robustness

| Case | Behaviour |
|---|---|
| `geo_label_map.json` deleted (the rollback path) | No crash; labels pass through raw; the legacy pension list still filters. Confirms § 9. |
| `geo_label_map.json` corrupt | No crash; same degradation |
| Generator run twice | Byte-identical output |
| Generator with a missing workbook | `workbook not found`, exit 1, existing map untouched |
| Every operator (Greater/Less/Between/Equals/≥/≤) | All six correct; `Equals 581,175.00` matches WMT alone |
| 3 geo criteria stacked (US → EMEA → APAC FY2023) | 99 → 22 → 13 companies, canonical labels throughout |
| All 216 options selected at once | 302 raw spellings in one `IN` list, 1.2 s, 256 matches |
| Select all in the UI | 216 chips, add criterion 5.6 s |
| Remove criterion | Universe returns to 465 |

### Not exercised, and why

* *(Closed 2026-09-09.)* **Saved Screenings with stored geo members** — no
  production row had any (all 30 used all-segments mode), so one was created
  through the UI, a throwaway "geo-mapping E2E" set holding `United States`,
  `Europe, Middle East and Africa (EMEA)` and `South Korea`. After a full page
  reload it appeared in the dialog, loaded (228 of 465 companies have data), and
  its Edit form pre-filled **all three canonical names exactly**. It was then
  deleted through the app's own delete path, which is a soft delete
  (`is_active = 0`) — the row is invisible in the listing (18 active) and no
  other saved screening was touched.
* **CQ / FQ period types** for segment criteria return the same rows as FY.
  `coreiq_screening_segment_values_cache` has no quarter column
  (`ticker, segment_type, metric_key, member_label, report_fiscal_year, value_mm`),
  so Step 4's Period Type has never affected a segment criterion. Pre-existing
  and out of scope here — flagged separately.
* **`_fetch_segment_options_aggregated_sql`** against `coreiq_filing_metrics_v5`
  times out (50 s, 0 rows for 40 tickers), so the watchlist-scoped dropdown always
  falls back to the global cache. Pre-existing; covered by unit test rather than
  live SQL.

## 8b. Cache rebuild (2026-09-09)

The read-time layer made the fix live with no rebuild, but the cache still held
pre-mapping labels. Rebuilding it (`build_segment_values_cache`, 462 tickers,
~16 min, staging + atomic RENAME) moves the names to source:

| | before | after |
|---|---|---|
| total rows | 70,962 | 70,549 |
| geo rows | 18,917 | 18,634 |
| distinct geo members | 376 | **264** |
| member cache (geo) | 323 | **228** |
| dropdown options | 216 | **215** |
| non-places stored | 32 | **0** |
| geo labels still renamed on read | 2 | **0** |
| business rows / members | 52,045 / 3,495 | 51,915 / 3,468 |

Business members fall by 27 because the element-name artifact is now stripped
per section, so "Europe Segment" merges into "Europe" instead of standing as its
own option.

Screening answers are unchanged: EMEA 66, United States 208, and the saved raw
aliases (`EMEA`, `U.S.`, `Europe/Middle East/Africa`) still resolve to the same
sets. APAC 59→60 and all-segments 251→252 because the rebuild picked up two more
tickers from fresher source data, not from any naming change.

The read-time layer stays. It is what makes a rebuild optional, keeps new
spellings working between rebuilds, and covers the labels a rebuild cannot fix.

It took four attempts. #1 ran before the two fixes below and exposed them; #2
published the fixed names; #3 **aborted** — 24 tickers hit the 20 s per-chunk
ceiling while the UI tests and app server were also querying, and the build's
completeness guard refused to publish a partial table, leaving the live cache
untouched (exactly the designed behaviour); #4 published cleanly on a quiet DB
in 942 s. The timeouts are the missing `coreiq_filing_metrics_v5`
`(ticker, is_dimensioned, doc_type)` index the build's own comment calls out, not
anything in this change.

### Two bugs the rebuild exposed

Both were invisible until the names were written to the cache and read back:

1. **A canonical name that renamed itself.** `Lacc` was mapped to an invented
   "Latin America & Caribbean (LACC)", whose loose key is the workbook's own
   "Latin America and Caribbean" — so `canonicalize()` renamed it again on read
   and the stored and displayed names disagreed. Fixed by reusing the workbook's
   "Latin America and the Caribbean" instead of coining a near-duplicate.
   `test_every_canonical_name_is_a_fixed_point` now pins this for all 220.

2. **The display-spelling map was section-blind.** `_member_display_map` keyed
   only on the member name, and only *geographic* members are canonicalised.
   Philip Morris tags "Middle East & Africa" on a business axis (81 rows, latest
   filing) and "Middle East and Africa" on the geographic one (15 rows), so the
   business spelling won and undid the canonicalisation — the rebuilt cache
   stored the raw "&" spelling for PM. The map is now keyed
   `(section, member_key)`.

Keying per section then exposed a third thing, this one caused by the fix: 23
business members share a `_member_key` with a geographic one but are spelled
differently ("Europe Segment" vs "Europe", "Other Segments" vs "Other"), and 8
of them had been getting their trailing `Segment`/`:` artifact removed *by
accident*, by borrowing the geographic spelling. With the sections separated they
kept the artifact. Stripping is now section-local
(`_strip_member_artifact`): a trailing "Segment"/"Segments" or ":" is removed
from the chosen display name. A trailing **full stop is left alone** — it is
stray in "Japan." but the abbreviation itself in "Outside U.S." and "Eastern
Mediterranean Ops.", and no rule separates those reliably, so ranking (which
already prefers a stop-free spelling when the same section filed one) handles it.
The first attempt did strip stops and turned "Eastern Mediterranean Ops." into
"Ops"; the test added for that case caught it.

## 9. Rollout

No migration, no DB write, no cache rebuild. Ship the code; the collapse is live
on the next page render. To roll back, delete `app/data/geo_label_map.json` —
`segment_aliases.py` degrades to an empty map and the dropdown returns to raw
labels.

## 10. Files

| File | Change |
|---|---|
| `app/data/geo_label_map.json` | **new** — generated map (490 labels → 221 names, 44 removed, ISO codes) |
| `scripts/build_geo_label_map.py` | **new** — workbook → JSON generator, plus `--report` |
| `app/data/segment_aliases.py` | rewritten around the two keys and `build_geo_label_groups` |
| `app/data/screening_service.py` | shared raw read, group-based collapse/expansion/labelling, newest-year rule, `IN` clause, geo row limit |
| `tests/test_geo_label_map.py` | **new** — 47 tests |
| `tests/test_segment_totals.py` | 2 expectations updated to the business names |
| `output/geo_mapping/geo-labels-unmapped-2026-09-08.xlsx` | **new** — 146 options awaiting a business name |
