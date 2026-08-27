"""The Quick Links footer must offer Forecasting Admin only to people who can use it.

The admin controls live on the Forecasting page — `forecasting_admin.py` is not
registered in main.py, so `/forecasting_admin` 404s. The page grants those controls
on `check_access("forecasting", ..., "edit")` (forecasting.py:_get_refresh_permissions),
which already treats admin and super_user as having every page. The footer link is
gated on the same call so the two cannot drift, and memoised so showing the link
costs no extra round-trip.

These run without a database: `AccessControlManager.check_access` is stubbed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import pytest  # noqa: E402
import streamlit as st  # noqa: E402

import components.navigation as navigation  # noqa: E402
import core.access_control as access_control  # noqa: E402

LINK = '<a href="/forecasting" target="_self">Forecasting Admin</a>'

GRANTED = {
    "mohdsaeedafri@coresight.com",     # admin role
    "nidhishamohandas@coresight.com",  # explicit `forecasting` edit grant
}


@pytest.fixture
def acl(monkeypatch):
    """check_access answers from GRANTED, and records every call."""
    calls = []

    def check_access(page_name, user_email, required_permission="allow"):
        calls.append((page_name, (user_email or "").strip().lower(), required_permission))
        return (user_email or "").strip().lower() in GRANTED

    monkeypatch.setattr(access_control.AccessControlManager, "check_access",
                        staticmethod(check_access))
    for key in [k for k in list(st.session_state.keys())
                if k.startswith("forecast_can_run_")]:
        del st.session_state[key]
    return calls


@pytest.mark.parametrize("email,expected", [
    ("mohdsaeedafri@coresight.com", True),
    ("nidhishamohandas@coresight.com", True),
    ("dataautomation@coresight.com", False),
    ("", False),
    (None, False),
])
def test_gate_admits_only_the_right_people(acl, email, expected):
    assert navigation._can_open_forecast_admin(email) is expected


def test_gate_asks_about_the_right_page(acl):
    navigation._can_open_forecast_admin("dataautomation@coresight.com")
    assert acl, "the ACL was never consulted"
    page_name, _, permission = acl[0]
    assert page_name == "forecasting", "/forecasting_admin is not a registered page"
    assert permission == "edit", "edit is what lets you run the models"


def test_result_is_memoised_so_the_footer_costs_one_query(acl):
    for _ in range(5):
        navigation._can_open_forecast_admin("dataautomation@coresight.com")
    assert len(acl) == 1, f"footer renders on every page; got {len(acl)} queries"


def test_the_answer_is_cached_under_a_key_that_names_it(acl):
    email = "dataautomation@coresight.com"
    navigation._can_open_forecast_admin(email)
    assert f"forecast_can_run_{email}" in st.session_state


def test_a_failing_lookup_is_not_cached(monkeypatch):
    """Caching a failure would hide the link from a real admin all session."""
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(access_control.AccessControlManager, "check_access",
                        staticmethod(boom))
    email = "mohdsaeedafri@coresight.com"
    for key in [k for k in list(st.session_state.keys())
                if k.startswith("forecast_can_run_")]:
        del st.session_state[key]

    assert navigation._can_open_forecast_admin(email) is False
    assert f"forecast_can_run_{email}" not in st.session_state


def _footer_html(monkeypatch, email):
    st.session_state["auth_data"] = {"user_email": email}
    captured = {}
    monkeypatch.setattr(st, "html", lambda body, **kw: captured.setdefault("html", body))
    navigation.render_coresight_footer()
    return captured.get("html", "")


def test_link_is_rendered_for_a_permitted_user(acl, monkeypatch):
    html = _footer_html(monkeypatch, "mohdsaeedafri@coresight.com")
    assert LINK in html
    # Sits in the Quick Links column, after the public links.
    assert html.index("AI Council") < html.index(LINK)


def test_link_is_absent_for_everyone_else(acl, monkeypatch):
    html = _footer_html(monkeypatch, "dataautomation@coresight.com")
    assert html, "footer did not render"
    assert LINK not in html
    assert "Research Portal" in html, "the rest of Quick Links must still render"
