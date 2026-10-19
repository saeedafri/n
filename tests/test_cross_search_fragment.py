"""Exercise the production page boundary without importing its database bootstrap."""

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple
import sys
import time

import pytest


PAGE = Path(__file__).resolve().parents[1] / "app/pages/earnings_calls.py"


class State(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


class FullRerun(BaseException):
    pass


class StopRun(BaseException):
    pass


class SearchFailure(RuntimeError):
    pass


class Slot:
    def __init__(self):
        self.cleared = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def empty(self):
        self.cleared = True


class Streamlit:
    def __init__(self):
        self.session_state = State(auth_data={"user_email": "one@example.com", "session_id": "one"})
        self.query_params = {}
        self.fragments, self.callbacks, self.widget_scopes, self.errors = [], {}, {}, []
        self.markups = []
        self.options = {}
        self.inside = None

    def fragment(self, function=None, **kwargs):
        if function is None:
            return lambda fn: self.fragment(fn, **kwargs)

        def run():
            previous = self.inside
            self.inside = function.__name__
            try:
                return function()
            finally:
                self.inside = previous

        self.fragments.append(run)
        return run

    def columns(self, widths, **kwargs):
        return [Slot() for _ in widths]

    def container(self, **kwargs):
        return Slot()

    def text_input(self, label, *, key, value="", **kwargs):
        self.widget_scopes[key] = self.inside
        return self.session_state.setdefault(key, value)

    def selectbox(self, label, *, key, options, index=0, on_change=None, **kwargs):
        self.widget_scopes[key] = self.inside
        self.options[key] = list(options)
        self.callbacks[key] = on_change
        return self.session_state.setdefault(key, options[index])

    def button(self, label, *, key, on_click=None, **kwargs):
        self.widget_scopes[key] = self.inside
        self.callbacks[key] = on_click
        return False

    def rerun(self, *, scope="app"):
        assert scope == "app"
        raise FullRerun()

    def stop(self):
        raise StopRun()

    def error(self, text):
        self.errors.append(text)

    def markdown(self, text, *args, **kwargs):
        self.markups.append(text)

    caption = markdown
    success = markdown
    info = markdown


@pytest.fixture
def page(monkeypatch):
    st, fetches, loaders, trackers = Streamlit(), [], [], []
    catalogues = dict(companies=0, non_sec=0, years=0, watchlists=0)

    def catalogue(name, value):
        catalogues[name] += 1
        return value

    repository = SimpleNamespace(
        get_companies_with_earnings=lambda: catalogue("companies", [{"ticker": "AMZN", "name": "Amazon"}]),
        get_non_sec_transcript_companies=lambda: catalogue("non_sec", []),
        get_all_available_years=lambda: catalogue("years", [2026, 2025]),
        get_years_and_quarters=lambda ticker: {2026: ["Q1", "Q2"]},
    )

    def fetch(*args):
        fetches.append(args)
        start = len(fetches) * 15
        return [dict(id=start + i, ticker="AMZN", year=2026, quarter="Q1", speaker="CEO",
                     snippet=args[0], paragraph=args[0]) for i in range(15)], 15, (2026, 1, start + 14)

    def loader(label, *, auto_hide):
        assert auto_hide is False
        slot = Slot()
        loaders.append(slot)
        return slot

    class Tracker:
        def __init__(self, *_):
            self.finishes = 0
            trackers.append(self)

        def step_start(self, *_):
            pass

        def step_end(self, *_):
            pass

        def finish(self):
            self.finishes += 1

    monkeypatch.setitem(sys.modules, "components.loading", SimpleNamespace(render_page_loader=loader))
    monkeypatch.setitem(sys.modules, "core.smart_search", SimpleNamespace(expand_synonyms=lambda _: []))
    no_op = lambda *args, **kwargs: None
    namespace = dict(
        st=st, _time=time, os=__import__("os"), ThreadPoolExecutor=ThreadPoolExecutor,
        Optional=Optional, Tuple=Tuple, List=List, Dict=Dict,
        EarningsCallRepository=repository, EarningsCalendarRepository=SimpleNamespace(),
        PageLoadTracker=Tracker, TranscriptSearchError=SearchFailure,
        get_current_user=lambda: st.session_state.get("auth_data", {}).get("user_email"),
        get_user_watchlists=lambda *args, **kwargs: catalogue("watchlists", []),
        get_earnings_css=lambda: "", load_earnings_calls_state=no_op, save_earnings_calls_state=no_op,
        new_rerun_id=no_op, log_timing=no_op, log_info=no_op, log_warning=no_op,
        log_error=no_op, log_structured_error=no_op, log_render_complete=no_op,
        render_cross_search_panel=lambda *args: "", _get_cross_search_results=fetch,
        _highlight_keyword=lambda text, *args, **kwargs: text,
        _safe_excel_filename_keyword=lambda value: value, _ec_excel_job_get=lambda _: {"bytes": b"csv"},
        lazy_download_button=no_op, _ec_excel_jobs_snapshot=lambda: [],
    )
    tree = ast.parse(PAGE.read_text())
    names = {"render_earnings_calls", "_request_more_cross_results", "_update_cross_search_state",
             "_preserve_ec_control_state_for_app_rerun"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(PAGE), "exec"), namespace)
    namespace["render_earnings_calls"]("ALL")
    def run():
        assert len(st.fragments) == 1, "Search and Show More must share a content fragment"
        return st.fragments[0]()

    return SimpleNamespace(st=st, ns=namespace, run=run, fetches=fetches,
                           catalogues=catalogues, loaders=loaders, trackers=trackers)


def test_keyword_and_more_rerun_content_without_reloading_catalogues(page):
    before = page.catalogues.copy()
    page.st.session_state.ec_search_input = "exchange"
    page.run()
    page.st.callbacks["ec_cross_load_more"]()
    page.run()
    assert page.catalogues == before
    assert len(page.fetches) == 2 and len(page.st.session_state.ec_cross_acc) == 30
    assert [call[-1] for call in page.fetches] == [None, (2026, 1, 29)]
    assert len(page.loaders) == 2 and all(slot.cleared for slot in page.loaders)
    assert len(page.trackers) == 3 and all(tracker.finishes == 1 for tracker in page.trackers)


@pytest.mark.parametrize("key,value", [
    ("ec_company_select", "AMZN"),
    ("ec_company_select", "ALL"),
    ("ec_year_select_ALL", "2025"),
    ("ec_quarter_select_ALL_ALL", "Q2"),
])
def test_filter_callbacks_request_full_run_before_using_old_options(page, key, value):
    page.st.session_state[key] = value
    page.st.callbacks[key]()
    with pytest.raises(FullRerun):
        page.run()
    assert page.fetches == []


def test_search_failure_clears_feedback_and_retry_refetches(page):
    page.st.session_state.ec_search_input = "exchange"
    fetch = page.ns["_get_cross_search_results"]
    page.ns["_get_cross_search_results"] = lambda *args: (_ for _ in ()).throw(SearchFailure("deadline"))
    page.run()
    assert page.loaders[-1].cleared
    assert "Transcript search could not finish" in page.st.errors[-1]
    assert page.st.widget_scopes["ec_retry_search"] is not None
    page.ns["_get_cross_search_results"] = fetch
    page.st.callbacks["ec_retry_search"]()
    page.run()
    assert len(page.fetches) == 1


def test_failed_more_keeps_cards_and_retry_uses_the_same_cursor(page):
    page.st.session_state.ec_search_input = "exchange"
    page.run()
    cursor = page.st.session_state.ec_cross_next_cursor
    fetch = page.ns["_get_cross_search_results"]
    page.ns["_get_cross_search_results"] = lambda *args: (_ for _ in ()).throw(SearchFailure("deadline"))
    page.st.callbacks["ec_cross_load_more"]()
    start = len(page.st.markups)
    page.run()
    assert sum('class="transcript-search-result-card"' in text for text in page.st.markups[start:]) == 15
    assert page.st.session_state.ec_cross_next_cursor == cursor
    assert page.loaders[-1].cleared
    page.ns["_get_cross_search_results"] = fetch
    page.st.callbacks["ec_retry_search"]()
    page.run()
    assert page.fetches[-1][-1] == cursor
    assert len(page.st.session_state.ec_cross_acc) == 30


def test_failed_new_keyword_never_renders_the_previous_signatures_cards(page):
    page.st.session_state.ec_search_input = "exchange"
    page.run()
    page.st.session_state.ec_search_input = "print"
    page.ns["_get_cross_search_results"] = lambda *args: (_ for _ in ()).throw(SearchFailure("deadline"))
    start = len(page.st.markups)
    page.run()
    assert not any('class="transcript-search-result-card"' in text for text in page.st.markups[start:])
    assert not any('No matches found' in text for text in page.st.markups[start:])
    assert "Transcript search could not finish" in page.st.errors[-1]


@pytest.mark.parametrize("change", ["user", "session", "logout"])
def test_auth_identity_change_requests_full_run_before_fetch(page, change):
    page.st.session_state.ec_search_input = "exchange"
    if change == "logout":
        page.st.session_state._auth_invalidated = True
    else:
        key = "user_email" if change == "user" else "session_id"
        page.st.session_state.auth_data[key] = "changed"
    with pytest.raises(FullRerun):
        page.run()
    assert page.fetches == []


def test_widgets_are_created_inside_the_fragment(page):
    assert all(page.st.widget_scopes[key] is not None for key in (
        "ec_search_input", "ec_company_select", "ec_year_select_ALL", "ec_quarter_select_ALL_ALL"))


@pytest.mark.parametrize("failure", [RuntimeError("unexpected"), FullRerun()])
def test_unexpected_errors_and_reruns_clear_the_fragment_loader(page, failure):
    page.st.session_state.ec_search_input = "exchange"
    page.ns["_get_cross_search_results"] = lambda *args: (_ for _ in ()).throw(failure)
    with pytest.raises(StopRun if isinstance(failure, Exception) else FullRerun):
        page.run()
    assert page.loaders[-1].cleared


def test_watchlist_selector_is_outside_and_membership_stays_fresh(page):
    members = []
    page.ns["get_user_watchlists"] = lambda *args, **kwargs: [{"id": 7, "name": "Example"}]
    page.ns["get_watchlist_companies"] = lambda identifier: members.append(identifier) or [{"ticker": "AMZN"}]
    page.ns["_watchlist_rows_to_tickers"] = lambda rows: tuple(row["ticker"] for row in rows)
    page.st.fragments.clear()
    page.ns["render_earnings_calls"]("ALL")
    assert page.st.widget_scopes["ec_calls_watchlist_filter"] is None
    page.st.session_state.ec_calls_watchlist_filter = page.st.options["ec_calls_watchlist_filter"][1]
    page.st.callbacks["ec_calls_watchlist_filter"]()
    before = page.catalogues.copy()
    page.st.fragments.clear()
    page.ns["render_earnings_calls"]("ALL")
    assert page.catalogues["companies"] == before["companies"] + 1
    members.clear()
    page.st.session_state.ec_search_input = "exchange"
    page.run()
    page.st.callbacks["ec_cross_load_more"]()
    page.run()
    assert members == [7, 7]


def test_single_transcript_search_uses_fresh_futures_each_run(page, monkeypatch):
    bodies, dates = [], []
    page.ns["EarningsCallRepository"].get_earnings_calls = lambda **kwargs: (
        bodies.append(kwargs) or [SimpleNamespace(transcript_text="revenue")])
    page.ns["EarningsCalendarRepository"].get_earnings_event_dates = lambda *args: dates.append(args)
    page.ns.update(parse_transcript=lambda _: [{"speaker": "CEO", "text": "revenue"}],
                   _cached_transcript_pdf=lambda *args: None,
                   _render_transcript_header_html=lambda **kwargs: "", render_transcript_card=lambda **kwargs: "")
    monkeypatch.setitem(sys.modules, "core.search_engine", SimpleNamespace(
        TranscriptSearchEngine=lambda segments: SimpleNamespace(search=lambda *args, **kwargs: [])))
    page.st.session_state.update(ec_company="AMZN", ec_company_select="AMZN", ec_year="2026",
                                 ec_quarter="Q2", _ec_quarter_user_set=True)
    page.st.fragments.clear()
    page.ns["render_earnings_calls"]("AMZN")
    before = page.catalogues.copy()
    page.st.session_state.ec_search_input = "revenue"
    page.run()
    assert len(bodies) == len(dates) == 2
    assert page.catalogues == before


@pytest.mark.parametrize("change,expected", [
    ("company", ("AMZN", "2026", "Q2")),
    ("year", ("ALL", "2025", "Q4")),
    ("quarter", ("ALL", "ALL", "Q2")),
    ("auth", ("ALL", "ALL", "ALL")),
])
def test_real_widget_cleanup_preserves_new_filter_dimensions_and_latest_input(page, monkeypatch, change, expected):
    from copy import deepcopy
    from streamlit.proto.WidgetStates_pb2 import WidgetStates
    from streamlit.runtime.state import session_state as state_module, session_state_proxy
    from streamlit.runtime.state.common import WidgetMetadata
    from streamlit.runtime.state.safe_session_state import SafeSessionState

    context = SimpleNamespace(fragment_ids_this_run=["content"], widget_ids_this_run=set(), form_ids_this_run=set())
    monkeypatch.setattr(state_module, "get_script_run_ctx", lambda: context)
    state = state_module.SessionState()
    widget_keys = ("ec_search_input", "ec_company_select", "ec_year_select_ALL", "ec_quarter_select_ALL_ALL")
    for key, value in page.st.session_state.items():
        if key not in widget_keys:
            state[key] = deepcopy(value)
    proxy = session_state_proxy.SessionStateProxy()
    monkeypatch.setattr(session_state_proxy, "get_session_state", lambda: SafeSessionState(state, lambda: None))
    wire = WidgetStates()
    for key in widget_keys:
        initial = page.st.session_state[key]
        identifier = f"$$ID-controls-{key}"
        metadata = WidgetMetadata(id=identifier, fragment_id="content", value_type="string_value",
                                  deserializer=lambda value, default=initial: default if value is None else value,
                                  serializer=lambda value: value, callback=page.st.callbacks.get(key))
        state.register_widget(metadata, key)
        widget = wire.widgets.add()
        widget.id = identifier
        widget.string_value = initial
        if key == "ec_search_input":
            widget.string_value = "exchange"
        elif change == "company" and key == "ec_company_select":
            widget.string_value = "AMZN"
        elif change == "year" and key == "ec_year_select_ALL":
            widget.string_value = "2025"
        elif change == "quarter" and key == "ec_quarter_select_ALL_ALL":
            widget.string_value = "Q2"
    page.st.session_state = proxy
    state.on_script_will_rerun(wire)
    company, year, quarter = expected
    if change == "company":
        state["ec_year_select_AMZN"] = "1999"
        state["ec_quarter_select_AMZN_2026"] = "Q4"
    elif change == "year":
        state["ec_quarter_select_ALL_2025"] = "Q1"
    elif change == "auth":
        proxy.auth_data["session_id"] = "changed"
    with pytest.raises(FullRerun):
        page.run()
    state.on_script_finished(set())
    assert proxy.get("ec_company_select") == company
    assert proxy.get(f"ec_year_select_{company}") == year
    assert proxy.get(f"ec_quarter_select_{company}_{year}") == quarter
    assert proxy.get("ec_search_input") == "exchange"
    assert page.fetches == []

    if change == "company":
        selected = []
        page.st.query_params["ticker"] = "ALL"
        page.st.set_page_config = lambda **kwargs: None
        namespace = dict(page.ns, render_styles=lambda: None, set_page_layout=lambda **kwargs: None,
                         render_header=lambda **kwargs: None, render_coresight_footer=lambda **kwargs: None,
                         render_earnings_calls=lambda ticker: selected.append(ticker),
                         validate_and_get_ticker=lambda **kwargs: pytest.fail("Preserved company must beat URL ALL"))
        main = next(node for node in ast.parse(PAGE.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        exec(compile(ast.Module(body=[main], type_ignores=[]), str(PAGE), "exec"), namespace)
        namespace["main"]()
        assert selected == ["AMZN"]


@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("label,key,value", [
    ("Search", "ec_search_input", "Fresh input"),
    ("Search", "ec_search_input", ""),
    ("Company", "ec_company_select", "AMZN"),
    ("Year", "ec_year_select_AMZN", "2026"),
    ("Quarter", "ec_quarter_select_AMZN_2026", "Q2"),
    ("Watchlist", "ec_calls_watchlist_filter", "Example"),
])
def test_actual_widgets_bind_saved_or_seeded_values_without_framework_warnings(monkeypatch, seeded, label, key, value):
    from streamlit.elements.lib import policies
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(policies, "_shown_default_value_warning", False)
    canonical = dict(ec_search="Saved keyword", ec_company="AMZN", ec_year="2026", ec_quarter="Q2")
    if seeded:
        canonical[key] = value
    node = next(node for node in ast.walk(ast.parse(PAGE.read_text()))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "st"
                and node.func.attr in ("text_input", "selectbox") and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == label)
    script = f"""
import streamlit as st
st.session_state.update({canonical!r})
tickers = ['ALL', 'AMZN']
company_options = [('ALL', 'All Companies'), ('AMZN', 'Amazon (AMZN)')]
year_options = ['ALL', '2026']
quarter_options = ['ALL', 'Q1', 'Q2']
_year_widget_key = 'ec_year_select_AMZN'
_quarter_widget_key = 'ec_quarter_select_AMZN_2026'
_ec_calls_wl_options = ['— No watchlist —', 'Example']
_ec_calls_wl_default_idx = 1
def on_company_change(): pass
def on_year_change(): pass
def on_quarter_change(): pass
def _on_ec_calls_watchlist_change(): pass
{ast.unparse(node)}
"""
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert not app.warning, [warning.value for warning in app.warning]
    widget = app.text_input[0] if label == "Search" else app.selectbox[0]
    assert widget.value == ("Saved keyword" if label == "Search" and not seeded else value)
