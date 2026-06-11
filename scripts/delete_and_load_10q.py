#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
  DELETE & LOAD 10-Q - Safe DB Operation (One Ticker/Year at a time)
═══════════════════════════════════════════════════════════════════════════════

Purpose: Safely replace 10-Q data in DB for a specific ticker/year

Process:
  1. Connect to DB
  2. DELETE all rows for ticker/year WHERE doc_type LIKE '10-Q%'
  3. Run generate_and_load.py for that ticker/year with --form 10-Q
  4. Verify insertion

Usage:
    python scripts/delete_and_load_10q.py ACI 2024
    python scripts/delete_and_load_10q.py ACI 2024 --stg  # For STG DB
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime

# Setup paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import pymysql


def get_db_config(use_stg=False):
    """Get database configuration."""
    if use_stg:
        return {
            "host": os.getenv("STG_DB_HOST", ""),
            "port": int(os.getenv("STG_DB_PORT", "3306")),
            "user": os.getenv("STG_DB_USER", ""),
            "password": os.getenv("STG_DB_PASSWORD", ""),
            "database": os.getenv("STG_DB_NAME", "coresight_market_data_stg"),
        }
    else:
        return {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": int(os.getenv("DB_PORT", "3306")),
            "user": os.getenv("DB_USER", "root"),
            "password": os.getenv("DB_PASSWORD", ""),
            "database": os.getenv("DB_NAME", "chainxydata_stg"),
        }


def get_connection(use_stg=False):
    """Get database connection."""
    import ssl as _ssl
    kwargs = get_db_config(use_stg)
    kwargs["charset"] = "utf8mb4"
    # Extended timeouts for large DELETE operations
    kwargs["connect_timeout"] = 120
    kwargs["read_timeout"] = 1200
    kwargs["write_timeout"] = 1200
    
    # Only use SSL for STG (Azure MySQL), never for LOCAL
    if use_stg:
        _ca = os.getenv("SSL_CA", "")
        if _ca and os.path.isfile(_ca):
            kwargs["ssl"] = _ssl.create_default_context(cafile=_ca)
    
    kwargs["max_allowed_packet"] = 128*1024*1024  # 128MB
    conn = pymysql.connect(**kwargs, autocommit=True)
    with conn.cursor() as cur:
        cur.execute('SET SESSION innodb_lock_wait_timeout = 1200')
        cur.execute('SET SESSION net_read_timeout = 1200')
        cur.execute('SET SESSION net_write_timeout = 1200')
        cur.execute('SET SESSION lock_wait_timeout = 1200')
        try:
            cur.execute('SET SESSION max_allowed_packet = 134217728')
        except Exception:
            pass  # max_allowed_packet may be read-only in some environments
    return conn


def count_rows(conn, ticker, year, doc_type_pattern="10-Q%"):
    """Count rows in DB for ticker/year/doc_type."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type LIKE %s",
            (ticker.upper(), year, doc_type_pattern)
        )
        return cur.fetchone()[0]


def delete_rows(conn, ticker, year, doc_type_pattern="10-Q%"):
    """Delete all rows for ticker/year/doc_type in batches to avoid timeout."""
    total_deleted = 0
    batch_size = 25
    ticker = ticker.upper()
    
    with conn.cursor() as cur:
        # Get total count first
        cur.execute(
            "SELECT COUNT(*) FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type LIKE %s",
            (ticker, year, doc_type_pattern)
        )
        total = cur.fetchone()[0]
        
        if total == 0:
            return 0
    
    # Delete in batches using LIMIT - reconnect between batches
    while total_deleted < total:
        conn.ping(reconnect=True)  # Ensure connection is alive
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type LIKE %s LIMIT %s",
                (ticker, year, doc_type_pattern, batch_size)
            )
            deleted = cur.rowcount
            total_deleted += deleted
            print(f"    Deleted batch: {deleted} rows (total: {total_deleted}/{total})")
            if deleted == 0:
                break
    
    return total_deleted


def run_generate_and_load(ticker, year, use_stg=False):
    """Run generate_and_load.py for specific ticker/year."""
    env = os.environ.copy()
    if use_stg:
        env["DB_HOST"] = os.getenv("STG_DB_HOST", "")
        env["DB_PORT"] = os.getenv("STG_DB_PORT", "3306")
        env["DB_NAME"] = os.getenv("STG_DB_NAME", "coresight_market_data_stg")
        env["DB_USER"] = os.getenv("STG_DB_USER", "")
        env["DB_PASSWORD"] = os.getenv("STG_DB_PASSWORD", "")
        env["ENABLE_SSL"] = "true"
    
    cmd = [
        "python", "scripts/generate_and_load.py",
        ticker,
        "--years", str(year),
        "--form", "10-Q"
    ]
    
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True
    )
    
    return result.returncode == 0, result.stdout, result.stderr


def process_ticker_year(ticker, year, use_stg=False, dry_run=False):
    """Process one ticker/year: delete old + load new."""
    ticker = ticker.upper()
    result = {
        "ticker": ticker,
        "year": year,
        "db": "STG" if use_stg else "LOCAL",
        "step1_count_before": 0,
        "step2_deleted": 0,
        "step3_loaded": False,
        "step4_count_after": 0,
        "errors": [],
        "timestamp": datetime.now().isoformat()
    }
    
    print(f"\n{'='*60}")
    print(f"Processing {ticker} - {year} ({result['db']})")
    print(f"{'='*60}")
    
    # Step 1: Count existing rows
    print(f"\n[Step 1/4] Counting existing rows...")
    try:
        conn = get_connection(use_stg)
        result["step1_count_before"] = count_rows(conn, ticker, year)
        print(f"  Found {result['step1_count_before']} existing rows")
        conn.close()
    except Exception as e:
        result["errors"].append(f"Step 1 failed: {e}")
        print(f"  ❌ Error: {e}")
        return result
    
    # Step 2: Delete existing rows
    print(f"\n[Step 2/4] Deleting existing rows...")
    if dry_run:
        print(f"  🚫 DRY RUN - Would delete {result['step1_count_before']} rows")
        result["step2_deleted"] = result["step1_count_before"]
    else:
        try:
            conn = get_connection(use_stg)
            deleted = delete_rows(conn, ticker, year)
            result["step2_deleted"] = deleted
            print(f"  ✅ Deleted {deleted} rows")
            conn.close()
        except Exception as e:
            result["errors"].append(f"Step 2 failed: {e}")
            print(f"  ❌ Error: {e}")
            return result
    
    # Step 3: Run generate_and_load.py
    print(f"\n[Step 3/4] Running generate_and_load.py...")
    if dry_run:
        print(f"  🚫 DRY RUN - Would run: generate_and_load.py {ticker} --years {year} --form 10-Q")
        result["step3_loaded"] = True
    else:
        success, stdout, stderr = run_generate_and_load(ticker, year, use_stg)
        if success:
            result["step3_loaded"] = True
            print(f"  ✅ generate_and_load.py completed")
        else:
            result["errors"].append(f"Step 3 failed: {stderr}")
            print(f"  ❌ Error: {stderr}")
            return result
    
    # Step 4: Verify new rows
    print(f"\n[Step 4/4] Verifying new rows...")
    try:
        conn = get_connection(use_stg)
        result["step4_count_after"] = count_rows(conn, ticker, year)
        print(f"  Found {result['step4_count_after']} new rows")
        conn.close()
        
        if result["step4_count_after"] > 0:
            print(f"  ✅ SUCCESS - Data loaded successfully")
        else:
            result["errors"].append("No rows found after loading")
            print(f"  ⚠️ Warning: No rows found after loading")
    except Exception as e:
        result["errors"].append(f"Step 4 failed: {e}")
        print(f"  ❌ Error: {e}")
    
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Delete & Load 10-Q data for one ticker/year")
    parser.add_argument("ticker", help="Ticker symbol (e.g., ACI)")
    parser.add_argument("year", type=int, help="Year (e.g., 2024)")
    parser.add_argument("--stg", action="store_true", help="Use STG database")
    parser.add_argument("--dry-run", action="store_true", help="Dry run (no actual changes)")
    args = parser.parse_args()
    
    result = process_ticker_year(args.ticker, args.year, args.stg, args.dry_run)
    
    print(f"\n{'='*60}")
    print("RESULT:")
    print(f"{'='*60}")
    print(f"Ticker: {result['ticker']}")
    print(f"Year: {result['year']}")
    print(f"DB: {result['db']}")
    print(f"Rows before: {result['step1_count_before']}")
    print(f"Rows deleted: {result['step2_deleted']}")
    print(f"Load success: {result['step3_loaded']}")
    print(f"Rows after: {result['step4_count_after']}")
    print(f"Errors: {result['errors'] if result['errors'] else 'None'}")
    print(f"{'='*60}")
