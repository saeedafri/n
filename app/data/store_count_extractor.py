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
    "supermarkets", "markets", "hypermarkets", "dealers", "franchises",
    "clinics", "gyms", "hotels", "pubs", "bakeries", "kiosks",
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

# Industries whose companies actually operate a fleet of premises customers
# visit, per coreiq_companies.primary_industry_coresight. This is the gate that
# decides whether a store count is even a meaningful question.
#
# A hand-kept ticker blocklist could never keep up: batch 1 alone surfaced
# Apple (29,749), Adobe (2,975), Archer Daniels (64,093) and Agree Realty
# (8,100) — a phone maker, a software firm, a grain processor and a REIT, none
# of which has a store fleet, all of which have numbers in their filings that
# look like one. Classification is the company's identity, not a list of
# exceptions, so it scales to the next 1,000 tickers on its own.
STORE_OPERATING_INDUSTRIES = {
    "apparel and footwear speciality retailers", "apparel retail",
    "apparel specialty retail", "apparel and footwear brands and retailers",
    "apparel, accessories and luxury goods", "automotive retail", "beauty",
    "broadline retail", "coffee & beverage", "consumer staples distribution and retail",
    "convenience stores", "department stores", "discount stores", "drug stores",
    "drugstores", "food retail", "footwear retail", "grocery",
    "home and home improvement", "home furnishings", "luxury",
    "mass merchandiser", "other specialty retail", "restaurants",
    "specialty retail", "supermarkets", "warehouse clubs", "wholesale clubs",
    "in-store",
}

# Brand owners run some of their own shops but mostly sell through others, so a
# count in their filing is usually a partial fleet. Allowed through, but the
# result is never treated as a confident worldwide total.
# "E-commerce" is deliberately absent: it covers companies that sell online
# rather than from premises, and in this dataset it also collects a
# misclassified Adobe. Amazon does run stores, but its filings are dense with
# large operational numbers (78,131 was being read as a store count), so it is
# better served by the XBRL layer than by parsing prose.
PARTIAL_FLEET_INDUSTRIES = {
    "apparel and footwear brand owners", "accessories", "footwear",
    "consumer electronics", "pet products",
}
# 'Leisure products' was here and should not have been: Brunswick makes boats
# and Caleres-style makers sell through others, so the only counts in their
# filings are plants and showrooms. It produced BC's fleet of 4 and CALY's 3.


def industry_allows_store_count(industry: Optional[str],
                                allow_unclassified: bool = False) -> bool:
    """Is a store count a meaningful figure for this kind of company?

    `allow_unclassified` decides what to do about a company we hold no
    classification for. 190 of the 504 tickers in the filing archive are not in
    coreiq_companies at all, and treating that silence as "not a retailer"
    silently dropped Grocery Outlet, Arhaus, Natural Grocers, Haverty's,
    Lovesac, Driven Brands, Mister Car Wash, Service Corporation, H&R Block,
    Savers Value Village and Village Super Market — every one of them a real
    store operator. Absence of data is not evidence of absence.

    Callers that can check a result against the company's own history should
    pass True and let the series tests decide; callers that publish a single
    value with nothing to check it against should leave it False.
    """
    if not industry:
        return allow_unclassified
    key = str(industry).replace("​", "").strip().lower()
    return key in STORE_OPERATING_INDUSTRIES or key in PARTIAL_FLEET_INDUSTRIES


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
    # Inline formatting tags are removed WITHOUT a space. EDGAR wraps fragments
    # of words in <span>/<ix:nonFraction> for styling, so substituting a space
    # shattered them: Home Depot's "we operated 2,359 stores" arrived as
    # "we o perated 2,359 stor es" and matched nothing.
    text = re.sub(r"(?is)</?(?:span|b|i|u|em|strong|font|a|sup|sub|small|"
                  r"ix:nonfraction|ix:nonnumeric|ix:continuation)\b[^>]*>", "", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_module.unescape(text)
    # Zero-width and non-breaking characters are rife in EDGAR HTML.
    text = text.replace("​", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _rejoin_split_rows(text)


# A fragment of a table row: pipes, digits, currency and punctuation, but no
# words. On its own it means nothing.
_CELL_FRAGMENT = re.compile(r"^[\s|$()%.,\-–—*†\d]*$")


def _rejoin_split_rows(text: str) -> str:
    """Put table rows back together.

    Some filing agents emit one row per CELL, so a row arrives as a label
    followed by its figures on separate lines:

        | Total stores
        |
        |
        | 6,411

    Read line by line the label has no number and the number has no label, so
    the row is invisible — which is why AutoZone's worldwide 6,411 was missed
    entirely and a US-only figure got published in its place. Merging a label
    line with the value-only lines that follow restores the row.
    """
    out: List[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if (out and stripped and _CELL_FRAGMENT.match(stripped)
                and re.search(r"[A-Za-z]", out[-1])):
            # Value-only fragment: it belongs to the label line above.
            out[-1] = out[-1].rstrip() + " " + stripped
        else:
            out.append(line)
    return "\n".join(out)


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
    # Match a whole comma-grouped number. The old pattern was length-capped, so
    # it chopped large figures into plausible small ones instead of rejecting
    # them: '1,234,567' came back as [1234, 567] and Macerich's square footage
    # '21,652,000' as 21,652, which then sailed through every plausibility test
    # as a store count. Parse it whole, then let MAX_PLAUSIBLE_COUNT judge it.
    found = [int(x.replace(",", ""))
             for x in re.findall(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b", line)]
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

# "1,212 stores as of January 31, 2026" — the count, its unit and the date it
# is stated for, with no verb needed. Burlington, Kroger and Dollar Tree all
# phrase it this way, and requiring a cue verb in front missed every one.
_PROSE_AS_OF = re.compile(
    rf"(?i)\b([\d,]{{1,7}})\s+(?:[A-Za-z][A-Za-z\.\-\u2019']{{0,16}}\s+){{0,3}}"
    rf"({UNIT_RE})\b[^.;]{{0,40}}?\bas of\b[^.;]{{0,30}}?\d{{4}}")

# The same fact with the date FIRST: "As of January 29, 2022, we operated a
# total of 1,923 stores". Ross Stores writes it this way, and matching only the
# number-then-date order left the real total ranked below a sibling brand's 295.
_PROSE_AS_OF_LEADING = re.compile(
    rf"(?i)\bas of\b[^.;]{{0,40}}?\d{{4}}[^.;]{{0,20}}?"
    # A verb of operation must sit between the date and the count, otherwise the
    # pattern drifts to any number later in the sentence \u2014 that drift made
    # Floor & Decor report 500 instead of 160.
    rf"\b(?:operated|operates|operate|had|owned)\b[^.;]{{0,70}}?"
    rf"\b([\d,]{{1,7}})\s+"
    rf"(?:[A-Za-z][A-Za-z\.\-\u2019']{{0,16}}\s+){{0,3}}({UNIT_RE})\b")

_PROSE = re.compile(
    # 'opened' was in this list and is not a fleet verb: McDonald's "the Company
    # opened 1,576 restaurants and closed 1,332" published a 1,576-restaurant
    # McDonald's against a real 40,275. Openings are activity during the year.
    rf"(?i)\b(?:operated|operate|operates|had|owned\s+and\s+operated|with|"
    rf"consisted\s+of|comprised\s+of|totaling|operator\s+of|"
    rf"operated,[^,]{{0,60}},|store\s+base\s+(?:to|of)|grown\s+to)\s+"
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

# What a table of floor area calls itself. Used to veto a bare 'Total' row
# whose table counts square feet rather than shops.
_AREA_CAPTION = re.compile(
    r"(?i)\b(?:square\s*(?:foot|feet|footage)|sq\.?\s*ft|selling\s+space|acres)\b")

# Language about where the company WANTS to get to, not where it is.
_ASPIRATIONAL = re.compile(
    r"(?i)\b(?:opportunity|potential|target|targeting|aspiration|long[\s-]*term|"
    r"eventually|we believe we can|runway|capacity for|could support|goal of|"
    # Warby Parker's "our retail footprint has room to expand in the U.S. to
    # 41,000" is a market-size claim, not a fleet of 41,000 shops.
    r"room\s+to\s+(?:expand|grow)|headroom|addressable)\b")

# Hedging that marks a number as rounded rather than counted.
_ROUNDED_QUALIFIER = re.compile(
    r"(?i)\b(?:approximately|about|more\s+than|over|nearly|roughly|in\s+excess\s+of)\b")

# A number that is a share of something, not a count of anything. Casey's
# "Approximately 3% of total revenue ... relates to the wholesale fuel network"
# reached the top of its filing as a fleet of 3 stores.
_PERCENTAGE = re.compile(r"^\s*(?:%|percent\b)")

# A count of PLACES the company trades in, not of the sites it trades from.
# Boot Barn's "the number of stores in each of the 36 states in which we
# operated" published 36 against a real fleet of 273.
_GEOGRAPHY_NOUN = re.compile(
    r"^\s*(?:states|countries|provinces|territories|jurisdictions|markets|"
    r"regions|time\s+zones)\b")


# A site the company works FROM, not one the public shops AT. The unit words
# are deliberately broad ('centers', 'facilities'), so the modifier in front of
# them is what separates a shop from a warehouse — Casey's "operates three
# distribution centers" was published as a fleet of 3 against ~2,900 stores.
_SUPPORT_SITE = re.compile(
    r"(?i)\b(?:distribution|fulfilment|fulfillment|processing|manufacturing|"
    r"production|packaging|data|call|contact|service|support|research|"
    r"development|corporate|administrative|office|warehouse\s+and\s+office|"
    r"transportation|logistics|maintenance|training)\s+"
    r"(?:and\s+\w+\s+)?(?:centers?|centres?|facilit(?:y|ies)|locations?|sites?)\b"
    # 'Units' is a share award as often as it is a shop. Wolverine's
    # "restricted stock or units ... under its stock-based compensation plan"
    # produced a fleet of 60,880.
    r"|\b(?:restricted|performance|deferred|stock|share|phantom)\s+"
    r"(?:stock\s+)?(?:or\s+)?units?\b")


def counts_something_else(body: str, end: int) -> bool:
    """Does the text right after a number disqualify it as a fleet count?

    Cheaper and far more reliable than trying to describe every sentence a
    number can appear in: look at what the number is a quantity OF.
    """
    tail = body[end:end + 24]
    return bool(_PERCENTAGE.match(tail) or _GEOGRAPHY_NOUN.match(tail))


# Somebody else's number. Group 1 Automotive quotes the National Automobile
# Dealers Association on how many dealerships exist in the whole United States
# — 16,396 — which is four orders of magnitude above its own 180.
_THIRD_PARTY = re.compile(
    r"(?i)\b(?:according\s+to|as\s+reported\s+by|estimates?\s+by|"
    r"(?:national|american|international)\s+\w+\s+association|industry[\s-]*wide|"
    r"per\s+the\s+\w+\s+association)\b")


# The filer talking about itself, as opposed to a peer or a former employer.
_SELF_REFERENCE = re.compile(r"(?i)\b(?:we|our|us|the\s+company|the\s+registrant)\b")

# A total that covers only PART of the company. AutoZone reports both
# 'Total Domestic stores 5,772' and 'Total stores 6,411'; Best Buy reports
# 'Total Domestic segment stores 991' against a real fleet of 1,159. Publishing
# a part as if it were the whole understates the company, so an unqualified
# total must always win over a qualified one.
_PARTIAL_SCOPE = re.compile(
    r"(?i)\b(?:domestic|international|u\.?s\.?|united\s+states|canada|mexico|"
    r"europe|asia|company[\s-]*(?:operated|owned)|franchis(?:ed|e)|"
    # 'licensed' alone missed lululemon's 'Total locations operated by third
    # parties under license and supply arrangements | 45', which then outranked
    # its own 811 company-operated stores.
    r"licens(?:e|ed|ing)|third\s+part(?:y|ies)|"
    r"segment|wholly[\s-]*owned|north\s+america|abroad|foreign)\b")


def data_cells(line: str) -> List[int]:
    """The numbers in a table row's DATA cells, ignoring its label cell.

    A footnote marker printed without brackets lives inside the label —
    Lowe's writes '| Number of stores 4 | 2,129 | 1,857 |', where the 4 is
    footnote four and 2,129 is the fleet. Reading the label's digits as data
    published a four-store Lowe's.

    It also discards rows whose every cell carries words, which is what a
    maturity schedule looks like: Macy's '| Total | Less than 1 Year | 1 - 3
    Years |' yielded a three-store Macy's.
    """
    cells = [cell.strip() for cell in line.split("|")]
    labelled = next((index for index, cell in enumerate(cells)
                     if re.search(r"[A-Za-z]", cell)), None)
    if labelled is None:
        return numbers_in(line)
    numbers: List[int] = []
    for cell in cells[labelled + 1:]:
        if not cell or re.search(r"[A-Za-z]", cell):
            continue
        numbers.extend(numbers_in(cell))
    return numbers


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
    # Find a cell that totals the ones before it, scanning from the right. The
    # total is not always the LAST cell — Texas Roadhouse's row reads
    # '666 | 118 | 784 | 8' where the trailing 8 is a footnote marker, and
    # testing only the final cell returned the company-owned count instead of
    # the 784 system-wide total.
    for position in range(len(cells) - 1, 1, -1):
        head, candidate = cells[:position], cells[position]
        if abs(sum(head) - candidate) <= max(1, candidate * 0.01):
            return candidate
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
        # Walk back over NON-EMPTY lines. Counting raw lines fell short on
        # McDonald's, where blank lines between table rows meant the eight-line
        # window held only segment rows and never reached the caption naming
        # what the table counts.
        # The caption is the NEAREST preceding line that names a unit — for
        # Best Buy that is '| Number of stores: |', sitting directly above
        # 'Domestic 991 / International 168 / Total 1,159'.
        #
        # Judge only that line. Scanning a whole window instead made an
        # unrelated 'Total assets' row further up veto the table, because the
        # exclusion list quite reasonably contains 'assets'. The exclusion is
        # about what the CAPTION counts (FND's square-footage table), not about
        # whatever happens to sit above it.
        preceding = [s for s in line_starts if s < position]
        seen = 0
        for start in reversed(preceding):
            end = body.find("\n", start)
            line = body[start:end if end > 0 else len(body)].strip()
            if not line:
                continue
            seen += 1
            # Stop at the first line that declares what a table counts —
            # whether that is units or floor area. Best Buy stacks the two:
            # 'Number of stores:' ... 'Total 1,159' then 'Retail square footage
            # (in thousands):' ... 'Total 42,108'. The area caption names no
            # unit, so scanning past it inherited the stores caption above and
            # published square feet as a store count.
            # Only AREA words disqualify a caption. Reusing the full exclusion
            # list here let an unrelated 'Revenues' or 'Total assets' row a few
            # lines above veto a perfectly good store table — that list is
            # about what a ROW contains, not what a table counts.
            area = _AREA_CAPTION.search(line)
            unit = re.search(rf"(?i)\b{UNIT_RE}\b", line)
            if area:
                return False
            if unit:
                return True
            if seen >= 12:
                break
        return None      # inconclusive — let the sibling-sum test decide

    def totals_its_siblings(position: int, value: int) -> bool:
        """Does this bare 'Total' equal the rows directly above it?

        McDonald's counts read 'U.S. 13,706 / International Operated 10,845 /
        International Developmental 20,805 / Total 45,356' with no caption
        naming restaurants anywhere near. The arithmetic identifies the row on
        its own: 13,706 + 10,845 + 20,805 = 45,356 exactly.
        """
        preceding = [s for s in line_starts if s < position]
        parts: List[int] = []
        for start in reversed(preceding):
            end = body.find("\n", start)
            line = body[start:end if end > 0 else len(body)].strip()
            if not line:
                continue
            if "|" not in line or not re.search(r"[A-Za-z]", line):
                break
            cells = numbers_in(line)
            if not cells:
                break
            parts.append(cells[0])
            if len(parts) >= 8:
                break
            if abs(sum(parts) - value) <= max(1, value * 0.01):
                return True
        return abs(sum(parts) - value) <= max(1, value * 0.01) if parts else False

    for match in _TOTAL_ROW.finditer(body):
        line = " ".join(match.group(0).split())
        # A real table row has cells. Without this, prose such as 'we operated a
        # total of 1,923 stores, of which 1,628...' is parsed with table rules
        # and the leading date ('At January 29') becomes the count.
        if "|" not in line:
            continue
        in_row = bool(re.search(rf"(?i)\b{UNIT_RE}\b", line))
        # An area caption does NOT veto a row that names units itself. Tried
        # that to catch Haverty's '| Total stores | 27,200 |' (square feet
        # under a stores label) and it cost more than it saved: Destination XL
        # states '| Total Stores | 288 | 1,950 |' — stores AND floor area in
        # one table — and the veto threw away its verified 288. Haverty's is
        # left to the series tests instead; losing a correct value to catch a
        # wrong one is a bad trade.
        if not in_row:
            # Fall back to the table's caption ONLY for a row labelled bare
            # 'Total'. Anything more specific names its own quantity — 'Total
            # paid members', 'Total operating expenses' — and must not inherit
            # the caption's meaning.
            label = line.split("|")[1] if line.count("|") > 1 else line
            if re.fullmatch(r"[\s|]*total[\s|]*", label, re.I) is None:
                continue
            verdict = header_mentions_units(match.start())
            if verdict is False:
                continue
            if verdict is None:
                provisional = row_total(numbers_in(line))
                if provisional is None or not totals_its_siblings(match.start(),
                                                                  provisional):
                    continue
        if any(bad in line.lower() for bad in NON_RETAIL_DIMENSIONS):
            continue
        if _SUPPORT_SITE.search(line):
            continue
        if _NOT_A_FLEET_ROW.search(line) or is_year_header(line):
            continue
        # Money and percentages are never a store count. Best Buy's financial
        # summary carries bare 'Total | $ | 7,542' rows that otherwise satisfy
        # every structural test a store total does.
        if "$" in line or "%" in line:
            continue
        cells = data_cells(line)
        # Roll-forward: 'Total stores | 1,026 | 5 | (22) | 1,009' — beginning,
        # opened, closed, ending. The fleet is the last cell.
        total = cells[-1] if (_ROLLFORWARD.search(line) and len(cells) >= 3) \
            else row_total(cells)
        # A fleet does not shrink by 99% in a year, so a "roll-forward" whose
        # ending count bears no relation to its opening one is not a
        # roll-forward of the fleet. Yum's '| Total | 18,381 | 17,639 | 18,703
        # | 4 | (6) |' ended on 4 restaurants.
        if total is not None and cells and max(cells) > max(total, 1) * 20:
            continue
        if total is None:
            continue
        # The chosen cell must not be a quantity of somewhere rather than of
        # something. Boot Barn's table caption — 'the number of stores in each
        # of the 36 states in which we operated' — published 36 stores against
        # a fleet of 273.
        # A fleet is a whole number of shops. Home Depot's bare
        # '| Total | | 302.5 |' is square footage in millions, and reading the
        # integer part of it published a 302-store Home Depot.
        # Either side of the point counts: Sherwin-Williams prints '| Total |
        # .24 |' (a per-share figure), whose 24 read as a 24-store fleet.
        if re.search(rf"\b{re.escape(f'{total:,}')}\.\d|\b{total}\.\d"
                     rf"|\.{total}\b", line):
            continue
        if any(counts_something_else(line, at + len(written))
               for written in ({f"{total:,}", str(total)})
               for at in [line.find(written)] if at >= 0):
            continue
        unit = re.search(rf"(?i)\b({UNIT_RE})\b", line)
        out.append(StoreCount(
            ticker="", fiscal_year=0, value=total, source_rule="L2_table",
            confidence="high", evidence=line[:400],
            unit_word=unit.group(1).lower() if unit else None))

    # ── Segments that add up to a fleet ──────────────────────────────────
    # Some retailers never print a worldwide total in one row; they print a
    # 'Total stores' row per segment table. TJX FY2026 shows 3,790 / 589 / 747
    # / 88 across four such tables, and the real worldwide fleet is their sum,
    # 5,214 — which is why a US-only 3,790 was being published.
    #
    # A sum is only trusted when the FILING ITSELF states it somewhere. TJX
    # writes '5,214' in its store-growth table, so the arithmetic is confirmed
    # by the document. AutoZone's 'Total Domestic 5,772' + 'Total 6,411' would
    # sum to 12,183, which appears nowhere — so that sum is correctly refused,
    # and the overlapping rows cannot double-count.
    # Group by the row's own label: the segment tables repeat the SAME label
    # ('Total stores') once per segment, so identical labels identify the
    # sibling set exactly and nothing unrelated gets swept in.
    by_label: Dict[str, List[int]] = {}
    for candidate in out:
        if candidate.source_rule != "L2_table" or not candidate.value:
            continue
        evidence = candidate.evidence or ""
        label = evidence.split("|")[1].strip().lower() if evidence.count("|") > 1 \
            else evidence[:40].strip().lower()
        by_label.setdefault(label, []).append(candidate.value)

    for label, values in by_label.items():
        distinct = sorted(set(values))
        if len(distinct) < 2:
            continue
        combined = sum(distinct)
        if not (max(distinct) < combined <= MAX_PLAUSIBLE_COUNT):
            continue
        stated = (re.search(rf"(?<![\d,]){combined:,}(?![\d,])", text)
                  or re.search(rf"(?<![\d,]){combined}(?![\d,])", text))
        if not stated:
            continue
        out.append(StoreCount(
            ticker="", fiscal_year=0, value=combined,
            source_rule="L2_table", confidence="high",
            # Wording matters: this string is scored later, and an earlier
            # version used the word "segment", which tripped the partial-scope
            # penalty and made the correct worldwide sum lose to a part of it.
            evidence=(f"'{label}' repeated per table: "
                      + " + ".join(f"{v:,}" for v in distinct)
                      + f" = {combined:,}, a figure stated in the filing"),
            unit_word="stores"))

    for match in list(_PROSE_AS_OF.finditer(body)) + \
            list(_PROSE_AS_OF_LEADING.finditer(body)):
        try:
            value = int(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if not (0 < value <= MAX_PLAUSIBLE_COUNT):
            continue
        window = body[max(0, match.start() - 160):match.end() + 40]
        # A statement carrying its own as-of date is a fact about this filer,
        # even when it names itself rather than saying "we" — Kroger writes
        # "Kroger operated ... 2,697 supermarkets", which the pronoun test
        # rejected outright.
        if _ACTIVITY_ONLY.search(window) or counts_something_else(body, match.end(1)) \
                or _SUPPORT_SITE.search(match.group(0)):
            continue
        if _THIRD_PARTY.search(body[max(0, match.start() - 200):match.end()]):
            continue
        out.append(StoreCount(
            ticker="", fiscal_year=0, value=value, source_rule="L2_prose_asof",
            confidence="medium",
            evidence=" ".join(window.split())[:400],
            unit_word=match.group(2).lower()))

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
        if _ACTIVITY_ONLY.search(lead_in) or counts_something_else(body, match.end(1)) \
                or _SUPPORT_SITE.search(match.group(0)):
            continue
        if _THIRD_PARTY.search(body[max(0, match.start() - 200):match.end()]):
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

    # ── Two whole-filing sanity passes ───────────────────────────────────
    # A count that equals the year being reported on is a date that survived
    # the column tests. Duluth Trading published fleets of 2,016 and 2,018 in
    # exactly those fiscal years, against a real fleet of about 60. Restricting
    # the test to the filing's OWN year keeps a genuine ~2,000-store fleet safe.
    stated = fiscal_year_of(body)
    if stated:
        # A 10-K prints two or three comparative years, so the stray date can be
        # a couple of years back — Duluth's FY2018 filing published 2,016.
        out = [c for c in out
               if not (stated[0] - 2 <= (c.value or 0) <= stated[0] + 1)]

    # A table row that never names its units is only worth considering when the
    # filing offers nothing that does. Dollar Tree's bare '| Total | 77,399 |'
    # is retail square footage, and Deckers' 82,410 the same — both outranked
    # rows that plainly counted stores.
    named = [c for c in out if c.source_rule == "L2_table" and c.unit_word]
    if named:
        out = [c for c in out if c.source_rule != "L2_table" or c.unit_word]

    # An unnamed 'Total' row can still prove it totals its own table's rows —
    # but that only proves it is A total, not a total of SHOPS. Floor & Decor's
    # 23,447 is square footage by state and adds up perfectly. So when anything
    # in the filing does name its units, an unnamed row far above it is
    # measuring something else: 23,447 against a stated 221 stores is not the
    # same company's fleet.
    ceiling = max((c.value or 0) for c in out if c.unit_word) if any(
        c.unit_word for c in out) else 0
    if ceiling:
        out = [c for c in out if c.unit_word or (c.value or 0) <= ceiling * 4]

    return out


def rank_candidates(candidates: List[StoreCount],
                    prior_value: Optional[int] = None) -> List[StoreCount]:
    """Every candidate, best first.

    The ranking used to be private to choose_best, but the LLM window builder
    needs it too: it seeds the excerpts it sends from where the parser found
    its strongest rows, and reading order is no substitute — Walmart's 'Total
    retail units' row sits at 41% of the document, behind a dozen weaker
    candidates that filled every slot ahead of it.

    Tables beat prose (a total row is a stated fact; prose is often rounded or
    talks about a subset). Within a rule, the value repeated most often wins —
    a real total tends to be restated. `prior_value` breaks remaining ties
    toward continuity with the previous year, which is what a store fleet does.
    """
    if not candidates:
        return []

    def evidence_quality(candidate: StoreCount) -> int:
        text = (candidate.evidence or "").lower()
        points = 0
        # A part of the company never outranks the whole of it, however
        # well-labelled the part is.
        label = text.split("|")[1] if text.count("|") > 1 else text[:70]
        if _PARTIAL_SCOPE.search(label):
            points -= 5
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
        # A row that never names what it counts is the weakest evidence there
        # is. Bath & Body Works' bare '| Total | | 57,157 |' is a square-footage
        # table; nothing in the row says otherwise, so it must lose to any row
        # that does name its units.
        elif not candidate.unit_word:
            points -= 3
        return points

    def score(candidate: StoreCount) -> tuple:
        # A sentence that states the count AS OF a date is a fact about the
        # year end. A sentence without one is often a rounded summary
        # ("approximately 2,700 supermarkets") or an ambition ("2,000 store
        # opportunity"), and ranking prose purely by magnitude let those beat
        # Kroger's exact 2,697 and Burlington's exact 1,212.
        rule_rank = {"L2_table": 3, "L2_prose_asof": 2,
                     "L2_prose": 1}.get(candidate.source_rule, 0)
        quality = evidence_quality(candidate)
        value = candidate.value or 0
        # Ambition and rounding are demoted, not vetoed. Zeroing the rank threw
        # away Ross Stores' correct 'we operated a total of 1,923 stores'
        # because the paragraph around it also discussed growth opportunity —
        # the sentence was a fact, its neighbourhood was strategy.
        if _ASPIRATIONAL.search(candidate.evidence or ""):
            quality -= 6
            # Within prose, magnitude decides before quality — so a quality
            # penalty alone never bit, and Warby Parker's "room to expand to
            # 41,000" beat its own 161 shops purely by being larger. Dropping a
            # tier puts it behind any factual sentence. One tier, not to zero:
            # zeroing the rank threw away Ross Stores' correct 1,923.
            rule_rank = max(rule_rank - 1, 0)
        # 'approximately 2,700 supermarkets' is a rounded restatement of an
        # exact figure stated elsewhere; Kroger's real count is 2,697.
        if value % 100 == 0 and _ROUNDED_QUALIFIER.search(candidate.evidence or ""):
            quality -= 4
            # As with aspirational prose, a quality penalty alone never bit,
            # because prose is ordered by magnitude before quality — and a
            # rounded figure is always the larger one. That is how Kroger
            # published "approximately 2,700 supermarkets" over its own exact
            # 2,697, Chipotle 4,000 over 4,056 and Levi's 1,300 over 1,231.
            rule_rank = max(rule_rank - 1, 0)
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
        # Tables carry precise labels, so evidence decides among them; prose
        # has no labels, so within prose the magnitude does. Keep this in step
        # with the rank map above — when tables moved from 2 to 3 this test
        # silently started ranking tables by value and CVS began publishing
        # its beginning-of-year count.
        if rule_rank == 3:
            return (rule_rank, quality, value, continuity)
        return (rule_rank, value, quality, continuity)

    return sorted(candidates, key=score, reverse=True)


def choose_best(candidates: List[StoreCount],
                prior_value: Optional[int] = None) -> Optional[StoreCount]:
    """The single best candidate, or None. Tables beat prose (a total row is a
    stated fact; prose is often rounded or talks about a subset), and
    `prior_value` breaks remaining ties toward continuity with last year."""
    ranked = rank_candidates(candidates, prior_value)
    return ranked[0] if ranked else None


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
