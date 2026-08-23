"""
Segment-cache auto-refresh — in-app staleness-driven scheduler.

The screening segment caches (``coreiq_screening_segment_values_cache`` /
``coreiq_screening_segment_member_cache``) are derived from
``coreiq_filing_metrics_v5`` (14.4M rows). The existing lazy trigger only
rebuilds when the values cache is EMPTY, but it is a persistent DB table that
never empties — so it never auto-refreshed and drifted 24 days stale (STG,
18-Jul-2026; last built 2026-06-24) as new 10-K filings landed.

This starts ONE daemon thread that, on a poll interval, checks a cheap
append-only high-water mark (``MAX(id)`` on the v5 PK — instant) and rebuilds
only when it grew. Segment data comes from 10-K annual filings, so real
refreshes are rare (monthly-ish, clustered Feb-Apr); the gate makes idle ticks
effectively free.

Design goals (mirrors ``forecast_auto_refresh``):
  * Reuse the proven build (``build_segment_values_cache``) — no new classify logic.
  * Minimal RAM — one sleeping thread; the build holds ~41K tuples (~10MB), NOT
    the edgartools XBRL objects that drove the ratings-sweep 3.7GB spike.
  * Exactly-once across instances via an atomic conditional-UPDATE DB claim
    (inside ``refresh_segment_cache_if_stale``).
  * ON by default; disable with ``ENABLE_SEGMENT_CACHE_AUTO_REFRESH=0``.
  * Never blocks page render.

Spec: docs/superpowers/specs/2026-07-18-log-investigation-and-segment-cache-automation-design.md
"""
from __future__ import annotations

import os
import threading
import time

from utils.server_logger import log_info, log_structured_error


def _enabled() -> bool:
    # ON by default (unset → enabled). Disable explicitly with =0.
    return os.getenv("ENABLE_SEGMENT_CACHE_AUTO_REFRESH", "1").strip() not in ("0", "", "false", "False")


def _first_delay_s() -> int:
    """Stay out of the post-boot cold-cache window: right after a restart,
    interactive traffic already pays cold caches, and a minutes-long build
    competing for DB I/O then makes first clicks worse. Default 5 min."""
    try:
        return max(0, int(os.getenv("SEGMENT_CACHE_REFRESH_FIRST_DELAY_SEC", "300")))
    except ValueError:
        return 300


def _poll_s() -> int:
    """Check cadence. The MAX(id) gate is O(1), so a frequent poll is cheap — a
    real rebuild only fires when v5 grew. Default 6h; floored at 10 min."""
    try:
        return max(600, int(os.getenv("SEGMENT_CACHE_REFRESH_POLL_SEC", str(6 * 3600))))
    except ValueError:
        return 6 * 3600


def run_segment_cache_refresh_tick(force: bool = False):
    """One scheduler tick: rebuild the segment cache iff the source grew.

    Returns the action dict from ``refresh_segment_cache_if_stale`` (or None if
    the import/tick failed). ``force=True`` rebuilds immediately (admin/manual).
    """
    try:
        from data.screening_service import refresh_segment_cache_if_stale
        return refresh_segment_cache_if_stale(force=force)
    except Exception as exc:
        log_structured_error(
            exc, page="segment_cache_auto_refresh",
            component="run_segment_cache_refresh_tick", operation="tick",
        )
        return None


def start_segment_cache_auto_refresh() -> None:
    """Start ONE daemon thread that polls + rebuilds on staleness. No-op if disabled.

    Double-start guarded by the ``APP_SEG_CACHE_REFRESH_STARTED`` env flag in the
    caller (main.py), mirroring the other warmups.
    """
    if not _enabled():
        log_info("[segment_cache_auto_refresh] disabled (ENABLE_SEGMENT_CACHE_AUTO_REFRESH!=1)")
        return

    def _loop() -> None:
        first, poll = _first_delay_s(), _poll_s()
        log_info(
            f"[segment_cache_auto_refresh] thread started — first_delay={first}s poll={poll}s"
        )
        time.sleep(first)
        while True:
            try:
                res = run_segment_cache_refresh_tick()
                if res and res.get("action") == "built":
                    log_info(f"[segment_cache_auto_refresh] rebuilt: {res}")
            except Exception:
                pass
            time.sleep(poll)

    threading.Thread(target=_loop, daemon=True, name="segment-cache-refresh").start()
