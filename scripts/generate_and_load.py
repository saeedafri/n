#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════
  Script 2: Generate Data, Load to DB, Cleanup JSONs
═══════════════════════════════════════════════════════════════════════

Expects filing.html to already exist in the proper folder structure.
This script:
  1. Generates JSONs from XBRL data (FINAL_FACTS_FILTERED, FINANCIAL_RATIOS,
     SECTION_CACHE, CREDIT_RATING, STORE_COUNT)
  2. Loads ALL data into DB — SKIPS if data already exists (no duplicates, ever)
  3. Deletes all JSON files after successful DB load

JSON → DB Mapping:
  ┌──────────────────────────────┬────────────────────┬──────────────────────┐
  │ JSON File                    │ DB Table           │ Source Tag           │
  ├──────────────────────────────┼────────────────────┼──────────────────────┤
  │ FINAL_FACTS_FILTERED.json    │ coreiq_filing_metrics     │ (default/xbrl)       │
  │ FINANCIAL_RATIOS.json        │ coreiq_filing_metrics     │ financial_ratio      │
  │ CREDIT_RATING.json           │ coreiq_filing_metrics     │ credit_rating        │
  │ STORE_COUNT.json             │ coreiq_filing_metrics     │ store_count          │
  │ SECTION_CACHE.json           │ NOT in DB          │ local cache only     │
  └──────────────────────────────┴────────────────────┴──────────────────────┘

Deduplication:
  For FINAL_FACTS: checks row count WHERE source NOT IN ('financial_ratio',
    'credit_rating', 'store_count', 'calculated'). Match → skip.
  For FINANCIAL_RATIOS: checks row count WHERE source='financial_ratio'. Match → skip.
  For CREDIT_RATING: checks row count WHERE source='credit_rating'. Match → skip.
  For STORE_COUNT: checks row count WHERE source='store_count'. Match → skip.

Usage:
    python scripts/generate_and_load.py AAPL 2024                 # 10-K
    python scripts/generate_and_load.py AAPL 2024 --form 10-Q     # 10-Q
    python scripts/generate_and_load.py AAPL 2020 2021 2022 2023 2024 2025
    python scripts/generate_and_load.py AAPL 2024 --skip-json     # DB load only (use existing JSONs)

For STG DB:
    DB_HOST=$STG_DB_HOST DB_PORT=$STG_DB_PORT DB_NAME=$STG_DB_NAME \\
    DB_USER=$STG_DB_USER DB_PASSWORD=$STG_DB_PASSWORD \\
    python scripts/generate_and_load.py AAPL 2024
═══════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import argparse
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# ── Edgar cache ──
_EDGAR_CACHE = "/tmp/edgar_cache"
os.makedirs(_EDGAR_CACHE, exist_ok=True)
os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", _EDGAR_CACHE)
os.environ.setdefault("EDGAR_CACHE_DIR", _EDGAR_CACHE)

import pymysql

OUTPUT_BASE = PROJECT_ROOT / "data" / "filings"


# ══════════════════════════════════════════════════════════════════════
#  DB CONNECTION (SSL-aware)
# ══════════════════════════════════════════════════════════════════════

def get_conn():
    import ssl as _ssl
    host = os.getenv("DB_HOST", "localhost")
    kwargs = {
        "host": host,
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "chainxydata_stg"),
        "charset": "utf8mb4",
        "read_timeout": 600,
        "write_timeout": 600,
        "connect_timeout": 60,
    }
    # Only use SSL for non-localhost connections (e.g., Azure MySQL)
    if host not in ("localhost", "127.0.0.1") and os.getenv("ENABLE_SSL", "").lower() == "true":
        _ca = os.getenv("SSL_CA", "")
        if _ca and os.path.isfile(_ca):
            kwargs["ssl"] = _ssl.create_default_context(cafile=_ca)
    conn = pymysql.connect(**kwargs, autocommit=True)
    with conn.cursor() as cur:
        cur.execute('SET SESSION innodb_lock_wait_timeout = 600')
        cur.execute('SET SESSION max_execution_time = 600000')
        cur.execute('SET SESSION net_read_timeout = 600')
        cur.execute('SET SESSION net_write_timeout = 600')
        try:
            cur.execute('SET SESSION max_allowed_packet = 134217728')
        except Exception:
            pass  # max_allowed_packet may be read-only in some environments
    return conn


def get_sqlalchemy_engine():
    """Get SQLAlchemy engine for credit_rating / store_count scripts."""
    from sqlalchemy import create_engine
    user = os.getenv("DB_USER", "root")
    pw   = os.getenv("DB_PASSWORD", "")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    db   = os.getenv("DB_NAME", "chainxydata_stg")

    ssl_args = ""
    if os.getenv("ENABLE_SSL", "").lower() == "true":
        _ca = os.getenv("SSL_CA", "")
        if _ca and os.path.isfile(_ca):
            ssl_args = f"?ssl_ca={_ca}"

    url = f"mysql+pymysql://{user}:{pw}@{host}:{port}/{db}{ssl_args}"
    return create_engine(url)


# ══════════════════════════════════════════════════════════════════════
#  HELPER: Check DB row count by source
# ══════════════════════════════════════════════════════════════════════

def db_count_by_source(conn, ticker, year, doc_type, source_filter):
    """Count rows in coreiq_filing_metrics for a given source filter."""
    with conn.cursor() as cur:
        if source_filter is None:
            # XBRL facts: source IS NULL or source NOT IN special sources
            cur.execute(
                """SELECT COUNT(*) FROM coreiq_filing_metrics
                   WHERE ticker=%s AND fiscal_year=%s AND doc_type=%s
                     AND (source IS NULL OR source NOT IN ('financial_ratio','credit_rating','store_count','calculated'))""",
                (ticker, year, doc_type),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type=%s AND source=%s",
                (ticker, year, doc_type, source_filter),
            )
        return cur.fetchone()[0]


# ══════════════════════════════════════════════════════════════════════
#  STEP 1: GENERATE JSONs (from XBRL via SEC EDGAR)
# ══════════════════════════════════════════════════════════════════════

def generate_xbrl_jsons(ticker: str, year: int, form: str) -> bool:
    """Run export_xbrl_facts → generates FINAL_FACTS_FILTERED + FINANCIAL_RATIOS."""
    from scripts.export_xbrl_facts import Config, process_company_year, set_identity

    cfg = Config()
    cfg.TICKERS = [ticker]
    cfg.YEARS = [year]
    cfg.FORM = form
    cfg.SKIP_EXISTING = False
    identity = os.getenv("EDGAR_IDENTITY", cfg.SEC_IDENTITY)
    set_identity(identity)

    try:
        return process_company_year(ticker, year, cfg)
    except Exception as e:
        print(f"  ❌ XBRL JSON generation failed: {e}")
        traceback.print_exc(limit=3)
        return False


def generate_section_cache(ticker: str, year: int, doc_type: str) -> bool:
    """Run enrich_from_edgartools → generates SECTION_CACHE.json."""
    try:
        from scripts.enrich_from_edgartools import process_filing
        doc_dir = OUTPUT_BASE / ticker / str(year) / doc_type
        html_path = doc_dir / "filing.html"
        if not html_path.exists():
            return False
        cache_path = doc_dir / "SECTION_CACHE.json"
        if cache_path.exists():
            return True  # already exists
        process_filing(ticker, year, doc_type)
        return cache_path.exists()
    except Exception as e:
        print(f"  ⚠️  SECTION_CACHE generation failed: {e}")
        return False


def generate_credit_rating(ticker: str, year: int, doc_type: str) -> bool:
    """Run extract_credit_ratings → generates CREDIT_RATING.json."""
    try:
        from scripts.extract_credit_ratings import process_filing
        result = process_filing(ticker, year, doc_type, use_llm=True, force=False)
        return result is not None
    except Exception as e:
        print(f"  ⚠️  CREDIT_RATING generation failed: {e}")
        return False


def generate_store_count(ticker: str, year: int, doc_type: str) -> bool:
    """Run extract_store_counts → generates STORE_COUNT.json."""
    try:
        from scripts.extract_store_counts import process_filing
        result = process_filing(ticker, year, doc_type, use_llm=True, force=False)
        return result is not None
    except Exception as e:
        print(f"  ⚠️  STORE_COUNT generation failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
#  STEP 2: LOAD DATA TO DB (with per-source deduplication)
# ══════════════════════════════════════════════════════════════════════

def load_final_facts(conn, ticker, year, doc_type) -> dict:
    """Load FINAL_FACTS_FILTERED.json → coreiq_filing_metrics (source=xbrl/NULL)."""
    json_path = OUTPUT_BASE / ticker / str(year) / doc_type / "FINAL_FACTS_FILTERED.json"
    if not json_path.exists():
        return {"action": "no_json", "rows": 0}

    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    if not records:
        return {"action": "empty_json", "rows": 0}

    # Filter out calculated/special source entries
    records = [r for r in records if r.get("source") not in ("calculated", "financial_ratio", "credit_rating", "store_count")]
    json_count = len(records)

    db_count = db_count_by_source(conn, ticker, year, doc_type, None)

    if db_count > 0 and db_count == json_count:
        return {"action": "skipped", "rows": db_count}

    if db_count > 0:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type=%s
                   AND (source IS NULL OR source NOT IN ('financial_ratio','credit_rating','store_count','calculated'))""",
                (ticker, year, doc_type),
            )

    company_name = _get_company_name(conn, ticker)
    from scripts.load_filings_to_db import record_to_row, INSERT_SQL
    rows = [record_to_row(r, ticker, company_name, year, doc_type) for r in records]

    # Chunked insert to prevent connection timeout on large batches
    BATCH_SIZE = 10
    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        conn.ping(reconnect=True)  # Ensure connection is alive
        with conn.cursor() as cur:
            cur.executemany(INSERT_SQL, batch)
            inserted += cur.rowcount

    return {"action": "inserted" if db_count == 0 else "replaced", "rows": inserted}


def load_financial_ratios(conn, ticker, year, doc_type) -> dict:
    """Load FINANCIAL_RATIOS.json → coreiq_filing_metrics (source='financial_ratio')."""
    json_path = OUTPUT_BASE / ticker / str(year) / doc_type / "FINANCIAL_RATIOS.json"
    if not json_path.exists():
        return {"action": "no_json", "rows": 0}

    db_count = db_count_by_source(conn, ticker, year, doc_type, "financial_ratio")
    if db_count > 0:
        return {"action": "skipped", "rows": db_count}

    # Use supplementary loader (it does delete+insert internally)
    try:
        supp_conn = get_conn()
        from scripts.load_supplementary_json import process as supp_process
        supp_process(supp_conn, ticker, year, doc_type, OUTPUT_BASE / ticker / str(year) / doc_type)
        new_count = db_count_by_source(conn, ticker, year, doc_type, "financial_ratio")
        supp_conn.close()
        return {"action": "inserted", "rows": new_count}
    except Exception as e:
        return {"action": "error", "rows": 0, "error": str(e)}


def load_credit_rating(conn, ticker, year, doc_type) -> dict:
    """Load CREDIT_RATING.json → coreiq_filing_metrics (source='credit_rating')."""
    json_path = OUTPUT_BASE / ticker / str(year) / doc_type / "CREDIT_RATING.json"
    if not json_path.exists():
        return {"action": "no_json", "rows": 0}

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    ratings = data.get("ratings", [])
    if not ratings:
        return {"action": "no_data", "rows": 0}

    db_count = db_count_by_source(conn, ticker, year, doc_type, "credit_rating")
    if db_count > 0 and db_count == len(ratings):
        return {"action": "skipped", "rows": db_count}

    # Use the existing inserter (it does idempotent delete+insert)
    try:
        engine = get_sqlalchemy_engine()
        from scripts.extract_credit_ratings import insert_credit_rating_to_db
        with engine.begin() as sa_conn:
            insert_credit_rating_to_db(
                sa_conn, ticker, year, doc_type,
                ratings=ratings,
                investment_grade=data.get("investment_grade"),
                confidence=data.get("confidence", 0.0),
                extraction_method=data.get("extraction_method", ""),
                notes=data.get("notes", ""),
            )
        new_count = db_count_by_source(conn, ticker, year, doc_type, "credit_rating")
        return {"action": "inserted" if db_count == 0 else "replaced", "rows": new_count}
    except Exception as e:
        return {"action": "error", "rows": 0, "error": str(e)}


def load_store_count(conn, ticker, year, doc_type) -> dict:
    """Load STORE_COUNT.json → coreiq_filing_metrics (source='store_count')."""
    json_path = OUTPUT_BASE / ticker / str(year) / doc_type / "STORE_COUNT.json"
    if not json_path.exists():
        return {"action": "no_json", "rows": 0}

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    store_count = data.get("store_count")
    if store_count is None:
        return {"action": "no_data", "rows": 0}

    db_count = db_count_by_source(conn, ticker, year, doc_type, "store_count")
    if db_count > 0:
        return {"action": "skipped", "rows": db_count}

    # Use the existing inserter (idempotent delete+insert)
    try:
        engine = get_sqlalchemy_engine()
        from scripts.extract_store_counts import insert_store_count_to_db
        with engine.begin() as sa_conn:
            insert_store_count_to_db(
                sa_conn, ticker, year, doc_type,
                store_count=store_count,
                store_type=data.get("store_type", "stores"),
                as_of_date=data.get("as_of_date"),
                source_sentence=data.get("source_sentence", ""),
                extraction_method=data.get("extraction_method", ""),
                confidence=data.get("confidence", 0.0),
                section_source=data.get("section", ""),
                notes=data.get("notes", ""),
            )
        return {"action": "inserted", "rows": 1}
    except Exception as e:
        return {"action": "error", "rows": 0, "error": str(e)}


def _get_company_name(conn, ticker):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(name_coresight, name) FROM coreiq_companies WHERE ticker=%s LIMIT 1",
                (ticker,),
            )
            row = cur.fetchone()
            return row[0] if row else ticker
    except Exception:
        return ticker


# ══════════════════════════════════════════════════════════════════════
#  STEP 3: DELETE JSONs
# ══════════════════════════════════════════════════════════════════════

def delete_jsons(ticker: str, year: int, doc_type: str) -> int:
    """Delete all JSON files in the doc_type directory."""
    doc_dir = OUTPUT_BASE / ticker / str(year) / doc_type
    if not doc_dir.exists():
        return 0

    count = 0
    for json_file in doc_dir.glob("*.json"):
        json_file.unlink()
        count += 1
    return count


# ══════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════

def process_one(conn, ticker: str, year: int, doc_type: str, form: str,
                skip_json: bool = False, keep_json: bool = False) -> dict:
    """Process one ticker/year/doc_type: generate → load → cleanup."""
    label = f"{ticker}/{year}/{doc_type}"
    result = {
        "label": label,
        "facts": {"action": "skipped", "rows": 0},
        "ratios": {"action": "skipped", "rows": 0},
        "credit": {"action": "skipped", "rows": 0},
        "store": {"action": "skipped", "rows": 0},
        "jsons_deleted": 0,
    }

    # ── Step 1: Generate JSONs ──
    if not skip_json:
        json_path = OUTPUT_BASE / ticker / str(year) / doc_type / "FINAL_FACTS_FILTERED.json"
        if not json_path.exists():
            print(f"    ⏳ [{label}] Step 1/4: Generating XBRL JSONs...")
            generate_xbrl_jsons(ticker, year, form)
            print(f"    ✅ [{label}] XBRL JSONs generated")
        else:
            print(f"    ✓ [{label}] XBRL JSONs already exist")

        # Only generate section cache / credit / store for 10-K
        if "10-K" in doc_type:
            cache_path = OUTPUT_BASE / ticker / str(year) / doc_type / "SECTION_CACHE.json"
            if not cache_path.exists():
                print(f"  📄 [{label}] Generating SECTION_CACHE...")
                generate_section_cache(ticker, year, doc_type)

            cr_path = OUTPUT_BASE / ticker / str(year) / doc_type / "CREDIT_RATING.json"
            if not cr_path.exists():
                print(f"  📄 [{label}] Extracting Credit Rating...")
                generate_credit_rating(ticker, year, doc_type)

            sc_path = OUTPUT_BASE / ticker / str(year) / doc_type / "STORE_COUNT.json"
            if not sc_path.exists():
                print(f"  📄 [{label}] Extracting Store Count...")
                generate_store_count(ticker, year, doc_type)

    # ── Step 2: Load ALL data to DB (each with its own dedup) ──
    def _log(name, res):
        if res["action"] == "skipped":
            print(f"  ✅ [{label}] {name}: {res['rows']} rows exist — SKIPPED")
        elif res["action"] == "inserted":
            print(f"  ✅ [{label}] {name}: {res['rows']} NEW rows inserted")
        elif res["action"] == "replaced":
            print(f"  🔄 [{label}] {name}: replaced with {res['rows']} rows")
        elif res["action"] == "error":
            print(f"  ❌ [{label}] {name}: error — {res.get('error', 'unknown')}")
        # no_json, no_data, empty_json → silent

    # ── Step 2: Load to DB ──
    print(f"    ⏳ [{label}] Step 2/4: Loading FINAL_FACTS to DB...")
    result["facts"] = load_final_facts(conn, ticker, year, doc_type)
    _log("FINAL_FACTS", result["facts"])
    
    print(f"    ⏳ [{label}] Step 3/4: Loading FINANCIAL_RATIOS to DB...")
    result["ratios"] = load_financial_ratios(conn, ticker, year, doc_type)
    _log("FINANCIAL_RATIOS", result["ratios"])

    # 2c. CREDIT_RATING (10-K only)
    if "10-K" in doc_type:
        result["credit"] = load_credit_rating(conn, ticker, year, doc_type)
        _log("CREDIT_RATING", result["credit"])

    # 2d. STORE_COUNT (10-K only)
    if "10-K" in doc_type:
        result["store"] = load_store_count(conn, ticker, year, doc_type)
        _log("STORE_COUNT", result["store"])

    # ── Step 4: Delete ALL JSONs (unless --keep-json) ──
    print(f"    ⏳ [{label}] Step 4/4: Cleaning up JSON files...")
    if keep_json:
        json_count = len(list((OUTPUT_BASE / ticker / str(year) / doc_type).glob('*.json')))
        if json_count > 0:
            print(f"    📁 [{label}] Kept {json_count} JSON files (--keep-json)")
    else:
        deleted = delete_jsons(ticker, year, doc_type)
        result["jsons_deleted"] = deleted
        if deleted > 0:
            print(f"    🗑️  [{label}] Deleted {deleted} JSON files")
    print(f"    ✅ [{label}] Cleanup complete")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Generate ALL data from XBRL/HTML, load to DB (no duplicates), delete JSONs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
JSON Files Handled:
  FINAL_FACTS_FILTERED.json  → coreiq_filing_metrics (source=xbrl)
  FINANCIAL_RATIOS.json      → coreiq_filing_metrics (source=financial_ratio)
  CREDIT_RATING.json         → coreiq_filing_metrics (source=credit_rating)         [10-K only]
  STORE_COUNT.json           → coreiq_filing_metrics (source=store_count)           [10-K only]
  SECTION_CACHE.json         → NOT in DB (local cache, deleted after use)

Examples:
  python scripts/generate_and_load.py AAPL --years 2024
  python scripts/generate_and_load.py AAPL AMZN WMT --years 2024 2025
  python scripts/generate_and_load.py AAPL --years 2024 --form 10-Q
  python scripts/generate_and_load.py TSM --years 2024 --form 20-F    # Foreign company
  python scripts/generate_and_load.py --all --years 2020 2021 2022 2023 2024 2025
  python scripts/generate_and_load.py AAPL --years 2024 --skip-json
        """,
    )
    parser.add_argument("tickers", type=str, nargs="*", help="Company ticker(s) (e.g. AAPL AMZN WMT)")
    parser.add_argument("--years", type=int, nargs="+", required=True, help="Year(s) to process")
    parser.add_argument("--form", type=str, default="both",
                        help="Form type: 10-K, 10-Q, 20-F, or both (default: both)")
    parser.add_argument("--skip-json", action="store_true",
                        help="Skip JSON generation (only load existing JSONs to DB)")
    parser.add_argument("--keep-json", action="store_true",
                        help="Keep JSON files after DB load (don't delete)")
    parser.add_argument("--all", action="store_true",
                        help="Process ALL companies in data/filings/")
    args = parser.parse_args()

    if args.all:
        tickers = sorted([d.name for d in OUTPUT_BASE.iterdir() if d.is_dir() and not d.name.startswith('.')])
    elif args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        parser.error("Provide ticker(s) or use --all")
        return

    form = args.form.upper()
    conn = get_conn()

    print(f"\n{'═' * 60}")
    print(f"  GENERATE & LOAD DATA (ALL JSON TYPES)")
    print(f"  Tickers: {len(tickers)}  |  Form: {form}  |  Years: {args.years}")
    print(f"  DB: {os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '3306')}/{os.getenv('DB_NAME', 'chainxydata_stg')}")
    print(f"{'═' * 60}")

    summary = {"inserted": 0, "skipped": 0, "replaced": 0, "jsons_deleted": 0}

    def _accum(res):
        if res["action"] == "inserted": summary["inserted"] += res["rows"]
        elif res["action"] == "skipped": summary["skipped"] += res["rows"]
        elif res["action"] == "replaced": summary["replaced"] += res["rows"]

    # Calculate total tasks for progress
    total_tasks = len(tickers) * len(args.years)
    current_task = 0
    
    for ticker in tickers:
        for year in args.years:
            current_task += 1
            progress_pct = (current_task / total_tasks) * 100
            print(f"\n{'─' * 60}")
            print(f"  [{current_task}/{total_tasks}] {progress_pct:.1f}% | {ticker} — FY{year}")
            print(f"{'─' * 60}")

            if form == "BOTH":
                forms_to_run = [("10-K", ["10-K"]), ("10-Q", ["10-Q-Q1", "10-Q-Q2", "10-Q-Q3"])]
            elif form in ("10-K",):
                forms_to_run = [("10-K", ["10-K"])]
            elif form in ("10-Q", "10Q"):
                forms_to_run = [("10-Q", ["10-Q-Q1", "10-Q-Q2", "10-Q-Q3"])]
            elif form in ("20-F", "20F"):
                forms_to_run = [("20-F", ["20-F"])]
            else:
                forms_to_run = [(form, [form])]

            doc_type_total = sum(len(docs) for _, docs in forms_to_run)
            doc_type_current = 0
            
            for form_name, doc_types in forms_to_run:
                for doc_type in doc_types:
                    doc_type_current += 1
                    print(f"\n  [DOC {doc_type_current}/{doc_type_total}] Processing {doc_type}...")
                    result = process_one(conn, ticker, year, doc_type, form_name, args.skip_json, args.keep_json)
                    for key in ("facts", "ratios", "credit", "store"):
                        _accum(result[key])
                    summary["jsons_deleted"] += result["jsons_deleted"]
                    print(f"  [DOC {doc_type_current}/{doc_type_total}] {doc_type} complete ✓")

    conn.close()

    print(f"\n{'═' * 60}")
    print(f"  SUMMARY")
    print(f"{'═' * 60}")
    print(f"  New rows inserted  : {summary['inserted']:,}")
    print(f"  Rows skipped (dup) : {summary['skipped']:,}")
    print(f"  Rows replaced      : {summary['replaced']:,}")
    print(f"  JSONs deleted      : {summary['jsons_deleted']}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
