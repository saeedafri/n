# 12 Real Examples You Can Match In UI

This file uses real values from the current STG-backed Revenue Estimates page.

The purpose is simple:

- open the page
- look at the numbers
- match them to the docs

## Example 1: ANF

Company:

- `Abercrombie & Fitch Co. (ANF)`

Source:

- `SEC`

Recent actual revenue from the page:

| Fiscal year | Revenue in billions | Growth % |
| --- | --- | --- |
| 2021 | 3.125 | -13.7% |
| 2022 | 3.713 | 18.8% |
| 2023 | 3.698 | -0.4% |
| 2024 | 4.281 | 15.8% |
| 2025 | 4.949 | 15.6% |
| 2026 | 5.266 | 6.4% |

What the page currently shows for 2027:

| Model | 2027 forecast in billions |
| --- | --- |
| Linear Regression | 4.4 |
| CAGR | 5.4 |
| Exponential Smoothing | 4.8 |
| Holt's Linear Trend | 4.9 |
| Moving Average Trend | 5.9 |
| Weighted Average Growth | 5.5 |
| Ensemble | 5.6 |
| Pessimistic Scenario | 5.0 |
| Baseline Scenario | 5.5 |
| Optimistic Scenario | 6.1 |

Backtest scores from the page:

| Model | MAPE | Bias | RMSE |
| --- | --- | --- | --- |
| MA Trend | 2.25 | -0.1 | 0.1 |
| Weighted Avg | 12.87 | -0.7 | 0.7 |
| CAGR | 13.07 | -0.7 | 0.7 |
| Holt's Linear | 16.96 | -0.9 | 0.9 |
| Exp Smoothing | 19.59 | -1.0 | 1.0 |
| Linear Regression | 24.42 | -1.3 | 1.3 |

### Why MA Trend came out highest

Look at the last 3 growth values:

- 2024 growth = 15.8%
- 2025 growth = 15.6%
- 2026 growth = 6.4%

Average of those recent growth values is roughly:

```text
(15.8 + 15.6 + 6.4) / 3 = 12.6%
```

Apply that to the latest actual revenue:

```text
5.266B * 1.126 = about 5.93B
```

Rounded on the page:

```text
5.9B
```

That is why the MA Trend line is high.

### Why Linear Regression is lower

Linear Regression fits one straight line across the full history.

ANF's recent jump is stronger than its long-run straight-line trend, so the straight-line model ends up below the latest actual momentum.

That is why the 2027 Linear Regression forecast is only:

```text
4.4B
```

## Example 2: ADS

Company:

- `adidas AG (ADS)`

Source:

- `YFinance`

Recent actual revenue from the page:

| Fiscal year | Revenue in billions | Growth % |
| --- | --- | --- |
| 2021 | 21.234 | — |
| 2022 | 22.511 | 6.0% |
| 2023 | 21.427 | -4.8% |
| 2024 | 23.683 | 10.5% |
| 2025 | 24.811 | 4.8% |

What the page currently shows for 2026:

| Model | 2026 forecast in billions |
| --- | --- |
| Linear Regression | 25.2 |
| CAGR | 25.8 |
| Exponential Smoothing | 23.8 |
| Holt's Linear Trend | 26.3 |
| Moving Average Trend | 25.7 |
| Weighted Average Growth | 26.0 |
| Ensemble | 25.7 |
| Pessimistic Scenario | 25.4 |
| Baseline Scenario | 26.1 |
| Optimistic Scenario | 26.6 |

Backtest scores from the page:

| Model | MAPE | Bias | RMSE |
| --- | --- | --- | --- |
| Holt's Linear | 2.65 | 0.6 | 0.6 |
| Linear Regression | 9.37 | -2.3 | 2.3 |
| MA Trend | 10.79 | -2.6 | 2.7 |
| CAGR | 10.98 | -2.7 | 2.7 |
| Exp Smoothing | 11.38 | -2.8 | 2.8 |
| Weighted Avg | 13.16 | -3.2 | 3.3 |

### Why Holt's Linear won here

ADS has a shorter and bumpier recent history:

- up
- down
- up again

Holt's method tracks:

- current level
- current trend

separately.

That often helps on shorter, changing series.

That is why Holt's Linear is the best backtested model on the current ADS page.

## How to use this file while presenting

Use this order:

1. show the actual history row values
2. show the 1-year-ahead forecasts
3. explain why different models disagree
4. show backtest to explain why one model is preferred
