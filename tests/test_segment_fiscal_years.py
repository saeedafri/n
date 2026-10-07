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


def test_a_rolled_up_region_beside_its_own_part_keeps_its_filed_name():
    # Amphenol: the workbook maps "Other foreign locations" to Foreign, which
    # beside China reads as if it included China.
    geo = {"Revenues": {"United States": {2023: 4405.4}, "China": {2023: 2884.0},
                        "Foreign": {2023: 5265.3}}}
    Segments._name_overlaps_as_filed(geo, {"Foreign": {2023: "Other foreign locations"}})
    assert list(geo["Revenues"]) == ["United States", "China", "Other foreign locations"]


def test_only_the_years_filed_as_a_residual_are_renamed():
    # Cadence: "Other Americas" beside the United States in 2014, the whole
    # "Americas" in 2023.
    geo = {"Revenues": {"United States": {2014: 696.6, 2023: None},
                        "Americas": {2014: 23.4, 2023: 1759.8}}}
    Segments._name_overlaps_as_filed(geo, {"Americas": {2014: "Other Americas", 2023: "Americas"}})
    assert geo["Revenues"]["Americas"] == {2014: None, 2023: 1759.8}
    assert geo["Revenues"]["Other Americas"] == {2014: 23.4, 2023: None}


def test_a_rolled_up_region_with_nothing_inside_it_keeps_the_business_name():
    geo = {"Revenues": {"United States": {2023: 10.0}, "Europe": {2023: 5.0}}}
    Segments._name_overlaps_as_filed(geo, {"Europe": {2023: "Other Europe"}})
    assert list(geo["Revenues"]) == ["United States", "Europe"]


def test_two_places_one_filing_lists_apart_never_share_a_row():
    # ADM files the Cayman Islands and the United Kingdom side by side; the
    # business workbook maps both to "United Kingdom".
    geo_axis = "srt:StatementGeographicalAxis"
    rows = [fact(name, value, date(2023, 1, 1), date(2023, 12, 31), axis=geo_axis, heading="Geographical")
            for name, value in (("United States", 38_783e6), ("Cayman Islands", 7_646e6),
                                ("United Kingdom", 2_219e6))]
    _biz, geo, _tot = Segments._classify_segment_rows(rows, [2023])
    assert geo["Revenues"]["Cayman Islands"][2023] == 7646.0
    assert geo["Revenues"]["United Kingdom"][2023] == 2219.0


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
