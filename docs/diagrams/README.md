# Market Size Forecasting — documentation set

Everything we understand about `/market_size_forecasting`, at four levels of
detail. Every `.html` here is one self-contained file: open it in a browser,
nothing to install, and **clicking any box opens a panel about that box**.

---

## Pick by audience

| You are | Open | Boxes |
|---|---|---|
| Presenting to the business | `market-size-forecasting-business.html` | 7 |
| Explaining the process to a colleague | `market-size-forecasting-flow.html` | 8 |
| Looking at one tab of the page | `market-size-forecasting-tab-*.html` | 6 each |
| Working on the code | `market-size-forecasting-lines.html` | 26 |
| Reading rather than looking | the Markdown files below | — |

---

## Diagrams

| File | What it shows |
|---|---|
| `market-size-forecasting-business.html` | **Start here for a presentation.** Seven boxes: upload, prepare, three models, one answer. No code names |
| `market-size-forecasting-flow.html` | The end-to-end process — upload, detect, configure, run, fit, deliver, with the SARIMAX branch |
| `market-size-forecasting-lines.html` | **The technical map.** All 26 steps in five stages, every box carrying its source file and line |
| `market-size-forecasting-tab-1-diagnostics.html` | The Diagnostics tab — what the models are being asked to fit |
| `market-size-forecasting-tab-2-sarimax.html` | The SARIMAX tab — does US macro data explain this series |
| `market-size-forecasting-tab-3-sarima.html` | The SARIMA tab — what the series says on its own |
| `market-size-forecasting-tab-4-prophet.html` | The Prophet tab — trend shifts and seasonality |
| `market-size-forecasting-tab-5-comparison.html` | The Comparison tab — which number to quote |

Each has a `.architecture.json` or `.workflow.json` beside it. That file is the
source; the HTML is generated from it.

## Written documents

| File | For |
|---|---|
| `market-size-forecasting-business-overview.md` | The business read — what it does, why three models, what it cannot do |
| `market-size-forecasting-runbook.md` | Running it yourself, step by step, with the traps |
| `market-size-forecasting-tabs.md` | Each of the five tabs — what it signifies and why it is used |

The full design record, including defects found and the decisions behind them,
is in `docs/superpowers/specs/2026-08-28-market-size-forecasting-design.md`.

## The explainer page

`market-size-forecasting-explorer.html` covers the same 26 steps as
`market-size-forecasting-lines.html`, but as a reading page: clicking a step
shows several paragraphs — what it does, why it is there, what goes in and out,
and what to watch for. Use the diagram to see the shape, the explainer to
understand a step.

---

## What clicking a box gives you

On `business.html` and `lines.html`, a click opens **two** panels:

- **Semantic Passport** (Archify's own) — the box's kind, which stage it sits in,
  its tags, every incoming and outgoing connection with its label, and the
  **verified source**: file and line in this repository, checked against a pinned
  commit rather than someone's working copy.
- **The explanation panel** — a summary, why the step is there, what goes in and
  out, and what to watch for. Press Escape or the × to close it.

The second panel is not an Archify feature. Its schema has no field for a
paragraph at any version, so `scripts/add_diagram_details.py` adds one to the
delivered HTML afterwards, reading a `.details.json` keyed by box id. Approach
borrowed from the US Census ETL pipeline.

The other diagrams show the Passport only.

## Regenerating a diagram

```bash
cd ~/.claude/skills/archify          # Archify 3.0.1
REPO=~/Code-Base/Real/CapIQReplacement
node bin/archify.mjs validate     architecture <spec>.json --quality showcase --repo-root $REPO --json
node bin/archify.mjs deliver      architecture <spec>.json <out>.html --quality showcase --repo-root $REPO --json
node bin/archify.mjs visual-check <out>.html --json
```

Then re-attach the explanation panel, which a fresh `deliver` overwrites:

```bash
.venv/bin/python scripts/add_diagram_details.py <out>.html <out>.details.json
```

It is idempotent and refuses details for a box id that is not in the diagram, so
a renamed box is caught rather than silently dropped.

Use `workflow` in place of `architecture` for the `.workflow.json` specs.
`--repo-root` is what verifies the source references; without it they are not
checked. All three must pass before a diagram is shared.

Two things are load-bearing and easy to break:

- **Node width and grid gaps.** Too narrow and the panel grows taller than a
  1440×900 screen; too wide and the small context text falls below the legibility
  floor. The committed values are the widest that pass.
- **Source line numbers are read from the pinned commit,** not the working tree.
  Move a function and the reference is wrong until the spec and the commit agree.
