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
_MARKER = "cs-boot-overlay-v1"

_HEAD_CSS = """
<style id="cs-boot-style" data-cs="cs-boot-overlay-v1">
/* Streamlit's grey skeleton placeholders — never show them, on any page. */
[data-testid="stSkeleton"],[data-testid="stAppSkeleton"]{display:none!important;}
/* Branded boot overlay: visible from first paint until real content renders. */
#cs-boot-overlay{position:fixed;inset:0;z-index:2147483600;background:#f2f2f2;
  display:flex;align-items:center;justify-content:center;
  transition:opacity .25s ease;font-family:'Source Sans Pro',system-ui,sans-serif;}
#cs-boot-overlay.cs-hide{opacity:0;pointer-events:none;}
#cs-boot-overlay .cs-card{display:flex;flex-direction:column;align-items:center;
  gap:18px;padding:32px 44px;background:#fff;border-radius:14px;
  box-shadow:0 6px 40px rgba(0,0,0,.10);}
#cs-boot-overlay .cs-card img{width:144px;height:auto;display:block;}
#cs-boot-overlay .cs-ring{width:32px;height:32px;border-radius:50%;
  border:3px solid rgba(214,46,47,.12);border-top-color:#d62e2f;
  animation:cs-boot-spin .7s linear infinite;}
@keyframes cs-boot-spin{to{transform:rotate(360deg)}}
#cs-boot-overlay .cs-sh{width:108px;height:2px;border-radius:1px;
  background:linear-gradient(90deg,#ebebeb 25%,#d62e2f 50%,#ebebeb 75%);
  background-size:200% 100%;animation:cs-boot-sh 1.6s ease infinite;}
@keyframes cs-boot-sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
</style>
"""

_BODY_HTML = """
<div id="cs-boot-overlay" data-cs="cs-boot-overlay-v1">
  <div class="cs-card">
    <img src="https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net/wp-content/uploads/2023/12/coresight-logo-1.png" alt="Coresight" referrerpolicy="no-referrer">
    <div class="cs-ring"></div>
    <div class="cs-sh"></div>
  </div>
</div>
<script data-cs="cs-boot-overlay-v1">
(function(){
  var ov=document.getElementById('cs-boot-overlay');
  if(!ov)return;
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

        # Drop any older version of our patch before re-injecting.
        # (Cheap guard so a token bump doesn't stack duplicates.)
        if "cs-boot-overlay" in html:
            return True  # an older marker exists; leave it — avoids double overlay

        if "</head>" in html:
            html = html.replace("</head>", _HEAD_CSS + "</head>", 1)
        if "<body>" in html:
            html = html.replace("<body>", "<body>" + _BODY_HTML, 1)

        index_path.write_text(html, encoding="utf-8")
        return True
    except Exception:
        return False
