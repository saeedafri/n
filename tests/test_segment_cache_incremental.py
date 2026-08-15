"""Incremental segment-cache refresh — the rules that keep screening correct.

The screening segment dropdowns and filters read
`coreiq_screening_segment_values_cache`. A refresh that drops a company's rows,
or that advances the high-water mark past filings it failed to process, silently
removes segments from the screener. These tests pin that behaviour with a fake
`db_manager`, so they run without a database.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import pytest  # noqa: E402

import data.screening_service as screening_service  # noqa: E402


class FakeDB:
    """Records every statement so the tests can assert what was written."""

    def __init__(self, read_results=None):
        self.read_results = read_results or {}
        self.deletes = []
        self.inserts = []
        self.updates = []

    def _match(self, sql):
        for key, value in self.read_results.items():
            if key in " ".join(sql.split()):
                return value
        return []

    def execute_query_readonly(self, sql, params=None):
        return self._match(sql)

    def execute_delete(self, sql, params=None):
        self.deletes.append((" ".join(sql.split()), params or {}))
        return 1

    def execute_insert(self, sql, params=None):
        self.inserts.append((" ".join(sql.split()), params or {}))
        return 1

    def execute_update(self, sql, params=None):
        self.updates.append((" ".join(sql.split()), params or {}))
        return 1


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(screening_service, "db_manager", db)
    return db


# ---------------------------------------------------------------------------
# Delta detection
# ---------------------------------------------------------------------------

def test_no_delta_returns_no_tickers(fake_db):
    assert screening_service._tickers_changed_since(100, 100) == []
    assert screening_service._tickers_changed_since(100, 50) == []


def test_delta_query_is_bounded_by_the_id_window(monkeypatch):
    captured = {}

    class DB(FakeDB):
        def execute_query_readonly(self, sql, params=None):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = params
            return [{"ticker": "SIG"}, {"ticker": "TPR"}, {"ticker": "SIG"}]

    monkeypatch.setattr(screening_service, "db_manager", DB())
    got = screening_service._tickers_changed_since(1000, 2000)

    assert got == ["SIG", "TPR"]                      # de-duplicated, sorted
    assert captured["params"] == {"last_id": 1000, "cur_id": 2000}
    # Bounded both sides: an unbounded scan of a 16M-row table is the thing
    # this design exists to avoid.
    assert "id > :last_id" in captured["sql"]
    assert "id <= :cur_id" in captured["sql"]
    assert "doc_type = '10-K'" in captured["sql"]


# ---------------------------------------------------------------------------
# Incremental write
# ---------------------------------------------------------------------------

def test_incremental_touches_only_the_changed_tickers(monkeypatch, fake_db):
    monkeypatch.setattr(screening_service, "ensure_segment_values_cache_table", lambda: True)
    monkeypatch.setattr(screening_service, "ensure_segment_member_cache_table", lambda: True)
    monkeypatch.setattr(screening_service, "_refresh_segment_member_cache", lambda: 7)
    monkeypatch.setattr(
        screening_service, "_segment_entries_for_tickers",
        lambda tickers, **kw: ([("SIG", "business", "Revenues", "Retail", 2024, 10.0)], []),
    )

    stats = screening_service.build_segment_values_cache_for_tickers(["SIG"])

    deleted = [p.get("tk") for sql, p in fake_db.deletes if "DELETE FROM" in sql and "tk" in p]
    assert deleted == ["SIG"], "only the changed ticker's rows may be deleted"
    assert stats["mode"] == "incremental"
    assert stats["tickers"] == 1 and stats["rows"] == 1


def test_timed_out_ticker_keeps_its_existing_rows(monkeypatch, fake_db):
    """A ticker whose scan failed must NOT be deleted-then-left-empty."""
    monkeypatch.setattr(screening_service, "ensure_segment_values_cache_table", lambda: True)
    monkeypatch.setattr(screening_service, "ensure_segment_member_cache_table", lambda: True)
    monkeypatch.setattr(screening_service, "_refresh_segment_member_cache", lambda: 0)
    monkeypatch.setattr(
        screening_service, "_segment_entries_for_tickers",
        # SIG succeeded, PFE timed out
        lambda tickers, **kw: ([("SIG", "business", "Revenues", "Retail", 2024, 10.0)], ["PFE"]),
    )

    stats = screening_service.build_segment_values_cache_for_tickers(["SIG", "PFE"])

    deleted = [p.get("tk") for sql, p in fake_db.deletes if "DELETE FROM" in sql and "tk" in p]
    assert "PFE" not in deleted, "a timed-out ticker's good rows must survive"
    assert deleted == ["SIG"]
    assert stats["skipped"] == ["PFE"]
    assert stats["tickers"] == 1


def test_empty_ticker_list_is_a_noop(fake_db):
    stats = screening_service.build_segment_values_cache_for_tickers([])
    assert stats == {"tickers": 0, "rows": 0, "entries": 0}
    assert fake_db.deletes == [] and fake_db.inserts == []


# ---------------------------------------------------------------------------
# Full-rebuild fallbacks
# ---------------------------------------------------------------------------

def test_full_rebuild_thresholds_are_sane():
    # Past these, a whole-table staging swap beats thousands of per-ticker
    # statements — and is atomic.
    assert screening_service._SEGMENT_INCREMENTAL_MAX_TICKERS >= 1
    assert 0 < screening_service._SEGMENT_INCREMENTAL_MAX_SHARE <= 1


def test_scan_raises_for_full_rebuild_but_not_for_incremental(monkeypatch):
    """The full rebuild republishes the whole table, so a timed-out chunk must
    abort it. The incremental path writes per ticker and must tolerate one."""
    import inspect

    source = inspect.getsource(screening_service._segment_entries_for_tickers)
    assert "raise_on_timeout" in source
    assert "return entries, skipped" in source

    caller = inspect.getsource(screening_service.build_segment_values_cache_for_tickers)
    assert "raise_on_timeout=False" in caller

    full = inspect.getsource(screening_service.build_segment_values_cache)
    assert "raise_on_timeout" not in full  # defaults to True
