"""Print a non-executing MySQL ngram maintenance proposal and bounded STG diagnostics."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl


TABLE = "coreiq_av_earnings_call_transcripts"
NGRAM_INDEX = "idx_transcript_ngram_search"
ASSUMED_DDL = """CREATE TABLE `coreiq_av_earnings_call_transcripts` (
  `transcript_text` longtext,
  FULLTEXT KEY `idx_transcript_fulltext_search` (`transcript_text`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""
PROOF_TERMS = [
    "foreign exchange", "xchange", "print", "revenue", "AI", "in", "aaa", "iia",
    "all", "and", "cafe", "naive", "été", "e-commerce", "R&D", "a-b", "abc",
    "abc def", "ab bc", "ＡＩ", "%", "_", "zqxm",
]


def quote_identifier(name):
    return "`" + name.replace("`", "``") + "`"


def fulltext_definitions(ddl):
    definitions = [line.strip().rstrip(",") for line in ddl.splitlines()
                   if re.match(r"\s*FULLTEXT\s+(?:KEY|INDEX)\b", line, re.I)]
    if not definitions:
        raise ValueError("SHOW CREATE TABLE must contain the existing FULLTEXT definitions")
    names = []
    for definition in definitions:
        match = re.match(r"FULLTEXT\s+(?:KEY|INDEX)\s+`((?:``|[^`])+)`", definition, re.I)
        if match is None:
            raise ValueError("Expected quoted FULLTEXT index names from SHOW CREATE TABLE")
        names.append(match.group(1).replace("``", "`"))
    if NGRAM_INDEX in names:
        raise ValueError("The proposed ngram index already exists; inspect its policy before planning a rebuild")
    return list(zip(names, definitions))


def ngram_candidate(term):
    if not term.isascii():
        return None
    runs = dict.fromkeys(run for run in re.findall(r"[^\W_]+", term) if len(run) >= 2)
    return " ".join(f'+"{run}"' for run in runs) or None


def literal_like(term):
    return "%" + term.replace("=", "==").replace("%", "=%").replace("_", "=_") + "%"


def _session_setup(stopwords, stopword_table=None):
    return [
        "SET @ec_saved_stopwords=@@SESSION.innodb_ft_enable_stopword;",
        "SET @ec_saved_user_stopword_table=COALESCE(@@SESSION.innodb_ft_user_stopword_table,'');",
        "SET @ec_saved_lock_wait=@@SESSION.lock_wait_timeout;",
        "SET @ec_saved_row_lock_wait=@@SESSION.innodb_lock_wait_timeout;",
        "SET SESSION lock_wait_timeout=5;",
        "SET SESSION innodb_lock_wait_timeout=5;",
        f"SET SESSION innodb_ft_enable_stopword={stopwords};",
    ] + ([f"SET SESSION innodb_ft_user_stopword_table={stopword_table};"]
         if stopword_table is not None else [])


def _session_restore():
    return [
        "SET SESSION innodb_ft_enable_stopword=@ec_saved_stopwords;",
        "SET SESSION innodb_ft_user_stopword_table=@ec_saved_user_stopword_table;",
        "SET SESSION lock_wait_timeout=@ec_saved_lock_wait;",
        "SET SESSION innodb_lock_wait_timeout=@ec_saved_row_lock_wait;",
    ]


def build_plan(ddl, original_stopwords=None, snapshot_verified=False, inspection=None,
               original_stopword_table=None):
    originals = fulltext_definitions(ddl)
    table = quote_identifier(TABLE)
    drops = ", ".join("DROP INDEX " + quote_identifier(name) for name, _ in originals)
    migration = _session_setup("OFF") + [
        f"ALTER TABLE {table} {drops}, ALGORITHM=INPLACE, LOCK=SHARED;",
        f"ALTER TABLE {table} ADD FULLTEXT INDEX {quote_identifier(NGRAM_INDEX)} "
        "(`transcript_text`) WITH PARSER ngram, ALGORITHM=INPLACE, LOCK=SHARED;",
    ] + [f"ALTER TABLE {table} ADD {definition}, ALGORITHM=INPLACE, LOCK=SHARED;"
         for _, definition in originals] + _session_restore()
    policy = ("ON" if original_stopwords else "OFF") if original_stopwords is not None else "@ec_original_stopword_policy"
    rollback_drops = drops + ", DROP INDEX " + quote_identifier(NGRAM_INDEX)
    if original_stopword_table is None:
        old_table = "@ec_original_stopword_table"
    elif original_stopword_table == "":
        old_table = "''"
    else:
        old_table = "CONVERT(0x" + original_stopword_table.encode().hex() + " USING utf8mb4)"
    rollback = _session_setup(policy, old_table) + [
        f"ALTER TABLE {table} {rollback_drops}, ALGORITHM=INPLACE, LOCK=SHARED;",
    ] + [f"ALTER TABLE {table} ADD {definition}, ALGORITHM=INPLACE, LOCK=SHARED;"
         for _, definition in originals] + _session_restore()
    unverified = []
    if not snapshot_verified:
        unverified.append("Original FULLTEXT definitions use an offline assumption; capture current SHOW CREATE TABLE.")
    if original_stopwords is None:
        unverified.append("Original table FULLTEXT stopword policy is unverified; exact rollback requires its captured ON/OFF value.")
    if original_stopword_table is None:
        unverified.append("Original persistent stopword table name is unverified; capture stopword_table_name and preserve any custom table for rollback.")
    unverified += [
        "A maintenance window and paused ingestion/writers have not been approved.",
        "Azure free storage and temporary build storage have not been verified; DATA_FREE is not server free disk.",
        "Production-sized clone build duration, extra disk usage, and uncached search performance remain unmeasured.",
    ]
    examples = []
    for term in ("exchange", "printenvnovalue", "AI", "foreign exchange", "R&D"):
        anchor = ngram_candidate(term)
        predicate = ("LOCATE(CAST(LOWER(%s) AS BINARY),"
                     "CAST(LOWER(REPLACE(transcript_text,CONVERT(0xC4B0 USING utf8mb4),"
                     "CONVERT(0x69CC87 USING utf8mb4))) AS BINARY))>0")
        params = [term]
        index = ""
        if anchor is not None:
            index = f" FORCE INDEX ({quote_identifier(NGRAM_INDEX)})"
            predicate = "MATCH(transcript_text) AGAINST(%s IN BOOLEAN MODE) AND " + predicate
            params.insert(0, anchor)
        examples.append({"term": term, "fallback_scan": anchor is None,
                         "sql": f"SELECT /*+ MAX_EXECUTION_TIME(5000) */ id FROM {table}{index} WHERE {predicate} ORDER BY id;",
                         "params": params})
    return {
        "execution": "disabled",
        "approval_ready": not unverified,
        "source_table": TABLE,
        "original_definition_sha256": hashlib.sha256(ddl.encode()).hexdigest(),
        "original_fulltext_definitions": [definition for _, definition in originals],
        "original_stopwords": original_stopwords,
        "original_stopword_table": original_stopword_table,
        "inspection": inspection,
        "unverified": unverified,
        "maintenance_effects": [
            "Dropping every FULLTEXT index resets the table's persistent stopword policy; ngram must be recreated first under OFF.",
            "Existing FULLTEXT definitions are recreated unchanged, but their stopword policy becomes OFF: words such as 'the' may now match.",
            "LOCK=SHARED permits reads and blocks source writes during each index build. MATCH consumers can fail while indexes are absent.",
            "The 5-second lock settings bound lock acquisition; they do not bound ALTER TABLE execution. SELECT MAX_EXECUTION_TIME does not cap DDL.",
            "Index builds are separate, implicitly committed DDL operations; failure requires stage-aware recovery during the maintenance window.",
            "No source triggers, sidecar tables, global variable changes, source row writes, or index DDL are executed by this script.",
        ],
        "before_and_after_data_check_sql": f"CHECKSUM TABLE {table} EXTENDED;",
        "data_check_note": "Run while writers remain paused; this reads table contents and can be expensive.",
        "migration_sql": migration,
        "rollback_sql": rollback,
        "rollback_note": "First provide verified original use_stopword (0/1) and stopword_table_name (empty for default) if unknown. Preserve any custom table contents; if default was used, ensure the server stopword-table override is still empty. After a partial failure, drop only indexes that currently exist.",
        "verification_sql": [
            "SELECT @@version,@@ngram_token_size,@@SESSION.innodb_ft_enable_stopword;",
            "SELECT PLUGIN_NAME,PLUGIN_STATUS FROM INFORMATION_SCHEMA.PLUGINS WHERE PLUGIN_NAME='ngram';",
            f"SHOW CREATE TABLE {table};",
            f"SHOW INDEX FROM {table};",
            f"SELECT SUM(year IS NULL) AS null_year, SUM(q IS NULL) AS null_q FROM {table} WHERE has_transcript=1;",
        ],
        "persistent_policy_preflight_command": "python3 scripts/ec_transcript_index_migration.py --inspect-fts-policy",
        "persistent_policy_preflight_note": "Use the existing configured STG credentials with DBA diagnostic privileges. This explicit mode temporarily changes only an unused GLOBAL diagnostic selector, restores it in finally, and verifies restoration. --inspect remains SELECT/SHOW only.",
        "query_examples": examples,
        "query_contract": [
            "Require native MySQL 8.0, ACTIVE ngram parser, ngram_token_size=2, and utf8mb4_0900_ai_ci source collation.",
            "AND quoted ASCII alphanumeric runs of at least two characters as positional ngram phrases, then verify binary LOWER literal matching with Python's dotted-I lowercase expansion. A quoted whole query can miss punctuation/phrase cases.",
            "For ASCII queries this follows the product's lowercase literal contract; accent-insensitive collation expansions such as strasse/Straße and ae/æ are not matches.",
            "Non-ASCII queries and terms with no usable bigram retain the exact scan fallback; this migration does not guarantee fast Unicode, one-character, or symbol-only negatives.",
            "The isolated 23-term fixture proves the tested cases, not arbitrary Unicode or production performance.",
            "Keep application routing unchanged until candidate recall, source data checks, original-index compatibility, and uncached UI tests pass.",
        ],
        "isolated_fixture_terms": PROOF_TERMS,
    }


def _staging_connection(read_timeout=10):
    import pymysql
    from dotenv import dotenv_values
    root = Path(__file__).resolve().parents[1]
    env = {**dotenv_values(root / ".env"), **os.environ}
    required = ("STG_DB_HOST", "STG_DB_USER", "STG_DB_PASSWORD", "STG_DB_NAME")
    if any(not env.get(key) for key in required):
        raise ValueError("STG_DB_HOST/USER/PASSWORD/NAME must be configured")
    context = ssl.create_default_context(cafile=str(root / "DigiCertGlobalRootG2.crt.pem"))
    return pymysql.connect(host=env["STG_DB_HOST"], port=int(env.get("STG_DB_PORT") or 3306),
                           user=env["STG_DB_USER"], password=env["STG_DB_PASSWORD"],
                           database=env["STG_DB_NAME"], ssl=context, autocommit=True,
                           connect_timeout=5, read_timeout=read_timeout, write_timeout=5,
                           cursorclass=pymysql.cursors.DictCursor)


def inspect_staging():
    """Capture metadata through SELECT/SHOW only; never change the FT auxiliary selector."""
    connection = _staging_connection()
    with connection, connection.cursor() as cursor:
        cursor.execute(f"SHOW CREATE TABLE {quote_identifier(TABLE)}")
        ddl = cursor.fetchone()["Create Table"]
        cursor.execute("SELECT DATABASE() AS database_name,@@version AS version,@@ngram_token_size AS token_size")
        server = cursor.fetchone()
        cursor.execute("SELECT ENGINE,TABLE_ROWS,DATA_LENGTH,INDEX_LENGTH,DATA_FREE,TABLE_COLLATION "
                       "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (TABLE,))
        table = cursor.fetchone()
        cursor.execute(f"SHOW FULL COLUMNS FROM {quote_identifier(TABLE)} WHERE Field='transcript_text'")
        text_column = cursor.fetchone()
        cursor.execute("SELECT PLUGIN_NAME,PLUGIN_STATUS FROM INFORMATION_SCHEMA.PLUGINS WHERE PLUGIN_NAME='ngram'")
        parser = cursor.fetchone()
    if not server["version"].startswith("8.0.") or server["token_size"] != 2:
        raise ValueError("This proposal requires MySQL 8.0 with ngram_token_size=2")
    if not parser or parser["PLUGIN_STATUS"] != "ACTIVE":
        raise ValueError("The native ngram parser must be ACTIVE")
    if table["ENGINE"] != "InnoDB" or text_column["Collation"] != "utf8mb4_0900_ai_ci":
        raise ValueError("Inspect engine/collation differences before using this proposal")
    return {"show_create_table": ddl, "server": server, "table": table, "text_column": text_column, "parser": parser,
            "original_stopwords": None,
            "original_stopword_table": None,
            "stopword_policy_note": "Session/global defaults do not prove the persistent per-table FTS policy."}


def _diagnostic_error(exc):
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    return {"type": type(exc).__name__, "code": code,
            "reason": "insufficient_privilege" if code == 1227 else "diagnostic_operation_failed"}


def inspect_fts_policy(connect=None):
    """Temporarily select an unused diagnostic view; always restore and verify it."""
    connect = connect or (lambda: _staging_connection(read_timeout=5))
    result = {"execution": "diagnostic_settings_only", "status": "error",
              "schema_or_data_writes": 0, "setting_change_attempted": False,
              "setting_change_succeeded": False, "restoration_verified": False}
    connection = None
    original = target = None
    config = None
    stage = "connect"
    try:
        connection = connect()
        with connection.cursor() as cursor:
            stage = "read_database"
            cursor.execute("SELECT DATABASE() AS database_name")
            database = cursor.fetchone()["database_name"]
            if not database:
                raise ValueError("A selected database is required")
            target = database + "/" + TABLE
            result["target_selector"] = target
            stage = "read_selector"
            cursor.execute("SHOW GLOBAL VARIABLES LIKE 'innodb_ft_aux_table'")
            original = cursor.fetchone()["Value"]
            result["selector_before"] = original
            if original != "":
                result["status"] = "skipped_selector_in_use"
                return result
            stage = "set_selector"
            result["setting_change_attempted"] = True
            cursor.execute("SET GLOBAL innodb_ft_aux_table=%s", (target,))
            result["setting_change_succeeded"] = True
            stage = "read_persistent_policy"
            cursor.execute("SELECT `KEY`,`VALUE` FROM INFORMATION_SCHEMA.INNODB_FT_CONFIG "
                           "WHERE `KEY` IN ('use_stopword','stopword_table_name')")
            config = {row["KEY"]: row["VALUE"] for row in cursor.fetchall()}
            if config.get("use_stopword") not in ("0", "1") or "stopword_table_name" not in config:
                raise ValueError("Persistent stopword policy is incomplete")
            result["status"] = "ok"
    except Exception as exc:
        result["error_stage"] = stage
        result["error"] = _diagnostic_error(exc)
    finally:
        if result["setting_change_attempted"]:
            for _attempt in range(2):
                try:
                    if connection is None or not connection.open:
                        connection = connect()
                    with connection.cursor() as cursor:
                        cursor.execute("SHOW GLOBAL VARIABLES LIKE 'innodb_ft_aux_table'")
                        current = cursor.fetchone()["Value"]
                        if current == target:
                            result["owned_selector_observed_during_restore"] = True
                            cursor.execute("SET GLOBAL innodb_ft_aux_table=%s", (original,))
                        elif current != original:
                            result["restore_error"] = {"reason": "concurrent_selector_change_not_overwritten"}
                            break
                        cursor.execute("SHOW GLOBAL VARIABLES LIKE 'innodb_ft_aux_table'")
                        result["selector_after"] = cursor.fetchone()["Value"]
                        result["restoration_verified"] = result["selector_after"] == original
                    if result["restoration_verified"]:
                        break
                except Exception as exc:
                    result["restore_error"] = _diagnostic_error(exc)
                    if connection is not None:
                        connection.close()
                    connection = None
            if not result["restoration_verified"]:
                result["status"] = "error"
                result["manual_action"] = "DBA must inspect the diagnostic selector; restore the captured prior value only if this probe still owns it."
        if connection is not None:
            connection.close()
    if result["status"] == "ok" and result["restoration_verified"]:
        result["persistent_config"] = config
        result["original_stopwords"] = config["use_stopword"] == "1"
        result["original_stopword_table"] = config["stopword_table_name"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="SELECT/SHOW staging metadata; no schema or setting changes")
    parser.add_argument("--inspect-fts-policy", action="store_true", help="Briefly select an unused GLOBAL FTS diagnostic view, read its policy, restore and verify; no DDL/data writes")
    parser.add_argument("--snapshot", type=Path, help="Previously captured inspection JSON; no database access")
    parser.add_argument("--original-stopwords", choices=("ON", "OFF"), help="Previously verified persistent table policy, not a session default")
    parser.add_argument("--original-stopword-table", help="Previously verified persistent stopword_table_name; pass an empty string for default")
    args = parser.parse_args(argv)
    if sum((args.inspect, args.inspect_fts_policy, args.snapshot is not None)) > 1:
        parser.error("Choose --inspect, --inspect-fts-policy, or --snapshot")
    if args.inspect_fts_policy:
        diagnostic = inspect_fts_policy()
        print(json.dumps(diagnostic, indent=2, ensure_ascii=False))
        return 0 if diagnostic["status"] == "ok" else 2
    snapshot = inspect_staging() if args.inspect else json.loads(args.snapshot.read_text()) if args.snapshot else None
    if snapshot and "inspection" in snapshot:
        snapshot = snapshot["inspection"]
    if args.snapshot and (not snapshot or "show_create_table" not in snapshot):
        parser.error("Snapshot must contain a captured SHOW CREATE TABLE inspection")
    original = args.original_stopwords == "ON" if args.original_stopwords else None
    if snapshot and original is None:
        original = snapshot.get("original_stopwords")
    original_table = args.original_stopword_table
    if snapshot and original_table is None:
        original_table = snapshot.get("original_stopword_table")
    ddl = snapshot["show_create_table"] if snapshot else ASSUMED_DDL
    plan = build_plan(ddl, original, snapshot_verified=snapshot is not None, inspection=snapshot,
                      original_stopword_table=original_table)
    print(json.dumps(plan, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
