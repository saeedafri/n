#!/usr/bin/env python3
"""
extract_net_sales_guidance.py
=============================
For every company in coreiq_companies (source=SEC), runs THREE tasks IN PARALLEL:
  1. edgartools  — 8-K Item 2.02 press releases → OpenAI → Net Sales guidance
  2. AV news     — articles with "guidance/outlook" in title → OpenAI → Net Sales guidance
  3. YF news     — articles with "guidance/outlook" in title → OpenAI → Net Sales guidance

ALL rows use the same 8-column format:
  Event Date | Company | Ticker | Event Category | Event Sub Category |
  Headline | Situation | Source

Event Category  = "Corporate Guidance"
Event Sub Category = "Net Sales"

Usage:
    python scripts/extract_net_sales_guidance.py
    python scripts/extract_net_sales_guidance.py --tickers M,TGT,WMT
    python scripts/extract_net_sales_guidance.py --lookback-months 24
    python scripts/extract_net_sales_guidance.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import pymysql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

try:
    from edgar import set_identity, Company
    set_identity(os.environ.get("EDGAR_IDENTITY", "Coresight Research research@coresight.com"))
except ImportError:
    print("ERROR: pip install edgartools"); sys.exit(1)

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: pip install openai"); sys.exit(1)

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
except ImportError:
    print("ERROR: pip install openpyxl"); sys.exit(1)

OUTPUT_DIR = PROJECT_ROOT / "scripts" / "net_sales_results"
OUTPUT_DIR.mkdir(exist_ok=True)

# Keywords that flag a news article as guidance-related
GUIDANCE_KEYWORDS = ["guidance", "outlook", "forecast", "raised guidance",
                     "lowered guidance", "updated guidance", "full-year", "fiscal year"]


# ═══════════════════════════════════════════════════════════
#  OPENAI  (one shared client, thread-safe)
# ═══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a financial data extractor.
Extract ONLY the Net Sales (or Total Net Sales / Total Revenue) GUIDANCE from the text below.
This is FORWARD-LOOKING guidance — do NOT return actual reported results.

Return ONLY valid JSON:
{
  "found": true or false,
  "net_sales_low": number or null,
  "net_sales_high": number or null,
  "unit": "billions" or "millions" or "thousands",
  "period": "FY 2025" or "Q3 2025" etc,
  "raw_text": "exact quote e.g. $21.0 billion to $21.4 billion"
}

Rules:
- net_sales_low / net_sales_high must be PLAIN NUMBERS (21.15 for $21.15 billion)
- If two guidance sets exist (updated vs prior), return the UPDATED/CURRENT one
- Point estimate → set both low and high to that value
- If the text mentions guidance but gives NO specific number → {"found": false}
- NEVER put text descriptions as numbers"""


def ask_openai(client: OpenAI, text: str) -> dict:
    """Call OpenAI. Returns {"data": {...}, "tokens_in": N, "tokens_out": N}."""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": text[:10000]},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        return {
            "data":       data,
            "tokens_in":  resp.usage.prompt_tokens     if resp.usage else 0,
            "tokens_out": resp.usage.completion_tokens if resp.usage else 0,
        }
    except Exception:
        return {"data": {"found": False}, "tokens_in": 0, "tokens_out": 0}


# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════

def get_connection():
    return pymysql.connect(
        host=os.environ["STG_DB_HOST"],
        port=int(os.environ.get("STG_DB_PORT", 3306)),
        user=os.environ["STG_DB_USER"],
        password=os.environ["STG_DB_PASSWORD"],
        database=os.environ["STG_DB_NAME"],
        ssl={"ca": str(PROJECT_ROOT / "DigiCertGlobalRootG2.crt.pem")},
        connect_timeout=20,
        cursorclass=pymysql.cursors.DictCursor,
    )


def load_companies(tickers_filter: Optional[list] = None) -> list:
    try:
        conn = get_connection()
        with conn.cursor() as c:
            c.execute("SELECT id, ticker, name FROM coreiq_companies WHERE source='SEC' ORDER BY ticker")
            rows = c.fetchall()
        conn.close()
        companies = [{"id": r["id"], "ticker": r["ticker"], "name": r["name"]} for r in rows]
        if tickers_filter:
            up = [t.strip().upper() for t in tickers_filter]
            companies = [c for c in companies if c["ticker"] in up]
        return companies
    except Exception as e:
        print(f"  DB load failed: {e}"); return []


def load_guidance_news_av(ticker: str, cutoff_date) -> list:
    """AV news filtered to guidance/outlook articles only."""
    try:
        kw_conditions = " OR ".join(
            f"(LOWER(title) LIKE '%{kw}%' OR LOWER(summary) LIKE '%{kw}%')"
            for kw in ["guidance", "outlook", "forecast"]
        )
        conn = get_connection()
        with conn.cursor() as c:
            c.execute(f"""
                SELECT time_published_utc AS event_date,
                       title, url AS link,
                       COALESCE(summary, title) AS body,
                       source_name AS publisher
                FROM coreiq_av_market_news_sentiment
                WHERE ticker = %s
                  AND time_published_utc >= %s
                  AND ({kw_conditions})
                ORDER BY time_published_utc DESC
                LIMIT 200
            """, (ticker, cutoff_date))
            rows = c.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def load_guidance_news_yf(ticker: str, cutoff_date) -> list:
    """YF news filtered to guidance/outlook articles only."""
    try:
        conn = get_connection()
        with conn.cursor() as c:
            c.execute("""
                SELECT published_at AS event_date,
                       title, link,
                       title AS body,
                       publisher
                FROM coreiq_yf_market_news_sentiment
                WHERE ticker = %s
                  AND published_at >= %s
                  AND (LOWER(title) LIKE '%guidance%'
                    OR LOWER(title) LIKE '%outlook%'
                    OR LOWER(title) LIKE '%forecast%')
                ORDER BY published_at DESC
                LIMIT 200
            """, (ticker, cutoff_date))
            rows = c.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════
#  EDGAR: guidance table text → OpenAI
# ═══════════════════════════════════════════════════════════

def get_text_for_openai_edgar(eight_k) -> Optional[str]:
    """
    Priority 1 — guidance/outlook table (most focused).
    Priority 2 — full press release text.
    """
    try:
        earnings = getattr(eight_k, "earnings", None)
        if earnings:
            for t in (getattr(earnings, "financial_tables", []) or []):
                title = (t.title or "").lower()
                if any(w in title for w in ["guidance", "outlook", "forecast"]):
                    df = t.dataframe
                    lines = [f"Guidance Table: {t.title}"]
                    lines.append("  " + " | ".join(str(c) for c in df.columns))
                    for lbl in df.index:
                        vals = " | ".join(str(df.loc[lbl, c]) for c in df.columns)
                        lines.append(f"  {lbl}: {vals}")
                    txt = "\n".join(lines)
                    if len(txt) > 50:
                        return txt
    except Exception:
        pass
    try:
        prs = getattr(eight_k, "press_releases", None)
        if prs:
            pr = list(prs)[0]
            for method in ["text", "to_markdown"]:
                fn = getattr(pr, method, None)
                if fn and callable(fn):
                    try:
                        result = fn()
                        if result and len(str(result)) > 200:
                            return str(result)
                    except Exception:
                        pass
    except Exception:
        pass
    return None


def make_edgar_url(cik: str, accession_no: str) -> str:
    if not cik or not accession_no:
        return ""
    cik_clean = str(cik).lstrip("0")
    acc_clean  = accession_no.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/{accession_no}-index.htm"


# ═══════════════════════════════════════════════════════════
#  ROW BUILDERS  (same 8-column format for all sources)
# ═══════════════════════════════════════════════════════════

def fmt_value(n, unit):
    if n is None: return ""
    if unit == "billions":  return f"${n:.2f}B"
    if unit == "millions":  return f"${n:,.0f}M"
    return str(n)


def build_row(company: dict, event_date: str, ai_data: dict, source_url: str) -> Optional[dict]:
    """Build a standardised output row from OpenAI result. Returns None if not found."""
    if not ai_data.get("found"):
        return None

    low    = ai_data.get("net_sales_low")
    high   = ai_data.get("net_sales_high")
    unit   = ai_data.get("unit", "")
    period = ai_data.get("period", "")
    raw    = ai_data.get("raw_text", "")

    if low is not None and high is not None and abs(low - high) > 0.0001:
        display = f"{fmt_value(low, unit)} \u2013 {fmt_value(high, unit)}"
    elif low is not None:
        display = fmt_value(low, unit)
    else:
        display = raw[:60]

    headline  = f"{period}: {display}" if period else display
    situation = (f"{period} Net Sales Guidance: {display}. {raw}").strip(" .")

    return {
        "event_date":         event_date,
        "company":            company["name"],
        "ticker":             company["ticker"],
        "event_category":     "Corporate Guidance",
        "event_sub_category": "Net Sales",
        "headline":           headline,
        "situation":          situation[:500],
        "source":             source_url,
    }


# ═══════════════════════════════════════════════════════════
#  THREE PARALLEL TASKS PER COMPANY
# ═══════════════════════════════════════════════════════════

def task_edgar(company: dict, cutoff_date, openai_client, dry_run: bool) -> Tuple[list, int, int]:
    """Fetch all 8-K Item 2.02 filings, extract Net Sales via OpenAI."""
    rows = []
    ti = to = 0
    try:
        co      = Company(company["ticker"])
        cik     = str(getattr(co, "cik", "") or "")
        filings = co.get_filings(form="8-K")
    except Exception:
        return [], 0, 0

    for f in filings:
        fd = f.filing_date
        try:
            fd = datetime.strptime(str(fd), "%Y-%m-%d").date()
        except Exception:
            if hasattr(fd, "date"): fd = fd.date()
        if fd < cutoff_date:
            break

        try:
            eight_k = f.obj()
        except Exception:
            continue

        items = [str(i) for i in (getattr(eight_k, "items", []) or [])]
        if not any("2.02" in it for it in items):
            continue

        text = get_text_for_openai_edgar(eight_k)
        if not text or len(text) < 80:
            continue

        accession_no = getattr(f, "accession_no", "") or getattr(f, "accession_number", "") or ""
        source_url   = make_edgar_url(cik, accession_no)

        if dry_run:
            ai_data = {"found": False}
        else:
            res     = ask_openai(openai_client, text)
            ai_data = res["data"]
            ti     += res["tokens_in"]
            to     += res["tokens_out"]

        row = build_row(company, str(f.filing_date), ai_data, source_url)
        if row:
            rows.append(row)

    return rows, ti, to


def task_av_news(company: dict, cutoff_date, openai_client, dry_run: bool) -> Tuple[list, int, int]:
    """Fetch guidance-filtered AV news, extract Net Sales via OpenAI."""
    rows = []
    ti = to = 0
    news_items = load_guidance_news_av(company["ticker"], cutoff_date)

    for n in news_items:
        body = str(n.get("body") or n.get("title") or "")
        if len(body) < 30:
            continue

        ed = n["event_date"]
        date_str = ed.strftime("%Y-%m-%d") if hasattr(ed, "strftime") else str(ed)[:10]

        if dry_run:
            ai_data = {"found": False}
        else:
            res     = ask_openai(openai_client, body)
            ai_data = res["data"]
            ti     += res["tokens_in"]
            to     += res["tokens_out"]

        source_url = str(n.get("link") or "")
        row = build_row(company, date_str, ai_data, source_url)
        if row:
            # Tag source as news in situation
            publisher = n.get("publisher", "Alpha Vantage")
            row["situation"] = f"[AV News – {publisher}] " + row["situation"]
            rows.append(row)

    return rows, ti, to


def task_yf_news(company: dict, cutoff_date, openai_client, dry_run: bool) -> Tuple[list, int, int]:
    """Fetch guidance-filtered YF news, extract Net Sales via OpenAI."""
    rows = []
    ti = to = 0
    news_items = load_guidance_news_yf(company["ticker"], cutoff_date)

    for n in news_items:
        body = str(n.get("body") or n.get("title") or "")
        if len(body) < 30:
            continue

        ed = n["event_date"]
        date_str = ed.strftime("%Y-%m-%d") if hasattr(ed, "strftime") else str(ed)[:10]

        if dry_run:
            ai_data = {"found": False}
        else:
            res     = ask_openai(openai_client, body)
            ai_data = res["data"]
            ti     += res["tokens_in"]
            to     += res["tokens_out"]

        source_url = str(n.get("link") or "")
        row = build_row(company, date_str, ai_data, source_url)
        if row:
            publisher = n.get("publisher", "Yahoo Finance")
            row["situation"] = f"[YF News – {publisher}] " + row["situation"]
            rows.append(row)

    return rows, ti, to


def process_company_parallel(company: dict, cutoff_date, openai_client,
                              dry_run: bool) -> Tuple[list, int, int]:
    """Run edgar + AV + YF tasks in parallel threads for one company."""
    all_rows = []
    total_ti = total_to = 0

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(task_edgar,   company, cutoff_date, openai_client, dry_run): "edgar",
            ex.submit(task_av_news, company, cutoff_date, openai_client, dry_run): "av",
            ex.submit(task_yf_news, company, cutoff_date, openai_client, dry_run): "yf",
        }
        for fut in as_completed(futures):
            try:
                rows, ti, to = fut.result()
                all_rows.extend(rows)
                total_ti += ti
                total_to += to
            except Exception as e:
                pass  # individual task failure doesn't kill the company

    return all_rows, total_ti, total_to


# ═══════════════════════════════════════════════════════════
#  EXCEL EXPORT
# ═══════════════════════════════════════════════════════════

COLUMNS = [
    ("Event Date",         "event_date",         13),
    ("Company",            "company",             28),
    ("Ticker",             "ticker",              10),
    ("Event Category",     "event_category",      20),
    ("Event Sub Category", "event_sub_category",  20),
    ("Headline",           "headline",            42),
    ("Situation",          "situation",           72),
    ("Source",             "source",              55),
]

HDR_FILL   = PatternFill("solid", fgColor="1F3864")
HDR_FONT   = Font(bold=True, color="FFFFFF", size=10)
EDGAR_FILL = PatternFill("solid", fgColor="D9EAD3")    # green  — edgar 8-K
NEWS_FILL  = PatternFill("solid", fgColor="CFE2F3")    # blue   — news
ALT_EDGAR  = PatternFill("solid", fgColor="B6D7A8")
ALT_NEWS   = PatternFill("solid", fgColor="9FC5E8")
THIN       = Side(style="thin", color="CCCCCC")
BORDER     = Border(bottom=THIN)


def export_excel(rows: list, out_path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Net Sales Guidance"

    for ci, (header, _, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=ci, value=header)
        cell.font      = HDR_FONT
        cell.fill      = HDR_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[cell.column_letter].width = width
    ws.row_dimensions[1].height = 22

    # Sort by ticker then date
    sorted_rows = sorted(rows, key=lambda r: (r["ticker"], r["event_date"]))

    for ri, row in enumerate(sorted_rows, 2):
        is_edgar = "AV News" not in row.get("situation", "") and "YF News" not in row.get("situation", "")
        alt      = (ri % 2 == 0)
        fill     = (ALT_EDGAR if alt else EDGAR_FILL) if is_edgar else (ALT_NEWS if alt else NEWS_FILL)

        for ci, (_, field, _) in enumerate(COLUMNS, 1):
            val  = row.get(field, "")
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.fill      = fill
            cell.border    = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=(ci >= 6), horizontal="left")
        ws.row_dimensions[ri].height = 40

    ws.freeze_panes = "A2"
    wb.save(out_path)


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers",         default="")
    parser.add_argument("--lookback-months", type=int, default=24)
    parser.add_argument("--dry-run",         action="store_true")
    args = parser.parse_args()

    tickers_filter = [t.strip() for t in args.tickers.split(",") if t.strip()] or None
    cutoff_date    = (datetime.now() - timedelta(days=args.lookback_months * 30)).date()

    print("=" * 70, flush=True)
    print("  NET SALES GUIDANCE — edgar + AV news + YF news  (parallel)", flush=True)
    print(f"  Lookback : {args.lookback_months} months  (since {cutoff_date})", flush=True)
    print(f"  OpenAI   : {'DISABLED (--dry-run)' if args.dry_run else 'gpt-4o-mini'}", flush=True)
    print("=" * 70, flush=True)

    companies = load_companies(tickers_filter)
    if not companies:
        print("No companies found."); return
    print(f"\nCompanies: {len(companies)}", flush=True)

    openai_client = None
    if not args.dry_run:
        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            openai_client = OpenAI(api_key=key)
            print("  OpenAI: ready", flush=True)
        else:
            print("  WARNING: OPENAI_API_KEY not set", flush=True)

    all_rows = []
    total_ti = total_to = 0

    for idx, company in enumerate(companies):
        ticker = company["ticker"]
        t0     = time.time()

        rows, ti, to = process_company_parallel(
            company, cutoff_date, openai_client, args.dry_run
        )

        elapsed = time.time() - t0
        edgar_n = sum(1 for r in rows if "AV News" not in r.get("situation","") and "YF News" not in r.get("situation",""))
        news_n  = len(rows) - edgar_n
        all_rows.extend(rows)
        total_ti += ti
        total_to += to

        print(f"  [{idx+1:3d}/{len(companies)}] {ticker:<6}  "
              f"edgar={edgar_n}  news={news_n}  total={len(rows)}  ({elapsed:.1f}s)", flush=True)

    # Summary
    print(f"\n  TOTAL ROWS : {len(all_rows)}", flush=True)
    if total_ti:
        cost = (total_ti / 1_000_000 * 0.150) + (total_to / 1_000_000 * 0.600)
        print(f"  OpenAI     : {total_ti:,} in + {total_to:,} out  ≈ ${cost:.4f}", flush=True)

    if not all_rows:
        print("Nothing to export."); return

    ts        = datetime.now().strftime("%Y%m%d_%H%M")
    xlsx_path = OUTPUT_DIR / f"guidance_news_{ts}.xlsx"
    json_path = OUTPUT_DIR / f"guidance_news_{ts}.json"

    export_excel(all_rows, xlsx_path)
    json_path.write_text(json.dumps(all_rows, indent=2, default=str))

    print(f"\n  Excel → {xlsx_path}", flush=True)
    print(f"  JSON  → {json_path}", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    main()
