"""
calculate_derived_metrics.py
-----------------------------
Computes derived/calculated metrics (EBITDA, Net Debt, margins etc.) from
values already in coreiq_filing_metrics, then appends them back to the
FINAL_FACTS_FILTERED.json files AND inserts directly into the DB.

Usage:
    python scripts/calculate_derived_metrics.py               # all filings
    python scripts/calculate_derived_metrics.py AAPL 2024 10K # specific filing
    python scripts/calculate_derived_metrics.py AAPL          # all years for AAPL

Run from project root:
    python scripts/calculate_derived_metrics.py
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Optional

# ── path setup ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from sqlalchemy import create_engine, text

# ── DB connection ────────────────────────────────────────────────────────────
def get_engine():
    url = (
        f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}/{os.getenv('DB_NAME')}"
    )
    connect_args = {}
    if os.getenv("ENABLE_SSL", "").lower() == "true":
        import ssl as _ssl
        _ca = os.getenv("SSL_CA", "")
        if _ca and os.path.isfile(_ca):
            connect_args["ssl"] = _ssl.create_default_context(cafile=_ca)
    return create_engine(url, connect_args=connect_args)


# ── Metric resolver: find a value from the coreiq_filing_metrics DB ─────────────────
def get_metric(conn, ticker: str, fiscal_year: int, doc_type: str,
               *label_candidates: str) -> Optional[float]:
    """
    Try each candidate label in order; return the first numeric_value found.
    - Only considers non-dimensioned, non-calculated rows (avoids circular deps).
    - Orders by CHAR_LENGTH(original_label) ASC so shorter/exact labels win
      over labels that merely contain the search term (e.g. 'Net income'
      beats 'Adjustment for net gains included in net income').
    """
    for label in label_candidates:
        result = conn.execute(text("""
            SELECT numeric_value FROM coreiq_filing_metrics
            WHERE ticker = :ticker AND fiscal_year = :fy AND doc_type = :dt
              AND is_dimensioned = 0
              AND source NOT IN ('calculated', 'financial_ratio')
              AND LOWER(original_label) LIKE :q
              AND numeric_value IS NOT NULL
            ORDER BY CHAR_LENGTH(original_label) ASC
            LIMIT 1
        """), {"ticker": ticker, "fy": fiscal_year, "dt": doc_type,
               "q": f"%{label.lower()}%"})
        row = result.fetchone()
        if row:
            return float(row[0])
    return None


# ── Build a JSON object for a derived metric ──────────────────────────────────
def make_derived_entry(label: str, concept: str, value: float,
                       unit_ref: str, note: str,
                       fiscal_year: int, ticker: str) -> dict:
    return {
        "concept":          concept,
        "context_ref":      None,
        "value":            str(int(value)) if unit_ref == "usd" else str(value),
        "unit_ref":         unit_ref,
        "decimals":         "-6",
        "numeric_value":    value,
        "period_type":      "duration",
        "period_start":     None,
        "period_end":       None,
        "entity_identifier": ticker,
        "entity_scheme":    None,
        "period_key":       None,
        "fiscal_period":    "FY",
        "fiscal_year":      fiscal_year,
        "is_dimensioned":   False,
        "label":            label,
        "original_label":   label,
        "balance":          None,
        "preferred_sign":   1,
        "statement_type":   "Calculated",
        "statement_role":   None,
        "weight":           None,
        "period_instant":   None,
        "dimension":        None,
        "member":           None,
        "dimension_label":  None,
        "dimension_member_label": None,
        "full_dimension_label":   None,
        "standard_concept": concept,
        "html_location":    None,
        "source":           "calculated",
        "llm_query":        None,
        "calculation_note": note,
    }


# ── Main calculation logic for one filing ────────────────────────────────────
def calculate_for_filing(conn, ticker: str, fiscal_year: int, doc_type: str):
    """
    Derive metrics from existing DB values and return list of new entries.
    Each entry is ready to INSERT into coreiq_filing_metrics and append to JSON.
    """
    def g(*labels):
        return get_metric(conn, ticker, fiscal_year, doc_type, *labels)

    derived = []

    # ── Income Statement metrics ─────────────────────────────────────────────
    operating_income = g("operating income")
    dna              = g("depreciation and amortization", "depreciation & amortization")
    net_sales        = g("net sales", "total revenue", "revenue")
    gross_margin_val = g("gross margin", "gross profit")
    net_income       = g("net income", "net income (loss)", "net earnings")
    sga              = g("selling, general and administrative", "selling general")
    rd               = g("research and development")

    # Fallback: compute gross profit from net sales - cost of sales (e.g. M/Macy's)
    if gross_margin_val is None and net_sales is not None:
        cost_of_sales = g("cost of sales", "cost of revenue", "cost of goods sold",
                          "cost of products sold")
        if cost_of_sales is not None:
            gross_margin_val = net_sales - cost_of_sales

    # EBITDA
    if operating_income is not None and dna is not None:
        ebitda = operating_income + dna
        derived.append(make_derived_entry(
            label="EBITDA",
            concept="CapIQ_EBITDA",
            value=ebitda,
            unit_ref="usd",
            note=f"Operating income ({operating_income:,.0f}) + D&A ({dna:,.0f})",
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    # Total Operating Expenses excl. COGS (SG&A + R&D) — useful as a check
    if sga is not None and rd is not None:
        opex = sga + rd
        derived.append(make_derived_entry(
            label="Total Operating Expenses (SG&A + R&D)",
            concept="CapIQ_TotalOpEx",
            value=opex,
            unit_ref="usd",
            note=f"SG&A ({sga:,.0f}) + R&D ({rd:,.0f})",
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    # Gross Margin %
    if gross_margin_val is not None and net_sales is not None and net_sales != 0:
        gm_pct = (gross_margin_val / net_sales) * 100
        derived.append(make_derived_entry(
            label="Gross Margin %",
            concept="CapIQ_GrossMarginPct",
            value=round(gm_pct, 4),
            unit_ref="percent",
            note=f"Gross margin ({gross_margin_val:,.0f}) / Net sales ({net_sales:,.0f}) × 100",
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    # Operating Margin %
    if operating_income is not None and net_sales is not None and net_sales != 0:
        op_pct = (operating_income / net_sales) * 100
        derived.append(make_derived_entry(
            label="Operating Margin %",
            concept="CapIQ_OperatingMarginPct",
            value=round(op_pct, 4),
            unit_ref="percent",
            note=f"Operating income ({operating_income:,.0f}) / Net sales ({net_sales:,.0f}) × 100",
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    # Net Margin %
    if net_income is not None and net_sales is not None and net_sales != 0:
        ni_pct = (net_income / net_sales) * 100
        derived.append(make_derived_entry(
            label="Net Income Margin %",
            concept="CapIQ_NetMarginPct",
            value=round(ni_pct, 4),
            unit_ref="percent",
            note=f"Net income ({net_income:,.0f}) / Net sales ({net_sales:,.0f}) × 100",
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    # EBITDA Margin %
    if operating_income is not None and dna is not None and net_sales is not None and net_sales != 0:
        ebitda_val = operating_income + dna
        ebitda_pct = (ebitda_val / net_sales) * 100
        derived.append(make_derived_entry(
            label="EBITDA Margin %",
            concept="CapIQ_EBITDAMarginPct",
            value=round(ebitda_pct, 4),
            unit_ref="percent",
            note=f"EBITDA ({ebitda_val:,.0f}) / Net sales ({net_sales:,.0f}) × 100",
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    # ── Balance Sheet metrics ─────────────────────────────────────────────────
    term_debt        = g("total term debt", "term debt")
    comm_paper       = g("commercial paper")
    cash             = g("cash and cash equivalents")
    mktbl_sec        = g("marketable securities", "total marketable")
    fin_lease_curr   = g("finance lease liabilities, current")
    fin_lease_noncurr= g("finance lease liabilities, non-current")

    # Total Debt
    total_debt_components = [v for v in [term_debt, comm_paper, fin_lease_curr, fin_lease_noncurr] if v is not None]
    if total_debt_components:
        total_debt = sum(total_debt_components)
        note_parts = []
        if term_debt        is not None: note_parts.append(f"Term debt ({term_debt:,.0f})")
        if comm_paper       is not None: note_parts.append(f"Commercial paper ({comm_paper:,.0f})")
        if fin_lease_curr   is not None: note_parts.append(f"Finance lease curr ({fin_lease_curr:,.0f})")
        if fin_lease_noncurr is not None: note_parts.append(f"Finance lease non-curr ({fin_lease_noncurr:,.0f})")
        derived.append(make_derived_entry(
            label="Total Debt",
            concept="CapIQ_TotalDebt",
            value=total_debt,
            unit_ref="usd",
            note=" + ".join(note_parts),
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

        # Net Debt (Total Debt - Cash - Marketable Securities)
        liquid = (cash or 0) + (mktbl_sec or 0)
        net_debt = total_debt - liquid
        note_parts2 = [f"Total Debt ({total_debt:,.0f})"]
        if cash     is not None: note_parts2.append(f"- Cash ({cash:,.0f})")
        if mktbl_sec is not None: note_parts2.append(f"- Marketable securities ({mktbl_sec:,.0f})")
        derived.append(make_derived_entry(
            label="Net Debt",
            concept="CapIQ_NetDebt",
            value=net_debt,
            unit_ref="usd",
            note=" ".join(note_parts2),
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    # Total Cash & ST Investments
    if cash is not None and mktbl_sec is not None:
        total_cash = cash + mktbl_sec
        derived.append(make_derived_entry(
            label="Total Cash & ST Investments",
            concept="CapIQ_TotalCashAndSTInv",
            value=total_cash,
            unit_ref="usd",
            note=f"Cash ({cash:,.0f}) + Marketable securities ({mktbl_sec:,.0f})",
            fiscal_year=fiscal_year,
            ticker=ticker,
        ))

    return derived


# ── Insert derived entries into DB ────────────────────────────────────────────
def insert_to_db(conn, ticker: str, fiscal_year: int, doc_type: str, entries: list):
    # Delete existing calculated entries for this filing first (idempotent)
    conn.execute(text("""
        DELETE FROM coreiq_filing_metrics
        WHERE ticker = :ticker AND fiscal_year = :fy AND doc_type = :dt
          AND source = 'calculated'
    """), {"ticker": ticker, "fy": fiscal_year, "dt": doc_type})

    for e in entries:
        conn.execute(text("""
            INSERT INTO coreiq_filing_metrics
                (ticker, company_name, fiscal_year, doc_type, concept, original_label, standard_concept,
                 numeric_value, value, unit_ref, is_dimensioned, dimension_label,
                 statement_type, source, llm_query, period_type, fiscal_period)
            VALUES
                (:ticker, (SELECT MIN(COALESCE(name_coresight, name)) FROM coreiq_companies WHERE ticker = :ticker),
                 :fy, :dt, :concept, :label, :concept,
                 :value, :value_str, :unit, 0, NULL,
                 'Calculated', 'calculated', :note, 'duration', 'FY')
        """), {
            "ticker":    ticker,
            "fy":        fiscal_year,
            "dt":        doc_type,
            "concept":   e["concept"],
            "label":     e["original_label"],
            "value":     e["numeric_value"],
            "value_str": e["value"],
            "unit":      e["unit_ref"],
            "note":      e["calculation_note"],
        })


# ── Append entries to FINAL_FACTS_FILTERED.json ───────────────────────────────
def update_json(ticker: str, fiscal_year: int, doc_type: str, entries: list):
    json_path = (
        PROJECT_ROOT / "data" / "filings" / ticker
        / str(fiscal_year) / doc_type / "FINAL_FACTS_FILTERED.json"
    )
    if not json_path.exists():
        print(f"  JSON not found: {json_path} — skipping JSON update")
        return

    with open(json_path) as f:
        data = json.load(f)

    # Remove previously calculated entries
    data = [item for item in data if item.get("source") != "calculated"]

    # Append new ones
    data.extend(entries)

    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"  JSON updated: {json_path} (+{len(entries)} entries)")


# ── Discover all filings in data/filings/ ─────────────────────────────────────
def discover_filings():
    filings_root = PROJECT_ROOT / "data" / "filings"
    found = []
    if not filings_root.exists():
        return found
    for ticker_dir in sorted(filings_root.iterdir()):
        if not ticker_dir.is_dir():
            continue
        ticker = ticker_dir.name
        for year_dir in sorted(ticker_dir.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            fiscal_year = int(year_dir.name)
            for doc_dir in sorted(year_dir.iterdir()):
                if not doc_dir.is_dir():
                    continue
                doc_type = doc_dir.name  # e.g. 10K, 10-K
                json_file = doc_dir / "FINAL_FACTS_FILTERED.json"
                if json_file.exists():
                    found.append((ticker, fiscal_year, doc_type))
    return found


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Calculate derived financial metrics")
    parser.add_argument("ticker",      nargs="?", help="Ticker symbol (e.g. AAPL)")
    parser.add_argument("fiscal_year", nargs="?", type=int, help="Fiscal year (e.g. 2024)")
    parser.add_argument("doc_type",    nargs="?", help="Doc type (e.g. 10K)")
    args = parser.parse_args()

    engine = get_engine()

    # Determine which filings to process
    if args.ticker and args.fiscal_year and args.doc_type:
        filings = [(args.ticker.upper(), args.fiscal_year, args.doc_type.upper())]
    elif args.ticker and args.fiscal_year:
        filings = [(args.ticker.upper(), args.fiscal_year, dt)
                   for _, _, dt in discover_filings()
                   if _ == args.ticker.upper() and _ == args.fiscal_year]
    elif args.ticker:
        filings = [(t, y, d) for t, y, d in discover_filings()
                   if t == args.ticker.upper()]
    else:
        filings = discover_filings()

    if not filings:
        print("No filings found to process.")
        return

    print(f"Processing {len(filings)} filing(s)...")

    with engine.begin() as conn:
        for ticker, fiscal_year, doc_type in filings:
            print(f"\n[{ticker} {fiscal_year} {doc_type}]")
            entries = calculate_for_filing(conn, ticker, fiscal_year, doc_type)

            if not entries:
                print("  No source values found — skipping")
                continue

            print(f"  Calculated {len(entries)} metrics:")
            for e in entries:
                val = e["numeric_value"]
                unit = e["unit_ref"]
                if unit == "usd":
                    if abs(val) >= 1e9:
                        display = f"${val/1e9:,.3f}B"
                    elif abs(val) >= 1e6:
                        display = f"${val/1e6:,.0f}M"
                    else:
                        display = f"${val:,.0f}"
                elif unit == "percent":
                    display = f"{val:.2f}%"
                else:
                    display = str(val)
                print(f"    {e['original_label']:<40} = {display}")

            insert_to_db(conn, ticker, fiscal_year, doc_type, entries)
            print(f"  DB updated ({len(entries)} rows inserted)")

            update_json(ticker, fiscal_year, doc_type, entries)

    print("\nDone.")


if __name__ == "__main__":
    main()
