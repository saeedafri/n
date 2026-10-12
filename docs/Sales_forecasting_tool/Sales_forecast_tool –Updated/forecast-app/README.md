# Forecast App — Full Stack Setup

## Architecture
- `app.py` — Streamlit forecasting app (runs on port 8501)
- `forecast-app/backend/` — FastAPI server (runs on port 8000)
- `forecast-app/frontend/` — Next.js chat UI (runs on port 3000)

## Quick Start

### 1. Start Streamlit (forecasting)
```bash
cd ~/Downloads/sales_forecast_app
pip install -r requirements.txt
streamlit run app.py
```

### 2. Start FastAPI backend
```bash
cd ~/Downloads/sales_forecast_app/forecast-app/backend
pip install -r requirements.txt --break-system-packages
uvicorn server:app --reload --port 8000
```

### 3. Start Next.js frontend
```bash
cd ~/Downloads/sales_forecast_app/forecast-app/frontend
npm install
npm run dev
```

### 4. Use it
1. Open **localhost:8501** — run your forecast analysis
2. Open **localhost:3000** — full AI chat with voice, model selector, TTS

## Features (Next.js chat)
- Voice input (mic button) — works natively in Chrome/Edge
- Text to speech (speaker button) — reads AI responses aloud
- Model selector — switch between all your Ollama models
- Streaming responses — see answers word by word
- Full forecast context — automatically synced from Streamlit
- Suggested questions grid
- Markdown formatting with bold, bullets, code
