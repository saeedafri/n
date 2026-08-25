# Forecasting — data anomalies found in the 2026-08-19 full re-test

Every item below was found by scanning **both** sources and cross-checking them:

1. the data scientist's own input workbooks
   (`MDP-Forecasting-System/Input data/Screening_Results_{Annual,Quarterly}.xlsx`)
2. the portal's live STG database
   (`coreiq_av_financials_income_statement`, `coreiq_yf_financials_income_statement`)

Anything present in **both** is an upstream problem, not a model problem, and is
listed under "for the Data team". Anything that is a modelling choice is listed
under "for the Data scientist".

Scripts used: `scratchpad/{anomaly_scan,anomaly_diag,anomaly_verify,db_crosscheck*,yf_collisions,gap_scan}.py`.

---

## Part A — for the Data team (data defects)

### A1. Two companies merged into one ticker — CONFIRMED, highest severity

The Yahoo ingestion resolves a bare ticker without pinning the exchange, so an
unrelated Milan-listed instrument is stored under the same key as the intended
London company. `coreiq_yf_financials_income_statement` holds both `yf_symbol`
values under one `ticker`:

| ticker | symbols stored | company names stored |
|---|---|---|
| `NXT` | `NXT.L` + `NXT.MI` | **Next PLC** + **NEXT GEOSOLUTIONS** |
| `RKT` | `RKT.L` + `RKT.MI` | **Reckitt Benckiser Group plc** + **ROCKET SHARING C** |

Effect on the series (millions, reporting currency):

```
RKT   2021:2      2022:3      2022:14,453  2023:3   2023:14,607  2024:4  2024:14,169  2025:14,205
NXT   2022:54     2023:5,034  2023:82      2024:5,491  2024:305   2025:6,118  2026:6,901
```

The small numbers are the Italian instruments. This is the **root cause** of the
values that look impossible in the data scientist's workbook (`RKT 2021 = 1.9`,
`NEXT 2022/2023/2024 = 54.5 / 81.6 / 305.0`) — the workbook inherited the
contaminated series.

**Fix:** key the Yahoo ingestion on `yf_symbol` (exchange-qualified), not on the
bare ticker; then re-ingest RKT and NXT.

### A2. Ticker recycled — Anadarko Petroleum's history under ARKO Petroleum

`coreiq_companies.APC` = **ARKO Petroleum Corp.**, CIK **2080921**, industry
"Automotive Retail". The AlphaVantage revenue stored under `APC` is:

```
2004:109  2005:96  2006:88  2007:100  2008:1,083  2009:657  2010:10,984  2011:13,967
2012:13,411  2013:14,581  2014:18,470   <-- Anadarko Petroleum (old NYSE:APC)
2022:7,086  2023:6,968  2024:6,368  2025:7,643   <-- ARKO
```

`APC` 2025 (7,643) is **identical** to `ARKO` 2025 (7,643), so the portal also
carries the same business twice, under two tickers. Additionally, `APC` holds
**8 negative quarterly revenue rows** (2007–2009, worst −3,266) — Anadarko's
derivative losses netted into revenue.

**Fix:** delete the pre-2019 `APC` rows (they belong to CIK 773910), and decide
whether `APC` and `ARKO` should be one company or two.

### A3. Impossible early years — Snowflake and Somnigroup

Only two companies in the whole SEC/AlphaVantage table show the "two entities
stitched together" signature (≥2-year interior hole plus a ≥3× scale break):

```
SNOW  2014:527  2015:588  2016:571   [3-year hole]   2019:97  2020:265  2021:592 ...
SGI   ... 2014:530  2015:521  2016:533   [4-year hole]   2020:3,677  2021:4,931 ...
```

- **Snowflake** was founded 2012 and first sold its platform in 2014; its S-1
  reports **FY2019 revenue of $96.7M** as the first meaningful year. Revenue of
  $527–588M in 2014–2016 is impossible — and those values are suspiciously close
  to the genuine FY2021 figure of $592M. **2014, 2015, 2016 should be deleted.**
- **Somnigroup** is Tempur Sealy renamed. Tempur Sealy's FY2015 net sales were
  **~$3,150M** (Q4 2015 alone was $767.3M). The stored 2015:521 / 2016:533 are
  roughly 6× too small. **The pre-2020 rows do not belong to this entity.**

### A4. Negative and zero revenue in the live portal database

Not just Coty. Re-counted **2026-08-24** — the population has grown since the
first pass, so quote these numbers, not the earlier ones:

| scope | rows | companies |
|---|---|---|
| quarterly, revenue **< 0** | **19** | `APC` (9), `SMFG` (4), `ACI`, `COTY`, `JACK`, `MDLZ`, `PM`, `VYX` |
| quarterly, revenue **= 0** | **70** | `GLDG` (33), `OR` (28), `SMFG` (4), `WING` (3), `TMHC` (2) |
| annual, revenue = 0 | 19 | `GLDG` (13), `OR` (6) |

Worst values: `SMFG` −133,720 (2013-03), `PM` −15,426 (2006-12),
`ACI` −11,995 (2014-11), `MDLZ` −4,273 (2012-12), `COTY` −1,098 (2020-06),
`APC` −3,266 (2009-06), `VYX` −2,064 (2023-12), `JACK` −116 (2012-09).
89 rows in total across 12 companies.

`OR` is L'Oréal in `coreiq_companies` but the zero-revenue rows under that key are
a **US filer with the ticker `OR`** (Osisko Gold Royalties) — the same
bare-ticker collision class as A1.

Several are genuine accounting artefacts (contra-revenue, hedging losses netted
into the revenue line, and `SMFG` is a bank reporting in JPY). They are still
unusable as revenue for forecasting.

**Ask:** should these be nulled at ingestion, or kept and excluded downstream?
The portal already drops them; the data scientist's script does not (see B1).

### A5. Duplicate rows

| where | ticker | duplicated keys |
|---|---|---|
| Yahoo income statement | `TSCO` | 204 duplicated (period, line-item) |
| Yahoo income statement | `MONC` | 196 duplicated (period, line-item) |
| `coreiq_companies` | `LULU` | two rows: **lululemon athletica** (CIK 1397187) and **Lulu Retail Holdings PLC** (CIK null) |

The SEC/AlphaVantage table is clean — **0** duplicate `(ticker, period, type)` keys.

### A6. Companies with no revenue at all

3 of the 46 Yahoo-sourced companies have zero usable annual revenue rows:
`LULU` (Lulu Retail, ADX), `QUIZ` (LSE), `TOS` (Toshiba, LSE).

Across the full 414-company universe, **every** company has annual revenue in at
least one source, so overall coverage is fine.

### A7. `exchange_acronym` still holds exchange names, not Yahoo suffixes

Mostly cleared. As of **2026-08-24 only one row remains**:

| ticker | stored value | should be |
|---|---|---|
| `LULU` | `ADX` | `.AD` for Abu Dhabi |

(`CMRC`, `ZBRA`, `ORCL` have been fixed since the first pass.) A bad acronym
builds an invalid composite symbol that Yahoo cannot resolve, so ingestion for
that company silently returns nothing.

### A8. Universe composition — one bank in a retail forecast set

The quarterly workbook contains **Cullen/Frost Bankers, Inc. (NYSE:CFR)** —
industry "Banks & Financial Services". The annual workbook has
**Compagnie Financière Richemont SA (SWX:CFR)** in the same slot. The ticker
`CFR` was almost certainly resolved to the US bank instead of Richemont.

Also in the annual workbook: `HP (NYSE:HPQ)` and `HP Inc. (NYSE:HPQ)` are the
same company entered twice (identical 11-point series), as are
`Shopify (NASDAQ:SHOP)` and `Shopify (NYSE:SHOP)`.

Nine companies are listed in the annual workbook with **no revenue data at all**:
`GES`, `JWN`, `SKX`, `SPTN`, `WBA`, `IMKT.A`, `QVCPQ`, `CTA-PA`, `TOS`.

---

## Part B — for the Data scientist (modelling decisions)

### B1. Coty's negative quarter — the original question, now with more context

`Coty Inc. (NYSE:COTY)` reports **−1,098.0** for the quarter ending 2020-06-30.
The value is in **both** the workbook and the portal database, so it is a real
stored figure, not a workbook typo.

- The reference script models it as-is; the app discards it as invalid.
- Dropping it moves Coty's structural break from Q1 2020 to Q2 2020 and improves
  backtest error from **6.8% → 4.4%**.
- It is the only company where our numbers differ from the reference report.

Note this is not an isolated case — A4 shows 18 negative quarterly revenue values
across 7 companies. A shared rule is needed, not a Coty-specific patch.

### B2. The quarterly model compounds without a ceiling — NEW, most important

The plausibility flag is advisory: it labels a forecast but does not bound it.
Live values in the store today:

| ticker | period | first forecast | peak forecast | multiple |
|---|---|---|---|---|
| `SNDK` | quarterly | 12,977 | **65,040,060** | ×5,012 |
| `CRWV` | quarterly | 3,311 | 272,279 | ×82 |
| `PLTR` | quarterly | 2,203 | 44,278 | ×20 |
| `APH` | quarterly | 9,931 | 146,650 | ×15 |

SanDisk's quarterly forecast reaches **$65 trillion**. The flag fires correctly
(`implied_cagr_492%`, `long_horizon_extrapolation_20q_from_11pts_7254.9x`) and the
UI shows the red banner — but the number is still rendered on the chart and in
the table.

The input is not bad data: SanDisk genuinely grew 175.3% in FY2026 (Q4 alone
$8.97B vs ~$1.9B a year earlier) on the AI/datacenter memory cycle. The annual
model handles it (`plausible = True`); only the 20-quarter compounding explodes.

**Ask:** should the quarterly engine damp or cap compounded growth over long
horizons — e.g. decay the growth rate toward a sector mean beyond 4–8 quarters —
rather than relying on a flag alone?

### B3. The model's structural breaks match real corporate events — confirm intent

The break detector independently found every mixed-basis series we identified by
hand:

| ticker | break detected | real-world cause |
|---|---|---|
| `BBWI` | 2020 | L Brands spun off Victoria's Secret |
| `DLTR` | 2023 | Family Dollar divested; continuing-ops restatement |
| `COTY` | 2020 | the negative quarter above |
| `APC` | 2015 | Anadarko/ARKO ticker recycling (A2) |
| `UL` | Q4 2019 | semi-annual reporting basis |

For these the model **excludes pre-break history**, so `DLTR` drops to `LIMITED`
tier on 3 usable years. That is defensible, but it means a big company can end up
on a thin series after a restatement.

**Ask:** is excluding pre-break history the intended behaviour when the break is
a reporting-basis change rather than a business change?

### B4. Annual and quarterly workbooks disagree on fiscal-year labels

Comparing the sum of four quarters against the reported annual figure for 685
company-years:

| result | company-years |
|---|---|
| matches the same year | 571 |
| matches the annual figure of **year + 1** | 78 |
| matches **year − 1** | 13 |
| matches **year − 2** | 2 |
| matches **no** nearby year | 21 |

The ±1 bucket is a labelling convention difference: for January-fiscal-year-end
retailers, Q4 carries the calendar year it ends in while Q1–Q3 carry the fiscal
year. Macy's, for example, runs `… Q1 2026, Q4 2026` with no Q2/Q3 2026 — those
quarters simply have not been reported yet (today is 2026-08-19). **This is not a
data hole**, but the two files should agree on one convention.

The 21 true mismatches are worth a look, especially:

```
UNILEVER  2019   Q-sum 149,458  vs annual  51,980   (+187%)   6 years affected
ACI       2015   Q-sum  84,410  vs annual  27,199   (+210%)
BBWI      2020   Q-sum  10,382  vs annual   5,405    (+92%)
DLTR      2023   Q-sum  29,685  vs annual  15,412    (+93%)
HLN       2023   Q-sum  17,086  vs annual  11,302    (+51%)
```

Unilever and Haleon report semi-annually; their "quarters" are probably half-year
figures counted four times.

### B5. Semi-annual reporters in a quarterly universe

21 of the 98 companies in the quarterly workbook have **no quarterly data at
all** — LVMH, Nestlé, L'Oréal, Kering, Hermès, Burberry, M&S, Sainsbury, Danone,
Carrefour, Woolworths and others. A further 10 have fewer than 8 quarters
(Fast Retailing and Seven & I have **two**).

Most of these are half-yearly reporters, so this is expected rather than broken —
but it raises the question of why they are in a quarterly forecast set. The portal
shows an explicit "no quarterly revenue history" notice for these (43 companies),
which is correct behaviour.

### B6. Still open from 2026-08-15

Questions 3–9 of `2026-08-15-questions-for-data-scientist.md` are unchanged:
annual/quarterly ordering difference, scenario columns, stricter `needs_review`,
thresholds by sector, rolling vs fixed horizon, refresh cadence, ensemble
weighting.

---

## Corrections to earlier reports

- **SanDisk (`SNDK`) revenue is CORRECT.** FY2026 = $20.25B (+175.3%) is verified
  against the company's Q4 FY26 release and stockanalysis.com. Earlier in this
  workstream it was reported as suspicious; it is not. Nothing to raise with the
  data team about SNDK's actuals — only the quarterly extrapolation (B2).
- **Currency labelling is not a defect.** `SMFG` correctly renders
  "Millions of JPY"; the page follows each company's reported currency.
- **`exchange_acronym` corruption is smaller than previously recorded** — 4 rows
  today, not ~34 (A7).

---

## Full re-test evidence (2026-08-19)

| check | result |
|---|---|
| `pytest tests/` | **55 passed** |
| Parity vs the reference report — annual | **347/347** companies, tier + best method + MAPE + every forecast value |
| Parity vs the reference report — quarterly | **97/98** (Coty excluded by design, asserted separately) |
| Backtest detail + seasonal indices parity | pass |
| UI sweep, `/forecasting`, **Annual** | **396/396 ok**, 0 problems, 33 review banners, 16.3 min |
| UI sweep, `/forecasting`, **Quarterly** | 352 ok + **43 correct "no quarterly history" notices** + 1 transient timeout, 25 review banners, 17.8 min |
| Quarterly email structure (BIRK) | 20 ensemble points, **20 unique keys, 0 lost**, 19 actual quarters, 11 models, **0 duplicates** |
| Annual email structure (M, SNDK) | unchanged — 5 periods, 5 unique keys, 10 models, 0 duplicates |
| Quarterly mail kill switch | quarterly sends on unset/`1`/`on`; suppressed on `0`/`off`/`false`; **annual never affected** |

### The one quarterly sweep "timeout" was not a defect

`FRSH` exceeded the 45 s page budget once. Re-measured in isolation:

```
quarterly M       13,090 ms   <- first call in the process (DB connect + engine import)
quarterly FRSH     1,437 ms
quarterly FRSH         0 ms   (cached)
quarterly AAPL     1,447 ms
```

The cost is process warm-up, not the ticker. `FRSH` is present in the quarterly
store and its page renders in full (all 7 models, ensemble, three scenarios) —
verified with a direct UI load.

### Forecast store coverage

| store | in store | missing |
|---|---|---|
| `coreiq_model_forecasts` (annual) | 396 / 396 | 0 |
| `coreiq_model_forecasts_quarterly` | 353 / 396 | 43 |

The 43 are exactly the composite-ticker international names that have no
quarterly history at all (`MC.PA`, `NESN.SW`, `9983.T`, …) — the same 43 the UI
sweep reports as `no_data`. Consistent, not a gap.
