# Screening → M&A Deal Card (Company / Acquirer / Target / Amount) — Investigation & Design

**Date:** 2026-09-16
**Status:** Investigation complete. No code changed. Build plan below awaits go-ahead.
**Ask:** business wants, in Key Developments screening, an M&A result card showing
**company name, acquirer, target, transaction amount**.
**Verdict:** buildable — but only behind a quality gate, and **not** from the
`ma_*` DB columns as they stand today. Numbers below are measured, not estimated.

---

## 1. Where the data already lives

`coreiq_company_events` (STG) already carries every field the card needs — no
schema change required:

```
ma_acquirer  ma_target  ma_transaction_value (text)  ma_transaction_value_usd_m (dec)
ma_deal_type ma_deal_status ma_is_closed ma_announce_date ma_close_date
ma_extraction_confidence
```

**22,122** active `event_category='M&A Activity'` rows, 2016-01-04 → 2026-09-14.

| Field | Filled | % |
|---|---|---|
| `ma_deal_type` | 20,271 | 92% |
| `ma_target` | 14,694 | 66% |
| `ma_acquirer` | 14,525 | 66% |
| `ma_transaction_value_usd_m` | 7,414 | **34%** |

Last 12 months (3,817 tickers, 14,261 events): 8,651 rows (61%) have
acquirer+target; 3,382 (24%) also have an amount.

Company name is already joined into the Key Devs grid (`_KEYDEV_COMPANY_COLS`),
so that quarter of the ask is free.

**Origin of the values:** a **server-side nightly enrichment job that is not in
this repo** regex-scrapes 8-K/news text into these columns. Nothing in this
repo writes them except `scripts/enrich_ma_events_v2.py apply` (data-team only).

---

## 2. Is the data correct? No — measured failure modes

### 2.1 Hand audit, 25 random 2026 rows that had BOTH names filled
~60% acquirer/target pairs correct. **8 of 25 (32%) are attached to the wrong
company**: the `GLW` row carries an AMETEK/Indicor deal, `NUE` carries
Worthington/Kloeckner, `NXPI` carries ICE/MarketAxess, `UBER` carries
Booking/Lola, `INTC` carries Jabil, `COR` carries
*"EVP acquires stock through Employee Stock Purchase Plan"* (not a deal at all).

### 2.2 Attribution check across all 22,122 rows
Neither the acquirer nor the target name overlaps the ticker's own company name
in **21% of EDGAR-sourced** and **26% of news-sourced** filled pairs. That is an
upper bound — some are legitimate subsidiary names (CPNG → "Surpique
Acquisition Limited", HLN → ChapStick divestiture) — but it brackets the
mis-attribution rate at roughly one row in four.

### 2.3 Transaction amount is the worst field — three independent defects

1. **Scale is inconsistent, provably.** Of 821 rows whose text is a plain `$N`
   figure, **142 (17%) were stored un-scaled** — off by 1,000,000×. Identical
   text formats convert differently: `TDUP $22,500,000 → 22.50` (right) vs
   `PAG $12,340,000 → 12340000.00` (i.e. $12.34 **trillion**).
   **42 rows currently claim a deal value above $1 trillion.**
2. **Per-share prices stored as deal value.** `TMHC "$72.50 per share" → 72.50`
   renders as a $72.5M deal for a ~$10B transaction; `GFI "C$4.15 per share" → 4.15`.
   **1,319 of 7,414 value rows (18%)** carry per-share, foreign-currency, or
   unscaled text.
3. **31% of value rows aren't deal values at all.** 2,332 of 7,414 come from
   `event_subtype='Material Agreement'` (8-K Item 1.01) — revolvers and note
   issuances: `MU $1,000,000,000`, `ADBE $1,000,000,000`, `TPR $500,000,000`.
4. **The DB contradicts itself:** 305 distinct acquirer/target pairs carry
   conflicting amounts across rows for the same deal.

### 2.4 The upstream job is still broken today
Live EDGAR re-fetch of recent rows (2026-08/09) still shows raw regex garbage
being written: `KPLT` acquirer = *"Financial Statements of Businesses or Funds"*,
`BBBY` acquirer = *"Acquisition or Disposition of Assets. As discussed in Item
1.01 of this Current Report…"*. This is the exact defect the
[2026-07-15 calendar M&A repair spec](2026-07-15-calendar-ma-acquirer-target-repair-design.md)
documented; the app-side overlay hid it on the calendar, the upstream job was
never fixed.

### 2.5 Why the calendar looked right
It isn't reading these columns raw. `repository.get_ma_completion_events` merges
`app/data/ma_event_overrides.json` — **262 LLM-extracted, spot-verified events**
(acquirer 235/262 = 90%, target 245 = 94%, amount 152 = 58%) — over the DB row,
plus a runtime overlay from `utils/ma_overrides_auto.py`, plus a display
sanitizer that renders "—" instead of garbage. The calendar is a curated 262-row
window, not proof that 22,122 rows are clean.

---

## 3. Can a better source be had? Tested, with results

| Source | Tested | Result |
|---|---|---|
| **SEC 8-K via edgartools** | re-fetched 10 recent rows | **10/10 fetched OK** — pipeline alive. But only **6,390 of 22,122 rows (29%)** carry a sec.gov URL; the other 71% are news links with no authoritative source anywhere in the stack. |
| **Deterministic extractor** (`utils/ma_8k_extract.py`) | ran on those 10 | name pair on **2/10**. Documented: 98% precision, **27-40% recall**. Amount is *deliberately never emitted* (peaked at 88% precision — a wrong dollar figure was judged worse than "—"). |
| **LLM extraction over 8-K item text** | the 262-row overlay is exactly this | **best result available**: 90%/94%/58% names/target/amount, verified against source filings. Needs a live LLM key. |
| **SEC XBRL** (`companyconcept` API) | queried 6 CIKs live | **rejected as the amount source.** `PaymentsToAcquireBusinessesNetOfCashAcquired` is a *period cash-flow aggregate* (AMZN Q2-2026 = $41.96B across all deals, not one deal); `BusinessCombinationConsiderationTransferred1` is sparse and dimensional (COCO: 0 facts). Useful only as a cross-check on large single-deal quarters. |
| **News feeds** (AV / YF sentiment tables) | schema reviewed | carry title/summary text only — no structured deal fields. They are the *origin* of the 71%, not a fix for it. |

**Honest ceiling:** CapIQ-grade coverage (acquirer + target + amount on every
deal) is **not obtainable** from the current stack. That is a licensed M&A feed
(CapIQ / Refinitiv / Mergermarket). What *is* obtainable is a high-precision
subset, sized in §4.

---

## 4. The quality gate, and exactly what survives it

One shared display gate. **Names** → existing `ma_8k_extract.sanitize_ma_name`
(already the single shared validator). **Amount** → dropped when any of:

- `event_subtype = 'Material Agreement'` (credit facility, not consideration)
- text matches `per share | each | /share | per unit`
- text carries a non-USD symbol (`€ £ ¥ C$ A$ HK$ …`)
- text is a plain `$N` with no `million`/`billion` scale word (17% mis-scaled)
- `usd_m` outside `[0.1, 1_000_000]` (kills the 42 >$1T rows)

Measured survival, last 12 months (14,261 events):

| | raw | after gate |
|---|---|---|
| rows with acquirer + target | 8,651 | **8,413** (2,244 tickers) |
| rows with acquirer + target + amount | 3,382 | **2,794** (970 tickers) |

So the card shows names on ~59% of M&A events and a trustworthy amount on ~20%.
Everything else renders "—" rather than a wrong number — the same precision-over-
recall rule the calendar already follows.

---

## 5. Build plan

### Tier 1 — ship the card (small, no new dependency)
1. Add `ma_acquirer, ma_target, ma_transaction_value, ma_transaction_value_usd_m,
   ma_deal_type` to the three Key Devs SELECTs in `app/data/screening_service.py`
   (`get_keydevs_events_for_tickers`, `get_keydevs_events_by_industry`,
   `fetch_all_keydevs_events`) — they select no `ma_*` column today.
2. One gate function (§4) in `app/utils/ma_8k_extract.py` next to
   `sanitize_ma_name`, imported by page + Excel export so the grid and the
   workbook can never disagree.
3. Render the card in `app/pages/screening.py` for `M&A Activity` rows:
   Company · Acquirer · Target · Amount · Deal type · Date, with a source chip
   (SEC filing vs news) so the reader can weigh it.
4. Merge `ma_event_overrides.json` here too (`repository._load_ma_overrides` is
   already loaded for the calendar) — the 262 gold rows should display correctly
   in screening as well.
5. Excel export carries the same gated columns.

### Tier 2 — raise coverage on the 29% that have a filing
Extend the existing `scripts/enrich_ma_events_v2.py` harvest → LLM → emit-overrides
run from 262 calendar rows to all **6,390** EDGAR-sourced M&A rows. Same
validated overlay file, **no DB writes** (owner rule stands). Expected lift, by
the 262-row precedent: names to ~90%, amount to ~58% on that 29% slice.
**Blocking dependency:** a live LLM key. The 2026-07-15 spec records the repo's
OpenAI/Gemini/Moonshot keys as dead/leaked; I could not re-test them (reading
credentials is blocked in this session). Needs the user's call.

### Tier 3 — root cause, data team
The nightly enrichment job must stop writing regex output. Hand over: this
document, the 142 un-scaled rows, the 1,319 per-share/currency rows, the 2,332
Material-Agreement contaminated values, the 42 >$1T rows, and
`scripts/enrich_ma_events_v2.py apply --dry-run`. Until they fix it, Tier 1's
gate is what stands between the business team and a $12-trillion deal value on
screen.

---

## 6. Testing plan

- **DB probes** (done, this document): fill rates, per-source/subtype breakdown,
  scale-consistency check, per-share detection, duplicate-deal conflicts.
- **Live source test** (done): 10 EDGAR re-fetches + extractor comparison;
  6 CIKs against the XBRL companyconcept API.
- **Gate unit check**: assert-based self-check over the known-bad corpus —
  `PAG $12,340,000`, `TMHC $72.50 per share`, `GFI C$4.15 per share`,
  `MU $1,000,000,000` (Material Agreement) all rejected; `ADI $1.5 billion`,
  `AWK $315 million`, `BIIB $5.6 billion` all kept.
- **Real UI** (`bash .claude/dev/run_local.sh` → `http://localhost:8501/screening`):
  Key Devs mode → Key Developments by Category → M&A Activity → Show Results;
  screenshot the card; confirm gated rows render "—" and never a wrong figure.
  Page and criterion panel verified working during this investigation
  (universe: 4,498 companies).

---

## 7. Open questions for the user

1. Is a card that shows names on ~59% and an amount on ~20% of M&A events
   acceptable to the business, or is complete coverage the requirement (→ licensed feed)?
2. Tier 2 needs a working LLM key. Which one do we use?
3. Who owns filing the upstream-job defect with the data team?

---

# PART 2 — Implementation (built 2026-09-16)

Constraint from the owner: **zero extra cost.** No LLM key, no paid feed — only
what the repo already has (the DB, the checked-in overlay, and the free
`edgartools`/SEC path). Tier 2's LLM pass from Part 1 §5 is therefore NOT built;
the free deterministic equivalent is, and §2.4 below measures what it is worth.

## 1. What changed

| File | Change |
|---|---|
| `app/utils/ma_8k_extract.py` | `ma_amount_is_trustworthy()` / `format_ma_amount()` — the amount gate. `sanitize_ma_name()` tightened with `_reads_as_prose()`. `_self_check()` runnable via `python app/utils/ma_8k_extract.py`. |
| `app/utils/ma_overrides_auto.py` | `load_ma_overlays()` — ONE reader for all three overlays (runtime < edgar backfill < repo gold), mtime-cached. `data_overlay_path()`, `_read_overlay()`. |
| `app/data/repository.py` | `_load_ma_overrides()` now delegates to `load_ma_overlays()`; the two duplicate class-level caches deleted. Calendar behaviour unchanged. |
| `app/data/screening_service.py` | `_keydev_ma_select()` adds the `ma_*` columns to the three Key Devs queries **only when 'M&A Activity' is selected**; `_keydevs_records_from_rows(rows, with_ma)` emits Acquirer / Target / Transaction Amount; `_ma_display_fields()` does the overlay-then-gate merge. |
| `scripts/enrich_ma_events_v2.py` | `backfill-edgar` subcommand — free, resumable, no LLM. |
| `app/data/ma_event_overrides_edgar.json` | NEW, generated: 21 recovered deals. |

## 2. Decisions, each with its measurement

### 2.1 Columns appear only on an M&A screen
`_keydev_ma_select(categories)` returns the extra SELECT list only when
`'M&A Activity'` is among the chosen categories, and `with_ma` gates the display
keys. Verified against STG: an M&A screen returns 12 columns including
Acquirer/Target/Transaction Amount; a Management-Changes screen returns the
original 9 and no M&A column leaks in.

### 2.2 Speed — the columns are free
They are more columns on rows already being fetched, no extra join and no extra
rows (this DB is packet-per-row bound). Measured on STG, 500 rows, warm pool:

| query | rows | time |
|---|---|---|
| without the M&A columns | 500 | 2,622 ms / 2,575 ms |
| with the M&A columns | 500 | 4,270 ms (first, cold TLS) / **2,317 ms** |

Within noise once warm. The overlay merge is a dict lookup per row over a
mtime-cached JSON (268 entries), and `load_ma_overlays()` costs one `os.stat`
per file per render, not a JSON parse.

### 2.3 Materialization: pickle.gz stays, parquet rejected
`utils/materialize.py` writes **pickle + gzip level-1**, not parquet. Measured on
the largest cache (`calendar_events_full`, 20,946 rows × 15 cols):

| format | read | write | size |
|---|---|---|---|
| pickle.gz (current) | 9.2 ms | 20.6 ms | 403,241 B |
| pickle raw | 8.5 ms | — | 2,269,362 B |
| parquet (snappy) | **5.2 ms** | 22.0 ms | 381,900 B |

Parquet is ~4 ms faster to read. One STG round-trip is ~250 ms, so the format is
0.1% of a page's cost — switching buys nothing and costs a pyarrow dependency on
every cache reader. **Not changed.** Faster *updates* are a freshness-signal
question, not a format question (see the CRC32 note in the materialization
memory); the Key Devs grid is not materialized at all — it reads the DB directly
with keyset pagination.

### 2.4 The free EDGAR backfill is real but small — and here is why
`scripts/enrich_ma_events_v2.py backfill-edgar` re-fetches the source filing with
`edgartools` (SEC is free) and runs the precision-tested template extractor.

First run targeted every EDGAR-sourced M&A row missing a name — **4,156 rows** —
and the first 40 returned **zero** names. Cause, from the filings themselves:

| subtype of the 4,156 rows missing names | count | why nothing can be extracted |
|---|---|---|
| Material Agreement | 2,707 | 8-K Item 1.01 credit-facility amendments — no buyer, no seller |
| M&A News (6-K) | 840 | foreign private issuer cover reports |
| M&A Cancellation | 462 | Item 1.02 terminations |
| **M&A Closing** | **124** | the genuinely extractable slice |
| Change in Control / Deal News | 23 | |

So the subcommand now restricts itself to the four deal-bearing subtypes
(`BACKFILL_SUBTYPES`) — otherwise it is 4,000 SEC round-trips for nothing.

**Result of the real run:** 462 candidate rows → 147 missing names → **21
recovered** (14% yield, consistent with the extractor's documented 27-40% recall
on filings that do describe a deal). Recovered names are correct on inspection:
Broadcom/VMware, Synopsys/Ansys, J.M. Smucker/Hostess Brands, Denny's/Keke's
Breakfast Cafe, Sanmina/ZT Group, Bed Bath & Beyond/The Container Store.

The overlay is **fill-only** — an event absent from it keeps its DB value. The
backfill can never blank a good name.

### 2.5 Tightening the name validator — the gold set is the oracle
The first live grid still showed prose in the Acquirer column
("B deal, its second in four", "Qualcomm shares", "credit card accounts issued by
Wells Fargo"). `sanitize_ma_name` now also rejects a value carrying a **lowercase**
deal noun or prose verb, or opening with an all-lowercase word on a 3+ word value.

Every rule is lowercase-sensitive on purpose, and was tuned against the 480
verified names in `ma_event_overrides.json` as a regression oracle:

| attempt | garbage caught | verified gold names destroyed |
|---|---|---|
| consecutive-lowercase-run | yes | **26 / 262** — "lululemon athletica inc.", "salesforce.com, inc.", "SK hynix Inc." |
| + legal-suffix exemption, verbs | yes | 3 / 262 |
| **final** (3+ words, `str.islower()` first word) | yes | **1 / 262** — "digital banking business", a descriptor, not a company |

`_self_check()` now asserts that at most one verified name is rejected, so a
future tightening that starts eating real companies fails the check.

## 3. Coverage after the build (last 12 months, 14,261 M&A events)

| | raw DB | shown after overlay + gate |
|---|---|---|
| rows with Acquirer **and** Target | 8,651 | **7,368** (2,112 tickers) |
| rows with a Transaction Amount | 3,382 | **2,486** |

The ~1,300 rows the gate removes are the prose fragments and the mis-scaled /
per-share / credit-facility amounts. They render "—".

## 4. How to re-run the free backfill

```bash
# picks up where it left off; writes app/data/ma_event_overrides_edgar.json
.venv/bin/python scripts/enrich_ma_events_v2.py backfill-edgar --workers 4
.venv/bin/python app/utils/ma_8k_extract.py          # gate + gold regression
```

The daemon in `utils/ma_overrides_auto.py` keeps handling newly-inserted calendar
rows; the backfill is for the historical screening rows it never looked at.

## 5. Still true, still the data team's job

None of this repairs `coreiq_company_events`. The nightly enrichment job outside
this repo is still writing regex output — the Sep-2026 rows re-fetched during
this build still had `ma_acquirer = "Financial Statements of Businesses or Funds"`.
The app now hides that; it does not fix it.

---

# PART 3 — All-company coverage + amount rework (2026-09-16, after review)

Review question: *do all companies arrive, not just Coresight ones, and are
acquirer / target / amount as complete as possible?*

## 1. All companies DO arrive — measured

| | |
|---|---|
| M&A events in `coreiq_company_events` | 22,122 across **3,871 tickers** |
| of those tickers, in the Coresight universe (`coreiq_companies`) | **453** |
| rows returned by the screening path (`ALL_TICKERS`, all history) | **22,184** |
| distinct tickers that reached the grid | **3,871 — all of them** |
| rows showing a real company name (not a bare ticker) | **22,184 / 22,184** |

An unbounded Key Devs screen passes the `ALL_TICKERS` sentinel, so
`_keydev_ticker_clause` emits no ticker restriction at all; names for the 3,418
non-Coresight tickers come from the AV / SEC / non-SEC masters already joined in
`_KEYDEV_COMPANY_JOINS`. The 62 extra rows over 22,122 are the known JD/LULU/TSCO
ticker fan-out, collapsed downstream by `_event_id`.

## 2. The amount is now read from the text, not from the broken column

`ma_transaction_value_usd_m` is no longer trusted at all. **No row has a number
without its text** (`num_only = 0`, `text_only = 569`, `both = 7,414`), so the
text is always available and is always the better source.

`parse_ma_amount()` reads the figure and its own scale word. This *recovers* the
142 mis-scaled rows instead of hiding them: PAG `"$12,340,000"` was stored as
12,340,000 (i.e. $12.34 **trillion**) and now reads **$12.3M**. Still refused:
per-share quotes, non-USD, and a bare `"$441"` whose scale is unknowable.

## 3. The `Material Agreement` exclusion was too blunt — corrected

8-K **Item 1.01 covers merger agreements AND credit agreements**, so excluding
the subtype wholesale hid **624 genuine deals** — Adobe/Figma $1,000M,
BidCo/Adevinta $2,200M, Denso/Silicon Carbide $500M, CarGurus/CarOffer $75M.

Replaced with two conditions the row must satisfy *itself*:

1. no financing vocabulary in its text, headline, situation or party names
   (`credit agreement`, `revolv`, `indenture`, `notes`, `securitiz`, `receivabl`,
   `repurchase`, `preferred stock`, `Funding, LLC`, `aggregate principal`, …);
2. **both** a valid acquirer and a valid target — an amount alone cannot tell a
   purchase price from a borrowing.

Tuning was measured, not assumed:

| rule | Item 1.01 rows shown | precision on a 14-row sample |
|---|---|---|
| exclude the whole subtype | 0 | — (624 real deals lost) |
| financing vocabulary only | 839 | ~6/14 — "2018 Notes", "Taco Bell Funding, LLC", "Series A Senior Preferred" leaked |
| **+ both parties required** | **456** | **~10/14** |

Residual leakage is real and acknowledged: `CDW LLC → CDW Finance Corporation`
(notes co-issuer) and `Merrill Lynch → Alliance Data Systems` (underwriting
purchase agreement) still show. Tightening further started cutting genuine deals,
so this is the stopping point.

## 4. Final coverage, all history, all companies

| | baseline (first gate) | now |
|---|---|---|
| Acquirer | 13,513 (60.9%) | 13,513 (60.9%) |
| Target | 13,248 (59.7%) | 13,248 (59.7%) |
| **Transaction Amount** | 4,447 (20.0%) | **4,643 (20.9%)** |
| acquirer + target | 11,080 (49.9%) | 11,080 (49.9%) |
| all three | 3,566 (16.1%) | **3,820 (17.2%)** |

The headline number moved little; what changed materially is *correctness* — the
mis-scaled rows are now right rather than hidden, and real Item 1.01 acquisitions
are no longer discarded.

## 5. Rejected after measurement: filling names from headlines

Tested high-confidence headline patterns (`X to acquire Y`, `X completes its
acquisition of Y`) against every M&A row with a missing name. Result: **91 rows
filled out of ~11,000 missing (0.4%)**, and the targets were visibly wrong —
`"Pinnacle Foods for"`, `"Larry H. Miller Dealerships for"`, and `"Salesforce"`
for a headline whose target is Waeg. Not implemented; the remaining ~40% gap in
acquirer/target is upstream data, not something the app can parse its way out of.
