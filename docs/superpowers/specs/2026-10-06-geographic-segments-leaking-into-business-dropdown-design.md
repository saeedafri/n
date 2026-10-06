# Geographic segments leaking into the Business Segments dropdown

**Date:** 2026-10-06
**Area:** Screening → Financial Criteria → Segment members (business)
**Files:** `app/data/repository.py`, `tests/test_business_segments_have_no_places.py`

---

## Symptom

The Business Segments member dropdown on `/screening` offered entries that read as
geography:

- `Asia Middle East Africa (1 companies)`
- `Asia Pacific Foods (Segment) (1 companies)`
- `Asia Pacific, Australia and New Zealand, and China Region (1 companies)`
- `Asia-Pacific, Africa (1 companies)`
- `Asia, Middle East and North Africa (1 companies)`

Geography is supposed to live only in the Geographical Segments section.

---

## Evidence

All five were traced to source rows in `coreiq_filing_metrics_v5` on the staging DB.

| Label | Ticker | How the 10-K files it |
|---|---|---|
| Asia-Pacific, Africa | JNJ | `srt:ConsolidationItemsAxis`, `"Consolidation Items: Operating Segments, Geographical: Asia-Pacific, Africa"` |
| Asia Middle East Africa | MDLZ | `us-gaap:StatementBusinessSegmentsAxis` (`"Asia Middle East Africa Segment"`) |
| Asia, Middle East and North Africa | PEP | `us-gaap:StatementBusinessSegmentsAxis` |
| Asia Pacific, Australia and New Zealand, and China Region | PEP | `us-gaap:StatementBusinessSegmentsAxis` |
| Asia Pacific Foods (Segment) | PEP | `us-gaap:StatementBusinessSegmentsAxis` |

The cache was **not** stale: `coreiq_screening_segment_member_cache` was rebuilt
2026-10-06 11:32, and replaying the live classifier
(`_segment_entries_for_tickers` → `_classify_segment_rows` → `_classify_member`)
against staging reproduced every row.

---

## Root cause

### Why nothing filtered them out

Both defenses against geography-in-Business are gated on one function,
`data.geo_hierarchy.names_a_place`:

- `repository.py` `_classify_member` — `elif names_a_place(member): return None` (write time)
- `screening_service.py` `_drop_places_from_business_options` (read time, safety net)

`names_a_place` is a pure lookup in `geo_hierarchy.json`, built from the business
team's Countries Mapping workbook. The workbook lists single places. None of the
five compound names resolves to a node, so both gates see `False` and pass them.

This is why `Europe`, `North America` and `Latin America` never appear in that
dropdown even though they sit in the business cache for JNJ and MDLZ — they have
nodes, so the read-time net hides them. Only spellings missing from the workbook leak.

### Two distinct situations behind one symptom

**JNJ — a genuine routing bug.** Its 10-K never puts `Asia-Pacific, Africa` on a
geographic axis; only its 10-Qs do, and the segment cache queries `doc_type = '10-K'`
only. In the 10-K the geography sits on the *second* axis of a multi-dimensional
fact, and all three detectors miss it:

1. the wrapper path's `forced_section = "geo"` requires `inner.lower() in geo_member_set`,
   and that set is built only from rows whose `dimension` column names a geo axis —
   on a wrapper row that column names the **wrapper's** axis (`ConsolidationItemsAxis`),
   never the inner one, so the set never learns the label;
2. `_get_heading()` reads only the text before the **first** colon → `"Consolidation Items"`
   → business;
3. `_is_geographic_axis()` also reads the `dimension` column → `ConsolidationItemsAxis`
   → `False`.

The word `Geographical:` is present in `full_dimension_label` and nothing read it.
The same path produced JNJ's `Europe` and `Western Hemisphere excluding U.S.`
(hidden by the read-time net, but still misfiled).

Consequence before the fix: JNJ's geographical cache held only `Europe`, `Non-US`,
`United States` — its Asia-Pacific and Western-Hemisphere revenue existed **only**
under Business. Dropping it without re-routing would have lost data.

**PEP ×3 and MDLZ ×1 — not misfiled.** PepsiCo's reportable segments literally are
APAC / AMENA / Asia Pacific Foods, and Mondelez's is AMEA, all declared by the filer
on `StatementBusinessSegmentsAxis`. They are geographic in *name* only. The existing
McDonald's escape hatch (`dimension in segment_axes`) deliberately keeps a place on a
real segment axis in Business for exactly this reason.

Scale check: 153 of the 3,303 business dropdown options match a geography keyword, but
most are real segments — `Frito Lay North America`, `Calvin Klein North America`,
`Domestic Streaming`, `Americas Simple Meals and Beverages`. A keyword filter would
gut the list.

---

## Fix

Scope decided with the user: **fix the JNJ routing bug only**; leave filer-declared
geographically-named segments in Business.

`SegmentDataRepository._inner_heading_is_geographic(row, inner)` (new) reads the inner
axis's own heading out of `full_dimension_label`. Both halves of a part can contain
commas (`"Consolidation Items: Operating Segments, Statement, Geographical: Asia-Pacific,
Africa"`), so the heading is found by locating `": <inner>"` and walking the prefix back
one `", "` at a time through `_normalize_heading` / `_classify_heading`, rather than by
splitting the string.

`_classify_member`'s wrapper branch now forces `geo` when the inner member looks
geographic **and** either `geo_member_set` knows it (unchanged) **or** its inner heading
is Geographical (new).

```python
if SegmentDataRepository._looks_geographic(inner) and (
        inner.lower() in geo_member_set
        or SegmentDataRepository._inner_heading_is_geographic(row, inner)):
    forced_section = "geo"
```

`_looks_geographic` is kept as a conjunct so the new clause can never fire on a name
nobody would read as a place.

### Why this cannot touch PEP/MDLZ

Their inner heading is `Segments:`, which classifies as business. Verified by test and
by replaying the live classifier.

---

## Verification

Live replay of the production classifier against staging for JNJ / MDLZ / PEP:

| Ticker | Before | After |
|---|---|---|
| JNJ business | `Asia-Pacific, Africa`, `Europe`, `Western Hemisphere excluding U.S.` present | all three gone |
| JNJ geographical | `Europe`, `Non-US`, `United States` | + `Asia-Pacific, Africa`, `Western Hemisphere excluding U.S.` |
| MDLZ | `Asia Middle East Africa` in business | unchanged |
| PEP | three geo-named segments in business | unchanged |

Tests: `tests/test_business_segments_have_no_places.py` — 79 passed (8 new
parametrised cases + 1 unit case for `_inner_heading_is_geographic`).
Adjacent suites (`test_segment_totals`, `test_segment_member_values`,
`test_segment_cache_incremental`) — 71 passed.

---

## Rollout

No migration. `_segment_classifier_version()` hashes `_classify_member`'s source, so
the change itself is a reason to rebuild: `refresh_segment_cache_if_stale()` sees a
new classifier hash on the next tick and republishes
`coreiq_screening_segment_values_cache` + `coreiq_screening_segment_member_cache`
once, automatically, on each environment.

Until that rebuild completes the dropdown keeps showing the old members — the fix is
in the classifier, not in the read path.

---

## Known remaining gap (not fixed here, by decision)

A place whose spelling is absent from `geo_hierarchy.json` is still invisible to
`names_a_place`, so the read-time net cannot hide it. The durable fix is workbook
coverage for compound region names, owned by the business/data team — see
`scripts/geo_label_decisions.csv`. Note that simply adding nodes for PepsiCo's and
Mondelez's segment names would *remove* those filers' real reportable segments from
the Business dropdown, so coverage work needs the `segment_axes` interaction checked
alongside it.
