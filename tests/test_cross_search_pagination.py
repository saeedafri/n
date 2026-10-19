import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple
import sys

import pytest


class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


@pytest.fixture
def pagination():
    page = Path(__file__).resolve().parents[1] / "app/pages/earnings_calls.py"
    tree = ast.parse(page.read_text())
    names = {"_request_more_cross_results", "_update_cross_search_state"}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in functions} == names, "Pagination must fetch before rendering, without a second rerun"
    state, calls = State(), []

    def fetch(*args):
        calls.append(args)
        offset, after = args[-2:]
        start = after[2] + 1 if after is not None else offset
        return [{"id": start + i} for i in range(15)], 15, (2026, 1, start + 14)

    namespace = {"st": SimpleNamespace(session_state=state), "_get_cross_search_results": fetch}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(page), "exec"), namespace)
    return namespace, state, calls


SIG = ("exchange", "ALL", "ALL", "ALL", None)


def test_show_more_fetches_next_page_once_before_render(pagination):
    ns, state, calls = pagination
    ns["_update_cross_search_state"](SIG, None, 15)
    ns["_request_more_cross_results"]()
    ns["_update_cross_search_state"](SIG, None, 15)
    ns["_update_cross_search_state"](SIG, None, 15)
    assert [c[-2] for c in calls] == [0, 0]
    assert [c[-1] for c in calls] == [None, (2026, 1, 14)]
    assert [r["id"] for r in state.ec_cross_acc] == list(range(30))
    assert state.ec_cross_offset == 30


def test_new_filter_discards_pending_old_page_request(pagination):
    ns, state, calls = pagination
    ns["_update_cross_search_state"](SIG, None, 15)
    ns["_request_more_cross_results"]()
    changed = ("revenue", "ALL", "ALL", "ALL", None)
    ns["_update_cross_search_state"](changed, None, 15)
    assert calls[-1][0] == "revenue" and calls[-1][-2:] == (0, None)
    assert len(state.ec_cross_acc) == 15


def test_failed_page_preserves_existing_results_and_offset(pagination):
    ns, state, calls = pagination
    ns["_update_cross_search_state"](SIG, None, 15)
    before = list(state.ec_cross_acc)

    def fail(*args):
        raise RuntimeError("query failed")

    ns["_get_cross_search_results"] = fail
    ns["_request_more_cross_results"]()
    with pytest.raises(RuntimeError, match="query failed"):
        ns["_update_cross_search_state"](SIG, None, 15)
    assert state.ec_cross_acc == before
    assert state.ec_cross_offset == 15


def test_filtered_empty_page_keeps_raw_cursor_and_can_continue(pagination):
    ns, state, calls = pagination
    ns["_get_cross_search_results"] = lambda *args: ([], 15, (2026, 1, 14))
    ns["_update_cross_search_state"](SIG, None, 15)
    assert state.ec_cross_has_more and state.ec_cross_acc == []
    assert state.ec_cross_next_cursor == (2026, 1, 14)
    ns["_get_cross_search_results"] = lambda *args: ([], 0, None)
    ns["_request_more_cross_results"]()
    ns["_update_cross_search_state"](SIG, None, 15)
    assert not state.ec_cross_has_more


@pytest.mark.parametrize("keyword, matching_text, filtered_text", [
    ("AI", "AI demand increased", "metadata match only"),
    ("strasse", "Strasse demand increased", "Straße demand increased"),
    ("ae", "ae demand increased", "æ demand increased"),
    ("ix", "IX demand increased", "İX demand increased"),
])
def test_raw_last_row_sets_cursor_even_when_excerpt_is_filtered(monkeypatch, keyword, matching_text, filtered_text):
    page = Path(__file__).resolve().parents[1] / "app/pages/earnings_calls.py"
    tree = ast.parse(page.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_get_cross_search_results")
    function.decorator_list = []
    rows = [
        dict(id=9, year=2026, q=2, ticker="A", transcript_text=matching_text),
        dict(id=8, year=2026, q=1, ticker="B", transcript_text=filtered_text),
    ]
    monkeypatch.setitem(sys.modules, "core.smart_search", SimpleNamespace(
        SmartQueryExpander=lambda: SimpleNamespace(expand_query=lambda _: {})))
    captured = {}

    def search(**kwargs):
        captured.update(kwargs)
        return rows

    namespace = dict(Optional=Optional, Tuple=Tuple, List=List, Dict=Dict,
                     EarningsCallRepository=SimpleNamespace(search_transcripts_fulltext=search),
                     parse_transcript=lambda text: [dict(text=text, speaker="CEO")])
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(page), "exec"), namespace)
    results, count, cursor = namespace[function.name](keyword, "ALL", "ALL", "ALL", after=(2026, 3, 10))
    assert len(results) == 1 and results[0]["ticker"] == "A"
    assert count == 2 and cursor == (2026, 1, 8)
    assert captured["after"] == (2026, 3, 10)
