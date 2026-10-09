"""
Management Changes
==================
Turns the Form 8-K Item 5.02 text we already store into one row per person
event: who left or joined, every title they held, when it was announced and
when it takes effect, and who succeeded whom. Regex only — no LLM, no download.

Source: coreiq_company_events rows written by scripts/generate_key_developments_v4.py
for 8-K Item 5.02 ("Departure of Directors or Certain Officers; Election of
Directors; Appointment of Certain Officers; Compensatory Arrangements of Certain
Officers"). The full item text sits in `situation`; nothing else is needed.

Item 5.02 has six sub-items, and only four are changes:
  (a) director resigns / declines re-election over a disagreement
  (b) CEO, President, CFO, CAO, COO or a director retires, resigns, is
      terminated, or will not stand for re-election
  (c) a new CEO / President / CFO / CAO / COO is appointed
  (d) a new director is elected other than at the annual meeting
  (e) a compensatory arrangement is adopted or amended  -> NOT a change
  (f) a late salary/bonus figure for the proxy           -> NOT a change
A filing that only carries (e)/(f) yields a single "Compensation Arrangement"
row so it is not mistaken for a departure.

Read-only. The frame is materialized like people_universe, so the 27 s fetch of
~9k long text rows is paid once per data change, never per page view.

See docs/superpowers/specs/2026-10-09-management-changes-8k-502-design.md.
"""

import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

from core.database import db_manager
from data.people_service import classify_role, person_key

try:
    from utils.server_logger import log_structured_error, log_timing
except ImportError:  # pragma: no cover - logging is never allowed to break a page
    def log_structured_error(exc, **kwargs): pass
    def log_timing(*a, **kw): pass


# =============================================================================
# TEXT CLEANUP
# =============================================================================

# Headings vary: the full four-part title, the pre-2006 "Principal Officers"
# wording, or just "Item 5.02." — greedy to the last "Officers" of the heading.
_ITEM_HEADING = re.compile(
    r"Item\s*5\.02[\s.:\-–—]*(?:Departure\s+of\s+Directors[^.]{0,240}Officers\.?)?", re.I)
# Where the 5.02 body ends: the next item, the safe-harbor boilerplate, or the
# signature block. The stored text often runs on into Item 7.01 / 9.01.
_ITEM_END = re.compile(
    r"\bItem\s*(?!5\.02)\d{1,2}\.\d\d\b|Forward[- ]Looking\s+Statements|Cautionary\s+(?:Note|Statement)|"
    r"\bSIGNATURES?\b|Pursuant to the requirements of the Securities Exchange Act", re.I)


# Term-of-office boilerplate: "until her successor is duly elected and qualified,
# or until her earlier death, resignation or removal" names no real event.
_BOILERPLATE = re.compile(
    r"(?:,\s*)?(?:and\s+|or\s+)?(?:until|upon)\s+[^.;]{0,40}?successors?\s+[^.;]{0,40}?(?:elected|appointed|named|qualified)"
    r"(?:\s+and\s+(?:has\s+)?qualified)?"
    r"|(?:,\s*)?(?:or\s+)?(?:until\s+)?(?:the\s+)?(?:his|her|their|such\s+director[’']s)?\s*earlier(?:\s+of)?\s+(?:his|her|their\s+)?(?:respective\s+)?(?:death|resignation)[^.;]{0,90}?(?:removal|disqualification)", re.I)
_QUOTES = str.maketrans({"\x93": "“", "\x94": "”", "\x92": "’"})


def item_body(situation: str) -> str:
    """The Item 5.02 prose alone: header, heading and trailing items removed."""
    text = (situation or "").split("─" * 60, 1)[-1]
    text = text.replace("\xa0", " ").translate(_QUOTES)
    text = re.sub(rf"\bOn(?=(?:{_MONTHS}))", "On ", re.sub(r"\s+", " ", text)).strip()
    # Nicknames break full-name matching: Shiu Leung (Fred) Chan, Charles “Chuck” Robel.
    text = re.sub(r"\s*(?:\([A-Z][a-z]+\)|“[A-Z][a-z]+”|\"[A-Z][a-z]+\")", "", text)
    text = _BOILERPLATE.sub("", text)
    heading = _ITEM_HEADING.search(text)
    if heading:
        text = text[heading.end():]
    return _ITEM_END.split(text, 1)[0].strip(" .")


# Periods that do NOT end a sentence. Protected before splitting.
_ABBREVIATIONS = re.compile(
    r"\b(Mr|Ms|Mrs|Dr|Jr|Sr|Inc|Corp|Co|Ltd|No|St|U\.S|Messrs|Mses|vs|approx|Prof)\.|\b([A-Z])\.(?=\s)")


def split_sentences(text: str) -> List[str]:
    protected = _ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", "\x00"), text)
    parts = re.split(r"(?<=[.;])\s+(?=[(“\"A-Z])", protected)
    return [p.replace("\x00", ".").strip() for p in parts if p.strip()]


# =============================================================================
# DATES
# =============================================================================

_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December")
_DATE = rf"(?:{_MONTHS})\s+\d{{1,2}}(?:\s*,\s*|\s+)\d{{4}}"
_MONTH_YEAR = rf"(?:{_MONTHS})\s+\d{{4}}"

_EFFECTIVE = re.compile(
    rf"(?:effective|effective\s+date\s+of)\s+(?:as\s+of\s+|on\s+|from\s+|in\s+)?(?P<date>{_DATE}|{_MONTH_YEAR})"
    rf"|(?:as\s+of|on|beginning|starting)\s+(?P<date2>{_DATE})\s*\(the\s+[“\"]?\w*\s*(?:Effective|Transition|Start|Employment|Separation|Retirement)",
    re.I)
_EFFECTIVE_NOW = re.compile(r"effective\s+immediately|with\s+immediate\s+effect", re.I)
_EFFECTIVE_MEETING = re.compile(
    r"(?:at|upon|following|immediately (?:prior to|after)|until|as of)\s+the\s+(?:conclusion of the\s+)?"
    r"(?:Company['’]s\s+)?(?:\d{4}\s+)?annual\s+meeting", re.I)
_NOTICE = re.compile(rf"\b(?:On|As of|Effective)\s+(?P<date>{_DATE})\s*[,(]", re.I)


def parse_date(text: Optional[str]) -> Optional[pd.Timestamp]:
    if not text:
        return None
    cleaned = re.sub(r"\s*,\s*", ", ", text.strip())
    for fmt in ("%B %d, %Y", "%B %d %Y", "%B %Y"):
        try:
            return pd.Timestamp(pd.to_datetime(cleaned, format=fmt))
        except (ValueError, TypeError):
            continue
    return None


# =============================================================================
# PEOPLE
# =============================================================================

# A capitalised word that is never part of a person's name in these filings.
_NOT_NAME = set("""
the a an on in of for and or to as at by with from upon effective company company's board
directors director chief officer executive president vice senior group global interim acting
chairman chair chairwoman chairperson independent lead member members committee committees
annual meeting stockholders shareholders inc incorporated corporation corp llc ltd plc co
holdings group's nasdaq nyse sec securities exchange commission item form report current
january february march april may june july august september october november december
general counsel controller treasurer secretary principal accounting financial operating
mr ms mrs dr also following prior previously additionally further in under pursuant during
this that these those his her their its our we he she they it there such each both all any
north america americas united states international us u.s. americas' emea apac europe
compensation nominating governance audit human resources talent leadership development
agreement plan employment separation transition offer letter award awards
act employee income security retirement trust savings deferred stock equity omnibus incentive
internal revenue code section rule regulation exhibit
management finance inventory operations marketing sales merchandising strategy technology supply
chain place stores brands foods retail products services partners capital digital commercial
banking segment division business americas asia pacific emerging markets consumer global
""".split())
_NAME_WORD = r"[A-Z][a-zA-Z'’\-]+"
_NAME = re.compile(
    rf"\b{_NAME_WORD}(?:\s+(?:[A-Z]\.|{_NAME_WORD}|[A-Z]\.\s*[A-Z]\.)){{1,3}}"
    r"(?:,?\s+(?:Jr\.|Sr\.|II\b|III\b|IV\b))?")
_HONORIFIC = re.compile(r"\b(?:Mr|Ms|Mrs|Dr|Messrs)\.?\s+(?P<surname>[A-Z][a-zA-Z\-]+(?:['’][A-Z][a-zA-Z\-]+)?)")


# "death of NAME", "appointed NAME", "replacing NAME": whatever follows is a person.
_NAME_LEAD = re.compile(
    r"(?:death|resignation|retirement|departure|appointment|election|succe\w+|replac\w+|appointed|elected|named|"
    r"promoted|hired|of\s+directors?|director[s,]?)\s+(?:of\s+)?(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?$", re.I)


def _is_name(candidate: str) -> bool:
    words = [w.strip(".,’'") for w in candidate.split()]
    real = [w for w in words if len(w) > 1 and w.lower() not in ("jr", "sr", "ii", "iii", "iv")]
    if len(real) < 2:
        return False
    return not any(w.lower() in _NOT_NAME or w.isupper() and len(w) > 2 for w in real)


def known_people(body: str) -> Dict[str, str]:
    """Surname -> full name for everyone the item introduces.

    Filings name a person in full once and then write "Mr. Surname", so a
    full name whose last word is a surname used with an honorific is a person
    with near certainty. Names never followed by an honorific (rare, mostly
    lists of directors) are kept only if they look like names.
    """
    surnames = {m.group("surname") for m in _HONORIFIC.finditer(body)}
    people: Dict[str, str] = {}
    for m in _NAME.finditer(body):
        name = re.sub(r"^(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)", "", m.group(0)).strip(" ,")
        # "Board appointed John Smith" style leading junk is filtered by _is_name;
        # trim leading non-name words greedily first.
        words = name.split()
        while len(words) > 2 and words[0].lower() in _NOT_NAME:
            words = words[1:]
        name = re.sub(r"[’']s$", "", " ".join(words))
        if not _is_name(name):
            continue
        last = re.sub(r",?\s+(?:Jr\.|Sr\.|II|III|IV)$", "", name).split()[-1].strip(".,")
        lead = body[max(0, m.start() - 40):m.start()]
        if surnames and last not in surnames and not _NAME_LEAD.search(lead):
            continue
        # The first full spelling wins; later ones are usually the same person.
        people.setdefault(last, name)
    return people


def mentions(sentence: str, people: Dict[str, str]) -> List[tuple]:
    """(start, full name) for every person mentioned in the sentence."""
    found = {}
    for surname, full in people.items():
        for m in re.finditer(rf"\b{re.escape(surname)}\b", sentence):
            found.setdefault(full, m.start())
    return sorted((pos, name) for name, pos in found.items())


# =============================================================================
# TITLES
# =============================================================================

_TITLE_START = (r"(?:Interim|Acting|Senior|Executive|Group|Global|Deputy|Assistant|Lead|"
                r"Independent|Non-Executive|Corporate|Vice|President|Chief|Chairman|Chairwoman|"
                r"Chairperson|Chair|Controller|Treasurer|Secretary|General\s+Counsel|"
                r"[Pp]rincipal\s+(?:executive|financial|accounting|operating)\s+officer|"
                r"CEO|CFO|COO|CAO|CTO|CIO|CMO|CHRO|CLO|[Dd]irector|[Mm]ember\s+of\s+the\s+Board)")
_TITLE_WORD = r"(?:[A-Z][A-Za-z&’'\-]*|[Pp]rincipal|executive|financial|accounting|operating|officer|of|the|and|&)"
_TITLE = re.compile(rf"\b(?:Co-)?{_TITLE_START}(?:[\s,]+{_TITLE_WORD}){{0,14}}")
# Trailing words that are the employer or sentence flow, not the title.
_TITLE_TAIL = re.compile(
    r"(?:[\s,]+(?:and|of|the|&|Company|Company’s|Company's|Inc|Corporation|"
    r"Mr|Ms|Mrs|Dr|On|Effective|In|As|The))+$")


_FUNCTION_WORDS = re.compile(
    r"\b(?:marketing|sales|operations|finance|financial|human|investor|engineering|product|strategy|"
    r"communications|accounting|tax|audit|legal|compliance|technology|merchandising|supply|research|"
    r"development|design|planning|business|corporate\s+\w+)\b", re.I)


def _clean_title(raw: str) -> str:
    title = re.sub(r"\s+", " ", raw).strip(" ,;")
    if re.match(r"(?i)(?:member\s+of\s+the\s+board|director\b)", title) and \
            not re.match(r"(?i)director\s+of\s+", title) or \
            re.match(r"(?i)director\s+of\s+", title) and not _FUNCTION_WORDS.search(title):
        return "Director"
    # "Chief Financial Officer of Starbucks Corporation" -> the employer is not
    # part of the title; "Vice President of Merchandising" keeps its "of".
    title = re.split(r"\s+of\s+(?:the\s+)?(?:Company\b|[A-Z][\w&’'\-]*(?:\s+[A-Z][\w&’'\-]*){0,4}\s+(?:Inc|Incorporated|Corp|Corporation|Company|Co|LLC|Ltd|Holdings|plc|Group|Brands)\b)", title)[0]
    title = re.split(r"\s+(?:Mr|Ms|Mrs|Dr)\.?\s", title)[0]
    # Run-in subheadings: "Appointment of X as Chief Accounting Officer On April 29, ..."
    title = re.split(rf"\s+(?:On|The|Effective|As|In|Upon|Following)\s+(?:{_MONTHS}|Company|Board|[A-Z])", title)[0]
    title = re.sub(r"\s+U\s*$", " U.S.", title)
    previous = None
    while previous != title:
        previous = title
        title = _TITLE_TAIL.sub("", title).strip(" ,;")
    return title


def split_titles(title: str) -> List[str]:
    """'Senior Vice President, Chief Accounting Officer and Controller'
    -> ['Senior Vice President', 'Chief Accounting Officer', 'Controller'].
    "President and Chief Executive Officer" splits too; each part stands alone."""
    parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*(?=[A-Z][a-z]+\s+Officer)", title or "")
    return [p.strip() for p in parts if re.search(_TITLE_START, p)]


_BOARD_SEAT = re.compile(
    r"^\s*(?:to|from|on|as\s+(?:a\s+)?member\s+of)\s+(?:the\s+|our\s+|its\s+)?(?:Company[’']s\s+)?Board\b(?![’']s)(?!\s+of\s+Trustees)"
    r"|^\s*as\s+(?:a|an)\s+(?:new\s+)?(?:independent\s+)?(?:non-employee\s+)?(?:Class\s+[IVX]+\s+)?director\b", re.I)
_AFTER_VERB = re.compile(
    rf"^\s*(?:(?:from|of)\s+(?:his|her|their|the)\s+(?:position|role|office|title)s?\s+(?:as|of)\s+|as\s+|to\s+serve\s+as\s+|"
    rf"to\s+(?:the\s+)?(?:newly\s+created\s+)?(?:position|role|office)s?\s+of\s+|to\s+(?:be|become)\s+|to\s+)?"
    rf"(?:the\s+|a\s+|an\s+)?(?:Company[’']s\s+|our\s+|its\s+)?(?:new\s+|next\s+)?(?P<t>{_TITLE.pattern})")
_APPOSITION = re.compile(
    rf"^\s*,\s*(?:age\s+\d+\s*,\s*)?(?:currently\s+|who\s+(?:currently\s+)?(?:serves|served|is\s+serving)\s+as\s+|"
    rf"the\s+|our\s+|its\s+)?(?:(?:Company|Corporation|Bank|Partnership)[’']s\s+)?"
    rf"(?:current\s+|then[- ]current\s+|former\s+|existing\s+)?(?P<t>{_TITLE.pattern})")


_SUCCEED_AS = re.compile(
    rf"\bsucce\w*\s+[^.;]{{0,60}}?\s+as\s+(?:the\s+)?(?:Company[’']s\s+|its\s+|our\s+)?(?P<t>{_TITLE.pattern})")


def title_after(sentence: str, start: int) -> str:
    """Title stated after a verb or name: 'appointed X as CFO', 'resigned from
    her position as COO', 'appointed X to the Board'."""
    tail = sentence[start:start + 260]
    m = _AFTER_VERB.match(tail)
    if m:
        return _clean_title(m.group("t"))
    if _BOARD_SEAT.match(tail):
        return "Director"
    return ""


def title_apposition(sentence: str, name_end: int) -> str:
    """'NAME, the Company's Senior Vice President and CAO, ...' — the title held now."""
    m = _APPOSITION.match(sentence[name_end:name_end + 260])
    return _clean_title(m.group("t")) if m else ""


# =============================================================================
# EVENTS
# =============================================================================

_DEPART = re.compile(
    r"\b(?:resign\w*|retire[sd]?\b|retiring\b|(?:his|her|their|planned|upcoming|intended|announced|anticipated)\s+retirement|"
    r"retirement\s+(?:of|from|as|effective)|step(?:s|ped|ping)?\s+down|depart(?:s|ed|ing|ure)\b|"
    r"(?:was|were|been|has|have|Board|Company)\s+terminated|terminated\s+(?:the\s+)?(?:employment|services)\s+of|"
    r"termination\s+of\s+(?:Mr|Ms|Mrs|Dr)\.?\s|"
    r"separat(?:e|ed|ion)\s+from|will\s+leave|leave\s+the\s+Company|left\s+the\s+Company|"
    r"(?:not|decline\w*|not\s+to)\s+(?:to\s+)?(?:stand|seek|run)\s+for\s+re-?\s?election|"
    r"not\s+be\s+(?:standing|nominated)|cease[sd]?\s+to\s+(?:serve|be)|removed|"
    r"passed\s+away|death\s+of|will\s+not\s+be\s+re-?nominated|no\s+longer)", re.I)
_APPOINT = re.compile(
    r"\b(?:appoint\w*|(?<!re-)(?<!re)(?<!re- )elect(?:ed|ion|s)?\b|named|promot\w*|hired|hire\s+of|"
    r"(?:will|to)\s+succeed|succeed(?:s|ed)\b|assum(?:e|es|ed|ing)\s+the\s+(?:role|position|title|duties|responsibilit)\w*|"
    r"(?:will|to)\s+(?:serve|join|become)|join(?:s|ed)\s+the\s+Company|commence\w*\s+employment)", re.I)
# A sentence about compensation, not about who holds the job.
_COMP = re.compile(
    r"\b(?:base\s+salary|bonus|equity\s+award|restricted\s+stock|RSUs?|PSUs?|stock\s+options?|"
    r"severance|retention|incentive|compensation|offer\s+letter|employment\s+agreement|"
    r"consulting\s+agreement|indemnification|awards?|vest\w*|forfeit\w*|tranche|good\s+reason)\b", re.I)
# Sentences that only describe a career history or a disclosure formality.
_SKIP = re.compile(
    r"\b(?:has\s+served|served\s+as|previously\s+served|prior\s+to\s+(?:joining|his|her|that)|"
    r"from\s+\d{4}\s+(?:to|until|through)|since\s+(?:\d{4}|[A-Z][a-z]+\s+\d{4})|began\s+(?:his|her)\s+career|"
    r"family\s+relationships?|Act\s+of\s+\d{4}|Item\s+404|arrangement\s+or\s+understanding|no\s+disagreement|"
    r"not\s+(?:the\s+)?result\s+of\s+any\s+disagreement|holds?\s+a\s+(?:bachelor|master|B\.|M\.)|"
    r"graduated|degree|recently\s+retired|is\s+the\s+(?:former|retired)|formerly|collectively|Named\s+Executive\s+Officers|"
    r"(?:was|were)\s+(?:promoted|appointed|named)\b[^.]{0,80}\bin\s+(?:19|20)\d{2}\b|"
    r"attached\s+(?:hereto|to\s+this)|incorporated\s+herein\s+by\s+reference|(?:filed|furnished)\s+(?:herewith\s+)?as\s+Exhibit)\b", re.I)
_HYPOTHETICAL = re.compile(
    r"\b(?:in\s+the\s+event|if\s|upon\s+(?:a|any|his|her|their|such)\s|prior\s+to\s+the|qualifying|would|"
    r"subject\s+to|in\s+connection\s+with\s+(?:a|any)|eligible|entitled|provides?\s+for)", re.I)
# Disclosure formalities that name a person but never an event.
_FORMALITY = re.compile(
    r"arrangements?\s+or\s+understandings?|family\s+relationships?|Item\s+40[14]|"
    r"selected\s+(?:to\s+serve\s+)?(?:on|as)|had\s+been\s+vacant\s+since|"
    r"(?:was|were)\s+(?:promoted|appointed|named)\b[^.]{0,80}\bin\s+(?:19|20)\d{2}\b", re.I)
_DISAGREEMENT = re.compile(r"\bnot\s+(?:the\s+)?(?:a\s+)?result\s+of\s+any\s+disagreement|no\s+disagreement", re.I)
_REASON = re.compile(
    r"\bto\s+(?:pursue\s+(?:an?\s+)?(?:other|another|new)\s+\w+(?:\s+\w+)?|spend\s+more\s+time\s+with\s+\w+|"
    r"focus\s+on\s+(?:his|her|their)\s+\w+(?:\s+\w+)?|accept\s+a\s+position\s+\w+(?:\s+\w+){0,3})"
    r"|\bfor\s+(?:personal|health|family)\s+reasons|\bdue\s+to\s+(?:personal|health|family)\s+\w+"
    r"|\bwithout\s+cause|\bfor\s+cause\b|\bmutual(?:ly)?\s+agree\w*", re.I)
_INTERIM = re.compile(r"\b(?:interim|acting)\b", re.I)
_PROMOTE = re.compile(r"\bpromot\w*|\b(?:currently|presently)\s+(?:serves|serving)\b", re.I)


def _departure_type(sentence: str) -> str:
    s = sentence.lower()
    if re.search(r"re-?\s?election|re-?nominat|not\s+be\s+(?:standing|nominated)", s):
        return "Not Standing for Re-election"
    if "retire" in s:
        return "Retirement"
    if "resign" in s:
        return "Resignation"
    if re.search(r"terminat|removed|for cause|without cause", s):
        return "Termination"
    if re.search(r"passed away|death", s):
        return "Death"
    return "Departure"


def _appointment_type(sentence: str, title: str) -> str:
    s = sentence.lower()
    if _INTERIM.search(title) or re.search(r"\b(?:interim|acting)\b", s):
        return "Interim Appointment"
    if "promot" in s:
        return "Promotion"
    if re.search(r"(?<!re-)\belect", s) and re.search(r"director|board|member", title.lower()):
        return "Director Election"
    return "Appointment"


def _person_for_verb(sentence: str, verb: re.Match, found: List[tuple]) -> Optional[tuple]:
    """Which mentioned person the verb is about.

    "appointed / named / elected / promoted / resignation of NAME" -> the name
    right after the verb; otherwise the nearest name before it (the subject:
    "NAME informed the Company of his intention to resign").
    """
    after = [(p, n) for p, n in found if 0 <= p - verb.end() <= 60]
    passive_object = re.match(r"(?:of|\s)", sentence[verb.end():verb.end() + 1] or " ")
    if after and passive_object and re.match(
            r"(?:appoint\w*|elect\w*|named|promot\w*|hire\w*|retirement|resignation|departure|"
            r"termination|death)", verb.group(0), re.I):
        gap = sentence[verb.end():after[0][0]]
        # "resignation of former Class B Director NAME": title words may sit between.
        if re.fullmatch(r"\s*(?:of\s+|by\s+)?(?:(?:former|then|our|the|its|Company[’']s)\s+)?"
                        r"(?:[A-Z][\w\-]*\s+){0,6}(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?", gap) and \
                not re.search(r"\b(?:as|to)\b", gap):
            return after[0]
    before = [(p, n) for p, n in found if p < verb.start()]
    if before:
        return before[-1]
    return after[0] if after else None


def extract_events(situation: str) -> List[dict]:
    """Every person event in one Item 5.02 text. Pure function, no I/O."""
    body = item_body(situation)
    if not body:
        return []
    people = known_people(body)
    notice_default = None
    m = _NOTICE.search(body)
    if m:
        notice_default = parse_date(m.group("date"))
    no_disagreement = bool(_DISAGREEMENT.search(body))

    events: Dict[tuple, dict] = {}
    comp_only = True
    for sentence in split_sentences(body):
        if _FORMALITY.search(sentence):
            continue
        if _SKIP.search(sentence) and not re.search(r"\b(?:will\s+be|has\s+been)\s+(?:appointed|named|elected)\b", sentence):
            continue
        found = mentions(sentence, people)
        if not found:
            continue
        notice = _NOTICE.search(sentence)
        notice_date = parse_date(notice.group("date")) if notice else notice_default
        eff = _EFFECTIVE.search(sentence)
        effective_date = parse_date((eff.group("date") or eff.group("date2"))) if eff else None
        effective_note = ""
        if effective_date is None and _EFFECTIVE_NOW.search(sentence):
            effective_date, effective_note = notice_date, "immediately"
        elif effective_date is None and _EFFECTIVE_MEETING.search(sentence):
            effective_note = "at annual meeting"
        reason = _REASON.search(sentence)

        for kind, rx in (("Departure", _DEPART), ("Appointment", _APPOINT)):
            if kind == "Departure" and _COMP.search(sentence) and _HYPOTHETICAL.search(sentence):
                continue
            for verb in rx.finditer(sentence):
                # "will continue to serve as" is a non-event; "successor" with no
                # name is a search announcement.
                if kind == "Appointment" and re.search(
                        r"continu\w*\s+to\s+serve|search\s+for|cease\w*\s+to|no\s+longer|nominat\w*|stand\s+for\s+election|Committee",
                        sentence[max(0, verb.start() - 30):verb.end() + 45], re.I):
                    continue
                hit = _person_for_verb(sentence, verb, found)
                if not hit:
                    continue
                pos, name = hit
                name_end = _name_end(sentence, pos)
                # "appointed NAME as CFO" reads after the name; "NAME resigned as
                # CFO" / "NAME was named CFO" read after the verb.
                stated = title_after(sentence, name_end if pos > verb.start() else verb.end())
                held = title_apposition(sentence, name_end)
                if kind == "Appointment":
                    if not stated:
                        # "elected NAME, 54, ... to succeed OTHER as Chief Executive Officer"
                        m = _SUCCEED_AS.search(sentence, verb.start())
                        stated = _clean_title(m.group("t")) if m else ""
                    if not stated and not re.search(r"succe", verb.group(0), re.I):
                        # An appointment with no title is usually a committee seat.
                        continue
                    title, previous = stated or held, (held if held and held != stated else "")
                else:
                    title, previous = stated or held, ""
                event_type = _departure_type(sentence) if kind == "Departure" else _appointment_type(sentence, title)
                if kind == "Appointment" and previous and event_type == "Appointment":
                    event_type = "Promotion / Role Change"
                key = (person_key(name), kind)
                record = events.get(key)
                if record is None:
                    record = events[key] = {
                        "person": name, "change": kind, "event_type": event_type,
                        "title": title, "previous_title": previous,
                        "effective_date": effective_date,
                        "effective_note": effective_note, "notice_date": notice_date,
                        "reason": reason.group(0) if reason and kind == "Departure" else "",
                        "evidence": sentence[:600],
                    }
                else:
                    record["title"] = record["title"] or title
                    record["previous_title"] = record["previous_title"] or previous
                    record["effective_date"] = record["effective_date"] or effective_date
                    record["effective_note"] = record["effective_note"] or effective_note
                    if kind == "Departure" and reason and not record["reason"]:
                        record["reason"] = reason.group(0)
                comp_only = False

        # Succession: "X will succeed Y (as TITLE)" links the pair both ways.
        for m in re.finditer(r"\b(?:will\s+|to\s+)?succe(?:ed|eds|eded)\s+(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?(?P<who>[A-Z][\w’'\-]+(?:\s+[A-Z][\w.’'\-]+){0,3})", sentence):
            successor = _person_for_verb(sentence, m, [f for f in found if f[0] < m.start()]) if found else None
            predecessor = next((n for p, n in found if p >= m.start() and p - m.start() < 40), None)
            if successor and predecessor and successor[1] != predecessor:
                app = events.setdefault((person_key(successor[1]), "Appointment"), {
                    "person": successor[1], "change": "Appointment", "event_type": "Succession",
                    "title": title_after(sentence, m.end()), "previous_title": "",
                    "effective_date": effective_date,
                    "effective_note": effective_note, "notice_date": notice_date, "reason": "",
                    "evidence": sentence[:600]})
                app["replaces"] = predecessor
                dep = events.get((person_key(predecessor), "Departure"))
                if dep is not None:
                    dep["replaced_by"] = successor[1]
                comp_only = False

    out = []
    for record in events.values():
        record["age"] = stated_age(body, record["person"])
        record.setdefault("replaces", "")
        record.setdefault("replaced_by", "")
        record["titles"] = " | ".join(split_titles(record["title"])) or record["title"]
        if record["event_type"] in ("Not Standing for Re-election", "Director Election") and not record["title"]:
            record["title"] = "Director"
        record["role"] = _role_of(record["title"], record["evidence"])
        record["is_board"] = record["role"] == "Board"
        # Measured on hand-checked samples (spec): High ~= title AND timing found.
        has_timing = record["effective_date"] is not None or bool(record["effective_note"])
        record["confidence"] = ("High" if record["title"] and has_timing
                                else "Medium" if record["title"] else "Low")
        if record["change"] == "Departure":
            record["no_disagreement"] = no_disagreement
        else:
            record["no_disagreement"] = None
        out.append(record)
    if not out and comp_only and _COMP.search(body):
        out.append({
            "person": "", "change": "Compensation", "event_type": "Compensation Arrangement",
            "title": "", "previous_title": "", "titles": "", "role": "", "is_board": False,
            "effective_date": None, "effective_note": "", "notice_date": notice_default,
            "reason": "", "replaces": "", "replaced_by": "", "no_disagreement": None,
            "confidence": "High",
            "evidence": split_sentences(body)[0][:600] if body else "",
        })
    return out


def stated_age(body: str, person: str) -> Optional[int]:
    """The age a 5.02(c) bio states: "Steve Bramlage, age 49," / "Mr. Bramlage, 49,"
    / "Christine Barone (age: 49)". None when the filing gives none."""
    surname = re.sub(r",?\s+(?:Jr\.|Sr\.|II|III|IV)$", "", person or "").split()
    if not surname:
        return None
    m = re.search(rf"\b{re.escape(surname[-1])}(?:[’']s)?\s*(?:,\s*(?:age\s*:?\s*)?|\(\s*age\s*:?\s*)(\d{{2}})\b",
                  body)
    age = int(m.group(1)) if m else None
    return age if age and 25 <= age <= 90 else None


def _name_end(sentence: str, pos: int) -> int:
    m = re.match(r"(?:(?:Mr|Ms|Mrs|Dr)\.?\s+)?[A-Z][\w’'\-]*(?:\s+(?:[A-Z]\.|[A-Z][\w’'\-]+)){0,3}(?:,?\s+(?:Jr\.|Sr\.|II|III|IV)\b)?", sentence[pos:])
    return pos + (m.end() if m else 0)


def _is_board(title: str, evidence: str) -> bool:
    if title:
        if re.search(r"\b(?:senior|managing|executive|regional|associate)\s+director|director\s+of\b", title, re.I):
            return False
        return bool(re.search(r"\b(?:director|board|chair)", title, re.I)) and \
            not re.search(r"\b(?:officer|president|counsel|controller|treasurer|secretary|executive\s+director)\b", title, re.I)
    return bool(re.search(r"\b(?:the\s+Board|director)\b", evidence, re.I)) and \
        not re.search(r"\bofficer\b", evidence, re.I)


def _role_of(title: str, evidence: str) -> str:
    if not title and _is_board(title, evidence):
        return "Board"
    if title and _is_board(title, evidence):
        return "Board"
    return classify_role(title)


# =============================================================================
# UNIVERSE
# =============================================================================

_EVENTS_QUERY = """
    SELECT event_id, ticker, edgar_filing_date, event_date, source_ref, situation
    FROM coreiq_company_events
    WHERE is_active = 1
      AND event_category IN ('Management Changes', 'Governance')
      AND source = 'SEC EDGAR 8-K'
      AND headline LIKE '%%Item 5.02%%'
"""

_COMPANIES_QUERY = """
    SELECT ticker, COALESCE(NULLIF(name_coresight, ''), name) AS name
    FROM coreiq_companies
"""

CHANGE_COLUMNS = [
    "ticker", "filing_date", "change", "event_type", "person", "title", "titles", "previous_title", "role",
    "is_board", "notice_date", "effective_date", "effective_note", "replaces", "replaced_by",
    "reason", "no_disagreement", "confidence", "age", "role_start", "role_end", "years_in_role",
    "moved_from", "moved_from_title", "moved_from_date",
    "evidence", "source_ref", "event_id",
]


_GENERIC_FIRST = {"american", "general", "united", "first", "national", "international",
                  "global", "new", "big", "the", "dollar", "best", "home", "five", "academy"}


def short_name(name: str) -> str:
    """The word a filing uses for a company: "Lowe's Companies, Inc." -> "Lowe's",
    "The Kroger Co." -> "Kroger", "Dollar General" -> "Dollar General"."""
    text = re.sub(r"^\s*The\s+", "", (name or "").replace("’", "'")).split(",")[0].strip()
    words = text.split()
    if not words:
        return ""
    first = re.sub(r"'s$", "", words[0])
    if first.lower() in _GENERIC_FIRST or len(first) < 3:
        return " ".join(words[:2])
    return first


def mark_moves(df: pd.DataFrame, bodies: Dict[int, str], names: Dict[str, str]) -> pd.DataFrame:
    """Flag an appointment at company B as a move from company A when
      * the same person (person_key) has a departure filed by A, at most a year
        later than B's appointment and never more than five years before it, AND
      * B's own filing names A (the 5.02(c) biography: "served as CFO of Lowe's").
    The second test is what stops two different people who share a name
    ("David Miller") from being reported as one executive changing jobs.
    """
    df = df.copy()
    df["moved_from"] = ""
    df["moved_from_title"] = ""
    df["moved_from_date"] = pd.NaT
    keys = df["person"].map(person_key)
    departures = df[(df["change"] == "Departure") & keys.ne("")]
    by_key = {k: g for k, g in departures.groupby(keys[departures.index])}
    appointments = df[(df["change"] == "Appointment") & keys.ne("")]
    joined = {k: g for k, g in appointments.groupby(keys[appointments.index])}
    for i in df.index[(df["change"] == "Appointment") & keys.ne("")]:
        candidates = by_key.get(keys[i])
        if candidates is None:
            continue
        ticker, when = df.at[i, "ticker"], df.at[i, "notice_date"]
        body = (bodies.get(df.at[i, "event_id"]) or "").replace("’", "'")
        others = candidates[candidates["ticker"] != ticker].copy()
        others["_gap"] = (when - others["notice_date"]).dt.days if pd.notna(when) else None
        others = others[others["_gap"].between(-366, 5 * 366)]
        # The departure closest BEFORE the appointment first, then one filed
        # shortly after it — never an older, unrelated departure from the same
        # company (Denton's 2022 Lowe's row vs his 2026 one).
        others = others.assign(_after=others["_gap"] < 0, _dist=others["_gap"].abs()) \
                       .sort_values(["_after", "_dist"])
        for _, dep in others.iterrows():
            word = short_name(names.get(str(dep["ticker"]), ""))
            if not word:
                continue
            # A job at a third company in between means this is not the move:
            # Denton left Lowe's (2022) for Pfizer, then Pfizer for Nike (2026).
            between = joined.get(keys[i])
            if between is not None and dep["_gap"] > 0 and (
                    (between["ticker"] != ticker) & (between["ticker"] != dep["ticker"])
                    & (between["notice_date"] > dep["notice_date"])
                    & (between["notice_date"] < when)).any():
                continue
            if re.search(rf"\b{re.escape(word)}(?:'s)?\b", body):
                df.at[i, "moved_from"] = str(dep["ticker"])
                df.at[i, "moved_from_title"] = dep["title"]
                df.at[i, "moved_from_date"] = dep["effective_date"] if pd.notna(dep["effective_date"]) else dep["notice_date"]
                break
    return df


def build_changes(rows: List[dict], names: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Flatten stored 5.02 rows into one row per person event.

    `names` (ticker -> company name) enables move detection; without it no
    appointment is flagged as a move."""
    records = []
    for r in rows:
        try:
            events = extract_events(r.get("situation") or "")
        except Exception as exc:  # one odd filing must not sink the frame
            log_structured_error(exc, page="management_changes", component="build_changes",
                                 operation="extract_events", context=str(r.get("event_id")))
            continue
        filed = pd.Timestamp(r.get("edgar_filing_date") or r.get("event_date"))
        for e in events:
            e.update(ticker=(r.get("ticker") or "").strip(), filing_date=filed,
                     source_ref=r.get("source_ref") or "", event_id=r.get("event_id"))
            # The filing date is the announcement when the text gives none.
            e["notice_date"] = e.get("notice_date") or filed
            records.append(e)
    df = pd.DataFrame(records, columns=CHANGE_COLUMNS)
    if df.empty:
        return df
    # The same event is re-filed (8-K/A, or announced then confirmed). Keep the
    # first filing per person, change and effective date.
    # Compensation-only rows carry no person, so they are one per filing (event_id).
    df["_key"] = df["person"].map(person_key).where(df["change"] != "Compensation",
                                                    df["event_id"].astype(str))
    df = (df.sort_values(["filing_date", "event_id"])
            .drop_duplicates(["ticker", "_key", "change", "effective_date"], keep="first")
            .drop(columns="_key"))
    for col in ("filing_date", "notice_date", "effective_date"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in ("ticker", "change", "event_type", "role", "confidence"):
        df[col] = df[col].fillna("").astype("category")
    df = add_tenure(df)
    df = mark_moves(df, {r.get("event_id"): r.get("situation") or "" for r in rows}, names or {})
    return df.sort_values(["filing_date", "ticker"], ascending=[False, True]).reset_index(drop=True)


def add_tenure(df: pd.DataFrame, today: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Role start / end / years for every appointment, and the same pair on the
    departure that closes it.

    A role starts at the appointment's effective date (announcement date when
    none is stated) and ends at the first later departure of the same person
    at the same company, of the same kind (board seat vs officer role). No
    departure -> the role is ongoing and years run to `today`. Only dates the
    filings state are used; a filing YEAR is never taken as a start date.
    Roles that began before our 8-K history (2016) have no appointment row, so
    their tenure is not computed rather than guessed.
    """
    today = today or pd.Timestamp.today().normalize()
    df = df.copy()
    for col in ("role_start", "role_end"):
        df[col] = pd.NaT
    df["years_in_role"] = float("nan")
    people = df["change"].isin(["Appointment", "Departure"])
    when = df["effective_date"].fillna(df["notice_date"])
    key = df["ticker"].astype(str) + "#" + df["person"].map(person_key) + "#" + df["is_board"].astype(str)
    for _, idx in df[people].groupby(key[people]).groups.items():
        group = df.loc[idx].assign(_when=when[idx]).sort_values("_when")
        departures = group[group["change"] == "Departure"]
        for i, row in group[group["change"] == "Appointment"].iterrows():
            start = row["_when"]
            if pd.isna(start):
                continue
            later = departures[departures["_when"] > start]
            end = later["_when"].iloc[0] if len(later) else pd.NaT
            df.at[i, "role_start"] = start
            df.at[i, "role_end"] = end
            df.at[i, "years_in_role"] = round(((end if pd.notna(end) else today) - start).days / 365.25, 1)
            if len(later):
                j = later.index[0]
                df.at[j, "role_start"] = start
                df.at[j, "role_end"] = end
                df.at[j, "years_in_role"] = df.at[i, "years_in_role"]
    return df


def _build_management_changes() -> pd.DataFrame:
    import time as _time
    t0 = _time.perf_counter()
    try:
        rows = db_manager.execute_query_readonly(_EVENTS_QUERY) or []
    except Exception as exc:
        log_structured_error(exc, page="management_changes", component="_build_management_changes",
                             operation="fetch_502_events")
        rows = []
    try:
        names = {str(r["ticker"]): r["name"] for r in
                 db_manager.execute_query_readonly(_COMPANIES_QUERY) or [] if r.get("ticker")}
    except Exception as exc:
        log_structured_error(exc, page="management_changes", component="_build_management_changes",
                             operation="fetch_company_names")
        names = {}
    df = build_changes(rows, names)
    log_timing("MGMT_CHANGES_BUILD", (_time.perf_counter() - t0) * 1000,
               details=f"filings={len(rows)} events={len(df)}")
    return df


@st.cache_data(ttl=21600, show_spinner=False)
def get_management_changes() -> pd.DataFrame:
    """Every 8-K Item 5.02 person event, materialized to disk."""
    from utils.materialize import materialized_or_build
    sources = [{
        "table": "coreiq_company_events",
        "signal": "MAX(event_id)",
        # idx_cat_subtype range — the signature never scans the whole table.
        "where": "event_category IN ('Management Changes', 'Governance')",
    }]
    return materialized_or_build(
        # _v2: age + tenure columns (2026-10-09). Bump on any column change.
        "management_changes_v2", _build_management_changes, sources,
        clear=lambda: get_management_changes.clear(),
    )


def compare_move_pay(moves: pd.DataFrame, people: pd.DataFrame) -> pd.DataFrame:
    """Prior vs new total compensation for each executive move.

    Prior = the person's latest disclosed year at the old company up to the year
    they left; new = their first disclosed year at the new company from the year
    they joined. Matched on (ticker, person_key). A side with no disclosure stays
    blank — a board seat, or a company whose proxy we do not hold, has none.
    """
    out = moves.copy()
    for col in ("prior_pay", "prior_pay_year", "new_pay", "new_pay_year"):
        out[col] = float("nan")
    if people is None or people.empty or out.empty:
        return out
    pay = people.assign(
        _key=people["executive_name"].map(person_key),
        _pay=people["total_compensation"].fillna(people["total_pay"]),
        _year=pd.to_numeric(people["year"], errors="coerce"),
    ).dropna(subset=["_pay", "_year"])
    index = {k: g for k, g in pay.groupby([pay["ticker"].astype(str), pay["_key"]])}
    for i, row in out.iterrows():
        key = person_key(row["person"])
        left = row["moved_from_date"] if pd.notna(row["moved_from_date"]) else row["notice_date"]
        joined = row["effective_date"] if pd.notna(row["effective_date"]) else row["notice_date"]
        before = index.get((str(row["moved_from"]), key))
        if before is not None and pd.notna(left):
            hit = before[before["_year"] <= left.year].sort_values("_year")
            if len(hit):
                out.at[i, "prior_pay"], out.at[i, "prior_pay_year"] = hit["_pay"].iloc[-1], hit["_year"].iloc[-1]
        after = index.get((str(row["ticker"]), key))
        if after is not None and pd.notna(joined):
            hit = after[after["_year"] >= joined.year].sort_values("_year")
            if len(hit):
                out.at[i, "new_pay"], out.at[i, "new_pay_year"] = hit["_pay"].iloc[0], hit["_year"].iloc[0]
    out["pay_change_pct"] = ((out["new_pay"] / out["prior_pay"] - 1) * 100).round(1)
    return out
