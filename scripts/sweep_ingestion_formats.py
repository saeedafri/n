"""Read every fixture both ways: their app's loader, then ours.

For each file this reports what reached the models -- usable rows -- and what
the user would have been told. A format their tool reads as zero rows is not a
disagreement about maths; it is a file the data team cannot forecast at all.

Their side runs their own functions (`normalize_dates`, `guess_date_column`,
`guess_value_column`) plus their own load block, imported from their file
rather than retyped: the module is exec'd with a Streamlit stand-in and an
early stop, so the functions are theirs byte for byte.

Run:
    .venv/bin/python scripts/sweep_ingestion_formats.py --fixtures /tmp/fixtures
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "scripts"))
os.environ.setdefault("APP_ENV", "LOCAL")

warnings.filterwarnings("ignore")

import pandas as pd

from data import market_size_forecast_service as svc              # noqa: E402
from run_data_team_app_headless import FakeStreamlit, Stop, THEIR_APP   # noqa: E402


def their_functions() -> dict:
    """Their module's namespace, up to the point it needs an uploaded file.

    With no file the script calls st.stop() in the sidebar, by which point
    every loader function is defined. Nothing is reimplemented here.
    """
    sys.modules["streamlit"] = FakeStreamlit(None, {})
    namespace = {"__name__": "__main__", "__file__": str(THEIR_APP)}
    try:
        exec(compile(THEIR_APP.read_text(), str(THEIR_APP), "exec"), namespace)
    except (Stop, ModuleNotFoundError):
        pass
    except BaseException:                                   # noqa: BLE001
        pass
    missing = [name for name in ("normalize_dates", "guess_date_column",
                                 "guess_value_column", "detect_frequency",
                                 "FREQ_CONFIGS")
               if name not in namespace]
    if missing:
        raise SystemExit(f"could not lift their loaders: missing {missing}")
    return namespace


def read_their_way(theirs: dict, path: Path) -> dict:
    """Their load block, line for line, on one file."""
    try:
        if path.suffix == ".csv":
            # Their uploader accepts xlsx/xls only; a CSV never reaches the loader.
            return {"usable": 0, "note": "rejected: uploader accepts .xlsx/.xls only"}
        raw = pd.read_excel(path)
        raw.columns = [str(c).strip() for c in raw.columns]
        date_col = theirs["guess_date_column"](raw)
        value_col = theirs["guess_value_column"](raw, date_col)
        if date_col is None or value_col is None:
            return {"usable": 0, "note": "refused: no date + numeric value column found"}

        dates = theirs["normalize_dates"](raw[date_col]).dropna()
        if dates.empty:
            return {"usable": 0, "note": "no parseable dates"}
        freq_name = theirs["detect_frequency"](pd.DatetimeIndex(dates))
        config = theirs["FREQ_CONFIGS"][freq_name]

        frame = raw[[date_col, value_col]].rename(
            columns={date_col: "Date", value_col: "Sales"})
        frame["Sales"] = pd.to_numeric(frame["Sales"], errors="coerce")
        frame["Date"] = theirs["normalize_dates"](frame["Date"])
        frame = frame.dropna(subset=["Date"])
        duplicates = int(frame["Date"].duplicated().sum())
        if duplicates:
            frame = frame.groupby("Date", as_index=False)["Sales"].sum()
        frame.set_index("Date", inplace=True)
        frame.index = pd.to_datetime(frame.index)
        frame.sort_index(inplace=True)
        try:
            frame.index.freq = config["freq"]
        except Exception:
            frame = frame.asfreq(config["freq"])
        usable = int(frame["Sales"].notna().sum())
        return {"usable": usable, "freq": freq_name,
                "columns": f"{date_col}/{value_col}",
                "note": "" if usable else "0 usable rows after cleaning"}
    except Exception as exc:
        return {"usable": 0, "note": f"{type(exc).__name__}: {exc}"}


def read_our_way(path: Path) -> dict:
    """The portal's loader on the same file."""
    try:
        handle = io.BytesIO(path.read_bytes())
        handle.name = path.name
        raw = svc.read_workbook(handle)
        date_col = svc.guess_date_column(raw)
        value_col = svc.guess_value_column(raw, date_col)
        if date_col is None or value_col is None:
            return {"usable": 0, "note": "refused: no date + numeric value column found"}
        dates = svc.normalize_dates(raw[date_col]).dropna()
        if dates.empty:
            return {"usable": 0, "note": "no parseable dates"}
        freq_name = svc.detect_frequency(pd.DatetimeIndex(dates))
        loaded = svc.load_series(raw, date_col, value_col, freq_name)
        usable = int(loaded.frame[svc.TARGET_COL].notna().sum())
        notes = []
        if loaded.snapped_to_period_end:
            notes.append(f"snapped {loaded.snapped_to_period_end} to period end")
        if loaded.duplicates_merged:
            notes.append(f"merged {loaded.duplicates_merged} duplicate period(s)")
        if loaded.dropped_rows:
            notes.append(f"dropped {loaded.dropped_rows} unreadable date(s)")
        blanks = int(loaded.frame[svc.TARGET_COL].isna().sum())
        if blanks:
            notes.append(f"{blanks} blank period(s) kept as gaps")
        return {"usable": usable, "freq": freq_name,
                "columns": f"{date_col}/{value_col}", "note": "; ".join(notes)}
    except Exception as exc:
        return {"usable": 0, "note": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True)
    args = parser.parse_args()

    files = sorted(Path(args.fixtures).glob("*.xlsx")) + \
            sorted(Path(args.fixtures).glob("*.csv"))
    if not files:
        print(f"no fixtures in {args.fixtures} — run build_ingestion_fixtures.py first")
        return 2

    theirs = their_functions()

    print("=" * 112)
    print("INGESTION SWEEP — usable rows reaching the models, their loader vs ours")
    print("=" * 112)
    print(f"{'fixture':<32} {'rows':>5} {'theirs':>7} {'ours':>6}  {'freq':<10} verdict")
    print("-" * 112)

    gained = regressed = 0
    rows = []
    for path in files:
        source = pd.read_csv(path) if path.suffix == ".csv" else pd.read_excel(path)
        their_result = read_their_way(theirs, path)
        our_result = read_our_way(path)
        their_usable = their_result["usable"]
        our_usable = our_result["usable"]

        if our_usable > their_usable:
            verdict = "FIXED" if their_usable == 0 else "MORE ROWS"
            gained += 1
        elif our_usable < their_usable:
            verdict = "REGRESSION"
            regressed += 1
        else:
            verdict = "same"
        print(f"{path.name:<32} {len(source):>5} {their_usable:>7} {our_usable:>6}  "
              f"{str(our_result.get('freq', '-')):<10} {verdict}")
        rows.append((path.name, their_result, our_result, verdict))

    print("-" * 112)
    print(f"{gained} file(s) read better by the portal, {regressed} regression(s)")
    print()
    print("messages a user would see")
    print("-" * 112)
    for name, their_result, our_result, _ in rows:
        if their_result["note"] or our_result["note"]:
            print(f"{name}")
            if their_result["note"]:
                print(f"    theirs: {their_result['note']}")
            if our_result["note"]:
                print(f"    ours:   {our_result['note']}")
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
