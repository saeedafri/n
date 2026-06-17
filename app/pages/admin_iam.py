#!/usr/bin/env python3
"""
Admin IAM Panel — Centralized access control management for all admin pages
Restricted to: shashankgupta@coresight.com, risthavilinadsa@coresight.com, mohdsaeedafri@coresight.com
"""
from __future__ import annotations

import streamlit as st

from components.navigation import render_header, render_coresight_footer
from components.styles import hide_sidebar, render_styles, set_page_layout
from core.auth_manager import get_current_user, require_auth
from core.access_control import AccessControlManager

# Hardcoded admin emails
ADMIN_USERS = {
    "shashankgupta@coresight.com",
    "risthavilinadsa@coresight.com",
    "PhilipMoore@coresight.com",
    "mohdsaeedafri@coresight.com",
}

# Pages that can have access control
MANAGED_PAGES = [
    "forecasting_admin",
    "retailer_adding",
    "company_filings_add_files",
]

PAGE_DISPLAY_NAMES = {
    "forecasting_admin": "Forecasting Admin",
    "retailer_adding": "Retailer Adding",
    "company_filings_add_files": "Company Filings File Manager",
}

# Colors
INK = "#2D2A29"
MUTED = "#6B7280"
SURFACE = "#FFFFFF"
BORDER = "#E5E7EB"
OK = "#1B6B24"
WARN = "#D97706"
ERROR = "#DC2626"


def _setup_page():
    hide_sidebar()
    render_styles()
    set_page_layout()


def _admin_css() -> None:
    st.markdown(
        f"""
<style>
.iam-page {{
  background: {SURFACE};
  color: {INK};
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}
.iam-hero {{
  background: linear-gradient(135deg, rgba(59,130,246,0.07) 0%, rgba(255,255,255,0.9) 55%, #fff 100%);
  border: 1px solid {BORDER};
  border-radius: 14px;
  padding: 28px 32px 26px 32px;
  margin-bottom: 22px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.04);
}}
.iam-hero h1 {{
  margin: 0 0 10px 0;
  font-size: 28px;
  font-weight: 700;
  color: {INK};
}}
.iam-hero p {{
  margin: 0;
  color: {MUTED};
  font-size: 15px;
  line-height: 1.6;
}}
.iam-pill {{
  display: inline-block;
  background: rgba(59,130,246,0.1);
  color: #1e40af;
  padding: 6px 12px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 12px;
}}
.iam-card {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 10px;
  padding: 20px;
  margin-bottom: 18px;
}}
.iam-card h2 {{
  margin: 0 0 16px 0;
  font-size: 18px;
  font-weight: 600;
  color: {INK};
}}
.iam-muted {{
  color: {MUTED};
  font-size: 14px;
  line-height: 1.5;
}}
.iam-field-label {{
  font-size: 13px;
  font-weight: 600;
  color: {INK};
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
  display: block;
}}
.iam-user-row {{
  display: grid;
  grid-template-columns: 2fr 1.5fr 1fr 0.5fr;
  gap: 12px;
  align-items: center;
  padding: 14px;
  border-bottom: 1px solid {BORDER};
  font-size: 14px;
}}
.iam-user-row:last-child {{
  border-bottom: none;
}}
.iam-user-email {{
  font-weight: 500;
  color: {INK};
  word-break: break-all;
}}
.iam-user-perm {{
  background: #f3f4f6;
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
}}
.iam-user-date {{
  color: {MUTED};
  font-size: 12px;
}}
.iam-user-actions {{
  display: flex;
  gap: 6px;
}}
.iam-permission-badge {{
  display: inline-block;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
}}
.iam-perm-allow {{
  background: #dbeafe;
  color: #1e40af;
}}
.iam-perm-edit {{
  background: #fef3c7;
  color: #92400e;
}}
.iam-perm-upload {{
  background: #dbeafe;
  color: #1e40af;
}}
.iam-perm-delete {{
  background: #fee2e2;
  color: #991b1b;
}}
.iam-perm-deny {{
  background: #f3f4f6;
  color: #374151;
}}
.iam-stats {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}}
.iam-stat {{
  background: #f9fafb;
  border: 1px solid {BORDER};
  border-radius: 8px;
  padding: 12px;
  text-align: center;
}}
.iam-stat-value {{
  font-size: 20px;
  font-weight: 700;
  color: {INK};
}}
.iam-stat-label {{
  font-size: 12px;
  color: {MUTED};
  margin-top: 4px;
}}
</style>
""",
        unsafe_allow_html=True,
    )


def _get_permission_color(perm: str) -> str:
    """Get CSS class for permission badge color"""
    if perm == "allow":
        return "iam-perm-allow"
    elif perm == "edit":
        return "iam-perm-edit"
    elif perm in ("upload_only", "view_only"):
        return "iam-perm-upload"
    elif perm == "delete_admin":
        return "iam-perm-delete"
    elif perm == "deny":
        return "iam-perm-deny"
    return "iam-perm-allow"


def main():
    _setup_page()
    # require_auth()

    user_email = get_current_user()
    if not user_email:
        st.error("Unable to identify user")
        return

    # Restrict access to hardcoded admins
    if user_email.lower() not in ADMIN_USERS:
        _admin_css()
        st.markdown('<div class="iam-page">', unsafe_allow_html=True)
        st.markdown(
            f"""
<div class="iam-hero">
  <div class="iam-pill">Admin Panel</div>
  <h1>Access Denied</h1>
  <p>This panel is restricted to system administrators only.</p>
</div>
""",
            unsafe_allow_html=True,
        )
        render_coresight_footer(full_width=True, stick_to_bottom=True)
        st.markdown("</div>", unsafe_allow_html=True)
        return

    _admin_css()
    render_header()
    st.markdown('<div class="iam-page">', unsafe_allow_html=True)

    # Hero section
    st.markdown(
        f"""
<div class="iam-hero">
  <div class="iam-pill">Admin · Access Control</div>
  <h1>IAM Access Control Panel</h1>
  <p>Centralized management of user access to all admin pages. Manage permissions, add/remove users, and control who can access what.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    # Page selector
    st.markdown('<div class="iam-card">', unsafe_allow_html=True)
    st.markdown("<h2>Select Page to Manage</h2>", unsafe_allow_html=True)

    col1, col2 = st.columns([2, 3])
    with col1:
        selected_page = st.selectbox(
            "Choose page",
            MANAGED_PAGES,
            format_func=lambda x: PAGE_DISPLAY_NAMES.get(x, x),
            label_visibility="collapsed",
            key="page_selector"
        )

    with col2:
        page_display_name = PAGE_DISPLAY_NAMES.get(selected_page, selected_page)
        st.markdown(f'<p class="iam-muted" style="margin-top:8px;"><strong>{page_display_name}</strong> — Manage user access to this admin page</p>', unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

    # Get page users
    page_users = AccessControlManager.get_page_users(selected_page)
    permissions = AccessControlManager.get_permission_options()

    # Stats
    st.markdown('<div class="iam-card">', unsafe_allow_html=True)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Users", len(page_users) if page_users else 0)
    with col2:
        allow_count = sum(1 for u in (page_users or []) if (u.get("permission_level") or u.get("permission")) == "allow")
        st.metric("Allow Access", allow_count)
    with col3:
        delete_count = sum(1 for u in (page_users or []) if (u.get("permission_level") or u.get("permission")) == "delete_admin")
        st.metric("Delete Admin", delete_count)

    st.markdown("</div>", unsafe_allow_html=True)

    # Add new user section with modal
    st.markdown('<div class="iam-card">', unsafe_allow_html=True)
    st.markdown("<h2>Add New User</h2>", unsafe_allow_html=True)
    st.markdown('<p class="iam-muted" style="margin-top:0;">Grant access to a new user for this page.</p>', unsafe_allow_html=True)

    col1, col2, col3 = st.columns([2, 1.5, 1], gap="small")
    with col1:
        new_email = st.text_input("Email address", placeholder="user@coresight.com", label_visibility="collapsed", key="add_new_email")
    with col2:
        new_perm = st.selectbox("Permission", permissions, label_visibility="collapsed", key="add_new_perm", index=0)
    with col3:
        add_btn = st.button("➕ Add User", use_container_width=True, key="btn_add_user", type="primary")

    if add_btn and new_email.strip():
        success, msg = AccessControlManager.set_user_access(
            selected_page, new_email.strip().lower(), new_perm, user_email
        )
        if success:
            st.success(f"✓ {msg}")
            st.rerun()
        else:
            st.error(f"✗ {msg}")

    st.markdown("</div>", unsafe_allow_html=True)

    # Current users list
    st.markdown('<div class="iam-card">', unsafe_allow_html=True)
    st.markdown("<h2>Current Users & Permissions</h2>", unsafe_allow_html=True)

    if not page_users:
        st.markdown('<p class="iam-muted">No users configured for this page yet. Add one above to get started.</p>', unsafe_allow_html=True)
    else:
        # Header row
        st.markdown(
            '<div class="iam-user-row" style="background:#f9fafb;font-weight:600;border-radius:8px 8px 0 0;">'
            '<div>Email</div>'
            '<div>Permission</div>'
            '<div>Updated</div>'
            '<div>Actions</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        # User rows
        for idx, user in enumerate(page_users):
            email = user.get("user_email") or user.get("email", "unknown")
            perm = user.get("permission_level") or user.get("permission", "unknown")
            updated = user.get("updated_at", "")
            # Handle both datetime objects and strings
            if updated:
                date_str = str(updated).split()[0] if isinstance(updated, str) else updated.strftime("%Y-%m-%d")
            else:
                date_str = "-"

            col1, col2, col3, col4 = st.columns([2, 1.5, 1, 0.5], gap="small", vertical_alignment="center")

            with col1:
                st.markdown(
                    f'<div style="font-weight:500;color:{INK};padding:8px 0;">{email}</div>',
                    unsafe_allow_html=True,
                )

            with col2:
                # Permission selector
                new_perm = st.selectbox(
                    "Permission",
                    permissions,
                    index=permissions.index(perm) if perm in permissions else 0,
                    label_visibility="collapsed",
                    key=f"perm_{idx}_{email}",
                )
                if new_perm != perm:
                    success, msg = AccessControlManager.set_user_access(selected_page, email, new_perm, user_email)
                    if success:
                        st.success(f"✓ {msg}")
                        st.rerun()
                    else:
                        st.error(f"✗ {msg}")

            with col3:
                st.markdown(
                    f'<div style="color:{MUTED};font-size:12px;padding:8px 0;">{date_str}</div>',
                    unsafe_allow_html=True,
                )

            with col4:
                if st.button("🗑️", key=f"del_{idx}_{email}", use_container_width=True, help="Remove user"):
                    success, msg = AccessControlManager.remove_user_access(selected_page, email)
                    if success:
                        st.success(f"✓ {msg}")
                        st.rerun()
                    else:
                        st.error(f"✗ {msg}")

    st.markdown("</div>", unsafe_allow_html=True)

    # Permission legend
    st.markdown('<div class="iam-card">', unsafe_allow_html=True)
    st.markdown("<h2>Permission Levels</h2>", unsafe_allow_html=True)

    perm_docs = {
        "deny": "Explicitly blocked access",
        "view_only": "Read-only access",
        "upload_only": "Can upload and create",
        "edit": "Can edit without delete",
        "allow": "Full general access",
        "delete_admin": "Admin - all permissions including delete",
    }

    cols = st.columns(3)
    for idx, (perm, desc) in enumerate(perm_docs.items()):
        with cols[idx % 3]:
            st.markdown(
                f'<div class="iam-permission-badge {_get_permission_color(perm)}">{perm.replace("_", " ").title()}</div>'
                f'<p class="iam-muted" style="margin:8px 0 0 0;">{desc}</p>',
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
    render_coresight_footer(full_width=True, stick_to_bottom=True)


main()
