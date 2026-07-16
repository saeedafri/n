"""Browser-side performance panel — the timings the server CANNOT see.

Every server-side marker we log (MAIN_AUTH_BOOTSTRAP, APP_RUN_START, PAGE_*,
TAB_*) measures Python work only, and on STG that work is sub-millisecond. The
wait users feel when they open the site is spent BEFORE Python runs: DNS, TCP,
TLS, the Streamlit JS/font download, and the websocket handshake — all paid in
network round trips to the App Service region.

Add `?perf=1` to any URL to render this panel. It reads the browser's own
Navigation Timing / Resource Timing APIs and prints the real breakdown, so a
slow landing is attributed to a phase instead of guessed at.

Measured 16-Jul-2026 (Gurugram → STG in Azure centralus / Des Moines, Iowa):
one TCP round trip ~233-338 ms, TLS handshake ~+490 ms, TTFB ~1.0-1.7 s for an
8 KB page whose server-side work was 0.03 ms. Protocol negotiated: HTTP/1.1.

Implementation note: this MUST use components.v1.html, not st.html — Streamlit
inserts st.html markup via innerHTML, and per the HTML spec scripts injected
that way never execute. The component runs in a same-origin iframe (height 0)
and injects the panel into the parent document.
"""

from __future__ import annotations

import streamlit as st

_PANEL_JS = """
<script>
(function(){
  var W = window.parent || window;
  var D = W.document;
  function ms(v){ return (v===null||v===undefined||isNaN(v)) ? "?" : Math.round(v)+" ms"; }
  function row(label, val, color){
    return "<div style='display:flex;justify-content:space-between;gap:16px'>"
         + "<span>" + label + "</span>"
         + "<span style='color:" + (color||"#7fd1ff") + "'>" + ms(val) + "</span></div>";
  }
  function paint(){
    try{
      var n = W.performance.getEntriesByType("navigation")[0];
      if(!n){ return; }
      var host = D.getElementById("cs-perf");
      if(!host){
        host = D.createElement("div");
        host.id = "cs-perf";
        host.style.cssText = "position:fixed;bottom:12px;right:12px;z-index:2147483600;"
          + "background:#111;color:#eee;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;"
          + "padding:12px 14px;border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.35);max-width:430px;opacity:.96;";
        D.body.appendChild(host);
      }
      var res = W.performance.getEntriesByType("resource") || [];
      var bytes = 0, slowest = null;
      res.forEach(function(r){
        bytes += (r.transferSize || 0);
        if(!slowest || r.duration > slowest.duration) slowest = r;
      });
      var fcpEntry = W.performance.getEntriesByName("first-contentful-paint")[0];
      var html = "<div style='font-weight:700;color:#fff;margin-bottom:6px'>"
               + "Browser timing — what the user waits for</div>"
        + row("DNS lookup",           n.domainLookupEnd - n.domainLookupStart)
        + row("TCP connect (1 RTT)",  n.connectEnd - n.connectStart)
        + row("TLS handshake",        n.secureConnectionStart ? (n.connectEnd - n.secureConnectionStart) : 0)
        + row("Request &rarr; 1st byte", n.responseStart - n.requestStart)
        + row("HTML download",        n.responseEnd - n.responseStart)
        + row("DOM interactive",      n.domInteractive - n.responseEnd)
        + row("Load event (total)",   n.loadEventEnd - n.startTime, "#ffd479")
        + "<hr style='border:0;border-top:1px solid #333;margin:6px 0'>"
        + row("First contentful paint", fcpEntry ? fcpEntry.startTime : null, "#8fe388")
        + "<div style='display:flex;justify-content:space-between;gap:16px'><span>Assets</span><span>"
        + res.length + " files / " + (bytes/1024).toFixed(0) + " KB</span></div>";
      if(slowest){
        html += row("Slowest asset", slowest.duration, "#ff9f9f")
             +  "<div style='color:#888;font-size:11px;word-break:break-all'>"
             +  String(slowest.name).split("/").pop().slice(0,54) + "</div>";
      }
      html += "<div style='color:#888;margin-top:6px;font-size:11px'>protocol: "
           + (n.nextHopProtocol || "?")
           + " &middot; server-side work is logged separately (APP_RUN_START / PAGE_* / TAB_*)</div>";
      host.innerHTML = html;
      try{
        console.table([
          {phase:"TCP connect (1 RTT)", ms: Math.round(n.connectEnd-n.connectStart)},
          {phase:"TLS handshake", ms: Math.round(n.secureConnectionStart ? n.connectEnd-n.secureConnectionStart : 0)},
          {phase:"Request->1st byte", ms: Math.round(n.responseStart-n.requestStart)},
          {phase:"Load total", ms: Math.round(n.loadEventEnd-n.startTime)}
        ]);
      }catch(e){}
    }catch(e){ /* diagnostics must never break the page */ }
  }
  if(D.readyState === "complete"){ setTimeout(paint, 120); }
  else { W.addEventListener("load", function(){ setTimeout(paint, 120); }); }
})();
</script>
"""


def render_perf_panel_if_requested() -> None:
    """Render the browser timing panel when ?perf=1 is in the URL. No-op otherwise."""
    try:
        if str(st.query_params.get("perf", "")).strip() not in ("1", "true", "yes"):
            return
        import streamlit.components.v1 as components
        components.html(_PANEL_JS, height=0, width=0)
    except Exception:
        pass  # diagnostics must never break a page
