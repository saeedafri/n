# 01 System Flow

This file explains the complete end-to-end runtime flow for the Revenue Estimates page.

## End-to-end architecture

```mermaid
flowchart TD
    A[User opens Revenue Estimates] --> B[Load company list]
    B --> C[Read coreiq_companies]
    C --> D[Build dropdown labels: name_coresight plus ticker]
    A --> E[User selects ticker]
    E --> F[Resolve source from coreiq_companies.source]
    F --> G{Source type}
    G -->|SEC| H[Query annual revenue from coreiq_av_financials_income_statement]
    G -->|YFinance| I[Query annual revenue from coreiq_yf_financials_income_statement]
    H --> J[Normalize rows]
    I --> J
    J --> K[Build annual year and sales series]
    K --> L[Detect outlier growth years]
    L --> M[Run summary statistics]
    M --> N[Run six forecast models]
    N --> O[Run backtest]
    O --> P[Pick best model by minimum MAPE]
    O --> Q[Pick top 3 models]
    N --> R[Generate model forecast table]
    Q --> S[Average top 3 into ensemble]
    K --> T[Generate scenario percentiles]
    R --> U[Render charts and tables]
    S --> U
    T --> U
    P --> U
```

## Runtime sequence

```mermaid
sequenceDiagram
    participant U as User
    participant P as Revenue Estimates Page
    participant S as RevenueForecastService
    participant D as STG DB
    participant F as RetailerForecaster

    U->>P: Open page
    P->>S: get_companies()
    S->>D: Read coreiq_companies and revenue coverage
    D-->>S: Company list
    S-->>P: Dropdown items

    U->>P: Select ticker
    P->>S: get_company_dashboard(ticker)
    S->>D: Resolve source and read actual annual rows
    D-->>S: Raw actuals
    S->>S: Normalize rows
    S->>F: from_dataframe(year, sales)
    F->>F: Detect outliers
    F->>F: Compute summary stats
    F->>F: Run backtest
    F->>F: Run forecasts
    F-->>S: Summary, backtest, raw forecasts, scenarios
    S-->>P: Dashboard payload
    P-->>U: Charts, cards, tables
```

## Main components

### 1. Company list

Source:

- `coreiq_companies`

Used fields:

- `ticker`
- `name_coresight`
- `source`

Why:

- `ticker` is the unique identity
- `name_coresight` is the approved display label
- `source` decides whether the company should use SEC or YFinance actuals

### 2. Actual revenue retrieval

Source is chosen using `coreiq_companies.source`.

- `SEC` -> `coreiq_av_financials_income_statement`
- `YFinance` -> `coreiq_yf_financials_income_statement`

### 3. Forecast engine

The forecast engine is `RetailerForecaster`.

It expects a simple normalized dataset:

| Column | Meaning |
| --- | --- |
| `year` | fiscal year as integer |
| `sales` | annual revenue |

### 4. UI output

The page then renders:

- company summary cards
- actual revenue line
- individual model outputs
- backtest metrics
- ensemble projection
- scenario projections
- data lineage and calculation notes

## Why the system is structured this way

This design keeps the page:

- read-only against STG DB
- independent from Excel uploads
- reusable across SEC and non-SEC companies
- explainable because all model logic lives in one forecasting engine
- extensible because new models can be added in one place and surfaced in the UI
