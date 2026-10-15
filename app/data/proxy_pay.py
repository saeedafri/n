"""
Proxy Pay
=========
Per-executive pay read from the Summary Compensation Table of each company's
DEF 14A proxy, with edgartools (free, already a dependency).

Why not XBRL: the SEC requires XBRL tags only on the Pay-versus-Performance table
(Item 402(v)) — the CEO's total, the CEO's "compensation actually paid" and the
AVERAGE of the other named executives. The Summary Compensation Table (salary,
bonus, stock, options, incentive, other, total per executive) is plain HTML, so
edgartools' ProxyStatement returns only those headline numbers. The table itself
is read here with edgartools' HTML parser.

coreiq_executives_compensation (the data team's extraction) covers 103 companies;
proxies exist for ~411. This frame fills the companies and years that table lacks
(see people_service.add_proxy_pay). It is NEVER built on a request: a background
refresh reads new proxies only and saves its progress, and readers get whatever is
on disk (empty until the first refresh has run).

No DB writes. The copy lives in the materialize cache dir (/home/mdp-cache on STG).
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import pandas as pd

from core.database import db_manager

try:
    from utils.server_logger import log_structured_error, log_timing, log_warning
except Exception:  # pragma: no cover
    def log_structured_error(exc, **kwargs): pass
    def log_timing(*a, **kw): pass
    def log_warning(*a, **kw): pass

# _v2: name/title split and shared-year-cell rows (2026-10-15).
FRAME_NAME = "proxy_pay_v2"
SEC_USER_AGENT = "Coresight Research mohdsaeedafri@coresight.com"

# Each proxy restates three fiscal years, so the newest three proxies give five.
PROXIES_PER_COMPANY = 3

_PROXY_QUERY = """
    SELECT ticker, event_date, source_ref
    FROM coreiq_company_events
    WHERE event_category = 'Proxy/Governance'
      AND event_subtype = 'Executive Compensation (DEF 14A)'
"""

# Header text -> frame column. Order matters: "Non-Equity Incentive" before
# anything else that says "compensation", "All Other" before "Total".
_COLUMN_PATTERNS = [
    ("year",                   r"\byear\b|^\W*fy\b"),
    ("salary",                 r"salary"),
    ("bonus",                  r"\bbonus"),
    ("stock_awards",           r"stock\s*awards?"),
    ("option_awards",          r"option\s*awards?"),
    ("non_equity_incentive",   r"non[\s\-‑–]*equity"),
    ("pension",                r"pension|deferred"),
    ("all_other_compensation", r"all\s*other"),
    ("total_compensation",     r"\btotal\b"),
]


def proxy_url(source_ref: str) -> str:
    """The plain document URL of an inline-XBRL viewer link."""
    ref = (source_ref or "").strip()
    if "ix?doc=" in ref:
        ref = "https://www.sec.gov" + ref.split("ix?doc=", 1)[1]
    return ref


def accession_from_url(url: str) -> str:
    m = re.search(r"/Archives/edgar/data/\d+/(\d{18})/", url or "")
    return m.group(1) if m else ""


def _amount(value) -> Optional[float]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"\(\d+\)|\[\d+\]", "", str(value))   # footnote markers
    text = text.replace("$", "").replace(",", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").strip()
    if not text or text in {"-", "—", "–", "‑"}:
        return 0.0 if text else None
    try:
        return -float(text) if negative else float(text)
    except ValueError:
        return None


def _year(value) -> Optional[int]:
    m = re.fullmatch(r"\s*(?:FY\s*|Fiscal\s*)?(19[89]\d|20\d\d)\s*(?:\(\w{1,3}\)|\*)?\s*", str(value), re.I)
    return int(m.group(1)) if m else None


def _metric_of(header: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", header)
    for name, pattern in _COLUMN_PATTERNS:
        if re.search(pattern, text, re.I):
            return name
    return None


def _grid(table) -> List[List[tuple]]:
    """edgartools' header + body cells laid out on a grid, spans expanded.

    grid[r][c] = (cell id, text). A spanned cell has the same id in every
    position it covers, which is how a value under a three-column "Salary"
    header is found whichever of the three columns it sits in.
    """
    rows = [list(h) for h in (table.headers or [])] + [list(r.cells) for r in table.rows]
    taken: dict = {}
    grid: List[List[tuple]] = []
    for r, cells in enumerate(rows):
        c = 0
        for cell in cells:
            while (r, c) in taken:
                c += 1
            text = (cell.text() or "").strip()
            for dr in range(max(1, cell.rowspan or 1)):
                for dc in range(max(1, cell.colspan or 1)):
                    taken[(r + dr, c + dc)] = (id(cell), text)
            c += max(1, cell.colspan or 1)
    for r in range(len(rows)):
        width = max([c for (rr, c) in taken if rr == r], default=-1) + 1
        grid.append([taken.get((r, c), (None, "")) for c in range(width)])
    return grid


def _explode(grid) -> List[List[tuple]]:
    """Split a row that stacks several years in one cell ("2025\n\n2024\n\n2023",
    every money cell stacked the same way) into one row per year."""
    out = []
    for row in grid:
        parts = [re.split(r"\n\s*\n", text) for _, text in row]
        stacked = next((p for p in parts if len(p) > 1 and all(_year(x) for x in p)), None)
        if not stacked:
            out.append(row)
            continue
        n = len(stacked)
        for k in range(n):
            out.append([((cell_id, k), p[k]) if len(p) == n else (cell_id, text)
                        for (cell_id, text), p in zip(row, parts)])
    return out


# Words that occur in titles and never in a person's name.
_TITLE_WORDS = re.compile(
    r"\b(?:chief|officer|president|director|vice|executive|chair\w*|financial|former|"
    r"senior|general|counsel|operating|treasurer|secretary|principal|interim|head|founder|"
    r"ceo|cfo|coo|cto|cio|evp|svp|board)\b", re.I)


def _split_name(line: str):
    """("Paul Paradis", "Executive Director") from "Paul Paradis, Executive Director".

    Comma parts are name parts until one reads like a title ("James A. Sabia, Jr."
    keeps its suffix). A parenthesis holding a title ends the name too
    ("Steve Klohn (Chief Information Officer)"); a nickname in one does not.
    """
    paren = line.find("(")
    if paren > 0 and _TITLE_WORDS.search(line[paren:]):
        return line[:paren].strip(" ,"), line[paren:].strip(" ()")
    parts = [p.strip() for p in line.split(",")]
    for k, part in enumerate(parts):
        if _TITLE_WORDS.search(part):
            return ", ".join(parts[:k]).strip(" ,"), ", ".join(parts[k:])
    return line, ""


def _sct_rows(grid) -> List[dict]:
    """Executive-year rows of one candidate table, [] when it is not the SCT."""
    from data.people_service import clean_person_name, is_person_name

    def year_at(row):
        # The first filled cell is the name; a year there is a stray header row.
        label_id = next((i for i, text in row if text), None)
        for c, (cell_id, text) in enumerate(row):
            if cell_id != label_id and _year(text):
                return c
        return None

    grid = _explode(grid)
    first = next((r for r, row in enumerate(grid) if year_at(row) is not None), None)
    if not first:
        return []
    year_col = year_at(grid[first])

    def label_of(row):
        return next(((i, t) for i, t in row[:year_col] if t), (None, ""))

    width = max(len(row) for row in grid)
    headers = []
    for c in range(width):
        seen, parts = set(), []
        for row in grid[:first]:
            if c < len(row) and row[c][0] not in seen and row[c][1]:
                seen.add(row[c][0])
                parts.append(row[c][1])
        headers.append(" ".join(parts))
    columns: dict = {}
    for c, header in enumerate(headers):
        metric = _metric_of(header)
        if metric and metric != "year" and c != year_col:
            columns.setdefault(metric, []).append(c)
    if not {"salary", "total_compensation"} <= columns.keys():
        return []

    years = [_year(row[year_col][1]) if len(row) > year_col else None for row in grid]
    steps = [b - a for a, b in zip(years, years[1:]) if a and b and a != b]
    newest_first = sum(s < 0 for s in steps) >= sum(s > 0 for s in steps)

    def looks_like_name(text):
        first = text.split("\n")[0]
        return not _TITLE_WORDS.search(first) and is_person_name(clean_person_name(first))

    blocks, current, pending, prev = [], None, [], None
    for r in range(first, len(grid)):
        cell_id, label = label_of(grid[r])
        year = years[r]
        if year is None:
            # The label cell spanning on past the figures repeats nothing new.
            if label and not (current and cell_id in {i for i, _ in current["labels"]}):
                # A name row with no figures opens the next executive; anything
                # else (a title line) belongs to the current one.
                target = pending if looks_like_name(label) or not current else current["labels"]
                target.append((cell_id, label))
            continue
        # The year cell spans this row and the last (TPR: name row + title row
        # share one row of figures): the same executive-year, one more label line.
        if current and r > first and len(grid[r - 1]) > year_col \
                and grid[r][year_col][0] == grid[r - 1][year_col][0]:
            if label and cell_id not in {i for i, _ in current["labels"]}:
                current["labels"].append((cell_id, label))
            continue
        if (current is None or prev is None or pending
                or (year >= prev if newest_first else year <= prev)):
            current = {"labels": pending, "rows": []}
            pending = []
            blocks.append(current)
        if label and cell_id not in {i for i, _ in current["labels"]}:
            current["labels"].append((cell_id, label))
        current["rows"].append(r)
        prev = year

    out = []
    for block in blocks:
        lines = [re.sub(r"\(\w{1,3}\)|\[\d+\]|\*+", "", ln).strip(" ,")
                 for _, lab in block["labels"] for ln in lab.split("\n")]
        lines = list(dict.fromkeys(re.sub(r"^\((.*)\)$", r"\1", ln) for ln in lines if ln))
        if not lines:
            continue
        name, rest = _split_name(lines[0])
        name = clean_person_name(name)
        # No name in the table (MSS lists two executives by title only): drop
        # the block rather than show a title as a person.
        if not is_person_name(name) or _TITLE_WORDS.search(name):
            continue
        title = " ".join(dict.fromkeys([rest] + lines[1:] if rest else lines[1:])).strip(" ,")
        for r in block["rows"]:
            rec = {"executive_name": name, "title": title, "year": years[r]}
            for metric, cols in columns.items():
                if metric == "pension":
                    continue
                seen, value = set(), None
                for c in cols:
                    cell_id, text = grid[r][c] if c < len(grid[r]) else (None, "")
                    if cell_id in seen or text in ("", "$"):
                        continue
                    seen.add(cell_id)
                    value = _amount(text)
                    if value is not None:
                        break
                rec[metric] = value
            out.append(rec)
    return out


def _is_candidate(table_html: str) -> bool:
    text = re.sub(r"<[^>]+>|&nbsp;|&#160;|&#xa0;", " ", table_html).lower()
    return ("salary" in text and "total" in text and re.search(r"\b20\d\d\b", text) is not None
            and re.search(r"stock\s*awards?|option\s*awards?|bonus", text) is not None)


def parse_summary_compensation(html: str) -> List[dict]:
    """Rows of the proxy's Summary Compensation Table: one per executive-year.

    Candidate tables are cut out of the raw HTML first (cheap) and only those are
    parsed with edgartools — a whole proxy is 150-500 tables. Each executive is a
    block of rows, newest year first (some proxies run oldest first); the name is
    the first line of the block's label cells and the lines after it the position.
    The first candidate that yields named executives wins: the SCT comes before the
    pay-versus-performance and director tables, which name nobody per row.
    """
    from edgar.documents import parse_html

    for m in re.finditer(r"<table\b.*?</table>", html, re.I | re.S):
        if not _is_candidate(m.group(0)):
            continue
        try:
            tables = parse_html(f"<html><body>{m.group(0)}</body></html>").tables
        except Exception:
            continue
        for table in tables:
            rows = _sct_rows(_grid(table))
            if rows:
                return rows
    return []

# =============================================================================
# FRAME — read from disk, refresh in background
# =============================================================================

def read_proxy_pay() -> pd.DataFrame:
    """The parsed rows on disk, or an empty frame. Never builds, never waits."""
    from utils.materialize import _disk_copy
    obj = _disk_copy(FRAME_NAME)
    if isinstance(obj, tuple) and len(obj) == 2:
        return obj[0]
    return pd.DataFrame()


def _wanted_proxies() -> pd.DataFrame:
    rows = db_manager.execute_query_readonly_raising(_PROXY_QUERY, {}) or []
    df = pd.DataFrame(rows, columns=["ticker", "event_date", "source_ref"])
    df["url"] = df["source_ref"].map(proxy_url)
    df["accession"] = df["url"].map(accession_from_url)
    df = df[df["accession"].ne("")].drop_duplicates("accession")
    df = df.sort_values("event_date", ascending=False)
    df["rank"] = df.groupby("ticker").cumcount()
    # Newest proxy of every company first, then the second, then the third: an
    # interrupted first run still covers every company's latest three years.
    return df[df["rank"] < PROXIES_PER_COMPANY].sort_values(["rank", "event_date"],
                                                           ascending=[True, False])


def refresh_proxy_pay(limit: Optional[int] = None, save_every: int = 25) -> int:
    """Read proxies not yet on disk; return how many were read.

    A proxy with no readable table is remembered too, so it is not fetched again.
    """
    import requests
    from utils.materialize import _disk_copy, write_materialized

    # Plain requests, not edgartools' downloader: that one keeps every
    # /Archives document in its disk cache forever (~3.4 MB a proxy, ~4 GB here).
    session = requests.Session()
    session.headers["User-Agent"] = SEC_USER_AGENT
    t0 = time.perf_counter()
    obj = _disk_copy(FRAME_NAME)
    rows, done = (obj if isinstance(obj, tuple) and len(obj) == 2
                  else (pd.DataFrame(), pd.DataFrame(columns=["accession"])))
    seen = set(done["accession"])
    todo = _wanted_proxies()
    todo = todo[~todo["accession"].isin(seen)]
    if limit:
        todo = todo.head(limit)
    if todo.empty:
        return 0

    def fetch(item):
        try:
            response = session.get(item.url, timeout=60)
            response.raise_for_status()
            return item, response.text
        except Exception as exc:
            return item, exc

    new_rows, new_done, read = [], [], 0
    # Four downloads in flight: each is 1-6 s of network wait for ~3 MB, and four
    # stay far below the SEC's 10 requests/s fair-access limit.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for item, html in pool.map(fetch, todo.itertuples()):
            if isinstance(html, Exception):
                # Left out of `done`: a network failure is retried on the next run.
                log_structured_error(html, page="proxy_pay", component="refresh_proxy_pay",
                                     operation="read_proxy", context=item.url)
                continue
            try:
                parsed = parse_summary_compensation(html)
            except Exception as exc:
                # Same bytes, same failure: recorded as done, never refetched.
                log_structured_error(exc, page="proxy_pay", component="refresh_proxy_pay",
                                     operation="parse_proxy", context=item.url)
                parsed = []
            for rec in parsed:
                rec.update(ticker=item.ticker, filing_date=item.event_date,
                           accession=item.accession, url=item.url)
                new_rows.append(rec)
            new_done.append(item.accession)
            read += 1
            if read % save_every == 0:
                rows = pd.concat([rows, pd.DataFrame(new_rows)], ignore_index=True)
                done = pd.concat([done, pd.DataFrame({"accession": new_done})], ignore_index=True)
                new_rows, new_done = [], []
                write_materialized(FRAME_NAME, (rows, done), [])
    if new_done:
        rows = pd.concat([rows, pd.DataFrame(new_rows)], ignore_index=True)
        done = pd.concat([done, pd.DataFrame({"accession": new_done})], ignore_index=True)
        write_materialized(FRAME_NAME, (rows, done), [])
    log_timing("PROXY_PAY_REFRESH", (time.perf_counter() - t0) * 1000,
               details=f"read={read} rows={len(rows)} proxies={len(done)}", level="WARNING")
    return read
