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

from utils.retailer_forecaster import drop_partial_periods

warnings.filterwarnings("ignore")

_METHODS = ("linear", "cagr", "exp_smoothing", "holt",
            "ma_trend", "weighted_avg", "seasonal_naive")

# Modelling constants. The outlier thresholds are deliberately looser than the
# annual engine's: a quarterly series has three to four times as many points, so
# the same threshold would flag far more of them by chance alone.
STRUCTURAL_BREAK_THRESHOLD = 0.40
STRUCTURAL_BREAK_PERSIST_FACTOR = 0.6
OUTLIER_Z_THRESHOLD = 2.2
OUTLIER_IQR_MULT = 2.0
MIN_TRAIN_FULL = 8                     # backtest min_train (quarters) for FULL companies
IMPLAUSIBLE_GROWTH_CAGR = 0.60         # annualised
IMPLAUSIBLE_DECLINE_CAGR = -0.25       # annualised
HIGH_OUTLIER_RATE = 0.15
_DESEASONALIZED_METHODS = ("linear", "cagr", "exp_smoothing", "holt",
                           "ma_trend", "weighted_avg")
_GROWTH_ONLY_METHODS = ("cagr", "ma_trend", "weighted_avg")


def compute_seasonal_indices(qidx_arr, quarter_arr, sales_arr) -> np.ndarray:
    """Ratio-to-moving-average (8+ points) or ratio-to-trend (4-7 points),
    normalised so the four factors average to 1."""
    qidx_arr = np.asarray(qidx_arr, dtype=float)
    quarter_arr = np.asarray(quarter_arr, dtype=int)
    sales_arr = np.asarray(sales_arr, dtype=float)
    if len(np.unique(quarter_arr)) < 2:
        return np.ones(4)

    if len(sales_arr) >= 8:
        moving_avg = np.convolve(sales_arr, np.ones(4) / 4, mode="same")
        ratios = [[] for _ in range(4)]
        for i in range(2, len(sales_arr) - 2):
            if moving_avg[i] > 0:
                ratios[quarter_arr[i] - 1].append(sales_arr[i] / moving_avg[i])
        indices = np.array([np.mean(r) if r else 1.0 for r in ratios])
    else:
        slope, intercept, *_ = stats.linregress(qidx_arr, sales_arr)
        trend = intercept + slope * qidx_arr
        # A fitted trend can go non-positive on a short, steep series; dividing
        # by it would invert the seasonal factor's sign.
        trend = np.where(trend <= 0, sales_arr.mean(), trend)
        detrended = sales_arr / trend
        by_quarter = {}
        for i, quarter in enumerate(quarter_arr):
            by_quarter.setdefault(quarter - 1, []).append(detrended[i])
        indices = np.ones(4)
        for quarter, values in by_quarter.items():
            indices[quarter] = np.mean(values)

    return indices / indices.mean()


def _quarter_index(year: int, quarter: int) -> int:
    """Convert (year, quarter) to a monotonic integer index for regression."""
    return year * 4 + quarter


def _qidx_to_label(index: int) -> str:
    year, quarter = _index_to_year_quarter(index)
    return f"Q{quarter} {year}"


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
        self.backtest_detail = {}
        self.best_method = None
        self.final_forecast = None
        self.tier = "NO_DATA"
        self.confidence = "none"
        self.structural_break = None
        self.flag_reasons = []
        self.plausible = True
        self.ensemble_methods = []
        self.mape = None

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

        self.clean_data = drop_partial_periods(
            df.drop_duplicates(subset=["qidx"]).sort_values("qidx").reset_index(drop=True)
        )
        self._build_fitting_frame()

    def _deseasonalise(self, sales: np.ndarray, quarters: np.ndarray) -> np.ndarray:
        return sales / self.seasonal_indices[np.asarray(quarters, dtype=int) - 1]

    def _reseasonalise(self, values: np.ndarray, quarters: np.ndarray) -> np.ndarray:
        return values * self.seasonal_indices[np.asarray(quarters, dtype=int) - 1]

    # -------------------------------------------------------------------------
    # DATA QUALITY: outliers → structural break → seasonality → tier
    # -------------------------------------------------------------------------

    @staticmethod
    def _detect_structural_break(qidx_arr, deseasonalised):
        """Quarter index of the last large, persistent drop in the
        de-seasonalised level. Run on de-seasonalised data so a merely weak Q1
        is never mistaken for a business that shrank."""
        if len(deseasonalised) < 6:
            return None
        breaks = [qidx_arr[i] for i in range(1, len(deseasonalised))
                  if deseasonalised[i - 1] > 0
                  and deseasonalised[i] / deseasonalised[i - 1] - 1 < -STRUCTURAL_BREAK_THRESHOLD]
        if not breaks:
            return None
        break_qidx = breaks[-1]
        idx = list(qidx_arr).index(break_qidx)
        if idx >= len(deseasonalised) - 2:
            return None  # too few points after the break to confirm it persisted
        pre_level, post_level = deseasonalised[idx - 1], np.mean(deseasonalised[idx:])
        if (pre_level - post_level) / pre_level > STRUCTURAL_BREAK_THRESHOLD * STRUCTURAL_BREAK_PERSIST_FACTOR:
            return break_qidx
        return None

    @staticmethod
    def _detect_outliers(deseasonalised, qidx_arr) -> list:
        """Combined z-score and IQR test on de-seasonalised quarter-on-quarter
        growth."""
        if len(deseasonalised) < 5:
            return []
        rates = np.diff(deseasonalised) / deseasonalised[:-1]
        if len(rates) < 3:
            return []
        flagged = set()
        std_g = rates.std()
        if std_g > 0:
            z = (rates - rates.mean()) / std_g
            flagged.update(qidx_arr[i + 1] for i, zi in enumerate(z) if abs(zi) > OUTLIER_Z_THRESHOLD)
        q1, q3 = np.percentile(rates, 25), np.percentile(rates, 75)
        iqr = q3 - q1
        if iqr > 0:
            lower, upper = q1 - OUTLIER_IQR_MULT * iqr, q3 + OUTLIER_IQR_MULT * iqr
            flagged.update(qidx_arr[i + 1] for i, r in enumerate(rates) if r < lower or r > upper)
        return sorted(int(q) for q in flagged)

    def _build_fitting_frame(self) -> None:
        """Set seasonality, tier and the frame every fit runs on.

        Order matters. Outliers are screened *before* the structural-break test:
        one anomalous quarter (a 53-week year, a one-off consolidation, a data
        artifact) shows up as a huge decline in the quarter after it, which the
        break detector would otherwise read as a permanent change and use to
        throw away years of good history.
        """
        self.flag_reasons = []
        self.structural_break = None
        self.outliers = []
        df = self.clean_data
        n = 0 if df is None else len(df)

        if n == 0:
            self.tier, self.confidence = "NO_DATA", "none"
            self.seasonal_indices = np.ones(4)
            self.data_excl_outliers = df.copy() if df is not None else None
            return

        qidx_arr = df["qidx"].values
        sales_arr = df["sales"].values
        quarters_arr = df["quarter"].values

        if n <= 3:
            self.tier, self.confidence = "FLAT", "very_low"
            self.seasonal_indices = np.ones(4)
            self.data_excl_outliers = df.copy().reset_index(drop=True)
            return

        # Pass 1 — provisional outliers, used only to protect the break test.
        self.seasonal_indices = compute_seasonal_indices(qidx_arr, quarters_arr, sales_arr)
        provisional = self._detect_outliers(
            self._deseasonalise(sales_arr, quarters_arr), qidx_arr
        )
        screened = ~np.isin(qidx_arr, provisional)
        if screened.sum() >= 6:
            break_qidx, break_sales, break_quarters = (
                qidx_arr[screened], sales_arr[screened], quarters_arr[screened],
            )
            break_indices = compute_seasonal_indices(break_qidx, break_quarters, break_sales)
            break_series = break_sales / break_indices[break_quarters - 1]
        else:
            break_qidx = qidx_arr
            break_series = self._deseasonalise(sales_arr, quarters_arr)

        structural_break = self._detect_structural_break(break_qidx, break_series)
        if structural_break is not None and (qidx_arr >= structural_break).sum() >= 4:
            self.structural_break = int(structural_break)
            self.flag_reasons.append(
                f"structural_break_{_qidx_to_label(int(structural_break))}_pre_break_data_excluded"
            )
            keep = qidx_arr >= structural_break
            qidx_arr, sales_arr, quarters_arr = qidx_arr[keep], sales_arr[keep], quarters_arr[keep]

        # Pass 2 — seasonality recomputed on post-break history, then outliers
        # detected against that.
        self.seasonal_indices = compute_seasonal_indices(qidx_arr, quarters_arr, sales_arr)
        self.outliers = self._detect_outliers(
            self._deseasonalise(sales_arr, quarters_arr), qidx_arr
        )
        kept = ~np.isin(qidx_arr, self.outliers)
        if kept.sum() < 4:
            self.outliers = []
            kept = np.ones(len(qidx_arr), dtype=bool)

        self.data_excl_outliers = df[df["qidx"].isin(qidx_arr[kept])].copy().reset_index(drop=True)

        # Pass 3 — final seasonality, free of both pre-break and outlier
        # quarters, so "normal for a Q4" is measured on normal quarters only.
        self.seasonal_indices = compute_seasonal_indices(
            qidx_arr[kept], quarters_arr[kept], sales_arr[kept]
        )

        n_fit = len(self.data_excl_outliers)
        if n_fit <= 7:
            self.tier, self.confidence = "MINIMAL", "low"
        elif n_fit <= 15:
            self.tier, self.confidence = "LIMITED", "low"
        else:
            self.tier = "FULL"
            self.confidence = "low" if self.structural_break is not None else "normal"

        # Excluding a large share of quarters is itself the finding: it points at
        # an inconsistent reporting basis rather than a handful of odd quarters.
        n_post_break = len(qidx_arr)
        if n_post_break and len(self.outliers) / n_post_break > HIGH_OUTLIER_RATE:
            self.flag_reasons.append(
                f"high_outlier_rate_{len(self.outliers)}of{n_post_break}_quarters_excluded"
            )
            if self.confidence == "normal":
                self.confidence = "low"

    def _tier_methods(self) -> tuple:
        if self.tier == "MINIMAL":
            return _GROWTH_ONLY_METHODS
        return _DESEASONALIZED_METHODS + ("seasonal_naive",)

    # -------------------------------------------------------------------------
    # SUMMARY STATS
    # -------------------------------------------------------------------------

    def summary_stats(self, exclude_outliers: bool = True) -> dict:
        df = self.data_excl_outliers if exclude_outliers else self.clean_data
        sales = df["sales"].values
        n = len(sales)
        base = {
            "tier": self.tier,
            "confidence": self.confidence,
            "structural_break": (_qidx_to_label(self.structural_break)
                                 if self.structural_break else None),
            "flag_reasons": list(self.flag_reasons),
            "plausible": self.plausible,
            "total_history_quarters": 0 if self.clean_data is None else len(self.clean_data),
        }
        if n == 0:
            return {**base, "n_quarters": 0, "outliers_excluded": []}

        growth = np.diff(sales) / sales[:-1] if n > 1 else np.array([])
        years_span = (df["qidx"].iloc[-1] - df["qidx"].iloc[0]) / 4
        cagr = (np.power(sales[-1] / sales[0], 1 / years_span) - 1) if years_span > 0 and sales[0] > 0 else 0
        return {
            **base,
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
        qoq_growth = np.power(ds[-1] / ds[-lb], 1 / n_q) - 1 if n_q > 0 and ds[-lb] > 0 else 0
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
        every = {
            "linear": self._forecast_linear,
            "cagr": self._forecast_cagr,
            "exp_smoothing": self._forecast_exp_smoothing,
            "holt": self._forecast_holt,
            "ma_trend": self._forecast_ma_trend,
            "weighted_avg": self._forecast_weighted_avg,
            "seasonal_naive": self._forecast_seasonal_naive,
        }
        return {key: every[key] for key in self._tier_methods()}

    # -------------------------------------------------------------------------
    # BACKTEST
    # -------------------------------------------------------------------------

    def backtest(self, holdout_quarters: int = 4, exclude_outliers: bool = True) -> pd.DataFrame:
        """Rolling-origin (walk-forward) validation: train through quarter t,
        predict t+1, step forward, average the errors over every viable origin.
        `holdout_quarters` is kept for call-site compatibility and unused."""
        df = self.data_excl_outliers if exclude_outliers else self.clean_data
        self.backtest_results = pd.DataFrame()
        self.backtest_detail = {}
        self.best_method = None
        self.mape = None
        if df is None or len(df) < 6 or self.tier in ("NO_DATA", "FLAT", "MINIMAL"):
            self.best_method = {"MINIMAL": "cagr", "FLAT": "flat_carry"}.get(self.tier)
            return self.backtest_results

        n = len(df)
        min_train = max(4, n - 4) if self.tier == "LIMITED" else min(MIN_TRAIN_FULL, n - 4)
        min_train = max(4, min(min_train, n - 1))

        methods = self._all_methods()
        errors = {key: [] for key in methods}
        detail = {key: [] for key in methods}
        actual_sales = df["sales"].values
        actual_qidx = df["qidx"].values

        for split in range(min_train, n):
            train = df.iloc[:split]
            actual = float(actual_sales[split])
            if actual == 0:
                continue
            # Removing outliers leaves gaps, so the next *observation* is not
            # always the next calendar quarter. The trend methods reseasonalise
            # at the quarter immediately after their training window; restate
            # that at the quarter actually being predicted, or a Q1 prediction
            # gets carried out with Q3's seasonal factor.
            assumed_quarter = int(train["qidx"].iloc[-1] + 1) % 4 or 4
            target_quarter = int(df["quarter"].iloc[split])
            seasonal_fix = (self.seasonal_indices[target_quarter - 1]
                            / self.seasonal_indices[assumed_quarter - 1])

            for key, method in methods.items():
                try:
                    predicted = float(method(train, 1)["forecast"][0])
                except Exception:
                    continue
                if key != "seasonal_naive":
                    predicted *= seasonal_fix
                error_pct = abs(predicted - actual) / abs(actual) * 100
                errors[key].append(error_pct)
                detail[key].append({
                    "predicting_quarter": _qidx_to_label(int(actual_qidx[split])),
                    "trained_through": _qidx_to_label(int(train["qidx"].iloc[-1])),
                    "predicted": round(predicted, 2),
                    "actual": round(actual, 2),
                    "error_pct": round(error_pct, 1),
                })

        results = []
        for key, scored in errors.items():
            if not scored:
                continue
            residuals = [d["predicted"] - d["actual"] for d in detail[key]]
            results.append({
                "method": key,
                # Full precision for the sort; rounded only once ranked.
                "mape": float(np.mean(scored)),
                "bias": round(float(np.mean(residuals)), 1),
                "rmse": round(float(np.sqrt(np.mean(np.square(residuals)))), 1),
                "n_origins": len(scored),
            })

        self.backtest_detail = detail
        self.backtest_results = pd.DataFrame(results)
        if len(self.backtest_results):
            self.backtest_results = self.backtest_results.sort_values("mape").reset_index(drop=True)
            self.backtest_results["mape"] = self.backtest_results["mape"].round(2)
            self.best_method = self.backtest_results.loc[0, "method"]
            self.mape = float(self.backtest_results.loc[0, "mape"])
        else:
            self.best_method = "holt"
        return self.backtest_results

    # -------------------------------------------------------------------------
    # FORECAST
    # -------------------------------------------------------------------------

    def forecast(self, periods: int = 20, exclude_outliers: bool = True) -> pd.DataFrame:
        df = self.data_excl_outliers if exclude_outliers else self.clean_data
        if df is None or df.empty:
            self.final_forecast = pd.DataFrame()
            return self.final_forecast

        if self.tier == "FLAT":
            return self._flat_forecast(df, periods)

        self.forecasts = {}
        for name, method in self._all_methods().items():
            try:
                self.forecasts[name] = method(df, periods)
            except Exception:
                pass
        if not self.forecasts:
            return self._flat_forecast(df, periods)

        sample = next(iter(self.forecasts.values()))
        results = pd.DataFrame({
            "fiscal_year": sample["years"].astype(int),
            "fiscal_quarter": sample["quarters"].astype(int),
            "label": [f"Q{q} {y}" for y, q in zip(sample["years"], sample["quarters"])],
        })
        for name, payload in self.forecasts.items():
            results[name] = np.round(payload["forecast"], 4)

        # Ensemble: top-3 by rolling-origin MAPE. backtest_results is already
        # ordered by exact MAPE, so take the head rather than re-ranking on the
        # rounded column.
        top3 = []
        if self.backtest_results is not None and len(self.backtest_results) >= 3:
            top3 = [key for key in self.backtest_results["method"].head(3).tolist()
                    if key in self.forecasts]
        if not top3:
            # No backtest to rank on (thin history) — blend the methods the tier
            # allows rather than insisting on a fixed trio that may not be fitted.
            top3 = list(self.forecasts)[:3]
        self.ensemble_methods = top3
        results["ensemble"] = np.round(results[top3].mean(axis=1), 4)

        if "ci_lower" in sample:
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
        self._check_plausibility(results, df)
        return results

    def _flat_forecast(self, df: pd.DataFrame, periods: int) -> pd.DataFrame:
        """Three quarters or fewer: no growth rate and no seasonality can be
        estimated, so carry the last level forward rather than invent a trend."""
        last = float(df["sales"].iloc[-1])
        fq, fquarters, fyears = self._future_quarters(int(df["qidx"].iloc[-1]), periods)
        results = pd.DataFrame({
            "fiscal_year": fyears.astype(int),
            "fiscal_quarter": fquarters.astype(int),
            "label": [f"Q{q} {y}" for y, q in zip(fyears, fquarters)],
        })
        for name in _METHODS:
            results[name] = last
        results["ensemble"] = last
        for label in ("pessimistic", "baseline", "optimistic"):
            results[f"scenario_{label}"] = last
        self.forecasts = {}
        self.ensemble_methods = []
        self.final_forecast = results
        self._check_plausibility(results, df)
        return results

    def _check_plausibility(self, results: pd.DataFrame, df: pd.DataFrame) -> None:
        """Sanity-check the ensemble. A number exists either way; this decides
        whether it means anything."""
        self.plausible = True
        self.flag_reasons = [r for r in self.flag_reasons
                             if not r.startswith(("implied_cagr", "long_horizon_extrapolation"))
                             and r != "forecast_non_positive"]
        if results.empty or "ensemble" not in results.columns or df.empty:
            return

        values = pd.to_numeric(results["ensemble"], errors="coerce").dropna()
        if values.empty:
            return
        if (values <= 0).any():
            self.plausible = False
            self.flag_reasons.append("forecast_non_positive")

        last_sales = float(df["sales"].iloc[-1])
        last_qidx = int(self.clean_data["qidx"].iloc[-1])
        final_qidx = int(results["fiscal_year"].iloc[-1]) * 4 + int(results["fiscal_quarter"].iloc[-1])
        quarters_out = final_qidx - last_qidx
        if last_sales > 0 and quarters_out > 0:
            implied_cagr = (float(values.iloc[-1]) / last_sales) ** (4 / quarters_out) - 1
            if implied_cagr > IMPLAUSIBLE_GROWTH_CAGR or implied_cagr < IMPLAUSIBLE_DECLINE_CAGR:
                self.plausible = False
                self.flag_reasons.append(f"implied_cagr_{implied_cagr:.0%}")

            # A modest per-quarter rate still compounds a long way over 20+
            # quarters. The annualised check dilutes that; this catches the
            # absolute multiple when the history behind it is thin.
            if self.tier in ("MINIMAL", "LIMITED") and quarters_out > 16:
                multiple = float(values.iloc[-1]) / last_sales
                if multiple > 3 or multiple < 0.33:
                    self.plausible = False
                    self.flag_reasons.append(
                        f"long_horizon_extrapolation_{quarters_out}q_from_{len(df)}pts_{multiple:.1f}x"
                    )

        if not self.plausible:
            self.confidence = "flagged_implausible"

    def needs_review(self) -> bool:
        """True when the forecast should not be taken at face value."""
        return bool(
            not self.plausible
            or (self.mape is not None and self.mape > 25)
            or self.flag_reasons
            or self.tier in ("NO_DATA", "FLAT", "MINIMAL")
        )

    # -------------------------------------------------------------------------
    # FULL PIPELINE
    # -------------------------------------------------------------------------

    def run(self, forecast_periods: int = 20, holdout_quarters: int = 4) -> dict:
        historical = self.clean_data.copy()
        historical["qoq_growth_pct"] = (historical["sales"].pct_change() * 100).round(1)
        historical["yoy_growth_pct"] = (historical["sales"].pct_change(4) * 100).round(1)
        historical["is_outlier"] = historical["qidx"].isin(self.outliers)

        backtest_df = self.backtest(holdout_quarters=holdout_quarters)
        forecast_df = self.forecast(periods=forecast_periods)
        # After forecast(), so the plausibility flag is in the returned stats.
        stats_data = self.summary_stats()
        return {
            "summary_stats": stats_data,
            "needs_review": self.needs_review(),
            "historical": historical,
            "backtest": backtest_df,
            "forecast": forecast_df,
            "best_method": self.best_method,
            "raw_forecasts": self.forecasts,
        }
