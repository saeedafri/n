# Header Font Consistency + White-Space-Above-Header-on-Filter-Change — Design & Fix

**Date:** 2026-07-10
**Author:** Claude (Opus 4.8) — systematic-debugging + verification-before-completion
**Scope:** Two production UI/UX defects reported by client, fixed at root, verified end-to-end in local UI (Playwright, port 8600 — other agents' servers on 8501/8591-8595 left untouched).

---

## Issue 1 — Header/nav font & boldness looks different on each page

### Symptom
Walking page-to-page, the top navigation ("Market Data Dashboard / Earnings Calls / Calendar / Screening / News") renders in a **different weight/shape on some pages** than on `market_data`. Client wants every page to match `market_data`.

### Root cause (proven, not guessed)
`components/navigation.py::render_header` styles the nav in `font-family:'Roboto'; font-weight:500` (logo/company/footer text use Montserrat), **but the header component does not load those fonts itself** — it relies on each *page* importing them. Pages import **different Google-Font subsets, or none**:

| Page | Roboto import | Result |
|------|---------------|--------|
| market_data, earnings_calls, newsroom, home | Roboto 400/500/600/700 | Roboto applied ✔ |
| **earnings_calendar** | Inter + Montserrat only (**no Roboto**) | nav falls back to OS font �’ |
| **screening** | Material Symbols only (**no Roboto**) | nav falls back to OS font ✗ |

**Evidence — rendered width of "Market Data Dashboard" (ground truth; `document.fonts.check()` lies and returns `true` even with no Roboto face):**

| Page | Before fix | After fix |
|------|-----------|-----------|
| market_data | 189.9px (Roboto) | 189.9px |
| earnings_calls | 189.9px | 189.9px |
| newsroom | 189.9px | 189.9px |
| **screening** | **191.1px (OS fallback)** | **189.9px (Roboto)** |
| **earnings_calendar** | **191.1px (OS fallback)** | **189.9px (Roboto)** |

On the client's macOS Chrome the fallback is Helvetica/Arial — a visibly different weight/shape than Roboto, hence "boldness feels different per page."

### Fix
Make the header **self-contained** — import its own fonts inside `render_header`'s `<style>` block (first rule; `@import` must precede other rules):

```css
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&family=Montserrat:wght@400;500;600;700&display=swap');
```

`st.html()` injects into the **main document** (not an iframe), so this loads the fonts globally on whatever page hosts the header — the nav now renders identically everywhere, independent of the page's own imports. CDN `@import` is proven to survive the STG proxy (same mechanism as the Material-icon-font fix).

**File:** `app/components/navigation.py` (in `render_header`).

---

## Issue 2 — White space appears above the header when changing a tab or filter (any page)

### Symptom
On `market_data` (and any page), changing a tab or a filter (e.g. Period Type Quarterly→Annual on the Forecasting tab) drops the sticky header down and opens a **white strip above the header**. Not present before the change.

### Root cause (proven)
The header is `position:sticky; top:0` and is the first *visible* element inside a Streamlit `stVerticalBlock` (a **flexbox column with `gap:1rem` = 16px**). Filter-state persistence renders **invisible, data-only components in the normal flow BEFORE the header**:

1. **`streamlit_local_storage` component** (`setItem` default `key="set"`, plus `getAll`, …). The iframe is `height=0`, but Streamlit's wrapping `stElementContainer` occupies **~25.6px** in flow.
2. **`save_to_local_storage_js`** — a *redundant* second write via `components.html(height=0,width=0)`. Its `st.iframe` is a real (display:block) flex item; even at height 0 the block's 1rem gap opens **16px** above the next item.

Combined, on a filter-change save these push the sticky header down **~57.6px** (measured: `headerTop` 0 → 57.6). This exact bug was already documented (and only partially worked around) in `earnings_calls.py` ("stCustomComponentV1 iframe … inserted at position [0] … BEFORE the custom header … ~58px blank strip") — `earnings_calls` saves at end-of-render to dodge it; `market_data` does not, so it exhibits the bug.

### Fix (two parts, both global — every page)
1. **CSS (`components/styles.py::_build_sidebar_css`, injected on every page via `hide_sidebar()`):** pull the invisible `streamlit_local_storage` iframe's container fully **out of flow** (`position:fixed; left:-99999px` — the proven `.st-key-cs_hidden_nav` idiom). Component JS + Streamlit postMessage are unaffected by CSS position, so persistence still works; the container reserves **zero** layout space.

   ```css
   div[data-testid="stElementContainer"]:has(iframe[title="streamlit_local_storage.st_local_storage"]) {
     position: fixed !important; left: -99999px !important; top: 0 !important;
     width: 1px !important; height: 1px !important; overflow: hidden !important;
     opacity: 0 !important; pointer-events: none !important; margin: 0 !important; padding: 0 !important;
   }
   ```

2. **Remove the redundant JS iframe (`utils/local_storage_manager.py`):** `save_to_local_storage_js` now runs **only when the component is unavailable** (true fallback). When the component is present (production), `setItem` already persisted the identical data, so the second `components.html` iframe is pure redundancy — dropping it removes the "position [0]" flex item entirely. Applied at both call sites (`LocalStorageManager.save_to_local_storage`, `save_earnings_calls_state`).

### Behaviour preserved
`setItem` (the primary write) is untouched. Verified after the change: `localStorage['sip_app_state']` is written on a filter change (persistence intact).

---

## Verification (Playwright, local UI, port 8600)

| Check | Before | After |
|-------|--------|-------|
| market_data — Period Type change, `headerTop` | 0 → **57.6px** | 0 → **0** |
| market_data — 2nd Period Type change | — | **0** |
| market_data — tab switches (Key Stats, Income Statement, Ratios, Cash Flow, Forecasting) | — | **0** every tab |
| newsroom — filter change (another page) | — | **0** |
| localStorage persistence (`sip_app_state` written on change) | ✔ | ✔ (unbroken) |
| Nav rendered width — screening / earnings_calendar | 191.1px (fallback) | **189.9px (Roboto)** — matches market_data |
| Server logs (errors/exceptions/duplicate-key) | — | **none** |

---

## Files changed
- `app/components/navigation.py` — header self-imports Roboto + Montserrat (Issue 1).
- `app/components/styles.py` — global CSS collapses the `streamlit_local_storage` iframe container out of flow (Issue 2, part 1).
- `app/utils/local_storage_manager.py` — JS-fallback write gated to component-absent case at both save sites (Issue 2, part 2).

## Rollout / risk
- CSS-only + a narrowed fallback path; no schema/DB/auth impact.
- `:has()` is already used elsewhere in the codebase (styles.py, loading.py) — supported on the client's Chrome.
- STG proxy: CDN `@import` fonts already proven to work (Material-icon-font precedent).
- Existing `earnings_calls` end-of-render save workaround left in place (now unnecessary but harmless).
