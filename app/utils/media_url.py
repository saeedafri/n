"""Serve file bytes to the browser over HTTP instead of the websocket.

Why this module exists
---------------------
Several pages used to base64-encode a whole file (Excel workbook, filing PDF,
transcript PDF) and inline it into a ``components.v1.html`` iframe. That HTML is a
protobuf string field, so the entire file travelled inside ONE Streamlit
``ForwardMsg``. Once the payload outgrows what the proxy in front of the app will
carry, the frame is truncated in transit and the browser's protobuf decoder dies
with e.g. ``RangeError: index out of range: 99 + 7909686 > 348066``, which Streamlit
surfaces as a bare "Connection error" modal. Measured cases (2026-07-30, STG):

* screening key-devs Excel — 102 MB workbook for the 146k-event all-history screen;
  the M&A / All-History screen a user reported produced exactly 7,909,686 bytes.
* company_filings PDFs — 885 of 3,162 blobs exceed 1 MB; p90 5.2 MB, max 44 MB.

``serve_bytes`` hands the bytes to Streamlit's media file manager and returns a
short ``/media/<hash>`` URL. The ForwardMsg then carries a few dozen bytes at any
file size, and the browser fetches the file over plain HTTP — the same path
``st.download_button`` / ``st.image`` / ``st.pdf`` already use.

Use ``embed_is_safe`` before falling back to inlining anything.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from html import escape
from typing import Optional, Sequence, Tuple

from utils.server_logger import log_structured_error, log_warning

# Largest payload we are willing to put inside a single websocket message.
# Frames of this order already ship successfully every day (an AG Grid page is
# ~870 KB); the failures start in the low megabytes.
WEBSOCKET_SAFE_BYTES = 1_000_000


def embed_is_safe(byte_count: int) -> bool:
    """True when ``byte_count`` may be inlined into a ForwardMsg (iframe HTML, etc.).

    Base64 inflates by 4/3, so the check is applied to the encoded size.
    """
    return byte_count * 4 / 3 <= WEBSOCKET_SAFE_BYTES


def serve_bytes(
    data: bytes,
    mimetype: str,
    filename: str,
    page: str = "",
    for_download: bool = True,
) -> Optional[str]:
    """Register ``data`` with Streamlit's media file manager and return its URL.

    Returns a ``/media/<hash>.<ext>`` path the browser can fetch over HTTP, or
    ``None`` when no Streamlit runtime is available (bare script / unit test), in
    which case the caller must decide what to do — see ``embed_is_safe``.

    Identical bytes de-duplicate to the same URL, so calling this on every rerun
    is cheap and the URL stays stable across reruns.

    IMPORTANT — call this on EVERY script run, not once behind a session_state
    cache. Streamlit drops all of a session's media references at the start of each
    run and deletes orphans at the end (``script_runner``
    ``clear_session_refs`` / ``remove_orphaned_files``), so a URL cached across
    reruns starts returning 404. Cache the bytes instead and re-register them.

    ``for_download`` marks the file ``DOWNLOADABLE`` rather than ``MEDIA``, which
    buys it a one-pass grace period in ``remove_orphaned_files`` (marked first,
    deleted only on the next sweep). Streamlit relies on that itself so an
    in-flight download can finish; without it a button whose element stops being
    re-rendered — e.g. an auto-download that clears its own trigger state — can
    have the file deleted out from under the browser's async fetch. Pass
    ``for_download=False`` only for streamed viewers that re-register every run.
    """
    if not data:
        return None
    try:
        from streamlit.runtime import get_instance

        # `coordinates` is the media manager's dedupe/ownership key. Deriving it
        # from the content keeps the URL stable across reruns of the same file.
        coordinates = "media_url-" + hashlib.sha256(data).hexdigest()[:32]
        return get_instance().media_file_mgr.add(
            data, mimetype, coordinates, file_name=filename,
            is_for_static_download=for_download,
        )
    except Exception as exc:
        log_structured_error(
            exc, page=page or "media_url", component="serve_bytes",
            operation="register_media_file", context=f"{filename} ({len(data)} bytes)",
        )
        return None


def absolute_app_url(path: str) -> str:
    """Turn a root-relative app path into an absolute URL for use inside an iframe.

    ``components.v1.html`` renders into an iframe with an opaque origin, so a
    ``/media/...`` path written into that HTML resolves against the wrong base.
    Same trick the screening page already uses for its Source Reference links.
    """
    if not path or not path.startswith("/"):
        return path
    try:
        import streamlit as st
        from urllib.parse import urlparse

        parsed = urlparse(getattr(st.context, "url", None) or "")
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{path}"
    except Exception:
        pass
    return path


def report_oversized_embed(byte_count: int, what: str, page: str = "") -> None:
    """Log that a payload too large for the websocket was about to be inlined.

    Called on the fallback path so an oversized embed is never silent — a
    truncated ForwardMsg only shows up as an unexplained "Connection error".
    """
    log_warning(
        f"[MEDIA_URL] refusing to inline {what}: {byte_count/1e6:.2f} MB "
        f"({byte_count*4/3/1e6:.2f} MB base64) exceeds the "
        f"{WEBSOCKET_SAFE_BYTES/1e6:.2f} MB websocket-safe limit; "
        f"page={page or 'unknown'}"
    )


# The one way this app saves a file: fetch the /media URL into a Blob, then click
# an in-memory `blob:` anchor. Never a plain `<a href="/media/...">`. See
# render_download_buttons for why the anchor loses the file.
_SAVE_BLOB_JS = """
function saveBlob(url,name){
  fetch(url).then(function(r){
    if(!r.ok) throw new Error("HTTP "+r.status);
    return r.blob();
  }).then(function(blob){
    var u=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=u; a.download=name;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(function(){URL.revokeObjectURL(u);},1000);
  }).catch(function(e){console.error("Download failed:",name,e);});
}
"""


def render_build_status(message: str, page: str = "") -> bool:
    """Draw a "working on it" line that the app's own CSS cannot hide.

    NOT ``st.spinner``. Both ``core/boot_overlay`` and ``components/loading``
    carry "ONE SPINNER ONLY" rules — ``[data-testid="stSpinner"]{display:none}``
    while a branded overlay is in the DOM — and a rerun triggered by a button
    click puts that overlay up. A spinner here is therefore rendered and then
    hidden, which is exactly how a multi-minute export came to look like a dead
    button. Inside a component iframe nothing on the page can touch it, and the
    elapsed clock keeps ticking client-side while the server thread is busy.
    """
    try:
        from streamlit.components.v1 import html as component_html

        # 44px / two lines: these buttons sit in a ~210px column (screening's
        # [9, 1.5] results header), where anything longer is clipped.
        markup = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{display:flex;align-items:center;justify-content:flex-end;gap:7px;height:44px;
  background:transparent;font-family:'Inter','Source Sans Pro',system-ui,sans-serif;
  font-size:12px;color:#D62E2F;font-weight:500;overflow:hidden;}}
.sp{{width:12px;height:12px;border:2px solid #f0d2d2;border-top-color:#D62E2F;
  border-radius:50%;animation:spin .8s linear infinite;flex:none;}}
@keyframes spin{{to{{transform:rotate(360deg);}}}}
.msg{{text-align:right;line-height:1.3;}}
#t{{opacity:.75;}}
</style></head><body>
<div class="sp"></div><div class="msg">{escape(message)}<br><span id="t"></span></div>
<script>
var t0=Date.now();
setInterval(function(){{
  var s=Math.round((Date.now()-t0)/1000);
  document.getElementById("t").textContent =
    (s<60 ? s+"s elapsed" : Math.floor(s/60)+"m "+(s%60)+"s elapsed");
}},1000);
</script></body></html>"""
        component_html(markup, height=44, scrolling=False)
        return True
    except Exception as exc:
        log_structured_error(
            exc, page=page or "media_url", component="render_build_status",
            operation="render_status", context=message[:80],
        )
        return False


def trigger_download(data: bytes, mimetype: str, filename: str, page: str = "") -> bool:
    """Save ``data`` in the browser immediately, rendering no visible control.

    Draws a zero-height component that fetches the file into a Blob as soon as it
    mounts. Use it right after a plain ``st.button`` click so the user sees one
    button and gets one file — see ``lazy_download_button``.
    """
    if not data:
        return False
    url = serve_bytes(data, mimetype, filename, page=page)
    if not url:
        report_oversized_embed(len(data), filename, page=page)
        return False
    try:
        import time as _time
        from streamlit.components.v1 import html as component_html

        # Unique nonce per invocation: openpyxl stamps its zip entries at 1-second
        # resolution, so two builds in the same second produce byte-identical HTML →
        # Streamlit keeps the existing DOM node and the script never re-runs, so a
        # deliberate re-download would silently do nothing.
        markup = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;height:0;overflow:hidden;">
<!-- dl-nonce {_time.time_ns()} -->
<script>{_SAVE_BLOB_JS}
saveBlob({json.dumps(absolute_app_url(url))},{json.dumps(filename)});
</script></body></html>"""
        component_html(markup, height=0, width=0, scrolling=False)
        return True
    except Exception as exc:
        log_structured_error(
            exc, page=page or "media_url", component="trigger_download",
            operation="render_trigger", context=filename,
        )
        return False


def lazy_download_button(
    label: str,
    filename: str,
    build_fn,
    mimetype: str,
    key: str,                      # the st.button widget key, verbatim
    page: str = "",
    help: Optional[str] = None,
    icon: Optional[str] = None,
    container_key: Optional[str] = None,
    width: str = "content",
    button_type: str = "secondary",
    empty_message: str = "No data available to export.",
    spinner_message: str = "Preparing your file…",
) -> None:
    """A normal ``st.button`` that builds its file on click and saves it as a Blob.

    Use this instead of ``st.download_button`` everywhere. ``st.download_button``
    renders a real ``<a href="/media/..." download>`` anchor, and a desktop download
    manager that hooks anchor clicks refetches that URL as a fresh request —
    typically after the user has picked a save location, by which point Streamlit's
    end-of-run orphan sweep has deleted the file. The manager reports it as
    "No Internet Connection or DNS Failed". Deferred (callable) data is worse:
    ``MediaFileManager.execute_deferred`` deliberately leaves the generated file
    unmapped from the session, so it is swept after two runs no matter what.

    Fetching into a Blob starts the transfer the instant the component mounts,
    while the file is certainly still there, and the anchor the browser finally
    clicks is an in-memory ``blob:`` URL that needs no network at all.

    ``build_fn`` is a zero-arg callable returning bytes; it runs inside an
    ``st.fragment``, so a click reruns this button alone and never the page.
    Everything is drawn inside ONE container, so the pair still fits in an
    ``st.empty()`` slot.

    The build ALWAYS shows ``render_build_status`` while it runs. That is not
    decoration. STG serves the app with ``--ui.hideTopBar=True``, so Streamlit's
    own "Running" indicator is invisible there, and the app's "ONE SPINNER ONLY"
    rules hide ``st.spinner`` outright while a branded overlay is up — which a
    button click puts up. Without this, the key-devs all-history export (75k+
    events, ~19s per 10k-row page against the staging DB, i.e. minutes) looks
    like a dead button and the user clicks again, queueing another build. The
    deferred ``st.download_button`` this replaced got that feedback for free:
    its frontend component spun while it waited for the file.
    """
    import streamlit as st

    @st.fragment
    def _download_button():
        with st.container(key=container_key) if container_key else nullcontext():
            if st.button(label, key=key, help=help, icon=icon, width=width,
                         type=button_type):
                status = st.empty()
                try:
                    with status:
                        render_build_status(spinner_message, page=page)
                    data = build_fn()
                except Exception as exc:
                    log_structured_error(
                        exc, page=page or "media_url", component="lazy_download_button",
                        operation="build_payload", context=filename,
                    )
                    st.error("Could not build this file. Please try again.")
                    return
                finally:
                    status.empty()
                if data:
                    trigger_download(data, mimetype, filename, page=page)
                else:
                    st.toast(empty_message, icon="⚠️")

    _download_button()


def render_download_buttons(
    files: Sequence[Tuple[str, bytes, str, str]],
    page: str = "",
    height: int = 52,
    align: str = "flex-start",
) -> bool:
    """Render one row of download buttons that fetch into a Blob.

    ``files`` is a sequence of ``(label, data, mimetype, filename)``. Returns
    False when nothing could be served.

    Why not ``st.download_button``. That widget renders a real
    ``<a href="/media/..." download>`` anchor and reruns the script on click.
    Two things go wrong with it. A desktop download manager that hooks anchor
    clicks refetches the URL as a fresh request with no session cookie and
    reports a connection failure instead of saving the file. And the rerun
    re-registers the payload, so bytes that are not identical run to run --
    an openpyxl workbook stamps the current time into its zip entries -- land
    on a new ``/media`` hash while the browser is still fetching the old one,
    which the end-of-run orphan sweep has just deleted.

    A button inside a component iframe reruns nothing, and the bytes are
    already in the page by the time the anchor is clicked, so neither applies.
    This is the pattern the filings, transcript, market-data and forecasting
    exports already use.
    """
    entries = []
    for label, data, mimetype, filename in files:
        url = serve_bytes(data, mimetype, filename, page=page)
        if not url:
            report_oversized_embed(len(data or b""), f"{label} {filename}", page=page)
            continue
        entries.append({"url": absolute_app_url(url), "name": filename, "label": label})
    if not entries:
        return False

    buttons = "".join(
        f'<button onclick="dl({i})">'
        f'<span class="material-symbols-outlined">download</span>'
        f'&nbsp;&nbsp;{escape(e["label"])}</button>'
        for i, e in enumerate(entries)
    )
    payload = json.dumps([{"url": e["url"], "name": e["name"]} for e in entries])
    markup = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&icon_names=download" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{display:flex;justify-content:{align};align-items:center;gap:10px;height:{height}px;
  background:transparent;font-family:'Inter','Roboto',Helvetica,Arial,sans-serif;}}
button{{background:transparent;border:1px solid #D62E2F;color:#D62E2F;border-radius:4px;
  padding:7px 12px;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap;
  transition:background 0.15s,color 0.15s;letter-spacing:0.01em;
  display:flex;align-items:center;gap:6px;}}
button:hover{{background:#D62E2F;color:#fff;}}
button:active{{opacity:0.85;}}
.material-symbols-outlined{{font-variation-settings:'FILL' 0,'wght' 400,'GRAD' 0,'opsz' 24;font-size:18px;}}
</style></head><body>
{buttons}
<script>{_SAVE_BLOB_JS}
var FILES={payload};
function dl(i){{saveBlob(FILES[i].url,FILES[i].name);}}
</script></body></html>"""
    try:
        from streamlit.components.v1 import html as component_html
        component_html(markup, height=height, scrolling=False)
        return True
    except Exception as exc:
        log_structured_error(
            exc, page=page or "media_url", component="render_download_buttons",
            operation="render_component", context=", ".join(e["name"] for e in entries),
        )
        return False
