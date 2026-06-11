"""
Best-effort earnings reminder dispatch for saved alert preferences.

Runs from a daemon thread and on a throttled tick before the app renders pages.
Requires EMAIL_PASSWORD and MySQL prefs (coreiq_earnings_alert_* via data/earnings_alert_service.py).

Send window: UTC hour >= EARNINGS_ALERT_DISPATCH_HOUR_UTC (default 8).

Catch-up: EARNINGS_ALERT_DISPATCH_LOOKBACK_DAYS (default 3) also checks recent
reminder days (today, yesterday, …). If the app was down, unsent rows are
emailed on the next active tick (dedupe log still prevents duplicates).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from data.earnings_alert_store import (
    delivery_exists,
    list_enabled_preferences,
    try_insert_delivery,
)
from data.repository import CompanyRepository, EarningsCalendarRepository
from data.watchlist_service import get_watchlist_tickers
from utils.earnings_alert_email import send_earnings_reminder_digest
from utils.server_logger import log_structured_error

_EC_WATCHLIST_ID_PREFIX = "watchlist_id:"

_tick_lock = threading.Lock()
_last_tick_monotonic = 0.0


def _normalize_selection_mode(saved: Dict[str, Any]) -> str:
    mode = (saved.get("selection_mode") or "companies") or "companies"
    tickers = saved.get("tickers") or []
    sectors = saved.get("sectors") or []
    if mode in ("companies", "sectors", "watchlist"):
        return mode
    if mode == "all":
        return "companies"
    if mode == "selected":
        return "companies"
    if mode == "sector":
        return "sectors"
    if mode == "company_or_sector":
        if tickers and not sectors:
            return "companies"
        if sectors and not tickers:
            return "sectors"
        if tickers:
            return "companies"
        if sectors:
            return "sectors"
        return "companies"
    return "companies"


def _allowed_ticker_set(pref: Dict[str, Any]) -> Optional[Set[str]]:
    """
    None = no ticker filter (all calendar names). Else uppercase tickers to keep.
    """
    mode = _normalize_selection_mode(pref)
    tickers = pref.get("tickers") or []
    sectors = pref.get("sectors") or []

    if mode == "watchlist":
        # Resolve tickers from the saved watchlist at dispatch time
        for t in tickers:
            ts = str(t).strip()
            if ts.startswith(_EC_WATCHLIST_ID_PREFIX):
                try:
                    wl_id = int(ts[len(_EC_WATCHLIST_ID_PREFIX):])
                    resolved = get_watchlist_tickers(wl_id)
                    if not resolved:
                        return set()  # watchlist empty or deleted → send nothing
                    return {str(tk).strip().upper() for tk in resolved if tk}
                except Exception:
                    return set()
        return set()  # malformed sentinel → send nothing

    if mode == "companies":
        if not tickers:
            return None
        return {str(t).strip().upper() for t in tickers if t}
    if not sectors:
        return None
    sector_set = set(sectors)
    out: Set[str] = set()
    try:
        cmap = CompanyRepository.get_companies_map()
        for t, row in cmap.items():
            sec = (row or {}).get("primary_industry_coresight") or ""
            if sec in sector_set:
                out.add(str(t).strip().upper())
    except Exception:
        return out
    return out


def _parse_event_date(value: Any) -> Optional[date]:
    try:
        if hasattr(value, "date") and callable(value.date):
            return value.date()  # type: ignore[no-any-return]
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _reminder_dedupe_key(
    *,
    user_email: str,
    ticker: str,
    earnings_d: date,
    days_before: int,
) -> str:
    return (
        f"{user_email.strip().lower()}|{ticker.upper()}|{earnings_d.isoformat()}"
        f"|{int(days_before)}|reminder_v1"
    )


def run_earnings_alert_dispatch_tick(*, force: bool = False) -> None:
    if not (os.getenv("EMAIL_PASSWORD") or "").strip():
        return

    global _last_tick_monotonic
    interval = float(os.getenv("EARNINGS_ALERT_DISPATCH_MIN_INTERVAL_SEC", "120"))
    now_m = time.monotonic()
    with _tick_lock:
        if not force and (now_m - _last_tick_monotonic) < interval:
            return
        _last_tick_monotonic = now_m

    try:
        send_hour = int(os.getenv("EARNINGS_ALERT_DISPATCH_HOUR_UTC", "8"))
    except ValueError:
        send_hour = 8

    now_utc = datetime.now(timezone.utc)
    if not force and now_utc.hour < send_hour:
        return

    today = now_utc.date()
    try:
        lookback = int(os.getenv("EARNINGS_ALERT_DISPATCH_LOOKBACK_DAYS", "3"))
    except ValueError:
        lookback = 3
    lookback = max(0, min(lookback, 14))

    try:
        prefs_list = list_enabled_preferences()
    except Exception as e:
        log_structured_error(
            e,
            page="earnings_alert_dispatcher",
            component="run_earnings_alert_dispatch_tick",
            operation="list_prefs",
            context="",
        )
        return

    for pref in prefs_list:
        user_email = (pref.get("user_email") or "").strip()
        if not user_email:
            continue
        if not pref.get("enabled"):
            continue
        try:
            days_before = int(pref.get("days_before", 1))
        except (TypeError, ValueError):
            days_before = 1
        if days_before < 0 or days_before > 60:
            continue

        allowed = _allowed_ticker_set(pref)
        if allowed is not None and len(allowed) == 0:
            continue

        pending_lines: List[str] = []
        dedupe_keys: List[str] = []
        has_catch_up = False

        for lag in range(0, lookback + 1):
            reminder_day = today - timedelta(days=lag)
            target_earnings_date = reminder_day + timedelta(days=days_before)

            try:
                tickers_arg: Optional[tuple] = None
                if allowed is not None and len(allowed) <= 400:
                    tickers_arg = tuple(sorted(allowed))
                events = EarningsCalendarRepository.get_calendar_events(
                    tickers=tickers_arg,
                    start_date=target_earnings_date,
                    end_date=target_earnings_date,
                )
            except Exception as e:
                log_structured_error(
                    e,
                    page="earnings_alert_dispatcher",
                    component="run_earnings_alert_dispatch_tick",
                    operation="get_calendar_events",
                    context=f"user={user_email!r}",
                )
                continue

            for ev in events:
                tkr = str(ev.get("ticker") or "").strip().upper()
                if not tkr:
                    continue
                if allowed is not None and tkr not in allowed:
                    continue
                ed = _parse_event_date(ev.get("earnings_date"))
                if not ed or ed != target_earnings_date:
                    continue
                dk = _reminder_dedupe_key(
                    user_email=user_email,
                    ticker=tkr,
                    earnings_d=ed,
                    days_before=days_before,
                )
                if delivery_exists(dk):
                    continue
                name = str(ev.get("company_name") or CompanyRepository.get_company_name(tkr) or tkr)
                fqe = ev.get("fiscal_quarter_ending") or ""
                line = f"• {tkr} — {name}"
                if fqe:
                    line += f" (FQE {fqe})"
                line += f" — earnings date {ed.isoformat()}"
                if lag > 0:
                    line += f" [catch-up: reminder day was {reminder_day.isoformat()}]"
                    has_catch_up = True
                pending_lines.append(line)
                dedupe_keys.append(dk)

        if not pending_lines:
            continue

        intro = (
            f"Reminders ({days_before} calendar day(s) before each earnings date)."
            + (" Includes catch-up after app downtime." if has_catch_up else "")
        )
        body_lines = [
            intro,
            "",
            *pending_lines,
            "",
            "— Coresight Research (automated)",
        ]
        ok = send_earnings_reminder_digest(to_email=user_email, body_lines=body_lines)
        if ok:
            for dk in dedupe_keys:
                try_insert_delivery(user_email, dk)


def start_background_dispatch_thread() -> None:
    """Daemon loop; guard with env APP_EARNINGS_ALERT_DISPATCH_THREAD_STARTED in caller."""

    def _loop() -> None:
        sleep_s = int(os.getenv("EARNINGS_ALERT_DISPATCH_THREAD_SLEEP_SEC", "600"))
        while True:
            try:
                run_earnings_alert_dispatch_tick()
            except Exception:
                pass
            time.sleep(max(60, sleep_s))

    threading.Thread(target=_loop, daemon=True).start()
