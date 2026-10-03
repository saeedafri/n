"""Keeps persisted caches current within ~15 s of a database write, with nobody waiting.

Gate: ``information_schema.TABLES.UPDATE_TIME`` — one ~0.3 s query returns the last
committed write (INSERT, UPDATE or DELETE) of every source table. Only when a cache's
source table moved is anything else done:

  * the cache's own signature is recomputed (COUNT / MAX(id) / checksum, scoped);
  * if it changed — or the source has no content signal, so an in-place edit would not
    show in it — the cache is rebuilt on ONE background worker (rebuilds are CPU and
    GIL heavy; two at once double each other's time);
  * the disk copy is written FIRST, then the in-memory copy is cleared, so the next read
    loads the fresh disk copy in ~0.1 s. Clearing first would let a concurrent request
    rebuild it live at full cost.

UPDATE_TIME is per server and not persisted across a MySQL restart (NULL). When it is
missing the watcher falls back to a full signature pass every FRESHNESS_FALLBACK_S.

    FRESHNESS=0                  disable
    FRESHNESS_SECONDS=10         check interval
    FRESHNESS_FALLBACK_S=300     signature pass interval when UPDATE_TIME is unavailable
"""
import gc
import os
import queue
import threading
import time

from utils.server_logger import log_warning as slog_warning

_REGISTRY = {}            # name -> {"build", "sources", "clear"}
_REG_LOCK = threading.Lock()
_STARTED = False
_QUEUED = set()
_AGAIN = set()       # asked again while queued/running → rebuild once more
_WORK = queue.Queue()
_LISTENERS = []           # (tables, fn): fn(changed_tables) when any of tables moved


def _enabled():
    return os.getenv("FRESHNESS", "1").strip().lower() in ("1", "true", "yes", "on")


def _interval():
    try:
        return max(5.0, float(os.getenv("FRESHNESS_SECONDS", "10")))
    except ValueError:
        return 15.0


def register(name, build_fn, sources, clear=None, inputs=None):
    """Remember how to rebuild a persisted cache. Called on every materialized read;
    the latest build_fn / clear win (they close over the same arguments).
    `inputs`: in-memory caches the build reads — cleared before a rebuild, or the
    rebuild would copy their stale snapshot under a fresh signature."""
    if not sources:
        return
    with _REG_LOCK:
        _REGISTRY[name] = {"build": build_fn, "sources": sources, "clear": clear,
                           "inputs": inputs}


def listen(tables, fn):
    """Call fn(set_of_moved_tables) on its own thread when any of `tables` is written."""
    with _REG_LOCK:
        _LISTENERS.append((set(tables), fn))


def registered_tables():
    try:
        from utils.persist import tables_in_use
        persisted = tables_in_use()
    except Exception:
        persisted = set()
    with _REG_LOCK:
        return sorted({s["table"] for e in _REGISTRY.values() for s in e["sources"]}
                      | {t for tabs, _ in _LISTENERS for t in tabs} | persisted)


def update_times(tables):
    """{table: 'YYYY-MM-DD HH:MM:SS' or None} for the given tables, one round trip."""
    if not tables:
        return {}
    from sqlalchemy import bindparam, text
    from core.database import db_manager
    eng = db_manager._read_engine or db_manager._engine
    if eng is None:
        db_manager.execute_query_readonly_raising("SELECT 1", {})
        eng = db_manager._read_engine or db_manager._engine
    with eng.connect() as conn:
        # The server caches TABLES statistics for 24 h by default; 0 = read live.
        conn.execute(text("SET SESSION information_schema_stats_expiry = 0"))
        rows = conn.execute(
            text("SELECT TABLE_NAME, UPDATE_TIME FROM information_schema.TABLES "
                 "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN :t")
            .bindparams(bindparam("t", expanding=True)),
            {"t": list(tables)},
        ).fetchall()
    return {str(r[0]): (None if r[1] is None else str(r[1])) for r in rows}


def _release_memory():
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _enqueue(name, why):
    with _REG_LOCK:
        if name in _QUEUED:
            _AGAIN.add(name)     # the running/queued build may predate this change
            return
        _QUEUED.add(name)
    _WORK.put((name, why))


def rebuild_readers(tables):
    """Rebuild every registered cache that reads one of `tables` — used by the news
    copy once it has applied a change (a rebuild the watcher started at the same
    moment may have read the copy before it caught up)."""
    with _REG_LOCK:
        names = [n for n, e in _REGISTRY.items() if any(s["table"] in tables for s in e["sources"])]
    for name in names:
        _enqueue(name, "local copy updated")


def _rebuild_one(name, why):
    from utils import materialize as mat
    try:
        with _REG_LOCK:
            entry = _REGISTRY.get(name)
        if entry is None:
            return
        started = time.perf_counter()
        if entry.get("inputs") is not None:
            try:
                entry["inputs"]()
            except Exception:
                pass
        fresh = entry["build"]()
        mat.write_materialized(name, fresh, entry["sources"])
        del fresh
        if entry["clear"] is not None:
            try:
                entry["clear"]()
            except Exception:
                pass
        slog_warning(f"[FRESH][{name}] rebuilt in {time.perf_counter() - started:.1f}s ({why})")
    except Exception as exc:
        slog_warning(f"[FRESH][{name}] rebuild failed ({why}): {type(exc).__name__}: {str(exc)[:150]}")
    finally:
        with _REG_LOCK:
            _QUEUED.discard(name)
            again = name in _AGAIN
            _AGAIN.discard(name)
        if again:
            _enqueue(name, "changed during rebuild")
        _release_memory()


def _rebuild_worker():
    while True:
        name, why = _WORK.get()
        _rebuild_one(name, why)


def _check(names, live_times, force_signature=False):
    """Queue a rebuild for every cache whose sources moved since its disk copy."""
    from utils import materialize as mat
    for name in names:
        with _REG_LOCK:
            entry = _REGISTRY.get(name)
        if entry is None:
            continue
        meta = mat.read_meta(name) or {}
        built_times = meta.get("update_times") or {}
        moved = [s["table"] for s in entry["sources"]
                 if live_times.get(s["table"]) is None
                 or live_times.get(s["table"]) != built_times.get(s["table"])]
        if not moved and not force_signature:
            continue
        live_sig = mat._live_signature(entry["sources"], fresh=True)
        content_blind = any(s.get("signal") is None and s["table"] in moved
                            and live_times.get(s["table"]) is not None
                            for s in entry["sources"])
        if live_sig != meta.get("signature") or (content_blind and built_times):
            _enqueue(name, f"source moved: {','.join(moved) or 'signature'}")
        else:
            # Nothing this cache reads changed (e.g. a write to other tickers' rows):
            # record the new times so the next pass does not recheck it.
            mat.update_meta(name, update_times={t: live_times.get(t) for t in
                                                {s['table'] for s in entry['sources']}})


def _loop():
    interval = _interval()
    fallback = float(os.getenv("FRESHNESS_FALLBACK_S", "300"))
    last_full = 0.0
    seen = {}
    slog_warning(f"[FRESH] watcher started — every {interval:.0f}s")
    first_pass = True
    while True:
        # First pass at once: until it reports the tables' write times, persisted
        # results are served unchecked and verified in the background.
        if not first_pass:
            time.sleep(interval)
        first_pass = False
        try:
            tables = registered_tables()
            if not tables:
                continue
            live = update_times(tables)
            with _REG_LOCK:
                names = list(_REGISTRY)
            unknown = any(live.get(t) is None for t in tables)
            full = unknown and time.time() - last_full >= fallback
            if full:
                last_full = time.time()
            _check(names, live, force_signature=full)
            moved = {t for t in tables if live.get(t) is None or live.get(t) != seen.get(t)}
            first = not seen
            seen = dict(live)
            with _REG_LOCK:
                listeners = list(_LISTENERS)
            if not first:
                try:
                    from utils.persist import on_tables_moved
                    on_tables_moved(moved, live)
                except Exception:
                    pass
            else:
                try:
                    from utils.persist import on_tables_moved
                    on_tables_moved(set(), live)
                except Exception:
                    pass
            for tabs, fn in listeners:
                hit = tabs & moved
                if hit:
                    threading.Thread(target=fn, args=(None if first else hit,),
                                     name="fresh-listener", daemon=True).start()
        except Exception as exc:
            slog_warning(f"[FRESH] pass failed: {type(exc).__name__}: {str(exc)[:150]}")


def start():
    """Start the watcher and its rebuild worker once per process."""
    global _STARTED
    if not _enabled():
        return
    with _REG_LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_rebuild_worker, name="fresh-rebuild", daemon=True).start()
    threading.Thread(target=_loop, name="fresh-watch", daemon=True).start()
