# 03 Outlier Detection

> **To see all formulas rendered beautifully** — open this file in VS Code, press `Cmd+Shift+P`, type **"Markdown Preview Enhanced: Open Preview"** and select it. All the math below will render as proper typeset equations. Alternatively install the **"Markdown+Math"** extension (search `goessner.mdmath` in VS Code extensions).

---

## What this stage does and why it exists

Before any forecasting model runs, the engine scans every year in the revenue history looking for **abnormal years** — years where growth was so unusual that it would distort all downstream calculations if included.

The engine does **not delete** those years. They stay visible in charts so analysts see the full picture. But they are **excluded from model fitting** — the slope, growth rates, smoothed levels, and all other parameters are computed without them.

**Why this matters:** A single COVID year with −40% revenue can pull every model's average growth rate negative. The z-score test detects this automatically, regardless of company size or growth level.

---

## The four steps — overview

```
Step 1 → Compute year-over-year growth rate for every year
Step 2 → Compute the mean (average) growth rate
Step 3 → Compute the standard deviation of growth rates
Step 4 → Compute z-score for each year → flag if |z| > 2.0
```

Each step is explained fully below with the formula, every term defined, and the exact ANF numbers.

---

## Step 1 — Year-over-year growth rate

**What it is:** For each year, how much did revenue change compared to the previous year, expressed as a fraction.

$$g_t = \frac{\text{sales}_t \;-\; \text{sales}_{t-1}}{\text{sales}_{t-1}}$$

**Every term explained:**

| Symbol | Full name | Meaning |
|--------|-----------|---------|
| $g_t$ | **Growth rate at time t** | The fractional revenue change in year t. 0.10 = grew 10%. −0.20 = fell 20%. |
| $\text{sales}_t$ | **Revenue in current year** | Actual reported annual revenue for year t ($B) |
| $\text{sales}_{t-1}$ | **Revenue in prior year** | Actual reported annual revenue for the year before t |
| $t$ | **Time index** | Which year we are computing. The first year has no prior year, so $g_1$ does not exist. |

**In plain English:** Subtract last year's revenue from this year's revenue, then divide by last year's revenue. A result of 0.15 means revenue grew 15% that year.

**In code:** `df["growth"] = df["sales"].pct_change()` — pandas `pct_change()` does exactly this for every row. The first row gets `NaN` because there is no year before it.

---

## Step 2 — The Mean (μ)

**What it is:** The arithmetic average of all the growth rates. It tells you: on average, how fast did this company grow per year across its entire history?

$$\mu = \frac{1}{n} \sum_{t=2}^{n} g_t$$

**Read aloud as:** "mu equals one over n, times the sum of all growth rates from t equals 2 to n."

**Every term explained:**

| Symbol | Full name | Meaning |
|--------|-----------|---------|
| $\mu$ | **Mu — the population mean** | The Greek letter for "average." Standard notation across all of statistics and ML. |
| $\frac{1}{n}$ | **One divided by n** | Dividing by n gives the average. If you have 10 growth rates, divide their sum by 10. |
| $n$ | **Number of growth rates** | Total count of valid growth rates. If you have 21 revenue years, you have 20 growth rates (first year has none). |
| $\sum$ | **Sigma — summation** | "Add up everything that follows." The Greek capital letter sigma. |
| $g_t$ | **Growth rate in year t** | Each individual year's growth rate being added up. |

**In plain English:** Add up all the growth rates, then divide by how many there are. That is the average growth rate.

**In code:** `mean_g = df["growth"].mean()` — pandas `.mean()` automatically skips the first-year `NaN`.

---

## Step 3 — Standard Deviation (σ)

This is the most important concept in outlier detection. It is built in two sub-steps: variance first, then standard deviation.

---

### Sub-step 3a — What is Variance?

**Variance measures how spread out the growth rates are around the mean.**

A company with growth rates of 5%, 5%, 5%, 5% has zero variance — all identical.
A company with growth rates of −30%, +50%, −20%, +40% has very high variance — wildly inconsistent.

**There are two versions of the variance formula. They differ by one number: n vs n−1.**

---

#### Version 1: Population Variance $\sigma^2_{pop}$ — divide by n

$$\sigma^2_{pop} = \frac{1}{n} \sum_{t=2}^{n} (g_t - \mu)^2$$

**Full form with every term named:**

$$\underbrace{\sigma^2_{pop}}_{\substack{\text{Population} \\ \text{Variance}}} = \frac{1}{\underbrace{n}_{\text{Count}}} \sum_{t=2}^{n} \left(\underbrace{g_t}_{\text{Growth rate}} - \underbrace{\mu}_{\text{Mean}}\right)^{\underbrace{2}_{\text{Squared}}}$$

**In words:**
- For each year: take its growth rate, subtract the mean, square the result
- Add all those squared differences together
- Divide by n (the number of growth rates)

**Why square it?**
If you just subtracted the mean without squaring: positive and negative deviations would cancel each other out and the sum would be close to zero — giving you no information about spread. Squaring makes everything positive and amplifies larger deviations more than smaller ones.

**Use this version only when:** You have data for the *entire* population — every single year that will ever exist for this company. In practice, that is never true. We only have a sample.

---

#### Version 2: Sample Variance $\sigma^2_{s}$ — divide by (n−1)

$$\sigma^2_{s} = \frac{1}{n-1} \sum_{t=2}^{n} (g_t - \mu)^2$$

**Full form with every term named:**

$$\underbrace{\sigma^2_{s}}_{\substack{\text{Sample} \\ \text{Variance}}} = \frac{1}{\underbrace{n-1}_{\substack{\text{Degrees of} \\ \text{Freedom}}}} \sum_{t=2}^{n} \left(\underbrace{g_t}_{\text{Growth rate}} - \underbrace{\mu}_{\text{Mean}}\right)^{\underbrace{2}_{\text{Squared}}}$$

**This is what the code uses.** The only difference from population variance is dividing by **n−1** instead of **n**.

---

### The big question: WHY n−1 and not n?

This is called **Bessel's Correction**, named after the German mathematician Friedrich Bessel.

**The intuition in plain English:**

When you compute $\mu$ (the mean) from your sample, you are already using the data to estimate something. The mean you computed is the *sample mean* — it is pulled toward the data. Every deviation $(g_t - \mu)$ is measured from this estimated mean, not the true unknown mean.

Because the sample mean is calculated from the same data, the deviations $(g_t - \mu)$ are systematically a little bit *too small*. The sample mean sits in the "middle" of the data by construction, so the deviations are as small as possible. Dividing by n would therefore slightly *underestimate* the true spread.

Dividing by n−1 corrects for this bias — it inflates the estimate just enough to compensate for the systematic underestimation.

**The degrees of freedom explanation:**

You have n data points but you used 1 degree of freedom to compute $\mu$. After computing the mean, only n−1 data points are "free to vary independently" — the last point is determined by the others (because they must average to $\mu$). So the effective sample size for estimating variance is n−1, not n.

**ddof = 1:**

`ddof` stands for **"Delta Degrees Of Freedom"** — the number subtracted from n in the denominator.

- `ddof=0` → divide by n → population variance (biased for samples)
- `ddof=1` → divide by n−1 → sample variance (unbiased for samples) ← **what pandas uses by default**

In pandas: `df["growth"].std()` uses `ddof=1` by default. This is the statistically correct choice whenever you have a sample (which is always the case with historical revenue data).

---

### Sub-step 3b — Standard Deviation $\sigma$

**Variance is in units of (growth rate)². That is not useful — you cannot compare a variance to a growth rate directly.**

Standard deviation fixes this by taking the square root:

$$\sigma = \sqrt{\sigma^2_{s}} = \sqrt{\frac{1}{n-1} \sum_{t=2}^{n} (g_t - \mu)^2}$$

**Full form with every term named:**

$$\underbrace{\sigma}_{\substack{\text{Standard} \\ \text{Deviation}}} = \sqrt{\frac{1}{\underbrace{n-1}_{\substack{\text{Degrees of} \\ \text{Freedom}}}} \;\sum_{t=2}^{n} \left(\underbrace{g_t}_{\substack{\text{Growth} \\ \text{rate}}}\; - \;\underbrace{\mu}_{\text{Mean}}\right)^2}$$

**Standard deviation is now in the same units as the growth rates** — it is a percentage. If $\sigma = 0.098$, that means a typical year's growth rate deviates from the mean by roughly ±9.8 percentage points.

**In code:** `std_g = df["growth"].std()` — this is the sample standard deviation with ddof=1.

---

## Step 4 — Z-Score

**What it is:** The z-score converts each year's growth rate into a standardised, unit-free number that says: "how many standard deviations away from the mean is this year?"

$$z_t = \frac{g_t - \mu}{\sigma}$$

**Full form with every term named:**

$$\underbrace{z_t}_{\substack{\text{Z-score} \\ \text{for year t}}} = \frac{\overbrace{g_t}^{\text{Growth rate}} \;-\; \overbrace{\mu}^{\text{Mean}}}{\underbrace{\sigma}_{\text{Std. Dev.}}}$$

**Every term:**

| Symbol | Full name | Meaning |
|--------|-----------|---------|
| $z_t$ | **Z-score for year t** | How many standard deviations year t is from the mean. No units — just a distance measure. |
| $g_t - \mu$ | **Deviation from mean** | How far this year's growth rate is above or below average. Positive = above average. Negative = below average. |
| $\sigma$ | **Standard deviation** | The "ruler" we divide by to normalise the deviation. Converts absolute difference into a relative one. |

**Z-score interpretation table:**

| Z-score | Meaning |
|---------|---------|
| $z = 0$ | Exactly average growth — perfectly normal |
| $z = +1$ | One σ above average — mildly high |
| $z = -1$ | One σ below average — mildly low |
| $z = +2$ | Two σ above average — the system flags this |
| $z = -2$ | Two σ below average — the system flags this |
| $\|z\| > 2$ | **Outlier — excluded from model fitting** |

**In code:**
```python
df["z_score"] = (df["growth"] - mean_g) / std_g
self.outliers = df.loc[df["z_score"].abs() > threshold, "year"].tolist()
```

---

## Why the threshold is |z| > 2.0

Under a **normal (Gaussian) distribution**, the probability that any observation falls beyond ±2 standard deviations is only **4.6%** — about 1 in 22 years.

If a year's growth falls more than 2σ from the mean, it is the kind of thing that statistically only happens about once in two decades under normal business conditions. That is a strong signal that something extraordinary happened — a pandemic, a bankruptcy, a merger, an accounting restatement — rather than ordinary business noise.

The threshold 2.0 is a fixed, deliberate design choice matching the statistical industry convention for anomaly detection on annual financial series.

---

## Full Python code with every line explained

```python
def _detect_outliers(self, threshold: float = 2.0) -> None:
    df = self.clean_data.copy()

    # Step 1: Year-over-year growth rate
    # pct_change() computes (salesₜ - salesₜ₋₁) / salesₜ₋₁ for every row
    # First row becomes NaN (no prior year)
    df["growth"] = df["sales"].pct_change()

    # Step 2: Mean of all growth rates (NaN row is automatically skipped)
    mean_g = df["growth"].mean()

    # Step 3: Sample std dev — pandas default is ddof=1 (Bessel's correction)
    std_g = df["growth"].std()

    # Step 4: Z-score for each year, then flag any |z| > 2.0
    if std_g > 0:   # guard: if all growth rates are identical, std=0, skip
        df["z_score"] = (df["growth"] - mean_g) / std_g
        self.outliers = df.loc[df["z_score"].abs() > threshold, "year"].tolist()

    # Keep a clean dataset with outlier years removed (used by all models)
    self.data_excluding_outliers = self.clean_data[
        ~self.clean_data["year"].isin(self.outliers)
    ].copy()
```

---

## Real ANF Example — every number computed by hand

ANF (Abercrombie & Fitch) has 21 revenue years (2006–2026). Below is the 2016–2026 window — 11 revenue years producing 10 growth rates.

---

### Step 1 result — growth rates computed

| Year | Revenue ($B) | Growth $g_t$ | Calculation |
|------|-------------|-------------|-------------|
| 2016 | 3.519 | — | First year — no prior year, NaN |
| 2017 | 3.493 | **−0.0074** | (3.493 − 3.519) / 3.519 |
| 2018 | 3.590 | **+0.0278** | (3.590 − 3.493) / 3.493 |
| 2019 | 3.624 | **+0.0095** | (3.624 − 3.590) / 3.590 |
| 2020 | 3.623 | **−0.0003** | (3.623 − 3.624) / 3.624 |
| 2021 | 3.129 | **−0.1363** | (3.129 − 3.623) / 3.623 — COVID year |
| 2022 | 3.712 | **+0.1863** | (3.712 − 3.129) / 3.129 — recovery |
| 2023 | 3.697 | **−0.0040** | (3.697 − 3.712) / 3.712 |
| 2024 | 4.281 | **+0.1579** | (4.281 − 3.697) / 3.697 |
| 2025 | 4.950 | **+0.1562** | (4.950 − 4.281) / 4.281 |
| 2026 | 5.266 | **+0.0638** | (5.266 − 4.950) / 4.950 |

10 valid growth rates. The 2016 NaN is skipped in all calculations below.

---

### Step 2 — Compute the mean μ

$$\mu = \frac{1}{10} \times \sum g_t$$

$$\mu = \frac{-0.0074 + 0.0278 + 0.0095 + (-0.0003) + (-0.1363) + 0.1863 + (-0.0040) + 0.1579 + 0.1562 + 0.0638}{10}$$

$$\mu = \frac{0.4535}{10} = \mathbf{0.04535} \approx \mathbf{4.54\%}$$

**The average annual growth rate for ANF across 2017–2026 was 4.54%.**

---

### Step 3 — Compute standard deviation σ

**First, compute each squared deviation $(g_t - \mu)^2$:**

| Year | $g_t$ | $g_t - \mu$ | $(g_t - \mu)^2$ |
|------|--------|------------|----------------|
| 2017 | −0.0074 | −0.0528 | 0.002788 |
| 2018 | +0.0278 | −0.0176 | 0.000310 |
| 2019 | +0.0095 | −0.0359 | 0.001289 |
| 2020 | −0.0003 | −0.0457 | 0.002088 |
| 2021 | **−0.1363** | **−0.1817** | **0.033015** ← largest |
| 2022 | +0.1863 | +0.1410 | 0.019881 |
| 2023 | −0.0040 | −0.0494 | 0.002440 |
| 2024 | +0.1579 | +0.1126 | 0.012679 |
| 2025 | +0.1562 | +0.1109 | 0.012299 |
| 2026 | +0.0638 | +0.0185 | 0.000342 |
| | | **Sum →** | **0.087131** |

**Now apply the sample variance formula (ddof=1, divide by n−1 = 9):**

$$\sigma^2_s = \frac{1}{n-1} \sum (g_t - \mu)^2 = \frac{0.087131}{9} = 0.009681$$

**Take the square root to get standard deviation:**

$$\sigma = \sqrt{0.009681} = \mathbf{0.09839} \approx \mathbf{9.84\%}$$

**The standard deviation of ANF's annual growth rates is 9.84%.** This means a "typical" ANF year deviates from the 4.54% mean by roughly ±9.84 percentage points.

---

### Step 4 — Compute z-score for every year

$$z_t = \frac{g_t - \mu}{\sigma} = \frac{g_t - 0.04535}{0.09839}$$

| Year | $g_t$ | $g_t - \mu$ | $z_t$ | \|$z_t$\| > 2.0? | Status |
|------|--------|------------|-------|-----------------|--------|
| 2017 | −0.0074 | −0.0528 | **−0.54** | No | Normal |
| 2018 | +0.0278 | −0.0176 | **−0.18** | No | Normal |
| 2019 | +0.0095 | −0.0359 | **−0.36** | No | Normal |
| 2020 | −0.0003 | −0.0457 | **−0.46** | No | Normal |
| 2021 | −0.1363 | −0.1817 | **−1.85** | **No — just inside** | Normal |
| 2022 | +0.1863 | +0.1410 | **+1.43** | No | Normal |
| 2023 | −0.0040 | −0.0494 | **−0.50** | No | Normal |
| 2024 | +0.1579 | +0.1126 | **+1.14** | No | Normal |
| 2025 | +0.1562 | +0.1109 | **+1.13** | No | Normal |
| 2026 | +0.0638 | +0.0185 | **+0.19** | No | Normal |

**Result: No outliers detected for ANF.**

---

### Why ANF's COVID year (2021) was NOT flagged — the key insight

The 2021 COVID year had z = −1.85. That is the most extreme year — but it does not cross the |z| > 2.0 threshold. Here is exactly why:

ANF is an inherently **volatile retailer**. Its $\sigma = 9.84\%$ is high. That means the threshold for "unusual" is:

$$\text{Outlier threshold} = \mu \pm 2\sigma = 4.54\% \pm 2 \times 9.84\% = [-15.14\%,\; +24.22\%]$$

ANF's 2021 growth was **−13.63%**. That falls inside the [−15.14%, +24.22%] range — barely, but it is inside. The z-score system correctly judges it as unusual-but-not-extraordinary for this specific company.

**Compare to a stable company with σ = 3%:**
For that company, a −13.63% year would give:
$$z = \frac{-0.1363 - 0.0454}{0.03} = \frac{-0.1817}{0.03} = -6.06$$
That would be flagged immediately — because it is 6 standard deviations from normal for a stable company.

**This is why the system adapts per company.** A −14% year is catastrophic for a stable utility company, but within the range of normal volatility for a fashion retailer. The z-score captures this automatically.

---

### Why Macy's shows "2 outlier years excluded" but ANF shows none

Macy's has lower baseline growth volatility than ANF. Its $\sigma$ is smaller, so the ±2σ band is narrower. COVID 2020 and another unusual year both fell outside that narrower band → z > 2.0 → flagged.

ANF's wider $\sigma$ means the threshold is set higher, and even COVID only reaches z = −1.85.

---

## What happens after a year is flagged

Two datasets exist from this point forward:

| Dataset | Contains | Used for |
|---------|----------|----------|
| `self.clean_data` | **All years including outliers** | Charts — the full history is always shown visually |
| `self.data_excluding_outliers` | **Only non-outlier years** | Every model (Linear Regression, CAGR, Holt, etc.) — they never see outlier years |

The outlier years appear in charts with a **marker** so analysts know they exist but were excluded from forecasting.

---

## Per-model impact of exclusion

| Model | What changes when an outlier year is removed |
|-------|----------------------------------------------|
| Linear Regression | That year is not a data point in the OLS fit — slope and intercept ignore it |
| CAGR | If it was the first or last year, the base or endpoint changes. Middle years already had no effect on CAGR. |
| Exponential Smoothing | That year's actual value never enters the EWMA recursion — the smoothed level is unaffected |
| Holt's Linear Trend | Same — neither level nor trend state is updated with the outlier year |
| MA Trend | If the outlier falls within the last 3 years, it is excluded from the 3-year average |
| Weighted Average Growth | The outlier's growth rate does not enter the weighted sum at all |

---

## Quick-reference: all symbols used

| Symbol | Name | In this context |
|--------|------|----------------|
| $g_t$ | Growth rate at time t | Year-over-year revenue change for year t |
| $\mu$ | Mu — mean | Average of all growth rates |
| $\sigma^2$ | Sigma squared — variance | Average squared deviation from mean |
| $\sigma$ | Sigma — standard deviation | Typical spread of growth rates (same units as $g_t$) |
| $n$ | n | Number of valid growth rate observations |
| $n-1$ | Degrees of freedom | n minus the number of estimated parameters (we estimated $\mu$, so ddof=1) |
| ddof | Delta Degrees Of Freedom | The number subtracted from n in the denominator. ddof=1 gives sample std dev. |
| $z_t$ | Z-score for year t | How many standard deviations year t is from the mean |
| $\Sigma$ | Summation | Add up all the terms that follow |

---

## What to say when presenting

**To anyone:**
> "We compute each year's revenue growth rate. We then find the average and typical spread of those growth rates for this company. Any year whose growth was more than 2 standard deviations from the company's own average is flagged as unusual. It stays visible in the chart but is excluded from all the forecasting calculations. The threshold adapts per company — a bad year for one company might be totally normal for another."

**To a technical audience:**
> "We compute year-over-year growth rates via `pct_change()`, then compute the sample mean and sample standard deviation using pandas defaults (ddof=1 — Bessel's correction, dividing by n−1 to correct for the bias introduced by estimating the mean from the same sample). We then standardise each growth rate into a z-score. Any year with |z| > 2.0 is added to the outlier list and excluded from `data_excluding_outliers`, which is the dataset passed to all six forecasting models."
