"""Persisted per-company results: stored with the tables they read, served from disk,
refreshed behind the reader when a table moved, never replaced by an empty result."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import utils.persist as ps  # noqa: E402
import utils.materialize as mat  # noqa: E402


def setup(monkeypatch, tmp_path, times):
    monkeypatch.setattr(mat, "_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ps, "_TIMES", dict(times))
    monkeypatch.setattr(ps, "_TIMES_AT", {t: ps.time.monotonic() + 3600 for t in times})
    monkeypatch.setattr(ps, "_live_times", lambda tables: {t: ps._TIMES.get(t) for t in tables})
    monkeypatch.setattr(ps, "_RECENT", ps.OrderedDict())
    queued = []
    monkeypatch.setattr(ps, "_enqueue", lambda n, k, a, kw, check_only=False: queued.append((n, a)))
    return queued


def test_built_once_then_served_from_disk(monkeypatch, tmp_path):
    q = setup(monkeypatch, tmp_path, {"coreiq_t": "T1"})
    calls = []

    @ps.persistent("demo")
    def f(t):
        calls.append(t)
        ps.record("SELECT * FROM coreiq_t WHERE ticker = 'M'")
        return {"v": t}

    assert f("M") == {"v": "M"} and f("M") == {"v": "M"}
    assert calls == ["M"] and q == []
    assert ps.tables_in_use() == {"coreiq_t"}


def test_moved_table_serves_old_and_refreshes_behind(monkeypatch, tmp_path):
    q = setup(monkeypatch, tmp_path, {"coreiq_t": "T1"})

    @ps.persistent("demo2")
    def f(t):
        ps.record("SELECT 1 FROM coreiq_t")
        return [t]

    f("M")
    ps._TIMES["coreiq_t"] = "T2"          # a write landed
    assert f("M") == ["M"]                 # served at once
    assert q == [("demo2", ("M",))]        # and refreshed in the background


def test_empty_result_is_never_stored(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, {"coreiq_t": "T1"})
    calls = []

    @ps.persistent("demo3")
    def f(t):
        calls.append(t)
        ps.record("SELECT 1 FROM coreiq_t")
        return []

    f("M"); f("M")
    assert calls == ["M", "M"]


def test_rebuild_returning_nothing_keeps_stored_copy(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, {"coreiq_t": "T1"})
    state = {"v": [1, 2]}

    @ps.persistent("demo4")
    def f(t):
        ps.record("SELECT 1 FROM coreiq_t")
        return state["v"]

    f("M")
    state["v"] = []                        # e.g. the query failed and returned []
    key = ps._key(("M",), {}, ps._code_version(f.__wrapped__))
    ps._rebuild("demo4", key, ("M",), {})
    assert ps._load("demo4", key)[0] == [1, 2]


def test_watcher_rebuilds_recent_results_of_moved_tables(monkeypatch, tmp_path):
    q = setup(monkeypatch, tmp_path, {"coreiq_a": "T1", "coreiq_b": "T1"})

    @ps.persistent("demo5")
    def f(t):
        ps.record("SELECT 1 FROM coreiq_a")
        return [t]

    f("M")
    ps.on_tables_moved({"coreiq_b"})
    assert q == []
    ps.on_tables_moved({"coreiq_a"})
    assert q == [("demo5", ("M",))]


def test_strict_rebuilds_before_answering(monkeypatch, tmp_path):
    q = setup(monkeypatch, tmp_path, {"coreiq_t": "T1"})
    state = {"v": ["old"]}

    @ps.persistent("demo6", strict=True)
    def f(t):
        ps.record("SELECT 1 FROM coreiq_t")
        return state["v"]

    f("M")
    state["v"] = ["new"]
    ps._TIMES["coreiq_t"] = "T2"
    assert f("M") == ["new"] and q == []


def test_unknown_write_times_serve_stored_copy_and_check_behind(monkeypatch, tmp_path):
    # Right after boot the watcher has not reported yet: no DB round trip on the page.
    q = setup(monkeypatch, tmp_path, {"coreiq_t": "T1"})
    checks = []
    monkeypatch.setattr(ps, "_enqueue", lambda n, k, a, kw, check_only=False: checks.append(check_only))

    @ps.persistent("demo7")
    def f(t):
        ps.record("SELECT 1 FROM coreiq_t")
        return [t]

    f("M")
    ps._TIMES_AT.clear()                                   # fresh process: nothing known
    monkeypatch.setattr(ps, "_live_times", lambda tables: (_ for _ in ()).throw(AssertionError("blocked on DB")))
    assert f("M") == ["M"] and checks == [True]


def test_write_for_another_ticker_does_not_rebuild(monkeypatch, tmp_path):
    q = setup(monkeypatch, tmp_path, {"coreiq_filing_metrics_v5": "T1"})
    monkeypatch.setattr(ps, "_VERIFIED", {})

    @ps.persistent("demo8")
    def f(t):
        ps.record("SELECT 1 FROM coreiq_filing_metrics_v5 WHERE ticker = 'x'")
        return [t]

    f("M"); f("WMT")
    monkeypatch.setattr(ps, "_moved_tickers", lambda table: {"WMT"})
    ps._TIMES["coreiq_filing_metrics_v5"] = "T1"
    ps.on_tables_moved({"coreiq_filing_metrics_v5"}, {"coreiq_filing_metrics_v5": "T2"})
    assert q == [("demo8", ("WMT",))]                    # only WMT's result rebuilt
    ps._TIMES_AT["coreiq_filing_metrics_v5"] = ps.time.monotonic() + 3600
    calls = []
    monkeypatch.setattr(ps, "_enqueue", lambda n, k, a, kw, check_only=False: calls.append(a))
    assert f("M") == ["M"] and calls == []               # M served, still exact at T2
    monkeypatch.setattr(ps, "_moved_tickers", lambda table: None)   # full pass / probe failed
    ps.on_tables_moved({"coreiq_filing_metrics_v5"}, {"coreiq_filing_metrics_v5": "T3"})
    assert ("M",) in calls and ("WMT",) in calls


def inner_rows():
    return list(MEMO["rows"])


MEMO = {}


def test_composite_inherits_inner_tables_and_is_rebuilt_after_it(monkeypatch, tmp_path):
    # outer() builds from inner_rows(), answered from memory (no SQL): it must still
    # depend on the inner's tables, and be rebuilt once the inner's value changes.
    q = setup(monkeypatch, tmp_path, {"coreiq_companies": "T1"})
    for reg in ("_NAME_TABLES", "_DEPS"):
        monkeypatch.setattr(ps, reg, {})
    global inner_rows

    @ps.persistent("demo_inner")
    def inner_rows():
        ps.record("SELECT * FROM coreiq_companies")
        return ["Macy's"]

    MEMO["rows"] = inner_rows()                        # inner built once (records its table)

    @ps.persistent("demo_outer")
    def outer():
        return [r.upper() for r in MEMO["rows"]]      # source names inner_rows( below
    outer.__wrapped__.__doc__ = "uses inner_rows()"

    monkeypatch.setattr(ps, "_deps", lambda name: {"demo_inner"} if name == "demo_outer" else set())
    assert outer() == ["MACY'S"]
    key = ps._key((), {}, ps._code_version(outer.__wrapped__))
    assert ps._load("demo_outer", key)[1]["tables"] == ["coreiq_companies"]
    ps.on_tables_moved({"coreiq_companies"}, {"coreiq_companies": "T2"})
    assert ("demo_outer", ()) in q


class _Rows:
    """A stored result that reaches another one only through a helper — the shape of
    SegmentDataRepository (stored tables -> builder -> stored filing rows)."""

    @staticmethod
    @ps.persistent("demo_rows")
    def rows(t):
        ps.record("SELECT * FROM coreiq_filing_metrics_v5")
        return [t]

    @staticmethod
    def build(t):
        return {"rows": _Rows.rows(t)}

    @staticmethod
    @ps.persistent("demo_tables")
    def tables(t):
        return _Rows.build(t)


def test_dependencies_are_found_through_helper_methods(monkeypatch, tmp_path):
    """Stored Segments tables read filing rows through a builder; without following
    the helper, a data load never rebuilt them."""
    setup(monkeypatch, tmp_path, {"coreiq_filing_metrics_v5": "T1"})
    monkeypatch.setattr(ps, "_DEPS", {})
    assert ps._deps("demo_tables") == {"demo_rows"}


def test_segment_tables_are_keyed_by_the_companys_data_version(monkeypatch):
    """A data load is a new data version, hence a new stored table: nothing to run."""
    from data.repository import SegmentDataRepository as repo
    seen = []
    monkeypatch.setattr(repo, "_stored_segment_tables",
                        staticmethod(lambda *a: seen.append(a[-1]) or {"years": []}))
    monkeypatch.setattr(repo, "current_data_version", staticmethod(lambda t: "v1"))
    repo.full_segment_tables("AAPL", fresh=True)
    monkeypatch.setattr(repo, "current_data_version", staticmethod(lambda t: "v2"))
    repo.full_segment_tables("AAPL", fresh=True)
    assert seen == ["v1", "v2"]
