# STG / PROD Azure App Settings — what to set, why, and the risks

**Date:** 2026-07-18
**Applies to:** `csr-awa-data-portal-stg` (STG) and `csr-awa-marketdata-pro` (PROD) — same values on both.
**Where IT sets them:** Portal → the Web App → **Settings → Environment variables → App settings → + Add** → **Apply** (Apply restarts the app).
**Precedence note:** `load_dotenv()` runs with `override=False`, so **Azure App Settings win over the deployed `.env`**. Set these as App Settings, and make sure no conflicting `.env` value is relied on.

---

## A. Email — REQUIRED for the forecast auto-refresh mail to send

| Setting | Value | Problem it fixes | How it fixes it | Risk / regression after setting |
|---------|-------|------------------|-----------------|---------------------------------|
| `FORECAST_EMAIL_TEST_MODE` | `0` | Emails never sent — the send path defaults to test/dry-run mode and only logs "SMTP send skipped". | Flips the code's live-send gate. With `EMAIL_PASSWORD` already present, real SMTP send is enabled. | Emails now leave the building. Only sends on a run that **actually updates ≥1 company** (only-on-update guard), so no "0 updated" spam. Recipients get a real email. |
| `FORECAST_EMAIL_RECIPIENTS` | `mohdsaeedafri@coresight.com,philipmoore@coresight.com,nidhishamohandas@coresight.com,shashankgupta@coresight.com,risthavilinadsa@coresight.com` | Ristha isn't in `coreiq_user_roles`, so the DB-derived recipient list would miss her. | Explicit override list — used verbatim, no DB write needed. | If the list is wrong/typo'd, wrong people get mail. Comma-separated, must all contain `@`. Leaving it unset falls back to DB admins (which omits Ristha). |

Already correct (do **not** change): `EMAIL_PASSWORD` (present), `FROM_EMAIL=dataautomation@coresight.com`, `SMTP_SERVER=smtp.office365.com`, `SMTP_PORT=587`, `FORECAST_AUTO_REFRESH_EMAIL=1`.

---

## B. RAM / performance — recommended, low risk

| Setting | Value | Problem it fixes | How it fixes it | Risk / regression |
|---------|-------|------------------|-----------------|-------------------|
| `MALLOC_ARENA_MAX` | `2` | glibc keeps too many memory arenas → retained (unused) RAM inflates RSS (observed ~675 MB retained). | Caps glibc arenas to 2. Must be present at **process start** (App Setting), not just the in-code `setdefault` which runs too late to be reliable. | None functional. Very slightly more allocator contention under extreme threading — negligible for this app. Observed: retained RAM 675 MB → ~150 MB. |
| `WEBSITES_CONTAINER_START_TIME_LIMIT` | `600` | Default 230 s. App runs warmups + cold SMB cache reads on boot; if boot exceeds 230 s the platform **restarts the container** (restart loop). | Raises the boot ceiling to 600 s (max 1800). | None. Only affects how long the platform waits before declaring boot failed. |
| `PYTHONUNBUFFERED` | `1` | Python buffers stdout → Log Stream lags when diagnosing. | Forces unbuffered stdout. | None. Marginally more I/O syscalls; irrelevant here. |

---

## C. Cache persistence — IT already confirmed these are set

| Setting | Value | Problem it fixes | How it fixes it | Risk / regression |
|---------|-------|------------------|-----------------|-------------------|
| `EDGAR_CACHE_DIR` | `/home/edgar_cache` | EDGAR disk cache lived in `wwwroot`, wiped on every deploy → cold EDGAR on every deploy ("EDGAR every run" slowness). | Points the cache at the persistent `/home` share (survives deploys/restarts). | Needs `/home` mounted (see below). Uses `/home` storage (ample: ~499 GB free). |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE` | `true` | `/home` may not be mounted → the persistent caches vanish. | Keeps the `/home` Azure Files share mounted. | None. Required for the cache settings above. |

---

## D. Evaluate later (NOT now — needs a check first)

- **`WEBSITE_RUN_FROM_PACKAGE=1`** — ~30% faster, atomic deploys, read-only `wwwroot`. Safe only after confirming **nothing writes to `wwwroot` at runtime** (our caches are on `/home`, so likely fine). IT to verify before enabling.
- **Session affinity = ON** — required **only if** we scale to 2+ instances (Streamlit keeps session state + websocket in one worker). Do not scale out without it.
- **Azure Front Door / CDN** — the real fix for India→centralus ~233 ms latency (edge POP in India). Architecture decision, biggest perceived-speed win.

---

## Rollout order (per environment)
1. Deploy the code first (settings without the matching code = old behavior).
2. Set section A + B settings, Apply (restarts app).
3. Confirm: watch the next 06:00/18:00 IST forecast run — an update sends the rich email to the 5 recipients.
4. Repeat on PROD once STG looks good.
