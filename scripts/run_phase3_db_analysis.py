#!/usr/bin/env python3
"""
Phase 3 — Index investigation for the Earnings Calendar optimization.

Run this from the repo root while connected to VPN / Azure network:

    cd /path/to/CapIQReplacement
    python3 scripts/run_phase3_db_analysis.py

Output is written to:
    docs/phase3_index_analysis.md

Do NOT add indexes yet — this script is read-only investigation only.
"""

import os, re, sys, textwrap
from datetime import date, timedelta
from pathlib import Path

# ── read .env ─────────────────────────────────────────────────────────────────
kv = {}
env_path = Path(__file__).parent.parent / ".env"
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip(); v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        kv[k] = v

# Set env for db_manager
for k, v in kv.items():
    os.environ[k] = v
os.environ['APP_ENV']   = 'LOCAL'
os.environ['DEBUG']     = 'true'
os.environ['DB_HOST']   = kv.get('STG_DB_HOST', '')
os.environ['DB_PORT']   = kv.get('STG_DB_PORT', '3306')
os.environ['DB_NAME']   = kv.get('STG_DB_NAME', '')
os.environ['DB_USER']   = kv.get('STG_DB_USER', '')
os.environ['DB_PASSWORD'] = kv.get('STG_DB_PASSWORD', '')
os.environ['ENABLE_SSL'] = 'true'

app_dir = Path(__file__).parent.parent / "app"
sys.path.insert(0, str(app_dir))

import pymysql

ssl_ca = str(Path(__file__).parent.parent / "DigiCertGlobalRootG2.crt.pem")
conn = pymysql.connect(
    host=kv['STG_DB_HOST'],
    port=int(kv.get('STG_DB_PORT', 3306)),
    user=kv['STG_DB_USER'],
    password=kv['STG_DB_PASSWORD'],
    database=kv['STG_DB_NAME'],
    ssl={'ca': ssl_ca},
    connect_timeout=30,
    read_timeout=120,
    write_timeout=30,
)
print("✓ Connected to Azure MySQL STG")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
output_lines = []

def out(s=""):
    print(s)
    output_lines.append(s)

def run_raw(sql, params=None):
    cur = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    cur.close()
    return rows, cols

def md_table(rows, cols):
    if not cols:
        return "(no columns)"
    widths = [len(str(c)) for c in cols]
    for row in rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(str(v) if v is not None else 'NULL'))
    sep  = '|' + '|'.join('-'*w for w in widths) + '|'
    hdr  = '|' + '|'.join(str(c).ljust(widths[i]) for i, c in enumerate(cols)) + '|'
    lines = [hdr, sep]
    for row in rows:
        lines.append('|' + '|'.join(str(v if v is not None else 'NULL').ljust(widths[i]) for i, v in enumerate(row)) + '|')
    return '\n'.join(lines)

def section(title, level=2):
    prefix = '#' * level
    out(f"\n{prefix} {title}\n")

def explain_analyze(label, sql, params=None):
    """Run EXPLAIN ANALYZE and EXPLAIN (for row estimates), format output."""
    out(f"\n### {label}\n")
    out("**Query:**")
    out("```sql")
    clean = textwrap.dedent(sql).strip()
    out(clean)
    out("```")
    if params:
        out(f"\n**Parameters:** `{params}`\n")

    # EXPLAIN (estimated rows, access type, key)
    try:
        exp_sql = "EXPLAIN " + clean
        rows, cols = run_raw(exp_sql, params)
        out("\n**EXPLAIN (estimated):**")
        out("```")
        for row in rows:
            for col, val in zip(cols, row):
                if val is not None:
                    out(f"  {col}: {val}")
            out("")
        out("```")
    except Exception as e:
        out(f"\n**EXPLAIN error:** {e}\n")

    # EXPLAIN ANALYZE (actual rows, actual time)
    try:
        ana_sql = "EXPLAIN ANALYZE " + clean
        rows, cols = run_raw(ana_sql, params)
        out("\n**EXPLAIN ANALYZE (actual):**")
        out("```")
        for row in rows:
            # MySQL EXPLAIN ANALYZE returns one EXPLAIN column
            for v in row:
                if v:
                    out(str(v))
        out("```")
    except Exception as e:
        out(f"\n**EXPLAIN ANALYZE error:** {e}\n")

# ─────────────────────────────────────────────────────────────────────────────
# Date parameters matching the app
# ─────────────────────────────────────────────────────────────────────────────
# Month view: June 2026 (anchor = today 2026-06-11)
today = date(2026, 6, 11)
first = today.replace(day=1)
days_back = (first.weekday() + 1) % 7
month_start = first - timedelta(days=days_back)   # 2026-05-31
month_end   = month_start + timedelta(days=41)     # 2026-07-11

# Year view: 2026
year_start = date(2026, 1, 1)
year_end   = date(2026, 12, 31)

# Single company
SAMPLE_TICKER = "AAPL"

out("# Phase 3 — Index Investigation")
out(f"\n**Generated:** {date.today().isoformat()}")
out(f"**DB:** `{kv.get('STG_DB_NAME', '')}` @ `{kv.get('STG_DB_HOST', '')}`")
out("\n> Read-only investigation. No indexes added yet.")

# ─────────────────────────────────────────────────────────────────────────────
# SHOW INDEX
# ─────────────────────────────────────────────────────────────────────────────
section("SHOW INDEX")

tables = [
    'coreiq_nasdaq_earnings_calendar',
    'coreiq_yf_earnings_calendar',
    'coreiq_av_earnings_call_transcripts',
    'coreiq_ir_websites',
    'company_ir_websites',
]
for t in tables:
    out(f"\n#### `{t}`\n")
    try:
        rows, cols = run_raw(f"SHOW INDEX FROM {t}")
        # Only key columns
        key_cols = ['Key_name','Non_unique','Seq_in_index','Column_name','Cardinality','Index_type','Comment']
        idx = [cols.index(c) for c in key_cols if c in cols]
        if idx:
            sub_cols = [cols[i] for i in idx]
            sub_rows = [[row[i] for i in idx] for row in rows]
        else:
            sub_cols, sub_rows = cols, list(rows)
        out(md_table(sub_rows, sub_cols))
        out(f"\n*({len(rows)} index records total)*")
    except Exception as e:
        out(f"ERROR: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Row counts
# ─────────────────────────────────────────────────────────────────────────────
section("Table Row Counts")
out("```")
for t in tables:
    try:
        rows, _ = run_raw(f"SELECT COUNT(*) FROM {t}")
        out(f"  {t}: {rows[0][0]:,}")
    except Exception as e:
        out(f"  {t}: ERROR {e}")
out("```")

# ─────────────────────────────────────────────────────────────────────────────
# The shared WHERE clause fragments (as used in get_calendar_events)
# ─────────────────────────────────────────────────────────────────────────────
# Note: MySQL EXPLAIN ANALYZE uses %s placeholders for pymysql.
# We inline dates as literals for the EXPLAIN because that's what the
# optimizer actually sees at query time with the values substituted.

NASDAQ_COLS = """
    ec.id,
    ec.ticker,
    ec.company_name   AS calendar_company_name,
    ec.earnings_date,
    ec.report_time,
    ec.fiscal_quarter_ending,
    QUARTER(ec.fiscal_quarter_ending) AS fiscal_q,
    YEAR(ec.fiscal_quarter_ending)    AS report_fiscal_year,
    ec.eps_actual,
    ec.eps_forecast,
    ec.surprise_pct,
    ec.market_cap,
    ec.num_estimates,
    ec.fetched_at_utc AS fetched_at_utc,
    'nasdaq'          AS data_source
"""

YF_COLS = """
    yf.id + 10000000  AS id,
    yf.ticker,
    yf.company_name   AS calendar_company_name,
    yf.earnings_date,
    'time-not-supplied' AS report_time,
    CONCAT(DATE_FORMAT(yf.earnings_date,'%b'),'/',YEAR(yf.earnings_date)) AS fiscal_quarter_ending,
    QUARTER(yf.earnings_date) AS fiscal_q,
    YEAR(yf.earnings_date)    AS report_fiscal_year,
    yf.reported_eps   AS eps_actual,
    yf.eps_estimate   AS eps_forecast,
    yf.surprise_pct,
    NULL              AS market_cap,
    NULL              AS num_estimates,
    yf.ingested_at    AS fetched_at_utc,
    'yf'              AS data_source
"""

OUTER = """
    SELECT id, ticker, calendar_company_name, earnings_date, report_time,
           fiscal_quarter_ending, fiscal_q, report_fiscal_year,
           eps_actual, eps_forecast, surprise_pct, market_cap,
           num_estimates, data_source
    FROM (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY ticker, fiscal_quarter_ending
            ORDER BY fetched_at_utc DESC,
                     CASE data_source WHEN 'nasdaq' THEN 0 ELSE 1 END ASC,
                     earnings_date DESC,
                     id DESC
        ) AS rn
        FROM (
            SELECT {nasdaq_cols}
            FROM coreiq_nasdaq_earnings_calendar ec
            WHERE ec.fiscal_quarter_ending IS NOT NULL
              {nasdaq_where}
            UNION ALL
            SELECT {yf_cols}
            FROM coreiq_yf_earnings_calendar yf
            WHERE yf.ticker IS NOT NULL
              {yf_where}
        ) combined
    ) ranked
    WHERE rn = 1
"""

section("EXPLAIN ANALYZE — Earnings Calendar Queries")

# ── 1. All-company, month window ──────────────────────────────────────────────
section("1. All-company, month window", level=3)
out(f"\n*Parameters:* `start_date={month_start}`, `end_date={month_end}` (42-day Sunday grid for June 2026)\n")
q1 = OUTER.format(
    nasdaq_cols=NASDAQ_COLS,
    yf_cols=YF_COLS,
    nasdaq_where=f"AND ec.earnings_date >= '{month_start}' AND ec.earnings_date <= '{month_end}'",
    yf_where=f"AND yf.earnings_date >= '{month_start}' AND yf.earnings_date <= '{month_end}'",
)
explain_analyze("All-company month window", q1)

# ── 2. All-company, year window ───────────────────────────────────────────────
section("2. All-company, year window", level=3)
out(f"\n*Parameters:* `start_date={year_start}`, `end_date={year_end}` (full year 2026)\n")
q2 = OUTER.format(
    nasdaq_cols=NASDAQ_COLS,
    yf_cols=YF_COLS,
    nasdaq_where=f"AND ec.earnings_date >= '{year_start}' AND ec.earnings_date <= '{year_end}'",
    yf_where=f"AND yf.earnings_date >= '{year_start}' AND yf.earnings_date <= '{year_end}'",
)
explain_analyze("All-company year window", q2)

# ── 3. Single-company, month window ──────────────────────────────────────────
section("3. Single-company, month window", level=3)
out(f"\n*Parameters:* `tickers=('{SAMPLE_TICKER}',)`, `start_date={month_start}`, `end_date={month_end}`\n")
q3 = OUTER.format(
    nasdaq_cols=NASDAQ_COLS,
    yf_cols=YF_COLS,
    nasdaq_where=f"AND ec.ticker IN ('{SAMPLE_TICKER}') AND ec.earnings_date >= '{month_start}' AND ec.earnings_date <= '{month_end}'",
    yf_where=f"AND yf.ticker IN ('{SAMPLE_TICKER}') AND yf.earnings_date >= '{month_start}' AND yf.earnings_date <= '{month_end}'",
)
explain_analyze("Single-company month window", q3)

# ── 4. Single-company, year window ───────────────────────────────────────────
section("4. Single-company, year window", level=3)
out(f"\n*Parameters:* `tickers=('{SAMPLE_TICKER}',)`, `start_date={year_start}`, `end_date={year_end}`\n")
q4 = OUTER.format(
    nasdaq_cols=NASDAQ_COLS,
    yf_cols=YF_COLS,
    nasdaq_where=f"AND ec.ticker IN ('{SAMPLE_TICKER}') AND ec.earnings_date >= '{year_start}' AND ec.earnings_date <= '{year_end}'",
    yf_where=f"AND yf.ticker IN ('{SAMPLE_TICKER}') AND yf.earnings_date >= '{year_start}' AND yf.earnings_date <= '{year_end}'",
)
explain_analyze("Single-company year window", q4)

# ── 5. Watchlist path note ────────────────────────────────────────────────────
section("5. Watchlist path", level=3)
out("""
**Watchlist mode does NOT use a ticker prefilter in the DB query.**

The current implementation:
1. Fetches all events for the visible date window (same query as All-company above).
2. Calls `get_watchlist_companies(watchlist_id)` to retrieve watchlist member rows.
3. Applies `_filter_events_by_watchlist(prefetched_events, watchlist_rows)` in Python memory.

Therefore, the relevant EXPLAIN ANALYZE for watchlist mode is **Query 1 (month)** or
**Query 2 (year)** — the same all-company date-window query.

The watchlist company lookup is a separate simple query:
```sql
SELECT ticker, company_name, sector, country, added_by
FROM coreiq_watchlist_companies
WHERE watchlist_id = <id>
```
This is a primary key / indexed lookup and is not a bottleneck.
""")

# ── 6. Transcript detail lookup ───────────────────────────────────────────────
section("6. Transcript detail lookup", level=3)
out(f"\n*Parameters:* `ticker='{SAMPLE_TICKER}'`\n")
q6 = f"""
    SELECT year, q, quarter
    FROM coreiq_av_earnings_call_transcripts
    WHERE ticker = '{SAMPLE_TICKER}'
      AND has_transcript = 1
    ORDER BY year DESC, q DESC
    LIMIT 80
"""
explain_analyze("Transcript lookup by ticker", q6)

# ── 7. IR website lookup ──────────────────────────────────────────────────────
section("7. IR website lookup (full table scan — both tables)", level=3)
out("""
The `_build_ir_website_lookup()` fetches **all rows** from both IR tables at startup
(cached via `@st.cache_data`). No per-ticker filter is applied in SQL.
""")
for ir_table in ("coreiq_ir_websites", "company_ir_websites"):
    q_ir = f"""
        SELECT *
        FROM {ir_table}
        WHERE ticker IS NOT NULL
          AND TRIM(ticker) != ''
    """
    explain_analyze(f"IR lookup — {ir_table} (all rows)", q_ir)

# ── 8. available_tickers ─────────────────────────────────────────────────────
section("8. get_available_tickers", level=3)
q8_nasdaq = """
    SELECT DISTINCT ec.ticker, c.company_name AS name
    FROM coreiq_nasdaq_earnings_calendar ec
    LEFT JOIN coreiq_companies c ON ec.ticker = c.ticker
    WHERE ec.ticker IS NOT NULL
    ORDER BY name
"""
# The actual query may differ — let's find it
try:
    from data.repository import EarningsCalendarRepository
    import inspect
    src = inspect.getsource(EarningsCalendarRepository.get_available_tickers)
    # find the SQL string in the source
    sql_match = re.search(r'"""(.*?)"""', src, re.DOTALL)
    if sql_match:
        q8 = sql_match.group(1).strip()
        explain_analyze("get_available_tickers", q8)
    else:
        out("Could not extract SQL from source — check repository.py manually.")
except Exception as e:
    out(f"Could not import repository: {e}")
    out("Run manually: EXPLAIN SELECT ... FROM coreiq_nasdaq_earnings_calendar ...")

conn.close()

# ─────────────────────────────────────────────────────────────────────────────
# Write output
# ─────────────────────────────────────────────────────────────────────────────
out_path = Path(__file__).parent.parent / "docs" / "phase3_index_analysis.md"
with open(out_path, 'w') as f:
    f.write('\n'.join(output_lines))

print(f"\n✓ Output written to {out_path}")
