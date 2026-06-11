"""
Structured dummy data for Phase 1 development.
Designed to be replaced with database queries in Phase 2 without UI refactoring.
"""
from datetime import datetime, date, timedelta
import random
from typing import List, Dict, Any, Optional

from .models import (
    SECFiling, SECFormType, MarketData, MarketMetric,
    NewsArticle, NewsCategory, DataFrequency
)


# ============================================================================
# SEC FILINGS DATA
# ============================================================================

def generate_sec_filings() -> List[SECFiling]:
    """Generate realistic SEC filing dummy data."""
    companies = [
        ("Apple Inc.", "AAPL", "0000320193"),
        ("Microsoft Corporation", "MSFT", "0000789019"),
        ("Amazon.com Inc.", "AMZN", "0001018724"),
        ("Tesla Inc.", "TSLA", "0001318605"),
        ("Alphabet Inc.", "GOOGL", "0001652044"),
        ("Meta Platforms Inc.", "META", "0001326801"),
        ("NVIDIA Corporation", "NVDA", "0001014122"),
        ("JPMorgan Chase & Co.", "JPM", "0000019672"),
        ("Johnson & Johnson", "JNJ", "0000200406"),
        ("Visa Inc.", "V", "0001403161"),
    ]
    
    form_types = [
        (SECFormType.FORM_10_K, "Annual Report"),
        (SECFormType.FORM_10_Q, "Quarterly Report"),
        (SECFormType.FORM_8_K, "Current Report"),
    ]
    
    filings = []
    base_date = date(2024, 1, 1)
    
    for i, (company, ticker, cik) in enumerate(companies):
        for j, (form_type, description) in enumerate(form_types):
            # Generate multiple filings per company
            for k in range(3):
                filing_date = base_date + timedelta(days=i * 10 + j * 30 + k * 90)
                
                filing = SECFiling(
                    id=f"FIL-{ticker}-{form_type.value}-{k}",
                    company_name=company,
                    ticker=ticker,
                    form_type=form_type,
                    filing_date=filing_date,
                    report_date=filing_date - timedelta(days=random.randint(1, 30)),
                    accession_number=f"000{cik}-{filing_date.strftime('%Y%m%d')}",
                    file_number=f"001-{random.randint(10000, 99999)}",
                    cik=cik,
                    items=["Item 1", "Item 2"] if form_type == SECFormType.FORM_8_K else [],
                    documents=[
                        {"type": form_type.value, "filename": f"{ticker}-{form_type.value}.htm"}
                    ],
                )
                filings.append(filing)
    
    return sorted(filings, key=lambda x: x.filing_date, reverse=True)


SEC_FILINGS_DATA: List[SECFiling] = generate_sec_filings()


# ============================================================================
# MARKET DATA
# ============================================================================

def generate_market_data(ticker: str = "AAPL", days: int = 252) -> MarketData:
    """Generate realistic market price data."""
    companies = {
        "AAPL": ("Apple Inc.", "NASDAQ", "USD"),
        "MSFT": ("Microsoft Corporation", "NASDAQ", "USD"),
        "AMZN": ("Amazon.com Inc.", "NASDAQ", "USD"),
        "TSLA": ("Tesla Inc.", "NASDAQ", "USD"),
        "GOOGL": ("Alphabet Inc.", "NASDAQ", "USD"),
        "META": ("Meta Platforms Inc.", "NASDAQ", "USD"),
        "NVDA": ("NVIDIA Corporation", "NASDAQ", "USD"),
        "JPM": ("JPMorgan Chase & Co.", "NYSE", "USD"),
        "JNJ": ("Johnson & Johnson", "NYSE", "USD"),
        "V": ("Visa Inc.", "NYSE", "USD"),
    }
    
    company_info = companies.get(ticker, (f"Company {ticker}", "NASDAQ", "USD"))
    
    # Generate price history
    base_price = random.uniform(50, 500)
    metrics = []
    
    end_date = date.today()
    
    for i in range(days):
        current_date = end_date - timedelta(days=days - i - 1)
        
        # Random walk with slight upward bias
        change = random.gauss(0.001, 0.02)
        base_price = base_price * (1 + change)
        
        daily_volatility = base_price * 0.02
        open_price = base_price + random.gauss(0, daily_volatility * 0.3)
        close_price = base_price + random.gauss(0, daily_volatility * 0.3)
        high_price = max(open_price, close_price) + random.uniform(0, daily_volatility)
        low_price = min(open_price, close_price) - random.uniform(0, daily_volatility)
        volume = random.randint(1_000_000, 100_000_000)
        
        metric = MarketMetric(
            date=current_date,
            open_price=round(open_price, 2),
            high_price=round(high_price, 2),
            low_price=round(low_price, 2),
            close_price=round(close_price, 2),
            volume=volume,
            adjusted_close=round(close_price, 2),
        )
        metrics.append(metric)
    
    market_data = MarketData(
        ticker=ticker,
        company_name=company_info[0],
        exchange=company_info[1],
        currency=company_info[2],
        metrics=metrics,
        market_cap=random.uniform(100_000_000_000, 3_000_000_000_000),
        pe_ratio=random.uniform(15, 45),
    )
    
    market_data.compute_metrics()
    return market_data


# Pre-generate market data for common tickers
MARKET_DATA_CACHE: Dict[str, MarketData] = {
    ticker: generate_market_data(ticker)
    for ticker in ["AAPL", "MSFT", "AMZN", "TSLA", "GOOGL", "META", "NVDA", "JPM", "JNJ", "V"]
}


# ============================================================================
# NEWS ARTICLES DATA
# ============================================================================

def generate_news_articles() -> List[NewsArticle]:
    """Generate realistic news article dummy data."""
    articles_data = [
        {
            "headline": "Apple Reports Record Q4 Revenue Despite Supply Chain Challenges",
            "summary": "Apple Inc. announced quarterly revenue of $89.5 billion, up 8% year over year, driven by strong iPhone and Services performance.",
            "category": NewsCategory.EARNINGS,
            "tickers": ["AAPL"],
            "source": "Reuters",
            "author": "Sarah Chen",
            "is_featured": True,
        },
        {
            "headline": "Federal Reserve Signals Potential Rate Cuts in 2024",
            "summary": "The Federal Reserve indicated it may begin cutting interest rates next year as inflation shows signs of cooling.",
            "category": NewsCategory.MARKET_MOVES,
            "tickers": ["SPY", "QQQ"],
            "source": "Bloomberg",
            "author": "Michael Roberts",
            "is_breaking": True,
        },
        {
            "headline": "Tesla Expands Gigafactory Network with New Mexico Facility",
            "summary": "Tesla announces plans for a new manufacturing facility in Mexico, expected to create 10,000 jobs and double production capacity.",
            "category": NewsCategory.PRODUCT_LAUNCH,
            "tickers": ["TSLA"],
            "source": "CNBC",
            "author": "Emily Watson",
        },
        {
            "headline": "Microsoft Completes $69 Billion Activision Blizzard Acquisition",
            "summary": "After regulatory approval in multiple jurisdictions, Microsoft finalizes its landmark gaming acquisition.",
            "category": NewsCategory.MERGERS_ACQUISITIONS,
            "tickers": ["MSFT", "ATVI"],
            "source": "WSJ",
            "author": "David Park",
            "is_featured": True,
        },
        {
            "headline": "NVIDIA Unveils Next-Generation AI Chips, Stock Surges",
            "summary": "The new H200 chip promises 90% performance improvement for AI workloads, solidifying NVIDIA's market leadership.",
            "category": NewsCategory.PRODUCT_LAUNCH,
            "tickers": ["NVDA"],
            "source": "TechCrunch",
            "author": "James Liu",
        },
        {
            "headline": "JPMorgan Reports Strong Q3 Results, Beats Estimates",
            "summary": "Investment banking fees rebound as deal activity picks up, driving better-than-expected earnings.",
            "category": NewsCategory.EARNINGS,
            "tickers": ["JPM", "BAC", "C"],
            "source": "Financial Times",
            "author": "Amanda Foster",
        },
        {
            "headline": "SEC Proposes New Climate Disclosure Rules for Public Companies",
            "summary": "Public companies may soon be required to report detailed greenhouse gas emissions and climate-related financial risks.",
            "category": NewsCategory.REGULATORY,
            "tickers": ["SPY", "ESG"],
            "source": "Reuters",
            "author": "Robert Klein",
        },
        {
            "headline": "Amazon Web Services Launches New AI-Powered Analytics Platform",
            "summary": "The new service integrates machine learning capabilities directly into data warehouses, competing with Google's BigQuery.",
            "category": NewsCategory.PRODUCT_LAUNCH,
            "tickers": ["AMZN", "GOOGL"],
            "source": "AWS Blog",
            "author": "Lisa Martinez",
        },
        {
            "headline": "Meta Announces Major Layoffs, Restructuring Plans",
            "summary": "The company will reduce workforce by 10% as it shifts focus toward AI and metaverse investments.",
            "category": NewsCategory.LEADERSHIP,
            "tickers": ["META"],
            "source": "The Verge",
            "author": "Alex Thompson",
        },
        {
            "headline": "Visa Expands Cross-Border Payment Solutions in Asia-Pacific",
            "summary": "New partnerships with regional banks aim to capture growing e-commerce market in Southeast Asia.",
            "category": NewsCategory.MARKET_MOVES,
            "tickers": ["V", "MA"],
            "source": "Business Insider",
            "author": "Jennifer Ho",
        },
        {
            "headline": "Johnson & Johnson Faces Patent Cliff, Pipeline Progress Critical",
            "summary": "Analysts await updates on key drug candidates as Stelara patent expiration approaches.",
            "category": NewsCategory.REGULATORY,
            "tickers": ["JNJ"],
            "source": "Fierce Pharma",
            "author": "Dr. Mark Stevens",
        },
        {
            "headline": "Tech Giants Rally as AI Optimism Drives Market Higher",
            "summary": "Nasdaq Composite reaches new highs as investors bet on artificial intelligence transformation.",
            "category": NewsCategory.MARKET_MOVES,
            "tickers": ["AAPL", "MSFT", "GOOGL", "NVDA", "META"],
            "source": "MarketWatch",
            "author": "Chris Williams",
        },
    ]
    
    articles = []
    base_time = datetime.now()
    
    for i, article_data in enumerate(articles_data):
        published_at = base_time - timedelta(hours=i * 6 + random.randint(0, 360))
        
        article = NewsArticle(
            id=f"NEWS-{i+1:04d}",
            headline=article_data["headline"],
            summary=article_data["summary"],
            content=article_data["summary"] + "\n\n" + "This is placeholder content for the full article. " * 10,
            source=article_data["source"],
            author=article_data["author"],
            published_at=published_at,
            category=article_data["category"],
            tags=[article_data["category"].value, "stocks", "market"],
            related_tickers=article_data["tickers"],
            image_url=f"https://picsum.photos/800/400?random={i}",
            url=f"#article-{i+1}",
            views=random.randint(1000, 500000),
            likes=random.randint(50, 5000),
            is_featured=article_data.get("is_featured", False),
            is_breaking=article_data.get("is_breaking", False),
        )
        articles.append(article)
    
    return sorted(articles, key=lambda x: x.published_at, reverse=True)


NEWS_ARTICLES_DATA: List[NewsArticle] = generate_news_articles()


# ============================================================================
# DATA ACCESS LAYER (Repository Pattern)
# ============================================================================

class SECFilingRepository:
    """Repository for SEC filing data access.
    
    Phase 1: Uses dummy data from Python lists
    Phase 2: Will query MySQL database using same interface
    """
    
    @staticmethod
    def get_all_filings(
        ticker: Optional[str] = None,
        form_type: Optional[SECFormType] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[SECFiling]:
        """Get filings with optional filtering."""
        filings = SEC_FILINGS_DATA
        
        if ticker:
            filings = [f for f in filings if f.ticker.upper() == ticker.upper()]
        
        if form_type:
            filings = [f for f in filings if f.form_type == form_type]
        
        if date_from:
            filings = [f for f in filings if f.filing_date >= date_from]
        
        if date_to:
            filings = [f for f in filings if f.filing_date <= date_to]
        
        return filings[offset:offset + limit]
    
    @staticmethod
    def get_filing_by_id(filing_id: str) -> Optional[SECFiling]:
        """Get single filing by ID."""
        for filing in SEC_FILINGS_DATA:
            if filing.id == filing_id:
                return filing
        return None
    
    @staticmethod
    def get_available_tickers() -> List[str]:
        """Get list of all available tickers."""
        return sorted(set(f.ticker for f in SEC_FILINGS_DATA))
    
    @staticmethod
    def get_form_types() -> List[SECFormType]:
        """Get list of available form types."""
        return list(SECFormType)


class MarketDataRepository:
    """Repository for market data access.
    
    Phase 1: Uses generated dummy data
    Phase 2: Will query market data API/database
    """
    
    @staticmethod
    def get_market_data(ticker: str) -> Optional[MarketData]:
        """Get market data for a ticker."""
        if ticker.upper() in MARKET_DATA_CACHE:
            return MARKET_DATA_CACHE[ticker.upper()]
        
        # Generate on-demand for unknown tickers
        return generate_market_data(ticker)
    
    @staticmethod
    def get_available_tickers() -> List[str]:
        """Get list of available tickers."""
        return list(MARKET_DATA_CACHE.keys())
    
    @staticmethod
    def get_price_history(
        ticker: str,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        frequency: DataFrequency = DataFrequency.DAILY
    ) -> List[MarketMetric]:
        """Get price history with filtering."""
        data = MarketDataRepository.get_market_data(ticker)
        if not data:
            return []
        
        metrics = data.metrics
        
        if date_from:
            metrics = [m for m in metrics if m.date >= date_from]
        
        if date_to:
            metrics = [m for m in metrics if m.date <= date_to]
        
        # Apply frequency aggregation if needed
        if frequency != DataFrequency.DAILY:
            metrics = MarketDataRepository._aggregate_metrics(metrics, frequency)
        
        return metrics
    
    @staticmethod
    def _aggregate_metrics(
        metrics: List[MarketMetric],
        frequency: DataFrequency
    ) -> List[MarketMetric]:
        """Aggregate daily metrics to higher frequency."""
        # Simplified aggregation - in production, use proper OHLCV aggregation
        return metrics  # Placeholder


class NewsRepository:
    """Repository for news article data access.
    
    Phase 1: Uses dummy data from Python lists
    Phase 2: Will query news database/API
    """
    
    @staticmethod
    def get_articles(
        search_query: Optional[str] = None,
        categories: Optional[List[NewsCategory]] = None,
        tickers: Optional[List[str]] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        sources: Optional[List[str]] = None,
        sort_by: str = "published_at",
        sort_order: str = "desc",
        limit: int = 20,
        offset: int = 0
    ) -> List[NewsArticle]:
        """Get articles with filtering and sorting."""
        articles = NEWS_ARTICLES_DATA.copy()
        
        if search_query:
            query = search_query.lower()
            articles = [
                a for a in articles
                if query in a.headline.lower() or query in a.summary.lower()
            ]
        
        if categories:
            articles = [a for a in articles if a.category in categories]
        
        if tickers:
            articles = [
                a for a in articles
                if any(t.upper() in [rt.upper() for rt in a.related_tickers] for t in tickers)
            ]
        
        if date_from:
            articles = [a for a in articles if a.published_at.date() >= date_from]
        
        if date_to:
            articles = [a for a in articles if a.published_at.date() <= date_to]
        
        if sources:
            articles = [a for a in articles if a.source in sources]
        
        # Sort
        reverse = sort_order == "desc"
        if sort_by == "published_at":
            articles.sort(key=lambda x: x.published_at, reverse=reverse)
        elif sort_by == "views":
            articles.sort(key=lambda x: x.views, reverse=reverse)
        elif sort_by == "likes":
            articles.sort(key=lambda x: x.likes, reverse=reverse)
        
        return articles[offset:offset + limit]
    
    @staticmethod
    def get_article_by_id(article_id: str) -> Optional[NewsArticle]:
        """Get single article by ID."""
        for article in NEWS_ARTICLES_DATA:
            if article.id == article_id:
                return article
        return None
    
    @staticmethod
    def get_featured_articles(limit: int = 3) -> List[NewsArticle]:
        """Get featured articles."""
        featured = [a for a in NEWS_ARTICLES_DATA if a.is_featured]
        return featured[:limit]
    
    @staticmethod
    def get_breaking_news(limit: int = 1) -> List[NewsArticle]:
        """Get breaking news."""
        breaking = [a for a in NEWS_ARTICLES_DATA if a.is_breaking]
        return breaking[:limit]
    
    @staticmethod
    def get_available_sources() -> List[str]:
        """Get list of all news sources."""
        return sorted(set(a.source for a in NEWS_ARTICLES_DATA))
    
    @staticmethod
    def get_available_categories() -> List[NewsCategory]:
        """Get list of available categories."""
        return list(NewsCategory)
