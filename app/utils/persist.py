"""Disk-persisted results for per-company functions, kept fresh by table write times.

Why: per-company data (statements, key stats, segments, estimates, transcripts list,
filing metadata…) lived only in process memory, so after every deploy the first
viewer of each company paid 2-14 s of database round trips. This layer keeps each
result on the persistent cache dir (/home/mdp-cache on Azure — survives deploys).

Use it UNDER @st.cache_data, so memory stays the fast path and this is the miss path:

    @staticmethod
    @st.cache_data(ttl=21600, show_spinner=False)
    @persistent("key_stats")
    def get_key_stats_data(ticker, ...): ...

FRESHNESS — no per-read database query:
  * While a result is built, every SQL statement it runs is recorded and the
    `coreiq_*` tables it read are stored with it, plus each table's last-write time
    (information_schema UPDATE_TIME) at build time.
  * A stored result is current while none of its tables has been written since.
  * The freshness watcher (utils/freshness.py) sees writes within ~15 s. Results of
    the moved tables that were used recently are rebuilt in the background at once;
    then the in-memory copy is cleared so the next read loads the new disk copy.
  * A result read after its tables moved (not rebuilt yet) is returned immediately
    and rebuilt in the background (stale-while-revalidate): nobody waits on it.
  * A build that fails never replaces a stored result.

    PERSIST=0          disable (plain function)
    PERSIST_VERSION=n  change to drop every stored result (e.g. after a helper's logic changed)
"""
import functools
import gzip
import hashlib
import json
import os
import pickle
import queue
import re
import threading
import time
from collections import OrderedDict

from utils.server_logger import log_warning as slog_warning

_REC = threading.local()
_TABLE_RE = re.compile(r"\b(coreiq_\w+|filing_llm_cache)\b", re.I)
_LOCK = threading.Lock()
_FUNCS = {}                  # name -> {"fn", "ram_clear", "store_if"}
_NAME_TABLES = {}            # name -> tables its builds have read (incl. what it calls)
_DEPS = {}                   # name -> persisted names its source calls
_RECENT = OrderedDict()      # (name, key) -> (args, kwargs, tables)  — recently read
_RECENT_MAX = int(os.getenv("PERSIST_RECENT_MAX", "2000"))
_TIMES = {}                  # table -> last UPDATE_TIME seen by the watcher
_VERIFIED = {}               # (name, key) -> {table: write time} checked unaffected since build
# Tables an ingest writes all day (v5 every ~5 s): a write anywhere in them must not
# rebuild every stored result that reads them. For these the watcher reads which
# tickers got NEW ids since its last look (primary-key range) and rebuilds only the
# results whose arguments name one of them. In-place edits/deletes leave no new id:
# every SCOPED_FULL_S all results of the table are rebuilt (ponytail: id watermark,
# switch to a change-log table if the data team adds one).
SCOPED = {"coreiq_filing_metrics_v5": ("id", "ticker"),
          "coreiq_company_events": ("event_id", "ticker")}
SCOPED_FULL_S = float(os.getenv("PERSIST_SCOPED_FULL_S", "1800"))
_WATERMARK = {}              # table -> (max id seen, last full pass monotonic)
_TIMES_AT = {}               # table -> when that value was read (monotonic)
_TIMES_MAX_AGE = float(os.getenv("PERSIST_TIMES_MAX_AGE_S", "30"))
_WORK = queue.Queue()
_QUEUED = set()
_AGAIN = set()               # asked again while queued/running → rebuild once more
_WORKER = None


def enabled():
    return os.getenv("PERSIST", "1").strip().lower() in ("1", "true", "yes", "on")


# ── statement recording (called from core.database before every execute) ──
def record(statement):
    tabs = getattr(_REC, "tables", None)
    if tabs is not None:
        tabs.update(m.lower() for m in _TABLE_RE.findall(str(statement)))


def _build_recording(fn, args, kwargs):
    outer = getattr(_REC, "tables", None)
    _REC.tables = set()
    try:
        value = fn(*args, **kwargs)
        return value, set(_REC.tables)
    finally:
        mine = _REC.tables
        _REC.tables = outer
        if outer is not None:
            outer.update(mine)   # a persisted call inside another build still counts


# ── storage ────────────────────────────────────────────────────────────────
def _dir(name):
    from utils.materialize import _cache_dir
    d = _cache_dir()
    if not d:
        return None
    p = os.path.join(d, "persist", name)
    os.makedirs(p, exist_ok=True)
    return p


def _code_version(fn):
    """Hash of fn's own source: a deploy that changes the function must not be served
    results the old code produced (disk copies outlive deploys). Hashing the whole
    file instead dropped every stored result on every deploy that touched any other
    function in it (most deploys). A change only in a helper fn calls is not seen —
    set PERSIST_VERSION to a new value to drop everything stored."""
    import inspect
    try:
        src = inspect.getsource(fn)
    except Exception:
        src = getattr(fn, "__qualname__", "unknown")
    raw = f"{os.getenv('PERSIST_VERSION', '1')}|{src}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _key(args, kwargs, version=""):
    raw = repr((version, args, sorted(kwargs.items())))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _paths(name, key):
    d = _dir(name)
    return (None, None) if not d else (os.path.join(d, key + ".pkl.gz"), os.path.join(d, key + ".json"))


def _load(name, key):
    pk, mp = _paths(name, key)
    if not pk or not os.path.exists(mp) or not os.path.exists(pk):
        return None
    try:
        with open(mp) as f:
            meta = json.load(f)
        with gzip.open(pk, "rb") as f:
            return pickle.load(f), meta
    except Exception:
        return None


def _save(name, key, value, tables, times):
    pk, mp = _paths(name, key)
    if not pk:
        return
    tmp_pk, tmp_mp = f"{pk}.tmp{threading.get_ident()}", f"{mp}.tmp{threading.get_ident()}"
    with gzip.open(tmp_pk, "wb", compresslevel=1) as f:
        pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
    with open(tmp_mp, "w") as f:
        json.dump({"tables": sorted(tables), "update_times": times, "built": time.time()}, f)
    os.replace(tmp_pk, pk)
    os.replace(tmp_mp, mp)


def _note_times(times):
    now = time.monotonic()
    with _LOCK:
        for k, v in times.items():
            _TIMES[k] = v
            _TIMES_AT[k] = now


def _live_times(tables):
    """Last-write times: the watcher's view (refreshed every ~15 s), re-read here only
    if older than _TIMES_MAX_AGE — so a stalled watcher can never freeze results."""
    now = time.monotonic()
    with _LOCK:
        known = {t: _TIMES[t] for t in tables
                 if t in _TIMES and now - _TIMES_AT.get(t, 0) < _TIMES_MAX_AGE}
    missing = [t for t in tables if t not in known]
    if missing:
        try:
            from utils.freshness import update_times
            got = update_times(missing)
            _note_times(got)
            known.update(got)
        except Exception:
            pass
    return known


def _scope(args, kwargs):
    """Ticker-like arguments of a call (its rows in a SCOPED table)."""
    return {str(v).strip().upper() for v in list(args) + list(kwargs.values())
            if isinstance(v, str) and 0 < len(v.strip()) <= 12}


def _deps(name):
    """Persisted functions `name` calls — directly or through helper methods of its
    own class. An inner call answered by its cache runs no SQL, so the outer build
    cannot see those tables; it inherits them from here instead. Reading only the
    function's own source missed them behind a helper: the Segments tables reach
    their filing rows through _build_segment_tables_from_db, so they were never
    tied to coreiq_filing_metrics_v5 and a data load did not rebuild them.
    (Same-named methods over-match: harmless, only extra rebuilds.)"""
    if name in _DEPS:
        return _DEPS[name]
    import inspect
    import sys
    with _LOCK:
        funcs = dict(_FUNCS)
    persisted = {f["fn"].__name__: n for n, f in funcs.items() if n != name}
    start = funcs[name]["fn"]
    owner = getattr(sys.modules.get(start.__module__), start.__qualname__.split(".")[0], None)
    found, seen, todo = set(), set(), [start]
    while todo:
        fn = todo.pop()
        try:
            src = inspect.getsource(fn).split(":", 1)[-1]
        except Exception:
            continue
        for called in set(re.findall(r"\b(\w+)\s*\(", src)):
            if called in persisted:
                found.add(persisted[called])
            elif owner is not None and called not in seen and isinstance(owner, type):
                helper = owner.__dict__.get(called)
                helper = getattr(helper, "__func__", helper)
                if callable(helper):
                    seen.add(called)
                    todo.append(inspect.unwrap(helper))
    _DEPS[name] = found
    return found


def _name_tables(name):
    t = _NAME_TABLES.get(name)
    if t is None:
        d = _dir(name)
        try:
            with open(os.path.join(d, "_tables.json")) as f:
                t = set(json.load(f))
        except Exception:
            t = set()
        _NAME_TABLES[name] = t
    return t


def _all_tables(name, recorded):
    """What a build of `name` depends on: tables it read + those of what it calls."""
    tables = set(recorded)
    for d in _deps(name):
        tables |= _name_tables(d)
    known = _name_tables(name)
    if not tables <= known:
        known |= tables
        try:
            d = _dir(name)
            with open(os.path.join(d, "_tables.json.tmp"), "w") as f:
                json.dump(sorted(known), f)
            os.replace(os.path.join(d, "_tables.json.tmp"), os.path.join(d, "_tables.json"))
        except Exception:
            pass
    return tables


def _current(meta, wait=True, verified=None):
    """True if no table the result read was written since it was built. None (with
    wait=False) when the watcher has not reported a table's time yet — the caller
    then serves the stored copy and has it checked in the background, instead of
    paying the first database round trip of the process on a page request."""
    tables = meta.get("tables") or []
    built = meta.get("update_times") or {}
    if not tables:
        return False          # nothing tracked → can't be known current
    if not wait:
        now = time.monotonic()
        with _LOCK:
            if any(t not in _TIMES or now - _TIMES_AT.get(t, 0) >= _TIMES_MAX_AGE for t in tables):
                return None
    live = _live_times(tables)
    verified = verified or {}
    return all(live.get(t) is not None and live.get(t) in (built.get(t), verified.get(t)) for t in tables)


def _empty(value):
    """A failed query in this codebase usually returns [] / None rather than raising,
    so an empty result is never stored: it would hide real data until the next write."""
    if value is None or value is False:
        return True
    try:
        import pandas as pd
        if isinstance(value, pd.DataFrame):
            return value.empty
    except Exception:
        pass
    try:
        return len(value) == 0
    except TypeError:
        return False


# ── background rebuilds ─────────────────────────────────────────────────────
def _rebuild(name, key, args, kwargs, check_only=False):
    with _LOCK:
        f = _FUNCS.get(name)
    if f is None:
        return
    started = time.perf_counter()
    try:
        old = _load(name, key)
        if check_only and old is not None and _current(old[1], verified=_VERIFIED.get((name, key))):
            return
        # Times BEFORE the build: a write landing during it makes the next check
        # see a newer time and rebuild again instead of missing that write.
        value, tables = _build_recording(f["fn"], args, kwargs)
        tables = _all_tables(name, tables)
        if not tables:
            pk, mp = _paths(name, key)
            for path in (pk, mp):
                try:
                    os.remove(path)
                except Exception:
                    pass
            return                # depends on no table: nothing to keep fresh
        rejected = f.get("store_if") is not None and not f["store_if"](value)
        if (_empty(value) or rejected) and old is not None and not _empty(old[0]):
            slog_warning(f"[PERSIST][{name}] rebuild returned nothing; keeping the stored copy")
            return
        if rejected:
            return
        times = _live_times(sorted(tables))
        _save(name, key, value, tables, times)
        with _LOCK:
            _VERIFIED.pop((name, key), None)
        _confirm(name, key, tables, times)
        changed = old is None or pickle.dumps(old[0]) != pickle.dumps(value)
        if changed and f["ram_clear"] is not None:
            try:
                f["ram_clear"]()
            except Exception:
                pass
        slog_warning(f"[PERSIST][{name}] rebuilt in {time.perf_counter() - started:.1f}s "
                     f"changed={changed} queued={_WORK.qsize()}")
        with _LOCK:
            if (name, key) in _RECENT:
                _RECENT[(name, key)] = (args, kwargs, tables)
            users = [(n, k, a, kw) for (n, k), (a, kw, _t) in _RECENT.items()
                     if changed and name in _DEPS.get(n, ())]
        for n, k, a, kw in users:
            _enqueue(n, k, a, kw)     # built from this value: rebuild after it
    except Exception as exc:
        slog_warning(f"[PERSIST][{name}] rebuild failed: {type(exc).__name__}: {str(exc)[:150]}")


def _worker_loop():
    while True:
        name, key, args, kwargs, check_only = _WORK.get()
        try:
            _rebuild(name, key, args, kwargs, check_only)
        finally:
            with _LOCK:
                _QUEUED.discard((name, key))
                again = (name, key) in _AGAIN
                _AGAIN.discard((name, key))
            if again:
                _enqueue(name, key, args, kwargs)       # a forced rebuild
            time.sleep(0.05)   # leave room for user requests between rebuilds


def _enqueue(name, key, args, kwargs, check_only=False):
    """Queue a background rebuild; `check_only` rebuilds only if a table moved."""
    global _WORKER
    with _LOCK:
        if (name, key) in _QUEUED:
            # Already queued or being rebuilt: that rebuild may have read the data
            # before this change landed, so run it once more afterwards.
            if not check_only:
                _AGAIN.add((name, key))
            return
        _QUEUED.add((name, key))
        if _WORKER is None:
            _WORKER = threading.Thread(target=_worker_loop, name="persist-rebuild", daemon=True)
            _WORKER.start()
    _WORK.put((name, key, args, kwargs, check_only))


def _remember(name, key, args, kwargs, tables):
    with _LOCK:
        _RECENT[(name, key)] = (args, kwargs, set(tables))
        _RECENT.move_to_end((name, key))
        while len(_RECENT) > _RECENT_MAX:
            _RECENT.popitem(last=False)


def tables_in_use():
    with _LOCK:
        return {t for _, _, tabs in _RECENT.values() for t in tabs}


def _moved_tickers(table):
    """Tickers with new ids in `table` since the last call; None = rebuild all of it
    (first look after a full-pass interval, or the probe failed)."""
    id_col, tk_col = SCOPED[table]
    from core.database import db_manager
    now = time.monotonic()
    try:
        hi = db_manager.execute_query_readonly_raising(f"SELECT MAX({id_col}) AS m FROM {table}", {})[0]["m"] or 0
    except Exception:
        return None
    with _LOCK:
        seen, last_full = _WATERMARK.get(table, (None, now))
        _WATERMARK[table] = (hi, last_full if seen is not None else now)
    if seen is None:
        return None                      # first look: no watermark yet → rebuild
    if now - last_full >= SCOPED_FULL_S:
        with _LOCK:
            _WATERMARK[table] = (hi, now)
        return None
    if hi <= seen:
        return set()
    try:
        rows = db_manager.execute_query_readonly_raising(
            f"SELECT DISTINCT {tk_col} AS t FROM {table} WHERE {id_col} > :lo AND {id_col} <= :hi",
            {"lo": seen, "hi": hi})
    except Exception:
        return None
    return {str(r["t"]).strip().upper() for r in rows if r.get("t")}


def _confirm(name, key, tables, times):
    """Record that (name, key) is exact as of these write times."""
    with _LOCK:
        v = _VERIFIED.setdefault((name, key), {})
        for t in tables:
            if times.get(t) is not None:
                v[t] = times[t]


def on_tables_moved(moved, live_times=None):
    """Watcher callback: rebuild recently used results that read a moved table."""
    with _LOCK:
        before = dict(_TIMES)
    if live_times:
        _note_times(live_times)
    if not moved:
        return
    tickers = {t: _moved_tickers(t) for t in moved if t in SCOPED}
    with _LOCK:
        items = list(_RECENT.items())
    for (n, k), (a, kw, tabs) in items:
        hit = tabs & moved
        if not hit:
            continue
        rebuild = False
        for t in hit:
            changed = tickers.get(t) if t in SCOPED else None
            if changed is None or not _scope(a, kw) or (_scope(a, kw) & changed):
                rebuild = True
        with _LOCK:
            confirmed = _VERIFIED.get((n, k), {})
            exact_before = all(confirmed.get(t) is not None and confirmed.get(t) == before.get(t) for t in hit)
        if rebuild or not exact_before or not live_times:
            _enqueue(n, k, a, kw)
        else:
            # Only other tickers' rows were added, and it was exact before: still exact.
            _confirm(n, k, hit, live_times)


# ── decorator ───────────────────────────────────────────────────────────────
def persistent(name, daily=False, strict=False, store_if=None):
    """Persist a pure function's result per argument tuple (see module docstring).
    The function must return picklable data and depend only on its arguments and
    the database. `daily=True` for functions that read "today" (CURDATE(), now()):
    the date is part of the key, so a new day never serves yesterday's window.
    `strict=True` for functions other caches are BUILT from: once their tables moved
    they rebuild before answering (a stale answer would be baked into that build).
    `store_if(value)`: only such values are stored — for functions whose failure
    result is not empty (e.g. a status dict saying "nothing there")."""
    def deco(fn):
        version = _code_version(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not enabled():
                return fn(*args, **kwargs)
            key = _key(args + ((time.strftime("%Y-%m-%d"),) if daily else ()), kwargs, version)
            hit = _load(name, key)
            if hit is not None:
                value, meta = hit
                _remember(name, key, args, kwargs, meta.get("tables") or [])
                fresh = _current(meta, wait=strict, verified=_VERIFIED.get((name, key)))
                if fresh:
                    with _LOCK:
                        live = {t: _TIMES.get(t) for t in meta.get("tables") or []}
                    _confirm(name, key, meta.get("tables") or [], live)
                    return value
                if not strict:
                    # serve now; refresh behind (or just check, if times unknown yet)
                    _enqueue(name, key, args, kwargs, check_only=fresh is None)
                    return value
                # strict: fall through to a synchronous rebuild
            times_before = None
            value, tables = _build_recording(fn, args, kwargs)
            tables = _all_tables(name, tables)
            if _empty(value) or not tables or (store_if is not None and not store_if(value)):
                return value          # not stored: see _empty; no table = nothing to track
            try:
                times_before = _live_times(sorted(tables))
                _save(name, key, value, tables, times_before)
                _confirm(name, key, tables, times_before)
            except Exception as exc:
                slog_warning(f"[PERSIST][{name}] save failed: {type(exc).__name__}: {str(exc)[:120]}")
            _remember(name, key, args, kwargs, tables)
            return value
        with _LOCK:
            _FUNCS[name] = {"fn": fn, "ram_clear": None, "store_if": store_if}
        wrapper._persist_name = name
        return wrapper
    return deco


def bind_ram_clear(name, clear):
    """Tell the layer which in-memory cache sits in front of `name`."""
    with _LOCK:
        if name in _FUNCS:
            _FUNCS[name]["ram_clear"] = clear
