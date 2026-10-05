# Screening — Capital IQ Baseline Comparison

> **How this document is structured:** three features are compared twice each — once as S&P Capital IQ documents it, once as the Coresight Market Data Portal implements it — followed by a gap register, the geography-label rename, and the numbers every claim rests on. Figures from the portal were measured against the staging database on 5 October 2026; figures from Capital IQ are quoted from S&P's own published guides.

---

## Executive summary

The portal reproduces **three** of Capital IQ's seven screen types and two of its three Key-Developments entry points. On the features that exist, the shapes match: a Company screen returns one row per company, a Key Developments screen returns one row per dated event, and Key Developments can also be attached to a Company screen as an extra column. The gaps are not in the shape but in the edges — Capital IQ offers keyword search over event text, a stock-return filter, a 10-category event taxonomy, AND/OR logic between criteria, and four screen types the portal shows as *coming soon*.

Two differences matter more than the feature checklist. First, the portal's event universe (4,660 tickers, 215,736 events back to January 2016) is far wider than its curated company universe (558 companies), so a Key Developments screen and a Company screen do not cover the same market. Second, where Capital IQ **drops** a company that fails a criterion, the portal **annotates** it with N/A for financial and event criteria and drops it only for industry and country. Both behaviours are defensible; they produce different counts from identical criteria, which is the single most common source of "Capital IQ says 40, we say 62".

---

## Why this document exists

The portal is built against a Capital IQ baseline: when a business user asks whether a screen is "right", the implicit standard is what Capital IQ would return. That comparison has so far lived in conversation, which has three costs:

- **Counts get mistrusted instead of explained.** A screen that returns more names than Capital IQ is usually correct — it annotates rather than drops. Without this written down, the difference reads as a bug.
- **Scope arguments restart.** "Key Developments screening" means three different things (a screen type, a criterion inside a company screen, and the company-level event feed). Each conversation re-establishes which one is meant.
- **Gaps are rediscovered.** Keyword search and the stock-return filter have come up more than once as new findings. They are documented baseline features we have not built.

This document is the written baseline. It is deliberately specific about file names, table names and counts so that any claim can be checked rather than argued.

---

## Terminology — what each name refers to

The three features have been referred to by several names, including garbled dictations such as *KDOF*, *KIDEL* and *Kido*. For this document:

| Name used here | Where it lives | What a result row is |
|---|---|---|
| **Company screening** | Screening → Screen For → **Companies** | One company |
| **Key Developments screen** | Screening → Screen For → **Key Devs** | One dated event |
| **Key Developments criterion** | Any screen → criteria palette → **Key Developments by Category** | A column added to the row the host screen produces |

"Key Devs" is S&P's own abbreviation, not a Coresight one: the Capital IQ platform user guide lists the screen types as "Companies, Transactions, People, Fixed Income, Equities and Key Devs".

---

## Side-by-side summary

| | Capital IQ | Coresight portal |
|---|---|---|
| **Screen types offered** | Companies, Equities, Fixed Income, Key Developments, People, Transactions, Projects/Portfolios | Companies, Key Devs, People built; Equities, Fixed Income, Transactions, Projects/Portfolios show "coming soon" |
| **Criteria breadth** | "1000+ criteria data points: industry, geography, business description, financials, estimates, ratios, key devs, firm type, etc." | Four criterion families: Industry Classifications, Geographic Locations, Financial Information, Key Developments by Category |
| **Criterion logic** | AND/OR selectable per criterion; criteria can be reordered and re-run | AND only; order is display order and does not change the result |
| **Failing a criterion** | Narrows the universe | Industry and country narrow it; financial and event criteria annotate with N/A |
| **Switching screen type** | "Easily navigate between different screener types while keeping filter criteria" | Criteria are shared across Companies / Key Devs / People and survive the switch |
| **Event taxonomy** | "10 Major Categories and over 100+ Sub Types"; 124 Key Developments types in total | 23 `event_category` values, 129 `event_subtype` values |
| **Event sourcing** | "over 20,000 news sources including press releases, regulatory filings, company web sites, web mining and call transcripts" | SEC Form 8-K via `edgartools`, Nasdaq earnings calendar, Alpha Vantage estimates, and a whitelisted set of ~50 news wires |
| **Diagnostics** | "Where is my company?" checks why a given name is in or out | A per-criterion trace of rows in / rows out / rows with data |
| **Saving and reuse** | Saved screens, display templates, Save to List, screening alerts by email | Saved criteria (`coreiq_saved_criteria`), watchlists (`coreiq_watchlists`), Excel export |

---

## 1. Company screening — how Capital IQ does it

S&P's screening guide describes the step as defining a universe: "Define the universe of companies you want to see with criteria from categories such as Company Details, Financial Information, Corporate Actions, Company Ownership, Investment Criteria, Fixed Income Securities, Person Details, and Transaction Details."

The mechanics worth comparing against:

- **Industry and geography are trees, not lists.** "When screening on industry and/or geography classifications, click into the link to access our trees… Click the plus sign to the left of any industry/geography node to expand the tree." A parent node selects its children.
- **Multiple selections are OR; criteria are AND or OR by choice.** "Multiple industries/geographies can be selected in one screen and will be treated with OR logic." Separately: "Screening allows you to add new criteria to your screen as an AND/OR search by selecting certain radio buttons when adding criteria."
- **Primary-only is a toggle.** "You can specify Primary Industry/Headquarters by checking the box below the tree. This will be selected by default."
- **Geography is split across two criteria.** Capital IQ carries both `Geographical Locations` and `Country of Incorporation`. S&P does not publish a definition distinguishing them; a Cambridge Judge Business School library note observes only that they "would appear to cover broadly similar areas" and suggests running them separately to see which fits.
- **Financial criteria carry a period and an operator.** "You can select time periods from a list of relative and absolute dates, such as LTM and LTM-1 as well as FY 2011 and FQ12012… designate an operator (greater than, less than, between, or equals), enter a value, and click Add Criteria."
- **Display is separate from filtering.** A metric can be added as "Display Only", and display columns can be saved to a template. Results can be grouped "by a category such as sector, primary industry, geographic region or country".
- **Counts are read off the last criterion.** "The number of results associated with your last criterion reflects your most targeted search." Criteria can be reordered, which changes the intermediate counts shown.

---

## 2. Company screening — how the portal does it

**Entry point.** Screening → Screen For → **Companies** (`app/pages/screening.py`, `_render_results`).

**Universe.** `coreiq_companies`, the curated Coresight master — **558 companies** as of 5 October 2026. All 558 carry a Coresight industry; 516 (92.5%) carry a country of incorporation. A watchlist narrows the universe before any criterion runs.

**Criteria and what each one does.** This is the sharpest divergence from Capital IQ:

| Criterion | Behaviour | Source |
|---|---|---|
| Industry Classifications | **Filters** — non-matching companies leave the list | `sector` on the in-memory universe, 0 DB calls |
| Geographic Locations | **Filters** | `country_of_incorporation` on the in-memory universe, 0 DB calls |
| Financial Information | **Annotates** — a company missing the metric shows N/A and stays | `coreiq_av_financials_*` / `coreiq_yf_financials_*` |
| Key Developments by Category | **Annotates** | `coreiq_company_events` |

The split is explicit in code: `FILTERING_CRITERION_TYPES = frozenset({"industry", "geography"})`. Everything else is left-merged onto the untouched universe, so missing data reads as N/A rather than as exclusion. Identity for the filtering step is `(ticker, company_name)`, never ticker alone, because tickers collide — TSCO is both Tractor Supply and Tesco.

**Financial criteria** accept a statement (Income Statement, Balance Sheet, Cash Flow), a metric, an operator including Between, a period type (FY / CQ / FQ) with year and quarter, or a trailing window of 4, 8 or 12 quarters. Values are entered in $mm and scaled by 1,000,000 before comparison.

**Reliability guarantee.** A single failing criterion cannot break a screen: on any exception the criterion degrades to a no-op, the universe is preserved, and its column shows N/A — the same as missing data. Errors are logged, not surfaced as a broken page.

**Output.** An AG Grid table with one row per company, Excel export, and the option to save the result as a watchlist or the criteria as a saved screen.

**Against the baseline.** Missing: the industry/geography trees (the portal uses flat multi-selects), AND/OR choice between criteria, criterion reordering, display-only metrics as a first-class concept, saved display templates, grouped results, email screening alerts, and the "Where is my company?" diagnostic. Present and arguably better: the per-criterion trace, and the annotate-don't-drop behaviour that keeps a company visible when a data point is simply absent.

---

## 3. Key Developments screen — how Capital IQ does it

Capital IQ treats it as a screen type of its own, documented in one line: "Filter Key Developments by category, type, keyword, date, and stock return." The Excel plug-in guide adds the taxonomy size: "Users can filter Key Developments across 10 Major Categories and over 100+ Sub Types within a specified time-frame."

Four documented capabilities to measure against:

1. **Category and type** — the two-level taxonomy, 124 types in total per the platform brochure (including 31 Events), with 36 further types behind the LCD add-on.
2. **Keyword** — free-text search across the event text.
3. **Date** — relative or absolute windows.
4. **Stock return** — filtering events by the share-price move around them. This is the one capability with no analogue anywhere in the portal.

Each event record carries "announced date, entered date, modified date, headline, situation summary, type, source, company role, and other identifiers", sourced from "over 20,000 news sources including press releases, regulatory filings, company web sites, web mining and call transcripts". **Company role** is worth noting: Capital IQ states each company's part in the event, which is what makes an M&A event unambiguous about who is acquiring whom.

---

## 4. Key Developments screen — how the portal does it

**Entry point.** Screening → Screen For → **Key Devs** (`_render_keydevs_results`).

**Universe.** Every ticker with an event, not the Coresight master — **4,660 tickers** across **215,736 events** dated 1 January 2016 to 3 October 2026. This is deliberate and not a toggle: bounding the screen to the curated companies hid 9,741 of 21,034 M&A events (46%).

**Row grain.** One row per event. The per-company event detail column is skipped entirely in this mode, because every row already *is* an event.

**Columns.** Key Developments By Date · Key Developments by Type · Company Name(s) · Key Development Headline · Summary · Key Development Sources · Source Reference. When **M&A Activity** is among the selected categories, five deal columns are added (acquirer, target, deal type, transaction value, USD value); they are withheld otherwise, since they are NULL on ~99% of non-M&A rows.

**Company labels come from a join.** `coreiq_company_events` holds only a ticker, so names and industries are joined from `coreiq_companies`, `coreiq_av_companies_all`, `coreiq_sec_companies_all` and the non-SEC master. Measured on M&A all-history: 98.7% of rows get a Coresight industry. Alpha Vantage is deliberately excluded as an industry fallback — it is a different taxonomy, and mixing it in would offer filter values the data does not use.

**Date windows.** Presets of Last 7 / 30 / 90 days through to **All History**, or an explicit start/end range. Multiple event criteria merge into the widest window that covers all of them.

**Paging and export.** 500 events per page by keyset pagination; when the screen is unbounded the first page is stratified at 8 events per industry so that no industry is invisible behind whatever is busy this week. Excel export walks the same keyset pages at 10,000 rows each with a 500,000-row runaway guard. The historical `LIMIT 2000` that silently truncated "All History" to the newest ~17 days is gone, and the header now shows the honest total from a separate COUNT.

**Against the baseline.** Missing: keyword search, the stock-return filter, and company role. The taxonomy is comparable in depth (129 subtypes against "100+") but flatter and differently grouped (23 categories against 10). Sourcing is narrower by design — SEC 8-K, Nasdaq, Alpha Vantage and a whitelist of about 50 wires, against Capital IQ's 20,000 sources.

---

## 5. Key Developments as a criterion — how Capital IQ does it

Capital IQ exposes the same data a second way, as qualitative criteria inside another screen: "Screen for qualitative information such as Corporate Actions (e.g., shareholder activity and deal history) and Key Developments (e.g., executive changes, share repurchases, and positive/negative earnings announcements)." The platform user guide lists "key devs" among the criteria available to a company screen.

In that mode the result row stays a company; the event is the test the company has to pass, and any event detail the user wants in the output is added through display columns. The Excel template for this path is explicitly bounded: "Allows screening for up to 15 key dev types on a list of companies with flexible ticker entry, date, and key dev type options."

---

## 6. Key Developments as a criterion — how the portal does it

**Entry point.** Any screen's criteria palette → **Key Developments by Category** (`apply_keydevs_criterion`).

**Behaviour.** Annotating, not filtering. The criterion resolves which companies have a matching event and writes a detail column; companies with no match keep their row and show N/A. This is the opposite of Capital IQ, where the criterion narrows the universe.

**What the column contains.** Up to **10 events per company**, newest first, each rendered as `M/D/YYYY (subtype)` followed by the first 120 characters of the headline. It is never a bare "matched" flag — the point of the criterion is the developments themselves.

**Two execution paths.** With the detail column (a Company or People screen) a single windowed query ranks events per ticker and joins back by primary key for the headline text. Without it (Key Devs mode, where the column is never shown) the query degrades to `SELECT DISTINCT ticker`, which is index-served and roughly four times faster — 0.36s against 1.46s on the measured case, because the cost here is one network packet per row, not bytes.

**Against the baseline.** The 10-event cap is ours, not Capital IQ's; the 15-type cap is Capital IQ's, not ours. Capital IQ's criterion filters and ours annotates. Neither carries a keyword filter in this mode, and neither exposes stock return.

---

## Gap register

| # | Gap against the Capital IQ baseline | Where it bites | Size |
|---|---|---|---|
| 1 | No keyword search over event text | Any thematic event hunt ("tariff", "store closure") is impossible without exporting first | Medium — needs a FULLTEXT index on headline/situation |
| 2 | No stock-return filter on events | Cannot isolate the events the market actually reacted to | Large — needs event dates joined to price series |
| 3 | No company role on events | M&A rows cannot say reliably who acquired whom; the existing `ma_acquirer` / `ma_target` columns are regex-derived and unreliable | Large — a data-side fix, not a UI one |
| 4 | Event taxonomy is flat (23 categories) vs 10 major categories + subtypes | Users cannot pick "all transaction events" in one click | Small — a grouping layer over existing values |
| 5 | AND only; no OR between criteria | "US *or* Canada apparel" needs two screens | Medium |
| 6 | No industry/geography trees | Selecting a sector does not select its sub-industries | Medium |
| 7 | Four screen types unbuilt (Equities, Fixed Income, Transactions, Projects/Portfolios) | Transaction screening is the most-requested of the four | Large |
| 8 | No screening alerts by email | Saved screens must be re-run by hand | Medium — the earnings-alert mailer already exists as a pattern |
| 9 | No "Where is my company?" diagnostic | Users cannot self-serve "why is X missing?" | Small — the per-criterion trace already has the data |
| 10 | Country of incorporation only; no operations or HQ geography | A company incorporated in Ireland but trading in the US answers to neither question cleanly | Medium — needs an HQ column in the master |

---

## The geography label rename

The business request was to rename **Geographic Locations** to **Country of Incorporation** so the label states what it measures. The data already does: the criterion filters on `coreiq_companies.country_of_incorporation`, and the form's own help text already reads "Filter companies by country of incorporation". Only the visible labels are wrong.

**Change these eight strings.** In `app/pages/screening.py`: the criteria menu entry (line 597), the inline criterion-type picker and its branch (lines 2365 and 2380, where the string is the shorter `"Geography"`), the form expander title (line 3694), the applied-criteria chip label (line 1908), the empty-state help text (line 5351), and the module docstring (line 16). In `app/data/screening_service.py`: the three summary strings in `build_geography_summary` (lines 6647–6650).

**Do not change** the internal key `"geography"`. It is the criterion type stored in saved criteria and used by the filter/annotate split; renaming it would break every screen already saved in `coreiq_saved_criteria`.

**Decide separately** on the results column, which the grid and the Excel export head as `Country`. Renaming it to `Country of Incorporation` is consistent but changes export headers, so confirm with whoever consumes those files first.

One consequence to accept knowingly: Capital IQ carries *both* `Geographical Locations` and `Country of Incorporation`. After the rename the portal has only the incorporation criterion, correctly labelled — and no filter on where a company operates or is headquartered. That is gap 10 above, not a reason to keep a misleading label.

---

## Numbers behind this document

Measured against the staging database on 5 October 2026. These move; re-measure before quoting them elsewhere.

| Measure | Value |
|---|---|
| Companies in `coreiq_companies` | 558 |
| …with a country of incorporation | 516 (92.5%) |
| …with a Coresight industry | 558 (100%) |
| Distinct countries of incorporation | 25 |
| Events in `coreiq_company_events` | 215,736 |
| Distinct tickers with events | 4,660 |
| Event categories | 23 |
| Event subtypes | 129 |
| Event date range | 2016-01-01 → 2026-10-03 |
| Events per page, Key Devs mode | 500 (keyset) |
| Events per industry, unbounded first page | 8 |
| Events per company, criterion detail column | 10 |
| Excel export page size / runaway guard | 10,000 / 500,000 rows |

---

## Sources

Capital IQ statements in this document are quoted from the following published documents. Statements about the portal come from its source code and from the staging database, cited inline.

| Source | Used for |
|---|---|
| [S&P Capital IQ — Screening Quick Reference Guide](https://www.wu.ac.at/fileadmin/wu/s/library/databases_info_image/SP_CIQ_Screening_-_Quick_Reference_Guide.pdf) (S&P Capital IQ, 2014) | Screen types; company screening criteria categories; industry/geography trees and OR logic; AND/OR radio buttons; financial periods and operators; display columns; grouping; saved screens and alerts; the Key Developments filter line |
| [S&P Capital IQ Platform User Guide](https://www.cbs.dk/sites/default/files/2025-11/sp_capital_iq_-_user_guide.pdf) (S&P Global Market Intelligence) | "Companies, Transactions, People, Fixed Income, Equities and Key Devs"; "1000+ criteria data points"; keeping criteria across screener types; "Where is my company?" |
| [S&P Capital IQ Excel Plug-in Template Guide](https://www.wu.ac.at/fileadmin/wu/s/library/databases_info_image/S_P_Capital_IQ_Excel_Plug-in_Template_Guide_.pdf) (August 2017) | "10 Major Categories and over 100+ Sub Types"; the 15-key-dev-type screening template |
| [The S&P Capital IQ Platform — brochure](https://s38328.pcdn.co/wp-content/uploads/sp-global-capital-iq-platform-brochure.pdf) (S&P Global Market Intelligence) | "124 Key Developments types (including 31 Events) utilizing over 20,000 news sources" |
| [Capital IQ Key Developments (WRDS)](https://guides.library.ualberta.ca/az/capital-iq-key-developments-wrds) (University of Alberta Library) | The per-event field list including company role |
| [Using the screening tool on Capital IQ](https://bizlib247.wordpress.com/2017/05/31/company-screening-in-capital-iq/) (Cambridge Judge Business School Information Centre) | `Geographical Locations` and `Country of Incorporation` "cover broadly similar areas" |
| [Screening — Capital IQ](https://libguides.cbs.dk/c.php?g=697687&p=5009935) (Copenhagen Business School library guide) | Independent confirmation of the screen-type list and the purpose of a Key Developments screen |

S&P does not publish a definition distinguishing `Geographical Locations` from `Country of Incorporation`, nor a public enumeration of the 124 Key Developments types. Both are stated as unknown above rather than filled in.

---

## Related pages

- [Screening — Business User Guide](./screening.md) — how to drive the screens described here
- [Market Data](./market-data.md) — the per-company view a screen result links into
