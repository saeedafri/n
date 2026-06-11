#!/usr/bin/env python3
"""
Audit — System audit logs and user activity tracking
Endpoints: get_logs, get_user_activity, get_system_changes, export_audit (4 total)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from enum import Enum

import pandas as pd
import streamlit as st

from components.navigation import render_header, render_coresight_footer
from components.styles import hide_sidebar, render_styles, set_page_layout
from core.auth_manager import get_current_user, require_auth
from core.access_control import AccessControlManager
from core.database import db_manager
from utils.server_logger import log_structured_error, log_error, error_boundary

INK = "#2D2A29"
MUTED = "#6B7280"
SURFACE = "#FFFFFF"
BORDER = "#E5E7EB"
OK = "#1B6B24"
ERROR = "#DC2626"
WARN = "#D97706"


class AuditActionType(str, Enum):
    """Audit action types"""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    READ = "read"
    EXPORT = "export"
    LOGIN = "login"
    LOGOUT = "logout"
    PERMISSION_CHANGE = "permission_change"


def _setup_page():
    """Initialize page settings"""
    hide_sidebar()
    render_styles()
    set_page_layout()


def _audit_css() -> None:
    """Custom styles for audit page"""
    st.markdown(
        f"""
<style>
.audit-page {{
  max-width: 1200px;
  margin: 0 auto 40px auto;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}
.audit-hero {{
  background: linear-gradient(135deg, rgba(220,38,38,0.07) 0%, rgba(255,255,255,0.9) 55%, #fff 100%);
  border: 1px solid {BORDER};
  border-radius: 14px;
  padding: 28px 32px 26px 32px;
  margin-bottom: 22px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.04);
}}
.audit-hero h1 {{
  margin: 0 0 10px 0;
  font-size: 28px;
  font-weight: 700;
  color: {INK};
}}
.audit-hero p {{
  margin: 0;
  color: {MUTED};
  font-size: 15px;
  line-height: 1.6;
}}
.audit-card {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 22px 24px;
  margin-bottom: 18px;
}}
.audit-card h3 {{
  margin: 0 0 14px 0;
  font-size: 17px;
  font-weight: 600;
  color: {INK};
}}
.audit-badge {{
  display: inline-block;
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
}}
.audit-badge-create {{
  background: rgba(27,107,36,0.1);
  color: {OK};
}}
.audit-badge-update {{
  background: rgba(59,130,246,0.1);
  color: #1e40af;
}}
.audit-badge-delete {{
  background: rgba(220,38,38,0.1);
  color: {ERROR};
}}
.audit-badge-read {{
  background: rgba(107,114,128,0.1);
  color: {MUTED};
}}
.audit-table {{
  font-size: 13px;
  line-height: 1.6;
}}
.audit-timeline {{
  border-left: 2px solid {BORDER};
  padding-left: 20px;
}}
.audit-timeline-item {{
  margin-bottom: 20px;
  padding-bottom: 20px;
  border-bottom: 1px solid {BORDER};
}}
.audit-timeline-item:last-child {{
  border-bottom: none;
}}
</style>
""",
        unsafe_allow_html=True,
    )


# =============================================================================
# ENDPOINT 1: GET_LOGS
# =============================================================================
def get_logs(
    limit: int = 100,
    offset: int = 0,
    action_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Get audit logs with filtering.

    Args:
        limit: Max results to return
        offset: Pagination offset
        action_type: Filter by action type
        date_from: Start date filter
        date_to: End date filter

    Returns:
        Audit logs list
    """
    # Check admin access
    user_email = get_current_user() or ""
    access_mgr = AccessControlManager()
    if not access_mgr.is_admin(user_email):
        return {"success": False, "error": "Admin access required"}

    try:
        query = "SELECT id, user_email, action, entity_type, entity_id, details, timestamp FROM coreiq_audit_logs WHERE 1=1"
        params: Dict[str, Any] = {}

        if action_type:
            query += " AND action = :action"
            params["action"] = action_type

        if date_from:
            query += " AND timestamp >= :date_from"
            params["date_from"] = date_from

        if date_to:
            query += " AND timestamp <= :date_to"
            params["date_to"] = date_to

        query += " ORDER BY timestamp DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset

        rows = db_manager.fetch_all(query, params)
        return {
            "success": True,
            "count": len(rows),
            "logs": [dict(row) for row in rows],
        }
    except Exception as e:
        log_structured_error(e, page="audit", operation="GET_LOGS")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 2: GET_USER_ACTIVITY
# =============================================================================
def get_user_activity(
    user_email: str,
    limit: int = 50,
    offset: int = 0,
    days_back: int = 30,
) -> Dict[str, Any]:
    """
    Get all activity for a specific user.

    Args:
        user_email: Email of user to track
        limit: Max results to return
        offset: Pagination offset
        days_back: How many days to look back

    Returns:
        User activity log
    """
    # Check admin access
    current_user = get_current_user() or ""
    access_mgr = AccessControlManager()
    if not access_mgr.is_admin(current_user):
        return {"success": False, "error": "Admin access required"}

    try:
        date_threshold = datetime.utcnow() - timedelta(days=days_back)
        rows = db_manager.fetch_all(
            """
            SELECT id, action, entity_type, entity_id, details, timestamp
            FROM coreiq_audit_logs
            WHERE user_email = :user_email AND timestamp >= :date_threshold
            ORDER BY timestamp DESC
            LIMIT :limit OFFSET :offset
            """,
            {
                "user_email": user_email,
                "date_threshold": date_threshold,
                "limit": limit,
                "offset": offset,
            },
        )

        # Get summary stats
        stats = db_manager.fetch_one(
            """
            SELECT
              COUNT(*) as total_actions,
              COUNT(DISTINCT DATE(timestamp)) as active_days,
              MIN(timestamp) as first_activity,
              MAX(timestamp) as last_activity
            FROM coreiq_audit_logs
            WHERE user_email = :user_email AND timestamp >= :date_threshold
            """,
            {
                "user_email": user_email,
                "date_threshold": date_threshold,
            },
        )

        return {
            "success": True,
            "user": user_email,
            "period_days": days_back,
            "stats": dict(stats) if stats else {},
            "activity": [dict(row) for row in rows],
        }
    except Exception as e:
        log_structured_error(e, page="audit", operation="GET_USER_ACTIVITY")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 3: GET_SYSTEM_CHANGES
# =============================================================================
def get_system_changes(
    entity_type: str,
    entity_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Get all changes to a specific entity or entity type.

    Args:
        entity_type: Type of entity (watchlist, criteria, company, etc)
        entity_id: Optional specific entity ID
        limit: Max results to return
        offset: Pagination offset

    Returns:
        Change history for entity
    """
    # Check admin access
    user_email = get_current_user() or ""
    access_mgr = AccessControlManager()
    if not access_mgr.is_admin(user_email):
        return {"success": False, "error": "Admin access required"}

    try:
        query = """
            SELECT id, user_email, action, entity_id, details, timestamp
            FROM coreiq_audit_logs
            WHERE entity_type = :entity_type AND action IN ('create', 'update', 'delete')
        """
        params: Dict[str, Any] = {"entity_type": entity_type}

        if entity_id:
            query += " AND entity_id = :entity_id"
            params["entity_id"] = entity_id

        query += " ORDER BY timestamp DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset

        rows = db_manager.fetch_all(query, params)

        return {
            "success": True,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "changes": [dict(row) for row in rows],
        }
    except Exception as e:
        log_structured_error(e, page="audit", operation="GET_SYSTEM_CHANGES")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 4: EXPORT_AUDIT
# =============================================================================
def export_audit(
    format: str = "csv",
    days_back: int = 90,
    include_filter: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Export audit logs for compliance and archival.

    Args:
        format: Export format (csv, json, excel)
        days_back: How many days to include
        include_filter: Optional filter dict

    Returns:
        Export result with file path or data
    """
    # Check admin access
    user_email = get_current_user() or ""
    access_mgr = AccessControlManager()
    if not access_mgr.is_admin(user_email):
        return {"success": False, "error": "Admin access required"}

    try:
        date_threshold = datetime.utcnow() - timedelta(days=days_back)

        query = """
            SELECT id, user_email, action, entity_type, entity_id, details, timestamp
            FROM coreiq_audit_logs
            WHERE timestamp >= :date_threshold
            ORDER BY timestamp DESC
        """
        params: Dict[str, Any] = {"date_threshold": date_threshold}

        if include_filter and "user_email" in include_filter:
            query = query.replace(
                "WHERE timestamp",
                "WHERE user_email = :filter_user AND timestamp",
            )
            params["filter_user"] = include_filter["user_email"]

        rows = db_manager.fetch_all(query, params)
        logs_data = [dict(row) for row in rows]

        # Record the export action itself
        db_manager.execute_insert(
            """
            INSERT INTO coreiq_audit_logs
            (user_email, action, entity_type, entity_id, details, timestamp)
            VALUES (:user_email, :action, :entity_type, :entity_id, :details, :timestamp)
            """,
            {
                "user_email": user_email,
                "action": "export",
                "entity_type": "audit_logs",
                "entity_id": None,
                "details": f"Exported {len(logs_data)} logs as {format}",
                "timestamp": datetime.utcnow(),
            },
        )

        return {
            "success": True,
            "format": format,
            "count": len(logs_data),
            "period_days": days_back,
            "exported_at": datetime.utcnow().isoformat(),
            "data": logs_data,
        }
    except Exception as e:
        log_structured_error(e, page="audit", operation="EXPORT_AUDIT")
        return {"success": False, "error": str(e)}


# =============================================================================
# UI RENDERING
# =============================================================================
def main() -> None:
    """Main UI"""
    _setup_page()
    _audit_css()

    # require_auth(redirect_to="login", page="audit")
    user_email = get_current_user() or ""

    # Check admin access
    access_mgr = AccessControlManager()
    if not access_mgr.is_admin(user_email):
        st.error("Access denied. Admin privileges required.")
        return

    render_header(full_width=True, current_page="audit")

    st.markdown('<div class="audit-page">', unsafe_allow_html=True)

    # Hero section
    st.markdown(
        """
        <div class="audit-hero">
        <h1>Audit Logs</h1>
        <p>Track system changes, user activity, and maintain compliance records.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Tab interface
    tab1, tab2, tab3, tab4 = st.tabs(
        ["System Logs", "User Activity", "Entity Changes", "Export"]
    )

    with tab1:
        st.markdown('<div class="audit-card">', unsafe_allow_html=True)
        st.markdown("### System Audit Logs")

        col1, col2 = st.columns(2)
        with col1:
            action_filter = st.selectbox(
                "Action Type",
                ["All"] + [a.value for a in AuditActionType],
                key="action_filter",
            )
        with col2:
            days = st.slider("Days back", 1, 365, 30, key="days_filter")

        date_from = datetime.utcnow() - timedelta(days=days)

        if st.button("Load Logs"):
            with st.spinner("Loading audit logs..."):
                result = get_logs(
                    action_type=None if action_filter == "All" else action_filter,
                    date_from=date_from,
                )
                if result["success"] and result["logs"]:
                    df = pd.DataFrame(result["logs"])
                    st.dataframe(df, use_container_width=True)
                else:
                    st.info("No logs found")

        st.markdown("</div>", unsafe_allow_html=True)

    with tab2:
        st.markdown('<div class="audit-card">', unsafe_allow_html=True)
        st.markdown("### User Activity")

        search_email = st.text_input(
            "User Email",
            placeholder="user@coresight.com",
        )

        if st.button("Search User Activity"):
            if search_email:
                with st.spinner("Loading user activity..."):
                    result = get_user_activity(search_email)
                    if result["success"]:
                        # Show stats
                        cols = st.columns(3)
                        stats = result.get("stats", {})
                        cols[0].metric("Total Actions", stats.get("total_actions", 0))
                        cols[1].metric("Active Days", stats.get("active_days", 0))
                        cols[2].metric("Days", result.get("period_days", 0))

                        # Show activity
                        if result["activity"]:
                            df = pd.DataFrame(result["activity"])
                            st.dataframe(df, use_container_width=True)
                        else:
                            st.info("No activity found")
                    else:
                        st.error(result.get("error", "Failed to load activity"))
            else:
                st.warning("Please enter a user email")

        st.markdown("</div>", unsafe_allow_html=True)

    with tab3:
        st.markdown('<div class="audit-card">', unsafe_allow_html=True)
        st.markdown("### Entity Change History")

        col1, col2 = st.columns(2)
        with col1:
            entity_type = st.text_input(
                "Entity Type",
                placeholder="e.g., watchlist, criteria, company",
            )
        with col2:
            entity_id = st.text_input(
                "Entity ID (optional)",
                placeholder="Leave blank for all",
            )

        if st.button("View Changes"):
            if entity_type:
                with st.spinner("Loading changes..."):
                    result = get_system_changes(
                        entity_type=entity_type,
                        entity_id=entity_id or None,
                    )
                    if result["success"] and result["changes"]:
                        df = pd.DataFrame(result["changes"])
                        st.dataframe(df, use_container_width=True)
                    else:
                        st.info("No changes found")
            else:
                st.warning("Please enter an entity type")

        st.markdown("</div>", unsafe_allow_html=True)

    with tab4:
        st.markdown('<div class="audit-card">', unsafe_allow_html=True)
        st.markdown("### Export Audit Logs")

        col1, col2 = st.columns(2)
        with col1:
            export_format = st.selectbox(
                "Format",
                ["csv", "json", "excel"],
            )
        with col2:
            export_days = st.slider(
                "Days to include",
                1,
                365,
                90,
                key="export_days",
            )

        if st.button("Export Logs"):
            with st.spinner("Exporting..."):
                result = export_audit(
                    format=export_format,
                    days_back=export_days,
                )
                if result["success"]:
                    st.success(
                        f"Exported {result['count']} logs as {result['format']}"
                    )
                    st.json(result)
                else:
                    st.error(result.get("error", "Export failed"))

        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
    render_coresight_footer()


if __name__ == "__main__":
    main()
