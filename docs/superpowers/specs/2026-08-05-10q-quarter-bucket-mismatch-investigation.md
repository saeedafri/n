# 10-Q quarter/date mismatch — root-cause investigation (2026-08-05)

**Symptom (reported):** on `/company_filings`, the header period/date does not match the
period printed inside the filing. Reproduced on FND, year 2026:

| Selection | Header | Document cover page | Verdict |
|---|---|---|---|
| 10-Q-Q1 | `2026 • Q1 • Apr 30, 2026 • Fiscal Period: Q1 FY2026` | quarterly period ended **March 26, 2026** | correct (header shows the *filing* date, not the period end) |
| 10-Q-Q2 | `2026 • Q2 • Jul 30, 2026 • Fiscal Period: Q2 FY2026` | quarterly period ended **March 26, 2026** | **wrong** — Q1's document |
| 10-Q-Q3 | `2026 • Q3 • Jul 30, 2026 • Fiscal Period: Q3 FY2026` | quarterly period ended **June 25, 2026** | **wrong** — Q3 does not exist yet; this is Q2's document |

This is not one bug. Three independent defects stack up.

---

## Where each piece of the screen comes from

- **Header** → `FilingMetricRepository.get_header_metadata()` → `coreiq_filing_metrics_v5`,
  keyed by `(ticker, COALESCE(fiscal_year, storage_year, report_fiscal_year), doc_type)`.
  It renders `filing_date` — the SEC **filed** date — never `period_end`.
  (`app/pages/company_filings.py:2067` `_render_filing_header_html`)
- **Document body** → Azure blob `{TICKER}/{YEAR}/{DOC_TYPE}/filing.html`, cached on local disk.
  (`app/pages/company_filings.py:3853-3900`)

The two sides are keyed by the same `doc_type` string but are produced by **different
ingestion writers**. When the writers disagree about which quarter a filing belongs to,
header and body disagree on screen.

---

## Root cause 1 (primary, data-side) — a writer buckets 10-Qs by the *calendar quarter of the filing date*

Evidence from the blob store, FND/2026:

```
10-Q-Q1/archive/000162828026028915.html   created 2026-05-06   (Q1 filing, filed Apr 30)
10-Q-Q2/archive/000162828026028915.html   created 2026-05-01   ← SAME Q1 filing, in the Q2 bucket
10-Q-Q2/archive/000162828026051069.html   created 2026-08-03   (Q2 filing, filed Jul 30)
10-Q-Q3/archive/000162828026051069.html   created 2026-07-31   ← SAME Q2 filing, in the Q3 bucket
```

`filing.meta.json` separates the two writers cleanly:

| bucket | accession | filing_date | fiscal_year_label | fiscal_quarter_slot | written |
|---|---|---|---|---|---|
| FND 10-Q-Q1 | …028915 | 2026-04-30 | 2026 | 1 | 2026-05-06 |
| FND 10-Q-Q2 | …051069 | 2026-07-30 | 2026 | 2 | 2026-08-03 |
| FND **10-Q-Q3** | …051069 | 2026-07-30 | **null** | **null** | 2026-07-31 |

Every mis-bucketed copy has `fiscal_year_label: null` **and** `fiscal_quarter_slot: null`.
The correct copies always carry both. So a second, older writer — one that has no DEI
`DocumentFiscalPeriodFocus` logic — is placing filings using
`quarter = ceil(filing_date.month / 3)`, i.e. the calendar quarter of the **filing** date.

US quarterly filers file ~30 days after quarter end, so this is **always off by exactly one
quarter**. Confirmed on every sample checked:

| ticker | fiscal quarter | filed | calendar Q of filing date | bucket it landed in |
|---|---|---|---|---|
| FND | Q1 | Apr 30 | Q2 | 10-Q-Q2 ✓ |
| FND | Q2 | Jul 30 | Q3 | 10-Q-Q3 ✓ |
| TGT | Q1 | May 29 | Q2 | 10-Q-Q2 ✓ |
| WMT | Q1 | May 29 | Q2 | 10-Q-Q2 ✓ |
| HD  | Q1 | May 27 | Q2 | 10-Q-Q2 ✓ |
| LULU| Q1 | Jun 4  | Q2 | 10-Q-Q2 ✓ |
| KO  | Q2 | Jul 29 | Q3 | 10-Q-Q3 ✓ |
| GOOGL| Q2 | Jul 23 | Q3 | 10-Q-Q3 ✓ |

The correct writer runs a few days later and fixes `filing.html` + `filing.meta.json` in the
right bucket — but **nothing ever deletes the wrong-bucket copy**, so the phantom quarter
survives and shows up in the Document Type dropdown.

**This writer is still live.** GOOGL's phantom Q3 was written 2026-08-04, KO's on 2026-08-03.

**The ingestion code is not in this repo.** No file here contains `fiscal_quarter_slot` or
`form_bucket`; the in-repo scripts (`scripts/fetch_filing_html.py`, `scripts/export_xbrl_facts.py`,
`scripts/azure_filing_uploader.py`) all use the correct DEI method (`determine_quarter()` →
`xbrl.entity_info['fiscal_period']`) and explicitly refuse a filing-date fallback. The offending
writer lives in the data team's pipeline.

### Blast radius (measured)

Blob scan of all 303 tickers that have 2026 10-Q metrics:

- **274 tickers (90%)** have at least one 10-Q sitting in the wrong quarter bucket for 2026.
- **295** Q1-filing-in-Q2-bucket occurrences, **122** Q2-filing-in-Q3-bucket occurrences.
- Earlier years show the same pattern (`ACI`, `AEO`, `ANF`, `BBY`, `DG`, … carry 2025 accessions
  duplicated across buckets too).

### The corruption arrived in two bulk waves, and the incremental writer still repeats it

Blob `creation_time` of every duplicated accession (FND, AAPL, TGT, WMT, HD, LULU, KO, GOOGL,
ANF, BBY, DG, ACI, AEO — 2023→2026) falls into three clean groups:

| wave | dates written | what it wrote |
|---|---|---|
| **A — bad historical backfill** | 2026-02-28, 03-01, 03-09, 04-14, 04-16 | every 10-Q, one quarter ahead, **including a `10-Q-Q4` folder** (Q3 filings land in "Q4" because they are filed in Oct–Dec) |
| **B — corrective DEI backfill** | 2026-05-06/07, 05-23, 06-20, 06-24 | the same accessions into the **correct** buckets — but never deleted wave A's copies |
| **C — ongoing incremental** | per filing, still today | FND 05-01→fixed 05-06 · FND 07-31→fixed 08-03 · KO 07-29→fixed 08-03 · GOOGL 07-23→fixed 08-04 · TGT 05-31→fixed 06-20 |

The `10-Q-Q4` folder is proof of the calendar-quarter formula: a 10-Q can only ever be Q1–Q3.
This app already knows about that junk — `app/utils/blob_doc_type_discovery.py:77` lists
`10-Q-Q4`, `10-Q-1`, `10-Q-2`, `10-Q-3` under `IGNORED_DOC_TYPES` — i.e. the wrong-bucket
writer's output was seen months ago and suppressed from the discovery report rather than
fixed upstream. `10-Q-Q4` is suppressed; `10-Q-Q3` is a *valid* name, so the phantom Q3
sails straight through into the dropdown.

ACI (Feb FYE) is scrambled worse — Q1↔Q3 crossings — because the filing-date formula drifts
by two quarters for late-February fiscal year ends.

## Root cause 2 (DB-side) — the same mis-bucketing leaked into `coreiq_filing_metrics_v5`

FND 2026 in the DB:

```
10-Q-Q1  acc 0001628280-26-028915  filed 2026-04-30  period_end 2026-03-26  519 rows
10-Q-Q2  acc 0001628280-26-051069  filed 2026-07-30  period_end 2026-06-25  716 rows
10-Q-Q3  acc 0001628280-26-051069  filed 2026-07-30  period_end 2026-06-25  716 rows   ← duplicate of Q2
```

The phantom Q3 is a byte-for-byte duplicate of Q2's metrics under a different `doc_type`.
That is why the Q3 header renders `Jul 30, 2026` and `Fiscal Period: Q3 FY2026`: the header
query trusts `doc_type`, and the row says Q3 while the facts are Q2's.

Consequences beyond this page: every consumer that reads v5 by `doc_type` (screening,
financial metrics, key developments) sees Q2 numbers reported twice — once as Q2, once as Q3.

Note the DB and the blob are corrected on **different schedules**: AAPL's blob still has the
stale Q1-in-Q2 copy while its DB rows are already clean; FND is dirty in both.

## Root cause 3 (app-side, in this repo) — the local filing cache never revalidates

`app/pages/company_filings.py:3873`:

```python
_disk_path = _local_cache_path_for_blob(blob_name)
if _disk_path and os.path.exists(_disk_path):
    html_path = _disk_path          # ← served purely on existence
else:
    html_path = _ensure_local_blob_optimized(blob_name, use_temp=False)
```

`_ensure_local_blob_optimized()` *does* have an ETag/last-modified freshness check
(`app/pages/company_filings.py:495-505`) — but the fast path above short-circuits before it
ever runs. Once a file is cached under a bucket path it is served forever.

FND's `10-Q-Q2/filing.html` was overwritten in Azure on **2026-08-03 18:54 UTC** (Q1 doc →
correct Q2 doc). Any host that had cached that path before then keeps serving the Q1 document
under the Q2 label — exactly the reported screenshot. The cache lives on the persistent
`/home` share on STG, so it survives restarts *and* deploys: this does not self-heal.

(Two further layers of the same staleness: `_load_and_process_html` is `@st.cache_data` keyed
on file path, and the `filings_data` prefetch is `@st.cache_data(ttl=…)`.)

## Root cause 4 (cosmetic, but it is what the user noticed first) — the header shows the filing date, not the period end

Even for a perfectly correct filing (FND Q1: header `Apr 30, 2026`, cover page
`March 26, 2026`) the header reads as a contradiction, because `_render_filing_header_html`
puts `filing_date` in the same chip row as the quarter and never surfaces `period_end`.
`period_end` is already available in `get_header_metadata()`'s SELECT — it just is not rendered.

---

## Summary of causes, in the order they bite

1. **Upstream pipeline** buckets 10-Qs by calendar quarter of the *filing* date → every filing
   is duplicated one quarter ahead. Wrong copies are never deleted. Still running today.
2. **`coreiq_filing_metrics_v5`** inherited the same wrong `doc_type`, so a phantom quarter has
   real header metadata and duplicated facts.
3. **This app** serves the cached blob on existence alone, so upstream corrections never reach
   users on a host that cached the bad file.
4. **Header** labels the filing date without the period end, which makes even correct filings
   look wrong.

---

## Is this caused by any commit on `stg-deploy`? No — five independent checks

Repo checked: `/Users/mohdsaeedafri/All-Code-Base/market-data-stg`, branch `stg-deploy`
(the branch running on `csr-awa-data-portal-stg`), plus this repo (`main`).

1. **The STG app cannot write these blobs.** `grep -r "upload_blob"` over the whole
   `stg-deploy` working tree returns **nothing**, and `scripts/` there contains only
   auth/forecast/playwright helpers — no filings uploader. The only four files mentioning
   `10-Q-Q` (`blob_doc_type_discovery.py`, `background_scanner.py`, `repository.py`,
   `company_filings.py`) are **read-only consumers**.
2. **The wrong copies' file layout does not exist in either repo, at any commit.**
   Bad buckets are written as `filing.html` + `filing.meta.json` + `archive/<accession>.html`
   + `versions/…`, with keys `form_bucket`, `fiscal_year_label`, `fiscal_quarter_slot`.
   `git log --all -S"fiscal_quarter_slot"` → no hits in either repo. This repo's own uploader
   (`scripts/azure_filing_uploader.py`) writes `metadata.json`, has no `archive/` or
   `versions/` concept, and derives the quarter from DEI only.
3. **It predates the code.** Wave A ran 2026-02-28 → 04-16 and corrupted 2023, 2024 and 2025
   filings. `CapIQReplacement`'s initial commit is 2026-06-11; the `stg-deploy` commits in
   question are all later still.
4. **The commits that coincide in time are unrelated.** 8b3bfcb (07-31), c38a545/52df8eb
   (08-01), 2ab2d8a (08-03) touch `forecast_refresh_service.py`, `repository.py`,
   `market_data.py`, `data/store_counts/**` and `scripts/*store_counts*`. On `stg-deploy`,
   8bc3fbdb / 17f1b2ab / 1190abe6 are store-count and logging work. None touch filings blob
   paths, `doc_type` assignment, or the filings uploader.
5. **The one page in this app that can write to blob storage cannot produce this.**
   `app/pages/company_filings_add_files.py` is a manual ADLS upload for admins — human-chosen
   path, human-named PDF, ACL-gated, audit-logged. It never emits the
   `filing.html`/`filing.meta.json`/`archive/` trio and has no quarter logic.

### Second pass over the full `market-data-stg` history (691 commits, from 2025-10-27)

The repo *did* once carry filings scripts (`azure_filing_uploader.py`, `fetch_filing_html.py`,
`export_xbrl_facts.py`, `run_pipeline.py`, …), all deleted in `c8ca433e` (2026-03-20,
"Removed Scripts"). They were checked at their historical revisions, and they still exonerate:

- `scripts/azure_filing_uploader.py` was **added 2026-03-06** (`b7a7e53e`) — *after* wave A's
  2026-02-28 / 03-01 writes. Its `determine_quarter()` is DEI-only, and it writes
  `{TICKER}/{YEAR}/{BUCKET}/metadata.json` — never `filing.meta.json`, `archive/` or `versions/`.
- `fetch_filing_html.py` uses the same DEI method and **raises** rather than falling back to a
  filing date.
- `run_pipeline.py` / `run_all_usa.py` (alive during wave A, deleted 03-06) only *iterate*
  already-fetched `10-Q-Q*` directories; they contain no Azure upload and no quarter derivation.
- `git log --all -S` for `fiscal_quarter_slot`, `form_bucket`, `filing.meta.json` → **no hits in
  any commit, by any author**. Only two commits in 691 ever contained the string `upload_blob`
  (`b7a7e53e` add, `c8ca433e` delete).
- Decisive: wave A also wrote on 2026-04-14/04-16 and wave C is **still writing today**
  (GOOGL 2026-08-04) — weeks and months after every filings script was deleted from the repo on
  2026-03-20. A deleted script cannot be writing blobs in August.

### Who to inform

**Shashank Gupta — `shashankgupta@coresight.com`.** He owns the filings ingestion and the
metrics table:

- created the repo (`7b469821`, 2025-10-27) and added the Azure Blob integration
  (`184e8390`, 2026-03-01);
- owns the filing-metrics table versions — `7d68fdd4` 2026-05-24 *"pushing a new table version
  of filing metrics"*, `1511451e` 2026-06-21 *"changing the company filings version from v4 to v5"*;
- his app-side commits line up with the pipeline's backfill dates almost exactly:
  `fceb6262` 05-06 *"fixing dates"*, 05-07 ×3 *"changing how latest document are sorted / fixing
  document sorting / reverting sorting changes"*, `f4d22169` 05-21 *"fixing a quick bug for date"*
  — versus blob wave-B writes on 05-06, 05-07, 05-23, 06-20/24.

Escalation/second contact if needed: **Philip Moore — `philipmoore@coresight.com`** (portal admin).

What to hand him: this document, the two detector queries at the end, and the three concrete
FND/KO/GOOGL examples — the ask is (a) drop the filing-date quarter fallback in the ingestion,
(b) delete the wrong-bucket blob copies and the phantom `doc_type` rows in
`coreiq_filing_metrics_v5`.

**Conclusion:** the mis-bucketing is written by the data team's ingestion pipeline, which lives
outside both repos. No commit of yours caused it. What *this* codebase owns is only
root cause 3 (cache never revalidates) and root cause 4 (header shows the filed date) — both
of which merely make the upstream corruption visible and sticky, they do not create it.

## Fix directions (not implemented — investigation only)

- **Upstream (data team, owner of the pipeline):** delete the filing-date quarter fallback; bucket
  strictly on DEI `DocumentFiscalPeriodFocus`, and treat a missing focus tag as a hard failure.
- **Backfill/cleanup:** for every `(ticker, year)` where one accession appears under 2+ 10-Q
  buckets, keep the bucket whose `filing.meta.json` has a non-null `fiscal_quarter_slot`, delete
  the other; then delete the matching phantom `doc_type` rows in v5. Detector query and blob scan
  used in this investigation reproduce the full list.
- **App-side (this repo, safe to do independently):**
  - revalidate the disk cache against blob ETag/last-modified instead of `os.path.exists()`;
  - render `period_end` in the filing header, and label the filed date as "Filed";
  - defensively hide a 10-Q bucket whose `period_end` duplicates another quarter's for the same
    ticker/year.

## Evidence commands

- v5 per-bucket accession/period: `SELECT doc_type, storage_year, filing_accession, MIN(filing_date), MAX(period_end), COUNT(*) FROM coreiq_filing_metrics_v5 WHERE ticker=… AND doc_type LIKE '10-Q%' GROUP BY 1,2,3`
- duplicate detector: same, grouped by `(ticker, filing_accession)` `HAVING COUNT(DISTINCT doc_type) > 1`
- blob scan: list `{TICKER}/{YEAR}/10-Q-*/archive/*` and flag any accession appearing under 2+ buckets.
