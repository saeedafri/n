# Geographic segments — one Region for the three non-place groups

**Date:** 2026-09-30
**Scope:** `Region` picker in Step 3 of the Geographical Segments criterion
**Asked for by:** business team

---

## The problem

The Region picker offered six values:

```
AMER   APAC   EMEA   Market grouping   Multi-region   Non-regional
```

Three of them are places. The other three answer a different question — *what
kind of label is this* — and between them they held 17 of the 98 segment members
a screener sees:

| Old Region | Members | What they are |
|---|---|---|
| `Multi-region` | 9 | Labels spanning two real regions: `EMEA And Asia Pacific`, `Asia, Middle East & Africa (AMEA)` |
| `Market grouping` | 2 | Economic groupings: `Developed Markets`, `Emerging Markets` |
| `Non-regional` | 6 | Buckets with no geography: `Domestic`, `International`, `Other`, `Rest of World (RoW)` |

Half the Region list, and none of it answering "where in the world".

## The change

The three fold into one Region, **`Multi-Regional`**, **flat**. Nothing moves
between groups — the same 17 members are reachable — but the three old names
leave the screen entirely rather than moving down a level.

```
Region                 Sub-region
─────────────────────  ──────────────────
AMER                   Latin America (LATAM), North America (NORAM), …
APAC                   East Asia, …
EMEA                   Western Europe, …
Multi-Regional         (nothing — the picker is disabled)
```

> **Revised 2026-10-03.** The first build kept the workbook's three words as
> Sub-regions under Multi-Regional. The business team asked for them off the
> screen, not one level down, so Sub-region now stays empty on those nodes and
> "Market Grouping" and "Non-Regional" appear nowhere in the UI.

`fold_multi_regional` **raises** rather than flatten a node whose Sub-region the
data team has since filled in — that would be real information about a real
place, and guessing where the node belongs is their call, not ours.

## Where it lives

`scripts/build_geo_hierarchy.py` — the generator, not the JSON. The JSON is
marked *do not hand-edit* and is rebuilt whenever the data team revises
`data/geo/MDP_Geographic_Hierarchy-stage2.xlsx`; putting the fold in the
generator means it survives the next workbook they send.

```python
MULTI_REGIONAL = "Multi-Regional"
FOLDED_REGIONS = frozenset({"Multi-region", "Market grouping", "Non-regional"})
```

Node names, label keys and the excluded list are untouched: 1,294 nodes and
2,080 exact keys before and after. Only the Region field on 20 nodes changed.

## Also removed: the "Include broader segments" checkbox

Same screen, same request. The box was ticked by default and the behaviour is
now hardcoded to that state — a filer reporting only "Americas" still shows when
you filter to United States, because the filing gives no country and dropping it
would silently lose a company that does operate there.

`filter_members(..., include_broader)` keeps the parameter, so the strict mode
still exists for tests and for anything that needs it later.

## Cost

Nothing touches the DB. The cascade reads `geo_hierarchy.json` through an
`lru_cache`, so the fold is free at runtime:

| | |
|---|---|
| `level_options(region)` | 34 µs |
| `level_options(sub-region \| Multi-Regional)` | 39 µs |
| `filter_members(Multi-Regional)` | 42 µs |
| all five levels, one render | **195 µs** |

## Testing

`tests/test_geo_hierarchy.py` — 34 tests, all passing.

- `test_regions_cover_the_live_members` — the Region list is now the four values
- `test_the_three_non_place_regions_became_one_flat_region` — Multi-Regional
  holds all 17, and sub_region / country / state / city are all empty under it
- `test_the_folded_names_appear_nowhere_in_the_cascade` — the six spellings of
  the three old names are offered at no level
- `test_a_real_region_keeps_its_sub_regions` — AMER still gives LATAM + NORAM
- `test_every_member_lands_in_exactly_one_region` — unchanged, still holds
- `describe_label` trails updated: `International` → `Multi-Regional`

End-to-end in a real browser (`http://localhost:8501/screening`, STG DB),
event-driven waits, no fixed sleeps:

```
Region options                 AMER, APAC, EMEA, Multi-Regional      ✓
Region = Multi-Regional        17 of 98 segments match       209 ms
Sub-region under it            disabled, offers nothing      ✓
"Market Grouping"/"Non-Regional" anywhere on the page        gone  ✓
Region = AMER                  LATAM + NORAM kept            545 ms
  Country under AMER           7 values                      113 ms
  State / Province under AMER  1 value                        68 ms
```

## Known, unchanged

`Americas (excluding United States)` is still kept when filtering to United
States — the broader rule is structural and keeps anything that stops above the
filtered level. There are a handful of these "excluding"/"outside" labels in the
source data. A data-side decision, flagged to the business team, not fixed here.

## Rollback

Delete `fold_multi_regional` and its call in `build_nodes`, rerun
`scripts/build_geo_hierarchy.py`, revert the two test expectations.
