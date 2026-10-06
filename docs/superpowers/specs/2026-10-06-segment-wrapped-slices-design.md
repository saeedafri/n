# Segments tab showed a slice of a segment as the whole segment

## Symptom

Chipotle (CMG) Market Data → Segments: the U.S. segment read **$59m** for FY2025,
while the 10-K reports U.S. segment total revenue of **$11,679m**.

## Is the extractor at fault? No.

All of CMG's facts are in `coreiq_filing_metrics_v5`, correctly:

| value ($m) | full_dimension_label |
|---|---|
| 11,679.417 | Consolidation Items: Operating Segments, Segments: U. S. Segment |
| 11,620.085 | Consolidation Items: Operating Segments, Product and Service: Food and beverage revenue, Segments: U. S. Segment |
| 59.332 | Consolidation Items: Operating Segments, Product and Service: Delivery service revenue, Segments: U. S. Segment |

All three carry `dimension = srt:ConsolidationItemsAxis`,
`dimension_member_label = Operating Segments`, `dimension_label = U. S. Segment`.

## Root cause

`_classify_member` unwraps the generic "Operating Segments" wrapper to the inner
segment (`dimension_label`). It ignored any **further** axis, so the whole segment
and its two Product-and-Service slices all became the same member "U. S.". The
member merge is first-wins (declared element first), and the delivery slice came
first — so the tab showed $59m as the U.S. segment.

This is not CMG-specific. A 387-ticker sweep (old vs new classifier on the same STG
rows) found 75 tickers affected, e.g. NIKE Brand revenue FY2021 $25m (now
$42,293m), GE Aerospace FY2022 $1,713m (now $26,050m), CVS Pharmacy Services FY2019
$52,141m (now $141,491m), Verizon Business FY2018 $11,197m (now $31,534m).

## Fix

`SegmentDataRepository._drop_wrapped_slices(rows)` — a pre-pass run at the top of
`_classify_segment_rows` (annual tab + screening cache) and
`_build_segment_tables_quarterly`:

* applies only to wrapped facts (`dimension_member_label` in
  `_SEGMENT_WRAPPER_MEMBERS`);
* groups them by **(metric, declared-element?, period, segment)** and keeps only
  the copies with the fewest axes.

Why each part of the key matters (each was a measured regression of a simpler rule):

| simpler rule | what broke |
|---|---|
| drop every 3+-axis wrapped fact | 1,024 real cells blanked: Pulte's Florida/Texas, Nike's North America, Abbott's Molecular exist **only** on a further Subsegments axis |
| key on XBRL element | CVS slices Pharmacy Services under a second revenue element with no whole twin → still a slice (0.0) |
| key on metric only | GE Power's Total assets (us-gaap:Assets) is filed with 4 axes; a 2-axis contract-asset fact (filer element) matching "Assets" displaced it (24,453 → 838) |

Axis count = number of `", <Heading>: "` separators in `full_dimension_label`
(a member containing a comma, e.g. "Europe, the Middle East and North Africa", is
not a separator because no `": "` follows before the next comma).

`_drop_wrapped_slices` is added to `screening_service._segment_classifier_version`,
so the screening segment values cache rebuilds itself after deploy.

## Results (final rule, 387 tickers)

* 75 tickers, 1,161 cells changed; **0 blanked**, 0 errors.
* 1,124 rose (slice → whole segment); 37 fell — each checked: restatement slices
  (ADM "Revised for transfer price"), product slices (PZZA commissary sales; new
  value equals the plain `Segments: X` fact), elimination lines.
* GLW Life Sciences 2019 unchanged (still the old 550 slice): its whole uses an
  undeclared filer element; GLW source data is a known ingestion problem.

## Testing

`tests/test_segment_member_values.py`:
* `test_the_segment_reads_its_own_figure_whatever_order_the_slices_arrive_in` (CMG shape; fails without the fix)
* `test_a_segment_filed_only_with_a_further_axis_keeps_its_figure` (Abbott/Pulte/Nike guard)
* `test_a_lookalike_measure_with_fewer_axes_does_not_displace_the_segment` (GE guard)
* version-hash test now requires `_drop_wrapped_slices`.

Segment suites 59/59. Full suite: only pre-existing failures
(`test_transcript_search_single_company`, `test_screening_keydevs_columns`).
UI: `http://localhost:8501/market_data?ticker=CMG&tab=segment_data` shows U. S.
8,516.21 / 9,720.37 / 11,111.73 / 11,679.42 (FY2022–2025).

## Rollout

Code-only (`app/data/repository.py`, `app/data/screening_service.py`). Segments tab
is `st.cache_data(ttl=3600)` → fresh on deploy. Screening segment cache rebuilds
once on its own (classifier version hash changed). No DB writes.

## Not fixed here (flagged separately)

CMG member-name drift ("Food and Beverage" FY2018–23 vs "Food and beverage
revenue" FY2022–25 as two rows), "Gift card liability" / "Deferred licensing
revenue" listed under Revenues, "U. S." not canonicalised to "U.S.", and the U.S.
segment surviving only because a junk member ("Corporate and other unallocated
expenses") qualifies the wrapper axis as a segment axis.

---

# Follow-up (same day): duplicate member names and junk rows

Chipotle's Business Segments → Revenues still listed each product twice and four
liability lines as if they were revenue.

## 1. Contract liabilities listed as revenue

"Chipotle Rewards", "Gift Card", "Gift card liability", "Deferred licensing
revenue" are `us-gaap:ContractWithCustomerLiabilityCurrent` /
`cmg:…RevenueRecognizedBreakage` facts labelled "Unearned revenue", "Liability in
unearned revenue", "Breakage revenue". The Revenues metric matches labels, and these
slipped past its existing excludes ("deferred", "recognized", "contract with
customer, liability").

**Fix:** `SEGMENT_METRIC_GROUPS["Revenues"]["db_exclude"]` += `"unearned"`,
`"breakage"` (`app/utils/constants.py`).
**Sweep (isolated):** 7 tickers change, every removed member a liability line —
BOOT, BROS, CMG, DRI (gift card), RRGB (unearned royalty/loyalty), WDAY (refundable
professional services), ALLY (members named "2022-01-01"…).

## 2. One product listed twice after a relabel

Chipotle renamed "Food and Beverage" / "Delivery Service" (FY2018-23) to "Food and
beverage revenue" / "Delivery service revenue" (FY2024-25). `_member_key` is literal
by design, so each product showed as two half-empty rows.

**Fix (`_member_display_map`):** two spellings of a section whose keys differ only by
a trailing `revenue(s)`/`sales` word are one member **iff** they report at least one
common (metric, fiscal year) and the identical set of numbers in every (metric,
fiscal year) they share. A merge therefore can never drop a figure.
**Sweep (isolated):** 14 tickers change, 13 with no number changed (CMG, ORCL, V,
TMUS, GEV, WING, QSR-style relabels). ADSK FY2019: both spellings report the same
ASC 606 set {1,802.3, 1,785.7, −16.6}; the one row now shows the first-filed figure,
as every other member does.

### Rejected approaches (measured, then reverted)

| approach | result on the 387-ticker sweep |
|---|---|
| merge on the XBRL member QName (`member` column) | 224 tickers changed; real segments folded (CENT "Pet" into corporate, AMZN, ABT) |
| QName + equal numbers on shared facts | 163 changed, 59 lost numbers |
| QName, single-axis facts only | 54 still lost numbers (KMB North America/IPC, WHR MDA Asia, TOL Home Building) — the stored QName does not reliably identify the displayed member |
| strip trailing revenue/sales from `_member_key` | 41 changed, 21 lost numbers (TSLA "Automotive" segment vs "Automotive sales" line) |

## 3. "U. S." → "U.S."

`_strip_member_artifact` removes the space inside initials (display only).
**Rejected:** canonicalising the suffix-stripped name through the geographic
workbook — it groups places into regions and folded PriceSmart's four country
segments into one "Latin America and the Caribbean" row, and INGR's Asia Pacific /
EMEA.

## Verification

* Segment suites 63/63; full suite only the pre-existing
  `test_transcript_search_single_company` failure.
* UI `http://localhost:8501/market_data?ticker=CMG&tab=segment_data`: Revenues =
  Delivery service revenue (64.09 … 59.55), Food and beverage revenue (5,920.55 …
  11,866.05), U.S. (8,516.21 … 11,679.42), Total — no liability rows.
* Screening cache rebuilds itself (`_member_display_map` is in the classifier hash).

## Still open (not in scope)

BOOT lists "Allowance for Sales Returns" (a contra account) under Revenues, before
and after. The edgartools fallback path (`_process_facts`) has neither rule.

### Quarterly view (found during the STG sync)

10-Q rows carry no `report_fiscal_year`, so the relabel agreement test keyed on
fiscal year saw every quarter as one `None` year and never merged in the quarterly
table. A 3-month fact's cell is now its quarter end. Re-sweep: annual results
identical (14 tickers, only ADSK's ASC 606 set changes a shown figure); quarterly on
those 14 tickers — 7 de-duplicate (BLMN, CMG, DIN, TMHC, TMUS, V, WING) with zero
numbers lost or added. Test: `test_a_relabel_is_one_row_in_the_quarterly_view_too`.
