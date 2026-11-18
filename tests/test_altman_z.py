import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def test_formula_and_ratios():
    from data.repository import altman_z_score
    # CA 100, CL 60, TA 200, RE 50, EBIT 30, MVE 300, TL 120, Revenue 400
    s = altman_z_score(100, 60, 200, 50, 30, 300, 120, 400)
    assert (s["a"], s["b"], s["c"], s["d"], s["e"]) == (0.2, 0.25, 0.15, 2.5, 2.0)
    assert s["z"] == pytest.approx(1.2 * 0.2 + 1.4 * 0.25 + 3.3 * 0.15 + 0.6 * 2.5 + 1.0 * 2.0)
    assert s["z"] == pytest.approx(4.585)


def test_negative_inputs_are_scored_not_dropped():
    from data.repository import altman_z_score
    # Negative working capital, accumulated deficit and an operating loss are real cases (LOW, AZO).
    s = altman_z_score(50, 80, 200, -40, -10, 100, 150, 300)
    assert s["a"] == -0.15 and s["b"] == -0.2 and s["c"] == -0.05


@pytest.mark.parametrize("args", [
    (None, 60, 200, 50, 30, 300, 120, 400),   # missing current assets
    (100, 60, 200, None, 30, 300, 120, 400),  # missing retained earnings
    (100, 60, 200, 50, 30, None, 120, 400),   # no market value of equity
    (100, 60, 0, 50, 30, 300, 120, 400),      # zero total assets
    (100, 60, 200, 50, 30, 300, 0, 400),      # zero total liabilities
    (100, 60, 200, 50, 500, 300, 120, 400),   # EBIT above revenue: AV scale error
])
def test_unusable_inputs_give_no_score(args):
    from data.repository import altman_z_score
    assert altman_z_score(*args) is None


def test_zones_follow_the_displayed_value():
    from data.repository import altman_zone
    assert altman_zone(1.794) == "red"
    assert altman_zone(1.8) == "yellow" and altman_zone(1.7951) == "yellow"  # shows as 1.80
    assert altman_zone(2.994) == "yellow"
    assert altman_zone(3.0) == "green" and altman_zone(2.996) == "green"     # shows as 3.00
    assert altman_zone(-0.5) == "red" and altman_zone(9.15) == "green"


def test_share_counts_around_the_close_must_agree():
    from datetime import date
    from data.repository import share_count_is_stable
    # Alpha Vantage WMT: 2024-01-31 carries the 3:1 split twice (24.2B).
    wmt = [(date(2023, 7, 31), 2.693e9), (date(2023, 10, 31), 2.693e9), (date(2024, 1, 31), 24.229e9),
           (date(2024, 4, 30), 8.053e9), (date(2024, 7, 31), 8.044e9), (date(2024, 10, 31), 8.038e9)]
    assert not share_count_is_stable(wmt, date(2024, 1, 31))
    # A split between the close and the next report: both bases inside six months.
    assert not share_count_is_stable(wmt[:2] + wmt[3:], date(2024, 1, 31))
    assert share_count_is_stable(wmt, date(2024, 10, 31))
    # Kroger: a bad row six months later must not blank a clean year end.
    kr = [(date(2025, 10, 31), 655e6), (date(2026, 1, 31), 631e6), (date(2026, 4, 30), 615e6), (date(2026, 7, 31), 995.4e6)]
    assert share_count_is_stable(kr, date(2026, 1, 30))
    # ANET-style gap: the reports either side are a year apart but agree.
    gap = [(date(2025, 3, 31), 1260e6), (date(2026, 3, 31), 1273.8e6), (date(2026, 6, 30), 1274.9e6)]
    assert share_count_is_stable(gap, date(2025, 12, 31))
    assert not share_count_is_stable([(date(2026, 3, 31), 0.3e6), (date(2026, 6, 30), 256.9e6)], date(2026, 1, 15))
    assert share_count_is_stable([(date(2025, 1, 31), 5e8)], date(2025, 1, 31))  # nothing to compare
    merger = [(date(2021, 7, 31), 368e6), (date(2021, 10, 31), 526e6)]     # ADI + Maxim: real, 1.43x
    assert share_count_is_stable(merger, date(2021, 10, 31))
    three_for_two = [(date(2025, 3, 31), 100e6), (date(2025, 6, 30), 150e6)]
    assert not share_count_is_stable(three_for_two, date(2025, 5, 15))
    assert share_count_is_stable([], date(2025, 1, 31))
