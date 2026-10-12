# -*- coding: utf-8 -*-
"""MDP Annual Projections and Testing.ipynb

"""

# Annual Projections
"""
CELL 1 -- MODEL: produces raw revenue projections
====================================================
Run this cell first. It loads the Excel file, fits all 6 forecasting
methods per company (with structural-break and outlier handling), backtests
them, and builds the ensemble forecast.

OUTPUT OF THIS CELL: a variable called `projections` (a pandas DataFrame,
one row per company) containing the raw model output -- tier, confidence,
best method, backtest MAPE, the forecast itself, outlier years, structural
breaks, and the walk-forward backtest detail. Nothing in this cell decides
what's "good" or "bad" -- that judgment happens in Cell 2, which consumes
`projections` directly.

Just set INPUT_FILE below and run this cell.
"""

import warnings
import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore")

# ============================================================================
# CONFIG -- tune here, not scattered through the code
# ============================================================================
STRUCTURAL_BREAK_THRESHOLD = 0.40      # one-year decline magnitude to consider a break
STRUCTURAL_BREAK_PERSIST_FACTOR = 0.6  # how much of the drop must persist afterward
OUTLIER_Z_THRESHOLD = 1.5
OUTLIER_IQR_MULT = 1.5
MIN_TRAIN_FULL = 4                     # backtest min_train for 6+ year (FULL) companies
IMPLAUSIBLE_GROWTH_CAGR = 0.60         # flag if implied annualized growth exceeds this
IMPLAUSIBLE_DECLINE_CAGR = -0.25       # flag if implied annualized decline exceeds this
TARGET_END_YEAR = 2031
REPORT_START_YEAR = 2026

# ============================================================================
# SET YOUR FILE HERE
# ============================================================================
INPUT_FILE = "Screening_Results_20260709_0638 (1).xlsx"          # e.g. "Screening_Results.xlsx"
SHEET_NAME = 0              # sheet name or index; 0 = first sheet


# ============================================================================
# Data loading
# ============================================================================
def load_long(path, sheet_name=0):
    """Wide (company x FY-year columns) -> long (company, year, sales)."""
    df = pd.read_excel(path, sheet_name=sheet_name)
    df = df.drop_duplicates(subset=[df.columns[0]]).reset_index(drop=True)
    company_col = df.columns[0]

    year_cols = {}
    for c in df.columns[1:]:
        digits = "".join(ch for ch in str(c) if ch.isdigit())
        for i in range(len(digits) - 3):
            candidate = digits[i:i + 4]
            if 1990 <= int(candidate) <= 2100:
                year_cols[c] = int(candidate)
    if not year_cols:
        raise ValueError("Could not find any year columns in the input file.")

    rows = []
    for _, row in df.iterrows():
        company = row[company_col]
        for c, year in year_cols.items():
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
            rows.append({"company": company, "year": year, "sales": v})
    return pd.DataFrame(rows), df[company_col].tolist()


# ============================================================================
# Gap-aware growth rates
# ============================================================================
def annualized_growth_rates(years, sales):
    rates = []
    for i in range(1, len(years)):
        dy = years[i] - years[i - 1]
        if dy <= 0 or sales[i - 1] <= 0:
            continue
        rates.append((sales[i] / sales[i - 1]) ** (1 / dy) - 1)
    return np.array(rates)


def detect_structural_break(years, sales, threshold=STRUCTURAL_BREAK_THRESHOLD):
    if len(sales) < 4:
        return None
    breaks = []
    for i in range(1, len(sales)):
        dy = years[i] - years[i - 1]
        if dy <= 0 or sales[i - 1] <= 0:
            continue
        chg = (sales[i] / sales[i - 1]) ** (1 / dy) - 1
        if chg < -threshold:
            breaks.append((years[i], chg))
    if not breaks:
        return None
    break_year, _ = breaks[-1]
    idx = list(years).index(break_year)
    if idx >= len(sales) - 1:
        return None
    pre_level = sales[idx - 1]
    post_level = np.mean(sales[idx:])
    if (pre_level - post_level) / pre_level > threshold * STRUCTURAL_BREAK_PERSIST_FACTOR:
        return break_year
    return None


def detect_outliers(years, sales, z_threshold=OUTLIER_Z_THRESHOLD, iqr_mult=OUTLIER_IQR_MULT):
    if len(sales) < 4:
        return []
    rates = annualized_growth_rates(years, sales)
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
                flags.add(years[i + 1])
    if iqr > 0:
        lower, upper = q1 - iqr_mult * iqr, q3 + iqr_mult * iqr
        for i, r in enumerate(rates):
            if r < lower or r > upper:
                flags.add(years[i + 1])
    return sorted(flags)


# ============================================================================
# Forecast methods
# ============================================================================
def f_linear(years, sales, periods):
    slope, intercept, *_ = stats.linregress(years, sales)
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return intercept + slope * fy, fy

def f_cagr(years, sales, periods):
    n = years[-1] - years[0]
    cagr = (sales[-1] / sales[0]) ** (1 / n) - 1 if n > 0 and sales[0] > 0 else 0
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return np.array([sales[-1] * (1 + cagr) ** i for i in range(1, periods + 1)]), fy

def f_exp_smoothing(years, sales, periods, alpha=0.3):
    smoothed = [sales[0]]
    for i in range(1, len(sales)):
        smoothed.append(alpha * sales[i] + (1 - alpha) * smoothed[-1])
    trend = (smoothed[-1] - smoothed[-2]) if len(smoothed) > 1 else 0
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return np.array([smoothed[-1] + trend * i for i in range(1, periods + 1)]), fy

def f_holt(years, sales, periods, alpha=0.3, beta=0.1):
    level, trend = sales[0], (sales[1] - sales[0] if len(sales) > 1 else 0)
    for i in range(1, len(sales)):
        prev = level
        level = alpha * sales[i] + (1 - alpha) * (level + trend)
        trend = beta * (level - prev) + (1 - beta) * trend
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    return np.array([level + trend * i for i in range(1, periods + 1)]), fy

def f_ma_trend(years, sales, periods, window=3):
    rates = annualized_growth_rates(years, sales)
    avg_g = (rates[-window:].mean() if len(rates) >= window else rates.mean()) if len(rates) else 0
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    out, last = [], sales[-1]
    for _ in range(periods):
        last *= (1 + avg_g)
        out.append(last)
    return np.array(out), fy

def f_weighted_avg(years, sales, periods):
    rates = annualized_growth_rates(years, sales)
    if len(rates) == 0:
        wg = 0
    else:
        w = np.arange(1, len(rates) + 1)
        wg = np.average(rates, weights=w)
    fy = np.arange(years[-1] + 1, years[-1] + 1 + periods)
    out, last = [], sales[-1]
    for _ in range(periods):
        last *= (1 + wg)
        out.append(last)
    return np.array(out), fy

METHODS = {
    "linear": f_linear, "cagr": f_cagr, "exp_smoothing": f_exp_smoothing,
    "holt": f_holt, "ma_trend": f_ma_trend, "weighted_avg": f_weighted_avg,
}
GROWTH_ONLY_METHODS = {"cagr": f_cagr, "ma_trend": f_ma_trend, "weighted_avg": f_weighted_avg}


# ============================================================================
# Rolling-origin backtest
# ============================================================================
def rolling_backtest(years, sales, methods, min_train):
    n = len(sales)
    errors = {m: [] for m in methods}
    detail = {m: [] for m in methods}
    for split in range(min_train, n):
        ty, ts, actual_year, actual = years[:split], sales[:split], years[split], sales[split]
        for name, fn in methods.items():
            try:
                pred = fn(ty, ts, 1)[0][0]
                errors[name].append(abs(pred - actual) / actual * 100)
                detail[name].append({
                    "predicting_year": int(actual_year),
                    "trained_on": f"{int(ty[0])}-{int(ty[-1])}",
                    "predicted": round(float(pred), 2),
                    "actual": round(float(actual), 2),
                    "error_pct": round(abs(pred - actual) / actual * 100, 1),
                })
            except Exception:
                pass
    rows = [{"method": m, "mape": np.mean(a), "n_origins": len(a)} for m, a in errors.items() if a]
    if not rows:
        return None, None, detail
    bt = pd.DataFrame(rows).sort_values("mape")
    return bt, bt.iloc[0]["method"], detail


# ============================================================================
# Per-company model: this is the core of Cell 1's output
# ============================================================================
def forecast_company(years, sales, target_end_year=TARGET_END_YEAR, report_start_year=REPORT_START_YEAR):
    years, sales = np.array(years), np.array(sales)
    n = len(sales)

    result = {
        "n_years": n, "total_history_years": n,
        "first_year": int(years[0]) if n else None,
        "last_year": int(years[-1]) if n else None,
        "last_sales": sales[-1] if n else None,
        "outliers": [], "flag_reasons": [], "plausible": True,
        "best_method": None, "mape": None, "forecast": {}, "backtest_detail": [],
    }

    if n == 0:
        result.update({"tier": "NO_DATA", "confidence": "none"})
        return result

    last_year = int(years[-1])
    periods = target_end_year - last_year
    if periods <= 0:
        result.update({"tier": "NO_DATA", "confidence": "none",
                       "flag_reasons": ["last_actual_year_already_past_target"]})
        return result

    if n == 1:
        fy_full = np.arange(last_year + 1, last_year + 1 + periods)
        full = dict(zip(fy_full, [sales[-1]] * periods))
        result.update({
            "tier": "FLAT", "confidence": "very_low", "best_method": "flat_carry",
            "forecast": {y: v for y, v in full.items() if report_start_year <= y <= target_end_year},
        })
        return result

    total_history = n
    struct_break = detect_structural_break(years, sales)
    reasons = []
    confidence_downgrade = False
    if struct_break is not None:
        keep = years >= struct_break
        if keep.sum() >= 2:
            years, sales = years[keep], sales[keep]
            n = len(sales)
            reasons.append(f"structural_break_{struct_break}_pre_break_data_excluded")
            confidence_downgrade = True

    outliers = detect_outliers(years, sales)
    mask = ~np.isin(years, outliers)
    y_clean, s_clean = years[mask], sales[mask]
    if len(y_clean) < 2:
        y_clean, s_clean = years, sales
        outliers = []

    if n == 2:
        methods, tier, confidence = GROWTH_ONLY_METHODS, "MINIMAL", "low"
        bt, best, bt_detail = None, "cagr", {}
    elif n <= 5:
        methods, tier, confidence = METHODS, "LIMITED", "low"
        bt, best, bt_detail = rolling_backtest(y_clean, s_clean, methods, max(2, len(s_clean) - 2))
        best = best or "cagr"
    else:
        methods, tier, confidence = METHODS, "FULL", "normal"
        bt, best, bt_detail = rolling_backtest(y_clean, s_clean, methods, min(MIN_TRAIN_FULL, len(s_clean) - 2))
        best = best or "holt"

    if confidence_downgrade and confidence == "normal":
        confidence = "low"

    fit_last_year = int(y_clean[-1])
    fit_periods = target_end_year - fit_last_year

    forecasts = {}
    for name, fn in methods.items():
        try:
            vals, _ = fn(y_clean, s_clean, fit_periods)
            forecasts[name] = vals
        except Exception:
            pass

    if bt is not None and len(bt) >= 3:
        top3 = bt.nsmallest(3, "mape")["method"].tolist()
    elif forecasts:
        top3 = list(forecasts.keys())[:3]
    else:
        top3 = []
    ensemble = (np.mean([forecasts[m] for m in top3 if m in forecasts], axis=0)
                if top3 else np.array([s_clean[-1]] * fit_periods))

    fy = np.arange(fit_last_year + 1, fit_last_year + 1 + fit_periods)
    mape = None
    if bt is not None:
        row = bt[bt["method"] == best]
        if len(row):
            mape = float(row.iloc[0]["mape"])

    full_forecast = dict(zip(fy, np.round(ensemble, 2)))
    effective_start = max(report_start_year, last_year + 1)
    reported = {y: v for y, v in full_forecast.items() if effective_start <= y <= target_end_year}

    plausible = True
    vals = list(reported.values())
    if any(v <= 0 for v in vals):
        plausible = False
        reasons.append("forecast_non_positive")
    if vals and s_clean[-1] > 0:
        last_forecast_year = max(reported.keys())
        yrs_out = last_forecast_year - years[-1]
        implied_cagr = (reported[last_forecast_year] / s_clean[-1]) ** (1 / yrs_out) - 1 if yrs_out > 0 else 0
        if implied_cagr > IMPLAUSIBLE_GROWTH_CAGR or implied_cagr < IMPLAUSIBLE_DECLINE_CAGR:
            plausible = False
            reasons.append(f"implied_cagr_{implied_cagr:.0%}")

    if not plausible:
        confidence = "flagged_implausible"

    result.update({
        "n_years": n, "total_history_years": total_history,
        "tier": tier, "confidence": confidence, "best_method": best, "mape": mape,
        "outliers": outliers, "ensemble_methods": top3, "plausible": plausible,
        "flag_reasons": reasons, "forecast": reported,
        "backtest_detail": bt_detail.get(best, []) if bt_detail else [],
    })
    return result


# ============================================================================
# RUN: produces `projections` -- this is Cell 1's output, consumed by Cell 2
# ============================================================================
def generate_projections(input_path, sheet_name=0,
                          target_end_year=TARGET_END_YEAR, report_start_year=REPORT_START_YEAR):
    long_df, all_companies = load_long(input_path, sheet_name=sheet_name)

    results = []
    for company in all_companies:
        sub = long_df[long_df["company"] == company].sort_values("year")
        r = forecast_company(sub["year"].tolist(), sub["sales"].tolist(),
                              target_end_year=target_end_year, report_start_year=report_start_year)
        r["company"] = company
        results.append(r)

    projections = pd.DataFrame(results)
    print(f"Modeled {len(projections)} companies")
    print(projections["tier"].value_counts().to_string())
    return projections, long_df


if INPUT_FILE:
    projections, long_df = generate_projections(INPUT_FILE, SHEET_NAME, TARGET_END_YEAR, REPORT_START_YEAR)
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
  - Does the forecast imply an implausible growth/decline rate? (already
    flagged in Cell 1 as `flagged_implausible`, surfaced here)
  - Did a structural break, outlier exclusion, or thin data history reduce
    confidence in the number?
  Any of these -> `needs_review = True`, with a plain-English `explanation`
  of why, so nothing gets treated as trustworthy just because a number
  exists.

OUTPUT OF THIS CELL: `forecast_report.xlsx` (or your custom OUTPUT_FILE) --
Summary / Needs Review / Backtest Detail / Methodology sheets.
"""

import pandas as pd
from datetime import datetime

TARGET_END_YEAR = globals().get("TARGET_END_YEAR", 2031)
REPORT_START_YEAR = globals().get("REPORT_START_YEAR", 2026)
IMPLAUSIBLE_GROWTH_CAGR = globals().get("IMPLAUSIBLE_GROWTH_CAGR", 0.60)
IMPLAUSIBLE_DECLINE_CAGR = globals().get("IMPLAUSIBLE_DECLINE_CAGR", -0.25)

OUTPUT_FILE = "forecast_report.xlsx"


# ============================================================================
# Human-readable explanations for the "why" behind a flag
# ============================================================================
def explain_reasons(reasons):
    if not reasons:
        return ""
    parts = []
    for r in reasons:
        if r.startswith("structural_break"):
            year = r.split("_")[2]
            parts.append(f"Likely corporate action (spinoff/divestiture) around {year}; "
                          f"forecast uses only post-{year} history.")
        elif r.startswith("implied_cagr"):
            pct = r.replace("implied_cagr_", "")
            parts.append(f"Forecast implies an unrealistic {pct}/yr compounded rate through "
                          f"{TARGET_END_YEAR} -- treat as a sanity-check failure, not a usable number.")
        elif r == "forecast_non_positive":
            parts.append("Forecast trend implies revenue reaching zero or negative -- model "
                          "extrapolated a decline too far.")
        elif r == "last_actual_year_already_past_target":
            parts.append("Already has actual data through/past the target forecast year.")
        else:
            parts.append(r)
    return " | ".join(parts)


def explain_confidence(row):
    notes = []
    if row["tier"] == "NO_DATA":
        notes.append("No usable revenue history.")
    elif row["tier"] == "FLAT":
        notes.append("Only 1 year of data -- no growth rate exists; forecast is a flat carry-forward.")
    elif row["tier"] == "MINIMAL":
        notes.append("Only 2 years of data -- a single growth rate, no backtest possible.")
    elif row["tier"] == "LIMITED":
        notes.append(f"Only {row['n_years']} years of usable data -- backtest has very few "
                      f"validation points, treat MAPE loosely.")
    if row.get("mape") is not None and row["mape"] > 25:
        notes.append(f"Backtest MAPE of {row['mape']:.0f}% is high -- the chosen method "
                      f"historically missed by a wide margin even on its own best case.")
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
def export_report(summary, output_path, target_end_year=TARGET_END_YEAR, report_start_year=REPORT_START_YEAR):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    forecast_years = list(range(report_start_year, target_end_year + 1))
    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", start_color="404040")
    body_font = Font(name="Arial", size=10)
    fill_flagged = PatternFill("solid", start_color="F4CCCC")
    fill_low = PatternFill("solid", start_color="FFF2CC")
    fill_none = PatternFill("solid", start_color="D9D9D9")

    wb = Workbook()

    # ---- Summary sheet ----
    ws = wb.active
    ws.title = "Summary"
    cols = (["Company", "Total History (yrs)", "Years Used", "Tier", "Confidence",
             "Best Method", "Backtest MAPE %", "Needs Review"]
            + [str(y) for y in forecast_years] + ["Flag Reasons"])
    ws.append(cols)
    for c in ws[1]:
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal="center")

    for _, row in summary.iterrows():
        fc = row["forecast"] or {}
        vals = [
            row["company"], row.get("total_history_years"), row.get("n_years"),
            row["tier"], row["confidence"], row.get("best_method"),
            round(row["mape"], 1) if pd.notna(row.get("mape")) else None,
            "YES" if row["needs_review"] else "",
        ] + [fc.get(y) for y in forecast_years] + [", ".join(row.get("flag_reasons", []))]
        ws.append(vals)
        r = ws.max_row
        for cell in ws[r]:
            cell.font = body_font
        if row["confidence"] == "flagged_implausible":
            for cell in ws[r]:
                cell.fill = fill_flagged
        elif row["tier"] == "NO_DATA":
            for cell in ws[r]:
                cell.fill = fill_none
        elif row["confidence"] in ("low", "very_low"):
            for cell in ws[r]:
                cell.fill = fill_low

    ws.freeze_panes = "A2"
    ws.column_dimensions["A"].width = 42
    for i in range(2, len(cols) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 14
    ws.column_dimensions[get_column_letter(len(cols))].width = 45

    # ---- Needs Review sheet ----
    ws2 = wb.create_sheet("Needs Review")
    ws2.append(["Company", "Tier", "Confidence", "Backtest MAPE %", "Why flagged"])
    for c in ws2[1]:
        c.font = header_font
        c.fill = header_fill
    review = summary[summary["needs_review"]].sort_values(
        "mape", ascending=False, na_position="last"
    )
    for _, row in review.iterrows():
        ws2.append([
            row["company"], row["tier"], row["confidence"],
            round(row["mape"], 1) if pd.notna(row.get("mape")) else None,
            row["explanation"],
        ])
        for cell in ws2[ws2.max_row]:
            cell.font = body_font
    ws2.freeze_panes = "A2"
    ws2.column_dimensions["A"].width = 42
    ws2.column_dimensions["E"].width = 100
    for col in ["B", "C", "D"]:
        ws2.column_dimensions[col].width = 16

    # ---- Backtest Detail sheet (walk-forward validation table, all companies) ----
    ws4 = wb.create_sheet("Backtest Detail")
    ws4.append(["Company", "Best Method", "Predicting", "Predicted", "Actual", "Error %"])
    for c in ws4[1]:
        c.font = header_font
        c.fill = header_fill
    for _, row in summary.iterrows():
        detail = row.get("backtest_detail") or []
        for d in detail:
            ws4.append([
                row["company"], row.get("best_method"),
                f'{d["predicting_year"]} (from {d["trained_on"]})',
                d["predicted"], d["actual"], d["error_pct"],
            ])
            for cell in ws4[ws4.max_row]:
                cell.font = body_font
    ws4.freeze_panes = "A2"
    ws4.column_dimensions["A"].width = 42
    ws4.column_dimensions["B"].width = 14
    ws4.column_dimensions["C"].width = 24
    for col in ["D", "E", "F"]:
        ws4.column_dimensions[col].width = 14

    # ---- Methodology sheet ----
    ws3 = wb.create_sheet("Methodology")
    lines = [
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Target forecast years", f"{report_start_year}-{target_end_year}"),
        ("", ""),
        ("Tier", "Meaning"),
        ("NO_DATA", "No revenue history available"),
        ("FLAT", "1 year of data; forecast is a flat carry-forward, no growth signal"),
        ("MINIMAL", "2 years of data; growth-rate methods only, no backtest possible"),
        ("LIMITED", "3-5 years of data; full method set but thin backtest (few validation points)"),
        ("FULL", "6+ years of data; full method set with rolling-origin backtest"),
        ("", ""),
        ("Flag", "Meaning"),
        ("structural_break_YYYY", "Large persistent decline detected (e.g. spinoff/divestiture); pre-break data excluded from fitting"),
        ("implied_cagr_X%", f"Forecast implies >{IMPLAUSIBLE_GROWTH_CAGR:.0%}/yr growth or worse than {IMPLAUSIBLE_DECLINE_CAGR:.0%}/yr decline through {target_end_year} -- flagged as implausible"),
        ("forecast_non_positive", "Forecast trend implies revenue reaching zero or negative"),
        ("", ""),
        ("Note", "Backtest MAPE is the mean absolute percentage error from rolling-origin "
                 "1-step-ahead validation of the selected method, not a guarantee of future accuracy."),
        ("", ""),
        ("Backtest Detail sheet", "Shows the walk-forward validation for each company's selected "
                                   "(\"Best Method\") method: every year it was tested against, what "
                                   "it predicted, what actually happened, and the resulting error. "
                                   "This is the evidence behind the Backtest MAPE % column in Summary."),
    ]
    for line in lines:
        ws3.append(line)
    ws3.column_dimensions["A"].width = 26
    ws3.column_dimensions["B"].width = 90
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
    export_report(tested, OUTPUT_FILE, TARGET_END_YEAR, REPORT_START_YEAR)
    print(f"\nReport written to: {OUTPUT_FILE}")
