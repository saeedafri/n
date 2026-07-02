"""Central registry of badge callouts for business documentation screenshots.

Marker format: badge_x,badge_y,number,target_x,target_y,label
(target_x/target_y are ignored by the numbers-only annotator; kept for legacy scripts.)
"""
from __future__ import annotations

CALLOUTS_BY_FILE: dict[str, list[str]] = {
    # Login
    "login-01.png": [
        "80,200,1,749,200,Portal title",
        "80,368,2,749,368,Sign in with Coresight",
        "1200,38,4,1339,39,Contact Us",
    ],
    "login-02-sso.png": [
        "120,293,1,749,293,Email",
        "120,336,2,749,336,Password",
        "120,527,4,749,527,Log In",
    ],
    "login-03-footer.png": [
        "80,700,1,400,750,Contact Us footer",
        "80,750,2,500,800,Privacy Policy",
        "80,800,3,600,850,Social icons",
    ],
    # Home
    "home-01.png": [
        "80,78,1,1180,83,Navigation",
        "80,745,2,820,745,View by Company",
        "80,745,3,1820,745,View by Sector",
        "80,792,4,1000,792,Company picker",
    ],
    "home-02-company-selected.png": [
        "80,792,1,1000,650,Company dropdown",
        "80,860,2,980,860,View button",
        "80,792,3,1980,792,Sector dropdown",
        "80,860,4,1980,860,Sector View",
    ],
    "market-data-entry-from-home.png": [
        "80,792,1,1000,650,Company dropdown",
        "80,860,2,980,860,View button",
        "80,792,3,1980,792,Sector dropdown",
        "80,860,4,1980,860,Sector View",
    ],
    "home-03-landing.png": [
        "80,78,1,1180,83,Top navigation",
        "80,200,2,400,220,Company header",
        "80,250,3,300,280,Company Profile tab",
    ],
    "home-04-nav-highlight.png": [
        "80,78,1,1180,83,Top navigation",
        "80,130,2,620,83,Market Data Dashboard",
        "80,182,3,1520,83,Screening",
        "80,234,4,1920,83,News",
        "80,286,5,2760,83,Logout",
    ],
    "home-05-dashboard.png": [
        "80,260,1,331,353,View by Company",
        "80,340,2,668,353,View by Sector",
        "80,420,3,331,343,Company picker",
    ],
    "nav-portal-overview.png": [
        "80,78,1,1180,83,Top navigation",
        "80,130,2,620,83,Market Data",
        "80,182,3,1520,83,Screening",
        "80,234,4,1920,83,News",
    ],
    # Market data
    "market-data-company-profile.png": [
        "80,200,1,255,248,Company Profile tab",
        "80,320,2,512,480,Profile fields",
        "80,440,3,512,680,Chart area",
    ],
    "market-data-income-statement.png": [
        "120,200,1,220,240,Income Statement tab",
        "600,320,2,700,360,Revenue row",
    ],
    "market-data-balance-sheet.png": [
        "120,200,1,220,240,Balance Sheet tab",
        "600,320,2,700,360,Total Assets row",
    ],
    "market-data-cash-flow.png": [
        "120,200,1,200,240,Cash Flow tab",
        "600,320,2,700,360,Operating cash row",
    ],
    "market-data-key-stats.png": [
        "120,200,1,200,240,Key Stats tab",
        "500,300,2,600,340,Revenue metric",
    ],
    "market-data-ratios.png": [
        "120,200,1,180,240,Ratios tab",
        "500,300,2,600,340,Gross Margin row",
    ],
    "market-data-estimates.png": [
        "120,200,1,200,240,Estimates tab",
        "600,300,2,700,350,Analyst estimates",
    ],
    "market-data-forecasting.png": [
        "120,200,1,220,240,Forecasting tab",
        "600,300,2,700,350,Forecast table",
    ],
    "market-data-segment.png": [
        "120,200,1,180,240,Segment tab",
        "500,300,2,600,350,Segment breakdown",
    ],
    # Newsroom
    "newsroom-01.png": [
        "80,300,1,400,345,Search box",
        "80,300,2,780,345,Date range",
        "80,300,3,1500,345,Sort and filters",
        "80,650,4,280,750,Search News panel",
    ],
    "newsroom-02-filters.png": [
        "80,300,1,400,345,Search box",
        "80,300,2,780,345,From / To dates",
        "80,300,3,1300,345,Sector dropdown",
        "80,300,4,1700,345,Category dropdown",
        "80,300,5,2100,345,Watchlist dropdown",
    ],
    "newsroom-03-articles.png": [
        "80,300,1,400,345,Search box",
        "80,650,2,280,750,Search News panel",
        "80,900,3,1200,950,Article headline",
        "80,900,4,1200,1100,Sentiment area",
    ],
    "newsroom-04-sort.png": [
        "80,300,1,400,345,Search box",
        "80,380,2,1130,420,Sort dropdown",
        "80,460,3,1500,345,Sector dropdown",
        "80,650,4,1200,950,Article feed",
    ],
    "newsroom-05-category.png": [
        "80,300,1,400,345,Search box",
        "80,380,2,1720,480,Category dropdown",
        "80,460,3,1300,345,Sector dropdown",
        "80,650,4,1200,950,Article feed",
    ],
    "newsroom-06-watchlist.png": [
        "80,300,1,400,345,Search box",
        "80,380,2,2120,480,Watchlist dropdown",
        "80,460,3,1700,345,Category dropdown",
        "80,650,4,1200,950,Article feed",
    ],
    "newsroom-07-date-picker.png": [
        "80,300,1,780,345,From date",
        "80,500,2,800,720,Calendar picker",
        "80,380,3,1050,345,To date",
        "80,650,4,1200,950,Article feed",
    ],
    "nav-newsroom-layout.png": [
        "80,300,1,400,345,News search",
        "80,650,2,280,750,Results panel",
        "80,900,3,1200,950,Article cards",
    ],
    "nav-newsroom-filters.png": [
        "80,300,1,400,345,Filter bar",
        "80,380,2,1300,345,Sector filter",
        "80,460,3,1700,345,Category filter",
    ],
    # Screening
    "screening-01-default.png": [
        "80,300,1,350,343,Screen For row",
        "80,420,2,700,420,Watchlists dropdown",
        "80,900,3,350,1350,Show Results",
    ],
    "screening-05-watchlists-dropdown.png": [
        "80,300,1,350,343,Screen For row",
        "80,420,2,700,400,Watchlists dropdown",
        "80,420,3,1100,420,Edit / Manage",
    ],
    "screening-04-watchlist.png": [
        "80,200,1,700,263,Watchlist selector",
        "80,350,2,700,400,Create New",
        "80,500,3,700,550,Company search",
        "80,700,4,700,900,Companies table",
    ],
    "screening-watchlist-dialog-new.png": [
        "80,250,1,700,320,Name field",
        "80,400,2,500,520,Search Companies",
        "80,400,3,1200,520,Search Sectors",
        "80,700,4,1800,780,Create Watchlist",
    ],
    "screening-watchlist-dialog-companies.png": [
        "80,250,1,700,320,Name field",
        "80,400,2,500,480,Company dropdown",
        "80,400,3,1200,520,Search Sectors",
        "80,700,4,1800,780,Create Watchlist",
    ],
    "screening-watchlist-dialog-sectors.png": [
        "80,250,1,700,320,Name field",
        "80,400,2,500,520,Search Companies",
        "80,400,3,1200,480,Sector dropdown",
        "80,700,4,1800,780,Create Watchlist",
    ],
    "screening-industry-form.png": [
        "80,400,1,350,430,Industry tab",
        "80,550,2,700,600,Select industries",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-03-industry.png": [
        "80,400,1,350,430,Industry tab",
        "80,550,2,700,600,Select industries",
    ],
    "screening-geographic.png": [
        "80,400,1,700,430,Geographic tab",
        "80,550,2,700,600,Select countries",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-geographic-countries.png": [
        "80,400,1,700,430,Geographic tab",
        "80,550,2,700,650,Country dropdown",
    ],
    "screening-02-financial.png": [
        "80,350,1,700,450,Financial form",
        "80,500,2,700,550,Select Metric",
    ],
    "screening-financial-statement-type.png": [
        "80,200,1,1100,200,Financial tab",
        "80,350,2,700,400,Statement type",
        "80,500,3,700,550,Year dropdown",
    ],
    "screening-financial-statement-dropdown.png": [
        "80,200,1,1100,200,Financial tab",
        "80,350,2,700,500,Cash Flow option",
    ],
    "screening-financial-metric.png": [
        "80,200,1,1100,200,Financial tab",
        "80,350,2,700,500,Select Metric",
        "80,500,3,700,400,Statement type",
    ],
    "screening-financial-period-type.png": [
        "80,300,1,500,400,Period Type",
        "80,450,2,700,450,Statement type",
        "80,450,3,700,550,Metric",
    ],
    "screening-financial-year-range.png": [
        "80,300,1,500,400,Period Type",
        "80,450,2,500,550,From year",
        "80,450,3,900,550,To year",
        "80,700,4,350,900,Add Criteria",
    ],
    "screening-financial-year.png": [
        "80,300,1,500,400,Period Type",
        "80,450,2,500,500,Year dropdown",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-financial-operator.png": [
        "80,300,1,500,400,Operator",
        "80,450,2,500,550,Between option",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-financial-value.png": [
        "80,300,1,500,400,Operator",
        "80,450,2,500,500,Value ($mm)",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-key-devs-mode.png": [
        "80,400,1,2100,484,Key Devs tab",
        "80,550,2,700,650,Select Categories",
    ],
    "screening-key-devs-categories.png": [
        "80,400,1,2100,430,Key Devs tab",
        "80,550,2,700,650,Category dropdown",
        "80,700,3,350,900,Add Criteria",
    ],
    "screening-nav-context.png": [
        "80,78,1,1180,83,Top navigation",
        "80,182,3,1520,83,Screening active",
        "80,300,1,350,343,Screening builder",
    ],
    # Earnings
    "earnings-calls-01.png": [
        "200,200,1,300,240,Ticker selector",
        "500,200,2,600,240,Year quarter picker",
        "400,350,3,500,400,Transcript content",
    ],
    "earnings-calls-02-list.png": [
        "200,200,1,300,240,Company filter",
        "500,200,2,600,240,Year and quarter",
        "400,500,3,500,550,Transcript list",
    ],
    "earnings-calls-03-search.png": [
        "200,250,1,350,300,Keyword search",
        "400,400,2,500,450,Search results",
        "900,350,3,1000,400,Transcript viewer",
    ],
    "earnings-calendar-01.png": [
        "80,170,1,620,155,Company filter",
        "80,250,2,827,145,Month Wise toggle",
        "80,360,3,489,517,Calendar grid",
    ],
    "earnings-calendar-02-events.png": [
        "200,220,1,300,260,Legend chips",
        "500,350,2,600,400,Day event markers",
        "900,300,3,1000,350,Event colors",
    ],
    "earnings-calendar-03-month.png": [
        "200,150,1,350,180,Email Alerts",
        "500,150,2,650,180,Month Wise toggle",
        "400,300,3,500,350,Calendar grid",
    ],
    # Live earnings
    "live-earnings-transcript-01.png": [
        "60,180,1,500,185,Session stats",
        "60,285,2,384,285,Company ticker",
        "1420,600,4,1114,600,Transcript stream",
    ],
    "live-earnings-transcript-02.png": [
        "60,400,1,400,450,Pipeline log",
        "1420,600,2,1114,600,Empty stream",
        "60,180,3,300,200,Session stats",
    ],
    "live-earnings-transcript-03.png": [
        "60,285,1,384,285,Company ticker field",
        "60,350,2,384,350,Year field",
        "60,415,3,384,415,Quarter field",
    ],
    # Company filings
    "company-filings.png": [
        "200,200,1,300,240,Ticker search",
        "500,300,2,600,350,Filing documents table",
        "900,200,3,1000,240,Document type filter",
    ],
    "company-filings-02.png": [
        "80,210,1,900,210,Filing header",
        "80,290,2,1380,175,Download button",
        "80,370,3,850,450,Document viewer",
    ],
    "company-filings-03.png": [
        "200,250,1,350,300,Metric search box",
        "200,400,2,350,450,Metric result cards",
        "700,300,3,900,350,Document body",
    ],
    # Forecasting
    "forecasting-01.png": [
        "200,200,1,350,240,Ticker selector",
        "600,300,2,700,350,Forecast chart",
        "900,200,3,1050,240,Refresh controls",
    ],
    "forecasting-02.png": [
        "400,200,1,500,240,Quarterly period toggle",
        "600,350,2,700,400,Quarterly forecast table",
    ],
    "forecasting-03.png": [
        "400,250,1,550,300,Scenario summary",
        "700,350,2,800,400,Pessimistic band",
        "900,350,3,1000,400,Optimistic band",
    ],
    "forecasting-refresh-dialog.png": [
        "300,200,1,450,250,Refresh dialog title",
        "600,350,2,700,400,Company refresh table",
    ],
    # Admin
    "access-management.png": [
        "60,170,1,135,160,IAM badge",
        "60,250,2,180,340,Users tab",
        "60,330,3,1280,580,Add User",
        "60,410,4,600,920,All Users table",
    ],
    "retailer-adding.png": [
        "120,200,1,350,240,Retailer Manager title",
        "400,350,2,500,400,Editable retailer grid",
        "900,200,3,1000,240,Bulk upload section",
    ],
    "company-filings-add-files.png": [
        "120,200,1,350,240,File Manager title",
        "400,350,2,500,400,Upload form",
        "800,350,3,900,400,Folder browser",
    ],
}
