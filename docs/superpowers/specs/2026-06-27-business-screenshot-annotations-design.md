# Business Screenshot Annotations — Design Spec

**Date:** 2026-06-27  
**Scope:** Google Drawings–style callouts for MDP business documentation screenshots

## Goal

Replace circle-dot markers with **numbered box + curved arrow** annotations using Coresight red (#D62E2F), embedded in markdown guides and regenerated PDFs.

## Architecture

```
callouts_config.json  →  batch_annotate_business_screenshots.py
                              ↓
                    annotate_business_screenshot.py (PIL)
                              ↓
                    *-annotated.png  →  *.md embeds  →  merge_business_doc_pdf.py
```

## Callout format

CLI: `--callout "sx,sy,bx,by,bw,bh,num[,label]"`

| Field | Meaning |
|-------|---------|
| sx,sy | Badge / arrow start (label side) |
| bx,by,bw,bh | Rounded rectangle on UI element |
| num | Callout number (1–15 → ①②③…) |
| label | Optional short text beside badge |

## Rendering (PIL)

1. Semi-transparent rounded rectangle (stroke 3px, fill ~18% opacity)
2. Quadratic Bézier arrow from badge edge to nearest box edge
3. Numbered circle badge at (sx,sy)
4. Optional label pill beside badge

Colors: RED `#D62E2F`, ORANGE `#E65100` (alternating by callout number).

## Batch config

`docs/business-documentation/_screenshots/v2/callouts_config.json` lists per-image callouts.  
`docs/business-documentation/_screenshots/v2/CALLOUTS.md` documents coordinates for authors.

## Skip rules

Do not annotate screenshots with:
- "Unable to load" / error banners
- Blank body (loading-only)
- No real UI data (empty screening, failed filings)

Guides use **Screenshot pending — connect to staging data** for those pages.

## Testing

```bash
.venv/bin/python scripts/annotate_business_screenshot.py INPUT.png -o OUTPUT.png --callout "..."
.venv/bin/python scripts/batch_annotate_business_screenshots.py
.venv/bin/python scripts/merge_business_doc_pdf.py --also-regenerate-individual
```

Validate: embedded images in PDF, no blank pages, TOC links resolve.

## Rollout

- 15 source PNGs annotated (home v2, login, newsroom, live transcript, access management, nav)
- Markdown guides updated with `*-annotated.png` and `| Callout | What it is | What to do |` tables
- Merged PDF regenerated (~171 pages, 186 embedded images)
