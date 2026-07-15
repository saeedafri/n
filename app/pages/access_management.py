#!/usr/bin/env python3
"""
Access Management — Centralized IAM for user roles and page permissions.
Accessible to: Admin and Super User roles only.
"""
from __future__ import annotations

import streamlit as st

from components.navigation import render_header, render_coresight_footer
from components.styles import hide_sidebar, render_styles, set_page_layout
from time import perf_counter

from core.auth_manager import get_current_user, require_auth
from core.access_control import AccessControlManager, UserRolesManager
from utils.server_logger import log_timing, log_structured_error, new_rerun_id

new_rerun_id("access_management")

# ── Config ────────────────────────────────────────────────────────────────────

MANAGED_PAGES = [
    ("company_filings_add_files", "Company Filings File Manager"),
    ("retailer_adding",           "Retailer Adding"),
    ("forecasting_admin",         "Forecasting Admin"),
]

# Simplified 3-tier permission model exposed in UI
PAGE_PERMS = [
    ("view_only",    "View Only", "#dbeafe", "#1e40af"),
    ("edit",         "Edit",      "#fef3c7", "#92400e"),
    ("delete_admin", "Delete",    "#fee2e2", "#991b1b"),
]

ROLES = [
    ("admin",      "Admin",       "#fee2e2", "#991b1b"),
    ("super_user", "Super User",  "#fef9c3", "#78350f"),
    ("user",       "User",        "#f3f4f6", "#374151"),
]

ROLE_DESCS = {
    "admin":      "Full access — manage users, grant all permissions, delete users",
    "super_user": "Can add users and manage page permissions — cannot delete users",
    "user":       "Standard platform user — no management capabilities",
}

PERM_DISPLAY = {
    "view_only":    "View Only",
    "edit":         "Edit",
    "delete_admin": "Delete",
    "allow":        "Allow",
    "deny":         "Deny",
    "upload_only":  "Upload",
}

# ── Colours ───────────────────────────────────────────────────────────────────

INK    = "#2D2A29"
MUTED  = "#6B7280"
BORDER = "#E5E7EB"


# ── CSS ───────────────────────────────────────────────────────────────────────

def _inject_css() -> None:
    st.markdown("""
<style>
/* ── Page wrapper ── */
.am-wrap { color: #2D2A29; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }

/* ── Hero ── */
.am-hero {
  background: linear-gradient(135deg, rgba(99,102,241,.08) 0%, rgba(255,255,255,.95) 60%, #fff 100%);
  border: 1px solid #E5E7EB;
  border-radius: 16px;
  padding: 32px 36px 28px;
  margin-bottom: 24px;
  box-shadow: 0 4px 28px rgba(0,0,0,.05);
}
.am-badge {
  display: inline-block;
  background: rgba(99,102,241,.12);
  color: #4338ca;
  padding: 4px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .6px;
  text-transform: uppercase;
  margin-bottom: 12px;
}
.am-hero h1 { margin: 0 0 8px; font-size: 27px; font-weight: 800; color: #2D2A29; }
.am-hero p  { margin: 0; color: #6B7280; font-size: 14px; line-height: 1.65; }

/* ── Stat cards ── */
.am-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 22px;
}
.am-stat {
  background: #fff;
  border: 1px solid #E5E7EB;
  border-radius: 12px;
  padding: 18px 16px;
  text-align: center;
  box-shadow: 0 1px 6px rgba(0,0,0,.04);
  transition: box-shadow .2s;
}
.am-stat:hover { box-shadow: 0 4px 16px rgba(0,0,0,.08); }
.am-stat-val { font-size: 32px; font-weight: 800; line-height: 1; margin-bottom: 5px; }
.am-stat-lbl { font-size: 12px; color: #6B7280; font-weight: 500; }

/* ── Card ── */
.am-card {
  background: #fff;
  border: 1px solid #E5E7EB;
  border-radius: 12px;
  padding: 22px 24px 18px;
  margin-bottom: 18px;
  box-shadow: 0 1px 6px rgba(0,0,0,.04);
}
.am-card-title { font-size: 15px; font-weight: 700; color: #2D2A29; margin: 0 0 14px; }
.am-muted { color: #6B7280; font-size: 13px; line-height: 1.55; }

/* ── Table header ── */
.am-th {
  background: #F9FAFB;
  border: 1px solid #E5E7EB;
  border-radius: 8px 8px 0 0;
  padding: 9px 4px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .6px;
  color: #6B7280;
}
.am-divider { border: none; border-top: 1px solid #E5E7EB; margin: 4px 0 10px; }

/* ── Inline pill badges ── */
.am-pill {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .2px;
  white-space: nowrap;
}

/* ── Role description cards ── */
.am-role-card {
  background: #F9FAFB;
  border: 1px solid #E5E7EB;
  border-radius: 10px;
  padding: 14px 16px;
}

/* ── Warning banner ── */
.am-warn {
  background: #FEF3C7;
  border: 1px solid #D97706;
  color: #92400E;
  border-radius: 8px;
  padding: 11px 14px;
  font-size: 13px;
  margin: 6px 0 12px;
}

/* ── Empty state ── */
.am-empty {
  text-align: center;
  padding: 32px 0;
  color: #9CA3AF;
  font-size: 14px;
}

/* ── Responsive stats ── */
@media (max-width: 700px) {
  .am-stats { grid-template-columns: repeat(2, 1fr); }
}
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _role_pill(role: str) -> str:
    cfg = {
        "admin":      ("#fee2e2", "#991b1b", "Admin"),
        "super_user": ("#fef9c3", "#78350f", "Super User"),
        "user":       ("#f3f4f6", "#374151", "User"),
    }
    bg, fg, label = cfg.get(role, ("#f3f4f6", "#374151", role.replace("_", " ").title()))
    return f'<span class="am-pill" style="background:{bg};color:{fg};">{label}</span>'


def _perm_pill(perm: str) -> str:
    cfg = {
        "view_only":    ("#dbeafe", "#1e40af", "View Only"),
        "edit":         ("#fef3c7", "#92400e", "Edit"),
        "delete_admin": ("#fee2e2", "#991b1b", "Delete"),
        "allow":        ("#d1fae5", "#065f46", "Allow"),
        "deny":         ("#f3f4f6", "#374151", "Deny"),
        "upload_only":  ("#ede9fe", "#4c1d95", "Upload"),
    }
    bg, fg, label = cfg.get(perm, ("#f3f4f6", "#374151", perm.replace("_", " ").title()))
    return f'<span class="am-pill" style="background:{bg};color:{fg};">{label}</span>'


def _fmt_date(val) -> str:
    if not val:
        return "—"
    s = str(val)
    return s.split(" ")[0] if " " in s else s[:10]


def _db_status_warning():
    st.markdown(
        '<div class="am-warn">⚠️ <strong>Database unavailable</strong> — '
        'showing cached/empty data. Mutations are disabled until connectivity is restored.</div>',
        unsafe_allow_html=True,
    )


# ── Tab 1 — Users & Roles ─────────────────────────────────────────────────────

def _tab_users(current_user: str, is_admin: bool, db_ok: bool):
    # Load users
    users = []
    if db_ok:
        with st.spinner("Loading users…"):
            users = UserRolesManager.get_all_users()

    if not db_ok:
        _db_status_warning()

    # ── Stats ──────────────────────────────────────────────────────────────
    n_total = len(users)
    n_admin = sum(1 for u in users if u.get("role") == "admin")
    n_super = sum(1 for u in users if u.get("role") == "super_user")
    n_user  = sum(1 for u in users if u.get("role") == "user")

    st.markdown(f"""
<div class="am-stats">
  <div class="am-stat">
    <div class="am-stat-val" style="color:#2D2A29;">{n_total}</div>
    <div class="am-stat-lbl">Total Users</div>
  </div>
  <div class="am-stat">
    <div class="am-stat-val" style="color:#991b1b;">{n_admin}</div>
    <div class="am-stat-lbl">Admins</div>
  </div>
  <div class="am-stat">
    <div class="am-stat-val" style="color:#78350f;">{n_super}</div>
    <div class="am-stat-lbl">Super Users</div>
  </div>
  <div class="am-stat">
    <div class="am-stat-val" style="color:#374151;">{n_user}</div>
    <div class="am-stat-lbl">Users</div>
  </div>
</div>
""", unsafe_allow_html=True)

    # ── Add / Update User ──────────────────────────────────────────────────
    st.markdown('<div class="am-card">', unsafe_allow_html=True)
    st.markdown('<p class="am-card-title">➕  Add / Update User</p>', unsafe_allow_html=True)

    role_ids     = [r[0] for r in ROLES]
    role_labels  = [r[1] for r in ROLES]

    c1, c2, c3, c4 = st.columns([2.8, 2, 1.5, 1])
    with c1:
        new_email = st.text_input(
            "Email", placeholder="user@coresight.com",
            label_visibility="collapsed", key="am_u_email"
        )
    with c2:
        new_name = st.text_input(
            "Name", placeholder="Display name (optional)",
            label_visibility="collapsed", key="am_u_name"
        )
    with c3:
        sel_idx = st.selectbox(
            "Role", range(len(role_ids)),
            format_func=lambda i: role_labels[i],
            label_visibility="collapsed", key="am_u_role"
        )
        sel_role = role_ids[sel_idx]
    with c4:
        add_btn = st.button("Add User", type="primary", use_container_width=True, key="am_u_add")

    if add_btn:
        if not new_email.strip():
            st.warning("Email is required.")
        elif not db_ok:
            st.error("Database unavailable — cannot add user.")
        elif sel_role == "admin" and not is_admin:
            st.error("Only Admins can create Admin users.")
        else:
            with st.spinner("Saving…"):
                ok, msg = UserRolesManager.set_user_role(
                    new_email.strip(), sel_role, new_name.strip(), current_user
                )
            if ok:
                st.toast(f"✅  {msg}", icon="✅")
                st.rerun()
            else:
                st.error(f"✗  {msg}")

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Users Table ────────────────────────────────────────────────────────
    st.markdown('<div class="am-card">', unsafe_allow_html=True)

    col_title, col_filter = st.columns([3, 1])
    with col_title:
        st.markdown('<p class="am-card-title">👥  All Users</p>', unsafe_allow_html=True)
    with col_filter:
        filter_opt = st.selectbox(
            "Filter", ["All Roles", "Admin", "Super User", "User"],
            label_visibility="collapsed", key="am_u_filter"
        )

    filter_map = {
        "All Roles": None,
        "Admin": "admin",
        "Super User": "super_user",
        "User": "user",
    }
    filtered = [
        u for u in users
        if not filter_map[filter_opt] or u.get("role") == filter_map[filter_opt]
    ]

    if not filtered:
        st.markdown(
            '<div class="am-empty">No users found. Add one above to get started.</div>',
            unsafe_allow_html=True,
        )
    else:
        # Table header
        h1, h2, h3, h4, h5, h6 = st.columns([2.6, 1.6, 1.3, 1, 1.5, 0.7])
        for col, label in zip(
            [h1, h2, h3, h4, h5, h6],
            ["Email", "Display Name", "Role", "Since", "Added By", "Del"],
        ):
            with col:
                st.markdown(f"**{label}**")
        st.markdown('<hr class="am-divider"/>', unsafe_allow_html=True)

        for idx, u in enumerate(filtered):
            email = u.get("user_email", "—")
            name  = u.get("display_name") or "—"
            role  = u.get("role", "user")
            since = _fmt_date(u.get("created_at"))
            by    = (u.get("created_by") or "system")

            r1, r2, r3, r4, r5, r6 = st.columns([2.6, 1.6, 1.3, 1, 1.5, 0.7])
            with r1:
                st.markdown(
                    f'<div style="font-size:13px;font-weight:500;padding:6px 0;word-break:break-all;">'
                    f'{email}</div>',
                    unsafe_allow_html=True,
                )
            with r2:
                st.markdown(
                    f'<div style="font-size:13px;padding:6px 0;">{name}</div>',
                    unsafe_allow_html=True,
                )
            with r3:
                st.markdown(
                    f'<div style="padding:6px 0;">{_role_pill(role)}</div>',
                    unsafe_allow_html=True,
                )
            with r4:
                st.markdown(
                    f'<div style="font-size:12px;color:#6B7280;padding:6px 0;">{since}</div>',
                    unsafe_allow_html=True,
                )
            with r5:
                st.markdown(
                    f'<div style="font-size:12px;color:#6B7280;padding:6px 0;">{by}</div>',
                    unsafe_allow_html=True,
                )
            with r6:
                confirm_key = f"_del_confirm_{email}"
                is_self     = email.lower() == current_user.lower()
                can_del     = is_admin and not is_self and db_ok

                if can_del:
                    if st.session_state.get(confirm_key):
                        if st.button(
                            "✓ Sure?", key=f"am_del_confirm_{idx}",
                            type="primary", use_container_width=True,
                        ):
                            with st.spinner("Deleting…"):
                                ok, msg = UserRolesManager.delete_user(email)
                            st.session_state.pop(confirm_key, None)
                            if ok:
                                st.toast(f"🗑️  {msg}", icon="🗑️")
                                st.rerun()
                            else:
                                st.error(msg)
                    else:
                        if st.button(
                            "🗑️", key=f"am_del_{idx}_{email}",
                            use_container_width=True, help=f"Delete {email}",
                        ):
                            st.session_state[confirm_key] = True
                            st.rerun()
                elif is_self:
                    st.markdown(
                        '<div style="font-size:11px;color:#9CA3AF;padding:6px 0;">you</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<div style="font-size:11px;color:#E5E7EB;padding:6px 0;">—</div>',
                        unsafe_allow_html=True,
                    )

            # Confirm warning inline
            if st.session_state.get(f"_del_confirm_{email}"):
                st.warning(
                    f"⚠️  About to permanently delete **{email}**. "
                    "Click **✓ Sure?** to confirm, or refresh to cancel."
                )

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Role Legend ────────────────────────────────────────────────────────
    st.markdown('<div class="am-card">', unsafe_allow_html=True)
    st.markdown('<p class="am-card-title">Role Hierarchy</p>', unsafe_allow_html=True)

    rc1, rc2, rc3 = st.columns(3)
    for col, (role_id, role_label, bg, fg) in zip([rc1, rc2, rc3], ROLES):
        with col:
            st.markdown(
                f'<div class="am-role-card">'
                f'<span class="am-pill" style="background:{bg};color:{fg};margin-bottom:8px;display:inline-block;">'
                f'{role_label}</span>'
                f'<p class="am-muted" style="margin:8px 0 0;">{ROLE_DESCS[role_id]}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)


# ── Tab 2 — Page Permissions ──────────────────────────────────────────────────

def _tab_permissions(current_user: str, is_admin: bool, db_ok: bool):
    if not db_ok:
        _db_status_warning()

    # Page selector
    st.markdown('<div class="am-card">', unsafe_allow_html=True)
    st.markdown('<p class="am-card-title">📄  Select Page</p>', unsafe_allow_html=True)

    page_ids    = [p[0] for p in MANAGED_PAGES]
    page_labels = [p[1] for p in MANAGED_PAGES]

    sel_idx = st.selectbox(
        "Page", range(len(MANAGED_PAGES)),
        format_func=lambda i: page_labels[i],
        label_visibility="collapsed", key="am_p_sel"
    )
    sel_page_id    = page_ids[sel_idx]
    sel_page_label = page_labels[sel_idx]

    st.markdown(
        f'<p class="am-muted" style="margin-top:6px;">Managing access for: '
        f'<strong>{sel_page_label}</strong></p>',
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)

    # Load page users
    page_users = []
    if db_ok:
        with st.spinner("Loading access list…"):
            page_users = AccessControlManager.get_page_users(sel_page_id)

    # Stats
    c1, c2, c3, c4 = st.columns(4)
    perm_counts = {}
    for u in page_users:
        p = u.get("permission", "")
        perm_counts[p] = perm_counts.get(p, 0) + 1

    c1.metric("Total with Access", len(page_users))
    c2.metric("View Only",  perm_counts.get("view_only",    0))
    c3.metric("Edit",       perm_counts.get("edit",         0))
    c4.metric("Delete",     perm_counts.get("delete_admin", 0))

    # Grant access
    st.markdown('<div class="am-card">', unsafe_allow_html=True)
    st.markdown('<p class="am-card-title">🔑  Grant Access</p>', unsafe_allow_html=True)

    # Permission options — super_users can only grant view/edit
    perm_opts = ["view_only", "edit"] + (["delete_admin"] if is_admin else [])

    g1, g2, g3 = st.columns([3, 1.8, 1])
    with g1:
        grant_email = st.text_input(
            "User email", placeholder="user@coresight.com",
            label_visibility="collapsed", key="am_p_email"
        )
    with g2:
        sel_perm = st.selectbox(
            "Permission", perm_opts,
            format_func=lambda x: PERM_DISPLAY.get(x, x),
            label_visibility="collapsed", key="am_p_perm"
        )
    with g3:
        grant_btn = st.button(
            "Grant Access", type="primary", use_container_width=True, key="am_p_grant"
        )

    if not is_admin:
        st.markdown(
            '<p class="am-muted" style="margin-top:4px;">ℹ️  Super Users can grant '
            '<strong>View Only</strong> and <strong>Edit</strong> only. '
            'Contact an Admin to grant Delete permission.</p>',
            unsafe_allow_html=True,
        )

    # Quick-pick from known users
    all_users = UserRolesManager.get_all_users() if db_ok else []
    known_emails = [u.get("user_email", "") for u in all_users]
    if known_emails:
        picked = st.selectbox(
            "Quick-pick a known user",
            ["— type email above —"] + known_emails,
            label_visibility="visible", key="am_p_pick"
        )
        if picked != "— type email above —":
            st.caption(f"Selected: **{picked}** — copy to the email field above to confirm")

    if grant_btn:
        email_to_use = grant_email.strip()
        if not email_to_use:
            st.warning("Enter an email address.")
        elif not db_ok:
            st.error("Database unavailable — cannot grant access.")
        elif sel_perm == "delete_admin" and not is_admin:
            st.error("Only Admins can grant Delete permission.")
        else:
            with st.spinner("Granting access…"):
                ok, msg = AccessControlManager.set_user_access(
                    sel_page_id, email_to_use, sel_perm, current_user
                )
            if ok:
                st.toast("🔓  Access granted!", icon="✅")
                st.rerun()
            else:
                st.error(f"✗  {msg}")

    st.markdown("</div>", unsafe_allow_html=True)

    # Current access table
    st.markdown('<div class="am-card">', unsafe_allow_html=True)
    st.markdown(
        f'<p class="am-card-title">Current Access — {sel_page_label}</p>',
        unsafe_allow_html=True,
    )

    if not page_users:
        st.markdown(
            '<div class="am-empty">No users have access to this page yet.</div>',
            unsafe_allow_html=True,
        )
    else:
        h1, h2, h3, h4, h5 = st.columns([2.8, 1.4, 1.2, 1.2, 0.8])
        for col, label in zip([h1, h2, h3, h4, h5],
                               ["Email", "Permission", "Granted", "By", "Revoke"]):
            with col:
                st.markdown(f"**{label}**")
        st.markdown('<hr class="am-divider"/>', unsafe_allow_html=True)

        for idx, u in enumerate(page_users):
            email   = u.get("email", "—")
            perm    = u.get("permission", "—")
            updated = _fmt_date(u.get("updated_at"))
            by      = u.get("created_by") or "system"

            r1, r2, r3, r4, r5 = st.columns([2.8, 1.4, 1.2, 1.2, 0.8])
            with r1:
                st.markdown(
                    f'<div style="font-size:13px;font-weight:500;padding:5px 0;'
                    f'word-break:break-all;">{email}</div>',
                    unsafe_allow_html=True,
                )
            with r2:
                # Inline permission change
                cur_idx = perm_opts.index(perm) if perm in perm_opts else 0
                new_perm = st.selectbox(
                    "Perm", perm_opts, index=cur_idx,
                    format_func=lambda x: PERM_DISPLAY.get(x, x),
                    label_visibility="collapsed",
                    key=f"am_pp_{idx}_{email}_{sel_page_id}"
                )
                if new_perm != perm and db_ok:
                    with st.spinner("Updating…"):
                        ok, msg = AccessControlManager.set_user_access(
                            sel_page_id, email, new_perm, current_user
                        )
                    if ok:
                        st.toast("✅  Permission updated", icon="✅")
                        st.rerun()
                    else:
                        st.error(msg)
            with r3:
                st.markdown(
                    f'<div style="font-size:12px;color:#6B7280;padding:5px 0;">{updated}</div>',
                    unsafe_allow_html=True,
                )
            with r4:
                st.markdown(
                    f'<div style="font-size:12px;color:#6B7280;padding:5px 0;">{by}</div>',
                    unsafe_allow_html=True,
                )
            with r5:
                if db_ok and st.button(
                    "🚫", key=f"am_rev_{idx}_{email}_{sel_page_id}",
                    use_container_width=True, help=f"Revoke {email}"
                ):
                    with st.spinner("Revoking…"):
                        ok, msg = AccessControlManager.remove_user_access(sel_page_id, email)
                    if ok:
                        st.toast(f"🚫  Access revoked: {email}", icon="🚫")
                        st.rerun()
                    else:
                        st.error(msg)

    st.markdown("</div>", unsafe_allow_html=True)

    # Permission legend
    st.markdown('<div class="am-card">', unsafe_allow_html=True)
    st.markdown('<p class="am-card-title">Permission Levels</p>', unsafe_allow_html=True)

    lc1, lc2, lc3 = st.columns(3)
    for col, (pid, plabel, bg, fg) in zip([lc1, lc2, lc3], PAGE_PERMS):
        descs = {
            "view_only":    "Read-only — cannot edit or delete any content",
            "edit":         "Can view and edit content — no delete capability",
            "delete_admin": "Full access — view, edit and delete (Admin-only grant)",
        }
        with col:
            st.markdown(
                f'<div class="am-role-card">'
                f'<span class="am-pill" style="background:{bg};color:{fg};'
                f'margin-bottom:8px;display:inline-block;">{plabel}</span>'
                f'<p class="am-muted" style="margin:8px 0 0;">{descs[pid]}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _page_start = perf_counter()
    hide_sidebar()
    render_styles()
    set_page_layout()
    _inject_css()

    # require_auth()
    user_email = get_current_user()
    log_timing("ACCESS_MGMT_auth", (perf_counter() - _page_start) * 1000,
               f"user={user_email}", level="INFO")

    # ── DB health check (fast) ──────────────────────────────────────────────
    _db_t = perf_counter()
    db_ok = True
    try:
        from core.database import DatabaseManager
        _db = DatabaseManager()
        _db.execute_query_readonly("SELECT 1", {})
    except Exception as _db_exc:
        db_ok = False
        # This failure drives an access-control fallback (below) — it MUST be traced.
        log_structured_error(_db_exc, page="access_management",
                             component="db_health_check", operation="SELECT_1",
                             context=f"user={user_email} — falling back to hardcoded admin list")
    log_timing("ACCESS_MGMT_db_health", (perf_counter() - _db_t) * 1000,
               f"db_ok={db_ok}", level="INFO")

    # ── Role check ─────────────────────────────────────────────────────────
    _role_t = perf_counter()
    # Fall back gracefully when DB is unavailable
    if db_ok:
        role = UserRolesManager.get_user_role(user_email)
    else:
        # Use hard-coded admin list as fallback
        _fallback = {
            "mohdsaeedafri@coresight.com",
            "philipmoore@coresight.com",
            "shashankgupta@coresight.com",
        }
        role = "admin" if user_email.lower() in _fallback else None

    if role not in ("admin", "super_user"):
        render_header()
        st.markdown('<div class="am-wrap">', unsafe_allow_html=True)
        st.markdown("""
<div class="am-hero">
  <div class="am-badge">Access Management</div>
  <h1>🔒 Access Denied</h1>
  <p>This panel is restricted to <strong>Administrators</strong> and
     <strong>Super Users</strong> only. Contact your system admin if you need access.</p>
</div>
""", unsafe_allow_html=True)
        render_coresight_footer(full_width=True, stick_to_bottom=True)
        st.markdown("</div>", unsafe_allow_html=True)
        return

    log_timing("ACCESS_MGMT_role_check", (perf_counter() - _role_t) * 1000,
               f"role={role}", level="INFO")
    is_admin = (role == "admin")

    render_header()
    st.markdown('<div class="am-wrap">', unsafe_allow_html=True)

    # Hero
    st.markdown(f"""
<div class="am-hero">
  <div class="am-badge">IAM · Access Management</div>
  <h1>🔐 Access Management</h1>
  <p>
    Manage user roles and page-level permissions across the Coresight platform.
    Signed in as {_role_pill(role)} &nbsp;<code style="font-size:13px;">{user_email}</code>
    {"&nbsp;· <strong>Full admin capabilities</strong>" if is_admin else "&nbsp;· Can add users, cannot delete"}
  </p>
</div>
""", unsafe_allow_html=True)

    if not db_ok:
        st.markdown(
            '<div class="am-warn">⚠️ <strong>Database connection unavailable</strong> — '
            'read operations are disabled. The page will show empty states. '
            'Please check your network / VPN connection and refresh.</div>',
            unsafe_allow_html=True,
        )

    # Tabs
    tab1, tab2 = st.tabs(["👥  Users & Roles", "🔑  Page Permissions"])

    with tab1:
        _tab_users(user_email, is_admin, db_ok)

    with tab2:
        _tab_permissions(user_email, is_admin, db_ok)

    st.markdown("</div>", unsafe_allow_html=True)
    log_timing("ACCESS_MGMT_total_page_render", (perf_counter() - _page_start) * 1000,
               f"user={user_email} role={role} is_admin={is_admin}", level="INFO")
    render_coresight_footer(full_width=True, stick_to_bottom=True)


# Top-level safety net: never leave an admin on a raw traceback / blank screen with
# no trace — every other page has this guard; access_management was the exception.
try:
    main()
except Exception as _page_exc:
    log_structured_error(_page_exc, page="access_management", component="main",
                         operation="PAGE_RENDER")
    st.error("Access Management is temporarily unavailable. Please refresh the page.")
