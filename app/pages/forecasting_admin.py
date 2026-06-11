#!/usr/bin/env python3
"""
Forecasting Admin — refresh `coreiq_model_forecasts` when annual actuals are newer
than what was last stored. Allowlisted users only.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from utils.server_logger import new_rerun_id
new_rerun_id("forecasting_admin")
st.set_page_config(page_title="Forecasting Admin", layout="wide")

from components.loading import inject_red_spinner_css
from components.navigation import render_header, render_coresight_footer
from components.styles import hide_sidebar, render_styles, set_page_layout
from core.auth_manager import get_current_user, require_auth
from core.access_control import AccessControlManager
from data.forecast_admin_service import (
    clear_revenue_forecast_caches,
    is_forecast_admin,
    sync_all_eligible,
    sync_forecast_for_ticker,
)
from data.revenue_forecast_service import RevenueForecastService
from utils.server_logger import log_structured_error

INK = "#2D2A29"
MUTED = "#6B7280"
SURFACE = "#FFFFFF"
BORDER = "#E5E7EB"
OK = "#1B6B24"


def _hero_css() -> None:
    st.markdown(
        f"""
<style>
.fc-admin-hero {{
  background: linear-gradient(135deg, rgba(214,46,47,0.07) 0%, rgba(255,255,255,0.9) 55%, #fff 100%);
  border: 1px solid {BORDER};
  border-radius: 14px;
  padding: 28px 32px 26px 32px;
  margin-bottom: 22px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.04);
}}
.fc-admin-hero h1 {{
  margin: 0 0 10px 0;
  font-size: 26px;
  font-weight: 700;
  color: {INK};
  letter-spacing: -0.02em;
}}
.fc-admin-hero p {{
  margin: 0;
  font-size: 15px;
  line-height: 1.65;
  color: {MUTED};
}}
.fc-admin-card {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 22px 24px;
  margin-bottom: 18px;
}}
.fc-admin-card h2 {{
  margin: 0 0 14px 0;
  font-size: 17px;
  font-weight: 600;
  color: {INK};
}}
.fc-admin-muted {{
  font-size: 13px;
  color: {MUTED};
  line-height: 1.55;
}}
.fc-admin-pill {{
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(27,107,36,0.1);
  color: {OK};
  margin-bottom: 12px;
}}
.fc-deny {{
  text-align: center;
  padding: 48px 24px;
  border: 1px dashed {BORDER};
  border-radius: 12px;
  background: #fafafa;
}}
.fc-deny h2 {{ color: {INK}; margin-bottom: 8px; }}
.fc-admin-page {{
  max-width: 920px;
  margin: 0 auto 40px auto;
  font-family: 'Roboto', 'Inter', sans-serif;
}}

/* Make Streamlit form labels + metric labels bolder (requested). */
div[data-testid="stTextInput"] label p,
div[data-testid="stTextInput"] label {{
  font-weight: 700 !important;
  color: {INK} !important;
}}
div[data-testid="stButton"] button {{
  font-weight: 700 !important;
}}
div[data-testid="stMetricLabel"] {{
  font-weight: 700 !important;
}}

.fc-admin-field-label {{
  font-size: 12px;
  font-weight: 700;
  color: {INK};
  margin: 0 0 6px 0;
}}
</style>
""",
        unsafe_allow_html=True,
    )


def _results_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def main() -> None:
    _timings: list = []
    _page_start = perf_counter()

    def _tick(label: str, since: float) -> float:
        ms = (perf_counter() - since) * 1000
        _timings.append((label, ms))
        return perf_counter()

    _t = perf_counter()
    hide_sidebar()
    render_styles()
    _t = _tick("hide_sidebar + render_styles", _t)

    inject_red_spinner_css()

    # require_auth(redirect_to="login", page="forecasting_admin")  # TEMP: Commented for testing
    user_email = get_current_user() or "mohdsaeedafri@coresight.com"  # TEMP: Use test user
    _t = _tick("get_current_user", _t)

    with st.spinner("Loading…"):
        render_header(full_width=True, current_page="forecasting_admin")
    _t = _tick("render_header (nav + DB session check)", _t)

    # IAM admins always have access to the IAM panel regardless of their own permission row
    _IAM_ADMIN_EMAILS = {
        "shashankgupta@coresight.com",
        "risthavilinadsa@coresight.com",
        "philipmoore@coresight.com",
        "mohdsaeedafri@coresight.com",
    }
    is_admin = user_email.lower() in {e.lower() for e in _IAM_ADMIN_EMAILS}

    # Cache access check in session_state — Azure RTT is 270-370ms per query.
    # Key includes user_email so it invalidates automatically on user switch.
    _cache_key = f"fc_admin_access_{user_email.lower()}"
    if _cache_key not in st.session_state:
        has_access = AccessControlManager.check_access("forecasting_admin", user_email, "allow")
        st.session_state[_cache_key] = has_access
    else:
        has_access = st.session_state[_cache_key]
    _t = _tick(f"check_access ({'DB hit' if _cache_key not in st.session_state else 'session cache'})", _t)

    _hero_css()
    inject_red_spinner_css()
    st.markdown(
        """
<style>
  .fc-page-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #D62E2F;
    margin-bottom: 4px;
  }
  .fc-page-title {
    font-size: 28px;
    font-weight: 700;
    color: #2D2A29;
    letter-spacing: -0.01em;
    margin: 0 0 20px 0;
    line-height: 1.2;
  }
</style>
<div class="fc-page-label">Internal · Forecast Store</div>
<div class="fc-page-title">Forecasting Admin</div>
""",
        unsafe_allow_html=True,
    )

    # ── IAM Access Management (Admin only) ──────────────────────────────────
    MANAGED_PAGES = ["forecasting_admin", "retailer_adding", "company_filings_add_files"]
    PAGE_LABELS = {
        "forecasting_admin": "Forecasting Admin",
        "retailer_adding": "Add Retailers",
        "company_filings_add_files": "Azure File Storage",
    }

    if is_admin:
        with st.expander("🔐 Access Control — Page Permissions", expanded=False):
            st.markdown(
                '<p class="fc-admin-muted" style="margin-top:0;">Manage user access across all admin pages. '
                'Select a page to view and edit its user list.</p>',
                unsafe_allow_html=True,
            )

            # Page selector
            selected_label = st.selectbox(
                "Manage permissions for",
                options=[PAGE_LABELS[p] for p in MANAGED_PAGES],
                key="iam_page_select",
            )
            selected_page = MANAGED_PAGES[[PAGE_LABELS[p] for p in MANAGED_PAGES].index(selected_label)]

            # Stats row
            with st.spinner("Loading users…"):
                page_users = AccessControlManager.get_page_users(selected_page)
            permissions = AccessControlManager.get_permission_options()

            total_users = len(page_users)
            allow_count = sum(1 for u in page_users if u.get("permission") in ("allow", "allow_all"))
            admin_count = sum(1 for u in page_users if u.get("permission") == "delete_admin")

            stat1, stat2, stat3 = st.columns(3)
            stat1.metric("Total Users", total_users)
            stat2.metric("Allow Access", allow_count)
            stat3.metric("Delete Admin", admin_count)

            st.markdown("<hr style='margin:12px 0;border:none;border-top:1px solid #e5e7eb;'>", unsafe_allow_html=True)

            # Add new user
            st.markdown('<p class="fc-admin-field-label" style="margin-bottom:8px;">Add User</p>', unsafe_allow_html=True)
            iam_c1, iam_c2, iam_c3 = st.columns([2, 1.5, 1], gap="small")
            with iam_c1:
                new_email = st.text_input("User email", placeholder="user@coresight.com", label_visibility="collapsed", key="iam_new_email")
            with iam_c2:
                new_perm = st.selectbox("Permission", permissions, label_visibility="collapsed", key="iam_new_perm", index=0)
            with iam_c3:
                add_user_btn = st.button("Add User", use_container_width=True, key="iam_add_btn", type="primary")

            if add_user_btn and new_email.strip():
                ok, msg = AccessControlManager.set_user_access(
                    selected_page,
                    new_email.strip().lower(),
                    new_perm,
                    user_email,
                )
                if ok:
                    st.toast(msg, icon="✅")
                    st.rerun()
                else:
                    st.error(msg)

            st.markdown("<hr style='margin:12px 0;border:none;border-top:1px solid #e5e7eb;'>", unsafe_allow_html=True)

            # User table
            if page_users:
                st.markdown('<p class="fc-admin-field-label" style="margin-bottom:8px;">Current Users</p>', unsafe_allow_html=True)

                h1, h2, h3, h4, h5 = st.columns([2.2, 1.5, 0.9, 1.1, 0.5], gap="small")
                h1.markdown('<div style="font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Email</div>', unsafe_allow_html=True)
                h2.markdown('<div style="font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Permission</div>', unsafe_allow_html=True)
                h3.markdown('<div style="font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Save</div>', unsafe_allow_html=True)
                h4.markdown('<div style="font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Updated</div>', unsafe_allow_html=True)
                h5.markdown("", unsafe_allow_html=True)

                for u in page_users:
                    r1, r2, r3, r4, r5 = st.columns([2.2, 1.5, 0.9, 1.1, 0.5], gap="small", vertical_alignment="center")
                    perm_key = f"iam_perm_{selected_page}_{u['email']}"

                    with r1:
                        st.markdown(f'<div style="font-size:13px;color:#2d2a29;padding:6px 0;">{u["email"]}</div>', unsafe_allow_html=True)
                    with r2:
                        cur_perm = u["permission"]
                        st.selectbox(
                            "perm",
                            permissions,
                            index=permissions.index(cur_perm) if cur_perm in permissions else 0,
                            label_visibility="collapsed",
                            key=perm_key,
                        )
                    with r3:
                        if st.button("Save", key=f"iam_save_{selected_page}_{u['email']}", use_container_width=True, type="primary"):
                            chosen = st.session_state.get(perm_key, cur_perm)
                            ok, msg = AccessControlManager.set_user_access(selected_page, u["email"], chosen, user_email)
                            if ok:
                                st.toast(f"Updated {u['email']} → {chosen}", icon="✅")
                                st.rerun()
                            else:
                                st.error(msg)
                    with r4:
                        upd = u["updated_at"]
                        date_str = str(upd).split()[0] if isinstance(upd, str) else upd.strftime("%Y-%m-%d") if upd else "-"
                        st.markdown(f'<div style="font-size:12px;color:#6b7280;padding:6px 0;">{date_str}</div>', unsafe_allow_html=True)
                    with r5:
                        if st.button("🗑️", key=f"iam_del_{selected_page}_{u['email']}", help=f"Remove {u['email']}"):
                            ok, msg = AccessControlManager.remove_user_access(selected_page, u["email"])
                            if ok:
                                st.toast(f"Removed {u['email']}", icon="🗑️")
                                st.rerun()
                            else:
                                st.error(msg)
            else:
                st.markdown('<p class="fc-admin-muted" style="margin-top:8px;">No users configured for this page yet. Add one above.</p>', unsafe_allow_html=True)

            # Permission legend
            st.markdown("<hr style='margin:16px 0;border:none;border-top:1px solid #e5e7eb;'>", unsafe_allow_html=True)
            st.markdown('<p class="fc-admin-field-label" style="margin-bottom:8px;">Permission Legend</p>', unsafe_allow_html=True)
            legend_items = [
                ("allow", "#1B6B24", "Can access this page"),
                ("deny", "#dc2626", "Explicitly blocked"),
                ("view_only", "#6b7280", "View-only access"),
                ("upload_only", "#0369a1", "Can upload files"),
                ("edit", "#b45309", "Can edit content"),
                ("delete_admin", "#7c3aed", "Full admin — upload, edit, delete"),
            ]
            leg_cols = st.columns(3)
            for i, (label, color, desc) in enumerate(legend_items):
                with leg_cols[i % 3]:
                    st.markdown(
                        f'<div style="margin-bottom:8px;">'
                        f'<span style="display:inline-block;background:{color};color:#fff;font-size:10px;font-weight:700;'
                        f'padding:2px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.04em;">{label}</span>'
                        f'<span style="font-size:12px;color:#6b7280;margin-left:6px;">{desc}</span></div>',
                        unsafe_allow_html=True,
                    )

    run_all = False
    run_one = False
    one = ""

    if has_access:
        st.markdown("<br>", unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            run_all = st.button("Sync all stale tickers", type="primary", use_container_width=True, key="fc_sync_all")
        with c2:
            one = st.text_input("Single ticker (e.g. TSCO)", placeholder="TSCO", key="fc_one_ticker")
            run_one = st.button("Sync this ticker only", use_container_width=True, key="fc_sync_one")
    else:
        st.warning("🔒 Your access to run forecasting sync has been revoked. Contact an admin to restore access.")

    results: List[Dict[str, Any]] = []
    t0 = perf_counter()

    if run_one and (one or "").strip():
        ticker = (one or "").strip()
        try:
            _companies = RevenueForecastService.get_companies()
        except Exception:
            _companies = []
        name_map = {row.get("ticker"): row.get("name") or "" for row in _companies}
        with st.spinner(f"Processing {ticker}…"):
            row = sync_forecast_for_ticker(ticker)
            row["company_name"] = name_map.get(ticker, "")
            results = [row]
        clear_revenue_forecast_caches()
    elif run_all:
        progress = st.progress(0.0, text="Starting…")

        def _cb(cur: int, total: int, disp: str) -> None:
            frac = (cur / total) if total else 0.0
            progress.progress(min(1.0, frac), text=f"{cur}/{total} — {disp}")

        try:
            results = sync_all_eligible(force=False, periods=5, progress_callback=_cb)
        finally:
            progress.progress(1.0, text="Done")
        clear_revenue_forecast_caches()

    if results:
        updated = sum(1 for r in results if r.get("status") == "updated")
        up_to_date = sum(1 for r in results if r.get("status") == "up_to_date")
        skipped = sum(1 for r in results if r.get("status") in ("skipped", "no_data"))
        errors = sum(1 for r in results if r.get("status") == "error")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Upserted", f"{updated:,}")
        m2.metric("Already current", f"{up_to_date:,}")
        m3.metric("Skipped / no data", f"{skipped:,}")
        m4.metric("Errors", f"{errors:,}")

        st.caption(f"Finished in {(perf_counter() - t0):.1f}s · Streamlit caches cleared for Revenue Estimates.")

        df = _results_df(results)
        if not df.empty and "status" in df.columns:
            show = df[
                ["ticker", "company_name", "status", "rows_upserted", "message"]
            ].copy() if "company_name" in df.columns else df[["ticker", "status", "rows_upserted", "message"]]
            st.dataframe(show, use_container_width=True, height=min(520, 36 + len(show) * 36))

    _t = perf_counter()
    render_coresight_footer(full_width=True, stick_to_bottom=True)
    _timings.append(("render_footer", (perf_counter() - _t) * 1000))
    _total_ms = (perf_counter() - _page_start) * 1000

    with st.expander(f"⏱ Page load diagnostics  —  total {_total_ms:.0f}ms", expanded=False):
        rows = ""
        for label, ms in _timings:
            bar_w = min(int(ms / 5), 200)
            color = "#D62E2F" if ms > 300 else "#1B6B24" if ms < 50 else "#b45309"
            rows += (
                f'<tr>'
                f'<td style="padding:5px 12px 5px 0;font-size:13px;color:#2d2a29;white-space:nowrap;">{label}</td>'
                f'<td style="padding:5px 12px 5px 0;font-size:13px;font-weight:700;color:{color};white-space:nowrap;">{ms:.1f}ms</td>'
                f'<td style="padding:5px 0;"><div style="height:8px;width:{bar_w}px;background:{color};border-radius:4px;opacity:.7;"></div></td>'
                f'</tr>'
            )
        st.markdown(
            f'<table style="border-collapse:collapse;width:100%;">{rows}'
            f'<tr><td colspan="3"><hr style="border:none;border-top:1px solid #e5e7eb;margin:6px 0;"></td></tr>'
            f'<tr><td style="font-size:13px;font-weight:700;color:#2d2a29;">TOTAL</td>'
            f'<td style="font-size:13px;font-weight:700;color:{"#D62E2F" if _total_ms>1000 else "#1B6B24"};">{_total_ms:.0f}ms</td>'
            f'<td></td></tr></table>',
            unsafe_allow_html=True,
        )


main()
