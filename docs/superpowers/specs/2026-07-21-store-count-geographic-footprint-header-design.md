# Store Count header — geographic footprint from Segments

**Date:** 21-Jul-2026
**Area:** Market Data → Additional Data tab (`/market_data?tab=ratings`)
**Files:** `app/data/repository.py`, `app/pages/market_data.py`, `app/utils/cache_manager.py`

---

## 1. Requirement

In the Additional Data tab, a company that discloses only a **worldwide** store
count shows a bare header:

```
Store Count (Worldwide, if Applicable)
```

Business ask: when there is **no per-country store breakdown**, check whether the
**Segments tab has geographic segments**. If it does, name those geographies in
the header instead of "Worldwide":

```
Store Count (United States, Canada, ...)
```

If there are no geographic segments, keep `Store Count (Worldwide, if Applicable)`.

Two product decisions were taken by the business on 21-Jul-2026 after being shown
real data from STG:

1. **Mirror the Segments tab** — list whatever geographic members that tab shows,
   including regions and buckets ("Domestic", "Non-US", "The Americas Group"), not
   only clean country names.
2. **Full names, list all** — no abbreviation (`United States`, not `US`) and no
   cap/`etc.` truncation.

---

## 2. Investigation — how geographic data actually exists in Segments

All evidence below was gathered against the **STG** database on 21-Jul-2026.

### 2.1 Storage model

Geographic segments live in `coreiq_filing_metrics_v5`:

| Column | Meaning |
|---|---|
| `dimension` | XBRL axis — geo axes are `srt:StatementGeographicalAxis`, `us-gaap:StatementGeographicalAxis`, `GeographicDistributionAxis`, `CountryAxis`, `RegionReportingInformationByRegionAxis`, `InvestmentGeographicRegionAxis` |
| `dimension_member_label` | the member, i.e. the geography ("UNITED STATES", "Canadian Operations") |
| `full_dimension_label` | e.g. `Geographical: FRANCE` — drives the Business/Geographical heading split |
| `is_dimensioned`, `doc_type='10-K'`, `numeric_value` | row filters |

`SegmentDataRepository._classify_segment_rows` is the **single source of truth**:
it routes rows to the Business or Geographic table, recovers members from
multi-dimensional operating-segment facts, drops wrapper/roll-up members, and runs
`canonicalize_geo_label` so filing-to-filing drift collapses
("United States Operations" → "United States"). The screening segment cache uses
the same function, so the screener can never drift from the Segments tab.

### 2.2 The raw geo axis is contaminated — do NOT read it directly

Querying the geo axes raw returns non-geography rows:

- **NKE** — `dimension_label='Notes Payable'` on `srt:StatementGeographicalAxis`
  (short-term debt by geography), and
  `Argentina And Uruguay … Disposal Group: Discontinued Operations, Held-for-sale`.
- **COST** — the same country appears under many spellings:
  `UNITED STATES`, `United States Operations`, `CANADA`, `Canadian Operations`,
  `KOREA`, `KOREA, REPUBLIC OF`.
- Pension/retirement plan breakdowns are filed on the same geo axis for some
  issuers (already excluded via `is_geo_pension_label`).

**Conclusion:** the header must be built from the **classified** `geo_segments`
output, never from a raw axis query. That is what this implementation does.

### 2.3 Which metric to use

The classified `geo_segments` contains several metrics (`Revenues`, `Assets`,
`Depreciation & Amortization`, …). Unioning them is wrong:

- **NKE** gains **Belgium** from the *Assets* metric — Nike's European
  distribution centre. Belgium is an asset location, not a retail market, so it
  would read as a Nike store market.

**Decision:** members come from **`geo_segments["Revenues"]` only** — the
commercial footprint (where the company sells). This is also exactly what the
business approved in the previews.

### 2.4 Are we capturing everything? — measured answer

Revenues-only geo members, real STG output:

| Ticker | Members captured | Assessment |
|---|---|---|
| WMT | United States, Walmart International, Non-US, Mexico and Central America, United Kingdom, Other, China, Canada | good country coverage; **"Walmart International" is a business-segment name leaking onto the geo axis** |
| LULU | Americas, United States, People's Republic of China, China Mainland, Rest of World, Canada, Other geographic areas, Outside of North America, Hong Kong SAR/Taiwan/Macau SAR, Mexico | **China listed twice** under two filing labels; heavy region/bucket drift |
| COST | United States, Canada, Other International Operations | clean |
| HD, AZO | United States, Non-US | clean but coarse |
| CROX | United States, International, North America, Non-US, International (1) | mostly buckets |
| DECK, SHW | Domestic / Foreign / International / "The Americas Group" | **no real country names at all** |
| **NKE** | **United States only** | **capture gap — Nike is global.** Its regional revenue (EMEA, Greater China, APLA) is filed on the *business*-segment axis, so it is classified as Business, not Geographic |
| TGT, ULTA, FIVE | *(none)* | no geographic revenue disclosure at all |

**Answer to "are we capturing all?":** we correctly capture everything filed on a
*geographic* axis, and consistently with the Segments tab. We do **not** capture
geographic revenue that issuers file on the *business*-segment axis (NKE). That is
a filing-taxonomy limitation, not a bug in our extraction — reclassifying it would
change the Segments tab and the screening cache and is out of scope here.

---

## 3. Performance — why this needed a persistent cache

Building the classified segment tables is **slow, cold** (measured, STG, 21-Jul):

| Ticker | Cold `_build_segment_tables_from_db` |
|---|---|
| NKE | 10.8 – 14.1 s |
| KR | 12.1 s |
| SHW | 8.2 s |
| TGT | 6.9 s |
| LULU | 4.2 s |

`_fetch_all_db_rows` is `@st.cache_data(ttl=300)` — only a 5-minute window, so it
goes cold constantly. Running this inside the Additional Data render would add
**5–14 seconds** to the tab. That is unacceptable and is exactly the class of bug
that caused the earlier tab-freeze incident.

### Design

The member list is a handful of short strings that only change when a new 10-K
lands (~yearly). So it is cached **on disk**, next to the existing EDGAR caches:

```
<edgar_cache_dir()>/geo_segment_members/<TICKER>.json
  { "cached_at": 1784650081.37, "members": ["United States", "Non-US"] }
```

`edgar_cache_dir()` already resolves to `/home/edgar_cache` on Azure App Service,
which **survives restarts and deploys** (the repo `wwwroot` is replaced on every
deploy). So this answers the "should we store it in a persistent store like the
edgartools cache?" question: **yes, and it reuses that exact mechanism.**

Three layers:

1. **Render (hot path)** — `store_count_geo_label()` → `geo_segment_members()` →
   one small disk read. **Never blocks.**
2. **Miss / stale** — schedules a background rebuild on the existing single-worker
   EDGAR executor and returns what is on disk (possibly empty) → the header shows
   `Worldwide` for that one render and self-heals on the next.
3. **Warm sweep** — the ratings warm sweep (`RATINGS_WARM_SWEEP=1`) builds the file
   for every ticker in the universe, so on a warmed server no user ever sees the
   one-render `Worldwide` fallback.

TTL reuses `_edgar_disk_ttl_seconds()` (`EDGAR_DISK_TTL_DAYS`, default 7 days).
Writes go through `_write_cache_atomic` (temp + `os.replace`) so a render thread
can never read a half-written file while the sweep rewrites it.

### Measured result

| Path | Time |
|---|---|
| Hot path in render (`RATINGS_geo_label`) | **0.0 – 1.1 ms** typical (0.3 ms median; 15–27 ms on the very first disk touch) |
| Background build (`GEO_MEMBERS_build`) | 3.9 s, off-thread, never in a render |
| Cold render with cache deleted | 2.1 s — **same as before the feature**, header falls back to `Worldwide` |

---

## 4. Implementation

### `app/data/repository.py`

- `_geo_members_cache_path(ticker)` — `<edgar_cache>/geo_segment_members/TICKER.json`
- `_build_geo_segment_members(ticker)` — **slow, background only.** Uses
  `_build_segment_tables_from_db`, falls back to `_fetch_from_edgartools` for
  tickers with no DB segment rows (SKX, TSCO) so it mirrors the Segments tab's own
  tiering. Takes `geo_segments["Revenues"]`, orders members by largest reported
  revenue, de-duplicates case-insensitively ("Rest of World" / "Rest of world").
- `_schedule_geo_members_refresh(ticker)` — one in-flight rebuild per ticker,
  guarded by a lock, submitted to the existing EDGAR executor.
- `geo_segment_members(ticker)` — disk read; schedules a refresh when missing/stale.
- `store_count_geo_label(ticker, has_country_rows)` — returns the module constant
  `STORE_COUNT_WORLDWIDE_LABEL` (`"Worldwide, if Applicable"`, the team's standard
  wording) when the company already shows a per-country breakdown, has no
  geographic segments, or the cache is not built yet; otherwise the comma-joined
  member list.

### `app/pages/market_data.py`

- Store Count section header now renders
  `Store Count ({store_count_geo_label(...)})`, gated on
  `bool(sbc_years and sbc_countries)` so a company that already lists countries
  keeps `Worldwide` (its own section names them).
- The **Excel export** uses the same helper, so the download matches the UI.
- New timing log `RATINGS_geo_label`.

### `app/utils/cache_manager.py`

- The ratings warm sweep now also builds and writes the geo-member file per ticker.

### Interaction with existing behaviour

`_hide_sc_section` still hides the Store Count section entirely when the country
breakdown covers every year (COST). The partial-disclosure case (`sbc_partial`,
e.g. CAL) still renders country rows, so `has_country_rows` is true and the header
stays `Worldwide` — the geographies are already visible in that section.

---

## 5. Testing

Verified in the **real UI** (`bash .claude/dev/run_local.sh`, staging DB, headless
Playwright), Additional Data tab per ticker:

| Ticker | Country rows? | Rendered header | Expected |
|---|---|---|---|
| WMT | no | `Store Count (United States, Walmart International, Non-US, Mexico and Central America, United Kingdom, Other, China, Canada)` | ✅ |
| HD | no | `Store Count (United States, Non-US)` | ✅ |
| LULU | no | `Store Count (Americas, United States, People's Republic of China, China Mainland, Rest of World, Canada, Other geographic areas, Outside of North America, Hong Kong SAR, Taiwan, and Macau SAR, Mexico)` | ✅ |
| DECK | no | `Store Count (Domestic, International, Foreign, All other countries, Other Countries)` | ✅ |
| CROX | no | `Store Count (United States, International, North America, Non-US, International (1))` | ✅ |
| SHW | no | `Store Count (The Americas Group, Non-US, Foreign Countries)` | ✅ |
| KR, NKE | no | `Store Count (United States)` | ✅ |
| TGT | no | `Store Count (Worldwide, if Applicable)` — no geo segments | ✅ |
| TXRH, ULTA | **yes** | `Store Count (Worldwide, if Applicable)` — country section names them | ✅ |
| COST | yes | section hidden entirely (`_hide_sc_section`) | ✅ |
| AZO | yes | Stores by Country + continent totals unchanged | ✅ |

Cold-cache / self-heal, verified by deleting `HD.json`:

1. Render → `Store Count (Worldwide, if Applicable)` in **2.1 s** (no blocking).
2. Background build wrote the file ~2 s later (`GEO_MEMBERS_build … 3879ms`).
3. Re-render → `Store Count (United States, Non-US)`.

Excel export clicked on HD — builds with no error, header matches the UI.
No `ERROR` or `Traceback` entries in `server-logs/server-log.log`.

---

## 6. Known data-quality caveats (for the data team)

These are **filing-side** issues surfaced, not regressions:

1. **NKE reads "United States" only** although Nike is global — its regional
   revenue is filed on the business-segment axis. Any fix belongs in
   `_classify_segment_rows` and would also move the Segments tab and the
   screening cache; deliberately out of scope.
2. **"Walmart International"** is a business-segment name appearing on WMT's
   geographic axis.
3. **LULU lists China twice** — "People's Republic of China" and "China Mainland"
   are the same country under two filing labels. Only exact case-variants are
   de-duplicated here; collapsing these would require extending
   `GEO_ALIAS_TO_CANONICAL`, which also changes the Segments tab and screener.
4. **Members containing commas** ("Hong Kong SAR, Taiwan, and Macau SAR") make the
   comma-joined header ambiguous to read.
5. **DECK / SHW / CROX** disclose only vague buckets, so the header names no real
   country.

Because the business chose "mirror the Segments tab / list all", none of these are
filtered out — the header is a faithful mirror. If the headers prove too long or
too noisy in practice, the cap is a one-line change in `store_count_geo_label`.

---

## 7. Rollout

Files to deploy:

- `app/data/repository.py`
- `app/pages/market_data.py`
- `app/utils/cache_manager.py`

No DB change. No new env var required. Recommended (already recommended
previously): set `EDGAR_CACHE_DIR=/home/edgar_cache` on STG/PROD so the geo-member
cache — like the other EDGAR caches — survives the ~6 deploys/day, otherwise every
deploy re-pays one 4–14 s background build per ticker on first view (never in a
render, so users still only ever see the `Worldwide` fallback once).
