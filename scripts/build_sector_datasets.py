"""Write market-size series that differ in the ways real uploads differ.

The format fixtures vary how a date is spelled; these vary the data itself —
magnitude (percent-share to trillions), shape (flat, booming, collapsing,
COVID-shocked), seasonality (none to a hard December peak), and sign (zeros and
negatives). Sector names are illustrative: what actually matters per sector is
that each series has a different correlation structure, so the Granger scan has
to select a different set of FRED drivers for each one.

Run:
    .venv/bin/python scripts/build_sector_datasets.py --out /tmp/sectors
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

START = "2015-01-31"
PERIODS = 120            # ten years of monthly history


def series(level, growth, seasonal_amplitude, december_lift, noise,
           covid=0.0, covid_recovery=24, break_at=None, break_factor=1.0,
           periods=PERIODS, start=START, freq="ME", seed=0):
    """One synthetic market-size series with a named shape."""
    index = pd.date_range(start, periods=periods, freq=freq)
    rng = np.random.default_rng(seed)
    step = np.arange(periods)

    values = level * (1 + growth) ** (step / 12)
    if seasonal_amplitude:
        cycle = 2 * np.pi * (index.month - 1) / 12
        values = values * (1 + seasonal_amplitude * np.sin(cycle - 1.2))
    if december_lift:
        values = values * np.where(index.month == 12, 1 + december_lift, 1.0)
    if covid:
        months_since = (index.year - 2020) * 12 + (index.month - 3)
        shock = np.where((months_since >= 0) & (months_since < covid_recovery),
                         1 - covid * np.exp(-months_since / 6.0), 1.0)
        values = values * shock
    if break_at is not None:
        values = values * np.where(index >= pd.Timestamp(break_at), break_factor, 1.0)
    if noise:
        values = values * rng.normal(1.0, noise, periods)
    return pd.DataFrame({"Date": index, "Sales": values})


def datasets() -> dict:
    """label -> (frame, rounding, note)."""
    out = {}

    # ── sector shapes, all monthly, each with a different driver profile ──
    out["apparel_specialty"] = (
        series(412_000, 0.045, 0.11, 0.24, 0.018, seed=1), 0,
        "$412M, hard December peak — discretionary, should track sentiment/credit")
    out["grocery_food_retail"] = (
        series(12_400_000, 0.031, 0.025, 0.04, 0.008, seed=2), 0,
        "$12.4B, barely seasonal, steady — staples, should track CPI/wages")
    out["consumer_electronics"] = (
        series(2_380_000, 0.012, 0.09, 0.38, 0.030,
               break_at="2022-07-31", break_factor=0.86, seed=3), 0,
        "$2.38B, Nov/Dec spike, post-2022 step down — should track durable goods orders")
    out["luxury_goods"] = (
        series(86_500, 0.068, 0.07, 0.19, 0.055, seed=4), 0,
        "$86M, high volatility — thin series, a stress test for the fits")
    out["home_improvement"] = (
        series(5_900_000, 0.022, 0.16, -0.08, 0.020,
               covid=-0.22, covid_recovery=30, seed=5), 0,
        "$5.9B, spring peak, COVID BOOM not dip — should track housing starts")
    out["restaurant_foodservice"] = (
        series(3_150_000, 0.028, 0.05, 0.03, 0.022,
               covid=0.58, covid_recovery=28, seed=6), 0,
        "$3.15B, COVID collapse and slow recovery — a real structural break")
    out["ecommerce_pureplay"] = (
        series(1_750_000, 0.145, 0.06, 0.41, 0.025,
               break_at="2020-04-30", break_factor=1.22, seed=7), 0,
        "$1.75B, 14.5% growth + COVID step up — strong trend, weak stationarity")
    out["auto_parts_aftermarket"] = (
        series(880_000, 0.016, 0.03, 0.01, 0.012, seed=8), 0,
        "$880M, nearly flat and nearly aseasonal — the null case for seasonality")
    out["beauty_personal_care"] = (
        series(45_200, 0.052, 0.08, 0.22, 0.019, seed=9), 0,
        "$45M, small absolute values with real seasonality")
    out["department_stores"] = (
        series(6_200_000, -0.038, 0.13, 0.31, 0.024, seed=10), 0,
        "$6.2B in STRUCTURAL DECLINE — negative growth, a direction nothing else tests")

    # ── magnitude, from share-of-market to trillions ──────────────────
    out["scale_market_share_pct"] = (
        series(4.8, 0.021, 0.05, 0.03, 0.020, seed=11), 2,
        "4.8 rising to ~6 — a percentage, two decimal places")
    out["scale_units_thousands"] = (
        series(1_250, 0.034, 0.10, 0.18, 0.022, seed=12), 0,
        "1,250 units — four-figure values")
    out["scale_trillions"] = (
        series(1_240_000_000_000, 0.027, 0.04, 0.06, 0.009, seed=13), 0,
        "$1.24 trillion — tests that nothing overflows or loses precision")
    out["scale_sub_unit"] = (
        series(0.84, 0.019, 0.06, 0.04, 0.025, seed=14), 4,
        "0.84 — values below 1, four decimals")

    # ── signs and degeneracies ────────────────────────────────────────
    flat = series(500_000, 0.0, 0.0, 0.0, 0.0005, seed=15)
    out["edge_almost_constant"] = (flat, 0, "500,000 flat +/- 0.05% — nothing to model")

    swings = series(120_000, 0.03, 0.22, 0.10, 0.04, seed=16)
    swings["Sales"] = swings["Sales"] - 118_000      # straddles zero
    out["edge_crosses_zero"] = (swings, 0,
                                "net change series crossing zero — MAPE is undefined here")

    withzeros = series(64_000, 0.04, 0.12, 0.15, 0.02, seed=17)
    withzeros.loc[withzeros.index[::17], "Sales"] = 0.0
    out["edge_contains_zeros"] = (withzeros, 0, "every 17th period is a true zero")

    negative = series(240_000, 0.02, 0.09, 0.05, 0.02, seed=18)
    negative["Sales"] = -negative["Sales"]
    out["edge_all_negative"] = (negative, 0, "an entirely negative series (net outflow)")

    # ── length, around the documented 24-point minimum ────────────────
    out["length_minimum_24"] = (
        series(310_000, 0.04, 0.10, 0.20, 0.02, periods=24, seed=19), 0,
        "exactly 24 points — the documented SARIMAX floor")
    out["length_below_minimum_18"] = (
        series(310_000, 0.04, 0.10, 0.20, 0.02, periods=18, seed=20), 0,
        "18 points — below the floor, SARIMAX must decline and say so")
    out["length_long_240"] = (
        series(2_100_000, 0.03, 0.09, 0.17, 0.018, periods=240,
               start="2006-01-31", seed=21), 0,
        "240 points, twenty years")

    # ── other frequencies with real sector shapes ─────────────────────
    out["weekly_grocery"] = (
        series(2_900_000, 0.025, 0.04, 0.0, 0.015, periods=260,
               start="2021-01-03", freq="W", seed=22), 0,
        "260 weekly points")
    out["quarterly_apparel"] = (
        series(1_240_000, 0.038, 0.12, 0.22, 0.020, periods=48,
               start="2014-03-31", freq="QE", seed=23), 0,
        "48 quarterly points")
    out["annual_total_retail"] = (
        series(740_000_000, 0.034, 0.0, 0.0, 0.012, periods=28,
               start="1998-12-31", freq="YE", seed=24), 0,
        "28 annual points")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)

    manifest = {}
    for label, (frame, places, note) in datasets().items():
        frame = frame.copy()
        frame["Sales"] = frame["Sales"].round(places)
        path = directory / f"{label}.xlsx"
        frame.to_excel(path, index=False)
        values = frame["Sales"]
        manifest[label] = {
            "rows": int(len(frame)),
            "min": float(values.min()), "max": float(values.max()),
            "mean": float(values.mean()),
            "note": note,
        }
        print(f"  {label:<28} rows={len(frame):<4} "
              f"range {values.min():,.2f} .. {values.max():,.2f}   {note}")

    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nwrote {len(manifest)} datasets to {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
