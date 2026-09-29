# Market Size Forecasting — business overview

One page for anyone who needs to know what the tool does and how far to trust
its number. No code, no statistics.

Diagram: `market-size-forecasting-business.html`

---

## What it is for

An analyst has a history of a market — retail sales, category spend, whatever the
request is — and needs a credible forward number.

The tool takes that history and produces a forecast, together with an honest
statement of how confident we should be in it.

## Where the data comes from

**The analyst uploads it.** Market size requests arrive from different
third-party sources every time and vary by request, so there is no single feed to
connect. This is a deliberate choice, confirmed with the data science team, not a
gap.

The one live connection is to the **US Federal Reserve economic data service**, which
supplies 32 economic indicators used as predictors.

## The five things that happen

1. **Upload the history** — a spreadsheet or CSV of periods and values.
2. **Check and clean** — the tool identifies the date and value columns, works out
   whether the data is weekly, monthly, quarterly or annual, merges duplicate
   periods and fills gaps so there is exactly one value per period.
3. **Choose** — which models to run and how far ahead to project.
4. **Run** — three models fit the same history in three different ways.
5. **Compare** — all three are scored, and the result is read as a range.

## Why three models, not one

| Model | The question it answers | When it is the right one |
|---|---|---|
| **SARIMAX** | Does the wider US economy explain this market? | Scenario questions — what happens if employment softens |
| **SARIMA** | What does this market's own history say? | The dependable baseline; no external dependency to fail |
| **Prophet** | Where did the underlying trend change? | Markets with a clear break, such as 2020 |

A single model gives a number with nothing to judge it against. Three
independent models landing close together is real evidence; three models
disagreeing is a warning we would otherwise never see.

## How to read the answer

Each model is scored on recent periods **it was never shown during fitting**. That
is what makes the ranking meaningful rather than a measure of how well a model
memorised the past.

The page then reports how far apart the three forecasts are:

- **Within 5%** — the models agree. Quote the midpoint as a working forecast.
- **More than 5%** — say so. Give a range, and flag that the structural drivers
  need a look before anyone commits to a single figure.

**Always quote a band across the horizon, never a single number.** The confidence
interval is part of the answer, not a footnote.

## What it cannot do

- **Non-US markets have no economic predictors here.** All 32 indicators are US
  series. For a UK or India market size, SARIMAX has nothing appropriate to work
  with — the other two models still apply.
- **It does not know about anything outside the history.** A competitor entering,
  a regulation changing, a product launch — none of that is in the data unless it
  has already shown up in past numbers.
- **The far end of a forecast is weaker than the near end.** This is true of any
  forecast, and the widening confidence band shows it.
- **A long horizon is not more information.** Projecting further ahead does not
  add knowledge; it only widens the uncertainty.

## What we need from the analyst

A clean history, with dates stamped at the **end** of each period —
`31 January 2016`, not `1 January 2016`. Everything else the tool handles.
