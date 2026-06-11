#!/usr/bin/env python3
"""
YFinance Filings Sync - STG DB VERSION
======================================
Syncs data to STG (Staging) Database
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from azure.storage.blob import BlobServiceClient
from sqlalchemy import create_engine, text
from datetime import datetime
import re

# STG DB CONFIG
STG_DB_HOST = os.getenv('STG_DB_HOST', 'csr-mysql8-flex-stg.mysql.database.azure.com')
STG_DB_PORT = os.getenv('STG_DB_PORT', '3306')
STG_DB_NAME = os.getenv('STG_DB_NAME', 'coresight_market_data_stg')
STG_DB_USER = os.getenv('STG_DB_USER', 'mohdsaeedafri')
STG_DB_PASSWORD = os.getenv('STG_DB_PASSWORD', '')

# AZURE CONFIG
AZURE_ACCOUNT = os.getenv('AZURE_STORAGE_ACCOUNT_NAME', 'csmarketdata')
AZURE_KEY = os.getenv('AZURE_STORAGE_ACCOUNT_KEY', '')
CONTAINER = os.getenv('AZURE_BLOB_CONTAINER', 'azure-storage-test')

# VALID PATTERNS - PDF filings (filing.pdf)
PDF_PATTERNS = {'annual-report', 'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3', 'interim-report-Q4'}
# VALID PATTERNS - HTML filings (filing.html)
HTML_PATTERNS = {'8-K', '6-K'}
# All valid patterns combined
VALID_PATTERNS = PDF_PATTERNS | HTML_PATTERNS
INTERIM_PATTERN = re.compile(r'^interim-report-Q\d+$', re.IGNORECASE)


def get_stg_engine():
    """Create SQLAlchemy engine for STG DB with SSL."""
    # SSL certificate path
    ssl_ca = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'DigiCertGlobalRootG2.crt.pem')
    
    connection_string = f"mysql+pymysql://{STG_DB_USER}:{STG_DB_PASSWORD}@{STG_DB_HOST}:{STG_DB_PORT}/{STG_DB_NAME}"
    
    connect_args = {
        'ssl': {
            'ca': ssl_ca
        }
    }
    
    return create_engine(connection_string, pool_pre_ping=True, connect_args=connect_args)


def get_company_names(engine):
    """Fetch company names from STG coreiq_companies (NON-SEC only)."""
    companies = {}
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT ticker, name 
                FROM coreiq_companies
                WHERE source != 'SEC'
            """))
            for row in result:
                companies[row[0]] = row[1]
        print(f"✓ Loaded {len(companies)} companies from STG DB")
    except Exception as e:
        print(f"⚠ Error loading companies: {e}")
    return companies


def scan_azure():
    """Scan Azure for filings - handles both PDF and HTML formats."""
    print("\n🔍 Scanning Azure Blob Storage...")
    print(f"   PDF patterns (filing.pdf): {PDF_PATTERNS}")
    print(f"   HTML patterns (filing.html): {HTML_PATTERNS}")
    
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
        if len(parts) != 4:
            continue
        
        ticker, year, folder, filename = parts
        
        if not (year.isdigit() and len(year) == 4):
            continue
        
        # Determine expected filename based on folder type
        if folder in HTML_PATTERNS:
            expected_filename = 'filing.html'
        elif folder in PDF_PATTERNS:
            expected_filename = 'filing.pdf'
        else:
            continue  # Skip if folder not in any valid pattern
        
        # File must match expected extension
        if filename.lower() != expected_filename:
            continue
        
        records.append({
            'ticker': ticker,
            'fiscal_year': int(year),
            'doc_type': folder,
        })
    
    print(f"✓ Found: {len(records)} records")
    return records, scanned


def insert_to_stg(engine, records, company_names):
    """Insert records to STG DB."""
    if not records:
        print("\n⚠ No records!")
        return 0, 0
    
    print(f"\n📝 Inserting {len(records)} records to STG DB...")
    
    # Use INSERT IGNORE to prevent duplicates (same ticker/year/doc_type)
    query = """
        INSERT IGNORE INTO coreiq_filing_metrics 
        (ticker, fiscal_year, doc_type, company_name)
        VALUES (:ticker, :fiscal_year, :doc_type, :company_name)
    """
    
    inserted = 0
    missing = []
    
    with engine.connect() as conn:
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
                result = conn.execute(text(query), params)
                if result.rowcount > 0:
                    inserted += 1
                
                if i % 50 == 0:
                    print(f"   {i}/{len(records)}")
                    
            except Exception as e:
                print(f"   ✗ {record}: {e}")
        
        conn.commit()
    
    return inserted, missing


def show_stg_summary(engine):
    """Show STG DB summary."""
    print("\n" + "="*60)
    print("📊 STG DB SUMMARY")
    print("="*60)
    
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT doc_type, COUNT(*) 
                FROM coreiq_filing_metrics 
                WHERE doc_type IN ('annual-report', 'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3', 'interim-report-Q4', '8-K', '6-K')
                GROUP BY doc_type
                ORDER BY doc_type
            """))
            
            total = 0
            for row in result:
                print(f"   {row[0]}: {row[1]}")
                total += row[1]
            print(f"   TOTAL: {total}")
            
            result = conn.execute(text("""
                SELECT COUNT(DISTINCT ticker)
                FROM coreiq_filing_metrics 
                WHERE doc_type IN ('annual-report', 'interim-report-Q1', 'interim-report-Q2', 'interim-report-Q3', 'interim-report-Q4', '8-K', '6-K')
            """))
            companies = result.scalar()
            print(f"\n   Companies: {companies}")
                
    except Exception as e:
        print(f"⚠ Error: {e}")


def main():
    print("="*60)
    print("🚀 YFINANCE SYNC - STG DATABASE")
    print(f"   DB: {STG_DB_HOST}")
    print(f"   DB Name: {STG_DB_NAME}")
    print(f"   Started: {datetime.now().isoformat()}")
    print("="*60)
    
    # Connect to STG DB
    print("\n🔌 Connecting to STG DB...")
    try:
        engine = get_stg_engine()
        # Test connection
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            print("✓ STG DB connection successful!")
    except Exception as e:
        print(f"✗ STG DB connection failed: {e}")
        return
    
    # Get company names from STG
    company_names = get_company_names(engine)
    
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
    
    # Insert to STG
    inserted, missing = insert_to_stg(engine, records, company_names)
    
    print(f"\n✓ Inserted: {inserted}/{len(records)}")
    if missing:
        print(f"⚠ Missing names: {', '.join(missing)}")
    
    # Summary
    show_stg_summary(engine)
    
    print("\n" + "="*60)
    print(f"✅ Done: {datetime.now().isoformat()}")
    print("="*60)


if __name__ == "__main__":
    main()
