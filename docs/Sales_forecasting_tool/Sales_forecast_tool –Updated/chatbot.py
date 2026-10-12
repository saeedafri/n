"""
chatbot.py — AI Forecast Assistant. Clean, working, reviewed.
"""

import streamlit as st
import streamlit.components.v1 as components
import requests
import pandas as pd
import numpy as np
import json as _json

BACKEND_URL   = "http://localhost:8000"
OLLAMA_URL    = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "llama3.1:8b"

APP_DOCUMENTATION = """
APP FEATURES & TOOLBAR GUIDE:

SIDEBAR CONTROLS:
- Upload Sales Excel file: Upload .xlsx or .xls file with date + sales columns
- Data Frequency: Auto-detected (Monthly/Quarterly/Weekly). Override if wrong.
- FRED API Key: Enter your FRED API key to pull macro economic variables for SARIMAX
- Models: Check/uncheck SARIMAX, SARIMA, Prophet to select which models to run
- Forecast horizon: Slider to set how many periods ahead to forecast (1-24)
- Max Granger lag: Max lag periods for Granger causality test (default 12)
- SARIMAX settings (expander):
    - Top N exog variables: How many FRED variables to consider (default 10)
    - Max combo size: Max number of variables to combine (1=single vars only)
- Prophet settings (expander):
    - Changepoint prior scale: Controls trend flexibility (higher=more flexible)
    - Seasonality prior scale: Controls seasonality strength
    - Seasonality mode: Additive or Multiplicative
- Run Analysis button: Starts the full forecast pipeline

MAIN TABS:
- Diagnostics tab: Shows data overview, stationarity tests (ADF/KPSS), 
  seasonal decomposition, outlier detection, monthly averages
- SARIMAX tab: Shows Granger causality results, selected variables, 
  model orders, walk-forward validation, forecast chart, residual diagnostics
- SARIMA tab: Shows auto-selected ARIMA orders, AIC scores, 
  walk-forward validation, forecast, residuals
- Prophet tab: Shows trend changepoints, seasonality components, 
  COVID regressor impact, walk-forward validation, forecast
- Comparison tab (when 2+ models): Side-by-side MAPE/RMSE comparison, 
  combined forecast chart, model agreement analysis, recommendations

FRED DATA PULL STATUS (expander): Shows which FRED variables were 
downloaded and their Granger causality p-values

AI FORECAST ASSISTANT (bottom of page):
- Model selector: Choose which Ollama model to use
- Mic button: Voice input (Chrome/Edge only)
- Speaker button: Read last reply aloud
- Clear button: Clear chat history
- Type questions about your results or the app
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
8. If context says NO_RESULTS, say "Please click Run Analysis first."
9. Never make up numbers — only use what is in SESSION DATA.

APP DOCUMENTATION:
{app_docs}

SESSION DATA:
{context}"""


def build_context() -> str:
    lines = []
    params = st.session_state.get('fc_params', {})
    if params:
        lines += ["PARAMETERS",
                  f"  Models: {', '.join(params.get('models_run', []))}",
                  f"  Horizon: {params.get('forecast_horizon')} {params.get('frequency','').lower()} periods",
                  f"  Frequency: {params.get('frequency')} s={params.get('seasonal_period_s')}",
                  f"  Seasonality: {params.get('seasonality_mode')} {params.get('seasonal_strength_pct')}%"]
    diag = st.session_state.get('fc_diagnostics', {})
    sales_series = st.session_state.get('fc_sales_series', pd.Series(dtype=float))
    if diag:
        lines += ["", "DATA",
            f"  {diag.get('date_start')} to {diag.get('date_end')} n={diag.get('total_records')}",
            f"  Mean={diag.get('mean_sales'):,.0f} Std={diag.get('std_sales'):,.0f}",
            f"  ADF p={diag.get('adf_p')} {'STATIONARY' if diag.get('adf_stationary') else 'NON-STATIONARY'}",
            f"  KPSS p={diag.get('kpss_p')} {'STATIONARY' if diag.get('kpss_stationary') else 'NON-STATIONARY'}",
            f"  Peak=Month{diag.get('peak_month')} avg={diag.get('peak_month_avg'):,.0f}",
            f"  Trough=Month{diag.get('trough_month')} avg={diag.get('trough_month_avg'):,.0f}",
            f"  Outliers={diag.get('outlier_count')}"]
        monthly = diag.get('monthly_avg_by_month', {})
        if monthly:
            lines += ["  Monthly: " + " ".join(f"M{m}={v:,.0f}" for m, v in monthly.items())]
    elif len(sales_series):
        lines += ["", f"DATA n={len(sales_series)} mean={sales_series.mean():,.0f}"]
    results = st.session_state.get('fc_results', {})
    detail  = st.session_state.get('fc_model_detail', {})
    if results:
        lines += ["", "RESULTS — USE EXACT NUMBERS:"]
        for mn, r in results.items():
            d = detail.get(mn, {})
            lines += [f"  {mn}: MAPE={r['mape']:.2f}% RMSE={r['rmse']:,.0f} Accuracy={100-r['mape']:.1f}%"]
            if mn == 'SARIMAX' and d:
                lines += [f"    order={d.get('order')} seasonal={d.get('seasonal_order')} vars={d.get('selected_variables')}"]
            elif mn == 'SARIMA' and d:
                lines += [f"    order={d.get('order')} seasonal={d.get('seasonal_order')} AIC={d.get('best_aic')}"]
            elif mn == 'Prophet' and d:
                lines += [f"    mode={d.get('seasonality_mode')} cp={d.get('significant_changepoints')}/{d.get('total_changepoints')}",
                          f"    CV_reg={d.get('cv_mape_with_regressor')}% CV_base={d.get('cv_mape_baseline')}%"]
                if d.get('changepoint_details'):
                    for cp in d['changepoint_details']:
                        lines += [f"    CP {cp['date']}: {cp['delta']:+.4f} {cp['direction']}"]
            if d:
                lines += [f"    LB10={'PASS' if d.get('lb10_pass') else 'FAIL'} "
                          f"LB20={'PASS' if d.get('lb20_pass') else 'FAIL'} "
                          f"Shapiro={'Normal' if d.get('normality_pass') else 'Non-normal'} "
                          f"DW={d.get('durbin_watson')} {'OK' if d.get('dw_pass') else 'SerialCorr'}"]
            if d and d.get('wf_dates'):
                for date, actual, pred in zip(d.get('wf_dates', []), d.get('wf_actual', []), d.get('wf_predicted', [])):
                    err = (actual - pred) / actual * 100 if actual else 0
                    lines += [f"    WF {date}: actual={actual:,.0f} pred={pred:,.0f} err={err:+.1f}%"]
            fc_idx = pd.to_datetime(r['fc_idx'])
            for dt, val, lo, hi in zip(fc_idx, r['fc_mean'], r['fc_lower'], r['fc_upper']):
                lines += [f"    FC {dt.strftime('%Y-%m')}: {val:,.0f} CI={lo:,.0f}-{hi:,.0f}"]
        winner = min(results, key=lambda m: results[m]['mape'])
        lines += [f"  BEST={winner} {results[winner]['mape']:.2f}%"]
    else:
        lines += ["", "NO_RESULTS: Tell user to click Run Analysis first."]
    return "\n".join(lines)


def push_context_to_backend(model: str = "llama3.1:8b"):
    try:
        requests.post(f"{BACKEND_URL}/api/context",
                      json={"context": build_context(), "model": model}, timeout=3)
    except Exception:
        pass


def _ollama_is_running():
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=2)
        if resp.status_code == 200:
            return True, [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        pass
    return False, []


def _stream_ollama(messages, model=DEFAULT_MODEL):
    payload = {"model": model, "messages": messages, "stream": True, "options": {"num_predict": 2048, "temperature": 0.3}}
    try:
        with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=120) as resp:
            if resp.status_code != 200:
                yield f"[Error {resp.status_code}]"; return
            for line in resp.iter_lines():
                if not line: continue
                try:
                    chunk = _json.loads(line)
                    if delta := chunk.get("message", {}).get("content", ""):
                        yield delta
                    if chunk.get("done"): break
                except Exception: continue
    except requests.exceptions.ConnectionError:
        yield "Ollama not running. Start with: `brew services start ollama`"
    except requests.exceptions.Timeout:
        yield "Timed out — try again."


def _do_chat(user_input, model):
    st.session_state.chat_messages.append({"role": "user", "content": user_input})
    st.markdown(
        f'<div style="display:flex;justify-content:flex-end;margin:8px 0 12px">'
        f'<div style="background:#C8102E;color:#fff;padding:10px 16px;'
        f'border-radius:18px 18px 4px 18px;font-size:14px;line-height:1.6;'
        f'max-width:72%">{user_input}</div></div>', unsafe_allow_html=True)
    context = build_context()
    sys_msg = {"role": "system", "content": SYSTEM_TEMPLATE.format(context=context, app_docs=APP_DOCUMENTATION)}
    history = [{"role": m["role"], "content": m["content"]}
               for m in st.session_state.chat_messages]
    ph = st.empty()
    full = ""
    for chunk in _stream_ollama([sys_msg] + history, model=model):
        full += chunk
        ph.markdown(full + "▌")
    ph.markdown(full)
    st.markdown('<hr style="border:none;border-top:1px solid #F3F4F6;margin:6px 0 10px">',
                unsafe_allow_html=True)
    st.session_state.chat_messages.append({"role": "assistant", "content": full})


def _render_direct_chat():
    # Init state
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []
    # _sq_pending stores the question selected from dropdown
    # separate from chatbot_pending to avoid conflicts with app.py
    if "_sq_pending" not in st.session_state:
        st.session_state._sq_pending = None

    running, model_opts = _ollama_is_running()
    model_opts = model_opts or [DEFAULT_MODEL]
    results    = st.session_state.get("fc_results", {})

    # ── CSS ──────────────────────────────────────────────────
    st.markdown("""
    <style>
    [data-testid="stChatInput"] > div {
        border:1.5px solid #E5E7EB !important; border-radius:16px !important;
        background:#FFFFFF !important; box-shadow:0 2px 12px rgba(0,0,0,0.07) !important;
    }
    [data-testid="stChatInput"] > div:focus-within {
        border-color:#C8102E !important;
    }
    [data-testid="stChatInput"] button { background:#C8102E !important; border-radius:10px !important; }
    </style>""", unsafe_allow_html=True)

    # ── Header ───────────────────────────────────────────────
    chips = ""
    for m, r in results.items():
        if r["mape"] < 2:   bg,tc,bc = "#D1FAE5","#065F46","#6EE7B7"
        elif r["mape"] < 5: bg,tc,bc = "#FEF3C7","#92400E","#FCD34D"
        else:               bg,tc,bc = "#FEE2E2","#991B1B","#FCA5A5"
        chips += (f'<span style="display:inline-flex;align-items:center;padding:3px 10px;'
                  f'border-radius:20px;font-size:11px;font-weight:600;margin:2px;'
                  f'background:{bg};color:{tc};border:1px solid {bc}">'
                  f'{m} · {r["mape"]:.2f}%</span>')

    st.markdown(f"""
    <div style="background:#FFFFFF;border:1px solid #E8EAED;border-radius:14px;
                padding:18px 22px;margin-bottom:12px;box-shadow:0 2px 8px rgba(0,0,0,0.05)">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="font-size:12px;color:#9CA3AF">Powered by Ollama</div>
        <span style="background:#F1F5F9;border:1px solid #E2E8F0;border-radius:6px;
               padding:3px 10px;font-size:11px;font-weight:600;color:#475569">{model_opts[0]}</span>
      </div>
      {"<div style='margin-top:8px;display:flex;flex-wrap:wrap'>" + chips + "</div>"
       if chips else "<div style='margin-top:6px;font-size:12px;color:#94A3B8'>Run Analysis to load context</div>"}
    </div>""", unsafe_allow_html=True)

    if not running:
        st.error("Ollama not running — `brew services start ollama`")
        return

    # ── Message history ───────────────────────────────────────
    for msg in st.session_state.chat_messages:
        if msg["role"] == "user":
            st.markdown(
                f'<div style="display:flex;justify-content:flex-end;margin:8px 0 12px">'
                f'<div style="background:#C8102E;color:#fff;padding:10px 16px;'
                f'border-radius:18px 18px 4px 18px;font-size:14px;line-height:1.6;'
                f'max-width:72%">{msg["content"]}</div></div>', unsafe_allow_html=True)
        else:
            st.markdown(msg["content"])
            st.markdown('<hr style="border:none;border-top:1px solid #F3F4F6;margin:6px 0 10px">',
                        unsafe_allow_html=True)

    # ── Toolbar: model + mic + speaker + clear ────────────────
    last_bot = next((m["content"] for m in reversed(st.session_state.chat_messages)
                     if m["role"] == "assistant"), "")
    safe_tts = (last_bot.replace("\\", "\\\\").replace("`", "\\`")
                        .replace("\n", " ").replace("**", "").replace("*", ""))

    cm, cmic, cspk, cclr = st.columns([5, 0.65, 0.65, 0.65])
    with cm:
        try:
            chosen = st.selectbox("Model", model_opts, label_visibility="collapsed",
                                  key="chatbot_model_select")
        except TypeError:
            chosen = st.selectbox("Model", model_opts, key="chatbot_model_select")
    with cmic:
        st.markdown("""
        <button title="Voice input (Chrome/Edge)"
          style="display:flex;align-items:center;justify-content:center;width:38px;height:38px;
                 border:1px solid #E5E7EB;border-radius:8px;background:#F9FAFB;cursor:pointer;color:#6B7280"
          onmouseenter="this.style.borderColor='#C8102E';this.style.color='#C8102E'"
          onmouseleave="this.style.borderColor='#E5E7EB';this.style.color='#6B7280'"
          onclick="(function(){
            var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
            if(!SR){alert('Voice needs Chrome or Edge');return;}
            var r=new SR();r.lang='en-US';r.interimResults=false;
            r.onresult=function(e){
              var t=e.results[0][0].transcript;
              var ta=document.querySelector('[data-testid=stChatInputTextArea]');
              if(ta){var s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
                s.call(ta,t);ta.dispatchEvent(new Event('input',{bubbles:true}));ta.focus();}
            };r.start();})()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" y1="19" x2="12" y2="23"/>
            <line x1="8" y1="23" x2="16" y2="23"/>
          </svg>
        </button>""", unsafe_allow_html=True)
    with cspk:
        st.markdown(f"""
        <button title="Read last reply"
          style="display:flex;align-items:center;justify-content:center;width:38px;height:38px;
                 border:1px solid #E5E7EB;border-radius:8px;background:#F9FAFB;cursor:pointer;color:#6B7280"
          onmouseenter="this.style.borderColor='#C8102E';this.style.color='#C8102E'"
          onmouseleave="this.style.borderColor='#E5E7EB';this.style.color='#6B7280'"
          onclick="(function(){{
            var ss=window.speechSynthesis;if(ss.speaking){{ss.cancel();return;}}
            var raw=`{safe_tts}`;if(!raw.trim())return;
            var u=new SpeechSynthesisUtterance(raw);u.rate=1.0;u.pitch=1.0;
            var v=ss.getVoices();
            var found=v.find(function(x){{return x.lang==='en-US'&&x.name.includes('Google');}})
                     ||v.find(function(x){{return x.lang.startsWith('en');}});
            if(found)u.voice=found;ss.speak(u);
          }})()">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round">
            <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
            <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
            <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
          </svg>
        </button>""", unsafe_allow_html=True)
    with cclr:
        if st.button("Clear", key="chatbot_clear", use_container_width=True):
            st.session_state.chat_messages = []
            st.rerun()

    # ── Chat input ────────────────────────────────────────────
    if prompt := st.chat_input("Ask anything about your forecast...", key="chatbot_input"):
        _do_chat(prompt, chosen)


def render_chatbot():
    st.divider()
    backend_up = False
    try:
        r = requests.get(f"{BACKEND_URL}/api/health", timeout=1)
        backend_up = r.status_code == 200
    except Exception:
        pass

    if backend_up:
        push_context_to_backend()
        components.iframe("http://localhost:3000", height=700, scrolling=False)
    else:
        _render_direct_chat()
