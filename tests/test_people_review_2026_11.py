"""Regression tests: every case from the data team's review of the four People
exports (2026-11-13). Real rows and filing text, trimmed. Pure functions only —
no DB, no Streamlit. See
docs/superpowers/specs/2026-11-13-people-exports-data-review-design.md."""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from data.layoff_events import _NOT_A_CUT, is_layoff_news, lead_owners, led_by_other_company  # noqa: E402
from data.management_changes import (extract_events, mark_moves, parse_news,  # noqa: E402
                                     split_titles)
from data.people_service import (_drop_copied_rows, _fix_copied_pay, _owner_by_title, canonical_ticker,
                                 company_name_key, derive_ticker_aliases, classify_role,  # noqa: E402
                                 clean_person_name, clean_title, company_display_name, person_id,
                                 person_key, row_owner, smart_title)
from data.proxy_pay import parse_summary_compensation  # noqa: E402

HEAD = ("[X | 2026-01-01 | SEC 8-K] Executive Changes\n" + "─" * 60 + "\n"
        "Item 5.02 Departure of Directors or Certain Officers; Election of Directors; "
        "Appointment of Certain Officers; Compensatory Arrangements of Certain Officers. ")


# --- People Screening: whose row is it -------------------------------------

def test_row_filed_under_previous_executive_goes_to_its_owner():
    # Lowe's 2025 proxy: McFarland's figures stored under Brandon J. Sink.
    name, title = row_owner(
        "Brandon J. Sink",
        "Executive Vice President, Chief Financial Officer Joseph M. McFarland III Executive Vice President, Stores",
        "Joseph M. McFarland III Executive Vice President, Stores 2024 875,815 — 3,504,080 1,097,388 860,401 13,829 6,351,513")
    assert (name, title) == ("Joseph M. McFarland III", "Executive Vice President, Stores")
    # Lowe's 2024 proxy: same fault, position not run together.
    name, title = row_owner("Brandon J. Sink", "Executive Vice President, Chief Financial Officer",
                            "Joseph M. McFarland III Executive Vice President, Stores 2023 857,704 —")
    assert name == "Joseph M. McFarland III" and title == "Executive Vice President, Stores"
    # Albertsons: the CFO's pay under the former CEO.
    assert row_owner("Vivek Sankaran", "Chief Executive Officer Sharon McCollam President and Chief Financial Officer",
                     "Sharon McCollam President and Chief Financial Officer 2023 1,000,000")[0] == "Sharon McCollam"
    # The row's own name, a bare year row, or a title lead: unchanged.
    assert row_owner("Marvin R. Ellison", "Chairman", "Marvin R. Ellison Chairman 2024 1,494,231")[0] == "Marvin R. Ellison"
    assert row_owner("Carl Ford", "EVP, CFO", "2024 521,635 — 1,249,882")[0] == "Carl Ford"
    assert row_owner("Hillary Super", "CEO", "Chief Executive Officer 2024 481,319")[0] == "Hillary Super"


def test_row_whose_text_starts_with_a_colleagues_title():
    # Victoria's Secret 2024: Sekella's rows carry McAfee's figures.
    df = pd.DataFrame({
        "ticker": ["VSXY"] * 3,
        "executive_name": ["Scott Sekella", "Melinda McAfee", "Scott Sekella"],
        "title": ["Chief Financial and Operating Officer Chief Human Resources and Legal Officer",
                  "Chief Human Resources and Legal Officer", "Chief Financial and Operating Officer"],
        "raw_lead": ["Chief Human Resources and Legal Officer", "Melinda McAfee", ""],
        "raw_position": ["Chief Financial and Operating Officer Chief Human Resources and Legal Officer",
                         "Chief Human Resources and Legal Officer", "Chief Financial and Operating Officer"],
        "role": ["CFO"] * 3, "is_former": [False] * 3})
    out = _owner_by_title(df)
    assert out["executive_name"].tolist() == ["Melinda McAfee", "Melinda McAfee", "Scott Sekella"]
    assert out.at[0, "title"] == "Chief Human Resources and Legal Officer"
    # Lowe's 2026 proxy: restated rows start with McFarland's title, and "EVP,
    # CFO" + "EVP, Stores" is two titles, not two wordings of one.
    low = pd.DataFrame({
        "ticker": ["LOW"] * 2, "executive_name": ["Brandon J. Sink", "Joseph M. McFarland III"],
        "title": [clean_title("Executive Vice President, Chief Financial Officer Executive Vice President, Stores"),
                  "Executive Vice President, Stores"],
        "raw_lead": ["Executive Vice President, Stores", "Joseph M. McFarland III"],
        "raw_position": ["Executive Vice President, Chief Financial Officer Executive Vice President, Stores",
                         "Executive Vice President, Stores"],
        "role": ["CFO"] * 2, "is_former": [False] * 2})
    assert _owner_by_title(low)["executive_name"].tolist() == ["Joseph M. McFarland III"] * 2


# --- People Screening: names and titles -------------------------------------

def test_titles_lose_footnotes_bio_years_and_second_people():
    cases = {
        "Former Chief Product Officer Aggregate grant date fair values are computed in accordance with ASC Topic 718.":
            "Former Chief Product Officer",                                    # Maplebear / Danker
        "Chairman and Chief Executive Officer, Macy’s, Inc. Age: 60 Director Since: 2023":
            "Chairman and Chief Executive Officer, Macy’s, Inc.",
        "President, Brands 2024 2023": "President, Brands",
        "President and Chief Legal Officer 6": "President and Chief Legal Officer",
        "Executive Vice President, Chief Technology Officer (through January 13, 2025)":
            "Executive Vice President, Chief Technology Officer",
        "Chief Accounting Officer and the Company’s principal accounting officer": "Chief Accounting Officer",
        "Executive Vice President – U.S. Stores Carol B. Tomé Former Chief Financial Officer":
            "Executive Vice President – U.S. Stores",
        "Drive product innovation and deliver improved customer Executive Vice President and GM, Digital Marketing":
            "Executive Vice President and GM, Digital Marketing",
        "EVP and Chief Operating Officer (COO) EVP and COO Executive Vice President and Chief Operating Officer":
            "Executive Vice President and Chief Operating Officer",
        "Senior Vice President &": "Senior Vice President",
        "Jr.* Former Chief Financial Officer": "Former Chief Financial Officer",
        "Principal Executive Officer": "Principal Executive Officer",
    }
    for raw, want in cases.items():
        assert clean_title(raw) == want, (raw, clean_title(raw))


def test_names_drop_honorifics_and_split_apostrophes():
    assert clean_person_name("Mr. Timothy D. Cook") == "Timothy D. Cook"
    assert person_key("Julie A. D’ Emilio") == person_key("Julie A. D'Emilio")
    assert person_id("Anthony DiSilvestro") == person_id("Anthony P. DiSilvestro") != ""


def test_role_buckets_match_abbreviations():
    for spelled, short in [("Chief Human Resources Officer", "EVP, CHRO"),
                           ("Chief Accounting Officer", "SVP, CAO"), ("Chief Marketing Officer", "EVP & CMO")]:
        assert classify_role(spelled) == classify_role(short) == "Other C-Suite", (spelled, short)


# --- Companies: one name, one ticker ---------------------------------------

def test_company_names_are_company_names():
    assert company_display_name("KFC, Pizza Hut, Taco Bell", "Yum! Brands, Inc.", "YUM") == "Yum! Brands, Inc."
    assert company_display_name("Olive Garden, LongHorn Steakhouse", "Darden Restaurants, Inc.", "DRI") \
        == "Darden Restaurants, Inc."
    assert company_display_name("Square", "Block, Inc. (Square)", "XYZ") == "Block, Inc."
    assert company_display_name("ELI LILLY & Co.", "ELI LILLY & Co", "LLY") == "Eli Lilly & Co."
    assert smart_title("SIGNET JEWELERS LTD") == "Signet Jewelers Ltd"
    assert company_display_name("Abercrombie & Fitch Co.", "ABERCROMBIE & FITCH CO /DE/", "ANF") == "Abercrombie & Fitch Co."


def test_one_ticker_per_company_found_from_the_database():
    # Rows as coreiq_sec_companies_all / coreiq_companies hold them (STG, 2026-11-13).
    listings = [
        {"ticker": "CCZ", "cik": "0001166691", "name": "COMCAST CORP"},
        {"ticker": "CMCSA", "cik": "0001166691", "name": "COMCAST CORP"},
        {"ticker": "VSCO", "cik": "0001856437", "name": "Victoria's Secret & Co."},
        {"ticker": "VSXY", "cik": "0001856437", "name": "Victoria's Secret"},
        {"ticker": "AFG", "cik": "0001042046", "name": "AMERICAN FINANCIAL GROUP INC"},
        {"ticker": "AFGC", "cik": "0001042046", "name": "AMERICAN FINANCIAL GROUP INC"},
        {"ticker": "AACB", "cik": "0002034334", "name": "Artius II Acquisition Inc."},
        {"ticker": "AACBR", "cik": "0002034334", "name": "Artius II Acquisition Inc."},
        {"ticker": "AACBU", "cik": "0002034334", "name": "Artius II Acquisition Inc."},
        # One CIK, two unrelated names: a data error, never merged.
        {"ticker": "CRI", "cik": "0001060822", "name": "CARTER'S INC"},
        {"ticker": "GIL", "cik": "0001060822", "name": "GILDAN ACTIVEWEAR INC"},
    ]
    coverage = {"VSXY": "Public"}
    listed = {"CMCSA": (True, 1.06e11), "CCZ": (True, 5.8e10), "AFG": (True, 1.03e10), "AFGC": (False, 1.8e7),
              "AACBU": (True, 2.7e8)}
    got = derive_ticker_aliases(listings, coverage, {}, listed)
    assert got == {"CCZ": "CMCSA", "VSCO": "VSXY", "AFGC": "AFG", "AACBR": "AACB", "AACBU": "AACB"}, got
    # What the database cannot tell stays in ticker_aliases.json.
    assert canonical_ticker("6288") == "9983" and canonical_ticker("QRTEA") == "QVCPQ"
    assert company_name_key("Block, Inc. (Square)") == company_name_key("Block, Inc.") == "block"


# --- Proxy parser -----------------------------------------------------------

_SCT = ("<table><tr><td>Name and Principal Position</td><td>Year</td><td>Salary ($)</td><td>Bonus ($)</td>"
        "<td>Stock Awards ($)</td><td>Total ($)</td></tr>{rows}</table>")


def test_parser_wrong_rowspan_does_not_merge_two_executives():
    # Tesla 2026: Musk's name cell spans three rows but he has one; Taneja follows.
    rows = ('<tr><td rowspan="3">Elon Musk<br>Technoking of Tesla and Chief Executive Officer</td>'
            '<td>2025</td><td>—</td><td>—</td><td>158,359,009,867</td><td>158,359,009,867</td></tr>'
            '<tr><td rowspan="3">Vaibhav Taneja<br>Chief Financial Officer</td>'
            '<td>2025</td><td>400,000</td><td>—</td><td>—</td><td>403,000</td></tr>'
            '<tr><td>2024</td><td>303,846</td><td>—</td><td>139,169,089</td><td>139,472,935</td></tr>'
            '<tr><td>2023</td><td>275,000</td><td>—</td><td>—</td><td>278,000</td></tr>')
    got = {(r["executive_name"], r["year"]): r["total_compensation"]
           for r in parse_summary_compensation(_SCT.format(rows=rows))}
    assert got == {("Elon Musk", 2025): 158_359_009_867, ("Vaibhav Taneja", 2025): 403_000,
                   ("Vaibhav Taneja", 2024): 139_472_935, ("Vaibhav Taneja", 2023): 278_000}, got


def test_parser_years_stacked_one_line_apart():
    # Arhaus 2026: "2025\n2024\n2023" in one cell, figures stacked the same way.
    rows = ("<tr><td>Dawn Phillipson<br><br>Former Chief Financial Officer</td>"
            "<td>2025<br>2024<br>2023</td><td>47,380<br>489,992<br>474,862</td><td>—<br>—<br>—</td>"
            "<td>—<br>200,000<br>700,000</td><td>49,394<br>895,955<br>1,412,985</td></tr>"
            "<tr><td>Kathy Veltri<br><br>Chief Retail Officer</td><td>2025</td><td>543,394</td><td>—</td>"
            "<td>2,500,000</td><td>3,376,058</td></tr>")
    got = {(r["executive_name"], r["year"]): (r["title"], r["total_compensation"])
           for r in parse_summary_compensation(_SCT.format(rows=rows))}
    assert got[("Dawn Phillipson", 2024)] == ("Former Chief Financial Officer", 895_955)
    assert got[("Kathy Veltri", 2025)] == ("Chief Retail Officer", 3_376_058)


# --- Management Changes -----------------------------------------------------

def events(text):
    return {(e["person"], e["change"]) for e in extract_events(HEAD + text)}


def test_biography_retirement_is_not_a_departure():
    # NIKE 2026-11-09: McKenzie rehired as CAO.
    got = events("On November 9, 2026, NIKE, Inc. announced the appointment of Dan McKenzie as Vice President, "
                 "Chief Accounting Officer, effective as of November 9, 2026. Mr. McKenzie, 55, retired from the "
                 "Company in July 2026 and has not held another position since his retirement.")
    assert ("Dan McKenzie", "Appointment") in got and ("Dan McKenzie", "Departure") not in got


def test_possessive_resignation_and_hypothetical_are_not_departures():
    got = events("On April 16, 2024, the Board of Directors of RumbleOn, Inc. appointed Michael Quartieri as a "
                 "director of the Company to fill the vacant seat created by Melvin Flanigan's resignation on "
                 "April 8, 2024.")
    assert ("Michael Quartieri", "Departure") not in got
    got = events("Ralph Lauren will continue as Executive Chairman. Upon at least 30 days written notice to the "
                 "Company, Mr. Lauren may step down as Chief Creative Officer and continue as Executive Chairman.")
    assert ("Ralph Lauren", "Departure") not in got


def test_certain_officers_is_not_a_person():
    got = events("The Compensation Committee approved retention awards for Certain Officers of the Company. "
                 "Certain Officers will resign if the merger closes.")
    assert not any(p.startswith("Certain") for p, _ in got)


def test_titles_split_whole():
    assert split_titles("President and Chief Operating and Financial Officer") == \
        ["President", "Chief Operating and Financial Officer"]
    assert split_titles("Chief Accounting Officer and principal accounting officer of Peloton Interactive") == \
        ["Chief Accounting Officer"]
    assert split_titles("principal accounting officer") == ["Principal Accounting Officer"]


def test_news_names_and_campaigns():
    got = parse_news("Lululemon Names Four Seasons' Ben Hallam Chief Brand Officer, Adds Chief Strategy Officer",
                     "", "Lululemon Athletica Inc.")
    assert got and got[0]["person"] == "Ben Hallam"
    assert parse_news("Elliott Investment takes $1-billion stake in Lululemon, pushes for new CEO",
                      "advocating for Jane Nielsen, former COO and CFO of Ralph Lauren, to become the new CEO.",
                      "Lululemon Athletica Inc.") == []


# --- Executive Moves --------------------------------------------------------

def _moves_frame(rows):
    df = pd.DataFrame(rows)
    for col in ("notice_date", "effective_date"):
        df[col] = pd.to_datetime(df[col])
    return df


def test_moves_ignore_interim_endings_and_board_seats_held_while_employed():
    names = {"INTC": "Intel Corporation", "ACI": "Albertsons Companies, Inc.", "HSY": "The Hershey Company",
             "JPM": "JPMorgan Chase & Co."}
    df = _moves_frame([
        dict(ticker="INTC", person="David Zinsner", change="Departure", title="Interim Co-Chief Executive Officer",
             notice_date="2025-03-10", effective_date="2025-03-17", is_board=False, event_id=1),
        dict(ticker="ACI", person="David Zinsner", change="Appointment", title="Director",
             notice_date="2025-09-15", effective_date=None, is_board=True, event_id=2),
        dict(ticker="HSY", person="Michele G. Buck", change="Departure", title="Chairman, President and CEO",
             notice_date="2025-12-01", effective_date="2026-06-30", is_board=False, event_id=3),
        dict(ticker="JPM", person="Michele G. Buck", change="Appointment", title="Director",
             notice_date="2024-12-12", effective_date="2025-03-17", is_board=True, event_id=4),
    ])
    bodies = {2: "Mr. Zinsner is CFO of Intel.", 4: "Ms. Buck is CEO of Hershey."}
    out = mark_moves(df, bodies, names)
    assert (out["moved_from"] == "").all(), out[["person", "moved_from"]]


def test_moves_take_the_nearest_departure():
    # Tanner: PepsiCo (2024) -> Wendy's -> Hershey (2025). Wendy's departure is
    # reported two days AFTER Hershey's announcement.
    names = {"PEP": "PepsiCo, Inc.", "WEN": "The Wendy's Company", "HSY": "The Hershey Company"}
    df = _moves_frame([
        dict(ticker="PEP", person="Kirk Tanner", change="Departure", title="CEO, PepsiCo Beverages North America",
             notice_date="2024-01-16", effective_date="2024-02-02", is_board=False, event_id=1),
        dict(ticker="WEN", person="Kirk Tanner", change="Departure", title="President and CEO",
             notice_date="2025-07-08", effective_date=None, is_board=False, event_id=2),
        dict(ticker="HSY", person="Kirk Tanner", change="Appointment", title="President and Chief Executive Officer",
             notice_date="2025-07-06", effective_date="2025-08-18", is_board=False, event_id=3),
    ])
    body = {3: "Mr. Tanner has served as President and CEO of Wendy's since 2024 and before that led PepsiCo Beverages."}
    out = mark_moves(df, body, names)
    assert out.loc[out["change"] == "Appointment", "moved_from"].iloc[0] == "WEN"


# --- Layoffs ----------------------------------------------------------------

def test_layoff_headlines_about_another_company_or_not_a_cut():
    names = {"ADBE": "Adobe Inc.", "DIS": "The Walt Disney Company", "DAL": "Delta Air Lines, Inc.",
             "ALK": "Alaska Air Group, Inc."}
    index = lead_owners(names)
    assert led_by_other_company("Disney Signs Adobe AI Deal Amid Reported Job Cut Plans", "ADBE", names["ADBE"], index)
    assert led_by_other_company("Alaska Air to cut 200 jobs as fuel costs threaten $700 million annual loss",
                                "DAL", names["DAL"], index)
    assert not led_by_other_company("Adobe Eliminates 100 Jobs in Sales", "ADBE", names["ADBE"], index)
    for noise in ("Law firm investigating Home Depot after 800 Georgia layoffs announced",
                  "Weekly news roundup: Anthropic hacking and AI safety concerns, plus ServiceNow layoffs",
                  "Daily Digest: Oracle's new layoff plan, Uber and Lyft drivers' union",
                  "Amazon.com, Inc. (AMZN) Stock: Layoffs and Lawsuit Put Spotlight on Company Culture",
                  "Why Is Comcast (NASDAQ:CMCSA) Trimming Streaming Staff Before Its Earnings?",
                  "Adobe Opens New Office Tower and Pledges No Companywide Layoffs in 2023"):
        assert not is_layoff_news(noise) or _NOT_A_CUT.search(noise), noise
    assert is_layoff_news("Comcast to cut 634 jobs at Philadelphia headquarters amid restructuring")


def test_copy_of_a_colleagues_row_is_dropped():
    # Target 2018: a restated row under Mulligan whose position names McNamara
    # and whose total is McNamara's. Mulligan's own 2018 row stays.
    df = pd.DataFrame({
        "ticker": ["TGT"] * 3,
        "executive_name": ["John J. Mulligan", "Michael E. McNamara", "John J. Mulligan"],
        "year": [2018] * 3, "total_compensation": [5_000_000.0, 5_000_000.0, 9_000_000.0],
        "raw_position": ["EVP & COO Michael E. McNamara EVP & CIO", "EVP & CIO", "EVP & COO"]})
    out = _drop_copied_rows(df)
    assert out.index.tolist() == [1, 2]


def test_copied_pay_checked_against_the_filing():
    universe = pd.DataFrame({
        "ticker": ["TGT", "TGT", "VSXY", "VSXY", "ELF", "ELF"],
        "executive_name": ["John J. Mulligan", "Michael E. McNamara", "Scott Sekella", "Melinda McAfee",
                           "Kory Marchisotto", "Scott Milsten"],
        "year": [2018, 2018, 2023, 2023, 2024, 2024],
        "total_compensation": [5_356_168.0, 5_356_168.0, 2_564_782.0, 2_564_782.0, 4_656_438.0, 4_656_438.0],
        "source": ["SEC"] * 6, "salary": [1.0] * 6, "filing_blob": [""] * 6})
    for col in ("bonus", "stock_awards", "option_awards", "non_equity_incentive", "all_other_compensation"):
        universe[col] = None
    # Victoria's Secret's 2023 is covered by two proxies; neither lists Sekella.
    filings = pd.DataFrame({
        "ticker": ["TGT", "TGT", "VSXY", "VSXY", "ELF", "ELF"],
        "executive_name": ["John J. Mulligan", "Michael E. McNamara", "Melinda McAfee", "Melinda McAfee",
                           "Kory Marchisotto", "Scott Milsten"],
        "year": [2018, 2018, 2023, 2023, 2024, 2024],
        "total_compensation": [8_575_733.0, 5_356_168.0, 2_564_782.0, 2_564_782.0, 4_656_438.0, 4_656_438.0],
        "salary": [2.0] * 6, "filing_blob": ["tgt-2019", "tgt-2019", "vs-2024", "vs-2025", "elf-2025", "elf-2025"]})
    for col in ("bonus", "stock_awards", "option_awards", "non_equity_incentive", "all_other_compensation"):
        filings[col] = None
    out = _fix_copied_pay(universe, filings).set_index("executive_name")["total_compensation"]
    assert out["John J. Mulligan"] == 8_575_733.0          # the filing's figure, not McNamara's
    assert "Scott Sekella" not in out.index                # not in the 2023 filing at all: a copy
    assert out["Kory Marchisotto"] == out["Scott Milsten"] == 4_656_438.0   # equal in the filing: genuine


# ---- Leftovers of the same kinds, found by re-checking the exports (2026-11-13) ----

def test_name_marks_and_spacing_are_cleaned():
    from data.people_service import clean_person_name
    cases = {"Chad D. Marquardt ††": "Chad D. Marquardt", "Manon Brouillette^^^": "Manon Brouillette",
             "Steven B. Shiffman, age": "Steven B. Shiffman", "John “ Hil” Davis": "John “Hil” Davis",
             'Amy " Levy" Trooskin': 'Amy "Levy" Trooskin', "John F. O’ Brien": "John F. O’Brien",
             "Tak Chung Mr Leung": "Tak Chung Leung", "Hong (Nmn) Chen": "Hong Chen",
             "Chirantan “CJ” Desai": "Chirantan “CJ” Desai"}
    assert {k: clean_person_name(k) for k in cases} == cases


def test_titles_holding_other_people_or_two_wordings():
    from data.people_service import clean_title
    assert clean_title("Senior VP, Software & Chief Commercial Officer G. Cohn , Vice Chairman A. Robinson , "
                       "Senior VP & Chief Legal Officer") == "Senior VP, Software & Chief Commercial Officer"
    assert clean_title("Executive Vice President, AI Acceleration, Product and Design EVP, AI Acceleration, "
                       "Product and Design") == "Executive Vice President, AI Acceleration, Product and Design"
    assert clean_title("EVP and Chief Operating Officer") == "EVP and Chief Operating Officer"
    assert clean_title("Senior Vice President, Stores Executive Vice President, Merchandising") == \
        "Senior Vice President, Stores Executive Vice President, Merchandising"


def test_a_word_split_by_the_parser_is_one_spelling():
    from data.people_service import _proxy_records
    proxy = pd.DataFrame({"ticker": ["CLX"] * 3, "executive_name": ["Benno Dor er", "Benno Dorer", "Benno Dor er"],
                          "title": ["CEO"] * 3, "year": [2016, 2017, 2018], "total_compensation": [1.0, 2.0, 3.0],
                          "filing_date": ["2018-10-01"] * 3, "url": [""] * 3})
    assert {r["executive_name"] for r in _proxy_records(proxy)} == {"Benno Dorer"}


def test_form3_funds_are_not_people():
    from data.management_changes import form3_changes
    rows = pd.DataFrame({
        "ticker": ["HHH", "HHH", "MAT"], "title": ["", "", "EVP"],
        "person": ["Square Capital Management, L.P. Pershing", "Investment Group Inc. Edgepoint", "Tiffany To"],
        "is_officer": [False, False, True], "is_director": [True, True, False],
        "became_insider": pd.to_datetime(["2024-01-02", "2024-02-02", "2024-03-02"]),
        "filing_date": pd.to_datetime(["2024-01-05", "2024-02-05", "2024-03-05"]), "url": [""] * 3})
    assert form3_changes(rows)["person"].tolist() == ["Tiffany To"]


def test_first_full_year_pays_a_full_salary():
    # Genesco's fiscal 2021 ended 30 Jan 2021, six weeks after Thomas George joined.
    from data.management_changes import compare_move_pay
    moves = pd.DataFrame({"person": ["Thomas A. George"], "moved_from": ["DECK"], "ticker": ["GCO"],
                          "moved_from_date": pd.to_datetime(["2018-03-02"]), "notice_date": pd.to_datetime(["2020-11-01"]),
                          "effective_date": pd.to_datetime(["2020-12-14"])})
    people = pd.DataFrame({"ticker": ["DECK", "GCO", "GCO"], "executive_name": ["Thomas A. George"] * 3,
                           "year": [2017, 2021, 2022], "total_compensation": [2_000_000.0, 86_985.0, 2_615_807.0],
                           "total_pay": [None] * 3, "salary": [500_000.0, 60_000.0, 600_000.0],
                           "title": ["CFO", "CFO", "CFO"]})
    out = compare_move_pay(moves, people).iloc[0]
    assert (out["new_pay_year"], out["new_pay"]) == (2022, 2_615_807.0)
    assert out["pay_comparable"] == "Not comparable: 5 years apart"


def test_a_name_wrapped_onto_the_title_line_is_rejoined():
    from data.people_service import rejoin_cut_name
    assert rejoin_cut_name("Lucas de", "Carvalho Rocha") == ("Lucas de Carvalho Rocha", "")
    assert rejoin_cut_name("Lucas de Carvalho Rocha", "Chief Executive Officer") == \
        ("Lucas de Carvalho Rocha", "Chief Executive Officer")
    assert rejoin_cut_name("Jan van", "Senior Vice President") == ("Jan van", "Senior Vice President")
