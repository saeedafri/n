#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
  AZURE FILING UPLOADER - Single Source of Truth for SEC Filings Upload
═══════════════════════════════════════════════════════════════════════════════

This is the ONLY script that should be used to upload SEC filings to Azure
Blob Storage. It ensures consistent path formats and proper quarter detection.

QUARTER DETECTION (Official EdgarTools Method):
    xbrl = filing.xbrl()
    fiscal_period = xbrl.entity_info.get('fiscal_period')
    if fiscal_period in ('Q1', 'Q2', 'Q3'):
        quarter = fiscal_period  # 'Q1', 'Q2', or 'Q3'

PATH FORMAT (STANDARD):
    10-K:   {TICKER}/{YEAR}/10-K/filing.html
    Q1:     {TICKER}/{YEAR}/10-Q-Q1/filing.html
    Q2:     {TICKER}/{YEAR}/10-Q-Q2/filing.html
    Q3:     {TICKER}/{YEAR}/10-Q-Q3/filing.html
    20-F:   {TICKER}/{YEAR}/20-F/filing.html

Prerequisites:
    pip install azure-storage-blob edgar python-dotenv

Environment Variables (.env):
    AZURE_STORAGE_ACCOUNT_NAME=csmarketdata
    AZURE_STORAGE_ACCOUNT_KEY=your_key_here
    AZURE_BLOB_CONTAINER=azure-storage-test
    EDGAR_IDENTITY=Your Name your@email.com

Usage:
    # Upload specific company/year
    python azure_filing_uploader.py --ticker AAPL --year 2024

    # Upload all filings for a company
    python azure_filing_uploader.py --ticker AAPL --all-years

    # Upload multiple companies
    python azure_filing_uploader.py --tickers AAPL,MSFT,GOOGL --year 2024

    # Force re-upload (overwrite existing)
    python azure_filing_uploader.py --ticker AAPL --year 2024 --force

    # Upload with custom workers and dry-run
    python azure_filing_uploader.py --tickers AAPL,MSFT --year 2024 --workers 8 --dry-run
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import re
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

# =============================================================================
# SETUP PATHS
# =============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# =============================================================================
# LOAD ENVIRONMENT
# =============================================================================
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# =============================================================================
# CONFIGURATION
# =============================================================================
AZURE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "csmarketdata")
AZURE_KEY = os.getenv("AZURE_STORAGE_ACCOUNT_KEY")
AZURE_CONTAINER = os.getenv("AZURE_BLOB_CONTAINER", "azure-storage-test")
EDGAR_IDENTITY = os.getenv("EDGAR_IDENTITY", "user@example.com")

# Cache directories
LOCAL_CACHE_DIR = PROJECT_ROOT / "data" / "filings_cache"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# =============================================================================
# ENUMS
# =============================================================================
class FilingType(str, Enum):
    """Supported SEC filing types."""
    TEN_K = "10-K"
    TEN_Q = "10-Q"
    TWENTY_F = "20-F"


class UploadStatus(str, Enum):
    """Upload operation status."""
    UPLOADED = "uploaded"
    EXISTS = "exists"
    SKIPPED = "skipped"
    ERROR = "error"


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class FilingMetadata:
    """Metadata for a filing upload."""
    ticker: str
    year: int
    filing_type: str
    quarter: Optional[str]
    fiscal_period: Optional[str]
    filing_date: Optional[str]
    period_end_date: Optional[str]
    upload_timestamp: str
    blob_path: str
    html_size_bytes: int
    source: str
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class UploadResult:
    """Result of an upload operation."""
    ticker: str
    year: int
    filing_type: str
    quarter: Optional[str]
    blob_path: str
    status: UploadStatus
    html_size: int
    json_size: int
    message: str
    timestamp: str


# =============================================================================
# LOGGING SETUP
# =============================================================================
def setup_logging(verbose: bool = False) -> logging.Logger:
    """Setup logging with file and console handlers."""
    logger = logging.getLogger("azure_filing_uploader")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    
    # Clear existing handlers
    logger.handlers = []
    
    # Format
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    
    # File handler
    log_file = LOG_DIR / f"azure_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    logger.info(f"Logging to: {log_file}")
    return logger


# =============================================================================
# AZURE CLIENT
# =============================================================================
class AzureBlobClient:
    """Azure Blob Storage client for filing uploads."""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._container = None
        self._verify_credentials()
    
    def _verify_credentials(self):
        """Verify Azure credentials are available."""
        if not AZURE_KEY:
            raise ValueError(
                "AZURE_STORAGE_ACCOUNT_KEY not found in environment. "
                "Please set it in your .env file."
            )
    
    def _get_container(self):
        """Lazy initialization of container client."""
        if self._container is None:
            from azure.storage.blob import BlobServiceClient
            account_url = f"https://{AZURE_ACCOUNT}.blob.core.windows.net"
            blob_service = BlobServiceClient(
                account_url=account_url, 
                credential=AZURE_KEY
            )
            self._container = blob_service.get_container_client(AZURE_CONTAINER)
            self.logger.debug(f"Connected to Azure: {AZURE_ACCOUNT}/{AZURE_CONTAINER}")
        return self._container
    
    def blob_exists(self, blob_name: str) -> bool:
        """Check if a blob exists in Azure."""
        try:
            blob_client = self._get_container().get_blob_client(blob_name)
            blob_client.get_blob_properties()
            return True
        except Exception:
            return False
    
    def get_blob_size(self, blob_name: str) -> int:
        """Get the size of a blob in bytes."""
        try:
            blob_client = self._get_container().get_blob_client(blob_name)
            props = blob_client.get_blob_properties()
            return props.size
        except Exception:
            return 0
    
    def upload_blob(self, blob_name: str, content: bytes, overwrite: bool = False) -> bool:
        """Upload content to Azure Blob Storage."""
        try:
            blob_client = self._get_container().get_blob_client(blob_name)
            blob_client.upload_blob(content, overwrite=overwrite)
            return True
        except Exception as e:
            self.logger.error(f"Failed to upload {blob_name}: {e}")
            return False
    
    def list_blobs(self, prefix: str = "") -> List[str]:
        """List all blobs with given prefix."""
        try:
            blobs = self._get_container().list_blobs(name_starts_with=prefix)
            return [b.name for b in blobs]
        except Exception as e:
            self.logger.error(f"Failed to list blobs: {e}")
            return []


# =============================================================================
# EDGAR/SEC CLIENT
# =============================================================================
class EdgarClient:
    """Client for fetching filings from SEC EDGAR."""
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._setup_edgar()
    
    def _setup_edgar(self):
        """Setup edgartools with identity and cache."""
        try:
            from edgar import set_identity, use_local_storage
            set_identity(EDGAR_IDENTITY)
            
            # Setup cache directory
            cache_dir = PROJECT_ROOT / ".edgar_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            use_local_storage(str(cache_dir))
            
            self.logger.debug(f"EdgarTools initialized with identity: {EDGAR_IDENTITY}")
        except Exception as e:
            self.logger.warning(f"Could not setup EdgarTools cache: {e}")
    
    def get_filing(self, ticker: str, year: int, form_type: str):
        """Get a filing from SEC EDGAR."""
        try:
            from edgar import Company
            company = Company(ticker)
            filings = company.get_filings(form=form_type)
            
            for filing in filings:
                if filing.filing_date.year == year:
                    return filing
            
            return None
        except Exception as e:
            self.logger.error(f"Error fetching {ticker}/{year}/{form_type}: {e}")
            return None
    
    def get_filings_for_year(self, ticker: str, year: int, form_type: str) -> List[Any]:
        """Get all filings of a specific type for a year."""
        try:
            from edgar import Company
            company = Company(ticker)
            filings = company.get_filings(form=form_type)
            
            result = []
            for filing in filings:
                if filing.filing_date.year == year:
                    result.append(filing)
            
            return result
        except Exception as e:
            self.logger.error(f"Error fetching {ticker}/{year}/{form_type}: {e}")
            return []
    
    def determine_quarter(self, filing) -> Optional[str]:
        """
        Determine the fiscal quarter using the OFFICIAL EdgarTools method.
        
        OFFICIAL EDGARTOOLS METHOD:
            xbrl = filing.xbrl()
            fiscal_period = xbrl.entity_info.get('fiscal_period')
            if fiscal_period in ('Q1', 'Q2', 'Q3'):
                quarter = fiscal_period
        
        Returns:
            'Q1', 'Q2', or 'Q3' for 10-Q filings
            None for other filing types
        """
        try:
            xbrl = filing.xbrl()
            fiscal_period = xbrl.entity_info.get('fiscal_period') or \
                          xbrl.entity_info.get('document_fiscal_period_focus')
            
            if fiscal_period:
                quarter = str(fiscal_period).strip().upper()
                if quarter in ('Q1', 'Q2', 'Q3'):
                    self.logger.debug(f"  Quarter from entity_info['fiscal_period']: {quarter}")
                    return quarter
                else:
                    self.logger.warning(f"  Unexpected fiscal_period value: {quarter}")
        except Exception as e:
            self.logger.debug(f"  Could not read fiscal_period from XBRL: {e}")
        
        return None
    
    def get_entity_info(self, filing) -> Dict[str, Any]:
        """Get entity information from XBRL."""
        try:
            xbrl = filing.xbrl()
            return dict(xbrl.entity_info) if xbrl.entity_info else {}
        except Exception as e:
            self.logger.debug(f"Could not get entity_info: {e}")
            return {}


# =============================================================================
# PATH BUILDER
# =============================================================================
class PathBuilder:
    """Builds Azure blob paths according to the STANDARD format."""
    
    @staticmethod
    def get_blob_path(ticker: str, year: int, filing_type: str, 
                      quarter: Optional[str] = None) -> str:
        """
        Build the Azure blob path for a filing.
        
        STANDARD PATH FORMAT:
            10-K:   {TICKER}/{YEAR}/10-K/filing.html
            Q1:     {TICKER}/{YEAR}/10-Q-Q1/filing.html
            Q2:     {TICKER}/{YEAR}/10-Q-Q2/filing.html
            Q3:     {TICKER}/{YEAR}/10-Q-Q3/filing.html
            20-F:   {TICKER}/{YEAR}/20-F/filing.html
        """
        ticker = ticker.upper()
        
        if filing_type == "10-Q" and quarter:
            # Standard format: 10-Q-Q1, 10-Q-Q2, 10-Q-Q3
            folder_name = f"10-Q-{quarter}"
        else:
            folder_name = filing_type
        
        return f"{ticker}/{year}/{folder_name}/filing.html"
    
    @staticmethod
    def get_metadata_path(ticker: str, year: int, filing_type: str,
                          quarter: Optional[str] = None) -> str:
        """Build the metadata JSON blob path."""
        ticker = ticker.upper()
        
        if filing_type == "10-Q" and quarter:
            folder_name = f"10-Q-{quarter}"
        else:
            folder_name = filing_type
        
        return f"{ticker}/{year}/{folder_name}/metadata.json"


# =============================================================================
# MAIN UPLOADER CLASS
# =============================================================================
class AzureFilingUploader:
    """
    Single source of truth for uploading SEC filings to Azure Blob Storage.
    """
    
    def __init__(self, logger: logging.Logger, dry_run: bool = False):
        self.logger = logger
        self.dry_run = dry_run
        self.azure = AzureBlobClient(logger)
        self.edgar = EdgarClient(logger)
        self.path_builder = PathBuilder()
        self.stats = {
            "uploaded": 0,
            "exists": 0,
            "skipped": 0,
            "errors": 0,
            "total_bytes": 0
        }
    
    def upload_filing(self, ticker: str, year: int, form_type: str,
                      force: bool = False) -> Optional[UploadResult]:
        """
        Upload a single filing to Azure.
        
        Args:
            ticker: Company ticker symbol
            year: Filing year
            form_type: '10-K', '10-Q', or '20-F'
            force: Overwrite if exists
            
        Returns:
            UploadResult with status information
        """
        ticker = ticker.upper()
        timestamp = datetime.now().isoformat()
        
        self.logger.info(f"Processing {ticker}/{year}/{form_type}...")
        
        try:
            # Fetch filing from SEC
            filing = self.edgar.get_filing(ticker, year, form_type)
            if not filing:
                msg = f"No {form_type} filing found for {ticker} in {year}"
                self.logger.warning(f"  ⚠️ {msg}")
                return UploadResult(
                    ticker=ticker, year=year, filing_type=form_type,
                    quarter=None, blob_path="", status=UploadStatus.SKIPPED,
                    html_size=0, json_size=0, message=msg, timestamp=timestamp
                )
            
            # Determine quarter for 10-Q filings
            quarter = None
            if form_type == "10-Q":
                quarter = self.edgar.determine_quarter(filing)
                if not quarter:
                    msg = f"Could not determine quarter for {ticker}/{year} 10-Q"
                    self.logger.error(f"  ❌ {msg}")
                    return UploadResult(
                        ticker=ticker, year=year, filing_type=form_type,
                        quarter=None, blob_path="", status=UploadStatus.ERROR,
                        html_size=0, json_size=0, message=msg, timestamp=timestamp
                    )
                self.logger.info(f"  📅 Fiscal Quarter: {quarter}")
            
            # Build blob paths
            html_blob_path = self.path_builder.get_blob_path(ticker, year, form_type, quarter)
            json_blob_path = self.path_builder.get_metadata_path(ticker, year, form_type, quarter)
            
            self.logger.debug(f"  HTML path: {html_blob_path}")
            self.logger.debug(f"  JSON path: {json_blob_path}")
            
            # Check if already exists
            if not force and self.azure.blob_exists(html_blob_path):
                existing_size = self.azure.get_blob_size(html_blob_path)
                msg = f"Already exists ({existing_size} bytes)"
                self.logger.info(f"  ⏭️ {msg}")
                self.stats["exists"] += 1
                return UploadResult(
                    ticker=ticker, year=year, filing_type=form_type,
                    quarter=quarter, blob_path=html_blob_path,
                    status=UploadStatus.EXISTS, html_size=existing_size,
                    json_size=0, message=msg, timestamp=timestamp
                )
            
            # Download HTML content
            self.logger.debug("  Downloading HTML from SEC...")
            html_content = filing.html()
            
            if not html_content or len(html_content) < 1000:
                msg = f"HTML content too small ({len(html_content) if html_content else 0} bytes)"
                self.logger.error(f"  ❌ {msg}")
                return UploadResult(
                    ticker=ticker, year=year, filing_type=form_type,
                    quarter=quarter, blob_path=html_blob_path,
                    status=UploadStatus.ERROR, html_size=0, json_size=0,
                    message=msg, timestamp=timestamp
                )
            
            html_bytes = html_content.encode('utf-8')
            self.logger.debug(f"  HTML size: {len(html_bytes)} bytes")
            
            # Build metadata
            entity_info = self.edgar.get_entity_info(filing)
            metadata = FilingMetadata(
                ticker=ticker,
                year=year,
                filing_type=form_type,
                quarter=quarter,
                fiscal_period=quarter,
                filing_date=str(filing.filing_date) if hasattr(filing, 'filing_date') else None,
                period_end_date=entity_info.get('period_end'),
                upload_timestamp=timestamp,
                blob_path=html_blob_path,
                html_size_bytes=len(html_bytes),
                source="SEC EDGAR via EdgarTools"
            )
            
            json_content = metadata.to_json()
            json_bytes = json_content.encode('utf-8')
            
            # Upload to Azure
            if self.dry_run:
                msg = f"DRY RUN - Would upload {len(html_bytes)} bytes HTML + {len(json_bytes)} bytes JSON"
                self.logger.info(f"  🔍 {msg}")
                self.stats["uploaded"] += 1
                return UploadResult(
                    ticker=ticker, year=year, filing_type=form_type,
                    quarter=quarter, blob_path=html_blob_path,
                    status=UploadStatus.UPLOADED, html_size=len(html_bytes),
                    json_size=len(json_bytes), message=msg, timestamp=timestamp
                )
            
            # Upload HTML
            html_success = self.azure.upload_blob(html_blob_path, html_bytes, overwrite=True)
            if not html_success:
                msg = "Failed to upload HTML"
                self.logger.error(f"  ❌ {msg}")
                self.stats["errors"] += 1
                return UploadResult(
                    ticker=ticker, year=year, filing_type=form_type,
                    quarter=quarter, blob_path=html_blob_path,
                    status=UploadStatus.ERROR, html_size=len(html_bytes),
                    json_size=0, message=msg, timestamp=timestamp
                )
            
            # Upload JSON metadata
            json_success = self.azure.upload_blob(json_blob_path, json_bytes, overwrite=True)
            if not json_success:
                msg = "HTML uploaded but metadata failed"
                self.logger.warning(f"  ⚠️ {msg}")
            
            msg = f"Uploaded {len(html_bytes)} bytes HTML + {len(json_bytes)} bytes JSON"
            self.logger.info(f"  ✅ {msg}")
            self.stats["uploaded"] += 1
            self.stats["total_bytes"] += len(html_bytes) + len(json_bytes)
            
            return UploadResult(
                ticker=ticker, year=year, filing_type=form_type,
                quarter=quarter, blob_path=html_blob_path,
                status=UploadStatus.UPLOADED, html_size=len(html_bytes),
                json_size=len(json_bytes), message=msg, timestamp=timestamp
            )
            
        except Exception as e:
            msg = f"Error: {str(e)}"
            self.logger.error(f"  ❌ {msg}")
            self.stats["errors"] += 1
            return UploadResult(
                ticker=ticker, year=year, filing_type=form_type,
                quarter=None, blob_path="", status=UploadStatus.ERROR,
                html_size=0, json_size=0, message=msg, timestamp=timestamp
            )
    
    def upload_10q_filings(self, ticker: str, year: int, 
                           force: bool = False) -> List[UploadResult]:
        """
        Upload all 10-Q filings (Q1, Q2, Q3) for a company/year.
        
        Returns:
            List of UploadResult for each quarter found
        """
        ticker = ticker.upper()
        results = []
        
        self.logger.info(f"Looking for 10-Q filings for {ticker}/{year}...")
        
        try:
            filings = self.edgar.get_filings_for_year(ticker, year, "10-Q")
            self.logger.info(f"  Found {len(filings)} 10-Q filing(s)")
            
            for filing in filings:
                quarter = self.edgar.determine_quarter(filing)
                if quarter:
                    result = self.upload_filing(ticker, year, "10-Q", force)
                    if result:
                        results.append(result)
                else:
                    self.logger.warning(f"  ⚠️ Could not determine quarter for a 10-Q filing")
            
        except Exception as e:
            self.logger.error(f"Error fetching 10-Q filings: {e}")
        
        return results
    
    def upload_company_year(self, ticker: str, year: int,
                            form_types: List[str] = None,
                            force: bool = False) -> List[UploadResult]:
        """
        Upload all filings for a company/year.
        
        Args:
            ticker: Company ticker
            year: Filing year
            form_types: List of form types to upload (default: ['10-K', '10-Q'])
            force: Overwrite existing
            
        Returns:
            List of UploadResult
        """
        if form_types is None:
            form_types = ["10-K", "10-Q"]
        
        results = []
        
        for form_type in form_types:
            if form_type == "10-Q":
                # Handle 10-Q specially - upload all quarters
                results.extend(self.upload_10q_filings(ticker, year, force))
            else:
                result = self.upload_filing(ticker, year, form_type, force)
                if result:
                    results.append(result)
        
        return results
    
    def upload_all_years(self, ticker: str, start_year: int = 2020,
                         end_year: int = None, form_types: List[str] = None,
                         force: bool = False) -> List[UploadResult]:
        """
        Upload filings for all years in range.
        
        Args:
            ticker: Company ticker
            start_year: Start year (default: 2020)
            end_year: End year (default: current year)
            form_types: Form types to upload
            force: Overwrite existing
            
        Returns:
            List of UploadResult
        """
        if end_year is None:
            end_year = datetime.now().year
        
        results = []
        
        for year in range(start_year, end_year + 1):
            year_results = self.upload_company_year(ticker, year, form_types, force)
            results.extend(year_results)
        
        return results
    
    def print_summary(self):
        """Print upload statistics summary."""
        self.logger.info("=" * 70)
        self.logger.info("UPLOAD SUMMARY")
        self.logger.info("=" * 70)
        self.logger.info(f"  📤 Uploaded:    {self.stats['uploaded']}")
        self.logger.info(f"  ⏭️  Exists:      {self.stats['exists']}")
        self.logger.info(f"  ⏭️  Skipped:     {self.stats['skipped']}")
        self.logger.info(f"  ❌ Errors:      {self.stats['errors']}")
        self.logger.info(f"  📊 Total Bytes: {self.stats['total_bytes']:,}")
        self.logger.info("=" * 70)


# =============================================================================
# COMMAND LINE INTERFACE
# =============================================================================
def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Upload SEC filings to Azure Blob Storage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Upload specific company/year
    python azure_filing_uploader.py --ticker AAPL --year 2024

    # Upload all filings for a company (all years)
    python azure_filing_uploader.py --ticker AAPL --all-years

    # Upload multiple companies
    python azure_filing_uploader.py --tickers AAPL,MSFT,GOOGL --year 2024

    # Force re-upload (overwrite existing)
    python azure_filing_uploader.py --ticker AAPL --year 2024 --force

    # Dry run - show what would be uploaded
    python azure_filing_uploader.py --ticker AAPL --year 2024 --dry-run

    # Upload only 10-K (no 10-Qs)
    python azure_filing_uploader.py --ticker AAPL --year 2024 --form-types 10-K

    # Upload with verbose logging
    python azure_filing_uploader.py --ticker AAPL --year 2024 --verbose
        """
    )
    
    # Ticker options
    ticker_group = parser.add_mutually_exclusive_group(required=True)
    ticker_group.add_argument(
        "--ticker", "-t",
        help="Single ticker symbol (e.g., AAPL)"
    )
    ticker_group.add_argument(
        "--tickers",
        help="Comma-separated list of tickers (e.g., AAPL,MSFT,GOOGL)"
    )
    
    # Year options
    year_group = parser.add_mutually_exclusive_group()
    year_group.add_argument(
        "--year", "-y", type=int,
        help="Specific year to upload (e.g., 2024)"
    )
    year_group.add_argument(
        "--all-years", action="store_true",
        help="Upload all years from 2020 to current year"
    )
    year_group.add_argument(
        "--year-range",
        help="Year range as START,END (e.g., 2020,2024)"
    )
    
    # Form type options
    parser.add_argument(
        "--form-types",
        default="10-K,10-Q",
        help="Comma-separated form types (default: 10-K,10-Q)"
    )
    
    # Behavior options
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="Force re-upload (overwrite existing files)"
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Dry run - show what would be uploaded without actually uploading"
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=4,
        help="Number of parallel workers for batch uploads (default: 4)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging"
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    # Setup logging
    logger = setup_logging(verbose=args.verbose)
    
    # Parse tickers
    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    
    # Parse form types
    form_types = [ft.strip().upper() for ft in args.form_types.split(",")]
    
    # Parse years
    if args.all_years:
        start_year = 2020
        end_year = datetime.now().year
        years = list(range(start_year, end_year + 1))
    elif args.year_range:
        start, end = map(int, args.year_range.split(","))
        years = list(range(start, end + 1))
    elif args.year:
        years = [args.year]
    else:
        # Default to current year if no year specified
        years = [datetime.now().year]
    
    # Print configuration
    logger.info("=" * 70)
    logger.info("AZURE FILING UPLOADER")
    logger.info("=" * 70)
    logger.info(f"Tickers:     {', '.join(tickers)}")
    logger.info(f"Years:       {', '.join(map(str, years))}")
    logger.info(f"Form Types:  {', '.join(form_types)}")
    logger.info(f"Force:       {args.force}")
    logger.info(f"Dry Run:     {args.dry_run}")
    logger.info(f"Workers:     {args.workers}")
    logger.info(f"Azure:       {AZURE_ACCOUNT}/{AZURE_CONTAINER}")
    logger.info("=" * 70)
    
    if args.dry_run:
        logger.info("🔍 DRY RUN MODE - No actual uploads will occur")
        logger.info("=" * 70)
    
    # Initialize uploader
    uploader = AzureFilingUploader(logger, dry_run=args.dry_run)
    
    # Process all ticker/year combinations
    all_results = []
    
    for ticker in tickers:
        for year in years:
            results = uploader.upload_company_year(
                ticker=ticker,
                year=year,
                form_types=form_types,
                force=args.force
            )
            all_results.extend(results)
    
    # Print summary
    uploader.print_summary()
    
    # Exit code based on errors
    error_count = sum(1 for r in all_results if r.status == UploadStatus.ERROR)
    if error_count > 0:
        logger.warning(f"Completed with {error_count} errors")
        sys.exit(1)
    else:
        logger.info("All uploads completed successfully")
        sys.exit(0)


if __name__ == "__main__":
    main()
