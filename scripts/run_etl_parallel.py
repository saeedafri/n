#!/usr/bin/env python3
"""
Parallel ETL Orchestrator
=========================
Runs ingest_key_developments.py across multiple ticker batches in parallel,
then runs edgartools extraction once sequentially.

Usage:
  python scripts/run_etl_parallel.py --workers 5 --dry-run
  python scripts/run_etl_parallel.py --workers 3
  python scripts/run_etl_parallel.py --skip-edgartools
"""

import argparse
import math
import os
import re
import ssl
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pymysql

# ─────────────────────────────────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ETL_SCRIPT = PROJECT_ROOT / "scripts" / "ingest_key_developments.py"
PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python3")
LOGS_DIR = PROJECT_ROOT / "logs"


# ─────────────────────────────────────────────────────────────────────────
#  DB HELPERS
# ─────────────────────────────────────────────────────────────────────────

def _get_stg_connection():
    """Connect to staging DB using .env credentials."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    ssl_ctx = ssl.create_default_context(
        cafile=str(PROJECT_ROOT / "DigiCertGlobalRootG2.crt.pem"))
    return pymysql.connect(
        host=os.environ.get("STG_DB_HOST", ""),
        port=int(os.environ.get("STG_DB_PORT", "3306")),
        user=os.environ.get("STG_DB_USER", ""),
        password=os.environ.get("STG_DB_PASSWORD", ""),
        database=os.environ.get("STG_DB_NAME", ""),
        ssl=ssl_ctx,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
    )


def load_tickers() -> List[str]:
    """Load all tickers from STG DB."""
    conn = _get_stg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ticker FROM coreiq_companies WHERE ticker IS NOT NULL ORDER BY ticker"
            )
            return [r["ticker"] for r in cur.fetchall()]
    finally:
        conn.close()


def clear_events_table():
    """DELETE all rows from coreiq_company_events once before parallel inserts."""
    conn = _get_stg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM coreiq_company_events")
            before = cur.fetchone()["cnt"]
            cur.execute("DELETE FROM coreiq_company_events")
            conn.commit()
            print(f"  Cleared {before} existing events from coreiq_company_events")
            return before
    finally:
        conn.close()


def print_verification_query():
    """Run and print the final verification query."""
    conn = _get_stg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT event_category, source, COUNT(*) AS cnt
                FROM coreiq_company_events
                GROUP BY event_category, source
                ORDER BY event_category
            """)
            rows = cur.fetchall()

        print("\n" + "=" * 70)
        print("  VERIFICATION: coreiq_company_events by category × source")
        print("=" * 70)
        print(f"  {'event_category':<40} {'source':<15} {'cnt':>8}")
        print("-" * 70)
        total = 0
        for r in rows:
            print(f"  {r['event_category']:<40} {r['source']:<15} {r['cnt']:>8}")
            total += r["cnt"]
        print("-" * 70)
        print(f"  {'TOTAL':<40} {'':<15} {total:>8}")
        print("=" * 70)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
#  BATCH SPLITTING
# ─────────────────────────────────────────────────────────────────────────

def split_into_batches(tickers: List[str], n: int) -> List[List[str]]:
    """Split tickers evenly into n batches."""
    batch_size = math.ceil(len(tickers) / n)
    return [tickers[i : i + batch_size] for i in range(0, len(tickers), batch_size)]


# ─────────────────────────────────────────────────────────────────────────
#  SUBPROCESS RUNNER (called by ProcessPoolExecutor)
# ─────────────────────────────────────────────────────────────────────────

def _run_batch(batch_info: Tuple[int, List[str], bool]) -> Dict:
    """Run one ETL subprocess for a batch of tickers.

    Returns a dict with batch_id, tickers, returncode, stdout, stderr, elapsed.
    """
    batch_num, tickers, dry_run = batch_info
    ticker_csv = ",".join(tickers)

    cmd = [
        PYTHON, str(ETL_SCRIPT),
        "--env", "staging",
        "--mode", "incremental",
        "--sources", "news,earnings,estimates",
        "--ticker", ticker_csv,
        "--yes",
    ]
    if dry_run:
        cmd.append("--dry-run")

    t0 = time.time()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=1800,  # 30 min max per batch
    )
    elapsed = time.time() - t0

    # Try to parse inserted/updated/errors from stdout
    stats = {"inserted": 0, "updated": 0, "errors": 0}
    m = re.search(
        r"inserted=(\d+)\s+updated=(\d+)\s+errors=(\d+)",
        proc.stdout + proc.stderr,
    )
    if m:
        stats["inserted"] = int(m.group(1))
        stats["updated"] = int(m.group(2))
        stats["errors"] = int(m.group(3))

    return {
        "batch_num": batch_num,
        "ticker_count": len(tickers),
        "tickers": ticker_csv,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "elapsed": elapsed,
        "stats": stats,
    }


# ─────────────────────────────────────────────────────────────────────────
#  EDGARTOOLS SEQUENTIAL RUN
# ─────────────────────────────────────────────────────────────────────────

def run_edgartools(dry_run: bool, log_file) -> Dict:
    """Run edgartools extraction once, sequentially."""
    print("\n── Running edgartools extraction (sequential) ──")
    log_file.write("\n\n" + "=" * 70 + "\n")
    log_file.write("[EDGARTOOLS] Sequential run\n")
    log_file.write("=" * 70 + "\n")

    cmd = [
        PYTHON, str(ETL_SCRIPT),
        "--env", "staging",
        "--mode", "incremental",
        "--sources", "edgartools",
        "--yes",
    ]
    if dry_run:
        cmd.append("--dry-run")

    t0 = time.time()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=3600,  # 60 min max
    )
    elapsed = time.time() - t0

    log_file.write(proc.stdout)
    if proc.stderr:
        log_file.write("\n--- STDERR ---\n")
        log_file.write(proc.stderr)

    stats = {"inserted": 0, "updated": 0, "errors": 0}
    m = re.search(
        r"inserted=(\d+)\s+updated=(\d+)\s+errors=(\d+)",
        proc.stdout + proc.stderr,
    )
    if m:
        stats["inserted"] = int(m.group(1))
        stats["updated"] = int(m.group(2))
        stats["errors"] = int(m.group(3))

    result = {
        "returncode": proc.returncode,
        "elapsed": elapsed,
        "stats": stats,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }

    if proc.returncode != 0:
        print(f"  ❌ edgartools FAILED (rc={proc.returncode}, {elapsed:.1f}s)")
        print(proc.stderr[-2000:] if proc.stderr else proc.stdout[-2000:])
    else:
        print(
            f"  ✅ edgartools DONE — inserted={stats['inserted']} "
            f"updated={stats['updated']} errors={stats['errors']} ({elapsed:.1f}s)"
        )

    return result


# ─────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Parallel ETL Orchestrator for Key Developments"
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="Number of parallel batch workers (default: 5)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to ETL")
    parser.add_argument(
        "--skip-edgartools", action="store_true",
        help="Skip the sequential edgartools step",
    )
    args = parser.parse_args()

    LOGS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"etl_parallel_{ts}.log"
    log_file = open(log_path, "w")

    try:
        header = (
            f"Parallel ETL Orchestrator — {ts}\n"
            f"Workers: {args.workers} | dry_run: {args.dry_run} | "
            f"skip_edgartools: {args.skip_edgartools}\n"
        )
        print(header)
        log_file.write(header + "\n")

        # Step 1 — Load tickers
        print("── Loading tickers from STG DB ──")
        tickers = load_tickers()
        print(f"  Loaded {len(tickers)} tickers")
        log_file.write(f"Tickers ({len(tickers)}): {','.join(tickers)}\n\n")

        # Step 1b — Clear table once (full rebuild)
        if not args.dry_run:
            print("── Clearing coreiq_company_events (full rebuild) ──")
            clear_events_table()

        # Step 2 — Split into batches
        batches = split_into_batches(tickers, args.workers)
        for i, batch in enumerate(batches):
            print(f"  [BATCH-{i+1}] {len(batch)} tickers: {batch[0]}..{batch[-1]}")

        # Step 3 — Run parallel subprocesses
        print(f"\n── Running {len(batches)} parallel batches "
              f"(sources: news,earnings,estimates) ──")
        t0_all = time.time()

        batch_args = [(i + 1, batch, args.dry_run) for i, batch in enumerate(batches)]
        results: List[Optional[Dict]] = [None] * len(batches)
        failed = False

        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            future_to_idx = {
                executor.submit(_run_batch, ba): ba[0] - 1 for ba in batch_args
            }

            for i, batch in enumerate(batches):
                print(
                    f"  [BATCH-{i+1}] {len(batch)} tickers | status: running..."
                )

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                    results[idx] = result

                    # Log output
                    bn = result["batch_num"]
                    log_file.write(f"\n{'='*70}\n")
                    log_file.write(f"[BATCH-{bn}] {result['ticker_count']} tickers\n")
                    log_file.write(f"{'='*70}\n")
                    log_file.write(result["stdout"])
                    if result["stderr"]:
                        log_file.write("\n--- STDERR ---\n")
                        log_file.write(result["stderr"])

                    s = result["stats"]
                    if result["returncode"] != 0:
                        print(
                            f"  [BATCH-{bn}] ❌ FAILED (rc={result['returncode']}, "
                            f"{result['elapsed']:.1f}s)"
                        )
                        print(result["stderr"][-2000:] if result["stderr"]
                              else result["stdout"][-2000:])
                        failed = True
                        # Cancel remaining futures
                        for f in future_to_idx:
                            f.cancel()
                        break
                    else:
                        print(
                            f"  [BATCH-{bn}] ✅ DONE — inserted={s['inserted']} "
                            f"updated={s['updated']} errors={s['errors']} "
                            f"({result['elapsed']:.1f}s)"
                        )
                except subprocess.TimeoutExpired:
                    print(f"  [BATCH-{idx+1}] ❌ TIMEOUT (>30 min)")
                    failed = True
                    for f in future_to_idx:
                        f.cancel()
                    break
                except Exception as exc:
                    print(f"  [BATCH-{idx+1}] ❌ EXCEPTION: {exc}")
                    failed = True
                    for f in future_to_idx:
                        f.cancel()
                    break

        parallel_elapsed = time.time() - t0_all

        if failed:
            print(f"\n❌ Parallel stage FAILED after {parallel_elapsed:.1f}s")
            log_file.write(f"\n\nPARALLEL STAGE FAILED after {parallel_elapsed:.1f}s\n")
            sys.exit(1)

        print(f"\n✅ All {len(batches)} batches completed in {parallel_elapsed:.1f}s")

        # Summary table
        print("\n" + "=" * 70)
        print("  BATCH SUMMARY")
        print("=" * 70)
        print(
            f"  {'Batch':<10} {'Tickers':>8} {'Inserted':>10} {'Updated':>10} "
            f"{'Errors':>8} {'Time':>8}"
        )
        print("-" * 70)
        totals = {"inserted": 0, "updated": 0, "errors": 0}
        for r in results:
            if r is None:
                continue
            s = r["stats"]
            print(
                f"  BATCH-{r['batch_num']:<4} {r['ticker_count']:>8} "
                f"{s['inserted']:>10} {s['updated']:>10} {s['errors']:>8} "
                f"{r['elapsed']:>7.1f}s"
            )
            for k in totals:
                totals[k] += s[k]
        print("-" * 70)
        print(
            f"  {'TOTAL':<10} {len(tickers):>8} {totals['inserted']:>10} "
            f"{totals['updated']:>10} {totals['errors']:>8} "
            f"{parallel_elapsed:>7.1f}s"
        )
        print("=" * 70)

        log_file.write("\n\nBATCH SUMMARY:\n")
        log_file.write(f"Total tickers: {len(tickers)}\n")
        log_file.write(f"Inserted: {totals['inserted']}\n")
        log_file.write(f"Updated: {totals['updated']}\n")
        log_file.write(f"Errors: {totals['errors']}\n")
        log_file.write(f"Elapsed: {parallel_elapsed:.1f}s\n")

        # Step 6 — edgartools sequential
        if not args.skip_edgartools:
            edgar_result = run_edgartools(args.dry_run, log_file)
            if edgar_result["returncode"] != 0:
                print("\n❌ edgartools step FAILED — exiting")
                log_file.write("\n\nEDGARTOOLS STEP FAILED\n")
                sys.exit(1)
        else:
            print("\n── Skipping edgartools (--skip-edgartools) ──")

        # Verification query
        print_verification_query()

        total_elapsed = time.time() - t0_all
        print(f"\n✅ Full ETL orchestration done in {total_elapsed:.1f}s")
        print(f"   Log file: {log_path}")
        log_file.write(f"\n\nTotal elapsed: {total_elapsed:.1f}s\n")

    finally:
        log_file.close()


if __name__ == "__main__":
    main()
