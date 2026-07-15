"""Suppress Streamlit's grey boot skeleton; show a branded Coresight loader.

WHY THIS EXISTS
---------------
When a page changes Streamlit does a full reload. Before the Python script
connects, Streamlit's JS bundle paints grey "skeleton" placeholders
(``[data-testid="stSkeleton"]`` — the grey bars/blocks). That happens *before*
any app code runs, so NO CSS injected from Python (st.markdown, components.html,
navigation.py) can ever reach it — by the time our CSS exists, the skeleton has
already flashed. On slow networks (e.g. STG) it lingers for a second or more.

The only layer early enough is the static ``index.html`` the server hands the
browser. We patch it once, at process start, to:
  1. hide the skeleton placeholders outright, and
  2. paint a branded Coresight overlay (logo + spinner + shimmer) from the very
     first frame, auto-removed by a tiny inline script the moment real Streamlit
     content appears.

The patch is idempotent and re-applied on every cold start, so it survives a
``pip install`` / Streamlit upgrade that would otherwise restore the stock file.
Streamlit serves index.html fresh from disk per request, so the patch takes
effect for every page load after the first one in a freshly started process.
"""

import pathlib

# Bump this token if the injected markup below changes, so an already-patched
# (stale) index.html is re-patched instead of skipped.
_MARKER = "cs-boot-overlay-v5"

# ─────────────────────────────────────────────────────────────────────────────
# Stale-chunk auto-recovery. After a redeploy, Streamlit's hashed JS chunk names
# change. A browser tab still holding the OLD index.html (or its module-preload
# hints) then does an import() for a chunk the server no longer has → the browser
# throws "Failed to fetch dynamically imported module: …/static/js/*.js" and the
# widget renders as a red TypeError box (seen on STG /newsroom after a deploy).
# This is NOT an app-code bug — it is a client holding stale asset URLs. The
# standard fix is to catch that specific error and hard-reload ONCE so the browser
# fetches the fresh index + chunks. Guarded by a timestamp in sessionStorage so it
# can never loop (a reload <12s ago is ignored) yet still recovers from a LATER
# deploy in the same tab. Injected into <head> so it registers before any module
# script runs. Runs on EVERY page (it is in index.html), so it also covers the
# nav/full-reload path.
# ─────────────────────────────────────────────────────────────────────────────
_HEAD_JS = """
<script id="cs-chunk-recover" data-cs="cs-boot-overlay-v5">
(function(){
  var K='__cs_chunk_reload_ts';
  function isChunkErr(m){
    m=String(m||'');
    return m.indexOf('Failed to fetch dynamically imported module')!==-1
        || m.indexOf('error loading dynamically imported module')!==-1
        || m.indexOf('Importing a module script failed')!==-1
        || (m.indexOf('dynamically imported module')!==-1 && m.indexOf('fetch')!==-1);
  }
  function recover(){
    try{
      var now=Date.now(), last=parseInt(sessionStorage.getItem(K)||'0',10);
      if(now-last<12000) return;         // reloaded very recently → don't loop
      sessionStorage.setItem(K,String(now));
      location.reload();
    }catch(e){ try{ location.reload(); }catch(_){} }
  }
  window.addEventListener('error',function(ev){
    if(ev && isChunkErr(ev.message)) recover();
  },true);
  window.addEventListener('unhandledrejection',function(ev){
    var r=ev&&ev.reason; if(r && isChunkErr(r.message||r)) recover();
  });
})();
</script>
"""

_HEAD_CSS = """
<style id="cs-boot-style" data-cs="cs-boot-overlay-v5">
/* Streamlit's grey skeleton placeholders — never show them, on any page. */
[data-testid="stSkeleton"],[data-testid="stAppSkeleton"]{display:none!important;}
/* Branded boot overlay: visible from first paint until real content renders.
   v3: translucent backdrop (the page stays visible while it loads behind) and
   CLICK-THROUGH (pointer-events:none) so the nav is never blocked. The card is
   pixel-identical to the in-app loader card (.cs-al-card in components/loading.py)
   so a boot→in-app handoff reads as ONE spinner whose label changes. */
#cs-boot-overlay{position:fixed;inset:0;z-index:2147483600;
  background:rgba(244,244,244,.62);
  -webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px);
  display:flex;align-items:center;justify-content:center;pointer-events:none;
  transition:opacity .25s ease;font-family:'Source Sans Pro',system-ui,sans-serif;}
#cs-boot-overlay.cs-hide{opacity:0;pointer-events:none;}
/* While the boot overlay is up, hide any other spinner — ONE spinner only. */
body:has(#cs-boot-overlay:not(.cs-hide)) [data-testid="stSpinner"],
body:has(#cs-boot-overlay:not(.cs-hide)) .cs-inline-loading,
body:has(#cs-boot-overlay:not(.cs-hide)) .cs-page-subspinner{display:none!important;}
#cs-boot-overlay .cs-card{display:flex;flex-direction:column;align-items:center;
  gap:16px;padding:30px 44px;background:#fff;border-radius:14px;
  box-shadow:0 6px 40px rgba(0,0,0,.12);pointer-events:none;}
#cs-boot-overlay .cs-card img{width:132px;height:auto;display:block;}
#cs-boot-overlay .cs-ring{width:32px;height:32px;border-radius:50%;
  border:3px solid rgba(214,46,47,.14);border-top-color:#d62e2f;
  animation:cs-boot-spin .7s linear infinite;}
@keyframes cs-boot-spin{to{transform:rotate(360deg)}}
#cs-boot-overlay .cs-txt{font-family:Montserrat,'Source Sans Pro',system-ui,sans-serif;
  font-size:14px;font-weight:600;color:#555;letter-spacing:.01em;text-align:center;}
#cs-boot-overlay .cs-sh{width:108px;height:2px;border-radius:1px;
  background:linear-gradient(90deg,#ebebeb 25%,#d62e2f 50%,#ebebeb 75%);
  background-size:200% 100%;animation:cs-boot-sh 1.6s ease infinite;}
@keyframes cs-boot-sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
</style>
"""

_BODY_HTML = """
<div id="cs-boot-overlay" data-cs="cs-boot-overlay-v5">
  <div class="cs-card">
    <img src="https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net/wp-content/uploads/2023/12/coresight-logo-1.png" alt="Coresight" referrerpolicy="no-referrer">
    <div class="cs-ring"></div>
    <div class="cs-txt" id="cs-boot-txt">Loading</div>
    <div class="cs-sh"></div>
  </div>
</div>
<script data-cs="cs-boot-overlay-v4">
(function(){
  var ov=document.getElementById('cs-boot-overlay');
  if(!ov)return;
  // Dynamic per-page label from the URL so a page switch reads e.g. "Loading
  // Earnings Calls" — never a bare "Loading". Special-cases the root/login/OIDC
  // callback so those aren't generic either:
  //   /?code=... or ?login_handoff  -> OIDC login success, heading to home -> "Loading Home"
  //   /login                        -> "Loading Login"
  //   bare /                        -> "Loading Home" if an auth_session cookie exists
  //                                    (already logged in), else "Loading Login"
  try{
    var P=(window.location.pathname||''), Q=(window.location.search||'');
    var M={'/market_data':'Loading Market Data','/earnings_calls':'Loading Earnings Calls',
      '/earnings_calendar':'Loading Calendar','/screening':'Loading Screening',
      '/newsroom':'Loading News','/home':'Loading Home','/company_filings':'Loading Filings',
      '/company_filings_add_files':'Loading Filings','/forecasting':'Loading Forecasting',
      '/live_earnings_transcript':'Loading Transcript','/logout_bridge':'Signing out',
      '/access_management':'Loading Access','/logs':'Loading Logs','/login':'Loading Login'};
    var lbl=null;
    for(var k in M){ if(P.indexOf(k)===0){ lbl=M[k]; break; } }
    if(lbl===null){
      var isRoot=(P===''||P==='/');
      var hasCode=/[?&](code|login_handoff|auth_flow_id)=/.test(Q);
      var hasCookie=/(^|;\\s*)auth_session=/.test(document.cookie||'');
      if(isRoot && hasCode) lbl='Loading Home';            // OIDC success -> home
      else if(isRoot && hasCookie) lbl='Loading Home';     // already authed -> home
      else if(isRoot) lbl='Loading Login';                 // fresh visit -> login
      else lbl='Loading';
    }
    var tx=document.getElementById('cs-boot-txt'); if(tx) tx.textContent=lbl;
  }catch(e){}
  // "Real content" = a concrete Streamlit widget inside the main view. Note we
  // deliberately do NOT match element-container/stSkeleton wrappers — those mount
  // first and would hide the overlay while the page is still blank/grey.
  var SEL='[data-testid="stMarkdownContainer"],[data-testid="stDataFrame"],'+
          '[data-testid="stSelectbox"],[data-testid="stMetric"],[data-testid="stTabs"],'+
          '[data-testid="stForm"],[data-testid="stImage"],[data-testid="stTextInput"],'+
          '[data-testid="stMultiSelect"],[data-testid="stDataEditor"],[data-testid="stTable"]';
  function ready(){
    var main=document.querySelector('[data-testid="stMain"]');
    return main && main.querySelector(SEL);
  }
  var done=false;
  function hide(){
    if(done)return; done=true;
    ov.classList.add('cs-hide');
    setTimeout(function(){ if(ov&&ov.parentNode) ov.parentNode.removeChild(ov); },450);
  }
  if(ready()){ hide(); return; }
  var mo=new MutationObserver(function(){ if(ready()) hide(); });
  mo.observe(document.documentElement,{childList:true,subtree:true});
  setTimeout(hide,15000); // safety: never trap the user behind the overlay
})();
</script>
"""


def patch_streamlit_index_html() -> bool:
    """Inject the skeleton-hide CSS + branded boot overlay into Streamlit's
    static index.html. Idempotent and best-effort (never raises). Returns True
    if a write happened (or the patch was already present), False on failure."""
    try:
        import streamlit
        index_path = pathlib.Path(streamlit.__file__).parent / "static" / "index.html"
        html = index_path.read_text(encoding="utf-8")

        if _MARKER in html:
            return True  # already patched with this version

        # Strip ANY older version of our injection, then re-inject the current one.
        # (A token bump must upgrade the markup, not stack a second overlay.)
        if "cs-boot-overlay" in html or "cs-chunk-recover" in html:
            import re
            html = re.sub(r'<script id="cs-chunk-recover".*?</script>\s*', '', html, flags=re.DOTALL)
            html = re.sub(r'<style id="cs-boot-style".*?</style>\s*', '', html, flags=re.DOTALL)
            html = re.sub(r'<div id="cs-boot-overlay".*?</script>\s*', '', html, flags=re.DOTALL)

        if "</head>" in html:
            # Chunk-recovery script FIRST (registers error handlers before module
            # scripts run), then the skeleton-hide + boot-overlay CSS.
            html = html.replace("</head>", _HEAD_JS + _HEAD_CSS + "</head>", 1)
        if "<body>" in html:
            html = html.replace("<body>", "<body>" + _BODY_HTML, 1)

        index_path.write_text(html, encoding="utf-8")
        return True
    except Exception:
        return False
