# A place is not a line of business

**Date:** 2026-10-03
**Scope:** Business Segments member list (Segments tab + screener)
**Asked for by:** business team, alongside the Multi-Regional fold

---

## The problem

Filers tag geography on business axes. The live cache held 3,586 business
segment members, and these were among the most common:

| Member | Companies |
|---|---|
| International | 27 |
| North America | 20 |
| Europe | 19 |
| Americas | 14 |
| EMEA | 14 |
| United States | 12 |
| Asia | 10 |
| Canada | 10 |

None of them is a line of business. They are geography filed on the wrong axis,
and they belong on the Geographical Segments screen.

## The rule

One function, `geo_hierarchy.names_a_place`, reads the same hierarchy the
Geographical Segments cascade reads, so the two screens cannot disagree about
what counts as a place.

```python
def names_a_place(label: str) -> bool:
    node = node_for_label(label)
    return bool(node) and node.get("node") not in _CATCH_ALL_NODES
```

`_CATCH_ALL_NODES` holds exactly one name: **`Other`**. The workbook files it
under a region, but as a segment name it is the revenue a filer did not break
out. 141 companies use it as a business segment; reading it as geography would
empty their business list. `All Other`, `Unallocated`, `Other (2)` and
`Other operations` all resolve to that node and are protected with it.

## Where it is applied

| File | Why |
|---|---|
| `repository.py::_classify_member` | **The root.** Its own docstring calls it the single source of truth for the annual builder, the quarterly builder and the screening cache. A place on a business axis returns `None` and never enters any of them. |
| `screening_service.py::read_segment_member_options_cache` | The cache is a table that rebuilds on its own schedule. Filtering on read means the dropdown is right now, not after the next rebuild. |
| `screening_service.py::read_segment_values_cache` | Same reason, for the values — otherwise a stale row reaches the results grid as a "United States" business column. |

## What it removes

**85 of 3,586 members.** 3,586 → 3,501.

The dropdown's top now reads: Other, Retail, Product, Services, Wholesale,
All Other, Service, Products, Other Revenue. International, North America and
Europe are gone from it.

Deliberately kept, because they are businesses with a geographic qualifier and
not places:

```
U.S. Retail · Americas Retail · Europe Segment Sales
```

## Why these were dropped and not moved

"Moved to the geographical segment" was the obvious reading of the request, so
it was measured before being rejected:

| | |
|---|---|
| Business rows naming a place (Revenues) | 1,761 |
| …already present under geo for the same ticker/member/year | **340** |
| Companies with place-named business members | 86 |
| …that already have a geographic section | **79** |
| …that have none, so would genuinely gain data | **7** |

Moving would double-count 340 rows across 40 companies, and the Totals logic is
concept-anchored and de-duplicated — the area where 2,573 cells were once wrong.
The entire upside is 7 companies: **CMG, GPI, LEN, MCD, MTH, PHM, PZZA** (176
rows). That is a scoped coverage gap for the data team to confirm, not a reason
to move facts between axes.

## Cost

No DB call. `node_for_label` is `lru_cache`d and boot already warms both member
lists, so a user never pays the cold pass.

| | |
|---|---|
| Cold, the 500 labels the dropdown reads | 21.3 ms (once per process, on boot) |
| Warm, per render | **0.032 ms** |
| Warm, the full 3,586-label list | 0.229 ms |

## Testing

`tests/test_business_segments_have_no_places.py` — **68 tests**: every place the
business team named, every catch-all, a sample of real business segments, the
dropdown filter, the root classifier on both axes, a speed assertion, the
separation in both directions, the anti-double-count invariant, and every real
misleading heading the probe found.

`tests/test_geo_label_map.py::test_display_map_cannot_leak_a_name_across_sections`
was updated: it asserted the business spelling of a place gets its own display
entry. It no longer does, because the place is dropped — a stronger form of the
same guarantee. A companion test now covers the per-section key with a real
business segment so the original protection is still exercised.

End to end in a browser (`http://localhost:8501/screening`, STG DB),
event-driven waits, no fixed sleeps: **36 of 36 passed**, slowest interaction
1,822 ms (opening the criterion form — pre-existing, unrelated), every cascade
interaction 52–1,104 ms.

The business assertions search the picker rather than reading its options: the
list is virtualised to ~10 DOM nodes, and an earlier version of the harness
passed "no place is offered" for that reason alone.

## The regression this nearly shipped, and the root fix

A safety probe over 8,914 distinct geo-axis rows asked a question the unit tests
could not: *can this drop real geography?*

**It could.** 108 of those rows do not classify as `geo`, because
`_classify_heading` matches the single word "Geographical" and the heading is
the filer's own prose:

```
Geographic Area: Americas
Geographic Areas Financial Data: Europe
Revenue From Customers Based In Different Geographic Regions
pf0:StatementGeographicalAxis: CHINA      -> heading "Pf0"
```

The last one is the clearest: `_get_heading` takes everything before the first
colon, so a namespace prefix becomes the heading. These rows were being filed as
**Business** all along — a pre-existing bug. Once places started being dropped
from Business they would have **vanished** instead, taking real countries
(CHINA, CANADA, MEXICO, HONG KONG, Taiwan, Americas, EMEA, APAC) with them.

The fix is at the root: the `dimension` column names the axis and is
machine-written, so it overrules the prose.

```python
if section != "geo" and SegmentDataRepository._is_geographic_axis(row):
    section = "geo"
```

`_is_geographic_axis` matches `SEGMENT_GEO_AXES` — the same list the screener's
SQL filters on, so the two cannot drift. This fixes the pre-existing
mis-sectioning as well as the regression.

## Known, unchanged

`Mci` (Molson Coors) resolves to `International` through the data team's own
workbook mapping and is therefore dropped from business. It is a business
segment, not a place. Left as-is rather than special-cased here — it is a
one-line workbook correction on their side.

## Rollback

Remove the `elif names_a_place(member)` branch in `_classify_member` and the two
read-time filters in `screening_service.py`. `names_a_place` itself is inert
without a caller.
