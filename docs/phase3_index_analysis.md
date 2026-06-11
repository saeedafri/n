# Phase 3 — Index Investigation

**Generated:** 2026-06-11
**DB:** `coresight_market_data_stg` @ `csr-mysql8-flex-stg.mysql.database.azure.com`

> Read-only investigation. No indexes added yet.

## SHOW INDEX


#### `coreiq_nasdaq_earnings_calendar`

|Key_name                |Non_unique|Seq_in_index|Column_name        |Cardinality|Index_type|Comment|
|------------------------|----------|------------|-------------------|-----------|----------|-------|
|PRIMARY                 |0         |1           |id                 |11124      |BTREE     |       |
|uq_src_date_ticker      |0         |1           |source             |1          |BTREE     |       |
|uq_src_date_ticker      |0         |2           |earnings_date      |3068       |BTREE     |       |
|uq_src_date_ticker      |0         |3           |ticker             |11141      |BTREE     |       |
|idx_ticker              |1         |1           |ticker             |207        |BTREE     |       |
|idx_earnings_date       |1         |1           |earnings_date      |3068       |BTREE     |       |
|idx_last_changed        |1         |1           |last_changed_at_utc|8406       |BTREE     |       |
|idx_last_action         |1         |1           |last_action        |2          |BTREE     |       |
|idx_ticker_earnings_date|1         |1           |ticker             |207        |BTREE     |       |
|idx_ticker_earnings_date|1         |2           |earnings_date      |11141      |BTREE     |       |

*(10 index records total)*

#### `coreiq_yf_earnings_calendar`

|Key_name             |Non_unique|Seq_in_index|Column_name  |Cardinality|Index_type|Comment|
|---------------------|----------|------------|-------------|-----------|----------|-------|
|PRIMARY              |0         |1           |id           |513        |BTREE     |       |
|uq_earnings          |0         |1           |company_id   |39         |BTREE     |       |
|uq_earnings          |0         |2           |yf_symbol    |39         |BTREE     |       |
|uq_earnings          |0         |3           |earnings_date|513        |BTREE     |       |
|idx_company_id       |1         |1           |company_id   |39         |BTREE     |       |
|idx_yf_symbol        |1         |1           |yf_symbol    |39         |BTREE     |       |
|idx_ticker           |1         |1           |ticker       |39         |BTREE     |       |
|idx_earnings_date    |1         |1           |earnings_date|421        |BTREE     |       |
|idx_yf_ticker_date   |1         |1           |ticker       |39         |BTREE     |       |
|idx_yf_ticker_date   |1         |2           |earnings_date|513        |BTREE     |       |
|idx_yf_cover_calendar|1         |1           |ticker       |39         |BTREE     |       |
|idx_yf_cover_calendar|1         |2           |earnings_date|513        |BTREE     |       |
|idx_yf_cover_calendar|1         |3           |company_name |513        |BTREE     |       |
|idx_yf_cover_calendar|1         |4           |reported_eps |513        |BTREE     |       |
|idx_yf_cover_calendar|1         |5           |eps_estimate |513        |BTREE     |       |
|idx_yf_cover_calendar|1         |6           |surprise_pct |513        |BTREE     |       |
|idx_yf_cover_tickers |1         |1           |ticker       |39         |BTREE     |       |
|idx_yf_cover_tickers |1         |2           |company_name |39         |BTREE     |       |

*(18 index records total)*

#### `coreiq_av_earnings_call_transcripts`

|Key_name                      |Non_unique|Seq_in_index|Column_name       |Cardinality|Index_type|Comment|
|------------------------------|----------|------------|------------------|-----------|----------|-------|
|PRIMARY                       |0         |1           |id                |17681      |BTREE     |       |
|uq_src_ticker_quarter         |0         |1           |source            |1          |BTREE     |       |
|uq_src_ticker_quarter         |0         |2           |ticker            |277        |BTREE     |       |
|uq_src_ticker_quarter         |0         |3           |quarter           |16864      |BTREE     |       |
|idx_ticker                    |1         |1           |ticker            |244        |BTREE     |       |
|idx_year_q                    |1         |1           |year              |21         |BTREE     |       |
|idx_year_q                    |1         |2           |q                 |80         |BTREE     |       |
|idx_ec_ticker_quarter         |1         |1           |ticker            |244        |BTREE     |       |
|idx_ec_ticker_quarter         |1         |2           |quarter           |17111      |BTREE     |       |
|idx_has_transcript_year       |1         |1           |has_transcript    |2          |BTREE     |       |
|idx_has_transcript_year       |1         |2           |year              |42         |BTREE     |       |
|idx_has_transcript_ticker     |1         |1           |has_transcript    |2          |BTREE     |       |
|idx_has_transcript_ticker     |1         |2           |ticker            |451        |BTREE     |       |
|idx_covering_transcript_lookup|1         |1           |has_transcript    |1          |BTREE     |       |
|idx_covering_transcript_lookup|1         |2           |ticker            |321        |BTREE     |       |
|idx_covering_transcript_lookup|1         |3           |transcript_text   |8467       |BTREE     |       |
|idx_ec_ticker_year_q          |1         |1           |ticker            |244        |BTREE     |       |
|idx_ec_ticker_year_q          |1         |2           |year              |4485       |BTREE     |       |
|idx_ec_ticker_year_q          |1         |3           |q                 |17111      |BTREE     |       |
|idx_ec_ticker_year_q_event    |1         |1           |ticker            |244        |BTREE     |       |
|idx_ec_ticker_year_q_event    |1         |2           |year              |4485       |BTREE     |       |
|idx_ec_ticker_year_q_event    |1         |3           |q                 |17111      |BTREE     |       |
|idx_ec_ticker_year_q_event    |1         |4           |event_datetime_utc|17111      |BTREE     |       |
|idx_ec_cover_transcript_fetch |1         |1           |ticker            |287        |BTREE     |       |
|idx_ec_cover_transcript_fetch |1         |2           |year              |5250       |BTREE     |       |
|idx_ec_cover_transcript_fetch |1         |3           |q                 |15615      |BTREE     |       |
|idx_ec_cover_transcript_fetch |1         |4           |has_transcript    |17681      |BTREE     |       |
|idx_ec_cover_transcript_fetch |1         |5           |transcript_text   |17253      |BTREE     |       |
|idx_ec_cover_company_lookup   |1         |1           |has_transcript    |2          |BTREE     |       |
|idx_ec_cover_company_lookup   |1         |2           |transcript_text   |568        |BTREE     |       |
|idx_ec_cover_company_lookup   |1         |3           |ticker            |1061       |BTREE     |       |
|idx_ec_cover_ticker_years     |1         |1           |ticker            |244        |BTREE     |       |
|idx_ec_cover_ticker_years     |1         |2           |has_transcript    |451        |BTREE     |       |
|idx_ec_cover_ticker_years     |1         |3           |transcript_text   |1061       |BTREE     |       |
|idx_ec_cover_ticker_years     |1         |4           |year              |6206       |BTREE     |       |
|idx_ec_cover_ticker_quarters  |1         |1           |ticker            |244        |BTREE     |       |
|idx_ec_cover_ticker_quarters  |1         |2           |year              |4485       |BTREE     |       |
|idx_ec_cover_ticker_quarters  |1         |3           |has_transcript    |5054       |BTREE     |       |
|idx_ec_cover_ticker_quarters  |1         |4           |transcript_text   |6206       |BTREE     |       |
|idx_ec_cover_ticker_quarters  |1         |5           |q                 |17111      |BTREE     |       |
|idx_ec_list_ordered           |1         |1           |ticker            |244        |BTREE     |       |
|idx_ec_list_ordered           |1         |2           |has_transcript    |451        |BTREE     |       |
|idx_ec_list_ordered           |1         |3           |year              |5054       |BTREE     |       |
|idx_ec_list_ordered           |1         |4           |q                 |17111      |BTREE     |       |
|idx_transcript_fulltext_search|1         |1           |transcript_text   |17681      |FULLTEXT  |       |

*(45 index records total)*

#### `coreiq_ir_websites`

|Key_name           |Non_unique|Seq_in_index|Column_name      |Cardinality|Index_type|Comment|
|-------------------|----------|------------|-----------------|-----------|----------|-------|
|PRIMARY            |0         |1           |id               |278        |BTREE     |       |
|uq_company_row     |0         |1           |source           |2          |BTREE     |       |
|uq_company_row     |0         |2           |ticker           |278        |BTREE     |       |
|uq_company_row     |0         |3           |company_name     |278        |BTREE     |       |
|uq_company_row     |0         |4           |company_source   |278        |BTREE     |       |
|uq_company_row     |0         |5           |yf_symbol        |278        |BTREE     |       |
|idx_company_website|1         |1           |company_website  |251        |BTREE     |       |
|idx_ticker         |1         |1           |ticker           |251        |BTREE     |       |
|idx_domain         |1         |1           |normalized_domain|238        |BTREE     |       |
|idx_last_action    |1         |1           |last_action      |1          |BTREE     |       |
|idx_yf_symbol      |1         |1           |yf_symbol        |255        |BTREE     |       |
|idx_fetched_at     |1         |1           |fetched_at_utc   |278        |BTREE     |       |

*(12 index records total)*

#### `company_ir_websites`

ERROR: (1146, "Table 'coresight_market_data_stg.company_ir_websites' doesn't exist")

## Table Row Counts

```
  coreiq_nasdaq_earnings_calendar: 11,996
  coreiq_yf_earnings_calendar: 521
  coreiq_av_earnings_call_transcripts: 18,336
  coreiq_ir_websites: 290
  company_ir_websites: ERROR (1146, "Table 'coresight_market_data_stg.company_ir_websites' doesn't exist")
```

## EXPLAIN ANALYZE — Earnings Calendar Queries


### 1. All-company, month window


*Parameters:* `start_date=2026-05-31`, `end_date=2026-07-11` (42-day Sunday grid for June 2026)


### All-company month window

**Query:**
```sql
SELECT id, ticker, calendar_company_name, earnings_date, report_time,
       fiscal_quarter_ending, fiscal_q, report_fiscal_year,
       eps_actual, eps_forecast, surprise_pct, market_cap,
       num_estimates, data_source
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY ticker, fiscal_quarter_ending
        ORDER BY fetched_at_utc DESC,
                 CASE data_source WHEN 'nasdaq' THEN 0 ELSE 1 END ASC,
                 earnings_date DESC,
                 id DESC
    ) AS rn
    FROM (
        SELECT 
ec.id,
ec.ticker,
ec.company_name   AS calendar_company_name,
ec.earnings_date,
ec.report_time,
ec.fiscal_quarter_ending,
QUARTER(ec.fiscal_quarter_ending) AS fiscal_q,
YEAR(ec.fiscal_quarter_ending)    AS report_fiscal_year,
ec.eps_actual,
ec.eps_forecast,
ec.surprise_pct,
ec.market_cap,
ec.num_estimates,
ec.fetched_at_utc AS fetched_at_utc,
'nasdaq'          AS data_source

        FROM coreiq_nasdaq_earnings_calendar ec
        WHERE ec.fiscal_quarter_ending IS NOT NULL
          AND ec.earnings_date >= '2026-05-31' AND ec.earnings_date <= '2026-07-11'
        UNION ALL
        SELECT 
yf.id + 10000000  AS id,
yf.ticker,
yf.company_name   AS calendar_company_name,
yf.earnings_date,
'time-not-supplied' AS report_time,
CONCAT(DATE_FORMAT(yf.earnings_date,'%b'),'/',YEAR(yf.earnings_date)) AS fiscal_quarter_ending,
QUARTER(yf.earnings_date) AS fiscal_q,
YEAR(yf.earnings_date)    AS report_fiscal_year,
yf.reported_eps   AS eps_actual,
yf.eps_estimate   AS eps_forecast,
yf.surprise_pct,
NULL              AS market_cap,
NULL              AS num_estimates,
yf.ingested_at    AS fetched_at_utc,
'yf'              AS data_source

        FROM coreiq_yf_earnings_calendar yf
        WHERE yf.ticker IS NOT NULL
          AND yf.earnings_date >= '2026-05-31' AND yf.earnings_date <= '2026-07-11'
    ) combined
) ranked
WHERE rn = 1
```

**EXPLAIN (estimated):**
```
  id: 1
  select_type: PRIMARY
  table: <derived2>
  type: ref
  possible_keys: <auto_key0>
  key: <auto_key0>
  key_len: 8
  ref: const
  rows: 6
  filtered: 100.0

  id: 2
  select_type: DERIVED
  table: <derived3>
  type: ALL
  rows: 67
  filtered: 100.0
  Extra: Using filesort

  id: 3
  select_type: DERIVED
  table: ec
  type: range
  possible_keys: idx_earnings_date
  key: idx_earnings_date
  key_len: 3
  rows: 68
  filtered: 90.0
  Extra: Using index condition; Using where

  id: 4
  select_type: UNION
  table: yf
  type: range
  possible_keys: idx_ticker,idx_earnings_date,idx_yf_ticker_date,idx_yf_cover_calendar,idx_yf_cover_tickers
  key: idx_earnings_date
  key_len: 3
  rows: 6
  filtered: 100.0
  Extra: Using index condition; Using where

```

**EXPLAIN ANALYZE (actual):**
```
-> Index lookup on ranked using <auto_key0> (rn=1)  (cost=0.35..2.35 rows=6.7) (actual time=68.5..68.5 rows=60 loops=1)
    -> Materialize  (cost=0..0 rows=0) (actual time=68.5..68.5 rows=74 loops=1)
        -> Window aggregate: row_number() OVER (PARTITION BY combined.ticker,combined.fiscal_quarter_ending ORDER BY combined.fetched_at_utc desc,`(case combined.data_source when 'nasdaq' then 0 else 1 end)`,combined.earnings_date desc,combined.id desc )   (actual time=68.2..68.3 rows=74 loops=1)
            -> Sort: combined.ticker, combined.fiscal_quarter_ending, combined.fetched_at_utc DESC, `(case combined.data_source when 'nasdaq' then 0 else 1 end)`, combined.earnings_date DESC, combined.id DESC  (cost=147..147 rows=67.2) (actual time=68.2..68.2 rows=74 loops=1)
                -> Table scan on combined  (cost=96.5..99.8 rows=67.2) (actual time=68.1..68.1 rows=74 loops=1)
                    -> Union all materialize  (cost=96.4..96.4 rows=67.2) (actual time=68.1..68.1 rows=74 loops=1)
                        -> Filter: (ec.fiscal_quarter_ending is not null)  (cost=81.5 rows=61.2) (actual time=10.1..39.7 rows=68 loops=1)
                            -> Index range scan on ec using idx_earnings_date over ('2026-05-31' <= earnings_date <= '2026-07-11'), with index condition: ((ec.earnings_date >= DATE'2026-05-31') and (ec.earnings_date <= DATE'2026-07-11'))  (cost=81.5 rows=68) (actual time=10..39.6 rows=68 loops=1)
                        -> Filter: (yf.ticker is not null)  (cost=8.21 rows=6) (actual time=24.1..27.1 rows=6 loops=1)
                            -> Index range scan on yf using idx_earnings_date over ('2026-05-31' <= earnings_date <= '2026-07-11'), with index condition: ((yf.earnings_date >= DATE'2026-05-31') and (yf.earnings_date <= DATE'2026-07-11'))  (cost=8.21 rows=6) (actual time=24.1..27.1 rows=6 loops=1)

```

### 2. All-company, year window


*Parameters:* `start_date=2026-01-01`, `end_date=2026-12-31` (full year 2026)


### All-company year window

**Query:**
```sql
SELECT id, ticker, calendar_company_name, earnings_date, report_time,
       fiscal_quarter_ending, fiscal_q, report_fiscal_year,
       eps_actual, eps_forecast, surprise_pct, market_cap,
       num_estimates, data_source
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY ticker, fiscal_quarter_ending
        ORDER BY fetched_at_utc DESC,
                 CASE data_source WHEN 'nasdaq' THEN 0 ELSE 1 END ASC,
                 earnings_date DESC,
                 id DESC
    ) AS rn
    FROM (
        SELECT 
ec.id,
ec.ticker,
ec.company_name   AS calendar_company_name,
ec.earnings_date,
ec.report_time,
ec.fiscal_quarter_ending,
QUARTER(ec.fiscal_quarter_ending) AS fiscal_q,
YEAR(ec.fiscal_quarter_ending)    AS report_fiscal_year,
ec.eps_actual,
ec.eps_forecast,
ec.surprise_pct,
ec.market_cap,
ec.num_estimates,
ec.fetched_at_utc AS fetched_at_utc,
'nasdaq'          AS data_source

        FROM coreiq_nasdaq_earnings_calendar ec
        WHERE ec.fiscal_quarter_ending IS NOT NULL
          AND ec.earnings_date >= '2026-01-01' AND ec.earnings_date <= '2026-12-31'
        UNION ALL
        SELECT 
yf.id + 10000000  AS id,
yf.ticker,
yf.company_name   AS calendar_company_name,
yf.earnings_date,
'time-not-supplied' AS report_time,
CONCAT(DATE_FORMAT(yf.earnings_date,'%b'),'/',YEAR(yf.earnings_date)) AS fiscal_quarter_ending,
QUARTER(yf.earnings_date) AS fiscal_q,
YEAR(yf.earnings_date)    AS report_fiscal_year,
yf.reported_eps   AS eps_actual,
yf.eps_estimate   AS eps_forecast,
yf.surprise_pct,
NULL              AS market_cap,
NULL              AS num_estimates,
yf.ingested_at    AS fetched_at_utc,
'yf'              AS data_source

        FROM coreiq_yf_earnings_calendar yf
        WHERE yf.ticker IS NOT NULL
          AND yf.earnings_date >= '2026-01-01' AND yf.earnings_date <= '2026-12-31'
    ) combined
) ranked
WHERE rn = 1
```

**EXPLAIN (estimated):**
```
  id: 1
  select_type: PRIMARY
  table: <derived2>
  type: ref
  possible_keys: <auto_key0>
  key: <auto_key0>
  key_len: 8
  ref: const
  rows: 10
  filtered: 100.0

  id: 2
  select_type: DERIVED
  table: <derived3>
  type: ALL
  rows: 424
  filtered: 100.0
  Extra: Using filesort

  id: 3
  select_type: DERIVED
  table: ec
  type: range
  possible_keys: idx_earnings_date
  key: idx_earnings_date
  key_len: 3
  rows: 383
  filtered: 90.0
  Extra: Using index condition; Using where

  id: 4
  select_type: UNION
  table: yf
  type: ALL
  possible_keys: idx_ticker,idx_earnings_date,idx_yf_ticker_date,idx_yf_cover_calendar,idx_yf_cover_tickers
  rows: 513
  filtered: 15.59
  Extra: Using where

```

**EXPLAIN ANALYZE (actual):**
```
-> Index lookup on ranked using <auto_key0> (rn=1)  (cost=0.35..3.53 rows=10.1) (actual time=57.4..57.5 rows=385 loops=1)
    -> Materialize  (cost=0..0 rows=0) (actual time=57.4..57.4 rows=463 loops=1)
        -> Window aggregate: row_number() OVER (PARTITION BY combined.ticker,combined.fiscal_quarter_ending ORDER BY combined.fetched_at_utc desc,`(case combined.data_source when 'nasdaq' then 0 else 1 end)`,combined.earnings_date desc,combined.id desc )   (actual time=55.9..56.6 rows=463 loops=1)
            -> Sort: combined.ticker, combined.fiscal_quarter_ending, combined.fetched_at_utc DESC, `(case combined.data_source when 'nasdaq' then 0 else 1 end)`, combined.earnings_date DESC, combined.id DESC  (cost=968..968 rows=425) (actual time=55.9..56 rows=463 loops=1)
                -> Table scan on combined  (cost=547..555 rows=425) (actual time=55.3..55.5 rows=463 loops=1)
                    -> Union all materialize  (cost=547..547 rows=425) (actual time=55.3..55.3 rows=463 loops=1)
                        -> Filter: (ec.fiscal_quarter_ending is not null)  (cost=447 rows=345) (actual time=9.87..30.5 rows=383 loops=1)
                            -> Index range scan on ec using idx_earnings_date over ('2026-01-01' <= earnings_date <= '2026-12-31'), with index condition: ((ec.earnings_date >= DATE'2026-01-01') and (ec.earnings_date <= DATE'2026-12-31'))  (cost=447 rows=383) (actual time=9.86..30.5 rows=383 loops=1)
                        -> Filter: ((yf.ticker is not null) and (yf.earnings_date >= DATE'2026-01-01') and (yf.earnings_date <= DATE'2026-12-31'))  (cost=58 rows=80) (actual time=13.6..23.5 rows=80 loops=1)
                            -> Table scan on yf  (cost=58 rows=513) (actual time=6.83..23.4 rows=521 loops=1)

```

### 3. Single-company, month window


*Parameters:* `tickers=('AAPL',)`, `start_date=2026-05-31`, `end_date=2026-07-11`


### Single-company month window

**Query:**
```sql
SELECT id, ticker, calendar_company_name, earnings_date, report_time,
       fiscal_quarter_ending, fiscal_q, report_fiscal_year,
       eps_actual, eps_forecast, surprise_pct, market_cap,
       num_estimates, data_source
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY ticker, fiscal_quarter_ending
        ORDER BY fetched_at_utc DESC,
                 CASE data_source WHEN 'nasdaq' THEN 0 ELSE 1 END ASC,
                 earnings_date DESC,
                 id DESC
    ) AS rn
    FROM (
        SELECT 
ec.id,
ec.ticker,
ec.company_name   AS calendar_company_name,
ec.earnings_date,
ec.report_time,
ec.fiscal_quarter_ending,
QUARTER(ec.fiscal_quarter_ending) AS fiscal_q,
YEAR(ec.fiscal_quarter_ending)    AS report_fiscal_year,
ec.eps_actual,
ec.eps_forecast,
ec.surprise_pct,
ec.market_cap,
ec.num_estimates,
ec.fetched_at_utc AS fetched_at_utc,
'nasdaq'          AS data_source

        FROM coreiq_nasdaq_earnings_calendar ec
        WHERE ec.fiscal_quarter_ending IS NOT NULL
          AND ec.ticker IN ('AAPL') AND ec.earnings_date >= '2026-05-31' AND ec.earnings_date <= '2026-07-11'
        UNION ALL
        SELECT 
yf.id + 10000000  AS id,
yf.ticker,
yf.company_name   AS calendar_company_name,
yf.earnings_date,
'time-not-supplied' AS report_time,
CONCAT(DATE_FORMAT(yf.earnings_date,'%b'),'/',YEAR(yf.earnings_date)) AS fiscal_quarter_ending,
QUARTER(yf.earnings_date) AS fiscal_q,
YEAR(yf.earnings_date)    AS report_fiscal_year,
yf.reported_eps   AS eps_actual,
yf.eps_estimate   AS eps_forecast,
yf.surprise_pct,
NULL              AS market_cap,
NULL              AS num_estimates,
yf.ingested_at    AS fetched_at_utc,
'yf'              AS data_source

        FROM coreiq_yf_earnings_calendar yf
        WHERE yf.ticker IS NOT NULL
          AND yf.ticker IN ('AAPL') AND yf.earnings_date >= '2026-05-31' AND yf.earnings_date <= '2026-07-11'
    ) combined
) ranked
WHERE rn = 1
```

**EXPLAIN (estimated):**
```
  id: 1
  select_type: PRIMARY
  table: <derived2>
  type: ref
  possible_keys: <auto_key0>
  key: <auto_key0>
  key_len: 8
  ref: const
  rows: 1
  filtered: 100.0

  id: 2
  select_type: DERIVED
  table: <derived3>
  type: ALL
  rows: 4
  filtered: 100.0
  Extra: Using filesort

  id: 3
  select_type: DERIVED
  table: ec
  type: range
  possible_keys: idx_ticker,idx_earnings_date,idx_ticker_earnings_date
  key: idx_ticker_earnings_date
  key_len: 133
  rows: 1
  filtered: 90.0
  Extra: Using index condition; Using where

  id: 4
  select_type: UNION
  table: yf
  type: ref
  possible_keys: idx_ticker,idx_earnings_date,idx_yf_ticker_date,idx_yf_cover_calendar,idx_yf_cover_tickers
  key: idx_ticker
  key_len: 259
  ref: const
  rows: 1
  filtered: 5.0
  Extra: Using index condition; Using where

```

**EXPLAIN ANALYZE (actual):**
```
-> Index lookup on ranked using <auto_key0> (rn=1)  (cost=0.35..0.35 rows=1) (actual time=0.0543..0.0543 rows=0 loops=1)
    -> Materialize  (cost=0..0 rows=0) (actual time=0.0536..0.0536 rows=0 loops=1)
        -> Window aggregate: row_number() OVER (PARTITION BY combined.ticker,combined.fiscal_quarter_ending ORDER BY combined.fetched_at_utc desc,`(case combined.data_source when 'nasdaq' then 0 else 1 end)`,combined.earnings_date desc,combined.id desc )   (actual time=0.0451..0.0451 rows=0 loops=1)
            -> Sort: combined.ticker, combined.fiscal_quarter_ending, combined.fetched_at_utc DESC, `(case combined.data_source when 'nasdaq' then 0 else 1 end)`, combined.earnings_date DESC, combined.id DESC  (cost=5.05..5.05 rows=0.95) (actual time=0.0441..0.0441 rows=0 loops=1)
                -> Table scan on combined  (cost=4.95..4.95 rows=0.95) (actual time=0.0373..0.0373 rows=0 loops=1)
                    -> Union all materialize  (cost=2.45..2.45 rows=0.95) (actual time=0.0357..0.0357 rows=0 loops=1)
                        -> Filter: (ec.fiscal_quarter_ending is not null)  (cost=2.1 rows=0.9) (actual time=0.0229..0.0229 rows=0 loops=1)
                            -> Index range scan on ec using idx_ticker_earnings_date over (ticker = 'AAPL' AND '2026-05-31' <= earnings_date <= '2026-07-11'), with index condition: ((ec.ticker = 'AAPL') and (ec.earnings_date >= DATE'2026-05-31') and (ec.earnings_date <= DATE'2026-07-11'))  (cost=2.1 rows=1) (actual time=0.022..0.022 rows=0 loops=1)
                        -> Filter: ((yf.earnings_date >= DATE'2026-05-31') and (yf.earnings_date <= DATE'2026-07-11'))  (cost=0.255 rows=0.05) (actual time=0.0082..0.0082 rows=0 loops=1)
                            -> Index lookup on yf using idx_ticker (ticker='AAPL'), with index condition: (yf.ticker is not null)  (cost=0.255 rows=1) (actual time=0.0076..0.0076 rows=0 loops=1)

```

### 4. Single-company, year window


*Parameters:* `tickers=('AAPL',)`, `start_date=2026-01-01`, `end_date=2026-12-31`


### Single-company year window

**Query:**
```sql
SELECT id, ticker, calendar_company_name, earnings_date, report_time,
       fiscal_quarter_ending, fiscal_q, report_fiscal_year,
       eps_actual, eps_forecast, surprise_pct, market_cap,
       num_estimates, data_source
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY ticker, fiscal_quarter_ending
        ORDER BY fetched_at_utc DESC,
                 CASE data_source WHEN 'nasdaq' THEN 0 ELSE 1 END ASC,
                 earnings_date DESC,
                 id DESC
    ) AS rn
    FROM (
        SELECT 
ec.id,
ec.ticker,
ec.company_name   AS calendar_company_name,
ec.earnings_date,
ec.report_time,
ec.fiscal_quarter_ending,
QUARTER(ec.fiscal_quarter_ending) AS fiscal_q,
YEAR(ec.fiscal_quarter_ending)    AS report_fiscal_year,
ec.eps_actual,
ec.eps_forecast,
ec.surprise_pct,
ec.market_cap,
ec.num_estimates,
ec.fetched_at_utc AS fetched_at_utc,
'nasdaq'          AS data_source

        FROM coreiq_nasdaq_earnings_calendar ec
        WHERE ec.fiscal_quarter_ending IS NOT NULL
          AND ec.ticker IN ('AAPL') AND ec.earnings_date >= '2026-01-01' AND ec.earnings_date <= '2026-12-31'
        UNION ALL
        SELECT 
yf.id + 10000000  AS id,
yf.ticker,
yf.company_name   AS calendar_company_name,
yf.earnings_date,
'time-not-supplied' AS report_time,
CONCAT(DATE_FORMAT(yf.earnings_date,'%b'),'/',YEAR(yf.earnings_date)) AS fiscal_quarter_ending,
QUARTER(yf.earnings_date) AS fiscal_q,
YEAR(yf.earnings_date)    AS report_fiscal_year,
yf.reported_eps   AS eps_actual,
yf.eps_estimate   AS eps_forecast,
yf.surprise_pct,
NULL              AS market_cap,
NULL              AS num_estimates,
yf.ingested_at    AS fetched_at_utc,
'yf'              AS data_source

        FROM coreiq_yf_earnings_calendar yf
        WHERE yf.ticker IS NOT NULL
          AND yf.ticker IN ('AAPL') AND yf.earnings_date >= '2026-01-01' AND yf.earnings_date <= '2026-12-31'
    ) combined
) ranked
WHERE rn = 1
```

**EXPLAIN (estimated):**
```
  id: 1
  select_type: PRIMARY
  table: <derived2>
  type: ref
  possible_keys: <auto_key0>
  key: <auto_key0>
  key_len: 8
  ref: const
  rows: 1
  filtered: 100.0

  id: 2
  select_type: DERIVED
  table: <derived3>
  type: ALL
  rows: 4
  filtered: 100.0
  Extra: Using filesort

  id: 3
  select_type: DERIVED
  table: ec
  type: range
  possible_keys: idx_ticker,idx_earnings_date,idx_ticker_earnings_date
  key: idx_ticker_earnings_date
  key_len: 133
  rows: 3
  filtered: 90.0
  Extra: Using index condition; Using where

  id: 4
  select_type: UNION
  table: yf
  type: ref
  possible_keys: idx_ticker,idx_earnings_date,idx_yf_ticker_date,idx_yf_cover_calendar,idx_yf_cover_tickers
  key: idx_ticker
  key_len: 259
  ref: const
  rows: 1
  filtered: 15.59
  Extra: Using index condition; Using where

```

**EXPLAIN ANALYZE (actual):**
```
-> Index lookup on ranked using <auto_key0> (rn=1)  (cost=0.35..0.35 rows=1) (actual time=0.143..0.145 rows=3 loops=1)
    -> Materialize  (cost=0..0 rows=0) (actual time=0.142..0.142 rows=3 loops=1)
        -> Window aggregate: row_number() OVER (PARTITION BY combined.ticker,combined.fiscal_quarter_ending ORDER BY combined.fetched_at_utc desc,`(case combined.data_source when 'nasdaq' then 0 else 1 end)`,combined.earnings_date desc,combined.id desc )   (actual time=0.123..0.127 rows=3 loops=1)
            -> Sort: combined.ticker, combined.fiscal_quarter_ending, combined.fetched_at_utc DESC, `(case combined.data_source when 'nasdaq' then 0 else 1 end)`, combined.earnings_date DESC, combined.id DESC  (cost=8.19..8.19 rows=2.86) (actual time=0.117..0.118 rows=3 loops=1)
                -> Table scan on combined  (cost=5.83..7.47 rows=2.86) (actual time=0.094..0.0952 rows=3 loops=1)
                    -> Union all materialize  (cost=4.95..4.95 rows=2.86) (actual time=0.0917..0.0917 rows=3 loops=1)
                        -> Filter: (ec.fiscal_quarter_ending is not null)  (cost=4.4 rows=2.7) (actual time=0.0523..0.0565 rows=3 loops=1)
                            -> Index range scan on ec using idx_ticker_earnings_date over (ticker = 'AAPL' AND '2026-01-01' <= earnings_date <= '2026-12-31'), with index condition: ((ec.ticker = 'AAPL') and (ec.earnings_date >= DATE'2026-01-01') and (ec.earnings_date <= DATE'2026-12-31'))  (cost=4.4 rows=3) (actual time=0.0513..0.0553 rows=3 loops=1)
                        -> Filter: ((yf.earnings_date >= DATE'2026-01-01') and (yf.earnings_date <= DATE'2026-12-31'))  (cost=0.266 rows=0.156) (actual time=0.0082..0.0082 rows=0 loops=1)
                            -> Index lookup on yf using idx_ticker (ticker='AAPL'), with index condition: (yf.ticker is not null)  (cost=0.266 rows=1) (actual time=0.0077..0.0077 rows=0 loops=1)

```

### 5. Watchlist path


**Watchlist mode does NOT use a ticker prefilter in the DB query.**

The current implementation:
1. Fetches all events for the visible date window (same query as All-company above).
2. Calls `get_watchlist_companies(watchlist_id)` to retrieve watchlist member rows.
3. Applies `_filter_events_by_watchlist(prefetched_events, watchlist_rows)` in Python memory.

Therefore, the relevant EXPLAIN ANALYZE for watchlist mode is **Query 1 (month)** or
**Query 2 (year)** — the same all-company date-window query.

The watchlist company lookup is a separate simple query:
```sql
SELECT ticker, company_name, sector, country, added_by
FROM coreiq_watchlist_companies
WHERE watchlist_id = <id>
```
This is a primary key / indexed lookup and is not a bottleneck.


### 6. Transcript detail lookup


*Parameters:* `ticker='AAPL'`


### Transcript lookup by ticker

**Query:**
```sql
SELECT year, q, quarter
FROM coreiq_av_earnings_call_transcripts
WHERE ticker = 'AAPL'
  AND has_transcript = 1
ORDER BY year DESC, q DESC
LIMIT 80
```

**EXPLAIN (estimated):**
```
  id: 1
  select_type: SIMPLE
  table: coreiq_av_earnings_call_transcripts
  type: ref
  possible_keys: idx_ticker,idx_ec_ticker_quarter,idx_has_transcript_year,idx_has_transcript_ticker,idx_covering_transcript_lookup,idx_ec_ticker_year_q,idx_ec_ticker_year_q_event,idx_ec_cover_transcript_fetch,idx_ec_cover_company_lookup,idx_ec_cover_ticker_years,idx_ec_cover_ticker_quarters,idx_ec_list_ordered
  key: idx_has_transcript_ticker
  key_len: 131
  ref: const,const
  rows: 72
  filtered: 100.0
  Extra: Using filesort

```

**EXPLAIN ANALYZE (actual):**
```
-> Limit: 80 row(s)  (cost=79.2 rows=72) (actual time=13.6..13.6 rows=72 loops=1)
    -> Sort: coreiq_av_earnings_call_transcripts.`year` DESC, coreiq_av_earnings_call_transcripts.q DESC, limit input to 80 row(s) per chunk  (cost=79.2 rows=72) (actual time=13.6..13.6 rows=72 loops=1)
        -> Index lookup on coreiq_av_earnings_call_transcripts using idx_has_transcript_ticker (has_transcript=1, ticker='AAPL')  (cost=79.2 rows=72) (actual time=5.92..13.6 rows=72 loops=1)

```

### 7. IR website lookup (full table scan — both tables)


The `_build_ir_website_lookup()` fetches **all rows** from both IR tables at startup
(cached via `@st.cache_data`). No per-ticker filter is applied in SQL.


### IR lookup — coreiq_ir_websites (all rows)

**Query:**
```sql
SELECT *
FROM coreiq_ir_websites
WHERE ticker IS NOT NULL
  AND TRIM(ticker) != ''
```

**EXPLAIN (estimated):**
```
  id: 1
  select_type: SIMPLE
  table: coreiq_ir_websites
  type: ALL
  rows: 278
  filtered: 100.0
  Extra: Using where

```

**EXPLAIN ANALYZE (actual):**
```
-> Filter: (trim(coreiq_ir_websites.ticker) <> '')  (cost=317 rows=278) (actual time=6.47..521 rows=290 loops=1)
    -> Table scan on coreiq_ir_websites  (cost=317 rows=278) (actual time=6.46..521 rows=290 loops=1)

```

### IR lookup — company_ir_websites (all rows)

**Query:**
```sql
SELECT *
FROM company_ir_websites
WHERE ticker IS NOT NULL
  AND TRIM(ticker) != ''
```

**EXPLAIN error:** (1146, "Table 'coresight_market_data_stg.company_ir_websites' doesn't exist")


**EXPLAIN ANALYZE error:** (1146, "Table 'coresight_market_data_stg.company_ir_websites' doesn't exist")


### 8. get_available_tickers


### get_available_tickers

**Query:**
```sql
Return distinct tickers + company names from both calendar tables.

        PERFORMANCE: Uses cached companies_map for name lookup — zero extra DB join.
        Two light index-only scans on both calendar tables (UNION deduplicates).
```

**EXPLAIN error:** (1064, "You have an error in your SQL syntax; check the manual that corresponds to your MySQL server version for the right syntax to use near 'Return distinct tickers + company names from both calendar tables.\n\n        PERF' at line 1")


**EXPLAIN ANALYZE error:** (1064, "You have an error in your SQL syntax; check the manual that corresponds to your MySQL server version for the right syntax to use near 'Return distinct tickers + company names from both calendar tables.\n\n        PERF' at line 1")
