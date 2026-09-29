#!/usr/bin/env bash
# Azure App Service startup command:  bash startup.sh
#
# Same as the startup command this app already uses, plus one thing: the moment the
# server answers, it triggers a full script run itself, so the caches are built before
# the first visitor arrives instead of by them.
#
# Why that extra step is needed: Streamlit only executes app/main.py when a CLIENT
# connects. Every warm-up inside main.py — the DB pools, the screening universes —
# therefore waited for a human. On a cold container the first person to open Screening
# paid ~9.6s building the all-companies universe, staring at a blank page. Hitting
# /_stcore/script-health-check runs the script with no browser attached, which starts
# those warms on EVERY container start: deploy, restart, crash, platform maintenance.
#
# Borrowed from the modular app (Code-Base/Modular-Code/startup.sh), which has been
# running this since Sep-2026. Look for [BOOT][WARM_TRIGGER] in the log stream.
#
# Rollback: set the startup command back to the plain `python -m streamlit run ...`
# line at the bottom of this file.
set -u

echo "[STARTUP] Installing WeasyPrint system dependencies..."
apt-get update -qq && apt-get install -y -qq \
  libpango-1.0-0 \
  libpangocairo-1.0-0 \
  libcairo2 \
  libgdk-pixbuf2.0-0 \
  libffi-dev \
  shared-mime-info \
  fontconfig
echo "[STARTUP] System dependencies installed."

# Prevent Python from writing .pyc bytecode files at all
export PYTHONDONTWRITEBYTECODE=1

echo "[STARTUP] Clearing Python bytecode cache..."
find /home/site/wwwroot -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find /home/site/wwwroot -name "*.pyc" -delete 2>/dev/null || true
find /tmp -maxdepth 3 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$(pwd)" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$(pwd)" -name "*.pyc" -delete 2>/dev/null || true
echo "[STARTUP] Bytecode cache cleared."

# Turns on the endpoint the warm below calls. An environment variable rather than a line
# in .streamlit/config.toml, so the config file stays untouched.
export STREAMLIT_SERVER_SCRIPT_HEALTH_CHECK_ENABLED=true

PORT="${WEBSITES_PORT:-8000}"

# The warm helper runs in the background and exits once it has fired; it never affects
# the app, and a failure here can only cost the old cold-start, never a broken boot.
PYTHON="$(command -v python || command -v python3)"
"$PYTHON" - "$PORT" <<'PY' &
import sys
import time
import urllib.request

port = sys.argv[1]
started = time.time()
for _ in range(600):          # up to 10 minutes for the server to start listening
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2)
        break
    except OSError:
        time.sleep(1)
up = time.time() - started

# The real endpoint answers "ok". With scriptHealthCheckEnabled off the same URL returns
# the normal page (also HTTP 200) and nothing is warmed — so the answer's TEXT is what
# decides, not the status code.
try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/_stcore/script-health-check", timeout=300
    ) as reply:
        answer = reply.read(64).decode("utf-8", "replace").strip()
    result = ("OK - warm started" if answer == "ok"
              else "NOT WARMED - the script health check endpoint is off")
except Exception as error:
    result = f"NOT WARMED - health check failed ({error})"
print(f"[BOOT][WARM_TRIGGER] {result} | port {port} | server up after {up:.1f}s | "
      f"{time.time() - started:.1f}s after start", flush=True)
PY

echo "[STARTUP] Starting Streamlit app on port ${PORT}..."
# exec: Streamlit replaces this shell, so it stays the same process Azure starts, stops
# and restarts today — no wrapper sits between it and Azure's signals.
exec python -m streamlit run app/main.py \
  --server.port "${PORT}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false
