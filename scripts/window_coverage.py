"""Can the model possibly answer? Free check, no API call.

A model cannot report a number it was never shown. Before paying for another
prompt experiment, measure the ceiling: for filings whose correct answer we
know, does that number actually appear in the excerpts llm_windows picks?

Prints the ceiling for the old density-only builder and the new
parser-seeded one, so the change is judged on evidence rather than intent.

    .venv/bin/python scripts/window_coverage.py
"""

from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from azure.storage.blob import BlobServiceClient

from data.store_count_extractor import (
    UNIT_RE, choose_best, extract_from_filing, filing_to_text, fiscal_year_of,
    front_section)
from data.store_count_sources import _WINDOW_CHARS, llm_windows

from test_llm_prompt import KNOWN  # the same hand-verified list

service = BlobServiceClient(
    f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
container = service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])


def old_windows(text: str, limit: int = 3) -> str:
    """The density-only builder, kept here purely as the comparison baseline."""
    body = front_section(text)
    cue = re.compile(r"(?i)\b(total|as of|we operated|we had|number of|at year end)\b")
    unit = re.compile(rf"(?i)\b{UNIT_RE}\b")
    spans = []
    for match in unit.finditer(body):
        start, end = max(0, match.start() - 700), min(len(body), match.start() + 700)
        chunk = body[start:end]
        if cue.search(chunk) and re.search(r"\b\d[\d,]{1,6}\b", chunk):
            spans.append((start, end))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    merged.sort(key=lambda s: -len(unit.findall(body[s[0]:s[1]])))
    return "\n---\n".join(body[s:min(e, s + _WINDOW_CHARS)] for s, e in merged[:limit])


def shows(window: str, value: int) -> bool:
    return re.search(rf"\b{value:,}\b|\b{value}\b", window) is not None


def run(case):
    ticker, folder, truth = case
    try:
        raw = container.get_blob_client(
            f"{ticker}/{folder}/10-K/filing.html").download_blob().readall()
        text = filing_to_text(raw.decode("utf-8", "ignore"))
    except Exception as exc:
        return (ticker, folder, truth, None, None, 0, type(exc).__name__)
    # The blob folder year is NOT the fiscal year the filing reports on — they
    # differ for most retailers with a January year end. Comparing against the
    # folder year made correct extractions look like failures (O'Reilly's
    # 2019 folder holds FY2018, where 5,219 is right and 5,460 is not).
    stated = fiscal_year_of(text)
    best = choose_best(extract_from_filing(text))
    before, after = old_windows(text), llm_windows(text)
    return (ticker, folder, truth, shows(before, truth), shows(after, truth),
            len(after) // 4, "",
            stated[0] if stated else None, best.value if best else None)


def main() -> None:
    results = list(ThreadPoolExecutor(max_workers=8).map(run, KNOWN))
    for ticker, folder, truth, before, after, tokens, error, stated, parsed in results:
        if error:
            print(f"  ?? {ticker:5} {folder}  [{error}]")
            continue
        mark = {(True, True): "both", (False, True): "FIXED",
                (True, False): "LOST", (False, False): "still missing"}[(bool(before), bool(after))]
        note = "" if truth == parsed else f"  <- parser says {parsed}, FY{stated}"
        print(f"  {ticker:5} dir{folder}  truth={truth:>7,}  {mark:<13} "
              f"~{tokens} tok{note}")

    ok = [r for r in results if not r[6]]
    print(f"\ntruth visible to the model — before : "
          f"{sum(1 for r in ok if r[3])}/{len(ok)}")
    print(f"truth visible to the model — after  : "
          f"{sum(1 for r in ok if r[4])}/{len(ok)}")
    print(f"newly visible: {sum(1 for r in ok if r[4] and not r[3])}   "
          f"lost: {sum(1 for r in ok if r[3] and not r[4])}")
    print(f"mean prompt size: ~{sum(r[5] for r in ok) // max(len(ok), 1)} tokens")


if __name__ == "__main__":
    main()
