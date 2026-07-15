# Store Count Accuracy Investigation + "Worldwide, if applicable" Label — 2026-07-15

## Ask (Shashank, 14-Jul-2026)
1. Store numbers shown in Additional Data are unreliable — investigate deeply against real data and confirm what matches and what doesn't.
2. Rename the Additional Data section header **Store Count** → **Store Count (Worldwide, if applicable)** (we don't get per-country counts generically). Production push planned end of this week.

## Architecture / Data Flow
```
SEC 10-K (Item 1 Business / Item 2 Properties / Item 7 MD&A)
  └─ scripts/extract_store_counts.py
       Step 1: section text from data/filings/<TICKER>/<YEAR>/<DOC>/SECTION_CACHE.json
       Step 2: regex candidates (6 patterns; sorted by count DESC — largest wins as fallback)
       Step 3: GPT-4o-mini verification (sees only first ~6,000 chars of sections;
               prompt asks for CONSOLIDATED TOTAL, "sum segments yourself")
       Step 4: INSERT into coreiq_filing_metrics (source='store_count',
               dimension=store_type, member=as_of_date, llm_query=JSON detail)
  └─ (migrated to) coreiq_filing_metrics_v5  ← what the app reads
       app/data/repository.py :: RatingsDataRepository.get_ratings_data()
         - groups rows by dimension (store_type); collapses duplicates via dict (last row wins)
  └─ app/pages/market_data.py :: render_ratings_data()  → Additional Data tab table
     app/pages/market_data.py (Excel export, ~line 4161)
     app/pages/screening.py    → "Store Count" display column (Additional Data criterion)
```

## DB Snapshot (staging, 15-Jul-2026)
- 657 `store_count` rows, 123 tickers, all `extraction_method=edgartools+regex+llm_verified`.
- **142/657 rows (21.6%) store a value that does not appear in their own `source_sentence`.**
  Some are legitimate segment sums (KSS 1,159+12; DECK US+intl; LEVI; PAG; BBY dom+intl);
  many are fabrications (see below).

## Manual Online Verification (latest FY per ticker vs. company 10-K / press release)
| Ticker | DB value | Real value (source) | Verdict |
|---|---|---|---|
| WMT FY2026 | 10,955 | 10,955 units worldwide, 10-K Jan-31-2026 | ✅ exact |
| COST FY2025 | 914 | 914 warehouses worldwide, 10-K Aug-31-2025 | ✅ exact |
| ULTA FY2026 | 1,591 | 1,505 US + 86 Space NK = 1,591, 10-K | ✅ exact |
| BBY FY2026 | 1,068 | 926 US + 142 CA, 10-K | ✅ exact (sum) |
| HD FY2026 | 2,359 | 2,359 (US+CA+MX), fiscal 2025 10-K | ✅ exact |
| TGT FY2026 | 1,995 | 1,995 US, 10-K | ✅ exact |
| KR FY2026 | 2,697 | 2,697 supermarkets, 10-K | ✅ exact |
| ORLY FY2025 | 6,585 | 6,585 (US+PR+MX+CA) | ✅ exact |
| AAP FY2026 | 4,305 | 4,305, 10-K Jan-03-2026 | ✅ exact |
| ANF FY2026 | 829 | 829, 10-K | ✅ exact |
| CMG FY2025 | 4,042 | 4,042 company-owned (4,056 incl. 14 partner-operated) | ✅ matches 10-K wording |
| **TJX FY2026** | **5,890** | **5,214 worldwide** (10-K) | ❌ +676 — LLM summed a US-only state table wrong; FY2022–FY2025 also wrong (3,680/4,480/4,585/5,764 vs ~4,689/4,835/4,972/5,085) |
| **DKS FY2026** | **42** | **3,195** (DICK'S + Foot Locker, 10-K) | ❌ garbage row |
| **LULU FY2024–26** | 47/52/58 ("outlets" only) | **711/767/811 total stores** | ❌ total row missing — only outlets extracted |
| **URBN FY2023–26** | 9–11 "locations" | **793 stores** (FY2026) | ❌ stored the franchisee/Menus&Venues count |

**Verdict: NOT reliable as-is.** ~11/15 majors exact at the latest year, but the error tail is fat and includes household names (TJX, DKS, LULU, URBN).

## Root Causes (evidence-backed)
1. **LLM extraction errors** — sums partial tables (TJX), transcribes wrong number
   (AAP FY2019 stored 4,872, own sentence says 4,877), fabricates when the count sits in a
   table beyond the 6,000-char prompt window (TJX FY2025 5,764 with no numbers in sentence).
2. **`RETAIL_SIC_PREFIXES` is defined but never enforced** (extract_store_counts.py:67; zero
   references). Non-retail companies get "store counts": DXC 27,541→500 "locations",
   Amphenol 480 plants, Arrow 263 (=220 facilities+43 DCs), Tyson, Whirlpool, Zebra, Chewy
   (fulfillment centers), Realty Income (REIT properties, and wrong-year value at that).
3. **Duplicate conflicting rows** for the same (ticker, FY, store_type): AAP, CRI, EYE, FOSL,
   SFM, SNBR, YETI — repository dict-collapse keeps whichever row arrives last (row order
   dependent). AAP FY2023 additionally has two store_types ('stores' 5,086 = prior-year value,
   'stores and branches' 5,107) rendered as two rows.
4. **Scope inconsistency across years** — BBY FY2020 stored US-only 977 while FY2021+ stores
   dom+intl sum → fake +18.6% YoY. LULU switched from total (694) to outlets-only (47).
   SHW alternates Americas-Group vs Paint-Stores-Group scope.
5. **Wrong regex fallback design** — candidates sorted by count DESC, "largest number wins";
   the LLM prompt receives those as anchors, biasing it to big numbers.

## Changes Made (this task)
- `app/pages/market_data.py:906` — Additional Data table section header:
  `Store Count` → `Store Count (Worldwide, if applicable)`.
- `app/pages/market_data.py:4161` — Excel export section row: same rename.
- Verified via Playwright on local (ULTA → Additional Data): header renders renamed; values
  1,264/1,308/1,355/1,385/1,445 match Ulta 10-Ks.

Deliberately NOT changed (scope discipline — flag separately):
- `app/pages/screening.py` "Store Count" display column + "Store Counts" checkbox labels.
- `app/data/repository.py:11874` `label` field (not used by the Additional Data renderer).

## Recommended Remediation (before prod, needs sign-off)
1. **Purge non-retail tickers** from `source='store_count'` (enforce SIC filter or explicit
   allowlist): ADT, APH, ARW, AVY, BLD, CHWY, COKE, CHSCP, DAR, DXC, FNKO, INGR, KVUE, MTH,
   DHI, PHM, NWL, O, TSN, UFI, WHR, ZBRA, QRTEA/QVC…
2. **Delete/fix known-bad series**: TJX (all years), DKS FY2026, URBN (all), LULU FY2024+
   (re-extract totals), AKA, ARKO FY2021, UPBD, SGI FY2024, CALY, WRBY FY2022–24, FLWS (0 row).
3. **De-dupe** (ticker, FY, store_type) keeping the row whose as_of_date falls inside the FY.
4. **Add a validation gate to the pipeline**: stored value must literally appear in
   `source_sentence` OR equal the sum of numbers in it; reject >40% YoY swings without a
   second confirming sentence; feed full section tables (not 6,000-char truncation).
5. Re-run `scripts/extract_store_counts.py --force` for the affected tickers after the gate.

## Testing
- DB probe + anomaly scan scripts (session scratchpad): duplicates, sentence-mismatch (142),
  YoY>40% swings, method breakdown.
- 15 companies manually verified against SEC 10-K / IR sources online (table above).
- Playwright UI proof on `/market_data?ticker=ULTA&tab=ratings`.

## Rollout
- Label rename: safe, no data migration, ships with this week's prod push.
- Data remediation (items 1–5): separate task; requires DB writes on staging first, then prod
  after spot-check. Do NOT ship current store-count data to production for the wrong tickers
  listed above without cleanup.

---

# Round 2 (same day) — Read-Path + EdgarTools Extraction Audit

## Question
Is the app's read logic from `coreiq_filing_metrics_v5` wrong? Are we wrongly extracting
from edgartools? (No state-wise data wanted — pure totals only.)

## Findings

### A. v5 read path (`RatingsDataRepository`) — SQL is faithful; two display bugs found & FIXED
1. **Read SQL is correct.** UI values = DB rows = (for clean tickers) 10-K values.
   The wrong numbers on screen come from the ingestion pipeline, not the SELECT.
2. **FIXED — nondeterministic duplicate collapse** (`repository.py` `get_ratings_data`):
   duplicate (ticker, FY, store_type) rows (AAP/CRI/EYE/FOSL/SFM/SNBR/YETI FY2022-23) were
   collapsed last-row-wins with no ORDER BY tiebreak → value could flip between page loads.
   Now deterministically keeps latest as-of date (member), then latest filing_date.
3. **FIXED — YoY computed on display order** (`market_data.py` store-count + square-footage
   YoY rows): with Sort = Latest the percentages were inverted and shifted one column
   (BBY FY2026 showed +4.6% instead of −4.4%). Now computed on chronological years always.
   Verified in UI both sorts (BBY Latest: −4.4/−0.7/−1.1/−0.5/−1.3 ✓; ULTA Earliest unchanged).
4. **LATENT — screening `_fetch_additional_display_values`** (`screening_service.py:3927`):
   `SUM(numeric_value)` over ALL rows at the latest FY → doubles the displayed count if a
   ticker ever has 2 rows at its newest year (AAP FY2023 5,086+5,107=10,193 while it was
   latest). Verified 0 tickers affected today; will re-trigger on the next bad ingest.
   Recommend MAX-per-store-type or latest-as-of row instead of blind SUM.

### B. EdgarTools extraction — one path silently DEAD, data itself clean
5. **`_edgartools_stores_by_country` is dead on edgartools 5.16.0** (installed locally;
   requirements.txt only pins `>=3.0.0`): `list(filings[:6])` raises
   `AttributeError: 'pyarrow.lib.ChunkedArray' object has no attribute 'as_py'`
   (EntityFilings no longer supports slice indexing) → caught by blanket except → returns {}
   → **the Stores by Country section never renders** in any env with edgartools ≥5.x.
   The credit-ratings path uses `list(filings)[:6]` (iterate-then-slice) and works.
   Fix when wanted: change to `list(filings)[:6]` — one character-level diff.
6. **No state-wise rows exist in the XBRL geo data.** Probed COST, ULTA, TGT, BBY, ROST,
   ACI, FND, CMG, TSCO, MUSA raw: every `StatementGeographicalAxis` member is a
   `country:XX` qname (US, CA, MX, JP, GB, KR, AU, TW, CN, ES, FR, IS, SE, NZ). Values are
   EXACT (COST FY2024 sums to 890 ✓; ULTA 1,505+84+2=1,591 ✓). Cosmetic only: member labels
   render as-is ("Canadian Operations" instead of "Canada").
7. **XBRL `us-gaap:NumberOfStores` is the accurate source we are NOT using for the headline
   number.** Many tickers tag exact totals: ACI 2,244 (undimensioned) ✓, ROST segment rows
   1,904+363=2,267 ✓, TSCO 2,602, MUSA 1,800. The bad DB numbers (TJX 5,890, DKS 42, URBN 9,
   LULU 58) all came from the regex+LLM text pipeline, whose failure mode is structural:
   flattened HTML tables + 6,000-char truncation feed the LLM partial/garbled numbers.

## Recommended (needs product sign-off, not done)
- Re-source the headline Store Count from XBRL `NumberOfStores` (undimensioned, else sum of
  top-level segment members) where tagged; use the LLM text pipeline only as fallback WITH a
  value-must-appear-in-sentence validation gate.
- Decide fate of "Stores by Country": currently dead (item 5). Either fix the slice and keep
  it (data is clean, per-country), or remove the section per "worldwide only" direction.
- Harden screening SUM (item 4). Pin edgartools version in requirements.txt.

## Files changed (Round 2)
- `app/data/repository.py` — deterministic duplicate collapse in `get_ratings_data`.
- `app/pages/market_data.py` — YoY chronological fix (store counts + square footage).

---

# Round 3 (same day) — XBRL-First Store Totals, Worldwide-Only, Hardening
**Approved by Saeed: proceed + E2E test; worldwide only; harden screening/pin; NO DB writes.**
All changes are app-side read logic. The staging DB was not modified.

## What ships
1. **`RatingsDataRepository._edgartools_store_totals(ticker)`** (repository.py, new) —
   worldwide totals per fiscal year from XBRL `us-gaap:NumberOfStores` (+Restaurants/
   UnitsOperated/OperatingLocations), 6 most-recent 10-Ks, `st.cache_data` ttl=600.
   - Facts keyed by their own as-of date (`period_instant.year`) — edgartools
     `fiscal_year` metadata mislabels comparatives (LULU series was shifted −1yr);
     instant-year matches the v5 `report_fiscal_year` convention exactly.
   - Per-instant total preference: undimensioned → ParentCompanyMember → geo-axis sum
     (countries are disjoint; `stpr:` state members ignored when `country:US` coexists)
     → single-axis member sum (acquisition axes excluded — MUSA's QuickChek deal fact
     would otherwise fabricate a year). Latest instant wins per calendar year.
2. **Source resolution in `get_ratings_data`** — XBRL replaces DB rows as ONE
   "Total" row only when: ≥3 XBRL years (kills BBY's junk single 800 and TJX's
   unrelated "300") AND (≥1 year agrees with a validated DB row within 1% —
   agreement tested over ALL years, not the selected window — OR the DB has zero
   valid rows). Otherwise validated DB rows stand (DKS: XBRL tags only the DICK'S
   banner, 728 vs true ~855 fleet).
3. **Source-sentence validation gate** (`_store_row_passes_source_check`) — every DB
   store_count row must reproduce its value from its own source sentence (literal or
   subset-sum, so KSS 1,159+12 passes). Failing rows are hidden ("-"): kills DKS
   FY2026=42, URBN=9, TJX's five fabricated years, AAP FY2019 4,872≠4,877,
   WMT's three wrong years.
4. **Stores by Country retired** (worldwide only). `stores_by_country` returns empty;
   renderer/Excel untouched (section self-hides). The fixed `_edgartools_stores_by_country`
   remains available (slice bug corrected to `list(filings)[:6]`) if ever re-enabled.
5. **Screening hardened** — `SUM` → `MAX` at latest year (double-count proof).
   Verified read-only against staging: MAX == SUM for all 123 tickers today.
6. **`requirements.txt`** — `edgartools>=3.0.0` → `==5.16.0` (tested version;
   5.x broke `filings[:6]` slicing).

## End-to-end evidence (15-Jul-2026)
Standalone `get_ratings_data` against staging DB + live EDGAR — ALL PASS (10 tickers):
ULTA/LULU/URBN/COST/ROST → XBRL Total rows exact vs 10-Ks (LULU full series
440→811 incl. the previously-missing FY2022=574); DKS/BBY/TJX/WMT/TGT → DB kept,
garbage years hidden (DKS FY2026 "-", TJX only validated 3,290/3,305 remain,
WMT FY2020-23 hidden / FY2024-26 correct).
Playwright UI (localhost:8501, STG DB): LULU Total 521-811 ✓, URBN 700-784 ✓,
DKS 854-856+"-" ✓, BBY unchanged 1,159-1,068 ✓, ULTA 1,264-1,591 ✓, TJX 3,305-only ✓;
no Stores by Country section anywhere; YoY correct in both sort orders.

## Known residual gaps (data-side; DB untouched by instruction)
- TJX shows only FY2020-21 (3,290/3,305) and those are US-only scope; BBY FY2020 (977)
  is US-only (outside the default window). Fix belongs to a pipeline re-ingest.
- Cold-cache renders may show DB-only store counts for ~1 render while the 25s-bounded
  XBRL fetch warms in the background (same pattern as the existing credit-ratings path).

## Files changed (Round 3)
- `app/data/repository.py` — new `_edgartools_store_totals` + `_store_row_passes_source_check`;
  phase-2 swap (totals replace by-country); resolution logic; slice fix.
- `app/data/screening_service.py` — SUM→MAX at latest year.
- `requirements.txt` — edgartools pinned to 5.16.0.

---

# Round 4 (same day) — ALL-123-Ticker Validation + Performance

## Performance (measured)
| Path | Before | After |
|---|---|---|
| XBRL totals, cold ticker | 11-40s (6 full XBRL parses) | 2.2-4.9s typical (companyfacts API + targeted parses only for uncovered years) |
| XBRL totals, warm | in-proc only, lost on restart | **0.1-0.2 ms** (disk cache, 24h TTL, survives restarts) |
| Additional Data fetch, warm ticker | 4.7-16s | **1.4 ms** (all tiers warm) |
| Additional Data fetch, cold server restart | 16s+ | ≤8.5s bounded (ratings wait 15s→5s; ratings also disk-cached now) |
| Full 123-ticker batch | n/a | 152s cold / 3-4s warm |

Mechanisms: (1) 3-tier totals fetch — disk cache → SEC companyfacts API (~1s, one HTTP
call, `fiscal_period='FY'` filter keeps FYE values and drops 10-Q interim counts) →
per-filing XBRL parses only for years companyfacts lacks (dimensioned-only taggers:
ROST/TSCO/COST-style). (2) Credit-ratings result disk-cached (24h) + phase-1 wait
15s→5s (cold fetch takes 30-60s; waiting longer only delayed renders). (3)
`preload_store_totals(ticker)` fires on every market_data page load next to the
ratings preload — Additional Data is warm before the user clicks it. (4) Render
stashes `get_ratings_data` in session_state; Excel export reuses it (was a full
second fetch per render). Disk caches live in `data/edgar_cache/` (gitignored).

## All-123-ticker validation (batch, staging DB read-only + live EDGAR)
- Sources after resolution: **35 XBRL (issuer-exact) / 84 validated DB / 4 honest empty**
  (CHWY, FNKO, PRMB, UFI — none operate retail stores; PRMB displayed 200,000 before).
- **15/15 manually-verified 10-K anchors exact** (WMT COST ULTA BBY HD* TGT KR ORLY AAP
  ANF CMG M ROST LULU URBN; *HD via DB row check).
- Gate refinement: spelled-out numbers ("five retail stores", "318 … and five in
  Canada") now count — recovers correct FIGS 5, WRBY 323, BRLT 43, AKA/YETI series.
- **Scope guard**: a validated DB value at the same-or-newer year that is >1% larger
  than XBRL's latest overrides XBRL (subsets are smaller). Caught BBW: XBRL tags
  553 (corporate+partner) but the true total incl. franchise is 662 — verified online.
- Headline fixes at latest year: DKS 42→856, LULU 58→811, URBN 9→784, TJX 5,890→3,305,
  PRMB 200,000→hidden, O 15,476→12,237 (its actual 2022 figure).
- Spot-verified new XBRL adoptions online: DXLG 295 ✓ (total fleet), LAD 455 vs 465
  wire count (−2%, issuer-tagged subset definition — review), BBW guarded back to DB ✓.

## Remaining review list (data-side; pipeline re-ingest, DB untouched)
Non-retail noise still in DB (ADT, BLD, DXC, O, SGI…), stale-year tickers (RH 2022=38
outlets-scope, SHW 2022, TJX 2021 US-only, UPBD 2021), LAD definition. These need the
retail-SIC filter + re-extraction in scripts/extract_store_counts.py (separate task).

## Files changed (Round 4)
- `app/data/repository.py` — 3-tier `_edgartools_store_totals`; recency guard;
  scope guard; word-number gate; ratings disk cache; 5s phase-1 bound;
  `preload_store_totals`.
- `app/pages/market_data.py` — totals preload on page load; session-stash reuse for
  Excel export.
- `.gitignore` — `data/edgar_cache/`.

---

# Round 5 (same day) — Full 341-Company Universe Coverage

## The question ("is it only 123 companies?")
The portal has **341 distinct tickers** (`coreiq_companies`), not 123. 123 was only
the set with `source='store_count'` rows in v5. The other **225 companies got no
store counts at all** — `get_ratings_data` early-returned before the XBRL phase
whenever a ticker had no DB rows and no regex credit ratings, so the EDGAR path
never ran for them (DG, MCD, SBUX, LOW, DLTR, CVS, CASY, KMX, GAP, JWN, ...).

## Fix
1. Early-return removed — phase 2 (XBRL totals + sqft) now runs for every ticker;
   the all-empty payload is returned at the end instead.
2. Tier-2 filing parses skipped when a company never tagged a store count in
   companyfacts (saves 6 wasted XBRL parses for ~190 non-retail tickers).
3. `_series_clean()` — median-outlier drop (>3x from median: LOW tags a stray 442
   amid ~1,850; LZB's 2012-14 are 2/3/9) then a >3x neighbor-jump rejection
   (3x not lower — DLTR's Family Dollar year is a legit 2.62x).
4. Explicit exclusion set for structurally-undetectable subset tagging: DRI tags
   only Ruth's Chris counts (~154, smooth series) vs its ~2,100 real fleet.

## Batch result over all 225 previously-empty companies (live EDGAR)
**18 major companies gain store counts** — all sanity-checked against public
counts: YUM 63,000 · MCD 45,356 · DG 20,893 · DLTR 16,700 · CVS 9,000 · WEN 7,397 ·
CASY 2,944 · VVV 2,200 · LOW 1,759 · AEO 1,500 · OLLI 645 · BOOT 539 · BKE 440 ·
SHOO 399 · ASO 322 · LZB 230 · DLTH 63 · MOV 53.
Correctly excluded: stale taggers (GME last tagged 2020, DBI 2016, DPZ footnotes),
single-year facts (AAPL 2015, SBUX, ABG, WOOF), 194 with no tagging (mostly
non-retail/international filers). UI-verified DG end-to-end (Total row
17,177→20,893, YoY sane, credit ratings now also appear via edgartools).

## Final coverage
137 of 341 companies show store counts (119 legacy + 18 new); the remainder either
run no stores or never tag counts in any filing. Regression suite ALL PASS;
123-ticker batch anchors 15/15 after every change.

## Files changed (Round 5)
- `app/data/repository.py` — early-return removal; tier-2 skip heuristic;
  `_series_clean`; DRI exclusion.

---

# Round 6 (same day) — Credit Ratings Accuracy (DB + edgartools)

## Found (all evidence-backed, manual online verification)
- DB (31 tickers, 363 rows): conflicting duplicates per (ticker, FY, agency)
  (KBH FY2024: BB vs B vs BB+; KHC BBB vs BBB-; AAP Baa2 vs Baa3); bogus
  distress ratings from regex noise (WMT Moody's "C" ×7 years — real Aa2;
  COST/PRMB/FND/COKE "C"/"B"); LLM-fabricated template cells verified wrong
  online: COST (shows Baa2, real Aa3/A+), TGT (Baa2/BBB, real A2/A/A),
  M (Baa2/BBB, real Ba1/BB+/BBB-), XRX (BBB 2025, real Caa2/CCC+ after the
  2025 downgrades), BBY FY2026 (fake Baa2 "downgrade"; Moody's affirmed A3
  Jan-2025).
- Live regex path (~310 tickers): McDonald's extracted as S&P "D" (bare-letter
  false positive); covenant thresholds ("below BBB-") and upgrade-transition
  sources ("raised from A- to A") captured as ratings; Item 1A risk-factor text
  scanned.

## Fixed (read-path, no DB writes)
- Notch scale (`_rating_notch`, AAA=1…D=22) powers: cell-level median dedupe
  (drop >2 notches from median, ≤1-notch spread or dash), series-level
  isolated-outlier drop (≥4 notches from every neighbor), CC/C/D ban,
  template-step guard (templated cell ≥2 notches from NEAREST non-templated
  year → dropped), verified-wrong blocklist {COST, TGT, M, XRX}.
- Live regex: bare B/C/D ban, distress-grade floor, covenant + "from" guards,
  Item 1A removed, disk cache (24h) + phase-1 wait 15s→5s.
- Verified after fix: WMT AA/Aa2/AA ✓, LOW BBB+/Baa1 ✓, BBY A3 through FY2026 ✓
  (live path filled the corrected year), DKS Baa3→Baa2 real upgrade kept ✓,
  MCD shows nothing rather than "D" ✓.

# Round 7 (same day) — Stores by Country RESTORED (requirement clarified)

Requirement: keep country-wise rows AND show the worldwide count as the last
row ("Total (Worldwide)"). Rebuilt `_edgartools_stores_by_country`:
- countries only (member qname `country:XX`; states/custom members excluded),
- ISO code → proper names ("Canadian Operations" → Canada),
- as-of-date keying (same convention as totals), minimal-dims preference,
  dedupe, 24h disk cache, preload warms it,
- **2% reconciliation gate**: a year's country rows display only when they sum
  to the verified worldwide total (COST reconciles exactly for all 6 years:
  795→914; ULTA 1,505+84+2=1,591 exact).
- Renderer + Excel export: countries (US first) then bold "Total (Worldwide)".
- Verified online: Costco Canada 110 ✓, Mexico 42 ✓, US 629 ✓ (Dec-2025
  snapshot 633/112/42/... = our Aug series one quarter later); Ulta per-country
  exact vs 10-K. 12 portal companies have genuine country-level tagging.

## Deliverable
`docs/reports/MDP_Data_Quality_Report_2026-07-15.xlsx` — 5 sheets:
Store Counts Worldwide (112 tickers × 2019-2026), Stores by Country (12
tickers, reconciled years only), Credit Ratings (33 tickers resolved),
Manual Verification (44 checks), Fixes & Method.

---

# Round 9 (same day) — Business feedback: non-store counts, layout, blank tab,
# data-team Excel reconciliation

1. **Non-store counts excluded** (Store Count = retail units only):
   count types `distribution centers / manufacturing facilities / properties /
   homes / franchises` dropped for everyone; 30 non-retail tickers excluded
   entirely (ADT, DXC, Tyson, Whirlpool, Zebra, homebuilders, REITs, PFG,
   UNFI, Coke Consolidated DCs, Qurate, ...). FND/COST "warehouses" stay —
   warehouse-format stores ARE their retail format. 94 legacy + 18 new = 112
   companies now display store counts; anchors 15/15 after change.
2. **Layout per business annotation**: when Stores by Country covers every
   year of the worldwide series, the separate "Store Count" section is hidden;
   the country section shows countries (US first) → bold left-aligned
   **Total (Worldwide)** → YoY Change beneath. When by-country covers fewer
   years (ULTA: only FY2026), both sections stay (no data loss).
3. **Blank-tab fix**: empty first paint now auto-retries twice (1.5s apart)
   while background fetches warm, instead of settling on an empty view;
   empty renders are never session-cached.
4. **Data-team Excel reconciled** (`store data.xlsx`, MDP vs SIP): every MDP
   figure matches our rebuilt values; variances vs SIP are SCOPE differences,
   not MDP errors — SIP counts US/brand-only (Nike SIP 243 US vs MDP 1,091
   worldwide; Macy's SIP 507 banner vs MDP 722 incl. Bloomingdale's/Bluemercury;
   FL SIP 873 brand vs MDP 3,129 all banners; Target variance ≈ 0). Their
   Costco sheet also shows the old duplicate "Canada"/"Canadian Operations"
   rows — fixed by ISO-code naming in Round 7.

---

## Round 10 — Column headers: filing dates → fiscal-year-end dates (2026-07-15)

**Report:** ANF's Additional Data columns showed `10-K Mar-29-2021 … Apr-01-2024 …`
while every other tab shows January dates.

**Root cause (verified against EDGAR):** the header dates were the SEC *filing
dates* (`coreiq_filing_metrics_v5.filing_date`; payload doc even said
`period_dates: {year: filing_date}`). ANF's FY ends the Saturday nearest Jan 31
and its 10-K is filed ~8 weeks later (2024's landed Mon Apr 1). All 119/119
checkable tickers had filing month ≠ FYE month — design-wide, not ANF-specific.
Data alignment was always correct; only the label type differed from the other
tabs (which use `fiscal_date_ending`, e.g. ANF = Jan-31 every year).

**Fix:**
- `repository.py get_ratings_data`: new `period_display_dates` = last day of the
  company's FYE month per year (reuses `SegmentDataRepository._fye_month` /
  `_fye_display_date`, same normalization the Segments tab uses). Safe keying:
  `report_fiscal_year` == calendar year of the fiscal period end for both
  Jan-FYE (ANF FY2021→Jan-2021) and Dec-FYE (CRI FY2023→Dec-2023) companies.
  `period_dates` (filing dates) kept untouched for internal logic + fallback
  when FYE month is unknown.
- `market_data.py`: header row and ratings Excel export prefer
  `period_display_dates`.

**Verified live (Playwright, staging DB):** ANF `Jan-31-2021…Jan-31-2026`
(values 735/729/762/765/789 unchanged under the right columns), ULTA `Jan-31-*`,
CMG `Dec-31-2024/Dec-31-2025` (FY2024 filed Feb-2025 correctly stays Dec-2024),
AZO payload `Aug-31-*`.
