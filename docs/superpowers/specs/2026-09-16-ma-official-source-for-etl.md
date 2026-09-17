# Official, free source for M&A acquirer / target / deal value — for the ETL

**Date:** 2026-09-16
**Audience:** the data team who own `coreiq_company_events.ma_*`
**Claim:** SEC EDGAR gives acquirer, target and the exact deal value as
**structured fields** on several form types — no NLP, no vendor, no API key, no
cost. Every number below was measured live against EDGAR on 2026-09-16, not
quoted from documentation.

---

## 1. Why this matters

The current `ma_*` columns are produced by regex over filing text. Audited over
all 22,122 'M&A Activity' rows: ~25% of filled acquirer/target pairs name neither
party of the ticker's company, 17% of plain-dollar amounts were scaled wrong
(42 rows claim >$1 trillion), and the app now has to hide them. The fix is not a
better regex — it is to stop parsing prose and read the fields SEC already
publishes as data.

## 2. What is structured, and where

### 2.1 Parties — the submission SGML header

Every EDGAR submission starts with a header carrying **separate, labelled blocks**
per party, each with a CIK:

```
FILED BY:
    COMPANY DATA:
        COMPANY CONFORMED NAME:  DIANA SHIPPING INC.        <- acquirer / bidder
        CENTRAL INDEX KEY:       0001318885
SUBJECT COMPANY:
    COMPANY DATA:
        COMPANY CONFORMED NAME:  GENCO SHIPPING & TRADING LTD   <- target
        CENTRAL INDEX KEY:       0001326200
```

URL: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{accession}.txt`
(the header is the first ~4 KB; no need to download the whole submission).

**The CIK is the prize** — it is a stable company identifier, so the ETL can join
both parties to a company master instead of string-matching names, which is what
makes the current mis-attribution possible.

### 2.2 Deal value — the structured filing-fee exhibit

Since 2022 the SEC requires a machine-readable filing-fee exhibit
(`<TYPE>EX-FILING FEES`) on transaction forms. It carries the **Transaction
Valuation** — the official consideration the fee was computed on.

## 3. Measured hit rates (2026 Q2 filings, live)

| form | what it is | acquirer + target from header | deal value |
|---|---|---|---|
| **SC TO-T** | third-party tender offer | **14/14 = 100%** | **6/14 = 43%** |
| **425** | merger communication | **7/12 = 58%** | via linked S-4 |
| **S-4** | merger registration | co-registrant pairs 6/12 | fee exhibit |
| SC TO-I | issuer self-tender | 0/14 (same entity, by design) | 14/14 |
| SC 14D9 | target's response | same entity, by design | — |

Real extractions, zero text parsing:

```
GSK plc                  -> Nuvalent, Inc.               $10,628,606,249.75
ANV Group Holdings Ltd.  -> Open Lending Corp               $390,282,747.75
Camac Fund, LP           -> DESTINATION XL GROUP, INC.        $1,122,259.47
Fox Corp                 -> ROKU, INC                     (425, value via S-4)
DIANA SHIPPING INC.      -> GENCO SHIPPING & TRADING LTD   (SC TO-T + 425)
```

## 4. Recommended ETL change

1. **Ingest by form type, not by news.** Sweep `SC TO-T`, `425`, `S-4`, `DEFM14A`
   and `8-K Item 2.01` from the daily EDGAR index.
2. **Read the parties from the header**, storing `ma_acquirer_cik` /
   `ma_target_cik` **alongside** the names. Join on CIK downstream.
3. **Read the value from the `EX-FILING FEES` exhibit** where present; fall back
   to the cover-page "Transaction Valuation" line on pre-2022 filings.
4. **Exclude `SC TO-I` and `SC 14D9` as deals** — the former is an issuer buying
   its own shares, the latter is the target's response to someone else's offer.
   Both legitimately show one entity in both roles.
5. **Store the value in one unit and state it.** The present
   `ma_transaction_value_usd_m` is mis-scaled on 17% of plain-dollar rows; if the
   raw text is kept alongside, the app can (and now does) re-derive the number.

## 5. Tooling — already in this repo

`edgartools` is an existing dependency and `scripts/enrich_ma_events_v2.py`
already fetches filings with it. The pieces the ETL needs:

```python
from edgar import set_identity, get_filings
set_identity("Coresight Research <email>")          # SEC requires a UA identity
filings = get_filings(form="SC TO-T", year=2026, quarter=2)
```

The header and fee exhibit are plain HTTP against `www.sec.gov/Archives/...`.
SEC asks for a descriptive User-Agent and ~10 req/s; no key, no quota, no cost.

## 6. Honest limits

- **Tender offers are a minority of deals.** `SC TO-T`'s 100% party coverage
  applies only to that population. One-step mergers arrive as `425` / `S-4` /
  `DEFM14A`, where the party structure is less uniform (co-registrants rather
  than a `SUBJECT COMPANY` block).
- **Private and foreign deals are out of scope** — no SEC filing, no data. The
  current news-derived rows are the only signal there, and they stay as weak as
  they are today.
- **Value coverage is ~43% even on the best form**, because a fee exhibit is only
  required when a fee is due.
- This raises quality and attribution confidence a long way; it does not reach
  CapIQ-style completeness, which remains a licensed-data question.

---

# PART 2 — Built and measured (2026-09-16)

Two scripts were written and run end to end. **No DB writes** — output is a
read-only overlay, same pattern as the existing M&A overlays.

| script | what it does |
|---|---|
| `scripts/sec_ma_forms_sweep.py` | harvests `SC TO-T` + `425` submission headers (both parties + CIKs) and the fee-exhibit value. Header reads use an HTTP `Range` request for the first 12 KB, so a 10,000-filing sweep is tractable. |
| `scripts/sec_ma_forms_apply.py` | matches those deals onto `coreiq_company_events` **by CIK** (via SEC's official `company_tickers.json`, 10,422 symbols) within a date window, and writes `app/data/ma_event_overrides_secforms.json`. Fill-only. |

## 1. What the sweep produced (2025Q1 → 2026Q3)

| | |
|---|---|
| filings yielding two distinct parties | 6,859 |
| **distinct deals** (acquirer CIK × target CIK) | **426** |
| of which carry an official transaction value | 81 (19%) |
| distinct acquirers / targets | 374 / 406 |
| by form | 425: 340 · SC TO-T: 86 |

Largest, all from the structured fee exhibit — no text parsing:

```
$74,339.2M  Paramount Skydance Corp -> Warner Bros. Discovery, Inc.
$10,628.6M  GSK plc                 -> Nuvalent, Inc.
$ 9,570.2M  Sanofi                  -> Blueprint Medicines Corp
$ 9,250.3M  Merck & Co., Inc.       -> Cidara Therapeutics, Inc.
$ 8,013.1M  GENMAB A/S              -> Merus N.V.
$ 7,916.0M  QXO, Inc.               -> BEACON ROOFING SUPPLY INC
```

## 2. Effect as a PATCH on existing rows — small, and here is why

Measured over the 15,182 M&A events since 2025-01-01 (3,848 tickers):

| | before | after | delta |
|---|---|---|---|
| Acquirer | 8,995 (59.2%) | 9,207 (60.6%) | **+212** |
| Target | 9,634 (63.5%) | 9,803 (64.6%) | **+169** |
| Transaction Amount | 3,270 (21.5%) | 3,285 (21.6%) | **+15** |
| acquirer + target | 7,745 (51.0%) | 7,989 (52.6%) | +244 |
| all three | 2,567 (16.9%) | 2,616 (17.2%) | +49 |

**Still missing after the sweep: acquirer 39.4%, target 35.4%, amount 78.4%.**

Why the patch is small — from the matcher's own counters:

| | count | meaning |
|---|---|---|
| ticker resolved to a CIK | 12,876 | 85% of events |
| **no SEC deal for that company in window** | **10,615** | **82% of resolved events** |
| no deal within ±30 days | 1,301 | timing mismatch |
| no CIK for ticker | 2,306 | foreign / OTC symbols absent from SEC's map |
| matched a SEC deal | 960 | of which only 253 needed filling — the rest already had names |

The ceiling is structural: **most `M&A Activity` rows are news articles**, often
about a deal between two *other* companies, and only a fraction correspond to an
SEC-registered transaction by the ticker's own company. SEC cannot fill a row
that does not describe an SEC-registered deal.

## 3. The real conclusion — use SEC as a SOURCE, not a patch

426 distinct deals in 7 quarters, every one with:

- both parties named **by the filer itself**, not by a regex over prose;
- a **CIK for each party**, so downstream joins use a stable identifier — this is
  precisely what removes the ~25% wrong-company attribution in the current data;
- an official transaction value on 19%.

Compared with the same window in `coreiq_company_events`: ~60% names, of which
roughly a quarter name neither party of the ticker's company, and 21% amounts of
which 17% of plain-dollar figures were mis-scaled.

**Recommendation to the data team:** ingest these form types as first-class deal
records with their CIKs, then attach news rows to those deals — rather than
regex-ing deal fields out of news text and hoping the ticker is right. The sweep
script is re-runnable per quarter and costs nothing.

## 4. Overlay precedence

`utils.ma_overrides_auto.load_ma_overlays` now merges four files, weakest first:

```
runtime (daemon, deterministic)
  < edgar    (offline deterministic backfill)
  < secforms (SEC merger-form headers, CIK-matched)   <- new
  < repo     (LLM-verified gold, 262 events)
```

SEC-form data outranks anything extracted by us because the parties are declared
by the filer and matched on CIK.

---

# PART 3 — Free sources for M&A CLOSING, tested (2026-09-16)

Question: beyond `edgartools`, what free/official sources reliably identify that a
deal has **closed**? Four were tested against ground truth. Two are excellent, one
is redundant, one is unusable.

## Why this matters: `ma_deal_status` cannot answer it

Every SEC-backed 'M&A Activity' row was verified by opening its filing (5,775
filings, 6,372 rows scored). Truth = the 8-K carries Item 2.01, or its text
asserts completion in the past tense.

| rule | selects | correct | wrong | missed | precision | recall |
|---|---|---|---|---|---|---|
| `event_subtype='M&A Closing'` | 408 | 376 | 32 | 344 | **92.2%** | 52.2% |
| `ma_deal_status='closed'` | 2,036 | 594 | **1,442** | 126 | **29.2%** | 82.5% |
| `ma_is_closed=1` | 2,044 | 595 | 1,449 | 125 | 29.1% | 82.6% |
| status=closed AND NOT Item 1.01 | 948 | 402 | 546 | 318 | 42.4% | 55.8% |
| headline has Item 2.01 | 408 | 376 | 32 | 344 | 92.2% | 52.2% |

**Only 720 of 6,372 SEC-backed M&A rows are truly closed.** `ma_deal_status`
selects 2,036 of them and is wrong on 1,442 — it conflates *a definitive agreement
exists* with *the deal completed*, and most of its false positives are Item 1.01
credit agreements. `ma_is_closed` is the same signal (29.1% vs 29.2%).

## 1. SEC submissions API — 8-K item numbers as a DECLARED field ★

`https://data.sec.gov/submissions/CIK##########.json`

```
filings.recent = { accessionNumber[], filingDate[], form[], items[], ... }
   2026-07-30  8-K  items='2.02,9.01'
   2026-04-20  8-K  items='5.02'
```

`items` is published by SEC as structured data — **"was Item 2.01 filed?" needs no
filing download at all**, one JSON per company (older filings paginate via
`filings.files[]`).

**Validated:** 3,686 filings across 180 companies, compared against actually
opening each filing — **98.2% agreement**. The 60 disagreements where the API
declared 2.01 and the download parse missed it outnumber the 6 the other way, so
the declared field is at least as accurate as parsing, and far cheaper: this
verification opened 5,775 filings where ~1,400 JSON calls would have done.

## 2. Form 25-NSE / Form 15 — definitive post-close confirmation ★

A public target only **delists** (Form 25 / 25-NSE, filed by the exchange) and
**deregisters** (Form 15, 15-12B / 15-12G) once the takeover has completed. Both
appear in the same submissions JSON, with dates.

| target | delist / deregister | deal | verdict |
|---|---|---|---|
| Blueprint Medicines | 25-NSE 2025-07-18, 15-12G 2025-07-29 | Sanofi | closed ✓ |
| Beacon Roofing Supply | 25-NSE 2025-04-29, 15-12G 2025-05-09 | QXO | closed ✓ |
| Aspen Technology | 25-NSE 2025-03-12, 15-12G 2025-03-24 | Emerson | closed ✓ |
| Nuvalent | none | GSK (pending) | correctly NOT closed ✓ |

This is an *independent* confirmation of closure that does not depend on how the
acquirer worded its 8-K, and it dates the completion. An empty `exchanges[]` on
the company record is a corroborating hint.

## 3. EDGAR full-text search — free, no key

`https://efts.sec.gov/LATEST/search-index?q=...&forms=...` returns JSON hit counts
and accessions. Useful for discovery/backfill; not a closure signal by itself.

## 4. GLEIF (LEI) — tested and REJECTED

Free, official, global, and it does model parent/child ownership. But it lags
badly: **Blueprint Medicines still reports `entity.status = ACTIVE` months after
Sanofi acquired it**, and `filter[entity.status]=MERGED` is not even a queryable
value (HTTP 400). Not usable for deal closure.

## Recommendation for the ETL

Derive closure from SEC structure, never from prose:

1. **`items` contains `2.01`** on the company's 8-K → closed (92% precision today,
   and the item list is a declared field, so this is essentially free).
2. **Target files Form 25 / Form 15** → closed, with a date — use it to confirm
   and to catch deals whose 8-K wording the parser missed.
3. Keep `ma_deal_status` only as a weak hint, or redefine it from (1)+(2).
4. Drop `ma_is_closed` — it duplicates `ma_deal_status`.

Recall is the remaining gap: rule (1) finds 52% of truly-closed rows because many
completions are described in prose without an Item 2.01 tag. Adding (2), plus the
past-tense completion phrases this verification script uses, is what closes it.

---

# PART 4 — Closure signal integrated into MDP (2026-09-17)

## 1. Architecture: offline sweep, local read

```
scripts/sec_ticker_cik_map.py   ticker -> CIK  (bulk file + EDGAR lookup fallback)
        |                                         -> app/data/sec_ticker_cik.json
scripts/sec_closure_index.py    per company: every 8-K with a DECLARED Item 2.01,
        |                       plus Form 25 / Form 15 delist-deregister filings
        |                                         -> app/data/sec_closure_index.json
utils/ma_overrides_auto.load_sec_closure_index()  mtime-cached local read
utils/ma_overrides_auto.sec_closure_for_event()   ticker + date -> provenance label
screening_service._ma_display_fields()            -> "Deal Status" column
```

**No filing is ever downloaded** — `items` is a declared field on
`data.sec.gov/submissions`. **The app never calls SEC at render time.**

## 2. Speed — measured

| | |
|---|---|
| index on disk | 860 KB, 2,578 tickers |
| first parse | 5 ms (once per process, mtime-cached) |
| warm lookup | 2.9 µs/call |
| labelling ALL 22,122 events | 277 ms (12.5 µs/row) |
| a real 500-row page | ~6 ms |
| **UI: grid after Show Results** | **1.6 s warm** — identical to before the column |

A 42.3 s first run was cold start after a process restart (Streamlit boot + warmup
+ cold DB pool); the warm re-run returned to 1.6 s.

## 3. Company reach

| | tickers | |
|---|---|---|
| with M&A events | 3,871 | |
| resolved to an SEC CIK | **2,578** | 66.6% |
| no SEC CIK | 1,293 | 33.4% (~12% of event volume) |

SEC's static `company_tickers.json` carries only 10,422 symbols and **omits major
US filers** — AvalonBay, Equity Residential, Denny's, Sleep Number all return
`None` from it and from `company_tickers_exchange.json`. EDGAR's own company
lookup resolves them, which is why `sec_ticker_cik_map.py` falls back to it:
2,211 -> 2,578 tickers (+367). The remaining 1,293 are genuinely non-SEC.

## 4. Event coverage and value

| | events | share |
|---|---|---|
| SEC 8-K Item 2.01 | 1,597 | 7.2% |
| SEC delisting (Form 25/15) | 251 | 1.1% |
| **SEC-confirmed closed** | **1,848** | **8.4%** (421 tickers) |

Against the DB column:

| | events |
|---|---|
| SEC-confirmed AND `ma_deal_status='closed'` | 968 |
| **SEC-confirmed, the column MISSED it** | **880** |
| `status='closed'` with NO SEC evidence | **3,601** |

8.4% is not a shortfall: filing-level verification found only ~720 genuinely
closed among SEC-backed rows. Most M&A events are announcements or news, not
closings. The column's extra 3,601 are the false positives behind its 29.2%
precision.

## 5. Display rule

`Deal Status` renders `Closed (SEC 8-K Item 2.01)` or `Closed (SEC delisting)`,
and `—` when SEC has no evidence — **absence is never rendered as "not closed"**.
A ±21-day window ties the filing to the event so an old completion cannot label an
unrelated new row.

## 6. Verification

- 15/15 category scenarios pass with four M&A columns (paginated, Excel export,
  by-industry); zero columns on non-M&A screens.
- Real UI: 12 headers in order — … Summary · Acquirer · Target · Transaction
  Amount · **Deal Status** · Key Development Sources · Source Reference.
- `python app/utils/ma_8k_extract.py` — gate self-check + 479/480 gold names.

## 7. Refresh

```bash
.venv/bin/python scripts/sec_ticker_cik_map.py    # cached; only new tickers cost anything
.venv/bin/python scripts/sec_closure_index.py     # resumes; re-fetches only new companies
```
Both are free, keyless and re-runnable per quarter. Nothing is written to the DB.

## 8. Not covered

The 1,293 non-SEC tickers (~12% of events) need per-jurisdiction sources — Japan
EDINET (free official API), Canada SEDAR+, EU DG COMP merger register, UK
Companies House. Each has its own identifiers and no ticker mapping, so this is a
scoped project, not an add-on. GLEIF was tested and rejected (Part 3).
