# Hierarchy-based geographic segments in Screening

**Date:** 2026-09-24
**Area:** Screening → Financial Information → Geographical Segments → Step 3
**Status:** built, verified end to end on the local STG-backed app

---

## What changed

Step 3 used to be one flat list of ~95 geographic segment names. It is now a
cascade over the place hierarchy the data team maintains:

```
Region  ▸  Sub-region  ▸  Country  ▸  State / Province  ▸  City
                                                    ▸  Segment members
```

Picking a wider level narrows every level below it and narrows the member list.
Picking nothing behaves exactly as before — the full member list, untouched.

## The rule everything is built around

> A filter narrows what you *see*. It never loses a company.

Three concrete consequences:

1. **Every segment name has a home.** All 95 live options, and all 702 labels
   MDP has ever catalogued, resolve to a hierarchy node. A name with no node
   would still be selectable — it collects under an `Unclassified` region rather
   than disappearing.
2. **A member that stops short of the filtered level is kept.** A filer
   reporting only `Americas` has no country, so filtering to *United States*
   keeps it. A **Include broader segments** checkbox (on by default) turns this
   off for a strict view.
3. **A level only offers values that lead somewhere.** `City` renders disabled
   today because no live segment reaches city level. No dropdown entry can come
   back empty.

## How it is put together

Two layers, deliberately separate:

```
raw filing label
      │  segment_aliases.canonicalize_geo_label      ← business team owns wording
      ▼
display name  ("United States", "Europe, Middle East and Africa (EMEA)")
      │  geo_hierarchy.node_for_label                ← data team owns placement
      ▼
node + path   (AMER ▸ North America (NORAM) ▸ United States)
```

Keeping them apart means the screener keeps showing the names the business team
signed off on, and re-grouping a place never renames it.

| File | Role |
|---|---|
| `data/geo/MDP_Geographic_Hierarchy-stage1.xlsx` | The data team's workbook. Stage 1 — it will keep growing. |
| `scripts/build_geo_hierarchy.py` | Workbook → JSON. Never hand-edit the JSON. |
| `app/data/geo_hierarchy.json` | 1,027 nodes, 1,745 exact + 1,661 loose label keys, 18 exclusions. |
| `app/data/geo_hierarchy.py` | Lookup + cascade. Pure functions, no DB, no Streamlit. |
| `app/pages/screening.py` | `_render_geo_hierarchy_filters()` renders the five levels. |
| `tests/test_geo_hierarchy.py` | 27 tests. |

### Label keys and the collision guard

A label is looked up three ways: as written, as its canonical display name, and
through the loose key that survives punctuation drift. Claims are layered —
a node's own name beats a raw label, which beats an old display name — and
**a key claimed by two different nodes is dropped, never guessed**.

That guard earned itself immediately. `Europe, Middle East and Africa (EMEA)` is
the former display name of both an EMEA label and an EMEA-and-APAC one. Without
it, whichever row the generator happened to read first won, and the screener's
71-company EMEA option was filed under *Multi-region*. Seven keys are dropped
this way today (`congo`, `georgia`, `são paulo`, …, plus two placeholder values
the workbook carries). They still resolve by their exact node names.

### Levels beyond the five

The workbook has more levels than the cascade shows — sub-region groups
(NAFTA/USMCA), components (the Nordics), sub-national regions (US Carolinas),
non-regional buckets (International, Domestic), multi-region composites and
market groupings (BRIC). None get their own filter step. They carry a
Region/Sub-region/Country like anything else, so they appear under whichever of
the five levels their path fills in. `International` — the third-largest option
at 79 companies — sits under region `Non-regional` and stays reachable.

## Performance

The cascade recomputes on every Streamlit rerun, so it has to be trivial. It is
pure dict work over a JSON loaded once per process (`lru_cache`), with
`node_for_label` memoised per label. **No database call exists on this path**,
and per the repo rule none may be added.

| Load | Cold | Warm |
|---|---|---|
| 95 members (today) | 1.4 ms | 0.3 ms |
| 950 members (10×) | 3.6 ms | 2.6 ms |
| 9,500 members (20× growth) | 29.5 ms | 29.1 ms |

JSON load is 1.8 ms, once per process. A test fails the build if the whole
cascade exceeds 250 ms at 4,500 members.

## Data growing daily

- **A new label with an existing place** resolves through the loose key with no
  change to anything.
- **A genuinely new place** lands in `Unclassified` — visible, selectable,
  never dropped — and `unplaced_members()` names it for the next workbook round.
- **A new workbook** means: drop it in `data/geo/`, rerun the generator, run the
  tests. `--check` fails if the committed JSON is stale, and a test rebuilds
  from the workbook and compares.

## Coverage today

Checked against all 702 geographic labels MDP has ever recorded:

| | |
|---|---|
| Resolved directly by the workbook | 651 |
| Resolved via the canonical display name | 21 |
| Listed as not-a-place | 18 + 3 already hidden by MDP |
| Placed by the workbook's own Rule 4 (see below) | 7 |
| **Unplaced** | **0** |

### The seven the workbook has not yet covered

`PENDING_NODES` in the generator maps them, all under the workbook's own Rule 4
("labels spanning regions go to a Multi-region node"). **These should go back to
the data team for the next revision** rather than living in our code forever:

| Label | Placed at | Why |
|---|---|---|
| Asia and Central America | Americas and APAC | spans two regions |
| California and France | Americas and EMEA | spans two regions |
| Ireland, Netherlands, and Singapore | EMEA and APAC | spans two regions |
| Latin America Europe And Other | Americas and EMEA | spans two regions |
| Saudi Arabia and Mexico | Americas and EMEA | spans two regions |
| Minneapolis, MN | Minneapolis | the city node already exists |
| Western Europe Reporting Unit | Western Europe | the component node already exists |

## Verification

`tests/test_geo_hierarchy.py` — 27 tests covering placement of every live
member, the cascade narrowing at each level, both filter modes, order
preservation, the awkward labels (US regions must not become countries), an
unknown label staying reachable, and the speed ceiling. The existing
`tests/test_geo_label_map.py` (71 tests) still passes unchanged.

End to end in the real UI (`bash .claude/dev/run_local.sh` →
`http://localhost:8501/screening`), 32 checks, all passing:

- five levels render; City correctly disabled
- `EMEA` → exactly its three sub-regions; 95 → 32 members
- `Europe` → 32 → 26; UK offered; Japan and the US correctly absent
- `United Kingdom` → 26 → 11
- strict mode 26 → 23, un-ticking restores 26
- clearing the filters brings back every segment, including Japan and the US
- a criterion built through the cascade carries `Segment Members: United States`
- the screen runs and renders results

## Known gaps

- **City is empty today.** The workbook has 214 city nodes but no live segment
  reaches one, so the level renders disabled. It lights up on its own when a
  filer reports a city.
- **`State / Province` has one value** (California). Same reason.
- **Business segments have no hierarchy** and keep the flat list — there is no
  equivalent structure for them.
- **The seven pending placements** above are ours, not the data team's.
