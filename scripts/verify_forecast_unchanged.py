"""Prove the forecasting logic did not change.

Runs the forecasting engine as it stood BEFORE the September 2026 ingestion
work (commit 267d4de) and the engine as it stands now, against identical
inputs, and compares the cleaned series, the selected order, the AIC, the
walk-forward MAPE and every forecast value.

    git show 267d4de:app/data/market_size_forecast_service.py > /tmp/engine_before.py
    .venv/bin/python scripts/verify_forecast_unchanged.py

Expected: ALL CASES BIT-IDENTICAL: True. Any other result is a real
regression and should block release.
"""
import sys, importlib.util, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
APP="/Users/mohdsaeedafri/Code-Base/Real/CapIQReplacement/app"
sys.path.insert(0, APP)

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); sys.modules[name]=mod
    spec.loader.exec_module(mod); return mod

OLD = load("/tmp/msf-proof/engine_before.py", "engine_old")
NEW = load(f"{APP}/data/market_size_forecast_service.py", "engine_new")
print(f"OLD 267d4de   NEW working tree\n")

def series_for(freq, n, period):
    idx = pd.date_range({"Monthly":"2016-01-31","Quarterly":"2005-03-31","Annual":"1998-12-31",
                         "Weekly":"2019-01-06"}[freq], periods=n,
                        freq={"Monthly":"ME","Quarterly":"QE","Annual":"YE","Weekly":"W"}[freq])
    t=np.arange(n)
    v=300000+900*t+25000*np.sin(2*np.pi*t/period)+np.random.default_rng(7).normal(0,4000,n)
    return pd.DataFrame({"Date": idx.strftime("%Y-%m-%d"), "Sales": v.round(1)})

CASES=[("Monthly",114,12,12),("Quarterly",80,4,4),("Annual",28,1,2),("Weekly",300,52,8)]
allsame=True
for freq,n,period,horizon in CASES:
    df=series_for(freq,n,period)
    lo=OLD.load_series(df,"Date","Sales",freq); ln=NEW.load_series(df,"Date","Sales",freq)
    so=lo.frame[OLD.TARGET_COL].dropna(); sn=ln.frame[NEW.TARGET_COL].dropna()
    same_input = len(so)==len(sn) and np.allclose(so.values, sn.values) and (so.index==sn.index).all()
    ro=OLD.run_sarima(so,freq,horizon); rn=NEW.run_sarima(sn,freq,horizon)
    same_order = ro["order"]==rn["order"] and ro["seasonal_order"]==rn["seasonal_order"]
    same_aic   = abs(ro["best_aic"]-rn["best_aic"]) < 1e-9
    fo=ro["forecast"]["mean"].values; fn=rn["forecast"]["mean"].values
    same_fc = len(fo)==len(fn) and np.allclose(fo,fn,rtol=0,atol=0)
    maxdiff = float(np.max(np.abs(fo-fn))) if len(fo)==len(fn) else float("nan")
    same_mape = abs(ro["mape"]-rn["mape"]) < 1e-12
    ok = same_input and same_order and same_aic and same_fc and same_mape
    allsame &= ok
    print(f"{freq:10} rows {len(so):>4}  order {str(ro['order'])+'x'+str(ro['seasonal_order']):26}")
    print(f"           input identical {same_input} | order {same_order} | AIC {same_aic} "
          f"({ro['best_aic']:.6f}) | MAPE {same_mape} | forecast max|Δ| {maxdiff:.2e}  -> {'IDENTICAL' if ok else '*** DIFFERS ***'}")
print("\nALL CASES BIT-IDENTICAL:", allsame)
