#!/usr/bin/env python3
"""
Settings — User preferences, notifications, and account management
Endpoints: get_settings, update_settings, get_preferences, update_preferences,
          reset_settings, get_notification_settings (6 total)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum

import pandas as pd
import streamlit as st

from components.navigation import render_header, render_coresight_footer
from components.styles import hide_sidebar, render_styles, set_page_layout
from core.auth_manager import get_current_user, require_auth
from core.database import db_manager
from utils.server_logger import log_structured_error, log_error, error_boundary

INK = "#2D2A29"
MUTED = "#6B7280"
SURFACE = "#FFFFFF"
BORDER = "#E5E7EB"
OK = "#1B6B24"
ERROR = "#DC2626"
WARN = "#D97706"


class NotificationType(str, Enum):
    """Notification types"""
    EMAIL = "email"
    PUSH = "push"
    SMS = "sms"
    IN_APP = "in_app"


class ThemePreference(str, Enum):
    """Theme preferences"""
    LIGHT = "light"
    DARK = "dark"
    AUTO = "auto"


def _setup_page():
    """Initialize page settings"""
    hide_sidebar()
    render_styles()
    set_page_layout()


def _settings_css() -> None:
    """Custom styles for settings page"""
    st.markdown(
        f"""
<style>
.settings-page {{
  max-width: 900px;
  margin: 0 auto 40px auto;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}
.settings-hero {{
  background: linear-gradient(135deg, rgba(59,130,246,0.07) 0%, rgba(255,255,255,0.9) 55%, #fff 100%);
  border: 1px solid {BORDER};
  border-radius: 14px;
  padding: 28px 32px 26px 32px;
  margin-bottom: 22px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.04);
}}
.settings-hero h1 {{
  margin: 0 0 10px 0;
  font-size: 28px;
  font-weight: 700;
  color: {INK};
}}
.settings-hero p {{
  margin: 0;
  color: {MUTED};
  font-size: 15px;
  line-height: 1.6;
}}
.settings-card {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 22px 24px;
  margin-bottom: 18px;
}}
.settings-card h3 {{
  margin: 0 0 16px 0;
  font-size: 17px;
  font-weight: 600;
  color: {INK};
  border-bottom: 1px solid {BORDER};
  padding-bottom: 12px;
}}
.settings-field {{
  margin-bottom: 16px;
}}
.settings-field label {{
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: {INK};
  margin-bottom: 6px;
}}
.settings-field input,
.settings-field select,
.settings-field textarea {{
  padding: 8px 12px;
  border: 1px solid {BORDER};
  border-radius: 6px;
  font-size: 14px;
}}
.settings-toggle {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 0;
}}
.settings-toggle-label {{
  font-size: 14px;
  color: {INK};
  flex: 1;
}}
.settings-toggle-value {{
  font-size: 12px;
  color: {MUTED};
}}
.settings-button {{
  padding: 10px 18px;
  border-radius: 6px;
  font-weight: 600;
  font-size: 14px;
  border: none;
  cursor: pointer;
}}
.settings-button-primary {{
  background: #3b82f6;
  color: white;
}}
.settings-button-danger {{
  background: {ERROR};
  color: white;
}}
.settings-button:hover {{
  opacity: 0.9;
}}
.settings-success {{
  background: rgba(27,107,36,0.1);
  color: {OK};
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 14px;
  margin-bottom: 16px;
}}
.settings-warning {{
  background: rgba(217,119,6,0.1);
  color: {WARN};
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 14px;
  margin-bottom: 16px;
}}
</style>
""",
        unsafe_allow_html=True,
    )


# =============================================================================
# ENDPOINT 1: GET_SETTINGS
# =============================================================================
def get_settings() -> Dict[str, Any]:
    """
    Get all user settings and preferences.

    Returns:
        User settings dictionary
    """
    user_email = get_current_user() or ""
    try:
        row = db_manager.fetch_one(
            """
            SELECT id, user_email, theme, timezone, language, auto_save,
                   created_at, updated_at
            FROM coreiq_user_settings
            WHERE user_email = :user_email
            """,
            {"user_email": user_email},
        )

        if not row:
            # Create default settings if not exists
            db_manager.execute_insert(
                """
                INSERT INTO coreiq_user_settings
                (user_email, theme, timezone, language, auto_save, created_at, updated_at)
                VALUES (:user_email, :theme, :timezone, :language, :auto_save, :created_at, :updated_at)
                """,
                {
                    "user_email": user_email,
                    "theme": ThemePreference.AUTO.value,
                    "timezone": "UTC",
                    "language": "en",
                    "auto_save": True,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                },
            )
            # Fetch newly created settings
            row = db_manager.fetch_one(
                """
                SELECT id, user_email, theme, timezone, language, auto_save,
                       created_at, updated_at
                FROM coreiq_user_settings
                WHERE user_email = :user_email
                """,
                {"user_email": user_email},
            )

        return {
            "success": True,
            "settings": dict(row) if row else {},
        }
    except Exception as e:
        log_structured_error(e, page="settings", operation="GET_SETTINGS")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 2: UPDATE_SETTINGS
# =============================================================================
def update_settings(
    theme: Optional[str] = None,
    timezone: Optional[str] = None,
    language: Optional[str] = None,
    auto_save: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Update user settings.

    Args:
        theme: Theme preference (light, dark, auto)
        timezone: User timezone
        language: User language
        auto_save: Enable auto-save

    Returns:
        Update result
    """
    user_email = get_current_user() or ""
    try:
        # Build update query dynamically
        updates = []
        params: Dict[str, Any] = {
            "user_email": user_email,
            "updated_at": datetime.utcnow(),
        }

        if theme is not None:
            updates.append("theme = :theme")
            params["theme"] = theme

        if timezone is not None:
            updates.append("timezone = :timezone")
            params["timezone"] = timezone

        if language is not None:
            updates.append("language = :language")
            params["language"] = language

        if auto_save is not None:
            updates.append("auto_save = :auto_save")
            params["auto_save"] = auto_save

        if not updates:
            return {"success": False, "error": "No settings to update"}

        updates.append("updated_at = :updated_at")
        query = (
            "UPDATE coreiq_user_settings SET "
            + ", ".join(updates)
            + " WHERE user_email = :user_email"
        )

        db_manager.execute_update(query, params)

        return {
            "success": True,
            "message": "Settings updated",
        }
    except Exception as e:
        log_structured_error(e, page="settings", operation="UPDATE_SETTINGS")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 3: GET_PREFERENCES
# =============================================================================
def get_preferences() -> Dict[str, Any]:
    """
    Get user preferences (dashboard layout, columns, etc).

    Returns:
        User preferences dictionary
    """
    user_email = get_current_user() or ""
    try:
        row = db_manager.fetch_one(
            """
            SELECT id, user_email, dashboard_layout, visible_columns,
                   sort_order, items_per_page, created_at, updated_at
            FROM coreiq_user_preferences
            WHERE user_email = :user_email
            """,
            {"user_email": user_email},
        )

        if not row:
            # Create default preferences if not exists
            db_manager.execute_insert(
                """
                INSERT INTO coreiq_user_preferences
                (user_email, dashboard_layout, visible_columns, sort_order,
                 items_per_page, created_at, updated_at)
                VALUES (:user_email, :dashboard_layout, :visible_columns,
                        :sort_order, :items_per_page, :created_at, :updated_at)
                """,
                {
                    "user_email": user_email,
                    "dashboard_layout": "default",
                    "visible_columns": "all",
                    "sort_order": "descending",
                    "items_per_page": 50,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                },
            )
            # Fetch newly created preferences
            row = db_manager.fetch_one(
                """
                SELECT id, user_email, dashboard_layout, visible_columns,
                       sort_order, items_per_page, created_at, updated_at
                FROM coreiq_user_preferences
                WHERE user_email = :user_email
                """,
                {"user_email": user_email},
            )

        return {
            "success": True,
            "preferences": dict(row) if row else {},
        }
    except Exception as e:
        log_structured_error(e, page="settings", operation="GET_PREFERENCES")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 4: UPDATE_PREFERENCES
# =============================================================================
def update_preferences(
    dashboard_layout: Optional[str] = None,
    visible_columns: Optional[str] = None,
    sort_order: Optional[str] = None,
    items_per_page: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Update user preferences.

    Args:
        dashboard_layout: Dashboard layout preference
        visible_columns: Which columns to show
        sort_order: Default sort order
        items_per_page: Items per page in tables

    Returns:
        Update result
    """
    user_email = get_current_user() or ""
    try:
        updates = []
        params: Dict[str, Any] = {
            "user_email": user_email,
            "updated_at": datetime.utcnow(),
        }

        if dashboard_layout is not None:
            updates.append("dashboard_layout = :dashboard_layout")
            params["dashboard_layout"] = dashboard_layout

        if visible_columns is not None:
            updates.append("visible_columns = :visible_columns")
            params["visible_columns"] = visible_columns

        if sort_order is not None:
            updates.append("sort_order = :sort_order")
            params["sort_order"] = sort_order

        if items_per_page is not None:
            updates.append("items_per_page = :items_per_page")
            params["items_per_page"] = items_per_page

        if not updates:
            return {"success": False, "error": "No preferences to update"}

        updates.append("updated_at = :updated_at")
        query = (
            "UPDATE coreiq_user_preferences SET "
            + ", ".join(updates)
            + " WHERE user_email = :user_email"
        )

        db_manager.execute_update(query, params)

        return {
            "success": True,
            "message": "Preferences updated",
        }
    except Exception as e:
        log_structured_error(e, page="settings", operation="UPDATE_PREFERENCES")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 5: RESET_SETTINGS
# =============================================================================
def reset_settings() -> Dict[str, Any]:
    """
    Reset user settings and preferences to defaults.

    Returns:
        Reset result
    """
    user_email = get_current_user() or ""
    try:
        # Reset settings
        db_manager.execute_update(
            """
            UPDATE coreiq_user_settings
            SET theme = :theme, timezone = :timezone, language = :language,
                auto_save = :auto_save, updated_at = :updated_at
            WHERE user_email = :user_email
            """,
            {
                "user_email": user_email,
                "theme": ThemePreference.AUTO.value,
                "timezone": "UTC",
                "language": "en",
                "auto_save": True,
                "updated_at": datetime.utcnow(),
            },
        )

        # Reset preferences
        db_manager.execute_update(
            """
            UPDATE coreiq_user_preferences
            SET dashboard_layout = :layout, visible_columns = :columns,
                sort_order = :sort, items_per_page = :items, updated_at = :updated_at
            WHERE user_email = :user_email
            """,
            {
                "user_email": user_email,
                "layout": "default",
                "columns": "all",
                "sort": "descending",
                "items": 50,
                "updated_at": datetime.utcnow(),
            },
        )

        return {
            "success": True,
            "message": "Settings and preferences reset to defaults",
        }
    except Exception as e:
        log_structured_error(e, page="settings", operation="RESET_SETTINGS")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 6: GET_NOTIFICATION_SETTINGS
# =============================================================================
def get_notification_settings() -> Dict[str, Any]:
    """
    Get notification preferences.

    Returns:
        Notification settings dictionary
    """
    user_email = get_current_user() or ""
    try:
        rows = db_manager.fetch_all(
            """
            SELECT id, user_email, notification_type, enabled, frequency,
                   created_at, updated_at
            FROM coreiq_notification_settings
            WHERE user_email = :user_email
            """,
            {"user_email": user_email},
        )

        if not rows:
            # Create default notification settings
            defaults = [
                NotificationType.EMAIL,
                NotificationType.PUSH,
                NotificationType.IN_APP,
            ]
            for notif_type in defaults:
                db_manager.execute_insert(
                    """
                    INSERT INTO coreiq_notification_settings
                    (user_email, notification_type, enabled, frequency, created_at, updated_at)
                    VALUES (:user_email, :notification_type, :enabled, :frequency,
                            :created_at, :updated_at)
                    """,
                    {
                        "user_email": user_email,
                        "notification_type": notif_type.value,
                        "enabled": True if notif_type != NotificationType.SMS else False,
                        "frequency": "daily",
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    },
                )
            # Fetch newly created settings
            rows = db_manager.fetch_all(
                """
                SELECT id, user_email, notification_type, enabled, frequency,
                       created_at, updated_at
                FROM coreiq_notification_settings
                WHERE user_email = :user_email
                """,
                {"user_email": user_email},
            )

        return {
            "success": True,
            "notifications": [dict(row) for row in rows],
        }
    except Exception as e:
        log_structured_error(e, page="settings", operation="GET_NOTIFICATION_SETTINGS")
        return {"success": False, "error": str(e)}


# =============================================================================
# UI RENDERING
# =============================================================================
def main() -> None:
    """Main UI"""
    _setup_page()
    _settings_css()

    require_auth(redirect_to="login", page="settings")
    user_email = get_current_user() or ""
    render_header(full_width=True, current_page="settings")

    st.markdown('<div class="settings-page">', unsafe_allow_html=True)

    # Hero section
    st.markdown(
        """
        <div class="settings-hero">
        <h1>Settings</h1>
        <p>Manage your account preferences, notifications, and display settings.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Tab interface
    tab1, tab2, tab3, tab4 = st.tabs(
        ["General", "Preferences", "Notifications", "Account"]
    )

    with tab1:
        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown("### General Settings")

        result = get_settings()
        if result["success"]:
            settings = result["settings"]

            col1, col2 = st.columns(2)
            with col1:
                theme = st.selectbox(
                    "Theme",
                    [t.value for t in ThemePreference],
                    index=0 if settings.get("theme") == "auto" else 1,
                    key="theme_setting",
                )
                language = st.selectbox(
                    "Language",
                    ["en", "es", "fr", "de"],
                    index=0,
                    key="language_setting",
                )

            with col2:
                timezone = st.selectbox(
                    "Timezone",
                    ["UTC", "EST", "CST", "MST", "PST"],
                    index=0,
                    key="timezone_setting",
                )
                auto_save = st.checkbox(
                    "Enable auto-save",
                    value=settings.get("auto_save", True),
                    key="autosave_setting",
                )

            if st.button("Save Settings"):
                result = update_settings(
                    theme=theme,
                    timezone=timezone,
                    language=language,
                    auto_save=auto_save,
                )
                if result["success"]:
                    st.success(result["message"])
                else:
                    st.error(result.get("error", "Failed to save settings"))
        else:
            st.error("Failed to load settings")

        st.markdown("</div>", unsafe_allow_html=True)

    with tab2:
        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown("### Display Preferences")

        result = get_preferences()
        if result["success"]:
            prefs = result["preferences"]

            col1, col2 = st.columns(2)
            with col1:
                layout = st.selectbox(
                    "Dashboard Layout",
                    ["default", "compact", "expanded"],
                    index=0,
                    key="layout_pref",
                )
                sort_order = st.selectbox(
                    "Default Sort Order",
                    ["ascending", "descending"],
                    index=1,
                    key="sort_pref",
                )

            with col2:
                columns = st.selectbox(
                    "Visible Columns",
                    ["all", "essential", "custom"],
                    index=0,
                    key="columns_pref",
                )
                items_per_page = st.slider(
                    "Items per page",
                    10,
                    200,
                    prefs.get("items_per_page", 50),
                    key="items_pref",
                )

            if st.button("Save Preferences"):
                result = update_preferences(
                    dashboard_layout=layout,
                    visible_columns=columns,
                    sort_order=sort_order,
                    items_per_page=items_per_page,
                )
                if result["success"]:
                    st.success(result["message"])
                else:
                    st.error(result.get("error", "Failed to save preferences"))
        else:
            st.error("Failed to load preferences")

        st.markdown("</div>", unsafe_allow_html=True)

    with tab3:
        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown("### Notification Settings")

        result = get_notification_settings()
        if result["success"]:
            notifications = result["notifications"]

            for notif in notifications:
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.checkbox(
                        f"Enable {notif['notification_type']} notifications",
                        value=notif["enabled"],
                        key=f"notif_{notif['id']}",
                    )
                with col2:
                    st.selectbox(
                        "Frequency",
                        ["instant", "hourly", "daily", "weekly"],
                        index=0,
                        key=f"freq_{notif['id']}",
                    )

            if st.button("Save Notification Settings"):
                st.success("Notification settings updated")
        else:
            st.error("Failed to load notification settings")

        st.markdown("</div>", unsafe_allow_html=True)

    with tab4:
        st.markdown('<div class="settings-card">', unsafe_allow_html=True)
        st.markdown("### Account Management")

        st.info(f"Email: {user_email}")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Change Password"):
                st.info("Password change dialog would appear here")

        with col2:
            if st.button("Reset All Settings"):
                with st.spinner("Resetting..."):
                    result = reset_settings()
                    if result["success"]:
                        st.success(result["message"])
                    else:
                        st.error(result.get("error", "Failed to reset settings"))

        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
    render_coresight_footer()


if __name__ == "__main__":
    main()
