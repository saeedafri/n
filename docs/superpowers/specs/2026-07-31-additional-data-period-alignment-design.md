# Additional Data tab — period filter & column alignment

Date: 2026-07-31
Scope: `app/data/repository.py` (RatingsDataRepository + new `annual_fiscal_dates`), `app/pages/market_data.py` (ratings header label)

## Symptom

For WMT, the Additional Data tab showed:

- Start Date **December 2018** → End Date **December 2026** (Income Statement offered
  **January 2021 → January 2026**)
- Column headers **`10-K / Jan-31-2026`**, a different set and count of periods than
  Income Statement / Key Stats / Segments (`12 Months / Jan-31-2021 … Jan-31-2026`)

## Root cause (two independent defects)

1. `RatingsDataRepository.get_available_dates()` returned `date(y, 12, 31)` and
   `get_date_range()` returned `(Jan-31 of first year, Dec-31 of last year)` — a **calendar**
   year end. Every other tab feeds the shared Start/End dropdowns with the company's **fiscal**
   period ends. Because the shared start/end session values get snapped to the closest available
   option, a Jan-FYE company also ended up with a *different period range* (hence a different
   column count), not just different labels.
2. The column header dates came from `period_display_dates`, built from
   `SegmentDataRepository._fye_month(ticker)` → `coreiq_av_company_overview.fiscal_year_end`.
   That column is **NULL for 49 of the 135 tickers** with ratings/store data (COST, LULU, CRI,
   DKS, VFC …), so those fell back to Dec-31 (or, when the map came back empty, to raw 10-K
   *filing* dates like `Oct-07-2020` for COST).

## Fix

- New module-level `annual_fiscal_dates(ticker) -> {year: date}` in `repository.py`, reading the
  already-cached annual income-statement rows (`_fetch_all_annual_rows`) — the exact dates the
  statement tabs print. No extra DB round-trip.
- `RatingsDataRepository._fiscal_dates(ticker, years)`: statement date per year first; otherwise
  the FYE month from the overview, otherwise the FYE month inferred from the statement dates
  (modal month), otherwise Dec-31. Used by **both** `get_available_dates` / `get_date_range`
  **and** `period_display_dates`, so the filter and the headers can no longer disagree.
- Ratings header period label now reads `12 Months` (same as the other annual tabs) whenever a
  fiscal date is known; `10-K` + bare year remains only as the unknown-date fallback. The
  `Annual (10-K filings)` sub-caption is unchanged.

No DB writes. The Excel export already consumed `period_display_dates`, so it inherits the fix.

## Blast radius

135 tickers have ratings/store rows. 40 have a known non-Dec FYE and were visibly wrong
(ACI, AI, ANF, AZO, BBW, BBY, BURL, CAL, CRMT, CTRN, CURV, DECK, DXC, DXLG, FIVE, FL, FLWS, HD,
HPE, HZO, KR, KSS, M, MNRO, NKE, OXM, PFGC, PLCE, ROST, TGT, TJX, TLYS, UAA, ULTA, URBN, VNCE,
VRA, WMT, WSM, ZUMZ). A further 49 with NULL `fiscal_year_end` were wrong via defect 2. The
remaining Dec-FYE tickers are unchanged.

## Verification (local UI, staging DB, Playwright)

| Ticker | Additional Data (after) | Income Statement | Match |
|---|---|---|---|
| WMT | Jan 2021 → Jan 2026, `12 Months / Jan-31-2021…` | Jan 2021 → Jan 2026, `12 Months / Jan-31-2021…` | ✅ |
| AZO | Aug 2020 → Aug 2025 | Aug 2020 → Aug 2025 | ✅ |
| COST (NULL FYE) | Aug 2020 → Aug 2025, `12 Months / Aug-31-2020…` | Aug 2020 → Aug 2025 | ✅ |
| LULU (NULL FYE) | Jan 2021 → Jan 2026, `12 Months / Jan-31-2021…` | Jan 2021 → Jan 2026 | ✅ |
| CRI (NULL FYE) | Dec 2021 → Dec 2026, `12 Months / Dec-31-2021…` | Dec 2020 → Dec 2025 | labels ✅ |

## Open item (data side, not fixed here)

CRI exposes a fiscal **2026** ratings/store year while its statements stop at 2025, and its
range starts a year later than the statements. That is a `coreiq_filing_metrics_v5`
`report_fiscal_year` coverage/labelling question for the data team — the display layer now
labels whatever years exist consistently with the other tabs.
