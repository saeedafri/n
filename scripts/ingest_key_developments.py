"""
Key Developments ETL — Production Canonical Script
===================================================
Single entrypoint for all Key Developments data ingestion.

Architecture: edgartools-FIRST, news-SECONDARY.

Sources (priority order):
  1. edgartools         (PRIMARY — SEC EDGAR via edgartools API: 8-K, SC 13D/G)
  2. Earnings calendar  (coreiq_nasdaq_earnings_calendar)
  3. News sentiment     (coreiq_av_market_news_sentiment — SECONDARY)
  4. Earnings estimates (coreiq_av_financials_earnings_estimates)

  NOTE: local filing cache (data/filings_blob_cache/) is NOT used.
        It was removed as the primary source in Phase 5 architecture correction.

Covers all 12 CIQ-aligned categories.

Features:
  - event_hash based idempotent upsert (INSERT … ON DUPLICATE KEY UPDATE)
  - full rebuild or incremental mode
  - source-specific / category-specific / ticker-specific runs
  - dry-run mode
  - batch tracking + audit log table
  - watermark tracking for incremental runs
  - per-source error isolation
  - timing logs
  - coverage report

Usage:
  python scripts/ingest_key_developments.py --env staging --yes
  python scripts/ingest_key_developments.py --env staging --dry-run
  python scripts/ingest_key_developments.py --env staging --mode incremental --yes
  python scripts/ingest_key_developments.py --env staging --sources edgartools,news,earnings --yes
  python scripts/ingest_key_developments.py --env staging --categories bankruptcy_updates --yes
"""

import argparse
import hashlib
import html as html_mod
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import uuid
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymysql

# ─────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────

ALL_CATEGORIES = [
    "results_announcements", "announced_completed_transactions",
    "bankruptcy_updates", "company_forecasts_ratings", "corporate_structure",
    "customer_product", "dividends_splits", "investor_activism",
    "listing_trading", "red_flags_distress", "potential_transactions",
    "transaction_updates",
]

CATEGORY_RENAMES = {
    "announced_transactions": "announced_completed_transactions",
    "red_flags": "red_flags_distress",
}

ALL_SOURCES = ["edgartools", "earnings", "news", "estimates"]

SOURCE_PRIORITY = {"edgartools": 4, "earnings_cal": 2, "estimates": 2, "news": 1}

# ── 8-K Item Code → (category, subtype, confidence) ──
ITEM_MAP: Dict[str, Tuple[str, Optional[str], float]] = {
    # Results
    "2.02": ("results_announcements", "Announcements of Earnings", 0.95),
    # 8.01 handled specially below (conference detection)
    # Corporate structure
    "5.02": ("corporate_structure", None, 0.90),
    "5.01": ("corporate_structure", "Executive/Board Changes", 0.85),
    "5.03": ("corporate_structure", "Bylaw/Governance Changes", 0.90),
    "5.07": ("corporate_structure", "Bylaw/Governance Changes", 0.90),
    # Transactions
    "1.01": ("announced_completed_transactions", "M&A Transaction Announcements", 0.90),
    "2.01": ("announced_completed_transactions", "M&A Transaction Closings", 0.90),
    "1.02": ("announced_completed_transactions", "M&A Transaction Announcements", 0.85),
    # Red flags / distress
    "2.05": ("red_flags_distress", "Restructuring", 0.90),
    "2.06": ("red_flags_distress", "Legal/Regulatory", 0.85),
    "4.01": ("red_flags_distress", "Auditor Changes", 0.85),
    "4.02": ("red_flags_distress", "Financial Restatement", 0.90),
    # Bankruptcy
    "1.03": ("bankruptcy_updates", "Bankruptcy/Receivership", 0.95),
    # Listing/trading
    "3.01": ("listing_trading", "Delisting/Listing Transfer", 0.90),
    # Dividends/splits
    "3.03": ("dividends_splits", "Material Modification of Rights", 0.85),
    # Forecasts (Reg FD)
    "7.01": ("company_forecasts_ratings", "Regulation FD Disclosure", 0.75),
    # Cybersecurity (SEC requirement since Dec 2023)
    "1.05": ("red_flags_distress", "Cybersecurity Incident", 0.90),
    # Debt events
    "2.03": ("red_flags_distress", "Debt Issuance", 0.80),
    "2.04": ("red_flags_distress", "Debt Default", 0.90),
    # Capital raises
    "3.02": ("announced_completed_transactions", "Private Placement", 0.85),
    # Shareholder nominations (investor activism)
    "5.08": ("investor_activism", "Shareholder Director Nomination", 0.85),
}

# 5.02 subtype inference keywords
MGMT_APPOINT_KW = [
    "appoint", "named", "promoted", "hired", "new ceo", "new cfo",
    "new coo", "new president", "new chief", "elected",
]
MGMT_DEPART_KW = [
    "resign", "depart", "retire", "terminated", "step down", "leave",
]

# ── News topic + keyword → category/subtype mapping ──
NEWS_RULES: List[Dict] = [
    # --- existing categories ---
    {"category": "results_announcements", "subtype": "Announcements of Earnings",
     "topic_key": "earnings", "title_keywords": None, "confidence": 0.80},
    {"category": "announced_completed_transactions", "subtype": "M&A Transaction Announcements",
     "topic_key": "mergers_and_acquisitions", "title_keywords": None, "confidence": 0.75},
    {"category": "customer_product", "subtype": "Product-Related Announcements",
     "topic_key": "technology",
     "title_keywords": ["launch", "product", "release", "partnership", "platform", "app"],
     "confidence": 0.65},
    {"category": "customer_product", "subtype": "Client Announcements",
     "topic_key": "retail_wholesale",
     "title_keywords": ["store", "expand", "open", "partner", "contract", "deal"],
     "confidence": 0.65},
    {"category": "red_flags_distress", "subtype": "Legal/Regulatory",
     "topic_key": None,
     "title_keywords": ["class action", "lawsuit", "sec investigation", "fraud",
                         "recall", "violation", "penalty", "fine", "settlement"],
     "confidence": 0.70},
    {"category": "red_flags_distress", "subtype": "Restructuring",
     "topic_key": None,
     "title_keywords": ["layoff", "restructuring", "downsizing", "job cut",
                         "workforce reduction", "plant closing"],
     "confidence": 0.70},
    # --- NEW: bankruptcy ---
    {"category": "bankruptcy_updates", "subtype": "Bankruptcy Filing",
     "topic_key": None,
     "title_keywords": ["bankruptcy", "chapter 11", "chapter 7", "liquidation",
                         "receivership", "creditor protection"],
     "confidence": 0.70},
    # --- NEW: listing/trading ---
    {"category": "listing_trading", "subtype": "IPO/Public Offering",
     "topic_key": "ipo", "title_keywords": None, "confidence": 0.70},
    {"category": "listing_trading", "subtype": "Listing Changes",
     "topic_key": None,
     "title_keywords": ["delist", "delisted", "delisting", "trading halt",
                         "trading suspension", "direct listing", "spac merger"],
     "confidence": 0.70},
    # --- NEW: dividends/splits ---
    {"category": "dividends_splits", "subtype": "Dividend Announcements",
     "topic_key": None,
     "title_keywords": ["dividend", "special dividend", "quarterly dividend",
                         "dividend increase", "dividend cut", "dividend suspension"],
     "confidence": 0.75},
    {"category": "dividends_splits", "subtype": "Stock Split",
     "topic_key": None,
     "title_keywords": ["stock split", "reverse split", "forward split"],
     "confidence": 0.75},
    # --- NEW: forecasts/ratings ---
    {"category": "company_forecasts_ratings", "subtype": "Company Guidance",
     "topic_key": None,
     "title_keywords": ["guidance", "raises guidance", "lowers guidance",
                         "outlook", "company forecast"],
     "confidence": 0.70},
    {"category": "company_forecasts_ratings", "subtype": "Analyst Rating",
     "topic_key": None,
     "title_keywords": ["upgrade", "downgrade", "price target", "overweight",
                         "underweight", "outperform", "underperform"],
     "confidence": 0.65},
    # --- NEW: investor activism ---
    {"category": "investor_activism", "subtype": "Activist Campaign",
     "topic_key": None,
     "title_keywords": ["activist investor", "activist stake", "proxy fight",
                         "proxy contest", "board seat", "shareholder proposal"],
     "confidence": 0.70},
    # --- NEW: transaction updates ---
    {"category": "transaction_updates", "subtype": "Deal Progress",
     "topic_key": None,
     "title_keywords": ["deal closed", "acquisition completed", "merger completed",
                         "deal terminated", "regulatory approval", "antitrust approval",
                         "shareholder vote", "merger vote"],
     "confidence": 0.70},
    # --- NEW: potential transactions (low confidence) ---
    {"category": "potential_transactions", "subtype": "Potential M&A",
     "topic_key": None,
     "title_keywords": ["takeover bid", "acquisition target", "merger talks",
                         "exploring sale", "strategic alternatives", "tender offer"],
     "confidence": 0.60},
    # --- Conferences / Investor Days ---
    {"category": "results_announcements", "subtype": "Conferences",
     "topic_key": None,
     "title_keywords": ["presents at", "presenting at", "investor day", "analyst day",
                         "investor conference", "at the annual conference"],
     "confidence": 0.65},
    {"category": "results_announcements", "subtype": "Conference Calls",
     "topic_key": None,
     "title_keywords": ["conference call", "earnings call", "earnings webcast",
                         "hosts webcast", "webcasts earnings"],
     "confidence": 0.70},
    # --- Divestitures ---
    {"category": "announced_completed_transactions", "subtype": "Divestitures",
     "topic_key": None,
     "title_keywords": ["divest", "divestiture", "spin-off", "spinoff",
                         "sells unit", "sell unit", "asset sale", "sells division"],
     "confidence": 0.70},
    # --- Joint Ventures ---
    {"category": "announced_completed_transactions", "subtype": "Joint Ventures",
     "topic_key": None,
     "title_keywords": ["joint venture", "co-development", "jv agreement",
                         "forms joint venture", "joint development"],
     "confidence": 0.65},
    # --- Secondary Offerings / Capital Raises ---
    {"category": "listing_trading", "subtype": "Secondary Offering",
     "topic_key": None,
     "title_keywords": ["secondary offering", "follow-on offering", "public offering",
                         "equity offering", "prices offering", "prices public offering"],
     "confidence": 0.75},
    # --- Debt / Capital Structure ---
    {"category": "red_flags_distress", "subtype": "Debt Issuance",
     "topic_key": None,
     "title_keywords": ["notes offering", "debt offering", "senior notes",
                         "prices notes", "prices bonds", "credit facility",
                         "term loan", "revolving credit"],
     "confidence": 0.70},
    # --- Going Concern ---
    {"category": "red_flags_distress", "subtype": "Going Concern",
     "topic_key": None,
     "title_keywords": ["going concern", "substantial doubt",
                         "doubt about continuing as a going concern"],
     "confidence": 0.85},
    # --- Cybersecurity ---
    {"category": "red_flags_distress", "subtype": "Cybersecurity Incident",
     "topic_key": None,
     "title_keywords": ["data breach", "cyberattack", "ransomware",
                         "cybersecurity incident", "hack", "security breach",
                         "unauthorized access"],
     "confidence": 0.75},
    # --- Regulatory Investigations ---
    {"category": "red_flags_distress", "subtype": "Regulatory Investigation",
     "topic_key": None,
     "title_keywords": ["doj investigation", "ftc investigation", "sec investigation",
                         "sec probe", "sec subpoena", "grand jury subpoena",
                         "antitrust investigation", "regulatory probe"],
     "confidence": 0.75},
    # --- Tender Offers ---
    {"category": "announced_completed_transactions", "subtype": "Tender Offer",
     "topic_key": None,
     "title_keywords": ["tender offer", "makes tender offer", "launches tender offer",
                         "commences tender offer"],
     "confidence": 0.80},
    # --- Name Changes / Rebranding ---
    {"category": "corporate_structure", "subtype": "Name Change",
     "topic_key": None,
     "title_keywords": ["changes name to", "renamed to", "rebranding", "rebrand",
                         "new name", "announces new brand"],
     "confidence": 0.80},
    # --- FDA / Life Sciences ---
    {"category": "customer_product", "subtype": "FDA Approval",
     "topic_key": "life_sciences",
     "title_keywords": ["fda approved", "fda approval", "fda clearance",
                         "receives fda", "granted fda"],
     "confidence": 0.80},
    {"category": "customer_product", "subtype": "Clinical Trial Results",
     "topic_key": "life_sciences",
     "title_keywords": ["phase 3 results", "phase 2 results", "clinical trial results",
                         "trial data", "trial results"],
     "confidence": 0.75},
    # --- Strategic Reviews ---
    {"category": "potential_transactions", "subtype": "Strategic Review",
     "topic_key": None,
     "title_keywords": ["strategic alternatives", "strategic review", "exploring sale",
                         "exploring options", "exploring a sale", "evaluate strategic"],
     "confidence": 0.70},
    # --- Shareholder Meeting / Proxy ---
    {"category": "corporate_structure", "subtype": "Annual Meeting/Proxy",
     "topic_key": None,
     "title_keywords": ["annual meeting", "proxy statement", "shareholder vote",
                         "annual shareholders", "annual general meeting"],
     "confidence": 0.65},
]

SUBTYPE_NORMALIZE = {
    "legal_regulatory": "Legal/Regulatory",
    "restructuring": "Restructuring",
    "mergers_acquisitions": "M&A Transaction Announcements",
    "product_launch": "Product-Related Announcements",
    # NEW normalizations
    "conference": "Conferences",
    "investor_day": "Conferences",
    "conference_call": "Conference Calls",
    "earnings_call": "Conference Calls",
    "divestiture": "Divestitures",
    "spin_off": "Divestitures",
    "tender_offer": "Tender Offer",
    "cybersecurity": "Cybersecurity Incident",
    "going_concern": "Going Concern",
    "late_filing": "Late Filing Notice",
    "debt_issuance": "Debt Issuance",
    "private_placement": "Private Placement",
    "secondary_offering": "Secondary Offering",
    "name_change": "Name Change",
    "fda_approval": "FDA Approval",
    "strategic_review": "Strategic Review",
    "annual_meeting": "Annual Meeting/Proxy",
    "shareholder_nomination": "Shareholder Director Nomination",
    "earnings_date_announced": "Earnings Release Date Announced",
}

log = logging.getLogger("keydev_etl")

# ─────────────────────────────────────────────────────────────────────────
#  CONNECTION
# ─────────────────────────────────────────────────────────────────────────

def get_connection(env: str):
    """Create a DB connection for the specified environment."""
    from dotenv import load_dotenv
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    if env == "staging":
        host = os.environ.get("STG_DB_HOST", "")
        port = int(os.environ.get("STG_DB_PORT", "3306"))
        user = os.environ.get("STG_DB_USER", "")
        pw = os.environ.get("STG_DB_PASSWORD", "")
        db = os.environ.get("STG_DB_NAME", "")
        ssl_ctx = ssl.create_default_context(
            cafile=str(project_root / "DigiCertGlobalRootG2.crt.pem"))
        return pymysql.connect(
            host=host, port=port, user=user, password=pw, database=db,
            ssl=ssl_ctx, cursorclass=pymysql.cursors.DictCursor,
            charset="utf8mb4")
    else:
        return pymysql.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASSWORD", ""),
            database=os.environ.get("DB_NAME", "chainxydata_stg"),
            cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")


def load_company_map(conn) -> Dict[str, int]:
    """Return ticker → company_id mapping."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, ticker FROM coreiq_companies")
        return {r["ticker"]: r["id"] for r in cur.fetchall()}


# ─────────────────────────────────────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────────────────────────────────────

def generate_batch_id() -> str:
    return str(uuid.uuid4())


def compute_event_hash(ev: Dict) -> str:
    """SHA-256 of (ticker|category|date|source|headline_prefix)."""
    raw = "|".join([
        ev["ticker"],
        ev["event_category"],
        str(ev["event_date"]),
        ev["source"],
        (ev.get("headline") or "")[:100].lower().strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_event(ev: Dict) -> Dict:
    """Category rename, subtype normalize, HTML decode, compute hash."""
    # Category rename
    cat = ev.get("event_category", "")
    ev["event_category"] = CATEGORY_RENAMES.get(cat, cat)

    # Subtype normalize
    st = ev.get("event_subtype") or ""
    key = st.lower().replace(" ", "_").replace("/", "_")
    ev["event_subtype"] = SUBTYPE_NORMALIZE.get(key, st) or None

    # HTML entity decode
    if ev.get("headline"):
        ev["headline"] = html_mod.unescape(ev["headline"])[:500]

    # Ensure source_detail is set
    if not ev.get("source_detail"):
        src = ev.get("source", "")
        if src == "news":
            ev["source_detail"] = ev.get("source_name") or "alpha_vantage"
        elif src == "earnings_cal":
            ev["source_detail"] = "nasdaq"
        elif src == "edgartools":
            ev["source_detail"] = ev.get("source_ref") or "sec_edgar"
        elif src == "estimates":
            ev["source_detail"] = "alpha_vantage"

    # Truncate fields to fit DB column limits
    if ev.get("source_detail"):
        ev["source_detail"] = ev["source_detail"][:500]

    # Compute hash
    ev["event_hash"] = compute_event_hash(ev)
    return ev


def _extract_8k_headline(html_text: str, ticker: str) -> Optional[str]:
    """Extract a short headline from 8-K HTML."""
    clean = re.sub(r"<[^>]+>", " ", html_text)
    clean = re.sub(r"\s+", " ", clean).strip()
    patterns = [
        r"(?:Item\s+\d+\.\d+[^.]*?\.\s*)([A-Z][^.]{20,120}\.)",
        r"(?:PRESS RELEASE|News Release)[:\s]*([A-Z][^.]{20,120}\.)",
    ]
    for pat in patterns:
        m = re.search(pat, clean)
        if m:
            return html_mod.unescape(m.group(1).strip()[:500])
    sentences = re.findall(r"([A-Z][A-Za-z0-9,\s\-\$%&'\"]{30,120}\.)", clean[:5000])
    for s in sentences:
        lower = s.lower()
        if ticker.lower() in lower or "report" in lower or "announc" in lower:
            return html_mod.unescape(s.strip()[:500])
    return None


def _try_extract_filing_date(html_text: str) -> Optional[date]:
    """Try to extract actual filing date from 8-K HTML (fix Jan 1 bug)."""
    patterns = [
        r"(?:Filed|Date of Report|Date of Filing)[:\s]*(\w+ \d{1,2},?\s*\d{4})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
        r"(\d{4}-\d{2}-\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, html_text[:3000], re.IGNORECASE)
        if m:
            ds = m.group(1).strip()
            for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(ds, fmt).date()
                except ValueError:
                    continue
    return None


# ─────────────────────────────────────────────────────────────────────────
#  EXTRACTOR: EDGARTOOLS (PRIMARY — replaces blob-cache 8-K extraction)
# ─────────────────────────────────────────────────────────────────────────

# Non-US tickers that have no SEC filings (skip silently)
_NON_US_HINT = {
    "1913", "2020", "2331", "3382", "3998", "9983",  # Asian exchanges
}

# Additional form types for specific categories
_ACTIVISM_FORMS = ["SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"]


# Shared rate limiter for SEC EDGAR (max 10 req/s)
_edgar_rate_lock = threading.Lock()
_edgar_last_call = 0.0

def _edgar_rate_limit():
    """Enforce ~8 req/s max to stay safely under EDGAR's 10 req/s limit."""
    global _edgar_last_call
    with _edgar_rate_lock:
        now = time.time()
        wait = 0.125 - (now - _edgar_last_call)
        if wait > 0:
            time.sleep(wait)
        _edgar_last_call = time.time()


def _extract_8k_item_text(filing, item_code: str, ticker: str,
                           event_date, subtype: str) -> str:
    """Extract meaningful text from a specific 8-K item using edgartools.

    Strategy:
    1. Get full filing text via obj().text()
    2. Regex-parse to find the target Item X.XX section
    3. Extract content between this item header and the next item header / SIGNATURES
    4. Truncate to ~1500 chars for OpenAI prompt (enough for good summary)
    """
    import re as _re
    try:
        eight_k = filing.obj()
        if eight_k is None:
            return None

        # Get full filing text
        full_text = eight_k.text() if callable(getattr(eight_k, 'text', None)) else None
        if not full_text:
            return None

        # Build regex for the target item (e.g. "Item 8.01" or "Item 2.02")
        escaped = _re.escape(item_code)  # e.g. "8\.01"
        # Match "Item 8.01" with optional dots/spaces
        pattern = rf'Item\s+{escaped}[\s.]*'

        # Find ALL item headers in the document
        all_headers = list(_re.finditer(
            r'Item\s+(\d+\.\d+)', full_text))

        # Find the FIRST occurrence of our target item
        target_match = None
        for h in all_headers:
            if h.group(1) == item_code:
                target_match = h
                break

        if target_match is None:
            # Fallback: try without dots (some filings use "Item 801")
            nodot = item_code.replace('.', '')
            for h in all_headers:
                if h.group(1).replace('.', '') == nodot:
                    target_match = h
                    break

        if target_match:
            start = target_match.start()
            # Find the end: next Item header that is a DIFFERENT item, or SIGNATURES
            end = len(full_text)
            for h in all_headers:
                if h.start() > start + 20 and h.group(1) != item_code:
                    end = h.start()
                    break
            # Also check for SIGNATURES block
            sig = _re.search(r'\bSIGNATURES?\b', full_text[start + 20:end])
            if sig:
                end = start + 20 + sig.start()

            section = full_text[start:end]
            section = _re.sub(r'<[^>]+>', ' ', section)
            section = _re.sub(r'\s+', ' ', section).strip()

            if len(section) > 80:
                return section[:1500] if len(section) > 1500 else section

        # Last-resort fallback: grab meaningful sentences from full text
        text = _re.sub(r'<[^>]+>', ' ', full_text)
        text = _re.sub(r'\s+', ' ', text).strip()
        # Skip SEC boilerplate — find first sentence after item mention
        item_pos = text.lower().find(f'item {item_code}'.lower())
        if item_pos > 0:
            text = text[item_pos:]
        sentences = [s.strip() for s in text.split('.') if len(s.strip()) > 40]
        if sentences:
            result = '. '.join(sentences[:5])
            return (result[:1500]) if len(result) > 1500 else result
    except Exception:
        pass
    return None


def _openai_generate_headline_and_summary(
    ticker: str,
    item_code: str,
    subtype: str,
    item_text: str,
    filing_date,
) -> tuple:
    """
    Use OpenAI API to generate:
      - headline: short (max 120 chars), factual, no hype
      - summary: 2-3 sentence situation description (max 400 chars)

    Returns (headline, summary) tuple.
    Falls back to template strings if API unavailable or raises.

    Only called for edgartools source events.
    """
    import os
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None, None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        context = item_text or f"SEC 8-K Item {item_code} ({subtype}) filed by {ticker} on {filing_date}."
        prompt = (
            f"You are a financial analyst. Analyze this SEC 8-K filing excerpt for {ticker}.\n\n"
            f"Item {item_code} — {subtype}:\n{context[:1500]}\n\n"
            f"Produce TWO outputs:\n"
            f"1. HEADLINE: One clear, factual sentence (max 120 characters). "
            f"   Start with the company ticker in brackets e.g. [{ticker}]. No hype words.\n"
            f"2. SUMMARY: 2-3 sentences explaining what happened and why it matters "
            f"   (max 400 characters). Be specific with numbers if present in the text.\n\n"
            f"Format your response EXACTLY as:\n"
            f"HEADLINE: <headline here>\n"
            f"SUMMARY: <summary here>"
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.2,
        )
        text = response.choices[0].message.content.strip()
        headline, summary = None, None
        for line in text.split("\n"):
            if line.startswith("HEADLINE:"):
                headline = line[len("HEADLINE:"):].strip()[:500]
            elif line.startswith("SUMMARY:"):
                summary = line[len("SUMMARY:"):].strip()[:500]
        return headline, summary
    except Exception as e:
        log.debug("[OpenAI] API error for %s item %s: %s", ticker, item_code, e)
        return None, None


def _process_ticker_edgartools(ticker: str, company_id: int,
                                cat_filter, accession_set: set) -> List[Dict]:
    """Process a single ticker via edgartools. Called from thread pool."""
    from edgar import Company
    events = []

    if ticker in _NON_US_HINT:
        return events

    try:
        _edgar_rate_limit()
        company = Company(ticker)
    except Exception as e:
        log.debug("  [edgartools] %s: not found in EDGAR (%s)", ticker, e)
        return events

    # ── 8-K filings ──
    try:
        _edgar_rate_limit()
        filings_8k = company.get_filings(form="8-K")
        for filing in filings_8k[:50]:
            items_str = filing.items or ""
            item_codes = [c.strip() for c in items_str.split(",") if c.strip()]
            if not item_codes:
                continue

            event_date = filing.filing_date
            homepage_url = filing.homepage_url or ""
            accession = filing.accession_no or ""

            for item_code in item_codes:
                mapping = ITEM_MAP.get(item_code)
                if mapping is None:
                    continue
                category, subtype, confidence = mapping
                if cat_filter and category not in cat_filter:
                    continue

                if item_code == "5.02" and subtype is None:
                    subtype = "Executive/Board Changes"

                item_text = _extract_8k_item_text(filing, item_code, ticker, event_date, subtype)

                # Try OpenAI for rich headline + summary
                ai_headline, ai_summary = _openai_generate_headline_and_summary(
                    ticker, item_code, subtype or "Other", item_text or "", event_date
                )

                headline = (
                    ai_headline
                    or (f"{ticker}: {subtype} (8-K Item {item_code})" if subtype
                        else f"{ticker} filed 8-K (Item {item_code})")
                )
                situation = (
                    ai_summary
                    or item_text
                    or f"{ticker} filed SEC Form 8-K on {event_date}. "
                       f"Item {item_code}: {subtype or 'Other Event'}."
                )

                events.append({
                    "company_id": company_id,
                    "ticker": ticker,
                    "event_date": event_date,
                    "event_category": category,
                    "event_subtype": subtype,
                    "headline": headline[:500],
                    "situation": situation[:500],
                    "source": "edgartools",
                    "source_ref": accession,
                    "source_detail": (
                        f"SEC Form 8-K — Item {item_code} | Accession: {accession} | "
                        + (homepage_url or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=8-K&dateb=&owner=include&count=10")
                    )[:500],
                    "confidence": confidence,
                })

            # ── 8.01 Other Events — classify as conference or general comms ──
            if "8.01" in item_codes:
                if not cat_filter or "results_announcements" in cat_filter:
                    item_text_801 = _extract_8k_item_text(filing, "8.01", ticker, event_date, "Other Corporate Communications")
                    ai_hl_801, ai_sum_801 = _openai_generate_headline_and_summary(
                        ticker, "8.01", "Other Corporate Communications", item_text_801 or "", event_date
                    )
                    events.append({
                        "company_id": company_id,
                        "ticker": ticker,
                        "event_date": event_date,
                        "event_category": "results_announcements",
                        "event_subtype": "Other Corporate Communications",
                        "headline": (ai_hl_801 or f"{ticker}: Other Corporate Communications (8-K Item 8.01)")[:500],
                        "situation": (
                            ai_sum_801
                            or item_text_801
                            or f"{ticker} filed SEC Form 8-K on {event_date} disclosing an "
                               f"Other Corporate Communication (Item 8.01)."
                        )[:500],
                        "source": "edgartools",
                        "source_ref": accession,
                        "source_detail": (
                            f"SEC Form 8-K — Item 8.01 | Accession: {accession} | "
                            + (homepage_url or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type=8-K&dateb=&owner=include&count=10")
                        )[:500],
                        "confidence": 0.60,
                    })
    except Exception as e:
        log.warning("  [edgartools] %s 8-K error: %s", ticker, e)

    # ── SC 13D/G → investor_activism ──
    if not cat_filter or "investor_activism" in cat_filter:
        for form_type in _ACTIVISM_FORMS:
            try:
                _edgar_rate_limit()
                activism_filings = company.get_filings(form=form_type)
                for filing in activism_filings[:10]:
                    filer_name = getattr(filing, "company", None) or "investor"
                    events.append({
                        "company_id": company_id,
                        "ticker": ticker,
                        "event_date": filing.filing_date,
                        "event_category": "investor_activism",
                        "event_subtype": "Activist Campaign",
                        "headline": f"{ticker}: {form_type} filed by {filer_name}"[:500],
                        "situation": (
                            f"{filer_name} filed {form_type} disclosing a significant equity "
                            f"position in {ticker}. This may indicate activist investor activity."
                        )[:500],
                        "source": "edgartools",
                        "source_ref": filing.accession_no or "",
                        "source_detail": (
                            f"SEC Filing — {form_type} | Accession: {filing.accession_no or ''} | "
                            + (filing.homepage_url or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form_type.replace(' ', '+')}&dateb=&owner=include&count=10")
                        )[:500],
                        "confidence": 0.85,
                    })
            except Exception as e:
                log.debug("  [edgartools] %s %s error: %s", ticker, form_type, e)

    # ── NT 10-K / NT 10-Q → Late Filing Notices (red_flags_distress) ──
    if not cat_filter or "red_flags_distress" in cat_filter:
        for form_type in ["NT 10-K", "NT 10-Q"]:
            try:
                _edgar_rate_limit()
                late_filings = company.get_filings(form=form_type)
                for filing in late_filings[:5]:
                    events.append({
                        "company_id": company_id,
                        "ticker": ticker,
                        "event_date": filing.filing_date,
                        "event_category": "red_flags_distress",
                        "event_subtype": "Late Filing Notice",
                        "headline": f"{ticker}: Late filing notice ({form_type})"[:500],
                        "situation": (
                            f"{ticker} filed {form_type} with the SEC, indicating the company "
                            f"requires additional time to file its periodic report."
                        )[:500],
                        "source": "edgartools",
                        "source_ref": filing.accession_no or "",
                        "source_detail": (
                            f"SEC Filing — {form_type} | Accession: {filing.accession_no or ''} | "
                            + (filing.homepage_url or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form_type.replace(' ', '+')}&dateb=&owner=include&count=10")
                        )[:500],
                        "confidence": 0.90,
                    })
            except Exception as e:
                log.debug("  [edgartools] %s %s error: %s", ticker, form_type, e)

    # ── SC TO-T / SC TO-I → Tender Offers (announced_completed_transactions) ──
    if not cat_filter or "announced_completed_transactions" in cat_filter:
        for form_type in ["SC TO-T", "SC TO-I"]:
            try:
                _edgar_rate_limit()
                tender_filings = company.get_filings(form=form_type)
                for filing in tender_filings[:5]:
                    events.append({
                        "company_id": company_id,
                        "ticker": ticker,
                        "event_date": filing.filing_date,
                        "event_category": "announced_completed_transactions",
                        "event_subtype": "Tender Offer",
                        "headline": f"{ticker}: Tender Offer ({form_type})"[:500],
                        "situation": (
                            f"{ticker} is subject to a Tender Offer ({form_type}) filed with the SEC. "
                            f"This indicates an acquisition attempt or issuer self-tender."
                        )[:500],
                        "source": "edgartools",
                        "source_ref": filing.accession_no or "",
                        "source_detail": (
                            f"SEC Filing — {form_type} | Accession: {filing.accession_no or ''} | "
                            + (filing.homepage_url or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form_type.replace(' ', '+')}&dateb=&owner=include&count=10")
                        )[:500],
                        "confidence": 0.90,
                    })
            except Exception as e:
                log.debug("  [edgartools] %s %s error: %s", ticker, form_type, e)

    # ── S-1 / 424B4 / F-1 → IPO / Public Offering (listing_trading) ──
    if not cat_filter or "listing_trading" in cat_filter:
        for form_type in ["S-1", "424B4", "424B1", "F-1"]:
            try:
                _edgar_rate_limit()
                offering_filings = company.get_filings(form=form_type)
                for filing in offering_filings[:5]:
                    subtype = "IPO/Public Offering" if form_type in ("S-1", "F-1") else "Secondary Offering"
                    events.append({
                        "company_id": company_id,
                        "ticker": ticker,
                        "event_date": filing.filing_date,
                        "event_category": "listing_trading",
                        "event_subtype": subtype,
                        "headline": f"{ticker}: {subtype} ({form_type})"[:500],
                        "situation": (
                            f"{ticker} filed {form_type} with the SEC for a public equity offering "
                            f"({'Initial Public Offering' if form_type in ('S-1', 'F-1') else 'Secondary Offering'})."
                        )[:500],
                        "source": "edgartools",
                        "source_ref": filing.accession_no or "",
                        "source_detail": (
                            f"SEC Filing — {form_type} | Accession: {filing.accession_no or ''} | "
                            + (filing.homepage_url or f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}&type={form_type.replace(' ', '+')}&dateb=&owner=include&count=10")
                        )[:500],
                        "confidence": 0.85,
                    })
            except Exception as e:
                log.debug("  [edgartools] %s %s error: %s", ticker, form_type, e)

    log.debug("  [edgartools] %s: %d events", ticker, len(events))
    return events


def extract_from_edgartools(company_map: Dict[str, int],
                            opts: Dict) -> List[Dict]:
    """Extract events from SEC filings via edgartools API (PRIMARY source).

    Uses ThreadPoolExecutor (max_workers=5) to process tickers in parallel.
    A shared rate limiter enforces ~8 req/s to stay under EDGAR's 10 req/s cap.
    """
    from edgar import set_identity
    set_identity("Coresight Research mohdsaeedafri@coresight.com")

    t0 = time.time()
    cat_filter = opts.get("categories")
    ticker_filter = opts.get("tickers")

    tickers = list(company_map.keys())
    if ticker_filter:
        tickers = [t for t in tickers if t in ticker_filter]
    total_tickers = len(tickers)
    log.info("[edgartools] Processing %d tickers with max_workers=5", total_tickers)

    all_events: List[Dict] = []
    accession_set: set = set()  # shared dedup set (not used here but passed for future use)
    errors = 0
    skipped = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_ticker = {
            executor.submit(
                _process_ticker_edgartools,
                ticker, company_map[ticker], cat_filter, accession_set
            ): ticker
            for ticker in tickers
        }

        completed = 0
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            completed += 1
            try:
                result = future.result()
                all_events.extend(result)
                if completed % 10 == 0 or completed == total_tickers:
                    log.info("  [edgartools] %d/%d tickers done, %d events so far",
                             completed, total_tickers, len(all_events))
            except Exception as e:
                log.warning("  [edgartools] %s failed: %s", ticker, e)
                errors += 1

    elapsed = time.time() - t0
    log.info("[edgartools] %d events, %d errors, %.1fs (parallel, workers=5)",
             len(all_events), errors, elapsed)
    return all_events


# ─────────────────────────────────────────────────────────────────────────
#  EXTRACTOR: EARNINGS CALENDAR
# ─────────────────────────────────────────────────────────────────────────

def extract_from_earnings(conn, company_map: Dict[str, int],
                          opts: Dict) -> List[Dict]:
    """Extract from coreiq_nasdaq_earnings_calendar."""
    t0 = time.time()
    cat_filter = opts.get("categories")
    if cat_filter and "results_announcements" not in cat_filter:
        return []

    ticker_filter = opts.get("tickers")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ticker, earnings_date, eps_actual, eps_forecast, surprise_pct,
                   fiscal_quarter_ending, report_time
            FROM coreiq_nasdaq_earnings_calendar
            WHERE earnings_date IS NOT NULL
            ORDER BY earnings_date DESC
        """)
        rows = cur.fetchall()

    events = []
    for r in rows:
        ticker = r["ticker"]
        if ticker_filter and ticker not in ticker_filter:
            continue
        company_id = company_map.get(ticker)
        if company_id is None:
            continue

        ed = r["earnings_date"]
        ea = r.get("eps_actual")
        ef = r.get("eps_forecast")
        sp = r.get("surprise_pct")
        fq = r.get("fiscal_quarter_ending") or ""

        parts = [f"{ticker} reported earnings"]
        if fq:
            parts.append(f"for {fq}")
        if ea is not None:
            parts.append(f"— EPS: ${float(ea):.2f}")
            if ef is not None:
                parts.append(f"vs est. ${float(ef):.2f}")
            if sp is not None:
                parts.append(f"(surprise: {float(sp):+.1f}%)")
        headline = " ".join(parts)

        # Situation: structured summary
        situation = None
        if ea is not None and ef is not None:
            beat = "beat" if float(ea) >= float(ef) else "missed"
            situation = f"{ticker} {beat} EPS estimates: ${float(ea):.2f} actual vs ${float(ef):.2f} est."
            if sp is not None:
                situation += f" Surprise: {float(sp):+.1f}%."
        elif ea is not None:
            situation = f"{ticker} reported EPS: ${float(ea):.2f}"
            if fq:
                situation += f" for {fq}."
        elif fq:
            situation = f"{ticker} earnings reported for {fq}."

        confidence = 0.90 if ea is not None else 0.85
        events.append({
            "company_id": company_id, "ticker": ticker,
            "event_date": ed,
            "event_category": "results_announcements",
            "event_subtype": "Announcements of Earnings",
            "headline": headline[:500],
            "situation": situation,
            "source": "earnings_cal",
            "source_detail": (
                f"Nasdaq Earnings Calendar — {ticker} "
                + (f"| Fiscal Quarter: {fq}" if fq else "")
                + f" | Report Time: {r.get('report_time') or 'TBD'}"
            )[:500],
            "source_ref": f"https://www.nasdaq.com/market-activity/stocks/{ticker.lower()}/earnings",
            "confidence": confidence,
        })

    # ── Second pass: upcoming earnings → "Earnings Release Date Announced" ──
    from datetime import date as _date
    today = _date.today()
    for r in rows:
        ticker = r["ticker"]
        if ticker_filter and ticker not in ticker_filter:
            continue
        company_id = company_map.get(ticker)
        if company_id is None:
            continue
        ed = r["earnings_date"]
        if hasattr(ed, "date"):
            ed = ed.date()
        if ed <= today:
            continue
        if r.get("eps_actual") is not None:
            continue  # already reported
        fq = r.get("fiscal_quarter_ending") or ""
        headline = f"{ticker} earnings scheduled"
        if fq:
            headline += f" for {fq}"
        rt = r.get("report_time")
        if rt:
            headline += f" ({rt})"
        events.append({
            "company_id": company_id, "ticker": ticker,
            "event_date": ed,
            "event_category": "results_announcements",
            "event_subtype": "Earnings Release Date Announced",
            "headline": headline[:500],
            "situation": (
                f"{ticker} has scheduled its earnings release for "
                f"{fq or str(ed)}. Report time: {rt or 'TBD'}. "
                f"Source: Nasdaq Earnings Calendar."
            )[:500],
            "source": "earnings_cal",
            "source_detail": (
                f"Nasdaq Earnings Calendar — {ticker} "
                + (f"| Fiscal Quarter: {fq}" if fq else "")
                + f" | Report Time: {rt or 'TBD'}"
            )[:500],
            "source_ref": f"https://www.nasdaq.com/market-activity/stocks/{ticker.lower()}/earnings",
            "confidence": 0.85,
        })

    elapsed = time.time() - t0
    log.info("[Earnings] %d events, %.1fs", len(events), elapsed)
    return events


# ─────────────────────────────────────────────────────────────────────────
#  EXTRACTOR: NEWS
# ─────────────────────────────────────────────────────────────────────────

def extract_from_news(conn, company_map: Dict[str, int],
                      opts: Dict) -> List[Dict]:
    """Extract from coreiq_av_market_news_sentiment using NEWS_RULES."""
    t0 = time.time()
    events: List[Dict] = []
    tickers = list(company_map.keys())
    ticker_filter = opts.get("tickers")
    if ticker_filter:
        tickers = [t for t in tickers if t in ticker_filter]
    cat_filter = opts.get("categories")

    active_rules = NEWS_RULES
    if cat_filter:
        active_rules = [r for r in NEWS_RULES if r["category"] in cat_filter]
    if not active_rules:
        return events

    topic_rules = [r for r in active_rules if r["topic_key"] and not r["title_keywords"]]
    keyword_rules = [r for r in active_rules if r["title_keywords"]]

    with conn.cursor() as cur:
        # Topic-based rules
        for rule in topic_rules:
            topic_key = rule["topic_key"]
            rule_count = 0
            for ticker in tickers:
                cid = company_map.get(ticker)
                if cid is None:
                    continue
                try:
                    cur.execute("""
                        SELECT ticker, time_published_utc, title, url, source_name, summary
                        FROM coreiq_av_market_news_sentiment
                        WHERE ticker = %s AND topics_json LIKE %s
                        ORDER BY time_published_utc DESC LIMIT 20
                    """, (ticker, f'%"topic": "{topic_key}",%'))
                    rows = cur.fetchall()
                except Exception as e:
                    log.debug("News topic error %s/%s: %s", ticker, topic_key, e)
                    continue
                for r in rows:
                    pub = r["time_published_utc"]
                    if pub is None:
                        continue
                    title = (r.get("title") or "")[:500]
                    if not title:
                        continue
                    events.append({
                        "company_id": cid, "ticker": ticker,
                        "event_date": pub.date() if hasattr(pub, "date") else pub,
                        "event_category": rule["category"],
                        "event_subtype": rule["subtype"],
                        "headline": title,
                        "situation": (r.get("summary") or "")[:500] or None,
                        "source": "news",
                        "source_detail": (r.get("source_name") or "Alpha Vantage News")[:500],
                        "source_ref": r.get("url") or "",
                        "source_name": r.get("source_name"),
                        "confidence": rule["confidence"],
                    })
                    rule_count += 1
            log.info("  [News] topic=%s: %d events", topic_key, rule_count)

        # Keyword-based rules
        ticker_sql = ", ".join(f"'{t}'" for t in tickers)
        for rule in keyword_rules:
            kws = rule["title_keywords"]
            kw_likes = " OR ".join(
                f"LOWER(title) LIKE '%{kw.replace(chr(39), chr(39)*2)}%'" for kw in kws)
            topic_clause = ""
            if rule.get("topic_key"):
                topic_clause = (f" AND topics_json LIKE "
                                f"'%\"topic\": \"{rule['topic_key']}\",%'")
            query = f"""
                SELECT ticker, time_published_utc, title, url, source_name, summary
                FROM coreiq_av_market_news_sentiment
                WHERE ticker IN ({ticker_sql}) {topic_clause}
                  AND ({kw_likes})
                ORDER BY time_published_utc DESC LIMIT 2000
            """
            try:
                cur.execute(query)
                rows = cur.fetchall()
            except Exception as e:
                log.warning("News kw error %s: %s", rule["subtype"], e)
                continue

            rule_count = 0
            for r in rows:
                tk = r["ticker"]
                cid = company_map.get(tk)
                if cid is None:
                    continue
                pub = r["time_published_utc"]
                if pub is None:
                    continue
                title = (r.get("title") or "")[:500]
                if not title:
                    continue
                events.append({
                    "company_id": cid, "ticker": tk,
                    "event_date": pub.date() if hasattr(pub, "date") else pub,
                    "event_category": rule["category"],
                    "event_subtype": rule["subtype"],
                    "headline": title,
                    "situation": (r.get("summary") or "")[:500] or None,
                    "source": "news",
                    "source_detail": (r.get("source_name") or "Alpha Vantage News")[:500],
                    "source_ref": r.get("url") or "",
                    "source_name": r.get("source_name"),
                    "confidence": rule["confidence"],
                })
                rule_count += 1
            log.info("  [News] kw=%s: %d events", rule["subtype"], rule_count)

    elapsed = time.time() - t0
    log.info("[News] %d events total, %.1fs", len(events), elapsed)
    return events


# ─────────────────────────────────────────────────────────────────────────
#  EXTRACTOR: EARNINGS ESTIMATES (company_forecasts_ratings)
# ─────────────────────────────────────────────────────────────────────────

def extract_from_estimates(conn, company_map: Dict[str, int],
                           opts: Dict) -> List[Dict]:
    """Detect significant estimate revisions → company_forecasts_ratings events."""
    t0 = time.time()
    cat_filter = opts.get("categories")
    if cat_filter and "company_forecasts_ratings" not in cat_filter:
        return []
    ticker_filter = opts.get("tickers")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT ticker, estimate_date, horizon,
                   eps_est_avg, eps_est_avg_30d_ago,
                   rev_est_avg, eps_est_analyst_count
            FROM coreiq_av_financials_earnings_estimates
            WHERE eps_est_avg IS NOT NULL
              AND eps_est_avg_30d_ago IS NOT NULL
              AND horizon IN ('fiscal quarter', 'next fiscal quarter')
            ORDER BY estimate_date DESC
        """)
        rows = cur.fetchall()

    events = []
    for r in rows:
        ticker = r["ticker"]
        if ticker_filter and ticker not in ticker_filter:
            continue
        cid = company_map.get(ticker)
        if cid is None:
            continue

        cur_est = float(r["eps_est_avg"])
        old_est = float(r["eps_est_avg_30d_ago"])
        if old_est == 0:
            continue
        pct_chg = (cur_est - old_est) / abs(old_est) * 100

        # Only create events for significant revisions (>5%)
        if abs(pct_chg) < 5.0:
            continue

        direction = "raised" if pct_chg > 0 else "lowered"
        headline = (f"{ticker} EPS estimate {direction}: "
                    f"${cur_est:.2f} (was ${old_est:.2f}, {pct_chg:+.1f}% change)")
        situation = (f"Consensus EPS estimate for {r['horizon']} "
                     f"{direction} from ${old_est:.2f} to ${cur_est:.2f} "
                     f"({int(r.get('eps_est_analyst_count') or 0)} analysts)")

        events.append({
            "company_id": cid, "ticker": ticker,
            "event_date": r["estimate_date"],
            "event_category": "company_forecasts_ratings",
            "event_subtype": "Estimate Revision",
            "headline": headline[:500],
            "situation": situation[:500],
            "source": "estimates",
            "source_detail": (
                f"Alpha Vantage Estimates — {ticker} | Horizon: {r['horizon']} "
                f"| Estimate Date: {r['estimate_date']} "
                f"| Analysts: {int(r.get('eps_est_analyst_count') or 0)}"
            )[:500],
            "source_ref": f"av_estimates/{ticker}/{r['estimate_date']}",
            "confidence": 0.80,
        })

    elapsed = time.time() - t0
    log.info("[Estimates] %d events, %.1fs", len(events), elapsed)
    return events


# ─────────────────────────────────────────────────────────────────────────
#  DEDUPE
# ─────────────────────────────────────────────────────────────────────────

def dedupe_events(events: List[Dict]) -> List[Dict]:
    """Cross-source dedupe: (ticker, category, date ±1 day) → keep highest confidence."""
    t0 = time.time()
    buckets: Dict[Tuple, List[Dict]] = {}
    for ev in events:
        key = (ev["ticker"], ev["event_category"])
        matched = False
        for bkey in list(buckets.keys()):
            if bkey[:2] == key:
                bucket_date = bkey[2]
                if abs((ev["event_date"] - bucket_date).days) <= 1:
                    buckets[bkey].append(ev)
                    matched = True
                    break
        if not matched:
            buckets[(ev["ticker"], ev["event_category"], ev["event_date"])] = [ev]

    result = []
    for group in buckets.values():
        best = max(group, key=lambda e: (
            e["confidence"], SOURCE_PRIORITY.get(e["source"], 0)))
        result.append(best)

    elapsed = time.time() - t0
    log.info("[Dedupe] %d → %d events (%.1fs)", len(events), len(result), elapsed)
    return result


# ─────────────────────────────────────────────────────────────────────────
#  UPSERT
# ─────────────────────────────────────────────────────────────────────────

def upsert_events(conn, events: List[Dict], batch_id: str,
                  mode: str = "full", dry_run: bool = False,
                  batch_size: int = 500) -> Dict:
    """Upsert events using event_hash for idempotency.

    mode='full': DELETE all existing rows, then INSERT.
    mode='incremental': INSERT … ON DUPLICATE KEY UPDATE.
    """
    stats = {"inserted": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}

    if dry_run:
        log.info("[DRY RUN] Would upsert %d events", len(events))
        for ev in events[:10]:
            log.info("  %s | %s | %s | %s | %s",
                     ev["ticker"], ev["event_date"], ev["event_category"],
                     ev["event_subtype"], (ev.get("headline") or "")[:60])
        stats["skipped"] = len(events)
        return stats

    t0 = time.time()

    with conn.cursor() as cur:
        if mode == "full":
            cur.execute("DELETE FROM coreiq_company_events")
            stats["deleted"] = cur.rowcount
            log.info("[Upsert] Cleared %d existing events (full mode)", stats["deleted"])

            insert_sql = """
                INSERT INTO coreiq_company_events
                    (company_id, ticker, event_date, event_category, event_subtype,
                     headline, situation, source, source_ref, source_detail,
                     confidence, event_hash, etl_batch_id, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            """
            batch = []
            for ev in events:
                batch.append((
                    ev["company_id"], ev["ticker"], ev["event_date"],
                    ev["event_category"], ev["event_subtype"],
                    ev.get("headline"), ev.get("situation"),
                    ev["source"], ev.get("source_ref"), ev.get("source_detail"),
                    ev["confidence"], ev["event_hash"], batch_id,
                ))
            for i in range(0, len(batch), batch_size):
                chunk = batch[i:i + batch_size]
                try:
                    cur.executemany(insert_sql, chunk)
                    stats["inserted"] += len(chunk)
                except Exception as e:
                    log.error("Insert batch error at offset %d: %s", i, e)
                    stats["errors"] += len(chunk)

        else:  # incremental
            upsert_sql = """
                INSERT INTO coreiq_company_events
                    (company_id, ticker, event_date, event_category, event_subtype,
                     headline, situation, source, source_ref, source_detail,
                     confidence, event_hash, etl_batch_id, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON DUPLICATE KEY UPDATE
                    headline = IF(VALUES(confidence) > confidence, VALUES(headline), headline),
                    situation = IF(VALUES(confidence) > confidence, VALUES(situation), situation),
                    confidence = GREATEST(confidence, VALUES(confidence)),
                    source_detail = COALESCE(VALUES(source_detail), source_detail),
                    updated_at = NOW(),
                    etl_batch_id = VALUES(etl_batch_id)
            """
            batch = []
            for ev in events:
                batch.append((
                    ev["company_id"], ev["ticker"], ev["event_date"],
                    ev["event_category"], ev["event_subtype"],
                    ev.get("headline"), ev.get("situation"),
                    ev["source"], ev.get("source_ref"), ev.get("source_detail"),
                    ev["confidence"], ev["event_hash"], batch_id,
                ))
            for i in range(0, len(batch), batch_size):
                chunk = batch[i:i + batch_size]
                try:
                    cur.executemany(upsert_sql, chunk)
                    # executemany rowcount = 1 per insert, 2 per update
                    stats["inserted"] += sum(1 for _ in chunk)  # approximation; exact via rowcount not reliable with executemany
                except Exception as e:
                    log.error("Upsert batch error at offset %d: %s", i, e)
                    stats["errors"] += len(chunk)

    conn.commit()
    elapsed = time.time() - t0
    log.info("[Upsert] inserted=%d updated=%d deleted=%d errors=%d (%.1fs)",
             stats["inserted"], stats["updated"], stats["deleted"],
             stats["errors"], elapsed)
    return stats


# ─────────────────────────────────────────────────────────────────────────
#  AUDIT + REPORTING
# ─────────────────────────────────────────────────────────────────────────

def write_batch_audit(conn, batch_id: str, stats: Dict, opts: Dict,
                      started_at: datetime, duration: float,
                      coverage: Dict):
    """Write a row to coreiq_etl_audit_log."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO coreiq_etl_audit_log
                    (batch_id, run_mode, env, sources_run, categories_run,
                     total_extracted, total_deduped, total_inserted, total_updated,
                     total_skipped, total_errors, duration_seconds,
                     started_at, finished_at, coverage_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            """, (
                batch_id, opts.get("mode", "full"), opts.get("env", "local"),
                ",".join(opts.get("sources", ALL_SOURCES)),
                ",".join(opts.get("categories") or ALL_CATEGORIES),
                stats.get("total_extracted", 0), stats.get("total_deduped", 0),
                stats.get("inserted", 0), stats.get("updated", 0),
                stats.get("skipped", 0), stats.get("errors", 0),
                round(duration, 2), started_at,
                json.dumps(coverage, default=str),
            ))
        conn.commit()
        log.info("[Audit] Batch %s logged", batch_id[:8])
    except Exception as e:
        log.warning("Failed to write audit log: %s", e)


def update_watermarks(conn, batch_id: str, sources: List[str]):
    """Update watermark timestamps after successful run."""
    now = datetime.utcnow()
    try:
        with conn.cursor() as cur:
            for src in sources:
                cur.execute("""
                    INSERT INTO coreiq_etl_watermarks (source, last_watermark, last_batch_id)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        last_watermark = VALUES(last_watermark),
                        last_batch_id = VALUES(last_batch_id)
                """, (src, now, batch_id))
        conn.commit()
    except Exception as e:
        log.warning("Failed to update watermarks: %s", e)


def get_before_counts(conn) -> Dict:
    """Capture category counts before ETL run."""
    counts = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT event_category, COUNT(*) AS cnt
            FROM coreiq_company_events GROUP BY event_category
        """)
        for r in cur.fetchall():
            counts[r["event_category"]] = r["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM coreiq_company_events")
        counts["__total__"] = cur.fetchone()["cnt"]
    return counts


def print_coverage_report(conn, before_counts: Dict):
    """Print detailed category coverage report."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT event_category, source, COUNT(*) AS cnt
            FROM coreiq_company_events GROUP BY event_category, source
            ORDER BY event_category, cnt DESC
        """)
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS cnt FROM coreiq_company_events")
        total = cur.fetchone()["cnt"]

    # Aggregate
    cat_source = {}
    cat_total = {}
    for r in rows:
        cat = r["event_category"]
        cat_source.setdefault(cat, {})[r["source"]] = r["cnt"]
        cat_total[cat] = cat_total.get(cat, 0) + r["cnt"]

    print("\n" + "=" * 80)
    print("  COVERAGE REPORT")
    print("=" * 80)
    print(f"{'Category':<40} {'Before':>7} {'After':>7} {'Delta':>7}   Source Breakdown")
    print("-" * 80)
    for cat in sorted(ALL_CATEGORIES):
        before = before_counts.get(cat, 0)
        after = cat_total.get(cat, 0)
        delta = after - before
        sources = cat_source.get(cat, {})
        src_str = ", ".join(f"{s}:{c}" for s, c in sorted(sources.items()))
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        print(f"  {cat:<38} {before:>7} {after:>7} {delta_str:>7}   {src_str}")

    before_total = before_counts.get("__total__", 0)
    delta_total = total - before_total
    delta_str = f"+{delta_total}" if delta_total > 0 else str(delta_total)
    print("-" * 80)
    print(f"  {'TOTAL':<38} {before_total:>7} {total:>7} {delta_str:>7}")
    print("=" * 80)

    # Field population
    with conn.cursor() as cur:
        for col in ["headline", "event_subtype", "situation", "source",
                     "source_detail", "related_tickers", "confidence", "event_hash"]:
            cur.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN {col} IS NOT NULL AND {col} != '' THEN 1 ELSE 0 END) AS pop
                FROM coreiq_company_events
            """)
            r = cur.fetchone()
            pct = round(100 * r["pop"] / r["total"], 1) if r["total"] > 0 else 0
            print(f"  {col:<20} {pct:>5.1f}% ({r['pop']}/{r['total']})")
    print("=" * 80)

    return {"total": total, "by_category": cat_total, "by_source": cat_source}


# ─────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────

def _extract_single_source(source_name, extract_fn, extract_args,
                            conn_factory, needs_conn=False):
    """Extract + normalize + dedup events for one source (no DB writes)."""
    log.info("[PARALLEL] Extracting: %s", source_name)
    t0 = time.time()
    try:
        if needs_conn:
            read_conn = conn_factory()
            try:
                events = extract_fn(read_conn, *extract_args)
            finally:
                read_conn.close()
        else:
            events = extract_fn(*extract_args)
        for ev in events:
            normalize_event(ev)
        events = dedupe_events(events)
        elapsed = time.time() - t0
        log.info("[PARALLEL] %s: %d events after dedup (%.1fs)",
                 source_name, len(events), elapsed)
        return source_name, events, None
    except Exception as e:
        log.error("[PARALLEL] %s extraction FAILED: %s", source_name, e,
                  exc_info=True)
        return source_name, [], str(e)


def main():
    parser = argparse.ArgumentParser(
        description="Key Developments ETL — Production Canonical Script")
    parser.add_argument("--env", choices=["local", "staging"], default="local")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", choices=["full", "incremental"], default="full")
    parser.add_argument("--sources", type=str, default=None,
                        help="Comma-separated sources: edgartools,earnings,news,estimates")
    parser.add_argument("--categories", type=str, default=None,
                        help="Comma-separated categories to process")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Comma-separated tickers to filter")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)-5s %(message)s",
                        datefmt="%H:%M:%S")

    # Safety guard
    if args.env in ("staging",) and not args.dry_run and not args.yes:
        answer = input(
            f"⚠️  Write to {args.env.upper()} DB? [y/N] ")
        if answer.strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)

    started_at = datetime.utcnow()
    batch_id = generate_batch_id()
    project_root = Path(__file__).resolve().parent.parent

    sources = args.sources.split(",") if args.sources else ALL_SOURCES
    categories = args.categories.split(",") if args.categories else None
    tickers = set(args.ticker.split(",")) if args.ticker else None

    opts = {
        "env": args.env, "mode": args.mode, "dry_run": args.dry_run,
        "sources": sources, "categories": categories, "tickers": tickers,
        "batch_size": args.batch_size,
    }

    log.info("Key Developments ETL — batch=%s env=%s mode=%s dry_run=%s",
             batch_id[:8], args.env, args.mode, args.dry_run)
    log.info("Sources: %s  Categories: %s  Tickers: %s",
             sources, categories or "ALL", tickers or "ALL")

    conn = get_connection(args.env)
    log.info("Connected to %s DB", args.env)

    company_map = load_company_map(conn)
    log.info("Loaded %d companies", len(company_map))

    before_counts = get_before_counts(conn)
    log.info("Before: %d total events", before_counts.get("__total__", 0))

    # ── Full mode: clear table first, then all inserts are incremental ──
    if args.mode == "full" and not args.dry_run:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM coreiq_company_events")
            deleted = cur.rowcount
            conn.commit()
        log.info("[PARALLEL] Cleared %d existing events (full mode)", deleted)

    # ── Build source tasks ──
    # needs_conn=True → _run_single_source creates a fresh per-thread connection
    source_tasks = []
    if "edgartools" in sources:
        source_tasks.append(("edgartools", extract_from_edgartools,
                             [company_map, opts], False))
    if "earnings" in sources:
        source_tasks.append(("earnings", extract_from_earnings,
                             [company_map, opts], True))
    if "news" in sources:
        source_tasks.append(("news", extract_from_news,
                             [company_map, opts], True))
    if "estimates" in sources:
        source_tasks.append(("estimates", extract_from_estimates,
                             [company_map, opts], True))

    def _conn_factory():
        return get_connection(args.env)

    # ── Run all sources: extract in PARALLEL, upsert SEQUENTIALLY ──
    log.info("── Parallel extraction for %d sources ──", len(source_tasks))
    extracted = {}  # source_name → events list
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                _extract_single_source,
                name, fn, fn_args, _conn_factory, needs_conn=needs_conn
            ): name
            for name, fn, fn_args, needs_conn in source_tasks
        }
        for future in concurrent.futures.as_completed(futures):
            name, events, err = future.result()
            extracted[name] = events
            if err:
                errors[name] = err
            log.info("[PARALLEL] Extracted: %s → %d events, error=%s",
                     name, len(events), err)

    # ── Sequential upsert (avoids deadlocks) ──
    log.info("── Sequential upsert for %d sources ──", len(extracted))
    results = []
    for name, events in extracted.items():
        if not events or args.dry_run:
            results.append({"source": name, "events": len(events),
                           "error": errors.get(name)})
            continue
        t0 = time.time()
        try:
            write_conn = _conn_factory()
            try:
                upsert_events(write_conn, events, batch_id,
                              mode="incremental", dry_run=False,
                              batch_size=args.batch_size)
                write_conn.commit()
                log.info("[UPSERT] %s: %d events in %.1fs",
                         name, len(events), time.time() - t0)
            finally:
                write_conn.close()
            results.append({"source": name, "events": len(events), "error": None})
        except Exception as e:
            log.error("[UPSERT] %s FAILED: %s", name, e)
            results.append({"source": name, "events": len(events), "error": str(e)})

    log.info("=== PARALLEL ETL COMPLETE ===")
    total_deduped = 0
    for r in results:
        status = "OK" if not r["error"] else f"FAILED: {r['error']}"
        log.info("  %-15s %4d events  %s", r["source"], r["events"], status)
        total_deduped += r["events"]
    total_extracted = total_deduped

    upsert_stats = {"inserted": total_deduped, "updated": 0, "deleted": 0,
                    "skipped": 0, "errors": 0}

    duration = (datetime.utcnow() - started_at).total_seconds()

    # ── Coverage report ──
    coverage = print_coverage_report(conn, before_counts)

    # ── Audit log ──
    audit_stats = {
        "total_extracted": total_extracted,
        "total_deduped": total_deduped,
        **upsert_stats,
    }
    if not args.dry_run:
        write_batch_audit(conn, batch_id, audit_stats, opts, started_at,
                          duration, coverage)
        update_watermarks(conn, batch_id, sources)

    conn.close()

    log.info("Done. batch=%s duration=%.1fs extracted=%d deduped=%d "
             "inserted=%d updated=%d errors=%d",
             batch_id[:8], duration, total_extracted, total_deduped,
             upsert_stats.get("inserted", 0), upsert_stats.get("updated", 0),
             upsert_stats.get("errors", 0))


if __name__ == "__main__":
    main()
