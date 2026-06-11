# Complete Flow Diagrams

[← Back to Overview](../README.md)

---

## Master Pipeline Flow

This diagram shows the **complete end-to-end execution** when you call `run_full_analysis()`.

```mermaid
flowchart TD
    START([User calls RetailerForecaster - file.xlsx]) --> INIT

    subgraph INIT["Stage 1: Initialization & Data Loading"]
        A1[load_data] --> A2{File is .xlsx/.xls?}
        A2 -->|Yes| A3[pd.read_excel]
        A2 -->|No| A3X[❌ ValueError]
        A3 --> A4[_prepare_data]
        A4 --> A5[Auto-detect date column]
        A5 --> A6[Auto-detect sales column]
        A6 --> A7[Convert to year + numeric sales]
        A7 --> A8[dropna → dedupe → sort → reset]
        A8 --> A9[self.clean_data ready]
    end

    A9 --> OUTLIER

    subgraph OUTLIER["Stage 2: Outlier Detection"]
        B1[Calculate YoY growth rates]
        B1 --> B2[Compute mean & std of growth]
        B2 --> B3[Z-score for each year]
        B3 --> B4{"|z| > 2.0?"}
        B4 -->|Yes| B5[Add to self.outliers]
        B4 -->|No| B6[Keep as normal]
        B5 --> B7[Create data_excluding_outliers]
        B6 --> B7
    end

    B7 --> ANALYSIS([run_full_analysis called])

    ANALYSIS --> STATS

    subgraph STATS["Stage 3: Summary Statistics"]
        C1[Calculate CAGR]
        C1 --> C2[Calculate avg/median growth]
        C2 --> C3[Calculate volatility, min, max]
        C3 --> C4[Print summary report]
    end

    C4 --> BACKTEST

    subgraph BACKTEST["Stage 5: Backtesting"]
        D1["Split: Train (N-2 yrs) / Test (2 yrs)"]
        D1 --> D2[Run all 6 models on train data]
        D2 --> D3[Predict holdout years]
        D3 --> D4[Calculate MAPE, Bias, RMSE]
        D4 --> D5[Rank by lowest MAPE]
        D5 --> D6[self.best_method selected]
    end

    D6 --> FORECAST

    subgraph FORECAST["Stages 4+6: Forecasting & Ensemble"]
        E1[Run all 6 models on FULL data]
        E1 --> E2[Select top 3 from backtest]
        E2 --> E3[Ensemble = avg of top 3]
        E3 --> E4[Calculate P25/P50/P75 scenarios]
        E4 --> E5[self.final_forecast DataFrame]
    end

    E5 --> GROWTH[Calculate implied YoY growth rates]

    GROWTH --> OUTPUT

    subgraph OUTPUT["Stage 7: Export & Visualization"]
        F1["export_results() → 4-sheet Excel"]
        F2["plot() → matplotlib chart"]
    end

    OUTPUT --> DONE([Analysis Complete - Return results dict])
```

---

## Data Transformation Flow

Shows how data is transformed at each stage.

```mermaid
flowchart LR
    A["Raw Excel
    ┌─────────────────┐
    │ Fiscal Year│ Rev │
    │ 2015-12-31│3445 │
    │ 2016-12-31│3546 │
    │ ...       │...  │
    └─────────────────┘"] 
    
    --> B["Clean Data
    ┌────────────┐
    │ year│sales │
    │ 2015│3445  │
    │ 2016│3546  │
    │ ...│...    │
    └────────────┘"]
    
    --> C["+ Outlier Flags
    ┌─────────────────┐
    │ year│sales│outl │
    │ 2020│3450│ ⚠   │
    │ 2021│4549│     │
    └─────────────────┘"]
    
    --> D["+ Growth Rates
    ┌──────────────────┐
    │ year│sales│growth│
    │ 2016│3546│ +2.9%│
    │ 2017│3616│ +2.0%│
    └──────────────────┘"]
    
    --> E["Forecasts
    ┌──────────────────────────┐
    │ year│ensemble│pessimistic│
    │ 2023│4549.1 │  4862.4   │
    │ 2024│4733.4 │  4930.7   │
    └──────────────────────────┘"]
```

---

## Model Competition Flow

How the 6 models compete in backtesting.

```mermaid
flowchart TD
    DATA["Training Data (2015-2020)"] --> L[Linear Regression]
    DATA --> C[CAGR]
    DATA --> ES[Exp Smoothing]
    DATA --> H[Holt's Method]
    DATA --> MA[MA Trend]
    DATA --> WA[Weighted Avg]

    L --> LP["Predict 2021-22
    → MAPE: 17.41%"]
    C --> CP["Predict 2021-22
    → MAPE: 26.07%"]
    ES --> ESP["Predict 2021-22
    → MAPE: 24.48%"]
    H --> HP["Predict 2021-22
    → MAPE: 14.36%"]
    MA --> MAP["Predict 2021-22
    → MAPE: 27.31%"]
    WA --> WAP["Predict 2021-22
    → MAPE: 28.22%"]

    LP --> RANK[Rank by MAPE]
    CP --> RANK
    ESP --> RANK
    HP --> RANK
    MAP --> RANK
    WAP --> RANK

    RANK --> TOP3["Top 3:
    1. Holt (14.36%)
    2. Linear (17.41%)
    3. Exp Smooth (24.48%)"]

    TOP3 --> ENSEMBLE["Ensemble = Average of Top 3"]
```

---

## Class Attribute Lifecycle

When each attribute gets set during execution.

```mermaid
sequenceDiagram
    participant U as User
    participant RF as RetailerForecaster
    participant D as Data Pipeline

    U->>RF: RetailerForecaster('file.xlsx')
    RF->>D: load_data()
    D-->>RF: self.raw_data ✓
    RF->>D: _prepare_data()
    D-->>RF: self.clean_data ✓
    RF->>D: _detect_outliers()
    D-->>RF: self.outliers ✓
    D-->>RF: self.data_excluding_outliers ✓

    U->>RF: run_full_analysis()
    RF->>RF: summary_stats()
    RF->>RF: backtest()
    RF-->>RF: self.backtest_results ✓
    RF-->>RF: self.best_method ✓
    RF->>RF: forecast()
    RF-->>RF: self.forecasts ✓
    RF-->>RF: self.final_forecast ✓
    RF-->>U: Return results dict

    U->>RF: export_results()
    RF-->>U: forecast_results.xlsx saved

    U->>RF: plot()
    RF-->>U: Chart displayed/saved
```

---

## Decision Points in the Pipeline

```mermaid
flowchart TD
    D1{"File extension?"}
    D1 -->|.xlsx/.xls| OK1[Continue]
    D1 -->|Other| FAIL1[ValueError]

    D2{"Date column found?"}
    D2 -->|Keyword match| OK2[Use matched column]
    D2 -->|No match| FB2[Use first column]

    D3{"Sales column found?"}
    D3 -->|Keyword match| OK3[Use matched column]
    D3 -->|No match| FB3[Use second column]

    D4{"datetime parse worked?"}
    D4 -->|Yes| OK4[Extract .dt.year]
    D4 -->|All NaN| FB4[Try pd.to_numeric]

    D5{"std_growth > 0?"}
    D5 -->|Yes| OK5[Run z-score outlier detection]
    D5 -->|No| FB5[Skip outlier detection]

    D6{"Enough data for holdout?"}
    D6 -->|len > holdout+2| OK6[Use full holdout]
    D6 -->|Too little| FB6[Reduce to 1-year holdout]

    D7{"Backtest results exist?"}
    D7 -->|Yes| OK7[Top 3 by MAPE for ensemble]
    D7 -->|No| FB7["Default: holt, cagr, weighted_avg"]

    D8{"matplotlib available?"}
    D8 -->|Yes| OK8[Generate chart]
    D8 -->|No| FB8[Print warning, skip]
```

---

## Object State at Each Stage

```
After __init__():
  ┌──────────────────────────────────────────┐
  │ raw_data            = DataFrame (raw)    │
  │ clean_data          = DataFrame (clean)  │
  │ data_excluding_out  = DataFrame          │
  │ outliers            = [2020] or []       │
  │ forecasts           = {}                 │ ← empty
  │ backtest_results    = {}                 │ ← empty
  │ best_method         = None               │
  │ final_forecast      = None               │
  └──────────────────────────────────────────┘

After run_full_analysis():
  ┌──────────────────────────────────────────┐
  │ raw_data            = DataFrame (raw)    │
  │ clean_data          = DataFrame (clean)  │
  │ data_excluding_out  = DataFrame          │
  │ outliers            = [2020] or []       │
  │ forecasts           = {6 model results}  │ ← populated
  │ backtest_results    = DataFrame          │ ← populated
  │ best_method         = 'holt'             │ ← selected
  │ final_forecast      = DataFrame          │ ← populated
  └──────────────────────────────────────────┘
```

---

## Error Handling Paths

```mermaid
flowchart TD
    E1["File not .xlsx/.xls"] --> R1["ValueError raised"]
    E2["Column auto-detect fails"] --> R2["Falls back to position 0/1"]
    E3["Date parsing fails"] --> R3["Falls back to numeric conversion"]
    E4["std_growth = 0"] --> R4["Outlier detection skipped"]
    E5["Not enough data for holdout"] --> R5["Reduce holdout to 1"]
    E6["A model crashes in backtest"] --> R6["Skip that model, continue"]
    E7["matplotlib not installed"] --> R7["Print warning, skip chart"]
```

---

[← Back to Overview](../README.md)
