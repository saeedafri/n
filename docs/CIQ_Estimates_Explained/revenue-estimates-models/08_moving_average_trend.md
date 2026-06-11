# 08 Moving Average Trend

## Full name and aliases

- **Moving Average Trend (MA Trend)**
- **Simple Moving Average of Growth Rates**
- Not the same as "price moving average" in technical analysis — this is applied to **year-over-year revenue growth rates**, not price levels

---

## Core assumption

This model believes:

> The most recent growth pattern is the best predictor of near-term future growth. Older years are irrelevant — they reflect a different business environment. Only the last 3 annual growth rates matter.

This is a deliberate recency bias — intentional and strong. Unlike Exponential Smoothing (which discounts old data gradually), MA Trend makes a hard cut: everything beyond 3 years ago is ignored entirely.

---

## The key parameter: window

```
window = 3   (fixed in code — not tuned per company)
```

**Why 3 and not 2 or 4?**

| Window | Behavior |
|--------|----------|
| window = 1 | Naïve forecast — next year = this year's growth rate. Extremely volatile. |
| window = 2 | Slightly smoother but still sensitive to one outlier year |
| window = 3 | Balances recency with enough data points to average out a single unusual year |
| window = 4 | Starts to pull in older data that may no longer be representative |
| window = 5+ | Too much history; defeats the purpose of emphasizing recent momentum |

The 3-year window is a standard in retail demand planning and is consistent with how equity analysts typically describe "recent growth trajectory" in earnings reports.

**Short series fallback:** If the cleaned revenue series has fewer than 4 data points, there are fewer than 3 growth rates available. The code uses however many growth rates it has (minimum 1).

---

## The mathematical formula

**Step 1 — Compute year-over-year growth rates:**

```
gₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁       for t = 2, 3, ..., n
```

This produces n−1 growth rates from n revenue observations.

**Step 2 — Take the simple arithmetic mean of the last `window` growth rates:**

```
ḡ = (1/window) × Σ gₜ    for the last `window` values of t
```

With window = 3:
```
ḡ = (gₙ₋₂ + gₙ₋₁ + gₙ) / 3
```

**Step 3 — Compound forward from the last actual:**

```
forecast_h = salesₙ × (1 + ḡ)^h       for h = 1, 2, 3, 4, 5
```

Note: this is **compounding**, not additive. Each future year builds on the previous forecast, not on the base sales directly.

---

## Exact Python code in retailer_forecaster.py

```python
def _forecast_ma_trend(self, years, sales, periods, window=3):
    growth_rates = np.diff(sales) / sales[:-1]          # Step 1: YoY growth rates
    window = min(window, len(growth_rates))              # handle short series
    avg_growth = np.mean(growth_rates[-window:])         # Step 2: last 3 rates, simple mean
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return {
        "method": "MA Trend",
        "forecast": np.array([sales[-1] * (1 + avg_growth) ** i for i in range(1, periods + 1)]),
        "years": fy,
    }
```

**Line-by-line explanation:**

- `np.diff(sales)` — computes `[sales[1]-sales[0], sales[2]-sales[1], ...]` — array of absolute year-over-year changes
- `/ sales[:-1]` — divides each change by the prior year's revenue to get the percentage growth rate; `sales[:-1]` is all elements except the last (the denominators)
- `growth_rates[-window:]` — Python slice: last `window` elements of the growth_rates array
- `np.mean(...)` — simple arithmetic mean (not weighted)
- `(1 + avg_growth) ** i` — Python `**` is exponentiation; this is the compounding formula

---

## np.diff() internal mechanics

`np.diff(a)` computes first-order differences:

```
np.diff([10, 12, 11, 14, 16])
→ [12-10, 11-12, 14-11, 16-14]
→ [2, -1, 3, 2]
```

Dividing by `sales[:-1]` = `[10, 12, 11, 14]`:
```
growth_rates = [2/10, -1/12, 3/11, 2/14]
             = [0.200, -0.083, 0.273, 0.143]
```

If `window=3`, the last 3 growth rates are `[-0.083, 0.273, 0.143]`.
```
avg_growth = (-0.083 + 0.273 + 0.143) / 3 = 0.333/3 = 0.111
```

---

## Worked example — step by step

| Year | Revenue ($B) | Growth Rate gₜ |
|------|-------------|---------------|
| 2019 | 3.0 | — |
| 2020 | 2.7 | (2.7−3.0)/3.0 = −10.0% |
| 2021 | 3.2 | (3.2−2.7)/2.7 = +18.5% |
| 2022 | 3.8 | (3.8−3.2)/3.2 = +18.8% |
| 2023 | 4.5 | (4.5−3.8)/3.8 = +18.4% |
| 2024 | 5.0 | (5.0−4.5)/4.5 = +11.1% |

Last 3 growth rates (2022–2024): `[+18.8%, +18.4%, +11.1%]`

```
ḡ = (0.188 + 0.184 + 0.111) / 3 = 0.483 / 3 = 0.161 = 16.1%
```

**Forecasts from last actual (5.0B):**
```
2025: 5.0 × (1.161)¹ = 5.805B
2026: 5.0 × (1.161)² = 6.739B
2027: 5.0 × (1.161)³ = 7.824B
```

Notice: the 2020 dip (−10.0%) is completely outside the 3-year window and has **zero influence** on the forecast.

---

## Real page example: ANF verification

| Year | ANF Revenue Growth |
|------|------------------|
| 2024 | +15.8% |
| 2025 | +15.6% |
| 2026 | +6.4% |

```
ḡ = (15.8 + 15.6 + 6.4) / 3 = 37.8 / 3 = 12.6%

2027 forecast = 5.266B × (1.126)¹ = 5.93B ≈ 5.9B  ✓
2028 forecast = 5.266B × (1.126)² = 6.68B
```

This model is the **most verifiable by hand** on the page — three recent percentages, average them, apply once.

---

## Stationarity assumption

MA Trend implicitly assumes that the growth rate process is **stationary over the 3-year window** — meaning the average growth rate is not itself trending up or down within those 3 years. If growth rates are themselves accelerating (5%, 8%, 12%), MA Trend averages them at 8.3% — it doesn't project that the acceleration will continue. For that, Holt's method would be more appropriate.

---

## MA Trend vs other models

| Model | Uses how many years? | Weights | Compounds? |
|-------|---------------------|---------|-----------|
| MA Trend | Last 3 only | Equal | Yes |
| Weighted Average Growth | All years | Linearly increasing | Yes |
| CAGR | First and last only | N/A (only endpoints) | Yes |
| Exponential Smoothing | All years (with decay) | Exponential (recent > old) | No — linear trend |
| Holt's Linear Trend | All years (two EWMAs) | Exponential on both level and trend | No — linear trend |

---

## Edge cases

| Situation | What happens |
|-----------|-------------|
| Only 2 data points | 1 growth rate available; window collapses to 1; forecast = last_sales × (1+g) |
| 3 data points | 2 growth rates; window collapses to 2 |
| 4+ data points | Full window=3 activates |
| Recent growth rate is negative | ḡ < 0; all forecasts decline — model projects contraction |
| One year was extreme (e.g. COVID dip inside window) | That single year distorts ḡ significantly; outlier removal in Stage 2 may have excluded it before this calculation |
| Very high recent growth (post-merger spike) | MA Trend projects that growth rate forward indefinitely — optimistic and possibly unrealistic for 5-year horizon |

---

## Why this model can be useful

- Extremely simple to verify and explain to any audience
- Reacts quickly to genuine momentum changes
- Natural fit when analyst consensus is that recent trajectory is the best signal
- Standard in retail forecasting and sell-side consensus modeling

## Where it can struggle

- If recent growth was driven by a one-time event (store openings, post-COVID recovery), projecting it forward is misleading
- Only 3 years of data — a single outlier inside the window heavily influences the output
- Produces overconfident forecasts at 5-year horizon (compounding a recent rate far into the future)

## What to say when presenting

> "MA Trend takes the last 3 annual growth rates, averages them equally, and compounds that average rate forward from the latest revenue. It gives zero weight to anything before the 3-year window. It is the model that reacts most aggressively to recent momentum."
