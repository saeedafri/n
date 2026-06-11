"""
llm_extractor.py
-----------------
On-demand LLM extraction for SEC filing metrics not found via XBRL search.

Flow:
  1. Normalize query
  2. Check filing_llm_cache table (DB) → return cached result if hit
  3. On miss: load SECTION_CACHE.json for the filing
  4. Keyword-window the section text to extract ~3000 chars of relevant context
  5. Call GPT-4o-mini with structured JSON prompt
  6. Store result in coreiq_filing_metrics_v2 (source='llm') + cache in filing_llm_cache
  7. Return list[FilingMetricResult]

Cost: ~$0.00012 per unique query (GPT-4o-mini), $0 for cached queries.
"""

import os
import re
import json
from pathlib import Path
from typing import List, Optional, Dict, Any

from utils.server_logger import log_structured_error

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_query(query: str) -> str:
    """Lowercase + collapse whitespace for cache key."""
    try:
        return re.sub(r"\s+", " ", query.strip().lower())
    except Exception as e:
        log_structured_error(e, page="", component="_normalize_query", operation="normalize query string")
        return query or ""


def _get_section_cache_path(ticker: str, report_fiscal_year: int, doc_type: str) -> Optional[Path]:
    """
    Find SECTION_CACHE.json for a filing.
    Tries both directory formats: '10K' and '10-K'.
    """
    try:
        filings_root = PROJECT_ROOT / "data" / "filings" / ticker

        # Try exact doc_type first, then common variants
        candidates = [doc_type, doc_type.replace("-", "")]
        # Also try adding a hyphen if the doc_type has no hyphen (e.g. "10K" → "10-K")
        if "-" not in doc_type and len(doc_type) > 2:
            candidates.append(doc_type[:-1] + "-" + doc_type[-1])
        for dt in candidates:
            p = filings_root / str(report_fiscal_year) / dt / "SECTION_CACHE.json"
            if p.exists():
                return p
        return None
    except Exception as e:
        log_structured_error(e, page="", component="_get_section_cache_path", operation="locate section cache file")
        return None


def _load_section_cache(path: Path) -> Dict[str, str]:
    """Load section cache, returning dict of section_key → text."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # Skip metadata key
        return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}
    except Exception as e:
        log_structured_error(e, page="", component="_load_section_cache", operation="load section cache JSON")
        return {}


def _extract_context_windows(sections: Dict[str, str], query: str,
                              window_chars: int = 1500,
                              max_windows: int = 3) -> str:
    """
    Find the most relevant context by searching all section text for query keywords.

    Strategy:
    - Split query into significant words (≥4 chars)
    - Search each section for occurrences of those words
    - Extract a ±window_chars window around each match
    - Deduplicate overlapping windows
    - Return up to max_windows windows joined with separators
    """
    try:
        query_words = [w.lower() for w in re.findall(r"\b\w{4,}\b", query.lower())]
        if not query_words:
            query_words = [query.lower().strip()]

        # Build a combined text corpus: item8 first (most relevant), then others
        section_order = ["part_ii_item_8", "part_ii_item_7", "part_i_item_1",
                         "part_ii_item_7a", "full_text"]
        corpus_parts = []
        for key in section_order:
            if key in sections:
                corpus_parts.append(sections[key])
        # Append any remaining sections not in the priority list
        for key, text in sections.items():
            if key not in section_order:
                corpus_parts.append(text)

        full_corpus = "\n\n".join(corpus_parts)
        corpus_lower = full_corpus.lower()

        # Find match positions for each query word
        match_positions = []
        for word in query_words:
            pos = 0
            while True:
                idx = corpus_lower.find(word, pos)
                if idx == -1:
                    break
                match_positions.append(idx)
                pos = idx + 1

        if not match_positions:
            # No keyword match → return first window_chars of item8 as fallback
            fallback = sections.get("part_ii_item_8", full_corpus)
            return fallback[:window_chars * max_windows]

        # Sort and deduplicate (merge positions within window_chars of each other)
        match_positions.sort()
        merged_centers = [match_positions[0]]
        for pos in match_positions[1:]:
            if pos - merged_centers[-1] > window_chars:
                merged_centers.append(pos)

        # Extract all candidate windows, score each by unique query-word coverage
        candidates = []
        for center in merged_centers:
            start = max(0, center - window_chars // 2)
            end = min(len(full_corpus), center + window_chars // 2)
            window = full_corpus[start:end].strip()
            window_lower = window.lower()
            score = sum(1 for w in query_words if w in window_lower)
            candidates.append((score, window))

        # Sort by score descending so windows covering more query words come first
        candidates.sort(key=lambda x: x[0], reverse=True)
        windows = [w for _, w in candidates[:max_windows]]

        return "\n\n[...]\n\n".join(windows)
    except Exception as e:
        log_structured_error(e, page="", component="_extract_context_windows", operation="extract context windows for LLM")
        return ""


def _detect_scale(sections: Dict[str, str]) -> int:
    """
    Detect the reporting scale from the financial statements section.
    Returns multiplier: 1_000_000 (millions), 1_000 (thousands), or 1 (actual).
    """
    try:
        item8 = sections.get("part_ii_item_8", "")
        text_lower = item8[:3000].lower()  # check header area

        if "in millions" in text_lower:
            return 1_000_000
        if "in thousands" in text_lower:
            return 1_000
        if "in billions" in text_lower:
            return 1_000_000_000
        return 1_000_000  # default: Apple + most large-cap 10-Ks use millions
    except Exception as e:
        log_structured_error(e, page="", component="_detect_scale", operation="detect financial reporting scale")
        return 1_000_000


# ── DB helpers ────────────────────────────────────────────────────────────────

def _check_llm_cache(conn, ticker: str, report_fiscal_year: int,
                     doc_type: str, query_normalized: str) -> Optional[str]:
    """
    Check filing_llm_cache table.
    Returns 'found', 'not_found', or None (not cached yet).
    """
    try:
        from sqlalchemy import text as sql_text
        result = conn.execute(sql_text("""
            SELECT status FROM filing_llm_cache
            WHERE ticker = :ticker AND fiscal_year = :fy AND doc_type = :dt
              AND query_normalized = :q
            LIMIT 1
        """), {"ticker": ticker, "fy": report_fiscal_year, "dt": doc_type, "q": query_normalized})
        row = result.fetchone()
        return row[0] if row else None
    except Exception as e:
        log_structured_error(e, page="", component="_check_llm_cache", operation="check LLM cache in database")
        return None


def _write_llm_cache(conn, ticker: str, report_fiscal_year: int, doc_type: str,
                     query_normalized: str, status: str, metric_id: Optional[int] = None):
    """Upsert a cache entry in filing_llm_cache."""
    try:
        from sqlalchemy import text as sql_text
        conn.execute(sql_text("""
            INSERT INTO filing_llm_cache
                (ticker, fiscal_year, doc_type, query_normalized, status, metric_id)
            VALUES (:ticker, :fy, :dt, :q, :status, :mid)
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                metric_id = VALUES(metric_id),
                searched_at = CURRENT_TIMESTAMP
        """), {
            "ticker": ticker, "fy": report_fiscal_year, "dt": doc_type,
            "q": query_normalized, "status": status, "mid": metric_id,
        })
    except Exception as e:
        log_structured_error(e, page="", component="_write_llm_cache", operation="write LLM cache to database")


def _insert_llm_metric(conn, ticker: str, report_fiscal_year: int, doc_type: str,
                       label: str, concept: str, numeric_value: float,
                       unit_ref: str, query_normalized: str,
                       context_excerpt: str) -> int:
    """
    Insert a new metric extracted by LLM into coreiq_filing_metrics_v2.
    Returns the new row id.
    """
    try:
        from sqlalchemy import text as sql_text
        value_str = str(int(numeric_value)) if unit_ref == "usd" else str(numeric_value)

        result = conn.execute(sql_text("""
            INSERT INTO coreiq_filing_metrics_v2
                (ticker, report_fiscal_year, doc_type, concept, original_label, standard_concept,
                 numeric_value, value, unit_ref, is_dimensioned,
                 statement_type, source, llm_query, period_type, fiscal_period)
            VALUES
                (:ticker, :fy, :dt, :concept, :label, :concept,
                 :value, :value_str, :unit, 0,
                 'LLM Extracted', 'llm', :query, 'duration', 'FY')
        """), {
            "ticker": ticker, "fy": report_fiscal_year, "dt": doc_type,
            "concept": concept, "label": label,
            "value": numeric_value, "value_str": value_str,
            "unit": unit_ref, "query": query_normalized,
        })
        return result.lastrowid
    except Exception as e:
        log_structured_error(e, page="", component="_insert_llm_metric", operation="insert LLM metric to database")
        return None


def _fetch_cached_metrics(conn, ticker: str, report_fiscal_year: int,
                          doc_type: str, query_normalized: str) -> list:
    """Retrieve previously LLM-extracted metrics for a query from coreiq_filing_metrics_v2."""
    try:
        from sqlalchemy import text as sql_text
        result = conn.execute(sql_text("""
            SELECT original_label, numeric_value, unit_ref,
                   COALESCE(report_fiscal_year, storage_year) AS report_fiscal_year,
                   is_dimensioned, dimension_label, statement_type, ixbrl_id,
                   standard_concept, concept, balance, period_type, value, source
            FROM coreiq_filing_metrics_v2
            WHERE ticker = :ticker AND COALESCE(report_fiscal_year, storage_year) = :fy AND doc_type = :dt
              AND source = 'llm'
              AND llm_query = :q
        """), {"ticker": ticker, "fy": report_fiscal_year, "dt": doc_type, "q": query_normalized})
        return result.fetchall()
    except Exception as e:
        log_structured_error(e, page="", component="_fetch_cached_metrics", operation="fetch cached LLM metrics from database")
        return []


# ── GPT-4o-mini call ──────────────────────────────────────────────────────────

def _call_llm(query: str, context_text: str, scale: int) -> Optional[Dict[str, Any]]:
    """
    Call GPT-4o-mini to extract a financial metric from the context text.

    Returns dict with keys: found, value, unit, label, context_excerpt
    or None if the API call fails.
    """
    try:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
        except ImportError:
            return None

        scale_note = {
            1_000_000: "The filing reports financial values in millions of US dollars. "
                       "So '391,035' in the text means $391,035 million = $391,035,000,000. "
                       "Return the actual dollar amount (multiply text value × 1,000,000).",
            1_000:     "The filing reports financial values in thousands of US dollars. "
                       "Return the actual dollar amount (multiply text value × 1,000).",
            1:         "Values are reported at face value with no scale multiplier.",
            1_000_000_000: "The filing reports financial values in billions of US dollars. "
                           "Return the actual dollar amount (multiply text value × 1,000,000,000).",
        }.get(scale, f"Scale multiplier: {scale}.")

        system_prompt = (
            "You are a financial analyst extracting specific metrics from SEC 10-K filings. "
            "Extract ONLY the metric asked for — do not confuse it with related metrics. "
            "Return ONLY valid JSON, no markdown, no explanation."
        )

        user_prompt = f"""Extract the metric: "{query}"

{scale_note}

For per-share values (EPS, dividends per share): return the actual per-share value, do NOT multiply by the scale.
For share counts: return the actual number of shares.
For percentages: return the decimal (e.g. 0.27 for 27%) OR the percentage value if clearly labeled as %.

Context from the SEC 10-K filing:
---
{context_text[:4000]}
---

Return JSON with this exact structure:
{{
  "found": true or false,
  "value": <number or null>,
  "unit": "<usd | shares | percent | ratio | other>",
  "label": "<exact label or description from the text>",
  "context_excerpt": "<15-30 word quote showing where you found this>"
}}

If the metric is not mentioned or cannot be determined, return {{"found": false, "value": null, "unit": null, "label": null, "context_excerpt": null}}"""

        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                max_tokens=256,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            return json.loads(raw)
        except Exception as e:
            log_structured_error(e, page="", component="_call_llm", operation="call GPT-4o-mini API")
            return None
    except Exception as e:
        log_structured_error(e, page="", component="_call_llm", operation="LLM extraction call")
        return None


# ── Main extraction entry point ───────────────────────────────────────────────

class LLMExtractor:
    """
    On-demand LLM extraction for SEC filing metrics.

    Usage (from repository):
        from core.llm_extractor import LLMExtractor
        results = LLMExtractor.extract(conn, ticker, report_fiscal_year, doc_type, query)
    """

    @staticmethod
    def extract(conn, ticker: str, report_fiscal_year: int,
                doc_type: str, query: str) -> list:
        """
        Extract a metric using LLM if not found in DB.

        Args:
            conn: SQLAlchemy connection (already open, will be used for cache R/W)
            ticker: company ticker (e.g. 'AAPL')
            report_fiscal_year: fiscal year (e.g. 2024)
            doc_type: doc type as stored in DB (e.g. '10K' or '10-K')
            query: user search query

        Returns:
            List of FilingMetricResult objects (empty if not found / API key missing)
        """
        try:
            from data.models import FilingMetricResult

            q_norm = _normalize_query(query)

            # ── 1. Check cache ────────────────────────────────────────────────────
            cached_status = _check_llm_cache(conn, ticker, report_fiscal_year, doc_type, q_norm)

            if cached_status == "not_found":
                return []

            if cached_status == "found":
                rows = _fetch_cached_metrics(conn, ticker, report_fiscal_year, doc_type, q_norm)
                return [
                    FilingMetricResult(
                        original_label=r[0] or "",
                        numeric_value=r[1],
                        unit_ref=r[2],
                        report_fiscal_year=r[3],
                        is_dimensioned=bool(r[4]),
                        dimension_label=r[5],
                        statement_type=r[6],
                        ixbrl_id=r[7],
                        standard_concept=r[8],
                        concept=r[9],
                        balance=r[10],
                        period_type=r[11],
                        value=r[12],
                        source=r[13],
                    )
                    for r in rows
                ]

            # ── 2. Load section cache ────────────────────────────────────────────
            cache_path = _get_section_cache_path(ticker, report_fiscal_year, doc_type)
            if not cache_path:
                return []

            sections = _load_section_cache(cache_path)
            if not sections:
                return []

            # ── 3. Build context ─────────────────────────────────────────────────
            context = _extract_context_windows(sections, query)
            scale = _detect_scale(sections)

            # ── 4. Call LLM ──────────────────────────────────────────────────────
            llm_result = _call_llm(query, context, scale)

            if llm_result is None:
                # API unavailable — don't cache, just return empty
                return []

            # ── 5. Parse + store result ──────────────────────────────────────────
            if not llm_result.get("found") or llm_result.get("value") is None:
                _write_llm_cache(conn, ticker, report_fiscal_year, doc_type, q_norm, "not_found")
                return []

            # Valid extraction
            try:
                numeric_value = float(llm_result["value"])
            except (TypeError, ValueError):
                _write_llm_cache(conn, ticker, report_fiscal_year, doc_type, q_norm, "not_found")
                return []

            unit_ref = (llm_result.get("unit") or "usd").lower()
            label = llm_result.get("label") or query.title()
            concept = f"LLM_{re.sub(r'[^A-Za-z0-9]', '_', label)}"

            # Insert metric into coreiq_filing_metrics_v2
            metric_id = _insert_llm_metric(
                conn=conn,
                ticker=ticker,
                report_fiscal_year=report_fiscal_year,
                doc_type=doc_type,
                label=label,
                concept=concept,
                numeric_value=numeric_value,
                unit_ref=unit_ref,
                query_normalized=q_norm,
                context_excerpt=llm_result.get("context_excerpt", ""),
            )

            # Write cache entry
            _write_llm_cache(conn, ticker, report_fiscal_year, doc_type, q_norm, "found", metric_id)

            # Return as FilingMetricResult
            return [FilingMetricResult(
                original_label=label,
                numeric_value=numeric_value,
                unit_ref=unit_ref,
                report_fiscal_year=report_fiscal_year,
                is_dimensioned=False,
                dimension_label=None,
                statement_type="LLM Extracted",
                ixbrl_id=None,
                standard_concept=concept,
                concept=concept,
                balance=None,
                period_type="duration",
                value=str(int(numeric_value)) if unit_ref == "usd" else str(numeric_value),
                source="llm",
            )]
        except Exception as e:
            log_structured_error(e, page="", component="LLMExtractor.extract", operation="extract metric using LLM")
            return []
