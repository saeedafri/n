"""Test the rewritten LLM prompt on a small sample before spending anything real.

Runs the model over filings whose correct answer we already know — either
verified against a public source, or produced by the filing parser and
corroborated. Reports how often the model agrees, and how often it correctly
declines rather than offering a subset.

    .venv/bin/python scripts/test_llm_prompt.py 50 0.05

Cost is capped by the second argument and printed exactly.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.getcwd(), "app"))
for _line in open(".env"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from azure.storage.blob import BlobServiceClient
from openai import OpenAI

from data.store_count_extractor import filing_to_text, fiscal_year_of
from data.store_count_sources import ask_llm, llm_call_cost, llm_windows

SAMPLE = int(sys.argv[1]) if len(sys.argv) > 1 else 50
CEILING = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05

# Verified by hand against company filings / IR pages earlier in this work.
KNOWN = [
    ("AZO", "2019", 6411), ("AZO", "2020", 6549), ("BBY", "2021", 1159),
    ("WMT", "2020", 11501), ("WMT", "2023", 10623), ("MCD", "2025", 45356),
    ("COST", "2025", 914), ("DG", "2025", 20662), ("TGT", "2021", 1897),
    ("PLCE", "2020", 924), ("ORLY", "2019", 5460), ("TSCO", "2019", 2024),
    ("SHAK", "2019", 275), ("ULTA", "2026", 1591), ("TXRH", "2024", 784),
    ("FND", "2021", 160), ("CVS", "2019", 9941), ("LOW", "2025", 1748),
    ("ROST", "2022", 1923), ("TJX", "2026", 5214), ("HD", "2026", 2359),
    ("KR", "2026", 2697), ("WEN", "2025", 7397), ("CVS", "2025", 8979),
    ("BURL", "2026", 1212), ("DLTR", "2024", 16774), ("CMG", "2025", 4056),
    ("DRI", "2026", 2373), ("NKE", "2026", 988), ("LEVI", "2025", 1231),
    ("MNRO", "2026", 1115), ("MUSA", "2025", 1800), ("OLLI", "2026", 645),
    ("CASY", "2026", 2944), ("CATO", "2026", 1069), ("EYE", "2026", 1250),
    ("FIVE", "2026", 1921), ("DXLG", "2025", 288), ("CWH", "2025", 196),
    ("AAP", "2016", 5189), ("ANF", "2016", 932), ("AEO", "2016", 949),
    ("ACI", "2021", 2277), ("PLCE", "2024", 523), ("SHAK", "2025", 373),
    ("AZO", "2022", 6943), ("ORLY", "2020", 5616), ("TGT", "2020", 1868),
    ("LOW", "2026", 1759), ("ULTA", "2025", 1445),
][:SAMPLE]

service = BlobServiceClient(
    f"https://{os.environ['AZURE_STORAGE_ACCOUNT_NAME']}.blob.core.windows.net",
    credential=os.environ["AZURE_STORAGE_ACCOUNT_KEY"])
container = service.get_container_client(os.environ["AZURE_BLOB_CONTAINER"])
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

spent = [0.0]
lock = threading.Lock()


def run(case):
    ticker, folder, truth = case
    with lock:
        if spent[0] >= CEILING:
            return (ticker, folder, truth, None, "budget", 0.0)
    try:
        raw = container.get_blob_client(
            f"{ticker}/{folder}/10-K/filing.html").download_blob().readall()
        text = filing_to_text(raw.decode("utf-8", "ignore"))
    except Exception:
        return (ticker, folder, truth, None, "blob error", 0.0)
    stated = fiscal_year_of(text)
    year = stated[0] if stated else int(folder)
    parsed, prompt_tokens, completion_tokens = ask_llm(
        client, ticker, year, stated[1] if stated else "", llm_windows(text))
    cost = llm_call_cost(prompt_tokens, completion_tokens)
    with lock:
        spent[0] += cost
    if not parsed:
        return (ticker, year, truth, None, "unparseable", cost)
    scope = parsed.get("scope")
    if not parsed.get("found") or not parsed.get("value"):
        return (ticker, year, truth, None, "declined", cost)
    if scope not in (None, "worldwide"):
        return (ticker, year, truth, int(parsed["value"]), f"refused:{scope}", cost)
    return (ticker, year, truth, int(parsed["value"]), "worldwide", cost)


def main() -> None:
    print(f"testing the rewritten prompt on {len(KNOWN)} filings, "
          f"ceiling ${CEILING:.2f}\n")
    results = list(ThreadPoolExecutor(max_workers=8).map(run, KNOWN))

    exact = [r for r in results if r[3] == r[2]]
    wrong = [r for r in results if r[3] is not None and r[3] != r[2]
             and not r[4].startswith("refused")]
    refused = [r for r in results if r[4].startswith("refused")]
    declined = [r for r in results if r[4] == "declined"]

    for ticker, year, truth, got, note, _ in results:
        mark = "OK " if got == truth else "x  "
        print(f"  {mark}{ticker:5} FY{year}  truth={truth:>7,}  "
              f"model={str(got):>8}  [{note}]")

    total = len(results)
    print(f"\nexact match          : {len(exact)}/{total} "
          f"({len(exact) / max(total, 1) * 100:.0f}%)")
    print(f"wrong number offered : {len(wrong)}")
    print(f"refused (subset)     : {len(refused)}   <- correctly withheld")
    print(f"declined (not found) : {len(declined)}")
    print(f"\nSPENT ${spent[0]:.4f} of ${CEILING:.2f}")
    json.dump([list(r) for r in results],
              open("/tmp/sc_report/llm_prompt_test.json", "w"), indent=1)


if __name__ == "__main__":
    main()
