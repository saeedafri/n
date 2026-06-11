# Market Data Portal — Server Debug Commands Reference

> **Private reference** — all commands from the hidden debug tabs in `app/pages/logs.py`.
> Access the page via `?o` query param. Use these directly via SSH when the UI is unavailable.

---

## How to Access the Debug Page

```
https://<your-domain>/logs?o
```

Requires owner login (`mohdsaeedafri@coresight.com`). All 7 hidden tabs are in the top-right
corner — tiny invisible dots. Click the far-right edge of the tab bar to reveal them.

**Tab order (left → right):**
1. `📝 Live Server Logs` — visible, default tab
2. `·`  — System Info
3. `··` — Hardware
4. `···` — Network
5. `····` — File Explorer (UI only, no shell commands)
6. `·····` — Environment Variables (reads from `os.getenv`, no shell commands)
7. `······` — Security
8. `·······` — Quick Commands + custom shell runner

---

## Tab 2 — System Info

### Platform / OS / Hostname

```bash
python3 -c "import platform, os; print(platform.platform()); print(platform.system()); print(platform.node()); print(os.getcwd())"
```

### Uptime & Load Average

```bash
uptime
cat /proc/loadavg
```

Output format for `/proc/loadavg`:
```
<load1> <load5> <load15> <running>/<total_threads> <last_pid>
```

### Running Processes (sorted by CPU)

```bash
ps aux --sort=-%cpu
# macOS fallback:
ps aux
```

### Disk Usage (human-readable)

```bash
df -h
```

### Largest Items in Working Directory

```bash
du -sh * 2>/dev/null | sort -hr | head -20
```

---

## Tab 3 — Hardware

### CPU Info

```bash
cat /proc/cpuinfo
# Or structured summary:
lscpu 2>/dev/null || cat /proc/cpuinfo | head -40
```

Key fields to look for in `/proc/cpuinfo`: `model name`, `physical id`, `cpu cores`, `cpu mhz`

### Memory (RAM + Swap)

```bash
cat /proc/meminfo
```

Key fields: `MemTotal`, `MemFree`, `MemAvailable`, `Buffers`, `Cached`, `SwapTotal`, `SwapFree`

### Block Devices

```bash
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,RO 2>/dev/null
# Fallback:
lsblk 2>/dev/null || fdisk -l 2>/dev/null
```

### Storage Partitions

```bash
df -h
```

### PCI Devices

```bash
lspci 2>/dev/null || echo 'lspci not available'
```

### USB Devices

```bash
lsusb 2>/dev/null || echo 'lsusb not available'
```

### NUMA / BIOS / System Info (needs root)

```bash
dmidecode -t system 2>/dev/null || echo 'dmidecode not available (needs root)'
```

### Kernel Version

```bash
uname -a
```

---

## Tab 4 — Network

### Network Interfaces & IP Addresses

```bash
ip addr show 2>/dev/null
# Fallback:
ifconfig 2>/dev/null
```

### Routing Table

```bash
ip route 2>/dev/null
# Fallback:
route -n 2>/dev/null
```

### Open Listening Ports

```bash
ss -tuln 2>/dev/null
# Fallback:
netstat -tuln 2>/dev/null
```

Flags: `-t` TCP, `-u` UDP, `-l` listening, `-n` numeric (no DNS resolve)

### Active TCP Connections (with process names)

```bash
ss -tup 2>/dev/null
# Fallback:
netstat -tup 2>/dev/null
```

### DNS Configuration

```bash
cat /etc/resolv.conf 2>/dev/null
```

### Hosts File

```bash
cat /etc/hosts 2>/dev/null
```

### Firewall Rules

```bash
ufw status verbose 2>/dev/null
# Fallback (iptables):
iptables -L -n 2>/dev/null
```

---

## Tab 5 — File Explorer

> UI-only tab. No shell commands — it uses Python's `pathlib.Path.iterdir()` to browse the
> filesystem and reads files directly. Use these SSH equivalents instead:

### List Directory Contents

```bash
ls -la /path/to/dir
```

### Find Files by Extension

```bash
find /path/to/dir -name "*.log" -type f
find /path/to/dir -name "*.py"  -type f | head -50
```

### Read a Text File

```bash
cat /path/to/file.log
# Large files — tail last N lines:
tail -n 200 /path/to/file.log
# Head first N lines:
head -n 100 /path/to/file.log
```

### Download / Copy a File

```bash
# Via SCP from your machine:
scp user@host:/path/to/file.log ./local-copy.log
```

---

## Tab 6 — Environment Variables

> UI-only tab. Reads live env vars via `os.getenv()`. Use these SSH equivalents:

### Print All Environment Variables

```bash
printenv
# or:
env
```

### Check a Specific Variable

```bash
echo $APP_ENV
echo $DB_HOST
echo $AZURE_STORAGE_ACCOUNT_NAME
```

### Check All Project Variables at Once

```bash
for var in APP_ENV DEBUG SECRET_KEY SESSION_TIMEOUT ENABLE_CACHING CACHE_TTL \
           DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD DB_POOL_SIZE DB_MAX_OVERFLOW \
           STG_DB_HOST STG_DB_PORT STG_DB_NAME STG_DB_USER STG_DB_PASSWORD \
           PROD_DB_HOST PROD_DB_PORT PROD_DB_NAME PROD_DB_USER PROD_DB_PASSWORD \
           ENABLE_SSL SSL_CA \
           AZURE_STORAGE_ACCOUNT_NAME AZURE_STORAGE_ACCOUNT_KEY AZURE_BLOB_CONTAINER AZURE_BLOB_PREFIX \
           AUTH_URL AUTH_URL_PAID \
           OPENAI_API_KEY \
           SMTP_SERVER SMTP_PORT FROM_EMAIL EMAIL_PASSWORD; do
  val=$(printenv "$var")
  if [ -n "$val" ]; then
    echo "✅ $var = $val"
  else
    echo "❌ $var = NOT SET"
  fi
done
```

---

## Tab 7 — Security

### Current User & Identity

```bash
whoami && id && groups 2>/dev/null
```

### All User Accounts on System (real users: UID 0 or >= 1000)

```bash
cat /etc/passwd 2>/dev/null
# Filter to real users only:
awk -F: '($3==0 || $3>=1000) {print $1, $3, $6, $7}' /etc/passwd
```

### Sudo Privileges

```bash
sudo -l 2>/dev/null || echo 'Cannot determine sudo access'
```

### SSH Configuration

```bash
# sshd config:
cat /etc/ssh/sshd_config 2>/dev/null
# Authorized keys for current user:
cat ~/.ssh/authorized_keys 2>/dev/null || echo 'Not found or empty'
```

### Cron Jobs

```bash
# Current user's crontab:
crontab -l 2>/dev/null

# System cron directories:
ls /etc/cron.d 2>/dev/null
ls /etc/cron.daily 2>/dev/null
ls /etc/cron.hourly 2>/dev/null
ls /etc/cron.weekly 2>/dev/null

# System crontab:
cat /etc/crontab 2>/dev/null
```

### Running Services

```bash
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null
# Fallback (older systems):
service --status-all 2>&1 | grep ' + '
```

### Python Packages Installed

```bash
pip list --format=columns 2>/dev/null
# Or:
pip3 list --format=columns 2>/dev/null
# Search for a specific package:
pip show streamlit 2>/dev/null
```

### Open Network Sockets / Files (lsof)

```bash
lsof -i 2>/dev/null | head -40
# All open files by a specific process:
lsof -p <PID>
# Connections on a specific port:
lsof -i :8501
```

---

## Tab 8 — Quick Commands

These are the preset one-click commands from the Quick Commands tab:

### List All Files (recursive, first 50)

```bash
find . -type f 2>/dev/null | head -50
```

### Find .log Files

```bash
find . -name '*.log' -type f 2>/dev/null | head -30
```

### Directory Tree (depth 3)

```bash
tree -L 3 2>/dev/null
# Fallback (no tree installed):
find . -maxdepth 3 -type d 2>/dev/null | head -50
```

### Running Processes

```bash
ps aux
```

### Network Connections

```bash
netstat -tuln 2>/dev/null || ss -tuln
```

### Memory Usage (quick summary)

```bash
free -h 2>/dev/null
# Fallback:
cat /proc/meminfo | head -5
```

### CPU Model (one line)

```bash
cat /proc/cpuinfo | grep 'model name' | head -1
```

### Azure Log Paths

```bash
ls -la /var/log/azure 2>/dev/null || echo 'Not found'
```

### Azure App Service Logs

```bash
ls -la /home/LogFiles 2>/dev/null || echo 'Not found'
# Common paths on Azure App Service:
ls -la /home/LogFiles/Application/ 2>/dev/null
ls -la /home/LogFiles/http/RawLogs/ 2>/dev/null
cat /home/LogFiles/Application/$(ls /home/LogFiles/Application/ 2>/dev/null | tail -1) 2>/dev/null | tail -100
```

### Current Directory Contents

```bash
ls -la
```

---

## Useful Combos (SSH Quick Diagnostics)

### Full health snapshot (one paste)

```bash
echo "=== UPTIME ===" && uptime
echo "=== LOAD ===" && cat /proc/loadavg
echo "=== MEMORY ===" && free -h 2>/dev/null || cat /proc/meminfo | grep -E 'MemTotal|MemAvailable|SwapTotal'
echo "=== DISK ===" && df -h
echo "=== TOP PROCESSES ===" && ps aux --sort=-%cpu | head -10
echo "=== LISTENING PORTS ===" && ss -tuln 2>/dev/null || netstat -tuln 2>/dev/null
echo "=== WHO AM I ===" && whoami && id
```

### Find the Streamlit process and its port

```bash
ps aux | grep streamlit
ss -tlnp | grep streamlit
lsof -i :8501 2>/dev/null
```

### Tail the server log file live

```bash
# Default log path from SERVER_LOG_FILE in server_logger.py:
tail -f server-logs/server-log.log
# Or with timestamps highlighted:
tail -f server-logs/server-log.log | grep --line-buffered -E 'ERROR|CRITICAL|WARNING'
```

### Check MySQL connectivity from the server

```bash
mysql -h $DB_HOST -P $DB_PORT -u $DB_USER -p$DB_PASSWORD -e "SELECT 1;" $DB_NAME 2>/dev/null && echo "DB OK" || echo "DB FAILED"
```

### Check Azure Blob connectivity

```bash
python3 -c "
from azure.storage.blob import BlobServiceClient
import os
client = BlobServiceClient(account_url=f'https://{os.getenv(\"AZURE_STORAGE_ACCOUNT_NAME\")}.blob.core.windows.net',
    credential=os.getenv('AZURE_STORAGE_ACCOUNT_KEY'))
containers = [c['name'] for c in client.list_containers()]
print('Azure OK, containers:', containers)
"
```

---

## Log File Locations

| File | Path |
|------|------|
| Application server log | `server-logs/server-log.log` (relative to app working dir) |
| Azure App Service logs | `/home/LogFiles/Application/` |
| Azure HTTP logs | `/home/LogFiles/http/RawLogs/` |
| System auth log | `/var/log/auth.log` |
| Syslog | `/var/log/syslog` |
| Nginx (if used) | `/var/log/nginx/access.log`, `/var/log/nginx/error.log` |

---

*Last updated: 2026-04-17*
