import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


class IndexDB:
    def __init__(self):
        self.rows = [
            dict(id=3, ticker="A", year=2027, q=1, quarter="2027Q1", transcript_text="Foreign exchange improved"),
            dict(id=9, ticker="A", year=2026, q=4, quarter="2026Q4", transcript_text="Foreign exchange stabilized"),
            dict(id=8, ticker="B", year=2026, q=4, quarter="2026Q4", transcript_text="Foreign operations; exchange costs"),
            dict(id=100, ticker="A", year=2026, q=3, quarter="2026Q3", transcript_text="Foreign exchange increased"),
        ]
        self.ids = [3, 9, 8, 100]
        self.queries = []

    def execute_query_readonly_raising(self, sql, params):
        self.queries.append((sql, params.copy()))
        if "MATCH(" in sql:
            return [{"id": i} for i in self.ids]
        if "LOCATE(" in sql:
            ids = {value for key, value in params.items() if key.startswith("text_id_")}
            return [r for r in self.rows if r["id"] in ids
                    and params["keyword"].casefold() in r["transcript_text"].casefold()][:params["needed"]]
        ids = set(json.loads(params["candidate_ids"]))
        rows = [r for r in self.rows if r["id"] in ids]
        if "scan_id" in params:
            cursor = (params["scan_year"], params["scan_q"], params["scan_id"])
            rows = [r for r in rows if (r["year"], r["q"], r["id"]) < cursor]
        return [{k: r[k] for k in ("id", "ticker", "year", "q")}
                for r in rows[:params["batch_size"]]]


def helper():
    return importlib.import_module("data.transcript_infix")


def test_candidate_false_positives_are_rechecked_as_literal_phrase():
    db = IndexDB()
    rows = helper().search_page(db, "foreign exchange", limit=2)
    assert [r["id"] for r in rows] == [3, 9]
    body_queries = [params for sql, params in db.queries if "LOCATE(" in sql]
    assert body_queries[0]["needed"] == 2


def test_empty_index_candidates_prove_negative_without_body_scan():
    db = IndexDB()
    db.ids = []
    assert helper().search_page(db, "printenvnovalue") == []
    assert len(db.queries) == 1


def test_index_cursor_obeys_date_order_when_ids_are_not_chronological():
    db = IndexDB()
    rows = helper().search_page(db, "foreign exchange", limit=2, after=(2026, 4, 9), batch_size=1)
    assert [r["id"] for r in rows] == [100]
    ids = {value for sql, params in db.queries for key, value in params.items() if key.startswith("text_id_")}
    assert not ({3, 9} & ids)


@pytest.mark.parametrize("term", ["R&D", "a-b", "%", "_", "a"])
def test_unanchorable_terms_request_exact_fallback(term):
    db = IndexDB()
    assert helper().search_page(db, term) is None
    assert db.queries == []


@pytest.mark.parametrize("term", ["café", "Straße", "e\u0301té", "ＡＩ", "été"])
def test_unverified_unicode_queries_request_exact_fallback(term):
    db = IndexDB()
    assert helper().search_page(db, term) is None
    assert db.queries == []


def test_body_queries_are_forced_to_the_bounded_primary_key_set():
    db = IndexDB()
    helper().search_page(db, "foreign exchange", limit=2)
    body_sql = next(sql for sql, _ in db.queries if "LOCATE(" in sql)
    assert "FORCE INDEX (PRIMARY)" in body_sql


def test_candidate_phrases_preserve_contiguous_runs_for_selective_search():
    candidate = helper().candidate_query
    assert candidate("printenv") == '+"printenv"'
    assert candidate("foreign exchange") == '+"foreign" +"exchange"'
    assert candidate("e-commerce") == '+"commerce"'
    assert candidate("abc-abc def") == '+"abc" +"def"'


def test_ascii_literal_predicate_preserves_python_dotted_i_lowercase():
    db = IndexDB()
    helper().search_page(db, "foreign exchange", limit=2)
    body_sql = next(sql for sql, _ in db.queries if "LOCATE(" in sql)
    assert "CAST(LOWER(:keyword) AS BINARY)" in body_sql
    assert "LOWER(REPLACE(transcript_text" in body_sql
    assert "0xC4B0" in body_sql and "0x69CC87" in body_sql


def test_index_query_error_cannot_become_no_matches():
    db = IndexDB()
    def failed(sql, params):
        raise TimeoutError("query deadline")
    db.execute_query_readonly_raising = failed
    with pytest.raises(helper().InfixSearchError):
        helper().search_page(db, "foreign exchange")


def test_search_has_no_response_cache():
    db = IndexDB()
    assert helper().search_page(db, "foreign exchange", limit=1)[0]["id"] == 3
    db.rows.pop(0)
    assert helper().search_page(db, "foreign exchange", limit=1)[0]["id"] == 9
    assert sum("MATCH(" in sql for sql, _ in db.queries) == 2


def test_enabled_index_path_is_preferred_before_application_mirror(monkeypatch):
    from data import repository as repo
    row = dict(id=3, ticker="A", year=2027, q=1, quarter="2027Q1", transcript_text="Foreign exchange")
    monkeypatch.setattr(repo, "_NATIVE_TRANSCRIPT_NGRAM_SEARCH", True, raising=False)
    monkeypatch.setattr(helper(), "search_page", lambda *args, **kwargs: [row])
    def forbidden(*args, **kwargs):
        raise AssertionError("Application mirror must not serve the enabled native-index route")
    monkeypatch.setattr(repo.EarningsCallRepository, "_like_page_from_word_index", forbidden)
    repo.EarningsCallRepository.search_transcripts_fulltext.clear()
    assert repo.EarningsCallRepository.search_transcripts_fulltext("foreign exchange") == [row]


def test_public_index_failure_is_a_retryable_search_error(monkeypatch):
    from data import repository as repo
    monkeypatch.setattr(repo, "_NATIVE_TRANSCRIPT_NGRAM_SEARCH", True, raising=False)
    def failed(*args, **kwargs):
        raise helper().InfixSearchError("query deadline")
    monkeypatch.setattr(helper(), "search_page", failed)
    monkeypatch.setattr(repo.EarningsCallRepository, "_like_page_from_word_index", failed)
    repo.EarningsCallRepository.search_transcripts_fulltext.clear()
    with pytest.raises(repo.TranscriptSearchError):
        repo.EarningsCallRepository.search_transcripts_fulltext("foreign exchange")
