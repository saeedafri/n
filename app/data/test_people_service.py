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
    _yf_records,
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
    for col in ("executive_name", "title", "filing_blob"):
        df[col] = df[col].fillna("").astype(str)
    return df


def _person(**kw):
    base = {c: None for c in _FRAME_COLUMNS}
    base.update({
        "ticker": "AAPL", "executive_name": "Jane Doe",
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


def test_name_keys_ignore_honorifics_and_title_words():
    """Honorifics, suffixes and titles glued onto a name are not the person.

    Yahoo prefixes every officer with Mr./Ms., so first+last token became
    'mr|arnault' and merged Bernard Arnault with his three sons. SEC rows glue
    the title onto the name ('Brian T. Olsavsky SVP and').
    """
    assert person_key("Mr. Antoine Arnault") != person_key("Mr. Bernard Arnault")
    assert person_key("Mr. Kazumi Yanai") != person_key("Mr. Tadashi Yanai")
    assert person_key("Mr. Tae-Moon  Roh Ph.D.") == person_key("Tae-Moon Roh")
    assert person_key("Ms. Karine Lenglart") == person_key("Karine Lenglart")
    # Full middle names are part of who someone is: two directors of one
    # Malaysian company, two of one Hong Kong company.
    assert person_key("Mr. See Long Soon") != person_key("Mr. See Beng Soon")
    assert person_key("Mr. Siu Wa Wong") != person_key("Mr. Siu Man Wong")
    for glued, plain in [
        ("Brian T. Olsavsky SVP and", "Brian T. Olsavsky"),
        ("Franck J. Moison Retired", "Franck J. Moison"),
        ("Martin P. Waters CEO/President", "Martin P. Waters"),
        ("Brian K. Robbins –", "Brian K. Robbins—"),
        ("Stuart A. Levy*", "Stuart A. Levy"),
        ("Kenneth Canestrari SEVP", "Kenneth Canestrari"),
        ("Jeffrey M. Williams VP &", "Jeffrey M. Williams"),
    ]:
        assert person_key(glued) == person_key(plain), (glued, person_key(glued), person_key(plain))
        assert clean_person_name(glued).rstrip("—–- *") == clean_person_name(plain).rstrip("—–- *"), \
            (glued, clean_person_name(glued))
    assert person_key("Jeffrey M. Williams VP &") == person_key("Jeffrey Williams")
    # Generational suffixes are dropped from the key, the name keeps them.
    assert person_key("John J. Donahoe II") == person_key("John Donahoe")
    assert clean_person_name("Harry A. Lawton III") == "Harry A. Lawton III"
    # A 'name' that is only a title is left alone rather than emptied.
    assert clean_person_name("Technical Advisor") == "Technical Advisor"
    print("  honorifics / title words in names: OK")


def test_nicknames_merge_only_on_equal_pay():
    """Mike / R. Michael Mohan on identical pay is one person; Olsavsky and
    Zapolsky on identical pay are two (Amazon pays its SVPs the same)."""
    rows = [
        _person(ticker="BBY", executive_name="Mike Mohan", year=2018, filing_year=2019,
                total_compensation=6233740.0),
        _person(ticker="BBY", executive_name="R. Michael Mohan", year=2018, filing_year=2020,
                total_compensation=6233740.0),
        _person(ticker="BBY", executive_name="R. Michael Mohan", year=2019, filing_year=2020,
                total_compensation=6693765.0),
        _person(ticker="WSM", executive_name="Karalyn Smith", year=2023, filing_year=2024,
                total_compensation=3312133.0),
        _person(ticker="WSM", executive_name="Karalyn Yearout", year=2023, filing_year=2025,
                total_compensation=3312133.0),
        # Names reach the collapse already cleaned by _sec_records().
        _person(ticker="AMZN", executive_name=clean_person_name("Brian T. Olsavsky SVP and"),
                year=2020, filing_year=2021, total_compensation=17174185.0),
        _person(ticker="AMZN", executive_name=clean_person_name("David A. Zapolsky SVP"), year=2020,
                filing_year=2021, total_compensation=17174185.0),
        # Same surname, different pay: two people (a family on one proxy).
        _person(ticker="DDS", executive_name="Alex Dillard", year=2020, filing_year=2021,
                total_compensation=3467727.0),
        _person(ticker="DDS", executive_name="Mike Dillard", year=2020, filing_year=2021,
                total_compensation=1965821.0),
    ]
    out = _collapse_restatements(_frame(rows))
    bby = out[out.ticker == "BBY"]
    assert len(bby) == 2, bby[["executive_name", "year"]]
    # One display name for the person in every year: the fullest spelling.
    assert set(bby.executive_name) == {"R. Michael Mohan"}, set(bby.executive_name)
    assert len(out[out.ticker == "WSM"]) == 1
    amzn = out[out.ticker == "AMZN"]
    assert len(amzn) == 2 and set(amzn.executive_name) == {"Brian T. Olsavsky", "David A. Zapolsky"}, \
        list(amzn.executive_name)
    assert len(out[out.ticker == "DDS"]) == 2
    print("  nickname / married-name merge on equal pay: OK")


def test_broken_pay_rows():
    """Rows the proxy extractor got wrong never reach the screen as fact."""
    rows = [
        # Footnote text parsed as a row: one number in every column.
        _person(ticker="ZBRA", executive_name="Cristen Kogl", year=2020, filing_year=2022,
                salary=11100484.0, bonus=11100484.0, stock_awards=11100484.0,
                total_compensation=11100484.0),
        # Smeared value: the stock award also landed in the blank Bonus cell.
        _person(ticker="AAPL", executive_name="Tim Cook", year=2023, filing_year=2024,
                salary=3000000.0, bonus=46970283.0, stock_awards=46970283.0,
                non_equity_incentive=10713450.0, all_other_compensation=2526112.0,
                total_compensation=63209845.0),
        # Two copies; the one whose total is below salary loses.
        _person(ticker="BBY", executive_name="Hubert Joly", year=2019, filing_year=2020,
                salary=866346.0, stock_awards=9415809.0, total_compensation=39424.0),
        _person(ticker="BBY", executive_name="Hubert Joly", year=2019, filing_year=2021,
                salary=866346.0, stock_awards=9415809.0, total_compensation=14239424.0),
        # Only copy, total impossible (below salary): total withheld, parts kept.
        # The filed row's last figure is the same mis-split number, so it is no rescue.
        _person(ticker="BBWI", executive_name="James L. Bersani", year=2021, filing_year=2022,
                salary=832308.0, bonus=1500000.0, total_compensation=90250.0, raw_total=90250.0),
        # Parts exceed total by 3.9% because of a NEGATIVE pension change the
        # table has no column for. That total is real and must stay.
        _person(ticker="CAL", executive_name="Daniel R. Friedman", year=2022, filing_year=2023,
                salary=492000.0, stock_awards=325500.0, non_equity_incentive=526071.0,
                all_other_compensation=13080.0, total_compensation=1305345.0),
        # PepsiCo's table splits non-equity pay into sub-columns; the extractor
        # took the SUBTOTAL as the total. The filed row ends with the real one.
        _person(ticker="PEP", executive_name="Ramon L. Laguarta", year=2024, filing_year=2025,
                salary=1763462.0, stock_awards=11005571.0, non_equity_incentive=3375000.0,
                all_other_compensation=537984.0, total_compensation=6766500.0,
                raw_total=28814759.0),
    ]
    out = _collapse_restatements(_frame(rows)).set_index("ticker")
    assert out.loc["PEP", "total_compensation"] == 28814759.0, out.loc["PEP", "total_compensation"]
    assert "raw_total" not in out.columns
    assert "ZBRA" not in out.index, "footnote row survived"
    cook = out.loc["AAPL"]
    assert pd.isna(cook["bonus"]) and cook["stock_awards"] == 46970283.0, (cook["bonus"], cook["stock_awards"])
    assert cook["total_compensation"] == 63209845.0
    assert out.loc["BBY", "total_compensation"] == 14239424.0, out.loc["BBY", "total_compensation"]
    assert pd.isna(out.loc["BBWI", "total_compensation"]) and out.loc["BBWI", "salary"] == 832308.0
    assert out.loc["CAL", "total_compensation"] == 1305345.0
    print("  broken pay rows: OK")


def test_raw_total_parsing():
    """The last thousands-separated figure in the filed row is its total."""
    from data.people_service import last_row_amount
    assert last_row_amount("Ramon L. Laguarta Chairman 2024 1,763,462 — 11,005,571 "
                           "3,375,000 3,391,500 6,766,500 8,741,242 537,984 28,814,759") == 28814759.0
    assert last_row_amount("Richard F. Westenberger 2017 $ 591,135 — $ 1,516’438") == 1516438.0
    assert last_row_amount("Daniel R. Friedman 2022 $ 492,000 $ (51,306)") is None   # negative
    assert last_row_amount("Tim Cook 2023 (3)") is None and last_row_amount(None) is None
    print("  raw-row total parsing: OK")


def test_yf_pay_converted_to_usd():
    """Yahoo totalPay is in the company's own currency; Total Pay is USD."""
    import json
    payload = lambda cur, officers: json.dumps({"info": {"financialCurrency": cur, "companyOfficers": officers}})
    rows = [
        {"ticker": "005930", "payload_json": payload("KRW", [
            {"name": "Mr. Tae-Moon Roh", "title": "CEO", "fiscalYear": 2024, "totalPay": 5_099_000_000,
             "exercisedValue": 0, "unexercisedValue": 0}])},
        {"ticker": "2321", "payload_json": payload("MYR", [
            {"name": "Mr. See Beng Soon", "title": "Executive Chairman & CEO", "fiscalYear": 2025,
             "totalPay": 5_337_258}])},
        {"ticker": "QVCGA", "payload_json": payload("USD", [
            {"name": "Mr. David Rawlinson", "title": "CEO", "fiscalYear": 2025, "totalPay": 7_000_000,
             "exercisedValue": 279_758}])},
    ]
    fx = {"KRW": {2023: 1300.0, 2024: 1400.0}}
    recs = {r["ticker"]: r for r in _yf_records(rows, fx)}
    roh = recs["005930"]
    assert roh["pay_currency"] == "KRW" and roh["total_pay_local"] == 5_099_000_000
    assert abs(roh["total_pay"] - 5_099_000_000 / 1400.0) < 1, roh["total_pay"]
    assert roh["exercised_value"] is None and roh["unexercised_value"] is None, "Yahoo 0 shown as $0"
    soon = recs["2321"]   # no MYR rate on file: local kept, USD left blank
    assert soon["total_pay"] is None and soon["total_pay_local"] == 5_337_258 and soon["pay_currency"] == "MYR"
    usd = recs["QVCGA"]
    assert usd["total_pay"] == 7_000_000 and usd["exercised_value"] == 279_758
    print("  YF pay converted to USD: OK")


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
    test_name_keys_ignore_honorifics_and_title_words()
    test_nicknames_merge_only_on_equal_pay()
    test_broken_pay_rows()
    test_raw_total_parsing()
    test_yf_pay_converted_to_usd()
    test_money_filter_boundaries()
    test_db_scale_applied()
    test_field_filters()
    test_criteria_stack_as_and()
    test_no_db_access()
    test_empty_frame_is_safe()
    test_summary()
    print("ALL PASS")
