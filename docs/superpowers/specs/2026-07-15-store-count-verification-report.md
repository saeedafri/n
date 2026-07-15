# Store Count (Worldwide) — Verification Report for Business Team — 15 Jul 2026

Shareable web version: https://claude.ai/code/artifact/366d9490-79af-4615-9dc0-053a9698dce4
(private by default — share from the page's share menu)

**Summary:** 341 companies audited · 137 now show store counts · 18 major retailers added ·
40+ values manually verified against public filings · 0 known wrong values displayed.

## 1 · Headline fixes (before → now, vs official)

| Company | Before (wrong) | Now in portal | Official | Status |
|---|---|---|---|---|
| lululemon | 58 (outlets only) | **811** | 811 (Feb 1, 2026) | MATCH |
| Urban Outfitters | 9 | **784** | 784 company-owned (Jan 31, 2026) | MATCH |
| DICK'S | 42 | **856** (FY2025) | 856 (Feb 1, 2025) | MATCH |
| TJX | 5,890 (fabricated) | 3,305 (FY2021 validated) | 3,305 US (Jan 2021) | PARTIAL — §5 |
| Primo Brands | 200,000 | hidden | not a store retailer | REMOVED |
| Build-A-Bear | 553 (subset, blocked) | **662** | 662 total (Feb 2026) | MATCH |
| Realty Income | 15,476 (wrong yr) | 12,237 (FY2022) | 12,237 (Dec 2022) | MATCH |
| Chewy/Funko/Unifi | 26 / 3 / 15 | hidden | warehouses/offices | REMOVED |

## 2 · Manual multi-year verification (internet sources vs portal)

| Company | FYE | Portal | Official | Source |
|---|---|---|---|---|
| McDonald's | Dec 2023 / 2024 / 2025 | 41,822 / 43,477 / 45,356 | 41,822 / 43,477 / 45,356 | McDonald's corporate "Restaurants by Market" PDFs |
| Dollar General | Feb 2024 / Jan 2025 / Jan 2026 | 19,986 / 20,594 / 20,893 | 19,986 / 20,594 / 20,893 | DG earnings releases / 10-K |
| Casey's | Apr 2024 / 2025 / 2026 | 2,658 / 2,904 / 2,944 | 2,658 / 2,904 / 2,944 | Casey's 10-Ks |
| lululemon | Jan 2022 / Jan 2024 / Feb 2025 / Feb 2026 | 574 / 711 / 767 / 811 | 574 / 711 / 767 / 811 | lululemon releases / Statista |
| Lowe's | Feb 2024 / Jan 2025 | 1,746 / 1,748 | 1,746 / 1,748 | Lowe's 10-Ks |
| Wendy's | Dec 2023 / Dec 2024 | 7,240 / 7,240 (flat yr) | 7,240 / 7,240 | Wendy's 10-Ks |
| Yum! Brands | Dec 2024 / 2025 | 61,000 / 63,000 | "surpassed 61,000" / "over 63,000" | Yum annual report/10-K |
| CVS | Dec 2021 / Dec 2025 | 9,900 / 9,000 | ≈9,900 / ≈9,000 | CVS 10-K (900-store closure program) |
| Ollie's | Jan 2026 | 645 | 645 | Q4 FY2025 release |
| Boot Barn | Mar 2026 | 539 | 539 | 10-K FY2026 |
| Walmart | Jan 2026 | 10,955 | 10,955 | 10-K |
| Costco | Aug 2020 / Aug 2025 | 795 / 914 | 795 / 914 | 10-Ks |
| Ulta | Feb 2025 / Jan 2026 | 1,445 / 1,591 | 1,445 / 1,591 | 10-Ks |
| Target | Jan 2026 | 1,995 | 1,995 | 10-K |
| Kroger | Jan 2026 | 2,697 | 2,697 | FY2025 results |
| O'Reilly | Dec 2025 | 6,585 | 6,585 | Q4 2025 results |
| Home Depot | Feb 2026 | 2,359 | 2,359 | Q4 2025 8-K |
| Best Buy | Jan 2026 | 1,068 | 926 US + 142 CA | 10-K |
| Advance Auto | Jan 2026 | 4,305 | 4,305 | Q4 2025 release |
| Abercrombie | Jan 2026 | 829 | 829 | 10-K |
| Chipotle | Dec 2025 | 4,042 | 4,042 company-operated | Q4 2025 release |
| URBN | Jan 2025 | 733 | 724 brand stores + 9 Menus & Venues | 10-K FY2025 |

**All checked values matched.**

## 3 · New coverage (18 companies, previously empty)
YUM 63,000 · MCD 45,356 · DG 20,893 · DLTR 16,700 · CVS 9,000 · WEN 7,397 · CASY 2,944 ·
VVV 2,200 · LOW 1,759 · AEO 1,500 · OLLI 645 · BOOT 539 · BKE 440 · SHOO 399 · ASO 322 ·
LZB 230 · DLTH 63 · MOV 53

## 4 · Reliability mechanics
- Primary source: company's own XBRL tagging (us-gaap:NumberOfStores family).
- AI-extracted values must reproduce from their own source sentence or are hidden.
- Guards: ≥3 years, recency, FY-only (no interim quarters), median outlier drop,
  >3x jump rejection, larger-validated-scope preference, DRI exclusion.
- Deterministic duplicate resolution; screening double-count fixed.
- Label: "Store Count (Worldwide, if applicable)".

## 5 · Known gaps (need pipeline re-ingest; DB intentionally untouched)
- TJX recent years hidden (real 5,214 Jan 2026); DKS FY2026 hidden (real 3,195 with
  Foot Locker); RH/SHW/UPBD latest reliable year 2021-22.
- ~190 companies never disclose machine-readable store counts (non-retail or non-SEC
  filers) — correctly show no data.
