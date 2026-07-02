# Logs Page — Technical Documentation

**Application:** Market Data Portal (MDP)  
**Source:** `app/pages/logs.py`  
**URL path:** `/logs` (registered in `app/main.py` as hidden navigation)  
**Purpose:** Internal server log viewer, screening segment-cache admin panel, and stealth shell command runner.  
**Audience:** Platform operators and engineers with direct URL access.

---

## Overview

The Logs page is a **debug / operations** surface for the Market Data Portal (MDP). It is **not linked from the main navigation** (`st.navigation(..., position="hidden")` in `main.py`). Users reach it only by navigating directly to `/logs`.

**Security note:** This hidden route does not enforce page-level `require_auth()` or ACL. Restrict reachability via network controls and monitor access in production.

---

## Registration & Routing

| Property | Value |
|----------|-------|
| File | `app/pages/logs.py` |
| `st.Page` title | `Logs` |
| `url_path` | `logs` |
| Default page | No |
| Sidebar | Hidden via `hide_sidebar()` |

Hidden route: `/logs` is registered but not linked from navigation — direct URL access only.

```mermaid
%%{init: {'flowchart': {'nodeSpacing': 50, 'curve': 'basis'}}}%%
flowchart LR
    START["Browser GET /logs"] --> MAIN["main.py — st.navigation"]
    MAIN --> PAGE["Streamlit Page — logs.py main()"]
    PAGE --> LAYOUT["set_page_layout + render_styles"]
    LAYOUT --> TABS["Three stealth tabs"]
    TABS --> T1["Live Server Logs"]
    TABS --> T2["Segment Cache"]
    TABS --> T3["Stealth Command Runner"]

    classDef start fill:#e8f4fd,stroke:#1e88e5,color:#0d47a1
    classDef db fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef ui fill:#e8f5e9,stroke:#43a047,color:#1b5e20
    classDef error fill:#ffebee,stroke:#e53935,color:#b71c1c
    classDef ext fill:#f3e5f5,stroke:#8e24aa,color:#4a148c
    classDef process fill:#eceff1,stroke:#546e7a,color:#263238
    class START start
    class PAGE,TABS,T1,T2,T3 ui
    class MAIN,LAYOUT process
```

---

## Authentication & Access Control

### Design

1. **`require_auth()`** — OpenID Connect (OIDC) session check at page entry.
2. **Owner-only gate** — restrict to authorized operator email or admin role.

### Current behavior

- No `AccessControlManager` check.
- No `UserRolesManager` check.
- Page renders for any visitor who can hit the Streamlit route.

```mermaid
%%{init: {'flowchart': {'nodeSpacing': 50, 'curve': 'basis'}}}%%
flowchart TD
    subgraph intended [Intended enforcement]
        I1[require_auth] --> I2{user == owner email?}
        I2 -->|no| I3[st.error + st.stop]
        I2 -->|yes| I4[Render page]
    end
    subgraph actual [Current]
        A1[Import logs.py] --> A2[main immediately]
        A2 --> A3[Full page UI]
    end
    classDef start fill:#e8f4fd,stroke:#1e88e5,color:#0d47a1
    classDef db fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef ui fill:#e8f5e9,stroke:#43a047,color:#1b5e20
    classDef error fill:#ffebee,stroke:#e53935,color:#b71c1c
    classDef ext fill:#f3e5f5,stroke:#8e24aa,color:#4a148c
    classDef process fill:#eceff1,stroke:#546e7a,color:#263238
    class I1 ui
```

**Recommendation for operators:** Enforce `require_auth()` and gate via `UserRolesManager.is_admin()` or a dedicated `coreiq_page_access_control` row for page `logs`.

---

## UI Structure

### Layout

- `set_page_layout(header_full_width=True, footer_full_width=True, body_padding="20px 40px", max_content_width="1400px")`
- `render_styles()` from `components.styles`
- Custom CSS hides the **third tab label** (stealth tab) — invisible 2–6px wide tab button aligned to the far right.

### Tabs

| Tab label (visible) | Internal key | Function |
|---------------------|--------------|----------|
| Live Server Logs | `tab1` | Metrics, clear/download/refresh log file |
| Segment Cache | `tab_seg` | Screening segment cache rebuild UI |
| `·` (stealth) | `tab2` | Shell command form |

```mermaid
%%{init: {'flowchart': {'nodeSpacing': 50, 'curve': 'basis'}}}%%
flowchart TD
    subgraph tab1 [Live Server Logs]
        T1A[get_log_stats metrics]
        T1B[Clear Logs → clear_logs]
        T1C[Download → download_logs]
        T1D[Refresh → st.rerun]
    end
    subgraph tab_seg [Segment Cache]
        S1[get_segment_values_cache_status]
        S2[get_segment_cache_build_state]
        S3[Rebuild Full → rebuild_segment_values_cache_async]
        S4[Reload Presets → populate_segment_member_cache_from_presets]
    end
    subgraph tab2 [Stealth]
        X1[st.form stealth_form]
        X2[run_command custom_cmd]
    end
    classDef start fill:#e8f4fd,stroke:#1e88e5,color:#0d47a1
    classDef db fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef ui fill:#e8f5e9,stroke:#43a047,color:#1b5e20
    classDef error fill:#ffebee,stroke:#e53935,color:#b71c1c
    classDef ext fill:#f3e5f5,stroke:#8e24aa,color:#4a148c
    classDef process fill:#eceff1,stroke:#546e7a,color:#263238
    class S2,S3 ui
```

---

## Tab 1 — Live Server Logs

### Dependencies

| Module | Symbols used |
|--------|----------------|
| `utils.server_logger` | `clear_logs`, `download_logs`, `get_log_stats`, `log_structured_error`, `SERVER_LOG_FILE` |
| `components.styles` | `hide_sidebar`, `render_styles`, `set_page_layout` |

### Log file location

- Directory: `server-logs/` (repo root, sibling of `app/`)
- File: `server-log.log` (`SERVER_LOG_FILE` in `server_logger.py`)

### Log format (written by `server_logger`)

```
{timestamp} | {level} | {rerun_id} | page={page} | {filename}:{funcName} | {message}
```

### User actions

| Button | Handler | Side effect |
|--------|---------|-------------|
| Clear Logs | `clear_logs()` | Stops queue listener, truncates file, writes IST "Logs cleared" marker |
| Download Logs | `st.download_button(data=download_logs())` | Full file bytes, filename `server-logs-{YYYYMMDD_HHMMSS}.log` |
| Refresh Now | `st.rerun()` | Re-reads stats |

### Metrics displayed

- File size (bytes)
- Total line count
- Last modified timestamp
- Static label `server-log.log`

**Note:** Tab 1 does **not** render inline log tail content in the current UI — only file metadata and actions. Full content is available via download. `get_log_content()` exists in `server_logger` but is unused on this page.

```mermaid
%%{init: {'flowchart': {'nodeSpacing': 50, 'curve': 'basis'}}}%%
sequenceDiagram
    participant U as User
    participant P as logs.py
    participant SL as server_logger
    participant FS as server-log.log

    U->>P: Open /logs Tab 1
    P->>SL: get_log_stats()
    SL->>FS: stat + line count
    SL-->>P: exists, size, lines, modified
    P-->>U: st.metric x4

    U->>P: Clear Logs
    P->>SL: clear_logs()
    SL->>FS: truncate + marker line
    SL-->>P: True/False
    P->>P: st.rerun()

    U->>P: Download
    P->>SL: download_logs()
    SL->>FS: read bytes
    SL-->>U: download button payload
```

---

## Tab 2 — Segment Cache Manager

Powers the **Business / Geographical Segments** screening filter. Data lives in MySQL tables managed by `app/data/screening_service.py`.

### Imported functions

```python
from data.screening_service import (
    get_segment_values_cache_status,
    get_segment_cache_build_state,
    rebuild_segment_values_cache_async,
    populate_segment_member_cache_from_presets,
)
```

### Database tables

| Table | Role |
|-------|------|
| `coreiq_screening_segment_values_cache` | Per-ticker segment metric values for screening |
| `coreiq_screening_segment_member_cache` | Segment member label options (presets + derived) |

### Status metrics

- `total_rows` — rows in values cache
- `distinct_tickers` — unique tickers covered
- `business_rows_count` / `geographical_rows_count`
- `last_updated_timestamp`

### Build state (`st.session_state` + module lock)

Module-level `_segment_cache_build_state` dict:

| Key | Meaning |
|-----|---------|
| `running` | Background thread active |
| `progress` | Current step |
| `total` | Total steps (typically 2) |
| `last_error` | Last failure message |

### Actions

1. **Rebuild Full Segment Cache** — `rebuild_segment_values_cache_async()`  
   - Replays segment classifier over full company universe (~15–25 min).  
   - Uses `coreiq_filing_metrics_v5` (per UI help text).  
   - Non-blocking background thread.

2. **Reload Preset Members** — `populate_segment_member_cache_from_presets()`  
   - Instant upsert of hardcoded preset labels (~0 ms).

```mermaid
%%{init: {'flowchart': {'nodeSpacing': 50, 'curve': 'basis'}}}%%
flowchart TD
    A[User clicks Rebuild Full] --> B{rebuild_segment_values_cache_async}
    B -->|already running| C[st.warning]
    B -->|started| D[Background thread]
    D --> E[build_segment_values_cache]
    E --> F[ensure_segment_values_cache_table]
    E --> G[Classifier over ticker universe]
    G --> H[Write coreiq_screening_segment_values_cache]
    H --> I[Update build_state running=false]

    J[Reload Presets] --> K[populate_segment_member_cache_from_presets]
    K --> L[Upsert coreiq_screening_segment_member_cache]
    classDef start fill:#e8f4fd,stroke:#1e88e5,color:#0d47a1
    classDef db fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef ui fill:#e8f5e9,stroke:#43a047,color:#1b5e20
    classDef error fill:#ffebee,stroke:#e53935,color:#b71c1c
    classDef ext fill:#f3e5f5,stroke:#8e24aa,color:#4a148c
    classDef process fill:#eceff1,stroke:#546e7a,color:#263238
    class A ui
```

---

## Tab 3 — Stealth Command Runner

### UX

- Third tab styled invisible (1px font, transparent).
- `st.form("stealth_form")` with collapsed text input.
- Submit button hidden via CSS (`opacity: 0; height: 0`).
- Output: `st.code(stdout)` or `st.error(stderr)`.

### `run_command(cmd, timeout=15)`

- Executes `subprocess.run(cmd, shell=True, ...)`.
- Docstring claims only predefined commands — **implementation accepts any string** from the form.
- Timeout: 15 seconds default.

### Security implications

This is effectively a **remote shell** if the page is reachable without auth. Path traversal guards exist in `list_directory` / `read_file_safe` helpers, but those helpers are **not wired to the stealth tab** in current `main()`.

### Unused helpers (present in file)

The module defines extensive Linux diagnostics helpers not rendered in `main()`:

- `get_system_info`, `get_environment_vars`, `get_meminfo`, `get_cpu_info`
- `parse_df`, `parse_ps`, `parse_network_interfaces`, `parse_open_ports`
- `list_directory`, `read_file_safe`, `get_file_download_bytes`
- etc.

These appear to be legacy from a fuller "System Logs Explorer" and may be re-enabled in future tabs.

---

## Session State

| Key | Set by | Purpose |
|-----|--------|---------|
| `stealth_cmd` | Streamlit widget | Last command typed in stealth form |

No other `st.session_state` keys are used on this page. Segment build state lives in **module globals** inside `screening_service.py`, not Streamlit session.

---

## Error Handling

All major blocks wrap `try/except` and call:

```python
log_structured_error(e, page="logs", component="...", operation="...")
```

Components: `module_init`, `run_command`, `main`, `_render_log_body`, etc.

---

## Related Documentation

- Shell commands reference: `docs/market-data-commands.md` (cited in stealth tab comment)
- Logging standard: `CLAUDE.md` → Logging section
- Segment cache implementation: `app/data/screening_service.py` (~lines 1530–2900)

---

## Operational Checklist

```mermaid
%%{init: {'flowchart': {'nodeSpacing': 50, 'curve': 'basis'}}}%%
flowchart LR
    A[Need log file?] --> B[Download from Tab 1]
    C[Segment screening empty?] --> D[Tab 2 Reload Presets]
    D --> E{Still broken?}
    E -->|yes| F[Rebuild Full Cache]
    G[Production exposure?] --> H[Verify auth on /logs]
    H --> I[Network / WAF restrict route]
    classDef start fill:#e8f4fd,stroke:#1e88e5,color:#0d47a1
    classDef db fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef ui fill:#e8f5e9,stroke:#43a047,color:#1b5e20
    classDef error fill:#ffebee,stroke:#e53935,color:#b71c1c
    classDef ext fill:#f3e5f5,stroke:#8e24aa,color:#4a148c
    classDef process fill:#eceff1,stroke:#546e7a,color:#263238
```

---

## File Dependency Graph

```mermaid
%%{init: {'flowchart': {'nodeSpacing': 50, 'curve': 'basis'}}}%%
graph TD
    logs[pages/logs.py]
    logs --> styles[components/styles.py]
    logs --> sl[utils/server_logger.py]
    logs --> ss[data/screening_service.py]
    ss --> db[core/database.py]
    ss --> fm[coreiq_filing_metrics_v5]
    sl --> file[server-logs/server-log.log]
```

---

*Generated from source analysis of the Market Data Portal (MDP). Last reviewed against codebase structure as of project documentation pass.*
