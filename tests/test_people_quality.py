"""The automatic People quality check and the hourly upkeep (2026-11-13).
Small frames, no DB, no network."""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from data.people_quality import compare_runs, run_checks  # noqa: E402

PAY_PARTS = ["salary", "bonus", "stock_awards", "option_awards", "non_equity_incentive", "all_other_compensation"]


def people_frame(rows):
    df = pd.DataFrame(rows)
    for col in PAY_PARTS + ["total_pay"]:
        if col not in df:
            df[col] = None
    df["source"] = df.get("source", "SEC")
    df["filing_blob"] = df.get("filing_blob", "TICK/2025/DEF14A/filing.html")
    return df


EMPTY_CHANGES = pd.DataFrame(columns=["ticker", "change", "person", "titles", "title", "event_id", "moved_from",
                                      "moved_from_title", "effective_date", "notice_date", "moved_from_date",
                                      "is_board"])
EMPTY_LAYOFFS = pd.DataFrame(columns=["ticker", "source", "headline"])


def by_name(checks):
    return {c["name"]: c for c in checks}


def test_copied_pay_against_the_filing_and_general_sanity():
    people = people_frame([
        # Copied: the filing gives Mulligan a different figure in both years.
        dict(ticker="TGT", executive_name="John J. Mulligan", title="COO", year=2018, total_compensation=5_356_168),
        dict(ticker="TGT", executive_name="Michael E. McNamara", title="CIO", year=2018, total_compensation=5_356_168),
        dict(ticker="TGT", executive_name="John J. Mulligan", title="COO", year=2019, total_compensation=6_593_014),
        dict(ticker="TGT", executive_name="Michael E. McNamara", title="CIO", year=2019, total_compensation=6_593_014),
        # Parts larger than the total: an extraction fault of any kind.
        dict(ticker="ABC", executive_name="Jane Roe", title="CFO", year=2024, total_compensation=1_000_000,
             salary=900_000, stock_awards=800_000),
        # A 40x jump year over year.
        dict(ticker="ABC", executive_name="John Doe", title="CEO", year=2023, total_compensation=500_000),
        dict(ticker="ABC", executive_name="John Doe", title="CEO", year=2024, total_compensation=20_000_000),
        # Not a person.
        dict(ticker="ABC", executive_name="Human Resources", title="EVP", year=2024, total_compensation=900_000),
    ])
    filings = pd.DataFrame({
        "ticker": ["TGT"] * 4, "executive_name": ["John J. Mulligan", "Michael E. McNamara"] * 2,
        "year": [2018, 2018, 2019, 2019], "total_compensation": [8_575_733, 5_356_168, 10_491_554, 6_593_014]})
    layoffs = pd.DataFrame({"ticker": ["ADBE", "DIS"], "source": ["AV News"] * 2,
                            "headline": ["Disney Signs Adobe AI Deal Amid Reported Job Cut Plans"] * 2})
    checks = by_name(run_checks(people, EMPTY_CHANGES, layoffs, {}, filings))
    assert checks["People: copied pay (filing shows different figures)"]["items"] == \
        ["TGT John J. Mulligan / Michael E. McNamara: [2018, 2019]"]
    assert checks["People: pay parts do not add up to the total"]["items"] == ["ABC Jane Roe 2024"]
    assert checks["People: pay moves more than 20x from one year to the next (check)"]["items"]
    assert "Human Resources" not in " ".join(checks["People: copied pay (filing shows different figures)"]["items"])
    assert checks["Layoffs: one story filed under two or more companies"]["items"]


def test_genuine_equal_pay_is_not_a_failure():
    people = people_frame([
        dict(ticker="ELF", executive_name=n, title="SVP", year=y, total_compensation=v)
        for y, v in ((2024, 4_656_438), (2025, 5_056_194)) for n in ("Kory Marchisotto", "Scott Milsten")])
    filings = people[["ticker", "executive_name", "year", "total_compensation"]]
    checks = by_name(run_checks(people, EMPTY_CHANGES, EMPTY_LAYOFFS, {}, filings))
    assert checks["People: copied pay (filing shows different figures)"]["items"] == []
    assert checks["People: equal pay the filings confirm or do not cover (check)"]["items"]


def test_new_flags_failures_and_shrinking_data_are_reported():
    previous = {"sizes": {"people": 1000, "layoffs": 100},
                "checks": [{"name": "A", "must_be_zero": False, "items": ["x"]},
                           {"name": "B", "must_be_zero": True, "items": []}]}
    checks = [{"name": "A", "must_be_zero": False, "items": ["x", "y"]},     # y is new
              {"name": "B", "must_be_zero": True, "items": ["broken"]}]       # must be zero
    found = compare_runs(checks, previous, {"people": 850, "layoffs": 99})
    assert found["new"] == {"A": ["y"], "B": ["broken"]}
    assert [c["name"] for c in found["failing"]] == ["B"]
    assert found["shrunk"] == {"people": (1000, 850)}
    # First run: nothing to compare, nothing called new.
    assert compare_runs(checks, None, {"people": 850})["new"] == {}


def test_upkeep_fetches_only_new_data_and_checks_after_an_update(monkeypatch):
    import data.form3_appointments as form3
    import data.people_quality as quality
    import data.people_service as people
    import data.proxy_pay as proxy
    from utils import cache_manager

    calls = []
    monkeypatch.setattr(people, "refresh_ticker_aliases", lambda *a, **k: calls.append("aliases"))
    monkeypatch.setattr(proxy, "refresh_proxy_pay", lambda: calls.append("proxies") or 3)   # 3 new proxies

    def rebuild():
        calls.append("rebuild")
    rebuild.clear = lambda: calls.append("clear")
    monkeypatch.setattr(people, "get_people_universe", rebuild)
    monkeypatch.setattr(form3, "refresh_form3", lambda **k: calls.append("form3"))
    monkeypatch.setattr(quality, "check_people_quality", lambda: calls.append("check"))

    did = cache_manager.people_upkeep(run_form3=False, run_check=False)
    assert did == {"proxies": 3, "form3": False, "checked": True}
    assert calls == ["aliases", "proxies", "clear", "rebuild", "check"]

    # Nothing new and no check due: no rebuild, no check.
    calls.clear()
    monkeypatch.setattr(proxy, "refresh_proxy_pay", lambda: calls.append("proxies") or 0)
    did = cache_manager.people_upkeep(run_form3=False, run_check=False)
    assert did == {"proxies": 0, "form3": False, "checked": False}
    assert calls == ["aliases", "proxies"]


def test_one_story_under_several_companies_stays_with_the_one_making_the_cut():
    from data.layoff_events import subject_company
    names = {"ACLX": "Arcellx, Inc.", "GILD": "Gilead Sciences, Inc.", "KO": "The Coca-Cola Company",
             "WMT": "Walmart Inc.", "NVDA": "NVIDIA Corporation", "PANW": "Palo Alto Networks, Inc."}
    assert subject_company("After Arcellx buyout close, Gilead trims 108 jobs at CAR-T biotech's Redwood City "
                           "outpost", ["ACLX", "GILD"], names) == "GILD"
    assert subject_company("Layoffs in food industry mount as PepsiCo joins Coca-Cola, General Mills, Walmart "
                           "and others", ["KO", "WMT"], names) is None          # a roundup
    assert subject_company("While some tech giants cut jobs, Palo Alto Networks and Nvidia are hiring hundreds "
                           "in Israel", ["NVDA", "PANW"], names) is None        # about hiring


def test_proxy_refresh_stops_at_once_when_nothing_new(monkeypatch, tmp_path):
    import data.proxy_pay as proxy
    monkeypatch.setattr(proxy, "_signal_path", lambda: str(tmp_path / "signal.txt"))
    monkeypatch.setattr(proxy, "_proxy_signal", lambda: "proxy_pay_v3:10:474507:4442")
    (tmp_path / "signal.txt").write_text("proxy_pay_v3:10:474507:4442")
    listed = []
    monkeypatch.setattr(proxy, "_wanted_proxies", lambda: listed.append(1))   # the 40 s query
    assert proxy.refresh_proxy_pay() == 0 and listed == []
