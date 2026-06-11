"""
Back-compat shim: earnings alerts persist in MySQL (coreiq_*), not SQLite.

Implementation: data/earnings_alert_service.py
"""
from data.earnings_alert_service import (  # noqa: F401
    delivery_exists,
    ensure_tables,
    list_enabled_preferences,
    load_preference,
    save_preference,
    try_insert_delivery,
)
