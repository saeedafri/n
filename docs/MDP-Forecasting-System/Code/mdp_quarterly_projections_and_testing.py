# -*- coding: utf-8 -*-
"""MDP Quarterly Projections and Testing.ipynb

"""

# Quarterly Projections
"""
CELL 1 -- MODEL: produces raw quarterly revenue projections
================================================================
Run this cell first. It loads the Excel file, computes seasonal
decomposition per company, applies structural-break and outlier
detection (with the ordering fix: outliers are checked before
structural breaks, so one anomalous quarter can't masquerade as a
permanent business change), fits all forecast methods, backtests them,
and builds the ensemble forecast.

OUTPUT OF THIS CELL: a variable called `projections` (a pandas DataFrame,
one row per company) containing the raw model output -- tier, confidence,
best method, backtest MAPE, the forecast itself, outlier quarters,
structural breaks, seasonal indices, and the walk-forward backtest
detail. Nothing in this cell decides what's "good" or "bad" -- that
judgment happens in Cell 2, which consumes `projections` directly.

Just set INPUT_FILE below and run this cell.
"""

import re
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

# ============================================================================
# CONFIG
# ============================================================================
STRUCTURAL_BREAK_THRESHOLD = 0.40
STRUCTURAL_BREAK_PERSIST_FACTOR = 0.6
OUTLIER_Z_THRESHOLD = 2.2              # tighter than annual's 1.5 -- quarterly series are longer,
                                        # so more points means more chances for a false positive
                                        # under the same threshold (multiple-testing effect)
OUTLIER_IQR_MULT = 2.0
MIN_TRAIN_FULL = 8                     # backtest min_train (quarters) for 16+ qtr (FULL) companies
IMPLAUSIBLE_GROWTH_CAGR = 0.60         # annualized
IMPLAUSIBLE_DECLINE_CAGR = -0.25       # annualized
TARGET_END_YEAR = 2031
TARGET_END_QUARTER = 4
REPORT_START_YEAR = 2026
REPORT_START_QUARTER = 1

INPUT_FILE = "Screening_Results_Quarterly.xlsx"
SHEET_NAME = 0
OUTPUT_FILE = "forecast_report_quarterly.xlsx"


# ============================================================================
# Quarter label parsing / indexing helpers
# ============================================================================
def _parse_quarter_label(val):
    s = str(val).strip()
    m = re.search(r'[Qq](\d)\D*(\d{4})', s)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r'(\d{4})\D*[Qq](\d)', s)
    if m:
        return int(m.group(1)), int(m.group(2))
    try:
        dt = pd.to_datetime(val, errors='raise')
        return dt.year, (dt.month - 1) // 3 + 1
    except Exception:
        pass
    try:
        yr = int(float(s))
        if 1900 < yr < 2200:
            return yr, 1
    except Exception:
        pass
    return None


def qidx(year, quarter):
    return year * 4 + quarter


def qidx_to_label(idx):
    quarter = idx % 4
    if quarter == 0:
        quarter = 4
        year = idx // 4 - 1
    else:
        year = idx // 4
    return f"Q{quarter} {year}"


TARGET_END_QIDX = qidx(TARGET_END_YEAR, TARGET_END_QUARTER)
REPORT_START_QIDX = qidx(REPORT_START_YEAR, REPORT_START_QUARTER)


# ============================================================================
# Data loading (wide: company x quarter columns)
# ============================================================================
def _find_header_row(path, sheet_name=0, max_scan=10):
    """Some exports (e.g. Coresight) have a title/summary row before the
    real header. Scan the first few rows for one that looks like a header
    -- i.e. the first cell reads like 'Company' and later cells contain
    parseable quarter labels."""
    preview = pd.read_excel(path, sheet_name=sheet_name, header=None, nrows=max_scan)
    for i in range(len(preview)):
        row = preview.iloc[i]
        first_cell = str(row.iloc[0]).strip().lower()
        looks_like_header = first_cell in ("company", "company name", "company/ticker") or (
            "compan" in first_cell and len(first_cell) < 20  # short label, not a sentence
        )
        if looks_like_header:
            quarter_hits = sum(1 for v in row.iloc[1:] if _parse_quarter_label(v) is not None)
            if quarter_hits >= 1:
                return i
    return 0  # fall back to assuming header is row 0


def load_long(path, sheet_name=0):
    header_row = _find_header_row(path, sheet_name=sheet_name)
    df = pd.read_excel(path, sheet_name=sheet_name, header=header_row)
    df = df.dropna(how="all")
    df = df.drop_duplicates(subset=[df.columns[0]]).reset_index(drop=True)
    company_col = df.columns[0]

    quarter_cols = {}
    for c in df.columns[1:]:
        parsed = _parse_quarter_label(c)
        if parsed:
            year, quarter = parsed
            quarter_cols[c] = qidx(year, quarter)
    if not quarter_cols:
        raise ValueError("Could not find any quarter columns in the input file.")

    rows = []
    for _, row in df.iterrows():
        company = row[company_col]
        for c, qi in quarter_cols.items():
            v = row[c]
            if pd.isna(v):
                continue
            if isinstance(v, str):
                v = v.replace(",", "").strip()
                if v in ("", "-", "NA", "N/A"):
                    continue
            try:
                v = float(v)
            except ValueError:
                continue
            rows.append({"company": company, "qidx": qi, "sales": v})
    long_df = pd.DataFrame(rows)
    if len(long_df):
        long_df["quarter"] = long_df["qidx"] % 4
        long_df["quarter"] = long_df["quarter"].replace(0, 4)
    return long_df, df[company_col].tolist()


# ============================================================================
# Seasonal decomposition
# ============================================================================
def compute_seasonal_indices(qi_arr, quarter_arr, sales_arr):
    """Ratio-to-moving-average (8+ points) or ratio-to-trend (4-7 points)."""
    quarters_present = np.unique(quarter_arr)
    if len(quarters_present) < 2:
        return np.ones(4)

    if len(sales_arr) >= 8:
        ma = np.convolve(sales_arr, np.ones(4) / 4, mode='same')
        ratios = [[] for _ in range(4)]
        for i in range(2, len(sales_arr) - 2):
            q = quarter_arr[i] - 1
            if ma[i] > 0:
                ratios[q].append(sales_arr[i] / ma[i])
        indices = np.array([np.mean(r) if r else 1.0 for r in ratios])
    else:
        qf = qi_arr.astype(float)
        slope, intercept, *_ = stats.linregress(qf, sales_arr)
        trend = intercept + slope * qf
        trend = np.where(trend <= 0, sales_arr.mean(), trend)  # guard against non-positive trend
        detrended = sales_arr / trend
        q_mean = {}
        for i, q in enumerate(quarter_arr):
            q_mean.setdefault(q - 1, []).append(detrended[i])
        indices = np.ones(4)
        for q, vals in q_mean.items():
            indices[q] = np.mean(vals)

    indices = indices / indices.mean()
    return indices


def deseasonalize(sales, quarters, seasonal_indices):
    return sales / seasonal_indices[quarters - 1]


def reseasonalize(values, quarters, seasonal_indices):
    return values * seasonal_indices[quarters - 1]


# ============================================================================
# Structural break + outlier detection (on DESEASONALIZED series)
# ============================================================================
def detect_structural_break(qi_arr, ds_sales, threshold=STRUCTURAL_BREAK_THRESHOLD):
    if len(ds_sales) < 6:
        return None
    breaks = []
    for i in range(1, len(ds_sales)):
        if ds_sales[i - 1] <= 0:
            continue
        chg = ds_sales[i] / ds_sales[i - 1] - 1
        if chg < -threshold:
            breaks.append((qi_arr[i], chg))
    if not breaks:
        return None
    break_qidx, _ = breaks[-1]
    idx = list(qi_arr).index(break_qidx)
    if idx >= len(ds_sales) - 2:  # need at least a couple points post-break to confirm persistence
        return None
    pre_level = ds_sales[idx - 1]
    post_level = np.mean(ds_sales[idx:])
    if (pre_level - post_level) / pre_level > threshold * STRUCTURAL_BREAK_PERSIST_FACTOR:
        return break_qidx
    return None


def detect_outliers(ds_sales, qi_arr, z_threshold=OUTLIER_Z_THRESHOLD, iqr_mult=OUTLIER_IQR_MULT):
    if len(ds_sales) < 5:
        return []
    rates = np.diff(ds_sales) / ds_sales[:-1]
    if len(rates) < 3:
        return []
    mean_g, std_g = rates.mean(), rates.std()
    q1, q3 = np.percentile(rates, 25), np.percentile(rates, 75)
    iqr = q3 - q1
    flags = set()
    if std_g > 0:
        z = (rates - mean_g) / std_g
        for i, zi in enumerate(z):
            if abs(zi) > z_threshold:
                flags.add(qi_arr[i + 1])
    if iqr > 0:
        lower, upper = q1 - iqr_mult * iqr, q3 + iqr_mult * iqr
        for i, r in enumerate(rates):
            if r < lower or r > upper:
                flags.add(qi_arr[i + 1])
    return sorted(flags)


# ============================================================================
# Forecast methods -- all operate on DESEASONALIZED data, caller reseasonalizes
# ============================================================================
def f_linear(qi, ds, periods):
    slope, intercept, *_ = stats.linregress(qi, ds)
    fq = np.arange(qi[-1] + 1, qi[-1] + 1 + periods)
    return intercept + slope * fq, fq

def f_cagr(qi, ds, periods):
    n = len(ds) - 1
    g = (ds[-1] / ds[0]) ** (1 / n) - 1 if n > 0 and ds[0] > 0 else 0
    fq = np.arange(qi[-1] + 1, qi[-1] + 1 + periods)
    return np.array([ds[-1] * (1 + g) ** i for i in range(1, periods + 1)]), fq

def f_exp_smoothing(qi, ds, periods, alpha=0.3):
    sm = [ds[0]]
    for v in ds[1:]:
        sm.append(alpha * v + (1 - alpha) * sm[-1])
    trend = sm[-1] - sm[-2] if len(sm) > 1 else 0
    fq = np.arange(qi[-1] + 1, qi[-1] + 1 + periods)
    return np.array([sm[-1] + trend * i for i in range(1, periods + 1)]), fq

def f_holt(qi, ds, periods, alpha=0.3, beta=0.1):
    level, trend = ds[0], (ds[1] - ds[0] if len(ds) > 1 else 0)
    for v in ds[1:]:
        prev = level
        level = alpha * v + (1 - alpha) * (level + trend)
        trend = beta * (level - prev) + (1 - beta) * trend
    fq = np.arange(qi[-1] + 1, qi[-1] + 1 + periods)
    return np.array([level + trend * i for i in range(1, periods + 1)]), fq

def f_ma_trend(qi, ds, periods, window=4):
    gr = np.diff(ds) / ds[:-1]
    avg_g = (gr[-window:].mean() if len(gr) >= window else gr.mean()) if len(gr) else 0
    fq = np.arange(qi[-1] + 1, qi[-1] + 1 + periods)
    out, last = [], ds[-1]
    for _ in range(periods):
        last *= (1 + avg_g)
        out.append(last)
    return np.array(out), fq

def f_weighted_avg(qi, ds, periods):
    gr = np.diff(ds) / ds[:-1]
    if len(gr) == 0:
        wg = 0
    else:
        w = np.arange(1, len(gr) + 1)
        wg = np.average(gr, weights=w)
    fq = np.arange(qi[-1] + 1, qi[-1] + 1 + periods)
    out, last = [], ds[-1]
    for _ in range(periods):
        last *= (1 + wg)
        out.append(last)
    return np.array(out), fq

DESEASONALIZED_METHODS = {
    "linear": f_linear, "cagr": f_cagr, "exp_smoothing": f_exp_smoothing,
    "holt": f_holt, "ma_trend": f_ma_trend, "weighted_avg": f_weighted_avg,
}
GROWTH_ONLY_METHODS = {"cagr": f_cagr, "ma_trend": f_ma_trend, "weighted_avg": f_weighted_avg}


def f_seasonal_naive(qi, sales, quarters, periods):
    """Same-quarter-last-year x average YoY growth. Operates on RAW sales
    (not deseasonalized -- it IS the seasonality model)."""
    fq = np.arange(qi[-1] + 1, qi[-1] + 1 + periods)
    fquarters = np.array([(q % 4) if (q % 4) != 0 else 4 for q in fq])

    yoy = []
    for i in range(4, len(sales)):
        if sales[i - 4] > 0:
            yoy.append(sales[i] / sales[i - 4] - 1)
    avg_yoy = np.mean(yoy) if yoy else 0.0

    out = []
    for i, fq_q in enumerate(fquarters):
        mask = quarters == fq_q
        if mask.any():
            base = sales[mask][-1]
            years_ahead = (i // 4) + 1
            out.append(base * (1 + avg_yoy) ** years_ahead)
        else:
            out.append(sales[-1])
    return np.array(out), fq


# ============================================================================
# Rolling-origin backtest (on DESEASONALIZED for the 6 trend methods,
# raw for seasonal_naive)
# ============================================================================
def rolling_backtest(qi_arr, ds_arr, raw_sales, quarters_arr, min_train):
    n = len(ds_arr)
    errors = {m: [] for m in list(DESEASONALIZED_METHODS.keys()) + ["seasonal_naive"]}
    detail = {m: [] for m in errors}
    seasonal_idx = compute_seasonal_indices(qi_arr, quarters_arr, raw_sales)  # fixed indices for backtest speed

    for split in range(min_train, n):
        tqi, tds = qi_arr[:split], ds_arr[:split]
        t_raw, t_q = raw_sales[:split], quarters_arr[:split]
        actual_qidx = qi_arr[split]
        actual = raw_sales[split]

        for name, fn in DESEASONALIZED_METHODS.items():
            try:
                pred_ds, _ = fn(tqi, tds, 1)
                pred = reseasonalize(pred_ds, np.array([quarters_arr[split]]), seasonal_idx)[0]
                err = abs(pred - actual) / actual * 100
                errors[name].append(err)
                detail[name].append({
                    "predicting_quarter": qidx_to_label(int(actual_qidx)),
                    "trained_through": qidx_to_label(int(tqi[-1])),
                    "predicted": round(float(pred), 2), "actual": round(float(actual), 2),
                    "error_pct": round(err, 1),
                })
            except Exception:
                pass

        try:
            pred, _ = f_seasonal_naive(tqi, t_raw, t_q, 1)
            pred = pred[0]
            err = abs(pred - actual) / actual * 100
            errors["seasonal_naive"].append(err)
            detail["seasonal_naive"].append({
                "predicting_quarter": qidx_to_label(int(actual_qidx)),
                "trained_through": qidx_to_label(int(tqi[-1])),
                "predicted": round(float(pred), 2), "actual": round(float(actual), 2),
                "error_pct": round(err, 1),
            })
        except Exception:
            pass

    rows = [{"method": m, "mape": np.mean(a), "n_origins": len(a)} for m, a in errors.items() if a]
    if not rows:
        return None, None, detail
    bt = pd.DataFrame(rows).sort_values("mape")
    return bt, bt.iloc[0]["method"], detail


# ============================================================================
# Per-company model
# ============================================================================
def forecast_company(qi_list, sales_list, quarter_list,
                      target_end_qidx=TARGET_END_QIDX, report_start_qidx=REPORT_START_QIDX):
    order = np.argsort(qi_list)
    qi_arr = np.array(qi_list)[order]
    sales_arr = np.array(sales_list)[order]
    quarters_arr = np.array(quarter_list)[order]
    n = len(sales_arr)

    result = {
        "n_quarters": n, "total_history_quarters": n,
        "first_quarter": qidx_to_label(int(qi_arr[0])) if n else None,
        "last_quarter": qidx_to_label(int(qi_arr[-1])) if n else None,
        "last_sales": sales_arr[-1] if n else None,
        "outliers": [], "flag_reasons": [], "plausible": True,
        "best_method": None, "mape": None, "forecast": {}, "backtest_detail": [],
        "seasonal_indices": None,
    }

    if n == 0:
        result.update({"tier": "NO_DATA", "confidence": "none"})
        return result

    last_qidx = int(qi_arr[-1])
    periods = target_end_qidx - last_qidx
    if periods <= 0:
        result.update({"tier": "NO_DATA", "confidence": "none",
                       "flag_reasons": ["last_actual_quarter_already_past_target"]})
        return result

    if n <= 3:
        fq = np.arange(last_qidx + 1, last_qidx + 1 + periods)
        full = dict(zip(fq, [sales_arr[-1]] * periods))
        labels = {qidx_to_label(y): v for y, v in full.items() if report_start_qidx <= y <= target_end_qidx}
        result.update({
            "tier": "FLAT", "confidence": "very_low", "best_method": "flat_carry",
            "forecast": labels,
        })
        return result

    total_history = n
    reasons = []
    confidence_downgrade = False

    # PRELIMINARY outlier pass (before structural break check). This matters:
    # a single anomalous quarter (e.g. a 53-week fiscal year, a one-time
    # consolidation, a data artifact) can look like a huge "decline" in the
    # quarter immediately after it -- which the structural break detector
    # would otherwise misread as a permanent business change, discarding
    # years of perfectly good history to "fix" what was really one bad
    # quarter. Excluding likely outliers first prevents that misdiagnosis.
    seasonal_idx_prelim = compute_seasonal_indices(qi_arr, quarters_arr, sales_arr)
    ds_prelim = deseasonalize(sales_arr, quarters_arr, seasonal_idx_prelim)
    prelim_outliers = detect_outliers(ds_prelim, qi_arr)
    prelim_mask = ~np.isin(qi_arr, prelim_outliers)
    if prelim_mask.sum() >= 6:  # only use the cleaned view if enough data remains
        qi_break_check = qi_arr[prelim_mask]
        seasonal_idx_break = compute_seasonal_indices(qi_break_check, quarters_arr[prelim_mask], sales_arr[prelim_mask])
        ds_break_check = deseasonalize(sales_arr[prelim_mask], quarters_arr[prelim_mask], seasonal_idx_break)
    else:
        qi_break_check, ds_break_check = qi_arr, ds_prelim

    struct_break = detect_structural_break(qi_break_check, ds_break_check)
    if struct_break is not None:
        keep = qi_arr >= struct_break
        if keep.sum() >= 4:
            qi_arr, sales_arr, quarters_arr = qi_arr[keep], sales_arr[keep], quarters_arr[keep]
            n = len(sales_arr)
            reasons.append(f"structural_break_{qidx_to_label(struct_break)}_pre_break_data_excluded")
            confidence_downgrade = True

    # Two-pass seasonal indices: recompute AFTER structural-break truncation,
    # then detect outliers on THAT deseasonalized series (fixes: seasonal
    # indices no longer contaminated by pre-break or outlier quarters)
    seasonal_idx = compute_seasonal_indices(qi_arr, quarters_arr, sales_arr)
    ds = deseasonalize(sales_arr, quarters_arr, seasonal_idx)
    outliers = detect_outliers(ds, qi_arr)

    mask = ~np.isin(qi_arr, outliers)
    qi_clean, sales_clean, q_clean = qi_arr[mask], sales_arr[mask], quarters_arr[mask]
    if len(qi_clean) < 4:
        qi_clean, sales_clean, q_clean = qi_arr, sales_arr, quarters_arr
        outliers = []

    # Recompute seasonal indices ONE more time on the fully cleaned series
    # (post structural-break AND post-outlier) for the final fit/forecast
    seasonal_idx = compute_seasonal_indices(qi_clean, q_clean, sales_clean)
    ds_clean = deseasonalize(sales_clean, q_clean, seasonal_idx)

    n_clean = len(sales_clean)
    if n_clean <= 7:
        tier, confidence = "MINIMAL", "low"
        methods = GROWTH_ONLY_METHODS
        bt, best, bt_detail = None, "cagr", {}
    elif n_clean <= 15:
        tier, confidence = "LIMITED", "low"
        methods = DESEASONALIZED_METHODS
        bt, best, bt_detail = rolling_backtest(qi_clean, ds_clean, sales_clean, q_clean,
                                                max(4, n_clean - 4))
        best = best or "holt"
    else:
        tier, confidence = "FULL", "normal"
        methods = DESEASONALIZED_METHODS
        bt, best, bt_detail = rolling_backtest(qi_clean, ds_clean, sales_clean, q_clean,
                                                min(MIN_TRAIN_FULL, n_clean - 4))
        best = best or "holt"

    if confidence_downgrade and confidence == "normal":
        confidence = "low"

    # A high fraction of excluded outliers is itself a signal the source
    # data is unreliable (e.g. inconsistent reporting basis across
    # quarters), not just a few genuine anomalies -- don't report "normal"
    # confidence in that case even if nothing else was flagged.
    outlier_rate = len(outliers) / n if n > 0 else 0
    if outlier_rate > 0.15 and confidence == "normal":
        confidence = "low"
        reasons.append(f"high_outlier_rate_{len(outliers)}of{n}_quarters_excluded")

    fit_last_qidx = int(qi_clean[-1])
    fit_periods = target_end_qidx - fit_last_qidx

    forecasts = {}
    for name, fn in methods.items():
        try:
            vals_ds, fq = fn(qi_clean, ds_clean, fit_periods)
            fquarters = np.array([(q % 4) if (q % 4) != 0 else 4 for q in fq])
            forecasts[name] = reseasonalize(vals_ds, fquarters, seasonal_idx)
        except Exception:
            pass
    if tier != "MINIMAL":
        try:
            vals_sn, fq = f_seasonal_naive(qi_clean, sales_clean, q_clean, fit_periods)
            forecasts["seasonal_naive"] = vals_sn
        except Exception:
            pass

    if bt is not None and len(bt) >= 3:
        top3 = bt.nsmallest(3, "mape")["method"].tolist()
        top3 = [m for m in top3 if m in forecasts] or list(forecasts.keys())[:3]
    elif forecasts:
        top3 = list(forecasts.keys())[:3]
    else:
        top3 = []
    ensemble = (np.mean([forecasts[m] for m in top3], axis=0)
                if top3 else np.array([sales_clean[-1]] * fit_periods))

    fq_full = np.arange(fit_last_qidx + 1, fit_last_qidx + 1 + fit_periods)
    mape = None
    if bt is not None:
        row = bt[bt["method"] == best]
        if len(row):
            mape = float(row.iloc[0]["mape"])

    full_forecast = dict(zip(fq_full, np.round(ensemble, 2)))
    effective_start = max(report_start_qidx, last_qidx + 1)
    reported_raw = {y: v for y, v in full_forecast.items() if effective_start <= y <= target_end_qidx}

    plausible = True
    vals = list(reported_raw.values())
    if any(v <= 0 for v in vals):
        plausible = False
        reasons.append("forecast_non_positive")
    if vals and sales_clean[-1] > 0:
        last_fq = max(reported_raw.keys())
        quarters_out = last_fq - qi_arr[-1]
        years_out = quarters_out / 4
        implied_cagr = (reported_raw[last_fq] / sales_clean[-1]) ** (1 / years_out) - 1 if years_out > 0 else 0
        if implied_cagr > IMPLAUSIBLE_GROWTH_CAGR or implied_cagr < IMPLAUSIBLE_DECLINE_CAGR:
            plausible = False
            reasons.append(f"implied_cagr_{implied_cagr:.0%}")

    if not plausible:
        confidence = "flagged_implausible"

    # Extra safeguard: MINIMAL/LIMITED tier compounding over a long horizon
    # can pass the annualized-CAGR check yet still produce an extreme
    # absolute multiple (a modest-looking per-quarter rate compounds a lot
    # over 20+ quarters). Flag this explicitly rather than relying only on
    # the annualized rate, which a long horizon can dilute below threshold.
    if tier in ("MINIMAL", "LIMITED") and vals:
        total_multiple = reported_raw[max(reported_raw.keys())] / sales_clean[-1] if sales_clean[-1] > 0 else None
        quarters_out_check = max(reported_raw.keys()) - qi_arr[-1]
        if total_multiple is not None and quarters_out_check > 16 and (total_multiple > 3 or total_multiple < 0.33):
            plausible = False
            confidence = "flagged_implausible"
            reasons.append(f"long_horizon_extrapolation_{quarters_out_check}q_from_{n_clean}pts_{total_multiple:.1f}x")

    reported = {qidx_to_label(y): v for y, v in reported_raw.items()}
    outlier_labels = [qidx_to_label(o) for o in outliers]

    result.update({
        "n_quarters": n_clean, "total_history_quarters": total_history,
        "tier": tier, "confidence": confidence, "best_method": best, "mape": mape,
        "outliers": outlier_labels, "ensemble_methods": top3, "plausible": plausible,
        "flag_reasons": reasons, "forecast": reported,
        "seasonal_indices": {f"Q{i+1}": round(float(v), 3) for i, v in enumerate(seasonal_idx)},
        "backtest_detail": bt_detail.get(best, []) if bt_detail else [],
    })
    return result


# ============================================================================
# RUN: produces `projections` -- this is Cell 1's output, consumed by Cell 2
# ============================================================================
def generate_projections(input_path, sheet_name=0,
                          target_end_qidx=TARGET_END_QIDX, report_start_qidx=REPORT_START_QIDX):
    long_df, all_companies = load_long(input_path, sheet_name=sheet_name)
    results = []
    for company in all_companies:
        sub = long_df[long_df["company"] == company].sort_values("qidx")
        r = forecast_company(sub["qidx"].tolist(), sub["sales"].tolist(), sub["quarter"].tolist(),
                              target_end_qidx=target_end_qidx, report_start_qidx=report_start_qidx)
        r["company"] = company
        results.append(r)
    projections = pd.DataFrame(results)
    print(f"Modeled {len(projections)} companies")
    print(projections["tier"].value_counts().to_string())
    return projections, long_df


if INPUT_FILE:
    projections, long_df = generate_projections(INPUT_FILE, SHEET_NAME, TARGET_END_QIDX, REPORT_START_QIDX)
else:
    print("Set INPUT_FILE above, then re-run this cell.")

# Automated Testing
"""
CELL 2 -- TESTING & FINAL FILE: validates Cell 1's projections and exports
=============================================================================
Run this cell AFTER Cell 1. It expects a `projections` DataFrame (and
`long_df`) already in memory from Cell 1 -- it does no modeling itself,
only validation and file output.

WHAT "TESTING" MEANS HERE (automated, per company):
  - Is the backtest MAPE high enough that the method demonstrably struggled
    on its own past data? (>25% flagged)
  - Does the forecast imply an implausible annualized growth/decline rate?
  - Does compounding over a long horizon from thin data imply an extreme
    absolute multiple, even if the annualized rate looks tame?
    (`long_horizon_extrapolation`)
  - Was an unusually high share of quarters excluded as outliers, signaling
    a likely source-data quality issue rather than a few genuine anomalies?
    (`high_outlier_rate`)
  - Did a structural break, outlier exclusion, or thin data history reduce
    confidence in the number?
  Any of these -> `needs_review = True`, with a plain-English `explanation`
  of why, so nothing gets treated as trustworthy just because a number
  exists.

OUTPUT OF THIS CELL: `forecast_report_quarterly.xlsx` (or your custom
OUTPUT_FILE) -- Summary / Needs Review / Backtest Detail / Seasonal
Indices / Methodology sheets.
"""

import pandas as pd
from datetime import datetime

TARGET_END_QIDX = globals().get("TARGET_END_QIDX", 2031 * 4 + 4)
REPORT_START_QIDX = globals().get("REPORT_START_QIDX", 2026 * 4 + 1)
IMPLAUSIBLE_GROWTH_CAGR = globals().get("IMPLAUSIBLE_GROWTH_CAGR", 0.60)
IMPLAUSIBLE_DECLINE_CAGR = globals().get("IMPLAUSIBLE_DECLINE_CAGR", -0.25)

OUTPUT_FILE = "forecast_report_quarterly.xlsx"


def qidx_to_label(idx):
    quarter = idx % 4
    if quarter == 0:
        quarter = 4
        year = idx // 4 - 1
    else:
        year = idx // 4
    return f"Q{quarter} {year}"


# ============================================================================
# Explanations
# ============================================================================
def explain_reasons(reasons):
    if not reasons:
        return ""
    parts = []
    for r in reasons:
        if r.startswith("structural_break"):
            q_label = r.split("_")[2]
            parts.append(f"Likely corporate action around {q_label}; forecast uses only post-{q_label} history.")
        elif r.startswith("implied_cagr"):
            pct = r.replace("implied_cagr_", "")
            parts.append(f"Forecast implies an unrealistic {pct}/yr annualized rate -- treat as a sanity-check failure.")
        elif r == "forecast_non_positive":
            parts.append("Forecast trend implies revenue reaching zero or negative.")
        elif r == "last_actual_quarter_already_past_target":
            parts.append("Already has actual data through/past the target forecast quarter.")
        elif r.startswith("high_outlier_rate"):
            frac = r.replace("high_outlier_rate_", "").replace("_quarters_excluded", "")
            parts.append(f"Unusually high share of quarters ({frac}) excluded as statistical outliers -- "
                          f"likely a source data quality issue (inconsistent reporting basis, currency, "
                          f"or restatements), not just a few genuine anomalies. Worth checking the raw data.")
        elif r.startswith("long_horizon_extrapolation"):
            parts.append("Forecast extrapolates many quarters beyond a very short usable history -- "
                          "treat the absolute magnitude with skepticism regardless of the annualized rate.")
        else:
            parts.append(r)
    return " | ".join(parts)


def explain_confidence(row):
    notes = []
    if row["tier"] == "NO_DATA":
        notes.append("No usable revenue history.")
    elif row["tier"] == "FLAT":
        notes.append("3 or fewer quarters of data -- no growth signal; flat carry-forward.")
    elif row["tier"] == "MINIMAL":
        notes.append("4-7 quarters (under 2 years) -- can't reliably separate seasonality from trend; growth-only methods, no backtest.")
    elif row["tier"] == "LIMITED":
        notes.append(f"Only {row['n_quarters']} quarters of usable data -- backtest has few validation points, treat MAPE loosely.")
    if row.get("mape") is not None and row["mape"] > 25:
        notes.append(f"Backtest MAPE of {row['mape']:.0f}% is high.")
    reason_text = explain_reasons(row.get("flag_reasons", []))
    if reason_text:
        notes.append(reason_text)
    return " ".join(notes)


# ============================================================================
# AUTOMATED TESTING: flag anything that shouldn't be trusted at face value
# ============================================================================
def validate_projections(projections):
    tested = projections.copy()
    tested["needs_review"] = (
        tested["confidence"].isin(["flagged_implausible"])
        | (tested["mape"].fillna(0) > 25)
        | tested["flag_reasons"].apply(lambda x: len(x) > 0)
    )
    tested["explanation"] = tested.apply(explain_confidence, axis=1)

    print(f"Tested {len(tested)} companies")
    print(f"Flagged for review: {tested['needs_review'].sum()}")
    return tested


# ============================================================================
# FINAL FILE EXPORT
# ============================================================================
def export_report(summary, output_path, target_end_qidx=TARGET_END_QIDX, report_start_qidx=REPORT_START_QIDX):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    forecast_labels = [qidx_to_label(q) for q in range(report_start_qidx, target_end_qidx + 1)]
    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", start_color="404040")
    body_font = Font(name="Arial", size=10)
    fill_flagged = PatternFill("solid", start_color="F4CCCC")
    fill_low = PatternFill("solid", start_color="FFF2CC")
    fill_none = PatternFill("solid", start_color="D9D9D9")

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    cols = (["Company", "Total History (qtrs)", "Quarters Used", "Tier", "Confidence",
             "Best Method", "Backtest MAPE %", "Needs Review"] + forecast_labels + ["Flag Reasons"])
    ws.append(cols)
    for c in ws[1]:
        c.font = header_font; c.fill = header_fill; c.alignment = Alignment(horizontal="center")

    for _, row in summary.iterrows():
        fc = row["forecast"] or {}
        vals = [
            row["company"], row.get("total_history_quarters"), row.get("n_quarters"),
            row["tier"], row["confidence"], row.get("best_method"),
            round(row["mape"], 1) if pd.notna(row.get("mape")) else None,
            "YES" if row["needs_review"] else "",
        ] + [fc.get(lbl) for lbl in forecast_labels] + [", ".join(row.get("flag_reasons", []))]
        ws.append(vals)
        r = ws.max_row
        for cell in ws[r]:
            cell.font = body_font
        if row["confidence"] == "flagged_implausible":
            for cell in ws[r]: cell.fill = fill_flagged
        elif row["tier"] == "NO_DATA":
            for cell in ws[r]: cell.fill = fill_none
        elif row["confidence"] in ("low", "very_low"):
            for cell in ws[r]: cell.fill = fill_low

    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 42
    for i in range(2, len(cols) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 12
    ws.column_dimensions[get_column_letter(len(cols))].width = 45

    ws2 = wb.create_sheet("Needs Review")
    ws2.append(["Company", "Tier", "Confidence", "Backtest MAPE %", "Why flagged"])
    for c in ws2[1]:
        c.font = header_font; c.fill = header_fill
    review = summary[summary["needs_review"]].sort_values("mape", ascending=False, na_position="last")
    for _, row in review.iterrows():
        ws2.append([row["company"], row["tier"], row["confidence"],
                    round(row["mape"], 1) if pd.notna(row.get("mape")) else None, row["explanation"]])
        for cell in ws2[ws2.max_row]:
            cell.font = body_font
    ws2.freeze_panes = "A2"
    ws2.column_dimensions["A"].width = 42
    ws2.column_dimensions["E"].width = 100
    for col in ["B", "C", "D"]:
        ws2.column_dimensions[col].width = 16

    ws4 = wb.create_sheet("Backtest Detail")
    ws4.append(["Company", "Best Method", "Predicting", "Trained Through", "Predicted", "Actual", "Error %"])
    for c in ws4[1]:
        c.font = header_font; c.fill = header_fill
    for _, row in summary.iterrows():
        for d in (row.get("backtest_detail") or []):
            ws4.append([row["company"], row.get("best_method"), d["predicting_quarter"],
                        d["trained_through"], d["predicted"], d["actual"], d["error_pct"]])
            for cell in ws4[ws4.max_row]:
                cell.font = body_font
    ws4.freeze_panes = "A2"
    ws4.column_dimensions["A"].width = 42
    for col in ["B", "C", "D", "E", "F", "G"]:
        ws4.column_dimensions[col].width = 15

    ws5 = wb.create_sheet("Seasonal Indices")
    ws5.append(["Company", "Q1", "Q2", "Q3", "Q4"])
    for c in ws5[1]:
        c.font = header_font; c.fill = header_fill
    for _, row in summary.iterrows():
        si = row.get("seasonal_indices")
        if si:
            ws5.append([row["company"], si.get("Q1"), si.get("Q2"), si.get("Q3"), si.get("Q4")])
            for cell in ws5[ws5.max_row]:
                cell.font = body_font
    ws5.column_dimensions["A"].width = 42

    ws3 = wb.create_sheet("Methodology")
    lines = [
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Target forecast quarters", f"{qidx_to_label(report_start_qidx)}-{qidx_to_label(target_end_qidx)}"),
        ("", ""),
        ("Tier", "Meaning"),
        ("NO_DATA", "No revenue history available"),
        ("FLAT", "1-3 quarters; flat carry-forward, no growth signal"),
        ("MINIMAL", "4-7 quarters (under 2 years); growth-only methods, no seasonal decomposition, no backtest"),
        ("LIMITED", "8-15 quarters (2-4 years); seasonal decomposition attempted, thin backtest"),
        ("FULL", "16+ quarters (4+ years); full method set, robust rolling-origin backtest"),
        ("", ""),
        ("Flag", "Meaning"),
        ("structural_break_QX_YYYY", "Persistent decline detected on deseasonalized data; pre-break data excluded from fitting"),
        ("implied_cagr_X%", f"Forecast implies >{IMPLAUSIBLE_GROWTH_CAGR:.0%}/yr or worse than {IMPLAUSIBLE_DECLINE_CAGR:.0%}/yr annualized -- flagged as implausible"),
        ("forecast_non_positive", "Forecast trend implies revenue reaching zero or negative"),
        ("", ""),
        ("Note", "All 6 trend methods (linear/cagr/exp_smoothing/holt/ma_trend/weighted_avg) fit on "
                 "DESEASONALIZED data, then reseasonalize the output. seasonal_naive uses raw same-quarter "
                 "year-over-year comparison directly. Seasonal indices are computed AFTER structural-break "
                 "and outlier exclusion, so anomalous quarters don't distort what 'normal' looks like for "
                 "each quarter of the year."),
    ]
    for line in lines:
        ws3.append(line)
    ws3.column_dimensions["A"].width = 28
    ws3.column_dimensions["B"].width = 95
    for r in ws3.iter_rows():
        for cell in r:
            cell.font = body_font

    wb.save(output_path)


# ============================================================================
# RUN: takes `projections` from Cell 1, tests it, writes the final file
# ============================================================================
if "projections" not in dir():
    print("No `projections` found -- run Cell 1 first.")
else:
    tested = validate_projections(projections)
    export_report(tested, OUTPUT_FILE, TARGET_END_QIDX, REPORT_START_QIDX)
    print(f"\nReport written to: {OUTPUT_FILE}")
