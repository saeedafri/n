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

import hashlib
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from core.database import db_manager
from data.screening_config import (
    DB_SCALE,
    OPERATOR_SQL,
    PEOPLE_CATEGORIES,
    PEOPLE_CATEGORY_CHANGES,
    PEOPLE_CATEGORY_MOVES,
    PEOPLE_CATEGORY_REMUNERATION,
    PEOPLE_EVENT_CATEGORIES,
    PEOPLE_MONEY_METRICS,
    PEOPLE_ROLE_BOARD,
    PEOPLE_ROLE_OTHER,
    PEOPLE_ROLE_PATTERNS,
    PEOPLE_ROLE_UNKNOWN,
    PEOPLE_ROLES,
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
# Trailing punctuation, except the period of "Jr." / "Sr." (MC wrote "Alfred F. Kelly, Jr.").
_NAME_END = re.compile(r"(?<!\b[JS]r)(?<!\b[JS]R)[\s,;.]+$|\s+$")
_NAME_TRAILING_MARKS = re.compile(r"[\s*\u2013\u2014\-†‡^§¶#]+$")
# Yahoo writes "Mr. Timothy D. Cook": the honorific is not part of the name
# (599 People rows carried one, data-team review 2026-11-13).
_NAME_HONORIFIC_LEAD = re.compile(r"^(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Sir|Dame)\.?\s+(?=\S)", re.I)

# Title words the extractor glues AFTER a name: "Brian T. Olsavsky SVP and",
# "Franck J. Moison Retired", "Martin P. Waters CEO/President". Each spelling
# became its own grid row next to the clean one.
_NAME_TITLE_WORDS = {
    "svp", "sevp", "evp", "vp", "and", "&", "ceo", "cfo", "coo", "president",
    "retired", "former", "division", "enterprise", "sector", "corporate",
    "special", "strategic", "technical", "advisor", "founder", "chief",
    "officer", "executive", "chairman", "group", "commerce",
    # Title fragments stored as names ("Senior Vice", "CHAIR OF THE BOARD",
    # "General Manager, RBIS"), each a copy of a real executive's pay row
    # (data-team review 2026-11-13).
    "senior", "vice", "general", "counsel", "manager", "chair", "board", "secretary",
    "treasurer", "controller", "operations", "of", "the",
}
# Never part of WHO someone is. Yahoo prefixes every officer with an honorific,
# so first-token keys read "Mr." as the first name and merged Bernard Arnault
# with his three sons ('mr|arnault').
_NAME_HONORIFICS = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame"}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "ph", "md", "cpa", "mba", "esq"}


def _is_title_token(token: str) -> bool:
    word = token.lower().strip(".,*()\u2013\u2014-")
    return not word or all(part in _NAME_TITLE_WORDS for part in word.split("/") if part)


@lru_cache(maxsize=200_000)
def clean_person_name(name):
    """Strip extraction junk from an executive name.

    The same person otherwise appears several times under spellings that differ
    only by a footnote marker or an invisible character, and each one became its
    own grid row.
    """
    text = _NAME_HONORIFIC_LEAD.sub("", _NAME_ZERO_WIDTH.sub("", (name or "")).strip())
    # "Julie A. D’ Emilio" and "Julie A. D’Emilio" are one person (two grid rows
    # with one pay history, review 2026-11-13).
    text = re.sub(r"\b([DO])['’]\s+(?=[A-Z])", r"\1’", text)
    text = re.sub(r"\b([A-Z])\s+\.(?=\s)", r"\1.", text)         # "A . Christina Hennington"
    # Form 3 "Leung Tak Chung Mr" reversed to "Tak Chung Mr Leung"; "(Nmn)" = no middle name.
    text = re.sub(r"\s(?:Mr|Mrs|Ms|Dr)\.?(?=\s)|\s\(?Nmn\)?(?=\s|$)", "", text, flags=re.I)
    text = re.sub(r",\s*age\b.*$", "", text, flags=re.I)              # "Steven B. Shiffman, age"
    text = re.sub(r"([“\"])\s+(?=[^“”\"]+[”\"])", r"\1", text)        # 'John “ Hil” Davis'
    first = text.split(" ", 1)[0]
    if len(first) > 2 and text.count(first + " ") > 1:            # "Coolidge E. Rhodes, Jr. Coolidge E"
        once = text[:text.index(first + " ", 1)].strip()
        text = once if len(once.split()) >= 2 else text
    # Twice: "Galanti 8 9" style double markers do occur.
    for _ in range(2):
        stripped = _NAME_FOOTNOTE.sub("", text).strip()
        if stripped == text or not stripped:
            break
        text = stripped
    text = _NAME_END.sub("", re.sub(r"\s+", " ", text)).lstrip(" ,;.")
    tokens = text.split(" ")
    # A "name" made only of title words ("Technical Advisor") is left alone —
    # emptying it would merge unrelated rows.
    if not all(_is_title_token(t) for t in tokens):
        while len(tokens) > 1 and _is_title_token(tokens[-1]):
            tokens.pop()
    # A footnote can sit before the title word ("Todd Underwood 2 Corporate"),
    # so it is only exposed once the title word is gone.
    text = _NAME_FOOTNOTE.sub("", " ".join(tokens)).strip()
    return _NAME_END.sub("", _NAME_TRAILING_MARKS.sub("", text)).lstrip(" ,;.")


# Business-unit labels the proxy extractor read as a name ("Consumer Brands",
# "Anthropologie Group", "Abercrombie & Fitch/ abercrombie kids").
# "Famous Footwear", "Label and Graphic", "Wholesale and Dist. Ops", "and
# Accounting", "Jr.** Douglas" (proxy title lines, 2026-10-16).
_NOT_PERSON_WORDS = {"america", "americas", "sales", "development", "strategy", "care", "engineering",
                     "brand", "fintech", "financial", "principal", "technologies", "director",
                     "inc", "corp", "llc", "ltd", "co-founder", "founder", "region", "business",
                     "brands", "group", "systems", "kids", "stores", "division", "segment",
                     "consumer", "international", "retail", "corporate", "markets", "services",
                     "coatings", "projects", "footwear", "graphic", "wholesale", "ops", "dist",
                     "accounting", "materials", "operations", "solutions", "products", "and"}
_NAME_SUFFIX_FIRST = re.compile(r"^(?:jr|sr|ii|iii|iv)\b", re.I)


def is_person_name(name: str) -> bool:
    """False for a cleaned "name" that is a business unit, not a person."""
    tokens = (name or "").split()
    return (len(tokens) >= 2 and not any(c in name for c in "&/*—()")
            and any(len(re.sub(r"[^A-Za-z]", "", t)) >= 2 for t in tokens)      # not "G. A"
            and not any(_is_title_token(t) for t in tokens)
            and not _NAME_SUFFIX_FIRST.match(name)
            and not any(t.lower().strip(".,") in _NOT_PERSON_WORDS for t in tokens))


@lru_cache(maxsize=200_000)
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
                          for t in re.split(r"[\s.]+", clean_person_name(name).lower().replace("’", "'")))
              if len(t) > 1 and t not in _NAME_HONORIFICS and t not in _NAME_SUFFIXES]
    return "|".join(tokens)


def person_id(name) -> str:
    """Stable ID for one person across the four People exports and their
    spellings: "Anthony DiSilvestro" and "Anthony P. DiSilvestro" share one.
    # ponytail: name-based; two different people with the same full name share
    # an ID. Add the company to the key if that ever matters more than movers.
    """
    key = person_key(name)
    return "P" + hashlib.sha1(key.encode()).hexdigest()[:8].upper() if key else ""


def _load_ticker_aliases() -> Dict[str, str]:
    path = os.path.join(os.path.dirname(__file__), "ticker_aliases.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


# Only what the database cannot tell (no SEC CIK, or a ticker no table holds).
# Everything else is derived from the DB (refresh_ticker_aliases).
TICKER_ALIAS_OVERRIDES = _load_ticker_aliases()
# The live map: overrides + the DB-derived pairs. Mutated in place on refresh so
# every module holding a reference sees the new pairs.
TICKER_ALIASES: Dict[str, str] = dict(TICKER_ALIAS_OVERRIDES)
_ALIASES_LOADED_AT = 0.0
ALIAS_REFRESH_SECONDS = 6 * 3600

_NAME_NOISE = re.compile(
    r"\b(?:the|inc|incorporated|corp|corporation|co|company|companies|ltd|limited|plc|llc|lp|holdings?|"
    r"group|sa|ag|nv|se|spa|class [a-z]|series [a-z]|adr|ads|common stock|ordinary shares|new|de)\b")


def company_name_key(name) -> str:
    """"Victoria's Secret & Co." and "VICTORIA'S SECRET" -> "victoria's secret";
    "Block, Inc. (Square)" -> "block"; "Brown-Forman Corp." -> "brown forman"."""
    text = re.sub(r"\(.*?\)|/[a-z]{2,4}/", " ", str(name or "").lower().replace("’", "'"))
    text = _NAME_NOISE.sub(" ", re.sub(r"[^a-z0-9' ]+", " ", text))
    return re.sub(r"\s+", " ", text).strip()


# A line that is never the company's main share: rights, units, warrants,
# preferreds (AACBR, AACBU, ABLVW, AAM-WT, ABR-PD).
_SECONDARY_LINE = re.compile(r"^[A-Z]{4}[UWRZ]$|^[A-Z]{3,4}WW$|-(?:WT?|WS|UN?|RT?|P[A-Z]?)$")


def derive_ticker_aliases(listings: List[dict], coverage: Dict[str, str],
                          activity: Dict[str, int], listed: Optional[Dict[str, tuple]] = None) -> Dict[str, str]:
    """ticker -> the company's one ticker, for tickers that are the same issuer.

    `listings`: {ticker, cik, name} rows from every company master. Two tickers
    are one company when they share a CIK AND a company name (normalised): CCZ
    and CMCSA ("COMCAST CORP"), VSCO and VSXY ("Victoria's Secret"). A shared
    CIK with different names is a data error, never merged (CRI Carter's / GIL
    Gildan, FTI / NOMD). `coverage`: ticker -> listing_status for the Coresight
    list; `listed`: ticker -> (active, market cap) from the Alpha Vantage
    listing. The one ticker is the Coresight one (Public first); else not a
    rights/unit/warrant/preferred line; else the active line with the largest
    market cap (CMCSA $106bn over CCZ, AFG over the AFGC debenture); else the
    most events; else the plainest symbol.
    """
    listed = listed or {}
    by_cik: Dict[str, Dict[str, set]] = {}
    for row in listings:
        cik = str(row.get("cik") or "").strip()
        ticker = str(row.get("ticker") or "").strip()
        if not cik.isdigit() or not ticker:
            continue
        names = by_cik.setdefault(str(int(cik)), {}).setdefault(ticker, set())
        key = company_name_key(row.get("name"))
        if key:
            names.add(key)
    aliases: Dict[str, str] = {}
    for tickers in by_cik.values():
        if len(tickers) < 2:
            continue
        # Groups of tickers that share a name inside this CIK.
        groups: List[set] = []
        for ticker, names in tickers.items():
            joined = [g for g in groups if any(tickers[t] & names for t in g)]
            merged = {ticker}.union(*joined) if joined else {ticker}
            groups = [g for g in groups if g not in joined] + [merged]
        for group in groups:
            if len(group) < 2:
                continue
            best = sorted(group, key=lambda t: (
                0 if coverage.get(t) == "Public" else 1 if t in coverage else 2,
                bool(_SECONDARY_LINE.search(t)),
                not listed.get(t, (False, 0))[0], -(listed.get(t, (False, 0))[1] or 0),
                -activity.get(t, 0), bool(re.search(r"[^A-Z0-9]", t)), len(t), t))[0]
            aliases.update({t: best for t in group if t != best})
    return aliases


def refresh_ticker_aliases(force: bool = False) -> Dict[str, str]:
    """Re-derive the alias map from the database (at most every 6 h unless
    forced). A new company listed under two tickers is folded on the next
    refresh, without anyone editing a list. A failed read keeps the last map."""
    global _ALIASES_LOADED_AT
    import time as _time
    if not force and _time.time() - _ALIASES_LOADED_AT < ALIAS_REFRESH_SECONDS:
        return TICKER_ALIASES
    try:
        listings = db_manager.execute_query_readonly_raising(
            "SELECT ticker, cik, name FROM coreiq_sec_companies_all", {}) or []
        covered = db_manager.execute_query_readonly_raising(
            "SELECT ticker, cik, name, name_coresight, listing_status FROM coreiq_companies", {}) or []
        listings = list(listings) + [dict(r, name=n) for r in covered
                                     for n in (r.get("name"), r.get("name_coresight")) if n]
        coverage = {str(r["ticker"]): str(r.get("listing_status") or "") for r in covered}
        # Event counts only for tickers that share a CIK (a few dozen).
        shared = {}
        for r in listings:
            if str(r.get("cik") or "").strip().isdigit():
                shared.setdefault(str(int(str(r["cik"]).strip())), set()).add(str(r["ticker"]))
        candidates = sorted({t for ts in shared.values() if len(ts) > 1 for t in ts})
        activity, listed = {}, {}
        if candidates:
            marks = ", ".join(f":t{i}" for i in range(len(candidates)))
            params = {f"t{i}": t for i, t in enumerate(candidates)}
            for r in db_manager.execute_query_readonly_raising(
                    f"SELECT ticker, COUNT(*) AS n FROM coreiq_company_events WHERE ticker IN ({marks}) GROUP BY ticker",
                    params) or []:
                activity[str(r["ticker"])] = int(r["n"])
            for r in db_manager.execute_query_readonly_raising(
                    f"SELECT symbol, listing_status, market_capitalization FROM coreiq_av_companies_all "
                    f"WHERE symbol IN ({marks})", params) or []:
                listed[str(r["symbol"])] = (str(r.get("listing_status") or "") == "Active",
                                            _to_float(r.get("market_capitalization")) or 0)
        derived = derive_ticker_aliases(listings, coverage, activity, listed)
    except Exception as exc:
        log_structured_error(exc, page="people_service", component="refresh_ticker_aliases",
                             operation="derive_ticker_aliases")
        return TICKER_ALIASES
    fresh = {**derived, **TICKER_ALIAS_OVERRIDES}
    added = sorted(set(fresh) - set(TICKER_ALIASES))
    first = _ALIASES_LOADED_AT == 0.0
    TICKER_ALIASES.clear()
    TICKER_ALIASES.update(fresh)
    _ALIASES_LOADED_AT = _time.time()
    if added and not first:
        # A company newly found under a second ticker: folded from now on.
        log_warning(f"[PEOPLE_TICKER_ALIASES] new: {', '.join(f'{t}->{fresh[t]}' for t in added[:20])}")
    log_timing("PEOPLE_TICKER_ALIASES", 0, details=f"pairs={len(fresh)} new={0 if first else len(added)}")
    return TICKER_ALIASES


def canonical_ticker(ticker) -> str:
    text = str(ticker or "").strip()
    return TICKER_ALIASES.get(text, text)


def canonical_tickers(df: pd.DataFrame, columns=("ticker",)) -> pd.DataFrame:
    """Fold alias tickers on read: a frame materialized before a pair was found
    still shows one ticker per company."""
    hit = [c for c in columns if c in df.columns
           and df[c].astype(str).isin(TICKER_ALIASES.keys()).any()]
    if not hit:
        return df
    df = df.copy()
    for col in hit:
        was_category = isinstance(df[col].dtype, pd.CategoricalDtype)
        df[col] = df[col].astype(str).map(canonical_ticker)
        if was_category:
            df[col] = df[col].astype("category")
    return df


_CORP_WORD = re.compile(r"\b(?:Inc|Incorporated|Corp|Corporation|Co|Company|Companies|Ltd|Limited|plc|"
                        r"LLC|LP|SA|AG|NV|SE|SpA|Group|Holdings?|Brands|Restaurants|Bancorp|"
                        r"Soci[ée]t[ée]|S\.?A\.?P\.?I|C\.?V)\b", re.I)
_KEEP_CAPS = {"AG", "SA", "NV", "SE", "LP", "LLC", "USA", "US", "UK", "ADR", "II", "III", "IV",
              "AT&T", "CVS", "IBM", "HP", "GE", "JD", "BP", "H&M", "TJX", "PVH", "3M", "ODP", "AMC", "NCR"}
_CAPS_AS = {"JPMORGAN": "JPMorgan", "PLC": "plc", "LTD": "Ltd", "MCDONALD'S": "McDonald's"}


# Names that are styled in capitals on purpose.
_STYLED_CAPS = {"JAKKS", "SYNNEX", "NVIDIA", "ASICS", "IKEA", "LEGO", "KAYAK", "ULTA", "GEICO", "YETI"}


def smart_title(text: str) -> str:
    """"SIGNET JEWELERS LTD" -> "Signet Jewelers Ltd"; "CHEVRON Corp." -> "Chevron
    Corp."; mixed-case names untouched."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters or " " not in text.strip():
        # One word is a listing code ("CLIFFORDML", HK 3686): it goes with the
        # ticker and is shown as filed (user, 2026-11-13).
        return text or ""
    if sum(c.isupper() for c in letters) < 0.8 * len(letters):
        # Mostly mixed case: only a shouted long word is fixed.
        return re.sub(r"\b[A-Z]{5,}\b", lambda m: m.group(0) if m.group(0) in _STYLED_CAPS
                      else m.group(0).capitalize(), text)

    def word(w):
        core = w.strip(".,")
        if core in _CAPS_AS:
            return w.replace(core, _CAPS_AS[core])
        if core in _KEEP_CAPS or core in _STYLED_CAPS \
                or (len(core) <= 3 and core.isalpha() and not re.search(r"[AEIOUY]", core)) \
                or re.fullmatch(r"(?:[A-Z]\.)+[A-Z]?\.?", core + "."):
            return w
        return "-".join(re.sub(r"^Mc(\w)", lambda m: "Mc" + m.group(1).upper(), p.capitalize())
                        for p in w.split("-"))
    return " ".join(word(w) for w in text.split())


def company_display_name(coresight: str, legal: str = "", ticker: str = "") -> str:
    """The company's name, not a brand list, a former name or a ticker.

    Coresight's own label wins, except (data-team review 2026-11-13):
      * a list of brands ("KFC, Pizza Hut, Taco Bell" -> "Yum! Brands, Inc.");
      * a former name the legal name records in brackets ("Square" ->
        "Block, Inc." from "Block, Inc. (Square)");
      * a ticker-like code ("QRTEA", "CLIFFORDML") when a real name exists;
    and an all-caps name is put in title case ("ELI LILLY & Co.").
    """
    coresight, legal = (coresight or "").strip(), re.sub(r"\s*/[A-Z]{2,4}/\s*$|\s*/\s*ADR$", "", (legal or "").strip())
    former = re.match(rf"^(.+?)\s*\(\s*{re.escape(coresight)}\s*\)$", legal) if coresight else None
    if former:
        return former.group(1).strip()
    brand_list = "," in coresight and not _CORP_WORD.search(coresight)
    code_like = bool(coresight) and (coresight == ticker or (" " not in coresight and coresight.isupper()
                                                            and len(coresight) > 4))
    name = legal if legal and (not coresight or brand_list or code_like) else coresight or legal
    return smart_title(name)


# Junk the proxy extractor glues onto a position (data-team review 2026-11-13):
# footnote prose, director-bio fields, years, footnote marks, dated asides.
_TITLE_FOOTNOTE_PROSE = re.compile(
    r"\s+(?:Aggregate\s+grant|The\s+amounts?\b|Amounts?\s+(?:shown|reported|represent|in\s+this)|"
    r"Represents\b|Reflects\b|Consists\s+of|See\s+(?:Note|the\s+section)|In\s+accordance\b|"
    r"(?:Mr|Ms|Mrs|Dr)\.\s+[A-Z][a-z]).*$", re.S)
_TITLE_BIO = re.compile(r"\b(?:Age\s*:?\s*\d{2}\b|Director\s+since\s*:?\s*(?:19|20)\d{2}|"
                        r"(?:Committees?|Race/Ethnicity|Gender|Tenure)\s*:\s*\S+)", re.I)
_TITLE_DATED_ASIDE = re.compile(r"\s*\([^()]*\b(?:19|20)\d{2}\b[^()]*\)"
                                r"|,?\s+(?:as\s+of|until|through|effective|since|from)\s+"
                                r"(?:[A-Z][a-z]+\.?\s+\d{1,2},?\s+)?(?:19|20)\d{2}\b")
_TITLE_YEARS = re.compile(r"(?:\s+(?:19|20)\d{2})+(?=\s*$)")
_TITLE_MARKS = re.compile(r"\s*(?:\*+|\(\s*\d{1,2}\s*\)|\[\s*\d{1,2}\s*\])|\s+\d{1,2}\s*$")
_TITLE_LEAD_JUNK = re.compile(r"^(?:(?:Jr|Sr|II|III|IV)\.?\**\s+|\d{2}\s+(?=[A-Z]))")
_TITLE_SEC_WORDING = re.compile(
    r"[,;]?\s*(?:and\s+)?(?:as\s+)?(?:the\s+)?(?:Company[’']s\s+)?(?:interim\s+)?"
    r"principal\s+(?:executive|financial|accounting|operating)\s+officer\b", re.I)
_TITLE_DANGLING = re.compile(r"[\s,;&/–—-]+(?:and|of|the)?\s*$|\s+(?:and|of|the)\s*$")


_TITLE_START_WORD = r"(?:Former|Interim|Executive|Senior|Chief|President|Vice|EVP|SVP|Group|Global|General|Chair\w*|Head)"
# "… U.S. Stores Carol B. Tomé Former Chief Financial Officer": a second person
# (first name, initial, surname) run on before their own title.
_TITLE_SECOND_PERSON = re.compile(
    rf"\s+[A-Z][a-z]+\s+[A-Z]\.\s+[A-Z][\w'’À-ſ\-]+(?:,?\s+(?:Jr|Sr|II|III)\.?)?\s+(?={_TITLE_START_WORD}\b)"
    # IBM: "… Chief Commercial Officer G. Cohn , Vice Chairman A. Robinson , …"
    rf"|\s+[A-Z]\.(?:\s?[A-Z]\.)?\s+[A-Z][\w'’À-ſ\-]+\s+,\s*(?={_TITLE_START_WORD}\b)")
# A performance goal read as a title: "Drive product innovation and deliver …".
_TITLE_GOAL = re.compile(r"^(?:Drive|Deliver|Complete|Improve|Lead|Grow|Achieve|Execute|Develop|Expand|"
                         r"Continue|Strengthen|Build|Advance|Accelerate|Support)\b", re.I)
_TITLE_ABBREVIATIONS = [(r"\bEVP\b", "executive vice president"), (r"\bSVP\b", "senior vice president"),
                        (r"\bVP\b", "vice president"), (r"\bCEO\b", "chief executive officer"),
                        (r"\bCFO\b", "chief financial officer"), (r"\bCOO\b", "chief operating officer"),
                        (r"&", "and"), (r"\bthe Board\b", ""), (r"\bof the\b", "of")]


# Rank words every executive title shares: "EVP, CFO" and "EVP, Stores" are
# two titles even though most of their words match.
_TITLE_RANK_WORDS = {"and", "of", "our", "the", "executive", "vice", "senior", "chief", "officer", "group", "global"}


def _title_words(text: str) -> set:
    for pattern, full in _TITLE_ABBREVIATIONS:
        text = re.sub(pattern, full, text)
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in _TITLE_RANK_WORDS}


def _one_wording(text: str) -> str:
    """"Executive Vice President & Chief Administrative Officer Executive Vice
    President and Chief Administrative Officer" -> the first wording: later
    proxies' spellings of the same title were run together."""
    # Only after a complete title ("... Officer", "... (COO)"): "EVP and" + "Chief
    # Operating Officer" is one title, not two wordings.
    starts = [m.start(1) for m in re.finditer(
        # Any word but a joining one may end the first wording: "… Product and Design EVP, AI …" (WMT).
        rf"(?:Officer|President|Director|Counsel|Secretary|Treasurer|Controller|Chair\w*|CEO|CFO|COO|CAO|\)|”|\"|"
        rf"\b[A-Za-z]{{3,}}(?<!\band)(?<!\bthe)(?<!\bfor))"
        rf"(\s+)(?={_TITLE_START_WORD}\b|our\s)", text)]
    for at in starts:
        head, tail = text[:at].strip(" ,;"), text[at:].strip(" ,;")
        a, b = _title_words(head), _title_words(tail)
        if a and b and len(a & b) >= 0.75 * min(len(a), len(b)) and len(a & b) >= 0.5 * max(len(a), len(b)):
            return head if len(head) >= len(tail) else _one_wording(tail)
    return text


@lru_cache(maxsize=200_000)
def clean_title(title) -> str:
    """A position as a title: no footnote prose, director-bio fields ("Age: 60",
    "Director Since: 2023"), year runs ("President, Brands 2024 2023"), footnote
    marks, dated asides ("(through Sept. 10, 2023)") or SEC designations
    ("principal accounting officer") next to a real title."""
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return ""
    if _TITLE_GOAL.match(text):
        start = re.search(rf"\b{_TITLE_START_WORD}\b", text)
        text = text[start.start():] if start else ""
    text = re.split(r"\s*_{3,}", text, 1)[0]
    text = _TITLE_SECOND_PERSON.split(text, 1)[0]
    text = _TITLE_FOOTNOTE_PROSE.sub("", text)
    text = _TITLE_BIO.sub(" ", text)
    text = _TITLE_DATED_ASIDE.sub("", text)
    text = _TITLE_MARKS.sub("", _TITLE_YEARS.sub("", text))
    text = _TITLE_LEAD_JUNK.sub("", re.sub(r"\s+", " ", text).strip())
    without = _TITLE_SEC_WORDING.sub("", text).strip(" ,;")
    if re.search(r"[A-Za-z]{2,}", without):
        text = without
    elif text:
        # The designation is the only title on file: say it as a title.
        text = re.sub(r"^(?:the\s+)?(?:Company[’']s\s+)?", "", text, flags=re.I).title()
    text = _one_wording(text)
    if len(text) > 150:
        # ponytail: an unrecognised footnote; keep its first clause only.
        text = re.split(r"\.\s", text, 1)[0][:150].rsplit(" ", 1)[0]
    previous = None
    while previous != text:
        previous, text = text, _TITLE_DANGLING.sub("", text).strip()
    return text


def raw_lead(raw_row: str) -> str:
    """The filed row's text before its first year: whose row it is."""
    return re.split(r"\s(?:19|20)\d\d\b", " " + (raw_row or ""), 1)[0].strip()


def row_owner(name: str, position: str, raw_row: str) -> tuple:
    """(name, position) of the executive a stored pay row belongs to.

    The data team's extractor sometimes files a Summary Compensation Table row
    under the PREVIOUS executive: Lowe's "Brandon J. Sink" rows carry Joseph M.
    McFarland III's figures, and often the position runs both titles together.
    The filed row text (raw_row) starts with whose row it is, so a row whose
    text starts with someone else's name is theirs — the source of the "same
    pay history" pairs (Sink/McFarland, Sankaran/McCollam, Ford/McCabe;
    data-team review 2026-11-13).
    """
    lead = raw_lead(raw_row)
    own = person_key(name).split("|")
    if not lead or (own[-1] and own[-1] in lead.lower()):
        return name, position
    words = lead.split()
    for k in range(min(6, len(words)), 1, -1):
        candidate = " ".join(words[:k]).strip(" ,")
        if any(_is_title_token(w) for w in words[:k]):
            continue
        cleaned = clean_person_name(candidate)
        if is_person_name(cleaned) and person_key(cleaned).split("|")[-1] not in own:
            rest = " ".join(words[k:]).strip(" ,;")
            title = position.split(candidate, 1)[1].strip(" ,;") if candidate in position else rest
            return cleaned, title
    return name, position


def _drop_copied_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Drop a row whose stored position names a colleague AND whose pay equals
    that colleague's pay for the same year: it is a copy of their row filed
    under the wrong executive, in a restated year whose row text names no one
    (Target 2018-19: "John J. Mulligan" / "... Michael E. McNamara EVP & CIO"
    carrying McNamara's totals). The executive's own row for that year, from
    another proxy, is kept."""
    total = pd.to_numeric(df["total_compensation"], errors="coerce").round(0)
    keys = df["executive_name"].map(person_key)
    pay = {}
    for t, k, y, v in zip(df["ticker"].astype(str), keys, df["year"], total):
        if pd.notna(v):
            pay.setdefault((t, k, y), set()).add(v)
    # Named by first and last name: "Coolidge E. Rhodes, Jr" in a position is
    # the "Coolidge E. Rhodes, Jr." on file.
    people = df.groupby(df["ticker"].astype(str), observed=True)["executive_name"].agg(
        lambda s: {person_key(n) for n in s if len(person_key(n).split("|")) >= 2})
    drop = []
    for i, t, k, y, v, pos in zip(df.index, df["ticker"].astype(str), keys, df["year"], total,
                                  df["raw_position"].fillna("")):
        if pd.isna(v) or not pos:
            continue
        words = set(re.findall(r"[\w'’-]+", pos.lower().replace("’", "'")))
        if any(ok != k and {ok.split("|")[0], ok.split("|")[-1]} <= words and v in pay.get((t, ok, y), ())
               for ok in people.get(t, ())):
            drop.append(i)
    return df.drop(index=drop)


def _owner_by_title(df: pd.DataFrame) -> pd.DataFrame:
    """Rows whose filed text starts with ANOTHER executive's title, not a name:
    Victoria's Secret 2024 "Scott Sekella" / "Chief Financial and Operating
    Officer Chief Human Resources and Legal Officer" with the row text "Chief
    Human Resources and Legal Officer 2024 746,154 ..." is Melinda McAfee's
    row. Reassigned when the title ends the run-together position and exactly
    one other executive of that company holds it."""
    raw = df["raw_lead"].fillna("")
    lead = raw.map(clean_title)
    # By person, not spelling: "Joseph McFarland III" and "Joseph M. McFarland
    # III" are one holder (the fullest spelling is used).
    holders: Dict[tuple, Dict[str, str]] = {}
    for t, n, title in zip(df["ticker"].astype(str), df["executive_name"], df["title"]):
        same = holders.setdefault((t, title.lower()), {})
        key = person_key(n)
        if len(n) > len(same.get(key, "")):
            same[key] = n
    df = df.copy()
    for i in df.index[lead.ne("")]:
        position, own = str(df.at[i, "raw_position"] or ""), df.at[i, "executive_name"]
        # The run-together position (before clean_title tidies it) ends with the title.
        if position.lower() == raw[i].lower() or not position.lower().endswith(" " + raw[i].lower()):
            continue
        others = {k: n for k, n in holders.get((str(df.at[i, "ticker"]), lead[i].lower()), {}).items()
                  if k != person_key(own)}
        if len(others) == 1:
            df.at[i, "executive_name"], df.at[i, "title"] = next(iter(others.values())), lead[i]
            df.at[i, "role"], df.at[i, "is_former"] = classify_role(lead[i]), is_former_title(lead[i])
    return df


@lru_cache(maxsize=50_000)
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


def role_bucket(roles: pd.Series) -> pd.Series:
    """Role as the Role dropdown lists it. Anything it does not list — blank,
    the retired "Unknown", a bucket added later — is Other, so selecting Other
    (or every role) never drops a row."""
    listed = set(PEOPLE_ROLES) | {PEOPLE_ROLE_BOARD}
    values = roles.astype(str)
    return values.where(values.isin(listed), PEOPLE_ROLE_OTHER)


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
    # Build-time only: the total as printed at the end of the filed row (used by
    # _repair_pay) and the row text before its year (used by _owner_by_title).
    "raw_total", "raw_lead", "raw_position",
]


_COMPANY_WORDS: Optional[Dict[str, set]] = None


def company_words() -> Dict[str, set]:
    """ticker -> the words of the company's names (coreiq_companies), so the
    company itself is never read as a person ("Urban Outfitters, Inc"). One
    query per process (~625 rows)."""
    global _COMPANY_WORDS
    if _COMPANY_WORDS is None:
        found: Dict[str, set] = {}
        try:
            for r in db_manager.execute_query_readonly(
                    "SELECT ticker, name, name_coresight FROM coreiq_companies") or []:
                found.setdefault(canonical_ticker(r.get("ticker")), set()).update(
                    re.findall(r"[a-z]+", f"{r.get('name') or ''} {r.get('name_coresight') or ''}".lower()))
        except Exception as exc:
            log_structured_error(exc, page="people_service", component="company_words",
                                 operation="fetch_company_names")
            return found
        _COMPANY_WORDS = found
    return _COMPANY_WORDS


def is_company_name(name: str, ticker: str, words: Dict[str, set]) -> bool:
    found = set(re.findall(r"[a-z]+", (name or "").lower())) - {"inc", "co", "corp"}
    return bool(found) and found <= words.get(ticker, set())


def _sec_records(rows: List[Dict[str, Any]], company_words: Optional[Dict[str, set]] = None) -> List[dict]:
    """Map SEC proxy rows onto the unified people schema.

    `company_words` (ticker -> words of the company's names) drops the company
    itself read as a person ("Urban Outfitters, Inc", 14 URBN rows copying a
    real executive's pay)."""
    company_words = company_words or {}
    out = []
    for r in rows:
        ticker = canonical_ticker(r.get("ticker"))
        raw = _NAME_ZERO_WIDTH.sub("", r.get("raw_row") or "")
        stored_position = re.sub(r"\s+", " ", _NAME_ZERO_WIDTH.sub("", r.get("position") or "")).strip()
        name, position = row_owner(clean_person_name(r.get("executive_name")), stored_position, raw)
        if not is_person_name(name):
            continue
        if is_company_name(name, ticker, company_words):
            continue
        title = clean_title(position)
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
            "raw_lead":               re.sub(r"\s+", " ", raw_lead(raw)),
            "raw_position":           stored_position,
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
        ticker = canonical_ticker(r.get("ticker"))
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
            title = clean_title(o.get("title"))
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
    return df.drop(columns=["raw_total", "raw_lead", "raw_position"], errors="ignore")


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


def _finish_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Column types of the stored frame."""
    df["is_former"] = df["is_former"].fillna(False).astype(bool)
    for col in ("year", "filing_year", "year_born", "confidence"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["age"] = pd.to_numeric(df["age"], errors="coerce").astype("Int64")
    for col in PEOPLE_MONEY_METRICS.values():
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["total_pay_local"] = pd.to_numeric(df["total_pay_local"], errors="coerce")
    for col in ("ticker", "role", "source", "filing_form", "pay_currency"):
        df[col] = df[col].astype(object).fillna("").astype("category")
    for col in ("executive_name", "title", "filing_blob"):
        df[col] = df[col].fillna("").astype(str)
    return df


_NAME_PARTICLE_END = re.compile(r"\s(?:de|da|do|dos|das|del|della|di|du|van|von|der|den|le|la|y)$")


def rejoin_cut_name(name: str, title: str):
    """("Lucas de", "Carvalho Rocha") -> ("Lucas de Carvalho Rocha", ""): a name
    cell wrapped onto a second line that the parser read as the title (Unifi)."""
    words = title.split()
    if (_NAME_PARTICLE_END.search(name) and 1 <= len(words) <= 3 and all(w[:1].isupper() for w in words)
            and not any(_is_title_token(w) for w in words)):
        return f"{name} {title}", ""
    return name, title


def _proxy_records(proxy: pd.DataFrame, words: Optional[Dict[str, set]] = None) -> List[dict]:
    """Map proxy_pay rows (edgartools-read Summary Compensation Tables) onto the
    unified people schema. They are SEC proxies like coreiq_executives_compensation."""
    words = words or {}
    out = []
    # One vectorised date parse: per-row pd.to_datetime was a third of this
    # function on 46k rows.
    filed_years = pd.to_datetime(proxy.get("filing_date"), errors="coerce").dt.year if len(proxy) else []
    for r, filed_year in zip(proxy.to_dict("records"), filed_years):
        name, title = rejoin_cut_name(clean_person_name(r.get("executive_name")), clean_title(r.get("title")))
        if not is_person_name(name) or is_company_name(name, canonical_ticker(r.get("ticker")), words):
            continue
        out.append({
            **{col: r.get(col) for col in ("salary", "bonus", "stock_awards", "option_awards",
                                           "non_equity_incentive", "all_other_compensation",
                                           "total_compensation")},
            "ticker":          canonical_ticker(r.get("ticker")),
            "executive_name":  name,
            "title":           title,
            "role":            classify_role(title),
            "is_former":       is_former_title(title),
            "year":            _to_int(r.get("year")),
            "pay_currency":    "",
            "source":          "SEC",
            "filing_form":     "DEF 14A",
            "filing_year":     None if pd.isna(filed_year) else int(filed_year),
            "filing_blob":     r.get("url") or "",
        })
    if out:
        names = one_spelling(pd.DataFrame(out, columns=["ticker", "executive_name"]))["executive_name"]
        for rec, name in zip(out, names):
            rec["executive_name"] = name
    return out


def one_spelling(df: pd.DataFrame) -> pd.DataFrame:
    """One spelling per person at a company. A word split at an inline tag in
    some proxies ("Benno Dor er", "I qbal Arshad", "Steven E. Five l") and not in
    others, in our parse and in the data team's table: names equal without
    spaces are one name, and the spelling with the fewest spaces wins."""
    if df.empty:
        return df
    names = df["executive_name"].astype(str)
    keys = list(zip(df["ticker"].astype(str), names.str.lower().str.replace(r"\W", "", regex=True)))
    best: Dict[tuple, str] = {}
    for key, name in zip(keys, names):
        if key not in best or name.count(" ") < best[key].count(" "):
            best[key] = name
    return df.assign(executive_name=[best[k] for k in keys])


NAMED_EXECUTIVE = "Named Executive Officer"


def fill_missing_titles(df: pd.DataFrame, form3: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """A blank title (289 proxy rows, 2026-10-16) takes, in order: the same
    person's title in another year at that company; their Form 3 officer title
    (or "Director"); else "Named Executive Officer" — a proxy's pay table lists
    only named executive officers, so that much is always true."""
    blank = df["title"].fillna("").str.strip().eq("")
    if not blank.any():
        return df
    if form3 is None:
        from data.form3_appointments import read_form3
        form3 = read_form3()
    keys = list(zip(df["ticker"].astype(str), df["executive_name"].map(person_key)))
    known = {}
    for k, t in sorted(zip(keys, df["title"].fillna("")), key=lambda kt: kt[1] == ""):
        if t.strip():
            known.setdefault(k, t)
    if form3 is not None and len(form3):
        for t, p, title in zip(form3["ticker"].astype(str), form3["person"].map(person_key),
                               form3["title"].where(form3["title"].astype(bool), "Director")):
            known.setdefault((t, p), title)
    df = df.copy()
    filled = [known.get(k, NAMED_EXECUTIVE) for k, b in zip(keys, blank) if b]
    df.loc[blank, "title"] = filled
    roles = df["role"].astype(object)
    roles.loc[blank] = [classify_role(t) for t in filled]
    df["role"] = roles.astype("category")
    return df


_TITLE_CUT_SHORT = re.compile(
    r"\b(?:Chief|Executive|Senior|Vice|Global|Group|Principal|Deputy|Assistant|Interim)$", re.I)


def _names_in_title(df: pd.DataFrame) -> List[Optional[str]]:
    """Per row, the first OTHER executive of the same company whose full name
    appears in the title — two positions run together. None when there is none."""
    names = df.groupby(df["ticker"].astype(str), observed=True)["executive_name"].agg(
        lambda s: {n for n in s if len(str(n).split()) >= 2})
    out = []
    for t, n, title in zip(df["ticker"].astype(str), df["executive_name"], df["title"].fillna("")):
        hits = [(title.find(o), o) for o in names.get(t, ()) if o != n and o in title]
        out.append(min(hits)[1] if hits else None)
    return out


def tidy_titles(df: pd.DataFrame) -> pd.DataFrame:
    """Title repairs that need the whole company or person (on read, after
    every source is in):
      * another executive's name inside a title is where a second position was
        run on ("Chief Executive Officer Patrice de Talhouët, Chief Financial
        Officer" on Camillo Pane) — the title is cut there;
      * a title cut short ("President and Chief", "Executive") takes the same
        person's fuller title from another year; a lone word is left blank for
        fill_missing_titles.
    """
    if df.empty:
        return df
    titles = df["title"].fillna("").astype(str).tolist()
    for i, other in enumerate(_names_in_title(df)):
        if other:
            titles[i] = clean_title(titles[i].split(other, 1)[0])
    keys = (df["ticker"].astype(str) + "#" + df["executive_name"].map(person_key)).tolist()
    by_person: Dict[str, List[str]] = {}
    for k, t in zip(keys, titles):
        by_person.setdefault(k, []).append(t)
    for i, (k, t) in enumerate(zip(keys, titles)):
        if t and _TITLE_CUT_SHORT.search(t):
            fuller = [x for x in by_person[k] if len(x) > len(t) and x.lower().startswith(t.lower())
                      and not _TITLE_CUT_SHORT.search(x)]
            titles[i] = max(set(fuller), key=fuller.count) if fuller else ("" if " " not in t else t)
    changed = [a != b for a, b in zip(titles, df["title"].fillna("").astype(str))]
    if not any(changed):
        return df
    df = df.copy()
    df["title"] = titles
    roles = df["role"].astype(object)
    roles[changed] = [classify_role(t) for t, c in zip(titles, changed) if c]
    df["role"] = roles.astype("category")
    return df


def add_status(df: pd.DataFrame) -> pd.DataFrame:
    """`status` per person: "Former" when their latest title says so, or when
    the company's latest proxy on file no longer names them; else "Current".
    Yahoo's officer list is a current list, so a Yahoo-only person is Current
    unless the title says Former."""
    if df.empty:
        return df.assign(status=pd.Series(dtype=object))
    year = pd.to_numeric(df["year"], errors="coerce")
    key = df["ticker"].astype(str) + "#" + df["executive_name"].map(person_key)
    sec = df["source"].astype(str).str.contains("SEC")
    company_latest = year.where(sec).groupby(df["ticker"].astype(str)).transform("max")
    person_latest = year.groupby(key).transform("max")
    gone = (sec & person_latest.lt(company_latest)).groupby(key).transform("max")
    latest_former = (df["is_former"].astype(bool) & year.eq(person_latest)).groupby(key).transform("max")
    return df.assign(status=(gone | latest_former).map({True: "Former", False: "Current"}))


def _name_words(name) -> frozenset:
    """Name words of 3+ letters, any order or spelling: "Cofer, Timothy",
    "Timothy Cofer" and "TIMOTHY COFER, age" all share {timothy, cofer}."""
    return frozenset(w for w in re.findall(r"[^\W\d_]{3,}", str(name or "").lower().replace("’", "'"))
                     if w not in _NAME_HONORIFICS and w not in _NAME_SUFFIXES and w != "age")


def _fix_copied_pay(universe: pd.DataFrame, recs: pd.DataFrame) -> pd.DataFrame:
    """Where the data team's table gives an executive the SAME total as a
    colleague in a year, check that year's proxy as we read it (proxy_pay):
      * the filing gives this person a different total -> the filing's figures
        (Target 2018: Mulligan stored with McNamara's $5,356,168; filed
        $8,575,733);
      * the filing does not list this person at all but lists a colleague with
        exactly this total -> the row is a copy and is dropped (Victoria's
        Secret 2023 "Scott Sekella" with Melinda McAfee's $2,564,782; he joined
        in 2024);
      * the filing gives both the same total -> genuine equal pay (e.l.f.,
        Amazon, co-founders), left alone.
    One person is matched across spellings by shared name words ("Cofer,
    Timothy" in the filing is "Timothy Cofer" in the table). Elsewhere the two
    sources agree on 98.7% of person-years (STG, 2026-11-13); nothing else is
    touched."""
    sec = universe["source"].astype(str).eq("SEC") & universe["total_compensation"].notna()
    if not sec.any() or recs.empty:
        return universe
    total = universe["total_compensation"].round(0)
    year = pd.to_numeric(universe["year"], errors="coerce")
    tick = universe["ticker"].astype(str)
    words = universe["executive_name"].map(_name_words)
    # Who holds each (company, year, total), in the table or in the filings.
    paid: Dict[tuple, List[frozenset]] = {}
    for t, w, y, v in zip(tick, words, year, total):
        if pd.notna(v) and pd.notna(y):
            paid.setdefault((t, int(y), v), []).append(w)
    filed: Dict[tuple, List[tuple]] = {}      # (ticker, year) -> [(name words, row)]
    proxies: Dict[tuple, set] = {}            # (ticker, year) -> proxies read that cover it
    for t, n, y, row in zip(recs["ticker"].astype(str), recs["executive_name"], recs["year"],
                            recs[_PAY_PARTS + ["total_compensation", "filing_blob"]].to_dict("records")):
        if pd.notna(row["total_compensation"]) and pd.notna(y):
            paid.setdefault((t, int(y), round(row["total_compensation"])), []).append(_name_words(n))
            filed.setdefault((t, int(y)), []).append((_name_words(n), row))
            proxies.setdefault((t, int(y)), set()).add(row["filing_blob"])
    # Executives the table itself holds for that company-year: a figure our
    # parser credits to a brand ("Old Navy") proves nothing about who is missing.
    known: Dict[tuple, List[frozenset]] = {}
    for t, w, y in zip(tick, words, year):
        if pd.notna(y):
            known.setdefault((t, int(y)), []).append(w)
    universe = universe.copy()
    fixed, drop = 0, []
    for i in universe.index[sec]:
        if pd.isna(year[i]) or not words[i]:
            continue
        key = (tick[i], int(year[i]))
        if not any(w and not (w & words[i]) for w in paid.get(key + (total[i],), [])):
            continue                                  # nobody else has this figure
        mine = [row for w, row in filed.get(key, []) if w & words[i]]
        if mine:
            if all(abs(row["total_compensation"] - total[i]) > 1000 for row in mine):
                for col, value in mine[0].items():
                    universe.at[i, col] = value
                fixed += 1
        elif len(proxies.get(key, ())) >= 2 and any(
                not (w & words[i]) and abs(row["total_compensation"] - total[i]) <= 1000
                and any(w & k for k in known.get(key, []) if k != words[i])
                for w, row in filed.get(key, [])):
            # Two or more proxies cover the year, none lists this person, and a
            # colleague the table knows holds exactly this figure.
            drop.append(i)
    log_timing("PEOPLE_COPIED_PAY_FIXED", 0, details=f"rows={fixed} dropped={len(drop)}")
    return universe.drop(index=drop)


def add_proxy_pay(universe: pd.DataFrame) -> pd.DataFrame:
    """Add the proxy_pay rows for company-years coreiq_executives_compensation lacks.

    That table stays the source wherever it has a company-year (it carries ten
    years of history); proxy_pay fills the ~300 companies it does not cover and
    the newest proxies it has not reached yet.
    """
    from data.proxy_pay import read_proxy_pay

    proxy = read_proxy_pay()
    if proxy.empty:
        return universe
    recs = pd.DataFrame(_proxy_records(proxy, company_words()), columns=_FRAME_COLUMNS)
    # A parsed block that ran two executives together (proxy_pay_v2; the parser
    # is fixed in v3) names the second one in its title and carries their
    # figures (Tesla 2023: "Elon Musk" with Vaibhav Taneja's $278,000). Dropped.
    pool = pd.concat([universe[["ticker", "executive_name", "title"]],
                      recs[["ticker", "executive_name", "title"]]], ignore_index=True)
    recs = recs[[o is None for o in _names_in_title(pool)[len(universe):]]]
    universe = _fix_copied_pay(universe, recs)
    sec = universe[universe["source"].astype(str).eq("SEC")]
    years = pd.to_numeric(sec["year"], errors="coerce")
    have = set(zip(sec["ticker"].astype(str), years))
    # A company-year the table holds can still lack someone: the rows row_owner
    # hands back to their real owner leave the executive they were filed under
    # with nothing (Lowe's 2024 had no Brandon Sink). Matched on surname so a
    # nickname spelling is not added twice.
    # Any shared name word counts: "A. Christina" is Target's "A. Christina
    # Hennington" filed without her surname.
    words = {}
    for t, y, n in zip(sec["ticker"].astype(str), years, sec["executive_name"]):
        words.setdefault((t, y), set()).update(person_key(n).split("|"))
    recs = recs[[(t, y) not in have or not (set(person_key(n).split("|")) & words[(t, y)])
                 for t, y, n in zip(recs["ticker"].astype(str), recs["year"], recs["executive_name"])]]
    if recs.empty:
        return universe
    # One person, one row in the year pivot: take the spelling already on file for
    # that company ("JOANNE CREVOISERAT" in TPR's 2026 proxy is "Joanne
    # Crevoiserat" in the table), else un-shout an all-caps name.
    on_file = dict(zip(zip(sec["ticker"].astype(str), sec["executive_name"].map(person_key)),
                       sec["executive_name"]))
    recs["executive_name"] = [
        on_file.get((t, person_key(n))) or (
            re.sub(r"\bMc(\w)", lambda m: "Mc" + m.group(1).upper(), n.title())
            if n.isupper() else n)
        for t, n in zip(recs["ticker"].astype(str), recs["executive_name"])]
    recs = _collapse_restatements(recs)
    return _finish_frame(pd.concat([universe, recs], ignore_index=True))


def _build_people_universe() -> pd.DataFrame:
    """Fetch BOTH people sources whole and return the unified frame.

    Two queries, neither carrying a ticker IN list. The whole dataset is only
    ~14.5k rows, and a universal copy serves every criteria combination —
    whereas the previous per-ticker-tuple cache key re-paid 5.9s of DB time on
    every criteria edit.
    """
    import time as _time
    t0 = _time.perf_counter()
    refresh_ticker_aliases()

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

    records = _sec_records(sec_rows, company_words()) + _yf_records(yf_rows, fx)

    df = one_spelling(pd.DataFrame(records, columns=_FRAME_COLUMNS))

    if df.empty:
        log_warning("[PEOPLE] universe built EMPTY — both sources returned nothing")
        return df

    df = _collapse_restatements(_drop_copied_rows(_owner_by_title(df)))
    df = _merge_sources(df)
    df = _finish_frame(df)

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
    universe = materialized_or_build(
        # _v2: the frame's columns and dedupe changed (2026-10-07). The disk copy
        # is keyed on the source tables only, so without a new name a deploy
        # would keep serving the old snapshot.
        # _v4: business-unit "names" dropped, footnote-before-title fix (2026-10-12).
        # _v5: more title lines read as names dropped ("Famous Footwear") (2026-10-16).
        # _v6: rows handed to the executive the filed row names (row_owner), titles
        #      cleaned, honorifics dropped, one ticker per company (2026-11-13).
        # _v7: footnote marks / stray spaces in names, other people's names in
        #      titles, doubled wordings (2026-11-13, re-check of the exports).
        "people_universe_v7", _build_people_universe, sources,
        clear=lambda: get_people_universe.clear(),
    )
    # Tickers folded on read too: a second ticker found after the frame was
    # saved (refresh_ticker_aliases) still lands on the company's one ticker.
    refresh_ticker_aliases()
    universe = canonical_tickers(universe)
    if len(universe) and universe.duplicated(["ticker", "executive_name", "year", "source"]).any():
        universe = universe.drop_duplicates(["ticker", "executive_name", "year", "source"])
    # Merged on read, not stored: proxy_pay grows in the background and clears
    # this cache when it does (cache_manager._warm_people_screening).
    universe = one_spelling(add_proxy_pay(universe))
    universe = universe.drop_duplicates(["ticker", "executive_name", "year", "source"])
    return add_status(fill_missing_titles(tidy_titles(universe)))


# =============================================================================
# CRITERION APPLICATION — pure pandas, no DB, no st.*
# =============================================================================

def _apply_money_filter(df: pd.DataFrame, criterion: dict) -> pd.DataFrame:
    """Filter on one compensation metric using the shared operator vocabulary.

    Values arrive in $mm (every financial criterion in this app does) and are
    scaled by DB_SCALE, because the people tables store raw dollars.
    """
    low, high = criterion.get("money_min"), criterion.get("money_max")
    # Remuneration criteria (2026-10-15) range over every chosen metric: a row
    # passes when ANY of them is inside Min–Max. The default pair is Total
    # Compensation (SEC) and Total Pay (YFinance) — one person only ever has one
    # of the two, so "any" keeps both sources in the screen.
    ranged = criterion.get("money_metrics") or []
    if ranged and (low is not None or high is not None):
        cols = [PEOPLE_MONEY_METRICS[m] for m in ranged
                if PEOPLE_MONEY_METRICS.get(m) in df.columns]
        if not cols:
            return df
        mask = pd.Series(False, index=df.index)
        for col in cols:
            values = df[col]
            inside = values.notna()
            if low is not None:
                inside &= values >= float(low) * DB_SCALE
            if high is not None:
                inside &= values <= float(high) * DB_SCALE
            mask |= inside.fillna(False).astype(bool)
        return df[mask]

    label = criterion.get("money_metric")
    if not label:
        return df
    col = PEOPLE_MONEY_METRICS.get(label)
    if not col or col not in df.columns:
        return df

    # Min / Max (either may be blank) — the form from 2026-10-12. Saved criteria
    # from before carry an operator + value1/value2 and take the path below.
    if low is not None or high is not None:
        values = df[col]
        mask = values.notna()
        if low is not None:
            mask &= values >= float(low) * DB_SCALE
        if high is not None:
            mask &= values <= float(high) * DB_SCALE
        return df[mask.fillna(False).astype(bool)]

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
        out = out[role_bucket(out["role"]).isin(wanted)]

    title_contains = (criterion.get("title_contains") or "").strip()
    if title_contains:
        out = out[out["title"].str.contains(title_contains, case=False, na=False, regex=False)]

    name_contains = (criterion.get("name_contains") or "").strip()
    if name_contains:
        out = out[out["executive_name"].str.contains(
            name_contains, case=False, na=False, regex=False)]

    # Former executives are always in (2026-10-12): a "Former CFO" row is still
    # that person's pay for that year, and the manager wanted no toggle for it.

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


def people_criteria_for(criteria: List[dict], category: str) -> List[dict]:
    """The People criteria that screen `category`.

    A criterion saved before categories (2026-10-15) has no "category": it was a
    remuneration screen whose "Show management changes?" / "Show executive
    moves?" boxes switched the event sections on, so it still counts for those.
    """
    out = []
    for c in criteria:
        if c.get("type") != "people":
            continue
        own = c.get("category")
        if own == category:
            out.append(c)
        elif own is None and (
                category == PEOPLE_CATEGORY_REMUNERATION
                or (category == PEOPLE_CATEGORY_CHANGES and c.get("show_management_changes"))
                or (category == PEOPLE_CATEGORY_MOVES
                    and (c.get("show_moves") or c.get("show_moves_only")))):
            out.append(c)
    return out


def _build_people_events(category: str) -> pd.DataFrame:
    event_category, subtypes = PEOPLE_EVENT_CATEGORIES[category]
    names = ", ".join(f":s{i}" for i in range(len(subtypes)))
    # Raises: a timeout read as "no events" would be materialized and served.
    # The item text only for filings (~270 rows): news `situation` is a summary
    # nothing here reads.
    rows = db_manager.execute_query_readonly_raising(f"""
        SELECT ticker, event_date, event_subtype, headline, source, source_ref,
               CASE WHEN source LIKE 'SEC%%' THEN situation END AS situation
        FROM coreiq_company_events
        WHERE is_active = 1 AND event_category = :category AND event_subtype IN ({names})
    """, {"category": event_category, **{f"s{i}": v for i, v in enumerate(subtypes)}})
    df = pd.DataFrame(rows, columns=["ticker", "event_date", "event_subtype", "headline",
                                     "source", "source_ref", "situation"])
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")
    refresh_ticker_aliases()
    df["ticker"] = df["ticker"].map(canonical_ticker)
    if category == "layoffs_restructuring":
        from data.layoff_events import clean_layoff_events
        from data.screening_service import get_all_companies_universe
        wide = get_all_companies_universe()
        df = clean_layoff_events(df, dict(zip(wide["ticker"].astype(str), wide["company_name"].astype(str))))
    return df.drop(columns="situation").sort_values("event_date", ascending=False, ignore_index=True)


@st.cache_data(ttl=21600, show_spinner=False)
def get_people_events(category: str) -> pd.DataFrame:
    """Every event of one People event category (PEOPLE_EVENT_CATEGORIES), all
    tickers and dates — at most ~4,000 rows; scope and period are applied in
    memory. Materialized: the read is ~30s cold (row-bound, not index-bound)."""
    from utils.materialize import materialized_or_build
    event_category, _ = PEOPLE_EVENT_CATEGORIES[category]
    sources = [{"table": "coreiq_company_events", "signal": "MAX(event_id)",
                "where": f"event_category = '{event_category}'"}]
    # _v2: layoffs filtered, attributed and folded (data.layoff_events) (2026-10-16).
    # _v3: one ticker per company; other-company headlines, law-firm ads,
    #      roundups and commentary dropped (2026-11-13).
    # _v4: one story under several companies kept only by the one making the cut.
    events = materialized_or_build(f"people_events_{category}_v4",
                                   lambda: _build_people_events(category), sources,
                                   clear=lambda: get_people_events.clear())
    refresh_ticker_aliases()
    return canonical_tickers(events)


def period_mask(dates: pd.Series, criterion: dict, today=None) -> pd.Series:
    """True where `dates` falls inside the criterion's period.

    Same contract as the Key Devs period: a "timeframe" of N days means on or
    after today minus N days (event_date >= CURDATE() - INTERVAL N DAY); a
    "date_range" is inclusive at both ends; no days (All History) or no period
    keeps every row. Dates are calendar days — filings carry no time of day.
    """
    dates = pd.to_datetime(dates, errors="coerce").dt.normalize()
    if criterion.get("date_filter_mode") == "date_range":
        mask = pd.Series(True, index=dates.index)
        if criterion.get("start_date"):
            mask &= dates >= pd.Timestamp(str(criterion["start_date"])[:10])
        if criterion.get("end_date"):
            mask &= dates <= pd.Timestamp(str(criterion["end_date"])[:10])
        return mask.fillna(False).astype(bool)
    days = criterion.get("days")
    if days is None:
        return pd.Series(True, index=dates.index)
    # UTC, as the database's CURDATE() that Key Devs filters with: the local
    # (IST) date runs ahead and dropped a day that Key Devs still shows.
    today = pd.Timestamp(today or pd.Timestamp.now(tz="UTC").date())
    return (dates >= today - pd.Timedelta(days=int(days))).fillna(False).astype(bool)


def build_people_summary(criterion: dict) -> str:
    """One-line label for the People criterion card."""
    parts = []
    if criterion.get("category") in PEOPLE_CATEGORIES:
        parts.append(PEOPLE_CATEGORIES.get(criterion["category"], criterion["category"]))
        if criterion["category"] != PEOPLE_CATEGORY_REMUNERATION:
            if criterion.get("date_filter_mode") == "date_range":
                parts.append(f"{criterion.get('start_date')} to {criterion.get('end_date')}")
            elif criterion.get("timeframe_label"):
                parts.append(criterion["timeframe_label"])

    roles = criterion.get("roles") or []
    if roles:
        parts.append(", ".join("Director (Board)" if r == PEOPLE_ROLE_BOARD else str(r)
                               for r in roles[:4])
                     + (f" +{len(roles) - 4}" if len(roles) > 4 else ""))

    if criterion.get("title_contains"):
        parts.append(f'Title contains "{criterion["title_contains"]}"')
    if criterion.get("name_contains"):
        parts.append(f'Name contains "{criterion["name_contains"]}"')

    metric = criterion.get("money_metric") or " or ".join(criterion.get("money_metrics") or [])
    low, high = criterion.get("money_min"), criterion.get("money_max")
    if metric and (low is not None or high is not None):
        if low is not None and high is not None:
            parts.append(f"{metric} ${low:,.1f}mm – ${high:,.1f}mm")
        elif low is not None:
            parts.append(f"{metric} ≥ ${low:,.1f}mm")
        else:
            parts.append(f"{metric} ≤ ${high:,.1f}mm")
    elif metric and criterion.get("money_value1") is not None:
        operator = criterion.get("money_operator") or "Greater Than"
        v1 = criterion["money_value1"]
        if operator == "Between" and criterion.get("money_value2") is not None:
            parts.append(f"{metric} ${v1:,.1f}mm – ${criterion['money_value2']:,.1f}mm")
        else:
            symbol = OPERATOR_SQL.get(operator, operator)
            parts.append(f"{metric} {symbol} ${v1:,.1f}mm")

    shown = criterion.get("show_metrics") or []
    if shown and shown != (criterion.get("money_metrics") or []):
        parts.append("Show: " + ", ".join(str(m) for m in shown))

    years = criterion.get("years") or []
    if years:
        ys = sorted(int(y) for y in years)
        if len(ys) == 1:
            parts.append(f"FY {ys[0]}")
        elif ys == list(range(ys[0], ys[-1] + 1)):
            parts.append(f"FY {ys[0]}–{ys[-1]}")
        else:
            parts.append("FY " + ", ".join(str(y) for y in ys))

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

    if criterion.get("show_management_changes"):
        parts.append("+ Management changes")
    if criterion.get("show_moves") or criterion.get("show_moves_only"):
        parts.append("+ Executive moves")

    return " · ".join(parts) if parts else "All people"


# =============================================================================
# YEAR-COLUMN GRID — pure pandas, no DB, no st.*
# =============================================================================

def pivot_people_years(df: pd.DataFrame, metrics: List[str],
                       newest_first: bool = False) -> pd.DataFrame:
    """One row per executive x metric, one column per year.

        Executive  Title  Metric  2024  2025  2026
        A          CEO    Salary  ...   ...   ...

    `metrics` are display labels (PEOPLE_MONEY_METRICS keys); only those are
    shown. Identity columns come from the executive's LATEST year on file, so a
    title change shows the current title once instead of splitting the row.
    An executive with no value for any chosen metric keeps one row with blank
    years — the person is in scope, the pay simply was not disclosed.
    `df` may carry Company / Industry / Country of Incorporation already joined.
    Year columns run oldest -> newest (chosen years compare left to right);
    `newest_first` reverses them — with NO years chosen every year since 2013
    is a column, and oldest-first opened the grid on a wall of empty columns.
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
                         "status", "age", "year_born") if c in latest.columns]
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
        year_cols = sorted((c for c in grid.columns if str(c).isdigit()), reverse=newest_first)
        missing = identity[~identity.set_index(who).index.isin(grid.set_index(who).index)]
        grid = grid.merge(identity, on=who, how="left")
        if not missing.empty:
            grid = pd.concat([grid, missing.assign(Metric="", _order=0)], ignore_index=True)

    sort_by = (["Company"] if "Company" in grid.columns else []) + ["executive_name", "_order"]
    grid = grid.sort_values(sort_by, kind="mergesort").drop(columns="_order")
    lead = [c for c in ("Company", "ticker", "executive_name", "title", "role", "status", "Industry",
                        "Country of Incorporation", "age", "year_born") if c in grid.columns]
    return grid[lead + ["Metric"] + year_cols].reset_index(drop=True)
