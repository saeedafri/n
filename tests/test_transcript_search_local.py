import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from data import news_mirror as nm
from data import repository as repo


@pytest.fixture
def mirror(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    nm._schema(conn)
    nm._insert(conn, "tx", [
        dict(id=i, ticker="M" if i % 2 == 0 else "WMT", year=2020 if i <= 500 else 2026,
             quarter="Q1", q=1, has_transcript=1, transcript_text="Inventory and exchanges")
        for i in range(1, 521)
    ] + [dict(id=600, ticker="M", year=2026, quarter="Q1", q=1,
              has_transcript=0, transcript_text="Inventory")])
    conn.commit()
    monkeypatch.setattr(nm, "_conn", lambda: conn)
    monkeypatch.setattr(nm, "ready", lambda key: True)
    monkeypatch.setattr(nm, "_VOCAB", {"terms": None})
    monkeypatch.setattr(nm, "_LIKE_IDS", {})
    repo.EarningsCallRepository.search_transcripts_fulltext.clear()
    yield conn
    repo.EarningsCallRepository.search_transcripts_fulltext.clear()
    conn.close()


def test_word_lookup_filters_without_reading_wide_transcript_rows(mirror):
    queries = []
    mirror.set_trace_callback(queries.append)
    assert set(nm.transcript_like_ids("invent")) == set(range(1, 521))
    sql = next(q for q in queries if "WHERE tx_words MATCH" in q)
    plan = [r[3] for r in mirror.execute("EXPLAIN QUERY PLAN " + sql)]
    assert any("COVERING INDEX" in step for step in plan), plan
    assert not any("INTEGER PRIMARY KEY" in step for step in plan), plan


def test_newest_candidates_are_kept_before_pagination(mirror):
    page = repo.EarningsCallRepository._like_page_from_word_index("inventory", (), None, None, 3, 0)
    assert [r["id"] for r in page] == [520, 519, 518]
    tail = repo.EarningsCallRepository._like_page_from_word_index("inventory", (), None, None, 10, 498)
    assert [r["id"] for r in tail] == [22, 21]


def test_metadata_page_uses_covering_index(mirror):
    queries = []
    mirror.set_trace_callback(queries.append)
    rows = repo.EarningsCallRepository._like_page_from_word_index("inventory", (), 2026, 1, 3, 0)
    assert [r["id"] for r in rows] == [520, 519, 518]
    sql = next(q for q in queries if "json_each" in q)
    plan = [r[3] for r in mirror.execute("EXPLAIN QUERY PLAN " + sql)]
    assert any("COVERING INDEX" in step for step in plan), plan


def test_older_mirror_without_metadata_index_still_returns_page(mirror):
    mirror.execute("DROP INDEX IF EXISTS ix_tx_search_metadata")
    rows = repo.EarningsCallRepository._like_page_from_word_index("inventory", (), 2026, 1, 3, 0)
    assert [r["id"] for r in rows] == [520, 519, 518]


def test_local_continuation_can_advance_beyond_first_500_candidates(mirror):
    rows = repo.EarningsCallRepository._like_page_from_word_index(
        "inventory", (), None, None, 3, 0, after=(2020, 1, 21))
    assert [r["id"] for r in rows] == [20, 19, 18]


def test_public_search_passes_continuation_to_local_page(mirror):
    rows = repo.EarningsCallRepository.search_transcripts_fulltext(
        "inventory", limit=3, after=(2026, 1, 518))
    assert [r["id"] for r in rows] == [517, 516, 515]


@pytest.mark.parametrize("kwargs,want", [
    ({"ticker": "M"}, [520, 518, 516]),
    ({"ticker": "ALL", "allowed_tickers": ("WMT",)}, [519, 517, 515]),
    ({"ticker": "M", "allowed_tickers": ("WMT",)}, []),
    ({"ticker": "M", "year": 2020, "quarter": "Q1"}, [500, 498, 496]),
])
def test_search_keeps_company_and_period_filters(mirror, kwargs, want):
    rows = repo.EarningsCallRepository.search_transcripts_fulltext("inventory", limit=3, **kwargs)
    assert [r["id"] for r in rows] == want


def test_primary_mysql_search_has_execution_deadline(monkeypatch):
    queries = []
    class DB:
        def execute_query_readonly(self, sql, params):
            queries.append(sql)
            return []

        execute_query_readonly_raising = execute_query_readonly

    monkeypatch.setattr(repo, "db_manager", DB())
    monkeypatch.setattr(nm, "ready", lambda key: False)
    repo.EarningsCallRepository.search_transcripts_fulltext.clear()
    repo.EarningsCallRepository.search_transcripts_fulltext("supply chain", ticker="ALL")
    assert "MAX_EXECUTION_TIME(" in queries[0]


def test_mysql_timeout_is_not_cached_as_no_matches(monkeypatch):
    calls = []
    class DB:
        def execute_query_readonly(self, sql, params):
            return []

        def execute_query_readonly_raising(self, sql, params):
            calls.append(sql)
            raise TimeoutError("query deadline")

    monkeypatch.setattr(repo, "db_manager", DB())
    monkeypatch.setattr(nm, "ready", lambda key: False)
    repo.EarningsCallRepository.search_transcripts_fulltext.clear()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Transcript search is temporarily unavailable") as caught:
            repo.EarningsCallRepository.search_transcripts_fulltext("supply chain", ticker="ALL")
        assert isinstance(caught.value.__cause__, TimeoutError)
    assert len(calls) == 2


def test_missing_transcript_text_is_not_cached_as_complete_results(mirror, monkeypatch):
    monkeypatch.setattr(repo, "_news_run", lambda sql, params, key: [])
    with pytest.raises(RuntimeError, match="Transcript search is temporarily unavailable"):
        repo.EarningsCallRepository.search_transcripts_fulltext("inventory", ticker="ALL", limit=3)


def test_null_transcript_text_is_not_cached_as_complete_results(mirror, monkeypatch):
    monkeypatch.setattr(repo, "_news_run", lambda sql, params, key: [{"id": 520, "transcript_text": None}])
    with pytest.raises(RuntimeError, match="Transcript search is temporarily unavailable"):
        repo.EarningsCallRepository.search_transcripts_fulltext("inventory", ticker="ALL", limit=1)
