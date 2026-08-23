# Forecast refresh notification email — global hold

**Date:** 2026-08-19
**Area:** `app/data/forecast_refresh_service.py::send_model_refresh_email`
**Status:** held by default (code only, not deployed)

---

## Decision

All forecast refresh notification mail is suppressed — **annual included**, not just
quarterly. The forecast data still has open issues we are working through one at a time
(duplicate ticker keys from retired `exchange_acronym` values, companies stuck "On hold"
for want of a reporting date). Emailing admins a report built on data we already know is
wrong spreads the confusion faster than we can fix it.

Refresh runs themselves are untouched. Models still compute, the popup still updates,
`coreiq_model_forecasts` still gets written. Only the notification is withheld.

## Implementation

One gate at the top of `send_model_refresh_email()`, the single choke point every caller
routes through:

| Caller | Path |
|---|---|
| "Refresh Now" (per ticker) | `pages/forecasting.py:3112` |
| "Refresh All" | `pages/forecasting.py:3131` |
| Scheduled worker (06:00 / 18:00) | `utils/forecast_auto_refresh.py:284` |

```python
if os.getenv("FORECAST_REFRESH_EMAIL", "0").strip().lower() not in ("1", "true", "yes", "on"):
    log_info("[forecast_email] SUPPRESSED: all forecast refresh mail is on hold ...")
    return False
```

Default unset = held, so the hold needs **no** App Setting to take effect — it ships with
the deploy. The gate runs before SMTP config is read, so nothing connects to Office365.

Every suppressed send logs one line with `period_type`, `triggered_by` and the result
count, so the hold is visible in `server-log.log` rather than silent.

## Resuming

```bash
az webapp config appsettings set -g csr-awa-rg-stg -n csr-awa-data-portal-stg \
  --settings FORECAST_REFRESH_EMAIL=1
```

(Setting it restarts the app.) Two narrower switches remain below the global one:

- `FORECAST_AUTO_REFRESH_EMAIL_QUARTERLY=0` — holds quarterly only, once the global hold
  is lifted. Added 2026-08-16 when quarterly series were keyed by `fiscal_year` alone and
  collapsed 3-4 quarters onto one x-value (14 of 20 points lost for FLWS); fixed
  2026-08-19 by keying periods on `(fiscal_year, quarter)`.
- `FORECAST_AUTO_REFRESH_EMAIL=0` — stops the scheduled worker from emailing, without
  affecting the two UI buttons.

## Verification

`send_model_refresh_email()` called directly with `EMAIL_PASSWORD` deliberately set, so a
pass-through would have reached SMTP:

```
HELD   annual     -> False, logged "all forecast refresh mail is on hold"
HELD   quarterly  -> False, same
RESUME quarterly  -> passes global gate, still held by FORECAST_AUTO_REFRESH_EMAIL_QUARTERLY=0
RESUME annual     -> passes both gates, reaches SMTP setup
```

No other forecast email path exists — `smtplib` appears in `earnings_alert_email.py`,
`retailer_adding.py` and `company_filings_add_files.py`, all unrelated features, all
untouched.
