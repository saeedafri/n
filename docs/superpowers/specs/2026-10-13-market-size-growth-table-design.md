# Market Size Forecasting — period-on-period growth table

**Date:** 2026-10-13
**Page:** `/market_size_forecasting` → **Growth** section
**Status:** built, verified in the browser against 11 ingestion fixtures

---

## What was asked for

A table in the forecasting results showing year-over-year growth, period-wise
sales, period-over-period growth, the prior-period and prior-year sales
themselves, and the percentage change — positive or negative — with a download.

## What it does

A new **Growth** section sits between Diagnostics and the model sections. It
shows one row per period:

| Column | Meaning |
|---|---|
| `Period` | `Jan 2024`, `Q1 2024`, `2024`, or a week's end date |
| `Type` | `Actual` for uploaded history, `Forecast` for model periods |
| `Sales` | that period's value |
| `MoM%` | growth on the period before |
| `YoY%` | growth on the same period a year earlier |

The period-on-period name follows the data's frequency: `WoW%` / `MoM%` /
`QoQ%`. Annual data gets one growth block only — a year against the previous
year is already year-on-year — so it has `YoY%` and nothing else.

Three controls:

* **Continue the table with** — `History only`, or any model that ran. The
  chosen model's central forecast is appended as `Forecast` rows, so the first
  forecast period is measured against the last actual one instead of
  restarting. Defaults to the best model by the page's own ranking metric.
* **Newest first** — reading order; the download follows it.
* **Download CSV / Download Excel** — the unformatted table, through the shared
  Blob downloader (`render_download_buttons`), same path as every other export
  on this page.

Three cards above the table carry the latest actual period, its period-on-period
growth and its year-on-year growth.

## Where the code lives

`app/data/market_size_forecast_service.py`

```
PERIOD_ON_PERIOD      {'Weekly': 'WoW', 'Monthly': 'MoM', ...}
PERIOD_LABEL_FORMAT   strftime per frequency
period_labels()       readable period names, quarters built by hand
_growth_percent()     change over the absolute base, blank on a zero base
growth_table()        history (+ optional forecast) -> the frame above
```

`app/pages/market_size_forecasting.py`

```
_render_growth_tab()  controls, cards, formatted grid, caption, downloads
labels / renderers    "Growth" registered in main()
```

## Decisions worth recording

**The shifts are positional, on the regular grid.** `load_series` reindexes
onto the frequency (`asfreq`), so every period has a row even when the upload
skipped it. `shift(1)` and `shift(s)` therefore mean "one period back" and "one
year back" literally, and a gap produces a blank rather than silently comparing
against the wrong period. The table is built from `loaded.frame`, not from the
`dropna()`'d modelling series, for exactly this reason.

**Percentages divide by the absolute base.** `(value − base) / |base| × 100`.
For the positive data this page normally sees this is an ordinary percent
change; where a series crosses zero it keeps the direction of the move honest
(a loss of 200 improving to 100 reads `+50%`, not `−50%`). A zero base is left
blank — nothing grows from zero — rather than printing `inf`, which is the same
rule `_percent` already applies to MAPE.

**The engine is untouched.** `growth_table` is a new, additive function; no
model, diagnostic or ingestion path was modified, so the data team's numbers
are unaffected. Everything in the table is arithmetic on values the page
already displays.

**Figures are drawn as text.** Streamlit's grid prints a blank numeric cell as
the word `None`. On a growth table the first row, and every row after a gap,
genuinely has nothing to compare against, so a column of `None`s reads as a
fault. The displayed copy is formatted to strings (`—` for blank, thousands
separators, percentages to two places, negatives in red) and right-aligned
through the column config; the download keeps the raw numbers.

**Only the growth, not its arithmetic.** The first cut also carried the base
values and the absolute changes — `Previous month`, `MoM change`,
`Same month last year`, `YoY change`. Nine columns to say what three say: the
base of every comparison is already a row of the same table, and the change in
currency is `Sales` minus that row. Dropped on 2026-10-14 against the format
the business team sent back (`Year · Sales · YoY%`), which is now the shape of
both the grid and the download.

## Verification

Unit — `tests/test_market_size_forecast.py`, 13 new tests (135 total, all pass):
known MoM/YoY arithmetic, the empty first row, forecast rows continuing from the
last actual, an overlapping forecast not duplicating a period, growth from zero,
a blank period blanking its own and the next row's growth, a negative series
keeping its direction, period naming and growth columns per frequency, annual
collapsing to one block, the YoY shift following the frequency, and a CSV
round-trip.

Browser — `.claude/dev/msf_growth_check.py` drives the real page: uploads a
workbook, runs the analysis, opens Growth, walks every picker option, clicks
both download buttons and then checks the saved file. The history half is
compared row for row against the engine reading the same workbook; every growth
column is recomputed from the file's own columns; the workbook is compared with
the CSV; the Newest first toggle is proved to be an exact reversal.

Clean on 11 fixtures from `scripts/build_ingestion_fixtures.py`: month-end,
CSV, formatted numbers, no column-name hints, accounting negatives, missing
periods, duplicate dates, quarterly, weekly, annual and year-only.

## UI clean-up, 2026-10-14

Raised after the first review of the Growth section; applies to the whole page.

* **Card spacing.** `.msf-card` had no bottom margin, and Streamlit stacks
  consecutive `st.columns` rows with no gap — so the second row of KPI cards,
  and whatever followed the cards (the outlier banner, a chart, a table), sat
  flush against them. One rule, `margin-bottom: 16px`, fixes every section:
  Diagnostics, Growth, SARIMAX, SARIMA, Prophet and Comparison all measure a
  16px gap below every card now.
* **Timing box removed.** The `Timing — analysis …ms` expander at the foot of
  the page is gone. The measurements are still taken and still logged as
  `MSF_RUN_ANALYSIS` in the server log, so `/logs` keeps the per-stage numbers.
* **FRED data-pull box only on failure.** A "32/32 variables loaded" expander
  under the decomposition chart is noise; it now renders only when a variable
  actually failed, and names those variables and reasons.
* **MoM / YoY spelled out.** The Growth heading, both KPI cards and the caption
  carry the full form beside the abbreviation ("Month-over-month (MoM)"). The
  table columns stay short so the table and its download stay narrow.

Verified with `.claude/dev/msf_ui_spacing_check.py`, which measures the real
pixel gap below every card in the browser, asserts both removed boxes are
absent from every section, and confirms CSV **and** Excel are offered wherever
a table is exportable. Clean on the dev tree and on the `market-data-stg`
checkout. The card-rise animation moves a card up to 10px while it plays, so
the check waits for `document.getAnimations()` to settle before measuring.

## Annual roll-up, 2026-10-14

The data team sent `Growth.xlsx`: monthly history, then monthly projections,
then the two summed into calendar years with the rate taken on the yearly
totals. "The analysts usually require Annual sales and Annual growth rates."

So a second table sits under the period table whenever the data is sub-annual
— `Year · Sales · YoY%`, the three columns their sheet has and no more — built
by `annual_rollup()` beside
`growth_table()` in the service. It follows the same model picker and the same
Newest first toggle, and has CSV and Excel of its own
(`market_size_growth_<model>_annual`). Annual uploads do not get it: the table
above already is it.

Decisions:

* **It is a sum, not a resample.** Calendar years over the combined
  history-plus-forecast series, `YoY% = (total − previous total) / previous
  total × 100` — the sheet's arithmetic, reproduced to the decimal.
* **A year the data does not cover in full carries no rate.** Their sheet runs
  Jan–Dec throughout, so it never meets the case; in the portal it is the
  normal one — an upload ending in June, a two-month horizon, a month blank in
  the middle. Eight months against twelve reads as a 33% collapse that never
  happened. Such a year still shows its total, and its rate — and the next
  year's — is blank. Likewise across a year the upload skips entirely. The
  caption under the table says so; the blank itself marks which years.
* **Three columns, no fourth.** A first cut carried `Type` — `Actual`,
  `Forecast`, `Actual + forecast`, and how much of a short year was there.
  Asked for on 2026-10-15 in the shape the analysts work in: "just three
  columns: year, sales, and year-over-year. Just three." The period table above
  still names every row `Actual` or `Forecast`, which is where the mix is
  readable.

Verified against `Growth.xlsx` itself: the engine reproduces all thirteen of
the analysts' years, sales and rate, with zero mismatches, and the portal
driven through the browser reproduces 2018–2025 from the same history
(`market_size_growth_prophet_annual.csv`). `.claude/dev/msf_growth_check.py`
rebuilds the roll-up from the period table's own download — sums per year,
rate on the totals, blank where a year is short — for every model, on monthly,
quarterly and weekly fixtures.

## Not done

* No chart. The table and the download were what was asked for.
* The assistant's context does not mention the growth table; it already has
  every underlying number.
