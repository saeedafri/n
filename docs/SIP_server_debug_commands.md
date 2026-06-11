# Server Debug Commands Reference

> **Private reference** — commands extracted from the System Logs debug page.
> Use these directly via SSH to get the same information that was in the UI tabs.

---

## 1. System Info

### Basic System Info

```bash
# Platform / OS / hostname
python3 -c "import platform; print(platform.platform()); print(platform.system()); print(platform.node())"

# Current working directory
pwd

# Home directory
echo $HOME

# Current user
whoami
```

### Uptime & Load Average

```bash
uptime
cat /proc/loadavg
```

### Running Processes (sorted by CPU)

```bash
ps aux --sort=-%cpu
# or on macOS:
ps aux
```

### Disk Usage

```bash
df -h
```

### Largest Items in Working Directory

```bash
du -sh * 2>/dev/null | sort -hr | head -20
```

---

## 2. Hardware

### CPU Info

```bash
# Linux
cat /proc/cpuinfo
lscpu

# macOS
sysctl -n machdep.cpu.brand_string
sysctl -a | grep cpu
```

### Memory (RAM + Swap)

```bash
# Linux — full memory details
cat /proc/meminfo

# Linux — human-readable summary
free -h

# macOS
sysctl hw.memsize
vm_stat
```

### Block Devices / Disks

```bash
# Linux
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,RO

# macOS
diskutil list
```

### Storage Partitions

```bash
df -h
```

### Kernel Version

```bash
uname -a
```

---

## 3. Network

### Network Interfaces & IP Addresses

```bash
# Linux
ip addr show

# macOS / fallback
ifconfig
```

### Routing Table

```bash
# Linux
ip route
# or
route -n

# macOS
netstat -rn
```

### Open Listening Ports

```bash
# Linux
ss -tuln

# macOS
lsof -i -P -n | grep LISTEN

# Fallback
netstat -tuln
```

### Active TCP Connections

```bash
# Linux
ss -tup

# macOS
netstat -an | grep ESTABLISHED | head -30
```

### DNS Configuration

```bash
# Linux
cat /etc/resolv.conf

# macOS
scutil --dns | head -30
```

### Hosts File

```bash
cat /etc/hosts
```

### Firewall Rules

```bash
# macOS
sudo pfctl -sr

# Ubuntu / Debian
ufw status verbose

# CentOS / RHEL / generic Linux
iptables -L -n
```

---

## 4. Security

### Current User & Identity

```bash
whoami
id
groups
```

### User Accounts on System

```bash
# Linux
cat /etc/passwd

# macOS
dscl . list /Users
```

### Sudo Privileges

```bash
sudo -l
```

### SSH Configuration

```bash
# Server SSH config
cat /etc/ssh/sshd_config

# Authorized keys
cat ~/.ssh/authorized_keys
```

### Cron Jobs

```bash
# Current user's crontab
crontab -l

# System cron directories
ls /etc/cron.d /etc/cron.daily /etc/cron.hourly /etc/cron.weekly

# System crontab
cat /etc/crontab
```

### Running Services

```bash
# systemd (modern Linux)
systemctl list-units --type=service --state=running --no-pager --no-legend

# SysV init (older Linux)
service --status-all 2>&1 | grep ' + '

# Quick check
ps aux | grep -v grep | head -30
```

### Python Packages Installed

```bash
pip list --format=columns
# or
pip3 list --format=columns
```

### Open Files / Network Sockets

```bash
lsof -i | head -40
```

---

## 5. Environment Variables (FULL — UNMASKED)

> These commands print the **actual values** with no masking.

### Get ALL environment variables at once

```bash
env | sort
# or
printenv | sort
```

### App / Environment

```bash
echo "ENVIRONMENT        = $(printenv ENVIRONMENT)"
echo "LOG_LEVEL          = $(printenv LOG_LEVEL)"
echo "REQUIRE_MEMBERSHIP = $(printenv REQUIRE_MEMBERSHIP)"
echo "ALLOW_LOCAL_BYPASS = $(printenv ALLOW_LOCAL_BYPASS)"
echo "BYPASS_EMAIL       = $(printenv BYPASS_EMAIL)"
echo "BYPASS_MEMBERSHIP_ID   = $(printenv BYPASS_MEMBERSHIP_ID)"
echo "BYPASS_MEMBERSHIP_TYPE = $(printenv BYPASS_MEMBERSHIP_TYPE)"
```

### Database (includes passwords)

```bash
echo "DB_HOST       = $(printenv DB_HOST)"
echo "DB_PORT       = $(printenv DB_PORT)"
echo "DB_NAME       = $(printenv DB_NAME)"
echo "DB_USER       = $(printenv DB_USER)"
echo "DB_PASSWORD   = $(printenv DB_PASSWORD)"
echo "ENABLE_SSL    = $(printenv ENABLE_SSL)"
echo "SSL_CA        = $(printenv SSL_CA)"
echo "DB_SSL_CA_PATH= $(printenv DB_SSL_CA_PATH)"
```

### OIDC / OAuth2 (includes client secret)

```bash
echo "OIDC_IDP_BASE_URL  = $(printenv OIDC_IDP_BASE_URL)"
echo "OIDC_CLIENT_ID     = $(printenv OIDC_CLIENT_ID)"
echo "OIDC_CLIENT_SECRET = $(printenv OIDC_CLIENT_SECRET)"
echo "OIDC_REDIRECT_URI  = $(printenv OIDC_REDIRECT_URI)"
echo "OIDC_SCOPES        = $(printenv OIDC_SCOPES)"
```

### Auth / WordPress

```bash
echo "AUTH_URL                = $(printenv AUTH_URL)"
echo "AUTH_URL_PAID           = $(printenv AUTH_URL_PAID)"
echo "MEMBERSHIP_ABSOLUTE_URL = $(printenv MEMBERSHIP_ABSOLUTE_URL)"
echo "MEMBERSHIP_IDS_CSV      = $(printenv MEMBERSHIP_IDS_CSV)"
```

### Azure Blob Storage (includes account key)

```bash
echo "AZURE_STORAGE_ACCOUNT_NAME = $(printenv AZURE_STORAGE_ACCOUNT_NAME)"
echo "AZURE_STORAGE_ACCOUNT_KEY  = $(printenv AZURE_STORAGE_ACCOUNT_KEY)"
echo "AZURE_BLOB_CONTAINER       = $(printenv AZURE_BLOB_CONTAINER)"
```

### Email / SMTP (includes password)

```bash
echo "SMTP_SERVER    = $(printenv SMTP_SERVER)"
echo "SMTP_PORT      = $(printenv SMTP_PORT)"
echo "FROM_EMAIL     = $(printenv FROM_EMAIL)"
echo "EMAIL_PASSWORD = $(printenv EMAIL_PASSWORD)"
```

### Misc (includes API keys)

```bash
echo "CXY_API_KEY          = $(printenv CXY_API_KEY)"
echo "CSV_FILE_PATH        = $(printenv CSV_FILE_PATH)"
echo "MISSING_LOGS_PASSWORD= $(printenv MISSING_LOGS_PASSWORD)"
```

### One-Liner: Dump ALL project env vars at once

```bash
printenv | grep -E '^(ENVIRONMENT|LOG_LEVEL|REQUIRE_MEMBERSHIP|ALLOW_LOCAL_BYPASS|BYPASS_EMAIL|BYPASS_MEMBERSHIP_ID|BYPASS_MEMBERSHIP_TYPE|DB_HOST|DB_PORT|DB_NAME|DB_USER|DB_PASSWORD|ENABLE_SSL|SSL_CA|DB_SSL_CA_PATH|OIDC_IDP_BASE_URL|OIDC_CLIENT_ID|OIDC_CLIENT_SECRET|OIDC_REDIRECT_URI|OIDC_SCOPES|AUTH_URL|AUTH_URL_PAID|MEMBERSHIP_ABSOLUTE_URL|MEMBERSHIP_IDS_CSV|AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_ACCOUNT_KEY|AZURE_BLOB_CONTAINER|SMTP_SERVER|SMTP_PORT|FROM_EMAIL|EMAIL_PASSWORD|CXY_API_KEY|CSV_FILE_PATH|MISSING_LOGS_PASSWORD)=' | sort
```

---

## Quick All-In-One Script

Save as `debug_dump.sh` and run via `bash debug_dump.sh` on the server:

```bash
#!/bin/bash
echo "===== SYSTEM ====="
uname -a && uptime && cat /proc/loadavg 2>/dev/null

echo -e "\n===== CPU ====="
lscpu 2>/dev/null || sysctl -n machdep.cpu.brand_string 2>/dev/null

echo -e "\n===== MEMORY ====="
free -h 2>/dev/null || vm_stat 2>/dev/null

echo -e "\n===== DISK ====="
df -h

echo -e "\n===== NETWORK ====="
ip addr show 2>/dev/null || ifconfig 2>/dev/null
echo "--- Ports ---"
ss -tuln 2>/dev/null || netstat -tuln 2>/dev/null

echo -e "\n===== SECURITY ====="
whoami && id
echo "--- Services ---"
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | head -20

echo -e "\n===== ENV VARS ====="
printenv | grep -E '^(DB_|OIDC_|AUTH_|AZURE_|SMTP_|EMAIL_|CXY_|ENVIRONMENT|LOG_LEVEL)' | sort

echo -e "\nDone."
```

---

## 6. File Explorer (via SSH)

### Navigate & List Files

```bash
# List current directory with details
ls -la

# List with human-readable sizes
ls -lah

# List specific directory
ls -la /path/to/dir

# Recursive listing (limit depth)
find /path/to/dir -maxdepth 2 -ls 2>/dev/null | head -50

# Count files and folders
echo "Folders: $(find . -maxdepth 1 -type d | wc -l)"
echo "Files:   $(find . -maxdepth 1 -type f | wc -l)"

# Tree view (if available)
tree -L 3 2>/dev/null || find . -maxdepth 3 -type d 2>/dev/null | head -50
```

### View File Contents

```bash
# View full file
cat /path/to/file

# View first N lines
head -50 /path/to/file

# View last N lines
tail -50 /path/to/file

# View with line numbers
cat -n /path/to/file

# View file size
stat /path/to/file
ls -lh /path/to/file
```

### Download File (via SCP from another machine)

```bash
# Download from server to local
scp user@server:/remote/path/file.py ./local_file.py

# Download entire directory
scp -r user@server:/remote/dir/ ./local_dir/
```

### Search Files

```bash
# Find files by name
find . -name "*.py" -type f 2>/dev/null

# Find files by content (grep)
grep -rn "search_term" /path/to/dir/ 2>/dev/null

# Find files modified in last 24 hours
find . -mtime -1 -type f 2>/dev/null

# Find large files (>10MB)
find . -size +10M -type f 2>/dev/null
```

---

## 7. Deploy File (via SSH)

### Backup Before Deploying

```bash
# Create timestamped backup
cp /path/to/target.py /path/to/target.py.bak.$(date +%Y%m%d_%H%M%S)

# Simple .bak backup
cp /path/to/target.py /path/to/target.py.bak
```

### Deploy by Pasting Content

```bash
# Write content to file using heredoc (paste content between EOF markers)
cat > /path/to/target.py << 'EOF'
# paste your full file content here
EOF

# Verify what was written
cat /path/to/target.py | head -10
wc -l /path/to/target.py
```

### Deploy via SCP (from local machine)

```bash
# Upload single file
scp ./local_file.py user@server:/remote/path/target.py

# Upload with backup first
ssh user@server "cp /remote/path/target.py /remote/path/target.py.bak"
scp ./local_file.py user@server:/remote/path/target.py

# Verify after upload
ssh user@server "wc -l /remote/path/target.py && md5sum /remote/path/target.py"
```

### Deploy via nano/vim (edit in-place)

```bash
# Edit file directly
nano /path/to/target.py
# or
vim /path/to/target.py
```

### Common Deploy Targets

```bash
# Known SIP project files:
# pages/logout_bridge.py
# login.py
# auth_utils.py
# pages/opening.py
# pages/logs.py

# Example: backup + deploy login.py
APP_ROOT=$(pwd)
cp "$APP_ROOT/login.py" "$APP_ROOT/login.py.bak"
# then paste or scp the new content
```

### Verify Deployment

```bash
# Check file exists and size
ls -la /path/to/target.py

# Compare with backup
diff /path/to/target.py /path/to/target.py.bak

# Check syntax (Python)
python3 -m py_compile /path/to/target.py && echo "Syntax OK" || echo "Syntax ERROR"
```

---

## 8. Quick System Commands (reference)

```bash
# List all files recursively
find . -type f 2>/dev/null | head -50

# Find .log files
find . -name '*.log' -type f 2>/dev/null | head -30

# Directory tree
find . -maxdepth 3 -type d 2>/dev/null | head -50

# Running processes
ps aux

# Network connections
lsof -i -P -n 2>/dev/null | head -30 || netstat -tuln 2>/dev/null || ss -tuln

# Memory usage
vm_stat 2>/dev/null || free -h 2>/dev/null || cat /proc/meminfo | head -5

# CPU info
sysctl -n machdep.cpu.brand_string 2>/dev/null || cat /proc/cpuinfo | grep 'model name' | head -1

# Current directory contents
ls -la

# Python version
python3 --version 2>/dev/null || python --version 2>/dev/null

# Streamlit version
python3 -c 'import streamlit; print(streamlit.__version__)' 2>/dev/null
```
