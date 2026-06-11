"""
MySQL persistence for live Streamlit-assembled earnings transcripts.

Writes to ``coreiq_earnings_call_live_transcripts`` using the app's DatabaseManager.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from core.database import db_manager

try:
    from utils.server_logger import log_structured_error
except ImportError:
    def log_structured_error(*a, **kw):  # type: ignore
        pass

LIVE_TRANSCRIPT_SOURCE = "live_streamlit"

_INSERT_UPSERT = """
INSERT INTO coreiq_earnings_call_live_transcripts (
    source,
    ticker,
    quarter,
    year,
    q,
    transcript_text,
    has_transcript,
    title,
    event_datetime_utc,
    fetched_at_utc,
    raw_json,
    raw_json_sha1,
    last_error
) VALUES (
    :source,
    :ticker,
    :quarter,
    :year,
    :q,
    :transcript_text,
    :has_transcript,
    :title,
    :event_datetime_utc,
    :fetched_at_utc,
    :raw_json,
    :raw_json_sha1,
    NULL
)
ON DUPLICATE KEY UPDATE
    year = VALUES(year),
    q = VALUES(q),
    transcript_text = VALUES(transcript_text),
    has_transcript = VALUES(has_transcript),
    title = VALUES(title),
    event_datetime_utc = VALUES(event_datetime_utc),
    fetched_at_utc = VALUES(fetched_at_utc),
    raw_json = VALUES(raw_json),
    raw_json_sha1 = VALUES(raw_json_sha1),
    last_error = NULL
"""


def normalize_ticker(raw: str) -> str:
    """Normalize ticker to fit ``ticker`` varchar(32)."""
    cleaned = re.sub(r"[^A-Za-z0-9.\-]", "", (raw or "").strip().upper())
    return cleaned[:32] or "UNKNOWN"


def export_period_key(fiscal_year: int, quarter: str) -> str:
    """e.g. 2024 + Q1 -> \"2024Q1\" (fits ``quarter`` varchar(8))."""
    q = (quarter or "").strip().upper()
    return f"{int(fiscal_year)}{q}"


def fiscal_label_to_q(quarter: str) -> int:
    m = re.match(r"^Q([1-4])$", (quarter or "").strip().upper())
    return int(m.group(1)) if m else 1


def build_earnings_transcript_payload(
    symbol: str,
    fiscal_year: int,
    quarter: str,
    segments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    sym = normalize_ticker(symbol)
    period = export_period_key(fiscal_year, quarter)
    rows: List[Dict[str, str]] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        sentiment = seg.get("sentiment")
        if sentiment is None or sentiment == "":
            sentiment_str = "0"
        else:
            sentiment_str = str(sentiment)
        rows.append(
            {
                "speaker": str(seg.get("speaker") or ""),
                "title": str(seg.get("title") or ""),
                "content": text,
                "sentiment": sentiment_str,
            }
        )
    return {"symbol": sym, "quarter": period, "transcript": rows}


def _assembled_plain_text(segments: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for seg in segments:
        t = (seg.get("text") or "").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts)


def upsert_live_transcript_row(
    ticker_raw: str,
    fiscal_year: int,
    quarter_label: str,
    segments: List[Dict[str, Any]],
) -> bool:
    """
    Upsert one row keyed by (source, ticker, quarter).

    ``quarter`` column stores the compact key (e.g. 2024Q1); ``year`` and ``q``
    follow fiscal_year / Q1–Q4.
    """
    payload = build_earnings_transcript_payload(
        ticker_raw, fiscal_year, quarter_label, segments
    )
    sym = payload["symbol"]
    period = payload["quarter"]
    plain = _assembled_plain_text(segments).strip()
    has_tr = 1 if plain else 0
    raw_json_str = json.dumps(payload, ensure_ascii=False)
    raw_sha1 = hashlib.sha1(raw_json_str.encode("utf-8")).hexdigest()
    fetched_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    qnum = fiscal_label_to_q(quarter_label)
    title = f"{sym} {period} live transcript"[:512]

    params: Dict[str, Any] = {
        "source": LIVE_TRANSCRIPT_SOURCE,
        "ticker": sym,
        "quarter": period,
        "year": int(fiscal_year),
        "q": qnum,
        "transcript_text": plain if plain else None,
        "has_transcript": has_tr,
        "title": title,
        "event_datetime_utc": None,
        "fetched_at_utc": fetched_naive,
        "raw_json": raw_json_str,
        "raw_json_sha1": raw_sha1,
    }

    n = db_manager.execute_insert(_INSERT_UPSERT, params)
    if n <= 0:
        log_structured_error(
            RuntimeError("upsert returned rowcount <= 0"),
            page="live_earnings_transcript_store",
            component="upsert_live_transcript_row",
            operation="UPSERT_LIVE_TRANSCRIPT",
            context={"ticker": sym, "quarter": period},
        )
        return False
    return True
