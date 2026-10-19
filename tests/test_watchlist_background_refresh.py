"""Expired watchlist dropdown metadata must not block transcript searches."""

import ast
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from data import watchlist_service as service  # noqa: E402


@pytest.fixture
def watchlists(monkeypatch):
    clock = [1000.0]
    release = threading.Event()
    entered = threading.Event()
    calls = []
    errors = []
    rows = {"one@example.test": [{"id": 1, "name": "Old"}],
            "two@example.test": [{"id": 2, "name": "Other"}]}
    failure = [False]

    def read(query, params):
        calls.append(("sync", params["email"]))
        return [dict(row) for row in rows[params["email"]]]

    def read_raising(query, params):
        calls.append(("background", params["email"]))
        snapshot = [dict(row) for row in rows[params["email"]]]
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test refresh not released")
        if failure[0]:
            raise RuntimeError("database unavailable")
        return snapshot

    class Pool:
        def __init__(self):
            self.executor = ThreadPoolExecutor(max_workers=2)
            self.futures = []

        def submit(self, *args, **kwargs):
            future = self.executor.submit(*args, **kwargs)
            self.futures.append(future)
            return future

        def finish(self):
            release.set()
            for future in self.futures:
                future.result(timeout=3)

    pool = Pool()
    monkeypatch.setattr(service, "_HAS_STREAMLIT", False)
    monkeypatch.setattr(service, "_tables_ready", True)
    monkeypatch.setattr(service, "_WL_PROCESS_CACHE", {})
    monkeypatch.setattr(service, "_WL_REFRESHING", {}, raising=False)
    monkeypatch.setattr(service, "_WL_CACHE_VERSION", 0, raising=False)
    monkeypatch.setattr(service, "_WL_REFRESH_EXECUTOR", pool, raising=False)
    monkeypatch.setattr(service, "_wl_time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(service, "db_manager", SimpleNamespace(
        execute_query_readonly=read, execute_query_readonly_raising=read_raising,
    ))
    monkeypatch.setattr(service, "log_structured_error", lambda *args, **kwargs: errors.append(args[0]))
    yield SimpleNamespace(clock=clock, release=release, entered=entered, calls=calls,
                          errors=errors, rows=rows, failure=failure, pool=pool)
    pool.finish()
    pool.executor.shutdown(wait=True)


def seed_expired(state, email="one@example.test"):
    service.get_user_watchlists(email)
    state.clock[0] = 1120.0


def test_expired_metadata_returns_immediately_and_deduplicates_refresh(watchlists):
    seed_expired(watchlists)
    started = time.monotonic()
    result = service.get_user_watchlists("one@example.test", background_refresh=True)
    assert time.monotonic() - started < 0.25
    assert result == [{"id": 1, "name": "Old"}]
    assert watchlists.entered.wait(1)

    result[0]["name"] = "Caller mutation"
    assert service.get_user_watchlists("one@example.test", background_refresh=True) == [
        {"id": 1, "name": "Old"}]
    assert len(watchlists.pool.futures) == 1


def test_successful_refresh_replaces_expired_metadata(watchlists):
    seed_expired(watchlists)
    watchlists.rows["one@example.test"] = [{"id": 1, "name": "Updated"}]
    service.get_user_watchlists("one@example.test", background_refresh=True)
    watchlists.pool.finish()

    assert service.get_user_watchlists("one@example.test", background_refresh=True) == [
        {"id": 1, "name": "Updated"}]
    assert len(watchlists.pool.futures) == 1


def test_failed_refresh_preserves_cached_rows_and_logs_failure(watchlists):
    seed_expired(watchlists)
    watchlists.failure[0] = True
    service.get_user_watchlists("one@example.test", background_refresh=True)
    watchlists.pool.finish()

    assert service._WL_PROCESS_CACHE["one@example.test"] == (
        1000.0, [{"id": 1, "name": "Old"}])
    assert len(watchlists.errors) == 1
    assert isinstance(watchlists.errors[0], RuntimeError)


def test_other_callers_still_refresh_synchronously(watchlists):
    seed_expired(watchlists)
    watchlists.rows["one@example.test"] = [{"id": 1, "name": "Updated"}]

    assert service.get_user_watchlists("one@example.test") == [{"id": 1, "name": "Updated"}]
    assert watchlists.calls == [("sync", "one@example.test"), ("sync", "one@example.test")]
    assert not watchlists.pool.futures


def test_first_call_without_cache_remains_synchronous(watchlists):
    assert service.get_user_watchlists("one@example.test", background_refresh=True) == [
        {"id": 1, "name": "Old"}]
    assert watchlists.calls == [("sync", "one@example.test")]
    assert not watchlists.pool.futures


def test_different_users_refresh_independently(watchlists):
    service.get_user_watchlists("one@example.test")
    service.get_user_watchlists("two@example.test")
    watchlists.clock[0] = 1120.0
    watchlists.rows["one@example.test"] = [{"id": 1, "name": "Updated one"}]
    watchlists.rows["two@example.test"] = [{"id": 2, "name": "Updated two"}]

    assert service.get_user_watchlists("one@example.test", background_refresh=True)[0]["id"] == 1
    assert service.get_user_watchlists("two@example.test", background_refresh=True)[0]["id"] == 2
    watchlists.pool.finish()
    assert service.get_user_watchlists("one@example.test")[0]["name"] == "Updated one"
    assert service.get_user_watchlists("two@example.test")[0]["name"] == "Updated two"


def test_invalidation_rejects_an_older_inflight_snapshot(watchlists):
    seed_expired(watchlists)
    service.get_user_watchlists("one@example.test", background_refresh=True)
    assert watchlists.entered.wait(1)
    service._invalidate_watchlist_cache("one@example.test")
    watchlists.rows["one@example.test"] = [{"id": 1, "name": "After write"}]
    assert service.get_user_watchlists("one@example.test", background_refresh=True)[0]["name"] == "After write"
    watchlists.pool.finish()

    assert service.get_user_watchlists("one@example.test")[0]["name"] == "After write"


def test_pending_refreshes_are_bounded(watchlists, monkeypatch):
    monkeypatch.setattr(service, "_WL_REFRESH_MAX_PENDING", 1, raising=False)
    service.get_user_watchlists("one@example.test")
    service.get_user_watchlists("two@example.test")
    watchlists.clock[0] = 1120.0
    service.get_user_watchlists("one@example.test", background_refresh=True)

    assert service.get_user_watchlists("two@example.test", background_refresh=True) == [
        {"id": 2, "name": "Other"}]
    assert len(watchlists.pool.futures) == 1


def test_earnings_calls_opts_into_background_refresh(watchlists):
    seed_expired(watchlists)
    watchlists.rows["one@example.test"] = [{"id": 1, "name": "Updated"}]
    page = Path(__file__).resolve().parents[1] / "app" / "pages" / "earnings_calls.py"
    tree = ast.parse(page.read_text())
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "get_user_watchlists")
    namespace = {"get_user_watchlists": service.get_user_watchlists,
                 "_ec_calls_user_email": "one@example.test"}
    result = eval(compile(ast.Expression(call), str(page), "eval"), namespace)

    assert result == [{"id": 1, "name": "Old"}]
    assert len(watchlists.pool.futures) == 1
