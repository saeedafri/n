# Linear Regression — Complete Deep Dive

## What this file covers

Everything about Linear Regression:
- what it is in plain English
- what problem it was invented to solve
- the full mathematical derivation
- what "training" means and how it happens
- what each number the model produces actually means
- where it is used in the real world beyond this page
- when it works brilliantly and when it fails
- real worked examples you can verify by hand

---

## 1. What is Linear Regression in plain English

Linear Regression draws the best straight line through a set of historical data points.

That is it.

Every fancy name and every formula is just a precise description of how to draw that line and how to measure how well it fits.

On this page:
- the historical data points are `(year, annual revenue)` pairs
- the best straight line is the one that is as close as possible to every point
- the projection is that same line extended into future years

---

## 2. What problem it was invented to solve

Linear Regression was invented in the 1800s by Francis Galton and Carl Friedrich Gauss (independently, in different forms).

Gauss used it to predict the position of the asteroid Ceres after it disappeared behind the sun.
He had only 41 days of observations. He needed to predict where it would reappear months later.
His predictions matched the actual re-emergence to within a fraction of a degree.

The method has been used ever since for any situation where you want to understand:
- how much one thing changes when another thing changes by one unit
- or how to project a trend into the future

---

## 3. The exact assumption this model makes

Linear Regression assumes:

**Revenue grows by the same dollar amount every year.**

Not the same percentage. The same absolute dollar number.

Example:
- 2021: $10B
- 2022: $11B (+$1B)
- 2023: $12B (+$1B)
- 2024: $13B (+$1B)

This is a perfect linear pattern. The model fits perfectly here.

If a company instead grows by 10% per year (compounding), the amounts are:
- 2021: $10B
- 2022: $11B (+$1B)
- 2023: $12.1B (+$1.1B)
- 2024: $13.31B (+$1.21B)

The dollar amounts are increasing each year. The model will fit reasonably but not perfectly — it will underestimate in later years because the actual growth is accelerating.

---

## 4. The training process — how the model learns

### What "training" means here

Training = computing the slope and intercept from the data.

This is not:
- iterative gradient descent (used in neural networks)
- trial and error
- random initialization

This is a single closed-form formula that gives the exact answer immediately.

The algorithm is called **Ordinary Least Squares (OLS)**.

### Why "Least Squares"

The word "squares" refers to the objective: we minimize the sum of **squared** errors.

Why square the errors and not just the errors themselves?

1. Squaring makes all errors positive (so +3 and -3 contribute equally)
2. Squaring penalizes larger errors more (a miss of 4 contributes 16, not 8)
3. Squaring produces a smooth convex function that has one global minimum

### The objective function

Let:
- `n` = number of historical data points (years of revenue)
- `yᵢ` = the actual revenue for year i
- `xᵢ` = the year value for observation i

We want to find `slope` (call it `β₁`) and `intercept` (call it `β₀`) to minimize:

```
SSE = Σᵢ (yᵢ − β₀ − β₁xᵢ)²
```

SSE = Sum of Squared Errors.

### Solving the minimization — the full derivation

To minimize SSE with respect to β₀, take the partial derivative and set to zero:

```
∂SSE/∂β₀ = −2 Σ(yᵢ − β₀ − β₁xᵢ) = 0
```

This simplifies to:
```
Σyᵢ = nβ₀ + β₁Σxᵢ
ȳ = β₀ + β₁x̄
β₀ = ȳ − β₁x̄
```

Where `ȳ` = mean revenue, `x̄` = mean year.

To minimize SSE with respect to β₁, take the partial derivative and set to zero:

```
∂SSE/∂β₁ = −2 Σxᵢ(yᵢ − β₀ − β₁xᵢ) = 0
```

Substituting `β₀ = ȳ − β₁x̄`:

```
Σxᵢ(yᵢ − ȳ + β₁x̄ − β₁xᵢ) = 0
Σ(xᵢ − x̄)(yᵢ − ȳ) = β₁ Σ(xᵢ − x̄)²
```

Therefore:

```
β₁ (slope) = Σ(xᵢ − x̄)(yᵢ − ȳ) / Σ(xᵢ − x̄)²
β₀ (intercept) = ȳ − β₁ × x̄
```

These two formulas compute the globally optimal slope and intercept in O(n) time.

In Python, `scipy.stats.linregress(years, sales)` computes exactly this.

---

## 5. What each output number means

### slope

How many billion dollars the revenue line rises per calendar year.

If slope = 0.5 → the model expects $0.5B more revenue each year.
If slope = -0.2 → the model expects revenue to decline by $0.2B each year.
If slope = 2.1 → strong upward trend of $2.1B per year.

### intercept

The mathematical starting point of the fitted line if you plug in year = 0.
For years like 2021–2030, this produces a very large negative number (like -4000) because year = 0 is 2000+ years before the data. The intercept has no stand-alone intuitive meaning.

The intercept exists only so the formula `intercept + slope × year` gives the right revenue for any given year.

### R² (R-squared)

How well the straight line explains the historical data.

Formula:
```
SS_total    = Σ(yᵢ − ȳ)²         ← total variation around the mean
SS_residual = Σ(yᵢ − ŷᵢ)²        ← variation not explained by the line
R²          = 1 − SS_residual / SS_total
```

Where `ŷᵢ = β₀ + β₁xᵢ` is the fitted value for year i.

Interpretation:
- R² = 0.95 → the line explains 95% of the variation in revenue
- R² = 0.50 → the line explains 50%
- R² = 0.10 → the line barely explains the data; revenue is very non-linear

For a perfectly straight historical series (revenue adding exactly $X every year), R² = 1.0.
For a highly volatile series (up, down, up, down), R² might be 0.2 or lower.

### The 90% prediction interval

The chart shows a shaded band around the forecast line. This band widens as you go further into the future.

The formula for the width of the band at a future year `x_future`:

```
se   = standard error of the regression
        = sqrt(SSE / (n - 2))

band = ±1.645 × se × sqrt(1 + 1/n + (x_future − x̄)² / Σ(xᵢ − x̄)²)
```

Why 1.645? That is the 95th percentile of the standard normal distribution. For a 90% interval we need 1.645 on each side (5% in each tail).

The three terms under the square root:
- `1` → prediction error from new data
- `1/n` → uncertainty in the intercept estimate
- `(x_future − x̄)² / Σ(xᵢ − x̄)²` → uncertainty grows as you forecast farther from the historical mean year

---

## 6. Full worked example — verify by hand

### Data

| Year | Revenue (B) |
|------|------------|
| 2020 | 5.0 |
| 2021 | 5.8 |
| 2022 | 6.2 |
| 2023 | 7.0 |
| 2024 | 7.5 |

### Step 1 — compute means

```
x̄ (mean year)    = (2020 + 2021 + 2022 + 2023 + 2024) / 5 = 2022
ȳ (mean revenue) = (5.0 + 5.8 + 6.2 + 7.0 + 7.5) / 5 = 6.3
```

### Step 2 — compute numerator of slope

```
(2020 − 2022)(5.0 − 6.3) = (−2)(−1.3) = +2.6
(2021 − 2022)(5.8 − 6.3) = (−1)(−0.5) = +0.5
(2022 − 2022)(6.2 − 6.3) = (0)(−0.1)  = 0.0
(2023 − 2022)(7.0 − 6.3) = (+1)(+0.7) = +0.7
(2024 − 2022)(7.5 − 6.3) = (+2)(+1.2) = +2.4

Sum = 2.6 + 0.5 + 0 + 0.7 + 2.4 = 6.2
```

### Step 3 — compute denominator of slope

```
(−2)² = 4
(−1)² = 1
(0)²  = 0
(+1)² = 1
(+2)² = 4

Sum = 10
```

### Step 4 — slope

```
slope = 6.2 / 10 = 0.62 B/year
```

The model says revenue grows by $0.62B per year.

### Step 5 — intercept

```
intercept = 6.3 − 0.62 × 2022 = 6.3 − 1253.64 = −1247.34
```

### Step 6 — forecast 2025 and 2026

```
forecast_2025 = −1247.34 + 0.62 × 2025 = −1247.34 + 1255.5 = 8.16 B
forecast_2026 = −1247.34 + 0.62 × 2026 = −1247.34 + 1256.12 = 8.78 B
```

---

## 7. What this model is called in Machine Learning / Statistics

**The formal names:**
- Linear Regression
- Ordinary Least Squares (OLS) Regression
- Simple Linear Regression (when there is one input variable, which is the case here: year)
- Multiple Linear Regression (when there are multiple input variables, which is not the case here)

**The process name:** Training = Fitting = Estimating the regression coefficients.

**The technical term for slope and intercept:** Regression coefficients or model parameters.

**The optimization method:** Analytical solution / closed-form solution (not iterative gradient descent).

**In statistics it belongs to:** Parametric estimation, frequentist regression.

**In machine learning it is classified as:** Supervised learning, regression task, linear model family.

---

## 8. Real-world places Linear Regression is used

### Finance and economics
- Predicting GDP growth from government spending
- Estimating stock returns from market factors (this is called Factor Models / CAPM)
- Pricing models: how much does house price change per extra square foot? (this is linear regression)

### Healthcare
- Predicting drug dosage needed based on body weight
- Estimating hospital readmission risk from patient characteristics
- Understanding how age predicts blood pressure

### Marketing
- How much does one more $1M in advertising spend change sales? The slope is called "marginal return on ad spend"
- Price elasticity: how much does demand drop when price rises by $1?

### Sports analytics
- How much does wins above replacement predict player salary?

### Engineering
- Calibrating sensors: how does voltage reading change with temperature?

### This page specifically
- Fitting a revenue trend line to 7–15 years of annual company revenue
- Projecting that same trend 5 years forward
- Showing the 90% prediction band for the projected years

---

## 9. Why Linear Regression can produce a forecast below the latest actual

This surprises many people.

If a company has been growing rapidly in the last 2 years but grew slowly in years 4–10, the long-run straight line will pass **below** the recent momentum.

Example: ANF on the current page.
- ANF grew 15.8% in 2024, 15.6% in 2025, 6.4% in 2026
- But before 2024, growth was slower
- The straight line through all history has a modest slope
- That straight line extrapolated to 2027 ends up below the 2026 actual

This is not the model being wrong — it is the model doing exactly what it was designed to do: reflect the **long-run trend**, not the most recent momentum.

---

## 10. Strengths and weaknesses

### Strengths
- Fastest to compute (one closed-form calculation)
- Provides R² fit quality score
- Provides uncertainty interval (prediction band)
- Very easy to explain to a non-technical audience
- Performs well for mature, stable businesses with linear growth patterns
- Immune to endpoint sensitivity (unlike CAGR)

### Weaknesses
- Assumes equal dollar growth each year (not appropriate for compounders)
- Sensitive to the full history — one extraordinary year pulls the line
- Extrapolation assumes the same straight-line trend continues indefinitely
- For companies with recent acceleration, it systematically under-forecasts
- For companies in decline then recovery, the straight line may miss the V-shape

### When to trust it
- Companies with 10+ years of very steady dollar-amount revenue growth
- R² > 0.90 and low residual standard error
- When the business model is stable and capital-intensive (utilities, infrastructure)

### When to be skeptical
- R² < 0.70 (line does not fit well)
- Company has had major structural changes (spinoffs, acquisitions, COVID dip)
- Most recent years are all on one side of the fitted line (suggests non-linearity)
