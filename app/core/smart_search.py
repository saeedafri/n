"""
Smart Search Engine — Fuzzy Matching, Trigram Indexing & Financial/Retail Synonym Expansion.
=============================================================================================
Provides:
  1. Financial & Retail Domain Synonym Expansion
  2. Typo-Tolerant Fuzzy Search (Levenshtein & Trigram Similarity)
  3. Query Pre-Processing & Query Expansion for Transcripts, News, and SEC Filings
Zero external cloud cost, runs 100% in-process.
"""

from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple, Optional


# ── Curated Financial & Retail Domain Synonyms ──────────────────────────────
FINANCIAL_RETAIL_SYNONYMS: Dict[str, List[str]] = {
    "revenue": ["revenue", "sales", "net sales", "top line", "gross revenue", "turnover"],
    "sales": ["sales", "revenue", "net sales", "top line", "comparable sales"],
    "profit": ["profit", "net income", "operating income", "ebitda", "ebit", "bottom line"],
    "earnings": ["earnings", "net income", "eps", "profit", "operating profit"],
    "margin": ["margin", "gross margin", "operating margin", "ebitda margin", "operating profitability"],
    "shrink": ["shrink", "shrinkage", "inventory loss", "retail theft", "loss prevention", "theft"],
    "shrinkage": ["shrinkage", "shrink", "inventory loss", "retail theft", "theft"],
    "comps": ["comps", "same-store sales", "comparable sales", "comp store sales", "same store"],
    "inventory": ["inventory", "stock", "merchandise inventory", "days of inventory", "dsi"],
    "capex": ["capex", "capital expenditure", "capital expenditures", "capital additions", "investments"],
    "guidance": ["guidance", "outlook", "forecast", "full year expectations", "target range"],
    "ecommerce": ["e-commerce", "ecommerce", "digital sales", "online sales", "omnichannel", "dtc"],
    "inflation": ["inflation", "price increase", "cost pressures", "input costs", "pricing power"],
    "traffic": ["foot traffic", "customer traffic", "store visits", "transactions", "shopper traffic"],
    "tariffs": ["tariffs", "trade duties", "import duties", "customs duties", "trade policy"],
    "supplychain": ["supply chain", "freight costs", "logistics", "shipping", "lead times"],
    "wages": ["wages", "labor costs", "minimum wage", "payroll", "store associate pay"],
}

# Inverted index of synonyms for fast lookup
_SYNONYM_LOOKUP: Dict[str, Set[str]] = {}
for canonical, variants in FINANCIAL_RETAIL_SYNONYMS.items():
    all_terms = set(variants + [canonical])
    for term in all_terms:
        clean = term.lower().strip()
        _SYNONYM_LOOKUP.setdefault(clean, set()).update(all_terms)


def expand_synonyms(term: str) -> List[str]:
    """Expand a financial/retail term into its domain-specific synonyms.
    
    Example:
        expand_synonyms('revenue') -> ['revenue', 'sales', 'net sales', 'top line', 'turnover']
    """
    clean = term.lower().strip()
    syns = _SYNONYM_LOOKUP.get(clean)
    if syns:
        # Keep original first, followed by others sorted
        return [clean] + sorted([s for s in syns if s != clean])
    return [clean]


# ── Trigram Generation & Distance Functions ─────────────────────────────────
def get_trigrams(word: str) -> Set[str]:
    """Generate character 3-grams for a word."""
    w = f"^{word.lower()}$"
    if len(w) < 3:
        return {w}
    return {w[i:i + 3] for i in range(len(w) - 2)}


def trigram_similarity(w1: str, w2: str) -> float:
    """Calculate Jaccard similarity of character trigrams between two words."""
    t1 = get_trigrams(w1)
    t2 = get_trigrams(w2)
    intersection = len(t1 & t2)
    union = len(t1 | t2)
    return intersection / union if union > 0 else 0.0


def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def find_fuzzy_matches(
    query_word: str,
    vocabulary: Set[str] | List[str],
    max_distance: int = 2,
    min_similarity: float = 0.2,
    top_k: int = 3,
) -> List[Tuple[str, float]]:
    """Find closest words in vocabulary using Levenshtein distance & trigram ranking.
    
    Returns:
        List of (matched_word, similarity_score) sorted by highest similarity.
    """
    q_lower = query_word.lower().strip()
    if not q_lower or len(q_lower) < 3:
        return []

    # Fast check: exact match
    if q_lower in vocabulary:
        return [(q_lower, 1.0)]

    candidates: List[Tuple[str, float]] = []
    q_len = len(q_lower)

    for word in vocabulary:
        w_lower = word.lower().strip()
        # Length prune: difference cannot exceed max_distance
        if abs(len(w_lower) - q_len) > max_distance:
            continue

        dist = levenshtein_distance(q_lower, w_lower)
        if dist <= max_distance:
            sim = trigram_similarity(q_lower, w_lower)
            # Combined score: normalized distance + trigram similarity
            score = (1.0 - (dist / max(q_len, len(w_lower)))) * 0.7 + sim * 0.3
            candidates.append((w_lower, round(score, 3)))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:top_k]


# ── High-Level Query Expansion Pipeline ─────────────────────────────────────
class SmartQueryExpander:
    """Expands queries with synonyms and typo corrections for instant search."""

    def __init__(self, common_vocabulary: Optional[Set[str]] = None):
        self.vocab = set(common_vocabulary or [])
        # Add all synonym terms to vocabulary
        for terms in _SYNONYM_LOOKUP.values():
            self.vocab.update(terms)

    def expand_query(self, raw_query: str, enable_synonyms: bool = True, enable_fuzzy: bool = True) -> Dict[str, any]:
        """Pre-processes and expands a search query with synonyms and fuzzy corrections.
        
        Returns:
            Dict with:
              - 'original': raw query
              - 'clean_words': cleaned tokenized words
              - 'expanded_terms': list of all search terms (including synonyms)
              - 'fuzzy_corrections': dict of {misspelled_word: best_correction}
              - 'fts_query': formatted query string for FTS5 (MATCH syntax)
        """
        if not raw_query or not raw_query.strip():
            return {
                "original": "",
                "clean_words": [],
                "expanded_terms": [],
                "fuzzy_corrections": {},
                "fts_query": "",
            }

        clean = re.sub(r"[^\w\s-]", " ", raw_query).strip()
        words = [w.strip() for w in clean.split() if w.strip()]

        expanded_terms: Set[str] = set()
        fuzzy_corrections: Dict[str, str] = {}

        for w in words:
            w_lower = w.lower()
            expanded_terms.add(w_lower)

            # 1. Fuzzy typo correction if word not recognized
            if enable_fuzzy and self.vocab and w_lower not in self.vocab:
                matches = find_fuzzy_matches(w_lower, self.vocab, max_distance=2, min_similarity=0.2, top_k=1)
                if matches:
                    best_match, score = matches[0]
                    if score >= 0.50:
                        fuzzy_corrections[w] = best_match
                        expanded_terms.add(best_match)
                        w_lower = best_match

            # 2. Synonym expansion
            if enable_synonyms:
                syns = expand_synonyms(w_lower)
                expanded_terms.update(syns)

        # Build SQLite FTS5 query with OR clauses
        # E.g. "revenue" -> '("revenue" OR "sales" OR "net sales" OR "top line")'
        fts_tokens = []
        for term in sorted(expanded_terms):
            if " " in term:
                fts_tokens.append(f'"{term}"')
            else:
                fts_tokens.append(f'"{term}"*')  # prefix matching

        fts_query = " OR ".join(fts_tokens) if fts_tokens else ""

        return {
            "original": raw_query,
            "clean_words": words,
            "expanded_terms": sorted(list(expanded_terms)),
            "fuzzy_corrections": fuzzy_corrections,
            "fts_query": fts_query,
        }
