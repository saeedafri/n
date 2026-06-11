"""
Database connector layer with connection pooling.
"""
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Generator, Callable, Tuple
from functools import wraps
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool

from .config import config

# Import error logger only
try:
    from utils.server_logger import log_error, log_structured_error, log_exception
except ImportError:
    def log_error(msg):
        pass
    def log_structured_error(exc, **kwargs):
        pass
    def log_exception(msg):
        pass


class DatabaseConnectionError(Exception):
    """Raised when database connection fails."""
    pass


class DatabaseQueryError(Exception):
    """Raised when a database query fails."""
    pass


class DatabaseManager:
    """
    Centralized database manager with connection pooling.
    Singleton pattern ensures single connection pool across the application.
    """
    _instance: Optional['DatabaseManager'] = None
    _lock = threading.Lock()

    # Phase 2: SQLAlchemy engine and session factory
    _engine: Any = None
    _session_factory: Any = None
    _read_engine: Any = None  # AUTOCOMMIT engine for read-only queries

    def __new__(cls) -> 'DatabaseManager':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._config = config.database
        self._initialized = True
        self._local = threading.local()

    def connect(self) -> None:
        """
        Initialize database connection pool with SSL and connection validation.
        Uses singleton pattern - if engine exists, reuse it to avoid 4s SSL handshake.

        PERF FIX (2026-04-02): Create BOTH engines first (no I/O), then warm ALL
        pool connections in ONE parallel batch. Previously engines were created and
        warmed sequentially — 2 batches of 2 SSL handshakes = ~9s. Now 1 batch
        of 6 concurrent handshakes = ~3.5s (one SSL latency instead of two).
        """
        # CRITICAL: If engine exists, reuse it (avoid expensive reconnection)
        if self._engine is not None:
            return

        try:
            ssl_enabled = self._config.ssl_enabled
            # Get actual connect_args (triggers late-binding if needed)
            connect_args = self._config.connect_args

            # pool_pre_ping=True sends a SELECT 1 before every connection checkout.
            # On local (RTT ~0ms) this is free. On Azure MySQL (RTT ~240ms) it adds
            # ~240ms to EVERY DB call, doubling query latency.
            # For remote DB (ssl_enabled = Azure/STAGING/PROD), disable pre_ping and
            # rely on pool_recycle to replace stale connections instead.
            _use_pre_ping = not ssl_enabled  # True for local, False for Azure

            # ── Step 1: Create both engines (no I/O, ~0.3s each) ─────────
            self._engine = create_engine(
                self._config.connection_string,
                poolclass=QueuePool,
                pool_size=self._config.pool_size,
                max_overflow=self._config.max_overflow,
                pool_timeout=self._config.pool_timeout,
                pool_recycle=self._config.pool_recycle,
                pool_pre_ping=_use_pre_ping,
                echo=False,
                connect_args=connect_args,
            )
            self._session_factory = sessionmaker(bind=self._engine)

            # Read-only engine (AUTOCOMMIT) — avoids implicit BEGIN/ROLLBACK.
            # On Azure MySQL Flex (~230ms RTT), saves 2 round-trips per SELECT.
            self._read_engine = create_engine(
                self._config.connection_string,
                poolclass=QueuePool,
                pool_size=self._config.pool_size,
                max_overflow=self._config.max_overflow,
                pool_timeout=self._config.pool_timeout,
                pool_recycle=self._config.pool_recycle,
                pool_pre_ping=_use_pre_ping,
                echo=False,
                connect_args=connect_args,
                isolation_level="AUTOCOMMIT",
                skip_autocommit_rollback=True,
            )

            # ── Step 2: Warm ALL connections in ONE parallel batch ────────
            # On SSL (Azure): 6 connections across both engines — all SSL
            # handshakes happen concurrently = ~3.5s (one handshake latency).
            # 3 read connections: serves DATE_RANGE (4 parallel MIN/MAX) + AV/YF.
            # 3 main connections: session-based writes + screening.
            # On local: 1 connection per engine (no SSL overhead).
            _main_engine = self._engine
            _rd_engine = self._read_engine

            def _warm_conn(engine):
                with engine.connect() as c:
                    c.execute(text("SELECT 1"))

            try:
                if ssl_enabled:
                    import concurrent.futures
                    _warm_tasks = (
                        [(_main_engine,)] * 3 +
                        [(_rd_engine,)] * 3
                    )
                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                        futs = [ex.submit(_warm_conn, eng) for (eng,) in _warm_tasks]
                        for f in futs:
                            f.result(timeout=30)
                else:
                    _warm_conn(_main_engine)
                    _warm_conn(_rd_engine)
            except Exception as test_err:
                raise DatabaseConnectionError(
                    f"Database connection test failed. Check credentials, network access, "
                    f"and SSL configuration. Error: {test_err}"
                )
        except DatabaseConnectionError:
            log_structured_error(DatabaseConnectionError("Connection test failed"), page="database", component="DatabaseManager.connect", operation="db_connection_test")
            raise
        except Exception as e:
            # If read engine creation failed, clean up
            if self._read_engine is None and self._engine is not None:
                pass  # main engine OK, read engine will fall back
            else:
                log_structured_error(e, page="database", component="DatabaseManager.connect", operation="db_engine_create")
                raise DatabaseConnectionError(f"Database connection failed: {e}")

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """
        Context manager for database sessions.
        Ensures proper transaction handling and connection cleanup.
        On SSL connection errors, disposes the engine and retries once.
        """
        if self._engine is None:
            self.connect()
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            # Detect SSL / insecure-transport errors and attempt engine recovery
            err_str = str(e)
            if "insecure transport" in err_str or "require_secure_transport" in err_str:
                log_structured_error(e, page="database", component="DatabaseManager.get_session", operation="ssl_connection_recovery")
                try:
                    session.close()
                except Exception:
                    pass
                self._dispose_and_reconnect()
                raise DatabaseQueryError(f"SSL connection failed — engine recycled, retry the operation: {e}")
            log_structured_error(e, page="database", component="DatabaseManager.get_session", operation="transaction_commit")
            raise DatabaseQueryError(f"Query execution failed: {e}")
        finally:
            session.close()

    def _dispose_and_reconnect(self):
        """Dispose the current engine and force a fresh connection with current SSL config."""
        try:
            if self._engine is not None:
                self._engine.dispose()
        except Exception as e:
            log_structured_error(e, page="database", component="DatabaseManager._dispose_and_reconnect", operation="dispose_main_engine")
        try:
            if self._read_engine is not None:
                self._read_engine.dispose()
        except Exception as e:
            log_structured_error(e, page="database", component="DatabaseManager._dispose_and_reconnect", operation="dispose_read_engine")
        self._engine = None
        self._session_factory = None
        self._read_engine = None
        try:
            self.connect()
        except Exception as e:
            log_structured_error(e, page="database", component="DatabaseManager._dispose_and_reconnect", operation="reconnect")
            raise

    def execute_query(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute a raw SQL query and return results as list of dictionaries.

        Args:
            query: SQL query string
            params: Optional query parameters

        Returns:
            List of row dictionaries
        """
        _start = time.perf_counter()

        # Extract query type and table for logging
        query_upper = query.strip().upper()
        query_type = "UNKNOWN"
        table = "UNKNOWN"

        if query_upper.startswith("SELECT"):
            query_type = "SELECT"
        elif query_upper.startswith("INSERT"):
            query_type = "INSERT"
        elif query_upper.startswith("UPDATE"):
            query_type = "UPDATE"
        elif query_upper.startswith("DELETE"):
            query_type = "DELETE"

        # Try to extract table name
        try:
            if "FROM" in query_upper:
                parts = query_upper.split("FROM")
                if len(parts) > 1:
                    table_part = parts[1].strip().split()[0]
                    table = table_part.strip()
        except Exception:
            pass

        # Extract ticker from params if present
        ticker = ""
        if params:
            for key in ['ticker', 'TICKER', ':ticker']:
                if key in params:
                    ticker = str(params[key])
                    break

        try:
            with self.get_session() as session:
                result = session.execute(text(query), params or {})
                rows = [dict(row._mapping) for row in result]
                return rows
        except Exception as _exc:
            log_structured_error(_exc, page="database", component="DatabaseManager.execute_query", operation="DB_QUERY")
            return []

    def execute_query_readonly(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute a read-only SQL query using the AUTOCOMMIT read engine.

        Uses a dedicated engine with isolation_level="AUTOCOMMIT" to avoid
        the implicit BEGIN and ROLLBACK that the default engine sends.
        On Azure MySQL Flex (~230ms RTT), this saves 2 network round-trips
        compared to the session path, yielding ~55-60% wall-clock improvement.

        Falls back to the main engine if the read engine is unavailable.

        Safe for: pure SELECT queries with no ORM identity dependencies,
        no session variables, and no multi-statement transactional consistency.

        Args:
            query: SQL SELECT query string
            params: Optional query parameters

        Returns:
            List of row dictionaries
        """
        if self._engine is None:
            self.connect()
        engine = self._read_engine or self._engine
        try:
            with engine.connect() as conn:
                result = conn.execute(text(query), params or {})
                return [dict(row._mapping) for row in result]
        except Exception as e:
            log_structured_error(e, page="database", component="DatabaseManager.execute_query_readonly", operation="readonly_query")
            return []

    def execute_query_readonly_raising(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Like execute_query_readonly but RAISES on error instead of returning [].

        Use inside @st.cache_data-decorated repository functions to prevent
        Streamlit from caching failure-returned empty lists (cache poisoning
        on DB timeout or transient error).
        """
        if self._engine is None:
            self.connect()
        engine = self._read_engine or self._engine
        with engine.connect() as conn:
            result = conn.execute(text(query), params or {})
            return [dict(row._mapping) for row in result]

    def execute_scalar(self, query: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute query returning single scalar value."""
        try:
            with self.get_session() as session:
                result = session.execute(text(query), params or {})
                return result.scalar()
        except Exception as e:
            log_structured_error(e, page="database", component="DatabaseManager.execute_scalar", operation="scalar_query")
            return None

    def execute_insert(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Execute an INSERT/UPDATE/DELETE query and return affected rows.

        Uses the AUTOCOMMIT engine (same as execute_query_readonly) to avoid
        the BEGIN + COMMIT round trips that get_session() sends.
        On Azure MySQL (~240ms RTT), this saves ~2 RTTs per write call.

        Safe for: any single-statement INSERT/UPDATE/DELETE.
        NOT safe for: multi-statement sequences that must roll back atomically
        (use get_session() directly for those).

        Args:
            query: SQL query string
            params: Optional query parameters

        Returns:
            Number of rows affected
        """
        if self._engine is None:
            self.connect()
        engine = self._read_engine or self._engine
        try:
            with engine.connect() as conn:
                result = conn.execute(text(query), params or {})
                return result.rowcount
        except Exception as e:
            log_structured_error(e, page="database", component="DatabaseManager.execute_insert", operation="insert_query")
            return 0

    def execute_insert_returning_id(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Execute a single-statement INSERT and return the new row id.

        Uses the AUTOCOMMIT read engine instead of a session, eliminating
        the BEGIN + COMMIT round trips (saves ~2 × RTT on Azure MySQL).

        Safe for: single INSERT statements that do not need multi-statement
        transactional atomicity.  NOT safe for multi-step sequences where
        partial failure must roll back earlier statements.

        Returns:
            lastrowid of the inserted row, or None on failure.
        """
        if self._engine is None:
            self.connect()
        engine = self._read_engine or self._engine
        try:
            with engine.connect() as conn:
                result = conn.execute(text(query), params or {})
                return result.lastrowid
        except Exception as e:
            log_structured_error(
                e, page="database",
                component="DatabaseManager.execute_insert_returning_id",
                operation="insert_autocommit",
            )
            return None

    def health_check(self) -> bool:
        """Check database connectivity."""
        try:
            with self.get_session() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def fetch_one(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a SELECT query and return first row.

        Args:
            query: SQL query string
            params: Optional query parameters

        Returns:
            First row as dictionary or None
        """
        rows = self.execute_query_readonly(query, params)
        return rows[0] if rows else None

    def fetch_all(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute a SELECT query and return all rows.

        Args:
            query: SQL query string
            params: Optional query parameters

        Returns:
            List of row dictionaries
        """
        return self.execute_query_readonly(query, params)

    def execute_update(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Execute an UPDATE query and return affected rows.

        Args:
            query: SQL query string
            params: Optional query parameters

        Returns:
            Number of rows affected
        """
        return self.execute_insert(query, params)

    def execute_delete(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Execute a DELETE query and return affected rows.

        Args:
            query: SQL query string
            params: Optional query parameters

        Returns:
            Number of rows affected
        """
        return self.execute_insert(query, params)

    def execute_queries_batch(
        self,
        queries: List[tuple]
    ) -> List[List[Dict[str, Any]]]:
        """
        Execute multiple queries in a SINGLE session for connection reuse.

        PERFORMANCE OPTIMIZATION:
        - Reuses single connection for all queries (avoids 4x connection overhead)
        - Best for Stock Quote type queries where 4 separate queries needed

        Args:
            queries: List of (query_string, params_dict) tuples

        Returns:
            List of results for each query

        Example:
            results = db_manager.execute_queries_batch([
                ("SELECT * FROM t1 WHERE id=:id", {"id": 1}),
                ("SELECT * FROM t2 WHERE id=:id", {"id": 1}),
            ])
        """
        results = []

        try:
            with self.get_session() as session:
                for query, params in queries:
                    result = session.execute(text(query), params or {})
                    rows = [dict(row._mapping) for row in result]
                    results.append(rows)
            return results
        except Exception as e:
            log_structured_error(e, page="database", component="DatabaseManager.execute_queries_batch", operation="batch_query")
            return []


class MockSession:
    """Mock session for Phase 1 development."""
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def execute(self, query, params=None):
        return MockResult()


class MockResult:
    """Mock result for Phase 1 development."""
    def scalar(self):
        return None

    def __iter__(self):
        return iter([])


# Global database manager instance
db_manager = DatabaseManager()


def with_db_session(func: Callable) -> Callable:
    """Decorator to inject database session into function."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            with db_manager.get_session() as session:
                kwargs['session'] = session
                return func(*args, **kwargs)
        except Exception as e:
            log_structured_error(
                e,
                page="database",
                component="with_db_session",
                operation=f"decorator_{func.__name__}",
            )
            raise
    return wrapper


def init_database():
    """Initialize database connection on application startup."""
    try:
        db_manager.connect()
    except Exception as e:
        log_structured_error(
            e,
            page="database",
            component="init_database",
            operation="db_init",
        )
        raise  # App cannot function without DB — fail fast


def warmup_av_fulltext():
    """
    One-shot FULLTEXT buffer-pool warmup for AV news sentiment table.

    Fires a lightweight FULLTEXT probe to pre-load ft_av_title index pages
    into the MySQL buffer pool. Runs once, typically < 1s.

    Also warms the earnings_call_transcripts covering indexes so the first
    user visiting the Earnings Calls page gets fast dropdown population.

    Gated by ENABLE_AV_FT_WARMUP=1 in main.py.
    """
    import os
    if os.getenv("ENABLE_AV_FT_WARMUP", "0").strip() != "1":
        return

    try:
        # Warm AV FULLTEXT index
        db_manager.execute_query_readonly(
            "SELECT id FROM coreiq_av_market_news_sentiment "
            "WHERE MATCH(title) AGAINST ('earnings' IN BOOLEAN MODE) LIMIT 1"
        )
    except Exception:
        pass

    try:
        # Warm earnings call transcripts covering indexes
        db_manager.execute_query_readonly(
            "SELECT DISTINCT ticker FROM coreiq_av_earnings_call_transcripts "
            "WHERE has_transcript = 1 ORDER BY ticker LIMIT 5"
        )
    except Exception:
        pass
