"""
Earnings calendar email alert preferences and delivery log — MySQL (coreiq_*).

Preferences are append-only history: each Save inserts a row in
coreiq_earnings_alert_preferences_history. Current settings = latest row per user (by id).

Legacy table coreiq_earnings_alert_preferences (single row per user) is migrated once
into history if present and the user has no history yet.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from core.database import db_manager

try:
    from utils.server_logger import log_info, log_structured_error
except ImportError:
    import logging

    _lg = logging.getLogger(__name__)

    def log_info(msg: str) -> None:
        _lg.info(msg)

    def log_structured_error(*_a, **_kw) -> None:
        pass


# =============================================================================
# DDL
# =============================================================================

_DDL_PREFERENCES_HISTORY = """
CREATE TABLE IF NOT EXISTS coreiq_earnings_alert_preferences_history (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    user_email      VARCHAR(255) NOT NULL,
    enabled         TINYINT(1)   NOT NULL DEFAULT 0,
    days_before     INT          NOT NULL,
    selection_mode  VARCHAR(64)  NOT NULL,
    tickers_json    LONGTEXT     NOT NULL COMMENT 'JSON array of ticker strings',
    sectors_json    LONGTEXT     NOT NULL COMMENT 'JSON array of sector strings',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_coreiq_ea_pref_hist_user_id (user_email, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_DELIVERY_LOG = """
CREATE TABLE IF NOT EXISTS coreiq_earnings_alert_delivery_log (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    user_email  VARCHAR(255) NOT NULL,
    dedupe_key  VARCHAR(512) NOT NULL,
    sent_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_coreiq_earnings_alert_dedupe (dedupe_key),
    INDEX idx_coreiq_earnings_alert_delivery_user (user_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_tables_ready = False
_tables_lock = threading.Lock()
_legacy_snapshot_migrated = False
_legacy_migrate_lock = threading.Lock()


def ensure_tables() -> bool:
    """Create coreiq_earnings_alert_* tables if missing. Idempotent."""
    global _tables_ready
    if _tables_ready:
        return True
    with _tables_lock:
        if _tables_ready:
            return True
        try:
            with db_manager.get_session() as session:
                session.execute(text(_DDL_PREFERENCES_HISTORY))
                session.execute(text(_DDL_DELIVERY_LOG))
            _migrate_legacy_snapshot_once()
            _tables_ready = True
            log_info("[EARNINGS_ALERT] MySQL tables ensured (coreiq_earnings_alert_*)")
            return True
        except Exception as exc:
            log_structured_error(
                exc,
                page="earnings_alert_service",
                component="ensure_tables",
                operation="create_tables",
                context="",
            )
            return False


def _migrate_legacy_snapshot_once() -> None:
    """Copy rows from old single-row table into history (one row per user) if needed."""
    global _legacy_snapshot_migrated
    if _legacy_snapshot_migrated:
        return
    with _legacy_migrate_lock:
        if _legacy_snapshot_migrated:
            return
        try:
            meta = db_manager.execute_query_readonly(
                """
                SELECT COUNT(*) AS c
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 'coreiq_earnings_alert_preferences'
                """,
                {},
            )
            if not meta or int(meta[0].get("c", 0) or 0) == 0:
                _legacy_snapshot_migrated = True
                return
            n = db_manager.execute_insert(
                """
                INSERT INTO coreiq_earnings_alert_preferences_history (
                    user_email, enabled, days_before, selection_mode, tickers_json, sectors_json
                )
                SELECT p.user_email, p.enabled, p.days_before, p.selection_mode,
                       p.tickers_json, p.sectors_json
                FROM coreiq_earnings_alert_preferences p
                WHERE NOT EXISTS (
                    SELECT 1 FROM coreiq_earnings_alert_preferences_history h
                    WHERE h.user_email = p.user_email
                )
                """,
                {},
            )
            if n:
                log_info(f"[EARNINGS_ALERT] Migrated {n} legacy preference row(s) into history")
            _legacy_snapshot_migrated = True
        except Exception as exc:
            log_structured_error(
                exc,
                page="earnings_alert_service",
                component="_migrate_legacy_snapshot_once",
                operation="migrate",
                context="",
            )
            _legacy_snapshot_migrated = True


def _require_tables() -> None:
    if not _tables_ready:
        ensure_tables()


def _row_to_pref(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        raw_t = row.get("tickers_json") or "[]"
        tickers = json.loads(raw_t) if isinstance(raw_t, str) else raw_t
    except (json.JSONDecodeError, TypeError):
        tickers = []
    try:
        raw_s = row.get("sectors_json") or "[]"
        sectors = json.loads(raw_s) if isinstance(raw_s, str) else raw_s
    except (json.JSONDecodeError, TypeError):
        sectors = []
    en = row.get("enabled")
    if en is None:
        enabled = False
    else:
        try:
            enabled = bool(int(en))
        except (TypeError, ValueError):
            enabled = bool(en)
    return {
        "enabled": enabled,
        "days_before": int(row.get("days_before") or 0),
        "selection_mode": str(row.get("selection_mode") or "companies"),
        "tickers": list(tickers) if isinstance(tickers, list) else [],
        "sectors": list(sectors) if isinstance(sectors, list) else [],
    }


def load_preference(user_email: str) -> Optional[Dict[str, Any]]:
    """Latest saved preferences for this user, or None."""
    key = (user_email or "").strip().lower()
    if not key:
        return None
    _require_tables()
    rows = db_manager.execute_query_readonly(
        """
        SELECT user_email, enabled, days_before, selection_mode, tickers_json, sectors_json
        FROM coreiq_earnings_alert_preferences_history
        WHERE user_email = :user_email
        ORDER BY id DESC
        LIMIT 1
        """,
        {"user_email": key},
    )
    if not rows:
        return None
    return _row_to_pref(dict(rows[0]))


def save_preference(
    *,
    user_email: str,
    enabled: bool,
    days_before: int,
    selection_mode: str,
    tickers: List[str],
    sectors: List[str],
) -> bool:
    """Append a new history row (every Save is a new entry)."""
    key = (user_email or "").strip().lower()
    if not key:
        return False
    _require_tables()
    tj = json.dumps(list(tickers or []))
    sj = json.dumps(list(sectors or []))
    n = db_manager.execute_insert(
        """
        INSERT INTO coreiq_earnings_alert_preferences_history (
            user_email, enabled, days_before, selection_mode, tickers_json, sectors_json
        ) VALUES (
            :user_email, :enabled, :days_before, :selection_mode, :tickers_json, :sectors_json
        )
        """,
        {
            "user_email": key,
            "enabled": 1 if enabled else 0,
            "days_before": int(days_before),
            "selection_mode": str(selection_mode),
            "tickers_json": tj,
            "sectors_json": sj,
        },
    )
    return n > 0


def list_enabled_preferences() -> List[Dict[str, Any]]:
    """Users whose *latest* history row has enabled = 1."""
    _require_tables()
    rows = db_manager.execute_query_readonly(
        """
        SELECT h.user_email, h.enabled, h.days_before, h.selection_mode, h.tickers_json, h.sectors_json
        FROM coreiq_earnings_alert_preferences_history h
        INNER JOIN (
            SELECT user_email, MAX(id) AS max_id
            FROM coreiq_earnings_alert_preferences_history
            GROUP BY user_email
        ) latest
          ON h.user_email = latest.user_email AND h.id = latest.max_id
        WHERE h.enabled = 1
        """,
        {},
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        email = (d.get("user_email") or "").strip()
        base = _row_to_pref(d)
        base["user_email"] = email.lower()
        out.append(base)
    return out


def delivery_exists(dedupe_key: str) -> bool:
    dk = (dedupe_key or "").strip()
    if not dk:
        return False
    _require_tables()
    rows = db_manager.execute_query_readonly(
        """
        SELECT 1 AS ok
        FROM coreiq_earnings_alert_delivery_log
        WHERE dedupe_key = :dedupe_key
        LIMIT 1
        """,
        {"dedupe_key": dk},
    )
    return bool(rows)


def try_insert_delivery(user_email: str, dedupe_key: str) -> bool:
    """Returns True if this send was newly recorded (dedupe_key was not present)."""
    key = (user_email or "").strip().lower()
    dk = (dedupe_key or "").strip()
    if not key or not dk:
        return False
    _require_tables()
    rc = db_manager.execute_insert(
        """
        INSERT IGNORE INTO coreiq_earnings_alert_delivery_log (user_email, dedupe_key)
        VALUES (:user_email, :dedupe_key)
        """,
        {"user_email": key, "dedupe_key": dk},
    )
    return rc == 1
