"""
Cache cleanup utility — prevents stale bytecode from hiding code changes.

Key behaviour:
- Sets sys.dont_write_bytecode = True AND os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
  on every call so subprocesses always inherit the flag.
- Deletes __pycache__ directories every call (fast no-op when nothing exists).
- No "run once" guard — Streamlit re-executes scripts on every rerun, so this
  must be idempotent and always effective.
"""

import os
import sys
import shutil
import time
from pathlib import Path
from utils.server_logger import log_structured_error, log_error


def clean_pycache(log_func=None):
    """
    Delete all __pycache__ directories under the app root and ensure
    bytecode writing is disabled for this process and all subprocesses.

    Returns:
        dict: cleanup results for monitoring
    """
    # Always enforce — covers this process and any child processes
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

    start_time = time.perf_counter()
    results = {
        "status": "success",
        "dirs_removed": 0,
        "files_removed": 0,
        "errors": [],
        "time_ms": 0,
        "app_dir": None,
    }

    def log(msg):
        if log_func:
            try:
                log_func(msg)
            except Exception:
                pass

    try:
        app_dir = Path(__file__).resolve().parent.parent  # utils/ -> app/
        results["app_dir"] = str(app_dir)

        pycache_dirs = list(app_dir.rglob("__pycache__"))

        if not pycache_dirs:
            elapsed = (time.perf_counter() - start_time) * 1000
            results["time_ms"] = round(elapsed, 2)
            return results

        log(f"[CACHE_CLEANER] Found {len(pycache_dirs)} __pycache__ dirs, removing...")

        for pycache_dir in pycache_dirs:
            try:
                file_count = len(list(pycache_dir.glob("*.pyc")))
                shutil.rmtree(pycache_dir, ignore_errors=True)
                results["dirs_removed"] += 1
                results["files_removed"] += file_count
                log(f"[CACHE_CLEANER] Removed: {pycache_dir} ({file_count} files)")
            except Exception as e:
                error_msg = f"Failed to remove {pycache_dir}: {e}"
                results["errors"].append(error_msg)
                log(f"[CACHE_CLEANER] ERROR: {error_msg}")

        # Catch any stray .pyc files outside __pycache__ dirs
        try:
            for pyc_file in list(app_dir.rglob("*.pyc")):
                try:
                    pyc_file.unlink(missing_ok=True)
                    results["files_removed"] += 1
                except Exception:
                    pass
        except Exception:
            pass

    except Exception as e:
        log_structured_error(e, page="cache_cleaner", component="clean_pycache", operation="cleaning_pycache_directories")
        results["status"] = "error"
        results["errors"].append(f"Cleanup failed: {e}")

    elapsed = (time.perf_counter() - start_time) * 1000
    results["time_ms"] = round(elapsed, 2)
    return results


def get_pycache_status():
    """Return current __pycache__ status without cleaning — useful for debugging."""
    try:
        app_dir = Path(__file__).resolve().parent.parent
        pycache_dirs = list(app_dir.rglob("__pycache__"))
        pyc_files = list(app_dir.rglob("*.pyc"))
        return {
            "dont_write_bytecode": sys.dont_write_bytecode,
            "env_var_set": os.environ.get('PYTHONDONTWRITEBYTECODE') == '1',
            "pycache_dirs_count": len(pycache_dirs),
            "pyc_files_count": len(pyc_files),
            "pycache_dirs": [str(d) for d in pycache_dirs[:10]],
            "app_dir": str(app_dir),
        }
    except Exception as e:
        log_structured_error(e, page="cache_cleaner", component="get_pycache_status", operation="checking_pycache_status")
        return {"error": str(e)}


# Kept for backwards compatibility — earnings_calls.py calls this
def clean_pycache_once(log_func=None):
    try:
        return clean_pycache(log_func)
    except Exception as e:
        log_structured_error(e, page="cache_cleaner", component="clean_pycache_once", operation="cleaning_pycache")
        return {}


def ensure_fresh_code(log_func=None):
    try:
        return clean_pycache(log_func)
    except Exception as e:
        log_structured_error(e, page="cache_cleaner", component="ensure_fresh_code", operation="cleaning_pycache")
        return {}
