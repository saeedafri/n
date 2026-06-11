# 05 CAGR — Compound Annual Growth Rate

## Full name breakdown

| Word | Meaning |
|------|---------|
| **Compound** | Growth builds on top of previous growth (each year's gain is applied to the previous year's total, not the starting value) |
| **Annual** | Per year |
| **Growth** | Rate of increase |
| **Rate** | A percentage |

Together: **"The single steady annual percentage that, if applied every year through compounding, would take revenue from the first observed value to the last."**

---

## Core assumption

This model believes:

> There is one representative constant growth rate that summarizes the company's entire history. That same rate will continue into the future.

Unlike Linear Regression (which assumes the same dollar added each year), CAGR assumes the same **percentage** added each year. This makes it a compounding model.

---

## The mathematical formula

**Training (computing the rate):**

```
CAGR = (last_sales / base_sales)^(1/n) − 1

where:
  last_sales = most recent cleaned annual revenue
  base_sales = earliest cleaned annual revenue
  n          = number of year-to-year intervals = (number of years − 1)
```

**Derivation:** If revenue grows at rate g every year by compounding:

```
sales_T = sales_0 × (1 + g)¹ × (1 + g)¹ × ... (n times)
        = sales_0 × (1 + g)ⁿ

Solving for g:
  (1 + g)ⁿ = sales_T / sales_0
  1 + g     = (sales_T / sales_0)^(1/n)
  g         = (sales_T / sales_0)^(1/n) − 1
```

**Forecasting:**

```
forecast_h = last_sales × (1 + CAGR)^h

for h = 1, 2, 3, 4, 5
```

---

## Exact Python code in retailer_forecaster.py

```python
def _forecast_cagr(self, years, sales, periods, lookback=None):
    if lookback and lookback < len(sales):
        base, n = sales[-lookback], lookback - 1
    else:
        base, n = sales[0], len(sales) - 1          # uses first and last points
    cagr = np.power(sales[-1] / base, 1/n) - 1 if n > 0 else 0
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return {
        "method": "CAGR",
        "forecast": np.array([sales[-1] * np.power(1 + cagr, i) for i in range(1, periods+1)]),
        "years": fy,
        "cagr": cagr,
    }
```

`np.power(a, b)` computes aᵇ. The CAGR call is `np.power(last/base, 1/n)`.

The forecast loop computes `last_sales × (1+CAGR)¹`, `last_sales × (1+CAGR)²`, etc.

---

## Geometric mean — the mathematical identity

CAGR is equivalent to the **geometric mean** of the annual gross growth factors:

```
Geometric mean = [(1+g₁)(1+g₂)...(1+gₙ)]^(1/n) − 1
```

**AM-GM Inequality:** For any non-constant positive series, the geometric mean is always **less than or equal to** the arithmetic mean:

```
Geometric mean ≤ Arithmetic mean
CAGR           ≤ Simple average of year-over-year growth rates
```

This is why CAGR is usually a more conservative estimate than the simple average of growth rates.

**Example:** Growth rates: +50%, −20%, +30%
- Simple average: (50 − 20 + 30) / 3 = **20%**
- CAGR: (1.50 × 0.80 × 1.30)^(1/3) − 1 = 1.56^(1/3) − 1 = **15.9%**

CAGR (15.9%) correctly captures actual compound growth; the arithmetic mean (20%) overstates it.

---

## Worked example — step by step

| Year | Revenue ($B) |
|------|-------------|
| 2019 | 3.0 |
| 2020 | 2.7 (dip) |
| 2021 | 3.2 |
| 2022 | 3.8 |
| 2023 | 4.2 |

**n** = 5 − 1 = **4 intervals**

```
CAGR = (4.2 / 3.0)^(1/4) − 1
     = 1.4^0.25 − 1
     = 1.0878 − 1
     = 0.0878
     = 8.78%
```

**Forecasts from last actual (4.2B):**

```
2024: 4.2 × 1.0878¹ = 4.57B
2025: 4.2 × 1.0878² = 4.97B
2026: 4.2 × 1.0878³ = 5.41B
2027: 4.2 × 1.0878⁴ = 5.89B
```

Note: The 2020 dip is invisible — CAGR only looks at 3.0 (start) and 4.2 (end). The dip did not affect the CAGR at all.

---

## The endpoint problem — the most important limitation

CAGR uses **only two data points**: the first year and the last year. Every year in between is completely ignored.

**This creates serious sensitivity to unusual start or end years:**

| Scenario | Effect on CAGR |
|----------|---------------|
| Last year was a COVID crash | CAGR is severely underestimated |
| Last year was a post-COVID spike | CAGR is overestimated |
| First year was an unusually strong year | CAGR is underestimated (high base) |
| First year was a weak year | CAGR is overestimated (low base) |

**Mitigation in the code:** The outlier detection step (Stage 2) removes years with z-score > 2.0 before CAGR is computed. So a COVID crash year (2020) flagged as an outlier would be excluded, and the CAGR would use the next available first and last points.

---

## Real page example: ANF

- First clean year revenue: approximately $2.3B (2006)
- Latest actual revenue (2026): $5.266B
- n ≈ 20 intervals

```
CAGR = (5.266 / 2.3)^(1/20) − 1 ≈ 2.29^0.05 − 1 ≈ 4.2%
2027 forecast = 5.266 × 1.042 ≈ 5.4B
```

The page shows **$5.4B** for ANF 2027 CAGR. ✓

Why is it close to the latest actual? Because a 4.2% growth rate applied once gives a small uplift over the current $5.266B.

---

## Edge cases and failure modes

| Situation | What happens |
|-----------|-------------|
| n = 0 (only one data point) | Code returns CAGR = 0, forecast = last_sales flat |
| last_sales = 0 | Division error avoided: `if n > 0 else 0` — returns 0 growth |
| base_sales = 0 | `last_sales / base_sales` → infinity → handled by outlier removal upstream |
| Very long history (20+ years) | CAGR is anchored to old starting point — may not reflect recent behavior |
| Company underwent major M&A | Starting or ending revenue is not comparable → CAGR misleads |

---

## CAGR vs other models

| Situation | CAGR is better than... | Because... |
|-----------|----------------------|-----------|
| Finance presentation | MA Trend | CAGR is the standard language of finance |
| Long-run summary | Linear Regression | Percentage growth is more natural than absolute dollars |
| Company with variable annual growth | Weighted Avg | CAGR smooths out all variability into one number |

| Situation | CAGR loses to... | Because... |
|-----------|----------------|-----------|
| Recent momentum matters | MA Trend | CAGR ignores what happened recently |
| Endpoint was unusual | Any model | CAGR is too sensitive to start/end |

---

## Why this model can be useful

- The standard financial language: "This company's 5-year CAGR is 8%" is universally understood
- Good for stable, consistently growing businesses
- Clean single-number summary of long-run growth
- Used in M&A, IPO roadshows, investor presentations

## Where it can struggle

- First or last year was anomalous
- Company changed its business model
- Recent acceleration or deceleration should be weighted more
- Middle-of-series volatility matters (CAGR ignores it entirely)

## What to say when presenting

> "CAGR asks: what single constant annual growth rate would take revenue from our earliest observation to the most recent one? It then applies that same rate into the future. It is the number most finance teams quote, but it is sensitive to which year we start and end on."
