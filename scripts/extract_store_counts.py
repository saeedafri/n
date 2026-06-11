#!/usr/bin/env python3
"""
extract_store_counts.py
-----------------------
Extracts store/location counts from SEC 10-K filings and loads into coreiq_filing_metrics.

Pipeline:
  Step 1: Read section text from SECTION_CACHE.json (pre-built by enrich_from_edgartools)
  Step 2: Apply enhanced regex patterns to extract store count candidates
  Step 3: LLM-verify each candidate with GPT-4o-mini for zero-error guarantee
  Step 4: Insert verified results into coreiq_filing_metrics table (source='store_count')

Results are inserted into coreiq_filing_metrics with:
  - source          = 'store_count'
  - statement_type  = 'Store Count'
  - label/original_label = 'Store Count' (searchable by "Store" / "Stores")
  - unit_ref        = 'count'
  - concept         = 'coreiq:StoreCount'
  - dimension/member fields used for store_type, as_of_date, source_sentence

Usage:
    python scripts/extract_store_counts.py                   # All filings
    python scripts/extract_store_counts.py M                 # Specific ticker
    python scripts/extract_store_counts.py M 2025            # Specific ticker+year
    python scripts/extract_store_counts.py M 2025 --no-llm   # Skip LLM verification
    python scripts/extract_store_counts.py --force            # Re-extract even if exists

This script is integrated as Step 6 in run_pipeline.py.
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

# Sections to search for store counts (in priority order)
STORE_SECTIONS = [
    "part_i_item_1",   # Item 1 — Business (primary)
    "part_i_item_2",   # Item 2 — Properties (secondary)
    "part_ii_item_7",  # Item 7 — MD&A (tables)
]

# Retail SIC code prefixes — only these companies have physical stores
RETAIL_SIC_PREFIXES = ("52", "53", "54", "55", "56", "57", "58", "59")

# Enhanced regex patterns for store count extraction
STORE_COUNT_PATTERNS = [
    # "operated X stores/locations/supermarkets/warehouses/clubs"
    (r'(?:operated|operates|operating)\s+(?:approximately\s+)?'
     r'([\d,]+)\s+(?:retail\s+|membership\s+|warehouse\s+|company-operated\s+)?'
     r'(store|location|supermarket|warehouse|club|outlet|supercenter|boutique)s?',
     "operated"),

    # "X stores/locations in operation as of"
    (r'([\d,]+)\s+(?:company-operated\s+)?'
     r'(store|location|supermarket|warehouse|club)s?\s+'
     r'(?:in operation|were in operation|as of)',
     "in_operation"),

    # "store count: X" or "store count of X"
    (r'(?:store|location)\s+count[:\s]+(?:of\s+)?([\d,]+)',
     "store_count_label"),

    # "total of X stores"
    (r'total\s+(?:of\s+)?([\d,]+)\s+'
     r'(store|location|supermarket|warehouse|club)s?',
     "total_of"),

    # "X stores in N states"
    (r'([\d,]+)\s+'
     r'(store|location|supermarket|warehouse|club)s?\s+'
     r'(?:in|across|throughout)\s+\d+\s+(?:state|countr)',
     "in_states"),

    # "number of stores/locations: X" or "number of store locations X"
    (r'(?:number|count)\s+of\s+(?:store\s+)?'
     r'(store|location|warehouse|club)s?\s*[:\s]*([\d,]+)',
     "number_of"),
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
#  STEP 1: Load Section Text from SECTION_CACHE
# ══════════════════════════════════════════════════════════════════════════

def load_section_text(ticker: str, year: int, doc_type: str = "10-K") -> Dict[str, str]:
    """
    Load section text from SECTION_CACHE.json.

    Falls back to direct EdgarTools extraction if cache doesn't exist.
    Returns: {section_key: text, ...}
    """
    # Try both directory formats: 10-K and 10K
    for dt in [doc_type, doc_type.replace("-", "")]:
        cache_path = FILINGS_DIR / ticker / str(year) / dt / "SECTION_CACHE.json"
        if cache_path.exists():
            with open(cache_path) as f:
                data = json.load(f)
            sections = {k: v for k, v in data.items() if not k.startswith("_")}
            if sections:
                return sections

    # Fallback: try direct EdgarTools extraction from filing.html
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
        for key in STORE_SECTIONS:
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

def extract_store_count_regex(sections: Dict[str, str]) -> List[dict]:
    """
    Apply enhanced regex patterns across all sections.
    Returns list of candidate dicts: {count, store_type, source_sentence, section, pattern}.
    """
    candidates = []
    seen_counts = set()

    for section_key in STORE_SECTIONS:
        section_text = sections.get(section_key, "")
        if not section_text:
            continue

        for pattern, pattern_name in STORE_COUNT_PATTERNS:
            for match in re.finditer(pattern, section_text, re.IGNORECASE):
                groups = match.groups()

                # Extract the count and store type from matched groups
                count_str = None
                store_type = "stores"

                for g in groups:
                    if g and re.match(r'^[\d,]+$', g):
                        count_str = g
                    elif g and g.lower() in (
                        "store", "location", "supermarket", "warehouse",
                        "club", "outlet", "supercenter", "boutique"
                    ):
                        store_type = g.lower() + "s"

                if not count_str:
                    continue

                count = int(count_str.replace(",", ""))

                # Skip tiny numbers (< 3) and huge numbers (> 100K) — likely noise
                if count < 3 or count > 100_000:
                    continue

                # Skip if we already found this exact count
                if count in seen_counts:
                    continue
                seen_counts.add(count)

                # Extract surrounding sentence for source reference
                match_start = max(0, match.start() - 100)
                match_end = min(len(section_text), match.end() + 100)
                context = section_text[match_start:match_end].strip()
                # Clean up to sentence boundaries
                context = re.sub(r'^[^A-Z]*', '', context)  # trim to sentence start
                context = re.sub(r'\s+', ' ', context)       # collapse whitespace

                candidates.append({
                    "count": count,
                    "count_str": count_str,
                    "store_type": store_type,
                    "source_sentence": context[:400],
                    "section": section_key,
                    "pattern": pattern_name,
                })

    # Sort by count descending — largest is most likely the total
    candidates.sort(key=lambda c: c["count"], reverse=True)
    return candidates


# ══════════════════════════════════════════════════════════════════════════
#  STEP 3: LLM Verification
# ══════════════════════════════════════════════════════════════════════════

LLM_VERIFY_PROMPT = """You are a financial data analyst extracting store/location counts from SEC 10-K filings.

COMPANY: {ticker}
FISCAL YEAR: {year}

Below is text from the company's 10-K filing. Extract:
1. The TOTAL store/location count as of the most recent date mentioned
2. The "as of" date
3. The store type (stores, supermarkets, warehouses, clubs, locations, etc.)
4. The EXACT sentence from the text that states this number

The regex pre-extraction found these candidates: {regex_results}

Verify or correct the store count. If the company operates multiple banners/segments,
provide the CONSOLIDATED TOTAL (sum of all segments). If only segment-level counts exist,
sum them yourself.

Return ONLY this JSON:
{{
  "found": true,
  "store_count": <int>,
  "as_of_date": "<YYYY-MM-DD or null>",
  "store_type": "<stores|supermarkets|warehouses|clubs|locations|outlets>",
  "source_sentence": "<exact 1-2 sentence excerpt from text>",
  "confidence": <0.0 to 1.0>,
  "notes": "<any clarification e.g. 'consolidated total of 3 banners'>"
}}

If the company does NOT operate physical retail locations (e.g. pure e-commerce, CPG
manufacturer), return:
{{
  "found": false,
  "store_count": null,
  "as_of_date": null,
  "store_type": null,
  "source_sentence": null,
  "confidence": 1.0,
  "notes": "Company does not operate physical retail stores"
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
    Returns verified dict or None if all LLM calls fail.
    """
    # Build context: concatenate relevant sections (max 4000 chars)
    section_text = ""
    for key in STORE_SECTIONS:
        if key in sections:
            section_text += f"\n--- {key} ---\n{sections[key][:3000]}\n"
    section_text = section_text[:6000]

    # Format regex results for LLM
    regex_summary = json.dumps([
        {"count": c["count"], "type": c["store_type"], "pattern": c["pattern"]}
        for c in regex_candidates[:5]
    ], indent=2) if regex_candidates else "No regex matches found"

    prompt = LLM_VERIFY_PROMPT.format(
        ticker=ticker,
        year=year,
        regex_results=regex_summary,
        section_text=section_text,
    )

    system_msg = (
        "You are a financial analyst specializing in SEC filings. "
        "Extract store/location counts with 100% accuracy. "
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
    """Call OpenAI GPT-4o-mini for store count verification."""
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
            max_tokens=300,
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
    """Call Google Gemini for store count verification."""
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
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        if "found" in result:
            return result
        return None
    except ImportError:
        print("    ⚠️  google-generativeai package not installed, installing...")
        try:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "google-generativeai"])
            return _call_gemini(api_key, system_msg, prompt)  # retry after install
        except Exception:
            print("    ❌ Failed to install google-generativeai")
            return None
    except Exception as e:
        print(f"    ❌ Gemini API error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
#  STEP 4: Insert into DB (coreiq_filing_metrics table)
# ══════════════════════════════════════════════════════════════════════════

def insert_store_count_to_db(
    conn,
    ticker: str,
    fiscal_year: int,
    doc_type: str,
    store_count: int,
    store_type: str,
    as_of_date: Optional[str],
    source_sentence: str,
    extraction_method: str,
    confidence: float,
    section_source: str,
    notes: str = "",
):
    """
    Insert store count into coreiq_filing_metrics table.

    Follows the same pattern as calculate_derived_metrics.py (source='calculated')
    and llm_extractor.py (source='llm'), but uses source='store_count'.

    The data is searchable by label "Store Count" / "Number of Stores" etc.
    """
    # Delete existing store_count entries for this filing (idempotent)
    conn.execute(text("""
        DELETE FROM coreiq_filing_metrics
        WHERE ticker = :ticker AND fiscal_year = :fy AND doc_type = :dt
          AND source = 'store_count'
    """), {"ticker": ticker, "fy": fiscal_year, "dt": doc_type})

    # Build descriptive labels for searchability
    # User will search "Store" or "Stores" — make sure both match
    label = "Store Count"
    original_label = f"Store Count ({store_type})"
    concept = "coreiq:StoreCount"

    # Build the detailed note (shown as calculation_note / llm_query)
    detail_note = json.dumps({
        "store_type": store_type,
        "as_of_date": as_of_date,
        "source_sentence": source_sentence[:400],
        "extraction_method": extraction_method,
        "confidence": confidence,
        "section": section_source,
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
             :value, :value_str, 'count', 0,
             :store_type, :as_of_date, :dim_label,
             'Store Count', 'store_count', :note, 'instant', 'FY')
    """), {
        "ticker":     ticker,
        "fy":         fiscal_year,
        "dt":         doc_type,
        "concept":    concept,
        "label":      original_label,
        "value":      store_count,
        "value_str":  str(store_count),
        "store_type": store_type,
        "as_of_date": as_of_date,
        "dim_label":  f"{store_count} {store_type} as of {as_of_date or 'FY end'}",
        "note":       detail_note,
    })

    return True


def save_store_count_json(
    ticker: str,
    fiscal_year: int,
    doc_type: str,
    result: dict,
):
    """Save store count result alongside the filing's FINAL_FACTS_FILTERED.json."""
    for dt in [doc_type, doc_type.replace("-", "")]:
        json_path = FILINGS_DIR / ticker / str(fiscal_year) / dt / "STORE_COUNT.json"
        filing_dir = FILINGS_DIR / ticker / str(fiscal_year) / dt
        if filing_dir.exists():
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
    """
    Full 4-step pipeline for one filing.
    Returns result dict or None.
    """
    print(f"\n  [{ticker} {year} {doc_type}]")

    # Check if already extracted (unless --force)
    if not force:
        for dt in [doc_type, doc_type.replace("-", "")]:
            sc_path = FILINGS_DIR / ticker / str(year) / dt / "STORE_COUNT.json"
            if sc_path.exists():
                with open(sc_path) as f:
                    existing = json.load(f)
                count = existing.get("store_count")
                if count is not None:
                    print(f"    Already extracted: {count} {existing.get('store_type', 'stores')}")
                    print(f"    (use --force to re-extract)")
                    return existing

    # ── Step 1: Load section text ──
    print("    Step 1: Loading section text...")
    sections = load_section_text(ticker, year, doc_type)
    if not sections:
        print("    ⚠️  No section text found — need SECTION_CACHE.json or filing.html")
        print("    Run: python scripts/enrich_from_edgartools.py {ticker}")
        return None

    relevant = [k for k in STORE_SECTIONS if k in sections]
    total_chars = sum(len(sections.get(k, "")) for k in relevant)
    print(f"    Loaded {len(relevant)} sections ({total_chars:,} chars): {', '.join(relevant)}")

    # ── Step 2: Regex extraction ──
    print("    Step 2: Applying regex patterns...")
    candidates = extract_store_count_regex(sections)

    if candidates:
        print(f"    Found {len(candidates)} candidate(s):")
        for c in candidates[:3]:
            print(f"      {c['count']:,} {c['store_type']} (pattern: {c['pattern']}, section: {c['section']})")
    else:
        print("    No regex matches found")

    # ── Step 3: LLM verification ──
    llm_result = None
    if use_llm:
        print("    Step 3: LLM verification (GPT-4o-mini)...")
        llm_result = verify_with_llm(ticker, year, sections, candidates)

        if llm_result:
            if llm_result.get("found"):
                print(f"    ✅ LLM verified: {llm_result['store_count']:,} {llm_result.get('store_type', 'stores')}")
                if llm_result.get("notes"):
                    print(f"       Notes: {llm_result['notes']}")
            else:
                print(f"    ℹ️  LLM says: no physical stores ({llm_result.get('notes', '')})")
        else:
            print("    ⚠️  LLM verification skipped/failed")
    else:
        print("    Step 3: LLM verification skipped (--no-llm)")

    # ── Determine final result ──
    final_result = _determine_final_result(ticker, year, candidates, llm_result, use_llm)

    if final_result["store_count"] is not None:
        print(f"\n    📊 RESULT: {final_result['store_count']:,} {final_result['store_type']}")
        print(f"       Method: {final_result['extraction_method']}")
        print(f"       Source: \"{final_result['source_sentence'][:100]}...\"")
    else:
        print(f"\n    ℹ️  No store count found for {ticker}")

    # ── Save JSON ──
    save_store_count_json(ticker, year, doc_type, final_result)

    return final_result


def _determine_final_result(
    ticker: str, year: int,
    candidates: List[dict],
    llm_result: Optional[dict],
    use_llm: bool,
) -> dict:
    """Determine the final store count from regex + LLM results."""

    # If LLM verification succeeded, use it (highest trust)
    if llm_result and llm_result.get("found"):
        return {
            "ticker": ticker,
            "fiscal_year": year,
            "store_count": llm_result["store_count"],
            "store_type": llm_result.get("store_type", "stores"),
            "as_of_date": llm_result.get("as_of_date"),
            "source_sentence": llm_result.get("source_sentence", ""),
            "extraction_method": "edgartools+regex+llm_verified",
            "confidence": llm_result.get("confidence", 0.95),
            "section": candidates[0]["section"] if candidates else "llm_only",
            "notes": llm_result.get("notes", ""),
        }

    # If LLM says no stores, trust it
    if llm_result and not llm_result.get("found"):
        return {
            "ticker": ticker,
            "fiscal_year": year,
            "store_count": None,
            "store_type": None,
            "as_of_date": None,
            "source_sentence": None,
            "extraction_method": "llm_verified_no_stores",
            "confidence": llm_result.get("confidence", 0.9),
            "section": None,
            "notes": llm_result.get("notes", "No physical stores"),
        }

    # Fallback: use regex only (no LLM or LLM failed)
    if candidates:
        best = candidates[0]  # highest count (sorted descending)
        return {
            "ticker": ticker,
            "fiscal_year": year,
            "store_count": best["count"],
            "store_type": best["store_type"],
            "as_of_date": None,
            "source_sentence": best["source_sentence"],
            "extraction_method": "edgartools+regex_only",
            "confidence": 0.70,  # lower confidence without LLM
            "section": best["section"],
            "notes": "Not LLM-verified" if use_llm else "LLM verification disabled",
        }

    # Nothing found
    return {
        "ticker": ticker,
        "fiscal_year": year,
        "store_count": None,
        "store_type": None,
        "as_of_date": None,
        "source_sentence": None,
        "extraction_method": "not_found",
        "confidence": 0.0,
        "section": None,
        "notes": "No store count found in any section",
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

                # Must have either SECTION_CACHE.json or filing.html
                if (doc_dir / "SECTION_CACHE.json").exists() or (doc_dir / "filing.html").exists():
                    found.append((ticker, fy, dt))

    return found


def run_store_count_extraction(
    ticker_filter=None,
    year_filter=None,
    doc_filter="10-K",
    use_llm=True,
    force=False,
):
    """Run store count extraction for all matching filings."""
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

            # Insert into DB if store count was found
            if result["store_count"] is not None:
                try:
                    with engine.begin() as conn:
                        insert_store_count_to_db(
                            conn=conn,
                            ticker=ticker,
                            fiscal_year=fy,
                            doc_type=dt,
                            store_count=result["store_count"],
                            store_type=result["store_type"],
                            as_of_date=result.get("as_of_date"),
                            source_sentence=result.get("source_sentence", ""),
                            extraction_method=result["extraction_method"],
                            confidence=result["confidence"],
                            section_source=result.get("section", ""),
                            notes=result.get("notes", ""),
                        )
                        print(f"    💾 Saved to DB (coreiq_filing_metrics, source='store_count')")
                except Exception as db_err:
                    print(f"    ⚠️  DB insert failed: {db_err}")
                    print(f"    (STORE_COUNT.json was still saved locally)")

    # ── Summary ──
    found = [r for r in results if r.get("store_count") is not None]
    not_found = [r for r in results if r.get("store_count") is None]

    print(f"\n{'═' * 60}")
    print(f"  STORE COUNT EXTRACTION SUMMARY")
    print(f"{'═' * 60}")
    print(f"  Filings processed: {len(filings)}")
    print(f"  Store count found: {len(found)}")
    print(f"  Not found / no stores: {len(not_found)}")

    if found:
        print(f"\n  Extracted Store Counts:")
        for r in sorted(found, key=lambda x: x["ticker"]):
            print(f"    {r['ticker']:>5} {r['fiscal_year']}: "
                  f"{r['store_count']:>6,} {r['store_type']:<15} "
                  f"({r['extraction_method']})")

    if not_found:
        print(f"\n  No Store Count:")
        for r in sorted(not_found, key=lambda x: x["ticker"]):
            print(f"    {r['ticker']:>5} {r['fiscal_year']}: {r.get('notes', 'N/A')}")


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Extract store/location counts from SEC 10-K filings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/extract_store_counts.py                    # All filings
  python scripts/extract_store_counts.py M                  # Macy's only
  python scripts/extract_store_counts.py M 2025             # Macy's 2025
  python scripts/extract_store_counts.py M 2025 --no-llm    # Skip LLM verify
  python scripts/extract_store_counts.py --force             # Re-extract all
  python scripts/extract_store_counts.py --doc-type 10-K     # Specify form

After extraction, search in the app by:
  - Label: "Store Count" or "Stores" or "Store"
  - Source: 'store_count'
  - Concept: 'coreiq:StoreCount'
        """,
    )

    parser.add_argument("ticker", nargs="?", help="Ticker symbol (e.g. M, WMT, AAPL)")
    parser.add_argument("year", nargs="?", type=int, help="Fiscal year (e.g. 2025)")
    parser.add_argument("--doc-type", default="10-K", help="Filing form type (default: 10-K)")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM verification")
    parser.add_argument("--force", action="store_true", help="Re-extract even if STORE_COUNT.json exists")

    args = parser.parse_args()

    run_store_count_extraction(
        ticker_filter=args.ticker,
        year_filter=args.year,
        doc_filter=args.doc_type,
        use_llm=not args.no_llm,
        force=args.force,
    )


if __name__ == "__main__":
    main()
