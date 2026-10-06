#!/usr/bin/env python3
"""Self-check for people_service role classification and criterion filtering.

Run:  .venv/bin/python app/data/test_people_service.py
No framework, no fixtures — asserts only. Every title string below is a real
value taken from coreiq_executives_compensation or a YF companyOfficers payload.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from data.people_service import (
    _FRAME_COLUMNS,
    _collapse_restatements,
    apply_people_criterion,
    build_people_summary,
    classify_role,
    clean_person_name,
    is_former_title,
    person_key,
)
from data.screening_config import DB_SCALE, PEOPLE_MONEY_METRICS, PEOPLE_ROLES


def test_role_classification():
    """Real title strings land in the documented bucket, order traps included."""
    cases = [
        # The ordering trap: both words present, C-level must win.
        ("President and Chief Executive Officer",                      "CEO"),
        ("Chief Executive Officer",                                    "CEO"),
        ("Former Chief Executive Officer Sharon McCollam President and "
         "Chief Financial Officer",                                    "CEO"),
        ("Senior Vice President, Chief Financial Officer",             "CFO"),
        ("Executive Vice President and Chief Financial Officer",       "CFO"),
        ("Chief Operating Officer",                                    "COO"),
        ("Senior Vice President, Chief Operating Officer",             "COO"),
        ("Executive Vice President and Chief Information Officer",     "CIO/CTO"),
        ("Executive Vice President and Chief Technology and "
         "Transformation Officer",                                     "CIO/CTO"),
        ("Senior Vice President, General Counsel and Secretary",       "General Counsel"),
        ("Executive Vice President, General Counsel and Chief "
         "Policy Officer",                                             "General Counsel"),
        ("President and Chief Financial Officer",                      "CFO"),
        ("Executive Vice President, M&A and Corporate Affairs",        "EVP/SVP/VP"),
        ("Senior Vice President, Retail + People",                     "EVP/SVP/VP"),
        # Non-US governance titles from the YF side.
        ("Executive Director",                                         "Board / Exec Director"),
        ("Company Secretary",                                          "Company Secretary"),
        ("Head of Investor Relations",                                 "Investor Relations"),
        ("CEO & MD",                                                   "CEO"),
        ("Financial Controller",                                       "Head of Function"),
        ("Deputy GM & Finance Director",                               "General Manager"),
        # Junk / missing.
        ("Executive",                                                  "Other"),
        ("",                                                           "Unknown"),
        (None,                                                         "Unknown"),
        ("   ",                                                        "Unknown"),
    ]
    for title, expected in cases:
        got = classify_role(title)
        assert got == expected, f"classify_role({title!r}) = {got!r}, want {expected!r}"

    # Every bucket a title can produce must be offered in the selector.
    for title, _ in cases:
        assert classify_role(title) in PEOPLE_ROLES, \
            f"{classify_role(title)!r} missing from PEOPLE_ROLES"
    print("  role classification: OK (%d cases)" % len(cases))


def test_former_detection():
    assert is_former_title("Former Senior Vice President, Chief Financial Officer")
    assert is_former_title("FORMER CEO")
    assert not is_former_title("Senior Vice President, Chief Financial Officer")
    assert not is_former_title("")
    assert not is_former_title(None)
    # 'Reformed' must not false-positive on the \bformer\b word boundary.
    assert not is_former_title("Head of Reformed Operations")
    print("  former detection: OK")


def _frame(rows):
    """Build a people frame with the real column set and dtypes."""
    df = pd.DataFrame(rows, columns=_FRAME_COLUMNS)
    df["is_former"] = df["is_former"].fillna(False).astype(bool)
    for col in PEOPLE_MONEY_METRICS.values():
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["age"] = pd.to_numeric(df["age"], errors="coerce").astype("Int64")
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    for col in ("executive_name", "email", "title", "filing_blob"):
        df[col] = df[col].fillna("").astype(str)
    return df


def _person(**kw):
    base = {c: None for c in _FRAME_COLUMNS}
    base.update({
        "ticker": "AAPL", "executive_name": "Jane Doe", "email": "",
        "title": "Chief Executive Officer", "role": "CEO", "is_former": False,
        "year": 2025, "source": "SEC", "filing_form": "DEF 14A", "filing_blob": "",
    })
    base.update(kw)
    return base


def test_money_filter_boundaries():
    """$mm inputs are scaled by DB_SCALE; NaN never matches; Between is inclusive."""
    df = _frame([
        _person(executive_name="Exactly5",  total_compensation=5 * DB_SCALE),
        _person(executive_name="Just over", total_compensation=5 * DB_SCALE + 1),
        _person(executive_name="Just under", total_compensation=5 * DB_SCALE - 1),
        _person(executive_name="Missing",   total_compensation=None),
    ])

    def names(criterion):
        return sorted(apply_people_criterion(df, criterion)["executive_name"])

    gt = {"money_metric": "Total Compensation", "money_operator": "Greater Than",
          "money_value1": 5.0}
    assert names(gt) == ["Just over"], names(gt)

    gte = dict(gt, money_operator="Greater Than or Equal To")
    assert names(gte) == ["Exactly5", "Just over"], names(gte)

    lt = dict(gt, money_operator="Less Than")
    assert names(lt) == ["Just under"], names(lt)

    lte = dict(gt, money_operator="Less Than or Equal To")
    assert names(lte) == ["Exactly5", "Just under"], names(lte)

    eq = dict(gt, money_operator="Equals")
    assert names(eq) == ["Exactly5"], names(eq)

    between = {"money_metric": "Total Compensation", "money_operator": "Between",
               "money_value1": 4.999999, "money_value2": 5.000001}
    assert names(between) == ["Exactly5", "Just over", "Just under"], names(between)

    # A missing value is never counted as zero.
    assert "Missing" not in names(lt)
    assert "Missing" not in names(gt)

    # No metric selected, or no value entered -> no money filtering at all.
    assert len(apply_people_criterion(df, {"money_metric": None})) == 4
    assert len(apply_people_criterion(
        df, {"money_metric": "Total Compensation", "money_value1": None})) == 4
    print("  money filter boundaries: OK")


def test_db_scale_applied():
    """A $5.0mm threshold compares against 5,000,000 raw dollars, not 5."""
    df = _frame([
        _person(executive_name="Big",   total_compensation=5_000_001),
        _person(executive_name="Small", total_compensation=4_999_999),
    ])
    out = apply_people_criterion(df, {
        "money_metric": "Total Compensation",
        "money_operator": "Greater Than", "money_value1": 5.0,
    })
    assert list(out["executive_name"]) == ["Big"], list(out["executive_name"])
    print("  DB_SCALE applied: OK")


def test_field_filters():
    df = _frame([
        _person(executive_name="Ann Chief",  title="Chief Executive Officer",
                role="CEO", year=2025, age=55, source="SEC", filing_form="DEF 14A"),
        _person(executive_name="Bob Finance", title="Chief Financial Officer",
                role="CFO", year=2024, age=47, source="SEC", filing_form="PRE 14A"),
        _person(executive_name="Cara Merch", title="EVP, Chief Merchandising Officer",
                role="Other C-Suite", year=2025, age=None, source="YFinance",
                filing_form=""),
        _person(executive_name="Dan Gone", title="Former Chief Executive Officer",
                role="CEO", year=2025, age=61, source="SEC", filing_form="DEF 14A",
                is_former=True),
    ])

    def names(criterion):
        # The page always stamps type="people"; an entirely empty dict is the
        # documented no-op and is asserted separately below.
        merged = dict(criterion)
        merged.setdefault("type", "people")
        return sorted(apply_people_criterion(df, merged)["executive_name"])

    # An empty criterion filters nothing at all.
    assert len(apply_people_criterion(df, {})) == 4

    # Former executives are excluded unless explicitly included.
    assert names({}) == ["Ann Chief", "Bob Finance", "Cara Merch"], names({})
    assert names({"include_former": True}) == \
        ["Ann Chief", "Bob Finance", "Cara Merch", "Dan Gone"]

    assert names({"roles": ["CEO"]}) == ["Ann Chief"]
    assert names({"roles": ["CEO", "CFO"]}) == ["Ann Chief", "Bob Finance"]
    assert names({"title_contains": "merchandising"}) == ["Cara Merch"]
    assert names({"name_contains": "finance"}) == ["Bob Finance"]
    assert names({"years": [2024]}) == ["Bob Finance"]
    assert names({"years": [2024, 2025]}) == ["Ann Chief", "Bob Finance", "Cara Merch"]
    assert names({"sources": ["YFinance"]}) == ["Cara Merch"]
    assert names({"filing_forms": ["PRE 14A"]}) == ["Bob Finance"]

    # Age: a missing age is excluded by an age bound, never treated as 0.
    assert names({"age_min": 50}) == ["Ann Chief"]
    assert names({"age_max": 50}) == ["Bob Finance"]
    assert names({"age_min": 40, "age_max": 60}) == ["Ann Chief", "Bob Finance"]

    # title_contains is literal, not a regex — a stray '(' must not explode.
    assert names({"title_contains": "Officer ("}) == []

    # Fields AND together.
    assert names({"roles": ["CEO", "CFO"], "years": [2024]}) == ["Bob Finance"]
    print("  field filters: OK")


def test_criteria_stack_as_and():
    df = _frame([
        _person(executive_name="Ann", role="CEO", year=2025,
                total_compensation=9 * DB_SCALE),
        _person(executive_name="Bob", role="CEO", year=2025,
                total_compensation=1 * DB_SCALE),
        _person(executive_name="Cara", role="CFO", year=2025,
                total_compensation=9 * DB_SCALE),
    ])
    out = df
    for criterion in ({"roles": ["CEO"]},
                      {"money_metric": "Total Compensation",
                       "money_operator": "Greater Than", "money_value1": 5.0}):
        out = apply_people_criterion(out, criterion)
    assert list(out["executive_name"]) == ["Ann"], list(out["executive_name"])
    print("  criteria stack as AND: OK")


def test_no_db_access():
    """apply_people_criterion must never touch the database.

    A DB call on this path would run per criteria edit and defeat the whole
    point of materializing the universe.
    """
    from core import database

    original = database.db_manager.execute_query_readonly

    def explode(*a, **kw):
        raise AssertionError("apply_people_criterion hit the database")

    database.db_manager.execute_query_readonly = explode
    try:
        df = _frame([_person(total_compensation=9 * DB_SCALE)])
        apply_people_criterion(df, {
            "roles": ["CEO"], "years": [2025], "age_min": 30,
            "money_metric": "Total Compensation",
            "money_operator": "Greater Than", "money_value1": 5.0,
            "title_contains": "chief", "name_contains": "jane",
            "sources": ["SEC"], "filing_forms": ["DEF 14A"],
        })
    finally:
        database.db_manager.execute_query_readonly = original
    print("  no DB access on the filter path: OK")


def test_empty_frame_is_safe():
    empty = _frame([])
    assert apply_people_criterion(empty, {"roles": ["CEO"]}).empty
    assert apply_people_criterion(None, {"roles": ["CEO"]}) is None
    print("  empty frame handling: OK")


def test_name_cleaning():
    """Extraction junk is stripped; real names are left alone."""
    cases = [
        ("Richard A. Galanti 8",  "Richard A. Galanti"),   # footnote glued on
        ("Fabrizio Freda ( 1 )",  "Fabrizio Freda"),       # bracketed footnote
        ("Jeffrey Davis \ufeff",  "Jeffrey Davis"),         # BOM
        ("Michael C. Creedon",    "Michael C. Creedon"),   # untouched
        ("Tim Cook",              "Tim Cook"),
        ("",                      ""),
        (None,                    ""),
    ]
    for raw, expected in cases:
        got = clean_person_name(raw)
        assert got == expected, f"clean_person_name({raw!r}) = {got!r}, want {expected!r}"
    print("  name cleaning: OK")


def test_person_key():
    """One person under two spellings matches; two people never do."""
    assert person_key("Mark D. Papermaster") == person_key("Mark Papermaster")
    assert person_key("Scott D. Lipesky") == person_key("Scott Lipesky")
    assert person_key("Richard A. Galanti 8") == person_key("Richard A. Galanti")
    # Apple pays several SVPs identically; they must stay separate people.
    assert person_key("Bruce Sewell") != person_key("Dan Riccio")
    assert person_key("Bruce Sewell") != person_key("Eddy Cue")
    assert person_key("") == ""
    print("  person key: OK")


def test_restatement_collapse():
    """One row per person-year, keeping the best disclosure."""
    rows = [
        # Same person-year filed by three successive proxies + a PRE 14A.
        # The 2018 DEF carries the fullest title; later ones abbreviate it.
        _person(ticker="CLX", executive_name="Laura Stein", year=2018,
                title="Executive Vice President, General Counsel and Corporate Affairs",
                role="General Counsel", filing_year=2018, filing_form="DEF 14A",
                confidence=105, total_compensation=2106867.0),
        _person(ticker="CLX", executive_name="Laura Stein", year=2018,
                title="Executive Vice President", role="EVP/SVP/VP",
                filing_year=2019, filing_form="DEF 14A",
                confidence=105, total_compensation=2106867.0),
        _person(ticker="CLX", executive_name="Laura Stein", year=2018,
                title="Executive Vice President", role="EVP/SVP/VP",
                filing_year=2019, filing_form="PRE 14A",
                confidence=105, total_compensation=2106867.0),
        # Name variant of ONE person, plus a truncated parse on one side.
        _person(ticker="ACI", executive_name="Robert B. Dimond", year=2020,
                title="Chief Financial Officer", role="CFO", filing_year=2020,
                filing_form="DEF 14A", confidence=105, total_compensation=6900.0),
        _person(ticker="ACI", executive_name="Robert Dimond", year=2020,
                title="Chief Financial Officer", role="CFO", filing_year=2020,
                filing_form="DEF 14A", confidence=105, total_compensation=7146900.0),
        # Two DIFFERENT people on identical pay must both survive.
        _person(ticker="AAPL", executive_name="Bruce Sewell", year=2016,
                title="General Counsel", role="General Counsel", filing_year=2016,
                filing_form="DEF 14A", confidence=105, total_compensation=22807544.0),
        _person(ticker="AAPL", executive_name="Dan Riccio", year=2016,
                title="Senior Vice President", role="EVP/SVP/VP", filing_year=2016,
                filing_form="DEF 14A", confidence=105, total_compensation=22807544.0),
    ]
    out = _collapse_restatements(_frame(rows))

    clx = out[(out.ticker == "CLX") & (out.year == 2018)]
    assert len(clx) == 1, f"CLX collapsed to {len(clx)} rows, want 1"
    assert "General Counsel and Corporate Affairs" in clx.iloc[0]["title"], \
        f"kept the abbreviated title: {clx.iloc[0]['title']!r}"

    aci = out[(out.ticker == "ACI") & (out.year == 2020)]
    assert len(aci) == 1, f"ACI collapsed to {len(aci)} rows, want 1"
    assert aci.iloc[0]["total_compensation"] == 7146900.0, \
        f"kept the truncated parse: {aci.iloc[0]['total_compensation']}"

    aapl = out[(out.ticker == "AAPL") & (out.year == 2016)]
    assert len(aapl) == 2, \
        f"two different people on equal pay collapsed to {len(aapl)} rows"

    assert not out.duplicated(subset=["ticker", "executive_name", "year", "source"]).any()
    print("  restatement collapse: OK")


def test_summary():
    s = build_people_summary({
        "roles": ["CEO", "CFO"], "money_metric": "Total Compensation",
        "money_operator": "Greater Than", "money_value1": 5.0,
        "years": [2024, 2025, 2026],
    })
    assert "CEO, CFO" in s and "Total Compensation > $5.0mm" in s and "FY 2024–2026" in s, s
    assert build_people_summary({}) == "All people"
    one_year = build_people_summary({"years": [2025]})
    assert "FY 2025" in one_year and "–" not in one_year, one_year
    print("  summary: OK")


if __name__ == "__main__":
    print("people_service self-check")
    test_role_classification()
    test_former_detection()
    test_name_cleaning()
    test_person_key()
    test_restatement_collapse()
    test_money_filter_boundaries()
    test_db_scale_applied()
    test_field_filters()
    test_criteria_stack_as_and()
    test_no_db_access()
    test_empty_frame_is_safe()
    test_summary()
    print("ALL PASS")
