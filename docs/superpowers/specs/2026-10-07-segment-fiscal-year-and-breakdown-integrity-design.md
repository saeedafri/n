# Segments tab: one fiscal year, one filing, one breakdown per column

**Date:** 2026-10-07
**Scope:** Market Data → Segment tab (annual + quarterly), the screening segment
cache built from the same classifier, the edgartools fallback.
**Trigger:** data/business team: "In the Segments tab, our filings contain different
data" — Wendy's (WEN) Business Segments did not match its 10-K.

---

## 1. What was wrong on Wendy's

| App column | App "Wendy's U.S." | What it actually was |
|---|---|---|
| Dec-31-2021 | 1,431,382 | FY**2020** (period ended Jan-3-2021) |
| Dec-31-2022 | 1,567,496 | FY**2021** (ended Jan-2-2022) |
| Dec-31-2023 | 1,750,242 | FY**2022** (ended Jan-1-2023) |
| — | — | FY2023 = 1,815,845 **missing** |

The same table also listed Sales, Franchise fees, Franchise royalty revenue,
Advertising funds revenue, "Franchise royalty revenue and fees" and "Real Estate"
beside the three operating segments, and the 2023 column paired FY2022 members with
the FY2023 Total.

## 2. Root causes (all in `SegmentDataRepository`, `app/data/repository.py`)

| # | Cause | Example | Fix |
|---|---|---|---|
| 1 | A 52/53-week year ending Jan 1–7 was bucketed into the next calendar year, colliding with the following fiscal year | WEN, AAP, DPZ, JNJ, KHC, VFC, YETI… (29 tickers) | `_fiscal_year_of`: Jan 1–7 → prior year. Validated against SEC's own `dei:DocumentFiscalYearFocus` (WEN period ending 2023-01-01 = fiscal 2022) |
| 2 | `_get_row_year` fell back to `report_fiscal_year`, which names the *filing*, not the period | HSY files fiscal 2017 under 2016 (21 tickers) | Year comes from the period only; non-annual durations (quarters inside a 10-K) return `None` |
| 3 | Balance-sheet facts dated mid-year (quarter ends, acquisition dates) filled annual cells | 103 tickers | `_at_year_end`: an instant counts only within 7 days of that year's annual period end |
| 4 | Revenue-by-source lines (`srt:ProductOrServiceAxis`, sales channel) were listed as business segments | WEN Sales/Franchise fees; ~300 tickers | `_classify_member`: product/channel members drop when the filer has an operating-segment axis. A filer with none keeps them (they are all it reports) |
| 5 | Restatements lost to the original: rows were ordered by filing year first, so the oldest 10-K always won first-wins | CPB FY2021 Snacks 3,944 vs restated 3,855 | `_newest_filing_first`: group by period, newest filing first — applied inside the classifier, so the disk-cached row order no longer matters |
| 6 | Start/End dropdown built years from `report_fiscal_year` while the table used period years | WEN dropdown "Dec 2021" showing a Dec-2020 column; phantom Dec-2026 column | `get_available_dates` / `get_date_range` / table years all use `_fiscal_year_ends` — a column exists only for a year with a full-year period |
| 7 | A reorganised year showed the old AND restated segments together | CVS 2021-22: Pharmacy Services + Retail/LTC + Health Services + Pharmacy & Consumer Wellness | `_keep_newest_filing`: per (section, metric, period) only the newest filing's members stay |
| 8 | Continuing/discontinued operations listed as segments | CPB "Continuing Operations", "Discontinued Operations, Held-for-sale…" | `_OPERATIONS_STATUS` member rule. The axis itself stays — SHOO, JACK, RDNW, AEO file real segments on it |

Also brought over from STG so both repos run the same segment code:
`_drop_wrapped_slices` (now also covers unwrapped multi-axis facts: a fact is
credited to its first axis member, so "Humira × United States" read as Humira),
the relabel merge in `_member_display_map`, "U. S." spacing, and the
"Unearned/Breakage revenue" exclusion in `constants.py`. STG received testing's
J&J inner-geographic-heading fix (`_inner_heading_is_geographic`).

The edgartools fallback (`_fetch_from_edgartools`) had causes 1/2 (it used the
filing's fiscal year for every comparative) and 4; it now derives the year from the
fact's period via `_get_row_year` and only uses product facts when a filing has no
segment facts.

## 3. Data flow after the change

```
_fetch_all_db_rows (disk-materialised raw rows, any order)
  └─ _build_segment_tables_from_db
       years = _fiscal_year_ends(rows)                 # full-year periods only
       └─ _classify_segment_rows   (also used by the screening cache)
            _drop_wrapped_slices → _newest_filing_first
            _at_year_end filter (instants)
            _classify_member (product / operations-status / place rules)
            first-wins merge (+ declared-element override), stored_filing per cell
            Totals → align → off-measure → impossible totals
            _keep_newest_filing
_build_segment_tables_quarterly: same _drop_wrapped_slices → _newest_filing_first
                                 and _keep_newest_filing
```

No DB writes. No schema change.

## 4. Verification

### Independent EDGAR check (all tickers)
`edgar_all.py` pulled every dimensioned annual revenue fact from 3 of each ticker's
latest 10-Ks (no amendments) straight from SEC XBRL; `verify.py` gave each Revenues
cell the app shows a verdict. 387 tickers with segment data, 348 with EDGAR facts.

| Verdict | Before | After |
|---|---|---|
| Exact one-axis segment fact | 9,210 | 9,560 |
| Wrong fiscal year | 427 | 17 |
| Stale (newer filing restated it) | 341 | 20 |
| Multi-axis match (mostly checker strictness: parent-grouping axes, reconciling-item wrappers) | 3,668 | 1,214 |
| Unexplained | 2,507 | 544 |
| Product/channel line in Business | 8,017 | 2,419 (filers with no segment axis only) |

### Members vs Total (tagging-independent)
Columns whose members add up to the Total within 2%:

| | Before | After |
|---|---|---|
| Business — adds up | 1,128 | 2,194 |
| Business — double counted | 2,480 | 674 |
| Geo — double counted | 892 | 598 |

### Tests
`tests/test_segment_fiscal_years.py` (WEN years, HSY report-year, quarter-in-10-K,
product lines, mid-year balance, CPB restatement, CVS reorganisation, continuing
operations). STG's wrapped-slice / relabel tests ported into
`tests/test_segment_member_values.py`. All segment/geo/screening tests pass in both
repos; the 5 Key Devs failures pre-exist and are unrelated.

### UI
WEN, CPB, CVS on testing (`http://localhost:8501/market_data?ticker=WEN&tab=segment_data`)
and STG (port 8503). WEN matches its 10-K to the dollar; FY2022 members sum to the
2,095,505 Total.

## 5. Known remaining issues (not fixed — need a decision)

1. **Two geographic breakdowns in one table** (NTAP, LEVI): US/Foreign *and*
   Americas/EMEA/APAC on the same geographic axis. Each value is filed; the table
   double counts.
2. **Companies whose operating segments are regions** (AAPL, COST): the 2026-10-03
   "a place is not a line of business" rule drops them, so Business shows product
   lines instead of Apple's Americas/Europe/Greater China/Japan/RoAP.
3. **Parent + child product lines** for single-segment filers (FTNT: Service =
   Security subscription + Technical support).
4. **US-only geographic fallback**: when a company files no geographic segments the
   tab synthesises "United States = income-statement revenue" (KR, M). It is an
   inference, not a filed number.
5. **Duplicate spellings** of one member with identical values (RL "Other
   non-reportable" / "Other Non-Reportable Segment-Related").
6. **Coverage gaps**: MRK segment revenue sits under a "Total segment profits"
   wrapper and is not shown.
7. Screening `_fetch_segment_revenue_v4` (specific-segments filter) SUMs every fact
   for a member label across a whole filing — all periods and concepts. Separate
   path from the Segments tab.

## 6. Rollout

- Restart the app (clears `st.cache_data` on `get_segment_data`).
- The screening segment cache rebuilds by itself once: its key hashes
  `_classify_segment_rows`, `_classify_member`, `_drop_wrapped_slices`,
  `_get_row_year` (added), `_segment_axes`, `_member_display_map`.
- `edgar_cache/geo_segment_members/` (Additional Data store-count header) has a
  7-day TTL; delete it to refresh immediately.
- Raw-row disk caches (`segment_rows_*`) need nothing: order is now imposed at use.
