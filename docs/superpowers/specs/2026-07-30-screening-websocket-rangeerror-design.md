# Screening "Connection error / RangeError" — root cause & fix

**Date:** 2026-07-30
**Page:** `/screening` (Key Devs mode, and every Excel button on the page)
**Symptom:** modal — *Connection error. Failed to process a Websocket message.
`RangeError: index out of range: 99 + 7909686 > 348066`*

---

## 1. Symptom

Streamlit's frontend decodes each `ForwardMsg` with protobuf-js. The error means a
length-delimited field declared **7,909,686 bytes** while the buffer the browser
actually held was **348,066 bytes**. The decoder aborts, Streamlit tears the socket
down and shows the generic **Connection error** modal. Nothing is wrong with the DB
or the query — the *message* is undeliverable.

## 2. Root cause

`_render_excel_js_download()` in `app/pages/screening.py` base64-encoded the entire
workbook and inlined it into a `components.html` iframe:

```python
b64 = base64.b64encode(excel_bytes).decode("ascii")
btn_html = f"...<script>var _d=\"{b64}\";...</script>..."
_sthtml(btn_html, height=52)
```

The iframe's HTML is a protobuf **string field inside one ForwardMsg**, so the whole
workbook travelled through the websocket in a single message. Its size scales
linearly with the result set, and after the Jul-24 "full export" change (commit
`a8fa8f7`, `kd_xl`) the workbook holds **every** matching event, not the 500 shown
in the grid.

### Measured on the STG DB (2026-07-30)

| | value |
|---|---|
| `coreiq_company_events` rows | 156,634 (4,022 tickers) |
| Screening universe, all history, 23 categories | **145,967 events** |
| xlsx bytes per row (`_build_keydevs_excel_fast`) | ~316 B |
| Full workbook for that query | **101,786,300 B (102 MB)** |
| …as base64 in the ForwardMsg | **~136 MB** |
| Payload matching the reported error (7,909,686 B) | ≈ 5.93 MB xlsx ≈ **18.7k events** |

Streamlit's own guard (`server.maxMessageSize`, default 200 MB) never fires, so the
oversized frame is handed to Tornado and truncated/re-framed in transit by the
`marketdata-stg.coresight.com` proxy. The browser then decodes a short buffer →
`RangeError`.

## 3. When it triggers

* Key Devs mode with a wide window (**All History** / long timeframe) or many
  categories — i.e. any screen whose export grows past a few MB.
  The reported instance was ~18.7k events; the 378-company all-history screen is
  ~146k events / 102 MB.
* Same code path, so the same failure exists for the Companies-results Excel and the
  People-results Excel on `/screening` whenever their workbook gets large.
* Does **not** reproduce on `localhost` — there is no proxy between Tornado and the
  browser locally. It is environment-dependent, which is why it only shows on STG.

## 4. Fix (root, not symptom)

Stop putting file bytes in a websocket message. `_render_excel_js_download()` now
renders a native `st.download_button`, which registers the workbook with Streamlit's
**media file manager** (`button.py` → `media_file_mgr.add`) and puts only a
`/media/<hash>` URL in the ForwardMsg; the browser fetches the bytes over plain
HTTP. Message size is now constant (a few dozen bytes) at any result size.

Details:
* Signature unchanged → all three `/screening` call sites (key-devs, company
  results, people results) are fixed by the one change.
* `on_click="ignore"` so downloading never triggers a rerun.
* Look preserved (red outline, 13px, table icon, right-aligned) via CSS scoped to
  `st-key-xlbtn-*` from `st.container(key=...)`, plus the native
  `icon=":material/table:"` — which also drops the per-button
  `fonts.googleapis.com` stylesheet the iframe used to load.

## 5. Verification (real UI, Playwright, STG DB)

`/screening` → Screen For **Key Devs** → 23 categories, **All History** → Show Results:

```
results appeared after ~171s
HEADER: 145,967 key development events found · showing newest 500
CONNECTION_ERROR_MODAL: False
RANGE_ERROR: []
largest 5 websocket frames (bytes): [868015, 74487, 32402, 24808, 23682]
DOWNLOAD: KeyDev_Screening_20260730_1103.xlsx 101786300 bytes
MEDIA_HTTP_REQUESTS: [... '3ab02d7958273651377bc5752260e330460df23f']
CONNECTION_ERROR_AFTER_CLICK: False
```

The complete 102 MB workbook downloads over `/media/…`; the largest websocket frame
is 868 KB (the AG Grid 500-row page) instead of ~136 MB. Screenshot: `/tmp/kd_after.png`.

### 5b. Re-verified against the exact reported scenario (M&A, All History)

The user (Ristha Vilina Dsa) hit the modal while filtering **M&A / All History** in
Key Devs mode. Reproduced that precise path on a clean local instance (STG DB,
current working tree), instrumenting console/page errors, every websocket frame, and
`/media/` fetches:

`/screening` → **Key Devs** → *Key Developments by Category* → **M&A Activity** /
**All History** → Show Results:

```
results header appeared after 40s
HEADER: 8,116 key development events found · showing newest 500
CONNECTION_ERROR_MODAL: False
RANGE_ERROR: []                              # no protobuf RangeError in console or pageerror
LARGEST_WS_FRAMES_BYTES: [944856, 74487, 32402, 24808, 23682]   # 945 KB grid page, nothing multi-MB
Excel click → DOWNLOAD: KeyDev_Screening_20260730_1504.xlsx
MEDIA_RESPONSE: 575e99c9…  status 200  content-length 5,930,986  # 5.93 MB workbook over HTTP
CONNECTION_ERROR_AFTER_CLICK: False
RANGE_ERROR_AFTER_CLICK: []
```

The 5.93 MB workbook — the same order of magnitude as the reported 7,909,686-byte
payload — travels over `/media/` HTTP, not the socket. The largest websocket frame
is the 945 KB AG Grid page. No RangeError, no Connection-error modal, before or after
the download. Screenshots: `kd_verify.png` (criteria + results), `kd_verify_after.png`
(results grid + branded Excel button). Driver: `scratchpad/kd_verify.py`.

**Regression sweep — "never again":** `grep` across `app/` confirms no download or
PDF-viewer helper still base64-inlines file bytes into a `components.html` iframe
(`atob(` / `var _d=` / `{b64}` → **0 hits** in `app/pages/`). The only remaining
`b64encode` calls are a tiny SVG-icon data-URI (`navigation.py`), PKCE nonces
(`login.py`, `logout_bridge.py`), and an emailed PNG (`forecast_email_report.py`) —
none ride a Streamlit ForwardMsg. All five file-serving paths (screening Excel,
market_data Excel, earnings_calls Excel + transcript PDF, company_filings PDF viewer +
download, forecasting Excel) now route through `utils/media_url`.

## 5c. Two further root causes found while click-testing the fix

Moving the bytes onto `/media/` is necessary but not sufficient. Two lifetime bugs
surfaced only when the UI was actually clicked *after a rerun*:

**(i) A URL cached across reruns 404s.** Streamlit drops every media reference a
session holds at the *start* of each script run and deletes orphans at the end
(`script_runner.py` → `clear_session_refs` / `remove_orphaned_files`). A file
therefore survives only if it is re-registered *during that run* — which is why
`st.download_button` / `st.image` re-register every time. The first version of the
PDF viewers cached the **URL** in `session_state` and called `serve_bytes` only on a
cache miss. Proven in the UI (Prada 2018, same URL):

```
before rerun               : 200 len=432390
after rerun 1 (same filing): 404 len=69
after rerun 2 (same filing): 404 len=69
```

Fix: cache the **bytes** and call `serve_bytes` on every run. Content-derived
coordinates keep the URL byte-identical, so PDF.js does not refetch. This also uses
*less* memory than the original code, which cached the 33 %-larger base64 string.

**(ii) An auto-download's file can be deleted before the browser's fetch lands.**
`remove_orphaned_files` deletes `MediaFileKind.MEDIA` immediately but gives
`DOWNLOADABLE` a one-pass grace period (marked first, deleted on the next sweep) —
Streamlit relies on that so an in-flight download can finish. The filings Download is
a two-step flow (`st.button` → rerun → auto-clicking iframe) that clears its own
trigger state as it renders, so its element is not re-rendered on the following run.
Registered as `MEDIA`, the file was deleted out from under the iframe's async
`fetch` → 404 and no download.

Fix: `serve_bytes(..., for_download=True)` (the default) marks payloads
`DOWNLOADABLE`; the two streamed viewers pass `for_download=False` since they
re-register every run and should be freed promptly.

## 5d. Full click-through verification (local app, STG DB, Playwright)

Every converted path was exercised by actually clicking it, including *after* a
rerun/filter interaction — the case that exposed both bugs above.

| Page / control | Evidence |
|---|---|
| screening Key Devs — M&A / All History (the reported case) | `8,116 events`; Excel → **5,930,986 B** valid xlsx over `/media/`; largest ws frame 944 KB |
| screening Key Devs — 23 cats / All History | `145,967 events`; **101,786,300 B** workbook; frame 868 KB |
| screening — AG Grid **column filter** (open popup, type in its search box, Select All) | no Connection error, no `RangeError`; Excel still downloads afterwards |
| screening Companies mode Excel | `Screening_Results_….xlsx` 14,874 B, valid |
| company_filings PDF viewer (8 MB) | rendered (3 canvases); `200` + `206` range requests |
| company_filings viewer **after 2 reruns** | `200` both times, URL stable |
| company_filings Download **after a rerun** | `1913_2018_interim-report-Q2.pdf` 432,390 B, `%PDF-` verified |
| earnings_calls transcript PDF Download | 35,629 B valid PDF — **before and after** a keyword-search rerun |
| forecasting per-ticker Excel | `FLWS_Revenue_Forecasting.xlsx` 11,788 B — before and after tab-switch reruns |
| market_data Excel ×4 (Income/Balance/Cash Flow/Key Stats, via `_lazy_excel_download` → `_trigger_excel_download`) | all valid xlsx, no 4xx |
| Isolated 8.2 MB PDF through the helper | `FETCH 200 / 8,206,919 B`, `PDFJS_PAGES=232`, largest ws frame **894 B** |

No `4xx`/`5xx` on any `/media/` request in any run; the only console errors anywhere
are the pre-existing `/_stcore/health` + `/_stcore/host-config` 404s (Streamlit's
frontend probing page-relative paths — unrelated, present before this work).

## 5e. Second reported issue — "the spinner vanishes but the data comes later"

**Report:** on `/screening`, after clicking *Show Results* the spinner appears, then
disappears, and the grid only arrives much later — the user cannot tell whether
anything is still happening. Screen recording: `Stg-screening.mov` (grid appears ~9 s
after the click on STG, with no spinner for most of that gap).

### Root cause — two independent faults

**(i) The loader hid on a blind timer.** `_STICKY_REMOVER_JS` in
`components/loading.py` ended with `setTimeout(hide, 20000)` — a hard cap meant to
avoid trapping the user. Traced in the real UI (23 categories / All History):

```
t+ 2.3s   overlay appears
t+22.1s   overlay HIDES   <- iframeH=-1, alert=None, header=False: nothing was ready
t+333.4s  results header appears
t+334.2s  grid paints
>>> DEAD AIR: 312.1s
```

The overlay vanished exactly 20 s after its JS started, while the server was still
working. Any run slower than 20 s therefore showed a static page with no feedback.

**(ii) 99% of the wait was an export nobody asked for.** The results path built the
FULL Excel workbook *before* rendering the grid. Measured against STG:

| step | time |
|---|---|
| count query | 1,148 ms |
| first 500-row page (all the grid needs) | 2,823 ms |
| full-export fetch (152,719 rows) | 323,270 ms |
| full-export xlsx (101.8 MB) | 12,609 ms |
| **grid-only path** | **4.0 s** |
| **what actually ran** | **339.8 s** |

### Fixes

1. **Build the export on click, not on render.** `st.download_button` accepts a
   *callable* for `data`; Streamlit registers it via `media_file_mgr.add_deferred` and
   executes it on click **in a worker thread** (`app_session` → `asyncio.to_thread`),
   so it never blocks the run or other sessions. The eager block is gone; the new
   `_build_full_keydevs_workbook()` closure is passed instead. The button shows
   `disabled=True` + a spinner for the whole build, so that wait is explicit.
2. **Make the overlay track the actual run.** The blind 20 s cap is replaced by a
   `[data-testid="stStatusWidget"]` check — the widget exists only while a script run
   is in flight (verified: absent when idle, present with "Stop" while running). The
   overlay now hides when the content paints, or when the run has finished and stayed
   idle ~900 ms with nothing painted (so a genuinely empty result still clears it).
   A 600 s absolute cap remains purely as a last-resort escape hatch.

### Verification (real UI)

| scenario | before | after |
|---|---|---|
| 23 cats / All History — grid ready | 334.2 s | **7.6 s** |
| …dead air (spinner gone, no grid) | **312.1 s** | **0.0 s** |
| M&A / All History (the reported case) — grid ready | — | **6.2 s**, dead air **0.0 s** |
| Excel completeness, 23 cats | 101,786,300 B | **101,786,465 B / 152,721 rows** |
| Excel completeness, M&A | 5,930,988 B | **5,930,990 B / 8,412 rows** (42 s, spinner shown) |
| "Load more events" | — | 500 → **1,000** |
| `/calendar` (shares the loader) | — | overlay clears exactly when its iframe paints (13.1 s) |
| Companies-mode Excel | — | 14,873 B valid |

Nothing was lost by deferring: the workbooks are byte-for-byte the same size and row
count as the eager build (the few-byte delta is the generated-at timestamp in the
subtitle row).

## 6. Out of scope — flagged separately

1. **Still not exercised by a click** (same helper as paths that are proven, so low
   risk, but stated plainly rather than implied): the forecasting *All-Companies*
   Excel (`forecasting.py:3195`, `auto_click=True` — though the equivalent auto-click
   flow is proven on company_filings and market_data), and the screening
   *People*-results Excel. `earnings_calls._render_excel_js_download` has **zero call
   sites** — dead code, nothing to exercise.
2. **`/media/` through the corporate hostname is unverified.**
   `marketdata-stg.coresight.com` is NXDOMAIN from a dev Mac, so only the App Service
   front door (`…azurewebsites.net`) could be tested — there `/media/<bogus>` returns
   Tornado's own 404 body, i.e. the handler is reachable. Confirm from a machine that
   resolves the public host:
   `curl -o /dev/null -w "%{http_code}\n" https://marketdata-stg.coresight.com/media/deadbeef.xlsx`
   → **404 is the good result**. Anything else means the proxy blocks `/media/` and a
   small-payload fallback is needed. The stale claim that it is blocked came from a
   comment in `earnings_calls.py` (~line 900), not from measurement.
3. **Corrupt filing PDFs (pre-existing, unrelated).** Some cached filing "PDFs" are
   Burberry CDN *"Access Denied"* HTML pages saved with a `.pdf` extension (~333 B).
   They can never render in any viewer. Every BRBY file sampled locally was one.
4. **Local dev needs `fpdf2`** (`requirements.txt` line 62) or the earnings_calls
   transcript Download silently does not render (`No module named 'fpdf'`).
5. **171s to first paint** on the all-history key-devs screen — that is
   `_build_keydevs_excel_fast` streaming 146k rows / 102 MB synchronously inside the
   `kd_sig` block, before the grid renders. Pre-existing, unrelated to this fix.
   A 102 MB xlsx is also not usable in Excel for most people; consider building it
   lazily on click, or capping/CSV-ing above some row count.
