#!/usr/bin/env python3
"""Form 3 appointments: title cleanup, XML parse, and how the rows join Management Changes.

Run:  .venv/bin/python tests/test_form3_appointments.py
Titles below are real officerTitle values from 2026 Form 3 filings.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import pandas as pd

from data.form3_appointments import ROW_COLUMNS, clean_officer_title, parse_form3
from data.management_changes import CHANGE_COLUMNS, _fold_repeats, form3_changes

XML = """<ownershipDocument>
<documentType>3</documentType><periodOfReport>2026-10-05</periodOfReport>
<issuer><issuerCik>0000883569</issuerCik><issuerName>FOSSIL GROUP, INC.</issuerName><issuerTradingSymbol>FOSL</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0002158411</rptOwnerCik><rptOwnerName>Freeze Shannon Rebecca</rptOwnerName></reportingOwnerId><reportingOwnerAddress><rptOwnerCity>Richardson</rptOwnerCity></reportingOwnerAddress>
<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>Chief People Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
<reportingOwner><reportingOwnerId><rptOwnerCik>0000000001</rptOwnerCik><rptOwnerName>Big Fund LP</rptOwnerName></reportingOwnerId><reportingOwnerAddress><rptOwnerCity>Richardson</rptOwnerCity></reportingOwnerAddress>
<reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship></reportingOwner>
<remarks>Exhibit 24 - Power of Attorney</remarks>
</ownershipDocument>"""


def test_titles():
    assert clean_officer_title("Vice President, Chief Financial Officer and Treasurer\nExhibit 24.1 - Power of Attorney") \
        == "Vice President, Chief Financial Officer and Treasurer"
    assert clean_officer_title("President - Aerospace Group of Eaton Corporation, a subsidiary of the Issuer. "
                               "Power of Attorney is attached hereto as Exhibit 24.") \
        == "President - Aerospace Group of Eaton Corporation, a subsidiary of the Issuer"
    assert clean_officer_title("CHIEF FINANCIAL OFFICER") == "Chief Financial Officer"
    assert clean_officer_title("See Remarks", "EVP, General Counsel") == "EVP, General Counsel"
    assert clean_officer_title("SVP & Chief Operations Officer") == "SVP & Chief Operations Officer"


def test_parse_keeps_officers_not_funds():
    rows = parse_form3(XML, {883569: {"FOSL"}}, "2026-10-14", "0002158411-26-000003", "u.txt")
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["ticker"] == "FOSL" and r["person"] == "Shannon Rebecca Freeze", r
    assert r["title"] == "Chief People Officer" and r["became_insider"] == pd.Timestamp("2026-10-05")
    assert parse_form3(XML, {1: {"X"}}, "2026-10-14", "a", "u") == []  # issuer not ours


def frame(rows):
    return pd.DataFrame([dict(zip(ROW_COLUMNS, r)) for r in rows], columns=ROW_COLUMNS)


def test_changes_skip_listing_batches_and_name_directors():
    day = pd.Timestamp("2026-10-01")
    listing = [("WKEY", f"Person {i}", "", False, True, day, day, f"a{i}", "u.txt") for i in range(8)]
    real = [("CBRL", "David J. Deno", "President and CEO", True, True, day, day, "b", "u.txt"),
            ("RPM", "Jeffrey D. Rowe", "", False, True, day, day, "c", "u.txt")]
    out = form3_changes(frame(listing + real))
    assert list(out["ticker"]) == ["CBRL", "RPM"], out
    assert list(out["title"]) == ["President and CEO", "Director"]
    assert list(out["is_board"]) == [False, True]
    assert (out["source"] == "SEC Form 3").all() and (out["change"] == "Appointment").all()


def test_form3_folds_into_the_8k_that_announced_it():
    eightk = pd.DataFrame([{"ticker": "RPM", "person": "Jeffrey Rowe", "change": "Appointment",
                            "notice_date": pd.Timestamp("2026-08-20"), "source": "SEC 8-K",
                            "also_reported": "", "replaces": ""}]).reindex(columns=CHANGE_COLUMNS)
    form3 = form3_changes(frame([("RPM", "Jeffrey D. Rowe", "", False, True, pd.Timestamp("2026-10-08"),
                                  pd.Timestamp("2026-10-13"), "c", "u.txt")]))
    kept, rest = _fold_repeats(form3, primary=eightk, days=183)
    assert rest.empty and kept.at[0, "also_reported"] == "SEC Form 3", kept


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL PASS")
