"""The Refresh Data popup: who may open it, and what a missing date may block.

Two defects are pinned here.

1. `_get_refresh_permissions` ran two uncached `check_access` calls on every page
   rerun and returned (False, False) on ANY exception. One timed-out Azure query
   therefore made the "Refresh Data" button disappear for an admin on a page
   refresh, and reappear on the next one.

2. A company with no earnings-calendar row was shown "On hold" and refused a
   refresh. The reporting date is informational — `sync_forecast_for_ticker`
   judges staleness from `last_actual_date` and never reads the calendar — so
   withholding the refresh blocked work that would have succeeded.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import pytest  # noqa: E402
import streamlit as st  # noqa: E402

import pages.forecasting as forecasting  # noqa: E402

ADMIN = "mohdsaeedafri@coresight.com"


@pytest.fixture(autouse=True)
def clean_session():
    for key in [k for k in list(st.session_state.keys())
                if k.startswith("_fc_refresh_perms_")]:
        del st.session_state[key]
    yield


class Access:
    """check_access that can be told to start failing."""

    def __init__(self, allow=True):
        self.allow, self.calls, self.fail = allow, 0, False

    def check_access(self, page_name, user_email, required_permission="allow"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("read timeout")
        return self.allow


@pytest.fixture
def access(monkeypatch):
    fake = Access()
    monkeypatch.setattr(forecasting.AccessControlManager, "check_access",
                        staticmethod(fake.check_access))
    return fake


# ---------------------------------------------------------------------------
# 1 — the vanishing button
# ---------------------------------------------------------------------------

def test_admin_gets_view_and_run(access):
    assert forecasting._get_refresh_permissions(ADMIN) == (True, True)


def test_no_email_is_denied(access):
    assert forecasting._get_refresh_permissions("") == (False, False)
    assert access.calls == 0, "an empty email must not hit the database"


def test_a_transient_failure_keeps_the_button(access):
    """The defect: one failed query used to hide the button from an admin."""
    assert forecasting._get_refresh_permissions(ADMIN) == (True, True)

    access.fail = True
    assert forecasting._get_refresh_permissions(ADMIN) == (True, True), \
        "a timed-out ACL query must not read as 'no access'"


def test_denial_still_applies_when_never_resolved(access):
    """Failing closed is right when we have no successful answer to fall back on."""
    access.fail = True
    assert forecasting._get_refresh_permissions("stranger@example.com") == (False, False)


def test_a_real_denial_is_respected(access):
    access.allow = False
    assert forecasting._get_refresh_permissions("viewer@example.com") == (False, False)


def test_permissions_are_resolved_once_per_session(access):
    """This page reruns constantly; two Azure round-trips per rerun is real latency."""
    for _ in range(4):
        forecasting._get_refresh_permissions(ADMIN)
    assert access.calls == 2, f"edit + view_only once, then cached; got {access.calls}"


# ---------------------------------------------------------------------------
# 2 — a pending reporting date must not block a refresh
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("Feb 26, 2026", True),
    ("", False), ("—", False), ("-", False), ("N/A", False), ("None", False),
    (None, False),
])
def test_has_report_date(value, expected):
    assert forecasting._has_report_date(value) is expected


def test_refresh_all_no_longer_excludes_undated_companies():
    src = inspect.getsource(forecasting._refresh_dialog)
    assert "exclude_tickers" not in src, \
        "companies without a reporting date must still be refreshed in bulk"


def test_every_row_offers_refresh_now():
    src = inspect.getsource(forecasting._refresh_dialog)
    assert "On hold" not in src, "the per-row refresh must no longer be withheld"
    assert 'action_ph.button("Refresh Now"' in src


def test_a_pending_date_is_still_visible_to_the_user():
    """Dropping the block must not hide the fact that the date is unknown."""
    src = inspect.getsource(forecasting._refresh_dialog)
    assert "Pending" in src
    assert "_undated_tickers" in src


def test_staleness_never_consults_the_earnings_calendar():
    """Why the block was unnecessary, pinned so it cannot silently change."""
    import data.forecast_admin_service as admin_service

    src = inspect.getsource(admin_service.sync_forecast_for_ticker)
    for calendar_ref in ("earnings_calendar", "_annual_q4_report_dates_bulk",
                         "annual_reported_on"):
        assert calendar_ref not in src


# ---------------------------------------------------------------------------
# 3 — the quarterly reporting date must not depend on the vendor overview
# ---------------------------------------------------------------------------

def test_quarterly_resolver_does_not_inner_join_the_vendor_overview():
    """The join dropped every ticker whose AV overview carried no fiscal_year_end
    (81 on STG), so BLD, SCVL and VSCO showed no quarterly date despite having
    recent calendar rows. FYE now comes from the same 4-source chain annual uses.
    """
    import data.forecast_refresh_service as refresh

    src = inspect.getsource(refresh._quarterly_report_dates_bulk)
    nasdaq_block = src[:src.index("YF path")] if "YF path" in src else src
    # Match the SQL, not the prose — the fix is explained in a comment that names
    # the table it stopped joining.
    assert "FROM coreiq_av_company_overview" not in nasdaq_block, \
        "quarterly must not re-introduce the vendor-overview join"
    assert "_fiscal_year_end_bulk()" in nasdaq_block


def test_quarterly_does_not_need_a_fiscal_year_end_to_pick_a_date():
    """Every quarter is eligible, so the FYE only labels which quarter it is —
    a ticker with no FYE anywhere must still get its reporting date."""
    import data.forecast_refresh_service as refresh

    src = inspect.getsource(refresh._quarterly_report_dates_bulk)
    # The annual helper filters on `fqe_month == fye_month`; quarterly must not.
    assert "fqe_m == fye_m" not in src
    assert "fye_months.get(tk) or -1" in src, "unknown FYE degrades to -1, not a drop"


# ---------------------------------------------------------------------------
# 4 — "Reporting Date" must say whether it is the last one or the next one
# ---------------------------------------------------------------------------

def test_rows_carry_whether_the_date_has_passed():
    """386 of 440 annual rows show a date already in the past — AMPL's is from
    2012 — because the calendar holds nothing newer. The date stays (a factual
    old date beats a hidden one, per the 2026-07-30 product decision) but the row
    must say which kind it is."""
    import data.forecast_refresh_service as refresh

    for fn in (refresh.get_refresh_table_data, refresh._get_quarterly_refresh_table_data):
        src = inspect.getsource(fn)
        assert "date_already_reported" in src, f"{fn.__name__} must flag the date kind"


def test_the_dialog_labels_both_kinds_of_date():
    src = inspect.getsource(forecasting._refresh_dialog)
    assert '"reported"' in src and '"expected"' in src
    assert "date_already_reported" in src


def test_old_dates_are_still_shown_not_hidden():
    """Hiding them would reverse a deliberate product decision: the column must
    stay traceable to a real calendar row."""
    import data.forecast_refresh_service as refresh

    src = inspect.getsource(refresh._annual_q4_report_dates_bulk)
    assert "NO staleness filter" in src, "the annual resolver must not start dropping old dates"
