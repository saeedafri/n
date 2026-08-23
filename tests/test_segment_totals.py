"""Segment Total rows must come from the members' own XBRL concept.

The Segments tab shows a filed "Total" under each metric instead of summing the
member rows. That total used to be chosen by substring-matching the metric name
against every non-dimensioned label, first alphabetical match wins — so TJX's
geographic assets ($7,346m of us-gaap:NoncurrentAssets) were totalled with
"(Increase) in prepaid expenses and other current assets" ($31m), because both
labels contain "assets" and "(" sorts first.

These tests pin the concept-anchored rule with hand-built rows — no database.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

from data.repository import SegmentDataRepository  # noqa: E402

YEARS = [2024, 2025]


def geo_member(label, concept, member, year, value):
    return {
        "original_label": label,
        "concept": concept,
        "numeric_value": value,
        "unit_ref": "usd",
        "report_fiscal_year": year,
        "period_type": "instant",
        "period_instant": date(year, 2, 1),
        "period_end": None,
        "period_start": None,
        "dimension": "srt:StatementGeographicalAxis",
        "dimension_label": member,
        "dimension_member_label": member,
        "full_dimension_label": f"Geographical: {member}",
        "filing_date": date(year, 4, 1),
    }


def consolidated(label, concept, year, value):
    return {
        "original_label": label,
        "concept": concept,
        "numeric_value": value,
        "unit_ref": "usd",
        "report_fiscal_year": year,
        "period_type": "instant",
        "period_instant": date(year, 2, 1),
        "period_end": None,
        "period_start": None,
        "dimension": None,
        "dimension_label": None,
        "dimension_member_label": None,
        "full_dimension_label": None,
        "filing_date": date(year, 4, 1),
        "_is_ndim": True,
    }


def tjx_rows():
    """TJX FY2025 shape: long-lived assets by country + two decoy "assets" facts."""
    rows = []
    for member, value in (("United States", 5_869e6), ("Europe", 1_041e6),
                          ("Canada", 364e6), ("Australia", 72e6)):
        rows.append(geo_member("Carrying values of long-lived assets",
                               "us-gaap:NoncurrentAssets", member, 2025, value))
    rows.append(consolidated("Carrying values of long-lived assets",
                             "us-gaap:NoncurrentAssets", 2025, 7_346e6))
    # Decoys: both match the "Assets" keyword list, both sort before "Carrying".
    rows.append(consolidated("(Increase) in prepaid expenses and other current assets",
                             "us-gaap:IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
                             2025, 31e6))
    rows.append(consolidated("Assets", "us-gaap:Assets", 2025, 30_814e6))
    return rows


def duration_member(label, concept, member, year, value):
    row = geo_member(label, concept, member, year, value)
    row.update(period_type="duration", period_instant=None,
               period_start=date(year - 1, 2, 2), period_end=date(year, 2, 1))
    return row


def consolidated_duration(label, concept, year, value):
    row = consolidated(label, concept, year, value)
    row.update(period_type="duration", period_instant=None,
               period_start=date(year - 1, 2, 2), period_end=date(year, 2, 1))
    return row


def classify(rows, years=YEARS):
    """Order rows the way `_fetch_all_db_rows` does — the classifier's first-wins
    merge relies on it, so a test that skipped it would not be testing production."""
    rows = sorted(rows, key=lambda r: (r.get("report_fiscal_year") or 0,
                                       r.get("full_dimension_label") or "",
                                       r.get("original_label") or "",
                                       -int(r["filing_date"].isoformat().replace("-", ""))))
    return SegmentDataRepository._classify_segment_rows(rows, years)


def test_total_matches_the_members_concept_not_a_lookalike_label():
    _, geo, totals = classify(tjx_rows())
    assert sorted(geo["Assets"]) == ["Australia", "Canada", "Europe", "United States"]
    assert totals["geo"]["Assets"][2025] == 7346.0
    assert sum(geo["Assets"][m][2025] for m in geo["Assets"]) == 7346.0


def test_business_and_geographic_totals_are_kept_apart():
    """Same metric name, different facts: geo splits long-lived assets while the
    business segments split balance-sheet assets. One shared total would be wrong
    for at least one of the two tables."""
    rows = tjx_rows()
    for member, value in (("Marmaxx", 18_000e6), ("HomeGoods", 12_814e6)):
        rows.append({
            **geo_member("Total assets", "us-gaap:Assets", member, 2025, value),
            "dimension": "us-gaap:StatementBusinessSegmentsAxis",
            "full_dimension_label": f"Segments: {member}",
        })
    _, _, totals = classify(rows)
    assert totals["geo"]["Assets"][2025] == 7346.0
    assert totals["business"]["Assets"][2025] == 30814.0


def test_no_total_when_the_company_never_filed_one():
    """Better a missing Total row than an unrelated fact dressed up as one."""
    rows = [r for r in tjx_rows()
            if not (r.get("_is_ndim") and r["concept"] == "us-gaap:NoncurrentAssets")]
    _, _, totals = classify(rows)
    assert totals["geo"].get("Assets", {}).get(2025) is None


def test_most_recent_filing_wins_for_a_restated_total():
    rows = tjx_rows()
    stale = consolidated("Carrying values of long-lived assets",
                         "us-gaap:NoncurrentAssets", 2025, 7_000e6)
    stale["filing_date"] = date(2024, 4, 1)
    _, _, totals = classify([stale] + rows)
    assert totals["geo"]["Assets"][2025] == 7346.0


def test_declared_synonym_concept_bridges_a_filers_element_drift():
    """T-Mobile shape: members tagged DepreciationAndAmortization, consolidated row
    tagged DepreciationDepletionAndAmortization. Both are named for the metric in
    SEGMENT_METRIC_GROUPS, so the Total is recoverable without label guessing."""
    rows = []
    for member, value in (("US", 20_000e6), ("International", 5_000e6)):
        rows.append(geo_member("Depreciation and amortization",
                               "us-gaap:DepreciationAndAmortization", member, 2025, value))
    rows.append(consolidated("Depreciation and amortization",
                             "us-gaap:DepreciationDepletionAndAmortization", 2025, 25_000e6))
    rows.append(consolidated("Depreciation expense", "us-gaap:Depreciation", 2025, 9e6))
    _, _, totals = classify(rows)
    assert totals["geo"]["Depreciation & Amortization"][2025] == 25000.0


def test_an_undeclared_concept_is_never_bridged():
    """Tesla shape: the only consolidated candidates are tsla:DepreciationAmortization
    AndImpairment and us-gaap:Depreciation — neither is a declared equivalent, so no
    Total row rather than a near-enough number."""
    rows = []
    for member, value in (("Automotive", 3_000e6), ("Energy", 500e6)):
        rows.append(geo_member("Depreciation and amortization",
                               "us-gaap:DepreciationDepletionAndAmortization", member, 2025, value))
    rows.append(consolidated("Depreciation, amortization and impairment",
                             "tsla:DepreciationAmortizationAndImpairment", 2025, 5_000e6))
    _, _, totals = classify(rows)
    assert totals["geo"].get("Depreciation & Amortization", {}).get(2025) is None

def test_declared_element_outranks_an_ad_hoc_member_concept():
    """AMD shape: a member row "Operating income related to licensed IP" is tagged
    amd:GainLossOnLicensingAgreement, so that element joins the eligible set. The
    $102m licensing gain sorts before "Operating income" by label — only ranking
    the declared us-gaap element first keeps the $1,264m total."""
    rows = []
    for member, value in (("Data Center", 6_043e6), ("Client", 1_190e6)):
        rows.append(duration_member("Operating income (loss)", "us-gaap:OperatingIncomeLoss",
                                    member, 2025, value))
    rows.append(duration_member("Operating income related to licensed IP",
                                "amd:GainLossOnLicensingAgreement", "Data Center", 2025, 102e6))
    rows.append(consolidated_duration("Licensing gain", "amd:GainLossOnLicensingAgreement",
                                      2025, 102e6))
    rows.append(consolidated_duration("Operating income", "us-gaap:OperatingIncomeLoss",
                                      2025, 1_264e6))
    _, geo, totals = classify(rows)
    assert totals["geo"]["Operating Profit Before Tax"][2025] == 1264.0


def test_a_fact_from_another_period_never_becomes_the_total():
    """A 10-K also carries quarterly and prior-year consolidated facts. Only one
    covering the members' own period may be the Total."""
    rows = [duration_member("Operating income", "us-gaap:OperatingIncomeLoss", m, 2025, v)
            for m, v in (("US", 800e6), ("International", 200e6))]
    stray = consolidated_duration("Operating income", "us-gaap:OperatingIncomeLoss", 2025, 250e6)
    stray["period_start"], stray["period_end"] = date(2025, 1, 1), date(2025, 3, 31)
    rows.append(stray)
    rows.append(consolidated_duration("Operating income", "us-gaap:OperatingIncomeLoss",
                                      2025, 1_000e6))
    _, _, totals = classify(rows)
    assert totals["geo"]["Operating Profit Before Tax"][2025] == 1000.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("segment total tests passed")
