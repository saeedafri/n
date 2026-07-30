# Reporting-date resolution — production-grade hardening

**Date:** 2026-07-30
**Status:** **F1, F2, F5 IMPLEMENTED and verified 2026-07-30.** F3 deliberately NOT done —
the auto-refresh already gates on `needs_update()` (data-change driven), which is correct.
F4 not done.

## IMPLEMENTED — results

| Metric | Before | After |
|---|---|---|
| Fiscal year-end resolved | 256 | **401** |
| Annual reporting dates resolved | 124 | **321 — every one a real calendar row** |
| On hold in the dialog | 245 | **27 — 100% data gaps** |
| Estimated / projected dates | — | **0** |

### F6 — REVERTED: no estimates, no staleness filter (product decision 2026-07-30)

A projection tier was built and then removed at the product owner's instruction.
Both the 460-day staleness filter and the projection are gone. The Reporting Date
column now shows **the real annual announcement date held in the calendar table,
whatever its age**, and every value is traceable to a row in
`coreiq_nasdaq_earnings_calendar` / `coreiq_yf_earnings_calendar`.

Selection still prefers the nearest UPCOMING annual date and only falls back to the
most recent PAST one, so a past date means the calendar genuinely holds nothing newer.
Split on STG: **31 upcoming, 290 already-reported.**

Rationale for the revert: the column must be verifiable against the source table. An
estimate — however well flagged — cannot be checked by anyone reading the dialog, and
a date the vendor never published should not appear in a client-facing surface.

**Known trade-off, accepted:** 290 of the 321 dates are in the past, because of the
Dec/2025–Mar/2026 ingestion hole. The column therefore reads "last known annual
reporting date", not "next". It becomes forward-looking again the moment the data team
backfills the calendar — no code change required.

Rejected outright: substituting the nearest *interim* announcement when no annual row
exists. A Q3 date labelled as the annual reporting date is a wrong fact, not an
approximation.

### Superseded — the projection design (kept for reference)

A cross-table sweep with routing disabled — every company checked against **both**
calendars by every key — showed 164 companies whose annual row *exists* but predates
the 460-day cutoff. They still report annually; the calendar simply never received the
newer row (the Dec/2025–Mar/2026 ingestion hole).

Companies report in a near-fixed calendar slot, so the last **confirmed** annual date
rolled forward in whole years lands within days of the real one. Two hard guards, because
a fabricated date is worse than no date:

1. **Anchor must be recent** — `_MAX_ANCHOR_AGE_DAYS = 920` (~2.5 years). 46 of the 164
   anchor on rows over four years old; `AMPL`'s is dated **2012**, years before the
   company existed. Projecting from junk yields junk, so those stay on hold.
2. **Always flagged** — the info dict carries `is_estimated` and `anchor_date`; the dialog
   renders `~Feb 26, 2027 (est.)` and exposes `annual_date_anchor`. An estimate can never
   masquerade as a confirmed calendar entry.

Result: **116 projected**, 48 correctly refused.

Note on the earlier "interim → closest to annual" idea: taking the nearest *interim*
announcement and calling it the annual date was rejected. A Q3 announcement date labelled
as the annual reporting date is simply a wrong fact. Projection from a confirmed annual
anchor is an estimate of the right quantity; substituting an interim date is not.

Regression test (pre-fix baseline captured via `git stash`, then diffed):

```
superset (old ⊆ new) : True
lost                 : []      ← no company lost a date it previously had
changed existing     : 0       ← no existing date moved
newly gained         : 34
```

Files changed:
- `app/utils/constants.py` — new `YAHOO_SUFFIXES`, `yahoo_symbol()`, `is_yahoo_suffix()`
- `app/data/repository.py` — `get_companies_map()` / `get_companies()` / `_resolve_forecast_ticker()` / 2 forecast-ticker sites
- `app/data/revenue_forecast_service.py` — 2 forecast-ticker sites
- `app/data/forecast_admin_service.py` — `_forecast_store_ticker()`
- `app/data/forecast_refresh_service.py` — FYE chain + Q4 resolver + drop diagnostics

Verified in the real UI: LSPD Forecasting tab renders (ensemble 1,426 → 2,276);
ADS.DE income statement unchanged (control).

### Final attribution — every remaining hold is a data gap

| Cause | Companies |
|---|---|
| Calendar's newest annual row >15 months old (the Dec/2025–Mar/2026 outage) | 158 |
| Calendar holds only interim rows — none matches their fiscal year end | 31 |
| No earnings-calendar row at all | 25 |
| **Engineering-caused** | **0** |

The 4 composite tickers checked individually: `SHOP.TO` has **zero** rows in
`coreiq_yf_earnings_calendar`; `JD.L` (2 rows), `OR.PA` (3 rows), `TSCO.L` (1 row)
hold only interim dates that can never match their year-end month.

---

## Original design follows
**Related:** `2026-07-30-missing-company-data-lspd-vu-sqsp-crto-design.md`

---

## 1. Measured verdict: how much is code, how much is data

Simulated a corrected resolver against STG and compared with production output.

| | Companies | Owner |
|---|---|---|
| Currently resolved | 124 | — |
| **Recoverable by a code fix alone** | **39** | **Engineering** |
| Still on hold — calendar has no annual row in window | 158 | Data |
| Still on hold — no Q4 row for their fiscal calendar | 41 | Data |
| Still on hold — no fiscal-year-end in any source | 11 | Data |

So **39 of 249 holds (16%) are self-inflicted**. The rest are real gaps. The claim
"there is no issue from the data team" is not supported — but neither was the earlier
implication that our code was blameless.

### 1a. The 39 we lose ourselves

`_annual_q4_report_dates_bulk()` **INNER JOINs** `coreiq_av_company_overview` for
`fiscal_year_end`. That single source resolves 221 tickers. Resolving from every source
we already hold gives **401**:

| Source | Tickers added |
|---|---|
| `coreiq_av_company_overview.fiscal_year_end` | 221 |
| `coreiq_model_forecasts.last_actual_date` (MONTH of MAX) | +138 |
| `coreiq_yf_company_overview` → `$.info.lastFiscalYearEnd` | +34 |
| `coreiq_av_financials_income_statement.fiscal_date_ending` | +8 |
| **Total** | **401** |

Recovered examples, all with valid upcoming annual dates:
`CRM 2026-02-25`, `CSCO 2026-08-12`, `IBM 2026-01-28`, `INTC 2026-01-22`,
`CVS 2026-02-10`, `CL 2026-01-30`, `CLX 2026-08-03`, `DASH 2026-02-18`,
`ETSY 2026-02-19`, `DG 2026-03-12`, `INTU 2026-08-20`, `DECK 2026-05-21`.

The irony: `coreiq_model_forecasts.last_actual_date` is **already displayed** as the
dialog's "Fiscal Period" column. We show the year-end month on screen and then drop the
company for not having one.

### 1b. The data gap is real and provable

`coreiq_nasdaq_earnings_calendar` — distinct tickers per fiscal quarter:

| Fiscal quarter | Tickers |
|---|---|
| Dec/2024 | 154 |
| Mar/2025 | 154 |
| Jun/2025 | 154 |
| Sep/2025 | 141 |
| **Dec/2025** | **52** |
| **Mar/2026** | **69** |
| Jun/2026 | 202 |

Coverage collapses across **Dec/2025 – Mar/2026**, then recovers. Of 105 December-FYE
companies, only **25** have a `Dec/2025` row while 67 have `Sep/2025`.

This is an **ingestion outage window**, not staleness. CRI (Carter's) proves it: its
calendar holds rows through `2026-07-31`, but jumps `Sep/2025 → Mar/2026` with no
`Dec/2025`. December is the most common FYE, so the hole lands squarely on annual dates.

`fiscal_quarter_ending` is never NULL (0 of 15,999 rows), so nothing is lost to that filter.

---

## 2. Defects in the current logic

### D1 — the dot heuristic
`'.' in ticker` is used to decide "foreign listing". The dot only exists because
`get_companies()` concatenates `ticker + '.' + exchange_acronym` without validating the
acronym. A bad acronym (`TSX`, `NASDAQ`) produced a dot, routing US companies down the
YFinance path and building symbols Yahoo never had. Inferring semantics from a string
artefact the app itself synthesised is circular.

### D2 — INNER JOIN on one FYE source
Costs 39 companies. See 1a.

### D3 — the reporting date is used as a refresh gate
`forecasting.py:2975` builds `_blocked_tickers` from `not _has_report_date(...)`, which
feeds `sync_all_eligible(exclude_tickers=...)` — used by both "Refresh All" and the
twice-daily scheduler.

**This gate is unnecessary.** `forecast_store.needs_update(ticker, last_actual_date)`
already decides staleness by comparing stored `last_actual_date` against the newest
actuals — it never consults the reporting date. The engine cannot be wastefully run,
because `needs_update()` short-circuits it at a cost of one read RTT.

Consequence today: 249 companies are excluded from every automatic refresh, so their
forecasts freeze permanently — because we do not know an announcement date that the
refresh decision does not need.

### D4 — silent drops
A company failing any gate vanishes with no diagnostic. Reconstructing *why* required
this whole investigation.

---

## 3. Proposed fix

### F1 — validate the acronym; delete the dot heuristic
Single source of truth for what a Yahoo suffix is:

```python
YAHOO_SUFFIXES = frozenset({
    "L","PA","DE","TO","V","HK","T","AX","ST","MI","SW","MC","KS","AS",
    "HM","F","BE","SG","VI","BR","LS","IR","OL","CO","HE","WA","TW","SI","NZ","SA",
})

def yahoo_symbol(ticker: str, exchange_acronym: str | None) -> str:
    """Composite symbol, or the plain ticker when the acronym is not a real suffix."""
    acr = (exchange_acronym or "").strip()
    return f"{ticker}.{acr}" if acr in YAHOO_SUFFIXES else ticker
```

Applied in `get_companies_map()`, `get_companies()`, `_forecast_store_ticker()` and the
Q4 resolver. A junk acronym then degrades to "plain ticker, no data" instead of
"invented ticker nothing can join on". Route path selection off
`coreiq_companies.source`, never off a dot.

### F2 — one FYE resolver, four sources, no INNER JOIN
Extend `_fiscal_year_end_bulk()` to the full chain (AV overview → `last_actual_date` →
AV income statement → YF payload), and have the Q4 query `LEFT JOIN` and resolve in
Python. Recovers 39 immediately, and more as data lands.

### F3 — the reporting date becomes informational only
Drive refresh eligibility from `needs_update()`. Remove `exclude_tickers` from the
"Refresh All" and scheduler paths. Keep "Refresh Now" enabled for every company;
render the reporting date as `—` where unknown without disabling the action.

### F4 — staleness guard: keep, but stop hiding
Keep the 460-day rule for *display* (a 17-month-old date must not read as current), but
show `last known: 25 Feb 2025` rather than a bare dash, and never let it gate refresh.

### F5 — make every drop observable
Emit a per-ticker reason (`no_calendar_row`, `no_fiscal_year_end`, `no_q4_row`,
`stale_annual_date`) via `log_structured_error`/`log_timing`, and surface it as a tooltip
on the hold chip.

---

## 4. Expected outcome

| | Now | After |
|---|---|---|
| Reporting date shown | 124 | **163** |
| Companies eligible for auto-refresh | 121 | **370** |
| Companies whose forecasts can go permanently stale | 249 | **0** |

F3 is the highest-value change: it is what stops forecasts freezing, and it is
independent of any data-team work.

---

## 5. Testing plan

1. Unit: `yahoo_symbol()` over `('ADS','DE')→ADS.DE`, `('LSPD','TSX')→LSPD`,
   `('CRTO','')→CRTO`, `('SHOP','TO')→SHOP.TO`.
2. Regression: `_annual_q4_report_dates_bulk()` must return a superset of today's 124,
   with no existing date changed. Diff old vs new keys and assert `old ⊆ new`.
3. Confirm the 39 recovered tickers match the simulated list exactly.
4. `needs_update()` must still short-circuit: run "Refresh All" twice, assert the second
   pass upserts 0 rows.
5. UI: Forecasting tab renders for LSPD and CRTO; ADS.DE (control) unchanged.
6. Scheduler dry-run: assert no ticker is excluded for a missing reporting date.

## 6. Rollout

F1/F2/F5 are read-path only — safe to ship together. **F3 changes what the twice-daily
scheduler does** (370 candidates instead of 121); `needs_update()` bounds the cost to one
read RTT per unchanged company, but ship it separately and watch one full cycle.

## 7. Data-team items (independent of all the above)

1. Backfill `coreiq_nasdaq_earnings_calendar` for **Dec/2025 – Mar/2026** — the outage window.
2. Populate `coreiq_av_company_overview.fiscal_year_end` (146 empty rows).
3. Add calendar rows for the 17 companies with none, incl. CRTO and LSPD.
