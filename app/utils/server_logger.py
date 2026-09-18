"""
Server Logger - Custom logging to file for debugging
Logs are stored in project_root/server-logs/server-log.log

PERFORMANCE PROFILING ADDED:
- Function execution timing
- DB query timing
- Component render timing
- Memory usage tracking

ENV FLAGS (read once at module import):
  APP_LOG_LEVEL=WARNING   Suppress INFO/DEBUG, keep WARNING+ERROR+TIMING (default: WARNING)
  APP_TIMING=1            When set, only [TIMING] and WARNING+ messages pass through
  APP_VERBOSE_LOGGING=0   When 0, suppress verbose non-timing INFO/DEBUG messages

LOG FORMAT:
  %(asctime)s | %(levelname)-7s | %(rerun_id)-8s | page=%(page)-18s | %(filename)s:%(funcName)s | %(message)s

RERUN ID:
  Uses contextvars.ContextVar (Streamlit-safe) with threading.local as fallback.
  Call new_rerun_id('page_name') at the top of each page/main before any logging.
  The ContextEnrichFilter injects rerun_id + page into every LogRecord automatically.
"""
import os

# Production defaults — explicit env always wins (setdefault only when unset).
_MDP_BOOT_DEFAULTS = {
    "MALLOC_ARENA_MAX": "2",
    "SERVER_HEARTBEAT_SECS": "60",
    "SERVER_RAM_CENSUS": "1",
    "SERVER_TRIM_ON_HEARTBEAT": "1",
    "PAGE_MATERIALIZE": "1",
    "MDP_CATEGORICAL": "1",
    "WARM_ON_BOOT": "1",
    "APP_TIMING": "1",
}
_MDP_BOOT_APPLIED: list[str] = []
for _dk, _dv in _MDP_BOOT_DEFAULTS.items():
    if _dk not in os.environ:
        os.environ.setdefault(_dk, _dv)
        _MDP_BOOT_APPLIED.append(f"{_dk}={_dv}")

import copy
import logging
import logging.handlers
import time
import traceback as _tb
import functools
import threading
import contextvars
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from contextlib import contextmanager
from queue import Queue

# IST timezone offset (UTC+5:30) — no external dependency
_IST = timezone(timedelta(hours=5, minutes=30))

# ── Log directory resolution — PERSIST ACROSS RESTARTS ──────────────────────
# Prefer Azure persistent /home/LogFiles/mdp; fallback to project server-logs/.


def _resolve_log_dir() -> Path:
    candidates = []
    _env = os.getenv("SERVER_LOG_DIR", "").strip()
    if _env:
        candidates.append(Path(_env))
    candidates.append(Path("/home/LogFiles/mdp"))
    candidates.append(Path("/home/mdp-logs"))
    candidates.append(Path(__file__).parent.parent.parent / "server-logs")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            _probe = d / ".write_probe"
            _probe.write_text("ok", encoding="utf-8")
            _probe.unlink()
            return d
        except Exception:
            continue
    return Path(__file__).parent.parent.parent / "server-logs"


SERVER_LOGS_DIR = _resolve_log_dir()
SERVER_LOG_FILE = SERVER_LOGS_DIR / "server-log.log"

_IMPORT_TS = time.time()
_HEARTBEAT_STARTED = False

# Thread-local storage — fallback for contexts where ContextVar isn't inherited
_thread_local = threading.local()

# ============================================================================
# CORRELATION ID — one per Streamlit rerun; stored in contextvars + thread-local
# ============================================================================
_rerun_id_counter = 0
_rerun_id_lock = threading.Lock()

# ContextVar: the preferred carrier — survives async boundaries within a rerun
_rerun_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    'server_logger_rerun_id', default='R?????'
)
_page_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    'server_logger_page', default='-'
)

# Module-level fallback for st.cache_data background threads.
# These threads are spawned by Streamlit's cache machinery and do NOT inherit
# contextvars from the calling thread. _last_known_page is set in the main
# request thread via new_rerun_id() and is readable by all threads.
_last_known_page: str = '-'
# NOTE: we deliberately do NOT cache the user/session id in a process-global. With many
# concurrent users on the MDP a global would let one user's background thread read
# another user's identity and MIS-ATTRIBUTE the event. User identity is read ONLY from
# per-request sources (this session's auth_data, then this request's auth_session cookie)
# — both are per-user, so an event is either correctly attributed or left blank, never
# attributed to the WRONG user.


def new_rerun_id(page: str = '') -> str:
    """Generate a new rerun correlation ID.

    Call at the very top of each page entry-point (before require_auth),
    and at the top of main.py before any log_timing calls.

    Args:
        page: short page/context name e.g. 'earnings_calls', 'main', 'login'
    """
    global _rerun_id_counter, _last_known_page
    with _rerun_id_lock:
        _rerun_id_counter += 1
        rid = f"R{_rerun_id_counter:05d}"
    # Set in both ContextVar (primary) and thread-local (fallback)
    _rerun_id_var.set(rid)
    _thread_local.rerun_id = rid
    if page:
        _last_known_page = page  # global fallback for st.cache_data background threads
        _page_var.set(page)
        _thread_local.page = page
    try:
        import streamlit as st
        _now = time.time()
        _rk, _lk = f"_rerun_count_{page or 'main'}", f"_rerun_last_{page or 'main'}"
        _n = st.session_state.get(_rk, 0) + 1
        _since = (_now - st.session_state[_lk]) * 1000 if _lk in st.session_state else -1.0
        st.session_state[_rk] = _n
        st.session_state[_lk] = _now
        _g = st.session_state.get("_rerun_count_global", 0) + 1
        st.session_state["_rerun_count_global"] = _g
        _gap = "first-load" if _since < 0 else f"{_since:.0f}ms"
        log_warning(
            f"[RERUN] page={page or '-'} | page_rerun#={_n} | session_rerun#={_g} | since_last={_gap}"
        )
        # Capture the full filter state (the app encodes tab / ticker / period /
        # sector / date range / keyword etc. in the URL query params) so the
        # analytics feed records WHAT the user is looking at — which tab, which
        # company, which filters — not just the page name. Emit on first load,
        # after an idle gap, OR whenever any filter/tab changed, so every tab
        # switch and filter change is captured (previously only coarse page
        # views on >1.2s gaps were logged).
        try:
            _qp_now = {k: v for k, v in dict(st.query_params).items()}
        except Exception:
            _qp_now = {}
        _qp_changed = st.session_state.get("_analytics_last_qp") != _qp_now
        st.session_state["_analytics_last_qp"] = _qp_now
        if page and (_since < 0 or _since > 1200.0 or _qp_changed):
            _prev_pg = st.session_state.get("_analytics_prev_page")
            log_user_event(
                "page_view",
                user=_get_user_email() if _get_user_email() != "-" else "",
                session=rid,
                page_title=page,
                rerun=_n,
                since_last_ms=None if _since < 0 else round(_since),
                filters=_qp_now or None,
                reason=("first-load" if _since < 0 else
                        ("filter-change" if _qp_changed else "dwell-gap")),
                prev_page=(_prev_pg if _prev_pg and _prev_pg != page else None),
            )
            if page != _prev_pg:
                st.session_state["_analytics_prev_page"] = page
    except Exception:
        pass
    return rid


def get_rerun_id() -> str:
    """Get current rerun correlation ID."""
    rid = _rerun_id_var.get()
    if rid == 'R?????':
        rid = getattr(_thread_local, 'rerun_id', 'R?????')
    return rid


def set_page_context(page: str) -> None:
    """Set the current page name for log enrichment without changing rerun id."""
    _page_var.set(page)
    _thread_local.page = page


def _get_page_context() -> str:
    page = _page_var.get()
    if not page or page == '-':
        page = getattr(_thread_local, 'page', '-')
    if not page or page == '-':
        page = _last_known_page
    return page or '-'


def _read_auth_identity():
    """Return (user_email, session_id) for the CURRENT request only.

    Reads this session's ``auth_data`` first, then this request's ``auth_session``
    cookie (present even before auth_data is re-hydrated into session_state — this is
    what fixed the empty-user 'main'/'logs' reruns). BOTH sources are per-user, so an
    event is either correctly attributed or left blank — NEVER attributed to the wrong
    user. Returns ('','') when truly unknown (e.g. the login page before auth).
    """
    try:
        import streamlit as st
        auth = st.session_state.get("auth_data") or {}
        email = (auth.get("user_email") or "").strip()
        sid = str(auth.get("session_id") or "").strip()
        if email:
            return email, sid
        # Fall back to the auth_session cookie (urllib-quoted JSON — decode the same
        # way auth_manager does).
        try:
            raw = st.context.cookies.get("auth_session")
            if raw:
                import urllib.parse as _up
                dec = _up.unquote(raw) if isinstance(raw, str) else raw
                if isinstance(dec, str) and len(dec) >= 2 and dec[0] == '"' and dec[-1] == '"':
                    dec = dec[1:-1]
                import json as _json
                c = _json.loads(dec)
                if isinstance(c, dict):
                    email = (c.get("user_email") or email or "").strip()
                    sid = str(c.get("session_id") or sid or "").strip()
        except Exception:
            pass
        return email, sid
    except Exception:
        return "", ""


def _get_user_email() -> str:
    """Best-effort user email for log/analytics enrichment — per-request only."""
    email, _ = _read_auth_identity()
    return email or "-"


def _get_session_id() -> str:
    """Stable per-user auth session id (short) — lets analytics group all of ONE
    user's events into their session and distinguish concurrent users. '' if unknown."""
    _, sid = _read_auth_identity()
    return sid[:16] if sid else ""


# ============================================================================
# IST FORMATTER — timestamps in IST (UTC+5:30) with 12-hour AM/PM clock
# ============================================================================

class ISTFormatter(logging.Formatter):
    """Formats log timestamps in Indian Standard Time with AM/PM."""

    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=_IST)
        return ct.strftime('%d-%b-%Y %I:%M:%S %p IST')


# ============================================================================
# CONTEXT ENRICH FILTER — injects page name into every LogRecord
# ============================================================================

class ContextEnrichFilter(logging.Filter):
    """Inject rerun_id, page, and user email into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        page = _page_var.get()
        if not page or page == '-':
            page = getattr(_thread_local, 'page', None)
        if not page or page == '-':
            page = _last_known_page
        record.page = page or '-'
        record.rerun_id = get_rerun_id()
        record.user_email = _get_user_email()
        return True


# ============================================================================
# ENV-FLAG LOG LEVEL CONTROL (read once at module import, not per-call)
# ============================================================================
_RAW_LEVEL = os.getenv("APP_LOG_LEVEL", "WARNING").upper()
_EFFECTIVE_LEVEL: int = getattr(logging, _RAW_LEVEL, logging.WARNING)
_TIMING_ONLY: bool = os.getenv("APP_TIMING", "1").strip() == "1"
_VERBOSE: bool = os.getenv("APP_VERBOSE_LOGGING", "0").strip() != "0"

# Background queue listener (singleton, started once)
_queue_listener: Optional[logging.handlers.QueueListener] = None
_queue_listener_lock = threading.Lock()


def ensure_log_dir():
    """Create server-logs directory if it doesn't exist."""
    SERVER_LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _proc_uptime_s() -> float:
    try:
        with open("/proc/self/stat") as _f:
            _starttime_ticks = int(_f.read().split()[21])
        _hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as _f:
            _sys_uptime = float(_f.read().split()[0])
        return max(0.0, _sys_uptime - _starttime_ticks / _hz)
    except Exception:
        return time.time() - _IMPORT_TS


def _malloc_trim() -> bool:
    try:
        import ctypes as _ct
        _ct.CDLL("libc.so.6").malloc_trim(0)
        return True
    except Exception:
        return False


def _build_stamp() -> str:
    """One line naming the code this process is running, written at boot.

    Until now nothing in the log said WHICH build was up, so "is this slow because
    of a deploy?" could not be answered from the log alone — the only way to tell a
    deploy from a plain restart was to go and look at the repo. Three fields answer
    it: `root` is the Oryx extract directory, which is a fresh /tmp/<hash> on every
    deploy and constant across a restart; `newest` is the most recent .py mtime in
    the app tree; `files`/`bytes` catch a hand-edited container (the Testing-2 sync
    method writes files in place, which moves neither root nor the deploy time).
    """
    try:
        _app = Path(__file__).resolve().parent.parent
        _newest, _n, _bytes = 0.0, 0, 0
        for _f in _app.rglob("*.py"):
            try:
                _st = _f.stat()
            except OSError:
                continue
            _n += 1
            _bytes += _st.st_size
            if _st.st_mtime > _newest:
                _newest = _st.st_mtime
        _when = datetime.fromtimestamp(
            _newest, timezone(timedelta(hours=5, minutes=30))
        ).strftime('%d-%b-%Y %H:%M:%S IST') if _newest else "?"
        return (f"[BUILD] root={_app.parent} | newest_py={_when} "
                f"| files={_n} bytes={_bytes} | APP_ENV={os.getenv('APP_ENV', '?')}")
    except Exception as _e:
        return f"[BUILD] unavailable ({type(_e).__name__})"


def _write_restart_ledger():
    try:
        _p = SERVER_LOGS_DIR / "restarts.log"
        _ts = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%Y-%m-%d %H:%M:%S IST')
        _arena = os.getenv("MALLOC_ARENA_MAX", "UNSET")
        try:
            _rss = ram_snapshot_mb()[0]
        except Exception:
            _rss = -1
        with open(_p, "a", encoding="utf-8") as _f:
            _f.write(f"{_ts} | pid={os.getpid()} | MALLOC_ARENA_MAX={_arena} | boot_rss={_rss}MB\n")
    except Exception:
        pass


def _start_heartbeat():
    global _HEARTBEAT_STARTED
    if _HEARTBEAT_STARTED:
        return
    try:
        _interval = int(os.getenv("SERVER_HEARTBEAT_SECS", "60"))
    except (TypeError, ValueError):
        _interval = 60
    if _interval <= 0:
        return
    _HEARTBEAT_STARTED = True

    def _beat():
        _n = 0
        while True:
            try:
                _n += 1
                _up = _proc_uptime_s()
                try:
                    _rss, _used, _total, _avail = ram_snapshot_mb()
                    _ram = f"app_rss={_rss}MB avail={_avail}MB"
                except Exception:
                    _ram = "app_rss=? avail=?"
                log_warning(f"[HEARTBEAT] pid={os.getpid()} uptime={_up:.0f}s {_ram}")
                if os.getenv("SERVER_TRIM_ON_HEARTBEAT", "1").strip().lower() in ("1", "true", "yes", "on"):
                    try:
                        _r0 = ram_snapshot_mb()[0]
                        if _malloc_trim():
                            _r1 = ram_snapshot_mb()[0]
                            if _r0 is not None and _r1 is not None:
                                log_warning(
                                    f"[MALLOC_TRIM] rss {_r0:.0f}→{_r1:.0f}MB "
                                    f"(released {max(0.0, _r0-_r1):.0f}MB) pid={os.getpid()}"
                                )
                    except Exception:
                        pass
                if _n % 3 == 0 and os.getenv("SERVER_RAM_CENSUS", "1").strip().lower() in ("1", "true", "yes", "on"):
                    # Detailed forensic report (types by count+bytes, glibc heap,
                    # rssAnon/File, sessions, caches, optional tracemalloc sites).
                    try:
                        mem_report("heartbeat")
                    except Exception:
                        _ram_census()  # fall back to the lightweight census
            except Exception:
                pass
            time.sleep(_interval)

    threading.Thread(target=_beat, daemon=True, name="mdp_heartbeat").start()


def _ram_census() -> None:
    try:
        import gc as _gc
        import pandas as _pd
        _tot = 0
        _sizes = []
        for _o in _gc.get_objects():
            if type(_o) is _pd.DataFrame:
                try:
                    _b = int(_o.memory_usage(deep=True).sum())
                    _shape = _o.shape
                except Exception:
                    continue
                _tot += _b
                _sizes.append((_b, _shape))
        _n = len(_sizes)
        _sizes.sort(reverse=True)
        _top = "; ".join(f"{_b/1e6:.0f}MB{_sh}" for _b, _sh in _sizes[:6])
        _rss, _used, _total, _avail = ram_snapshot_mb()
        _rss_s = "?" if _rss is None else f"{_rss:.0f}"
        _av_s = "?" if _avail is None else f"{_avail:.0f}"
        _ret = "?" if _rss is None else f"{max(0.0, _rss - _tot/1e6):.0f}"
        log_warning(
            f"[RAM_CENSUS] live_frames={_n} data_total={_tot/1e6:.0f}MB "
            f"rss={_rss_s}MB retained_glibc~{_ret}MB free={_av_s}MB | top: {_top}"
        )
        _sizes = None
    except Exception as _e:
        log_warning(f"[RAM_CENSUS] failed: {type(_e).__name__}: {str(_e)[:120]}")


def _glibc_mallinfo():
    """Read glibc heap accounting via mallinfo2(). Separates memory the process
    genuinely uses (uordblks) from memory glibc freed but has NOT returned to
    the OS (fordblks = fragmentation/retention) — the number that explains an
    RSS far larger than live Python objects."""
    try:
        import ctypes as _ct
        _c = _ct.c_size_t

        class _MI2(_ct.Structure):
            _fields_ = [(_n, _c) for _n in (
                "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
                "fsmblks", "uordblks", "fordblks", "keepcost")]

        _libc = _ct.CDLL("libc.so.6")
        _libc.mallinfo2.restype = _MI2
        _mi = _libc.mallinfo2()
        return {
            "arena_MB": _mi.arena / 1e6,        # heap from sbrk (main arena)
            "mmap_MB": _mi.hblkhd / 1e6,         # large allocs via mmap
            "in_use_MB": _mi.uordblks / 1e6,     # actually allocated & used
            "free_retained_MB": _mi.fordblks / 1e6,  # freed, NOT returned to OS
            "releasable_MB": _mi.keepcost / 1e6,     # trimmable top-of-heap
        }
    except Exception:
        return None


def mem_report(tag: str = "heartbeat") -> None:
    """Detailed 'where is my RAM' forensic report. One log block, greppable by
    [MEM_REPORT]. Answers: is RSS in live Python objects (which types?), in glibc
    heap fragmentation, or in file-backed pages — plus Streamlit session/cache
    accumulation. Set MEM_TRACEMALLOC=1 (and it auto-starts tracemalloc at boot)
    to also print the top Python allocation SITES (file:line)."""
    import gc as _gc
    import sys as _sys
    from collections import defaultdict as _dd
    _out = [f"[MEM_REPORT] tag={tag}"]

    # ── 1. Process RSS split: anonymous (heap/data) vs file-backed (code/mmap)
    try:
        _anon = _file = _shr = _swap = None
        try:
            with open("/proc/self/smaps_rollup") as _f:
                for _l in _f:
                    if _l.startswith("Anonymous:"):
                        _anon = int(_l.split()[1]) / 1024.0
                    elif _l.startswith("Swap:") and not _l.startswith("SwapPss"):
                        _swap = int(_l.split()[1]) / 1024.0
                    elif _l.startswith("Shared_"):
                        _shr = (_shr or 0) + int(_l.split()[1]) / 1024.0
                    elif _l.startswith("Private_Dirty:"):
                        _file = None  # placeholder; RssFile via status below
        except Exception:
            pass
        _rssf = None
        try:
            with open("/proc/self/status") as _f:
                for _l in _f:
                    if _l.startswith("RssAnon:"):
                        _anon = int(_l.split()[1]) / 1024.0
                    elif _l.startswith("RssFile:"):
                        _rssf = int(_l.split()[1]) / 1024.0
                    elif _l.startswith("VmSwap:"):
                        _swap = int(_l.split()[1]) / 1024.0
        except Exception:
            pass
        _rss, _used, _total, _avail = ram_snapshot_mb()
        _out.append(
            f"rss={('?' if _rss is None else f'{_rss:.0f}')}MB "
            f"rssAnon(heap)={('?' if _anon is None else f'{_anon:.0f}')}MB "
            f"rssFile(code/mmap)={('?' if _rssf is None else f'{_rssf:.0f}')}MB "
            f"swap={('?' if _swap is None else f'{_swap:.0f}')}MB "
            f"host_avail={('?' if _avail is None else f'{_avail:.0f}')}MB")
    except Exception:
        pass

    # ── 2. glibc heap: used vs freed-but-retained (fragmentation)
    try:
        _mi = _glibc_mallinfo()
        if _mi:
            _out.append(
                "glibc: in_use={in_use_MB:.0f}MB free_retained={free_retained_MB:.0f}MB "
                "arena={arena_MB:.0f}MB mmap={mmap_MB:.0f}MB releasable={releasable_MB:.0f}MB".format(**_mi))
    except Exception:
        pass

    # ── 3. Python objects: ONE pass — count by type, size the big categories.
    #    Catches the List[Dict] row data (dict/str/list/tuple) the DataFrame-only
    #    census cannot see, plus numpy arrays.
    try:
        _objs = _gc.get_objects()
        _cnt = _dd(int)
        _byt = _dd(int)
        _np_bytes = 0
        _df_bytes = 0
        try:
            import numpy as _np
            _ndarray = _np.ndarray
        except Exception:
            _ndarray = ()
        try:
            import pandas as _pd
            _DF = _pd.DataFrame
        except Exception:
            _DF = ()
        _SIZED = {"dict", "str", "bytes", "list", "tuple", "set", "frozenset", "float", "int"}
        for _o in _objs:
            _t = type(_o).__name__
            _cnt[_t] += 1
            if _ndarray and isinstance(_o, _ndarray):
                try:
                    _np_bytes += int(_o.nbytes)
                except Exception:
                    pass
            elif _DF and type(_o) is _DF:
                try:
                    _df_bytes += int(_o.memory_usage(deep=True).sum())
                except Exception:
                    pass
            elif _t in _SIZED:
                try:
                    _byt[_t] += _sys.getsizeof(_o)
                except Exception:
                    pass
        _total_objs = len(_objs)
        _gc_tracked_MB = sum(_byt.values()) / 1e6
        _out.append(
            f"py_objects={_total_objs:,} numpy={_np_bytes/1e6:.0f}MB "
            f"dataframes={_df_bytes/1e6:.0f}MB shallow_sized={_gc_tracked_MB:.0f}MB")
        _top_c = sorted(_cnt.items(), key=lambda _x: -_x[1])[:12]
        _out.append("top_types_by_COUNT: " + " ".join(f"{_t}={_c:,}" for _t, _c in _top_c))
        _top_b = sorted(_byt.items(), key=lambda _x: -_x[1])[:8]
        _out.append("top_types_by_BYTES(shallow): "
                    + " ".join(f"{_t}={_b/1e6:.0f}MB" for _t, _b in _top_b))
        _objs = None
    except Exception as _e:
        _out.append(f"py_objects_failed={type(_e).__name__}")

    # ── 4. Streamlit accumulation: live sessions + cache_data entries
    try:
        from streamlit.runtime import get_instance as _gi
        _rt = _gi()
        _ns = "?"
        try:
            _ns = len(_rt._session_mgr.list_sessions())
        except Exception:
            try:
                _ns = len(list(_rt._session_mgr._session_info_by_id))
            except Exception:
                pass
        _out.append(f"streamlit_sessions={_ns}")
    except Exception:
        pass
    try:
        from streamlit.runtime.caching import cache_data as _cd
        _fc = getattr(getattr(_cd, "_data_caches", None), "_function_caches", {}) or {}
        _n_fns = len(_fc)
        _n_entries = 0
        for _c in _fc.values():
            try:
                _mc = getattr(_c, "_mem_cache", None) or getattr(_c, "cache", None)
                _n_entries += len(_mc) if _mc is not None else 0
            except Exception:
                pass
        _out.append(f"st_cache_data functions={_n_fns} entries={_n_entries}")
    except Exception:
        pass

    # ── 5. tracemalloc: the exact allocation SITES (opt-in, has overhead)
    try:
        if os.getenv("MEM_TRACEMALLOC", "0").strip().lower() in ("1", "true", "yes", "on"):
            import tracemalloc as _tm
            if _tm.is_tracing():
                _snap = _tm.take_snapshot()
                for _stat in _snap.statistics("lineno")[:12]:
                    _fr = _stat.traceback[0]
                    _out.append(f"  ALLOC {_stat.size/1e6:.1f}MB count={_stat.count:,} "
                                f"{_fr.filename.split('/')[-1]}:{_fr.lineno}")
            else:
                _out.append("  tracemalloc=ENABLED_BUT_NOT_TRACING (call tracemalloc.start() at boot)")
    except Exception:
        pass

    log_warning(" | ".join(_out))


def log_render_complete(page: str, render_s: float, tab: str = "", data_status: str = "") -> None:
    try:
        _rss, _u, _lim, _av = ram_snapshot_mb()
        _flag = ""
        if render_s >= 2.0:
            _flag = " | SLOW"
            if _av is not None and _av < 800:
                _flag = " | SLOW<-LOW-MEM"
        _rss_s = "?" if _rss is None else f"{_rss:.0f}"
        _av_s = "?" if _av is None else f"{_av:.0f}"
        _tab_s = f" tab={tab}" if tab else ""
        _d_s = f" data={data_status}" if data_status else ""
        log_warning(
            f"[CLICK->RENDER] page={page}{_tab_s} render={render_s:.2f}s"
            f" rss={_rss_s}MB avail={_av_s}MB{_d_s}{_flag}"
        )
        if render_s >= 0.5 and os.getenv("SERVER_TRIM_ON_RENDER", "1").strip().lower() in ("1", "true", "yes", "on"):
            _malloc_trim()
    except Exception:
        pass


def get_server_logger():
    """Get or create the server logger with QueueHandler for non-blocking I/O."""
    global _queue_listener

    ensure_log_dir()

    logger = logging.getLogger("server_logger")

    if not logger.handlers:
        with _queue_listener_lock:
            if not logger.handlers:
                logger.setLevel(logging.WARNING)
                logger.propagate = False

                # RETENTION POLICY: never delete logs. Default = DAILY rotation with
                # backupCount=0 → every day's log is kept forever in /home (persistent,
                # ~500GB). At midnight server-log.log rolls to server-log.log.YYYY-MM-DD
                # and a fresh file starts; nothing is ever purged. SERVER_LOG_ROTATION=size
                # reverts to the old size scheme; SERVER_LOG_BACKUPS>0 caps retention
                # (leave 0 = keep everything).
                _backups = int(os.getenv("SERVER_LOG_BACKUPS", "0"))
                _max_bytes = int(os.getenv("SERVER_LOG_MAX_BYTES", str(25 * 1024 * 1024)))
                if os.getenv("SERVER_LOG_ROTATION", "daily").strip().lower() == "size":
                    fh = logging.handlers.RotatingFileHandler(
                        SERVER_LOG_FILE, mode='a', maxBytes=_max_bytes,
                        backupCount=_backups, encoding='utf-8'
                    )
                    _rotate_desc = f"size {_max_bytes // (1024 * 1024)}MB x{_backups or 'ALL'}"
                else:
                    fh = logging.handlers.TimedRotatingFileHandler(
                        SERVER_LOG_FILE, when='midnight', backupCount=_backups,
                        encoding='utf-8', utc=False
                    )
                    _rotate_desc = f"daily x{_backups or 'ALL-kept'}"
                fh.setLevel(logging.WARNING)
                formatter = ISTFormatter(
                    '%(asctime)s | %(levelname)-8s | %(rerun_id)-8s | page=%(page)-15s'
                    ' | user=%(user_email)-30s | %(filename)s:%(lineno)d:%(funcName)s'
                    ' | %(message)s'
                )
                fh.setFormatter(formatter)

                log_queue: Queue = Queue(-1)
                queue_handler = logging.handlers.QueueHandler(log_queue)
                queue_handler.setLevel(logging.WARNING)
                queue_handler.addFilter(ContextEnrichFilter())
                logger.addHandler(queue_handler)

                _queue_listener = logging.handlers.QueueListener(
                    log_queue, fh, respect_handler_level=True
                )
                _queue_listener.start()

                _persist = "yes" if str(SERVER_LOGS_DIR).startswith("/home") else "no (EPHEMERAL)"
                _arena = os.getenv("MALLOC_ARENA_MAX", "<UNSET>")
                _defaults_s = (
                    ", ".join(_MDP_BOOT_APPLIED)
                    if _MDP_BOOT_APPLIED
                    else "all pre-set by environment"
                )
                logger.warning(
                    f"[BOOT] server_logger up | pid={os.getpid()} | log_dir={SERVER_LOGS_DIR} "
                    f"| persistent={_persist} | rotate={_rotate_desc} "
                    f"| MALLOC_ARENA_MAX={_arena} | defaults_applied=[{_defaults_s}]"
                )
                logger.warning(_build_stamp())
                _write_restart_ledger()
                _start_heartbeat()

    return logger


# ============================================================================
# BASIC LOGGING FUNCTIONS
# stacklevel is propagated so %(filename)s/%(funcName)s show the CALLER,
# not server_logger.py itself.
#   log_info(msg)           → stacklevel=2 → frame calling log_info     ✓
#   log_timing → log_info   → stacklevel=3 → frame calling log_timing   ✓
# ============================================================================

def _is_timing_msg(message: str) -> bool:
    """Check if message is a timing/performance message that always passes through."""
    return (
        message.startswith("[TIMING]")
        or message.startswith("[PAGE_")
        or message.startswith("[DB_")
        or message.startswith("[AUTH_")
    )


def log_info(message: str, _stacklevel: int = 2):
    """Log an INFO level message. Timing-tagged messages always pass through.

    FIX (2026-03-31): Timing messages must be emitted at WARNING level to bypass
    the logger.setLevel(WARNING) gate. Previously, logger.info() was silently
    dropped by Python's logging framework before reaching any handler.
    """
    if _is_timing_msg(message):
        # Emit at WARNING level so the message passes logger.setLevel(WARNING).
        # The [TIMING]/[PAGE_]/[DB_]/[AUTH_] prefix in the message body
        # distinguishes these from real warnings during log analysis.
        get_server_logger().warning(message, stacklevel=_stacklevel)
        return
    if _EFFECTIVE_LEVEL > logging.INFO:
        return
    if _TIMING_ONLY:
        return
    get_server_logger().info(message, stacklevel=_stacklevel)


def log_debug(message: str, _stacklevel: int = 2):
    """Log a DEBUG level message. Suppressed if APP_LOG_LEVEL > DEBUG."""
    if _EFFECTIVE_LEVEL > logging.DEBUG:
        return
    if _TIMING_ONLY:
        return
    get_server_logger().debug(message, stacklevel=_stacklevel)


def log_warning(message: str, _stacklevel: int = 2):
    """Log a WARNING level message. Always passes through."""
    get_server_logger().warning(message, stacklevel=_stacklevel)


def log_error(message: str, _stacklevel: int = 2):
    """Log an ERROR level message. Always passes through."""
    get_server_logger().error(message, stacklevel=_stacklevel)


def log_exception(message: str, _stacklevel: int = 2):
    """Log an ERROR level message with exception info. Always passes through."""
    get_server_logger().exception(message, stacklevel=_stacklevel)


def log_critical(message: str, _stacklevel: int = 2):
    """Log a CRITICAL level message. Always passes through."""
    get_server_logger().critical(message, stacklevel=_stacklevel)


def log_structured_error(
    exc: Exception,
    *,
    page: str = "",
    component: str = "",
    operation: str = "",
    context: str = "",
    _stacklevel: int = 2,
) -> str:
    """Log a structured error with exception type, origin frame, and compact traceback.

    Returns:
        A compact origin string ``"file.py:func_name:42"`` for traceability.

    Log line format::

        [STRUCTURED_ERROR] page=newsroom | component=render_cards
        | op=DB_FETCH | type=ConnectionError | msg=Connection refused
        | file=repository.py:get_articles:882 | tb=...
    """
    tb = exc.__traceback__
    origin_file, origin_func, origin_line = "?", "?", "?"
    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        origin_file = os.path.basename(tb.tb_frame.f_code.co_filename)
        origin_func = tb.tb_frame.f_code.co_name
        origin_line = str(tb.tb_lineno)

    parts = ["[STRUCTURED_ERROR]"]
    if page:
        parts.append(f"page={page}")
    if component:
        parts.append(f"component={component}")
    if operation:
        parts.append(f"op={operation}")
    parts.append(f"type={type(exc).__name__}")
    parts.append(f"msg={str(exc)[:200]}")
    parts.append(f"file={origin_file}:{origin_func}:{origin_line}")
    if context:
        parts.append(f"ctx={context}")

    tb_lines = _tb.format_exception(type(exc), exc, exc.__traceback__)
    tb_compact = "".join(tb_lines[-5:]).replace("\n", " | ").strip()
    if len(tb_compact) > 500:
        tb_compact = tb_compact[:500] + "..."

    msg = " | ".join(parts) + f" | tb={tb_compact}"
    log_error(msg, _stacklevel=_stacklevel + 1)
    _origin = f"{origin_file}:{origin_func}:{origin_line}"
    # Mirror into the user-analytics feed so failures are visible per-user/session
    # (rate-limited per origin). Best-effort — never affects the caller.
    _emit_analytics_error(
        page=page, component=component, operation=operation,
        error_type=type(exc).__name__, message=str(exc)[:200],
        origin=_origin, context=context,
    )
    return _origin


@contextmanager
def error_boundary(page: str = "", component: str = "", operation: str = "", reraise: bool = False):
    """Context manager that catches and logs any exception inside the block.

    Usage::

        with error_boundary(page="login", component="render_form"):
            render_form()
    """
    try:
        yield
    except Exception as exc:
        log_structured_error(exc, page=page, component=component, operation=operation, _stacklevel=3)
        if reraise:
            raise


def safe_execute(func, *args, page: str = "", component: str = "", fallback=None, **kwargs):
    """Execute *func* safely; on failure log a structured error and return *fallback*.

    Never raises.

    Usage::

        data = safe_execute(fetch_articles, date_from, date_to,
                            page="newsroom", component="DATA_LOAD", fallback=[])
    """
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        log_structured_error(
            exc,
            page=page,
            component=component,
            operation=getattr(func, "__name__", str(func)),
            _stacklevel=3,
        )
        return fallback


# ============================================================================
# USER ANALYTICS — separate JSON-lines stream (non-blocking)
# ============================================================================
_analytics_logger: Optional[logging.Logger] = None
_analytics_listener = None
_analytics_lock = threading.Lock()


def get_analytics_logger():
    global _analytics_logger, _analytics_listener
    if _analytics_logger is not None:
        return _analytics_logger
    with _analytics_lock:
        if _analytics_logger is not None:
            return _analytics_logger
        ensure_log_dir()
        lg = logging.getLogger("mdp_user_analytics")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        _file = SERVER_LOGS_DIR / "user_analytics.log"
        # Same retention policy as the server log: DAILY rotation, keep every day
        # forever (backupCount=0) — analytics history is never purged either.
        _bk = int(os.getenv("ANALYTICS_LOG_BACKUPS", "0"))
        if os.getenv("SERVER_LOG_ROTATION", "daily").strip().lower() == "size":
            _mb = int(os.getenv("ANALYTICS_LOG_MAX_BYTES", str(50 * 1024 * 1024)))
            fh = logging.handlers.RotatingFileHandler(
                _file, mode='a', maxBytes=_mb, backupCount=_bk, encoding='utf-8'
            )
        else:
            fh = logging.handlers.TimedRotatingFileHandler(
                _file, when='midnight', backupCount=_bk, encoding='utf-8', utc=False
            )
        fh.setFormatter(logging.Formatter('%(message)s'))
        _q: Queue = Queue(-1)
        qh = logging.handlers.QueueHandler(_q)
        lg.addHandler(qh)
        _analytics_listener = logging.handlers.QueueListener(_q, fh, respect_handler_level=True)
        _analytics_listener.start()
        _analytics_logger = lg
        return lg


# ---------------------------------------------------------------------------
# ANALYTICS ENVELOPE — constants + client/device context (computed once)
# ---------------------------------------------------------------------------
_ANALYTICS_SCHEMA_VERSION = 2
_APP_ENV = (os.getenv("APP_ENV", "local") or "local").strip().lower()


def _resolve_app_version() -> str:
    """Best-effort build id: explicit env wins; else read the git HEAD sha ONCE
    (no subprocess); else 'unknown'. Lets analytics correlate behavior with a release."""
    v = (os.getenv("APP_VERSION") or os.getenv("GIT_SHA") or os.getenv("BUILD_ID") or "").strip()
    if v:
        return v[:40]
    try:
        _root = Path(__file__).resolve().parents[2]  # app/utils/ -> repo root
        head = (_root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return (_root / ".git" / ref).read_text().strip()[:12]
        return head[:12]
    except Exception:
        return "unknown"


_APP_VERSION = _resolve_app_version()


def _parse_user_agent(ua: str) -> dict:
    """Dependency-free browser/os/device classification from a User-Agent string."""
    ua_l = (ua or "").lower()
    is_bot = any(k in ua_l for k in ("bot", "spider", "crawler", "headless", "python-requests", "curl"))
    is_tablet = ("ipad" in ua_l) or ("tablet" in ua_l)
    is_mobile = (not is_tablet) and any(k in ua_l for k in ("iphone", "android", "mobile", "ipod"))
    device = "bot" if is_bot else ("tablet" if is_tablet else ("mobile" if is_mobile else "desktop"))
    if "windows" in ua_l:
        os_name = "Windows"
    elif "iphone" in ua_l or "ipad" in ua_l or "; ios" in ua_l:
        os_name = "iOS"
    elif "mac os" in ua_l or "macintosh" in ua_l:
        os_name = "macOS"
    elif "android" in ua_l:
        os_name = "Android"
    elif "linux" in ua_l or "x11" in ua_l:
        os_name = "Linux"
    else:
        os_name = "other"
    if "edg" in ua_l:
        br = "Edge"
    elif "opr" in ua_l or "opera" in ua_l:
        br = "Opera"
    elif "chrome" in ua_l and "chromium" not in ua_l:
        br = "Chrome"
    elif "firefox" in ua_l or "fxios" in ua_l:
        br = "Firefox"
    elif "safari" in ua_l:
        br = "Safari"
    else:
        br = "other"
    return {"browser": br, "os": os_name, "device": device}


def _get_client_context() -> dict:
    """Parse {browser, os, device} from this session's UA header. Cached per session
    (parsed once). Best-effort; returns {} off the script thread."""
    try:
        import streamlit as st
        cached = st.session_state.get("_analytics_client_ctx")
        if cached is not None:
            return cached
        ua = ""
        try:
            h = st.context.headers
            ua = h.get("User-Agent", "") or h.get("user-agent", "") or ""
        except Exception:
            pass
        ctx = _parse_user_agent(ua)
        st.session_state["_analytics_client_ctx"] = ctx
        return ctx
    except Exception:
        return {}


def _analytics_should_emit(ns: str, val) -> bool:
    """Session-scoped 'changed since last time' gate — returns True to emit.

    Suppresses re-renders of the SAME download/search/action across Streamlit reruns,
    but re-emits when the value genuinely changes. Off-thread (no session_state) → True."""
    try:
        import streamlit as st
        d = st.session_state.get("_analytics_dedupe")
        if d is None:
            d = {}
            st.session_state["_analytics_dedupe"] = d
        if d.get(ns) == val:
            return False
        d[ns] = val
        return True
    except Exception:
        return True


def log_user_event(event: str, user: str = "", session: str = "", **fields) -> None:
    try:
        import json
        import uuid
        # ALWAYS stamp the best-available identity so every event is attributable to a
        # specific user — critical with many concurrent MDP users. If the caller didn't
        # pass a user, resolve it per-request (session/cookie). user_session is the
        # STABLE auth session id (groups one user's events); `session` stays the
        # per-rerun correlation id.
        _email = (user or "").strip() or _get_user_email()
        _usess = _get_session_id()
        rec = {
            "ts": datetime.now(tz=_IST).isoformat(),
            "event": event,
            "event_id": uuid.uuid4().hex[:16],
            "schema": _ANALYTICS_SCHEMA_VERSION,
            "env": _APP_ENV,
            "app_version": _APP_VERSION,
            "user": "" if _email in ("", "-") else _email,
            "user_session": _usess or None,
            "session": session or get_rerun_id(),
            "page": _get_page_context(),
        }
        # Device/browser/os segmentation — cached per session, best-effort.
        try:
            for _ck, _cv in _get_client_context().items():
                rec.setdefault(_ck, _cv)
        except Exception:
            pass
        for _k, _v in fields.items():
            if _v is None:
                continue
            if isinstance(_v, (set, tuple)):
                _v = list(_v)
            rec[_k] = _v
        get_analytics_logger().info(json.dumps(rec, default=str, ensure_ascii=False))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ANALYTICS EVENT HELPERS — session lifecycle, downloads, searches, actions
# All best-effort, non-blocking; thin wrappers over log_user_event().
# ---------------------------------------------------------------------------
def track_login(user: str = "", method: str = "oidc", success: bool = True, reason: str = "") -> None:
    """Emit `login` (success) or `login_failed`."""
    try:
        log_user_event(
            "login" if success else "login_failed",
            user=user, method=(method or None), reason=(reason or None),
        )
    except Exception:
        pass


def track_logout(user: str = "", route: str = "") -> None:
    """Emit `logout` with the routing path (logout_bridge / local)."""
    try:
        log_user_event("logout", user=user, route=(route or None))
    except Exception:
        pass


def track_download(kind: str, name: str = "", page: str = "", **props) -> None:
    """Emit a `download` event (transcript / filing / export). Deduped per (kind,name)
    within a session so button re-renders across reruns don't spam the feed."""
    try:
        if not _analytics_should_emit(f"dl:{kind}", name or kind):
            return
    except Exception:
        pass
    try:
        log_user_event("download", kind=kind, name=(name or None),
                       page_title=(page or None), **props)
    except Exception:
        pass


def track_search(page: str, query: str, results=None, kind: str = "keyword", **props) -> None:
    """Emit a `search` event with the ACTUAL query text + result count. Deduped per
    (page,kind) on (query,results) so an identical re-render doesn't repeat, but a new
    or repeated-after-different search does."""
    try:
        q = (query or "").strip()
        if not q:
            return
        if not _analytics_should_emit(f"search:{page}:{kind}", (q, results)):
            return
        log_user_event("search", page_title=page, kind=kind, query=q[:200],
                       results=(results if isinstance(results, int) else None), **props)
    except Exception:
        pass


def track_action(action: str, page: str = "", dedupe_key: str = "", **props) -> None:
    """Emit a generic `action` event (screen_run, watchlist_add, filing_open, …).
    Pass dedupe_key to suppress identical re-renders within a session."""
    try:
        if dedupe_key and not _analytics_should_emit(f"action:{action}", dedupe_key):
            return
    except Exception:
        pass
    try:
        log_user_event("action", action=action, page_title=(page or None), **props)
    except Exception:
        pass


# Process-level rate-limit for auto error events (avoid flooding on repeated reruns).
_ANALYTICS_ERR_LOCK = threading.Lock()
_ANALYTICS_ERR_SEEN: Dict[str, float] = {}


def _emit_analytics_error(page: str, component: str, operation: str,
                          error_type: str, message: str, origin: str, context: str) -> None:
    try:
        now = time.time()
        with _ANALYTICS_ERR_LOCK:
            if now - _ANALYTICS_ERR_SEEN.get(origin, 0.0) < 5.0:
                return
            _ANALYTICS_ERR_SEEN[origin] = now
            if len(_ANALYTICS_ERR_SEEN) > 500:
                _ANALYTICS_ERR_SEEN.clear()
        log_user_event(
            "error",
            page_title=(page or None), component=(component or None),
            operation=(operation or None), error_type=error_type,
            message=message, origin=origin, ctx=(context or None),
        )
    except Exception:
        pass


def log_filters_if_changed(page: str, **filters) -> None:
    """Emit a `filter_change` analytics event when a page's filter state changes.

    Complements the URL-based page_view capture for filters kept in SESSION-STATE
    (screening criteria, newsroom keyword/sector, earnings_calls year/quarter,
    market_data currency/units, calendar toggles) — so EVERY filter is captured, not
    only the URL-encoded ones. Deduped per page so it fires only on an actual change,
    and best-effort / non-blocking (never raises into the page render).
    """
    try:
        import streamlit as st
        cur = {}
        for _k, _v in filters.items():
            if _v in (None, "", [], ()):
                continue
            if isinstance(_v, (set, tuple)):
                _v = list(_v)
            cur[_k] = _v
        _key = f"_analytics_filters_{page}"
        if st.session_state.get(_key) == cur:
            return  # unchanged — do not emit
        _first = _key not in st.session_state
        st.session_state[_key] = cur
        log_user_event(
            "filter_change",
            user=_get_user_email() if _get_user_email() != "-" else "",
            page_title=page,
            filters=cur or None,
            reason=("first-load" if _first else "filter-change"),
        )
    except Exception:
        pass


# ============================================================================
# RAM MONITORING
# ============================================================================

def _read_int_file(path):
    try:
        with open(path) as _f:
            return int(_f.read().strip())
    except Exception:
        return None


def ram_snapshot_mb():
    rss = None
    try:
        with open("/proc/self/status") as _f:
            for _l in _f:
                if _l.startswith("VmRSS:"):
                    rss = int(_l.split()[1]) / 1024.0
                    break
    except Exception:
        pass
    _MB = 1024.0 ** 2
    total_mb = avail_mb = None
    try:
        _mi = {}
        with open("/proc/meminfo") as _f:
            for _l in _f:
                if _l.startswith("MemTotal:"):
                    _mi["t"] = int(_l.split()[1]) / 1024.0
                elif _l.startswith("MemAvailable:"):
                    _mi["a"] = int(_l.split()[1]) / 1024.0
                if len(_mi) == 2:
                    break
        total_mb, avail_mb = _mi.get("t"), _mi.get("a")
    except Exception:
        pass
    if total_mb is None:
        _lim = _read_int_file("/sys/fs/cgroup/memory.max") or _read_int_file(
            "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        )
        if _lim is not None and _lim < 256 * 1024 ** 3:
            total_mb = _lim / _MB
    used_mb = (total_mb - avail_mb) if (total_mb is not None and avail_mb is not None) else None
    return rss, used_mb, total_mb, avail_mb


def log_ram(tag: str = "") -> None:
    try:
        rss, used, total, avail = ram_snapshot_mb()
        anon = fdrss = None
        try:
            with open("/proc/self/status") as _f:
                for _l in _f:
                    if _l.startswith("RssAnon:"):
                        anon = int(_l.split()[1]) / 1024.0
                    elif _l.startswith("RssFile:"):
                        fdrss = int(_l.split()[1]) / 1024.0
        except Exception:
            pass
        _p = [f"[RAM]{(' ' + tag) if tag else ''}"]
        if rss is not None:
            _p.append(f"app_rss={rss:.0f}MB")
        if anon is not None:
            _p.append(f"rssAnon={anon:.0f}MB")
        if fdrss is not None:
            _p.append(f"rssFile={fdrss:.0f}MB")
        if used is not None:
            _p.append(f"used={used:.0f}MB")
        if total is not None:
            _p.append(f"total={total:.0f}MB")
        if avail is not None and total:
            _p.append(f"available={avail:.0f}MB ({100.0 * avail / total:.0f}% free)")
        log_warning(" | ".join(_p), _stacklevel=3)
    except Exception:
        pass


# ============================================================================
# PERFORMANCE TIMING FUNCTIONS
# ============================================================================

def log_timing(operation: str, elapsed_ms: float, details: str = "", level: str = "INFO"):
    """Log a timing event.

    The rerun_id is injected by ContextEnrichFilter — do NOT embed it manually.
    File/function shown in log will be the caller of log_timing (stacklevel=3).

    Format produced:
        [TIMING] AUTH_RETRY_SCHEDULED | 500.00ms | attempt=2/5 delay=1.0s
    """
    msg = f"[TIMING] {operation} | {elapsed_ms:.2f}ms"
    if details:
        msg += f" | {details}"

    # stacklevel=3: skip log_timing frame + log_XXX frame → shows original caller
    if level == "DEBUG":
        log_debug(msg, _stacklevel=3)
    elif level == "WARNING":
        log_warning(msg, _stacklevel=3)
    elif level == "ERROR":
        log_error(msg, _stacklevel=3)
    else:
        log_info(msg, _stacklevel=3)

def log_data_volume(
    table: str,
    rows: int,
    cols: int = 0,
    operation: str = "",
    details: str = "",
) -> None:
    """Log large result sets for STG diagnosis ([DATA_VOLUME] tag)."""
    if rows < 1000:
        return
    parts = [f"[DATA_VOLUME] table={table}", f"rows={rows}"]
    if cols > 0:
        parts.append(f"cols={cols}")
    if operation:
        parts.append(f"op={operation}")
    if details:
        parts.append(details)
    log_warning(" | ".join(parts), _stacklevel=3)


def log_db_timing(query_type: str, table: str, elapsed_ms: float, rows: int = -1, ticker: str = ""):
    """Log database query timing."""
    details = f"table={table}"
    if rows >= 0:
        details += f" rows={rows}"
    if ticker:
        details += f" ticker={ticker}"
    level = "WARNING" if elapsed_ms > 500 else "INFO"
    # stacklevel=4: log_db_timing → log_timing → log_XXX → logger.info → caller shown
    msg = f"[TIMING] DB_{query_type} | {elapsed_ms:.2f}ms | {details}"
    if level == "WARNING":
        log_warning(msg, _stacklevel=3)
    else:
        log_info(msg, _stacklevel=3)
    if rows >= 1000:
        log_data_volume(table, rows, operation=query_type, details=details)

def log_render_timing(component: str, elapsed_ms: float, ticker: str = "", details: str = ""):
    """Log component rendering timing."""
    extra = ""
    if ticker:
        extra += f" ticker={ticker}"
    if details:
        extra += f" {details}"
    level = "WARNING" if elapsed_ms > 1000 else "INFO"
    msg = f"[TIMING] RENDER_{component} | {elapsed_ms:.2f}ms{extra.strip() and ' | ' + extra.strip()}"
    if level == "WARNING":
        log_warning(msg, _stacklevel=3)
    else:
        log_info(msg, _stacklevel=3)


def log_function_timing(func_name: str, elapsed_ms: float, args_summary: str = ""):
    """Log function execution timing."""
    details = f" | {args_summary}" if args_summary else ""
    level = "WARNING" if elapsed_ms > 200 else "DEBUG"
    msg = f"[TIMING] FUNC_{func_name} | {elapsed_ms:.2f}ms{details}"
    if level == "WARNING":
        log_warning(msg, _stacklevel=3)
    else:
        log_debug(msg, _stacklevel=3)

# ============================================================================
# CONTEXT MANAGERS FOR AUTOMATIC TIMING
# ============================================================================

@contextmanager
def timed_operation(operation: str, details: str = "", log_level: str = "INFO"):
    """
    Context manager for timing a block of code.

    Usage:
        with timed_operation("DATA_LOAD", "ticker=AAPL"):
            data = load_data()
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_timing(operation, elapsed_ms, details, log_level)

@contextmanager
def timed_db_query(query_type: str, table: str, ticker: str = ""):
    """
    Context manager for timing database queries.

    Usage:
        with timed_db_query("SELECT", "coreiq_av_financials", "AAPL"):
            results = db.execute_query(query, params)
    """
    start = time.perf_counter()
    rows = -1
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_db_timing(query_type, table, elapsed_ms, rows, ticker)

@contextmanager
def timed_render(component: str, ticker: str = ""):
    """
    Context manager for timing component rendering.

    Usage:
        with timed_render("INCOME_STATEMENT", "AAPL"):
            render_income_statement(...)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log_render_timing(component, elapsed_ms, ticker)

# ============================================================================
# DECORATORS FOR AUTOMATIC TIMING
# ============================================================================

def timed_function(log_args: bool = False, max_arg_length: int = 50):
    """
    Decorator to automatically time function execution.

    Usage:
        @timed_function(log_args=True)
        def get_company_data(ticker: str):
            return db.query(...)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            func_name = func.__name__

            # Build args summary if enabled
            args_summary = ""
            if log_args:
                args_str = ", ".join([
                    str(a)[:max_arg_length] for a in args if a is not None
                ])
                kwargs_str = ", ".join([
                    f"{k}={str(v)[:max_arg_length]}"
                    for k, v in kwargs.items() if v is not None
                ])
                all_args = ", ".join(filter(None, [args_str, kwargs_str]))
                args_summary = all_args[:100]  # Limit total length

            try:
                result = func(*args, **kwargs)
                return result
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000
                log_function_timing(func_name, elapsed_ms, args_summary)

        return wrapper
    return decorator

def timed_db_operation(query_type: str, table: str):
    """
    Decorator to time database operations.

    Usage:
        @timed_db_operation("SELECT", "coreiq_av_financials_income_statement")
        def get_income_data(ticker: str):
            return db.query(...)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Try to extract ticker from first arg if it's a string
            ticker = ""
            if args and isinstance(args[0], str):
                ticker = args[0]

            with timed_db_query(query_type, table, ticker):
                return func(*args, **kwargs)

        return wrapper
    return decorator

# ============================================================================
# PAGE-LEVEL TIMING HELPERS
# ============================================================================

class PageTimer:
    """
    Helper class for timing page load and render phases.

    Usage:
        timer = PageTimer("market_data")
        timer.start_phase("db_fetch")
        data = fetch_data()
        timer.end_phase("db_fetch")

        timer.start_phase("render")
        render_page()
        timer.end_phase("render")

        timer.summary()  # Logs total and breakdown
    """

    def __init__(self, page_name: str, ticker: str = ""):
        self.page_name = page_name
        self.ticker = ticker
        self.phases: Dict[str, Dict[str, Any]] = {}
        self.current_phase: Optional[str] = None
        self.start_time = time.perf_counter()

    def start_phase(self, phase_name: str):
        """Start timing a phase."""
        self.current_phase = phase_name
        self.phases[phase_name] = {
            'start': time.perf_counter(),
            'end': None,
            'elapsed_ms': None
        }

    def end_phase(self, phase_name: Optional[str] = None):
        """End timing a phase."""
        phase = phase_name or self.current_phase
        if phase and phase in self.phases:
            self.phases[phase]['end'] = time.perf_counter()
            elapsed = self.phases[phase]['end'] - self.phases[phase]['start']
            self.phases[phase]['elapsed_ms'] = elapsed * 1000

    def summary(self):
        """Log summary of all phases."""
        total_elapsed = (time.perf_counter() - self.start_time) * 1000

        breakdown = " | ".join([
            f"{name}={data['elapsed_ms']:.1f}ms"
            for name, data in self.phases.items()
            if data['elapsed_ms'] is not None
        ])

        ticker_str = f" ticker={self.ticker}" if self.ticker else ""
        msg = (
            f"[TIMING] PAGE_SUMMARY | {total_elapsed:.1f}ms"
            f" | page={self.page_name}{ticker_str}"
            f"{' | ' + breakdown if breakdown else ''}"
        )

        if total_elapsed > 2000:
            log_warning(msg, _stacklevel=3)
        else:
            log_info(msg, _stacklevel=3)

# ============================================================================
# FILE OPERATIONS
# ============================================================================

def get_log_content(max_lines: int = 1000, max_bytes: int = 500000) -> str:
    """Get the current log file content."""
    ensure_log_dir()

    if not SERVER_LOG_FILE.exists():
        return "No logs yet. Start using the app to generate logs."

    try:
        # Read file
        with open(SERVER_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(max_bytes)

        # Get last N lines
        lines = content.split('\n')
        if len(lines) > max_lines:
            lines = lines[-max_lines:]

        return '\n'.join(lines)
    except Exception as e:
        return f"Error reading logs: {e}"

def clear_logs() -> bool:
    """DISABLED by retention policy — logs are NEVER deleted.

    Retention policy (2026-07): server + analytics logs rotate at midnight and are
    kept FOREVER in persistent /home (server-log.log.YYYY-MM-DD). The old behaviour
    (unlink every rotated segment + truncate the live file) permanently destroyed
    history, so it is disabled. This is a non-destructive no-op — every byte is
    preserved. Use Download Logs to export; browse the dated files for history.
    """
    try:
        logging.getLogger("server_logger").warning(
            "[RETENTION] clear_logs() requested but IGNORED — log deletion is "
            "disabled; all server/analytics logs are retained in /home per policy."
        )
    except Exception:
        pass
    return False

def _rotated_log_files() -> list:
    """All log segments oldest→newest, for Download. Matches BOTH suffix styles:
    daily rotation → date (server-log.log.2026-07-18); size rotation → numeric
    (server-log.log.1). Without the date match, downloads would silently omit the
    retained daily history."""
    import re as _re
    base = SERVER_LOG_FILE
    dated, numbered = [], []
    try:
        for p in base.parent.glob(base.name + ".*"):
            suffix = p.name[len(base.name) + 1:]
            if _re.match(r"^\d{4}-\d{2}-\d{2}", suffix):
                dated.append((suffix, p))
            elif suffix.isdigit():
                numbered.append((int(suffix), p))
    except Exception:
        pass
    ordered = [p for _, p in sorted(dated)]                    # oldest date first
    ordered += [p for _, p in sorted(numbered, reverse=True)]  # oldest .N first
    if base.exists():
        ordered.append(base)                                   # current (newest) last
    return ordered


def download_logs() -> bytes:
    ensure_log_dir()
    files = _rotated_log_files()
    if not files:
        return b"No logs available."
    cap = int(os.getenv("SERVER_LOG_DOWNLOAD_MAX_BYTES", str(80 * 1024 * 1024)))
    try:
        chunks = []
        for p in files:
            try:
                sep = f"\n===== {p.name} =====\n".encode("utf-8")
                with open(p, 'rb') as f:
                    chunks.append(sep + f.read())
            except Exception:
                continue
        blob = b"".join(chunks)
        if len(blob) > cap:
            blob = b"...[older log lines truncated to newest %dMB]...\n" % (cap // (1024 * 1024)) + blob[-cap:]
        return blob if blob else b"No logs available."
    except Exception as e:
        return f"Error reading logs: {e}".encode('utf-8')


def download_analytics_logs() -> bytes:
    ensure_log_dir()
    _base = SERVER_LOGS_DIR / "user_analytics.log"
    _files = []
    try:
        for p in _base.parent.glob(_base.name + ".*"):
            _suf = p.name[len(_base.name) + 1:]
            if _suf.isdigit():
                _files.append((int(_suf), p))
    except Exception:
        pass
    _ordered = [p for _, p in sorted(_files, key=lambda t: t[0], reverse=True)]
    if _base.exists():
        _ordered.append(_base)
    if not _ordered:
        return b"No analytics yet."
    _cap = int(os.getenv("ANALYTICS_DOWNLOAD_MAX_BYTES", str(80 * 1024 * 1024)))
    try:
        _chunks = []
        for p in _ordered:
            try:
                with open(p, 'rb') as f:
                    _chunks.append(f.read())
            except Exception:
                continue
        _blob = b"".join(_chunks)
        if len(_blob) > _cap:
            _blob = _blob[-_cap:]
        return _blob if _blob else b"No analytics yet."
    except Exception as e:
        return f"Error reading analytics: {e}".encode('utf-8')


def get_analytics_stats() -> dict:
    ensure_log_dir()
    _base = SERVER_LOGS_DIR / "user_analytics.log"
    try:
        _total = 0
        _lines = 0
        for p in _base.parent.glob(_base.name + "*"):
            try:
                _total += p.stat().st_size
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    _lines += sum(1 for _ in f)
            except Exception:
                pass
        return {'exists': _base.exists(), 'size': _total, 'events': _lines, 'path': str(_base)}
    except Exception as e:
        return {'exists': False, 'error': str(e)}


def get_log_stats() -> dict:
    ensure_log_dir()
    segments = _rotated_log_files()
    retained_bytes = 0
    for p in segments:
        try:
            retained_bytes += p.stat().st_size
        except Exception:
            pass

    if not SERVER_LOG_FILE.exists():
        return {
            'exists': False,
            'size': 0,
            'lines': 0,
            'modified': None,
            'segments': len(segments),
            'retained_size': retained_bytes,
            'dir': str(SERVER_LOGS_DIR),
            'persistent': str(SERVER_LOGS_DIR).startswith("/home"),
        }

    try:
        stat = SERVER_LOG_FILE.stat()
        with open(SERVER_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = sum(1 for _ in f)

        return {
            'exists': True,
            'size': stat.st_size,
            'lines': lines,
            'modified': datetime.fromtimestamp(stat.st_mtime, tz=_IST).strftime('%d-%b-%Y %I:%M %p IST'),
            'path': str(SERVER_LOG_FILE),
            'segments': len(segments),
            'retained_size': retained_bytes,
            'dir': str(SERVER_LOGS_DIR),
            'persistent': str(SERVER_LOGS_DIR).startswith("/home"),
        }
    except Exception as e:
        return {'exists': False, 'error': str(e)}

# ============================================================================
# PERFORMANCE ANALYSIS HELPERS
# ============================================================================

def analyze_slow_operations(log_lines: list = None) -> Dict[str, Any]:
    """
    Analyze logs to find slow operations.

    Returns:
        Dict with slowest DB queries, renders, and functions
    """
    if log_lines is None:
        content = get_log_content(max_lines=5000)
        log_lines = content.split('\n')

    slow_db_queries = []
    slow_renders = []
    slow_functions = []

    for line in log_lines:
        if '[TIMING]' in line:
            # Parse timing log
            try:
                # Extract operation and time
                parts = line.split('|')
                if len(parts) >= 3:
                    message = parts[2].strip()
                    if '[TIMING]' in message:
                        # Parse: [TIMING] OPERATION | TIMEms | details
                        timing_parts = message.split('|')
                        if len(timing_parts) >= 2:
                            op_parts = timing_parts[0].strip().split()
                            if len(op_parts) >= 2:
                                operation = op_parts[1]
                                time_str = timing_parts[1].strip()
                                if 'ms' in time_str:
                                    time_ms = float(time_str.replace('ms', '').strip())

                                    entry = {
                                        'operation': operation,
                                        'time_ms': time_ms,
                                        'details': timing_parts[2].strip() if len(timing_parts) > 2 else "",
                                        'line': line
                                    }

                                    if operation.startswith('DB_'):
                                        slow_db_queries.append(entry)
                                    elif operation.startswith('RENDER_'):
                                        slow_renders.append(entry)
                                    elif operation.startswith('FUNC_'):
                                        slow_functions.append(entry)
            except Exception:
                pass

    # Sort by time (descending)
    slow_db_queries.sort(key=lambda x: x['time_ms'], reverse=True)
    slow_renders.sort(key=lambda x: x['time_ms'], reverse=True)
    slow_functions.sort(key=lambda x: x['time_ms'], reverse=True)

    return {
        'slowest_db_queries': slow_db_queries[:10],
        'slowest_renders': slow_renders[:10],
        'slowest_functions': slow_functions[:10],
        'total_slow_operations': len(slow_db_queries) + len(slow_renders) + len(slow_functions)
    }


# ============================================================================
# COMPREHENSIVE PAGE LOAD SEQUENCE TRACKER (PRODUCT-GRADE)
# ============================================================================

class PageLoadTracker:
    """
    Product-grade page load sequence tracker.

    Tracks EVERY step of page load from the moment user clicks navigation
    to the final render completion. No step is missed.

    Usage in pages:
        from utils.server_logger import PageLoadTracker

        tracker = PageLoadTracker("market_data")
        tracker.step_start("HEADER_RENDER")
        render_header(...)
        tracker.step_end("HEADER_RENDER")

        tracker.step_start("DATA_FETCH")
        data = fetch_data()
        tracker.step_end("DATA_FETCH")

        tracker.finish()  # Logs complete breakdown

    Log Output Format:
        [PAGE_LOAD] market_data | STEP=HEADER_RENDER | elapsed=45.2ms
        [PAGE_LOAD] market_data | STEP=DATA_FETCH | elapsed=234.1ms
        [PAGE_LOAD] market_data | STEP=CONTENT_RENDER | elapsed=12.5ms
        [PAGE_LOAD] market_data | TOTAL=291.8ms | BREAKDOWN=HEADER:45.2,DATA:234.1,CONTENT:12.5
    """

    def __init__(self, page_name: str, ticker: str = "", user: str = ""):
        self.page_name = page_name
        self.ticker = ticker
        self.user = user
        self.steps = []
        self.current_step = None
        self.start_time = time.perf_counter()

        log_info(f"[PAGE_LOAD] === {page_name} | PAGE LOAD SEQUENCE START ===")

    def step_start(self, step_name: str, details: str = ""):
        """Start tracking a step."""
        self.current_step = {
            'name': step_name,
            'start': time.perf_counter(),
            'end': None,
            'elapsed_ms': None,
            'details': details
        }
        log_debug(f"[PAGE_LOAD] {self.page_name} | START {step_name}" + (f" | {details}" if details else ""))

    def step_end(self, step_name: str = None, details: str = ""):
        """End tracking a step."""
        step = self.current_step
        if step and (step_name is None or step['name'] == step_name):
            step['end'] = time.perf_counter()
            step['elapsed_ms'] = (step['end'] - step['start']) * 1000
            if details:
                step['details'] = details
            self.steps.append(step)

            log_info(f"[PAGE_LOAD] {self.page_name} | STEP={step['name']} | "
                    f"elapsed={step['elapsed_ms']:.2f}ms" +
                    (f" | {step['details']}" if step['details'] else ""))
            self.current_step = None

    def step(self, step_name: str, details: str = ""):
        """Context manager for a step."""
        class StepContext:
            def __init__(ctx_self, tracker, name, details):
                ctx_self.tracker = tracker
                ctx_self.name = name
                ctx_self.details = details

            def __enter__(ctx_self):
                ctx_self.tracker.step_start(ctx_self.name, ctx_self.details)
                return ctx_self

            def __exit__(ctx_self, exc_type, exc_val, exc_tb):
                ctx_self.tracker.step_end(ctx_self.name)
                return False

        return StepContext(self, step_name, details)

    def finish(self, status: str = "OK"):
        """Finish tracking and log complete summary."""
        total_elapsed = (time.perf_counter() - self.start_time) * 1000

        # Build breakdown string
        breakdown = ", ".join([
            f"{step['name']}:{step['elapsed_ms']:.1f}ms"
            for step in self.steps
        ])

        ticker_str = f" ticker={self.ticker}" if self.ticker else ""
        user_str = f" user={self.user}" if self.user else ""

        msg = (f"[PAGE_LOAD] {self.page_name}{ticker_str}{user_str} | "
               f"TOTAL={total_elapsed:.1f}ms | status={status} | BREAKDOWN={breakdown}")

        # Slow-page thresholds — always WARNING (never ERROR; slow != broken).
        # _stacklevel=3 skips log_warning() + finish() frames → shows the
        # actual page file (e.g. newsroom.py:render_newsroom) in the log.
        if total_elapsed > 3000:
            log_warning(f"[PAGE_LOAD_SLOW] {msg}", _stacklevel=3)
        elif total_elapsed > 1000:
            log_warning(f"[PAGE_LOAD_MEDIUM] {msg}", _stacklevel=3)
        else:
            log_info(msg, _stacklevel=3)

        log_info(f"[PAGE_LOAD] === {self.page_name} | PAGE LOAD SEQUENCE END ===")

        return {
            'page': self.page_name,
            'total_ms': total_elapsed,
            'steps': self.steps,
            'status': status
        }


# ============================================================================
# STREAMLIT-SPECIFIC PAGE LOAD WRAPPER
# ============================================================================

def track_page_load(page_name: str):
    """
    Decorator to automatically track page load sequence.

    Usage:
        @track_page_load("market_data")
        def render_page():
            render_header()
            render_content()
            render_footer()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            tracker = PageLoadTracker(page_name)

            try:
                # Track the main function execution
                tracker.step_start("PAGE_FUNCTION")
                result = func(*args, **kwargs)
                tracker.step_end("PAGE_FUNCTION")

                tracker.finish("OK")
                return result
            except Exception as e:
                tracker.finish(f"ERROR: {str(e)[:50]}")
                raise

        return wrapper
    return decorator


# ============================================================================
# NAVIGATION CLICK TRACKER
# ============================================================================

class NavigationTracker:
    """
    Tracks navigation clicks and page transitions.

    This helps identify:
    - Which navigation links are clicked
    - Time from click to page start
    - Time from click to page fully loaded
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self.nav_click_time = None
        self.source_page = None
        self.target_page = None

    def record_nav_click(self, source_page: str, target_page: str):
        """Record when user clicks navigation."""
        self.nav_click_time = time.perf_counter()
        self.source_page = source_page
        self.target_page = target_page

        log_info(f"[NAV_CLICK] {source_page} -> {target_page} | click_recorded")

    def record_page_start(self, page_name: str):
        """Record when target page starts loading."""
        if self.target_page == page_name and self.nav_click_time:
            elapsed_ms = (time.perf_counter() - self.nav_click_time) * 1000
            log_info(f"[NAV_TRANSITION] {self.source_page} -> {page_name} | "
                    f"time_to_start={elapsed_ms:.1f}ms")
            return elapsed_ms
        return None


# Global navigation tracker instance
nav_tracker = NavigationTracker()
