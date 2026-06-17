"""
Screening Configuration
=======================
Defines available statement types, metrics, operators, and timeframes for the
Coresight Screening feature. This is the single source of truth for what
criteria are supported and how they map to DB tables/columns.

All sec_col values are verified against actual STG DB column names.
All yf_item values are verified against actual DISTINCT line_item values in STG DB.
No DB calls are ever made to populate these lists — they are permanent constants.

DO NOT scatter table names or metric mappings across page or service code.
All additions should go here.
"""

# ---------------------------------------------------------------------------
# Comparison operators and their SQL equivalents
# ---------------------------------------------------------------------------

OPERATORS = [
    "Greater Than",
    "Less Than",
    "Between",
    "Equals",
    "Greater Than or Equal To",
    "Less Than or Equal To",
]

OPERATOR_SQL = {
    "Greater Than":             ">",
    "Less Than":                "<",
    "Equals":                   "=",
    "Greater Than or Equal To": ">=",
    "Less Than or Equal To":    "<=",
    # "Between" is handled separately in query building (val BETWEEN :v1 AND :v2)
}

# ---------------------------------------------------------------------------
# Timeframes supported for financial filtering
# ---------------------------------------------------------------------------

import datetime as _dt
_CURRENT_FY = _dt.date.today().year

# Logger import — error logging only
try:
    from utils.server_logger import log_error, log_exception, log_structured_error
except ImportError:
    def log_error(msg): pass        # type: ignore[misc]
    def log_exception(msg): pass    # type: ignore[misc]
    def log_structured_error(exc, **kwargs): pass  # type: ignore[misc]

# Period types for financial screening — mirrors market_data.py CQ/FQ nomenclature
# FY = Fiscal Year (Annual), CQ = Calendar Quarter, FQ = Fiscal Quarter
# TQ = Trailing Quarters (last N quarterly values shown as separate columns)
PERIOD_TYPES = ["FY", "CQ", "FQ"]

# Quarters available when CQ or FQ is selected
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

# ── Trailing-quarters mode ────────────────────────────────────────────────
# Display the last N quarterly values of a metric as separate columns in one
# view (e.g. last 8 quarters of Inventory / COGS). Display-only — no operator
# or threshold filter is applied; every company keeps its row and missing
# quarters show N/A. Columns are aligned by calendar quarter of period-end
# (e.g. "Q3 2025") so values are comparable across companies.
TRAILING_QUARTERS = "TQ"
TRAILING_QUARTERS_OPTIONS = [4, 8, 12]
TRAILING_QUARTERS_DEFAULT = 8

# Statement types that support trailing-quarters (have quarterly SEC/YF tables).
TRAILING_QUARTERS_STMTS = {"Income Statement", "Balance Sheet", "Cash Flow"}

# Quarter Range = one display-only column per calendar quarter in a chosen
# [from, to] window. Same quarterly tables as TQ; bounded by the analyst's range
# instead of "last N". Reuses the quarter_cols pipeline.
QUARTER_RANGE = "QR"

# Friendly labels for the Period Type selector.
PERIOD_TYPE_LABELS = {
    "FY": "FY (Annual)",
    "CQ": "Calendar Quarter",
    "FQ": "Fiscal Quarter",
    "TQ": "Last N Quarters",
    "QR": "Quarter Range",
}

# Year range for financial screening (historical statements)
SCREENING_YEARS = list(range(_CURRENT_FY, 2014, -1))  # 2026 … 2015

# Forward-looking statements (Estimates / Forecasting) project several years
# ahead — `coreiq_model_forecasts` carries fiscal years out to ~_CURRENT_FY+5.
# Their Year selector must offer those future years, not just historical ones.
_MAX_FORWARD_FY = _CURRENT_FY + 5  # e.g. 2031
FORWARD_SCREENING_YEARS = list(range(_MAX_FORWARD_FY, 2014, -1))  # 2031 … 2015

# Legacy TIMEFRAMES kept for backward compatibility (annual-only callers)
TIMEFRAMES = (
    ["Latest"]                                          # MAX(date) per ticker
    + [f"FY {y}" for y in range(_CURRENT_FY, 2014, -1)]  # FY 2026 … FY 2015
)

# ---------------------------------------------------------------------------
# Numeric scale: DB stores raw dollar values; UI/user works in $mm
# When building SQL, multiply user's $mm value by DB_SCALE before comparing.
# ---------------------------------------------------------------------------

DB_SCALE = 1_000_000  # $1mm = 1,000,000 raw DB units

# ---------------------------------------------------------------------------
# Financial statement → metric configuration
#
# Each entry defines:
#   sec_table        : AV (SEC) financial table name
#   yf_table         : YFinance financial table name
#   sec_date_col     : date column in SEC table
#   sec_period_col   : period-type column in SEC table
#   sec_period_value : value to filter on (e.g. 'annual')
#   yf_date_col      : date column in YF table
#   yf_period_col    : period-type column in YF table
#   yf_period_value  : value to filter on in YF table
#   metrics          : list of metric dicts (see below)
#
# Each metric dict:
#   label    : UI display label
#   sec_col  : column name in SEC table (None → SEC not available for this metric)
#   yf_item  : line_item string in YF table  (None → YF not available)
#   unit     : unit label for the value input (default "$mm")
#
# VERIFIED: all sec_col names checked against DESCRIBE on STG DB tables.
# VERIFIED: all yf_item strings checked against SELECT DISTINCT line_item on STG DB tables.
# ---------------------------------------------------------------------------

STATEMENT_CONFIG = {
    "Income Statement": {
        "sec_table":        "coreiq_av_financials_income_statement",
        "yf_table":         "coreiq_yf_financials_income_statement",
        "sec_date_col":     "fiscal_date_ending",
        "sec_period_col":   "report_type",
        "sec_period_value": "annual",
        "yf_date_col":      "period_end",
        "yf_period_col":    "frequency",
        "yf_period_value":  "annual",
        "metrics": [
            {
                "label":   "Total Revenue",
                "sec_col": "total_revenue",
                "yf_item": "Total Revenue",
                "unit":    "$mm",
            },
            {
                "label":   "Gross Profit",
                "sec_col": "gross_profit",
                "yf_item": "Gross Profit",
                "unit":    "$mm",
            },
            {
                "label":   "Operating Income",
                "sec_col": "operating_income",
                "yf_item": "Operating Income",
                "unit":    "$mm",
            },
            {
                "label":   "EBIT",
                "sec_col": "ebit",
                "yf_item": "EBIT",
                "unit":    "$mm",
            },
            {
                "label":   "EBITDA",
                "sec_col": "ebitda",
                "yf_item": "EBITDA",
                "unit":    "$mm",
            },
            {
                "label":   "Net Income",
                "sec_col": "net_income",
                "yf_item": "Net Income",
                "unit":    "$mm",
            },
            {
                "label":   "Pretax Income",
                "sec_col": "income_before_tax",
                "yf_item": "Pretax Income",
                "unit":    "$mm",
            },
            {
                "label":   "Cost of Revenue",
                "sec_col": "cost_of_revenue",
                "yf_item": "Cost Of Revenue",
                "unit":    "$mm",
            },
            {
                "label":   "R&D Expense",
                "sec_col": "research_and_development",
                "yf_item": "Research And Development",   # FIXED: was "Research Development"
                "unit":    "$mm",
            },
            {
                "label":   "SG&A Expense",
                "sec_col": "selling_general_and_administrative",
                "yf_item": "Selling General And Administration",
                "unit":    "$mm",
            },
            {
                "label":   "Operating Expenses",
                "sec_col": "operating_expenses",
                "yf_item": "Operating Expense",
                "unit":    "$mm",
            },
            {
                "label":   "Depreciation & Amortization",
                "sec_col": "depreciation_and_amortization",
                "yf_item": "Depreciation And Amortization In Income Statement",
                "unit":    "$mm",
            },
            {
                "label":   "Interest Expense",
                "sec_col": "interest_expense",
                "yf_item": "Interest Expense",
                "unit":    "$mm",
            },
        ],
    },

    "Balance Sheet": {
        "sec_table":        "coreiq_av_financials_balance_sheet",
        "yf_table":         "coreiq_yf_financials_balance_sheet",
        "sec_date_col":     "fiscal_date_ending",
        "sec_period_col":   "report_type",
        "sec_period_value": "annual",
        "yf_date_col":      "period_end",
        "yf_period_col":    "frequency",
        "yf_period_value":  "annual",
        "metrics": [
            {
                "label":   "Total Assets",
                "sec_col": "total_assets",
                "yf_item": "Total Assets",
                "unit":    "$mm",
            },
            {
                "label":   "Total Current Assets",
                "sec_col": "total_current_assets",
                "yf_item": "Current Assets",             # FIXED: was "Total Current Assets" (not in YF DB)
                "unit":    "$mm",
            },
            {
                "label":   "Total Non-Current Assets",
                "sec_col": "total_non_current_assets",
                "yf_item": "Total Non Current Assets",
                "unit":    "$mm",
            },
            {
                "label":   "Net PPE",
                "sec_col": "property_plant_equipment",
                "yf_item": "Net PPE",
                "unit":    "$mm",
            },
            {
                "label":   "Goodwill",
                "sec_col": "goodwill",
                "yf_item": "Goodwill",
                "unit":    "$mm",
            },
            {
                "label":   "Inventory",
                "sec_col": "inventory",
                "yf_item": "Inventory",
                "unit":    "$mm",
            },
            {
                "label":   "Cash & Cash Equivalents",
                "sec_col": "cash_and_cash_equivalents_at_carrying_value",
                "yf_item": "Cash And Cash Equivalents",
                "unit":    "$mm",
            },
            {
                "label":   "Total Liabilities",
                "sec_col": "total_liabilities",
                "yf_item": "Total Liabilities Net Minority Interest",
                "unit":    "$mm",
            },
            {
                "label":   "Total Current Liabilities",
                "sec_col": "total_current_liabilities",
                "yf_item": "Current Liabilities",
                "unit":    "$mm",
            },
            {
                "label":   "Long Term Debt",
                "sec_col": "long_term_debt",
                "yf_item": "Long Term Debt",
                "unit":    "$mm",
            },
            {
                "label":   "Short Term Debt",
                "sec_col": "short_term_debt",
                "yf_item": "Current Debt",
                "unit":    "$mm",
            },
            {
                "label":   "Total Debt",
                "sec_col": "short_long_term_debt_total",
                "yf_item": "Total Debt",
                "unit":    "$mm",
            },
            {
                "label":   "Shareholder Equity",
                "sec_col": "total_shareholder_equity",
                "yf_item": "Stockholders Equity",
                "unit":    "$mm",
            },
            {
                "label":   "Retained Earnings",
                "sec_col": "retained_earnings",
                "yf_item": "Retained Earnings",
                "unit":    "$mm",
            },
        ],
    },

    "Cash Flow": {
        "sec_table":        "coreiq_av_financials_cash_flow",
        "yf_table":         "coreiq_yf_financials_cash_flow",
        "sec_date_col":     "fiscal_date_ending",
        "sec_period_col":   "report_type",
        "sec_period_value": "annual",
        "yf_date_col":      "period_end",
        "yf_period_col":    "frequency",
        "yf_period_value":  "annual",
        "metrics": [
            {
                "label":   "Operating Cash Flow",
                "sec_col": "operating_cashflow",
                "yf_item": "Operating Cash Flow",
                "unit":    "$mm",
            },
            {
                "label":   "Free Cash Flow",
                "sec_col": None,                         # SEC table has no pre-computed FCF column
                "yf_item": "Free Cash Flow",
                "unit":    "$mm",
            },
            {
                "label":   "Capital Expenditures",
                "sec_col": "capital_expenditures",
                "yf_item": "Capital Expenditure",
                "unit":    "$mm",
            },
            {
                "label":   "Investing Cash Flow",
                "sec_col": "cashflow_from_investment",
                "yf_item": "Investing Cash Flow",
                "unit":    "$mm",
            },
            {
                "label":   "Financing Cash Flow",
                "sec_col": "cashflow_from_financing",
                "yf_item": "Financing Cash Flow",
                "unit":    "$mm",
            },
            {
                "label":   "Depreciation & Amortization",
                "sec_col": "depreciation_depletion_and_amortization",
                "yf_item": "Depreciation Amortization Depletion",
                "unit":    "$mm",
            },
            {
                "label":   "Stock-Based Compensation",
                "sec_col": None,                         # Not a column in AV cash flow table
                "yf_item": "Stock Based Compensation",
                "unit":    "$mm",
            },
            {
                "label":   "Dividend Payout",
                "sec_col": "dividend_payout",
                "yf_item": "Cash Dividends Paid",
                "unit":    "$mm",
            },
        ],
    },

    # Key Stats — period-based values from KeyStatsRepository (Market Data Key Stats tab).
    # metric_key maps to line_items[].metric_key in repository output.
    "Key Stats": {
        "tabular_market_data": True,
        "metrics": [
            {"label": "Total Revenue",                      "metric_key": "total_revenue",              "unit": "$mm"},
            {"label": "Growth Over Prior Year",             "metric_key": "revenue_growth_yoy",         "unit": "%"},
            {"label": "Gross Profit",                       "metric_key": "gross_profit",               "unit": "$mm"},
            {"label": "Gross Profit Margin %",              "metric_key": "gross_profit_margin",        "unit": "%"},
            {"label": "EBITDA",                             "metric_key": "ebitda",                     "unit": "$mm"},
            {"label": "EBITDA Margin %",                    "metric_key": "ebitda_margin",              "unit": "%"},
            {"label": "EBIT",                               "metric_key": "ebit",                       "unit": "$mm"},
            {"label": "EBIT Margin %",                      "metric_key": "ebit_margin",                "unit": "%"},
            {"label": "Earnings from Cont. Ops.",           "metric_key": "cont_ops_income",            "unit": "$mm"},
            {"label": "Earnings from Cont. Ops. Margin %", "metric_key": "cont_ops_margin",            "unit": "%"},
            {"label": "Net Income",                         "metric_key": "net_income",                 "unit": "$mm"},
            {"label": "Net Income Margin %",              "metric_key": "net_income_margin",          "unit": "%"},
            {"label": "Diluted EPS Excl. Extra Items",      "metric_key": "diluted_eps_excl_extra_items", "unit": "$"},
            {"label": "EPS Growth Over Prior Year",         "metric_key": "eps_growth_yoy",             "unit": "%"},
        ],
    },

    # Business Segments — Segment tab business/product members (coreiq_filing_metrics_v4).
    "Business Segments": {
        "segment_statement": True,
        "segment_type": "business",
        "metrics": [
            {"label": "Revenues",                      "metric_key": "Revenues",                      "unit": "$mm"},
            {"label": "Operating Profit Before Tax",   "metric_key": "Operating Profit Before Tax",   "unit": "$mm"},
            {"label": "Assets",                        "metric_key": "Assets",                        "unit": "$mm"},
            {"label": "Depreciation & Amortization",   "metric_key": "Depreciation & Amortization",   "unit": "$mm"},
            {"label": "Capital Expenditure",           "metric_key": "Capital Expenditure",           "unit": "$mm"},
        ],
    },

    # Geographical Segments — Segment tab geographic members (coreiq_filing_metrics_v4).
    "Geographical Segments": {
        "segment_statement": True,
        "segment_type": "geographical",
        "metrics": [
            {"label": "Revenues",                      "metric_key": "Revenues",                      "unit": "$mm"},
            {"label": "Operating Profit Before Tax",   "metric_key": "Operating Profit Before Tax",   "unit": "$mm"},
            {"label": "Assets",                        "metric_key": "Assets",                        "unit": "$mm"},
            {"label": "Depreciation & Amortization",   "metric_key": "Depreciation & Amortization",   "unit": "$mm"},
            {"label": "Capital Expenditure",           "metric_key": "Capital Expenditure",           "unit": "$mm"},
        ],
    },

    # Ratios — period-based values from RatiosRepository (Market Data Ratios tab).
    "Ratios": {
        "tabular_market_data": True,
        "metrics": [
            {"label": "Current Ratio",                  "metric_key": "current_ratio",           "unit": "x"},
            {"label": "Quick Ratio",                    "metric_key": "quick_ratio",             "unit": "x"},
            {"label": "Cash Ratio",                     "metric_key": "cash_ratio",              "unit": "x"},
            {"label": "Net Debt / EBITDA",              "metric_key": "net_debt_ebitda",         "unit": "x"},
            {"label": "Net Debt / (EBITDA - Capex)",    "metric_key": "net_debt_ebitda_capex",   "unit": "x"},
            {"label": "Debt / Equity",                  "metric_key": "debt_equity",             "unit": "x"},
            {"label": "Debt / Assets",                  "metric_key": "debt_assets",             "unit": "x"},
            {"label": "Equity Ratio",                   "metric_key": "equity_ratio",            "unit": "x"},
            {"label": "Inventory Turnover",             "metric_key": "inventory_turnover",      "unit": "x"},
            {"label": "Receivables Turnover",           "metric_key": "receivables_turnover",    "unit": "x"},
            {"label": "Asset Turnover",                 "metric_key": "asset_turnover",          "unit": "x"},
            {"label": "Gross Margin %",                 "metric_key": "gross_margin_pct",        "unit": "%"},
            {"label": "EBITDA Margin %",                "metric_key": "ebitda_margin_pct",       "unit": "%"},
            {"label": "EBIT Margin %",                  "metric_key": "ebit_margin_pct",         "unit": "%"},
            {"label": "Net Margin %",                   "metric_key": "net_margin_pct",          "unit": "%"},
            {"label": "ROA %",                          "metric_key": "roa_pct",                 "unit": "%"},
            {"label": "ROE %",                          "metric_key": "roe_pct",                 "unit": "%"},
            {"label": "P/E",                            "metric_key": "pe",                      "unit": "x"},
            {"label": "Price / Book",                   "metric_key": "price_book",              "unit": "x"},
            {"label": "Dividend Yield %",               "metric_key": "dividend_yield_pct",      "unit": "%"},
            {"label": "EV / Revenue",                   "metric_key": "ev_revenue",              "unit": "x"},
            {"label": "EV / EBITDA",                    "metric_key": "ev_ebitda",               "unit": "x"},
        ],
    },

    # Estimates — analyst consensus (next fiscal year/quarter horizon).
    # AV: coreiq_av_financials_earnings_estimates (wide). YF: ..._yf_... (long).
    # av_col = column in AV table; yf_type/yf_metric = (estimate_type, metric) in YF table.
    "Estimates": {
        "estimates_based": True,
        "metrics": [
            {"label": "Revenue Estimate (Avg)",   "av_col": "rev_est_avg",            "yf_type": "revenue_estimate",  "yf_metric": "avg",              "unit": "$mm", "scale_mm": True},
            {"label": "Revenue Estimate (High)",  "av_col": "rev_est_high",           "yf_type": "revenue_estimate",  "yf_metric": "high",             "unit": "$mm", "scale_mm": True},
            {"label": "Revenue Estimate (Low)",   "av_col": "rev_est_low",            "yf_type": "revenue_estimate",  "yf_metric": "low",              "unit": "$mm", "scale_mm": True},
            {"label": "EPS Estimate (Avg)",       "av_col": "eps_est_avg",            "yf_type": "earnings_estimate", "yf_metric": "avg",              "unit": "$"},
            {"label": "EPS Estimate (High)",      "av_col": "eps_est_high",           "yf_type": "earnings_estimate", "yf_metric": "high",             "unit": "$"},
            {"label": "EPS Estimate (Low)",       "av_col": "eps_est_low",            "yf_type": "earnings_estimate", "yf_metric": "low",              "unit": "$"},
            {"label": "# Analysts (EPS)",         "av_col": "eps_est_analyst_count",  "yf_type": "earnings_estimate", "yf_metric": "numberOfAnalysts", "unit": "x"},
            {"label": "# Analysts (Revenue)",     "av_col": "rev_est_analyst_count",  "yf_type": "revenue_estimate",  "yf_metric": "numberOfAnalysts", "unit": "x"},
        ],
    },

    # Forecasting — revenue model forecasts (coreiq_model_forecasts, value_millions).
    # model_key selects the model/scenario. Values already in $mm.
    "Forecasting": {
        "forecast_based": True,
        "metrics": [
            {"label": "Revenue Forecast (Ensemble)",    "model_key": "ensemble",             "unit": "$mm"},
            {"label": "Revenue Forecast (Baseline)",    "model_key": "scenario_baseline",    "unit": "$mm"},
            {"label": "Revenue Forecast (Optimistic)",  "model_key": "scenario_optimistic",  "unit": "$mm"},
            {"label": "Revenue Forecast (Pessimistic)", "model_key": "scenario_pessimistic", "unit": "$mm"},
            {"label": "Revenue Forecast (Linear)",      "model_key": "linear",               "unit": "$mm"},
            {"label": "Revenue Forecast (CAGR)",        "model_key": "cagr",                 "unit": "$mm"},
        ],
    },
}

# Statement types without Geo/Biz segment steps (Key Stats / Ratios / Estimates / Forecasting).
OVERVIEW_BASED_STMTS = {"Key Stats", "Ratios", "Estimates", "Forecasting"}
TABULAR_MARKET_DATA_STMTS = {"Key Stats", "Ratios"}
ESTIMATES_STMT = "Estimates"
FORECAST_STMT  = "Forecasting"

# Forward-looking statements whose Year selector should offer future fiscal years.
FORWARD_LOOKING_STMTS = {ESTIMATES_STMT, FORECAST_STMT}

SEGMENT_STATEMENT_TYPES = {"Business Segments", "Geographical Segments"}

# Financial form: hide Additional Data for tabular + segment statement types.
FINANCIAL_NO_ADDITIONAL_DATA_STMTS = TABULAR_MARKET_DATA_STMTS | SEGMENT_STATEMENT_TYPES

# ---------------------------------------------------------------------------
# Helper: get all metric labels for a statement type
# (excludes _derived placeholders that have no queryable DB columns)
# ---------------------------------------------------------------------------

def get_metric_labels(statement: str):
    """Return list of metric label strings for the given statement type."""
    try:
        cfg = STATEMENT_CONFIG.get(statement, {})
        return [m["label"] for m in cfg.get("metrics", [])]
    except Exception as e:
        log_structured_error(
            e,
            page="screening_config",
            component="get_metric_labels",
            operation="get_labels",
            context=f"statement={statement}",
        )
        return []


def get_metric_info(statement: str, label: str):
    """Return the metric dict for a given statement + label, or None."""
    try:
        cfg = STATEMENT_CONFIG.get(statement, {})
        for m in cfg.get("metrics", []):
            if m["label"] == label:
                return m
        return None
    except Exception as e:
        log_structured_error(
            e,
            page="screening_config",
            component="get_metric_info",
            operation="get_info",
            context=f"statement={statement}, label={label}",
        )
        return None


# ---------------------------------------------------------------------------
# News & Developments — topic and timeframe constants
# ---------------------------------------------------------------------------

# All 15 Alpha Vantage NLP topics from coreiq_av_market_news_sentiment.topics_json.
# Labels are human-readable; keys are the exact JSON values stored in the DB.
NEWSDEV_TOPICS = {
    "Earnings":                  "earnings",
    "Financial Markets":         "financial_markets",
    "Finance":                   "finance",
    "Technology":                "technology",
    "Economy — Macro":           "economy_macro",
    "Life Sciences":             "life_sciences",
    "Energy & Transportation":   "energy_transportation",
    "Retail & Wholesale":        "retail_wholesale",
    "Manufacturing":             "manufacturing",
    "Mergers & Acquisitions":    "mergers_and_acquisitions",
    "Real Estate":               "real_estate",
    "Economy — Fiscal":          "economy_fiscal",
    "Economy — Monetary":        "economy_monetary",
    "IPO":                       "ipo",
    "Blockchain":                "blockchain",
}

NEWSDEV_TIMEFRAMES = {
    "Last 7 days":    7,
    "Last 30 days":   30,
    "Last 90 days":   90,
    "Last 365 days":  365,
}

# ---------------------------------------------------------------------------
# Key Developments — CIQ-aligned categories, subtypes, timeframes
# ---------------------------------------------------------------------------

# Internal key → CIQ display label (full 12-category CIQ taxonomy)
KEYDEV_CATEGORIES = {
    "results_announcements":            "Results Announcements/Corporate Communications",
    "announced_completed_transactions": "Announced/Completed Transactions",
    "bankruptcy_updates":               "Bankruptcy Updates",
    "company_forecasts_ratings":        "Company Forecasts and Ratings",
    "corporate_structure":              "Corporate Structure Related",
    "customer_product":                 "Customer/Product Related",
    "dividends_splits":                 "Dividends/Splits",
    "investor_activism":                "Investor Activism",
    "listing_trading":                  "Listing/Trading Related",
    "red_flags_distress":               "Potential Red Flags/Distress Indicators",
    "potential_transactions":           "Potential Transactions",
    "transaction_updates":              "Transaction Updates",
}

# All 15 categories from Excel files (Keyaitems.docx + generated data)
# UI Label -> Exact DB event_category value (for exact match queries)
KEYDEV_CATEGORIES_ALL = {
    # ── Earnings / Guidance ───────────────────────────────────────────────────
    "Earnings & Guidance":          "Earnings & Guidance",
    "Press Release":                "Press Release",
    "Restatements":                 "Restatements",
    # ── M&A / Corporate Actions ───────────────────────────────────────────────
    "M&A Activity":                 "M&A Activity",
    "Capital Raising":              "Capital Raising",
    # ── Management / Governance ───────────────────────────────────────────────
    "Management Changes":           "Management Changes",
    "Governance":                   "Governance",
    "Proxy/Governance":             "Proxy/Governance",
    "Auditor Changes":              "Auditor Changes",
    # ── Capital Returns ───────────────────────────────────────────────────────
    "Capital Returns":              "Capital Returns",
    # ── Distress / Restructuring ──────────────────────────────────────────────
    "Layoffs & Restructuring":      "Layoffs & Restructuring",
    "Bankruptcy":                   "Bankruptcy",
    "Debt Default":                 "Debt Default",
    "Impairments":                  "Impairments",
    "Risk Factors":                 "Risk Factors",
    # ── Legal / Regulatory ────────────────────────────────────────────────────
    "Legal":                        "Legal",
    # ── Investor / Activism ───────────────────────────────────────────────────
    "Investor Activism":            "Investor Activism",
    "Investor / Institutional Ownership": "Investor / Institutional Ownership",
    # ── Trading / Listing ─────────────────────────────────────────────────────
    "Trading/Listing":              "Trading/Listing",
    # ── Credit / Ratings ─────────────────────────────────────────────────────
    "Credit/Ratings":               "Credit/Ratings",
    # ── Operations ────────────────────────────────────────────────────────────
    "Store Operations":             "Store Operations",
    # ── Other / General ───────────────────────────────────────────────────────
    "General News":                 "General News",
    "Other Events":                 "Other Events",
}

# Backward-compat: old DB/session-state keys → canonical new keys.
# Used during the transition period before DB category values are migrated.
KEYDEV_CATEGORY_ALIASES = {
    "announced_transactions": "announced_completed_transactions",
    "red_flags":              "red_flags_distress",
    # Excel-style categories from generate_key_developments_v3.py → DB categories
    "Earnings & Guidance":    "results_announcements",
    "Press Release":          "results_announcements",
    "General News":           "results_announcements",
    "Management Changes":     "corporate_structure",
    "Governance":             "corporate_structure",
    "Proxy/Governance":       "corporate_structure",
    "Capital Returns":        "dividends_splits",
    "M&A Activity":           "announced_completed_transactions",
    "Capital Raising":        "announced_completed_transactions",
    "Store Operations":       "customer_product",
    "Layoffs & Restructuring": "red_flags_distress",
    "Bankruptcy":             "bankruptcy_updates",
    "Risk Factors":           "red_flags_distress",
    "Legal":                  "red_flags_distress",
    "Other Events":           "results_announcements",
    "Other":                  "results_announcements",
}

# Reverse mapping: canonical DB key → list of all Excel-style aliases (for query expansion)
KEYDEV_CATEGORY_REVERSE_ALIASES = {
    "results_announcements":    ["Earnings & Guidance", "Press Release", "General News", "Other Events", "Other"],
    "corporate_structure":      ["Management Changes", "Governance", "Proxy/Governance"],
    "dividends_splits":         ["Capital Returns"],
    "announced_completed_transactions": ["M&A Activity", "Capital Raising"],
    "customer_product":         ["Store Operations"],
    "red_flags_distress":       ["Layoffs & Restructuring", "Risk Factors", "Legal"],
    "bankruptcy_updates":       ["Bankruptcy"],
}

# For the summary bar — CIQ display label → internal key (reverse)
KEYDEV_LABEL_TO_KEY = {v: k for k, v in KEYDEV_CATEGORIES.items()}

KEYDEV_TIMEFRAMES = {
    "Last 1 Day":    1,
    "Last 7 Days":   7,
    "Last 30 Days":  30,
    "Last 90 Days":  90,
    "Last 6 Months": 180,
    "Last 9 Months": 270,
    "Last 365 Days": 365,
    "All History":   None,
}
