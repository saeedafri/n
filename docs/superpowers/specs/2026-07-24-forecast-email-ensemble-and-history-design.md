# Forecast Refresh Email — Ensemble Value + Historical-on-Chart

**Date:** 2026-07-24
**Author:** Mohd Saeed Afri (via Claude Code)
**Requested by:** Nidhisha Mohandas → forwarded by Shashank Gupta (`.msg`: "Re: Forecast Refresh — 1 Updated · Jul 23, 2026 12:34 UTC")
**Status:** Approved design — pending implementation

---

## 1. Problem / Request

The automated **Forecast Refresh** notification email (sent by
`forecast_refresh_service.send_model_refresh_email`, rendered by
`app/data/forecast_email_report.py`) currently reports only the **best individual
model's** forecast. The business reviewer made two requests:

1. **Ensemble value.** The ensemble forecast is the team's *final output*. Include the
   ensemble forecast value in the email (tables), not just the best individual model.
2. **Historical on charts.** In the charts, show **historical actual values** alongside
   the **final ensemble forecast**, so the forecast can be reviewed/validated at a glance.

Test constraint from the requester (Mohd): **send the first test email to
`mohdsaeedafri@coresight.com` ONLY** — nobody else, no CC.

---

## 2. Evidence (real STG DB + code)

- **Ensemble is already persisted per fiscal year** in `coreiq_model_forecasts`
  (`model_key='ensemble'`, `is_ensemble=1`). Probe (BUSER.ST, `total_revenue`):

  | FY   | Ensemble | Best model (`ma_trend`) |
  |------|----------|-------------------------|
  | 2026 | $61.7M   | $62.6M |
  | 2027 | $45.8M   | $47.1M |
  | 2028 | $34.0M   | $35.5M |
  | 2029 | $25.2M   | $26.7M |
  | 2030 | $18.7M   | $20.1M |

  → **No DB write required.** Read-only surfacing of an existing column.

- **Historical actuals** are obtainable via the exact path the forecaster already uses:
  `RevenueForecastService._fetch_actual_rows(display_ticker, source)` +
  `_normalize_actual_rows(...)`, with `source = get_company_source(ticker)`. Returns
  `fiscal_year` + `total_revenue_billions` (× 1000 → millions to match `value_millions`).

- **Recipient scoping is already env-driven** in `send_model_refresh_email`
  (no code change needed for the test):
  - `FORECAST_EMAIL_RECIPIENTS` — comma list, overrides the admin/super_user DB list.
  - `FORECAST_EMAIL_CC` — set empty string to suppress the default `dataautomation@` CC.
  - `FORECAST_EMAIL_TEST_MODE` — default `"1"` = SMTP send skipped; set `"0"` to send.
  - `triggered_by` is appended as a recipient only if it contains `@`.
  - SMTP creds present in `.env` (`EMAIL_PASSWORD` set) → real send works.

- **Ticker mapping:** `fetch_ticker_detail` is called with the *display* ticker; for
  composite tickers (e.g. `BUSER.ST`) display == store, and `_fetch_actual_rows` matches
  `yf_symbol`. (The YF *non-composite* store-suffix edge is pre-existing and **out of
  scope** — flagged separately, not touched here.)

---

## 3. Design

### 3.1 Scope

**One production file changes: `app/data/forecast_email_report.py`.** The SMTP send path,
recipient logic, and scheduler are untouched. `forecast_refresh_service` already calls
`build_details` / `render_charts` / `build_report_html`, so all changes flow through
automatically for **both annual and quarterly** reports.

- **Ensemble in tables:** applies to annual **and** quarterly (ensemble rows exist in
  both `coreiq_model_forecasts` and `coreiq_model_forecasts_quarterly`).
- **Historical-on-chart:** **annual first.** For quarterly, actuals fetch returns `None`
  (guarded) so the quarterly chart renders exactly as today — **no regression**. Quarterly
  historical overlay is a documented follow-up.

### 3.2 Data layer — `fetch_ticker_detail(ticker, period_type)`

Add two reads to the returned `detail` dict (both nullable, failures are swallowed and
logged so the email is never lost):

1. `ensemble_series: List[(fy:int, value_mm:float)]` — from the table already in use:
   `SELECT fiscal_year, value_millions WHERE ticker=:t AND metric=:m AND model_key='ensemble' ORDER BY fiscal_year`.
2. `actuals: List[(fy:int, value_mm:float)]` (annual only) — new helper
   `fetch_actuals(ticker)`:
   - lazy-import `get_company_source`, `RevenueForecastService` (avoids import cycle),
   - `source = get_company_source(ticker)`; if not in (`SEC`,`YFinance`) → `None`,
   - `_fetch_actual_rows` → `_normalize_actual_rows` → `[(fiscal_year, total_revenue_billions*1000)]`,
   - sorted ascending; returns `None` on any error.

`ensemble_series` maps to the existing `series` (best model) shape so table/chart code
can consume both uniformly.

### 3.3 Tables — HTML

**Per-company forecast table** (`_forecast_table`) — ensemble as headline, best alongside:

| Fiscal Year | Ensemble Forecast | Best-Model Forecast | Best Model | Periods Ahead |
|-------------|-------------------|---------------------|------------|---------------|

- Ensemble column visually emphasised (bold + accent tint); a caption notes
  "Ensemble = final output". If `ensemble_series` is empty, fall back to today's
  best-only layout.

**Run-summary table** (`build_report_html`) — add one column **"Ensemble (next yr)"**
(first forecast year's ensemble value) after "Best Model".

**Model backtest table** (`_model_table`) — unchanged (already lists the `ensemble` row).

**Per-company meta line** — append `· Ensemble (FY{first}): $X.XM` next to the existing
best-model/MAPE summary.

### 3.4 Chart — `draw_trajectory_png` (History + Ensemble + Best + Band)

Rebuild the x-axis to span **historical actual years → forecast years** as one timeline:

- **Actuals**: solid line + markers over historical years (neutral/dark color, e.g. slate).
- **Ensemble**: highlighted accent line + markers over forecast years — the visual focus;
  starts connected to the last actual point.
- **Best model**: lighter/thinner reference line over forecast years.
- **Scenario band**: optimistic↔pessimistic fill over forecast years (as today).
- **Legend**: Actual · Ensemble · Best · Scenario range.
- **Title**: `{ticker} — actuals + ensemble forecast ($M)`.
- Y-scale spans all series (actuals + forecasts + band). Guard: if no actuals, render the
  current forecast-only chart (no regression). Pillow-only (no new deps).

The MAPE bar chart (`draw_mape_png`) is unchanged.

### 3.5 Recipient scoping for the test (no code change)

Env only, run against STG data:

```
FORECAST_EMAIL_RECIPIENTS=mohdsaeedafri@coresight.com
FORECAST_EMAIL_CC=            # empty → no dataautomation CC
FORECAST_EMAIL_TEST_MODE=0    # actually send
triggered_by=mohdsaeedafri@coresight.com
```

This guarantees a single recipient and no CC. **Nobody on the business thread is emailed.**

---

## 4. Data Flow

```
send_model_refresh_email(results, period_type)
        │
        ├─ build_details(results, period_type)
        │        └─ fetch_ticker_detail(ticker, period_type)
        │                 ├─ best series      (model_key is_best_model=1)      [existing]
        │                 ├─ ensemble_series  (model_key='ensemble')           [NEW]
        │                 ├─ models / scenarios                                [existing]
        │                 └─ actuals          (annual: _fetch_actual_rows)     [NEW, guarded]
        │
        ├─ render_charts(detail) → draw_trajectory_png(actuals+ensemble+best+band)  [CHANGED]
        │                          draw_mape_png(...)                               [unchanged]
        │
        └─ build_report_html(...) → summary(+Ensemble col) + per-co(ensemble headline) [CHANGED]
                 → MIMEMultipart(related) with cid: charts → SMTP (env-scoped recipients)
```

---

## 5. Testing / Verification Plan

Per repo rule (verification-before-completion — evidence, not assertions):

1. **Unit-ish local render.** Script in scratchpad (NOT committed to app): load `.env`
   (STG DB), build `detail = fetch_ticker_detail("BUSER.ST","annual")`, assert
   `ensemble_series` and `actuals` are non-empty with the FY26–30 values above.
2. **Visual proof.** Render `build_report_html` with charts as base64 `data:` URIs
   (`png_data_uri`) to `scratchpad/preview.html`; open in a headless browser, screenshot,
   and eyeball: ensemble column present + emphasised, summary has "Ensemble (next yr)",
   chart shows actual→ensemble→best + band with correct legend. Show the user the shot.
3. **Real test send (to Mohd only).** With the env vars in §3.5, call
   `send_model_refresh_email(triggered_by="mohdsaeedafri@coresight.com",
   results=[{"ticker":"BUSER.ST","status":"updated","company_name":"Bambuser AB","exchange":"STO"}],
   period_type="annual")`. Confirm log line `[forecast_email] SENT to=['mohdsaeedafri@coresight.com'] cc=[]`
   and function returns `True`. User confirms inbox receipt.
4. **No-regression checks.** (a) With ensemble/actuals forced empty, tables + chart fall
   back to today's layout. (b) `period_type="quarterly"` renders unchanged (actuals `None`).

**Guardrails honored:** no DB writes; no commit/push; no edits to `auth_manager.py`;
scoped to `forecast_email_report.py`; the test script is throwaway (scratchpad).

---

## 6. Rollout

- Code change is display-only and backward-compatible (graceful fallbacks). Ships with the
  normal deploy of `forecast_email_report.py`.
- The **live** automated email keeps its existing recipient logic — the env overrides in
  §3.5 are for the **test only** and are NOT baked into code. After the reviewer signs off
  on the test, remove the test env overrides so the scheduled email resumes its normal
  admin/super_user distribution.

---

## 7. Out of Scope / Follow-ups

- **Quarterly historical overlay** on the chart (annual shipped first).
- **YF non-composite store-ticker mapping** in `fetch_ticker_detail` (pre-existing; a
  `store_ticker` mismatch could drop such tickers from the email — flagged, not fixed here).
- Any change to the forecasting model math or the DB schema.
