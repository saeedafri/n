"""
RetailerForecaster — revenue forecasting engine.
Originally extracted from docs/CIQ_Estimates.ipynb.aspx.

The engine now supports both Excel uploads and in-memory dataframes so the
same six-model forecasting logic can run on staging-database actual revenue
series as well as ad hoc spreadsheet inputs.

The modelling rules follow the data-science MDP Forecasting System revision:
combined z-score + IQR outlier detection, structural-break truncation,
gap-aware annualised growth rates, rolling-origin backtesting, data tiers and
a plausibility check on the ensemble.
"""

import io
import pandas as pd
import numpy as np
from scipy import stats
import warnings

warnings.filterwarnings("ignore")

# Modelling constants — tune here, not scattered through the code.
STRUCTURAL_BREAK_THRESHOLD = 0.40      # one-year decline magnitude to consider a break
STRUCTURAL_BREAK_PERSIST_FACTOR = 0.6  # how much of the drop must persist afterward
OUTLIER_Z_THRESHOLD = 1.5
OUTLIER_IQR_MULT = 1.5
MIN_TRAIN_FULL = 4                     # backtest min_train for 6+ year (FULL) companies
IMPLAUSIBLE_GROWTH_CAGR = 0.60         # flag if implied annualised growth exceeds this
IMPLAUSIBLE_DECLINE_CAGR = -0.25       # flag if implied annualised decline exceeds this

_METHOD_LABELS = {
    "linear": "Linear Regression", "cagr": "CAGR", "exp_smoothing": "Exp Smoothing",
    "holt": "Holt's Linear", "ma_trend": "MA Trend", "weighted_avg": "Weighted Avg",
}
_GROWTH_ONLY_METHODS = ("cagr", "ma_trend", "weighted_avg")


PARTIAL_PERIOD_FRACTION = 0.10


def drop_partial_periods(df: pd.DataFrame, value_col: str = "sales") -> pd.DataFrame:
    """Remove stub rows that cover only part of a reporting period.

    A stub is a period that is tiny *compared with the periods either side of
    it* — a YF phantom Dec-31 row for a January fiscal-year end, or a short
    transition year. Comparing against the all-time peak instead (the previous
    rule) also deleted the genuine early years of any company that has since
    grown more than tenfold, which silently truncated the history of exactly
    the fast-growing names the forecast is least able to guess at.

    Leading rows are therefore never dropped for being small — the start of a
    growth ramp is real data. Only an interior row starved on both sides, or a
    trailing row that collapses against the period before it, is a stub.
    """
    if df is None or df.empty:
        return df
    values = pd.to_numeric(df[value_col], errors="coerce")

    # Revenue cannot be negative and a zero period carries no information. Where
    # one appears it is a restatement or sign artifact in the source, and it
    # poisons every growth rate, seasonal factor and break test computed from
    # it — so it is removed as invalid rather than modelled.
    df = df[values > 0].reset_index(drop=True)
    if len(df) < 2:
        return df

    values = pd.to_numeric(df[value_col], errors="coerce").values
    keep = np.ones(len(values), dtype=bool)
    for i in range(1, len(values)):
        previous = values[i - 1]
        following = values[i + 1] if i + 1 < len(values) else None
        if values[i] >= PARTIAL_PERIOD_FRACTION * previous:
            continue
        if following is None or values[i] < PARTIAL_PERIOD_FRACTION * following:
            keep[i] = False
    return df[keep].reset_index(drop=True)


def annualized_growth_rates(years, sales):
    """Growth rates annualised by the actual year gap, not by row position.

    A 2019 → 2022 jump is three years of compounding, not one period of growth;
    treating it as one period is what made gapped histories forecast wild.
    """
    years, sales = np.asarray(years, dtype=float), np.asarray(sales, dtype=float)
    rates = []
    for i in range(1, len(years)):
        gap = years[i] - years[i - 1]
        if gap <= 0 or sales[i - 1] <= 0:
            continue
        rates.append((sales[i] / sales[i - 1]) ** (1 / gap) - 1)
    return np.array(rates)


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
        self.clean_data = drop_partial_periods(tmp)
        if not self.clean_data.empty:
            self.clean_data["year"] = self.clean_data["year"].astype(int)
        self._build_fitting_frame()

    # -------------------------------------------------------------------------
    # DATA QUALITY: structural break → outliers → tier
    # -------------------------------------------------------------------------

    def _detect_structural_break(self, years, sales):
        """Year of the last large, *persistent* one-year decline (spinoff,
        divestiture, discontinued segment). Pre-break history is not evidence
        about the business that exists today, so it is dropped from the fit."""
        if len(sales) < 4:
            return None
        breaks = []
        for i in range(1, len(sales)):
            gap = years[i] - years[i - 1]
            if gap <= 0 or sales[i - 1] <= 0:
                continue
            change = (sales[i] / sales[i - 1]) ** (1 / gap) - 1
            if change < -STRUCTURAL_BREAK_THRESHOLD:
                breaks.append(years[i])
        if not breaks:
            return None
        break_year = breaks[-1]
        idx = list(years).index(break_year)
        if idx >= len(sales) - 1:
            return None  # nothing after the break to confirm it persisted
        pre_level, post_level = sales[idx - 1], np.mean(sales[idx:])
        if (pre_level - post_level) / pre_level > STRUCTURAL_BREAK_THRESHOLD * STRUCTURAL_BREAK_PERSIST_FACTOR:
            return break_year
        return None

    def _detect_outliers(self, years, sales) -> list:
        """Combined z-score and IQR test on gap-aware growth rates. Either test
        firing is enough — z-score alone misses anomalies in a series whose own
        volatility inflates the standard deviation."""
        if len(sales) < 4:
            return []
        rates = annualized_growth_rates(years, sales)
        if len(rates) < 3:
            return []
        flagged = set()
        std_g = rates.std()
        if std_g > 0:
            z = (rates - rates.mean()) / std_g
            flagged.update(years[i + 1] for i, zi in enumerate(z) if abs(zi) > OUTLIER_Z_THRESHOLD)
        q1, q3 = np.percentile(rates, 25), np.percentile(rates, 75)
        iqr = q3 - q1
        if iqr > 0:
            lower, upper = q1 - OUTLIER_IQR_MULT * iqr, q3 + OUTLIER_IQR_MULT * iqr
            flagged.update(years[i + 1] for i, r in enumerate(rates) if r < lower or r > upper)
        return sorted(int(y) for y in flagged)

    def _build_fitting_frame(self) -> None:
        """Set the tier, drop pre-break and outlier years, and expose the frame
        every downstream fit runs on (`data_excluding_outliers`)."""
        self.flag_reasons = []
        self.structural_break = None
        self.outliers = []
        df = self.clean_data
        n = 0 if df is None else len(df)

        if n == 0:
            self.tier, self.confidence = "NO_DATA", "none"
            self.data_excluding_outliers = df.copy() if df is not None else None
            return
        if n == 1:
            self.tier, self.confidence = "FLAT", "very_low"
            self.data_excluding_outliers = df.copy()
            return

        years, sales = df["year"].values, df["sales"].values

        # Structural break is tested BEFORE outlier screening here, the opposite
        # of the quarterly engine — and deliberately so. A break is itself a large
        # decline, so on an annual series the outlier test flags the break year
        # and screening first would delete the evidence of the very thing being
        # detected. Measured on the 347-company reference set, reversing the order
        # loses the BBWI/DLTR/COTY 2020-21 breaks and drives backtest error from
        # 4.1%/1.8%/6.4% to 17.1%/17.5%/15.2%, with WDC extrapolating negative.
        # Quarterly can afford the other order because 80+ points distinguish a
        # one-off quarter from a permanent shift; ~20 annual points cannot.
        break_year = self._detect_structural_break(years, sales)
        if break_year is not None and (years >= break_year).sum() >= 2:
            self.structural_break = int(break_year)
            self.flag_reasons.append(f"structural_break_{int(break_year)}_pre_break_data_excluded")
            keep = years >= break_year
            years, sales = years[keep], sales[keep]

        self.outliers = self._detect_outliers(years, sales)
        kept = ~np.isin(years, self.outliers)
        if kept.sum() < 2:
            self.outliers = []
            kept = np.ones(len(years), dtype=bool)

        self.data_excluding_outliers = pd.DataFrame(
            {"year": years[kept].astype(int), "sales": sales[kept]}
        ).reset_index(drop=True)

        # Tier reflects how much *history* the company has, so it is measured on
        # the post-break series before outlier removal — dropping one anomalous
        # year does not make a long-established company data-poor.
        n_fit = len(years)
        if n_fit <= 2:
            self.tier, self.confidence = "MINIMAL", "low"
        elif n_fit <= 5:
            self.tier, self.confidence = "LIMITED", "low"
        else:
            self.tier = "FULL"
            # A structural break means the usable history is younger than the row
            # count suggests — don't advertise normal confidence in that case.
            self.confidence = "low" if self.structural_break is not None else "normal"

    def _tier_methods(self) -> tuple:
        """Method keys allowed for the current tier. Two data points cannot
        support a regression or a smoother — only a growth rate."""
        if self.tier == "MINIMAL":
            return _GROWTH_ONLY_METHODS
        return tuple(_METHOD_LABELS.keys())

    # -------------------------------------------------------------------------
    # SUMMARY STATS
    # -------------------------------------------------------------------------

    def summary_stats(self, exclude_outliers: bool = True) -> dict:
        df = self.data_excluding_outliers if exclude_outliers else self.clean_data
        sales = df["sales"].values
        years = df["year"].values
        n = len(sales)
        base = {
            "tier": self.tier,
            "confidence": self.confidence,
            "structural_break": self.structural_break,
            "flag_reasons": list(self.flag_reasons),
            "plausible": self.plausible,
            "total_history_years": 0 if self.clean_data is None else len(self.clean_data),
        }
        if n == 0:
            return {**base, "n_years": 0, "outliers_excluded": []}

        growth_rates = annualized_growth_rates(years, sales)
        span = years[-1] - years[0]
        cagr = (np.power(sales[-1] / sales[0], 1 / span) - 1) if span > 0 and sales[0] > 0 else 0
        return {
            **base,
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
        # Span in *years*, not rows — a gapped history compounds over the gap.
        if lookback and lookback < len(sales):
            base, n = sales[-lookback], years[-1] - years[-lookback]
        else:
            base, n = sales[0], years[-1] - years[0]
        cagr = np.power(sales[-1] / base, 1 / n) - 1 if n > 0 and base > 0 else 0
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
        growth_rates = annualized_growth_rates(years, sales)
        if len(growth_rates) == 0:
            avg_growth = 0.0
        else:
            avg_growth = np.mean(growth_rates[-window:]) if len(growth_rates) >= window else np.mean(growth_rates)
        fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
        forecast, last = [], sales[-1]
        for _ in range(periods):
            last = last * (1 + avg_growth)
            forecast.append(last)
        return {"method": "MA Trend", "forecast": np.array(forecast), "years": fy}

    def _forecast_weighted_avg(self, years, sales, periods):
        growth_rates = annualized_growth_rates(years, sales)
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

    def _method_callables(self) -> dict:
        every = {
            "linear": self._forecast_linear,
            "cagr": self._forecast_cagr,
            "exp_smoothing": self._forecast_exp_smoothing,
            "holt": self._forecast_holt,
            "ma_trend": self._forecast_ma_trend,
            "weighted_avg": self._forecast_weighted_avg,
        }
        return {k: every[k] for k in self._tier_methods()}

    def backtest(self, holdout_years: int = 2, exclude_outliers: bool = True) -> pd.DataFrame:
        """Rolling-origin (walk-forward) validation.

        Every viable origin is used: train on years up to t, predict t+1, step
        forward. A single fixed holdout scored each method on one or two lucky
        years; MAPE here is the average over every year the method could have
        been asked to predict. `holdout_years` is accepted for call-site
        compatibility and no longer used.
        """
        df = self.data_excluding_outliers if exclude_outliers else self.clean_data
        self.backtest_results = pd.DataFrame()
        self.backtest_detail = {}
        self.best_method = None
        self.mape = None
        if df is None or len(df) < 3 or self.tier in ("NO_DATA", "FLAT", "MINIMAL"):
            # Fewer than three points leaves no origin with data on both sides.
            self.best_method = {"MINIMAL": "cagr", "FLAT": "flat_carry"}.get(self.tier)
            return self.backtest_results

        years, sales = df["year"].values, df["sales"].values
        n = len(sales)
        min_train = max(2, n - 2) if self.tier == "LIMITED" else min(MIN_TRAIN_FULL, n - 2)
        min_train = max(2, min(min_train, n - 1))

        methods = self._method_callables()
        errors = {key: [] for key in methods}
        detail = {key: [] for key in methods}
        for split in range(min_train, n):
            train_years, train_sales = years[:split], sales[:split]
            actual_year, actual = years[split], sales[split]
            if actual == 0:
                continue
            for key, method in methods.items():
                try:
                    predicted = float(method(train_years, train_sales, 1)["forecast"][0])
                except Exception:
                    continue
                error_pct = abs(predicted - actual) / actual * 100
                errors[key].append(error_pct)
                detail[key].append({
                    "predicting_year": int(actual_year),
                    "trained_on": f"{int(train_years[0])}-{int(train_years[-1])}",
                    "predicted": round(predicted, 2),
                    "actual": round(float(actual), 2),
                    "error_pct": round(error_pct, 1),
                })

        results = []
        for key, scored in errors.items():
            if not scored:
                continue
            predictions = [d["predicted"] - d["actual"] for d in detail[key]]
            results.append({
                "method": key,
                "display": _METHOD_LABELS[key],
                # Full precision here: rounding before the sort turns near-misses
                # into ties and lets the wrong method win the selection.
                "mape": float(np.mean(scored)),
                "bias": round(float(np.mean(predictions)), 1),
                "rmse": round(float(np.sqrt(np.mean(np.square(predictions)))), 1),
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
            self.best_method = "holt" if self.tier == "FULL" else "cagr"
        return self.backtest_results

    # -------------------------------------------------------------------------
    # FORECAST
    # -------------------------------------------------------------------------

    def forecast(self, periods: int = 5, exclude_outliers: bool = True) -> pd.DataFrame:
        df = self.data_excluding_outliers if exclude_outliers else self.clean_data
        if df is None or df.empty:
            self.final_forecast = pd.DataFrame()
            return self.final_forecast

        years, sales = df["year"].values, df["sales"].values
        future_years = np.arange(years[-1] + 1, years[-1] + 1 + periods)

        if self.tier == "FLAT":
            # One data point: there is no growth rate to estimate, so carrying the
            # level forward is the only honest answer.
            results = pd.DataFrame({"year": future_years})
            for key in _METHOD_LABELS:
                results[key] = float(sales[-1])
            results["ensemble"] = float(sales[-1])
            for label in ("pessimistic", "baseline", "optimistic"):
                results[f"scenario_{label}"] = float(sales[-1])
            self.forecasts = {}
            self.ensemble_methods = []
            self.final_forecast = results
            self._check_plausibility(results, sales[-1], years[-1])
            return results

        raw = {}
        for key, method in self._method_callables().items():
            try:
                raw[key] = method(years, sales, periods)
            except Exception:
                pass
        self.forecasts = raw

        results = pd.DataFrame({"year": future_years})
        for key, payload in raw.items():
            results[key] = payload["forecast"]

        # Ensemble: top-3 by rolling-origin MAPE, falling back to the methods
        # that survive on thin data.
        if self.backtest_results is not None and len(self.backtest_results) >= 3:
            # backtest_results is already ordered by exact MAPE — take the head
            # rather than re-ranking on the rounded column.
            top3 = [key for key in self.backtest_results["method"].head(3).tolist() if key in raw]
        else:
            top3 = []
        if not top3:
            # No backtest to rank on (thin history) — blend the methods the tier
            # allows rather than insisting on a fixed trio that may not be fitted.
            top3 = list(raw)[:3]
        self.ensemble_methods = top3
        results["ensemble"] = results[top3].mean(axis=1) if top3 else float(sales[-1])

        # Scenarios: retained for the dashboard's range view. They are a spread
        # around history, not a validated forecast — the plausibility flag below
        # is what says whether the ensemble itself can be trusted.
        growth_rates = annualized_growth_rates(years, sales)
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
        self._check_plausibility(results, sales[-1], years[-1])
        return results

    def _check_plausibility(self, results: pd.DataFrame, last_sales: float, last_year: int) -> None:
        """Sanity-check the ensemble against what a real business can do.

        A forecast is a number whether or not it means anything; this is what
        separates 'the model produced output' from 'the output is usable'.
        """
        self.plausible = True
        self.flag_reasons = [r for r in self.flag_reasons
                             if not r.startswith("implied_cagr") and r != "forecast_non_positive"]
        if results.empty or "ensemble" not in results.columns:
            return

        values = pd.to_numeric(results["ensemble"], errors="coerce").dropna()
        if values.empty:
            return
        if (values <= 0).any():
            self.plausible = False
            self.flag_reasons.append("forecast_non_positive")

        horizon = int(results["year"].iloc[-1]) - int(last_year)
        if last_sales > 0 and horizon > 0:
            implied_cagr = (float(values.iloc[-1]) / last_sales) ** (1 / horizon) - 1
            if implied_cagr > IMPLAUSIBLE_GROWTH_CAGR or implied_cagr < IMPLAUSIBLE_DECLINE_CAGR:
                self.plausible = False
                self.flag_reasons.append(f"implied_cagr_{implied_cagr:.0%}")

        if not self.plausible:
            self.confidence = "flagged_implausible"

    def needs_review(self) -> bool:
        """True when the number should not be taken at face value: an
        implausible ensemble, a backtest that missed badly on its own history,
        or any data-quality flag raised while fitting."""
        return bool(
            not self.plausible
            or (self.mape is not None and self.mape > 25)
            or self.flag_reasons
            or self.tier in ("NO_DATA", "FLAT", "MINIMAL")
        )

    # -------------------------------------------------------------------------
    # FULL PIPELINE
    # -------------------------------------------------------------------------

    def run(self, forecast_periods: int = 5, holdout_years: int = 2) -> dict:
        historical = self.clean_data.copy()
        historical["growth_pct"] = (historical["sales"].pct_change() * 100).round(1)
        historical["is_outlier"] = historical["year"].isin(self.outliers)

        backtest_df = self.backtest(holdout_years=holdout_years)
        forecast_df = self.forecast(periods=forecast_periods)
        # After forecast(), so the plausibility flag is in the returned stats.
        stats_data = self.summary_stats()

        return {
            "summary_stats": stats_data,
            "historical": historical,
            "backtest": backtest_df,
            "forecast": forecast_df,
            "best_method": self.best_method,
            "raw_forecasts": self.forecasts,
            "needs_review": self.needs_review(),
        }

    def run_full_analysis(self, forecast_periods: int = 5, holdout_years: int = 2) -> dict:
        """Compatibility alias for the original notebook API."""
        return self.run(forecast_periods=forecast_periods, holdout_years=holdout_years)
