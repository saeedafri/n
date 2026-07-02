# Screenshot annotation — arrows-only redesign

**Date:** 2026-06-27  
**Status:** Implemented

## Problem

Business documentation screenshots used bounding boxes + arrows. Coordinates in `callouts_config.json` were wrong for several pages (notably `login-02-coresight-sso` where Email/Password arrows landed on the Log In button because y-coordinates were ~520–820 instead of ~293–527 on 1500×1708 PNGs).

## Solution

1. Rewrote `scripts/annotate_business_screenshot.py` to draw **only** numbered badges + quadratic-bezier arrows (no boxes).
2. New format: `badge_x,badge_y,target_x,target_y,number[,label]`
3. Re-measured targets from raw PNGs (pixel probes + crosshair debug overlays).
4. Regenerated all 15 annotated screenshots; documented coordinates in `CALLOUTS.md` and checklist in `VERIFICATION.md`.

## Arrow format

```bash
--arrow "80,368,749,368,2,Sign in"
```

## Verification

- Crosshair overlay script confirmed login-02 targets hit Email (749,293), Password (749,336), Security (749,378), Log In (749,527).
- Visual review of `-annotated.png` outputs — see `_screenshots/v2/VERIFICATION.md`.

## Skipped screenshots

19 raw captures remain unusable (staging DB empty/error states) — listed in `callouts_config.json` `skipped` array.

## Rollout

Regenerate annotations: `.venv/bin/python scripts/batch_annotate_business_screenshots.py`  
Regenerate PDFs (after VERIFICATION.md all YES): `.venv/bin/python scripts/merge_business_doc_pdf.py --also-regenerate-individual`
