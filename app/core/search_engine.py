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
    TF-IDF based search for earnings call transcripts.

    Builds a TF-IDF matrix from transcript segments and uses
    cosine similarity to rank segments by relevance to a query.

    Features:
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
        try:
            self.segments = segments
            self.texts = [s.text for s in segments]
            self._use_tfidf = HAS_SKLEARN and len(self.texts) > 0

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

            if self._use_tfidf:
                return self._tfidf_search(clean_query, top_k, min_score)
            else:
                return self._fallback_search(clean_query, top_k)
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine.search", operation="search transcript segments")
            return []

    def _tfidf_search(self, query: str, top_k: int, min_score: float) -> List[SearchResult]:
        """TF-IDF based search with cosine similarity ranking.
        Only returns segments that actually contain at least one query word."""
        try:
            try:
                query_vec = self.vectorizer.transform([query])
                scores = cosine_similarity(query_vec, self.tfidf_matrix).flatten()
            except Exception as e:
                log_structured_error(e, page="", component="TranscriptSearchEngine._tfidf_search", operation="vectorizer transform")
                return self._fallback_search(query, top_k)

            # Get indices sorted by score (descending)
            ranked_indices = scores.argsort()[::-1]

            # Query words for mandatory containment check
            query_words = [w.lower() for w in query.strip().split() if w.strip()]

            results = []
            for idx in ranked_indices:
                if len(results) >= top_k:
                    break
                score = float(scores[idx])
                if score < min_score:
                    break

                seg = self.segments[idx]
                text_lower = seg.text.lower()

                # MUST contain at least one query word — no phantom results
                if not any(word in text_lower for word in query_words):
                    continue

                snippet = self._extract_snippet(seg.text, query, context_chars=120)

                results.append(SearchResult(
                    index=int(idx),
                    speaker=seg.speaker,
                    text=seg.text,
                    score=score,
                    snippet=snippet,
                ))

            return results
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine._tfidf_search", operation="TF-IDF search")
            return []

    def _fallback_search(self, query: str, top_k: int) -> List[SearchResult]:
        """
        Simple string-match fallback when sklearn is unavailable.
        Counts keyword occurrences as a basic relevance score.
        """
        try:
            query_lower = query.lower()
            scored = []

            for i, seg in enumerate(self.segments):
                text_lower = seg.text.lower()
                count = text_lower.count(query_lower)
                if count > 0:
                    # Normalize score by text length for fairness
                    score = min(count / max(len(text_lower.split()), 1), 1.0)
                    snippet = self._extract_snippet(seg.text, query, context_chars=120)
                    scored.append(SearchResult(
                        index=i,
                        speaker=seg.speaker,
                        text=seg.text,
                        score=score,
                        snippet=snippet,
                    ))

            # Sort by score descending
            scored.sort(key=lambda r: r.score, reverse=True)
            return scored[:top_k]
        except Exception as e:
            log_structured_error(e, page="", component="TranscriptSearchEngine._fallback_search", operation="fallback keyword search")
            return []

    @staticmethod
    def _extract_snippet(text: str, query: str, context_chars: int = 120) -> str:
        """
        Extract a snippet centered around the keyword match.

        Tries: (1) exact query, (2) each individual word — ensures
        the snippet always shows the keyword when possible.
        """
        try:
            text_lower = text.lower()

            # 1. Try exact full query match
            idx = text_lower.find(query.lower())

            # 2. If not found, try each individual word
            if idx == -1:
                for word in query.strip().split():
                    word_idx = text_lower.find(word.lower())
                    if word_idx != -1:
                        idx = word_idx
                        break

            # 3. If still not found, return beginning of text
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
