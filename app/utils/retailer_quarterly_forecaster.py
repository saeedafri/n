"""
RetailerQuarterlyForecaster — quarterly revenue forecasting engine.
Originally extracted from docs/CIQ_Quarterly_Estimates.ipynb.aspx.

The quarterly sibling of `RetailerForecaster`. It adds seasonal decomposition
(ratio-to-trend / ratio-to-centred-moving-average) so all forecasting works on a
de-seasonalised series and re-applies the seasonal pattern to the output. Seven
methods are fitted (the six annual models plus a seasonal-naive baseline); the
ensemble blends the top three by backtest MAPE.

Like the annual engine it consumes an in-memory dataframe so the same logic runs
on staging-database quarterly actuals. The caller supplies a frame with the
columns `year`, `quarter`, `sales`.
"""

import numpy as np
import pandas as pd
from scipy import stats
import warnings

warnings.filterwarnings("ignore")

_METHODS = ("linear", "cagr", "exp_smoothing", "holt",
            "ma_trend", "weighted_avg", "seasonal_naive")


def _quarter_index(year: int, quarter: int) -> int:
    """Convert (year, quarter) to a monotonic integer index for regression."""
    return year * 4 + quarter


def _index_to_year_quarter(index: int) -> tuple:
    """Inverse of _quarter_index → (year, quarter)."""
    quarter = index % 4
    if quarter == 0:
        return index // 4 - 1, 4
    return index // 4, quarter


class RetailerQuarterlyForecaster:
    """Quarterly revenue forecasting for a single retailer."""

    def _initialize_state(self) -> None:
        self.raw_data = None
        self.clean_data = None            # columns: year, quarter, qidx, sales, label
        self.data_excl_outliers = None
        self.outliers = []                # list of qidx values flagged
        self.seasonal_indices = np.ones(4)  # Q1..Q4 seasonal factors
        self.forecasts = {}
        self.backtest_results = None
        self.best_method = None
        self.final_forecast = None

    @classmethod
    def from_dataframe(cls, dataframe: pd.DataFrame) -> "RetailerQuarterlyForecaster":
        """Create a forecaster from a frame exposing `year`, `quarter`, `sales`."""
        obj = cls.__new__(cls)
        obj._initialize_state()
        obj.raw_data = dataframe.copy()
        obj._prepare_data()
        return obj

    # -------------------------------------------------------------------------
    # DATA PREPARATION
    # -------------------------------------------------------------------------

    def _prepare_data(self) -> None:
        df = self.raw_data[["year", "quarter", "sales"]].copy()
        df["year"] = pd.to_numeric(df["year"], errors="coerce")
        df["quarter"] = pd.to_numeric(df["quarter"], errors="coerce")
        df["sales"] = pd.to_numeric(df["sales"], errors="coerce")
        df = df.dropna()

        df["year"] = df["year"].astype(int)
        df["quarter"] = df["quarter"].astype(int)
        df["qidx"] = [_quarter_index(y, q) for y, q in zip(df["year"], df["quarter"])]
        df["label"] = [f"Q{q} {y}" for y, q in zip(df["year"], df["quarter"])]

        self.clean_data = (
            df.drop_duplicates(subset=["qidx"])
              .sort_values("qidx")
              .reset_index(drop=True)
        )

        self._compute_seasonal_indices()
        self._detect_outliers()

    def _compute_seasonal_indices(self) -> None:
        """
        Seasonal indices via ratio-to-trend. Works with as few as 4 quarters;
        for 8+ quarters uses the ratio-to-centred-moving-average. Normalised so
        the mean of the four factors equals 1.
        """
        df = self.clean_data

        if df["quarter"].nunique() < 2:
            self.seasonal_indices = np.ones(4)
            return

        if len(df) >= 8:
            sales = df["sales"].values
            moving_avg = np.convolve(sales, np.ones(4) / 4, mode="same")
            ratios = [[] for _ in range(4)]
            for i in range(2, len(sales) - 2):
                q = df["quarter"].iloc[i] - 1
                if moving_avg[i] > 0:
                    ratios[q].append(sales[i] / moving_avg[i])
            indices = np.array([np.mean(r) if r else 1.0 for r in ratios])
        else:
            qidx = df["qidx"].values.astype(float)
            sales = df["sales"].values
            slope, intercept, *_ = stats.linregress(qidx, sales)
            trend = intercept + slope * qidx
            detrended = sales / trend

            by_quarter = {}
            for position, quarter in enumerate(df["quarter"].values):
                by_quarter.setdefault(quarter - 1, []).append(detrended[position])
            indices = np.ones(4)
            for q, values in by_quarter.items():
                indices[q] = np.mean(values)

        self.seasonal_indices = indices / indices.mean()

    def _deseasonalise(self, sales: np.ndarray, quarters: np.ndarray) -> np.ndarray:
        return sales / self.seasonal_indices[quarters - 1]

    def _reseasonalise(self, values: np.ndarray, quarters: np.ndarray) -> np.ndarray:
        return values * self.seasonal_indices[quarters - 1]

    def _detect_outliers(self, threshold: float = 2.5) -> None:
        df = self.clean_data
        deseasonalised = self._deseasonalise(df["sales"].values, df["quarter"].values)
        growth = np.diff(deseasonalised) / deseasonalised[:-1]
        if growth.std() > 0:
            z = (growth - growth.mean()) / growth.std()
            # diff() drops one element — offset the indices back by one
            flagged = np.where(np.abs(z) > threshold)[0] + 1
            self.outliers = df["qidx"].iloc[flagged].tolist()

        self.data_excl_outliers = (
            df[~df["qidx"].isin(self.outliers)].copy().reset_index(drop=True)
        )

    # -------------------------------------------------------------------------
    # SUMMARY STATS
    # -------------------------------------------------------------------------

    def summary_stats(self, exclude_outliers: bool = True) -> dict:
        df = self.data_excl_outliers if exclude_outliers else self.clean_data
        sales = df["sales"].values
        n = len(sales)
        growth = np.diff(sales) / sales[:-1]
        years_span = (n - 1) / 4
        cagr = (np.power(sales[-1] / sales[0], 1 / years_span) - 1) if years_span > 0 else 0
        return {
            "n_quarters": n,
            "first_quarter": df["label"].iloc[0],
            "last_quarter": df["label"].iloc[-1],
            "first_sales": sales[0],
            "last_sales": sales[-1],
            "total_growth_pct": (sales[-1] / sales[0] - 1) * 100,
            "annual_cagr_pct": cagr * 100,
            "avg_qoq_growth_pct": np.mean(growth) * 100 if len(growth) else 0,
            "median_qoq_growth_pct": np.median(growth) * 100 if len(growth) else 0,
            "growth_volatility_pct": np.std(growth) * 100 if len(growth) else 0,
            "min_qoq_growth_pct": np.min(growth) * 100 if len(growth) else 0,
            "max_qoq_growth_pct": np.max(growth) * 100 if len(growth) else 0,
            "seasonal_indices": dict(zip(["Q1", "Q2", "Q3", "Q4"],
                                         np.round(self.seasonal_indices, 4))),
            "outliers_excluded": self.outliers if exclude_outliers else [],
        }

    # -------------------------------------------------------------------------
    # FORECASTING METHODS  (operate on de-seasonalised data, then re-apply)
    # -------------------------------------------------------------------------

    def _future_quarters(self, last_qidx: int, periods: int):
        """Return (qidx, quarter, year) arrays for the next `periods` quarters."""
        future_qidx = np.arange(last_qidx + 1, last_qidx + 1 + periods)
        future_quarters = np.array([qi % 4 if qi % 4 != 0 else 4 for qi in future_qidx])
        future_years = np.array([_index_to_year_quarter(qi)[0] for qi in future_qidx])
        return future_qidx, future_quarters, future_years

    def _forecast_linear(self, df: pd.DataFrame, periods: int) -> dict:
        qidx = df["qidx"].values.astype(float)
        ds = self._deseasonalise(df["sales"].values, df["quarter"].values)
        slope, intercept, r_value, _, std_err = stats.linregress(qidx, ds)
        fq, fquarters, fyears = self._future_quarters(int(qidx[-1]), periods)

        ds_forecast = intercept + slope * fq
        forecast = self._reseasonalise(ds_forecast, fquarters)

        n = len(qidx)
        se = std_err * np.sqrt(1 + 1 / n + (fq - qidx.mean()) ** 2 / np.sum((qidx - qidx.mean()) ** 2))
        ci_lower = self._reseasonalise(ds_forecast - 1.645 * se * np.sqrt(n), fquarters)
        ci_upper = self._reseasonalise(ds_forecast + 1.645 * se * np.sqrt(n), fquarters)
        return {"method": "linear", "forecast": forecast, "qidx": fq,
                "quarters": fquarters, "years": fyears, "r_squared": r_value ** 2,
                "ci_lower": ci_lower, "ci_upper": ci_upper}

    def _forecast_cagr(self, df: pd.DataFrame, periods: int, lookback: int = None) -> dict:
        qidx = df["qidx"].values
        ds = self._deseasonalise(df["sales"].values, df["quarter"].values)
        lb = lookback if lookback and lookback < len(ds) else len(ds)
        n_q = lb - 1
        qoq_growth = np.power(ds[-1] / ds[-lb], 1 / n_q) - 1 if n_q > 0 else 0
        fq, fquarters, fyears = self._future_quarters(int(qidx[-1]), periods)
        ds_forecast = np.array([ds[-1] * np.power(1 + qoq_growth, i) for i in range(1, periods + 1)])
        forecast = self._reseasonalise(ds_forecast, fquarters)
        return {"method": "cagr", "forecast": forecast, "qidx": fq,
                "quarters": fquarters, "years": fyears, "qoq_growth": qoq_growth}

    def _forecast_exp_smoothing(self, df: pd.DataFrame, periods: int, alpha: float = 0.3) -> dict:
        qidx = df["qidx"].values
        ds = self._deseasonalise(df["sales"].values, df["quarter"].values)
        smoothed = [ds[0]]
        for value in ds[1:]:
            smoothed.append(alpha * value + (1 - alpha) * smoothed[-1])
        trend = smoothed[-1] - smoothed[-2] if len(smoothed) > 1 else 0
        fq, fquarters, fyears = self._future_quarters(int(qidx[-1]), periods)
        ds_forecast = np.array([smoothed[-1] + trend * i for i in range(1, periods + 1)])
        forecast = self._reseasonalise(ds_forecast, fquarters)
        return {"method": "exp_smoothing", "forecast": forecast, "qidx": fq,
                "quarters": fquarters, "years": fyears}

    def _forecast_holt(self, df: pd.DataFrame, periods: int,
                       alpha: float = 0.3, beta: float = 0.1) -> dict:
        qidx = df["qidx"].values
        ds = self._deseasonalise(df["sales"].values, df["quarter"].values)
        level = ds[0]
        trend = ds[1] - ds[0] if len(ds) > 1 else 0
        for value in ds[1:]:
            prev_level = level
            level = alpha * value + (1 - alpha) * (level + trend)
            trend = beta * (level - prev_level) + (1 - beta) * trend
        fq, fquarters, fyears = self._future_quarters(int(qidx[-1]), periods)
        ds_forecast = np.array([level + trend * i for i in range(1, periods + 1)])
        forecast = self._reseasonalise(ds_forecast, fquarters)
        return {"method": "holt", "forecast": forecast, "qidx": fq,
                "quarters": fquarters, "years": fyears}

    def _forecast_ma_trend(self, df: pd.DataFrame, periods: int, window: int = 4) -> dict:
        qidx = df["qidx"].values
        ds = self._deseasonalise(df["sales"].values, df["quarter"].values)
        growth = np.diff(ds) / ds[:-1]
        avg_growth = np.mean(growth[-window:]) if len(growth) >= window else np.mean(growth)
        fq, fquarters, fyears = self._future_quarters(int(qidx[-1]), periods)
        ds_forecast, last = [], ds[-1]
        for _ in range(periods):
            last = last * (1 + avg_growth)
            ds_forecast.append(last)
        forecast = self._reseasonalise(np.array(ds_forecast), fquarters)
        return {"method": "ma_trend", "forecast": forecast, "qidx": fq,
                "quarters": fquarters, "years": fyears}

    def _forecast_weighted_avg(self, df: pd.DataFrame, periods: int) -> dict:
        qidx = df["qidx"].values
        ds = self._deseasonalise(df["sales"].values, df["quarter"].values)
        growth = np.diff(ds) / ds[:-1]
        weights = np.arange(1, len(growth) + 1)
        weighted_growth = np.average(growth, weights=weights) if len(growth) else 0.0
        fq, fquarters, fyears = self._future_quarters(int(qidx[-1]), periods)
        ds_forecast, last = [], ds[-1]
        for _ in range(periods):
            last = last * (1 + weighted_growth)
            ds_forecast.append(last)
        forecast = self._reseasonalise(np.array(ds_forecast), fquarters)
        return {"method": "weighted_avg", "forecast": forecast, "qidx": fq,
                "quarters": fquarters, "years": fyears}

    def _forecast_seasonal_naive(self, df: pd.DataFrame, periods: int) -> dict:
        """Repeat each quarter's most recent value, grown by the average YoY rate."""
        qidx = df["qidx"].values
        sales = df["sales"].values
        quarters = df["quarter"].values
        fq, fquarters, fyears = self._future_quarters(int(qidx[-1]), periods)

        yearly_growth = [sales[i] / sales[i - 4] - 1 for i in range(4, len(sales)) if sales[i - 4] > 0]
        avg_yoy = np.mean(yearly_growth) if yearly_growth else 0.0

        forecast = []
        for i, quarter in enumerate(fquarters):
            same_quarter = sales[quarters == quarter]
            if same_quarter.size:
                years_ahead = (i // 4) + 1
                forecast.append(same_quarter[-1] * np.power(1 + avg_yoy, years_ahead))
            else:
                forecast.append(sales[-1])
        return {"method": "seasonal_naive", "forecast": np.array(forecast), "qidx": fq,
                "quarters": fquarters, "years": fyears}

    def _all_methods(self):
        return {
            "linear": self._forecast_linear,
            "cagr": self._forecast_cagr,
            "exp_smoothing": self._forecast_exp_smoothing,
            "holt": self._forecast_holt,
            "ma_trend": self._forecast_ma_trend,
            "weighted_avg": self._forecast_weighted_avg,
            "seasonal_naive": self._forecast_seasonal_naive,
        }

    # -------------------------------------------------------------------------
    # BACKTEST
    # -------------------------------------------------------------------------

    def backtest(self, holdout_quarters: int = 4, exclude_outliers: bool = True) -> pd.DataFrame:
        df = self.data_excl_outliers if exclude_outliers else self.clean_data
        if len(df) <= holdout_quarters + 4:
            holdout_quarters = max(1, len(df) // 4)

        train = df.iloc[:-holdout_quarters]
        actual = df.iloc[-holdout_quarters:]["sales"].values

        results = []
        for name, method in self._all_methods().items():
            try:
                predicted = method(train, holdout_quarters)["forecast"]
                mape = np.mean(np.abs(predicted - actual) / actual) * 100
                results.append({
                    "method": name,
                    "mape": round(float(mape), 2),
                    "bias": round(float(np.mean(predicted - actual)), 1),
                    "rmse": round(float(np.sqrt(np.mean((predicted - actual) ** 2))), 1),
                })
            except Exception:
                pass

        self.backtest_results = pd.DataFrame(results)
        if len(self.backtest_results):
            self.best_method = self.backtest_results.loc[
                self.backtest_results["mape"].idxmin(), "method"
            ]
        return self.backtest_results

    # -------------------------------------------------------------------------
    # FORECAST
    # -------------------------------------------------------------------------

    def forecast(self, periods: int = 20, exclude_outliers: bool = True) -> pd.DataFrame:
        df = self.data_excl_outliers if exclude_outliers else self.clean_data

        self.forecasts = {name: method(df, periods)
                          for name, method in self._all_methods().items()}

        sample = self.forecasts["linear"]
        results = pd.DataFrame({
            "fiscal_year": sample["years"].astype(int),
            "fiscal_quarter": sample["quarters"].astype(int),
            "label": [f"Q{q} {y}" for y, q in zip(sample["years"], sample["quarters"])],
        })
        for name, payload in self.forecasts.items():
            results[name] = np.round(payload["forecast"], 4)

        # Ensemble: top-3 by backtest MAPE (or a sensible seasonal default)
        if self.backtest_results is not None and len(self.backtest_results):
            top3 = self.backtest_results.nsmallest(3, "mape")["method"].tolist()
        else:
            top3 = ["holt", "seasonal_naive", "weighted_avg"]
        results["ensemble"] = np.round(results[top3].mean(axis=1), 4)

        results["ci_lower"] = np.round(sample["ci_lower"], 4)
        results["ci_upper"] = np.round(sample["ci_upper"], 4)

        # Scenarios: percentile growth rates applied to the de-seasonalised series
        ds_hist = self._deseasonalise(df["sales"].values, df["quarter"].values)
        growth_hist = np.diff(ds_hist) / ds_hist[:-1]
        if len(growth_hist) == 0:
            growth_hist = np.array([0.0])
        for label, pct in [("pessimistic", 25), ("baseline", 50), ("optimistic", 75)]:
            g = np.percentile(growth_hist, pct)
            scenario, last = [], ds_hist[-1]
            for _ in range(periods):
                last *= 1 + g
                scenario.append(last)
            results[f"scenario_{label}"] = np.round(
                self._reseasonalise(np.array(scenario), sample["quarters"]), 4
            )

        self.final_forecast = results
        return results

    # -------------------------------------------------------------------------
    # FULL PIPELINE
    # -------------------------------------------------------------------------

    def run(self, forecast_periods: int = 20, holdout_quarters: int = 4) -> dict:
        stats_data = self.summary_stats()
        historical = self.clean_data.copy()
        historical["qoq_growth_pct"] = (historical["sales"].pct_change() * 100).round(1)
        historical["yoy_growth_pct"] = (historical["sales"].pct_change(4) * 100).round(1)
        historical["is_outlier"] = historical["qidx"].isin(self.outliers)

        backtest_df = self.backtest(holdout_quarters=holdout_quarters)
        forecast_df = self.forecast(periods=forecast_periods)
        return {
            "summary_stats": stats_data,
            "historical": historical,
            "backtest": backtest_df,
            "forecast": forecast_df,
            "best_method": self.best_method,
            "raw_forecasts": self.forecasts,
        }
