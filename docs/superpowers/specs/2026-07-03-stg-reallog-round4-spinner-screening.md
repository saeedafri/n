# STG Round-4 — Branded Loaders, Screening Fix, Persist-Until-Render

**Date:** 2026-07-03
**Input:** 8 screenshots (#11–#18) + two real errors on STG. Each fix reproduced &
verified in the local UI (Playwright) before claiming done.

---

## 1. Screening "Could not load screening results" (#17) — REAL BUG, FIXED
- **Reproduced** locally (Companies mode + Key-Dev criterion → Show Results).
- **Root cause (server-log):** `NameError: name 'log_warning' is not defined` at
  `screening.py:5885` in `_render_results`. A pre-existing `log_warning(...)` call
  (the SHOW_RESULTS trace) was never imported → every "Show Results" click in Company
  Screening raised → caught → "Could not load screening results."
- **Fix:** add `log_warning` (+ fallback stubs for `log_warning`/`log_render_complete`)
  to the screening import. **Verified:** 0 NameErrors, results render.

## 2. Branded Coresight loader everywhere + dynamic text (#12,#13,#16)
- New `render_page_loader(label)` in `components/loading.py` renders the **branded
  Coresight card** (logo + red spinner + label) — the same look as the boot splash,
  replacing the small "lame" spinner.
- **Dynamic per-tab text** on market_data: "Loading Income Statement", "Loading Key
  Stats", … (from the `tab` query param). **Verified** label = "Loading Income Statement".
- **earnings_calls** now uses the branded card ("Loading Earnings Calls"). **Verified.**
- **Boot splash (page switches, #13):** `core/boot_overlay.py` bumped to **v2** — adds a
  dynamic label read from the URL (`/earnings_calls` → "Loading Earnings Calls", etc.).
  The index.html patcher now **upgrades** an older version in place (strips v1, injects
  v2). **Verified** served index.html has v2 + the label.

## 3. Persist-until-rendered (#14 calendar, #18 screening blank grid)
- **Problem:** the old spinner cleared when *Python* finished, but AgGrid / the calendar
  render CLIENT-SIDE afterwards → the spinner vanished onto a blank grid ("2000 events
  found" over nothing).
- **Fix:** new `render_sticky_loader(label)` — the branded overlay stays up until the
  heavy iframe (AgGrid / streamlit_calendar) has actually **painted** (real height), then
  a JS remover fades it out. Applied to earnings_calendar and the screening Key-Devs grid.
- **Critical detail found in testing:** an early version hid on a "DOM settled" heuristic,
  which fired during the server-compute wait (DOM idle ⇒ looks settled) and removed the
  overlay while still blank. **Fixed:** hide ONLY when the iframe paints (or a "no
  results" alert appears), never on settle.
- **Verified (Playwright):**
  - Calendar: overlay stayed through the load, iframe painted (h=680), overlay removed
    AFTER — correct.
  - Screening Key Devs: overlay stayed until grid painted (h=520), removed AFTER, 0 errors.
- **Failsafe:** a pure-CSS animation removes the overlay at **22s** even if the JS remover
  never loads (e.g. STG proxy can't serve the component assets) — the overlay can never
  trap the user.

## 4. Filter-change lag (#11)
- The greyed/faded filter widgets during a Period-Type change are Streamlit re-rendering
  the widgets mid-rerun. The branded overlay now shows on EVERY rerun (it's injected at
  the top of `render_page`), so the dimmed filters sit behind the branded "Loading …"
  card instead of looking broken. **Verified:** the overlay appears on a Period-Type change.

## 5. Log noise + non_sec (carried from round-3b, in this push)
- 19 diagnostic `log_error("[TAG]…")` traces in market_data → `log_info` (out of the ERROR
  stream). non_sec cold-timeout → `log_warning`, bounded 30s→15s.

## 6. NOT a code bug — needs STG proxy config (#15)
- **"trouble loading the streamlit_calendar.calendar component … frontend assets due to
  network latency or proxy settings":** Streamlit serves custom-component assets from
  `/component/<name>/…`. The STG reverse proxy is not reliably forwarding those paths (same
  class as the Material-Symbols font issue). **This is a deployment/proxy fix, not code** —
  the nginx/ingress must forward `/component/*` and Streamlit's `/static`/`/media` paths to
  the Streamlit server. Locally the component loads fine. (My sticky loader also uses a
  component; the 22s CSS failsafe means a proxy failure degrades gracefully, never sticks.)

## Files
`components/loading.py`, `core/boot_overlay.py`, `pages/market_data.py`,
`pages/screening.py`, `pages/earnings_calendar.py`, `pages/earnings_calls.py`.
