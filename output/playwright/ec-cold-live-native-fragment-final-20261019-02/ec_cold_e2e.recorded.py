"""Record real-browser, result-cache-bypassed Earnings Calls scenarios on port 8513."""

import argparse
import ast
import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
CURRENT_SQL_PATH = None
ACTION_SQL_WATERMARK = 0
ACTION_REQUIRES_SEARCH_QUERY = False
CURRENT_SCOPE_LOG = None
ACTION_LOG_WATERMARK = 0


def set_audit_case(path, scope_log=None):
    global CURRENT_SQL_PATH, ACTION_SQL_WATERMARK, ACTION_REQUIRES_SEARCH_QUERY, CURRENT_SCOPE_LOG, ACTION_LOG_WATERMARK
    CURRENT_SQL_PATH = path
    ACTION_SQL_WATERMARK = 0
    ACTION_REQUIRES_SEARCH_QUERY = False
    CURRENT_SCOPE_LOG = scope_log
    ACTION_LOG_WATERMARK = 0
PORT = 8513
SAMPLER = r"""(() => {
  window.__ecColdSamples = []; window.__ecColdAction = 'startup'; window.__ecColdExpect = false;
  function visible(e) {
    if (!e) return false;
    let r = e.getBoundingClientRect(); if (!r.width || !r.height || r.bottom < 0 || r.top > innerHeight) return false;
    for (let p=e; p && p.nodeType===1; p=p.parentElement) {
      let s=getComputedStyle(p); if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<0.1) return false;
    } return true;
  }
  setInterval(() => {
    const cards=[...document.querySelectorAll('.transcript-search-result-card')];
    const status=[...document.querySelectorAll('[data-testid="stStatusWidget"]')];
    const loader=[...document.querySelectorAll('.cs-al-ov,.cs-page-subspinner')];
    const alerts=[...document.querySelectorAll('[data-testid="stException"],[data-testid="stAlert"]')];
    window.__ecColdSamples.push({at:Date.now(), perf:performance.now(), action:window.__ecColdAction,
      expecting:window.__ecColdExpect, loader:loader.some(visible),
      loader_styles:loader.map(e=>({opacity:getComputedStyle(e).opacity,display:getComputedStyle(e).display,visible:visible(e)})),
      busy:document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')!=='notRunning',
      app_state:document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state'),
      cards:cards.map(e=>({text:e.innerText,href:e.querySelector('a')?.getAttribute('href')||''})),
      errors:alerts.filter(visible).map(e=>e.innerText),
      controls:[...document.querySelectorAll('input,[role="combobox"]')].map(e=>({label:e.getAttribute('aria-label'),value:e.value||e.innerText})),
      body:document.body?.innerText||''});
  },100);
})();"""


def progress(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def source_hashes():
    files = ["app/core/database.py", "app/data/repository.py", "app/data/news_mirror.py",
             "app/pages/earnings_calls.py", "app/components/loading.py", "app/data/watchlist_service.py",
             "app/utils/local_storage_manager.py",
             "scripts/ec_cold_app.py", "scripts/ec_cold_e2e.py"]
    if (ROOT / "app/data/transcript_infix.py").exists():
        files.append("app/data/transcript_infix.py")
    if (ROOT / "scripts/ec_ngram_corpus_probe.py").exists():
        files.append("scripts/ec_ngram_corpus_probe.py")
    if (ROOT / "scripts/ec_transcript_index_migration.py").exists():
        files.append("scripts/ec_transcript_index_migration.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def load_environment():
    env = os.environ.copy()
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    env.update(APP_ENV="LOCAL", DEBUG="true", LOCAL_TEST_USER_EMAIL=env.get("LOCAL_TEST_USER_EMAIL") or "mohdsaeedafri@coresight.com",
               ENABLE_SSL="true", PYTHONUNBUFFERED="1", STREAMLIT_BROWSER_GATHER_USAGE_STATS="false")
    for key in ("HOST", "PORT", "NAME", "USER", "PASSWORD"):
        env[f"DB_{key}"] = env.get(f"STG_DB_{key}", "3306" if key == "PORT" else "")
    return env


def inspect_local_index(path):
    path = path.resolve()
    if not path.is_file() or path.name != "news_mirror.sqlite":
        raise RuntimeError("--local-index-file must identify an existing news_mirror.sqlite")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('tx_words','tx_vocab','ix_tx_search_metadata')")}
        state = dict(conn.execute("SELECT k,v FROM mirror_state WHERE k IN ('tx:complete','tx:verify','tx:verify_at','tx:reconciled_at','tx:max_id','tx:words','companies:complete')"))
    reconciled = float(state.get("tx:reconciled_at") or 0)
    return {"path": str(path), "size_bytes": path.stat().st_size, "state": state,
            "index_objects": sorted(names),
            "ready_at_start": state.get("tx:complete") == "1" and state.get("tx:words") == "1"
                              and state.get("companies:complete") == "1" and state.get("tx:verify") != "1",
            "last_reconcile_age_seconds": time.time() - reconciled if reconciled else None,
            "origin_freshness_verified": False,
            "freshness_note": "Existing index state recorded; no origin oracle before videos and background synchronization disabled."}


async def flush_samples(page, path):
    try:
        rows = await page.evaluate("() => window.__ecColdSamples?.splice(0) || []")
        with path.open("a") as out:
            for row in rows:
                out.write(json.dumps(row) + "\n")
    except Exception:
        pass


async def mark(page, label, expecting=True):
    global ACTION_SQL_WATERMARK, ACTION_REQUIRES_SEARCH_QUERY, ACTION_LOG_WATERMARK
    if expecting:
        rows = audit_sql_rows()
        ACTION_SQL_WATERMARK = max((row["counter"] for row in rows), default=0)
        ACTION_REQUIRES_SEARCH_QUERY = bool(re.match(r"search\b|Show more\b|new_keyword|restore_original_keyword", label, re.I))
        ACTION_LOG_WATERMARK = CURRENT_SCOPE_LOG.stat().st_size if CURRENT_SCOPE_LOG and CURRENT_SCOPE_LOG.exists() else 0
    await page.evaluate("([a,e])=>{window.__ecColdAction=a;window.__ecColdExpect=e}", [label, expecting])
    progress(label)


def audit_sql_rows():
    if CURRENT_SQL_PATH is None or not CURRENT_SQL_PATH.exists():
        return []
    rows = []
    for line in CURRENT_SQL_PATH.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def action_sql_complete():
    if CURRENT_SQL_PATH is None:
        return True
    rows = [row for row in audit_sql_rows() if row["counter"] > ACTION_SQL_WATERMARK]
    starts = {row["counter"]: row for row in rows if row["event"] == "start"}
    ended = {row["counter"] for row in rows if row["event"] == "end"}
    finished = {row["counter"] for row in rows if row["event"] in {"end", "error"}}
    required = [counter for counter, row in starts.items()
                if not ACTION_REQUIRES_SEARCH_QUERY or row.get("role", "").startswith("search_")]
    return bool(ended.intersection(required)) and not (starts.keys() - finished)


def canonical_global_scope_complete(keyword):
    if not CURRENT_SCOPE_LOG or not CURRENT_SCOPE_LOG.exists():
        return False
    with CURRENT_SCOPE_LOG.open() as source:
        source.seek(ACTION_LOG_WATERMARK)
        fresh = source.read()
    for raw in re.findall(r"\bEC_EXPORT_STATE\b[^\n]*?sig=(.*?) job=", fresh):
        try:
            if ast.literal_eval(raw) == (keyword, "ALL", "ALL", "ALL", None):
                return True
        except (ValueError, SyntaxError):
            pass
    return False


async def wait_complete(page, timeout, *, cards=None, initial=False, empty=False, allow_empty=False, expected_keyword=None, restored_pages=None):
    deadline = time.monotonic() + timeout
    stable = None
    last = None
    missing_keyword_since = None
    while time.monotonic() < deadline:
        snapshot = await page.evaluate("""() => {
          function visible(e) {
            let r=e.getBoundingClientRect(); if(!r.width||!r.height)return false;
            for(let p=e;p&&p.nodeType===1;p=p.parentElement){let s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<0.1)return false;}return true;
          }
          return ({
          cards:[...document.querySelectorAll('.transcript-search-result-card')].map(e=>({text:e.innerText,href:e.querySelector('a')?.getAttribute('href')||'',meta:e.querySelector('.transcript-result-meta')?.innerText||''})),
          controls:document.querySelectorAll('[role="combobox"]').length,
          control_labels:[...document.querySelectorAll('[role="combobox"]')].map(e=>e.getAttribute('aria-label')||''),
          search_value:document.querySelector('input[aria-label="Search"]')?.value,
          loader:[...document.querySelectorAll('.cs-al-ov')].some(visible),
          busy:document.querySelector('[data-testid="stApp"]')?.getAttribute('data-test-script-state')!=='notRunning',
          error:[...document.querySelectorAll('[data-testid="stException"],[data-testid="stAlert"]')].map(e=>e.innerText).filter(t=>/error|could not|unavailable|cannot|failed/i.test(t)),
          noMatches:document.body.innerText.includes('No matches found')})}""")
        if snapshot["error"]:
            raise AssertionError("Visible application error: " + " | ".join(snapshot["error"])[:700])
        if expected_keyword is not None and not snapshot["busy"] and snapshot["controls"] >= 3 and snapshot["search_value"] != expected_keyword:
            missing_keyword_since = missing_keyword_since or time.monotonic()
            if time.monotonic() - missing_keyword_since > 2:
                raise AssertionError("Submitted search keyword was not retained")
        else:
            missing_keyword_since = None
        enough = snapshot["controls"] >= 3 if initial else (snapshot["noMatches"] and not snapshot["cards"] if empty else len(snapshot["cards"]) >= (cards or 1))
        enough = enough or (allow_empty and snapshot["noMatches"] and not snapshot["cards"])
        key = json.dumps(snapshot["cards"]) if not initial else str(snapshot["controls"])
        signature_matches = allow_empty or expected_keyword is None or not snapshot["cards"] or all(
            expected_keyword.lower() in card["text"].lower() for card in snapshot["cards"])
        restored_scope = restored_pages and snapshot["search_value"] == expected_keyword and canonical_global_scope_complete(expected_keyword) and card_keys(snapshot["cards"]) in restored_pages and all(
            any(re.search(pattern, label, re.I) for label in snapshot["control_labels"])
            for pattern in (r"Selected All Companies\. Company$", r"Selected ALL\. Year$", r"Selected ALL\. Quarter$", r"No watchlist.*Watchlist$"))
        keyword_matches = expected_keyword is None or snapshot["search_value"] == expected_keyword
        scope_matches = not restored_pages or restored_scope
        if enough and not snapshot["busy"] and not snapshot["loader"] and signature_matches and keyword_matches and scope_matches and (action_sql_complete() or restored_scope):
            if key != last:
                last, stable = key, time.monotonic()
            elif stable is not None and time.monotonic() - stable >= 1:
                await mark(page, "settled", False)
                return snapshot["cards"]
        else:
            stable = None
        if snapshot["noMatches"] and not snapshot["busy"] and not snapshot["loader"] and not initial and not empty and not allow_empty:
            raise AssertionError("Known keyword produced no matches")
        await page.wait_for_timeout(100)
    raise TimeoutError(f"Page did not reach verified {'controls' if initial else 'results'} within {timeout}s")


def card_keys(cards):
    return [hashlib.sha256((c["href"] + "\n" + c["text"]).encode()).hexdigest() for c in cards]


def restored_global_page(cards, manifest):
    keys = card_keys(cards)
    return keys in (manifest["pages"][0]["keys"], manifest["pages"][-1]["keys"])


def card_scope(card):
    values = parse_qs(urlparse(card["href"]).query)
    if values.get("ticker"):
        return values
    parts = [part.strip() for part in card.get("meta", "").split("•")]
    if len(parts) >= 3:
        ticker = re.search(r"\(([^()]*)\)\s*$", parts[0])
        values.update(ticker=[ticker.group(1) if ticker else parts[0]], year=[parts[1]], quarter=[parts[2]])
    return values


async def choose_filter(page, label, value=None):
    box = page.get_by_test_id("stSelectbox").filter(has_text=re.compile(r"^" + label))
    combo = box.get_by_role("combobox")
    before = await combo.get_attribute("aria-label") or ""
    matched = re.match(r"Selected (.*?)\.\s*" + re.escape(label) + r"$", before)
    current = matched.group(1) if matched else (await box.inner_text()).splitlines()[-1].strip()
    target = {"Company": "All Companies", "Watchlist": "No watchlist"}.get(label, "ALL") if value == "__FIRST__" else value
    if target and (target.casefold() == current.casefold() or label == "Watchlist" and target.casefold() in current.casefold()):
        return {"selected": current, "before": before, "no_op": True}
    await combo.click()
    if value == "__FIRST__":
        await combo.fill(target)
        option = page.get_by_role("option").filter(has_text=re.compile(re.escape(target), re.I)).first
        selected = (await option.inner_text()).strip()
        await option.click()
        return {"selected": selected, "before": before, "no_op": False}
    if value:
        await combo.fill(value)
        option = page.get_by_role("option").filter(has_text=re.compile(r"\b" + re.escape(value) + r"\b", re.I)).first
    else:
        options = page.get_by_role("option")
        option = None
        for i in range(await options.count()):
            candidate = options.nth(i)
            text = (await candidate.inner_text()).strip()
            if text != current and not re.search(r"^(?:ALL|—\s*No watchlist\s*—)$", text, re.I) and await candidate.get_attribute("aria-selected") != "true":
                option = candidate
                break
        if option is None:
            raise AssertionError(f"No distinct real {label} option available")
    selected = (await option.inner_text()).strip()
    await option.click()
    return {"selected": selected, "before": before, "no_op": False}


async def verify_filter_flow(page, keyword, cards, manifest, directory, args):
    first = parse_qs(urlparse(cards[0]["href"]).query)
    ticker = first.get("ticker", [None])[0]
    if not ticker or ticker == "ALL":
        raise AssertionError("First result did not expose a concrete company")
    manifest["filter_flow"] = []
    expected_scope = {}

    async def select(action, label, value=None, scope=None, clear_scope=False):
        await flush_samples(page, directory / "timeline.jsonl")
        started, wall = time.monotonic(), time.time()
        await mark(page, action)
        selection = await choose_filter(page, label, value)
        selected = selection["selected"]
        if selection["no_op"]:
            await mark(page, action + " no-op", False)
            result = await page.locator(".transcript-search-result-card").evaluate_all("els=>els.map(e=>({text:e.innerText,href:e.querySelector('a')?.getAttribute('href')||'',meta:e.querySelector('.transcript-result-meta')?.innerText||''}))")
        else:
            restored = [manifest["pages"][0]["keys"], manifest["pages"][-1]["keys"]] if action in {"reset_company", "reset_watchlist"} else None
            result = await wait_complete(page, args.timeout, allow_empty=True, expected_keyword=keyword, restored_pages=restored)
        elapsed = (time.monotonic() - started) * 1000
        row = {"action": action, **selection, "started_at": wall, "elapsed_ms": elapsed,
               "cards": len(result), "result_query_parameters": [card_scope(c) for c in result],
               "transcript_header": await page.locator(".ec-hdr-title-section").all_text_contents(),
               "controls_after": await page.get_by_role("combobox").evaluate_all("els=>els.map(e=>({label:e.getAttribute('aria-label'),value:e.value}))")}
        if action in {"reset_company", "reset_watchlist"} and not selection["no_op"]:
            row["canonical_scope_evidence"] = {"expected": [keyword, "ALL", "ALL", "ALL", None],
                "fresh_export_signature": canonical_global_scope_complete(keyword), "log_byte_watermark": ACTION_LOG_WATERMARK}
        manifest["action_timings"].append(row)
        manifest["filter_flow"].append(row)
        if clear_scope:
            expected_scope.clear()
        if scope:
            field, value = scope(selected)
            expected_scope[field] = value
        row["expected_scope"] = expected_scope.copy()
        if any(str(values.get(field, [""])[0]) != str(value)
               for values in row["result_query_parameters"] for field, value in expected_scope.items()):
            raise AssertionError(f"{action} returned a card outside the cumulative selected filters")
        if result and all(card["href"].startswith("#seg-") for card in result):
            header = " ".join(row["transcript_header"])
            if expected_scope.get("ticker") and f"({expected_scope['ticker']})" not in header:
                raise AssertionError("Concrete transcript header disagrees with selected company")
            period = re.search(r"(\d{4})\s*(Q[1-4])", header)
            if not period or any(str(expected_scope[field]) != period.group(i)
                                 for field, i in (("year", 1), ("quarter", 2)) if field in expected_scope):
                raise AssertionError("Concrete transcript header disagrees with selected period")
        await page.screenshot(path=str(directory / f"filter-{action}.png"))
        await page.wait_for_timeout(manifest["review_dwell_ms"])
        return result

    cards = await select("filter_company", "Company", ticker, lambda _: ("ticker", ticker))
    cards = await select("filter_year", "Year", scope=lambda s: ("year", s))
    cards = await select("filter_quarter", "Quarter", scope=lambda s: ("quarter", s))
    cards = await select("reset_company", "Company", "__FIRST__", clear_scope=True)
    if not restored_global_page(cards, manifest):
        raise AssertionError("Company ALL reset did not restore the original All/All/All result page")
    manifest["filter_flow"][-1]["reused_accumulator"] = card_keys(cards) == manifest["pages"][-1]["keys"]
    cards = await select("filter_watchlist", "Watchlist", clear_scope=True)
    cards = await select("reset_watchlist", "Watchlist", "__FIRST__", clear_scope=True)
    for label in ("Company", "Year", "Quarter"):
        box = page.get_by_test_id("stSelectbox").filter(has_text=re.compile(r"^" + label))
        if not re.search(r"\bAll\b", await box.inner_text(), re.I):
            cards = await select("restore_" + label.lower(), label, "__FIRST__")
    if not restored_global_page(cards, manifest):
        raise AssertionError("No-watchlist reset did not restore the original global result page")

    async def search(action, term):
        await flush_samples(page, directory / "timeline.jsonl")
        started, wall = time.monotonic(), time.time()
        await mark(page, action)
        control = page.get_by_test_id("stTextInput").filter(has_text=re.compile(r"^Search")).get_by_role("textbox")
        await control.fill(term)
        await control.press("Enter")
        result = await wait_complete(page, args.timeout, expected_keyword=term)
        manifest["action_timings"].append({"action": action, "keyword": term, "started_at": wall,
            "elapsed_ms": (time.monotonic() - started) * 1000, "cards": len(result)})
        await page.wait_for_timeout(manifest["review_dwell_ms"])
        return result

    cards = await search("new_keyword", "print" if keyword != "print" else "exchange")
    previous = card_keys(cards)
    await flush_samples(page, directory / "timeline.jsonl")
    started, wall = time.monotonic(), time.time()
    await mark(page, "new_keyword_show_more")
    await page.get_by_role("button", name="Show more ↓", exact=True).click()
    cards = await wait_complete(page, args.timeout, cards=len(previous) + 1)
    keys = card_keys(cards)
    if keys[:len(previous)] != previous or len(set(keys)) != len(keys):
        raise AssertionError("New-keyword Show More lost, reordered or duplicated cards")
    manifest["action_timings"].append({"action": "new_keyword_show_more", "started_at": wall,
        "elapsed_ms": (time.monotonic() - started) * 1000, "cards": len(cards)})
    await page.screenshot(path=str(directory / "filter-new-keyword-more.png"))
    await page.wait_for_timeout(manifest["review_dwell_ms"])
    return await search("restore_original_keyword", keyword)


def analyze_timeline(path):
    samples = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    bad, start = [], None
    for sample in samples:
        completed_error = sample.get("errors") and not sample["busy"] and sample.get("app_state") == "notRunning"
        blank_busy = sample["action"] != "startup" and (sample["busy"] or sample["expecting"]) and not sample["loader"] and not sample["cards"] and not completed_error and len(sample["body"].strip()) < 600
        if blank_busy and start is None:
            start = sample["at"]
        if not blank_busy and start is not None:
            if sample["at"] - start > 500:
                bad.append({"start_ms": start, "duration_ms": sample["at"] - start})
            start = None
    if start is not None and samples[-1]["at"] - start > 500:
        bad.append({"start_ms": start, "duration_ms": samples[-1]["at"] - start})
    framework_warnings = sorted({error for sample in samples for error in sample.get("errors", [])
        if re.search(r"widget with key.*default value.*Session State API", error, re.I | re.S)})
    return {"samples": len(samples), "interval_ms": 100, "blank_busy_without_loader": bad,
            "framework_widget_binding_warnings": framework_warnings,
            "completed_error_samples_excluded": sum(bool(s.get("errors")) and not s["busy"] and s.get("app_state") == "notRunning" for s in samples),
            "loader_visible_samples": sum(bool(s["loader"]) for s in samples)}


def analyze_video(video, directory, actions=None, video_origin=None):
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate", "-of", "json", str(video)]))
    a, b = probe["streams"][0]["avg_frame_rate"].split("/")
    fps = float(a) / float(b)
    transition_frames = {}
    if video_origin is not None:
        for action in actions or []:
            start = action["started_at"] - video_origin
            end = start + action["elapsed_ms"] / 1000
            for seconds, stage in ((start, "start"), (start + 0.5, "busy"), (end - 0.5, "finishing"), (end, "complete")):
                transition_frames.setdefault(max(0, round(seconds * fps)), []).append(action["action"] + " " + stage)
    command = ["ffmpeg", "-v", "error", "-i", str(video), "-vf", "scale=360:250", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    pipe = subprocess.Popen(command, stdout=subprocess.PIPE)
    frame_bytes, count, blank, faded, roi_blank, roi_faded = 360 * 250 * 3, 0, 0, 0, 0, 0
    contacts = []
    with (directory / "video-frames.csv").open("w") as out:
        out.write("frame,time_s,mean,std,bright_ratio,red_ratio,near_blank,low_contrast_fade,main_roi_mean,main_roi_std,main_roi_near_blank,main_roi_low_contrast_fade\n")
        while True:
            raw = pipe.stdout.read(frame_bytes)
            if not raw:
                break
            if len(raw) != frame_bytes:
                raise RuntimeError("Incomplete decoded video frame")
            pixels = np.frombuffer(raw, dtype=np.uint8).reshape(250, 360, 3)
            gray = pixels.mean(axis=2)
            mean, std = float(gray.mean()), float(gray.std())
            bright = float((gray > 240).mean())
            red = float(((pixels[:, :, 0] > 150) & (pixels[:, :, 0].astype(int) > pixels[:, :, 1].astype(int) * 1.35) & (pixels[:, :, 0].astype(int) > pixels[:, :, 2].astype(int) * 1.35)).mean())
            near_blank = std < 1
            low_contrast = mean > 225 and 1 <= std < 10
            roi = gray[80:233, 118:348]
            roi_mean, roi_std = float(roi.mean()), float(roi.std())
            roi_is_blank = roi_std < 1
            roi_is_faded = roi_mean > 225 and 1 <= roi_std < 10
            blank += near_blank
            faded += low_contrast
            roi_blank += roi_is_blank
            roi_faded += roi_is_faded
            out.write(f"{count},{count/fps:.3f},{mean:.3f},{std:.3f},{bright:.5f},{red:.5f},{int(near_blank)},{int(low_contrast)},{roi_mean:.3f},{roi_std:.3f},{int(roi_is_blank)},{int(roi_is_faded)}\n")
            if count % max(1, int(fps * 5)) == 0 or count in transition_frames:
                contacts.append((count / fps, Image.fromarray(pixels.copy()), ", ".join(transition_frames.get(count, []))))
            count += 1
    if pipe.wait() != 0:
        raise RuntimeError("Video decoding failed")
    if count and (count - 1) / fps > contacts[-1][0]:
        contacts.append(((count - 1) / fps, Image.fromarray(pixels.copy()), "final frame"))
    sheets = []
    for offset in range(0, len(contacts), 24):
        group = contacts[offset:offset + 24]
        sheet = Image.new("RGB", (360 * 4, 275 * ((len(group) + 3) // 4)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, (seconds, frame, label) in enumerate(group):
            x, y = (i % 4) * 360, (i // 4) * 275
            sheet.paste(frame, (x, y))
            draw.text((x + 5, y + 252), f"{seconds:.1f}s {label}"[:65], fill="black")
        target = directory / f"contact-sheet-{offset//24:02d}.jpg"
        sheet.save(target, quality=90)
        sheets.append(str(target))
    return {"decoded_frames": count, "fps": fps, "duration_s": count / fps,
            "near_blank_ratio": blank / max(count, 1), "low_contrast_fade_ratio": faded / max(count, 1),
            "main_content_roi": {"scaled_frame_rect_xyxy": [118, 80, 348, 233], "scaled_frame_size": [360, 250],
                "near_blank_ratio": roi_blank / max(count, 1), "low_contrast_fade_ratio": roi_faded / max(count, 1),
                "interpretation": "Raw measurements excluding header/sidebar; the normal cross-search placeholder can be quiet/bright. These flags alone never imply a defect; compare DOM loader, empty/error state and transcript validation."},
            "method": "Every decoded frame scaled to360x250; blank=std<1; lowcontrastfade=mean>225 and1<=std<10. Loading overlays intentionally fade content; these metrics are paired with100ms DOM loader evidence.",
            "contact_transition_alignment": "Approximate video frame targets from wall-clock page-creation bounds; exact action wall clocks and100ms DOM states remain in manifest/timeline.",
            "contact_sheets": sheets}


async def record_case(keyword, directory, args):
    directory.mkdir(parents=True)
    set_audit_case(None if args.local_index else directory / "sql.jsonl", directory / "server-logs/server-log.log")
    env = load_environment()
    cache = Path(tempfile.mkdtemp(prefix="ec-cold-cache-"))
    env.update(EC_COLD_AUDIT_DIR=str(directory), SERVER_LOG_DIR=str(directory / "server-logs"),
               MDP_CACHE_DIR=str(cache), NEWS_MIRROR_DIR=str(cache / "mirror"))
    mode = ("isolated_native_snapshot" if args.isolated_native_socket else
            "durable_sqlite_index" if args.local_index else "mysql_origin")
    env["EC_COLD_MODE"] = mode
    manifest = {"keyword": keyword, "status": "running", "directory": str(directory), "cache_dir": str(cache), "port": PORT,
                "mode": mode, "cache_scope": "application result caches bypassed; native MySQL/SQLite/OS buffers not flushed", "errors": [],
                "case_audit_initial_state": {"watermark": ACTION_SQL_WATERMARK, "requires_search_query": ACTION_REQUIRES_SEARCH_QUERY,
                                             "path": str(CURRENT_SQL_PATH) if CURRENT_SQL_PATH else None},
                "action_timings": [], "review_dwell_ms": 2000,
                "filter_flow_requested": bool(args.verify_filter_flow and keyword == args.keywords[0]),
                "fragment_query_verification": args.verify_fragment_queries,
                "expected_empty": keyword in args.expect_empty}
    if args.local_index:
        manifest["local_index"] = inspect_local_index(args.local_index_file)
        env["NEWS_MIRROR_DIR"] = str(args.local_index_file.resolve().parent)
    if args.isolated_native_socket:
        socket_path = args.isolated_native_socket.resolve()
        if args.isolated_native_restart:
            progress(f"{keyword!r}: restarting private MySQL; no corpus preflight query")
            controller_report = directory / "native-controller.json"
            controller_started = time.time()
            command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/ec_ngram_corpus_probe.py"),
                       "server", "--action", "restart", "--socket", str(socket_path),
                       "--output", str(controller_report)]
            with (directory / "native-controller-output.log").open("wb") as controller_output:
                controller = await asyncio.create_subprocess_exec(*command, cwd=ROOT,
                    stdout=controller_output, stderr=asyncio.subprocess.STDOUT)
                try:
                    code = await asyncio.wait_for(controller.wait(), timeout=75)
                except TimeoutError:
                    controller.kill()
                    await controller.wait()
                    raise RuntimeError("Private MySQL restart controller timed out")
            if code != 0:
                raise RuntimeError(f"Private MySQL restart controller failed with exit code {code}")
            report = json.loads(controller_report.read_text())
            if (report.get("corpus_queries_performed") != 0
                    or report.get("buffer_pool_load_at_startup") is not False
                    or report.get("buffer_pool_dump_at_shutdown") is not False):
                raise RuntimeError("Private MySQL restart proof did not satisfy cold policy")
            manifest["private_server_restart"] = {"started_at": controller_started,
                "elapsed_ms": (time.time() - controller_started) * 1000, "report": report}
            manifest["cache_scope"] = "application result caches bypassed; private MySQL buffer pool reset before this keyword; live STG/OS buffers uncontrolled"
        if not socket_path.is_socket():
            raise RuntimeError("--isolated-native-socket must identify a ready private MySQL Unix socket")
        env.update(EC_COLD_ISOLATED_SOCKET=str(socket_path), EC_COLD_ISOLATED_DB=args.isolated_native_db,
                   EC_COLD_ISOLATED_USER=args.isolated_native_user)
        manifest["isolated_native"] = {
            "socket": str(socket_path), "database": args.isolated_native_db,
            "origin": "private native MySQL eligible-transcript corpus snapshot, NOT live STG",
            "support_metadata_origin": "live STG, read-only",
            "index_flag": "TRANSCRIPT_NATIVE_NGRAM_SEARCH=1",
            "connections": "NullPool private connections; fresh browser/application process for each keyword",
            "readiness_manifest": str(args.isolated_native_manifest.resolve()) if args.isolated_native_manifest else None,
        }
        if args.isolated_native_manifest:
            manifest["isolated_native"]["readiness_manifest_sha256"] = hashlib.sha256(args.isolated_native_manifest.read_bytes()).hexdigest()
            readiness = json.loads(args.isolated_native_manifest.read_text())
            if not readiness.get("ready") or readiness.get("helper_sha256") != hashlib.sha256((ROOT / "app/data/transcript_infix.py").read_bytes()).hexdigest():
                raise RuntimeError("Private corpus readiness/helper hash does not match the frozen application")
    manifest["source_hashes_start"] = source_hashes()
    if manifest["source_hashes_start"] != args.frozen_source_hashes:
        raise RuntimeError("Source files changed after the campaign freeze")
    output = (directory / "server-output.log").open("w")
    command = [str(ROOT / ".venv/bin/python"), "-m", "streamlit", "run", str(ROOT / "scripts/ec_cold_app.py"),
               "--server.port", str(PORT), "--server.address", "127.0.0.1", "--server.headless", "true",
               "--server.fileWatcherType", "none", "--client.showSidebarNavigation", "false"]
    server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    manifest["pid"] = server.pid
    video = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError("Owned Streamlit process exited during startup")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/_stcore/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            raise TimeoutError("Streamlit health endpoint timed out")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not args.headed)
            context = await browser.new_context(viewport={"width": 1440, "height": 1000},
                                                record_video_dir=str(directory / "videos"),
                                                record_video_size={"width": 1440, "height": 1000})
            await context.add_init_script(SAMPLER)
            manifest["video_recording_started_at_estimate"] = time.time()
            page = await context.new_page()
            manifest["video_recording_ready_at"] = time.time()
            video = page.video
            console = []
            page.on("pageerror", lambda error: console.append({"type": "pageerror", "message": str(error)}))
            try:
                progress(f"cold baseline {keyword!r}: navigating")
                action_started = time.monotonic()
                action_wall = time.time()
                await page.goto(f"http://127.0.0.1:{PORT}/earnings_calls?ticker=ALL", wait_until="domcontentloaded", timeout=30000)
                await wait_complete(page, args.timeout, initial=True)
                manifest["action_timings"].append({"action": "initial_load", "started_at": action_wall,
                                                  "elapsed_ms": (time.monotonic() - action_started) * 1000})
                await page.screenshot(path=str(directory / "01-initial.png"))
                await page.wait_for_timeout(manifest["review_dwell_ms"])
                await flush_samples(page, directory / "timeline.jsonl")
                company = page.get_by_test_id("stSelectbox").filter(has_text=re.compile(r"^Company"))
                if "All Companies" not in await company.inner_text():
                    await mark(page, "select All Companies")
                    control = company.get_by_role("combobox")
                    await control.click()
                    await control.press("Home")
                    await control.press("Enter")
                    await wait_complete(page, args.timeout, initial=True)
                for label in ("Year", "Quarter"):
                    selectbox = page.get_by_test_id("stSelectbox").filter(has_text=re.compile(r"^" + label))
                    control = selectbox.get_by_role("combobox")
                    if not re.search(r"\bAll\b", await selectbox.inner_text(), re.I):
                        await mark(page, f"select {label}=All")
                        await control.click()
                        await control.press("Home")
                        await control.press("Enter")
                        await wait_complete(page, args.timeout, initial=True)
                await flush_samples(page, directory / "timeline.jsonl")
                action_started = time.monotonic()
                action_wall = time.time()
                await mark(page, f"search {keyword}")
                search = page.get_by_test_id("stTextInput").filter(has_text=re.compile(r"^Search")).get_by_role("textbox")
                await search.fill(keyword)
                await search.press("Enter")
                expected_empty = keyword in args.expect_empty
                cards = await wait_complete(page, args.timeout, empty=expected_empty)
                manifest["action_timings"].append({"action": "search", "started_at": action_wall,
                                                  "elapsed_ms": (time.monotonic() - action_started) * 1000,
                                                  "cards": len(cards)})
                manifest["expected_empty"] = expected_empty
                manifest["pages"] = [{"cards": len(cards), "keys": card_keys(cards)}]
                await page.screenshot(path=str(directory / "02-results.png"))
                await page.wait_for_timeout(manifest["review_dwell_ms"])
                for number in range(0 if expected_empty else args.show_more):
                    previous = card_keys(cards)
                    await flush_samples(page, directory / "timeline.jsonl")
                    action_started = time.monotonic()
                    action_wall = time.time()
                    await mark(page, f"Show more {number + 1}")
                    more = page.get_by_role("button", name="Show more ↓", exact=True)
                    await more.wait_for(state="visible", timeout=5000)
                    await more.click()
                    cards = await wait_complete(page, args.timeout, cards=len(previous) + 1)
                    keys = card_keys(cards)
                    if keys[:len(previous)] != previous:
                        raise AssertionError("Existing cards were lost or reordered after Show more")
                    if len(set(keys)) != len(keys):
                        raise AssertionError("Duplicate result cards after Show more")
                    manifest["action_timings"].append({"action": f"show_more_{number + 1}", "started_at": action_wall,
                                                      "elapsed_ms": (time.monotonic() - action_started) * 1000,
                                                      "cards": len(cards)})
                    manifest["pages"].append({"cards": len(cards), "keys": keys})
                    await page.screenshot(path=str(directory / f"03-more-{number + 1}.png"))
                    await page.wait_for_timeout(manifest["review_dwell_ms"])
                manifest["final_cards"] = cards
                if manifest["filter_flow_requested"] and not expected_empty:
                    cards = await verify_filter_flow(page, keyword, cards, manifest, directory, args)
                if args.view and not expected_empty:
                    await flush_samples(page, directory / "timeline.jsonl")
                    action_started = time.monotonic()
                    action_wall = time.time()
                    await mark(page, "View first transcript")
                    manifest["results_url"] = page.url
                    await page.locator(".transcript-search-result-card a").first.click()
                    await wait_complete(page, args.timeout, initial=True)
                    transcript = page.locator(".transcript-content").first
                    await transcript.wait_for(state="visible", timeout=5000)
                    if len((await transcript.inner_text()).strip()) < 100:
                        raise AssertionError("View did not display substantive transcript text")
                    manifest["action_timings"].append({"action": "view_first", "started_at": action_wall,
                                                      "elapsed_ms": (time.monotonic() - action_started) * 1000})
                    await page.screenshot(path=str(directory / "04-view.png"))
                    await page.wait_for_timeout(manifest["review_dwell_ms"])
                    manifest["view_url"] = page.url
                    if args.return_to_results:
                        await flush_samples(page, directory / "timeline.jsonl")
                        action_started = time.monotonic()
                        action_wall = time.time()
                        await mark(page, "return_to_results")
                        await page.go_back(wait_until="domcontentloaded")
                        await wait_complete(page, args.timeout, expected_keyword=keyword,
                            restored_pages=[manifest["pages"][0]["keys"], manifest["pages"][-1]["keys"]])
                        if page.url != manifest["results_url"]:
                            raise AssertionError("Browser Back did not restore the original results navigation")
                        manifest["action_timings"].append({"action": "return_to_results", "started_at": action_wall,
                                                          "elapsed_ms": (time.monotonic() - action_started) * 1000})
                        await page.screenshot(path=str(directory / "05-return.png"))
                        await page.wait_for_timeout(manifest["review_dwell_ms"])
                manifest["status"] = "passed"
            except asyncio.CancelledError:
                manifest["status"] = "interrupted"
                manifest["errors"].append("Audit interrupted by operator")
                (directory.parent / "STOP").touch()
            except Exception as exc:
                manifest["status"] = "failed"
                manifest["errors"].append(f"{type(exc).__name__}: {exc}")
                if "action_started" in locals():
                    manifest["action_timings"].append({"action": "failed_action", "started_at": action_wall,
                                                      "elapsed_ms": (time.monotonic() - action_started) * 1000})
                await page.screenshot(path=str(directory / "failure.png"))
                (directory / "failure-body.txt").write_text(await page.locator("body").inner_text())
                await page.wait_for_timeout(manifest["review_dwell_ms"])
            finally:
                await flush_samples(page, directory / "timeline.jsonl")
                (directory / "browser-errors.json").write_text(json.dumps(console, indent=2))
                if console:
                    manifest["errors"].append(f"Browser pageerrors: {len(console)}")
                await context.close()
                if video:
                    manifest["video"] = str(await video.path())
                await browser.close()
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        output.close()
    sql_path = directory / "sql.jsonl"
    sql = [json.loads(line) for line in sql_path.read_text().splitlines()] if sql_path.exists() else []
    manifest["sql_queries"] = sum(row["event"] == "start" for row in sql)
    manifest["sql_violations"] = [row for row in sql if row.get("blocked")]
    local_path = directory / "local-index.jsonl"
    local = [json.loads(line) for line in local_path.read_text().splitlines()] if local_path.exists() else []
    log_path = directory / "server-logs/server-log.log"
    log_text = log_path.read_text() if log_path.exists() else ""
    manifest["search_route_evidence"] = {
        "sqlite_word_index_calls": sum(row["event"] == "word_ids" and row.get("served") for row in local),
        "sqlite_word_index_declines": sum(row["event"] == "word_ids" and not row.get("served") for row in local),
        "sqlite_queries": sum(row["event"] == "query" for row in local),
        "origin_transcript_queries": sum(row["event"] == "start" and row.get("kind") in ("transcript_metadata", "transcript_text") for row in sql),
        "native_search_log_lines": log_text.count("TRANSCRIPT_NATIVE_") + log_text.count("table=mysql_native_batches"),
        "local_search_log_lines": log_text.count("table=local_word_index"),
        "private_snapshot_transcript_queries": sum(row["event"] == "start" and row.get("destination") == "private_native_snapshot" for row in sql),
        "private_snapshot_ngram_queries": sum(row["event"] == "start" and row.get("destination") == "private_native_snapshot" and "idx_transcript_ngram_search" in row.get("forced_indexes", []) for row in sql),
        "live_stg_support_queries": sum(row["event"] == "start" and row.get("destination") == "live_stg_readonly" for row in sql),
    }
    for action in manifest["action_timings"]:
        start, end = action["started_at"], action["started_at"] + action["elapsed_ms"] / 1000
        starts = [row for row in sql if row["event"] == "start" and start <= row["at"] <= end]
        counters = {row["counter"] for row in starts}
        action["sql"] = {
            "counters": sorted(counters), "SELECTs": sum(row["operation"] == "SELECT" for row in starts),
            "transcript_metadata_queries": sum(row.get("kind") == "transcript_metadata" for row in starts),
            "transcript_text_queries": sum(row.get("kind") == "transcript_text" for row in starts),
            "supporting_metadata_queries": sum(row.get("kind") == "supporting_metadata" for row in starts),
            "errors": sum(row["event"] == "error" and row["counter"] in counters for row in sql),
            "private_snapshot_queries": sum(row.get("destination") == "private_native_snapshot" for row in starts),
            "private_snapshot_ngram_queries": sum(row.get("destination") == "private_native_snapshot" and "idx_transcript_ngram_search" in row.get("forced_indexes", []) for row in starts),
            "live_stg_support_queries": sum(row.get("destination") == "live_stg_readonly" for row in starts),
            "catalogue_queries": sum(row.get("role") == "transcript_catalogue" for row in starts),
            "detail_queries": sum(row.get("role") == "transcript_detail" for row in starts),
            "search_queries": sum(str(row.get("role", "")).startswith("search_") for row in starts),
        }
        if args.verify_fragment_queries and (action["action"] == "search" or action["action"].startswith("show_more_") or action["action"] in {"new_keyword", "new_keyword_show_more", "restore_original_keyword"}):
            action["search_queries_only"] = not any(action["sql"][key] for key in ("supporting_metadata_queries", "catalogue_queries", "detail_queries"))
            if not action["search_queries_only"]:
                manifest["errors"].append(f"{action['action']} reloaded catalogue/support metadata or transcript detail")
        action["local_index"] = {
            "queries": sum(row["event"] == "query" and start <= row["started_at"] <= end for row in local),
            "word_index_served": sum(row["event"] == "word_ids" and row.get("served") and start <= row["started_at"] <= end for row in local),
            "word_index_declined": sum(row["event"] == "word_ids" and not row.get("served") and start <= row["started_at"] <= end for row in local),
        }
    if manifest["sql_violations"]:
        manifest["errors"].append("Remote SQL mutation attempt blocked")
    if mode == "isolated_native_snapshot" and not manifest["search_route_evidence"]["private_snapshot_ngram_queries"]:
        manifest["errors"].append("No private native ngram index query observed")
    manifest["timeline_analysis"] = analyze_timeline(directory / "timeline.jsonl")
    if manifest["timeline_analysis"]["blank_busy_without_loader"]:
        manifest["errors"].append("Blank busy interval without visible loader exceeded500ms")
    if manifest["timeline_analysis"]["framework_widget_binding_warnings"]:
        manifest["errors"].append("Visible framework default/session-state widget binding warning")
    if manifest.get("video"):
        try:
            manifest["video_analysis"] = analyze_video(Path(manifest["video"]), directory,
                manifest["action_timings"], manifest.get("video_recording_started_at_estimate"))
        except Exception as exc:
            manifest["errors"].append(f"Video analysis failed: {type(exc).__name__}: {exc}")
    manifest["source_hashes_end"] = source_hashes()
    manifest["source_changed_during_case"] = manifest["source_hashes_start"] != manifest["source_hashes_end"]
    if manifest["source_changed_during_case"]:
        manifest["errors"].append("Application or audit source changed during recording")
    if manifest["errors"]:
        manifest["status"] = "failed"
    manifest["functional_search_passed"] = (
        any(action["action"] == "search" and action.get("cards") == 0 for action in manifest["action_timings"])
        if manifest["expected_empty"] else len(manifest.get("pages", [])) == args.show_more + 1)
    requested = {"initial_load", "search"}
    search_actions = {"search"}
    if not manifest["expected_empty"]:
        search_actions.update(f"show_more_{i + 1}" for i in range(args.show_more))
        requested.update(search_actions)
        if args.view:
            requested.add("view_first")
            if args.return_to_results:
                requested.add("return_to_results")
    if manifest["filter_flow_requested"]:
        requested.update({"filter_company", "filter_year", "filter_quarter", "reset_company", "filter_watchlist", "reset_watchlist", "new_keyword", "new_keyword_show_more", "restore_original_keyword"})
        search_actions.update({"new_keyword", "new_keyword_show_more", "restore_original_keyword"})
    completed = {action["action"] for action in manifest["action_timings"]}
    manifest["requested_actions"] = sorted(requested)
    manifest["functional_passed"] = manifest["status"] == "passed" and requested <= completed and manifest["functional_search_passed"]
    if not manifest["functional_passed"] and manifest["status"] == "passed":
        manifest["errors"].append("Requested workflow did not complete: " + ", ".join(sorted(requested - completed)))
        manifest["status"] = "failed"
    manifest["performance_budget_ms"] = 5000
    manifest["performance_passed"] = bool(manifest["action_timings"]) and all(
        action["elapsed_ms"] <= manifest["performance_budget_ms"] for action in manifest["action_timings"])
    search_timings = [action["elapsed_ms"] for action in manifest["action_timings"] if action["action"] in search_actions]
    manifest["search_pagination_max_latency_ms"] = max(search_timings, default=None)
    manifest["search_pagination_performance_passed"] = search_actions <= completed and all(
        latency <= manifest["performance_budget_ms"] for latency in search_timings)
    manifest["case_audit_final_state"] = {"watermark": ACTION_SQL_WATERMARK, "requires_search_query": ACTION_REQUIRES_SEARCH_QUERY}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    progress(f"{keyword!r}: {manifest['status']}; manifest={directory / 'manifest.json'}")
    return manifest


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", nargs="+", default=["exchange", "print", "revenue", "foreign exchange", "printenv"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--show-more", type=int, default=2)
    parser.add_argument("--expect-empty", nargs="*", default=["printenv"])
    parser.add_argument("--view", action="store_true")
    parser.add_argument("--return-to-results", action="store_true")
    parser.add_argument("--verify-filter-flow", action="store_true")
    parser.add_argument("--verify-fragment-queries", action="store_true")
    parser.add_argument("--local-index", action="store_true")
    parser.add_argument("--local-index-file", type=Path, default=ROOT / "server-logs/agg-cache/news_mirror.sqlite")
    parser.add_argument("--isolated-native-socket", type=Path)
    parser.add_argument("--isolated-native-db", default="ec_ngram_corpus_probe")
    parser.add_argument("--isolated-native-user", default="root")
    parser.add_argument("--isolated-native-manifest", type=Path)
    parser.add_argument("--isolated-native-restart", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if args.local_index and args.isolated_native_socket:
        raise SystemExit("Choose either durable SQLite index or isolated native snapshot mode")
    if args.isolated_native_restart and not args.isolated_native_socket:
        raise SystemExit("Private MySQL restart requires --isolated-native-socket")
    if args.verify_filter_flow and args.keywords[0] in args.expect_empty:
        raise SystemExit("Filter-flow verification requires a known positive first keyword")
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            check.bind(("127.0.0.1", PORT))
        except OSError:
            raise SystemExit("Port8513 is already occupied; no process was stopped")
    output = (args.output or ROOT / "output/playwright" / f"ec-cold-{datetime.now().strftime('%Y%m%d-%H%M%S')}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    args.frozen_source_hashes = source_hashes()
    cases = []
    for index, keyword in enumerate(args.keywords, 1):
        slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-") or "keyword"
        directory = output / f"{index:02d}-{slug}"
        try:
            result = await record_case(keyword, directory, args)
        except Exception as exc:
            result = {"keyword": keyword, "status": "failed", "directory": str(directory),
                      "errors": [f"Preflight failed: {type(exc).__name__}: {exc}"],
                      "action_timings": [], "functional_search_passed": False, "performance_passed": False}
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "manifest.json").write_text(json.dumps(result, indent=2))
            (output / "STOP").touch()
            progress(result["errors"][0])
        cases.append(result)
        if (output / "STOP").exists():
            progress("STOP file present; no next browser or server will be started")
            break
    (output / "manifest.json").write_text(json.dumps({"source_hashes": args.frozen_source_hashes,
        "cases": cases, "passed": all(c["status"] == "passed" and c.get("functional_passed") for c in cases)}, indent=2))
    progress(f"SUITE {output / 'manifest.json'}")
    return 0 if all(c["status"] == "passed" and c.get("functional_passed") for c in cases) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
