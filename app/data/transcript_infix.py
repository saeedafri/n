"""ASCII lowercase literal pages using a verified stopwords-OFF bigram index."""

import json
import re
import time


TABLE = "coreiq_av_earnings_call_transcripts"
INDEX = "idx_transcript_ngram_search"


class InfixSearchError(RuntimeError):
    """An indexed search failed before a complete page was available."""


def literal_match_sql(keyword):
    """Match ASCII queries with the UI's lowercase literal contract."""
    if not keyword.isascii():
        return "LOCATE(:keyword, transcript_text) > 0"
    return ("LOCATE(CAST(LOWER(:keyword) AS BINARY), "
            "CAST(LOWER(REPLACE(transcript_text, CONVERT(0xC4B0 USING utf8mb4), "
            "CONVERT(0x69CC87 USING utf8mb4))) AS BINARY)) > 0")


def candidate_query(term):
    if not term.isascii():
        return None
    runs = dict.fromkeys(run for run in re.findall(r"[^\W_]+", term) if len(run) >= 2)
    return " ".join(f'+"{run}"' for run in runs) or None


def search_page(db, keyword, scoped_tickers=(), year=None, q=None, limit=50, offset=0,
                after=None, query_ms=5000, budget_seconds=20, batch_size=64):
    """Return a complete literal page, None for unanchorable terms, or raise on failure.

    Enabling this path attests that the ngram index's persistent table stopword policy
    was verified OFF. Non-ASCII queries require the existing exact fallback.
    Candidate anchors are a superset of the UI's ASCII lowercase literal matching.
    """
    anchor = candidate_query(keyword)
    if anchor is None:
        return None
    if int(limit) <= 0:
        return []
    deadline = time.monotonic() + budget_seconds

    def query(select, params, phase):
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise InfixSearchError("Indexed transcript search exceeded its deadline")
        started = time.perf_counter()
        try:
            return db.execute_query_readonly_raising(
                f"SELECT /*+ MAX_EXECUTION_TIME({max(1, min(query_ms, remaining))}) */ " + select, params)
        except Exception as exc:
            raise InfixSearchError("Indexed transcript search could not finish") from exc
        finally:
            from utils.server_logger import log_timing
            elapsed = (time.perf_counter() - started) * 1000
            log_timing(f"TRANSCRIPT_INFIX_{phase}", elapsed, f"keyword={keyword[:80]!r}",
                       level="WARNING" if elapsed > 500 else "INFO")

    hits = query(f"id FROM {TABLE} FORCE INDEX ({INDEX}) "
                 "WHERE MATCH(transcript_text) AGAINST(:ngram_query IN BOOLEAN MODE)",
                 {"ngram_query": anchor}, "CANDIDATES")
    if not hits:
        return []
    candidate_ids = json.dumps(list(dict.fromkeys(row["id"] for row in hits)))
    where = ["has_transcript = 1", "id IN (SELECT hit_id FROM JSON_TABLE(:candidate_ids, '$[*]' "
             "COLUMNS(hit_id BIGINT PATH '$')) hits)"]
    filters = {"candidate_ids": candidate_ids, "batch_size": batch_size}
    if scoped_tickers:
        names = []
        for i, ticker in enumerate(scoped_tickers):
            name = f"scope_ticker_{i}"
            names.append(f":{name}")
            filters[name] = ticker
        where.append("ticker IN (" + ", ".join(names) + ")")
    if year is not None:
        where.append("year = :year")
        filters["year"] = year
    if q is not None:
        where.append("q = :q")
        filters["q"] = q
    cursor = after
    skip = max(0, int(offset or 0))
    selected = []
    while True:
        params = filters.copy()
        predicates = where.copy()
        if cursor is not None:
            predicates.append("(year, q, id) < (:scan_year, :scan_q, :scan_id)")
            params.update(scan_year=cursor[0], scan_q=cursor[1], scan_id=cursor[2])
        candidates = query(f"id, ticker, year, q FROM {TABLE} FORCE INDEX (idx_ec_list_ordered) "
                           f"WHERE {' AND '.join(predicates)} "
                           "ORDER BY year DESC, q DESC, id DESC LIMIT :batch_size", params, "METADATA")
        if not candidates:
            return selected
        text_params = {f"text_id_{i}": row["id"] for i, row in enumerate(candidates)}
        keys = ", ".join(f":{name}" for name in text_params)
        text_params.update(keyword=keyword, needed=skip + int(limit) - len(selected))
        matches = query(f"id, quarter, transcript_text FROM {TABLE} FORCE INDEX (PRIMARY) "
                        f"WHERE id IN ({keys}) AND has_transcript = 1 "
                        f"AND {literal_match_sql(keyword)} "
                        "ORDER BY year DESC, q DESC, id DESC LIMIT :needed", text_params, "TEXT_MATCHES")
        by_id = {row["id"]: row for row in matches}
        for candidate in candidates:
            match = by_id.get(candidate["id"])
            if match is None:
                continue
            if match.get("transcript_text") is None:
                raise InfixSearchError("Indexed transcript search returned incomplete text")
            if skip:
                skip -= 1
                continue
            selected.append({**candidate, **match})
            if len(selected) == int(limit):
                return selected
        if len(candidates) < batch_size:
            return selected
        last = candidates[-1]
        cursor = (last["year"], last["q"], last["id"])
