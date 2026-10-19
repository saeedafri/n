"""Run the real portal with result caches bypassed and a read-only SQL audit."""

import builtins
import functools
import json
import os
from pathlib import Path
import re
import runpy
import sys
import threading
import time

import streamlit as st
from sqlalchemy import event
from sqlalchemy.engine import Engine


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
AUDIT = Path(os.environ["EC_COLD_AUDIT_DIR"]).resolve()
AUDIT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(APP))
os.chdir(APP)
MODE = os.environ.get("EC_COLD_MODE", "mysql_origin")
if MODE not in {"mysql_origin", "durable_sqlite_index", "isolated_native_snapshot"}:
    raise RuntimeError("Unknown cold audit mode")

POLICY = {
    "PERSIST": "0", "PAGE_MATERIALIZE": "0", "NEWS_MIRROR": "0",
    "TRANSCRIPT_LOCAL_SEARCH": "0", "FRESHNESS": "0", "WARM_ON_BOOT": "0",
    "ENABLE_BG_WARMUP": "0", "ENABLE_EC_WARMUP": "0", "ENABLE_FILINGS_WARMUP": "0",
    "ENABLE_BG_SCANNER": "0", "ENABLE_SEGMENT_CACHE_AUTO_REFRESH": "0",
    "ENABLE_FORECAST_AUTO_REFRESH": "0", "ENABLE_MA_OVERRIDES_AUTO": "0",
    "EARNINGS_ALERT_DISPATCH": "0", "EARNINGS_ALERT_DISPATCH_THREAD": "0",
    "APP_DB_WARMUP_STARTED": "1", "APP_SCREENING_PREWARM_STARTED": "1",
    "APP_AV_FT_WARMED": "1", "APP_EC_WARMED": "1", "APP_NON_SEC_WARMUP_STARTED": "1",
    "APP_STORE_COUNT_STARTED": "1", "APP_SCREENING_WARM_STARTED": "1",
    "DB_POOL_PREWARM": "0", "FORECAST_STORE_READONLY": "1",
    "TRANSCRIPT_NATIVE_NGRAM_SEARCH": "0",
    "FORECAST_EMAIL_TEST_MODE": "1", "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
}
if MODE == "durable_sqlite_index":
    POLICY.update(NEWS_MIRROR="1", TRANSCRIPT_LOCAL_SEARCH="1")
elif MODE == "isolated_native_snapshot":
    POLICY["TRANSCRIPT_NATIVE_NGRAM_SEARCH"] = "1"
os.environ.update(POLICY)
if os.environ.get("APP_ENV", "").upper() != "LOCAL" or os.environ.get("DEBUG", "").lower() != "true":
    raise RuntimeError("Cold audit requires the repository LOCAL/DEBUG bypass")


if not hasattr(builtins, "_ec_cold_audit"):
    state = {"lock": threading.RLock(), "calls": {}, "sql_count": 0, "violations": 0, "reruns": 0}
    builtins._ec_cold_audit = state

    def emit(name, payload):
        with state["lock"]:
            payload = {"at": time.time(), "monotonic": time.monotonic(), **payload}
            with (AUDIT / name).open("a", encoding="utf-8") as out:
                out.write(json.dumps(payload, default=str) + "\n")

    state["emit"] = emit

    def passthrough(func=None, **options):
        def decorate(fn):
            name = f"{fn.__module__}.{fn.__qualname__}"

            @functools.wraps(fn)
            def invoke(*args, **kwargs):
                with state["lock"]:
                    count = state["calls"][name] = state["calls"].get(name, 0) + 1
                emit("cache-calls.jsonl", {"function": name, "count": count, "cache": "BYPASS"})
                return fn(*args, **kwargs)

            invoke.clear = lambda *args, **kwargs: None
            return invoke

        return decorate(func) if func is not None else decorate

    passthrough.clear = lambda *args, **kwargs: None
    st.cache_data = passthrough

    if MODE == "durable_sqlite_index":
        from data import news_mirror

        class NoMemo(dict):
            def __getitem__(self, key):
                return None

            def get(self, key, default=None):
                return default

            def __setitem__(self, key, value):
                pass

        news_mirror._VOCAB = NoMemo()
        news_mirror._LIKE_IDS = NoMemo()
        news_mirror._READY = NoMemo()
        original_local_query = news_mirror.query
        original_local_ids = news_mirror.transcript_like_ids

        @functools.wraps(original_local_query)
        def audited_local_query(sql, params):
            started, wall = time.monotonic(), time.time()
            rows = original_local_query(sql, params)
            tables = sorted(set(re.findall(r"\b(?:FROM|JOIN)\s+`?([\w.]+)", str(sql), re.I)))
            projection = re.split(r"\bFROM\b", str(sql), maxsplit=1, flags=re.I)[0]
            kind = ("transcript_text" if re.search(r"\btranscript_text\b", projection, re.I)
                    else "transcript_metadata") if "coreiq_av_earnings_call_transcripts" in tables else "supporting_metadata"
            emit("local-index.jsonl", {"event": "query", "started_at": wall, "kind": kind,
                                       "tables": tables, "rows": len(rows),
                                       "elapsed_ms": (time.monotonic() - started) * 1000})
            return rows

        @functools.wraps(original_local_ids)
        def audited_local_ids(keyword):
            started, wall = time.monotonic(), time.time()
            ids = original_local_ids(keyword)
            emit("local-index.jsonl", {"event": "word_ids", "started_at": wall,
                                       "served": ids is not None, "rows": len(ids) if ids is not None else None,
                                       "elapsed_ms": (time.monotonic() - started) * 1000})
            return ids

        news_mirror.query = audited_local_query
        news_mirror.transcript_like_ids = audited_local_ids

    original_page = st.Page
    original_switch = st.switch_page

    def absolute_page(page, *args, **kwargs):
        if isinstance(page, (str, Path)) and (APP / str(page)).is_file():
            page = str(APP / str(page))
        return original_page(page, *args, **kwargs)

    def absolute_switch(page, *args, **kwargs):
        if isinstance(page, (str, Path)) and (APP / str(page)).is_file():
            page = str(APP / str(page))
        return original_switch(page, *args, **kwargs)

    st.Page = absolute_page
    st.switch_page = absolute_switch

    def sql_identity(statement):
        clean = re.sub(r"/\*.*?\*/|--[^\n]*", " ", str(statement), flags=re.S).strip()
        operation = clean.split(None, 1)[0].upper() if clean else "EMPTY"
        tables = sorted({name.replace("`", "").split(".")[-1]
                         for name in re.findall(r"\b(?:FROM|JOIN|UPDATE|INTO|TABLE)\s+((?:`?\w+`?\.)?`?\w+`?)", clean, re.I)
                         if name.replace("`", "").upper() not in {"JSON_TABLE", "JSON_EACH"}})
        allowed = operation in {"SELECT", "SHOW", "EXPLAIN", "DESCRIBE"}
        if operation == "SET":
            allowed = bool(re.match(r"SET\s+(?:NAMES\b|CHARACTER\s+SET\b|(?:SESSION\s+)?TRANSACTION\b|@@session\.|SESSION\s+(?:sql_mode|time_zone)\b|autocommit\b)", clean, re.I))
        if re.search(r"\b(?:INTO\s+(?:OUTFILE|DUMPFILE)|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)\b", clean, re.I):
            allowed = False
        if ";" in clean.rstrip(";").strip():
            allowed = False
        return operation, tables, allowed

    @event.listens_for(Engine, "before_cursor_execute")
    def before_query(conn, cursor, statement, parameters, context, executemany):
        operation, tables, allowed = sql_identity(statement)
        destination = getattr(conn.engine, "_ec_cold_destination", "live_stg_readonly")
        if MODE == "isolated_native_snapshot" and "coreiq_av_earnings_call_transcripts" in tables:
            allowed = allowed and destination == "private_native_snapshot" and set(tables) == {"coreiq_av_earnings_call_transcripts"}
        with state["lock"]:
            state["sql_count"] += 1
            counter = state["sql_count"]
            if not allowed:
                state["violations"] += 1
        context._ec_cold_query = (time.monotonic(), counter, operation, tables)
        if "coreiq_av_earnings_call_transcripts" in tables:
            projection = re.split(r"\bFROM\b", str(statement), maxsplit=1, flags=re.I)[0]
            kind = "transcript_text" if re.search(r"\btranscript_text\b", projection, re.I) else "transcript_metadata"
        else:
            kind = "supporting_metadata"
        forced_indexes = sorted({index.strip(" `")
                                 for group in re.findall(r"\bFORCE\s+INDEX\s*\(([^)]*)\)", str(statement), re.I)
                                 for index in group.split(",")})
        role = "supporting_metadata"
        if "coreiq_av_earnings_call_transcripts" in tables:
            if "idx_transcript_ngram_search" in forced_indexes:
                role = "search_candidates"
            elif re.search(r"\bLOCATE\s*\(", str(statement), re.I) or (
                    "/* EC_TRANSCRIPT_SEARCH_TEXT */" in str(statement)
                    and kind == "transcript_text"
                    and set(tables) == {"coreiq_av_earnings_call_transcripts"}):
                role = "search_text"
            elif "idx_ec_list_ordered" in forced_indexes and re.search(r"\bid\b", projection, re.I):
                role = "search_metadata"
            else:
                role = "transcript_detail" if re.search(r"\bid\b", projection, re.I) else "transcript_catalogue"
        context._ec_cold_role = role
        emit("sql.jsonl", {"event": "start", "counter": counter, "operation": operation,
                           "tables": tables, "kind": kind, "destination": destination,
                           "forced_indexes": forced_indexes, "role": role, "blocked": not allowed})
        if not allowed:
            raise RuntimeError(f"Cold audit blocked remote {operation}")

    def transcript_payload_metrics(cursor):
        rows = getattr(cursor, "_rows", None)
        columns = [column[0] for column in (cursor.description or ())]
        if rows is None or "transcript_text" not in columns:
            return {"available": False}
        index = columns.index("transcript_text")
        populated, characters, byte_count = 0, 0, 0
        for row in rows:
            value = row.get("transcript_text") if isinstance(row, dict) else row[index]
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                return {"available": False}
            populated += 1
            characters += len(value)
            byte_count += len(value.encode("utf-8"))
        return {"available": True, "rows": len(rows), "nonempty_rows": populated,
                "characters": characters, "utf8_bytes": byte_count}

    @event.listens_for(Engine, "after_cursor_execute")
    def after_query(conn, cursor, statement, parameters, context, executemany):
        started, counter, operation, tables = context._ec_cold_query
        observation = {"event": "end", "counter": counter, "operation": operation,
                       "tables": tables, "elapsed_ms": (time.monotonic() - started) * 1000,
                       "rowcount": cursor.rowcount}
        if context._ec_cold_role == "search_text":
            observation["transcript_payload"] = transcript_payload_metrics(cursor)
        emit("sql.jsonl", observation)

    @event.listens_for(Engine, "handle_error")
    def query_error(context):
        info = getattr(context.execution_context, "_ec_cold_query", None)
        if info:
            started, counter, operation, tables = info
            emit("sql.jsonl", {"event": "error", "counter": counter, "operation": operation,
                               "tables": tables, "elapsed_ms": (time.monotonic() - started) * 1000,
                               "error_type": type(context.original_exception).__name__})

    if MODE == "isolated_native_snapshot":
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL
        from sqlalchemy.pool import NullPool
        from core.database import db_manager

        private_engine = create_engine(
            URL.create("mysql+pymysql", username=os.environ.get("EC_COLD_ISOLATED_USER", "root"),
                       password=os.environ.get("EC_COLD_ISOLATED_PASSWORD") or None,
                       host="localhost", database=os.environ["EC_COLD_ISOLATED_DB"]),
            connect_args={"unix_socket": os.environ["EC_COLD_ISOLATED_SOCKET"], "charset": "utf8mb4",
                          "connect_timeout": 10, "read_timeout": 120, "write_timeout": 120},
            isolation_level="AUTOCOMMIT", skip_autocommit_rollback=True,
            poolclass=NullPool, echo=False,
        )
        private_engine._ec_cold_destination = "private_native_snapshot"
        db_manager.connect()
        remote_engine = db_manager._read_engine

        class RoutedReadConnection:
            def __init__(self, remote, private):
                self.remote, self.private = remote, private
                self.opened = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                for connection in self.opened.values():
                    connection.close()

            def execute(self, statement, parameters=None, *args, **kwargs):
                operation, tables, allowed = sql_identity(statement)
                uses_transcripts = "coreiq_av_earnings_call_transcripts" in tables
                if uses_transcripts and (set(tables) != {"coreiq_av_earnings_call_transcripts"} or not allowed):
                    with state["lock"]:
                        state["violations"] += 1
                        state["sql_count"] += 1
                        counter = state["sql_count"]
                    emit("sql.jsonl", {"event": "start", "counter": counter, "operation": operation,
                                       "tables": tables, "blocked": True,
                                       "destination": "rejected_mixed_or_mutating_transcript_query"})
                    raise RuntimeError("Isolated audit rejected a mixed-table or mutating transcript query")
                destination = "private" if uses_transcripts else "remote"
                if destination not in self.opened:
                    self.opened[destination] = getattr(self, destination).connect()
                return self.opened[destination].execute(statement, parameters or {}, *args, **kwargs)

        class RoutedReadEngine:
            def __init__(self, remote, private):
                self.remote, self.private = remote, private

            def connect(self):
                return RoutedReadConnection(self.remote, self.private)

            def dispose(self):
                self.private.dispose()
                self.remote.dispose()

            def __getattr__(self, name):
                return getattr(self.remote, name)

        db_manager._read_engine = RoutedReadEngine(remote_engine, private_engine)
        state["private_engine"] = private_engine

    (AUDIT / "policy.json").write_text(json.dumps({
        "mode": MODE, "environment": POLICY, "pid": os.getpid(), "st_cache_data": "counted passthrough",
        "transcript_origin": "private eligible-transcript corpus MySQL snapshot over Unix socket; NOT live STG" if MODE == "isolated_native_snapshot" else MODE,
        "sqlite_index": "existing durable index; no readiness/vocabulary/LIKE-ID memoization" if MODE == "durable_sqlite_index" else "disabled",
        "resource_cache": "retained for workflow resources and connection pools",
        "watchlist_result_cache": "cleared before every script rerun",
        "remote_sql": "read-only allowlist; attempted writes fail the audit",
        "database_buffers": "native MySQL/SQLite/OS buffers were not flushed",
        "authentication": "existing APP_ENV=LOCAL DEBUG=true bypass",
    }, indent=2))

state = builtins._ec_cold_audit
state["reruns"] += 1
from data import watchlist_service

watchlist_service._tables_ready = True
watchlist_service._invalidate_watchlist_cache()
state["emit"]("runs.jsonl", {"event": "start", "rerun": state["reruns"],
                              "watchlist_entries": len(watchlist_service._WL_PROCESS_CACHE)})
try:
    runpy.run_path(str(APP / "main.py"), run_name="__main__")
finally:
    state["emit"]("runs.jsonl", {"event": "exit", "rerun": state["reruns"],
                                  "sql_count": state["sql_count"], "violations": state["violations"]})
