#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Start Streamlit in LOCAL perf-test mode.
#
# Reads STG DB credentials from the project .env file via Python (handles
# special characters in values), then relaunches with APP_ENV=LOCAL so the
# auth bypass activates without changing the real .env.
#
# Usage:
#   chmod +x scripts/start_ec_perf_local.sh
#   ./scripts/start_ec_perf_local.sh
#
# Then in a separate terminal:
#   python scripts/ec_perf_playwright.py [--headed]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found"
    exit 1
fi

# Use Python to safely parse .env (handles quotes, special chars, comments)
eval "$(python3 - "$ENV_FILE" <<'PYEOF'
import sys, re, json

env_file = sys.argv[1]
kv = {}
with open(env_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        # Strip surrounding quotes if present
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        kv[k] = v

# Emit shell export statements with proper quoting
for k, v in kv.items():
    safe_v = v.replace("'", "'\\''")
    print(f"export {k}='{safe_v}'")
PYEOF
)"

# Override APP_ENV to LOCAL so auth bypass triggers
export APP_ENV=LOCAL
export DEBUG=true
export LOCAL_TEST_USER_EMAIL="${LOCAL_TEST_USER_EMAIL:-mohdsaeedafri@coresight.com}"

# Map STG_DB_* → DB_* (config.py uses DB_* when APP_ENV=LOCAL)
export DB_HOST="${STG_DB_HOST:-localhost}"
export DB_PORT="${STG_DB_PORT:-3306}"
export DB_NAME="${STG_DB_NAME:-}"
export DB_USER="${STG_DB_USER:-}"
export DB_PASSWORD="${STG_DB_PASSWORD:-}"
# Keep SSL on for Azure
export ENABLE_SSL=true

echo "──────────────────────────────────────────────────────"
echo " Starting Streamlit in LOCAL perf-test mode"
echo " APP_ENV=LOCAL  DEBUG=true"
echo " LOCAL_TEST_USER_EMAIL=$LOCAL_TEST_USER_EMAIL"
echo " DB_HOST=$DB_HOST"
echo " Run Playwright test in a separate terminal:"
echo "   python scripts/ec_perf_playwright.py [--headed]"
echo "──────────────────────────────────────────────────────"

cd "$REPO_ROOT/app"
exec streamlit run main.py --server.port 8501 --server.headless true
