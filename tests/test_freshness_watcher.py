"""Freshness watcher: rebuild only what changed, disk copy before the in-memory clear."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import utils.freshness as fr  # noqa: E402
import utils.materialize as mat  # noqa: E402


def setup(monkeypatch, meta, live_sig):
    events = []
    monkeypatch.setattr(fr, "_REGISTRY", {})
    monkeypatch.setattr(fr, "_QUEUED", set())
    monkeypatch.setattr(mat, "read_meta", lambda name: meta)
    monkeypatch.setattr(mat, "_live_signature", lambda sources, fresh=False: live_sig)
    monkeypatch.setattr(mat, "update_meta", lambda name, **f: events.append(("meta", f)))
    monkeypatch.setattr(mat, "write_materialized", lambda name, obj, sources: events.append(("write", obj)))
    monkeypatch.setattr(fr, "_enqueue", lambda name, why: events.append(("queue", name)))
    return events


def test_unmoved_tables_do_nothing(monkeypatch):
    ev = setup(monkeypatch, {"signature": [["t", "1", 5]], "update_times": {"t": "A"}}, [["t", "1", 5]])
    fr.register("c", lambda: 1, [{"table": "t", "signal": "MAX(id)"}])
    fr._check(["c"], {"t": "A"})
    assert ev == []


def test_moved_and_signature_changed_rebuilds(monkeypatch):
    ev = setup(monkeypatch, {"signature": [["t", "1", 5]], "update_times": {"t": "A"}}, [["t", "2", 6]])
    fr.register("c", lambda: 1, [{"table": "t", "signal": "MAX(id)"}])
    fr._check(["c"], {"t": "B"})
    assert ev == [("queue", "c")]


def test_moved_but_this_slice_unchanged_only_records(monkeypatch):
    ev = setup(monkeypatch, {"signature": [["t", "1", 5]], "update_times": {"t": "A"}}, [["t", "1", 5]])
    fr.register("c", lambda: 1, [{"table": "t", "signal": "MAX(id)", "where": "ticker='M'"}])
    fr._check(["c"], {"t": "B"})
    assert ev == [("meta", {"update_times": {"t": "B"}})]


def test_count_only_source_rebuilds_on_any_write(monkeypatch):
    # COUNT(*) cannot see an in-place UPDATE; the table's write time can.
    ev = setup(monkeypatch, {"signature": [["t", None, 5]], "update_times": {"t": "A"}}, [["t", None, 5]])
    fr.register("c", lambda: 1, [{"table": "t", "signal": None}])
    fr._check(["c"], {"t": "B"})
    assert ev == [("queue", "c")]


def test_worker_writes_disk_before_clearing_memory(monkeypatch):
    order = []
    monkeypatch.setattr(fr, "_REGISTRY", {})
    monkeypatch.setattr(fr, "_QUEUED", {"c"})
    monkeypatch.setattr(mat, "write_materialized", lambda name, obj, sources: order.append("write"))
    fr.register("c", lambda: order.append("build") or 1, [{"table": "t"}],
                clear=lambda: order.append("clear"), inputs=lambda: order.append("inputs"))
    fr._rebuild_one("c", "test")
    assert order == ["inputs", "build", "write", "clear"]
    assert "c" not in fr._QUEUED


def test_failed_build_keeps_previous_copy(monkeypatch):
    order = []
    monkeypatch.setattr(fr, "_REGISTRY", {})
    monkeypatch.setattr(fr, "_QUEUED", set())
    monkeypatch.setattr(mat, "write_materialized", lambda name, obj, sources: order.append("write"))
    def boom():
        raise RuntimeError("db down")
    fr.register("c", boom, [{"table": "t"}], clear=lambda: order.append("clear"))
    fr._rebuild_one("c", "test")
    assert order == []          # nothing written, in-memory copy kept
