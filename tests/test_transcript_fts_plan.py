"""The transcript word index cannot match phrases (detail=none), so the query planner
must AND the user's words, keep synonyms inside their own word, and hand exact phrases
and symbol words (R&D, AT&T, C++) back as literals to confirm against the text."""

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

from core.smart_search import build_fts_plan  # noqa: E402
from data.screening_service import _MetricCache  # noqa: E402


def test_word_is_prefix_matched_with_synonyms_as_word_groups():
    plan = build_fts_plan("tariff")
    assert '"tariff"*' in plan["match"]
    assert '("customs"* AND "duties"*)' in plan["match"]
    assert plan["literals"] == []
    assert "tariff" in plan["primary"]


def test_every_word_is_required():
    plan = build_fts_plan("supply chain AI")
    assert plan["match"].count(" AND ") >= 2


def test_single_letter_word_is_not_searchable():
    assert build_fts_plan("a") is None
    assert build_fts_plan("   ") is None
    assert build_fts_plan("") is None


def test_quoted_phrase_becomes_a_literal():
    plan = build_fts_plan('"gross margin"')
    assert plan["literals"] == ["gross margin"]
    assert '"gross" AND "margin"' in plan["match"]


def test_symbol_words_become_literals():
    for query, literal in (("R&D", "r&d"), ("AT&T", "at&t"), ("C++", "c++")):
        assert build_fts_plan(query)["literals"] == [literal]


def test_alternatives_drop_literals():
    plan = build_fts_plan("tariff OR duty")
    assert " OR " in plan["match"] and plan["literals"] == []


def test_typo_is_corrected():
    assert '"tariffs"*' in build_fts_plan("tarrifs")["match"]


def test_metric_cache_is_bounded_and_expires():
    cache = _MetricCache(max_keys=2, ttl_seconds=0.2)
    cache.merge(("a",), {"X": 1.0})
    cache.merge(("b",), {"Y": 2.0})
    cache.merge(("c",), {"Z": 3.0})
    assert cache.get(("a",)) is None and cache.get(("c",)) == {"Z": 3.0}
    cache.merge(("c",), {"W": 4.0})
    assert cache.get(("c",)) == {"Z": 3.0, "W": 4.0}
    time.sleep(0.3)
    assert cache.get(("c",)) is None
