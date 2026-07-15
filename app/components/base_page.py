"""
Base page observability bootstrap — rerun tracing, RAM, user analytics.

Call bootstrap_page_observability(page_key) as the first line of each page entry
(or rely on main.py calling it before pg.run()).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

import streamlit as st

from utils.server_logger import (
    get_rerun_id,
    log_ram,
    log_user_event,
    log_warning,
    nav_tracker,
    new_rerun_id,
    ram_snapshot_mb,
    set_page_context,
)


def _patch_st_rerun_once() -> None:
    if getattr(st, "_mdp_rerun_traced", False):
        return
    try:
        _orig_rerun = st.rerun

        def _traced_rerun(*_a, **_k):
            try:
                _fr = sys._getframe(1)
                log_warning(
                    f"[RERUN_TRIGGER] {os.path.basename(_fr.f_code.co_filename)}:"
                    f"{_fr.f_lineno}:{_fr.f_code.co_name}"
                )
            except Exception:
                pass
            return _orig_rerun(*_a, **_k)

        st.rerun = _traced_rerun
        st._mdp_rerun_traced = True
    except Exception:
        pass


def _bg_malloc_trim() -> None:
    def _trim():
        try:
            import ctypes as _ct
            _ct.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

    try:
        threading.Thread(target=_trim, daemon=True, name="mdp_nav_trim").start()
    except Exception:
        pass


def bootstrap_page_observability(page_key: str, page_title: str = "") -> float:
    """Initialize rerun/RAM/user-analytics telemetry for one page script run.

    Returns perf_counter start time for log_render_complete at page end.
    """
    _page_start = time.perf_counter()
    new_rerun_id(page_key)
    set_page_context(page_key)
    _patch_st_rerun_once()

    _now = time.time()
    _rk, _lk = f"_rerun_count_{page_key}", f"_rerun_last_{page_key}"
    _n = st.session_state.get(_rk, 0) + 1
    _since = (_now - st.session_state[_lk]) * 1000 if _lk in st.session_state else -1.0
    st.session_state[_rk] = _n
    st.session_state[_lk] = _now
    _g = st.session_state.get("_rerun_count_global", 0) + 1
    st.session_state["_rerun_count_global"] = _g
    _gap = "first-load" if _since < 0 else f"{_since:.0f}ms"
    log_warning(
        f"[RERUN] page={page_key} | page_rerun#={_n} | session_rerun#={_g} | since_last={_gap}"
    )

    _prev = st.session_state.get("_mdp_prev_page_for_nav")
    if _prev and _prev != page_key:
        nav_tracker.record_nav_click(_prev, page_key)
        nav_tracker.record_page_start(page_key)
    st.session_state["_mdp_prev_page_for_nav"] = page_key

    if _since < 0 or _since > 1200.0:
        _bg_malloc_trim()

    if _since < 0 or _since > 1200.0:
        try:
            log_ram(f"page={page_key}")
            _rss, _u, _lim, _av = ram_snapshot_mb()
            _auth = st.session_state.get("auth_data") or {}
            log_user_event(
                "page_view",
                user=_auth.get("user_email", ""),
                session=str(st.session_state.get("session_id", "") or get_rerun_id()),
                page_title=page_title or page_key,
                rerun=_n,
                since_last_ms=None if _since < 0 else round(_since),
                rss_mb=None if _rss is None else round(_rss),
                avail_mb=None if _av is None else round(_av),
            )
        except Exception:
            pass

    return _page_start


def finish_page_observability(
    page_key: str,
    page_start: float,
    tab: str = "",
    data_status: str = "",
) -> None:
    from utils.server_logger import log_render_complete

    render_s = time.perf_counter() - page_start
    log_render_complete(page_key, render_s, tab=tab, data_status=data_status)
