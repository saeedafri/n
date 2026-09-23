# STG-2 missing filings + segment data — root-cause report

**Date:** 2026-09-22
**Reported by:** business team (screenshots: AFRM, SEZL, PYPL, ZIP)
**Symptoms:** Segment tab shows "No extracted data available"; Company Filings Year
dropdown offers only `2025`; ZIP shows red "Filing not found in Azure".
**Verdict:** NOT an app bug. STG-2's `coreiq_filing_metrics_v5` has been frozen since
**2026-08-25**, and `coreiq_filing_documents` since **2026-04-16**. ZIP is a separate,
genuine coverage gap present in both environments.

---

## 1. The two environments

| | STG-1 (Testing 1) | STG-2 (Testing 2) |
|---|---|---|
| DB | `csr-mysql8-flex-stg…` / `coresight_market_data_stg` | `10.2.1.9` / `coresight_market_data_pro` |
| creds | `.env` (`STG_DB_*`) | `.env2` (`PROD_DB_*`) |
| Azure blob account | `csmarketdata` | `csmarketdata` (**same**) |

The blob store is shared. Only the DB differs. That is the whole story.

## 2. Freshness comparison (measured 2026-09-22)

| marker | STG-1 | STG-2 | gap |
|---|---|---|---|
| `coreiq_companies` rows | 510 | 510 | in sync |
| `coreiq_companies` max `data_inserted_at` | 2026-09-11 12:22:52 | 2026-09-11 12:22:52 | in sync |
| `coreiq_filing_metrics_v5` MAX(id) | 95,075,638 | 92,985,205 | **2,090,433 rows missing** |
| `coreiq_filing_metrics_v5` newest row ts | 2026-09-21 03:42:05 | **2026-08-25 20:39:47** | **28 days stale** |
| `coreiq_filing_documents` rows | 107,725 | 14,063 | **93,662 rows missing** |
| `coreiq_filing_documents` newest | 2026-09-22 04:47:02 | **2026-04-16 14:30:28** | **5 months stale** |

AlphaVantage financial tables DID sync (AFRM 36 income-statement rows, overview 1 row
on both). That is why Income Statement / Key Stats / Ratios look normal on STG-2 while
Segment and Company Filings are empty — they read different tables.

## 3. Per-ticker evidence

| ticker | STG-1 v5 | STG-2 v5 | STG-1 filing_documents | STG-2 | Azure blobs |
|---|---|---|---|---|---|
| AFRM | 38,464 | **0** | 99 | **0** | 497 (2021-2026) |
| SEZL | 11,862 | **0** | 144 | **0** | 841 (2021-2026) |
| PYPL | 53,275 | **0** | 213 | **0** | 1,066 (2016-2026) |
| ZIP  | **0** | **0** | **0** | **0** | **0** |
| WMT (control) | 51,936 | 50,965 | 255 | 185 | — |

Segment-eligible rows on STG-1 (`is_dimensioned=1`, `doc_type='10-K'`, segment axes):
AFRM 18–24/yr FY2021-FY2026, SEZL FY2021-FY2025, PYPL FY2015-FY2025. On STG-2: 0.

## 4. Blast radius on STG-2

510 companies, **78 with zero `coreiq_filing_metrics_v5` rows**:
- 48 `source='YFinance'` — expected, they have no SEC filings.
- **30 `source='SEC'`** — these are broken for users:
  - JWN (Nordstrom, taken private — legitimately absent)
  - 2026-08-25 batch: PLAY, RRGB, SAM, UTZ
  - 2026-08-28 batch (BNPL/fintech): AFRM, ALLY, BFH, CFG, GDOT, KLAR, KPLT, PRG, PYPL, SEZL, SYF
  - 2026-09-11 batch (food/CPG): CALM, CENT, CHS, FDP, FRPT, HAIN, LANC, LOVE, NAPA, SMPL, THS, VITL, VLGEA, WEST

The cut-off is exactly the v5 freeze date: **every SEC company added on or after
2026-08-25 is invisible on STG-2.** On STG-1 the same probe returns only 49 misses
(48 YFinance + JWN) — i.e. STG-1 is healthy.

## 5. Why the UI looks the way it does

`coreiq_companies` is the source of the Company dropdown
(`_load_companies_from_db` → `CompanyRepository.get_companies_rows`), and it IS in sync.
So the company is selectable while every filings table behind it is empty.

**Company Filings** (`app/pages/company_filings.py`)
- `_prefetch_ticker_filter_data(ticker)` (line 1941) reads **only**
  `coreiq_filing_metrics_v5`. 0 rows → `all_years == []`.
- Line 3333: `available_years = (… ) or ["2025"]` → the Year dropdown becomes the
  **hardcoded literal `["2025"]`**. That is the single option in the screenshots.
- Line 3341: `available_doc_types = … or DOCUMENT_TYPES` → `DOCUMENT_TYPES[0]` = `"10-K"`.
- `_render_filing_header_html` (line 2114) prints `filing_date` and
  `Fiscal Period: …` only when DB metadata exists — in the screenshots both are absent,
  confirming the DB returned nothing. On STG-1 the same page shows
  `2026 • Annual • Aug 27, 2026 • Fiscal Period: FY2026`.
- The document still rendered because the blob `AFRM/2025/10-K/filing.html` exists in the
  shared storage account. So users see a real 2025 10-K and no way to reach 2021-2026.

**Segment tab** (`SegmentDataRepository`, `app/data/repository.py` ~line 9998)
- `_YEAR_SQL_TPL` / `_ROW_SQL_TPL` read `coreiq_filing_metrics_v5` with
  `is_dimensioned = 1 AND doc_type = '10-K' AND (dimension LIKE …)`. 0 rows → the
  edgartools fallback also yields nothing → `_build_table` returns `""` →
  "No extracted data available. Check official filings."

**ZIP** — a different problem, present in BOTH environments:
- `coreiq_companies.ticker='ZIP'` = Zip Co Limited (ASX, `cik NULL`, `source='YFinance'`),
  so there is no SEC feed at all, and 0 blobs exist under `ZIP/`.
- The same `["2025"]` + `"10-K"` fallback builds the path `ZIP/2025/10-K/filing.html`,
  Azure 404s → red "Filing not found in Azure". Retry can never help.
- `ZIP` is also a ticker collision: `coreiq_sec_companies_all.ticker='ZIP'` is
  ZipRecruiter (cik 0001617553). Never ingest ZIP by bare ticker — it would file
  ZipRecruiter's 10-Ks under Zip Co. ASX docs must come via
  `company_filings_add_files.py`.

## 6. Why the sync silently fails

`coreiq_pipeline_runs` / `coreiq_pipeline_run_steps` on STG-1 show two separate steps:

```
stg_to_prod_migration            … --skip coreiq_filing_metrics_v5   (works)
stg_to_prod_filing_metrics_v5    … --only coreiq_filing_metrics_v5   (reports success, writes nothing)
```

Script: `python3 /Users/shashankgupta/market-data-updates/production/stg_to_prod_migration.py`

- The `--skip` step succeeds nightly → that is why `coreiq_companies` and the AV
  financial tables are current on STG-2.
- The `--only coreiq_filing_metrics_v5` step reports `status=success, exit=0` on
  2026-08-30, 08-31, 09-01, 09-02, 09-04…09-19, yet STG-2's newest v5 row is still
  2026-08-25 20:39:47. **The step's success status is not trustworthy** — it exits 0
  without transferring rows.
- It is also at its runtime limit: 5h45m (09-07) → 9h28m (09-12) → 11h41m (09-14), and
  on 2026-09-21 it died after **12h 22m** with `Pipeline crashed: InterfaceError: (0, '')`
  (run 652, `stg_to_prod_filing_metrics_v5 failed with exit=1`).
- `coreiq_filing_documents` appears in no migration step at all — hence the 5-month gap.

Schedule (UTC, from `coreiq_pipeline_runs`): `coreiq_data_ingest` 01:30,
`coreiq_docs_ingest` 18:00, `coreiq_news_and_events_ingest` 22:10.

## 7. Verification performed

Local app on **http://localhost:8501** (`bash .claude/dev/run_local.sh`, STG-1 DB,
current `main`), headless Playwright:

- `/company_filings?ticker=AFRM&doc_type=10-K` → Year options
  `['2026','2025','2024','2023','2022','2021']`; SEZL identical; PYPL
  `2026…2017`. Header: `2026 • Annual • Aug 27, 2026 • Fiscal Period: FY2026`.
- `/market_data?ticker=AFRM&tab=segment_data` → Business Segments (Card network,
  Merchant network) and Geographic Segments (United States, Canada, Other) render for
  FY2021-FY2026. SEZL and PYPL also render both tables.

`app/pages/company_filings.py` and `app/components/toolbar.py` are byte-identical
between this repo and `market-data-stg` HEAD, so no code difference is involved.

## 8. What has to happen (data-team actions — no app change)

1. Fix `stg_to_prod_migration.py --only coreiq_filing_metrics_v5`: it must fail loudly
   instead of exiting 0 with 0 rows moved, and it must be chunked/resumable so a 12-hour
   run cannot lose everything to `InterfaceError: (0, '')`.
2. Backfill STG-2 `coreiq_filing_metrics_v5` from STG-1 for ids > 92,985,205
   (~2.09M rows), covering the 29 SEC companies listed in §4.
3. Add `coreiq_filing_documents` to the migration and backfill it (14,063 → 107,725).
4. ZIP / HUM / BOO (ASX, LSE) will still be empty — that needs manual upload through
   `/company_filings_add_files`, not the SEC pipeline.
5. Optional app hardening (separate task, not part of this report): replace the
   `or ["2025"]` / `or DOCUMENT_TYPES` fallbacks at `company_filings.py:3333` and
   `:3341` with a proper "No filings available for this company" empty state, so a
   coverage gap never masquerades as a wrong-year document or a red Azure error.
