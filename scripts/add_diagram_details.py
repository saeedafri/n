"""Add a click-to-read explanation panel to a delivered Archify diagram.

Archify's own click panel (the Semantic Passport) shows a box's kind, sublabel,
tags and connections. Its schema has no field for a paragraph, at any version --
checked across all five upstream schemas. But the deliverable is an HTML file,
so the explanation can be added to the file after `archify deliver` has run.

    .venv/bin/python scripts/add_diagram_details.py \
        docs/diagrams/market-size-forecasting-lines.html \
        docs/diagrams/market-size-forecasting-lines.details.json

The details file is keyed by the box id used in the diagram spec:

    {"granger": {"layer": "FRED macro branch",
                 "title": "granger_scan",
                 "summary": "One sentence.",
                 "sections": [{"heading": "Why it is there", "items": ["..."]}]}}

Re-run after every `archify deliver`; the block replaces its own previous copy,
so running twice is safe. Approach borrowed from the US Census ETL pipeline's
scripts/add_diagram_details.py.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

BEGIN, END = "<!-- msf-details:begin -->", "<!-- msf-details:end -->"

STYLE = """
<style>
  :root { --md-bg:#ffffff; --md-ink:#232020; --md-muted:#6e6866; --md-line:#e3dfde;
          --md-accent:#d62e2f; --md-chip:#fdecec; --md-soft:#f6f5f4;
          --md-shadow:0 18px 48px -18px rgba(15,23,42,.42); }
  :root[data-theme="dark"] { --md-bg:#201d1c; --md-ink:#efebea; --md-muted:#a49d9a;
          --md-line:#322d2c; --md-accent:#ff6b5e; --md-chip:#3a211f; --md-soft:#282423;
          --md-shadow:0 18px 48px -18px rgba(0,0,0,.9); }
  #md-panel { position:fixed; top:0; right:0; height:100%; width:min(440px,100vw);
              background:var(--md-bg); color:var(--md-ink); border-left:1px solid var(--md-line);
              box-shadow:var(--md-shadow); z-index:2147483000; transform:translateX(100%);
              visibility:hidden; transition:transform .22s ease, visibility 0s linear .22s;
              display:flex; flex-direction:column;
              font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; }
  #md-panel.open { transform:none; visibility:visible; transition:transform .22s ease; }
  #md-head { padding:20px 22px 14px; border-bottom:1px solid var(--md-line);
             display:flex; gap:12px; align-items:flex-start; }
  #md-head div { flex:1; min-width:0; }
  #md-layer { display:inline-block; font-size:11px; font-weight:600; letter-spacing:.12em;
              text-transform:uppercase; color:var(--md-accent); background:var(--md-chip);
              padding:3px 8px; border-radius:3px; margin-bottom:8px; }
  #md-title { font-size:19px; font-weight:650; margin:0; line-height:1.25; overflow-wrap:anywhere; }
  #md-close { border:1px solid var(--md-line); background:transparent; color:var(--md-ink);
              border-radius:4px; width:32px; height:32px; font-size:18px; line-height:1;
              cursor:pointer; flex:none; }
  #md-close:focus-visible, #md-hint:focus-visible { outline:2px solid var(--md-accent); outline-offset:2px; }
  #md-body { padding:16px 22px 28px; overflow-y:auto; }
  #md-summary { margin:0 0 16px; font-size:15px; }
  #md-body h3 { font-size:11.5px; letter-spacing:.1em; text-transform:uppercase;
                color:var(--md-muted); margin:18px 0 6px; }
  #md-body ul { margin:0; padding-left:18px; }
  #md-body li { margin:5px 0; }
  #md-src { margin-top:18px; padding:11px 13px; background:var(--md-soft);
            border-left:3px solid var(--md-accent); border-radius:3px;
            font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-wrap:anywhere; }
  #md-hint { position:fixed; left:16px; bottom:16px; z-index:2147482999; background:var(--md-ink);
             color:var(--md-bg); border:0; border-radius:999px; padding:8px 14px;
             font:600 12.5px ui-sans-serif,-apple-system,Arial,sans-serif; cursor:pointer;
             box-shadow:var(--md-shadow); }
  @media (prefers-reduced-motion: reduce) { #md-panel, #md-panel.open { transition:none; } }
</style>
"""

MARKUP = """
<button id="md-hint" type="button">Click any box for its full explanation</button>
<aside id="md-panel" role="dialog" aria-modal="false" aria-labelledby="md-title" aria-hidden="true">
  <div id="md-head"><div><span id="md-layer"></span><h2 id="md-title"></h2></div>
    <button id="md-close" type="button" aria-label="Close explanation">&times;</button></div>
  <div id="md-body"></div>
</aside>
"""

SCRIPT = """
<script>
(function () {
  var DETAILS = __DETAILS__;
  var panel = document.getElementById('md-panel'), body = document.getElementById('md-body');
  var hint = document.getElementById('md-hint');
  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
  function open(id) {
    var d = DETAILS[id]; if (!d) return;
    document.getElementById('md-layer').textContent = d.layer || '';
    document.getElementById('md-title').textContent = d.title || id;
    var out = d.summary ? '<p id="md-summary">' + esc(d.summary) + '</p>' : '';
    (d.sections || []).forEach(function (s) {
      out += '<h3>' + esc(s.heading) + '</h3><ul>'
           + s.items.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('')
           + '</ul>';
    });
    if (d.source) out += '<div id="md-src">' + esc(d.source) + '</div>';
    body.innerHTML = out; body.scrollTop = 0;
    panel.classList.add('open'); panel.setAttribute('aria-hidden', 'false'); hint.hidden = true;
  }
  function close() { panel.classList.remove('open'); panel.setAttribute('aria-hidden', 'true'); }
  document.addEventListener('click', function (e) {
    var node = e.target.closest && e.target.closest('[data-node-id]');
    if (node && DETAILS[node.getAttribute('data-node-id')]) open(node.getAttribute('data-node-id'));
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { close(); return; }
    var node = e.target.closest && e.target.closest('[data-node-id]');
    if (node && (e.key === 'Enter' || e.key === ' ')) open(node.getAttribute('data-node-id'));
  });
  document.getElementById('md-close').addEventListener('click', close);
  hint.addEventListener('click', function () { open(Object.keys(DETAILS)[0]); });
})();
</script>
"""


def add_details(html_path: str, details_path: str) -> tuple[int, list[str]]:
    page = Path(html_path).read_text()
    details = json.loads(Path(details_path).read_text())
    # Anchor to the rendered SVG group: a bare data-node-id match also picks up
    # Archify's own JS template strings and reports phantom missing boxes.
    ids = set(re.findall(r'<g id="node-[^"]+" data-node-id="([^"]+)"', page))
    unknown = sorted(set(details) - ids)
    if unknown:
        raise SystemExit(f"details given for boxes that are not in {html_path}: {unknown}")
    missing = sorted(ids - set(details))
    page = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", page, flags=re.S)
    payload = json.dumps(details, ensure_ascii=False).replace("</", "<\\/")
    block = f"{BEGIN}{STYLE}{MARKUP}{SCRIPT.replace('__DETAILS__', payload)}{END}\n"
    page = page.replace("</body>", block + "</body>", 1)
    Path(html_path).write_text(page)
    return len(details), missing


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("html")
    parser.add_argument("details")
    args = parser.parse_args()
    count, missing = add_details(args.html, args.details)
    note = f"; no details yet for {', '.join(missing)}" if missing else ""
    print(f"{args.html}: {count} boxes explained{note}")


if __name__ == "__main__":
    main()
