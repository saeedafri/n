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
| `Previous month` | the value one period earlier |
| `MoM change` | `Sales − Previous month` |
| `MoM growth %` | that change over the earlier period |
| `Same month last year` | the value one year earlier |
| `YoY change` | `Sales − Same month last year` |
| `YoY growth %` | that change over the year-earlier period |

The column names follow the data's frequency: `WoW` / `MoM` / `QoQ`, and
`Previous week` / `Previous quarter` / `Previous year`. Annual data gets one
growth block only — a year against the previous year is already
year-on-year — so it has no separate `Same year last year` column.

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
separators, signed percentages, negatives in red) and right-aligned through the
column config; the download keeps the raw numbers.

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

## Not done

* No chart. The table and the download were what was asked for.
* The assistant's context does not mention the growth table; it already has
  every underlying number.
