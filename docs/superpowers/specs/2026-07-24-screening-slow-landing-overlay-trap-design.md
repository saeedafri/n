# Screening "slow every time" — root cause & fix (2026-07-24)

## Symptom
Clients report the `/screening` page "takes too much time" **every time** they
navigate to it — on local, STG, and prod. Feels like a multi-second load on every
visit.

## Investigation (evidence-first)

Measured server compute, DB, browser rendering, and network **separately** from the
24-Jul STG + prod logs and a live local repro (Playwright, real nav-click path).

| Signal | Finding |
|---|---|
| Screening `PAGE_LOAD` server time | p50 **135ms** first landing, 38–115ms warm (STG+prod). Server is fast. |
| `CLICK->RENDER` (server-measured) | avg **0.29s** prod / 0.50s STG. |
| Browser during the stall | **0 long-tasks, ~7KB JS, 564 DOM nodes** — browser idle, not rendering. |
| Nav overlay lifecycle (real click) | overlay ON at 26ms, **OFF at 15012ms** — the hard 15s safety timeout, **every visit**. |
| Screening main `innerText` length | **773 chars** (fully rendered). |

## Root cause
The branded page-transition overlay in `components/navigation.py`
(`_inject_transition_js` → `__csShow.__csPoll`) decides the destination has
rendered via:

```js
destReady = onDest && !sourceStillShown && (pageLoaderUp() || mainLen() > 800);
```

Screening's fully-rendered main text is **773 chars — just under the 800 threshold**.
Screening has no page-owned sticky loader either. So `destReady` **never becomes
true**, and the overlay only hides via the 15-second hard safety timeout
(`ticks >= 150`). The translucent blurred "Loading Screening" card therefore covers
the (already-rendered, ~0.3–2s) page for a full **15 seconds on every navigation**.

This is 100% client-side and independent of DB/network, so it reproduces identically
on local, STG, and prod (same code, same ~773-char page). The `mainLen()>800`
heuristic is simply miscalibrated for compact pages.

Secondary (VPN/first-session only): `_ensure_db_tables()` was gated on
`st.session_state`, so every **new browser session** paid ~5 `CREATE TABLE IF NOT
EXISTS` round-trips + a segment-cache read on its first screening landing (~4s over
VPN; ~ms on prod).

## Fix

**1. Reliable "page rendered" beacon (primary).** `render_coresight_footer()` runs
last in every page's `main()` and emits `.coresight-footer-exact` into the parent
DOM via `st.html`. Added `footerUp()` and made the overlay hide when the destination
header is active **and** its footer has painted:

```js
function footerUp(){ return !!doc.querySelector('.coresight-footer-exact'); }
...
destReady = onDest && !sourceStillShown &&
            (pageLoaderUp() || (act === tp && footerUp()) || mainLen() > 800);
```

Purely additive — can only hide the overlay **earlier**, never later. `mainLen()>800`
kept as a legacy fallback. Works for **all** compact pages, not just screening.

**2. Process-level table bootstrap.** `_ensure_db_tables()` now gates on a module
global `_DB_TABLES_READY` instead of `st.session_state`, so the DDLs run once per
server process rather than once per browser session.

Files: `app/components/navigation.py`, `app/pages/screening.py`.

## Verification (local UI, Playwright, over VPN → STG DB)

| | Before | After |
|---|---|---|
| Screening nav overlay hides at | **15,012 ms** (every visit) | **1,014–2,313 ms** (content-ready) |
| Warm re-visit felt time | ~15 s spinner | ~1–2.2 s |
| First landing (cold process) | 15 s | 6 s (one-time DDL, then footer beacon) |

Regression check (overlay hide time, no premature flash, no stuck overlay):
- `market_data` 343ms (via `mainLen>800`) · `earnings_calls` 114ms (own loader handoff)
- `newsroom` 11.7s — overlay correctly tracks the page's **real** render time (heavy
  page over VPN), not a regression.

On prod (no VPN, ~135ms server render) the overlay clears in well under 1s.

## Out of scope (flagged, not changed)
- **Prod RAM ~1.6–2.0 GB**: dominated by edgartools/BeautifulSoup filing objects
  (`FinancialFact`/`Tag`/`XMLAttributeDict`/`NavigableString`, ~745k objects) from
  SEC-filing parsing (company_filings + EDGAR warmup), **not screening** (STG runs at
  340–430 MB). Causes GC pressure that mildly slows all pages. Recommend a separate
  task: cap edgartools object retention or move filing parsing to a subprocess.
- **Network floor**: India ↔ Azure centralus ~233ms RTT, http/1.1, no HTTP/2/CDN
  (documented in CLAUDE.md). Only infra (HTTP/2 + CDN/region) fixes this; no code change can.
- **Heavy Key-Devs / additional-data screens**: the 90–121s `filing_metrics_v5`
  queries in the log are background segment-cache refresh from 18–19 Jul (already noted).
