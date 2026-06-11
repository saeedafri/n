#!/usr/bin/env python3
"""
Key Developments Generator - VERSION 4 (Production-Grade)

Fixes and Improvements:
  - Correct exhibit access: eight_k.press_releases[0].text() instead of broken get_exhibits()
  - All 19 material 8-K item types (v3 had 8)
  - CEO vs CFO vs Board split on 8-K Item 5.02
  - ProxyStatement structured class (not regex) for DEF 14A
  - Schedule 13D/13G — Investor Activism layer
  - S-3/424B — Shelf/Offering layer
  - NT 10-K/NT 10-Q — Delayed Filing layer
  - 10-K: adds Item 3 (Legal Proceedings)
  - 10-Q: adds Part I Item 2 (MD&A guidance) and Part II Item 2 (unregistered equity)
  - News: parameterized query (no SQL injection), uses start_date, 30+ keywords
  - EDGAR identity + retry wrapper for rate-limit resilience
"""

import os
import sys
import uuid
import hashlib
import re
import time

# Use local edgartools dev clone if present (takes precedence over pip-installed version).
# If not present, the pip-installed edgartools is used automatically — no action needed.
_local_edgar = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'edgartools')
if os.path.isdir(_local_edgar):
    sys.path.insert(0, _local_edgar)

import pandas as pd
import pymysql
from datetime import datetime, timedelta

from edgar import set_identity, Company
from edgar.proxy import ProxyStatement
from edgar.beneficial_ownership import Schedule13D, Schedule13G

import openai
# ── Category mapping (inlined — no external dependency) ──────────────────────
# Maps script event_category → DB screening category used by screening service.
# This replaces the external category_mapping.py import so the script is fully
# self-contained (one file to deploy, no missing import errors).
CATEGORY_MAPPING = {
    # Earnings / Guidance
    "Earnings & Guidance":                "results_announcements",
    "Press Release":                      "results_announcements",
    "Restatements":                       "results_announcements",

    # Corporate Structure / M&A
    "M&A Activity":                       "announced_completed_transactions",
    "Capital Raising":                    "announced_completed_transactions",
    "IPO / Offerings":                    "announced_completed_transactions",
    "Corporate Actions":                  "announced_completed_transactions",
    "Strategic Alternatives":             "announced_completed_transactions",

    # Management / Governance
    "Management Changes":                 "corporate_structure",
    "Governance":                         "corporate_structure",
    "Proxy/Governance":                   "corporate_structure",
    "Auditor Changes":                    "corporate_structure",

    # Capital Returns
    "Capital Returns":                    "dividends_splits",
    "Dividends":                          "dividends_splits",

    # Distress / Restructuring
    "Layoffs & Restructuring":            "red_flags_distress",
    "Bankruptcy":                         "bankruptcy_updates",
    "Debt Default":                       "red_flags_distress",
    "Impairments":                        "red_flags_distress",
    "Risk Factors":                       "red_flags_distress",

    # Legal / Regulatory
    "Legal":                              "red_flags_distress",
    "Regulatory":                         "red_flags_distress",

    # Investor / Activism
    "Investor Activism":                  "corporate_structure",
    "Investor / Institutional Ownership": "corporate_structure",

    # Trading / Listing
    "Trading/Listing":                    "corporate_structure",

    # Operations
    "Store Operations":                   "customer_product",

    # Other / Catch-all
    "Other Events":                       "results_announcements",
    "General News":                       "results_announcements",
    "Credit/Ratings":                     "red_flags_distress",
}


def map_category(our_category: str) -> str:
    """Map script event_category to DB screening category. Defaults to results_announcements."""
    return CATEGORY_MAPPING.get(our_category, "results_announcements")


# ── API keys ────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv(
    'OPENAI_API_KEY',
    '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'
)
openai.api_key = OPENAI_API_KEY

# ── DB config ────────────────────────────────────────────────────────────────
# The DB is on an Azure private endpoint — hostname resolves only via corporate
# DNS (10.2.21.4). When running outside VPN the hostname lookup fails.
# We resolve to the private IP directly as a reliable fallback.
# Override via env vars for production deployments.
def _resolve_db_host() -> str:
    """Try hostname first; fall back to private IP if DNS fails."""
    hostname = os.getenv('DB_HOST_STG', 'csr-mysql8-flex-stg.mysql.database.azure.com')
    private_ip = os.getenv('DB_HOST_STG_IP', '10.2.6.4')
    import socket
    try:
        socket.getaddrinfo(hostname, 3306)
        return hostname
    except socket.gaierror:
        return private_ip


DB_CONFIG = {
    'host':            _resolve_db_host(),
    'port':            int(os.getenv('DB_PORT_STG', 3306)),
    'user':            os.getenv('DB_USER_STG', 'mohdsaeedafri'),
    'password':        os.getenv('DB_PASSWORD_STG', 'phirahshu2Ieshai8Aigei4xohx$'),
    'database':        os.getenv('DB_NAME_STG', 'coresight_market_data_stg'),
    'ssl_ca':          os.getenv('DB_SSL_CA_STG',
                           '/Users/mohdsaeedafri/Library/CloudStorage/'
                           'OneDrive-CoresightResearch/Coresight-Research/'
                           'DigiCertGlobalRootG2.crt.pem'),
    'ssl_verify_cert': True,
}

ETL_BATCH_ID = str(uuid.uuid4())

# ── 8-K item → (category, subtype) ──────────────────────────────────────────
# Covers every material item per your docx requirements.
EIGHT_K_ITEM_MAP = {
    # Earnings
    '2.02': ('Earnings & Guidance',      'Earnings Release'),
    # M&A / Corporate Actions
    '1.01': ('M&A Activity',             'Material Agreement'),
    '1.02': ('M&A Activity',             'M&A Cancellation'),
    '2.01': ('M&A Activity',             'M&A Closing'),
    '5.01': ('M&A Activity',             'Change in Control'),
    # Management / Governance  (5.02 refined by content below)
    '5.02': ('Management Changes',       'Executive/Director Change'),
    '5.03': ('Governance',               'Bylaw Amendment'),
    '5.07': ('Governance',               'Shareholder Vote'),
    '5.08': ('Governance',               'Director Nomination'),
    # Capital / Financing
    '2.03': ('Capital Raising',          'Debt Financing'),
    '2.04': ('Debt Default',             'Debt Default/Acceleration'),
    '3.02': ('Capital Raising',          'Private Placement'),
    # Distress / Restructuring
    '1.03': ('Bankruptcy',               'Bankruptcy Filing'),
    '2.05': ('Layoffs & Restructuring',  'Exit/Disposal Costs'),
    '2.06': ('Impairments',              'Material Impairment'),
    # Auditor / Restatement
    '4.01': ('Auditor Changes',          'Auditor Change'),
    '4.02': ('Restatements',             'Financial Restatement'),
    # Trading / Listing
    '3.01': ('Trading/Listing',          'Delisting Notice'),
    '3.03': ('Governance',               'Rights Modification'),
    # Legal / Regulatory
    '1.05': ('Legal',                    'Cybersecurity Incident'),
    # Press Release / Other
    '7.01': ('Press Release',            'Regulation FD Disclosure'),
    '8.01': ('Other Events',             'General'),
    '9.01': ('Other Events',             'Financial Statements'),
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_company_info(ticker: str) -> dict:
    """
    Fetch company metadata from coreiq_companies.

    Returns:
      company_id, ticker, cik, source, country_of_incorporation, is_sec

    is_sec = True  → company files with SEC EDGAR (has a CIK)
    is_sec = False → non-SEC company (no CIK, foreign non-filer); skip all EDGAR layers
    """
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()
        # Prefer the SEC row when a ticker appears under multiple sources (e.g. JD appears
        # as both SEC and YFinance). ORDER BY ensures SEC row wins when CIK is present.
        cursor.execute("""
            SELECT id, ticker, cik, source, country_of_incorporation,
                   COALESCE(name_coresight, name, '') as company_name
            FROM   coreiq_companies
            WHERE  ticker = %s
            ORDER  BY CASE WHEN cik IS NOT NULL AND cik != '' THEN 0 ELSE 1 END,
                      id ASC
            LIMIT  1
        """, (ticker,))
        result = cursor.fetchone()
        conn.close()
        if result:
            cik    = result[2] or ''
            source = result[3] or ''
            is_sec = bool(cik.strip()) or source.upper() == 'SEC'
            raw_name = (result[5] or '').strip()
            # Build short name variants for news relevance filtering
            # e.g. "Koninklijke Ahold Delhaize N.V." → ["ahold delhaize", "ahold", "delhaize"]
            # e.g. "Alimentation Couche-Tard Inc." → ["couche-tard", "alimentation"]
            import re as _re
            name_lower = raw_name.lower()
            # Strip legal suffixes (order matters — longer first)
            # Use regex word-boundary replacements to avoid partial word corruption
            # e.g. "corporation" should not become "oration" after removing "corp"
            _legal_patterns = [
                r',?\s+inc\.?', r',?\s+llc\.?', r',?\s+ltd\.?', r',?\s+plc',
                r'\s+n\.v\.', r'\s+s\.p\.a\.', r'\s+s\.a\.', r'\b ag\b', r'\b se\b',
                r'\b ab\b', r'\b oy\b', r',?\s+co\.,?\s+ltd\.?', r',?\s+co\.',
                r',?\s+corp\.?(?:oration)?', r'\.com', r',',
            ]
            for pat in _legal_patterns:
                name_lower = _re.sub(pat, ' ', name_lower)
            name_lower = _re.sub(r'\s+', ' ', name_lower).strip()

            # Words that are NOT useful company discriminators for news relevance.
            # Either too generic (appear in many company names) or too common in
            # English news text (would cause false positives for any article
            # mentioning the sector).
            _generic = {
                # Legal entity suffixes
                'group', 'holdings', 'holding', 'international', 'koninklijke',
                'wholesale', 'corporation', 'company', 'industries', 'enterprises',
                'global', 'limited', 'systems',
                # Retail / commerce
                'retail', 'retailing', 'retailer', 'commerce', 'trading',
                'commercial', 'distribution', 'wholesale',
                # Finance
                'capital', 'financial', 'finance', 'investment', 'investments',
                'ventures', 'partners', 'banking', 'insurance', 'asset', 'assets',
                # Technology
                'digital', 'technology', 'technologies', 'solutions', 'software',
                'platform', 'platforms', 'networks', 'network',
                # Real estate
                'properties', 'realty', 'property', 'estate', 'infrastructure',
                # General ops
                'management', 'resources', 'logistics', 'operations',
                'services', 'products', 'foods', 'food', 'brands', 'brand',
                'consumer', 'beverages', 'nutrition',
                # Geography adjectives that are part of many company names
                'national', 'american', 'european', 'british', 'french', 'german',
                'chinese', 'japanese', 'canadian', 'australian', 'asian', 'pacific',
                'atlantic', 'western', 'eastern', 'northern', 'southern',
                'north', 'south', 'east', 'west', 'united',
                # Common descriptive adjectives in company names
                'associated', 'alliance', 'premier', 'standard', 'general',
                'first', 'fast', 'smart', 'fresh', 'clean', 'pure', 'clear',
                'advanced', 'modern', 'future', 'prime', 'select', 'best',
                'great', 'super', 'mega', 'ultra', 'plus', 'core', 'new',
                # Energy / healthcare
                'energy', 'power', 'solar', 'health', 'healthcare', 'pharma',
                'medical',
                # Market / bank
                'market', 'markets', 'exchange', 'trust', 'bank',
            }

            # Build list: ticker (lower) + full cleaned name compound +
            # individual words that are specific enough to be useful filters.
            # Min length 5 avoids short noise words (e.g. "fast" len 4).
            # Max length 3 words already covered by the compound name variant.
            name_variants = [ticker.lower(), name_lower]
            for word in name_lower.split():
                word = word.strip(".,'-")
                if len(word) >= 5 and word not in _generic:
                    name_variants.append(word)
            return {
                'company_id':             result[0],
                'ticker':                 result[1],
                'cik':                    cik,
                'source':                 source,
                'country_of_incorporation': result[4] or '',
                'is_sec':                 is_sec,
                'company_name':           raw_name,
                'name_variants':          list(dict.fromkeys(name_variants)),  # dedup, preserve order
            }
    except Exception as e:
        print(f"    DB error: {e}")
    # Unknown company — optimistically attempt SEC (it will just find 0 filings)
    return {'company_id': None, 'ticker': ticker, 'cik': '', 'source': '',
            'country_of_incorporation': '', 'is_sec': True,
            'company_name': ticker, 'name_variants': [ticker.lower()]}


def compute_event_hash(event: dict, index: int = 0) -> str:
    """
    Content-stable hash — identical events always get the same hash regardless
    of run order or batch. This makes INSERT IGNORE truly idempotent across runs.

    NOTE: `index` parameter kept for API compatibility but is NO LONGER included
    in the hash — removing it was the critical production fix.
    """
    hash_input = "|".join([
        str(event.get('ticker', '')),
        str(event.get('event_category', '')),
        str(event.get('event_date', '')),
        str(event.get('source', '')),
        str(event.get('headline', ''))[:100].lower().strip(),
    ])
    return hashlib.sha256(hash_input.encode('utf-8')).hexdigest()


def get_primary_document_url(filing) -> str | None:
    """Return the direct iXBRL viewer URL for the primary HTML document.

    Format: https://www.sec.gov/ix?doc=/Archives/edgar/data/{cik}/{accession}/{doc}
    Falls back to the index page only if we can't determine the primary doc filename.
    """
    try:
        cik = str(filing.cik) if hasattr(filing, 'cik') else ''
        accession = filing.accession_no.replace('-', '')  # e.g. 000162828026019024

        # Try to get the primary HTML document filename from edgartools attachments
        try:
            atts = filing.attachments
            primary = None
            # Primary document is usually sequence 1, type matching the form
            for att in (atts.primary_documents if hasattr(atts, 'primary_documents') else []):
                if att.extension in ('.htm', '.html') and not att.is_report():
                    primary = att.document
                    break
            if primary:
                return f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{cik}/{accession}/{primary}"
        except Exception:
            pass

        # Fallback: index page
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filing.accession_no}-index.htm"
    except Exception:
        pass
    return None


def get_exhibit_url(filing, exhibit_type: str = 'EX-99.1') -> str | None:
    """Return the direct URL for a specific exhibit (e.g. EX-99.1 press release)."""
    try:
        cik = str(filing.cik) if hasattr(filing, 'cik') else ''
        accession = filing.accession_no.replace('-', '')
        atts = filing.attachments
        for att in (atts.exhibits if hasattr(atts, 'exhibits') else []):
            if att.document_type and att.document_type.upper().startswith(exhibit_type.upper()):
                if att.document and att.extension in ('.htm', '.html', '.txt'):
                    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{att.document}"
    except Exception:
        pass
    return None


def _parse_period_date(filing):
    """Return (year_str, period_date_str) from filing.

    Prefers reportDate (from SEC index, no download) over period_of_report
    (triggers SGML download) to keep this fast.
    """
    from datetime import datetime as _dt
    # reportDate is available on Filing objects from the index without SGML download
    for attr in ('reportDate', 'report_date'):
        val = getattr(filing, attr, None)
        if val:
            period_str = str(val)[:10]
            try:
                d = _dt.strptime(period_str, '%Y-%m-%d')
                return str(d.year), period_str
            except Exception:
                pass
    # Fallback: filing_date (always available from index)
    try:
        fd = filing.filing_date
        period_str = str(fd)[:10]
        d = _dt.strptime(period_str, '%Y-%m-%d')
        return str(d.year), period_str
    except Exception:
        return '', ''


def build_10q_quarter_map(company) -> dict:
    """
    Pre-fetch ALL 10-Q filings for a company and build a lookup:
        period_date_str → (year_str, doc_type)  (e.g. "2025-08-02" → ("2025", "10-Q-Q2"))

    Uses the filings index DataFrame (reportDate column) — no per-filing SGML
    download needed, so this is fast (~1 network call to SEC).

    Logic: group filings by calendar year of reportDate, then within each year
    sort ascending by date → first = Q1, second = Q2, third = Q3.
    This is accurate for ANY fiscal year convention (calendar or non-calendar)
    because we rely on ordering, not month ranges.
    """
    from collections import defaultdict
    year_periods = defaultdict(set)  # year_str → set of period_date_str
    try:
        all_tenq = company.get_filings(form='10-Q')
        df = all_tenq.to_pandas()
        # reportDate is the period_of_report from the SEC submissions index
        for _, row in df.iterrows():
            report_date = str(row.get('reportDate', '') or '')[:10]
            if not report_date or report_date == 'nan':
                continue
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(report_date, '%Y-%m-%d')
                year_periods[str(d.year)].add(report_date)
            except Exception:
                pass
    except Exception:
        return {}

    quarter_map = {}
    for year_str, periods in year_periods.items():
        sorted_periods = sorted(periods)  # ascending = Q1, Q2, Q3
        labels = ['10-Q-Q1', '10-Q-Q2', '10-Q-Q3']
        for idx, p in enumerate(sorted_periods[:3]):
            quarter_map[p] = (year_str, labels[idx])
    return quarter_map


def _internal_filing_url(ticker: str, form: str, filing, quarter_map: dict = None) -> str:
    """
    Build a deep-link URL to our internal Company Filings page.

    For 10-Q: uses quarter_map (built by build_10q_quarter_map) for accurate
    Q1/Q2/Q3 assignment regardless of fiscal year convention.
    Falls back to filing_date year if no map provided.

    Returns a URL of the form:
      /company_filings?ticker=M&doc_type=10-K&year=2026
      /company_filings?ticker=M&doc_type=10-Q-Q1&year=2025
    """
    BASE = "/company_filings"
    form_upper = str(form).upper().replace('/', '')

    if form_upper in ('10K', '10-K'):
        year_str, _ = _parse_period_date(filing)
        doc_type = '10-K'

    elif form_upper in ('10Q', '10-Q'):
        _, period_str = _parse_period_date(filing)
        if quarter_map and period_str in quarter_map:
            year_str, doc_type = quarter_map[period_str]
        else:
            # Fallback: use filing_date year and filing_date order within year
            # (less accurate but better than month-range guessing)
            year_str, _ = _parse_period_date(filing)
            doc_type = '10-Q-Q1'  # safe fallback; log so we can investigate

    elif form_upper in ('DEF14A', 'DEF 14A'):
        year_str, _ = _parse_period_date(filing)
        doc_type = '10-K'
    else:
        year_str, _ = _parse_period_date(filing)
        doc_type = '10-K'

    params = f"ticker={ticker}&doc_type={doc_type}&year={year_str}"
    return f"{BASE}?{params}"


def safe_filing_obj(filing, retries: int = 2):
    """Call filing.obj() with simple retry on transient errors."""
    for attempt in range(retries + 1):
        try:
            return filing.obj()
        except Exception as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                raise


def normalize_item_number(raw: str) -> str:
    """'Item 2.02' → '2.02', '2.02' → '2.02'."""
    m = re.search(r'(\d+\.\d+)', str(raw))
    return m.group(1) if m else str(raw).strip()


def classify_502_subtype(content_text: str) -> tuple[str, str]:
    """Refine 8-K Item 5.02 into CEO / CFO / COO / Board / Other."""
    t = (content_text or '').lower()
    if 'chief executive' in t or ' ceo' in t:
        return 'Management Changes', 'Executive Changes - CEO'
    if 'chief financial' in t or ' cfo' in t:
        return 'Management Changes', 'Executive Changes - CFO'
    if 'chief operating' in t or ' coo' in t:
        return 'Management Changes', 'Executive Changes - COO'
    if 'chief' in t or 'president' in t:
        return 'Management Changes', 'Executive Changes - Other C-Suite'
    if 'director' in t or 'board' in t:
        return 'Governance', 'Board Director Change'
    return 'Management Changes', 'Executive/Board Changes - Other'


# ── LLM extraction ───────────────────────────────────────────────────────────

# ── Standard situation formatter ─────────────────────────────────────────────

def _fmt(label: str, value) -> str:
    """Format a label:value pair. Returns empty string if value is None/empty."""
    v = str(value).strip() if value else ''
    return f"{label}: {v}\n" if v else ''


def _safe_text(obj, max_chars: int = 3000) -> str:
    """Safely extract text from an edgartools section/property object."""
    if obj is None:
        return ''
    try:
        if hasattr(obj, 'text'):
            t = obj.text()
            return str(t)[:max_chars] if t else ''
        return str(obj)[:max_chars]
    except Exception:
        return ''


def _standard_header(ticker: str, filing_date, form: str, section: str) -> str:
    """
    One-line standard header for every situation field.
    Ensures consistent display format across all companies and filing types.
    Example: "[M | 2025-03-18 | 10-K] Risk Factors"
    """
    return f"[{ticker} | {filing_date} | {form}] {section}\n{'─'*60}\n"


def extract_with_llm(content: str, ticker: str, filing_date, context: str = "earnings") -> str | None:
    """
    LLM extraction — called ONLY for earnings press releases (8-K 2.02 / 6-K).
    All other content is extracted directly from edgartools structured properties.

    Token discipline:
    - Input capped at 10,000 chars (enough for a full press release)
    - Output max_tokens=800 (structured tables only — no narrative padding)
    - temperature=0.0 for deterministic extraction

    Output format is standardised across all companies:
      GUIDANCE_DIRECTION: RAISED | LOWERED | MAINTAINED | NEW | NONE
      PERIOD_RESULTS: table
      FORWARD_GUIDANCE: table
      KEY_HIGHLIGHTS: bullets
    """
    if not content or len(content) < 100:
        return None

    # Hard cap at 10k chars — beyond this is boilerplate / legal text
    content_sample = content[:10000]

    prompt = f"""Extract structured financial data from this {ticker} earnings press release.
Filing date: {filing_date}. Context: {context}.

INSTRUCTIONS:
1. This press release may report QUARTERLY results, FULL YEAR (annual) results, or BOTH — extract ALL that are present.
2. For each period, show GUIDANCE (what was predicted) vs ACTUALS (what was achieved) side by side.
3. Guidance = company's own prior guidance range or analyst consensus mentioned in the document.
4. Actuals = reported results for that period.
5. If no prior guidance was given for a metric, write "[not provided]".
6. Look for phrases like "exceeded guidance", "within guidance range", "compared to prior guidance of".
7. Always include the actual numbers — never leave actuals blank.

DOCUMENT:
---
{content_sample}
---

Respond EXACTLY in this format (no preamble, no explanation):

GUIDANCE_DIRECTION: [RAISED|LOWERED|MAINTAINED|NEW|NONE]

FULL YEAR (ANNUAL) RESULTS:
Metric | Prior Guidance | Actuals | YoY Change
Net sales | $X.X–$X.Xbn | $X.Xbn | +/-X%
Comparable sales change | X% to X% | X% | —
Adjusted diluted EPS | $X.XX–$X.XX | $X.XX | +/-X%
[continue for ALL annual metrics in the document; SKIP SECTION if no annual results]

QUARTERLY RESULTS (Q[N] FY[YYYY]):
Metric | Prior Guidance | Actuals | YoY Change
Net sales | $X.X–$X.Xbn | $X.Xbn | +/-X%
Comparable sales change | X% to X% | X% | —
Adjusted diluted EPS | $X.XX–$X.XX | $X.XX | +/-X%
[continue for ALL quarterly metrics; SKIP SECTION if no quarterly results]

FORWARD GUIDANCE (next period/year, if provided):
Period | Metric | Low | High | Notes
[NONE if not present]

KEY HIGHLIGHTS:
- [specific number, e.g. "Net sales $7.9bn, beat consensus $7.7bn; Q4 comp sales +0.8%"]
- [guidance vs actuals comparison with specific figures]
- [any notable items: impairments, restructuring charges, one-time items]
[3–5 bullets only; numbers required, no vague narrative]

CRITICAL RULES:
- Never fabricate numbers — only use figures explicitly stated in the document.
- Always show BOTH guidance and actuals in the same table row.
- Use the company's own metric names exactly as written.
- Include ALL metrics mentioned (revenue, EPS, comp sales, margins, cash flow, etc.)."""

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": "Financial data extraction. Output only the structured format requested. "
                            "No preamble. No explanation. Numbers from the document only."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1200,   # Annual + quarterly tables need more room than single-period
            temperature=0.0,   # Deterministic — same doc always gives same output
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"      LLM error: {e}")
        return None


# ── Press release content ─────────────────────────────────────────────────────

def get_press_release_text(eight_k) -> str | None:
    """
    Get EX-99.1 press release text using the correct edgartools API.

    v3 bug: called get_exhibits('EX-99') incorrectly — content was never actually
    retrieved, so LLM was running on 8-K body text ("See Exhibit 99.1").

    v4 fix: use eight_k.press_releases (edgartools property that already queries
    for EX-99.1/EX-99/EX-99.01 HTML attachments) and call .text() on the result.
    Fallback: get_exhibits(prefix='EX-99') with explicit download().
    """
    # Primary: press_releases property
    try:
        prs = eight_k.press_releases
        if prs and len(prs) > 0:
            text = prs[0].text()
            if text and len(text) > 200:
                return text
    except Exception:
        pass

    # Fallback: get_exhibits with prefix filter + explicit download
    try:
        exhibits = eight_k.get_exhibits(prefix='EX-99')
        for ex in exhibits:
            try:
                raw = ex.download()
                if raw is None:
                    continue
                text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
                if len(text) > 200:
                    return text[:20000]
            except Exception:
                pass
    except Exception:
        pass

    return None


def get_8k_item_text_clean(filing, item_label: str, max_chars: int = 6000) -> str:
    """
    Extract clean text for a specific 8-K item by downloading the primary HTML
    document and parsing with BeautifulSoup.

    The standard edgartools item accessor (eight_k[item]) returns poorly formatted
    text for older filings that use multi-column HTML layouts — the columns get
    interleaved and content gets garbled. This function downloads the actual HTML
    and finds the item section directly, producing clean readable prose.

    Falls back to empty string on any error (caller uses edgartools fallback).
    """
    try:
        import httpx
        from bs4 import BeautifulSoup

        atts = filing.attachments
        # Find the primary 8-K HTML document (sequence 1, .htm/.html, not XBRL)
        primary_att = None
        for att in (atts.primary_documents if hasattr(atts, 'primary_documents') else []):
            if att.extension in ('.htm', '.html') and not att.is_report():
                primary_att = att
                break
        if primary_att is None:
            # Fallback: first .htm document
            for att in atts.documents:
                if att.extension in ('.htm', '.html'):
                    primary_att = att
                    break
        if primary_att is None:
            return ''

        html = httpx.get(
            primary_att.url,
            headers={'User-Agent': 'Coresight Research dev@coresight.com'},
            timeout=15,
        ).text

        soup = BeautifulSoup(html, 'lxml')
        for tag in soup(['script', 'style', 'head']):
            tag.decompose()
        full_text = soup.get_text(separator=' ', strip=True)
        # Collapse excessive whitespace
        full_text = re.sub(r'[ \t]{2,}', ' ', full_text)
        full_text = re.sub(r'\n{3,}', '\n\n', full_text)

        # Find the item section (try multiple label formats)
        search_labels = [
            f'item {item_label}.',
            f'item {item_label}',
            f'item{item_label}.',
        ]
        start_idx = -1
        for lbl in search_labels:
            idx = full_text.lower().find(lbl)
            if idx >= 0:
                start_idx = idx
                break

        if start_idx < 0:
            return ''

        # End: next item section or signature block
        section_text = full_text[start_idx:start_idx + max_chars]
        # Trim at signature line to remove boilerplate
        for stop_marker in ['SIGNATURE\n', 'Pursuant to the requirements of the Securities Exchange Act']:
            sig_idx = section_text.find(stop_marker)
            if sig_idx > 200:  # keep at least 200 chars
                section_text = section_text[:sig_idx].strip()
                break

        return section_text[:max_chars]

    except Exception:
        return ''


# ── Filing processors ─────────────────────────────────────────────────────────

def process_8k_filing(filing, ticker: str, company_info: dict) -> list:
    """
    Process 8-K filing.
    - Covers all 19 material item types (v3 had 8)
    - Correct press release extraction via eight_k.press_releases
    - CEO/CFO/Board differentiation for Item 5.02
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)

    try:
        eight_k = safe_filing_obj(filing)
        if eight_k is None:
            return events
    except Exception as e:
        print(f"    8-K obj() error [{filing.accession_no}]: {e}")
        return events

    raw_items = eight_k.items if hasattr(eight_k, 'items') else []

    for raw_item in raw_items:
        item_num = normalize_item_number(raw_item)

        # Bug Fix: suppress 9.01 — pure exhibit cover page, zero information content
        if item_num == '9.01':
            continue

        if item_num not in EIGHT_K_ITEM_MAP:
            # Capture anything unmapped as Other Events so nothing is dropped
            category, subtype = 'Other Events', f'8-K Item {item_num}'
        else:
            category, subtype = EIGHT_K_ITEM_MAP[item_num]

        # Get item body text — try clean HTML extraction first, fallback to edgartools parser
        # No truncation here — full content preserved, only the DB insert caps at TEXT limit
        item_content = ''
        try:
            clean = get_8k_item_text_clean(filing, item_num, max_chars=30000)
            if clean and len(clean) > 100:
                item_content = clean
        except Exception:
            pass
        if not item_content:
            try:
                raw_content = eight_k[str(raw_item)]
                if raw_content:
                    item_content = str(raw_content)[:30000]
            except Exception:
                pass

        situation = item_content

        # Bug Fix: reclassify 8.01 based on content (dividend/buyback/split vs generic)
        if item_num == '8.01':
            content_lower = item_content.lower()
            if any(k in content_lower for k in ['dividend', 'distribution', 'declared', 'per share', 'quarterly dividend']):
                category, subtype = 'Capital Returns', 'Dividend Announcement'
            elif any(k in content_lower for k in ['repurchase', 'buyback', 'buy back', 'share purchase program']):
                category, subtype = 'Capital Returns', 'Buyback Announcement'
            elif any(k in content_lower for k in ['stock split', 'stock dividend', 'forward split']):
                category, subtype = 'Capital Returns', 'Stock Split/Stock Dividend'

        # Refine 5.02 category based on what title is mentioned
        if item_num == '5.02':
            category, subtype = classify_502_subtype(item_content)

        # ── 2.02 Earnings: structured earnings + press release + LLM ───────────
        if item_num == '2.02':
            header = _standard_header(ticker, filing_date, 'SEC 8-K', 'Earnings Release')

            # Use EX-99.1 press release URL as source_ref (more useful than index page)
            pr_url = get_exhibit_url(filing, 'EX-99')
            if pr_url:
                doc_url = pr_url

            # 1. Try edgartools structured earnings object first (no LLM needed)
            structured = ''
            try:
                if eight_k.has_earnings and eight_k.earnings:
                    e = eight_k.earnings
                    structured = _safe_text(e, 4000)
            except Exception:
                pass

            # 2. Get press release text for LLM extraction
            pr_text = get_press_release_text(eight_k)
            llm_result = ''
            if pr_text:
                llm_result = extract_with_llm(pr_text, ticker, filing_date,
                                              'Earnings Press Release (EX-99.1)') or ''

            # 3. Build situation with standard header
            parts = [header]
            if structured:
                parts.append(f"STRUCTURED DATA:\n{structured}\n")
            if llm_result:
                parts.append(f"LLM EXTRACTION:\n{llm_result}")
            elif item_content:
                parts.append(item_content)
            situation = ''.join(parts)

            # 4. Enrich subtype with guidance direction
            if llm_result and 'GUIDANCE_DIRECTION:' in llm_result:
                m = re.search(r'GUIDANCE_DIRECTION:\s*(\w+)', llm_result)
                if m and m.group(1) not in ('NONE', 'N/A'):
                    subtype = f"Earnings Release - Guidance {m.group(1).title()}"

        # ── 5.02 Management Changes: full clean item text, no LLM ──────────────
        elif item_num == '5.02':
            header = _standard_header(ticker, filing_date, 'SEC 8-K', subtype)
            # item_content already has clean HTML-extracted text from get_8k_item_text_clean()
            situation = header + (item_content or f"{ticker} filed 8-K Item 5.02.")

        # ── 4.01 Auditor: use structured auditor object ─────────────────────
        elif item_num == '4.01':
            header = _standard_header(ticker, filing_date, 'SEC 8-K', 'Auditor Change')
            try:
                aud = eight_k.auditor
                if aud:
                    aud_text = _safe_text(aud, 2000)
                    situation = header + (aud_text or item_content)
            except Exception:
                situation = header + item_content

        if not situation:
            situation = _standard_header(ticker, filing_date, 'SEC 8-K', f'Item {item_num}') + \
                        (item_content or f"{ticker} filed SEC Form 8-K Item {item_num}.")

        event = {
            'company_id':     company_info.get('company_id'),
            'ticker':         company_info.get('ticker', ticker),
            'event_date':     filing_date,
            'edgar_filing_date': filing_date,
            'event_category': category,
            'event_subtype':  subtype,
            'headline':       f"{subtype} (8-K Item {item_num})",
            'situation':      situation[:30000],
            'source':         'SEC EDGAR 8-K',
            'source_ref':     doc_url,
        }
        event['event_hash']   = compute_event_hash(event)
        event['etl_batch_id'] = ETL_BATCH_ID
        events.append(event)

    return events


def process_6k_filing(filing, ticker: str, company_info: dict) -> list:
    """Process 6-K (foreign private issuers). Uses press_releases property."""
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)

    try:
        six_k = safe_filing_obj(filing)
        if six_k is None:
            return events

        # Try press_releases first (EX-99.x HTML attachments)
        text_content = ''
        try:
            prs = six_k.press_releases
            if prs and len(prs) > 0:
                text_content = prs[0].text() or ''
        except Exception:
            pass

        if not text_content:
            try:
                text_content = filing.text() or ''
            except Exception:
                pass

        text_lower = text_content.lower()[:10000]

        if any(kw in text_lower for kw in ['earnings', 'results for the', 'quarter ended', 'revenue', 'eps']):
            category, subtype = 'Earnings & Guidance', 'Earnings Release (6-K)'
        elif any(kw in text_lower for kw in ['dividend', 'distribution']):
            category, subtype = 'Capital Returns', 'Dividend (6-K)'
        elif any(kw in text_lower for kw in ['appointed', 'resigned', 'chief executive', 'chief financial']):
            category, subtype = 'Management Changes', 'Management Change (6-K)'
        elif any(kw in text_lower for kw in ['acquisition', 'merger', 'divestiture']):
            category, subtype = 'M&A Activity', 'M&A News (6-K)'
        elif any(kw in text_lower for kw in ['bankruptcy', 'chapter 11', 'restructur']):
            category, subtype = 'Bankruptcy', 'Bankruptcy/Restructuring (6-K)'
        else:
            category, subtype = 'Other Events', '6-K Filing'

        situation = text_content[:2000] if text_content else f"{ticker} filed 6-K."
        if category == 'Earnings & Guidance' and text_content:
            llm_result = extract_with_llm(text_content[:15000], ticker, filing_date, '6-K Earnings')
            if llm_result:
                situation = f"Foreign company 6-K filing.\n\n{'='*60}\nLLM EXTRACTION:\n{'='*60}\n{llm_result}"

    except Exception as e:
        print(f"    6-K error: {e}")
        return events

    event = {
        'company_id':     company_info.get('company_id'),
        'ticker':         company_info.get('ticker', ticker),
        'event_date':     filing_date,
        'edgar_filing_date': filing_date,
        'event_category': category,
        'event_subtype':  subtype,
        'headline':       subtype,
        'situation':      situation[:30000],
        'source':         'SEC EDGAR 6-K',
        'source_ref':     doc_url,
    }
    event['event_hash']   = compute_event_hash(event)
    event['etl_batch_id'] = ETL_BATCH_ID
    events.append(event)
    return events


def process_10k_filing(filing, ticker: str, company_info: dict) -> list:
    """
    Process 10-K.
    v4 adds Item 3 (Legal Proceedings) — was missing in v3.
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)

    try:
        ten_k = safe_filing_obj(filing)
        if ten_k is None:
            return events
    except Exception as e:
        print(f"    10-K obj() error: {e}")
        return events

    internal_url = _internal_filing_url(ticker, '10-K', filing)

    def _make_10k_event(category, subtype, content_text):
        hdr = _standard_header(ticker, filing_date, '10-K', subtype)
        event = {
            'company_id':     company_info.get('company_id'),
            'ticker':         company_info.get('ticker', ticker),
            'event_date':     filing_date,
            'edgar_filing_date': filing_date,
            'event_category': category,
            'event_subtype':  subtype,
            'headline':       f"Annual Report (10-K) — {subtype}",
            'situation':      (hdr + str(content_text)[:29000])[:30000],
            'source':         'SEC EDGAR 10-K',
            'source_ref':     internal_url,
        }
        event['event_hash']   = compute_event_hash(event)
        event['etl_batch_id'] = ETL_BATCH_ID
        return event

    # ── Use structured edgartools properties directly (no item key guessing) ──

    # 1. Risk Factors — ten_k.risk_factors
    try:
        rf = _safe_text(ten_k.risk_factors, 5000)
        if rf and len(rf) > 200:
            events.append(_make_10k_event('Risk Factors', '10-K Risk Factors', rf))
    except Exception:
        pass

    # 2. Legal Proceedings — Item 3 via get_item_with_part or items dict
    try:
        legal = None
        for key in ['Item 3', '3', 'Part I, Item 3']:
            try:
                if hasattr(ten_k, 'get_item_with_part'):
                    legal = ten_k.get_item_with_part('1', '3')
                if not legal:
                    legal = ten_k[key] if key in (ten_k.items or []) else None
                if legal:
                    break
            except Exception:
                pass
        if legal:
            legal_text = _safe_text(legal, 3000)
            if (legal_text and len(legal_text) > 80 and
                    not any(k in legal_text.lower()[:300] for k in
                            ['none', 'not applicable', 'no material', 'no pending'])):
                events.append(_make_10k_event('Legal', 'Legal Proceedings (10-K)', legal_text))
    except Exception:
        pass

    # 3. MD&A / Annual Guidance — ten_k.management_discussion
    try:
        mda = _safe_text(ten_k.management_discussion, 5000)
        if mda and len(mda) > 500:
            if any(k in mda.lower() for k in ['guidance', 'outlook', 'expect', 'anticipate',
                                               'fiscal year', 'forward-looking']):
                events.append(_make_10k_event('Earnings & Guidance', 'Annual Guidance (10-K MD&A)', mda))
    except Exception:
        pass

    # 4. Business overview — ten_k.business (strategic context)
    try:
        biz = _safe_text(ten_k.business, 3000)
        if biz and len(biz) > 300:
            events.append(_make_10k_event('Earnings & Guidance', '10-K Business Overview', biz))
    except Exception:
        pass

    # 5. Going concern — scan risk_factors + MD&A text
    try:
        combined = ' '.join([
            _safe_text(ten_k.risk_factors, 5000),
            _safe_text(ten_k.management_discussion, 5000),
        ])
        if 'going concern' in combined.lower():
            events.append(_make_10k_event('Auditor Changes', 'Going Concern Doubt (10-K)',
                                          'Going concern language detected in 10-K filing. ' + combined[:2000]))
    except Exception:
        pass

    return events


def process_20f_filing(filing, ticker: str, company_info: dict) -> list:
    """
    Process 20-F — Annual report for foreign private issuers (e.g. JD.com, Haleon).
    Equivalent to 10-K for SEC domestic filers. Extracts same 3 sections:
      - Item 3.D  Risk Factors (→ Risk Factors)
      - Item 8.A  Financial Statements / MD&A (→ Earnings & Guidance)
      - Item 4.B  Business Overview / legal proceedings (→ Legal if present)

    Also checks for going-concern language in the MD&A section.
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)

    try:
        twenty_f = safe_filing_obj(filing)
        if twenty_f is None:
            return events
    except Exception as e:
        print(f"    20-F obj() error: {e}")
        return events

    # 20-F item keys vary by filer; try multiple key formats
    specs = [
        (
            ['Item 3D', 'Item 3.D', '3D', '3.D', 'Item 3', '3'],
            'Risk Factors', '20-F Risk Factors',
            200, None
        ),
        (
            ['Item 5', '5', 'Item 5A', '5A'],
            'Earnings & Guidance', 'Annual Report - MD&A (20-F)',
            500,
            lambda t: any(k in t.lower() for k in ['revenue', 'results', 'fiscal', 'guidance',
                                                     'outlook', 'expect', 'million', 'billion'])
        ),
        (
            ['Item 8', '8', 'Item 8A', '8A'],
            'Earnings & Guidance', 'Annual Guidance (20-F MD&A)',
            500,
            lambda t: any(k in t.lower() for k in ['guidance', 'outlook', 'forward-looking',
                                                     'fiscal year', 'expect', 'anticipate'])
        ),
        (
            ['Item 4B', 'Item 4.B', '4B', '4.B'],
            'Legal', 'Legal Proceedings (20-F)',
            80,
            lambda t: not any(k in t.lower()[:300] for k in ['none', 'not applicable', 'no material'])
        ),
        # Going concern
        (
            ['Item 5', '5', 'Item 3D', '3D'],
            'Auditor Changes', 'Going Concern Doubt (20-F)',
            50,
            lambda t: 'going concern' in t.lower()
        ),
    ]

    seen = set()
    for keys, category, subtype, min_len, validator in specs:
        if subtype in seen:
            continue
        for key in keys:
            try:
                items = twenty_f.items or []
                if key not in items:
                    continue
                content = twenty_f[key]
                if not content or len(str(content)) < min_len:
                    continue
                if validator and not validator(str(content)):
                    continue
                seen.add(subtype)
                event = {
                    'company_id':     company_info.get('company_id'),
                    'ticker':         company_info.get('ticker', ticker),
                    'event_date':     filing_date,
                    'edgar_filing_date': filing_date,
                    'event_category': category,
                    'event_subtype':  subtype,
                    'headline':       f"Annual Report (20-F) - {subtype}",
                    'situation':      str(content)[:6000],
                    'source':         'SEC EDGAR 20-F',
                    'source_ref':     doc_url,
                }
                event['event_hash']   = compute_event_hash(event)
                event['etl_batch_id'] = ETL_BATCH_ID
                events.append(event)
                break
            except Exception:
                pass

    # If structured items failed, fall back to raw text extraction
    if not events:
        try:
            text = (filing.text() or '')[:15000]
            if text and len(text) > 500:
                llm_result = extract_with_llm(text, ticker, filing_date, '20-F Annual Report')
                situation = f"20-F Annual Report (foreign private issuer).\n\n{llm_result or text[:3000]}"
                event = {
                    'company_id':     company_info.get('company_id'),
                    'ticker':         company_info.get('ticker', ticker),
                    'event_date':     filing_date,
                    'edgar_filing_date': filing_date,
                    'event_category': 'Earnings & Guidance',
                    'event_subtype':  'Annual Report (20-F)',
                    'headline':       f"Annual Report (20-F) filed",
                    'situation':      situation[:30000],
                    'source':         'SEC EDGAR 20-F',
                    'source_ref':     doc_url,
                }
                event['event_hash']   = compute_event_hash(event)
                event['etl_batch_id'] = ETL_BATCH_ID
                events.append(event)
        except Exception:
            pass

    return events


def process_10q_filing(filing, ticker: str, company_info: dict, quarter_map: dict = None) -> list:
    """
    Process 10-Q.
    v4 adds:
      - Part I Item 2 (MD&A — quarterly guidance) — was missing in v3
      - Part II Item 2 (Unregistered equity sales) — was missing in v3
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)

    try:
        ten_q = safe_filing_obj(filing)
        if ten_q is None:
            return events
    except Exception as e:
        print(f"    10-Q obj() error: {e}")
        return events

    internal_url = _internal_filing_url(ticker, '10-Q', filing, quarter_map=quarter_map)

    def _make_10q_event(category, subtype, content_text):
        hdr = _standard_header(ticker, filing_date, '10-Q', subtype)
        event = {
            'company_id':     company_info.get('company_id'),
            'ticker':         company_info.get('ticker', ticker),
            'event_date':     filing_date,
            'edgar_filing_date': filing_date,
            'event_category': category,
            'event_subtype':  subtype,
            'headline':       f"Quarterly Report (10-Q) — {subtype}",
            'situation':      (hdr + str(content_text)[:29000])[:30000],
            'source':         'SEC EDGAR 10-Q',
            'source_ref':     internal_url,
        }
        event['event_hash']   = compute_event_hash(event)
        event['etl_batch_id'] = ETL_BATCH_ID
        return event

    # ── Use edgartools structured properties + item key fallback ─────────────

    # 1. Financials (income statement, balance sheet, cash flow) — structured
    try:
        fin_parts = []
        for prop_name, label in [('income_statement', 'Income Statement'),
                                  ('balance_sheet', 'Balance Sheet'),
                                  ('cash_flow_statement', 'Cash Flow')]:
            try:
                obj = getattr(ten_q, prop_name)
                t = _safe_text(obj, 1500)
                if t:
                    fin_parts.append(f"── {label} ──\n{t}")
            except Exception:
                pass
        if fin_parts:
            events.append(_make_10q_event('Earnings & Guidance', 'Quarterly Financials (10-Q)',
                                          '\n\n'.join(fin_parts)))
    except Exception:
        pass

    # 2. MD&A — try item keys (no dedicated property on TenQ)
    try:
        mda = None
        for key in ['Part I, Item 2', 'Part I Item 2', 'Item 2']:
            if key in (ten_q.items or []):
                mda = ten_q[key]
                break
        if mda:
            mda_text = _safe_text(mda, 4000)
            if (mda_text and len(mda_text) > 500 and
                    any(k in mda_text.lower() for k in ['guidance', 'outlook', 'expect', 'fiscal',
                                                          'quarter', 'forward', 'revenue', 'results'])):
                events.append(_make_10q_event('Earnings & Guidance', 'Quarterly Guidance (10-Q MD&A)', mda_text))
    except Exception:
        pass

    # 3. Legal Proceedings
    try:
        legal = None
        for key in ['Part II, Item 1', 'Part II Item 1']:
            if key in (ten_q.items or []):
                legal = ten_q[key]
                break
        if legal:
            legal_text = _safe_text(legal, 3000)
            if (legal_text and len(legal_text) > 80 and
                    not any(k in legal_text.lower()[:300] for k in
                            ['none', 'not applicable', 'no material', 'no pending'])):
                events.append(_make_10q_event('Legal', 'Legal Proceedings (10-Q)', legal_text))
    except Exception:
        pass

    # 4. Risk Factor updates
    try:
        for key in ['Part II, Item 1A', 'Part II Item 1A']:
            if key in (ten_q.items or []):
                rf = ten_q[key]
                rf_text = _safe_text(rf, 3000)
                if rf_text and len(rf_text) > 200:
                    events.append(_make_10q_event('Risk Factors', 'Risk Factor Update (10-Q)', rf_text))
                break
    except Exception:
        pass

    # 5. Unregistered Equity Sales
    try:
        for key in ['Part II, Item 2', 'Part II Item 2']:
            if key in (ten_q.items or []):
                eq = ten_q[key]
                eq_text = _safe_text(eq, 2000)
                if (eq_text and len(eq_text) > 80 and
                        not any(k in eq_text.lower()[:300] for k in ['none', 'not applicable'])):
                    events.append(_make_10q_event('Capital Raising', 'Unregistered Equity Sales (10-Q)', eq_text))
                break
    except Exception:
        pass

    return events


def process_proxy_filing(filing, ticker: str, company_info: dict) -> list:
    """
    Process DEF 14A.
    Shows XBRL-backed key metrics at top, then full proxy document text below.
    No LLM, no truncation — user gets the complete filing content.
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)

    header = _standard_header(ticker, filing_date, 'DEF 14A', 'Proxy Statement — Executive Compensation & Annual Meeting')

    # ── Step 1: Structured XBRL key metrics ──────────────────────────────────
    structured_lines = []
    try:
        proxy = ProxyStatement.from_filing(filing)
        if proxy is not None:
            if proxy.peo_name:
                structured_lines.append(f"CEO: {proxy.peo_name}")
            if proxy.peo_total_comp:
                structured_lines.append(f"CEO Total Compensation: ${proxy.peo_total_comp:,.0f}")
            if proxy.peo_actually_paid_comp:
                structured_lines.append(f"CEO Comp Actually Paid: ${proxy.peo_actually_paid_comp:,.0f}")
            if proxy.neo_avg_total_comp:
                structured_lines.append(f"NEO Avg Total Compensation: ${proxy.neo_avg_total_comp:,.0f}")
            if proxy.company_selected_measure:
                structured_lines.append(f"Company Performance Measure: {proxy.company_selected_measure}")
            if proxy.total_shareholder_return:
                structured_lines.append(f"Total Shareholder Return: {proxy.total_shareholder_return}")
            try:
                ec_df = proxy.executive_compensation
                if ec_df is not None and not ec_df.empty:
                    structured_lines.append("\nExecutive Compensation Table:")
                    structured_lines.append(ec_df.to_string(index=False))
            except Exception:
                pass
    except Exception:
        pass

    # ── Step 2: Full proxy document text via HTML download ───────────────────
    full_text = ''
    try:
        import httpx
        from bs4 import BeautifulSoup
        atts = filing.attachments
        primary_att = None
        for att in (atts.primary_documents if hasattr(atts, 'primary_documents') else []):
            if att.extension in ('.htm', '.html') and not att.is_report():
                primary_att = att
                break
        if primary_att is None:
            for att in atts.documents:
                if att.extension in ('.htm', '.html'):
                    primary_att = att
                    break
        if primary_att:
            html = httpx.get(
                primary_att.url,
                headers={'User-Agent': 'Coresight Research dev@coresight.com'},
                timeout=20,
            ).text
            soup = BeautifulSoup(html, 'lxml')
            for tag in soup(['script', 'style', 'head']):
                tag.decompose()
            raw = soup.get_text(separator='\n', strip=True)
            raw = re.sub(r'\n{3,}', '\n\n', raw)
            full_text = raw[:28000]
    except Exception:
        pass

    # Fallback to edgartools text method
    if not full_text:
        try:
            full_text = (filing.text() or '')[:28000]
        except Exception:
            pass

    # ── Build situation ───────────────────────────────────────────────────────
    parts = [header]
    if structured_lines:
        parts.append("── KEY METRICS (XBRL) ──────────────────────────────────\n")
        parts.append("\n".join(structured_lines))
        parts.append("\n\n── FULL DOCUMENT TEXT ──────────────────────────────────\n")
    if full_text:
        parts.append(full_text)
    else:
        parts.append("Proxy statement text unavailable — see source reference link.")

    situation = "".join(parts)

    event = {
        'company_id':     company_info.get('company_id'),
        'ticker':         company_info.get('ticker', ticker),
        'event_date':     filing_date,
        'edgar_filing_date': filing_date,
        'event_category': 'Proxy/Governance',
        'event_subtype':  'Executive Compensation (DEF 14A)',
        'headline':       'Proxy Statement - Annual Meeting & Executive Compensation',
        'situation':      situation[:30000],
        'source':         'SEC EDGAR DEF 14A',
        'source_ref':     doc_url,
    }
    event['event_hash']   = compute_event_hash(event)
    event['etl_batch_id'] = ETL_BATCH_ID
    events.append(event)
    return events


def process_13dg_filing(filing, ticker: str, company_info: dict) -> list:
    """
    Process Schedule 13D / 13G — Investor Activism / Institutional Ownership.
    Entirely new in v4 — v3 had zero coverage of this category.
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)
    form = filing.form

    is_activist = '13D' in form
    category = 'Investor Activism' if is_activist else 'Investor / Institutional Ownership'
    subtype_base = 'Activist Stake' if is_activist else 'Institutional Stake'
    if '/A' in form:
        subtype_base += ' (Amendment)'

    situation = ''
    headline_pct = ''

    try:
        schedule = (Schedule13D.from_filing(filing) if is_activist
                    else Schedule13G.from_filing(filing))
        if schedule is None:
            raise ValueError(f"{form} returned None")

        persons = schedule.reporting_persons or []
        names = ', '.join(
            p.name for p in persons if hasattr(p, 'name') and p.name
        ) or 'Unknown filer'

        total_pct   = getattr(schedule, 'total_percent', None)
        total_shares = getattr(schedule, 'total_shares', None)

        parts = [f"Form {form} filed by: {names}"]
        if total_pct:
            parts.append(f"Ownership percentage: {total_pct:.2f}%")
            headline_pct = f" ({total_pct:.1f}%)"
        if total_shares:
            parts.append(f"Total shares held: {total_shares:,}")

        # 13D Item 4 — Purpose of transaction (activist intent)
        if is_activist:
            try:
                purpose = (schedule.items.item4_purpose_of_transaction
                           if schedule.items else None)
                if purpose:
                    parts.append(f"\nPurpose of Transaction (Item 4):\n{purpose[:2000]}")
            except Exception:
                pass

        situation = '\n'.join(parts)

    except Exception as e:
        # Fallback to raw text
        try:
            situation = (filing.text() or '')[:2000]
        except Exception:
            situation = ''
        if not situation:
            situation = f"Form {form} beneficial ownership disclosure."

    event = {
        'company_id':     company_info.get('company_id'),
        'ticker':         company_info.get('ticker', ticker),
        'event_date':     filing_date,
        'edgar_filing_date': filing_date,
        'event_category': category,
        'event_subtype':  subtype_base,
        'headline':       f"{form} Beneficial Ownership Disclosure{headline_pct}",
        'situation':      situation[:30000],
        'source':         f'SEC EDGAR {form}',
        'source_ref':     doc_url,
    }
    event['event_hash']   = compute_event_hash(event)
    event['etl_batch_id'] = ETL_BATCH_ID
    events.append(event)
    return events


def process_offering_filing(filing, ticker: str, company_info: dict) -> list:
    """
    Process S-1/F-1/S-3/F-3/424B* offering filings.
    Entirely new in v4 — v3 had zero coverage of IPOs, shelf registrations, follow-ons.
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)
    form = filing.form

    if 'S-1' in form or 'F-1' in form:
        category, subtype = 'Capital Raising', 'IPO / Registration Filing'
    elif 'S-3' in form or 'F-3' in form:
        category, subtype = 'Capital Raising', 'Shelf Registration'
    elif '424B' in form:
        category, subtype = 'Capital Raising', 'Prospectus (Follow-on / Debt Offering)'
    else:
        category, subtype = 'Capital Raising', 'Securities Offering'

    try:
        text = (filing.text() or '')[:5000]
        amount_m = re.search(r'\$([\d,.]+)\s*(?:million|billion)', text, re.IGNORECASE)
        amount_info = f"Offering size: {amount_m.group(0)}" if amount_m else ''
        situation = f"Form {form} securities filing. {amount_info}\n\n{text[:2000]}".strip()
    except Exception:
        situation = f"Form {form} securities offering filing."

    event = {
        'company_id':     company_info.get('company_id'),
        'ticker':         company_info.get('ticker', ticker),
        'event_date':     filing_date,
        'edgar_filing_date': filing_date,
        'event_category': category,
        'event_subtype':  subtype,
        'headline':       f"{subtype} ({form})",
        'situation':      situation[:30000],
        'source':         f'SEC EDGAR {form}',
        'source_ref':     doc_url,
    }
    event['event_hash']   = compute_event_hash(event)
    event['etl_batch_id'] = ETL_BATCH_ID
    events.append(event)
    return events


def process_nt_filing(filing, ticker: str, company_info: dict) -> list:
    """
    Process NT 10-K / NT 10-Q — Delayed Filing Notifications.
    Entirely new in v4 — v3 had zero coverage.
    """
    events = []
    filing_date = filing.filing_date
    doc_url = get_primary_document_url(filing)
    form = filing.form

    filing_type = 'Annual Report (10-K)' if '10-K' in form else 'Quarterly Report (10-Q)'

    try:
        text = (filing.text() or '')[:3000]
        reason_m = re.search(r'(?:reason|unable|delay)[:\s]+(.{60,500})', text, re.IGNORECASE | re.DOTALL)
        reason = reason_m.group(1).strip()[:500] if reason_m else 'See NT filing for details.'
    except Exception:
        reason = 'See NT filing for details.'

    situation = (f"{ticker} filed {form} — unable to file {filing_type} on time.\n"
                 f"Reason: {reason}")

    event = {
        'company_id':     company_info.get('company_id'),
        'ticker':         company_info.get('ticker', ticker),
        'event_date':     filing_date,
        'edgar_filing_date': filing_date,
        'event_category': 'Earnings & Guidance',
        'event_subtype':  f'Delayed {filing_type}',
        'headline':       f"Delayed SEC Filing Notification ({form})",
        'situation':      situation[:30000],
        'source':         f'SEC EDGAR {form}',
        'source_ref':     doc_url,
    }
    event['event_hash']   = compute_event_hash(event)
    event['etl_batch_id'] = ETL_BATCH_ID
    events.append(event)
    return events


# ── News layer ────────────────────────────────────────────────────────────────

def _news_is_relevant(title: str, summary: str, company_info: dict) -> bool:
    """
    Relevance gate: verify the article is actually about the target company.

    Problem: News DB tables tag articles by ticker, but ticker symbols are
    often ambiguous across exchanges (e.g. 'AD' = Ahold Delhaize AND Array
    Digital Infrastructure). This causes false positives where unrelated
    company articles appear under the target ticker.

    Solution: require that at least one name variant (ticker or company name
    keyword) appears in the article title or summary.

    For SEC companies with unambiguous US tickers (like 'M', 'AMZN') this
    check almost always passes because the ticker itself appears in the article.
    For non-SEC / international tickers the check prevents cross-contamination.

    Returns True (keep) / False (discard).
    """
    ticker_str = company_info.get('ticker', '').lower()
    variants   = company_info.get('name_variants', [ticker_str])
    if not variants:
        return True  # no info → keep (don't discard)

    # Normalize text and variants identically so spelling variants match:
    #   "Wal-Mart"   → "walmart"   matches variant "walmart"
    #   "JD.com"     → "jd"        matches variant "jd"
    #   "Couche-Tard"→ "couchetard" matches variant "couche-tard" → "couchetard"
    raw_text = (title + ' ' + (summary or '')).lower()
    text = raw_text.replace('.com', '').replace('-', '')
    norm_variants = [v.replace('.com', '').replace('-', '') for v in variants]

    # For very short tickers (≤2 chars) the ticker alone is too ambiguous as a
    # substring match (e.g. 'ad' matches 'added', 'advanced', 'advisor').
    # Require at least one NAME variant (not just the ticker) to match.
    if len(ticker_str) <= 2:
        name_variants = [v for v in norm_variants if v != ticker_str]
        if name_variants:
            return any(v in text for v in name_variants)
        # No name variants at all — fall back to ticker whole-word match
        # Use normalized text (hyphens/dots removed) so "jd.com" → "jd" matches
        return ticker_str in text.split() or ticker_str in raw_text.split()

    return any(v in text for v in norm_variants)

def get_news(ticker: str, start_date: str, company_info: dict,
             end_date: str = None) -> list:
    """
    Pull news from DB.
    v3 bugs fixed:
      1. SQL injection: ticker is now a parameterized bind variable
      2. start_date was ignored (hardcoded 12 months) — now properly used
      3. Keywords expanded to cover all docx categories
    """
    events = []
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # ── SQL keyword filter: cast wide net on title OR summary ─────────────────
        # Rules for keyword design:
        #   1. Specific enough to eliminate obvious noise (no bare '%profit%' — too broad)
        #   2. Match real PR Newswire/BusinessWire/GlobeNewswire headline patterns
        #   3. Both title AND summary are checked so no event is missed
        keywords = [
            # ── Earnings / Results ─────────────────────────────────────────────
            '%reports % results%',       # "Reports Q1 2025 Results"
            '%reports % earnings%',      # "Reports Fourth Quarter Earnings"
            '%announces % results%',     # "Announces Financial Results"
            '%financial results%',
            '%quarterly results%',
            '%annual results%',
            '%quarterly earnings%',
            '%full year results%',
            '%full-year results%',
            '%fourth quarter%',
            '%third quarter%',
            '%second quarter%',
            '%first quarter%',
            '%q1 20%', '%q2 20%', '%q3 20%', '%q4 20%',
            # ── Guidance / Outlook ────────────────────────────────────────────
            '%raises guidance%',
            '%lowers guidance%',
            '%updates guidance%',
            '%reaffirms guidance%',
            '%full year guidance%',
            '%full-year guidance%',
            '%raises full year%',
            '%lowers full year%',
            '%raises outlook%',
            '%lowers outlook%',
            '%updates outlook%',
            '%fiscal year outlook%',
            '%guidance for 20%',
            # ── Management Changes ─────────────────────────────────────────────
            '%chief executive officer%',
            '%chief financial officer%',
            '%chief operating officer%',
            '% ceo %',     '% cfo %',    '% coo %',
            '%appoints %',
            '%names % as %',
            '%announces % as ceo%', '%announces % as cfo%',
            '%ceo transition%', '%cfo transition%',
            '%ceo resignation%', '%cfo resignation%',
            '%steps down as%',
            '%departs as%',
            '%transition to%ceo%', '%transition to%cfo%',
            # ── M&A ──────────────────────────────────────────────────────────
            '%to acquire%',
            '%completes acquisition%',
            '%announces acquisition%',
            '%acquisition of%',
            '%agrees to acquire%',
            '%merger with%',
            '%completes merger%',
            '%announces merger%',
            '%stockholders approve merger%',
            '%divests%',
            '%divestiture of%',
            '%completes sale of%',
            '%announces sale of%',
            '%spin-off%',         '%spinoff%',
            '%announces spin%',
            '%strategic alternative%',
            '%exploring strategic%',
            '%sale process%',
            '%go-private%',
            # ── Capital Returns ───────────────────────────────────────────────
            '%declares dividend%',
            '%declares quarterly dividend%',
            '%increases quarterly dividend%',
            '%raises quarterly dividend%',
            '%increases dividend%',
            '%raises dividend%',
            '%dividend increase%',
            '%dividend cut%',
            '%suspends dividend%',
            '%eliminates dividend%',
            '%special dividend%',
            '%stock split%',
            '%reverse stock split%',
            '%share repurchase program%',
            '%share repurchase authorization%',
            '%buyback program%',
            '%repurchase authorization%',
            '%authorizes % repurchase%',
            # ── Layoffs / Restructuring ───────────────────────────────────────
            '%layoff%', '%lay off%',
            '%job cut%', '%job cuts%',
            '%workforce reduction%',
            '%reduces workforce%',
            '%eliminates % position%',
            '%eliminates % job%',
            '%restructuring plan%',
            '%restructuring charge%',
            '%reorganization plan%',
            '%cost reduction plan%',
            # ── Bankruptcy / Distress ─────────────────────────────────────────
            '%chapter 11%',
            '%chapter 7%',
            '%files for bankruptcy%',
            '%bankruptcy filing%',
            '%bankruptcy protection%',
            '%dip financing%',
            '%debtor in possession%',
            '%voluntary chapter%',
            '%emerges from bankruptcy%',
            '%exit from bankruptcy%',
            '%liquidation plan%',
            # ── Legal / Regulatory ────────────────────────────────────────────
            '%settlement of%',
            '%agrees to settle%',
            '%settles % lawsuit%',
            '%settles % litigation%',
            '%class action%',
            '%sec investigation%',
            '%ftc investigation%',
            '%doj investigation%',
            '%regulatory settlement%',
            '%sec action%',
            '%ftc action%',
            '%cease and desist%',
            '%consent order%',
            '%securities litigation%',
            # ── Credit / Ratings ──────────────────────────────────────────────
            '%moody%upgrade%', '%moody%downgrade%',
            "% s&p %upgrade%", "% s&p %downgrade%",
            '%fitch%upgrade%', '%fitch%downgrade%',
            '%credit rating upgrade%',
            '%credit rating downgrade%',
            '%investment grade rating%',
            '%rating action%',
            '%creditwatch%',
            '%watch negative%',
            '%watch positive%',
            '%outlook negative%',
            '%outlook positive%',
            '%outlook stable%',
            # ── Impairments / Write-offs ──────────────────────────────────────
            '%goodwill impairment%',
            '%impairment charge%',
            '%impairment of%',
            '%write-off%', '%writedown%', '%write down%',
            '%asset write%',
            # ── Activist / Governance ─────────────────────────────────────────
            '%activist investor%',
            '%activist stake%',
            '%proxy fight%',
            '%hostile bid%',
            '%going concern%',
            '%shareholder vote%',
            '%stockholder vote%',
            '%board of directors%',
            '%board member%',
            # ── Trading / Listing ─────────────────────────────────────────────
            '%delisting%', '%delist%',
            '%ticker change%',
            '%name change%',
            '%added to s&p%',
            '%removed from s&p%',
            '%index inclusion%',
            '%index exclusion%',
            '%joins s&p%',
            # ── Restatements ─────────────────────────────────────────────────
            '%restatement%',
            '%restates %financial%',
            '%non-reliance%',
            # ── Store Operations (retail) ─────────────────────────────────────
            '%store closure%',
            '%store closing%',
            '%closes % store%',
            '%store opening%',
            '%opens % store%',
            '%store expansion%',
            '%new store%',
            '%location opening%',
            # ── Strategic Partnerships / JV ──────────────────────────────────
            '%joint venture%',
            '%strategic partnership%',
            '%strategic alliance%',
            '%forms partnership%',
            '%announces partnership%',
            # ── Debt Financing ────────────────────────────────────────────────
            '%senior notes%',
            '%notes offering%',
            '%debt offering%',
            '%bond offering%',
            '%credit facility%',
            '%term loan%',
            '%revolving credit%',
            # ── Investor Day / IR Events ──────────────────────────────────────
            '%investor day%',
            '%analyst day%',
            '%investor conference%',
        ]

        # Match on BOTH title AND summary so nothing is missed
        like_title   = ' OR '.join(['title LIKE %s']   * len(keywords))
        like_summary = ' OR '.join(['summary LIKE %s'] * len(keywords))
        like_clause  = f"({like_title}) OR ({like_summary})"

        # Build date filters and params list together so they always stay in sync
        date_filters = ['time_published_utc >= %s']
        date_params  = [ticker, start_date]
        if end_date:
            date_filters.append('time_published_utc <= %s')
            date_params.append(end_date)

        query = f"""
            SELECT time_published_utc, title, summary, source_name,
                   overall_sentiment_label, url
            FROM coreiq_av_market_news_sentiment
            WHERE ticker = %s
              AND {' AND '.join(date_filters)}
              AND ({like_clause})
            ORDER BY time_published_utc DESC
        """

        # params = ticker + date params + title keywords + summary keywords
        params = date_params + keywords + keywords
        cursor.execute(query, params)

        filtered_out = 0
        for row in cursor.fetchall():
            date_val    = row[0].strftime('%Y-%m-%d') if row[0] else 'Unknown'
            title       = row[1] or 'No Title'
            summary     = row[2] or title
            source_name = row[3] or ''
            sentiment   = row[4] or 'Neutral'
            url         = row[5] if len(row) > 5 else None

            # Relevance gate — discard articles about other companies sharing this ticker symbol
            if not _news_is_relevant(title, summary, company_info):
                filtered_out += 1
                continue

            category, subtype = _classify_news(title)

            # Use actual publisher name for traceability
            source_label = f"AV News — {source_name}" if source_name else "AV News"

            event = {
                'company_id':     company_info.get('company_id'),
                'ticker':         company_info.get('ticker', ticker),
                'event_date':     date_val,
                'event_category': category,
                'event_subtype':  subtype,
                'headline':       title,
                'situation':      f"{summary}\n\nSentiment: {sentiment}",
                'source':         source_label,
                'source_ref':     url,
            }
            event['event_hash']   = compute_event_hash(event)
            event['etl_batch_id'] = ETL_BATCH_ID
            events.append(event)

        if filtered_out:
            print(f"    Relevance filter: discarded {filtered_out} articles about other companies")
        conn.close()
    except Exception as e:
        print(f"    News error: {e}")

    return events


def _classify_news(title: str) -> tuple[str, str]:
    """
    Keyword-based news categorization.

    Design principles (based on real PR Newswire/BusinessWire/GlobeNewswire patterns):
      1. Most-specific rules first — prevents broad terms from over-capturing
      2. Multi-word phrases preferred over single words to reduce false positives
      3. False-positive guards: bare words like 'deal', 'profit', 'upgrade' are
         only used in compound checks, not standalone
      4. Verb patterns match how companies actually title press releases:
         "Reports", "Announces", "Raises/Lowers", "Completes", "Declares",
         "Authorizes", "Files", "Settles", "Appoints/Names", "Steps Down"
    """
    t = title.lower()

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 1 — HIGH PRECISION: Rare events with unmistakable vocabulary
    # ══════════════════════════════════════════════════════════════════════════

    # ── Bankruptcy / Distress ─────────────────────────────────────────────────
    if any(p in t for p in ['chapter 11', 'chapter 7', 'files for bankruptcy',
                             'bankruptcy filing', 'bankruptcy protection',
                             'voluntary chapter', 'debtor in possession',
                             'dip financing', 'liquidation plan']):
        if any(p in t for p in ['emerges from', 'exit from bankruptcy', 'emerged from',
                                 'exit bankruptcy', 'plan confirmed', 'plan of reorganization']):
            return 'Bankruptcy', 'Bankruptcy Emergence/Exit'
        if any(p in t for p in ['dip financing', 'debtor in possession']):
            return 'Bankruptcy', 'Bankruptcy DIP Financing'
        if any(p in t for p in ['asset sale', 'liquidation', 'liquidating']):
            return 'Bankruptcy', 'Bankruptcy Asset Sale/Liquidation'
        return 'Bankruptcy', 'Distress News'

    # ── Restatements ─────────────────────────────────────────────────────────
    if any(p in t for p in ['restatement', 'restates financial', 'restated financial',
                             'non-reliance', 'restate its']):
        return 'Restatements', 'Earnings Restatement'

    # ── Trading / Listing ─────────────────────────────────────────────────────
    if any(p in t for p in ['delisting notice', 'receives delisting', 'delisted from',
                             'delist from', 'noncompliance notice']):
        return 'Trading/Listing', 'Delisting News'
    if any(p in t for p in ['ticker change', 'ticker symbol change', 'trading symbol',
                             'name change', 'changes its name', 'rebrands to',
                             'formerly known as']):
        return 'Trading/Listing', 'Name/Ticker Change'
    # Index changes — must precede S&P credit/ratings check
    if any(p in t for p in ['added to s&p', 'removed from s&p', 'joins s&p',
                             'leaves s&p', 's&p 500 addition', 's&p 500 removal',
                             'index inclusion', 'index exclusion',
                             'added to the index', 'dropped from the index',
                             'removed from the index']):
        return 'Trading/Listing', 'Index Constituent Change'

    # ── Auditor / Going Concern ───────────────────────────────────────────────
    if any(p in t for p in ['going concern', 'doubt about', 'ability to continue',
                             'substantial doubt', 'auditor resignation',
                             'change of auditor', 'changes auditor']):
        return 'Auditor Changes', 'Going Concern'

    # ── Credit / Ratings ─────────────────────────────────────────────────────
    # Watch/Outlook — more specific, check before bare upgrade/downgrade
    if any(p in t for p in ['creditwatch', 'credit watch', 'watch negative',
                             'watch positive', 'on review for', 'placed on watch']):
        return 'Credit/Ratings', 'Rating Watch/Outlook'
    if any(p in t for p in ['outlook negative', 'outlook positive', 'outlook stable',
                             'outlook revised', 'revises outlook to']):
        return 'Credit/Ratings', 'Rating Watch/Outlook'
    # Rating action — require agency name to avoid equity analyst upgrade/downgrade false positives
    if any(p in t for p in ["moody's", "moody's ratings", 's&p global ratings',
                             "standard & poor's", 'fitch ratings', 'fitch downgrades',
                             'fitch upgrades', 'credit rating upgrade', 'credit rating downgrade',
                             'investment grade rating', 'rating action', 'rated by moody',
                             'rated by s&p', 'rated by fitch']):
        return 'Credit/Ratings', 'Rating Action'

    # ── Impairments ───────────────────────────────────────────────────────────
    if any(p in t for p in ['goodwill impairment', 'impairment charge', 'impairment of assets',
                             'records impairment', 'impairment loss',
                             'write-down of', 'writedown of', 'write down of',
                             'asset write-off', 'write-off of']):
        return 'Impairments', 'Impairment News'

    # ── Investor Activism ─────────────────────────────────────────────────────
    if any(p in t for p in ['activist investor', 'activist stake', 'proxy fight',
                             'hostile bid', 'hostile takeover', 'board fight',
                             'shareholder fight', 'campaigns against',
                             'demands board', 'demands ceo']):
        return 'Investor Activism', 'Activist Communication'

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 2 — HIGH VALUE CORPORATE EVENTS: Specific multi-word patterns
    # ══════════════════════════════════════════════════════════════════════════

    # ── Layoffs / Restructuring ───────────────────────────────────────────────
    if any(p in t for p in ['layoff', 'lay off', 'job cut', 'job cuts',
                             'workforce reduction', 'reduces workforce',
                             'eliminates positions', 'eliminates jobs',
                             'headcount reduction']):
        return 'Layoffs & Restructuring', 'Headcount Reduction'
    if any(p in t for p in ['restructuring plan', 'restructuring charge',
                             'restructuring initiative', 'reorganization plan',
                             'cost reduction plan', 'cost-reduction plan',
                             'announces restructuring', 'initiates restructuring']):
        return 'Layoffs & Restructuring', 'Restructuring'

    # ── Legal / Regulatory ────────────────────────────────────────────────────
    if any(p in t for p in ['sec investigation', 'ftc investigation', 'doj investigation',
                             'regulatory settlement', 'cease and desist',
                             'consent order', 'regulatory fine', 'regulatory penalty',
                             'sec enforcement', 'ftc action', 'ftc order']):
        return 'Legal', 'Regulatory Enforcement'
    if any(p in t for p in ['settlement of', 'agrees to settle', 'settles lawsuit',
                             'settles litigation', 'class action', 'class-action',
                             'securities litigation', 'subpoena', 'patent infringement',
                             'settles securities']):
        return 'Legal', 'Legal News'

    # ── Management Changes ────────────────────────────────────────────────────
    # Use full titles and action verbs — avoids false positives from quotes/analysis
    if any(p in t for p in ['chief executive officer', 'chief financial officer',
                             'chief operating officer']):
        return 'Management Changes', 'Executive News'
    # Short forms with action verbs only (avoids bare 'ceo' in analyst article titles)
    if any(p in t for p in ['appoints ', 'names ', 'announces ']):
        if any(p in t for p in [' ceo', ' cfo', ' coo', ' president', ' chair']):
            return 'Management Changes', 'Executive News'
    if any(p in t for p in ['ceo transition', 'cfo transition', 'ceo resignation',
                             'cfo resignation', 'ceo departure', 'cfo departure',
                             'steps down as ceo', 'steps down as cfo',
                             'departs as ceo', 'departs as cfo',
                             'new ceo', 'new cfo', 'interim ceo', 'interim cfo',
                             'ceo to retire', 'cfo to retire']):
        return 'Management Changes', 'Executive News'

    # ── Capital Returns ───────────────────────────────────────────────────────
    # Stock splits — high signal
    if any(p in t for p in ['stock split', 'forward stock split', 'reverse stock split',
                             'reverse split']):
        return 'Capital Returns', 'Stock Split'
    # Buybacks — require "program" or "authorization" to avoid false positives
    if any(p in t for p in ['share repurchase program', 'share repurchase authorization',
                             'of share repurchases',    # "Authorizes $4B of Share Repurchases"
                             'buyback program', 'repurchase authorization',
                             'authorizes repurchase', 'authorizes buyback',
                             'increases buyback', 'new repurchase program']):
        return 'Capital Returns', 'Buyback Announcement'
    # Dividends — specific declaration/change patterns
    if any(p in t for p in ['declares dividend', 'declares quarterly dividend',
                             'declares special dividend', 'declares cash dividend']):
        if 'special' in t or 'one-time' in t or 'extra' in t:
            return 'Capital Returns', 'Special Dividend'
        return 'Capital Returns', 'Dividend Affirmation'
    if any(p in t for p in ['increases quarterly dividend', 'raises quarterly dividend',
                             'increases dividend', 'raises dividend', 'boosts dividend',
                             'hikes dividend', 'dividend increase', 'dividend hike',
                             'consecutive annual increase', 'annual dividend increase']):
        return 'Capital Returns', 'Dividend Increase'
    if any(p in t for p in ['cuts dividend', 'reduces dividend', 'lowers dividend',
                             'slashes dividend', 'dividend cut', 'dividend reduction']):
        return 'Capital Returns', 'Dividend Decrease'
    if any(p in t for p in ['suspends dividend', 'eliminates dividend', 'halts dividend',
                             'pauses dividend', 'omits dividend', 'dividend suspended',
                             'dividend eliminated']):
        return 'Capital Returns', 'Dividend Suspension'
    if any(p in t for p in ['initiates dividend', 'first dividend', 'new dividend',
                             'begins dividend', 'introduces dividend']):
        return 'Capital Returns', 'Dividend Initiation'
    # Catch-all dividend — only if "dividend" is the main subject + no misdirection
    if 'dividend' in t and not any(exc in t for exc in ['dividend yield', 'dividend stock',
                                                          'dividend investor', 'dividend growth',
                                                          'dividend etf', 'dividend portfolio',
                                                          'best dividend', 'top dividend']):
        return 'Capital Returns', 'Dividend Affirmation'

    # ── M&A ───────────────────────────────────────────────────────────────────
    if any(p in t for p in ['strategic alternative', 'exploring strategic',
                             'strategic review', 'sale process', 'go-private',
                             'going private']):
        return 'M&A Activity', 'Strategic Alternatives'
    if any(p in t for p in ['spin-off', 'spinoff', 'spin off', 'spinout',
                             'separation of', 'carve-out', 'announces spin']):
        return 'M&A Activity', 'Spin-Off/Separation'
    if any(p in t for p in ['divests', 'divestiture of', 'completes sale of',
                             'announces sale of', 'completes divestiture',
                             'divests its', 'sells its']):
        if not any(exc in t for exc in ['sells its stores', 'sells its products']):
            return 'M&A Activity', 'Divestiture/Asset Sale'
    if any(p in t for p in ['joint venture', 'strategic partnership',
                             'strategic alliance', 'forms partnership',
                             'announces partnership', 'enters partnership',
                             'collaboration agreement']):
        return 'M&A Activity', 'Strategic Alliance/Partnership'
    if any(p in t for p in ['to acquire ', 'agrees to acquire', 'completes acquisition',
                             'announces acquisition', 'acquisition of ',
                             'completes merger', 'announces merger', 'merger with ',
                             'stockholders approve merger', 'closes acquisition']):
        return 'M&A Activity', 'Deal News'

    # ── Store Operations (retail) ─────────────────────────────────────────────
    _is_store_closure = (
        any(p in t for p in ['store closure', 'store closures', 'store closing',
                             'closes stores', 'closes its stores', 'closing stores',
                             'closes all ', 'closes remaining', 'closure of all',
                             'permanent closure', 'permanent closures'])
        or ('clos' in t and any(p in t for p in ['store', 'location', 'outlet', 'branch']))
    )
    if _is_store_closure:
        return 'Store Operations', 'Store Closure'
    if any(p in t for p in ['store opening', 'opens new store', 'opens its ',
                             'grand opening', 'new store opening', 'store expansion',
                             'announces opening', 'opens first store', 'first location']):
        return 'Store Operations', 'Store Opening'

    # ── Debt Financing ────────────────────────────────────────────────────────
    if any(p in t for p in ['senior notes', 'notes offering', 'debt offering',
                             'bond offering', 'prices offering', 'closes offering',
                             'term loan', 'credit facility', 'revolving credit',
                             'pricing of notes', 'closes notes', 'senior unsecured']):
        return 'Capital Raising', 'Debt Financing'

    # ── IR / Events ───────────────────────────────────────────────────────────
    if any(p in t for p in ['investor day', 'analyst day', 'investor conference',
                             'capital markets day', 'capital markets event']):
        return 'Other Events', 'IR Event/Conference'

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 3 — EARNINGS & GUIDANCE: High volume, needs precise patterns
    # Uses specific verb+noun combos from actual press release title patterns
    # ══════════════════════════════════════════════════════════════════════════

    # Guidance changes — must check BEFORE bare 'earnings' pattern
    if any(p in t for p in ['raises guidance', 'lowers guidance', 'updates guidance',
                             'reaffirms guidance', 'raises full year', 'lowers full year',
                             'raises its guidance', 'lowers its guidance',
                             'raises fiscal', 'lowers fiscal', 'updates fiscal',
                             'raises 20', 'lowers 20',         # "raises 2025 guidance"
                             'raises outlook', 'lowers outlook', 'updates outlook',
                             'full year guidance', 'full-year guidance',
                             'guidance for 20']):
        return 'Earnings & Guidance', 'Guidance Update'

    # Earnings results — use "reports X results" / "announces X results" patterns
    if any(p in t for p in ['reports first quarter', 'reports second quarter',
                             'reports third quarter', 'reports fourth quarter',
                             'reports q1', 'reports q2', 'reports q3', 'reports q4',
                             'reports full year', 'reports full-year',
                             'reports annual', 'reports fiscal',
                             'announces first quarter', 'announces second quarter',
                             'announces third quarter', 'announces fourth quarter',
                             'quarterly results', 'quarterly earnings',
                             'annual results', 'full year results', 'full-year results',
                             'financial results', 'operating results',
                             'fourth quarter results', 'third quarter results',
                             'second quarter results', 'first quarter results',
                             'q1 results', 'q2 results', 'q3 results', 'q4 results',
                             'q1 earnings', 'q2 earnings', 'q3 earnings', 'q4 earnings',
                             'fourth quarter earnings', 'third quarter earnings',
                             'quarterly sales', 'annual sales']):
        return 'Earnings & Guidance', 'Earnings News'

    # ══════════════════════════════════════════════════════════════════════════
    # TIER 4 — BROAD FALLBACKS: Use only with strong multi-word anchors
    # ══════════════════════════════════════════════════════════════════════════

    # M&A fallback — only with "acquisition" or "merger" (not bare "deal")
    if any(p in t for p in ['acquisition', 'merger', 'takeover bid', 'tender offer']):
        return 'M&A Activity', 'Deal News'

    # Restructuring fallback
    if any(p in t for p in ['restructuring', 'reorganization', 'turnaround plan']):
        return 'Layoffs & Restructuring', 'Restructuring'

    # Legal fallback — only with legal nouns, not bare "investigation"
    if any(p in t for p in ['lawsuit', 'litigation', 'legal settlement',
                             'court ruling', 'court order', 'judgment against',
                             'arbitration award']):
        return 'Legal', 'Legal News'

    return 'General News', 'General'


# ── DB write helpers ──────────────────────────────────────────────────────────

def _get_db_conn():
    """Return a fresh pymysql connection."""
    return pymysql.connect(**DB_CONFIG)


def ensure_partitions(end_date: datetime):
    """
    Auto-create monthly RANGE COLUMNS partitions in coreiq_company_events for any
    month not yet partitioned (i.e. absorbed by pfuture).

    The table's last explicit partition is p202605 (< 2026-06-01).  When we run
    for data that extends into later months we need to REORGANIZE pfuture so the
    new month gets its own partition and pfuture is re-created beyond it.

    Safe to call multiple times — it only acts if the month is actually missing.
    """
    try:
        conn = _get_db_conn()
        cur  = conn.cursor()

        # Fetch existing partition upper bounds
        cur.execute("""
            SELECT PARTITION_NAME, PARTITION_DESCRIPTION
            FROM   information_schema.PARTITIONS
            WHERE  TABLE_SCHEMA = %s
              AND  TABLE_NAME   = 'coreiq_company_events'
              AND  PARTITION_NAME != 'pfuture'
            ORDER  BY PARTITION_ORDINAL_POSITION
        """, (DB_CONFIG['database'],))
        rows = cur.fetchall()

        existing_bounds = set()
        for name, desc in rows:
            # PARTITION_DESCRIPTION for RANGE COLUMNS is the literal value string
            try:
                existing_bounds.add(desc.strip("'"))
            except Exception:
                pass

        # Walk month by month from 2024-01 up to the month AFTER end_date
        cursor_date = datetime(2024, 1, 1)
        target_end  = datetime(end_date.year, end_date.month, 1) + timedelta(days=32)
        target_end  = target_end.replace(day=1)

        while cursor_date < target_end:
            next_month = (cursor_date.replace(day=1) + timedelta(days=32)).replace(day=1)
            bound_str  = next_month.strftime('%Y-%m-%d')

            if bound_str not in existing_bounds:
                part_name = cursor_date.strftime('p%Y%m')
                sql = f"""
                    ALTER TABLE coreiq_company_events
                    REORGANIZE PARTITION pfuture INTO (
                        PARTITION {part_name} VALUES LESS THAN ('{bound_str}'),
                        PARTITION pfuture VALUES LESS THAN (MAXVALUE)
                    )
                """
                try:
                    cur.execute(sql)
                    conn.commit()
                    print(f"  ✓ Created partition {part_name} (< {bound_str})")
                except Exception as pe:
                    # Partition may already exist (race condition or duplicate call)
                    if 'already exists' not in str(pe).lower():
                        print(f"  ⚠ Partition {part_name} error: {pe}")

            cursor_date = next_month

        conn.close()
    except Exception as e:
        print(f"  ⚠ ensure_partitions error (non-fatal): {e}")


def delete_ticker_events(ticker: str) -> int:
    """
    Hard-delete all events for a ticker from coreiq_company_events.
    Used for clean re-ingestion during testing.
    Returns number of rows deleted.
    """
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM coreiq_company_events WHERE ticker = %s",
            (ticker,)
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def push_to_db(df: pd.DataFrame) -> tuple[int, int]:
    """
    Incrementally load events into coreiq_company_events.

    Dedup strategy: INSERT IGNORE on UNIQUE KEY (event_hash, event_date).
    - event already in DB   → silently skipped (IGNORE)
    - event not yet in DB   → inserted

    Returns (inserted_count, skipped_count).
    """
    if df.empty:
        return 0, 0

    insert_sql = """
        INSERT IGNORE INTO coreiq_company_events
            (company_id, ticker, event_date, edgar_filing_date,
             event_category, event_subtype,
             headline, situation, source, source_ref,
             confidence, etl_batch_id, event_hash, is_active)
        VALUES
            (%s, %s, %s, %s,
             %s, %s,
             %s, %s, %s, %s,
             0.80, %s, %s, 1)
    """

    rows_to_insert = []
    for _, row in df.iterrows():
        event_date = row.get('event_date')
        if pd.isnull(event_date):
            continue  # skip rows with no date — can't satisfy NOT NULL
        if isinstance(event_date, pd.Timestamp):
            event_date = event_date.date()

        # edgar_filing_date: actual SEC submission date for EDGAR events, NULL for news
        edgar_filing_date = row.get('edgar_filing_date')
        if edgar_filing_date is not None and not pd.isnull(edgar_filing_date):
            if isinstance(edgar_filing_date, pd.Timestamp):
                edgar_filing_date = edgar_filing_date.date()
        else:
            edgar_filing_date = None

        rows_to_insert.append((
            row.get('company_id'),
            str(row.get('ticker', ''))[:20],
            event_date,
            edgar_filing_date,
            str(row.get('event_category', ''))[:50],
            str(row.get('event_subtype', '') or '')[:100],
            str(row.get('headline', '') or '')[:500],
            str(row.get('situation', '') or '')[:65535],
            str(row.get('source', '') or '')[:20],
            str(row.get('source_ref', '') or '')[:2000] if row.get('source_ref') else None,
            str(row.get('etl_batch_id', ETL_BATCH_ID)),
            str(row.get('event_hash', '')),
        ))

    if not rows_to_insert:
        return 0, 0

    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.executemany(insert_sql, rows_to_insert)
        inserted = cur.rowcount  # rows actually inserted (IGNORE suppresses dupes)
        conn.commit()
        skipped = len(rows_to_insert) - inserted
        return inserted, skipped
    finally:
        conn.close()


def _load_tickers_from_table(table: str, ticker_col: str = 'ticker',
                              where: str = None) -> list[str]:
    """
    Load a list of tickers from any DB table.

    Examples:
      _load_tickers_from_table('coreiq_companies')
      _load_tickers_from_table('coreiq_companies', where='is_active=1 AND sector="Retail"')
      _load_tickers_from_table('my_watchlist_table', ticker_col='stock_symbol')

    Returns a deduplicated, sorted list of uppercase tickers with None/empty filtered out.
    Only the ticker column is read — no other columns are touched.
    """
    try:
        # Whitelist table and column name characters to prevent SQL injection
        # (these are identifiers, not values, so %s parameterisation doesn't apply)
        safe_table  = re.sub(r'[^\w]', '', table)
        safe_col    = re.sub(r'[^\w]', '', ticker_col)
        if not safe_table or not safe_col:
            raise ValueError(f"Invalid table '{table}' or column '{ticker_col}'")

        where_clause = f'WHERE {where}' if where else ''
        sql = f'SELECT DISTINCT `{safe_col}` FROM `{safe_table}` {where_clause} ORDER BY `{safe_col}`'

        conn = _get_db_conn()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
        finally:
            conn.close()

        tickers = [str(r[0]).strip().upper() for r in rows if r[0]]
        return [t for t in tickers if t]  # remove any remaining empty strings

    except Exception as e:
        print(f"  ❌ _load_tickers_from_table error: {e}")
        return []


def get_db_event_count(ticker: str) -> int:
    """Return total active event rows in DB for a ticker."""
    try:
        conn = _get_db_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM coreiq_company_events WHERE ticker = %s AND is_active = 1",
            (ticker,)
        )
        result = cur.fetchone()
        conn.close()
        return result[0] if result else 0
    except Exception:
        return -1


# ── YF News layer ─────────────────────────────────────────────────────────────

def get_yf_news(ticker: str, start_date: str, company_info: dict,
                end_date: str = None) -> list:
    """
    Pull news from coreiq_yf_market_news_sentiment (Yahoo Finance news feed).

    Why this table in addition to AV news:
      - 494k rows vs AV's 1.8M — different publisher set (Yahoo Finance ecosystem)
      - Has primary_topic_v2 pre-classification (ML-tagged: earnings, retail_wholesale,
        financial_markets, economy_macro, technology, etc.) — used to boost classification
      - Has payload_json with content.summary (article body text, not just title)
      - Covers non-US and international retailers (uses yf_symbol like CA.PA)

    Dedup: same _classify_news() + compute_event_hash() as AV news, so duplicates
    between AV and YF on the same headline are naturally deduplicated by INSERT IGNORE.
    """
    import json as _json
    events = []
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # Same keyword set as AV news for consistency —
        # also check payload_json body text via the summary/description fields
        keywords = [
            '%reports % results%', '%reports % earnings%', '%announces % results%',
            '%financial results%', '%quarterly results%', '%annual results%',
            '%quarterly earnings%', '%full year results%', '%full-year results%',
            '%fourth quarter%', '%third quarter%', '%second quarter%', '%first quarter%',
            '%q1 20%', '%q2 20%', '%q3 20%', '%q4 20%',
            '%raises guidance%', '%lowers guidance%', '%updates guidance%',
            '%reaffirms guidance%', '%full year guidance%', '%full-year guidance%',
            '%raises full year%', '%lowers full year%', '%raises outlook%',
            '%lowers outlook%', '%updates outlook%', '%fiscal year outlook%',
            '%guidance for 20%',
            '%chief executive officer%', '%chief financial officer%', '%chief operating officer%',
            '% ceo %', '% cfo %', '% coo %',
            '%appoints %', '%names % as %',
            '%ceo transition%', '%cfo transition%', '%ceo resignation%', '%cfo resignation%',
            '%steps down as%', '%departs as%',
            '%to acquire%', '%completes acquisition%', '%announces acquisition%',
            '%acquisition of%', '%agrees to acquire%',
            '%merger with%', '%completes merger%', '%announces merger%',
            '%divests%', '%divestiture of%', '%completes sale of%',
            '%spin-off%', '%spinoff%', '%strategic alternative%', '%go-private%',
            '%declares dividend%', '%declares quarterly dividend%',
            '%increases quarterly dividend%', '%raises quarterly dividend%',
            '%increases dividend%', '%raises dividend%', '%dividend increase%',
            '%dividend cut%', '%suspends dividend%', '%special dividend%',
            '%stock split%', '%reverse stock split%',
            '%share repurchase program%', '%share repurchase authorization%',
            '%buyback program%', '%repurchase authorization%', '%authorizes % repurchase%',
            '%layoff%', '%lay off%', '%job cut%', '%job cuts%',
            '%workforce reduction%', '%reduces workforce%', '%restructuring plan%',
            '%restructuring charge%', '%cost reduction plan%',
            '%chapter 11%', '%chapter 7%', '%files for bankruptcy%',
            '%bankruptcy filing%', '%bankruptcy protection%',
            '%dip financing%', '%debtor in possession%', '%voluntary chapter%',
            '%emerges from bankruptcy%', '%liquidation plan%',
            '%settlement of%', '%agrees to settle%', '%settles % lawsuit%',
            '%class action%', '%sec investigation%', '%ftc investigation%',
            '%regulatory settlement%', '%securities litigation%',
            '%moody%upgrade%', '%moody%downgrade%',
            "% s&p %upgrade%", "% s&p %downgrade%",
            '%fitch%upgrade%', '%fitch%downgrade%',
            '%credit rating upgrade%', '%credit rating downgrade%',
            '%investment grade rating%', '%rating action%',
            '%creditwatch%', '%watch negative%', '%watch positive%',
            '%outlook negative%', '%outlook positive%', '%outlook stable%',
            '%goodwill impairment%', '%impairment charge%', '%impairment of%',
            '%write-off%', '%writedown%', '%write down%',
            '%activist investor%', '%activist stake%', '%proxy fight%', '%hostile bid%',
            '%going concern%', '%shareholder vote%', '%stockholder vote%',
            '%delisting%', '%delist%', '%ticker change%', '%name change%',
            '%added to s&p%', '%removed from s&p%', '%index inclusion%',
            '%restatement%', '%restates %financial%',
            '%store closure%', '%store closing%', '%closes % store%',
            '%store opening%', '%opens % store%', '%store expansion%',
            '%joint venture%', '%strategic partnership%', '%strategic alliance%',
            '%senior notes%', '%notes offering%', '%debt offering%',
            '%credit facility%', '%term loan%',
            '%investor day%', '%analyst day%', '%investor conference%',
        ]

        like_title   = ' OR '.join(['title LIKE %s']   * len(keywords))
        like_clause  = f"({like_title})"
        date_filters = ['published_at >= %s']
        date_params  = [ticker, start_date]
        if end_date:
            date_filters.append('published_at <= %s')
            date_params.append(end_date)

        query = f"""
            SELECT published_at, title, publisher, link,
                   sentiment_score, primary_topic_v2, payload_json
            FROM   coreiq_yf_market_news_sentiment
            WHERE  ticker = %s
              AND  {' AND '.join(date_filters)}
              AND  ({like_clause})
            ORDER  BY published_at DESC
        """

        params = date_params + keywords
        cursor.execute(query, params)

        yf_filtered_out = 0
        for row in cursor.fetchall():
            pub_date  = row[0].strftime('%Y-%m-%d') if row[0] else None
            title     = row[1] or 'No Title'
            publisher = row[2] or ''
            link      = row[3]
            sentiment = row[4]
            yf_topic  = row[5] or ''     # ML pre-classified topic (primary_topic_v2)
            payload   = row[6]

            if not pub_date:
                continue

            # Extract article body from payload_json → content.summary (needed for relevance check)
            body = ''
            if payload:
                try:
                    pj = _json.loads(payload)
                    body = (pj.get('content', {}).get('summary', '')
                            or pj.get('content', {}).get('description', ''))
                    body = str(body)[:1000]
                except Exception:
                    pass

            # Classify on title (same function as AV news for consistency)
            # Relevance gate — discard articles about other companies sharing this ticker symbol
            if not _news_is_relevant(title, body, company_info):
                yf_filtered_out += 1
                continue

            category, subtype = _classify_news(title)

            # Boost using YF's own ML topic when our keyword classifier returns General
            if category == 'General News' and yf_topic:
                topic_map = {
                    'earnings':         ('Earnings & Guidance', 'Earnings News'),
                    'retail_wholesale': ('Store Operations',    'Retail Industry News'),
                    'financial_markets':('Earnings & Guidance', 'Market News'),
                    'economy_macro':    ('General News',        'Macro/Economy'),
                    'mergers_and_acquisitions': ('M&A Activity', 'Deal News'),
                    'dividends_and_splits':     ('Capital Returns', 'Dividend/Split News'),
                    'bankruptcy':       ('Bankruptcy',          'Distress News'),
                }
                if yf_topic in topic_map:
                    category, subtype = topic_map[yf_topic]

            sentiment_label = ('Bullish' if sentiment and sentiment > 0.15
                               else 'Bearish' if sentiment and sentiment < -0.15
                               else 'Neutral')

            situation = f"{body}\n\nPublisher: {publisher}\nSentiment: {sentiment_label}"
            if yf_topic:
                situation += f"\nYF Topic: {yf_topic}"

            event = {
                'company_id':     company_info.get('company_id'),
                'ticker':         company_info.get('ticker', ticker),
                'event_date':     pub_date,
                'event_category': category,
                'event_subtype':  subtype,
                'headline':       title,
                'situation':      situation.strip()[:6000],
                'source':         f"YF News — {publisher}" if publisher else 'YF News',
                'source_ref':     link,
            }
            event['event_hash']   = compute_event_hash(event)
            event['etl_batch_id'] = ETL_BATCH_ID
            events.append(event)

        if yf_filtered_out:
            print(f"    Relevance filter: discarded {yf_filtered_out} articles about other companies")
        conn.close()
    except Exception as e:
        print(f"    YF News error: {e}")

    return events


# ── Main ──────────────────────────────────────────────────────────────────────

def _get_incremental_starts(ticker: str, user_start_date: str) -> tuple[str, str]:
    """
    Compute per-ticker incremental start dates for EDGAR and news independently.

    WHY two separate anchors:
    ─────────────────────────────────────────────────────────────────────────
    EDGAR and news use completely different date semantics:

      • EDGAR date_filter (edgartools) filters by FILING SUBMISSION DATE.
        e.g. a 10-K for FY ending Dec 31 is submitted to SEC on March 15.
        edgartools will only return it if date_filter includes March 15.

      • event_date for 10-K/10-Q stores the PERIOD END DATE (Dec 31),
        NOT the submission date. Using MAX(event_date) as the EDGAR anchor
        would miss filings submitted AFTER the period end.

      • News event_date = publication date → correct anchor for news SQL.

    Using a SINGLE date anchor fails like this:
      Last EDGAR filing submitted Jan 15. Last news article April 4.
      MAX(event_date) = April 4.  effective_start = March 5.
      edgartools searches filings submitted >= March 5.
      → January 10-K MISSED. ✗

    With TWO anchors:
      edgar_start = MAX(edgar_filing_date) - 30 days = Dec 16 ✓
      news_start  = MAX(news event_date)   - 30 days = March 5 ✓

    Both are derived from the events table itself (column edgar_filing_date).
    No separate watermark table needed. --delete-first automatically resets
    both anchors by deleting the rows.

    30-day overlap on each anchor independently handles:
      • Amended filings (8-K/A, 10-K/A) submitted weeks after the original
      • News articles backfilled by data vendors
      • Late proxy or 13D/G filings
      • Different companies with different last-processed dates

    Returns:
        (edgar_effective_start, news_effective_start) as 'YYYY-MM-DD' strings.
        Each falls back to user_start_date if no prior data exists.
    """
    OVERLAP_DAYS = 30
    edgar_start = user_start_date
    news_start  = user_start_date

    try:
        conn = _get_db_conn()
        cur  = conn.cursor()

        # ── EDGAR anchor: MAX of actual filing submission dates ───────────────
        # edgar_filing_date is populated for all SEC EDGAR-sourced events.
        # NULL for news events → excluded automatically by MAX().
        cur.execute("""
            SELECT MAX(edgar_filing_date)
            FROM   coreiq_company_events
            WHERE  ticker = %s
              AND  edgar_filing_date IS NOT NULL
        """, (ticker,))
        row = cur.fetchone()
        if row and row[0]:
            last_edgar_dt = datetime.strptime(str(row[0])[:10], '%Y-%m-%d')
            candidate     = (last_edgar_dt - timedelta(days=OVERLAP_DAYS)).strftime('%Y-%m-%d')
            if candidate > user_start_date:
                edgar_start = candidate

        # ── News anchor: MAX of news publication dates ────────────────────────
        # News events always have edgar_filing_date = NULL; use event_date.
        cur.execute("""
            SELECT MAX(event_date)
            FROM   coreiq_company_events
            WHERE  ticker = %s
              AND  edgar_filing_date IS NULL
        """, (ticker,))
        row = cur.fetchone()
        if row and row[0]:
            last_news_dt = datetime.strptime(str(row[0])[:10], '%Y-%m-%d')
            candidate    = (last_news_dt - timedelta(days=OVERLAP_DAYS)).strftime('%Y-%m-%d')
            if candidate > user_start_date:
                news_start = candidate

        conn.close()
    except Exception as e:
        print(f"  ⚠ Could not compute incremental starts (falling back to full load): {e}")
        return user_start_date, user_start_date

    return edgar_start, news_start


def run_ticker(ticker: str, start_date: str, end_date: str, output_dir: str,
               push_db: bool) -> pd.DataFrame:
    """
    Run the full 9-layer ETL for a single ticker over [start_date, end_date].
    Returns the events DataFrame (also saved to Excel).

    INCREMENTAL LOADING:
    If the DB already has records for this ticker, the effective fetch start date
    is automatically advanced to (last_event_date - 7 days) instead of the
    user-supplied start_date. This means re-running the same command the next day
    only fetches the last week of data — not everything from 2016.

    The 7-day overlap window handles edge cases where a late-filing or amended
    filing arrives a few days after its original event date.

    The user-supplied start_date is still respected as an absolute floor —
    if the DB has no records yet, the full historical range is processed.
    """
    print(f"\n{'='*70}")
    print(f"KEY DEVELOPMENTS GENERATOR — VERSION 4")
    print(f"Company:   {ticker}")
    print(f"Batch ID:  {ETL_BATCH_ID[:8]}...")
    print(f"{'='*70}\n")

    # Company info from DB (includes is_sec flag, cik, country)
    print("Fetching company info from database...")
    company_info = get_company_info(ticker)
    is_sec = company_info.get('is_sec', True)
    print(f"  Company ID : {company_info.get('company_id')}")
    print(f"  Ticker     : {company_info.get('ticker')}")
    print(f"  CIK        : {company_info.get('cik') or '(none)'}")
    print(f"  Source     : {company_info.get('source') or '(unknown)'}")
    print(f"  Country    : {company_info.get('country_of_incorporation') or '(unknown)'}")
    print(f"  SEC filer  : {'YES — all 10 EDGAR layers active' if is_sec else 'NO — skipping EDGAR layers, news-only mode'}")

    # ── Incremental start dates — two independent anchors ────────────────────
    edgar_start, news_start = _get_incremental_starts(ticker, start_date)

    is_incremental = edgar_start != start_date or news_start != start_date
    if is_incremental:
        print(f"\n  ✅ INCREMENTAL MODE (derived from existing DB data):")
        if edgar_start != start_date:
            print(f"     EDGAR fetch  : {edgar_start} → {end_date}  (MAX(filing_date) − 30 days)")
        else:
            print(f"     EDGAR fetch  : {start_date} → {end_date}  (no prior EDGAR data — full load)")
        if news_start != start_date:
            print(f"     News fetch   : {news_start} → {end_date}  (MAX(news date) − 30 days)")
        else:
            print(f"     News fetch   : {start_date} → {end_date}  (no prior news — full load)")
        print(f"     Skipping     : {start_date} → (anchor − 30d)  (already in DB)\n")
    else:
        print(f"\n  ℹ️  No existing data — full historical load: {start_date} → {end_date}\n")

    print(f"EDGAR Range: {edgar_start}  →  {end_date}")
    print(f"News Range : {news_start}  →  {end_date}")
    print(f"Push DB    : {'YES' if push_db else 'no (Excel only)'}\n")

    # edgartools date_filter works on filing submission date
    date_filter = f'{edgar_start}:{end_date}'
    all_events  = []

    if not is_sec:
        # ── NON-SEC COMPANY — skip all EDGAR layers ───────────────────────────
        print("\n  ⚠  Non-SEC company detected. Skipping EDGAR layers 1-8.")
        print("     All events will come from AV News (Layer 9) + YF News (Layer 10).\n")
        for i in range(1, 9):
            print(f"LAYER {i}: SKIPPED (non-SEC company)\n")
    else:
        # ── EDGAR identity (required by SEC fair-access policy) ───────────────
        set_identity('Research Portal mohdsaeedafri@coresight.com')
        try:
            company = Company(ticker)
            print(f"  EDGAR      : {company.name}\n")
        except Exception as e:
            print(f"  ⚠  EDGAR Company() failed: {e}")
            print("  Falling back to news-only mode.\n")
            is_sec = False
            company = None

    def _log_filing(form, filing, n_events):
        """Per-filing progress line: form | accession | filing_date | events extracted."""
        try:
            fd   = str(filing.filing_date)[:10]
            acc  = str(getattr(filing, 'accession_no', '?'))
            print(f"    [{form}] {fd}  acc={acc}  → {n_events} event(s) extracted")
        except Exception:
            print(f"    [{form}] → {n_events} event(s) extracted")

    if is_sec and company is not None:
        # ── LAYER 1: 8-K (all items) ────────────────────────────────────────────
        print("LAYER 1: Processing 8-K filings (all item types)...")
        count = 0
        filing_count = 0
        try:
            filings_8k = list(company.get_filings(form='8-K', amendments=True).filter(date=date_filter))
            print(f"  Found {len(filings_8k)} 8-K filings in range {date_filter}")
            for filing in filings_8k:
                filing_count += 1
                try:
                    evts = process_8k_filing(filing, ticker, company_info)
                    all_events.extend(evts)
                    count += len(evts)
                    _log_filing('8-K', filing, len(evts))
                except Exception as e:
                    print(f"    [8-K] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ 8-K fetch error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 1 done — {filing_count} filings processed, {count} events extracted\n")

        # ── LAYER 2: 6-K (foreign private issuers — current reports) ────────────
        print("LAYER 2: Processing 6-K filings (foreign private issuer current reports)...")
        count = 0
        filing_count = 0
        try:
            filings_6k = list(company.get_filings(form='6-K', amendments=True).filter(date=date_filter))
            print(f"  Found {len(filings_6k)} 6-K filings in range {date_filter}")
            for filing in filings_6k:
                filing_count += 1
                try:
                    evts = process_6k_filing(filing, ticker, company_info)
                    all_events.extend(evts)
                    count += len(evts)
                    _log_filing('6-K', filing, len(evts))
                except Exception as e:
                    print(f"    [6-K] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ 6-K fetch error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 2 done — {filing_count} filings processed, {count} events extracted\n")

        # ── LAYER 3: 10-K (domestic annual) + 20-F (foreign annual) ─────────────
        print("LAYER 3: Processing 10-K / 20-F annual filings (Risk Factors, Legal, MD&A)...")
        count = 0
        filing_count = 0
        try:
            filings_10k = list(company.get_filings(form='10-K', amendments=True).filter(date=date_filter))
            print(f"  Found {len(filings_10k)} 10-K filings in range {date_filter}")
            for filing in filings_10k:
                filing_count += 1
                try:
                    evts = process_10k_filing(filing, ticker, company_info)
                    all_events.extend(evts)
                    count += len(evts)
                    _log_filing('10-K', filing, len(evts))
                except Exception as e:
                    print(f"    [10-K] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ 10-K fetch error: {type(e).__name__}: {e}")
        try:
            for form_type in ['20-F', '20-F/A']:
                filings_20f = list(company.get_filings(form=form_type).filter(date=date_filter))
                if filings_20f:
                    print(f"  Found {len(filings_20f)} {form_type} filings in range {date_filter}")
                for filing in filings_20f:
                    filing_count += 1
                    try:
                        evts = process_20f_filing(filing, ticker, company_info)
                        all_events.extend(evts)
                        count += len(evts)
                        _log_filing(form_type, filing, len(evts))
                    except Exception as e:
                        print(f"    [{form_type}] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ 20-F fetch error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 3 done — {filing_count} filings processed, {count} events extracted\n")

        # ── LAYER 4: 10-Q ───────────────────────────────────────────────────────
        print("LAYER 4: Processing 10-Q filings (MD&A, Legal, Risk, Equity)...")
        count = 0
        filing_count = 0
        try:
            print("  Building 10-Q quarter map (all history)...")
            tenq_quarter_map = build_10q_quarter_map(company)
            print(f"  Quarter map: {len(tenq_quarter_map)} period entries mapped")
            filings_10q = list(company.get_filings(form='10-Q', amendments=True).filter(date=date_filter))
            print(f"  Found {len(filings_10q)} 10-Q filings in range {date_filter}")
            for filing in filings_10q:
                filing_count += 1
                try:
                    evts = process_10q_filing(filing, ticker, company_info, quarter_map=tenq_quarter_map)
                    all_events.extend(evts)
                    count += len(evts)
                    _log_filing('10-Q', filing, len(evts))
                except Exception as e:
                    print(f"    [10-Q] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ 10-Q fetch/process error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 4 done — {filing_count} filings processed, {count} events extracted\n")

        # ── LAYER 5: DEF 14A (ProxyStatement structured class) ──────────────────
        print("LAYER 5: Processing DEF 14A proxy filings (structured API)...")
        count = 0
        filing_count = 0
        try:
            filings_proxy = list(company.get_filings(form='DEF 14A').filter(date=date_filter))
            print(f"  Found {len(filings_proxy)} DEF 14A filings in range {date_filter}")
            for filing in filings_proxy:
                filing_count += 1
                try:
                    evts = process_proxy_filing(filing, ticker, company_info)
                    all_events.extend(evts)
                    count += len(evts)
                    _log_filing('DEF 14A', filing, len(evts))
                except Exception as e:
                    print(f"    [DEF 14A] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ DEF 14A fetch error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 5 done — {filing_count} filings processed, {count} events extracted\n")

        # ── LAYER 6: 13D / 13G (Investor Activism) ──────────────────────────────
        print("LAYER 6: Processing Schedule 13D/13G (investor activism)...")
        count = 0
        filing_count = 0
        try:
            for form_type in ['SC 13D', 'SC 13D/A', 'SC 13G', 'SC 13G/A']:
                try:
                    filings_13 = list(company.get_filings(form=form_type).filter(date=date_filter))
                    if filings_13:
                        print(f"  Found {len(filings_13)} {form_type} filings in range {date_filter}")
                    for filing in filings_13:
                        filing_count += 1
                        try:
                            evts = process_13dg_filing(filing, ticker, company_info)
                            all_events.extend(evts)
                            count += len(evts)
                            _log_filing(form_type, filing, len(evts))
                        except Exception as e:
                            print(f"    [{form_type}] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
                except Exception as e:
                    print(f"  ✗ {form_type} fetch error: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ 13D/13G error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 6 done — {filing_count} filings processed, {count} events extracted\n")

        # ── LAYER 7: S-1/S-3/F-1/F-3/424B (Offerings — domestic + foreign) ──────
        print("LAYER 7: Processing offering filings (S-1/S-3/F-1/F-3/424B)...")
        count = 0
        filing_count = 0
        try:
            for form_type in ['S-3', 'S-3/A', 'S-1', 'S-1/A',
                              'F-1', 'F-1/A', 'F-3', 'F-3/A',
                              '424B3', '424B4', '424B5']:
                try:
                    filings_off = list(company.get_filings(form=form_type).filter(date=date_filter))
                    if filings_off:
                        print(f"  Found {len(filings_off)} {form_type} filings in range {date_filter}")
                    for filing in filings_off:
                        filing_count += 1
                        try:
                            evts = process_offering_filing(filing, ticker, company_info)
                            all_events.extend(evts)
                            count += len(evts)
                            _log_filing(form_type, filing, len(evts))
                        except Exception as e:
                            print(f"    [{form_type}] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
                except Exception as e:
                    print(f"  ✗ {form_type} fetch error: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ Offering error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 7 done — {filing_count} filings processed, {count} events extracted\n")

        # ── LAYER 8: NT 10-K / NT 10-Q (Delayed Filings) ────────────────────────
        print("LAYER 8: Processing NT 10-K / NT 10-Q delayed filing notices...")
        count = 0
        filing_count = 0
        try:
            for form_type in ['NT 10-K', 'NT 10-Q']:
                try:
                    filings_nt = list(company.get_filings(form=form_type).filter(date=date_filter))
                    if filings_nt:
                        print(f"  Found {len(filings_nt)} {form_type} filings in range {date_filter}")
                    for filing in filings_nt:
                        filing_count += 1
                        try:
                            evts = process_nt_filing(filing, ticker, company_info)
                            all_events.extend(evts)
                            count += len(evts)
                            _log_filing(form_type, filing, len(evts))
                        except Exception as e:
                            print(f"    [{form_type}] ERROR on {getattr(filing,'accession_no','?')}: {type(e).__name__}: {e}")
                except Exception as e:
                    print(f"  ✗ {form_type} fetch error: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ✗ NT filing error: {type(e).__name__}: {e}")
        print(f"  ✓ LAYER 8 done — {filing_count} filings processed, {count} events extracted\n")

    # ── LAYER 9: News ────────────────────────────────────────────────────────
    print(f"LAYER 9: Fetching AV news (news_start={news_start}, end={end_date})...")
    try:
        av_news = get_news(ticker, news_start, company_info, end_date=end_date)
        all_events.extend(av_news)
        print(f"  ✓ LAYER 9 done — {len(av_news)} AV news articles fetched\n")
    except Exception as e:
        print(f"  ✗ LAYER 9 AV news error: {type(e).__name__}: {e}\n")
        av_news = []

    # ── LAYER 10: YF News (Yahoo Finance — coreiq_yf_market_news_sentiment) ──
    print(f"LAYER 10: Fetching YF news (news_start={news_start}, end={end_date})...")
    try:
        yf_news = get_yf_news(ticker, news_start, company_info, end_date=end_date)
        all_events.extend(yf_news)
        print(f"  ✓ LAYER 10 done — {len(yf_news)} YF news articles fetched\n")
    except Exception as e:
        print(f"  ✗ LAYER 10 YF news error: {type(e).__name__}: {e}\n")
        yf_news = []

    # ── Build DataFrame ───────────────────────────────────────────────────────
    df = pd.DataFrame(all_events)
    if df.empty:
        print("❌ No events found.")
        return pd.DataFrame()

    df['event_date'] = pd.to_datetime(df['event_date'], errors='coerce')
    # Drop rows with no parseable date — they cannot be inserted into the DB
    df = df.dropna(subset=['event_date'])
    df = df.sort_values('event_date', ascending=False).reset_index(drop=True)

    # Recompute hashes with row index to guarantee per-run uniqueness
    for idx, row in df.iterrows():
        df.at[idx, 'event_hash'] = compute_event_hash(row.to_dict(), idx)

    # Add DB category column (screening service mapping)
    df['db_category'] = df['event_category'].apply(map_category)

    # Enforce column order for Excel
    # NOTE: edgar_filing_date MUST be kept — push_to_db reads it to populate
    # the incremental anchor column. Do NOT remove it from this list.
    col_order = [
        'company_id', 'ticker', 'event_date', 'edgar_filing_date',
        'event_category', 'event_subtype',
        'headline', 'situation', 'source', 'source_ref',
        'db_category', 'etl_batch_id', 'event_hash',
    ]
    df = df[[c for c in col_order if c in df.columns]]

    # Write Excel
    os.makedirs(output_dir, exist_ok=True)
    out_file = os.path.join(output_dir, f"{ticker.lower()}_key_developments_v4.xlsx")
    df.to_excel(out_file, index=False)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"{'='*70}")
    print(f"✅  EXCEL: {out_file}")
    print(f"{'='*70}")
    print(f"Total Events : {len(df)}  (after dedup)")
    print(f"\nBy Source:")
    for src, cnt in df['source'].value_counts().items():
        print(f"  {src:<35} {cnt}")
    print(f"\nBy Category:")
    for cat, cnt in df['event_category'].value_counts().items():
        print(f"  {cat:<45} {cnt}")
    print(f"\nBy Category + Subtype:")
    for (cat, sub), cnt in df.groupby(['event_category','event_subtype']).size().sort_values(ascending=False).items():
        print(f"  {cat:<40} | {sub:<45} | {cnt}")
    print(f"\nDate Range: {df['event_date'].min().date()} → {df['event_date'].max().date()}")

    # ── DB push ───────────────────────────────────────────────────────────────
    if push_db:
        print(f"\n{'='*70}")
        print("PUSHING TO coreiq_company_events (incremental — INSERT IGNORE)...")
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        ensure_partitions(end_dt)
        inserted, skipped = push_to_db(df)
        total_in_db = get_db_event_count(ticker)
        print(f"  Rows attempted : {len(df)}")
        print(f"  Inserted (new) : {inserted}")
        print(f"  Skipped (dupe) : {skipped}")
        print(f"  Total in DB now: {total_in_db}")
        print(f"{'='*70}")

        # ── Print next-run anchors so operator can verify incremental is set ──
        # These are what the NEXT execution will use as effective_start dates.
        try:
            next_edgar, next_news = _get_incremental_starts(ticker, start_date)
            print(f"\n  ── NEXT RUN INCREMENTAL ANCHORS (auto-derived from DB) ──")
            print(f"     EDGAR anchor : {next_edgar}  (MAX(edgar_filing_date) − 30 days)")
            print(f"     News  anchor : {next_news}  (MAX(news event_date)   − 30 days)")
            print(f"     → Next run will only fetch {next_edgar} → <end_date> for EDGAR")
            print(f"       and {next_news} → <end_date> for news")
            print(f"       (NOT from {start_date} — full historical load is skipped)")
        except Exception as e:
            print(f"  ⚠ Could not compute next-run anchors: {e}")

    return df


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Key Developments Generator v4 — Production Grade',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single ticker, last 12 months, Excel only
  python3 generate_key_developments_v4.py --ticker M

  # Single ticker, full history from 2017, push to DB
  python3 generate_key_developments_v4.py --ticker M --start-date 2017-01-01 --end-date 2026-04-02 --push-db

  # Multiple tickers, last 24 months, push to DB
  python3 generate_key_developments_v4.py --tickers M,WMT,TGT --months 24 --push-db

  # Read ALL tickers from coreiq_companies table, process one by one, push to DB
  python3 generate_key_developments_v4.py --from-table coreiq_companies --months 12 --push-db

  # Read active retail tickers only from a table, custom ticker column
  python3 generate_key_developments_v4.py --from-table coreiq_companies --ticker-col ticker --where "is_active=1" --months 12 --push-db

  # Read from a watchlist table with a different column name
  python3 generate_key_developments_v4.py --from-table coreiq_watchlist_companies --ticker-col company_ticker --push-db

  # Clean re-ingest: delete existing then push fresh
  python3 generate_key_developments_v4.py --ticker M --delete-first --push-db
        """
    )
    # Target companies — three mutually exclusive input modes
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--ticker',     help='Single ticker (e.g. M)')
    group.add_argument('--tickers',    help='Comma-separated tickers (e.g. M,WMT,TGT)')
    group.add_argument('--from-table', dest='from_table',
                       help='Read tickers from a DB table (e.g. coreiq_companies). '
                            'Reads all active rows. Use --ticker-col and --where to customise.')
    parser.add_argument('--ticker-col', dest='ticker_col', default='ticker',
                        help='Column name containing the ticker in --from-table (default: ticker)')
    parser.add_argument('--where', dest='where_clause', default=None,
                        help='Optional WHERE clause for --from-table, e.g. "is_active=1 AND sector=\'Retail\'"')

    # Date range — use --start-date/--end-date OR --months (default 12)
    parser.add_argument('--start-date', dest='start_date',
                        help='Start date YYYY-MM-DD (overrides --months)')
    parser.add_argument('--end-date',   dest='end_date',
                        default=datetime.now().strftime('%Y-%m-%d'),
                        help='End date YYYY-MM-DD (default: today)')
    parser.add_argument('--months', type=int, default=12,
                        help='Months of history counted back from --end-date (default 12, ignored if --start-date given)')

    # Output / DB
    parser.add_argument('--output',       default='.', help='Excel output directory')
    parser.add_argument('--push-db',      action='store_true', dest='push_db',
                        help='Push events to coreiq_company_events (incremental INSERT IGNORE)')
    parser.add_argument('--delete-first', action='store_true', dest='delete_first',
                        help='Delete existing DB rows for the ticker(s) before pushing. '
                             'Use for clean re-ingestion. Requires --push-db.')

    args = parser.parse_args()

    # Resolve ticker list
    if args.ticker:
        tickers = [args.ticker.upper()]
    elif args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
    else:
        # --from-table: read tickers from any DB table, one at a time
        tickers = _load_tickers_from_table(
            table      = args.from_table,
            ticker_col = args.ticker_col,
            where      = args.where_clause,
        )
        if not tickers:
            print(f"❌  No tickers found in table '{args.from_table}' "
                  f"(col='{args.ticker_col}', where='{args.where_clause}'). Aborting.")
            return
        print(f"  Loaded {len(tickers)} tickers from table '{args.from_table}'\n")

    # Resolve date range
    end_date = args.end_date
    if args.start_date:
        start_date = args.start_date
    else:
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        start_date = (end_dt - timedelta(days=30 * args.months)).strftime('%Y-%m-%d')

    # Safety: --delete-first requires --push-db
    if args.delete_first and not args.push_db:
        print("⚠  --delete-first requires --push-db. Aborting.")
        return

    for ticker in tickers:
        if args.delete_first and args.push_db:
            print(f"\nDeleting existing DB rows for {ticker}...")
            deleted = delete_ticker_events(ticker)
            print(f"  Deleted {deleted} rows.")
            # No separate watermark to clear — deleting rows automatically resets
            # both EDGAR and news anchors (MAX queries return NULL → full load).

        run_ticker(
            ticker    = ticker,
            start_date= start_date,
            end_date  = end_date,
            output_dir= args.output,
            push_db   = args.push_db,
        )


if __name__ == '__main__':
    main()
