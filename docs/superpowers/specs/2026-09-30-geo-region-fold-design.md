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

The three fold into one Region, **`Multi-Regional`**, and each keeps the
workbook's own word as its **Sub-region**. Nothing is hidden and nothing moves
between groups — the same 17 members are reachable, one level deeper.

```
Region                 Sub-region
─────────────────────  ──────────────────
AMER                   Latin America (LATAM), North America (NORAM), …
APAC                   East Asia, …
EMEA                   Western Europe, …
Multi-Regional         Market Grouping (2), Multi-Region (9), Non-Regional (6)
```

The Sub-region slot was free: every one of those 20 hierarchy nodes has an empty
Sub-region in the workbook. `fold_multi_regional` **raises** rather than
overwrite if the data team ever fills one in — that would be real information,
and guessing where the node belongs is their call, not ours.

## Where it lives

`scripts/build_geo_hierarchy.py` — the generator, not the JSON. The JSON is
marked *do not hand-edit* and is rebuilt whenever the data team revises
`data/geo/MDP_Geographic_Hierarchy-stage2.xlsx`; putting the fold in the
generator means it survives the next workbook they send.

```python
MULTI_REGIONAL = "Multi-Regional"
FOLDED_REGIONS = {
    "Multi-region":    "Multi-Region",
    "Market grouping": "Market Grouping",
    "Non-regional":    "Non-Regional",
}
```

Node names, label keys and the excluded list are untouched: 1,294 nodes and
2,080 exact keys before and after. Only two fields on 20 nodes changed.

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

`tests/test_geo_hierarchy.py` — 32 tests, all passing.

- `test_regions_cover_the_live_members` — the Region list is now the four values
- `test_the_three_non_place_regions_became_one` — the three Sub-regions exist,
  they partition the 17, and 2 + 9 + 6 sums back to the whole
- `test_every_member_lands_in_exactly_one_region` — unchanged, still holds
- `describe_label` trails updated: `International` → `Multi-Regional > Non-Regional`

End-to-end in a real browser (`http://localhost:8501/screening`, STG DB),
event-driven waits, no fixed sleeps:

```
Region options                 AMER, APAC, EMEA, Multi-Regional      ✓
Region = Multi-Regional        17 of 98 segments match       221 ms
Sub-region options             Market Grouping, Multi-Region, Non-Regional  ✓
  Market Grouping                2 of 98                     211 ms
  + Multi-Region                11 of 98   (+9)              215 ms
  + Non-Regional                17 of 98   (+6)              209 ms
Region = AMER                  27 of 98                      571 ms
  Sub-region under AMER        LATAM, NORAM — no leakage     ✓
```

## Known, unchanged

`Americas (excluding United States)` is still kept when filtering to United
States — the broader rule is structural and keeps anything that stops above the
filtered level. There are a handful of these "excluding"/"outside" labels in the
source data. A data-side decision, flagged to the business team, not fixed here.

## Rollback

Delete `fold_multi_regional` and its call in `build_nodes`, rerun
`scripts/build_geo_hierarchy.py`, revert the two test expectations.
