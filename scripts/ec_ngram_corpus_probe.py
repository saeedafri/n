"""Build and query an isolated MySQL corpus proof from a read-only SQLite snapshot."""

import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import types
import threading
import time

import pymysql


ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / "output/playwright/ec-cold-index-proof"
SCHEMA = "ec_ngram_corpus_probe"
TABLE = "coreiq_av_earnings_call_transcripts"
COLUMNS = ("id", "source", "ticker", "quarter", "year", "q", "transcript_text",
           "has_transcript", "title", "event_datetime_utc", "fetched_at_utc", "raw_json_sha1")
TERMS = ["exchange", "print", "revenue", "foreign exchange", "printenv", "AI",
         "e-commerce", "R&D", "%", "_", "zqxm"]
LITERAL = ("LOCATE(CAST(LOWER(:keyword) AS BINARY),CAST(LOWER(REPLACE(transcript_text,"
           "CONVERT(0xC4B0 USING utf8mb4),CONVERT(0x69CC87 USING utf8mb4))) AS BINARY))>0")


def allocated_bytes(path):
    total = 0
    for base, _, names in os.walk(path):
        for name in names:
            try:
                total += os.stat(Path(base) / name).st_blocks * 512
            except FileNotFoundError:
                pass
    return total


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def emit(**values):
    print(json.dumps(values, ensure_ascii=False, default=str), flush=True)


def connect(socket, database=None):
    resolved = Path(socket).resolve()
    if not resolved.is_relative_to(PROOF.resolve()):
        raise ValueError("Only the isolated proof socket is allowed")
    connection = pymysql.connect(unix_socket=str(resolved), user="root", autocommit=True,
                                 charset="utf8mb4", database=database,
                                 cursorclass=pymysql.cursors.DictCursor,
                                 read_timeout=3600, write_timeout=60)
    with connection.cursor() as cursor:
        cursor.execute("SELECT VERSION() AS version,@@ngram_token_size AS token_size")
        server = cursor.fetchone()
    if not server["version"].startswith("8.0.") or server["token_size"] != 2:
        connection.close()
        raise ValueError(f"Expected local MySQL8.0 with bigrams: {server}")
    return connection


class DiskGuard:
    def __init__(self, socket, baseline, budget):
        self.socket, self.baseline, self.budget = socket, baseline, budget
        self.stop = threading.Event()
        self.exceeded = threading.Event()
        self.peak = baseline
        self.free_baseline = shutil.disk_usage(PROOF).free + max(0, allocated_bytes(PROOF) - baseline)
        self.peak_filesystem_growth = 0

    def __enter__(self):
        self.thread = threading.Thread(target=self.monitor, daemon=True)
        self.thread.start()
        return self

    def monitor(self):
        while not self.stop.wait(0.5):
            current = allocated_bytes(PROOF)
            self.peak = max(self.peak, current)
            free = shutil.disk_usage(PROOF).free
            filesystem_growth = self.free_baseline - free
            self.peak_filesystem_growth = max(self.peak_filesystem_growth, filesystem_growth)
            if max(current - self.baseline, filesystem_growth) > self.budget - 300_000_000 or free < 24_000_000_000:
                self.exceeded.set()
                emit(disk_guard="stopping isolated server", incremental_bytes=current - self.baseline,
                     filesystem_growth_bytes=filesystem_growth)
                try:
                    with connect(self.socket) as connection, connection.cursor() as cursor:
                        cursor.execute("SHUTDOWN")
                except Exception as error:
                    emit(disk_guard_shutdown_error=str(error))
                return

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=3)


def prepare(args):
    baseline = args.baseline_bytes if args.baseline_bytes is not None else allocated_bytes(PROOF)
    report = {"purpose": "isolated snapshot corpus proof; no live STG or OS-cold claim",
              "baseline_allocated_bytes": baseline, "budget_bytes": args.max_growth_bytes,
              "source": str(Path(args.sqlite).resolve()), "phases": []}
    with DiskGuard(args.socket, baseline, args.max_growth_bytes) as guard:
        with connect(args.socket) as connection, connection.cursor() as cursor:
            if not args.resume:
                cursor.execute(f"DROP DATABASE IF EXISTS {SCHEMA}")
                cursor.execute(f"CREATE DATABASE {SCHEMA} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            cursor.execute(f"USE {SCHEMA}")
            if not args.resume:
                cursor.execute(f"""CREATE TABLE {TABLE}(
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                source VARCHAR(64) NOT NULL, ticker VARCHAR(32) NOT NULL,
                quarter VARCHAR(8) NOT NULL, year SMALLINT NOT NULL, q TINYINT NOT NULL,
                transcript_text LONGTEXT, has_transcript TINYINT(1) NOT NULL DEFAULT 0,
                title VARCHAR(512), event_datetime_utc DATETIME, fetched_at_utc DATETIME NOT NULL,
                raw_json JSON, raw_json_sha1 CHAR(40), last_error TEXT,
                FTS_DOC_ID BIGINT UNSIGNED NOT NULL,
                UNIQUE KEY FTS_DOC_ID_INDEX(FTS_DOC_ID),
                UNIQUE KEY uq_src_ticker_quarter(source,ticker,quarter),
                KEY idx_ticker(ticker), KEY idx_year_q(year,q),
                KEY idx_ec_ticker_quarter(ticker,quarter),
                KEY idx_has_transcript_year(has_transcript,year),
                KEY idx_has_transcript_ticker(has_transcript,ticker),
                KEY idx_covering_transcript_lookup(has_transcript,ticker,transcript_text(100)),
                KEY idx_ec_ticker_year_q(ticker,year,q),
                KEY idx_ec_ticker_year_q_event(ticker,year,q,event_datetime_utc),
                KEY idx_ec_cover_transcript_fetch(ticker,year,q,has_transcript,transcript_text(100)),
                KEY idx_ec_cover_company_lookup(has_transcript,transcript_text(10),ticker),
                KEY idx_ec_cover_ticker_years(ticker,has_transcript,transcript_text(10),year),
                KEY idx_ec_cover_ticker_quarters(ticker,year,has_transcript,transcript_text(10),q),
                KEY idx_ec_list_ordered(ticker,has_transcript,year,q)
            ) ENGINE=InnoDB""")
            if args.incremental and not args.resume:
                cursor.execute("SET SESSION innodb_ft_enable_stopword=OFF")
                cursor.execute(f"ALTER TABLE {TABLE} ADD FULLTEXT KEY idx_transcript_ngram_search(transcript_text) WITH PARSER ngram, ALGORITHM=INPLACE, LOCK=SHARED")
                cursor.execute(f"ALTER TABLE {TABLE} ADD FULLTEXT KEY idx_transcript_fulltext_search(transcript_text), ALGORITHM=INPLACE, LOCK=SHARED")
                cursor.execute("SET SESSION innodb_ft_enable_stopword=ON")
                report["construction"] = "empty FTS first; incremental inserts; bulk ALTER build exceeded temporary disk budget"
            source = sqlite3.connect(f"file:{Path(args.sqlite).resolve()}?mode=ro", uri=True)
            source.execute("PRAGMA query_only=ON")
            source.execute("PRAGMA mmap_size=0")
            source.execute("PRAGMA cache_size=20")
            count = body_bytes = resume_id = 0
            if args.resume:
                cursor.execute(f"SELECT COUNT(*) AS count,MAX(id) AS last_id,MAX(FTS_DOC_ID) AS last_doc_id FROM {TABLE}")
                copied = cursor.fetchone()
                count, resume_id = copied["count"], copied["last_id"] or 0
                if count != (copied["last_doc_id"] or 0):
                    raise AssertionError("Resume requires contiguous internal FTS document IDs")
                body_bytes = source.execute(f"SELECT SUM(length(CAST(transcript_text AS BLOB))) FROM {TABLE} WHERE has_transcript=1 AND id<=?", (resume_id,)).fetchone()[0] or 0
                report["resumed_existing_rows"] = count
                report["construction"] = "empty FTS first; incremental inserts; resumed after disk-guard pause"
                emit(phase="resume", rows=count, last_source_id=resume_id)
            rows = source.execute(f"SELECT {','.join(COLUMNS)} FROM {TABLE} WHERE has_transcript=1 AND id>? ORDER BY id", (resume_id,))
            insert = f"INSERT INTO {TABLE}({','.join(COLUMNS)},FTS_DOC_ID) VALUES({','.join(['%s'] * 13)})"
            started = time.monotonic()
            while batch := rows.fetchmany(50):
                cursor.executemany(insert, [tuple(row) + (count + i + 1,) for i, row in enumerate(batch)])
                count += len(batch)
                body_bytes += sum(len((row[6] or "").encode("utf-8")) for row in batch)
                if count % 1000 == 0:
                    emit(phase="import", rows=count, seconds=round(time.monotonic() - started, 3),
                         incremental_bytes=allocated_bytes(PROOF) - baseline)
                if guard.exceeded.is_set():
                    raise RuntimeError("Disk budget exceeded")
            source.close()
            phase = {"name": "import", "rows": count, "body_bytes": body_bytes,
                     "seconds": time.monotonic() - started,
                     "incremental_allocated_bytes": allocated_bytes(PROOF) - baseline}
            report["phases"].append(phase)
            save(Path(args.output), report)
            emit(**phase)
            cursor.execute("SET SESSION innodb_ft_enable_stopword=OFF")
            for name, ddl in (("ngram_index", f"ALTER TABLE {TABLE} ADD FULLTEXT KEY idx_transcript_ngram_search(transcript_text) WITH PARSER ngram, ALGORITHM=INPLACE, LOCK=SHARED"),
                              ("original_word_index", f"ALTER TABLE {TABLE} ADD FULLTEXT KEY idx_transcript_fulltext_search(transcript_text), ALGORITHM=INPLACE, LOCK=SHARED")):
                if args.incremental:
                    continue
                started = time.monotonic()
                emit(phase=name, state="started", sql=ddl)
                cursor.execute(ddl)
                phase = {"name": name, "seconds": time.monotonic() - started,
                         "incremental_allocated_bytes": allocated_bytes(PROOF) - baseline}
                report["phases"].append(phase)
                save(Path(args.output), report)
                emit(**phase)
            cursor.execute("SET SESSION innodb_ft_enable_stopword=ON")
            cursor.execute(f"SET GLOBAL innodb_ft_aux_table='{SCHEMA}/{TABLE}'")
            cursor.execute("SELECT `KEY`,`VALUE` FROM INFORMATION_SCHEMA.INNODB_FT_CONFIG")
            report["fts_config"] = {row["KEY"]: row["VALUE"] for row in cursor.fetchall()}
            cursor.execute(f"SHOW CREATE TABLE {TABLE}")
            report["create_table"] = cursor.fetchone()["Create Table"]
            cursor.execute("SELECT @@version AS version,@@innodb_buffer_pool_size AS buffer_pool_bytes")
            report["server"] = cursor.fetchone()
        report["peak_incremental_allocated_bytes"] = guard.peak - baseline
        report["peak_filesystem_growth_bytes"] = guard.peak_filesystem_growth
        report["final_incremental_allocated_bytes"] = allocated_bytes(PROOF) - baseline
        save(Path(args.output), report)
        emit(output=args.output, peak_incremental_bytes=report["peak_incremental_allocated_bytes"])


def helper_module():
    logger = types.ModuleType("utils.server_logger")
    logger.log_timing = lambda *args, **kwargs: None
    sys.modules["utils.server_logger"] = logger
    spec = importlib.util.spec_from_file_location("isolated_transcript_infix", ROOT / "app/data/transcript_infix.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_phrase_candidate(term):
    import re
    if not term.isascii():
        return None
    runs = dict.fromkeys(run for run in re.findall(r"[^\W_]+", term) if len(run) >= 2)
    return " ".join(f'+"{run}"' for run in runs) or None


class NativeAdapter:
    def __init__(self, connection):
        self.connection, self.queries = connection, []

    def execute_query_readonly_raising(self, sql, params):
        import re
        converted = re.sub(r":([A-Za-z_]\w*)", r"%(\1)s", sql)
        started = time.perf_counter()
        with self.connection.cursor() as cursor:
            cursor.execute(converted, params)
            result = cursor.fetchall()
        self.queries.append({"seconds": time.perf_counter() - started, "rows": len(result),
                             "body_bytes": sum(len((row.get("transcript_text") or "").encode()) for row in result),
                             "sql": sql, "parameter_keys": list(params)})
        return result


def campaign(args):
    module = helper_module()
    prototype_equals_actual = module.candidate_query(args.term) == run_phrase_candidate(args.term)
    if args.run_phrases:
        if not prototype_equals_actual:
            module.candidate_query = run_phrase_candidate
    term = args.term
    with connect(args.socket, SCHEMA) as connection:
        adapter = NativeAdapter(connection)
        started = time.perf_counter()
        rows = module.search_page(adapter, term, limit=15, query_ms=5000, budget_seconds=20)
        method = "native_ngram_helper"
        if rows is None:
            method = "unanchorable_literal_scan"
            rows = adapter.execute_query_readonly_raising(
                f"SELECT id,quarter,transcript_text FROM {TABLE} WHERE has_transcript=1 "
                f"AND {LITERAL} ORDER BY year DESC,q DESC,id DESC LIMIT :needed",
                {"keyword": term, "needed": 15})
        elapsed = time.perf_counter() - started
        timed_queries = adapter.queries.copy()
        ids = [row["id"] for row in rows]
        body_bytes = sum(len((row.get("transcript_text") or "").encode()) for row in rows)
        emit(term=term, phase="timed_first_page", method=method, seconds=elapsed,
             rows=len(rows), body_bytes=body_bytes)
        with connection.cursor() as cursor:
            started = time.perf_counter()
            cursor.execute(f"SELECT id FROM {TABLE} FORCE INDEX(PRIMARY) WHERE has_transcript=1 "
                           f"AND {LITERAL.replace(':keyword', '%s')} ORDER BY year DESC,q DESC,id DESC", (term,))
            expected = [row["id"] for row in cursor.fetchall()]
            oracle_seconds = time.perf_counter() - started
            anchor = module.candidate_query(term)
            if anchor is not None:
                cursor.execute(f"SELECT id FROM {TABLE} FORCE INDEX(idx_transcript_ngram_search) "
                               "WHERE MATCH(transcript_text) AGAINST(%s IN BOOLEAN MODE) "
                               f"AND has_transcript=1 AND {LITERAL.replace(':keyword', '%s')} "
                               "ORDER BY year DESC,q DESC,id DESC", (anchor, term))
                indexed_all = [row["id"] for row in cursor.fetchall()]
            else:
                indexed_all = None
            cursor.execute("SELECT @@version AS version,@@innodb_buffer_pool_size AS buffer_pool_bytes,"
                           "@@innodb_buffer_pool_load_at_startup AS buffer_pool_load_at_startup,"
                           "@@innodb_buffer_pool_dump_at_shutdown AS buffer_pool_dump_at_shutdown")
            server = cursor.fetchone()
            run_comparison = None
            if term in ("foreign exchange", "printenv"):
                run_anchor = run_phrase_candidate(term)
                started = time.perf_counter()
                cursor.execute(f"SELECT id FROM {TABLE} FORCE INDEX(idx_transcript_ngram_search) "
                               "WHERE MATCH(transcript_text) AGAINST(%s IN BOOLEAN MODE)", (run_anchor,))
                run_candidates = [row["id"] for row in cursor.fetchall()]
                run_seconds = time.perf_counter() - started
                import re
                grams = dict.fromkeys(run[i:i + 2] for run in re.findall(r"[^\W_]+", term)
                                      for i in range(len(run) - 1))
                gram_anchor = " ".join(f'+"{gram}"' for gram in grams)
                cursor.execute(f"SELECT id FROM {TABLE} FORCE INDEX(idx_transcript_ngram_search) "
                               "WHERE MATCH(transcript_text) AGAINST(%s IN BOOLEAN MODE)", (gram_anchor,))
                gram_count = len(cursor.fetchall())
                run_comparison = {"anchor": run_anchor, "candidate_count": len(run_candidates),
                                  "candidate_query_seconds_after_timing": run_seconds,
                                  "missing_literal_oracle_ids": sorted(set(expected) - set(run_candidates)),
                                  "loose_gram_candidate_count": gram_count,
                                  "timed_helper_candidate_count": timed_queries[0]["rows"]}
        report = {"term": term, "method": method, "first_page_seconds": elapsed,
                  "returned_body_bytes": body_bytes, "page_ids": ids,
                  "expected_page_ids": expected[:15], "total_literal_matches": len(expected),
                  "full_oracle_seconds_after_timing": oracle_seconds,
                  "indexed_exact_all_count": len(indexed_all) if indexed_all is not None else None,
                  "indexed_exact_all_equals_oracle": indexed_all == expected if indexed_all is not None else None,
                  "first_page_equals_oracle": ids == expected[:15], "timed_queries": timed_queries,
                  "server": server, "coldness": "fresh mysqld buffer pool; OS file cache uncontrolled",
                  "source_scope": "eligible rows of existing local SQLite snapshot; not current live STG"}
        report["helper_sha256"] = hashlib.sha256((ROOT / "app/data/transcript_infix.py").read_bytes()).hexdigest()
        report["oracle_predicate"] = LITERAL
        report["candidate_override"] = "quoted_alnum_runs" if args.run_phrases and not prototype_equals_actual else None
        report["quoted_run_prototype_equals_actual_builder"] = prototype_equals_actual
        report["quoted_run_comparison_after_timing"] = run_comparison
        save(Path(args.output), report)
        if ids != expected[:15] or indexed_all is not None and indexed_all != expected:
            raise AssertionError({"term": term, "page_ok": ids == expected[:15],
                                  "missing": sorted(set(expected) - set(indexed_all or expected)),
                                  "extra": sorted(set(indexed_all or expected) - set(expected))})
        emit(term=term, output=args.output, first_page_seconds=elapsed,
             total_matches=len(expected), all_matches_verified=indexed_all == expected if anchor else "scan")


def fixture(args):
    spec = importlib.util.spec_from_file_location("ngram_original_fixture", ROOT / "scripts/ec_ngram_index_probe.py")
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    module = helper_module()
    module.TABLE = "runtime_fixture"
    if args.run_phrases:
        module.candidate_query = run_phrase_candidate
    rows = original.ROWS + [(20, "cafe\u0301 nai\u0308ve Straße æ can't don't"),
                            (21, "ex\u00adchange ex\u200bchange print\u200ber"),
                            (22, "caf\u00e9 naïve strasse ae can’t don’t"),
                            (23, "ＡＩ ﬁ ligature e\u0301 and É"),
                            (24, "İX İN KA Kelvin ẞ STRASSE Æ"),
                            (25, "foreign&nbsp;exchange PR&#73;NT")]
    rows += [(100 + control, "a" + chr(control) + "b") for control in list(range(32)) + [127]]
    rows += [(300 + index, "a" + punctuation + "b") for index, punctuation in enumerate(".,-/\\_:+;!?()[]{} ")]
    terms = list(dict.fromkeys(original.TERMS + ["exchange", "strasse", "Straße", "ae", "æ", "fi",
                                               "can't", "don't", "can’t", "cafe\u0301", "naive", "naïve", "printer",
                                               "ab", "ix", "in", "ka", "kelvin"]))
    with connect(args.socket, SCHEMA) as connection, connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS runtime_fixture")
        cursor.execute("SET SESSION innodb_ft_enable_stopword=ON")
        cursor.execute(f"CREATE TABLE runtime_fixture LIKE {TABLE}")
        cursor.execute("SET GLOBAL innodb_ft_aux_table='ec_ngram_corpus_probe/runtime_fixture'")
        cursor.execute("SELECT `KEY`,`VALUE` FROM INFORMATION_SCHEMA.INNODB_FT_CONFIG")
        before = {row["KEY"]: row["VALUE"] for row in cursor.fetchall()}
        ddls = ["ALTER TABLE runtime_fixture DROP INDEX idx_transcript_ngram_search, DROP INDEX idx_transcript_fulltext_search, ALGORITHM=INPLACE, LOCK=SHARED",
                "ALTER TABLE runtime_fixture ADD FULLTEXT KEY idx_transcript_ngram_search(transcript_text) WITH PARSER ngram, ALGORITHM=INPLACE, LOCK=SHARED",
                "ALTER TABLE runtime_fixture ADD FULLTEXT KEY idx_transcript_fulltext_search(transcript_text), ALGORITHM=INPLACE, LOCK=SHARED"]
        cursor.execute(ddls[0])
        cursor.execute("SET SESSION innodb_ft_enable_stopword=OFF")
        for sql in ddls[1:]:
            cursor.execute(sql)
        cursor.execute("SET SESSION innodb_ft_enable_stopword=ON")
        cursor.executemany("INSERT INTO runtime_fixture(id,source,ticker,quarter,year,q,transcript_text,"
                           "has_transcript,fetched_at_utc,FTS_DOC_ID) VALUES(%s,'fixture',%s,'2026Q1',2026,1,%s,1,'2026-10-19 00:00:00',%s)",
                           [(row_id, "FIX" + str(row_id), text, row_id) for row_id, text in rows])
        cursor.execute("SELECT `KEY`,`VALUE` FROM INFORMATION_SCHEMA.INNODB_FT_CONFIG")
        after = {row["KEY"]: row["VALUE"] for row in cursor.fetchall()}
        adapter = NativeAdapter(connection)
        cases = []
        for term in terms:
            result = module.search_page(adapter, term, limit=100)
            cursor.execute("SELECT id FROM runtime_fixture WHERE has_transcript=1 AND LOCATE(%s,transcript_text)>0 "
                           "ORDER BY year DESC,q DESC,id DESC", (term,))
            expected = [row["id"] for row in cursor.fetchall()]
            cursor.execute("SELECT id FROM runtime_fixture WHERE has_transcript=1 AND "
                           "LOCATE(CAST(LOWER(%s) AS BINARY),CAST(LOWER(transcript_text) AS BINARY))>0 "
                           "ORDER BY year DESC,q DESC,id DESC", (term,))
            binary_lower = [row["id"] for row in cursor.fetchall()]
            python_lower = sorted([row_id for row_id, text in rows if term.lower() in text.lower()], reverse=True)
            anchor = module.candidate_query(term)
            if anchor:
                cursor.execute("SELECT id FROM runtime_fixture FORCE INDEX(idx_transcript_ngram_search) "
                               "WHERE MATCH(transcript_text) AGAINST(%s IN BOOLEAN MODE) ORDER BY id", (anchor,))
                candidate_ids = [row["id"] for row in cursor.fetchall()]
                missing = sorted(set(expected) - set(candidate_ids))
            else:
                candidate_ids = missing = None
            actual = [row["id"] for row in result] if result is not None else None
            cases.append({"term": term, "anchor": anchor, "oracle_locate_ids": expected,
                          "binary_lower_locate_ids": binary_lower, "python_lower_ids": python_lower,
                          "binary_lower_matches_python": binary_lower == python_lower,
                          "candidate_missing_python_lower": sorted(set(python_lower) - set(candidate_ids)) if candidate_ids is not None else None,
                          "candidate_ids": candidate_ids, "candidate_missing": missing,
                          "helper_ids": actual, "fallback_scan_required": result is None,
                          "passed": actual == python_lower if result is not None else anchor is None})
        report = {"rows": rows, "cases": cases, "explicit_ddl_options_executed": ddls,
                  "policy_before_drop": before, "policy_after_recreate_and_default_on_inserts": after,
                  "oracle": "native MySQL LOCATE; no LIKE oracle", "cases_passed": sum(case["passed"] for case in cases)}
        save(Path(args.output), report)
        emit(output=args.output, cases=len(cases), passed=report["cases_passed"], before=before, after=after)
        if any(not case["passed"] or case["candidate_missing_python_lower"] for case in cases):
            raise AssertionError([case for case in cases if not case["passed"] or case["candidate_missing_python_lower"]])


def server(args):
    socket = PROOF / "mysql.sock"
    if Path(args.socket).resolve() != socket.resolve():
        raise ValueError("Server control only supports the fixed isolated proof socket")
    pid_path = PROOF / "mysql.pid"
    if args.action in ("stop", "restart") and socket.exists():
        with connect(socket) as connection, connection.cursor() as cursor:
            cursor.execute("SHUTDOWN")
        deadline = time.monotonic() + 30
        while (socket.exists() or pid_path.exists()) and time.monotonic() < deadline:
            time.sleep(0.1)
        if socket.exists() or pid_path.exists():
            raise TimeoutError("Isolated MySQL did not stop within30seconds")
    if args.action in ("start", "restart"):
        if socket.exists() or pid_path.exists():
            raise RuntimeError("Isolated socket or PID file already exists; stop it before starting")
        binary = PROOF / "mysql-8.0.44-macos15-arm64"
        (PROOF / "tmp").mkdir(exist_ok=True)
        argv = [str(binary / "bin/mysqld"), "--no-defaults", f"--basedir={binary}",
                f"--datadir={PROOF / 'data'}", f"--socket={socket}", f"--pid-file={pid_path}",
                f"--log-error={PROOF / 'mysql-server.log'}", f"--tmpdir={PROOF / 'tmp'}",
                f"--innodb-tmpdir={PROOF / 'tmp'}", "--skip-networking", "--mysqlx=OFF",
                "--skip-log-bin", "--performance-schema=OFF", "--innodb-buffer-pool-size=128M",
                "--innodb-buffer-pool-dump-at-shutdown=OFF", "--innodb-buffer-pool-load-at-startup=OFF",
                "--innodb-ft-sort-pll-degree=2"]
        with (PROOF / "mysql-process.log").open("ab") as log:
            process = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Isolated MySQL exited with{process.returncode}")
            try:
                with connect(socket):
                    break
            except pymysql.Error:
                time.sleep(0.1)
        else:
            raise TimeoutError("Isolated MySQL did not become ready within30seconds")
    report = {"action": args.action, "socket": str(socket),
              "running_pid": int(pid_path.read_text().strip()) if pid_path.exists() else None,
              "pool_bytes": 134217728, "buffer_pool_load_at_startup": False,
              "buffer_pool_dump_at_shutdown": False,
              "corpus_queries_performed": 0, "OS_file_cache": "uncontrolled"}
    save(Path(args.output), report)
    emit(**report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "campaign", "fixture", "server"))
    parser.add_argument("--socket", required=True)
    parser.add_argument("--sqlite", default=str(ROOT / "server-logs/agg-cache/news_mirror.sqlite"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-growth-bytes", type=int, default=5_000_000_000)
    parser.add_argument("--baseline-bytes", type=int)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--term")
    parser.add_argument("--run-phrases", action="store_true")
    parser.add_argument("--action", choices=("start", "stop", "restart"), default="restart")
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args)
    elif args.phase == "fixture":
        fixture(args)
    elif args.phase == "server":
        server(args)
    else:
        if args.term is None:
            parser.error("campaign requires --term")
        campaign(args)


if __name__ == "__main__":
    main()
