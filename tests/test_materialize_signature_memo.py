"""Materialized-cache reads reuse a recent freshness signature instead of
querying the DB on every read (the screening universe is read every rerun)."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import utils.materialize as mat  # noqa: E402


class CountingDB:
    def __init__(self):
        self.calls = 0

    def execute_query_readonly(self, sql, params):
        self.calls += 1
        return [{"t": "coreiq_companies", "sig": None, "c": 558}]


SOURCES = [{"table": "coreiq_companies", "signal": None}]


def test_reads_within_ttl_share_one_query(monkeypatch):
    db = CountingDB()
    monkeypatch.setattr(mat, "db_manager", db)
    monkeypatch.setattr(mat, "_SIG_MEMO", {})
    first = mat._live_signature(SOURCES)
    assert mat._live_signature(SOURCES) == first
    assert mat._live_signature(SOURCES) == first
    assert db.calls == 1


def test_expired_or_fresh_signature_queries_again(monkeypatch):
    db = CountingDB()
    monkeypatch.setattr(mat, "db_manager", db)
    monkeypatch.setattr(mat, "_SIG_MEMO", {})
    mat._live_signature(SOURCES)
    mat._live_signature(SOURCES, fresh=True)
    assert db.calls == 2
    monkeypatch.setattr(mat, "_SIG_TTL_S", 0)
    mat._live_signature(SOURCES)
    assert db.calls == 3
