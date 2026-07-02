# MDP SIP-Style Observability & Performance Design

**Date:** 2026-07-02  
**Scope:** CapIQReplacement (Coresight Market Data Portal)  
**Reference:** Modular-Code SIP performance/observability patterns

## Goals

1. Make restarts, reruns, click→render latency, and RAM pressure visible in persistent logs.
2. Standardize page-level correlation IDs (`rerun_id`) across all registered pages.
3. Reduce memory footprint of large screening universes via lossless categorical encoding.
4. Optional disk materialization for cold-start acceleration.
5. Expose diagnostics on the stealth `/logs` admin page without removing the shell.

## Architecture

### Logging (`app/utils/server_logger.py`)

| Feature | Implementation |
|---------|----------------|
| Persistent log dir | `/home/LogFiles/mdp` → `/home/mdp-logs` → `server-logs/` |
| Non-blocking I/O | `QueueHandler` + `QueueListener` + `RotatingFileHandler` (25MB × 8) |
| Correlation | `rerun_id` + `page` + `user_email` via `ContextEnrichFilter` |
| Boot ledger | Append-only `restarts.log` |
| Liveness | `[HEARTBEAT]` daemon (`SERVER_HEARTBEAT_SECS`, default 60) |
| Click→render | `[CLICK->RENDER]` via `log_render_complete()` |
| Rerun tracing | `[RERUN]` + `[RERUN_TRIGGER]` (monkey-patch `st.rerun` in `base_page`) |
| Analytics | `user_analytics.log` JSON lines via `log_user_event()` |
| RAM | `ram_snapshot_mb()`, `log_ram()`, `[RAM_CENSUS]`, `_malloc_trim()` |

### Page bootstrap (`app/components/base_page.py`)

- `bootstrap_page_observability(page_key)` — rerun counts, nav transitions, page_view analytics.
- `finish_page_observability()` — emits `[CLICK->RENDER]`.
- Called from `main.py` (`_patch_st_rerun_once`) and per-page `new_rerun_id()` first lines.

### Memory (`app/utils/mem_opt.py`)

- `optimize_frame_memory()` — object→category when `MDP_CATEGORICAL=1`.
- `decategorize()` — safety on filter output.
- `release_memory()` — post-build `malloc_trim`.

### Materialization (`app/utils/materialize.py`)

- `materialized_or_build(name, build_fn, sources)` — gzip pickle on persistent share.
- Toggle: `PAGE_MATERIALIZE=1`.
- Applied to screening base universe (`screening_service.get_base_company_universe`).

### Cache warmup (`app/utils/cache_manager.py`)

- `builtins._mdp_warmup_started` — survives Streamlit reload.
- `WARM_ON_BOOT=0` kill-switch.
- Sequential dropdown warmup to avoid DB connection storms.

### `/logs` page

- Tab 1: live log tail (`get_log_content`), stats, downloads (server + analytics).
- Tab 2: `analyze_slow_operations()` panel.
- Tab 3: CPU/RAM/disk helpers (existing shell functions).
- Admin allowlist + optional `ENFORCE_PAGE_AUTH=1`; stealth tab retained.

## Environment flags

| Flag | Default | Purpose |
|------|---------|---------|
| `SERVER_LOG_DIR` | (auto) | Override log directory |
| `SERVER_HEARTBEAT_SECS` | 60 | Heartbeat interval (0=off) |
| `SERVER_TRIM_ON_HEARTBEAT` | 1 | Periodic malloc_trim |
| `SERVER_TRIM_ON_RENDER` | 1 | Trim after slow renders |
| `SERVER_RAM_CENSUS` | 1 | RAM census every 3rd heartbeat |
| `MDP_CATEGORICAL` | 1 | Frame memory optimization |
| `PAGE_MATERIALIZE` | 1 | Disk materialization |
| `WARM_ON_BOOT` | 1 | Background cache warmup |
| `ENFORCE_PAGE_AUTH` | 0 | Re-enable `require_auth` on `/logs` |

## Testing plan

1. `bash .claude/dev/run_local.sh` (LOCAL OIDC bypass unchanged).
2. Playwright: `/home`, `/market_data`, `/screening`, `/earnings_calendar`, `/newsroom`.
3. Grep `server-logs/server-log.log` for `[RERUN]`, `[RERUN_TRIGGER]`, `[CLICK->RENDER]`, `[HEARTBEAT]`, `[RAM]`, `rerun_id` in formatter.
4. Confirm `user_analytics.log` has `page_view` events.
5. Verify screenshots show rendered pages (not login redirect).

## Rollout risks

- `malloc_trim` only effective on Linux/glibc (no-op on macOS dev).
- Materialization requires writable `/home` on Azure; falls back to live build.
- QueueHandler adds ~ms latency vs sync writes but prevents render blocking on slow CIFS.
