# Revenue Estimates Forecasting

This doc set is written for a person who is new to AI/ML and new to forecasting.

The goal is not only to show formulas.

The goal is to help someone answer these questions clearly:

1. What data are we reading
2. What does each model assume
3. Why is one model line higher or lower than another in the UI
4. How do we know which model did better
5. Which exact numbers on the page came from which calculation

## Best reading order for a new person

### Foundations (start here)
1. [00 Start Here For New People](./00_start_here_for_new_people.md)
2. [01 System Flow](./01_system_flow.md)
3. [02 Data Sources And Preparation](./02_data_sources_and_preparation.md)
4. [03 Outlier Detection](./03_outlier_detection.md)

### Model overviews (one page each, concise)
5. [04 Linear Regression — overview](./04_linear_regression.md)
6. [05 CAGR — overview](./05_cagr.md)
7. [06 Exponential Smoothing — overview](./06_exponential_smoothing.md)
8. [07 Holt Linear Trend — overview](./07_holt_linear_trend.md)
9. [08 Moving Average Trend — overview](./08_moving_average_trend.md)
10. [09 Weighted Average Growth — overview](./09_weighted_average_growth.md)
11. [10 Backtesting Ensemble And Scenarios — overview](./10_backtesting_ensemble_and_scenarios.md)

### UI and real examples
12. [11 UI Number Mapping](./11_ui_number_mapping.md)
13. [12 Real Examples You Can Match In UI](./12_real_examples_you_can_match_in_ui.md)

### Complete deep-dive files (full derivations, real-world context, hand-workable examples)
14. [Linear Regression — Full Deep Dive](./MODEL_01_linear_regression_deep_dive.md)
15. [CAGR — Full Deep Dive](./MODEL_02_cagr_deep_dive.md)
16. [Exponential Smoothing — Full Deep Dive](./MODEL_03_exponential_smoothing_deep_dive.md)
17. [Holt's Linear Trend — Full Deep Dive](./MODEL_04_holt_linear_trend_deep_dive.md)
18. [Moving Average Trend — Full Deep Dive](./MODEL_05_ma_trend_deep_dive.md)
19. [Weighted Average Growth — Full Deep Dive](./MODEL_06_weighted_average_growth_deep_dive.md)
20. [Ensemble and Scenarios — Full Deep Dive](./MODEL_07_ensemble_and_scenarios_deep_dive.md)

### Reference
21. [ML From Scratch — Complete System Guide](./13_ml_from_scratch_complete_guide.md)
22. [Complete Glossary — Every Term Defined](./14_complete_glossary.md)

### Technical Reference (for Data Scientists, ML Engineers, Data Analysts)
23. [**TECHNICAL_REFERENCE_COMPLETE**](./TECHNICAL_REFERENCE_COMPLETE.md) — The authoritative single-file reference. Covers: system architecture, exact DB queries, every model formula (matched line-by-line to the source code), OLS derivation, Gauss-Markov theorem, EWMA weight proof, Holt's ETS classification, backtesting methodology, ensemble variance reduction theory, percentile interpolation, MAPE/Bias/RMSE decomposition, all numpy/scipy/pandas functions, complete glossary, statistical theorems, and 13 FAQ for technical reviewers. **Start here if you are a data scientist or ML engineer.**

## How these docs are written

Each model file now follows the same pattern:

- plain-English meaning first
- new keyword explained before it is used
- tiny toy example
- exact formula used in code
- real example from the current page
- what to say while explaining it to others

## One important mindset

In this page, a "model" is not magic.

A model is only:

- a rule for looking at past revenue
- and extending that history into future revenue

Different models use different rules.

That is why the page shows different forecast lines for the same company.

## Current production defaults

| Item | Current value in code |
| --- | --- |
| Forecast horizon | 5 years |
| Backtest holdout | 2 years |
| Outlier threshold | absolute z-score greater than 2.0 |
| Exponential smoothing alpha | 0.3 |
| Holt alpha | 0.3 |
| Holt beta | 0.1 |
| Moving average window | 3 growth values |
| Scenario percentiles | 25th, 50th, 75th |

## Current real examples used in these docs

To keep the explanations tied to the live page, the docs use:

- `ANF` for a SEC company example
- `ADS` for a YFinance company example

Those examples were taken from the current STG-backed page outputs.
