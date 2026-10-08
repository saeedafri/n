"""The Segments tab must show the segment's own numbers, and all of its segments.

Two faults found on McDonald's, both visible at once in the Business Segments
table: every member's Revenues row held "Net restaurant purchases (sales)"
instead of "Total revenues", and the largest segment of the company — "U.S." —
was missing from all five metrics while the Total still counted it.
"""
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

from data.repository import SegmentDataRepository as Segments  # noqa: E402

AXIS = "us-gaap:StatementBusinessSegmentsAxis"


def segment_row(member, label, concept, value, year, filed="2025-02-25"):
    """One dimensioned 10-K fact, shaped as coreiq_filing_metrics_v5 returns it."""
    return {
        "dimension": AXIS,
        "dimension_member_label": member,
        "dimension_label": member,
        "full_dimension_label": f"Statement, Business Segments: {member}",
        "original_label": label,
        "concept": concept,
        "numeric_value": value,
        "unit_ref": "usd",
        "period_type": "duration",
        "period_start": date(year, 1, 1),
        "period_end": date(year, 12, 31),
        "period_instant": None,
        "report_fiscal_year": year,
        "filing_date": date.fromisoformat(filed),
    }


# McDonald's FY2024 10-K, as filed: three segments, one of them named after a
# place, each carrying a revenue fact AND a goodwill-movement fact whose label
# also contains the word "sales".
MCD_2024 = [
    segment_row("U.S.", "Net restaurant purchases (sales)",
                "us-gaap:GoodwillPeriodIncreaseDecrease", 18_000_000, 2024),
    segment_row("U.S.", "Total revenues", "us-gaap:Revenues", 10_631_000_000, 2024),
    segment_row("International Operated Markets", "Net restaurant purchases (sales)",
                "us-gaap:GoodwillPeriodIncreaseDecrease", 3_000_000, 2024),
    segment_row("International Operated Markets", "Total revenues",
                "us-gaap:Revenues", 12_628_000_000, 2024),
    segment_row("International Developmental Licensed Markets & Corporate",
                "Net restaurant purchases (sales)",
                "us-gaap:GoodwillPeriodIncreaseDecrease", 158_000_000, 2024),
    segment_row("International Developmental Licensed Markets & Corporate",
                "Total revenues", "us-gaap:Revenues", 2_661_000_000, 2024),
]


def business_revenues(rows, years=(2024,)):
    biz, _geo, _totals = Segments._classify_segment_rows(rows, list(years))
    return biz.get("Revenues", {})


def test_a_member_shows_the_element_the_metric_declares():
    """"Net restaurant purchases (sales)" matches the Revenues keywords and sorts
    before "Total revenues", so first-wins used to put $3m of goodwill movement
    where $12,628m of segment revenue belongs."""
    revenues = business_revenues(MCD_2024)
    assert revenues["International Operated Markets"][2024] == 12_628.0
    assert revenues["International Developmental Licensed Markets & Corporate"][2024] == 2_661.0


def test_a_segment_named_after_a_place_survives_on_a_real_segment_axis():
    """"U.S." is one of McDonald's three reportable segments. The axis carries two
    members no one could read as geography, so it is a segment breakdown, not
    geography filed in the wrong place. It is shown under the canonical spelling
    of the place, as the geographic table shows one."""
    assert business_revenues(MCD_2024)["United States"][2024] == 10_631.0


def test_the_members_add_up_to_what_McDonalds_filed():
    revenues = business_revenues(MCD_2024)
    assert round(sum(v[2024] for v in revenues.values()), 1) == 25_920.0


def test_regions_on_the_operating_segment_axis_are_the_segments():
    """Decided 2026-10-08: StatementBusinessSegmentsAxis is the operating-segment
    axis, so Mondelez's and Apple's regional segments are business segments."""
    rows = [
        segment_row("United States", "Total revenues", "us-gaap:Revenues", 900_000_000, 2024),
        segment_row("EMEA", "Total revenues", "us-gaap:Revenues", 100_000_000, 2024),
    ]
    assert set(business_revenues(rows)) == {"United States", "Europe, Middle East and Africa (EMEA)"}


def test_geography_on_an_axis_that_is_not_for_segments_is_still_dropped():
    rows = [{**segment_row(name, "Total revenues", "us-gaap:Revenues", value, 2024),
             "dimension": "us-gaap:StatementOperatingActivitiesSegmentAxis",
             "full_dimension_label": f"Operating Activities Segment: {name}"}
            for name, value in (("United States", 900_000_000), ("EMEA", 100_000_000))]
    assert business_revenues(rows) == {}


def test_one_segment_renamed_is_still_one_segment():
    """McDonald's renamed the segment from "United States" to "U.S." in 2021.
    Both spellings canonicalise to one member holding every year, instead of two
    rows each missing the other's years."""
    rows = [
        segment_row("United States", "Total revenues", "us-gaap:Revenues",
                    7_828_500_000, 2020, filed="2021-02-23"),
        segment_row("Other Business", "Total revenues", "us-gaap:Revenues",
                    1_000_000_000, 2020, filed="2021-02-23"),
        segment_row("U.S.", "Total revenues", "us-gaap:Revenues",
                    10_631_000_000, 2024),
        segment_row("Other Business", "Total revenues", "us-gaap:Revenues",
                    2_000_000_000, 2024),
    ]
    revenues = business_revenues(rows, years=(2020, 2024))
    assert "U.S." not in revenues
    assert revenues["United States"] == {2020: 7_828.5, 2024: 10_631.0}


def test_a_filer_whose_elements_are_all_its_own_is_untouched():
    """Nothing is declared for this metric, so first-wins still decides and the
    change cannot reshuffle a company it was never about."""
    rows = [
        segment_row("Retail", "Net sales", "acme:SegmentNetSales", 500_000_000, 2024),
        segment_row("Wholesale", "Net sales", "acme:SegmentNetSales", 300_000_000, 2024),
    ]
    revenues = business_revenues(rows)
    assert revenues["Retail"][2024] == 500.0
    assert revenues["Wholesale"][2024] == 300.0


# ── screening: a geography criterion must find a place-named segment ─────────

def cache_entries(biz, geo):
    from data.screening_service import _segment_cache_entries
    return _segment_cache_entries("TEST", biz, geo)


def test_screening_indexes_a_place_named_segment_as_geography():
    """McDonald's files no geographic revenue at all, so "United States" reaches
    the screener only through its segment disclosure."""
    biz = {"Revenues": {"United States": {2024: 10_631.0},
                        "International Operated Markets": {2024: 12_628.0}}}
    entries = cache_entries(biz, {})
    geography = {(member, year, value) for _t, st, _m, member, year, value in entries
                 if st == "geographical"}
    assert geography == {("United States", 2024, 10_631.0)}
    # the segment it is not a place stays out of geography
    assert "International Operated Markets" not in {m for m, _y, _v in geography}


def test_a_real_geographic_disclosure_is_never_mixed_with_segments():
    """Amazon reports a "North America" segment AND a United States / Germany /
    … geographic breakdown. They are different cuts of the same revenue, so a
    filer that publishes geography keeps geography exactly as filed — matching
    member by member is not enough, because "North America" is in one cut only."""
    biz = {"Revenues": {"North America": {2024: 387_497.0}}}
    geo = {"Revenues": {"United States": {2024: 438_000.0}}}
    geography = {member for _t, st, _m, member, _y, _v in cache_entries(biz, geo)
                 if st == "geographical"}
    assert geography == {"United States"}


def test_a_metric_the_geographic_disclosure_skips_still_gets_the_segment():
    """The rule is per metric and year: a filer that breaks revenue down by
    country but not assets still gets its place-named segment assets indexed."""
    biz = {"Assets": {"United States": {2024: 22_547.0}}}
    geo = {"Revenues": {"United States": {2024: 438_000.0}}}
    assets = {(member, value) for _t, st, metric, member, _y, value
              in cache_entries(biz, geo) if st == "geographical" and metric == "Assets"}
    assert assets == {("United States", 22_547.0)}


def test_the_business_index_still_holds_every_segment():
    biz = {"Revenues": {"United States": {2024: 10_631.0}, "Retail": {2024: 50.0}}}
    business = {member for _t, st, _m, member, _y, _v in cache_entries(biz, {})
                if st == "business"}
    assert business == {"United States", "Retail"}


# ── the cache must notice that the rules changed ─────────────────────────────

def test_the_classifier_version_covers_every_rule_that_decides_a_segment():
    """The refresh gate watches the source table growing. Segment data comes from
    10-Ks, so a deploy that only changes classification would otherwise serve the
    old members until the next filing season."""
    import inspect
    from data import screening_service as svc

    version = svc._segment_classifier_version()
    assert len(version) == 12

    source = inspect.getsource(svc._segment_classifier_version)
    assert "rules_version()" in source and "_segment_cache_entries" in source
    # rules_version hashes the whole repository class, so no helper can be missed
    assert "inspect.getsource(SegmentDataRepository)" in inspect.getsource(Segments.rules_version)


def test_the_version_changes_when_a_rule_changes():
    import hashlib
    import inspect
    from data import screening_service as svc

    before = svc._segment_classifier_version()
    patched = inspect.getsource(Segments._classify_member) + "# a new rule"
    after = hashlib.sha1(
        (patched + "".join(
            inspect.getsource(fn) for fn in (
                Segments._classify_segment_rows, Segments._segment_axes,
                Segments._member_display_map, svc._segment_cache_entries)))
        .encode("utf-8", "replace")).hexdigest()[:12]
    assert before != after


# ── a member must measure what its Total measures ────────────────────────────

def ndim_row(label, concept, value, year):
    """A consolidated (non-dimensioned) fact, as the Total is picked from."""
    row = segment_row("", label, concept, value, year)
    row.update(dimension=None, dimension_member_label=None, dimension_label=None,
               full_dimension_label=None, _is_ndim=True)
    return row


def amex_row(member, label, concept, value, year, second_axis="Segments"):
    """American Express files segment revenue on a two-axis operating-segment
    wrapper: ConsolidationItems=Operating Segments + Segments=<the segment>."""
    row = segment_row("Operating Segments", label, concept, value, year)
    row.update(dimension="us-gaap:ConsolidationItemsAxis", dimension_label=member,
               full_dimension_label=f"Consolidation Items: Operating Segments, {second_axis}: {member}")
    return row


AXP_2025 = [
    # USCS carries the metric four ways at once; only one is what the Total means.
    amex_row("USCS", "Revenue from contracts with customers",
             "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", 15_626_000_000, 2025),
    amex_row("USCS", "Total revenues net of interest expense",
             "us-gaap:RevenuesNetOfInterestExpense", 34_814_000_000, 2025),
    amex_row("ICS", "Total revenues net of interest expense",
             "us-gaap:RevenuesNetOfInterestExpense", 13_000_000_000, 2025),
    ndim_row("Total revenues net of interest expense",
             "us-gaap:RevenuesNetOfInterestExpense", 72_229_000_000, 2025),
]


def test_a_member_is_read_with_the_element_its_total_used():
    """Nothing in the label separates AmEx's four revenue elements, so first-wins
    took the alphabetically-first one and listed $15.6bn of a segment under a
    $72.2bn Total."""
    revenues = business_revenues(AXP_2025, years=(2025,))
    assert revenues["USCS"][2025] == 34_814.0
    assert revenues["ICS"][2025] == 13_000.0


def test_realignment_will_not_reach_into_a_further_breakdown():
    """The Andersons file Rail revenue once for the segment and once for its
    Canadian slice, both us-gaap:SalesRevenueNet. Only a candidate filed under
    the same axes may replace the segment's own figure."""
    rail = segment_row("Operating Segments", "Sales and merchandising revenues",
                       "us-gaap:SalesRevenueNet", 172_123_000, 2017)
    rail.update(dimension="us-gaap:ConsolidationItemsAxis", dimension_label="Rail",
                full_dimension_label="Consolidation Items: Operating Segments, Segments: Rail")
    canada = segment_row("Rail", "Revenues", "us-gaap:Revenues", 13_300_000, 2017)
    canada.update(full_dimension_label="Segments: Rail, Geographical: Canada")
    rows = [rail, canada, ndim_row("Revenues", "us-gaap:Revenues", 4_160_000_000, 2017)]

    assert business_revenues(rows, years=(2017,))["Rail"][2017] == 172.123


def test_a_member_whose_element_the_metric_never_declared_is_left_alone():
    """Only a tie between two declared elements is settled this way. A member
    holding a filer's own element is measuring something else, and swapping it
    for a stray fact sharing the Total's element loses the real figure."""
    own = segment_row("Specialty", "Total revenues", "acme:SpecialtySalesMeasure",
                      500_000_000, 2024)
    rows = [own, ndim_row("Total revenues", "us-gaap:Revenues", 900_000_000, 2024)]
    assert business_revenues(rows)["Specialty"][2024] == 500.0


# ── screening: a segment criterion over a range of years ─────────────────────

def test_a_segment_year_range_makes_one_column_per_year_and_drops_nobody():
    """The range mirrors the financial one: display-only, one column per fiscal
    year, and a company missing a year reads N/A there instead of vanishing."""
    import pandas as pd
    from data import screening_service as svc

    universe = pd.DataFrame({"ticker": ["MCD", "KR"]})
    seen_years = []

    def fake_single_year(criterion, working_df):
        year = criterion["year"]
        seen_years.append(year)
        col = criterion["display_col"]
        # Kroger files no geography, so only McDonald's comes back — exactly what
        # the single-year path does today.
        frame = pd.DataFrame({"ticker": ["MCD"], col: [f"United States: {year}"]})
        return frame, {}

    real = svc.apply_segment_statement_criterion
    svc.apply_segment_statement_criterion = fake_single_year
    try:
        criterion = {"display_col": "Geo Seg Revenues ($mm)", "year_range": [2022, 2023],
                     "statement": "Geographical Segments", "segment_type": "geographical"}
        out, stats = svc._apply_segment_year_range_criterion(criterion, universe)
    finally:
        svc.apply_segment_statement_criterion = real

    assert seen_years == [2022, 2023]
    assert stats["year_cols"] == ["Geo Seg Revenues ($mm) [FY 2022]",
                                  "Geo Seg Revenues ($mm) [FY 2023]"]
    assert criterion["year_cols"] == stats["year_cols"]
    assert list(out["ticker"]) == ["MCD", "KR"]          # nobody dropped
    assert out.loc[out.ticker == "MCD", stats["year_cols"][0]].iloc[0] == "United States: 2022"
    assert pd.isna(out.loc[out.ticker == "KR", stats["year_cols"][0]].iloc[0])


def test_a_year_range_never_carries_a_value_filter():
    """Display-only, so each year is fetched unfiltered — a threshold meant for
    one year must not silently drop companies from the others."""
    import pandas as pd
    from data import screening_service as svc

    passed = []

    def capture(criterion, working_df):
        passed.append(criterion)
        return pd.DataFrame({"ticker": [], criterion["display_col"]: []}), {}

    real = svc.apply_segment_statement_criterion
    svc.apply_segment_statement_criterion = capture
    try:
        svc._apply_segment_year_range_criterion(
            {"display_col": "Biz Seg Revenues ($mm)", "year_range": [2024],
             "filter_enabled": True, "operator": "Greater Than", "value1": 5000.0},
            pd.DataFrame({"ticker": ["MCD"]}))
    finally:
        svc.apply_segment_statement_criterion = real

    assert passed[0]["filter_enabled"] is False
    assert passed[0]["value1"] == 0.0
    assert "year_range" not in passed[0]


def test_a_segment_year_range_survives_a_ticker_shared_by_two_companies():
    """The screening universe carries shared-ticker siblings (TSCO is Tractor
    Supply and Tesco). Mapping by a duplicated ticker index raised "Reindexing
    only valid with uniquely valued Index objects", the pipeline degraded the
    criterion, and the card read "0 of 4736 companies have data"."""
    import pandas as pd
    from data import screening_service as svc

    universe = pd.DataFrame({"ticker": ["MCD", "TSCO", "TSCO"],
                             "company_name": ["McDonald's", "Tractor Supply", "Tesco"]})

    def fake_single_year(criterion, working_df):
        col = criterion["display_col"]
        frame = working_df.copy()
        frame[col] = "United States: 1"
        frame[f"{col}__raw"] = [{"United States": 1.0}] * len(frame)
        return frame, {}

    real = svc.apply_segment_statement_criterion
    svc.apply_segment_statement_criterion = fake_single_year
    try:
        out, stats = svc._apply_segment_year_range_criterion(
            {"display_col": "Geo Seg Revenues ($mm)", "year_range": [2024]}, universe)
    finally:
        svc.apply_segment_statement_criterion = real

    assert len(out) == 3
    assert stats["with_data"] == 3
    assert out[stats["year_cols"][0]].notna().all()


def test_the_card_counts_a_geo_segment_year_range_through_the_pipeline():
    """The Geographical Segments wrapper passes the service a COPY of the
    criterion, so year_cols stamped there never reached the pipeline's dict:
    the columns filled but the card still said "0 of N companies have data"."""
    import pandas as pd
    from data import screening_service as svc

    universe = pd.DataFrame({"ticker": ["MCD", "KR", "TSCO", "TSCO"],
                             "company_name": ["McDonald's", "Kroger", "Tractor Supply", "Tesco"]})
    real_single = svc._apply_segment_year_range_criterion.__globals__["apply_segment_statement_criterion"]

    def fake_single_year(criterion, working_df):
        if criterion.get("year_range"):
            return svc._apply_segment_year_range_criterion(criterion, working_df)
        col = criterion["display_col"]
        frame = working_df[working_df["ticker"] != "KR"].copy()
        frame[col] = "United States: 1"
        return frame, {}

    real_universe = svc.get_base_company_universe
    svc.get_base_company_universe = lambda: universe.copy()
    svc.apply_segment_statement_criterion = fake_single_year
    try:
        criterion = {"type": "financial", "statement": "Geographical Segments",
                     "display_col": "Revenues ($mm) — FY 2022–2023",
                     "year_range": [2022, 2023]}
        out, trace = svc.recompute_working_set([criterion])
    finally:
        svc.get_base_company_universe = real_universe
        svc.apply_segment_statement_criterion = real_single

    assert criterion["year_cols"] == ["Revenues ($mm) — FY 2022–2023 [FY 2022]",
                                      "Revenues ($mm) — FY 2022–2023 [FY 2023]"]
    assert trace[0]["with_data"] == 3        # MCD + both TSCO rows; KR is N/A
    assert len(out) == 4                     # nobody dropped


# ── a slice of a segment is not the segment ──────────────────────────────────

def cmg_row(value, slice_label=None, segment="U. S. Segment"):
    """Chipotle FY2025: us-gaap:Revenues on the Operating Segments wrapper."""
    row = amex_row(segment, "Revenue", "us-gaap:Revenues", value, 2025)
    if slice_label:
        row["full_dimension_label"] = (
            f"Consolidation Items: Operating Segments, Product and Service: "
            f"{slice_label}, Segments: {segment}")
    return row


def test_a_segment_filed_only_with_a_further_axis_keeps_its_figure():
    """Abbott's Molecular (and Pulte's Florida, Nike's North America) exist only
    as Subsegments of a wrapped segment. With no less-sliced copy to prefer, the
    figure stays — only a slice of something filed whole is dropped."""
    molecular = cmg_row(817_000_000, segment="Molecular")
    molecular["full_dimension_label"] = (
        "Consolidation Items: Operating Segments, Segments: Diagnostics, "
        "Subsegments: Molecular")
    rows = [molecular, ndim_row("Revenue", "us-gaap:Revenues", 43_000_000_000, 2025)]
    assert business_revenues(rows, years=(2025,))["Molecular"][2025] == 817.0


def test_the_segment_reads_its_own_figure_whatever_order_the_slices_arrive_in():
    rows = [cmg_row(59_332_000, "Delivery service revenue", "Restaurants"),
            cmg_row(11_620_085_000, "Food and beverage revenue", "Restaurants"),
            cmg_row(11_679_417_000, segment="Restaurants"),
            ndim_row("Revenue", "us-gaap:Revenues", 11_925_601_000, 2025)]
    assert business_revenues(rows, years=(2025,))["Restaurants"][2025] == 11_679.417


def test_a_lookalike_measure_with_fewer_axes_does_not_displace_the_segment():
    """GE files Power's Total assets (us-gaap:Assets, 24,453m) with four axes and
    a contract-asset fact (838m, filer element) with two. Both match "Assets";
    only a less-sliced fact of the same declared-ness may win."""
    total = cmg_row(24_453_000_000, segment="Power")
    total.update(original_label="Total assets", concept="us-gaap:Assets",
                 full_dimension_label=("Consolidation Items: Operating segments, Segments: "
                                       "GE Industrial, Operating Activities: Continuing "
                                       "Operations, Subsegments: Power"))
    contract = cmg_row(838_000_000, segment="Power")
    contract.update(original_label="Contract and other deferred assets",
                    concept="ge:ContractAndOtherDeferredAssetsNoncurrent")
    kept = Segments._drop_wrapped_slices([total, contract])
    assert total in kept and contract in kept


# ── one member, one row: relabels, liabilities, place names ──────────────────

def product_row(label_on_axis, value, year, filed):
    """Chipotle's product axis, as filed."""
    row = segment_row(label_on_axis, "Total revenue", "us-gaap:Revenues", value, year, filed)
    row.update(dimension="srt:ProductOrServiceAxis",
               full_dimension_label=f"Product and Service: {label_on_axis}")
    return row


def test_a_relabelled_member_is_one_row():
    """Chipotle filed its products as "Food and Beverage" / "Delivery Service"
    through FY2023 and as "Food and beverage revenue" / "Delivery service
    revenue" from FY2024: each product listed twice with half the years. The
    FY2024 10-K repeats FY2023 under the new name with the same number — that
    agreement is what makes it one member."""
    rows = [
        product_row("Food and Beverage", 9_804_124_000, 2023, "2024-02-06"),
        product_row("Food and beverage revenue", 9_804_124_000, 2023, "2025-02-05"),
        product_row("Food and beverage revenue", 11_866_051_000, 2025, "2026-02-04"),
        product_row("Delivery Service", 67_525_000, 2023, "2024-02-06"),
        product_row("Delivery service revenue", 67_525_000, 2023, "2025-02-05"),
        product_row("Delivery service revenue", 59_550_000, 2025, "2026-02-04"),
    ]
    revenues = business_revenues(rows, years=(2023, 2025))
    assert revenues == {
        "Food and beverage revenue": {2023: 9_804.124, 2025: 11_866.051},
        "Delivery service revenue": {2023: 67.525, 2025: 59.55},
    }


def test_a_sub_line_named_like_its_segment_stays_separate():
    """Tesla's "Automotive" segment and its "Automotive sales" line differ only
    by "sales" but report different numbers — merging them would lose one."""
    rows = [product_row("Automotive", 20_821_000_000, 2019, "2020-02-13"),
            product_row("Automotive sales", 19_358_000_000, 2019, "2020-02-13")]
    assert business_revenues(rows, years=(2019,)) == {
        "Automotive": {2019: 20_821.0}, "Automotive sales": {2019: 19_358.0}}


def test_a_contract_liability_is_not_revenue():
    """"Unearned revenue" and "Breakage revenue" are gift-card and rewards
    liabilities; their labels say "revenue" and they were listed as segments."""
    from utils.constants import SEGMENT_METRIC_GROUPS
    revenues = SEGMENT_METRIC_GROUPS["Revenues"]
    for label in ("Unearned revenue", "Liability in unearned revenue", "Breakage revenue"):
        assert not Segments._matches_metric(label, revenues), label
    assert Segments._matches_metric("Total revenue", revenues)


def test_initials_lose_their_inner_spaces_and_nothing_else():
    """Chipotle's one segment is "U. S. Segment". Only the spacing is fixed: the
    geographic workbook groups places into regions, and running a business
    segment through it folded PriceSmart's four country segments into one."""
    assert Segments._strip_member_artifact("U. S. Segment") == "U.S."
    assert Segments._strip_member_artifact("Caribbean Operations Segment") == "Caribbean Operations"
    assert Segments._strip_member_artifact("Outside U.S.") == "Outside U.S."


def test_a_relabel_is_one_row_in_the_quarterly_view_too():
    """10-Q rows carry no report_fiscal_year, so a year-keyed agreement test saw
    every quarter as one "None" year and never matched: the quarterly table kept
    "Delivery Service" and "Delivery service revenue" apart."""
    def quarter_row(label, value, end, filed):
        row = product_row(label, value, end.year, filed)
        row.update(period_start=date(end.year, end.month - 2, 1), period_end=end,
                   report_fiscal_year=None)
        return row
    rows = [quarter_row("Delivery Service", 17_571_000, date(2023, 3, 31), "2023-04-26"),
            quarter_row("Delivery service revenue", 17_571_000, date(2023, 3, 31), "2024-04-25"),
            quarter_row("Delivery service revenue", 18_204_000, date(2024, 3, 31), "2024-04-25")]
    names = Segments._member_display_map(rows, set())
    assert set(names.values()) == {"Delivery service revenue"}
