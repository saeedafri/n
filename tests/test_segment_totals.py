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

# ── Member rows: which facts are segments at all, and what they are called ──────

def wrapper_row(inner, concept, member_label, year, value, axis="us-gaap:StatementBusinessSegmentsAxis"):
    """A multi-dimensional operating-segment fact: the primary member is the generic
    wrapper, the real segment sits on a second axis in dimension_label."""
    row = duration_member("Operating income (loss)", concept, member_label, year, value)
    row.update(dimension="srt:ConsolidationItemsAxis", dimension_member_label=member_label,
               dimension_label=inner,
               full_dimension_label=f"Consolidation Items: {member_label}, Segments: {inner}")
    return row


def test_real_segment_recovered_from_the_operating_segments_wrapper():
    """AMD shape: every segment fact is tagged ConsolidationItems=Operating Segments
    plus BusinessSegments=<segment>. Recovering only geographies left the tab showing
    nothing but the reconciliation line."""
    rows = [wrapper_row(seg, "us-gaap:OperatingIncomeLoss", "Operating Segments", 2025, v)
            for seg, v in (("Datacenter", 3_482e6), ("Client", 897e6), ("Gaming", 290e6))]
    rows.append(consolidated_duration("Operating income", "us-gaap:OperatingIncomeLoss",
                                      2025, 1_900e6))
    biz, _, totals = classify(rows)
    assert sorted(biz["Operating Profit Before Tax"]) == ["Client", "Datacenter", "Gaming"]
    assert totals["business"]["Operating Profit Before Tax"][2025] == 1900.0


def test_wrapper_with_no_inner_member_is_the_rollup_and_is_dropped():
    rows = [wrapper_row("Datacenter", "us-gaap:OperatingIncomeLoss", "Operating Segments", 2025, 10e6),
            wrapper_row("Operating Segments", "us-gaap:OperatingIncomeLoss", "Operating Segments",
                        2025, -4_979e6)]
    biz, _, _ = classify(rows)
    assert sorted(biz["Operating Profit Before Tax"]) == ["Datacenter"]


def test_reconciliation_rows_are_not_segments():
    """"Segment Reconciling Items" and "…Reconciling Item, Excluding Corporate
    Nonsegment" are the bridge to the consolidated figure, not segments — the same
    policy SEGMENT_SKIP_MEMBERS already applies to eliminations and corporate."""
    rows = [duration_member("Operating income (loss)", "us-gaap:OperatingIncomeLoss", m, 2025, v)
            for m, v in (("Datacenter", 3_482e6), ("Client", 897e6),
                         ("Segment Reconciling Items", 4_190e6),
                         ("Segment Reporting, Reconciling Item, Excluding Corporate Nonsegment", 4_190e6),
                         ("Corporate, Non -Segment", -133e6),
                         ("Total Wholesale", 11e6))]
    for r in rows:
        r["dimension"] = "us-gaap:StatementBusinessSegmentsAxis"
        r["full_dimension_label"] = f"Segments: {r['dimension_member_label']}"
    biz, _, _ = classify(rows)
    assert sorted(biz["Operating Profit Before Tax"]) == ["Client", "Datacenter"]


def test_label_drift_collapses_to_one_member():
    """Ingredion files one segment three ways across filings."""
    keys = {SegmentDataRepository._member_key(m) for m in
            ("Asia Pacific Segment", "Asia- Pacific", "Asia-Pacific")}
    assert len(keys) == 1
    assert SegmentDataRepository._member_key("F&II - LATAM") == SegmentDataRepository._member_key("F&II\u2013LATAM")
    assert SegmentDataRepository._member_key("North America Segment") == SegmentDataRepository._member_key("North America")


def test_member_key_never_merges_genuinely_different_segments():
    distinct = ["Client", "Client and Gaming", "Wholesale Footwear", "Wholesale Accessories",
                "North America", "South America", "China", "China (including Hong Kong)"]
    assert len({SegmentDataRepository._member_key(m) for m in distinct}) == len(distinct)


def test_newest_filings_label_is_the_one_displayed():
    old = duration_member("Net sales", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                          "F&II - LATAM", 2024, 2_450e6)
    old["filing_date"] = date(2024, 2, 20)
    new = duration_member("Net sales", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                          "F&II\u2013LATAM", 2025, 2_341e6)
    new["filing_date"] = date(2025, 2, 20)
    other = duration_member("Net sales", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                            "T&HS", 2025, 2_397e6)
    _, geo, _ = classify([old, new, other])
    assert "F&II\u2013LATAM" in geo["Revenues"]
    assert "F&II - LATAM" not in geo["Revenues"]


def test_acronyms_survive_title_casing():
    assert SegmentDataRepository._title_case_member("F&II-LATAM") == "F&II-LATAM"
    assert SegmentDataRepository._title_case_member("T&HS") == "T&HS"
    assert SegmentDataRepository._title_case_member("EMEA") == "EMEA"
    # …while ordinary all-caps labels still read as words
    assert SegmentDataRepository._title_case_member("UNITED STATES") == "United States"
    assert SegmentDataRepository._title_case_member("NORTH AMERICA") == "North America"
    assert SegmentDataRepository._title_case_member("JAPAN") == "Japan"

def test_invisible_characters_do_not_split_a_member():
    """PriceSmart files bidi marks inside its member names — invisible on screen,
    but they made one segment render as two."""
    assert (SegmentDataRepository._member_key("United \u200eStates \u200eOperations")
            == SegmentDataRepository._member_key("United States Operations"))


def test_ampersand_and_the_word_and_are_the_same_member():
    assert (SegmentDataRepository._member_key("Apparel, Gear & Other")
            == SegmentDataRepository._member_key("Apparel, Gear and Other"))


def test_cost_lines_that_merely_mention_depreciation_are_not_da():
    """EPAM and Steve Madden file "Cost of revenues (exclusive of depreciation and
    amortization)" — a cost line whose value dwarfs real D&A."""
    from utils.constants import SEGMENT_METRIC_GROUPS
    cfg = SEGMENT_METRIC_GROUPS["Depreciation & Amortization"]
    assert not SegmentDataRepository._matches_metric(
        "Cost of revenues (exclusive of depreciation and amortization)", cfg)
    assert not SegmentDataRepository._matches_metric(
        "Cost of goods sold, excluding depreciation and amortization", cfg)
    # …while the real lines still match
    assert SegmentDataRepository._matches_metric("Depreciation and amortization", cfg)
    assert SegmentDataRepository._matches_metric("Depreciation and amortization expense", cfg)


def test_total_prefers_the_concept_most_members_use():
    """Under Armour shape: the members are long-lived assets (NoncurrentAssets), and
    a minor fact sharing the metric must not win just by being filed later."""
    rows = []
    for m, v in (("United States", 801e6), ("Canada", 21e6), ("EMEA", 100e6)):
        rows.append(geo_member("Long-lived assets", "us-gaap:NoncurrentAssets", m, 2025, v))
    stray = geo_member("Assets held for sale", "us-gaap:AssetsHeldForSaleNotPartOfDisposalGroup",
                       "United States", 2025, 2e6)
    rows.append(stray)
    rows.append(consolidated("Long-lived assets", "us-gaap:NoncurrentAssets", 2025, 1_055e6))
    late = consolidated("Assets held for sale",
                        "us-gaap:AssetsHeldForSaleNotPartOfDisposalGroup", 2025, 2e6)
    late["filing_date"] = date(2026, 5, 1)
    rows.append(late)
    _, _, totals = classify(rows)
    assert totals["geo"]["Assets"][2025] == 1055.0

def test_assets_metric_ignores_held_for_sale_and_amortization_lines():
    """Jack in the Box's segment "assets" were "Assets held for sale" and
    "Amortization of favorable and unfavorable lease assets"."""
    from utils.constants import SEGMENT_METRIC_GROUPS
    cfg = SEGMENT_METRIC_GROUPS["Assets"]
    assert not SegmentDataRepository._matches_metric("Assets held for sale", cfg)
    assert not SegmentDataRepository._matches_metric(
        "Amortization of favorable and unfavorable lease assets", cfg)
    assert not SegmentDataRepository._matches_metric("Reclassified to assets held for sale", cfg)
    assert SegmentDataRepository._matches_metric("Total assets", cfg)
    assert SegmentDataRepository._matches_metric("Carrying values of long-lived assets", cfg)

def test_revenue_total_is_never_smaller_than_one_segment():
    """The Andersons shape: 150 product-detail rows are tagged
    RevenueFromContractWithCustomerExcludingAssessedTax and only the segment rows
    us-gaap:Revenues, so the most-used concept is the wrong one. A $2,211m
    candidate cannot be the top line when one segment alone booked $9,304m."""
    rows = [duration_member("Sales and merchandising revenues", "us-gaap:Revenues", m, 2025, v)
            for m, v in (("Trade", 9_304e6), ("Renewables", 2_440e6), ("Nutrient", 866e6))]
    for i in range(6):   # the product-detail rows that dominate by count
        rows.append(duration_member("Revenue from contract with customers",
                                    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                                    f"Product {i}", 2025, 300e6))
    rows.append(consolidated_duration("Revenue from contract with customers",
                                      "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                                      2025, 2_211e6))
    rows.append(consolidated_duration("Sales and merchandising revenues", "us-gaap:Revenues",
                                      2025, 12_612e6))
    _, geo, totals = classify(rows)
    assert totals["geo"]["Revenues"][2025] == 12612.0


def test_the_covering_revenue_nearest_the_segments_wins():
    """Goldman files both net revenues — exactly its segments — and a smaller
    subtotal; both clear the largest-segment rule, so nearness decides."""
    rows = [duration_member("Revenues", "us-gaap:Revenues", m, 2025, v)
            for m, v in (("Markets", 30_000e6), ("Banking", 20_000e6))]
    rows.append(consolidated_duration("Total net revenues", "us-gaap:Revenues", 2025, 50_000e6))
    subtotal = consolidated_duration("Total non-interest revenues", "us-gaap:Revenues",
                                     2025, 38_000e6)
    subtotal["filing_date"] = date(2026, 3, 1)   # newer, so only rules 2-3 can stop it
    rows.append(subtotal)
    _, geo, totals = classify(rows)
    assert totals["geo"]["Revenues"][2025] == 50000.0


def test_a_segment_may_out_earn_the_company_on_profit():
    """The revenue invariant must not leak into Operating Profit: unallocated
    corporate costs legitimately put the total below a segment (AMD FY2025)."""
    rows = [duration_member("Operating income (loss)", "us-gaap:OperatingIncomeLoss", m, 2025, v)
            for m, v in (("Datacenter", 3_482e6), ("Client", 897e6))]
    rows.append(consolidated_duration("Operating income", "us-gaap:OperatingIncomeLoss",
                                      2025, 1_900e6))
    _, geo, totals = classify(rows)
    assert totals["geo"]["Operating Profit Before Tax"][2025] == 1900.0

def test_the_year_beats_the_quarters_that_share_its_label():
    """Kohl's files nine facts labelled "Total revenue" in one 10-K — the year and
    each quarter, all the same concept. Only the one covering the members' period
    is the Total, whatever the quarterly values happen to be worth."""
    rows = [duration_member("Other revenue",
                            "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                            m, 2025, v) for m, v in (("Gift Card", 149e6), ("Other Revenue", 924e6))]
    rows.append(consolidated_duration("Total revenue",
                                      "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                                      2025, 15_955e6))
    for i, qv in enumerate((2_428e6, 3_979e6, 4_087e6)):
        q = consolidated_duration("Total revenue",
                                  "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                                  2025, qv)
        q["period_start"], q["period_end"] = date(2025, 1 + i * 3, 1), date(2025, 3 + i * 3, 28)
        rows.append(q)
    _, geo, totals = classify(rows)
    assert totals["geo"]["Revenues"][2025] == 15955.0

def test_members_measuring_something_else_are_dropped():
    """Roper files segment assets three ways: us-gaap:NoncurrentAssets plus two
    Roper-defined elements. The tab listed $577.6m of "operating assets" under a
    $187.1m long-lived-assets Total."""
    rows = [geo_member("Long-lived assets", "us-gaap:NoncurrentAssets", m, 2025, v)
            for m, v in (("Network Software", 120e6), ("Process Technologies", 60e6))]
    rows.append(geo_member("Operating assets", "rop:SegmentReportingOperatingAssets",
                           "Application Software", 2025, 577e6))
    rows.append(consolidated("Long-lived assets", "us-gaap:NoncurrentAssets", 2025, 187e6))
    _, geo, totals = classify(rows)
    assert sorted(geo["Assets"]) == ["Network Software", "Process Technologies"]
    assert totals["geo"]["Assets"][2025] == 187.0


def test_a_filer_using_only_its_own_elements_keeps_every_member():
    """The guard: when the Total itself is filer-defined there is no standard
    measure to hold members to, so nothing is dropped."""
    rows = [geo_member("Operating assets", "rop:SegmentReportingOperatingAssets", m, 2025, v)
            for m, v in (("Application Software", 577e6), ("Network Software", 215e6))]
    rows.append(consolidated("Operating assets", "rop:SegmentReportingOperatingAssets",
                             2025, 1_356e6))
    _, geo, totals = classify(rows)
    assert sorted(geo["Assets"]) == ["Application Software", "Network Software"]
    assert totals["geo"]["Assets"][2025] == 1356.0

def test_a_total_its_own_members_disprove_is_withheld():
    """Corning's consolidated long-lived assets are stored as $68m against $44.7bn
    of its own geographic members — a scale error no selection rule can see, since
    the fact has the right concept and period. Show nothing, not $68m."""
    rows = [geo_member("Long-lived assets", "us-gaap:NoncurrentAssets", m, 2025, v)
            for m, v in (("Asia Pacific", 10_948e6), ("North America", 9_003e6))]
    rows.append(consolidated("Long-lived assets", "us-gaap:NoncurrentAssets", 2025, 68e6))
    _, geo, totals = classify(rows)
    assert sorted(geo["Assets"]) == ["Asia Pacific", "North America"]   # members still shown
    assert totals["geo"]["Assets"][2025] is None                        # the impossible Total is not


def test_the_guard_stays_off_operating_profit():
    rows = [duration_member("Operating income (loss)", "us-gaap:OperatingIncomeLoss", m, 2025, v)
            for m, v in (("Datacenter", 3_482e6), ("Client", 897e6))]
    rows.append(consolidated_duration("Operating income", "us-gaap:OperatingIncomeLoss",
                                      2025, 1_900e6))
    _, _, totals = classify(rows)
    assert totals["geo"]["Operating Profit Before Tax"][2025] == 1900.0


def test_the_total_is_only_compared_with_members_of_its_own_measure():
    """Asbury shape: a correct $69m D&A Total must not be hidden because a member
    carries a different measure worth $165m — that member is not part of it."""
    rows = [geo_member("Depreciation and amortization",
                       "us-gaap:DepreciationDepletionAndAmortization", "Dealerships", 2025, 68.2e6)]
    odd = geo_member("Amortization of deferred acquisition costs",
                     "abg:BusinessAcquisitionDeferredAcquisitionCostsAmortization",
                     "TCA", 2025, 165.7e6)
    rows.append(odd)
    rows.append(consolidated("Depreciation and amortization",
                             "us-gaap:DepreciationDepletionAndAmortization", 2025, 69e6))
    _, _, totals = classify(rows)
    assert totals["geo"]["Depreciation & Amortization"][2025] == 69.0


def test_the_guard_stays_off_when_a_member_is_negative():
    """Eliminations legitimately pull a consolidated figure below a segment."""
    rows = [geo_member("Total assets", "us-gaap:Assets", m, 2025, v)
            for m, v in (("Retail", 900e6), ("Eliminations, net", -400e6))]
    rows.append(consolidated("Total assets", "us-gaap:Assets", 2025, 500e6))
    _, _, totals = classify(rows)
    assert totals["geo"]["Assets"][2025] == 500.0

def test_an_impossible_total_is_withheld_even_when_members_are_mixed():
    """Constellation shape: the members carry two measures, and the Total is below
    the one it shares a concept with, so it is still impossible."""
    rows = [geo_member("Total assets", "us-gaap:Assets", "Wine and Spirits", 2025, 6_865e6),
            geo_member("Operating assets", "stz:SegmentOperatingAssets",
                       "Craft Beer Business", 2025, 120e6)]
    rows.append(consolidated("Total assets", "us-gaap:Assets", 2025, 0.0))
    _, _, totals = classify(rows)
    assert totals["geo"]["Assets"][2025] is None

def test_assets_is_a_balance_not_a_flow():
    """Darling's entire segment "Assets" table was "Gain on sale of assets", and
    Constellation's Total came from a tax reconciliation on asset disposals."""
    from utils.constants import SEGMENT_METRIC_GROUPS
    cfg = SEGMENT_METRIC_GROUPS["Assets"]
    for flow in ("Gain on sale of assets", "Impairment of long-lived assets",
                 "Proceeds from sale of assets", "Long-lived assets, estimated fair value",
                 "Net income tax provision (benefit) on disposition of assets",
                 "Right-of-use assets obtained in exchange for finance lease liabilities",
                 "Derivative assets", "Debt issued for assets",
                 "Actual return on plan assets"):
        assert not SegmentDataRepository._matches_metric(flow, cfg), flow
    for balance in ("Total assets", "Assets", "Long-lived tangible assets",
                    "Carrying values of long-lived assets", "Operating assets",
                    "Long-lived assets"):
        assert SegmentDataRepository._matches_metric(balance, cfg), balance


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("segment total tests passed")
