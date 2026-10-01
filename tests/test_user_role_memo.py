"""Role lookups are memoised briefly (the header/footer ask on every render)
and invalidated immediately when a role is changed or removed."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import core.access_control as ac  # noqa: E402


class FakeDB:
    def __init__(self, role):
        self.role = role
        self.reads = 0

    def execute_query_readonly(self, sql, params):
        self.reads += 1
        return [{"role": self.role}] if self.role else []

    def execute_insert(self, sql, params):
        return 1


def test_role_is_memoised_then_invalidated_on_change(monkeypatch):
    db = FakeDB("admin")
    monkeypatch.setattr(ac, "db", db)
    monkeypatch.setattr(ac.UserRolesManager, "_role_memo", {})
    urm = ac.UserRolesManager
    assert urm.get_user_role("A@coresight.com") == "admin"
    assert urm.get_user_role("a@coresight.com") == "admin"
    assert db.reads == 1
    db.role = "user"
    assert urm.set_user_role("a@coresight.com", "user")[0]
    assert urm.get_user_role("a@coresight.com") == "user"
    assert db.reads == 2
    urm.delete_user("a@coresight.com")
    db.role = None
    assert urm.get_user_role("a@coresight.com") is None


def test_memo_expires(monkeypatch):
    db = FakeDB("super_user")
    monkeypatch.setattr(ac, "db", db)
    monkeypatch.setattr(ac.UserRolesManager, "_role_memo", {})
    monkeypatch.setattr(ac.UserRolesManager, "_ROLE_TTL_S", 0)
    ac.UserRolesManager.get_user_role("b@coresight.com")
    ac.UserRolesManager.get_user_role("b@coresight.com")
    assert db.reads == 2
