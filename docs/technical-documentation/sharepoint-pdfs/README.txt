Market Data Portal — Technical Documentation (PDFs for SharePoint)
========================================================================

This folder contains PDF copies only. Markdown sources stay in the parent
technical-documentation/ tree and are NOT required on SharePoint.

Folder layout
---------------
  README.txt (this file)
  Market-Data-Portal-Technical-Documentation-Complete.pdf
      Complete merged technical reference (all sections in one file).
  00-infrastructure/
      Shared platform docs: bootstrap/auth, components, core, data layer, utils.
  pages/
      One PDF per Streamlit route (login, market-data, screening, etc.).

SharePoint upload
-------------------
1. Create a document library (or folder) for MDP technical documentation.
2. Upload this entire sharepoint-pdfs/ folder preserving subfolders.
3. Optionally pin the merged PDF at the library root for quick access.
4. Do not upload markdown (.md), _templates/, or _diagram_cache/ — PDFs only.

Regenerating
------------
  python scripts/generate_technical_doc_pdf.py
  python scripts/merge_technical_doc_pdf.py
  python scripts/sync_technical_pdfs_sharepoint.py

The generate and merge scripts refresh this folder automatically after each run.

Last sync: 22 file(s) copied.
