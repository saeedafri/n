"""
Single source of truth: OIDC vs JWT-only (production).

Production: any of ENV, ENVIRONMENT, or APP_ENV equals ``production``
(case-insensitive) → JWT login/logout (same idea as market-prod).

Anything else → OIDC (temporary until OIDC is validated for prod).
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def is_production_deploy() -> bool:
    """True when this process should behave like production (JWT auth only)."""
    return (
        os.getenv("ENV", "").strip().lower() == "production"
        or os.getenv("ENVIRONMENT", "").strip().lower() == "production"
        or os.getenv("APP_ENV", "").strip().lower() == "production"
    )


def is_oidc_enabled() -> bool:
    """OIDC login/logout for all environments."""
    return True  # JWT disabled; OIDC used for both staging and production


# Snapshot at import (Streamlit workers load .env before pages import this module).
IS_OIDC_ENV: bool = is_oidc_enabled()
