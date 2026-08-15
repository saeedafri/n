"""
Constants and Mappings
======================
Centralized location for all constant mappings used across the application.
"""
from typing import Optional

from utils.server_logger import log_structured_error

# =============================================================================
# FEATURE FLAGS
# =============================================================================

# Quarterly revenue forecasting was paused at the Data team's request (2026-07-13)
# and re-enabled on 2026-08-15 alongside the revised quarterly model.
# When False, every quarterly-forecasting surface falls back to its Annual-only
# behavior: the /forecasting Quarterly toggle, the /market_data Forecasting-tab
# Quarterly view, and the twice-daily background quarterly refresh are all hidden
# or skipped. Annual forecasting is unaffected and does not read this flag.
# Flip to False to pause quarterly again — one-line revert.
QUARTERLY_FORECASTING_ENABLED = True

# =============================================================================
# YAHOO FINANCE TICKER SUFFIXES
# =============================================================================

# `coreiq_companies.exchange_acronym` is contractually a Yahoo Finance ticker
# SUFFIX ('DE' → ADS.DE), never an exchange name. When the data load writes an
# exchange name instead ('NYSE', 'NasdaqGS', 'TSX'), every consumer that builds
# `ticker + '.' + acronym` invents a symbol no table and no vendor knows, and the
# company silently disappears from every financial view.
#
# This set is the single source of truth for "is this acronym a real suffix".
# Verified on STG 2026-07-30: every suffix below resolves to real rows in
# coreiq_yf_financials_income_statement, and every rejected value resolves to
# zero (NYSE 0, NASDAQ 0, NasdaqGS 0, ADX 0).
YAHOO_SUFFIXES = frozenset({
    "AS", "AX", "BE", "BR", "CO", "DE", "F", "HE", "HK", "HM", "IR", "IS",
    "KS", "KQ", "L", "LS", "MC", "MI", "MU", "NS", "NZ", "OL", "PA", "SA",
    "SG", "SI", "ST", "SW", "T", "TO", "TW", "V", "VI", "WA",
})


def yahoo_symbol(ticker: str, exchange_acronym: Optional[str]) -> str:
    """Composite Yahoo symbol, or the plain ticker when the acronym is not a suffix.

    Never infer "foreign listing" from a dot in a ticker — the dot only exists
    because this function put it there. Route on `coreiq_companies.source`.

        yahoo_symbol('ADS',  'DE')   -> 'ADS.DE'    (real suffix)
        yahoo_symbol('SHOP', 'TO')   -> 'SHOP.TO'   (real suffix)
        yahoo_symbol('LSPD', 'TSX')  -> 'LSPD'      (exchange name, rejected)
        yahoo_symbol('CRTO', '')     -> 'CRTO'
        yahoo_symbol('ORCL', None)   -> 'ORCL'
    """
    ticker = (ticker or "").strip()
    acronym = (exchange_acronym or "").strip()
    if acronym and acronym in YAHOO_SUFFIXES:
        return f"{ticker}.{acronym}"
    return ticker


def is_yahoo_suffix(exchange_acronym: Optional[str]) -> bool:
    """True when the acronym is a genuine Yahoo suffix and a composite is valid."""
    return (exchange_acronym or "").strip() in YAHOO_SUFFIXES


# =============================================================================
# CURRENCY MAPPINGS
# =============================================================================

CURRENCY_NAMES = {
    "USD": "US Dollar",
    "EUR": "Euro",
    "GBP": "British Pound",
    "JPY": "Japanese Yen",
    "CNY": "Chinese Yuan",
    "HKD": "Hong Kong Dollar",
    "AUD": "Australian Dollar",
    "CAD": "Canadian Dollar",
    "CHF": "Swiss Franc",
    "SEK": "Swedish Krona",
    "NZD": "New Zealand Dollar",
    "SGD": "Singapore Dollar",
    "KRW": "South Korean Won",
    "INR": "Indian Rupee",
    "BRL": "Brazilian Real",
    "ZAR": "South African Rand",
    "MXN": "Mexican Peso",
    "RUB": "Russian Ruble",
    "AED": "UAE Dirham",
    "SAR": "Saudi Riyal",
    "TRY": "Turkish Lira",
    "THB": "Thai Baht",
    "MYR": "Malaysian Ringgit",
    "IDR": "Indonesian Rupiah",
    "PHP": "Philippine Peso",
    "VND": "Vietnamese Dong",
    "TWD": "Taiwan Dollar",
    "PLN": "Polish Zloty",
    "NOK": "Norwegian Krone",
    "DKK": "Danish Krone",
    "HUF": "Hungarian Forint",
    "CZK": "Czech Koruna",
    "ILS": "Israeli Shekel",
    "CLP": "Chilean Peso",
    "COP": "Colombian Peso",
    "ARS": "Argentine Peso",
    "PEN": "Peruvian Sol",
    "EGP": "Egyptian Pound",
    "NGN": "Nigerian Naira",
    "KES": "Kenyan Shilling",
    "PKR": "Pakistani Rupee",
    "BDT": "Bangladeshi Taka",
    "QAR": "Qatari Riyal",
    "KWD": "Kuwaiti Dinar",
    "BHD": "Bahraini Dinar",
    "OMR": "Omani Rial",
    "JOD": "Jordanian Dinar",
    "LBP": "Lebanese Pound",
    "ISK": "Icelandic Krona",
    "HRK": "Croatian Kuna",
    "RON": "Romanian Leu",
    "BGN": "Bulgarian Lev",
}

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "HKD": "$",
    "AUD": "$",
    "CAD": "$",
    "CHF": "Fr",
    "SEK": "kr",
    "NZD": "$",
    "SGD": "$",
    "KRW": "₩",
    "INR": "₹",
    "BRL": "R$",
    "ZAR": "R",
    "MXN": "$",
    "RUB": "₽",
    "AED": "د.إ",
    "SAR": "﷼",
    "TRY": "₺",
    "THB": "฿",
    "MYR": "RM",
    "IDR": "Rp",
    "PHP": "₱",
    "VND": "₫",
    "TWD": "NT$",
    "PLN": "zł",
    "NOK": "kr",
    "DKK": "kr",
    "HUF": "Ft",
    "CZK": "Kč",
    "ILS": "₪",
    "CLP": "$",
    "COP": "$",
    "ARS": "$",
    "PEN": "S/",
    "EGP": "£",
    "NGN": "₦",
    "KES": "KSh",
    "PKR": "₨",
    "BDT": "৳",
    "QAR": "﷼",
    "KWD": "د.ك",
    "BHD": "د.ب",
    "OMR": "﷼",
    "JOD": "د.ا",
    "LBP": "ل.ل",
    "ISK": "kr",
    "HRK": "kn",
    "RON": "lei",
    "BGN": "лв",
}


# =============================================================================
# EXCHANGE MAPPINGS
# =============================================================================

EXCHANGE_NAMES = {
    "NYSE": "New York Stock Exchange",
    "NASDAQ": "Nasdaq Stock Market",
    "HKSE": "Hong Kong Stock Exchange",
    "LSE": "London Stock Exchange",
    "TSE": "Tokyo Stock Exchange",
    "SSE": "Shanghai Stock Exchange",
    "SZSE": "Shenzhen Stock Exchange",
    "EURONEXT": "Euronext",
    "XETRA": "Xetra",
    "FRA": "Frankfurt Stock Exchange",
    "PAR": "Paris Stock Exchange",
    "AMS": "Amsterdam Stock Exchange",
    "SWX": "SIX Swiss Exchange",
    "BSE": "Bombay Stock Exchange",
    "NSE": "National Stock Exchange of India",
    "ASX": "Australian Securities Exchange",
    "TSX": "Toronto Stock Exchange",
    "KRX": "Korea Exchange",
    "SGX": "Singapore Exchange",
    "B3": "B3 (Brazil Stock Exchange)",
    "JSE": "Johannesburg Stock Exchange",
    "MOEX": "Moscow Exchange",
    "TADAWUL": "Saudi Stock Exchange",
    "DFM": "Dubai Financial Market",
    "ADX": "Abu Dhabi Securities Exchange",
    "QSE": "Qatar Stock Exchange",
    "BAHRAIN": "Bahrain Stock Exchange",
    "KWSE": "Kuwait Stock Exchange",
    "MSM": "Muscat Securities Market",
    "ASE": "Amman Stock Exchange",
    "BEY": "Beirut Stock Exchange",
    "EGX": "Egyptian Exchange",
    "NSENG": "Nigerian Stock Exchange",
    "GSE": "Ghana Stock Exchange",
}


# =============================================================================
# COUNTRY MAPPINGS
# =============================================================================

COUNTRY_NAMES = {
    "USA": "United States",
    "US": "United States",
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "HK": "Hong Kong",
    "CN": "China",
    "JP": "Japan",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "NL": "Netherlands",
    "CH": "Switzerland",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "BE": "Belgium",
    "AT": "Austria",
    "PT": "Portugal",
    "IE": "Ireland",
    "LU": "Luxembourg",
    "AU": "Australia",
    "CA": "Canada",
    "BR": "Brazil",
    "MX": "Mexico",
    "AR": "Argentina",
    "CL": "Chile",
    "CO": "Colombia",
    "PE": "Peru",
    "ZA": "South Africa",
    "EG": "Egypt",
    "NG": "Nigeria",
    "KE": "Kenya",
    "GH": "Ghana",
    "IN": "India",
    "ID": "Indonesia",
    "MY": "Malaysia",
    "PH": "Philippines",
    "SG": "Singapore",
    "TH": "Thailand",
    "VN": "Vietnam",
    "KR": "South Korea",
    "TW": "Taiwan",
    "AE": "United Arab Emirates",
    "SA": "Saudi Arabia",
    "QA": "Qatar",
    "KW": "Kuwait",
    "BH": "Bahrain",
    "OM": "Oman",
    "JO": "Jordan",
    "LB": "Lebanon",
    "IL": "Israel",
    "TR": "Turkey",
    "RU": "Russia",
    "PL": "Poland",
    "CZ": "Czech Republic",
    "HU": "Hungary",
    "RO": "Romania",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "GR": "Greece",
    "IS": "Iceland",
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_currency_symbol(currency_code: Optional[str]) -> str:
    """Get currency symbol for a currency code."""
    try:
        if not currency_code:
            return ""
        code = currency_code.upper().strip()
        return CURRENCY_SYMBOLS.get(code, "")
    except Exception as exc:
        log_structured_error(exc, page="constants", component="constants", operation="get_currency_symbol")
        return ""


def format_currency_full(currency_code: Optional[str]) -> str:
    """Format currency as 'Full Name (CODE) (Symbol)' e.g., 'Hong Kong Dollar (HKD) ($)'."""
    try:
        if not currency_code:
            return "N/A"
        code = currency_code.upper().strip()
        name = CURRENCY_NAMES.get(code, code)
        symbol = CURRENCY_SYMBOLS.get(code, "")
        if symbol:
            return f"{name} ({code}) ({symbol})"
        return f"{name} ({code})"
    except Exception as exc:
        log_structured_error(exc, page="constants", component="constants", operation="format_currency_full")
        return "N/A"


# =============================================================================
# EXCHANGE CODE MAPPING (Full Name / Variations -> Short Code)
# =============================================================================
# This maps various exchange name formats from different data sources to standardized codes

EXCHANGE_CODE_MAP = {
    # United States
    "NEW YORK STOCK EXCHANGE": "NYSE",
    "NYSE": "NYSE",
    "NY": "NYSE",
    "NEW YORK": "NYSE",
    "NASDAQ": "NASDAQ",
    "NASDAQ STOCK MARKET": "NASDAQ",
    "NASDAQ GS": "NASDAQ",
    "NASDAQGM": "NASDAQ",
    "NASDAQ GLOBAL MARKET": "NASDAQ",
    "NASDAQ GLOBAL SELECT": "NASDAQ",
    "NASDAQ CM": "NASDAQ",
    "NASDAQ CAPITAL MARKET": "NASDAQ",
    "OTC": "OTC",
    "OVER THE COUNTER": "OTC",
    "OTC MARKETS": "OTC",
    "OTCQX": "OTC",
    "OTCQB": "OTC",
    "PINK": "OTC",
    "BATS": "BATS",
    "BATS EXCHANGE": "BATS",
    "CBOE": "CBOE",
    "IEX": "IEX",

    # Canada
    "TORONTO STOCK EXCHANGE": "TSX",
    "TORONTO": "TSX",
    "TSX": "TSX",
    "TO": "TSX",
    "TSE": "TSX",
    "TSX VENTURE": "TSXV",
    "TSXV": "TSXV",
    "VENTURE": "TSXV",
    "CSE": "CSE",
    "CANADIAN SECURITIES EXCHANGE": "CSE",
    "NEO": "NEO",
    "NEO EXCHANGE": "NEO",

    # United Kingdom
    "LONDON STOCK EXCHANGE": "LSE",
    "LONDON": "LSE",
    "LSE": "LSE",
    "LON": "LSE",
    "UK": "LSE",
    "AIM": "AIM",
    "AIM LONDON": "AIM",
    "ALTERNATIVE INVESTMENT MARKET": "AIM",

    # Europe - Germany
    "XETRA": "XETRA",
    "XETRA GERMANY": "XETRA",
    "XTRA": "XETRA",
    "DE": "XETRA",
    "GER": "XETRA",
    "GERMANY": "XETRA",
    "FRANKFURT STOCK EXCHANGE": "FRA",
    "FRANKFURT": "FRA",
    "FRA": "FRA",
    "STUTTGART": "STU",
    "STU": "STU",
    "MUNICH": "MUN",
    "MUN": "MUN",
    "BERLIN": "BER",
    "BER": "BER",
    "HANNOVER": "HAN",
    "HAN": "HAN",
    "HAMBURG": "HAM",
    "HAM": "HAM",
    "DUSSELDORF": "DUS",
    "DUS": "DUS",

    # Europe - France
    "EURONEXT": "EURONEXT",
    "EURONEXT PARIS": "EURONEXT",
    "PARIS STOCK EXCHANGE": "PAR",
    "PARIS": "EURONEXT",
    "PAR": "EURONEXT",
    "FR": "EURONEXT",

    # Europe - Netherlands
    "EURONEXT AMSTERDAM": "AMS",
    "AMSTERDAM STOCK EXCHANGE": "AMS",
    "AMSTERDAM": "AMS",
    "AMS": "AMS",
    "NL": "AMS",

    # Europe - Belgium
    "EURONEXT BRUSSELS": "EBR",
    "BRUSSELS STOCK EXCHANGE": "EBR",
    "BRUSSELS": "EBR",
    "EBR": "EBR",

    # Europe - Portugal
    "EURONEXT LISBON": "ELI",
    "LISBON STOCK EXCHANGE": "ELI",
    "LISBON": "ELI",
    "ELI": "ELI",

    # Europe - Switzerland
    "SIX SWISS EXCHANGE": "SWX",
    "SIX": "SWX",
    "SWISS": "SWX",
    "SWX": "SWX",
    "SWISS EXCHANGE": "SWX",
    "CH": "SWX",
    "BERNE": "SWX",

    # Europe - Italy
    "Borsa Italiana": "MIL",
    "ITALIAN EXCHANGE": "MIL",
    "ITALIAN": "MIL",
    "MILAN STOCK EXCHANGE": "MIL",
    "MILAN": "MIL",
    "MIL": "MIL",
    "IT": "MIL",

    # Europe - Spain
    "BOLSA DE MADRID": "BME",
    "MADRID STOCK EXCHANGE": "BME",
    "MADRID": "BME",
    "BME": "BME",
    "SPANISH EXCHANGE": "BME",
    "ES": "BME",

    # Europe - Nordic
    "NASDAQ OMX": "OMX",
    "OMX": "OMX",
    "NORDIC": "OMX",
    "NASDAQ OMX NORDIC": "OMX",

    # Sweden
    "NASDAQ STOCKHOLM": "STO",
    "STOCKHOLM STOCK EXCHANGE": "STO",
    "STOCKHOLM": "STO",
    "STO": "STO",
    "SE": "STO",
    "OMXS": "STO",

    # Norway
    "OSLO BORS": "OSL",
    "OSLO STOCK EXCHANGE": "OSL",
    "OSLO": "OSL",
    "OSL": "OSL",
    "NO": "OSL",
    "OBX": "OSL",

    # Denmark
    "NASDAQ COPENHAGEN": "CPH",
    "COPENHAGEN STOCK EXCHANGE": "CPH",
    "COPENHAGEN": "CPH",
    "CPH": "CPH",
    "DK": "CPH",

    # Finland
    "NASDAQ HELSINKI": "HEL",
    "HELSINKI STOCK EXCHANGE": "HEL",
    "HELSINKI": "HEL",
    "HEL": "HEL",
    "FI": "HEL",

    # Iceland
    "NASDAQ ICELAND": "ICE",
    "ICELAND STOCK EXCHANGE": "ICE",
    "ICELAND": "ICE",
    "ICE": "ICE",
    "IC": "ICE",

    # Ireland
    "EURONEXT DUBLIN": "DUB",
    "IRISH STOCK EXCHANGE": "DUB",
    "DUBLIN STOCK EXCHANGE": "DUB",
    "DUBLIN": "DUB",
    "DUB": "DUB",
    "IE": "DUB",
    "ISE": "DUB",

    # Austria
    "WIENER BORSE": "VIE",
    "VIENNA STOCK EXCHANGE": "VIE",
    "VIENNA": "VIE",
    "VIE": "VIE",
    "AT": "VIE",
    "WBAG": "VIE",

    # Poland
    "WARSAW STOCK EXCHANGE": "WSE",
    "WARSAW": "WSE",
    "WSE": "WSE",
    "WAR": "WSE",
    "GPW": "WSE",
    "PL": "WSE",

    # Czech Republic
    "PRAGUE STOCK EXCHANGE": "PRA",
    "PRAGUE": "PRA",
    "PRA": "PRA",
    "CZ": "PRA",
    "PSE": "PRA",

    # Hungary
    "BUDAPEST STOCK EXCHANGE": "BUD",
    "BUDAPEST": "BUD",
    "BUD": "BUD",
    "HU": "BUD",
    "BET": "BUD",

    # Russia
    "MOSCOW EXCHANGE": "MOEX",
    "MOSCOW": "MOEX",
    "MOEX": "MOEX",
    "MICEX": "MOEX",
    "RU": "MOEX",
    "ME": "MOEX",

    # Turkey
    "BORSA ISTANBUL": "BIST",
    "ISTANBUL STOCK EXCHANGE": "BIST",
    "ISTANBUL": "BIST",
    "BIST": "BIST",
    "TR": "BIST",
    "ISE": "BIST",

    # Greece
    "ATHENS EXCHANGE": "ATHEX",
    "ATHENS STOCK EXCHANGE": "ATHEX",
    "ATHENS": "ATHEX",
    "ATHEX": "ATHEX",
    "GR": "ATHEX",
    "ASE": "ATHEX",

    # Asia - Hong Kong
    "HONG KONG STOCK EXCHANGE": "HKEX",
    "HONG KONG": "HKEX",
    "HKEX": "HKEX",
    "HK": "HKEX",
    "HKSE": "HKEX",
    "HKG": "HKEX",

    # Asia - China
    "SHANGHAI STOCK EXCHANGE": "SSE",
    "SHANGHAI": "SSE",
    "SSE": "SSE",
    "SHH": "SSE",
    "CN": "SSE",
    "SH": "SSE",
    "SHG": "SSE",

    "SHENZHEN STOCK EXCHANGE": "SZSE",
    "SHENZHEN": "SZSE",
    "SZSE": "SZSE",
    "SHZ": "SZSE",
    "SZ": "SZSE",

    "BEIJING STOCK EXCHANGE": "BSE",
    "BEIJING": "BSE",
    "CHINA": "SSE",

    # Asia - Japan
    "TOKYO STOCK EXCHANGE": "TSE",
    "TOKYO": "TSE",
    "TSE": "TSE",
    "TYO": "TSE",
    "JP": "TSE",
    "JAPAN": "TSE",
    "JPX": "TSE",
    "NIKKEI": "TSE",
    "JASDAQ": "JASDAQ",
    "MOTHERS": "MOTHERS",

    # Asia - South Korea
    "KOREA EXCHANGE": "KRX",
    "KOREA": "KRX",
    "KRX": "KRX",
    "KSE": "KRX",
    # Direct keys MUST exist for these — otherwise the partial-match fallback
    # below matches 'KS' inside 'HKSE' and mislabels Korean stocks as Hong Kong
    # (HKEX). 'KS' is yfinance's Korea suffix (e.g. 005930.KS); 'KSC' is Samsung's
    # info.exchange value.
    "KS": "KRX",
    "KSC": "KRX",
    "KOSPI": "KRX",
    "KOSDAQ": "KOSDAQ",
    "KR": "KRX",
    "KOREAN EXCHANGE": "KRX",

    # Asia - Taiwan
    "TAIWAN STOCK EXCHANGE": "TWSE",
    "TAIWAN": "TWSE",
    "TWSE": "TWSE",
    "TAIPEI": "TWSE",
    "TW": "TWSE",
    "TWO": "TWSE",
    "TPEx": "TPEx",

    # Asia - Singapore
    "SINGAPORE EXCHANGE": "SGX",
    "SINGAPORE": "SGX",
    "SGX": "SGX",
    "SG": "SGX",
    "SP": "SGX",
    "SES": "SGX",

    # Asia - Malaysia
    "BURSA MALAYSIA": "MYX",
    "MALAYSIA": "MYX",
    "MALAYSIAN EXCHANGE": "MYX",
    "MYX": "MYX",
    "KLSE": "MYX",
    "KUALA LUMPUR": "MYX",
    "MY": "MYX",

    # Asia - Thailand
    "STOCK EXCHANGE OF THAILAND": "SET",
    "THAILAND": "SET",
    "THAI": "SET",
    "SET": "SET",
    "TH": "SET",
    "BKK": "SET",
    "BANGKOK": "SET",

    # Asia - Indonesia
    "INDONESIA STOCK EXCHANGE": "IDX",
    "INDONESIA": "IDX",
    "INDONESIAN EXCHANGE": "IDX",
    "IDX": "IDX",
    "ID": "IDX",
    "JAKARTA": "IDX",
    "JSX": "IDX",

    # Asia - Philippines
    "PHILIPPINE STOCK EXCHANGE": "PSE",
    "PHILIPPINES": "PSE",
    "PHILIPPINE": "PSE",
    "PSE": "PSE",
    "PH": "PSE",
    "MANILA": "PSE",

    # Asia - Vietnam
    "HO CHI MINH STOCK EXCHANGE": "HOSE",
    "VIETNAM": "HOSE",
    "VIETNAMESE": "HOSE",
    "HOSE": "HOSE",
    "VN": "HOSE",
    "HSX": "HOSE",
    "HNX": "HNX",
    "HANOI": "HNX",

    # Asia - India
    "BOMBAY STOCK EXCHANGE": "BSE",
    "BSE": "BSE",
    "BOM": "BSE",
    "MUMBAI": "BSE",
    "BOMBAY": "BSE",
    "IN": "NSE",
    "INDIA": "NSE",
    "NATIONAL STOCK EXCHANGE": "NSE",
    "NSE": "NSE",
    "NSI": "NSE",
    "NSE INDIA": "NSE",

    # Australia & New Zealand
    "AUSTRALIAN SECURITIES EXCHANGE": "ASX",
    "AUSTRALIA": "ASX",
    "AUSTRALIAN": "ASX",
    "ASX": "ASX",
    "AU": "ASX",
    "SYDNEY": "ASX",

    "NEW ZEALAND EXCHANGE": "NZX",
    "NEW ZEALAND": "NZX",
    "NZX": "NZX",
    "NZ": "NZX",
    "WELLINGTON": "NZX",

    # Middle East - Israel
    "TEL AVIV STOCK EXCHANGE": "TASE",
    "TEL AVIV": "TASE",
    "TASE": "TASE",
    "ISRAEL": "TASE",
    "ISRAELI": "TASE",
    "IL": "TASE",
    "TA": "TASE",

    # Middle East - UAE
    "DUBAI FINANCIAL MARKET": "DFM",
    "DUBAI": "DFM",
    "DFM": "DFM",
    "ABU DHABI SECURITIES EXCHANGE": "ADX",
    "ABU DHABI": "ADX",
    "ADX": "ADX",
    "UAE": "DFM",
    "AE": "DFM",
    "NASDAQ DUBAI": "DIFX",
    "DIFX": "DIFX",

    # Middle East - Saudi Arabia
    "SAUDI STOCK EXCHANGE": "TADAWUL",
    "SAUDI EXCHANGE": "TADAWUL",
    "TADAWUL": "TADAWUL",
    "SAUDI": "TADAWUL",
    "SAUDI ARABIA": "TADAWUL",
    "SA": "TADAWUL",
    "SAU": "TADAWUL",

    # Middle East - Qatar
    "QATAR STOCK EXCHANGE": "QSE",
    "QATAR": "QSE",
    "QSE": "QSE",
    "QA": "QSE",
    "DOHA": "QSE",

    # Middle East - Kuwait
    "KUWAIT STOCK EXCHANGE": "BKP",
    "KUWAIT": "BKP",
    "BKP": "BKP",
    "KW": "BKP",
    "KSE": "BKP",
    "BOURSA KUWAIT": "BKP",

    # Middle East - Bahrain
    "BAHRAIN STOCK EXCHANGE": "BSE",
    "BAHRAIN": "BSE",
    "BH": "BSE",
    "BAHRAIN BOURSE": "BSE",

    # Middle East - Oman
    "MUSCAT SECURITIES MARKET": "MSM",
    "MUSCAT": "MSM",
    "MSM": "MSM",
    "OMAN": "MSM",
    "OM": "MSM",

    # Middle East - Jordan
    "AMMAN STOCK EXCHANGE": "ASE",
    "AMMAN": "ASE",
    "JORDAN": "ASE",
    "ASE": "ASE",
    "JO": "ASE",

    # Middle East - Lebanon
    "BEIRUT STOCK EXCHANGE": "BSE",
    "BEIRUT": "BSE",
    "LEBANON": "BSE",
    "LB": "BSE",

    # Middle East - Egypt
    "EGYPTIAN EXCHANGE": "EGX",
    "EGYPT": "EGX",
    "EGX": "EGX",
    "EGYPTIAN": "EGX",
    "CAIRO": "EGX",
    "CAIRO ALEXANDRIA": "EGX",
    "EG": "EGX",
    "CASE": "EGX",

    # Africa - South Africa
    "JOHANNESBURG STOCK EXCHANGE": "JSE",
    "JOHANNESBURG": "JSE",
    "JSE": "JSE",
    "JSE LIMITED": "JSE",
    "SOUTH AFRICA": "JSE",
    "ZA": "JSE",
    "ZAR": "JSE",

    # Africa - Nigeria
    "NIGERIAN EXCHANGE GROUP": "NGX",
    "NIGERIAN STOCK EXCHANGE": "NGX",
    "NIGERIA": "NGX",
    "NGX": "NGX",
    "NSE": "NGX",
    "NG": "NGX",
    "LAGOS": "NGX",

    # Africa - Kenya
    "NAIROBI SECURITIES EXCHANGE": "NSE",
    "NAIROBI": "NSE",
    "KENYA": "NSE",
    "KE": "NSE",
    "NSE KENYA": "NSE",

    # Africa - Ghana
    "GHANA STOCK EXCHANGE": "GSE",
    "GHANA": "GSE",
    "GSE": "GSE",
    "GH": "GSE",
    "ACCRA": "GSE",

    # Africa - Morocco
    "CASABLANCA STOCK EXCHANGE": "CSE",
    "CASABLANCA": "CSE",
    "MOROCCO": "CSE",
    "CSE": "CSE",
    "MA": "CSE",

    # Africa - Tunisia
    "TUNIS STOCK EXCHANGE": "BVMT",
    "TUNIS": "BVMT",
    "TUNISIA": "BVMT",
    "BVMT": "BVMT",
    "TN": "BVMT",

    # Latin America - Brazil
    "B3": "B3",
    "BRAZIL": "B3",
    "BRAZILIAN": "B3",
    "BRAZIL STOCK EXCHANGE": "B3",
    "BOVESPA": "B3",
    "SAO PAULO": "B3",
    "SAO PAULO STOCK EXCHANGE": "B3",
    "BR": "B3",
    "BVMF": "B3",

    # Latin America - Mexico
    "MEXICAN STOCK EXCHANGE": "BMV",
    "MEXICO": "BMV",
    "MEXICAN": "BMV",
    "BMV": "BMV",
    "MEXBOL": "BMV",
    "MX": "BMV",
    "MEX": "BMV",
    "BOLSA MEXICANA": "BMV",

    # Latin America - Argentina
    "BUENOS AIRES STOCK EXCHANGE": "BCBA",
    "BUENOS AIRES": "BCBA",
    "ARGENTINA": "BCBA",
    "BCBA": "BCBA",
    "AR": "BCBA",
    "MERVAL": "BCBA",

    # Latin America - Chile
    "SANTIAGO STOCK EXCHANGE": "BCS",
    "SANTIAGO": "BCS",
    "CHILE": "BCS",
    "BCS": "BCS",
    "CL": "BCS",
    "IPSA": "BCS",
    "BOLSA DE COMERCIO": "BCS",

    # Latin America - Colombia
    "COLOMBIA STOCK EXCHANGE": "BVC",
    "COLOMBIA": "BVC",
    "COLOMBIAN": "BVC",
    "BVC": "BVC",
    "BOGOTA": "BVC",
    "CO": "BVC",
    "COL": "BVC",

    # Latin America - Peru
    "LIMA STOCK EXCHANGE": "BVL",
    "LIMA": "BVL",
    "PERU": "BVL",
    "BVL": "BVL",
    "PE": "BVL",
    "BOLSA DE VALORES DE LIMA": "BVL",

    # Latin America - Venezuela (limited access)
    "CARACAS STOCK EXCHANGE": "BVC",
    "CARACAS": "BVC",
    "VENEZUELA": "BVC",
    "VE": "BVC",

    # Cryptocurrency Exchanges (for reference)
    "COINBASE": "COINBASE",
    "BINANCE": "BINANCE",
    "KRAKEN": "KRAKEN",
    "BITSTAMP": "BITSTAMP",
    "BITFINEX": "BITFINEX",
    "GEMINI": "GEMINI",
}


def get_exchange_code(exchange_name: Optional[str]) -> str:
    """
    Normalize exchange name to standardized short code.

    Args:
        exchange_name: Raw exchange name from data source (e.g., "Toronto Stock Exchange", "NYSE", "Toronto")

    Returns:
        Standardized exchange code (e.g., "TSX", "NYSE", "NASDAQ")
        Returns original name (uppercase) if no mapping found
        Returns empty string if input is None/empty

    Examples:
        >>> get_exchange_code("Toronto Stock Exchange")
        'TSX'
        >>> get_exchange_code("NYSE")
        'NYSE'
        >>> get_exchange_code("New York Stock Exchange")
        'NYSE'
        >>> get_exchange_code("toronto")  # case insensitive
        'TSX'
    """
    try:
        if not exchange_name:
            return ""

        # Normalize: uppercase, strip whitespace
        normalized = exchange_name.upper().strip()

        # Direct lookup
        code = EXCHANGE_CODE_MAP.get(normalized)
        if code:
            return code

        # Try partial matching for cases like "NYSE American" -> "NYSE"
        for key, value in EXCHANGE_CODE_MAP.items():
            if normalized in key or key in normalized:
                return value

        # Fallback: return original uppercase
        return normalized
    except Exception as exc:
        log_structured_error(exc, page="constants", component="constants", operation="get_exchange_code")
        return ""


def format_exchange(exchange_code: Optional[str]) -> str:
    """Format exchange as 'Full Name (Short Code)' e.g., 'New York Stock Exchange (NYSE)'."""
    try:
        if not exchange_code:
            return "N/A"
        code = exchange_code.upper().strip()
        name = EXCHANGE_NAMES.get(code, code)
        return f"{name} ({code})"
    except Exception as exc:
        log_structured_error(exc, page="constants", component="constants", operation="format_exchange")
        return "N/A"


def format_country(country_code: Optional[str]) -> str:
    """Format country as 'Full Name (Short Code)' e.g., 'United States (USA)'."""
    try:
        if not country_code:
            return "N/A"
        code = country_code.upper().strip()
        name = COUNTRY_NAMES.get(code, code)
        return f"{name} ({code})"
    except Exception as exc:
        log_structured_error(exc, page="constants", component="constants", operation="format_country")
        return "N/A"


# =============================================================================
# NEWS TOPIC / CATEGORY LABELS  (from coreiq_av_market_news_sentiment.topics_json)
# =============================================================================
# AV topic labels — fixed set defined by Alpha Vantage API (never changes).
AV_TOPIC_LABELS = {
    "earnings":            "Earnings",
    "technology":          "Technology",
    "financial_markets":   "Financial Markets",
    "retail_wholesale":    "Retail & Wholesale",
    "economy_fiscal":      "Economy - Fiscal",
    "economy_monetary":    "Economy - Monetary",
    "economy_macro":       "Economy - Macro",
    "energy_transportation": "Energy & Transportation",
    "finance":             "Finance",
    "life_sciences":       "Life Sciences",
    "manufacturing":       "Manufacturing",
    "real_estate":         "Real Estate & Construction",
    "ipo":                 "IPO",
    "blockchain":          "Blockchain",
    "mergers_and_acquisitions": "Mergers & Acquisitions",
}

# NEWS_TOPIC_LABELS starts with AV topics + uncategorized.
# YF dynamic topics are merged in at runtime via merge_yf_topics().
NEWS_TOPIC_LABELS = {
    **AV_TOPIC_LABELS,
    "uncategorized":       "Uncategorized",
}


def merge_yf_topics(yf_topic_keys: list) -> None:
    """Merge dynamically fetched YF topic keys into NEWS_TOPIC_LABELS.

    Called once at app startup after the parallel DB fetch completes.
    New keys get a display label via title-casing with underscore→space.
    Existing AV keys are never overwritten.
    """
    for key in yf_topic_keys:
        if key and key not in NEWS_TOPIC_LABELS and key != 'uncategorized':
            NEWS_TOPIC_LABELS[key] = key.replace("_", " ").title()


def get_topic_display_label(topic_key: Optional[str]) -> str:
    """Return the human-readable label for a raw topic key."""
    try:
        if not topic_key:
            return ""
        return NEWS_TOPIC_LABELS.get(topic_key, topic_key.replace("_", " ").title())
    except Exception as exc:
        log_structured_error(exc, page="constants", component="constants", operation="get_topic_display_label")
        return ""


# =============================================================================
# SEGMENT DATA — DIMENSION PATTERNS & METRIC DEFINITIONS
# =============================================================================

# --- Dimension axis patterns for DB queries (SQL LIKE) ---
# Business / Operating Segment axes
SEGMENT_BUSINESS_AXES = [
    "%StatementBusinessSegmentsAxis%",
    "%LegalEntityAxis%",
    "%StatementOperatingActivitiesSegmentAxis%",
    "%SubsegmentsAxis%",
    "%ReportingbyChannelAxis%",
    "%RevenueChannelAxis%",
    "%BrandsAxis%",
    "%SegmentInformationByServicesAxis%",
    "%ConsolidationItemsAxis%",
]

# Product / Service axes
SEGMENT_PRODUCT_AXES = [
    "%ProductOrServiceAxis%",
    "%ContractWithCustomerSalesChannelAxis%",
]

# Geographic axes
SEGMENT_GEO_AXES = [
    "%StatementGeographicalAxis%",
    "%GeographicDistributionAxis%",
    "%RegionReportingInformationByRegionAxis%",
    "%CountryAxis%",
    "%InvestmentGeographicRegionAxis%",
]

# Combined: ALL axes the SQL queries should search
SEGMENT_ALL_AXES = SEGMENT_BUSINESS_AXES + SEGMENT_PRODUCT_AXES + SEGMENT_GEO_AXES

# --- Dimension axis patterns for edgartools queries (short names) ---
EDGAR_BUSINESS_AXES = [
    "StatementBusinessSegmentsAxis",
    "ConsolidationItemsAxis",
    "StatementOperatingActivitiesSegmentAxis",
]

EDGAR_PRODUCT_AXES = [
    "ProductOrServiceAxis",
    "ContractWithCustomerSalesChannelAxis",
]

EDGAR_GEO_AXES = [
    "StatementGeographicalAxis",
    "GeographicDistributionAxis",
]

# --- Heading normalisation map ---
# Maps lower-cased heading prefixes (text before first ':' in full_dimension_label)
# to a clean display category.
SEGMENT_HEADING_MAP = {
    # Business / Operating
    "segments": "Business",
    "statement, business segments": "Business",
    "statement business segments": "Business",
    "business segments": "Business",
    "operating activities segment": "Business",
    "statement, operating activities segment": "Business",
    "consolidation items": "Business",
    "legal entity": "Business",
    "reporting by channel": "Business",
    "revenue channel": "Business",
    "brands": "Business",
    "segment information by services": "Business",
    "subsegments": "Business",
    # Product / Service
    "product and service": "Product and Service",
    "product or service": "Product and Service",
    "products and services": "Product and Service",
    "contract with customer, sales channel": "Product and Service",
    # Geographical
    "geographical": "Geographical",
    "statement geographical": "Geographical",
    "statement, geographical": "Geographical",
    "geographic distribution": "Geographical",
    "region reporting information by region": "Geographical",
    "country": "Geographical",
    "investment geographic region": "Geographical",
}

# --- Members to skip (consolidation/eliminations, not real operating segments) ---
SEGMENT_SKIP_MEMBERS = {
    'parent', 'parent company', 'guarantor subsidiaries',
    'non-guarantor subsidiaries', 'subsidiary issuer',
    'other subsidiaries', 'consolidated entities',
    'eliminations', 'consolidation eliminations',
    'consolidation, eliminations',
    # Aggregated "Total" members — skipped to avoid double-counting
    # since we compute our own Total row by summing the actual members.
    'total', 'total company', 'total consolidated', 'consolidated',
    'total segments', 'all segments', 'corporate and other',
    'corporate & other', 'corporate, other and eliminations',
    'unallocated corporate', 'unallocated', 'corporate',
    # Common XBRL aggregated members filed as segments
    'operating segments', 'reportable segments', 'reportable segment',
    'total reportable segments',
    'net sales',  # Net Sales as a member = consolidated total, not a segment
    'total net sales', 'total revenues', 'total revenue',
    'total sales', 'total sales - all categories',
    'combined', 'combined total', 'subtotal',
    'intersegment', 'intersegment eliminations', 'inter-segment',
    'other and corporate', 'other corporate',
    # ACI-style: "Retail segment sales" = sum of individual product segments
    'retail segment sales', 'retail segment',
    # Other common consolidated-entity labels
    'total company operations', 'total operations',
    # Pension / retirement-plan geographic breakdowns filed on StatementGeographicalAxis.
    # These are NOT geographic revenue (HON, MDLZ, DBD pension plan data leaks here).
    'u.s. plans', 'u.s. plan',
    'domestic plan', 'domestic plans',
    'u.s. defined benefit plan', 'u.s. defined benefit plans',
    'non-u.s. plans', 'non-u.s. plan', 'non-us plans', 'non-us plan',
    'canadian salaried and hourly plans',
    'u.k. plans', 'uk plans',
    'foreign plans', 'foreign plan',
    'pension plans', 'pension plan',
    'retirement plans', 'retirement plan',
    'defined benefit plans', 'defined benefit plan',
    'u.s. pension plans', 'u.s. pension plan',
    'international plans', 'international plan',
}

# --- Metric groups: CapIQ section name → matching rules ---
# Each metric: display_label, db_include (label keywords), db_exclude, edgar concepts/labels
SEGMENT_METRIC_GROUPS = {
    "Revenues": {
        "display": "Revenues",
        "db_include": ["revenue", "sales", "net sales"],
        "db_exclude": [
            "deferred", "cost of", "proceeds from", "reduction in",
            "redemption", "loyalty", "recognized", "aftertax",
            "installment", "gain on", "gain (loss)", "gain related",
            "contract with customer, liability", "percentage",
        ],
        "edgar_concepts": [
            "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet",
        ],
        "edgar_labels": ["revenue", "sales", "net sales"],
    },
    "Operating Profit Before Tax": {
        "display": "Operating Profit Before Tax",
        "db_include": ["operating income", "operating profit", "operating loss"],
        "db_exclude": ["non-operating", "other operating"],
        "edgar_concepts": [
            "OperatingIncomeLoss",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        ],
        "edgar_labels": ["operating income", "operating profit"],
    },
    "Assets": {
        "display": "Assets",
        "db_include": ["total assets", "assets"],
        "db_exclude": ["net assets", "total current assets", "intangible assets",
                       "deferred tax", "other assets", "operating lease"],
        "edgar_concepts": ["Assets"],
        "edgar_labels": ["total assets", "assets"],
    },
    "Depreciation & Amortization": {
        "display": "Depreciation & Amortization",
        "db_include": ["depreciation", "amortization"],
        "db_exclude": ["accumulated", "less:", "net of"],
        "edgar_concepts": [
            "DepreciationDepletionAndAmortization",
            "DepreciationAndAmortization",
            "DepreciationAmortizationAndAccretionNet",
        ],
        "edgar_labels": ["depreciation", "amortization"],
    },
    "Capital Expenditure": {
        "display": "Capital Expenditure",
        "db_include": ["capital expenditure", "property and equipment additions",
                       "purchases of property", "payments for capital"],
        "db_exclude": [],
        "edgar_concepts": [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "CapitalExpenditureDiscontinuedOperations",
        ],
        "edgar_labels": ["capital expenditure", "property and equipment additions",
                         "purchases of property"],
    },
}

# unit_ref patterns that are clearly percentages / ratios (skip these)
SEGMENT_PERCENT_UNIT_REFS = {'number', 'pure', 'ratio'}

# Non-USD currency keywords to skip in unit_ref
SEGMENT_NON_USD_CURRENCIES = ('eur', 'jpy', 'ars', 'vnd', 'aud', 'gbp', 'brl', 'krw', 'cny', 'inr')

# Non-monetary unit keywords to skip in unit_ref
SEGMENT_NON_MONETARY_UNITS = (
    'store', 'location', 'warehouse', 'brand', 'sqft',
    'clinic', 'province', 'platform', 'center', 'segment',
    'entity', 'employee', 'parcel', 'shipcenter', 'credit',
    'country', 'product', 'states', 'customer', 'share',
)
