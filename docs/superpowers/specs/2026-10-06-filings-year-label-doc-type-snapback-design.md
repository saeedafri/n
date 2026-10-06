# Company Filings — 10-K snaps back to 10-Q-Q1 (Year label bug)

**Date:** 2026-10-06 · **Page:** `/company_filings` · **File:** `app/pages/company_filings.py`

## Symptom

CMG → Year **2023** → Document Type **10-K**:
- the change sometimes did not stick on the first click;
- once 10-K stuck, typing in the Search box (e.g. "revenue") flipped it back to **10-Q-Q1**.

## Root cause

The Year selectbox used `format_func=lambda y: _fiscal_label_for(company, doc_type, y)`,
so the **label** of each bucket year depended on the selected document type.

The bucket year (`storage_year`) and the DEI fiscal year (`fiscal_year`) differ for 10-Ks:
a 10-K is filed the year after the fiscal year it covers.

| CMG bucket | label under 10-Q | label under 10-K |
|---|---|---|
| 2024 | 2024 | **2023** |
| 2023 | **2023** | 2022 |

Streamlit 1.55 stores a selectbox value as its **formatted label string**
(`SelectboxSerde`, `value_type="string_value"`). The browser keeps the string `"2023"`.
After the switch to 10-K, the server deserializes `"2023"` with the 10-K labels, which gives
**bucket 2024**. On the next rerun (any widget, including the search box), Streamlit sees a
"changed" year and fires `_on_year_change(new_year=2024)`. That callback resets the document
to the best one for 2024, which is **10-Q-Q1**.

### Evidence (`[PAGE_FILINGS_SEL]` logs, local STG DB, before the fix)

```
on_year_change      new_year=2023 prev_year=2026          ← user picks 2023
on_doc_type_change  new_doc=10-K  year=2023               ← user picks 10-K
rendered            doc_type=10-K year=2023 year_label=2022   ← label silently changed
on_year_change      new_year=2024 prev_year=2023 doc_before=10-K  ← search rerun, nobody touched Year
rendered            doc_type=10-Q-Q1 year=2024
```

## Fix

Removed the doc-type-dependent `format_func` from the Year selectbox. Year labels are now the
bucket year, so each label depends only on its option value. The filing header already shows
the DEI fiscal year, e.g. `2022 · Annual · Feb 9, 2023 · Fiscal Period: FY2022`.

**Rule:** a keyed `st.selectbox` label must be a pure function of the option. It must never
depend on another widget's value.

## Logging

`_on_doc_type_change` and `_on_year_change` each emit one `[PAGE_FILINGS_SEL]` line (the
`[PAGE_` prefix passes the WARNING-pinned server logger). They fire only on a user change, so
a spurious `on_year_change` that nobody clicked is visible straight away.

## Testing

`.claude/dev/filings_doc_year_check.py <port>` (Playwright) runs:
CMG → 2023 → 10-K → search → 10-Q-Q1 → 10-K → search.

After the fix, every step holds 10-K/2023, and the only `on_year_change` in the log is the one
the user triggered.

## Rollout

One-file change (`company_filings.py`). No DB or config changes. UX change: under 10-K, the
Year dropdown shows the bucket year (2023), not the fiscal year (2022). The fiscal year stays
in the header.
