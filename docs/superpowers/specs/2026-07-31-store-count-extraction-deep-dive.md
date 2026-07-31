# Store Counts — deep dive: where we actually lack, and the pipeline that fixes it

Date: 2026-07-31 · Analysis only, no code changed · Evidence: STG DB + Azure blob `csmarketdata/azure-storage-test`

---

## 1. What we own (and are not using)

Full blob inventory (499,482 objects scanned):

| Asset | Held | Ever mined for store counts |
|---|---|---|
| 10-K filings (`{TICKER}/{year}/10-K/filing.html`) | **4,816** | 659 (**14%**) |
| Tickers with 10-Ks | **504** | 123 (**24%**) |
| Storage years | 2015–2026 | store data mostly FY2019+ |
| 10-Qs | 13,849 | 0 |
| 8-Ks / 20-F / 6-K | 787 | 0 |

Portal companies: 379. 10-K coverage per year is essentially complete from 2016
(368) through 2025 (489); 2026 is at 402 and still filling.

Every `store_count` row already carries `archive_blob` — a direct pointer to the
filing it came from. `coreiq_filing_metrics_v5` also has `filing_accession`,
`storage_year`, and unused `start_position`/`end_position` columns.

**We have the primary sources on disk. The pipeline read them once, kept one
sentence, and discarded the document.**

---

## 2. The core defect

`llm_query.source_sentence` is the ONLY evidence retained per extracted value,
and the app validates the number against that single sentence
(`_store_row_passes_source_check`). When the number came from a table and the
stored sentence is the table's caption, validation fails on a correct value.

Measured across all 659 rows, checking each value against **its own filing**:

| | Rows | Value literally present in its filing |
|---|---|---|
| Gate **passed** | 596 | **596 — 100%** |
| Gate **failed** | 63 | 28 present · **35 absent** |
| Unreadable blob | 1 | — |

Two conclusions, both important:

1. **The strict gate has perfect precision.** It has never displayed a value
   that is absent from the filing. It is not the problem — it is the only thing
   protecting the tab.
2. **The gate's 63 rejections split 28 / 35.** 28 are correct values it cannot
   verify because the wrong sentence was stored (PLCE, SHAK, AZO, RH…). 35 are
   genuinely not in the filing at all (WMT FY2020/22/23, all five TJX, all four
   SHW, LEVI, KSS, LULU, O, RCKY 10,000 …) — LLM fabrications or mis-reads.

### 2a. This invalidates the corroboration fallback shipped earlier today

The 15% trend-corroboration fallback recovered 28 year-values. Checked against
the filings, **10 of them are provably absent from the source document**:

| Ticker | FY | Shown by fallback | Filing actually says |
|---|---|---|---|
| WMT | 2020 | 11,471 | `Total retail units … 11,501` |
| WMT | 2022 | 10,493 | `Total retail units … 10,593` |
| WMT | 2023 | 10,325 | `Total retail units … 10,623` |
| SHW | 2020/21/23/24 | 4,995 / 5,081 / 5,012 / 5,107 | 4,774 / 4,859 / 4,694 / 4,773 |
| TJX | 2022 | 3,680 | table total 3,380 |
| LEVI | 2023 | 1,165 | absent |
| UPBD | 2022 | 573 | absent |

A 36% error rate on recovered values. **Recommendation: revert that fallback**
(`_corroborated_store_rows` + `_SC_CORROBORATION_TOLERANCE`) and solve the same
problem properly by re-verifying against the filing. The label-drift merge and
the XBRL per-year gap-fill are unaffected and should stay — neither invents a
value.

---

## 3. Where we lack — four distinct gaps

| # | Gap | Size | Fix belongs to |
|---|---|---|---|
| G1 | Wrong evidence stored → correct values hidden | 28 values, ~10 tickers (PLCE all 7 years, SHAK, RH, AZO…) | re-extract + verify against filing |
| G2 | Bad values in DB | 35 values incl. all of TJX and SHW | verify against filing → drop |
| G3 | Extraction never ran on most filings | 4,157 of 4,816 10-Ks (86%); 381 tickers have zero store data | run the pipeline over the archive |
| G4 | No history before FY2019 | 2016–2018 10-Ks held (1,140 filings) but unmined | same run, backfill |

G3 is the big one: 225+ portal companies show an empty Additional Data tab today
while their 10-K sits in the container.

---

## 4. Why this is harder than a regex (measured, not assumed)

A deliberately naive two-rule extractor (table row matching
`Total <unit>`, plus `we operated N <unit>` prose) scored **22/45 = 49%** against
known-good values. Representative failures:

- **TGT FY2020** — truth 1,868 lives in a row labelled only `Total | 1,868 | 240,516`;
  the word "stores" is in the column header, not the row label.
- **CAL FY2021** — truth 1,086; candidates 13 / 50 / 69 / 916 (segment splits).
- **KSS FY2021** — truth 1,174; extractor found 1,162 (a different year's column).
- **BNED, KR, UAA, YETI** — no candidate at all; the count is in a properties
  table with an entirely different shape.

So: pattern matching alone is not the answer, which is precisely why an LLM is
in the pipeline. The fix is not "drop the LLM" — it is **give the LLM the whole
table as evidence and keep that evidence**, then verify mechanically.

---

## 5. Proposed pipeline — three sources, one verifier, permanent cache

### 5.1 Source precedence, per (ticker, fiscal year)

| Rank | Source | Why | Confidence |
|---|---|---|---|
| S1 | **iXBRL facts** — `us-gaap:NumberOfStores` & siblings, from the filing's own XBRL | issuer-tagged, exact, machine-readable, no parsing | highest |
| S2 | **Filing table extraction** — locate the store table (row label OR column header), anchor on the FYE date, take the total column | covers TGT/CAL/KSS shapes that S1 misses | high |
| S3 | **LLM over the located section** — only when S1 and S2 miss, and the model is handed the FULL section text | catches prose-only disclosures | medium |

Today's pipeline is effectively S3-only, with a single sentence retained.

### 5.2 The verifier (the part that actually matters)

Every candidate, whatever its source, must reproduce from the **stored evidence
block** — the full table or section, not one sentence. Store alongside the value:

```
evidence_text     the table/section as extracted (a few KB)
evidence_offsets  start/end into filing.html   (columns already exist, unused)
source_rule       S1_xbrl | S2_table | S3_llm
fye_date          the as-of date the figure is stated for
```

This makes the app-side check trivial and exact, and kills both failure modes at
once: G1 (correct value, unverifiable) and G2 (fabricated value passes review).
It also means the accuracy gate in `repository.py` can stay strict forever —
no tolerance heuristics, no trend guessing.

### 5.3 Cross-source agreement → confidence, not silence

When two sources agree within 1%, mark `high` and display. When they conflict,
prefer S1 > S2 > S3 and record the conflict rather than hiding the year. Today
a conflict silently becomes `-`.

### 5.4 Persistent cache on `/home` (your point about data never changing)

A filed 10-K is immutable. Key the cache by **accession number**, never by date:

```
/home/filing_extract/store_counts/{ACCESSION}.json
  { ticker, cik, accession, fye_date, fiscal_year,
    value, source_rule, evidence_text, evidence_offsets,
    extracted_at, extractor_version }
```

- `/home` is the persistent Azure share — **500 GB, ~499 GB free**, survives
  restart AND deploy. 4,816 JSON files at ~4 KB ≈ 20 MB. Nothing.
- Immutable by construction: a given accession's extraction never needs to
  re-run. Only `extractor_version` bumps force a re-run.
- Requires `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` (already needed for
  `EDGAR_CACHE_DIR`, currently unset on STG — that is a separate standing issue).
- Same JSON is what a batch job would write into the DB, so the cache doubles as
  the ingestion staging area. No divergence between "what the app sees" and
  "what the data team loaded".

### 5.5 New company onboarding — the same path, no special case

```
new ticker
  → list its blobs {TICKER}/*/10-K/filing.html   (already how the archive is laid out)
  → for each accession not in /home cache:  S1 → S2 → S3 → verify → cache
  → serve from cache; DB ingestion picks the same JSON up later
```

First render for a brand-new company costs one pass over its filings
(~1–2s per filing, ~12 filings) and is permanent thereafter. Everything after
that is a disk read.

---

## 5.6 LLM cost — measured, not estimated

Measured on 25 real filings from our own container with `tiktoken` (`o200k_base`):

| | Tokens per filing |
|---|---|
| Whole filing text | median **106,492** · mean 106,247 · max 184,464 |
| Top-3 keyword windows (4,000 chars each, the `llm_extractor.py` pattern) | median **1,381** · mean 1,449 · max 2,730 |

**A 73× reduction.** (The max also matters: a 184k-token filing does not fit
gpt-4o-mini's 128k context at all, so whole-filing prompting is not merely
expensive, it is impossible for the largest filings.)

Rates used — OpenAI list price for `gpt-4o-mini`, **$0.15 / 1M input,
$0.60 / 1M output**, Batch API −50%. Verify current rates before committing spend.
Per call: ~1,700 input tokens (window + instructions) + ~120 output.

### 1,000 companies × 11 years = 11,000 filings

| Strategy | One-off cost |
|---|---|
| A. Whole filing, every filing | **$176.10** |
| B. Windowed, every filing | $3.60 |
| C. Windowed + Batch API | $1.80 |
| D. + deterministic sources first (LLM on ~40% residue) | **$0.72** |
| E. D + a second verification call per value | $1.44 |

15,000 filings (15 years): $240 → **$0.98**. Our current 4,816 filings: $77 → **$0.31**.

Unit economics: **$0.33 per 1,000 filings**. Steady state after backfill — 1,000
new 10-Ks a year — is **$0.16/year**.

### Cost levers, in order of impact

1. **Never send the whole filing** — window on unit keywords. 73×, the single
   biggest lever, and it is already how `llm_extractor.py` works (`[:4000]`).
2. **Deterministic sources first.** S1 (iXBRL) already resolves 27 of our 123
   mined tickers with zero LLM. S2 (table parse) takes more. Only the residue
   pays. *(The 40% residue figure is an assumption — it should be measured by
   running S1+S2 offline before any spend.)*
3. **Batch API, −50%.** Backfill is not latency-sensitive; 24h turnaround is fine.
4. **Accession-keyed `/home` cache → the cost is paid once, ever.** A filed 10-K
   is immutable. This is what turns a recurring bill into a one-off.
5. **Pre-filter non-retail filings to zero LLM calls.** No candidate window
   containing a unit keyword + a total cue + a number ⇒ never call the model.
   Most of the 381 tickers with no store concept cost nothing.
6. **Cap `max_tokens`** at 128–256 with `response_format=json_object`
   (already done today).

### What would actually be expensive

- Whole-filing prompts: $176 instead of $0.72 — 244×.
- Calling the LLM at page-render time instead of from a cache: 1,000 users ×
  1 view/day × 11 years ≈ the entire backfill cost **every day**.
- A per-metric call per filing (the company-filings extractor's pattern) rather
  than one call returning the store count for the whole filing.

## 6. Cost / effort

| Item | Estimate |
|---|---|
| One-off backfill over 4,816 10-Ks | ~2–4 h wall clock at 8 workers; S3 LLM only for the residue |
| LLM spend | S1+S2 should cover the majority; budget ~1,500 GPT-4o-mini calls for the rest |
| Storage | ~20 MB on `/home` |
| App-side change | Read cache → merge with existing sources; the strict gate stays |
| Risk | Additive — a cached value only ever fills a year that is blank or replaces one that fails verification |

---

## 7. Recommended order

1. **Revert the corroboration fallback** — it is displaying 10 wrong numbers now.
   (Keep the label-drift merge and the XBRL per-year gap-fill.)
2. Build the extractor + verifier as an offline script; run it over the 123
   tickers we can score against, and report precision/recall before anything
   touches the app.
3. Backfill all 4,816 filings into `/home`, ship the app-side read.
4. Hand the same JSON to the data team for `coreiq_filing_metrics_v5` ingestion
   so the DB and the cache agree.

Open question for the business: when S1/S2/S3 all fail for a year, do we keep
showing `-`, or show the value with a "unverified" marker? Current behaviour and
my recommendation is `-`.
