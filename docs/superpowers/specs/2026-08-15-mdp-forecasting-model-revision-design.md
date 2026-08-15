# MDP Forecasting Model Revision — Annual + Quarterly

**Date:** 2026-08-15
**Scope:** Port the data-science team's revised MDP forecasting model (annual and
quarterly) into the app engines, re-enable quarterly forecasting, and surface the
new trust signals in the UI.
**Source material:** `~/Downloads/MDP-Forecasting-System` — `Code/` (two reference
scripts), `Input data/` (two spreadsheets), `Automated Testing Output Data/` (two
reference reports), `MDP Forecasting System.docx` (change summary).

---

## 1. What changed in the model

The data scientist's change table, mapped onto our code:

| Component | Before | After |
|---|---|---|
| Outlier detection | z-score only, threshold 2.0 | z-score (1.5 annual / 2.2 quarterly) **plus** an IQR test; either firing flags the period |
| Structural breaks | not handled | detect a large (>40%) *persistent* one-period decline; exclude pre-break history from the fit |
| Gap handling | every row assumed one period apart | growth rates annualised by the **actual** year gap |
| Backtesting | single fixed 2-year holdout | rolling-origin (walk-forward) validation across every viable origin |
| Scenarios | percentile growth compounded forever | **kept** (see §4), with a plausibility flag as the real trust signal |
| Plausibility check | none | flag forecasts implying >60%/yr growth or worse than −25%/yr decline |
| Data tiers | none | NO_DATA / FLAT / MINIMAL / LIMITED / FULL, each with its own method set |

Quarterly additionally gets: outlier screening **before** the structural-break test
(so one bad quarter cannot masquerade as a permanent business change), three-pass
seasonal indices (recomputed after break truncation and after outlier removal), a
`high_outlier_rate` flag, and a `long_horizon_extrapolation` flag for thin-history
companies compounding past 16 quarters.

---

## 2. Architecture

No new modules. The revision lands inside the two existing engines, whose public
API is unchanged so every caller keeps working:

```
app/utils/retailer_forecaster.py            annual engine   (+ shared helpers)
app/utils/retailer_quarterly_forecaster.py  quarterly engine
```

New shared helpers live in the annual module and are imported by the quarterly
module and both services — they are not copy-pasted:

- `drop_partial_periods(df, value_col)` — stub/invalid period removal (§3)
- `annualized_growth_rates(years, sales)` — gap-aware growth
- `compute_seasonal_indices(...)` — quarterly only, module-level so the three
  passes can each call it on a different slice

New engine state, on both classes: `tier`, `confidence`, `structural_break`,
`flag_reasons`, `plausible`, `ensemble_methods`, `mape`, `backtest_detail`, and a
`needs_review()` method.

Unchanged and still relied on by callers: `from_dataframe`, `summary_stats`,
`clean_data`, `outliers`, `backtest(...)`, `forecast(periods=...)`, `forecasts`,
`best_method`, `run`/`run_full_analysis`. `backtest()` still accepts
`holdout_years` / `holdout_quarters`; the parameter is now inert because
validation is rolling-origin.

### Data flow

```
actual rows (STG)
  → dedupe by period, keep max
  → drop_partial_periods              ← shared, identical in engine and service
  → structural break  ──┐ (quarterly: provisional outliers screened first)
  → outlier detection  ─┤ → data_excluding_outliers / data_excl_outliers
  → tier + confidence  ─┘
  → rolling-origin backtest → MAPE per method, ordered by exact MAPE
  → forecast: tier-allowed methods, ensemble = top 3
  → plausibility check → plausible / flag_reasons / confidence
  → summary payload → UI banner
```

---

## 3. Root-cause fix found while porting

Parity testing surfaced a defect that predates this work. `_prepare_data` dropped
any period below **10% of peak revenue**, intended to remove partial/transition
periods (YF phantom Dec-31 rows, the ELF March transition year). Because the
comparison was against the all-time peak, it also deleted the genuine early years
of any company that has since grown more than tenfold.

Measured blast radius on the reference set: **29 of 347 companies** lost history,
including NVDA, CoreWeave, AppLovin, Arista and Atlassian. Consequences: NVDA and
EVgo were mis-tiered FULL → LIMITED, and CoreWeave's 2031 forecast came out
**193× too high**.

The rule was a symptom patch. A stub period is small *relative to the periods
either side of it*, not relative to the all-time peak. `drop_partial_periods` now:

- never drops a **leading** row for being small — the start of a growth ramp is real data;
- drops an **interior** row only when it is under 10% of *both* neighbours;
- drops a **trailing** row when it collapses against the period before it;
- drops any row with **non-positive** revenue as invalid rather than modelling it.

The same 10%-of-peak block was copy-pasted in four places
(`revenue_forecast_service.py` ×2, `forecast_admin_service.py` ×2), which would
have re-truncated the history before the engine ever saw it. All four now call the
shared helper.

---

## 4. Decisions taken

**Scenarios kept.** The reference dropped the pessimistic/baseline/optimistic
columns because compounding a percentile growth rate exploded the ranges. In this
app those columns feed the overview chart, the scenario chart, the forecast table,
the Excel export and the email report. Removing them is a large UI change and a
loss of a view users have today, so they stay; the plausibility flag is now the
authoritative trust signal, and the scenarios are presented as a spread around
history rather than a validated forecast.

**Stale-store invalidation.** `needs_update()` compared only `last_actual_date`, so
after a model change the stores would keep serving old-model numbers until a
company happened to report again. `forecast_store._MODEL_REVISION_DATE` (an
existing mechanism, previously `_DEDUP_FIX_DATE`) is bumped to 2026-08-15, and the
same guard is **added** to `quarterly_forecast_store`, which had none. Any row
computed before that date is treated as stale and re-forecast lazily. Bump the
constant on every future model change.

**Quarterly re-enabled.** `constants.QUARTERLY_FORECASTING_ENABLED` flipped
`False → True`. See §7 for the operational consequence.

---

## 5. UI surface

`_render_review_notice()` in `app/pages/forecasting.py` renders under the company
hero when `summary['needs_review']` is set. Two states:

- **"Forecast failed its plausibility check"** (red) — `plausible is False`
- **"Treat this forecast with caution"** (amber) — thin tier, MAPE > 25%, or any data-quality flag

Each flag is rendered as a plain-English sentence via `_explain_flag()` rather than
the raw token. The services now pass `tier`, `confidence`, `plausible`,
`flag_reasons`, `needs_review` and `structural_break` through the `summary` dict,
for both annual and quarterly.

---

## 6. Testing

### Parity harness — `tests/test_mdp_forecast_parity.py`

The reference scripts were first re-run locally against their own inputs and
reproduced their shipped reports **exactly** (max numeric diff 0.0, zero text
mismatches) — establishing ground truth. The app engines are then replayed over
the same spreadsheets and required to match tier, selected method, backtest MAPE
and every forecast value.

| | Companies | Result |
|---|---|---|
| Annual | 347 | tier, best method, MAPE and every forecast value match exactly |
| Quarterly | 98 | 97 match exactly; Coty differs by design (below) |

Fixtures are committed under `tests/fixtures/mdp_forecasting/` so the suite is
self-contained; it skips cleanly if they are removed.

**Documented divergence — Coty Inc. (NYSE:COTY).** Coty reports revenue of
**−1098.0** for Q4 2020. The reference script models that negative figure; we drop
it as invalid. This shifts Coty's detected structural break by one quarter and
**lowers** its backtest error from 6.8% to 4.4%. Asserted explicitly in
`test_negative_revenue_period_is_dropped_not_modelled`.

### Bugs found by the harness and fixed

1. **10%-of-peak truncation** (§3) — 29 companies, up to 193× forecast error.
2. **Rounding before sorting.** MAPE was rounded to 2dp before ranking, turning
   near-misses into ties and letting the wrong method win. Now ranked on full
   precision, rounded only for display.
3. **Wrong seasonal factor in the quarterly backtest.** Outlier removal leaves
   gaps, so the next *observation* is not always the next calendar quarter; the
   trend methods were re-seasonalising a prediction at the quarter after their
   training window instead of the quarter actually being predicted. A Q1
   prediction was being scaled by Q3's factor.
4. **Ensemble fallback with no backtest.** Thin-history companies fell back to a
   hardcoded trio that the tier had not fitted, silently averaging one method.

### Live UI verification (local, STG data, Playwright)

| Ticker | View | Result |
|---|---|---|
| FLWS | Annual | 20 yrs, 3 outliers, weighted_avg, MAPE 12.4% — no banner (correct) |
| NVDA | Annual | 20 yrs retained (was truncated before the §3 fix) — no banner |
| M, WOOF, APP | Annual | clean — no banner |
| CVNA | Annual | caution banner, MAPE 30.8% |
| COTY, BBWI | Annual | caution banner, structural break 2020 |
| **EVGO** | Annual | **plausibility-failure banner** — MAPE 44.5%, implied 85%/yr |
| M | Quarterly | 83 quarters, 3 outliers, exp_smoothing MAPE 3.6%, seasonality visible |
| COTY | Quarterly | **59 quarters trained from 60 loaded** — negative-quarter drop confirmed on live data |

`/forecasting_admin` and `/market_data` both load without regression. Full suite:
13 passed; the 2 failures in `test_market_data_company_resolution.py` pre-exist on
`main` and are unrelated (exchange-acronym composites).

---

## 7. Rollout notes

- **Staging writes did occur during verification.** Bumping `_MODEL_REVISION_DATE`
  makes every stored ticker stale, and `revenue_forecast_service` fires a
  fire-and-forget upsert whenever it computes fresh — so browsing `/forecasting`
  locally rewrote staging rows for the 11 ticker/period combinations opened during
  UI testing (9 annual, 2 quarterly). The values written are the new model, i.e.
  the intended end state. STG's own twice-daily schedule (`12:34 UTC`) was not
  touched and ran normally with `quarterly_updated: 0`.
- **Local runs are now read-only against the store.** `FORECAST_STORE_READONLY=1`
  is exported by `run_local.sh` and short-circuits both `upsert_forecasts`
  functions, so a dev laptop pointed at staging can no longer rewrite it.
  Verified: TGT/COST/LULU browsed locally, stored `computed_at` unchanged.
- The store otherwise re-forecasts lazily on first page view. Trigger a full
  re-sync from `/forecasting_admin` to refresh eagerly instead of letting it
  arrive ticker-by-ticker via user traffic.
- **Quarterly re-enable restores the twice-daily background quarterly refresh.**
  The schedule itself is untouched — only the quarterly leg that the flag had been
  skipping resumes. Quarterly was paused at the Data team's request on 2026-07-13,
  so they should be told it is back on.
- Bump `_MODEL_REVISION_DATE` in **both** stores on any future model change.
- The reference "Cell 2" Excel report has no in-app equivalent by design: its
  per-company validation now lives as `needs_review()` on the engines, the UI
  banner, and the pytest parity suite.

---

## 8. Performance

The revised engine does strictly more work per run (rolling-origin fits every
viable window instead of one holdout), so it was measured rather than assumed:

| Series | Engine compute |
|---|---|
| Annual, 10 / 20 / 30 years | 3.4 / 4.5 / 6.2 ms |
| Quarterly, 40 / 83 / 120 quarters | 11.5 / 24.8 / 37.4 ms |

Against a ~250 ms Azure round trip and a ~233 ms user RTT, engine time is noise.
No optimisation was warranted and none was added.

The real cost sat elsewhere, and was fixed:

**`FORECASTING_schema_init` ran ~500 ms on every session.** It was guarded by
`st.session_state`, but Streamlit re-executes a page file on each rerun, so a
module global in a *page* cannot hold state either (the same caveat applies to
`screening._DB_TABLES_READY`). The guards now live in the imported data modules,
where they persist for the process:

- `forecast_refresh_service._COLUMNS_READY` (new)
- `quarterly_forecast_store._TABLE_READY` (new)
- `forecast_refresh_service._BACKFILL_DONE` (already existed — the correct pattern)

Measured: **~500 ms → 0.12 ms** per session after the first.

The remaining first-call cost (~5.6 s of cold DDL) is now absorbed by the boot
warmup (`cache_manager._warm_forecast_schema`), so the first user to open
`/forecasting` after a deploy is a hit, not the one who pays for it. Measured
after boot: `schema_init | 0.05 ms`.

---

## 9. Stale documentation corrected

The UI still described the *old* backtest to users: "the engine holds out the most
recent 2 years…". Eleven copy blocks in `forecasting.py` were rewritten to
describe rolling-origin validation — the Test tab, the methodology section, the
glossary (`backtest`, `holdout` → `rolling origin`, `MAPE`), the MAPE
interpretation thresholds, the bias/RMSE notes, and **the Backtest sheet of the
downloadable Excel export**, which was shipping the wrong explanation to anyone
who opened it.

`seasonal_naive` and `flat_carry` also gained proper labels in both
`MODEL_FULL_LABELS` and the email report's `_MODEL_LABEL`, since quarterly is now
live and `seasonal_naive` is a first-class model rather than an unreachable key.

---

## 10. Coverage of degenerate input

`tests/test_forecaster_tiers_and_edges.py` (30 cases) pins the paths real data
reaches rarely: 0/1/2/3-period histories, all-zero series, negative
restatements, perfectly flat revenue, explosive growth, and the partial-period
rule (a 100x growth ramp keeps its early years; interior and trailing stubs are
still dropped). Both engines return a usable ensemble — with scenario columns
intact for the dashboard — at every tier, and neither raises on any input tested.

---

## 11. Final verification matrix

All rendered from live STG data with no errors:

| Ticker | View | History | Best model | MAPE | Banner |
|---|---|---|---|---|---|
| FLWS | Annual | 20 yrs | Weighted Average Growth | 12.4% | — |
| M | Annual | 21 yrs | Weighted Average Growth | 3.2% | — |
| NVDA | Annual | 20 yrs | CAGR | 18.9% | — |
| CVNA | Annual | 12 yrs | Linear Regression | 30.8% | caution |
| EVGO | Annual | 8 yrs | CAGR | 44.5% | **plausibility failure** |
| M | Quarterly | 83 qtrs | Exponential Smoothing | 3.6% | — |
| COTY | Quarterly | 59 qtrs | CAGR | 8.5% | — |
| BBWI | Quarterly | 83 qtrs | CAGR | 6.3% | — |

Excel export builds for annual, quarterly and flagged tickers. `/forecasting_admin`
and the `/market_data` Forecasting tab both render. Suite: 43 passed; the 2
failures in `test_market_data_company_resolution.py` pre-exist on `main`.

---

## 12. Screening segment cache — incremental refresh

**Ask:** the cache was being refreshed manually; it should be materialised, check
daily, and update **only newly-added segments** rather than everything.

Two of the three were already true: the caches are persistent DB tables
(`coreiq_screening_segment_values_cache` / `_member_cache`) and a daemon
(`segment_cache_auto_refresh`) already gates rebuilds on a `MAX(id)` high-water
mark over `coreiq_filing_metrics_v5`. Verified healthy on STG — last built
2026-08-15 00:59, 57,318 rows across 334 tickers, high-water mark current.

The gap was the third: when the gate fired it ran a **full** rebuild — reclassify
all ~396 tickers, then swap the whole 57K-row table — even if one 10-K had landed.

### What changed

- `_segment_entries_for_tickers(...)` — the scan+classify pass, lifted out of
  `build_segment_values_cache` so the full and incremental paths share one
  classifier and can never drift.
- `_tickers_changed_since(last_id, cur_id)` — primary-key range scan over the
  delta only, returning just the tickers with new dimensioned 10-K rows.
- `build_segment_values_cache_for_tickers(tickers)` — per-ticker delete+insert,
  leaving every other ticker's rows untouched, then one aggregate recompute of
  the member cache published by `RENAME` so the dropdown is never seen empty.
- `refresh_segment_cache_if_stale` now chooses: incremental by default, full
  rebuild when there is no baseline (first run), when `force=True`, or when the
  changed set exceeds `_SEGMENT_INCREMENTAL_MAX_TICKERS` (60) or
  `_SEGMENT_INCREMENTAL_MAX_SHARE` (25% of the universe) — past which a
  whole-table staging swap is both faster and atomic.
- Poll interval default 6h → **24h**, matching "check daily" (segment data comes
  from annual 10-Ks).

### Robustness

The shared scan aborts on a chunk timeout so a full rebuild never publishes a
partial table. That is wrong for the incremental path — one slow ticker would
block the high-water mark forever — so `raise_on_timeout=False` there:

- tickers that timed out keep their existing rows (never deleted-then-blank),
- they are returned as `skipped` and reported,
- the high-water mark is **not** advanced when anything was skipped, so the next
  daily tick re-derives the same delta and retries them rather than skipping past
  their filings permanently,
- incremental uses `ticker_chunk=4` (vs 12) so queries are far less likely to hit
  the 20s server-side ceiling on data-heavy filers.

### Verified on live STG data

| Check | Result |
|---|---|
| Classifier output vs live cache (SIG, TPR) | **identical** — 0 missing, 0 extra |
| Incremental write of 2 tickers | 8.1s, 715 rows; table totals unchanged (57,318 / 334 / 3,262) |
| Forced tick with a rewound high-water mark | detected 6 changed tickers (AVT, COHR, PFGC, SIG, TPR, XOM), rebuilt only those in **26.9s**, totals unchanged |
| High-water mark after run | advanced correctly to current `MAX(id)` |
| Second tick | `action: fresh` — clean no-op |

A full rebuild touches 396 tickers; this touched 6.

---

## 13. Screening per-rerun DDL

`screening._ensure_db_tables` is guarded by a module global in a **page** file,
which Streamlit resets on every rerun — so the guard never actually held. Three of
its calls (`saved_criteria`, `watchlist`, `portal_users`) self-guard correctly in
their own modules, but `ensure_segment_member_cache_table` and
`ensure_segment_values_cache_table` did not, so every screening rerun paid two
DDL round trips (~250ms each on Azure).

Both now carry process-level guards in `screening_service`. The page-level flag is
kept only as a same-run short-circuit, with a comment recording why it cannot be
relied on across reruns — so the pattern is not reintroduced here or elsewhere.

---

## 14. Reverted: annual outlier/break ordering

The annual engine tests for a structural break *before* screening outliers, the
opposite of quarterly. This was initially treated as a defect and changed to match
quarterly. Measured on the 347-company reference set, it was **worse**:

| Company | Break-first (kept) | Outlier-first (reverted) |
|---|---|---|
| Dollar Tree | 1.8% MAPE | 17.5% |
| Bath & Body Works | 4.1% | 17.1% |
| Coty | 6.4% | 15.2% |
| Western Digital | valid | forecast goes **negative** |

A structural break *is* a large decline, so on a ~20-point annual series the
outlier test flags the break year itself and screening first destroys the evidence
of what is being detected. Quarterly can afford the other order because 80+ points
separate a one-off quarter from a permanent shift. The ordering is now documented
in code as deliberate, and raised with the data scientist for confirmation
(question 3 in `2026-08-15-questions-for-data-scientist.md`).

---

## 15. Full re-sync of the forecast store

Run 2026-08-15, `force=True`, both cadences, after all code changes were final:

| | Tickers | Result | Rows |
|---|---|---|---|
| Annual | 396 | **396 updated, 0 problems** | 39,320 |
| Quarterly | 396 | 353 updated, 19 skipped, 24 no-data, 0 errors | 139,577 |

The 24 quarterly no-data companies have no quarterly revenue rows at all (see §17).

### Orphaned-row defect found and fixed during verification

After the re-sync, some tickers still held rows stamped with old `computed_at`
values. Cause: a forecast whose horizon previously reached further out (e.g. CRM
had 2030–31 rows from a run made when the last actual was older) leaves those tail
rows behind, because the upsert only overwrites the years it writes. They are
invisible at the current 5-year horizon but are stale old-model values that would
resurface if the horizon ever widened.

Both `upsert_forecasts` functions now delete any row for the ticker/metric whose
`computed_at` predates the current computation — every row the forecast owns is
stamped in the same write, so an older stamp is by definition orphaned. A one-off
cleanup removed the existing ones: **200 annual + 307 quarterly rows**.

Verified afterwards: **0 tickers in the sync universe hold any pre-resync row**.
The only remaining old rows belong to 11 annual / 2 quarterly dead composite
tickers (`APP.NasdaqGS`, `SHOP.NYSE`, `NOW.NYSE`, `ORCL`, `ZBRA`…) which are not
in the company universe and unreachable from the UI. These match the known
`exchange_acronym` corruption and were deliberately left in place rather than
deleted, so the upstream data issue is not masked.

---

## 16. Background refresh — quarterly on the Annual schedule

Confirmed by code and by execution. Both cadences run inside the **same**
06:00 / 18:00 Asia-Kolkata window under one `run_key`; quarterly was never a
separate schedule, so no schedule change was required — only the feature flag.

A full cycle was executed end to end (`_run_cycle(force=False)`, 701s):

```
annual:    {updated: 0, up_to_date: 396, skipped: 0,  no_data: 0,  errors: 0}
quarterly: {updated: 0, up_to_date: 353, skipped: 19, no_data: 24, errors: 0}
errors: []
```

`updated: 0` is the correct result immediately after a full re-sync and proves the
staleness gate works — the scheduler does no redundant writes.

---

## 17. All-ticker UI sweep

`.claude/dev/forecast_ui_sweep.py` drives `/forecasting` for **every** company in
one reused browser, asserting a rendered forecast and capturing any traceback.

| Period | Tickers | Result |
|---|---|---|
| Annual | 396 | **396 ok · 0 problems** · 33 showing a review banner |
| Quarterly | 396 | **353 ok · 43 no-data · 0 problems** · 18 showing a review banner |

### Defect found by the sweep and fixed

The first quarterly sweep reported 43 failures, every one a non-US listing
(`ADS.DE`, `MC.PA`, `9983.T`, `ABF.L`, `CA.PA`, `HM-B.ST`…). These companies report
semi-annually and have no quarterly revenue series, so the page rendered the full
dashboard with an em-dash in every card — indistinguishable from a broken page.

This was newly reachable: quarterly had been disabled, so the surface had never
been exercised. `/forecasting` now detects an empty quarterly payload and shows an
explicit notice naming the reason and pointing at the Annual view, instead of an
empty dashboard. Re-swept: 0 problems.

---

## 18. Root cause: the two "pre-existing" test failures

`test_market_data_company_resolution.py` had two failures that predated this work.
Root cause: **the tests were stale, the production code was correct.**

The fixture declared VusionGroup with `exchange_acronym: "EPA"` and asserted a
`VU.EPA` composite ticker. But Yahoo's Euronext Paris suffix is **`.PA`**
(VusionGroup is `VU.PA`); `EPA:` is *Google Finance* notation. `YAHOO_SUFFIXES`
contains `PA` and not `EPA`, so `yahoo_symbol('VU', 'EPA')` correctly returns the
plain `VU` and mints no composite.

That whitelist was added deliberately (verified on STG 2026-07-30: every accepted
suffix resolves to real `coreiq_yf_financials_income_statement` rows, every
rejected one resolves to zero). Minting `VU.EPA` invents a symbol no vendor and no
table knows, which hides the company from every financial view — the same
corruption that leaves dead composite rows in the forecast store (§15).

Fix: the fixture now uses the real `PA` suffix, and a **new** test
(`test_exchange_name_acronym_never_mints_a_composite`) pins the rejection so the
old behaviour cannot be reintroduced. No assertion was weakened.

**Suite: 55 passed, 0 failed.**

---

## 19. Re-verification of the data scientist's deliverable

Repeated from scratch, comparing **every sheet** rather than only Summary:

| Report | Sheet | Rows | Max numeric diff | Text mismatches |
|---|---|---|---|---|
| Annual | Summary | 347 | 0.0 | 0 |
| Annual | Needs Review | 23 | 0.0 | 0 |
| Annual | Backtest Detail | 1,657 | 0.0 | 0 |
| Quarterly | Summary | 98 | 0.0 | 0 |
| Quarterly | Needs Review | 6 | 0.0 | 0 |
| Quarterly | Backtest Detail | 2,147 | 0.0 | 0 |
| Quarterly | Seasonal Indices | 75 | 0.0 | 0 |

All four source files are byte-identical (sha256) to the fixtures committed under
`tests/fixtures/mdp_forecasting/`.

The parity suite previously asserted only the Summary sheet. It now also verifies
the **engine internals** against the reference:

- `test_annual_backtest_detail_matches_reference` — every walk-forward origin
  (year predicted, prediction, actual, error) for 250+ companies.
- `test_quarterly_seasonal_indices_match_reference` — all four quarterly seasonal
  factors for 60+ companies.

Matching Summary shows the outputs agree; matching these shows the engines reach
them the same way rather than coinciding.

**Note on the reference scripts:** they were not modified. They are the ground
truth this port is measured against, so changing them would remove the
independent check. The one place our behaviour intentionally differs (Coty's
negative revenue) is raised as question 1 for the data scientist.

---

## 20. Segment cache — verified as screening's dependency

| Check | Result |
|---|---|
| High-water mark vs `v5 MAX(id)` | in sync (91,781,667), `building=0` |
| Values cache | 57,318 rows / 334 tickers |
| Member cache | 3,262 rows |
| Business dropdown | 500 options |
| Geographical dropdown | 333 options |
| Real screening query (60 tickers, US 2024 revenue) | 36 rows in 460ms, values correct (AAPL US $151.79B FY2025) |

`tests/test_segment_cache_incremental.py` (7 tests, no DB required) pins the rules
that keep screening correct: the delta query stays bounded on both sides of the id
window; only changed tickers are deleted; a timed-out ticker keeps its existing
rows and is reported rather than blanked; an empty ticker list is a no-op; and the
full rebuild still aborts on a timeout while the incremental path tolerates one.

### Screening cold-start fixed

The first `read_segment_member_options_cache("business")` in a fresh process took
**5,199ms** — investigated and found to be the Azure connection handshake, not the
query (warm, the same call is 306ms and geographical 261ms). Added to the boot
warmup alongside the forecast schema, so the first user to open /screening after a
deploy no longer pays it. Measured after: **306ms**.

---

## 21. Refresh dialog — root cause and fix

**Symptom:** the Refresh popup took far too long to open.

**Measured before any change** (service layer, warm DB connection):

| | Cold process | Warm |
|---|---|---|
| annual | **23,769 ms** | 4,239 ms |
| quarterly | 7,063 ms | 4,675 ms |

Breakdown showed one dominant term — `_annual_q4_report_dates_bulk`:

```
DIALOG_Q1_ticker_max_computed_at      552 ms
DIALOG_Q2_company_meta                802 ms
DIALOG_Q3_annual_q4_dates          21,860 ms   <-- 92% of cold time
DIALOG_TOTAL                       23,768 ms
```

### What was actually wrong

`get_refresh_table_data` carried **`@st.cache_data(ttl=120)`** — a two-minute TTL
on a four-second query. Both sync paths already call
`get_refresh_table_data.clear()` explicitly, so the short TTL bought no freshness
whatever; it just guaranteed that anyone opening the dialog more than two minutes
after the last one re-ran the whole thing. That is the normal case.

**Fix:** TTL 120 s → 3600 s (freshness still comes from the explicit `clear()`),
plus both cadences added to the boot warmup (`cache_manager._warm_refresh_dialog`)
— quarterly had never been pre-warmed at all, and the page's existing warm thread
only started at page load, so a click within the first ~24 s still waited.

**Result — measured end to end in the browser** (click → dialog populated):

| | Open time |
|---|---|
| Annual, first open | **2,412 ms** |
| Annual, repeat | 2,801 ms |
| Quarterly | 2,589 ms |

No DB work now occurs on open; the remaining ~2.5 s is Streamlit's page-rerun
round trip.

### Two optimisations tried and rejected

1. **Windowing the calendar scan to 24 months** (18,552 → 2,221 rows). Rejected:
   it changed **329 of 373** returned values, because the window also changes
   which fiscal-quarter months a ticker appears to have, which feeds the
   annual-month inference. Reverted and verified byte-identical to baseline.
2. **Reducing the 407 rendered rows.** Rejected: rendering only 50 rows measured
   *slower* (4,036 ms) than all 407 (2,412 ms), so row count is not the driver.

Both were measured rather than assumed, and neither shipped.

---

## 22. Refresh button coverage — every company accounted for

| Cadence | Total | "Refresh Now" | "On hold" |
|---|---|---|---|
| Annual | 407 | **370** | 37 (9.1%) |
| Quarterly | 355 | **346** | 9 (2.5%) |

A row shows "On hold" instead of a button when no annual reporting date could be
identified (`_has_report_date` → "—"). Every one of the 37 annual cases, by cause:

**A. Composite ticker with no row in the YF earnings calendar — 17**
`2020.HK, 2331.HK, 3998.HK, APP.NasdaqGS, CMRC.NASDAQ, CMRC.NasdaqGS, HPQ.NYSE,
JD.L, MANH.NASDAQ, NOW.NYSE, NXT.L, ORCL.NYSE, S4M.HM, SHOP.NYSE, TSCO.L,
TWLO.NYSE, ZBRA.NASDAQ`

Nine of these (`.NasdaqGS`, `.NYSE`, `.NASDAQ` suffixes) are the
`exchange_acronym` corruption described in §18 — an exchange *name* appended
where a Yahoo suffix belongs, producing a symbol no vendor knows. They are dead
rows and can never resolve. The rest (`.HK`, `.L`, `.HM`) are genuine foreign
listings that simply have no YF calendar coverage.

**B. Calendar rows exist, but no quarter ends in the fiscal-year-end month — 18**

| FYE | Quarters present | Tickers |
|---|---|---|
| December | Jun | GTM, IBTA, INGM, KVUE, KVYO, SPT, USFD, ZETA |
| January | Jul | BRZE, MDB, YEXT |
| April | Jul | AI, ESTC |
| March | Jun | DXC, KD |
| December | Jun, Mar, Sep | CRWV |
| October | Apr, Jan, Jul | HPE |
| August | Feb, May | CHSCP |

These are mostly recent listings whose first full-year quarter has not been
reported or ingested yet. They resolve on their own once it lands — correct
"pending" behaviour, not a bug.

**C. No earnings-calendar rows at all — 2**
`FL` (Foot Locker), `QRTEA`. FL is an active company, so this is a genuine
ingestion gap worth raising with the data team.

---

## 23. Page load benchmark

`.claude/dev/page_load_bench.py`. "Loaded" = a page-specific body marker is on
screen, not merely that the HTTP response arrived — Streamlit returns a shell
instantly and fills it over the websocket, so navigation timing alone measures
nothing. Nav-bar labels are explicitly avoided as markers for the same reason
(an early run "measured" /calendar at 22 ms by matching the nav link).

Measured on a dev Mac over VPN to the STG database, so cold numbers are
indicative rather than production-identical.

| Page | Cold (first hit in a fresh process) | Warm |
|---|---|---|
| forecasting_admin | 817 ms | 893 ms |
| market_data | 1,543 ms | 814 ms |
| forecasting (quarterly) | 1,877 ms | 1,186 ms |
| **screening** | **5,371 ms** | **839 ms** |
| forecasting (annual) | 5,516 ms | 1,475 ms |
| home | 7,505 ms | 424 ms |
| newsroom | 9,992 ms | 1,265 ms |
| **calendar** | **18,444 ms** | 1,684 ms |

Warm performance is healthy everywhere (0.4–1.7 s). The cold column is the
first-user-after-deploy cost. Screening and market_data — the two pages asked
about — are both fine warm, and screening's cold path improved separately (§20:
5,199 ms → 306 ms for the segment dropdown).

**Not addressed here:** `/calendar` at 18.4 s cold is the worst page in the app
and is outside this change's scope. `/newsroom` at 10.0 s is second. Both are
cold-start only. Flagged rather than fixed, since neither was part of this work.

---

## 24. Visual record

`.claude/dev/capture_screens.py` writes a full-page screenshot of every
forecasting-related screen to `/tmp/ui_screens/`:

`01_home` · `02_forecasting_annual` · `03_forecasting_flagged` (EVGO, red
plausibility banner) · `04_forecasting_caution` (CVNA, amber) ·
`05_forecasting_quarterly` · `06_forecasting_no_qtr` (ADS.DE notice) ·
`07_market_data_fcst_ann` · `07b_market_data_fcst_qtr` · `08_screening` ·
`09_forecasting_admin`

All ten captured successfully.

**market_data Forecasting tab, both cadences confirmed:**
- Annual: columns 2027–2031, Ensemble 22,233.11 — matches the /forecasting page's
  $22.23B exactly.
- Quarterly: columns `3 months FQ2/CQ3 Jul-31-2026` onward, following Macy's
  Jan-31 fiscal calendar, all seven models including Seasonal Naive. The
  Jan-31-2027 column reads 7,372 against ~4,800 neighbours — the holiday quarter,
  i.e. seasonality is being applied correctly.

Note: the first load of a company resets Period Type to Annual by design, so
reaching the quarterly view requires selecting it (or a second navigation with
`period_type=Quarterly`).

---

## 25. Security finding — /forecasting has no auth gate

While testing the dialog, the Refresh button would not render locally. Root cause:

```python
# app/pages/forecasting.py:481
# require_auth(page="forecasting")
```

`require_auth` is **commented out, in HEAD** — committed, not a local edit. With
it disabled the debug bypass never populates `auth_data`, so `get_current_user()`
returns None, `_get_refresh_permissions` returns `(False, False)`, and the button
is hidden. Re-enabling it locally made the button appear immediately, which is
how the dialog measurements above were taken.

The line was restored to its committed state afterwards — **this is not changed in
the diff.** It is raised as a decision rather than actioned: with it commented,
`/forecasting` performs no authentication check at all, which matters before a
deploy.

---

## 26. Root cause of the "slow page" numbers

The cold figures in §23 were re-investigated after they were flagged as
concerning. They were real measurements but a misleading summary: they folded
three unrelated one-time costs into a single "page load" number, and every one of
them is inflated by measuring from a laptop.

### The measurement environment inflates everything ~100x

A trivial `SELECT 1` from this Mac, over VPN to Azure MySQL in centralus:

```
median 305 ms   (min 249, max 411)
```

The STG app runs **inside centralus with the database**. In-region MySQL RTT is
1-5 ms. So every round trip costs this bench ~100x what it costs a real user, and
any cold figure measured here is an upper bound that production will not see.

### Cost 1 — connection setup, ~5.0 s, once per process

Directly measured in a fresh process:

```
FIRST trivial query : 5,251 ms   <-- TLS handshake + auth
SECOND              :   235 ms
=> one-off cost     : 5,016 ms
```

This is why `get_companies_rows` "took 5.3 s" on /home: the query itself is
**520 ms** warm — the rest was the handshake, attributed to whichever caller
happened to run first. The same 5 s appeared as `ensure_forecast_columns` on
/forecasting and as the segment dropdown on /screening (§20). It is one cost,
paid once, by whatever page is first.

### Cost 2 — cold cache builds, once per process per dataset

| Dataset | Cold | Steady |
|---|---|---|
| calendar | ~18.5 s | **900 ms** |
| newsroom | 9.7 s | 1.4 s |
| screening segment options | 5.2 s | 306 ms (fixed, §20) |
| forecast refresh dialog | 23.8 s | cached (fixed, §21) |

Calendar measured six consecutive times on a warm process: 960, 903, 904, 904,
890, 905 ms. The 18.5 s reading was a cold build occurring while the boot warmup
was still working through its sequential track — over VPN that track takes far
longer than it will in-region.

### Cost 3 — steady state, which is what users actually experience

Fresh browser session against a warm app process:

| Page | New session | Reload |
|---|---|---|
| home | 412 ms | 532 ms |
| market_data | 943 ms | 1,106 ms |
| screening | 1,905 ms | 891 ms |
| forecasting | 2,276 ms | 1,442 ms |
| newsroom | 9,706 ms (first build) | 1,361 ms |
| calendar | 18,561 ms (first build) | ~900 ms |

**No page is slow in steady state.** Everything lands between 0.4 s and 2.3 s,
and that is on a link where each DB round trip costs 305 ms instead of ~3 ms.

### What this means

- There is **no page-level performance bug**. The >2 s numbers were one-time
  process warm-up costs plus a ~100x latency tax from the test environment.
- The correct lever is **boot warmup coverage**, not query optimisation — pay the
  one-time costs in the background thread rather than in a user's first request.
  Warmup already covers calendar, news dropdowns and company rows; this change
  added the forecast schema, the refresh dialog (both cadences) and the screening
  segment dropdowns.
- Verifying this properly needs a measurement **on STG**, where the app and DB
  share a region. Local numbers cannot answer "is production fast".

### Methodology note

An earlier version of the benchmark reported `/calendar` at 22 ms and
`/earnings_calls` at 25 ms. Those were false: the markers ("Calendar",
"Earnings") are nav-bar labels present on every page, so the wait matched
instantly. Markers must be page-body content. The corrected benchmark is what
§23 reports.

---

## 27. Every forecast surface, tested in both cadences

The earlier rounds tested `/forecasting` exhaustively and the market_data
Forecasting tab on a single ticker. That was not full coverage. All surfaces that
read the forecast stores were enumerated from the code and tested.

| Surface | Annual | Quarterly | Result |
|---|---|---|---|
| `/forecasting` | 396 tickers | 396 tickers | 0 problems (§17) |
| market_data → **Forecasting tab** | 40 tickers | 40 tickers | 0 problems |
| market_data → **Key Stats (F- columns)** | 7 tickers | 7 tickers | 0 problems |
| `/screening` → Forecasting criterion | 6 tickers | n/a (annual-only) | values correct |
| `/forecasting` → Refresh dialog | yes | yes | 2.4–2.6 s (§21) |
| `/forecasting_admin` | yes | **added this round** | toggle verified |

### Key Stats was a real consumer and had never been tested

`forecast_store.get_forecasts()` is dead code — referenced only in docstrings.
Key Stats actually reads both stores through raw SQL in `KeyStatsRepository`
(`repository.py:4743` annual "next 5 years", `:4791` quarterly "next 8 quarters"),
rendering `F` columns beside `A` (actual) and `E` (analyst estimate).

Verified for M, and the numbers cross-check exactly across three independent
surfaces:

```
Annual   header  … Jan-31-2026A | 2027E | 2027E | 2028E | Jan-31-2027F …
Annual   revenue … 22,233.11  21,857.71  21,494.33  21,142.57  20,801.99
                   ^ identical to /forecasting ensemble and the Forecasting tab

Quarterly revenue … 4,954.90  4,768.80  7,372.20  4,835.00  4,850.60
                                        ^ Macy's holiday quarter — seasonality applied
```

Swept across M, FLWS, NVDA, COTY, BBWI, TGT, AAPL in both cadences: the
`F Forecasted (Revenue Model)` legend and populated revenue rows render in all 14
combinations.

### Screening forecast criterion is annual-only — by design, and unchanged

`apply_forecast_criterion` reads `coreiq_model_forecasts` and offers six model
keys (ensemble, three scenarios, linear, cagr). There is **no quarterly forecast
criterion**, and none was added — that would be a new screening feature, not part
of enabling quarterly. Verified the annual path returns the new model's numbers
(M = 22,233.11, matching every other surface).

### Gap found and fixed: forecasting_admin had no quarterly at all

`/forecasting_admin` imported only `sync_all_eligible` and
`sync_forecast_for_ticker` — both annual. With quarterly enabled, an administrator
had no way to sync quarterly forecasts from the admin page (only from the
`/forecasting` Refresh dialog, which does carry a cadence toggle).

Added a Period selector, gated on `QUARTERLY_FORECASTING_ENABLED`, routing to the
existing `sync_all_eligible_quarterly` / `sync_quarterly_forecast_for_ticker`
(both already exercised by the full re-sync in §15). Verified in the browser:

```
BEFORE: 'Sync all stale tickers (Annual)'    'Sync this ticker only (Annual)'
click Quarterly ->
AFTER : 'Sync all stale tickers (Quarterly)' 'Sync this ticker only (Quarterly)'
```

### Non-US companies: correct behaviour, not a defect

The market_data sweep initially reported 6 failures — all Quarterly, all non-US
(ADS.DE, ATD.TO, 2020.HK, ATZ.TO, 7936.T, ABF.L). These have **0 rows** in the
quarterly store because they report semi-annually, and
`get_available_period_types` correctly returns `['Annual']` for them, so the UI
never offers Quarterly. The sweep had forced `period_type=Quarterly` through the
URL — a state a user cannot reach — and the page correctly fell back to Annual.
Real problem count: **0**.

---

## 28. Segment tab crash (CRITICAL) — fixed

**Symptom:** "Something went wrong. Please try again." on market-data → Segment
(reported for TSCO; affected every ticker).

**From the STG log:**

```
type=AttributeError  msg='pyarrow.lib.ChunkedArray' object has no attribute 'as_py'
  repository.py:10884  _fetch_from_edgartools -> filing_list = list(filings[:6])
  edgar/entity/filings.py:198  __getitem__ -> get_filing_at()
```

**Root cause.** `EntityFilings.__getitem__` forwards straight to
`get_filing_at(item)`, which does `self.data['form'][item].as_py()`. An **int**
index yields a pyarrow `Scalar` (has `.as_py()`); a **slice** yields a
`ChunkedArray` (does not). `filings[:6]` passes a slice, so it raises.

This was already a known trap — the codebase carries the fix and an explanatory
comment in three other places (`repository.py:11915, 12056, 12382`):

```python
# NOTE: list(filings)[:6], not list(filings[:6]) — EntityFilings does not
# support slice indexing on edgartools >=5.x (pyarrow ChunkedArray error).
```

Line 10883 was simply missed. Fixed to `list(filings)[:6]`, matching the existing
convention. Reproduced the failure and verified the fix against live EDGAR
(TSCO, 29 10-K filings), then confirmed in the browser: no error banner and
"Business Segments" renders for TSCO, M, TGT and AAPL.

---

## 29. Refresh dialog on STG — second round

STG timings (in-region, so far below the local numbers in §21):

```
DIALOG_Q3_annual_q4_dates          1,900 ms
DIALOG_TOTAL_get_refresh_table_data 2,624 ms   (annual)
DIALOG_TOTAL ..._quarterly          4,522 ms
```

The §21 fix cached the dialog's *outer* function, but on a cache miss the
underlying date lookup still ran. Both bulk lookups are now cached directly
(`_annual_q4_report_dates_bulk`, `_quarterly_report_dates_bulk`, ttl 1h) — they
derive purely from the earnings calendar, which is ingested at most daily.

Verified the cached output is **identical** to the uncached (373 entries), then
measured the worst case (outer cache bypassed):

| | Before | After |
|---|---|---|
| annual dialog data | 4,239 ms | **1,045 ms** |
| quarterly dialog data | 5,336 ms | 1,636 ms → 1,029 ms repeat |

With the outer cache and boot warmup, an open now does no database work at all.

---

## 30. Quarterly vs annual reporting date — mostly correct, data gap for 36

**Observation:** the Refresh dialog shows the same Reporting Date for Annual and
Quarterly.

**Measured:** of 355 tickers in both, **294 (83%) already differ** — e.g. AAPL
annual `Oct 30, 2025` vs quarterly `Jul 30, 2026`. The quarterly helper
(`_quarterly_report_dates_bulk`) correctly accepts any quarter and picks the
nearest upcoming, else most recent past.

**The 56 that match are a source-data gap, not a logic bug.** Today is
2026-08-16; ABBV's entire recent calendar is:

```
2026-02-04  Dec/2025      <- newest row
2025-10-31  Sep/2025
2025-07-31  Jun/2025
2025-04-25  Mar/2025
```

There is no Q1-2026 or Q2-2026 row, so "most recent past" *is* the annual date.
Same for ABT (`2026-01-22`) and BAC (`2026-01-14`).

Calendar coverage overall (`coreiq_nasdaq_earnings_calendar`, 355 tickers, last
fetched 2026-08-15):

| | Tickers |
|---|---|
| newest row is upcoming | 114 (32%) |
| newest row within 3 months | 316 (89%) |
| **newest row older than 6 months** | **36 (10%)** |
| newest row older than 12 months | 2 (1%) |

The ingestion is running; it is just not producing recent quarters for ~10% of
companies. That belongs with the data team.

---

## 31. The 37 "On hold" — genuinely missing dates, not a bug

Checked the raw calendar rows for the category-B companies from §22:

| Ticker | FYE | Rows in calendar | Quarters present |
|---|---|---|---|
| KVUE | December | **1** | Jun/2026 |
| MDB | January | 2 | Jul/2026 |
| HPE | October | 4 | Jul/2026 + three from **2016** |
| DXC | March | 1 | Jun/2026 |
| USFD | December | 2 | Jun/2026, Jun/2016 |
| GTM | December | 1 | Jun/2026 |

The annual (FYE-month) quarter row does not exist in the source table for any of
them, so no annual reporting date can be derived. The selection logic is correct;
the rows are absent. The only genuine *bug* among the 37 is the 9 corrupt
composite tickers (§22 group A), and that corruption lives upstream in
`coreiq_companies.exchange_acronym`.

