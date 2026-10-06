# Segments tab — wrong member values and a missing segment

**Date:** 2026-10-06
**Reported on:** McDonald's (MCD), Segment tab, Business Segments
**Files changed:** `app/data/repository.py`, `app/data/screening_service.py`,
`tests/test_segment_member_values.py` (new)

---

## 1. What was wrong

The Business Segments table for MCD showed two members instead of three, and the
Revenues row for both of them held numbers two to three orders of magnitude too
small, while the Total row was correct:

| FY2024 | shown | filed |
|---|---|---|
| International Developmental Licensed Markets & Corporate | 158.00 | 2,661.0 |
| International Operated Markets | 3.00 | 12,628.0 |
| U.S. | *absent* | 10,631.0 |
| **Total** | **25,920.00** | 25,920.0 |

Two independent faults, both in `SegmentDataRepository`, both affecting every
company — not only McDonald's.

---

## 2. Root cause A — the member took an element that was not the metric

`SEGMENT_METRIC_GROUPS["Revenues"]["db_include"]` matches a label containing
"sales". McDonald's files `us-gaap:GoodwillPeriodIncreaseDecrease` under the
label **"Net restaurant purchases (sales)"** on the same segment axis as
`us-gaap:Revenues` / "Total revenues".

Both matched the Revenues group. Rows reach the classifier sorted by
`(report_fiscal_year, full_dimension_label, original_label, filing_date DESC)`
and the merge is first-wins, so **"Net restaurant purchases (sales)" sorted
before "Total revenues"** and won every member cell.

The Total row was right because `_rank_total_candidate` already prefers an
element the metric declares (`edgar_concepts`). Members had no such rule.

### Fix

`_classify_segment_rows` (annual and quarterly builders): a row whose concept is
declared for the metric overwrites a value stored from a concept that is not.
First-wins still decides between two declared rows, and between two undeclared
ones — so a filer that reports a metric exclusively with its own elements is
untouched.

### Measured effect

Replaying 67 cached tickers through the old and new code (identical inputs,
only this hunk differing): **306 member cells changed across 22 tickers, none to
`None`.** Spot checks against the filings:

| ticker | member / metric | before | after |
|---|---|---|---|
| ALLY | Ally Bank, Assets 2014 | 7,541 | 104,400 |
| ANET | Product, Revenues 2024 | 61.6 | 5,884.0 |
| AVGO | APAC, geo Revenues 2025 | 559 | 35,896 |
| ABBV | Imbruvica, Revenues 2020 | 1,009 | 4,305 |
| BJ | Membership, Revenues 2026 | 253.3 | 499.8 |

---

## 3. Root cause B — a reportable segment was deleted for being a place

`_classify_member` dropped any member that `names_a_place()` recognises when it
arrived on a business axis. That rule exists because 86 companies file
geography there by mistake, and it is right for them.

McDonald's is not one of them. Its three ASC 280 reportable segments are
**U.S.**, **International Operated Markets** and **International Developmental
Licensed Markets & Corporate**, all on `us-gaap:StatementBusinessSegmentsAxis`.
The rule deleted the largest segment of the company from all five metrics while
the Total kept counting it. Amazon lost **North America** and **International**
the same way — two of its three segments.

### Fix

`_segment_axes(rows, geo_member_set)` → the set of axes carrying at least one
member the classifier keeps as business. A member the classifier keeps is by
definition not a place, so such an axis is a real segment breakdown and the
places on it are segments. An axis carrying nothing but places is still
geography filed in the wrong place, and still goes.

A place kept this way takes the canonical spelling the geographic table gives
one (`canonicalize_geo_label`): McDonald's renamed the same segment from
"United States" to "U.S." in 2021, and without this the table lists both, each
holding only the years the other is missing.

### Measured effect

Over 157 cached tickers the member lists changed for **9, every change an
addition, none a removal, and no geographic member affected**:

```
ADM  +Asia          AMZN +International, +North America   BLMN +International
C    +Asia, +Latin America    CAG +International          CL   +Asia
CLX  +International  CMG +U. S.                           MCD  +United States
```

---

## 4. Root cause D — members and Total measured different things

Found while verifying §3 in the UI. American Express's Total was right after the
bank element was added (§9), but every *member* was still wrong, because a member
often carries the metric **several ways at once** and nothing in the label tells
them apart. USCS, FY2025:

| element | value |
|---|---|
| `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax` | 15,626 ← shown |
| `us-gaap:NoninterestIncome` | 22,307 |
| `us-gaap:RevenuesNetOfInterestExpense` | **34,814** ← what the Total is built from |
| `axp:TotalRevenuesNetOfInterestExpenseAfterProvisionsForLosses` | 31,847 |

First-wins sorts on `original_label`, so "Revenue from contracts with customers"
beat "Total revenues net of interest expense" and the table listed $15.6bn of a
segment under a $72.2bn Total.

### Fix

`_align_members_with_total` runs after the Total is chosen and re-reads each
member with the element the Total settled on. AXP FY2025 now reads
USCS 34,814 · CS 16,926 · ICS 13,000 · GMNS 7,759 · Corporate −270 =
**72,229**, the filed Total exactly.

Three guards, each added after a measured regression:

1. **Only a tie between declared elements.** A member holding an element the
   metric never declared is measuring something else; swapping it for a stray
   fact that shares the Total's element replaced real revenue with a rounding
   line (DXC segment D&A).
2. **Unambiguous candidates only.** AbbVie files HUMIRA 2017 three times under
   one element — US 12,361, international 6,066 and the 18,427 total, separated
   by a further axis. v1 of this rule showed 6,066.
3. **Same axes as the value being replaced.** v2 still turned The Andersons'
   Rail revenue (172.1) into its Canadian slice (13.3, `Segments: Rail,
   Geographical: Canada`) and Chevron's Oil and Gas (157,505) into its Chevron
   Phillips joint venture (9,063).

### Measured effect

**199 cells across 34 of 242 replayed tickers.** The outliers it repairs:

| ticker | member / metric | before | after |
|---|---|---|---|
| ALLY | Automotive Finance, Revenues 2019–21 | **0.0** | 4,390 / 4,488 / 5,460 |
| ADM | Ag Services, D&A 2024 | 14 | 376 |
| AAPL | Services, Revenues FY2017 | 29,980 | 32,700 |
| AXP | USCS, Revenues 2025 | 15,626 | 34,814 |

Applied in both the annual and the quarterly builder.

---

## 5. Which table a place-named segment belongs in

Decided with the product owner on 2026-10-06: **it stays in Business Segments**,
where the filer put it and where Capital IQ shows it. The evidence:

* McDonald's geographic axis carries no revenue at all — only
  `us-gaap:LineOfCredit` foreign bank borrowings. Its geography *is* the segment
  disclosure, so there is nothing to double-count against.
* Amazon publishes both cuts: segments (North America / International / AWS) and
  an enterprise-wide geographic breakdown (United States / Germany / United
  Kingdom / Japan / Rest of world). North America ≠ United States, so merging
  them into one table lists 426bn beside 490bn and nothing reconciles.

What the screener does instead is in §6.

---

## 6. Screening — a geography criterion must find these companies

`_segment_cache_entries(ticker, biz, geo)` (new, extracted from
`_segment_entries_for_tickers`) builds the rows of
`coreiq_screening_segment_values_cache`. A **business** member that names a
place is now also written as a **geographical** row, so selecting a geography
returns the company.

The filer's own geographic disclosure wins, whole: if the company publishes
*any* geographic value for that metric and year, no business segment is mirrored
into geography for it. Matching member by member is not enough — "North America"
appears in only one of Amazon's two cuts, and a per-member guard let it through.

Verified on real rows (FY latest, Revenues):

| ticker | geographical index before | after |
|---|---|---|
| MCD | *(empty)* | United States 10,825 |
| CMG | *(empty)* | U. S. 59.3 |
| AMZN | US / Germany / UK / Japan / RoW | unchanged |
| CLX | United States / Foreign | unchanged |

The Segments tab is unaffected — only the screening index holds the member
twice.

---

## 7. Root cause C — the screening cache would not have rebuilt

`refresh_segment_cache_if_stale` rebuilds only when `MAX(id)` on
`coreiq_filing_metrics_v5` advances past `last_built_max_id`. A code change does
not move that mark. Segment data comes from 10-Ks, which land in a burst each
spring, so §5 would have sat dormant for months — and so would every earlier
classification fix.

### Fix

`_segment_classifier_version()` hashes the source of the five functions that
decide what a segment is and where it goes (`_classify_segment_rows`,
`_classify_member`, `_segment_axes`, `_member_display_map`,
`_segment_cache_entries`). It is stored in a new
`coreiq_screening_segment_refresh_state.classifier_version` column (added by
`_ensure_segment_refresh_state_table`, which already owns that table's DDL). A
mismatch is a reason to rebuild, and forces the *full* rebuild path because new
rules reach every ticker. On a build that had tickers time out the version is
left blank, so the next tick retries rather than recording a partial build as
current.

---

## 8. Testing

* `tests/test_segment_member_values.py` (new, 15 tests) — McDonald's FY2024
  rows as filed: the declared element wins, the place-named segment survives,
  the members add to the filed Total, geography on a places-only axis is still
  dropped, a renamed segment stays one member, a filer using only its own
  elements is untouched, the three screening-index rules, the classifier-version
  gate, and the three §4 guards (AmEx realigns, The Andersons does not reach
  into its Canada slice, a filer's own element is left alone).
* `tests/test_business_segments_have_no_places.py` (70 existing tests) — passes
  unchanged. `_classify_member`'s new `segment_axes` argument defaults to empty,
  which is exactly the old behaviour.
* Full suite: 556 passed. The 6 failures
  (`test_screening_keydevs_columns`, `test_transcript_search_single_company`)
  fail identically before the change — pre-existing and unrelated.
* UI, `http://localhost:8502/market_data?ticker=MCD&tab=segment_data` — all five
  metric groups show three members summing to the filed Total.

---

## 9. Rollout

No schema change, no data change, no migration.

No manual step. `coreiq_screening_segment_values_cache` and
`coreiq_screening_segment_member_cache` (last built 2026-10-03) rebuild
themselves on the first auto-refresh tick after the deploy, because §6 makes the
changed classifier a staleness signal. The first tick runs 5 minutes after boot
(`SEGMENT_CACHE_REFRESH_FIRST_DELAY_SEC`) and takes the full-rebuild path.
Nothing was written to the database from this session.

Per-ticker segment classification costs ~10–20% more (one extra pass over the
rows to compute `_segment_axes`): 50→53 ms for MCD, 303→384 ms for Citigroup's
26.6k rows. Both are small next to the DB fetch they follow.

---

## 10. Known gaps, not fixed here

* **Arko's "Other Revenue"** (2 cells) drops from 63.5 to −0.167 under §4: both
  elements are declared, both are filed under the same axes, and nothing
  separates the real product line from the unallocated one. Known and accepted
  against the 199 cells the rule repairs.
* **Region roll-up in screening.** Selecting "Americas" matches members labelled
  Americas, not "United States" — `geo_label_groups` collapses spellings of one
  place, it does not walk `geo_hierarchy`. Selecting both returns the union.
* **"U. S." (Chipotle).** `geo_label_map.json` has no alias for the spaced
  spelling, so it shows as filed. That file is the business team's.

---

## 11. Bank and card-issuer revenue (§9 fix, measured)

`us-gaap:RevenuesNetOfInterestExpense` added to the Revenues `edgar_concepts`.
Over 160 replayed tickers **3 changed, all corrections**:

| ticker | | before | after | filed |
|---|---|---|---|---|
| AXP | total revenue FY2023 | 37,218 | 60,515 | $60.5bn |
| AMJB | Asset & Wealth Mgmt 2021 | 13,071 | 16,957 | $16.96bn |
| BFH | total FY2023 | *(blank)* | 4,289 | $4.29bn |

`us-gaap:InterestAndDividendIncomeOperating` was deliberately **not** added. It
is interest income, which Cal-Maine (eggs) and Celsius also file on their cash
balances, and is nobody's revenue.

## 12. Performance

The classifier spent most of its time in four pure label functions
(`normalize_geo_key`, `geo_match_key`, `canonicalize_geo_label`,
`is_geo_excluded_label`) plus `_normalize_member_text` / `_member_key` — called
once per fact over tens of thousands of rows, while a company has only dozens of
distinct labels. 1.49M `re.sub` calls for Citigroup alone. Six `lru_cache`
lines:

| ticker | before this work | after (3 extra passes AND the caches) |
|---|---|---|
| C (26,599 rows) | 303 ms | **163 ms** |
| AMZN | 65 ms | **57 ms** |
| WMT | 66 ms | **52 ms** |
| MCD | 50 ms | **46 ms** |

Repeat views were already ~0.2 ms (`get_segment_data` is `@st.cache_data`, and
`render_segment_data` pins the built HTML in session state). Multi-second first
views on a dev Mac are the TLS handshake to Azure (~1.9 s per new pool
connection), not the app.
