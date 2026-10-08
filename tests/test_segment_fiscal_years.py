"""Each Segments column must hold one fiscal year, and only segments.

Found on Wendy's (WEN): its 52/53-week years end on Jan-3-2021, Jan-2-2022,
Jan-1-2023 and then Dec-31-2023. Bucketing by the calendar year of the end date
put fiscal 2022 and fiscal 2023 in the same "2023" column; the first one won,
fiscal 2023 vanished, and every earlier year moved one column right. SEC's own
dei:DocumentFiscalYearFocus for the period ending 2023-01-01 is 2022.

The same table listed the revenue-by-source lines (Sales, Franchise fees, …,
filed on srt:ProductOrServiceAxis) beside the three operating segments, so the
Business Segments rows added up to twice the company.
"""
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

from data.repository import SegmentDataRepository as Segments  # noqa: E402

SEGMENTS = "us-gaap:StatementBusinessSegmentsAxis"
PRODUCTS = "srt:ProductOrServiceAxis"


def fact(member, value, start, end, axis=SEGMENTS, rfy=None, heading="Segments"):
    return {
        "dimension": axis,
        "dimension_member_label": member,
        "dimension_label": member,
        "full_dimension_label": f"{heading}: {member}",
        "original_label": "Revenues",
        "concept": "us-gaap:Revenues",
        "numeric_value": value,
        "unit_ref": "usd",
        "period_type": "duration",
        "period_start": start,
        "period_end": end,
        "period_instant": None,
        "report_fiscal_year": rfy or end.year,
        "filing_date": date(end.year, 3, 1),
    }


# Wendy's U.S. revenue as filed in its 10-Ks (thousands → dollars).
WEN_US = [
    fact("Wendy's U.S.", 1_431_382_000, date(2019, 12, 30), date(2021, 1, 3)),
    fact("Wendy's U.S.", 1_567_496_000, date(2021, 1, 4), date(2022, 1, 2)),
    fact("Wendy's U.S.", 1_750_242_000, date(2022, 1, 3), date(2023, 1, 1)),
    fact("Wendy's U.S.", 1_815_845_000, date(2023, 1, 2), date(2023, 12, 31)),
    fact("Wendy's U.S.", 1_859_745_000, date(2024, 1, 1), date(2024, 12, 29)),
]


def test_a_year_ending_in_early_january_belongs_to_the_year_before():
    years = [Segments._get_row_year(r) for r in WEN_US]
    assert years == [2020, 2021, 2022, 2023, 2024]


def test_every_wendys_year_lands_in_its_own_column():
    biz, _geo, _tot = Segments._classify_segment_rows(WEN_US, [2020, 2021, 2022, 2023, 2024])
    assert biz["Revenues"]["Wendy's U.S."] == {
        2020: 1431.382, 2021: 1567.496, 2022: 1750.242, 2023: 1815.845, 2024: 1859.745}


def test_a_mislabelled_report_year_does_not_move_a_period():
    # HSY: a fiscal-2017 fact stored under report_fiscal_year 2016.
    row = fact("North America", 1, date(2017, 1, 1), date(2017, 12, 31), rfy=2016)
    assert Segments._get_row_year(row) == 2017


def test_a_quarter_inside_a_10k_is_not_an_annual_value():
    row = fact("North America", 1, date(2023, 10, 1), date(2023, 12, 31))
    assert Segments._get_row_year(row) is None


def test_revenue_by_source_does_not_join_the_operating_segments():
    rows = WEN_US + [
        fact("Sales", 925_905_000, date(2024, 1, 1), date(2024, 12, 29),
             axis=PRODUCTS, heading="Product and Service"),
    ]
    biz, _geo, _tot = Segments._classify_segment_rows(rows, [2024])
    assert set(biz["Revenues"]) == {"Wendy's U.S."}


def test_a_company_without_segments_keeps_its_product_lines():
    rows = [fact("Grocery", 5_000_000_000, date(2024, 1, 1), date(2024, 12, 31),
                 axis=PRODUCTS, heading="Product and Service")]
    biz, _geo, _tot = Segments._classify_segment_rows(rows, [2024])
    assert set(biz["Revenues"]) == {"Grocery"}


def test_a_mid_year_balance_is_not_a_year_end_balance():
    year_end = dict(fact("Restaurants", 1, date(2023, 1, 1), date(2023, 12, 31)))
    def assets(value, day):
        return {**year_end, "original_label": "Assets", "concept": "us-gaap:Assets",
                "numeric_value": value, "period_type": "instant", "period_start": None,
                "period_end": None, "period_instant": day}
    rows = [assets(9_000_000_000, date(2023, 6, 30)),
            assets(7_000_000_000, date(2023, 12, 31)), year_end]
    biz, _geo, _tot = Segments._classify_segment_rows(rows, [2023])
    assert biz["Assets"]["Restaurants"][2023] == 7000.0


def test_a_restated_figure_replaces_the_original():
    # Campbell's FY2021 Snacks: 3,944 in the FY2021 10-K, restated to 3,855 a
    # year later. Rows arrive in filing-year order, oldest first.
    original = {**fact("Snacks", 3_944_000_000, date(2020, 8, 3), date(2021, 8, 1), rfy=2021),
                "filing_date": date(2021, 9, 23)}
    restated = {**fact("Snacks", 3_855_000_000, date(2020, 8, 3), date(2021, 8, 1), rfy=2022),
                "filing_date": date(2022, 9, 22)}
    biz, _geo, _tot = Segments._classify_segment_rows([original, restated], [2021])
    assert biz["Revenues"]["Snacks"][2021] == 3855.0


def test_a_restatement_wins_even_when_its_label_is_worded_differently():
    # AppLovin's FY2025 10-K restated 2024 to continuing operations and re-cased
    # "Total Revenue" to "Total revenue".
    original = {**fact("Advertising", 2_688_993_000, date(2024, 1, 1), date(2024, 12, 31)),
                "original_label": "Total Revenue", "filing_date": date(2025, 2, 27)}
    restated = {**fact("Advertising", 1_726_202_000, date(2024, 1, 1), date(2024, 12, 31)),
                "original_label": "Total revenue", "filing_date": date(2026, 2, 19)}
    biz, _geo, _tot = Segments._classify_segment_rows([original, restated], [2024])
    assert biz["Revenues"]["Advertising"][2024] == 1726.202


def test_a_reorganised_year_shows_only_the_newest_filings_segments():
    # CVS FY2022: the original 10-K split it into Pharmacy Services / Retail-LTC;
    # the FY2023 10-K restated it as Health Services / Pharmacy & Consumer Wellness.
    def cvs(member, value, filed):
        return {**fact(member, value, date(2022, 1, 1), date(2022, 12, 31)),
                "filing_date": filed}
    rows = [cvs("Pharmacy Services", 169_236_000_000, date(2023, 2, 8)),
            cvs("Retail/LTC", 106_594_000_000, date(2023, 2, 8)),
            cvs("Health Services", 169_576_000_000, date(2024, 2, 7)),
            cvs("Pharmacy & Consumer Wellness", 108_596_000_000, date(2024, 2, 7))]
    biz, _geo, _tot = Segments._classify_segment_rows(rows, [2022])
    assert set(biz["Revenues"]) == {"Health Services", "Pharmacy & Consumer Wellness"}


def test_continuing_and_discontinued_operations_are_not_segments():
    axis = "us-gaap:StatementOperatingActivitiesSegmentAxis"
    rows = [fact("Continuing Operations", 9_000_000_000, date(2023, 1, 1), date(2023, 12, 31), axis=axis),
            fact("Discontinued Operations, Held-for-sale or Disposed of by Sale", 1,
                 date(2023, 1, 1), date(2023, 12, 31), axis=axis),
            fact("Wholesale Footwear", 1_000_000_000, date(2023, 1, 1), date(2023, 12, 31), axis=axis)]
    biz, _geo, _tot = Segments._classify_segment_rows(rows, [2023])
    assert set(biz["Revenues"]) == {"Wholesale Footwear"}


def test_an_aggregate_listed_beside_its_parts_is_withheld():
    # Apple lists "Products" beside iPhone, Mac, iPad and Wearables on one axis.
    rows = [fact(m, v, date(2024, 9, 29), date(2025, 9, 27), axis=PRODUCTS, heading="Product and Service")
            for m, v in (("Products", 307_003e6), ("iPhone", 209_586e6), ("Mac", 33_708e6),
                         ("iPad", 28_023e6), ("Wearables", 35_686e6), ("Services", 109_158e6))]
    total = {**fact("x", 416_161e6, date(2024, 9, 29), date(2025, 9, 27)),
             "_is_ndim": True, "dimension": None, "dimension_member_label": None,
             "full_dimension_label": None}
    biz, _geo, _tot = Segments._classify_segment_rows(rows + [total], [2025])
    assert "Products" not in biz["Revenues"]
    assert sum(v[2025] for v in biz["Revenues"].values()) == 416_161.0


def test_a_country_beside_its_region_is_withheld():
    geo = "srt:StatementGeographicalAxis"
    rows = [fact(m, v, date(2024, 1, 1), date(2024, 12, 31), axis=geo, heading="Geographical")
            for m, v in (("Americas", 2_474e6), ("United States", 2_255e6),
                         ("Europe", 1_057e6), ("Asia", 609e6))]
    total = {**fact("x", 4_140e6, date(2024, 1, 1), date(2024, 12, 31)),
             "_is_ndim": True, "dimension": None, "dimension_member_label": None,
             "full_dimension_label": None}
    _biz, geo_data, _tot = Segments._classify_segment_rows(rows + [total], [2024])
    assert set(geo_data["Revenues"]) == {"Americas", "Europe", "Asia"}






def test_a_revenue_member_carrying_its_cost_table_label_is_named_by_its_stream():
    # C3.ai: subscription revenue arrives labelled "Cost of subscription".
    rows = [fact("Cost of subscription", 227_090_000, date(2025, 5, 1), date(2026, 4, 30),
                 axis=PRODUCTS, heading="Product and Service")]
    biz, _geo, _tot = Segments._classify_segment_rows(rows, [2026])
    assert list(biz["Revenues"]) == ["Subscription"]


def test_the_xbrl_element_decides_the_metric_over_label_words():
    # Campbell's capital expenditure is labelled "Purchases of plant assets".
    capex = {**fact("Meals & Beverages", 156_000_000, date(2018, 7, 30), date(2019, 7, 28)),
             "concept": "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
             "original_label": "Purchases of plant assets"}
    biz, _geo, _tot = Segments._classify_segment_rows([capex], [2019])
    assert "Assets" not in biz
    assert biz["Capital Expenditure"]["Meals & Beverages"][2019] == 156.0


def test_an_acronym_filed_in_capitals_stays_an_acronym():
    assert Segments._title_case_member("GIS") == "GIS"            # DXC
    assert Segments._title_case_member("FHS") == "FHS"            # Restaurant Brands
    assert Segments._title_case_member("RH SEGMENT") == "RH Segment"
    assert Segments._title_case_member("UNITED STATES") == "United States"
    assert Segments._title_case_member("OTHER") == "Other"


def test_a_restatement_is_not_undone_by_an_older_filings_element():
    # Synopsys FY2022 Europe: 493.4 under us-gaap:Revenues (FY2022 10-K),
    # restated to 430.4 under RevenueFromContract… (FY2024 10-K). The Total's
    # element is us-gaap:Revenues, last filed in the older 10-K.
    slot = ("geo", "Revenues", "Europe", 2022)
    geo = {"Revenues": {"Europe": {2022: 430.369}}}
    Segments._align_members_with_total(
        {}, geo, {("geo", "Revenues", 2022): "us-gaap:Revenues"},
        {slot + ("us-gaap:Revenues",): (493.43, "Geographical: Europe", date(2022, 12, 12))},
        {slot}, {slot: "Geographical: Europe"}, {slot: date(2024, 12, 19)})
    assert geo["Revenues"]["Europe"][2022] == 430.369


def test_a_column_is_sized_by_the_values_it_shows_not_every_filing_repeating_them():
    # AppLovin 2024 geography: 2,689 + 2,020 in the FY2024 10-K, restated to
    # 1,726 + 1,498 in the FY2025 10-K. The column shows the restatement.
    geo = {"Revenues": {"United States": {2024: 1726.2}, "Rest of World (RoW)": {2024: 1497.9}}}
    filed = {("geo", "Revenues", "United States", 2024): date(2026, 2, 19),
             ("geo", "Revenues", "Rest of World (RoW)", 2024): date(2026, 2, 19)}
    largest, sums = Segments._column_sizes({}, geo, filed)
    assert round(sums[("geo", "Revenues", 2024)], 1) == 3224.1
    assert largest[("geo", "Revenues", 2024)] == 1726.2


def test_a_wrapped_segment_stays_with_its_unwrapped_years():
    # Mondelez: "Segments: Europe" in its FY2023 10-K, "Consolidation Items:
    # Operating Segments, Segments: Europe" from FY2024; Europe is also on its
    # geographic axis.
    wrapped = {"dimension": "srt:ConsolidationItemsAxis", "dimension_member_label": "Operating Segments",
               "dimension_label": "Europe",
               "full_dimension_label": "Consolidation Items: Operating Segments, Segments: Europe"}
    axes = frozenset({"us-gaap:StatementBusinessSegmentsAxis"})
    assert Segments._classify_member(wrapped, {"europe"}, axes) == ("business", "Europe")


def test_every_spelling_of_the_segment_wrapper_is_recognised():
    # Labels found across our companies for us-gaap:OperatingSegmentsMember.
    for label in ("Operating Segments", "Operating segment", "Segment", "Segments",
                  "Reporting Segments", "Business Segments", "Reportable Operating Segments",
                  "Total for operating segments", "Total segment profits", "Segment totals",
                  "Total segment net revenue", "Operating groups", "Operating"):
        assert Segments.is_wrapper_label(label), label
    for label in ("Intersegment eliminations", "Segment reconciling items", "Corporate, non-segment",
                  "All other segments", "Europe"):
        assert not Segments.is_wrapper_label(label), label


def test_a_new_segment_profit_element_is_kept_in_the_years_it_alone_is_filed():
    # ADM: us-gaap:OperatingIncomeLoss through 2023, adm:OperatingProfitAdjusted from 2024.
    biz = {"Operating Profit Before Tax": {"Nutrition": {2023: 427.0, 2025: 417.0}}}
    concepts = {("business", "Operating Profit Before Tax", "Nutrition", 2023): {"us-gaap:OperatingIncomeLoss"},
                ("business", "Operating Profit Before Tax", "Nutrition", 2025): {"adm:OperatingProfitAdjusted"}}
    import collections
    totals = {("business", "Operating Profit Before Tax"): collections.Counter({"us-gaap:OperatingIncomeLoss": 3})}
    Segments._drop_offmeasure_members(biz, {}, concepts, totals)
    assert biz["Operating Profit Before Tax"]["Nutrition"] == {2023: 427.0, 2025: 417.0}


def test_a_label_that_describes_its_segment_is_named_by_the_segment():
    # CDW: "Public Segment: Government Agencies, Education and Healthcare" in some
    # years, "Public" in others.
    rows = [fact("Public Segment: Government Agencies, Education and Healthcare", 9e9, date(2024, 1, 1), date(2024, 12, 31)),
            fact("Public", 8e9, date(2023, 1, 1), date(2023, 12, 31))]
    biz, _geo, _tot = Segments._classify_segment_rows(rows, [2023, 2024])
    assert list(biz["Revenues"]) == ["Public"]


def test_an_element_name_used_as_a_label_reads_as_words():
    assert Segments.readable_member("HomeBuildingMember") == "Home Building"          # D.R. Horton
    assert Segments.readable_member("Auto Parts Stores [ Member]") == "Auto Parts Stores"
    assert Segments.readable_member("Retail (Member)") == "Retail"
    assert Segments.readable_member("pf0:AsiaMember") == "Asia"
    assert Segments.readable_member("Direct to Consumer: (1)") == "Direct to Consumer"
    assert Segments.readable_member("iPhone") == "iPhone"                             # brands untouched
    assert Segments.readable_member("McDonald's") == "McDonald's"


def test_a_single_segment_companys_total_is_not_a_segment():
    for label in ("Company's One Reportable Operating Segment", "Operating and Reportable Segments"):
        assert Segments.is_wrapper_label(label), label


def test_subsegments_that_repeat_under_every_parent_do_not_replace_them():
    # MercadoLibre: each country segment split into Commerce and Fintech.
    def row(parent, child, value):
        label = f"Segments: {parent}" + (f", Subsegments: {child}" if child else "")
        return {**fact(parent, value, date(2025, 1, 1), date(2025, 12, 31)),
                "dimension_label": child or parent, "full_dimension_label": label}
    rows = [row("Brazil", None, 100e6), row("Brazil", "Commerce", 60e6), row("Brazil", "Fintech", 40e6),
            row("Mexico", None, 50e6), row("Mexico", "Commerce", 30e6), row("Mexico", "Fintech", 20e6)]
    kept = Segments._resolve_subsegments(rows)
    assert {r["full_dimension_label"] for r in kept} == {"Segments: Brazil", "Segments: Mexico"}


def test_an_exclusion_reads_the_element_name_as_well_as_the_label():
    # Caterpillar: us-gaap:DerivativeAssets labelled "Net Amount of Assets".
    from utils.constants import SEGMENT_METRIC_GROUPS
    assert not Segments._matches_metric("Net Amount of Assets", SEGMENT_METRIC_GROUPS["Assets"],
                                        "us-gaap:DerivativeAssets")


def test_a_wrapper_member_on_a_later_axis_does_not_slice_the_fact():
    # Microsoft FY2018 10-K: the segment first, the reconciling-item wrapper second.
    row = {"dimension": "us-gaap:StatementBusinessSegmentsAxis", "dimension_member_label": "Intelligent Cloud",
           "full_dimension_label": "Statement, Business Segments: Intelligent Cloud, "
                                   "Segment Reporting Reconciling Item: Reportable Segments"}
    assert Segments._row_breakdown(row) == ("business", "canon")


def test_intersegment_qualified_segment_totals_are_wrappers():
    assert Segments.is_wrapper_label("Reportable Segments Including Intersegment Eliminations")   # Caterpillar
    assert Segments.is_wrapper_label("Operating segments, inclusive of intersegment sales")        # GE Vernova
