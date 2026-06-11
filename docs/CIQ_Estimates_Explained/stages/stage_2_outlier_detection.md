# Stage 2: Outlier Detection

[← Stage 1: Data Loading](stage_1_data_loading.md) | [Back to Overview](../README.md) | [Next: Stage 3 — Summary Statistics →](stage_3_summary_statistics.md)

---

## Purpose

Retail sales data often contains **anomalous years** — for example, COVID-19 in 2020 caused massive drops, while 2021 saw unusually high recovery growth. These outliers can **distort forecasting models** because the model tries to "fit" the anomaly rather than the actual trend.

This stage **automatically identifies** those outlier years so they can be excluded from forecasting while still being kept in the historical record.

---

## Flow Diagram

```mermaid
flowchart TD
    A[self.clean_data from Stage 1] --> B[Calculate YoY growth rates]
    B --> C[Compute mean growth rate]
    C --> D[Compute std deviation of growth rates]
    D --> E{std_growth > 0?}
    E -->|No - all same growth| F[No outliers detected]
    E -->|Yes| G[Calculate z-score for each year]
    G --> H{|z-score| > threshold?}
    H -->|Yes| I[Mark as outlier]
    H -->|No| J[Mark as normal]
    I --> K[Store outlier years in self.outliers]
    J --> K
    K --> L[Create self.data_excluding_outliers]
    L --> M[Continue to Stage 3+]
```

---

## The Math Behind It

### Step 1: Calculate Year-over-Year (YoY) Growth Rates

```python
df['growth'] = df['sales'].pct_change()
```

For each year, compute:

$$\text{Growth}_t = \frac{\text{Sales}_t - \text{Sales}_{t-1}}{\text{Sales}_{t-1}}$$

**Example with sample data:**

| Year | Sales | Growth Rate |
|------|-------|-------------|
| 2015 | 3,445 | — (first year, no prior) |
| 2016 | 3,546 | (3546 - 3445) / 3445 = **+2.9%** |
| 2017 | 3,616 | (3616 - 3546) / 3546 = **+2.0%** |
| 2018 | 3,951 | (3951 - 3616) / 3616 = **+9.3%** |
| 2019 | 3,984 | (3984 - 3951) / 3951 = **+0.8%** |
| 2020 | 3,450 | (3450 - 3984) / 3984 = **-13.4%** |
| 2021 | 4,549 | (4549 - 3450) / 3450 = **+31.9%** |
| 2022 | 4,795 | (4795 - 4549) / 4549 = **+5.4%** |

---

### Step 2: Calculate Mean and Standard Deviation

```python
mean_growth = df['growth'].mean()
std_growth = df['growth'].std()
```

From the example:
- **Mean growth** = (2.9 + 2.0 + 9.3 + 0.8 - 13.4 + 31.9 + 5.4) / 7 = **5.56%**
- **Std deviation** ≈ **14.2%** (high volatility due to COVID swing)

---

### Step 3: Calculate Z-Scores

```python
df['z_score'] = (df['growth'] - mean_growth) / std_growth
```

The z-score tells you **how many standard deviations** a year's growth is from the average:

$$z_t = \frac{\text{Growth}_t - \mu_{\text{growth}}}{\sigma_{\text{growth}}}$$

**Example z-scores:**

| Year | Growth | Z-Score | Interpretation |
|------|--------|---------|----------------|
| 2015 | — | — | No growth (first year) |
| 2016 | +2.9% | -0.19 | Very normal |
| 2017 | +2.0% | -0.25 | Very normal |
| 2018 | +9.3% | +0.26 | Slightly above average |
| 2019 | +0.8% | -0.33 | Slightly below average |
| 2020 | -13.4% | **-1.33** | Below average but within threshold |
| 2021 | +31.9% | **+1.85** | High but just within threshold |
| 2022 | +5.4% | -0.01 | Almost exactly average |

---

### Step 4: Apply Threshold

```python
outlier_mask = df['z_score'].abs() > threshold  # default threshold = 2.0
self.outliers = df.loc[outlier_mask, 'year'].tolist()
```

The default threshold is **2.0 standard deviations**. Any year with |z-score| > 2.0 is marked as an outlier.

**In our example**: No outliers detected (highest |z| = 1.85 for 2021, which is < 2.0).

**But if threshold were 1.5**: 2021 would be flagged as an outlier (|z| = 1.85 > 1.5).

---

### Step 5: Create Clean Dataset

```python
self.data_excluding_outliers = self.clean_data[
    ~self.clean_data['year'].isin(self.outliers)
].copy()
```

Creates a second DataFrame that removes the outlier years. This dataset is used for forecasting by default.

---

## Why Z-Score Method?

| Method | Pros | Cons |
|--------|------|------|
| **Z-Score (used here)** | Simple, well-understood, works with small datasets | Assumes roughly normal distribution of growth rates |
| IQR Method | More robust to extreme outliers | Needs more data points |
| Modified Z-Score (MAD) | Better for skewed data | More complex |

The z-score approach was chosen because:
- Retail growth rates are roughly symmetric (can go up or down)
- Datasets are small (5-15 years), so complex methods aren't beneficial
- The threshold (2.0) is easily adjustable

---

## Visual Example

```
Growth Rates Distribution:

     ◄── outlier zone ──►    Normal Zone    ◄── outlier zone ──►
     
  |         |                |    ▓▓▓▓    |                |         |
  |         |           ▓▓▓▓|▓▓▓▓▓▓▓▓▓▓▓▓|▓▓▓▓           |         |
  |         |      ▓▓▓▓▓▓▓▓▓|▓▓▓▓▓▓▓▓▓▓▓▓|▓▓▓▓▓▓▓▓▓      |         |
  +---------+---------+-----+----+----+----+-----+---------+---------+
  -3σ      -2σ       -1σ        μ         +1σ        +2σ       +3σ
                              (5.56%)
  
  2020 (-13.4%) → z = -1.33 → Inside normal zone ✓
  2021 (+31.9%) → z = +1.85 → Inside normal zone ✓ (barely)
```

---

## Configuring the Threshold

The threshold of `2.0` is hardcoded in the method signature:

```python
def _detect_outliers(self, threshold: float = 2.0) -> None:
```

**Guidelines for threshold selection:**

| Threshold | Sensitivity | Best For |
|-----------|-------------|----------|
| **1.5** | High — catches more outliers | Stable retailers with consistent growth |
| **2.0** | Medium (default) — balanced | Most use cases |
| **2.5** | Low — only extreme outliers | Volatile retailers (e.g., fashion, luxury) |
| **3.0** | Very low | Almost nothing gets flagged |

---

## What Gets Stored After Stage 2

| Attribute | Type | Content |
|-----------|------|---------|
| `self.outliers` | List[int] | Years flagged as outliers, e.g., `[2020]` |
| `self.data_excluding_outliers` | DataFrame | Clean data minus outlier years |

---

## Important Notes

1. **Outliers are NOT deleted** — they stay in `self.clean_data`. They're only excluded from `self.data_excluding_outliers` which is used for forecasting
2. **The first year never has a growth rate** (no prior year to compare), so it can never be flagged as an outlier
3. **If all growth rates are identical** (std = 0), no outlier detection runs — prevents division by zero
4. **COVID-19 (2020)** is the most common outlier in retail data. The system handles this automatically

---

[← Stage 1: Data Loading](stage_1_data_loading.md) | [Back to Overview](../README.md) | [Next: Stage 3 — Summary Statistics →](stage_3_summary_statistics.md)
