# Segments tab: patterns found by checking every cell against SEC

Follows `2026-10-07-segment-fiscal-year-and-breakdown-integrity-design.md` (one
fiscal year, one filing, one breakdown per column). That round fixed what Wendy's
showed; this one asked a broader question: **for every company, every metric,
every year and every quarter, does each number the Segments tab shows exist in
the company's own SEC filings, under that name, for that period?** Every
difference was traced to a root cause; each cause below is a rule that holds for
all filers, not a per-company patch.

## 1. Method

1. **SEC truth, one company at a time.** For each of the 381 companies with
   segment data, the latest 14 10-Ks and 32 10-Qs were fetched with edgartools
   into a private folder (`EDGAR_LOCAL_DATA_DIR`), every dimensioned
   currency fact was extracted (≈50 KB per company), and the download folder was
   deleted before the next company. `~/.edgar` was never written. A second pass
   with every axis (not only segment/geographic ones) covered filers that use
   their own axis names (Gap's `RegionReportingInformationByRegionAxis`).
2. **App truth.** The real builders (`_build_segment_tables_from_db`,
   `_build_segment_tables_quarterly`) were run for every company against the
   staging DB with `MDP_CACHE_DIR` pointed at a throwaway folder, so no cached
   result could stand in for a fresh build.
3. **Cell-by-cell verdict.** Each displayed value was matched to SEC facts of the
   same metric and period (fiscal year from the period end; quarter by its end
   date; Q4 as 10-K year minus 10-Q nine months):
   `exact` (value and member), `restated_old` (a newer filing reports another
   number), `other_label` (value right, filed under another member name),
   `slice`, `wrong_period`, `not_in_sec`, `not_covered`, `us_only_total`.
   A business-team canonical name counts as the same member (the checker runs the
   app's own `canonicalize_geo_label`).

## 2. Patterns and root causes (all in `app/data/repository.py` unless noted)

| # | What a reader would see | Root cause | Rule now |
|---|---|---|---|
| 1 | AppLovin 2024 US revenue 2,689 next to restated 2025 | `_newest_filing_first` grouped by label text before filing date; the FY2025 10-K re-cased "Total Revenue" → "Total revenue", so the older filing sorted first | Sort key is (period, **filing date**, labels). A rewording can no longer bring an old number back |
| 2 | Synopsys FY2022 Europe 493.4 (restated to 430.4) | `_align_members_with_total` re-read the member with the Total's element, found it only in the older 10-K | A re-read never uses a candidate from an older filing than the value it replaces (`_older_filing`) |
| 3 | Mondelez FY2022 Europe capex 355 (newest 10-K: 335) | ASU 2023-07 filings tag segment capex `SegmentExpenditureAdditionToLongLivedAssets`; undeclared, so an older declared element outranked it | Element declared for Capital Expenditure (`utils/constants.py`) |
| 4 | Campbell's capex shown as **Assets** | Metric chosen from label words: "Purchases of plant **assets**" | `_matches_metric(label, cfg, concept)`: an element declared for another metric never matches; one declared for this metric needs no keyword; exclusions always apply; undeclared elements use the label. All 6 call sites + screening pass the element |
| 5 | Amphenol "Foreign" next to China; DXC "Europe" next to United Kingdom; Fresh Del Monte "Latin America" (Central America) next to South America | The business workbook rolls residuals up ("Other foreign locations" → Foreign) — right alone, wrong beside a sibling it appears to contain | `_name_overlaps_as_filed`: a canonical name that contains another row listed in the same period goes back to the filed name, per year (Cadence: "Other Americas" 2014, "Americas" 2023) |
| 6 | ADM "United Kingdom" = Cayman Islands; the real UK figure missing | Workbook maps both to United Kingdom; first-wins kept one | `_colliding_geo_names`: names one filing lists separately with different values never merge — shown as filed |
| 7 | C3.ai / Workday / Palo Alto revenue rows named "Cost of subscription" | A member reused in the cost table arrives with its cost label | "Cost(s) of X" names the stream X, without a trailing "revenue" ("Cost of product revenue" → Product); a bare "Cost of sales" is left for the data team (`_COST_LABEL`, `_GENERIC_LINE`) |
| 8 | EPAM "Other Income Included in Segment Revenues", PVH "Restructuring and other items", Conagra "Net derivative losses allocated to Foodservice" as segments | Reconciling lines on the segment axis | `_RECONCILING_LINE` skips them |
| 9 | "Gis", "Uscs", "Fhs", "Rh", "Kfc" | `str.title()` on all-capitals labels | `_title_case_member` keeps acronyms: vowel-free tokens always, vowelled ones via `_SEGMENT_ACRONYMS` |
| 10 | AppLovin 2023-24 Total 3,283 / 4,709 under rows adding to 1,842 / 3,224 | `largest_member` / `member_sum` added every matching row of every filing, so each figure counted once per 10-K repeating it; the Total nearest that inflated sum was the superseded one | `_column_sizes`: sizes come from the values the column shows, newest filing only |
| 11 | Mondelez FY2022 capex 355 (restated 335) | `_drop_wrapped_slices` counted the "Consolidation Items: Operating Segments" wrapper as a slicing axis, so an older plain "Segments: Europe" beat the newer wrapped copy | A wrapper divides nothing; it no longer counts as an axis |
| 12 | Mondelez Europe under Business to FY2021 and under Geographic from FY2022; FY2024-25 business cells blank | A wrapper row's `dimension` names the wrapper's axis. Its place-named inner member was dropped as "a place off any segment axis", or forced to Geographic because the name also appears on the geographic axis | A wrapped row is judged as its unwrapped twin: the inner heading ("Segments") gives its axis (`_inner_heading`, `_INNER_SEGMENT_AXIS`); geography only when the inner heading is geographic, or a known place with no segment heading |

Screening: `_segment_classifier_version` now also hashes `_matches_metric`,
`_newest_filing_first`, `_colliding_geo_names`, `_name_overlaps_as_filed`,
`_keep_newest_filing`, `_reconcile_with_totals` and `SEGMENT_METRIC_GROUPS`, so
the screener's segment cache rebuilds once after any of them changes.

## 3. Results (381 companies; annual FY2012-26, quarterly 2018-26)

Every displayed cell, checked against SEC. "Before" is the app at the start of
2026-10-07 (after the first-round fixes); "after" is this round.

| Result | Annual before | Annual after | Quarterly before | Quarterly after |
|---|---|---|---|---|
| Exact (value, member, period, newest filing) | 39,031 | **44,190** | 40,489 | **45,952** |
| Value right, filed under another member name | 839 | 980 | 1,127 | 1,380 |
| US-only fallback (no geographic disclosure) | 474 | 474 | – | – |
| Older figure, later restated | 312 | **101** | 337 | **155** |
| Not in SEC | 70 | **56** | 33 | **17** |
| Wrong period | 0 | 1 | 1 | 0 |
| No SEC fact to compare (coverage) | 666 | 671 | 512 | 395 |

Annual 95.1% exact (98.1% with the right value); quarterly 95.9% (98.7%).
"Another member name" is almost entirely the business team's names and
acronyms (EMEA spelled out, "GIS" for Global Infrastructure Services); the
workbook shows the SEC member beside each one. The exact count rose by ~5,000
per view because element-first matching (rule 4) shows segment profit and capex
that label words used to miss, and every one of those matches SEC.

Per-company, per-cell results: `output/segment_sec_check/Segments vs SEC - all
companies 2026-10-08.xlsx`.

## 4. Not fixable in the app (source data)

- **Dollar Tree**: from FY2023 the DB labels the Dollar Tree segment
  "Cost of sales" (`Segments: Cost of sales`) — the ASU 2023-07 expense line's
  label was stored as the member. No member id is stored, so the app cannot
  recover the name. Data team fix.
- **Fresh Del Monte "Totals"**, **Farmer Bros "Net sales by product category"**:
  same class — a table heading stored as the member label.
- **ALMEA → "Asia, Middle East & Africa (AMEA)"** (Coty) and **Cayman Islands /
  Bermuda / British Virgin Islands → United Kingdom**: entries in the business
  team's mapping (`MANUAL_CANONICALS` in `scripts/build_geo_label_map.py`, the
  workbook). Rule 6 already separates ADM's real UK; the names themselves are a
  business decision. The workbook is not on this machine, so the map was not
  regenerated.

- **10-Q filing dates**: sampled over 10 affected companies, 1,035 of 130,393
  10-Q segment rows have no `filing_date` and 4,335 are dated before the period
  they report (LOW's Q1 FY2022 row "filed" 2021-05-27). Without a filing date the
  newest-filing rule cannot see a restatement, which is most of the remaining
  quarterly "restated" cells. Data team fix (the 10-Q filing-date writer).

## 5. Tests

`tests/test_segment_fiscal_years.py` — one test per rule above, each failing on
the code before the change. `tests/test_segment_member_values.py` expects
"USCS"/"ICS" (rule 9).

## 6. Rollout

- Restart the app (clears `st.cache_data`). Screening cache rebuilds once by itself.
- Copy the same files to the STG repo (`market-data-stg`) after review.
