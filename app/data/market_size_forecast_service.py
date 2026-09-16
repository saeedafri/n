"""
Market Size Forecasting — compute engine.

Port of the data-science team's standalone `app_New.py` Streamlit tool
("Sales Forecast Intelligence") into the portal. Everything here is pure
Python: no Streamlit, no session state, no disk writes. The page module
(`app/pages/market_size_forecasting.py`) owns all rendering.

Three models run over an uploaded spreadsheet:

  SARIMAX  seasonal ARIMA + FRED macro exogenous variables chosen by
           Granger causality, then a combination search ranked on RMSE
  SARIMA   pure seasonal ARIMA, order picked by AIC grid search
  Prophet  trend/seasonality with a COVID-period regressor

The maths is deliberately kept identical to the original script so the
portal reproduces the numbers the research team already validated. Where the
original could not run unchanged it is noted inline with "PORT:".
"""

from __future__ import annotations

import hashlib
import os
import pickle
import re
import tempfile
import time
import warnings
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from time import perf_counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from pandas.tseries.offsets import MonthEnd
from scipy import stats
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.stats.stattools import durbin_watson
from statsmodels.tsa.seasonal import seasonal_decompose
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.stattools import adfuller, grangercausalitytests, kpss

from utils.server_logger import log_structured_error, log_timing

warnings.filterwarnings("ignore")

TARGET_COL = "Sales"
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_WORKERS = 16

# SARIMAX leans on macro variables and repeated differencing; below this many
# observations neither is meaningful and the fit throws instead of degrading.
SARIMAX_MIN_OBS = 24

FRED_VARS: Dict[str, str] = {
    'UNRATENSA'      : 'Unemployment Rate',
    'LNU01300000'    : 'Labor Force Participation Rate',
    'HOUSTNSA'       : 'New Privately-Owned Housing Units Started: Total Units',
    'HSN1FNSA'       : 'New One Family Houses Sold: United States',
    'GASREGCOVM'     : 'US Regular Conventional Gas Price',
    'UMCSENT'        : 'University of Michigan: Consumer Sentiment',
    'CSUSHPINSA'     : 'S&P/Case-Shiller U.S. National Home Price Index',
    'PPIACO'         : 'Producer Price Index by Commodity: All Commodities',
    'T10YIEM'        : '10-Year Breakeven Inflation Rate',
    'TOTALNS'        : 'Total Consumer Credit Owned and Securitized',
    'CCLACBM027NBOG' : 'Consumer Loans: Credit Cards and Other Revolving Plans',
    'MICH'           : 'University of Michigan: Inflation Expectation',
    'CPALTT01USM657N': 'Consumer Price Index: Total All Items for the United States',
    'CONSUMERNSA'    : 'Consumer Loans, All Commercial Banks',
    'IPB51000N'      : 'Industrial Production: Consumer Goods',
    'RETAILIMNSA'    : 'Retailers Inventories',
    'MNFCTRIMNSA'    : 'Manufacturers Inventories',
    'TOTBUSIRNSA'    : 'Total Business: Inventories to Sales Ratio',
    'RETAILIRNSA'    : 'Retailers: Inventories to Sales Ratio',
    'TOTBUSIMNSA'    : 'Total Business Inventories',
    'UCOGNO'         : 'Manufacturers New Orders: Consumer Goods',
    'UCDGNO'         : 'Manufacturers New Orders: Consumer Durable Goods',
    'UMNMNO'         : 'Manufacturers New Orders: Nondurable Goods',
    'UMDMNO'         : 'Manufacturers New Orders: Durable Goods',
    'UMTMVS'         : 'Manufacturers Value of Shipments: Total Manufacturing',
    'WHLSLRIRNSA'    : 'Merchant Wholesalers: Inventories to Sales Ratio',
    'MNFCTRIRNSA'    : 'Manufacturers: Inventories to Sales Ratio',
    'CEU4349200001'  : 'All Employees, Couriers and Messengers',
    'IR'             : 'Import Price Index (End Use): All Commodities',
    'CES0500000003'  : 'Average Hourly Earnings of All Employees, Total Private',
    'FEDFUNDS'       : 'Federal Funds Effective Rate',
    'A229RX0'        : 'Real Disposable Personal Income: Per Capita',
}

FREQ_CONFIGS: Dict[str, Dict[str, Any]] = {
    'Weekly': {
        'freq': 'W',  'alias': 'W', 's': 52, 'max_lag': 52, 'pred_default': 4,
        'cv_initial': '730 days', 'cv_period': '90 days', 'cv_horizon': '30 days',
        'decomp_period': 52, 'covid_rule': 'weekly', 'resample': 'ffill',
        'label': 'Weekly', 'unit': 'weeks', 'max_horizon': 260,
    },
    'Monthly': {
        'freq': 'ME', 'alias': 'ME', 's': 12, 'max_lag': 12, 'pred_default': 2,
        'cv_initial': '1095 days', 'cv_period': '182 days', 'cv_horizon': '92 days',
        'decomp_period': 12, 'covid_rule': 'monthly', 'resample': None,
        'label': 'Monthly', 'unit': 'months', 'max_horizon': 60,
    },
    'Quarterly': {
        'freq': 'QE', 'alias': 'QE', 's': 4, 'max_lag': 8, 'pred_default': 2,
        'cv_initial': '1095 days', 'cv_period': '182 days', 'cv_horizon': '92 days',
        'decomp_period': 4, 'covid_rule': 'quarterly', 'resample': 'mean',
        'label': 'Quarterly', 'unit': 'quarters', 'max_horizon': 20,
    },
    'Annual': {
        'freq': 'YE', 'alias': 'YE', 's': 1, 'max_lag': 5, 'pred_default': 1,
        'cv_initial': '1825 days', 'cv_period': '365 days', 'cv_horizon': '365 days',
        'decomp_period': None, 'covid_rule': 'annual', 'resample': 'mean',
        'label': 'Annual', 'unit': 'years', 'max_horizon': 5,
    },
}

FREQ_NAMES = list(FREQ_CONFIGS)


# ═══════════════════════════════════════════════════════════════════
#  FREQUENCY, DATES, COLUMN DETECTION
# ═══════════════════════════════════════════════════════════════════

def detect_frequency(date_index) -> str:
    """Infer data frequency from the median gap between consecutive dates."""
    if len(date_index) < 2:
        return 'Monthly'
    median_gap = pd.Series(date_index).diff().dropna().dt.days.median()
    if median_gap <= 10:
        return 'Weekly'
    if median_gap <= 45:
        return 'Monthly'
    if median_gap <= 100:
        return 'Quarterly'
    return 'Annual'


QUARTER_END = {'1': '-03-31', '2': '-06-30', '3': '-09-30', '4': '-12-31'}


def _looks_like_year(value) -> bool:
    try:
        as_int = int(float(value))
        return 1900 <= as_int <= 2100 and float(value) == as_int
    except (ValueError, TypeError):
        return False


def _parse_quarter(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace('-', ' ').replace('_', ' ')
    match = re.search(r'Q([1-4])\s*(\d{4})', text) or re.search(r'(\d{4})\s*Q([1-4])', text)
    if not match:
        compact = re.search(r'Q([1-4])(\d{4})', text) or re.search(r'(\d{4})Q([1-4])', text)
        if not compact:
            return None
        groups = compact.groups()
        quarter, year = (groups[0], groups[1]) if len(groups[0]) == 1 else (groups[1], groups[0])
        return f"{year}{QUARTER_END[quarter]}"
    groups = match.groups()
    if len(groups[0]) == 1:
        quarter, year = groups[0], groups[1]
    else:
        year, quarter = groups[0], groups[1]
    return f"{year}{QUARTER_END[quarter]}"


def normalize_dates(series: pd.Series) -> pd.Series:
    """Turn messy period labels into real dates.

    Beyond ordinary date strings this handles bare years (2020 -> 2020-12-31)
    and quarter labels ('Q1 2020', '2020Q1', '2020-Q1'). Bare years matter:
    pandas otherwise reads the integer 2020 as nanoseconds and lands in 1970.
    Unparseable entries come back as NaT.
    """
    values = series.copy()
    populated = values.dropna()

    if len(populated) > 0 and populated.apply(_looks_like_year).mean() >= 0.9:
        return pd.to_datetime(
            values.apply(lambda v: f"{int(float(v))}-12-31" if _looks_like_year(v) else None),
            errors='coerce')

    if len(populated) > 0 and populated.astype(str).apply(
            lambda v: _parse_quarter(v) is not None).mean() >= 0.9:
        return pd.to_datetime(values.apply(_parse_quarter), errors='coerce')

    # A column of plain numbers that are not years is not a date column.
    # pandas would read 90000 as nanoseconds since the epoch and "parse"
    # every row into 1970 — which made a sales column outscore the real date
    # column in the auto-detection below. Plausible years already returned above.
    if len(populated) > 0 and not pd.api.types.is_datetime64_any_dtype(values):
        numeric = pd.to_numeric(populated, errors='coerce')
        if numeric.notna().mean() >= 0.9:
            return pd.Series(pd.NaT, index=values.index, dtype='datetime64[ns]')

    return pd.to_datetime(values, errors='coerce')


def _date_score(series: pd.Series) -> float:
    """Fraction of a column's values that parse as dates."""
    try:
        return float(normalize_dates(series).notna().mean())
    except Exception:
        return 0.0


def guess_date_column(df: pd.DataFrame) -> Optional[str]:
    """Pick the likeliest date column: name hints first, then best-parsing."""
    hints = ['date', 'month', 'period', 'time', 'day', 'week',
             'quarter', 'year', 'ds', 'timestamp', 'dt']
    for col in df.columns:
        if any(h in str(col).strip().lower() for h in hints) and _date_score(df[col]) >= 0.6:
            return col
    best_col, best_score = None, 0.0
    for col in df.columns:
        score = _date_score(df[col])
        if score > best_score:
            best_col, best_score = col, score
    return best_col if best_score >= 0.6 else None


def guess_value_column(df: pd.DataFrame, date_col) -> Optional[str]:
    """Pick the likeliest sales column: name hints first, then most-numeric."""
    hints = ['sales', 'revenue', 'value', 'amount', 'demand', 'units',
             'volume', 'qty', 'quantity', 'y', 'target', 'total', 'count']
    candidates = [c for c in df.columns if c != date_col]
    for col in candidates:
        name = str(col).strip().lower()
        if any(h == name or h in name for h in hints):
            if pd.to_numeric(df[col], errors='coerce').notna().mean() >= 0.6:
                return col
    best_col, best_score = None, 0.0
    for col in candidates:
        score = pd.to_numeric(df[col], errors='coerce').notna().mean()
        if score > best_score:
            best_col, best_score = col, score
    return best_col if best_score >= 0.6 else None


def get_covid_mask(index: pd.DatetimeIndex, rule: str):
    """COVID dummy at the resolution of the caller's data."""
    if rule == 'weekly':
        return ((index >= pd.Timestamp('2020-03-01'))
                & (index <= pd.Timestamp('2020-06-30'))).astype(float)
    if rule == 'monthly':
        return index.isin(pd.date_range('2020-03-31', '2020-06-30', freq='ME')).astype(float)
    if rule == 'quarterly':
        return index.isin(pd.date_range('2020-03-31', '2020-09-30', freq='QE')).astype(float)
    if rule == 'annual':
        return (index.year == 2020).astype(float)
    return pd.Series(0.0, index=index)


def resample_fred_to_freq(monthly_series: pd.Series, target_index: pd.DatetimeIndex,
                          method: Optional[str]) -> pd.Series:
    """Bring a monthly FRED series onto the target index."""
    if method is None:
        return monthly_series.reindex(target_index)
    if method == 'ffill':
        combined = monthly_series.reindex(
            monthly_series.index.union(target_index)).ffill()
        return combined.reindex(target_index)
    return monthly_series.resample(target_index.freq).mean().reindex(target_index)


# ═══════════════════════════════════════════════════════════════════
#  LOADING
# ═══════════════════════════════════════════════════════════════════

@dataclass
class LoadedSeries:
    """A cleaned single-metric time series ready for modelling."""
    frame: pd.DataFrame              # index=Date, one column ('Sales')
    duplicates_merged: int = 0
    dropped_rows: int = 0


def read_workbook(source) -> pd.DataFrame:
    """Read an uploaded .xlsx/.xls/.csv into a frame with trimmed column names."""
    name = getattr(source, 'name', '') or ''
    if str(name).lower().endswith('.csv'):
        df = pd.read_csv(source)
    else:
        df = pd.read_excel(source)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_series(df: pd.DataFrame, date_col, value_col, freq_name: str) -> LoadedSeries:
    """Standardise a two-column selection into a dated, gap-free Sales series."""
    config = FREQ_CONFIGS[freq_name]
    frame = df[[date_col, value_col]].rename(
        columns={date_col: 'Date', value_col: TARGET_COL})
    frame[TARGET_COL] = pd.to_numeric(frame[TARGET_COL], errors='coerce')
    frame['Date'] = normalize_dates(frame['Date'])

    before = len(frame)
    frame = frame.dropna(subset=['Date'])
    dropped = before - len(frame)

    # Repeated periods break the reindexing every model below relies on.
    duplicates = int(frame['Date'].duplicated().sum())
    if duplicates:
        frame = frame.groupby('Date', as_index=False)[TARGET_COL].sum()

    frame = frame.set_index('Date').sort_index()
    frame.index = pd.to_datetime(frame.index)
    try:
        frame.index.freq = config['freq']
    except Exception:
        frame = frame.asfreq(config['freq'])
    return LoadedSeries(frame=frame, duplicates_merged=duplicates, dropped_rows=dropped)


# ═══════════════════════════════════════════════════════════════════
#  FRED
# ═══════════════════════════════════════════════════════════════════

class FredKeyError(RuntimeError):
    """FRED rejected the API key — every variable will fail the same way."""


def fetch_fred_series(api_key: str, series_id: str, timeout: int = 20,
                     session: Optional[requests.Session] = None) -> pd.Series:
    """Pull one FRED series as a month-end indexed Series.

    Pass a session to reuse connections: 32 fresh TLS handshakes cost far more
    than the responses themselves (measured 9.8s vs 3.7s for the full panel).
    """
    getter = session or requests
    response = getter.get(
        f"{FRED_BASE_URL}?series_id={series_id}&api_key={api_key}&file_type=json",
        timeout=timeout)
    payload = response.json()
    if 'observations' not in payload:
        # FRED answers a bad key with a 400 and an error_message, not an
        # exception — surface it so the page can say "check your key".
        message = payload.get('error_message', 'unexpected FRED response')
        if response.status_code in (400, 403) and 'api_key' in str(message):
            raise FredKeyError(message)
        raise RuntimeError(message)
    observations = pd.DataFrame(payload['observations'])[['date', 'value']]
    observations['value'] = pd.to_numeric(observations['value'], errors='coerce')
    observations['date'] = pd.to_datetime(observations['date']) + MonthEnd(1)
    observations = observations.set_index('date')
    observations.index.freq = 'ME'
    return observations['value']


def fetch_fred_monthly(api_key: str,
                       progress: Optional[Callable[[int, int, str], None]] = None
                       ) -> Tuple[Dict[str, pd.Series], Dict[str, str]]:
    """Pull every FRED variable as raw monthly series.

    Frequency-independent on purpose: this is the expensive step (32 HTTPS
    round-trips) and the natural thing for the page to cache. Requests run
    concurrently — served serially the pull takes ~90s, threaded ~6s.

    Returns (monthly, status) keyed by human-readable variable name. A
    variable that fails is recorded in `status` and left out of `monthly`;
    one bad series must not sink the whole run.
    """
    cached = read_fred_cache(api_key)
    if cached is not None:
        return cached

    started = perf_counter()
    monthly: Dict[str, pd.Series] = {}
    status: Dict[str, str] = {}
    total = len(FRED_VARS)

    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=FRED_WORKERS, pool_maxsize=FRED_WORKERS, max_retries=1))

    def pull(item: Tuple[str, str]) -> Tuple[str, Optional[pd.Series], str]:
        series_id, label = item
        try:
            return label, fetch_fred_series(api_key, series_id, session=session), 'ok'
        except Exception as exc:
            log_structured_error(exc, page="market_size_forecasting",
                                 component="market_size_forecast_service.fetch_fred_monthly",
                                 operation="fetch_fred_series", context=series_id)
            return label, None, f'failed: {exc}'

    try:
        with ThreadPoolExecutor(max_workers=FRED_WORKERS) as pool:
            for done, (label, series, outcome) in enumerate(
                    pool.map(pull, FRED_VARS.items()), start=1):
                status[label] = outcome
                if series is not None:
                    monthly[label] = series
                if progress:
                    progress(done, total, label)
    finally:
        session.close()

    log_timing("MSF_FRED_PULL", (perf_counter() - started) * 1000,
               details=f"ok={len(monthly)}/{total}")
    write_fred_cache(api_key, monthly, status)
    return monthly, status


# ── Disk cache ────────────────────────────────────────────────────
# The FRED panel is identical for every user and changes at most monthly, but
# an in-process cache dies on every restart — and STG deploys several times a
# day, so the first user after each deploy paid the full pull. Persist it the
# way the repo already persists the EDGAR caches: under /home, which survives
# both restarts and deploys.

FRED_DISK_TTL_HOURS = 24
RESULT_DISK_TTL_HOURS = 168      # a week; a fitted model does not go stale
MAX_CACHED_RESULTS = 200


def fred_cache_path() -> Optional[Path]:
    """Where the FRED panel is cached, or None if nowhere is writable."""
    directory = _cache_dir()
    return None if directory is None else directory / "fred_monthly.pkl"


def _cache_dir() -> Optional[Path]:
    """Writable cache directory, or None if there is nowhere to write."""
    configured = os.getenv("MSF_CACHE_DIR", "").strip()
    candidates = [Path(configured)] if configured else [
        Path("/home/msf_cache"),                     # Azure App Service share
        Path(tempfile.gettempdir()) / "msf_cache",   # dev laptops
    ]
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        except Exception:
            continue
    return None


def series_fingerprint(series: pd.Series) -> str:
    """Stable identity for a cleaned series — values and periods."""
    payload = (np.ascontiguousarray(series.index.astype("int64").to_numpy()).tobytes()
               + np.ascontiguousarray(series.to_numpy(), dtype="float64").tobytes())
    return hashlib.sha256(payload).hexdigest()[:24]


def disk_cache_get(namespace: str, key: str, ttl_hours: float = RESULT_DISK_TTL_HOURS):
    """Read a cached result, or None. Never raises."""
    directory = _cache_dir()
    if directory is None:
        return None
    path = directory / f"{namespace}-{key}.pkl"
    try:
        if not path.exists():
            return None
        if (time.time() - path.stat().st_mtime) / 3600 > ttl_hours:
            return None
        with path.open("rb") as handle:
            value = pickle.load(handle)
        path.touch()                       # keep hot entries from being evicted
        log_timing(f"MSF_DISK_HIT_{namespace.upper()}", 0.0, details=key)
        return value
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecast_service.disk_cache_get",
                             operation="read", context=namespace)
        return None


def disk_cache_put(namespace: str, key: str, value) -> None:
    """Persist a result. A cache failure must never break a run."""
    directory = _cache_dir()
    if directory is None:
        return
    path = directory / f"{namespace}-{key}.pkl"
    try:
        scratch = path.with_suffix(".tmp")
        with scratch.open("wb") as handle:
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        scratch.replace(path)              # atomic: readers never see a partial file
        _evict_oldest(directory)
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecast_service.disk_cache_put",
                             operation="write", context=namespace)


def _evict_oldest(directory: Path, keep: Optional[int] = None) -> None:
    """Bound the cache so a busy week cannot fill the share.

    The cap is read at call time, not bound as a default — a default argument
    is evaluated once at import and could never be changed afterwards.
    """
    keep = MAX_CACHED_RESULTS if keep is None else keep
    try:
        entries = sorted((p for p in directory.glob("*.pkl") if not p.name.startswith("fred_")),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in entries[keep:]:
            stale.unlink(missing_ok=True)
    except Exception:
        pass


def read_fred_cache(api_key: str) -> Optional[Tuple[Dict[str, pd.Series], Dict[str, str]]]:
    """Return the cached panel if it is fresh and was pulled with this key."""
    path = fred_cache_path()
    if path is None or not path.exists():
        return None
    try:
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > FRED_DISK_TTL_HOURS:
            return None
        with path.open("rb") as handle:
            payload = pickle.load(handle)
        # A different key may have different entitlements; don't serve across keys.
        if payload.get("key_fingerprint") != _key_fingerprint(api_key):
            return None
        monthly = payload["monthly"]
        if not monthly:
            return None
        log_timing("MSF_FRED_DISK_HIT", 0.0,
                   details=f"vars={len(monthly)} age={age_hours:.1f}h")
        return monthly, payload["status"]
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecast_service.read_fred_cache",
                             operation="read_cache")
        return None


def write_fred_cache(api_key: str, monthly: Dict[str, pd.Series],
                     status: Dict[str, str]) -> None:
    """Persist the panel. A cache failure must never break a run."""
    path = fred_cache_path()
    if path is None or not monthly:
        return
    try:
        scratch = path.with_suffix(".tmp")
        with scratch.open("wb") as handle:
            pickle.dump({"key_fingerprint": _key_fingerprint(api_key),
                         "monthly": monthly, "status": status}, handle,
                        protocol=pickle.HIGHEST_PROTOCOL)
        scratch.replace(path)          # atomic: readers never see a partial file
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecast_service.write_fred_cache",
                             operation="write_cache")


def _key_fingerprint(api_key: str) -> str:
    """Identify the key without ever writing it to disk."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def align_fred(monthly: Dict[str, pd.Series], target_index: pd.DatetimeIndex,
               resample: Optional[str]) -> Dict[str, pd.Series]:
    """Put the monthly FRED panel onto the caller's frequency."""
    return {label: resample_fred_to_freq(series, target_index, resample)
            for label, series in monthly.items()}


def build_model_frame(sales: pd.DataFrame, freq_name: str,
                      fred_aligned: Optional[Dict[str, pd.Series]] = None,
                      end_date=None) -> pd.DataFrame:
    """Sales + FRED exog + COVID dummy on one continuous index."""
    config = FREQ_CONFIGS[freq_name]
    end_date = end_date or pd.Timestamp.today().date()
    frame = pd.DataFrame(index=pd.date_range(sales.index[0], end_date, freq=config['freq']))
    frame = frame.join(sales)
    for label, series in (fred_aligned or {}).items():
        frame[label] = series
    frame = frame.fillna(frame.mean())
    frame[TARGET_COL] = sales[TARGET_COL].reindex(frame.index)
    frame['covid_shock'] = get_covid_mask(frame.index, config['covid_rule'])
    return frame


# ═══════════════════════════════════════════════════════════════════
#  DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════════

def data_diagnostics(sales_series: pd.Series, freq_name: str,
                     season_mode: str = 'auto-detect') -> Dict[str, Any]:
    """Stationarity, seasonality, outliers — everything the Diagnostics tab shows."""
    config = FREQ_CONFIGS[freq_name]
    period_avg = sales_series.groupby(sales_series.index.month).mean()
    seasonal_strength = (period_avg.max() - period_avg.min()) / sales_series.mean() * 100

    if season_mode == 'auto-detect':
        seasonality_mode = 'multiplicative' if seasonal_strength > 20 else 'additive'
    else:
        seasonality_mode = season_mode

    adf_p = kpss_p = float('nan')
    try:
        adf_p = float(adfuller(sales_series, autolag='AIC')[1])
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecast_service.data_diagnostics",
                             operation="adfuller")
    try:
        kpss_p = float(kpss(sales_series, regression='c', nlags='auto')[1])
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="market_size_forecast_service.data_diagnostics",
                             operation="kpss")

    z_scores = np.abs(stats.zscore(sales_series))
    outliers = sales_series[z_scores > 3]

    decomposition = None
    if config['decomp_period'] and len(sales_series) > config['decomp_period'] * 2:
        try:
            decomposition = seasonal_decompose(sales_series, model='additive',
                                               period=config['decomp_period'])
        except Exception as exc:
            log_structured_error(exc, page="market_size_forecasting",
                                 component="market_size_forecast_service.data_diagnostics",
                                 operation="seasonal_decompose")

    return {
        'period_avg': period_avg,
        'seasonal_strength': float(seasonal_strength),
        'seasonality_mode': seasonality_mode,
        'adf_p': adf_p,
        'adf_stationary': bool(adf_p <= 0.05),
        'kpss_p': kpss_p,
        'kpss_stationary': bool(kpss_p > 0.05),
        'outliers': outliers,
        'decomposition': decomposition,
        'total_records': int(len(sales_series)),
        'mean_sales': float(sales_series.mean()),
        'std_sales': float(sales_series.std()),
        'min_sales': float(sales_series.min()),
        'max_sales': float(sales_series.max()),
        'date_start': sales_series.index[0],
        'date_end': sales_series.index[-1],
    }


def residual_diagnostics(resid) -> Dict[str, Any]:
    """Ljung-Box / Shapiro-Wilk / Durbin-Watson, with lags capped to what fits.

    Short series get the largest lags they can support rather than an
    exception, so a 20-point annual dataset still reports something usable.
    """
    values = pd.Series(resid).dropna()
    count = len(values)
    if count < 4:
        return {'n': count, 'too_short': True, 'lags': [],
                'lb_short': float('nan'), 'lb_long': float('nan'),
                'shapiro_p': float('nan'), 'durbin_watson': float('nan'),
                'failures': []}

    short_lag = min(10, max(1, count // 2))
    long_lag = min(20, max(short_lag, count - 2))
    lags = sorted({short_lag, long_lag})

    try:
        table = acorr_ljungbox(values, lags=lags, return_df=True)
        lb_short = float(table['lb_pvalue'].iloc[0])
        lb_long = float(table['lb_pvalue'].iloc[-1])
    except Exception:
        lb_short = lb_long = float('nan')
    try:
        shapiro_p = float(stats.shapiro(values)[1]) if count >= 3 else float('nan')
    except Exception:
        shapiro_p = float('nan')
    try:
        dw = float(durbin_watson(values))
    except Exception:
        dw = float('nan')

    def failed(value, ok) -> bool:
        return not (value is None or (isinstance(value, float) and np.isnan(value))) and not ok(value)

    failures = []
    if failed(lb_short, lambda v: v > 0.05):
        failures.append('lb_short')
    if failed(lb_long, lambda v: v > 0.05):
        failures.append('lb_long')
    if failed(shapiro_p, lambda v: v > 0.05):
        failures.append('normality')
    if failed(dw, lambda v: 1.5 < v < 2.5):
        failures.append('dw')

    return {'n': count, 'too_short': False, 'lags': lags,
            'lb_short': lb_short, 'lb_long': lb_long,
            'shapiro_p': shapiro_p, 'durbin_watson': dw,
            'failures': failures}


def _accuracy(actual: np.ndarray, predicted: np.ndarray) -> Tuple[float, float]:
    """MAPE (%) and RMSE for a held-out window."""
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    mape = float(np.mean(np.abs((actual - predicted) / actual)) * 100)
    rmse = float(np.sqrt(mean_squared_error(actual, predicted)))
    return mape, rmse


def validation_window(horizon: int, freq_name: str, sample_size: int) -> int:
    """How many recent periods to hold out.

    Capped independently of the forecast horizon: holding out 60 months to
    test a 5-year forecast would gut the training set.
    """
    config = FREQ_CONFIGS[freq_name]
    return max(1, min(horizon, max(config['s'], 12), sample_size // 3))


# ═══════════════════════════════════════════════════════════════════
#  SARIMA
# ═══════════════════════════════════════════════════════════════════

def _seasonal_order(order: Tuple[int, int, int], season: int) -> Tuple[int, int, int, int]:
    """Seasonal order for `season`, or a disabled one when there is no season.

    statsmodels rejects a seasonal period of 1, which is what the Annual
    config carries, so annual runs fall back to a plain ARIMA.
    """
    if season < 2:
        return (0, 0, 0, 0)
    return (*order, season)


# A seasonal AR or MA term at lag 52 is estimated from one observation per
# annual cycle, so it needs many years of weekly history to identify at all —
# and each such fit costs ~3.6s against ~40ms without them. On a 288-week
# series (5.5 cycles) the 36-model grid took 50s locally, several minutes on
# STG, which is what "takes forever" was.
#
# Below the threshold we keep seasonal DIFFERENCING (D=1, which is what
# actually removes the annual pattern) and stop searching seasonal AR/MA.
# Monthly and quarterly are never affected: their seasons are short, their fits
# are cheap, and `LARGE_SEASON` keeps them on the original exhaustive grid so
# the numbers the research team validated do not move.
MIN_CYCLES_FOR_SEASONAL_TERMS = 8
LARGE_SEASON = 26


def seasonal_terms_supported(sample_size: int, season: int) -> bool:
    """Can this series identify seasonal AR/MA terms at `season`?"""
    if season < 2:
        return False
    if season < LARGE_SEASON:
        return True                      # monthly (12), quarterly (4): always search
    return sample_size >= MIN_CYCLES_FOR_SEASONAL_TERMS * season


def run_sarima(sales_series: pd.Series, freq_name: str, horizon: int,
               progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, Any]:
    """Grid-search the ARIMA order by AIC, validate, then forecast."""
    started = perf_counter()
    config = FREQ_CONFIGS[freq_name]
    season = config['s']

    window = validation_window(horizon, freq_name, len(sales_series))
    train, test = sales_series[:-window], sales_series[-window:]

    # PORT: the original stored the seasonal order with a hardcoded period of
    # 12 while fitting the grid at the data's real period, so quarterly and
    # weekly runs were refit against a season they never searched.
    p_range, d_range, q_range = (0, 2), (1, 1), (0, 2)
    seasonal_search = seasonal_terms_supported(len(train), season)
    P_range, D_range, Q_range = ((0, 1) if seasonal_search else (0, 0)), (1, 1), \
                                ((0, 1) if seasonal_search else (0, 0))
    combos = [(p, d, q, P, D, Q)
              for p in range(p_range[0], p_range[1] + 1)
              for d in range(d_range[0], d_range[1] + 1)
              for q in range(q_range[0], q_range[1] + 1)
              for P in range(P_range[0], P_range[1] + 1)
              for D in range(D_range[0], D_range[1] + 1)
              for Q in range(Q_range[0], Q_range[1] + 1)]

    best_aic = np.inf
    best_order = (1, 1, 1)
    best_seasonal = _seasonal_order((0, 1, 1), season)
    fit = None
    for position, (p, d, q, P, D, Q) in enumerate(combos, start=1):
        seasonal = _seasonal_order((P, D, Q), season)
        try:
            # cov_type='none' skips the Hessian: the grid ranks on AIC, which
            # does not use standard errors. The winner is refitted normally
            # below so the model summary still reports them.
            candidate = SARIMAX(train, order=(p, d, q), seasonal_order=seasonal,
                                validate_specification=False).fit(disp=False, cov_type='none')
            if candidate.aic < best_aic:
                best_aic, best_order, best_seasonal = candidate.aic, (p, d, q), seasonal
        except Exception:
            pass
        if progress:
            progress(position, len(combos), f"ARIMA({p},{d},{q})x({P},{D},{Q},{season})")

    fit = SARIMAX(train, order=best_order, seasonal_order=best_seasonal).fit(disp=False)
    predicted = fit.forecast(steps=window)
    mape, rmse = _accuracy(test.values, predicted.values)
    walkforward = fit.get_forecast(steps=window).summary_frame(alpha=0.05)
    walkforward.index = test.index

    full = SARIMAX(sales_series, order=best_order, seasonal_order=best_seasonal).fit(disp=False)
    forecast = full.get_forecast(steps=horizon).summary_frame(alpha=0.05)
    forecast.index = pd.date_range(sales_series.index[-1] + _offset(freq_name),
                                   periods=horizon, freq=config['freq'])

    log_timing("MSF_SARIMA", (perf_counter() - started) * 1000,
               details=f"order={best_order}{best_seasonal} fits={len(combos)} "
                       f"seasonal_search={seasonal_search} mape={mape:.2f}")
    return {
        'model': 'SARIMA',
        'seasonal_search': seasonal_search,
        'cycles': (len(train) / season) if season >= 2 else None,
        'label': f"SARIMA{best_order}x{best_seasonal}",
        'order': best_order,
        'seasonal_order': best_seasonal,
        'best_aic': float(best_aic) if np.isfinite(best_aic) else None,
        'mape': mape, 'rmse': rmse,
        'walkforward': walkforward,
        'wf_actual': test, 'wf_predicted': predicted,
        'forecast': forecast,
        'residuals': fit.resid,
        'summary': str(fit.summary()),
        'history': sales_series,
    }


def _offset(freq_name: str):
    return pd.tseries.frequencies.to_offset(FREQ_CONFIGS[freq_name]['freq'])


# ═══════════════════════════════════════════════════════════════════
#  PROPHET
# ═══════════════════════════════════════════════════════════════════

def prophet_available() -> bool:
    try:
        import prophet  # noqa: F401
        return True
    except Exception:
        return False


def run_prophet(sales_series: pd.Series, freq_name: str, horizon: int,
                seasonality_mode: str, changepoint_prior: float,
                seasonality_prior: float, run_cross_validation: bool = True) -> Dict[str, Any]:
    """Fit Prophet with a COVID regressor, validate, then forecast."""
    from prophet import Prophet
    from prophet.diagnostics import cross_validation, performance_metrics

    started = perf_counter()
    config = FREQ_CONFIGS[freq_name]

    frame = sales_series.rename('y').rename_axis('ds').reset_index()
    frame['ds'] = pd.to_datetime(frame['ds'])
    frame['covid_shock'] = get_covid_mask(
        pd.DatetimeIndex(frame['ds']), config['covid_rule']).astype(float)

    def build(with_regressor: bool):
        model = Prophet(growth='linear', seasonality_mode=seasonality_mode,
                        changepoint_prior_scale=changepoint_prior,
                        seasonality_prior_scale=seasonality_prior,
                        yearly_seasonality=True, weekly_seasonality=False,
                        daily_seasonality=False)
        if with_regressor:
            model.add_regressor('covid_shock')
        return model

    model = build(True)
    model.fit(frame)
    baseline = build(False)
    baseline.fit(frame[['ds', 'y']])

    deltas = model.params['delta'].mean(axis=0)
    significant = np.where(np.abs(deltas) > 0.01)[0]
    changepoints = [
        {'date': model.changepoints.iloc[i].date(),
         'delta': float(deltas[i]),
         'direction': 'Upward' if deltas[i] > 0 else 'Downward'}
        for i in significant
    ]

    cv_mape = baseline_mape = float('nan')
    cv_table = None
    if run_cross_validation:
        try:
            # parallel="threads": Prophet's Stan backend releases the GIL, so the
            # dozen cutpoint fits overlap. Measured 2.2s -> 0.56s on 114 points.
            cv = cross_validation(model, initial=config['cv_initial'],
                                  period=config['cv_period'],
                                  horizon=config['cv_horizon'], parallel="threads")
            cv_table = performance_metrics(cv)
            cv_mape = float(cv_table['mape'].mean() * 100)
            cv_base = cross_validation(baseline, initial=config['cv_initial'],
                                       period=config['cv_period'],
                                       horizon=config['cv_horizon'], parallel="threads")
            baseline_mape = float(performance_metrics(cv_base)['mape'].mean() * 100)
        except Exception:
            cv_table = None   # too little history — walk-forward below still runs

    window = validation_window(horizon, freq_name, len(frame))
    train, test = frame[:-window].copy(), frame[-window:].copy().reset_index(drop=True)
    holdout = build(True)
    holdout.fit(train)
    predicted = holdout.predict(test[['ds', 'covid_shock']])
    predicted = predicted[predicted['ds'].isin(test['ds'].values)].reset_index(drop=True)
    aligned = min(len(test), len(predicted))
    mape, rmse = _accuracy(test['y'].values[:aligned], predicted['yhat'].values[:aligned])

    insample = model.predict(frame[['ds', 'covid_shock']])
    residuals = frame['y'].values - insample['yhat'].values

    future = pd.DataFrame({
        'ds': pd.date_range(sales_series.index[-1] + _offset(freq_name),
                            periods=horizon, freq=config['freq']),
        'covid_shock': 0.0,
    })
    # `model` is already fitted on the full frame with these exact settings —
    # the original refit an identical third model here.
    projection = model.predict(future)
    forecast = pd.DataFrame({
        'mean': projection['yhat'].values,
        'mean_ci_lower': projection['yhat_lower'].values,
        'mean_ci_upper': projection['yhat_upper'].values,
    }, index=pd.DatetimeIndex(projection['ds']))

    log_timing("MSF_PROPHET", (perf_counter() - started) * 1000,
               details=f"mode={seasonality_mode} mape={mape:.2f}")
    return {
        'model': 'Prophet',
        'label': f"Prophet ({seasonality_mode})",
        'seasonality_mode': seasonality_mode,
        'mape': mape, 'rmse': rmse,
        'wf_actual': test['y'].values[:aligned],
        'wf_predicted': predicted['yhat'].values[:aligned],
        'wf_dates': test['ds'].iloc[:aligned],
        'forecast': forecast,
        'residuals': residuals,
        'changepoints': changepoints,
        'total_changepoints': int(len(model.changepoints)),
        'cv_mape': cv_mape,
        'cv_baseline_mape': baseline_mape,
        'cv_table': cv_table,
        'components_model': model,
        'history': sales_series,
    }


# ═══════════════════════════════════════════════════════════════════
#  SARIMAX
# ═══════════════════════════════════════════════════════════════════

def _adf_pvalue(series) -> float:
    """ADF p-value, tolerant of short or constant series."""
    values = pd.Series(series).dropna()
    if len(values) < 6 or values.nunique() <= 1:
        return 1.0    # untestable — treat as non-stationary
    try:
        return float(adfuller(values, autolag='AIC')[1])
    except Exception:
        return 1.0


# Regular plus seasonal differencing tops out here in practice. The original
# loop had only a length guard, so on weekly data — where forward-filled FRED
# series are step functions ADF never accepts — it differenced 76 times and
# handed the Granger scan pure noise. Monthly data converges at 3, so the
# validated monthly path is unchanged by the cap.
MAX_DIFFERENCING_ROUNDS = 3


def difference_to_stationary(frame: pd.DataFrame, freq_name: str
                             ) -> Tuple[pd.DataFrame, int, bool]:
    """Difference until every column passes ADF.

    Stops early if the frame gets too short to model, or at
    MAX_DIFFERENCING_ROUNDS. Returns (frame, rounds, hit_cap).
    """
    config = FREQ_CONFIGS[freq_name]
    working = frame.dropna().copy()
    rounds = 0
    while rounds < MAX_DIFFERENCING_ROUNDS:
        stationary = [c for c in working.columns if _adf_pvalue(working[c]) <= 0.05]
        if set(stationary) == set(working.columns):
            return working, rounds, False
        if len(working) - 1 < max(config['s'] * 2, 12):
            return working, rounds, False
        working = working.diff().dropna()
        rounds += 1
    remaining = sum(1 for c in working.columns if _adf_pvalue(working[c]) > 0.05)
    return working, rounds, remaining > 0


def granger_scan(stationary: pd.DataFrame, max_lag: int) -> pd.DataFrame:
    """Strongest significant Granger lag per candidate variable."""
    rows = []
    for column in stationary.columns:
        if column == TARGET_COL:
            continue
        try:
            # PORT: statsmodels 0.15 removed the `verbose` argument the
            # original passed here; it printed a table nobody read.
            tests = grangercausalitytests(stationary[[TARGET_COL, column]], maxlag=max_lag)
            significant = [
                {'x': column, 'lag': lag,
                 'ftest_stat': tests[lag][0]['ssr_ftest'][0],
                 'ftest_pval': tests[lag][0]['ssr_ftest'][1],
                 'xlabel': f'{column} ({lag}m ago)'}
                for lag in range(1, max_lag + 1)
                if tests[lag][0]['ssr_ftest'][1] <= 0.05
            ]
            if significant:
                rows.append(max(significant, key=lambda r: r['ftest_stat']))
        except Exception:
            pass
    if not rows:
        return pd.DataFrame(columns=['x', 'lag', 'ftest_stat', 'ftest_pval', 'xlabel', 'rmse'])
    return pd.DataFrame(rows).reset_index(drop=True)


def build_lagged_frame(model_frame: pd.DataFrame, granger: pd.DataFrame,
                       last_actual, horizon: int, freq_name: str) -> pd.DataFrame:
    """Sales plus each Granger variable shifted forward by its winning lag."""
    config = FREQ_CONFIGS[freq_name]
    lagged = model_frame[[TARGET_COL]].dropna().copy()
    future_index = pd.date_range(last_actual + _offset(freq_name),
                                 periods=horizon + 1, freq=config['freq'])
    lagged = lagged.reindex(lagged.index.union(future_index))
    for _, row in granger.iterrows():
        # Shift on the dataset's own frequency; a hardcoded monthly shift
        # produced colliding dates on weekly and quarterly data.
        lagged[row['xlabel']] = model_frame[row['x']].shift(
            periods=int(row['lag']), freq=config['freq'])
    return lagged


def rank_exog_combinations(lagged: pd.DataFrame, candidates: List[str], max_lag: int,
                           max_combo: int, horizon: int, freq_name: str,
                           progress: Optional[Callable[[int, int, str], None]] = None
                           ) -> pd.DataFrame:
    """Fit every variable combination and rank them by held-out RMSE."""
    config = FREQ_CONFIGS[freq_name]
    trimmed = lagged[candidates + [TARGET_COL]].iloc[max_lag:].dropna()
    window = max(1, min(horizon, max(config['s'], 12), len(trimmed) // 3))
    train, test = trimmed[:-window], trimmed[-window:]

    all_combos = [list(c) for size in range(1, max_combo + 1)
                  for c in combinations(candidates, size)]
    rows = []
    # Same reasoning as the SARIMA grid: at lag 52 the seasonal AR/MA terms are
    # both unidentifiable and ~90x more expensive per fit.
    seasonal = _seasonal_order(
        (1, 1, 1) if seasonal_terms_supported(len(train), config['s']) else (0, 1, 0),
        config['s'])
    for position, combo in enumerate(all_combos, start=1):
        try:
            # Ranked on forecast RMSE, so standard errors are never read —
            # skipping the Hessian keeps the search lean. The chosen
            # combination is refitted in full by run_sarimax.
            fit = SARIMAX(train[TARGET_COL], exog=train[combo],
                          order=(1, 1, 1), seasonal_order=seasonal,
                          validate_specification=False).fit(disp=False, cov_type='none')
            predicted = fit.forecast(steps=window, exog=test[combo])
            mape, rmse = _accuracy(test[TARGET_COL].values, predicted)
            # Keep the orders, not the fitted model: the page holds this table
            # in session state and a list of fitted SARIMAX objects is dead
            # weight there. Every combo is fit at the same order anyway.
            rows.append({'rmse': rmse, 'mape': mape, 'predvars': combo,
                         'order': fit.model.order,
                         'seasonal_order': fit.model.seasonal_order})
        except Exception:
            pass
        if progress:
            progress(position, len(all_combos), ', '.join(combo))
    if not rows:
        return pd.DataFrame(columns=['rmse', 'mape', 'predvars', 'order', 'seasonal_order'])
    return pd.DataFrame(rows).sort_values('rmse').reset_index(drop=True)


def run_sarimax(lagged: pd.DataFrame, predvars: List[str], order, seasonal_order,
                last_actual, horizon: int, max_lag: int, freq_name: str) -> Dict[str, Any]:
    """Validate the chosen combination, then forecast the full horizon.

    Beyond the lag window the real FRED values run out, so remaining periods
    get each variable's historical mean: the exog terms contribute ~0 and the
    model falls back on its own trend and seasonality.
    """
    started = perf_counter()
    config = FREQ_CONFIGS[freq_name]
    predvars = list(predvars)

    clean = lagged[[TARGET_COL] + predvars].iloc[max_lag:].dropna()
    window = max(1, min(horizon, max(config['s'], 12), len(clean) // 3))
    fit = SARIMAX(clean[:-window][TARGET_COL], exog=clean[:-window][predvars],
                  order=order, seasonal_order=seasonal_order).fit(disp=False)
    predicted = fit.forecast(steps=window, exog=clean[-window:][predvars])
    mape, rmse = _accuracy(clean[-window:][TARGET_COL].values, predicted.values)
    walkforward = fit.get_forecast(
        steps=window, exog=clean[-window:][predvars]).summary_frame(alpha=0.05)
    walkforward.index = clean[-window:].index

    full_slice = lagged[[TARGET_COL] + predvars].iloc[max_lag:]
    actual = full_slice[full_slice.index <= last_actual].dropna()
    future = full_slice[full_slice.index > last_actual].dropna(subset=predvars)

    final = SARIMAX(actual[TARGET_COL], exog=actual[predvars],
                    order=order, seasonal_order=seasonal_order).fit(disp=0)

    future_index = pd.date_range(last_actual + _offset(freq_name),
                                 periods=horizon, freq=config['freq'])
    exog_future = pd.DataFrame(index=future_index, columns=predvars, dtype=float)
    real_rows = future.iloc[:horizon]
    if len(real_rows):
        exog_future.iloc[:len(real_rows)] = real_rows[predvars].values
    if len(real_rows) < horizon:
        exog_future.iloc[len(real_rows):] = actual[predvars].mean().values

    forecast = final.get_forecast(steps=horizon,
                                  exog=exog_future[predvars].values).summary_frame()
    forecast.index = future_index

    log_timing("MSF_SARIMAX", (perf_counter() - started) * 1000,
               details=f"vars={len(predvars)} mape={mape:.2f}")
    return {
        'model': 'SARIMAX',
        'label': f"SARIMAX{tuple(order)}x{tuple(seasonal_order)}",
        'order': tuple(order),
        'seasonal_order': tuple(seasonal_order),
        'predvars': predvars,
        'mape': mape, 'rmse': rmse,
        'walkforward': walkforward,
        'wf_actual': clean[-window:][TARGET_COL],
        'wf_predicted': predicted,
        'forecast': forecast,
        'residuals': fit.resid,
        'summary': str(fit.summary()),
        'real_exog_periods': int(min(len(real_rows), horizon)),
        'history': actual[TARGET_COL],
    }


def forecast_spread(results: Dict[str, Dict[str, Any]]) -> float:
    """Worst per-period disagreement between any two models, in percent.

    Measured against each period's own level — dividing every gap by the first
    period's value overstates divergence on a trending forecast.
    """
    series = [np.asarray(r['forecast']['mean'].values, dtype=float) for r in results.values()]
    if len(series) < 2:
        return 0.0
    shortest = min(len(s) for s in series)
    series = [s[:shortest] for s in series]
    worst = 0.0
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            midpoint = (series[i] + series[j]) / 2.0
            midpoint = np.where(midpoint == 0, np.nan, midpoint)
            gap = np.abs(series[i] - series[j]) / midpoint * 100
            if np.isfinite(gap).any():
                worst = max(worst, float(np.nanmax(gap)))
    return worst
