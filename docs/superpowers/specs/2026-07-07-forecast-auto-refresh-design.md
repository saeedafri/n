# Forecast Auto-Refresh — In-App Twice-Daily Scheduler

**Date:** 2026-07-07
**Owner:** Forecasting (largest client — zero tolerance for errors)
**New file:** `app/utils/forecast_auto_refresh.py`
**Touched:** `app/main.py` (boot wiring only)
**New DB object:** `coreiq_forecast_refresh_runs` (tiny coordination/audit table, `CREATE TABLE IF NOT EXISTS`)

---

## 1. Problem

The forecasting page has a manual **Refresh Data** dialog with per-ticker **Refresh Now**
and **Refresh All** buttons, for both **annual** (`coreiq_model_forecasts`) and
**quarterly** (`coreiq_model_forecasts_quarterly`) models. An operator must open the
page and click. The client wants this to happen **automatically, driven by each
company's reporting date**, with **no manual intervention**, and it must be
**very lightweight (minimal RAM)** and rock-solid.

## 2. Non-negotiable constraints

- **Reuse the exact proven refresh path.** No new forecasting/model logic. The
  scheduler is a thin wrapper around the *same* functions "Refresh All" already calls.
  This means **zero new risk to the numbers**.
- **Minimal RAM.** One sleeping daemon thread between runs (~KB). During a run the
  engine processes **one ticker at a time** (existing loop) — peak RAM is one ticker's
  small DataFrame, not the whole universe.
- **Exactly-once**, even if Azure App Service runs multiple app instances/processes.
- **`force=False`** — the job can *never* overwrite a fresh forecast; it only does real
  work for tickers whose stored forecast is stale vs. latest actuals.
- **Off by default.** Gated by an env flag; enabled only after end-to-end verification.
- **No UI change.** The manual dialog stays exactly as-is.

## 3. Chosen approach

**In-app background daemon thread** (the same pattern already in production for
`earnings_alert_dispatcher.start_background_dispatch_thread`), scheduled to fire at
**two configured local times per day** (default 06:00 & 18:00 IST), sending an **email
summary each run**.

Rejected alternatives:
- *OS cron / Azure WebJob / APScheduler* — extra moving parts, extra process/dependency,
  more RAM, separate deploy surface. The in-app daemon reuses the exact proven pattern
  already shipped in this repo and adds **zero dependencies**.
- *Run inline on page render* (like the earnings-alert tick) — rejected: a full cycle
  takes minutes and must **never block a user's page**. The forecast job is
  thread-only.

## 4. Architecture

### 4.1 Components (single new module)

| Unit | Responsibility |
|------|----------------|
| `_ensure_runs_table()` | Idempotent `CREATE TABLE IF NOT EXISTS coreiq_forecast_refresh_runs`. |
| `_claim_window(run_key)` | Atomic `INSERT IGNORE` of `run_key` → `True` iff *this* process claimed the window (mirrors `try_insert_delivery`). Cross-instance exactly-once. |
| `_finalize_window(run_key, …)` | `UPDATE` the claimed row with status + counts + finished_at. |
| `_compute_blocked(period_type)` | From `get_refresh_table_data(period_type)`, the set of tickers whose reporting date is not yet identified (`_has_report_date` is false) — identical rule to the UI's `Refresh All`. |
| `_run_period(period_type, sync_all_fn, force, extra_exclude)` | Run one cadence: exclude = blocked ∪ extra_exclude; call `sync_all_fn(force=force, exclude_tickers=exclude)`; email summary; return counts. |
| `_run_cycle(force, extra_exclude)` | Ensure table → run annual → run quarterly → `clear_revenue_forecast_caches()` → return combined summary. |
| `_due_window_key(now_local)` | Given configured times, return the `YYYY-MM-DD:HHMM` key of the **most recent past-due window today**, else `None`. |
| `run_forecast_auto_refresh_tick(force=False)` | Resolve due window → claim → `_run_cycle` → finalize. In-process re-entrancy guard (`threading.Lock` + running flag). |
| `start_forecast_auto_refresh()` | Spawn ONE daemon thread that loops: `run_forecast_auto_refresh_tick()` then `sleep(interval)`. |

### 4.2 Data flow (one cycle)

```
daemon wake → _due_window_key(now) = "2026-07-07:0600"
  → _claim_window(key)            # INSERT IGNORE; rc==1 ⇒ we own this window
      claimed? no  → return (another instance/earlier tick already ran it)
      claimed? yes ↓
  → _run_cycle(force=False):
      annual:    blocked = _compute_blocked("annual")
                 results_a = sync_all_eligible(force=False, exclude_tickers=blocked)
                 send_model_refresh_email(triggered_by="auto-scheduler",
                                          results=results_a, period_type="annual")
      quarterly: blocked_q = _compute_blocked("quarterly")
                 results_q = sync_all_eligible_quarterly(force=False, exclude_tickers=blocked_q)
                 send_model_refresh_email(..., results=results_q, period_type="quarterly")
      clear_revenue_forecast_caches()
  → _finalize_window(key, status="ok", annual/quarterly updated+errors counts)
  → log_timing("FORECAST_AUTO_REFRESH_CYCLE", elapsed_ms, …)
```

Because each ticker's `sync_*_forecast_for_ticker` internally checks
`needs_update(store_ticker, last_actual_date)` under `force=False`, only tickers whose
**latest actuals are newer than the stored forecast** (i.e. a company that just
reported) actually run the engine. Every other ticker returns `up_to_date` cheaply.
This is precisely "reporting-date-driven" refresh — annual keyed on the Q4/full-year
actual, quarterly keyed on each individual quarter's actual.

**No reporting-date gating in the scheduler (zero-miss).** An earlier draft excluded
tickers whose *calendar* reporting date was unresolved (mirroring the manual "Refresh
All"). That was **removed** from the scheduler: the re-run is already driven by real
actual-data arrival via `needs_update`, so excluding on an unresolved calendar date
would *skip a company that genuinely has new data*. The scheduler now refreshes **every
eligible company that has newer actuals — no exclusions.** (The manual UI keeps its own
"on hold" behavior; that's a separate concern.) `extra_exclude` remains only for scoped
testing.

### 4.3 Catch-up semantics

`_due_window_key` returns the most recent past-due window of the **current local day**.
If the app was down at 06:00 and boots at 09:00, the 06:00 window is still unclaimed, so
the first tick claims and runs it (catch-up). The DB claim guarantees it runs **once**.
Windows are per-local-day, so yesterday's windows are never re-run.

### 4.4 DB object

```sql
CREATE TABLE IF NOT EXISTS coreiq_forecast_refresh_runs (
    run_key            VARCHAR(64)  NOT NULL,        -- 'YYYY-MM-DD:HHMM'
    started_at         DATETIME     NOT NULL,
    finished_at        DATETIME     NULL,
    status             VARCHAR(32)  NOT NULL,        -- running | ok | error
    annual_updated     INT          NOT NULL DEFAULT 0,
    annual_errors      INT          NOT NULL DEFAULT 0,
    quarterly_updated  INT          NOT NULL DEFAULT 0,
    quarterly_errors   INT          NOT NULL DEFAULT 0,
    detail             VARCHAR(500)  NULL,
    PRIMARY KEY (run_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`run_key` PK = the lock. `INSERT IGNORE` is the atomic claim (rowcount 1 ⇒ owner).
Row doubles as an audit trail of every cycle. All writes via the AUTOCOMMIT read/write
helpers (`execute_insert`, `execute_update`) — 1 RTT each; never a transaction.

## 5. Configuration (env, safe defaults)

| Var | Default | Meaning |
|-----|---------|---------|
| `ENABLE_FORECAST_AUTO_REFRESH` | `1` (ON) | Master switch. **ON by default** (unset → enabled), so a deploy activates it without an Azure setting. Disable explicitly with `=0`. |
| `FORECAST_AUTO_REFRESH_TIMES` | `06:00,18:00` | Comma-separated local `HH:MM` run times (twice daily). |
| `FORECAST_AUTO_REFRESH_TZ` | `Asia/Kolkata` | IANA tz for the run times. |
| `FORECAST_AUTO_REFRESH_SLEEP_SEC` | `300` | Daemon poll interval (clamped ≥ 60). |
| `FORECAST_AUTO_REFRESH_EMAIL` | `1` | Send the per-run summary email (uses existing `send_model_refresh_email`). |

Email actually leaves the building only when the existing `FORECAST_EMAIL_TEST_MODE=0`
**and** `EMAIL_PASSWORD` is set — same gate the manual dialog already uses. Recipients =
existing admin + super_user list, Cc `dataautomation@coresight.com`. `triggered_by` is
the literal `"auto-scheduler"`.

## 6. Error handling

- Every ticker is already wrapped in `try/except` inside `sync_*_for_ticker`; one bad
  ticker yields `status="error"` and the loop continues — the run never aborts.
- `_run_cycle` wraps annual and quarterly independently; a failure in one still lets the
  other run and still finalizes the row (`status="error"`, `detail=<reason>`).
- The daemon loop wraps each tick in `try/except` and sleeps regardless, so the thread
  can never die.
- If `_claim_window` loses the race (another instance), the tick returns immediately — no
  work, no email, no error.

## 7. RAM / cost profile

- **Idle:** one daemon thread blocked in `sleep` (~KB). No timers, no dependency.
- **Per cycle:** the existing per-ticker loop — peak memory ≈ one ticker's deduped
  revenue DataFrame + one engine instance (small). With `force=False`, engine work runs
  only for the handful of tickers that just reported; the rest are cheap `up_to_date`
  checks (~1–2 read queries each). Trade-off accepted: correctness (reuse the proven
  path) over shaving those cheap per-ticker existence checks. Runs twice daily.

## 8. Testing plan (must pass before "done")

Headless against the real STG DB (`APP_ENV=staging`, services run in bare mode):

1. **Table create** — `_ensure_runs_table()` creates `coreiq_forecast_refresh_runs`; second call is a no-op.
2. **Atomic claim (exactly-once)** — `_claim_window(k)` → `True`; immediate `_claim_window(k)` → `False`.
3. **Concurrency** — two threads claim the same key simultaneously → exactly one `True`.
4. **Blocked compute** — `_compute_blocked("annual")` / `("quarterly")` return sets (on-hold tickers), matching the UI rule.
5. **Scoped end-to-end cycle** — `_run_cycle(force=True, extra_exclude=<all-but-3>)` runs the real annual + quarterly path for 3 tickers, clears caches, attempts email (test-mode logs), and returns correct counts.
6. **Run-row finalized** — after a `run_forecast_auto_refresh_tick(force=True)`, the `coreiq_forecast_refresh_runs` row exists with `status='ok'` and populated counts.
7. **Idempotent window** — a second `run_forecast_auto_refresh_tick()` in the same window claims nothing and does no work.
8. **Daemon smoke** — `start_forecast_auto_refresh()` starts a thread; env-off ⇒ no thread.

## 9. Rollout

1. Ship code — **ON by default** (no Azure setting needed to activate). To keep it
   dormant on any environment, set `ENABLE_FORECAST_AUTO_REFRESH=0` there.
2. After deploy, watch the first 06:00/18:00 IST cycle + the `coreiq_forecast_refresh_runs`
   row + logs + email.
3. Email live-send stays gated by `FORECAST_EMAIL_TEST_MODE=0` + `EMAIL_PASSWORD`
   (same gate the manual dialog uses). Recipients = admin + super_user (Saeed, Shashank,
   Philip, Nidhisha) + Cc dataautomation@coresight.com. The `"auto-scheduler"` trigger
   label is never added as a recipient (guarded on `@`).
4. Local dev boxes carry `ENABLE_FORECAST_AUTO_REFRESH=0` in `.env` so the running dev
   app never fires a full cycle against STG.
