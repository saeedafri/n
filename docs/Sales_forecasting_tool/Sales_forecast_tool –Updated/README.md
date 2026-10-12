# Retail Sales Forecasting App

Streamlit app running SARIMAX, SARIMA, and Prophet — upload your Excel, pick models, get full analysis.

## Setup (one time)

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Opens at http://localhost:8501

## First-time config

1. Run the app
2. In the sidebar — paste your FRED API key (get one free at https://fred.stlouisfed.org/docs/api/api_key.html)
3. Key is saved to `config.txt` — no need to re-enter next session

## Usage

1. Upload your Excel file (must have `Date` and `Sales` columns)
2. Select which models to run (SARIMAX / SARIMA / Prophet)
3. Adjust parameters in the sidebar if needed
4. Click **▶ Run Analysis**
5. Each model gets its own tab with full diagnostics
6. If 2+ models selected → Comparison tab appears automatically

## Parameters

| Parameter | Default | Description |
|---|---|---|
| Forecast horizon | 2 months | How many months ahead to forecast |
| Max Granger lag | 12 | Max lag tested in Granger causality |
| Top N exog vars | 10 | Top variables by F-stat passed to combo search |
| Max combo size | 1 | 1=single vars (fast), 2=pairs, 3=triples (slow) |
| Changepoint prior | 0.05 | Prophet trend flexibility (higher = more flexible) |
| Seasonality mode | auto | additive / multiplicative / auto-detect |

## Speed tips

- Set **Max combo size = 1** for fast runs (under 5 min total)
- Uncheck SARIMAX if you only need Prophet/SARIMA (no FRED pull needed)
- SARIMAX combo search is the slowest step — combo size 2 = ~15 min, size 3 = ~45 min

## File structure

```
app.py            ← main Streamlit app
requirements.txt  ← dependencies
config.txt        ← auto-created, stores FRED API key
```
