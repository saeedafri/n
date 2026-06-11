# 04 Linear Regression

---

## Start here — the big picture in one sentence

**Linear Regression draws the single best-fitting straight line through all the historical revenue data points, then extends that line into the future as the forecast.**

That is the entire idea. Everything else on this page is just explaining *how* it finds that line and *why* the math works the way it does.

---

## A real-world analogy before any math

Imagine you are tracking how tall a child grows each year:

| Age | Height (cm) |
|-----|-------------|
| 5   | 110 |
| 6   | 116 |
| 7   | 122 |
| 8   | 128 |
| 9   | 133 |

If someone asks "how tall will they be at age 12?", you would draw a straight line through the dots and read off the answer. That is linear regression. You are assuming growth happens at a roughly **constant amount per year** — not a constant percentage, not a curve — a fixed amount.

Linear regression on revenue does the exact same thing, replacing "age" with "calendar year" and "height" with "$B revenue".

---

## The core assumption — the most important thing to understand

> **This model assumes revenue grows by the same dollar amount every year.**

Not the same *percentage* — the same *dollars*.

- "Adds $300M every year" → linear assumption ✓
- "Grows 8% every year" → NOT linear (that is CAGR or exponential)

If a company actually grows at a steady percentage, linear regression will underestimate future revenues because percentage growth accelerates in dollar terms over time. This is the model's biggest limitation, and it explains why the ANF forecast looks low.

---

## The equation — what it means in plain English

The fitted line has this form:

```
Revenue in year t  =  β₀  +  β₁ × (calendar year t)  +  εₜ
```

Let's decode every symbol one by one:

---

### β₁ — the slope (pronounced "beta one")

**Plain English:** How many dollars revenue goes up for each extra year that passes.

**Example:** If β₁ = 0.25, that means every year the company adds $250M. Year after year, same amount.

**Standard name:** Slope coefficient, regression coefficient, or just "the slope."

If β₁ is **positive** → revenue is growing over time.
If β₁ is **negative** → revenue is declining over time.
If β₁ is **zero** → revenue is flat; year has no effect.

---

### β₀ — the intercept (pronounced "beta zero")

**Plain English:** The mathematical starting point of the line — where the line would cross the y-axis if year = 0.

**Why it looks strange:** Since we are using calendar years (2006, 2007, …), year = 0 would mean the year zero AD. That is not a real revenue value. So β₀ comes out as a huge negative number like −502 or −656. **This is normal and expected** — it is just a mathematical artefact. The forecasts themselves are perfectly sensible because the large negative β₀ and the large positive β₁ × year cancel each other at realistic year values.

Think of it this way: the line equation is like a ruler. β₀ tells you where the ruler starts, β₁ tells you the angle. You only read the ruler at the years that matter (2027, 2028, …), and at those years the answer is correct.

**Standard name:** Intercept, constant term, or bias term.

---

### εₜ — the residual (pronounced "epsilon t")

**Plain English:** The gap between what the line predicts and what actually happened that year.

```
εₜ  =  actual revenue in year t  −  what the line says for year t
```

If actual = $4.0B and line says $3.8B, then εₜ = +0.2B. The company beat the line.
If actual = $3.2B and line says $3.5B, then εₜ = −0.3B. The company missed the line.

Residuals are not errors in the sense of "mistakes." They are the natural year-to-year noise that no straight line can capture perfectly.

**Standard name:** Residual, error term, or disturbance.

---

### β notation — why we use Greek letters

In statistics and machine learning, Greek letters are used for **parameters** — the underlying true values we are trying to estimate. A hat symbol (^) is added when the value is **estimated from data** rather than known exactly:

| Symbol | Reads as | Meaning |
|--------|----------|---------|
| β₁ | "beta one" | The true slope (unknowable — we only have a sample of years) |
| β̂₁ | "beta-hat one" | Our best estimate of β₁, computed from the data |
| β₀ | "beta zero" | The true intercept |
| β̂₀ | "beta-hat zero" | Our best estimate of the intercept |
| εₜ | "epsilon t" | The true error in year t |
| σ | "sigma" | True standard deviation of errors |
| σ̂ | "sigma-hat" | Estimated standard deviation of errors |

You will see this hat convention throughout all of statistics and ML — whenever a value is estimated, it gets a hat.

---

## How does it find the best line? — OLS explained simply

There are infinitely many straight lines you could draw through the data. OLS (Ordinary Least Squares) picks the one specific line that minimises the total squared gap between the line and every data point.

**Step 1:** Draw any line through the data.

**Step 2:** For each year, measure the vertical gap between the actual revenue dot and the line. This gap is the residual εₜ.

**Step 3:** Square each gap (so negatives and positives both count as positive, and bigger gaps are penalised more).

**Step 4:** Add all the squared gaps together. This sum is called **RSS — Residual Sum of Squares**.

**Step 5:** Adjust the line (change slope and intercept) until RSS is as small as possible.

The line that achieves the minimum RSS is the OLS line. There is exactly one such line, and the math gives us a direct formula to compute it without trial and error.

```
RSS  =  Σ (actual revenue in year t  −  fitted line value in year t)²
```

The word "Ordinary" in OLS just means the basic version — no tricks, no weights, no modifications. Just minimise the sum of squared residuals.

---

## The formulas for slope and intercept — derived from calculus

You do not need to follow the calculus to use the model. But for those who want to understand where the formulas come from:

To minimise RSS, take the partial derivatives with respect to β₀ and β₁, set them both to zero, and solve. The result is:

```
β̂₁  =  Sxy / Sxx

β̂₀  =  ȳ − β̂₁ × x̄
```

Where:

```
x̄   =  average of all training years
ȳ   =  average of all training revenues

Sxx =  Σ (xᵢ − x̄)²              ← how spread out the years are
Sxy =  Σ (xᵢ − x̄) × (yᵢ − ȳ)   ← how years and revenues move together
```

**Sxx in plain English:** Take each year, subtract the average year, square it. Add all those squares up. This measures how spread out the years are. A longer history (more years) means a larger Sxx, which gives a more stable slope estimate.

**Sxy in plain English:** For each year, take how far above/below average the year is AND how far above/below average the revenue is. Multiply those two together. Add them all up. If revenue tends to be above average in later years, Sxy is positive → upward slope. If revenue tends to be below average in later years (declining company), Sxy is negative → downward slope.

**The slope β̂₁ = Sxy/Sxx is also equal to:**
```
β̂₁  =  r × (σy / σx)
```
where r is the Pearson correlation between years and revenues, σy is the standard deviation of revenues, and σx is the standard deviation of years. This is a standard identity — slope equals correlation times the ratio of spreads.

---

## Exact Python code

```python
def _forecast_linear(self, years, sales, periods):
    slope, intercept, r_value, _, std_err = stats.linregress(years, sales)
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    forecast = intercept + slope * fy
    n = len(years)
    se = std_err * np.sqrt(
        1 + 1/n + (fy - np.mean(years))**2 / np.sum((years - np.mean(years))**2)
    )
    return {
        "method": "Linear Regression",
        "forecast": forecast,
        "years": fy,
        "r_squared": r_value ** 2,
        "ci_lower": forecast - 1.645 * se * np.sqrt(n),
        "ci_upper": forecast + 1.645 * se * np.sqrt(n),
    }
```

`scipy.stats.linregress(years, sales)` — one function call does all the math above and returns:

| Return value | What it is | Plain meaning |
|---|---|---|
| `slope` | β̂₁ | How many $B revenue increases per calendar year |
| `intercept` | β̂₀ | The large negative constant (mathematical anchor only) |
| `r_value` | r (Pearson correlation) | How strongly year and revenue move together; −1 to +1 |
| `_` | p-value | Not used; would test whether the slope is statistically zero |
| `std_err` | SE(β̂₁) | How uncertain we are about the slope estimate |

---

## R² — how well does the line fit? (Coefficient of Determination)

R² answers the question: **"What fraction of the ups and downs in revenue does the straight line explain?"**

```
R²  =  1  −  RSS / TSS

RSS  =  Σ (actual − fitted)²       ← variation the line did NOT explain
TSS  =  Σ (actual − ȳ)²           ← total variation in revenue
```

TSS is the total variation in revenue — how much revenue bounces around its own average.
RSS is what is left over after the line captures the trend.
R² = 1 − (leftover / total) = the fraction the line explained.

| R² value | What it means in plain terms |
|----------|------------------------------|
| 1.00 | The line passes through every single data point perfectly |
| 0.95 | 95% of the revenue variation follows the linear trend; 5% is noise |
| 0.70 | 70% explained; still a meaningful trend but noisy |
| 0.50 | Only half explained; the line captures little of the actual movement |
| 0.00 | The line is no better than just predicting the average every year |

**Important warning:** High R² does not mean good forecasts. A company that grew steadily for 15 years then suddenly changed direction can have R² = 0.95 on the old trend but a completely wrong forecast for the future.

In the code: `r_squared = r_value ** 2`. The function returns `r_value` (Pearson correlation r), and R² = r².

---

## The shaded band — what is it and why does it get wider?

The shaded region on the chart is a **90% Prediction Interval**. It answers:

> "If I had to bet on where the actual revenue will land, I am 90% confident it falls inside this band."

```
Upper bound  =  forecast  +  1.645 × SE_pred
Lower bound  =  forecast  −  1.645 × SE_pred

SE_pred  =  σ̂_ε  ×  √(1  +  1/n  +  (future year − x̄)² / Sxx)
```

**Where does 1.645 come from?**
Under a normal distribution, 90% of all values fall within ±1.645 standard deviations of the mean. So to create a range that captures 90% of outcomes, we go 1.645 standard deviations either side of the forecast. (For 95%, it would be ±1.96.)

**Why does the band get wider as we go further into the future?**

Look at the term `(future year − x̄)² / Sxx` inside the square root. As the forecast year moves further from the average training year x̄, this term grows. The further you are forecasting from the data you trained on, the more uncertain you are. The math makes this explicit.

The three components inside the square root each represent a different source of uncertainty:

| Term | Source of uncertainty |
|------|-----------------------|
| `1` | Irreducible noise — even with perfect parameters, individual years scatter around the line |
| `1/n` | Uncertainty in where the line sits on average — shrinks as you have more training years |
| `(future year − x̄)² / Sxx` | How far you are extrapolating — grows as forecast horizon increases |

---

## Gauss-Markov Theorem — why OLS is the best possible linear method

The Gauss-Markov theorem is a mathematical proof that says:

> **Under four conditions, OLS gives you the most accurate linear estimator possible — no other unbiased linear method has smaller error.**

The four conditions:

| # | Condition name | Plain meaning |
|---|---------------|---------------|
| 1 | Linearity | Revenue really does follow a straight-line relationship with time |
| 2 | Exogeneity | The year variable is not correlated with the noise — the errors are not predictable from the year |
| 3 | Homoscedasticity | The scatter around the line is roughly the same size every year (not bigger for high years, smaller for low years) |
| 4 | No multicollinearity | There is only one predictor (year), so this is automatically satisfied |

When these hold, OLS is called **BLUE — Best Linear Unbiased Estimator**:
- **Best** = smallest possible variance (most precise) among all linear estimators
- **Linear** = the estimator is a simple weighted sum of the observations
- **Unbiased** = on average it gets the right answer (no systematic over- or under-estimation)
- **Estimator** = it produces an estimate from data

**For revenue data:** Conditions 1 and 3 are slightly violated — revenue often grows by percentage (not fixed dollars), and larger companies have bigger absolute errors. OLS is still used because it is simple, explainable, and a good baseline. The violations make it imperfect, not useless.

---

## Real ANF example — full calculation

Let us walk through the exact calculation using 7 years of ANF revenue (2020–2026). The actual model uses all 21 years, but 7 years makes the arithmetic readable.

**Revenue data:**

| Year (x) | Revenue ($B) (y) |
|----------|-----------------|
| 2020 | 3.623 |
| 2021 | 3.129 |
| 2022 | 3.712 |
| 2023 | 3.697 |
| 2024 | 4.281 |
| 2025 | 4.950 |
| 2026 | 5.266 |

**Step 1 — Find the averages:**
```
x̄  =  (2020+2021+2022+2023+2024+2025+2026) / 7  =  2023.0
ȳ  =  (3.623+3.129+3.712+3.697+4.281+4.950+5.266) / 7  =  4.094B
```

**Step 2 — Build the deviation table:**

For each year: how far is it from x̄ = 2023? How far is revenue from ȳ = 4.094?

| Year x | x − x̄ | Revenue y | y − ȳ | (x−x̄)² | (x−x̄)(y−ȳ) |
|--------|-------|-----------|------|---------|------------|
| 2020 | −3 | 3.623 | −0.471 | 9 | +1.413 |
| 2021 | −2 | 3.129 | −0.965 | 4 | +1.930 |
| 2022 | −1 | 3.712 | −0.382 | 1 | +0.382 |
| 2023 |  0 | 3.697 | −0.397 | 0 |  0.000 |
| 2024 | +1 | 4.281 | +0.187 | 1 | +0.187 |
| 2025 | +2 | 4.950 | +0.856 | 4 | +1.712 |
| 2026 | +3 | 5.266 | +1.172 | 9 | +3.516 |
| | | | **Totals →** | **Sxx = 28** | **Sxy = 9.140** |

**Step 3 — Compute slope and intercept:**
```
β̂₁  =  Sxy / Sxx  =  9.140 / 28  =  0.326  ($B per year)

β̂₀  =  ȳ − β̂₁ × x̄  =  4.094 − 0.326 × 2023  =  4.094 − 659.5  =  −655.4
```

**The fitted line:** `Revenue = −655.4 + 0.326 × year`

**Step 4 — Forecast future years:**
```
2027:  −655.4 + 0.326 × 2027  =  −655.4 + 661.0  =  5.6B
2028:  −655.4 + 0.326 × 2028  =  5.9B
2029:  −655.4 + 0.326 × 2029  =  6.2B
```

**Note:** The full 21-year model shows $4.4B for 2027 — lower than this 7-year window — because the older years 2006–2015 had much lower revenues, which pulls the slope down significantly when all 21 years are included equally.

---

## Why ANF's 2027 linear forecast is BELOW the 2026 actual

This surprises people at first. The 2026 actual is $5.266B but the 2027 forecast is $4.4B — the model is predicting a decline?

Here is exactly what is happening:

1. OLS fits one straight line across **all 21 years equally** — from 2006 ($3.0B) to 2026 ($5.266B)
2. ANF's revenues were **flat to declining from 2012 to 2020** (brand struggles, store closures)
3. Those flat/declining years pull the long-run slope down — the line's angle is shallower than recent momentum suggests
4. ANF's recent 2022–2026 surge puts actual revenues **above the long-run line**
5. The forecast extends the long-run line — not the recent surge
6. Because the 2026 actual sits above the long-run line, the very next point on the long-run line (2027) is below the 2026 actual

The model is not wrong — it is answering a specific question: "If we trust all 21 years of history equally and assume a constant dollar growth rate, what does 2027 look like?" The answer is $4.4B. Whether you should trust all 21 years equally for ANF is a different question — and that is exactly why six models are compared.

---

## Edge cases

| Situation | What happens | What to watch for |
|-----------|-------------|------------------|
| 3–4 years of history | One unusual year can flip the slope sign entirely | R² very high (overfitting) or very low; SE(β̂₁) large |
| Company recently pivoted business model | Old revenues are from a different business; slope is meaningless | Forecast far below or above recent actuals |
| Revenue declining overall | β̂₁ is negative → forecasts decline indefinitely | Check if secular decline or temporary dip |
| 20+ years of history | Old slow-growth years dominate; slope is lower than recent reality | All other models will forecast higher |
| Perfect flat revenue | β̂₁ = 0, R² = 0, forecast = average revenue forever | Correct behaviour |

---

## When to trust this model more vs less

**Trust it more when:**
- The company has grown at roughly the same dollar amount per year for many years
- R² is above 0.85 (the line fits history well)
- The forecast is within the range of recent actuals (not a big outlier)

**Trust it less when:**
- The company grows by percentage (tech, e-commerce, fast-growth retail)
- There was a recent structural change (acquisition, spin-off, new CEO, new market)
- The forecast is far below recent actuals (as with ANF) — this means recent momentum is not captured

**Its main value:** It is the conservative baseline. If all other models forecast $6B and linear says $4.4B, the gap tells you the other models are betting on recent momentum continuing. Linear regression makes no such bet.

---

## What to say when presenting

**To a non-technical audience:**
> "Linear regression draws the best straight line through all the historical revenue data and extends it forward. The slope of that line tells us how many dollars revenue has grown per year on average across the entire history. It is the most conservative model — it does not chase recent momentum."

**To a technical audience:**
> "We fit an OLS simple linear regression of annual revenue on calendar year. β̂₁ is Sxy/Sxx — the sample covariance of year and revenue divided by the sample variance of year — equivalently the Pearson r times the ratio of standard deviations. β̂₀ is large and negative due to calendar year scaling, a pure artefact. The prediction interval uses σ̂_ε times root(1 + 1/n + leverage), where σ̂_ε uses n−2 degrees of freedom (Bessel's correction for two estimated parameters). The 90% band uses z = 1.645. R² is r² from `scipy.stats.linregress`. Under the Gauss-Markov conditions, OLS is BLUE; mild heteroscedasticity and the log-linear true DGP make it approximate, but it serves as the conservative baseline anchor."
