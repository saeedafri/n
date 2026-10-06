# Every download goes through a Blob fetch — design

**Date:** 2026-10-06
**Status:** implemented, verified locally against the STG database
**Trigger:** Screening → Excel on STG failed with a download-manager modal:
`Error : No Internet Connection or DNS Failed` ·
`URL : https://marketdata-stg.coresight.com/media/49c1dbe8…b820d6c9.xlsx`,
while Market Data (Income Statement / Balance Sheet / Cash Flow) and every PDF
download on the same host worked.

---

## 1. Root cause

The difference between the pages that worked and the ones that failed was never
the file, the size, or the proxy. It was **how the browser was told to fetch the
`/media/` URL**.

| Mechanism | Pages | Result |
|---|---|---|
| `st.download_button` → `<a href="/media/… .xlsx" download>` | screening, live transcript, retailer admin, earnings-calls CSV, logs, filings upload | **broken** |
| `components.html` + `fetch(url) → Blob → blob: anchor` | market data, company filings, transcript PDF, market-size forecasting | works |

Two independent failure modes sit on the anchor, both of which end in the file
being gone by the time the bytes are actually requested:

1. **The anchor is refetched by a third party.** A desktop download manager
   (the dialog in the report is one) hooks the anchor click and issues its own
   cookie-less request, usually *after* the user picks a save location. The
   fetch therefore starts seconds after the click, outside the Streamlit
   session.
2. **The file is swept while the anchor is idle.** `MediaFileManager` drops a
   session's media references at the start of each script run and deletes
   orphans at the end. `MediaFileKind.DOWNLOADABLE` buys one sweep of grace —
   marked first, deleted on the next sweep. Worse, for deferred (callable)
   data, `MediaFileManager.execute_deferred` stores the generated file and
   **deliberately leaves it unmapped from the session** (source comment: *"We
   leave actual_file_id unmapped so repeat clicks rerun the callable"*), so it
   is swept after two runs no matter what the user does. Screening's Key
   Developments export is exactly that path.

A fetch into a Blob suffers neither: the transfer starts the instant the trigger
component mounts (the file is certainly still registered at that moment), and
the anchor the browser finally clicks is an in-memory `blob:` URL that needs no
network and no cookie.

Not the cause, and ruled out: the `/media` route is reachable through the STG
proxy (market data uses it), the workbook is not too large for the websocket
(only a ~60-byte URL travels there — the 2026-07 `RangeError` fix), and there is
no auth on the media endpoint.

## 2. The fix

One shared helper, `app/utils/media_url.py`, used by every download in the app:

```python
lazy_download_button(label, filename, build_fn, mimetype, key, …)
```

* a plain `st.button` inside an `st.fragment` — a click reruns that button
  alone, never the page;
* `build_fn()` runs on click, so expensive workbooks are never built for users
  who do not ask (screening Key Devs: 336s of the old 340s "Show Results" wait);
* the bytes go to `serve_bytes()` (media manager → `/media/<sha>.<ext>`), and a
  zero-height `trigger_download()` component fetches that URL into a Blob and
  saves it;
* everything is drawn inside ONE container, so the pair still fits in an
  `st.empty()` slot (the Key Devs download slot is one).

`trigger_download()` and the existing `render_download_buttons()` now share one
`saveBlob(url, name)` JS function; `market_data._trigger_excel_download` and its
copy of the fetch/Blob snippet were deleted and `_lazy_excel_download` now
delegates to the shared helper.

Button keys are unchanged where page CSS targets them
(`st-key-_xlbtn_*` in market data, `st-key-xlbtn-*` in screening), so no
styling moved.

### Call sites converted

| File | Download |
|---|---|
| `app/pages/screening.py` | Companies / Key Devs / People Excel (all three via `_render_excel_js_download`) |
| `app/pages/market_data.py` | all tab Excels (dedupe only — it was already Blob-based) |
| `app/pages/earnings_calls.py` | cross-search CSV, single-transcript CSV |
| `app/pages/live_earnings_transcript.py` | MD / TXT / JSON |
| `app/pages/logs.py` | server log, analytics log |
| `app/pages/retailer_adding.py` | screen-data CSV, SEC+NON_SEC CSV, bulk template |
| `app/pages/company_filings_add_files.py` | blob file download |

Two call sites also stopped doing eager work as a side effect: the logs page no
longer reads every log segment into RAM on each render, and the filings upload
page no longer downloads every file in a listed folder on each render — both are
now deferred to the click.

`app/utils/market_data.py` still contains `st.download_button` calls. Nothing
imports that module (it is a stale duplicate of `app/pages/market_data.py`), so
it is excluded from the guard test and left alone — flagged for deletion
separately.

## 2b. Follow-up: the button looked dead on a slow export

Reported right after the first fix: on the Key Developments screen with no
Industry criterion (75,593 events), clicking Excel appeared to do nothing, so
the user clicked repeatedly.

The click was working. The build is simply long, and the new button showed
nothing while it ran. Measured against the staging DB on 2026-10-06, the export
pages the workbook is assembled from:

```
page  1: 10,000 rows in 19.9s      page  7: 10,000 rows in 11.2s
page  2: 10,000 rows in 13.8s      page  8: 10,000 rows in 15.7s
...                                 page 12: 10,000 rows in 23.7s
TOTAL 120,000 rows in 227.2s over 12 pages   (~19s per 10k rows)
```

So a 75k-event export is ~2.5 minutes of fetching before openpyxl even starts.
`st.download_button` with a callable had covered this for free: its frontend
component spun while it waited for the deferred file. A plain `st.button` has no
such state, and two things hide every other indicator:

* STG runs with `--ui.hideTopBar=True`, so Streamlit's own "Running" status is
  not on screen at all;
* `core/boot_overlay.py` and `components/loading.py` both carry "ONE SPINNER
  ONLY" rules — `[data-testid="stSpinner"]{display:none!important}` while a
  branded overlay is in the DOM — and a button click puts that overlay up.
  Measured on the real page: `st.spinner` rendered with the correct text at
  **0×0, `display: none`**. An `st.spinner` fix would have been invisible too.

Fix: `media_url.render_build_status()` — the status line lives inside a
component iframe, which no page CSS can reach, and its elapsed clock ticks
client-side so it keeps counting while the server thread is busy. It is shown
before `build_fn()`, cleared in a `finally`, and only then does the download
fire. Verified on the real screening page (deliberately slowed build): the line
reads "Building full export… / 2s elapsed" in the 212px slot, legible and
unclipped, and the workbook still arrives.

## 3. Data flow

```
click → fragment rerun → build_fn() → bytes
      → media_file_mgr.add(bytes, …, is_for_static_download=True) → /media/<sha>.xlsx
      → <iframe height=0> fetch(absolute /media URL) → Blob → blob: anchor → disk
```

No DB schema change. No new dependency. Nothing added to the websocket.

## 4. Testing

`tests/test_downloads_use_blob_fetch.py`

* repo guard: no file under `app/` (except the unloaded `utils/market_data.py`)
  may call `st.download_button` — this is what stops the bug coming back;
* a click builds exactly once, emits `saveBlob(...)` with a JSON-quoted
  filename, and contains no `<a href=` anchor;
* a slow build shows the iframe status (message + client-side clock) *before*
  the build, clears it after, and never falls back to `st.spinner` — the page
  CSS hides that;
* an empty build toasts instead of serving a 0-byte file;
* a failing build is reported, not raised.

Real-UI verification (`bash .claude/dev/run_local.sh`, `http://localhost:8501`,
staging DB, 2026-10-06):

| Page | Click | File |
|---|---|---|
| `/screening` Companies | Excel | `Screening_Results_…xlsx` — 20,618 B, 562 rows |
| `/screening` Key Devs (deferred build) | Excel | `KeyDev_Screening_…xlsx` — 407,829 B, 1,098 events × 12 cols |
| `/screening` People | Excel | `People_Screening.xlsx` — 670 rows × 22 cols |
| `/market_data?ticker=AMZN` | Excel | `AMZN_Company_Profile.xlsx` — 10,166 B, 3 sheets |
| `/logs` | Download Logs | `server-logs-…log` — **55,273,034 B** |
| `/retailer_adding` | Bulk template | `coreiq_companies_bulk_upload_template.csv` — 142 B |

23 `/media` requests across the runs, **0 failures**. The 55 MB log proves the
path holds for large files, which is where the anchor used to lose the race.

## 5. Rollout

Code-only; no app settings, no DB, no migration. Deploy is a normal STG push.
Rollback is reverting the listed files. The user-visible change is nil except
that screening and the other converted buttons now behave like the Market Data
button users already trust.
