"""Background auto-enrichment of NEW M&A calendar events (no DB writes).

The nightly key-developments ETL keeps inserting 'M&A Closing' rows whose
ma_acquirer/ma_target are regex garbage (see 2026-07-15 M&A repair spec). The
curated repo overlay (app/data/ma_event_overrides.json) fixes the 262 rows
known at build time; THIS module keeps the calendar correct for rows inserted
after that, until the data team fixes the upstream extractor:

  * one daemon thread, ticking every MA_OVERRIDES_AUTO_SLEEP_SEC (default 6h)
  * each tick diffs the calendar's M&A event_ids against BOTH overlay files
  * for new ids only (typically 0-2/day) it fetches the source 8-K via
    edgartools — entirely off the request path — and runs the deterministic,
    precision-tested extractor (utils/ma_8k_extract.py)
  * results land in a runtime overlay JSON in the MDP cache dir, which
    repository.get_ma_completion_events merges under the repo overlay

Page renders never wait on this: they only ever read two local JSON files.
Failed fetches are retried at most MAX_ATTEMPTS times, then left alone (the
calendar shows "—" for them via the display sanitizer). Kill switch:
ENABLE_MA_OVERRIDES_AUTO=0.
"""
import json
import os
import re
import threading
import time
from typing import Any, Dict, Optional

from utils.server_logger import log_info, log_structured_error, log_timing
from utils.ma_8k_extract import extract_item_sections, extract_ma_fields

RUNTIME_OVERLAY_BASENAME = "ma_event_overrides_runtime.json"
REPO_OVERLAY_BASENAME = "ma_event_overrides.json"           # LLM-verified gold, 262 events
EDGAR_OVERLAY_BASENAME = "ma_event_overrides_edgar.json"    # deterministic EDGAR backfill
SECFORMS_OVERLAY_BASENAME = "ma_event_overrides_secforms.json"  # SEC form headers + fee exhibit
MAX_ATTEMPTS = 3
MAX_FETCH_PER_TICK = 40

_CALENDAR_MA_IDS_QUERY = """
    SELECT event_id, source_ref
    FROM coreiq_company_events
    WHERE event_category = 'M&A Activity'
      AND event_subtype  = 'M&A Closing'
      AND is_active = 1
      AND ma_is_closed = 1
      AND (ma_deal_type IS NULL OR ma_deal_type <> 'spin-off')
      AND event_date IS NOT NULL
"""


def runtime_overlay_path() -> Optional[str]:
    from utils.materialize import _cache_dir
    d = _cache_dir()
    return os.path.join(d, RUNTIME_OVERLAY_BASENAME) if d else None


def data_overlay_path(basename: str) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", basename)


def _read_overlay(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {str(k): v for k, v in (json.load(f).get("events", {}) or {}).items()}
    except Exception as exc:
        log_structured_error(exc, page="ma_overrides_auto", component="_read_overlay",
                             operation="load_overlay", context=path)
        return {}


_OVERLAY_CACHE: Dict[str, Any] = {"stamp": None, "merged": {}}


def load_ma_overlays() -> Dict[str, Dict[str, Any]]:
    """Every M&A field correction, keyed by event_id (str). One shared reader.

    Precedence, weakest first — runtime (deterministic, written by the daemon for
    rows the ETL added later) < edgar (deterministic offline backfill) < secforms
    (SEC merger-form headers: both parties named by the filer itself and matched
    on CIK, so it outranks anything we extract ourselves) < repo (LLM-verified
    gold). Reloaded only when a file's mtime changes, so a page render costs one
    os.stat per file, not a JSON parse.
    """
    paths = [runtime_overlay_path(),
             data_overlay_path(EDGAR_OVERLAY_BASENAME),
             data_overlay_path(SECFORMS_OVERLAY_BASENAME),
             data_overlay_path(REPO_OVERLAY_BASENAME)]
    stamp = tuple(os.path.getmtime(p) if p and os.path.exists(p) else None for p in paths)
    if _OVERLAY_CACHE["stamp"] != stamp:
        merged: Dict[str, Any] = {}
        for path in paths:
            merged.update(_read_overlay(path))
        _OVERLAY_CACHE["stamp"] = stamp
        _OVERLAY_CACHE["merged"] = merged
    return _OVERLAY_CACHE["merged"]


def _repo_overlay_ids() -> set:
    return set(_read_overlay(data_overlay_path(REPO_OVERLAY_BASENAME)).keys())


def _load_runtime() -> Dict[str, Any]:
    path = runtime_overlay_path()
    if not path or not os.path.exists(path):
        return {"events": {}, "attempts": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {"events": raw.get("events", {}) or {},
                "attempts": raw.get("attempts", {}) or {}}
    except Exception:
        return {"events": {}, "attempts": {}}


def _write_runtime(data: Dict[str, Any]) -> None:
    path = runtime_overlay_path()
    if not path:
        return
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _accession_from_url(url: Optional[str]) -> Optional[str]:
    m = re.search(r"/(\d{18})/", url or "")
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"


def run_ma_overrides_auto_tick() -> int:
    """One diff-and-enrich pass. Returns number of newly enriched events."""
    from core.database import db_manager

    t0 = time.perf_counter()
    rows = db_manager.execute_query_readonly(_CALENDAR_MA_IDS_QUERY, {})
    if not rows:
        return 0
    known = _repo_overlay_ids()
    runtime = _load_runtime()
    known |= set(runtime["events"].keys())

    todo = []
    for r in rows:
        eid = str(r["event_id"])
        if eid in known:
            continue
        if int(runtime["attempts"].get(eid, 0)) >= MAX_ATTEMPTS:
            continue
        todo.append((eid, r.get("source_ref")))
    if not todo:
        return 0
    todo = todo[:MAX_FETCH_PER_TICK]
    log_info(f"[ma_overrides_auto] {len(todo)} new M&A event(s) to enrich")

    # edgartools is imported lazily: only ticks that actually found new rows
    # pay for it, and boot never does
    from edgar import set_identity, find
    set_identity("Coresight Research mohdsaeedafri@coresight.com")

    enriched = 0
    for eid, source_ref in todo:
        runtime["attempts"][eid] = int(runtime["attempts"].get(eid, 0)) + 1
        try:
            acc = _accession_from_url(source_ref)
            if not acc:
                # nothing fetchable — record a no-name entry so the popup
                # shows "—" instead of the DB garbage, and stop retrying
                runtime["events"][eid] = {"acquirer": None, "target": None,
                                          "auto": True, "reason": "no_accession"}
                continue
            filing = find(acc)
            item_text = extract_item_sections(filing.text())
            fields = extract_ma_fields(str(getattr(filing, "company", "") or ""), item_text)
            fields.update({"accession": acc, "auto": True})
            runtime["events"][eid] = fields
            enriched += 1
        except Exception as exc:
            log_structured_error(
                exc, page="ma_overrides_auto", component="run_ma_overrides_auto_tick",
                operation="enrich_event", context=f"event_id={eid} ref={source_ref}",
            )
    _write_runtime(runtime)
    log_timing("ma_overrides_auto_tick", (time.perf_counter() - t0) * 1000,
               details=f"new={len(todo)} enriched={enriched}")
    return enriched


def _enabled() -> bool:
    return os.getenv("ENABLE_MA_OVERRIDES_AUTO", "1").strip() not in ("0", "", "false", "False")


def start_ma_overrides_auto() -> None:
    """Start ONE daemon thread. Caller guards double-start via env flag."""
    if not _enabled():
        log_info("[ma_overrides_auto] disabled (ENABLE_MA_OVERRIDES_AUTO=0)")
        return

    def _loop() -> None:
        try:
            sleep_s = max(600, int(os.getenv("MA_OVERRIDES_AUTO_SLEEP_SEC", "21600")))
        except ValueError:
            sleep_s = 21600
        time.sleep(int(os.getenv("MA_OVERRIDES_AUTO_BOOT_DELAY_SEC", "120")))
        log_info(f"[ma_overrides_auto] thread started — poll={sleep_s}s")
        while True:
            try:
                run_ma_overrides_auto_tick()
            except Exception as exc:
                log_structured_error(
                    exc, page="ma_overrides_auto", component="start_ma_overrides_auto",
                    operation="tick", context="auto-enrich tick failed",
                )
            time.sleep(sleep_s)

    threading.Thread(target=_loop, daemon=True, name="ma-overrides-auto").start()


# ── SEC deal-closure index ───────────────────────────────────────────────────
#
# `ma_deal_status` cannot be shown as-is: verified against 5,775 source filings,
# 'closed' is right on only 29.2% of rows (1,442 false positives, mostly Item 1.01
# credit agreements). SEC publishes the real signal as structure:
#   * an 8-K whose DECLARED items include 2.01 ("Completion of Acquisition or
#     Disposition of Assets") — 92.2% precision;
#   * Form 25 / Form 15, which a target files only AFTER a takeover completes.
# scripts/sec_closure_index.py harvests both offline into app/data. Renders only
# ever read that local file — never SEC — so the page stays instant.

CLOSURE_INDEX_BASENAME = "sec_closure_index.json"
_CLOSURE_CACHE: Dict[str, Any] = {"mtime": None, "companies": {}}


def load_sec_closure_index() -> Dict[str, Any]:
    """Per-ticker SEC closure evidence. Reloaded only when the file changes."""
    path = data_overlay_path(CLOSURE_INDEX_BASENAME)
    try:
        mtime = os.path.getmtime(path) if os.path.exists(path) else None
    except OSError:
        mtime = None
    if mtime != _CLOSURE_CACHE["mtime"]:
        companies: Dict[str, Any] = {}
        if mtime is not None:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    companies = json.load(f).get("companies", {}) or {}
            except Exception as exc:
                log_structured_error(exc, page="ma_overrides_auto",
                                     component="load_sec_closure_index",
                                     operation="load_closure_index", context=path)
        _CLOSURE_CACHE["mtime"] = mtime
        _CLOSURE_CACHE["companies"] = companies
    return _CLOSURE_CACHE["companies"]


def sec_closure_for_event(ticker: Optional[str], event_date: Any,
                          window_days: int = 21) -> Optional[str]:
    """Label an event 'closed' only when SEC structure says so, near its date.

    Returns a short provenance string ("SEC 8-K Item 2.01", "SEC delisting") or
    None. The window keeps an old completion from marking an unrelated new event.
    """
    if not ticker or not event_date:
        return None
    entry = load_sec_closure_index().get(str(ticker).upper()) or \
        load_sec_closure_index().get(str(ticker))
    if not entry:
        return None
    try:
        from datetime import date, datetime
        if isinstance(event_date, datetime):
            event_date = event_date.date()
        if not isinstance(event_date, date):
            event_date = datetime.strptime(str(event_date)[:10], "%Y-%m-%d").date()
    except Exception:
        return None

    def near(iso: Optional[str]) -> bool:
        if not iso:
            return False
        try:
            from datetime import datetime as _dt
            return abs((_dt.strptime(iso[:10], "%Y-%m-%d").date() - event_date).days) <= window_days
        except Exception:
            return False

    if any(near(f.get("date")) for f in entry.get("item201") or ()):
        return "SEC 8-K Item 2.01"
    if any(near(f.get("date")) for f in entry.get("delistings") or ()):
        return "SEC delisting"
    return None
