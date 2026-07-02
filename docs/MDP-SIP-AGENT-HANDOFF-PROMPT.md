# MDP Performance Optimization — Agent Handoff Prompt

You previously optimized the **SIP (Store Intelligence Platform)**. Apply the same discipline to **MDP (Coresight Research Portal / CapIQReplacement)** — same observability standards, same cold-load methodology, same byte-identical optimization rules.

---

## Your mission

1. Read SIP optimization docs and the SIP enhancement session (below).
2. Read all MDP investigation docs produced in this effort.
3. **Audit** whether MDP has SIP-equivalent debugging/observability (gaps list).
4. **Run true cold-load tests locally** (not warm/cache-hit numbers).
5. Produce a gap report + prioritized fix plan. **Do not claim done without cold test evidence.**

**Performance targets (user SLA):**

- **Cold:** every page/tab/filter **< 1 second** full data loaded
- **Warm:** **milliseconds** (post-cache reruns)

**Current reality (documented):** **0/24 interactions** meet cold <1s; median cold **~12.7s**; worst **23.5s** (screening Show Results).

---

## Repositories

| Project | Path |
|---------|------|
| **SIP (reference — what you built)** | `/Users/mohdsaeedafri/Documents/Documents/Code-Base/Modular-Code` |
| **MDP (target — optimize this)** | `/Users/mohdsaeedafri/Documents/Documents/Code-Base/Real/CapIQReplacement` |

---

## Mandatory reading — SIP (read ALL, in order)

### Performance docs

```
Modular-Code/docs/performance/README.md
Modular-Code/docs/performance/01-active-page.md
Modular-Code/docs/performance/02-opening-page.md
Modular-Code/docs/performance/03-closing-page.md
Modular-Code/docs/performance/04-net-page.md
Modular-Code/docs/performance/05-concurrency-and-scaling.md
Modular-Code/docs/performance/06-env-flags-and-logs.md
```

### Enhancement journey (critical)

```
Modular-Code/docs/performance/enhancement-journey/00-README.md
Modular-Code/docs/performance/enhancement-journey/01-glossary-plain-english.md
Modular-Code/docs/performance/enhancement-journey/02-problems-findings-fixes.md
Modular-Code/docs/performance/enhancement-journey/03-flow-diagrams.md
Modular-Code/docs/performance/enhancement-journey/04-logging-and-how-to-verify.md
Modular-Code/docs/performance/enhancement-journey/05-render-speed-research.md
Modular-Code/docs/performance/enhancement-journey/SIP-Enhancement-FULL.md
```

### SIP implementation (source of truth)

```
Modular-Code/server_logger.py
Modular-Code/sip_components/base_page.py
Modular-Code/sip_components/mem_opt.py
Modular-Code/sip_components/materialize.py
Modular-Code/sip_components/active_data.py
Modular-Code/sip_components/shared_data.py
Modular-Code/sip_components/filter_manager.py
```

### SIP enhancement session (your prior work)

**Read this Claude Code session where you solved SIP end-to-end:**

https://claude.ai/code/session_01Te2rqrDGgg561Qi4rRbjFf

Reconcile session decisions with MDP docs. If the session is unavailable, use SIP docs + source above.

---

## Mandatory reading — MDP (read ALL)

### Project rules

```
CapIQReplacement/CLAUDE.md
CapIQReplacement/AGENTS.md
```

### Investigation & audit reports (THIS effort — primary MDP context)

```
CapIQReplacement/docs/superpowers/specs/2026-07-02-mdp-performance-investigation-report.md   ← MASTER REPORT
CapIQReplacement/docs/superpowers/specs/2026-07-02-mdp-e2e-perf-audit.md
CapIQReplacement/docs/superpowers/specs/2026-07-02-mdp-sip-observability-design.md
CapIQReplacement/docs/superpowers/specs/2026-07-02-mdp-db-optimization-design.md
CapIQReplacement/docs/MDP-STG-PERF-DIAGNOSTICS.md
CapIQReplacement/docs/SIP_server_debug_commands.md
CapIQReplacement/docs/EARNINGS_CALENDAR_OPTIMIZATION_NOTES.md
```

### MDP implementation already ported from SIP

```
CapIQReplacement/app/utils/server_logger.py
CapIQReplacement/app/components/base_page.py
CapIQReplacement/app/utils/mem_opt.py
CapIQReplacement/app/utils/materialize.py
CapIQReplacement/app/utils/cache_manager.py
CapIQReplacement/.claude/dev/cold_load_benchmark.py
CapIQReplacement/.claude/dev/e2e_perf_audit.py
CapIQReplacement/.claude/dev/run_local.sh
CapIQReplacement/.claude/dev/ui_test.py
```

---

## Phase 1 — Observability audit (before any perf fix)

Compare SIP vs MDP. Produce a checklist:

| SIP capability | SIP file/tag | MDP status | Gap action |
|----------------|--------------|------------|------------|
| `[BOOT]` + defaults | server_logger | ? | |
| `[HEARTBEAT]` + RAM | server_logger | ? | |
| `[RAM_CENSUS]` | server_logger | ? | |
| `[RERUN]` + `[RERUN_TRIGGER]` | base_page | ? | |
| `[CLICK->RENDER]` | log_render_complete | ? | |
| `user_analytics.log` | log_user_event | ? | |
| `[MAT]` disk materialize | materialize.py | ? | |
| `[MEM_OPT]` / `[AUDIT]` | mem_opt.py | ? | |
| `[DATA_VOLUME]` large fetches | server_logger | ? | |
| Per-page `new_rerun_id` | all pages | ? | |
| Per-tab timing | market_data TAB_* | ? | |
| QueueHandler non-blocking logs | server_logger | ? | |
| `restarts.log` | server_logger | ? | |
| `/logs` live tail + slow-ops | pages/logs.py | ? | |

**New MDP tags already added (verify they fire on STG/local):**

- `[TIMING] DB_get_companies_rows`
- `[TIMING] NEWSROOM_CHUNK_FETCH`
- `[SHOW_RESULTS]` / `[TIMING] SHOW_RESULTS_RERUN_TOTAL`
- `[TIMING] TAB_{Income_Statement|Balance_Sheet|...}`
- `[TIMING] FILINGS_LOAD` / `FILINGS_PREFETCH`
- `[DATA_VOLUME]` (rows ≥ 1000)

**Confirm every registered page has:** `new_rerun_id` first line, `log_structured_error` in except blocks, `log_render_complete` on heavy pages.

---

## Phase 2 — Root causes already documented (verify in code + logs)

Do not re-discover from scratch — **validate** these in live cold tests:

| Priority | Page/interaction | Cold | Root cause |
|----------|------------------|------|------------|
| P0 | screening → Show Results | 23.5s | **9 Streamlit reruns**; SQL ~900ms only |
| P0 | newsroom load/filter | 19–22s | Week-chunk AV+YF fan-out; 4k rows fetched, deduped in Python |
| P0 | company_filings cold | 15.4s | `get_companies_rows()` full `coreiq_companies` scan **~13.2s** |
| P0 | earnings_calls cold | 18.4s | Materialize MISS + 7.8s Azure cold connection |
| P1 | market_data each tab | 10–16s | 2 reruns/tab + financial pivot queries |
| P1 | earnings_calendar | 11–15s | Parallel metadata ~25s DB cumulative |
| P1 | home | 11.1s | `get_companies()` + BG_SCAN audit |

For each slow path document: **tables, columns SELECTed, row counts, whether data is actually used in UI.**

---

## Phase 3 — Cold-load testing (MANDATORY — no warm numbers)

**Do NOT report warm/cache-hit times as cold.** User was misled by warm numbers (EC 107ms, earnings_calls 0.26ms) — real cold is **seconds**.

### Methodology

1. **Fresh Streamlit process per scenario** — kill port 8501, restart
2. **`WARM_ON_BOOT=0`** — no background warmup
3. **Playwright incognito** — `browser.new_context()` per test
4. **Wait until data fully loaded** — no spinners, no "Loading…", actual table rows/numbers visible
5. Measure **wall-clock** goto → fully interactive

### Run existing scripts

```bash
# Full E2E: every page, tab, filter — cold + warm pairs
.venv/bin/python .claude/dev/e2e_perf_audit.py

# Per-page cold restart
.venv/bin/python .claude/dev/cold_load_benchmark.py
.venv/bin/python .claude/dev/cold_load_benchmark.py --strict-cold  # no disk materialize

# Single page
.venv/bin/python .claude/dev/e2e_perf_audit.py --filter market_data
```

### Launch server

```bash
bash .claude/dev/run_local.sh   # APP_ENV=LOCAL, OIDC bypass, STG DB — do NOT edit auth_manager.py
```

### Interaction matrix to test (ALL)

| Page | Interactions |
|------|----------------|
| `/home` | Full dashboard load |
| `/market_data` | Cold load + tabs: Income Statement, Balance Sheet, Cash Flow, Key Statistics, Segments, Estimates |
| `/newsroom` | Cold load + sector/ticker/date filters |
| `/earnings_calls` | Cold load + company switch + transcript render |
| `/earnings_calendar` | Cold load + month next/prev + company filter |
| `/screening` | Cold load + add criterion + Show Results |
| `/company_filings` | Cold load until filings list (NOT "Loading filing data") + company change |
| `/forecasting` | Cold load + company change |

### After each test — grep logs

```bash
grep -a "\[CLICK->RENDER\]" server-logs/server-log.log | tail -5
grep -a "\[RERUN\]" server-logs/server-log.log | tail -20
grep -a "\[RERUN_TRIGGER\]" server-logs/server-log.log | sort | uniq -c | sort -rn
grep -a "\[DATA_VOLUME\]" server-logs/server-log.log
grep -a "\[MAT\]" server-logs/server-log.log
grep -a "DB_get_companies" server-logs/server-log.log
```

Use `docs/MDP-STG-PERF-DIAGNOSTICS.md` as the STG grep card after deploy.

---

## Phase 4 — Optimization rules (same as SIP)

1. **Byte-identical numbers** — no UI/data changes without parity proof
2. **Three safe optimization types:** transport/parallelism, algebraically identical SQL rewrites, caching/materialization
3. **SIP patterns to apply:** disk materialize + watermark, `optimize_frame_memory`, `builtins`-guarded warm-once, eliminate double-fetch, push filters to SQL, parallel independent queries (`use_pure=True`), segment-cache tables, EC date-windowing
4. **Env defaults already in code** (`app/main.py`, `server_logger.py`): `MALLOC_ARENA_MAX=2`, `PAGE_MATERIALIZE=1`, etc. — verify on Linux/Azure

---

## Hard rules (MDP repo)

- **NEVER commit or push** unless user says exact words "please commit" or "please push"
- **KEEP OIDC bypass** — `APP_ENV=LOCAL` + `DEBUG=true` + `LOCAL_TEST_USER_EMAIL` via `run_local.sh`. Do NOT remove local bypass.
- **`require_auth()` commented on pages is intentional** for dev
- Use `utils.server_logger` only — no stdlib `logging`
- Never edit shadow dirs: `app/pages 2/`, `app/components 2/`, `app/data 2/`
- New design specs under `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
- Verify in real UI with Playwright before claiming done

---

## Deliverables expected from you

1. **Observability gap report** — SIP vs MDP checklist with ✅/❌/⚠️
2. **Cold benchmark table** — every page/tab/filter with wall-clock ms (not warm)
3. **Root cause map** — table/columns/rows/wasted data per slow query
4. **Rerun forensics** — `st.rerun()` locations, rerun count per interaction, fix priority
5. **Prioritized fix plan** — phased, impact-ordered (like SIP enhancement journey)
6. **STG verification plan** — grep commands, expected log lines after deploy

---

## What MDP already has (don't re-port blindly)

- SIP-style `server_logger` (heartbeat, RAM, analytics, rerun_id in formatter)
- `base_page.py`, `mem_opt.py`, `materialize.py`
- Screening universe materialization
- EC 42-day date window
- Earnings transcript ticker materialization
- `retailer_adding` → `db_manager`
- E2E audit scripts + investigation docs

**Your job:** close remaining gaps to SIP parity, then optimize cold paths to approach <1s SLA using the same methodology you used on SIP.
