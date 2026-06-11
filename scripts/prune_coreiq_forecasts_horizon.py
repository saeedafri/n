#!/usr/bin/env python3
"""Delete coreiq_model_forecasts rows beyond the default 5-year horizon (periods_ahead > 5)."""
import os
import sys

# Run from repo root: PYTHONPATH=app python3 scripts/prune_coreiq_forecasts_horizon.py
_APP = os.path.join(os.path.dirname(__file__), "..", "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from data.forecast_store import delete_forecasts_beyond_horizon  # noqa: E402

if __name__ == "__main__":
    n = delete_forecasts_beyond_horizon(5)
    print(f"Deleted rows (periods_ahead > 5): {n}")
