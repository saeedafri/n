# 07 Holt's Linear Trend

## Full name and aliases

- **Holt's Linear Trend Method** (1957, Charles C. Holt)
- **Double Exponential Smoothing**
- **Two-parameter exponential smoothing**
- ETS classification: **ETS(A,A,N)** — Additive errors, Additive trend, No seasonality (Hyndman & Athanasopoulos, "Forecasting: Principles and Practice")

---

## Why "double" exponential smoothing?

Simple Exponential Smoothing (SES) applies one EWMA to the level only. Holt's method applies **two** EWMAs simultaneously:

1. One EWMA on the **level** (controlled by α)
2. One EWMA on the **trend** (controlled by β)

This is why it is called Double Exponential Smoothing — two smoothing operations run in parallel.

---

## Core assumption

This model believes:

> Revenue has both a current baseline position (level) and a current direction of change (trend). Both evolve over time. A model that adapts only the level but not the trend will systematically lag in series that are accelerating or decelerating.

Unlike Simple Exponential Smoothing (which computes a backward-difference trend from only the last two smoothed values), Holt's method keeps a **continuously updated trend state** that itself has exponential memory.

---

## The two parameters: α and β

```
α = 0.3   (level smoothing — fixed in code)
β = 0.1   (trend smoothing — fixed in code)
```

| Parameter | Controls | Effect of higher value | Effect of lower value |
|-----------|----------|----------------------|----------------------|
| α | How fast the level responds to new actuals | Level tracks actuals closely — more reactive | Level is smoother and slower to react |
| β | How fast the trend responds to level changes | Trend updates rapidly — sensitive to short-term shifts | Trend is slow to change — persistent momentum |

**Why α = 0.3:**
Same rationale as Exponential Smoothing — industry default for short annual series (5–15 data points). Maximum likelihood estimation tends to overfit on short series.

**Why β = 0.1:**
A deliberately low value. The trend state should be conservative — it should reflect sustained direction, not single-year noise. A β of 0.1 means only 10% of each new trend signal enters the trend state immediately; 90% is carried from the prior trend. This prevents the model from over-reacting to one strong or weak year and projecting it aggressively into the future.

---

## The mathematical equations

### Initialization (from first two data points)

```
L₁ = sales₁
T₁ = sales₂ − sales₁
```

This sets the initial level to the first actual and the initial trend to the first year-over-year change. After this point, both L and T evolve recursively.

### Level update equation

```
Lₜ = α × salesₜ + (1 − α) × (Lₜ₋₁ + Tₜ₋₁)
```

**Interpretation:**
- `α × salesₜ` — this year's actual, weighted by α
- `(1 − α) × (Lₜ₋₁ + Tₜ₋₁)` — last year's level **plus the expected trend increment**, carried forward

The term `Lₜ₋₁ + Tₜ₋₁` is the model's one-step-ahead prediction for this year. The level update is essentially a weighted blend of the actual and the prediction.

### Trend update equation

```
Tₜ = β × (Lₜ − Lₜ₋₁) + (1 − β) × Tₜ₋₁
```

**Interpretation:**
- `β × (Lₜ − Lₜ₋₁)` — the new backward difference of the smoothed level, weighted by β
- `(1 − β) × Tₜ₋₁` — the prior smoothed trend, carried forward

This is a second EWMA applied to the backward difference of the level series. The trend state is an exponentially weighted average of all past level differences, with recent ones weighted more.

### Forecast equation

```
forecast_h = L_last + h × T_last       for h = 1, 2, 3, 4, 5
```

This is a linear extrapolation from the final state. The final trend T_last is applied additively h steps into the future.

---

## ETS(A,A,N) formal classification

In the state-space ETS framework:

| Component | Type | Meaning |
|-----------|------|---------|
| Error | A (Additive) | Forecast errors enter additively |
| Trend | A (Additive) | Trend adds to the level (not multiplied) |
| Seasonality | N (None) | No seasonal component |

The state-space form of ETS(A,A,N):

```
yₜ = Lₜ₋₁ + Tₜ₋₁ + εₜ           (observation equation)
Lₜ = Lₜ₋₁ + Tₜ₋₁ + α × εₜ       (level state equation)
Tₜ = Tₜ₋₁ + α × β × εₜ           (trend state equation)
```

The parameters in this code are **fixed** (not estimated by maximum likelihood). In a fully specified ETS implementation, α and β would be jointly optimized by minimizing the log-likelihood of the state-space model. Fixed parameters are a deliberate simplification to avoid overfitting on short annual series.

---

## Exact Python code in retailer_forecaster.py

```python
def _forecast_holt(self, years, sales, periods, alpha=0.3, beta=0.1):
    level = sales[0]
    trend = sales[1] - sales[0] if len(sales) > 1 else 0
    for i in range(1, len(sales)):
        prev_level = level
        level = alpha * sales[i] + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return {
        "method": "Holt's Linear",
        "forecast": np.array([level + trend * i for i in range(1, periods + 1)]),
        "years": fy,
    }
```

**Walkthrough:**
- `level = sales[0]` — L₁ = first actual
- `trend = sales[1] - sales[0]` — T₁ = first difference
- Loop starts at `i=1` (not 0) — we already initialized from index 0
- `prev_level = level` — save Lₜ₋₁ before updating (needed for trend update)
- Level update: `alpha * sales[i] + (1 - alpha) * (level + trend)`
- Trend update: `beta * (level - prev_level) + (1 - beta) * trend`
- After the loop, `level` = L_last, `trend` = T_last
- Forecast: `[level + trend * i for i in range(1, periods + 1)]` = h=1,2,3,4,5

---

## Worked example — every step

| Year | Actual | Prev Level | Prev Trend | New Level | New Trend | Calculation |
|------|--------|-----------|-----------|-----------|-----------|-------------|
| 2020 | 10.0 | — | — | **10.000** | **2.000** | Init: L=10.0, T=12−10=2.0 |
| 2021 | 12.0 | 10.000 | 2.000 | **11.400** | **1.940** | L: 0.3×12 + 0.7×(10+2)=3.6+8.4; T: 0.1×(11.4−10)+0.9×2 |
| 2022 | 11.0 | 11.400 | 1.940 | **11.578** | **1.764** | L: 0.3×11 + 0.7×(11.4+1.94)=3.3+9.338; T: 0.1×(11.578−11.4)+0.9×1.94 |
| 2023 | 14.0 | 11.578 | 1.764 | **12.791** | **1.867** | L: 0.3×14 + 0.7×(11.578+1.764)=4.2+9.341; T: 0.1×(12.791−11.578)+0.9×1.764 |
| 2024 | 16.0 | 12.791 | 1.867 | **14.039** | **1.905** | L: 0.3×16 + 0.7×(12.791+1.867)=4.8+10.257; T: 0.1×(14.039−12.791)+0.9×1.867 |

**Using α = 0.3, β = 0.1, Initialization: L₁=10.0, T₁=2.0**

Step-by-step for 2021 (first update):
```
prev_level = 10.000
level = 0.3 × 12 + 0.7 × (10 + 2) = 3.6 + 8.4 = 11.400
trend = 0.1 × (11.400 − 10.000) + 0.9 × 2.000 = 0.14 + 1.8 = 1.940
```

**Final state after all training years:**
```
L_last = 14.039
T_last = 1.905
```

**Forecasts:**
```
2025: 14.039 + 1 × 1.905 = 15.944B
2026: 14.039 + 2 × 1.905 = 17.849B
2027: 14.039 + 3 × 1.905 = 19.754B
```

---

## Comparison: Holt's Linear Trend vs Exponential Smoothing

| Dimension | Exponential Smoothing | Holt's Linear Trend |
|-----------|----------------------|---------------------|
| State variables | 1 (level only) | 2 (level + trend) |
| Parameters | α only | α and β |
| Trend update | One backward difference from last 2 smoothed values | Continuously EWMA-smoothed trend state |
| Trend adapts? | No — frozen at the final backward difference | Yes — updates every year via β |
| ETS class | ETS(A,N,N) (simple); closer to ETS(A,A,N) with post-hoc trend | True ETS(A,A,N) |
| Reacts to momentum shift? | Poor — trend computed only from final step | Better — trend absorbs shift gradually via β |
| Better for | Stable series, mild noise | Series with changing momentum, medium-length history |

---

## Edge cases

| Situation | What happens |
|-----------|-------------|
| Only 1 data point | `trend = 0`; forecast is flat at that value |
| 2 data points | Level and trend are both initialized; exactly one loop iteration runs |
| Very noisy series | Level tracks well, but trend may oscillate; β=0.1 damps this significantly |
| Strong recent acceleration | Trend state adapts, but slowly (β=0.1); a few years of acceleration required before trend catches up |
| Declining series | Trend goes negative; forecasts decline linearly — Holt extrapolates in whatever direction the trend points |
| β = 1 (theoretical) | Trend = current backward difference only — equivalent to SES with a one-step trend |
| β = 0 (theoretical) | Trend is frozen at initialization (T₁) forever — same as SES with a constant linear increment |

---

## Why this model can be useful

- Adapts both the revenue baseline and the direction of change over time
- Strong performance on series that are changing shape (acceleration or deceleration)
- More responsive than Linear Regression to recent data
- More stable than MA Trend because both level and trend are smoothed

## Where it can struggle

- Very short series (< 4 points) — initialization dominates
- Sudden structural breaks — β=0.1 is slow; the trend state takes several years to absorb a reversal
- Very noisy data — both L and T absorb noise, and small β only partially dampens it

## What to say when presenting

> "Holt's method maintains two running states: where revenue is right now (level), and how fast it is changing (trend). Both states update every year using exponentially weighted averages. The level parameter α controls responsiveness to new actuals; the trend parameter β controls how quickly the model's trend estimate adapts to changes in direction. The forecast extrapolates the final level plus h steps of the final trend."
