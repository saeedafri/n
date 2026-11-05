import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def test_pence_quotes_become_pounds():
    from data.repository import quote_currency
    assert quote_currency("GBp") == ("GBP", 100.0)
    assert quote_currency("USD") == ("USD", 1.0)
    assert quote_currency(None) == (None, 1.0)


def test_cash_adds_short_term_investments_only_when_av_repeats_cash():
    from data.repository import balance_capitalization
    # AMZN 2026-09-30: cashAndShortTermInvestments repeats cash; leases are in the debt total.
    amzn = balance_capitalization({
        "cashAndCashEquivalentsAtCarryingValue": "55783", "cashAndShortTermInvestments": "55783",
        "shortTermInvestments": "58443", "shortLongTermDebtTotal": "271949",
        "totalShareholderEquity": "613601"})
    assert amzn["cash_st"] == 114226 and amzn["total_debt"] == 271949
    # ATD: the total already includes short-term investments, and there is no debt total.
    atd = balance_capitalization({
        "cashAndCashEquivalentsAtCarryingValue": 3198.3, "cashAndShortTermInvestments": 3242.9,
        "currentDebt": 0.7, "longTermDebtNoncurrent": 10328.6})
    assert atd["cash_st"] == 3242.9 and round(atd["total_debt"], 1) == 10329.3
    assert balance_capitalization({})["total_debt"] is None
