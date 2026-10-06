# Segment year-range criterion reported "0 of 4736 companies have data"

## Symptom

Screening → Financial Information → Geographical Segments / Revenues, Year range
FY 2022–2026. The criterion card showed `0 of 4736 companies have data`, and no
year columns were filled. A single year (e.g. FY 2024) of the same criterion worked.

The segment cache was not the problem: `coreiq` segment values cache holds
geographical Revenues for 248–259 tickers per year 2022–2025 (85 for 2026).

## Root causes (two, stacked)

### 1. Duplicate tickers crash the per-year merge

`_apply_segment_year_range_criterion` (app/data/screening_service.py) runs the
ordinary single-year path once per year, then copies each year's column back with

```python
out[col] = out["ticker"].map(frame.set_index("ticker")[col])
```

The screening universe contains shared-ticker siblings (TSCO = Tractor Supply and
Tesco), so `frame`'s ticker index is not unique and pandas raises
`InvalidIndexError: Reindexing only valid with uniquely valued Index objects`.
That exception is outside the per-year `try`, so it escaped to
`recompute_working_set`'s safety net, which degraded the criterion to a no-op
(`with_data=0`). The existing unit tests used unique tickers only, so they passed.

**Fix:** map from `frame.drop_duplicates(subset=["ticker"])` — the same
one-value-per-ticker rule the pipeline merge already uses.

### 2. `year_cols` stamped on a copy of the criterion

`apply_geographical_segments_statement_criterion` / the business twin pass the
service `{**criterion, ...}` — a copy. The year-range function stamps
`criterion["year_cols"]` on that copy, so the pipeline's own criterion never gets
it. The pipeline counts `with_data` from `criterion["year_cols"]` (the
`display_col` is a label, not a column), and the UI uses it to pick the grid and
Excel columns. Result after fix #1 alone: columns filled (250–261 per year) but
the card still said 0.

The pipeline already re-stamped `quarter_cols` / `year_cols` from the returned
stats — but only on a criterion-cache hit.

**Fix:** in `recompute_working_set`, re-stamp `quarter_cols` / `year_cols` from the
stats after every run (cache hit or fresh), so any wrapper that copies the
criterion is covered in one place.

## Data flow

```
UI form (_submit_financial_criterion, year_range=[2022..2026])
  → recompute_working_set → _run_criterion
    → apply_geographical_segments_statement_criterion   (copies criterion)
      → apply_segment_statement_criterion → _apply_segment_year_range_criterion
          per year: apply_segment_statement_criterion(single year)  → frame
          map frame → out by ticker (now de-duplicated)              [fix 1]
          return stats{year_cols, with_data}
  ← pipeline stamps criterion.year_cols from stats                   [fix 2]
  ← with_data = any year non-null over year_cols → card count
```

No DB schema change, no new queries.

## Testing

- `tests/test_segment_member_values.py`
  - `test_a_segment_year_range_survives_a_ticker_shared_by_two_companies` —
    failed with `InvalidIndexError` before fix 1.
  - `test_the_card_counts_a_geo_segment_year_range_through_the_pipeline` —
    drives `recompute_working_set` through the copying wrapper; asserts
    `year_cols` reaches the caller's criterion and `with_data` is right.
- Real STG probe (558 universe): `with_data` 0 → 263; per-year columns
  FY2022 250, FY2023 260, FY2024 261, FY2025 251, FY2026 85.
- Real UI (`http://localhost:8501/screening`, Key Devs mode, universe 4736):
  card reads `263 of 4736 companies have data`; grid shows five
  `Revenues — FY 2022–2026 [FY yyyy]` columns with values.

## Rollout

Code-only change in `app/data/screening_service.py`. No cache rebuild, no
settings change. Existing saved criteria work as-is.

## Related finding (not fixed here)

`tests/test_screening_keydevs_columns.py` calls
`get_all_companies_universe.__wrapped__()` with mocked DB rows; that goes through
`materialized_or_build` and writes the 3-row fixture (SBUX/BMY/GHOST) into the
real local `server-logs/agg-cache/screening_all_companies_universe.pkl.gz`. The
local Key Devs universe then shows 3 companies until the file is deleted.
Local-only (tests do not run on STG).
