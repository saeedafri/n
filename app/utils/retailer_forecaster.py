"""
RetailerForecaster — revenue forecasting engine.
Originally extracted from docs/CIQ_Estimates.ipynb.aspx.

The engine now supports both Excel uploads and in-memory dataframes so the
same six-model forecasting logic can run on staging-database actual revenue
series as well as ad hoc spreadsheet inputs.
"""

import io
import pandas as pd
import numpy as np
from scipy import stats
import warnings

warnings.filterwarnings("ignore")


class RetailerForecaster:
    """Annual revenue forecasting for a single retailer."""

    def __init__(self, file_bytes: bytes, filename: str):
        self._initialize_state()
        self._load_from_bytes(file_bytes, filename)

    def _initialize_state(self) -> None:
        self.raw_data = None
        self.clean_data = None
        self.data_excluding_outliers = None
        self.outliers = []
        self.forecasts = {}
        self.backtest_results = None
        self.best_method = None
        self.final_forecast = None

    @classmethod
    def from_dataframe(cls, dataframe: pd.DataFrame) -> "RetailerForecaster":
        """Create a forecaster from an already-loaded dataframe.

        The dataframe must expose a year-like column and a revenue-like column.
        Common accepted shapes:
        - year / sales
        - fiscal_date_ending / total_revenue
        - period_end / revenue
        """
        obj = cls.__new__(cls)
        obj._initialize_state()
        obj.raw_data = dataframe.copy()
        obj._prepare_data()
        return obj

    # -------------------------------------------------------------------------
    # DATA LOADING
    # -------------------------------------------------------------------------

    def _load_from_bytes(self, file_bytes: bytes, filename: str) -> None:
        buf = io.BytesIO(file_bytes)
        if filename.endswith(".xlsx") or filename.endswith(".xls"):
            self.raw_data = pd.read_excel(buf)
        else:
            raise ValueError("Only Excel files (.xlsx, .xls) are supported.")
        self._prepare_data()

    def _prepare_data(self) -> None:
        df = self.raw_data.copy()

        date_col = None
        for col in df.columns:
            if any(t in col.lower() for t in ["date", "year", "period", "time"]):
                date_col = col
                break
        if date_col is None:
            date_col = df.columns[0]

        sales_col = None
        for col in df.columns:
            if any(t in col.lower() for t in ["sales", "revenue", "amount", "value"]):
                sales_col = col
                break
        if sales_col is None:
            sales_col = df.columns[1]

        # Try numeric first — integer years (2015, 2016…) must not go through
        # pd.to_datetime, which interprets them as nanoseconds since epoch.
        numeric_years = pd.to_numeric(df[date_col], errors="coerce")
        looks_like_year = numeric_years.dropna().between(1900, 2200)
        if looks_like_year.all() and not numeric_years.isna().all():
            df["year"] = numeric_years
        else:
            df["year"] = pd.to_datetime(df[date_col], errors="coerce").dt.year

        df["sales"] = pd.to_numeric(df[sales_col], errors="coerce")

        tmp = (
            df[["year", "sales"]]
            .dropna()
            .loc[lambda d: d.groupby("year")["sales"].transform("max") == d["sales"]]
            .drop_duplicates(subset=["year"], keep="last")
            .sort_values("year")
            .reset_index(drop=True)
        )
        # Drop rows that are <10% of peak revenue — these are partial/transition periods
        # (e.g., YF phantom Dec-31 rows for Jan FY-end companies, ELF March transition year).
        if not tmp.empty:
            threshold = tmp["sales"].max() * 0.10
            tmp = tmp[tmp["sales"] >= threshold].reset_index(drop=True)
        self.clean_data = tmp
        self.clean_data["year"] = self.clean_data["year"].astype(int)
        self._detect_outliers()

    def _detect_outliers(self, threshold: float = 2.0) -> None:
        df = self.clean_data.copy()
        df["growth"] = df["sales"].pct_change()
        mean_g = df["growth"].mean()
        std_g = df["growth"].std()
        if std_g > 0:
            df["z_score"] = (df["growth"] - mean_g) / std_g
            self.outliers = df.loc[df["z_score"].abs() > threshold, "year"].tolist()
        self.data_excluding_outliers = self.clean_data[
            ~self.clean_data["year"].isin(self.outliers)
        ].copy()

    # -------------------------------------------------------------------------
    # SUMMARY STATS
    # -------------------------------------------------------------------------

    def summary_stats(self, exclude_outliers: bool = True) -> dict:
        df = self.data_excluding_outliers if exclude_outliers else self.clean_data
        sales = df["sales"].values
        years = df["year"].values
        growth_rates = np.diff(sales) / sales[:-1]
        n = len(sales)
        cagr = (np.power(sales[-1] / sales[0], 1 / (n - 1)) - 1) if n > 1 else 0
        return {
            "n_years": n,
            "first_year": int(years[0]),
            "last_year": int(years[-1]),
            "first_sales": sales[0],
            "last_sales": sales[-1],
            "total_growth_pct": (sales[-1] / sales[0] - 1) * 100,
            "cagr_pct": cagr * 100,
            "avg_annual_growth_pct": np.mean(growth_rates) * 100 if len(growth_rates) else 0,
            "median_annual_growth_pct": np.median(growth_rates) * 100 if len(growth_rates) else 0,
            "growth_volatility_pct": np.std(growth_rates) * 100 if len(growth_rates) else 0,
            "min_growth_pct": np.min(growth_rates) * 100 if len(growth_rates) else 0,
            "max_growth_pct": np.max(growth_rates) * 100 if len(growth_rates) else 0,
            "outliers_excluded": self.outliers if exclude_outliers else [],
        }

    # -------------------------------------------------------------------------
    # FORECASTING METHODS
    # -------------------------------------------------------------------------

    def _forecast_linear(self, years, sales, periods):
        slope, intercept, r_value, _, std_err = stats.linregress(years, sales)
        fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
        forecast = intercept + slope * fy
        n = len(years)
        se = std_err * np.sqrt(1 + 1 / n + (fy - np.mean(years)) ** 2 / np.sum((years - np.mean(years)) ** 2))
        return {
            "method": "Linear Regression",
            "forecast": forecast,
            "years": fy,
            "r_squared": r_value ** 2,
            "ci_lower": forecast - 1.645 * se * np.sqrt(n),
            "ci_upper": forecast + 1.645 * se * np.sqrt(n),
        }

    def _forecast_cagr(self, years, sales, periods, lookback=None):
        if lookback and lookback < len(sales):
            base, n = sales[-lookback], lookback - 1
        else:
            base, n = sales[0], len(sales) - 1
        cagr = np.power(sales[-1] / base, 1 / n) - 1 if n > 0 else 0
        fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
        return {
            "method": "CAGR",
            "forecast": np.array([sales[-1] * np.power(1 + cagr, i) for i in range(1, periods + 1)]),
            "years": fy,
            "cagr": cagr,
        }

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

    def _forecast_ma_trend(self, years, sales, periods, window=3):
        growth_rates = np.diff(sales) / sales[:-1]
        avg_growth = np.mean(growth_rates[-window:]) if len(growth_rates) >= window else np.mean(growth_rates)
        fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
        forecast, last = [], sales[-1]
        for _ in range(periods):
            last = last * (1 + avg_growth)
            forecast.append(last)
        return {"method": "MA Trend", "forecast": np.array(forecast), "years": fy}

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

    # -------------------------------------------------------------------------
    # BACKTEST
    # -------------------------------------------------------------------------

    def backtest(self, holdout_years: int = 2, exclude_outliers: bool = True) -> pd.DataFrame:
        df = self.data_excluding_outliers if exclude_outliers else self.clean_data
        if len(df) <= holdout_years + 2:
            holdout_years = 1
        train, test = df.iloc[:-holdout_years], df.iloc[-holdout_years:]
        ty, ts, actual = train["year"].values, train["sales"].values, test["sales"].values

        _methods = [
            ("linear", self._forecast_linear),
            ("cagr", self._forecast_cagr),
            ("exp_smoothing", self._forecast_exp_smoothing),
            ("holt", self._forecast_holt),
            ("ma_trend", self._forecast_ma_trend),
            ("weighted_avg", self._forecast_weighted_avg),
        ]
        results = []
        for key, method in _methods:
            try:
                predicted = method(ty, ts, holdout_years)["forecast"]
                mape = np.mean(np.abs(predicted - actual) / actual) * 100
                results.append({
                    "method": key,
                    "display": {
                        "linear": "Linear Regression", "cagr": "CAGR",
                        "exp_smoothing": "Exp Smoothing", "holt": "Holt's Linear",
                        "ma_trend": "MA Trend", "weighted_avg": "Weighted Avg",
                    }[key],
                    "mape": round(mape, 2),
                    "bias": round(float(np.mean(predicted - actual)), 1),
                    "rmse": round(float(np.sqrt(np.mean((predicted - actual) ** 2))), 1),
                })
            except Exception:
                pass

        self.backtest_results = pd.DataFrame(results)
        if len(self.backtest_results):
            self.best_method = self.backtest_results.loc[self.backtest_results["mape"].idxmin(), "method"]
        return self.backtest_results

    # -------------------------------------------------------------------------
    # FORECAST
    # -------------------------------------------------------------------------

    def forecast(self, periods: int = 5, exclude_outliers: bool = True) -> pd.DataFrame:
        df = self.data_excluding_outliers if exclude_outliers else self.clean_data
        years, sales = df["year"].values, df["sales"].values

        raw = {
            "linear": self._forecast_linear(years, sales, periods),
            "cagr": self._forecast_cagr(years, sales, periods),
            "exp_smoothing": self._forecast_exp_smoothing(years, sales, periods),
            "holt": self._forecast_holt(years, sales, periods),
            "ma_trend": self._forecast_ma_trend(years, sales, periods),
            "weighted_avg": self._forecast_weighted_avg(years, sales, periods),
        }
        self.forecasts = raw

        fy = raw["linear"]["years"]
        results = pd.DataFrame({"year": fy})
        for key, r in raw.items():
            results[key] = r["forecast"]

        # Ensemble: top-3 by backtest MAPE (or defaults)
        if self.backtest_results is not None and len(self.backtest_results):
            top3 = self.backtest_results.nsmallest(3, "mape")["method"].tolist()
        else:
            top3 = ["holt", "cagr", "weighted_avg"]
        results["ensemble"] = results[top3].mean(axis=1)

        # Scenarios
        growth_rates = np.diff(sales) / sales[:-1] if len(sales) > 1 else np.array([0.0])
        if len(growth_rates) == 0:
            growth_rates = np.array([0.0])
        for label, pct in [("pessimistic", 25), ("baseline", 50), ("optimistic", 75)]:
            g = np.percentile(growth_rates, pct)
            sc, last = [], sales[-1]
            for _ in range(periods):
                last *= 1 + g
                sc.append(last)
            results[f"scenario_{label}"] = sc

        self.final_forecast = results
        return results

    # -------------------------------------------------------------------------
    # FULL PIPELINE
    # -------------------------------------------------------------------------

    def run(self, forecast_periods: int = 5, holdout_years: int = 2) -> dict:
        stats_data = self.summary_stats()
        historical = self.clean_data.copy()
        historical["growth_pct"] = (historical["sales"].pct_change() * 100).round(1)
        historical["is_outlier"] = historical["year"].isin(self.outliers)

        backtest_df = self.backtest(holdout_years=holdout_years)
        forecast_df = self.forecast(periods=forecast_periods)

        return {
            "summary_stats": stats_data,
            "historical": historical,
            "backtest": backtest_df,
            "forecast": forecast_df,
            "best_method": self.best_method,
            "raw_forecasts": self.forecasts,
        }

    def run_full_analysis(self, forecast_periods: int = 5, holdout_years: int = 2) -> dict:
        """Compatibility alias for the original notebook API."""
        return self.run(forecast_periods=forecast_periods, holdout_years=holdout_years)
