# 06 Exponential Smoothing

## Full name and aliases

- **Simple Exponential Smoothing (SES)** with a post-hoc linear trend
- **EWMA — Exponentially Weighted Moving Average**
- **Brown's Single Exponential Smoothing**
- ETS classification: **ETS(A,N,N)** for the level; the code adds a backward-difference trend, making it closer to **ETS(A,A,N)** without joint parameter estimation

---

## Core assumption

This model believes:

> Recent years matter more than older years. A bad year from a decade ago should barely influence the forecast. But we should not over-react to every spike — we smooth the series first, then project the smoothed trend.

---

## The key parameter: α (alpha)

```
α = 0.3   (fixed in code — not learned from data)
```

| α value | Meaning |
|---------|---------|
| α = 0.3 | 30% weight to the newest actual, 70% carried from the previous smoothed level |
| α → 0 | Very slow to react; forecast is close to the long-run mean |
| α → 1 | Naïve model — forecast = last actual value only |

**Why 0.3 is a common default:** Industry standard in many ERP and demand planning systems. On short annual series (5–15 data points), estimating α by maximum likelihood tends to overfit. A fixed α = 0.3 generalizes well.

---

## The mathematical formula

**Initialization:**

```
L₁ = sales₁       (set the first smoothed level to the first actual value)
```

**Recursive update for each subsequent year:**

```
Lₜ = α × salesₜ + (1 − α) × Lₜ₋₁

where:
  Lₜ   = smoothed level at time t
  salesₜ = actual revenue at time t
  α    = 0.3
```

**Trend extraction (applied once after the full history is processed):**

```
trend = L_last − L_prev
      = Lₙ − Lₙ₋₁
```

**Forecast:**

```
forecast_h = L_last + h × trend       for h = 1, 2, 3, 4, 5
```

---

## Exact Python code in retailer_forecaster.py

```python
def _forecast_exp_smoothing(self, years, sales, periods, alpha=0.3):
    smoothed = [sales[0]]                                 # L₁ = first actual
    for i in range(1, len(sales)):
        smoothed.append(alpha * sales[i] + (1 - alpha) * smoothed[-1])
    trend = smoothed[-1] - smoothed[-2] if len(smoothed) > 1 else 0
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return {
        "method": "Exp Smoothing",
        "forecast": np.array([smoothed[-1] + trend * i for i in range(1, periods + 1)]),
        "years": fy,
    }
```

---

## Why "exponential"? — The weight decay proof

Expand the recursion backwards for Lₙ:

```
Lₙ = α × salesₙ + (1−α) × Lₙ₋₁
   = α × salesₙ + (1−α)[α × salesₙ₋₁ + (1−α) × Lₙ₋₂]
   = α × salesₙ + α(1−α) × salesₙ₋₁ + (1−α)² × Lₙ₋₂
   = ...
   = α × Σₖ₌₀ⁿ⁻¹ (1−α)ᵏ × salesₙ₋ₖ   +   (1−α)ⁿ × L₁
```

Each past observation gets weight `α × (1−α)^k` where k = how many years ago.

**With α = 0.3:**

| Years ago (k) | Weight |
|--------------|--------|
| 0 (most recent) | 0.300 |
| 1 | 0.210 |
| 2 | 0.147 |
| 3 | 0.103 |
| 4 | 0.072 |
| 5 | 0.050 |
| 10 | 0.009 |

Weights decrease **exponentially** as data gets older — that is why the model is called "Exponential Smoothing."

---

## Worked example — every step

| Year | Actual ($B) | Smoothed Lₜ | Calculation |
|------|------------|-------------|-------------|
| 2020 | 10.0 | **10.000** | L₁ = first actual |
| 2021 | 14.0 | **11.200** | 0.3×14 + 0.7×10 = 4.2 + 7.0 |
| 2022 | 13.0 | **11.740** | 0.3×13 + 0.7×11.2 = 3.9 + 7.84 |
| 2023 | 15.0 | **12.718** | 0.3×15 + 0.7×11.74 = 4.5 + 8.218 |
| 2024 | 16.0 | **13.903** | 0.3×16 + 0.7×12.718 = 4.8 + 8.903 |

```
trend = L_last − L_prev = 13.903 − 12.718 = 1.185

2025 forecast = 13.903 + 1 × 1.185 = 15.088B
2026 forecast = 13.903 + 2 × 1.185 = 16.273B
2027 forecast = 13.903 + 3 × 1.185 = 17.458B
```

Notice: the smoothed values (10.0, 11.2, 11.74...) are calmer than the actual values (10, 14, 13, 15, 16). That is the smoothing effect.

---

## Comparison: Exp Smoothing vs Holt's method

Both use α for level smoothing. The key difference:

| | Exp Smoothing (this model) | Holt's Linear Trend |
|---|---|---|
| Trend update | Single backward difference of last 2 smoothed values | Separate β-smoothed trend state updated every year |
| Trend adapts? | No — fixed at final backward difference | Yes — updates every year |
| Parameters | α only | α and β |
| Better for | Stable series, mild noise | Series with momentum shifts |

---

## Real page example: ANF

ANF 2027 forecast from Exponential Smoothing: **$4.8B**

Why is it lower than MA Trend ($5.9B)?

- MA Trend uses only the last 3 growth rates (recent strong momentum)
- Exponential Smoothing processes the full history with exponential decay
- The smoothed level reflects decades of older, slower growth pulling the estimate down

This is the expected behaviour — Exp Smoothing is more conservative than MA Trend for companies with recent acceleration.

---

## Edge cases

| Situation | What happens |
|-----------|-------------|
| Only 1 data point | `len(smoothed) = 1`, trend defaults to 0; forecast = flat at that value |
| 2 data points | `trend = smoothed[1] − smoothed[0]`; works but noisy |
| Very stable series (low variance) | Smoothed values close to actuals; trend close to 0; forecast is near flat |
| Series with a sharp recent reversal | Smoothed level lags the reversal (α=0.3 is slow); forecast misses the turn |

---

## Why this model can be useful

- Reduces the effect of any single noisy year
- Gives most influence to recent data without hard-coding a window size
- Often gives a calmer, more conservative path than MA Trend
- Standard in demand forecasting for consumer goods and retail operations

## Where it can struggle

- Sharp structural shifts (new business segment, post-merger integration)
- Needs at least 3 years for the smoothing to be meaningful
- α is not tuned per company — one unusual company might need a different α

## What to say when presenting

> "Exponential Smoothing builds a calmer version of the revenue history by giving more weight to recent years and less to older ones — the weight drops off exponentially the further back you go. It then extends the most recent smoothed direction as its forecast."
