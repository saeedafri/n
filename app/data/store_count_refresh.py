"""Keep the store-count cache current without anyone having to remember to.

Runs as one low-priority background thread started at boot. Every pass it asks
a single question — *which filings do we hold that we have not extracted yet?* —
and works through whatever the answer is. That covers both cases the business
cares about:

  * a new company appears in coreiq_companies and its 10-Ks are in blob
  * an existing company files a new 10-K

Both are the same thing to this worker: an accession with no cache entry.

Design constraints, in priority order:

  1. NEVER slow a page down. The thread is a daemon, sleeps between filings,
     and every reader path falls back to the existing DB behaviour when the
     cache has nothing. Nothing waits on it.
  2. NEVER surprise the bill. LLM use is off unless STORE_COUNT_REFRESH_LLM=1,
     and even then a per-pass ceiling applies. The free layers alone resolve
     most filings.
  3. NEVER repeat work. A filed 10-K is immutable, so a cached accession is
     never re-read.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

from utils.server_logger import log_error, log_structured_error, log_timing

_worker: Optional[threading.Thread] = None
_lock = threading.Lock()

# Gentle by default: a filing every few seconds costs nothing noticeable and
# still clears a few hundred a day, which is far more than filings arrive.
PAUSE_BETWEEN_FILINGS = float(os.getenv("STORE_COUNT_REFRESH_PAUSE", "3.0"))
PAUSE_BETWEEN_PASSES = float(os.getenv("STORE_COUNT_REFRESH_INTERVAL", "21600"))
MAX_PER_PASS = int(os.getenv("STORE_COUNT_REFRESH_MAX", "200"))
LLM_ENABLED = os.getenv("STORE_COUNT_REFRESH_LLM", "0") == "1"
LLM_BUDGET_PER_PASS = float(os.getenv("STORE_COUNT_REFRESH_BUDGET", "0.10"))


def _pending_filings(limit: int) -> List[Dict[str, Any]]:
    """10-Ks in blob storage with no cache entry yet, newest first.

    Newest first matters: a freshly filed 10-K is what users are looking at,
    and a decade-old backfill gap can wait for the next pass.
    """
    from data import store_count_cache as cache
    from data.store_count_extractor import NON_RETAIL_TICKERS
    from utils.azure_blob import get_container_client

    known = set()
    index_file = cache.cache_dir() / "_index.json"
    if index_file.is_file():
        try:
            import json
            for accessions in json.loads(index_file.read_text()).values():
                known.update(accessions)
        except Exception:
            pass

    pending: List[Dict[str, Any]] = []
    try:
        container = get_container_client()
    except Exception:
        return pending

    # Only companies the portal actually lists — the container holds more.
    from core.database import db_manager
    tickers = [row["ticker"] for row in db_manager.execute_query_readonly(
        "SELECT DISTINCT ticker FROM coreiq_companies WHERE ticker IS NOT NULL", {})]

    for ticker in tickers:
        if ticker.upper() in NON_RETAIL_TICKERS:
            continue
        try:
            blobs = [b.name for b in container.list_blobs(name_starts_with=f"{ticker}/")
                     if b.name.endswith("/10-K/filing.html")]
        except Exception:
            continue
        for blob in sorted(blobs, reverse=True):
            if any(blob in accession for accession in known):
                continue
            pending.append({"ticker": ticker, "blob": blob})
            if len(pending) >= limit:
                return pending
    return pending


def _extract_one(filing: Dict[str, Any], container, cursor, budget: Dict[str, float]) -> bool:
    """Run the ladder for one filing and cache the result. True if it produced
    a value."""
    from data import store_count_cache as cache
    from data.store_count_extractor import (
        choose_best, extract_from_filing, filing_to_text, fiscal_year_of)
    from data.store_count_sources import (
        geography_from_filing, roll_up_continents, xbrl_by_year, xbrl_rows_for)

    raw = container.get_blob_client(filing["blob"]).download_blob().readall()
    text = filing_to_text(raw.decode("utf-8", "ignore"))
    stated = fiscal_year_of(text)
    if not stated:
        return False
    year, fye = stated
    accession = filing["blob"]
    if cache.read(f"{accession}::{year}"):
        return False

    value, rule, evidence = None, "none", ""
    facts = xbrl_by_year(xbrl_rows_for(cursor, filing["ticker"])).get(year)
    if facts and facts.value:
        value, rule, evidence = facts.value, facts.source_rule, facts.evidence

    best = choose_best(extract_from_filing(text))
    if best and best.value:
        # The filing is the source of truth — it outranks the XBRL layer.
        value, rule, evidence = best.value, best.source_rule, best.evidence

    if value is None and LLM_ENABLED and budget["spent"] < LLM_BUDGET_PER_PASS:
        from openai import OpenAI
        from data.store_count_sources import ask_llm, llm_call_cost, llm_windows
        try:
            parsed, prompt_tokens, completion_tokens = ask_llm(
                OpenAI(api_key=os.environ["OPENAI_API_KEY"]),
                filing["ticker"], year, fye, llm_windows(text))
            budget["spent"] += llm_call_cost(prompt_tokens, completion_tokens)
            if parsed and parsed.get("found") and parsed.get("value"):
                value, rule = int(parsed["value"]), "L3_llm"
                evidence = str(parsed.get("quote") or "")[:400]
        except Exception:
            pass

    if value is None:
        return False

    by_country = geography_from_filing(text, value)
    cache.write(f"{accession}::{year}", {
        "ticker": filing["ticker"], "fiscal_year": year, "value": value,
        "confidence": "medium", "basis": rule, "evidence": evidence[:400],
        "fye_date": fye, "by_country": by_country,
        "by_continent": roll_up_continents(by_country), "needs_review": False,
    })
    return True


def _run() -> None:
    from data import store_count_cache as cache
    from core.database import db_manager
    from utils.azure_blob import get_container_client

    while True:
        started = time.time()
        produced = 0
        budget = {"spent": 0.0}
        try:
            pending = _pending_filings(MAX_PER_PASS)
            if pending:
                container = get_container_client()
                import pymysql  # noqa: F401 — cursor comes from the shared engine
                connection = db_manager._engine.raw_connection()
                cursor = connection.cursor(pymysql.cursors.DictCursor)
                try:
                    for filing in pending:
                        try:
                            produced += bool(_extract_one(filing, container, cursor, budget))
                        except Exception as exc:
                            log_structured_error(
                                exc, page="store_count_refresh",
                                component="store_count_refresh._extract_one",
                                operation="extract_filing",
                                context=filing.get("blob", ""))
                        time.sleep(PAUSE_BETWEEN_FILINGS)
                finally:
                    cursor.close()
                    connection.close()
                if produced:
                    cache.rebuild_index()
            log_timing("store_count_refresh_pass", (time.time() - started) * 1000,
                       details=f"pending={len(pending)} cached={produced} "
                               f"llm=${budget['spent']:.4f}")
        except Exception as exc:
            log_structured_error(exc, page="store_count_refresh",
                                 component="store_count_refresh._run",
                                 operation="refresh_pass")
        time.sleep(PAUSE_BETWEEN_PASSES)


def start_store_count_refresh() -> None:
    """Start the background refresh once per process. Safe to call repeatedly."""
    global _worker
    if os.getenv("STORE_COUNT_REFRESH", "1") != "1":
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name="store_count_refresh",
                                   daemon=True)
        _worker.start()
