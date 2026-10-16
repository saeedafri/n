"""
Layoff Events
=============
Cleans the Layoffs & Restructuring rows of coreiq_company_events for People
Screening. The news feeds tag a story to every company it mentions and to every
listed line of a company, so on 2026-10-16 the last 30 days held 209 rows for 124
"companies", most of them noise:
  * Netflix's layoffs filed under WBD, DIS and COIN; Kroger's under WMT and AMZN;
  * stock-price, analyst and debt-restructuring stories with no job or site cut
    ("Prudential Financial Inc preferred stock hits 52-week low");
  * one story five times (five outlets) and once per preferred line (TFC, TFC-P-O).

Kept: every 8-K Item 2.05 filing ("Costs Associated with Exit or Disposal
Activities"), and a news item only when its headline (a) reports a cut — jobs,
workforce, layoffs, plant / store / office closures, a restructuring plan or
charge — and (b) names the company it is filed under. Reports of one company's
cut within 3 days are one event: the filing if there is one, else the most
specific headline.

Each event gets Jobs, % of Workforce and Charges where the text states them;
an 8-K's headline is its first sentence about the action, not the item title.
"""

import re
from typing import Dict, Optional

import pandas as pd

_CUT_VERB = r"(?:cut(?:s|ting)?|slash(?:es|ing)?|eliminat\w+|trim(?:s|ming)?|axe[sd]?|reduc\w+|shed(?:s|ding)?)"
_CUT_NOUN = (r"(?:jobs?|roles|positions|workers|employees|staff|workforce|work\s+force|headcount|"
             r"locations|stores|cafes|coffeehouses)")
_SITE = (r"(?:plants?|stores?|facilit\w+|factor\w+|mills?|offices?|locations|cafes|coffeehouses|"
         r"sites?|warehouses?|distribution\s+centers?)")
_RELEVANT = re.compile(
    rf"\blay(?:s|ing)?[\s-]?offs?\b|\blaid\s+off\b|\bjob\s+(?:cuts?|losses)\b|\bWARN\b|"
    rf"\b{_CUT_VERB}\b[^.]{{0,40}}?\b{_CUT_NOUN}\b|"
    rf"\b(?:jobs?|roles|positions)\b[^.]{{0,20}}\b(?:cut|eliminated|axed)\b|"
    rf"\b(?:close[sd]?|closing|closures?|shut(?:s|ting)?\s+down|to\s+shut|idles?)\b[^.]{{0,40}}?\b{_SITE}\b|"
    rf"\b{_SITE}\b[^.]{{0,20}}\b(?:closures?|to\s+close|closes|closing)\b|"
    rf"\brestructuring\s+(?:plan|charges?|program|costs?)\b|\bheadcount\b|"
    rf"\bworkforce\s+reductions?\b|\breductions?\s+in\s+(?:force|workforce)\b|"
    rf"\bend(?:s|ing)?\s+(?:\w+\s+)?operations\s+at\b",
    re.I)
# Cuts that are not this company's job or site cuts.
_NOT_A_CUT = re.compile(r"\bavoid(?:s|ed|ing)?\s+layoffs\b|\b(?:debt|capital)\s+restructuring\b|"
                        r"restructuring\s+(?:deal|support\s+agreement)|\brecall\w*\b", re.I)

_GENERIC_WORDS = set("""
inc incorporated corp corporation co company companies group holdings holding international intl
the and of plc ltd limited sa ag nv se spa class series common stock shares new american general
united national global financial services technologies technology industries enterprises brands
systems trust bancorp bank partners lp llc ord adr ads sponsored preferred depositary
""".split())

_JOBS = re.compile(r"(?<![\d.$])(\d{1,3}(?:,\d{3})+|\d+)(?:\s+(?:more|additional|corporate|employees'?|"
                   r"\w+-based|full-time|u\.s\.))?\s+(?:jobs|positions|roles|employees|workers|staff)\b", re.I)
_SHARE = re.compile(r"(\d+(?:\.\d+)?)\s?(?:%|percent)\s+of\s+(?:its\s+|the\s+company['’]s\s+|our\s+|\w+['’]s\s+)?"
                    r"(?:current\s+|global\s+|total\s+|corporate\s+)?(?:work\s?force|employees|staff|headcount|team)", re.I)
_MONEY = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(million|billion|[mb])\b"
_CHARGE = re.compile(rf"{_MONEY}(?:\s*(?:to|-|–|and)\s*{_MONEY})?", re.I)
_ITEM_205 = re.compile(r"Item\s+2\.05\b[^A-Za-z]*(?P<heading>Costs\s+Associated\s+with\s+Exit\s+or\s+Disposal\s+"
                       r"Activities)?\.?(?P<ref>\s*(?:above|below))?", re.I)
_NEXT_ITEM = re.compile(r"\bItem\s+\d\.\d\d\b", re.I)
_ACTION = re.compile(r"reduc|workforce|positions|clos|severance|exit|restructur|realign|consolidat", re.I)


def _words(text: str) -> list:
    return re.findall(r"[A-Za-z][A-Za-z'&]*", (text or "").replace("’", "'"))


def name_words(name: str) -> set:
    """The words of a company name that identify it ("The Walt Disney Company"
    -> {"walt", "disney"}; "Bristol-Myers Squibb" -> {"bristol", "myers", "squibb"}),
    plus what headlines call it: "P&G" for Procter & Gamble, "BMW" for
    Bayerische Motoren Werke (initials of three or more words)."""
    words = [w for w in _words(name) if w.lower() not in _GENERIC_WORDS]
    found = {w.lower().removesuffix("'s") for w in words if len(w) >= 3}
    amp = re.search(r"\b([A-Za-z])\w*\s*&\s*([A-Za-z])", name or "")
    if amp:
        found.add(f"{amp.group(1)}&{amp.group(2)}".lower())
    if len(words) >= 3:
        found.add("".join(w[0] for w in words).lower())
    return found


def names_company(headline: str, ticker: str, name: str) -> bool:
    """The headline is about this company: a distinctive word of its name, or its
    ticker written as a ticker ("(FICO:NYSE)", "$DIS", "NFLX")."""
    text = (headline or "").replace("’", "'")
    words = {w.lower().removesuffix("'s") for w in _words(text)}
    if name_words(name) & words:
        return True
    base = re.escape(ticker.split("-")[0].split(".")[0])
    return bool(base) and bool(re.search(rf"(?:\(|\$|\b){base}(?::|\)|\b)", text))


def is_layoff_news(headline: str) -> bool:
    text = headline or ""
    return bool(_RELEVANT.search(text)) and not _NOT_A_CUT.search(text)


def _amount(number: str, unit: str) -> float:
    value = float(number.replace(",", ""))
    return value * (1000 if unit.lower() in ("billion", "b") else 1)


def charges_of(text: str) -> str:
    """'$65 million to $80 million in charges' -> '$65M–$80M'; '$1.0 billion' -> '$1.0B'.
    Only an amount in a sentence about charges or costs counts."""
    for sentence in re.split(r"(?<=[.;])\s+", text or ""):
        if not re.search(r"\bcharges?\b|\bcosts?\b|\bseverance\b", sentence, re.I):
            continue
        m = _CHARGE.search(sentence)
        if m:
            def fmt(n, u):
                v = _amount(n, u)
                return f"${v / 1000:.1f}B" if v >= 1000 else f"${v:,.0f}M"
            low = fmt(m.group(1), m.group(2))
            return f"{low}–{fmt(m.group(3), m.group(4))}" if m.group(3) else low
    return ""


def jobs_of(text: str) -> str:
    m = _JOBS.search(text or "")
    if not m or re.fullmatch(r"(?:19|20)\d\d", m.group(1)):
        return ""
    return m.group(1)


def share_of(text: str) -> str:
    m = _SHARE.search(text or "")
    return f"{m.group(1)}%" if m else ""


def item_205_summary(situation: str) -> str:
    """The first sentence of the Item 2.05 body that says what is being done."""
    text = re.sub(r"\s+", " ", situation or "")
    # "Item 2.05 below" / "above" is a cross-reference; the heading is the item.
    found = [m for m in _ITEM_205.finditer(text) if not m.group("ref")]
    m = next((m for m in found if m.group("heading")), found[0] if found else None)
    if not m:
        return ""
    body = text[m.end():]
    end = _NEXT_ITEM.search(body)
    body = body[:end.start()] if end else body
    for sentence in re.split(r"(?<=[.])\s+(?=[A-Z])", body):
        if _ACTION.search(sentence) and len(sentence) > 40 and "incorporated" not in sentence:
            return sentence.strip()[:300]
    return ""


def clean_layoff_events(df: pd.DataFrame, names: Dict[str, str], window_days: int = 3) -> pd.DataFrame:
    """Filter, attribute and fold the raw rows (see the module docstring).

    `df` columns: ticker, event_date, event_subtype, headline, source, source_ref,
    situation (8-K rows only). Returns them plus jobs, workforce_pct, charges."""
    if df.empty:
        return df.assign(jobs="", workforce_pct="", charges="")
    df = df.copy()
    df["ticker"] = df["ticker"].astype(str)
    is_filing = df["source"].astype(str).str.startswith("SEC")
    summary = df["situation"].where(is_filing, "").fillna("").map(item_205_summary)
    df["headline"] = df["headline"].where(~is_filing | summary.eq(""), summary)
    named = pd.Series([names_company(h, t, names.get(t, "")) for h, t in zip(df["headline"], df["ticker"])],
                      index=df.index)
    keep = is_filing | (df["headline"].map(is_layoff_news) & named)
    df = df[keep].copy()
    text = df["headline"].fillna("") + " " + df["situation"].fillna("")
    df["jobs"] = text.map(jobs_of)
    df["workforce_pct"] = text.map(share_of)
    df["charges"] = df["situation"].fillna("").map(charges_of)

    # One company listed several times (TFC and TFC-P-O; BMWKY and BAMXF): the
    # same story on the same day is kept under one of its lines.
    company = df["ticker"].map(lambda t: " ".join(sorted(name_words(names.get(t, t)))) or t)
    df = (df.assign(_co=company, _rank=df["ticker"].str.contains(r"[-.]").astype(int) * 100 + df["ticker"].str.len())
            .sort_values("_rank")
            .drop_duplicates(["_co", "event_date", "headline"])
            .drop(columns="_rank"))

    # Several reports of one cut: within `window_days` of the first, one row.
    df["_specific"] = (df["jobs"].ne("") | df["workforce_pct"].ne("") | df["charges"].ne("")).astype(int)
    df = df.sort_values(["_co", "event_date"])
    rows = []
    for _, group in df.groupby("_co", sort=False):
        start = None
        cluster = []
        for i, row in group.iterrows():
            if start is not None and (row["event_date"] - start).days > window_days:
                rows.append(_pick(group.loc[cluster]))
                cluster = []
            if not cluster:
                start = row["event_date"]
            cluster.append(i)
        if cluster:
            rows.append(_pick(group.loc[cluster]))
    out = pd.DataFrame(rows).drop(columns=["_co", "_specific"])
    return out.sort_values("event_date", ascending=False, ignore_index=True)


def _pick(cluster: pd.DataFrame) -> pd.Series:
    """The filing if there is one, else the headline that states numbers, else the
    first; the numbers any report states are carried onto it."""
    filing = cluster["source"].astype(str).str.startswith("SEC")
    best = cluster.assign(_f=filing.astype(int)).sort_values(
        ["_f", "_specific", "event_date"], ascending=[False, False, True]).iloc[0].drop("_f")
    for col in ("jobs", "workforce_pct", "charges"):
        if not best[col]:
            found = cluster[col][cluster[col].ne("")]
            if len(found):
                best[col] = found.iloc[0]
    return best
