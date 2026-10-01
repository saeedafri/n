"""backfill_company_info runs once per process and never writes when the store is read-only.

It used to gate its "done" flag on updated == 0, but the UPDATE's rowcount counts
matched rows, so tickers with no better exchange value re-matched on every call:
400 identical UPDATEs (3.6-4.6s) on every /forecasting render, even from a
laptop with FORECAST_STORE_READONLY=1.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import data.forecast_refresh_service as svc  # noqa: E402


class FakeDB:
    def __init__(self):
        self.updates = 0
        self.reads = 0

    def fetch_one(self, sql, params):
        self.reads += 1
        return {"ticker": "SIG"}

    def execute_query_readonly(self, sql, params):
        self.reads += 1
        if "FROM coreiq_companies" in sql:
            return [{"ticker": "SIG", "exchange_acronym": "", "name_coresight": "Signet", "exchange": ""}]
        return [{"ticker": "SIG"}]

    def execute_update(self, sql, params):
        self.updates += 1
        return 50  # matched rows, identical values


def test_backfill_runs_once_even_when_rows_keep_matching(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(svc, "db_manager", db)
    monkeypatch.setattr(svc, "_BACKFILL_DONE", False)
    monkeypatch.delenv("FORECAST_STORE_READONLY", raising=False)
    svc.backfill_company_info()
    svc.backfill_company_info()
    svc.backfill_company_info()
    assert db.updates == 1


def test_backfill_never_writes_when_read_only(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(svc, "db_manager", db)
    monkeypatch.setattr(svc, "_BACKFILL_DONE", False)
    monkeypatch.setenv("FORECAST_STORE_READONLY", "1")
    svc.backfill_company_info()
    assert db.updates == 0 and db.reads == 0
