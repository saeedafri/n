# Market Size Forecasting — the five tabs, explained

After **Run analysis** the page shows five sections. Each has its own one-screen
diagram in this folder. This page says, in plain terms, what each tab means and
why it exists.

| Tab | The question it answers | Diagram |
|---|---|---|
| Diagnostics | What kind of series is this? | `market-size-forecasting-tab-1-diagnostics.html` |
| SARIMAX | Does US macro data explain it? | `market-size-forecasting-tab-2-sarimax.html` |
| SARIMA | What does the series say on its own? | `market-size-forecasting-tab-3-sarima.html` |
| Prophet | Where did the trend actually bend? | `market-size-forecasting-tab-4-prophet.html` |
| Comparison | Which number do I quote? | `market-size-forecasting-tab-5-comparison.html` |

Only the selected tab renders, which is why switching is instant. Downloads live
on the four model tabs — **Diagnostics has none**.

---

## 1 · Diagnostics — what the models are being asked to fit

**What it signifies.** The shape of your data before any model touches it. Eight
cards, a history chart and a seasonal decomposition.

| You see | It means |
|---|---|
| Records, Mean, Range | Size and spread of the cleaned series |
| Seasonal strength | How much of the movement is a repeating yearly cycle |
| ADF test / KPSS test | Whether the level and spread stay put over time |
| Peak / Trough month | Which period of the year is reliably highest and lowest |
| Outliers | Single periods beyond three standard deviations |
| Decomposition | The series split into trend, repeating season, and leftover |
| FRED pull status | How many of the 32 macro variables loaded, and why any failed |

**Why it is used.** Everything downstream is configured from here. Non-stationary
data has to be differenced, and this is where that is decided. The seasonal
period `s` comes from here and drives all three models. And an outlier is far
more often a data error than a real event — this is your last chance to catch it
before it distorts a forecast.

**Reading it honestly.**
- Both tests reporting *Non-stationary* is completely normal for sales data. It
  is not a failure; it tells the models to difference.
- ADF and KPSS are run together on purpose — they can disagree, and disagreement
  is itself informative.
- Peak and trough months should match what the business actually does. If they
  do not, suspect the date column.
- If outliers are listed, open those rows in your source file before trusting
  anything further down.

---

## 2 · SARIMAX — does US macro data explain this series?

**What it signifies.** Your series modelled together with outside economic
indicators, rather than in isolation.

| You see | It means |
|---|---|
| Granger-significant variables | Macro series whose *past* helps predict yours, with the lag and p-value |
| Combination search | Every driver combination tried, ranked by error on a held-out window |
| Combination rank box | Refit on any other ranked set to see how the forecast changes |
| Variables / Order / Real macro periods | The exact specification that produced this forecast |
| Walk-forward validation | Accuracy on recent periods the model never saw |
| Residual diagnostics | Whether structure is still left unexplained |

**Why it is used.** It is the only model here that can answer a scenario
question — *what if employment softens?* The driver list is frequently more
useful than the forecast itself, because it gives a reason for the number rather
than just a shape. Use it when the market plausibly moves with employment,
interest rates or consumer spending.

**Reading it honestly.**
- Granger significance is correlation *in time*. It is not proven causation.
- **Real macro periods** below your horizon matters: past that point the
  exogenous terms fall back to a neutral baseline, so the far end of the forecast
  is weaker than the near end.
- FRED publishes monthly. On a weekly run each macro value is forward-filled
  across the weeks, so the macro signal is still monthly in resolution.
- All 32 predictors are US series. For a UK or India market size this tab has no
  appropriate drivers — use SARIMA or Prophet.
- If SARIMAX does not beat SARIMA, the macro drivers added nothing. That is a
  real finding, not a broken run.

---

## 3 · SARIMA — what the series says on its own

**What it signifies.** The pattern the series can explain using only its own
history. No external data whatsoever.

The order is written `ARIMA(p,d,q)x(P,D,Q,s)`:

| Term | Plain meaning |
|---|---|
| `p` | How many past periods the model looks back at |
| `d` | How many times the series was differenced to stabilise it |
| `q` | How long a past shock keeps echoing |
| `P, D, Q` | The same three ideas, repeated once per season |
| `s` | The season length — 12 monthly, 4 quarterly, 52 weekly |

**Why it is used.** It has no external dependency, so it cannot break on a
missing API key. It is the fair baseline that SARIMAX has to beat to justify its
extra complexity. For a stable, strongly seasonal market it is often the most
reliable of the three.

**Reading it honestly.**
- The order is chosen automatically by grid search on AIC. There is nothing to
  tune by hand.
- AIC compares orders *within this tab only*. Never compare an AIC here against
  another model — use the Comparison tab's MAPE for that.
- Failing residual diagnostics means real structure is still unmodelled and the
  confidence band is too narrow.
- On weekly data with under 8 years of history, seasonal AR/MA terms are not
  searched — seasonal differencing still removes the annual pattern, and the fit
  is far quicker. The page tells you when this applies.

---

## 4 · Prophet — trend shifts and seasonality, no macro data

**What it signifies.** The curve split into a bending trend plus a repeating
seasonal shape, with one structural-break flag for COVID.

| You see | It means |
|---|---|
| Seasonality mode | Whether the seasonal swing is a fixed amount or a percentage |
| Changepoints | Dates where the underlying growth rate actually changed |
| Regressor value | How much the `covid_shock` flag improved accuracy, in points |
| Cross-validation | Rolling accuracy across several cut-points, with and without the flag |
| Walk-forward + residuals | The score that is comparable across models, plus Ljung-Box, Shapiro-Wilk, Durbin-Watson |
| Components | Trend and seasonality drawn separately |

**Why it is used.** It handles trend breaks that SARIMA and SARIMAX tend to
smooth over. It needs no stationarity and no order selection, so there is nothing
to tune. It copes with gaps and irregular history better than the ARIMA family.
And the changepoint dates are directly quotable in a written market narrative.

**Reading it honestly.**
- A *negative* regressor value means the COVID flag did not help on this series.
  That is a legitimate result, not an error.
- Cross-validation is skipped on short history. Walk-forward validation still
  runs, so the model is still scored.
- Many changepoints on a short series usually means it is chasing noise.
- No FRED data is used here, so this tab cannot answer scenario questions.

---

## 5 · Comparison — which number do you quote?

**What it signifies.** All three models judged on identical terms: the same
held-out periods and the same error measure.

| You see | It means |
|---|---|
| One card per model, one marked BEST | Walk-forward MAPE — average error as a percentage, lower is better |
| Summary table | MAPE, RMSE, accuracy, whether FRED was used, and the exact specification |
| Period-by-period table | The three forecasts side by side, exact figures |
| Agreement message | How far apart the models are across the horizon |
| Export all models | One CSV or Excel containing every model's forecast and bounds |

**Why it is used.** Three independent models landing in the same place is far
stronger evidence than one model alone. It is also the only tab that exports
everything in a single file, and the one to put in front of a client.

**Reading it honestly.**
- **Agreement within 5%** — the midpoint is a reasonable working forecast.
- **Divergence above 5%** — investigate the structural drivers before quoting
  anything. The page says which case you are in.
- Quote a **band across the horizon**, never a single number.
- BEST means lowest error on history. It does not guarantee best in future —
  read the spread alongside it.
- MAPE is comparable across these three models here *because* the window and
  measure are identical. That is the whole point of the tab.

---

## Which model should I actually use?

| Situation | Use |
|---|---|
| You need to answer "what if the economy does X?" | SARIMAX |
| Non-US market size | SARIMA or Prophet — the FRED drivers are US-only |
| Stable, strongly seasonal market | SARIMA |
| Obvious trend break (COVID, a policy change, a new entrant) | Prophet |
| You have to hand a number to a client | Comparison — quote the band, not one model |
