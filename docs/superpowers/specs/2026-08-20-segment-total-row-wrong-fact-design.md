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

## 6. Member rows — three further defects fixed 2026-08-23

Auditing the Totals fleet-wide exposed defects in the *member* rows they sit under.

**6.1 Real segments were being discarded.** Facts tagged
`ConsolidationItems=Operating Segments` + `BusinessSegments=<segment>` were only
unwrapped when the inner member was a *geography*. AMD's Datacenter, Client,
Gaming and Embedded were thrown away, leaving a table whose only "segments" were
reconciliation lines — and TJX had **no Business Segments table at all**
(Marmaxx, HomeGoods, TJX Canada, TJX International all dropped). Now the inner
member is recovered whatever it names; a wrapper with no inner member is still
the roll-up and is still dropped.

**6.2 Reconciliation rows listed as segments.** "Segment Reconciling Items",
"Segment Reporting, Reconciling Item, Excluding Corporate Nonsegment",
"Corporate, Non-Segment" and roll-ups like "Total Wholesale" were rendered as
members. They bridge segments to the consolidated figure — which is what the
Total already shows. Dropped, extending the policy `SEGMENT_SKIP_MEMBERS` already
applied to eliminations and corporate.

**6.3 One segment listed two or three times.** Labels drift between filings:
Ingredion files "Asia Pacific Segment", "Asia- Pacific" and "Asia-Pacific" for
one segment, and "F&II - LATAM" then "F&II–LATAM" for another; O'Reilly files
"Automotive Aftermarket Parts Segment" and "…parts segment"; PriceSmart embeds
invisible bidi marks. `_member_key` collapses punctuation, spacing, `&`/"and",
invisible characters and a trailing "Segment"; the newest filing's spelling is
displayed. It is deliberately literal — "Client" and "Client and Gaming" remain
two segments.

Related: `str.title()` was mangling acronyms ("F&II–LATAM" → "F&Ii–Latam", "EMEA"
→ "Emea"). Fixed without disturbing "UNITED STATES" → "United States".

## 7. Metric matching — two leaks fixed

- **D&A absorbed cost lines.** EPAM and Steve Madden file "Cost of revenues
  (exclusive of depreciation and amortization)"; matching on "depreciation"
  pulled a cost line worth 20× real D&A into the metric. `db_exclude` now carries
  "exclusive of", "excluding" and "cost of" — the Revenues group has excluded
  "cost of" all along.
- **Assets absorbed anything containing the word.** Jack in the Box's segment
  "assets" were "Assets held for sale" and "Amortization of favorable and
  unfavorable lease assets". `db_exclude` now carries "held for sale" and
  "amortization".

## 8. Assets is a balance, not a flow

`"assets"` matched any label containing the word, so flows were rendered as
balances: Darling's entire segment "Assets" table was "Gain on sale of assets",
and Constellation's Total came from "Net income tax provision (benefit) on
disposition of assets". Gains, losses, impairments, proceeds, disposals,
right-of-use additions, derivative and fair-value lines are now excluded, while
"Total assets", "Long-lived tangible assets" and "Operating assets" still match.

## 9. Revenue: the income statement is a fallback, not an override

`coreiq_av_financials_income_statement` used to overwrite the Revenues Total. That
was a workaround from when the XBRL total was label-matched and unreliable — the
very bug §2 fixes — and it let a partial-year ingest win: Coherent's FY2026 row
reads $2,045.5m against $7,118.2m in its own 10-K, under members totalling $7.1bn.
The filing the segments come from now wins; the income statement fills years the
filing has no consolidated revenue fact for.

This corrected 259 revenue cells across 86 tickers, verifiably: GE FY2021
74,196 (was 56,469), JNJ FY2022 94,943 (was 78,740), Dollar Tree FY2024 30,581.6
(was 16,770.3), and the banks now reconcile to their segments — Goldman 58,283
(net revenues, exactly its segments) instead of 126,853 (gross interest income).

## 10. When the members disprove the Total

Two guards run after selection, both in `repository.py`:

**`_drop_offmeasure_members`** blanks member values that measure something other
than the Total. Roper files segment assets three ways — `us-gaap:NoncurrentAssets`
plus two Roper-defined elements — so $577.6m of *operating* assets sat under a
$187.1m *long-lived* assets Total. Values are judged per period, not per member:
Asbury tags its TCA segment with the standard D&A element in some years and a
deferred-acquisition-cost element in others, so dropping the whole member would
lose good years. Only filer-defined elements go, and only when the Total settled
on a standard element that some member also uses — a company reporting a metric
exclusively with its own elements keeps everything.

**`_suppress_impossible_totals`** withholds a Total its own members disprove.
Corning's consolidated long-lived assets are stored as $68m against $44.7bn of
its own geographic members — a scale error in `coreiq_filing_metrics_v5` that no
selection rule can see, because the fact carries the right concept and the right
period. A consolidated figure is never smaller than one of its parts, so the row
is withheld and the members stay on screen. The comparison is like for like —
only members tagged with the Total's own concept — and it never runs on Operating
Profit (a segment can out-earn the company once unallocated corporate costs come
out) or where a comparable member is negative (eliminations).

## 11. Results of the full sweep

Universe check first — no ticker with segment data sits outside the sweep:

| | |
|---|---|
| Tickers with dimensioned 10-K rows in `coreiq_filing_metrics_v5` | 357 |
| Tickers swept (`coreiq_companies`) | 428 |
| Tickers with segment data **not** swept | **0** |

| | |
|---|---|
| Total cells rendered | 5,733 across 341 tickers |
| Cells whose Total changed | **2,573 across 267 tickers** |
| — corrected to the filed value | 2,425 |
| — row withheld (never filed, de-duplicated to one member, or disproved) | 148 |
| Builder errors | **0** |
| Tests | 32 for this behaviour, 119 in the suite |

Verified against the filings:

| Ticker | Table | Metric | Year | Was shown | Now |
|---|---|---|---|---:|---:|
| TJX | Geographic | Assets | 2025 | 31.00 | 7,346.00 |
| TJX | Business | — | all | *table absent* | Marmaxx, HomeGoods, TJX Canada, TJX International |
| AMD | Business | Operating Profit | 2022 | 1,031.0 | 1,264.0 |
| INGR | Geographic | Operating Profit | 2025 | 1,028.0 | 31.0 |
| CAT | Business | Assets | 2021 | 4,407.0 | 82,793.0 |
| ANDE | Business | Revenues | 2021 | 2,211.5 | 12,612.0 |
| GS | Business | Revenues | 2025 | 126,853.0 | 58,283.0 |
| GE | Business | Revenues | 2021 | 56,469.0 | 74,196.0 |
| UAA | Geographic | Assets | 2021 | 2.0 | 1,055.6 |
| GLW | Geographic | Assets | 2021 | 30,154.0 | *withheld — filed value is corrupt* |

### Residual

Of 5,733 cells, 52 (0.9%) show a Total below the largest member and **all 52 are
Operating Profit**, where that is correct — AMD FY2025 (segments 3,482 + 897, total
1,900), AEO FY2021's real -271.3 COVID operating loss. **No non-profit metric is
left flagged.**

Two source-data faults remain in `coreiq_filing_metrics_v5` and still want an
ingestion fix, even though the app no longer prints them: **GLW** stores
consolidated `us-gaap:NoncurrentAssets` as $67–83m for FY2022 where the FY2016/17
filings hold $16–18bn, and **COHR** carries a geographic "asset" of $737,151m.

## 12. Incident 2026-08-24 — NameError on the Quarterly tab

`?ticker=TJX&tab=segment_data&period_type=Quarterly` showed "Something went
wrong. Please try again." on staging at 11:22.

```
NameError: name 'member_concepts' is not defined
repository.py:_build_segment_tables_quarterly:11163
```

**Cause.** The tab has two parallel builders. `member_concepts` was declared in
the annual builder and used in both, so the quarterly path raised on every render.

**Why the tests missed it.** All 36 tests exercised `_classify_segment_rows`, the
annual path. The quarterly builder — a near-copy that every change in this spec
also touched — was never executed by the suite, and the 428-ticker sweeps only
ever called the annual builder. Breadth of tickers hid absence of path coverage.

**Fixed, and made unrepeatable:**
- `member_concepts` declared in the quarterly builder.
- `test_the_quarterly_builder_runs_and_totals_correctly` runs
  `_build_segment_tables_quarterly` with fabricated 10-Q rows, so the suite now
  executes both builders.
- `test_both_builders_declare_everything_they_use` runs pyflakes over
  `repository.py` and fails on any undefined name — this exact class of bug
  (declared in one builder, used in the other) can no longer ship.
- Both tests were confirmed to fail with the bug reintroduced.
- Fleet smoke test of the quarterly builder: **428 tickers, 0 exceptions** (337
  render quarterly data, 91 have none).

## 12. Rollout

Code-only change — no schema change, no data migration, no writes. The Segments
tab caches (`@st.cache_data(ttl=3600)` on `get_segment_data`, `ttl=21600` on
`_fetch_all_db_rows`, and the per-render `st.session_state` HTML cache) mean the
corrected totals appear within one TTL of deploy, immediately on a fresh session.
