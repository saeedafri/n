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
from typing import Optional

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
