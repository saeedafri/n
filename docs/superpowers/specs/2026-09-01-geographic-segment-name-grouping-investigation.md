# Geographic Segment Names — Full STG Scrape & Grouping Investigation

**Date:** 2026-09-01
**DB:** `coresight_market_data_stg` @ `csr-mysql8-flex-stg.mysql.database.azure.com`
**Deliverable:** `~/Desktop/Geographic_Segment_Names_STG.xlsx` (13 sheets)
**Raw CSVs:** `~/Desktop/geo_segment_csv_raw/`

---

## 1. Where geographic segments come from

```
SEC 10-K XBRL filing
  └─ coreiq_filing_metrics_v5   (is_dimensioned = 1)      ← ONLY source of segment names
       columns: dimension, dimension_label,
                dimension_member_label, full_dimension_label
         │
         ├─ axis filter (app/utils/constants.py:1056  SEGMENT_GEO_AXES)
         │     StatementGeographicalAxis
         │     GeographicDistributionAxis
         │     RegionReportingInformationByRegionAxis
         │     CountryAxis
         │     InvestmentGeographicRegionAxis
         │
         ├─ SegmentDataRepository._classify_segment_rows   (app/data/repository.py:10659)
         │     • axis match            → geo
         │     • _looks_geographic()   → geo, for members recovered off a
         │       multi-dimensional operating-segment fact (Costco pattern)
         │     • SEGMENT_SKIP_MEMBERS  → drop totals / eliminations
         │     • canonicalize_geo_label()  ← the only grouping that exists today
         │
         ├─ Market Data → Segments tab       (app/pages/market_data.py:710)
         └─ coreiq_screening_segment_values_cache   → Screening page
              └─ coreiq_screening_segment_member_cache  → the dropdown
```

The name a user sees **is the raw XBRL `dimension_member_label` string**, passed
through `_title_case_member()` only. Every spelling a filer invents becomes a
separate segment.

## 2. What the scrape found

| Scope | Count |
|---|---|
| Companies in `coreiq_filing_metrics_v5` | 450 |
| Distinct dimension members, ALL axes | 84,527 |
| Distinct **geographic** member names (5 geo axes) | **1,260** |
| Company × geo-name pairs | 3,765 |
| Geo names offered by the screener today | 376 |
| Clusters that are the same place spelled 2+ ways | **178** |
| Geo names already grouped by `segment_aliases.py` | **41 of 1,260 (3%)** |

### Classification of the 1,260 geo names

| Kind | Count |
|---|---|
| Unmapped (needs a human eye) | 419 |
| Composite / qualified country ("U.S. and Canada") | 255 |
| Composite / qualified region ("Rest of Asia Pacific") | 194 |
| Country | 173 |
| State / Province | 100 |
| Region / Bloc | 72 |
| City | 29 |
| Non-geographic — pension/benefit plan contamination | 17 |
| Non-geographic — business segment | 1 |

### Worst duplicate clusters

| Canonical | Spellings | Companies |
|---|---|---|
| United States | 23 | 401 |
| Europe, Middle East and Africa | 21 | 50 |
| Non-US ("Outside …" phrasing) | 15 | 33 |
| Other | 13 | 75 |
| Non-US ("Non-…" phrasing) | 12 | 101 |
| Asia Pacific | 10 | 77 |
| United States and Canada | 10 | 21 |
| Other International | 9 | 25 |
| United Kingdom | 8 | 127 |
| Rest of World | 8 | 40 |

Real variants seen for the United States cluster: `In the U.S.`, `Total U.S.`,
`U S`, `U.S`, `U.S.`, `U.S. Member]`, `U.S. operations`, `U.S. segment`,
`UNITED STATES`, `US`, `USA`, `United State`, `United States`,
`United States ("U.S.")`, `United States Geographic Region`,
`United States Operations`, `United States [Member}`, `United States:`,
`United States Federal`, `Domestic/United States`, `pf0:US` …

## 3. Cities in the DB

**No table in the STG DB has a city column.** City names exist only as geographic
segment member labels (29 of them, e.g. Shanghai, London, Seoul), plus 100
state/province labels. They are isolated on the `Cities_And_Subnational` sheet.
Country values that DO live on company tables (75 rows, 5 tables) are on
`Company_Country_Fields`.

## 4. Workbook sheets

| Sheet | Rows | Content |
|---|---|---|
| README | 21 | Sources, axis list, column meanings |
| **Geo_Members_Master** | 1,260 | Every geo name + kind + counts + proposed group |
| **Grouping_Clusters** | 178 | THE FIX LIST — names with 2+ spellings |
| Cities_And_Subnational | 129 | City / state / province labels |
| Geo_By_Company | 3,765 | Which company reports which name, which years |
| Screener_Geo_Members | 376 | What the screener offers today |
| Screener_Geo_By_Company | 2,592 | Screener cache at ticker × member × metric |
| Segment_Member_Cache | 3,645 | The dropdown source table (biz + geo) |
| Geo_Axes_In_DB | 2,831 | Which XBRL axes the names arrive on |
| Company_Country_Fields | 75 | `country_of_incorporation` / `country` values |
| Companies | 465 | `coreiq_companies` master |
| Geo_Members_v4_Legacy | 827 | Same scrape against `coreiq_filing_metrics_v4` |
| All_Members_Any_Axis | 84,527 | Every member on every axis — geo names filed on business axes show here |

### Key columns

- `group_key` — normalised clustering key: case, punctuation, parentheticals,
  U.S./U.K. abbreviations, word order and noise words (`segment`, `operations`,
  `region`, `total`, `member`) all removed.
- `suggested_group` — proposed canonical label. Existing app canonical wins;
  otherwise the most-used spelling in the cluster.
- `existing_app_canonical` — blank means **not grouped in code yet**.
- `geo_kind` — Country / Region / State-Province / City / Composite / Unmapped /
  Non-geographic, resolved against ISO-3166 (pycountry) and GeoNames (pop ≥ 50k).

## 5. Method notes (for whoever repeats this)

- A single full-table `GROUP BY` over `coreiq_filing_metrics_v5` with the five
  `dimension LIKE '%…Axis%'` predicates **ran 28 minutes without completing** —
  TEXT columns force an on-disk temp table.
- The working approach is **450 per-ticker queries** hitting
  `idx_v5_ticker_dim_doctype (ticker, is_dimensioned, doc_type)`, 8 threads:
  **v5 in 5.6 min, v4 in 4.4 min**. Scripts live in the session scratchpad
  (`scrape_members.py`, `build_workbook.py`).
- The `UNION ALL` over country columns fails with error 1271 (mixed collations);
  query each table separately instead.

## 6. Proposed fix (not implemented)

Extend `app/data/segment_aliases.py` from 6 canonical groups to cover the 178
clusters, driven by `Grouping_Clusters`:

1. Sign off the `suggested_group` column cluster by cluster (the composite and
   Unmapped rows need human judgement — `U.S. and Canada` must NOT collapse into
   `United States`; the existing file header already warns about exactly this).
2. Emit the signed-off map as `GEO_CANONICAL_GROUPS` entries.
3. Add the 17 pension/plan labels to `GEO_PENSION_SKIP_LABELS` (some are already
   there) and the 1 business label to `GEO_KNOWN_BIZ_LABELS`.
4. Rebuild `coreiq_screening_segment_values_cache` so the dropdown collapses.
5. Verify in the real UI: `/screening` geo dropdown and `/market_data` Segments
   tab must show one `United States`, not 23.

**No DB writes were made.** This was a read-only scrape.
