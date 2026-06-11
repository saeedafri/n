"""
Data models for Market Data and Newsroom pages.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, List, Dict, Any

try:
    from utils.server_logger import log_error, log_exception, log_structured_error
except ImportError:
    import logging as _logging
    log_error = _logging.error
    log_exception = _logging.error
    log_structured_error = lambda exc, **kw: _logging.error(str(exc))


@dataclass
class TickerSentiment:
    """Ticker sentiment data from news article."""
    ticker: str
    relevance_score: str
    ticker_sentiment_label: str
    ticker_sentiment_score: str
    display_name: str = ''  # optional override; used by YF to pass company_name directly


@dataclass
class NewsArticle:
    """News article model from coreiq_av_market_news_sentiment table."""
    id: int
    title: str
    summary: str
    url: str
    source: str
    source_domain: str
    time_published: datetime
    time_published_raw: str
    overall_sentiment_score: float
    overall_sentiment_label: str
    banner_image: Optional[str]
    ticker_sentiment: List[TickerSentiment]
    topics: List[Dict[str, str]]
    category_within_source: str
    # source_tag identifies the data origin: 'av' = Alpha Vantage, 'yf' = Yahoo Finance.
    # Default 'av' preserves backward compatibility with all existing AV instantiations.
    source_tag: str = 'av'

    @property
    def formatted_date(self) -> str:
        """Format date as 'January 21, 2026'."""
        try:
            return self.time_published.strftime("%B %d, %Y")
        except Exception as exc:
            log_structured_error(exc, page="models", component="NewsArticle.formatted_date",
                                 operation="format_date", context=f"time_published={self.time_published}")
            return ""

    @property
    def company_tickers(self) -> List[str]:
        """Get list of company tickers."""
        try:
            return [ts.ticker for ts in self.ticker_sentiment]
        except Exception as exc:
            log_structured_error(exc, page="models", component="NewsArticle.company_tickers",
                                 operation="get_tickers", context="ticker_sentiment iteration")
            return []


@dataclass
class Company:
    """Company model from coreiq_companies table."""
    ticker: str
    name: str
    name_coresight: Optional[str]
    exchange: Optional[str]

    @property
    def display_name(self) -> str:
        """Format: 'Macy's Inc. (NYSE:M)'"""
        try:
            name = self.name_coresight
            exchange = self.exchange or ""
            return f"{name} ({exchange}:{self.ticker})"
        except Exception as exc:
            log_structured_error(exc, page="models", component="Company.display_name",
                                 operation="format_display_name", context=f"ticker={self.ticker}")
            return self.ticker


@dataclass
class IncomeStatementLineItem:
    """Income statement line item for display."""
    label: str
    key: str  # Database column name
    values: List[Optional[float]]  # Values for each period
    is_calculated: bool = False


def get_calendar_quarter(month: int) -> int:
    """Get calendar quarter (CQ) from month (1-12).

    CQ1: Jan-Mar, CQ2: Apr-Jun, CQ3: Jul-Sep, CQ4: Oct-Dec
    """
    try:
        return (month - 1) // 3 + 1
    except Exception as exc:
        log_structured_error(exc, page="models", component="get_calendar_quarter",
                             operation="compute_cq", context=f"month={month}")
        return 1


def get_fiscal_quarter(month: int, fiscal_year_end_month: int) -> int:
    """Get fiscal quarter (FQ) from month and fiscal year end month.

    FQ4 ends at the fiscal year end month.
    Each quarter is 3 months.

    Args:
        month: Current month (1-12)
        fiscal_year_end_month: Month when fiscal year ends (1-12)

    Returns:
        Fiscal quarter number (1-4)
    """
    try:
        # FQ4 ends at fiscal_year_end_month
        # Work backwards to find which quarter the current month falls in
        # FQ3 ends 3 months before FQ4
        # FQ2 ends 3 months before FQ3
        # FQ1 ends 3 months before FQ2 (which is also 3 months after FQ4)

        # Calculate the month that ends FQ4
        fq4_end = fiscal_year_end_month
        fq3_end = ((fq4_end - 3 - 1) % 12) + 1  # 3 months before FQ4
        fq2_end = ((fq4_end - 6 - 1) % 12) + 1  # 6 months before FQ4
        fq1_end = ((fq4_end - 9 - 1) % 12) + 1  # 9 months before FQ4

        # Determine which quarter the current month falls into
        # Each quarter spans 3 months ending at fqX_end
        def is_in_quarter(m: int, quarter_end: int) -> bool:
            """Check if month m falls in the quarter ending at quarter_end."""
            try:
                # Quarter spans from (quarter_end - 2) to quarter_end (modulo 12)
                start = ((quarter_end - 3) % 12) + 1
                if start <= quarter_end:
                    # Normal case (e.g., quarter_end=5, start=3: months 3,4,5)
                    return start <= m <= quarter_end
                else:
                    # Wrapped case (e.g., quarter_end=2, start=12: months 12,1,2)
                    return m >= start or m <= quarter_end
            except Exception as exc:
                log_structured_error(exc, page="models", component="is_in_quarter",
                                     operation="check_quarter", context=f"m={m} quarter_end={quarter_end}")
                return False

        if is_in_quarter(month, fq4_end):
            return 4
        elif is_in_quarter(month, fq3_end):
            return 3
        elif is_in_quarter(month, fq2_end):
            return 2
        else:
            return 1
    except Exception as exc:
        log_structured_error(exc, page="models", component="get_fiscal_quarter",
                             operation="compute_fq",
                             context=f"month={month} fiscal_year_end_month={fiscal_year_end_month}")
        return 1


def parse_fiscal_year_end(fiscal_year_end: Optional[str]) -> Optional[int]:
    """Parse fiscal_year_end string to month number (1-12).

    Handles formats like:
    - "February" -> 2
    - "Feb" -> 2
    - "2" -> 2
    - "2024-02-01" -> 2 (extracts month from date)
    """
    try:
        if not fiscal_year_end:
            return None

        fiscal_year_end = fiscal_year_end.strip()

        # Try to parse as month name
        month_map = {
            'january': 1, 'jan': 1,
            'february': 2, 'feb': 2,
            'march': 3, 'mar': 3,
            'april': 4, 'apr': 4,
            'may': 5,
            'june': 6, 'jun': 6,
            'july': 7, 'jul': 7,
            'august': 8, 'aug': 8,
            'september': 9, 'sep': 9, 'sept': 9,
            'october': 10, 'oct': 10,
            'november': 11, 'nov': 11,
            'december': 12, 'dec': 12,
        }

        lower = fiscal_year_end.lower()
        if lower in month_map:
            return month_map[lower]

        # Try to parse as integer
        try:
            month_num = int(fiscal_year_end)
            if 1 <= month_num <= 12:
                return month_num
        except ValueError:
            pass

        # Try to parse as date (e.g., "2024-02-01" or "02/01/2024")
        import re
        # ISO date format: 2024-02-01
        iso_match = re.match(r'\d{4}-(\d{2})-\d{2}', fiscal_year_end)
        if iso_match:
            return int(iso_match.group(1))

        # US date format: 02/01/2024 or 02-01-2024
        us_match = re.match(r'(\d{2})[/-]\d{2}[/-]\d{4}', fiscal_year_end)
        if us_match:
            return int(us_match.group(1))

        return None
    except Exception as exc:
        log_structured_error(exc, page="models", component="parse_fiscal_year_end",
                             operation="parse_fy_end", context=f"fiscal_year_end={fiscal_year_end}")
        return None


@dataclass
class FiscalPeriod:
    """Fiscal period for column headers."""
    date: date
    label: str
    is_estimated: bool = False  # True for AV analyst-estimate columns (E-)
    is_forecast: bool = False   # True for model-generated forecast columns (F-)
    calendar_quarter: Optional[int] = None  # CQ1-CQ4
    fiscal_quarter: Optional[int] = None  # FQ1-FQ4

    @classmethod
    def from_date(cls, dt: date, period_type: str = "annual", fiscal_year_end: Optional[str] = None) -> "FiscalPeriod":
        """Create from date.

        For annual: '12 Months\nJan-29-2021'
        For quarterly: '3 months CQ1/FQ1\nSep-30-2024' (with fiscal_year_end)
                      '3 months CQ1\nSep-30-2024' (without fiscal_year_end)
        """
        try:
            import time as _time
            _start = _time.perf_counter()

            if period_type.lower() == "quarterly":
                # Calculate calendar quarter (CQ)
                cq = get_calendar_quarter(dt.month)

                # Calculate fiscal quarter (FQ) if fiscal_year_end is provided
                fy_end_month = parse_fiscal_year_end(fiscal_year_end)
                if fy_end_month:
                    fq = get_fiscal_quarter(dt.month, fy_end_month)
                    label = f"3 months FQ{fq}/CQ{cq}\n{dt.strftime('%b-%d-%Y')}"
                else:
                    fq = None
                    label = f"3 months CQ{cq}\n{dt.strftime('%b-%d-%Y')}"

                _elapsed = (_time.perf_counter() - _start) * 1000
                if _elapsed > 0.1:  # Only log if slow (should be instant)
                    print(f"[FQ_CALC_SLOW] date={dt} fy_end={fiscal_year_end} took {_elapsed:.3f}ms")

                return cls(
                    date=dt,
                    label=label,
                    calendar_quarter=cq,
                    fiscal_quarter=fq
                )
            else:
                # Annual default
                return cls(
                    date=dt,
                    label=f"12 Months\n{dt.strftime('%b-%d-%Y')}"
                )
        except Exception as exc:
            log_structured_error(exc, page="models", component="FiscalPeriod.from_date",
                                 operation="create_period",
                                 context=f"dt={dt} period_type={period_type} fiscal_year_end={fiscal_year_end}")
            # Return a safe fallback instead of None so callers can still access .date
            return cls(date=dt, label=dt.strftime('%b-%d-%Y'))


@dataclass
class IncomeStatementData:
    """Complete income statement data for a company."""
    company: Company
    periods: List[FiscalPeriod]
    line_items: List[IncomeStatementLineItem]


@dataclass
class CompanyOverview:
    """Company overview data from coreiq_av_company_overview table."""
    ticker: str
    name: str
    exchange: Optional[str]
    currency: Optional[str]
    country: Optional[str]
    sector: Optional[str]
    industry: Optional[str]
    primary_industry_coresight: Optional[str]  # From coreiq_companies table
    company_description: Optional[str]
    official_site: Optional[str]
    fiscal_year_end: Optional[str]
    cik: Optional[str]
    market_capitalization: Optional[int]
    pe_ratio: Optional[float]
    eps: Optional[float]
    dividend_yield: Optional[float]
    analyst_target_price: Optional[float]
    fetched_at_utc: datetime

    # Fields from raw_json
    address: Optional[str] = None
    revenue_ttm: Optional[float] = None
    ebitda: Optional[float] = None
    profit_margin: Optional[float] = None
    shares_outstanding: Optional[int] = None
    week_52_high: Optional[float] = None
    week_52_low: Optional[float] = None
    dividend_per_share: Optional[float] = None
    latest_quarter: Optional[str] = None

    # Placeholder fields (not in database, will show N/A)
    employees: Optional[str] = None
    year_founded: Optional[str] = None
    professionals_profiled: Optional[str] = None
    coverage_summary: Optional[str] = None
    coverage_list: Optional[str] = None
    relationships: Optional[str] = None
    projects: Optional[str] = None
    activity_logs: Optional[str] = None

    @property
    def display_name(self) -> str:
        """Format: 'Macy's Inc. (NYSE:M)'"""
        try:
            exchange = self.exchange or ""
            return f"{self.name} ({exchange}:{self.ticker})"
        except Exception as exc:
            log_structured_error(exc, page="models", component="CompanyOverview.display_name",
                                 operation="format_display_name", context=f"ticker={self.ticker}")
            return self.ticker

    @property
    def formatted_market_cap(self) -> str:
        """Format market cap in billions/millions."""
        try:
            if not self.market_capitalization:
                return "N/A"
            if self.market_capitalization >= 1_000_000_000:
                return f"${self.market_capitalization / 1_000_000_000:.2f}B"
            elif self.market_capitalization >= 1_000_000:
                return f"${self.market_capitalization / 1_000_000:.2f}M"
            else:
                return f"${self.market_capitalization:,}"
        except Exception as exc:
            log_structured_error(exc, page="models", component="CompanyOverview.formatted_market_cap",
                                 operation="format_market_cap",
                                 context=f"market_capitalization={self.market_capitalization}")
            return "N/A"

    @property
    def website_display(self) -> str:
        """Clean website URL for display - matches wireframe format (macys.com)."""
        try:
            if not self.official_site:
                return "N/A"
            # Remove https://, http://, www., and trailing slashes
            cleaned = self.official_site.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")
            return cleaned
        except Exception as exc:
            log_structured_error(exc, page="models", component="CompanyOverview.website_display",
                                 operation="format_website", context=f"official_site={self.official_site}")
            return "N/A"


@dataclass
class BalanceSheetLineItem:
    """Balance sheet line item for display."""
    label: str
    key: str  # Database column name
    values: List[Optional[float]]  # Values for each period
    is_calculated: bool = False
    section: str = ""  # 'assets', 'liabilities', 'equity'


@dataclass
class BalanceSheetData:
    """Complete balance sheet data for a company."""
    company: Company
    periods: List[FiscalPeriod]
    line_items: List[BalanceSheetLineItem]


@dataclass
class CashFlowLineItem:
    """Cash flow statement line item for display."""
    label: str
    key: str  # Database column name or raw_json key
    values: List[Optional[float]]  # Values for each period
    is_calculated: bool = False
    section: str = ""  # 'operating', 'investing', 'financing'


@dataclass
class CashFlowData:
    """Complete cash flow statement data for a company."""
    company: Company
    periods: List[FiscalPeriod]
    line_items: List[CashFlowLineItem]


@dataclass
class EarningsCall:
    """Earnings call transcript model from coreiq_av_earnings_call_transcripts table."""
    id: int
    source: Optional[str]
    ticker: str
    quarter: str  # e.g., "2025Q4"
    year: int
    q: int  # Quarter number 1-4
    transcript_text: Optional[str]
    has_transcript: bool
    title: Optional[str]
    event_datetime_utc: Optional[datetime]
    fetched_at_utc: datetime

    @property
    def display_title(self) -> str:
        """Format: 'Amazon (AMZN) Earnings Call 2025 - Q1'"""
        try:
            return f"{self.ticker} Earnings Call {self.year} - Q{self.q}"
        except Exception as exc:
            log_structured_error(exc, page="models", component="EarningsCall.display_title",
                                 operation="format_display_title", context=f"ticker={self.ticker}")
            return self.ticker

    @property
    def formatted_quarter(self) -> str:
        """Format: 'Q1 2025'"""
        try:
            return f"Q{self.q} {self.year}"
        except Exception as exc:
            log_structured_error(exc, page="models", component="EarningsCall.formatted_quarter",
                                 operation="format_quarter",
                                 context=f"q={self.q} year={self.year}")
            return ""

    @property
    def formatted_date(self) -> str:
        """Format event date or return N/A."""
        try:
            if self.event_datetime_utc:
                return self.event_datetime_utc.strftime("%B %d, %Y")
            return "N/A"
        except Exception as exc:
            log_structured_error(exc, page="models", component="EarningsCall.formatted_date",
                                 operation="format_date",
                                 context=f"event_datetime_utc={self.event_datetime_utc}")
            return "N/A"

    @property
    def speaker_sections(self) -> List[Dict[str, str]]:
        """Parse transcript into speaker sections.

        Returns list of dicts with 'speaker' and 'text' keys.
        Speaker names are identified by pattern: "Name:" at start of line.
        """
        try:
            if not self.transcript_text:
                return []

            sections = []
            current_speaker = "Operator"
            current_text = []

            for line in self.transcript_text.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue

                # Check if line starts with a speaker name (Pattern: "Name:" or "Name :")
                if ':' in line:
                    potential_speaker = line.split(':')[0].strip()
                    # If the rest of the line after colon is not empty, it's likely a speaker
                    remaining_text = line[len(potential_speaker) + 1:].strip()

                    # Common speaker patterns
                    if potential_speaker and (
                        potential_speaker.isupper() or  # OPERATOR, DAVE FILDES
                        potential_speaker.istitle() or  # Operator, Dave Fildes
                        ' ' in potential_speaker or     # Full names
                        potential_speaker.lower() in ['operator', 'ceo', 'cfo']
                    ):
                        # Save previous section
                        if current_text:
                            sections.append({
                                'speaker': current_speaker,
                                'text': ' '.join(current_text)
                            })

                        current_speaker = potential_speaker
                        if remaining_text:
                            current_text = [remaining_text]
                        else:
                            current_text = []
                    else:
                        current_text.append(line)
                else:
                    current_text.append(line)

            # Don't forget the last section
            if current_text:
                sections.append({
                    'speaker': current_speaker,
                    'text': ' '.join(current_text)
                })

            return sections
        except Exception as exc:
            log_structured_error(exc, page="models", component="EarningsCall.speaker_sections",
                                 operation="parse_transcript",
                                 context=f"ticker={self.ticker} quarter={self.quarter}")
            return []


@dataclass
class FilingMetricResult:
    """A filing metric from the filing_metrics table."""
    original_label: str
    numeric_value: Optional[float]
    unit_ref: Optional[str]
    report_fiscal_year: int
    is_dimensioned: bool
    dimension_label: Optional[str]
    statement_type: Optional[str]
    ixbrl_id: Optional[str]
    standard_concept: Optional[str] = None
    concept: Optional[str] = None
    balance: Optional[str] = None
    period_type: Optional[str] = None
    period_start: Optional[str] = None    # "2023-01-29"  (duration start)
    period_end: Optional[str] = None      # "2024-02-03"  (duration end)
    period_instant: Optional[str] = None  # "2024-02-03"  (instant snapshot)
    value: Optional[str] = None  # raw text value (e.g. "P1Y" duration, text)
    source: Optional[str] = None  # 'xbrl' | 'calculated' | 'llm' | 'edgartools'
    calculation_note: Optional[str] = None  # raw formula string for calculated metrics
    dimension: Optional[str] = None  # XBRL axis (e.g. 'srt:StatementGeographicalAxis')
    llm_query: Optional[str] = None  # JSON detail for store_count/credit_rating sources
    full_dimension_label: Optional[str] = None  # e.g. "Product and Service: Technology, Segments: Sams Club"

    @property
    def formatted_value(self) -> str:
        """Format numeric value: USD -> $ X.XXXB / $ X,XXXM, others as-is.
        Falls back to raw value string if numeric_value is None.
        """
        try:
            if self.numeric_value is None:
                return self._format_raw_value()
            val = abs(self.numeric_value)  # bracket notation (123) stored as negative — always display positive
            if self.unit_ref and self.unit_ref.lower() == "usd":
                if abs(val) >= 1e12:
                    return f"$ {val / 1e12:,.3f} T"
                elif abs(val) >= 1e9:
                    return f"$ {val / 1e9:,.3f} B"
                elif abs(val) >= 1e6:
                    return f"$ {val / 1e6:,.0f} M"
                elif abs(val) >= 1e3:
                    return f"$ {val / 1e3:,.0f} K"
                else:
                    return f"$ {val:,.2f}"
            # Percentage: label or concept contains "percent" → decimal × 100
            label_lower = self.original_label.lower()
            concept_lower = (self.standard_concept or "").lower()
            if "percent" in label_lower or "percent" in concept_lower or "rate" in label_lower:
                pct = val * 100 if abs(val) <= 1.0 else val  # already in % if > 1
                return f"{pct:,.2f}%"
            # Plain number
            if val == int(val):
                return f"{int(val):,}"
            return f"{val:,.4f}"
        except Exception as exc:
            log_structured_error(exc, page="models", component="FilingMetricResult.formatted_value",
                                 operation="format_value",
                                 context=f"original_label={self.original_label}")
            return "N/A"

    def _format_raw_value(self) -> str:
        """Format the raw text value when numeric_value is absent."""
        try:
            raw = getattr(self, 'value', None)
            if not raw:
                return "N/A"
            # ISO 8601 duration: P1Y → "1 Year", P2Y → "2 Years", P1M → "1 Month", etc.
            import re
            m = re.fullmatch(r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?', raw.strip())
            if m:
                years, months, days = m.group(1), m.group(2), m.group(3)
                parts = []
                if years:
                    parts.append(f"{years} {'Year' if years == '1' else 'Years'}")
                if months:
                    parts.append(f"{months} {'Month' if months == '1' else 'Months'}")
                if days:
                    parts.append(f"{days} {'Day' if days == '1' else 'Days'}")
                return " ".join(parts) if parts else raw
            return raw
        except Exception as exc:
            log_structured_error(exc, page="models", component="FilingMetricResult._format_raw_value",
                                 operation="format_raw_value",
                                 context=f"original_label={self.original_label}")
            return "N/A"

    @property
    def display_label(self) -> str:
        """Label only — dimension info shown separately on the card."""
        try:
            return self.original_label
        except Exception as exc:
            log_structured_error(exc, page="models", component="FilingMetricResult.display_label",
                                 operation="get_display_label", context="label access")
            return ""

    @property
    def display_statement_type(self) -> str:
        """Human-readable statement type, inferring segment category from dimension axis."""
        try:
            d = self.dimension or ""
            if "StatementGeographicalAxis" in d:
                return "Geographic Segments"
            if "StatementBusinessSegmentsAxis" in d:
                return "Business Segments"
            if "ProductOrServiceAxis" in d:
                return "Product Segments"
            if "ConsolidationItemsAxis" in d and self.is_dimensioned:
                return "Business Segments"
            return self.statement_type or "Financial Metric"
        except Exception as exc:
            log_structured_error(exc, page="models", component="FilingMetricResult.display_statement_type",
                                 operation="get_statement_type",
                                 context=f"original_label={self.original_label}")
            return "Financial Metric"
