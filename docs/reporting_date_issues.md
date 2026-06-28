# Forecasting — Reporting Date Issues (On-Hold Tickers)

**For:** Data team / Manager
**Scope:** Forecasting page → "Refresh Data" popup. These 49 tickers have **no Annual Reporting Date** identified, so their *Refresh Now* button is hidden ("on hold") and they are skipped by *Refresh All*.
**Finding:** All 49 trace back to missing source data in the earnings-calendar feeds — **not an application bug.** The app correctly hides them.

## How the reporting date is derived
The annual **Reporting Date** comes from the earnings-calendar feeds — `coreiq_nasdaq_earnings_calendar` (US tickers) and `coreiq_yf_earnings_calendar` (foreign/composite tickers). The resolver picks the **full-year (Q4) announcement date** (fiscal-quarter-end month = fiscal-year-end month), preferring the upcoming date, else the most recent past. A **staleness guard drops any date older than 460 days (~15 months)** (`app/data/forecast_refresh_service.py:569`) — a 15-month-old "latest" date means the company's current annual report simply hasn't been ingested.

## The 4 reasons (49 companies)

| Reason | Count | What it means for the data team |
|---|---|---|
| **Outdated calendar** | 25 | US tickers — they have annual dates, but the latest is the **prior fiscal year** (>460 days old). The current-FY annual report date is **missing from the NASDAQ feed**. |
| **No full-year (Q4) date** | 20 | Foreign tickers — the Yahoo feed has only **interim/half-year** dates; no identifiable full-year report date for the fiscal year-end. |
| **Not in calendar** | 2 | No earnings-calendar rows at all for the ticker. |
| **Missing fiscal year-end** | 2 | No `fiscal_year_end` on file, so the Q4 period can't be identified. |

## Full list (grouped)

### ① Outdated calendar — current-FY annual date missing from NASDAQ feed (25)
AAP (Advance Auto Parts), AKA (a.k.a. Brands Holding), CMG (Chipotle Mexican Grill), CRI (Carter's), DDS (Dillard's), DPZ (Domino's Pizza), FOSL (Fossil Group), HLN (Haleon), KHC (Kraft Heinz), KTB (Kontoor Brands), MCD (McDonald's), MUSA (Murphy USA), QSR (Restaurant Brands International), REAL (The RealReal), SBH (Sally Beauty), SHAK (Shake Shack), SHOO (Steven Madden), SHW (Sherwin-Williams), SNBR (Sleep Number), TDUP (ThredUp), TXRH (Texas Roadhouse), VYX (NCR Voyix), WEN (The Wendy's Company), WWW (Wolverine World Wide), YUM (Yum! Brands)

### ② No full-year (Q4) date — only interim/half-year dates in Yahoo feed (20)
1913.HK (Prada), 2020.HK (ANTA), 2331.HK (Li Ning), BEI.DE (Beiersdorf), BN.PA (Danone), CA.PA (Carrefour), JD.L (JD Sports), KER.PA (Kering), MC.PA (LVMH), MONC.MI (Moncler), NESN.SW (Nestlé), NXT.L (NEXT), OCDO.L (Ocado), OR.PA (L'Oréal), PVH, RKT.L (Reckitt), RMS.PA (Hermès), TSCO.L (Tesco), VNCE (Vince), VRA (Vera Bradley)

### ③ Not in calendar (2)
3998.HK (Bosideng), QRTEA (QVC)

### ④ Missing fiscal year-end (2)
SCVL (Shoe Carnival), VSCO (Victoria's Secret)

## Recommended action for the data team
- **①/③ (NASDAQ):** Re-source / back-fill `coreiq_nasdaq_earnings_calendar` with current-FY annual reporting dates (e.g. the ~Feb 2026 announcements) — they are absent even though rows were re-fetched recently. The feed is missing forward annual dates.
- **②/③ (foreign, Yahoo):** Ingest full-year (annual) reporting dates for these listings into `coreiq_yf_earnings_calendar` — only interim/half-year dates are present.
- **④:** Populate `fiscal_year_end` for SCVL and VSCO in `coreiq_av_company_overview`.

---
*Day-counts in the CSV reflect the diagnostic run on 2026-06-16; the ticker classification itself is stable. Re-run the diagnostic for fresh day-counts if needed.*
