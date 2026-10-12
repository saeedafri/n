"""
server.py — FastAPI that serves everything on one port.
- /          → Next.js frontend (AI chat)
- /api/*     → FastAPI endpoints
- /streamlit → proxies to Streamlit on 8501

Run: uvicorn server:app --port 8000
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
import json
import os
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_URL    = "http://localhost:11434"
STREAMLIT_URL = "http://localhost:8501"

_context_store: dict = {"context": "", "model": "llama3.1:8b"}

APP_DOCUMENTATION = """
SIDEBAR: Upload Excel file (date+sales columns) · Data Frequency (auto-detected, override if wrong) ·
FRED API Key for macro variables · Model checkboxes (SARIMAX/SARIMA/Prophet) ·
Forecast horizon slider (periods ahead) · Max Granger lag slider ·
SARIMAX settings: Top N exog variables, Max combo size ·
Prophet settings: Changepoint prior, Seasonality prior, Seasonality mode ·
Run Analysis button starts the pipeline.

TABS: Diagnostics (ADF/KPSS tests, decomposition, outliers) ·
SARIMAX (Granger causality, variables, orders, walk-forward, forecast, residuals) ·
SARIMA (auto ARIMA orders, AIC, walk-forward, forecast, residuals) ·
Prophet (changepoints, seasonality, COVID regressor, walk-forward, forecast) ·
Comparison (side-by-side MAPE/RMSE, combined chart, recommendations).

AI ASSISTANT: Model selector · Mic (Chrome/Edge) · Speaker (TTS) · Type questions about results or app features.
"""

SYSTEM_TEMPLATE = """Answer questions about forecast results and app features concisely.

RULES:
1. Results questions: **bold** key metrics, bullet points, max 5 bullets, exact numbers only.
2. App/UI questions: clear step-by-step guidance from APP DOCUMENTATION.
3. NO_RESULTS + results question: say "Please click Run Analysis first."

APP DOCUMENTATION:
{app_docs}

SESSION DATA:
{context}"""


class ChatRequest(BaseModel):
    messages: list
    model: Optional[str] = None


class ContextUpdate(BaseModel):
    context: str
    model: Optional[str] = "llama3.1:8b"


@app.get("/api/results")
async def get_results():
    import os, json
    path = os.path.join(os.path.dirname(__file__), '..', '..', 'results.json')
    if not os.path.exists(path):
        return {"ready": False}
    try:
        with open(path) as f:
            data = json.load(f)
        data["ready"] = True
        return data
    except Exception as e:
        return {"ready": False, "error": str(e)}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/models")
async def get_models():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"models": models}
    except Exception:
        return {"models": ["llama3.1:8b"]}


@app.post("/api/context")
def update_context(data: ContextUpdate):
    _context_store["context"] = data.context
    if data.model:
        _context_store["model"] = data.model
    return {"ok": True}


@app.get("/api/context")
def get_context():
    return _context_store


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    model = req.model or _context_store.get("model", "llama3.1:8b")
    context = _context_store.get("context", "")
    system_msg = {"role": "system", "content": SYSTEM_TEMPLATE.format(context=context, app_docs=APP_DOCUMENTATION)}
    messages = [system_msg] + req.messages

    async def generate():
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_URL}/api/chat",
                    json={"model": model, "messages": messages, "stream": True, "options": {"num_predict": 2048, "temperature": 0.3}},
                    timeout=120,
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            if delta := chunk.get("message", {}).get("content", ""):
                                yield f"data: {json.dumps({'text': delta})}\n\n"
                            if chunk.get("done"):
                                yield "data: [DONE]\n\n"
                                break
                        except Exception:
                            continue
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Proxy all other requests to Streamlit ────────────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_streamlit(request: Request, path: str):
    """Proxy everything else to Streamlit."""
    url = f"{STREAMLIT_URL}/{path}"
    params = dict(request.query_params)
    headers = dict(request.headers)
    headers.pop("host", None)
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                params=params,
                headers=headers,
                content=body,
            )
            return StreamingResponse(
                iter([resp.content]),
                status_code=resp.status_code,
                headers=dict(resp.headers),
            )
    except Exception:
        return HTMLResponse(
            "<h2>Streamlit not running</h2><p>Start with: streamlit run app.py</p>",
            status_code=502
        )
