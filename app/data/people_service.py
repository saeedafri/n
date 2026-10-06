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
    return re.sub(r"\s+", " ", text).strip(" ,;.")


def person_key(name):
    """First + last name token, for matching one person across spellings.

    "Mark D. Papermaster" and "Mark Papermaster" are one person filed two ways;
    single-letter tokens (middle initials) are dropped so both collapse to
    ("mark", "papermaster"). Measured on STG: 176 (ticker, year) groups held one
    person under more than one spelling, 363 rows, 5.7% of the SEC side.
    """
    tokens = [t for t in re.split(r"[\s.]+", clean_person_name(name).lower())
              if len(t) > 1]
    if not tokens:
        return ""
    return f"{tokens[0]}|{tokens[-1]}"


def classify_role(title: Optional[str]) -> str:
    """Collapse a free-text executive title into one selectable role bucket.

    Blank / missing title -> PEOPLE_ROLE_UNKNOWN (511 SEC rows have no position
    at all; the extractor records 'missing_position' for them).
    No pattern matched -> PEOPLE_ROLE_OTHER.
    """
    text = (title or "").strip()
    if not text:
        return PEOPLE_ROLE_UNKNOWN
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
           all_other_compensation, total_compensation, blob_name, confidence_score
    FROM coreiq_executives_compensation
"""

# ROW_NUMBER picks the newest overview row per ticker. Without it the table
# returns one row per daily ETL run and every officer is duplicated ~80x.
_YF_QUERY = """
    SELECT ticker, payload_json
    FROM (
        SELECT ticker, payload_json,
               ROW_NUMBER() OVER (
                   PARTITION BY ticker ORDER BY ingested_at DESC
               ) AS _rn
        FROM coreiq_yf_company_overview
    ) _ranked
    WHERE _rn = 1
"""

# The email source the data team owns. It does not exist yet, so the probe
# below fails closed to an all-blank Email column rather than erroring.
# ponytail: no contacts table yet, so Email ships empty. When
# coreiq_people_contacts lands, this query starts returning rows and nothing
# else has to change.
_CONTACTS_QUERY = """
    SELECT ticker, executive_name, email
    FROM coreiq_people_contacts
"""

_FRAME_COLUMNS = [
    "ticker", "executive_name", "email", "title", "role", "is_former", "year",
    "salary", "bonus", "stock_awards", "option_awards", "non_equity_incentive",
    "all_other_compensation", "total_compensation",
    "total_pay", "exercised_value", "unexercised_value",
    "age", "year_born",
    "source", "filing_form", "filing_year", "filing_blob", "confidence",
]


def _fetch_contacts() -> Dict[tuple, str]:
    """Return {(ticker, executive_name): email}, empty when no source exists.

    Kept separate and defensive because coreiq_people_contacts is a planned
    data-team deliverable: a missing table must leave the column blank, not
    break People Screening.
    """
    # Ask information_schema FIRST. Running the SELECT and catching the failure
    # works, but db_manager logs a STRUCTURED_ERROR before raising, so every
    # cache build wrote an ERROR line for a table that is simply not built yet —
    # noise that looks like a real fault to whoever reads the log.
    try:
        exists = db_manager.execute_query_readonly(
            "SELECT 1 AS present FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'coreiq_people_contacts' "
            "LIMIT 1"
        )
        if not exists:
            return {}
        rows = db_manager.execute_query_readonly(_CONTACTS_QUERY)
    except Exception:
        return {}
    out = {}
    for r in rows or []:
        tk = (r.get("ticker") or "").strip()
        nm = (r.get("executive_name") or "").strip()
        em = (r.get("email") or "").strip()
        if tk and nm and em:
            out[(tk, nm.casefold())] = em
    return out


def _sec_records(rows: List[Dict[str, Any]], contacts: Dict[tuple, str]) -> List[dict]:
    """Map SEC proxy rows onto the unified people schema."""
    out = []
    for r in rows:
        ticker = (r.get("ticker") or "").strip()
        name = clean_person_name(r.get("executive_name"))
        title = (r.get("position") or "").strip()
        out.append({
            "ticker":                 ticker,
            "executive_name":         name,
            "email":                  contacts.get((ticker, name.casefold()), ""),
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
            "exercised_value":        None,
            "unexercised_value":      None,
            "age":                    None,
            "year_born":              None,
            "source":                 "SEC",
            "filing_form":            (r.get("form") or "").strip(),
            "filing_year":            _to_int(r.get("year")),
            "filing_blob":            (r.get("blob_name") or "").strip(),
            "confidence":             _to_int(r.get("confidence_score")),
        })
    return out


def _yf_records(rows: List[Dict[str, Any]], contacts: Dict[tuple, str]) -> List[dict]:
    """Map YF companyOfficers onto the unified people schema.

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
            officers = (json.loads(payload).get("info") or {}).get("companyOfficers") or []
        except (ValueError, TypeError, AttributeError):
            continue
        for o in officers:
            if not isinstance(o, dict):
                continue
            name = clean_person_name(o.get("name"))
            title = (o.get("title") or "").strip()
            if not name and not title:
                continue
            out.append({
                "ticker":                 ticker,
                "executive_name":         name,
                "email":                  contacts.get((ticker, name.casefold()), ""),
                "title":                  title,
                "role":                   classify_role(title),
                "is_former":              is_former_title(title),
                "year":                   _to_int(o.get("fiscalYear")),
                "salary":                 None,
                "bonus":                  None,
                "stock_awards":           None,
                "option_awards":          None,
                "non_equity_incentive":   None,
                "all_other_compensation": None,
                "total_compensation":     None,
                "total_pay":              _to_float(o.get("totalPay")),
                "exercised_value":        _to_float(o.get("exercisedValue")),
                "unexercised_value":      _to_float(o.get("unexercisedValue")),
                "age":                    _to_int(o.get("age")),
                "year_born":              _to_int(o.get("yearBorn")),
                "source":                 "YFinance",
                "filing_form":            "",
                "filing_year":            None,
                "filing_blob":            "",
                "confidence":             None,
            })
    return out


def _collapse_restatements(df: pd.DataFrame) -> pd.DataFrame:
    """Keep ONE row per (ticker, person, year, source) — the best disclosure.

    A proxy's Summary Compensation Table restates the three prior fiscal years,
    and PRE 14A / DEF 14A are the preliminary and final versions of the same
    proxy. So coreiq_executives_compensation holds 14,007 rows covering only
    6,418 real person-years: 2.2x on average, up to 6x for one executive-year.
    Every one of those copies was being rendered as its own grid row.

    The copies agree on money (they disagree in 386 of 6,418 groups, and then
    only by rounding — 26,248,995 vs 26,248,997), but disagree on `position` in
    2,480 groups: later proxies abbreviate the title and eventually prefix it
    with "Former". The contemporaneous filing is therefore the one to keep —
    it carries the fullest title and describes the role as it was during the
    year being reported, so `role` and `is_former` stay true to that year.

    Preference order, best first:
      1. a row that actually has a position (511 rows have none at all)
      2. higher confidence_score (the extractor's own quality signal)
      3. DEF 14A over PRE 14A — final over preliminary
      4. earliest filing year — the original disclosure, not a later abbreviation
    """
    if df.empty:
        return df

    before = len(df)
    _pay = df["total_compensation"].fillna(df["total_pay"])
    ranked = df.assign(
        _person=df["executive_name"].map(person_key),
        _has_title=df["title"].astype(str).str.strip().ne("").astype(int),
        _confidence=pd.to_numeric(df["confidence"], errors="coerce").fillna(-1),
        # Rounded to the nearest $1,000 so the ±$2 differences between
        # restatements TIE here and the filing-year preference below still
        # picks the title. Only a materially different figure breaks the tie,
        # which is what a truncated parse looks like: ACI 2020 filed Robert
        # Dimond at $7,146,900 and "Robert B. Dimond" at $6,900.
        # ponytail: largest-wins heuristic; every observed disagreement was a
        # truncation, never an inflation. Revisit if that stops holding.
        _pay_rank=pd.to_numeric(_pay, errors="coerce").fillna(-1).round(-3),
        _is_def=df["filing_form"].astype(str).str.upper().str.startswith("DEF").astype(int),
        _filing_year=pd.to_numeric(df["filing_year"], errors="coerce").fillna(9999),
        # Longer spelling = the one carrying the middle initial.
        _name_len=df["executive_name"].astype(str).str.len(),
    ).sort_values(
        ["_has_title", "_confidence", "_pay_rank", "_is_def", "_filing_year", "_name_len"],
        ascending=[False, False, False, False, True, False],
        kind="mergesort",
    )

    # Key on the NORMALIZED person, not the raw string: "Mark D. Papermaster"
    # and "Mark Papermaster" are one person and were two adjacent grid rows
    # carrying identical pay.
    collapsed = ranked.drop_duplicates(
        subset=["ticker", "_person", "year", "source"], keep="first"
    ).drop(columns=["_person", "_has_title", "_confidence", "_pay_rank",
                    "_is_def", "_filing_year", "_name_len"])

    # Preserve the natural reading order rather than the ranking order.
    collapsed = collapsed.sort_values(
        ["ticker", "executive_name", "year"], ascending=[True, True, False],
        kind="mergesort",
    ).reset_index(drop=True)

    log_timing("PEOPLE_RESTATEMENT_COLLAPSE", 0,
               details=f"{before} source rows -> {len(collapsed)} person-years")
    return collapsed


def _build_people_universe() -> pd.DataFrame:
    """Fetch BOTH people sources whole and return the unified frame.

    Two queries, neither carrying a ticker IN list. The whole dataset is only
    ~14.5k rows, and a universal copy serves every criteria combination —
    whereas the previous per-ticker-tuple cache key re-paid 5.9s of DB time on
    every criteria edit.
    """
    import time as _time
    t0 = _time.perf_counter()

    contacts = _fetch_contacts()

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

    records = _sec_records(sec_rows, contacts) + _yf_records(yf_rows, contacts)

    df = pd.DataFrame(records, columns=_FRAME_COLUMNS)

    if df.empty:
        log_warning("[PEOPLE] universe built EMPTY — both sources returned nothing")
        return df

    df = _collapse_restatements(df)

    df["is_former"] = df["is_former"].fillna(False).astype(bool)
    for col in ("year", "filing_year", "year_born", "confidence"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["age"] = pd.to_numeric(df["age"], errors="coerce").astype("Int64")
    for col in PEOPLE_MONEY_METRICS.values():
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("ticker", "role", "source", "filing_form"):
        df[col] = df[col].fillna("").astype("category")
    for col in ("executive_name", "email", "title", "filing_blob"):
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
        "people_universe", _build_people_universe, sources,
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
        out = out[out["role"].astype(str).isin([str(r) for r in roles])]

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
        out = out[out["source"].astype(str).isin([str(s) for s in sources])]

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

    years = criterion.get("years") or []
    if years:
        ys = sorted(int(y) for y in years)
        parts.append(f"FY {ys[0]}" if len(ys) == 1 else f"FY {ys[0]}–{ys[-1]}")

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
