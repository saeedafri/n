# Technical Documentation Folder Structure

Reusable taxonomy for Coresight product technical documentation. The **Market Data Portal (MDP)** is the reference implementation under `docs/technical-documentation/`.

---

## Folder Count and Purpose

| Folder / file | Count | Purpose |
|---------------|-------|---------|
| `README.md` | 1 | Master index — architecture, auth summary, page registry, PDF commands |
| `FOLDER-STRUCTURE.md` | 1 | This template — copy pattern to new products |
| `00-infrastructure/` | 5 docs | Cross-cutting layers that every page depends on (not route-specific) |
| `pages/` | 16+ docs | One markdown file per Streamlit route in `app/pages/` |
| `_pdf/individual/` | 22 PDFs | Generated per-document exports (gitignored or regenerated in CI) |
| `_pdf/*.pdf` | 1 merged | Production bundle for distribution |
| `_templates/` | 2 files | HTML/CSS templates for Playwright PDF pipeline |
| `_assets/` | static | Logo and branding assets |
| `_diagram_cache/` | cache | Cached Mermaid SVG renders (safe to delete; rebuilt on PDF gen) |
| `_mermaid_config.json` | 1 | Mermaid CLI theme |
| `_mermaid_theme.css` | 1 | Mermaid CLI custom CSS |

**Total content folders:** 2 (`00-infrastructure/`, `pages/`) plus 4 support prefixes (`_pdf/`, `_templates/`, `_assets/`, `_diagram_cache/`).

We deliberately **do not** create empty `components/`, `core/`, `data/`, or `utils/` doc folders — infrastructure docs in `00-infrastructure/` already map to those `app/` paths with numbered reading order.

---

## App Path → Doc Path Mapping

| Application path | Documentation path | MDP example |
|------------------|-------------------|-------------|
| `app/main.py` + auth bootstrap | `00-infrastructure/00-app-bootstrap-and-auth.md` | OIDC, page registration, warmups |
| `app/components/` | `00-infrastructure/01-shared-components.md` | Navigation, styles, charts |
| `app/core/` | `00-infrastructure/02-core-infrastructure.md` | Config, DB, auth, ACL, LLM |
| `app/data/` | `00-infrastructure/03-data-layer.md` | Models, repository, screening |
| `app/utils/` | `00-infrastructure/04-utils-and-caching.md` | Logger, cache, blob, email |
| `app/pages/<module>.py` | `pages/<kebab-name>.md` | `market_data.py` → `pages/market-data.md` |

**Naming rule:** Python `snake_case` module → markdown `kebab-case` filename.

---

## Checklist — Documenting a New Product

Use product display name in titles (e.g. **Market Data Portal (MDP)**), not the git repository name.

1. **Create root**
   - `docs/technical-documentation/README.md` — index with architecture diagram, page registry, PDF commands
   - `docs/technical-documentation/FOLDER-STRUCTURE.md` — copy this file; adjust product name

2. **Infrastructure (read order 00–04)**
   - [ ] `00-app-bootstrap-and-auth.md` — entry point, auth, navigation registration
   - [ ] `01-shared-components.md` — shared UI components
   - [ ] `02-core-infrastructure.md` — config, database, auth manager, ACL
   - [ ] `03-data-layer.md` — models, repositories, services
   - [ ] `04-utils-and-caching.md` — cross-cutting utilities

3. **Pages**
   - [ ] One `pages/<kebab-name>.md` per registered route
   - [ ] Include unregistered but deployed pages as appendix docs (e.g. `forecasting-admin.md`)

4. **PDF pipeline**
   - [ ] Copy `_templates/`, `_assets/`, `_mermaid_config.json`, `_mermaid_theme.css`
   - [ ] Update `scripts/generate_technical_doc_pdf.py` → `ALL_DOCS` list and merged title
   - [ ] Update `scripts/merge_technical_doc_pdf.py` → `MERGE_SEQUENCE` and `MERGED_TITLE`
   - [ ] Run `python scripts/generate_technical_doc_pdf.py` then `python scripts/merge_technical_doc_pdf.py`
   - [ ] Validate: `python scripts/validate_technical_doc_pdfs.py`

5. **Cross-links**
   - [ ] README links use relative paths (`./pages/login.md`, `./00-infrastructure/02-core-infrastructure.md`)
   - [ ] Page docs link to infrastructure with `../00-infrastructure/...`
   - [ ] No references to internal repo name (e.g. avoid `CapIQReplacement`)

6. **Conventions (all products)**
   - Mermaid diagrams first in each doc
   - Table names, env vars, and file paths match production code
   - PDF output only under `_pdf/` — never mix generated PDFs with markdown sources

---

## Scripts Reference

| Script | Input | Output |
|--------|-------|--------|
| `scripts/generate_technical_doc_pdf.py` | All `ALL_DOCS` markdown paths | `_pdf/individual/<stem>.pdf` |
| `scripts/merge_technical_doc_pdf.py` | `MERGE_SEQUENCE` | `_pdf/<Product>-Technical-Documentation-Complete.pdf` |
| `scripts/validate_technical_doc_pdfs.py` | `_pdf/individual/*.pdf` | JSON report + exit code |

---

## MDP File Inventory

- **22** markdown sources (1 README + 5 infrastructure + 16 pages)
- **22** individual PDFs + **1** merged PDF
- **16** registered pages in `main.py` + **1** unregistered appendix (`forecasting-admin`)
