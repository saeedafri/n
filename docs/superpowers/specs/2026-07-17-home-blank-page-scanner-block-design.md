# Home page blank / 24–62s stall — root cause & fix

**Date:** 2026-07-17
**Author:** debugging session (systematic-debugging)
**Scope:** `app/pages/home.py` (render-path only)
**Severity:** P1 — `/home` (the landing page every user hits after login) rendered a
blank white screen for 24–62 seconds on STG.

---

## Symptom

- `https://marketdata-stg.coresight.com/home` shows a **blank white page** for tens of
  seconds after login; "no matter how many times I open it, the home page is taking time."
- The stall gets **worse** the longer the process runs (24s → 34s → 60s → 62s within one
  session).

## Evidence (STG server log, 17-Jul, `server-logs-20260717_161325.log`)

| Signal | Value | Meaning |
|--------|-------|---------|
| `home.py:main [PAGE_LOAD] home TOTAL` | **15–77 ms**, `status=OK` | The page *body* is fast and healthy. |
| `[CLICK->RENDER] page=home render` (home.py:251, from `main()` start) | **0.02–0.08 s** | Body → footer is instant. |
| `[CLICK->RENDER] page=home render` (base_page finish, main.py:736, from rerun top) | **24.00 / 34.57 / 60.39 / 62.03 s SLOW** | The *whole rerun* took a minute. |
| `page=logs` `[CLICK->RENDER]` during the same window | **0.05 s** | Other pages render fine — **home-specific**, not global GIL/OOM. |
| Heartbeats | steady 60s, `rss≈1 GB`, `avail≈2 GB` | Server is **idle-waiting**, not CPU-bound, not OOM. |
| `[FILINGS_CACHE_ROOT] path=/home/filings_cache source=azure_home_persistent` | — | Filings cache is on the **Azure `/home` SMB share**. |
| `[FILINGS_CACHE_STATS] files=0` at process start (09:13) | — | Blob cache starts **empty**; the background scanner then downloads blobs all session (mem report: `BlobLite=44,040`, `FinancialFact=43,427`, `Tag=215,527`). |

**Localising the 60s:** for the slow rerun `R00408`, `home.py:14 new_rerun_id` logged at
`09:41:00` and `PAGE_LOAD SEQUENCE START` (inside `main()`) at `09:42:00` — a 60s gap
**between the top of `home.py` and `main()`**, i.e. entirely inside home's *module-level*
code. `main()` itself, `_load_companies()` (cached), and `hide_sidebar()` are all sub-ms.

## Root cause

`app/pages/home.py` ran the background-filings-scanner **initialization synchronously at
module scope on every render** (`pg.run()` re-executes the page file each rerun):

```python
ensure_cache_purge()
_audit = get_cache_audit()          # ← _audit_file_cache(): os.walk(FILINGS_BLOB_CACHE_DIR)
if _audit.get('should_scan'):
    init_background_scanner(auto_start=True)
```

`get_cache_audit()` → `_audit_file_cache()` does an **uncached `os.walk()` over the entire
filings blob cache**. On STG that cache lives on `/home/filings_cache/filings_blob_cache`
— the **Azure Files SMB share**, where every directory listing / `stat` is a network round
trip. As the background scanner downloads blobs (up to ~44k files), the walk grows to tens
of seconds and **blocks the Streamlit render thread** — the browser shows blank the whole
time. This is why it worsens over the session and why only `home` was affected.

The work was also **redundant**: the scanner is already started **once per process** in
`app/main.py` (`ENABLE_BG_SCANNER` block, guarded by `APP_BG_SCANNER_STARTED`;
`MAIN_BG_SCANNER_INIT` logged at 09:13:11). `init_background_scanner()` is additionally
guarded by a module-global `_initialized`, so home's re-kick did nothing useful — only the
uncached SMB walk in `get_cache_audit()` had a side effect: the stall.

## Fix

Remove the synchronous scanner/audit/purge block from `home.py`'s module body. The scanner
still starts process-wide via `main.py`; `company_filings.py` still calls the fast
`ensure_cache_purge()` no-op for direct landings. Home displays nothing about filings, so
it must never touch that cache on the render path. A prominent comment replaces the block
to prevent regression.

**Diff:** `app/pages/home.py` — deleted the `try/except` BG_SCAN block (and its duplicate
`import time as _perf_time`; the line-12 import remains for `main()`).

## Verification

- **STG log (before):** `[CLICK->RENDER] page=home render=60.39s SLOW` while body = 77ms.
- **Local UI (after, `run_local.sh` + `ui_test.py --path /home`):** page renders full
  content (nav, "CORESIGHT MARKET DATA", Company/Sector selectors, footer);
  `[PAGE_LOAD] home TOTAL=4.3ms status=OK`, `[CLICK->RENDER] page=home render=0.00s`; **zero**
  `cache_audit`/`cache_purge`/`BG_SCAN` entries on `page=home`. No regression.
- Local cannot reproduce the SMB latency (local cache = `<repo>/data`, fast); the root-cause
  proof is the STG production log + code path above. The local run proves the block no
  longer executes on home and the page renders correctly.

## Rollout

- Ship `app/pages/home.py`. No DB, no schema, no config change. Reversible (re-add block).
- STG deploys ~6×/day; the fix takes effect on next deploy. Confirm on STG by watching for
  `[CLICK->RENDER] page=home render` to drop to sub-second.

## Related issues (flagged, NOT fixed here — scope discipline)

1. **`_audit_file_cache()` should not full-`os.walk` the SMB cache uncached.** It is still
   called (once, guarded) inside `scanner.start()`. Replace with a cheap signal (a
   maintained file-count/marker, or `@st.cache_data(ttl=...)`) so no code path pays a
   full SMB walk. Benefits any future caller.
2. **edgartools parse-tree retention.** Mem report holds `Tag=215,527` +
   `XMLAttributeDict=214,538` + `FinancialFact=43,427` from 09:31 onward without shrinking
   — the scanner's XBRL/BeautifulSoup trees appear retained. Investigate freeing them after
   each filing to cut steady-state RSS (~1 GB → lower). Separate memory workstream.
3. **STG App Setting** `EDGAR_CACHE_DIR=/home/edgar_cache` is still unset (per CLAUDE.md),
   causing cold EDGAR caches every deploy. Unrelated to this bug but compounds first-load cost.

---

## UPDATE 2026-07-17 (later) — second blocking site + RAM/forecast investigation

### Second location of the SAME root cause (fixed)

The next STG log (`server-logs-20260717_171827.log`, process restarted ~22:25) showed
`MAIN_BG_SCANNER_INIT | 52393 ms` — **main.py's scanner init ran SYNCHRONOUSLY for 52s**
on the first authenticated load after restart, blanking that first page. Cause: the same
`_audit_file_cache()` `os.walk()` over the **persistent** `/home` filings cache (which
survives restarts, so it still holds ~44k files right after boot). Every other warmup in
`main.py` is threaded; this one wasn't.

**Fix:** `app/main.py` — moved `ensure_cache_purge()` + `init_background_scanner()` onto a
daemon thread (`bg-scanner-init`), env flag set before spawn to avoid a duplicate. First
page load no longer waits. Verified: app boots clean, home renders, no errors.

Together with the home.py fix, no page render path touches the SMB cache anymore.

### RAM — where it actually goes (from MEM_REPORT census)

Steady-state RSS climbed 214 MB → **1024 MB** in the earlier process. The census pins it on
the **background filings scanner + edgartools**, NOT on user data:
`Tag=215,527` + `XMLAttributeDict=214,538` + `NavigableString=56,571` + `FinancialFact=43,427`
— retained BeautifulSoup/XBRL parse trees (`dict=108 MB`, `list=26 MB` shallow). Plus
transient Azure blob-listing objects (`BlobLite=44,040`). `dataframes=10 MB` — the actual
app data is tiny.

- `MALLOC_ARENA_MAX=2` **is** the RAM lever (glibc `free_retained` 675 MB → ~146 MB with it).
  It's set via `os.environ.setdefault` at `main.py:11`, which is **after glibc init →
  unreliable for the current process**. To guarantee it, set it as a real **Azure App
  Setting** (env present before the container starts).
- **Biggest lever if the aggressive filing pre-scan isn't required:** `ENABLE_BG_SCANNER=0`
  (and optionally `ENABLE_MA_OVERRIDES_AUTO=0`) removes the edgartools parse-tree RAM and
  the 52s walk entirely. Product decision — flagged, not toggled.

### Forecast auto-refresh (annual) — verified running & correct

`coreiq_forecast_refresh_runs` shows every 06:00/18:00 IST window `status='ok'`. Companies
auto-forecasted (by `computed_at`): **annual** CAG+CRMT (Jul-16), AAP (Jul-12), PSMT (Jul-9),
334-company backfill (Jul-8); **quarterly** AAP (Jul-12), BNED (Jul-11), CHSCP/LEVI/PEP
(Jul-10), PSMT (Jul-9). `force=False` reporting-date-driven behavior is working as designed;
Jul-17 windows ran and updated 0 (nothing had newer actuals). No action needed.
