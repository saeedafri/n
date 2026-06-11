# 09 Weighted Average Growth

## Full name and aliases

- **Weighted Average Growth Rate**
- **Linearly Weighted Historical Growth Rate**
- Related to: triangular weighting, linear ramp weighting

---

## Core assumption

This model believes:

> Every historical year contains useful information, but recent years are more informative than older ones. The weight should increase linearly with recency — the most recent year gets the highest weight, older years get progressively less, but none are completely excluded.

This sits philosophically between two extremes:
- **MA Trend**: hard cutoff at 3 years — old data discarded entirely
- **CAGR**: only first and last points — middle history ignored entirely
- **Weighted Average**: all history included, smoothly discounted by age

---

## The weighting scheme

For a series with n years, there are n−1 year-over-year growth rates.

Assign integer weights:
```
oldest growth rate → weight = 1
second oldest      → weight = 2
third oldest       → weight = 3
...
most recent        → weight = n−1
```

The weight vector is: `[1, 2, 3, ..., n−1]`

**Sum of weights (triangular number):**
```
Σ weights = 1 + 2 + 3 + ... + (n−1) = (n−1) × n / 2
```

This is the triangular number formula: `T(n−1) = n(n−1)/2`.

**Example:** 5 growth rates → weights = [1, 2, 3, 4, 5], sum = 15 = 5×6/2.

---

## The mathematical formula

**Step 1 — Compute year-over-year growth rates:**

```
gₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁       for t = 2, 3, ..., n
```

Produces n−1 growth rates: `[g₂, g₃, ..., gₙ]`

**Step 2 — Assign linear weights:**

```
wₜ = t − 1       (so w₂=1, w₃=2, ..., wₙ=n−1)
```

Or equivalently, assign `[1, 2, 3, ..., n−1]` in order of increasing recency.

**Step 3 — Compute the weighted average growth rate:**

```
ḡ = Σ(wₜ × gₜ) / Σ(wₜ)
```

This is the **weighted arithmetic mean** of all growth rates.

**Step 4 — Compound from the last actual:**

```
forecast_h = salesₙ × (1 + ḡ)^h       for h = 1, 2, 3, 4, 5
```

---

## Exact Python code in retailer_forecaster.py

```python
def _forecast_weighted_avg(self, years, sales, periods):
    growth_rates = np.diff(sales) / sales[:-1]              # Step 1: YoY growth rates
    weights = np.arange(1, len(growth_rates) + 1)           # Step 2: [1, 2, 3, ..., n-1]
    avg_growth = np.average(growth_rates, weights=weights)   # Step 3: weighted mean
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return {
        "method": "Weighted Avg",
        "forecast": np.array([sales[-1] * (1 + avg_growth) ** i for i in range(1, periods + 1)]),
        "years": fy,
    }
```

**Line-by-line explanation:**

- `np.diff(sales) / sales[:-1]` — same as MA Trend: array of YoY growth rates
- `np.arange(1, len(growth_rates) + 1)` — generates `[1, 2, 3, ..., n−1]`; `arange(start, stop)` is exclusive at `stop`, so `arange(1, 5)` = `[1, 2, 3, 4]`
- `np.average(growth_rates, weights=weights)` — computes `Σ(wᵢ × gᵢ) / Σ(wᵢ)` internally; **not** `np.mean()` (which is unweighted)
- Forecast loop: identical to MA Trend — compound the single ḡ forward

**np.average() vs np.mean():**

```python
np.mean([0.02, 0.04, 0.08, 0.12])           # = 0.065 (simple average)
np.average([0.02, 0.04, 0.08, 0.12],
           weights=[1, 2, 3, 4])             # = 0.082 (weighted — recent has more pull)
```

The weighted average is always closer to the most recent values than the simple average.

---

## Gauss triangular number — why the weights sum is exact

The sum of weights `1 + 2 + 3 + ... + (n−1)` is a **triangular number**.

Gauss's formula (discovered by Gauss at age 9 according to legend):
```
1 + 2 + 3 + ... + k = k(k+1)/2
```

For k = n−1 (we have n−1 growth rates):
```
Σ weights = (n−1) × n / 2
```

**Example:** n=5 revenue years → 4 growth rates → weights [1,2,3,4] → sum = 4×5/2 = 10

`np.average()` computes this sum internally — you never need to compute it yourself.

---

## Worked example — step by step

| Year | Revenue ($B) | Growth Rate gₜ | Weight wₜ | wₜ × gₜ |
|------|-------------|---------------|----------|---------|
| 2019 | 3.0 | — | — | — |
| 2020 | 3.3 | 10.0% | 1 | 0.100 |
| 2021 | 3.6 | 9.1% | 2 | 0.182 |
| 2022 | 4.0 | 11.1% | 3 | 0.333 |
| 2023 | 4.5 | 12.5% | 4 | 0.500 |
| 2024 | 5.0 | 11.1% | 5 | 0.555 |

**Sum of weights:** 1+2+3+4+5 = 15

**Weighted sum:** 0.100 + 0.182 + 0.333 + 0.500 + 0.555 = 1.670

```
ḡ = 1.670 / 15 = 0.1113 = 11.13%
```

**Compare to simple average:** (10.0+9.1+11.1+12.5+11.1)/5 = 10.76%

Weighted average (11.13%) is higher because recent years (higher growth) have higher weights.

**Forecasts from last actual (5.0B):**
```
2025: 5.0 × (1.1113)¹ = 5.557B
2026: 5.0 × (1.1113)² = 6.174B
2027: 5.0 × (1.1113)³ = 6.862B
```

---

## Real page example: ANF

ANF's full growth history spans ~20 years. Early years (2006–2015) had slower growth; 2023–2026 had stronger growth. The linear weights assign weight 1 to 2006–2007 growth and weight ~20 to 2025–2026 growth.

```
Page shows: 2027 Weighted Average Growth forecast = 5.5B
Latest actual: 5.266B
Implied ḡ ≈ (5.5 / 5.266)^(1/1) − 1 ≈ 4.4% for 1-year
```

Why is it below MA Trend (5.9B)?
- MA Trend uses only the last 3 years of very strong growth (15.8%, 15.6%, 6.4%), averaging ~12.6%
- Weighted Average includes 15+ years of slower growth as well, which pulls the weighted mean down significantly
- Even with linear recency weighting, the long history of 3–8% growth years has non-trivial total weight

---

## Comparison matrix: Weighted Average vs similar models

| Dimension | Weighted Avg | MA Trend | CAGR | Exp Smoothing |
|-----------|-------------|---------|------|--------------|
| History used | All years | Last 3 only | First + last only | All years |
| Weight scheme | Linear ramp (1,2,...,n) | Equal within window | N/A | Exponential decay |
| Decay speed | Slow (linear, not exponential) | Hard cutoff | No decay | Fast (α controls) |
| Recent years' influence | High but not dominant | Dominant | Minimal (unless recent = endpoint) |  High |
| Result for stable series | Close to simple mean | Close to CAGR | Equal to CAGR | Close to Exp Smoothing |
| Result for accelerating series | Pulled toward recent high growth | Maximum recency | Anchored to long-run rate | Partial recency |

---

## Why linear and not exponential weights?

Exponential weighting (like Exp Smoothing) would make the oldest values negligible even sooner. Linear weighting is a deliberate choice: it gives recent years meaningfully more influence than old years, but does not make old years negligible.

For a 20-year series with exponential decay (α=0.3), the oldest year gets weight ~0.3 × (0.7)^19 ≈ 0.001 — effectively zero.

For a 20-year series with linear weights, the oldest year gets weight 1 out of 210 total ≈ 0.5% — small but not zero.

Linear weighting is more transparent and explainable: "the most recent year gets 20× more weight than the oldest year" is easy to state. "The most recent year gets (1/α)^(n−1) times more weight" is harder to interpret.

---

## Edge cases

| Situation | What happens |
|-----------|-------------|
| Only 2 data points | 1 growth rate; weights = [1]; weighted mean = that single growth rate |
| 3 data points | 2 growth rates; weights = [1, 2]; most recent has 2× influence |
| Declining series | ḡ < 0; all forecasts project further decline |
| Very long history (20+ years) | Early years have negligible weight numerically; recent years dominate, but old years are still non-zero |
| Mixed history (decline then recovery) | Weighted mean balances old decline with recent recovery; may give a moderate positive forecast |
| Negative revenue year | Growth rate that year could be extreme (e.g., −100% to +500%); outlier detection in Stage 2 should remove such years first |

---

## Why this model can be useful

- Uses the full revenue history — no data discarded
- Naturally gives more influence to recent years without being as extreme as a hard-window cutoff
- Produces a single, easily communicable growth rate ("our historically-weighted average growth rate")
- Often provides a middle ground between very recent-heavy (MA Trend) and very long-run (CAGR, Linear Regression)

## Where it can struggle

- If the company had a major structural change (acquisition, market entry) that makes early-year history irrelevant — those years still have positive weight
- Recent noise (one exceptional year inside the high-weight region) can still move ḡ significantly
- Linear weights are not adaptive — the same scheme is applied to all companies regardless of their specific history shape

## What to say when presenting

> "Weighted Average Growth uses all historical annual growth rates but gives more importance to recent ones. The weighting is linear — the most recent year gets n times the weight of the oldest year. It runs through numpy's weighted average function. The result is a single representative growth rate that balances long-run history with recency, typically landing between a pure recent-momentum model and a full long-run model."
