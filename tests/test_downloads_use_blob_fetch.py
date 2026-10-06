"""Every download in the app must fetch its /media file into a Blob.

`st.download_button` renders a real `<a href="/media/..." download>` anchor. Two
things go wrong with that anchor and both were reported from STG:

* a desktop download manager hooks the click and refetches the URL as a fresh,
  cookie-less request — usually after the user has picked a save location, by
  which point Streamlit's end-of-run orphan sweep has deleted the file. The
  manager then reports "No Internet Connection or DNS Failed".
* deferred (callable) data is worse: `MediaFileManager.execute_deferred`
  deliberately leaves the generated file unmapped from the session, so it is
  swept after two runs no matter what the user does.

`utils.media_url.lazy_download_button` starts the fetch the instant the trigger
mounts and hands the browser an in-memory `blob:` URL, which needs no network.
"""

import re
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

# Not loaded by Streamlit (nothing imports it) — a stale duplicate of
# pages/market_data.py kept around; its download buttons are unreachable.
UNLOADED_FILES = {APP / "utils" / "market_data.py"}


def test_no_live_page_uses_st_download_button():
    offenders = [
        f"{path.relative_to(APP)}:{i}"
        for path in sorted(APP.rglob("*.py"))
        if path not in UNLOADED_FILES and " 2/" not in f"/{path.relative_to(APP)}"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bst\.download_button\s*\(", line)
    ]
    assert not offenders, (
        "st.download_button hands the browser a /media anchor; use "
        "utils.media_url.lazy_download_button instead: " + ", ".join(offenders)
    )


@pytest.fixture
def fake_streamlit(monkeypatch):
    """Streamlit with the click already made, so the button body runs inline."""
    import contextlib
    import streamlit as st
    import streamlit.components.v1 as components
    from utils import media_url

    drawn = {"markup": None, "status": None, "toast": None, "error": None,
             "order": [], "spinner_used": False}

    class _Slot:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def empty(self): drawn["order"].append("status_cleared")

    def _html(markup, **kw):
        # The status line and the download trigger both render as components;
        # tell them apart by what they contain.
        if "saveBlob(" in markup:
            drawn["markup"] = markup; drawn["order"].append("download")
        else:
            drawn["status"] = markup; drawn["order"].append("status")

    monkeypatch.setattr(st, "fragment", lambda fn: fn)
    monkeypatch.setattr(st, "container", lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(st, "empty", lambda: _Slot())
    monkeypatch.setattr(st, "button", lambda *a, **kw: True)
    monkeypatch.setattr(st, "toast", lambda msg, **kw: drawn.update(toast=msg))
    monkeypatch.setattr(st, "error", lambda msg, **kw: drawn.update(error=msg))
    monkeypatch.setattr(st, "spinner",
                        lambda *a, **kw: drawn.update(spinner_used=True) or contextlib.nullcontext())
    monkeypatch.setattr(media_url, "serve_bytes",
                        lambda data, mimetype, filename, page="", **kw: "/media/abc.xlsx")
    monkeypatch.setattr(media_url, "absolute_app_url", lambda path: "https://host" + path)
    monkeypatch.setattr(components, "html", _html)
    return drawn


def test_slow_build_shows_a_status_the_page_css_cannot_hide(fake_streamlit):
    """A build that takes minutes must look like it is working.

    `st.spinner` cannot do this job here: core/boot_overlay and
    components/loading both carry "ONE SPINNER ONLY" rules that set
    `[data-testid="stSpinner"]{display:none}` while a branded overlay is in the
    DOM, and a button click puts that overlay up. Measured on the real screening
    page: the spinner rendered with the right text at 0x0, `display: none`.
    """
    from utils import media_url

    media_url.lazy_download_button(
        label="Excel", filename="x.xlsx", build_fn=lambda: b"PK\x03\x04",
        mimetype="application/vnd.ms-excel", key="k", page="test",
        spinner_message="Building full export…",
    )

    status = fake_streamlit["status"]
    assert status, "no status rendered while building"
    assert fake_streamlit["spinner_used"] is False, "st.spinner is hidden on these pages"
    assert "Building full export…" in status
    assert "elapsed" in status and "setInterval" in status   # clock ticks client-side
    # Shown BEFORE the build, cleared after, and only then the download fires.
    assert fake_streamlit["order"] == ["status", "status_cleared", "download"]


def test_click_builds_once_and_downloads_through_a_blob(fake_streamlit):
    from utils import media_url

    builds = []
    media_url.lazy_download_button(
        label="Excel", filename='odd "name".xlsx',
        build_fn=lambda: builds.append(1) or b"PK\x03\x04",
        mimetype="application/vnd.ms-excel", key="k", page="test",
    )

    markup = fake_streamlit["markup"]
    assert builds == [1]                       # built on click, exactly once
    assert "createObjectURL" in markup and "saveBlob(" in markup
    assert "<a href=" not in markup            # no anchor for a download manager to hook
    assert '"https://host/media/abc.xlsx"' in markup
    assert r'"odd \"name\".xlsx"' in markup    # JSON-quoted, not a broken JS literal


def test_empty_build_says_so_instead_of_serving_a_zero_byte_file(fake_streamlit):
    from utils import media_url

    media_url.lazy_download_button(
        label="Excel", filename="x.xlsx", build_fn=lambda: b"",
        mimetype="application/vnd.ms-excel", key="k", page="test",
        empty_message="Nothing to export.",
    )
    assert fake_streamlit["markup"] is None
    assert fake_streamlit["toast"] == "Nothing to export."


def test_build_failure_is_reported_not_raised(fake_streamlit):
    from utils import media_url

    def _boom():
        raise RuntimeError("workbook blew up")

    media_url.lazy_download_button(
        label="Excel", filename="x.xlsx", build_fn=_boom,
        mimetype="application/vnd.ms-excel", key="k", page="test",
    )
    assert fake_streamlit["markup"] is None
    assert "Could not build" in fake_streamlit["error"]
