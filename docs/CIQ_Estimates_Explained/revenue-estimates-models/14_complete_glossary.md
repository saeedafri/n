# 14 Complete Glossary — Every Term on the Revenue Estimates Page

Every word, abbreviation, and concept that appears anywhere on the Revenue Estimates page is defined here.
Definitions are ordered alphabetically. Terms that reference other terms are cross-linked inline.

---

## A

**actuals**
Real historical revenue numbers pulled from the database. They represent what actually happened. Contrast with *forecast* (a future projection) and *scenario* (a planning path). On this page actuals come from `coreiq_av_financials_income_statement` (for SEC companies) or `coreiq_yf_financials_income_statement` (for YFinance companies).

**alpha (α)**
A parameter used in Exponential Smoothing and Holt's Linear Trend. It controls how fast the smoothed level reacts to the newest actual value. Value fixed at 0.3 in this codebase. Formula context: `Lₜ = α × actualₜ + (1−α) × Lₜ₋₁`. Range: 0 to 1. Higher = reacts faster to new data; lower = smoother, slower-reacting.

**annual revenue**
The total revenue a company earned over one full fiscal year. On this page all data is at annual (yearly) frequency — no quarterly or monthly data is used in the model fit.

**avg_annual_growth_pct**
The arithmetic mean of all year-over-year growth rates in the cleaned training series. Computed as: `mean(growthₜ)` where `growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁`. Shown in the summary stats section. Different from CAGR (which is a geometric mean connecting endpoints only).

---

## B

**backtest**
A simulation that measures how well each model would have predicted the most recent real years, had those years been hidden during model fitting. Default: hide the last 2 years. The model is trained on earlier history only, then asked to predict the hidden years. The predictions are compared to the real values to compute MAPE, Bias, and RMSE.

**base_sales**
The starting revenue value used in the CAGR formula. On this page it is the earliest revenue value in the cleaned (outlier-removed) training series.

**best_method**
The model with the lowest backtest MAPE. Displayed in the top metric card. This model is used as the primary single-model reference and its MAPE is shown alongside the label.

**beta (β)**
A parameter used in Holt's Linear Trend only. Controls how fast the trend component reacts to a change in the level. Value fixed at 0.1 in this codebase. Formula context: `trendₜ = β × (levelₜ − levelₜ₋₁) + (1−β) × trendₜ₋₁`. Lower beta → smoother, slower-reacting trend.

**bias**
A backtest metric that measures whether a model systematically over- or under-predicted. Formula: `Bias = mean(predicted − actual)`. Positive bias → model over-predicted. Negative bias → model under-predicted. Bias near zero → predictions were centered around reality even if individual misses were large.

**billions**
Revenue on this page is expressed in US dollars, divided by 1,000,000,000. So "$5.3B" means $5,300,000,000 in annual revenue. All forecast tables and charts use this unit.

---

## C

**CAGR**
Compound Annual Growth Rate. One steady annual percentage that, if applied every year through compounding, would carry revenue from the first historical value to the last. Formula: `(last_sales / base_sales)^(1/n) − 1`. Sensitive to the choice of start and end point.

**cagr_pct**
The CAGR value expressed as a percentage (e.g., 8.2 means 8.2% per year). Shown in the Documentation tab summary section. It is a property of the data, not of any specific model — it describes the historical growth path.

**clean training series**
The annual revenue series after outlier years have been removed. This is the input to all six forecasting models. Visible in the Data Prep tab under "Clean training series".

**closed-form solution**
A mathematical formula that produces the exact answer in one computation without iteration. Linear Regression's slope and intercept are closed-form: one formula, one pass through the data, exact answer. Contrast with iterative methods like gradient descent used in deep learning.

**compounding**
Growth that builds on itself. If revenue is 100 and grows 10% per year through compounding: year 1 = 110, year 2 = 121 (not 120), year 3 = 133.1 (not 130). The base grows each year. CAGR, MA Trend, Weighted Avg, and scenarios all project through compounding.

**confidence interval** → see *prediction interval*

**coreiq_av_financials_income_statement**
The staging database table that holds annual income statement data for SEC-sourced companies. The page reads `fiscal_date_ending` (as the year) and `total_revenue` (as the sales value) from this table.

**coreiq_companies**
The staging database table that identifies which companies are in coverage, their display names, and their data source (SEC or YFinance).

**coreiq_yf_financials_income_statement**
The staging database table that holds financial data for YFinance-sourced companies, stored in a pivoted row format. The page pivots `line_item = 'Total Revenue'` to extract the revenue series.

---

## D

**data_excluding_outliers**
The internal name for the clean training series inside `RetailerForecaster`. This is the filtered version of `clean_data` where outlier years have been removed.

**display**
In the backtest results table, the human-readable model name (e.g., "MA Trend", "Holt's Linear"). Distinct from the internal key (e.g., "ma_trend", "holt") used in the code.

---

## E

**ensemble**
The average of the top 3 models selected by backtest MAPE. Formula for each future year: `ensemble_h = (model_a_h + model_b_h + model_c_h) / 3`. Purpose: reduce single-model risk by blending the strongest performers from backtesting. The ensemble is not itself a separately trained model — it is a post-processing combination.

**ensemble_h**
The ensemble forecast value for future year h (h=1 means next year after actuals end, h=2 means two years ahead, etc.).

**error**
The difference between what a model predicted and what actually happened. `error = predicted − actual`. Errors can be positive (over-prediction) or negative (under-prediction). MAPE, Bias, and RMSE all measure error in different ways.

**exponential smoothing**
A model that creates a smoothed version of the revenue series by applying a weighted average where older values get exponentially less weight. The parameter α controls how quickly older values fade. After smoothing all history, the trend for projection is extracted as the difference between the last two smoothed levels.

---

## F

**fiscal year**
The 12-month accounting period used by a company. Not necessarily the calendar year. For example, a company with a January fiscal year end reports its annual results in January. The page extracts the year from `fiscal_date_ending` (SEC) or `period_end` (YFinance) and treats it as the fiscal year integer.

**forecast**
A future revenue estimate produced by one of the six models. Forecasts are projections based on mathematical rules applied to historical actuals. They are not guaranteed outcomes.

**forecast_h**
The forecast value h years into the future. h=1 = one year beyond the last actual. h=5 = five years beyond the last actual.

**forecast_periods**
The number of future years shown in the forecast table. Default: 5. Shown as a metric card on the page.

**frequency**
In the YFinance source table, the column that identifies whether a row represents annual or quarterly data. The page filters to `frequency = 'annual'` before extracting revenue.

---

## G

**geometric mean**
A way of averaging a sequence of multiplicative quantities. CAGR is a geometric mean: it finds the single growth rate that, when compounded n times, produces the observed total growth. Different from the arithmetic mean (simple average) used for avg_annual_growth_pct.

**growth rate** → also see *year-over-year growth*
The percentage change in revenue from one year to the next. Formula: `growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁`. Used to build all growth-based models (CAGR, MA Trend, Weighted Avg, scenarios) and to detect outliers.

**growth_pct**
The growth rate expressed as a percentage. Shown in both the Raw actual history and Clean training series tables in the Data Prep tab.

**growth_volatility_pct**
The standard deviation of all historical year-over-year growth rates. A measure of how consistent the company's growth has been. High volatility → growth swings wildly year to year. Low volatility → growth is steady. Shown in the Documentation tab summary section.

---

## H

**historical_rows**
The count of years in the clean training series (after outlier removal). Shown as a metric card. This is the number of data points the models were trained on.

**holdout**
The years hidden from model training during backtesting. The model is trained on history minus holdout, then predicts the holdout years. Default: 2 years. Falls back to 1 year if total history ≤ 4 years.

**holdout_years**
The number of years held out during backtesting. Typically 2.

**Holt's Linear Trend**
A model that tracks two components simultaneously: level (current revenue baseline) and trend (current direction and speed of change). Both are updated every year using separate smoothing parameters (α and β). Often the strongest general-purpose model when the series has changing momentum.

---

## I

**intercept**
In Linear Regression, the value of the fitted line when year = 0. In practice, for years like 2021–2030, this is a very large negative number (because the fitted line would intersect zero far in the past). The intercept by itself has no intuitive meaning — only slope + intercept together produce the forecast.

---

## L

**last_sales**
The latest revenue value in the cleaned training series. The starting point for compounding in CAGR, MA Trend, Weighted Avg, and scenarios.

**level**
In Holt's Linear Trend, the current estimated revenue position. Updated every year by equation: `levelₜ = α × actualₜ + (1−α) × (levelₜ₋₁ + trendₜ₋₁)`. The level slowly adapts to where revenue actually is.

**line_item**
In the YFinance source table, the column that names each financial metric. The page filters to `line_item = 'Total Revenue'` to extract the revenue series. Other line items (Gross Profit, Net Income, etc.) are also available but not used in model fitting.

**Linear Regression**
A model that fits a straight line through the historical (year, revenue) data points using Ordinary Least Squares. The fitted line is then extended into future years. It assumes revenue grows by the same dollar amount each year. Returns an R² fit quality score and a 90% prediction interval.

---

## M

**MA Trend** → *Moving Average Trend*

**MAPE**
Mean Absolute Percentage Error. The primary metric used to rank models in backtesting. Formula: `MAPE = mean(|predicted − actual| / actual) × 100`. Units: percent. Lower is better. Thresholds: < 5% excellent, 5–15% acceptable, > 25% poor.

**mean**
The simple arithmetic average: sum of values divided by the count of values. Used in MAPE (mean of percentage errors), Bias (mean of signed errors), avg_annual_growth_pct (mean of growth rates).

**mean_growth**
The arithmetic mean of all historical year-over-year growth rates. Used in outlier detection: z-scores measure how far each growth rate is from this mean.

**model**
One specific mathematical method for projecting future revenue. This page runs six: Linear Regression, CAGR, Exponential Smoothing, Holt's Linear Trend, Moving Average Trend, Weighted Average Growth.

**Moving Average Trend**
A model that computes year-over-year growth rates, averages the most recent 3 (default window size), and projects forward by compounding that average growth rate from the latest actual revenue value. The most transparent model — the forecast can be verified by hand in 30 seconds.

---

## N

**n**
The count of year-to-year intervals. If there are 7 years of history (2017–2023), n = 6 (six intervals). Used in the CAGR formula as the exponent denominator.

**next_forecast_value_billions**
The ensemble forecast value for the first year after actuals end. Shown in the "Ensemble next year" metric card.

---

## O

**OLS** → *Ordinary Least Squares*

**optimistic scenario**
A planning path using the 75th percentile of historical growth rates. Represents what revenue would look like if future growth resembled the company's stronger historical years. Not a model output — a planning boundary.

**Ordinary Least Squares (OLS)**
The algorithm used by Linear Regression to find the best-fit line. It minimizes the sum of squared differences between the fitted line and each historical data point. Produces the globally optimal slope and intercept in one closed-form computation.

**outlier**
A year whose year-over-year growth rate was unusually far from the historical average, as measured by z-score. Any year with |z-score| > 2.0 is flagged. Outlier years are kept visible in the UI but removed from model training so they do not distort the forecast.

**outlier_years**
The list of fiscal years identified as outliers for the selected company. Shown as a chip in the hero section. These years appear as yellow X markers in the charts.

---

## P

**percentile**
A position in a sorted list. The 25th percentile is the value below which 25% of the observations fall. On this page, growth rate percentiles define the pessimistic (25th), baseline (50th), and optimistic (75th) scenarios.

**period_date**
The date associated with each annual revenue row in the actual rows. Converted to a fiscal year integer for model input.

**pessimistic scenario**
A planning path using the 25th percentile of historical growth rates. Represents what revenue would look like if future growth resembled the company's weaker historical years. Useful for downside planning and stress testing.

**prediction interval**
The shaded band shown on the Linear Regression model chart. It represents the 90% range — if the model's assumptions are correct, the true future value should fall within this band 90% of the time. The band widens as the forecast horizon extends further into the future.

---

## R

**R²** (R-squared)
A measure of how well the Linear Regression line fits the historical data. Formula: `R² = 1 − (sum of squared residuals / sum of squared total variation)`. Range: 0 to 1. R² = 1 means the line passes through every point. R² = 0 means the line is no better than just using the historical mean. Shown in the "Model stat" card on the Linear Regression tab.

**raw_forecasts**
The internal dictionary returned by `RetailerForecaster.forecast()`. It contains one entry per model with the forecast values, years, and any model-specific outputs (like R² for linear, CAGR for cagr, prediction intervals for linear).

**reported_currency**
The currency in which the company reports its financials. For SEC companies, read from the `reported_currency` column. For YFinance companies, resolved from the company overview payload. Typically "USD" for US companies.

**RMSE**
Root Mean Squared Error. A backtest metric that measures average prediction error with extra penalty for large misses. Formula: `RMSE = sqrt(mean((predicted − actual)²))`. Units: same as revenue (billions). A miss of 2B is penalized 4× more than a miss of 1B because the squaring amplifies large errors.

---

## S

**sales**
The internal column name used inside `RetailerForecaster` for revenue values. Annual revenue is stored in billions in the `sales` column of the normalized dataframe.

**scenario**
A planning path derived from historical growth percentiles. Three scenarios are generated: pessimistic (25th percentile growth), baseline (50th percentile growth), and optimistic (75th percentile growth). Scenarios are not model forecasts — they are sensitivity analysis tools.

**SEC**
U.S. Securities and Exchange Commission. Companies that report to the SEC file annual financial statements (10-K). The staging database stores these reports in `coreiq_av_financials_income_statement`. The source column in `coreiq_companies` is "SEC" for these companies.

**slope**
In Linear Regression, the rate of change of the fitted line per unit increase in year. A slope of 0.5 means the model expects revenue to grow by $0.5B per calendar year. The slope captures the dominant direction of the long-run trend.

**smoothed level** → see *level*

**source**
The origin of a company's financial data. Possible values: "SEC" or "YFinance". Determined from the `source` column in `coreiq_companies`. Controls which actuals table is read.

**source_label**
The display version of the source: "SEC" or "YFinance". Shown as a chip in the hero section and as the "Source" column in the Data Prep table.

**standard deviation**
A measure of how spread out a set of values is around their mean. For growth rates: if all growth rates cluster near 10%, std dev is small (stable growth). If growth rates range from −20% to +40%, std dev is large (volatile growth). Used in z-score outlier detection: `z = (value − mean) / std_dev`.

---

## T

**ticker**
The stock market abbreviation for a company. "ANF" = Abercrombie & Fitch, "ADS" = Adidas. Used as the unique company identifier throughout the page. Selectable from the company dropdown.

**timings_ms**
Performance timing dictionary returned by `RevenueForecastService.get_company_dashboard()`. Keys: `load_actuals`, `summary_stats`, `backtest`, `forecast`, `total`. Shown in the "Service timings" metric card.

**total_growth_pct**
Total percentage revenue growth from first year to last year in the cleaned training series. Not the same as CAGR (which annualizes it). Formula: `((last_sales / first_sales) − 1) × 100`.

**training data** → see *clean training series*

**trend** (in Holt's method)
The second component tracked by Holt's Linear Trend model. Represents the current estimated direction and speed of revenue change. Updated each year: `trendₜ = β × (levelₜ − levelₜ₋₁) + (1−β) × trendₜ₋₁`. A positive trend means the model sees upward momentum; a negative trend means it sees a declining trajectory.

---

## W

**Weighted Average Growth**
A model that computes all historical year-over-year growth rates and takes a weighted average where the newest growth rate gets the highest weight (= n) and the oldest gets the lowest weight (= 1). Projects forward by compounding this weighted average rate from the latest actual.

**window**
In Moving Average Trend, the number of recent growth rates included in the average. Default: 3. If fewer than 3 growth rates exist, the average uses all available rates.

---

## Y

**year-over-year growth**
The percentage change in revenue compared to the prior year. Formula: `growthₜ = (salesₜ − salesₜ₋₁) / salesₜ₋₁`. Expressed as a decimal (0.10 = 10%) in model computations, and as a percentage (10%) in the UI.

**YFinance**
Yahoo Finance. A data source for non-SEC or international companies. Data stored in `coreiq_yf_financials_income_statement` in a row-per-metric format. The page pivots `line_item = 'Total Revenue'` rows into one annual revenue value per year.

---

## Z

**z-score**
A normalized measure of how far a value is from the mean, expressed in units of standard deviation. Formula: `z = (value − mean) / std_dev`. For outlier detection on this page: any growth year with `|z| > 2.0` is flagged as an outlier. A z-score of 2.0 corresponds roughly to the top/bottom 2.3% of a normal distribution — a rare event.
