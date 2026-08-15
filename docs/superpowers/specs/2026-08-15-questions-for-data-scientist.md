# Questions for the data scientist — MDP Forecasting System

Context: the revised annual and quarterly models have been ported into the app.
Both reproduce the reference reports exactly (347/347 annual, 97/98 quarterly),
so these are genuine open questions, not defects.

---

## 1. Coty: negative revenue in Q4 2020 — please confirm the handling

`Coty Inc. (NYSE:COTY)` reports **revenue of −1098.0** for Q4 2020 in the
quarterly input file.

- **Your script models it as-is.** It survives into the deseasonalised series and
  shifts the detected structural break to Q1 2020.
- **We drop it** as invalid, on the basis that revenue cannot be negative and the
  value poisons growth rates, seasonal indices and the break test.
- Effect: the break moves to Q2 2020, and Coty's backtest error **improves from
  6.8% to 4.4%**.

**Ask:** do you agree revenue ≤ 0 should be discarded as a data error rather than
modelled? If yes, we would like the same rule in your script so the two stay
identical. If no, tell us what the negative figure represents.

This is the **only** company where our output differs from yours.

---

## 2. Should the source data contain negative revenue at all?

Follow-on from #1: is `-1098.0` a restatement artifact, a sign error in the
ingestion, or a genuine reported figure (e.g. a contra-revenue quarter)? If it is
an ingestion bug, the fix belongs upstream in the data pipeline rather than in
either model.

---

## 3. Please confirm the deliberate annual/quarterly ordering difference

The two scripts order the data-quality steps **differently**:

| | Order |
|---|---|
| Quarterly | outlier screening → structural break |
| Annual | structural break → outlier screening |

We assumed at first that annual was an oversight and "fixed" it to match
quarterly. **It made results significantly worse** and we reverted:

| Company | MAPE, annual order (yours) | MAPE, quarterly order |
|---|---|---|
| Dollar Tree (DLTR) | 1.8% | 17.5% |
| Bath & Body Works (BBWI) | 4.1% | 17.1% |
| Coty (COTY) | 6.4% | 15.2% |
| Western Digital (WDC) | — | forecast goes **negative** |

Our reading: a structural break *is* a large decline, so on a ~20-point annual
series the outlier test flags the break year itself and screening first deletes
the evidence of the thing being detected. Quarterly can afford the other order
because 80+ points distinguish a one-off quarter from a permanent shift.

**Ask:** confirm this is deliberate, so we can document it as intended rather
than as an inconsistency someone "fixes" again later.

---

## 4. Scenario columns — we kept them, you dropped them

You replaced the 25th/50th/75th-percentile scenario columns with the plausibility
flag, because compounding them exploded the ranges.

In the portal those columns feed the overview chart, the scenario chart, the
forecast table, the Excel export and the email report, so we **kept** them and
made the plausibility flag the authoritative trust signal instead.

**Ask:** are you comfortable with scenarios remaining visible as a "spread around
history" framing, or do you want them removed from the product too? If they stay,
is a dampened/bounded compounding worth doing so the ranges stay defensible?

---

## 5. `needs_review` is stricter in the app than in your Cell 2

Your `validate_projections` sets `needs_review` from: implausible confidence,
MAPE > 25%, or any flag reason. That means a **FLAT** company (one reported year,
flat carry-forward) comes through with `needs_review = False`.

We additionally treat `NO_DATA` / `FLAT` / `MINIMAL` tiers as needing review,
because a one-year carry-forward shown in a UI without a caution label reads as a
real forecast.

**Ask:** agree with the stricter rule? Should `LIMITED` (3–5 years) also be
flagged by default, or is the MAPE gate enough there?

---

## 6. Thresholds — are these final, and should they differ by sector?

Currently shared across every company:

| Constant | Annual | Quarterly |
|---|---|---|
| Outlier z-score | 1.5 | 2.2 |
| Outlier IQR multiplier | 1.5 | 2.0 |
| Structural break | 40% decline, 60% persistence | same |
| Implausible growth | > 60%/yr | same |
| Implausible decline | worse than −25%/yr | same |
| High MAPE | > 25% | same |

**Ask:** the >60%/yr implausibility gate flags genuinely fast-growing companies
(EVGO shows an implied 85%/yr and is flagged red). Is flagging them correct
behaviour, or should high-growth names get a different threshold? Same question
for retail vs. tech, where normal growth rates differ a lot.

---

## 7. Forecast horizon

Your scripts target a fixed **2031** end year. The portal forecasts a **rolling**
horizon from each company's last actual (5 years annual / 20 quarters quarterly),
because it is a live product rather than a point-in-time report.

**Ask:** confirm the rolling horizon is fine, and that there is no reason the
model needs a fixed endpoint.

---

## 8. Refresh cadence

Forecasts recompute automatically twice daily (06:00 and 18:00 Asia/Kolkata),
annual and quarterly in the same run. Quarterly had been paused since 2026-07-13
and is now re-enabled.

**Ask:** is twice daily appropriate, given actuals only change when a company
reports? And should the Data team be told before quarterly goes live, since they
requested the original pause?

---

## 9. Anything intentionally left out?

Your change document lists seven components. We ported all seven. **Ask:** is
there anything in your working notes that did not make it into the document —
particularly around the ensemble weighting (currently a plain mean of the top 3
by MAPE; a weighted blend was not specified).
