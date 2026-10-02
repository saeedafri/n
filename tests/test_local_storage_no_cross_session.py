"""The module-level LocalStorageManager is shared by every session in the server
process. A value one user stores must never be returned to another session (it
used to be: a process-wide dict was read before session_state)."""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import utils.local_storage as ls  # noqa: E402


def test_value_set_in_one_session_is_not_seen_by_another(monkeypatch):
    user_a = SimpleNamespace(session_state={})
    user_b = SimpleNamespace(session_state={})

    monkeypatch.setattr(ls, "st", user_a)
    ls.local_storage.set(ls.StorageKey.MARKETDATA_SELECTED_COMPANY, "HD")
    assert ls.get_marketdata_company() == "HD"

    monkeypatch.setattr(ls, "st", user_b)
    assert ls.get_marketdata_company() is None


def test_delete_only_touches_the_current_session(monkeypatch):
    user_a = SimpleNamespace(session_state={})
    user_b = SimpleNamespace(session_state={})
    monkeypatch.setattr(ls, "st", user_a)
    ls.local_storage.set(ls.StorageKey.MARKETDATA_SELECTED_COMPANY, "WMT")
    monkeypatch.setattr(ls, "st", user_b)
    ls.local_storage.set(ls.StorageKey.MARKETDATA_SELECTED_COMPANY, "TGT")
    ls.local_storage.delete(ls.StorageKey.MARKETDATA_SELECTED_COMPANY)
    assert ls.get_marketdata_company() is None
    monkeypatch.setattr(ls, "st", user_a)
    assert ls.get_marketdata_company() == "WMT"
