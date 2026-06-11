"""
Load filing metrics from FINAL_FACTS_FILTERED.json into MySQL coreiq_filing_metrics table.

Scans data/filings/{TICKER}/{YEAR}/{DOC_TYPE}/FINAL_FACTS_FILTERED.json
Flat JSON array — each record maps directly to a DB row.
Idempotent: deletes existing rows for same ticker/year/doc_type before insert.

Usage:
    python scripts/load_filings_to_db.py                    # Load all
    python scripts/load_filings_to_db.py AAPL               # Load one ticker
    python scripts/load_filings_to_db.py AAPL 2025          # Load one ticker+year
    python scripts/load_filings_to_db.py AAPL 2025 10-K     # Load specific filing
"""
import os
import sys
import json
import pymysql
from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

FILINGS_DIR = os.path.join(PROJECT_ROOT, "data", "filings")
JSON_FILENAME = "FINAL_FACTS_FILTERED.json"

# Columns we extract from JSON → DB (order matters for INSERT)
DB_COLUMNS = [
    "ticker", "company_name", "fiscal_year", "doc_type",
    "concept", "context_ref", "value", "unit_ref", "decimals", "numeric_value",
    "period_type", "period_start", "period_end", "period_instant", "fiscal_period",
    "label", "original_label", "standard_concept", "balance",
    "statement_type", "statement_role",
    "is_dimensioned", "dimension", "member",
    "dimension_label", "dimension_member_label", "full_dimension_label",
    "ixbrl_id", "start_position", "end_position",
]

INSERT_SQL = f"""
    INSERT INTO coreiq_filing_metrics ({', '.join(DB_COLUMNS)})
    VALUES ({', '.join(['%s'] * len(DB_COLUMNS))})
"""


def get_connection():
    import ssl as _ssl
    kwargs = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "chainxydata_stg"),
        "charset": "utf8mb4",
    }
    # Add SSL if enabled (required for Azure MySQL STG)
    if os.getenv("ENABLE_SSL", "").lower() == "true":
        _ca = os.getenv("SSL_CA", "")
        if _ca and os.path.isfile(_ca):
            kwargs["ssl"] = _ssl.create_default_context(cafile=_ca)
    return pymysql.connect(**kwargs)


def _load_company_names():
    """One-time lookup: ticker → company_name from coreiq_companies."""
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, COALESCE(name_coresight, name) FROM coreiq_companies WHERE ticker IS NOT NULL")
            names = {row[0].strip(): row[1] for row in cur.fetchall() if row[0]}
        conn.close()
        return names
    except Exception:
        return {}

COMPANY_NAME_MAP = _load_company_names()


def record_to_row(record: dict, ticker: str, company_name: str, year: int, doc_type: str) -> tuple:
    """Convert a JSON record to a DB row tuple matching DB_COLUMNS order."""
    loc = record.get("html_location") or {}
    return (
        ticker,
        company_name,
        year,
        doc_type,
        record.get("concept"),
        record.get("context_ref"),
        record.get("value"),
        record.get("unit_ref"),
        record.get("decimals"),
        record.get("numeric_value"),
        record.get("period_type"),
        record.get("period_start"),
        record.get("period_end"),
        record.get("period_instant"),
        record.get("fiscal_period"),
        record.get("label"),
        record.get("original_label"),
        record.get("standard_concept"),
        record.get("balance"),
        record.get("statement_type"),
        record.get("statement_role"),
        record.get("is_dimensioned", False),
        record.get("dimension"),
        record.get("member"),
        record.get("dimension_label"),
        record.get("dimension_member_label"),
        record.get("full_dimension_label"),
        loc.get("ixbrl_id"),
        loc.get("start_position"),
        loc.get("end_position"),
    )


def load_filing(conn, ticker: str, year: str, doc_type: str, json_path: str):
    """Load one FINAL_FACTS_FILTERED.json into DB."""
    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    if not records:
        print(f"  SKIP {ticker}/{year}/{doc_type} — empty JSON")
        return 0

    # Skip calculated entries — they are appended to the JSON by calculate_derived_metrics.py
    # and will be re-inserted by that script with source='calculated'. Loading them here
    # would create duplicates with source='xbrl' (DB default) and no ixbrl_id.
    records = [r for r in records if r.get("source") != "calculated"]

    year_int = int(year)
    company_name = COMPANY_NAME_MAP.get(ticker, ticker)
    rows = [record_to_row(r, ticker, company_name, year_int, doc_type) for r in records]

    with conn.cursor() as cur:
        # Idempotent: delete existing rows for this filing
        cur.execute(
            "DELETE FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type=%s",
            (ticker, year_int, doc_type),
        )
        deleted = cur.rowcount

        # Batch insert
        cur.executemany(INSERT_SQL, rows)
        inserted = cur.rowcount

    conn.commit()
    if deleted:
        print(f"  {ticker}/{year}/{doc_type}: replaced {deleted} → {inserted} rows")
    else:
        print(f"  {ticker}/{year}/{doc_type}: inserted {inserted} rows")
    return inserted


def scan_and_load(filter_ticker=None, filter_year=None, filter_doc_type=None):
    """Scan filings directory and load all matching FINAL_FACTS_FILTERED.json files."""
    conn = get_connection()
    total = 0

    for ticker in sorted(os.listdir(FILINGS_DIR)):
        ticker_dir = os.path.join(FILINGS_DIR, ticker)
        if not os.path.isdir(ticker_dir) or ticker.startswith((".", "_")):
            continue
        if filter_ticker and ticker != filter_ticker:
            continue

        for year_name in sorted(os.listdir(ticker_dir)):
            year_dir = os.path.join(ticker_dir, year_name)
            if not os.path.isdir(year_dir) or not year_name.isdigit():
                continue
            if filter_year and year_name != filter_year:
                continue

            for doc_type_dir in sorted(os.listdir(year_dir)):
                doc_dir = os.path.join(year_dir, doc_type_dir)
                if not os.path.isdir(doc_dir):
                    continue
                if filter_doc_type and doc_type_dir != filter_doc_type:
                    continue

                json_path = os.path.join(doc_dir, JSON_FILENAME)
                if not os.path.isfile(json_path):
                    continue

                total += load_filing(conn, ticker, year_name, doc_type_dir, json_path)

    conn.close()
    print(f"\nTotal rows loaded: {total}")


if __name__ == "__main__":
    args = sys.argv[1:]
    filter_ticker = args[0] if len(args) >= 1 else None
    filter_year = args[1] if len(args) >= 2 else None
    filter_doc_type = args[2] if len(args) >= 3 else None

    print(f"Loading filings from: {FILINGS_DIR}")
    if filter_ticker:
        print(f"  Filter: ticker={filter_ticker}, year={filter_year or 'all'}, doc_type={filter_doc_type or 'all'}")
    else:
        print(f"  Loading ALL tickers/years/doc_types")

    scan_and_load(filter_ticker, filter_year, filter_doc_type)
