# Why 26 companies are "On hold" and why TAP shows Feb 11, 2016

**Date:** 2026-08-19
**Area:** `/forecasting` → Refresh Data → Reporting Date column
**Status:** analysis — root cause is an ingestion gap in `coreiq_nasdaq_earnings_calendar`,
not app logic. No code change proposed here.

---

## 0. The whole popup in one table

Reporting Date shown across all 395 rows:

| Year shown | Rows | |
|---|---:|---|
| 2027 | 11 | scheduled future annual date |
| 2026 | 165 | current |
| 2025 | 143 | last annual cycle — normal |
| 2024 | 6 | stale |
| 2019 | 1 | stale |
| 2017 | 6 | stale |
| **2016** | **25** | stale |
| 2015 | 8 | stale |
| 2013 | 3 | stale |
| 2012 | 1 | stale |
| **On hold** | **26** | no annual date identifiable |

**319 of 395 (81%) are correct. 76 are not, and all 76 trace to one defect.**

## 1. How the date is chosen

`_annual_q4_report_dates_bulk()` reads the earnings calendar and, per ticker:

1. keeps rows whose **fiscal-quarter-ending month == the company's fiscal-year-end
   month** (that is the annual/Q4 announcement; interim quarters are excluded);
2. picks the **nearest upcoming** such date, else the **most recent past** one;
3. if no row survives step 1 → the ticker is shown "—" and put On hold.

There is deliberately no staleness filter and no projection — the column shows a real row
from the calendar table or nothing (product decision 2026-07-30).

A narrow tolerance exists for 52/53-week US retailers, whose vendors disagree by one month
(AV says "February", NASDAQ labels the same quarter "Jan/2025"): if no quarter matches the
FYE month exactly and exactly one quarter is one month away, that one is used. This is why
PVH, ZUMZ, VRA, OXM, TLYS, VNCE, CXM and SFD resolve rather than going On hold.

## 2. Root cause — the calendar backfill stopped at 2016 for 49 tickers

`coreiq_nasdaq_earnings_calendar` holds 18,553 rows over 355 tickers. Coverage is not
uniform. **49 tickers have a hard nine-year hole**: a block of ~35 rows ending in 2016,
**zero rows for 2017-2025**, then 1-2 rows in 2026.

TAP is the exact case from the screenshot:

```
2008-02-12 .. 2016-08-02   35 rows, every quarter, ALL fetched_at_utc = 2026-06-19
        (nothing at all for 2017 .. 2025)
2026-08-04  fqe=Jun/2026   fetched 2026-07-07   last_action=update
2026-08-06  fqe=Jun/2026   fetched 2026-08-06   last_action=update
```

TAP's fiscal year ends in December. Its newest **December-quarter** row is
`2016-02-11 (fqe Dec/2015)`. There is no upcoming annual row, so rule 2 falls back to the
most recent past one — **Feb 11, 2016**. The app is displaying exactly what the table
holds.

The signature of the defect: every pre-2017 row for these 49 tickers was written by a
single run on **2026-06-19**, and each ticker got **33-35 rows** — 2008-2016 is nine years
× four quarters ≈ 35. That is a first-page-only fetch: the source returns oldest-first,
the job took one page per ticker and never paginated to the recent rows. Compare AMZN,
backfilled on 2026-03-03 / 2026-04-16, which has all 68 rows from 2010 to 2026 with no
gap.

Same pattern in the YF calendar — Prada (1913.HK): 2011-2017 all `ingested_at`
2026-03-05, then nothing until 2026-07-30 and 2027-03-02 from the daily scrape.

Affected tickers (rows <2017 present, 2017-2025 empty, 2026 present):

```
ADM AMPL ANDE ANET APH ARW AVT CDW COKE CSCO CTSH CXM DELL DHI GLW HPE HPQ HRL JBL
KDP LEN MHK MO MSI NSIT NVR PANW PFGC PHM ROP SANM SEB SFD SGI SJM SMCI SNDK SNX
STZ SYY TAP TMHC TOL TSN UL  (+ a few more, 49 total)
```

## 3. Why the 26 are On hold

None of them has a single calendar row in their fiscal-year-end month.

| Reason | Count | Tickers |
|---|---:|---|
| Rows exist, but only interim quarters — never the annual one | 24 | `AI BRZE CHSCP CRWV DXC ESTC GTM HPE IBTA INGM KD KVUE KVYO MDB SPT USFD YEXT ZETA 2020.HK 2331.HK JD.L NXT.L S4M.HM TSCO.L` |
| No calendar rows at all | 2 | `FL`, `3998.HK` |

Nearly all of the 24 are recent listings (CRWV, GTM, IBTA, KVYO, KVUE, SPT, ZETA, BRZE,
MDB, ESTC, AI, INGM, KD) holding **one row**, dated Aug/Sep 2026 with
`fiscal_quarter_ending = Jun/2026` or `Jul/2026`. They were never backfilled at all — the
only thing that ever wrote them is the daily upcoming-earnings scrape, which by definition
only ever sees the next quarter. Until one of those companies actually announces a
full-year result inside the scrape window, no annual row exists.

Two special cases worth naming:

- **HPE** — has both problems: three rows before 2017, one in 2026, and its FYE is October
  while its rows are Apr/Jan/Jul. Nine years of October quarters are missing.
- **CHSCP** — FYE August, only Feb and May quarters, newest row 2023-04-05. Its coverage
  stopped entirely three years ago.

## 4. Why some rows show 2026 and 2027

Those tickers have future-dated rows from the daily scrape: **129 future-dated rows across
110 tickers** (e.g. WMT 2026-08-20, TGT 2026-08-19, all fetched 2026-08-18). Where that
future row happens to be the annual quarter, the popup shows it. Prada's Mar 02, 2027 is a
genuinely scheduled FY2026 annual announcement pulled on 2026-08-19.

A "2025" date is normal, not stale: a December-FYE company that announced FY2025 in
February 2025 and has no FY2026 date scheduled yet correctly shows Feb 2025.

## 5. What would fix it

All three symptoms — On hold, 2016 dates, and the 9-year holes — are the same data gap and
none is fixable in the portal. The calendar ingestion needs:

1. **Re-run the historical backfill with pagination**, so each ticker gets its full series
   rather than the oldest ~35 rows. Verification query: no ticker should have rows before
   2017 and zero rows in 2017-2025.
2. **Backfill the 26 On-hold tickers**, most of which have never been backfilled once.
3. **Alert on the shape, not the count.** A ticker whose newest annual row is more than ~15
   months old while its interim rows are current is a broken feed, and today nothing flags
   it — the app just shows a 2016 date and looks wrong.

Until then the app's behaviour is correct and traceable: every date in that column comes
from a real row in `coreiq_nasdaq_earnings_calendar` / `coreiq_yf_earnings_calendar`, and
On hold means no annual row exists to point at.
