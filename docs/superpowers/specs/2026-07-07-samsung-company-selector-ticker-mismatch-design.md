# Company Selector — Foreign Ticker Mismatch Crash (Samsung / 005930)

**Date:** 2026-07-07
**Area:** `app/components/navigation.py` (company header selectbox), `app/utils/constants.py` (exchange labels)
**Severity:** High — an entire class of companies (foreign / YFinance-with-exchange) was unselectable on Market Data.

---

## Symptom

On Market Data, the header showed **"Samsung (HKEX:005930)"** but the company dropdown was dead — Samsung (and other foreign listings) could **not** be selected — even though Samsung's data is present in STG and the page rendered when reached by URL.

## Root Cause (proven with DB + code evidence)

Two independent bugs.

### 1. Ticker-representation split → keyed selectbox crash (the real bug)

The company universe and the page routing use **different ticker forms** for foreign listings:

| Layer | Ticker form for Samsung | Built by |
|---|---|---|
| Dropdown option (`CompanyRepository.get_companies`) | **`005930.KS`** → `"Samsung (005930.KS)"` | composite = `ticker` + `.` + `exchange_acronym` (`repository.py` `get_companies_map`) |
| Page header / URL (`?ticker=`) | **`005930`** (bare) | market_data routing |

`get_companies()` (`repository.py:306-308`) **drops the bare-ticker entry** whenever a company has an `exchange_acronym`, keeping only the composite. So the only option for Samsung is `Samsung (005930.KS)`.

`render_company_header` set the selectbox's stored value to `f"{company_name} ({ticker})"` = `"Samsung (005930)"` (bare). **Streamlit `st.selectbox(key=…)` raises `StreamlitAPIException` when its stored value is not among `options`.** That exception was swallowed by the surrounding `try/except` → the dropdown silently failed to render → "unable to select."

Confirmed by replaying the exact `get_companies()` code against STG:
```
Dropdown option(s): ['Samsung (005930.KS)']
'Samsung (005930)'    in options? False   ← value the header set
'Samsung (005930.KS)' in options? True
```

**Blast radius:** every `YFinance` company with a non-empty `exchange_acronym` reached by its bare ticker.

### 2. Wrong exchange label "HKEX"

`get_exchange_code('KS')` returned `'HKEX'`. The South-Korea block of `EXCHANGE_CODE_MAP` had `KSE`, `KR`, `KOSPI` but **no direct `KS`**, so the partial-match fallback matched `'KS'` inside `'HKSE'` (Hong Kong) first. `KS` is yfinance's Korea suffix (`005930.KS`).

## Fix

### `constants.py` — add direct Korea keys
Added `"KS": "KRX"` and `"KSC": "KRX"` to the South-Korea block. Direct lookup runs before the partial-match fallback, so `get_exchange_code('KS') → 'KRX'`.
(Left the pre-existing duplicate-key issue `"KSE" → "BKP"` alone — unrelated to Samsung, flagged separately.)

### `navigation.py` — make the selector value provably valid, always
Replaced the fragile "set once, hope it matches" logic with a resolver that guarantees the selectbox's value is **always** a member of its own options:

1. **Exact ticker match** against option values.
2. **Base-ticker match** (bare `005930` ↔ composite `005930.KS`) — handles the foreign split in both directions.
3. **Inject-if-missing** — if the loaded ticker isn't in `coreiq_companies` at all (data-only ticker), add it as its own option and `log_warning` it, so the picker stays usable and shows the right company.
4. **Sync every run** — assign `st.session_state.company_selector_header = resolved_label` on every render (not just first init), so URL-driven navigation never strands a stale/invalid selection.

After this block `resolved_label ∈ option_list` by construction, so the "value not in options" crash is **structurally impossible** — for Samsung, for any foreign/delisted/data-only ticker, for anything the page can load. `on_company_change` also hardened to `.get(...)` + early-return on blank.

## Verification (real UI, Playwright, STG data via local bypass harness)

- Samsung `?ticker=005930`: header **`Samsung (KRX:005930)`**, selectbox value **`Samsung (005930.KS)`**, no crash, profile shows **Korea Exchange (KRX)** / KRW / South Korea.
- Resolver unit-sim (5 cases): bare-foreign, composite-foreign, data-only (`QVCGA`), and normal SEC (`AAPL`, `JWN`) — selectbox value in options for **every** case.
- Strict multi-company session nav `AAPL → 005930 → 005930.KS → JWN → 005930`: selector tracked every company, correct exchange labels (NASDAQ/NYSE/KRX), **zero** `RENDER_SELECTBOX` crashes, no desync.

## Why this class won't recur

The picker no longer trusts that the loaded ticker matches an option — it **resolves** to a valid option (bare↔composite) and **injects** a fallback when it can't, so no ticker representation the app can produce can crash or blank the selector again. The exchange fix removes the specific mislabel and the pattern (missing direct key → wrong partial match) is documented.
