"""The boot pre-build writes segment rows only for tickers with no disk copy,
skips malformed tickers, and never writes a failed query as "no segment data"."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

import utils.materialize as mat  # noqa: E402
from data.repository import SegmentDataRepository  # noqa: E402


def test_builds_only_missing_valid_tickers(monkeypatch):
    written = {}
    monkeypatch.setattr(mat, "on_disk", lambda name: name == "segment_rows_AMZN")
    monkeypatch.setattr(mat, "write_materialized", lambda name, obj, sources: written.__setitem__(name, (obj, sources)))

    def query(ticker, raising=False):
        assert raising, "a DB error must raise, not come back as []"
        if ticker == "BAD":
            raise RuntimeError("db down")
        return [{"ticker": ticker}]
    monkeypatch.setattr(SegmentDataRepository, "_query_all_db_rows", staticmethod(query))

    built = SegmentDataRepository.build_segment_rows_on_disk(["AMZN", "M", "BAD", "x'; DROP", ""])

    assert built == 1
    assert list(written) == ["segment_rows_M"]
    obj, sources = written["segment_rows_M"]
    assert obj == {"rows": [{"ticker": "M"}]}
    assert sources == SegmentDataRepository._segment_rows_sources("M")
