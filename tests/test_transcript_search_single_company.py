"""A one-company transcript search must not use FULLTEXT: MySQL scores every
matching transcript in the table before the ticker filter (110s for a common
word). All-company searches keep FULLTEXT, which stops at the candidate cap."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import data.repository as repo  # noqa: E402


class FakeDB:
    def __init__(self):
        self.queries = []

    def execute_query_readonly(self, sql, params=None):
        self.queries.append(sql)
        return []


def first_query(monkeypatch, **kw):
    db = FakeDB()
    monkeypatch.setattr(repo, "db_manager", db)
    repo.EarningsCallRepository.search_transcripts_fulltext("inventory", **kw)
    return db.queries


def test_one_company_uses_ticker_and_like(monkeypatch):
    queries = first_query(monkeypatch, ticker="M", year="ALL", quarter="Q2")
    assert len(queries) == 1
    assert "MATCH(" not in queries[0]
    assert "LIKE :kw" in queries[0] and "ticker = :ticker" in queries[0]


def test_all_companies_keep_fulltext(monkeypatch):
    queries = first_query(monkeypatch, ticker="ALL")
    assert "MATCH(transcript_text)" in queries[0]
    assert "LIKE" in queries[1]
