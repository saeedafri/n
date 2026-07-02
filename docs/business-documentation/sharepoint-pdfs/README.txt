Market Data Portal — Business User Guide (PDFs for SharePoint)
========================================================================

PDF copies for end-user documentation. Markdown sources remain in
business-documentation/ and are NOT required on SharePoint.

Folder layout
---------------
  README.txt (this file)
  Market-Data-Portal-User-Guide-Complete.pdf
      Complete merged user guide.
  pages/
      Individual section PDFs (login, home, market-data, etc.).

Excluded from SharePoint mirror: FINAL-AUDIT.pdf, QA-REPORT.pdf, SCREENSHOT-GAPS.pdf
(internal QA artifacts only).

Regenerating
------------
  python scripts/generate_business_doc_pdf.py
  python scripts/merge_business_doc_pdf.py
  python scripts/sync_technical_pdfs_sharepoint.py --business

Last sync: 19 file(s) copied.
