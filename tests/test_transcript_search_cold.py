import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from data import repository as repo


class NativeDB:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.transported = []
        self.payloads = []

    def execute_query_readonly_raising(self, sql, params):
        self.queries.append((sql, params.copy()))
        if "LOCATE(" in sql or "EC_TRANSCRIPT_SEARCH_TEXT" in sql:
            ids = {value for key, value in params.items() if key.startswith("text_id_")}
            matching = [r for r in self.rows if r["id"] in ids]
            if "LOCATE(" in sql and "CASE WHEN" not in sql:
                matching = [r for r in matching
                            if params["keyword"].lower() in r["transcript_text"].lower()]
            if "LIMIT :text_limit" in sql:
                assert "ORDER BY year DESC, q DESC, id DESC" in sql
                matching = sorted(matching, key=lambda r: (r["year"], r["q"], r["id"]), reverse=True)
                matching = matching[:params["text_limit"]]
            self.transported.extend(r["id"] for r in matching)
            payloads = []
            for row in reversed(matching):
                text = row["transcript_text"]
                if "CASE WHEN" in sql:
                    text = (None if text is None or not text else
                            text if params["keyword"].lower() in text.lower() else "")
                payloads.append(dict(id=row["id"], quarter=row["quarter"], transcript_text=text))
            self.payloads.extend(payloads)
            return payloads
        rows = sorted(self.rows, key=lambda r: (r["year"], r["q"], r["id"]), reverse=True)
        if "scan_id" in params:
            rows = [r for r in rows if (r["year"], r["q"], r["id"]) <
                    (params["scan_year"], params["scan_q"], params["scan_id"])]
        return [{k: r[k] for k in ("id", "ticker", "year", "q")}
                for r in rows[:params["batch_size"]]]


@pytest.fixture
def native(monkeypatch):
    rows = [dict(id=i, ticker="M", year=2026, q=1, quarter="2026Q1",
                 transcript_text="exchange" if i in (5, 4, 2, 1) else "different")
            for i in range(6, 0, -1)]
    db = NativeDB(rows)
    monkeypatch.setattr(repo, "db_manager", db)
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_BATCH_SIZE", 2, raising=False)
    return db


def test_native_search_paginates_matching_rows_across_metadata_batches(native):
    rows = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 1)
    assert [r["id"] for r in rows] == [4, 2]
    metadata = [q for q, _ in native.queries if "idx_ec_list_ordered" in q]
    assert all("idx_ec_list_ordered" in q and "transcript_text" not in q for q in metadata)
    assert all("MAX_EXECUTION_TIME(" in q for q, _ in native.queries)


def test_native_no_matches_requires_exhausting_candidates(native):
    assert repo.EarningsCallRepository._search_transcripts_mysql("absent", (), None, None, 2, 0) == []
    metadata = [p for q, p in native.queries if "idx_ec_list_ordered" in q]
    assert [p.get("scan_id") for p in metadata] == [None, 5, 3, 1]


def test_native_continuation_skips_previous_page_candidates(native):
    first = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
    assert [r["id"] for r in first] == [5, 4]
    native.queries.clear()
    second = repo.EarningsCallRepository._search_transcripts_mysql(
        "exchange", (), None, None, 2, 0, after=(2026, 1, 4))
    assert [r["id"] for r in second] == [2, 1]
    assert native.queries[0][1]["scan_id"] == 4
    text_ids = {value for sql, params in native.queries for key, value in params.items()
                if key.startswith("text_id_")}
    assert not ({5, 4} & text_ids)


def test_native_conditional_body_fetch_is_bounded_and_preserves_metadata_order(native, monkeypatch):
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_BATCH_SIZE", 64)
    first = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
    assert [r["id"] for r in first] == [5, 4]
    assert native.transported == [6, 5, 4, 3, 2, 1]
    text_sql, params = next((sql, params) for sql, params in native.queries
                            if "EC_TRANSCRIPT_SEARCH_TEXT" in sql)
    assert "FORCE INDEX (PRIMARY)" in text_sql
    assert params["body_limit"] <= 16


def test_native_ascii_match_returns_conditional_bodies_without_sql_sort(native):
    rows = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
    assert [row["id"] for row in rows] == [5, 4]
    sql, params = next((sql, params) for sql, params in native.queries
                       if "EC_TRANSCRIPT_SEARCH_TEXT" in sql)
    assert "CASE WHEN transcript_text IS NULL OR LENGTH(transcript_text) = 0 THEN NULL" in sql
    assert "WHEN LOCATE(CAST(LOWER(:keyword) AS BINARY)" in sql
    assert "ELSE '' END AS transcript_text" in sql and "ORDER BY" not in sql
    assert "LIMIT :body_limit" in sql and params["body_limit"] <= 16


def test_native_nonmatching_bodies_are_not_transferred(native, monkeypatch):
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_BATCH_SIZE", 64)
    found = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
    assert [row["id"] for row in found] == [5, 4]
    assert {row["id"] for row in native.payloads} == {6, 5, 4, 3, 2, 1}
    assert all(row["transcript_text"] == "" for row in native.payloads if row["id"] in (6, 3))
    assert all(row["transcript_text"] == "exchange" for row in native.payloads if row["id"] in (5, 4, 2, 1))


def test_native_non_ascii_query_retains_legacy_collation_predicate(native):
    native.rows[0]["transcript_text"] = "CAFÉ"
    rows = repo.EarningsCallRepository._search_transcripts_mysql("café", (), None, None, 1, 0)
    assert [row["id"] for row in rows] == [6]
    sql = next(sql for sql, _ in native.queries if "LOCATE(" in sql)
    assert "LOCATE(:keyword, transcript_text) > 0" in sql
    assert "AS BINARY" not in sql


def test_native_sparse_matches_fetch_chunks_once_and_stop_after_enough(monkeypatch):
    rows = [dict(id=i, ticker="M", year=2026, q=1, quarter="2026Q1",
                 transcript_text="exchange" if i in (39, 20, 3) else "different")
            for i in range(40, 0, -1)]
    db = NativeDB(rows)
    monkeypatch.setattr(repo, "db_manager", db)
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_BATCH_SIZE", 64)
    found = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
    assert [row["id"] for row in found] == [39, 20]
    bodies = [params for sql, params in db.queries if "EC_TRANSCRIPT_SEARCH_TEXT" in sql]
    assert [p["body_limit"] for p in bodies] == [16, 16]
    assert len(db.transported) == 32 and len(set(db.transported)) == 32
    assert not set(range(1, 9)) & set(db.transported)
    assert len([sql for sql, _ in db.queries if "idx_ec_list_ordered" in sql]) == 1


def test_native_dense_page_never_fetches_a_second_body_chunk(monkeypatch):
    db = NativeDB([dict(id=i, ticker="M", year=2026, q=1, quarter="2026Q1", transcript_text="exchange")
                   for i in range(40, 0, -1)])
    monkeypatch.setattr(repo, "db_manager", db)
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_BATCH_SIZE", 64)
    found = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 15, 0)
    assert [row["id"] for row in found] == list(range(40, 25, -1))
    assert len(db.transported) == 16
    assert len([sql for sql, _ in db.queries if "EC_TRANSCRIPT_SEARCH_TEXT" in sql]) == 1


def test_native_plain_body_cursor_uses_date_order_when_ids_are_not_chronological(monkeypatch):
    db = NativeDB([dict(id=i, ticker="M", year=year, q=q, quarter=f"{year}Q{q}", transcript_text="exchange")
                   for i, year, q in ((2, 2027, 2), (9, 2027, 1), (100, 2026, 4), (1, 2026, 3))])
    monkeypatch.setattr(repo, "db_manager", db)
    first = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
    assert [row["id"] for row in first] == [2, 9]
    db.queries.clear()
    db.transported.clear()
    second = repo.EarningsCallRepository._search_transcripts_mysql(
        "exchange", (), None, None, 2, 0, after=(2027, 1, 9))
    assert [row["id"] for row in second] == [100, 1]
    assert not {2, 9} & set(db.transported)


@pytest.mark.parametrize("keyword,expected", [
    ("strasse", [5]), ("ae", [5]), ("cafe", [5]), ("ix", [5]), ("kelvin", [4]),
])
def test_native_ascii_filter_uses_raw_python_lowercase_truth(native, keyword, expected):
    native.rows = [dict(id=i, ticker="M", year=2026, q=1, quarter="2026Q1", transcript_text=text)
                   for i, text in ((6, "Straße æ café İX"), (5, "STRASSE AE CAFE IX"), (4, "Kelvin"))]
    found = repo.EarningsCallRepository._search_transcripts_mysql(keyword, (), None, None, 15, 0)
    assert [row["id"] for row in found] == expected


@pytest.mark.parametrize("missing", ["row", "text", "empty"])
def test_native_incomplete_conditional_body_chunk_is_a_failure(native, monkeypatch, missing):
    if missing == "empty":
        native.rows[0]["transcript_text"] = ""
    execute = native.execute_query_readonly_raising
    def incomplete(sql, params):
        rows = execute(sql, params)
        if "EC_TRANSCRIPT_SEARCH_TEXT" in sql and rows:
            if missing == "row":
                rows.pop()
            elif missing == "text":
                rows[0]["transcript_text"] = None
        return rows
    monkeypatch.setattr(native, "execute_query_readonly_raising", incomplete)
    with pytest.raises(repo.TranscriptSearchError):
        repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)


def test_native_deadline_between_body_chunks_never_returns_partial_page(monkeypatch):
    db = NativeDB([dict(id=i, ticker="M", year=2026, q=1, quarter="2026Q1",
                       transcript_text="exchange" if i == 40 else "different")
                   for i in range(40, 0, -1)])
    monkeypatch.setattr(repo, "db_manager", db)
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_BATCH_SIZE", 64)
    ticks = iter((0.0, 0.0, 0.0, 21.0))
    monkeypatch.setattr(repo, "time", SimpleNamespace(monotonic=lambda: next(ticks), perf_counter=time.perf_counter))
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_DEADLINE_SECONDS", 20)
    with pytest.raises(repo.TranscriptSearchError):
        repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
    assert len([sql for sql, _ in db.queries if "EC_TRANSCRIPT_SEARCH_TEXT" in sql]) == 1


def test_native_scope_and_period_filters_reach_covering_query(native):
    repo.EarningsCallRepository._search_transcripts_mysql("exchange", ("M", "WMT"), 2026, 1, 1, 0)
    sql, params = native.queries[0]
    assert "ticker IN" in sql and "year = :year" in sql and "q = :q" in sql
    assert params["year"] == 2026 and params["q"] == 1
    assert params["scope_ticker_0"] == "M" and params["scope_ticker_1"] == "WMT"


def test_native_search_has_no_500_match_cutoff(monkeypatch):
    db = NativeDB([dict(id=i, ticker="M", year=2026, q=1, quarter="2026Q1", transcript_text="exchange")
                   for i in range(520, 0, -1)])
    monkeypatch.setattr(repo, "db_manager", db)
    rows = repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 15, 505)
    assert [r["id"] for r in rows] == list(range(15, 0, -1))


def test_native_query_failure_remains_a_search_failure(native, monkeypatch):
    def failed(sql, params):
        raise TimeoutError("query deadline")
    monkeypatch.setattr(native, "execute_query_readonly_raising", failed)
    with pytest.raises(repo.TranscriptSearchError):
        repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)


def test_native_overall_deadline_never_returns_partial_results(native, monkeypatch):
    ticks = iter((0.0, 10.0))
    monkeypatch.setattr(repo, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(repo, "_MYSQL_TRANSCRIPT_DEADLINE_SECONDS", 1, raising=False)
    with pytest.raises(repo.TranscriptSearchError):
        repo.EarningsCallRepository._search_transcripts_mysql("exchange", (), None, None, 2, 0)
