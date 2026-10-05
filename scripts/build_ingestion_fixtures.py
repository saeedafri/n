"""Write one workbook per date/number format the portal has to accept.

The list is their handover documentation's accepted-formats table (section 5.1)
plus the shapes the data team's own files actually arrive in -- apostrophe
years, period-start dates, formatted numbers, gaps, duplicates, header rows,
no column-name hints. Same underlying series in every file, so any difference
in the forecast is the reader's fault, not the data's.

Run:
    .venv/bin/python scripts/build_ingestion_fixtures.py --out /tmp/fixtures
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def monthly_series(periods: int = 114, start: str = "2017-01-31") -> pd.DataFrame:
    """A seasonal, trending monthly series -- shaped like the real uploads."""
    index = pd.date_range(start, periods=periods, freq="ME")
    trend = np.linspace(240_000, 460_000, periods)
    season = 1 + 0.11 * np.sin(np.arange(periods) / 12 * 2 * np.pi - 1.1)
    december = np.where(index.month == 12, 1.09, 1.0)
    rng = np.random.default_rng(20260101)
    values = trend * season * december * rng.normal(1.0, 0.012, periods)
    return pd.DataFrame({"Date": index, "Sales": values.round(0)})


def fixtures(base: pd.DataFrame) -> dict:
    """label -> (frame, kwargs for to_excel/to_csv, note)."""
    index = pd.DatetimeIndex(base["Date"])
    sales = base["Sales"]
    out = {}

    # ── their documented formats (doc section 5.1) ───────────────────
    out["01_full_date_month_end"] = (base.copy(), {},
                                     "2017-01-31 -- the documented 'full date'")
    out["02_full_date_month_start"] = (
        pd.DataFrame({"Date": index.to_period("M").to_timestamp(how="start"),
                      "Sales": sales}), {},
        "2017-01-01 -- same periods labelled at the start")
    out["03_month_year_dashed"] = (
        pd.DataFrame({"Date": index.strftime("%Y-%m"), "Sales": sales}), {},
        "'2020-01' -- documented 'month + year'")
    out["04_month_year_name"] = (
        pd.DataFrame({"Date": index.strftime("%b %Y"), "Sales": sales}), {},
        "'Jan 2020' -- documented 'month + year'")
    out["05_quarter_q_first"] = (
        pd.DataFrame({"Date": [f"Q{d.quarter} {d.year}" for d in
                               index.to_period("Q").drop_duplicates().to_timestamp(how="end")],
                      "Sales": sales.groupby(index.to_period("Q")).sum().values}), {},
        "'Q1 2020' -- documented quarter format")
    out["06_quarter_year_first"] = (
        pd.DataFrame({"Date": [f"{d.year}Q{d.quarter}" for d in
                               index.to_period("Q").drop_duplicates().to_timestamp(how="end")],
                      "Sales": sales.groupby(index.to_period("Q")).sum().values}), {},
        "'2020Q1' -- documented quarter format")
    out["07_quarter_hyphen"] = (
        pd.DataFrame({"Date": [f"{d.year}-Q{d.quarter}" for d in
                               index.to_period("Q").drop_duplicates().to_timestamp(how="end")],
                      "Sales": sales.groupby(index.to_period("Q")).sum().values}), {},
        "'2020-Q1' -- documented quarter format")
    out["08_year_only"] = (
        pd.DataFrame({"Date": sorted(set(index.year)),
                      "Sales": sales.groupby(index.year).sum().values}), {},
        "2020 -- documented 'year only'")

    # ── shapes the data team's files actually arrive in ──────────────
    out["09_apostrophe_year"] = (
        pd.DataFrame({"Date": [f"{MONTHS[d.month - 1]} '{d.strftime('%y')}" for d in index],
                      "Sales": sales}), {},
        "\"Dec '22\" -- straight apostrophe")
    out["10_curly_apostrophe"] = (
        pd.DataFrame({"Date": [f"{MONTHS[d.month - 1]}’{d.strftime('%y')}" for d in index],
                      "Sales": sales}), {},
        "'Jan’23' -- Word's curly apostrophe")
    out["11_month_dash_year2"] = (
        pd.DataFrame({"Date": [f"{MONTHS[d.month - 1]}-{d.strftime('%y')}" for d in index],
                      "Sales": sales}), {},
        "'Dec-22' -- Excel's own short display")
    out["12_fred_style_period"] = (
        pd.DataFrame({"Date": index.strftime("%YM%m"), "Sales": sales}), {},
        "'2020M01' -- how FRED and BLS label months")
    out["13_formatted_numbers"] = (
        pd.DataFrame({"Date": index.strftime("%Y-%m-%d"),
                      "Sales": [f"${v:,.0f}" for v in sales]}), {},
        "'$243,826' -- currency and thousands separators")
    out["14_accounting_negatives"] = (
        pd.DataFrame({"Date": index.strftime("%Y-%m-%d"),
                      "Sales": [f"({abs(v):,.0f})" if i % 37 == 0 else f"{v:,.0f}"
                                for i, v in enumerate(sales)]}), {},
        "'(87)' -- accounting negatives")
    gapped = base.drop(base.index[[13, 25, 37, 49]]).reset_index(drop=True)
    out["15_missing_periods"] = (gapped, {},
                                 "four months absent -- the real file's shape")
    duplicated = pd.concat([base, base.iloc[[5, 6]]]).sort_values("Date").reset_index(drop=True)
    out["16_duplicate_dates"] = (duplicated, {},
                                 "two periods repeated -- documented as combined")
    out["17_no_name_hints"] = (
        base.rename(columns={"Date": "Column A", "Sales": "Column B"}), {},
        "no 'date'/'sales' in either header")
    out["18_extra_columns"] = (
        base.assign(Region="National", Notes="", Units="USD"), {},
        "three decoration columns alongside the two real ones")
    out["19_weekly"] = (
        pd.DataFrame({"Date": pd.date_range("2021-01-03", periods=250, freq="W"),
                      "Sales": (np.linspace(48_000, 92_000, 250)
                                * (1 + 0.08 * np.sin(np.arange(250) / 52 * 2 * np.pi))
                                ).round(0)}), {},
        "250 weekly points")
    out["20_annual"] = (
        pd.DataFrame({"Date": pd.date_range("1995-12-31", periods=30, freq="YE"),
                      "Sales": np.linspace(1_100_000, 3_900_000, 30).round(0)}), {},
        "30 annual points")
    out["21_csv"] = (base.copy(), {"csv": True}, "the same series as .csv")
    out["22_unreadable_dates"] = (
        pd.DataFrame({"Date": ["sometime in 2020"] * 20 + list(index.strftime("%Y-%m-%d")[20:]),
                      "Sales": sales}), {},
        "20 rows of prose where a date belongs")
    out["23_text_in_values"] = (
        pd.DataFrame({"Date": index.strftime("%Y-%m-%d"),
                      "Sales": ["n/a" if i % 29 == 0 else v for i, v in enumerate(sales)]}), {},
        "'n/a' scattered through the value column")
    out["24_too_short"] = (base.iloc[:14].copy(), {},
                           "14 points -- under the documented 24 minimum")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)

    base = monthly_series()
    written = []
    for label, (frame, options, note) in fixtures(base).items():
        if options.get("csv"):
            path = directory / f"{label}.csv"
            frame.to_csv(path, index=False)
        else:
            path = directory / f"{label}.xlsx"
            frame.to_excel(path, index=False)
        written.append((path.name, len(frame), note))

    print(f"wrote {len(written)} fixtures to {directory}")
    for name, rows, note in written:
        print(f"  {name:<34} rows={rows:<5} {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
