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


# Sentence fragments the nightly regex captures instead of a party name read as
# prose, not as a company: "Qualcomm shares", "credit card accounts issued by
# Wells Fargo", "B deal, its second in four". Two tells catch them, and both are
# deliberately narrow — the 262 LLM-verified names in ma_event_overrides.json are
# the regression oracle, and they include "lululemon athletica inc.",
# "salesforce.com, inc." and "Starbucks packaged goods and foodservice business".
#
#   1. a LOWERCASE deal noun ("…shares", "…notes") — capitalised is fine, because
#      "Stock Yards Bancorp" and "Surpique Acquisition Limited" are real companies;
#   2. a value that STARTS lowercase and carries no legal-entity suffix — real
#      lowercase brands ("lululemon athletica inc.") always keep their suffix.
MA_NAME_DEAL_NOUNS = {
    "deal", "deals", "share", "shares", "stock", "stocks", "notes",
    "receivables", "restaurants", "accounts", "offering", "proceeds",
}
# a finite verb or a reporting adverb means we captured a sentence, not a name —
# no company name in the 480-name gold set contains one
MA_NAME_PROSE_VERBS = {
    "is", "was", "are", "were", "be", "been", "being", "has", "have", "had",
    "will", "would", "said", "says", "expects", "expected", "announced",
    "plans", "agreed", "completed", "reported", "acquires", "acquired",
    "buys", "bought", "sells", "sold", "according", "significantly",
}
_JOINERS = {"of", "the", "and", "for", "de", "a", "an", "to", "by", "in",
            "at", "on", "its", "van", "von", "del", "la", "le", "du"}
_LEGAL_SUFFIX = re.compile(
    r"\b(?:inc|inc\.|corp|corp\.|corporation|co|co\.|company|llc|l\.l\.c\.|lp|llp|"
    r"plc|ltd|ltd\.|limited|gmbh|ag|s\.a\.|sa|n\.v\.|nv|se|ab|oy|as|a/s|pte|"
    r"holdings?|group|partners|bancorp|trust)\b[.,]?\s*$", re.IGNORECASE)


def _reads_as_prose(name: str) -> bool:
    """True when the value looks like a clause rather than a company name."""
    words = re.findall(r"[A-Za-z][\w'’.-]*", name)
    if not words:
        return True
    lowers = {w.lower().strip(".,") for w in words if w[0].islower()}
    if lowers & MA_NAME_DEAL_NOUNS or lowers & MA_NAME_PROSE_VERBS:
        return True
    # "GE Healthcare in Talks" — a headline clause whose noun happens to be
    # capitalised, so the lowercase rules above miss it
    if re.search(r"\bin\s+(?:talks|discussions|negotiations|advanced\s+talks)\b",
                 name, re.IGNORECASE):
        return True
    # An all-lowercase opening word on a 3+ word value is a clause picked up
    # mid-sentence. Short lowercase brands are left alone ("ghd", "buybuy BABY"),
    # so is camelCase ("eBay Classifieds business") and anything that kept its
    # legal suffix ("lululemon athletica inc.").
    first = words[0]
    if (len(words) >= 3 and first.islower() and first not in _JOINERS
            and not _LEGAL_SUFFIX.search(name)):
        return True
    return False


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
    if _reads_as_prose(v):
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


# ── display gate for the stored ma_* columns ─────────────────────────────────
#
# The upstream nightly job writes these columns straight from regex captures, and
# an audit of all 22,122 'M&A Activity' rows (2026-09-16 spec) found the amount
# wrong often enough that it cannot be shown raw:
#
#   * 142 of 821 plain-"$N" figures were stored UN-scaled — off by 1,000,000x.
#     42 rows currently claim a deal above $1 trillion (PAG "$12,340,000" -> 12340000).
#   * 1,319 of 7,414 amounts are per-share or non-USD quotes stored as a total
#     (TMHC "$72.50 per share" -> 72.50, i.e. a $72.5M tag on a ~$10B deal).
#   * 2,332 come from 8-K Item 1.01 'Material Agreement' rows — revolvers and note
#     issuances (MU $1,000,000,000), never deal consideration.
#
# Same philosophy as the extractor above: show "—" rather than a wrong number.

# 8-K Item 1.01 covers BOTH merger agreements and credit agreements, so the
# 'Material Agreement' subtype cannot be excluded wholesale — measured on STG it
# hides 624 genuine deals (Adobe/Figma $1,000M, BidCo/Adevinta $2,200M,
# Denso/Silicon Carbide $500M). A financing instrument is identified by its own
# vocabulary instead, checked over the headline, the situation text AND the party
# names ("RumbleOn Finance Receivables" is a securitization, not a target).
MA_NON_DEAL_SUBTYPES = frozenset()

_FINANCING = re.compile(
    r"credit\s+(?:agreement|facility)|revolv|term\s+loan|indenture|\bnotes\b|"
    r"floorplan|promissory|securitiz|receivabl|loan\s+agreement|\babl\b|"
    r"forbearance|warehouse|repurchase|depositor|aggregate\s+principal|"
    r"preferred\s+(?:stock|shares|units)|\bdebentures?\b|"
    r"(?:funding|securities|mortgage|finance|capital|master)\b[,\s]+(?:llc|l\.l\.c\.|lp|trust)",
    re.IGNORECASE)


def ma_amount_is_financing(*parts: Any) -> bool:
    """True when the row describes a loan/notes instrument rather than a deal."""
    return bool(_FINANCING.search(" ".join(str(p) for p in parts if p)))

_PER_SHARE = re.compile(r"per\s+share|per\s+unit|/\s*share|\beach\b", re.IGNORECASE)
_NON_USD = re.compile(r"[€£¥]|\bC\$|\bA\$|\bHK\$|"
                      r"\b(?:chf|sek|nok|dkk|eur|gbp|cad|aud|rmb|cny|jpy|yen|inr)\b",
                      re.IGNORECASE)
_AMOUNT = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*"
                     r"(billion|million|trillion|thousand|bn|mn|[bmk])?\b", re.IGNORECASE)
_SCALE_TO_MILLIONS = {
    "trillion": 1_000_000.0, "billion": 1000.0, "bn": 1000.0, "b": 1000.0,
    "million": 1.0, "mn": 1.0, "m": 1.0, "thousand": 0.001, "k": 0.001,
}

MA_AMOUNT_MIN_USD_M = 0.1
MA_AMOUNT_MAX_USD_M = 1_000_000.0          # $1T — above this the row is mis-scaled


def parse_ma_amount(value_text: Any) -> Optional[float]:
    """Deal amount in USD millions, read from the filing's own words.

    The stored `ma_transaction_value_usd_m` is NOT trusted: 142 of 821 plain-dollar
    rows were converted the wrong way (PAG "$12,340,000" was stored as 12,340,000,
    i.e. $12.34 TRILLION), and no row has a number without its text, so the text is
    always the better source. Parsing it fixes those rows instead of hiding them.
    """
    text = (value_text or "").strip()
    if not text or _PER_SHARE.search(text) or _NON_USD.search(text):
        return None                        # a per-share quote is not a deal size
    match = _AMOUNT.search(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    unit = (match.group(2) or "").lower()
    if unit:
        amount = number * _SCALE_TO_MILLIONS[unit]
    elif number >= 10_000:
        amount = number / 1e6              # bare dollars, e.g. "$22,500,000"
    else:
        return None                        # bare "$441" — dollars or millions? unknowable
    if not (MA_AMOUNT_MIN_USD_M <= amount <= MA_AMOUNT_MAX_USD_M):
        return None
    return amount


# On an Item 1.01 row the amount alone cannot tell a purchase price from a
# borrowing, so a named buyer AND a named seller are required as corroboration.
# Every other subtype already says it is a deal.
MA_SUBTYPES_NEEDING_PARTIES = frozenset({"Material Agreement"})


def format_ma_amount(value_text: Any, value_usd_m: Any = None,
                     event_subtype: Optional[str] = None,
                     financing_context: Any = None,
                     parties_known: bool = True) -> str:
    """Deal amount for display, or '—' when the filing does not state one clearly.

    `value_usd_m` is accepted for call compatibility and deliberately unused — the
    text is parsed instead (see parse_ma_amount). `financing_context` is any extra
    text (headline, situation, party names) used to spot a loan or notes issue.
    `parties_known` says whether both the acquirer and the target survived
    validation, which Item 1.01 rows need before an amount can be believed.
    """
    if event_subtype in MA_NON_DEAL_SUBTYPES:
        return "—"
    if event_subtype in MA_SUBTYPES_NEEDING_PARTIES and not parties_known:
        return "—"
    amount = parse_ma_amount(value_text)
    if amount is None:
        return "—"
    if ma_amount_is_financing(value_text, financing_context):
        return "—"
    if amount >= 1000:
        return f"${amount / 1000:,.2f}B"
    return f"${amount:,.1f}M"


def _self_check() -> None:
    """Run: python app/utils/ma_8k_extract.py — asserts against the real audit corpus."""
    # rejected: the exact rows the 2026-09-16 audit proved wrong
    # financing is rejected by its vocabulary, not by its 8-K item number
    assert format_ma_amount("$12,340,000", None, "Material Agreement",
                            "amendment to ABL credit facility") == "—"            # PAG
    assert format_ma_amount("$600 million", None, "Material Agreement",
                            "J.P. Morgan Securities LLC aggregate principal") == "—"
    # …but a real acquisition filed under the same item is shown
    assert format_ma_amount("$1,000 million", None, "Material Agreement",
                            "Adobe Inc. Figma, Inc.") == "$1.00B"
    # Item 1.01 financing that slipped through an earlier, looser rule
    assert format_ma_amount("$925 million", None, "Material Agreement",
                            "The Wendy’s Company 2018 Notes") == "—"
    assert format_ma_amount("$900 million", None, "Material Agreement",
                            "Taco Bell Funding, LLC") == "—"
    assert format_ma_amount("$15 million", None, "Material Agreement",
                            "JAKK Series A Senior Preferred stock") == "—"
    assert format_ma_amount("$575 million", None, "Material Agreement",
                            "NVIDIA Corporation", parties_known=False) == "—"
    assert format_ma_amount("$72.50 per share", 72.50, "M&A Closing") == "—"           # TMHC
    assert format_ma_amount("C$4.15 and C$4.00 per share", 4.15, "Deal News") == "—"   # GFI
    assert format_ma_amount("£21,850,000", 27.00, "M&A News (6-K)") == "—"             # HBCYF
    assert format_ma_amount("$1,000,000,000", None, "Material Agreement",
                            "revolving credit agreement") == "—"                  # MU
    assert format_ma_amount("$441", 0.44, "Deal News") == "—"                          # bare, scale unknowable
    assert format_ma_amount(None, None, "Deal News") == "—"
    # the stored number is ignored — the text decides, so mis-scaled rows are
    # RECOVERED rather than hidden
    assert format_ma_amount("$12,340,000", 12340000.00, "Deal News") == "$12.3M"
    assert format_ma_amount("$22,500,000", 22.50, "Deal News") == "$22.5M"
    assert format_ma_amount("$49,068,844.", 49068844.00, "M&A Closing") == "$49.1M"
    # kept: amounts whose text states its own scale
    assert format_ma_amount("$1.5 billion", 1500.00, "M&A Closing") == "$1.50B"        # ADI/Empower
    assert format_ma_amount("$315 million", 315.00, "Deal News") == "$315.0M"          # AWK/Nexus
    assert format_ma_amount("$5.6 billion", 5600.00, "Deal News") == "$5.60B"          # BIIB/Apellis
    assert format_ma_amount("$55 billion", 55000.00, "Deal News") == "$55.00B"         # EA buyout
    assert format_ma_amount("for $1.2 billion", 1200.00, "M&A Closing") == "$1.20B"    # YUM
    # names: boilerplate the nightly job still writes today
    assert sanitize_ma_name("Financial Statements of Businesses or Funds") is None     # KPLT
    assert sanitize_ma_name("Acquisition or Disposition of Assets. As discussed in "
                            "Item 1.01 of this Current Report") is None               # BBBY
    assert sanitize_ma_name("reference") is None
    # prose fragments seen in the live grid (2026-09-16 UI run)
    assert sanitize_ma_name("B deal, its second in four") is None
    assert sanitize_ma_name("Qualcomm shares") is None
    assert sanitize_ma_name("GE Healthcare in Talks") is None
    assert sanitize_ma_name("stock through Employee Stock Purchase Plan") is None
    assert sanitize_ma_name("credit card accounts issued by Wells Fargo") is None
    assert sanitize_ma_name("automotive finance receivables") is None
    assert sanitize_ma_name("Additional 9.750% senior notes due 2029") is None
    assert sanitize_ma_name("21 Applebee's restaurants") is None
    assert sanitize_ma_name("Clearwater is expected to significantly boost its "
                            "investment banking business") is None
    # real names must survive
    assert sanitize_ma_name("Bed Bath & Beyond, Inc.") == "Bed Bath & Beyond, Inc."
    assert sanitize_ma_name("buybuy BABY") == "buybuy BABY"
    assert sanitize_ma_name("Surpique Acquisition Limited") == "Surpique Acquisition Limited"
    assert sanitize_ma_name("Stock Yards Bancorp, Inc.") == "Stock Yards Bancorp, Inc."
    assert sanitize_ma_name("Alif Semiconductor") == "Alif Semiconductor"
    assert sanitize_ma_name("The Container Store Holdings, LLC") == "The Container Store Holdings, LLC"
    assert sanitize_ma_name("Amazon Web Services (AWS)") == "Amazon Web Services (AWS)"
    assert sanitize_ma_name("Vita Coco Company, Inc.") == "Vita Coco Company, Inc."
    assert sanitize_ma_name("Leggett & Platt, Incorporated") == "Leggett & Platt, Incorporated"
    # Regression oracle: the 262 LLM-verified events in the curated overlay are
    # the only names we know to be right. Tightening sanitize_ma_name until it
    # starts eating them is how this check fails.
    import json as _json, os as _os
    _gold = _os.path.join(_os.path.dirname(__file__), "..", "data", "ma_event_overrides.json")
    if _os.path.exists(_gold):
        with open(_gold, encoding="utf-8") as _f:
            _events = _json.load(_f).get("events", {})
        _names = [v[k] for v in _events.values() for k in ("acquirer", "target") if v.get(k)]
        _lost = [n for n in _names if not sanitize_ma_name(n)]
        assert len(_lost) <= 1, f"validator now rejects {len(_lost)} verified names: {_lost[:5]}"
        print(f"gold regression: {len(_names) - len(_lost)}/{len(_names)} verified names kept")
    print("ma_8k_extract self-check OK")


if __name__ == "__main__":
    _self_check()
