# Segment data: what the data team needs to fix in `coreiq_filing_metrics_v5`

From the MIP team, 2026-10-08. Every Segments-tab number for every company was
checked against the company's own SEC XBRL, in both directions: every number shown
must be filed, and every segment fact filed must be shown. The app now accounts
for every fact it can see in the database. What remains needs a change in the data
itself, below in order of impact. Counts are filled in from the final run.

## 1. Store the member's element name, not only its label (highest impact)

**Problem.** A dimensioned row keeps the axis (`dimension`) and a text label for the
member (`dimension_member_label`), but not the member element itself (e.g.
`country:US`, `dltr:DollarTreeMember`). The label is taken from whichever table
of the filing used the member, so the same member arrives with another table's
label:

| Company | Member element | Label stored | Effect |
|---|---|---|---|
| Mondelez | `country:US` (revenue 9,343) | "U.S. Plans" (pension table) | US revenue dropped as a pension category |
| Dollar Tree | `dltr:DollarTreeMember` (FY2023-26) | "Cost of sales" | Dollar Tree segment named "Cost of sales" |
| C3.ai, Workday, Palo Alto, CXM, RealReal | revenue members | "Cost of subscription", "Cost of product revenue" | cost-table label on revenue |
| Fresh Del Monte | `fdp:Product3Member` | "Totals" | a segment named "Totals" |
| Farmer Bros | product member | "Net sales by product category" | table heading as a segment |

**Cause, found in the filings.** A company's label file gives each member several
labels by role. Dollar Tree's `dltr:DollarTreeMember` has the standard label
"Dollar Tree [Member]", the documentation "Dollar Tree" — and the *terse* label
"Cost of sales", which it uses in its expense table. The extraction stored the
terse label.

**Ask.**
1. Store the member's **standard label** (`http://www.xbrl.org/2003/role/label`,
   without " [Member]"), never the terse or verbose one.
2. Also store the member element name per axis, e.g. a column
   `dimension_members` = `{"srt:StatementGeographicalAxis": "country:US"}`. With
   it every member is identified exactly, whatever any label says.
3. Write the axis heading in `full_dimension_label` from the axis, not a member:
   MarineMax rows read "Consolidation Items: Operating Segments, Product
   Manufacturing [Member]: Retail Operations".

## 2. 10-Q rows without a filing date, or dated before their own period

**Problem.** Sampled over 10 companies: 1,035 of 130,393 10-Q segment rows have
`filing_date` NULL, and 4,335 are dated before the period they report (Lowe's Q1
FY2022 row "filed" 2021-05-27). The tab shows the newest filing's figure for each
period; without a correct date a restatement cannot be told from the original.
`doc_type` is also shifted one quarter (ADM's quarter ending 2024-06-30 is stored
as `10-Q-Q3`).

**Ask.** Write `filing_date` from the filing's own acceptance date and `doc_type`
from the filing's fiscal period (`dei:DocumentFiscalPeriodFocus`), and backfill.

## 3. Filings, or their segment facts, missing

**Problem.** SEC facts whose value is nowhere in the table for that period. Two
shapes:
- the whole 10-Q/10-K is absent (Avnet quarter ending 2024-03-30, Cheesecake
  Factory Q3 2025);
- the filing is present but only some tables were extracted (Arrow's 10-Q for the
  quarter ending 2024-06-29: equity-statement rows only, no segment note).

**Size (all 402 companies, every 10-K since FY2012 and 10-Q since 2018).**
1,091 annual and 8,889 quarterly SEC segment facts have no matching value in the
table for that period; 20 annual and 68 quarterly cells show a figure a newer
filing restated, because the table lacks the newer filing.

**Ask.** For every company in `coreiq_companies`, every 10-K and 10-Q since 2012:
load all dimensioned facts of the filing, including the segment note (ASC 280),
and the comparative periods each filing restates. Every affected company and
period is in the attached workbook, sheet "Open items", filter "Who acts" = Data
team.

## 4. Check after every load

`scripts/verify_segments_vs_sec.py` (MIP repo) checks a company against SEC in both
directions in about a minute, downloading into a temporary folder that is deleted
afterwards:

    .venv/bin/python scripts/verify_segments_vs_sec.py TICKER [TICKER ...]
    .venv/bin/python scripts/verify_segments_vs_sec.py --all --out results.jsonl

A company is complete when its result has no `missing_not_in_db` and no
`missing_app_gap` lines. Running it on each newly loaded company gives the
same guarantee for new companies.

## Proposed plan (for the data team to confirm)

| Step | Owner | Done when |
|---|---|---|
| 1. Standard labels; member element column; axis headings — backfill all 10-K/10-Q rows | Data team | column populated for every dimensioned row |
| 2. Fix `filing_date` / `doc_type` writer, backfill 10-Qs | Data team | no NULL or pre-period dates; doc_type matches the filing |
| 3. Load missing filings and segment notes (attached list) | Data team | `missing_not_in_db` empty |
| 4. App names members from the element column | MIP | label-error table above clears |
| 5. Run the verifier on all companies; repeat on each new company | Both | every company complete |
