# 10 Backtesting, Ensemble, and Scenarios

## Overview

This file explains three mechanisms that appear on the Estimates page after the individual models run:

1. **Backtesting** — how the system measures each model's historical accuracy
2. **Ensemble** — how the best models are blended into a single forecast
3. **Scenarios** — how pessimistic/baseline/optimistic paths are constructed

---

# Part 1: Backtesting

## What backtesting is

Backtesting is **holdout validation**: hide some known historical data from the model, let the model forecast that hidden period, then measure how wrong it was.

The principle: if a model would have predicted past real values accurately, we have more confidence it will predict future values accurately.

This is the only objective, data-driven way to compare models on a per-company basis. Without backtesting, you can only compare models based on assumptions — backtesting compares them based on actual forecasting performance.

---

## Holdout rule — exact code logic

```
Default holdout: 2 years

Short-series fallback:
  if (total cleaned years) <= (holdout + 2):
      holdout = 1
```

**Why the fallback?** To run a model we need at least 3 data points for training (so parameters like slope, CAGR, smoothed level can be estimated). If we hold out 2 years and have only 5 total, the training set has 3 years — borderline. If we hold out 2 years and have only 4 total, training has only 2 years — too few. The fallback triggers when total_years ≤ 4 (holdout=2 case).

**Exact Python:**
```python
holdout = 2
if len(clean_years) <= holdout + 2:
    holdout = 1
train_years  = clean_years[:-holdout]
train_sales  = clean_sales[:-holdout]
test_years   = clean_years[-holdout:]
test_sales   = clean_sales[-holdout:]
```

**Example — 6-year history (2020–2025), holdout=2:**
```
Train: 2020, 2021, 2022, 2023
Test:  2024, 2025  (hidden from models)
```

Each model is trained on 2020–2023, then asked to forecast 2024 and 2025. The forecasts are compared against the real 2024 and 2025 values.

---

## The three error metrics

### MAPE — Mean Absolute Percentage Error

```
MAPE = (1/k) × Σ |predictedₜ − actualₜ| / actualₜ × 100

where k = number of holdout years
```

| Property | Value |
|----------|-------|
| Unit | % (dimensionless) |
| Range | 0% to ∞ |
| Lower is | Better |
| Scale-free? | Yes — comparable across companies of different sizes |
| Used for model selection? | **Yes — this is the primary metric** |

**MAPE asymmetry limitation:** MAPE penalizes over-predictions and under-predictions differently on a relative scale.

- If actual = 10 and predicted = 15: MAPE contribution = |15−10|/10 = 50%
- If actual = 10 and predicted = 5:  MAPE contribution = |5−10|/10  = 50%

So 50% too high and 50% too low seem equal. But in compounding terms, +50% and −50% are not symmetric (one gives 1.5×, the other 0.5×). MAPE does not capture this asymmetry.

**Also:** MAPE is undefined when actual = 0 (division by zero). For revenue data this is rare but the outlier removal step helps prevent zero-revenue years from reaching this computation.

---

### Bias — Directional Error

```
Bias = (1/k) × Σ (predictedₜ − actualₜ)
```

| Bias Sign | Meaning |
|-----------|---------|
| Positive | Model systematically **over-predicts** (optimistic bias) |
| Negative | Model systematically **under-predicts** (conservative bias) |
| Zero | On average, model is accurate; errors cancel out |

Bias is not used for model selection (MAPE is), but it is shown in the Backtest tab so you can understand the direction of each model's error tendency.

**Why bias matters for a demo:** A model with low MAPE but high positive bias might be selected as "best" while consistently over-estimating. In a planning context, biased models can systematically mislead revenue targets.

---

### RMSE — Root Mean Squared Error

```
RMSE = √[(1/k) × Σ (predictedₜ − actualₜ)²]
```

| Property | Value |
|----------|-------|
| Unit | Same as revenue (e.g., $B) |
| Lower is | Better |
| Squaring effect | Larger errors are penalized disproportionately (quadratically) |
| vs MAPE | Not scale-free — only comparable across same company |

**Bias-Variance decomposition of MSE:**

MSE (Mean Squared Error = RMSE²) decomposes as:

```
MSE = Bias² + Variance

where:
  Bias²    = (mean predicted − mean actual)²    → systematic error
  Variance = variance of (predicted − actual)    → random/noise error
```

A model with low Bias² but high Variance: accurate on average but inconsistent (noisy).
A model with high Bias² but low Variance: consistently wrong in one direction.
The best model minimizes both simultaneously.

This decomposition is why both MAPE and Bias are displayed — they give you different views of the same error.

---

## Model selection: DataFrame.nsmallest(3, "mape")

```python
backtest_df.nsmallest(3, "mape")
```

This selects the 3 rows with the smallest MAPE values from the backtest results DataFrame. `nsmallest` uses a partial sort (O(n log k)) rather than a full sort (O(n log n)) — more efficient for large DataFrames, though irrelevant for 6 rows.

**Tie-breaking:** If two models have equal MAPE (rare with floating-point values), `nsmallest` preserves the original DataFrame row order (stable sort behavior inherited from pandas). In practice, float MAPE values are never exactly equal.

**Best single model:** `backtest_df.nsmallest(1, "mape").iloc[0]["method"]` — the model with the absolute lowest MAPE, shown as "Best model" in the UI.

---

## Holdout validation vs walk-forward validation

Our implementation uses **simple holdout**: train once on years 1 to n−2, test on years n−1 and n.

An alternative is **walk-forward validation** (also called time-series cross-validation):
- Fold 1: train on years 1..k, test on year k+1
- Fold 2: train on years 1..k+1, test on year k+2
- ... repeat for each fold
- Average all fold errors

Walk-forward gives a more robust MAPE estimate but requires running each model T times (where T = number of folds). For annual data with 10–20 data points, simple holdout is standard — the computational savings are not the reason, the data scarcity is. Walk-forward would often leave training sets of 3–4 years which are too short for reliable parameter estimation.

---

# Part 2: Ensemble

## What ensemble means in this system

"Ensemble" in machine learning broadly means combining multiple models. In this system it has a specific, simple definition:

> **Ensemble = simple arithmetic mean of the top 3 models by backtest MAPE for each forecast year**

It is **not**:
- A stacked model (no meta-learner)
- A weighted average by MAPE score
- A Bayesian model average
- A boosting or bagging scheme

It is: unweighted arithmetic mean of three model outputs, applied year by year.

---

## Exact computation

```python
top3_methods = backtest_df.nsmallest(3, "mape")["method"].tolist()
top3_forecasts = forecast_df[top3_methods]           # DataFrame: rows=years, cols=3 methods
forecast_df["ensemble"] = top3_forecasts.mean(axis=1)   # axis=1 = row-wise mean
```

**For each forecast year:**
```
ensemble_h = (model_A_h + model_B_h + model_C_h) / 3
```

---

## Why top 3 and not all 6?

**Bates & Granger (1969)** — the foundational paper on forecast combination — proved that combining forecasts reduces variance if the individual forecasts have uncorrelated errors.

The variance reduction from combining k independent forecasts with variance σ²:
```
Var(ensemble) = σ²/k
```

But this only holds when forecasts are **diverse** (errors are uncorrelated). If all 6 models produce similar errors (which can happen when they all have the same directional bias), combining all 6 doesn't help much and the three worst models drag down the quality.

By including only the top 3 by MAPE:
1. We exclude models that performed poorly on this specific company's history
2. The remaining 3 have a better chance of being diverse (different methodologies)
3. The ensemble inherits the strengths of models that have already been empirically validated on this company's data

**Example — ANF:** If Linear Regression has MAPE=24% and MA Trend has MAPE=2%, including Linear Regression in the blend would pull the ensemble away from the best forecast direction. Excluding it gives a cleaner signal.

---

## Real page example: ANF

| Model | MAPE | In Ensemble? |
|-------|------|-------------|
| MA Trend | 2.25% | ✓ |
| Weighted Avg | 12.87% | ✓ |
| CAGR | 13.07% | ✓ |
| Holt's Linear | 16.96% | ✗ |
| Exp Smoothing | 19.59% | ✗ |
| Linear Regression | 24.42% | ✗ |

ANF 2027 ensemble:
```
(5.9 + 5.5 + 5.4) / 3 = 16.8 / 3 = 5.6B
```

Page shows **5.6B** ✓

---

# Part 3: Scenarios

## What scenarios are

Scenarios are not model forecasts — they are **distributional bounds** derived from the company's own historical growth rates. They answer: "If the company's future growth matches a pessimistic, median, or optimistic draw from its own historical growth distribution, what would revenue look like?"

---

## Exact computation

**Step 1 — Compute all historical year-over-year growth rates:**
```
gₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁       for t = 2, ..., n
```

**Step 2 — Compute percentiles:**
```python
growth_p25 = np.percentile(growth_rates, 25)   # pessimistic
growth_p50 = np.percentile(growth_rates, 50)   # baseline
growth_p75 = np.percentile(growth_rates, 75)   # optimistic
```

**Step 3 — Compound each growth rate forward from last actual:**
```
scenario_h = salesₙ × (1 + g_pX)^h       for h = 1, 2, 3, 4, 5
```

---

## numpy.percentile — internal interpolation

`np.percentile(a, q)` uses **linear interpolation** between the two nearest data points when the percentile falls between two values (the default method='linear').

**Example:** 5 growth rates: [2%, 5%, 8%, 12%, 18%]

For P25:
- Sorted: [2, 5, 8, 12, 18]
- Index position for P25: 0.25 × (5−1) = 1.0 → exactly index 1 → 5%

For P75:
- Index position for P75: 0.75 × (5−1) = 3.0 → exactly index 3 → 12%

For a non-integer position (e.g., P40 on 5 values):
- Index = 0.40 × 4 = 1.6 → interpolate between index 1 (5%) and index 2 (8%): 5 + 0.6 × (8−5) = 6.8%

This linear interpolation (numpy's default) is the same as "Type 7" in R's `quantile()` function.

---

## Why P25, P50, P75 and not P10, P50, P90?

The choice is a deliberate design decision:

| Choice | Width | Philosophy |
|--------|-------|-----------|
| P10/P50/P90 | Wider band | Captures tail scenarios — appropriate for risk management, stress testing |
| P25/P50/P75 | Narrower band (interquartile range) | Represents the "central 50% of historical behavior" — appropriate for planning |

For a revenue forecasting tool aimed at analyst planning (not risk management), P25/P75 gives a realistic spread. P10/P90 would give scenarios so extreme they are less actionable for annual planning purposes.

Also, with short annual histories (5–15 data points), P10 and P90 are often exactly the minimum and maximum values — unstable and overly sensitive to single outlier years.

---

## What scenarios are NOT

- **Not model-based**: scenarios don't use any of the 6 models — they come purely from the company's own growth rate distribution
- **Not confidence intervals**: a confidence interval is derived from model uncertainty; scenarios are from empirical historical growth
- **Not guarantees**: they show what revenue would look like if future growth matches historical growth patterns

---

## Full flow chart (annotated)

```
Historical revenue (cleaned, outliers removed)
        │
        ├──▶ Year-over-year growth rates (n−1 values)
        │           │
        │           ├──▶ [Scenarios] P25/P50/P75 of growth rates
        │           │           → compound each forward → 3 scenario lines
        │           │
        │           └──▶ [Weighted Avg, MA Trend] use directly as inputs to models
        │
        ├──▶ Split into train / holdout
        │           │
        │           └──▶ 6 models trained on train set
        │                       │
        │                       └──▶ each model forecasts holdout years
        │                                   │
        │                                   └──▶ MAPE, Bias, RMSE computed per model
        │                                               │
        │                                               ├──▶ best model = min(MAPE)
        │                                               └──▶ top 3 by MAPE → ensemble
        │
        └──▶ 6 models re-trained on full history
                    │
                    └──▶ 5-year forecasts produced
                                │
                                ├──▶ Ensemble = mean(top 3 forecasts, per year)
                                └──▶ UI displays all 6 + ensemble + 3 scenarios
```

---

## Error metric formulas — complete reference

| Metric | Formula | Unit | Selection? |
|--------|---------|------|-----------|
| MAPE | `mean(|pred−actual|/actual) × 100` | % | Yes — primary |
| Bias | `mean(pred−actual)` | $B | No — diagnostic |
| RMSE | `√(mean((pred−actual)²))` | $B | No — diagnostic |
| MSE decomp. | `MSE = Bias² + Variance` | $B² | Theory |

---

## Common questions at technical demos

**Q: Why not optimize ensemble weights by MAPE?**
A: With only 2 holdout years, weight optimization would overfit. Simple equal-weight averaging is more robust on small holdout samples (Bates & Granger showed equal weighting is often hard to beat in practice).

**Q: Why not walk-forward cross-validation?**
A: Annual revenue series are short (typically 5–20 points). Walk-forward would leave training sets of 3–5 years in early folds — too small for reliable model fitting. Simple holdout on 2 years is standard for annual financial data.

**Q: Is the backtest rerun every time the page loads?**
A: No. Results are cached via `@st.cache_data` with a 900-second TTL per company. Backtesting runs once and is served from memory for 15 minutes.

**Q: What if all 6 models have the same MAPE?**
A: Mathematically impossible with floating-point output unless two models are identical (would require identical parameters). In practice MAPE values always differ.

**Q: Could the ensemble be worse than the best single model?**
A: Yes, in theory. If the 2nd and 3rd models both have large errors in the same direction as the 1st, the ensemble inherits that directional error. This is why we use top-3 rather than all-6 — reducing the probability of including systematically biased models.

## What to say when presenting

> "Backtesting hides the last 2 years from each model, forces each model to forecast those hidden years, then measures the percentage miss using MAPE. The 3 models with the lowest MAPE are averaged equally to form the ensemble — this is the Bates-Granger forecast combination approach, which reduces variance through diversification. Scenarios are separate: they take the company's own historical growth rate distribution and compound the 25th, 50th, and 75th percentile rates forward as three planning paths."
