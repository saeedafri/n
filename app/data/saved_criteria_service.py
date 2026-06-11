"""
Saved Criteria Service
======================
Persistence layer for user-saved screening criteria sets.

Tables (auto-created on first use):
  coreiq_saved_criteria        — criteria sets (name, JSON, owner)
  coreiq_saved_criteria_access — per-user ACL grants

ACL Rules (enforced server-side, never in UI alone):
  Creator   → full control: view, edit, delete, grant/revoke access
  Granted   → only what was explicitly granted (view is always implicit)
  Listing   → user sees own criteria + any where they have a grant row
"""

import json
import threading
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


# =============================================================================
# DDL — Tables are idempotent (CREATE IF NOT EXISTS)
# =============================================================================

_DDL_SAVED_CRITERIA = """
CREATE TABLE IF NOT EXISTS coreiq_saved_criteria (
    id             INT          NOT NULL AUTO_INCREMENT,
    name           VARCHAR(100) NOT NULL,
    description    TEXT,
    criteria_json  LONGTEXT     NOT NULL COMMENT 'JSON array of criterion dicts',
    created_by     VARCHAR(255) NOT NULL COMMENT 'user_email of creator',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP,
    is_active      TINYINT(1)   NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    INDEX idx_sc_owner  (created_by),
    INDEX idx_sc_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_DDL_SAVED_CRITERIA_ACCESS = """
CREATE TABLE IF NOT EXISTS coreiq_saved_criteria_access (
    id           INT          NOT NULL AUTO_INCREMENT,
    criteria_id  INT          NOT NULL,
    user_email   VARCHAR(255) NOT NULL COMMENT 'who has access',
    granted_by   VARCHAR(255) NOT NULL COMMENT 'who granted it (must be creator)',
    can_edit     TINYINT(1)   NOT NULL DEFAULT 0,
    can_delete   TINYINT(1)   NOT NULL DEFAULT 0,
    granted_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_sca_criteria_user (criteria_id, user_email),
    INDEX idx_sca_user (user_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


# =============================================================================
# Table bootstrap — called once per server lifetime
# =============================================================================

_tables_ready = False
_tables_lock  = threading.Lock()


def ensure_tables() -> bool:
    """Create saved criteria tables if they do not exist. Idempotent."""
    global _tables_ready
    if _tables_ready:
        return True
    with _tables_lock:
        if _tables_ready:
            return True
        try:
            # Run both DDL statements in one session — saves one round trip on Azure.
            with db_manager.get_session() as session:
                session.execute(text(_DDL_SAVED_CRITERIA))
                session.execute(text(_DDL_SAVED_CRITERIA_ACCESS))
            _tables_ready = True
            log_info("[SAVED_CRITERIA] Tables ensured")
            return True
        except Exception as exc:
            log_structured_error(exc, page="saved_criteria_service",
                                 component="ensure_tables", operation="create_tables")
            return False


def _require_tables():
    if not _tables_ready:
        ensure_tables()


# =============================================================================
# CREATE
# =============================================================================

def save_criteria(
    name: str,
    criteria_list: list,
    user_email: str,
    description: str = "",
) -> Optional[int]:
    """Insert a new saved criteria set.

    Returns the new row id, or None on failure.
    """
    _require_tables()
    try:
        sql = """
            INSERT INTO coreiq_saved_criteria
                (name, description, criteria_json, created_by)
            VALUES
                (:name, :description, :criteria_json, :created_by)
        """
        params = {
            "name":          name.strip()[:100],
            "description":   (description or "").strip(),
            "criteria_json": json.dumps(criteria_list, default=str),
            "created_by":    user_email,
        }
        new_id = db_manager.execute_insert_returning_id(sql, params)
        log_info(f"[SAVED_CRITERIA] Saved id={new_id} by={user_email} name={name!r}")
        return new_id
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="save_criteria", operation="insert")
        return None


# =============================================================================
# READ
# =============================================================================

def get_user_criteria(user_email: str) -> List[Dict]:
    """Return all active saved criteria for any logged-in user.

    All criteria are visible to every user. Permission flags
    (is_owner, can_edit, can_delete) control who can modify/delete.

    Each row includes:
      id, name, description, criteria_list (Python list), created_by,
      created_at, updated_at, is_owner (bool), can_edit (bool), can_delete (bool)
    """
    _require_tables()
    try:
        sql = """
            SELECT
                sc.id,
                sc.name,
                sc.description,
                sc.criteria_json,
                sc.created_by,
                sc.created_at,
                sc.updated_at,
                IF(sc.created_by = :email, 1, 0)                              AS is_owner,
                COALESCE(sca.can_edit,
                         IF(sc.created_by = :email, 1, 0))                    AS can_edit,
                COALESCE(sca.can_delete,
                         IF(sc.created_by = :email, 1, 0))                    AS can_delete
            FROM coreiq_saved_criteria sc
            LEFT JOIN coreiq_saved_criteria_access sca
                   ON sca.criteria_id = sc.id
                  AND sca.user_email  = :email
            WHERE sc.is_active = 1
            ORDER BY sc.updated_at DESC
        """
        rows = db_manager.execute_query_readonly(sql, {"email": user_email})
        result = []
        for row in rows:
            row = dict(row)
            try:
                row["criteria_list"] = json.loads(row.pop("criteria_json", "[]"))
            except Exception:
                row["criteria_list"] = []
            result.append(row)
        return result
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="get_user_criteria", operation="select")
        return []


def get_criteria_by_id(criteria_id: int, user_email: str) -> Optional[Dict]:
    """Fetch a single criteria row with permission flags. Returns None if not found."""
    _require_tables()
    try:
        sql = """
            SELECT
                sc.id,
                sc.name,
                sc.description,
                sc.criteria_json,
                sc.created_by,
                sc.created_at,
                sc.updated_at,
                IF(sc.created_by = :email, 1, 0)                              AS is_owner,
                COALESCE(sca.can_edit,
                         IF(sc.created_by = :email, 1, 0))                    AS can_edit,
                COALESCE(sca.can_delete,
                         IF(sc.created_by = :email, 1, 0))                    AS can_delete
            FROM coreiq_saved_criteria sc
            LEFT JOIN coreiq_saved_criteria_access sca
                   ON sca.criteria_id = sc.id
                  AND sca.user_email  = :email
            WHERE sc.id        = :id
              AND sc.is_active = 1
            LIMIT 1
        """
        rows = db_manager.execute_query_readonly(sql, {"id": criteria_id, "email": user_email})
        if not rows:
            return None
        row = dict(rows[0])
        try:
            row["criteria_list"] = json.loads(row.pop("criteria_json", "[]"))
        except Exception:
            row["criteria_list"] = []
        return row
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="get_criteria_by_id", operation="select")
        return None


# =============================================================================
# UPDATE
# =============================================================================

def update_criteria(
    criteria_id: int,
    user_email: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    criteria_list: Optional[list] = None,
) -> bool:
    """Update a saved criteria set.

    Only the creator or a user with can_edit may call this.
    Uses a conditional UPDATE (1 DB round trip instead of SELECT + UPDATE).
    Returns True on success.
    """
    _require_tables()
    try:
        updates: Dict = {}
        if name is not None:
            updates["name"] = name.strip()[:100]
        if description is not None:
            updates["description"] = description.strip()
        if criteria_list is not None:
            updates["criteria_json"] = json.dumps(criteria_list, default=str)

        if not updates:
            return True

        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        # ACL check embedded in WHERE: only update if user is creator or has can_edit.
        sql = f"""
            UPDATE coreiq_saved_criteria sc
            SET    {set_clause}
            WHERE  sc.id        = :id
              AND  sc.is_active = 1
              AND  (
                     sc.created_by = :email
                     OR EXISTS (
                         SELECT 1 FROM coreiq_saved_criteria_access sca
                         WHERE  sca.criteria_id = sc.id
                           AND  sca.user_email  = :email
                           AND  sca.can_edit    = 1
                     )
                   )
        """
        updates["id"]    = criteria_id
        updates["email"] = user_email
        affected = db_manager.execute_insert(sql, updates)
        if not affected:
            log_error(f"[SAVED_CRITERIA] update denied or not found: {user_email} id={criteria_id}")
            return False
        log_info(f"[SAVED_CRITERIA] Updated id={criteria_id} by={user_email}")
        return True
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="update_criteria", operation="update")
        return False


# =============================================================================
# DELETE (soft)
# =============================================================================

def delete_criteria(criteria_id: int, user_email: str) -> bool:
    """Soft-delete a criteria set.

    Only the creator or a user with can_delete may call this.
    Uses a conditional UPDATE (1 DB round trip instead of SELECT + UPDATE).
    """
    _require_tables()
    try:
        sql = """
            UPDATE coreiq_saved_criteria sc
            SET    is_active = 0
            WHERE  sc.id        = :id
              AND  sc.is_active = 1
              AND  (
                     sc.created_by = :email
                     OR EXISTS (
                         SELECT 1 FROM coreiq_saved_criteria_access sca
                         WHERE  sca.criteria_id = sc.id
                           AND  sca.user_email  = :email
                           AND  sca.can_delete  = 1
                     )
                   )
        """
        affected = db_manager.execute_insert(sql, {"id": criteria_id, "email": user_email})
        if not affected:
            log_error(f"[SAVED_CRITERIA] delete denied or not found: {user_email} id={criteria_id}")
            return False
        log_info(f"[SAVED_CRITERIA] Soft-deleted id={criteria_id} by={user_email}")
        return True
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="delete_criteria", operation="soft_delete")
        return False


# =============================================================================
# ACCESS MANAGEMENT  (only creator may grant/revoke)
# =============================================================================

def grant_access(
    criteria_id: int,
    target_email: str,
    granter_email: str,
    can_edit: bool = False,
    can_delete: bool = False,
) -> bool:
    """Grant (or update) access to a criteria set.

    Only the creator (created_by == granter_email) may grant.
    Uses INSERT-SELECT to embed the ownership check — 1 DB round trip instead of
    the previous SELECT-then-INSERT (2 RTTs).
    """
    _require_tables()
    if target_email == granter_email:
        return True  # no-op; creator always has full access
    try:
        sql = """
            INSERT INTO coreiq_saved_criteria_access
                (criteria_id, user_email, granted_by, can_edit, can_delete)
            SELECT :criteria_id, :user_email, :granted_by, :can_edit, :can_delete
            FROM   coreiq_saved_criteria
            WHERE  id         = :criteria_id
              AND  is_active  = 1
              AND  created_by = :granted_by
            ON DUPLICATE KEY UPDATE
                can_edit   = VALUES(can_edit),
                can_delete = VALUES(can_delete),
                granted_by = VALUES(granted_by)
        """
        affected = db_manager.execute_insert(sql, {
            "criteria_id": criteria_id,
            "user_email":  target_email,
            "granted_by":  granter_email,
            "can_edit":    int(can_edit),
            "can_delete":  int(can_delete),
        })
        if not affected:
            log_error(f"[SAVED_CRITERIA] grant denied: {granter_email} not owner of id={criteria_id}")
            return False
        log_info(f"[SAVED_CRITERIA] Granted id={criteria_id} to={target_email} "
                 f"edit={can_edit} delete={can_delete} by={granter_email}")
        return True
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="grant_access", operation="upsert_access")
        return False


def revoke_access(criteria_id: int, target_email: str, granter_email: str) -> bool:
    """Revoke a user's access. Only creator may revoke.

    Uses a multi-table DELETE with JOIN to embed the ownership check — 1 DB round
    trip instead of the previous SELECT-then-DELETE (2 RTTs).
    """
    _require_tables()
    try:
        sql = """
            DELETE sca
            FROM   coreiq_saved_criteria_access sca
            JOIN   coreiq_saved_criteria sc ON sc.id = sca.criteria_id
            WHERE  sca.criteria_id = :cid
              AND  sca.user_email  = :email
              AND  sc.created_by   = :granter
              AND  sc.is_active    = 1
        """
        db_manager.execute_insert(sql, {
            "cid":     criteria_id,
            "email":   target_email,
            "granter": granter_email,
        })
        log_info(f"[SAVED_CRITERIA] Revoked id={criteria_id} from={target_email}")
        return True
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="revoke_access", operation="delete_access")
        return False


def get_access_grants(criteria_id: int, owner_email: str) -> List[Dict]:
    """Return all access grants for a criteria set (owner view only) — 1 DB round trip."""
    _require_tables()
    try:
        # Single query: INNER JOIN ensures we only return rows when the caller
        # is the owner AND there are grant rows.
        sql = """
            SELECT sca.user_email, sca.granted_by, sca.can_edit,
                   sca.can_delete, sca.granted_at
            FROM   coreiq_saved_criteria sc
            JOIN   coreiq_saved_criteria_access sca ON sca.criteria_id = sc.id
            WHERE  sc.id        = :id
              AND  sc.is_active = 1
              AND  sc.created_by = :owner
            ORDER BY sca.granted_at DESC
        """
        return [dict(r) for r in db_manager.execute_query_readonly(
            sql, {"id": criteria_id, "owner": owner_email}
        )]
    except Exception as exc:
        log_structured_error(exc, page="saved_criteria_service",
                             component="get_access_grants", operation="select_grants")
        return []
