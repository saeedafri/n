"""
Watchlist Service
=================
Persistence layer for user watchlists.

Tables (auto-created on first use):
  coreiq_watchlists           — watchlist metadata
  coreiq_watchlist_access     — per-user ACL grants
  coreiq_watchlist_companies  — companies belonging to each watchlist

ACL mirrors saved_criteria:
  Creator   → full control
  Granted   → only what was explicitly granted (view is always implicit)
  Listing   → user sees own watchlists + any where they have a grant row

Watchlist purpose: pre-filter the screening base universe to a curated
company set before criteria are applied.  Companies can be added by explicit
ticker selection or by sector/country filter.
"""

import threading
import time as _wl_time
from typing import Dict, List, Optional

from sqlalchemy import text

from core.database import db_manager

try:
    from utils.server_logger import log_error, log_info, log_structured_error
except ImportError:
    import logging
    _lg = logging.getLogger(__name__)
    log_error = _lg.error
    log_info = _lg.info
    def log_structured_error(*a, **kw): pass

try:
    import streamlit as _st
    _HAS_STREAMLIT = True
except ImportError:
    _HAS_STREAMLIT = False

_WL_CACHE_KEY = "_coreiq_wl_cache_v1"
_WL_CACHE_TTL = 120  # seconds


# =============================================================================
# DDL
# =============================================================================

_DDL_WATCHLISTS = """
CREATE TABLE IF NOT EXISTS coreiq_watchlists (
    id          INT          NOT NULL AUTO_INCREMENT,
    name        VARCHAR(100) NOT NULL,
    description TEXT,
    created_by  VARCHAR(255) NOT NULL,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    is_active   TINYINT(1)   NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    INDEX idx_wl_owner  (created_by),
    INDEX idx_wl_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_WATCHLIST_ACCESS = """
CREATE TABLE IF NOT EXISTS coreiq_watchlist_access (
    id           INT          NOT NULL AUTO_INCREMENT,
    watchlist_id INT          NOT NULL,
    user_email   VARCHAR(255) NOT NULL,
    granted_by   VARCHAR(255) NOT NULL,
    can_edit     TINYINT(1)   NOT NULL DEFAULT 0,
    can_delete   TINYINT(1)   NOT NULL DEFAULT 0,
    granted_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_wla_watchlist_user (watchlist_id, user_email),
    INDEX idx_wla_user (user_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_WATCHLIST_COMPANIES = """
CREATE TABLE IF NOT EXISTS coreiq_watchlist_companies (
    id           INT          NOT NULL AUTO_INCREMENT,
    watchlist_id INT          NOT NULL,
    ticker       VARCHAR(20)  NOT NULL,
    company_name VARCHAR(255),
    sector       VARCHAR(100),
    country      VARCHAR(100),
    added_by     VARCHAR(255) NOT NULL,
    added_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- Composite unique key (ticker + name): JD / LULU / TSCO are each shared by
    -- two distinct companies on different exchanges, so ticker alone is not unique.
    UNIQUE KEY uq_wlc_watchlist_ticker_name (watchlist_id, ticker, company_name),
    INDEX idx_wlc_watchlist (watchlist_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


# =============================================================================
# Table bootstrap
# =============================================================================

_tables_ready = False
_tables_lock  = threading.Lock()


def ensure_tables() -> bool:
    """Create watchlist tables if they do not exist. Idempotent."""
    global _tables_ready
    if _tables_ready:
        return True
    with _tables_lock:
        if _tables_ready:
            return True
        try:
            # Run all DDL in one session — saves 2 round trips vs. 3 separate sessions.
            # MySQL auto-commits DDL, but reusing one connection avoids repeated
            # pool checkout + COMMIT overhead on Azure (~240ms RTT per round trip).
            with db_manager.get_session() as session:
                session.execute(text(_DDL_WATCHLISTS))
                session.execute(text(_DDL_WATCHLIST_ACCESS))
                session.execute(text(_DDL_WATCHLIST_COMPANIES))
            _tables_ready = True
            log_info("[WATCHLIST] Tables ensured")
            return True
        except Exception as exc:
            log_structured_error(exc, page="watchlist_service",
                                 component="ensure_tables", operation="create_tables")
            return False


def _require_tables():
    if not _tables_ready:
        ensure_tables()


# =============================================================================
# CREATE
# =============================================================================

def create_watchlist(
    name: str,
    user_email: str,
    description: str = "",
) -> Optional[int]:
    """Create a new watchlist. Returns new id or None on failure."""
    _require_tables()
    try:
        sql = """
            INSERT INTO coreiq_watchlists (name, description, created_by)
            VALUES (:name, :description, :created_by)
        """
        new_id = db_manager.execute_insert_returning_id(sql, {
            "name":        name.strip()[:100],
            "description": (description or "").strip(),
            "created_by":  user_email,
        })
        log_info(f"[WATCHLIST] Created id={new_id} name={name!r} by={user_email}")
        _invalidate_watchlist_cache(user_email)
        return new_id
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="create_watchlist", operation="insert")
        return None


# =============================================================================
# READ
# =============================================================================

def _invalidate_watchlist_cache(user_email: str) -> None:
    """Clear the TTL cache entry for a user after any write operation."""
    if not _HAS_STREAMLIT:
        return
    try:
        key = (user_email or "").strip().lower()
        cache = _st.session_state.get(_WL_CACHE_KEY, {})
        if key in cache:
            del cache[key]
            _st.session_state[_WL_CACHE_KEY] = cache
    except Exception:
        pass


def get_user_watchlists(user_email: str) -> List[Dict]:
    """Return all active watchlists visible to every logged-in user.

    All watchlists are visible to all users. Permission flags
    (is_owner, can_edit, can_delete) control who can modify/delete.

    Each row includes:
      id, name, description, created_by, created_at, updated_at,
      is_owner, can_edit, can_delete, company_count

    Uses a 120s session-state TTL cache to avoid hitting the DB on every
    Streamlit rerun. Invalidated automatically after any write operation.
    """
    key = (user_email or "").strip().lower()

    # TTL cache check
    if _HAS_STREAMLIT:
        try:
            cache = _st.session_state.get(_WL_CACHE_KEY, {})
            entry = cache.get(key)
            if entry is not None:
                cached_at, cached_val = entry
                if _wl_time.time() - cached_at < _WL_CACHE_TTL:
                    return list(cached_val)
        except Exception:
            pass

    _require_tables()
    sql = """
        SELECT
            wl.id,
            wl.name,
            wl.description,
            wl.created_by,
            wl.created_at,
            wl.updated_at,
            IF(wl.created_by = :email, 1, 0)                           AS is_owner,
            COALESCE(wla.can_edit,
                     IF(wl.created_by = :email, 1, 0))                 AS can_edit,
            COALESCE(wla.can_delete,
                     IF(wl.created_by = :email, 1, 0))                 AS can_delete,
            COALESCE(wlc_cnt.cnt, 0)                                   AS company_count
        FROM coreiq_watchlists wl
        LEFT JOIN coreiq_watchlist_access wla
               ON wla.watchlist_id = wl.id
              AND wla.user_email   = :email
        LEFT JOIN (
            SELECT watchlist_id, COUNT(*) AS cnt
            FROM   coreiq_watchlist_companies
            GROUP  BY watchlist_id
        ) wlc_cnt ON wlc_cnt.watchlist_id = wl.id
        WHERE wl.is_active = 1
        ORDER BY wl.updated_at DESC
    """
    try:
        result = [dict(r) for r in db_manager.execute_query_readonly(sql, {"email": user_email})]
        if _HAS_STREAMLIT:
            try:
                cache = _st.session_state.get(_WL_CACHE_KEY, {})
                cache[key] = (_wl_time.time(), result)
                _st.session_state[_WL_CACHE_KEY] = cache
            except Exception:
                pass
        return result
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="get_user_watchlists", operation="select")
        # Return stale cache if available rather than empty list
        if _HAS_STREAMLIT:
            try:
                cache = _st.session_state.get(_WL_CACHE_KEY, {})
                entry = cache.get(key)
                if entry is not None:
                    return list(entry[1])
            except Exception:
                pass
        return []


def get_watchlist_companies(watchlist_id: int) -> List[Dict]:
    """Return all companies in a watchlist (ticker, company_name, sector, country)."""
    _require_tables()
    try:
        sql = """
            SELECT ticker, company_name, sector, country, added_at, added_by
            FROM   coreiq_watchlist_companies
            WHERE  watchlist_id = :id
            ORDER BY company_name
        """
        return [dict(r) for r in db_manager.execute_query_readonly(sql, {"id": watchlist_id})]
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="get_watchlist_companies", operation="select")
        return []


def get_watchlist_tickers(watchlist_id: int) -> set:
    """Return the set of tickers in a watchlist (fast path for base-universe filter)."""
    _require_tables()
    try:
        sql = "SELECT ticker FROM coreiq_watchlist_companies WHERE watchlist_id = :id"
        rows = db_manager.execute_query_readonly(sql, {"id": watchlist_id})
        return {r["ticker"] for r in rows}
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="get_watchlist_tickers", operation="select")
        return set()


# =============================================================================
# UPDATE
# =============================================================================

def update_watchlist(
    watchlist_id: int,
    user_email: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> bool:
    """Update watchlist metadata. Only creator or can_edit users may call this.

    Uses a conditional UPDATE (1 DB round trip instead of SELECT + UPDATE).
    """
    _require_tables()
    try:
        updates: Dict = {}
        if name is not None:
            updates["name"] = name.strip()[:100]
        if description is not None:
            updates["description"] = description.strip()
        if not updates:
            return True
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        sql = f"""
            UPDATE coreiq_watchlists wl
            SET    {set_clause}
            WHERE  wl.id        = :id
              AND  wl.is_active = 1
              AND  (
                     wl.created_by = :email
                     OR EXISTS (
                         SELECT 1 FROM coreiq_watchlist_access wla
                         WHERE  wla.watchlist_id = wl.id
                           AND  wla.user_email   = :email
                           AND  wla.can_edit      = 1
                     )
                   )
        """
        updates["id"]    = watchlist_id
        updates["email"] = user_email
        affected = db_manager.execute_insert(sql, updates)
        if not affected:
            log_error(f"[WATCHLIST] update denied or not found: {user_email} on id={watchlist_id}")
            return False
        _invalidate_watchlist_cache(user_email)
        return True
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="update_watchlist", operation="update")
        return False


def add_companies_to_watchlist(
    watchlist_id: int,
    companies: List[Dict],
    user_email: str,
) -> int:
    """Bulk-insert companies into watchlist (INSERT IGNORE on duplicate ticker).

    companies: list of dicts with keys: ticker, company_name, sector, country
    Returns count of newly inserted rows.

    2 DB round trips:
    RTT 1 — Conditional UPDATE for ACL check + updated_at bump in one statement.
             rowcount == 0 → denied (creator mismatch or can_edit = 0).
    RTT 2 — Batch INSERT IGNORE (all rows in one execute call via executemany).
    """
    _require_tables()
    if not companies:
        return 0
    try:
        # RTT 1: ACL check + updated_at bump — conditional UPDATE embeds permission check.
        acl_sql = """
            UPDATE coreiq_watchlists wl
            SET    updated_at = NOW()
            WHERE  wl.id        = :wid
              AND  wl.is_active = 1
              AND  (
                     wl.created_by = :email
                     OR EXISTS (
                         SELECT 1 FROM coreiq_watchlist_access wla
                         WHERE  wla.watchlist_id = wl.id
                           AND  wla.user_email   = :email
                           AND  wla.can_edit      = 1
                     )
                   )
        """
        affected = db_manager.execute_insert(acl_sql, {"wid": watchlist_id, "email": user_email})
        if not affected:
            log_error(f"[WATCHLIST] add_companies denied: {user_email} on id={watchlist_id}")
            return 0

        # RTT 2: Batch INSERT IGNORE — all rows in one execute call (executemany).
        sql = """
            INSERT IGNORE INTO coreiq_watchlist_companies
                (watchlist_id, ticker, company_name, sector, country, added_by)
            VALUES
                (:watchlist_id, :ticker, :company_name, :sector, :country, :added_by)
        """
        rows_params = [
            {
                "watchlist_id": watchlist_id,
                "ticker":       (co.get("ticker") or "").strip()[:20],
                "company_name": (co.get("company_name") or "").strip()[:255],
                "sector":       (co.get("sector") or "").strip()[:100],
                "country":      (co.get("country") or "").strip()[:100],
                "added_by":     user_email,
            }
            for co in companies
        ]
        if db_manager._engine is None:
            db_manager.connect()
        engine = db_manager._read_engine or db_manager._engine
        with engine.connect() as conn:
            result  = conn.execute(text(sql), rows_params)
            conn.commit()
            inserted = result.rowcount

        log_info(f"[WATCHLIST] Added {inserted} companies to id={watchlist_id} by={user_email}")
        _invalidate_watchlist_cache(user_email)
        return inserted
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="add_companies_to_watchlist", operation="bulk_insert")
        return 0


def remove_company_from_watchlist(
    watchlist_id: int,
    ticker: str,
    user_email: str,
    company_name: Optional[str] = None,
) -> bool:
    """Remove a single company from a watchlist.

    company_name scopes the delete to one exact company. Three tickers (JD, LULU,
    TSCO) are shared by two different companies on different exchanges; without the
    name, deleting by ticker alone would remove BOTH siblings. Pass company_name
    whenever it's known (the UI always does).

    2 DB round trips:
    RTT 1 — Multi-table DELETE: removes the row AND embeds ACL check via JOIN.
             rowcount == 0 means denied OR ticker not in watchlist (both OK outcomes).
    RTT 2 — Bump updated_at on the watchlist.
    """
    _require_tables()
    try:
        # RTT 1: DELETE with embedded ACL check (+ optional exact-company scope)
        name_clause = "AND wc.company_name = :company_name" if company_name is not None else ""
        del_sql = f"""
            DELETE wc
            FROM   coreiq_watchlist_companies wc
            JOIN   coreiq_watchlists wl ON wl.id = wc.watchlist_id
            WHERE  wc.watchlist_id = :wid
              AND  wc.ticker       = :ticker
              {name_clause}
              AND  wl.is_active    = 1
              AND  (
                     wl.created_by = :email
                     OR EXISTS (
                         SELECT 1 FROM coreiq_watchlist_access wla
                         WHERE  wla.watchlist_id = wl.id
                           AND  wla.user_email   = :email
                           AND  wla.can_edit      = 1
                     )
                   )
        """
        _params = {"wid": watchlist_id, "ticker": ticker, "email": user_email}
        if company_name is not None:
            _params["company_name"] = company_name
        db_manager.execute_insert(del_sql, _params)
        # RTT 2: Bump updated_at
        db_manager.execute_insert(
            "UPDATE coreiq_watchlists SET updated_at = NOW() WHERE id = :id",
            {"id": watchlist_id},
        )
        _invalidate_watchlist_cache(user_email)
        return True
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="remove_company_from_watchlist", operation="delete")
        return False


def replace_watchlist_companies(
    watchlist_id: int,
    companies: List[Dict],
    user_email: str,
) -> bool:
    """Replace all companies in a watchlist.

    Deletes existing rows then bulk-inserts new set.
    Only creator or can_edit users may call this.

    3 DB round trips (regardless of company count — batch executemany for INSERT):
    RTT 1 — Conditional UPDATE: ACL check + updated_at bump in one statement.
    RTT 2 — DELETE all existing companies (unconditional; ACL already verified).
    RTT 3 — Batch INSERT IGNORE all new companies (single executemany call).
    """
    _require_tables()
    try:
        # RTT 1: ACL check + updated_at bump
        acl_sql = """
            UPDATE coreiq_watchlists wl
            SET    updated_at = NOW()
            WHERE  wl.id        = :wid
              AND  wl.is_active = 1
              AND  (
                     wl.created_by = :email
                     OR EXISTS (
                         SELECT 1 FROM coreiq_watchlist_access wla
                         WHERE  wla.watchlist_id = wl.id
                           AND  wla.user_email   = :email
                           AND  wla.can_edit      = 1
                     )
                   )
        """
        affected = db_manager.execute_insert(acl_sql, {"wid": watchlist_id, "email": user_email})
        if not affected:
            log_error(f"[WATCHLIST] replace_companies denied: {user_email} on id={watchlist_id}")
            return False

        # RTT 2: Delete all existing companies
        db_manager.execute_insert(
            "DELETE FROM coreiq_watchlist_companies WHERE watchlist_id = :id",
            {"id": watchlist_id},
        )

        # RTT 3: Batch INSERT IGNORE — all rows in one execute call (executemany)
        if companies:
            sql = """
                INSERT IGNORE INTO coreiq_watchlist_companies
                    (watchlist_id, ticker, company_name, sector, country, added_by)
                VALUES
                    (:watchlist_id, :ticker, :company_name, :sector, :country, :added_by)
            """
            rows_params = [
                {
                    "watchlist_id": watchlist_id,
                    "ticker":       (co.get("ticker") or "").strip()[:20],
                    "company_name": (co.get("company_name") or "").strip()[:255],
                    "sector":       (co.get("sector") or "").strip()[:100],
                    "country":      (co.get("country") or "").strip()[:100],
                    "added_by":     user_email,
                }
                for co in companies
            ]
            if db_manager._engine is None:
                db_manager.connect()
            engine = db_manager._read_engine or db_manager._engine
            with engine.connect() as conn:
                conn.execute(text(sql), rows_params)
                conn.commit()

        log_info(f"[WATCHLIST] Replaced companies in id={watchlist_id} "
                 f"new_count={len(companies)} by={user_email}")
        _invalidate_watchlist_cache(user_email)
        return True
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="replace_watchlist_companies", operation="replace")
        return False


# =============================================================================
# DELETE (soft)
# =============================================================================

def delete_watchlist(watchlist_id: int, user_email: str) -> bool:
    """Soft-delete a watchlist. Only creator or can_delete users may call this.

    Uses a conditional UPDATE (1 DB round trip instead of SELECT + UPDATE).
    """
    _require_tables()
    try:
        sql = """
            UPDATE coreiq_watchlists wl
            SET    is_active = 0
            WHERE  wl.id        = :id
              AND  wl.is_active = 1
              AND  (
                     wl.created_by = :email
                     OR EXISTS (
                         SELECT 1 FROM coreiq_watchlist_access wla
                         WHERE  wla.watchlist_id = wl.id
                           AND  wla.user_email   = :email
                           AND  wla.can_delete   = 1
                     )
                   )
        """
        affected = db_manager.execute_insert(sql, {"id": watchlist_id, "email": user_email})
        if not affected:
            log_error(f"[WATCHLIST] delete denied or not found: {user_email} on id={watchlist_id}")
            return False
        log_info(f"[WATCHLIST] Soft-deleted id={watchlist_id} by={user_email}")
        _invalidate_watchlist_cache(user_email)
        return True
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="delete_watchlist", operation="soft_delete")
        return False


# =============================================================================
# ACCESS MANAGEMENT
# =============================================================================

def grant_watchlist_access(
    watchlist_id: int,
    target_email: str,
    granter_email: str,
    can_edit: bool = False,
    can_delete: bool = False,
) -> bool:
    """Grant (or update) access. Only creator may grant.

    Uses INSERT-SELECT to embed the ownership check — 1 DB round trip instead of
    the previous SELECT-then-INSERT (2 RTTs).  If the granter is not the creator
    the subquery returns 0 rows and no row is inserted (rowcount == 0 → denied).
    """
    _require_tables()
    if target_email == granter_email:
        return True  # no-op; creator always has full access
    try:
        sql = """
            INSERT INTO coreiq_watchlist_access
                (watchlist_id, user_email, granted_by, can_edit, can_delete)
            SELECT :watchlist_id, :user_email, :granted_by, :can_edit, :can_delete
            FROM   coreiq_watchlists
            WHERE  id         = :watchlist_id
              AND  is_active  = 1
              AND  created_by = :granted_by
            ON DUPLICATE KEY UPDATE
                can_edit   = VALUES(can_edit),
                can_delete = VALUES(can_delete),
                granted_by = VALUES(granted_by)
        """
        affected = db_manager.execute_insert(sql, {
            "watchlist_id": watchlist_id,
            "user_email":   target_email,
            "granted_by":   granter_email,
            "can_edit":     int(can_edit),
            "can_delete":   int(can_delete),
        })
        if not affected:
            log_error(f"[WATCHLIST] grant denied: {granter_email} not owner of id={watchlist_id}")
            return False
        log_info(f"[WATCHLIST] Granted id={watchlist_id} to={target_email} "
                 f"edit={can_edit} delete={can_delete}")
        # target_email: now sees the watchlist — invalidate their TTL cache
        # granter_email: ownership is unchanged but invalidate for safety (no-op if cache miss)
        # Note: early-return no-op at the top of this function handles target == granter.
        _invalidate_watchlist_cache(target_email)
        _invalidate_watchlist_cache(granter_email)
        return True
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="grant_watchlist_access", operation="upsert_access")
        return False


def revoke_watchlist_access(
    watchlist_id: int,
    target_email: str,
    granter_email: str,
) -> bool:
    """Revoke a user's access. Only creator may revoke.

    Uses a multi-table DELETE with JOIN to embed the ownership check — 1 DB round
    trip instead of the previous SELECT-then-DELETE (2 RTTs).
    """
    _require_tables()
    try:
        sql = """
            DELETE wa
            FROM   coreiq_watchlist_access wa
            JOIN   coreiq_watchlists wl ON wl.id = wa.watchlist_id
            WHERE  wa.watchlist_id = :wid
              AND  wa.user_email   = :email
              AND  wl.created_by   = :granter
              AND  wl.is_active    = 1
        """
        db_manager.execute_insert(sql, {
            "wid":     watchlist_id,
            "email":   target_email,
            "granter": granter_email,
        })
        log_info(f"[WATCHLIST] Revoked id={watchlist_id} from={target_email}")
        # target_email: no longer sees the watchlist — invalidate their TTL cache
        # granter_email: ownership unchanged but invalidate for safety
        _invalidate_watchlist_cache(target_email)
        _invalidate_watchlist_cache(granter_email)
        return True
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="revoke_watchlist_access", operation="delete_access")
        return False


def get_watchlist_access_grants(watchlist_id: int, owner_email: str) -> List[Dict]:
    """Return all access grants (owner view only) — 1 DB round trip."""
    _require_tables()
    try:
        # Single query: INNER JOIN ensures we only return rows when the caller
        # is the owner AND there are grant rows.  Returns [] for non-owners or
        # owners with no grants (correct for both cases).
        sql = """
            SELECT wla.user_email, wla.granted_by, wla.can_edit,
                   wla.can_delete, wla.granted_at
            FROM   coreiq_watchlists wl
            JOIN   coreiq_watchlist_access wla ON wla.watchlist_id = wl.id
            WHERE  wl.id        = :id
              AND  wl.is_active = 1
              AND  wl.created_by = :owner
            ORDER BY wla.granted_at DESC
        """
        return [dict(r) for r in db_manager.execute_query_readonly(
            sql, {"id": watchlist_id, "owner": owner_email}
        )]
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="get_watchlist_access_grants", operation="select_grants")
        return []


# =============================================================================
# Internal helpers
# =============================================================================

def _get_watchlist_acl(watchlist_id: int, user_email: str) -> Optional[Dict]:
    """Return ACL info dict for the watchlist. Returns None if not found."""
    try:
        sql = """
            SELECT
                wl.id,
                wl.created_by,
                IF(wl.created_by = :email, 1, 0)                           AS is_owner,
                COALESCE(wla.can_edit,
                         IF(wl.created_by = :email, 1, 0))                 AS can_edit,
                COALESCE(wla.can_delete,
                         IF(wl.created_by = :email, 1, 0))                 AS can_delete
            FROM coreiq_watchlists wl
            LEFT JOIN coreiq_watchlist_access wla
                   ON wla.watchlist_id = wl.id
                  AND wla.user_email   = :email
            WHERE wl.id        = :id
              AND wl.is_active = 1
            LIMIT 1
        """
        rows = db_manager.execute_query_readonly(sql, {"id": watchlist_id, "email": user_email})
        return dict(rows[0]) if rows else None
    except Exception as exc:
        log_structured_error(exc, page="watchlist_service",
                             component="_get_watchlist_acl", operation="select")
        return None
