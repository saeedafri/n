"""
People Quality
==============
Checks the four People datasets (People Screening pay, Management Changes,
Executive Moves, Layoffs) for every error class the data team has reported,
plus general sanity checks that catch errors of a kind nobody has seen yet.

Runs by itself: cache_manager's People thread calls `check_people_quality()`
after every update (new proxies, new Form 3s, a data rebuild) and at least
every 6 hours. Each run:
  * logs a WARNING for every must-be-zero check that is not zero,
  * logs a WARNING for every item NEWLY flagged by any check since the last run
    (so a new kind of error shows up the day it appears, not when the data team
    finds it), and for a dataset that shrank by more than 10%,
  * saves the full result to people_quality_report.json in the cache dir.
`scripts/people_quality_sweep.py` prints the same checks on demand.

Read-only: SELECTs and the materialized frames only. Pure pandas in
`run_checks` (no DB, no st.*), so it is testable with small frames.
See docs/superpowers/specs/2026-11-13-people-exports-data-review-design.md.
"""

import json
import os
import re
import time
from typing import Dict, List, Optional

import pandas as pd

try:
    from utils.server_logger import log_structured_error, log_timing, log_warning
except ImportError:  # pragma: no cover
    def log_structured_error(exc, **kwargs): pass
    def log_timing(*a, **kw): pass
    def log_warning(*a, **kw): pass

REPORT_NAME = "people_quality_report.json"
# A dataset that loses more than this share of its rows between two runs is a
# broken build or ingestion, not a real change.
SHRINK_LIMIT = 0.10
# A name: capitalised words (with the particles real names carry), no digits.
_PERSON_NAME = re.compile(
    r"^(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]*|de|da|del|della|der|di|du|la|le|van|von|bin|al|y)"
    r"(?:,?\s+(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.\-]*|de|da|del|della|der|di|du|la|le|van|von|bin|al|y|[A-Z]\.?)){1,5}$")


def _check(name: str, must_be_zero: bool, items) -> dict:
    return {"name": name, "must_be_zero": must_be_zero, "items": [str(i) for i in items]}


def run_checks(people: pd.DataFrame, changes: pd.DataFrame, layoffs: pd.DataFrame,
               company_names: Dict[str, str], filings: Optional[pd.DataFrame] = None,
               proxies_without_rows: Optional[List[str]] = None) -> List[dict]:
    """Every check, as {name, must_be_zero, items}. `filings` = proxy_pay rows
    in the people schema (our own read of the proxies)."""
    from data.layoff_events import _NOT_A_CUT
    from data.management_changes import _NOT_NAME
    from data.people_service import (_CORP_WORD, _STYLED_CAPS, TICKER_ALIASES, _names_in_title,
                                     canonical_ticker, company_name_key, person_key)

    out = []
    filings = filings if filings is not None else pd.DataFrame(
        columns=["ticker", "executive_name", "year", "total_compensation"])

    # ---- People Screening: the reported error classes --------------------------
    out.append(_check("People: title holds another executive's name", True,
                      [f"{t} {n}: {title[:90]}" for t, n, title, o in
                       zip(people["ticker"].astype(str), people["executive_name"], people["title"],
                           _names_in_title(people)) if o]))
    junk = people["title"].astype(str).str.contains(
        r"\b(?:Age\s*:|Director\s+since|Committees?\s*:)|(?:\s(?:19|20)\d\d)+\s*$|\*|"
        r"Aggregate\s+grant|^the\s+Company", case=False, regex=True)
    out.append(_check("People: title carries footnote / bio / year junk", True,
                      [f"{t} {n}: {x[:90]}" for t, n, x in people.loc[junk, ["ticker", "executive_name", "title"]].values]))
    out.append(_check("People: name starts with Mr./Ms./Dr.", True,
                      people.loc[people["executive_name"].str.match(r"(?:Mr|Ms|Mrs|Dr)\.?\s"), "executive_name"]))

    # Copied pay: colleagues with the same total in 2+ of the same years, checked
    # against the filing. Equal in the filing = genuine (e.l.f., Amazon).
    sec = people[people["source"].astype(str).str.contains("SEC") & people["total_compensation"].notna()]
    shared: Dict[tuple, set] = {}
    for (t, y, _), grp in sec.groupby([sec["ticker"].astype(str), "year", sec["total_compensation"].round(0)],
                                      observed=True):
        names = sorted(set(grp["executive_name"]))
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                shared.setdefault((t, a, b), set()).add(int(y))
    filed: Dict[tuple, set] = {}
    for t, n, y, v in zip(filings["ticker"].astype(str), filings["executive_name"], filings["year"],
                          filings["total_compensation"]):
        if pd.notna(v) and pd.notna(y):
            filed.setdefault((canonical_ticker(t), person_key(n).split("|")[-1], int(y)), set()).add(round(v))
    copied, genuine = [], []
    for (t, a, b), years in shared.items():
        if len(years) < 2:
            continue
        verdicts = []
        for y in years:
            fa, fb = filed.get((t, person_key(a).split("|")[-1], y)), filed.get((t, person_key(b).split("|")[-1], y))
            verdicts.append(None if not (fa and fb) else bool(fa & fb))
        (copied if False in verdicts else genuine).append(f"{t} {a} / {b}: {sorted(years)}")
    out.append(_check("People: copied pay (filing shows different figures)", True, copied))
    out.append(_check("People: equal pay the filings confirm or do not cover (check)", False, genuine))

    # ---- People Screening: general sanity (new kinds of error) -----------------
    parts = people[["salary", "bonus", "stock_awards", "option_awards", "non_equity_incentive",
                    "all_other_compensation"]].apply(pd.to_numeric, errors="coerce")
    total = pd.to_numeric(people["total_compensation"], errors="coerce")
    over = total.notna() & ((parts.sum(axis=1) > total * 1.05 + 1000) | (parts.max(axis=1) > total + 1000))
    out.append(_check("People: pay parts do not add up to the total", True,
                      [f"{t} {n} {y}" for t, n, y in people.loc[over, ["ticker", "executive_name", "year"]].values]))
    pay = total.fillna(pd.to_numeric(people["total_pay"], errors="coerce"))
    # Items name the person-year only, never the amount: a Yahoo figure re-converted
    # at a new FX rate must not read as a new flag.
    out.append(_check("People: pay over $500 million (check)", False,
                      [f"{t} {n} {y}" for t, n, y in people.loc[pay > 5e8, ["ticker", "executive_name", "year"]].values]))
    out.append(_check("People: pay under $50,000 (check: $1 CEOs and part years are genuine)", False,
                      [f"{t} {n} {y}" for t, n, y in people.loc[pay < 50_000, ["ticker", "executive_name", "year"]].values]))
    seq = people.assign(_p=pay, _y=pd.to_numeric(people["year"], errors="coerce")).dropna(subset=["_p", "_y"]) \
                .sort_values(["ticker", "executive_name", "_y"])
    prev = seq.groupby([seq["ticker"].astype(str), "executive_name"], observed=True)["_p"].shift()
    jump = (prev > 100_000) & (seq["_p"] > 100_000) & ((seq["_p"] / prev > 20) | (prev / seq["_p"] > 20))
    out.append(_check("People: pay moves more than 20x from one year to the next (check)", False,
                      [f"{t} {n} {int(y)}" for t, n, y in seq.loc[jump, ["ticker", "executive_name", "_y"]].values]))
    out.append(_check("People: name does not look like a person's name (check)", False,
                      sorted(set(people.loc[~people["executive_name"].astype(str).str.match(_PERSON_NAME),
                                            "executive_name"].astype(str)))))
    words = people["title"].astype(str).str.split().str.len()
    sentence = (words > 18) | people["title"].astype(str).str.contains(r"\.\s+[A-Z][a-z]+\s+[a-z]+\s+[a-z]+", regex=True)
    out.append(_check("People: title reads like a sentence or note (check)", False,
                      [f"{t} {n}: {x[:90]}" for t, n, x in people.loc[sentence, ["ticker", "executive_name", "title"]].values]))
    # Table vs filing per company: a company whose stored pay disagrees with its
    # own proxy on more than a quarter of person-years has an extraction fault.
    table = sec[~sec["filing_blob"].astype(str).str.startswith("http")]
    agree, seen = {}, {}
    for t, n, y, v in zip(table["ticker"].astype(str), table["executive_name"], table["year"],
                          table["total_compensation"]):
        got = filed.get((t, person_key(n).split("|")[-1], int(y)))
        if got:
            seen[t] = seen.get(t, 0) + 1
            agree[t] = agree.get(t, 0) + any(abs(v - g) <= 1000 for g in got)
    out.append(_check("People: company where stored pay and the proxy disagree on over 25% of person-years", False,
                      [f"{t}: {seen[t] - agree[t]} of {seen[t]} differ" for t in sorted(seen)
                       if seen[t] >= 4 and (seen[t] - agree[t]) / seen[t] > 0.25]))
    if proxies_without_rows is not None:
        out.append(_check("Proxies read with no pay table found (check parser)", False, proxies_without_rows))

    # ---- Companies and tickers ---------------------------------------------------
    names = pd.Series(company_names, dtype=object)
    out.append(_check("Companies: brand list as company name", True,
                      [f"{t}: {n}" for t, n in names[names.str.contains(",") & ~names.str.contains(_CORP_WORD)].items()]))
    caps = names[names.str.contains(" ") & names.map(
        lambda s: any(w not in _STYLED_CAPS for w in re.findall(r"\b[A-Z]{5,}\b", s))) & names.map(
        lambda s: sum(c.isupper() for c in s) > 0.8 * max(1, sum(c.isalpha() for c in s)))]
    out.append(_check("Companies: ALL CAPS name", True, [f"{t}: {n}" for t, n in caps.items()]))
    used = set(people["ticker"].astype(str)) | set(changes["ticker"].astype(str)) | set(layoffs["ticker"].astype(str))
    out.append(_check("All files: a folded ticker still used", True, sorted(used & set(TICKER_ALIASES))))
    # Second tickers with no SEC CIK to prove it (like 6288 / 9983): same name.
    by_name: Dict[str, set] = {}
    for t in used:
        key = company_name_key(company_names.get(t, ""))
        if len(key) >= 4:
            by_name.setdefault(key, set()).add(t)
    out.append(_check("Companies: two tickers with the same company name (check: add to ticker_aliases.json)", False,
                      [f"{k}: {', '.join(sorted(v))}" for k, v in sorted(by_name.items()) if len(v) > 1]))

    # ---- Management Changes -------------------------------------------------------
    ev = changes[changes["change"].isin(["Appointment", "Departure"])]
    tokens = ev["person"].fillna("").str.lower().str.split()
    out.append(_check("Management Changes: non-person listed as a person", True,
                      ev.loc[tokens.map(lambda w: bool(w) and all(x.strip(".,") in _NOT_NAME for x in w)), "person"].unique()))
    out.append(_check("Management Changes: possessive prefix in a name", True,
                      ev.loc[ev["person"].fillna("").str.contains(r"\w(?:'s|s')\s", regex=True), "person"]))
    short = r"\bChief(?:\s+(?!Officer\b|Executive\b)[A-Z][a-z]+){1,2}$"
    parts_t = ev["titles"].fillna("").str.split(r"\s\|\s")
    cut = parts_t.map(lambda ps: any(re.search(short, p) for p in ps)) & ~ev["title"].fillna("").str.contains(short, regex=True)
    out.append(_check("Management Changes: title cut short by the splitter", True, ev.loc[cut, "titles"]))
    designation = re.compile(r"^(?:the\s+)?principal\s+\w+\s+officer", re.I)
    mixed = parts_t.map(lambda ps: any(designation.match(p) for p in ps) and not all(designation.match(p) for p in ps))
    out.append(_check("Management Changes: SEC 'principal ... officer' beside a real title", True, ev.loc[mixed, "titles"]))
    out.append(_check("Management Changes: person name does not look like a person's name (check)", False,
                      sorted(set(ev.loc[~ev["person"].fillna("").str.match(_PERSON_NAME), "person"].astype(str)))))
    per_filing = ev[ev["event_id"].notna()].groupby("event_id")["person"].nunique()
    out.append(_check("Management Changes: one filing yields more than 8 people (check extractor)", False,
                      [f"event {int(e)}: {n} people" for e, n in per_filing[per_filing > 8].items()]))
    both = ev[ev["event_id"].notna()].groupby(["event_id", ev["person"].map(person_key)])["change"].nunique()
    out.append(_check("Management Changes: one filing appoints AND removes the same person (check)", False,
                      [f"event {int(e)} {p}" for e, p in both[both > 1].index]))

    # ---- Executive Moves ---------------------------------------------------------
    moves = changes[changes["moved_from"].astype(str) != ""].copy()
    for col in ("effective_date", "notice_date", "moved_from_date"):
        moves[col] = pd.to_datetime(moves[col], errors="coerce")
    out.append(_check("Executive Moves: left an interim title only", True,
                      moves.loc[moves["moved_from_title"].fillna("").str.contains(r"\b(?:Interim|Acting)\b"), "person"]))
    start = moves["effective_date"].fillna(moves["notice_date"])
    early = moves[moves["is_board"].astype(bool) & (moves["moved_from_date"] > start + pd.Timedelta(days=30))]
    out.append(_check("Executive Moves: board seat taken before leaving the old job", True, early["person"]))
    out.append(_check("Executive Moves: joined the new company before leaving the old one (check)", False,
                      moves.loc[moves["moved_from_date"] > start + pd.Timedelta(days=60), "person"]))
    gap = (start - moves["moved_from_date"]).dt.days / 30.44
    out.append(_check("Executive Moves: over 12 months between Left and Joined (flagged in export)", False,
                      [f"{p}: {g:.0f} months" for p, g in zip(moves["person"], gap) if g > 12]))

    # ---- Layoffs ------------------------------------------------------------------
    news = layoffs[~layoffs["source"].astype(str).str.startswith("SEC")]
    out.append(_check("Layoffs: commentary / law-firm / roundup headline", True,
                      news.loc[news["headline"].fillna("").str.contains(_NOT_A_CUT), "headline"]))
    multi = news.groupby("headline")["ticker"].nunique()
    out.append(_check("Layoffs: one story filed under two or more companies", True,
                      [f"{h[:90]} ({', '.join(sorted(set(news.loc[news['headline'] == h, 'ticker'].astype(str))))})"
                       for h in multi[multi > 1].index]))
    return out


def _report_path() -> Optional[str]:
    from utils.materialize import _cache_dir
    folder = _cache_dir()
    return os.path.join(folder, REPORT_NAME) if folder else None


def compare_runs(checks: List[dict], previous: Optional[dict], sizes: Dict[str, int]) -> dict:
    """What to warn about: must-be-zero checks that are not zero, items newly
    flagged since the previous run, and datasets that shrank by over 10%."""
    before = {c["name"]: set(c["items"]) for c in (previous or {}).get("checks", [])}
    failing = [c for c in checks if c["must_be_zero"] and c["items"]]
    new = {c["name"]: sorted(set(c["items"]) - before[c["name"]]) for c in checks
           if previous and c["name"] in before and set(c["items"]) - before[c["name"]]}
    shrunk = {k: (previous["sizes"][k], v) for k, v in sizes.items()
              if previous and previous.get("sizes", {}).get(k) and v < previous["sizes"][k] * (1 - SHRINK_LIMIT)}
    return {"failing": failing, "new": new, "shrunk": shrunk}


def load_inputs():
    """The live frames the checks read (materialized / cached — no rebuild
    unless the source tables changed)."""
    from data.management_changes import get_management_changes
    from data.people_service import (_FRAME_COLUMNS, _proxy_records, company_display_name, company_words,
                                     get_people_events, get_people_universe)
    from data.proxy_pay import FRAME_NAME, read_proxy_pay
    from data.repository import CompanyRepository
    from utils.materialize import _disk_copy

    people = get_people_universe()
    changes = get_management_changes()
    layoffs = get_people_events("layoffs_restructuring")
    company_names = {str(r["ticker"]): company_display_name(r.get("name_coresight") or "", r.get("name") or "",
                                                            ticker=str(r["ticker"]))
                     for r in CompanyRepository.get_companies_rows() or []}
    filings = pd.DataFrame(_proxy_records(read_proxy_pay(), company_words()), columns=_FRAME_COLUMNS)
    proxies_without_rows = None
    obj = _disk_copy(FRAME_NAME)
    if isinstance(obj, tuple) and len(obj) == 2 and len(obj[1]):
        read_ok = set(obj[0]["accession"]) if len(obj[0]) else set()
        proxies_without_rows = sorted(set(obj[1]["accession"]) - read_ok)
    return people, changes, layoffs, company_names, filings, proxies_without_rows


def check_people_quality(report_path: Optional[str] = None) -> dict:
    """Run every check on the live frames, warn, and save the report.
    Returns {"failing": n, "new": n, "shrunk": n}."""
    t0 = time.perf_counter()
    people, changes, layoffs, company_names, filings, proxies_without_rows = load_inputs()
    checks = run_checks(people, changes, layoffs, company_names, filings, proxies_without_rows)
    sizes = {"people": len(people), "management_changes": len(changes), "layoffs": len(layoffs),
             "proxy_filings": len(filings)}
    path = report_path or _report_path()
    previous = None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                previous = json.load(fh)
        except (OSError, ValueError):
            previous = None
    found = compare_runs(checks, previous, sizes)

    for c in found["failing"]:
        log_warning(f"[PEOPLE_QUALITY] FAIL {c['name']}: {len(c['items'])} — e.g. {c['items'][:3]}")
    for name, items in found["new"].items():
        log_warning(f"[PEOPLE_QUALITY] NEW {name}: +{len(items)} since last check — e.g. {items[:3]}")
    for name, (was, now) in found["shrunk"].items():
        log_warning(f"[PEOPLE_QUALITY] SHRUNK {name}: {was} -> {now} rows (over {SHRINK_LIMIT:.0%} fewer)")
    summary = {"failing": len(found["failing"]), "new": sum(len(v) for v in found["new"].values()),
               "shrunk": len(found["shrunk"])}
    log_warning(f"[PEOPLE_QUALITY] checked {len(checks)} checks in {time.perf_counter() - t0:.1f}s — "
                f"failing={summary['failing']} new_flags={summary['new']} shrunk={summary['shrunk']}")
    if path:
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "sizes": sizes, "summary": summary,
                           "checks": checks}, fh)
            os.replace(path + ".tmp", path)
        except OSError as exc:
            log_structured_error(exc, page="people_quality", component="check_people_quality",
                                 operation="write_report")
    return summary
