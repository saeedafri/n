# Segment "Total" row shows an unrelated fact — root cause & fix

**Date:** 2026-08-20
**Reported by:** internal user, on `/market_data` → TJX → **Segment** tab
**Severity:** Critical — a wrong number is displayed as a filed total, in the UI and in the Excel export
**Files touched:** `app/data/repository.py`, `app/pages/market_data.py`, `tests/test_segment_totals.py`

---

## 1. The report

TJX → Segment → Geographic Segments → **Assets**:

| For Fiscal Period Ending | Jan-31-2021 | Jan-31-2022 | Jan-31-2023 | Jan-31-2024 | Jan-31-2025 |
|---|---:|---:|---:|---:|---:|
| Australia | 51.78 | 55.34 | 68.00 | 75.00 | 72.00 |
| Canada | 241.09 | 247.51 | 274.00 | 341.00 | 364.00 |
| Europe | 898.52 | 927.02 | 923.00 | 1,028.00 | 1,041.00 |
| United States | 3,844.71 | 4,040.95 | 4,518.00 | 5,127.00 | 5,869.00 |
| **Total (shown)** | **57.45** | **5,270.83** | **73.00** | **40.00** | **31.00** |
| *Correct total* | *5,036.10* | *5,270.83* | *5,783.00* | *6,571.00* | *7,346.00* |

Four of five columns were wrong. FY2022 was right by accident.

## 2. Root cause

The Segments tab deliberately does **not** sum the member rows — it shows the
consolidated value the company actually filed, so the Total ties to the filing
even when members don't cover the whole company
(`app/pages/market_data.py`, `_build_table`: *"use exact filed (non-dimensioned)
total, never sum members"*).

That filed total was selected by **substring-matching the metric name against
every non-dimensioned label in the filing**, first match wins
(`SegmentDataRepository._classify_segment_rows` → `_matches_metric`):

```python
"Assets": {"db_include": ["total assets", "assets"],
           "db_exclude": ["net assets", "total current assets", "intangible assets",
                          "deferred tax", "other assets", "operating lease"]}
```

Two failures compound:

1. **`"assets"` matches far more than an assets total.** Any label containing the
   word qualifies — including cash-flow movements and tax-footnote lines.
2. **"First match" is alphabetical, not semantic.** Rows are ordered
   `(report_fiscal_year, full_dimension_label, original_label)`; non-dimensioned
   rows have an empty `full_dimension_label`, so they sort by label. A label
   starting with `(` sorts before every letter and wins.

What actually won for TJX (verified against STG `coreiq_filing_metrics_v5`):

| Metric | Label that hijacked the Total | Concept | FY2025 value |
|---|---|---|---|
| Assets | `(Increase) in prepaid expenses and other current assets` | `IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets` | 31.00 |
| Depreciation & Amortization | `Amortization of loss on cash flow hedge, taxes` | `OtherComprehensiveIncomeLossCashFlowHedgeGainLossReclassificationTax` | 0.30 |
| Operating Profit Before Tax | `Net operating loss carryforward` | `DeferredTaxAssetsOperatingLossCarryforwards` | 104.00 |

The correct fact was in the same table all along:
`Carrying values of long-lived assets` / `us-gaap:NoncurrentAssets`, which equals
the member sum **to the cent in every year**.

FY2022 looked correct only because that filing labelled the cash-flow line
`Decrease (increase) in …` — a `D`, which sorts *after* `Carrying values …`.

Revenues escaped the bug because `_build_segment_tables_from_db` overwrites the
Revenues total with the income-statement value. The other four metric groups had
no such backstop.

### Second defect found on the way

The Excel export computed its Total as the **sum of members** while the screen
showed the label-matched fact — the same table exported two different numbers.

### Third defect: one totals dict for two tables

`metric_totals` was keyed by metric only and handed to both tables. The two
tables can split *different facts* under one metric name — TJX's geographic
"Assets" are long-lived assets (`NoncurrentAssets`), its business "Assets" would
be balance-sheet assets (`Assets`). A single shared total is wrong for at least
one of them whenever a company reports both.

### Why summing the members is not the answer

Filers routinely tag two overlapping breakdowns in one footnote, so the member
list covers the company more than once:

- **NKE** business revenue members are Footwear + Apparel + Equipment (the whole
  company) *and* Direct-to-Consumer + Wholesale (the whole company again) — the
  member sum is exactly 2× revenue.
- **CAT** geographic revenue members are Inside US + Non-US *and* North America /
  Asia-Pacific / EAME / Latin America — again 2×.
- **CAT** business revenue adds subsegment roll-ups and elimination columns.

A summed Total would be wrong for every one of these. The filed consolidated fact
is the only defensible Total, which is what the tab was always trying to show.

## 3. The fix — anchor the Total to the members' own XBRL concept

A Total is only a Total if it is the **same XBRL concept the members are tagged
with**. Every dimensioned member fact carries `concept`; so does every
non-dimensioned fact. The concept is the join key; the label never was one.

`_classify_segment_rows` now runs in two passes:

1. **Members.** Unchanged classification (wrapper-unwrap, geo routing,
   canonical geo labels, first-wins recency), plus it records the set of
   concepts each `(section, metric)` is built from and the periods those members
   cover.
2. **Totals.** Every non-dimensioned fact is *ranked* as a candidate
   (`_rank_total_candidate`) and the best one wins the cell:

   | # | Rule | Why it is needed |
   |---|------|------------------|
   | 1 | the members' own concept **and** an element declared for the metric in `SEGMENT_METRIC_GROUPS` | AMD tags a member "Operating income related to licensed IP" with `amd:GainLossOnLicensingAgreement`, so the member concepts alone let a $102m licensing gain outrank $1,264m of operating income |
   | 2 | the members' own concept | the plain case, and the only evidence when a filer uses an element the metric never declared (TJX: `NoncurrentAssets`) |
   | 3 | covers a period the members cover | a 10-K also carries quarterly and prior-year consolidated facts; without this a stray context can win |
   | 4 | newest filing | a restatement supersedes the original |

   A fact that is neither the members' concept nor a declared equivalent of it is
   not a candidate at all. The declared-equivalent bridge exists for filers whose
   segment rows and consolidated row use different elements for one measure
   (T-Mobile: `DepreciationAndAmortization` vs `DepreciationDepletionAndAmortization`);
   it can only cross concepts that `SEGMENT_METRIC_GROUPS` already names, so it
   cannot reopen the label-matching hole.

Consequences:

- A fact that merely shares a word with the metric can never be a Total again.
- Business and Geographic get **separate** totals (`metric_totals` is now
  `{"business": {...}, "geo": {...}}`).
- When a company filed no consolidated value for the concept its members split,
  **no Total row renders** — `has_total` is already false-safe in the renderer.
  A missing row beats a confident wrong number.
- The income-statement Revenues override still applies, now to both sections.

Same change applied to the quarterly builder (`_build_segment_tables_quarterly`),
which had an identical copy of the bug.

Both non-dimensioned queries selected `NULL AS concept`; they now select the real
column. `screening_service._SEGMENT_CACHE_FETCH_COLS` already included `concept`,
and the screening cache ignores `metric_totals`, so the screener is unaffected.

Excel now renders the same Total as the screen — filed value when there is one,
member sum as a fallback.

### Rejected alternatives

| Option | Why not |
|---|---|
| Sum the members | Loses the point of the row: members often exclude corporate/unallocated, and CapIQ shows the filed total. Would also silently disagree with the filing. |
| Extend `db_exclude` with the offending phrases | Whack-a-mole across 431 tickers × every label variant a filer invents; the FY2022 `Decrease (increase)` vs FY2023 `(Increase) decrease` drift shows the labels move filing to filing. |
| Rank candidate labels by a "looks like a total" score | A heuristic on top of a heuristic. `concept` is the authoritative identifier and is already in the table. |

## 4. Blast radius

Measured by rebuilding the Segments tab for all 431 tickers in
`coreiq_companies` (FY2021–FY2026) and diffing the old label-matched Total
against the new concept-anchored one, for every Total row the UI actually
renders (metric with ≥2 members). Results in §6.

## 5. Testing

- `tests/test_segment_totals.py` — four database-free tests pinning the rule:
  the TJX shape with both decoy facts present, business/geo separation, no-Total
  when nothing was filed, and recency on a restated total.
- Live rebuild against the STG database for TJX: Total now
  `5,036.10 / 5,270.83 / 5,783 / 6,571 / 7,346 / 8,220`, equal to the member sum
  in every year.
- UI verification with `.claude/dev/ui_test.py` on `/market_data`.

## 6. Results of the full sweep

Universe check first — no ticker with segment data sits outside the sweep:

| | |
|---|---|
| Tickers with dimensioned 10-K rows in `coreiq_filing_metrics_v5` | 357 |
| Tickers swept (`coreiq_companies`) | 428 |
| Tickers with segment data **not** swept | **0** |

Every Total cell the tab renders (ticker × table × metric × year, metrics with ≥2
members), old label-matched value vs new ranked value:

| | |
|---|---|
| Total cells rendered | 4,423 across 326 tickers |
| Cells whose Total changed | **1,568 across 216 tickers** |
| — corrected to the filed value | 1,494 |
| — Total row removed (never filed) | 74 |
| Cells unchanged | 2,855 |
| Builder errors | **0** |

Changed cells by metric and table:

| Metric | Geographic | Business |
|---|---:|---:|
| Assets | 559 | 302 |
| Depreciation & Amortization | 64 | 384 |
| Operating Profit Before Tax | 55 | 127 |
| Capital Expenditure | 6 | 56 |
| Revenues | 4 | 11 |

Samples of what users were seeing:

| Ticker | Table | Metric | Year | Was shown | Now |
|---|---|---|---|---:|---:|
| TJX | Geographic | Assets | 2025 | 31.00 | 7,346.00 |
| INGR | Geographic | Operating Profit | 2025 | 1,028.0 | 31.0 |
| PLCE | Business | D&A | 2024 | 1,157.2 | 47.2 |
| CAT | Business | Assets | 2021 | 4,407.0 | 82,793.0 |
| AMD | Business | Operating Profit | 2022 | 1,031.0 | 1,264.0 |
| MNST | Business | Operating Profit | 2022 | 19.9 | 1,584.7 |

The 74 removed rows were wrong before, not lost coverage — e.g. Tyson's
geographic assets Total read **60.0** against members of **26,500**. Each was
confirmed against the database: no non-dimensioned fact exists carrying the
concept its members split.

## 7. Two defects found alongside this one — NOT fixed here

Both are member-side and pre-existing; they change what rows appear, so they need
their own change and their own verification.

1. **Duplicate and wrapper member rows.** Members are matched to a metric by
   label substring, and business-segment labels are never canonicalised the way
   geographic ones are (`segment_aliases.py`). Ingredion therefore lists
   "Asia Pacific Segment", "Asia- Pacific" and "Asia-Pacific" as three separate
   members of the same metric; AMD's only two "segments" for operating profit are
   "Segment Reconciling Items" and "Segment Reporting, Reconciling Item,
   Excluding Corporate Nonsegment" — reconciliation wrappers, not segments.
   **238 cells across 34 tickers** carry at least one such wrapper row. This is
   why a member sum can differ wildly from a correct Total.

2. **Corrupt source rows for GLW.** `coreiq_filing_metrics_v5` holds Corning's
   consolidated `us-gaap:NoncurrentAssets` for FY2022 as **$67–83m** where the
   FY2016/FY2017 filings record **$16–18bn**, and its own geographic members for
   the same year total ~$11bn. The Total row now faithfully shows the filed fact,
   so it shows 68.0. This is a data-ingestion bug for the data team — the app
   must not paper over it, and this repo does not write to the database.

## 8. Rollout

Code-only change — no schema change, no data migration, no writes. The Segments
tab caches (`@st.cache_data(ttl=3600)` on `get_segment_data`, `ttl=21600` on
`_fetch_all_db_rows`, and the per-render `st.session_state` HTML cache) mean the
corrected totals appear within one TTL of deploy, immediately on a fresh session.
