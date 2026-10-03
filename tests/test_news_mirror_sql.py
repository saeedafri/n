"""The repository's MySQL news SQL runs unchanged on the local copy (data/news_mirror.py)
and gives the answers MySQL would: accent/case-insensitive title match, sector join,
YF per-story ticker merge, date bounds."""

import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import data.news_mirror as nm  # noqa: E402


def local_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    nm._schema(c)
    av = [
        dict(id=1, time_published_utc=datetime(2026, 9, 1, 10), ticker="M", title="Café chain lifts outlook",
             summary="s", url="u1", source_name="x", source_domain="d", ticker_sentiment_json="[]", topics_json=""),
        dict(id=2, time_published_utc=datetime(2026, 9, 2, 9), ticker="WMT", title="Tariff talk weighs on retail",
             summary="s", url="u2", source_name="x", source_domain="d", ticker_sentiment_json="[]", topics_json=""),
        dict(id=3, time_published_utc=datetime(2026, 9, 3, 23, 59, 59), ticker="M", title="AT&T and 50% gains",
             summary="s", url="u3", source_name="x", source_domain="d", ticker_sentiment_json="[]", topics_json=""),
        dict(id=4, time_published_utc=datetime(2026, 9, 4, 0, 0, 0), ticker="ZZ", title="TARIFFS again",
             summary="s", url="u4", source_name="x", source_domain="d", ticker_sentiment_json="[]", topics_json=""),
    ]
    nm._insert(c, "av", av)
    yf = [dict(id=10, news_id="n1", published_at=datetime(2026, 9, 2, 8), ticker="M", title="Retail tariffs",
               publisher="p", link="l", primary_topic_v1="a", primary_topic_v2=None),
          dict(id=11, news_id="n1", published_at=datetime(2026, 9, 2, 8), ticker="WMT", title="Retail tariffs",
               publisher="p", link="l", primary_topic_v1="a", primary_topic_v2=None),
          dict(id=12, news_id="n2", published_at=datetime(2026, 9, 3, 8), ticker="TGT", title="Other",
               publisher="p", link="l", primary_topic_v1="b", primary_topic_v2=None)]
    nm._insert(c, "yf", yf)
    c.executemany("INSERT INTO coreiq_av_companies_all (symbol, sector) VALUES (?, ?)",
                  [("M", "Consumer Cyclical"), ("WMT", "consumer cyclical"), ("ZZ", None)])
    c.commit()
    monkeypatch.setattr(nm, "_conn", lambda: c)
    return c


def test_title_like_is_case_and_accent_insensitive(monkeypatch):
    local_db(monkeypatch)
    rows = nm.query("SELECT /*+ MAX_EXECUTION_TIME(10000) */ id FROM coreiq_av_market_news_sentiment "
                    "WHERE time_published_utc >= :d1 AND time_published_utc < :d2 AND title LIKE :w0 "
                    "ORDER BY time_published_utc DESC LIMIT :lim",
                    {"d1": date(2026, 9, 1), "d2": date(2026, 9, 5), "w0": "%cafe%", "lim": 10})
    assert [r["id"] for r in rows] == [1]
    rows = nm.query("SELECT id FROM coreiq_av_market_news_sentiment WHERE title LIKE :w0 ORDER BY id",
                    {"w0": "%tariff%"})
    assert [r["id"] for r in rows] == [2, 4]


def test_percent_and_ampersand_behave_like_mysql_like(monkeypatch):
    local_db(monkeypatch)
    q = "SELECT id FROM coreiq_av_market_news_sentiment WHERE title LIKE :w0"
    assert [r["id"] for r in nm.query(q, {"w0": "%AT&T%"})] == [3]
    assert [r["id"] for r in nm.query(q, {"w0": "%50%%"})] == [3]      # user '%' is a wildcard in MySQL too


def test_date_bounds_include_start_day_and_exclude_next_day(monkeypatch):
    local_db(monkeypatch)
    rows = nm.query("SELECT n.id FROM coreiq_av_market_news_sentiment n USE INDEX (idx_av_time_id) "
                    "WHERE n.time_published_utc >= :date_from AND n.time_published_utc < :date_to_excl",
                    {"date_from": date(2026, 9, 3), "date_to_excl": date(2026, 9, 4)})
    assert [r["id"] for r in rows] == [3]       # 23:59:59 on the 3rd in, 00:00 on the 4th out


def test_fulltext_becomes_substring_on_every_word(monkeypatch):
    local_db(monkeypatch)
    rows = nm.query("SELECT n.id FROM coreiq_av_market_news_sentiment n "
                    "WHERE MATCH(n.title) AGAINST(:av_ft_kw IN BOOLEAN MODE) ORDER BY n.id",
                    {"av_ft_kw": "+tariff* +retail*"})
    assert [r["id"] for r in rows] == [2]


def test_sector_join_and_not_exists(monkeypatch):
    local_db(monkeypatch)
    join = ("INNER JOIN (  SELECT DISTINCT symbol FROM coreiq_av_companies_all"
            "  WHERE UPPER(sector) = UPPER(:sector)) av ON av.symbol = n.ticker")
    rows = nm.query(f"SELECT n.id FROM coreiq_av_market_news_sentiment n {join} ORDER BY n.id",
                    {"sector": "Consumer Cyclical"})
    assert [r["id"] for r in rows] == [1, 2, 3]
    rows = nm.query("SELECT n.id FROM coreiq_av_market_news_sentiment n WHERE 1=1 AND NOT EXISTS ("
                    "SELECT 1 FROM coreiq_av_companies_all av WHERE av.symbol = n.ticker"
                    " AND av.sector IS NOT NULL AND av.sector != '' AND UPPER(av.sector) != 'NONE') ORDER BY n.id", {})
    assert [r["id"] for r in rows] == [4]


def test_yf_group_concat_merges_tickers_per_story(monkeypatch):
    local_db(monkeypatch)
    rows = nm.query("""
        SELECT y.id, d.news_id, d.published_at, d.tickers, y.title
        FROM coreiq_yf_market_news_sentiment y
        INNER JOIN (
            SELECT news_id, MAX(published_at) AS published_at, MIN(id) AS min_id,
                   GROUP_CONCAT(DISTINCT ticker SEPARATOR ';;') AS tickers
            FROM coreiq_yf_market_news_sentiment USE INDEX (idx_yf_pub_nid_ticker)
            WHERE published_at >= :date_from AND published_at < :date_to_excl
            GROUP BY news_id ORDER BY MAX(published_at) DESC LIMIT :limit OFFSET :offset
        ) d ON y.id = d.min_id ORDER BY d.published_at DESC""",
        {"date_from": date(2026, 9, 1), "date_to_excl": date(2026, 9, 5), "limit": 10, "offset": 0})
    assert [r["news_id"] for r in rows] == ["n2", "n1"]
    assert sorted(rows[1]["tickers"].split(";;")) == ["M", "WMT"]
    assert isinstance(rows[0]["published_at"], datetime)


def test_not_ready_falls_back_to_mysql(monkeypatch):
    calls = []
    monkeypatch.setattr(nm, "ready", lambda key: False)
    import core.database as cdb
    monkeypatch.setattr(cdb.db_manager, "execute_query_readonly", lambda sql, p: calls.append(sql) or [{"id": 9}])
    assert nm.run("SELECT 1", {}, "av") == [{"id": 9}] and calls == ["SELECT 1"]


def test_equal_timestamps_follow_mysql_index_order(monkeypatch):
    # idx_av_time_id is (time_published_utc DESC, id ASC): a DESC page lists ties by
    # id ascending, an ASC page by id descending. A LIMIT edge must cut the same rows.
    c = local_db(monkeypatch)
    same = datetime(2026, 9, 5, 7, 30)
    nm._insert(c, "av", [dict(id=i, time_published_utc=same, ticker="M", title=f"t{i}", summary="", url="",
                              source_name="", source_domain="", ticker_sentiment_json="[]", topics_json="")
                         for i in (20, 21, 22)])
    q = ("SELECT n.id FROM coreiq_av_market_news_sentiment n WHERE n.time_published_utc >= :d "
         "ORDER BY n.time_published_utc {} LIMIT 2")
    assert [r["id"] for r in nm.query(q.format("DESC"), {"d": date(2026, 9, 5)})] == [20, 21]
    assert [r["id"] for r in nm.query(q.format("ASC"), {"d": date(2026, 9, 5)})] == [22, 21]


def test_ready_does_not_build_schema(monkeypatch):
    # Building a new index on the real 4 GB copy takes ~10 s: never on a page request.
    c = sqlite3.connect(":memory:", check_same_thread=False)
    monkeypatch.setattr(nm, "_conn", lambda: c)
    monkeypatch.setattr(nm, "_READY", {})
    monkeypatch.setattr(nm, "_schema", lambda conn: (_ for _ in ()).throw(AssertionError("schema built")))
    assert nm.ready("av") is False


def test_snapshot_restore_is_verified_before_serving(monkeypatch, tmp_path):
    import threading
    import utils.materialize as mat
    home, local = tmp_path / "home", tmp_path / "local"
    home.mkdir()
    monkeypatch.setattr(mat, "_cache_dir", lambda: str(home))
    monkeypatch.setenv("NEWS_MIRROR_DIR", str(local))
    monkeypatch.setattr(nm, "_LOCAL", threading.local())
    monkeypatch.setattr(nm, "_READY", {})
    c = nm._conn()
    nm._schema(c)
    for k in list(nm.TABLES) + ["companies"]:
        nm._set_state(c, f"{k}:complete", "1")
    c.commit()
    assert nm.ready("av")
    nm._write_snapshot()                                   # survives the deploy
    c.close()
    import os
    os.remove(nm._path())                                  # deploy: local disk wiped
    monkeypatch.setattr(nm, "_LOCAL", threading.local())
    nm._READY.clear()
    assert nm.ready("av") is False and not os.path.exists(nm._path())   # no empty file created
    checked = []
    monkeypatch.setattr(nm, "_tail", lambda conn, key: 0)
    monkeypatch.setattr(nm, "_pull_changed", lambda conn, key: 0)
    monkeypatch.setattr(nm, "_reconcile", lambda conn, key, **kw: checked.append(key))
    monkeypatch.setattr(nm, "_copy_companies", lambda conn: 0)
    monkeypatch.setattr(nm, "_maybe_snapshot", lambda conn: None)
    monkeypatch.setattr(nm, "slog_warning", lambda m: print("LOG", m))
    nm.sync(changed_tables=set())
    import time
    deadline = time.time() + 5
    while not (nm.ready("av") and nm.ready("tx")) and time.time() < deadline:
        time.sleep(0.02)                                   # sweeps run in their own threads
    assert set(checked) == set(nm.TABLES)                  # every table checked first
    assert nm.ready("av") and nm.ready("tx")


def test_block_recopy_is_atomic_and_hot_check_finds_recent_edit(monkeypatch):
    c = local_db(monkeypatch)
    nm._set_state(c, "av:max_id", 4)
    remote = {r["id"]: dict(r) for r in c.execute("SELECT * FROM coreiq_av_market_news_sentiment")}
    remote[3]["title"] = "AT&T and 60% gains"                  # an in-place UPDATE upstream
    import zlib

    def fake_db(sql, params):
        lo, hi = params["lo"], params["hi"]
        rows = [r for i, r in sorted(remote.items()) if lo <= i <= hi]
        if "CRC32" not in sql:
            return rows
        out = {}
        for r in rows:
            vals = [str(r["id"])] + ["~N~" if r.get(col) is None else str(r.get(col)) for col in nm._checksum_cols("av")]
            e = out.setdefault(r["id"] // nm._block("av"), [0, 0])
            e[0] += 1
            e[1] += zlib.crc32("|".join(vals).encode("utf-8"))
        return [{"b": b, "n": n, "h": h} for b, (n, h) in out.items()]
    monkeypatch.setattr(nm, "_db_rows", fake_db)
    assert nm._reconcile(c, "av", from_id=1) == 1
    row = c.execute("SELECT title, norm_title FROM coreiq_av_market_news_sentiment WHERE id = 3").fetchone()
    assert tuple(row) == ("AT&T and 60% gains", "at&t and 60% gains")
    assert c.execute("SELECT COUNT(*) FROM coreiq_av_market_news_sentiment").fetchone()[0] == 4
    assert nm._reconcile(c, "av", from_id=1) == 0               # now identical


def test_change_reported_during_a_running_sync_is_not_dropped(monkeypatch):
    import threading
    import time
    seen, gate = [], threading.Event()

    def slow_pass(changed, reconcile):
        seen.append(set(changed or ()))
        if len(seen) == 1:
            gate.wait(2)                 # first pass still running…
    monkeypatch.setattr(nm, "enabled", lambda: True)
    monkeypatch.setattr(nm, "_sync_pass", slow_pass)
    t = threading.Thread(target=nm.sync, args=({"coreiq_av_market_news_sentiment"},))
    t.start()
    time.sleep(0.1)
    nm.sync({"coreiq_yf_market_news_sentiment"})        # …when the yf edit is reported
    gate.set()
    t.join(3)
    assert seen == [{"coreiq_av_market_news_sentiment"}, {"coreiq_yf_market_news_sentiment"}]


def test_applied_change_rebuilds_readers_then_clears_memory(monkeypatch):
    import utils.freshness as fr
    import utils.persist as ps
    order = []
    monkeypatch.setattr(nm, "ready", lambda key: True)
    monkeypatch.setattr(fr, "rebuild_readers", lambda tables: order.append(("disk", sorted(tables))))
    monkeypatch.setattr(ps, "on_tables_moved", lambda tables: order.append(("persist", sorted(tables))))
    monkeypatch.setattr(nm, "_ON_CHANGE", {})
    nm.on_change("news", {"av", "yf"}, lambda: order.append(("ram", "news")))
    nm.on_change("tx", {"tx"}, lambda: order.append(("ram", "tx")))
    nm._changed({"yf"})
    assert order == [("disk", ["coreiq_yf_market_news_sentiment"]),
                     ("persist", ["coreiq_yf_market_news_sentiment"]), ("ram", "news")]


def test_rebuild_requested_while_running_runs_again(monkeypatch):
    import utils.freshness as fr
    monkeypatch.setattr(fr, "_QUEUED", set())
    monkeypatch.setattr(fr, "_AGAIN", set())
    put = []
    monkeypatch.setattr(fr._WORK, "put", lambda item: put.append(item))
    fr._enqueue("x", "first")
    fr._enqueue("x", "second while running")          # deduped, but remembered
    assert len(put) == 1 and fr._AGAIN == {"x"}


def test_transcript_refetched_in_place_is_pulled(monkeypatch):
    c = local_db(monkeypatch)
    old = dict(id=5, source="av", ticker="M", quarter="Q1", year=2020, q=1, transcript_text="old text",
               has_transcript=1, title="t", event_datetime_utc=datetime(2020, 5, 1), fetched_at_utc=datetime(2026, 9, 1),
               raw_json_sha1="a")
    nm._insert(c, "tx", [old])
    nm._set_state(c, "tx:max_id", 10)
    c.commit()
    new = {**old, "transcript_text": "corrected text", "fetched_at_utc": datetime(2026, 10, 2), "raw_json_sha1": "b"}
    monkeypatch.setattr(nm, "_db_rows", lambda sql, p: [{"id": 5}] if sql.startswith("SELECT id ") else [new])
    assert nm._pull_changed(c, "tx") == 1
    assert c.execute("SELECT transcript_text FROM coreiq_av_earnings_call_transcripts WHERE id = 5").fetchone()[0] == "corrected text"
    assert nm._pull_changed(c, "tx") == 0                  # already current → nothing re-copied


def test_aggregate_datetimes_come_back_as_datetime(monkeypatch):
    # get_news_date_range: MIN/MAX(...) AS ts — the Newsroom subtracts timedeltas from it.
    local_db(monkeypatch)
    r = nm.query("SELECT MIN(time_published_utc) AS ts FROM coreiq_av_market_news_sentiment", {})
    assert r[0]["ts"] == datetime(2026, 9, 1, 10)
    r = nm.query("SELECT title FROM coreiq_av_market_news_sentiment WHERE id = 1", {})
    assert r[0]["title"] == "Café chain lifts outlook"


def test_transcript_ties_follow_order_direction():
    sql, _ = nm._to_sqlite("SELECT id FROM coreiq_av_earnings_call_transcripts WHERE 1=1 ORDER BY year DESC, q DESC LIMIT :limit", {"limit": 5})
    assert "ORDER BY year DESC, q DESC, id DESC LIMIT" in sql
    sql, _ = nm._to_sqlite("SELECT DISTINCT q FROM coreiq_av_earnings_call_transcripts WHERE ticker = :t ORDER BY q", {"t": "M"})
    assert sql.rstrip().endswith("ORDER BY q")              # DISTINCT: no ties to break


def test_partial_copy_serves_only_fully_copied_recent_windows(monkeypatch):
    import json
    c = local_db(monkeypatch)
    monkeypatch.setattr(nm, "_COVERED", {})
    monkeypatch.setattr(nm, "_PARTIAL_OK", {"av": True})
    monkeypatch.setattr(nm, "enabled", lambda: True)
    nm._set_state(c, "companies:complete", "1")
    nm._set_state(c, "av:max_id", 4)
    monkeypatch.setitem(nm.TABLES["av"], "chunk", 2)            # chunks: ids 1-2 (start 0), 3-4 (start 2)
    # rows published since 2026-09-03 all have id >= 3; since 2026-09-01: id >= 1
    nm._set_state(c, "av:tiers", json.dumps({"2026-09-03 00:00:00": 3, "2026-09-01 00:00:00": 1}))
    nm._set_state(c, "av:chunks_done", json.dumps([2]))         # newest chunk only
    nm._update_coverage(c, "av")
    assert nm._COVERED["av"] == "2026-09-03 00:00:00"
    assert nm._partial_covers("av", {"date_from": date(2026, 9, 3), "date_to_excl": date(2026, 9, 5)})
    assert not nm._partial_covers("av", {"date_from": date(2026, 9, 2)})          # older → MySQL
    assert not nm._partial_covers("av", {})                                       # unbounded → MySQL
    assert nm._partial_covers("av", {"i0": 3, "i1": 4})                           # by-id, all present
    assert not nm._partial_covers("av", {"i0": 3, "i1": 99})                      # one missing → MySQL
    nm._set_state(c, "av:chunks_done", json.dumps([0, 2]))
    nm._update_coverage(c, "av")
    assert nm._COVERED["av"] == "2026-09-01 00:00:00"
    monkeypatch.setattr(nm, "_PARTIAL_OK", {})                  # not yet brought up to date
    assert not nm._partial_covers("av", {"date_from": date(2026, 9, 3)})
