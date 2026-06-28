#!/usr/bin/env python3
"""Apply the Coresight boot-overlay patch to Streamlit's static index.html.

Run this ONCE before `streamlit run` (in the launcher / deploy entrypoint) so the
grey boot skeleton is suppressed and the branded loader shows from the very first
request — before any Streamlit session has executed app code. Idempotent.

    python scripts/patch_streamlit_boot.py
"""
import sys
from pathlib import Path

# Make `core` importable whether run from repo root or scripts/.
_APP = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(_APP))

try:
    from core.boot_overlay import patch_streamlit_index_html
    ok = patch_streamlit_index_html()
    print(f"[patch_streamlit_boot] applied={ok}")
    sys.exit(0 if ok else 1)
except Exception as e:  # never block startup
    print(f"[patch_streamlit_boot] skipped: {e!r}")
    sys.exit(0)
