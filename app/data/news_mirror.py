"""Local copy of the two news tables, so the Newsroom feed and keyword search answer
in milliseconds instead of 1-16 s per query over the database link.

WHAT IT HOLDS
    A SQLite file on the persistent cache dir (/home/mdp-cache on Azure — survives
    deploys) with exactly the columns the Newsroom reads from
    coreiq_av_market_news_sentiment, coreiq_yf_market_news_sentiment and
    coreiq_av_companies_all (symbol, sector), plus `norm_title`: the title lower-cased
    with accents stripped, because MySQL's utf8mb4_0900_ai_ci compares titles that way
    and a plain SQLite LIKE would not ("café" must match "cafe").

HOW IT STAYS EXACT
    * A table is served from the copy only once the copy is COMPLETE (the first full
      copy runs in the background; until then every query goes to MySQL as before).
    * New rows (id above the copied maximum) are pulled on every freshness pass.
    * An in-place UPDATE or a DELETE moves the table's write time without a new id:
      the copy then compares COUNT + checksum per block of ids with MySQL and re-copies
      only the blocks that differ (SIP's per-slice approach).
    * Any error in the copy path falls back to MySQL for that query.

The SQL the repository already runs is executed unchanged against the copy, apart from
MySQL-only syntax (index hints, MATCH…AGAINST, GROUP_CONCAT…SEPARATOR), see _to_sqlite.

    NEWS_MIRROR=0     disable (always MySQL)
"""
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from datetime import date, datetime

from utils.server_logger import log_warning as slog_warning

TABLES = {
    "av": {
        "source": "coreiq_av_market_news_sentiment",
        "ts": "time_published_utc",
        "columns": ["id", "time_published_utc", "ticker", "title", "summary", "url",
                    "source_name", "source_domain", "ticker_sentiment_json", "topics_json"],
        # (time, norm_title): the keyword search tests the title inside the index
        # instead of reading every wide row — a 5-year no-hit search 1.4 s → 0.18 s.
        "indexes": ["time_published_utc, id", "ticker, time_published_utc", "time_published_utc, norm_title"],
    },
    "yf": {
        "source": "coreiq_yf_market_news_sentiment",
        "ts": "published_at",
        "columns": ["id", "news_id", "published_at", "ticker", "title", "publisher", "link",
                    "primary_topic_v1", "primary_topic_v2"],
        "indexes": ["published_at, id", "news_id", "published_at, norm_title"],
    },
    "tx": {
        "source": "coreiq_av_earnings_call_transcripts",
        "ts": "event_datetime_utc",
        "columns": ["id", "source", "ticker", "quarter", "year", "q", "transcript_text",
                    "has_transcript", "title", "event_datetime_utc", "fetched_at_utc", "raw_json_sha1"],
        "indexes": ["ticker, year, q", "has_transcript, ticker"],
        # Re-hashing ~2 GB of transcript text per check is too heavy; the table keeps
        # its own content hash (raw_json_sha1) and the text length moves on any edit.
        "checksum": ["source", "ticker", "quarter", "year", "q", "has_transcript", "title",
                     "event_datetime_utc", "fetched_at_utc", "raw_json_sha1",
                     "CHAR_LENGTH(transcript_text)"],
        "batch": 500,
        "block": 2000,     # ~450 transcripts: one checksum query stays well under the read timeout
        # Transcripts are re-fetched IN PLACE (~1,700 old ids a day), which a check of
        # the newest blocks never sees; every re-fetch stamps fetched_at_utc.
        "changed_col": "fetched_at_utc",
    },
}
COMPANIES = {"source": "coreiq_av_companies_all", "columns": ["symbol", "sector"]}
BATCH = 20000          # rows per copy round trip
BLOCK = 10000          # ids per reconciliation block (a 10k block checks in ~0.4 s)
HOT_BLOCKS = int(os.getenv("NEWS_MIRROR_HOT_BLOCKS", "2"))   # newest blocks checked on every write (~13 days of AV)
RESTORE_HOT_BLOCKS = 5                                          # …and once after a restore
# Full checksum sweep: a whole-table scan on MySQL, so rare and throttled — recent
# edits are caught every cycle by the hot-block check / changed_col instead.
RECONCILE_EVERY_S = float(os.getenv("NEWS_MIRROR_RECONCILE_S", "21600"))
SWEEP_DUTY = float(os.getenv("NEWS_MIRROR_SWEEP_DUTY", "0.25"))     # max share of time spent querying
SNAPSHOT_EVERY_S = float(os.getenv("NEWS_MIRROR_SNAPSHOT_S", "21600"))
_SNAPSHOT_LOCK = threading.Lock()
_BLOCK_LOCK = threading.Lock()     # one block re-copy at a time (sweep vs hot check)
_SWEEP_LOCK = threading.Lock()     # one full sweep at a time, across tables
_SWEEP_QUEUE = []                  # keys waiting for a sweep
_LOCAL = threading.local()
_STATE_LOCK = threading.Lock()
_READY = {}            # key -> bool (complete copy)
_SYNC_LOCK = threading.Lock()
_WANT = {"run": False, "all": False, "reconcile": False, "tables": set()}   # queued sync request
_ON_CHANGE = {}        # name -> (keys, fn): in-memory caches to clear when the copy changes
_STARTED = False


def enabled():
    return os.getenv("NEWS_MIRROR", "1").strip().lower() in ("1", "true", "yes", "on")


def normalize(text):
    """Lower-case and strip accents, the way utf8mb4_0900_ai_ci compares."""
    if text is None:
        return None
    s = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _snapshot_path():
    from utils.materialize import _cache_dir
    d = _cache_dir()
    return os.path.join(d, "news_mirror.sqlite") if d else None


def _path():
    """The working copy. On Azure the cache dir is /home — an SMB share, where SQLite's
    WAL locking does not work and every page read is a network trip — so the working
    copy lives on local disk and /home only keeps a snapshot that survives deploys."""
    snap = _snapshot_path()
    local = os.getenv("NEWS_MIRROR_DIR", "").strip()
    if not local:
        if not (snap and snap.startswith("/home/")):
            return snap
        local = "/tmp/mdp-news"
    os.makedirs(local, exist_ok=True)
    return os.path.join(local, "news_mirror.sqlite")


def _restore_snapshot():
    """First sync after a deploy/restart: start from the /home snapshot instead of a
    40-minute full copy. Rows changed since the snapshot are unknown, so each table is
    marked for a full checksum pass before it may be served (see ready())."""
    work, snap = _path(), _snapshot_path()
    if not snap or work == snap or os.path.exists(work) or not os.path.exists(snap):
        return False
    started = time.perf_counter()
    tmp = work + ".restore"
    shutil.copyfile(snap, tmp)
    os.replace(tmp, work)
    c = _conn()
    _schema(c)
    for key in TABLES:
        _set_state(c, f"{key}:verify", "1")
    c.commit()
    _VOCAB["terms"] = None
    slog_warning(f"[NEWS_MIRROR] restored snapshot in {time.perf_counter() - started:.0f}s; verifying")
    return True


def _write_snapshot():
    """Consistent copy of the working file onto the persistent dir (backup API → local
    temp → one sequential write to /home → atomic rename)."""
    work, snap = _path(), _snapshot_path()
    if not snap or work == snap:
        return
    started = time.perf_counter()
    local_tmp, snap_tmp = work + ".snap", snap + ".tmp"
    src, dst = sqlite3.connect(work), sqlite3.connect(local_tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    shutil.copyfile(local_tmp, snap_tmp)
    os.replace(snap_tmp, snap)
    os.remove(local_tmp)
    slog_warning(f"[NEWS_MIRROR] snapshot written in {time.perf_counter() - started:.0f}s")


def _conn():
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        p = _path()
        if not p:
            raise RuntimeError("no cache dir")
        c = sqlite3.connect(p, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA mmap_size=268435456")
        # INSERT OR REPLACE must fire the DELETE trigger that keeps tx_words in step.
        c.execute("PRAGMA recursive_triggers=ON")
        _LOCAL.conn = c
    return c


def _schema(c):
    for key, t in TABLES.items():
        cols = ", ".join(("id INTEGER PRIMARY KEY" if col == "id" else f"{col}")
                         for col in t["columns"]) + ", norm_title TEXT"
        c.execute(f"CREATE TABLE IF NOT EXISTS {t['source']} ({cols})")
        for i, ix in enumerate(t["indexes"]):
            c.execute(f"CREATE INDEX IF NOT EXISTS ix_{key}_{i} ON {t['source']} ({ix})")
    c.execute(f"CREATE TABLE IF NOT EXISTS {COMPANIES['source']} (symbol TEXT, sector TEXT)")
    c.execute(f"CREATE INDEX IF NOT EXISTS ix_avc_symbol ON {COMPANIES['source']} (symbol)")
    c.execute("CREATE TABLE IF NOT EXISTS mirror_state (k TEXT PRIMARY KEY, v TEXT)")
    _word_index(c)
    c.commit()


def _word_index(c):
    """tx_words: every word of every transcript (unicode61, accents removed, case-folded
    — the transcript column's utf8mb4_0900_ai_ci comparison), kept in step with the
    table by triggers. Backs transcript_like_ids()."""
    src = TABLES["tx"]["source"]
    c.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS tx_words USING fts5(transcript_text,
                  content='{src}', content_rowid='id', tokenize="unicode61 remove_diacritics 2", detail=none)""")
    c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS tx_vocab USING fts5vocab(tx_words, 'row')")
    c.execute(f"""CREATE TRIGGER IF NOT EXISTS tx_words_ai AFTER INSERT ON {src} BEGIN
                  INSERT INTO tx_words(rowid, transcript_text) VALUES (new.id, new.transcript_text); END""")
    c.execute(f"""CREATE TRIGGER IF NOT EXISTS tx_words_ad AFTER DELETE ON {src} BEGIN
                  INSERT INTO tx_words(tx_words, rowid, transcript_text) VALUES ('delete', old.id, old.transcript_text); END""")
    c.execute(f"""CREATE TRIGGER IF NOT EXISTS tx_words_au AFTER UPDATE ON {src} BEGIN
                  INSERT INTO tx_words(tx_words, rowid, transcript_text) VALUES ('delete', old.id, old.transcript_text);
                  INSERT INTO tx_words(rowid, transcript_text) VALUES (new.id, new.transcript_text); END""")
    if _get_state(c, "tx:words") != "1":
        c.execute("INSERT INTO tx_words(tx_words) VALUES ('rebuild')")
        _set_state(c, "tx:words", "1")


def _get_state(c, k, default=None):
    r = c.execute("SELECT v FROM mirror_state WHERE k = ?", (k,)).fetchone()
    return r[0] if r else default


def _set_state(c, k, v):
    c.execute("INSERT INTO mirror_state(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
              (k, None if v is None else str(v)))


def ready(key):
    """True once `key` ('av' / 'yf') is a complete copy and safe to serve from."""
    if not enabled():
        return False
    with _STATE_LOCK:
        if key in _READY:
            return _READY[key]
    try:
        # No _schema() here: a new index on a 4 GB copy takes ~10 s to build and
        # this runs on a page request. sync() builds it in the background.
        p = _path()
        if not p or not os.path.exists(p):
            return False     # connecting would create an empty file and block the restore
        c = _conn()
        ok = (_get_state(c, f"{key}:complete") == "1" and _get_state(c, "companies:complete") == "1"
              and _get_state(c, f"{key}:verify") != "1")
    except Exception:
        ok = False
    with _STATE_LOCK:
        _READY[key] = ok
    return ok


# ── copying ────────────────────────────────────────────────────────────────
def _db_rows(sql, params):
    from core.database import db_manager
    return db_manager.execute_query_readonly_raising(sql, params)


def _sqlite_value(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v


def _insert(c, key, rows, replace=True):
    t = TABLES[key]
    cols = t["columns"] + ["norm_title"]
    marks = ", ".join("?" for _ in cols)
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    c.executemany(f"{verb} INTO {t['source']} ({', '.join(cols)}) VALUES ({marks})",
                  [tuple(_sqlite_value(r.get(col)) for col in t["columns"]) + (normalize(r.get("title")),)
                   for r in rows])


def _copy_range(c, key, where, params, order, progress=None):
    """Copy rows matching `where` in id order, BATCH at a time. Returns rows copied.
    `progress`: state key that records the last id copied, so a copy interrupted by a
    restart resumes where it stopped."""
    t = TABLES[key]
    sel = ", ".join(t["columns"])
    done = 0
    cursor_id = params.pop("_start")
    while True:
        cmp = ">" if order == "ASC" else "<"
        batch = t.get("batch", BATCH)
        rows = _db_rows(f"SELECT {sel} FROM {t['source']} WHERE id {cmp} :cur {where} "
                        f"ORDER BY id {order} LIMIT {batch}", {**params, "cur": cursor_id})
        if not rows:
            return done
        _insert(c, key, rows)
        done += len(rows)
        cursor_id = rows[-1]["id"]
        if progress:
            _set_state(c, progress, cursor_id)
        c.commit()
        if len(rows) < batch:
            return done


def _copy_companies(c):
    """Replace the (symbol, sector) copy. True if its content changed."""
    rows = sorted((r.get("symbol"), r.get("sector")) for r in
                  _db_rows(f"SELECT symbol, sector FROM {COMPANIES['source']}", {}))
    old = sorted(tuple(r) for r in c.execute(f"SELECT symbol, sector FROM {COMPANIES['source']}"))
    if rows == old and _get_state(c, "companies:complete") == "1":
        return False
    c.execute(f"DELETE FROM {COMPANIES['source']}")
    c.executemany(f"INSERT INTO {COMPANIES['source']} (symbol, sector) VALUES (?, ?)", rows)
    _set_state(c, "companies:complete", "1")
    c.commit()
    return True


def _full_copy(c, key):
    """First copy: newest ids first so the recent range is usable soonest is not
    needed (we serve only when complete), so copy ascending — resumable via state."""
    t = TABLES[key]
    started = time.perf_counter()
    top = _db_rows(f"SELECT MAX(id) AS mx FROM {t['source']}", {})[0]["mx"] or 0
    resume = int(_get_state(c, f"{key}:copied_to", 0) or 0)
    n = _copy_range(c, key, "AND id <= :top", {"_start": resume, "top": top}, "ASC",
                    progress=f"{key}:copied_to")
    _set_state(c, f"{key}:copied_to", top)
    _set_state(c, f"{key}:max_id", top)
    _set_state(c, f"{key}:complete", "1")
    c.commit()
    slog_warning(f"[NEWS_MIRROR][{key}] full copy {n:,} rows to id {top} in {time.perf_counter() - started:.0f}s")


def _tail(c, key):
    """Pull rows newer than the copied maximum. Returns rows pulled."""
    hi = int(_get_state(c, f"{key}:max_id", 0) or 0)
    n = _copy_range(c, key, "", {"_start": hi}, "ASC")
    if n:
        mx = c.execute(f"SELECT MAX(id) FROM {TABLES[key]['source']}").fetchone()[0] or hi
        _set_state(c, f"{key}:max_id", mx)
        c.commit()
    return n


def _pull_changed(c, key):
    """Re-copy rows whose changed_col is at or after the copy's newest value (rows
    edited in place since the last pass). Returns rows re-copied."""
    col = TABLES[key].get("changed_col")
    if not col:
        return 0
    t = TABLES[key]
    since = c.execute(f"SELECT MAX({col}) FROM {t['source']}").fetchone()[0]
    if not since:
        return 0
    ids = [r["id"] for r in _db_rows(f"SELECT id FROM {t['source']} WHERE {col} >= :since", {"since": since})]
    hi = int(_get_state(c, f"{key}:max_id", 0) or 0)
    ids = [i for i in ids if i <= hi]          # above hi: _tail's job
    sel = ", ".join(t["columns"])
    changed = 0
    for n in range(0, len(ids), 200):
        part = ids[n:n + 200]
        marks = ", ".join(f":i{k}" for k in range(len(part)))
        rows = _db_rows(f"SELECT {sel} FROM {t['source']} WHERE id IN ({marks})",
                        {f"i{k}": v for k, v in enumerate(part)})
        local = {r[0]: r[1] for r in c.execute(
            f"SELECT id, {col} FROM {t['source']} WHERE id IN ({', '.join('?' * len(part))})", part)}
        rows = [r for r in rows if local.get(r["id"]) != _sqlite_value(r.get(col))]
        if rows:
            with _BLOCK_LOCK:
                _insert(c, key, rows)
                c.commit()
            changed += len(rows)
    return changed


def _checksum_cols(key):
    t = TABLES[key]
    return t.get("checksum") or [col for col in t["columns"] if col != "id"]


def _block(key):
    return TABLES[key].get("block", BLOCK)


def _block_checksum_sql(key):
    t = TABLES[key]
    parts = ", ".join(f"IFNULL({col if '(' in col else '`' + col + '`'}, '~N~')" for col in _checksum_cols(key))
    return (f"SELECT FLOOR(id / {_block(key)}) AS b, COUNT(*) AS n, "
            f"SUM(CRC32(CONCAT_WS('|', id, {parts}))) AS h "
            f"FROM {t['source']} WHERE id BETWEEN :lo AND :hi GROUP BY b")


def _local_block_stats(c, key, lo, hi):
    import zlib
    t = TABLES[key]
    size = _block(key)
    # CHAR_LENGTH(x) → SQLite length(x) (characters, same as MySQL CHAR_LENGTH).
    cols = [re.sub(r"CHAR_LENGTH\((\w+)\)", r"length(\1)", col) for col in _checksum_cols(key)]
    out = {}
    for row in c.execute(f"SELECT id, {', '.join(cols)} FROM {t['source']} WHERE id BETWEEN ? AND ?", (lo, hi)):
        b = row[0] // size
        vals = [str(row[0])] + ["~N~" if v is None else str(v) for v in row[1:]]
        e = out.setdefault(b, [0, 0])
        e[0] += 1
        e[1] += zlib.crc32("|".join(vals).encode("utf-8"))
    return out


def _recopy_block(c, key, lo, hi):
    """Replace ids lo..hi in ONE transaction: a reader sees the old rows or the new
    ones, never a block with rows missing."""
    t = TABLES[key]
    with _BLOCK_LOCK:
        rows = _db_rows(f"SELECT {', '.join(t['columns'])} FROM {t['source']} "
                        f"WHERE id BETWEEN :lo AND :hi", {"lo": lo, "hi": hi})
        c.execute(f"DELETE FROM {t['source']} WHERE id BETWEEN ? AND ?", (lo, hi))
        _insert(c, key, rows)
        c.commit()


def _reconcile(c, key, from_id=0, duty=1.0):
    """Compare COUNT + checksum per block of ids (from `from_id` up to the copied
    maximum) with MySQL and re-copy just the blocks that differ. `duty` < 1 pauses
    after each query so MySQL spends at most that share of the time on this."""
    size = _block(key)
    hi = int(_get_state(c, f"{key}:max_id", 0) or 0)
    started = time.perf_counter()
    fixed = 0
    for lo in range((max(0, from_id) // size) * size, hi + 1, size * 10):
        top = min(hi, lo + size * 10 - 1)
        q_started = time.perf_counter()
        remote = {int(r["b"]): (int(r["n"]), int(r["h"] or 0))
                  for r in _db_rows(_block_checksum_sql(key), {"lo": lo, "hi": top})}
        if duty < 1.0:
            time.sleep((time.perf_counter() - q_started) * (1.0 / duty - 1.0))
        local = _local_block_stats(c, key, lo, top)
        for b in sorted(set(remote) | set(local)):
            if remote.get(b) != (tuple(local[b]) if b in local else None):
                _recopy_block(c, key, max(lo, b * size), min(top, b * size + size - 1))
                fixed += 1
    if fixed or not from_id:
        slog_warning(f"[NEWS_MIRROR][{key}] reconcile from id {from_id}: {fixed} block(s) "
                     f"re-copied in {time.perf_counter() - started:.0f}s")
    return fixed


def _start_sweep(key):
    """Full checksum pass in the background, one table at a time and throttled to
    SWEEP_DUTY of MySQL's time (it scans the whole table: minutes)."""
    with _STATE_LOCK:
        if key not in _SWEEP_QUEUE:
            _SWEEP_QUEUE.append(key)
    if not _SWEEP_LOCK.acquire(blocking=False):
        return                       # the running sweeper takes it from the queue

    def run():
        try:
            while True:
                with _STATE_LOCK:
                    if not _SWEEP_QUEUE:
                        return
                    k = _SWEEP_QUEUE.pop(0)
                try:
                    c = _conn()
                    _set_state(c, f"{k}:reconciled_at", time.time())
                    c.commit()
                    if _reconcile(c, k, duty=SWEEP_DUTY):
                        _changed({k})
                except Exception as exc:
                    slog_warning(f"[NEWS_MIRROR][{k}] sweep failed: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            _SWEEP_LOCK.release()
    threading.Thread(target=run, name="news-mirror-sweep", daemon=True).start()


def sync(changed_tables=None, reconcile=False):
    """Sync the copy. `changed_tables`: source tables whose write time moved (None =
    all). Called from the freshness thread; passes never overlap, and a call that
    arrives during a pass is queued and run right after it — the watcher reports a
    write only once, so dropping it would leave that edit unseen.
      new rows                  → pulled now (ids above the copied maximum)
      edits to recent rows      → the newest HOT_BLOCKS blocks are checksummed now
      edits anywhere            → full sweep in the background every RECONCILE_EVERY_S
      restored from snapshot    → full sweep before the table is served again"""
    if not enabled():
        return
    with _STATE_LOCK:
        _WANT["run"] = True
        _WANT["reconcile"] |= bool(reconcile)
        if changed_tables is None:
            _WANT["all"] = True
        else:
            _WANT["tables"] |= set(changed_tables)
    while True:
        if not _SYNC_LOCK.acquire(blocking=False):
            return                      # the running pass picks the request up
        try:
            with _STATE_LOCK:
                want = dict(_WANT)
                _WANT.update(run=False, all=False, reconcile=False, tables=set())
            if want["run"]:
                _sync_pass(None if want["all"] else want["tables"], want["reconcile"])
        finally:
            _SYNC_LOCK.release()
        with _STATE_LOCK:
            if not _WANT["run"]:
                return


def _sync_pass(changed_tables, reconcile):
    changed = set()
    try:
        _restore_snapshot()
        c = _conn()
        _schema(c)
        with _STATE_LOCK:
            _READY.clear()      # a copy that had no state table yet is re-checked
        if _get_state(c, "companies:complete") != "1" or (
                changed_tables is None or COMPANIES["source"] in changed_tables):
            if _copy_companies(c):
                changed.add("companies")
        for key, t in TABLES.items():
            if _get_state(c, f"{key}:complete") != "1":
                _full_copy(c, key)
                with _STATE_LOCK:
                    _READY.pop(key, None)
                continue
            moved = changed_tables is None or t["source"] in changed_tables
            verify = _get_state(c, f"{key}:verify") == "1"
            if moved or verify:
                if _tail(c, key):
                    changed.add(key)
                if _pull_changed(c, key):
                    changed.add(key)
                hi = int(_get_state(c, f"{key}:max_id", 0) or 0)
                blocks = RESTORE_HOT_BLOCKS if verify else HOT_BLOCKS
                if _reconcile(c, key, from_id=max(1, hi - blocks * _block(key))):
                    changed.add(key)
            if verify:
                # Restored snapshot: new rows, re-fetched rows and the recent blocks
                # are now exact — serve it; older edits are left to the sweep below.
                _set_state(c, f"{key}:verify", "0")
                _set_state(c, f"{key}:reconciled_at", 0)
                c.commit()
                with _STATE_LOCK:
                    _READY.pop(key, None)
            last = float(_get_state(c, f"{key}:reconciled_at", 0) or 0)
            if reconcile or time.time() - last >= RECONCILE_EVERY_S:
                _start_sweep(key)
        _maybe_snapshot(c)
    except Exception as exc:
        slog_warning(f"[NEWS_MIRROR] sync failed: {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        if changed:
            _changed(changed)


def _maybe_snapshot(c):
    snap = _snapshot_path()
    if not snap or snap == _path():
        return
    if any(_get_state(c, f"{k}:complete") != "1" or _get_state(c, f"{k}:verify") == "1" for k in TABLES):
        return
    if os.path.exists(snap) and time.time() - os.path.getmtime(snap) < SNAPSHOT_EVERY_S:
        return
    if not _SNAPSHOT_LOCK.acquire(blocking=False):
        return

    def run():
        try:
            _write_snapshot()
        except Exception as exc:
            slog_warning(f"[NEWS_MIRROR] snapshot failed: {type(exc).__name__}: {str(exc)[:160]}")
        finally:
            _SNAPSHOT_LOCK.release()
    threading.Thread(target=run, name="news-mirror-snapshot", daemon=True).start()


def start_background_copy():
    """Kick off the first full copy (resumable) off the request path, once."""
    global _STARTED
    if not enabled():
        return
    with _STATE_LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=sync, name="news-mirror-copy", daemon=True).start()


# ── reading ────────────────────────────────────────────────────────────────
_HINT = re.compile(r"/\*\+.*?\*/", re.S)
_INDEX_HINT = re.compile(r"\b(?:USE|FORCE|IGNORE)\s+INDEX\s*\([^)]*\)", re.I)
_MATCH = re.compile(r"MATCH\s*\(\s*([\w.]*?)title\s*\)\s*AGAINST\s*\(\s*:(\w+)\s+IN\s+BOOLEAN\s+MODE\s*\)", re.I)
_GROUP_CONCAT = re.compile(r"GROUP_CONCAT\s*\(\s*DISTINCT\s+(\w+)\s+SEPARATOR\s+'([^']*)'\s*\)", re.I)
_TITLE_LIKE = re.compile(r"(?<![\w.])([\w]+\.)?title\s+LIKE\s+:(\w+)", re.I)
_TX_ORDER = re.compile(r"ORDER BY ([^()]+?)(\s+LIMIT\b|\s*$)", re.I)
_AV_TIME_ORDER = re.compile(r"(?<![\w.])(\w+\.)?time_published_utc\s+(ASC|DESC)\b", re.I)


def _to_sqlite(sql, params):
    """Translate the repository's MySQL news SQL to SQLite with identical meaning."""
    p = {}
    for k, v in params.items():
        p[k] = _sqlite_value(v)
    sql = _HINT.sub("", sql)
    sql = _INDEX_HINT.sub("", sql)

    def _match(m):
        alias, name = m.group(1), m.group(2)
        # '+word*' terms: the Newsroom uses substring LIKE for every range up to a
        # year; this keeps that meaning for wider ranges too (a superset of the
        # FULLTEXT word-prefix match — no article that matched before is lost).
        words = [w.strip("+*") for w in str(p.pop(name)).split() if w.strip("+*")]
        conds = []
        for i, w in enumerate(words):
            k = f"{name}__{i}"
            p[k] = f"%{normalize(w)}%"
            conds.append(f"{alias}norm_title LIKE :{k}")
        return "(" + " AND ".join(conds or ["1=0"]) + ")"
    sql = _MATCH.sub(_match, sql)
    sql = _GROUP_CONCAT.sub(lambda m: f"REPLACE(GROUP_CONCAT(DISTINCT {m.group(1)}), ',', '{m.group(2)}')", sql)

    def _like(m):
        alias, name = m.group(1) or "", m.group(2)
        if name in p and isinstance(p[name], str):
            p[name] = normalize(p[name])
        return f"{alias}norm_title LIKE :{name}"
    sql = _TITLE_LIKE.sub(_like, sql)
    # MySQL walks idx_av_time_id (time_published_utc DESC, id ASC), so equal
    # timestamps come out with id running opposite to the time direction; SQLite's
    # tie order is arbitrary and would pick different rows at a LIMIT edge.
    sql = _AV_TIME_ORDER.sub(
        lambda m: f"{m.group(0)}, {m.group(1) or ''}id {'DESC' if m.group(2).upper() == 'ASC' else 'ASC'}", sql)
    # Transcript lists sort on (…, year, q) indexes whose last key is the primary key,
    # scanned in the ORDER BY's direction: ties come out by id in that same direction.
    if ("coreiq_av_earnings_call_transcripts" in sql and not re.search(r"\bDISTINCT\b", sql, re.I)
            and not re.search(r"\bGROUP\s+BY\b", sql, re.I)):
        sql = _TX_ORDER.sub(_tx_tie, sql)
    return sql, p


def _tx_tie(m):
    terms = m.group(1)
    if re.search(r"(?<![\w.])id\b", terms, re.I):
        return m.group(0)
    last = terms.strip().split(",")[-1].split()
    direction = last[-1].upper() if last and last[-1].upper() in ("ASC", "DESC") else "ASC"
    return f"ORDER BY {terms.rstrip()}, id {direction}{m.group(2)}"


# SQLite stores DATETIME as text. MySQL hands back datetime objects for these columns
# AND for anything computed from them (MIN/MAX(...) AS ts), so restore by value shape.
_DATETIME_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def query(sql, params):
    """Run repository news SQL against the copy. Rows as dicts, datetimes restored."""
    sql2, p2 = _to_sqlite(sql, params)
    c = _conn()
    out = []
    for row in c.execute(sql2, p2):
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, str) and len(v) == 19 and _DATETIME_TEXT.match(v):
                try:
                    d[k] = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
        out.append(d)
    return out


def on_change(name, keys, fn):
    """Call `fn` after the copy of any of `keys` ('av', 'yf', 'tx', 'companies')
    took new or corrected rows. Keyed by `name`, so re-registering replaces."""
    with _STATE_LOCK:
        _ON_CHANGE[name] = (set(keys), fn)


def _changed(keys):
    """The copy now differs from what readers cached: rebuild the disk caches built
    from it (they may have been rebuilt from the copy a moment BEFORE it caught up),
    then clear the in-memory caches."""
    if "tx" in keys:
        _VOCAB["terms"] = None          # new or changed transcripts may add words
    keys = {k for k in keys if k == "companies" or ready(k)}
    if not keys:
        return
    tables = {TABLES[k]["source"] for k in keys if k in TABLES}
    if "companies" in keys:
        tables.add(COMPANIES["source"])
    try:
        from utils.freshness import rebuild_readers
        rebuild_readers(tables)
    except Exception:
        pass
    try:
        from utils.persist import on_tables_moved
        on_tables_moved(tables)
    except Exception:
        pass
    with _STATE_LOCK:
        fns = [fn for ks, fn in _ON_CHANGE.values() if ks & keys]
    for fn in fns:
        try:
            fn()
        except Exception:
            pass


_VOCAB = {"terms": None}
_FOLD = {}                  # non-ASCII char -> its one-char case/accent fold, or None


def _fold_char(ch):
    f = _FOLD.get(ch, "")
    if f == "":
        n = normalize(ch)
        # Exactly one non-ignorable character keeps every position where it was;
        # anything else (ß→ss, æ, soft hyphen, zero-width space) → ask MySQL.
        f = n if len(n) == 1 and unicodedata.category(ch) not in ("Cf", "Mn", "Me", "Cc") else None
        _FOLD[ch] = f
    return f


def locate_ci(needle, text):
    """MySQL LOCATE(needle, text) under utf8mb4_0900_ai_ci (1-based, 0 = absent), or
    None when it can't be reproduced exactly here (caller asks MySQL)."""
    if not needle.isascii():
        return None
    n = needle.lower()
    if text.isascii():
        return text.lower().find(n) + 1
    table = {}
    for ch in set(text):
        if ord(ch) > 127:
            f = _fold_char(ch)
            if f is None:
                return None
            table[ord(ch)] = f
    return text.translate(table).lower().find(n) + 1


def transcript_windows(ids, needle, before, width):
    """{id: SUBSTRING(transcript_text, GREATEST(1, LOCATE(needle, text) - before), width)}
    for the ids answerable exactly from the copy; the rest are left out (ask MySQL)."""
    if not ready("tx"):
        return {}
    c = _conn()
    out = {}
    for n in range(0, len(ids), 500):
        part = ids[n:n + 500]
        for rid, text in c.execute(
                f"SELECT id, transcript_text FROM {TABLES['tx']['source']} "
                f"WHERE id IN ({', '.join('?' * len(part))})", part):
            text = text or ""
            pos = locate_ci(needle, text)
            if pos is None:
                continue
            start = max(1, pos - before)
            out[rid] = text[start - 1:start - 1 + width]
    return out


def transcript_like_ids(keyword):
    """ids of transcripts (has_transcript = 1) whose text matches LIKE '%keyword%' — the
    rows MySQL's LIKE returns (verified identical on 13 keywords) — from the word index,
    in milliseconds instead of a 6-14 s scan of 2 GB of text. A keyword of letters and
    digits can only occur inside one word, so "words containing it" is exact.
    None → not answerable here (copy not ready, or the keyword has other characters):
    the caller runs its MySQL query."""
    kw = normalize((keyword or "").strip())
    if not kw or not kw.isalnum() or not ready("tx"):
        return None
    try:
        c = _conn()
        if _get_state(c, "tx:words") != "1":
            return None
        terms = _VOCAB["terms"]
        if terms is None:
            terms = _VOCAB["terms"] = [r[0] for r in c.execute("SELECT term FROM tx_vocab")]
        hits = [t for t in terms if kw in t]
        if not hits:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in hits)
        src = TABLES["tx"]["source"]
        return [r[0] for r in c.execute(
            f"SELECT t.id FROM tx_words w JOIN {src} t ON t.id = w.rowid "
            f"WHERE tx_words MATCH ? AND t.has_transcript = 1", (match,))]
    except Exception as exc:
        slog_warning(f"[NEWS_MIRROR][tx] word index lookup failed, using MySQL: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def run(sql, params, key):
    """MySQL result for `sql`, served from the copy when it is complete."""
    try:
        from utils.persist import record
        record(sql)        # a persisted result read from the copy still tracks its tables
    except Exception:
        pass
    if ready(key):
        try:
            return query(sql, params)
        except Exception as exc:
            slog_warning(f"[NEWS_MIRROR][{key}] local query failed, using MySQL: "
                         f"{type(exc).__name__}: {str(exc)[:160]}")
    from core.database import db_manager
    return db_manager.execute_query_readonly(sql, params)
