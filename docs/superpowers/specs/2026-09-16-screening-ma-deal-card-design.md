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
