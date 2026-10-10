"""
People Service
==============
Data-access layer for People Screening (executive compensation + officers).

Responsibilities:
- Build ONE materialized frame holding every executive-year we have, from both
  sources, with no ticker filter (see `get_people_universe`)
- Classify free-text titles into selectable role buckets
- Apply a People Attributes criterion to that frame — pure pandas, no DB

This module NEVER touches Streamlit UI beyond @st.cache_data.
All DB access goes through db_manager from core.database.
All logging via server_logger only.

Schema notes:
- coreiq_executives_compensation holds SEC proxy extractions. Every money column
  is a VARCHAR of raw dollars, '' when the proxy had no value for that row.
  `compensation_year` is the proxy's own fiscal label; `year` is the filing year,
  and they differ often enough that both are surfaced rather than reconciled.
- coreiq_yf_company_overview.payload_json -> info.companyOfficers carries the
  non-SEC side. It has 57-86 rows per ticker (one per daily ETL run), so the
  latest row per ticker MUST be picked or every officer repeats ~80x.

See docs/superpowers/specs/2026-10-06-screening-people-criteria-design.md.
"""

import json
import re
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from core.database import db_manager
from data.screening_config import (
    DB_SCALE,
    OPERATOR_SQL,
    PEOPLE_MONEY_METRICS,
    PEOPLE_ROLE_OTHER,
    PEOPLE_ROLE_PATTERNS,
    PEOPLE_ROLE_UNKNOWN,
)

try:
    from utils.server_logger import log_structured_error, log_timing, log_warning
except ImportError:  # pragma: no cover - logging is never allowed to break a page
    def log_structured_error(exc, **kwargs): pass
    def log_timing(*a, **kw): pass
    def log_warning(*a, **kw): pass


# =============================================================================
# ROLE CLASSIFICATION
# =============================================================================

# Compiled once at import. First match wins, so the list order in
# screening_config.PEOPLE_ROLE_PATTERNS is part of the contract.
_ROLE_REGEX = [(name, re.compile(pat, re.I)) for name, pat in PEOPLE_ROLE_PATTERNS]

_FORMER_REGEX = re.compile(r"\bformer\b", re.I)

# Junk the proxy-table extractor glues onto a name: zero-width/BOM characters,
# and the footnote markers that sit beside the name in the Summary Compensation
# Table ("Richard A. Galanti 8", "Fabrizio Freda ( 1 )", "Jeffrey Davis \ufeff").
_NAME_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\ufeff]")
_NAME_FOOTNOTE = re.compile(r"\s*(\(\s*\d+\s*\)|\[\s*\d+\s*\]|\d{1,2})\s*$")
_NAME_TRAILING_MARKS = re.compile(r"[\s*\u2013\u2014-]+$")

# Title words the extractor glues AFTER a name: "Brian T. Olsavsky SVP and",
# "Franck J. Moison Retired", "Martin P. Waters CEO/President". Each spelling
# became its own grid row next to the clean one.
_NAME_TITLE_WORDS = {
    "svp", "sevp", "evp", "vp", "and", "&", "ceo", "cfo", "coo", "president",
    "retired", "former", "division", "enterprise", "sector", "corporate",
    "special", "strategic", "technical", "advisor", "founder", "chief",
    "officer", "executive", "chairman",
}
# Never part of WHO someone is. Yahoo prefixes every officer with an honorific,
# so first-token keys read "Mr." as the first name and merged Bernard Arnault
# with his three sons ('mr|arnault').
_NAME_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame"}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "ph", "md", "cpa", "mba", "esq"}


def _is_title_token(token: str) -> bool:
    word = token.lower().strip(".,*()\u2013\u2014-")
    return not word or all(part in _NAME_TITLE_WORDS for part in word.split("/") if part)


def clean_person_name(name):
    """Strip extraction junk from an executive name.

    The same person otherwise appears several times under spellings that differ
    only by a footnote marker or an invisible character, and each one became its
    own grid row.
    """
    text = _NAME_ZERO_WIDTH.sub("", (name or "")).strip()
    # Twice: "Galanti 8 9" style double markers do occur.
    for _ in range(2):
        stripped = _NAME_FOOTNOTE.sub("", text).strip()
        if stripped == text or not stripped:
            break
        text = stripped
    text = re.sub(r"\s+", " ", text).strip(" ,;.")
    tokens = text.split(" ")
    # A "name" made only of title words ("Technical Advisor") is left alone —
    # emptying it would merge unrelated rows.
    if not all(_is_title_token(t) for t in tokens):
        while len(tokens) > 1 and _is_title_token(tokens[-1]):
            tokens.pop()
    return _NAME_TRAILING_MARKS.sub("", " ".join(tokens)).strip(" ,;.")


def person_key(name):
    """Every full name word, for matching one person across spellings.

    "Mark D. Papermaster" and "Mark Papermaster" are one person filed two ways;
    single-letter tokens (middle initials) are dropped so both collapse to
    "mark|papermaster". Measured on STG: 176 (ticker, year) groups held one
    person under more than one spelling, 363 rows, 5.7% of the SEC side.

    Full middle names are KEPT: first + last alone merged "See Long Soon" with
    "See Beng Soon" (two directors of one company). Spellings that differ by a
    whole word are matched by `_merge_name_variants` on equal pay instead.

    Honorifics and generational / degree suffixes are dropped too, so
    "Mr. Tae-Moon Roh Ph.D." keys as tae-moon|roh, not mr|ph.
    """
    tokens = [t for t in (re.sub(r"[^\w'-]", "", t)
                          for t in re.split(r"[\s.]+", clean_person_name(name).lower()))
              if len(t) > 1 and t not in _NAME_HONORIFICS and t not in _NAME_SUFFIXES]
    return "|".join(tokens)


def classify_role(title: Optional[str]) -> str:
    """Collapse a free-text executive title into one selectable role bucket.

    Blank title and no pattern matched both -> PEOPLE_ROLE_OTHER: the two meant
    the same thing to a user ("not one of the named roles"), so they are one
    bucket (manager, 2026-10-09). 511 SEC rows have no position at all.
    """
    text = (title or "").strip()
    if not text:
        return PEOPLE_ROLE_OTHER
    for name, rx in _ROLE_REGEX:
        if rx.search(text):
            return name
    return PEOPLE_ROLE_OTHER


def is_former_title(title: Optional[str]) -> bool:
    """True when the title marks a departed executive ('Former CFO')."""
    return bool(_FORMER_REGEX.search(title or ""))


# =============================================================================
# VALUE PARSING
# =============================================================================

def _to_float(val: Any) -> Optional[float]:
    """Cast a raw DB value to float, treating '' and junk as missing.

    Every money column in coreiq_executives_compensation is a VARCHAR, and the
    extractor writes '' (not NULL) when a proxy row had no value for a column.
    """
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        # Some rows carry footnote marks or stray commas from the proxy table.
        try:
            cleaned = re.sub(r"[^0-9.\-]", "", str(val))
            return float(cleaned) if cleaned not in ("", "-", ".", "-.") else None
        except (TypeError, ValueError):
            return None


# A Summary Compensation Table amount as printed: thousands separators
# (including the typographic apostrophe one proxy uses), optionally negative in
# parentheses. Plain 4-digit numbers are years, and "(3)" is a footnote.
_ROW_AMOUNT = re.compile(r"(\()?\s*\$?\s*(\d{1,3}(?:[,\u2019']\d{3})+)\s*(\))?")


def last_row_amount(raw_row: Optional[str]) -> Optional[float]:
    """The last amount in a filed table row — its Total column.

    None when the row ends on a negative (that is a part, never the total) or
    has no amount at all.
    """
    matches = list(_ROW_AMOUNT.finditer(raw_row or ""))
    if not matches:
        return None
    last = matches[-1]
    if last.group(1) and last.group(3):
        return None
    return float(re.sub(r"[,\u2019']", "", last.group(2)))


def _to_int(val: Any) -> Optional[int]:
    """Cast a raw DB value to int, treating '' and junk as missing."""
    f = _to_float(val)
    return int(f) if f is not None else None


# =============================================================================
# UNIVERSE BUILD
# =============================================================================

_SEC_QUERY = """
    SELECT ticker, executive_name, compensation_year, position, form, year,
           salary, bonus, stock_awards, option_awards, non_equity_incentive,
           all_other_compensation, total_compensation, blob_name, confidence_score,
           raw_row
    FROM coreiq_executives_compensation
"""

# ROW_NUMBER picks the newest overview row per ticker. Without it the table
# returns one row per daily ETL run and every officer is duplicated ~80x.
# The ranking reads only (ticker, ingested_at, id) from idx_ticker_ingested; payloads
# are fetched for the 75 winners only. Ranking with payload_json in the window read every
# snapshot's JSON (~9.5k rows): 7.3 s cold vs 1.7 s, same rows.
_YF_QUERY = """
    SELECT o.ticker, o.payload_json
    FROM coreiq_yf_company_overview o
    JOIN (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY ticker ORDER BY ingested_at DESC
               ) AS _rn
        FROM coreiq_yf_company_overview
    ) _ranked ON _ranked.id = o.id AND _ranked._rn = 1
"""

# Year-end USD rate per currency: ~20 years x 10 currencies = ~200 rows, so the
# window runs server-side instead of shipping 30k daily rows (cost here is rows
# fetched, not bytes). Units of to_currency per 1 USD.
# ponytail: the current year's rate is taken when the frame is built and moves
# only when the people tables change; fine for pay, which is annual.
_FX_QUERY = """
    SELECT to_currency, yr, close
    FROM (
        SELECT to_currency, YEAR(day_date) AS yr, close,
               ROW_NUMBER() OVER (
                   PARTITION BY to_currency, YEAR(day_date) ORDER BY day_date DESC
               ) AS _rn
        FROM coreiq_av_forex_daily
        WHERE from_currency = 'USD' AND close > 0
    ) _ranked
    WHERE _rn = 1
"""

_FRAME_COLUMNS = [
    "ticker", "executive_name", "title", "role", "is_former", "year",
    "salary", "bonus", "stock_awards", "option_awards", "non_equity_incentive",
    "all_other_compensation", "total_compensation",
    "total_pay", "total_pay_local", "pay_currency",
    "exercised_value", "unexercised_value",
    "age", "year_born",
    "source", "filing_form", "filing_year", "filing_blob", "confidence",
    # Build-time only: the total as printed at the end of the filed row. Used by
    # _repair_pay and dropped before the frame is stored.
    "raw_total",
]


def _sec_records(rows: List[Dict[str, Any]]) -> List[dict]:
    """Map SEC proxy rows onto the unified people schema."""
    out = []
    for r in rows:
        ticker = (r.get("ticker") or "").strip()
        name = clean_person_name(r.get("executive_name"))
        title = (r.get("position") or "").strip()
        out.append({
            "ticker":                 ticker,
            "executive_name":         name,
            "title":                  title,
            "role":                   classify_role(title),
            "is_former":              is_former_title(title),
            "year":                   _to_int(r.get("compensation_year")),
            "salary":                 _to_float(r.get("salary")),
            "bonus":                  _to_float(r.get("bonus")),
            "stock_awards":           _to_float(r.get("stock_awards")),
            "option_awards":          _to_float(r.get("option_awards")),
            "non_equity_incentive":   _to_float(r.get("non_equity_incentive")),
            "all_other_compensation": _to_float(r.get("all_other_compensation")),
            "total_compensation":     _to_float(r.get("total_compensation")),
            "total_pay":              None,
            "total_pay_local":        None,
            "pay_currency":           "",
            "exercised_value":        None,
            "unexercised_value":      None,
            "age":                    None,
            "year_born":              None,
            "source":                 "SEC",
            "filing_form":            (r.get("form") or "").strip(),
            "filing_year":            _to_int(r.get("year")),
            "filing_blob":            (r.get("blob_name") or "").strip(),
            "confidence":             _to_int(r.get("confidence_score")),
            "raw_total":              last_row_amount(r.get("raw_row")),
        })
    return out


def _usd_rate(fx: Dict[str, Dict[int, float]], currency: str, year: Optional[int]) -> Optional[float]:
    """Units of `currency` per 1 USD at the end of `year` (latest year on file
    at or before it). None when the currency has no rate on file (MYR, SGD)."""
    if currency == "USD":
        return 1.0
    by_year = fx.get(currency) or {}
    usable = [y for y in by_year if year is None or y <= year]
    return by_year[max(usable)] if usable else None


def _yf_money(value: Any) -> Optional[float]:
    """Yahoo sends 0 for a value it does not have, so 0 is treated as missing."""
    f = _to_float(value)
    return f if f else None


def _yf_records(rows: List[Dict[str, Any]], fx: Dict[str, Dict[int, float]]) -> List[dict]:
    """Map YF companyOfficers onto the unified people schema.

    Yahoo reports pay in the company's own currency (info.financialCurrency):
    Samsung's 5,099,000,000 is won, not dollars. `total_pay` is converted to USD
    so it sits beside SEC pay and filters in $mm; the original figure and its
    currency are kept in `total_pay_local` / `pay_currency`.

    Officers with no totalPay are KEPT. The previous implementation skipped
    them, which dropped 603 of 916 officers (66%) even though they carry a
    name, title, age and exercised/unexercised value — People Screening
    silently under-reported every non-SEC company.
    """
    out = []
    for r in rows:
        ticker = (r.get("ticker") or "").strip()
        payload = r.get("payload_json")
        if not payload:
            continue
        try:
            info = json.loads(payload).get("info") or {}
            officers = info.get("companyOfficers") or []
        except (ValueError, TypeError, AttributeError):
            continue
        currency = (info.get("financialCurrency") or "").strip().upper()
        for o in officers:
            if not isinstance(o, dict):
                continue
            name = clean_person_name(o.get("name"))
            title = (o.get("title") or "").strip()
            if not name and not title:
                continue
            year = _to_int(o.get("fiscalYear"))
            rate = _usd_rate(fx, currency, year) if currency else None

            def usd(value):
                return value / rate if value is not None and rate else None

            pay_local = _yf_money(o.get("totalPay"))
            out.append({
                "ticker":                 ticker,
                "executive_name":         name,
                "title":                  title,
                "role":                   classify_role(title),
                "is_former":              is_former_title(title),
                "year":                   year,
                "salary":                 None,
                "bonus":                  None,
                "stock_awards":           None,
                "option_awards":          None,
                "non_equity_incentive":   None,
                "all_other_compensation": None,
                "total_compensation":     None,
                "total_pay":              usd(pay_local),
                "total_pay_local":        pay_local,
                "pay_currency":           currency,
                "exercised_value":        usd(_yf_money(o.get("exercisedValue"))),
                "unexercised_value":      usd(_yf_money(o.get("unexercisedValue"))),
                "age":                    _to_int(o.get("age")),
                "year_born":              _to_int(o.get("yearBorn")),
                "source":                 "YFinance",
                "filing_form":            "",
                "filing_year":            None,
                "filing_blob":            "",
                "confidence":             None,
                "raw_total":              None,
            })
    return out


# Summary Compensation Table columns, left to right as the proxy prints them.
_PAY_PARTS = ["salary", "bonus", "stock_awards", "option_awards",
              "non_equity_incentive", "all_other_compensation"]


def _repair_pay(df: pd.DataFrame) -> pd.DataFrame:
    """Undo the two proxy-extractor faults that are provable from the row itself.

    1. Footnote text read as a pay row: the same number in the total AND two or
       more pay columns (Zebra "Cristen Kogl" $11,100,484 everywhere). Not a
       person-year at all, so the row is dropped.
    2. A smeared value: when a proxy leaves a cell blank without a dash, the
       extractor slides the next value left into it, so one figure sits in two
       adjacent columns and the parts overshoot the total (Apple 2023: the
       $46,970,283 stock award is also stored as Bonus). The rightmost copy is
       the real column; the others are cleared — only when that makes the row
       add up.
    3. A subtotal read as the total: PepsiCo prints non-equity pay as two
       sub-columns plus their sum, and the extractor took that sum ($6,766,500
       for Ramon Laguarta 2024) as the total. The total is the LAST figure of
       the filed row ($28,814,759); it replaces the stored one only when it
       makes the row add up and the stored one does not.

    Adds `_sane`: the total is at least every single part and the parts do not
    overshoot it by more than 5% (a NEGATIVE pension change, which the table has
    no column for, legitimately does — Caleres 2022, -$51,306).
    """
    parts = df[_PAY_PARTS]
    total = df["total_compensation"]
    footnote = parts.eq(total, axis=0).sum(axis=1) >= 2
    df = df[~footnote].copy()
    total = df["total_compensation"]

    over = df[_PAY_PARTS].sum(axis=1, min_count=1) > total + 1000
    for idx in df.index[over]:
        row = df.loc[idx, _PAY_PARTS]
        repaired = row.copy()
        for value, cols in row.dropna().groupby(row.dropna()).groups.items():
            if value > 0 and len(cols) > 1:
                repaired[list(cols)[:-1]] = None
        if repaired.sum(min_count=1) <= df.at[idx, "total_compensation"] + 1000:
            df.loc[idx, _PAY_PARTS] = repaired.astype(float)

    def adds_up(total):
        parts = df[_PAY_PARTS]
        return (total >= parts.max(axis=1).fillna(0)) & (parts.sum(axis=1) <= total * 1.05 + 1000)

    total = df["total_compensation"]
    rescued = ~adds_up(total) & total.notna() & adds_up(df["raw_total"])
    df.loc[rescued, "total_compensation"] = df.loc[rescued, "raw_total"]
    df["_sane"] = (df["total_compensation"].isna() | adds_up(df["total_compensation"])).astype(int)
    return df.drop(columns=["raw_total"])


def _merge_name_variants(df: pd.DataFrame) -> pd.Series:
    """Map every row to one person id per (ticker, source), across spellings.

    Same key ("Mark D. Papermaster" / "Mark Papermaster") is one person. So is a
    pair filed in the same year on the SAME pay that shares a first name or a
    surname: nicknames (Mike / R. Michael Mohan, Bob / Robert W. Eddy), married
    names (Karalyn Smith / Karalyn Yearout), split surnames (Benno Dor er).
    Checked on all 45 such pairs in STG — every one is one person.

    Equal pay ALONE is not enough: Apple pays Sewell, Riccio and Cue the same,
    Amazon pays Olsavsky and Zapolsky the same. They share no name, so they stay
    apart; Alex and Mike Dillard share a surname but not their pay.
    """
    keys = df["executive_name"].map(person_key)
    parent = {}

    def find(k):
        while parent.get(k, k) != k:
            k = parent[k]
        return k

    pay = df["total_compensation"].fillna(df["total_pay"]).round(-3)
    scope = df["ticker"].astype(str) + "|" + df["source"].astype(str)
    node = scope + "#" + keys
    bucket = pd.DataFrame({"node": node, "key": keys, "scope": scope,
                           "year": df["year"], "pay": pay})
    bucket = bucket[bucket["pay"].gt(0) & keys.ne("")].drop_duplicates(["node", "year", "pay"])
    for _, grp in bucket.groupby(["scope", "year", "pay"]):
        pairs = list(zip(grp["node"], grp["key"].str.split("|")))
        for i, (node_a, words_a) in enumerate(pairs):
            for node_b, words_b in pairs[i + 1:]:
                if words_a[0] == words_b[0] or words_a[-1] == words_b[-1]:
                    parent[find(node_b)] = find(node_a)
    return node.map(find)


def _collapse_restatements(df: pd.DataFrame) -> pd.DataFrame:
    """Keep ONE row per (ticker, person, year, source) — the best disclosure.

    A proxy's Summary Compensation Table restates the three prior fiscal years,
    and PRE 14A / DEF 14A are the preliminary and final versions of the same
    proxy. So coreiq_executives_compensation holds 14,007 rows covering only
    6,418 real person-years: 2.2x on average, up to 6x for one executive-year.
    Every one of those copies was being rendered as its own grid row.

    The copies disagree on `position` in 2,480 groups — later proxies abbreviate
    the title and eventually prefix it with "Former" — and in 338 groups on the
    total by more than $1,000, because one copy was mis-parsed. So:

    Preference order, best first:
      1. a copy whose pay adds up (`_repair_pay`) — 99 person-years had a broken
         copy AND a clean one, and the broken one was winning
      2. a row that actually has a position (511 rows have none at all)
      3. higher confidence_score (the extractor's own quality signal)
      4. the larger total, to $1,000 — every other disagreement is a truncation
         (ACI 2020: Robert Dimond $7,146,900 vs "Robert B. Dimond" $6,900)
      5. DEF 14A over PRE 14A — final over preliminary
      6. earliest filing year — the original disclosure, not a later abbreviation

    When no copy adds up, the total is withheld (blank) rather than shown: a
    total below the salary is a mis-read, not a fact. The parts are kept.
    """
    if df.empty:
        return df

    before = len(df)
    df = _repair_pay(df)
    _pay = df["total_compensation"].fillna(df["total_pay"])
    ranked = df.assign(
        _person=_merge_name_variants(df),
        _has_title=df["title"].astype(str).str.strip().ne("").astype(int),
        _confidence=pd.to_numeric(df["confidence"], errors="coerce").fillna(-1),
        # Rounded to the nearest $1,000 so the ±$2 differences between
        # restatements TIE here and the filing-year preference below still
        # picks the title.
        # ponytail: largest-wins heuristic; every observed disagreement was a
        # truncation, never an inflation. Revisit if that stops holding.
        _pay_rank=pd.to_numeric(_pay, errors="coerce").fillna(-1).round(-3),
        _is_def=df["filing_form"].astype(str).str.upper().str.startswith("DEF").astype(int),
        _filing_year=pd.to_numeric(df["filing_year"], errors="coerce").fillna(9999),
        # Longer spelling = the one carrying the middle initial.
        _name_len=df["executive_name"].astype(str).str.len(),
    ).sort_values(
        ["_sane", "_has_title", "_confidence", "_pay_rank", "_is_def", "_filing_year", "_name_len"],
        ascending=[False, False, False, False, False, True, False],
        kind="mergesort",
    )

    keys = ["ticker", "_person", "year", "source"]
    collapsed = ranked.drop_duplicates(subset=keys, keep="first").copy()

    # The copy that adds up can be the one filed without a title; borrow the
    # title from the best titled copy of the same person-year.
    titled = ranked[ranked["_has_title"].eq(1)].drop_duplicates(subset=keys)
    best_title = collapsed[keys].merge(titled[keys + ["title"]], on=keys, how="left")["title"]
    no_title = collapsed["title"].astype(str).str.strip().eq("").to_numpy() & best_title.notna().to_numpy()
    if no_title.any():
        collapsed.loc[no_title, "title"] = best_title[no_title].to_numpy()
        collapsed.loc[no_title, "role"] = collapsed.loc[no_title, "title"].map(classify_role)
        collapsed.loc[no_title, "is_former"] = collapsed.loc[no_title, "title"].map(is_former_title)

    collapsed.loc[collapsed["_sane"].eq(0), "total_compensation"] = None

    # One display name per person in every year: the fullest spelling filed.
    longest = (ranked.assign(_len=ranked["executive_name"].str.len())
               .sort_values("_len", ascending=False, kind="mergesort")
               .drop_duplicates(subset=["ticker", "_person", "source"])
               .set_index(["ticker", "_person", "source"])["executive_name"])
    collapsed["executive_name"] = longest.reindex(
        pd.MultiIndex.from_frame(collapsed[["ticker", "_person", "source"]])).to_numpy()

    collapsed = collapsed.drop(columns=["_person", "_sane", "_has_title", "_confidence",
                                        "_pay_rank", "_is_def", "_filing_year", "_name_len"])

    # Preserve the natural reading order rather than the ranking order.
    collapsed = collapsed.sort_values(
        ["ticker", "executive_name", "year"], ascending=[True, True, False],
        kind="mergesort",
    ).reset_index(drop=True)

    log_timing("PEOPLE_RESTATEMENT_COLLAPSE", 0,
               details=f"{before} source rows -> {len(collapsed)} person-years")
    return collapsed


def _names_match(a: List[str], b: List[str]) -> bool:
    """One person filed two ways: same surname, and first names equal or one a
    short form of the other (Doug / Douglas). Yahoo often leads with an initial
    ("C. Douglas McMillon"), which person_key already drops."""
    if not a or not b or a[-1] != b[-1]:
        return False
    if len(a) == 1 or len(b) == 1:
        return False
    first_a, first_b = a[0], b[0]
    return (first_a == first_b
            or (min(len(first_a), len(first_b)) >= 3
                and (first_a.startswith(first_b) or first_b.startswith(first_a))))


def _merge_sources(df: pd.DataFrame) -> pd.DataFrame:
    """Combine a US company's SEC proxy rows with its Yahoo officer rows.

    Same company, same person (`_names_match`), and the match must be unique in
    both directions — an ambiguous pair is left as two people rather than risk
    giving one executive another's pay. Precedence:
      * SEC wins every field it has (pay parts, total, title);
      * Yahoo fills what SEC never discloses — year born, total pay, exercised /
        unexercised value — onto the SEC row of the SAME year;
      * age is derived per year from year born, so a 2019 row does not show the
        executive's age today.
    A Yahoo year with no SEC row stays, renamed to the SEC spelling so the
    person is one row in the grid. Merged rows read source "SEC+YFinance".
    """
    both = set(df.loc[df["source"] == "SEC", "ticker"]) & set(df.loc[df["source"] == "YFinance", "ticker"])
    if not both:
        return df
    df = df.copy()
    keys = df["executive_name"].map(person_key).str.split("|")
    drop = []
    matched_people = 0
    for ticker in both:
        sec_idx = df.index[(df["ticker"] == ticker) & (df["source"] == "SEC")]
        yf_idx = df.index[(df["ticker"] == ticker) & (df["source"] == "YFinance")]
        sec_people = {}
        for i in sec_idx:
            sec_people.setdefault("|".join(keys[i]), []).append(i)
        yf_people = {}
        for i in yf_idx:
            yf_people.setdefault("|".join(keys[i]), []).append(i)
        pairs = {}
        for yk in yf_people:
            hits = [sk for sk in sec_people if _names_match(sk.split("|"), yk.split("|"))]
            if len(hits) == 1:
                pairs.setdefault(hits[0], []).append(yk)
        for sk, yks in pairs.items():
            if len(yks) != 1:
                continue
            matched_people += 1
            yk = yks[0]
            sec_rows = sec_people[sk]
            name = df.at[sec_rows[0], "executive_name"]
            born = next((df.at[i, "year_born"] for i in yf_people[yk]
                         if pd.notna(df.at[i, "year_born"])), None)
            if born is None:
                yi = yf_people[yk][0]
                if pd.notna(df.at[yi, "age"]) and pd.notna(df.at[yi, "year"]):
                    born = int(df.at[yi, "year"]) - int(df.at[yi, "age"])
            for i in sec_rows:
                df.at[i, "source"] = "SEC+YFinance"
                if born is not None:
                    df.at[i, "year_born"] = born
                    if pd.notna(df.at[i, "year"]):
                        df.at[i, "age"] = int(df.at[i, "year"]) - int(born)
            by_year = {df.at[i, "year"]: i for i in sec_rows}
            for yi in yf_people[yk]:
                target = by_year.get(df.at[yi, "year"])
                if target is not None:
                    for col in ("total_pay", "total_pay_local", "pay_currency",
                                "exercised_value", "unexercised_value"):
                        if pd.isna(df.at[target, col]) or df.at[target, col] in ("", None):
                            df.at[target, col] = df.at[yi, col]
                    drop.append(yi)
                else:
                    df.at[yi, "executive_name"] = name
                    df.at[yi, "source"] = "SEC+YFinance"
                    if born is not None:
                        df.at[yi, "year_born"] = born
    log_timing("PEOPLE_SOURCE_MERGE", 0,
               details=f"tickers={len(both)} people={matched_people} yf_rows_folded={len(drop)}")
    return df.drop(index=drop).reset_index(drop=True)


def _build_people_universe() -> pd.DataFrame:
    """Fetch BOTH people sources whole and return the unified frame.

    Two queries, neither carrying a ticker IN list. The whole dataset is only
    ~14.5k rows, and a universal copy serves every criteria combination —
    whereas the previous per-ticker-tuple cache key re-paid 5.9s of DB time on
    every criteria edit.
    """
    import time as _time
    t0 = _time.perf_counter()

    try:
        sec_rows = db_manager.execute_query_readonly(_SEC_QUERY) or []
    except Exception as exc:
        log_structured_error(exc, page="people_service",
                             component="_build_people_universe",
                             operation="fetch_sec_compensation")
        sec_rows = []

    try:
        yf_rows = db_manager.execute_query_readonly(_YF_QUERY) or []
    except Exception as exc:
        log_structured_error(exc, page="people_service",
                             component="_build_people_universe",
                             operation="fetch_yf_officers")
        yf_rows = []

    fx: Dict[str, Dict[int, float]] = {}
    try:
        for r in db_manager.execute_query_readonly(_FX_QUERY) or []:
            fx.setdefault(str(r["to_currency"]).upper(), {})[int(r["yr"])] = float(r["close"])
    except Exception as exc:
        # No rates -> non-USD Yahoo pay stays local-only; never a crash.
        log_structured_error(exc, page="people_service",
                             component="_build_people_universe",
                             operation="fetch_fx_rates")

    records = _sec_records(sec_rows) + _yf_records(yf_rows, fx)

    df = pd.DataFrame(records, columns=_FRAME_COLUMNS)

    if df.empty:
        log_warning("[PEOPLE] universe built EMPTY — both sources returned nothing")
        return df

    df = _collapse_restatements(df)
    df = _merge_sources(df)

    df["is_former"] = df["is_former"].fillna(False).astype(bool)
    for col in ("year", "filing_year", "year_born", "confidence"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["age"] = pd.to_numeric(df["age"], errors="coerce").astype("Int64")
    for col in PEOPLE_MONEY_METRICS.values():
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["total_pay_local"] = pd.to_numeric(df["total_pay_local"], errors="coerce")
    for col in ("ticker", "role", "source", "filing_form", "pay_currency"):
        df[col] = df[col].fillna("").astype("category")
    for col in ("executive_name", "title", "filing_blob"):
        df[col] = df[col].fillna("").astype(str)

    try:
        from utils.mem_opt import audit_frame_numbers, release_memory
        audit_frame_numbers("people_universe", df)
        release_memory()
    except Exception:
        pass

    log_timing("PEOPLE_UNIVERSE_BUILD", (_time.perf_counter() - t0) * 1000,
               details=f"rows={len(df)} sec={len(sec_rows)} yf_tickers={len(yf_rows)}")
    return df


@st.cache_data(ttl=21600, show_spinner=False)
def get_people_universe() -> pd.DataFrame:
    """Every executive-year we have, from both sources. Materialized to disk.

    One universal copy, so narrowing or widening a company criterion costs
    nothing — the whole People filter pipeline runs in pandas from here.
    """
    from utils.materialize import materialized_or_build

    # The freshness signal MUST be a checksum, not COUNT(*). Re-extracting a
    # proxy filing is an UPDATE: the row count does not move, so a count-based
    # signal would never notice and the snapshot would serve stale compensation
    # forever. Same failure this repo already hit on screening_universe, where
    # 13 retired industry values were served long after the DB had changed.
    sources = [
        {
            "table": "coreiq_executives_compensation",
            "signal": ("SUM(CRC32(CONCAT_WS('|', ticker, executive_name, "
                       "compensation_year, position, salary, bonus, stock_awards, "
                       "option_awards, non_equity_incentive, "
                       "all_other_compensation, total_compensation)))"),
        },
        {
            "table": "coreiq_yf_company_overview",
            "signal": "SUM(CRC32(CONCAT_WS('|', ticker, ingested_at)))",
        },
    ]
    return materialized_or_build(
        # _v2: the frame's columns and dedupe changed (2026-10-07). The disk copy
        # is keyed on the source tables only, so without a new name a deploy
        # would keep serving the old snapshot.
        # _v3: Unknown folded into Other + SEC/Yahoo merge (2026-10-09).
        "people_universe_v3", _build_people_universe, sources,
        clear=lambda: get_people_universe.clear(),
    )


# =============================================================================
# CRITERION APPLICATION — pure pandas, no DB, no st.*
# =============================================================================

def _apply_money_filter(df: pd.DataFrame, criterion: dict) -> pd.DataFrame:
    """Filter on one compensation metric using the shared operator vocabulary.

    Values arrive in $mm (every financial criterion in this app does) and are
    scaled by DB_SCALE, because the people tables store raw dollars.
    """
    label = criterion.get("money_metric")
    if not label:
        return df
    col = PEOPLE_MONEY_METRICS.get(label)
    if not col or col not in df.columns:
        return df

    v1 = criterion.get("money_value1")
    v2 = criterion.get("money_value2")
    if v1 is None:
        return df

    operator = criterion.get("money_operator") or "Greater Than"
    threshold = float(v1) * DB_SCALE
    values = df[col]

    # A missing value never satisfies a comparison. Treating NaN as 0 would
    # report every executive with no option awards as earning exactly zero,
    # which is a different claim from "the proxy did not disclose it".
    if operator == "Between":
        if v2 is None:
            return df
        low, high = sorted((threshold, float(v2) * DB_SCALE))
        mask = values.between(low, high, inclusive="both")
    elif operator == "Greater Than":
        mask = values > threshold
    elif operator == "Less Than":
        mask = values < threshold
    elif operator == "Greater Than or Equal To":
        mask = values >= threshold
    elif operator == "Less Than or Equal To":
        mask = values <= threshold
    elif operator == "Equals":
        mask = values == threshold
    else:
        # Unknown operator: fall back to the SQL vocabulary rather than guess.
        if operator not in OPERATOR_SQL:
            return df
        mask = values > threshold

    return df[mask.fillna(False)]


def apply_people_criterion(df: pd.DataFrame, criterion: dict) -> pd.DataFrame:
    """Apply one People Attributes criterion to the people frame.

    Fields AND together; values inside one multiselect OR together. Stacking
    two criteria therefore narrows, matching how every other criterion type in
    Screening composes.

    Pure pandas on purpose: no DB handle and no st.* call. An st.* call inside
    a cached path fails only on cache HITS, which masquerades as a data bug.
    """
    if df is None or df.empty or not criterion:
        return df

    out = df

    roles = criterion.get("roles") or []
    if roles:
        # Saved criteria may still name the retired "Unknown" bucket.
        wanted = {PEOPLE_ROLE_OTHER if str(r) == PEOPLE_ROLE_UNKNOWN else str(r) for r in roles}
        out = out[out["role"].astype(str).isin(wanted)]

    title_contains = (criterion.get("title_contains") or "").strip()
    if title_contains:
        out = out[out["title"].str.contains(title_contains, case=False, na=False, regex=False)]

    name_contains = (criterion.get("name_contains") or "").strip()
    if name_contains:
        out = out[out["executive_name"].str.contains(
            name_contains, case=False, na=False, regex=False)]

    if not criterion.get("include_former", False):
        out = out[~out["is_former"].astype(bool)]

    years = criterion.get("years") or []
    if years:
        wanted = {int(y) for y in years}
        out = out[out["year"].isin(wanted)]

    age_min = criterion.get("age_min")
    age_max = criterion.get("age_max")
    if age_min is not None:
        out = out[(out["age"] >= int(age_min)).fillna(False)]
    if age_max is not None:
        out = out[(out["age"] <= int(age_max)).fillna(False)]

    sources = criterion.get("sources") or []
    if sources:
        # A merged row is "SEC+YFinance" and belongs to both.
        wanted = {str(s) for s in sources}
        # astype(bool): on an empty frame .map() yields float64, and df[float
        # Series] is a COLUMN selection that silently drops every column.
        out = out[out["source"].astype(str).map(lambda v: bool(wanted & set(v.split("+")))).astype(bool)]

    forms = criterion.get("filing_forms") or []
    if forms:
        out = out[out["filing_form"].astype(str).isin([str(f) for f in forms])]

    out = _apply_money_filter(out, criterion)

    return out


def build_people_summary(criterion: dict) -> str:
    """One-line label for the People criterion card."""
    parts = []

    roles = criterion.get("roles") or []
    if roles:
        parts.append(", ".join(str(r) for r in roles[:4])
                     + (f" +{len(roles) - 4}" if len(roles) > 4 else ""))

    if criterion.get("title_contains"):
        parts.append(f'Title contains "{criterion["title_contains"]}"')
    if criterion.get("name_contains"):
        parts.append(f'Name contains "{criterion["name_contains"]}"')

    metric = criterion.get("money_metric")
    if metric and criterion.get("money_value1") is not None:
        operator = criterion.get("money_operator") or "Greater Than"
        v1 = criterion["money_value1"]
        if operator == "Between" and criterion.get("money_value2") is not None:
            parts.append(f"{metric} ${v1:,.1f}mm – ${criterion['money_value2']:,.1f}mm")
        else:
            symbol = OPERATOR_SQL.get(operator, operator)
            parts.append(f"{metric} {symbol} ${v1:,.1f}mm")

    shown = criterion.get("show_metrics") or []
    if shown:
        parts.append("Show: " + ", ".join(str(m) for m in shown))

    years = criterion.get("years") or []
    if years:
        ys = sorted(int(y) for y in years)
        parts.append(f"FY {ys[0]}" if len(ys) == 1 else "FY " + ", ".join(str(y) for y in ys))

    if criterion.get("age_min") is not None or criterion.get("age_max") is not None:
        lo = criterion.get("age_min")
        hi = criterion.get("age_max")
        parts.append(f"Age {lo or ''}–{hi or ''}".replace(" –", "–").strip())

    sources = criterion.get("sources") or []
    if sources and len(sources) == 1:
        parts.append(f"{sources[0]} only")

    forms = criterion.get("filing_forms") or []
    if forms:
        parts.append(", ".join(str(f) for f in forms))

    if criterion.get("include_former"):
        parts.append("incl. former")

    return " · ".join(parts) if parts else "All people"


# =============================================================================
# YEAR-COLUMN GRID — pure pandas, no DB, no st.*
# =============================================================================

def pivot_people_years(df: pd.DataFrame, metrics: List[str]) -> pd.DataFrame:
    """One row per executive x metric, one column per year.

        Executive  Title  Metric  2024  2025  2026
        A          CEO    Salary  ...   ...   ...

    `metrics` are display labels (PEOPLE_MONEY_METRICS keys); only those are
    shown. Identity columns come from the executive's LATEST year on file, so a
    title change shows the current title once instead of splitting the row.
    An executive with no value for any chosen metric keeps one row with blank
    years — the person is in scope, the pay simply was not disclosed.
    `df` may carry Company / Industry / Country of Incorporation already joined.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    cols = [(label, PEOPLE_MONEY_METRICS[label]) for label in metrics
            if label in PEOPLE_MONEY_METRICS and PEOPLE_MONEY_METRICS[label] in df.columns]
    rows = df.assign(year=pd.to_numeric(df["year"], errors="coerce"))
    who = ["ticker", "executive_name"]
    latest = (rows.sort_values("year", ascending=False, na_position="last", kind="mergesort")
                  .drop_duplicates(who))
    attrs = [c for c in ("Company", "Industry", "Country of Incorporation", "title", "role",
                         "age", "year_born") if c in latest.columns]
    identity = latest[who + attrs]

    long = []
    for order, (label, col) in enumerate(cols):
        part = rows.loc[rows[col].notna() & rows["year"].notna(), who + ["year", col]]
        long.append(part.rename(columns={col: "value"}).assign(Metric=label, _order=order))
    long = pd.concat(long, ignore_index=True) if long else pd.DataFrame(
        columns=who + ["year", "value", "Metric", "_order"])

    if long.empty:
        grid = identity.assign(Metric="", _order=0)
        year_cols = []
    else:
        long["year"] = long["year"].astype(int).astype(str)
        grid = (long.pivot_table(index=who + ["Metric", "_order"], columns="year",
                                 values="value", aggfunc="first")
                    .reset_index())
        grid.columns.name = None
        year_cols = sorted(c for c in grid.columns if str(c).isdigit())
        missing = identity[~identity.set_index(who).index.isin(grid.set_index(who).index)]
        grid = grid.merge(identity, on=who, how="left")
        if not missing.empty:
            grid = pd.concat([grid, missing.assign(Metric="", _order=0)], ignore_index=True)

    sort_by = (["Company"] if "Company" in grid.columns else []) + ["executive_name", "_order"]
    grid = grid.sort_values(sort_by, kind="mergesort").drop(columns="_order")
    lead = [c for c in ("Company", "ticker", "executive_name", "title", "role", "Industry",
                        "Country of Incorporation", "age", "year_born") if c in grid.columns]
    return grid[lead + ["Metric"] + year_cols].reset_index(drop=True)
