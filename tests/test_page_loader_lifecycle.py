"""Slow transcript searches keep feedback visible until the render exits."""

import ast
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from components import loading  # noqa: E402
from data.repository import TranscriptSearchError  # noqa: E402


class Slot:
    def __init__(self):
        self.markup = None

    def markdown(self, markup, **kwargs):
        self.markup = markup

    def empty(self):
        self.markup = None


def overlay_animation(markup):
    declarations = re.search(r"\.cs-al-ov\{([^}]+)\}", markup).group(1)
    return re.search(r"animation:([^;]+);", declarations).group(1)


def test_run_bound_loader_has_no_timed_hide():
    slot = Slot()
    holder = loading.render_page_loader("Searching transcripts", placeholder=slot, auto_hide=False)

    assert "cs-al-failsafe" not in overlay_animation(slot.markup)
    assert "Searching transcripts" in slot.markup
    holder.empty()
    assert slot.markup is None


def test_other_pages_keep_the_default_timeout():
    slot = Slot()
    loading.render_page_loader("Loading", placeholder=slot)

    assert "cs-al-failsafe .4s ease 6s forwards" in overlay_animation(slot.markup)


class ScriptExit(BaseException):
    pass


@pytest.mark.parametrize("signal", [None, RuntimeError("search failed"), ScriptExit(), TranscriptSearchError("deadline")])
@pytest.mark.parametrize("fresh", [False, True])
def test_earnings_page_clears_its_run_bound_loader_on_every_exit(monkeypatch, signal, fresh):
    events = []
    errors = []
    buttons = []

    class Holder:
        def empty(self):
            events.append("hide")

    def show_loader(label, *, auto_hide):
        assert auto_hide is False
        events.append("show")
        return Holder()

    def render_content(ticker):
        events.append("render")
        if signal is not None:
            raise signal

    def stop():
        raise ScriptExit()

    monkeypatch.setattr(loading, "render_page_loader", show_loader)
    st = SimpleNamespace(
        session_state={} if fresh else {"ec_company_select": "ALL"},
        query_params={"ticker": "ALL"} if fresh else {},
        set_page_config=lambda **kwargs: None,
        error=errors.append, stop=stop,
        button=lambda label, **kwargs: buttons.append((label, kwargs)),
    )
    namespace = {
        "st": st,
        "render_styles": lambda: None,
        "set_page_layout": lambda **kwargs: None,
        "render_header": lambda **kwargs: None,
        "render_earnings_calls": render_content,
        "render_coresight_footer": lambda **kwargs: None,
        "log_structured_error": lambda *args, **kwargs: None,
        "TranscriptSearchError": TranscriptSearchError,
        "validate_and_get_ticker": lambda **kwargs: pytest.fail("ALL must not query company validation"),
    }
    st.session_state = type("State", (dict,), {
        "__getattr__": dict.__getitem__, "__setattr__": dict.__setitem__,
    })(st.session_state)
    tree = ast.parse((APP / "pages" / "earnings_calls.py").read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    exec(compile(ast.Module(body=[main], type_ignores=[]), "earnings_calls.py", "exec"), namespace)

    if signal is None:
        namespace["main"]()
    else:
        with pytest.raises(ScriptExit):
            namespace["main"]()

    assert events == ["show", "render", "hide"]
    if isinstance(signal, TranscriptSearchError):
        assert "Transcript search could not finish" in errors[0]
        assert buttons[0][0] == "Retry search"
        st.session_state.ec_cross_sig = ("inventory", "ALL", "ALL", "ALL", None)
        buttons[0][1]["on_click"]()
        assert "ec_cross_sig" not in st.session_state
