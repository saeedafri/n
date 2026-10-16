"""
Transcript Search Engine — TF-IDF based intelligent search.
============================================================
Uses scikit-learn's TfidfVectorizer + cosine_similarity for
relevance-ranked search across earnings call transcript segments.
"""
import re
from typing import List, Dict, Optional
from dataclasses import dataclass

from utils.server_logger import log_structured_error
from core.smart_search import SmartQueryExpander

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


@dataclass
class SearchResult:
    """A single search result from transcript search."""
    index: int
    speaker: str
    text: str
    score: float
    snippet: str


class TranscriptSearchEngine:
    """
    TF-IDF based search for earnings call transcripts with Smart Search
    (Synonym Expansion + Typo Tolerance).

    Builds a TF-IDF matrix from transcript segments and uses
    cosine similarity to rank segments by relevance to a query.

    Features:
        - Financial & retail domain synonym expansion (comps, shrink, revenue, guidance, etc.)
        - Typo tolerance via trigram similarity & Levenshtein edit distance
        - Unigram + bigram support ("net income" as a phrase)
        - English stopword removal
        - Relevance scoring (0.0 to 1.0)
        - Snippet extraction around best match
        - Graceful fallback to basic string search if sklearn unavailable
    """

    def __init__(self, segments: list):
        """
        Initialize the search engine with transcript segments.

        Args:
            segments: List of SpeakerSegment objects (must have .speaker and .text attrs)
        """
        # Safe defaults ensure the object is never attribute-less if init fails
        self.segments = []
        self.texts = []
        self._use_tfidf = False
        self.expander = None
        try:
            self.segments = segments
            self.texts = [s.text if hasattr(s, 'text') else str(s.get('text', '')) for s in segments]
            self._use_tfidf = HAS_SKLEARN and len(self.texts) > 0

            # Seed vocabulary from segments for typo correction
            vocab = set()
            for text in self.texts:
                for token in re.findall(r"\b[a-zA-Z]{3,}\b", text):
                    vocab.add(token.lower())
            self.expander = SmartQueryExpander(common_vocabulary=vocab)

            if self._use_tfidf:
                self.vectorizer = TfidfVectorizer(
                    stop_words='english',
                    max_features=10000,
                    ngram_range=(1, 2),      # unigrams + bigrams
                    min_df=1,                 # include terms even if only in 1 doc
                    sublinear_tf=True,        # apply log normalization to term freq
                )
                try:
                    self.tfidf_matrix = self.vectorizer.fit_transform(self.texts)
                except ValueError as e:
                    # All documents empty or only stopwords
                    log_structured_error(e, page="", component="TranscriptSearchEngine.__init__", operation="fit TF-IDF matrix")
                    self._use_tfidf = False
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine.__init__", operation="initialize search engine")
            if not self.expander:
                self.expander = SmartQueryExpander()

    def search(self, query: str, top_k: int = 20, min_score: float = 0.01) -> List[SearchResult]:
        """
        Search transcript segments for relevance to query.

        Args:
            query: Search query string
            top_k: Maximum number of results to return
            min_score: Minimum relevance score threshold (0.0-1.0)

        Returns:
            List of SearchResult objects sorted by relevance (highest first)
        """
        try:
            if not query or not query.strip():
                return []

            clean_query = query.strip()
            expansion = self.expander.expand_query(clean_query) if self.expander else {
                "original": clean_query,
                "clean_words": clean_query.split(),
                "expanded_terms": [clean_query.lower()],
                "fuzzy_corrections": {},
            }

            if self._use_tfidf:
                return self._tfidf_search(clean_query, expansion, top_k, min_score)
            else:
                return self._fallback_search(clean_query, expansion, top_k)
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine.search", operation="search transcript segments")
            return []

    def _tfidf_search(self, query: str, expansion: Dict, top_k: int, min_score: float) -> List[SearchResult]:
        """TF-IDF based search with cosine similarity ranking and synonym expansion."""
        try:
            # Build search vector including original terms and expanded synonyms
            terms = [query] + list(expansion.get("expanded_terms", []))
            expanded_search_str = " ".join(dict.fromkeys(terms))

            try:
                query_vec = self.vectorizer.transform([expanded_search_str])
                scores = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
            except Exception as e:
                log_structured_error(e, page="", component="TranscriptSearchEngine._tfidf_search", operation="vectorizer transform")
                return self._fallback_search(query, expansion, top_k)

            # Get indices sorted by score (descending)
            ranked_indices = scores.argsort()[::-1]

            # Query words + synonyms + corrections for containment check
            query_words = [w.lower() for w in query.strip().split() if w.strip()]
            candidate_terms = set(query_words)
            candidate_terms.update([t.lower() for t in expansion.get("expanded_terms", [])])
            candidate_terms.update([c.lower() for c in expansion.get("fuzzy_corrections", {}).values()])

            results = []
            for idx in ranked_indices:
                if len(results) >= top_k:
                    break
                score = float(scores[idx])
                if score < min_score:
                    break

                seg = self.segments[idx]
                seg_text = seg.text if hasattr(seg, 'text') else str(seg.get('text', ''))
                seg_speaker = seg.speaker if hasattr(seg, 'speaker') else str(seg.get('speaker', ''))
                text_lower = seg_text.lower()

                # Containment check: matches query or any expanded synonym / typo fix
                if not any(term in text_lower for term in candidate_terms):
                    continue

                snippet = self._extract_snippet(seg_text, query, expansion=expansion, context_chars=120)

                results.append(SearchResult(
                    index=int(idx),
                    speaker=seg_speaker,
                    text=seg_text,
                    score=score,
                    snippet=snippet,
                ))

            return results
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine._tfidf_search", operation="TF-IDF search")
            return []

    def _fallback_search(self, query: str, expansion: Dict, top_k: int) -> List[SearchResult]:
        """
        String-match fallback with synonym and typo support when sklearn is unavailable.
        """
        try:
            query_lower = query.lower()
            candidate_terms = [query_lower]
            if expansion:
                candidate_terms.extend([t.lower() for t in expansion.get("expanded_terms", [])])
                candidate_terms.extend([c.lower() for c in expansion.get("fuzzy_corrections", {}).values()])
            candidate_terms = list(dict.fromkeys(t for t in candidate_terms if t))

            scored = []
            for i, seg in enumerate(self.segments):
                seg_text = seg.text if hasattr(seg, 'text') else str(seg.get('text', ''))
                seg_speaker = seg.speaker if hasattr(seg, 'speaker') else str(seg.get('speaker', ''))
                text_lower = seg_text.lower()
                count = sum(text_lower.count(term) for term in candidate_terms)
                if count > 0:
                    score = min(count / max(len(text_lower.split()), 1), 1.0)
                    snippet = self._extract_snippet(seg_text, query, expansion=expansion, context_chars=120)
                    scored.append(SearchResult(
                        index=i,
                        speaker=seg_speaker,
                        text=seg_text,
                        score=score,
                        snippet=snippet,
                    ))

            scored.sort(key=lambda r: r.score, reverse=True)
            return scored[:top_k]
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine._fallback_search", operation="fallback keyword search")
            return []

    @staticmethod
    def _extract_snippet(text: str, query: str, expansion: Optional[Dict] = None, context_chars: int = 120) -> str:
        """
        Extract a snippet centered around the keyword match.

        Tries: (1) exact query, (2) individual query words, (3) fuzzy corrections, (4) expanded synonyms.
        """
        try:
            text_lower = text.lower()

            # 1. Try exact full query match
            idx = text_lower.find(query.lower())

            # 2. Try each individual word
            if idx == -1:
                for word in query.strip().split():
                    word_idx = text_lower.find(word.lower())
                    if word_idx != -1:
                        idx = word_idx
                        break

            # 3. Try fuzzy corrections or synonyms
            if idx == -1 and expansion:
                corrections = expansion.get("fuzzy_corrections", {})
                for corrected in corrections.values():
                    c_idx = text_lower.find(corrected.lower())
                    if c_idx != -1:
                        idx = c_idx
                        break

                if idx == -1:
                    for syn in expansion.get("expanded_terms", []):
                        syn_idx = text_lower.find(syn.lower())
                        if syn_idx != -1:
                            idx = syn_idx
                            break

            # 4. If still not found, return beginning of text
            if idx == -1:
                return (text[:context_chars * 2] + '...') if len(text) > context_chars * 2 else text

            start = max(0, idx - context_chars)
            end = min(len(text), idx + len(query) + context_chars)

            snippet = text[start:end]

            if start > 0:
                snippet = '...' + snippet
            if end < len(text):
                snippet = snippet + '...'

            return snippet
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine._extract_snippet", operation="extract snippet")
            return text[:context_chars * 2] if text else ""
