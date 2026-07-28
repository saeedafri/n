# Geographic Segment Label Audit — full inventory for the data team

**Date:** 2026-07-27
**Deliverable:** `~/Desktop/geographic_segments_audit.xlsx`
**Purpose:** the data team asked for a complete, accurate list of every geographic
segment name the portal shows — overall and per ticker — so they can decide which
labels to merge.

---

## 1. Scope

`/market_data` → **Segment** tab → **Geographic Segments** table, annual (10-K) only.

## 2. Method — why the numbers are trustworthy

The extraction does **not** re-implement the label logic. It calls the application's
own code paths, in-process, against the STG DB:

| Step | Code called |
|------|-------------|
| Fetch rows | `SegmentDataRepository._fetch_all_db_rows(ticker)` |
| Classify business vs geo, unwrap multi-dim facts, canonicalise | `SegmentDataRepository._classify_segment_rows(rows, years)` |
| Alias collapse | `data.segment_aliases.canonicalize_geo_label` |

`_classify_segment_rows` is documented in the repo as the **single source of truth**
shared by the Segments tab and the screening cache, so anything in the workbook is
literally what the tab renders.

Probe scripts: `scratchpad/extract_geo.py` (extract), `scratchpad/build_excel.py` (workbook).
Universe: all **305** tickers with `is_dimensioned=1 AND doc_type='10-K'` rows in
`coreiq_filing_metrics_v5`. Runtime 287 s, 8 threads, 0 errors.

### UI verification

`bash .claude/dev/run_local.sh` + `ui_test.py --path "/market_data?ticker=COST&tab=segment_data"`.
UI Geographic Segments for COST = **Canada / Other International Operations / United States**
across Revenues, Operating Profit Before Tax, Assets, D&A — byte-identical to the
workbook's `By_Ticker` rows for COST. Screenshot: `/tmp/cost_geo.png`.

## 3. Results

| Metric | Count |
|--------|-------|
| Tickers scanned | 305 |
| Tickers showing geographic segments | 223 |
| Tickers with none | 82 |
| **Distinct geographic segment names displayed** | **357** |
| Distinct raw XBRL `dimension_member_label` values on geo axes | 801 |
| Ticker × segment rows | 1,447 |
| Merge-candidate groups | 71 (46 safe, 25 review) |
| Names used by only ONE ticker | 249 of 357 (70 %) |

Top labels: United States (187 tickers), International (58), Europe (54), Canada (52),
United Kingdom (45), EMEA (43), Other (43), Americas (39), Non-US (36), North America (36).

## 4. Findings the data team should act on

1. **Invisible-character duplicates.** `'United States'` (187 tickers) vs `'United States.'`
   (KD) vs `'United\xa0States'` (WHR — a non-breaking space). These render identically
   and will never merge on a plain string compare.
2. **Punctuation / case drift** — 46 groups that are safe merges:
   Asia Pacific / Asia-Pacific / Asia/Pacific (44 tickers), Rest of World / Rest of world (20),
   Other countries / Other Countries / Other countries, (28),
   China (including Hong Kong) in 5 different casings, Vietnam / Viet Nam.
3. **Filler-word variants** — 25 groups needing human sign-off:
   International / Total International / International Region (64 tickers),
   Europe / Total Europe, EMEA / Total EMEA, Domestic / Domestic Operations,
   Netherlands / The Netherlands, Other Americas / Americas – Other (note the en-dash).
4. **Acronyms are title-cased by the display layer** — `_title_case_member` turns
   `EMEA` → **"Emea"** (43 tickers) and `APAC` → **"Apac"** (13). That is a display bug,
   not a data-team merge decision.
5. **Pension-plan geographies on the geo axis.** 6 raw labels (`U.S. Plans` — DBD, HON,
   HPE, MDLZ, PM; plus KHC, PPC, FOSL, HAS, GOLF) are retirement-plan breakdowns filed on
   `StatementGeographicalAxis`. `GEO_PENSION_SKIP_LABELS` filters them in the **screener**
   but is **not** applied in `_classify_segment_rows`; they only stay out of the tab today
   because they don't match a `SEGMENT_METRIC_GROUPS` entry. That is incidental, not a guard.
6. **Long tail.** 70 % of names are used by a single ticker — merging should be driven by
   the `Merge_Candidates` sheet, not by eyeballing the master list.

## 5. Workbook layout

| Sheet | Contents |
|-------|----------|
| `README` | method, counts, caveats |
| `Geo_Segments_Master` | 1 row per displayed name: ticker count, ticker list, years, metrics, normalised keys |
| `By_Ticker` | 1 row per ticker × segment: metrics, year span, every year |
| `Merge_Candidates` | 71 clusters, split "SAFE merge" vs "REVIEW", with suggested canonical |
| `Raw_Labels_Master` | 801 raw XBRL labels → what the app displays them as, axis, units |
| `Raw_Labels_By_Ticker` | same per ticker, with sample line-item labels and full dimension labels |
| `Tickers_No_Geo` | 82 tickers, and whether raw geo rows exist but get filtered out |
| `Errors` | empty |

## 6. Caveats

- Annual 10-K facts only; 10-Q segment members are excluded.
- "Displayed" is post-alias-canonicalisation. The six existing canonical groups
  (United States, Canada, United Kingdom, North America, Non-US, Americas) are already
  merged and flagged in the master sheet.
- No DB writes were made. Read-only probes throughout.
