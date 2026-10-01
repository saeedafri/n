# Market Size Forecasting — proof the modelling logic did not change

**Date:** 30 September 2026
**Question raised by:** the data science team
**Question:** the September 2026 work on file ingestion — did it change the
forecasting logic, the models, or the numbers for any date or dataset?

**Answer: no.** On every file that produced a forecast before, the cleaned
series, the selected order, the AIC, the walk-forward MAPE and every forecast
value are **bit-identical**. Evidence below, reproducible in one command.

---

## 1. Method

The engine as committed **before** the work (`267d4de`,
`app/data/market_size_forecast_service.py`, 1,167 lines, containing none of the
new helpers) is loaded side by side with the current engine. Both are given
byte-identical input. Everything the models produce is compared.

```bash
git show 267d4de:app/data/market_size_forecast_service.py > /tmp/msf-proof/engine_before.py
.venv/bin/python scripts/verify_forecast_unchanged.py
```

The script is committed at `scripts/verify_forecast_unchanged.py` so the data
science team can run it themselves.

## 2. Result

```
OLD 267d4de   NEW working tree

Monthly    rows  114  order (0, 1, 0)x(1, 1, 1, 12)
           input identical True | order True | AIC True (1805.529350) | MAPE True | forecast max|Δ| 0.00e+00  -> IDENTICAL
Quarterly  rows   80  order (2, 1, 2)x(0, 1, 0, 4)
           input identical True | order True | AIC True (1444.429782) | MAPE True | forecast max|Δ| 0.00e+00  -> IDENTICAL
Annual     rows   28  order (0, 1, 0)x(0, 0, 0, 0)
           input identical True | order True | AIC True (488.796883)  | MAPE True | forecast max|Δ| 0.00e+00  -> IDENTICAL
Weekly     rows  300  order (0, 1, 1)x(0, 1, 0, 52)
           input identical True | order True | AIC True (4948.901910) | MAPE True | forecast max|Δ| 0.00e+00  -> IDENTICAL

ALL CASES BIT-IDENTICAL: True
```

`forecast max|Δ| 0.00e+00` is the maximum absolute difference across every
forecast period. Not "close" — zero.

Separately, the validated monthly dataset still selects the pinned order
`(2,1,2)x(0,1,0,12)`.

## 3. What was changed

Everything sits **upstream of the models**, in reading the uploaded file.

| Change | What it does |
|---|---|
| `_parse_month_year` | reads `Dec '22`, `Dec’22`, `Dec' 22`, `Dec-22`, `2020M01` |
| `_short_year_is_plausible` | stops `Jan 01` being read as the year 2001 |
| `to_numeric_values` | reads `1,234`, `$1,234`, `12.5%`, `(87)`, `1 234` |
| period snap in `load_series` | puts a period label at the end of the period it names |
| `ensure_regular_index` | restores the index frequency statsmodels requires |

## 4. What was NOT changed

- No model added, removed or substituted
- No change to the SARIMA/SARIMAX order grid or its bounds
- No change to the AIC selection rule
- No change to the walk-forward validation window
- No change to the seasonal-term gate for weekly data
- No change to the 32 FRED variables, the Granger scan, or the combination search
- No change to any Prophet prior, the COVID regressor, or changepoint detection
- No change to residual diagnostics

## 5. Three places the OUTCOME differs — all error-to-result

None is a logic change. In each case the previous behaviour was a failure.

**5.1 Period-start date labels.** A file labelled `Jan 2020`, `2020-01`,
`Dec-2020` or `01/2020` parsed correctly and was then reindexed onto
period-**end** labels, matching nothing, so every value became blank and the
page reported *Only 0 usable rows after cleaning*. Dates are now placed at the
end of the period the label names.

Files already stamped at period end are unaffected and take a no-op path —
the diagnostic counter `snapped_to_period_end` reads `0` for them.

Both spellings of the same data now produce the same model and AIC.

**5.2 A gap in the series.** Dropping blank periods left an irregular index
with no frequency, and SARIMAX raised `No supported index is available`. Any
file with a hole hit this; the data team's test file has four, one February per
year. The series is now reindexed onto its full period range, leaving the gaps
as missing observations — which the Kalman filter already treats as missing.

**5.3 Prophet cross-validation is now on demand.** The computation and its
result are unchanged. It is simply no longer run on every analysis: it is 26
additional Stan fits feeding one card, and it cost more than the forecast
itself. The Prophet tab shows a **Run cross-validation** button, and the
"Regressor value" card reads *On request* until it is pressed.

**This is the only difference a data scientist will notice on a file that
already worked.** Same inputs, same method, same number — computed when asked
for rather than always.

## 6. Test coverage

`tests/test_market_size_forecast.py` — **87 tests**, no network, no database.
The new ones pin:

- all six monthly date spellings surviving cleaning end to end
- all four apostrophe styles, `Mon-YY`, `2020M01`
- `Jan 01…Dec 12` **not** being read as years 2001–2012
- a real short-year series still reading as years
- formatted numbers (`1,234`, `$1,234`, `12.5%`, `(87)`, non-breaking space)
- a gapped series not crashing SARIMA, and a gapless one being untouched
- a numeric column still being refused as a date column

## 7. How to re-verify at any time

```bash
cd <repo>
git show 267d4de:app/data/market_size_forecast_service.py > /tmp/msf-proof/engine_before.py
.venv/bin/python scripts/verify_forecast_unchanged.py
.venv/bin/python -m pytest tests/test_market_size_forecast.py -q
```

Expected: `ALL CASES BIT-IDENTICAL: True` and 87 passing tests. Anything else
is a regression and should block release.
