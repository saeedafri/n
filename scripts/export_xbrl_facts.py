#!/usr/bin/env python3
"""
XBRL Facts Export - Generalized Version
Supports multiple companies and years with robust error handling

Usage:
    python export_xbrl_facts.py

Configuration:
    Edit the CONFIG section below to set tickers, years, and other options
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

import pandas as pd

# ── MUST run BEFORE edgar import: redirect httpxthrottlecache away from macOS-protected ~/.edgar/_tcache ──
try:
    import httpxthrottlecache.filecache.transport as _tc
    _orig_fc_init = _tc.FileCache.__init__
    def _patched_fc_init(self, cache_dir=None, **kwargs):
        import tempfile, os
        safe_dir = os.path.join(tempfile.gettempdir(), 'edgar_tcache')
        os.makedirs(safe_dir, exist_ok=True)
        _orig_fc_init(self, cache_dir=safe_dir, **kwargs)
    _tc.FileCache.__init__ = _patched_fc_init
except Exception:
    pass

from edgar import Company, set_identity

# ========== CONFIGURATION ==========
@dataclass
class Config:
    """Configuration for the export process"""
    # Companies to process
    TICKERS: List[str] = field(default_factory=lambda: ["AAPL", "AMZN", "M"])

    # Years to process
    YEARS: List[int] = field(default_factory=lambda: [2022, 2023, 2024, 2025])

    # Filing form type
    FORM: str = "10-K"

    # Output directory
    OUTPUT_BASE_DIR: str = "data/filings"

    # Processing options
    SKIP_EXISTING: bool = False      # Skip if output already exists
    DRY_RUN: bool = False            # Don't save files, just log
    LOG_LEVEL: str = "INFO"          # DEBUG, INFO, WARNING, ERROR

    # Identity for SEC
    SEC_IDENTITY: str = "Mohd Saeed Afri mohdsaeedafri@coresight.com"

# ========== STANDARD CONCEPT MAPPING ==========
# This can be extended for more companies
STANDARD_CONCEPT_MAP = {
    # ===== INCOME STATEMENT =====
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": "Revenue",
    "us-gaap:Revenues": "Revenue",
    "us-gaap:SalesRevenueNet": "Revenue",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax": "Revenue",
    "aapl:NetSales": "Revenue",
    "us-gaap:CostOfGoodsAndServicesSold": "Cost of Goods Sold",
    "us-gaap:CostOfRevenue": "Cost of Revenue",
    "us-gaap:CostOfGoodsSold": "Cost of Goods Sold",
    "us-gaap:GrossProfit": "Gross Profit",
    "us-gaap:GrossProfitLoss": "Gross Profit",
    "us-gaap:OperatingIncomeLoss": "Operating Income",
    "us-gaap:NetIncomeLoss": "Net Income",
    "us-gaap:ProfitLoss": "Net Income",

    # ===== BALANCE SHEET =====
    "us-gaap:Assets": "Total Assets",
    "us-gaap:AssetsCurrent": "Current Assets",
    "us-gaap:Liabilities": "Total Liabilities",
    "us-gaap:LiabilitiesCurrent": "Current Liabilities",
    "us-gaap:StockholdersEquity": "Stockholders Equity",

    # ===== CASH FLOW =====
    "us-gaap:NetCashProvidedByUsedInOperatingActivities": "Operating Cash Flow",
    "us-gaap:NetCashProvidedByUsedInInvestingActivities": "Investing Cash Flow",
    "us-gaap:NetCashProvidedByUsedInFinancingActivities": "Financing Cash Flow",

    # ===== DEI =====
    "dei:DocumentFiscalYearFocus": "Fiscal Year",
    "dei:EntityCentralIndexKey": "CIK",
    "dei:TradingSymbol": "Trading Symbol",
    "dei:EntityRegistrantName": "Company Name",
}

# ========== UTILITY FUNCTIONS ==========

def log(message: str, level: str = "INFO"):
    """Log a message with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")

def ensure_dir(path: str) -> Path:
    """Ensure directory exists"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p

# ========== STEP 1: FETCH DATA ==========

def download_filing(ticker: str, year: int, form_type: str) -> Optional[Any]:
    """Download filing for a company/year. For 10-K returns the latest; for 10-Q use download_all_filings."""
    try:
        company = Company(ticker)
        filings = company.get_filings(form=form_type, year=year, amendments=False)

        if len(filings) == 0:
            log(f"No {form_type} filing found for {ticker} {year}", "WARNING")
            return None

        return filings.latest()
    except Exception as e:
        log(f"Error downloading filing for {ticker} {year}: {e}", "ERROR")
        return None


def download_all_filings(ticker: str, year: int, form_type: str) -> List[Any]:
    """Download ALL filings for a company/year (needed for 10-Q which has up to 3 per year)."""
    try:
        company = Company(ticker)
        filings = company.get_filings(form=form_type, year=year, amendments=False)

        if len(filings) == 0:
            log(f"No {form_type} filings found for {ticker} {year}", "WARNING")
            return []

        result = list(filings)
        log(f"Found {len(result)} {form_type} filing(s) for {ticker} {year}")
        return result
    except Exception as e:
        log(f"Error downloading filings for {ticker} {year}: {e}", "ERROR")
        return []


def determine_quarter(filing) -> str:
    """
    Determine the fiscal quarter (Q1/Q2/Q3) from a 10-Q filing using EdgarTools.

    Uses EdgarTools' official XBRL entity_info['fiscal_period'] attribute.
    This is the RECOMMENDED way to get fiscal period in EdgarTools.

    Reference: https://edgartools.readthedocs.io/en/latest/xbrl/facts/#access-xbrl-entity-and-filing-metadata

    Returns:
        str: 'Q1', 'Q2', or 'Q3'

    Raises:
        ValueError: If fiscal period cannot be determined from XBRL
    """
    # OFFICIAL EDGARTOOLS METHOD: Use entity_info['fiscal_period']
    # This reads the DEI:DocumentFiscalPeriodFocus tag internally
    try:
        xbrl = filing.xbrl()
        fiscal_period = xbrl.entity_info.get('fiscal_period') or xbrl.entity_info.get('document_fiscal_period_focus')

        if fiscal_period:
            quarter = str(fiscal_period).strip().upper()
            if quarter in ('Q1', 'Q2', 'Q3'):
                log(f"  Quarter from EdgarTools entity_info['fiscal_period']: {quarter}")
                return quarter
    except Exception as e:
        log(f"  Could not read fiscal_period from entity_info: {e}", "WARNING")

    # If official method fails, raise error (NO FALLBACK to filing date)
    # This prevents silent data corruption
    filing_info = f"{filing.company} {filing.form} filed {filing.filing_date}" if hasattr(filing, 'company') else str(filing)
    error_msg = f"Could not determine quarter for {filing_info}. EdgarTools XBRL fiscal_period not available and filing date fallback failed. Manual review required."
    log(f"  ERROR: {error_msg}", "ERROR")
    raise ValueError(error_msg)

def extract_ixbrl_locations(html_content: str) -> Dict[Tuple[str, str], Dict]:
    """
    Extract all ixbrl tag locations from HTML content.
    Handles nested ixbrl tags and self-closing tags.
    Returns: {(concept, context): {ixbrl_id, start_pos, end_pos}}
    """
    locations = {}

    def extract_attrs(tag_content: str) -> Dict[str, str]:
        """Extract attributes from tag content"""
        attrs = {}
        patterns = [
            (r'contextRef="([^"]*)"', 'contextRef'),
            (r'name="([^"]*)"', 'name'),
            (r'id="([^"]*)"', 'id'),
        ]
        for pattern, key in patterns:
            m = re.search(pattern, tag_content, re.IGNORECASE)
            if m:
                attrs[key] = m.group(1)
        return attrs

    # Find all ixbrl tags (opening and self-closing)
    tag_pattern = r'<ix:(nonNumeric|nonFraction)\s+([^>]*?)(/?>)'

    for match in re.finditer(tag_pattern, html_content, re.IGNORECASE | re.DOTALL):
        tag_type = match.group(1)
        tag_content = match.group(2)
        is_self_closing = match.group(3) == '/>'
        start_pos = match.start()

        attrs = extract_attrs(tag_content)
        if 'name' not in attrs or 'id' not in attrs:
            continue

        concept = attrs['name'].lower()
        context_ref = attrs.get('contextRef', '').lower()

        if not context_ref:
            continue

        if is_self_closing:
            end_pos = match.end()
            key = (concept, context_ref)
            locations[key] = {
                'ixbrl_id': attrs['id'],
                'start_position': start_pos,
                'end_position': end_pos
            }
        else:
            # Handle nested tags
            search_start = match.end()
            depth = 1
            pos = search_start

            while depth > 0 and pos < len(html_content):
                next_open = html_content.find(f'<ix:{tag_type}', pos)
                next_close = html_content.find(f'</ix:{tag_type}>', pos)

                if next_close == -1:
                    break

                if next_open != -1 and next_open < next_close:
                    depth += 1
                    pos = next_open + len(f'<ix:{tag_type}')
                else:
                    depth -= 1
                    if depth == 0:
                        end_pos = next_close + len(f'</ix:{tag_type}>')
                        break
                    pos = next_close + len(f'</ix:{tag_type}>')

            if depth == 0:
                key = (concept, context_ref)
                locations[key] = {
                    'ixbrl_id': attrs['id'],
                    'start_position': start_pos,
                    'end_position': end_pos
                }

    return locations

def fetch_xbrl_facts(filing) -> Optional[pd.DataFrame]:
    """Fetch XBRL facts from a filing"""
    try:
        facts = filing.xbrl().facts
        return facts.to_dataframe()
    except Exception as e:
        log(f"Error fetching XBRL facts: {e}", "ERROR")
        return None

def step_1_fetch_data(ticker: str, year: int, form_type: str) -> Optional[Tuple]:
    """
    Step 1: Fetch all required data from SEC
    Returns: (html_content, ixbrl_locations, facts_df, filing_date) or None
    """
    log(f"Step 1: Fetching data for {ticker} {year}")

    # Download filing
    filing = download_filing(ticker, year, form_type)
    if not filing:
        return None

    filing_date = filing.filing_date
    log(f"  Filing date: {filing_date}")

    # Download HTML
    try:
        html_content = filing.html()
        log(f"  Downloaded HTML: {len(html_content)} bytes")
    except Exception as e:
        log(f"  Error downloading HTML: {e}", "ERROR")
        return None

    # Extract ixbrl locations
    ixbrl_locations = extract_ixbrl_locations(html_content)
    log(f"  Extracted {len(ixbrl_locations)} ixbrl locations")

    # Fetch XBRL facts
    facts_df = fetch_xbrl_facts(filing)
    if facts_df is None:
        return None
    log(f"  Fetched {len(facts_df)} XBRL facts")

    return (html_content, ixbrl_locations, facts_df, filing_date)

# ========== STEP 2: FILTER & DEDUPLICATE ==========

def filter_by_fiscal_year(facts_list: List[Dict], fiscal_year: int) -> List[Dict]:
    """Filter facts by fiscal year"""
    filtered = []

    for fact in facts_list:
        period_type = fact.get('period_type')
        include = False

        if period_type == 'duration':
            period_end = fact.get('period_end')
            if period_end:
                date_str = period_end.split('T')[0] if 'T' in str(period_end) else str(period_end)
                try:
                    fact_year = datetime.strptime(date_str, '%Y-%m-%d').year
                    if fact_year == fiscal_year:
                        include = True
                except ValueError:
                    pass

        elif period_type == 'instant':
            period_instant = fact.get('period_instant')
            if period_instant:
                date_str = period_instant.split('T')[0] if 'T' in str(period_instant) else str(period_instant)
                try:
                    fact_year = datetime.strptime(date_str, '%Y-%m-%d').year
                    if fact_year == fiscal_year:
                        include = True
                except ValueError:
                    pass

        if include:
            filtered.append(fact)

    return filtered

def remove_duplicates(facts: List[Dict]) -> List[Dict]:
    """Remove duplicate facts (same fields except fact_key)"""
    seen = set()
    unique_facts = []

    for fact in facts:
        fact_copy = {k: v for k, v in fact.items() if k != 'fact_key'}
        hash_key = json.dumps(fact_copy, sort_keys=True, default=str)

        if hash_key not in seen:
            seen.add(hash_key)
            unique_facts.append(fact)

    return unique_facts

def step_2_filter_dedupe(facts_df: pd.DataFrame, year: int) -> Tuple[List[Dict], int]:
    """
    Step 2: Deduplicate facts (no date filter — keep all periods).

    Keeping all periods is intentional: a 10-K contains comparative data for
    3 income statement years and 2 balance sheet dates.  Filtering to a single
    year discards prior-period comparatives that the UI needs to show correct
    period labels (e.g. Jan '23 → Feb '24 inside a FY2025 filing).

    Returns: (unique_facts, fiscal_year_from_dei)
    """
    log("Step 2: Deduplicating (no date filter — all periods kept)")

    facts_list = json.loads(facts_df.to_json(orient="records", date_format='iso'))

    # Determine fiscal year from DEI tag (used for output folder naming only)
    fiscal_year = year
    fy_facts = facts_df[facts_df['concept'] == 'dei:DocumentFiscalYearFocus']
    if len(fy_facts) > 0:
        fiscal_year = int(fy_facts.iloc[0]['value'])
    log(f"  Fiscal year (from DEI): {fiscal_year}")
    log(f"  Total facts before dedup: {len(facts_list)}")

    # Remove duplicates
    unique_facts = remove_duplicates(facts_list)
    removed = len(facts_list) - len(unique_facts)
    log(f"  After dedup: {len(unique_facts)} facts (removed {removed} duplicates)")

    return unique_facts, fiscal_year

# ========== STEP 3: ENRICH ==========

def get_standard_concept(concept: str) -> str:
    """Get standard concept name for a company concept"""
    if concept in STANDARD_CONCEPT_MAP:
        return STANDARD_CONCEPT_MAP[concept]

    # Fallback: convert CamelCase to words
    concept_name = concept.split(':')[-1] if ':' in concept else concept
    return re.sub(r'(?<!^)(?=[A-Z])', ' ', concept_name)

def get_html_location(fact: Dict, ixbrl_locations: Dict) -> Optional[Dict]:
    """Get html_location for a fact"""
    concept = fact.get('concept', '').lower()
    context_ref = fact.get('context_ref', '').lower()
    key = (concept, context_ref)
    return ixbrl_locations.get(key)

def step_3_enrich(facts: List[Dict], ixbrl_locations: Dict) -> List[Dict]:
    """
    Step 3: Enrich facts with standard_concept and html_location
    """
    log("Step 3: Enriching facts")

    enriched_count = 0
    location_count = 0

    for fact in facts:
        # Add standard_concept
        concept = fact.get('concept', '')
        fact['standard_concept'] = get_standard_concept(concept)
        if concept in STANDARD_CONCEPT_MAP:
            enriched_count += 1

        # Add html_location
        location = get_html_location(fact, ixbrl_locations)
        if location:
            fact['html_location'] = location
            location_count += 1
        else:
            fact['html_location'] = None

    log(f"  Added standard_concept: {enriched_count}/{len(facts)}")
    log(f"  Added html_location: {location_count}/{len(facts)} ({location_count/len(facts)*100:.1f}%)")

    return facts

# ========== STEP 4: CLEAN ==========

def clean_text_value(text: str) -> str:
    """Clean text value: remove HTML, normalize unicode, normalize whitespace"""
    if not isinstance(text, str):
        return text

    # Remove HTML tags
    cleaned = re.sub(r'<[^>]+>', '', text)

    # Normalize whitespace
    cleaned = re.sub(r'\s+', ' ', cleaned)

    # Decode HTML entities
    import html
    cleaned = html.unescape(cleaned)

    # Normalize unicode to ASCII
    unicode_replacements = [
        ('\u2018', "'"), ('\u2019', "'"), ('\u201a', ","), ('\u201b', "'"),  # Single quotes
        ('\u201c', '"'), ('\u201d', '"'), ('\u201e', '"'), ('\u201f', '"'),  # Double quotes
        ('\u2010', '-'), ('\u2011', '-'), ('\u2012', '-'), ('\u2013', '-'),  # Dashes
        ('\u2014', '-'), ('\u2015', '-'),                                    # More dashes
        ('\u00ae', '(R)'), ('\u00a9', '(C)'), ('\u2122', '(TM)'),           # Symbols
        ('\u00b0', ' deg'), ('\u20ac', 'EUR'), ('\u00a3', 'GBP'),           # Currency
        ('\u00a5', 'JPY'), ('\u00a2', 'c'),                                 # More currency
    ]

    for unicode_char, ascii_char in unicode_replacements:
        cleaned = cleaned.replace(unicode_char, ascii_char)

    return cleaned.strip()

def step_4_clean(facts: List[Dict]) -> List[Dict]:
    """
    Step 4: Clean text values
    """
    log("Step 4: Cleaning text values")

    cleaned_count = 0
    textblock_count = 0

    for fact in facts:
        value = fact.get('value')
        if value and isinstance(value, str):
            has_html = bool(re.search(r'<[^>]+>', value))
            has_special = any(ord(c) > 127 for c in value)

            if has_html or has_special:
                fact['value'] = clean_text_value(value)
                cleaned_count += 1

                concept = fact.get('concept', '')
                if concept.endswith('TextBlock'):
                    textblock_count += 1

    log(f"  Cleaned {cleaned_count} values ({textblock_count} TextBlock + {cleaned_count - textblock_count} other)")
    return facts

# ========== STEP 5: FINALIZE ==========

def step_5_finalize(facts: List[Dict]) -> List[Dict]:
    """
    Step 5: Finalize by removing internal fields
    """
    log("Step 5: Finalizing")

    # Remove internal fields
    fields_to_remove = ['fact_key']

    for fact in facts:
        for field in fields_to_remove:
            fact.pop(field, None)

    return facts

# ========== SEGMENT EXTRACTION ==========

def extract_segments(xbrl, year: int, statements: Dict = None, year_col: str = None) -> Tuple[List[Dict], List[Dict]]:
    """
    Extract Business and Geographic segments from XBRL
    Returns: (business_segments, geographic_segments)
    """
    facts_df = xbrl.facts.to_dataframe()
    contexts = xbrl.contexts

    # Get fiscal year from DEI or facts
    fy_facts = facts_df[facts_df['concept'] == 'dei:DocumentFiscalYearFocus']
    if len(fy_facts) > 0:
        fiscal_year = int(fy_facts.iloc[0]['value'])
    else:
        fiscal_year = year - 1  # Assume filing year - 1

    business_segments = []
    geographic_segments = []

    # Track seen segments to avoid duplicates
    seen_business = set()
    seen_geographic = set()

    for ctx_id, ctx in contexts.items():
        if not hasattr(ctx, 'dimensions') or not ctx.dimensions:
            continue

        dims = ctx.dimensions
        period = ctx.period
        if not isinstance(period, dict):
            continue

        # Check period end date matches fiscal year
        period_end = period.get('endDate', '') if period.get('type') == 'duration' else period.get('instant', '')
        if str(fiscal_year) not in period_end:
            continue

        # Business Segments - Revenue & Operating Income
        if period.get('type') == 'duration':
            if 'us-gaap:StatementBusinessSegmentsAxis' in dims:
                member = dims['us-gaap:StatementBusinessSegmentsAxis']
                # Extract segment name from member
                raw_name = member.split(':')[-1] if ':' in member else member
                raw_name = raw_name.replace('SegmentMember', '').replace('Member', '')

                # Clean up common prefixes
                segment_name = raw_name
                if segment_name.startswith('aapl:'):
                    segment_name = segment_name[5:]  # Remove aapl: prefix
                elif segment_name.startswith('amzn:'):
                    segment_name = segment_name[5:]  # Remove amzn: prefix

                # Map common names
                name_map = {
                    'NorthAmerica': 'North America',
                    'International': 'International',
                    'AmazonWebServices': 'AWS',
                    'Americas': 'Americas',
                    'Europe': 'Europe',
                    'GreaterChina': 'Greater China',
                    'Japan': 'Japan',
                    'RestOfAsiaPacific': 'Rest of Asia Pacific',
                }
                segment_name = name_map.get(segment_name, segment_name)

                # Revenue
                rev = facts_df[
                    (facts_df['context_ref'] == ctx_id) &
                    (facts_df['concept'] == 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax')
                ]
                if len(rev) > 0:
                    row = rev.iloc[0]
                    key = (segment_name, 'Revenue')
                    if key not in seen_business:
                        seen_business.add(key)
                        business_segments.append({
                            "concept": row.get('concept', ''),
                            "context_ref": ctx_id,
                            "value": str(int(row['value'])) if pd.notna(row['value']) else str(row.get('value', '')),
                            "dimension": row.get('dimension', ''),
                            "member": row.get('member', ''),
                            "dimension_label": segment_name,
                            "segment_name": segment_name,
                            "metric_type": "Revenue",
                            "original_label": row.get('original_label', ''),
                            "standard_concept": row.get('concept', '').replace('us-gaap:', '').replace('aapl:', '').replace('amzn:', ''),
                            "unit": row.get('unit_ref', 'USD'),
                            "decimals": str(row.get('decimals', ''))
                        })

                # Operating Income
                op = facts_df[
                    (facts_df['context_ref'] == ctx_id) &
                    (facts_df['concept'] == 'us-gaap:OperatingIncomeLoss')
                ]
                if len(op) > 0:
                    row = op.iloc[0]
                    key = (segment_name, 'Operating Income')
                    if key not in seen_business:
                        seen_business.add(key)
                        business_segments.append({
                            "concept": row.get('concept', ''),
                            "context_ref": ctx_id,
                            "value": str(int(row['value'])) if pd.notna(row['value']) else str(row.get('value', '')),
                            "dimension": row.get('dimension', ''),
                            "member": row.get('member', ''),
                            "dimension_label": segment_name,
                            "segment_name": segment_name,
                            "metric_type": "Operating Income",
                            "original_label": row.get('original_label', ''),
                            "standard_concept": row.get('concept', '').replace('us-gaap:', '').replace('aapl:', '').replace('amzn:', ''),
                            "unit": row.get('unit_ref', 'USD'),
                            "decimals": str(row.get('decimals', ''))
                        })

        # Geographic Segments - Revenue (duration period)
        if period.get('type') == 'duration':
            if 'srt:StatementGeographicalAxis' in dims:
                member = dims['srt:StatementGeographicalAxis']

                # Country code to name mapping
                country_map = {
                    'country:US': 'United States',
                    'country:CN': 'China',
                    'country:DE': 'Germany',
                    'country:GB': 'United Kingdom',
                    'country:JP': 'Japan',
                }

                if member in country_map:
                    segment_name = country_map[member]
                elif 'UnitedStates' in member:
                    segment_name = 'United States'
                elif 'China' in member or 'GreaterChina' in member:
                    segment_name = 'Greater China'
                elif 'OtherCountries' in member:
                    segment_name = 'Other Countries'
                else:
                    # Skip unknown countries for now
                    continue

                rev = facts_df[
                    (facts_df['context_ref'] == ctx_id) &
                    (facts_df['concept'] == 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax')
                ]
                if len(rev) > 0:
                    row = rev.iloc[0]
                    key = (segment_name, 'Revenue')
                    if key not in seen_geographic:
                        seen_geographic.add(key)
                        geographic_segments.append({
                            "concept": row.get('concept', ''),
                            "context_ref": ctx_id,
                            "value": str(int(row['value'])) if pd.notna(row['value']) else str(row.get('value', '')),
                            "dimension": row.get('dimension', ''),
                            "member": row.get('member', ''),
                            "dimension_label": segment_name,
                            "segment_name": segment_name,
                            "metric_type": "Revenue",
                            "original_label": row.get('original_label', ''),
                            "standard_concept": row.get('concept', '').replace('us-gaap:', '').replace('aapl:', '').replace('amzn:', ''),
                            "unit": row.get('unit_ref', 'USD'),
                            "decimals": str(row.get('decimals', ''))
                        })

        # Geographic Segments - Assets (instant period)
        if period.get('type') == 'instant':
            if 'srt:StatementGeographicalAxis' in dims:
                member = dims['srt:StatementGeographicalAxis']

                country_map = {
                    'country:US': 'United States',
                    'country:CN': 'China',
                    'country:DE': 'Germany',
                    'country:GB': 'United Kingdom',
                    'country:JP': 'Japan',
                }

                if member in country_map:
                    segment_name = country_map[member]
                elif 'UnitedStates' in member:
                    segment_name = 'United States'
                elif 'China' in member or 'GreaterChina' in member:
                    segment_name = 'Greater China'
                elif 'OtherCountries' in member:
                    segment_name = 'Other Countries'
                else:
                    continue

                # Try multiple asset concepts
                asset_concepts = [
                    'us-gaap:NoncurrentAssets',
                    'us-gaap:Assets'
                ]
                for asset_concept in asset_concepts:
                    ast = facts_df[
                        (facts_df['context_ref'] == ctx_id) &
                        (facts_df['concept'] == asset_concept)
                    ]
                    if len(ast) > 0:
                        row = ast.iloc[0]
                        key = (segment_name, 'Assets')
                        if key not in seen_geographic:
                            seen_geographic.add(key)
                            geographic_segments.append({
                                "concept": row.get('concept', ''),
                                "context_ref": ctx_id,
                                "value": str(int(row['value'])) if pd.notna(row['value']) else str(row.get('value', '')),
                                "dimension": row.get('dimension', ''),
                                "member": row.get('member', ''),
                                "dimension_label": segment_name,
                                "segment_name": segment_name,
                                "metric_type": "Assets",
                                "original_label": row.get('original_label', ''),
                                "standard_concept": row.get('concept', '').replace('us-gaap:', '').replace('aapl:', '').replace('amzn:', ''),
                                "unit": row.get('unit_ref', 'USD'),
                                "decimals": str(row.get('decimals', ''))
                            })
                        break

    # Handle single-segment companies
    if not business_segments and statements and year_col:
        # Check if this is a single-segment company
        num_segments = facts_df[facts_df['concept'] == 'us-gaap:NumberOfReportableSegments']
        if len(num_segments) > 0:
            try:
                num = int(num_segments.iloc[0]['value'])
                if num == 1:
                    # Create synthetic segment from consolidated data
                    revenue = None
                    op_income = None

                    if statements.get('income') is not None:
                        income_df = statements['income']
                        # Try to get revenue
                        rev_row = income_df[income_df['concept'] == 'us-gaap_Revenues']
                        if len(rev_row) > 0:
                            val = rev_row.iloc[0].get(year_col)
                            if pd.notna(val):
                                revenue = float(val)

                        # Try to get operating income
                        op_row = income_df[income_df['concept'] == 'us-gaap_OperatingIncomeLoss']
                        if len(op_row) > 0:
                            val = op_row.iloc[0].get(year_col)
                            if pd.notna(val):
                                op_income = float(val)

                    segment_name = "Retail - Department Stores"  # Generic retail name

                    if revenue:
                        business_segments.append({
                            "concept": "us-gaap:Revenues",
                            "context_ref": "consolidated",
                            "value": str(int(revenue)),
                            "dimension": "",
                            "member": "",
                            "dimension_label": segment_name,
                            "segment_name": segment_name,
                            "metric_type": "Revenue",
                            "original_label": "Total revenue",
                            "standard_concept": "Revenues",
                            "unit": "USD",
                            "decimals": "-6"
                        })

                    if op_income:
                        business_segments.append({
                            "concept": "us-gaap:OperatingIncomeLoss",
                            "context_ref": "consolidated",
                            "value": str(int(op_income)),
                            "dimension": "",
                            "member": "",
                            "dimension_label": segment_name,
                            "segment_name": segment_name,
                            "metric_type": "Operating Income",
                            "original_label": "Operating income",
                            "standard_concept": "OperatingIncomeLoss",
                            "unit": "USD",
                            "decimals": "-6"
                        })
            except Exception as e:
                pass  # Silent fail for synthetic segment

    return business_segments, geographic_segments

def save_segments(business_segments: List[Dict], geographic_segments: List[Dict],
                  ticker: str, year: int, config: Config):
    """Save segment data to separate JSON files"""
    output_dir = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / config.FORM

    try:
        ensure_dir(output_dir)

        # Save Business Segments
        if business_segments:
            bs_path = output_dir / "BUSINESS_SEGMENTS.json"
            with open(bs_path, 'w', encoding='utf-8') as f:
                json.dump(business_segments, f, indent=2, ensure_ascii=False)
            log(f"  Saved Business Segments: {len(business_segments)} entries -> {bs_path}")

        # Save Geographic Segments
        if geographic_segments:
            gs_path = output_dir / "GEOGRAPHIC_SEGMENTS.json"
            with open(gs_path, 'w', encoding='utf-8') as f:
                json.dump(geographic_segments, f, indent=2, ensure_ascii=False)
            log(f"  Saved Geographic Segments: {len(geographic_segments)} entries -> {gs_path}")

    except Exception as e:
        log(f"  Error saving segments: {e}", "ERROR")

# ========== STEP 7: CALCULATE FINANCIAL RATIOS ==========

def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safe division with default value"""
    if denominator and denominator != 0:
        return numerator / denominator
    return default

def get_statement_value(statements: Dict, statement_type: str, concept_pattern: str, year_col: str) -> Optional[float]:
    """Get consolidated value from financial statements DataFrame"""
    if statement_type not in statements or statements[statement_type] is None:
        return None

    df = statements[statement_type]
    # Convert to underscore format
    underscore_pattern = concept_pattern.replace(':', '_')

    # Try exact match on underscore format
    matches = df[df['concept'] == underscore_pattern]

    candidates = []
    for _, row in matches.iterrows():
        val = row.get(year_col)
        if pd.notna(val) and val is not None:
            try:
                float_val = float(val)
                # Skip unreasonable values
                if abs(float_val) > 1e15:
                    continue
                # Check if consolidated (no dimension or member)
                dimension = row.get('dimension', '') or ''
                member = row.get('member', '') or ''
                is_consolidated = not dimension and not member
                candidates.append({
                    'value': float_val,
                    'is_consolidated': is_consolidated,
                    'concept': row.get('concept', '')
                })
            except:
                continue

    if not candidates:
        return None

    # Return first consolidated value, or largest value
    consolidated = [c for c in candidates if c['is_consolidated']]
    if consolidated:
        return consolidated[0]['value']
    return candidates[0]['value']

def get_year_columns(statements: Dict, target_year: int) -> Tuple[str, Optional[str]]:
    """Dynamically detect year columns from statements"""
    # Get columns from income statement (or any available statement)
    df = None
    for stmt_type in ['income', 'balance', 'cashflow']:
        if statements.get(stmt_type) is not None:
            df = statements[stmt_type]
            break

    if df is None:
        return (str(target_year), None)

    # Get all columns that look like dates (YYYY-MM-DD)
    date_cols = []
    for col in df.columns:
        if isinstance(col, str) and len(col) == 10 and col[4] == '-' and col[7] == '-':
            try:
                year = int(col[:4])
                date_cols.append((year, col))
            except:
                pass

    # Sort by year descending
    date_cols.sort(reverse=True)

    if not date_cols:
        return (str(target_year), None)

    # Find the column matching target year (fiscal year might be year-1 for calendar companies)
    year_col = None
    prev_year_col = None

    for i, (yr, col) in enumerate(date_cols):
        if yr == target_year or yr == target_year - 1:
            year_col = col
            if i + 1 < len(date_cols):
                prev_year_col = date_cols[i + 1][1]
            break

    if not year_col:
        year_col = date_cols[0][1]  # Most recent
        if len(date_cols) > 1:
            prev_year_col = date_cols[1][1]

    return (year_col, prev_year_col)

def calculate_all_ratios(statements: Dict, facts: List[Dict], ticker: str, year: int) -> List[Dict]:
    """Calculate all 47 financial ratios from statements and facts"""
    ratios = []

    # Dynamically detect year columns
    year_col, prev_year_col = get_year_columns(statements, year)
    log(f"  Using year columns: current={year_col}, prior={prev_year_col}")

    # Helper to get values from statements
    def get_val(statement_type, concept, col):
        return get_statement_value(statements, statement_type, concept, col)

    # ========== EXTRACT ALL METRICS ==========

    # Income Statement - Current Year
    # Revenue - multiple patterns for different companies
    revenue = get_val('income', 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', year_col)
    revenue_prev = get_val('income', 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax', prev_year_col) if prev_year_col else None

    # Net Income
    net_income = get_val('income', 'us-gaap:NetIncomeLoss', year_col)
    net_income_prev = get_val('income', 'us-gaap:NetIncomeLoss', prev_year_col) if prev_year_col else None

    # Gross Profit - try explicit first, then calculate from Revenue - COGS
    gross_profit = get_val('income', 'us-gaap:GrossProfit', year_col)
    gross_profit_prev = get_val('income', 'us-gaap:GrossProfit', prev_year_col) if prev_year_col else None
    cogs = get_val('income', 'us-gaap:CostOfGoodsAndServicesSold', year_col)
    if gross_profit is None and revenue and cogs:
        gross_profit = revenue - cogs
    if gross_profit_prev is None and revenue_prev and prev_year_col:
        cogs_prev = get_val('income', 'us-gaap:CostOfGoodsAndServicesSold', prev_year_col)
        if revenue_prev and cogs_prev:
            gross_profit_prev = revenue_prev - cogs_prev

    # Operating Income
    operating_income = get_val('income', 'us-gaap:OperatingIncomeLoss', year_col)
    operating_income_prev = get_val('income', 'us-gaap:OperatingIncomeLoss', prev_year_col) if prev_year_col else None

    # R&D - some companies don't report this separately (e.g., Amazon uses TechnologyAndInfrastructure)
    rd_exp = get_val('income', 'us-gaap:ResearchAndDevelopmentExpense', year_col)
    if rd_exp is None:
        rd_exp = get_val('income', 'us-gaap:TechnologyAndDevelopmentExpense', year_col)

    # SG&A - try combined first, then sum components
    sgna_exp = get_val('income', 'us-gaap:SellingGeneralAndAdministrativeExpense', year_col)
    if sgna_exp is None:
        # Try to sum Selling/Marketing + GeneralAndAdministrative
        selling = get_val('income', 'us-gaap:SellingExpense', year_col)
        if selling is None:
            selling = get_val('income', 'us-gaap:MarketingExpense', year_col)
        admin = get_val('income', 'us-gaap:GeneralAndAdministrativeExpense', year_col)
        if selling and admin:
            sgna_exp = selling + admin
        elif admin:
            sgna_exp = admin  # Some companies only report G&A

    # Balance Sheet - Current Year
    total_assets = get_val('balance', 'us-gaap:Assets', year_col)
    total_assets_prev = get_val('balance', 'us-gaap:Assets', prev_year_col) if prev_year_col else None

    # Total Liabilities - some companies don't have single concept, calculate from components
    total_liabilities = get_val('balance', 'us-gaap:Liabilities', year_col)
    if total_liabilities is None:
        current_liab = get_val('balance', 'us-gaap:LiabilitiesCurrent', year_col) or 0
        noncurrent_liab = get_val('balance', 'us-gaap:LiabilitiesNoncurrent', year_col) or 0
        other_noncurrent = get_val('balance', 'us-gaap:OtherLiabilitiesNoncurrent', year_col) or 0
        total_liabilities = current_liab + noncurrent_liab + other_noncurrent

    equity = get_val('balance', 'us-gaap:StockholdersEquity', year_col)
    equity_prev = get_val('balance', 'us-gaap:StockholdersEquity', prev_year_col) if prev_year_col else None
    current_assets = get_val('balance', 'us-gaap:AssetsCurrent', year_col)
    current_liabilities = get_val('balance', 'us-gaap:LiabilitiesCurrent', year_col)
    cash = get_val('balance', 'us-gaap:CashAndCashEquivalentsAtCarryingValue', year_col)
    ar = get_val('balance', 'us-gaap:AccountsReceivableNetCurrent', year_col)
    ar_prev = get_val('balance', 'us-gaap:AccountsReceivableNetCurrent', prev_year_col) if prev_year_col else None
    inventory = get_val('balance', 'us-gaap:InventoryNet', year_col)
    inventory_prev = get_val('balance', 'us-gaap:InventoryNet', prev_year_col) if prev_year_col else None

    # PP&E - multiple patterns for different companies
    ppe = get_val('balance', 'us-gaap:PropertyPlantAndEquipmentNet', year_col)
    if ppe is None:
        ppe = get_val('balance', 'us-gaap:PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization', year_col)

    # Debt
    lt_debt = get_val('balance', 'us-gaap:LongTermDebtNoncurrent', year_col)
    st_debt = get_val('balance', 'us-gaap:LongTermDebtCurrent', year_col)
    comm_paper = get_val('balance', 'us-gaap:CommercialPaper', year_col)

    # Cash Flow
    ocf = get_val('cashflow', 'us-gaap:NetCashProvidedByUsedInOperatingActivities', year_col)
    ocf_prev = get_val('cashflow', 'us-gaap:NetCashProvidedByUsedInOperatingActivities', prev_year_col) if prev_year_col else None

    # CapEx - multiple patterns
    capex = get_val('cashflow', 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment', year_col)
    if capex is None:
        capex = get_val('cashflow', 'us-gaap:PaymentsToAcquireProductiveAssets', year_col)

    depreciation = get_val('cashflow', 'us-gaap:DepreciationDepletionAndAmortization', year_col)

    # EPS from facts (not in statements)
    eps_basic = None
    eps_diluted = None
    shares = None
    for fact in facts:
        if fact.get('concept') == 'us-gaap:EarningsPerShareBasic' and not fact.get('dimension'):
            try:
                eps_basic = float(fact['value'])
            except:
                pass
        if fact.get('concept') == 'us-gaap:EarningsPerShareDiluted' and not fact.get('dimension'):
            try:
                eps_diluted = float(fact['value'])
            except:
                pass
        if fact.get('concept') == 'us-gaap:CommonStockSharesOutstanding' and not fact.get('dimension'):
            try:
                shares = float(fact['value'])
            except:
                pass

    # ========== DERIVED VALUES ==========
    total_debt = (lt_debt or 0) + (st_debt or 0) + (comm_paper or 0)
    ebitda = (operating_income or 0) + (depreciation or 0)
    ebitda_prev = (operating_income_prev or 0) + (depreciation or 0) if operating_income_prev else None
    fcf = (ocf or 0) - abs(capex or 0)
    fcf_prev = (ocf_prev or 0) - abs(capex or 0) if ocf_prev else None
    # NOPAT calculation using actual effective tax rate (NOT hardcoded 21%)
    tax_provision = get_val('income', 'us-gaap:IncomeTaxExpenseBenefit', year_col)
    pretax_income_calc = get_val('income', 'us-gaap:IncomeLossBeforeIncomeTaxExpenseBenefit', year_col)

    if tax_provision and pretax_income_calc and pretax_income_calc != 0:
        effective_tax_rate = abs(tax_provision) / pretax_income_calc
        nopat = operating_income * (1 - effective_tax_rate) if operating_income else None
        nopat_note = f"Using actual tax rate {effective_tax_rate:.1%}"
    else:
        # Fallback to statutory rate with CLEAR FLAG
        nopat = (operating_income or 0) * 0.79 if operating_income else None
        nopat_note = "WARNING: Using estimated 21% tax rate (actual unavailable)"
    # END NOPAT fix
    invested_capital = total_debt + (equity or 0)
    capital_employed = (total_assets or 0) - (current_liabilities or 0)

    # Average balances for turnover ratios
    avg_assets = ((total_assets or 0) + (total_assets_prev or 0)) / 2 if total_assets_prev else total_assets
    avg_equity = ((equity or 0) + (equity_prev or 0)) / 2 if equity_prev else equity
    avg_ar = ((ar or 0) + (ar_prev or 0)) / 2 if ar_prev else ar
    avg_inv = ((inventory or 0) + (inventory_prev or 0)) / 2 if inventory_prev else inventory

    def add_ratio(name: str, value: float, formula: str, category: str, is_pct: bool = False):
        if value is not None and not (is_pct and value == 0):
            display_val = value * 100 if is_pct else value
            ratios.append({
                "concept": "calculated",
                "context_ref": "calculated",
                "value": str(round(display_val, 2)),
                "dimension": "",
                "member": "",
                "dimension_label": "Company Wide",
                "ratio_name": name,
                "category": category,
                "formula": formula,
                "metric_type": "Financial Ratio",
                "is_percentage": is_pct,
                "fiscal_year": year,
                "original_label": name,
                "standard_concept": name.replace(' ', '').replace('/', '').replace('%', ''),
                "unit": "percent" if is_pct else "ratio",
                "html_location": {}
            })

    # ========== PROFITABILITY (8 ratios) ==========
    # Gross Margin - use explicit or calculated gross profit
    gross_margin_gp = gross_profit  # May have been calculated as Revenue - COGS
    if revenue and gross_margin_gp:
        add_ratio("Gross Margin %", safe_divide(gross_margin_gp, revenue), "Gross Profit / Revenue", "Profitability", True)
    if revenue and operating_income:
        add_ratio("Operating Margin %", safe_divide(operating_income, revenue), "Operating Income / Revenue", "Profitability", True)
    if revenue and net_income:
        add_ratio("Net Margin %", safe_divide(net_income, revenue), "Net Income / Revenue", "Profitability", True)
    if revenue and ebitda:
        add_ratio("EBITDA Margin %", safe_divide(ebitda, revenue), "EBITDA / Revenue", "Profitability", True)
    if revenue and operating_income:
        add_ratio("EBIT Margin %", safe_divide(operating_income, revenue), "EBIT / Revenue", "Profitability", True)
    if revenue and sgna_exp:
        add_ratio("SG&A Margin %", safe_divide(sgna_exp, revenue), "SG&A / Revenue", "Profitability", True)
    if revenue and rd_exp:
        add_ratio("R&D Margin %", safe_divide(rd_exp, revenue), "R&D / Revenue", "Profitability", True)
    # Pre-tax Margin - use actual Pre-tax Income (not Operating Income)
    pretax_income_val = get_val('income', 'us-gaap:IncomeLossBeforeIncomeTaxExpenseBenefit', year_col)
    if revenue and pretax_income_val:
        add_ratio("Pre-tax Margin %", safe_divide(pretax_income_val, revenue), "Pre-tax Income / Revenue", "Profitability", True)
    elif revenue and operating_income:
        # Fallback with clear flag
        add_ratio("Pre-tax Margin % (Est.)", safe_divide(operating_income, revenue), "Operating Income / Revenue (Pre-tax Income unavailable)", "Profitability", True)

    # ========== RETURNS (5 ratios) ==========
    if net_income and avg_assets:
        add_ratio("Return on Assets %", safe_divide(net_income, avg_assets), "Net Income / Avg Total Assets", "Returns", True)
    if net_income and avg_equity:
        add_ratio("Return on Equity %", safe_divide(net_income, avg_equity), "Net Income / Avg Equity", "Returns", True)
    if operating_income and invested_capital:
        add_ratio("Return on Capital %", safe_divide(operating_income, invested_capital), "Operating Income / Invested Capital", "Returns", True)
    if nopat and invested_capital:
        add_ratio("Return on Invested Capital %", safe_divide(nopat, invested_capital), "NOPAT / Invested Capital", "Returns", True)
    if nopat and capital_employed:
        add_ratio("Return on Capital Employed %", safe_divide(nopat, capital_employed), "NOPAT / Capital Employed", "Returns", True)

    # ========== LEVERAGE (8 ratios) ==========
    if total_debt and equity:
        add_ratio("Total Debt-to-Equity", safe_divide(total_debt, equity), "Total Debt / Equity", "Leverage")
    if total_debt and total_assets:
        add_ratio("Total Debt-to-Assets", safe_divide(total_debt, total_assets), "Total Debt / Total Assets", "Leverage")
    if lt_debt and equity:
        add_ratio("LT Debt-to-Equity", safe_divide(lt_debt, equity), "Long-term Debt / Equity", "Leverage")
    if total_debt and ebitda:
        add_ratio("Debt-to-EBITDA", safe_divide(total_debt, ebitda), "Total Debt / EBITDA", "Leverage")
    if total_liabilities and total_assets:
        add_ratio("Total Liabilities-to-Assets", safe_divide(total_liabilities, total_assets), "Total Liabilities / Total Assets", "Leverage")
    if total_debt and invested_capital:
        add_ratio("Total Debt-to-Capital", safe_divide(total_debt, invested_capital), "Debt / (Debt + Equity)", "Leverage")
    if lt_debt and invested_capital:
        add_ratio("LT Debt-to-Capital", safe_divide(lt_debt, invested_capital), "LT Debt / (Debt + Equity)", "Leverage")
    # Interest Coverage - try to get actual interest expense, then estimate
    interest_expense = get_val('income', 'us-gaap:InterestExpense', year_col)
    if interest_expense is None:
        interest_expense = get_val('income', 'us-gaap:InterestExpenseNonoperating', year_col)

    if operating_income and interest_expense and interest_expense > 0:
        add_ratio("Interest Coverage", safe_divide(operating_income, interest_expense), "Operating Income / Interest Expense", "Leverage")
    # REMOVED: Hardcoded $321M fallback was misleading - only calculate if actual data available
    elif operating_income and total_debt and total_debt > 0:
        # Estimate based on debt balance (5% avg interest rate assumption)
        est_interest = total_debt * 0.05
        add_ratio("Interest Coverage (Est.)", safe_divide(operating_income, est_interest), "Operating Income / Est. Interest (5% of Total Debt)", "Leverage")

    # ========== LIQUIDITY (4 ratios) ==========
    if current_assets and current_liabilities:
        add_ratio("Current Ratio", safe_divide(current_assets, current_liabilities), "Current Assets / Current Liabilities", "Liquidity")
        if inventory:
            add_ratio("Quick Ratio", safe_divide(current_assets - inventory, current_liabilities), "(CA - Inventory) / CL", "Liquidity")
    if cash and current_liabilities:
        add_ratio("Cash Ratio", safe_divide(cash, current_liabilities), "Cash / Current Liabilities", "Liquidity")
    if ocf and current_liabilities:
        add_ratio("OCF Ratio", safe_divide(ocf, current_liabilities), "Operating Cash Flow / Current Liabilities", "Liquidity")

    # ========== EFFICIENCY (8 ratios) ==========
    if revenue and avg_assets:
        add_ratio("Asset Turnover", safe_divide(revenue, avg_assets), "Revenue / Avg Total Assets", "Efficiency")
    if revenue and ppe:
        add_ratio("Fixed Asset Turnover", safe_divide(revenue, ppe), "Revenue / PP&E", "Efficiency")
    if cogs and avg_inv and avg_inv > 0:
        add_ratio("Inventory Turnover", safe_divide(cogs, avg_inv), "COGS / Avg Inventory", "Efficiency")
    if revenue and avg_ar and avg_ar > 0:
        add_ratio("Receivables Turnover", safe_divide(revenue, avg_ar), "Revenue / Avg AR", "Efficiency")
    if avg_ar and revenue:
        add_ratio("Days Sales Outstanding", safe_divide(avg_ar, revenue) * 365, "(Avg AR / Revenue) × 365", "Efficiency")
    if avg_inv and cogs:
        add_ratio("Days Inventory Outstanding", safe_divide(avg_inv, cogs) * 365, "(Avg Inv / COGS) × 365", "Efficiency")
    # Days Payable Outstanding - try actual AP first
    ap_actual = get_val('balance', 'us-gaap:AccountsPayableCurrent', year_col)
    if ap_actual and cogs:
        add_ratio("Days Payable Outstanding", safe_divide(ap_actual, cogs) * 365, "(AP / COGS) × 365", "Efficiency")
    elif cogs:
        # Estimate only if actual AP unavailable
        estimated_dpo_days = 45  # Conservative estimate
        ap_est = cogs * (estimated_dpo_days / 365)
        add_ratio("Days Payable Outstanding (Est.)", safe_divide(ap_est, cogs) * 365, f"Estimated {estimated_dpo_days} days (actual AP unavailable)", "Efficiency")
        dso = safe_divide(avg_ar, revenue) * 365 if avg_ar and revenue else 0
        dio = safe_divide(avg_inv, cogs) * 365 if avg_inv and cogs else 0
        dpo = safe_divide(ap_est, cogs) * 365
        add_ratio("Cash Conversion Cycle", dso + dio - dpo, "DSO + DIO - DPO", "Efficiency")

    # ========== CASH FLOW (5 ratios) ==========
    if ocf is not None and capex is not None:
        add_ratio("Free Cash Flow", fcf, "Operating Cash Flow - CapEx", "Cash Flow")
    if fcf and revenue:
        add_ratio("FCF Margin %", safe_divide(fcf, revenue), "FCF / Revenue", "Cash Flow", True)
    if ocf and revenue:
        add_ratio("OCF Margin %", safe_divide(ocf, revenue), "OCF / Revenue", "Cash Flow", True)
    if ocf and net_income:
        add_ratio("OCF to Net Income", safe_divide(ocf, net_income), "OCF / Net Income", "Cash Flow")
    if fcf and net_income:
        add_ratio("FCF to Net Income", safe_divide(fcf, net_income), "FCF / Net Income", "Cash Flow")

    # ========== GROWTH (5 ratios) ==========
    if revenue and revenue_prev:
        add_ratio("Revenue Growth %", (revenue - revenue_prev) / revenue_prev, "(Current - Prior) / Prior", "Growth", True)
    if net_income and net_income_prev:
        add_ratio("Net Income Growth %", (net_income - net_income_prev) / net_income_prev, "(Current - Prior) / Prior", "Growth", True)
    if gross_profit and gross_profit_prev:
        add_ratio("Gross Profit Growth %", (gross_profit - gross_profit_prev) / gross_profit_prev, "(Current - Prior) / Prior", "Growth", True)
    if ebitda and ebitda_prev:
        add_ratio("EBITDA Growth %", (ebitda - ebitda_prev) / ebitda_prev, "(Current - Prior) / Prior", "Growth", True)
    if operating_income and operating_income_prev:
        add_ratio("Operating Income Growth %", (operating_income - operating_income_prev) / operating_income_prev, "(Current - Prior) / Prior", "Growth", True)

    # ========== PER SHARE (4 ratios) ==========
    if eps_basic:
        add_ratio("EPS Basic", eps_basic, "Earnings Per Share Basic", "Per Share")
    if eps_diluted:
        add_ratio("EPS Diluted", eps_diluted, "Earnings Per Share Diluted", "Per Share")
    if equity and shares:
        add_ratio("Book Value Per Share", safe_divide(equity, shares), "Equity / Shares Outstanding", "Per Share")
    if fcf and shares:
        add_ratio("FCF Per Share", safe_divide(fcf, shares), "FCF / Shares Outstanding", "Per Share")

    return ratios

def fetch_financial_statements(filing) -> Dict:
    """Fetch all three financial statements as DataFrames"""
    statements = {'income': None, 'balance': None, 'cashflow': None}

    try:
        financials = filing.obj().financials
        statements['income'] = financials.income_statement().to_dataframe()
    except Exception as e:
        log(f"  Could not fetch income statement: {e}", "WARNING")

    try:
        financials = filing.obj().financials
        statements['balance'] = financials.balance_sheet().to_dataframe()
    except Exception as e:
        log(f"  Could not fetch balance sheet: {e}", "WARNING")

    try:
        financials = filing.obj().financials
        statements['cashflow'] = financials.cashflow_statement().to_dataframe()
    except Exception as e:
        log(f"  Could not fetch cash flow statement: {e}", "WARNING")

    return statements

def calculate_and_save_ratios(facts: List[Dict], filing, ticker: str, year: int, config: Config):
    """Calculate ratios and save to JSON"""
    # Fetch financial statements
    statements = fetch_financial_statements(filing)

    # Calculate ratios
    ratios = calculate_all_ratios(statements, facts, ticker, year)

    if not ratios:
        log("  No ratios calculated")
        return

    output_dir = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / config.FORM
    output_path = output_dir / "FINANCIAL_RATIOS.json"

    try:
        ensure_dir(output_dir)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(ratios, f, indent=2, ensure_ascii=False)
        log(f"  Saved {len(ratios)} ratios -> {output_path}")

        # Log summary by category
        by_cat = {}
        for r in ratios:
            cat = r['category']
            by_cat[cat] = by_cat.get(cat, 0) + 1
        log(f"  Ratios by category: {dict(by_cat)}")
    except Exception as e:
        log(f"  Error saving ratios: {e}", "ERROR")

def calculate_and_save_ratios_with_statements(facts: List[Dict], statements: Dict, year_col: str,
                                               ticker: str, year: int, config: Config):
    """Calculate ratios using pre-fetched statements and save to JSON"""
    # Calculate ratios
    ratios = calculate_all_ratios(statements, facts, ticker, year)

    if not ratios:
        log("  No ratios calculated")
        return

    output_dir = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / config.FORM
    output_path = output_dir / "FINANCIAL_RATIOS.json"

    try:
        ensure_dir(output_dir)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(ratios, f, indent=2, ensure_ascii=False)
        log(f"  Saved {len(ratios)} ratios -> {output_path}")

        # Log summary by category
        by_cat = {}
        for r in ratios:
            cat = r['category']
            by_cat[cat] = by_cat.get(cat, 0) + 1
        log(f"  Ratios by category: {dict(by_cat)}")
    except Exception as e:
        log(f"  Error saving ratios: {e}", "ERROR")

# ========== SAVE OUTPUT ==========

def save_output(facts: List[Dict], ticker: str, year: int, config: Config) -> bool:
    """Save output to JSON file"""
    output_dir = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / config.FORM
    output_path = output_dir / "FINAL_FACTS_FILTERED.json"

    if config.DRY_RUN:
        log(f"  DRY RUN: Would save to {output_path}")
        return True

    try:
        ensure_dir(output_dir)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(facts, f, indent=2, ensure_ascii=False)
        log(f"  Saved: {output_path}")
        return True
    except Exception as e:
        log(f"  Error saving output: {e}", "ERROR")
        return False

def save_html(html_content: str, ticker: str, year: int, config: Config) -> bool:
    """Save HTML file"""
    output_dir = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / config.FORM
    html_path = output_dir / "filing.html"

    if config.DRY_RUN:
        return True

    try:
        ensure_dir(output_dir)
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        log(f"  Saved HTML: {html_path}")
        return True
    except Exception as e:
        log(f"  Error saving HTML: {e}", "ERROR")
        return False

# ========== MAIN PROCESSING ==========

def process_company_year(ticker: str, year: int, config: Config) -> bool:
    """Process a single company-year combination.
    For 10-Q: discovers ALL quarterly filings and processes each one.
    For 10-K: processes the single annual filing (original behavior).
    """
    base_form = config.FORM  # e.g. "10-Q" or "10-K"

    # ── 10-Q: iterate ALL quarterly filings ──────────────────────────
    if base_form.upper() in ("10-Q", "10Q"):
        all_filings = download_all_filings(ticker, year, "10-Q")
        if not all_filings:
            log(f"No 10-Q filings found for {ticker} {year}", "WARNING")
            return False

        all_ok = True
        processed_quarters = []
        for filing in all_filings:
            quarter = determine_quarter(filing)
            # NEW FORMAT: Always use "10-Q-Q{N}" format (e.g., "10-Q-Q1")
            dir_name = f"10-Q-{quarter}"

            if quarter in processed_quarters:
                log(f"  Skipping duplicate {quarter} filing for {ticker} {year}", "WARNING")
                continue
            processed_quarters.append(quarter)

            print("\n" + "="*60)
            print(f"🔄 Processing: {ticker} - Year {year} - {quarter} (10-Q)")
            print("="*60)

            # Check if output already exists
            output_path = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / dir_name / "FINAL_FACTS_FILTERED.json"
            if config.SKIP_EXISTING and output_path.exists():
                log(f"Skipping (output exists): {output_path}")
                continue

            # Temporarily set config.FORM to the quarter-specific dir name
            config.FORM = dir_name
            ok = _process_single_filing(ticker, year, filing, config)
            if not ok:
                all_ok = False

        # Restore original form
        config.FORM = base_form
        return all_ok

    # ── 10-K (and other forms): original behavior ────────────────────
    print("\n" + "="*60)
    print(f"🔄 Processing: {ticker} - Year {year}")
    print("="*60)

    # Check if output already exists
    output_path = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / config.FORM / "FINAL_FACTS_FILTERED.json"
    if config.SKIP_EXISTING and output_path.exists():
        log(f"Skipping (output exists): {output_path}")
        return True

    # Step 1: Fetch data (uses download_filing → latest())
    data = step_1_fetch_data(ticker, year, config.FORM)
    if not data:
        return False

    html_content, ixbrl_locations, facts_df, filing_date = data
    return _process_fetched_data(ticker, year, html_content, ixbrl_locations, facts_df, config)


def _process_single_filing(ticker: str, year: int, filing, config: Config) -> bool:
    """Process a single filing object (used for 10-Q quarterly filings)."""
    filing_date = filing.filing_date
    log(f"  Filing date: {filing_date}")

    # Download HTML
    try:
        html_content = filing.html()
        log(f"  Downloaded HTML: {len(html_content)} bytes")
    except Exception as e:
        log(f"  Error downloading HTML: {e}", "ERROR")
        return False

    # Extract ixbrl locations
    ixbrl_locations = extract_ixbrl_locations(html_content)
    log(f"  Extracted {len(ixbrl_locations)} ixbrl locations")

    # Fetch XBRL facts
    facts_df = fetch_xbrl_facts(filing)
    if facts_df is None:
        return False
    log(f"  Fetched {len(facts_df)} XBRL facts")

    return _process_fetched_data(ticker, year, html_content, ixbrl_locations, facts_df, config, filing_obj=filing)


def _process_fetched_data(ticker: str, year: int, html_content: str,
                          ixbrl_locations: Dict, facts_df, config: Config,
                          filing_obj=None) -> bool:
    """Shared processing logic for both 10-K and 10-Q filings."""
    # Save HTML
    save_html(html_content, ticker, year, config)

    # Step 2: Filter & deduplicate
    facts, fiscal_year = step_2_filter_dedupe(facts_df, year)

    # Step 3: Enrich
    facts = step_3_enrich(facts, ixbrl_locations)

    # Step 4: Clean
    facts = step_4_clean(facts)

    # Step 5: Finalize
    facts = step_5_finalize(facts)

    # Save output
    if not save_output(facts, ticker, year, config):
        return False

    # Step 6: Fetch statements (used by Step 7 for financial ratios)
    # NOTE: Segment JSON generation disabled — all segment data (business &
    # geographic) is already in FINAL_FACTS_FILTERED.json with ixbrl_id.
    # Separate JSON files were redundant and are no longer created.
    log("Step 6: Preparing financial statements data")

    # Fetch statements once for ratios
    statements = {}
    year_col = None
    try:
        if filing_obj is not None:
            # Use the filing object directly (10-Q path)
            statements = fetch_financial_statements(filing_obj)
            year_col, _ = get_year_columns(statements, year)
        else:
            # Fetch fresh (10-K original path)
            company = Company(ticker)
            # For output dir, config.FORM may be "10-Q-Q1" etc., use base form for SEC API
            sec_form = config.FORM.split('-Q')[0] + '-' + config.FORM.split('-')[1] if '10-Q' in config.FORM else config.FORM
            if sec_form.startswith('10-Q'):
                sec_form = '10-Q'
            filings = company.get_filings(form=sec_form, year=year, amendments=False)
            if len(filings) > 0:
                filing_obj = filings.latest()
                statements = fetch_financial_statements(filing_obj)
                year_col, _ = get_year_columns(statements, year)
    except Exception as e:
        log(f"  Error fetching statements for ratios: {e}", "WARNING")
    # Step 7: Calculate and Save Financial Ratios
    log("Step 7: Calculating Financial Ratios")
    try:
        if statements and year_col:
            calculate_and_save_ratios_with_statements(facts, statements, year_col, ticker, year, config)
        else:
            # Fallback to old method
            if filing_obj is not None:
                calculate_and_save_ratios(facts, filing_obj, ticker, year, config)
            else:
                company = Company(ticker)
                sec_form = '10-Q' if '10-Q' in config.FORM else config.FORM
                filings = company.get_filings(form=sec_form, year=year, amendments=False)
                if len(filings) > 0:
                    calculate_and_save_ratios(facts, filings.latest(), ticker, year, config)
    except Exception as e:
        log(f"  Error calculating ratios: {e}", "WARNING")

    # Summary
    all_concepts = set(f['concept'] for f in facts)
    with_location = sum(1 for f in facts if f.get('html_location'))

    # Count segments and ratios
    output_dir = Path(config.OUTPUT_BASE_DIR) / ticker / str(year) / config.FORM
    bs_count = 0
    gs_count = 0
    ratios_count = 0
    try:
        bs_path = output_dir / "BUSINESS_SEGMENTS.json"
        gs_path = output_dir / "GEOGRAPHIC_SEGMENTS.json"
        ratios_path = output_dir / "FINANCIAL_RATIOS.json"
        if bs_path.exists():
            with open(bs_path) as f:
                bs_count = len(json.load(f))
        if gs_path.exists():
            with open(gs_path) as f:
                gs_count = len(json.load(f))
        if ratios_path.exists():
            with open(ratios_path) as f:
                ratios_count = len(json.load(f))
    except:
        pass

    display_form = config.FORM
    print(f"\n📊 Summary for {ticker} FY{fiscal_year} ({display_form}):")
    print(f"   Total facts: {len(facts)}")
    print(f"   Unique concepts: {len(all_concepts)}")
    print(f"   With html_location: {with_location} ({with_location/len(facts)*100:.1f}%)")
    if bs_count > 0:
        print(f"   Business Segments: {bs_count} entries")
    if gs_count > 0:
        print(f"   Geographic Segments: {gs_count} entries")
    if ratios_count > 0:
        print(f"   Financial Ratios: {ratios_count} ratios")

    return True

def main():
    """Main entry point"""
    config = Config()

    # Set identity
    set_identity(config.SEC_IDENTITY)

    # Statistics
    success_count = 0
    fail_count = 0
    total = len(config.TICKERS) * len(config.YEARS)

    print("="*60)
    print("🚀 XBRL Facts Export - Generalized")
    print("="*60)
    print(f"Companies: {', '.join(config.TICKERS)}")
    print(f"Years: {', '.join(map(str, config.YEARS))}")
    print(f"Form: {config.FORM}")
    print(f"Total combinations: {total}")
    print("="*60)

    # Process each combination
    for ticker in config.TICKERS:
        for year in config.YEARS:
            try:
                if process_company_year(ticker, year, config):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                log(f"Unexpected error processing {ticker} {year}: {e}", "ERROR")
                fail_count += 1

    # Final summary
    print("\n" + "="*60)
    print("🏁 FINAL SUMMARY")
    print("="*60)
    print(f"✅ Successful: {success_count}")
    print(f"❌ Failed: {fail_count}")
    print(f"📊 Total: {total}")
    print(f"\n📁 Output Files:")
    print(f"   {config.OUTPUT_BASE_DIR}/<TICKER>/<YEAR>/{config.FORM}/")
    print(f"   ├── FINAL_FACTS_FILTERED.json  (Main XBRL facts)")
    print(f"   ├── BUSINESS_SEGMENTS.json     (Operating segments)")
    print(f"   ├── GEOGRAPHIC_SEGMENTS.json   (Geographic breakdown)")
    print(f"   ├── FINANCIAL_RATIOS.json      (Calculated ratios)")
    print(f"   └── filing.html                (Raw HTML)")
    print("="*60)

    if fail_count == 0:
        print("🎉 All combinations processed successfully!")
    else:
        print(f"⚠️  {fail_count} combination(s) failed. Check logs above.")

if __name__ == "__main__":
    main()
