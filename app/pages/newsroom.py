"""
Newsroom Page - Coresight Research
==================================
Financial news feed with filtering and sentiment analysis.

Progressive Loading Architecture (2026-04-03):
  On first load, user sees last 7 days immediately.  A non-blocking
  background thread then pre-populates @st.cache_data for weeks 2-4
  so that expanding the date range to "last 1 month" is near-instant.
  Any arbitrary date range is split into 7-day chunks and fetched in
  parallel — each chunk is a separate @st.cache_data entry, reusable
  across any overlapping date selection.
"""
import streamlit as st
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import threading
import math

from components.styles import hide_sidebar, set_page_layout, render_styles, COLORS, TYPOGRAPHY, SPACING
from core.auth_manager import require_auth, get_current_user

# require_auth(page="newsroom")
hide_sidebar()
from components.navigation import render_header, render_coresight_footer
from data.models import NewsArticle, TickerSentiment
from data.repository import NewsRepository, CompanyRepository, _format_company_name
from data.watchlist_service import get_user_watchlists, get_watchlist_companies
from core.database import init_database
from utils.ticker_utils import validate_and_get_ticker, DEFAULT_FALLBACK_TICKER
from utils.constants import NEWS_TOPIC_LABELS, get_topic_display_label
from utils.server_logger import (
    PageLoadTracker, new_rerun_id,
    log_structured_error, log_warning,
)
import time


# =============================================================================
# PROGRESSIVE LOADING — WEEK-CHUNKED FETCH ENGINE
# =============================================================================
_CHUNK_DAYS = 7          # each chunk spans 7 days
_MAX_WORKERS = 6         # max parallel DB workers for chunk fetching
_BG_PREFETCH_WEEKS = 4   # how many extra weeks to pre-populate in background


def _split_into_chunks(
    d_from: date, d_to: date, chunk_days: int = _CHUNK_DAYS,
) -> List[Tuple[date, date]]:
    """Split [d_from, d_to] into **full ISO-week** chunks (Mon–Sun).

    Using full-week boundaries (instead of clamping edges to d_from/d_to)
    guarantees that the @st.cache_data key for each chunk is identical
    regardless of the caller's overall date range.  A chunk cached during
    a 7-day load is reused byte-for-byte when the user expands to 30 days.

    Edge chunks may extend slightly beyond [d_from, d_to]; the caller
    filters out-of-range articles during the merge step.

    Chunks are returned **newest-first** so the most-recent data arrives
    first when fetched in parallel.
    """
    if d_from > d_to:
        return [(d_to, d_from)]

    chunks: List[Tuple[date, date]] = []

    # Find the Monday of d_to's week and the Sunday of d_from's week
    last_monday = d_to - timedelta(days=d_to.weekday())
    first_monday = d_from - timedelta(days=d_from.weekday())

    cursor_monday = last_monday
    while cursor_monday >= first_monday:
        chunk_start = cursor_monday
        chunk_end = cursor_monday + timedelta(days=6)  # Sunday
        chunks.append((chunk_start, chunk_end))
        cursor_monday -= timedelta(days=7)

    return chunks  # newest chunk first


def _fetch_chunked(
    d_from: date, d_to: date,
    sector: Optional[str], company_ticker: Optional[str],
    sort_ascending: bool,
    *,
    av_limit: int = 1000,
    yf_limit: int = 2000,
) -> Tuple[List[NewsArticle], List[NewsArticle]]:
    """Fetch AV + YF articles for an arbitrary date range using week-chunks.

    Each chunk is a separate call to the @st.cache_data-decorated repository
    methods, so identical chunks across different date-range selections are
    served from cache instantly.

    Returns (all_av_articles, all_yf_articles) merged across all chunks.
    """
    chunks = _split_into_chunks(d_from, d_to)
    n_chunks = len(chunks)
    # Each chunk needs 2 workers (AV + YF).  Cap total workers.
    workers = min(_MAX_WORKERS, n_chunks * 2)

    all_av: List[NewsArticle] = []
    all_yf: List[NewsArticle] = []

    if n_chunks == 1:
        # Single chunk — no extra threading overhead
        c_from, c_to = chunks[0]
        all_av = NewsRepository.get_articles(
            date_from=c_from, date_to=c_to,
            sector=sector, company_ticker=company_ticker,
            keyword=None, limit=av_limit, offset=0,
            sort_ascending=sort_ascending,
        )
        all_yf = NewsRepository.get_yf_articles(
            date_from=c_from, date_to=c_to,
            company_ticker=company_ticker, keyword=None,
            limit=yf_limit, sort_ascending=sort_ascending,
            sector=sector,
        )
    else:
        # Multi-chunk — fire all AV + YF calls in one pool
        av_results: dict = {}  # chunk_idx -> List[NewsArticle]
        yf_results: dict = {}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {}
            for idx, (c_from, c_to) in enumerate(chunks):
                f_av = pool.submit(
                    NewsRepository.get_articles,
                    date_from=c_from, date_to=c_to,
                    sector=sector, company_ticker=company_ticker,
                    keyword=None, limit=av_limit, offset=0,
                    sort_ascending=sort_ascending,
                )
                future_map[f_av] = ('av', idx)

                f_yf = pool.submit(
                    NewsRepository.get_yf_articles,
                    date_from=c_from, date_to=c_to,
                    company_ticker=company_ticker, keyword=None,
                    limit=yf_limit, sort_ascending=sort_ascending,
                    sector=sector,
                )
                future_map[f_yf] = ('yf', idx)

            for fut in as_completed(future_map):
                kind, idx = future_map[fut]
                try:
                    result = fut.result(timeout=120)
                    if kind == 'av':
                        av_results[idx] = result
                    else:
                        yf_results[idx] = result
                except Exception as exc:
                    log_warning(f"[CHUNKED_FETCH] {kind} chunk {idx} failed: {exc}")
                    if kind == 'av':
                        av_results[idx] = []
                    else:
                        yf_results[idx] = []

        # Reassemble in chunk order (newest first)
        for idx in range(n_chunks):
            all_av.extend(av_results.get(idx, []))
            all_yf.extend(yf_results.get(idx, []))

    # ── Edge trim: full-week chunks may include articles outside [d_from, d_to].
    # Filter them out so the caller sees only the requested range.
    _d_from_dt = datetime.combine(d_from, datetime.min.time())
    _d_to_dt   = datetime.combine(d_to, datetime.max.time())
    _pre_av, _pre_yf = len(all_av), len(all_yf)
    all_av = [a for a in all_av if _d_from_dt <= a.time_published <= _d_to_dt]
    all_yf = [a for a in all_yf if _d_from_dt <= a.time_published <= _d_to_dt]

    return all_av, all_yf


def _spawn_background_prefetch(
    base_date_from: date,
    base_date_to: date,
    sector: Optional[str],
    company_ticker: Optional[str],
    sort_ascending: bool,
    extra_weeks: int = _BG_PREFETCH_WEEKS,
):
    """Spawn a daemon thread that pre-populates @st.cache_data for *extra_weeks*
    worth of weekly chunks BEFORE base_date_from.

    This is purely non-blocking — if it fails, user simply gets a cold query
    when they expand the date range (same as before this feature).

    The thread does NOT touch st.session_state (no add_script_run_ctx needed).
    It only calls @st.cache_data-decorated functions to warm them.
    """
    prefetch_to = base_date_from - timedelta(days=1)
    prefetch_from = prefetch_to - timedelta(days=extra_weeks * 7 - 1)

    if prefetch_from > prefetch_to:
        return

    def _bg_worker():
        try:
            _t = time.perf_counter()
            chunks = _split_into_chunks(prefetch_from, prefetch_to)
            # Use fewer workers to leave headroom for foreground queries
            workers = min(4, len(chunks) * 2)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = []
                for c_from, c_to in chunks:
                    futs.append(pool.submit(
                        NewsRepository.get_articles,
                        date_from=c_from, date_to=c_to,
                        sector=None, company_ticker=None,
                        keyword=None, limit=1000, offset=0,
                        sort_ascending=False,
                    ))
                    futs.append(pool.submit(
                        NewsRepository.get_yf_articles,
                        date_from=c_from, date_to=c_to,
                        company_ticker=None, keyword=None,
                        limit=2000, sort_ascending=False,
                        sector=None,
                    ))
                done = 0
                failed = 0
                for f in as_completed(futs):
                    try:
                        f.result(timeout=120)
                        done += 1
                    except Exception:
                        failed += 1
            _ms = (time.perf_counter() - _t) * 1000
            log_warning(f"[BG_PREFETCH] Completed in {_ms:.0f}ms | "
                        f"weeks={extra_weeks} chunks={len(chunks)} "
                        f"done={done} failed={failed} "
                        f"range={prefetch_from}..{prefetch_to}")
        except Exception as exc:
            log_warning(f"[BG_PREFETCH] Thread error: {exc}")

    t = threading.Thread(target=_bg_worker, name="NewsroomBgPrefetch", daemon=True)
    t.start()
    log_warning(f"[BG_PREFETCH] Started for {prefetch_from}..{prefetch_to} ({extra_weeks} weeks, {len(_split_into_chunks(prefetch_from, prefetch_to))} chunks)")


# =============================================================================
# INITIALIZE
# =============================================================================
def initialize_app():
    """Initialize application state and dependencies.

    NOTE (2026-04-02): Removed redundant _warm() thread that slept 6s then
    called get_articles() + get_yf_articles(). The page's own prefetch
    (render_page lines 718-732) already fires these queries immediately.
    Background pre-warming is handled by cache_manager.start_background_warmup()
    in main.py.
    """
    try:
        init_database()
    except Exception as _exc:
        log_structured_error(_exc, page="newsroom", component="initialize_app", operation="init_database")
        return


def get_company_name_map() -> dict:
    """Get mapping of ticker to company name (session-cached)."""
    try:
        if 'company_name_map' not in st.session_state:
            from data.repository import CompanyRepository
            companies_map = CompanyRepository.get_companies_map()
            st.session_state.company_name_map = {
                ticker: _format_company_name(c['name_coresight'])
                for ticker, c in companies_map.items()
            }
        return st.session_state.company_name_map
    except Exception as _exc:
        log_structured_error(_exc, page="newsroom", component="get_company_name_map", operation="fetch_company_names")
        return {}


def _get_ticker_sector(ticker: str) -> Optional[str]:
    try:
        from data.repository import CompanyRepository
        return CompanyRepository.get_company_sector(ticker)
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="_get_ticker_sector", operation="GET_SECTOR")
        return {}


def format_company_display(ticker: str, company_map: dict) -> str:
    return company_map.get(ticker, ticker)


def calculate_relative_time(published_time: datetime) -> str:
    """Calculate relative time string on server side."""
    try:
        now = datetime.now(published_time.tzinfo) if published_time.tzinfo else datetime.now()
        if not published_time.tzinfo:
            published_time = published_time.replace(tzinfo=None)
            now = now.replace(tzinfo=None)
        diff = now - published_time
        diff_seconds = diff.total_seconds()
        if diff_seconds < 0:
            return '(just now)'
        diff_mins = int(diff_seconds / 60)
        diff_hours = int(diff_seconds / 3600)
        diff_days = int(diff_seconds / 86400)
        if diff_mins < 1:
            return '(just now)'
        elif diff_mins < 60:
            return f'({diff_mins} minute{"s" if diff_mins != 1 else ""} ago)'
        elif diff_hours < 24:
            remaining_mins = diff_mins % 60
            if remaining_mins == 0:
                return f'({diff_hours} hour{"s" if diff_hours != 1 else ""} ago)'
            else:
                return f'({diff_hours} hour{"s" if diff_hours != 1 else ""}, {remaining_mins} minute{"s" if remaining_mins != 1 else ""} ago)'
        elif diff_days == 1:
            return '(1 day ago)'
        else:
            return f'({diff_days} days ago)'
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="calculate_relative_time", operation="CALC_TIME")
        return ''


def _highlight_keyword(text: str, keyword: str) -> str:
    """Wrap keyword matches with <mark> tags."""
    try:
        if not keyword or not keyword.strip():
            return text
        words = keyword.strip().split()
        result = text
        for word in words:
            if word.strip():
                pattern = re.compile(re.escape(word.strip()), re.IGNORECASE)
                result = pattern.sub(lambda m: f'<mark>{m.group()}</mark>', result)
        return result
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="_highlight_keyword", operation="HIGHLIGHT")
        return text


def _union_and_sort_articles(
    av_articles: List[NewsArticle],
    yf_articles: List[NewsArticle],
    sort_ascending: bool = False,
) -> List[NewsArticle]:
    """Union AV + YF articles, sort by time_published."""
    try:
        combined = list(av_articles) + list(yf_articles)
        _epoch = datetime.min
        combined.sort(
            key=lambda a: a.time_published if a.time_published else _epoch,
            reverse=not sort_ascending,
        )
        return combined
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="_union_and_sort_articles", operation="MERGE_SORT")
        return list(av_articles) if av_articles else list(yf_articles) if yf_articles else []


def _filter_articles_by_keyword(
    articles: List[NewsArticle],
    keyword: Optional[str],
) -> List[NewsArticle]:
    """Filter loaded articles by keyword on title + summary."""
    try:
        if not keyword or not keyword.strip():
            return articles
        kw = keyword.strip().lower()
        if len(kw) < 2:
            return articles
        return [
            a for a in articles
            if kw in (a.title or '').lower() or kw in (a.summary or '').lower()
        ]
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="_filter_articles_by_keyword", operation="FILTER_KEYWORD")
        return articles


def _filter_yf_by_sector(
    articles: List[NewsArticle],
    sector: Optional[str],
) -> List[NewsArticle]:
    """Filter YF articles by sector.
    YF articles have no sector data, so they all belong to 'Unknown Sector'.
    - 'Unknown Sector' selected → return all YF articles
    - Any other sector selected → return empty (YF has no sector assignment)
    - No sector (All) → return all
    """
    if not sector:
        return articles
    if sector == "Unknown Sector":
        return articles
    return []


def _filter_av_by_sector(
    articles: List[NewsArticle],
    sector: Optional[str],
) -> List[NewsArticle]:
    """Filter AV articles by sector using ticker→sector mapping.

    Uses NewsRepository.get_ticker_sector_map() (cached) which maps
    ticker → title-cased sector from coreiq_av_companies_all.sector.
    Same source table as the sector dropdown → guaranteed match.
    """
    if not sector:
        return articles
    if sector == "Unknown Sector":
        return []  # AV articles always have sectors

    try:
        ticker_sector_map = NewsRepository.get_ticker_sector_map()

        if sector == "None":
            def _has_no_sector(article):
                for ts in article.ticker_sentiment:
                    s = ticker_sector_map.get(ts.ticker)
                    if s and s != 'None':
                        return False
                return True
            return [a for a in articles if _has_no_sector(a)]

        def _matches_sector(article):
            for ts in article.ticker_sentiment:
                if ticker_sector_map.get(ts.ticker) == sector:
                    return True
            return False
        return [a for a in articles if _matches_sector(a)]
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="_filter_av_by_sector", operation="FILTER_AV_SECTOR")
        return articles


def _filter_articles_by_topic(
    articles: List[NewsArticle],
    topic_key: Optional[str],
) -> List[NewsArticle]:
    """Client-side filter: keep only articles whose topics_json contains the topic.
    Special case: 'uncategorized' matches articles with no topics (e.g. YF articles).
    """
    try:
        if not topic_key:
            return articles
        if topic_key == 'uncategorized':
            return [a for a in articles if not a.topics]
        return [
            a for a in articles
            if any(t.get('topic') == topic_key for t in (a.topics or []))
        ]
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="_filter_articles_by_topic", operation="FILTER_TOPIC")
        return articles


def render_news_card(article: NewsArticle, company_map: dict,
                     keyword: str = None, idx: int = 0) -> str:
    """Build HTML string for a single news article card."""
    try:
        formatted_date = article.formatted_date
        relative_time = calculate_relative_time(article.time_published)

        tagged_companies_html = ""
        if article.ticker_sentiment:
            companies_parts = []
            for ts in article.ticker_sentiment:
                company_name = format_company_display(ts.ticker, company_map)
                tooltip_text = (
                    f"Relevance: {float(ts.relevance_score)*100:.1f}% | "
                    f"Sentiment: {ts.ticker_sentiment_label} ({float(ts.ticker_sentiment_score):.2f})"
                )
                if ts.ticker in company_map:
                    company_html = (
                        f'<a href="/market_data?ticker={ts.ticker}" class="company-link"'
                        f' title="{tooltip_text}">{company_name}</a>'
                    )
                else:
                    company_html = (
                        f'<span class="company-tag-inactive"'
                        f' title="{tooltip_text}">{company_name}</span>'
                    )
                companies_parts.append(company_html)
            tagged_companies_html = (
                "<span class='tagged-label'>Tagged Companies: </span>"
                + " | ".join(companies_parts)
            )

        display_title   = _highlight_keyword(article.title, keyword)   if keyword else article.title
        display_summary = _highlight_keyword(article.summary, keyword)  if keyword else article.summary

        return f"""
    <div class="news-card" id="article-{idx}">
        <div class="news-header">
            <div class="news-source">{article.source}</div>
            <div class="news-date">
                {formatted_date} <span class="relative-time">{relative_time}</span>
            </div>
        </div>
        <a href="{article.url}" target="_blank" class="news-title-link" onclick="event.stopPropagation();">{display_title}</a>
        <div class="news-summary">{display_summary}</div>
        {f'<div class="tagged-companies">{tagged_companies_html}</div>' if tagged_companies_html else ''}
    </div>
    <div class="divider"></div>
    """
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="render_news_card", operation="RENDER_CARD")
        return ""


def get_news_css() -> str:
    """Get custom CSS for newsroom styling - matches Figma exactly."""
    return """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&family=Montserrat:wght@400;500;600;700&display=swap');

    /* Keyword highlight — brand red theme */
    mark {
        background: rgba(214, 46, 47, 0.15);
        color: #D62E2F;
        padding: 2px 4px;
        border-radius: 2px;
        font-weight: 600;
    }

    .news-container {
        max-width: 1238px;
        margin: 0 auto;
        padding: 16px 0;
    }

    .news-card {
        padding: 16px 0;
        font-family: 'Roboto', sans-serif;
        position: relative; /* Anchor for stretched link */
        transition: background-color 0.2s ease;
        content-visibility: auto;
        contain-intrinsic-size: 0 180px;
    }
    .news-card:hover {
        background-color: #FAFAFA; /* Subtle hover effect */
        border-radius: 8px;
    }

    .news-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        margin-bottom: 12px;
        position: relative;
        z-index: 2; /* Keep above stretched link */
    }

    .news-source {
        font-family: 'Roboto', sans-serif;
        font-weight: 700;
        font-size: 18px;
        line-height: 21px;
        color: #888888;
    }

    .news-date {
        font-family: 'Roboto', sans-serif;
        font-weight: 700;
        font-size: 16px;
        line-height: 22px;
        color: #888888;
        text-align: right;
    }

    .relative-time {
        color: #888888;
        font-weight: 700;
    }

    .news-title-link {
        font-family: 'Montserrat', sans-serif;
        font-weight: 700;
        font-size: 20px;
        line-height: 24px;
        letter-spacing: -0.28px;
        color: #000000 !important;
        text-decoration: none !important;
        margin-bottom: 12px;
        display: block;
    }

    /* Stretched link so entire card is clickable */
    .news-title-link::after {
        content: "";
        position: absolute;
        inset: 0;
        z-index: 1;
    }

    /* Ensure title link is black and not underlined */
    .news-title-link,
    .news-title-link:hover,
    .news-title-link:active,
    .news-title-link:visited {
        color: #000000 !important;
        text-decoration: none !important;
    }

    .news-summary {
        font-family: 'Roboto', sans-serif;
        font-weight: 400;
        font-size: 18px;
        line-height: 21px;
        color: #4F4F4F;
        margin-bottom: 12px;
        padding-left: 24px;
        position: relative;
        z-index: 2; /* Keep above stretched link */
    }

    .tagged-companies {
        font-family: 'Roboto', sans-serif;
        font-weight: 700;
        font-size: 18px;
        line-height: 21px;
        color: #4F4F4F;
        padding-left: 24px;
        position: relative;
        z-index: 2; /* Keep tags completely above the stretched link */
    }

    .tagged-label {
        font-weight: 700;
        color: #4F4F4F;
    }

    .company-link {
        color: #d62e2f !important;
        text-decoration: none;
        cursor: pointer;
        position: relative;
        z-index: 3; /* Highest z-index */
    }

    .company-link:hover {
        color: #d62e2f !important;
        text-decoration: underline;
    }

    .company-tag-inactive {
        color: #4F4F4F;
        cursor: default;
        position: relative;
        z-index: 3;
    }

    /* Override Streamlit's default link colors */
    a.company-link,
    a.company-link:visited,
    a.company-link:hover,
    a.company-link:active {
        color: #d62e2f !important;
    }

    .divider {
        height: 1px;
        background: #CBCACA;
        margin: 8px 0;
        width: 100%;
    }

    /* Filter section styles */
    .filter-container {
        background: #f8f9fa;
        padding: 20px;
        border-radius: 8px;
        margin-bottom: 24px;
    }

    .filter-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 600;
        font-size: 18px;
        color: #323232;
        margin-bottom: 16px;
    }

    /* ===== NEWS SEARCH SIDEBAR ===== */
    .news-search-sidebar {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        padding: 16px;
        height: calc(100vh - 340px);
        min-height: 480px;
        overflow-y: auto;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05);
    }

    .news-search-sidebar::-webkit-scrollbar {
        width: 6px;
    }
    .news-search-sidebar::-webkit-scrollbar-track {
        background: #F2F2F2;
        border-radius: 3px;
    }
    .news-search-sidebar::-webkit-scrollbar-thumb {
        background: #CBCACA;
        border-radius: 3px;
    }

    .news-search-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid #F2F2F2;
    }

    .news-search-title {
        font-family: 'Montserrat', sans-serif;
        font-weight: 600;
        font-size: 16px;
        color: #2D2A29;
    }

    .news-search-count {
        font-family: 'Roboto', sans-serif;
        font-size: 12px;
        color: #888888;
        margin: 4px 0 12px 4px;
    }

    .news-search-result-card {
        background: #FFFFFF;
        border: 1px solid #E5E5E5;
        border-radius: 12px;
        padding: 12px;
        margin-bottom: 10px;
        cursor: pointer;
        transition: all 0.2s ease;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06), 0 1px 3px rgba(0, 0, 0, 0.04);
        display: block;
        text-decoration: none !important;
        color: inherit !important;
    }
    .news-search-result-card:hover {
        border-color: #CBCACA;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.10), 0 2px 6px rgba(0, 0, 0, 0.06);
    }
    .news-search-result-title {
        font-family: 'Roboto', sans-serif;
        font-weight: 500;
        font-size: 13px;
        color: #2D2A29;
        margin-bottom: 4px;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .news-search-result-snippet {
        font-family: 'Roboto', sans-serif;
        font-size: 12px;
        color: #6B6B6B;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .news-search-result-meta {
        display: flex;
        align-items: center;
        gap: 6px;
        font-family: 'Roboto', sans-serif;
        font-size: 11px;
        color: #888888;
        margin-top: 6px;
    }
    .news-search-result-meta-dot {
        width: 3px;
        height: 3px;
        background: #888888;
        border-radius: 50%;
    }
    .news-search-result-title a {
        color: inherit;
        text-decoration: none;
    }
    .news-search-result-title a:hover {
        color: #D62E2F;
    }
    .news-search-placeholder {
        text-align: center;
        color: #888;
        padding: 40px 0;
        font-family: 'Roboto', sans-serif;
        font-size: 14px;
    }

    /* =======================================================================
       SCROLLBARS — Left search panel (Streamlit container)
       ======================================================================= */
    [data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]::-webkit-scrollbar,
    [data-testid="stVerticalBlockBorderWrapper"] div::-webkit-scrollbar {
        width: 6px;
    }
    [data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]::-webkit-scrollbar-track,
    [data-testid="stVerticalBlockBorderWrapper"] div::-webkit-scrollbar-track {
        background: #F2F2F2;
        border-radius: 3px;
    }
    [data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"]::-webkit-scrollbar-thumb,
    [data-testid="stVerticalBlockBorderWrapper"] div::-webkit-scrollbar-thumb {
        background: #CBCACA;
        border-radius: 3px;
    }

    [data-testid="stVerticalBlockBorderWrapper"] {
        border: 1px solid #E5E5E5 !important;
        border-radius: 12px !important;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07), 0 1px 3px rgba(0, 0, 0, 0.05) !important;
    }


    </style>
    """


# =============================================================================
# CHUNKED CARD RENDERER
# =============================================================================
def _render_cards_chunked(articles: List[NewsArticle], company_map: dict,
                          keyword: Optional[str], chunk_size: int = 50) -> int:
    """Render news cards in chunks via separate st.markdown calls."""
    try:
        total = len(articles)
        for start in range(0, total, chunk_size):
            chunk = articles[start:start + chunk_size]
            html_parts = []
            for idx_in_chunk, article in enumerate(chunk):
                global_idx = start + idx_in_chunk
                html_parts.append(
                    render_news_card(article, company_map, keyword=keyword, idx=global_idx)
                )
            st.markdown("".join(html_parts), unsafe_allow_html=True)
        return total
    except Exception as exc:
        log_structured_error(exc, page="newsroom", component="_render_cards_chunked", operation="RENDER_CHUNKS")
        return 0


# =============================================================================
# NEWSROOM WATCHLIST FILTERING HELPERS
# =============================================================================
# Articles carry tickers via ticker_sentiment; company names are not reliably
# present per-article. Filtering uses exact + base ticker variants (ADS.DE→ADS).
# This means a watchlist entry for "JD (JD.com Inc.)" will also match "JD (Dillard's)"
# since per-article name disambiguation is not possible. Accepted limitation.

def _normalize_news_ticker(raw: str) -> str:
    return (raw or "").strip().upper()


def _news_ticker_variants(raw: str) -> set:
    """Return exact uppercase ticker and base ticker before the first '.'."""
    t = _normalize_news_ticker(raw)
    if not t:
        return set()
    variants = {t}
    if "." in t:
        variants.add(t.split(".")[0])
    return variants


def _watchlist_rows_to_news_scope(rows: List[Dict]) -> Dict:
    """Build ticker variant set from watchlist company rows for news filtering."""
    ticker_variants: set = set()
    for row in rows:
        t_raw = (row.get("ticker") or "").strip()
        if t_raw:
            ticker_variants.update(_news_ticker_variants(t_raw))
    return {"ticker_variants": ticker_variants}


def _article_matches_watchlist(article: NewsArticle, scope: Dict) -> bool:
    tv = scope.get("ticker_variants")
    if not tv:
        return False
    for ts in (article.ticker_sentiment or []):
        if _news_ticker_variants(ts.ticker) & tv:
            return True
    return False


def _filter_articles_by_watchlist(
    articles: List[NewsArticle], watchlist_rows: List[Dict]
) -> List[NewsArticle]:
    if not watchlist_rows:
        return []
    scope = _watchlist_rows_to_news_scope(watchlist_rows)
    if not scope.get("ticker_variants"):
        return []
    return [a for a in articles if _article_matches_watchlist(a, scope)]


# =============================================================================
# RENDER PAGE
# =============================================================================
def render_page():
    """Render newsroom content."""
    tracker = PageLoadTracker("newsroom")

    # ── Page title ───────────────────────────────────────────────────────────
    with tracker.step("PAGE_TITLE"):
        st.markdown("""
        <div style="margin: 24px 0; animation: nwsPageEntry .3s ease-out;">
            <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 24px; color: #d62e2f; letter-spacing: 1px;">CORESIGHT MARKET DATA</div>
            <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 28px; color: #323232;">News Results</div>
        </div>
        <style>@keyframes nwsPageEntry{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}</style>
        """, unsafe_allow_html=True)

    # ── Restore category from localStorage into URL query param ───────────────
    with tracker.step("LOCALSTORAGE_RESTORE"):
        if 'category' not in st.query_params:
            st.markdown("""<script>(function(){
                var c=localStorage.getItem('newsroom_category');
                if(c && c!=='All'){
                    var u=new URL(window.parent.location.href);
                    if(!u.searchParams.has('category')){
                        u.searchParams.set('category',c);
                        window.parent.history.replaceState({},'',u);
                    }
                }
            })();</script>""", unsafe_allow_html=True)

    # ── URL ticker / cross-page state ────────────────────────────────────────
    url_ticker = st.query_params.get("ticker", None)
    _prev_url_ticker = st.session_state.get('_newsroom_url_ticker')
    if _prev_url_ticker and not url_ticker:
        st.session_state.pop('news_company_select', None)
    st.session_state['_newsroom_url_ticker'] = url_ticker

    # ── Session state initialisation (stale keys cleanup) ──────────────────
    for _stale_key in ('news_full_search', 'news_full_search_input', '_news_fullsearch_kw_too_short'):
        st.session_state.pop(_stale_key, None)

    if "news_active_watchlist_id" not in st.session_state:
        st.session_state.news_active_watchlist_id = None
    if "news_active_watchlist_name" not in st.session_state:
        st.session_state.news_active_watchlist_name = ""

    _PAGE_SIZE = 1000

    # ── PREFETCH: date_range + progressive week-chunked AV/YF ─────────
    # First load: fire date_range, determine actual dates, then chunked fetch.
    # Re-visits: if filter key matches session_state, skip DB entirely.
    _first_load = 'date_from' not in st.session_state

    tracker.step_start("PREFETCH_SETUP")

    _date_range_future  = None
    _prefetch_exec      = None
    _chunked_future     = None   # Future[Tuple[av, yf]] for chunked fetch

    if _first_load:
        _prefetch_exec = ThreadPoolExecutor(max_workers=2)
        _date_range_future = _prefetch_exec.submit(
            NewsRepository.get_news_date_range,
        )
        st.session_state.date_from = date.today() - timedelta(days=7)
        st.session_state.date_to   = date.today()
        st.session_state.setdefault('news_sort_order', "Latest")
        _cache_hit = False
    else:
        if 'news_sort_order' not in st.session_state:
            st.session_state.news_sort_order = "Latest"

    _pf_date_from   = st.session_state.date_from
    _pf_date_to     = st.session_state.date_to
    _pf_q_company   = None  # Company filter disabled

    # Prefetch key is date-only — sector/sort are client-side operations
    _pf_filter_key = f"{_pf_date_from}|{_pf_date_to}"

    if not _first_load:
        _existing_articles = st.session_state.get('_news_loaded_articles', [])
        _existing_key      = st.session_state.get('_news_data_key')
        _cache_hit         = bool(_existing_articles and _existing_key == _pf_filter_key)

    if not _cache_hit and not _first_load:
        # Returning visit with changed filters → fire date_range + chunked fetch
        _prefetch_exec = ThreadPoolExecutor(max_workers=2)
        _date_range_future = _prefetch_exec.submit(
            NewsRepository.get_news_date_range,
        )
        # Start chunked fetch immediately (chunks hit @st.cache_data per-week)
        _chunked_future = _prefetch_exec.submit(
            _fetch_chunked,
            _pf_date_from, _pf_date_to,
            None, None, False,  # Always fetch unfiltered for cache stability
        )
    tracker.step_end("PREFETCH_SETUP")

    # ── Consume date_range future ─────────────────────────────────────────
    with tracker.step("DATE_RANGE_FETCH"):
        if _date_range_future is not None:
            date_bounds = _date_range_future.result(timeout=30)
        else:
            date_bounds = NewsRepository.get_news_date_range()
    news_min_date = date_bounds['min_date']
    news_max_date = date_bounds['max_date']

    # On first load: set real dates and fire chunked fetch
    if _first_load:
        st.session_state.date_from = max(news_min_date, news_max_date - timedelta(days=7))
        st.session_state.date_to   = news_max_date
        _pf_date_from = st.session_state.date_from
        _pf_date_to   = st.session_state.date_to
        _pf_filter_key = f"{_pf_date_from}|{_pf_date_to}"
        # Fire week-chunked fetch with correct dates (date_range done)
        _chunked_future = _prefetch_exec.submit(
            _fetch_chunked,
            _pf_date_from, _pf_date_to,
            None, None, False,  # Always fetch unfiltered for cache stability
        )
    # ── CSS inject ───────────────────────────────────────────────────────────
    with tracker.step("CSS_INJECT"):
        st.markdown(get_news_css(), unsafe_allow_html=True)

    # =========================================================================
    # FILTER ROW
    # =========================================================================
    tracker.step_start("FILTER_WIDGETS")

    # ── Watchlist toolbar load ────────────────────────────────────────────────
    _news_user_email = get_current_user() or ""
    try:
        _news_watchlists = get_user_watchlists(_news_user_email) if _news_user_email else []
    except Exception as _nws_wl_exc:
        log_structured_error(_nws_wl_exc, page="newsroom", component="render_page",
                             operation="load_watchlists", context=f"user={_news_user_email}")
        _news_watchlists = []

    _news_no_wl_label = "— No watchlist —"
    _news_wl_options = [_news_no_wl_label] + [
        f"{wl['name']} ({wl.get('company_count', 0)} companies)"
        for wl in _news_watchlists
    ]
    _news_wl_ids: List[Optional[int]] = [None] + [wl["id"] for wl in _news_watchlists]
    _news_wl_names: List[str] = [""] + [wl["name"] for wl in _news_watchlists]

    # Validate stale active watchlist (deleted/hidden since last render)
    _news_cur_wl_id = st.session_state.news_active_watchlist_id
    if _news_cur_wl_id is not None and _news_cur_wl_id not in _news_wl_ids:
        st.session_state.news_active_watchlist_id = None
        st.session_state.news_active_watchlist_name = ""
        st.session_state.pop("news_watchlist_filter", None)
        _news_cur_wl_id = None

    _news_wl_default_idx = _news_wl_ids.index(_news_cur_wl_id) if _news_cur_wl_id in _news_wl_ids else 0

    def _on_news_watchlist_change():
        try:
            _sel = st.session_state.get("news_watchlist_filter", "")
            if not _sel or _sel == _news_no_wl_label or _sel not in _news_wl_options:
                st.session_state.news_active_watchlist_id = None
                st.session_state.news_active_watchlist_name = ""
            else:
                _idx = _news_wl_options.index(_sel)
                st.session_state.news_active_watchlist_id = _news_wl_ids[_idx]
                st.session_state.news_active_watchlist_name = _news_wl_names[_idx]
        except Exception as _wl_cb_exc:
            log_structured_error(_wl_cb_exc, page="newsroom", component="_on_news_watchlist_change",
                                 operation="watchlist_change_callback",
                                 context=f"label={st.session_state.get('news_watchlist_filter', '')}")

    search_col, col_from, col_to, col_sort, col_sector, col_category, col_watchlist = st.columns(
        [2.3, 1, 1, 0.9, 1.8, 2.0, 1.8], gap="small"
    )

    with search_col:
        # Hide the form submit button — Enter key already submits the form
        st.markdown(
            '<style>[data-testid="stFormSubmitButton"] {display:none !important;}</style>',
            unsafe_allow_html=True,
        )
        with st.form("newsroom_search_form", clear_on_submit=False, border=False):
            search_term = st.text_input(
                "Search",
                placeholder="eg., inflation, earnings, retail... (press Enter)",
                value=st.session_state.get('news_search', ''),
                key="news_search_input",
            )
            _search_submitted = st.form_submit_button(
                "Search", use_container_width=True, type="primary",
            )
        if _search_submitted:
            st.session_state.news_search = search_term
        search_term = st.session_state.get('news_search', '')

    with col_from:
        date_from = st.date_input(
            "From",
            value=st.session_state.date_from,
            min_value=news_min_date,
            max_value=news_max_date,
        )
        st.session_state.date_from = date_from

    with col_to:
        date_to = st.date_input(
            "To",
            value=st.session_state.date_to,
            min_value=news_min_date,
            max_value=news_max_date,
        )
        st.session_state.date_to = date_to

    with col_sort:
        sort_order = st.selectbox(
            "Sort",
            options=["Latest", "Earliest"],
            index=0 if st.session_state.news_sort_order == "Latest" else 1,
            key="news_sort_select",
        )
        st.session_state.news_sort_order = sort_order
    sort_ascending = sort_order == "Earliest"

    _LOADING_SPINNER = (
        '<div style="display:flex;align-items:center;gap:14px;padding:24px 28px;'
        'background:#fafafa;border:1px solid #f0f0f0;border-radius:10px;'
        'box-shadow:0 1px 4px rgba(0,0,0,0.04);animation:nwsPageEntry .3s ease-out">'
        '<div style="width:22px;height:22px;border:2.5px solid #eee;'
        'border-top-color:#D62E2F;border-radius:50%;'
        'animation:nws-spin .8s linear infinite;flex-shrink:0"></div>'
        '<span style="color:#444;font-size:14px">Loading news articles\u2026</span>'
        '</div>'
        '<style>@keyframes nws-spin{to{transform:rotate(360deg)}}</style>'
    )
    _article_loading_hint = st.empty()
    if _chunked_future is not None:
        _article_loading_hint.markdown(_LOADING_SPINNER, unsafe_allow_html=True)

    # Sector dropdown
    sectors = ['All'] + NewsRepository.get_sectors()

    default_sector_idx = 0
    if st.session_state.get('news_selected_sector') and \
            st.session_state['news_selected_sector'] in sectors:
        default_sector_idx = sectors.index(st.session_state['news_selected_sector'])

    with col_sector:
        selected_sector = st.selectbox(
            "Sector",
            options=sectors,
            index=default_sector_idx,
            key="news_sector_select",
        )
    st.session_state.news_selected_sector = selected_sector

    # ── Category (topic) dropdown — client-side filter on cached articles ────
    query_sector = None if selected_sector == 'All' else selected_sector

    # Merge any YF-only topic keys (e.g. "layoffs") into NEWS_TOPIC_LABELS so they
    # appear in the dropdown.  get_yf_topic_keys() is cached (6h) so this is ~0ms
    # on subsequent reruns.  merge_yf_topics() is a no-op for keys already present.
    try:
        from utils.constants import merge_yf_topics
        merge_yf_topics(NewsRepository.get_yf_topic_keys())
    except Exception as _myt_exc:
        log_structured_error(_myt_exc, page="newsroom", component="category_dropdown", operation="MERGE_YF_TOPICS")

    _topic_keys  = ['All'] + sorted(NEWS_TOPIC_LABELS.keys())
    _topic_labels = {'All': 'All Categories'}
    _topic_labels.update({k: v for k, v in NEWS_TOPIC_LABELS.items()})

    # Restore category from URL query param (set by localStorage JS on load)
    _url_category = st.query_params.get('category', None)
    default_category_idx = 0
    _prev_cat = _url_category or st.session_state.get('news_selected_category', 'All')
    if _prev_cat in _topic_keys:
        default_category_idx = _topic_keys.index(_prev_cat)

    with col_category:
        selected_category = st.selectbox(
            "Category",
            options=_topic_keys,
            format_func=lambda k: _topic_labels.get(k, k),
            index=default_category_idx,
            key="news_category_select",
        )
    st.session_state.news_selected_category = selected_category
    query_category = None if selected_category == 'All' else selected_category

    # ── Persist category to localStorage (survives across browser sessions) ──
    st.markdown(
        f"<script>localStorage.setItem('newsroom_category','{selected_category}');</script>",
        unsafe_allow_html=True,
    )

    with col_watchlist:
        if _news_watchlists:
            st.selectbox(
                "Watchlist",
                options=_news_wl_options,
                index=_news_wl_default_idx,
                key="news_watchlist_filter",
                on_change=_on_news_watchlist_change,
            )
        else:
            st.selectbox(
                "Watchlist",
                options=[_news_no_wl_label],
                index=0,
                key="news_watchlist_filter",
                disabled=True,
                help="No watchlists found. Create one on the Screening page.",
            )

    # ── Company filter (commented out – kept for future use) ─────────────────
    # companies = [{'ticker': 'All', 'name': 'All Companies'}] + NewsRepository.get_companies()
    # company_tickers = [c['ticker'] for c in companies]
    #
    # if 'news_company_select' not in st.session_state:
    #     if url_ticker and url_ticker in company_tickers:
    #         st.session_state['news_company_select'] = url_ticker
    #     else:
    #         st.session_state['news_company_select'] = 'All'
    # elif st.session_state.get('news_company_select') not in company_tickers:
    #     st.session_state['news_company_select'] = 'All'
    #
    # company_display_map = {}
    # for c in companies:
    #     if c['ticker'] == 'All':
    #         company_display_map['All'] = 'All Companies'
    #     else:
    #         company_display_map[c['ticker']] = f"{c['name']} ({c['ticker']})"
    #
    # with col_company:
    #     selected_company = st.selectbox(
    #         "Company",
    #         options=company_tickers,
    #         format_func=lambda t: company_display_map.get(t, t),
    #         key="news_company_select",
    #     )
    #
    # if selected_company and selected_company != 'All':
    #     st.session_state.active_ticker = selected_company
    # elif selected_company == 'All':
    #     st.session_state.pop('active_ticker', None)

    query_company  = None  # Company filter disabled; pass None to DB queries
    active_keyword = search_term.strip() if search_term and search_term.strip() else None
    tracker.step_end("FILTER_WIDGETS")

    # ── Two-level filter detection ───────────────────────────────────────────
    # _data_key: only date range — controls DB fetching
    # _view_key: includes sort/sector — controls client-side re-filter
    _data_key   = f"{date_from}|{date_to}"
    _view_key   = f"{date_from}|{date_to}|{query_sector}|{sort_ascending}"
    _data_changed = st.session_state.get('_news_data_key') != _data_key
    _view_changed = st.session_state.get('_news_view_key') != _view_key

    if _data_changed:
        # Date range changed — need fresh DB fetch
        st.session_state['_news_data_key']          = _data_key
        st.session_state['_news_view_key']          = _view_key
        st.session_state['_news_loaded_articles']   = []
        st.session_state['_news_offset']            = 0
        st.session_state['_news_has_more']          = False
        st.session_state['_news_loading_more']      = False
        st.session_state['_yf_raw']                 = []
        st.session_state['_yf_loaded']              = False
        st.session_state['_av_raw']                 = []
    elif _view_changed:
        # Sort/sector changed — re-filter from raw data, no DB fetch needed
        st.session_state['_news_view_key']          = _view_key

    new_av_articles: List[NewsArticle] = []
    new_yf_articles: List[NewsArticle] = []

    _need_load = len(st.session_state['_news_loaded_articles']) == 0
    _need_rematch = (_view_changed and not _data_changed
                     and len(st.session_state.get('_av_raw', [])) + len(st.session_state.get('_yf_raw', [])) > 0)

    # =========================================================================
    # INITIAL DATA LOAD — Week-Chunked Progressive Fetch
    # =========================================================================
    if _need_load:
        tracker.step_start("DATA_LOAD", "cache_miss")
        _article_loading_hint.markdown(_LOADING_SPINNER, unsafe_allow_html=True)

        # Check if prefetch future matches current date range
        _pf_key_matches = (
            _chunked_future is not None
            and date_from == _pf_date_from and date_to == _pf_date_to
        )

        try:

            if _pf_key_matches:
                # Use the already-running chunked future
                new_av_articles, new_yf_articles = _chunked_future.result(timeout=180)
            else:
                # Filters changed after prefetch — cancel and do fresh chunked fetch
                if _chunked_future is not None:
                    _chunked_future.cancel()
                new_av_articles, new_yf_articles = _fetch_chunked(
                    date_from, date_to,
                    None, None, False,  # Always fetch unfiltered for cache stability
                )

            # Store unfiltered raw data for client-side re-filtering
            st.session_state['_av_raw'] = new_av_articles
            st.session_state['_yf_raw'] = new_yf_articles
            st.session_state['_yf_loaded'] = True

            # Sector filter (client-side for both AV and YF)
            if query_sector:
                new_av_articles = _filter_av_by_sector(new_av_articles, query_sector)
                new_yf_articles = _filter_yf_by_sector(new_yf_articles, query_sector)

            merged = _union_and_sort_articles(new_av_articles, new_yf_articles, sort_ascending)

            st.session_state['_news_loaded_articles'] = merged
            # No more pagination offset — chunked fetch gets everything for the date range
            st.session_state['_news_has_more'] = False

            # ── Background prefetch: pre-populate @st.cache_data for extra weeks ──
            _spawn_background_prefetch(
                date_from, date_to,
                None, None, False,  # Always prefetch unfiltered for cache stability
            )

        except Exception as e:
            log_structured_error(e, page="newsroom", component="render_page", operation="FETCH_NEWS")
            st.error(f"Error fetching news: {e}")

        _article_loading_hint.empty()
        tracker.step_end("DATA_LOAD")

    else:
        if _chunked_future is not None:
            _chunked_future.cancel()

    # ── Client-side re-merge when only view key (sort/sector) changed ─────────
    if _need_rematch:
        raw_av = st.session_state.get('_av_raw', [])
        raw_yf = st.session_state.get('_yf_raw', [])

        # Apply sector filter client-side
        if query_sector:
            raw_av = _filter_av_by_sector(raw_av, query_sector)
            raw_yf = _filter_yf_by_sector(raw_yf, query_sector)

        merged = _union_and_sort_articles(raw_av, raw_yf, sort_ascending)
        st.session_state['_news_loaded_articles'] = merged

    if _prefetch_exec is not None:
        _prefetch_exec.shutdown(wait=False)

    articles = st.session_state.get('_news_loaded_articles', [])

    # =========================================================================
    # LOCAL WATCHLIST FILTER — applied before keyword/category
    # =========================================================================
    _news_active_wl_id = st.session_state.news_active_watchlist_id
    if _news_active_wl_id is not None:
        try:
            _news_wl_company_rows = get_watchlist_companies(_news_active_wl_id)
        except Exception as _nws_wl_filter_exc:
            log_structured_error(_nws_wl_filter_exc, page="newsroom", component="render_page",
                                 operation="get_watchlist_companies",
                                 context=f"watchlist_id={_news_active_wl_id}")
            _news_wl_company_rows = []
        articles = _filter_articles_by_watchlist(articles, _news_wl_company_rows)
        _news_wl_active_name = st.session_state.news_active_watchlist_name
        st.caption(
            f"Filtering by watchlist: **{_news_wl_active_name}** — "
            f"{len(_news_wl_company_rows)} {'company' if len(_news_wl_company_rows) == 1 else 'companies'}, "
            f"{len(articles):,} matching {'article' if len(articles) == 1 else 'articles'} "
            f"in the selected date range."
        )

    # =========================================================================
    # LOCAL KEYWORD FILTER
    # =========================================================================
    if active_keyword:
        articles = _filter_articles_by_keyword(articles, active_keyword)

    # =========================================================================
    # LOCAL CATEGORY (TOPIC) FILTER — zero DB calls, filters cached articles
    # =========================================================================
    if query_category:
        articles = _filter_articles_by_topic(articles, query_category)

    # ── Company name map ──────────────────────────────────────────────────────
    with tracker.step("COMPANY_NAME_MAP"):
        company_map = get_company_name_map()

    # =========================================================================
    # TWO-COLUMN LAYOUT
    # =========================================================================
    tracker.step_start("RENDER_LAYOUT")
    left_col, right_col = st.columns([0.25, 0.75])

    # ── JavaScript Scroll Handler ────────────────────────────────────────────
    # Handles clicking on left panel search results and scrolling to right panel articles
    import streamlit.components.v1 as _stc
    _stc.html("""<script>(function(){
        var pd = window.parent.document;
        var pw = pd.defaultView || window.parent;
        if(pd._nwsScrollSetup) return;
        pd._nwsScrollSetup = true;
        console.log('[Newsroom] Scroll handler initialized');

        function doScroll(el, targetId) {
            var sp = el.parentElement;
            while(sp){
                if(sp.scrollHeight > sp.clientHeight + 10){
                    var st = pw.getComputedStyle(sp);
                    var ov = st.overflow + st.overflowY;
                    if(ov.indexOf('auto') !== -1 || ov.indexOf('scroll') !== -1){
                        break;
                    }
                }
                sp = sp.parentElement;
            }

            if(sp){
                /* Use getBoundingClientRect for reliable scroll positioning.
                   offsetTop fails when articles span multiple st.markdown chunks
                   because each chunk has its own offsetParent container. */
                var elRect = el.getBoundingClientRect();
                var spRect = sp.getBoundingClientRect();
                var scrollTarget = sp.scrollTop + (elRect.top - spRect.top) - 20;
                console.log('[Newsroom] Scrolling to:', scrollTarget, 'for', targetId);
                sp.scrollTo({top: scrollTarget, behavior:'smooth'});
            } else {
                console.log('[Newsroom] Using scrollIntoView');
                el.scrollIntoView({behavior:'smooth', block:'start'});
            }

            el.style.transition = 'background-color 0.3s ease';
            el.style.backgroundColor = 'rgba(214,46,47,0.15)';
            setTimeout(function(){ el.style.backgroundColor = ''; }, 2000);
        }

        pd.addEventListener('click', function(e){
            var card = e.target.closest('[data-nws-to]');
            if(!card) return;
            e.preventDefault();
            e.stopPropagation();
            var targetId = card.getAttribute('data-nws-to');
            console.log('[Newsroom] Card clicked, targetId:', targetId);

            var el = pd.getElementById(targetId);
            if(!el) {
                console.warn('[Newsroom] Target not found immediately:', targetId);
                setTimeout(function(){
                    var retryEl = pd.getElementById(targetId);
                    if(retryEl) {
                        console.log('[Newsroom] Found on retry');
                        doScroll(retryEl, targetId);
                    } else {
                        console.error('[Newsroom] Target not found after retry:', targetId);
                    }
                }, 500);
                return;
            }
            doScroll(el, targetId);
        }, true);
    })();</script>""", height=0)

    # ── LEFT COLUMN: Search Results Panel ─────────────────────────────────────
    with left_col:
        search_icon = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#D62E2F" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>'
        with st.container(border=True, height=900):
            st.markdown(
                f'<div class="news-search-header">{search_icon}'
                f'<span class="news-search-title">Search News</span></div>',
                unsafe_allow_html=True,
            )

            if active_keyword:
                if articles:
                    _total_loaded = len(articles)
                    _all_loaded = len(st.session_state.get('_news_loaded_articles', []))
                    st.markdown(
                        f'<div class="news-search-count">{_total_loaded} article{"s" if _total_loaded != 1 else ""}'
                        f' matching "<b>{active_keyword}</b>"'
                        f' <span style="color:#999;font-size:12px">(in {_all_loaded} most recent)</span></div>',
                        unsafe_allow_html=True,
                    )
                    _LEFT_RENDER_LIMIT = 2000000
                    _left_articles = articles[:_LEFT_RENDER_LIMIT]

                    _left_html_parts = []
                    for idx, article in enumerate(_left_articles):
                        title_short   = (article.title[:80] + '...') if len(article.title) > 80 else article.title
                        summary_short = (article.summary[:100] + '...') if article.summary and len(article.summary) > 100 else (article.summary or '')
                        title_short   = _highlight_keyword(title_short,   active_keyword)
                        summary_short = _highlight_keyword(summary_short, active_keyword)
                        source   = article.source_domain or article.source or ''
                        pub_date = article.time_published.strftime('%b %d, %Y') if article.time_published else ''
                        _article_id = f'article-{idx}'
                        _left_html_parts.append(
                            f'<div class="news-search-result-card" data-nws-to="{_article_id}">'
                            f'<div class="news-search-result-title">'
                            f'<a href="javascript:void(0)" onclick="return false;">{title_short}</a>'
                            f'</div>'
                            f'<div class="news-search-result-snippet">{summary_short}</div>'
                            f'<div class="news-search-result-meta">'
                            f'<span>{source}</span>'
                            f'<span class="news-search-result-meta-dot"></span>'
                            f'<span>{pub_date}</span>'
                            f'</div></div>'
                        )
                    if len(articles) > _LEFT_RENDER_LIMIT:
                        _left_html_parts.append(
                            f"<div style='text-align:center;color:#888;font-size:11px;padding:8px 0'>"
                            f"+ {len(articles) - _LEFT_RENDER_LIMIT:,} more matches</div>"
                        )
                    st.markdown("".join(_left_html_parts), unsafe_allow_html=True)

                else:
                    _all_loaded = len(st.session_state.get('_news_loaded_articles', []))
                    st.markdown(
                        f'<div class="news-search-placeholder">'
                        f'No matches for "<b>{active_keyword}</b>" in the {_all_loaded} most recent articles.'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.markdown(
                    '<div class="news-search-placeholder">Type a keyword above to search across news headlines and summaries</div>',
                    unsafe_allow_html=True,
                )

    # ── RIGHT COLUMN: News Cards ───────────────────────────────────────────────
    with right_col:
        with st.container(border=False, height=900):
            if articles:
                _total_matched = len(articles)
                _render_articles = articles

                _render_cards_chunked(
                    _render_articles, company_map,
                    keyword=active_keyword, chunk_size=500,
                )
            else:
                if active_keyword and st.session_state.get('_news_loaded_articles'):
                    st.info(
                        f"No matches for **\"{active_keyword}\"** in the "
                        f"{len(st.session_state['_news_loaded_articles'])} most recent articles."
                    )
                else:
                    st.info("No news articles found for the selected filters.")

        # ── Article count indicator ───────────────────────────────────────────
        _loaded_articles = st.session_state.get('_news_loaded_articles', [])

        if _loaded_articles:
            _loaded_count = len(_loaded_articles)
            _display_count = len(articles)
            _days_span = (date_to - date_from).days + 1
            if active_keyword and _display_count != _loaded_count:
                st.markdown(
                    f"<div style='text-align:center;color:#888;font-size:13px;padding:8px 0'>"
                    f"{_display_count:,} matching / {_loaded_count:,} loaded "
                    f"({_days_span} days)</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div style='text-align:center;color:#888;font-size:13px;padding:8px 0'>"
                    f"All {_loaded_count:,} articles loaded "
                    f"({_days_span} days)</div>",
                    unsafe_allow_html=True,
                )

    tracker.step_end("RENDER_LAYOUT")
    tracker.finish()


# =============================================================================
# MAIN
# =============================================================================
def main():
    """Newsroom page entry point."""
    _rid = new_rerun_id("newsroom")

    render_styles()
    st.set_page_config(page_title="Newsroom", layout="wide")
    set_page_layout(
        header_full_width=True,
        footer_full_width=True,
        body_padding="0 20px",
        max_content_width="1440px",
        remove_top_padding=True,
        footer_at_bottom=True,
    )

    initialize_app()

    # Newsroom does NOT need a default ticker — show all categories by default.
    # Only use a ticker if explicitly provided via URL (e.g. cross-page nav).
    _url_ticker     = st.query_params.get("ticker", None)
    _session_ticker = st.session_state.get("active_ticker")
    active_ticker   = _url_ticker or _session_ticker
    # For header nav links we still need a ticker; fall back to "M" only for header
    _header_ticker  = active_ticker or "M"

    render_header(full_width=True, current_page="newsroom", ticker=_header_ticker)

    render_page()

    render_coresight_footer(full_width=True, stick_to_bottom=True)

try:
    main()
except Exception as _exc:
    log_structured_error(_exc, page="newsroom", component="main_call", operation="PAGE_RENDER")
    st.error("An unexpected error occurred. Please refresh the page.")

if __name__ == "__main__":
    pass
