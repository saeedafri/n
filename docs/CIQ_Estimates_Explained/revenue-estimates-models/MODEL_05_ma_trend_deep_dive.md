# Moving Average Trend — Complete Deep Dive

## What this file covers

Everything about the Moving Average Trend model:
- what a moving average is and why it is useful
- what assumption this model makes
- the full step-by-step computation
- what training means for this model
- a complete worked example you can verify in 60 seconds
- where moving averages are used in the real world
- why this model often wins for companies with strong recent momentum

---

## 1. What a moving average is

### The simplest possible explanation

A moving average replaces each value in a sequence with the average of a window of recent values.

Example: stock price moving average

| Day | Price | 3-day MA |
|-----|-------|----------|
| 1 | 100 | — |
| 2 | 105 | — |
| 3 | 98 | (100+105+98)/3 = 101 |
| 4 | 110 | (105+98+110)/3 = 104.3 |
| 5 | 108 | (98+110+108)/3 = 105.3 |

The 3-day moving average smooths out the day-to-day noise. You see the trend more clearly.

The window "moves" because as each new value arrives, the window shifts forward by one step, dropping the oldest value and adding the newest.

---

## 2. How this model applies the moving average idea

On this page, the moving average is NOT applied to revenue values directly.

It is applied to **year-over-year growth rates**.

This is more powerful because:
- Averaging growth rates tells you the typical recent growth pace
- Compounding that average pace forward gives the revenue forecast
- The model explicitly captures momentum (recent growth) rather than level

---

## 3. The exact assumption this model makes

**The average of the last 3 year-over-year growth rates is the best predictor of next year's growth.**

This is the "momentum" assumption:
- If a company has been growing at 10–15% in the last 3 years, assume it keeps growing near that pace
- Old history (4+ years ago) is ignored entirely
- The 3-year window is chosen to balance: too short (1 year) is too noisy; too long uses outdated rates

---

## 4. The step-by-step computation

### Step 1 — compute year-over-year growth rates

For each consecutive pair of years in the cleaned training series:

```
growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁
```

Example:
- sales_2022 = 100, sales_2023 = 112 → growth = (112−100)/100 = 0.12 = 12%
- sales_2023 = 112, sales_2024 = 125 → growth = (125−112)/112 = 0.116 = 11.6%

### Step 2 — select the last 3 (or fewer if history is short)

```
if len(growth_rates) >= window:
    recent_growth = growth_rates[-window:]
else:
    recent_growth = growth_rates  # use all if fewer than window
```

Window = 3 (fixed in this codebase).

### Step 3 — average the selected growth rates

```
avg_growth = mean(recent_growth)
```

This one number is what the model "learned" from the data.

### Step 4 — compound forward from the latest actual revenue

```
forecast_1 = last_sales × (1 + avg_growth)
forecast_2 = forecast_1 × (1 + avg_growth)
forecast_3 = forecast_2 × (1 + avg_growth)
...
forecast_h = last_sales × (1 + avg_growth)^h
```

The growth rate is applied repeatedly. Each year's revenue becomes the base for the next year.

---

## 5. What training means for Moving Average Trend

Training is the simplest of all six models:

1. Compute all year-over-year growth rates
2. Take the last 3
3. Average them

That is the entire training process. Three numbers go in, one number comes out (avg_growth).

No iteration, no recursion, no parameters to tune. The computation takes microseconds.

---

## 6. Full worked example — the most verifiable model on the page

### Data (using ANF real values from the page)

| Year | Revenue (B) | Growth |
|------|------------|--------|
| 2021 | 3.125 | — |
| 2022 | 3.713 | 18.8% |
| 2023 | 3.698 | −0.4% |
| 2024 | 4.281 | 15.8% |
| 2025 | 4.949 | 15.6% |
| 2026 | 5.266 | 6.4% |

### Step 1 — compute growth rates

Already done above (last column).

### Step 2 — select last 3

```
window = 3
recent_3_growth = [15.8%, 15.6%, 6.4%]
```

(Using the last 3 values: 2024, 2025, 2026 growth)

### Step 3 — average

```
avg_growth = (15.8 + 15.6 + 6.4) / 3 = 37.8 / 3 = 12.6%
```

### Step 4 — forecast

Latest actual = 5.266B

```
2027 = 5.266 × (1 + 0.126) = 5.266 × 1.126 = 5.930B ≈ 5.9B
2028 = 5.930 × 1.126 = 6.677B ≈ 6.7B
2029 = 6.677 × 1.126 = 7.518B ≈ 7.5B
```

**The 5.9B matches what the page shows for ANF's 2027 MA Trend forecast.**

This is the most transparent model on the page — you can reproduce any forecast in 60 seconds using only the last 3 growth rates and a calculator.

---

## 7. Why this model often wins the backtest

Moving Average Trend wins when:

**The last 3 growth rates closely represent what the next 2 years also looked like.**

In the backtest, the engine:
1. Trains on everything except the last 2 years
2. Uses the last 3 growth rates of that training set
3. Predicts the 2 holdout years

If the company had consistent momentum (e.g., growing at 12–16% consistently through the training years), the 3-year average from the training set will closely predict the holdout years. MAPE will be very low.

**For ANF:** The company's growth was strong and consistent in 2022–2024 (the training period). The 3-year average growth captured this well. MA Trend predicted the holdout years (2025 and 2026) within about 2% accuracy → MAPE 2.25%.

---

## 8. What this model is called in Data Science / Time Series

### Formal names
- Moving Average Trend (as used on this page)
- Simple Moving Average (SMA) applied to growth rates
- Rolling Mean Projection
- Naive momentum forecasting

Note: **do not confuse with MA in ARIMA**.

In ARIMA notation, "MA" stands for "Moving Average of errors" (a very different concept). In ARIMA(p, d, q), the q parameter controls the MA component, which averages past forecast errors.

On this page, MA Trend means: take a moving average of past growth rates.

### In technical analysis (finance)
The 50-day and 200-day Simple Moving Average (SMA) of stock prices is the same mathematical idea applied to daily price data.

When the 50-day SMA crosses above the 200-day SMA, technical traders call this a "golden cross" and interpret it as a bullish signal. When it crosses below, it is a "death cross."

This page applies the same concept to annual revenue growth rates.

### In ML terms
- **Univariate time series forecasting**
- **Baseline / benchmark model** — often compared against in more sophisticated forecasting research
- **Non-parametric** — the model has no learned parameters per se, just a window size

---

## 9. Real-world uses of moving averages

### Finance and trading
- The SMA and EMA (Exponentially Weighted Moving Average) are the most widely used tools in technical stock analysis
- Bloomberg and every trading terminal displays moving averages by default
- Algorithmic trading strategies (momentum strategies) buy stocks trading above their 200-day SMA

### Demand forecasting in retail
- Walmart uses moving averages of recent weeks' sales as a baseline forecast for individual products
- The window is usually 4–13 weeks depending on product type
- Simple, fast, and surprisingly accurate for fast-moving consumer goods with stable demand

### Economic indicators
- The BLS (Bureau of Labor Statistics) reports 3-month moving average unemployment to smooth month-to-month noise
- GDP growth reports often show 4-quarter moving averages

### Quality control
- Control charts in manufacturing use moving averages to detect when a process is drifting out of spec
- 7-point or 9-point moving averages are common in SPC (Statistical Process Control)

### Sports analytics
- Expected points in NFL play-calling models use moving averages of recent performance
- Pitcher ERA+ uses rolling 30-day stats

### This page
- Applied to 7–15 annual company revenue growth rates, using the most recent 3, to project 5 years forward

---

## 10. The window size trade-off

| Window | Behavior |
|--------|----------|
| 1 | Just uses last year's growth rate. Very noisy. Extremely reactive. |
| 2 | Uses last 2 growth rates. Still reactive. |
| 3 | Uses last 3 growth rates. (Default on this page.) Good balance. |
| 5 | Uses last 5 growth rates. More stable. Less reactive to recent change. |
| All history | Equivalent to simple mean growth rate. Ignores recent momentum entirely. |

With a window of 3 (as on this page):
- If growth was 20%, 20%, 20%: avg = 20% (strong momentum)
- If growth was 20%, 20%, 5%: avg = 15% (one slower year pulls it down)
- If growth was 20%, 20%, −5%: avg = 11.7% (one dip year significantly reduces the forecast)

This is why MA Trend can be volatile — one bad year in the last 3 meaningfully reduces the forecast.

---

## 11. Strengths and weaknesses

### Strengths
- Completely transparent: the forecast can be verified by hand in under a minute
- Captures recent momentum directly
- Ignores stale history that may no longer be relevant
- Very fast to compute
- Intuitive to explain to any audience
- Often very accurate for companies with consistent recent growth

### Weaknesses
- Sensitive to one unusual year in the last 3 (e.g., a dip year pulls the forecast down meaningfully)
- Ignores everything older than 3 years
- Projects one growth rate forward indefinitely (no dampening)
- Not suited for companies where long-run history matters
- If recent years were exceptional (post-COVID rebound), the model may be overly optimistic

### When to trust it
- Company has shown consistent growth momentum in the last 3–5 years
- The growth rates in the last 3 years are representative of the future (no special events)
- You want the most recent-momentum-driven forecast

### When to be skeptical
- One of the last 3 years was unusually strong or weak for external reasons
- Company is going through a transition that makes recent rates unrepresentative
- History before the 3-year window shows a very different growth pattern (mean reversion expected)
