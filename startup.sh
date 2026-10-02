#!/usr/bin/env bash
# Azure App Service startup command:  bash startup.sh
#
# This runs EXACTLY the startup command MDP STG already used:
#
#   streamlit run --server.port=8000 --server.address=0.0.0.0 \
#                 --client.showSidebarNavigation=False --ui.hideTopBar=True app/main.py
#
# and adds ONE thing: the moment the server answers, it triggers a full script
# run itself, so the caches are built before the first visitor arrives instead of
# by them.
#
# Why that extra step is needed: Streamlit only executes app/main.py when a
# CLIENT connects. Every warm-up inside main.py — the DB pools, the screening
# universes, the calendar and earnings caches — therefore waited for a human. On
# a cold container the first person to open Screening paid ~9.6s building the
# all-companies universe, and Calendar was a ~25s cold load, both staring at a
# blank page. Hitting /_stcore/script-health-check runs the script with no
# browser attached, which starts those warms on EVERY container start: deploy,
# restart, crash, platform maintenance.
#
# Borrowed from the SIP app (Code-Base/Modular-Code/startup.sh), which has run
# this since Sep-2026. Look for [BOOT][WARM_TRIGGER] in the log stream, and in
# the app's own server-log.log (so it shows on the /logs page too).
#
# Rollback: set the startup command back to the plain `streamlit run ...` line
# quoted above. Or leave this in place and set app setting WARM_ON_BOOT=0, which
# disables the warming without touching the startup command.
set -u

# Turns on the endpoint the warm below calls. An environment variable rather than
# a line in .streamlit/config.toml, so the config file stays untouched.
export STREAMLIT_SERVER_SCRIPT_HEALTH_CHECK_ENABLED=true

# Stop every user's browser posting Streamlit usage telemetry (data.streamlit.io /
# webhooks.fivetran.com). .streamlit/config.toml sets this too, but .streamlit/ is
# gitignored and never deployed, so STG ran with Streamlit's default: on.
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# No source-file watching on a server: the default polls every imported file every
# 0.2 s and re-walks every loaded module after each page run (realpath on each),
# per session. Measured at 10 users: 1.85 → 1.48 CPU-s per page, median wait −22%.
export STREAMLIT_SERVER_FILE_WATCHER_TYPE=none

# WEBSITES_PORT is unset on MDP STG today, so this is 8000 — the same port the
# previous startup command hardcoded. If Azure ever sets it, we follow it.
PORT="${WEBSITES_PORT:-8000}"

# The warm helper runs in the background and exits once it has fired. It never
# affects the app: a failure here can only cost the old cold-start, never a
# broken boot.
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

# The real endpoint answers "ok". With scriptHealthCheckEnabled off the same URL
# returns the normal page (also HTTP 200) and nothing is warmed — so the answer's
# TEXT is what decides, not the status code.
try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/_stcore/script-health-check", timeout=300
    ) as reply:
        answer = reply.read(64).decode("utf-8", "replace").strip()
    result = ("OK - warm started" if answer == "ok"
              else "NOT WARMED - the script health check endpoint is off")
except Exception as error:
    result = f"NOT WARMED - health check failed ({error})"

message = (f"[BOOT][WARM_TRIGGER] {result} | port {port} | server up after {up:.1f}s | "
           f"{time.time() - started:.1f}s after start")
print(message, flush=True)          # Azure log stream

# Also write it into the app's own log file, so this is visible on the /logs page
# without anyone having to pull the Azure log stream. Same directory resolution
# and same line format the app uses, so it sorts and filters with everything else.
try:
    import datetime
    import os
    import pathlib

    def _log_dir():
        candidates = []
        env_dir = os.getenv("SERVER_LOG_DIR", "").strip()
        if env_dir:
            candidates.append(pathlib.Path(env_dir))
        candidates += [pathlib.Path("/home/LogFiles/mdp"), pathlib.Path("/home/mdp-logs"),
                       pathlib.Path("server-logs")]
        for directory in candidates:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                probe = directory / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                return directory
            except Exception:
                continue
        return None

    target = _log_dir()
    if target:
        ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        stamp = datetime.datetime.now(ist).strftime("%d-%b-%Y %I:%M:%S %p IST")
        line = (f"{stamp} | WARNING  | R?????   | page=boot            | user=-"
                f"                              | startup.sh:warm_trigger | {message}\n")
        with open(target / "server-log.log", "a", encoding="utf-8") as log_file:
            log_file.write(line)
except Exception as log_error:      # never let logging break the boot
    print(f"[BOOT][WARM_TRIGGER] could not write to the app log: {log_error}", flush=True)
PY

echo "[STARTUP] Starting Streamlit on port ${PORT} (same flags as before)..."
# exec: Streamlit replaces this shell, so it stays the same process Azure starts,
# stops and restarts today — no wrapper sits between it and Azure's signals.
#
# These flags are character-for-character the previous startup command. Nothing
# added, nothing removed: no --server.headless, no --server.enableCORS, no
# --server.enableXsrfProtection. An earlier draft of this file carried those
# three from a stale startup.sh in the repo that Azure never actually ran, and
# turning XSRF protection off is not a change a performance fix gets to make.
exec streamlit run \
  --server.port="${PORT}" \
  --server.address=0.0.0.0 \
  --client.showSidebarNavigation=False \
  --ui.hideTopBar=True \
  app/main.py
