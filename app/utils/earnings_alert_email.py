"""
Confirmation email when users save earnings calendar alert preferences.
Uses the same SMTP env vars as other app mailers (see pages/logs.py).
"""
from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from typing import List

from utils.server_logger import log_info, log_structured_error


def send_earnings_alert_preferences_confirmation(
    *,
    to_email: str,
    enabled: bool,
    body_lines: List[str],
) -> bool:
    """
    Send a plain-text confirmation to the user. Returns True if SMTP accepted the message.
    No-op (returns False) if EMAIL_PASSWORD is not set.
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return False

    smtp_host = os.getenv("SMTP_SERVER", "smtp.office365.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    from_addr = os.getenv("FROM_EMAIL", "dataautomation@coresight.com")
    password = os.getenv("EMAIL_PASSWORD", "")
    if not password:
        log_info(
            "[earnings_alert_email] Skipping confirmation email: EMAIL_PASSWORD not set"
        )
        return False

    if enabled:
        subject = "Earnings reminders — preferences saved"
    else:
        subject = "Earnings reminders — turned off"

    header = (
        "Coresight Research — Earnings calendar\n"
        "Your alert preferences were updated.\n"
    )
    body = header + "\n" + "\n".join(body_lines) + "\n\n— Automated message"

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(from_addr, password)
            server.send_message(msg)
        return True
    except Exception as e:
        log_structured_error(
            e,
            page="earnings_alert_email",
            component="send_earnings_alert_preferences_confirmation",
            operation="smtp_send",
            context=f"to={to_email!r}",
        )
        return False


def send_earnings_reminder_digest(
    *,
    to_email: str,
    body_lines: List[str],
) -> bool:
    """
    Plain-text digest of upcoming earnings reminders. Returns True if SMTP accepted.
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return False

    smtp_host = os.getenv("SMTP_SERVER", "smtp.office365.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    from_addr = os.getenv("FROM_EMAIL", "dataautomation@coresight.com")
    password = os.getenv("EMAIL_PASSWORD", "")
    if not password:
        log_info(
            "[earnings_alert_email] Skipping reminder email: EMAIL_PASSWORD not set"
        )
        return False

    subject = "Earnings calendar — reminder"
    body = (
        "Coresight Research — Earnings calendar reminders\n\n"
        + "\n".join(body_lines)
        + "\n"
    )

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(from_addr, password)
            server.send_message(msg)
        return True
    except Exception as e:
        log_structured_error(
            e,
            page="earnings_alert_email",
            component="send_earnings_reminder_digest",
            operation="smtp_send",
            context=f"to={to_email!r}",
        )
        return False
