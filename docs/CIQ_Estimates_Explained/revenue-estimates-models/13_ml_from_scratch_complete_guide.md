# 13 Machine Learning From Scratch — Complete Guide for This Page

This document explains every algorithm on the Revenue Estimates page from first principles.
No prior machine learning, statistics, or Python experience assumed.

---

## Part 1. What "Machine Learning" actually means here

### The common misconception

Most people hear "machine learning" and picture:

- robots
- neural networks with millions of parameters
- models trained for weeks on GPUs

### What it means on this page

The models here are **classical statistical forecasting methods**.

They do not:
- use gradient descent
- use neural networks
- require GPU training
- take more than milliseconds to fit

They do:
- read a series of past revenue values
- compute one or a few parameters mathematically
- produce a projection of future revenue

**Every model on this page can be computed by hand with a calculator.**

---

## Part 2. The learning problem

### What we are trying to do

Given this:

| Year | Revenue (B) |
|------|------------|
| 2018 | 3.0 |
| 2019 | 3.5 |
| 2020 | 3.1 |
| 2021 | 4.0 |
| 2022 | 4.5 |
| 2023 | 5.0 |

Predict this:

| Year | Revenue (B) |
|------|------------|
| 2024 | ? |
| 2025 | ? |
| 2026 | ? |

Each model answers this question using a **different rule** based on what it observes in the history.

### Why different models give different answers

Each model makes a different **assumption** about how companies grow:

| Model | Core assumption |
|-------|----------------|
| Linear Regression | Revenue grows by the same dollar amount every year |
| CAGR | Revenue grows by the same percentage every year |
| Exp Smoothing | Recent years matter more; smooth out the noise |
| Holt's Linear | Both the level and speed of change update every year |
| MA Trend | The last 3 growth rates best predict the next year |
| Weighted Avg | All history matters, but recent history more so |

Same history → different assumptions → different forecast lines.

---

## Part 3. Linear Regression — the full derivation

### What we want

Find a straight line:

```
Revenue = intercept + slope × year
```

That line should be **as close as possible** to every historical data point.

### What "as close as possible" means mathematically

We measure closeness using **squared error**:

```
error_i = actual_i − (intercept + slope × year_i)
```

We square each error so that:
- positive and negative errors do not cancel
- large errors are penalized more than small ones

Then we sum all the squared errors:

```
SSE = Σ (actual_i − intercept − slope × year_i)²
```

**Objective:** find the slope and intercept that minimize SSE.

### Solving the minimization

This has a beautiful closed-form solution. We take the derivative of SSE with respect to slope and intercept, set each to zero, and solve:

**Slope:**
```
slope = Σ((year_i − mean_year) × (sales_i − mean_sales))
        ────────────────────────────────────────────────
        Σ((year_i − mean_year)²)
```

**Intercept:**
```
intercept = mean_sales − slope × mean_year
```

These two equations require exactly one pass through the data. No iteration.

### Worked example from scratch

Data:
| Year | Sales |
|------|-------|
| 2021 | 10 |
| 2022 | 12 |
| 2023 | 14 |
| 2024 | 16 |

Step 1 — compute means:
```
mean_year  = (2021 + 2022 + 2023 + 2024) / 4 = 2022.5
mean_sales = (10 + 12 + 14 + 16) / 4 = 13.0
```

Step 2 — compute numerator of slope:
```
(2021 - 2022.5)(10 - 13)  = (-1.5)(-3)  = 4.5
(2022 - 2022.5)(12 - 13)  = (-0.5)(-1)  = 0.5
(2023 - 2022.5)(14 - 13)  = (+0.5)(+1)  = 0.5
(2024 - 2022.5)(16 - 13)  = (+1.5)(+3)  = 4.5
Sum = 10.0
```

Step 3 — compute denominator of slope:
```
(-1.5)² = 2.25
(-0.5)² = 0.25
(+0.5)² = 0.25
(+1.5)² = 2.25
Sum = 5.0
```

Step 4 — slope:
```
slope = 10.0 / 5.0 = 2.0
```

This means: **revenue increases by $2B per year**.

Step 5 — intercept:
```
intercept = 13.0 − 2.0 × 2022.5 = 13.0 − 4045.0 = −4032.0
```

Step 6 — forecast 2025:
```
forecast_2025 = −4032 + 2.0 × 2025 = −4032 + 4050 = 18.0
```

This is exactly $18B, which matches the pattern perfectly (adding $2B each year from 16B).

### What R² means

R² measures how well the line explains the historical variation:

```
SS_total    = Σ(actual_i − mean_sales)²  ← total variation in the data
SS_residual = Σ(actual_i − fitted_i)²    ← variation not explained by the line
R²          = 1 − (SS_residual / SS_total)
```

- R² = 1.0 → the line passes through every point exactly
- R² = 0.5 → the line explains half the variation
- R² = 0.0 → the line is no better than just using the mean

For companies with very steady dollar-amount growth, R² is often > 0.95.

### The 90% prediction interval

The linear model also builds a band around the forecast to show uncertainty:

```
standard_error = residual standard error from the regression
band           = ±1.645 × se × √(1 + 1/n + (future_year − mean_year)² / Σ(year − mean_year)²)
```

1.645 comes from the 90th percentile of the standard normal distribution.
The band widens as `future_year` moves farther from `mean_year` — the model is less certain about years far into the future.

---

## Part 4. CAGR — the simplest compounding model

### The intuition

If a company grows from $100 to $162 over 5 years, and the growth was perfectly steady, what was the annual rate?

Answer: the rate `g` such that `100 × (1+g)^5 = 162`.
Solve: `g = (162/100)^(1/5) − 1 = 1.1^1 − 1 = 0.10 = 10%`.

That is CAGR.

### Formula

```
CAGR = (last_sales / base_sales)^(1 / n) − 1
```

Where:
- `last_sales` = latest annual revenue (after outlier removal)
- `base_sales` = earliest annual revenue
- `n` = number of year-to-year intervals (= count of years − 1)

### Forecasting

```
forecast_h = last_sales × (1 + CAGR)^h
```

h = 1 for next year, h = 2 for year after, etc.

### Why CAGR can be misleading

If the first year was unusually bad (COVID 2020) or unusually good (acquisition year), CAGR will be distorted because it only looks at the two endpoint values.

Example:
- 2019 = 100, 2020 = 70 (COVID), 2021 = 110, 2022 = 115, 2023 = 120

CAGR from 2019 to 2023 = (120/100)^(1/4) − 1 = 4.66%
But if you use 2020 as the base: (120/70)^(1/3) − 1 = 19.7%

Same company, wildly different CAGR depending on start year.

The model on this page uses the **first cleaned year** as base and the **last cleaned year** as the endpoint (after outlier removal).

---

## Part 5. Exponential Smoothing — smoothing out noise

### The problem it solves

Raw revenue can be noisy year to year. If we just use the latest growth rate, one bad or great year distorts everything.

Exponential Smoothing creates a **weighted average of all past values**, where weights decrease exponentially as you go back in time.

### The update equation

Starting with the first actual value:
```
L₁ = first actual sales
```

For every subsequent year t:
```
Lₜ = α × actualₜ + (1 − α) × Lₜ₋₁
```

With α = 0.3:
- 30% weight to the newest actual
- 70% weight to the previous smoothed level

After processing the whole series, the trend for projection:
```
trend = L_last − L_prev
```

Forecast:
```
forecast_h = L_last + h × trend
```

### Why the weights are "exponential"

When you expand the recursion, each older actual value gets weight:
```
L_n = α × s_n + α(1−α) × s_(n-1) + α(1−α)² × s_(n-2) + ...
```

The weights `α`, `α(1−α)`, `α(1−α)²`, ... form a geometric (exponential) series that always sums to 1.

With α = 0.3:
- Year n: weight = 0.30
- Year n-1: weight = 0.21
- Year n-2: weight = 0.147
- Year n-3: weight = 0.103
- ...

Older years get exponentially less weight.

### Full worked example (α = 0.3)

Data: 100, 140, 130, 160

L₁ = 100
L₂ = 0.3 × 140 + 0.7 × 100 = 42 + 70 = **112**
L₃ = 0.3 × 130 + 0.7 × 112 = 39 + 78.4 = **117.4**
L₄ = 0.3 × 160 + 0.7 × 117.4 = 48 + 82.18 = **130.18**

trend = 130.18 − 117.4 = **12.78**

forecast_h1 = 130.18 + 12.78 = **142.96**
forecast_h2 = 130.18 + 2×12.78 = **155.74**

Notice: the forecast is more conservative than the raw latest value (160), because the smoothed level (130.18) is pulled toward the history.

---

## Part 6. Holt's Linear Trend — tracking level and trend separately

### Why a second component helps

Exponential Smoothing uses one smoothed level and one fixed trend extracted at the end.

Holt's method improves this by **updating both level and trend every single year**.

If a company suddenly starts growing faster, Holt's trend term will gradually adapt to this. Exponential Smoothing will be slower to respond because it extracts trend only once at the end.

### Two equations running in parallel

**Level update** (α = 0.3):
```
levelₜ = α × actualₜ + (1−α) × (levelₜ₋₁ + trendₜ₋₁)
```

This says: the new level is 30% the newest actual, and 70% where we expected to be (old level + old trend).

**Trend update** (β = 0.1):
```
trendₜ = β × (levelₜ − levelₜ₋₁) + (1−β) × trendₜ₋₁
```

This says: the new trend is 10% the observed level change this year, and 90% the previous trend. The trend reacts **slowly** (β is small) so it doesn't overreact to one unusual year.

### Starting values

```
level₁ = first sales value
trend₁ = second sales − first sales
```

### Forecast

```
forecast_h = level_last + h × trend_last
```

### Full worked example (α=0.3, β=0.1)

Data: 2021→10, 2022→12, 2023→9, 2024→13

Start:
level₁ = 10, trend₁ = 12 − 10 = 2

Year 2 (actual = 12):
level₂ = 0.3×12 + 0.7×(10+2) = 3.6 + 8.4 = **12.0**
trend₂ = 0.1×(12−10) + 0.9×2 = 0.2 + 1.8 = **2.0**

Year 3 (actual = 9 — a dip year):
level₃ = 0.3×9 + 0.7×(12+2) = 2.7 + 9.8 = **12.5**
trend₃ = 0.1×(12.5−12) + 0.9×2 = 0.05 + 1.8 = **1.85**

Note: even though actual dropped to 9, the level barely moved (12.5) and trend only slightly adjusted (1.85).
This is how Holt's method handles one-year dips without panicking.

Year 4 (actual = 13 — recovery):
level₄ = 0.3×13 + 0.7×(12.5+1.85) = 3.9 + 10.045 = **13.945**
trend₄ = 0.1×(13.945−12.5) + 0.9×1.85 = 0.1445 + 1.665 = **1.81**

Forecast:
2025 = 13.945 + 1×1.81 = **15.755**
2026 = 13.945 + 2×1.81 = **17.565**

This is why Holt's model often wins for companies with bumpy-but-upward histories.

---

## Part 7. Moving Average Trend — the most transparent model

### The simplest to verify by hand

This model uses only the last 3 year-over-year growth rates and assumes they will continue.

### Step-by-step

Step 1 — compute all year-over-year growth rates:
```
growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁
```

Step 2 — take the last 3:
```
avg_growth = (growth_n + growth_(n-1) + growth_(n-2)) / 3
```

Step 3 — compound from the latest actual:
```
forecast_1 = last_sales × (1 + avg_growth)
forecast_2 = forecast_1 × (1 + avg_growth)
...
```

### Why it wins often for companies with strong recent momentum

If a company has been growing at 15%, 16%, 14% in the last 3 years, the model says: the best estimate for next year is ~15% growth. That is straightforward, transparent, and often surprisingly accurate when momentum is persistent.

### Real ANF example

ANF recent growth:
- 2024: +15.8%
- 2025: +15.6%
- 2026: +6.4%

avg_growth = (15.8 + 15.6 + 6.4) / 3 = **12.6%**

Latest actual = 5.266B

2027 forecast = 5.266 × 1.126 = **5.93B** → shown as **5.9B** on the page

You can verify this number yourself in under 30 seconds using the Data Prep tab.

---

## Part 8. Weighted Average Growth — full history with recency bias

### The difference from MA Trend

MA Trend uses only the last 3 growth rates (ignores older history entirely).
Weighted Average Growth uses **all** historical growth rates, but gives more weight to recent ones.

### How weights are assigned

If there are n growth rates (years 2 through n+1 of history), the weights are:
```
weight for oldest growth rate = 1
weight for second-oldest = 2
...
weight for newest growth rate = n
```

### Weighted average formula

```
weighted_growth = Σ(weight_i × growth_i) / Σ(weight_i)
```

The denominator `Σ(weight_i)` = 1 + 2 + 3 + ... + n = n×(n+1)/2.

### Why this matters

Suppose 5 years of growth rates: 3%, 5%, 7%, 10%, 15%

Simple average = (3+5+7+10+15)/5 = **8.0%**
Weighted average (weights 1,2,3,4,5):
= (1×3 + 2×5 + 3×7 + 4×10 + 5×15) / (1+2+3+4+5)
= (3 + 10 + 21 + 40 + 75) / 15
= 149 / 15
= **9.93%**

The weighted average is pulled toward the more recent higher growth rates.

### Forecasting

```
forecast_h = last_sales × (1 + weighted_growth)^h
```

Same compounding formula as CAGR and MA Trend.

---

## Part 9. Ensemble — blending the best models

### Why not just use the winner?

The model with the lowest backtest MAPE on the last 2 years might have been lucky — those 2 years may have happened to suit its assumptions.

By averaging the top 3 models, we:
- reduce dependence on any single model being right
- average out individual biases
- typically get lower variance in the forecast

### How it works

```
top3 = sort all models by backtest MAPE ascending → take first 3
ensemble_h = (model_a_h + model_b_h + model_c_h) / 3
```

### Worked example (ANF)

Top 3 backtest scores:
- MA Trend: MAPE 2.25%
- Weighted Avg: MAPE 12.87%
- CAGR: MAPE 13.07%

Their 2027 forecasts:
- MA Trend = 5.9B
- Weighted Avg = 5.5B
- CAGR = 5.4B

Ensemble 2027 = (5.9 + 5.5 + 5.4) / 3 = **5.6B**

This matches the page exactly. The ensemble pulled the forecast slightly below MA Trend because the other two models were more conservative.

---

## Part 10. Scenarios — percentile-based planning paths

### What scenarios are NOT

- Not a model
- Not a trained output
- Not a forecast in the same sense as the six models above

### What scenarios ARE

Planning paths derived from the historical growth distribution.

### How they are built

Step 1 — collect all historical year-over-year growth rates:
```
growth_2 = (sales_2 − sales_1) / sales_1
growth_3 = (sales_3 − sales_2) / sales_2
...
```

Step 2 — sort the growth rates and take percentiles:
```
g_pessimistic = 25th percentile of all growth rates
g_baseline    = 50th percentile (median)
g_optimistic  = 75th percentile
```

Step 3 — compound from the latest actual:
```
scenario_h = last_sales × (1 + g)^h
```

### What percentile means

If you have 8 historical growth rates: -5%, 2%, 4%, 6%, 8%, 10%, 15%, 20%

Sorted: -5%, 2%, 4%, 6%, 8%, 10%, 15%, 20%

25th percentile ≈ 2.75% (lower end of history)
50th percentile ≈ 7.0% (middle of history)
75th percentile ≈ 11.25% (upper end of history)

So pessimistic assumes the company grows at its historically weaker pace, and optimistic assumes it grows at its historically stronger pace.

---

## Part 11. Backtesting — measuring model accuracy honestly

### Why backtesting exists

It is tempting to run each model on all history and see which fits best. But this is misleading — any model can overfit its own training data.

Backtesting simulates what would have happened **if you had run the model 2 years ago** and then measured how close the 2-year-ahead predictions were to reality.

### The holdout procedure

Default: 2 holdout years.
If total history ≤ 4 years: fallback to 1 holdout year.

```
train = all years except the last 2
test  = last 2 years
```

Run each model on `train`, predict `test`, compare predictions to actual `test` values.

### MAPE — the primary ranking metric

```
MAPE = mean(|predicted − actual| / actual) × 100
```

Example: if predicted [5.0, 5.2] vs actual [4.9, 5.1]:
- Error 1: |5.0 − 4.9| / 4.9 = 0.0204 = 2.04%
- Error 2: |5.2 − 5.1| / 5.1 = 0.0196 = 1.96%
- MAPE = (2.04 + 1.96) / 2 = **2.0%** — excellent.

### Bias — systematic over or under prediction

```
Bias = mean(predicted − actual)
```

- Bias = +1.2B → model over-predicted by $1.2B on average
- Bias = −0.5B → model under-predicted by $0.5B on average

Bias tells you the **direction** of the error. A model with MAPE 10% and Bias +2B is consistently too optimistic. A model with MAPE 10% and Bias near 0 is just noisy — it goes both ways.

### RMSE — magnitude of error with extra penalty for large misses

```
RMSE = sqrt(mean((predicted − actual)²))
```

The squaring means a miss of 2B is penalized 4× more than a miss of 1B. This is useful when you care more about avoiding catastrophically wrong forecasts.

---

## Part 12. Score interpretation guide

### MAPE thresholds for annual revenue forecasting

| MAPE | Grade | What it means |
|------|-------|---------------|
| < 3% | Exceptional | Model closely tracked both holdout years |
| 3–7% | Excellent | Strong performer for annual data |
| 7–12% | Good | Solid but not perfect |
| 12–20% | Acceptable | Some error, still useful in an ensemble |
| 20–30% | Weak | Significant miss; may dominate in wrong direction |
| > 30% | Poor | Model should be interpreted with caution |

### Why MAPE can look high for good forecasts

- Annual revenue has only 2 holdout years by default. One unusual holdout year (COVID rebound, acquisition) inflates MAPE for all models.
- Check Bias: if all models have similar MAPE and similar negative Bias, the holdout period was probably unusually strong.

### How to read the ensemble given these scores

If the top 3 models have MAPE of 2%, 13%, 13% — the ensemble is dominated by the top model. The other two don't move it much.
If the top 3 models have MAPE of 4%, 5%, 6% — all three are close in quality, and the ensemble benefits most from diversification.

---

## Part 13. Worked verification — reproduce the page from scratch

### For any company on the page, here is what to check

**Verify CAGR:**
1. Open Data Prep tab → Clean training series
2. Note first revenue and last revenue
3. Count the number of years − 1 to get n
4. Compute: (last / first)^(1/n) − 1
5. Multiply latest revenue by (1 + CAGR) for the 1-year forecast
6. Compare to CAGR column in the forecast table

**Verify MA Trend:**
1. Open Data Prep tab → Clean training series
2. Note the last 3 Growth % values
3. Average them
4. Multiply latest revenue by (1 + avg_growth_rate)
5. Compare to Moving Average Trend column in the forecast table

**Verify ensemble:**
1. Open Backtest tab
2. Note the top 3 models by Rank
3. Find their Year 1 forecast values in the forecast table
4. Average those 3 values
5. Compare to Ensemble column in the forecast table

**Verify Scenario Pessimistic:**
1. Open Data Prep tab → Clean training series
2. Collect all Growth % values
3. Sort them lowest to highest
4. Take the 25th percentile value (lower-quarter growth rate)
5. Multiply latest revenue by (1 + g_25th)
6. Compare to Pessimistic Scenario column in the forecast table
