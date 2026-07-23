"""Deterministic M&A field extraction from 8-K Item 2.01/1.01 text.

Used by the background overlay auto-enricher (utils/ma_overrides_auto.py) for
M&A rows the nightly ETL inserts AFTER the curated repo overlay was built.
No LLM, no network: pure template matching over legal boilerplate, tuned and
precision-tested against 262 LLM-verified gold extractions (see
docs/superpowers/specs/2026-07-15-calendar-ma-acquirer-target-repair-design.md).

Philosophy: HIGH PRECISION over recall. A field is emitted only when a
high-confidence template matches; anything uncertain stays None and the
calendar renders "—" instead of misinformation.
"""
import re
from typing import Any, Dict, Optional

# ── shared name validation (also used by pages/calendar.py) ─────────

MA_NAME_BOILERPLATE = (
    "item 1.01", "item 2.01", "item 9.01", "current report", "form 8-k",
    "incorporated herein", "the information contained", "exhibit 99",
    "exhibit 2.1", "financial statements of business", "disposition of assets",
    "definitive agreement", "described above", "completed its previously",
    "completed the previously",
)
MA_NAME_GENERIC = {
    "the company", "purchaser", "seller", "buyer", "parent", "merger sub",
    "sellers", "reference", "borrower", "company", "target", "acquiror",
    "acquirer", "merger subsidiary", "the buyer", "the purchaser",
    "the seller", "the sellers", "the parent", "the target", "the business",
    "the acquiror", "the acquirer",
}


def sanitize_ma_name(val: Any) -> Optional[str]:
    """Reject regex-extraction garbage in M&A acquirer/target names."""
    if not val or not isinstance(val, str):
        return None
    v = re.sub(r"\s+", " ", val).strip().strip('"')
    if not (2 <= len(v) <= 120):
        return None
    low = v.lower()
    if any(tok in low for tok in MA_NAME_BOILERPLATE):
        return None
    if low in MA_NAME_GENERIC:
        return None
    return v


# ── 8-K item section slicing (shared with scripts/enrich_ma_events_v2.py) ────

def extract_item_sections(full_text: str, wanted=("2.01", "1.01", "8.01"),
                          max_chars: int = 7000) -> str:
    """Pull the named Item sections out of 8-K text (2.01 first)."""
    text = full_text
    headers = list(re.finditer(r"Item\s+(\d+\.\d+)", text))
    sections = {}
    for i, h in enumerate(headers):
        code = h.group(1)
        if code not in wanted or code in sections:
            continue
        start = h.start()
        end = len(text)
        for h2 in headers[i + 1:]:
            if h2.group(1) != code and h2.start() > start + 20:
                end = h2.start()
                break
        sig = re.search(r"\bSIGNATURES?\b", text[start + 20:end])
        if sig:
            end = start + 20 + sig.start()
        chunk = re.sub(r"<[^>]+>", " ", text[start:end])
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if len(chunk) > 60:
            sections[code] = chunk
    ordered = [sections[c] for c in ("2.01", "1.01", "8.01") if c in sections]
    combined = "\n\n".join(ordered)
    if not combined:
        combined = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    return combined[:max_chars]


# ── deterministic field extraction ────────────────────────────────────────────

# A company name: capitalized words incl. &,.'’- and lowercase joiners, up to
# ~10 tokens. Requires a leading capital/digit so sentence fragments don't match.
_NAME = r"(?:[A-Z0-9][\w&.,'’\-]*|of|the|and|de|d'|for)(?:\s+(?:[A-Z0-9][\w&.,'’\-]*|of|the|and|de|d'|for)){0,9}"

_MONTH = (r"(?:January|February|March|April|May|June|July|August|September|"
          r"October|November|December)")
_DATE = rf"{_MONTH}\s+\d{{1,2}},\s+\d{{4}}"

# name terminators: legal-entity descriptor or clause boundary
_STOP = r"(?=,?\s*\(|,\s+a\s|,\s+an\s|,\s+each\s|,\s+and\s|,\s+pursuant|\s+pursuant\s|,\s+for\s|\s+for\s+(?:approximately\s+)?\$|,\s+from\s|\s+from\s|,\s+dated|\.|;|,\s+in\s)"

_ACQUIRED_BY_FILER = [
    rf"completed\s+(?:its|the)\s+(?:previously\s+announced\s+)?(?:acquisition|purchase)\s+of\s+({_NAME}){_STOP}",
    rf"completed\s+(?:its|the)\s+(?:previously\s+announced\s+)?merger\s+with\s+({_NAME}){_STOP}",
    rf"(?:acquired|purchased)\s+(?:all\s+(?:of\s+)?the\s+(?:outstanding\s+)?(?:capital\s+stock|equity\s+interests|shares|membership\s+interests)\s+of\s+)?({_NAME}){_STOP}",
    rf"merger\s+of\s+.{{0,80}}?\s+with\s+and\s+into\s+({_NAME}){_STOP}",
    rf"Merger\s+Sub\s+(?:was\s+)?merged\s+with\s+and\s+into\s+({_NAME}){_STOP}",
]

_SOLD_BY_FILER = [
    rf"completed\s+(?:its|the)\s+(?:previously\s+announced\s+)?(?:sale|divestiture|disposition)\s+of\s+(?:its\s+)?({_NAME})\s+(?:business\s+)?to\s+({_NAME}){_STOP}",
    rf"sold\s+(?:its\s+)?({_NAME})\s+(?:business\s+)?to\s+({_NAME}){_STOP}",
]

# strict on purpose: looser "for $X million" variants matched financing
# amounts (revolvers, escrows) and dragged precision below 80% on the gold set
_VALUE = [
    r"(?:aggregate\s+|total\s+)?(?:purchase\s+price|consideration|transaction\s+value|enterprise\s+value)\s+of\s+(?:approximately\s+)?\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)",
]

_CLOSE_DATE = [
    rf"^On\s+({_DATE})",
    rf"On\s+({_DATE})[^.]{{0,120}}?\b(?:completed|consummated|closed)\b",
    rf"\b(?:completed|consummated|closed)\b[^.]{{0,80}}?\bon\s+({_DATE})",
]

_ANNOUNCE_DATE = [
    rf"(?:Agreement\s+and\s+Plan\s+of\s+Merger|Merger\s+Agreement|Purchase\s+Agreement|Stock\s+Purchase\s+Agreement|Asset\s+Purchase\s+Agreement)[^.]{{0,80}}?dated\s+as\s+of\s+({_DATE})",
    rf"previously\s+announced[^.]{{0,120}}?dated\s+as\s+of\s+({_DATE})",
]

_ENTITY_SUFFIX_FIX = {
    " Llc": " LLC", " Lp": " LP", " Llp": " LLP", " Plc": " plc",
    " Nv": " N.V.", " Sa": " S.A.", " Ag": " AG", " Se": " SE",
    " Ii": " II", " Iii": " III", " Iv": " IV", " Usa": " USA",
}


def _title_if_shouty(name: str) -> str:
    """EDGAR filer names are often ALL CAPS with /STATE/ suffixes; normalize."""
    if not name:
        return name
    name = re.sub(r"\s*/[A-Za-z]{2,4}/?\s*$", "", name).strip()
    if name != name.upper():
        return name
    t = name.title()
    for bad, good in _ENTITY_SUFFIX_FIX.items():
        if t.endswith(bad):
            t = t[: -len(bad)] + good
        t = t.replace(bad + " ", good + " ").replace(bad + ",", good + ",")
    t = re.sub(r"\bInc\b\.?", "Inc.", t)
    t = re.sub(r"\bCorp\b(?!\.)", "Corp.", t)
    return t


def _clean_captured(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    n = re.sub(r"\s+", " ", name).strip().strip(",").strip()
    # trim trailing joiners the tolerant charset may have swallowed
    n = re.sub(r"\s+(?:of|the|and|for|de)$", "", n)
    # a real company name has at least one word that isn't a joiner
    if not re.search(r"[A-Z]", n):
        return None
    return sanitize_ma_name(n)


def _parse_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    from datetime import datetime
    try:
        return datetime.strptime(re.sub(r"\s+", " ", raw), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def extract_ma_fields(filer_name: str, item_text: str) -> Dict[str, Any]:
    """Template extraction → overlay-schema dict. Unmatched fields are None."""
    out: Dict[str, Any] = {
        "acquirer": None, "target": None, "deal_type": None,
        "transaction_value_usd_m": None, "announce_date": None,
        "close_date": None, "confidence": 0.0, "extractor": "deterministic-v1",
    }
    if not item_text:
        return out
    text = re.sub(r"\s+", " ", item_text)
    filer_disp = _title_if_shouty((filer_name or "").strip())

    for pat in _SOLD_BY_FILER:
        m = re.search(pat, text)
        if m:
            target = _clean_captured(m.group(1))
            buyer = _clean_captured(m.group(2))
            if buyer and target:
                out.update(acquirer=buyer, target=target,
                           deal_type="divestiture", confidence=0.8)
                break

    # spin-merge structures (Reverse Morris Trust etc.) make the FILER the
    # seller-parent, not the acquirer — the filer-is-acquirer assumption fails,
    # so leave names to the LLM/manual pass rather than guess wrong
    spin_merge = re.search(r"\bspin-?off\b|Separation\s+and\s+Distribution", text, re.IGNORECASE)

    if out["acquirer"] is None and not spin_merge:
        for pat in _ACQUIRED_BY_FILER:
            m = re.search(pat, text)
            if m:
                target = _clean_captured(m.group(1))
                if target and filer_disp and sanitize_ma_name(filer_disp):
                    deal = "merger" if "merger" in pat.lower() or "with and into" in pat else "acquisition"
                    out.update(acquirer=filer_disp, target=target,
                               deal_type=deal, confidence=0.75)
                    break

    # transaction value is deliberately NOT emitted: even the strict
    # purchase-price pattern peaked at 88% precision on the gold set (filings
    # quote base vs adjusted totals) — a slightly-wrong dollar figure is worse
    # than the "—" the popup shows for None. _VALUE retained for future tuning.

    for pat in _CLOSE_DATE:
        m = re.search(pat, text, flags=re.MULTILINE)
        if m:
            out["close_date"] = _parse_date(m.group(1))
            if out["close_date"]:
                break
    for pat in _ANNOUNCE_DATE:
        m = re.search(pat, text)
        if m:
            out["announce_date"] = _parse_date(m.group(1))
            if out["announce_date"]:
                break
    return out
