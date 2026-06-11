#!/usr/bin/env python3
"""
YFinance Filings Sync - FINAL CLEAN VERSION
===========================================
ONLY these 5 patterns:
- annual-report
- interim-report-Q1
- interim-report-Q2
- interim-report-Q3
- interim-report-Q4

ONLY files named: filing.pdf
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from azure.storage.blob import BlobServiceClient
from sqlalchemy import text
from core.database import db_manager
from datetime import datetime

AZURE_ACCOUNT = os.getenv('AZURE_STORAGE_ACCOUNT_NAME', 'csmarketdata')
AZURE_KEY = os.getenv('AZURE_STORAGE_ACCOUNT_KEY', '')
CONTAINER = os.getenv('AZURE_BLOB_CONTAINER', 'azure-storage-test')

# SIRF 5 PATTERNS - ISKE ALAWA KUCH NAHI
VALID_PATTERNS = {'annual-report', 'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3', 'interim-report-Q4'}


def get_company_names():
    """Get company names from coreiq_companies (non-SEC only)."""
    companies = {}
    try:
        with db_manager.get_session() as session:
            result = session.execute(text("""
                SELECT ticker, name 
                FROM coreiq_companies
                WHERE source != 'SEC'
            """))
            for row in result:
                companies[row[0]] = row[1]
        print(f"✓ Loaded {len(companies)} companies")
    except Exception as e:
        print(f"⚠ Error: {e}")
    return companies


def scan_azure():
    """Scan Azure for ONLY 5 patterns with filing.pdf."""
    print("\n🔍 Scanning Azure...")
    print(f"   Patterns: {VALID_PATTERNS}")
    print(f"   File: filing.pdf only")
    
    blob_service = BlobServiceClient(
        account_url=f'https://{AZURE_ACCOUNT}.blob.core.windows.net',
        credential=AZURE_KEY
    )
    container = blob_service.get_container_client(CONTAINER)
    
    records = []
    scanned = 0
    
    for blob in container.list_blobs():
        scanned += 1
        if scanned % 10000 == 0:
            print(f"   Scanned {scanned}...")
        
        parts = blob.name.split('/')
        
        # EXACT 4 parts: ticker/year/folder/filing.pdf
        if len(parts) != 4:
            continue
        
        ticker, year, folder, filename = parts
        
        # Year must be 4 digits
        if not (year.isdigit() and len(year) == 4):
            continue
        
        # File must be filing.pdf
        if filename.lower() != 'filing.pdf':
            continue
        
        # Folder must be one of 5 patterns
        if folder not in VALID_PATTERNS:
            continue
        
        records.append({
            'ticker': ticker,
            'fiscal_year': int(year),
            'doc_type': folder,
        })
    
    print(f"\n✓ Found: {len(records)} records")
    return records, scanned


def insert_records(records, company_names):
    """Insert to DB."""
    if not records:
        print("\n⚠ No records!")
        return 0, 0
    
    print(f"\n📝 Inserting {len(records)} records...")
    
    query = """
        INSERT IGNORE INTO coreiq_filing_metrics 
        (ticker, fiscal_year, doc_type, company_name)
        VALUES (:ticker, :fiscal_year, :doc_type, :company_name)
    """
    
    inserted = 0
    missing = []
    
    with db_manager.get_session() as session:
        for i, record in enumerate(records, 1):
            ticker = record['ticker']
            company_name = company_names.get(ticker)
            
            if not company_name:
                company_name = ticker
                if ticker not in missing:
                    missing.append(ticker)
            
            try:
                params = {
                    'ticker': ticker,
                    'fiscal_year': record['fiscal_year'],
                    'doc_type': record['doc_type'],
                    'company_name': company_name,
                }
                result = session.execute(text(query), params)
                if result.rowcount > 0:
                    inserted += 1
                
                if i % 50 == 0:
                    print(f"   {i}/{len(records)}")
                    
            except Exception as e:
                print(f"   ✗ {record}: {e}")
    
    return inserted, missing


def show_summary():
    """Show DB summary."""
    print("\n" + "="*60)
    print("📊 SUMMARY")
    print("="*60)
    
    try:
        with db_manager.get_session() as session:
            # By doc_type
            result = session.execute(text("""
                SELECT doc_type, COUNT(*) 
                FROM coreiq_filing_metrics 
                WHERE doc_type IN ('annual-report', 'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3', 'interim-report-Q4')
                GROUP BY doc_type
                ORDER BY doc_type
            """))
            
            total = 0
            for row in result:
                print(f"   {row[0]}: {row[1]}")
                total += row[1]
            print(f"   TOTAL: {total}")
            
            # By company
            result = session.execute(text("""
                SELECT ticker, COUNT(*) as cnt
                FROM coreiq_filing_metrics 
                WHERE doc_type IN ('annual-report', 'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3', 'interim-report-Q4')
                GROUP BY ticker
                ORDER BY cnt DESC
            """))
            rows = result.fetchall()
            
            print(f"\n   Companies: {len(rows)}")
            for row in rows:
                print(f"   {row[0]}: {row[1]}")
                
    except Exception as e:
        print(f"⚠ Error: {e}")


def main():
    print("="*60)
    print("🚀 YFINANCE SYNC - FINAL CLEAN")
    print(f"   {datetime.now().isoformat()}")
    print("="*60)
    
    # Get company names
    company_names = get_company_names()
    
    # Scan Azure
    records, scanned = scan_azure()
    
    if not records:
        print("\n⚠ No records found!")
        return
    
    # Show by pattern
    by_pattern = {}
    for r in records:
        p = r['doc_type']
        by_pattern[p] = by_pattern.get(p, 0) + 1
    
    print("\n📊 By pattern:")
    for p in sorted(by_pattern.keys()):
        print(f"   {p}: {by_pattern[p]}")
    
    # Insert
    inserted, missing = insert_records(records, company_names)
    
    print(f"\n✓ Inserted: {inserted}/{len(records)}")
    if missing:
        print(f"⚠ Missing names: {', '.join(missing)}")
    
    # Summary
    show_summary()
    
    print("\n" + "="*60)
    print(f"✅ Done: {datetime.now().isoformat()}")
    print("="*60)


if __name__ == "__main__":
    main()
