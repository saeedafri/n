"""
Authentication Manager - Session Token Pattern
- Login: Store in DB + Cookie
- Auth check: Cookie only (fast)
- DB used for audit, not validation

Cookie design (STG/prod):
  auth_session = slim JSON (session_id, user_email, user_display_name, login_at)
                 NO id_token — keeps cookie well under 4 KB browser limit.
  id_token is stored in a temp-file cache (keyed by session_id) for OIDC logout
  and also kept in st.session_state.auth_data for same-session logout.
"""
import os
import json
import uuid
import hashlib
import tempfile
import time as time_module
import urllib.parse
from datetime import datetime, timezone, timedelta
from time import sleep
from typing import Optional, Dict, Any
from dataclasses import dataclass

import streamlit as st
from dotenv import load_dotenv
from streamlit_cookies_controller import CookieController

from core.database import db_manager

# Import error logger only
try:
    from utils.server_logger import log_structured_error, log_error, log_exception, log_timing, log_info
except ImportError:
    def log_error(msg):
        pass
    def log_structured_error(exc, **kwargs):
        pass
    def log_exception(msg):
        pass
    def log_timing(operation, elapsed_ms, details="", level="INFO"):
        pass
    def log_info(msg):
        pass

load_dotenv()
APP_ENV = os.getenv("APP_ENV", "staging").strip().lower()

# Cookie settings
COOKIE_NAME = "auth_session"
COOKIE_MAX_AGE_DAYS = 7
LEGACY_COOKIE_DOMAIN = ".coresight.com"
LOGOUT_BRIDGE_COOKIE_NAME = "auth_logout_bridge"
_LOGOUT_BRIDGE_MAX_AGE_SECONDS = int(os.getenv("AUTH_LOGOUT_BRIDGE_MAX_AGE_SECONDS", "120"))


def _auth_verbose() -> bool:
    return os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1"


def _request_is_secure() -> bool:
    """True when auth cookies should include the Secure attribute."""
    try:
        headers = st.context.headers
        proto = (
            headers.get("x-forwarded-proto")
            or headers.get("x-scheme")
            or ""
        ).split(",", 1)[0].strip().lower()
        if proto == "https":
            return True
        host = (headers.get("host") or "").split(":", 1)[0].strip().lower()
        if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.startswith("192."):
            return False
        return True
    except Exception:
        return True


def get_auth_flow_id() -> str:
    """Correlation ID for the current SSO/login journey (safe to log)."""
    try:
        return str(st.session_state.get("auth_flow_id") or "")
    except Exception:
        return ""


def get_or_create_auth_flow_id() -> str:
    """Create auth_flow_id at SSO callback start if not already set."""
    existing = get_auth_flow_id()
    if existing:
        return existing
    flow_id = uuid.uuid4().hex[:16]
    try:
        st.session_state["auth_flow_id"] = flow_id
    except Exception:
        pass
    return flow_id


def _auth_request_meta() -> str:
    """Safe request metadata for auth diagnostics (no secrets)."""
    try:
        headers = st.context.headers
        host = (headers.get("host") or "").split(",", 1)[0].strip()
        referer = (headers.get("referer") or "").split(",", 1)[0].strip()
        origin = (headers.get("origin") or "").split(",", 1)[0].strip()
        qp_keys = ",".join(sorted(dict(st.query_params).keys()))
        return (
            f"auth_flow_id={get_auth_flow_id() or '-'} "
            f"host={host!r} referer={referer!r} origin={origin!r} "
            f"qp_keys=[{qp_keys}]"
        )
    except Exception:
        return f"auth_flow_id={get_auth_flow_id() or '-'}"


def log_auth_cookie_server_presence(page: str = "", context: str = "") -> bool:
    """Log whether the server sees auth_session in st.context.cookies."""
    present = False
    byte_len = 0
    try:
        raw = st.context.cookies.get(COOKIE_NAME)
        if raw:
            present = True
            byte_len = len(raw.encode("utf-8")) if isinstance(raw, str) else 0
    except Exception:
        pass
    marker = "AUTH_COOKIE_SERVER_SEEN" if present else "AUTH_COOKIE_SERVER_MISSING"
    log_timing(
        marker,
        0,
        f"{_auth_request_meta()} page={page or '-'} context={context} cookie_bytes={byte_len}",
        level="WARNING",
    )
    return present


# ── Adaptive auth retry backoff ────────────────────────────────────────────────
# st.context.cookies (sync, HTTP request headers) is tried first in _get_cookie().
# After login handoff, every protected page should already have a real cookie.
# Tiny retry budget only for rare browser sync races. Override via ENV.
_RETRY_DELAYS_STR = os.getenv("AUTH_RETRY_DELAYS", "0.15,0.35")
_AUTH_RETRY_DELAYS: list = [float(x.strip()) for x in _RETRY_DELAYS_STR.split(",")]
_COOKIE_SYNC_WAIT_MS = int(os.getenv("AUTH_COOKIE_SYNC_WAIT_MS", "350"))
_COOKIE_SYNC_POLL_MS = int(os.getenv("AUTH_COOKIE_SYNC_POLL_MS", "50"))
_LOGOUT_BRIDGE_SYNC_WAIT_MS = int(os.getenv("AUTH_LOGOUT_BRIDGE_SYNC_WAIT_MS", "150"))
_LOGOUT_BRIDGE_SYNC_POLL_MS = int(os.getenv("AUTH_LOGOUT_BRIDGE_SYNC_POLL_MS", "50"))


# ── id_token temp-file cache ───────────────────────────────────────────────────
# id_token is NOT stored in the auth_session cookie (too large, risks >4 KB).
# It is persisted here for OIDC logout across browser refreshes/new sessions.
_ID_TOKEN_TTL = COOKIE_MAX_AGE_DAYS * 86400  # same lifetime as cookie


def _id_token_path(session_id: str) -> str:
    h = hashlib.sha256(session_id.encode()).hexdigest()[:20]
    return os.path.join(tempfile.gettempdir(), f"_oidc_idtok_{h}.json")


def _store_id_token(session_id: str, id_token: str, user_email: str = "") -> None:
    """Persist id_token to temp file for cross-session OIDC logout."""
    if not session_id or not id_token:
        return
    try:
        path = _id_token_path(session_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"id_token": id_token, "user_email": user_email, "ts": time_module.time()}, fh)
    except Exception:
        pass


def _load_id_token(session_id: str) -> str:
    """Retrieve id_token from temp file."""
    if not session_id:
        return ""
    try:
        path = _id_token_path(session_id)
        if not os.path.exists(path):
            return ""
        age = time_module.time() - os.path.getmtime(path)
        if age > _ID_TOKEN_TTL:
            try:
                os.unlink(path)
            except OSError:
                pass
            return ""
        with open(path, "r") as fh:
            data = json.load(fh)
        return data.get("id_token", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


# ── Server-side session blacklist ──────────────────────────────────────────────
# JS cookie deletion races with browser navigation and is unreliable.
# This blacklist is the authoritative logout signal: even if the browser still
# sends the stale auth_session cookie after logout, _get_cookie() rejects it.
# Stored in /tmp so all workers on the same machine share it (single-instance
# Azure App Service STG). Same TTL as the cookie (7 days).

def _invalidated_path(session_id: str) -> str:
    h = hashlib.sha256(session_id.encode()).hexdigest()[:20]
    return os.path.join(tempfile.gettempdir(), f"_oidc_invalid_{h}.json")


def invalidate_session(session_id: str, user_email: str = "") -> None:
    """Mark session as logged-out. _get_cookie() will reject it."""
    if not session_id:
        return
    try:
        path = _invalidated_path(session_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"session_id": session_id, "user_email": user_email, "ts": time_module.time()}, fh)
    except Exception:
        pass


def _is_session_invalidated(session_id: str) -> bool:
    """Return True if session was explicitly logged out."""
    if not session_id:
        return False
    try:
        path = _invalidated_path(session_id)
        if not os.path.exists(path):
            return False
        if time_module.time() - os.path.getmtime(path) > _ID_TOKEN_TTL:
            try:
                os.unlink(path)
            except OSError:
                pass
            return False
        return True
    except Exception:
        return False


def get_current_domain(default: Optional[str] = None) -> str:
    """Best-effort current host → cookie domain (normalized)."""
    try:
        headers = st.context.headers
        host = headers.get("host", "")

        if not host:
            try:
                from streamlit.web.server.websocket_headers import _get_websocket_headers
                ws = _get_websocket_headers() or {}
                host = ws.get("host") or ws.get("Host") or ""
            except ImportError:
                pass

        # Extract only the domain part (ignore port if present)
        domain = host.split(":", 1)[0].strip().lower()

        # --- SIP-style normalization (stable) ---
        if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
            try:
                log_info(f"[AUTH_FLOW] domain_resolve inputs | host={host!r}")
            except Exception:
                pass

        if domain in ("localhost", "0.0.0.0", "127.0.0.1"):
            if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
                try:
                    log_info("[AUTH_FLOW] domain_resolve result | chosen='localhost' reason=local_host")
                except Exception:
                    pass
            return "localhost"

        if domain.endswith(".coresight.com"):
            if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
                try:
                    log_info(f"[AUTH_FLOW] domain_resolve result | chosen='.coresight.com' reason=coresight_host host={domain!r}")
                except Exception:
                    pass
            return ".coresight.com"

        if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
            try:
                log_info(f"[AUTH_FLOW] domain_resolve result | chosen={default or ''!r} reason=fallback_unrecognized_host host={domain!r}")
            except Exception:
                pass
        return default or ""
    except Exception:
        return default or ""


@dataclass
class AuthResult:
    """Result of authentication attempt."""
    success: bool
    session_id: Optional[str] = None
    user_email: Optional[str] = None
    error_message: Optional[str] = None


def _inject_parent_document_script(parent_script_body: str) -> None:
    """Run JS in the parent Streamlit document via components.v1.html iframe."""
    import streamlit.components.v1 as components_v1

    body_js = json.dumps(parent_script_body)
    iframe_script = f"""<script>
(function() {{
  try {{
    var pdoc = window.parent.document;
    var s = pdoc.createElement('script');
    s.textContent = {body_js};
    pdoc.head.appendChild(s);
    s.parentNode && s.parentNode.removeChild(s);
  }} catch (e) {{}}
}})();
</script>"""
    components_v1.html(iframe_script, height=0)


def build_cookie_clear_js_lines(cookie_name: str, secure: Optional[bool] = None) -> str:
    """JS lines that expire host-only and legacy .coresight.com cookies."""
    if secure is None:
        secure = _request_is_secure()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    expires_str = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    secure_suffix = "; Secure" if secure else ""
    host_clear = f"{cookie_name}=; expires={expires_str}; path=/; SameSite=Lax{secure_suffix}"
    domain_clear = (
        f"{cookie_name}=; expires={expires_str}; path=/; "
        f"domain={LEGACY_COOKIE_DOMAIN}; SameSite=Lax{secure_suffix}"
    )
    return (
        f"document.cookie={json.dumps(host_clear)};\n"
        f"document.cookie={json.dumps(domain_clear)};\n"
    )


def build_host_only_set_cookie_js(cookie_name: str, cookie_value_json: str) -> str:
    """Build host-only Set-Cookie assignment (no Domain attribute)."""
    expires_dt = datetime.now(timezone.utc) + timedelta(days=COOKIE_MAX_AGE_DAYS)
    expires_str = expires_dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
    encoded = urllib.parse.quote(cookie_value_json)
    secure_suffix = "; Secure" if _request_is_secure() else ""
    cookie_str = f"{cookie_name}={encoded}; expires={expires_str}; path=/; SameSite=Lax{secure_suffix}"
    return f"document.cookie={json.dumps(cookie_str)};"


def render_auth_cookie_handoff_redirect(
    target_path: str = "/home",
    message: str = "Completing sign-in…",
) -> None:
    """
    Browser handoff: clear stale cookies, set host-only auth_session, poll, hard-redirect.
    Login is not complete until the next HTTP request includes auth_session.
    """
    auth_data = st.session_state.get("auth_data") or {}
    session_id = auth_data.get("session_id") or ""
    user_email = auth_data.get("user_email") or ""
    if not session_id or not user_email:
        log_timing(
            "AUTH_LOGIN_HANDOFF_FAILED",
            0,
            f"{_auth_request_meta()} reason=missing_session_state_auth_data",
            level="WARNING",
        )
        st.error("Sign-in could not be completed. Please try again.")
        return

    slim_payload = {
        "session_id": session_id,
        "user_email": user_email,
        "user_display_name": auth_data.get("user_display_name", ""),
        "login_at": auth_data.get("login_at", ""),
    }
    cookie_value_json = json.dumps(slim_payload, separators=(",", ":"))
    cookie_bytes = len(cookie_value_json.encode("utf-8"))
    flow_id = get_or_create_auth_flow_id()
    target = target_path if target_path.startswith("/") else f"/{target_path}"
    success_url = (
        f"{target}?login_handoff=1&auth_flow_id={urllib.parse.quote(flow_id)}&browser_cookie_ok=1"
    )
    fail_url = (
        f"/login?auth_error=cookie_write_failed&auth_flow_id={urllib.parse.quote(flow_id)}"
        f"&browser_cookie_ok=0"
    )

    clear_lines = (
        build_cookie_clear_js_lines(COOKIE_NAME)
        + build_cookie_clear_js_lines(LOGOUT_BRIDGE_COOKIE_NAME)
    )
    set_line = build_host_only_set_cookie_js(COOKIE_NAME, cookie_value_json)
    parent_script = (
        f"{clear_lines}"
        f"{set_line}\n"
        "var _ok=false,_n=0;\n"
        "function _poll(){\n"
        "  _n++;\n"
        "  try{_ok=document.cookie.indexOf('auth_session=')!==-1;}catch(e){}\n"
        f"  if(_ok||_n>=20){{window.location.replace(_ok?{json.dumps(success_url)}:{json.dumps(fail_url)});return;}}\n"
        "  setTimeout(_poll,50);\n"
        "}\n"
        "_poll();\n"
    )

    if message:
        overlay = (
            "<style>"
            "#csr-handoff-overlay{position:fixed;inset:0;z-index:999999;background:#fff;"
            "display:flex;flex-direction:column;align-items:center;justify-content:center;"
            "font-family:Inter,sans-serif;}"
            ".csr-handoff-spin{width:40px;height:40px;border:3px solid #f0f0f0;"
            "border-top-color:#D62E2F;border-radius:50%;animation:csr-handoff-spin .8s linear infinite;}"
            "@keyframes csr-handoff-spin{to{transform:rotate(360deg);}}"
            ".csr-handoff-text{margin-top:16px;color:#888;font-size:14px;}"
            "</style>"
            '<div id="csr-handoff-overlay">'
            '<div class="csr-handoff-spin"></div>'
            f'<div class="csr-handoff-text">{message}</div>'
            "</div>"
        )
        st.markdown(overlay, unsafe_allow_html=True)

    log_timing(
        "AUTH_COOKIE_HANDOFF_EMITTED",
        0,
        f"{_auth_request_meta()} bytes={cookie_bytes} host_only=true secure={_request_is_secure()} "
        f"session_id={session_id[:8]} user={user_email} target={target!r}",
        level="WARNING",
    )
    _inject_parent_document_script(parent_script)


class AuthManager:
    """Manages user authentication with session token pattern."""

    def _get_controller(self) -> CookieController:
        """Get CookieController for this Streamlit session.

        IMPORTANT:
        Streamlit treats components as widgets keyed by `key=...`. Creating a new
        CookieController with the same key multiple times in one session can raise:
          `st.session_state.auth_cookies cannot be modified after the widget with key auth_cookies is instantiated.`

        So we create it once per session and reuse it via st.session_state.
        """
        try:
            ctrl = st.session_state.get("__auth_cookie_controller")
            if isinstance(ctrl, CookieController):
                try:
                    if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
                        log_info("[AUTH_FLOW] CookieController reuse | key=auth_cookies source=session_state_cached")
                except Exception:
                    pass
                return ctrl
        except Exception:
            pass

        ctrl = CookieController(key="auth_cookies")
        try:
            st.session_state["__auth_cookie_controller"] = ctrl
        except Exception:
            pass
        try:
            if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
                log_info("[AUTH_FLOW] CookieController create | key=auth_cookies source=new_instance")
        except Exception:
            pass
        return ctrl

    # =========================================================================
    # LOGIN FLOW
    # =========================================================================

    def login(self, user_email: str, user_nicename: str,
              user_display_name: str, token: str,
              id_token: str = "", wp_user_id=None) -> AuthResult:
        """
        Complete login flow:
        1. Generate session_id
        2. Store in database (audit trail)
        3. Set session state (cookie written by render_auth_cookie_handoff_redirect)
        """
        _start = time_module.perf_counter()
        db_success = False
        try:
            # Generate cryptographically secure session ID
            session_id = self._generate_session_id()

            # Store in database (for audit, can be disabled if DB down)
            _db_start = time_module.perf_counter()
            db_success = self._store_session_in_db(
                user_email=user_email,
                user_nicename=user_nicename,
                user_display_name=user_display_name,
                token=token
            )
            log_timing(
                "AUTH_SESSION_STORE",
                (time_module.perf_counter() - _db_start) * 1000,
                f"{_auth_request_meta()} user={user_email} success={db_success} "
                f"session_id={session_id[:8]}",
            )

            # Slim cookie payload — id_token and wp_user_id are intentionally
            # excluded to keep auth_session well under the 4 KB browser limit.
            # id_token is stored in a temp file and in session_state for logout.
            login_at = datetime.now(timezone.utc).isoformat()
            cookie_auth_data = {
                "session_id": session_id,
                "user_email": user_email,
                "user_display_name": user_display_name,
                "login_at": login_at,
            }

            # Full auth_data for session_state (includes id_token for same-session logout)
            full_auth_data = {
                **cookie_auth_data,
                "id_token": id_token,
                "wp_user_id": wp_user_id,
            }

            # Persist id_token to temp file for cross-session OIDC logout
            _store_id_token(session_id, id_token, user_email)

            # Set session state for handoff (full data including id_token for same-session logout)
            st.session_state.auth_data = full_auth_data
            st.session_state.authenticated = True
            # Clear any previous logout invalidation
            st.session_state.pop("_auth_invalidated", None)

            return AuthResult(
                success=True,
                session_id=session_id,
                user_email=user_email
            )

        except Exception as e:
            log_structured_error(e, page="auth_manager", component="login", operation="user_login", context=f"user={user_email}")
            return AuthResult(
                success=False,
                error_message="Login failed. Please try again."
            )
        finally:
            log_timing(
                "AUTH_LOGIN_TOTAL",
                (time_module.perf_counter() - _start) * 1000,
                f"{_auth_request_meta()} user={user_email} db={db_success} "
                f"handoff_pending=true session_id={session_id[:8]}",
            )

    def _generate_session_id(self) -> str:
        """Generate secure random session ID."""
        try:
            return uuid.uuid4().hex  # 32 character hex string
        except Exception as e:
            log_structured_error(e, page="auth_manager", component="_generate_session_id", operation="generate_uuid")
            raise

    def _store_session_in_db(self,  user_email: str,
                             user_nicename: str, user_display_name: str,
                             token: str) -> bool:
        """
        Store session in database for audit trail.
        Non-blocking: Login succeeds even if DB fails.
        """
        try:
            query = """
            INSERT INTO market_data_user_sessions
                (user_email, user_nicename, user_display_name, token,
                 login_at, last_activity)
            VALUES
                (:user_email, :user_nicename, :user_display_name, :token,
                 NOW(), NOW())
            """

            db_manager.execute_insert(query, {
                "user_email": user_email,
                "user_nicename": user_nicename,
                "user_display_name": user_display_name,
                "token": token
            })

            return True

        except Exception:
            # Don't fail login - cookie is source of truth
            log_structured_error(Exception("DB session store failed"), page="auth_manager", component="_store_session_in_db", operation="insert_session")
            return False

    # =========================================================================
    # JS-INJECTION COOKIE HELPERS
    #
    # On STG (Azure App Service behind a reverse proxy), CookieController
    # consistently fails in two ways:
    #
    #   1. StreamlitAPIException: "st.session_state.auth_cookies cannot be
    #      modified after the widget with key auth_cookies is instantiated."
    #      — happens when _get_cookie() renders the widget first, then
    #        _set_cookie() tries to create a second instance with the same key.
    #
    #   2. Even when CookieController.set() appears to succeed (synced=True),
    #      the cookie is never in the browser.  The sync check reads from
    #      st.session_state["auth_cookies"] (in-memory mirror), NOT from the
    #      real browser cookie jar — so synced=True is a false positive.
    #
    # The fix: use the same parent-frame JS injection that the SSO button in
    # login.py already uses and that is proven to work on STG.  This injects
    # a <script> into the parent Streamlit document which runs document.cookie=
    # in the main frame — no iframe sandbox restrictions, same origin, works.
    #
    # CookieController is kept only for READING (as a fallback after
    # st.context.cookies, which reads HTTP request headers synchronously).
    # =========================================================================

    @staticmethod
    def _build_cookie_string(name: str, value: str, expires_dt, domain: str, delete: bool = False) -> str:
        """Build a Set-Cookie string for JS injection."""
        encoded = urllib.parse.quote(value)
        if delete:
            expires_str = "Thu, 01 Jan 1970 00:00:00 GMT"
            encoded = ""
        else:
            expires_str = expires_dt.strftime("%a, %d %b %Y %H:%M:%S GMT")

        parts = [f"{name}={encoded}", f"expires={expires_str}", "path=/"]
        if domain:
            parts.append(f"domain={domain}")
        # SameSite=Lax works for both http and https.  Secure is added for
        # non-localhost HTTPS domains so Chrome doesn't silently drop it.
        is_secure_domain = domain and domain not in ("localhost", "127.0.0.1", "0.0.0.0") and not domain.startswith("192.")
        parts.append("SameSite=Lax")
        if is_secure_domain:
            parts.append("Secure")
        return "; ".join(parts)

    def stash_logout_context(self, auth_data: Optional[Dict[str, Any]] = None) -> bool:
        """Persist minimal OIDC logout context for logout_bridge.py."""
        start = time_module.perf_counter()
        synced = False
        source = "none"
        try:
            payload = auth_data or st.session_state.get("auth_data") or self._get_cookie() or {}
            if not isinstance(payload, dict):
                return False

            id_token = payload.get("id_token") or ""
            user_email = payload.get("user_email") or "UNKNOWN"
            session_id = payload.get("session_id") or ""

            # id_token may be absent from cookie (slim cookie design) — fetch from temp file
            if not id_token and session_id:
                id_token = _load_id_token(session_id)
                if id_token:
                    log_timing(
                        "AUTH_STASH_LOGOUT_ID_TOKEN",
                        0,
                        f"source=temp_file session_id={session_id[:8]} user={user_email}",
                        level="WARNING",
                    )

            # Blacklist the session NOW — guaranteed point where session_id is in scope.
            # This makes _get_cookie() reject the stale browser cookie even before
            # logout_bridge.py runs and even if the JS cookie deletion races/fails.
            if session_id:
                invalidate_session(session_id, user_email)
                log_timing(
                    "AUTH_SESSION_BLACKLISTED",
                    0,
                    f"session_id={session_id[:8]} user={user_email}",
                    level="WARNING",
                )

            if not id_token:
                log_timing(
                    "AUTH_STASH_LOGOUT_ID_TOKEN",
                    0,
                    f"source=NOT_FOUND session_id={session_id[:8] if session_id else '?'} user={user_email}",
                    level="WARNING",
                )
                return False

            bridge_data = {
                "id_token": id_token,
                "user_email": user_email,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            st.session_state["__logout_bridge_data"] = bridge_data
            source = "session_state"

            cookie_value = json.dumps(bridge_data, separators=(",", ":"))
            expires_dt = datetime.now(timezone.utc) + timedelta(seconds=_LOGOUT_BRIDGE_MAX_AGE_SECONDS)
            expires_str = expires_dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
            encoded = urllib.parse.quote(cookie_value)
            secure_suffix = "; Secure" if _request_is_secure() else ""
            bridge_cookie_str = (
                f"{LOGOUT_BRIDGE_COOKIE_NAME}={encoded}; expires={expires_str}; "
                f"path=/; SameSite=Lax{secure_suffix}"
            )
            parent_script = f"document.cookie={json.dumps(bridge_cookie_str)};"
            _inject_parent_document_script(parent_script)
            synced = True
            source = "cookie+session_state"

            log_timing(
                "AUTH_LOGOUT_BRIDGE_STASH",
                0,
                f"synced={synced} user={user_email}",
                level="WARNING",
            )
            return True
        except Exception as e:
            log_structured_error(e, page="auth_manager", component="stash_logout_context", operation="stash_logout_context")
            return False
        finally:
            log_timing(
                "AUTH_LOGOUT_BRIDGE_TOTAL",
                (time_module.perf_counter() - start) * 1000,
                f"source={source} synced={synced}",
            )

    # =========================================================================
    # AUTH CHECK (NO DB CALL - FAST)
    # =========================================================================

    def is_authenticated(self) -> bool:
        """
        Check if user is authenticated.
        FAST: Only checks cookie, no database call.
        """
        start = time_module.perf_counter()
        source = "none"
        result = False
        try:
            # Auth was explicitly invalidated (logout) — don't restore from
            # stale cookie still present in st.context.cookies HTTP headers.
            if st.session_state.get("_auth_invalidated"):
                if _auth_verbose():
                    log_timing(
                        "AUTH_IS_AUTHENTICATED",
                        0,
                        f"{_auth_request_meta()} result=False source=invalidated",
                        level="WARNING",
                    )
                return False

            # Fast path: already in session state (same Streamlit session)
            if st.session_state.get("authenticated") and st.session_state.get("auth_data"):
                source = "session_state"
                result = True
                if _auth_verbose():
                    _ad = st.session_state.get("auth_data") or {}
                    log_timing(
                        "AUTH_IS_AUTHENTICATED",
                        (time_module.perf_counter() - start) * 1000,
                        f"{_auth_request_meta()} result=True path=session_state "
                        f"user={_ad.get('user_email', '?')} session={str(_ad.get('session_id', ''))[:8]}",
                        level="WARNING",
                    )
                return True

            # Restore from HTTP cookie (cross-page / fresh request)
            auth_data = self._get_cookie()

            if auth_data and auth_data.get("session_id"):
                st.session_state.auth_data = auth_data
                st.session_state.authenticated = True
                source = st.session_state.get("_auth_cookie_source", "cookie")
                result = True
                if _auth_verbose():
                    log_timing(
                        "AUTH_IS_AUTHENTICATED",
                        (time_module.perf_counter() - start) * 1000,
                        f"{_auth_request_meta()} result=True path=cookie source={source} "
                        f"user={auth_data.get('user_email', '?')} "
                        f"session={auth_data.get('session_id', '?')[:8]}",
                        level="WARNING",
                    )
                return True

            source = "missing"
            if _auth_verbose():
                log_timing(
                    "AUTH_IS_AUTHENTICATED",
                    (time_module.perf_counter() - start) * 1000,
                    f"{_auth_request_meta()} result=False path=missing",
                    level="WARNING",
                )
            return False
        except Exception as e:
            log_structured_error(e, page="auth_manager", component="is_authenticated", operation="check_auth_state")
            return False
        finally:
            elapsed_ms = (time_module.perf_counter() - start) * 1000
            if elapsed_ms > 25 or source not in ("session_state", "none"):
                log_timing(
                    "AUTH_IS_AUTHENTICATED",
                    elapsed_ms,
                    f"{_auth_request_meta()} result={result} source={source}",
                )

    def require_auth(self, redirect_to: str = "login", page: str = "") -> Optional[Dict[str, Any]]:
        """
        Require authentication for protected pages.
        Uses st.rerun() retry to handle async CookieController mount on refresh.
        SECURITY: st.stop() is called after redirect/rerun as a safety net
                  to guarantee execution NEVER continues past this point.

        DEBUG MODE: Auth is bypassed when APP_ENV=LOCAL and DEBUG=true
        """
        start = time_module.perf_counter()
        outcome = "unknown"
        try:
            log_only = os.getenv("AUTH_REQUIRE_AUTH_LOG_ONLY", "0").strip() == "1"
            bypass = os.getenv("AUTH_REQUIRE_AUTH_BYPASS", "0").strip() == "1"
            non_blocking = log_only or bypass
            if non_blocking:
                try:
                    log_info(f"[AUTH_FLOW] require_auth non-blocking enabled | page={page or '?'} redirect_to={redirect_to} log_only={log_only} bypass={bypass}")
                except Exception:
                    pass

            # DEBUG MODE BYPASS: Allow access without auth in local debug mode.
            # Gate: APP_ENV=LOCAL and DEBUG=true only.
            # Set LOCAL_TEST_USER_EMAIL=<email> to impersonate a specific account
            # (e.g. mohdsaeedafri@coresight.com for admin-level testing).
            # Never active in staging/production because APP_ENV is not "LOCAL" there.
            app_env = os.getenv("APP_ENV", "").upper()
            debug_mode = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")
            if app_env == "LOCAL" and debug_mode:
                _bypass_email = (
                    os.getenv("LOCAL_TEST_USER_EMAIL", "").strip()
                    or "local@test.com"
                )
                if not st.session_state.get("authenticated"):
                    st.session_state.auth_data = {
                        "session_id": "local-debug-session",
                        "user_email": _bypass_email,
                        "user_display_name": "Local Test User",
                        "login_at": datetime.now(timezone.utc).isoformat()
                    }
                    st.session_state.authenticated = True
                outcome = "debug_bypass"
                return st.session_state.get("auth_data")

            # ── Quick exit: auth explicitly invalidated (logout) ──────────────────
            if st.session_state.get("_auth_invalidated"):
                outcome = "redirect_invalidated"
                _redirect_target = f"pages/{redirect_to}.py"
                log_timing(
                    "AUTH_REQUIRE_REDIRECT_LOGIN",
                    0,
                    f"{_auth_request_meta()} reason=session_invalidated page={page or '?'} "
                    f"redirect_target={_redirect_target}",
                    level="WARNING",
                )
                log_timing(
                    "AUTH_REQUIRE_AUTH",
                    0,
                    f"{_auth_request_meta()} outcome=redirect_invalidated "
                    f"redirect_to={redirect_to} page={page or '?'}",
                    level="WARNING",
                )
                if not non_blocking:
                    st.switch_page(_redirect_target)
                    st.stop()
                return None

            # ── NON-BLOCKING mode: run auth check + timed waits, never redirect/stop ─
            if non_blocking:
                _has_session = bool(st.session_state.get("authenticated") and st.session_state.get("auth_data"))
                log_timing(
                    "AUTH_REQUIRE_AUTH_ENTRY",
                    0,
                    f"has_session_state={_has_session} page={page or '?'} redirect_to={redirect_to} mode=log_only",
                    level="WARNING",
                )

                if self.is_authenticated():
                    outcome = "allowed_log_only"
                    return st.session_state.get("auth_data")

                # Cookie may be set via JS asynchronously; wait budget from AUTH_RETRY_DELAYS
                delays = _AUTH_RETRY_DELAYS or []
                for i, delay in enumerate(delays, start=1):
                    log_timing(
                        "AUTH_REQUIRE_AUTH_RETRY",
                        0,
                        f"attempt={i} delay={delay}s page={page or '?'} redirect_to={redirect_to} mode=log_only",
                        level="WARNING",
                    )
                    sleep(delay)
                    if self.is_authenticated():
                        outcome = f"allowed_after_wait_{i}_log_only"
                        return st.session_state.get("auth_data")

                outcome = "log_only_exhausted_allow"
                log_timing(
                    "AUTH_REQUIRE_AUTH_EXHAUSTED",
                    0,
                    f"all retries used — NOT redirecting (log-only) page={page or '?'} redirect_to={redirect_to}",
                    level="WARNING",
                )
                return None

            # ── Detect whether this is a fresh page load or a retry rerun ──────────
            _is_retry_rerun = st.session_state.get("_auth_retry_in_progress", False)
            if not _is_retry_rerun:
                st.session_state._auth_retry_count = 0
                st.session_state._auth_retry_elapsed_ms = 0.0

            _has_session = bool(st.session_state.get("authenticated") and st.session_state.get("auth_data"))
            if _auth_verbose():
                log_auth_cookie_server_presence(page=page or "require_auth", context="entry")
                log_timing(
                    "AUTH_REQUIRE_AUTH_ENTRY",
                    0,
                    f"{_auth_request_meta()} has_session_state={_has_session} "
                    f"retry_in_progress={_is_retry_rerun} "
                    f"retry_count={st.session_state.get('_auth_retry_count', 0)} "
                    f"redirect_to={redirect_to} page={page or '?'}",
                    level="WARNING",
                )

            # ── Fast path: session_state already has auth ────────────────────────────
            if self.is_authenticated():
                # Clear retry tracking state
                st.session_state._auth_retry_count = 0
                st.session_state._auth_retry_in_progress = False
                st.session_state._auth_retry_elapsed_ms = 0.0
                outcome = "allowed"
                return st.session_state.get("auth_data")

            # ── Cookie not ready yet — CookieController may still be mounting ────────
            if st.session_state._auth_retry_count < len(_AUTH_RETRY_DELAYS):
                delay = _AUTH_RETRY_DELAYS[st.session_state._auth_retry_count]
                st.session_state._auth_retry_count += 1
                st.session_state._auth_retry_in_progress = True
                outcome = f"retry_{st.session_state._auth_retry_count}"
                log_timing(
                    "AUTH_REQUIRE_RETRY",
                    0,
                    f"{_auth_request_meta()} attempt={st.session_state._auth_retry_count} "
                    f"delay={delay}s page={page or '?'} redirect_to={redirect_to}",
                    level="WARNING",
                )
                log_timing(
                    "AUTH_REQUIRE_AUTH_RETRY",
                    0,
                    f"{_auth_request_meta()} attempt={st.session_state._auth_retry_count} "
                    f"delay={delay}s redirect_to={redirect_to} page={page or '?'}",
                    level="WARNING",
                )
                sleep(delay)
                st.rerun()
                st.stop()  # Safety net: execution must not continue
                return None

            # ── Max retries exhausted — no cookie after full budget ──────────────────
            # Clear retry tracking
            st.session_state._auth_retry_count = 0
            st.session_state._auth_retry_in_progress = False
            st.session_state._auth_retry_elapsed_ms = 0.0
            outcome = "redirect_exhausted"
            _redirect_target = f"pages/{redirect_to}.py"
            log_timing(
                "AUTH_REQUIRE_AUTH_EXHAUSTED",
                0,
                f"{_auth_request_meta()} all retries used page={page or '?'} "
                f"redirect_target={_redirect_target}",
                level="WARNING",
            )
            log_timing(
                "AUTH_REQUIRE_REDIRECT_LOGIN",
                0,
                f"{_auth_request_meta()} reason=retries_exhausted page={page or '?'} "
                f"redirect_target={_redirect_target} retry_count={len(_AUTH_RETRY_DELAYS)}",
                level="WARNING",
            )
            if non_blocking:
                outcome = "exhausted_allow_non_blocking"
                return None
            st.switch_page(_redirect_target)
            st.stop()  # Safety net: execution must not continue
            return None
        except Exception as e:
            log_structured_error(e, page="auth_manager", component="require_auth", operation="auth_check")
            if os.getenv("AUTH_REQUIRE_AUTH_LOG_ONLY", "0").strip() == "1" or os.getenv("AUTH_REQUIRE_AUTH_BYPASS", "0").strip() == "1":
                outcome = "exception_allow_non_blocking"
                return None
            # On error, redirect to login as safest fallback
            try:
                st.switch_page(f"pages/{redirect_to}.py")
                st.stop()
            except Exception:
                pass
            raise RuntimeError("Authentication check failed and redirect was not possible") from e
        finally:
            if _auth_verbose() or outcome not in ("allowed", "unknown"):
                log_timing(
                    "AUTH_REQUIRE_AUTH",
                    (time_module.perf_counter() - start) * 1000,
                    f"{_auth_request_meta()} outcome={outcome} redirect_to={redirect_to} page={page or '?'}",
                )

    def _get_cookie(self) -> Optional[Dict[str, Any]]:
        """Read and parse auth cookie.

        Tries st.context.cookies first (synchronous, reads from HTTP request
        headers on the initial WebSocket upgrade — no mounting delay).
        Falls back to CookieController for cookies set during this WebSocket
        session (e.g. immediately after login before a hard browser refresh).
        """
        start = time_module.perf_counter()
        source = "none"
        if st.session_state.get("_auth_invalidated"):
            return None
        try:
            # ── FAST PATH: HTTP request headers (sync, no React mount needed) ─────
            raw = st.context.cookies.get(COOKIE_NAME)
            if raw:
                raw_bytes = len(raw.encode("utf-8"))
                # Browser URL-encodes JSON cookie values (%7B → {, %22 → ", etc.)
                decoded = urllib.parse.unquote(raw) if isinstance(raw, str) else raw
                # Defensive parse: sometimes the cookie exists but is empty/garbled.
                if isinstance(decoded, str):
                    s = decoded.strip()
                    if not s:
                        raise ValueError("empty_cookie_value")
                    # Trim wrapping quotes if a proxy/component double-quoted the value
                    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
                        s = s[1:-1].strip()
                    if not s:
                        raise ValueError("empty_cookie_value_after_trim")
                    parsed = json.loads(s)
                else:
                    parsed = decoded
                if isinstance(parsed, dict):
                    _sid = parsed.get("session_id", "")
                    if _sid and _is_session_invalidated(_sid):
                        log_timing(
                            "AUTH_GET_COOKIE",
                            (time_module.perf_counter() - start) * 1000,
                            f"source=st.context.cookies REJECTED — session_id={_sid[:8]} is blacklisted (logged out)",
                            level="WARNING",
                        )
                        return None
                    st.session_state["_auth_cookie_source"] = "st.context.cookies"
                    source = "st.context.cookies"
                    if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
                        try:
                            h = st.context.headers
                            host_hdr = (h.get("host") or "").split(",", 1)[0].strip()
                            origin_hdr = (h.get("origin") or "").split(",", 1)[0].strip()
                            referer_hdr = (h.get("referer") or "").split(",", 1)[0].strip()
                            log_info(
                                "[AUTH_FLOW] cookie_seen_http | "
                                f"host={host_hdr!r} origin={origin_hdr!r} referer={referer_hdr!r} "
                                f"bytes={raw_bytes} session={str(parsed.get('session_id',''))[:8]!r}"
                            )
                        except Exception:
                            pass
                    if _auth_verbose():
                        log_timing(
                            "AUTH_GET_COOKIE",
                            (time_module.perf_counter() - start) * 1000,
                            f"{_auth_request_meta()} source=st.context.cookies bytes={raw_bytes} "
                            f"session={parsed.get('session_id', '?')[:8]}",
                            level="WARNING",
                        )
                    return parsed
                if _auth_verbose():
                    log_timing(
                        "AUTH_GET_COOKIE",
                        (time_module.perf_counter() - start) * 1000,
                        f"{_auth_request_meta()} source=st.context.cookies parse_failed=dict_expected",
                        level="WARNING",
                    )
        except Exception as e:
            if _auth_verbose():
                try:
                    _raw_len = len(st.context.cookies.get(COOKIE_NAME) or "")
                except Exception:
                    _raw_len = 0
                log_timing(
                    "AUTH_GET_COOKIE",
                    0,
                    f"{_auth_request_meta()} source=st.context.cookies "
                    f"parse_failed={type(e).__name__} raw_bytes={_raw_len}",
                    level="WARNING",
                )

        # ── FALLBACK: CookieController (same WebSocket session only) ────────────
        try:
            controller = self._get_controller()
            cookie_value = controller.get(COOKIE_NAME)
            if not cookie_value:
                if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
                    try:
                        h = st.context.headers
                        host_hdr = (h.get("host") or "").split(",", 1)[0].strip()
                        origin_hdr = (h.get("origin") or "").split(",", 1)[0].strip()
                        referer_hdr = (h.get("referer") or "").split(",", 1)[0].strip()
                        log_info(f"[AUTH_FLOW] cookie_missing | source=CookieController host={host_hdr!r} origin={origin_hdr!r} referer={referer_hdr!r}")
                    except Exception:
                        pass
                if _auth_verbose():
                    log_timing(
                        "AUTH_GET_COOKIE",
                        (time_module.perf_counter() - start) * 1000,
                        f"{_auth_request_meta()} source=CookieController present=no",
                        level="WARNING",
                    )
                return None
            if isinstance(cookie_value, str):
                s = cookie_value.strip()
                if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
                    s = s[1:-1].strip()
                parsed = json.loads(s) if s else {}
            else:
                parsed = cookie_value
            if isinstance(parsed, dict):
                _sid = parsed.get("session_id", "")
                if _sid and _is_session_invalidated(_sid):
                    log_timing(
                        "AUTH_GET_COOKIE",
                        (time_module.perf_counter() - start) * 1000,
                        f"source=CookieController REJECTED — session_id={_sid[:8]} is blacklisted (logged out)",
                        level="WARNING",
                    )
                    return None
                st.session_state["_auth_cookie_source"] = "CookieController"
                source = "CookieController"
                if _auth_verbose():
                    log_timing(
                        "AUTH_GET_COOKIE",
                        (time_module.perf_counter() - start) * 1000,
                        f"{_auth_request_meta()} source=CookieController "
                        f"session={parsed.get('session_id', '?')[:8]}",
                        level="WARNING",
                    )
                return parsed
            return None
        except Exception as e:
            if _auth_verbose():
                log_timing(
                    "AUTH_GET_COOKIE",
                    0,
                    f"{_auth_request_meta()} source=CookieController parse_failed={type(e).__name__}",
                    level="WARNING",
                )
                if os.getenv("AUTH_FLOW_VERBOSE", "0").strip() == "1":
                    _has_cached = bool(st.session_state.get("__auth_cookie_controller"))
                    log_info(
                        f"[AUTH_FLOW] CookieController exception detail | "
                        f"has_cached_controller={_has_cached} err={type(e).__name__}:{e}"
                    )
            return None

    # =========================================================================
    # USER INFO GETTERS
    # =========================================================================

    def get_current_user(self) -> Optional[str]:
        """Get current logged-in user email."""
        try:
            auth_data = st.session_state.get("auth_data")
            return auth_data.get("user_email") if auth_data else None
        except Exception as e:
            log_structured_error(e, page="auth_manager", component="get_current_user", operation="read_session_state")
            return None

    def get_session_id(self) -> Optional[str]:
        """Get current session ID."""
        try:
            auth_data = st.session_state.get("auth_data")
            return auth_data.get("session_id") if auth_data else None
        except Exception as e:
            log_structured_error(e, page="auth_manager", component="get_session_id", operation="read_session_state")
            return None

    def get_auth_data(self) -> Optional[Dict[str, Any]]:
        """Get full auth data from session."""
        try:
            return st.session_state.get("auth_data")
        except Exception as e:
            log_structured_error(e, page="auth_manager", component="get_auth_data", operation="read_session_state")
            return None

    # =========================================================================
    # LOGOUT
    # =========================================================================

    def logout(self):
        """Logout user.
        staging/local  → logout_bridge.py (destroys IdP session via OIDC logout).
        production     → local-only logout (JWT, no IdP session to destroy).
        """
        start = time_module.perf_counter()
        route = "local"
        from core.auth_environment import is_oidc_enabled

        try:
            if is_oidc_enabled():
                try:
                    self.stash_logout_context()
                    route = "logout_bridge"
                    st.switch_page("pages/logout_bridge.py")
                    return
                except Exception as e:
                    log_structured_error(e, page="auth_manager", component="logout", operation="route_logout_bridge")

            # Production path (or OIDC fallback)
            for key in ["auth_data", "authenticated"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.session_state["_auth_invalidated"] = True
        finally:
            log_timing("AUTH_LOGOUT", (time_module.perf_counter() - start) * 1000, f"route={route}")


# =========================================================================
# GLOBAL INSTANCE (Singleton Pattern)
# =========================================================================

_auth_manager: Optional[AuthManager] = None


def get_auth_manager() -> AuthManager:
    """Get or create AuthManager instance."""
    global _auth_manager
    try:
        if _auth_manager is None:
            _auth_manager = AuthManager()
        return _auth_manager
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="get_auth_manager", operation="create_singleton")
        raise


# =========================================================================
# CONVENIENCE FUNCTIONS (Module-level)
# =========================================================================

def login_user(user_email: str, user_nicename: str,
               user_display_name: str, token: str,
               id_token: str = "", wp_user_id=None) -> AuthResult:
    """Module-level login function."""
    try:
        return get_auth_manager().login(user_email, user_nicename, user_display_name, token,
                                        id_token=id_token, wp_user_id=wp_user_id)
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="login_user", operation="module_login", context=f"user={user_email}")
        return AuthResult(success=False, error_message="Login failed unexpectedly.")


def stash_logout_context(auth_data: Optional[Dict[str, Any]] = None) -> bool:
    """Module-level helper for OIDC logout handoff."""
    try:
        return get_auth_manager().stash_logout_context(auth_data)
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="stash_logout_context", operation="module_stash_logout_context")
        return False


def require_auth(redirect_to: str = "login", page: str = "") -> Optional[Dict[str, Any]]:
    """Module-level auth check for protected pages."""
    try:
        return get_auth_manager().require_auth(redirect_to, page=page)
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="require_auth", operation="module_require_auth")
        return None


def is_authenticated() -> bool:
    """Module-level auth check."""
    try:
        return get_auth_manager().is_authenticated()
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="is_authenticated", operation="module_auth_check")
        return False


def logout():
    """Module-level logout."""
    try:
        return get_auth_manager().logout()
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="logout", operation="module_logout")


def get_current_user() -> Optional[str]:
    """Module-level getter for current user."""
    try:
        return get_auth_manager().get_current_user()
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="get_current_user", operation="module_get_user")
        return None


def get_session_id() -> Optional[str]:
    """Module-level getter for session ID."""
    try:
        return get_auth_manager().get_session_id()
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="get_session_id", operation="module_get_session")
        return None


def get_auth_data() -> Optional[Dict[str, Any]]:
    """Module-level getter for full auth data dict."""
    try:
        return get_auth_manager().get_auth_data()
    except Exception as e:
        log_structured_error(e, page="auth_manager", component="get_auth_data", operation="module_get_auth_data")
        return None
