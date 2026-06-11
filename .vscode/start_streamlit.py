#!/usr/bin/env python3
"""
Streamlit launcher with automatic port detection.
Finds an available port starting from 8501 and launches the app.
"""
import socket
import os
import sys
from pathlib import Path

def find_available_port(start_port=8501, max_port=8510):
    """Find an available port in the given range."""
    for port in range(start_port, max_port + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available ports found between {start_port} and {max_port}")

def main():
    # Get workspace folder
    workspace = Path(__file__).parent.parent
    main_py = workspace / "app" / "main.py"
    venv_path = workspace / ".venv"
    venv_python = venv_path / "bin" / "python"
    
    # Ensure we're using the venv Python
    if not venv_python.exists():
        print(f"❌ Virtual environment Python not found at: {venv_python}")
        print("Please ensure the virtual environment is set up at .venv/")
        sys.exit(1)
    
    # Find available port
    try:
        port = find_available_port()
        print(f"✅ Found available port: {port}")
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
    
    print(f"🚀 Starting Streamlit on port {port}...")
    print(f"🐍 Python: {venv_python}")
    print(f"🌐 URL: http://localhost:{port}")
    print(f"📁 Main file: {main_py}")
    print("-" * 60)
    
    # Set VIRTUAL_ENV to ensure venv packages are found
    os.environ["VIRTUAL_ENV"] = str(venv_path)
    os.environ["PATH"] = str(venv_path / "bin") + ":" + os.environ.get("PATH", "")
    os.environ["PYTHONPATH"] = str(workspace / "app")

    # CRITICAL: Prevent Python from ever writing __pycache__ / .pyc files.
    # Must be set here (before execv) so the Streamlit process AND all its
    # worker sub-processes inherit the flag from the environment.
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    # Delete any leftover __pycache__ before the server starts
    try:
        import shutil
        for pycache in list((workspace / "app").rglob("__pycache__")):
            shutil.rmtree(pycache, ignore_errors=True)
        print("✅ Cleared __pycache__ directories")
    except Exception as e:
        print(f"⚠️  Could not clear __pycache__: {e}")

    # Build command — -B flag tells the Python interpreter itself to skip
    # bytecode writing, independent of any user-level code setting.
    args = [
        str(venv_python),
        "-B",                          # Never write .pyc / __pycache__
        "-m", "streamlit",
        "run", str(main_py),
        "--server.runOnSave", "true",
        "--server.headless", "true",
        "--server.port", str(port)
    ]

    # Replace current process with streamlit - this ensures the environment is clean
    os.execv(str(venv_python), args)

if __name__ == "__main__":
    main()
