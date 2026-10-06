# Company Filings — "no 10-K under Year 2025" (MCD and ~all December-FY companies)

**Date:** 2026-10-06
**Page:** `/company_filings`
**Reported symptom:** For McDonald's (MCD), selecting **Year = 2025** shows a Document Type
list of `10-Q-Q1, 10-Q-Q2, 10-Q-Q3, 8-K, DEF14A, DEFA14A` — **no 10-K**.
**Verdict (revised — see §8):** Two causes that only bite together.
1. **Data:** `coreiq_filing_metrics_v5.storage_year` is stale for the newest 10-K (2026, filing
   year) while the Azure blob was already re-bucketed to the fiscal year (`MCD/2025/10-K/filing.html`
   exists and holds the FY2025 10-K).
2. **App code:** commit `39e578e` (2026-06-23) made `storage_year` the Year key (it was
   `fiscal_year` before), and commit `7e1282a` (2026-07-18) made Year the parent filter.
   Together they hide the 10-K from Year 2025. Both commits are authored by Mohd Saeed Afri.
No filing is lost — the FY2025 10-K opens under Year **2026**.

---

## 1. Symptom reproduced

Local app (`bash .claude/dev/run_local.sh` → `http://localhost:8501/company_filings`,
staging DB), driven headless:

```
Company = McDonald's (MCD)
Year option #0 ('2026') -> Document Types: ['10-K', '10-Q-Q1', '10-Q-Q2', '8-K', 'DEF14A', 'DEFA14A']
Year option #1 ('2025') -> Document Types: ['10-Q-Q1', '10-Q-Q2', '10-Q-Q3', '8-K', 'DEF14A', 'DEFA14A']   ← no 10-K
Year option #2 ('2024') -> Document Types: ['10-K', '10-Q-Q1', '10-Q-Q2', '10-Q-Q3', '8-K', 'DEF14A', 'DEFA14A', 'PRE14A']
```

Selecting Year = 2026 + Document Type = 10-K renders the correct document, with the header
`McDonald's (MCD) · 2025 · Annual · Feb 24, 2026 · Fiscal Period: FY2025`.

---

## 2. Where the dropdown comes from

`app/pages/company_filings.py:1966` `_prefetch_ticker_filter_data()` is the only source:

```sql
SELECT doc_type,
       COALESCE(storage_year, report_fiscal_year, fiscal_year) AS bucket_year,
       COALESCE(fiscal_year, storage_year, report_fiscal_year) AS display_year,
       fiscal_quarter, COUNT(*)
FROM coreiq_filing_metrics_v5
WHERE ticker = :ticker
GROUP BY ...
```

* **Year** dropdown options = the distinct `bucket_year` values (`company_filings.py:3360`).
* **Document Type** dropdown = only the types present in the selected `bucket_year`
  (`company_filings.py:3368`).
* The Year *label* is `display_year` (the DEI fiscal year) via `_fiscal_label_for()`.

So a 10-K only appears under Year *Y* if a row exists with `bucket_year = Y`.

---

## 3. Root cause — the data

MCD's 10-K rows in `coreiq_filing_metrics_v5`:

| storage_year | report_fiscal_year | filing_date | data_insert_timestamp |
|---|---|---|---|
| **2026** | **2025** | 2026-02-24 | **2026-05-29 12:38** |
| 2024 | 2024 | 2025-02-25 | 2026-06-01 13:27 |
| 2023 | 2023 | 2024-02-22 | 2026-06-01 13:27 |
| 2022 | 2022 | 2023-02-24 | 2026-06-01 13:27 |
| 2021 | 2021 | 2022-02-24 | 2026-06-01 13:26 |
| 2020 | 2020 | 2021-02-23 | 2026-06-01 13:26 |

Every historical 10-K is bucketed by **fiscal year**. The FY2025 10-K alone is bucketed by
**filing year** (2026). ~~The blob path follows the same mistake~~ — **wrong, see §8**: the blob
exists in BOTH `MCD/2026/10-K/` (old copy, 2026-05-04) and `MCD/2025/10-K/` (fiscal-year bucket,
2026-05-28). Only the DB row was never moved.

Consequence: bucket 2025 contains MCD's three 10-Qs but no annual report, so the
Document Type list for Year 2025 legitimately has no 10-K.

### Two ingestion runs, two different rules

The insert timestamps show it was not a code-version drift but **two runs with different
bucketing logic**:

* **2026-05-29 run** (newest filings) → `storage_year = YEAR(filing_date)`
* **2026-06-01 run** (history backfill) → `storage_year = report_fiscal_year`

### It is systemic, not MCD-specific

Same pattern on every December-fiscal-year company checked:

| Ticker | latest 10-K | prior 10-K |
|---|---|---|
| MCD | storage 2026 / FY 2025 (filed 2026-02-24) | storage 2024 / FY 2024 |
| AMZN | storage 2026 / FY 2025 (filed 2026-02-06) | storage 2024 / FY 2024 |
| KO | storage 2026 / FY 2025 (filed 2026-02-20) | storage 2024 / FY 2024 |
| PEP | storage 2026 / FY 2025 | storage 2024 / FY 2024 |
| YUM, DPZ | storage 2026 / FY 2025 | storage 2024 / FY 2024 |

Non-December fiscal years are unaffected because filing year and fiscal year agree
(WMT, TGT, NKE, PG, AAPL, SBUX all have `storage_year == report_fiscal_year`).

**356 tickers** currently have a 10-K sitting in `storage_year = 2026`.

(CMG is a separate, older case — its 10-Ks have always been off by one: storage 2025 /
FY 2024, storage 2024 / FY 2023.)

### Pre-dates `_v5`

`coreiq_filing_metrics_v4` carries the same thing: MCD's Feb-2026 10-K is stored with
`fiscal_year = 2026`, 1,297 rows — the exact row count that v5 inherited (1,272 + 25).
The mis-bucketing therefore came in with the data, before any v5 display-year work.

---

## 4. Application code (first review — superseded by §8)

* **No app code writes `storage_year`.** Every reference in `app/` reads it
  (`company_filings.py`, `main.py:568`, `repository.py:7339`, `llm_extractor.py:256`,
  `filing_units.py:104`). The column is produced entirely by the ingestion pipeline.
* Commit `7e1282a` (2026-07-18) changed the Document Type dropdown from
  "every type the ticker ever filed" to "only the types present in the selected year"
  (`_get_available_doc_types_from_db` → `_get_available_doc_types_for_year`). Before that
  commit, 10-K would still have been *listed* under Year 2025 — and would have pointed at a
  `MCD/2025/10-K/` bucket that does not exist. The commit did not remove any filing; it made
  an already-wrong year bucket visible as an absent option.
* The three most recent commits touching this page (2026-10-01, 2026-10-03) contain **no**
  changes to `doc_type`, `storage_year`, `bucket_year` or the year/doc-type dropdown logic.
  There are no uncommitted changes to `company_filings.py` in the working tree.
* Repo authorship: all 74 commits in this repository are by Mohd Saeed Afri. The ingestion
  pipeline that writes `coreiq_filing_metrics_v5` and the blob folders lives outside this
  repository and is owned by the data team.

---

## 5. Secondary UI defect that follows from this

With Document Type = 10-K selected, the Year dropdown renders **two options both labelled
"2025"**:

```
YEAR OPTIONS with 10-K selected: ['2025', '2025', '2024', '2023', ...]
```

Bucket 2026 is labelled with its DEI fiscal year (2025), while bucket 2025 falls back to its
own number. One works, one is empty. The collapsed widget still shows the raw bucket (2026),
so the label the user picks and the value they see disagree.

---

## 6. Fix options

**Primary (data team, correct fix).** Re-bucket the 2026-05-29 batch so
`storage_year = report_fiscal_year` for annual filings, and move / re-point the blob folder
`<TICKER>/2026/10-K/...` → `<TICKER>/2025/10-K/...`, matching every prior year. Then make the
incremental ingestion use the same rule as the backfill so next February does not repeat it.

**Interim (app side, if the data cannot be corrected quickly).** Drive the Year dropdown from
`display_year` (dedup) and translate back to `bucket_year` only when loading the blob — the
split already exists in `_prefetch_ticker_filter_data()`. This also removes the duplicate
"2025" entries. Not applied; it is a behaviour change to the year contract and should be a
deliberate decision, not a hotfix.

---

## 8. Second review (2026-10-06) — corrections

### 8.1 The 2025 blob folder exists

Azure container listing (`list_blobs`):

```
MCD/2025/10-K/filing.html            2,182,264 B  2026-05-29   accession 0000063908-26-000035, period_end 2025-12-31
MCD/2026/10-K/filing.html            2,182,264 B  2026-05-04   same accession (older filing-year copy)
AMZN/2025/10-K/filing.html           1,968,341 B  2026-06-20   accession 0001018724-26-000004, fiscal_year_label 2025
AMZN/2026/10-K/filing.html           1,968,341 B  2026-02-28   same accession (older copy)
```

The blob writer switched to fiscal-year buckets (`MCD/2025/10-K/filing.meta.json` has
`filing_year: 2025`, `period_end_date: 2025-12-31`). The DB rows for the same accession still say
`storage_year = 2026`. **The DB and the blob disagree; the blob is right.**

### 8.2 The app used to show 10-K under 2025

`git log -S` on the prefetch query:

| Commit | Date | Year key in the dropdown |
|---|---|---|
| `3f2c032` initial | 2026-06-11 | `COALESCE(fiscal_year, storage_year, report_fiscal_year)` → fiscal first |
| `39e578e` "testing" | 2026-06-23 | `COALESCE(storage_year, report_fiscal_year, fiscal_year)` → **storage first** |

Same DB, both expressions run today:

```
MCD  10-K years | OLD fiscal-first: [2026, 2025, 2024, 2023] | NOW storage-first: [2026, 2024, 2023, 2022]
AMZN 10-K years | OLD fiscal-first: [2026, 2025, 2024, 2023] | NOW storage-first: [2026, 2024, 2023, 2022]
```

Under the old key, Year 2025 + 10-K builds `MCD/2025/10-K/filing.html`, which exists — so it
would have opened. (The stray 2026 comes from 25 v5 rows with `fiscal_year = 2026`.)

### 8.3 When it became visible

* `39e578e` (06-23): Year values became storage buckets but labels kept the fiscal year
  (`_fiscal_label_for`). With Document Type as the parent, picking 10-K showed bucket 2026
  labelled "2025" — users still found it.
* `7e1282a` (07-18): Year became the parent filter (`all_years`), Document Type scoped to the
  bucket. Year "2025" now means bucket 2025 → no 10-K. This is the change users see.

CapIQReplacement is a downstream copy. The team repo is
`/Users/mohdsaeedafri/All-Code-Base/market-data-stg` (branch `stg-deploy`, Bitbucket
`coresight_admin/marketdata`, HEAD `1c21ec5e` = origin). Its history, which is authoritative:

| Change | Commit | Author | Date |
|---|---|---|---|
| Year key fiscal-first → **storage-first** (`bucket_year`), plus `_fiscal_label_for` labels | `1511451e` "changing the comany filings version from v4 to v5" | **Shashank Gupta** | 2026-06-21 |
| Current query text (`company_filings.py:1989-2006`, adds `fiscal_quarter` / `COUNT`; keeps storage-first) | `f09565cc` "fixing a filing date bug" | **Shashank Gupta** | 2026-08-06 |
| Year as parent filter, Document Type scoped to year (`:3360`, `:3368`) | `aa678199` "Debug Logs added in Filings page" | Mohd Saeed Afri | 2026-07-18 |
| v4→v5 table rename only (does not touch `bucket_year`) | `e5f5a951` | Mohd Saeed Afri | 2026-06-23 |

CapIQReplacement `39e578e` (06-23) and `7e1282a` (07-18) are those same changes copied in.
`git blame HEAD` today: query lines → Shashank Gupta; Year-parent lines → Mohd Saeed Afri.
No uncommitted change to `company_filings.py` in either repo.

### 8.4 Corrected fix

* **Data (root):** set `storage_year = report_fiscal_year` for the 2026-05-29 10-K batch in
  v5 (356 tickers). No blob move needed — the 2025 folders already exist. Data team owns the DB.
* **App (independent of data):** key the Year dropdown on `display_year` / `report_fiscal_year`
  and use it as the blob year — the blob layout is fiscal-year now. This also removes the
  duplicate "2025" labels. Not applied; awaiting decision.

---

## 7. Verification evidence

* App URL used: `http://localhost:8501/company_filings` (local launcher, staging DB).
* Dropdown contents captured headless for MCD across the first three year buckets (§1).
* Screenshot of Year = 2026 + 10-K rendering the FY2025 annual report.
* DB probes against `csr-mysql8-flex-stg` on `coreiq_filing_metrics_v5` and
  `coreiq_filing_metrics_v4` (§3).
