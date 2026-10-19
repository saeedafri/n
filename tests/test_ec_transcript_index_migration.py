import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ec_transcript_index_migration.py"
DDL = """CREATE TABLE `coreiq_av_earnings_call_transcripts` (
  `id` bigint NOT NULL,
  `transcript_text` longtext,
  FULLTEXT KEY `idx_transcript_fulltext_search` (`transcript_text`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"""


def module():
    spec = importlib.util.spec_from_file_location("ec_transcript_index_migration", SCRIPT)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_default_command_prints_nonexecuting_plan_without_database_access():
    run = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    plan = json.loads(run.stdout)
    assert plan["execution"] == "disabled"
    assert not plan["approval_ready"]
    assert plan["migration_sql"] and plan["rollback_sql"]


def test_script_has_no_apply_command_before_maintenance_approval():
    run = subprocess.run([sys.executable, str(SCRIPT), "--apply"], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode != 0
    assert "unrecognized arguments: --apply" in run.stderr


def test_rebuild_creates_ngram_first_under_stopwords_off():
    plan = module().build_plan(DDL, original_stopwords=True, snapshot_verified=True)
    sql = plan["migration_sql"]
    off = next(i for i, q in enumerate(sql) if "innodb_ft_enable_stopword=OFF" in q)
    additions = [(i, q) for i, q in enumerate(sql) if "ADD FULLTEXT" in q]
    assert off < additions[0][0]
    assert "WITH PARSER ngram" in additions[0][1]
    assert "idx_transcript_fulltext_search" in additions[1][1]
    assert all("LOCK=SHARED" in q for q in sql if q.startswith("ALTER TABLE"))
    assert not any("CREATE TRIGGER" in q or "DROP TABLE" in q for q in sql)


def test_rollback_preserves_original_fulltext_definitions():
    custom = DDL.replace("(`transcript_text`)\n", "(`transcript_text`) /*!50100 WITH PARSER `ngram` */\n")
    plan = module().build_plan(custom, original_stopwords=True, snapshot_verified=True)
    assert any("ADD FULLTEXT KEY `idx_transcript_fulltext_search` (`transcript_text`) /*!50100 WITH PARSER `ngram` */"
               in q for q in plan["rollback_sql"])
    assert any("innodb_ft_enable_stopword=ON" in q for q in plan["rollback_sql"])
    assert plan["original_definition_sha256"]


def test_short_and_punctuation_queries_require_exact_scan_fallback():
    m = module()
    assert m.ngram_candidate("AI") == '+"AI"'
    assert m.ngram_candidate("print") == '+"print"'
    assert m.ngram_candidate("foreign exchange") == '+"foreign" +"exchange"'
    assert m.ngram_candidate("e-commerce") == '+"commerce"'
    for term in ("R&D", "a-b", "%", "_", "a"):
        assert m.ngram_candidate(term) is None
    assert m.literal_like("50%_a=b") == "%50=%=_a==b%"


def test_non_ascii_queries_keep_conservative_exact_fallback():
    m = module()
    for term in ("été", "ＡＩ", "Straße", "cafe\u0301", "can’t"):
        assert m.ngram_candidate(term) is None


def test_query_examples_use_the_application_literal_predicate():
    plan = module().build_plan(DDL)
    assert all("LOCATE(CAST(LOWER(%s) AS BINARY)" in example["sql"]
               for example in plan["query_examples"])
    assert all("0xC4B0" in example["sql"] and "0x69CC87" in example["sql"]
               for example in plan["query_examples"])
    assert plan["query_examples"][0]["params"][-1] == "exchange"


def test_unverified_stopword_policy_blocks_approval_ready_status():
    plan = module().build_plan(DDL, original_stopwords=None, snapshot_verified=True)
    assert not plan["approval_ready"]
    assert any("stopword" in issue.lower() for issue in plan["unverified"])


def test_rollback_requires_and_restores_original_custom_stopword_table():
    m = module()
    plan = m.build_plan(DDL, original_stopwords=True, snapshot_verified=True,
                        original_stopword_table="audit/original_words")
    sql = plan["rollback_sql"]
    restore_table = next(i for i, query in enumerate(sql)
                         if "innodb_ft_user_stopword_table=CONVERT(0x" in query)
    create_original = next(i for i, query in enumerate(sql) if "ADD FULLTEXT" in query)
    assert restore_table < create_original
    assert "audit/original_words".encode().hex() in sql[restore_table]
    assert plan["original_stopword_table"] == "audit/original_words"
    unknown = m.build_plan(DDL, original_stopwords=True, snapshot_verified=True)
    assert any("stopword table" in issue.lower() for issue in unknown["unverified"])


def test_saved_inspection_plan_can_be_reused_offline(tmp_path):
    snapshot = tmp_path / "inspection.json"
    plan = module().build_plan(DDL, original_stopwords=True, snapshot_verified=True,
                               inspection={"show_create_table": DDL, "original_stopwords": True})
    snapshot.write_text(json.dumps(plan))
    run = subprocess.run([sys.executable, str(SCRIPT), "--snapshot", str(snapshot)],
                         cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    loaded = json.loads(run.stdout)
    assert loaded["original_stopwords"] is True
    assert loaded["original_definition_sha256"] == plan["original_definition_sha256"]


class DiagnosticDB:
    def __init__(self, selector="", fail_config=False, deny_set=False):
        self.selector = selector
        self.fail_config = fail_config
        self.deny_set = deny_set
        self.queries = []
        self.open = True
        self.rows = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if sql.startswith("SELECT DATABASE()"):
            self.rows = [{"database_name": "audit_stg"}]
        elif sql.startswith("SHOW GLOBAL"):
            self.rows = [{"Variable_name": "innodb_ft_aux_table", "Value": self.selector}]
        elif sql.startswith("SET GLOBAL"):
            if self.deny_set:
                raise RuntimeError(1227, "password_secret must not be returned")
            self.selector = params[0]
        elif "INNODB_FT_CONFIG" in sql:
            if self.fail_config:
                raise RuntimeError(1142, "password_secret must not be returned")
            self.rows = [{"KEY": "use_stopword", "VALUE": "1"},
                         {"KEY": "stopword_table_name", "VALUE": ""}]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows

    def close(self):
        self.open = False


def test_diagnostic_selector_is_restored_when_config_query_fails():
    db = DiagnosticDB(fail_config=True)
    result = module().inspect_fts_policy(connect=lambda: db)
    assert result["status"] == "error"
    assert result["setting_change_succeeded"]
    assert result["restoration_verified"]
    assert db.selector == ""
    assert [params[0] for sql, params in db.queries if sql.startswith("SET GLOBAL")] == [
        "audit_stg/" + module().TABLE, ""]
    assert "password_secret" not in json.dumps(result)


def test_diagnostic_selector_in_use_is_left_untouched():
    db = DiagnosticDB(selector="other_schema/other_table")
    result = module().inspect_fts_policy(connect=lambda: db)
    assert result["status"] == "skipped_selector_in_use"
    assert not result["setting_change_succeeded"]
    assert not any(sql.startswith("SET GLOBAL") for sql, _ in db.queries)
    assert db.selector == "other_schema/other_table"


def test_diagnostic_privilege_denied_verifies_unchanged_selector():
    db = DiagnosticDB(deny_set=True)
    result = module().inspect_fts_policy(connect=lambda: db)
    assert result["status"] == "error"
    assert result["error"]["code"] == 1227
    assert not result["setting_change_succeeded"]
    assert result["restoration_verified"]
    assert db.selector == ""
    assert "password_secret" not in json.dumps(result)


def test_diagnostic_policy_result_contains_verified_values_only_after_restore():
    db = DiagnosticDB()
    result = module().inspect_fts_policy(connect=lambda: db)
    assert result["status"] == "ok"
    assert result["restoration_verified"]
    assert result["original_stopwords"] is True
    assert result["original_stopword_table"] == ""


def test_diagnostic_cli_outputs_safe_json_and_reports_failure(monkeypatch, capsys):
    m = module()
    diagnostic = {"status": "error", "setting_change_succeeded": False,
                  "restoration_verified": True, "error": {"code": 1227}}
    monkeypatch.setattr(m, "inspect_fts_policy", lambda: diagnostic)
    assert m.main(["--inspect-fts-policy"]) == 2
    assert json.loads(capsys.readouterr().out) == diagnostic


def test_diagnostic_cli_cannot_be_combined_with_readonly_inspection():
    run = subprocess.run([sys.executable, str(SCRIPT), "--inspect", "--inspect-fts-policy"],
                         cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 2
    assert "Choose --inspect, --inspect-fts-policy, or --snapshot" in run.stderr
