# pages/logout_bridge.py
#
# INVISIBLE BACKEND HANDLER — do NOT add any visible UI here.
#
# Flow:
#   1. Capture id_token from cookie BEFORE clearing it
#   2. Clear local session cookies + session state
#   3. Redirect BROWSER to IdP /csr-idp/logout/ via <meta http-equiv="refresh">
#      — Browsers process meta-refresh even when injected via React dangerouslySetInnerHTML.
#      — stage3 clears its WordPress session cookie, then redirects to
#        post_logout_redirect_uri (the Market Data Portal login page).
#   4. If no id_token: local-only logout (IdP session NOT destroyed — user may be
#      silently re-authed on next sign-in, but this is the safe fallback).
#
# WHY meta-refresh NOT st.html() JavaScript:
#   Streamlit 1.54+ strips <script> via DOMPurify in st.html().
#   components.html() is sandboxed (allow-top-navigation absent).
#   <meta http-equiv="refresh"> is pure HTML — no JavaScript needed.
#   Confirmed: Chrome, Firefox, Safari, Edge all process it when injected via
#   React's dangerouslySetInnerHTML.

import os
import base64
import hashlib
import json as _json
import ipaddress
import tempfile
import time as _time_module
import urllib.parse
from datetime import datetime, timezone, timedelta
from time import sleep, perf_counter
from urllib.parse import urlencode

import streamlit as st
from dotenv import load_dotenv
from core.auth_environment import is_production_deploy, idp_base_url
from utils.server_logger import new_rerun_id, log_structured_error

new_rerun_id("logout_bridge")

try:
    from utils.server_logger import log_warning as slog_warning, log_error as slog_error, log_timing
except ImportError:
    def slog_warning(msg): pass
    def slog_error(msg): pass
    def log_timing(operation, elapsed_ms, details="", level="INFO"): pass

load_dotenv()

# OIDC config — must match login.py
# MUST match the host login.py minted the id_token against — a stage3 token sent
# to coresight.com/csr-idp/logout/ is rejected with "Invalid id_token_hint".
IDP_BASE_URL = idp_base_url()
OIDC_REDIRECT_URI = "https://marketdata.coresight.com" if is_production_deploy() else "https://marketdata-stg.coresight.com"
OIDC_CLIENT_ID = "market-data"
COOKIE_NAME = "auth_session"
LOGOUT_BRIDGE_COOKIE_NAME = "auth_logout_bridge"
LOGOUT_COOKIE_SYNC_WAIT_MS = int(os.getenv("LOGOUT_COOKIE_SYNC_WAIT_MS", "250"))
LOGOUT_COOKIE_SYNC_POLL_MS = int(os.getenv("LOGOUT_COOKIE_SYNC_POLL_MS", "50"))
OIDC_RETURN_CONTEXT_MAX_AGE_SECONDS = int(os.getenv("OIDC_RETURN_CONTEXT_MAX_AGE_SECONDS", "900"))


def _random_urlsafe(nbytes: int = 18) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode().rstrip("=")


def _ephemeral_cache_path(prefix: str, key: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{key}".encode()).hexdigest()[:24]
    return os.path.join(tempfile.gettempdir(), f"_oidc_{prefix}_{digest}.json")


def _write_ephemeral_cache(prefix: str, key: str, data: dict) -> None:
    try:
        path = _ephemeral_cache_path(prefix, key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            _json.dump(data, handle)
    except Exception as e:
        slog_warning(f"[LOGOUT] failed to write {prefix} cache: {e}")


def _get_request_origin() -> str:
    try:
        headers = st.context.headers
        for header_name in ("origin", "referer"):
            raw = (headers.get(header_name) or "").split(",", 1)[0].strip()
            if not raw:
                continue
            parsed = urllib.parse.urlsplit(raw)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"

        host = (
            headers.get("x-forwarded-host")
            or headers.get("host")
            or ""
        ).split(",", 1)[0].strip()
        if not host:
            return ""

        proto = (
            headers.get("x-forwarded-proto")
            or headers.get("x-scheme")
            or ""
        ).split(",", 1)[0].strip().lower()
        hostname = host.split(":", 1)[0].strip().lower()
        if proto not in ("http", "https"):
            proto = "http"
            try:
                ip = ipaddress.ip_address(hostname)
                if not (ip.is_private or ip.is_loopback):
                    proto = "https"
            except ValueError:
                if hostname not in ("localhost", "127.0.0.1", "0.0.0.0"):
                    proto = "https"

        return f"{proto}://{host}"
    except Exception:
        return ""


def _normalize_origin(origin: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(origin or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        hostname = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.scheme}://{hostname}:{port}"
    except Exception:
        return ""


def _is_allowed_return_origin(origin: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(origin or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        if parsed.username or parsed.password or parsed.path not in ("", "/"):
            return False

        hostname = (parsed.hostname or "").lower()
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
            return True
        if hostname.endswith(".coresight.com"):
            return True

        try:
            ip = ipaddress.ip_address(hostname)
            return ip.is_private or ip.is_loopback
        except ValueError:
            return False
    except Exception:
        return False


def _build_post_logout_redirect_uri() -> str:
    current_origin = _get_request_origin()
    if not current_origin or not _is_allowed_return_origin(current_origin):
        return OIDC_REDIRECT_URI
    if _normalize_origin(current_origin) == _normalize_origin(OIDC_REDIRECT_URI):
        return OIDC_REDIRECT_URI

    return_key = _random_urlsafe()
    _write_ephemeral_cache(
        "logout_return",
        return_key,
        {
            "origin": current_origin,
            "ts": int(datetime.now(timezone.utc).timestamp()),
        },
    )

    parsed = urllib.parse.urlsplit(OIDC_REDIRECT_URI)
    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_items.append(("oidc_post_logout", return_key))
    redirect_uri = urllib.parse.urlunsplit(parsed._replace(query=urlencode(query_items)))
    slog_warning(
        f"[LOGOUT] post-logout return handoff prepared | current={current_origin!r} | "
        f"redirect_uri={redirect_uri!r}"
    )
    return redirect_uri

# Minimal page — hide all Streamlit chrome, show "Logging out..."
st.markdown("""
<style>
    header[data-testid="stHeader"], footer, #MainMenu { display: none !important; }
    .stApp, .main, .block-container { padding: 0 !important; margin: 0 !important; }
    .stApp { background: #fff !important; }
    [data-testid="stSidebar"], [data-testid="stSidebarNav"] { display: none !important; }
</style>
<div style="display:flex;align-items:center;justify-content:center;height:100vh;
            font-family:sans-serif;font-size:18px;color:#555;">
    Logging out&hellip;
</div>
""", unsafe_allow_html=True)

slog_warning(
    f"[LOGOUT] logout_bridge.py started | "
    f"IDP_BASE_URL={IDP_BASE_URL!r} | OIDC_CLIENT_ID={OIDC_CLIENT_ID!r}"
)

if st.session_state.get("__logout_oidc_redirect_started"):
    slog_warning("[LOGOUT] duplicate logout_bridge render — redirect already in progress")
    st.stop()


# ── Step 1: Capture id_token BEFORE clearing cookies ────────────────────────
id_token_hint = ""
user_email = "UNKNOWN"
recovery_source = "none"

_recover_start = perf_counter()

bridge_state = st.session_state.get("__logout_bridge_data")
if isinstance(bridge_state, dict) and bridge_state.get("id_token"):
    id_token_hint = bridge_state.get("id_token", "") or ""
    user_email = bridge_state.get("user_email", "UNKNOWN") or "UNKNOWN"
    recovery_source = "session_state_bridge"
    slog_warning(
        f"[LOGOUT] Step 1 — Bridge state read (session_state) | "
        f"id_token=YES (len={len(id_token_hint)}) | user_email={user_email!r}"
    )

if not id_token_hint:
    try:
        raw = st.context.cookies.get(LOGOUT_BRIDGE_COOKIE_NAME)
        if raw:
            decoded = urllib.parse.unquote(raw) if isinstance(raw, str) else raw
            bridge_cookie = _json.loads(decoded) if isinstance(decoded, str) else decoded
            id_token_hint = bridge_cookie.get("id_token", "") or ""
            user_email = bridge_cookie.get("user_email", "UNKNOWN") or "UNKNOWN"
            recovery_source = "st.context.bridge_cookie"
            slog_warning(
                f"[LOGOUT] Step 1 — Bridge state read (st.context cookie) | "
                f"id_token={'YES (len=' + str(len(id_token_hint)) + ')' if id_token_hint else 'NO'} | "
                f"user_email={user_email!r}"
            )
    except Exception as e:
        slog_warning(f"[LOGOUT] Step 1 — bridge st.context cookie read failed: {e}")

if not id_token_hint:
    try:
        # Try st.context.cookies first (sync, no mounting delay)
        raw = st.context.cookies.get(COOKIE_NAME)
        if raw:
            decoded = urllib.parse.unquote(raw) if isinstance(raw, str) else raw
            cookie_data = _json.loads(decoded) if isinstance(decoded, str) else decoded
            id_token_hint = cookie_data.get("id_token", "") or ""
            user_email = cookie_data.get("user_email", "UNKNOWN") or "UNKNOWN"
            recovery_source = "st.context.auth_cookie"
            slog_warning(
                f"[LOGOUT] Step 1 — Cookie read (st.context) | "
                f"id_token={'YES (len=' + str(len(id_token_hint)) + ')' if id_token_hint else 'NO'} | "
                f"user_email={user_email!r}"
            )
    except Exception as e:
        slog_warning(f"[LOGOUT] Step 1 — st.context cookie read failed: {e}")

if not id_token_hint:
    try:
        from streamlit_cookies_controller import CookieController
        _cc = CookieController(key="logout_bridge_cc")
        bridge_raw = _cc.get(LOGOUT_BRIDGE_COOKIE_NAME)
        if bridge_raw:
            bridge_cookie = _json.loads(bridge_raw) if isinstance(bridge_raw, str) else bridge_raw
            if isinstance(bridge_cookie, dict):
                id_token_hint = bridge_cookie.get("id_token", "") or ""
                user_email = bridge_cookie.get("user_email", "UNKNOWN") or "UNKNOWN"
                recovery_source = "CookieController.bridge_cookie"
                slog_warning(
                    f"[LOGOUT] Step 1 — Bridge state read (CookieController) | "
                    f"id_token={'YES' if id_token_hint else 'NO'} | user_email={user_email!r}"
                )
        raw = _cc.get(COOKIE_NAME)
        if raw and not id_token_hint:
            cookie_data = _json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(cookie_data, dict):
                id_token_hint = cookie_data.get("id_token", "") or ""
                user_email = cookie_data.get("user_email", "UNKNOWN") or "UNKNOWN"
                recovery_source = "CookieController.auth_cookie"
                slog_warning(
                    f"[LOGOUT] Step 1 — Cookie read (CookieController) | "
                    f"id_token={'YES' if id_token_hint else 'NO'} | user_email={user_email!r}"
                )
    except Exception as e:
        slog_warning(f"[LOGOUT] Step 1 — CookieController read failed: {e}")

# Also try session_state
if not id_token_hint:
    _auth = st.session_state.get("auth_data") or {}
    id_token_hint = _auth.get("id_token", "") or ""
    if id_token_hint:
        user_email = _auth.get("user_email", "UNKNOWN") or "UNKNOWN"
        recovery_source = "session_state_auth"
        slog_warning(f"[LOGOUT] Step 1 — id_token recovered from session_state | user={user_email!r}")

# Temp file fallback (covers slim-cookie deployments where id_token not in auth_session)
if not id_token_hint:
    _slim_session_id = ""
    try:
        # Try to get session_id from auth_session cookie (slim format)
        _raw_slim = st.context.cookies.get("auth_session") or ""
        if _raw_slim:
            _decoded_slim = urllib.parse.unquote(_raw_slim)
            _slim_data = _json.loads(_decoded_slim) if isinstance(_decoded_slim, str) else _decoded_slim
            _slim_session_id = (_slim_data or {}).get("session_id", "") if isinstance(_slim_data, dict) else ""
        if not _slim_session_id:
            _slim_session_id = (st.session_state.get("auth_data") or {}).get("session_id", "")
    except Exception:
        pass

    if _slim_session_id:
        try:
            _h = hashlib.sha256(_slim_session_id.encode()).hexdigest()[:20]
            _tok_path = os.path.join(tempfile.gettempdir(), f"_oidc_idtok_{_h}.json")
            if os.path.exists(_tok_path) and (_time_module.time() - os.path.getmtime(_tok_path)) < 7 * 86400:
                with open(_tok_path, "r") as _fh:
                    _tok_data = _json.load(_fh)
                if isinstance(_tok_data, dict) and _tok_data.get("id_token"):
                    id_token_hint = _tok_data["id_token"]
                    user_email = _tok_data.get("user_email", user_email) or user_email
                    recovery_source = "temp_file_idtok"
                    slog_warning(
                        f"[LOGOUT] Step 1 — id_token recovered from temp file | "
                        f"session_id={_slim_session_id[:8]} user={user_email!r}"
                    )
        except Exception as _e:
            slog_warning(f"[LOGOUT] Step 1 — temp file id_token read failed: {_e}")

if not id_token_hint:
    slog_warning("[LOGOUT] Step 1 — id_token NOT found in any source — local-only logout")

log_timing(
    "OIDC_LOGOUT_BRIDGE_RECOVER",
    (perf_counter() - _recover_start) * 1000,
    f"source={recovery_source} has_id_token={bool(id_token_hint)}",
)


# ── Steps 2 + 3: Clear session state, clear cookies, redirect — all atomic ───
#
# WHY combined: previously Step 2 injected an iframe JS to clear cookies, and
# Step 3 used <meta http-equiv="refresh" content="0;..."> to redirect. The race:
# meta-refresh delay=0 fired before the iframe's JS had loaded/run, so cookies
# were never cleared and the user re-entered as authenticated.
#
# Fix: single parent-frame JS (via components.v1.html) that:
#   1. Clears both cookies atomically in the parent document
#   2. Navigates window.location.href to IdP logout (or login if no id_token)
# No separate meta-refresh needed — redirect happens inside the same script.
slog_warning(f"[LOGOUT] Step 2 — Clearing local auth | user={user_email!r}")

# Set guard flag + clear server-side session state
st.session_state["__just_logged_out"] = True
for _k in ["auth_data", "authenticated", "_auth_invalidated",
           "__completed_oidc_code", "__completed_oidc_token_data",
           "__oidc_cb_code", "__oidc_cb_state", "__oidc_cb_captured",
           "__oidc_exchange_result", "__logout_bridge_data",
           "__completed_oidc_handoff_key", "__completed_oidc_handoff_token_data",
           "__failed_oidc_handoff_key", "__completed_oidc_post_logout_key"]:
    st.session_state.pop(_k, None)

slog_warning(f"[LOGOUT] Step 2 — Session state cleared | user={user_email!r}")

# ── Step 3: Single atomic JS — clear cookies + navigate ─────────────────────
_oidc_configured = bool(IDP_BASE_URL and OIDC_CLIENT_ID)

try:
    import streamlit.components.v1 as _components_v1
    from core.auth_manager import build_cookie_clear_js_lines

    _clear_js = (
        build_cookie_clear_js_lines(COOKIE_NAME)
        + build_cookie_clear_js_lines(LOGOUT_BRIDGE_COOKIE_NAME)
    )

    if _oidc_configured and id_token_hint:
        st.session_state["__logout_oidc_redirect_started"] = True
        logout_params = {"id_token_hint": id_token_hint}
        post_logout_redirect_uri = _build_post_logout_redirect_uri() if OIDC_REDIRECT_URI else ""
        if post_logout_redirect_uri:
            logout_params["post_logout_redirect_uri"] = post_logout_redirect_uri
        _dest = f"{IDP_BASE_URL}/csr-idp/logout/?{urlencode(logout_params)}"
        slog_warning(
            f"[LOGOUT] Step 3 — JS clear+redirect to IdP | user={user_email!r} | "
            f"idp={IDP_BASE_URL!r} | "
            f"post_logout_redirect_uri={logout_params.get('post_logout_redirect_uri', 'NOT_SET')!r}"
        )
    else:
        st.session_state.pop("__logout_oidc_redirect_started", None)
        _dest = (OIDC_REDIRECT_URI or "").rstrip("/") + "/" if OIDC_REDIRECT_URI else "/"
        slog_warning(
            f"[LOGOUT] Step 3 — JS clear+redirect to login (local-only) | "
            f"oidc_configured={_oidc_configured} | id_token={'YES' if id_token_hint else 'NO'}"
        )

    _dest_js = _json.dumps(_dest)
    _parent_script_body = f"{_clear_js}window.location.href={_dest_js};"
    _parent_script_js = _json.dumps(_parent_script_body)
    _combined_script = f"""<script>
(function() {{
  try {{
    var pdoc = window.parent.document;
    var s = pdoc.createElement('script');
    s.textContent = {_parent_script_js};
    pdoc.head.appendChild(s);
    s.parentNode && s.parentNode.removeChild(s);
  }} catch(e) {{
    try {{ window.parent.location.href = {_dest_js}; }} catch(e3) {{}}
  }}
}})();
</script>"""

    _components_v1.html(_combined_script, height=0)
    log_timing(
        "OIDC_LOGOUT_JS_CLEAR_REDIRECT", 0,
        f"clear=host_only_and_legacy oidc={_oidc_configured and bool(id_token_hint)} dest_len={len(_dest)}",
        level="WARNING",
    )
    slog_warning(f"[LOGOUT] Step 3 — JS injected (clear+redirect) | dest={_dest[:80]!r}")
    st.stop()

except Exception as e:
    slog_warning(f"[LOGOUT] Step 3 — JS clear+redirect failed, using st.switch_page fallback: {e}")
    st.switch_page("pages/login.py")
    st.stop()
