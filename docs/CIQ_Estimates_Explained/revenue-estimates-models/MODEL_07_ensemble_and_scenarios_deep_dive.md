# Ensemble and Scenarios — Complete Deep Dive

## What this file covers

Two post-processing techniques that appear on the page alongside the six individual models:
1. **Ensemble** — blending the top 3 models by backtest MAPE
2. **Scenarios** — percentile-based planning paths (Pessimistic, Baseline, Optimistic)

Both are computed from the outputs of the six models and the historical growth distribution. Neither is a separately trained model.

---

# PART 1: ENSEMBLE

## 1. What the ensemble is

The ensemble is not a new forecasting algorithm.

It is the **arithmetic average of the top 3 models as ranked by backtesting MAPE**.

```
ensemble_h = (model_a_h + model_b_h + model_c_h) / 3
```

For each future year h, you take the forecast from the three best-performing models and average them.

---

## 2. Why average instead of just using the winner

### The problem with the "best model" alone

The model with the lowest MAPE on the holdout years might have been accurate for the wrong reasons.

Example:
- The last 2 holdout years happened to be perfectly suited for MA Trend (strong, consistent momentum)
- MA Trend wins the backtest
- But in the next 2 years, growth decelerates
- MA Trend now over-predicts

By blending the top 3, you reduce your dependence on MA Trend being right in the future. The other two models (which might be more conservative) partially offset MA Trend's optimism.

### The academic basis for ensembling

In 2001, a famous machine learning competition (Netflix Prize) and many academic forecasting competitions established that:

**Ensembles of diverse models consistently outperform the single best model on out-of-sample data.**

Why? Because models make different types of errors. If model A over-predicts when there's a dip and model B under-predicts when there's a dip, their average is closer to the truth than either one alone.

This is called **variance reduction through diversification** — the same principle behind investment portfolio diversification.

### Why top 3 specifically?

The number 3 is a practical choice:
- 1 model = no diversification
- 2 models = meaningful improvement but limited
- 3 models = significant variance reduction
- 4+ models = diminishing returns, and you risk including weak models

Many forecasting benchmarks use 3–5 models for ensemble blending. 3 is the standard for parsimony.

---

## 3. How the top 3 are selected

After backtesting, all 6 models have a MAPE score:

```
best_n = sort all models by MAPE ascending → take first 3
```

If MAPE results are:
| Model | MAPE |
|-------|------|
| MA Trend | 2.25 |
| Weighted Avg | 12.87 |
| CAGR | 13.07 |
| Holt's Linear | 16.96 |
| Exp Smoothing | 19.59 |
| Linear Regression | 24.42 |

Top 3: MA Trend, Weighted Avg, CAGR.

---

## 4. Worked example — ANF

Top 3: MA Trend (5.9B), Weighted Avg (5.5B), CAGR (5.4B) for 2027.

```
ensemble_2027 = (5.9 + 5.5 + 5.4) / 3 = 16.8 / 3 = 5.6B
```

The page shows **5.6B** for ANF 2027 Ensemble. ✓

The ensemble pulled the forecast slightly below MA Trend (the most optimistic of the three) because the other two models were more conservative.

---

## 5. Default ensemble if backtesting cannot run

If there is not enough history to backtest (very short series), the code defaults to:

```python
top3 = ['holt', 'cagr', 'weighted_avg']
```

These three are chosen as reasonable defaults because:
- Holt's handles trends well
- CAGR provides a long-run growth anchor
- Weighted Avg balances recency with history

---

## 6. What the ensemble is called in ML

### Formal names
- **Model ensemble / ensemble averaging**
- **Simple average ensemble**
- **Committee machine** (older ML term)
- **Model blending** (used in Kaggle competitions)

### The broader ML context

Ensembles are one of the most powerful and most used techniques in ML:

**Bagging (Bootstrap Aggregating)**
- Random Forest: trains many decision trees on random subsets of data, averages their predictions
- Reduces variance by averaging models that make different errors on different subsets

**Boosting**
- XGBoost, LightGBM, Gradient Boosting: train models sequentially where each model tries to correct the errors of the previous
- Reduces bias by focusing later models on hard cases

**Stacking**
- Train a "meta-learner" on the outputs of multiple models
- The meta-learner learns the optimal weights for each model rather than simple averaging

**This page**
- Simple average ensemble of the top 3 models (no meta-learner, equal weights)
- This is called "equal-weight model averaging" or "simple blending"

### The forecasting competition context

The M4 Competition (2018), the largest ever academic forecasting competition, found that:
1. Simple ensembles outperformed most individual models
2. Combining ML methods with classical methods worked best
3. No individual model dominated across all time series

---

## 7. Real-world uses of ensemble forecasting

### Weather forecasting
The US National Weather Service uses ensemble forecasting as its primary method.

Instead of one weather model, they run 20–50 slightly different models (each starting from slightly perturbed initial conditions). The ensemble spread tells you forecast confidence.

The "cone of uncertainty" you see in hurricane forecasts is literally the spread of an ensemble.

### Finance / hedge funds
- Two Sigma, D.E. Shaw, Renaissance Technologies use ensemble methods for their quantitative strategies
- Rather than betting on one model being right, they blend dozens of models with different time horizons and signals

### Economic forecasting
The Fed, IMF, and World Bank publish "Survey of Professional Forecasters" — a survey of many economists' predictions. The consensus estimate is literally an equal-weight ensemble of all submitted forecasts.

### Medical diagnosis
- Ensemble of multiple algorithms (random forest + neural network + logistic regression) for cancer detection
- No single algorithm dominates; blending reduces false positive/negative rates

### This page
- Top 3 models by backtest MAPE, equal-weight averaged for each future year

---

# PART 2: SCENARIOS

## 8. What scenarios are

Scenarios are **planning tools, not forecasting models**.

They answer: what would revenue look like if the company's future growth matched its historical lower, middle, or upper growth distribution?

They are not:
- produced by any of the six models
- trained on historical data in a fitting sense
- a recommendation for what revenue WILL be

They are:
- three reference paths for planning
- anchored in historical growth percentiles
- used to frame the uncertainty around the model forecasts

---

## 9. How scenarios are built

### Step 1 — collect all historical year-over-year growth rates

```
growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁
```

For a company with 8 years of history, there are 7 growth rates.

### Step 2 — take percentiles of the growth rate distribution

```
g_pessimistic = percentile_25(all growth rates)
g_baseline    = percentile_50(all growth rates)   ← median
g_optimistic  = percentile_75(all growth rates)
```

### Step 3 — compound from the latest actual

```
scenario_h = last_sales × (1 + g)^h
```

Where h = 1, 2, 3, 4, 5 (the forecast years).

---

## 10. What percentile means — with a worked example

### Data: 7 historical growth rates for a company

Unsorted: 12%, -5%, 18%, 6%, 9%, 22%, 3%

Sorted ascending: -5%, 3%, 6%, 9%, 12%, 18%, 22%

For 7 values:
- 25th percentile position: 0.25 × (7+1) = 2.0 → value at position 2 = **3%**
- 50th percentile position: 0.50 × (7+1) = 4.0 → value at position 4 = **9%**
- 75th percentile position: 0.75 × (7+1) = 6.0 → value at position 6 = **18%**

So:
- Pessimistic: g = 3%
- Baseline: g = 9%
- Optimistic: g = 18%

### Forecast (latest actual = 100)

```
Pessimistic: 100 × 1.03^1 = 103
Baseline:    100 × 1.09^1 = 109
Optimistic:  100 × 1.18^1 = 118
```

For year 5:
```
Pessimistic: 100 × 1.03^5 = 115.9
Baseline:    100 × 1.09^5 = 153.9
Optimistic:  100 × 1.18^5 = 229.0
```

The scenarios diverge significantly over 5 years — this is the power of compounding.

---

## 11. What percentile means in plain English

- **25th percentile (pessimistic):** 25% of the company's historical growth years were at or below this rate. It represents the lower quarter of growth performance.
  - Plain English: the company grew at its "weaker historical pace"

- **50th percentile (baseline / median):** Half of the growth years were above this, half below. The typical growth rate.
  - Plain English: the company grew at its "typical historical pace"

- **75th percentile (optimistic):** 75% of the growth years were at or below this rate. Only the top quarter of growth years exceeded this.
  - Plain English: the company grew at its "stronger historical pace"

---

## 12. Why scenarios exist alongside the model forecasts

### The model forecasts answer: "what is the most likely future given the historical pattern?"

Each of the six models has a specific structural assumption about how growth works. The ensemble blends the best three.

### The scenarios answer: "what is the range of outcomes?"

Even if the ensemble says "revenue will be 5.6B in 2027," the true outcome depends on:
- macro environment (recession risk, interest rates)
- competitive dynamics
- consumer demand
- company execution

Scenarios frame the uncertainty without claiming to know the answer.

**Scenario Use Cases:**
- Board presentation: "Our base case is 5.6B, but under pessimistic conditions it could be 5.0B and under optimistic conditions 6.1B"
- Stress testing: "Can we sustain our cost structure if revenue comes in at the pessimistic scenario?"
- Planning: "If optimistic scenario materializes, we need to plan for higher headcount and CapEx"

---

## 13. What scenarios are called in Finance / ML

### In Finance
- **Scenario analysis** — standard practice in FP&A (Financial Planning & Analysis)
- **Sensitivity analysis** — testing how outcomes change with different input assumptions
- **Monte Carlo simulation** — a more sophisticated version where thousands of scenarios are randomly drawn from a distribution (not used on this page — this uses three specific percentile points)

### In ML / Statistics
- **Prediction intervals** — Linear Regression computes a formal 90% prediction interval
- **Quantile forecasting** — forecasting the 10th, 50th, 90th percentiles of the output distribution directly (used in Amazon's demand forecasting system DeepAR)
- **Conformal prediction** — a modern framework for computing valid prediction sets

The scenarios on this page are a simplified version of quantile forecasting: instead of modeling the quantiles of the forecast distribution, they use the quantiles of the historical growth rate distribution as a proxy.

---

## 14. Real-world uses of scenario analysis

### Investment banking
Every M&A analysis has a "bull / base / bear" scenario model. The scenarios define the range of enterprise values used to price the deal.

### Corporate FP&A
Every publicly traded company's annual planning cycle uses scenario analysis:
- Management plan: base case
- Stretch: optimistic case
- Conservative: pessimistic case

The CFO presents all three to the board.

### Government / Policy
The Congressional Budget Office (CBO) publishes optimistic and pessimistic scenarios for GDP growth, deficit, and employment alongside the baseline.

### Insurance
Insurance companies price products by modeling pessimistic scenarios for mortality, catastrophe, and credit. They must hold reserves large enough to survive the pessimistic scenario.

### This page
- Three revenue scenarios derived from the 25th, 50th, and 75th percentiles of the company's historical annual growth distribution
- Used as planning bounds around the model ensemble forecast

---

## 15. How to use the ensemble and scenarios together

### Best reading approach

1. Look at the ensemble forecast → this is the best single estimate given the historical pattern
2. Look at the baseline scenario → this tells you what the historical median growth rate implies
   - If ensemble > baseline: recent models are betting on above-historical-median growth
   - If ensemble < baseline: recent models are being conservative about growth
3. Look at the pessimistic-to-optimistic range → this is the historical growth distribution, applied forward
   - A narrow range means the company had very consistent growth historically
   - A wide range means the company's growth was highly variable historically

### Warning signs to look for

- Ensemble significantly above optimistic scenario → recent model momentum is outside the company's historical norm. Be cautious.
- Ensemble below pessimistic scenario → the models are projecting growth below the company's worst historical pace. Usually means linear regression is dragging down the ensemble.
- Baseline scenario roughly matching ensemble → the best single-model estimate is consistent with the historical median. Most reassuring reading.
