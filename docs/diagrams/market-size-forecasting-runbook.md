# Market Size Forecasting — Runbook

Companion to `market-size-forecasting-flow.html` (open it in any browser; it is a
single self-contained file with search, focus, tracing and four guided chapters).
Each heading below is one box in that diagram.

Page: `/market_size_forecasting` · Source: `app/pages/market_size_forecasting.py`,
`app/data/market_size_forecast_service.py`

---

## Reading the diagram

Open `market-size-forecasting-flow.html` in any browser — one file, nothing to install.

- **Three lanes**: what the analyst does in the browser, what the pipeline does on
  the server, and the FRED branch that only runs when SARIMAX is ticked.
- **Click any box** to open its Semantic Passport: kind, lane and phase, tags,
  every incoming and outgoing link, and the verified source files with line
  numbers. *Copy link* deep-links straight to that box.
- **PATH / LENS** in the bottom bar trace a route between two boxes and summarise
  node kinds. **Node index** below the diagram lists every box.
- **Zoom in, or use LENS**, to reveal the fine-detail tag on each box (`≤ 200 MB`,
  `period-END dates`, `24 h disk cache`, `nothing runs on load`, `cached per stage`,
  `Blob, no rerun`). They are hidden at the default zoom on purpose so the overview
  stays readable.
- **Light / Dark** and **Export** are in the top right; export produces a truthful
  copy of what you see.

---

## Before you start

You need a sales or market-size history with **two usable columns**: one holding
the period, one holding the value. Anything else in the file is ignored.

The dataset does **not** come from our database, and that is deliberate. Market
size requests arrive from different third-party sources every time and vary by
analyst, so the upload stays manual. Only the FRED macro predictors are wired to
a live API.

**Dates must be stamped at the END of the period.** `2016-01-31`, not
`2016-01-01`; `2016-Q1` as `2016-03-31`; annual as `2016-12-31`. A first-of-period
file currently produces `Only 0 usable rows after cleaning`. This is a known open
bug, not a problem with your file — see *Known traps*.

---

## 1 · Upload history  → `analyst` lane, column 0

Drag an `.xlsx`, `.xls` or `.csv` into **Sales History**, or use *Browse files*.
The cap is 200 MB, far above anything this tool needs.

Nothing is computed on upload. The file is parsed only far enough to offer you
column choices.

---

## 2 · Detect + clean  → `pipeline` lane, column 1

The page inspects your file and pre-selects three things. Check all three; any of
them can be overridden from the dropdowns.

**Date column.** Real dates, bare years (`2016`) and quarter labels (`2024-Q3`)
all parse. A column that is entirely numeric is deliberately *refused* as a date
column — without that guard a sales column of values like `90000.0` gets read as
epoch nanoseconds and can outscore the real date column.

**Value column.** Coerced with `to_numeric`. Cells that are not numbers become
blanks rather than raising.

**Frequency.** Weekly, Monthly, Quarterly or Annual. This is not cosmetic — it
sets the seasonal period `s`, the Granger lag ceiling, the default forecast
horizon and the validation window for everything downstream.

Then `load_series` cleans:

| Step | What it does |
|---|---|
| Parse dates | Unparseable rows are dropped and counted |
| Merge duplicates | Repeated periods are summed, not silently kept |
| Snap to calendar | The series is reindexed onto a gap-free period grid |

The header chips report exactly how many rows were dropped and how many duplicate
periods were merged. Read them — a large drop count means the wrong date column.

---

## 3 · Configure run  → `analyst` lane, column 2

**Models.** Tick any combination:

- **SARIMAX · FRED macro drivers** — the series plus external US macro predictors.
- **SARIMA · series only** — no external data; the honest baseline.
- **Prophet · trend and seasonality** — additive trend/seasonality with changepoints.

**Forecast horizon.** How many periods ahead, in your cadence's units. The slider
maximum is capped per cadence.

**Max Granger lag.** How far back the driver scan looks for lead/lag relationships.
Capped per cadence.

**SARIMAX settings / Prophet settings.** Collapsed by default. Open them only if
you need non-default priors or search bounds; the defaults are the data science
team's.

---

## 4 · Run analysis  → `pipeline` lane, column 3

One click, one pass. Nothing runs on page load — models start only here.

The run is deliberately a single pass rather than per-interaction refitting,
because Streamlit re-executes the whole script on any widget change and refitting
each time would cost minutes.

Stages, in order:

1. **Diagnostics** — ADF and KPSS stationarity, seasonal strength, additive vs
   multiplicative mode, peak and trough period, outliers at `|z| > 3`, decomposition.
2. **FRED pull** — only if SARIMAX is ticked (see box 5).
3. **Driver ranking** — only if SARIMAX is ticked (see box 6).
4. **Model fitting and scoring** — see box 7.

---

## 5 · Fetch FRED  → `macro` lane, column 3 · *SARIMAX only*

32 US macro series are pulled from the St. Louis Fed API over 16 threads, then
resampled to your cadence.

Results are cached on disk for 24 hours, on the persistent `/home` share, so they
survive both a restart and an app deploy.

If the API key is missing or rejected, **SARIMAX is skipped rather than failed** —
SARIMA and Prophet still run and the comparison still renders. Check the header
chip before treating a comparison as complete.

These are US series. A UK or India market size has no appropriate predictor here;
use SARIMA or Prophet for non-US data.

---

## 6 · Rank drivers  → `macro` lane, column 4 · *SARIMAX only*

1. **Granger causality scan** across all 32 series up to your Max Granger lag.
2. **Lagged frame build** — each surviving driver is aligned at its best lag.
3. **Combination search** — combinations of drivers are scored and ranked.

The chosen set becomes SARIMAX's exogenous input. The ranked table is visible in
the SARIMAX section, and you can refit on a different rank from there.

---

## 7 · Fit + score  → `pipeline` lane, column 4

Each ticked model is fitted, then scored by **walk-forward validation** — the model
is repeatedly refitted on a growing window and asked to predict the next block, so
the reported MAPE reflects out-of-sample behaviour rather than fit quality.

The winner appears as **Best** in the header.

Every stage caches independently on its own inputs. Nudging a Prophet prior
refits Prophet only — it does not refit SARIMA and does not re-pull FRED. A repeat
run with identical settings returns from cache in milliseconds.

**Weekly is the slow cadence.** At a 52-period season, seasonal AR/MA terms are
only searched when the series has at least 8 years of history; below that the
model keeps seasonal differencing and skips those terms. Without that gate a
single fit costs ~3.6 s against ~41 ms, and the grid never finishes. The page
tells you when the restriction is active.

---

## 8 · Review + export  → `analyst` lane, column 5

Five sections, selected from the strip under the header: **Diagnostics, SARIMAX,
SARIMA, Prophet, Comparison**.

Only the selected section renders. That is why switching is instant instead of
redrawing five sets of charts, and it is also why the section survives a download
click.

**Downloads.** Every model section and the Comparison section carry *Download CSV*
and *Download Excel*. **Diagnostics has no exports** — switch to a model section
first.

The buttons fetch the file into a Blob inside the page and save it locally. They
do not trigger a page rerun, and they do not hand the browser a URL to follow.
A correct download is named for its content — `prophet_forecast.csv`. If you ever
see a long hex filename, you are on an old build.

---

## Known traps

| Symptom | Cause | What to do |
|---|---|---|
| `Only 0 usable rows after cleaning` | Dates stamped at the first of the period; the cleaner reindexes onto period-END labels and everything becomes blank | Restamp to period-END (`2016-01-31`). Open bug |
| SARIMAX missing from the results | FRED rejected the API key | Read the header chip; SARIMA and Prophet are still valid |
| No download buttons | You are on the Diagnostics section | Switch to SARIMAX, SARIMA, Prophet or Comparison |
| Download saves as a long hex name | Old build using the anchor download | Redeploy; current builds name the file for its content |
| SARIMAX drivers look irrelevant | The 32 predictors are US macro series | For non-US market sizes use SARIMA or Prophet |
| Weekly run feels slow the first time | 52-period seasonal grid | Expected; the second run is cached. See box 7 |

---

## Regenerating the diagram

```bash
cd ~/.claude/skills/archify
node bin/archify.mjs validate workflow <repo>/docs/diagrams/market-size-forecasting-flow.workflow.json --quality showcase --json
node bin/archify.mjs deliver  workflow <repo>/docs/diagrams/market-size-forecasting-flow.workflow.json \
                                        <repo>/docs/diagrams/market-size-forecasting-flow.html --quality showcase --json
node bin/archify.mjs visual-check      <repo>/docs/diagrams/market-size-forecasting-flow.html --json
```

Node widths are pinned per diagram (220 / 175 / 145 / 145 / 140 / 145) and are
load-bearing. Width sets the viewBox aspect: too narrow and the panel grows
taller than a 1440x900 screen, too wide and the 8px context text projects below
the 6px legibility floor. Each value is the widest that still validates. Pass
`--repo-root <repo>` on validate and deliver so the source evidence is verified
against the pinned commit. Changing lanes, widths or labels means re-running
`visual-check`.
