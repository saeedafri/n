# Revenue Estimates — Complete Technical Reference

**Audience:** Data Scientists, ML Engineers, Data Analysts, and anyone who needs to understand every detail
of the forecasting system — formulas, statistical theory, implementation code, data pipeline, and design decisions.

**Status:** Generated from and verified against live source code in:
- `app/utils/retailer_forecaster.py`
- `app/data/revenue_forecast_service.py`
- `app/pages/estimates.py`

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Data Pipeline — DB to Python](#2-data-pipeline--db-to-python)
3. [Data Preparation and Normalization](#3-data-preparation-and-normalization)
4. [Outlier Detection — Full Math](#4-outlier-detection--full-math)
5. [Model 1: Linear Regression (OLS)](#5-model-1-linear-regression-ols)
6. [Model 2: CAGR — Compound Annual Growth Rate](#6-model-2-cagr--compound-annual-growth-rate)
7. [Model 3: Exponential Smoothing (SES + Linear Trend)](#7-model-3-exponential-smoothing-ses--linear-trend)
8. [Model 4: Holt's Linear Trend Method](#8-model-4-holts-linear-trend-method)
9. [Model 5: Moving Average Trend (MA Trend)](#9-model-5-moving-average-trend-ma-trend)
10. [Model 6: Weighted Average Growth](#10-model-6-weighted-average-growth)
11. [Backtesting — Holdout Validation](#11-backtesting--holdout-validation)
12. [Ensemble Construction](#12-ensemble-construction)
13. [Scenario Analysis — Percentile Paths](#13-scenario-analysis--percentile-paths)
14. [Evaluation Metrics — MAPE, Bias, RMSE](#14-evaluation-metrics--mape-bias-rmse)
15. [Caching and Performance](#15-caching-and-performance)
16. [Python Libraries and Functions Used](#16-python-libraries-and-functions-used)
17. [Complete Glossary — Every Term Defined](#17-complete-glossary--every-term-defined)
18. [Statistical Theorems and Principles Referenced](#18-statistical-theorems-and-principles-referenced)
19. [Frequently Asked Questions from a Technical Audience](#19-frequently-asked-questions-from-a-technical-audience)

---

## 1. System Architecture

### Layer diagram

```
┌──────────────────────────────────────────────────────┐
│  Streamlit UI — app/pages/estimates.py               │
│  - SelectBox triggers ticker change                  │
│  - Renders hero, metric cards, tabs, charts          │
└────────────────────────┬─────────────────────────────┘
                         │ calls
┌────────────────────────▼─────────────────────────────┐
│  RevenueForecastService — app/data/revenue_forecast_service.py │
│  - Reads from MySQL (Azure staging)                  │
│  - Normalizes rows (raw → billions)                  │
│  - Calls RetailerForecaster                          │
│  - Returns one dashboard payload dict                │
│  - Cached: get_companies() → 3600s TTL               │
│            get_company_dashboard() → 900s TTL        │
└────────────────────────┬─────────────────────────────┘
                         │ calls
┌────────────────────────▼─────────────────────────────┐
│  RetailerForecaster — app/utils/retailer_forecaster.py│
│  - from_dataframe(df) classmethod                    │
│  - _prepare_data()                                   │
│  - _detect_outliers(threshold=2.0)                   │
│  - 6 _forecast_* methods                             │
│  - backtest(holdout_years=2)                         │
│  - forecast(periods=5)                               │
└──────────────────────────────────────────────────────┘
                         │ uses
┌──────────────────────────────────────────────────────┐
│  scipy.stats.linregress (Linear Regression)          │
│  numpy (arrays, percentile, average, diff, power)    │
│  pandas (DataFrames, pct_change, dropna)             │
└──────────────────────────────────────────────────────┘
```

### Key design decisions

**Stateless per request:** `RevenueForecastService` methods are `@staticmethod` with `@st.cache_data`. No shared mutable state between requests.

**Read-only DB:** The service never runs INSERT, UPDATE, or DELETE. Only `execute_query_readonly()` is used, which uses a AUTOCOMMIT read-only SQLAlchemy engine.

**In-memory computation:** All six models, backtesting, ensemble, and scenarios are computed in Python (numpy/scipy). No model weights are stored in the database.

**No iterative optimization:** None of the six models use gradient descent, Adam, or any optimizer. All are closed-form or sequential-pass algorithms. This makes them deterministic and fast (sub-millisecond each).

---

## 2. Data Pipeline — DB to Python

### Step 1: Company discovery

SQL query in `RevenueForecastService.get_companies()`:

```sql
WITH sec_stats AS (
    SELECT ticker,
           COUNT(DISTINCT fiscal_date_ending) AS row_count,
           MAX(fiscal_date_ending) AS max_period
    FROM coreiq_av_financials_income_statement
    WHERE report_type = 'annual'
      AND total_revenue IS NOT NULL
    GROUP BY ticker
),
yf_stats AS (
    SELECT ticker,
           COUNT(DISTINCT period_end) AS row_count,
           MAX(period_end) AS max_period
    FROM (
        SELECT ticker, period_end
        FROM coreiq_yf_financials_income_statement
        WHERE frequency = 'annual'
          AND line_item = 'Total Revenue'
          AND value IS NOT NULL
    ) yf
    GROUP BY ticker
)
SELECT
    c.ticker,
    c.name_coresight AS company_name,
    c.source,
    COALESCE(sec_stats.row_count, yf_stats.row_count, 0) AS row_count,
    COALESCE(sec_stats.max_period, yf_stats.max_period) AS max_period
FROM coreiq_companies c
LEFT JOIN sec_stats  ON sec_stats.ticker = c.ticker
LEFT JOIN yf_stats   ON yf_stats.ticker  = c.ticker
WHERE c.source IN ('SEC', 'YFinance')
  AND COALESCE(sec_stats.row_count, yf_stats.row_count, 0) >= 3
ORDER BY company_name ASC
```

**Minimum threshold: 3 annual rows.** Companies with fewer than 3 data points are excluded because you need at least 2 years for a growth rate and 1 year for a forecast.

**Cache TTL: 3600 seconds (1 hour).** The company list changes rarely; 1-hour stale tolerance is acceptable.

### Step 2: Actual revenue fetch — SEC path

```sql
SELECT DISTINCT
    fiscal_date_ending AS period_date,
    total_revenue,
    gross_profit,
    operating_income,
    net_income,
    ebitda,
    reported_currency
FROM coreiq_av_financials_income_statement
WHERE ticker = :ticker
  AND report_type = 'annual'
ORDER BY fiscal_date_ending ASC
```

Source: AlphaVantage annual income statement data, already staged in MySQL.

### Step 3: Actual revenue fetch — YFinance path

YFinance stores data in a normalized (long) format: one row per (ticker, period, line_item). A pivot is done in SQL:

```sql
SELECT
    period_end AS period_date,
    MAX(CASE WHEN line_item = 'Total Revenue'      THEN value END) AS total_revenue,
    MAX(CASE WHEN line_item = 'Gross Profit'       THEN value END) AS gross_profit,
    MAX(CASE WHEN line_item = 'Operating Income'   THEN value END) AS operating_income,
    MAX(CASE WHEN line_item = 'Net Income'         THEN value END) AS net_income,
    MAX(CASE WHEN line_item = 'EBITDA'             THEN value END) AS ebitda
FROM coreiq_yf_financials_income_statement
WHERE ticker = :ticker
  AND frequency = 'annual'
GROUP BY period_end
ORDER BY period_end ASC
```

`MAX(CASE WHEN ...)` is a standard SQL conditional aggregation pivot — it selects exactly one value per line_item per period_end.

### Step 4: Normalization

`_normalize_actual_rows()` converts raw DB rows into a standardized dict per year:

- `total_revenue_billions = total_revenue / 1_000_000_000` (raw values in USD)
- `growth_pct = ((revenue - prev_revenue) / abs(prev_revenue)) * 100`  — signed percentage

This normalization is pure Python, no DB call. Result is a list of dicts passed into `RetailerForecaster.from_dataframe()`.

### Step 5: Engine construction

```python
model_df = pd.DataFrame({
    "year":  [row["fiscal_year"]            for row in actual_rows],
    "sales": [row["total_revenue_billions"] for row in actual_rows],
}).dropna()

engine = RetailerForecaster.from_dataframe(model_df)
```

`from_dataframe()` is a classmethod that bypasses the file-bytes constructor and calls `_prepare_data()` directly.

### Step 6: Dashboard payload

`get_company_dashboard()` runs the full pipeline and returns one large dict with keys:
`ticker`, `company_name`, `source`, `actual_rows`, `historical_rows`, `backtest_rows`,
`forecast_rows`, `raw_forecasts`, `best_method`, `best_method_display`, `best_mape`,
`available_models`, `outlier_years`, `timings_ms`, `summary`.

**Cache TTL: 900 seconds (15 minutes)** per (ticker). Revenue data from staging changes slowly; 15-minute stale tolerance is acceptable for a forecasting page.

---

## 3. Data Preparation and Normalization

Inside `RetailerForecaster._prepare_data()`:

### Column detection

Column names are not hard-coded. The code searches for substrings:

```python
# date column: any column name containing "date", "year", "period", "time"
date_col = next((col for col in df.columns
                 if any(t in col.lower() for t in ["date","year","period","time"])), df.columns[0])

# sales column: any column name containing "sales", "revenue", "amount", "value"
sales_col = next((col for col in df.columns
                  if any(t in col.lower() for t in ["sales","revenue","amount","value"])), df.columns[1])
```

### Year parsing

Integer years (2015, 2016…) must not go through `pd.to_datetime` because pandas interprets integers as nanoseconds since the Unix epoch:

```python
numeric_years = pd.to_numeric(df[date_col], errors="coerce")
looks_like_year = numeric_years.dropna().between(1900, 2200)
if looks_like_year.all() and not numeric_years.isna().all():
    df["year"] = numeric_years
else:
    df["year"] = pd.to_datetime(df[date_col], errors="coerce").dt.year
```

### Clean data construction

```python
self.clean_data = (
    df[["year", "sales"]]
    .dropna()                            # remove NaN in either column
    .drop_duplicates(subset=["year"])    # one row per fiscal year
    .sort_values("year")                 # ascending chronological
    .reset_index(drop=True)
)
self.clean_data["year"] = self.clean_data["year"].astype(int)
```

**No revenue values are dropped for being zero or negative.** Only NaN is dropped. A zero-revenue year can cause division-by-zero in growth rates — that is handled by checking `std_g > 0` in outlier detection.

---

## 4. Outlier Detection — Full Math

**File:** `RetailerForecaster._detect_outliers(threshold=2.0)`

**Exact code:**

```python
def _detect_outliers(self, threshold: float = 2.0) -> None:
    df = self.clean_data.copy()
    df["growth"] = df["sales"].pct_change()       # pandas pct_change
    mean_g = df["growth"].mean()
    std_g  = df["growth"].std()                   # pandas default: ddof=1
    if std_g > 0:
        df["z_score"] = (df["growth"] - mean_g) / std_g
        self.outliers = df.loc[df["z_score"].abs() > threshold, "year"].tolist()
    self.data_excluding_outliers = self.clean_data[
        ~self.clean_data["year"].isin(self.outliers)
    ].copy()
```

### What pct_change() computes

`pandas.Series.pct_change()` computes:

```
growth_t = (sales_t - sales_{t-1}) / sales_{t-1}
```

The first row is always `NaN` (no previous year to compare). This NaN propagates into the mean and std computations unless handled — pandas `.mean()` and `.std()` skip NaN by default, so the first-row NaN is automatically excluded.

### Z-score formula

```
z_t = (growth_t - μ_g) / σ_g

where:
  μ_g = (1/n) Σ growth_t        (arithmetic mean of growth rates)
  σ_g = sqrt[ Σ(growth_t - μ_g)² / (n-1) ]   (sample std dev, ddof=1)
```

**ddof=1 (Bessel's correction):** Pandas `.std()` uses `ddof=1` by default — divides by (n−1) rather than n. This gives an unbiased estimate of the population variance when the sample is small.

**Why ddof=1 matters here:** With short revenue series (8–15 years of data), using n in the denominator would systematically underestimate the true variance. Bessel's correction partially compensates.

### Threshold: |z| > 2.0

A z-score of 2.0 corresponds to approximately the **95th percentile** of a standard normal distribution (one-tailed). In a normal distribution, only ~4.5% of values should exceed |z| = 2.0.

For annual revenue growth rates, this means: a year is flagged as an outlier if its growth rate is more than 2 standard deviations from the mean growth rate of the company's history.

**Examples of what gets flagged:**
- COVID-2020 crash years (large negative growth)
- Post-COVID recovery years (unusually large positive growth)
- Year of a major acquisition (spike in revenue)
- Year of a major divestiture (sudden drop)

### What happens to outliers

Flagged years are listed in `self.outliers` (a list of integer years). They are:
- **Kept visible** in the UI (shown as orange × markers on charts)
- **Excluded** from model fitting (via `self.data_excluding_outliers`)
- **Excluded** from backtesting
- **Excluded** from growth rate computation for scenarios

### What does NOT get flagged

Revenue level changes alone are not flagged — only the year-over-year **growth rate** is tested. A company that grew from $2B to $3B to $4B has growth rates of 50% and 33%, not outliers by z-score unless the rest of history is different.

---

## 5. Model 1: Linear Regression (OLS)

### Full name and aliases

- **Ordinary Least Squares (OLS) Linear Regression**
- **Simple Linear Regression** (one predictor: year)
- **Univariate OLS**
- In time series: **Linear Trend Model**

### Mathematical formulation

**Model equation:**
```
sales_t = β₀ + β₁ × year_t + ε_t

where:
  β₀ = intercept (in billions of USD)
  β₁ = slope (USD billions per calendar year)
  ε_t = residual error ~ N(0, σ²)   [OLS assumption]
```

**OLS objective — minimize the residual sum of squares (RSS):**
```
minimize over (β₀, β₁):  RSS = Σᵢ (salesᵢ - β₀ - β₁ × yearᵢ)²
```

**Closed-form solution (Normal Equations):**

Let x̄ = mean(year), ȳ = mean(sales), n = number of training points.

```
Sxx = Σ (yearᵢ - x̄)²
Sxy = Σ (yearᵢ - x̄)(salesᵢ - ȳ)

β₁ = Sxy / Sxx      ← slope
β₀ = ȳ - β₁ × x̄   ← intercept
```

This is the unique global minimum — OLS has no local optima, no convergence issues.

### Python implementation — exact code

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

### scipy.stats.linregress — what it returns

`scipy.stats.linregress(x, y)` returns a `LinregressResult` namedtuple:
- `slope` → β₁
- `intercept` → β₀
- `rvalue` → Pearson correlation coefficient r (not r²)
- `pvalue` → two-tailed p-value for H₀: β₁ = 0
- `stderr` → standard error of the **slope estimate** (not the residual std)

**Important:** `stderr` here = SE(β̂₁), not the residual standard deviation σ̂.

The relationship: `SE(β̂₁) = σ̂ / sqrt(Sxx)`, so `σ̂ = stderr × sqrt(Sxx)`.

### Prediction interval construction

The standard (1-α) prediction interval for a new observation at future year x*:

```
ŷ(x*) ± t_{n-2, α/2} × σ̂ × sqrt(1 + 1/n + (x* - x̄)² / Sxx)
```

In the code, `t` is replaced by z = 1.645 (the 90% one-sided z-score, equivalent to 90% PI assuming large n).

The code computes:
```python
se = std_err * sqrt(1 + 1/n + (fy - mean_year)² / Sxx)
```
where `std_err` is SE(β̂₁). Multiplying by `sqrt(n)` inside the band computation converts this to an approximation of the residual standard deviation scaled for the prediction interval.

### R² — Coefficient of Determination

```
R² = r_value²    (using Pearson r from scipy.stats.linregress)

Equivalently:
R² = 1 - RSS / TSS

where:
  RSS = Σ(salesᵢ - ŷᵢ)²     (residual sum of squares)
  TSS = Σ(salesᵢ - ȳ)²      (total sum of squares)
```

**Interpretation:**
- R² = 1.0 → model explains 100% of variance in the training data (perfect linear fit)
- R² = 0.0 → model explains nothing; predictions are no better than the mean
- R² > 0.9 → very strong linear trend, model is reliable on training set

**Warning for technical reviewers:** High R² on training data does not mean good out-of-sample forecasting. For time series, you can have R² = 0.99 with poor MAPE on holdout years if the trend changes.

### Gauss-Markov Theorem

Under the four classical linear regression assumptions (linearity, exogeneity, homoscedasticity, no perfect multicollinearity), OLS estimators are:

**BLUE — Best Linear Unbiased Estimators**

- **Unbiased:** E[β̂] = β (OLS does not systematically over- or under-estimate)
- **Minimum variance:** Among all linear unbiased estimators, OLS has the smallest variance

For revenue forecasting, these assumptions rarely hold perfectly (revenue is not stationary, errors are not iid), but OLS still provides a useful and interpretable baseline.

### Complexity

- Time complexity: O(n) — one pass through data to compute Sxy and Sxx
- Space complexity: O(1) after data is loaded
- No iterations, no convergence, fully deterministic

### When it works well

- Company with steady absolute-dollar growth each year
- Long history with no structural breaks
- R² > 0.85

### When it fails

- Company that grows by percentage (should be exponential, not linear)
- Structural breaks (acquisition, spin-off, COVID)
- Short history (< 6 years) — OLS on 4 points is almost always spurious
- Trend reversal in recent years — OLS fits the whole history, not recent direction

---

## 6. Model 2: CAGR — Compound Annual Growth Rate

### Full name and aliases

- **CAGR — Compound Annual Growth Rate**
- **Geometric Mean Return** (in finance)
- **Geometric Growth Rate**
- In statistics: **Geometric Mean of Growth Factors**

### Mathematical formulation

```
CAGR = (last_sales / base_sales)^(1/n) - 1

where:
  last_sales  = most recent cleaned annual revenue
  base_sales  = earliest cleaned annual revenue
  n           = number of year-to-year intervals = (number of years - 1)
```

**Derivation:** If revenue compounds at rate g every year:

```
sales_T = sales_0 × (1 + g)^n
→ (1 + g)^n = sales_T / sales_0
→ 1 + g = (sales_T / sales_0)^(1/n)
→ g = (sales_T / sales_0)^(1/n) - 1
```

The geometric mean of growth factors `(1+g_1)(1+g_2)...(1+g_n)` raised to 1/n equals the CAGR. This is why CAGR is sometimes called the **geometric mean return**.

### Forecasting

```
forecast_h = last_sales × (1 + CAGR)^h

for h = 1, 2, 3, 4, 5 (the forecast horizon)
```

### Python implementation — exact code

```python
def _forecast_cagr(self, years, sales, periods, lookback=None):
    if lookback and lookback < len(sales):
        base, n = sales[-lookback], lookback - 1
    else:
        base, n = sales[0], len(sales) - 1
    cagr = np.power(sales[-1] / base, 1/n) - 1 if n > 0 else 0
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return {
        "method": "CAGR",
        "forecast": np.array([sales[-1] * np.power(1 + cagr, i) for i in range(1, periods+1)]),
        "years": fy,
        "cagr": cagr,
    }
```

`np.power(a, b)` = aᵇ. For the CAGR: `np.power(sales[-1]/base, 1/n)`.

### The endpoint problem

CAGR uses only two points: the first year and the last year. All intermediate years are ignored.

**Consequence:** If either endpoint is anomalous (e.g., last_sales was inflated by a one-time acquisition, or base_sales was during a downturn), the CAGR will be systematically biased.

**Example:**
- History: 100, 110, 90 (dip), 120
- CAGR = (120/100)^(1/3) - 1 = 1.2^0.333 - 1 = 6.27%
- Simple average of growth rates: ((10% + (-18.2%) + 33.3%) / 3) = 8.4%
- CAGR is lower because the starting point (100) is used as the base — the dip is invisible to CAGR.

### Arithmetic vs Geometric mean

CAGR is the **geometric mean** of gross growth factors:

```
Geometric mean = [(1+g₁)(1+g₂)...(1+gₙ)]^(1/n) - 1
```

The arithmetic mean of growth rates:
```
Arithmetic mean = (g₁ + g₂ + ... + gₙ) / n
```

For any non-constant series: **Geometric mean ≤ Arithmetic mean** (AM-GM inequality).

The arithmetic mean overestimates the actual compound growth. CAGR (geometric) is the correct answer for "what constant annual rate produces this endpoint?"

### Complexity

- O(1) — reads two values (first and last), no iteration

---

## 7. Model 3: Exponential Smoothing (SES + Linear Trend)

### Full name and aliases

- **Simple Exponential Smoothing (SES)** with a post-hoc trend term
- **EWMA — Exponentially Weighted Moving Average**
- **Brown's single exponential smoothing**
- In state-space notation: **ETS(A,N,N)** for level only; the code adds a linear trend projection afterwards, making it closer to **ETS(A,A,N)** but without jointly estimating β

### Mathematical formulation

**Smoothing update (applied year by year, left to right):**

```
L₁ = sales₁                                    (initialization: first actual)
Lₜ = α × salesₜ + (1 - α) × Lₜ₋₁             (for t = 2, 3, ..., n)

where:
  α = 0.3   (fixed in code — not estimated)
  Lₜ = smoothed level at time t
```

**Trend extraction (after processing all historical data):**

```
trend = L_last - L_prev
      = Lₙ - Lₙ₋₁
```

This is a simple backward difference of the last two smoothed levels.

**Forecast:**

```
forecast_h = L_last + h × trend

for h = 1, 2, 3, 4, 5
```

### Python implementation — exact code

```python
def _forecast_exp_smoothing(self, years, sales, periods, alpha=0.3):
    smoothed = [sales[0]]
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

### Exponential weight decay — proof

Expanding the recursion backwards:

```
Lₙ = α × salesₙ + (1-α) × Lₙ₋₁
   = α × salesₙ + (1-α) × [α × salesₙ₋₁ + (1-α) × Lₙ₋₂]
   = α × salesₙ + α(1-α) × salesₙ₋₁ + (1-α)² × Lₙ₋₂
   = ...
   = α × Σₖ₌₀ⁿ⁻¹ (1-α)ᵏ × salesₙ₋ₖ   +   (1-α)ⁿ × L₁
```

Each past observation receives a weight proportional to `(1-α)^k` where k is how many periods ago it occurred. With α = 0.3:

| Periods ago (k) | Weight = 0.3 × 0.7^k |
|-----------------|----------------------|
| 0 (most recent) | 0.300 |
| 1               | 0.210 |
| 2               | 0.147 |
| 3               | 0.103 |
| 4               | 0.072 |
| 5               | 0.050 |
| 10              | 0.009 |

Weights decrease geometrically (exponentially) as k increases — hence the name "Exponential Smoothing."

### Effect of α parameter

| α value | Behavior |
|---------|----------|
| α → 0   | Very slow to react; forecast is close to the long-run mean |
| α = 0.1 | Smooth; puts only 10% on the newest data point |
| α = 0.3 | **Used in this code** — balanced between reactivity and smoothing |
| α = 0.5 | Gives equal weight to current value and all past history combined |
| α → 1   | Naive model — forecast = last actual value |

### Why α is fixed, not estimated

In production exponential smoothing implementations (e.g., `statsmodels.tsa.holtwinters`), α is optimized by minimizing the sum of squared one-step-ahead errors. This code uses a fixed α = 0.3 which is a common industry default and avoids overfitting on short series.

### Complexity

- O(n) — one sequential pass through the sales series
- Memory: O(n) to store `smoothed` list (could be O(1) with a running update, but list is kept for potential debugging)

---

## 8. Model 4: Holt's Linear Trend Method

### Full name and aliases

- **Holt's Linear Trend Method** (Charles C. Holt, 1957)
- **Double Exponential Smoothing**
- **Holt's Two-Parameter Exponential Smoothing**
- In ETS classification: **ETS(A,A,N)** — Error: Additive, Trend: Additive, Seasonality: None
- **Brown's double smoothing** is a special case (α = β)

### Mathematical formulation

**Two state variables updated at each time step:**

```
Level update:
  levelₜ = α × salesₜ + (1 - α) × (levelₜ₋₁ + trendₜ₋₁)

Trend update:
  trendₜ = β × (levelₜ - levelₜ₋₁) + (1 - β) × trendₜ₋₁

Parameters:
  α = 0.3   (level smoothing — fixed)
  β = 0.1   (trend smoothing — fixed)
```

**Initialization (from first two data points):**

```
level₁ = sales₁
trend₁ = sales₂ - sales₁
```

**Forecast h steps ahead:**

```
forecast_h = level_last + h × trend_last
```

### Python implementation — exact code

```python
def _forecast_holt(self, years, sales, periods, alpha=0.3, beta=0.1):
    level = sales[0]
    trend = sales[1] - sales[0] if len(sales) > 1 else 0
    for i in range(1, len(sales)):
        prev = level
        level = alpha * sales[i] + (1 - alpha) * (level + trend)
        trend = beta * (level - prev) + (1 - beta) * trend
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return {
        "method": "Holt's Linear",
        "forecast": np.array([level + trend * i for i in range(1, periods + 1)]),
        "years": fy,
    }
```

**Note on the loop:** The variable `prev` captures `level` before the level update, so the trend update `β × (levelₜ - levelₜ₋₁)` uses the newly updated level minus the old level — this is the standard Holt implementation.

### Why two parameters beat one

Exponential Smoothing (Model 3) tracks one state: the smoothed level. Its trend is a single backward difference of the last two smoothed values — static, not updated.

Holt's method tracks two evolving states:
- `level` adjusts the revenue baseline every year (controlled by α)
- `trend` adjusts the direction and speed of change every year (controlled by β)

When a company hits a dip (e.g., COVID), the level drops. Then as it recovers, the level rises and the trend turns positive. The separate trend state allows the model to "re-learn" direction without being permanently anchored to the pre-dip slope.

### β = 0.1 — why slow trend adaptation

Low β (0.1) means the trend changes slowly. Only 10% of each year's observed level shift is incorporated into the trend estimate. This prevents the model from over-reacting to a single unusual year.

Compare: if β = 0.9, one bad year would almost completely reset the trend estimate, leading to very volatile forecasts.

### ETS Framework (Hyndman & Athanasopoulos)

Holt's method belongs to the ETS (Error, Trend, Seasonality) framework:

| Component | Code value |
|-----------|-----------|
| Error     | A (Additive) |
| Trend     | A (Additive linear) |
| Seasonality | N (None — annual data, no sub-annual seasonality) |

Full ETS notation: **ETS(A,A,N)**

The ETS framework (Hyndman, Koehler, Snyder, Grose, 2002) proves that exponential smoothing methods are optimal predictors for specific underlying state-space models.

### Complexity

- O(n) — one sequential loop through sales
- O(1) space — only `level` and `trend` scalars need to be stored

---

## 9. Model 5: Moving Average Trend (MA Trend)

### Full name and aliases

- **Moving Average Trend** (MA Trend)
- **Simple Moving Average of Growth Rates**
- Related to: **AR(1) model** (special case where window = 1), **ARIMA(0,1,q)** (the MA part)
- In forecasting: **Naïve method with windowed averaging**

### Mathematical formulation

**Step 1 — Compute year-over-year growth rates:**

```
growth_t = (sales_t - sales_{t-1}) / sales_{t-1}    for t = 2, 3, ..., n
```

This produces (n-1) growth rates.

**Step 2 — Average the last `window` growth rates:**

```
avg_growth = (1/w) × Σₖ₌₀^{w-1} growth_{n-k}

where w = min(window, len(growth_rates))
      window = 3   (fixed in code)
```

**Step 3 — Compound from latest actual:**

```
forecast_1 = last_sales × (1 + avg_growth)
forecast_2 = forecast_1 × (1 + avg_growth)
...
forecast_h = last_sales × (1 + avg_growth)^h
```

### Python implementation — exact code

```python
def _forecast_ma_trend(self, years, sales, periods, window=3):
    growth_rates = np.diff(sales) / sales[:-1]
    avg_growth = (np.mean(growth_rates[-window:])
                  if len(growth_rates) >= window
                  else np.mean(growth_rates))
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    forecast, last = [], sales[-1]
    for _ in range(periods):
        last = last * (1 + avg_growth)
        forecast.append(last)
    return {"method": "MA Trend", "forecast": np.array(forecast), "years": fy}
```

`np.diff(sales)` computes `sales[1:] - sales[:-1]` (first differences). Dividing by `sales[:-1]` gives growth rates.

`growth_rates[-window:]` slices the last `window` elements of the growth rate array.

### np.diff — what it does

`np.diff(a)` = `a[1:] - a[:-1]`

For `sales = [100, 110, 105, 120]`:
```
np.diff(sales) = [10, -5, 15]
sales[:-1]     = [100, 110, 105]
growth_rates   = [0.10, -0.0455, 0.1429]
                 [10%, -4.55%, 14.29%]
```

### Short series fallback

If `len(growth_rates) < window` (fewer than 3 growth rates, i.e., fewer than 4 years of data), it uses the mean of all available growth rates instead of crashing. This prevents a `window=3` failure on short series.

### Stationarity note

MA Trend implicitly assumes that the growth rate process is **stationary** within the window — i.e., the distribution of the last 3 growth rates is representative of future growth rates.

This is the key assumption that breaks when a company undergoes a structural change after the window period.

---

## 10. Model 6: Weighted Average Growth

### Full name and aliases

- **Linearly Weighted Moving Average (LWMA)** of growth rates
- **Weighted Moving Average (WMA)** with linear weights
- In statistics: **Weighted Mean** with domain-specified weights
- Related to: **Attention mechanism** in transformers (conceptually — both compute weighted averages)

### Mathematical formulation

**Step 1 — Compute all year-over-year growth rates (same as MA Trend):**

```
growth_t = (sales_t - sales_{t-1}) / sales_{t-1}   for t = 2, ..., n
```

Produces (n-1) growth rates.

**Step 2 — Assign linearly increasing weights:**

```
weights = [1, 2, 3, ..., n-1]   (for n-1 growth rates)
```

Oldest growth rate gets weight 1. Newest growth rate gets weight (n-1).

**Step 3 — Compute weighted average:**

```
weighted_growth = Σᵢ (wᵢ × growthᵢ) / Σᵢ wᵢ

where Σᵢ wᵢ = 1 + 2 + 3 + ... + (n-1) = (n-1) × n / 2
```

The denominator formula `n(n+1)/2` for the sum 1+2+...+n is **Gauss's triangular number formula**:

```
S = Σₖ₌₁ⁿ k = n(n+1)/2
```

Proof (Gauss's trick): Write S forward and backward, add them:
```
S   = 1   + 2   + ... + (n-1) + n
S   = n   + (n-1) + ... + 2   + 1
2S  = (n+1)(n+1)...(n+1)  [n pairs]
2S  = n(n+1)
S   = n(n+1)/2
```

**Step 4 — Compound from latest actual:**

```
forecast_h = last_sales × (1 + weighted_growth)^h
```

### Python implementation — exact code

```python
def _forecast_weighted_avg(self, years, sales, periods):
    growth_rates = np.diff(sales) / sales[:-1]
    if len(growth_rates) == 0:
        weighted_growth = 0.0
    else:
        weights = np.arange(1, len(growth_rates) + 1, dtype=float)
        weighted_growth = np.average(growth_rates, weights=weights)
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    forecast, last = [], sales[-1]
    for _ in range(periods):
        last = last * (1 + weighted_growth)
        forecast.append(last)
    return {"method": "Weighted Avg", "forecast": np.array(forecast), "years": fy}
```

### numpy.average with weights — what it computes

`np.average(a, weights=w)` = `Σ(wᵢ × aᵢ) / Σ(wᵢ)`

This is exactly the weighted mean formula. It handles the denominator automatically (no need to normalize weights to sum to 1 before passing).

### Comparison: Weighted Avg vs MA Trend vs Simple Mean

| Property | Simple Mean | Weighted Avg | MA Trend |
|----------|------------|--------------|----------|
| Uses all history | Yes | Yes | No (last 3 only) |
| Recency bias | No | Yes (linear) | Strong (uniform over window) |
| Decay type | None | Linear (slow) | Hard cutoff |
| Number of parameters | 0 | 0 | 1 (window) |

---

## 11. Backtesting — Holdout Validation

### Full name and aliases

- **Holdout Validation** (or **Hold-out Set Evaluation**)
- **Out-of-Sample Testing**
- In time series: **Walk-Forward Validation** (simplified single-step version)
- **Time Series Cross-Validation** (a more general form, not used here)

### How it works — exact implementation

```python
def backtest(self, holdout_years: int = 2, exclude_outliers: bool = True) -> pd.DataFrame:
    df = self.data_excluding_outliers if exclude_outliers else self.clean_data

    # Fallback: if too short for 2 holdout years, use 1
    if len(df) <= holdout_years + 2:
        holdout_years = 1

    train = df.iloc[:-holdout_years]   # all years except the last 2
    test  = df.iloc[-holdout_years:]   # the last 2 years (hidden from training)

    ty, ts = train["year"].values, train["sales"].values
    actual = test["sales"].values

    # Run each of the 6 models on training data only
    for key, method in _methods:
        predicted = method(ty, ts, holdout_years)["forecast"]
        mape = np.mean(np.abs(predicted - actual) / actual) * 100
        bias = float(np.mean(predicted - actual))
        rmse = float(np.sqrt(np.mean((predicted - actual) ** 2)))
        ...
```

### Why holdout validation (not cross-validation)

Standard k-fold cross-validation randomly shuffles data. For time series, this is **invalid** because it causes data leakage: you would train on future data to predict the past, which is not possible in production.

Holdout validation preserves the temporal ordering: the model only ever sees past data when predicting future data — exactly as it would in production.

### Why 2 holdout years

2 years is the minimum to test both short-term (next year) and medium-term (2 years out) accuracy. With only 1 holdout year, a lucky or unlucky single year could entirely determine which model wins.

**Short series fallback:** If `len(df) <= holdout_years + 2`, i.e., fewer than 5 clean years, the holdout is reduced to 1 year. This ensures at least 2 years remain for training (minimum for growth rate computation).

### What the backtest does NOT do

- It does **not** use time-series cross-validation (multiple rolling windows)
- It does **not** re-train on different windows
- It is a single train/test split — the split point is always `n - 2`

This simplicity is a deliberate product decision: interpretable, fast, and sufficient for annual revenue series with 5–20 data points.

### Walk-Forward Validation (what we would add for production ML)

A more robust approach for production ML would be:

```
Window 1: train on years 1-5,  predict year 6
Window 2: train on years 1-6,  predict year 7
Window 3: train on years 1-7,  predict year 8
...
Average MAPE across all windows
```

This is not used here because annual revenue series often have fewer than 10 years of data, making multiple windows impractical.

---

## 12. Ensemble Construction

### Full name and aliases

- **Simple Average Ensemble** / **Equal-Weight Model Averaging**
- **Model Blending** (Kaggle terminology)
- **Committee Machine** (older ML term)
- **Unweighted Combination Forecast**
- Related to: **Bagging** (Bootstrap Aggregating), though bagging uses the same algorithm on different subsets, while this uses different algorithms on the same data

### Selection logic — exact code

```python
# After backtest():
if self.backtest_results is not None and len(self.backtest_results):
    top3 = self.backtest_results.nsmallest(3, "mape")["method"].tolist()
else:
    top3 = ["holt", "cagr", "weighted_avg"]   # default when no backtest available
results["ensemble"] = np.round(results[top3].mean(axis=1), 1)
```

`DataFrame.nsmallest(3, "mape")` — returns the 3 rows with the smallest MAPE values. This is pandas's efficient partial sort, O(n log 3) rather than O(n log n) for full sort.

`.mean(axis=1)` — row-wise mean across the 3 selected model columns. For each forecast year, this averages the 3 model values.

### Why top 3, not all 6

The principle is **variance reduction through diversification**, from the statistical theory of forecast combination (Bates & Granger, 1969).

Key insight: If models have **correlated errors** (they all miss in the same direction when the economy changes), ensembling helps less. If they have **uncorrelated errors** (one over-predicts while another under-predicts), ensembling helps more.

Including the weakest models:
- Adds their errors to the blend
- Dilutes the contribution of the strong models
- Net effect: ensemble is worse than just using top 3

**Diminishing returns study (M4 Competition, 2018):**
- Top 1 model → baseline
- Top 2 models → meaningful improvement
- Top 3 models → significant improvement
- Top 4–6 → marginal or no improvement

### Default fallback — when backtest cannot run

```python
top3 = ["holt", "cagr", "weighted_avg"]
```

This fires when: the series is too short for backtesting (fewer than 4 cleaned years after outlier removal).

These three defaults cover different structural assumptions:
- Holt's handles trends with momentum
- CAGR anchors on long-run compounding
- Weighted Average balances recent and historical growth

### Variance reduction theory

If three models have errors ε₁, ε₂, ε₃ with the same variance σ² and pairwise correlation ρ:

```
Var(ensemble) = σ²/3 × (1 + 2ρ)

when ρ = 0 (uncorrelated):  Var = σ²/3   ← 66% reduction vs single model
when ρ = 1 (identical):     Var = σ²     ← no benefit
```

In practice, revenue forecasting models have ρ between 0.5 and 0.9 — correlated but not identical. So ensemble reduces variance by roughly 15–40% compared to the best single model.

---

## 13. Scenario Analysis — Percentile Paths

### Full name and aliases

- **Historical Percentile Scenario Analysis**
- **Quantile-Based Growth Paths**
- Related to: **Quantile Forecasting**, **VaR (Value at Risk)** methodology in finance, **Monte Carlo Simulation** (more general)

### How scenarios are built — exact code

```python
growth_rates = np.diff(sales) / sales[:-1] if len(sales) > 1 else np.array([0.0])
for label, pct in [("pessimistic", 25), ("baseline", 50), ("optimistic", 75)]:
    g = np.percentile(growth_rates, pct)
    sc, last = [], sales[-1]
    for _ in range(periods):
        last *= 1 + g
        sc.append(last)
    results[f"scenario_{label}"] = np.round(sc, 1)
```

### numpy.percentile — interpolation method

`np.percentile(a, q)` uses **linear interpolation** by default (method='linear' in NumPy ≥ 1.22, equivalent to 'fraction' in older versions).

For a sorted array of length n, the qth percentile position is computed as:
```
position = q/100 × (n - 1)
```

If position is not an integer:
```
percentile = a[floor(position)] + fraction × (a[ceil(position)] - a[floor(position)])
```

**Example:** 7 growth rates sorted: [-5%, 3%, 6%, 9%, 12%, 18%, 22%]
- 25th percentile: position = 0.25 × 6 = 1.5 → 3% + 0.5 × (6% - 3%) = **4.5%**
- 50th percentile: position = 0.50 × 6 = 3.0 → **9%** exactly
- 75th percentile: position = 0.75 × 6 = 4.5 → 12% + 0.5 × (18% - 12%) = **15%**

### Compounding formula

```
scenario_h = last_sales × (1 + g)^h

This is the Future Value formula from finance:
FV = PV × (1 + r)^t

where PV = present value (last_sales)
      r  = growth rate (g_percentile)
      t  = time horizon (h)
```

### Scenarios are not model outputs

Key distinction: models estimate "the most likely revenue given this company's historical pattern."

Scenarios ask: "If future growth matches the company's own historical distribution at this percentile, where does revenue end up?"

Scenarios use **historical growth rates only** — no model fitting, no regression, no learning. They are purely distributional.

### When the scenario band is wide vs narrow

**Narrow band** (pessimistic ≈ baseline ≈ optimistic):
- Company has very consistent growth rates historically
- Low growth volatility (std dev)
- High predictability

**Wide band:**
- Company has highly variable growth rates
- High volatility companies (e.g., cyclical retailers, commodity-linked companies)
- Low predictability — the scenarios themselves are uncertain

---

## 14. Evaluation Metrics — MAPE, Bias, RMSE

### MAPE — Mean Absolute Percentage Error

**Formula:**
```
MAPE = (1/h) × Σₜ₌₁ʰ |predicted_t - actual_t| / actual_t × 100
```

**In code:**
```python
mape = np.mean(np.abs(predicted - actual) / actual) * 100
```

**Properties:**
- Unit-free (percentage) — enables comparison across companies with different revenue scales
- Symmetric with respect to sign of error? **No.** A 50% over-prediction = MAPE 50%, but a 50% under-prediction = MAPE 50%. However, over-prediction is bounded below by 0% while under-prediction is unbounded above (if actual → 0).
- Undefined when actual = 0 (division by zero) — not an issue here since revenue is always > 0

**Known limitation (MAPE asymmetry):**
If actual = 100:
- Predicted 150 → MAPE = 50% (over-predicted by 50)
- Predicted 50  → MAPE = 50% (under-predicted by 50)

Both give MAPE = 50%, but they represent different magnitudes in dollar terms. Alternative: SMAPE (Symmetric Mean Absolute Percentage Error), not used here.

**Interpretation thresholds (annual revenue, multi-year holdout):**

| MAPE | Classification |
|------|---------------|
| < 5% | Excellent |
| 5–10% | Good |
| 10–15% | Acceptable |
| 15–25% | Weak |
| > 25% | Poor |

These thresholds are higher than for short-horizon (monthly/weekly) forecasting because annual data has inherently higher year-to-year variability.

### Bias

**Formula:**
```
Bias = (1/h) × Σₜ₌₁ʰ (predicted_t - actual_t)
```

**In code:**
```python
bias = float(np.mean(predicted - actual))
```

**Unit:** Same as revenue (billions of USD in this system).

**Interpretation:**
- Bias > 0 → model systematically over-predicted the holdout years (optimistic bias)
- Bias < 0 → model systematically under-predicted (pessimistic bias)
- Bias ≈ 0 → predictions were centered around the actuals on average

**MAPE + Bias together:**
- MAPE = 8%, Bias = -0.1B → model misses by 8% but without consistent direction — random errors, acceptable
- MAPE = 8%, Bias = -3.0B → model consistently under-predicts by $3B — structural pessimism, investigate

### RMSE — Root Mean Squared Error

**Formula:**
```
RMSE = sqrt[ (1/h) × Σₜ₌₁ʰ (predicted_t - actual_t)² ]
```

**In code:**
```python
rmse = float(np.sqrt(np.mean((predicted - actual) ** 2)))
```

**Unit:** Same as revenue (billions of USD).

**Properties:**
- Penalizes large misses more than MAPE (because errors are squared before averaging)
- Sensitive to outlier holdout years — one very bad prediction year inflates RMSE heavily
- Related to variance of prediction errors: `RMSE² = Variance(errors) + Bias²`
- This is the **Bias-Variance Decomposition** of MSE

**MSE Decomposition:**
```
MSE = Bias² + Variance(errors)
RMSE = sqrt(MSE)
```

A model with low Bias but high Variance will have high RMSE due to inconsistent errors. A model with high Bias but low Variance misses consistently but by a predictable amount.

---

## 15. Caching and Performance

### Streamlit cache layers

```python
@st.cache_data(ttl=3600, show_spinner=False)
def get_companies() -> List[Dict]:
    ...

@st.cache_data(ttl=900, show_spinner=False)
def get_company_dashboard(ticker: str) -> Dict:
    ...
```

`@st.cache_data` serializes the return value to disk/memory and returns the cached copy on subsequent calls with the same arguments. Cache invalidates after TTL (time-to-live) seconds.

**get_companies: 3600s (1 hour)**
Rationale: Company list in staging changes infrequently (new companies added during manual data loads). 1-hour stale tolerance is acceptable.

**get_company_dashboard: 900s (15 minutes)**
Rationale: Revenue actuals change only during quarterly data ingestion. 15-minute stale is fine. Ticker is the cache key — changing the dropdown creates a new cache entry.

### Timing instrumentation

Every major step logs timing via `log_timing()`:

| Log key | What it measures |
|---------|-----------------|
| `FORECAST_GET_COMPANIES` | SQL query + company list construction |
| `FORECAST_LOAD_ACTUALS` | SQL query + row normalization |
| `FORECAST_SUMMARY_STATS` | engine.summary_stats() |
| `FORECAST_BACKTEST` | All 6 models × holdout period |
| `FORECAST_FIT` | All 6 models × full 5-year forecast |
| `FORECAST_TOTAL` | End-to-end dashboard load |
| `ESTIMATES_PAGE_LOAD_DASHBOARD` | RevenueForecastService call total |
| `ESTIMATES_PAGE_RENDER` | Streamlit rendering |

### Expected performance profile

- SQL query: ~250ms (Azure MySQL RTT floor)
- Model fitting (all 6): < 5ms (simple numpy operations on 5–20 data points)
- Backtest (all 6): < 5ms
- Total without cache: ~300–500ms
- With cache hit: < 5ms (serialized dict lookup)

---

## 16. Python Libraries and Functions Used

### scipy.stats.linregress

```python
slope, intercept, r_value, p_value, stderr = scipy.stats.linregress(x, y)
```

Computes OLS regression of y on x. Uses the closed-form Normal Equations internally.

`stderr` = standard error of the slope estimate = `sqrt(RSS / (n-2)) / sqrt(Sxx)`

### numpy functions used — every one

| Function | Used for |
|----------|----------|
| `np.diff(a)` | First differences: `a[1:] - a[:-1]` — computes growth in absolute terms |
| `np.mean(a)` | Arithmetic mean — used in backtest metrics |
| `np.abs(a)` | Element-wise absolute value — used in MAPE |
| `np.sqrt(x)` | Square root — used in RMSE and prediction interval |
| `np.power(a, b)` | Element-wise power aᵇ — used in CAGR and scenario compounding |
| `np.arange(start, stop)` | Integer range for forecast years |
| `np.percentile(a, q)` | Percentile with linear interpolation — scenarios |
| `np.average(a, weights=w)` | Weighted mean — Weighted Average Growth |
| `np.round(a, decimals)` | Rounding forecast values to 1 decimal |
| `np.array(list)` | Convert Python list to numpy array |

### pandas functions used

| Function | Used for |
|----------|----------|
| `df.pct_change()` | Year-over-year growth rates — outlier detection, summary stats |
| `df.dropna()` | Remove rows with NaN in any column |
| `df.drop_duplicates(subset=["year"])` | One row per fiscal year |
| `df.sort_values("year")` | Chronological ordering |
| `df.iloc[:-n]` | All rows except the last n (train split) |
| `df.iloc[-n:]` | The last n rows (test split) |
| `df.nsmallest(3, "mape")` | Top 3 models by MAPE — ensemble selection |
| `df["col"].mean(axis=1)` | Row-wise mean across model columns — ensemble |
| `df.to_dict("records")` | Convert DataFrame to list of dicts for JSON serialization |

---

## 17. Complete Glossary — Every Term Defined

**α (alpha):** Smoothing parameter in Exponential Smoothing and Holt's method. Range: (0, 1). Higher α → more weight to the most recent data point; lower α → smoother, slower-reacting estimates. Fixed at 0.3 in this code.

**β (beta):** Trend smoothing parameter in Holt's method only. Range: (0, 1). Controls how fast the trend component reacts to changes in the level. Fixed at 0.1 (slow adaptation).

**Actual / Actuals:** True historical revenue values read from the database. Not estimated. The source of truth for all model fitting.

**Additive error:** In ETS notation, the model assumes forecast errors are additive (error is added to the forecast, not multiplied). All models here use additive formulation.

**AM-GM Inequality:** Arithmetic Mean ≥ Geometric Mean for any non-negative, non-constant series. CAGR (geometric mean) is always ≤ simple average of growth rates.

**ARIMA:** AutoRegressive Integrated Moving Average. A general class of time series models. The models in this system are simplified special cases of ARIMA.

**Backtest:** Simulation of past forecasting. Train on historical data minus the last 2 years, predict those 2 hidden years, compare to actuals. Measures how accurate the model would have been if run 2 years ago.

**Bessel's Correction:** Using (n-1) instead of n in the variance denominator to produce an unbiased estimate of population variance from a sample. Applied in `pandas.Series.std()` (ddof=1).

**Bias (forecast):** Mean signed error = mean(predicted - actual). Positive = systematic over-prediction. Negative = systematic under-prediction.

**BLUE:** Best Linear Unbiased Estimator. OLS estimators are BLUE under Gauss-Markov assumptions.

**CAGR:** Compound Annual Growth Rate. = (last/first)^(1/n) - 1. The geometric mean of the gross growth factor.

**CI (Confidence Interval):** Interval for the true parameter value (e.g., slope). Distinguished from Prediction Interval — CI is narrower and does not include residual variance. The chart shows a Prediction Interval, not a CI.

**Clean data:** Revenue series after NaN removal and deduplication, before outlier removal.

**Closed-form solution:** An exact mathematical formula for the answer, as opposed to an iterative numerical optimization. OLS, CAGR, and Weighted Average Growth all have closed-form solutions.

**Compounding / Compound Growth:** Growth where the gain each period is applied to the previous period's result, not the original base. Formula: FV = PV × (1+r)^n.

**Correlation (Pearson r):** Measure of linear association between two variables. Range: [-1, 1]. r² = R².

**Data Excluding Outliers:** The training series passed to model fitting methods. = clean_data minus years whose growth rate z-score exceeds 2.0.

**ddof:** Delta degrees of freedom. `ddof=1` in pandas std means dividing by (n-1), not n (Bessel's correction).

**Deterministic:** Given the same input, always produces the same output. All six models are deterministic — no random number generation, no stochastic optimization.

**Double Exponential Smoothing:** Another name for Holt's method. Applies exponential smoothing twice: once for level, once for trend.

**ETS:** Error/Trend/Seasonality framework for exponential smoothing models. Holt's is ETS(A,A,N). Simple ES is ETS(A,N,N).

**Ensemble:** Average of the top 3 models by backtest MAPE. Not a separately trained model — computed from outputs of the individual models.

**Endpoint problem:** The CAGR's sensitivity to the first and last data points in the series. An unusual start or end year distorts the CAGR.

**Exponentially Weighted Moving Average (EWMA):** A weighted average where weights decrease geometrically as data ages. Used in Exponential Smoothing.

**First differences:** `Δsales_t = sales_t - sales_{t-1}`. Computed by `np.diff()`. Used to get growth in absolute terms before dividing by the base to get the rate.

**Forecast horizon:** Number of future years to project. Fixed at 5 in this system.

**Gauss-Markov Theorem:** Under four assumptions (linearity, strict exogeneity, homoscedasticity, no perfect multicollinearity), OLS estimators are BLUE.

**Gauss's Triangular Number Formula:** `1 + 2 + ... + n = n(n+1)/2`. Used to compute the denominator of the weighted average.

**Geometric Mean:** (a₁ × a₂ × ... × aₙ)^(1/n). Equivalent to CAGR when applied to gross growth factors.

**Growth rate:** Year-over-year percentage change in revenue. = (salesₜ - salesₜ₋₁) / salesₜ₋₁.

**Holdout:** Years hidden from model training and used for backtesting evaluation. Default: 2 years.

**Holt's method:** Double exponential smoothing with two parameters (α, β) tracking level and trend. Published by Charles C. Holt in 1957.

**Homoscedasticity:** One of the OLS assumptions — residual variance is constant across all observations. Rarely holds exactly for revenue data.

**Intercept (β₀):** In OLS, the value of the fitted line when year = 0 (theoretical). Interpretation: `intercept = mean(sales) - slope × mean(year)`.

**L (Level):** In Holt's method, the smoothed estimate of the current revenue level. Updated each year using α.

**LWMA:** Linearly Weighted Moving Average. Another name for the Weighted Average Growth model.

**MAPE:** Mean Absolute Percentage Error. Average of absolute percentage misses across holdout years. Lower = better.

**MSE:** Mean Squared Error = RMSE². Decomposed as: Bias² + Variance.

**Model:** A mathematical rule that takes historical revenue as input and produces a forecast as output. This system runs 6 models in parallel.

**nsmallest(k, col):** Pandas method to efficiently return the k rows with the smallest values in column `col`. Used to select top 3 models by MAPE.

**Normal Equations:** The system of linear equations `XᵀX β = Xᵀy` whose solution gives OLS estimates. For simple linear regression, they reduce to the slope/intercept formulas.

**OLS:** Ordinary Least Squares. The method of finding regression coefficients by minimizing the sum of squared residuals.

**Outlier:** A year whose year-over-year growth rate has absolute z-score > 2.0. Excluded from model fitting.

**p-value:** From `scipy.stats.linregress` — the two-tailed p-value for testing H₀: slope = 0. Not displayed in the UI but available in the raw payload.

**pct_change():** Pandas method computing (current - previous) / previous. Equivalent to growth rate formula. Returns NaN for the first row.

**Percentile:** The value below which a given percentage of observations fall. Computed with linear interpolation by `numpy.percentile`.

**Prediction Interval:** Interval expected to contain a new future observation with a specified probability. Wider than a confidence interval because it includes both parameter uncertainty AND residual variance. The shaded band on the Linear Regression chart is a 90% Prediction Interval.

**R² (R-squared):** Coefficient of Determination. = 1 - RSS/TSS. Proportion of variance in y explained by the model. Range [0, 1] for well-fitted models.

**Residual:** Difference between actual and fitted value: `ε̂_t = sales_t - ŷ_t`.

**RSS:** Residual Sum of Squares = `Σ(salesᵢ - ŷᵢ)²`. OLS minimizes this.

**RMSE:** Root Mean Squared Error. Square root of MSE. Units = revenue (billions). Penalizes large errors more than MAPE.

**Scenarios:** Three revenue paths (Pessimistic, Baseline, Optimistic) derived from 25th, 50th, 75th percentiles of historical growth rates. Not model outputs — planning bounds.

**Simple Exponential Smoothing (SES):** ETS(A,N,N) — exponential smoothing without a trend component. This code uses SES for the level, then adds a trend via backward difference.

**Slope (β₁):** In OLS, the change in revenue per unit change in year. Units: billions of USD per year. Positive = growing; negative = declining.

**Standard error (of slope):** `SE(β̂₁) = σ̂ / sqrt(Sxx)`. Returned by `scipy.stats.linregress` as `stderr`.

**Stationarity:** A time series property where mean, variance, and autocorrelation are constant over time. Revenue is non-stationary (trending). Growth rates are closer to stationary for stable companies.

**Sxx:** Sum of squared deviations of x from its mean: `Σ(yearᵢ - x̄)²`. Appears in OLS slope formula.

**Training data:** The clean revenue series (after outlier removal) used to fit model parameters.

**Trend (in Holt's method):** The smoothed estimate of the annual revenue change direction. Updated each year using β.

**TSS:** Total Sum of Squares = `Σ(salesᵢ - ȳ)²`. Used in R² computation.

**Variance reduction:** The statistical principle that averaging independent models reduces forecast variance. The theoretical basis for ensembling.

**Walk-forward validation:** A more rigorous version of backtesting where the train/test split is moved forward one period at a time, producing multiple holdout evaluations.

**Weighted Mean:** `Σ(wᵢ × xᵢ) / Σ(wᵢ)`. The weighted average growth formula.

**WMA:** Weighted Moving Average. Another name for the Weighted Average Growth model.

**z-score:** Standardized value: `z = (x - μ) / σ`. Used in outlier detection to identify extreme growth rate years.

---

## 18. Statistical Theorems and Principles Referenced

### Gauss-Markov Theorem (1821/1900)

**Statement:** Given the four classical OLS assumptions, the OLS estimator is BLUE — Best Linear Unbiased Estimator.

**Relevance here:** Provides theoretical justification for using OLS as the linear model. Even when assumptions are mildly violated (which they are for revenue), OLS remains computationally efficient and interpretable.

**Assumptions (which may be violated in practice):**
1. Linearity: `y = Xβ + ε`
2. Strict exogeneity: `E[ε | X] = 0` — errors are uncorrelated with predictors. Violated if revenue has autocorrelated structure.
3. No perfect multicollinearity: `rank(X) = k`. Not an issue for simple linear regression.
4. Homoscedasticity: `Var(ε | X) = σ²I`. Often violated for growing revenue (larger companies have larger absolute errors).

### AM-GM Inequality

**Statement:** For non-negative real numbers a₁, ..., aₙ:
```
(a₁ + a₂ + ... + aₙ) / n ≥ (a₁ × a₂ × ... × aₙ)^(1/n)
```
with equality iff all aᵢ are equal.

**Relevance:** CAGR (geometric mean of growth factors) is always ≤ arithmetic mean of growth rates. Explains why CAGR and simple mean diverge for volatile series.

### Law of Large Numbers (Bernoulli, 1713)

**Statement:** The sample mean converges to the true population mean as sample size grows.

**Relevance:** The mean growth rate computed from 15+ years of history is more reliable than from 3–4 years. Short series produce unreliable model fits.

### Bias-Variance Decomposition

**Statement:**
```
E[(ŷ - y)²] = Bias(ŷ)² + Var(ŷ) + σ²

where:
  Bias(ŷ)  = E[ŷ] - E[y]       (systematic error)
  Var(ŷ)   = variance of predictions across repeated samples
  σ²       = irreducible noise
```

**Relevance:** Ensemble reduces variance. MA Trend (high variance, potential low bias for trending companies) is offset by CAGR (lower variance, potential bias from endpoint selection).

### Bates & Granger Forecast Combination (1969)

**Statement:** "The Combination of Forecasts" (Operations Research Quarterly, 1969) showed that combining two forecasts with uncorrelated errors produces a combined forecast with lower mean squared error than either individual forecast.

**Relevance:** Theoretical foundation for using an ensemble of models rather than the single best model.

### Gauss's Triangular Number Formula

```
1 + 2 + 3 + ... + n = n(n+1)/2
```

**Relevance:** Denominator of the Weighted Average Growth formula. Computed implicitly by `np.average(weights=...)`.

---

## 19. Frequently Asked Questions from a Technical Audience

**Q: Why not use ARIMA or Prophet?**
A: The series are annual (5–20 data points). ARIMA requires sufficient observations for parameter estimation (typically 30+). Prophet is designed for sub-annual data with seasonal patterns. For annual series this short, the classical statistical models used here are more robust and interpretable.

**Q: Why is α fixed at 0.3 instead of estimated from data?**
A: On series with 5–15 data points, MLE estimation of α often overfits. A fixed α = 0.3 is a common industry default (used in many ERP systems) that generalizes well. Full MLE would require `scipy.optimize.minimize` on the SSE objective, which adds complexity and can converge to local minima for short series.

**Q: Is the 90% Prediction Interval actually calibrated?**
A: The PI uses z = 1.645 (normal approximation) and applies the standard OLS prediction interval formula. For the PI to be exactly 90%, the residuals would need to be normally distributed with constant variance — assumptions that rarely hold for revenue data. The interval is an approximation and should be interpreted directionally rather than as a precise probability statement.

**Q: Why is outlier detection done on growth rates, not revenue levels?**
A: Revenue levels are non-stationary (they trend upward). Z-scoring a non-stationary series is not meaningful — a value from 2006 cannot be compared to a value from 2022 on the same z-score scale. Growth rates are closer to stationary (mean-reverting around the company's long-run growth rate), so z-scoring them is more meaningful.

**Q: What happens if a company has 0 or negative revenue in one year?**
A: The growth rate computation `(salesₜ - salesₜ₋₁) / salesₜ₋₁` is undefined when salesₜ₋₁ = 0 (division by zero). The code does not explicitly handle this case — it would produce NaN or Inf in the growth rate array, which propagates into the outlier detection. In practice, the database never contains zero or negative total revenue rows for the companies in scope (all are large public retailers with strictly positive revenue).

**Q: Why use holdout = 2 years, not more?**
A: With 8–15 years of clean data per company, using 3+ holdout years would leave only 5–12 years for training — too few for reliable OLS or Holt fits. 2 years is the minimum meaningful holdout that tests both next-year and 2-year-out accuracy.

**Q: Does the ensemble always outperform the best individual model on holdout data?**
A: No — in individual cases, the best model can outperform the ensemble (this is possible by definition, since the ensemble includes models that are weaker). However, across a portfolio of companies and future periods, ensembles are statistically expected to outperform (Bates & Granger, 1969; M4 Competition, 2018).

**Q: What does `np.round(results[top3].mean(axis=1), 1)` do exactly?**
A: `results[top3]` selects the three columns (one per top model) from the forecast DataFrame. `.mean(axis=1)` computes the row-wise (year-wise) arithmetic mean. `np.round(..., 1)` rounds to 1 decimal place in billions. So for each forecast year, the ensemble is the average of the three model forecasts for that year.

**Q: How is the source (SEC vs YFinance) determined for a given ticker?**
A: `get_company_source(ticker)` from `app/data/source_router.py` queries the `coreiq_companies` table for the `source` column. If no source is found, the service falls back to trying SEC first, then YFinance.

**Q: Is there any risk of data leakage in the backtesting setup?**
A: The backtest correctly splits the data temporally: `train = df.iloc[:-holdout_years]`. Models are trained only on `train` and predict `test`. There is no data leakage. The outlier detection runs on the full series (including holdout years), which is a minor form of information leakage — the outlier flag for a holdout year could influence which years are in the training set. In practice this matters only if the holdout years themselves are outliers, which is possible but not corrected for.

**Q: What Python version and library versions are expected?**
A: The code uses:
- `scipy.stats.linregress` — available since scipy 0.1, stable API
- `numpy.percentile` with default `method='linear'` — stable since numpy 1.0; the `method` parameter name changed in numpy 1.22 (previously `interpolation`)
- `pandas.DataFrame.nsmallest` — available since pandas 0.17.0
- `streamlit.cache_data` — available since Streamlit 1.18.0 (replaced `cache` decorator)

**Q: Could two models tie on MAPE and cause a non-deterministic ensemble selection?**
A: `nsmallest(3, "mape")` returns rows in original DataFrame order when MAPE values are tied. The order of the `_methods` list in `backtest()` is fixed: linear, cagr, exp_smoothing, holt, ma_trend, weighted_avg. So ties are broken by this order — the model appearing earlier in the list wins. This is deterministic.
