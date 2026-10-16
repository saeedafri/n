"""
Form 3 Appointments
===================
New officers and directors from SEC Form 3 ("Initial Statement of Beneficial
Ownership"), read with edgartools (free, already a dependency).

Every person who becomes a Section 16 insider — a director, or an officer the
board designates (CEO, CFO, CAO, GC, division presidents, ...) — must file a
Form 3 within 10 days. Its XML carries the name, the director / officer flags,
the officer title and the date they became an insider (periodOfReport), so no
regex is needed. It catches appointments no 8-K Item 5.02 reports: Item 5.02(c)
covers only the CEO, President, CFO, CAO and COO.

Where it comes from:
  * which Form 3s exist: the SEC quarterly form index, via edgartools
    get_filings(form="3") — one request per quarter, every filer in it;
  * whose they are: issuer CIK -> our ticker, via edgartools get_cik_tickers();
  * the content: each filing's .txt from sec.gov/Archives, read only up to the
    end of the XML (see parse_form3 for why not edgartools' Form3 parser).

Like proxy_pay it is NEVER built on a request: a background refresh reads only
filings not read before and saves its progress; readers get whatever is on disk.
No DB writes. The copy lives in the materialize cache dir (/home/mdp-cache on STG).
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import pandas as pd

from core.database import db_manager
from data.proxy_pay import SEC_USER_AGENT

try:
    from utils.server_logger import log_structured_error, log_timing
except Exception:  # pragma: no cover
    def log_structured_error(exc, **kwargs): pass
    def log_timing(*a, **kw): pass

FRAME_NAME = "form3_appointments_v1"
# ponytail: fixed start (~5.5k filings a year for our issuers, ~0.15 s each on
# the first run, newest first); move it back if older appointments are wanted.
FORM3_SINCE = "2025-01-01"
REFRESH_SECONDS = 6 * 3600

_CIK_QUERY = """
    SELECT ticker, cik FROM coreiq_companies
    WHERE cik IS NOT NULL AND cik <> '' AND ticker IS NOT NULL AND ticker <> ''
"""

ROW_COLUMNS = ["ticker", "person", "title", "is_officer", "is_director",
               "became_insider", "filing_date", "accession", "url"]

# "VP, CFO and Treasurer\nExhibit 24.1 - Power of Attorney",
# "President - Aerospace Group ... Issuer. Power of Attorney is attached hereto".
_TITLE_NOISE = re.compile(r"(?:[.;]\s*|\s*\n\s*|\s+-\s+)?(?:Exhibit\b|Power\s+of\s+Attorney|See\s+Remarks|Ex\.\s*24).*$",
                          re.I | re.S)


def clean_officer_title(title: str, remarks: str = "") -> str:
    """The officer title without the exhibit list filers paste into it; "See
    Remarks" reads the title from the remarks instead."""
    text = (title or "").strip()
    if re.fullmatch(r"see\s+remarks?\.?", text, re.I):
        text = (remarks or "").strip()
    text = _TITLE_NOISE.sub("", text).strip(" ,.-\n")
    text = re.sub(r"\s+", " ", text)
    if text.isupper():
        text = text.title()
    return text


def filing_url(cik, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.txt"


def _flag(node, tag: str) -> bool:
    return (node.findtext(tag) or "").strip().lower() in ("1", "true")


def parse_form3(xml: str, tickers_of: dict, filing_date, accession: str, url: str) -> List[dict]:
    """One row per person who became an officer or director of one of our
    companies (`tickers_of`: issuer CIK -> our tickers). 10%-only holders are
    not management.

    Read with the stdlib, not edgartools' Form3.parse_xml: that one looks up
    every owner on data.sec.gov (one more request each, cached to ~/.edgar) only
    to tell people from companies — and an officer or director is a person."""
    import xml.etree.ElementTree as ET

    from edgar.display.formatting import reverse_name

    root = ET.fromstring(xml)
    tickers = tickers_of.get(int(root.findtext("issuer/issuerCik") or 0))
    if not tickers:
        return []
    symbol = (root.findtext("issuer/issuerTradingSymbol") or "").strip().upper()
    ticker = symbol if symbol in tickers else sorted(tickers)[0]
    period = root.findtext("periodOfReport")
    rows = []
    for owner in root.findall("reportingOwner"):
        role = owner.find("reportingOwnerRelationship")
        if role is None:
            continue
        is_officer, is_director = _flag(role, "isOfficer"), _flag(role, "isDirector")
        if not (is_officer or is_director):
            continue
        title = clean_officer_title(role.findtext("officerTitle"), root.findtext("remarks")) if is_officer else ""
        rows.append({
            # SEC files names "Last First Middle"; reverse_name makes "First Middle Last".
            "ticker": ticker, "person": reverse_name((owner.findtext("reportingOwnerId/rptOwnerName") or "").strip()),
            "title": title, "is_officer": is_officer, "is_director": is_director,
            "became_insider": pd.Timestamp(period) if period else pd.NaT,
            "filing_date": pd.Timestamp(filing_date), "accession": accession, "url": url,
        })
    return rows


# =============================================================================
# FRAME — read from disk, refresh in background
# =============================================================================

def read_form3() -> pd.DataFrame:
    """The parsed rows on disk, or an empty frame. Never builds, never waits."""
    from utils.materialize import _disk_copy
    obj = _disk_copy(FRAME_NAME)
    if isinstance(obj, tuple) and len(obj) == 2:
        return obj[0]
    return pd.DataFrame(columns=ROW_COLUMNS)


def _wanted_form3():
    """Form 3 filings (amendments excluded) filed by or for one of our companies,
    newest first, and issuer CIK -> our tickers."""
    from edgar import get_filings, set_identity
    from edgar.reference.tickers import get_cik_tickers

    from data.screening_service import get_all_companies_universe

    set_identity(SEC_USER_AGENT)
    ours = set(get_all_companies_universe()["ticker"].astype(str))
    ciks = get_cik_tickers()
    ciks = ciks[ciks["ticker"].isin(ours)][["ticker", "cik"]]
    # coreiq_companies' own CIKs first: a company added today is there before it
    # reaches edgartools' bundled ticker list (a snapshot from its release).
    own = pd.DataFrame(db_manager.execute_query_readonly(_CIK_QUERY) or [], columns=["ticker", "cik"])
    ciks = pd.concat([own, ciks], ignore_index=True)
    ciks["cik"] = pd.to_numeric(ciks["cik"], errors="coerce")
    ciks = ciks.dropna(subset=["cik"])
    tickers_of = ciks.groupby(ciks["cik"].astype(int))["ticker"].agg(set).to_dict()
    index = get_filings(form="3", filing_date=f"{FORM3_SINCE}:", amendments=False).to_pandas()
    # The index lists a filing once per filer (issuer and each owner); the XML
    # names the issuer, so any one line will do.
    index = index[index["cik"].astype(int).isin(tickers_of)].drop_duplicates("accession_number")
    return index.sort_values("filing_date", ascending=False), tickers_of


def refresh_form3(limit: Optional[int] = None, save_every: int = 100, on_save=None) -> int:
    """Read Form 3 filings not yet on disk; return how many were read.

    A filing with no officer or director is remembered too, so it is not fetched again.
    `on_save` runs after each save (the reader's cache clear), so rows show as they land.
    """
    import requests
    from utils.materialize import _disk_copy, write_materialized

    session = requests.Session()
    session.headers["User-Agent"] = SEC_USER_AGENT
    t0 = time.perf_counter()
    obj = _disk_copy(FRAME_NAME)
    rows, done = (obj if isinstance(obj, tuple) and len(obj) == 2
                  else (pd.DataFrame(columns=ROW_COLUMNS), pd.DataFrame(columns=["accession"])))
    todo, tickers_of = _wanted_form3()
    todo = todo[~todo["accession_number"].isin(set(done["accession"]))]
    if limit:
        todo = todo.head(limit)
    if todo.empty:
        return 0

    def fetch(item):
        url = filing_url(item.cik, item.accession_number)
        try:
            # The XML is the first document; exhibits after it run to ~700 KB.
            with session.get(url, timeout=60, stream=True) as response:
                response.raise_for_status()
                text = b""
                for chunk in response.iter_content(16384):
                    text += chunk
                    if b"</ownershipDocument>" in text:
                        break
            text = text.decode("utf-8", "replace")
            return item, url, text[text.find("<ownershipDocument"):text.find("</ownershipDocument>") + 20]
        except Exception as exc:
            return item, url, exc

    new_rows, new_done, read = [], [], 0
    # Four in flight stays far below the SEC's 10 requests/s fair-access limit.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for item, url, xml in pool.map(fetch, todo.itertuples()):
            if isinstance(xml, Exception):
                # Left out of `done`: a network failure is retried on the next run.
                log_structured_error(xml, page="form3_appointments", component="refresh_form3",
                                     operation="read_form3", context=url)
                continue
            try:
                new_rows += parse_form3(xml, tickers_of, item.filing_date, item.accession_number, url)
            except Exception as exc:
                # Same bytes, same failure: recorded as done, never refetched.
                log_structured_error(exc, page="form3_appointments", component="refresh_form3",
                                     operation="parse_form3", context=url)
            new_done.append(item.accession_number)
            read += 1
            if read % save_every == 0:
                rows = pd.concat([rows, pd.DataFrame(new_rows, columns=ROW_COLUMNS)], ignore_index=True)
                done = pd.concat([done, pd.DataFrame({"accession": new_done})], ignore_index=True)
                new_rows, new_done = [], []
                write_materialized(FRAME_NAME, (rows, done), [])
                if on_save:
                    on_save()
    if new_done:
        rows = pd.concat([rows, pd.DataFrame(new_rows, columns=ROW_COLUMNS)], ignore_index=True)
        done = pd.concat([done, pd.DataFrame({"accession": new_done})], ignore_index=True)
        write_materialized(FRAME_NAME, (rows, done), [])
        if on_save:
            on_save()
    log_timing("FORM3_REFRESH", (time.perf_counter() - t0) * 1000,
               details=f"read={read} rows={len(rows)} filings={len(done)}", level="WARNING")
    return read
