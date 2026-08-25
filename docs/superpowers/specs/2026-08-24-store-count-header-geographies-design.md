# "Store Count (Worldwide)" where the Segments tab names countries

**Date:** 2026-08-24
**Reported by:** internal user, on `/market_data` → TJX → **Additional Data**
**Files touched:** `app/data/repository.py`, `tests/test_segment_totals.py`

---

## 1. The report

TJX → Additional Data showed **"Store Count (Worldwide, if Applicable)"** while its
Segments tab listed four geographies (Australia, Canada, Europe, United States).
The header is supposed to name those geographies when the company has no
per-country store breakdown.

## 2. Root cause

`_build_geo_segment_members` read **only the geographic `Revenues` metric**:

```python
revenues = (res.get("geo_segments") or {}).get("Revenues") or {}
if not revenues:
    return []
```

TJX reports **no geographic revenue at all** — its Geographic Segments table is
Assets only. So the list came back empty and the header fell to the worldwide
fallback. Its cache file held `{"members": []}` to prove it.

Not a regression from the segments work: the restriction was deliberate, and its
reason is sound — asset-location countries are distribution hubs rather than
markets, and Nike files Belgium there, which would read as a Nike retail market.

## 3. The fix

Revenues is still preferred, so the Nike case is unchanged: wherever a filer
reports geographic revenue, asset locations are ignored. Only when Revenues has
no members do the other metrics get a turn — Assets, then Operating Profit,
CapEx, D&A. Best available naming rather than none.

The complete fallback chain the header now walks:

| Step | Condition | Header |
|---|---|---|
| 1 | per-country store rows cover every year | section hidden — Stores by Country names them itself |
| 2 | per-country rows exist but partial | "Worldwide, if Applicable" |
| 3 | geographic **Revenues** members | those names |
| 4 | else geographic **Assets** → Operating Profit → CapEx → D&A | those names |
| 5 | no geographic segments at all | "Worldwide, if Applicable" |

Both the on-screen header (`_store_count_header_cell`) and the Excel export
(`store_count_geo_label`) read the same builder, so they cannot disagree.

## 4. Member labels — three fixes found while verifying

Rebuilding the cache surfaced label artifacts, each fixed in
`_member_display_map` and pinned by a test:

| Filed as | Was displayed | Now | Why |
|---|---|---|---|
| `China Mainland` / `China Mainland Segment` (LULU) | `China Mainland Segment` | `China Mainland` | "Segment" is an element-naming artifact; newest-filing-wins picked it up |
| `Other` / `Other.` (CAL, KD) | `Other.` | `Other` | the tiebreak fell through to alphabetical order, which prefers the full stop |
| `Outside U.S.` (LLY) | `Outside US` | `Outside U.S.` | a stop after a **single letter** is an abbreviation, not stray punctuation |

The last is the subtle one: the rule distinguishes `[A-Za-z]{2,}\.$` (a whole word
then a stop — an artifact) from a stop after one letter (part of the name).

## 5. Cache

`geo_segment_members` is a disk cache under `edgar_cache/geo_segment_members/`,
read in the render and rebuilt in the background on a miss or a stale file, so a
render never blocks. Existing entries were 23–33 days old and predated the
segment-member work — ANF's still read `Emea` alongside `Europe`. All 428 tickers
were rebuilt with the fixed builder.

## 6. Verification

Fleet rebuild: **428 tickers, 0 errors. 298 now carry geography names; 130 carry
none.** The 130 were checked, not assumed — of the eight that previously cached
empty, DG, KSS, TGT and ULTA report business segments only and FL and ROST have
no segment data at all, so "Worldwide, if Applicable" is correct for them. Only
TJX and MCD were genuinely wrong, and both now name their geographies.

Live UI on the STG database:

| Ticker | Header |
|---|---|
| TJX | `Store Count (United States, Europe …)` — Canada and Australia in the expander |
| AAP | `Store Count (United States, Canada)` |
| BURL | `Store Count (United States)` |
| ANF | `Store Count (Americas, United States …)` |
| LULU | `Store Count (Americas, United States …)` |
| CAL | `Store Count (United States, Far East …)` |
| TGT | `Store Count (Worldwide, if Applicable)` — correct, no geographic segments |

LLY, PVH and MCD render no store-count section at all (no store data); the server
log is clean for them, so there is nothing to label.
