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
<script>
var FILES={payload};
function dl(i){{
  var f=FILES[i];
  fetch(f.url).then(function(r){{
    if(!r.ok) throw new Error("HTTP "+r.status);
    return r.blob();
  }}).then(function(blob){{
    var url=URL.createObjectURL(blob);
    var a=document.createElement("a");
    a.href=url; a.download=f.name;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(function(){{URL.revokeObjectURL(url);}},200);
  }}).catch(function(e){{console.error("Download failed:",f.name,e);}});
}}
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
