# CIQ Estimates — Retailer Annual Revenue Forecasting System

## Complete End-to-End Documentation

> **For technical reviewers (data scientists, ML engineers):** Go directly to the [**TECHNICAL_REFERENCE_COMPLETE**](revenue-estimates-models/TECHNICAL_REFERENCE_COMPLETE.md) — covers every formula, every line of code, every statistical theorem, and 13 technical FAQ.
>
> **For business / new team members:** Start with [Stage 1](stages/stage_1_data_loading.md) or [00 Start Here](revenue-estimates-models/00_start_here_for_new_people.md).

---

## What Is This Script?

This script (`CIQ_Estimates.ipynb.aspx`) is a **self-contained revenue forecasting system** for retail companies. It replaces the manual Capital IQ (CapIQ) estimates workflow with an automated, reproducible Python pipeline.

**In plain English**: You give it an Excel file with historical yearly sales data for a retailer, and it:
1. Cleans and validates the data
2. Detects unusual years (outliers like COVID-2020)
3. Runs **6 different forecasting models** on the data
4. Tests which model is most accurate (backtesting)
5. Combines the best models into a single "ensemble" forecast
6. Generates pessimistic/baseline/optimistic scenarios
7. Exports everything to Excel and generates charts

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    RetailerForecaster Class                         │
│                                                                     │
│  ┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌────────────┐  │
│  │  Stage 1  │──▶│  Stage 2  │──▶│   Stage 3    │──▶│  Stage 4   │  │
│  │  Data     │   │  Outlier  │   │   Summary    │   │ Forecasting│  │
│  │  Loading  │   │ Detection │   │   Stats      │   │  Models    │  │
│  └──────────┘   └──────────┘   └──────────────┘   └─────┬──────┘  │
│                                                          │          │
│  ┌──────────┐   ┌──────────┐   ┌──────────────┐         │          │
│  │  Stage 7  │◀──│  Stage 6  │◀──│   Stage 5    │◀────────┘          │
│  │  Export & │   │ Ensemble  │   │  Backtesting │                    │
│  │  Visualize│   │ & Scenario│   │  & Selection │                    │
│  └──────────┘   └──────────┘   └──────────────┘                    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Stages Overview

| Stage | Name | What It Does | Key Method(s) |
|-------|------|-------------|----------------|
| **1** | [Data Loading & Preparation](stages/stage_1_data_loading.md) | Reads Excel, identifies columns, extracts years, cleans data | `load_data()`, `_prepare_data()` |
| **2** | [Outlier Detection](stages/stage_2_outlier_detection.md) | Finds anomalous years using z-score on growth rates | `_detect_outliers()` |
| **3** | [Summary Statistics](stages/stage_3_summary_statistics.md) | Calculates CAGR, growth volatility, ranges | `summary_stats()` |
| **4** | [Forecasting Models (6 Methods)](stages/stage_4_forecasting_models.md) | Runs Linear, CAGR, Exp Smoothing, Holt, MA Trend, Weighted Avg | `_forecast_*()` methods |
| **5** | [Backtesting & Model Selection](stages/stage_5_backtesting.md) | Tests each model's accuracy on held-out data, picks the best | `backtest()` |
| **6** | [Ensemble & Scenario Analysis](stages/stage_6_ensemble_scenarios.md) | Blends top models, creates pessimistic/baseline/optimistic | `forecast()` |
| **7** | [Export & Visualization](stages/stage_7_export_visualization.md) | Writes Excel workbook, generates matplotlib chart | `export_results()`, `plot()` |

---

## Quick Start (How To Use)

```python
# 1. Initialize with your Excel file
forecaster = RetailerForecaster('Retailer.xlsx')

# 2. Run the complete analysis pipeline
results = forecaster.run_full_analysis(forecast_periods=5)

# 3. Export to Excel
forecaster.export_results('forecast_output.xlsx')

# 4. Generate chart
forecaster.plot(save_path='forecast_chart.png')
```

### Input File Requirements

Your Excel file needs at minimum **two columns**:

| Column Type | Auto-Detected Names | Example |
|-------------|---------------------|---------|
| **Date/Year** | `date`, `year`, `period`, `time` (or first column) | 2015, 2016, ... |
| **Sales/Revenue** | `sales`, `revenue`, `amount`, `value` (or second column) | 3445, 3546, ... |

### Sample Input Data

| Year | Sales |
|------|-------|
| 2015 | 3,445 |
| 2016 | 3,546 |
| 2017 | 3,616 |
| 2018 | 3,951 |
| 2019 | 3,984 |
| 2020 | 3,450 |
| 2021 | 4,549 |
| 2022 | 4,795 |

### Sample Output

| Year | Ensemble | Pessimistic | Baseline | Optimistic |
|------|----------|-------------|----------|------------|
| 2023 | 4,549.1 | 4,862.4 | 4,935.6 | 5,146.8 |
| 2024 | 4,733.4 | 4,930.7 | 5,080.3 | 5,524.3 |
| 2025 | 4,917.7 | 4,999.9 | 5,229.2 | 5,929.6 |
| 2026 | 5,102.0 | 5,070.1 | 5,382.5 | 6,364.6 |
| 2027 | 5,286.3 | 5,141.4 | 5,540.3 | 6,831.5 |

---

## Dependencies

| Package | Purpose | Required? |
|---------|---------|-----------|
| `pandas` | Data manipulation, Excel I/O | Yes |
| `numpy` | Numerical computations | Yes |
| `scipy.stats` | Linear regression, statistics | Yes |
| `matplotlib` | Chart generation | Optional |
| `openpyxl` | Excel export engine | Yes (for export) |

---

## Complete Data Flow Diagram

See [diagrams/complete_flow.md](diagrams/complete_flow.md) for the full Mermaid-based flow diagrams.

---

## Navigation

- **[Stage 1: Data Loading & Preparation →](stages/stage_1_data_loading.md)**
- **[Stage 2: Outlier Detection →](stages/stage_2_outlier_detection.md)**
- **[Stage 3: Summary Statistics →](stages/stage_3_summary_statistics.md)**
- **[Stage 4: Forecasting Models →](stages/stage_4_forecasting_models.md)**
- **[Stage 5: Backtesting & Model Selection →](stages/stage_5_backtesting.md)**
- **[Stage 6: Ensemble & Scenario Analysis →](stages/stage_6_ensemble_scenarios.md)**
- **[Stage 7: Export & Visualization →](stages/stage_7_export_visualization.md)**
- **[Complete Flow Diagrams →](diagrams/complete_flow.md)**
