"""
Portal Users Service
====================
Manages the internal Coresight portal user registry.

Table: coreiq_portal_users
  - Single source of truth for all users who can be granted
    access to Saved Criteria and Watchlists.
  - Seeded with the current Coresight team on first startup.
  - Used to drive user-picker dropdowns in the UI (no free-text email entry).

Performance:
  - All reads via execute_query_readonly (AUTOCOMMIT, no BEGIN/COMMIT RTTs).
  - Streamlit-level cache wraps the list query at 300s TTL — effectively static.
  - Table is tiny (<100 rows); queries complete <5ms on LAN.
"""

import threading
from typing import Dict, List, Optional

from core.database import db_manager

try:
    from utils.server_logger import log_error, log_info, log_structured_error
except ImportError:
    import logging
    _lg = logging.getLogger(__name__)
    log_error = _lg.error
    log_info  = _lg.info
    def log_structured_error(*a, **kw): pass


# =============================================================================
# DDL
# =============================================================================

_DDL_PORTAL_USERS = """
CREATE TABLE IF NOT EXISTS coreiq_portal_users (
    id          INT          NOT NULL AUTO_INCREMENT,
    display_name VARCHAR(120) NOT NULL,
    email        VARCHAR(255) NOT NULL,
    is_active    TINYINT(1)   NOT NULL DEFAULT 1,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_pu_email (email),
    INDEX idx_pu_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# =============================================================================
# Seed data  — Coresight team members
# =============================================================================

_SEED_USERS: List[Dict] = [
    {"display_name": "Elijah Andrew",             "email": "elijah.andrew@coresight.com"},
    {"display_name": "Aditya Kaushik",            "email": "aditya.kaushik@coresight.com"},
    {"display_name": "Anand Kumar",               "email": "anand.kumar@coresight.com"},
    {"display_name": "Anand Raj",                 "email": "anand.raj@coresight.com"},
    {"display_name": "Charlie Poon",              "email": "charlie.poon@coresight.com"},
    {"display_name": "Anusheeliya K",             "email": "anusheeliya.k@coresight.com"},
    {"display_name": "Aaron Weingott",            "email": "aaron.weingott@coresight.com"},
    {"display_name": "Aaron DSouza",              "email": "aaron.dsouza@coresight.com"},
    {"display_name": "Aysha Abid",                "email": "aysha.abid@coresight.com"},
    {"display_name": "Abhinav Tagore",            "email": "abhinav.tagore@coresight.com"},
    {"display_name": "Jiayue Zhao",               "email": "jiayue.zhao@coresight.com"},
    {"display_name": "Dhanyashree",               "email": "dhanyashree@coresight.com"},
    {"display_name": "Anna Beller",               "email": "anna.beller@coresight.com"},
    {"display_name": "Harshit Singh",             "email": "harshit.singh@coresight.com"},
    {"display_name": "John Mercer",               "email": "john.mercer@coresight.com"},
    {"display_name": "John Harmon",               "email": "john.harmon@coresight.com"},
    {"display_name": "John (JT) Blubaugh",        "email": "john.blubaugh@coresight.com"},
    {"display_name": "Jyotsna Vas",               "email": "jyotsna.vas@coresight.com"},
    {"display_name": "Keerthan Shetty",           "email": "keerthan.shetty@coresight.com"},
    {"display_name": "Madhav Pitaliya",           "email": "madhav.pitaliya@coresight.com"},
    {"display_name": "Manik Bhatia",              "email": "manik.bhatia@coresight.com"},
    {"display_name": "Max Kahn",                  "email": "max.kahn@coresight.com"},
    {"display_name": "Mohd Afri",                 "email": "mohdsaeedafri@coresight.com"},
    {"display_name": "Navya Kini",                "email": "navya.kini@coresight.com"},
    {"display_name": "Neha Bakshi",               "email": "neha.bakshi@coresight.com"},
    {"display_name": "Nidhisha Mohandas",         "email": "nidhisha.mohandas@coresight.com"},
    {"display_name": "Nitheesh Haridas",          "email": "nitheesh.haridas@coresight.com"},
    {"display_name": "Nithesh M",                 "email": "nithesh.m@coresight.com"},
    {"display_name": "Nishant Mahajan",           "email": "nishant.mahajan@coresight.com"},
    {"display_name": "Philip Moore",              "email": "philip.moore@coresight.com"},
    {"display_name": "Prerana Kotian",            "email": "prerana.kotian@coresight.com"},
    {"display_name": "Risheek R Dandekeri",       "email": "risheek.dandekeri@coresight.com"},
    {"display_name": "Ristha DSa",                "email": "ristha.dsa@coresight.com"},
    {"display_name": "Saumya Sharma",             "email": "saumya.sharma@coresight.com"},
    {"display_name": "Shashank Gupta",            "email": "shashank.gupta@coresight.com"},
    {"display_name": "Sheryl Roche",              "email": "sheryl.roche@coresight.com"},
    {"display_name": "Shweta Chavan",             "email": "shweta.chavan@coresight.com"},
    {"display_name": "Sophie Anne Luo",           "email": "sophie.luo@coresight.com"},
    {"display_name": "Steven Winnick",            "email": "steven.winnick@coresight.com"},
    {"display_name": "Sujeet Naik",               "email": "sujeet.naik@coresight.com"},
    {"display_name": "Swarooprani Muralidhar",    "email": "swarooprani.muralidhar@coresight.com"},
    {"display_name": "Vaishnavi K Nayak",         "email": "vaishnavi.nayak@coresight.com"},
]


# =============================================================================
# Bootstrap
# =============================================================================

_tables_ready = False
_tables_lock  = threading.Lock()


def ensure_tables() -> bool:
    """Create portal users table and seed it if empty. Idempotent."""
    global _tables_ready
    if _tables_ready:
        return True
    with _tables_lock:
        if _tables_ready:
            return True
        try:
            db_manager.execute_insert(_DDL_PORTAL_USERS, {})

            # Check if already seeded
            rows = db_manager.execute_query_readonly(
                "SELECT COUNT(*) AS cnt FROM coreiq_portal_users", {}
            )
            count = rows[0]["cnt"] if rows else 0
            if count == 0:
                _seed_users()

            _tables_ready = True
            log_info(f"[PORTAL_USERS] Table ready, {count} existing users")
            return True
        except Exception as exc:
            log_structured_error(exc, page="portal_users_service",
                                 component="ensure_tables", operation="create_and_seed")
            return False


def _seed_users():
    """Insert seed users — uses INSERT IGNORE to be idempotent."""
    sql = """
        INSERT IGNORE INTO coreiq_portal_users (display_name, email)
        VALUES (:display_name, :email)
    """
    inserted = 0
    for user in _SEED_USERS:
        cnt = db_manager.execute_insert(sql, user)
        inserted += cnt or 0
    log_info(f"[PORTAL_USERS] Seeded {inserted} users into coreiq_portal_users")


# =============================================================================
# READ
# =============================================================================

def get_all_portal_users(exclude_email: Optional[str] = None) -> List[Dict]:
    """Return all active portal users, optionally excluding one email.

    Returns: [{id, display_name, email, label}]
    where label = "Display Name (email)" for selectbox display.
    """
    try:
        sql = "SELECT id, display_name, email FROM coreiq_portal_users WHERE is_active = 1 ORDER BY display_name"
        rows = [dict(r) for r in db_manager.execute_query_readonly(sql, {})]
        if exclude_email:
            rows = [r for r in rows if r["email"] != exclude_email]
        for r in rows:
            r["label"] = f"{r['display_name']} ({r['email']})"
        return rows
    except Exception as exc:
        log_structured_error(exc, page="portal_users_service",
                             component="get_all_portal_users", operation="select")
        return []


def get_portal_user_by_email(email: str) -> Optional[Dict]:
    """Return a single portal user by email, or None if not found."""
    try:
        rows = db_manager.execute_query_readonly(
            "SELECT id, display_name, email FROM coreiq_portal_users "
            "WHERE email = :email AND is_active = 1 LIMIT 1",
            {"email": email},
        )
        return dict(rows[0]) if rows else None
    except Exception as exc:
        log_structured_error(exc, page="portal_users_service",
                             component="get_portal_user_by_email", operation="select")
        return None


def upsert_portal_user(display_name: str, email: str) -> bool:
    """Insert or update a portal user (admin utility)."""
    try:
        sql = """
            INSERT INTO coreiq_portal_users (display_name, email)
            VALUES (:display_name, :email)
            ON DUPLICATE KEY UPDATE
                display_name = VALUES(display_name),
                is_active    = 1
        """
        db_manager.execute_insert(sql, {"display_name": display_name, "email": email})
        return True
    except Exception as exc:
        log_structured_error(exc, page="portal_users_service",
                             component="upsert_portal_user", operation="upsert")
        return False
