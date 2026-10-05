"""Execute the data team's original Streamlit app headlessly, unmodified.

Why this exists. Comparing our port to their recorded `results.json` mixes two
different questions: did the port change the maths, and did the newer
statsmodels/prophet move the last digits? This separates them. It runs THEIR
app.py -- the exact bytes they handed over, no edits -- against the same
library versions the portal runs, by substituting a Streamlit stand-in that
returns each widget's default and swallows every draw call.

The script exec()s their file, then pickles the local variables that hold the
numbers: SARIMA order/AIC/forecast, Prophet changepoints/forecast, the
diagnostics, the FRED pull. `scripts/compare_port_to_data_team.py` reads that
pickle next to our engine's output.

Nothing in their file is rewritten. The only thing injected is the environment:
which file is "uploaded", and what each sidebar widget returns.

Run:
    .venv/bin/python scripts/run_data_team_app_headless.py \
        --excel /path/to/TestingDataset.xlsx --horizon 60 --out /tmp/theirs.pkl
"""

from __future__ import annotations

import argparse
import io
import pickle
import sys
import types
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

warnings.filterwarnings("ignore")

THEIR_APP = Path("/Users/mohdsaeedafri/Downloads/Salesforecastingtool/app_New.py")

# The ONLY edits --compat makes to their file, printed on every run so the diff
# is never taken on trust. statsmodels 0.15 dropped `verbose` from
# grangercausalitytests; their call still passes it, so on the portal's
# statsmodels every variable raises TypeError inside their own
# `except Exception: pass` and the Granger scan silently returns nothing. With
# the argument gone their SARIMAX runs again and can be compared to the port.
COMPAT_PATCHES = [
    ("maxlag=max_lag, verbose=False)", "maxlag=max_lag)"),
]


class Stop(Exception):
    """Raised by the stand-in st.stop()."""


class Box:
    """Stands in for anything Streamlit hands back that gets drawn on.

    Columns, tabs, expanders, popovers, spinners, progress bars and st.empty()
    all end up here: every attribute is a no-op that returns another Box, and
    it works as a context manager so `with tabs[0]:` runs its body.
    """

    def __init__(self, label=""):
        self.label = label

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __call__(self, *a, **k):
        return Box()

    def __getattr__(self, name):
        return Box(name)

    def __iter__(self):
        return iter(())


class State(dict):
    """st.session_state: a dict that also answers to attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class FakeStreamlit(types.ModuleType):
    """Returns each widget's default and draws nothing.

    A widget's default is whatever their own code passed as the default, so the
    run that comes out is the run a user gets on first click with the sidebar
    untouched -- except for the handful of values in `overrides`, which stand in
    for the file the user uploads and the sliders they move.
    """

    def __init__(self, uploaded, overrides):
        super().__init__("streamlit")
        self.session_state = State()
        self._uploaded = uploaded
        self._overrides = overrides
        self.sidebar = Box("sidebar")
        self.column_config = Box("column_config")

    # ── draw calls: ignored ──────────────────────────────────────────
    def __getattr__(self, name):
        return Box(name)

    def set_page_config(self, *a, **k):
        return None

    def markdown(self, *a, **k):
        return None

    def columns(self, spec, **k):
        count = spec if isinstance(spec, int) else len(spec)
        return [Box(f"col{i}") for i in range(count)]

    def tabs(self, labels, **k):
        return [Box(str(label)) for label in labels]

    def spinner(self, *a, **k):
        return Box("spinner")

    def expander(self, *a, **k):
        return Box("expander")

    def popover(self, *a, **k):
        return Box("popover")

    def progress(self, *a, **k):
        return Box("progress")

    def empty(self, *a, **k):
        return Box("empty")

    def stop(self):
        raise Stop()

    # ── widgets: return the default, or our override ─────────────────
    def file_uploader(self, label, *a, **k):
        return self._uploaded

    def _default(self, label, fallback):
        return self._overrides.get(label, fallback)

    def selectbox(self, label, options, index=0, **k):
        options = list(options)
        if label in self._overrides:
            return self._overrides[label]
        if index is None:
            index = 0
        return options[index] if options else None

    def slider(self, label, min_value=None, max_value=None, value=None, **k):
        return self._default(label, value if value is not None else min_value)

    def number_input(self, label, min_value=None, max_value=None, value=None, **k):
        if label in self._overrides:
            return self._overrides[label]
        key = k.get("key")
        if key is not None and key in self.session_state:
            return self.session_state[key]
        return value if value is not None else (min_value or 0)

    def checkbox(self, label, value=False, **k):
        return self._default(label, value)

    def text_input(self, label, value="", **k):
        return self._default(label, value)

    def button(self, label, *a, **k):
        return self._default(label, False)

    def download_button(self, *a, **k):
        return False

    def radio(self, label, options, index=0, **k):
        options = list(options)
        return self._overrides.get(label, options[index] if options else None)

    def multiselect(self, label, options, default=None, **k):
        return self._default(label, default or [])

    def toggle(self, label, value=False, **k):
        return self._default(label, value)


def capture(excel: Path, horizon: int, models: dict, fred_key: str,
            max_combo: int, max_exog: int, freq: str | None,
            compat: bool = False) -> dict:
    """Run their app once and return the variables holding the numbers."""
    overrides = {
        "Run Analysis": True,
        "SARIMAX  (FRED exog)": models.get("SARIMAX", False),
        "SARIMA": models.get("SARIMA", True),
        "Prophet": models.get("Prophet", True),
        "FRED API Key": fred_key,
        "Max combo size": max_combo,
        "Top N exog variables": max_exog,
    }
    for unit in ("weeks", "months", "quarters", "years"):
        overrides[f"Forecast horizon ({unit})"] = horizon
    if freq:
        overrides["Frequency (auto-detected — override if wrong)"] = freq

    # A Streamlit UploadedFile is a named, seekable buffer; their code calls
    # both .seek(0) and read_excel on it, so hand it the same shape.
    class Upload(io.BytesIO):
        name = excel.name

    handle = Upload(excel.read_bytes())
    fake = FakeStreamlit(handle, overrides)
    sys.modules["streamlit"] = fake

    # Their app writes config.txt next to itself; run from its own directory so
    # the FRED key file and any relative path behave exactly as it expects.
    source = THEIR_APP.read_text()
    if compat:
        for before, after in COMPAT_PATCHES:
            if before not in source:
                raise SystemExit(f"compat patch no longer applies: {before!r}")
            source = source.replace(before, after)
            print(f"compat patch: {before!r} -> {after!r}")
    namespace = {"__name__": "__main__", "__file__": str(THEIR_APP)}
    stopped = False
    crash = None
    try:
        exec(compile(source, str(THEIR_APP), "exec"), namespace)
    except Stop:
        stopped = True
    except ModuleNotFoundError as exc:
        # Their final line imports the chatbot panel, which is not part of the
        # forecasting maths and is not ported. Everything above it has run.
        if exc.name != "chatbot":
            raise
    except BaseException as exc:                        # noqa: BLE001
        # An unhandled error IS the result for an ingestion test: it is what a
        # user sees when their file reaches a code path that cannot read it.
        crash = f"{type(exc).__name__}: {exc}"

    def get(name):
        return namespace.get(name)

    captured = {
        "ran_to_end": "results" in namespace,
        "stopped_early": stopped,
        "crash": crash,
        "dfsales_rows": int(len(get("dfsales"))) if get("dfsales") is not None else None,
        "usable_rows": int(get("sales_series").notna().sum())
                       if get("sales_series") is not None else None,
        "freq": get("freq_override"),
        "pred_periods": get("pred_periods"),
        "seasonality_mode": get("SEASONALITY_MODE"),
        "seasonal_strength": get("seasonal_strength"),
        "sales_index": list(get("sales_series").index) if get("sales_series") is not None else None,
        "sales_values": list(get("sales_series").values) if get("sales_series") is not None else None,
        "adf_p": get("adf_p"),
        "kpss_p": get("kpss_p"),
        "monthly_avg": get("monthly_avg"),
        "fred_status": get("fred_status"),
        # their raw monthly panel, so the port can be fed the SAME macro data
        # and the comparison measures code rather than two FRED pulls
        "fred_monthly": get("fred_monthly"),
        "model_frame": get("df"),
        "model_frame_columns": list(get("df").columns) if get("df") is not None else None,
        "results": {},
    }

    if get("SM_ORDER") is not None:
        captured["sarima"] = {
            "order": get("SM_ORDER"),
            "seasonal_order": get("SM_S_ORDER"),
            "best_aic": get("best_aic"),
            "mape": get("sm_mape"),
            "rmse": get("sm_rmse"),
            "wf_predicted": list(get("sm_pred").values) if get("sm_pred") is not None else None,
            "wf_actual": list(get("sm_test").values) if get("sm_test") is not None else None,
            "forecast": get("sm_fc"),
            "resid": get("sm_fit").resid if get("sm_fit") is not None else None,
        }

    if get("pb_fc") is not None:
        captured["prophet"] = {
            "mape": get("pb_mape"),
            "rmse": get("pb_rmse"),
            "cv_mape": get("pb_cv_mape"),
            "cv_baseline": get("pb_base_mape"),
            "forecast": get("pb_fc")[["ds", "yhat", "yhat_lower", "yhat_upper"]],
            "deltas": get("deltas"),
            "changepoints": list(get("m_prophet").changepoints) if get("m_prophet") is not None else None,
            "resid": get("pb_resid"),
            "wf_predicted": list(get("pb_fc_wf")["yhat"].values) if get("pb_fc_wf") is not None else None,
        }

    if get("grangerdf") is not None:
        res_df = get("res_df")
        captured["sarimax"] = {
            "granger": get("grangerdf"),
            "exog_ftest": get("exog_ftest"),
            "diffs": get("diffs"),
            "model_frame_rows": int(len(get("df").dropna())) if get("df") is not None else None,
            # the fitted SARIMAX objects do not pickle usefully; keep the numbers
            "combo_table": (res_df[["predvars", "rmse", "mape"]]
                            if res_df is not None and not res_df.empty else res_df),
            "predvars": get("predvars"),
            "order": get("sxorder"),
            "seasonal_order": get("sxsorder"),
            "mape": get("sx_mape"),
            "rmse": get("sx_rmse"),
            "forecast": get("sx_fc") if get("sx_fc") is not None else get("ff_sx"),
            "fred_status": get("fred_status"),
        # their raw monthly panel, so the port can be fed the SAME macro data
        # and the comparison measures code rather than two FRED pulls
        "fred_monthly": get("fred_monthly"),
        "model_frame": get("df"),
        }

    results = get("results") or {}
    captured["results"] = {
        name: {k: v for k, v in payload.items() if k in
               ("mape", "rmse", "fc_mean", "fc_lower", "fc_upper", "label")}
        for name, payload in results.items()
    }
    return captured


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--excel", required=True)
    parser.add_argument("--horizon", type=int, default=60)
    parser.add_argument("--out", required=True)
    parser.add_argument("--models", default="SARIMA,Prophet")
    parser.add_argument("--fred-key", default="")
    parser.add_argument("--max-combo", type=int, default=1)
    parser.add_argument("--max-exog", type=int, default=10)
    parser.add_argument("--freq", default=None)
    parser.add_argument("--compat", action="store_true",
                        help="apply COMPAT_PATCHES so their Granger scan runs on "
                             "statsmodels 0.15; every patch is printed")
    args = parser.parse_args()

    wanted = {name.strip() for name in args.models.split(",") if name.strip()}
    models = {name: name in wanted for name in ("SARIMAX", "SARIMA", "Prophet")}

    captured = capture(Path(args.excel), args.horizon, models, args.fred_key,
                       args.max_combo, args.max_exog, args.freq, args.compat)
    Path(args.out).write_bytes(pickle.dumps(captured))
    print(f"captured their run -> {args.out}")
    print(f"  ran_to_end={captured['ran_to_end']} freq={captured['freq']} "
          f"horizon={captured['pred_periods']}")
    for name in ("sarima", "prophet", "sarimax"):
        if name in captured:
            print(f"  {name}: present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
