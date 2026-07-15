# Calendar M&A Acquirer/Target Repair — Design

**Date:** 2026-07-15
**Scope:** Earnings Calendar → M&A completion detail popups showing garbage
acquirer/target values (e.g. Acquirer = "reference", Target = "SFV Services.
The information contained in Item 1.01 of this Current Report is incorporated
herein").

---

## 1. Root cause

The calendar's M&A feed (`EarningsCalendarRepository.get_ma_completion_events`,
`app/data/repository.py`) reads `coreiq_company_events` rows with
`event_subtype='M&A Closing' AND ma_is_closed=1` — 262 rows — and renders
`ma_acquirer` / `ma_target` verbatim in the detail panel
(`app/pages/earnings_calendar.py::_render_ma_detail_panel`).

Those `ma_*` columns are populated by a **server-side nightly enrichment job
(not in this repo)** that regex-captures names from raw 8-K text. The regex
matches legal boilerplate instead of party names:

| Garbage value | Where it came from |
|---|---|
| `reference` | "…is qualified in its entirety by **reference** to the full text…" |
| `completed its previously` (8 rows) | "…**completed its previously** announced acquisition…" |
| `Financial Statements of Businesses` (5 rows) | Item 9.01 heading |
| `SFV Services. The information contained in Item 1.01 …` | sentence run-on past the defined-term |

Verified against the source 8-K (accession 0001140361-26-027094): the real
answer for the screenshot row is Acquirer = **Bed Bath & Beyond, Inc.**,
Target = **TwoPonds, Inc. ("SFV Services")**, consideration 7,200,000 BBBY
shares, closed 2026-06-30.

**Data quality before repair (262 calendar rows):** 31 NULL acquirers,
32 NULL targets, ~25 sentence-fragment run-ons (>60 chars), many
short-fragment garbage values; 137 missing transaction value; 139 missing
announce date.

## 2. Why edgartools + LLM (and not something else)

- **edgartools** (already a project dependency, already used by
  `scripts/ingest_key_developments.py`): every one of the 262 rows carries a
  `source_ref` URL containing the SEC accession number → we re-fetch the
  exact source 8-K and cut out Item 2.01/1.01/8.01 section text. SEC EDGAR is
  the authoritative primary source for completion events; no other feed in
  the stack (Alpha Vantage news sentiment, Yahoo Finance) carries structured
  deal fields at all.
- **LLM structured extraction** over the item text replaces the broken regex.
  Legal prose ("Merger Sub merged with and into X, with X surviving…, wholly
  owned subsidiary of Parent…") is exactly the case regexes fail and LLMs
  handle. The repo's OpenAI key (and the Gemini/Moonshot keys) in `.env` are
  dead/leaked, so the one-off backfill extraction was run with Claude
  subagents over harvested chunks — the script is deliberately split so any
  LLM can run the middle pass.

## 3. Architecture / data flow

**Constraint (owner decision): the app team does NOT write to the DB — DB
ingestion/repair belongs to the data team.** The app therefore ships a
read-only overlay file; the DB stays untouched by us.

```
scripts/enrich_ma_events_v2.py harvest          (read-only DB SELECT + EDGAR)
    STG coreiq_company_events (262 calendar rows)
      → parse accession from source_ref
      → edgartools find(accession).text()
      → Item 2.01 + 1.01 + 8.01 section extraction (≤7000 chars)
      → scratchpad ma_chunk_NN.jsonl (10 chunks; 13 PoolTimeout rows
        re-fetched sequentially into chunk_09 → 262/262 coverage)

LLM pass (Claude subagents, one per chunk — .env OpenAI/Gemini/Moonshot
keys are all dead, so extraction runs key-independent)
      → per event: {acquirer, target, deal_type, transaction_value_usd_m,
                    announce_date, close_date, confidence}
      → scratchpad ma_extracted_NN.jsonl

scripts/enrich_ma_events_v2.py emit-overrides   (validation, NO DB writes)
      → name length/boilerplate/generic-term rejection, date + value
        bounds, deal-type canonicalisation, low-confidence nulling
      → app/data/ma_event_overrides.json  (checked into the repo)

app/data/repository.py::get_ma_completion_events
      → merges overlay over DB rows AFTER the materialized read
      → overlay key present (even null) replaces acquirer/target;
        null = "verified unknown — hide the garbage"
      → zero runtime EDGAR/LLM calls; one local JSON load per process
        (lightning-fast constraint honored)

scripts/enrich_ma_events_v2.py apply            (DATA TEAM ONLY)
      → same validated fields as UPDATEs, --dry-run first;
        we hand over script + ma_extracted_*.jsonl, we do not run it
```

### Self-maintaining path for NEW events (added same day)

The nightly ETL keeps inserting new 'M&A Closing' rows with garbage names.
Rather than manually re-running the pipeline, the app auto-enriches them:

```
utils/ma_overrides_auto.py  (daemon thread, 6-hourly, boot-delayed 120s)
      → diff calendar M&A event_ids vs repo overlay + runtime overlay
      → NEW ids only (typically 0-2/day, cap 40/tick, ≤3 retries each)
      → edgartools fetch (lazy import, off the request path)
      → utils/ma_8k_extract.extract_ma_fields — deterministic template
        extractor, precision-tested against the 204 high-confidence LLM gold
        records:  acquirer 98.2%, target 98.2%, close_date 96.3%,
        announce_date 94.4% precision (recall 27-40%; unmatched → None → "—").
        Transaction value deliberately NOT emitted (peaked at 88% — a wrong
        dollar figure is worse than "—").
      → runtime overlay <mdp-cache>/ma_event_overrides_runtime.json
        (atomic replace; repository reloads it on mtime change)
```

Merge precedence in `get_ma_completion_events`:
repo overlay (LLM gold) → runtime overlay (deterministic) → DB row.
Spin-merge filings (Reverse Morris Trust etc.) are detected and skipped by
the deterministic extractor because the filer-is-acquirer assumption fails
there. Kill switch: `ENABLE_MA_OVERRIDES_AUTO=0`;
`MA_OVERRIDES_AUTO_SLEEP_SEC` tunes cadence.
`utils/ma_8k_extract.sanitize_ma_name` is now the single shared validator
(page, script, and auto-enricher all import it).

## 4. App changes (this repo)

1. **`app/pages/earnings_calendar.py`** — `_sanitize_ma_name()`: rejects
   boilerplate/fragment/generic values (`reference`, `Purchaser`,
   "…Item 1.01…", >120 chars, …) at payload build
   (`_ma_to_fullcalendar`) *and* at render (`_render_ma_detail_panel`).
   Garbage now renders as `—`, never as misinformation — defense in depth
   against future bad ingests from the nightly job.
2. **`app/data/repository.py`** —
   - `_load_ma_overrides()` + overlay merge in `get_ma_completion_events()`
     (see §3);
   - the `ma_completion_events` disk materialization freshness signal now
     includes `MAX(updated_at)`; before it was row-count only, so the data
     team's future in-place repairs would have been served stale forever.
3. **`app/data/ma_event_overrides.json`** — the curated overlay (262 events).
4. **`scripts/enrich_ma_events_v2.py`** — harvest/emit-overrides/apply CLI
   described above; re-runnable any time the nightly job pollutes new rows.

## 5. DB schema

No schema change. Columns used: `ma_acquirer`, `ma_target`, `ma_deal_type`,
`ma_transaction_value_usd_m`, `ma_announce_date`, `ma_close_date`,
`ma_extraction_confidence`, `updated_at` on `coreiq_company_events`.

## 6. Testing plan

- Sanitizer unit-checked against the real garbage corpus (all rejected) and
  real company names incl. "Bed Bath & Beyond, Inc.", "buybuy BABY" (all kept).
- Overlay spot-checked against source filings (BBBY events verified by hand
  against accessions 0001140361-26-027094 / -028107).
- Playwright against the real local UI (`bash .claude/dev/run_local.sh`):
  click M&A chips (incl. the BBBY Jul 01 2026 event from the bug report),
  screenshot the detail popup, confirm clean Acquirer/Target/value fields.

## 7. Rollout & follow-ups

- **No DB writes from the app team.** The overlay ships with the app; the
  data team receives `ma_extracted_*.jsonl` + the `apply` subcommand
  (`--dry-run` first) to repair `coreiq_company_events` on their side. Once
  the DB is fixed, the overlay file can simply be deleted — the merge is a
  no-op without it.
- The `MAX(updated_at)` signature change makes the data team's future
  in-place repairs bust the app's disk materialization automatically.
- **Flagged, out of scope here:** the server-side nightly enrichment job
  still writes regex-quality `ma_*` fields for NEW events. Its extraction
  should be replaced with the same item-text + LLM approach; until then the
  display sanitizer hides (rather than shows) bad names for rows not in the
  overlay, and the harvest → extract → emit-overrides pass is re-runnable.
- **Security:** `.env` contains a Gemini API key that Google has flagged as
  leaked, plus dead OpenAI/Moonshot keys — rotate/remove.
