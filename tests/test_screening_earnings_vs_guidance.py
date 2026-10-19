"""Earnings and Guidance are two halves of one stored category — keep them exact.

coreiq_company_events stores a single 'Earnings & Guidance' category. The
screening UI now offers 'Earnings' and 'Guidance' separately, and that split is
expressed THREE times: as a predicate (keydev_is_guidance_row), as a WHERE
fragment (keydev_category_clause) and as a column on the on-disk event index
(build_keydev_event_index). If those three ever disagree, a screen, its row
count and its Excel export quietly return different sets.

These tests run against the STG DB (VPN required) and pin:
  1. the two halves partition the stored category exactly — no row lost, none
     counted twice;
  2. the SQL fragment and the Python predicate return the same event_ids;
  3. the news-derived 'Guidance Update' rows whose headline is not about
     guidance land in Earnings, not Guidance (the reported Kroger layoff row);
  4. a screen saved before the split still asks for the whole category.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

from data.screening_config import (  # noqa: E402
    KEYDEV_CATEGORIES_ALL,
    KEYDEV_EARNINGS,
    KEYDEV_EARNINGS_GUIDANCE,
    KEYDEV_GUIDANCE,
    KEYDEV_NEWS_EARNINGS_SUBTYPE,
    KEYDEV_NEWS_GUIDANCE_SUBTYPE,
    keydev_display_subtype,
    keydev_is_guidance_row,
)
from data.screening_service import (  # noqa: E402
    keydev_category_clause,
    keydev_db_categories,
)


def rows(sql):
    from core.database import db_manager
    return db_manager.execute_query_readonly_raising(sql)


def count_where(clause, since=None):
    """COUNT over the events table. `since` bounds it to one date partition range.

    The additivity check below ORs two categories together with the headline test
    and, unbounded, scans ~66k rows across all 34 date partitions — it measured
    right on the 120s DB_READ_TIMEOUT and timed out twice. The property it tests
    (mixed == plain + guidance) holds on any window, and the app never issues this
    query unbounded anyway: the page always carries a ticker clause and usually a
    date window. So the arithmetic is checked on a year, not on all history.
    """
    where = f"{clause}" + (f" AND e.event_date >= '{since}'" if since else "")
    return int(rows(f"SELECT COUNT(*) AS c FROM coreiq_company_events e "
                    f"WHERE {where}")[0]["c"])


# ── Pure, no DB ──────────────────────────────────────────────────────────────

def test_both_halves_are_offered_and_the_merged_label_is_not():
    assert KEYDEV_CATEGORIES_ALL[KEYDEV_EARNINGS] == KEYDEV_EARNINGS
    assert KEYDEV_CATEGORIES_ALL[KEYDEV_GUIDANCE] == KEYDEV_GUIDANCE
    assert KEYDEV_EARNINGS_GUIDANCE not in KEYDEV_CATEGORIES_ALL


def test_both_halves_resolve_to_the_one_stored_category():
    assert keydev_db_categories((KEYDEV_EARNINGS, KEYDEV_GUIDANCE)) == \
        (KEYDEV_EARNINGS_GUIDANCE,)
    assert keydev_db_categories(("M&A Activity", KEYDEV_GUIDANCE)) == \
        ("M&A Activity", KEYDEV_EARNINGS_GUIDANCE)


def test_a_screen_saved_before_the_split_still_asks_for_everything():
    """Saved criteria hold the old 'Earnings & Guidance' token."""
    legacy = keydev_category_clause((KEYDEV_EARNINGS_GUIDANCE,))
    both = keydev_category_clause((KEYDEV_EARNINGS, KEYDEV_GUIDANCE))
    assert legacy == both
    assert "event_subtype" not in legacy      # no half applied → plain category


def test_the_predicate_reads_the_headline_only_for_the_news_subtype():
    # Filing-derived guidance is trusted on its subtype alone.
    assert keydev_is_guidance_row("Annual Guidance (10-K MD&A)", "Anything at all")
    assert keydev_is_guidance_row("Earnings Release - Guidance Raised", "No g-word here")
    # The news-derived subtype has to prove itself in the headline.
    assert keydev_is_guidance_row(
        KEYDEV_NEWS_GUIDANCE_SUBTYPE, "Lamb Weston Lifts Full-Year Guidance")
    assert not keydev_is_guidance_row(
        KEYDEV_NEWS_GUIDANCE_SUBTYPE, "Kroger to cut 3,000 corporate roles as AI use expands")
    # Earnings subtypes are never guidance.
    assert not keydev_is_guidance_row("Earnings Release", "Raises guidance for 2026")
    assert not keydev_is_guidance_row(None, None)


def test_a_news_row_is_labelled_by_the_half_it_screens_into():
    """The Type column must not contradict the screen the row appears in."""
    # Not guidance by headline -> screened as Earnings -> labelled as Earnings.
    assert keydev_display_subtype(
        KEYDEV_NEWS_GUIDANCE_SUBTYPE,
        "Kroger to cut 3,000 corporate roles as AI use expands",
    ) == KEYDEV_NEWS_EARNINGS_SUBTYPE
    # Guidance by headline -> keeps its stored label.
    assert keydev_display_subtype(
        KEYDEV_NEWS_GUIDANCE_SUBTYPE,
        "Lamb Weston Lifts Full-Year Guidance",
    ) == KEYDEV_NEWS_GUIDANCE_SUBTYPE
    # Every other subtype is passed through untouched.
    for subtype in ("Earnings Release", "Annual Guidance (10-K MD&A)",
                    "Earnings Release - Guidance Raised", "Market News"):
        assert keydev_display_subtype(subtype, "anything at all") == subtype
    assert keydev_display_subtype(None, None) == ""


def test_the_displayed_label_is_always_in_that_screens_filter_domain():
    """A Type the column filter cannot offer would silently drop rows from Excel."""
    from data.screening_service import keydev_subtype_domain
    domain = keydev_subtype_domain((KEYDEV_EARNINGS,))
    if not domain:
        pytest.skip("subtype taxonomy not materialized here")
    assert KEYDEV_NEWS_GUIDANCE_SUBTYPE not in domain
    assert KEYDEV_NEWS_EARNINGS_SUBTYPE in domain
    assert KEYDEV_NEWS_GUIDANCE_SUBTYPE in keydev_subtype_domain((KEYDEV_GUIDANCE,))


# ── Against STG (VPN) ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def stored_total():
    try:
        return count_where(f"e.event_category = '{KEYDEV_EARNINGS_GUIDANCE}'")
    except Exception as exc:                                  # pragma: no cover
        pytest.skip(f"STG not reachable (VPN?): {exc}")


def test_the_halves_partition_the_stored_category(stored_total):
    earnings = count_where(keydev_category_clause((KEYDEV_EARNINGS,)))
    guidance = count_where(keydev_category_clause((KEYDEV_GUIDANCE,)))
    assert earnings + guidance == stored_total
    assert earnings and guidance          # neither half may be empty


def test_both_halves_together_equal_the_old_single_category(stored_total):
    assert count_where(keydev_category_clause(
        (KEYDEV_EARNINGS, KEYDEV_GUIDANCE))) == stored_total


def test_sql_and_python_agree_row_for_row(stored_total):
    """The WHERE fragment and keydev_is_guidance_row must label every row the
    same — the on-disk event index is built from the Python side and queried
    through the SQL side."""
    sample = rows(
        "SELECT e.event_id, e.event_subtype, e.headline FROM coreiq_company_events e "
        f"WHERE e.event_category = '{KEYDEV_EARNINGS_GUIDANCE}' "
        "ORDER BY e.event_id DESC LIMIT 4000")
    assert sample, "no Earnings & Guidance rows came back"
    by_python = {r["event_id"] for r in sample
                 if keydev_is_guidance_row(r["event_subtype"], r["headline"])}
    ids = ", ".join(str(int(r["event_id"])) for r in sample)
    by_sql = {r["event_id"] for r in rows(
        "SELECT e.event_id FROM coreiq_company_events e "
        f"WHERE e.event_id IN ({ids}) "
        f"AND {keydev_category_clause((KEYDEV_GUIDANCE,))}")}
    assert by_python == by_sql


def test_a_layoff_headline_does_not_land_in_guidance():
    """The row the business team reported: 'Kroger to cut 3,000 corporate roles',
    stored as Earnings & Guidance / Guidance Update."""
    hit = rows(
        "SELECT e.event_id FROM coreiq_company_events e "
        "WHERE e.headline LIKE 'Kroger to cut 3,000 corporate roles%' "
        f"AND {keydev_category_clause((KEYDEV_GUIDANCE,))}")
    assert hit == []
    assert rows(
        "SELECT e.event_id FROM coreiq_company_events e "
        "WHERE e.headline LIKE 'Kroger to cut 3,000 corporate roles%' "
        f"AND {keydev_category_clause((KEYDEV_EARNINGS,))}")


def test_guidance_half_carries_no_plain_earnings_subtype():
    found = rows(
        "SELECT DISTINCT e.event_subtype AS s FROM coreiq_company_events e "
        f"WHERE {keydev_category_clause((KEYDEV_GUIDANCE,))}")
    subtypes = [r["s"] for r in found]
    assert subtypes
    assert all("guidance" in s.lower() or "outlook" in s.lower() for s in subtypes)


def test_other_categories_are_untouched_by_the_split():
    """A sibling category passes through, and a mixed pick is exactly additive."""
    since = "2026-01-01"
    plain = count_where("e.event_category IN ('M&A Activity')", since)
    assert plain, "no M&A events in the window — widen it"
    assert count_where(keydev_category_clause(("M&A Activity",)), since) == plain
    mixed = count_where(keydev_category_clause(("M&A Activity", KEYDEV_GUIDANCE)), since)
    guidance = count_where(keydev_category_clause((KEYDEV_GUIDANCE,)), since)
    assert guidance
    assert mixed == plain + guidance
