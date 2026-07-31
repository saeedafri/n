# Store Counts missing years — MDP "Additional Data" tab

Date: 2026-07-31 · Page: `/market_data` (tab `ratings`) · Code: `app/data/repository.py` (`RatingsDataRepository`)

## Symptom

Store Count rows show values for only part of the year range for several
companies. Reported: Walmart (WMT), Target (TGT), Floor & Decor (FND).

## Evidence (STG DB, `coreiq_filing_metrics_v5`, `source='store_count'`)

| Ticker | Rows in DB | Years | Rendered before fix |
|---|---|---|---|
| WMT | 7 | FY2020–FY2026 | 3 (FY2024–26) |
| TGT | 7 | FY2020–FY2026 | 7 — correct |
| FND | 6 | FY2020–FY2025 | split 2 + 4 across two rows |

Fleet-wide: 659 `store_count` rows, 123 tickers.

## Root causes — three independent, none of them "the DB is empty"

### 1. Source-sentence accuracy gate drops correct values (WMT)

`RatingsDataRepository._store_row_passes_source_check` requires the stored value
to appear in — or be a subset-sum of — the numbers in its own
`llm_query.source_sentence`. It exists to kill LLM fabrications, and it does.
But the extraction pipeline frequently stores a **caption or narrative sentence**
while the value came from the **table underneath it**, which the gate cannot see:

- WMT FY2021 sentence: *"…approximately 11,400 stores…"*, stored value 11,443 (correct).
- WMT FY2020 sentence: a U.S.-only unit-count table intro; stored value 11,471 (correct worldwide).
- Same class: AZO FY2022 6,943, SHAK FY2019–21, PLCE all years, RH, SHW, UPBD, WRBY.

Fleet impact: **63 of 659 rows (9.6%) gated out**; 20 tickers lose part of their
series, 6 lose all of it (of those, PLCE and URBN are retail — the rest are
already excluded as non-retail).

### 2. Store-type label drift splits one series into two rows (FND)

Rows are grouped by `dimension` (the 10-K's own wording). Issuers rename the same
fleet between filings — FND "stores" (FY2020–21) → "warehouses" (FY2022–25), WMT
"stores" → "locations", PSMT "warehouses" → "clubs" — producing two rows that each
show a few years and `-` elsewhere. 20 tickers affected.

### 3. Extraction coverage starts at FY2019/FY2020

Rows per fiscal year: 2016:1, 2017:1, 2018:4, 2019:42, 2020:68, 2021:89, 2022:100,
2023:105, 2024:100, 2025:96, 2026:53. `coreiq_filing_metrics_v5` holds filings back
to 2015–2017 for these tickers, so pre-2019 store counts were never extracted.
Data-team pipeline scope, not an app bug. No app-side fix.

XBRL is not a rescue path here: WMT tags only rounded, stale totals
(2018:11,700 / 2019:11,300 / 2020:11,500 — correctly rejected as not current);
TGT and FND tag nothing.

## Fix 1 — merge label drift into one series (SHIPPED)

`get_ratings_data`, after `sc_map` is built. Merge the store-type rows into a
single "Total" row only when it is provably a rename, not a breakdown:

1. the types' fiscal years are **pairwise disjoint** (a real breakdown reports its
   parts in the same year), and
2. the values on either side of every label switch are within **25%** (guards
   against a genuinely different, smaller quantity that merely doesn't overlap).

Merged: FND, PSMT, VRA, ARKO, PLBY, OXM, ONEW, CALY, PAG, QVCPQ.
Correctly left split: LULU (694 stores FY2023 → 47 outlets FY2024), RH (106 → 38),
BBW (350 stores → 525 locations, a genuinely broader scope).

Verified in the real UI (`bash .claude/dev/run_local.sh`, `/market_data?ticker=FND&tab=ratings`):

```
Store Count (United States)
Total          -  -  -  133  160  191  221  251  270
YoY Change     -  -  -   -  +20.3% +19.4% +15.7% +13.6% +7.6%
```

TGT verified unchanged and complete. WMT still 3 years — that is Fix 2.

## Fix 2 — gate escape hatch (SHIPPED at 15%, business-approved 31-Jul-2026)

`RatingsDataRepository._corroborated_store_rows`: run the source-sentence gate,
then accept a rejected row when the ticker has ≥2 gate-passed years and the value
is within `_SC_CORROBORATION_TOLERANCE` (15%) of the nearest passed year.
A ticker whose every row failed gets no anchor and stays empty.

| Tolerance | Rows recovered | Still hidden |
|---|---|---|
| 15% | 19 | 30 |
| 25% | 22 | 27 |

Shipped at 15%: WMT fully recovered (FY2020–23, all four values correct), along with
AZO, SHW, UPBD, LEVI, VRA, WRBY, AAP, PLBY. Known cost: it also re-admits
TJX FY2022 = 3,680 (the 10-K table says 3,380 — a documented transcription error)
and AAP FY2019 = 4,872 vs 4,877. This weakens the standing
"accuracy-first: a `-` is better than a wrong number" rule, so it is a business
decision, not an engineering one.

Not fixed by any tolerance: PLCE (7 years, values look correct but there is no
validated anchor to corroborate against), URBN, RH, SHAK FY2019–20. Those need the
extraction to store the *table* it read, not just the caption sentence.

## Fix 3 — XBRL wins per YEAR, not wholesale (SHIPPED)

Found while running the regression sweep, and the same class of bug as the
original report. When the XBRL branch took over it *replaced* the DB series
outright. Issuers tag `NumberOfStores` only in the years they choose to, so this
punched holes in a complete series: ORLY tagged FY2011–18 + FY2021 + FY2025 and
lost its validated DB values for FY2019/20/22/23/24 — a complete 7-year series
became a gappy 10-year one. SAH lost FY2018–19, RDNW FY2021.

Now XBRL's figure is kept wherever it has one and the validated DB value fills
the years it skipped. Safe because the branch is only entered once
`_xbrl_agrees_db` has proved the two sources describe the same quantity.
ORLY: 10 gappy years → 15 continuous (FY2011–FY2025).

This bug is present on the ORIGINAL code too — it only surfaces when the EDGAR
disk cache is warm, which is why it was invisible in day-to-day use and why the
first cold-vs-warm regression sweep reported false positives.

## Regression sweep — zero regressions

Two full passes over all 123 tickers holding `store_count` rows, FY2010–FY2030,
original code vs enhanced code, both with a WARM EDGAR disk cache (a cold pass
produces false diffs — the 4s bounded XBRL deadline flips `_sc_source` between
runs; SHAK/ORLY/RDNW/SAH were each confirmed as timing artifacts by running the
enhanced code three times and getting an identical payload every time).

| Outcome | Ticker-years |
|---|---|
| Unchanged | 621 |
| Recovered (was `-`, now shown) | 28 |
| **Lost** | **0** |
| **Changed** | **0** |

13 tickers improved: WMT +4, ORLY +5, SHW +4, UPBD +4, SAH +2, DXLG +2,
AAP/LEVI/PLBY/RDNW/TJX/VRA/WRBY +1 each.

Report: `store_count_before_after.xlsx` (sheets: Regression Verdict,
Ticker Summary, Per Ticker Per Year, Matrix Before-After).

### Why no regression is possible by construction

- `_corroborated_store_rows` runs the original gate first and returns its result
  untouched when there is no anchor; the fallback only ever `append`s rows the
  original gate had already discarded.
- The label merge unions years into one row — a year that had a value keeps it.
- The XBRL gap-fill only fills years that were previously blank.

## Testing

- DB probes against STG (`coreiq_filing_metrics_v5`) for row-level evidence.
- Offline `get_ratings_data` runs across the 20 multi-label tickers to confirm
  merge/no-merge decisions.
- Playwright UI check via `.claude/dev/ui_test.py` for FND, WMT, TGT.

Post-fix UI output:

```
WMT  Total   11,443  10,493  10,325  10,616  10,771  10,955   (FY2020 11,471 outside default window)
FND  Total   133  160  191  221  251  270
TGT  Stores  1,897  1,926  1,948  1,956  1,978  1,995        (unchanged)
```

Regression checks at the data layer: DKS FY2026=42 still rejected, PLCE still
empty (no anchor), LULU/RH/BBW still correctly split, URBN/LULU still served from
XBRL.

Final UI check on the shipped code (all three fixes in place):

```
WMT   Total   11,443 10,493 10,325 10,616 10,771 10,955
FND   Total   133 160 191 221 251 270
TGT   Stores  1,897 1,926 1,948 1,956 1,978 1,995     (unchanged)
ORLY  Total   5,616 5,784 5,971 6,157 6,378 6,585     (was gappy)
```
