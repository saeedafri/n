import inspect
import os
import sys


sys.path.insert(0, os.path.join(os.getcwd(), "app"))

from data import repository  # noqa: E402
from data.models import Company  # noqa: E402
from data.repository import (  # noqa: E402
    CompanyOverviewRepository,
    CompanyRepository,
    ExecutiveCompensationRepository,
)
from utils import ticker_utils  # noqa: E402


SEC_SQSP = {
    "ticker": "SQSP",
    "name": "Squarespace, Inc.",
    "name_coresight": "Squarespace",
    "exchange": "New York Stock Exchange",
    "source": "SEC",
    "exchange_acronym": "NYSE",
    "primary_industry_coresight": "Technology",
}

# Yahoo's Euronext Paris suffix is ".PA" — VusionGroup is VU.PA. ("EPA:" is
# Google Finance notation and is NOT a Yahoo suffix; see the rejection test
# below, which pins that distinction.)
YF_VUSION = {
    "ticker": "VU",
    "name": "VusionGroup S.A.",
    "name_coresight": "VusionGroup",
    "exchange": "Euronext Paris",
    "source": "YFinance",
    "exchange_acronym": "PA",
    "primary_industry_coresight": "Technology",
}

# Same company as the source data actually holds it on STG: an exchange label
# where a Yahoo suffix belongs. No composite may be minted from this.
YF_VUSION_BAD_ACRONYM = {**YF_VUSION, "exchange_acronym": "EPA"}


def _uncached(function):
    return inspect.unwrap(function)


def test_company_map_only_builds_exchange_composites_for_yfinance(monkeypatch):
    monkeypatch.setattr(
        CompanyRepository,
        "get_companies_rows",
        staticmethod(lambda: [SEC_SQSP, YF_VUSION]),
    )

    companies = _uncached(CompanyRepository.get_companies_map)()

    assert set(companies) == {"SQSP", "VU", "VU.PA"}
    assert companies["SQSP"]["ticker"] == "SQSP"
    assert companies["VU.PA"]["ticker"] == "VU.PA"


def test_company_dropdown_keeps_sec_base_and_yfinance_composite(monkeypatch):
    companies_map = {
        "SQSP": SEC_SQSP,
        "VU": YF_VUSION,
        "VU.PA": {**YF_VUSION, "ticker": "VU.PA"},
    }
    monkeypatch.setattr(
        CompanyRepository,
        "get_companies_map",
        staticmethod(lambda: companies_map),
    )

    companies = _uncached(CompanyRepository.get_companies)()

    assert companies == [
        {"ticker": "SQSP", "name": "Squarespace"},
        {"ticker": "VU.PA", "name": "VusionGroup"},
    ]


def test_exchange_name_acronym_never_mints_a_composite(monkeypatch):
    """An exchange label in `exchange_acronym` must not become a ticker.

    `coreiq_companies.exchange_acronym` is contractually a Yahoo suffix, but some
    rows hold an exchange name instead ("EPA", "NYSE", "TSX", "NasdaqGS").
    Appending one invents a symbol no vendor and no table knows (VU.EPA), which
    hides the company from every financial view. The plain ticker must survive.
    """
    monkeypatch.setattr(
        CompanyRepository,
        "get_companies_rows",
        staticmethod(lambda: [YF_VUSION_BAD_ACRONYM]),
    )

    companies = _uncached(CompanyRepository.get_companies_map)()

    assert set(companies) == {"VU"}
    assert "VU.EPA" not in companies
    assert companies["VU"]["ticker"] == "VU"


def test_legacy_sec_composite_resolves_to_canonical_base_ticker(monkeypatch):
    monkeypatch.setattr(
        CompanyRepository,
        "get_companies_map",
        staticmethod(lambda: {"SQSP": SEC_SQSP}),
    )

    company = _uncached(CompanyRepository.get_company_by_ticker)("SQSP.NYSE")

    assert company is not None
    assert company.ticker == "SQSP"
    assert company.name_coresight == "Squarespace"


def test_ticker_validation_canonicalizes_legacy_sec_composite(monkeypatch):
    monkeypatch.setattr(
        ticker_utils.CompanyRepository,
        "get_company_by_ticker",
        staticmethod(
            lambda ticker: Company(
                ticker="SQSP",
                name="Squarespace, Inc.",
                name_coresight="Squarespace",
                exchange="New York Stock Exchange",
            )
        ),
    )

    ticker, used_fallback = ticker_utils.validate_and_get_ticker(
        url_ticker="SQSP.NYSE",
        session_ticker=None,
        page_name="market_data",
    )

    assert ticker == "SQSP"
    assert used_fallback is False


def test_sec_composite_overview_query_uses_base_ticker(monkeypatch):
    captured = {}

    def execute_query_readonly(_query, params):
        captured.update(params)
        return []

    monkeypatch.setattr(
        "data.source_router.get_company_source",
        lambda _ticker: "SEC",
    )
    monkeypatch.setattr(
        repository.db_manager,
        "execute_query_readonly",
        execute_query_readonly,
    )
    monkeypatch.setattr(
        CompanyRepository,
        "get_companies_map",
        staticmethod(lambda: {"SQSP": SEC_SQSP}),
    )

    result = _uncached(CompanyOverviewRepository.get_company_overview)("SQSP.NYSE")

    assert result is None
    assert captured == {"ticker": "SQSP"}


def test_real_dotted_sec_ticker_is_not_stripped(monkeypatch):
    captured = {}
    imkt = {
        **SEC_SQSP,
        "ticker": "IMKT.A",
        "name": "Ingles Markets, Incorporated",
        "name_coresight": "Ingles Markets",
        "exchange_acronym": None,
    }

    def execute_query_readonly(_query, params):
        captured.update(params)
        return []

    monkeypatch.setattr(
        "data.source_router.get_company_source",
        lambda _ticker: "SEC",
    )
    monkeypatch.setattr(
        repository.db_manager,
        "execute_query_readonly",
        execute_query_readonly,
    )
    monkeypatch.setattr(
        CompanyRepository,
        "get_companies_map",
        staticmethod(lambda: {"IMKT.A": imkt}),
    )

    result = _uncached(CompanyOverviewRepository.get_company_overview)("IMKT.A")

    assert result is None
    assert captured == {"ticker": "IMKT.A"}


def test_valid_master_company_is_returned_when_detailed_overview_is_missing(monkeypatch):
    monkeypatch.setattr(
        CompanyOverviewRepository,
        "get_company_overview",
        staticmethod(lambda _ticker: None),
    )
    monkeypatch.setattr(
        CompanyRepository,
        "get_companies_map",
        staticmethod(lambda: {"SQSP": SEC_SQSP}),
    )

    company = CompanyOverviewRepository.get_company_overview_or_master("SQSP")

    assert company is not None
    assert company.ticker == "SQSP"
    assert company.name == "Squarespace"
    assert company.exchange == "New York Stock Exchange"
    assert company.primary_industry_coresight == "Technology"
    assert company.company_description is None


def test_yfinance_compensation_query_uses_base_ticker(monkeypatch):
    captured = {}

    def execute_query_readonly(_query, params):
        captured.update(params)
        return []

    monkeypatch.setattr(
        repository.db_manager,
        "execute_query_readonly",
        execute_query_readonly,
    )

    result = _uncached(
        ExecutiveCompensationRepository.get_yf_latest_compensation
    )("VU.EPA")

    assert result == []
    assert captured == {"ticker": "VU"}
