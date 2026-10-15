#!/usr/bin/env python3
"""Self-check for the 8-K Item 5.02 management-change extractor.

Run:  .venv/bin/python app/data/test_management_changes.py
No framework, no fixtures — asserts only. Every text below is real 5.02 prose
from coreiq_company_events (STG), including the traps the extractor once fell into.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from data.management_changes import extract_events, item_body, split_titles

HEAD = ("[X | 2026-01-01 | SEC 8-K] Executive Changes\n" + "─" * 60 + "\n"
        "Item 5.02 Departure of Directors or Certain Officers; Election of Directors; "
        "Appointment of Certain Officers; Compensatory Arrangements of Certain Officers. ")


def events(text):
    return {(e["person"], e["change"]): e for e in extract_events(HEAD + text)}


def test_departure_with_multiple_titles_and_dates():
    """HAIN 2026-09-28: all three titles kept, notice and effective dates split."""
    got = events(
        "On September 25, 2026, Michael J. Ragusa, the Company’s Senior Vice President, "
        "Chief Accounting Officer and principal accounting officer, informed the Company of "
        "his intention to resign from the Company, effective November 1, 2026, to pursue "
        "another opportunity.")
    e = got[("Michael J. Ragusa", "Departure")]
    assert e["event_type"] == "Resignation"
    assert split_titles(e["title"]) == ["Senior Vice President", "Chief Accounting Officer",
                                        "principal accounting officer"], e["title"]
    assert e["notice_date"] == pd.Timestamp("2026-09-25")
    assert e["effective_date"] == pd.Timestamp("2026-11-01")
    assert "another opportunity" in e["reason"]
    assert e["confidence"] == "High"


def test_appointment_with_succession():
    """CASY 2020-05-13: incoming CFO linked to the person he replaces."""
    got = events(
        "On May 13, 2020, Casey’s General Stores, Inc. (the “Company”) announced that Steve "
        "Bramlage has been appointed as the Company’s Chief Financial Officer, effective June 1, "
        "2020 (the “Effective Date”). Mr. Bramlage will succeed William J. Walljasper, whose "
        "retirement was announced earlier this year.")
    e = got[("Steve Bramlage", "Appointment")]
    assert e["title"] == "Chief Financial Officer" and e["role"] == "CFO"
    assert e["effective_date"] == pd.Timestamp("2020-06-01")
    assert e["replaces"] == "William J. Walljasper"


def test_promotion_keeps_previous_title():
    """PLCE 2021-02-01: the appositive is the old role, the stated title the new one."""
    got = events(
        "On February 1, 2021, The Children’s Place, Inc. announced that Robert F. Helm, Senior "
        "Vice President, Finance & Inventory Management, has been appointed Chief Financial "
        "Officer effective on April 1, 2021.")
    e = got[("Robert F. Helm", "Appointment")]
    assert e["title"] == "Chief Financial Officer"
    assert e["previous_title"].startswith("Senior Vice President, Finance")
    assert e["event_type"] == "Promotion / Role Change"


def test_director_not_standing_is_not_an_election():
    """CAT 2022-02-07: 're-election' once matched the 'elect' appointment verb."""
    got = events(
        "On February 1, 2022, Miles D. White communicated to the Board of Directors of "
        "Caterpillar Inc. his decision not to stand for re-election to the Board at the "
        "Company’s 2022 Annual Meeting of Shareholders.")
    assert ("Miles D. White", "Appointment") not in got
    e = got[("Miles D. White", "Departure")]
    assert e["event_type"] == "Not Standing for Re-election" and e["role"] == "Board"
    assert e["effective_note"] == "at annual meeting"


def test_term_boilerplate_is_not_a_departure():
    """BROS 2024-01-02: 'until her earlier death, resignation, or removal' names no event."""
    got = events(
        "In addition, the Board appointed Christine Barone to the Board, to serve as a director "
        "until the Company’s 2024 annual meeting of stockholders, and until her successor has "
        "been duly elected and qualified, or until her earlier death, resignation, or removal.")
    assert ("Christine Barone", "Departure") not in got
    assert got[("Christine Barone", "Appointment")]["title"] == "Director"


def test_hypothetical_termination_is_ignored():
    """NKE 2018-07-12: a vesting condition is not a departure."""
    got = events(
        "Ms. Cathleen Benko received restricted stock. The shares are subject to forfeiture in "
        "the event that Ms. Benko's service as a director of the Company terminates prior to "
        "the first anniversary of the grant.")
    assert not any(c == "Departure" for _, c in got)


def test_committee_seat_is_not_a_board_appointment():
    """MCD: being appointed to a Board committee is not joining the Board."""
    got = events(
        "On March 1, 2024, Ms. Amy E. Weaver was appointed to the Board’s Audit & Finance "
        "Committee and Governance Committee.")
    assert ("Amy E. Weaver", "Appointment") not in got


def test_compensation_only_filing():
    """TSLA 2017-08-23: a 5.02(e) incentive plan is labelled, not called a change."""
    out = extract_events(HEAD + (
        "(e) On August 18, 2017, Tesla entered into an incentive compensation plan with Jon "
        "McNeill pursuant to which Mr. McNeill will be eligible to receive variable "
        "compensation upon the achievement of certain target levels."))
    assert [e["change"] for e in out] == ["Compensation"], out


def test_short_heading_body_is_kept():
    """TLYS 2023-10-06: the pre-2006 'Principal Officers' heading once emptied the body."""
    text = ("[TLYS | 2023-10-06 | SEC 8-K] x\n" + "─" * 60 + "\nItem 5.02 Departure of Directors "
            "or Principal Officers; Election of Directors; Appointment of Principal Officers\n\n"
            "On October 2, 2023, Bernard Zeichner retired from his position as a member of the "
            "Board of Directors of Tilly’s, Inc.")
    assert item_body(text).startswith("On October 2, 2023")
    e = {x["person"]: x for x in extract_events(text)}["Bernard Zeichner"]
    assert e["event_type"] == "Retirement" and e["role"] == "Board"


def test_stated_age():
    from data.management_changes import stated_age
    assert stated_age("Mr. Bramlage, age 49, served as CFO", "Steve Bramlage") == 49
    assert stated_age("Christine Barone (age: 49) will be appointed", "Christine Barone") == 49
    assert stated_age("Mr. Motley, 62, has served", "David L. Motley") == 62
    assert stated_age("Mr. Smith, the Company’s CFO", "John Smith") is None


def _row(**kw):
    base = dict(ticker="X", filing_date=pd.Timestamp("2020-01-01"), change="Appointment",
                event_type="Appointment", person="Jane Roe", title="Chief Financial Officer",
                titles="Chief Financial Officer", previous_title="", role="CFO", is_board=False,
                notice_date=pd.Timestamp("2020-01-01"), effective_date=pd.NaT, effective_note="",
                replaces="", replaced_by="", reason="", no_disagreement=None, confidence="High",
                age=None, evidence="", source_ref="", event_id=1)
    base.update(kw)
    return base


def test_tenure_pairs_appointment_and_departure():
    from data.management_changes import add_tenure
    df = pd.DataFrame([
        _row(effective_date=pd.Timestamp("2020-03-01"), event_id=1),
        _row(change="Departure", event_type="Resignation", notice_date=pd.Timestamp("2023-02-01"),
             effective_date=pd.Timestamp("2023-03-01"), event_id=2),
        _row(person="Open Role", effective_date=pd.Timestamp("2024-01-01"), event_id=3),
    ])
    out = add_tenure(df, today=pd.Timestamp("2026-01-01")).set_index("event_id")
    assert out.loc[1, "years_in_role"] == 3.0 and out.loc[2, "years_in_role"] == 3.0
    assert out.loc[2, "role_start"] == pd.Timestamp("2020-03-01")
    assert pd.isna(out.loc[3, "role_end"]) and out.loc[3, "years_in_role"] == 2.0


def test_move_needs_old_company_named_in_new_filing():
    """Same name at two companies is a move only if the new filing names the old one."""
    from data.management_changes import mark_moves
    df = pd.DataFrame([
        _row(ticker="LOW", change="Departure", event_type="Resignation",
             notice_date=pd.Timestamp("2026-04-01"), event_id=1, person="David Denton"),
        _row(ticker="NKE", notice_date=pd.Timestamp("2026-06-23"), event_id=2, person="David Denton"),
        _row(ticker="TMUS", change="Departure", notice_date=pd.Timestamp("2021-09-10"),
             event_id=3, person="David Miller"),
        _row(ticker="PLBY", notice_date=pd.Timestamp("2022-02-22"), event_id=4, person="David Miller"),
    ])
    bodies = {2: "Mr. Denton served as Chief Financial Officer of Lowe’s Companies, Inc.",
              4: "Mr. Miller previously led Playboy’s licensing business."}
    names = {"LOW": "Lowe's Companies, Inc.", "TMUS": "T-Mobile US, Inc."}
    out = mark_moves(df, bodies, names).set_index("event_id")
    assert out.loc[2, "moved_from"] == "LOW"
    assert out.loc[4, "moved_from"] == ""

    # Denton: Lowe's -> Pfizer (2022) -> Nike (2026). Nike's bio still names Lowe's,
    # but the Nike appointment is not a move from Lowe's.
    chain = pd.DataFrame([
        _row(ticker="LOW", change="Departure", notice_date=pd.Timestamp("2022-04-04"), event_id=1, person="David Denton"),
        _row(ticker="PFE", notice_date=pd.Timestamp("2022-04-11"), event_id=2, person="David Denton"),
        _row(ticker="NKE", notice_date=pd.Timestamp("2026-06-23"), event_id=3, person="David Denton"),
    ])
    out = mark_moves(chain, {2: "formerly CFO of Lowe's", 3: "CFO of Pfizer and before that Lowe's"},
                     {"LOW": "Lowe's Companies, Inc.", "PFE": "Pfizer Inc."}).set_index("event_id")
    assert out.loc[2, "moved_from"] == "LOW" and out.loc[3, "moved_from"] == "", out[["moved_from"]]


def test_move_pay_comparison():
    from data.management_changes import compare_move_pay
    moves = pd.DataFrame([_row(ticker="NKE", moved_from="LOW", person="David Denton",
                               moved_from_date=pd.Timestamp("2026-04-01"),
                               notice_date=pd.Timestamp("2026-06-23"))])
    people = pd.DataFrame({
        "ticker": ["LOW", "LOW", "NKE"], "executive_name": ["David M. Denton"] * 3,
        "year": [2025, 2026, 2026], "total_compensation": [10.0, 99.0, 15.0],
        "total_pay": [None, None, None]})
    out = compare_move_pay(moves, people).iloc[0]
    assert out["prior_pay"] == 99.0 and out["new_pay"] == 15.0
    assert out["pay_change_pct"] == round((15 / 99 - 1) * 100, 1)


def test_company_name_is_not_a_person():
    from data.management_changes import is_company_not_person
    assert is_company_not_person("Lithia Motors", "lithia", "Lithia Motors, Inc. announced that a director resigned.")
    assert is_company_not_person("Honeywell Aerospace", "honeywell", "elected Mr. Madsen to lead Honeywell Aerospace")
    # Wendy's: the person shares the company's first word but IS a person.
    assert not is_company_not_person("Wendy C. Arlin", "wendy", "elected Ms. Arlin as a director")
    assert not is_company_not_person("John Smith", "lithia", "Mr. Smith resigned")


def test_news_headlines():
    """Real Executive News headlines (coreiq_company_events, 2026-10-15)."""
    from data.management_changes import parse_news

    def one(headline, company, summary=""):
        got = parse_news(headline, summary, company)
        return (got[0]["change"], got[0]["person"], got[0]["role"] if "role" in got[0] else got[0]["title"]) if got else None

    got = parse_news("Smith & Wesson Brands, Inc. Names Deana L. McPherson as CFO", "", "Smith & Wesson Brands, Inc.")
    assert got and got[0]["person"] == "Deana L. McPherson" and got[0]["title"] == "CFO", got
    assert one("Karen Lynch tapped as new CEO of CVS", "CVS Health")[:2] == ("Appointment", "Karen Lynch")
    assert one("eBay names Jamie Iannone Chief Executive Officer", "eBay Inc.")[:2] == ("Appointment", "Jamie Iannone")
    assert one("Jennifer LaClair steps down as Ally Financial CFO", "Ally Financial Inc.")[:2] == ("Departure", "Jennifer LaClair")
    assert one("La-Z-Boy Q3 Profit Down; CEO Kurt Darrow To Retire", "La-Z-Boy Incorporated")[:2] == ("Departure", "Kurt Darrow")
    got = parse_news("Lululemon names former Nike exec Heidi O’Neill as next CEO", "", "lululemon athletica inc.")
    assert got and got[0]["person"] == "Heidi O'Neill", got
    got = parse_news("Intel appoints Lip-Bu Tan as CEO to orchestrate turnaround at struggling chipmaker", "", "Intel Corporation")
    assert got and got[0]["title"] == "CEO", got
    got = parse_news("Intel Appoints Tech Industry Leader Pat Gelsinger as New CEO", "", "Intel Corporation")
    assert got and got[0]["person"] == "Pat Gelsinger", got
    two = parse_news("Dine Brands Appoints John Dillon as President of Denny's and David Schmidt as President of Keke's",
                     "", "Dine Brands Global")
    assert [e["person"] for e in two] == ["John Dillon", "David Schmidt"], two
    # Another company's change filed under this ticker.
    assert one("Northern Tool + Equipment taps Sleep Number exec as new CEO", "Sleep Number Corporation") is None
    assert one("Lumenci Appoints Former Qualcomm President Derek Aberle to iLumOS Advisory Board", "QUALCOMM Incorporated") is None
    assert one("Steve Menneto departs Polaris, named Malibu Boats CEO", "Malibu Boats, Inc.") is None
    assert one("Redwood City-based Imperva taps former Oracle, Infor exec as new CEO", "Oracle Corporation") is None
    assert one("Home Depot CEO Takes Temporary Medical Leave, Company Announces Interim Management Plan",
               "The Home Depot, Inc.") is None
    # Not a change at all.
    assert one("Insider trades: P&G, Marvell Technology, TSMC among notable names (PG:NYSE)", "Procter & Gamble") is None
    assert one("Clorox Announces November 2 Webcast of First-Quarter Fiscal Year 2027 Results", "The Clorox Company") is None
    # The headline names the role only: the person comes from the summary.
    got = parse_news("Dollar General names COO",
                     "Dollar General Corporation announced that Jeff Owen has been appointed Chief Operating Officer, "
                     "effective immediately.", "Dollar General Corporation")
    assert got and got[0]["person"] == "Jeff Owen" and got[0]["confidence"] == "Low", got


def test_news_repeats_fold_into_filing():
    from data.management_changes import CHANGE_COLUMNS, _fold_repeats
    day = pd.Timestamp("2026-03-02")

    def row(source, when, person="Dana McNabb"):
        r = dict.fromkeys(CHANGE_COLUMNS, "")
        r.update(ticker="GIS", person=person, change="Appointment", notice_date=when, source=source)
        return r

    filings = pd.DataFrame([row("SEC 8-K", day)])
    news = pd.DataFrame([row("Reuters", day + pd.Timedelta(days=1)),
                         row("CNBC", day + pd.Timedelta(days=60)),
                         row("Reuters", day, person="Someone Else")])
    filings, rest = _fold_repeats(news, primary=filings)
    assert filings.at[0, "also_reported"] == "Reuters"
    assert sorted(rest["source"]) == ["CNBC", "Reuters"] and len(rest) == 2



def test_wording_the_extractor_used_to_miss():
    """Real 5.02 prose that yielded no person before 2026-10-16."""
    # KMB 2026-10-01: bare "depart", "the Corporation".
    got = events("On September 29, 2026, Russell Torres, the Corporation’s President and Chief "
                 "Operating Officer, notified the Corporation that he will depart the Corporation "
                 "effective November 2, 2026 to pursue other opportunities.")
    assert ("Russell Torres", "Departure") in got
    # KBH 2026-01-07: "not to seek re-election" (no "for").
    got = events("(b) On January 6, 2026, director James C. Weaver informed KB Home of his decision "
                 "not to seek re-election at KB Home’s 2026 Annual Meeting of Stockholders. Mr. Weaver "
                 "will carry out the remainder of his term.")
    assert got[("James C. Weaver", "Departure")]["event_type"] == "Not Standing for Re-election"
    # GO 2022-02-02: accented name.
    got = events("On January 28, 2022, María Fernanda Mejía informed Grocery Outlet Holding Corp. of her "
                 "decision, effective February 2, 2022, to resign as a Class III member of the Board.")
    assert any(p.endswith("Mejía") and c == "Departure" for p, c in got)
    # ZUMZ 2021-06-02: honorific on the full name.
    got = events("(d) Departure of Director Effective as of June 3, 2021, Ms. Sarah G. McCoy has resigned "
                 "from the Board of Directors of Zumiez Inc. Ms. McCoy will remain an advisor.")
    assert ("Sarah G. McCoy", "Departure") in got
    # CTRN 2025-01-21: "is leaving the Company".
    got = events("On January 21, 2025, Citi Trends, Inc. announced that Vivek Bhargava, Senior Vice "
                 "President of Supply Chain, is leaving the Company, effective January 21, 2025.")
    assert ("Vivek Bhargava", "Departure") in got
    # PEP 2026-09-17: a board seat with no title is a director appointment.
    got = events("On September 17, 2026, the Board of Directors of PepsiCo, Inc. elected Joaquin Duato as "
                 "an independent member of the Board, effective December 1, 2026. Mr. Duato will serve "
                 "on the Audit Committee of the Board, effective December 1, 2026.")
    assert got[("Joaquin Duato", "Appointment")]["role"] == "Board"


def test_possessive_company_is_not_a_person():
    """VZ 2025-10-04: "Mr. Vestberg’s Verizon Board" once displaced the new CEO."""
    got = events("On October 4, 2025, the Verizon Board of Directors appointed Daniel H. Schulman as "
                 "Chief Executive Officer, effective immediately, and Hans E. Vestberg stepped down as "
                 "Chairman and Chief Executive Officer. Mr. Vestberg’s Verizon equity awards will vest. "
                 "Mr. Schulman, 67, joined the Board in 2018.")
    assert ("Daniel H. Schulman", "Appointment") in got
    assert not any("Verizon" in p for p, _ in got)


def test_news_filed_under_the_former_employer():
    """The feed files "Five Below Appoints Former Ulta Beauty CFO ..." under ULTA."""
    from data.management_changes import build_news_changes
    names = {"ULTA": "Ulta Beauty, Inc.", "FIVE": "Five Below, Inc."}
    rows = [{"event_id": 1, "ticker": "ULTA", "event_date": "2026-09-24", "source": "AV News — Reuters",
             "headline": "Five Below Appoints Former Ulta Beauty CFO Scott Settersten to Board",
             "situation": "", "source_ref": ""}]
    df = build_news_changes(rows, names)
    assert list(zip(df["ticker"], df["person"])) == [("FIVE", "Scott Settersten")]

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
