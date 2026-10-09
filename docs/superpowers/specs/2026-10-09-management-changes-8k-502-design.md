# Management Changes from Form 8-K Item 5.02 — Design & Feasibility

Date: 2026-10-09 · Status: prototype, verified locally against STG DB (read-only) · Scope: People Screening

Source brief: `docs/MIPMeetingSummary.md` §8–12, §29.3, Phase 3–4.

---

## 1. Where SEC registrants report management changes

| Filing | Where in it | What it gives People Screening | Structured? |
|---|---|---|---|
| **Form 8-K, Item 5.02** | "Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers; Compensatory Arrangements of Certain Officers" — filed within 4 business days | The **event**: who left / joined, titles, announcement + effective dates, successor, reason (sometimes), and for (c) the newcomer's age, prior jobs and pay terms | Free text (this spec) |
| Form 8-K, Items 7.01 / 8.01 + EX-99.1 press release | Voluntary announcements | Moves of executives **not covered by 5.02** (e.g. Chief Merchandising Officer, segment presidents) | Free text |
| **DEF 14A** proxy | Summary Compensation Table; director & executive-officer bios (S-K Item 401) | Pay by year (already ingested → `coreiq_executives_compensation`); "has served as X since YYYY" = role start | Table + text |
| **10-K** Part I "Information about our Executive Officers" / Part III Item 10 | Annual roster (often incorporated from the proxy) | Who held which title each year → role history baseline | Text/table |
| **Forms 3 / 4 / 5** (Section 16) | XML: `isOfficer`, `isDirector`, `officerTitle` | Form 3 within 10 days of becoming an officer/director = an **appointment signal with no regex**; Form 4 titles track title history | **XML — fully structured** |
| Form 6-K (foreign private issuers) | No item numbers | Management changes of FPIs (we already store 1,401 rows, 35 tickers) | Free text |
| Form 8-K Item 5.07 | Vote results | Directors elected **at the annual meeting** (these are not 5.02(d)) | Table |

### Item 5.02 sub-items — only four are changes

| Sub-item | Event | Change? |
|---|---|---|
| (a) | Director resigns / refuses re-election over a **disagreement** | yes |
| (b) | CEO, President, CFO, CAO, COO (or equivalent) or a director retires, resigns, is terminated, or won't stand for re-election | yes |
| (c) | New CEO / President / CFO / CAO / COO appointed (with age, bio, pay terms) | yes |
| (d) | New director elected **outside** the annual meeting | yes |
| (e) | Compensatory plan or arrangement adopted/amended (NEOs) | **no** |
| (f) | Late salary/bonus figure for the proxy | **no** |

**Coverage limit (important for the manager's "chief merchandising officer" example):** 5.02(b)/(c)
only *requires* the principal executive/financial/accounting/operating officers and the President.
Other C-suite moves appear only when the company volunteers them (often still under 5.02, otherwise
7.01/8.01 press releases). A complete people-move feed therefore needs 5.02 **plus** press releases.

---

## 2. What we have today in the key-events table

`coreiq_company_events`, `source='SEC EDGAR 8-K'`, headline `… (8-K Item 5.02)`, written by
`scripts/generate_key_developments_v4.py` (full item text in `situation`, no LLM).

| Stored subtype | Rows | Compensation-only (5.02(e)/(f)) | Text empty | Label confirmed by extraction* |
|---|---:|---:|---:|---:|
| Executive Changes - CEO | 2,696 | 847 | 24 | ~21 % |
| Board Director Change | 2,594 | 787 | 199 | ~52 % |
| Executive Changes - Other C-Suite | 1,771 | 286 | 14 | — |
| Executive Changes - CFO | 1,433 | 288 | 13 | ~38 % |
| Executive Changes - COO | 436 | 93 | 11 | ~28 % |
| Executive/Board Changes - Other | 121 | 6 | 38 | — |
| **Total** | **9,051** (414 tickers, 2016-01 → 2026-10) | **2,307 (25 %)** | 299 | |

\* lower bound — the extractor's own recall is not 100 %.

Findings:
1. **25 % of "management change" rows are not changes** — they are 5.02(e) pay arrangements
   (HSY retention RSUs, TSLA incentive plan, WWW severance amendment) labelled "Executive Changes - CEO/CFO".
2. **The subtype is keyword-based** (`classify_502_subtype`: first of "chief executive", "chief financial"…
   anywhere in the text). A new director whose bio says "former CFO of Starbucks" is filed as a CFO change (JACK 2026-09-21).
3. No person, title, date or successor is stored — only the raw text.
4. 299 rows have no usable text (ingestion stored only "(e)" or a pointer to another item) — a pipeline gap.

---

## 3. Solution — regex extraction over the text we already store

No download, no LLM, no DB write. `app/data/management_changes.py`:

```
coreiq_company_events (9,051 Item 5.02 rows, read-only, 1 query)
   └─ item_body()        strip header + heading, cut at next Item / safe-harbor / signatures,
                         remove term boilerplate ("until her successor is duly elected …")
   └─ split_sentences()  protects Mr./Inc./initials
   └─ known_people()     full names anchored on "Mr./Ms. Surname" usage (or an event phrase)
   └─ per sentence:      departure verbs  / appointment verbs  → which person (subject vs object)
                         title_after()  "appointed X as CFO", "resigned from her position as COO"
                         title_apposition()  "X, the Company's SVP, CAO and Controller," = current title
                         dates: "On <date>," = announced; "effective <date>" = effective; "effective
                         immediately"; "at the annual meeting"
                         succession: "X will succeed Y" links both rows
   └─ build_changes()    one row per person event; re-filings de-duplicated
   └─ materialized_or_build("management_changes_v1")  → disk, 1.15 MB, 9,449 rows
```

### Output columns

`ticker, filing_date, change (Appointment|Departure|Compensation), event_type (Appointment, Interim
Appointment, Promotion, Promotion / Role Change, Director Election, Succession, Resignation, Retirement,
Termination, Not Standing for Re-election, Departure, Death, Compensation Arrangement), person, title
(full string), titles (pipe-split — multiple titles kept), previous_title, role (same buckets as People
Screening + Board), is_board, notice_date, effective_date, effective_note, replaces, replaced_by,
reason, no_disagreement, confidence (High|Medium|Low), evidence (source sentence), source_ref, event_id`.

Announcement and effective dates are kept separate (brief §9.5). Filing year is never used as a start date (§11.2).

### Confidence tiers (hand-validated)

| Tier | Rule | Rows | Precision on blind samples |
|---|---|---:|---|
| **High** | title **and** timing (effective date / "immediately" / "at annual meeting") found | 3,940 | **45 / 45 events correct** (2 cosmetic title glitches, fixed) |
| Medium | title found, no timing | 1,966 | 19 / 20 after final fixes (11 / 15 before) |
| Low | no title | 1,236 | ~7 / 15 — review only |

Validation method: five rounds of 30–40 random events, each judged against its source sentence; every
error class found was fixed and pinned by a test (`app/data/test_management_changes.py`, 9 cases from real filings).

### Coverage

* 5,483 filings (61 %) produce a person event; 2,307 (25 %) are compensation-only and labelled as such;
  ~1,000 more had their event de-duplicated to an earlier filing of the same change (8-K/A, re-filing);
  ~300 are genuine misses; 299 have no text.
* 7,142 person events. Fill rates: title 83 %, effective timing 63 %, multi-title 1,471 events,
  successor link 407, previous title 176, departure reason 318.

---

## 4. UI — People Screening → "Management Changes"

Rendered under the People grid (`_render_management_changes` in `app/pages/screening.py`), scoped to the
same companies and the same People Attributes filters (name contains, title contains — matches current
**or** previous title — and Role). Controls: Change (Appointment / Departure), Include board seats,
Include lower-confidence (off → High only), Show source text (off → no filing link / source sentence,
per the manager's "don't expose the source" request). "Unknown" role is shown as "Other".
Excel export + AG Grid column filters with full value domains.

## 5. Performance (local, STG DB over VPN, recorded Playwright runs)

| Path | Time |
|---|---|
| Build from DB (no disk copy) | 43 s (27 s fetch of 9k long text rows + 12.5 s regex) — once per data change; boot warm-up (`_warm_people_screening`) pays it, not a user |
| Server restart, disk copy present | disk read 0.04 s; first Show Results 0.96 s |
| Warm Show Results (People + Management Changes) | 0.32–0.34 s |
| Section render per rerun | 11–20 ms |

Freshness: signature `MAX(event_id)` over `event_category IN ('Management Changes','Governance')`
(index range on `idx_cat_subtype`, never a full scan); a newer filing triggers a background rebuild
while the old copy keeps serving.

## 6. What the 5.02 data supports beyond the grid (measured)

* **Executive movement across companies:** 28 departure-then-appointment moves at a different ticker in
  the High tier — e.g. Amit Banati (KVUE CFO → MDLZ CFO, 2026), Anthony DiSilvestro (CPB → MAT → KDP CFO),
  David Denton (LOW CFO → NKE CFO). Name-only matching also produced "David Miller" TMUS GC → PLBY
  President (almost certainly two people) → cross-company matching needs age/year-born or bio evidence.
* **Tenure:** 151 person-roles have both appointment and departure inside 2016–2026; anything earlier
  needs proxy "since YYYY" text or 10-K rosters.

## 7. Known limits

* Regex long tail: each review round found new phrasing; High tier is safe, Low tier is not.
* External appointments ("resigned as director to become Executive Director of UNICEF") are occasionally
  recorded as an appointment at the filer.
* Names without any "Mr./Ms." reference fall back to a capitalised-name heuristic.
* 5.02 itself does not cover every executive (see §1); press releases are not parsed.

## 8. Rollout / recommendations

1. Prototype ships as app-side materialized data (no schema change, no DB write). Same files must be
   copied to market-data-stg by hand; frame name `management_changes_v1` (bump on any column change).
2. **Data team (Shashank):** move `extract_events()` into the ETL as a silver table
   (`coreiq_management_changes`, one row per person event) and fix the upstream subtype — tag 5.02(e)/(f)
   as "Compensation Arrangement" and derive CEO/CFO from the extracted role, not from keywords.
   Re-ingest the 299 empty-text filings.
3. Add Form 3 XML ingestion as the structured cross-check for appointments and `officerTitle` history.
4. Phase 4 (tenure, movement with pay deltas) only on the High tier, with person matching that uses
   YF age / year-born.

## 9. Testing

* `.venv/bin/python app/data/test_management_changes.py` — 9 real-filing cases (multi-title, dates,
  succession, promotion, re-election trap, term boilerplate, hypothetical termination, committee seat,
  compensation-only, short heading).
* `.venv/bin/python app/data/test_people_service.py` — still passes.
* Playwright (system Chrome, video recorded then deleted): People → Role CFO → Show Results →
  Management Changes 534 rows / 256 companies (High); with lower-confidence + source 789 / 315.

---

## 10. Round 2 — every non-pipeline item from the meeting brief (2026-10-09 PM)

| Brief item | What changed | Where |
|---|---|---|
| Combine Other + Unknown | Blank titles classify as **Other**; Unknown removed from the Role list; saved criteria naming Unknown map to Other | `screening_config.PEOPLE_ROLES`, `people_service.classify_role`, `apply_people_criterion` |
| Show only selected metrics | New **Compensation metrics to show** multiselect (default Total Compensation + Total Pay). The grid shows exactly those, plus the threshold metric if one is set | `_render_people_widgets`, `_render_people_results` |
| Multi-year obvious | "Compensation years (select one or more)", placeholder "each becomes a column", card summary lists every year (`FY 2024, 2025, 2026`) | `_render_people_widgets`, `build_people_summary` |
| Years as dynamic columns | `people_service.pivot_people_years`: one row per executive × metric, one column per year; identity from the latest year. Criteria pick WHO; the grid shows those people's chosen years, so a threshold met in 2025 still shows 2024/2026 beside it | `people_service.py`, `_format_people_display` |
| Hide source / filing form | Source, Filing Form, Filing Year, Source Filing, Confidence removed from the grid and the form; captions no longer name SEC / YFinance. Management Changes keeps source text behind an opt-in toggle | `screening.py`, `screening_config.PEOPLE_DISPLAY_COLUMNS` |
| SEC + Yahoo merge | `people_service._merge_sources`: per ticker, unique two-way name match (surname + first name or its short form); SEC wins every field it has, Yahoo fills year born, total pay, exercised / unexercised value on the SAME year; age derived per year. **Data gap:** Yahoo officers are ingested for 75 non-SEC tickers only — 1 ticker overlaps — so the merge is live but has almost nothing to merge until the pipeline runs Yahoo for US companies (Shashank) | `people_service.py`, frame `people_universe_v3` |
| Age without Yahoo | 5.02(c) bios state age ("Mr. Bramlage, age 49") — 1,624 events. Year born = filing year − age, joined on (ticker, person_key); age shown per year | `management_changes.stated_age`, `screening._fill_age_from_filings` |
| Tenure | `add_tenure`: appointment (effective, else announced) to the next departure of the same person, company and seat type, or today. 4,054 rows, 629 closed roles. Filing year is never used as a start date | `management_changes.add_tenure` → Role Start / Role End / Years in Role |
| Executive movement + pay | `mark_moves`: departure at A + appointment at B, AND B's own filing names A; departure closest before the appointment; skipped when the person took a job at a third company in between (Denton: Lowe's → Pfizer → Nike). 92 moves (53 officer, 39 board). `compare_move_pay`: latest pay at A ≤ year left vs first pay at B ≥ year joined | **Executive Moves** grid under Management Changes |
| ASICS 7936 future years | Root cause (data team): the non-SEC ingester read `%20` in URL-encoded file names as the year ("June%2030" → 2030, "March%2031" → 2031). App guard: buckets later than next year are not offered (also hides OR, 1913, ZAL, ATD) | `company_filings._prefetch_ticker_filter_data` |
| BOO without filings | Never ingested (0 v5 rows, 0 blobs). Dropdown now lists only tickers with ≥1 v5 row (76 hidden) — index semi-join, 0.26s warm, run in parallel, fails open | `company_filings._tickers_with_filings` |
| Foreign-language filings | 7936: 58/67 `filing.pdf` English; English copies exist in `archive/` for some Japanese ones. No language column/metadata exists → pipeline fix (prefer `_en`, store language). Not app-side | data team |
| ER / document-type order | Year already renders before Document Type (commit aa2e5ce); "ER" is read as "year" | no change |
| Key Devs date range | Already offered (timeframe or custom start/end) | no change |
| Screen organisation | Companies / Key Devs / Equities / Fixed Income / People / Transactions / Projects already in "Screen For" | no change |
| Warm-up | People + Management Changes warm on their own thread at boot (78s), no longer queued behind the Key Devs warm | `cache_manager._background_warmup_thread` |

**Left for Shashank / data team:** Yahoo officer ingestion for US tickers; re-filing the mis-named non-SEC blobs (7936, OR, 1913, ZAL, ATD); English-first document choice + language column; ingesting BOO and the other 75 tickers; re-labelling 5.02 pay-only rows upstream; indexes.

**Tests:** `test_people_service.py` (ALL PASS, + merge / pivot / Unknown cases), `test_management_changes.py` (13 passed, + age / tenure / moves incl. shared-name and intervening-employer traps / pay).

**E2E (Playwright, system Chrome, video recorded then deleted), both testing :8501 and STG checkout :8503:**
Companies (Discount Stores → 4 rows), Key Devs (Management Changes → 25 rows), People (CFO · Salary + Bonus · 2024–2026 → 148 executives / 133 companies, columns 2024/2025/2026, no source columns, 0.23s), Management Changes (534 / 256, tenure columns), Executive Moves, Company Filings (BOO absent, ASICS years 2015–2026 only). No page errors.
