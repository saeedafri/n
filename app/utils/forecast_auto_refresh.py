"""
Forecast auto-refresh — in-app twice-daily scheduler.

A single lightweight daemon thread wakes on a poll interval and, at each
configured local run time (default 06:00 & 18:00 IST), refreshes the annual and
quarterly forecasting models for every eligible company — reporting-date driven,
with `force=False`, so only companies whose stored forecast is stale vs. their
latest actuals actually run the engine.

Design goals (largest client — zero tolerance for errors):
  * Reuse the EXACT proven refresh path (`sync_all_eligible` /
    `sync_all_eligible_quarterly`) — no new forecasting logic, no new risk.
  * Minimal RAM — one sleeping thread; the engine runs one ticker at a time.
  * Exactly-once across app instances via an atomic DB claim
    (`INSERT IGNORE` into `coreiq_forecast_refresh_runs`, mirroring the proven
    `earnings_alert` delivery-log dedupe).
  * ON by default; disable with `ENABLE_FORECAST_AUTO_REFRESH=0`. Never blocks page render.

Spec: docs/superpowers/specs/2026-07-07-forecast-auto-refresh-design.md
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from core.database import db_manager
from utils.constants import QUARTERLY_FORECASTING_ENABLED
from utils.server_logger import log_error, log_info, log_structured_error, log_timing

try:  # zoneinfo is stdlib on Python 3.9+; degrade to UTC if the tz db is missing.
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


_TABLE = "coreiq_forecast_refresh_runs"
_TRIGGERED_BY = "auto-scheduler"

# In-process guards: never run two cycles at once inside one process, and only
# ensure the table once per process.
_run_lock = threading.Lock()
_running = False
_table_ready = False


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def _enabled() -> bool:
    # ON by default (unset → enabled). Disable explicitly with
    # ENABLE_FORECAST_AUTO_REFRESH=0.
    return os.getenv("ENABLE_FORECAST_AUTO_REFRESH", "1").strip() not in ("0", "", "false", "False")


def _email_enabled() -> bool:
    return os.getenv("FORECAST_AUTO_REFRESH_EMAIL", "1").strip() not in ("0", "", "false", "False")


def _run_times() -> List[str]:
    """Parsed, validated, sorted 'HH:MM' run times. Defaults to 06:00 & 18:00."""
    raw = os.getenv("FORECAST_AUTO_REFRESH_TIMES", "06:00,18:00")
    out: List[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            h, m = int(hh), int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                out.append(f"{h:02d}{m:02d}")
        except Exception:
            continue
    if not out:
        out = ["0600", "1800"]
    return sorted(set(out))


def _tz():
    name = os.getenv("FORECAST_AUTO_REFRESH_TZ", "Asia/Kolkata").strip()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def _now_local(now_utc: Optional[datetime] = None) -> datetime:
    base = now_utc or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(_tz())


# ---------------------------------------------------------------------------
# DB coordination table + atomic claim
# ---------------------------------------------------------------------------
def _ensure_runs_table() -> None:
    global _table_ready
    if _table_ready:
        return
    db_manager.execute_insert(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            run_key            VARCHAR(64)  NOT NULL,
            started_at         DATETIME     NOT NULL,
            finished_at        DATETIME     NULL,
            status             VARCHAR(32)  NOT NULL,
            annual_updated     INT          NOT NULL DEFAULT 0,
            annual_errors      INT          NOT NULL DEFAULT 0,
            quarterly_updated  INT          NOT NULL DEFAULT 0,
            quarterly_errors   INT          NOT NULL DEFAULT 0,
            detail             VARCHAR(500) NULL,
            PRIMARY KEY (run_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        {},
    )
    _table_ready = True


def _claim_window(run_key: str) -> bool:
    """Atomically claim a run window. True iff THIS process won the claim.

    `INSERT IGNORE` on the PRIMARY KEY: rowcount 1 → inserted (we own it),
    0 → key already present (another instance/tick owns it). Same exactly-once
    primitive as earnings_alert `try_insert_delivery`.
    """
    _ensure_runs_table()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rc = db_manager.execute_insert(
        f"""
        INSERT IGNORE INTO {_TABLE} (run_key, started_at, status)
        VALUES (:run_key, :started_at, 'running')
        """,
        {"run_key": run_key, "started_at": now_utc},
    )
    return rc == 1


def _finalize_window(
    run_key: str,
    *,
    status: str,
    annual: Optional[Dict[str, int]] = None,
    quarterly: Optional[Dict[str, int]] = None,
    detail: str = "",
) -> None:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    a = annual or {}
    q = quarterly or {}
    try:
        db_manager.execute_update(
            f"""
            UPDATE {_TABLE}
               SET finished_at = :finished_at,
                   status = :status,
                   annual_updated = :au, annual_errors = :ae,
                   quarterly_updated = :qu, quarterly_errors = :qe,
                   detail = :detail
             WHERE run_key = :run_key
            """,
            {
                "finished_at": now_utc,
                "status": status,
                "au": int(a.get("updated", 0)),
                "ae": int(a.get("errors", 0)),
                "qu": int(q.get("updated", 0)),
                "qe": int(q.get("errors", 0)),
                "detail": (detail or "")[:500],
                "run_key": run_key,
            },
        )
    except Exception as exc:
        log_structured_error(
            exc, page="forecast_auto_refresh", component="_finalize_window",
            operation="UPDATE", context={"run_key": run_key},
        )


# ---------------------------------------------------------------------------
# Blocked-ticker set (reporting date not yet identified) — same rule as the UI
# ---------------------------------------------------------------------------
def _has_report_date(value: Any) -> bool:
    return bool(value) and str(value).strip() not in ("", "—", "-", "N/A", "None")


def _compute_blocked(period_type: str) -> Set[str]:
    """Tickers whose reporting date is not identified — held back from refresh,
    exactly as forecasting.py's Refresh All does."""
    try:
        from data.forecast_refresh_service import get_refresh_table_data

        # Clear the 120s cache so the cycle sees fresh reporting dates.
        try:
            get_refresh_table_data.clear()
        except Exception:
            pass
        rows = get_refresh_table_data(period_type) or []
        return {
            r["ticker"] for r in rows
            if r.get("ticker") and not _has_report_date(r.get("annual_reported_on"))
        }
    except Exception as exc:
        log_structured_error(
            exc, page="forecast_auto_refresh", component="_compute_blocked",
            operation="blocked_set", context={"period_type": period_type},
        )
        # Fail safe: block nothing rather than crash — sync still gates per-ticker.
        return set()


# ---------------------------------------------------------------------------
# One cadence + full cycle
# ---------------------------------------------------------------------------
def _summarize(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """Full status breakdown for logging + the audit row."""
    def _n(status: str) -> int:
        return sum(1 for r in results if r.get("status") == status)
    return {
        "updated": _n("updated"),
        "up_to_date": _n("up_to_date"),
        "skipped": _n("skipped"),
        "no_data": _n("no_data"),
        "errors": _n("error"),
        "total": len(results),
    }


def _run_period(
    period_type: str,
    *,
    force: bool,
    extra_exclude: Optional[Set[str]] = None,
) -> Dict[str, int]:
    """Run one cadence (annual or quarterly) via the proven bulk sync fn."""
    from data.forecast_admin_service import sync_all_eligible, sync_all_eligible_quarterly
    from data.forecast_refresh_service import send_model_refresh_email

    sync_all = sync_all_eligible_quarterly if period_type == "quarterly" else sync_all_eligible
    # NO reporting-date gating: needs_update() already re-runs strictly when NEWER
    # actual data has arrived, so the scheduler never skips a company that has new
    # data — "no miss." (extra_exclude exists only for scoped testing.) The manual
    # UI keeps its own reporting-date "on hold" behavior; the scheduler does not.
    exclude = {str(t).strip().upper() for t in (extra_exclude or ())}

    log_info(f"[forecast_auto_refresh] {period_type} START — force={force} companies_excluded={len(exclude)}")
    t0 = time.perf_counter()
    results = sync_all(force=force, exclude_tickers=exclude)
    elapsed = (time.perf_counter() - t0) * 1000
    summary = _summarize(results)
    # WARNING level so it's visible under the production APP_LOG_LEVEL=WARNING;
    # includes the tickers that actually refreshed (usually few).
    _updated = [r.get("ticker") for r in results if r.get("status") == "updated"]
    log_timing(
        f"FORECAST_AUTO_REFRESH_{period_type.upper()}", elapsed,
        f"updated={summary['updated']} up_to_date={summary['up_to_date']} "
        f"skipped={summary['skipped']} no_data={summary['no_data']} "
        f"errors={summary['errors']} total={summary['total']} excluded={len(exclude)} "
        f"updated_tickers={_updated[:60]}",
        level="WARNING",
    )
    log_info(
        f"[forecast_auto_refresh] {period_type} DONE {elapsed/1000:.1f}s — "
        f"updated={summary['updated']} up_to_date={summary['up_to_date']} "
        f"skipped={summary['skipped']} no_data={summary['no_data']} errors={summary['errors']}"
    )
    if _updated:
        log_info(f"[forecast_auto_refresh] {period_type} UPDATED tickers ({len(_updated)}): {_updated}")
    for r in results:
        if r.get("status") == "error":
            log_error(
                f"[forecast_auto_refresh] {period_type} ERROR ticker={r.get('ticker')} "
                f"msg={(r.get('message') or '')[:120]}"
            )

    if _email_enabled():
        try:
            send_model_refresh_email(
                triggered_by=_TRIGGERED_BY, results=results, period_type=period_type,
            )
        except Exception as exc:
            log_structured_error(
                exc, page="forecast_auto_refresh", component="_run_period",
                operation="email", context={"period_type": period_type},
            )
    return summary


def _run_cycle(*, force: bool = False, extra_exclude: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Ensure table → annual → quarterly → clear caches. Each cadence isolated."""
    from data.forecast_admin_service import clear_revenue_forecast_caches

    _ensure_runs_table()
    out: Dict[str, Any] = {"annual": {}, "quarterly": {}, "errors": []}

    # Quarterly forecasting paused (see constants.QUARTERLY_FORECASTING_ENABLED):
    # run the Annual cadence only. The quarterly entry in `out` stays empty (0s).
    _periods = ("annual", "quarterly") if QUARTERLY_FORECASTING_ENABLED else ("annual",)
    for period in _periods:
        try:
            out[period] = _run_period(period, force=force, extra_exclude=extra_exclude)
        except Exception as exc:
            out["errors"].append(f"{period}: {str(exc)[:200]}")
            log_structured_error(
                exc, page="forecast_auto_refresh", component="_run_cycle",
                operation=period, context={},
            )

    try:
        clear_revenue_forecast_caches()
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Windowing + tick
# ---------------------------------------------------------------------------
def _due_window_key(now_local: Optional[datetime] = None) -> Optional[str]:
    """The 'YYYY-MM-DD:HHMM' key of the most recent past-due run window TODAY.

    Returns None before the day's first configured time. Per-local-day, so
    yesterday's windows are never re-run; the DB claim guarantees once-only.
    """
    local = now_local or _now_local()
    cur = f"{local.hour:02d}{local.minute:02d}"
    times = _run_times()
    due = [t for t in times if t <= cur]
    if not due:
        return None
    return f"{local.date().isoformat()}:{due[-1]}"


def run_forecast_auto_refresh_tick(*, force: bool = False) -> Optional[Dict[str, Any]]:
    """One scheduler tick. Resolve the due window, claim it, run the cycle.

    `force=True` runs immediately using the current window key (or a synthetic
    'forced' key when none is due) and forces per-ticker engine runs — used for
    testing / manual kick.
    """
    global _running

    if force:
        run_key = _due_window_key() or f"{_now_local().date().isoformat()}:forced"
    else:
        run_key = _due_window_key()
        if not run_key:
            return None

    # In-process re-entrancy guard: never overlap cycles in one process.
    with _run_lock:
        if _running:
            return None
        _running = True

    try:
        if not force:
            # Cross-instance exactly-once: only the winner proceeds.
            if not _claim_window(run_key):
                log_info(
                    f"[forecast_auto_refresh] window {run_key} already claimed — "
                    f"skipping (another instance/tick owns it)"
                )
                return None
        else:
            # Forced runs still record a row (claim if new; ignore if present).
            _claim_window(run_key)

        log_info(f"[forecast_auto_refresh] cycle START run_key={run_key} force={force}")
        t0 = time.perf_counter()
        result = _run_cycle(force=force)
        elapsed = (time.perf_counter() - t0) * 1000

        status = "error" if result.get("errors") else "ok"
        _finalize_window(
            run_key, status=status,
            annual=result.get("annual"), quarterly=result.get("quarterly"),
            detail="; ".join(result.get("errors") or []),
        )
        log_timing(
            "FORECAST_AUTO_REFRESH_CYCLE", elapsed,
            f"run_key={run_key} status={status} "
            f"annual={result.get('annual')} quarterly={result.get('quarterly')}",
            level="WARNING",
        )
        log_info(f"[forecast_auto_refresh] cycle DONE run_key={run_key} status={status}")
        result["run_key"] = run_key
        result["status"] = status
        return result
    except Exception as exc:
        log_structured_error(
            exc, page="forecast_auto_refresh", component="run_forecast_auto_refresh_tick",
            operation="tick", context={"run_key": run_key, "force": force},
        )
        try:
            _finalize_window(run_key, status="error", detail=str(exc)[:200])
        except Exception:
            pass
        return None
    finally:
        with _run_lock:
            _running = False


# ---------------------------------------------------------------------------
# Daemon thread
# ---------------------------------------------------------------------------
def start_forecast_auto_refresh() -> None:
    """Start ONE daemon thread that ticks on the poll interval. No-op if disabled.

    Guard against double-start with the APP_FORECAST_AUTO_REFRESH_STARTED env flag
    in the caller (main.py), mirroring the other warmups.
    """
    if not _enabled():
        log_info("[forecast_auto_refresh] disabled (ENABLE_FORECAST_AUTO_REFRESH!=1)")
        return

    def _loop() -> None:
        sleep_s = 300
        try:
            sleep_s = int(os.getenv("FORECAST_AUTO_REFRESH_SLEEP_SEC", "300"))
        except ValueError:
            sleep_s = 300
        sleep_s = max(60, sleep_s)
        log_info(
            f"[forecast_auto_refresh] thread started — times={_run_times()} "
            f"tz={os.getenv('FORECAST_AUTO_REFRESH_TZ', 'Asia/Kolkata')} poll={sleep_s}s"
        )
        while True:
            try:
                run_forecast_auto_refresh_tick()
            except Exception:
                pass
            time.sleep(sleep_s)

    threading.Thread(target=_loop, daemon=True, name="forecast-auto-refresh").start()
