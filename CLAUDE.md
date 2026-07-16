# CapIQReplacement — Coresight Research Portal

**App name:** Coresight Research Portal (internal repo name: CapIQReplacement)
**Stack:** Streamlit + Python 3, SQLAlchemy 2, MySQL (Azure Flexible Server), MCP servers

---

## HARD RULES — NEVER VIOLATE

- **NEVER commit or push ANYTHING, ANYWHERE** — not to Bitbucket, not to GitHub, not to any remote. Period.
- Do NOT run `git commit`. Do NOT run `git push`. Not even if asked.
- Only exception: user types the exact words "please commit" or "please push" in that specific message.
- Always work within this codebase only — NEVER touch other directories.
- New content → new file. Never edit existing GS study files.
- Do NOT touch shadow directories: `app/pages 2/`, `app/components 2/`, `app/data 2/` — they are stale duplicates, not loaded by Streamlit.

---

## Workflow Standards — MANDATORY (every task in this repo)

1. **Root cause first, then fix at the root.** Never patch a symptom. Use the
   `superpowers:systematic-debugging` skill: investigate with real DB/data evidence
   (run probes against the staging DB) until the true cause is proven, *then* fix.
2. **Always verify in the real UI with Playwright before claiming done.** Use the
   `superpowers:verification-before-completion` discipline — evidence, not assertions.
   - Launch:  `bash .claude/dev/run_local.sh`  → app on `http://localhost:8501`
     (built-in OIDC bypass as `mohdsaeedafri@coresight.com`, staging DB; **no code edits**).
   - Drive:   `.venv/bin/python .claude/dev/ui_test.py --path /<page> [--click "<btn>"] [--dialog] --shot /tmp/x.png`
   - Read the screenshot + dumped text; show the user proof.
   - NEVER add a bypass to `auth_manager.py` — the launcher uses the existing
     `APP_ENV=LOCAL + DEBUG=true + LOCAL_TEST_USER_EMAIL` path; nothing reaches STG.
3. **Use Claude skills to cut tokens & raise quality.** Prefer `code-review-graph`
   MCP / `graphify` over Grep/Glob/Read for exploration (cheaper, structural);
   `superpowers:*` for process. Keep output terse. Do NOT add new MCP servers/skills.
4. **Scope discipline.** Fix exactly what was asked; flag unrelated issues separately
   (background-task chip) instead of bundling them.
5. **Always write detailed documentation.** Every non-trivial feature or change gets a
   written design spec under `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
   (architecture, data flow, DB schema, testing plan, rollout). This OVERRIDES the
   global "never create documentation files unless asked" rule — for this repo,
   documentation is mandatory. Keep docs human-readable and current.

---

## Knowledge Graph — Use FIRST (before Grep/Glob/Read)

`code-review-graph` MCP is configured. Use it before any file reads.

| Tool | When to use |
|------|-------------|
| `semantic_search_nodes` | Find functions/classes by name or concept |
| `query_graph` | Trace callers, callees, imports |
| `get_impact_radius` | Blast radius before touching a file |
| `detect_changes` | Risk-scored code review |
| `get_review_context` | Token-efficient source snippets |

---

## Paths

```
app/main.py              # Entry point: page registration, auth bootstrap, warmups
app/pages/               # One file = one Streamlit page
app/components/          # Reusable UI: navigation, charts, tables, styles, toolbar
app/core/                # Infrastructure: auth, db, config, LLM, search, SSL
app/data/                # Models, repositories, services, screening config
app/utils/               # Logger, cache, blob, PDF, email, market data
app/assets/              # Static SVG/PNG icons
```

---

## Pages (registered in main.py)

| File | URL | Description |
|------|-----|-------------|
| `login.py` | `/login` | OIDC login; PKCE code exchange |
| `logout_bridge.py` | `/logout_bridge` | RP-initiated OIDC logout |
| `home.py` | `/home` | Landing dashboard |
| `market_data.py` | `/market_data` | Financial statements + Key Stats + estimates |
| `newsroom.py` | `/newsroom` | News + sentiment; ticker/sector filters |
| `earnings_calls.py` | `/earnings_calls` | Transcripts; TF-IDF search, PDF download |
| `live_earnings_transcript.py` | `/live_earnings_transcript` | Live transcript display |
| `earnings_calendar.py` | `/earnings_calendar` | FullCalendar earnings dates |
| `screening.py` | `/screening` | Multi-criteria financial screener |
| `company_filings.py` | `/company_filings` | SEC filing browser; XBRL + Azure blob PDF |
| `company_filings_add_files.py` | `/company_filings_add_files` | Admin: upload non-SEC filings to blob |
| `logs.py` | `/logs` | Server log viewer (admin) |
| `retailer_adding.py` | `/retailer_adding` | Retail company management (ACL-gated) |
| `forecasting.py` | `/forecasting` | Revenue forecast viewer |
| `forecasting_admin.py` | `/forecasting_admin` | Forecast admin (ACL-gated) |
| `access_management.py` | `/access_management` | Page-level ACL management |

Unregistered (admin-only, exist in pages/): `admin_iam.py`, `audit.py`, `reports.py`, `settings.py`

---

## DB Patterns

### Engines
Two SQLAlchemy engines in `app/core/database.py`:
- **Read engine** — `isolation_level="AUTOCOMMIT"`, `skip_autocommit_rollback=True` → 1 RTT (~250ms on Azure)
- **Main engine** — default isolation → use only when atomicity is required (3 RTTs)

```python
# READ (1 RTT — always prefer):
db_manager.execute_query_readonly(query, params)   # → List[Dict]
db_manager.fetch_one(query, params)                # → Optional[Dict]
db_manager.fetch_all(query, params)                # → List[Dict]

# WRITE (1 RTT — AUTOCOMMIT):
db_manager.execute_insert(query, params)           # → int (rowcount)
db_manager.execute_insert_returning_id(query, params)  # → Optional[int] (lastrowid)
db_manager.execute_update(query, params)           # → int
db_manager.execute_delete(query, params)           # → int
db_manager.execute_queries_batch([(q,p), ...])    # reuses one connection

# TRANSACTIONAL (3 RTTs — slow, only for atomicity):
with db_manager.get_session() as session:
    session.execute(text(query), params)
```

- `pool_pre_ping=False` on Azure (avoids 250ms `SELECT 1` per checkout)
- ACL: check in WHERE clause (conditional UPDATE) — never a separate SELECT
- All tables prefixed `coreiq_` (55 tables total)

### Key Tables

**Market data:** `coreiq_companies`, `coreiq_av_company_overview`, `coreiq_yf_company_overview`, `coreiq_av_financials_*`, `coreiq_yf_financials_*`, `coreiq_av_time_series_daily`, `coreiq_yf_time_series_daily`

**News:** `coreiq_av_market_news_sentiment` (FULLTEXT index `ft_av_title`), `coreiq_yf_market_news_sentiment`

**Earnings:** `coreiq_av_earnings_call_transcripts`, `coreiq_earnings_call_live_transcripts`, `coreiq_nasdaq_earnings_calendar`, `coreiq_yf_earnings_calendar`

**Filings:** `coreiq_filing_metrics_v2` (sources: `xbrl`, `calculated`, `llm`, `edgartools`), `coreiq_ir_websites`

**Auth/Roles:** `market_data_user_sessions`, `coreiq_user_roles`, `coreiq_page_access_control`, `coreiq_portal_users`

**Screening/Watchlists:** `coreiq_companies_screen_data`, `coreiq_watchlists`, `coreiq_watchlist_companies`, `coreiq_saved_criteria`

**Alerts:** `coreiq_earnings_alert_preferences`, `coreiq_earnings_alert_dedupe`, `coreiq_earnings_alert_delivery_log`

**Audit:** `coreiq_audit_logs`, `coreiq_companies_audit_log`, `coreiq_azure_blob_audit_log`

---

## Logging

All code imports from `utils.server_logger` only — standard library `logging` is forbidden.

```python
from utils.server_logger import log_structured_error, log_error, log_exception, log_timing, new_rerun_id

# At top of each page:
new_rerun_id("page_name")

# In every except block:
log_structured_error(exc, page="module_name", component="ClassName.method_name", operation="verb_noun", context="optional_details")

# Performance:
log_timing(operation, elapsed_ms, details="", level="INFO")
```

Log output: `server-logs/server-log.log`
Format: `{timestamp} | {level} | {rerun_id} | page={page} | {filename}:{funcName} | {message}`

Env flags: `APP_LOG_LEVEL=WARNING`, `APP_TIMING=1`, `AUTH_FLOW_VERBOSE=1`

---

## Auth

OIDC-only. `app/core/auth_environment.py` `is_oidc_enabled()` always returns `True`.

**Login flow:** PKCE code exchange → `IDP_TOKEN_URL` → userinfo → `AuthManager.login()` → slim cookie (`auth_session`, 7-day, `.coresight.com`) + `id_token` in `/tmp/_oidc_idtok_{hash20}.json`

**`require_auth()` check order:**
1. `st.session_state._auth_invalidated` → redirect
2. `st.session_state.authenticated + auth_data` → fast path
3. `st.context.cookies` → JSON parse → blacklist check
4. `CookieController.get()` fallback
5. Retry 4× with delays `[1.0, 2.0, 3.0, 4.0]`s → redirect to login

**Logout:** `?action=logout` → `logout_bridge.py` → OIDC `end_session_endpoint` with `id_token_hint`

**ACL:** `AccessControlManager.check_access(page_name, user_email)` → `coreiq_page_access_control`
Permission levels: `view_only(1) < upload_only(2) < edit(3) < delete_admin(4) = allow(4)`

Hardcoded admins: `mohdsaeedafri@coresight.com`, `philipmoore@coresight.com`, `shashankgupta@coresight.com`

---

## Screening

**Config:** `app/data/screening_config.py` — all constants, zero DB calls

- Statement type keys (Title Case with spaces): `'Income Statement'`, `'Balance Sheet'`, `'Cash Flow'`
- `PERIOD_TYPES = ["FY", "CQ", "FQ"]`; `DB_SCALE = 1_000_000`
- Geo/industry filters: always in-memory (0ms) — **never add DB calls here**

**Financial criterion dict:**
```python
{
    "metric_info":  {...},     # from screening_config.py
    "value1":       float,
    "value2":       float,     # only for Between operator
    "period_type":  "FY"|"CQ"|"FQ",
    "year":         int,
    "quarter":      "Q1"-"Q4",
    "display_col":  str,
}
```

---

## External Integrations

| Integration | Env Var(s) | Purpose |
|-------------|-----------|---------|
| Coresight OIDC IdP | `IDP_BASE_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI` | Auth |
| Azure MySQL | `STG_DB_HOST/PORT/NAME/USER/PASSWORD` or `PROD_DB_*` | All persistent data |
| Azure Blob Storage | `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_STORAGE_ACCOUNT_KEY`, `AZURE_BLOB_CONTAINER`, `AZURE_BLOB_PREFIX` | Filing PDFs, non-SEC docs |
| OpenAI | (runtime env var) | GPT-4o-mini for XBRL metric extraction (`app/core/llm_extractor.py`) |
| EDGAR (SEC) | `edgartools` library | XBRL filing parsing |
| SMTP | (email env vars) | Earnings alert emails |

Data from Alpha Vantage and Yahoo Finance is pre-ingested into DB — not called live.

---

## Deployment / Infrastructure (STG) — stable facts, do not re-derive

**Hosting**
- App Service (STG): **`csr-awa-data-portal-stg`** — Azure App Service **Linux**.
- Plan/SKU: **Premium0V3 (P0v3)** → **2 vCPU / 4,794 MB RAM**. No cgroup memory
  limit (`failcnt=0`); observed peak RSS ~1.3–2.8 GB — NOT OOM-killed.
- Region: **centralus** (Des Moines, Iowa). Users are in India → ~233 ms RTT per
  request; ALPN negotiates http/1.1. The app is fast; the network is the latency
  (see spec Round 11j). No app-side code change fixes that — CDN/HTTP2/region do.
- Public URL: `https://marketdata-stg.coresight.com` (behind a proxy). Default host:
  `csr-awa-data-portal-stg-gug0gxghbvgmhdc2.centralus-01.azurewebsites.net`.
- OS: **Ubuntu 24.04 LTS** container. Runtime: **Python 3.14.4**, **Streamlit on
  internal port 8000** (`WEBSITES_PORT` unset → default). `APP_ENV=staging`. Startup:
  `streamlit run --server.port=8000 --server.address=0.0.0.0 --client.showSidebarNavigation=False --ui.hideTopBar=True app/main.py`
- **Resource group: `csr-awa-rg-stg`** · **Subscription: `c749e5c1-f1f5-4d4e-87bf-3b07d7541703`**

**Filesystem (critical for caches)**
- `/home` = **persistent** Azure share (SMB `//…/volume-…`) — **500 GB, ~499 GB free**
  (storage is NOT a constraint). Survives restart AND deploy (~29 ms/write, ~2.4 ms
  cold read, ~0.1 ms warm). Use for anything that must outlive a deploy.
- Container root `/` (overlay) = 74 GB, ~48 GB free — ephemeral, replaced on deploy.
- `/tmp` = local SSD, very fast (~0.1 ms) but **WIPED on restart/deploy**.
- `wwwroot` (the repo, incl. `data/edgar_cache`) is **REPLACED on every deploy** →
  anything gitignored there is lost. STG deploys ~6×/day → cold caches all day.

**Required App Settings (set them; they are NOT in code)**
- `EDGAR_CACHE_DIR=/home/edgar_cache` — persists the 4 EDGAR disk caches across
  deploys. UNSET (current STG state) = cold EDGAR on every deploy = the "EDGAR every
  run" slowness (spec Round 13).
- `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` — keeps `/home` mounted.
- Optional `EDGAR_DISK_TTL_DAYS` (default **7**) — how long EDGAR disk cache stays
  fresh before a background re-fetch (data changes ~quarterly).

**Database**
- `csr-mysql8-flex-stg.mysql.database.azure.com:3306` — Azure MySQL **Flexible**.
- **Firewalled**: reachable from a dev Mac ONLY over the company **VPN**.
  `bash .claude/dev/run_local.sh` connects to this STG DB. If TCP to :3306 times
  out, the VPN is down — stop and reconnect before testing UI.

**How to change App Settings (never from inside the container)**
- `az` is NOT installed in the App Service SSH, and env vars set there do not persist.
  Change settings from OUTSIDE: **Azure Portal** (Configuration → Application
  settings) or **`az` on a VPN'd Mac / Cloud Shell**:
  ```bash
  az login
  az webapp config appsettings set -g csr-awa-rg-stg -n csr-awa-data-portal-stg \
    --settings EDGAR_CACHE_DIR=/home/edgar_cache WEBSITES_ENABLE_APP_SERVICE_STORAGE=true
  ```
  (Setting an app setting restarts the app.) **Claude must NOT set these itself —
  hand the user the command** (same spirit as the never-commit/never-deploy rule).

---

## Core Module Reference

| Module | Key exports |
|--------|------------|
| `app/core/config.py` | `AppConfig`, `DatabaseConfig`, `load_config()`, `config` singleton |
| `app/core/database.py` | `DatabaseManager` singleton, `db_manager` |
| `app/core/auth_manager.py` | `AuthManager`, `require_auth()`, `login_user()`, `logout_user()` |
| `app/core/access_control.py` | `AccessControlManager`, `UserRolesManager` |
| `app/core/llm_extractor.py` | GPT-4o-mini extraction; DB cache in `filing_llm_cache` |
| `app/core/search_engine.py` | `TranscriptSearchEngine` (TF-IDF) |
| `app/data/source_router.py` | `get_company_source(ticker)` → `'SEC'` or `'YFinance'`; `@st.cache_data(ttl=600)` |
| `app/data/screening_service.py` | Builds + executes screening SQL |
| `app/components/styles.py` | `COLORS`, `TYPOGRAPHY`, `SPACING`, `BORDER_RADIUS`, `hide_sidebar()` |
| `app/utils/azure_blob.py` | Azure Blob SDK wrappers |
| `app/utils/cache_manager.py` | `start_background_warmup()` |

---

## Token Management

- `/caveman` — terse output mode (saves 65-75% output tokens)
- `/compact` — compress context at 60% utilization
- `/clear` — hard reset between unrelated tasks
- Model: Haiku for quick lookups, Sonnet for features, Opus for architecture only
