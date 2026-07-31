"""Store-count extraction ladder — free sources first, LLM last.

One value per (ticker, fiscal year), plus a geographic breakdown when the filing
discloses one. Every candidate carries the evidence it came from, so the result
can be verified mechanically instead of trusted.

Ladder, cheapest first:

    L1  XBRL facts already sitting in coreiq_filing_metrics_v5     free, ~5ms
    L2  the filing HTML itself, from Azure blob                    free, ~1.5s
    L3  gpt-4o-mini over a windowed slice of the filing            $0.000327

L3 is only ever reached when L1 and L2 both come up empty, and it never sees
more than ~1,700 tokens — store counts sit at a median 12% into a 10-K
(Regulation S-K puts Item 1 Business and Item 2 Properties up front), so the
back two thirds of the document are never read.

Results are written once per SEC accession number. A filed 10-K is immutable,
so a cached entry can never go stale; a new fiscal year is simply a new
accession. See store_count_cache.py.
"""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

# ── Vocabulary ───────────────────────────────────────────────────────────────

# Retail unit words as issuers actually write them. Order matters only for
# readability; matching is a plain alternation.
UNIT_WORDS = (
    "stores", "store", "shacks", "restaurants", "warehouses", "clubs",
    "locations", "dealerships", "galleries", "showrooms", "shops", "salons",
    "centers", "centres", "branches", "outlets", "units", "cafes",
    "supercenters", "boutiques", "studios", "pharmacies", "theatres", "theaters",
)
UNIT_RE = r"(?:" + "|".join(sorted(UNIT_WORDS, key=len, reverse=True)) + r")"

# XBRL concepts issuers use for a store count. These are the standard us-gaap
# taxonomy tags — the machine-readable path, and always preferred.
XBRL_CONCEPTS = (
    "us-gaap:NumberOfStores",
    "us-gaap:NumberOfRestaurants",
    "us-gaap:NumberOfUnitsOperated",
    "us-gaap:NumberOfOperatingLocations",
)

# A store count above this is not a store count (it is square footage, a share
# count, or a dollar figure that lost its units).
MAX_PLAUSIBLE_COUNT = 100_000

# Companies whose "locations" are not shops: sales offices, plants, depots,
# distribution centres, REIT assets, franchise agreements. Their counts must
# never appear as store counts, and they must never reach the LLM — ADT's 240
# is sales offices, AKA's 8-22 are showrooms.
NON_RETAIL_TICKERS = {
    "ADT", "AKA", "APH", "ARW", "AVY", "BLD", "CHSCP", "COKE", "DAR", "DHI",
    "DXC", "INGR", "KVUE", "MTH", "O", "PFGC", "PHM", "SPG", "TSN", "UFI",
    "WHR", "ZBRA", "UNFI", "QRTEA", "QVCA.Q", "QVCPQ", "SGI", "FLWS", "PRMB",
    "CHWY", "FNKO", "RCKY",
}

# Counts that are really something else — infrastructure, not retail units.
NON_RETAIL_DIMENSIONS = {
    "distribution centers", "manufacturing facilities", "properties",
    "homes", "franchises", "offices", "plants",
}

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class StoreCount:
    """One extracted store count with the evidence that justifies it."""

    ticker: str
    fiscal_year: int
    accession: Optional[str] = None
    value: Optional[int] = None
    source_rule: str = "none"        # L1_xbrl | L2_table | L2_prose | L3_llm | none
    confidence: str = "none"         # high | medium | low | none
    evidence: str = ""               # the row or sentence the value came from
    fye_date: Optional[str] = None
    unit_word: Optional[str] = None
    by_geography: Dict[str, int] = field(default_factory=dict)
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Text preparation ─────────────────────────────────────────────────────────

def filing_to_text(raw_html: str) -> str:
    """Flatten filing HTML to text while KEEPING table structure.

    Table cells become ' | ' and rows become newlines, so a row like
    'Total retail units | 6,535 | 4,966 | 11,501' survives as one line and can
    be reasoned about. Losing that structure is what makes plain text scraping
    of 10-Ks unreliable.
    """
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw_html)
    text = re.sub(r"(?is)<t[dh][^>]*>", " | ", text)
    # Break on BOTH the opening and closing row tag. EDGAR HTML frequently omits
    # </tr>, and splitting on the closing tag alone welded consecutive rows
    # together — WMT's 'Total retail units | 6,535 | 4,966 | 11,501' arrived cut
    # after the first cell, so the worldwide total was invisible.
    text = re.sub(r"(?is)</?tr[^>]*>", "\n", text)
    text = re.sub(r"(?is)<br[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_module.unescape(text)
    # Zero-width and non-breaking characters are rife in EDGAR HTML.
    text = text.replace("​", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def front_section(text: str) -> str:
    """The part of a 10-K where store counts live.

    Regulation S-K fixes the item order, so we can cut deterministically rather
    than read the whole document. The cut is at Item 8 Financial Statements:
    everything useful — Item 1 Business, Item 2 Properties and the MD&A store
    roll-forward — sits before it, and the financial statements that follow are
    pure noise for this purpose.

    An earlier cut at Item 3 (36% of the document) was measured too aggressive:
    WMT's 'Total units | 11,501' sits at 41% and TGT's store roll-forward at
    41%, both in MD&A. The table-of-contents entry for Item 8 is skipped by
    ignoring matches in the first 10%.
    """
    end = None
    for pattern in (r"(?i)item\s*8\s*[\.\-–—:]?\s*(?:consolidated\s+)?financial\s+statements",
                    r"(?i)report\s+of\s+independent\s+registered\s+public\s+accounting"):
        spots = [s for s in (m.start() for m in re.finditer(pattern, text))
                 if s > len(text) * 0.10]          # skip the table-of-contents hit
        if spots:
            end = max(end or 0, spots[0])
    if not end or end < len(text) * 0.25:
        end = int(len(text) * 0.70)
    return text[:end]


# 'Number of stores at end of period (7) | 850 | 859' — the (7) is a footnote
# reference, not data. Left in, it became the answer.
_FOOTNOTE_MARKER = re.compile(r"\((\d{1,2})\)")


_FISCAL_YEAR_END = re.compile(
    r"(?i)fiscal\s+year\s+ended\s*:?\s*"
    r"([A-Z][a-z]+)\s+(\d{1,2})\s*,?\s*(\d{4})")

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def fiscal_year_of(text: str) -> Optional[Tuple[int, str]]:
    """The fiscal year this 10-K reports on, from its own cover page.

    Every 10-K states 'For the fiscal year ended <date>' — required by the
    form itself, which makes it reliable for companies we hold no DB row for.
    The year returned is the calendar year the period ENDS in, matching the
    report_fiscal_year convention used in coreiq_filing_metrics_v5 (a January
    2026 year end is FY2026).
    """
    match = _FISCAL_YEAR_END.search(text[:200_000])
    if not match:
        return None
    month, day, year = match.group(1).lower(), match.group(2), match.group(3)
    if month not in _MONTHS:
        return None
    return int(year), f"{year}-{_MONTHS[month]:02d}-{int(day):02d}"


def numbers_in(line: str, drop_footnotes: bool = True) -> List[int]:
    """Plausible store counts appearing in a line, digits and spelled-out."""
    if drop_footnotes:
        line = _FOOTNOTE_MARKER.sub(" ", line)
    found = [int(x.replace(",", "")) for x in re.findall(r"\b\d[\d,]{0,7}\b", line)]
    found += [_WORD_NUMBERS[w] for w in re.findall(r"[A-Za-z]+", line.lower())
              if w in _WORD_NUMBERS]
    return [n for n in found if 0 < n <= MAX_PLAUSIBLE_COUNT]


def is_year_header(line: str) -> bool:
    """'| Change in Number of Stores | 2019 | 2018 |' is a column header.

    Fiscal years are bare four-digit numbers that step down by one or two. A
    store count in the same range (TGT's 1,995) always carries a thousands
    separator, which is what tells them apart.
    """
    raw = _FOOTNOTE_MARKER.sub(" ", line)
    tokens = re.findall(r"\b\d[\d,]{0,7}\b", raw)
    if not tokens or any("," in t for t in tokens):
        return False
    values = [int(t) for t in tokens]
    if not all(1990 <= v <= 2100 for v in values):
        return False
    if len(values) == 1:
        return True
    return all(0 < values[i] - values[i + 1] <= 2 for i in range(len(values) - 1))


# ── L2: read the filing ──────────────────────────────────────────────────────

# A row that states a total. Either the label says so, or the label IS the unit.
# Table rows start with a pipe once cells are flattened ('| Total stores | 924'),
# so the label may be preceded by pipes and spaces.
_TOTAL_ROW = re.compile(
    rf"(?im)^[\s|]{{0,6}}[^\n|]{{0,60}}\b(?:total|grand\s+total|total\s+company|"
    rf"number\s+of\s+{UNIT_RE}|{UNIT_RE}\s+at\s+(?:year|period)[\s-]*end)\b[^\n]{{0,250}}$")

_PROSE = re.compile(
    rf"(?i)\b(?:operated|operate|had|owned\s+and\s+operated|opened|with|"
    rf"consisted\s+of|comprised\s+of|totaling)\s+"
    rf"(?:a\s+total\s+of\s+)?(?:approximately\s+|about\s+|over\s+|more\s+than\s+|nearly\s+)?"
    rf"([\d,]{{1,7}}|{'|'.join(_WORD_NUMBERS)})\s+"
    # Brand and qualifier words sit between the count and the unit, and they
    # carry punctuation: "1,159 Kohl's stores", "191 U.S. warehouse-format
    # stores". Excluding apostrophes and dots silently lost both.
    rf"(?:[A-Za-z][A-Za-z\.\-’']{{0,16}}\s+){{0,3}}({UNIT_RE})\b")


# Rows that talk about area, money or activity rather than the fleet itself.
# 'Total AutoZone store square footage | 43,502' and 'Total new stores | 8'
# both look like totals and are both the wrong quantity.
_NOT_A_FLEET_ROW = re.compile(
    r"(?i)\b(?:square\s*(?:foot|feet|footage)|sq\.?\s*ft|selling\s+space|"
    r"new\s+(?:stores|units|locations)|stores?\s+(?:opened|closed|acquired|"
    r"remodeled|relocated|converted)|opened|closed|acquisitions?|"
    r"revenue|sales|income|expense|assets|liabilit|shares|employees|"
    r"backlog|capital\s+expenditure)\b")

# A roll-forward states beginning, additions, closures (in parentheses) and the
# ending fleet. The ending count is the LAST cell, not the first. The closure
# figure must be a CELL of its own — a '(7)' glued to the row label is a
# footnote reference and must not turn the row into a roll-forward.
_ROLLFORWARD = re.compile(r"\|\s*\(\s*\d[\d,]*\s*\)\s*\|")

# Activity words disqualify a row outright; area/money words only disqualify a
# TABLE row. A sentence may legitimately mention both the fleet and its square
# footage — 'we operated 1,159 Kohl's stores with 82.2 million selling square
# feet' is the authoritative statement for KSS.
_ACTIVITY_ONLY = re.compile(
    r"(?i)\b(?:new\s+(?:stores|units|locations)|stores?\s+(?:opened|closed|"
    r"acquired|remodeled|relocated|converted)|openings?|closures?)\b")

# The filer talking about itself, as opposed to a peer or a former employer.
_SELF_REFERENCE = re.compile(r"(?i)\b(?:we|our|us|the\s+company|the\s+registrant)\b")


def row_total(cells: List[int]) -> Optional[int]:
    """Pick the total out of one table row's numbers.

    Three shapes occur, and telling them apart is what makes table reading
    trustworthy:

      components + total   'Total retail units | 6,535 | 4,966 | 11,501'
                           last cell equals the sum of the others -> take it
      year series          'Total Stores | 924 | 972'
                           current fiscal year is the first column -> take it
      single value         'Total | 1,868'
    """
    if not cells:
        return None
    if len(cells) == 1:
        return cells[0]
    if len(cells) >= 3:
        head, tail = cells[:-1], cells[-1]
        if abs(sum(head) - tail) <= max(1, tail * 0.01):
            return tail
    # Otherwise the leftmost column is the current period in every filing
    # layout we sampled (issuers put the most recent year first).
    return cells[0]


def extract_from_filing(text: str, fye_hint: Optional[str] = None) -> List[StoreCount]:
    """Candidate store counts from filing text, best rule first.

    Returns candidates, not a decision — ranking happens in choose_best so the
    caller can see everything that was considered.
    """
    body = front_section(text)
    out: List[StoreCount] = []
    line_starts = [m.start() for m in re.finditer(r"^", body, re.M)]

    def header_mentions_units(position: int) -> bool:
        """Does the table this row belongs to announce a store count above it?

        TGT's roll-forward ends '| Total | 1,897 | 1,868 |' — the word 'stores'
        appears only in the caption 'Change in Number of Stores' a few rows up.
        Without looking back, the authoritative row is invisible.
        """
        above = [s for s in line_starts if s < position][-8:]
        if not above:
            return False
        caption = body[above[0]:position]
        # A caption about floor area also mentions 'stores' — FND's square
        # footage table is headed 'warehouse-format stores' and its bare
        # '| Total | 21,017 |' is thousands of square feet, not a fleet.
        if _NOT_A_FLEET_ROW.search(caption):
            return False
        return bool(re.search(rf"(?i)\b{UNIT_RE}\b", caption))

    for match in _TOTAL_ROW.finditer(body):
        line = " ".join(match.group(0).split())
        # A real table row has cells. Without this, prose such as 'we operated a
        # total of 1,923 stores, of which 1,628...' is parsed with table rules
        # and the leading date ('At January 29') becomes the count.
        if "|" not in line:
            continue
        in_row = bool(re.search(rf"(?i)\b{UNIT_RE}\b", line))
        if not in_row:
            # Fall back to the table's caption ONLY for a row labelled bare
            # 'Total'. Anything more specific names its own quantity — 'Total
            # paid members', 'Total operating expenses' — and must not inherit
            # the caption's meaning.
            label = line.split("|")[1] if line.count("|") > 1 else line
            if re.fullmatch(r"[\s|]*total[\s|]*", label, re.I) is None:
                continue
            if not header_mentions_units(match.start()):
                continue
        if any(bad in line.lower() for bad in NON_RETAIL_DIMENSIONS):
            continue
        if _NOT_A_FLEET_ROW.search(line) or is_year_header(line):
            continue
        cells = numbers_in(line)
        # Roll-forward: 'Total stores | 1,026 | 5 | (22) | 1,009' — beginning,
        # opened, closed, ending. The fleet is the last cell.
        total = cells[-1] if (_ROLLFORWARD.search(line) and len(cells) >= 3) \
            else row_total(cells)
        if total is None:
            continue
        unit = re.search(rf"(?i)\b({UNIT_RE})\b", line)
        out.append(StoreCount(
            ticker="", fiscal_year=0, value=total, source_rule="L2_table",
            confidence="high", evidence=line[:400],
            unit_word=unit.group(1).lower() if unit else None))

    for match in _PROSE.finditer(body):
        token = match.group(1)
        value = (_WORD_NUMBERS.get(token.lower())
                 if not token[0].isdigit() else int(token.replace(",", "")))
        if not value or value > MAX_PLAUSIBLE_COUNT:
            continue
        # 'the number of stores that we either opened or were preparing to open'
        # is activity, not the fleet. Only the words BEFORE the count matter —
        # what follows often, and legitimately, describes square footage.
        lead_in = body[max(0, match.start() - 90):match.start() + 40]
        if _ACTIVITY_ONLY.search(lead_in):
            continue
        # The sentence must be about THIS company. Director and officer
        # biographies quote other chains' fleets — Shake Shack's 10-K mentions
        # an executive who ran "Arby's, one of the largest ... 3,400 restaurants",
        # which is larger than Shake Shack and would otherwise win outright.
        if not _SELF_REFERENCE.search(body[max(0, match.start() - 200):match.end() + 40]):
            continue
        start = max(0, match.start() - 120)
        out.append(StoreCount(
            ticker="", fiscal_year=0, value=value, source_rule="L2_prose",
            confidence="medium",
            evidence=" ".join(body[start:match.end() + 60].split())[:400],
            unit_word=match.group(2).lower()))

    return out


def choose_best(candidates: List[StoreCount],
                prior_value: Optional[int] = None) -> Optional[StoreCount]:
    """Rank candidates into one answer.

    Tables beat prose (a total row is a stated fact; prose is often rounded or
    talks about a subset). Within a rule, the value repeated most often wins —
    a real total tends to be restated. `prior_value` breaks remaining ties
    toward continuity with the previous year, which is what a store fleet does.
    """
    if not candidates:
        return None

    def evidence_quality(candidate: StoreCount) -> int:
        text = (candidate.evidence or "").lower()
        points = 0
        # A label that names the fleet at a point in time is the authoritative
        # one: 'Number of stores at end of period', 'Total units', 'Ending
        # store count'. These beat any other total in the filing.
        if re.search(r"\b(?:at (?:year|period)[\s-]*end|end of (?:the )?(?:period|year|fiscal)"
                     r"|ending (?:store|unit|shack)|units? at period end)\b", text):
            points += 4
        if re.search(r"\btotal\b", text):
            points += 2
        if re.search(r"\bas of\b|\bat (?:year|period)[\s-]*end\b", text):
            points += 2
        if re.search(r"\b(?:operated|operate|we had)\b", text):
            points += 1
        # A store word the company actually retails from, not a support site.
        if candidate.unit_word in ("stores", "store", "warehouses", "clubs",
                                   "restaurants", "shacks", "galleries",
                                   "dealerships", "supercenters"):
            points += 1
        return points

    def score(candidate: StoreCount) -> tuple:
        rule_rank = {"L2_table": 2, "L2_prose": 1}.get(candidate.source_rule, 0)
        quality = evidence_quality(candidate)
        value = candidate.value or 0
        continuity = 0.0
        if prior_value and value:
            continuity = 1.0 - min(abs(value - prior_value) / max(prior_value, 1), 1.0)
        # Only candidates of the SAME rule are ever compared on the next two
        # fields, so each rule can order by what actually identifies its total.
        #
        # Tables: the label is precise, so evidence wins ('Number of stores at
        # end of period' beats a segment subtotal of larger magnitude).
        # Prose: labels do not exist, but a fleet total is by definition the
        # largest unit count in the filing — its parts are all smaller. Ranking
        # SHAK's '112 licensed Shacks' above '275 Shacks' was exactly this
        # mistake: the subset simply sat in a wordier sentence.
        if rule_rank == 2:
            return (rule_rank, quality, value, continuity)
        return (rule_rank, value, quality, continuity)

    return max(candidates, key=score)


# ── Sanity checks ────────────────────────────────────────────────────────────

def looks_plausible(value: Optional[int], prior: Optional[int]) -> Tuple[bool, str]:
    """Reject values that cannot be a year-over-year store count.

    A fleet does not double or halve in a year. Without a prior year we can
    only bound the magnitude.
    """
    if value is None or not (0 < value <= MAX_PLAUSIBLE_COUNT):
        return False, "out of range"
    if prior:
        drift = abs(value - prior) / max(prior, 1)
        if drift > 0.60:
            return False, f"implausible move vs prior year ({prior} -> {value})"
    return True, ""


def value_supported_by(value: int, evidence: str) -> bool:
    """The value must be reproducible from its own evidence — literally present,
    or the sum of a subset of the evidence's numbers (segment totals)."""
    pool = numbers_in(evidence)
    if value in pool:
        return True
    reachable = {0}
    for number in pool[:14]:
        reachable |= {s + number for s in reachable if s + number <= value}
    return value in reachable
