#!/usr/bin/env python3
"""Replace generic auto-generated Mermaid captions with readable prose."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC_DIR = REPO / "docs" / "technical-documentation"

REPLACEMENTS = {
    "This diagram shows the architecture overview for this module.": (
        "High-level map from browser request through `main.py`, application layers, and external integrations."
    ),
    "This diagram shows the data flow (typical authenticated page) for this module.": (
        "Typical authenticated request: cookie hydrate in `main.py`, optional `require_auth()` on the page, then a repository read via the AUTOCOMMIT read engine."
    ),
    "This diagram shows the login flow (`authmanager.login`) for this module.": (
        "`AuthManager.login` creates a session, writes an audit row, stores `id_token` off-cookie, and hands off a slim `auth_session` cookie to the browser."
    ),
    "This diagram shows the forecasting pipeline for this module.": (
        "Revenue forecasts flow from the forecasting engine into `coreiq_model_forecasts`, with admin sync and Key Stats consumption paths."
    ),
    "This diagram shows the architecture for this module.": (
        "Cross-cutting utilities consumed by pages, core modules, and background warmups."
    ),
    "This diagram shows the earnings alert email pipeline for this module.": (
        "Background dispatcher loads enabled preferences, dedupes deliveries, and sends SMTP alerts for upcoming earnings."
    ),
    "This diagram shows the 2. architecture overview for this module.": (
        "Page architecture: UI orchestration, data repositories, and external services for this feature."
    ),
    "This diagram shows the 2. high-level architecture for this module.": (
        "Filing viewer architecture: SEC XBRL path, non-SEC blob PDFs, and LLM fallback extraction."
    ),
    "This diagram shows the 3. high-level architecture for this module.": (
        "Admin upload page: ACL gate, Azure Blob storage, and audit logging for non-SEC filings."
    ),
    "This diagram shows the 7.1 progressive pipeline (`recompute_working_set`) for this module.": (
        "Screening progressively filters the company universe — each active criterion narrows the working set."
    ),
    "This diagram shows the 12. flowcharts for this module.": (
        "End-to-end user journeys on this page, from load through interaction to data fetch."
    ),
    "This diagram shows the 12.6 export flow for this module.": (
        "Export builds a CSV from the current working set and triggers a browser download."
    ),
    "This diagram shows the save pipeline for this module.": (
        "Retailer save validates ACL, upserts company rows, and writes an audit log entry."
    ),
    "This diagram shows the flow for this module.": (
        "Bulk CSV upload validates rows, inserts companies, and reports per-row results."
    ),
    "This diagram shows the 7. filter pipeline for this module.": (
        "News articles pass through date chunking, ticker/sector filters, and deduplication before render."
    ),
    "This diagram shows the 8.1 keyword search flow for this module.": (
        "Keyword search queries MySQL FULLTEXT indexes and merges AV + YFinance article sources."
    ),
    "This diagram shows the 16. main entry flow for this module.": (
        "`main()` entry point: bootstrap UI, read session/query state, fetch data, and render widgets."
    ),
    "This diagram shows the page load flow — `main()` for this module.": (
        "`main()` loads forecast data for the selected ticker and renders model cards plus refresh controls."
    ),
    "This diagram shows the 8. filing load flow for this module.": (
        "Selecting a filing loads metrics from XBRL tables, with optional LLM extraction on cache miss."
    ),
    "This diagram shows the 9.3 xbrl extract / metric search flow for this module.": (
        "Metric search checks filing tables first, then may invoke GPT-4o-mini extraction for missing values."
    ),
    "This diagram shows the 11. pdf viewing flow (non-sec) for this module.": (
        "Non-SEC PDFs stream from Azure Blob via SAS URL embedded in the Streamlit viewer."
    ),
    "This diagram shows the 12. sec html → pdf download flow for this module.": (
        "SEC HTML filings are converted server-side to PDF for download via the PDF utility."
    ),
    "This diagram shows the 8. upload flow for this module.": (
        "Upload writes the PDF to Azure Blob, records metadata in MySQL, and logs the blob audit entry."
    ),
    "This diagram shows the 9. delete flow for this module.": (
        "Delete removes the blob object, soft-deletes metadata, and records an audit log row."
    ),
    "This diagram shows the 10. browse / download flow for this module.": (
        "Browse lists non-SEC filings from the DB; download generates a time-limited blob SAS URL."
    ),
}


def main() -> None:
    for path in sorted(DOC_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        original = text
        for old, new in REPLACEMENTS.items():
            text = text.replace(old, new)
        if text != original:
            path.write_text(text, encoding="utf-8")
            print(f"Fixed captions: {path.name}")


if __name__ == "__main__":
    main()
