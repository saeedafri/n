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

---

## Round 11 — Continent totals, tab performance, data-team Q&A (2026-07-16)

### 1. Continent grouping in Stores by Country (UI + Excel export)

**Requirement (business, 16-Jul):** single-continent footprint → total labeled
by its continent instead of "Worldwide"; multi-continent footprint →
per-continent subtotals first, then the worldwide total.

**Implementation:**
- `repository.py` (module level, above `RatingsDataRepository`):
  `CONTINENT_ORDER`, `COUNTRY_TO_CONTINENT` (7-continent model; covers the 30
  country names the edgartools extraction emits + a superset), `continent_of()`,
  and `group_countries_by_continent(countries)` → ordered
  `[(continent, [country, ...], {year: subtotal})]`, US-first within a bucket
  (exact-name match — "United States Virgin Islands" no longer floats first).
- `market_data.py` render + ratings Excel export both consume the shared helper:
  country rows grouped by continent; bold `Total (<continent>)` subtotal per
  group when multi-continent; final row `Total (<continent>)` when
  single-continent, else `Total (Worldwide)`.
- **Partial-split guard:** the continent label is applied ONLY when the listed
  countries account for the verified total exactly. The 2% reconciliation gate
  admits partial splits — CMG lists only US restaurants while its total includes
  the 10-K "international" aggregate — and those stay "Total (Worldwide)".

**Verified live (Playwright, staging DB):** COST → Total (North America)
692→781 / Total (Europe) 34→39 / Total (Asia) 57→78 / Total (Oceania) 12→16 /
Total (Worldwide) 795→914; ORLY → single `Total (North America)` 5,616→6,585;
PSMT → NA 46 + SA 10 → Worldwide 56; ULTA → NA 1,505 + Europe 86 → Worldwide
1,591; CMG payload probe → label stays `Total (Worldwide)` (guard works).

### 2. Additional Data tab performance

**Evidence (server-log 15-Jul):** warm renders ~1ms; FIRST render per ticker
2.0–4.3s. Root cause chain (probed 16-Jul):
- `_fetch_all_rows` was the bottleneck: `coreiq_filing_metrics_v5` (12.5M rows)
  has **no (ticker, source) composite index**, so MySQL serves the query from
  the source-only index and post-filters ticker → ~1,020 scattered row pages →
  ~5.7s on a cold Azure buffer (0.3s warm). ⚠ **Flag for data team: a composite
  index on (ticker, source) is the root fix; we cannot do DDL.**
- Cold EDGAR layers burned the bounded waits (2s CR + shared 4s phase-2).

**Fixes (app-side, read-only):**
- `_fetch_all_rows`: single round trip (was DISTINCT-years + one query per year
  fanned out over a pool = 1+N RTTs); `st.cache_data` TTL 300s → 13h.
- `_fetch_sqft_from_edgartools`: new 24h disk cache
  (`data/edgar_cache/sqft/<TICKER>.json`) mirroring the other three EDGAR
  families — it was the only path without one. Empty results cached too.
- `cache_manager` Track 3 — **12-hourly full-universe warm sweep** (leverages
  App Service Always On, `RATINGS_WARM_SWEEP=0` to disable): one buffer-pool
  warm query for the credit_rating/store_count row pages, then a throttled
  (1s/ticker) walk over all portal tickers warming `_fetch_all_rows`, the three
  EDGAR disk layers, `get_square_footage_data`, and `_fye_month`. First sweep
  on a fresh cache dir is slow (cold CR extraction 30–60s/ticker); later sweeps
  are cache validations (~15 min).

**Measured after fix:** ANF first open 886ms (was 3.8–4.0s); steady-state
post-sweep render 0.75ms (AZO, new session). Mid-first-sweep renders of
un-swept tickers can still hit the 4s bounded ceiling — one-time cost per
deploy.

### 3. Data-team Excel report refresh

`docs/reports/MDP_Data_Quality_Report_2026-07-16.xlsx` (+ copy in ~/Downloads),
new sheet **"Data Team Q&A (16-Jul)"** answering every 15-Jul comment:
- **CAL "Can we add a total"** → TOTAL (Worldwide) row added from validated
  XBRL totals (2020: 1,177 … 2026: 1,009); country rows stay hidden (the 10-K
  split is a Brand-Portfolio-segment subset that never reconciles). Rule applied
  generically to all hidden tickers with validated totals.
- **CMG "which is the other country?"** → not a country: the 10-K's aggregate
  "international" figure (Canada/UK/France/Germany, never split). Row labeled
  `International (not split by country in 10-K)`; values 40/44/53/66 = verified
  total − US, matching the 10-K exactly.
- **LOW / ORLY "Total North America"** → done via the single-continent rule.
- **PSMT "Total Americas"** → TOTAL (North America) + TOTAL (South America)
  subtotals then TOTAL (Worldwide) ("Americas" is not a continent in the
  7-continent model; Trinidad + Virgin Islands sit in North America).
- Sheet 2 now mirrors the live continent logic (continent subtotals +
  relabeled totals); Fixes & Method sheet gained rows 10–12 (FYE headers,
  continent totals, performance).

### Round 11b — user feedback fixes (16-Jul, afternoon)

**Bug: continent section could vanish for a whole session.** The tab pinned its
rendered HTML in `st.session_state`; a render that missed a bounded background
fetch (Stores by Country starved by first-sweep DB contention) was cached
incomplete and stuck until the session ended — user saw COST with no country
rows. Fix: pin REMOVED (both read and write). The data fetch is layer-cached
(st.cache_data 13h + 24h disk + sweep) and ~1ms warm, so rebuilding per rerun
is cheap and self-healing; the `_ratings_data_` stash for the Excel button
stays.

**Enhancements:**
- Indentation hierarchy (multi-continent): countries `indent-2` →
  `Total (<continent>)` bold `indent-1` → grand total bold `indent-0`.
  Mirrored in the ratings Excel export (indents 2/1/0).
- `Total (Americas)` when the footprint is exactly North + South America and
  the rows account for the total exactly — new shared helper
  `stores_total_label(grouped, total_row)` in repository.py used by UI, Excel
  export, and the data-quality report (AZO, PSMT get it).

**Full-universe calculation audit (all sbc cache files, app gating logic):**
8 tickers display country data; 7 are EXACT (grand total == sum of country
rows for every displayed year): AZO/PSMT → Total (Americas), LOW/MOV/ORLY →
Total (North America), COST/ULTA → Total (Worldwide). Continent subtotals ==
sum of their countries by construction (asserted). The single genuine gap is
CMG — total − rows = 40/44/53/66 = the 10-K's "international" aggregate, not
split by country — so it stays Total (Worldwide) by design.

**Verified live:** AZO `US+Mexico → Total (North America) 6,506…7,510; Brazil →
Total (South America) 43…147; Total (Americas) 6,549…7,657` (sums exact),
PSMT `NA 46 + SA 10 → Total (Americas) 56`, ORLY single `Total (North
America)`, COST 4 subtotals + `Total (Worldwide)` (795=692+34+57+12 … exact
every year). Report regenerated with the same labels.

### Round 11c — critical UX fixes (16-Jul, evening)

**1. "Old data first, real data after refresh" (critical).** First view of a
ticker right after a server restart rendered a PARTIAL payload (bounded
background fetches missed their deadline → DB store types like PSMT
Clubs/Warehouses appeared instead of the XBRL Total + Stores by Country).
Fixes:
- `get_ratings_data` now returns `_pending_fetches` — exactly which bounded
  waits timed out (db_rows / credit_ratings / store_totals /
  stores_by_country / square_footage).
- Render auto-heals: on a partial payload it logs `RATINGS_partial_payload`,
  shows a spinner, and reruns (≤2×) — the background fetch completes meanwhile.
  Verified cold-process PSMT: first fetch 4.3s flagged `pending=square_footage`,
  auto-rerun 2s later fetched in 0.8ms and rendered `Total (Americas) 56` —
  one visit, no manual refresh.
- Disk-cache writes are now atomic (`_write_cache_atomic`, temp + os.replace):
  the sweep rewriting a JSON while a render read it could yield a parse error
  → silently missing section.

**2. Totals looked same as normal rows.** `.row-bold` forces
`color: dark-grey !important` + semibold — visually indistinguishable from
black regular country rows. New `.row-total-strong` class (true bold, full
black) applied to continent subtotals and the grand total; country rows
explicitly regular weight.

**3. "Oceania" renamed** to "Australia & New Zealand" everywhere (business
clarity; only AU/NZ ever appear for portal retailers).

**4. Launch window (tabs unresponsive 10–15s).** STG log evidence
(user download, 07→16-Jul): the process restarted 21× in 9 days (3× on 16-Jul)
— every restart wipes in-process caches and re-triggers the cold window; boot
warmups + first-render DB fetches then compete for cold Azure I/O while
Streamlit queues clicks during script runs. App-side mitigations: warm sweep
now starts 5 min AFTER boot (was immediately) and paces 2s/ticker; explicit
`APP_PROCESS_BOOT pid=` marker logs once per process so restarts are countable.
⚠ Root causes outside app code: restart frequency (deploys/platform recycling
— flag to team) and the missing (ticker, source) DB index (data team).

### Round 11d — CAL showed "Chow Tai Seng Jewellery" with Caleres data (16-Jul)

**Root cause — corrupted rows in `coreiq_companies` (data team owns fix):**
one row per ticker mixes two different companies. Verified 16-Jul (STG):
- `CAL`: name='Caleres, Inc.' (correct, SEC/NYSE — matches every SEC data
  layer) but name_coresight='Chow Tai Seng Jewellery Co., Ltd.' and
  exchange='GPW' (Warsaw). Header/dropdown use name_coresight → the page
  titled a Chinese jeweller over Caleres' store data (1,086→960 = Caleres,
  correct data, wrong nameplate).
- `CFR`: name='CULLEN/FROST BANKERS, INC.' but
  name_coresight='Compagnie Financière Richemont SA', exchange='SWX'.
`coreiq_av_company_overview` is CORRECT for both (Caleres Inc/NYSE,
Cullen/Frost Bankers Inc/NYSE). Full-universe scan found no other rows where
name and name_coresight are different companies.

**App-side fix (read-only overlay, ma_event_overrides pattern):**
`app/data/company_display_overrides.json` + `company_display_overrides()`
merged into `CompanyRepository.get_companies_rows()` — the single source for
the dropdown, companies map, and Market Data header (get_company_overview
deliberately prefers name_coresight). Verified live: header now
"Caleres, Inc. (NYSE:CAL)".

**SQL for the data team (the real fix; remove the overlay entries after):**
```sql
UPDATE coreiq_companies
   SET name_coresight = 'Caleres, Inc.', exchange = 'NYSE'
 WHERE ticker = 'CAL' AND name = 'Caleres, Inc.';
UPDATE coreiq_companies
   SET name_coresight = 'Cullen/Frost Bankers, Inc.', exchange = 'NYSE'
 WHERE ticker = 'CFR' AND name LIKE 'CULLEN/FROST%';
```
(If Chow Tai Seng / Richemont are meant to be portal companies, they need
their own rows with their own tickers — every coreiq_* data table keys these
tickers to the US issuers.)

**Square Footage noise gate:** CAL also showed two junk rows
"Disposal Group, Held-for-Sale, Not Discontinued Operations (sq ft) = 9"
(duplicated by label capitalization). `get_square_footage_data` now drops
disposal-group / held-for-sale / discontinued-operations dimension slices and
merges case-insensitive duplicate labels. CAL's Square Footage section (which
contained only noise) is gone; real metrics elsewhere unaffected.

### Round 11e — landing-click delay instrumentation, click-blocking loader, CAL/CMG data visibility (16-Jul, night)

**1. Landing → immediate tab click felt dead (critical, STG).** Mechanics:
Streamlit executes a session's script runs SEQUENTIALLY — a click during the
landing run silently queues, then triggers a fresh full run; on STG the
landing run itself is slow (cold DB + 21 restarts/9 days), so clicks felt
dead for 10-15s with zero log evidence. Changes:
- `MD_RUN_START` log at the top of every market_data run: tab, ticker, and
  `gap_since_prev_run_end_ms` — a gap <100ms is flagged
  `interaction_QUEUED_behind_previous_run`. Together with the existing
  `PAGE_*`/`TAB_*` phase timings and the new `APP_PROCESS_BOOT` marker, STG
  logs now show precisely WHERE any wait went (queue vs render vs restart).
- The branded tab-loading overlay now BLOCKS background interaction
  (pointer-events auto + cursor:wait; was deliberately click-through). Users
  see a busy state instead of clicks that vanish into the queue; the 22s CSS
  failsafe still guarantees it can never trap (no !important on
  pointer-events so the failsafe keyframe wins).
- Local repro (Playwright, cold process): tabs visible +1.5s, click blocked
  1.5s by the visible busy overlay, Additional Data content 1.2s after click;
  MD_RUN_START gap log captured the click-run sequence.
- Fixed a latent `UnboundLocalError`: `main()` had a local
  `import streamlit as st` shadowing the module import.

**2. CAL "Excel shows countries, UI shows only worldwide".** The 2% recon
gate hid non-reconciling splits entirely. Now: a split that exists but never
reconciles renders AS DISCLOSED under
"Stores by Country (partial — as disclosed in 10-K; does not sum to total)" —
no subtotals, no total row (the validated Store Count total stays above).
Verified live: CAL shows US 107/70/63/62/60, Canada 50, China 13/16/29.
Report Sheet 2 aligned (values shown with the same partial note).

**3. CMG "no data in UI".** `get_date_range`/`get_available_dates` used DB
years only — CMG has just FY2024-25 in v5 while XBRL tags FY2020-25, so the
tab collapsed to two columns and the Stores by Country section (recon years
2020-23) fell outside the window. New `_all_known_years` merges DB years with
the disk-cached XBRL total years (bounded 3s). Verified live: CMG now spans
Dec-2020→Dec-2025, totals 2,764→4,042, country section renders.

### Round 11f — Excel completeness audit + STG slowness/RAM root cause (16-Jul, late night)

**Excel coverage:** programmatic audit — Sheet 1 holds all 112 tickers whose
store counts the app can actually display. The warm sweep has since produced
8 more XBRL cache files (AAPL, ABG, DBI, DPZ, DRI, GME, SBUX, WOOF); each was
resolved through the app's own gates and **correctly rejected**: AAPL one
stale year (463 @2015), DPZ franchise-deal counts (14/4/17/12/62 vs ~20k real
stores), GME last tagged 2020, SBUX single-year subset (113), DBI last 2016,
ABG single year =2, DRI blocklisted (Ruth's Chris subset). WOOF is the one
borderline (1,433/1,423 @2022/24 — plausible but only 2 years; the ≥3-year
gate holds) — flagged to the data team.

**STG slowness root cause (from the user's 07→16-Jul log pull):**
memory exhaustion → restart loop → permanent cold-start window.
- RSS: median 940MB, p90 1.16GB, p99 1.71GB, **max 2.84GB**; 16-Jul 17:04 the
  process sat at 2.4GB (malloc_trim reclaimed only 230MB → live Python
  objects, not heap fragmentation) and by 17:08 the same worker was at 102MB
  — recycled. 21 restarts in 9 days.
- Each restart re-triggers: cold Azure DB buffer (ratings query 5.7s),
  51s filings prefetch warm (background but I/O-competing),
  calendar FYE bulk 2.7-4.9s, empty st.cache_data everywhere.
- WARM steady-state is already fast: calendar CLICK→RENDER 0.28-0.53s in the
  same log; ratings ~1ms. Users are not slow because the code is slow — they
  are slow because the app keeps being reborn.
- Historical tab pain (mostly pre-16-Jul fixes): TAB_Ratings avg 8.3s
  (n=44, max 111s), TAB_Segments avg 4.4s, Company_Profile max 9.3s.

**Recommendations (ranked):**
1. Raise App Service memory headroom to ≥4GB (peaks hit 2.84GB) — stops the
   restart loop; with Always On + the 12h warm sweep the app then stays warm
   24/7 and calendar/market-data sit at their measured 0.3-1s warm numbers.
2. Heap-profile a live worker (tracemalloc snapshot endpoint or dump on
   HEARTBEAT when rss>2GB) to attribute the growth — likely large long-TTL
   caches; fix at the source rather than guessing. (Offer open.)
3. Existing instrumentation to watch: `[HEARTBEAT] app_rss=`,
   `[MALLOC_TRIM]`, `restarts.log` ledger, new `APP_PROCESS_BOOT`,
   `MD_RUN_START gap_since_prev_run_end_ms`.

### Round 11g — STG forensics: empty ratings tab, login bounce, retry churn (16-Jul, 19:30)

**Evidence base:** user log pull server-logs-20260716_131633.log (07-Jul → 16-Jul 18:46).

**Finding 1 — STG deploys ship with NO edgar caches.** `data/edgar_cache/`
is gitignored (line 734); Azure redeploys replace wwwroot, so all four disk
cache families start EMPTY after every deploy. STG restarted/deployed 6× on
16-Jul alone (12:42, 16:24, 16:56, 17:30, 17:59, 18:40); the warm sweep
(needs hours cold — CR extraction is 30-60s/ticker) started at 17:35, 18:04,
18:46 and was killed by the next restart every time → caches never built →
every user view stayed cold all day.
Fix: `edgar_cache_dir()` honors new env var **EDGAR_CACHE_DIR** (all four
cache families). STG/PROD must set it to a persistent path, e.g.
`/home/edgar_cache` (App Service /home survives restarts AND deploys).

**Finding 2 — retry churn: pending=credit_ratings retry=2 ×14.** On cold STG
the CR fetch can never finish inside the bounded wait, so the auto-heal loop
burned ~15s of spinner (2 retries × (2s sleep + 2-6.8s refetch — CMG 6863ms,
CRI 6453/6326/5673ms)) and then rendered the same table anyway. The user's
screenshot (CMG, empty tab area) is this loop mid-flight. Fix: retry-rerun
only when a STORE-shaping fetch is pending (db_rows / store_totals /
stores_by_country / square_footage); credit-ratings-only partials render
immediately and CR fills on a later rerun.

**Finding 3 — "login issue":** both `[OIDC] invalid/expired state` errors
(07-Jul 18:17:11+13) coincide EXACTLY with the 18:17:11 restart — a restart
mid-login invalidates the state and bounces the user. Same restart-storm root
cause. (16-Jul's only auth error is a correctly-denied personal gmail.)

**Finding 4 — old noise ruled out:** `key='set'` localStorage errors and
LocalStorageManager init failures are all from 07/09/15-Jul — pre-existing,
unrelated to this week's changes.

**Deploy checklist for STG:** set `EDGAR_CACHE_DIR=/home/edgar_cache`;
stop deploying 6×/day to prod-like envs (each deploy = full cold start);
memory headroom ≥4GB (Round 11f).

### Round 11h — storage benchmark on STG (csr-awa-data-portal-stg, P0v3)

Measured on the App Service worker (200 small JSONs, same atomic write the app uses):

| storage | write/file | cold read/file | single read | survives deploy? |
|---|---|---|---|---|
| /home (persistent share) | 29.33 ms | 2.40 ms | 0.105 ms | YES |
| /tmp (local SSD)         |  0.13 ms | 0.03 ms | 0.033 ms | NO |

**Decision: EDGAR_CACHE_DIR=/home/edgar_cache.** /home is ~80× slower per cold
read, but 2.4ms vs the 30-60s EDGAR fetch it replaces = ~20,000× cheaper, and
st.cache_data absorbs the hot path (disk touched a few times/hour/ticker).
The 29ms/file write only affects the 12-hourly sweep: ~341 tickers × 4 families
≈ 1,364 files ≈ 40s spread over hours — invisible. /tmp's speed advantage
(0.07ms/read) is unmeasurable to users and is lost on every deploy, which is
exactly what kept STG cold all of 16-Jul.

**Worker facts:** SKU=P0v3 → 1 vCPU / 4,794MB total RAM; 16-Jul peak RSS 2.84GB
(~60% of the box in one process). For the stated target of 30-40 concurrent
users, 1 vCPU is the hard bottleneck: Streamlit renders in Python (GIL-bound),
so concurrent sessions serialize on CPU. Recommend P2v3 (4 vCPU/16GB) and
deployment slots + swap for zero-downtime releases.

### Round 11i — STG index created + RAM theory RETRACTED (16-Jul, 21:05)

**1. Index created on STG (user-authorized DDL).**
```sql
ALTER TABLE coreiq_filing_metrics_v5
  ADD INDEX idx_v5_ticker_source_fy (ticker, source, report_fiscal_year),
  ALGORITHM=INPLACE, LOCK=NONE;
```
Online build ~29 min on the 14.4M-row / 13.4GB table; readers never blocked.
The 3rd column also satisfies `ORDER BY source, report_fiscal_year` → no filesort.

| | before | after |
|---|---|---|
| index chosen | idx_v2_source | **idx_v5_ticker_source_fy** |
| rows examined | 1,020 | **8** |
| filter efficiency | 0.67% | **100%** |
| fetch (from laptop, incl. ~250ms×2 Azure RTT) | 5,718ms cold | **avg 712ms** (10 fresh tickers) |

⚠ **PROD still needs the identical index** — hand the SQL above to the data team.
Note: client connections time out ~10 min while the ALTER continues server-side;
poll `information_schema.processlist` (state='altering table'), do NOT re-run.

**2. RETRACTION — the OOM/memory-exhaustion theory was WRONG.**
Round 11f claimed memory exhaustion drove the restart loop. Live cgroup data
from the STG worker refutes it:
- `memory.failcnt = 0` → the container has NEVER hit a memory limit
- `memory.limit_in_bytes = 9223372036854771712` → no cgroup ceiling at all
- current app RSS = 261MB (container only 7 min old)
The high RSS in the logs was real, but nothing was OOM-killed. **The 21 restarts
are almost certainly deploys** (6 on 16-Jul alone, matching the day's release
activity) — a process problem, not a hardware one.
What IS real: `Swap: used 1132MB` on a 4,794MB box under light load — memory
pressure that makes everything slower without killing anything. P0v3 reports
nproc=2.

**Corrected recommendations:** (1) deployment slots + swap = zero-downtime
releases (kills the cold-start storm at its true source); (2) EDGAR_CACHE_DIR
=/home/edgar_cache on both slots; (3) P2v3 (4 vCPU/16GB) for the stated 30-40
concurrent users — justified by swap pressure + Python GIL serialization, NOT
by OOM.

### Round 11j — ROOT CAUSE of "site is slow to open": geography + HTTP/1.1 (16-Jul, 21:30)

**The app is NOT slow. The network is.** Measured from the user's machine
(Gurugram, India) against https://marketdata-stg.coresight.com:

| phase | measured | note |
|---|---|---|
| DNS | ~4 ms | cached |
| **TCP connect** | **233-338 ms** | = ONE round trip to the server |
| **TLS handshake** | **+~490 ms** | on top of TCP |
| **TTFB** | **1,013-1,748 ms** | for an 8 KB page |
| server-side work | **0.03 ms** | MAIN_AUTH_BOOTSTRAP, from the same log |

**Why:** `remote_ip=13.89.172.9` → **Azure App Service `centralus`, Des Moines,
Iowa, USA**. Users are in Gurugram, India — ~12,000 km. 233 ms RTT is physics +
routing, not code. Every request, asset and websocket frame pays it.

**Aggravator: HTTP/1.1.** ALPN negotiates `http/1.1` (verified with
`openssl s_client -alpn h2` and `curl --http2` → still 1.1). No multiplexing →
the browser opens up to 6 parallel connections and **each one re-pays TCP+TLS
(~720 ms)**. The landing page pulls 21 files / ~1.1 MB (biggest: Material
Symbols font 353 KB, Source Sans font 166 KB).

So: landing ≈ (720 ms connection setup × several connections) + N × 233 ms RTT
+ 1.1 MB transfer + websocket handshake + a sub-millisecond Python run.
That is the 10-15 s users feel, and no app-side change can fix it.

**Fixes, ranked by impact (all infrastructure, no code):**
1. **Azure Front Door / CDN in front of the app** — terminates TLS at an Indian
   edge POP (Mumbai/Chennai): connection setup ~720 ms → ~40 ms, static assets
   (the 1.1 MB) served from the edge, and the origin leg rides a warm pooled
   connection. Biggest win without moving anything.
2. **Enable HTTP/2** (App Service → Configuration → General settings → HTTP
   version 2.0). One toggle, free, removes the per-connection TLS multiplier.
3. **Host the app in Central India** if the user base is India-based — RTT
   233 ms → ~20-30 ms on EVERY round trip. (Keep app and MySQL in the SAME
   region; the DB is already Azure MySQL Flexible — check its region first.)

**New instrumentation shipped:**
- `core/perf_panel.py` + `?perf=1` on any URL → live browser panel: DNS / TCP /
  TLS / TTFB / DOM / load / FCP / asset count+bytes / slowest asset / protocol.
  Verified working locally (21 files, 1,144 KB, protocol http/1.1).
  MUST use components.v1.html — st.html markup never executes <script>.
- `APP_RUN_START` in main.py for EVERY page: page name +
  `gap_since_prev_run_end_ms`, flagged `interaction_QUEUED_behind_previous_run`
  when <100 ms (mirrors MD_RUN_START).
