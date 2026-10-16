import pytest
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import pandas as pd
import numpy as np
import time

from core.smart_search import (
    expand_synonyms,
    get_trigrams,
    trigram_similarity,
    levenshtein_distance,
    find_fuzzy_matches,
    SmartQueryExpander,
    FINANCIAL_RETAIL_SYNONYMS,
)
from core.duckdb_engine import DuckDBEngine, duckdb_engine


def test_synonym_expansion():
    # Revenue synonyms
    rev_syns = expand_synonyms("revenue")
    assert "revenue" in rev_syns
    assert "sales" in rev_syns
    assert "top line" in rev_syns

    # Shrink / retail theft synonyms
    shrink_syns = expand_synonyms("shrink")
    assert "shrink" in shrink_syns
    assert "shrinkage" in shrink_syns
    assert "theft" in shrink_syns

    # Comps synonyms
    comp_syns = expand_synonyms("comps")
    assert "same-store sales" in comp_syns or "comparable sales" in comp_syns


def test_trigram_and_distance():
    # Identical words
    assert trigram_similarity("walmart", "walmart") == 1.0
    assert levenshtein_distance("walmart", "walmart") == 0

    # Typo: "walamrt" vs "walmart"
    sim = trigram_similarity("walamrt", "walmart")
    dist = levenshtein_distance("walamrt", "walmart")
    assert dist <= 2
    assert sim > 0.2


def test_fuzzy_matches():
    vocab = {"walmart", "target", "costco", "amazon", "kroger", "inventory", "guidance"}
    
    # Misspelled "walamrt"
    matches = find_fuzzy_matches("walamrt", vocab)
    assert len(matches) > 0
    assert matches[0][0] == "walmart"

    # Misspelled "costoc"
    matches_c = find_fuzzy_matches("costoc", vocab)
    assert len(matches_c) > 0
    assert matches_c[0][0] == "costco"


def test_smart_query_expander():
    expander = SmartQueryExpander(common_vocabulary={"walmart", "target", "costco", "guidance"})
    
    # Test query with typo and financial concept
    res = expander.expand_query("walamrt revenue guidence")
    assert "walmart" in res["expanded_terms"] or "walmart" in res["fuzzy_corrections"].values()
    assert "sales" in res["expanded_terms"]  # from revenue synonym
    assert len(res["fts_query"]) > 0


def test_duckdb_columnar_query():
    # Create sample DataFrame of 50,000 financial metric rows
    n = 50000
    df = pd.DataFrame({
        "ticker": np.random.choice(["WMT", "TGT", "COST", "AMZN", "KR"], size=n),
        "year": np.random.choice([2023, 2024, 2025, 2026], size=n),
        "metric_name": np.random.choice(["Total Revenue", "Gross Profit", "Operating Income", "Net Income"], size=n),
        "value": np.random.uniform(100.0, 50000.0, size=n),
    })

    # Warm-up run to initialize JIT and connection
    duckdb_engine.query_df("SELECT count(*) FROM metrics", metrics=df)

    t0 = time.perf_counter()
    res = duckdb_engine.query_df(
        """
        SELECT ticker, year, SUM(value) as total_val, AVG(value) as avg_val, COUNT(*) as cnt
        FROM metrics
        WHERE year >= 2024
        GROUP BY ticker, year
        ORDER BY ticker, year
        """,
        metrics=df
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert len(res) > 0
    assert "total_val" in res.columns
    print(f"\n[BENCHMARK] DuckDB 50k rows aggregation elapsed: {elapsed_ms:.2f}ms")
    assert elapsed_ms < 15.0  # Vectorized in-process should be under 15ms!


def test_transcript_search_engine_smart_search():
    from core.search_engine import TranscriptSearchEngine

    class MockSegment:
        def __init__(self, speaker, text):
            self.speaker = speaker
            self.text = text

    segments = [
        MockSegment("CEO", "We are pleased to report strong net sales across all digital channels."),
        MockSegment("CFO", "Our inventory loss and retail theft challenges have begun to stabilize this quarter."),
        MockSegment("Analyst", "Could you provide more color on your full year outlook and operating margin targets?"),
    ]

    engine = TranscriptSearchEngine(segments)

    # 1. Domain synonym: "shrink" should match segment 1 ("theft" / "inventory loss")
    results_shrink = engine.search("shrink")
    assert len(results_shrink) > 0
    assert "theft" in results_shrink[0].text or "inventory loss" in results_shrink[0].text

    # 2. Domain synonym: "revenue" should match segment 0 ("net sales")
    results_rev = engine.search("revenue")
    assert len(results_rev) > 0
    assert "net sales" in results_rev[0].text

    # 3. Typo tolerance: "guidence" should match segment 2 ("outlook" / targets)
    results_typo = engine.search("guidence")
    assert len(results_typo) > 0
    assert "outlook" in results_typo[0].text or "operating margin" in results_typo[0].text

