"""Native transcript search selects covering metadata before reading transcript bodies."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import data.repository as repo  # noqa: E402
from data import news_mirror  # noqa: E402


class FakeDB:
    def __init__(self):
        self.queries = []

    def execute_query_readonly(self, sql, params=None):
        self.queries.append(sql)
        return []

    execute_query_readonly_raising = execute_query_readonly


def first_query(monkeypatch, **kw):
    db = FakeDB()
    monkeypatch.setattr(repo, "db_manager", db)
    monkeypatch.setattr(repo, "_LOCAL_TRANSCRIPT_SEARCH", False)
    monkeypatch.setattr(news_mirror, "transcript_like_ids", lambda keyword: None)
    repo.EarningsCallRepository.search_transcripts_fulltext.clear()
    repo.EarningsCallRepository.search_transcripts_fulltext("inventory", **kw)
    return db.queries


def test_one_company_uses_scoped_covering_metadata(monkeypatch):
    queries = first_query(monkeypatch, ticker="M", year="ALL", quarter="Q2")
    assert len(queries) == 1
    assert "MATCH(" not in queries[0]
    assert "idx_ec_list_ordered" in queries[0]
    assert "ticker IN (:scope_ticker_0)" in queries[0]
    assert "q = :q" in queries[0]


def test_all_companies_use_covering_metadata_without_fulltext_scoring(monkeypatch):
    queries = first_query(monkeypatch, ticker="ALL")
    assert len(queries) == 1
    assert "idx_ec_list_ordered" in queries[0]
    assert "MATCH(" not in queries[0]
