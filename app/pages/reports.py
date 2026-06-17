#!/usr/bin/env python3
"""
Reports — Generate and manage data reports
Endpoints: create, list, download, delete, export, schedule, cancel, status (8 total)
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


class ReportType(str, Enum):
    """Report types"""
    SCREENING = "screening"
    EARNINGS = "earnings"
    ESTIMATES = "estimates"
    FILINGS = "filings"
    MARKET_DATA = "market_data"


class ReportStatus(str, Enum):
    """Report statuses"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SCHEDULED = "scheduled"


def _setup_page():
    """Initialize page settings"""
    hide_sidebar()
    render_styles()
    set_page_layout()


def _reports_css() -> None:
    """Custom styles for reports page"""
    st.markdown(
        f"""
<style>
.reports-page {{
  max-width: 1200px;
  margin: 0 auto 40px auto;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}
.reports-hero {{
  background: linear-gradient(135deg, rgba(31,41,55,0.07) 0%, rgba(255,255,255,0.9) 55%, #fff 100%);
  border: 1px solid {BORDER};
  border-radius: 14px;
  padding: 28px 32px 26px 32px;
  margin-bottom: 22px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.04);
}}
.reports-hero h1 {{
  margin: 0 0 10px 0;
  font-size: 28px;
  font-weight: 700;
  color: {INK};
}}
.reports-hero p {{
  margin: 0;
  color: {MUTED};
  font-size: 15px;
  line-height: 1.6;
}}
.reports-card {{
  background: {SURFACE};
  border: 1px solid {BORDER};
  border-radius: 12px;
  padding: 22px 24px;
  margin-bottom: 18px;
}}
.reports-card h3 {{
  margin: 0 0 14px 0;
  font-size: 17px;
  font-weight: 600;
  color: {INK};
}}
.reports-status {{
  display: inline-block;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
}}
.reports-status-completed {{
  background: rgba(27,107,36,0.1);
  color: {OK};
}}
.reports-status-pending {{
  background: rgba(217,119,6,0.1);
  color: {WARN};
}}
.reports-status-failed {{
  background: rgba(220,38,38,0.1);
  color: {ERROR};
}}
.reports-table {{
  font-size: 14px;
  line-height: 1.6;
}}
</style>
""",
        unsafe_allow_html=True,
    )


# =============================================================================
# ENDPOINT 1: CREATE_REPORT
# =============================================================================
def create_report(
    report_type: str,
    filters: Dict[str, Any],
    format: str = "csv",
    schedule: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new report.

    Args:
        report_type: Type of report (screening, earnings, estimates, filings, market_data)
        filters: Filter criteria for the report
        format: Output format (csv, excel, json)
        schedule: Optional cron schedule for recurring reports

    Returns:
        Report creation result with report_id
    """
    user_email = get_current_user() or ""
    try:
        result = db_manager.execute_insert_returning_id(
            """
            INSERT INTO coreiq_reports
            (user_email, report_type, filters, format, status, schedule, created_at)
            VALUES (:user_email, :report_type, :filters, :format, :status, :schedule, :created_at)
            """,
            {
                "user_email": user_email,
                "report_type": report_type,
                "filters": str(filters),
                "format": format,
                "status": ReportStatus.PENDING.value,
                "schedule": schedule,
                "created_at": datetime.utcnow(),
            },
        )
        return {
            "success": True,
            "report_id": result,
            "message": f"Report created with ID {result}",
        }
    except Exception as e:
        log_structured_error(e, page="reports", operation="CREATE_REPORT")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 2: LIST_REPORTS
# =============================================================================
def list_reports(
    limit: int = 50,
    offset: int = 0,
    status_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    List all reports for current user.

    Args:
        limit: Max results to return
        offset: Pagination offset
        status_filter: Filter by status

    Returns:
        List of reports with metadata
    """
    user_email = get_current_user() or ""
    try:
        query = """
            SELECT id, report_type, status, format, schedule, created_at, completed_at
            FROM coreiq_reports
            WHERE user_email = :user_email
        """
        params = {"user_email": user_email}

        if status_filter:
            query += " AND status = :status"
            params["status"] = status_filter

        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset

        rows = db_manager.fetch_all(query, params)
        return {
            "success": True,
            "count": len(rows),
            "reports": [dict(row) for row in rows],
        }
    except Exception as e:
        log_structured_error(e, page="reports", operation="LIST_REPORTS")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 3: DOWNLOAD_REPORT
# =============================================================================
def download_report(report_id: int) -> Dict[str, Any]:
    """
    Download a completed report.

    Args:
        report_id: ID of report to download

    Returns:
        Report data and metadata
    """
    user_email = get_current_user() or ""
    try:
        row = db_manager.fetch_one(
            """
            SELECT id, report_type, format, data, created_at, completed_at
            FROM coreiq_reports
            WHERE id = :report_id AND user_email = :user_email AND status = :status
            """,
            {
                "report_id": report_id,
                "user_email": user_email,
                "status": ReportStatus.COMPLETED.value,
            },
        )

        if not row:
            return {"success": False, "error": "Report not found or not completed"}

        return {
            "success": True,
            "report": dict(row),
        }
    except Exception as e:
        log_structured_error(e, page="reports", operation="DOWNLOAD_REPORT")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 4: DELETE_REPORT
# =============================================================================
def delete_report(report_id: int) -> Dict[str, Any]:
    """
    Delete a report.

    Args:
        report_id: ID of report to delete

    Returns:
        Deletion result
    """
    user_email = get_current_user() or ""
    try:
        result = db_manager.execute_delete(
            """
            DELETE FROM coreiq_reports
            WHERE id = :report_id AND user_email = :user_email
            """,
            {"report_id": report_id, "user_email": user_email},
        )

        if result == 0:
            return {"success": False, "error": "Report not found"}

        return {"success": True, "message": "Report deleted"}
    except Exception as e:
        log_structured_error(e, page="reports", operation="DELETE_REPORT")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 5: EXPORT_REPORT
# =============================================================================
def export_report(report_id: int, export_format: str) -> Dict[str, Any]:
    """
    Export a report in different format.

    Args:
        report_id: ID of report to export
        export_format: Format to export (csv, excel, json)

    Returns:
        Export result with file URL or data
    """
    user_email = get_current_user() or ""
    try:
        row = db_manager.fetch_one(
            """
            SELECT id, data, format
            FROM coreiq_reports
            WHERE id = :report_id AND user_email = :user_email AND status = :status
            """,
            {
                "report_id": report_id,
                "user_email": user_email,
                "status": ReportStatus.COMPLETED.value,
            },
        )

        if not row:
            return {"success": False, "error": "Report not found"}

        # Update export format in database
        db_manager.execute_update(
            """
            UPDATE coreiq_reports
            SET format = :new_format
            WHERE id = :report_id
            """,
            {"new_format": export_format, "report_id": report_id},
        )

        return {
            "success": True,
            "message": f"Report exported as {export_format}",
            "format": export_format,
        }
    except Exception as e:
        log_structured_error(e, page="reports", operation="EXPORT_REPORT")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 6: SCHEDULE_REPORT
# =============================================================================
def schedule_report(
    report_id: int,
    cron_schedule: str,
) -> Dict[str, Any]:
    """
    Schedule a report for recurring generation.

    Args:
        report_id: ID of report to schedule
        cron_schedule: Cron expression for schedule

    Returns:
        Schedule result
    """
    user_email = get_current_user() or ""
    try:
        result = db_manager.execute_update(
            """
            UPDATE coreiq_reports
            SET schedule = :schedule, status = :status
            WHERE id = :report_id AND user_email = :user_email
            """,
            {
                "report_id": report_id,
                "schedule": cron_schedule,
                "status": ReportStatus.SCHEDULED.value,
                "user_email": user_email,
            },
        )

        if result == 0:
            return {"success": False, "error": "Report not found"}

        return {
            "success": True,
            "message": f"Report scheduled with cron: {cron_schedule}",
        }
    except Exception as e:
        log_structured_error(e, page="reports", operation="SCHEDULE_REPORT")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 7: CANCEL_REPORT
# =============================================================================
def cancel_report(report_id: int) -> Dict[str, Any]:
    """
    Cancel a scheduled or pending report.

    Args:
        report_id: ID of report to cancel

    Returns:
        Cancellation result
    """
    user_email = get_current_user() or ""
    try:
        result = db_manager.execute_update(
            """
            UPDATE coreiq_reports
            SET schedule = NULL, status = :status
            WHERE id = :report_id AND user_email = :user_email
            AND status IN (:pending, :scheduled)
            """,
            {
                "report_id": report_id,
                "user_email": user_email,
                "status": ReportStatus.PENDING.value,
                "pending": ReportStatus.PENDING.value,
                "scheduled": ReportStatus.SCHEDULED.value,
            },
        )

        if result == 0:
            return {"success": False, "error": "Report not found or cannot be cancelled"}

        return {"success": True, "message": "Report cancelled"}
    except Exception as e:
        log_structured_error(e, page="reports", operation="CANCEL_REPORT")
        return {"success": False, "error": str(e)}


# =============================================================================
# ENDPOINT 8: GET_REPORT_STATUS
# =============================================================================
def get_report_status(report_id: int) -> Dict[str, Any]:
    """
    Get status of a report.

    Args:
        report_id: ID of report

    Returns:
        Report status and metadata
    """
    user_email = get_current_user() or ""
    try:
        row = db_manager.fetch_one(
            """
            SELECT id, report_type, status, progress, created_at, completed_at, error_message
            FROM coreiq_reports
            WHERE id = :report_id AND user_email = :user_email
            """,
            {"report_id": report_id, "user_email": user_email},
        )

        if not row:
            return {"success": False, "error": "Report not found"}

        return {
            "success": True,
            "report": dict(row),
        }
    except Exception as e:
        log_structured_error(e, page="reports", operation="GET_REPORT_STATUS")
        return {"success": False, "error": str(e)}


# =============================================================================
# UI RENDERING
# =============================================================================
def main() -> None:
    """Main UI"""
    _setup_page()
    _reports_css()

    # require_auth(redirect_to="login", page="reports")
    user_email = get_current_user() or ""
    render_header(full_width=True, current_page="reports")

    st.markdown('<div class="reports-page">', unsafe_allow_html=True)

    # Hero section
    st.markdown(
        """
        <div class="reports-hero">
        <h1>Reports</h1>
        <p>Generate, manage, and download data reports for screening, earnings, estimates, and more.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Tab interface
    tab1, tab2, tab3 = st.tabs(["Create Report", "My Reports", "Report Status"])

    with tab1:
        st.markdown('<div class="reports-card">', unsafe_allow_html=True)
        st.markdown("### Create New Report")

        col1, col2 = st.columns(2)
        with col1:
            report_type = st.selectbox(
                "Report Type",
                [t.value for t in ReportType],
            )
        with col2:
            report_format = st.selectbox(
                "Format",
                ["csv", "excel", "json"],
            )

        schedule_enabled = st.checkbox("Schedule recurring report")
        schedule_val = None
        if schedule_enabled:
            schedule_val = st.text_input(
                "Cron schedule (e.g., '0 0 * * 0' for weekly)",
                placeholder="0 0 * * 0",
            )

        if st.button("Create Report"):
            with st.spinner("Creating report..."):
                result = create_report(
                    report_type=report_type,
                    filters={},
                    format=report_format,
                    schedule=schedule_val,
                )
                if result["success"]:
                    st.success(result["message"])
                else:
                    st.error(result.get("error", "Failed to create report"))
        st.markdown("</div>", unsafe_allow_html=True)

    with tab2:
        st.markdown('<div class="reports-card">', unsafe_allow_html=True)
        st.markdown("### Your Reports")

        with st.spinner("Loading reports..."):
            result = list_reports()
            if result["success"] and result["reports"]:
                df = pd.DataFrame(result["reports"])
                st.dataframe(df, use_container_width=True)
            else:
                st.info("No reports found")
        st.markdown("</div>", unsafe_allow_html=True)

    with tab3:
        st.markdown('<div class="reports-card">', unsafe_allow_html=True)
        st.markdown("### Check Report Status")

        report_id = st.number_input(
            "Report ID",
            min_value=1,
            step=1,
        )

        if st.button("Check Status"):
            result = get_report_status(int(report_id))
            if result["success"]:
                report = result["report"]
                col1, col2, col3 = st.columns(3)
                col1.metric("Status", report["status"])
                col2.metric("Type", report["report_type"])
                col3.metric("Created", report["created_at"])
            else:
                st.error(result.get("error", "Failed to fetch status"))
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
    render_coresight_footer()


if __name__ == "__main__":
    main()
