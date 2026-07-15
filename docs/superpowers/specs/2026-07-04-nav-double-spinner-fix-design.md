# Nav double-spinner / "bounce back" fix — design

**Date:** 2026-07-04
**Area:** `app/components/navigation.py` (`_inject_transition_js`), `app/components/loading.py`
**Severity:** High — every header navigation on STG shows two spinners with a flash back to the source page in between.

## Symptom (client report)

Clicking any header nav button (Market Data / Earnings Calls / Calendar / Screening / News):
1. A spinner appears and "loads".
2. The **original page comes back** (visible again).
3. A **second spinner — this one with text** appears.
4. Only then does the destination page open.

## Root cause (proven with a Playwright overlay-timeline probe)

There are **three uncoordinated loading overlays** in play on a client-side page switch:

| Overlay | Element | Where | Visual |
|--------|---------|-------|--------|
| Nav overlay | `#cs-ov` | `navigation.py` driver (parent realm) | logo + ring + shimmer, **no text**, `.cs-c` card |
| Boot splash | `#cs-boot-overlay` | `boot_overlay.py` (index.html) | logo + ring + **text** + shimmer (full reload only) |
| Page loader | `.cs-al-ov` / `#cs-sticky-loader` | `loading.py`, called by each page | logo + ring + **text** + shimmer, `.cs-al-card` card |

On a header click the driver shows `#cs-ov`, forwards the click to a hidden `st.page_link`
(client-side rerun), then a settle-poll hides `#cs-ov`. That poll hid the overlay after
**~700 ms of "content present"** (`contentTicks>=7`). Its "content" selector matched
`stMarkdownContainer` — **which the header itself is**. So:

- **Locally (fast server, first delta ~180 ms):** the destination's own loader `.cs-al-ov`
  is already mounted when `#cs-ov` hides → looks continuous (but still two different-looking
  cards briefly overlap).
- **On STG (first delta > 700 ms):** `#cs-ov` hides **while the source page is still on
  screen and no destination delta has arrived** → the source page is revealed ("comes back
  to same page") → then the destination page mounts its **own text loader** (`.cs-al-ov` /
  `#cs-sticky-loader`) → perceived as a **second, different spinner**.

Probe evidence (`/market_data` → Earnings Calls, local warm):
```
   72ms | #cs-ov ON  | pageLoader -   | /market_data     | mainlen 2713
  181ms | #cs-ov ON  | pageLoader ON  | /earnings_calls  | mainlen 160   (both live; header only)
  841ms | #cs-ov off | pageLoader ON  | /earnings_calls  | mainlen 257   (#cs-ov early-hide)
 2401ms | #cs-ov off | pageLoader off | /earnings_calls  | mainlen 53308 (content painted)
```
The `contentTicks>=7` hide at 841 ms is the defect: it is driven by the header, not by the
destination actually being ready.

The full-reload fallback (`__csNavWatch` → `location.href` at ~1.4 s) is **not** the main
culprit — success is detected as soon as `stMain` text changes (header paint), so it rarely
fires. It is nonetheless hardened here (longer budget, robust success signal).

## Fix — "one continuous spinner"

1. **Make `#cs-ov` identical to the boot/page loaders.** Same translucent click-through
   backdrop, same `.cs-al-card`-style card, **add a text label** set per-destination from a
   path→label map (mirrors `boot_overlay.py`). A handoff `#cs-ov → .cs-al-ov` is then
   imperceptible.

2. **Hide `#cs-ov` only when the DESTINATION has actually rendered — never while the source
   page is visible.** New hide condition (per nav to `targetPath`):
   - the browser is on `targetPath`, **and**
   - the header's `.active` nav link now points at `targetPath` (i.e. the *new* page's header
     has painted — the source page's header pointed at the source), **and**
   - the destination's own loader is up (`.cs-al-ov`/`#cs-sticky-loader`) **or** real content
     beyond the header has painted (`mainInnerText.length > 800`).
   - Guard: while `.active` still points at a *different* nav page, the source page is still
     on screen → **do not hide** (this is what closes the "flash back to source" gap).
   - Hard safety timeout (15 s) unchanged — the overlay can never trap the user.

3. **Coordinate so only one card is ever visible.** CSS `body:has(.cs-al-ov) #cs-ov`,
   `body:has(#cs-sticky-loader) #cs-ov { opacity:0 }` (in the driver styles and, defensively,
   in `loading.py`). Because the cards are now identical, the page loader simply continues the
   same visual.

4. **Guard the URL-change poller** so it does not restart a generic (early-hiding) poll while a
   click-driven nav is in flight (`if (win.__csNavigating) return;`). For genuine back/forward
   navigations it runs with `targetPath = location.pathname` and the same robust hide logic.

5. **Harden the never-block fallback.** Success signal = destination header active (or page
   loader up, or content painted); budget extended to ~8 s so a slow-but-progressing STG render
   is never aborted into a spurious full reload. A truly wedged switch still hard-navigates.

## Files touched
- `app/components/navigation.py` — rewrite the `_inject_transition_js` driver (styles, overlay
  DOM with label, `__csShow`/`__csPoll` hide logic, click handler success watch, poller guard).
- `app/components/loading.py` — add `#cs-ov` to the "one spinner only" hide rule.

## Verification
- Playwright overlay-timeline probe on every nav pair: assert (a) `#cs-ov` never hides while
  `.active` still points at the source page, (b) at most one *distinct* card is visible at a
  time, (c) no full-document reload on a normal nav, (d) label text matches the destination.
- Screenshot mid-transition to confirm a single branded card with the correct "Loading X" label.

## Result (verified 2026-07-04)
All five nav pairs (market_data→{Earnings Calls, Calendar, Screening, News}, earnings_calls→market_data)
PASS: no source-page reveal, no bare gap, correct per-destination label, client-side nav (no reload).
Mid-transition screenshot shows exactly one branded card.

---

# Follow-up fixes (same day, from a full-flow STG video + logs)

Testing the whole login→home→pages flow surfaced three more issues (none from the nav
fix above). Root-caused and fixed:

## 1. Newsroom `TypeError: Failed to fetch dynamically imported module …/static/js/*.js`
- **Not reproducible locally** (newsroom loads clean; only benign `_stcore/health` 404s).
  STG-only ⇒ **stale JS chunk after a redeploy**: the browser holds an old index.html /
  module-preload hints referencing chunk hashes the redeployed server no longer serves.
- **Fix:** `app/core/boot_overlay.py` — inject an early `<head>` script (`cs-chunk-recover`,
  marker bumped `v4→v5`) that catches exactly that error (`error` + `unhandledrejection`)
  and `location.reload()`s ONCE, guarded by a `sessionStorage` timestamp (reload <12s ago
  ⇒ ignored) so it can never loop yet still recovers from a later deploy. Verified: 1 error
  → 1 reload; immediate repeat → no 2nd reload.

## 2. `/logs` layout shoved to the right (horizontal overflow)
- The SIP-style log viewer uses `white-space:pre` (needed to keep the `|` columns aligned).
  With no width cap, inside Streamlit's flex tree it grew to the widest log line (~3512px).
  **The actual unconstrained ancestor was the baseweb `[data-baseweb="tab-panel"]`** — a
  flex child with default `min-width:auto` that grew to the box's content width and dragged
  the whole column stack right (page clipped by `overflow-x:hidden`, so it read as "shifted
  right" rather than a scrollbar).
- **Fix:** `app/pages/logs.py` — `min-width:0` on the tab-panel + the flex chain
  (`stVerticalBlock/stElementContainer/stMarkdown/stMarkdownContainer`), cap block-container
  to viewport, and give the box `.cs-logbox{max-width:100%;overflow:auto}` so long lines
  scroll INSIDE the box. Verified: page overflow 0px, box 1093px, content 3500px scrolls
  internally.

## 3. Login "Loading Home" rendered twice (extra home rerun during OIDC handoff)
- After the OIDC callback, `auth_manager` redirects to `/home?login_handoff=1&…` to verify
  the cookie was set in the browser. `main.py` then **deleted the handoff params via
  `del st.query_params[...]`** — and each `st.query_params` mutation schedules a full app
  RERUN, re-rendering the landing page a second time.
- **Fix:** `app/main.py` — strip the handoff params **client-side** with
  `history.replaceState` (a 0-height `components.html` script) instead of mutating
  `st.query_params`. No rerun; the next natural rerun reads the already-clean URL so the
  handoff is not re-processed. Same end state, one fewer home render.
- **⚠ Unverifiable locally:** the `_login_handoff` branch only runs during real SSO; the
  LOCAL launcher bypasses OIDC, so this specific change MUST be confirmed on STG. App boots
  clean locally and all other flows regress-tested green.

---

# company_filings 12s cold load (2026-07-05, from STG logs)

- **Evidence:** cold load `FILINGS_PREFETCH 10.9s` → 12.5s total (`PAGE_LOAD CRITICAL`);
  warm load 297ms → 674ms. Live DB probe (STG): cold Azure connection `SELECT 1` = **6.7s**;
  the prefetch `EXPLAIN` scans **~75,446 rows** for AMZN with `Using temporary; Using
  filesort`. AMZN genuinely has ~75k metric rows on `coreiq_filing_metrics_v5`; the
  `DISTINCT ... COALESCE(storage_year, report_fiscal_year, fiscal_year)` is covered by **no**
  index (the `idx_v2_dropdown_covering` index lacks the fiscal-year cols; forcing it made it
  *slower* — 137k rows). So the query reads 75k row pages: ~0.5s warm, ~11s on a cold buffer
  pool after a process restart.
- **Fix (`app/main.py`):** a BACKGROUND, env-gated (`APP_FILINGS_PREFETCH_WARMED`),
  once-per-process warmup thread inside the `if _auth_ready_for_bg:` block — same pattern as
  `warmup_av_fulltext`/`warmup_ec_caches`. It runs the exact prefetch query for the common
  heavy filers (`FILINGS_WARMUP_TICKERS`, default `AMZN,AAPL,WMT,TGT,COST,HD,NKE,M`) to load
  the shared ticker index + those tickers' pages into MySQL's buffer pool before a user
  reaches /company_filings. Never blocks the first render (background thread). Toggle with
  `ENABLE_FILINGS_WARMUP=0`.
- **Not done (needs DDL / bigger refactor, flagged for later):** a covering index on
  `(ticker, doc_type, storage_year, fiscal_year, report_fiscal_year)` would make the query
  index-only (fast even cold, and for ALL tickers); or render the filing first and load the
  filter dropdowns async. Both out of scope here (can't run DDL; can't verify on STG).
- **⚠ Unverifiable locally:** the whole `if _auth_ready_for_bg:` warmup block is skipped
  under the LOCAL bypass (same as the existing EC/FT warmups), and STG's buffer pool is
  already warm from other traffic, so the cold→warm speedup can only be confirmed on STG
  after a fresh process start. Query validated (~500ms) and placement verified structurally.
