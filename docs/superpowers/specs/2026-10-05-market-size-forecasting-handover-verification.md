# Market Size Forecasting — verification against the data team's handover

**Date:** 2026-10-05
**Scope:** the `/market_size_forecasting` page and `app/data/market_size_forecast_service.py`,
verified against the forecasting tool the data team handed over.

**Handover received:** `~/Downloads/Salesforecastingtool/` —
`app_New.py` (2,664 lines, identical by SHA-256 to `Sales_forecast_tool –Updated/app.py`),
`TestingDataset.xlsx` (114 monthly points), `results.json` (a run they recorded on
that fixture), `Sales_Forecast_Intelligence_Documentation.docx`, `config.txt`
(their FRED key), `chatbot.py`, `start.py`, and the FastAPI + Next.js chat app.

---

## 1. Headline

Their app was executed **unmodified** inside the portal's own Python environment and
compared, stage by stage, with the portal's engine on the same inputs.

| Model | What was compared | Largest difference |
|---|---|---|
| SARIMA | order, seasonal order, AIC, MAPE, RMSE, walk-forward, 60-period forecast | **0** |
| Prophet | CV MAPE (both variants), MAPE, RMSE, walk-forward, 60-period forecast, all 25 changepoint deltas | **0** |
| SARIMAX | model frame, differencing rounds, Granger F-statistics, ranking, exog selection, combination RMSE table, chosen order, walk-forward, forecast and confidence bands | **0** |

Not "close" — bit-identical, to full float precision, at every stage.

The forecasting logic is unchanged. Everything fixed this session is in reading the
file and showing the result, never in the mathematics.

---

## 2. How that was established

Three artefacts, in `scripts/`, all re-runnable:

**`run_data_team_app_headless.py`** executes their `app_New.py` as-is. Streamlit is
replaced by a stand-in that returns each widget's own default and discards every draw
call, so the numeric code is theirs byte for byte; only the uploaded file and the
sidebar values are injected. It pickles the variables holding the results.

**`compare_port_to_data_team.py`** runs that pickle against our engine and against
their recorded `results.json`, three ways at once.

**`compare_sarimax_to_data_team.py`** does SARIMAX stage by stage. SARIMAX is the one
model whose inputs are not purely the uploaded file — it pulls 32 FRED series live —
so this feeds our pipeline the exact macro panel *their* run captured. After that the
only variable left is the code.

```bash
.venv/bin/python scripts/run_data_team_app_headless.py \
    --excel ~/Downloads/Salesforecastingtool/TestingDataset.xlsx \
    --horizon 60 --models SARIMA,Prophet --out /tmp/theirs.pkl
.venv/bin/python scripts/compare_port_to_data_team.py --theirs /tmp/theirs.pkl
```

Expected last line: `VERDICT: PORT IS FAITHFUL`.

### Their recorded run, and why it differs slightly from both of us

`results.json` was produced on their machine on older libraries. Against it, both
engines land in the same place:

| | our engine vs `results.json` | **their app** vs `results.json` |
|---|---|---|
| SARIMA 60-period forecast | 14.94 (0.0027% of level) | **14.94 (0.0027%)** |
| Prophet 60-period forecast | 672.21 (0.134% of level) | **672.21 (0.134%)** |

Their own app no longer reproduces their own recorded numbers *by exactly the amount
we don't*. The drift is statsmodels 0.14 → 0.15 and prophet 1.1.5 → 1.4.0, both of
which satisfy their own `requirements.txt` (`statsmodels>=0.14`, `prophet>=1.1.5`).
Model selection is unaffected: SARIMA picks `(2,1,2)x(0,1,0,12)` with AIC 1904.61 on
all three, and Prophet's CV MAPE is 2.06% / 2.18% on all three.

---

## 3. Two things that stop their tool running on today's libraries

Found while reproducing their results, and relevant to any plan to keep the original
around as a reference implementation.

**SARIMAX is silently disabled.** Their Granger scan calls
`grangercausalitytests(..., verbose=False)`. statsmodels 0.15 removed `verbose`, so
every variable raises `TypeError` inside their own `except Exception: pass`. The page
then reports "No Granger-significant variables found" and SARIMAX never runs — on
their own test dataset, where 31 variables are in fact significant. Our port had
already dropped that argument. To compare SARIMAX at all, the headless runner has a
`--compat` flag that removes this one token and prints the substitution on every run.

**A file with a missing month kills the app.** On a fixture with four absent months,
their app raises `ValueError: No supported index is available` partway through SARIMA
and never reaches Prophet. The portal handles the same file (Prophet on it is
bit-identical to theirs when SARIMA is deselected so their app survives to get there).

---

## 4. Where the portal deliberately behaves differently

All of this is **off the monthly path**. Monthly — the frequency the research team
validated, and the one `results.json` records — is bit-identical in every case.

### 4.1 SARIMA's seasonal period on non-monthly data

Their grid searches at the data's real seasonal period but stores and refits the
winner at a hardcoded 12:

```python
mf = SARIMAX(sm_train, order=(p,d,q), seasonal_order=(P,D,Q,FC['s'])).fit(...)
if mf.aic < best_aic:
    best_aic, SM_ORDER, SM_S_ORDER = mf.aic, (p,d,q), (P,D,Q,12)   # <- always 12
```

Their own documentation lists this class of bug as fixed ("Forecast dates and lag
alignment corrected to use the dataset's own frequency; previously hardcoded
monthly") — this one line was missed. Measured:

| Frequency | their order | our order | their AIC | our AIC | their MAPE | our MAPE | forecast gap |
|---|---|---|---|---|---|---|---|
| Quarterly | (2,1,2)x(0,1,0,**12**) | (2,1,2)x(0,1,0,**4**) | 671.21 | 671.21 | 0.85 | **0.57** | 1.88% |
| Weekly | (2,1,2)x(0,1,1,**12**) | (2,1,2)x(0,1,0,**52**) | 386.18 | 446.68 | 0.0058 | **0.0004** | 0.0059% |
| Annual | (1,1,1)x(0,1,1,**12**) | (2,1,1)x(0,0,0,0) | **inf** | 60.21 | 0.0000 | 0.0000 | 0.2 on 4.0M |

Quarterly is the one that matters: the AIC is identical, so the *search* agrees
exactly — only the refit differs, and ours validates better. Annual `AIC = inf` means
all 36 of their grid fits raised: statsmodels rejects a seasonal period of 1, so their
annual model is the hardcoded fallback rather than anything searched.

**This is a deviation from their code and needs the data scientist's word.** To match
their behaviour exactly instead, change one line in `run_sarima`:
`best_seasonal = _seasonal_order((P, D, Q), season)` → `(P, D, Q, 12)`. I have not
done so, because it reinstates a bug their documentation says was removed.

### 4.2 Weekly SARIMAX differencing

Their differencing loop runs until every column passes ADF, bounded only by series
length. On weekly data, where forward-filled monthly FRED series are step functions
ADF never accepts, it runs away. Measured on 250 weekly points × 34 columns:

| | differences | rows left | "significant" variables |
|---|---|---|---|
| their loop | **146** | 104 of 250 | 32 of 33 |
| ours (`MAX_DIFFERENCING_ROUNDS = 3`) | 3 | 247 of 250 | 16 |

146 differences is not a model; finding 32 of 33 variables significant is what noise
looks like. Monthly converges at 3 either way, so the validated path is untouched.
Also a deviation, also reversible by raising the constant.

### 4.3 Three differences that turned out to be none

- Prophet cross-validation runs with `parallel="threads"`. Verified identical.
- Prophet's final forecast reuses the already-fitted model rather than refitting a
  third identical one. Verified identical.
- Prophet's validation window counts blank periods in their code and not in ours. The
  window is capped at `max(s, 12)` before that ever binds, so on a gapped file the
  forecasts are identical.

### 4.4 One operational caveat, not a logic change

The FRED panel is cached on disk for 24 hours. In one measurement a day-old panel
moved the SARIMAX forecast by 0.35% versus a live pull — nothing else, since SARIMA
and Prophet don't use FRED. If the data scientist wants SARIMAX pinned to a same-day
panel, lower `FRED_DISK_TTL_HOURS`; the cost is ~3.6s added to the first run of the day.

---

## 4b. The same comparison across 24 different datasets

§1 proves fidelity on their fixture. This proves it on data that varies the way real
uploads vary: `scripts/build_sector_datasets.py` writes 24 series spanning magnitude
(a 4.8% market share, 0.84 sub-unit values, 1,250 units, $45M, $12.4B, $1.24
trillion), shape (flat, 14.5% growth, structural decline, COVID collapse, COVID boom,
a 2022 step down), seasonality (none to a hard December peak), sign (crosses zero,
all-negative, contains true zeros), length (18, 24, 120, 240, 260 points) and every
frequency. `scripts/sweep_datasets_against_data_team.py` runs their app and ours over
all of them.

**24 datasets · 327 comparisons · 20 mismatches · 0 unexplained.**

**20 of 24 datasets matched on every single check**, SARIMAX included — every monthly
sector, every magnitude, negatives, zero-crossing, near-constant, 18 / 24 / 240
points, structural decline, COVID break.

All 20 mismatches fall in the four datasets below, and every one is a deviation
already documented in §4.1 / §4.2:

| Dataset | What differs | Cause |
|---|---|---|
| `quarterly_apparel` | SARIMA order `(0,1,0)x(0,1,0,`**`12`**`)` vs ours `…,`**`4`**`)`, forecast 5,120 | §4.1 hardcoded 12 |
| `weekly_grocery` | SARIMA order + Granger set (32 vs 20) | §4.1 **and** §4.2 |
| `annual_total_retail` | SARIMA order + Granger set (0 vs 9) | §4.1 **and** §4.2 |
| `edge_contains_zeros` | MAPE `inf` on **both** sides | measure undefined at zero — see §6 |

Prophet matched on **all 24** datasets without exception. SARIMAX matched on every
monthly and quarterly dataset.

The two Granger differences are the differencing cap, measured directly:

| | their loop | ours |
|---|---|---|
| annual, 28 rows | 16 differences → 12 rows left → **0** significant | 3 → 25 rows → 9 |
| weekly, 260 rows | 156 differences → 104 rows left → **32 of 33** significant | 3 → 257 rows → 20 |

Their loop destroys the data in both directions — nothing survives on annual, and
everything looks significant on weekly.

### Macro drivers are selected per dataset, not from a fixed list

Across the 23 datasets where SARIMAX ran, the combination search chose **20 distinct
driver-and-lag combinations**. A sample:

| Dataset | Chosen driver |
|---|---|
| `apparel_specialty` | Manufacturers: Inventories to Sales Ratio (4m ago) |
| `home_improvement` | Manufacturers: Inventories to Sales Ratio (4m ago) |
| `grocery_food_retail` | Manufacturers New Orders: Nondurable Goods (1m ago) |
| `consumer_electronics` | Total Business Inventories (2m ago) |
| `ecommerce_pureplay` | Manufacturers New Orders: Durable Goods (4m ago) |
| `restaurant_foodservice` | Labor Force Participation Rate (1m ago) |
| `scale_market_share_pct` | S&P/Case-Shiller U.S. National Home Price Index (2m ago) |
| `luxury_goods` | Producer Price Index by Commodity: All Commodities (1m ago) |

`length_minimum_24` is the one dataset where SARIMAX found no drivers at all: 24
points cannot support a 12-lag Granger test. The page says so and still runs SARIMA
and Prophet.

---

## 5. File formats: 24 shapes, both readers

`scripts/build_ingestion_fixtures.py` writes one workbook per format in their
documented accepted-formats table plus the shapes the data team's files actually
arrive in. `scripts/sweep_ingestion_formats.py` reads each one with their loader
(lifted from their module, not retyped) and with ours.

**10 files the portal reads that their tool cannot read at all. 0 regressions.**

| File | theirs | ours | |
|---|---|---|---|
| `2017-01-01` period-start dates | 0 rows | 114 | their documented "full date" |
| `2020-01` | 0 rows | 114 | **their documented format** |
| `Jan 2020` | 0 rows | 114 | **their documented format** |
| `Dec '22` / `Jan’23` | refused | 114 | straight and curly apostrophes |
| `Dec-22` | refused | 114 | Excel's own short display |
| `2020M01` | refused | 114 | FRED and BLS month labels |
| `$243,826` | refused | 114 | currency + thousands separators |
| `(87)` | refused | 114 | accounting negatives |
| `.csv` | rejected | 114 | **their documentation claims CSV support** |

Three of those are formats their own handover document promises and their own code
cannot read. The rest are what the data team keeps sending.

Everything their tool reads, the portal reads identically: full dates, all three
quarter spellings, year-only, no column-name hints, extra columns, duplicate dates
(combined), unreadable dates (dropped), weekly, annual, short series.

---

## 6. Defects found and fixed this session

**Formatted value columns were refused before the parser ran.**
`guess_value_column` scored candidates with plain `pd.to_numeric`, so a column of
`$243,826` or `(87)` scored 0 and the page said "no numeric value column found" —
about a file `to_numeric_values` could read perfectly. Detection now scores with the
same reader the loader uses. (`_value_score`, 4 new tests.)

**A single blank period disabled SARIMAX.**
`rank_exog_combinations` and `run_sarimax` both `.dropna()`, which leaves a
`DatetimeIndex` with no `freq`; SARIMAX answers that with "No supported index is
available" rather than fitting. Every combination failed, and the page said so without
saying why — on exactly the file shape the data team's real file has. `positional_if_irregular`
numbers the rows when the dates are no longer regular; the index here only labels
output, and both callers relabel from the dated frame, so no number moves. Verified:
gapless results still bit-identical to their app; gapped files now fit 10 of 10
combinations. (3 new tests.)

**The combination search swallowed every failure.**
Its `except Exception: pass` meant "every variable combination failed to fit" was all
anyone ever got. The first failure is now logged with the frequency, combination
count, row count and window.

**A series containing zeros printed `inf%` and picked a meaningless winner.**
MAPE divides by each actual value, so one true zero makes it infinite — for every
model, on both engines, because it is their formula and it is correct. But the page
printed `inf%` and `Accuracy -inf%`, and `min(results, key=mape)` then declared a
"best" model by whichever happened to be first in the dict. `_accuracy` is untouched;
the display now reads **n/a**, the ranking falls back to **RMSE** (already computed,
finite, in the series' own units), and a note explains why. Verified in the browser:
the hero chip reads "Best: SARIMAX · 66,791 RMSE". (4 new tests.)

**The progress bar was Streamlit blue under a red spinner.**
`st.progress` is painted from the theme's `primaryColor` and no stylesheet of ours
reached inside it. The branded loader now carries the stage text and percentage
itself, so there is one indicator and it is on brand.

Test suite: **99 passed** (`tests/test_market_size_forecast.py`).

---

## 7. Live checks

**FRED API** — 32 of 32 series in 3.6s with the key from their `config.txt`, current
through September 2026. No failures.

**Browser sweep** — `.claude/dev/msf_ui_sweep.py` drives the real page for all 24
format fixtures AND all 24 sector/scale datasets: upload, Run analysis, then every
section. **48 of 48 rendered all five sections with no exceptions and no error
banners.** From cold caches, the format fixtures rendered
all five sections (Diagnostics, SARIMAX, SARIMA, Prophet, Comparison) with **no
exceptions and no error banners**. 155 screenshots and a screen recording accompany
this document. Click-to-result in the browser: min 3.3s, median 6.3s.

The branded loader was captured mid-run on 11 of 24 — the rest finished from cache
before a frame could be grabbed.

**Timings** (114 monthly points, all three models, 60-month horizon):

| Stage | cold ms |
|---|---|
| read workbook, detect columns, load, diagnostics | 64 |
| SARIMA grid (36 fits) | 3,393 |
| Prophet fit + forecast (CV deferred) | 118 |
| FRED pull | 3,600 live / 25 cached |
| differencing + Granger scan | 346 |
| SARIMAX combination search (10 fits) | 2,182 |
| SARIMAX validate + forecast | 514 |
| **total** | **6.7s** |

Repeat runs of the same file and settings return from cache: **0.9–1.4s** measured
click-to-result in the browser.

**One outlier worth knowing about.** On annual data Prophet costs **12.7s against
SARIMA's 0.2s** on the same 30 points — it is 98% of that page's wait. The cause is
in their model construction: `yearly_seasonality=True` is passed unconditionally, so
on annual data Prophet tries to fit a yearly cycle to one observation per year and
the optimiser labours. Changing it would change their model, so it is reported, not
touched. If the data scientist is willing to set `yearly_seasonality=False` when the
frequency is Annual, that page goes from ~18s to under 1s.

A cold run cannot reach 1–2s without computing less. The time is 46 model fits, and
their specification is what sets that number: the 36-model SARIMA grid and the
combination search are 83% of it. Threads do not help (statsmodels holds the GIL
through a fit); processes measured 1.28x on STG's 2 vCPU. Reaching 1–2s on a *first*
click means moving the work off the click — precomputing on upload, or returning
SARIMA and Prophet first and streaming SARIMAX in behind them. Both change how the
page behaves, so neither is built.

---

## 8. Files

**Engine / page**
- `app/data/market_size_forecast_service.py`
- `app/pages/market_size_forecasting.py`

**Tests**
- `tests/test_market_size_forecast.py` (87 → 95)

**Verification scripts**
- `scripts/run_data_team_app_headless.py`
- `scripts/compare_port_to_data_team.py`
- `scripts/compare_sarimax_to_data_team.py`
- `scripts/verify_against_data_team_results.py`
- `scripts/build_ingestion_fixtures.py`
- `scripts/sweep_ingestion_formats.py`
- `scripts/time_forecast_stages.py`
- `scripts/build_sector_datasets.py`
- `scripts/sweep_datasets_against_data_team.py`

**Dev harness**
- `.claude/dev/msf_ui_sweep.py`

**Docs**
- `docs/superpowers/specs/2026-10-05-market-size-forecasting-handover-verification.md`

---

## 9. Open questions for the data scientist

1. Non-monthly SARIMA seasonal period (§4.1) — keep the portal's corrected behaviour,
   or reinstate their hardcoded 12? Monthly is unaffected either way.
2. Weekly SARIMAX differencing cap (§4.2) — keep 3, or restore the unbounded loop?
3. FRED cache freshness (§4.4) — is a panel up to 24 hours old acceptable for SARIMAX?
4. Prophet on annual data (§7) — may `yearly_seasonality` be set to `False` when the
   frequency is Annual? It is 12.7s of a ~18s wait, and a yearly cycle cannot be
   estimated from one observation per year in any case.
