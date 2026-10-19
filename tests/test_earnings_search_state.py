"""Browser navigation restores search input without persisting result rows."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from utils import local_storage_manager as storage  # noqa: E402


@pytest.fixture
def browser_storage(monkeypatch):
    saved = {}

    class BrowserStorage:
        def getItem(self, key):
            return saved.get(key)

        def setItem(self, key, value):
            saved[key] = value

    class Manager:
        _SS_CACHE_KEY = "browser_state"
        STORAGE_KEY = "portal_filters"

        def __init__(self, page):
            self.local_storage = BrowserStorage()

    st = SimpleNamespace(session_state={})
    monkeypatch.setattr(storage, "st", st)
    monkeypatch.setattr(storage, "LocalStorageManager", Manager)
    return st, saved


def test_back_navigation_restores_keyword_to_a_new_session(browser_storage):
    st, saved = browser_storage
    st.session_state.update(ec_company="ALL", ec_year="ALL", ec_quarter="ALL",
                            ec_search="foreign exchange", ec_cross_acc=[{"snippet": "old result"}],
                            ec_cross_offset=45, ec_cross_cursor={"after_id": 123})
    assert storage.save_earnings_calls_state() is True

    payload = json.loads(saved["portal_filters"])["earnings_calls"]
    assert payload == {"ec_company": "ALL", "ec_year": "ALL", "ec_quarter": "ALL",
                       "ec_search": "foreign exchange"}
    st.session_state = {}
    assert storage.load_earnings_calls_state() is True
    assert st.session_state["ec_search"] == "foreign exchange"
    assert "ec_cross_acc" not in st.session_state


def test_clearing_search_replaces_the_previously_saved_keyword(browser_storage):
    st, saved = browser_storage
    st.session_state.update(ec_company="ALL", ec_year="ALL", ec_quarter="ALL", ec_search="revenue")
    storage.save_earnings_calls_state()
    st.session_state["ec_search"] = ""
    storage.save_earnings_calls_state()
    st.session_state = {}

    storage.load_earnings_calls_state()
    assert st.session_state["ec_search"] == ""


def test_restore_does_not_replace_current_session_input(browser_storage):
    st, saved = browser_storage
    saved["portal_filters"] = json.dumps({"earnings_calls": {"ec_search": "stored keyword"}})
    st.session_state["ec_search"] = "explicit current input"

    storage.load_earnings_calls_state()
    assert st.session_state["ec_search"] == "explicit current input"
