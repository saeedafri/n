# People Screening — Bug Fixes, De-duplication, and Career-Data Research

**Date:** 2026-10-07
**Status:** Implemented in testing (CapIQReplacement) and synced to STG (market-data-stg), not committed
**Scope:** People Screening only (`/screening` → Screen For: People)
**Files:** `app/data/people_service.py`, `app/data/test_people_service.py`,
`app/data/screening_config.py` (People display columns only),
`app/pages/screening.py` (`_format_people_display` only)

Follows `2026-10-06-screening-people-criteria-design.md`. The bug list it fixes is the
"Bugs" sheet of `docs/people-screening/People_Screening_Data_Inventory_2026-10-06.xlsx`.

---

## 1. What was wrong (measured on STG, 2026-10-06/07)

| ID | Bug | Rows | Root cause |
|---|---|---|---|
| APP-1 | Yahoo Total Pay shown and filtered as US dollars | 155 officers | `totalPay` is in `info.financialCurrency` (KRW, JPY, EUR…); the code never read it |
| APP-2 | Officers sharing a surname merged (Arnault sons, Yanai family) | 14 | `person_key` = first + last word, and Yahoo puts "Mr." first → `mr\|arnault` |
| APP-3 | Exercised / Unexercised Value shown as $0 | 497 / 498 | Yahoo sends 0 when it has no value |
| DATA-1 | Pay breakdown adds to more than the total | 141 on screen | Extractor slides a value into an adjacent blank cell (no dash in the proxy), so one figure sits in two columns — Apple 2023 stock award also stored as Bonus |
| DATA-2 | Total below salary | 2 | Total split by a stray space ("5,6 90,250") or a footnote glued to it |
| DATA-3 | Footnote text read as a pay row | 4 | Same number in every column (Zebra Kogl, Instacart Sharma) |
| — | PepsiCo totals wrong for every executive-year | 58 | PepsiCo's table splits non-equity pay into two sub-columns plus their sum; the extractor took the sum as the total (Laguarta 2024: $6,766,500 stored, $28,814,759 filed) |
| — | Same person still shown twice | 45 pairs + glued titles | Title words glued to names ("Brian T. Olsavsky SVP and", "Franck J. Moison Retired"), nicknames (Mike / R. Michael Mohan), married names (Karalyn Smith / Yearout) |
| — | Email column always blank | all | Read from `coreiq_people_contacts`, which does not exist. Removed (it had been re-added by a sync after the 2026-10-06 removal) |

No DB write was made. Everything is fixed where the app reads the data
(`_build_people_universe`), so the data team's tables are untouched.

## 2. How duplicates are removed (automatic, no user action)

`coreiq_executives_compensation` stores one executive-year up to 6 times: each proxy
restates the 2 prior years, and PRE 14A / DEF 14A are drafts of the same proxy. The
frame keeps exactly one row per (company, person, year, source), in three steps:

1. **One identity per spelling** — `person_key()` keeps every full name word, drops
   middle initials, honorifics (Mr./Ms./Dr.), suffixes (Jr., II, Ph.D.), and title words
   the extractor glued on (SVP, Retired, CEO/President…). `clean_person_name()` strips
   the same junk for display.
2. **One identity per person** — `_merge_name_variants()`: two spellings filed for the
   same company and year with the **same pay** (to $1,000) that share a first name **or**
   a surname are one person. Checked on every such pair in STG (45) — all one person.
   Equal pay alone is never enough: Apple's Sewell/Riccio/Cue and Amazon's
   Olsavsky/Zapolsky are paid identically and share no name, so they stay apart.
3. **One row per person-year** — the best copy wins, in this order: pay adds up →
   has a title → extractor confidence → larger total (truncations) → DEF over PRE →
   earliest filing (the contemporaneous title). A blank title is borrowed from the best
   titled copy. The fullest spelling of the name is used in every year.

Yahoo needs no restatement handling (one snapshot per company, latest row only).

## 3. How broken pay is handled (`_repair_pay`)

Only what is provable from the row itself:

- **Footnote rows** (the total equals ≥ 2 pay columns) are dropped.
- **Smeared values** (one figure in two columns and the parts overshoot the total):
  the rightmost copy is the real column, the others are cleared — only if the row then
  adds up.
- **Subtotal taken as total**: the last amount printed in the filed row (`raw_row`) is
  the Total column. It replaces the stored total only when it makes the row add up and
  the stored one does not.
- "Adds up" = total ≥ every single part, and parts ≤ total × 1.05 + $1,000. The 5%
  allows a **negative** pension change, which the table has no column for
  (Caleres 2022, −$51,306).
- If no copy adds up, the total is left **blank**, the parts are kept. 10 person-years.

## 4. Currency (`_yf_records`)

`total_pay` is converted to USD with the year-end rate of the officer's fiscal year
from `coreiq_av_forex_daily` (USD → KRW, JPY, EUR, GBP, CNY, HKD, CHF, SEK, AUD, CAD).
The figure as filed and its currency are kept as `total_pay_local` / `pay_currency`
and shown as **Total Pay (Local)** and **Pay Currency**. MYR and SGD have no rate on
file (5 officers): their local figure shows, Total Pay is blank. Exercised/Unexercised
values are converted the same way; Yahoo's 0 is treated as no value.

## 5. Cache

The materialized frame is now `people_universe_v2`. The disk copy is keyed on the
source tables only, so the new name is what stops a deploy from serving the old,
buggy snapshot. Cold build 7–10 s once (warmed on boot), warm read 0.05 s, filters
15–38 ms as before.

## 6. Result (STG data, 2026-10-07)

| | Before | After |
|---|---|---|
| People rows | 6,727 | 6,609 (6,093 SEC + 516 YF) |
| Yahoo officers kept | 502 of 516 | 516 of 516 |
| Parts > total + $1k | 141 | 17 (all ≤ 4.1%, negative pension) |
| Total below salary | 2 | 0 |
| Footnote rows | 4 | 0 |
| Totals withheld | 0 (wrong ones shown) | 10 |
| Surviving duplicate pairs | 45 + glued names | 0 |
| CEO+CFO, Total Comp > $10mm | 670 / 89 companies | 678 / 89 companies |

The +8 is fully explained: recovered PepsiCo totals now pass the threshold, and nine
non-US executives that passed only because yen/won were read as dollars drop out
(Tadashi Yanai ¥550mm ≈ $3.5mm).

## 7. Testing

- `app/data/test_people_service.py`: 18 checks, all pass, in both repos. New:
  honorifics/title words, nickname merge on equal pay (and Olsavsky/Zapolsky,
  Dillard siblings kept apart), broken pay rows (footnote, smear, below-salary, negative
  pension, PepsiCo subtotal), raw-row total parsing, Yahoo USD conversion + zeros.
- UI, both repos (testing on :8505, STG on :8506, staging DB): People regression flow
  (CEO+CFO → Total Comp > $10mm → Excel, 21 columns × 678 rows, no Email); Name contains
  "Arnault" (4 family members, USD + EUR + currency); "Laguarta" (PepsiCo totals
  $23.9mm–$33.9mm); "Mohan" (one row per year, one spelling). Only console errors are
  Streamlit's pre-existing `/_stcore/health` 404s.

## 8. Executive career data — research (not built)

Asked: executive, previous company/title, new company/title, effective date, change
type, source, current title, start/end date, tenure, compensation, year, amount,
currency. Tested on real filings with edgartools **5.61** (the installed 5.16 cannot
read filing documents — `primary_documents` comes back empty; the data team must
upgrade).

| Field | Best free source | Structured? |
|---|---|---|
| Executive, new title, start date | Form 3 (`reporting_owners`, `officer_title`, period) | Yes |
| Previous companies / titles | Forms 3/4/5 history under the person's owner CIK; 8-K 5.02 bio | Form 3/4 yes (SEC filers only); bio needs an LLM |
| Effective date, change type | 8-K Item 5.02 | Date by regex (56%); type needs an LLM |
| End date | Departure 8-K; last Form 4 | Text / proxy |
| Tenure | Derived from start/end; 10-K "served as X since …" | Derived |
| Compensation, year, amount | DEF 14A Summary Compensation Table (already ingested); 8-K offer terms; Pay-vs-Performance XBRL (CEO only) | SCT yes; offer terms regex 14% |
| Currency | XBRL unit; Yahoo `financialCurrency` | Yes |
| Source | Accession number / URL | Yes |
| Non-US | Yahoo (current officers only); UK Companies House (free key; directors with appointed/resigned dates); Wikidata (hint only, often stale) | Mixed |

Example: Best Buy's Anne Bramman — Form 3 gives EVP, CFO from 2026-08-19; her owner
CIK's Form 3/4 history rebuilds Nordstrom CFO 2017-06 → 2022-08 and Avery Dennison
CFO 2015 → 2017, matching her 8-K bio.

Our own `coreiq_company_events` already holds 6,457 Item 5.02 texts (`situation`,
avg 3.1 KB) with sec.gov `source_ref` — re-parseable without re-fetching. Its subtype
labels are unreliable (3 of 6 sampled "CEO" rows were not CEO changes).

**Recommended pipeline (data team, offline — the app never calls EDGAR live):**
nightly Forms 3/4/5 → person + role-history tables keyed on owner CIK; LLM pass with a
strict JSON schema over Item 5.02 text, cross-checked against the Form 3 date; annual
10-K executive-officer bios; Yahoo + Companies House for non-US. The app would then read
those tables into People Screening like the compensation table today.

## 9. Rollout

Testing and STG working trees updated; nothing committed. After deploy no manual cache
step is needed (`_v2`). Old `people_universe.*` files in the cache dir are orphaned and
can be deleted.
