#!/usr/bin/env python3
"""
extract_credit_ratings.py
--------------------------
Extracts credit rating data from SEC 10-K filings and loads into coreiq_filing_metrics.

Pipeline:
  Step 1: Read section text from SECTION_CACHE.json (pre-built by enrich_from_edgartools)
  Step 2: Apply enhanced regex patterns to extract credit rating candidates
  Step 3: LLM-verify each candidate with GPT-4o-mini / Gemini for zero-error guarantee
  Step 4: Insert verified results into coreiq_filing_metrics table (source='credit_rating')

Results are inserted into coreiq_filing_metrics with:
  - source          = 'credit_rating'
  - statement_type  = 'Credit Rating'
  - label           = 'Credit Rating (S&P: BBB / Moody's: Baa2)'
  - unit_ref        = 'rating'
  - concept         = 'coreiq:CreditRating'

Usage:
    python scripts/extract_credit_ratings.py                    # All filings
    python scripts/extract_credit_ratings.py M                  # Specific ticker
    python scripts/extract_credit_ratings.py M 2025             # Specific ticker+year
    python scripts/extract_credit_ratings.py M 2025 --no-llm    # Skip LLM
    python scripts/extract_credit_ratings.py --force             # Re-extract

This script is integrated as Step 7 in run_pipeline.py.
"""

import os
import re
import sys
import json
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Path setup ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════

FILINGS_DIR = PROJECT_ROOT / "data" / "filings"

# Sections to search for credit ratings (in priority order)
CREDIT_SECTIONS = [
    "part_ii_item_7",   # Item 7 — MD&A (Liquidity) — best source, rating tables
    "part_i_item_1a",   # Item 1A — Risk Factors — specific rating mentions
    "part_i_item_1",    # Item 1 — Business — sometimes has rating info
    "part_ii_item_8",   # Item 8 — Financial Statements — debt notes
]

# ── S&P Rating Scale (for numeric scoring) ──
SP_RATING_SCORES = {
    "AAA": 21, "AA+": 20, "AA": 19, "AA-": 18,
    "A+": 17, "A": 16, "A-": 15,
    "BBB+": 14, "BBB": 13, "BBB-": 12,
    "BB+": 11, "BB": 10, "BB-": 9,
    "B+": 8, "B": 7, "B-": 6,
    "CCC+": 5, "CCC": 4, "CCC-": 3,
    "CC": 2, "C": 1, "D": 0,
}

# ── Moody's → S&P mapping ──
MOODYS_TO_SP = {
    "Aaa": "AAA", "Aa1": "AA+", "Aa2": "AA", "Aa3": "AA-",
    "A1": "A+", "A2": "A", "A3": "A-",
    "Baa1": "BBB+", "Baa2": "BBB", "Baa3": "BBB-",
    "Ba1": "BB+", "Ba2": "BB", "Ba3": "BB-",
    "B1": "B+", "B2": "B", "B3": "B-",
    "Caa1": "CCC+", "Caa2": "CCC", "Caa3": "CCC-",
    "Ca": "CC", "C": "C",
}

# ── All valid rating pattern tokens ──
SP_RATINGS_RE = r'(?:AAA|AA[\+\-]?|A[\+\-]?|BBB[\+\-]?|BB[\+\-]?|B[\+\-]?|CCC[\+\-]?|CC|C|D)'
MOODYS_RATINGS_RE = r'(?:Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca|C)'
OUTLOOK_RE = r'(?:[Ss]table|[Nn]egative|[Pp]ositive|[Ww]atch)'

# Enhanced regex patterns for credit rating extraction
CREDIT_RATING_PATTERNS = [
    # Pattern 1: "S&P/Standard & Poor's [rates/rated/rating/assigns] ... BBB"
    (r"""(?:S&P|Standard\s*[&\s]+Poor'?s?)[^.]{0,100}?"""
     r"""(?:rat(?:es?|ed|ing)|assign(?:s|ed)|affirm(?:s|ed)|credit\s+rating)[^.]{0,80}?"""
     rf"({SP_RATINGS_RE})",
     "sp_rating"),

    # Pattern 2: "Moody's [rates/rated/rating] ... Baa2"
    (r"""(?:Moody'?s)[^.]{0,100}?"""
     r"""(?:rat(?:es?|ed|ing)|assign(?:s|ed)|affirm(?:s|ed)|credit\s+rating)[^.]{0,80}?"""
     rf"({MOODYS_RATINGS_RE})",
     "moodys_rating"),

    # Pattern 3: "Fitch [rates/rated/rating] ... BBB"
    (r'(?:Fitch)[^.]{0,100}?'
     r'(?:rat(?:es?|ed|ing)|assign(?:s|ed)|affirm(?:s|ed)|credit\s+rating)[^.]{0,80}?'
     rf'({SP_RATINGS_RE})',
     "fitch_rating"),

    # Pattern 4: "credit rating of BBB" / "credit rating is BBB"
    (rf'credit\s+rating[^.]*?\b({SP_RATINGS_RE})\b',
     "credit_rating_of_sp"),

    # Pattern 5: "credit rating ... Baa2" (Moody's format)
    (rf'credit\s+rating[^.]*?\b({MOODYS_RATINGS_RE})\b',
     "credit_rating_of_moodys"),

    # Pattern 6: Rating table format — "Standard & Poor's  BBB+  Stable"
    (rf'(?:Standard|S&P)[^.]*?({SP_RATINGS_RE})[^.]*?({OUTLOOK_RE})',
     "sp_table"),

    # Pattern 7: Rating table — "Moody's  A3  Stable"
    (rf'(?:Moody)[^.]*?({MOODYS_RATINGS_RE})[^.]*?({OUTLOOK_RE})',
     "moodys_table"),

    # Pattern 8: Downgrade/Upgrade — "downgraded from BB to BB-"
    (rf'(?:downgrad|upgrad)\w*[^.]*?(?:from|to)\s+({SP_RATINGS_RE})',
     "upgrade_downgrade"),

    # Pattern 9: "Fitch ... BBB ... Stable/Negative"
    (rf'(?:Fitch)[^.]*?({SP_RATINGS_RE})[^.]*?({OUTLOOK_RE})',
     "fitch_table"),
]


# ══════════════════════════════════════════════════════════════════════════
#  DB CONNECTION
# ══════════════════════════════════════════════════════════════════════════

def get_engine():
    user = os.getenv("DB_USER", "root")
    pw   = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    db   = os.getenv("DB_NAME", "chainxydata_stg")
    return create_engine(f"mysql+pymysql://{user}:{pw}@{host}:{port}/{db}")


# ══════════════════════════════════════════════════════════════════════════
#  STEP 1: Load Section Text
# ══════════════════════════════════════════════════════════════════════════

def load_section_text(ticker: str, year: int, doc_type: str = "10-K") -> Dict[str, str]:
    """Load section text from SECTION_CACHE.json."""
    for dt in [doc_type, doc_type.replace("-", "")]:
        cache_path = FILINGS_DIR / ticker / str(year) / dt / "SECTION_CACHE.json"
        if cache_path.exists():
            with open(cache_path) as f:
                data = json.load(f)
            sections = {k: v for k, v in data.items() if not k.startswith("_")}
            if sections:
                return sections

    # Fallback: direct EdgarTools extraction
    for dt in [doc_type, doc_type.replace("-", "")]:
        html_path = FILINGS_DIR / ticker / str(year) / dt / "filing.html"
        if html_path.exists():
            return _extract_sections_live(html_path)

    return {}


def _extract_sections_live(html_path: Path) -> Dict[str, str]:
    """Extract sections directly from filing.html using EdgarTools."""
    try:
        from edgar.documents import HTMLParser
        parser = HTMLParser()
        doc = parser.parse_file(str(html_path))

        sections = {}
        for key in CREDIT_SECTIONS:
            try:
                txt = doc.get_sec_section(key)
                if txt and len(txt.strip()) > 200:
                    sections[key] = txt
            except Exception:
                pass
        return sections
    except Exception as e:
        print(f"    WARNING: EdgarTools extraction failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════
#  STEP 2: Regex Extraction
# ══════════════════════════════════════════════════════════════════════════

def extract_credit_ratings_regex(sections: Dict[str, str]) -> List[dict]:
    """
    Apply credit rating regex patterns across all sections.
    Returns list of candidate dicts with rating details.
    """
    candidates = []
    seen_ratings = set()  # (agency, rating) dedup

    for section_key in CREDIT_SECTIONS:
        section_text = sections.get(section_key, "")
        if not section_text:
            continue

        for pattern, pattern_name in CREDIT_RATING_PATTERNS:
            for match in re.finditer(pattern, section_text, re.IGNORECASE):
                groups = match.groups()

                rating = None
                outlook = None
                agency = None

                # Determine agency from pattern name
                if "sp" in pattern_name or pattern_name in ("credit_rating_of_sp", "upgrade_downgrade"):
                    agency = "S&P"
                elif "moodys" in pattern_name or "credit_rating_of_moodys" in pattern_name:
                    agency = "Moody's"
                elif "fitch" in pattern_name:
                    agency = "Fitch"

                # Extract rating and outlook from groups
                for g in groups:
                    if not g:
                        continue
                    # Check if it's a rating value
                    if re.match(rf'^{SP_RATINGS_RE}$', g) or re.match(rf'^{MOODYS_RATINGS_RE}$', g):
                        if rating is None:
                            rating = g
                    elif re.match(rf'^{OUTLOOK_RE}$', g, re.IGNORECASE):
                        outlook = g.capitalize()

                if not rating:
                    continue

                # Normalize: determine if S&P or Moody's format
                is_moodys = bool(re.match(rf'^{MOODYS_RATINGS_RE}$', rating))
                sp_equivalent = MOODYS_TO_SP.get(rating, rating) if is_moodys else rating
                if is_moodys and agency is None:
                    agency = "Moody's"
                elif not is_moodys and agency is None:
                    agency = "S&P"

                dedup_key = (agency, rating)
                if dedup_key in seen_ratings:
                    continue
                seen_ratings.add(dedup_key)

                # Extract surrounding sentence for source reference
                match_start = max(0, match.start() - 100)
                match_end = min(len(section_text), match.end() + 150)
                context = section_text[match_start:match_end].strip()
                context = re.sub(r'\s+', ' ', context)

                candidates.append({
                    "agency": agency,
                    "rating": rating,
                    "sp_equivalent": sp_equivalent,
                    "numeric_score": SP_RATING_SCORES.get(sp_equivalent, 0),
                    "outlook": outlook,
                    "is_moodys": is_moodys,
                    "source_sentence": context[:400],
                    "section": section_key,
                    "pattern": pattern_name,
                })

    # Sort by numeric score descending (higher rating first)
    candidates.sort(key=lambda c: c["numeric_score"], reverse=True)
    return candidates


# ══════════════════════════════════════════════════════════════════════════
#  STEP 3: LLM Verification
# ══════════════════════════════════════════════════════════════════════════

LLM_VERIFY_PROMPT = """You are a financial analyst extracting credit ratings from SEC 10-K filings.

COMPANY: {ticker}
FISCAL YEAR: {year}

Below is text from the company's 10-K filing. Extract ALL credit ratings mentioned.

The regex pre-extraction found: {regex_results}

For EACH rating agency mentioned (S&P, Moody's, Fitch), extract:
1. The agency name
2. Long-term debt/issuer rating
3. Short-term/commercial paper rating (if any)
4. Outlook (Stable/Negative/Positive/Watch)
5. The exact sentence from the text

Return ONLY this JSON:
{{
  "found": true,
  "ratings": [
    {{
      "agency": "S&P",
      "long_term_rating": "BBB",
      "short_term_rating": "A-2",
      "outlook": "Stable",
      "source_sentence": "<exact text>"
    }},
    {{
      "agency": "Moody's",
      "long_term_rating": "Baa2",
      "short_term_rating": "P-2",
      "outlook": "Stable",
      "source_sentence": "<exact text>"
    }}
  ],
  "investment_grade": true,
  "confidence": 0.95,
  "notes": "any clarification"
}}

If the company does NOT disclose any credit rating, return:
{{
  "found": false,
  "ratings": [],
  "investment_grade": null,
  "confidence": 1.0,
  "notes": "No credit rating disclosed in this filing"
}}

TEXT:
{section_text}
"""


def verify_with_llm(
    ticker: str, year: int,
    sections: Dict[str, str],
    regex_candidates: List[dict],
) -> Optional[dict]:
    """
    Send section text + regex results to LLM for verification.
    Tries OpenAI GPT-4o-mini first, falls back to Gemini if unavailable.
    """
    section_text = ""
    for key in CREDIT_SECTIONS:
        if key in sections:
            section_text += f"\n--- {key} ---\n{sections[key][:3000]}\n"
    section_text = section_text[:6000]

    regex_summary = json.dumps([
        {"agency": c["agency"], "rating": c["rating"], "outlook": c.get("outlook"),
         "pattern": c["pattern"]}
        for c in regex_candidates[:8]
    ], indent=2) if regex_candidates else "No regex matches found"

    prompt = LLM_VERIFY_PROMPT.format(
        ticker=ticker,
        year=year,
        regex_results=regex_summary,
        section_text=section_text,
    )

    system_msg = (
        "You are a financial analyst specializing in SEC filings and credit ratings. "
        "Extract credit ratings with 100% accuracy. "
        "Return ONLY valid JSON, no markdown fences, no explanation."
    )

    # ── Try OpenAI first ──
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        result = _call_openai(openai_key, system_msg, prompt)
        if result is not None:
            return result
        print("    ⚠️  OpenAI call failed, trying Gemini fallback...")

    # ── Fallback to Gemini ──
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        result = _call_gemini(gemini_key, system_msg, prompt)
        if result is not None:
            return result

    if not openai_key and not gemini_key:
        print("    ⚠️  No LLM API key set (OPENAI_API_KEY or GEMINI_API_KEY) — skipping verification")

    return None


def _call_openai(api_key: str, system_msg: str, prompt: str) -> Optional[dict]:
    """Call OpenAI GPT-4o-mini for credit rating verification."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)
        if "found" in result:
            return result
        return None
    except ImportError:
        print("    ⚠️  openai package not installed")
        return None
    except Exception as e:
        print(f"    ❌ OpenAI API error: {e}")
        return None


def _call_gemini(api_key: str, system_msg: str, prompt: str) -> Optional[dict]:
    """Call Google Gemini for credit rating verification."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)

        model = genai.GenerativeModel(
            "gemini-2.0-flash",
            system_instruction=system_msg,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0,
            ),
        )

        response = model.generate_content(prompt)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        if "found" in result:
            return result
        return None
    except ImportError:
        print("    ⚠️  google-generativeai package not installed")
        try:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "google-generativeai"])
            return _call_gemini(api_key, system_msg, prompt)
        except Exception:
            print("    ❌ Failed to install google-generativeai")
            return None
    except Exception as e:
        print(f"    ❌ Gemini API error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
#  STEP 4: Insert into DB
# ══════════════════════════════════════════════════════════════════════════

def insert_credit_rating_to_db(
    conn,
    ticker: str,
    fiscal_year: int,
    doc_type: str,
    ratings: List[dict],
    investment_grade: Optional[bool],
    confidence: float,
    extraction_method: str,
    notes: str = "",
):
    """
    Insert credit rating(s) into coreiq_filing_metrics table.
    One row per rating agency (S&P, Moody's, Fitch).
    """
    # Delete existing credit_rating entries for this filing (idempotent)
    conn.execute(text("""
        DELETE FROM coreiq_filing_metrics
        WHERE ticker = :ticker AND fiscal_year = :fy AND doc_type = :dt
          AND source = 'credit_rating'
    """), {"ticker": ticker, "fy": fiscal_year, "dt": doc_type})

    for rt in ratings:
        agency = rt.get("agency", "Unknown")
        long_term = rt.get("long_term_rating") or rt.get("rating", "")
        short_term = rt.get("short_term_rating", "")
        outlook = rt.get("outlook", "")
        source_sentence = rt.get("source_sentence", "")[:400]

        # Build label for searchability
        label = f"Credit Rating ({agency}: {long_term})"
        if short_term:
            label += f" / ST: {short_term}"

        # Numeric score for sorting
        sp_eq = MOODYS_TO_SP.get(long_term, long_term)
        score = SP_RATING_SCORES.get(sp_eq, 0)

        detail_note = json.dumps({
            "agency": agency,
            "long_term_rating": long_term,
            "short_term_rating": short_term,
            "outlook": outlook,
            "investment_grade": investment_grade,
            "source_sentence": source_sentence,
            "extraction_method": extraction_method,
            "confidence": confidence,
            "notes": notes,
        }, ensure_ascii=False)

        conn.execute(text("""
            INSERT INTO coreiq_filing_metrics
                (ticker, company_name, fiscal_year, doc_type, concept, original_label, standard_concept,
                 numeric_value, value, unit_ref, is_dimensioned,
                 dimension, member, dimension_label,
                 statement_type, source, llm_query, period_type, fiscal_period)
            VALUES
                (:ticker, (SELECT MIN(COALESCE(name_coresight, name)) FROM coreiq_companies WHERE ticker = :ticker),
                 :fy, :dt, :concept, :label, :concept,
                 :score, :rating, 'rating', 0,
                 :agency, :outlook, :dim_label,
                 'Credit Rating', 'credit_rating', :note, 'instant', 'FY')
        """), {
            "ticker":     ticker,
            "fy":         fiscal_year,
            "dt":         doc_type,
            "concept":    "coreiq:CreditRating",
            "label":      label,
            "score":      score,
            "rating":     long_term,
            "agency":     agency,
            "outlook":    outlook or "",
            "dim_label":  f"{agency}: {long_term}" + (f" ({outlook})" if outlook else ""),
            "note":       detail_note[:500],
        })

    return True


def save_credit_rating_json(
    ticker: str,
    fiscal_year: int,
    doc_type: str,
    result: dict,
):
    """Save credit rating result alongside the filing."""
    for dt in [doc_type, doc_type.replace("-", "")]:
        filing_dir = FILINGS_DIR / ticker / str(fiscal_year) / dt
        if filing_dir.exists():
            json_path = filing_dir / "CREDIT_RATING.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            return json_path
    return None


# ══════════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════

def process_filing(
    ticker: str,
    year: int,
    doc_type: str = "10-K",
    use_llm: bool = True,
    force: bool = False,
) -> Optional[dict]:
    """Full 4-step pipeline for one filing."""
    print(f"\n  [{ticker} {year} {doc_type}]")

    # Check if already extracted (unless --force)
    if not force:
        for dt in [doc_type, doc_type.replace("-", "")]:
            cr_path = FILINGS_DIR / ticker / str(year) / dt / "CREDIT_RATING.json"
            if cr_path.exists():
                with open(cr_path) as f:
                    existing = json.load(f)
                ratings = existing.get("ratings", [])
                if ratings:
                    agencies = ", ".join(r.get("agency", "?") + ":" + r.get("long_term_rating", "?") for r in ratings)
                    print(f"    Already extracted: {agencies}")
                    print(f"    (use --force to re-extract)")
                    return existing

    # ── Step 1: Load section text ──
    print("    Step 1: Loading section text...")
    sections = load_section_text(ticker, year, doc_type)
    if not sections:
        print("    ⚠️  No section text found — need SECTION_CACHE.json or filing.html")
        return None

    relevant = [k for k in CREDIT_SECTIONS if k in sections]
    total_chars = sum(len(sections.get(k, "")) for k in relevant)
    print(f"    Loaded {len(relevant)} sections ({total_chars:,} chars)")

    # ── Step 2: Regex extraction ──
    print("    Step 2: Applying credit rating regex patterns...")
    candidates = extract_credit_ratings_regex(sections)

    if candidates:
        print(f"    Found {len(candidates)} candidate(s):")
        for c in candidates[:5]:
            outlook_str = f" ({c['outlook']})" if c.get('outlook') else ""
            print(f"      {c['agency']}: {c['rating']}{outlook_str} (pattern: {c['pattern']}, section: {c['section']})")
    else:
        print("    No rating matches found")

    # ── Step 3: LLM verification ──
    llm_result = None
    if use_llm:
        print("    Step 3: LLM verification...")
        llm_result = verify_with_llm(ticker, year, sections, candidates)
        if llm_result:
            if llm_result.get("found"):
                for rt in llm_result.get("ratings", []):
                    print(f"    ✅ LLM: {rt.get('agency','?')}: {rt.get('long_term_rating','?')} ({rt.get('outlook','')})")
            else:
                print(f"    ℹ️  LLM says: no credit rating disclosed ({llm_result.get('notes', '')})")
        else:
            print("    ⚠️  LLM verification skipped/failed")
    else:
        print("    Step 3: LLM verification skipped (--no-llm)")

    # ── Determine final result ──
    final_result = _determine_final_result(ticker, year, candidates, llm_result, use_llm)

    if final_result["ratings"]:
        agencies = ", ".join(
            f"{r.get('agency','?')}:{r.get('long_term_rating','?')}"
            for r in final_result["ratings"]
        )
        print(f"\n    📊 RESULT: {agencies}")
        print(f"       Grade: {'Investment' if final_result.get('investment_grade') else 'Speculative/Unknown'}")
        print(f"       Method: {final_result['extraction_method']}")
    else:
        print(f"\n    ℹ️  No credit rating found for {ticker}")

    # ── Save JSON ──
    save_credit_rating_json(ticker, year, doc_type, final_result)

    return final_result


def _determine_final_result(
    ticker: str, year: int,
    candidates: List[dict],
    llm_result: Optional[dict],
    use_llm: bool,
) -> dict:
    """Determine the final credit rating from regex + LLM results."""

    # If LLM verification succeeded, use it (highest trust)
    if llm_result and llm_result.get("found") and llm_result.get("ratings"):
        return {
            "ticker": ticker,
            "fiscal_year": year,
            "ratings": llm_result["ratings"],
            "investment_grade": llm_result.get("investment_grade"),
            "extraction_method": "edgartools+regex+llm_verified",
            "confidence": llm_result.get("confidence", 0.95),
            "notes": llm_result.get("notes", ""),
        }

    # If LLM says no ratings, trust it
    if llm_result and not llm_result.get("found"):
        return {
            "ticker": ticker,
            "fiscal_year": year,
            "ratings": [],
            "investment_grade": None,
            "extraction_method": "llm_verified_no_rating",
            "confidence": llm_result.get("confidence", 0.9),
            "notes": llm_result.get("notes", "No credit rating disclosed"),
        }

    # Fallback: use regex only
    if candidates:
        ratings = []
        for c in candidates:
            ratings.append({
                "agency": c["agency"],
                "long_term_rating": c["rating"],
                "short_term_rating": "",
                "outlook": c.get("outlook", ""),
                "source_sentence": c["source_sentence"],
            })

        # Determine investment grade from highest-scored rating
        best_score = max(c["numeric_score"] for c in candidates)
        is_investment = best_score >= SP_RATING_SCORES.get("BBB-", 12)

        return {
            "ticker": ticker,
            "fiscal_year": year,
            "ratings": ratings,
            "investment_grade": is_investment,
            "extraction_method": "edgartools+regex_only",
            "confidence": 0.70,
            "notes": "Not LLM-verified" if use_llm else "LLM verification disabled",
        }

    # Nothing found
    return {
        "ticker": ticker,
        "fiscal_year": year,
        "ratings": [],
        "investment_grade": None,
        "extraction_method": "not_found",
        "confidence": 0.0,
        "notes": "No credit rating found in any section",
    }


# ══════════════════════════════════════════════════════════════════════════
#  BATCH RUNNER
# ══════════════════════════════════════════════════════════════════════════

def discover_filings(ticker_filter=None, year_filter=None, doc_filter="10-K"):
    """Discover filings that have either SECTION_CACHE.json or filing.html."""
    found = []
    if not FILINGS_DIR.exists():
        return found

    for ticker_dir in sorted(FILINGS_DIR.iterdir()):
        if not ticker_dir.is_dir() or ticker_dir.name.startswith((".", "_")):
            continue
        ticker = ticker_dir.name
        if ticker_filter and ticker != ticker_filter.upper():
            continue

        for year_dir in sorted(ticker_dir.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            fy = int(year_dir.name)
            if year_filter and fy != year_filter:
                continue

            for doc_dir in sorted(year_dir.iterdir()):
                if not doc_dir.is_dir():
                    continue
                dt = doc_dir.name
                if doc_filter and dt.upper().replace("-", "") != doc_filter.upper().replace("-", ""):
                    continue
                if (doc_dir / "SECTION_CACHE.json").exists() or (doc_dir / "filing.html").exists():
                    found.append((ticker, fy, dt))

    return found


def run_credit_rating_extraction(
    ticker_filter=None,
    year_filter=None,
    doc_filter="10-K",
    use_llm=True,
    force=False,
):
    """Run credit rating extraction for all matching filings."""
    filings = discover_filings(ticker_filter, year_filter, doc_filter)

    if not filings:
        print("No filings found to process.")
        return

    print(f"Processing {len(filings)} filing(s)...\n")

    engine = get_engine()
    results = []

    for ticker, fy, dt in filings:
        result = process_filing(ticker, fy, dt, use_llm=use_llm, force=force)
        if result:
            results.append(result)

            # Insert into DB if ratings were found
            if result.get("ratings"):
                try:
                    with engine.begin() as conn:
                        insert_credit_rating_to_db(
                            conn=conn,
                            ticker=ticker,
                            fiscal_year=fy,
                            doc_type=dt,
                            ratings=result["ratings"],
                            investment_grade=result.get("investment_grade"),
                            confidence=result["confidence"],
                            extraction_method=result["extraction_method"],
                            notes=result.get("notes", ""),
                        )
                        n_ratings = len(result["ratings"])
                        print(f"    💾 Saved {n_ratings} rating(s) to DB (source='credit_rating')")
                except Exception as db_err:
                    print(f"    ⚠️  DB insert failed: {db_err}")
                    print(f"    (CREDIT_RATING.json was still saved locally)")

    # ── Summary ──
    found = [r for r in results if r.get("ratings")]
    not_found = [r for r in results if not r.get("ratings")]

    print(f"\n{'═' * 60}")
    print(f"  CREDIT RATING EXTRACTION SUMMARY")
    print(f"{'═' * 60}")
    print(f"  Filings processed: {len(filings)}")
    print(f"  With ratings: {len(found)}")
    print(f"  Without ratings: {len(not_found)}")

    if found:
        print(f"\n  Extracted Credit Ratings:")
        for r in sorted(found, key=lambda x: x["ticker"]):
            agencies = ", ".join(
                f"{rt.get('agency','?')}:{rt.get('long_term_rating','?')}"
                for rt in r["ratings"]
            )
            grade = "IG" if r.get("investment_grade") else "HY"
            print(f"    {r['ticker']:>5} {r['fiscal_year']}: {agencies} [{grade}] ({r['extraction_method']})")

    if not_found:
        print(f"\n  No Credit Rating Found:")
        for r in sorted(not_found, key=lambda x: x["ticker"]):
            print(f"    {r['ticker']:>5} {r['fiscal_year']}: {r.get('notes', 'N/A')}")


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Extract credit ratings from SEC 10-K filings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/extract_credit_ratings.py                     # All filings
  python scripts/extract_credit_ratings.py M                   # Macy's only
  python scripts/extract_credit_ratings.py M 2025              # Macy's 2025
  python scripts/extract_credit_ratings.py --no-llm            # Skip LLM verify
  python scripts/extract_credit_ratings.py --force              # Re-extract all
        """,
    )

    parser.add_argument("ticker", nargs="?", help="Ticker symbol (e.g. M, WMT, DKS)")
    parser.add_argument("year", nargs="?", type=int, help="Fiscal year (e.g. 2025)")
    parser.add_argument("--doc-type", default="10-K", help="Filing form type (default: 10-K)")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM verification")
    parser.add_argument("--force", action="store_true", help="Re-extract even if CREDIT_RATING.json exists")

    args = parser.parse_args()

    run_credit_rating_extraction(
        ticker_filter=args.ticker,
        year_filter=args.year,
        doc_filter=args.doc_type,
        use_llm=not args.no_llm,
        force=args.force,
    )


if __name__ == "__main__":
    main()
