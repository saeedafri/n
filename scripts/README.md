# SEC Filing Scripts - User Guide

## Overview

Two main scripts to download SEC filings and load them into the database:

- **Script 1**: Download HTML from SEC Edgar
- **Script 2**: Generate JSON + Load to DB + Cleanup

---

## Script 1: fetch_filing_html.py

Download 10-K, 10-Q, and 20-F filing HTML from SEC EDGAR.

### Usage Examples

```bash
# Single company, single year (10-K + 10-Q)
python3 scripts/fetch_filing_html.py AAPL --years 2024

# Multiple companies, multiple years
python3 scripts/fetch_filing_html.py AAPL AMZN WMT --years 2024 2025

# All companies in data/filings/ (full backfill)
python3 scripts/fetch_filing_html.py --all --years 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025

# Only 10-K (annual reports)
python3 scripts/fetch_filing_html.py AAPL --years 2024 --form 10-K

# Only 10-Q (quarterly reports)
python3 scripts/fetch_filing_html.py AAPL --years 2024 --form 10-Q

# 20-F for foreign companies (e.g., TSM, BABA, NVO)
python3 scripts/fetch_filing_html.py TSM --years 2024 --form 20-F

# Multiple foreign companies
python3 scripts/fetch_filing_html.py TSM BABA NVO --years 2023 2024 --form 20-F
```

### Output Location

```
data/filings/{TICKER}/{YEAR}/{DOC_TYPE}/filing.html

Examples:
data/filings/AAPL/2024/10-K/filing.html
data/filings/AAPL/2024/10-Q-Q1/filing.html
data/filings/TSM/2024/20-F/filing.html
```

---

## Script 2: generate_and_load.py

Generate JSON files from XBRL data and load to database.

### Usage Examples

```bash
# Single company, single year (10-K + 10-Q)
python3 scripts/generate_and_load.py AAPL --years 2024

# Multiple companies, multiple years
python3 scripts/generate_and_load.py AAPL AMZN WMT --years 2024 2025

# All companies (full backfill)
python3 scripts/generate_and_load.py --all --years 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025

# Only 10-K (annual reports)
python3 scripts/generate_and_load.py AAPL --years 2024 --form 10-K

# Only 10-Q (quarterly reports)
python3 scripts/generate_and_load.py AAPL --years 2024 --form 10-Q

# 20-F for foreign companies
python3 scripts/generate_and_load.py TSM --years 2024 --form 20-F

# Multiple foreign companies
python3 scripts/generate_and_load.py TSM BABA --years 2023 2024 --form 20-F
```

### What It Does

1. **Generates JSONs** from XBRL data:
   - `FINAL_FACTS_FILTERED.json` - XBRL facts
   - `FINANCIAL_RATIOS.json` - Calculated ratios
   - `SECTION_CACHE.json` - Document sections (10-K only)
   - `CREDIT_RATING.json` - Credit ratings (10-K only)
   - `STORE_COUNT.json` - Store counts (10-K only)

2. **Loads to DB** - All JSON data goes to `coreiq_filing_metrics` table

3. **Cleanup** - Deletes JSON files after successful DB load

---

## Supported Filing Types

| Form | Description | Companies | XBRL Support |
|------|-------------|-----------|--------------|
| **10-K** | Annual Report | US Companies | ✅ Full |
| **10-Q** | Quarterly Report | US Companies | ✅ Full |
| **20-F** | Annual Report (Foreign) | Foreign Companies | ✅ Full |

### Foreign Companies (20-F examples)

| Ticker | Company | Country |
|--------|---------|---------|
| TSM | Taiwan Semiconductor | Taiwan |
| BABA | Alibaba Group | China |
| NVO | Novo Nordisk | Denmark |
| SAP | SAP SE | Germany |
| SONY | Sony Group | Japan |

---

## STG Database Configuration

To load data into STG (Staging) database instead of local:

```bash
# Set STG DB environment variables
export DB_HOST=$STG_DB_HOST
export DB_PORT=$STG_DB_PORT
export DB_NAME=$STG_DB_NAME
export DB_USER=$STG_DB_USER
export DB_PASSWORD=$STG_DB_PASSWORD
export ENABLE_SSL=true

# Run scripts (same commands)
python3 scripts/fetch_filing_html.py TGT --years 2025
python3 scripts/generate_and_load.py TGT --years 2025
```

### STG DB Details

- **Host**: `csr-mysql8-flex-stg.mysql.database.azure.com`
- **Database**: `coresight_market_data_stg`
- **SSL**: Required

---

## Full Workflow Example

```bash
# Step 1: Download filings from SEC
python3 scripts/fetch_filing_html.py TGT --years 2025

# Step 2: Generate JSON and load to DB
python3 scripts/generate_and_load.py TGT --years 2025

# For foreign companies (20-F)
python3 scripts/fetch_filing_html.py TSM --years 2024 --form 20-F
python3 scripts/generate_and_load.py TSM --years 2024 --form 20-F
```

---

## Script Dependencies

```
scripts/
├── fetch_filing_html.py          ← SCRIPT 1: Download HTML
├── generate_and_load.py          ← SCRIPT 2: JSON → DB → Cleanup
├── export_xbrl_facts.py          ← Dependency: XBRL processing
├── load_filings_to_db.py         ← Dependency: DB loading
├── load_supplementary_json.py    ← Dependency: Financial ratios
├── enrich_from_edgartools.py     ← Dependency: Section cache
├── extract_credit_ratings.py     ← Dependency: Credit ratings
├── extract_store_counts.py       ← Dependency: Store counts
├── calculate_derived_metrics.py  ← Dependency: Ratio calculations
├── azure_filing_uploader.py      ← Utility: Azure upload
└── README.md                     ← This file
```

---

## Troubleshooting

### Issue: "No filing found on SEC EDGAR"

- Check if the ticker is correct
- Check if the year has filings (some companies don't file every year)
- For 20-F: Ensure the company is foreign (not US-based)

### Issue: "DB connection failed"

- Check `.env` file has correct DB credentials
- For STG: Ensure SSL is enabled (`ENABLE_SSL=true`)
- Check if VPN is required for STG database

### Issue: Empty HTML content

- Try again later (SEC might be rate limiting)
- Check Edgar identity is set in `.env` file

---

## Notes

- **10-K**: Annual report, filed once per year
- **10-Q**: Quarterly report, filed 3 times per year (Q1, Q2, Q3)
- **20-F**: Foreign company annual report (no quarterly 20-F filings)
- Scripts skip already downloaded/processed filings automatically
- JSON files are deleted after successful DB load (use `--keep-json` to preserve)
