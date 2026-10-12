"""AI Forecast Assistant — ask questions about the run you are looking at.

A port of the research team's `chatbot.py` from the Sales Forecasting Tool
handover. What makes it more than a generic chat box is theirs: the whole run is
flattened into a text block and injected as the system prompt on every message,
so the model quotes the actual MAPE, order, p-value, changepoint and forecast
period instead of inventing one. Their prompt rules are kept word for word —
exact numbers, cover every test and every model, never invent, and say "Run
analysis first" when there is nothing to talk about.

Two things had to change for the portal:

* Their context builder read the standalone app's session keys (`fc_results`,
  `fc_diagnostics`, `fc_model_detail`). The portal keeps one `outcome` dict, so
  `build_context` is rewritten against that — same fields, different source.
* They call Ollama on localhost:11434. The portal's container cannot host an
  8B model, so the address is an app setting (`OLLAMA_URL`) pointing at wherever
  Ollama actually runs. Nothing else about the call changes, and no other
  provider is used: the portal's OpenAI key belongs to filing extraction and is
  deliberately not touched here.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import streamlit as st

from utils.server_logger import log_structured_error

OLLAMA_BASE = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
BRAND_RED = "#C8102E"
MAX_TURNS = 12          # keep the prompt bounded; the context block is large


APP_DOCUMENTATION = """
PAGE CONTROLS (Market Size Forecasting):
- File format button (top right): shows the spreadsheet layout the page expects.
- Upload: .xlsx, .xls or .csv with one date column and one numeric value column.
- Date column / Value column / Frequency: auto-detected after upload, each
  overridable from a dropdown. Frequency is inferred from the gaps between dates
  (Weekly / Monthly / Quarterly / Annual) and sets the seasonal period.
- A file with unreadable dates is BLOCKED before anything runs; the page lists
  what is wrong and Run analysis is not offered until it is fixed.
- Models: SARIMAX (FRED macro drivers), SARIMA (series only), Prophet (trend and
  seasonality). Any combination.
- Forecast horizon slider: how many periods ahead.
- Max Granger lag slider: the longest lag tested for Granger causality.
- SARIMAX settings: Top N exogenous variables, Max combination size (1 = single
  variables and fastest), FRED API key.
- Prophet settings: Changepoint prior scale (trend flexibility), Seasonality
  prior scale (seasonality strength), Seasonality mode (auto-detect / additive /
  multiplicative).
- Run analysis: fits every selected model in one pass.

SECTIONS AFTER A RUN:
- Diagnostics: record count, date span, mean and standard deviation, ADF and
  KPSS stationarity tests, seasonal strength and the chosen seasonality mode,
  peak and trough period, outliers at |z| > 3, seasonal decomposition.
- SARIMAX: differencing rounds, Granger-significant macro variables with their
  F-statistics and lags, the ranked combination search, the chosen model order,
  walk-forward validation, forecast with confidence bands, residual diagnostics.
- SARIMA: the AIC grid search result, chosen order, walk-forward, forecast,
  residual diagnostics.
- Prophet: seasonality mode, changepoints with dates and deltas, optional
  cross-validation, walk-forward, forecast, residual diagnostics.
- Comparison: every model side by side on MAPE and RMSE with a declared best.
  When the series contains a zero, MAPE is undefined and the ranking uses RMSE.
"""

SYSTEM_TEMPLATE = """Answer questions about forecast results and app features concisely.

RULES:
1. Always quote EXACT numbers from SESSION DATA (MAPE, RMSE, orders, p-values, dates, forecast values).
2. Use **bold** for key metrics.
3. NEVER truncate or summarize — cover EVERY point fully. Do not skip items.
4. For diagnostics questions: cover ALL tests (ADF, KPSS, Ljung-Box, Shapiro-Wilk, Durbin-Watson, seasonality, outliers).
5. For model questions: cover ALL models that were run, not just the best one.
6. For summaries: include data overview, all model results, diagnostics, forecasts.
7. For app/UI questions: clear step-by-step guidance from APP DOCUMENTATION.
8. If context says NO_RESULTS, say "Please click Run analysis first."
9. Never make up numbers — only use what is in SESSION DATA.

APP DOCUMENTATION:
{app_docs}

SESSION DATA:
{context}"""


# ═══════════════════════════════════════════════════════════════════
#  CONTEXT — the run, flattened for the model
# ═══════════════════════════════════════════════════════════════════

def _values(obj) -> list:
    """A plain list out of a Series, an ndarray or a list."""
    if obj is None:
        return []
    if isinstance(obj, pd.Series):
        return list(obj.values)
    if isinstance(obj, (np.ndarray, list, tuple)):
        return list(obj)
    return []


def _dates(obj) -> list:
    if isinstance(obj, pd.Series):
        index = obj.index if not np.issubdtype(obj.dtype, np.datetime64) else obj
        return [pd.Timestamp(d) for d in index]
    if isinstance(obj, (pd.DatetimeIndex, list, tuple, np.ndarray)):
        return [pd.Timestamp(d) for d in obj]
    return []


def build_context(outcome: Optional[Dict[str, Any]]) -> str:
    """Flatten a completed run into the text the model is given every turn.

    Mirrors the research team's `build_context` field for field. Anything the
    model is allowed to state has to appear here, because rule 9 forbids it
    inventing the rest.
    """
    from data import market_size_forecast_service as engine

    if not outcome or not outcome.get("results"):
        return "NO_RESULTS: Tell user to click Run analysis first."

    settings = outcome.get("settings", {})
    diagnostics = outcome.get("diagnostics", {})
    results = outcome.get("results", {})
    freq = outcome.get("freq_name", "")
    lines: List[str] = []

    lines += ["PARAMETERS",
              f"  Models: {', '.join(results)}",
              f"  Horizon: {outcome.get('horizon')} {freq.lower()} periods",
              f"  Frequency: {freq} s={engine.FREQ_CONFIGS.get(freq, {}).get('s')}",
              f"  Seasonality: {diagnostics.get('seasonality_mode')} "
              f"{diagnostics.get('seasonal_strength', float('nan')):.1f}%",
              f"  Date column: {settings.get('date_col')}  "
              f"Value column: {settings.get('value_col')}"]

    if diagnostics:
        period_avg = diagnostics.get("period_avg")
        lines += ["", "DATA",
                  f"  {diagnostics.get('date_start'):%Y-%m-%d} to "
                  f"{diagnostics.get('date_end'):%Y-%m-%d} "
                  f"n={diagnostics.get('total_records')}",
                  f"  Mean={diagnostics.get('mean_sales', 0):,.0f} "
                  f"Std={diagnostics.get('std_sales', 0):,.0f} "
                  f"Min={diagnostics.get('min_sales', 0):,.0f} "
                  f"Max={diagnostics.get('max_sales', 0):,.0f}",
                  f"  ADF p={diagnostics.get('adf_p'):.4f} "
                  f"{'STATIONARY' if diagnostics.get('adf_stationary') else 'NON-STATIONARY'}",
                  f"  KPSS p={diagnostics.get('kpss_p'):.4f} "
                  f"{'STATIONARY' if diagnostics.get('kpss_stationary') else 'NON-STATIONARY'}"]
        if period_avg is not None and len(period_avg):
            lines += [f"  Peak=Month{period_avg.idxmax()} avg={period_avg.max():,.0f}",
                      f"  Trough=Month{period_avg.idxmin()} avg={period_avg.min():,.0f}",
                      "  Monthly: " + " ".join(f"M{m}={v:,.0f}"
                                               for m, v in period_avg.items())]
        outliers = diagnostics.get("outliers")
        count = 0 if outliers is None else len(outliers)
        lines += [f"  Outliers={count}"
                  + ("" if not count else
                     " at " + ", ".join(f"{d:%Y-%m} ({v:,.0f})"
                                        for d, v in list(outliers.items())[:8]))]

    loaded = outcome.get("loaded")
    if loaded is not None:
        notes = []
        if getattr(loaded, "duplicates_merged", 0):
            notes.append(f"{loaded.duplicates_merged} duplicate period(s) summed")
        if getattr(loaded, "snapped_to_period_end", 0):
            notes.append(f"{loaded.snapped_to_period_end} date(s) snapped to period end")
        blanks = int(loaded.frame[engine.TARGET_COL].isna().sum())
        if blanks:
            notes.append(f"{blanks} blank period(s)")
        if notes:
            lines += ["", "INGESTION: " + "; ".join(notes)]

    lines += ["", "RESULTS — USE EXACT NUMBERS:"]
    for name, result in results.items():
        mape, rmse = result.get("mape"), result.get("rmse")
        mape_text = "n/a (series contains zero)" if mape is None or not np.isfinite(mape) \
            else f"{mape:.2f}%"
        accuracy = "" if mape is None or not np.isfinite(mape) else \
            f" Accuracy={100 - mape:.1f}%"
        lines += [f"  {name}: MAPE={mape_text} RMSE={rmse:,.0f}{accuracy}"]
        lines += [f"    specification={result.get('label')}"]

        if name == "SARIMAX":
            lines += [f"    order={result.get('order')} "
                      f"seasonal={result.get('seasonal_order')} "
                      f"vars={result.get('predvars')}",
                      f"    real exog periods={result.get('real_exog_periods')} "
                      f"(the rest fall back to each variable's historical mean)"]
            granger = outcome.get("granger")
            if granger is not None and len(granger):
                top = granger.sort_values("ftest_stat", ascending=False).head(5)
                lines += ["    top Granger variables:"]
                lines += [f"      {r.xlabel} F={r.ftest_stat:.2f} p={r.ftest_pval:.4g}"
                          for r in top.itertuples()]
            if outcome.get("sarimax_diffs") is not None:
                lines += [f"    stationary after {outcome['sarimax_diffs']} difference(s)"]
        elif name == "SARIMA":
            lines += [f"    order={result.get('order')} "
                      f"seasonal={result.get('seasonal_order')} "
                      f"AIC={result.get('best_aic'):.2f}"]
        elif name == "Prophet":
            lines += [f"    mode={result.get('seasonality_mode')} "
                      f"changepoints={len(result.get('changepoints', []))}"
                      f"/{result.get('total_changepoints')}"]
            if result.get("cv_requested") and np.isfinite(result.get("cv_mape", np.nan)):
                lines += [f"    CV_with_regressor={result['cv_mape']:.2f}% "
                          f"CV_baseline={result['cv_baseline_mape']:.2f}%"]
            for cp in result.get("changepoints", [])[:12]:
                lines += [f"    CP {cp['date']}: {cp['delta']:+.4f} {cp['direction']}"]

        residuals = result.get("residuals")
        if residuals is not None:
            checks = engine.residual_diagnostics(residuals)
            if not checks.get("too_short"):
                # One test per line, each with its own verdict spelled out. Packed
                # onto a single line, an 8B model read the four p-values and four
                # verdicts out of order and reported a PASS as a FAIL.
                lines += [
                    f"    Ljung-Box at lag {checks['lags'][0]}: p={checks['lb_short']:.4f} "
                    f"-> {'PASS (no autocorrelation)' if checks['lb_short'] > 0.05 else 'FAIL (autocorrelation left)'}",
                    f"    Ljung-Box at lag {checks['lags'][-1]}: p={checks['lb_long']:.4f} "
                    f"-> {'PASS (no autocorrelation)' if checks['lb_long'] > 0.05 else 'FAIL (autocorrelation left)'}",
                    f"    Shapiro-Wilk: p={checks['shapiro_p']:.4f} "
                    f"-> {'PASS (residuals normal)' if checks['shapiro_p'] > 0.05 else 'FAIL (residuals not normal)'}",
                    f"    Durbin-Watson: {checks['durbin_watson']:.4f} "
                    f"-> {'PASS (no serial correlation)' if 1.5 < checks['durbin_watson'] < 2.5 else 'FAIL (serial correlation)'}",
                    f"    Residual verdict: "
                    f"{'all four tests PASS' if not checks['failures'] else 'FAILED: ' + ', '.join(checks['failures'])}",
                ]

        actual = _values(result.get("wf_actual"))
        predicted = _values(result.get("wf_predicted"))
        when = _dates(result.get("wf_dates")) or _dates(result.get("wf_actual"))
        for index, (a, p) in enumerate(zip(actual, predicted)):
            label = when[index].strftime("%Y-%m") if index < len(when) else f"t{index + 1}"
            error = (a - p) / a * 100 if a else 0.0
            lines += [f"    WF {label}: actual={a:,.0f} pred={p:,.0f} err={error:+.1f}%"]

        forecast = result.get("forecast")
        if forecast is not None and len(forecast):
            for when_, row in forecast.iterrows():
                lines += [f"    FC {pd.Timestamp(when_):%Y-%m}: {row['mean']:,.0f} "
                          f"CI={row['mean_ci_lower']:,.0f}-{row['mean_ci_upper']:,.0f}"]

    usable = {n: r for n, r in results.items()
              if r.get("mape") is not None and np.isfinite(r.get("mape"))}
    if usable:
        winner = min(usable, key=lambda m: usable[m]["mape"])
        lines += [f"  BEST={winner} {usable[winner]['mape']:.2f}% MAPE"]
    elif results:
        winner = min(results, key=lambda m: results[m].get("rmse", float("inf")))
        lines += [f"  BEST={winner} {results[winner]['rmse']:,.0f} RMSE "
                  f"(MAPE undefined — the series contains a zero)"]

    for section, note in outcome.get("notes", []):
        lines += [f"  NOTE [{section}]: {note}"]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  OLLAMA
# ═══════════════════════════════════════════════════════════════════

def _ollama_models() -> List[str]:
    try:
        response = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=1.5)
        if response.status_code == 200:
            return [m["name"] for m in response.json().get("models", [])]
    except Exception:
        pass
    return []


def available_models() -> List[str]:
    """The models Ollama reports, or an empty list when it cannot be reached.

    Ollama is the only backend. The research team built against it, and the
    portal's OpenAI key is reserved for filing extraction, so the assistant
    simply says it is unavailable rather than quietly answering from somewhere
    else.

    `OLLAMA_MODEL` names the one to offer first — the research team's default
    is llama3.1:8b, but a host may carry several and the picker should open on
    the intended one rather than on whatever Ollama happens to list first.
    """
    models = _ollama_models()
    if OLLAMA_MODEL in models:
        models = [OLLAMA_MODEL] + [m for m in models if m != OLLAMA_MODEL]
    return models


def _stream_ollama(messages: List[Dict[str, str]], model: str) -> Iterator[str]:
    payload = {"model": model, "messages": messages, "stream": True,
               "options": {"num_predict": 2048, "temperature": 0.3}}
    with requests.post(f"{OLLAMA_BASE}/api/chat", json=payload,
                       stream=True, timeout=180) as response:
        if response.status_code != 200:
            yield f"[Ollama error {response.status_code}]"
            return
        for line in response.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                yield delta
            if chunk.get("done"):
                break


def stream_reply(messages: List[Dict[str, str]], model: str) -> Iterator[str]:
    """Stream one answer from Ollama, degrading to a message on any failure."""
    try:
        yield from _stream_ollama(messages, model)
    except Exception as exc:
        log_structured_error(exc, page="market_size_forecasting",
                             component="forecast_assistant.stream_reply",
                             operation="stream", context=f"{OLLAMA_BASE}/{model}")
        yield (f"\n\nThe assistant could not reach Ollama at {OLLAMA_BASE}: {exc}. "
               "The forecast above is unaffected.")


# ═══════════════════════════════════════════════════════════════════
#  UI
# ═══════════════════════════════════════════════════════════════════

_CSS = f"""
<style>
[data-testid="stChatInput"] > div {{
  border:1.5px solid #E5E7EB !important; border-radius:16px !important;
  background:#FFFFFF !important; box-shadow:0 2px 12px rgba(0,0,0,0.07) !important;
}}
[data-testid="stChatInput"] > div:focus-within {{ border-color:{BRAND_RED} !important; }}
[data-testid="stChatInput"] button {{ background:{BRAND_RED} !important; border-radius:10px !important; }}
.fa-bubble {{
  display:flex; justify-content:flex-end; margin:8px 0 12px;
}}
.fa-bubble > div {{
  background:{BRAND_RED}; color:#fff; padding:10px 16px;
  border-radius:18px 18px 4px 18px; font-size:14px; line-height:1.6; max-width:72%;
}}
.fa-rule {{ border:none; border-top:1px solid #F3F4F6; margin:6px 0 10px; }}
</style>
"""


def _chips(results: Dict[str, Any]) -> str:
    out = ""
    for name, result in results.items():
        mape = result.get("mape")
        if mape is None or not np.isfinite(mape):
            background, text, border = "#F1F5F9", "#475569", "#E2E8F0"
            label = f"{name} · RMSE {result.get('rmse', 0):,.0f}"
        else:
            if mape < 2:
                background, text, border = "#D1FAE5", "#065F46", "#6EE7B7"
            elif mape < 5:
                background, text, border = "#FEF3C7", "#92400E", "#FCD34D"
            else:
                background, text, border = "#FEE2E2", "#991B1B", "#FCA5A5"
            label = f"{name} · {mape:.2f}%"
        out += (f'<span style="display:inline-flex;align-items:center;padding:3px 10px;'
                f'border-radius:20px;font-size:11px;font-weight:600;margin:2px;'
                f'background:{background};color:{text};border:1px solid {border}">'
                f'{label}</span>')
    return out


def _user_bubble(text: str) -> None:
    st.markdown(f'<div class="fa-bubble"><div>{text}</div></div>', unsafe_allow_html=True)


def render_assistant(outcome: Optional[Dict[str, Any]], key: str = "msf_") -> None:
    """The assistant panel, rendered at the foot of the forecasting page."""
    state_key = f"{key}chat"
    if state_key not in st.session_state:
        st.session_state[state_key] = []

    st.markdown(_CSS, unsafe_allow_html=True)
    models = available_models()
    results = (outcome or {}).get("results", {})

    served_by = f"Ollama · {models[0]}" if models else "Ollama unreachable"
    chips = _chips(results)
    st.markdown(
        f'<div style="background:#FFFFFF;border:1px solid #E8EAED;border-radius:14px;'
        f'padding:16px 20px;margin-bottom:12px;box-shadow:0 2px 8px rgba(0,0,0,0.05)">'
        f'<div style="display:flex;justify-content:space-between;align-items:center">'
        f'<div style="font-size:12px;color:#9CA3AF">Answers come from the run above — '
        f'every figure it quotes is on this page.</div>'
        f'<span style="background:#F1F5F9;border:1px solid #E2E8F0;border-radius:6px;'
        f'padding:3px 10px;font-size:11px;font-weight:600;color:#475569">{served_by}</span>'
        f'</div>'
        + (f'<div style="margin-top:8px;display:flex;flex-wrap:wrap">{chips}</div>'
           if chips else
           '<div style="margin-top:6px;font-size:12px;color:#94A3B8">'
           'Run analysis to load the results into the assistant.</div>')
        + '</div>', unsafe_allow_html=True)

    if not models:
        st.info(
            f"The assistant cannot reach Ollama at `{OLLAMA_BASE}`, so it is off. "
            "**Everything else on this page is unaffected** — the forecast does "
            "not use it.\n\n"
            "Locally: `brew services start ollama` then "
            "`ollama pull llama3.1:8b`.\n\n"
            "On a server: point the `OLLAMA_URL` app setting at the host running "
            "Ollama.")
        return

    for message in st.session_state[state_key]:
        if message["role"] == "user":
            _user_bubble(message["content"])
        else:
            st.markdown(message["content"])
            st.markdown('<hr class="fa-rule">', unsafe_allow_html=True)

    picker, clear = st.columns([6, 1])
    chosen = picker.selectbox("Model", models, label_visibility="collapsed",
                              key=f"{key}chat_model")
    if clear.button("Clear", key=f"{key}chat_clear", use_container_width=True):
        st.session_state[state_key] = []
        st.rerun()

    prompt = st.chat_input("Ask anything about your forecast…", key=f"{key}chat_input")
    if not prompt:
        return

    st.session_state[state_key].append({"role": "user", "content": prompt})
    _user_bubble(prompt)

    system = {"role": "system",
              "content": SYSTEM_TEMPLATE.format(context=build_context(outcome),
                                                app_docs=APP_DOCUMENTATION)}
    history = [{"role": m["role"], "content": m["content"]}
               for m in st.session_state[state_key][-MAX_TURNS:]]

    holder = st.empty()
    answer = ""
    for piece in stream_reply([system] + history, chosen):
        answer += piece
        holder.markdown(answer + "▌")
    holder.markdown(answer)
    st.markdown('<hr class="fa-rule">', unsafe_allow_html=True)
    st.session_state[state_key].append({"role": "assistant", "content": answer})
