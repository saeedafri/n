"""
load_supplementary_json.py
---------------------------
Loads BUSINESS_SEGMENTS.json, GEOGRAPHIC_SEGMENTS.json, and FINANCIAL_RATIOS.json
into the coreiq_filing_metrics MySQL table.

Run order (after load_filings_to_db.py, before calculate_derived_metrics.py):
  1. python scripts/load_filings_to_db.py          -- loads XBRL facts
  2. python scripts/load_supplementary_json.py     -- loads segments + ratios
  3. python scripts/calculate_derived_metrics.py   -- adds EBITDA, margins, etc.

Source tags:
  BUSINESS_SEGMENTS   -> source = 'xbrl'
  GEOGRAPHIC_SEGMENTS -> source = 'xbrl'
  FINANCIAL_RATIOS    -> source = 'financial_ratio'

Usage:
    python scripts/load_supplementary_json.py               # all filings
    python scripts/load_supplementary_json.py AAPL          # one ticker
    python scripts/load_supplementary_json.py AAPL 2024 10-K
"""

import os, sys, json, argparse
import pymysql
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

FILINGS_DIR = PROJECT_ROOT / "data" / "filings"

def get_conn():
    import ssl as _ssl
    kwargs = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "chainxydata_stg"),
        "charset": "utf8mb4",
    }
    if os.getenv("ENABLE_SSL", "").lower() == "true":
        _ca = os.getenv("SSL_CA", "")
        if _ca and os.path.isfile(_ca):
            kwargs["ssl"] = _ssl.create_default_context(cafile=_ca)
    return pymysql.connect(**kwargs)

INSERT_SQL = """
    INSERT INTO coreiq_filing_metrics
        (ticker, company_name, fiscal_year, doc_type,
         concept, context_ref, value, unit_ref, numeric_value,
         period_type, fiscal_period,
         original_label, standard_concept, statement_type,
         is_dimensioned, dimension, member, dimension_label,
         source)
    VALUES (%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s, %s,%s,%s, %s,%s,%s,%s, %s)
"""

def _load_company_names():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, COALESCE(name_coresight, name) FROM coreiq_companies WHERE ticker IS NOT NULL")
            names = {row[0].strip(): row[1] for row in cur.fetchall() if row[0]}
        conn.close()
        return names
    except Exception:
        return {}

COMPANY_NAME_MAP = _load_company_names()

def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except:
        return None

# Ratios already computed by calculate_derived_metrics.py — skip to avoid duplicates
_SKIP_RATIOS = {
    "gross margin %", "operating margin %", "net income margin %", "net margin %",
    "ebitda margin %", "ebitda", "total debt", "net debt",
    "total cash & st investments", "total operating expenses (sg&a + r&d)",
}

def load_segments(conn, ticker, year, doc_type, path, stmt_type):
    records = json.loads(path.read_text(encoding="utf-8"))
    if not records:
        return 0
    rows = []
    for r in records:
        nv = _num(r.get("value"))
        mt = r.get("metric_type", "Revenue")
        ptype = "instant" if mt == "Assets" else "duration"
        company_name = COMPANY_NAME_MAP.get(ticker, ticker)
        rows.append((
            ticker, company_name, year, doc_type,
            r.get("concept",""), r.get("context_ref",""),
            r.get("value"), (r.get("unit") or "usd").lower(), nv,
            ptype, "FY",
            r.get("original_label") or mt,
            r.get("standard_concept",""),
            stmt_type,
            True, r.get("dimension",""), r.get("member",""),
            r.get("dimension_label") or r.get("segment_name",""),
            "xbrl",
        ))
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type=%s AND statement_type=%s",
            (ticker, year, doc_type, stmt_type)
        )
        deleted = cur.rowcount
        cur.executemany(INSERT_SQL, rows)
        inserted = cur.rowcount
    conn.commit()
    print(f"    {stmt_type}: -{deleted} +{inserted}")
    return inserted

def load_financial_ratios(conn, ticker, year, doc_type, path):
    records = json.loads(path.read_text(encoding="utf-8"))
    if not records:
        return 0
    rows = []
    for r in records:
        name = (r.get("ratio_name") or r.get("original_label") or "").strip()
        if name.lower() in _SKIP_RATIOS:
            continue
        nv = _num(r.get("value"))
        if nv is None:
            continue
        unit = (r.get("unit") or "ratio").lower()
        company_name = COMPANY_NAME_MAP.get(ticker, ticker)
        rows.append((
            ticker, company_name, year, doc_type,
            "calculated", "calculated",
            r.get("value"), unit, nv,
            "duration", "FY",
            name,
            r.get("standard_concept", name.replace(" ","").replace("/","").replace("%","")),
            r.get("category","Financial Ratio"),
            False, "", "", "Company Wide",
            "financial_ratio",
        ))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM coreiq_filing_metrics WHERE ticker=%s AND fiscal_year=%s AND doc_type=%s AND source='financial_ratio'",
            (ticker, year, doc_type)
        )
        deleted = cur.rowcount
        cur.executemany(INSERT_SQL, rows)
        inserted = cur.rowcount
    conn.commit()
    print(f"    Financial Ratios: -{deleted} +{inserted}")
    return inserted

def process(conn, ticker, year, doc_type, doc_dir):
    total = 0
    print(f"\n  [{ticker} {year} {doc_type}]")
    # NOTE: Business Segments and Geographic Segments are intentionally skipped.
    # All segment data (with ixbrl_id / html_location) is already loaded from
    # FINAL_FACTS_FILTERED.json in Step 2 (load_filings_to_db.py).
    # Loading them again from separate JSON files would create duplicate rows
    # WITHOUT ixbrl_id, breaking the "View in Document" feature.
    fr = doc_dir / "FINANCIAL_RATIOS.json"
    if fr.exists(): total += load_financial_ratios(conn, ticker, year, doc_type, fr)
    if total == 0:
        print("    (nothing to load)")
    return total

def discover(ticker_f=None, year_f=None, doc_f=None):
    found = []
    for td in sorted(FILINGS_DIR.iterdir()):
        if not td.is_dir(): continue
        t = td.name
        if ticker_f and t != ticker_f.upper(): continue
        for yd in sorted(td.iterdir()):
            if not yd.is_dir() or not yd.name.isdigit(): continue
            y = int(yd.name)
            if year_f and y != year_f: continue
            for dd in sorted(yd.iterdir()):
                if not dd.is_dir(): continue
                dt = dd.name
                if doc_f and dt.upper() != doc_f.upper(): continue
                found.append((t, y, dt, dd))
    return found

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", nargs="?")
    ap.add_argument("fiscal_year", nargs="?", type=int)
    ap.add_argument("doc_type", nargs="?")
    a = ap.parse_args()
    filings = discover(a.ticker, a.fiscal_year, a.doc_type)
    if not filings:
        print("No filings found."); return
    print(f"Processing {len(filings)} filing(s)...")
    conn = get_conn()
    total = 0
    try:
        for t, y, dt, dd in filings:
            total += process(conn, t, y, dt, dd)
    finally:
        conn.close()
    print(f"\nDone — {total} rows inserted.")

if __name__ == "__main__":
    main()
