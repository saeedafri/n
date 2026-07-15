"""Persistent materialization for heavy MDP data frames."""
import json
import os
import threading
import time

import pandas as pd

from core.database import db_manager
from utils.server_logger import log_warning as slog_warning

_MAT_LOCK = threading.Lock()


def _materialize_on():
    return os.getenv("PAGE_MATERIALIZE", "1").strip().lower() in ("1", "true", "yes", "on")


_CACHE_DIR = None  # memoized once resolved — the writable dir never changes per process


def _cache_dir():
    # Resolve once and cache. The old code re-probed on EVERY read/write, and the
    # write-probe used a SHARED filename (.mat_write_probe). Under the calendar's
    # ThreadPool (ma + ipo + events read concurrently) two threads raced on that one
    # file: thread B's os.remove() hit FileNotFoundError (A already deleted it) →
    # the probe raised → _cache_dir() returned None → read_materialized reported a
    # false MISS → a needless full rebuild (up to ~86s cold). Memoizing behind the
    # lock + a per-(pid,thread) probe name removes the race entirely.
    global _CACHE_DIR
    if _CACHE_DIR is not None:
        return _CACHE_DIR
    cands = []
    _env = os.getenv("MDP_CACHE_DIR", "").strip()
    if _env:
        cands.append(_env)
    cands += [
        "/home/mdp-cache",
        "/home/LogFiles/mdp",
        os.path.join(os.path.dirname(__file__), "..", "..", "server-logs", "agg-cache"),
    ]
    with _MAT_LOCK:
        if _CACHE_DIR is not None:
            return _CACHE_DIR
        for d in cands:
            try:
                os.makedirs(d, exist_ok=True)
                _probe = os.path.join(d, f".mat_write_probe.{os.getpid()}.{threading.get_ident()}")
                with open(_probe, "w") as _f:
                    _f.write("ok")
                os.remove(_probe)
                _CACHE_DIR = d
                return d
            except Exception:
                continue
    return None


def _live_signature(sources):
    if not sources:
        return []
    parts = []
    params = {}
    for _s in sources:
        _tab = _s["table"]
        _sig = _s.get("signal") or "NULL"
        # Optional "where" scopes the freshness COUNT(*) to the SUBSET this
        # cache actually depends on. Without it the signature does COUNT(*) over
        # the whole table — for coreiq_filing_metrics_v5 (12.5M rows) that was a
        # 65s full-index scan on cold buffer, run on EVERY call, and the count
        # changed on every ingest so the disk cache never hit (the 63s
        # non_sec_transcript_companies blocker, STG 03-Jul). A scoped WHERE
        # (e.g. doc_type IN (transcripts)) makes it 106 rows in ~0.3s AND
        # stable — the count only moves when the relevant subset changes.
        _where = _s.get("where")
        _from = _tab + (f" WHERE {_where}" if _where else "")
        parts.append(
            f"SELECT '{_tab}' AS t, CAST(({_sig}) AS CHAR) AS sig, COUNT(*) AS c FROM {_from}"
        )
    _sql = " UNION ALL ".join(parts)
    rows = db_manager.execute_query_readonly(_sql, params)
    _by_tab = {
        str(r["t"]): [str(r["t"]), (None if r["sig"] is None else str(r["sig"])), int(r["c"])]
        for r in rows
    }
    return [_by_tab[_s["table"]] for _s in sources]


def _fingerprint(obj):
    if isinstance(obj, pd.DataFrame):
        try:
            _h = int(pd.util.hash_pandas_object(obj, index=False).sum() & 0x7FFFFFFFFFFFFFFF)
        except Exception:
            _h = -1
        return {"kind": "df", "rows": int(len(obj)), "cols": [str(_c) for _c in obj.columns], "hash": _h}
    if isinstance(obj, (tuple, list)):
        return {"kind": "seq", "n": len(obj), "items": [_fingerprint(_x) for _x in obj]}
    return {"kind": "other", "type": type(obj).__name__}


def _total_rows(obj):
    if isinstance(obj, pd.DataFrame):
        return len(obj)
    if isinstance(obj, (tuple, list)):
        return sum(_total_rows(_x) for _x in obj)
    return 0


def read_materialized(name, sources):
    try:
        if not _materialize_on():
            return None
        _d = _cache_dir()
        if not _d:
            slog_warning(f"[MAT][{name}] MISS-CAUSE=no_cache_dir")
            return None
        _pk = os.path.join(_d, f"{name}.pkl.gz")
        _meta_p = os.path.join(_d, f"{name}.meta.json")
        if not (os.path.exists(_pk) and os.path.exists(_meta_p)):
            slog_warning(
                f"[MAT][{name}] MISS-CAUSE=file_absent dir={_d} "
                f"pk={os.path.exists(_pk)} meta={os.path.exists(_meta_p)}"
            )
            return None
        with open(_meta_p) as _f:
            _meta = json.load(_f)
        _live_sig = _live_signature(sources)
        if _meta.get("signature") != _live_sig:
            slog_warning(f"[MAT][{name}] STALE → rebuild")
            return None
        _t = time.perf_counter()
        _obj = pd.read_pickle(_pk, compression="gzip")
        _fp = _fingerprint(_obj)
        if _fp != _meta.get("fingerprint"):
            # The signature (freshness) already matched and the pickle is an
            # atomically os.replace'd, complete file — so the data IS fresh. A
            # fingerprint mismatch here means meta.json and .pkl.gz are from
            # different write generations: we read them mid-write while a
            # concurrent builder swapped the two files separately (they are
            # replaced in sequence, not atomically together). Trusting the
            # signature and using the on-disk data avoids a needless, expensive
            # rebuild — this false-MISS was the calendar's ~86s cold-load spike
            # (the earnings UNION re-ran over the wire). A genuinely corrupt
            # pickle can't reach here: it raises in read_pickle above and is
            # caught → returns None → rebuild.
            slog_warning(
                f"[MAT][{name}] fingerprint skew (concurrent write) — using fresh disk data "
                f"rows={_total_rows(_obj):,} read={time.perf_counter()-_t:.2f}s"
            )
            return _obj
        slog_warning(
            f"[MAT][{name}] HIT rows={_total_rows(_obj):,} read={time.perf_counter()-_t:.2f}s"
        )
        return _obj
    except Exception as _e:
        slog_warning(f"[MAT][{name}] read failed ({type(_e).__name__})")
        return None


def write_materialized(name, obj, sources):
    try:
        if not _materialize_on():
            return
        _d = _cache_dir()
        if not _d:
            return
        _sig = _live_signature(sources)
        _fp = _fingerprint(obj)
        with _MAT_LOCK:
            _pk = os.path.join(_d, f"{name}.pkl.gz")
            _meta_p = os.path.join(_d, f"{name}.meta.json")
            _tmp_pk, _tmp_meta = _pk + ".tmp", _meta_p + ".tmp"
            pd.to_pickle(obj, _tmp_pk, compression={"method": "gzip", "compresslevel": 1})
            with open(_tmp_meta, "w") as _f:
                json.dump({"signature": _sig, "fingerprint": _fp}, _f)
            os.replace(_tmp_pk, _pk)
            os.replace(_tmp_meta, _meta_p)
            try:
                import ctypes as _ct
                _ct.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
            slog_warning(f"[MAT][{name}] WROTE rows={_total_rows(obj):,}")
    except Exception as _e:
        slog_warning(f"[MAT][{name}] write failed: {type(_e).__name__}")


def materialized_or_build(name, build_fn, sources):
    if not _materialize_on():
        return build_fn()
    _obj = read_materialized(name, sources)
    if _obj is not None:
        return _obj
    slog_warning(f"[MAT][{name}] MISS → live build")
    _obj = build_fn()
    write_materialized(name, _obj, sources)
    return _obj
