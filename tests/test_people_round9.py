#!/usr/bin/env python3
"""Round 9 People checks: layoff cleaning, offer salary, title fill, junk names,
Form 3 page link, news feed label. Real headlines / filing text from STG (2026-10-16).

Run:  .venv/bin/python -m pytest -q tests/test_people_round9.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import pandas as pd

from data.form3_appointments import form_url
from data.layoff_events import (charges_of, clean_layoff_events, is_layoff_news, item_205_summary,
                                jobs_of, names_company, share_of)
from data.management_changes import offer_salary, source_label
from data.people_service import NAMED_EXECUTIVE, fill_missing_titles, is_person_name

NAMES = {"NFLX": "Netflix Inc", "WBD": "Warner Bros. Discovery Inc", "TFC": "Truist Financial Corporation",
         "TFC-P-O": "Truist Financial Corporation", "BMY": "Bristol-Myers Squibb Company",
         "PG": "The Procter & Gamble Company", "BMWKY": "Bayerische Motoren Werke AG",
         "WDAY": "Workday Inc", "DIS": "Walt Disney Co (The)"}


def test_layoff_relevance():
    assert is_layoff_news("Netflix to cut 1,000 jobs as growth concerns mount")
    assert is_layoff_news("Starbucks to close 250 stores in North America this week")
    assert is_layoff_news("Needham reiterates Buy on HubSpot stock after workforce reduction")
    assert not is_layoff_news("AI helps Bank of America avoid layoffs, CEO Brian Moynihan tells investors")
    assert not is_layoff_news("Gauzy receives court approval for debt restructuring plan By Investing.com")
    assert not is_layoff_news("Prudential Financial Inc preferred stock hits 52-week low at 14.73 USD")
    assert not is_layoff_news("Jones Lang LaSalle stock dips after leadership restructuring")


def test_layoff_names_the_company():
    assert names_company("Netflix to cut 1,000 jobs", "NFLX", NAMES["NFLX"])
    assert not names_company("Netflix to cut 1,000 jobs", "WBD", NAMES["WBD"])
    assert names_company("Bristol Myers cuts another 265 jobs", "BMY", NAMES["BMY"])
    assert names_company("P&G Has Cut 5,000 Jobs So Far", "PG", NAMES["PG"])
    assert names_company("BMW's strategy: Job cuts, AI", "BMWKY", NAMES["BMWKY"])
    assert names_company("Disney lays off a few hundred more employees", "DIS", NAMES["DIS"])


def test_numbers():
    assert jobs_of("Kroger to lay off 3,000 corporate employees over 3 years") == "3,000"
    assert jobs_of("HubSpot cuts 660 roles, or 7% of its team") == "660"
    assert share_of("HubSpot cuts 660 roles, or 7% of its team") == "7%"
    assert share_of("a reduction of approximately 2.5% of Workday’s current workforce") == "2.5%"
    assert charges_of("Workday estimates that it will incur approximately $65 million to "
                      "$80 million in charges, of which") == "$65M–$80M"
    assert charges_of("expected to result in pre-tax charges of approximately $1.0 billion") == "$1.0B"


def test_item_205_summary_skips_cross_reference():
    text = ("Item 2.05 below, Workday now expects its margin to be lower. Item 2.05 - Costs Associated "
            "with Exit or Disposal Activities On September 29, 2026, certain functions within Workday "
            "announced reorganizations. These actions include a reduction of approximately 2.5% of "
            "Workday’s current workforce. Item 7.01 Regulation FD.")
    assert item_205_summary(text).startswith("These actions include a reduction")


def test_clean_folds_outlets_and_listed_lines():
    day = pd.Timestamp("2026-10-14")
    rows = [
        ("NFLX", day, "Netflix to cut 1,000 jobs as growth concerns mount", "AV News — Reuters"),
        ("NFLX", day + pd.Timedelta(days=1), "Netflix Lays Off Around 1,000 Employees", "AV News — X"),
        ("WBD", day, "Netflix to cut 1,000 jobs as growth concerns mount", "AV News — Reuters"),
        ("TFC", day, "Truist to cut more than 200 jobs in Arlington", "AV News — X"),
        ("TFC-P-O", day, "Truist to cut more than 200 jobs in Arlington", "AV News — X"),
    ]
    df = pd.DataFrame([{"ticker": t, "event_date": d, "event_subtype": "Headcount Reduction", "headline": h,
                        "source": s, "source_ref": "", "situation": None} for t, d, h, s in rows])
    out = clean_layoff_events(df, NAMES)
    assert sorted(out["ticker"]) == ["NFLX", "TFC"], out
    assert out.set_index("ticker").at["NFLX", "jobs"] == "1,000"


def test_offer_salary_is_the_persons():
    text = ("Ms. Jane Roe will serve as Chief Financial Officer. Ms. Roe will receive an annual base "
            "salary of $850,000 and a target bonus of 100%. Separately, Mr. Smith's base salary of "
            "$1,200,000 is unchanged.")
    assert offer_salary(text, "Jane Roe") == 850_000
    assert pd.isna(offer_salary("The base salary of $900,000 applies to the CEO.", "Jane Roe"))


def test_titles_and_names():
    assert not is_person_name("Famous Footwear")
    assert not is_person_name("and Accounting")
    assert not is_person_name("Jr.** Douglas")
    assert not is_person_name("Wholesale and Dist. Ops")
    assert is_person_name("Anthony A. Iskander")
    df = pd.DataFrame({"ticker": ["ADBE", "ADBE", "TXN"], "executive_name": ["Daniel Durn", "Daniel Durn", "Haviv Ilan"],
                       "title": ["", "EVP and CFO", ""], "role": pd.Categorical(["Other", "CFO", "Other"])})
    form3 = pd.DataFrame({"ticker": [], "person": [], "title": []})
    out = fill_missing_titles(df, form3)
    assert list(out["title"]) == ["EVP and CFO", "EVP and CFO", NAMED_EXECUTIVE]


def test_form3_link_and_feed_label():
    head = "<FILENAME>wk-form3_1792013423.xml\n<ownershipDocument><schemaVersion>X0607</schemaVersion>"
    url = "https://www.sec.gov/Archives/edgar/data/883569/000215841126000003/0002158411-26-000003.txt"
    assert form_url(url, head) == ("https://www.sec.gov/Archives/edgar/data/883569/000215841126000003/"
                                   "xslF345X06/wk-form3_1792013423.xml")
    assert form_url(url, "") == url.replace(".txt", "-index.htm")
    assert source_label("AV News — Business Wire") == "AV News"
    assert source_label("YF News — Reuters") == "YF News"
    assert source_label("SEC EDGAR 8-K") == "SEC 8-K"
