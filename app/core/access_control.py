"""
Access Control Manager - handles per-page IAM access checks and management
Table: coreiq_page_access_control
- page_name: identifier of the admin page
- user_email: user's email
- permission_level: allow, deny, upload_only, delete_admin, view_only, edit
"""
import streamlit as st
from typing import List, Dict, Optional, Tuple
from core.database import DatabaseManager
from utils.server_logger import log_error, log_structured_error

db = DatabaseManager()

class AccessControlManager:
    """Manage page-level access control via coreiq_page_access_control table"""

    @staticmethod
    def check_access(page_name: str, user_email: str, required_permission: str = "allow") -> bool:
        """
        Check if user has required access to a page.

        Args:
            page_name: Page identifier (e.g., 'forecasting_admin', 'retailer_adding')
            user_email: User email to check
            required_permission: Required permission level to grant access
                - 'allow': Any positive permission (allow, upload_only, delete_admin, view_only, edit)
                - 'view_only': Read-only access (view_only and above)
                - 'edit': Edit access (edit and above)
                - 'upload_only': Upload capability
                - 'delete_admin': Admin delete capability

        Returns:
            True if user has required access, False otherwise
        """
        if not user_email or not page_name:
            return False

        try:
            # Admin and super_user always have full access to every page — no ACL row needed
            role = UserRolesManager.get_user_role(user_email)
            if role in ("admin", "super_user"):
                return True

            query = """
                SELECT permission_level FROM coreiq_page_access_control
                WHERE page_name = :page_name AND user_email = :user_email
                LIMIT 1
            """
            results = db.execute_query_readonly(query, {
                "page_name": page_name,
                "user_email": user_email.strip().lower()
            })

            if not results:
                log_error(f"[ACL] access_denied page={page_name} user={user_email}")
                return False

            perm = results[0].get("permission_level")
            if perm == "deny":
                log_error(f"[ACL] access_denied(explicit) page={page_name} user={user_email}")
                return False

            # Check if permission meets requirement
            permission_hierarchy = {
                "view_only": 1,
                "upload_only": 2,
                "edit": 3,
                "delete_admin": 4,
                "allow": 4,  # "allow" is general positive permission
            }

            req_level = permission_hierarchy.get(required_permission, 0)
            user_level = permission_hierarchy.get(perm, 0)

            granted = user_level >= req_level
            log_error(f"[ACL] access_check page={page_name} user={user_email} perm={perm} req={required_permission} granted={granted}")
            return granted

        except Exception as exc:
            log_structured_error(exc, page="access_control", operation="check_access",
                               context=f"page={page_name} user={user_email}")
            return False

    @staticmethod
    def get_page_users(page_name: str) -> List[Dict[str, str]]:
        """Get all users and their permissions for a page"""
        try:
            query = """
                SELECT user_email, permission_level, created_at, updated_at, created_by
                FROM coreiq_page_access_control
                WHERE page_name = :page_name
                ORDER BY user_email
            """
            results = db.execute_query_readonly(query, {"page_name": page_name})
            return [
                {
                    "email": r.get("user_email"),
                    "permission": r.get("permission_level"),
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                    "created_by": r.get("created_by"),
                }
                for r in results
            ]
        except Exception as exc:
            log_structured_error(exc, page="access_control", operation="get_page_users",
                               context=f"page={page_name}")
            return []

    @staticmethod
    def set_user_access(page_name: str, user_email: str, permission_level: str,
                       created_by: str = "system") -> Tuple[bool, str]:
        """
        Add or update user access for a page.

        Returns:
            (success, message)
        """
        valid_permissions = {"allow", "deny", "upload_only", "delete_admin", "view_only", "edit"}
        if permission_level not in valid_permissions:
            return False, f"Invalid permission level: {permission_level}"

        try:
            query = """
                INSERT INTO coreiq_page_access_control
                (page_name, user_email, permission_level, created_by)
                VALUES (:page_name, :user_email, :permission_level, :created_by)
                ON DUPLICATE KEY UPDATE
                permission_level = VALUES(permission_level), updated_at = NOW()
            """
            rows_affected = db.execute_insert(query, {
                "page_name": page_name,
                "user_email": user_email.strip().lower(),
                "permission_level": permission_level,
                "created_by": created_by,
            })
            log_error(f"[ACL] user_access_set page={page_name} user={user_email} perm={permission_level} rows={rows_affected}")
            return True, f"Access updated: {user_email} → {permission_level}"

        except Exception as exc:
            log_structured_error(exc, page="access_control", operation="set_user_access",
                               context=f"page={page_name} user={user_email} perm={permission_level}")
            return False, f"Failed to update access: {str(exc)}"

    @staticmethod
    def remove_user_access(page_name: str, user_email: str) -> Tuple[bool, str]:
        """Remove user access from a page"""
        try:
            query = """
                DELETE FROM coreiq_page_access_control
                WHERE page_name = :page_name AND user_email = :user_email
            """
            db.execute_insert(query, {
                "page_name": page_name,
                "user_email": user_email.strip().lower(),
            })
            log_error(f"[ACL] user_access_removed page={page_name} user={user_email}")
            return True, f"Access removed: {user_email}"

        except Exception as exc:
            log_structured_error(exc, page="access_control", operation="remove_user_access",
                               context=f"page={page_name} user={user_email}")
            return False, f"Failed to remove access: {str(exc)}"

    @staticmethod
    def get_permission_options() -> List[str]:
        """Get available permission levels"""
        return ["allow", "deny", "view_only", "upload_only", "edit", "delete_admin"]

    @staticmethod
    def get_permission_description(permission: str) -> str:
        """Get human-readable description of a permission"""
        descriptions = {
            "allow": "Full access",
            "deny": "No access (explicitly denied)",
            "view_only": "View only - no edit/upload/delete",
            "upload_only": "Upload files/data only",
            "edit": "Edit and upload (no delete)",
            "delete_admin": "Admin - all permissions including delete",
        }
        return descriptions.get(permission, "Unknown permission")


# ─────────────────────────────────────────────────────────────────────────────
# UserRolesManager  —  system-level role management (coreiq_user_roles table)
# Roles: admin > super_user > user
# ─────────────────────────────────────────────────────────────────────────────

class UserRolesManager:
    """
    Manage system-level user roles.
    Table: coreiq_user_roles (user_email, display_name, role, created_by)
    """

    VALID_ROLES = {"admin", "super_user", "user"}

    # Fallback hard-coded admins used when the DB table is unavailable
    _FALLBACK_ADMINS = {
        "mohdsaeedafri@coresight.com",
        "philipmoore@coresight.com",
        "shashankgupta@coresight.com",
    }

    @staticmethod
    def get_user_role(user_email: str) -> Optional[str]:
        """Get role for a user. Returns None if not found."""
        if not user_email:
            return None
        try:
            query = """
                SELECT role FROM coreiq_user_roles
                WHERE user_email = :user_email LIMIT 1
            """
            results = db.execute_query_readonly(
                query, {"user_email": user_email.strip().lower()}
            )
            if results:
                return results[0].get("role")
            return None
        except Exception as exc:
            log_structured_error(
                exc, page="access_control", operation="get_user_role",
                context=f"user={user_email}"
            )
            # Fallback: treat hard-coded admins as admin
            if user_email.strip().lower() in UserRolesManager._FALLBACK_ADMINS:
                return "admin"
            return None

    @staticmethod
    def get_all_users() -> List[Dict]:
        """Return all users ordered by role hierarchy then email."""
        try:
            query = """
                SELECT user_email, display_name, role,
                       created_at, updated_at, created_by
                FROM coreiq_user_roles
                ORDER BY FIELD(role, 'admin', 'super_user', 'user'), user_email
            """
            results = db.execute_query_readonly(query, {})
            return list(results) if results else []
        except Exception as exc:
            log_structured_error(
                exc, page="access_control", operation="get_all_users"
            )
            return []

    @staticmethod
    def set_user_role(
        user_email: str,
        role: str,
        display_name: str = "",
        created_by: str = "system",
    ) -> Tuple[bool, str]:
        """Add or update a user's role (upsert)."""
        if role not in UserRolesManager.VALID_ROLES:
            return False, f"Invalid role: {role}"
        try:
            query = """
                INSERT INTO coreiq_user_roles
                    (user_email, display_name, role, created_by)
                VALUES
                    (:user_email, :display_name, :role, :created_by)
                ON DUPLICATE KEY UPDATE
                    display_name = VALUES(display_name),
                    role         = VALUES(role),
                    updated_at   = NOW()
            """
            db.execute_insert(query, {
                "user_email":   user_email.strip().lower(),
                "display_name": (display_name or "").strip(),
                "role":         role,
                "created_by":   created_by,
            })
            # Bust session-level role cache
            import streamlit as _st
            _st.session_state.pop(
                f"_urm_role_{user_email.strip().lower()}", None
            )
            return True, f"Role set: {user_email} → {role}"
        except Exception as exc:
            log_structured_error(
                exc, page="access_control", operation="set_user_role",
                context=f"user={user_email} role={role}"
            )
            return False, f"Failed: {str(exc)}"

    @staticmethod
    def delete_user(user_email: str) -> Tuple[bool, str]:
        """Delete a user from the roles table (Admin-only operation)."""
        try:
            query = "DELETE FROM coreiq_user_roles WHERE user_email = :user_email"
            db.execute_insert(query, {"user_email": user_email.strip().lower()})
            import streamlit as _st
            _st.session_state.pop(
                f"_urm_role_{user_email.strip().lower()}", None
            )
            return True, f"User deleted: {user_email}"
        except Exception as exc:
            log_structured_error(
                exc, page="access_control", operation="delete_user",
                context=f"user={user_email}"
            )
            return False, f"Failed: {str(exc)}"

    # ── Convenience checks ────────────────────────────────────────────────────

    @staticmethod
    def is_admin(user_email: str) -> bool:
        return UserRolesManager.get_user_role(user_email) == "admin"

    @staticmethod
    def is_admin_or_super_user(user_email: str) -> bool:
        return UserRolesManager.get_user_role(user_email) in ("admin", "super_user")

    @staticmethod
    def can_delete_users(user_email: str) -> bool:
        """Only admins can delete users."""
        return UserRolesManager.is_admin(user_email)

    @staticmethod
    def can_grant_delete_permission(user_email: str) -> bool:
        """Only admins can grant delete_admin page permission."""
        return UserRolesManager.is_admin(user_email)
