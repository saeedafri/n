# Forecast Auto-Refresh — Production-Readiness Audit

**Date:** 2026-07-12
**Auditor:** Claude (max-effort, evidence-based)
**Scope:** Verify the twice-daily forecast automation (annual + quarterly + new companies)
is fully autonomous and safe to promote STG → PROD. Largest client; zero error tolerance.
**Method:** Read the shipped code + design spec, then prove behavior against the **real STG
DB** (`coresight_market_data_stg`) with read-only probes and one scoped live end-to-end run.
**Verdict:** **GO, conditional on ONE Azure setting** (App Service "Always On" = ON). See §6.

---

## 1. What was claimed vs. what I verified

The system: a single in-app daemon thread (`app/utils/forecast_auto_refresh.py`) started from
`app/main.py`, firing at **06:00 & 18:00 IST**, calling the *same* proven bulk-sync path the
manual "Refresh All" uses (`sync_all_eligible` / `sync_all_eligible_quarterly`), with
`force=False` so only companies whose stored forecast is stale vs. latest actuals actually run
the engine. Exactly-once across instances via an atomic `INSERT IGNORE` claim into
`coreiq_forecast_refresh_runs`.

I did **not** trust the prior "it's fixed" claim. I verified from ground truth.

---

## 2. Evidence — the automation IS running on STG

Audit table `coreiq_forecast_refresh_runs` (the job's own heartbeat), most recent first:

| run_key | started (UTC) | IST | status | A upd/err | Q upd/err |
|---|---|---|---|---|---|
| 2026-07-12:0600 | 00:34:30 | 06:00 | ok | 0/0 | 0/0 |
| 2026-07-11:1800 | 12:33:52 | 18:00 | ok | 0/0 | **1**/0 |
| 2026-07-11:0600 | 00:33:39 | 06:00 | ok | 0/0 | 0/0 |
| 2026-07-10:1800 | 12:33:25 | 18:00 | ok | 0/0 | **2**/0 |
| 2026-07-10:0600 | 00:31:59 | 06:00 | ok | 0/0 | **1**/0 |
| 2026-07-09:1800 | 12:30:08 | 18:00 | ok | 0/0 | **1**/0 |
| 2026-07-09:0600 | 00:34:47 | 06:00 | ok | 0/0 | 0/0 |
| 2026-07-08:1800 | 17:53:57 | (initial rollout) | ok | 0/0 | 0/0 |

- **Both windows fire daily.** 0600 IST = ~00:30 UTC; 1800 IST = ~12:30 UTC — timings match exactly.
- **8/8 runs `status='ok'`, zero errors.**
- The scheduler writes and finalizes its audit row every cycle (exactly-once claim working).

## 3. Evidence — the numbers are fully caught up (annual `upd=0` is CORRECT, not broken)

Integrity check: for every stored forecast, compare its `last_actual_date` to the newest actual
period actually present in the source financial tables.

- **Annual:** 339 forecast tickers checked → **0 stale** (none lag their available actuals).
- **Quarterly:** 299 forecast tickers checked → **0 stale**.

So `annual_updated=0` on every run is the *right* answer: annual actuals change once per year per
company, and none changed in these windows. Quarterly shows real incremental updates
(`upd=1,2`) on the days companies posted new quarters — proving the detection→re-forecast path
fires on genuine new data. Forecast tables: annual = 16,935 rows / 339 tickers; quarterly =
65,780 rows / 299 tickers. Freshest actuals = 2026-06-30 (current).

## 4. Evidence — the three client guarantees, proven at the mechanism level

Live, on current code, against STG (target ticker **AAP**, `force=True` scoped to one ticker):

| Guarantee | Proof | Result |
|---|---|---|
| Engine + DB upsert works today | annual `force=True` | `updated`, 100 rows; `computed_at` 07-08 → **07-12 09:12** |
| " (quarterly) | quarterly `force=True` | `updated`, 440 rows; `computed_at` advanced |
| **New annual data triggers** | `needs_update(AAP, newer_date)` | **True** |
| No needless re-run | `needs_update(AAP, same_date)` | **False** |
| **New quarterly data triggers** | quarterly `needs_update(newer)` | **True** |
| **New company builds from scratch** | `needs_update('ZZZ_NONEXISTENT', date)` | **True** (no row → re-forecast) |
| Universe includes new companies | `get_companies()` = every `coreiq_companies` row (SEC/YFinance) with ≥3 annual actuals | full universe enumerated each cycle, `exclude=∅` |
| Daemon actually spawns | `start_forecast_auto_refresh()` | thread alive w/ ENABLE=1; no-op w/ ENABLE=0 |

**New-company coverage gap check:** of 339 universe companies, exactly **1** lacks an annual
forecast — **CRWV (CoreWeave)**. Diagnosed read-only: 4 annual years but hyper-growth
($15M→$229M→$1.9B→$5.1B); the standard 10%-of-max outlier filter drops 2022–23, leaving 2
usable years < 3 required → **correctly skipped**. Not a miss — a data-sufficiency floor. It will
auto-forecast once it has ≥3 substantial years. (Same principle: quarterly needs ≥8 quarters.)

## 5. Robustness (verified in code)

- **Exactly-once / multi-instance:** `INSERT IGNORE` on PK `run_key`; only rowcount==1 proceeds.
- **Crash-proof thread:** each tick wrapped in try/except; loop sleeps regardless — thread can't die.
- **Per-ticker isolation:** one bad ticker → `status='error'`, loop continues; run never aborts.
- **Cadence isolation:** annual failure doesn't stop quarterly; row still finalized.
- **Catch-up:** if the app was down at a window, the first tick after boot claims & runs it (per local day).
- **No reporting-date gating in the scheduler** (by design) → zero-miss: re-runs are driven purely by
  real actual-data arrival via `needs_update`, so no company with new data is ever skipped.
- **Email** is isolated in try/except and gated by `FORECAST_EMAIL_TEST_MODE=0` + `EMAIL_PASSWORD`.

## 6. THE ONE PRODUCTION CONDITION (must confirm before/at deploy)

The daemon only **starts** after the first authenticated page-load following each process
(re)start — `main.py:400,633` gate on `_auth_ready_for_bg`. Once started it self-sustains
(persistent `while True` loop) for the life of the OS process. This is the *same* pattern already
running `earnings_alert_dispatcher` in production.

**Implication:** the process must stay alive across the day so the thread survives from the first
morning login through the 18:00 window. On Azure App Service this is guaranteed by
**"Always On" = ON**. Without it, an idle process can unload; if it then restarts (or a deploy
lands) in the evening and *no one logs in before 18:00*, that single evening window is missed
(recovered next morning).

**Action:** confirm **Always On = ON** on the PROD App Service (it evidently is on STG — that's
why STG fires the 18:00 window daily). This is the only thing between "works in practice" and
"guaranteed zero-touch."

## 7. Post-deploy watch-items (first 24h)

1. After the first 06:00 & 18:00 IST cycles, confirm a new `coreiq_forecast_refresh_runs` row with
   `status='ok'`.
2. Confirm the summary email arrives (verify `FORECAST_EMAIL_TEST_MODE=0` and `EMAIL_PASSWORD`
   are set in PROD) — it's the human-visible heartbeat.
3. The first *real annual* update lands only when a company next posts full-year actuals; the
   mechanism is identical to quarterly, which is already landing updates — so this is expected, not a gap.

## 8. Minor / non-blocking

- `_compute_blocked()` in `forecast_auto_refresh.py` is now dead code (reporting-date gating was
  intentionally removed from the scheduler). Harmless; optional cleanup.
- `.env` on dev boxes carries `ENABLE_FORECAST_AUTO_REFRESH=0` so local runs never fire a cycle — correct.

## 9. Bottom line

The automation is **genuinely autonomous and correct** for all three cases the client asked about
— new annual data, new quarterly data, and newly-added companies — with results pushed to the DB
and no manual work. It is **empirically running on STG** (both windows, 8/8 clean) and **fully
caught up** (0 stale of 638 ticker-forecasts). **Ship to PROD once "Always On" is confirmed
enabled** and the first-day watch-items are green.
