"""Cold pool setup opens only the connections needed by real queries."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from core import database  # noqa: E402


@pytest.fixture
def pools(monkeypatch):
    created = []
    opened = []
    queries = []
    disposed = []
    block_at = [None]
    fail_at = [None]
    entered = threading.Event()
    release = threading.Event()

    def make_engine(url, **kwargs):
        index = len(created)
        if index == block_at[0]:
            entered.set()
            assert release.wait(3)
        if index == fail_at[0]:
            raise RuntimeError("engine setup failed")
        label = "read" if kwargs.get("isolation_level") == "AUTOCOMMIT" else "main"
        options = {key: value for key, value in kwargs.items() if key != "connect_args"}
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, **options)
        event.listen(engine, "connect", lambda *args: opened.append(label))
        event.listen(engine, "after_cursor_execute", lambda conn, cursor, sql, *args: queries.append((label, sql)))
        event.listen(engine, "engine_disposed", lambda engine: disposed.append(label))
        created.append(engine)
        return engine

    monkeypatch.delenv("DB_POOL_PREWARM", raising=False)
    monkeypatch.setattr(database, "create_engine", make_engine)
    monkeypatch.setattr(database, "_install_db_slow_query_logging", lambda *args: None)
    manager = object.__new__(database.DatabaseManager)
    manager._engine = manager._read_engine = manager._session_factory = None
    manager._config = SimpleNamespace(
        ssl_enabled=True, connect_args={}, connection_string="test://", pool_size=10,
        max_overflow=10, pool_timeout=5, pool_recycle=3600,
    )
    yield SimpleNamespace(manager=manager, created=created, opened=opened, queries=queries,
                          disposed=disposed, block_at=block_at, fail_at=fail_at,
                          entered=entered, release=release)
    release.set()
    for engine in created:
        engine.dispose()


@pytest.mark.parametrize("setting", [None, "0"])
def test_pool_setup_performs_no_eager_connection_work(pools, monkeypatch, setting):
    if setting is not None:
        monkeypatch.setenv("DB_POOL_PREWARM", setting)
    pools.manager.connect()
    pools.manager.connect()

    assert len(pools.created) == 2
    assert pools.opened == []
    assert pools.queries == []


@pytest.mark.parametrize("ssl_enabled, expected_queries", [(True, 6), (False, 2)])
def test_explicit_prewarm_retains_existing_validation(pools, monkeypatch, ssl_enabled, expected_queries):
    monkeypatch.setenv("DB_POOL_PREWARM", "1")
    pools.manager._config.ssl_enabled = ssl_enabled
    pools.manager.connect()

    assert len(pools.queries) == expected_queries
    assert all(sql == "SELECT 1" for _, sql in pools.queries)


def test_first_read_query_opens_only_the_read_pool(pools):
    result = pools.manager.execute_query_readonly_raising("SELECT 42 AS value")

    assert result == [{"value": 42}]
    assert pools.opened == ["read"]
    assert pools.queries == [("read", "SELECT 42 AS value")]


def test_concurrent_initialization_publishes_only_complete_pools(pools):
    pools.block_at[0] = 1
    with ThreadPoolExecutor(max_workers=2) as executor:
        initializing = executor.submit(pools.manager.connect)
        assert pools.entered.wait(1)
        unpublished = pools.manager._engine is None and pools.manager._read_engine is None
        reading = executor.submit(pools.manager.execute_query_readonly_raising, "SELECT 7 AS value")
        pools.release.set()
        initializing.result(timeout=3)
        result = reading.result(timeout=3)

    assert unpublished, "a caller could observe the main engine before the read pool existed"
    assert len(pools.created) == 2
    assert pools.opened == ["read"]
    assert result == [{"value": 7}]


def test_failed_pool_setup_can_retry_without_leaking_a_partial_engine(pools):
    pools.fail_at[0] = 1
    with pytest.raises(database.DatabaseConnectionError):
        pools.manager.connect()

    assert pools.manager._engine is None
    assert pools.manager._read_engine is None
    assert pools.disposed == ["main"]
    pools.fail_at[0] = None
    pools.manager.connect()
    assert len(pools.created) == 3
