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


def _repo_overlay_ids() -> set:
    try:
        path = os.path.join(os.path.dirname(__file__), "..", "data", "ma_event_overrides.json")
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f).get("events", {}).keys())
    except Exception:
        return set()


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
