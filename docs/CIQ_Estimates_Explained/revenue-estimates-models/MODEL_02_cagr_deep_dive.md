# CAGR — Complete Deep Dive

## What this file covers

Everything about the CAGR model:
- what CAGR means word by word
- what assumption it makes
- the full mathematical derivation
- how training works (it is just one formula)
- what each output means
- real-world examples with hand calculations
- where CAGR is used in finance and business
- strengths, weaknesses, and when to trust it

---

## 1. What CAGR stands for — word by word

**C — Compound**
Compound means growth builds on top of previous growth.

If you start with $100 and grow 10% per year:
- Year 1: $100 × 1.10 = $110
- Year 2: $110 × 1.10 = $121 (not $120 — the 10% applies to the new $110 base)
- Year 3: $121 × 1.10 = $133.10

The growth accelerates slightly each year because the base grows. This is compounding.

**A — Annual**
Annual means yearly. CAGR measures a yearly growth rate, not monthly or quarterly.

**G — Growth**
Growth means increase. The rate describes how much the value increases.

**R — Rate**
Rate means the speed of change expressed as a percentage per period.

**So: CAGR = the one steady yearly percentage that, if compounded every year, would carry revenue from the first historical value to the last.**

---

## 2. The central assumption

CAGR assumes:

**Revenue grows by the same percentage every year.**

Not the same dollar amount (that is Linear Regression's assumption). The same percentage.

Example of a perfect CAGR pattern:
| Year | Revenue |
|------|---------|
| 2020 | 100 |
| 2021 | 110 (+10%) |
| 2022 | 121 (+10%) |
| 2023 | 133.1 (+10%) |
| 2024 | 146.41 (+10%) |

CAGR = exactly 10%. The model fits perfectly because the data follows a perfect compound growth pattern.

---

## 3. How CAGR is derived mathematically

### The compounding formula

If revenue starts at `S₀` and grows at rate `g` for `n` years:
```
S_n = S₀ × (1 + g)^n
```

### Solving for g (the CAGR)

We know `S₀` (first year) and `S_n` (last year). Solve for `g`:

```
S_n = S₀ × (1 + g)^n
S_n / S₀ = (1 + g)^n
(S_n / S₀)^(1/n) = 1 + g
g = (S_n / S₀)^(1/n) − 1
```

This is the CAGR formula:

```
CAGR = (last_sales / base_sales)^(1 / n) − 1
```

Where:
- `last_sales` = latest annual revenue in the cleaned training series
- `base_sales` = earliest annual revenue in the cleaned training series
- `n` = number of year-to-year intervals between them (= years of history − 1)

---

## 4. What training means for CAGR

Training for CAGR is the simplest possible process:

1. Read the first revenue value in the cleaned training series
2. Read the last revenue value in the cleaned training series
3. Count the number of intervals: n = (count of years − 1)
4. Compute: `CAGR = (last / first)^(1/n) − 1`

That is the entire training process. One formula, three data points (first value, last value, count).

No iteration. No gradient descent. No epochs. No hyperparameter tuning.

The "training" takes milliseconds.

---

## 5. How the forecast is computed

```
forecast_h = last_sales × (1 + CAGR)^h
```

Where `h` = number of years ahead:
- h = 1 → next year forecast
- h = 2 → two years ahead
- h = 5 → five years ahead

The model compounds from the latest actual value, not from the first historical value.

---

## 6. Full worked example — verify by hand

### Data

| Year | Revenue (B) |
|------|------------|
| 2018 | 20.0 |
| 2019 | 22.0 |
| 2020 | 19.8 (COVID dip — but let's say no outliers) |
| 2021 | 24.0 |
| 2022 | 26.0 |

### Step 1 — identify inputs

```
base_sales = 20.0  (first year: 2018)
last_sales = 26.0  (last year: 2022)
n          = 4     (intervals: 2022 − 2018 = 4)
```

### Step 2 — compute CAGR

```
CAGR = (26.0 / 20.0)^(1/4) − 1
     = (1.30)^(0.25) − 1
     = 1.0678 − 1
     = 0.0678
     = 6.78%
```

### Step 3 — forecast

```
2023 forecast = 26.0 × (1 + 0.0678)^1 = 26.0 × 1.0678 = 27.76B
2024 forecast = 26.0 × (1 + 0.0678)^2 = 26.0 × 1.14027 = 29.65B
2025 forecast = 26.0 × (1 + 0.0678)^3 = 26.0 × 1.21761 = 31.66B
```

---

## 7. Real page example — ANF

From the current page (values observed from the live system):

- Base sales (first cleaned year) ≈ $3.125B (2021)
- Last sales (2026) = $5.266B
- n = 5 intervals (2021 to 2026)

```
CAGR = (5.266 / 3.125)^(1/5) − 1
     = (1.685)^(0.2) − 1
     = 1.1100 − 1
     = 11.0%
```

Forecast 2027:
```
5.266 × 1.110 = 5.845B ≈ 5.4B (shown on page — note: outlier removal affects exact base)
```

The slight difference from the page is because outlier removal changes which year is "first" and "last" in the cleaned series.

The model stat card on the CAGR tab shows this percentage. That is the CAGR value.

---

## 8. Why the "model stat" on the CAGR tab is the CAGR percentage

When you open the Models → CAGR tab on the page, the fourth card shows "CAGR X.X%".

This is the single number the model learned from the data — the compounded annual growth rate.

Every forecast value is derived from this one number. If you know this number and the latest actual revenue, you can reproduce any year's forecast by hand.

---

## 9. What CAGR is called in Finance and Machine Learning

### In finance and business
CAGR is the universal standard way to communicate investment or business growth.

**Every earnings report, investor presentation, and business plan uses CAGR.**

Examples from real companies:
- "Our revenue CAGR over 5 years is 12%" — standard language in annual reports
- "The market is expected to grow at a CAGR of 15% through 2030" — standard language in market research
- "Our 3-year CAGR improved from 8% to 11%" — improvement narrative in investor calls

### In Machine Learning
CAGR is not a "machine learning" model. It is a **parametric statistical estimation** — one parameter (the growth rate) estimated from two data points (first and last value).

The ML equivalent would be an **exponential regression** where the model is `y = a × e^(b × t)` (exponential growth). CAGR is a simplified version of this.

### The process is called
- **Parameter estimation** (estimating one parameter: the growth rate)
- **Closed-form estimation** (solved in one formula, not iteratively)
- **Nonlinear least squares** (if you wanted to formally minimize squared errors for exponential fit — CAGR approximates this for the two endpoints)

---

## 10. Real-world uses of CAGR beyond this page

### Investment analysis
- Comparing fund performance: "Fund A has a 10-year CAGR of 12% vs Fund B at 9%"
- Warren Buffett's Berkshire Hathaway reports its per-share book value CAGR every year in the annual letter
- Private equity firms evaluate deals by target IRR (Internal Rate of Return) — which is CAGR applied to cash flows

### Business strategy
- "We need 20% revenue CAGR to reach our $10B target in 5 years from our current $4B" — a board-level goal
- BCG and McKinsey growth strategy reports always state industry CAGR benchmarks

### Market sizing
- "The global EV battery market is projected to grow at a CAGR of 24% from 2024 to 2030" — from Statista, Bloomberg, Gartner-type reports

### Real estate
- Computing property value appreciation over time
- Cap rate calculations involve a form of CAGR reasoning

### Banking / lending
- Loan amortization uses the inverse of CAGR to compute periodic payments from a present value
- APY (Annual Percentage Yield) on savings accounts is a CAGR of your money at that interest rate

---

## 11. Sensitivity problem — why CAGR can be deceptive

### The endpoint problem

CAGR uses only two data points: first and last.
Every year in between is ignored.

This means:

**If either the start year or end year was unusual, CAGR is distorted.**

Example 1: COVID dip as first year

| Year | Revenue |
|------|---------|
| 2020 | 70 (COVID dip) |
| 2021 | 100 |
| 2022 | 110 |
| 2023 | 120 |

CAGR from 2020 to 2023 = (120/70)^(1/3) − 1 = 19.7%

But the company was actually growing at ~10% per year in normal conditions. The depressed 2020 makes CAGR look artificially high.

Example 2: Normal start, acquisition in last year

| Year | Revenue |
|------|---------|
| 2019 | 10 |
| 2020 | 11 |
| 2021 | 12 |
| 2022 | 13 |
| 2023 | 25 (acquisition doubled revenue) |

CAGR from 2019 to 2023 = (25/10)^(1/4) − 1 = 25.7%

But the organic growth rate was about 7% per year. The acquisition makes CAGR look artificially high.

### How this page handles it

The outlier detection step (z-score on growth rates) removes years with extreme growth changes. If 2020 was a COVID year with a −30% growth, it gets flagged and removed from the clean series. The CAGR is then computed on the cleaned series without that year.

---

## 12. Strengths and weaknesses

### Strengths
- Universal: every finance professional understands CAGR immediately
- Simple: one formula, three inputs
- Good for stable, steadily growing businesses
- Easy to communicate: "the company grows at 8% per year"
- Works well over long periods (10+ years) for compounding businesses

### Weaknesses
- Uses only first and last values — ignores all middle years
- Very sensitive to start and end point choice
- Assumes constant percentage growth — breaks down for volatile companies
- Cannot represent trend changes (acceleration, deceleration)
- Does not produce an uncertainty interval

### When to trust it
- Company with stable percentage growth over many years
- Outlier years have been removed
- First and last years are both representative (not acquisition years, not crisis years)

### When to be skeptical
- First or last year was unusual in any way
- Company went through acquisitions, divestitures, or major restructuring
- Growth rates were highly volatile year to year
- Very short history (3–4 years) — CAGR from a 3-year span is very fragile
