# Market Size Forecasting — design

**Date:** 2026-08-28
**Page:** `/market_size_forecasting` · `app/pages/market_size_forecasting.py`
**Engine:** `app/data/market_size_forecast_service.py`
**Tests:** `tests/test_market_size_forecast.py` (39)
**Source material:** `Sales_forecast_tool – Updated` (data science team) —
`app_New.py`, `chatbot.py`, `forecast-app/`, plus
`Sales_Forecast_Intelligence_Documentation.docx` and `TestingDataset.xlsx`.

---

## 1. What it does

Upload a spreadsheet of historical sales or market size; three models forecast
it and are compared on identical terms.

| Model | Inputs | Order selection |
|---|---|---|
| **SARIMAX** | sales + FRED macro indicators chosen by Granger causality | fixed (1,1,1)x(1,1,1,s) per combination, combinations ranked by held-out RMSE |
| **SARIMA** | sales only | grid search on AIC |
| **Prophet** | sales + a COVID-period regressor | Prophet's own changepoint detection |

Every model is validated the same way — walk-forward on recent periods it never
saw, reported as MAPE and RMSE — then residuals go through Ljung-Box,
Shapiro-Wilk and Durbin-Watson, with plain-language guidance on what to change
when a test fails.

Frequency (weekly / monthly / quarterly / annual) is inferred from the spacing
between dates and drives the seasonal period, horizon limits and validation
windows. Date and value columns are detected from the file; both are
overridable.

Tabs: **Diagnostics** · **SARIMAX** · **SARIMA** · **Prophet** ·
**Comparison** (only when two or more models ran).

---

## 1b. Why upload, and not the database — settled 2026-08-29

Confirmed by Nidhisha (data science), 2026-08-29, after the question was put to
her directly:

- **The uploaded dataset stays.** Market-size series are sourced ad hoc from
  various third-party websites in response to analyst requests, and are
  different every time. There is no fixed source to connect to, so the upload
  is the correct design — not a workaround for the data team lacking a DB.
- **The FRED variables must stay wired to the API, with the same structure.**
  They are the exogenous/independent variables for predicting any US-related
  sales, and are not a per-dataset input.
- `TestingDataset.xlsx` was only a sample for testing.

For the record, that sample was identified exactly (correlation 1.0000, 0.00%
mean error over 114 months): US Census core retail sales, from FRED as
`MRTSSM44W72USN − MRTSSM722USN` — retail sales excluding motor vehicles,
gasoline and food services, monthly, $M, NSA. It is a sample only; the page
does not read it.

**What this rules out.** An earlier proposal to drive the page from STG —
aggregating `coreiq_av_financials_income_statement` revenue by
`primary_industry_coresight` — is not the right target. It would answer a
different question (quarterly revenue of 407 listed companies) from the one
analysts ask (monthly whole-market size including private companies). It is
recorded here so it is not re-proposed. Three defects found while testing that
route, which any future aggregation of that table must handle:

1. **Staggered retail fiscal calendars.** In Apparel: 27 firms end quarters
   Jan/Apr/Jul/Oct, 8 end Feb/May/Aug/Nov, 21 end Mar/Jun/Sep/Dec. Grouping on
   `fiscal_date_ending` yields a sawtooth ($51.7bn → $17.5bn → $272.2bn), not
   seasonality. Assigning each fiscal quarter to the calendar quarter holding
   its midpoint fixes it.
2. **The newest quarter is always incomplete** (Apparel 2026-Q2: 38 of 56 firms
   reported) and reads as a collapse.
3. **Mixed and missing currency.** Summing all companies gives 2025 = $29.5tn,
   +43% year on year, because **SMFG reports in JPY** and is added as dollars
   (≈ +$15tn). A further **1,073 rows across 138 tickers have
   `reported_currency` NULL**, including 26 companies inside Apparel and
   Footwear Retail.

**No table was created for this page, and none is needed.** The page holds the
uploaded series in session, and caches fitted models on `/home` keyed by a hash
of the data — each new analyst dataset is simply a new key.

---

## 2. Architecture

```
app/pages/market_size_forecasting.py   UI only — controls, tabs, charts, exports
        │  calls
app/data/market_size_forecast_service.py   pure compute — no streamlit, no DB, no disk
        │  calls
   FRED API (api.stlouisfed.org)   32 monthly macro series, HTTPS, read-only
```

No database. Nothing is written to disk. Uploaded files stay in the Streamlit
session; results live in `st.session_state` under the `msf_` prefix.

### Data flow

1. `read_workbook` → `guess_date_column` / `guess_value_column` /
   `detect_frequency` — the page shows what was detected and lets the user
   override all three.
2. **Run analysis** → `load_series` normalises dates, coerces values, sums
   duplicate periods, drops unparseable rows, reindexes at the chosen frequency.
3. `data_diagnostics` — ADF, KPSS, seasonal strength and mode, outliers
   (|z| > 3), seasonal decomposition.
4. SARIMAX only: `fetch_fred_monthly` (cached 6h) → `align_fred` →
   `build_model_frame` → `difference_to_stationary` → `granger_scan` →
   `build_lagged_frame` → `rank_exog_combinations` → `run_sarimax`.
5. `run_sarima`, `run_prophet`.
6. Everything is computed in **one pass** and stored; tabs render from the
   stored result. Streamlit reruns the whole script on any widget change, so
   refitting per interaction would cost minutes.

The one exception is the SARIMAX **combination rank** selector, which refits a
single model (~0.3s) against the stored ranking table.

---

## 3. What changed from the standalone tool, and why

The maths is a faithful port. The SARIMA forecast for `TestingDataset.xlsx`
matches a verbatim run of the original script to the digit
(478,175 / 472,427 for Jul–Aug 2026; AIC 2030.5543; MAPE 1.400498%).
These are the differences:

### Required by the portal

| Change | Reason |
|---|---|
| All controls moved from the sidebar into the page body | Every portal page calls `hide_sidebar()` |
| No `st.set_page_config` | `app/main.py` already calls it once per run |
| `require_auth()` + `render_header` + `render_coresight_footer` | Portal page contract |
| FRED key from the `FRED_API_KEY` app setting, overridable per session | The original wrote `config.txt` next to the script; `wwwroot` is replaced on every deploy, and a shared API key does not belong in the repo |
| `results.json` not written | It existed only to feed the chat assistant |
| Charts rebuilt in Plotly | The portal's chart library; matplotlib is used only for Prophet's own components plot, forced to the `Agg` backend |
| Logging through `utils.server_logger` | Repo standard — `MSF_*` timing keys |
| Session keys namespaced `msf_` | Avoids collision with any other page |
| **AI chat assistant dropped** | `chatbot.py` requires an Ollama server on `localhost:11434` and a FastAPI backend on `:8000`; neither exists in the portal. The forecasting engine never depended on it. |

The 32 FRED variables are carried over **verbatim** — same series IDs, same
labels, same order — verified by diffing our `FRED_VARS` against theirs
(`same series ids: True`, `same labels: True`, `same order: True`). The
exogenous pipeline they specify is unchanged: Granger causality → F-statistic
ranking → lag shift on the dataset's own frequency → combination search on
held-out RMSE → hybrid future exog.

### Defects found and fixed at the root

1. **`grangercausalitytests(..., verbose=False)`** — statsmodels 0.15 removed
   the argument; the call raised `TypeError` and every SARIMAX run would have
   found zero significant variables. Argument dropped.

2. **Hardcoded seasonal period of 12.** The SARIMA grid searched at the data's
   real period but stored `(P, D, Q, 12)`, so the refit and the final forecast
   used a 12-period season on quarterly (s=4) and weekly (s=52) data. Now the
   stored order carries the data's own period. Monthly results are unchanged.
   Annual data (s=1, which statsmodels rejects) falls back to a plain ARIMA.

3. **Unbounded differencing.** The stationarity loop had only a length guard.
   On weekly data — where forward-filled monthly FRED series are step functions
   ADF never accepts — it differenced **76 times** and handed the Granger scan
   pure noise. Capped at `MAX_DIFFERENCING_ROUNDS = 3`; when the cap binds the
   page says the rankings are indicative. Monthly converges at 3, so the
   validated monthly path is unchanged.

4. **A numeric column could outscore the real date column.** Column detection
   scored candidates by "fraction that parses as a date", and
   `pd.to_datetime([90000.0, ...])` succeeds by reading bare numbers as
   nanoseconds since the epoch. With unhelpful column names (`when`,
   `how_much`) the sales column won, and detection then failed outright with
   "couldn't find both a date column and a numeric value column". A column of
   plain numbers that are not plausible years is no longer treated as dates.
   (Same family as the year-only bug the handover document records as fixed.)

5. **Tabs reset on the first combination-rank change.** Progress bars were
   drawn above `st.tabs` only on the run that performed the analysis, so the
   element tree shifted on the next rerun and the tabs remounted at
   Diagnostics. Progress now renders into a container that exists on every
   run, and `st.tabs` carries a key.

6. **Prophet's walk-forward dates crashed the tab.** Prophet returns its
   holdout dates as a Series; `pd.to_datetime(Series).strftime(...)` raises
   (`AttributeError: 'Series' object has no attribute 'strftime'` — a Series
   only has `.dt.strftime`). Both shapes are normalised to a `DatetimeIndex`.

7. **Downloading a forecast ejected the user from the section** and left them
   on Diagnostics permanently — see Charts and sections below.

8. **The result-cache size cap could never be changed.** `_evict_oldest` took
   `keep: int = MAX_CACHED_RESULTS` as a default argument, which Python
   evaluates once at import; the constant is now read at call time.

### Performance

The standalone tool refit everything on every click. Measured end to end in a
browser against `TestingDataset.xlsx` (114 monthly points, three models):

| Action | Server | What actually reran |
|---|---|---|
| First run ever, nothing cached anywhere | 12.7s | FRED pull (9.7s of it) + all three models |
| First run after a restart or deploy | **0.7s** | nothing — read back from `/home` |
| Run again, nothing changed | **33ms** | nothing |
| Change a Prophet prior | **1.4s** | Prophet only |
| Change the horizon | 6.1s | all three models |
| Change the combination rank | 0.5s | one SARIMAX fit |
| Switch section | 0.15–0.20s | one section's charts |
| Page render per rerun | **~25ms** (was ~400ms) | the active section only |

Five changes carry that:

**1. Per-stage caching, memory in front and disk behind.** Each stage is keyed
on exactly its own inputs, so nudging a Prophet prior refits Prophet alone. The
in-process cache dies with the process and STG redeploys several times a day,
so fitted models and the FRED panel also land on the persistent `/home` share —
the same reasoning as `EDGAR_CACHE_DIR`. A colleague opening the same series,
or anyone after a deploy, gets it back instead of refitting. Entries are keyed
by a hash of the series values and periods plus the parameters, capped at 200
files, TTL a week (24h for FRED), and written atomically. A corrupt or
unwritable cache degrades to "recompute", never to an error.

A cached function may not call a Streamlit element: `st.cache_data` records and
replays element calls, and replaying one that targets a block created outside
the function raises. A progress callback inside these wrappers made every
cached call fail with *"a streamlit element is called on some layout block
created outside the function"*, which surfaced to the user as *"SARIMA could
not be fitted"*. Progress is reported per stage from outside.

**2. The FRED pull: pooled, parallel, overlapped, persisted.** One
`requests.Session` with a sized connection pool instead of 32 fresh TLS
handshakes (measured 9.8s → 3.7s), 16 workers, started on a worker thread
before SARIMA and Prophet so its network wait hides behind their CPU, and
cached to disk for 24h. From a laptop in India the pull varies 3.7–15.7s; STG
sits in centralus next to FRED.

**3. Prophet cross-validation runs on threads.** `parallel="threads"` — the
Stan backend releases the GIL, so the dozen cutpoint fits overlap. Measured
2,230ms → 558ms for the main CV; whole model 4.3s → 1.4s. The original also
fit a third, identical model for the final projection; that is gone.

**4. SARIMA and Prophet fit concurrently.** They share nothing, and Prophet is
mostly Stan, so it rides along behind SARIMA's grid rather than queueing after
it.

**5. One section renders per run.** See below — this took the render step from
~400ms to ~25ms.

Not changed, deliberately: the SARIMA AIC grid is still exhaustive (36 fits).
Threads do not help — statsmodels' Kalman filter holds the GIL — and a process
pool returned only 1.36x here because each spawned worker re-imports
statsmodels; on STG's 2 vCPU it would be worse. BLAS threading makes no
difference (the matrices are tiny). The fit options that *are* faster all
change the selected model — `enforce_stationarity=False` (1.97x),
`concentrate_scale=True` (2.61x), `method='powell'` — so none were taken; only
`cov_type='none'` in the grid, which cannot affect AIC. Weekly data (s=52,
~76s for a first run) says so in the UI before you press the button.

### Weekly was unusable — seasonal AR/MA at lag 52 (fixed 2026-09-15)

Reported by data science: *"Run analysis takes forever"* on weekly data
(288 weekly points, 260-week horizon).

**Root cause, measured.** A seasonal AR or MA term at lag 52 is estimated from
one observation per annual cycle. Each such fit costs **~3.6s** against **~41ms**
for the same model without them — roughly 90x. The SARIMA grid fits 36 of them
and the SARIMAX combination search another 10, so:

| Stage | Before | After |
|---|---|---|
| SARIMA grid (36 fits) | **49.6s** | 1.8s |
| SARIMAX combination search (10 fits) | ~36s | 2.1s |
| Whole weekly run, compute only | ~86s | **~6s** |
| Whole weekly run in the browser, incl. cold FRED | never finished | **13.8s** (7.8s server) |

On STG, which runs these single-threaded fits roughly 7x slower than a laptop,
the old path was several minutes — the "forever".

**The fix.** With 288 weekly points you have 5.5 annual cycles; seasonal AR/MA
at lag 52 is not identifiable from that, so searching it bought nothing and cost
everything. `seasonal_terms_supported(sample_size, season)` now gates the search:

```python
MIN_CYCLES_FOR_SEASONAL_TERMS = 8
LARGE_SEASON = 26

def seasonal_terms_supported(sample_size, season):
    if season < 2:            return False   # annual: no season at all
    if season < LARGE_SEASON: return True    # monthly (12), quarterly (4): always
    return sample_size >= MIN_CYCLES_FOR_SEASONAL_TERMS * season
```

When it returns False the models keep **seasonal differencing** (`D=1`, which is
what actually removes the annual pattern) and stop searching `P`/`Q`. The SARIMA
tab says so explicitly, naming the cycle count.

**Monthly and quarterly are deliberately never affected.** `LARGE_SEASON`
short-circuits them, so the exhaustive grid the research team validated still
runs. Verified after the change, in the browser: order still
`(2,1,2)x(0,1,0,12)`, AIC still `2114.3794`, forecast still
**478,175 / 472,427** — identical to a verbatim run of `app_New.py`. Five tests
pin the gate, including that monthly keeps searching seasonal terms and that
short weekly drops `P`/`Q` while keeping `D=1`.

**Judgement recorded:** this does change which model weekly data selects. That
is a change from "no result at all" to "a result in seconds", on a series whose
length cannot support the terms being dropped. `simple_differencing=True` was
measured as an alternative (7.6x faster) and rejected — it changes the selected
order on monthly too, which would have broken parity.

### Charts and sections

Same conventions as the Revenue Estimates page: `lines+markers`, 3px lines,
`hovertemplate`, `displayModeBar` off, a dotted "Forecast begins" divider at
the last actual period, and KPI cards rather than `st.metric`.

**`st.tabs` was replaced by a keyed `st.segmented_control`, styled as tabs.**
`st.tabs` keeps its selection only in the browser, so any rerun that remounts
it silently reset to the first tab — a download button click did exactly that
and *stayed* there, so a user could never download twice from the same tab.
The selector's state lives in `session_state` and survives anything.

It also renders one section instead of five, which fixed a second bug for
free: Streamlit renders every tab body up front, a body that is not active has
no layout width, and plotly therefore baked in its 700px default and never
reflowed. Every chart outside Diagnostics was drawing at 700px inside a 1220px
column. Neither a window `resize` event nor `Plotly.Plots.resize` clears that
(only `Plotly.relayout` did) — but with one section rendered, charts always lay
out visible and the JavaScript workaround was deleted.

---

## 4. Behaviour on awkward inputs

These are deliberate, and verified end to end in the browser:

| Input | Behaviour |
|---|---|
| Fewer than 24 points | SARIMAX skipped with a message; SARIMA and Prophet run |
| Too little history for Prophet CV | CV skipped; walk-forward still runs |
| Short series | Ljung-Box lags scale down (e.g. 8 and 14 instead of 10 and 20) |
| Annual data | No seasonal decomposition, no seasonal order; SARIMAX usually blocked by the 24-point minimum |
| Weekly data | Notice that monthly FRED is forward-filled, so the macro signal is monthly-resolution |
| Bare-year dates (`2020`) | 31 December of that year |
| Quarter labels (`Q1 2020`, `2020Q1`, `2020-Q3`, `Q4-2020`) | Quarter end |
| Duplicate periods | Summed, and reported in Diagnostics |
| Unreadable dates | Dropped, and reported in Diagnostics |
| No FRED key | SARIMAX skipped with instructions; other models run |
| Wrong FRED key | Named as the likely cause; other models run |
| No Granger-significant variables | SARIMAX skipped with a suggestion to raise the lag |
| No model selected | Warning, Run button disabled |
| One model selected | No Comparison tab |

---

## 5. Deployment

### Dependencies added to `requirements.txt`

```
statsmodels>=0.14.0   # SARIMAX/SARIMA, ADF/KPSS, Granger, Ljung-Box, Durbin-Watson
prophet>=1.1.5        # pulls cmdstanpy + matplotlib
```

Verified installing and running on the STG runtime version (Python 3.14):
statsmodels 0.15.0, prophet 1.4.0, matplotlib 3.11.1 (a Prophet dependency).
Prophet is the fragile one — it ships a Stan backend — so **confirm the STG
image builds it before announcing the page**.

### App settings to add

```bash
az webapp config appsettings set -g csr-awa-rg-stg -n csr-awa-data-portal-stg \
  --settings FRED_API_KEY=<key> MSF_CACHE_DIR=/home/msf_cache
```

`MSF_CACHE_DIR` is what makes a fitted model and the FRED panel survive a
deploy — without it the cache falls back to `/tmp`, which is wiped on every
restart, and the first user after each deploy pays the full pull again. It
needs `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`, which STG already has.
The directory holds a few hundred KB per entry and is capped at 200 entries.

Without it the page still works: SARIMA and Prophet run, and SARIMAX asks for a
key in the UI. Outbound HTTPS to `api.stlouisfed.org` must be permitted.

### Access

Registered in `app/main.py` as `("pages/market_size_forecasting.py", "Market
Size Forecasting", "market_size_forecasting", False)` and linked from the
footer **Quick Links** column in `app/components/navigation.py`. Open to every
authenticated user — no ACL gate, matching Screening and Market Data. Add a
`coreiq_page_access_control` row if that should change.

### Cost profile

CPU-bound with no DB traffic. The only slow moment is the very first run on a
series nobody has forecast before: ~12.7s on the test dataset, ~9.7s of which
is the FRED pull from a laptop in India (STG sits next to FRED in centralus).
Everything after that — including the first run after a deploy — is 33ms to a
few seconds; see Performance. Weekly data is the slow first case (~76s) because
the SARIMA grid fits 36 models at a 52-period season; the page says so before
you run it. Nothing runs on boot — the models only run when a user clicks
**Run analysis**.

---

## 6. Testing

`tests/test_market_size_forecast.py` — 39 tests, no network, no DB. Frequency
detection, date normalisation (bare years, quarter labels, unparseable),
column detection including the numeric-column trap, duplicate and dropped
rows, the COVID dummy at each cadence, seasonal-order selection, validation
window capping, residual diagnostics on short series, SARIMA on monthly and
quarterly data, forecast-spread measurement, the differencing cap, and the
FRED failure paths (rejected key, one broken variable, resampling both ways).

UI verification against `http://localhost:8501/market_size_forecasting`
(launcher: `bash .claude/dev/run_local.sh`) with the supplied
`TestingDataset.xlsx` plus generated weekly, quarterly-with-Q-labels,
annual-bare-year, short-monthly, messy and CSV fixtures: upload, detection,
run, all five tabs, combination-rank refit, and all eight download buttons.
Chart widths are asserted per tab (every chart 1206–1220px inside a 1220px
column), and the cold/warm/one-setting-changed timings in the Performance
table are measured from the browser, not estimated. A screen recording of the
whole flow — branded loader, stage progress, every tab — is at
`~/Downloads/market_size_forecasting.gif`.

**Not carried over and therefore not tested:** the Ollama chat assistant, the
FastAPI backend and the Next.js chat frontend.
