#!/usr/bin/env python3
"""
start.py — One command to start everything.
Usage: python3 start.py
"""

import subprocess
import sys
import os
import time
import signal
import threading
import shutil

ROOT     = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join(ROOT, "forecast-app", "frontend")
BACKEND  = os.path.join(ROOT, "forecast-app", "backend")

processes = []


def find_python_with_streamlit():
    candidates = [
        "python3.11", "python3.12", "python3.10", "python3",
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/usr/local/bin/python3",
        os.path.expanduser("~/Library/Python/3.11/bin/python3"),
    ]
    for c in candidates:
        try:
            r = subprocess.run([c, "-c", "import streamlit"],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return None


def stream_output(proc, prefix, color):
    codes = {"streamlit":"\033[36m","backend":"\033[33m","frontend":"\033[35m"}
    reset = "\033[0m"
    c = codes.get(color, "")
    for line in iter(proc.stdout.readline, b""):
        text = line.decode("utf-8", errors="ignore").rstrip()
        if text:
            print(f"{c}[{prefix}]{reset} {text}", flush=True)


def kill_all(sig=None, frame=None):
    print("\n\033[31mShutting down...\033[0m")
    for p in processes:
        try: p.terminate()
        except: pass
    sys.exit(0)


signal.signal(signal.SIGINT,  kill_all)
signal.signal(signal.SIGTERM, kill_all)

print("""
\033[1m╔══════════════════════════════════════════════╗
║   Coresight Sales Forecast Intelligence      ║
╚══════════════════════════════════════════════╝\033[0m
""")

# ── Find Python ───────────────────────────────────────────
print("Finding Python with Streamlit...", end=" ", flush=True)
PYTHON = find_python_with_streamlit()
if not PYTHON:
    print("\033[31mNot found!\033[0m")
    print("Fix: pip3 install streamlit")
    sys.exit(1)
print(f"\033[32m{PYTHON}\033[0m")

# ── Check Node ────────────────────────────────────────────
if not shutil.which("npm"):
    print("\033[31mError: npm not found. Install from nodejs.org\033[0m")
    sys.exit(1)

# ── Install deps (skip if already done) ───────────────────
def backend_ready():
    try:
        r = subprocess.run([PYTHON, "-c", "import fastapi, uvicorn, httpx"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except: return False

if not backend_ready():
    print("\033[33m[backend]\033[0m Installing deps (first time only)...")
    subprocess.run([PYTHON, "-m", "pip", "install", "-r", "requirements.txt",
                    "--break-system-packages", "-q"],
                   cwd=BACKEND, check=False)
else:
    print("\033[33m[backend]\033[0m Deps ready ✓")

if not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
    print("\033[35m[frontend]\033[0m Running npm install (first time only)...")
    subprocess.run(["npm", "install", "--silent"], cwd=FRONTEND, check=True)
else:
    print("\033[35m[frontend]\033[0m node_modules ready ✓")

# ── Start backend ─────────────────────────────────────────
print("\n\033[33m[backend]\033[0m Starting FastAPI...")
bp = subprocess.Popen(
    [PYTHON, "-m", "uvicorn", "server:app", "--port", "8000"],
    cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
processes.append(bp)
threading.Thread(target=stream_output, args=(bp,"backend","backend"), daemon=True).start()

# ── Start frontend ────────────────────────────────────────
print("\033[35m[frontend]\033[0m Starting Next.js...")
fp = subprocess.Popen(
    ["npm", "run", "dev"],
    cwd=FRONTEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
processes.append(fp)
threading.Thread(target=stream_output, args=(fp,"frontend","frontend"), daemon=True).start()

# ── Kill anything on port 8501 first ─────────────────────
print("\033[36m[streamlit]\033[0m Clearing port 8501...", end=" ", flush=True)
subprocess.run(["lsof", "-ti:8501"], capture_output=True)
kill_result = subprocess.run(
    "lsof -ti:8501 | xargs kill -9 2>/dev/null; true",
    shell=True, capture_output=True
)
time.sleep(1)
print("done")

# ── Start Streamlit ───────────────────────────────────────
print("\033[36m[streamlit]\033[0m Starting Streamlit...")
sp = subprocess.Popen(
    [PYTHON, "-m", "streamlit", "run", "app.py",
     "--server.headless", "true",
     "--server.port", "8501",
     "--browser.gatherUsageStats", "false",
     "--server.fileWatcherType", "none"],
    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
processes.append(sp)
threading.Thread(target=stream_output, args=(sp,"streamlit","streamlit"), daemon=True).start()

# ── Wait for Streamlit to confirm it's ready ──────────────
print("\nWaiting for services to start", end="", flush=True)
started = False
for i in range(60):
    time.sleep(1)
    print(".", end="", flush=True)
    # Check Streamlit output for the ready signal
    if sp.poll() is not None:
        print(f"\n\033[31m[error]\033[0m Streamlit crashed (exit {sp.returncode})")
        print("Check the [streamlit] lines above for the error.")
        kill_all()
    # Try connecting after a few seconds
    if i >= 5:
        try:
            import urllib.request
            urllib.request.urlopen("http://localhost:8501", timeout=2)
            started = True
            break
        except Exception:
            pass

print()

if started:
    import webbrowser
    webbrowser.open("http://localhost:8501")
    print(f"""
\033[1m\033[32m✓ Ready!\033[0m

  \033[36m→\033[0m http://localhost:8501

\033[90mPress Ctrl+C to stop\033[0m
""")
else:
    print("\033[33mStreamlit is taking longer than usual.\033[0m")
    print("Try opening manually: http://localhost:8501")
    print("Or check the [streamlit] log lines above for errors.\n")

# ── Keep alive ────────────────────────────────────────────
while True:
    time.sleep(2)
    for p in processes:
        if p.poll() is not None:
            pass  # service stopped, already logged
