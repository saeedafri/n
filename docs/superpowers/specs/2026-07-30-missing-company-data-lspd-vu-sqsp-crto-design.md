# Missing MDP data for Lightspeed / VusionGroup / Squarespace / Criteo — root-cause investigation

**Date:** 2026-07-30
**Env:** STG (`coresight_market_data_stg` @ `csr-mysql8-flex-stg`)
**Verdict:** **DATA issue (data team) — with a code amplifier.** Not a display bug in MDP.
**Blast radius:** **34 `coreiq_companies` rows**, not 4. Includes ORCL, NOW, SHOP, HPQ, MDB, TTD, APP, WIX, ESTC, HUBS, TWLO, ZBRA, MANH, LULU.

---

## 1. Verdict summary

| Company | In `coreiq_companies`? | Dropdown ticker MDP emits | Market/financial data in DB | Verdict |
|---|---|---|---|---|
| Lightspeed Commerce (LSPD) | yes (id 10879, `source=YFinance`, TSX) | **`LSPD.TSX`** | none from YF; 300 SEC facts rows | data |
| VusionGroup (VU) | yes (id 10881, `source=YFinance`, EPA) | **`VU.EPA`** | none anywhere | data |
| Squarespace (SQSP) | yes (id 10872, `source=SEC`, NYSE, **Delisted**) | **`SQSP.NYSE`** | no AV/YF/SEC-facts; only filings | data |
| Criteo (CRTO) | yes (id 10894, `source=YFinance`, NasdaqGS) | **`CRTO.NasdaqGS`** | none from YF; 945 SEC facts rows | data |

---

## 2. Root cause

`coreiq_companies.exchange_acronym` is contractually a **Yahoo Finance ticker suffix**
(`L`, `PA`, `DE`, `TO`, `HK`, `T`, `AX`, `ST`, `MI`, `SW`, `MC`, `KS`, `AS`). Both the
ingestion pipelines and the portal build a composite symbol as
`CONCAT(ticker, '.', exchange_acronym)`.

For 34 rows the data team wrote an **exchange name** into that column instead:
`NYSE` (14), `NasdaqGS` (13), `NASDAQ` (3), `TSX` (2), `EPA` (1), `ADX` (1).

Consequences, both proven below:

1. **Ingestion side (why there is no data).** The YF pipeline requested
   `LSPD.TSX`, `VU.EPA`, `CRTO.NASDAQGS` from Yahoo. Those are not valid Yahoo
   symbols (correct: `LSPD.TO`, `VU.PA`, `CRTO`). Yahoo returned an empty stub, so
   every YF fact table is empty for these tickers.
2. **Portal side (why the ticker looks wrong).** `CompanyRepository.get_companies()`
   (`app/data/repository.py:335`) drops the plain-ticker entry whenever
   `exchange_acronym` is set, on the assumption a valid composite twin exists. The
   dropdown therefore offers `LSPD.TSX` / `SQSP.NYSE`, and every downstream lookup
   keyed on that composite misses rows stored under the plain ticker.

---

## 3. Evidence

### 3.1 Master record exists for all four

```sql
SELECT id,ticker,name_coresight,exchange,exchange_acronym,source,listing_status
FROM coreiq_companies WHERE ticker IN ('LSPD','VU','SQSP','CRTO');
```

| id | ticker | name_coresight | exchange | exchange_acronym | source | listing_status |
|---|---|---|---|---|---|---|
| 10872 | SQSP | Squarespace, Inc. | NYSE | `NYSE` | SEC | **Delisted** |
| 10879 | LSPD | Lightspeed Commerce Inc. | TSX | `TSX` | YFinance | Public |
| 10881 | VU | VusionGroup | EPA | `EPA` | YFinance | Public |
| 10894 | CRTO | Criteo S.A. | NasdaqGS | `NasdaqGS` | YFinance | Public |

Secondary defect on the same rows: `ADR_TICKER` holds industry text
(`'POS & Commerce Platform'`, `'Retail Technology'`, `'Advertising Technology'`)
instead of an ADR ticker.

### 3.2 Full 141-table sweep

Every table in the schema carrying `ticker` / `symbol` / `yf_symbol` /
`query_ticker` / `cik` / `company_name` was counted for all ticker variants
(`LSPD`, `LSPD.TO`, `VU`, `VU.PA`, `VUSNF`, `SQSP`, `CRTO`) plus CIK and name.
**80 of 103 candidate tables returned zero rows for all four.** Non-zero:

| Table | LSPD | VU | SQSP | CRTO |
|---|---|---|---|---|
| `coreiq_companies` | 1 | 1 | 1 | 1 |
| `coreiq_search_company_view` | 1 | 1 | 1 | 1 |
| `coreiq_sec_companies_all` | 2 | – | – | 1 |
| `coreiq_non_sec_companies_all` | – | 1 | – | 1 |
| `coreiq_av_companies_all` | 1 | – | 1 | 1 |
| `coreiq_yf_company_overview` | 6 | 6 | – | 6 |
| `coreiq_sec_company_facts_prod` | 300 | – | – | 945 |
| `coreiq_sec_company_facts_prod_hist` | 300 | – | – | 945 |
| `coreiq_sec_company_facts_all_metrics` | 4,217 | – | – | 16,396 |
| `coreiq_sec_company_facts_all_metrics_hist` | 29,519 | – | – | 114,772 |
| `coreiq_company_events` (Key Devs) | 169 | – | – | 334 |
| `coreiq_av_market_news_sentiment` | 77 | – | – | 184 |
| `coreiq_yf_market_news_sentiment` | 30 | 25 | – | 76 |
| `yf_article_head` | 29 | 503 | – | 41 |
| `coreiq_filing_document_logs` | – | – | 352 | – |
| `coreiq_filing_documents` | – | – | 44 | – |
| `coreiq_filing_metrics_v5` | – | – | 30 | – |

Zero everywhere that matters for the Market Data page:

- `coreiq_av_company_overview` — **0** for all four (ORCL = 1). No Key Stats.
- `coreiq_av_financials_income_statement / balance_sheet / cash_flow / earnings / earnings_estimates` — **0** for all four (ORCL = 103 IS rows).
- `coreiq_av_time_series_daily` — **0** for all four (ORCL = 6,722). No price/chart.
- `coreiq_yf_financials_*`, `coreiq_yf_time_series_daily`, `coreiq_yf_shares_outstanding` — **0** for all four (ADS.DE = 573 IS rows / 2,652 price rows).
- `coreiq_nasdaq_earnings_calendar`, `coreiq_yf_earnings_calendar*`, all transcript tables, `coreiq_model_forecasts*`, `coreiq_ir_websites`, `coreiq_screening_*_cache` — **0** for all four.

`coreiq_filing_metrics_v2/v3/v4` returned 0 by `ticker` for LSPD/VU/CRTO; only
`v5` has 30 SQSP rows (8-K, 2021–2024). Note `coreiq_filing_metrics_*.cik` is
**unindexed and NULL for these rows** (SQSP: 30/30 NULL), so `ticker` is the only
usable key there.

### 3.3 The Yahoo symbol was built wrong — the stub proves it

`coreiq_yf_company_overview` writes a daily row per company. For the three
YF-sourced targets the stored `yf_symbol` is the corrupted composite and the
payload is an empty yfinance error stub:

```
ticker=LSPD  yf_symbol=LSPD.TSX        payload_json len=388
ticker=VU    yf_symbol=VU.EPA          payload_json len=388
ticker=CRTO  yf_symbol=CRTO.NASDAQGS   payload_json len=388

payload_json = {"info": {"trailingPegRatio": null}, "fast_info": "lazy-loading dict with keys = [...]"}
```

Control — adidas, whose acronym is correct:

```
ticker=ADS   yf_symbol=ADS.DE          payload_json len=8259   (real address/sector/summary)
ADS: yf_company_overview 165 rows · yf_financials_income_statement 573 · yf_time_series_daily 2,652
```

388 bytes with a lone `trailingPegRatio: null` is exactly what `yfinance` returns
for an unknown symbol. The pipeline has been writing that stub every night
(2026-07-25 → 2026-07-30, 01:31 UTC) without flagging failure.

### 3.4 Portal drops the plain ticker (code amplifier)

`app/data/repository.py:335` in `get_companies()`:

```python
exch = row.get('exchange_acronym')
if exch and '.' not in ticker:
    continue          # assumes a composite twin exists in the map
```

`get_companies_map()` (`repository.py:387`) manufactures that twin as
`f"{ticker}.{exchange_acronym}"`. Headless run of the real code path:

```
total dropdown entries: 379
  DROPDOWN -> {'ticker': 'CRTO.NasdaqGS', 'name': 'Criteo S.A.'}
  DROPDOWN -> {'ticker': 'LSPD.TSX',      'name': 'Lightspeed Commerce Inc.'}
  DROPDOWN -> {'ticker': 'SQSP.NYSE',     'name': 'Squarespace, Inc.'}
  DROPDOWN -> {'ticker': 'VU.EPA',        'name': 'VusionGroup'}
  'LSPD' present: False   'VU' present: False   'SQSP' present: False   'CRTO' present: False
  'ADS.DE' present: True  'ADS' present: False
```

77 rows total take this skip path; for the 43 with a legitimate Yahoo suffix it is
correct (`ADS` → `ADS.DE`), for the 34 corrupted ones it produces a dead ticker.

### 3.5 Live UI verification (localhost:8501 → STG DB)

`/market_data?ticker=LSPD.TSX`:

```
Ticker LSPD · Name Lightspeed Commerce Inc. · Exchange Toronto Stock Exchange (TSX)
Business Description   No business description available.
Stock Quote and Chart (Currency: N/A (N/A))
Closing Price -   Opening Price -   Change on Day -   Market Capitalization -
Shares Out. (mm) -   Dividend Yield % -   52 Week High/Low - / -
No price history available
```

Control `/market_data?ticker=ADS.DE` on the same build renders fully
(P/E 23.64, EPS €7.71, Revenue TTM €25.25B, currency, country, fiscal year end),
so the page itself is healthy — only these companies have no rows to render.

---

## 4. Full list of affected companies (34 rows)

| bad acronym | tickers |
|---|---|
| `NYSE` (14) | AI, CXM, ESTC, HPQ, HUBS, IBTA, KVYO, NOW, ORCL, SHOP, **SQSP**, TWLO, YEXT, ZETA |
| `NasdaqGS` (13) | AMPL, APP, BRZE, CMRC, **CRTO**, FRSH, GTM, MDB, NICE, SPSC, SPT, TTD, WIX |
| `NASDAQ` (3) | CMRC, MANH, ZBRA |
| `TSX` (2) | **LSPD**, SHOP |
| `EPA` (1) | **VU** |
| `ADX` (1) | LULU |

The 30 SEC-sourced rows here are less visibly broken (SEC facts arrive via CIK, so
ORCL/NOW/MDB still have `coreiq_sec_company_facts_prod` data), but they are all
still offered in MDP under a dead composite ticker (`ORCL.NYSE`, `MDB.NasdaqGS`),
so anything keyed on the dropdown ticker — forecasts (`coreiq_model_forecasts`
stores `ADS.DE`-style keys), watchlists, saved criteria — mismatches for them too.

---

## 5. Fix plan

### 5.1 Data team (owner of the fix — required)

1. Correct `coreiq_companies.exchange_acronym` on the 34 rows to the real Yahoo
   suffix, or `NULL`/`''` for US listings that need no suffix:
   `SQSP → NULL`, `CRTO → NULL`, `ORCL → NULL`, `NOW → NULL`, … ,
   `LSPD → 'TO'`, `VU → 'PA'`, `LULU → 'AE'` (verify), `SHOP` → `NULL` for the
   NYSE row and `'TO'` for the TSX row.
2. Clear `ADR_TICKER` on the rows where it holds industry text.
3. Re-run the YF and AV ingestion for the corrected symbols so
   `coreiq_av_company_overview`, `coreiq_av_financials_*`,
   `coreiq_av_time_series_daily`, `coreiq_yf_*` get populated.
4. SQSP is **Delisted** (went private Oct 2024) — decide whether it stays in the
   universe. If it stays it will never get fresh price data; only its 2021–2024
   filings exist.
5. Add a pipeline guard: treat a `yfinance` payload of `{"info": {"trailingPegRatio": null}}`
   as a **hard failure** and alert, instead of writing the stub nightly.

Per project policy this repo does **no DB writes** — the data team must apply 1–3.

### 5.2 Portal (defensive, optional, our side)

`get_companies()` currently trusts `exchange_acronym` blindly. Harden it to only
emit the composite when the composite actually resolves to data — e.g. validate
the acronym against the known Yahoo-suffix set, or fall back to the plain ticker
when no fact table has rows under the composite. This would have surfaced the bad
rows as "company present, no data" rather than "company present under a ticker
nothing can join on".

Not started — needs a decision from the user before any code change, since the
underlying defect is upstream and fixing only the portal would hide it.

---

## 5b. Resolution log (2026-07-30, same day)

Data team applied the column fix on STG; portal side verified end to end.

| ticker | final state | outcome |
|---|---|---|
| LSPD | `exchange_acronym=NULL`, `source='SEC'` | **FIXED** — AV 11 annual / 34 quarterly; 100 forecast rows; Forecasting tab renders |
| CRTO | `exchange_acronym=''`, `source='SEC'` | **FIXED** — AV 16 annual / 60 quarterly; 50 forecast rows; Forecasting tab renders |
| SQSP | `exchange_acronym=''`, `source='SEC'` | **NOT FIXABLE** — zero rows in every AV/YF/SEC fact table; delisted |
| VU → H4M | retickered to `H4M`/`HAM`/`HM` | **STILL BROKEN** — `H4M.HM` also returns the 388-byte stub; needs `VU` + `PA` |

Forecast rows were generated with `sync_forecast_for_ticker(force=True)` (the same
call `/forecasting_admin` makes): CRTO 50 rows, LSPD 100 rows, both stored under the
plain ticker.

### Two traps hit during the fix, worth remembering

1. **`SET exchange_acronym = 'NULL'`** (quoted) — stored the 4-char string `NULL`
   (`HEX` = `4E554C4C`). The code does `(val or '').strip() or None`, and `'NULL'` is
   truthy, so it built `LSPD.NULL` — worse than `LSPD.TSX`. Use `''` or unquoted
   `NULL`, and verify with `CHAR_LENGTH`.
2. **Stale process.** After the column fix the page still showed
   "No extracted data available" because the running Streamlit process had cached the
   pre-fix result (`get_date_range` 1h, `get_companies_map` 1h, `get_companies` 6h).
   A restart is required, and the PID must actually change — `pkill -f "streamlit
   run.*app/main.py"` does **not** match the real command line
   (`streamlit run main.py`, cwd `app/`).

### Design smell found (not fixed — needs its own change)

`app/pages/market_data.py:2807` gates **every** financial tab, Forecasting included,
on the *income statement* date range:

```python
if not min_date or not max_date:
    if selected_tab != "company_profile":
        st.info("No extracted data available. Check official filings.")
else:
    ...  # entire tab body, incl. the forecasting branch at line 4227
```

`/forecasting` has no such gate — it reads `coreiq_model_forecasts` directly. So a
company with forecasts but no AV income statement shows forecasts on `/forecasting`
and a misleading "check official filings" on the tab. The gate should be per-tab.
This is what made LSPD look like a forecasting bug when it was an income-statement
routing bug. Awaiting a decision before changing.

Note: the market-data Forecasting tab has **no refresh button by design** — refresh
lives only in `app/pages/forecasting.py` and `app/pages/forecasting_admin.py`. The
tab's only control is the "Show More ↗" link.

## 6. Testing plan

- Re-run the 141-table sweep script after the data fix; expect non-zero
  `coreiq_av_company_overview` / `coreiq_av_time_series_daily` for LSPD, VU, CRTO.
- Headless `CompanyRepository.get_companies()` must emit `LSPD.TO` (or `LSPD`),
  `VU.PA`, `CRTO`, and `ADS.DE` must be unchanged.
- UI: `/market_data?ticker=<corrected>` must render Closing Price, Market Cap and
  a price chart, matching the ADS.DE control.
