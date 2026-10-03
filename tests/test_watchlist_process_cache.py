"""Watchlists are cached process-wide per user; any write clears every user's entry."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import data.watchlist_service as wl  # noqa: E402


def test_second_session_reads_cache_and_write_clears_all(monkeypatch):
    calls = []
    monkeypatch.setattr(wl, "_require_tables", lambda: None)
    monkeypatch.setattr(wl.db_manager, "execute_query_readonly",
                        lambda sql, p: calls.append(p["email"]) or [{"id": 1, "name": "A", "is_owner": 0}])
    wl._WL_PROCESS_CACHE.clear()
    assert wl.get_user_watchlists("a@x.com")[0]["name"] == "A"
    wl.get_user_watchlists("a@x.com")
    wl.get_user_watchlists("b@x.com")
    assert calls == ["a@x.com", "b@x.com"]
    wl._invalidate_watchlist_cache("b@x.com")           # b edits a shared watchlist
    wl.get_user_watchlists("a@x.com")
    assert calls == ["a@x.com", "b@x.com", "a@x.com"]   # a sees the change too
    rows = wl.get_user_watchlists("a@x.com")
    rows[0]["name"] = "mutated by caller"
    assert wl.get_user_watchlists("a@x.com")[0]["name"] == "A"
