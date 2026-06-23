#!/usr/bin/env python3
"""
Login Page - Coresight Research Market Data Portal
- Non-production (ENV / ENVIRONMENT / APP_ENV not ``production``) → OIDC
- Production (any of those set to ``production``) → JWT email/password (market-prod parity)
"""
import os
import json
import base64
import hashlib
import ipaddress
import requests
import traceback
import tempfile
import threading
from time import sleep, perf_counter
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union, Tuple
from urllib.parse import urlencode, urlsplit
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Environment gate — must be read BEFORE any st.* call ──────────────────────
from core.auth_environment import IS_OIDC_ENV, is_production_deploy

# ── OIDC Config (only used when IS_OIDC_ENV=True) ─────────────────────────────
IDP_BASE_URL        = "https://coresight.com"
IDP_AUTHORIZE_URL   = os.getenv("IDP_AUTHORIZE_URL")   or f"{IDP_BASE_URL}/csr-idp/authorize"
IDP_TOKEN_URL       = os.getenv("IDP_TOKEN_URL")       or f"{IDP_BASE_URL}/csr-idp/token"
IDP_USERINFO_URL    = os.getenv("IDP_USERINFO_URL")    or f"{IDP_BASE_URL}/wp-json/csr-idp/v1/userinfo"
OIDC_CLIENT_ID      = "market-data"
OIDC_CLIENT_SECRET  = "IwtYEUtc9nsi)j8g!LGliVsV!OkVn%dQuv0IZfu9hiy(ZOpr"
OIDC_REDIRECT_URI   = "https://marketdata.coresight.com" if is_production_deploy() else "https://marketdata-stg.coresight.com"
OIDC_SCOPE          = os.getenv("OIDC_SCOPE",          "openid profile email").strip()
OIDC_STATE_MAX_AGE_SECONDS = int(os.getenv("OIDC_STATE_MAX_AGE_SECONDS", "900"))
OIDC_RETURN_CONTEXT_MAX_AGE_SECONDS = int(
    os.getenv("OIDC_RETURN_CONTEXT_MAX_AGE_SECONDS", str(OIDC_STATE_MAX_AGE_SECONDS))
)
OIDC_HANDOFF_MAX_AGE_SECONDS = int(os.getenv("OIDC_HANDOFF_MAX_AGE_SECONDS", "180"))
OIDC_EXCHANGE_CACHE_RETRY_DELAYS = [
    float(value.strip())
    for value in os.getenv("OIDC_EXCHANGE_CACHE_RETRY_DELAYS", "0.15,0.25,0.35,0.5").split(",")
    if value.strip()
]
POST_LOGIN_REDIRECT_DELAY_MS = int(os.getenv("POST_LOGIN_REDIRECT_DELAY_MS", "300"))

# ── JWT Config (only used when IS_OIDC_ENV=False) ─────────────────────────────
AUTH_URL      = os.getenv("AUTH_URL", "").strip()
AUTH_URL_PAID = os.getenv("AUTH_URL_PAID", "https://stage.coresight.com/wp-json").strip()

APP_DIR = Path(__file__).parent.parent

# ── OIDC: module-level exchange lock + cache ───────────────────────────────────
if IS_OIDC_ENV:
    _oidc_exchange_lock: threading.Lock = threading.Lock()
    _oidc_exchange_cache: Dict[str, Dict[str, Any]] = {}

import streamlit as st
import streamlit.components.v1 as _st_components

from utils.server_logger import log_structured_error, log_error
try:
    from utils.server_logger import log_warning as slog_warning, log_error as slog_error, log_timing
except ImportError:
    def slog_warning(msg): pass
    def slog_error(msg): pass
    def log_timing(operation, elapsed_ms, details="", level="INFO"): pass

from core.auth_manager import (
    login_user,
    is_authenticated,
    get_current_domain,
    get_or_create_auth_flow_id,
    get_auth_flow_id,
    render_auth_cookie_handoff_redirect,
    _auth_request_meta,
)
from components.styles import hide_sidebar


# =============================================================================
# OIDC UTILITIES  (staging / local only)
# =============================================================================

def _random_urlsafe(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode().rstrip("=")

def _b64url_encode_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _b64url_decode_to_bytes(data: str) -> bytes:
    data = (data or "").strip()
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

def _encode_state_payload(payload: Dict[str, Any]) -> str:
    return _b64url_encode_bytes(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode())

def _decode_state_payload(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(_b64url_decode_to_bytes(token).decode())
        if not isinstance(payload, dict):
            return None
        ts = int(payload.get("ts", 0))
        if not ts or (int(datetime.now(timezone.utc).timestamp()) - ts) > OIDC_STATE_MAX_AGE_SECONDS:
            slog_warning("[OIDC] state payload expired")
            return None
        return payload
    except Exception as e:
        slog_warning(f"[OIDC] failed to decode state: {e}")
        return None

def _decode_jwt_payload_unverified(jwt_token: str) -> Dict[str, Any]:
    if not jwt_token or jwt_token.count(".") < 2:
        return {}
    try:
        data = json.loads(_b64url_decode_to_bytes(jwt_token.split(".")[1]).decode())
        return data if isinstance(data, dict) else {}
    except Exception as e:
        slog_warning(f"[OIDC] failed to decode id_token: {e}")
        return {}

def _get_query_params() -> Dict[str, Any]:
    try:
        def _safe(v): return v[0] if isinstance(v, list) else v
        return {k: _safe(v) for k, v in dict(st.query_params).items()}
    except Exception:
        return {}


def _ephemeral_cache_path(prefix: str, key: str) -> str:
    digest = hashlib.sha256(f"{prefix}:{key}".encode()).hexdigest()[:24]
    return os.path.join(tempfile.gettempdir(), f"_oidc_{prefix}_{digest}.json")


def _write_ephemeral_cache(prefix: str, key: str, data: Dict[str, Any]) -> None:
    try:
        path = _ephemeral_cache_path(prefix, key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
    except Exception as e:
        slog_warning(f"[OIDC] failed to write {prefix} cache: {e}")


def _read_ephemeral_cache(
    prefix: str,
    key: str,
    max_age_seconds: int,
    consume: bool = False,
) -> Optional[Dict[str, Any]]:
    path = _ephemeral_cache_path(prefix, key)
    try:
        if not os.path.exists(path):
            return None
        if (datetime.now().timestamp() - os.path.getmtime(path)) > max_age_seconds:
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        with open(path, "r") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception as e:
        slog_warning(f"[OIDC] failed to read {prefix} cache: {e}")
        return None
    finally:
        if consume:
            try:
                os.unlink(path)
            except OSError:
                pass


def _get_request_origin() -> str:
    try:
        headers = st.context.headers
        for header_name in ("origin", "referer"):
            raw = (headers.get(header_name) or "").split(",", 1)[0].strip()
            if not raw:
                continue
            parsed = urlsplit(raw)
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
        parsed = urlsplit(origin or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        hostname = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.scheme}://{hostname}:{port}"
    except Exception:
        return ""


def _is_allowed_return_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin or "")
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


def _stash_return_origin() -> Optional[str]:
    origin = _get_request_origin()
    if not origin or not _is_allowed_return_origin(origin):
        slog_warning(f"[OIDC] no valid return origin captured | origin={origin!r}")
        return None

    key = _random_urlsafe(18)
    slog_warning(f"[OIDC] captured return origin | origin={origin!r} | key={key[:8]}...")
    _write_ephemeral_cache(
        "return",
        key,
        {
            "origin": origin,
            "ts": int(datetime.now(timezone.utc).timestamp()),
        },
    )
    return key


def _consume_handoff_token_data(handoff_key: str) -> Optional[Dict[str, Any]]:
    payload = _read_ephemeral_cache(
        "handoff",
        handoff_key,
        OIDC_HANDOFF_MAX_AGE_SECONDS,
        consume=True,
    )
    if not isinstance(payload, dict):
        return None

    token_data = payload.get("token_data")
    return token_data if isinstance(token_data, dict) else None


def _maybe_redirect_oidc_handoff(token_data: Dict[str, Any], state_payload: Dict[str, Any]) -> bool:
    return_key = (state_payload or {}).get("rk")
    if not return_key:
        return False

    return_payload = _read_ephemeral_cache(
        "return",
        return_key,
        OIDC_RETURN_CONTEXT_MAX_AGE_SECONDS,
        consume=True,
    )
    return_origin = (return_payload or {}).get("origin", "")
    current_origin = _get_request_origin()
    slog_warning(
        f"[OIDC] handoff check | current={current_origin!r} | return={return_origin!r} | "
        f"return_key={str(return_key)[:8]}..."
    )
    if not return_origin or not current_origin:
        return False
    if not _is_allowed_return_origin(return_origin):
        slog_warning(f"[OIDC] ignoring unsafe return origin: {return_origin!r}")
        return False
    if _normalize_origin(return_origin) == _normalize_origin(current_origin):
        return False

    handoff_key = _random_urlsafe(18)
    _write_ephemeral_cache(
        "handoff",
        handoff_key,
        {
            "token_data": token_data,
            "ts": int(datetime.now(timezone.utc).timestamp()),
        },
    )
    handoff_url = f"{return_origin.rstrip('/')}/?{urlencode({'oidc_handoff': handoff_key})}"
    slog_warning(
        f"[OIDC] handing login back to original host | current={current_origin!r} | "
        f"return={return_origin!r}"
    )
    st.markdown(
        f'<meta http-equiv="refresh" content="0; url={handoff_url}">',
        unsafe_allow_html=True,
    )
    st.stop()
    return True


def _process_oidc_handoff() -> bool:
    handoff_key = (_get_query_params().get("oidc_handoff") or "").strip()
    if not handoff_key:
        return False

    if st.session_state.get("__completed_oidc_handoff_key") == handoff_key:
        cached = st.session_state.get("__completed_oidc_handoff_token_data")
        if isinstance(cached, dict):
            slog_warning("[OIDC] completing cached host handoff login")
            _complete_oidc_login(cached)
            return True

    if st.session_state.get("__failed_oidc_handoff_key") == handoff_key:
        return False

    token_data = _consume_handoff_token_data(handoff_key)
    if not token_data:
        st.session_state["__failed_oidc_handoff_key"] = handoff_key
        st.session_state["_oidc_login_error"] = "Login session expired. Please sign in again."
        slog_warning("[OIDC] host handoff missing or expired")
        return False

    st.session_state["__completed_oidc_handoff_key"] = handoff_key
    st.session_state["__completed_oidc_handoff_token_data"] = token_data
    slog_warning("[OIDC] completing host handoff login")
    _complete_oidc_login(token_data)
    return True


def _process_post_logout_return() -> bool:
    return_key = (_get_query_params().get("oidc_post_logout") or "").strip()
    if not return_key:
        return False
    if st.session_state.get("__completed_oidc_post_logout_key") == return_key:
        return False

    st.session_state["__completed_oidc_post_logout_key"] = return_key
    return_payload = _read_ephemeral_cache(
        "logout_return",
        return_key,
        OIDC_RETURN_CONTEXT_MAX_AGE_SECONDS,
        consume=True,
    )
    return_origin = (return_payload or {}).get("origin", "")
    current_origin = _get_request_origin()
    if not return_origin or not current_origin:
        return False
    if not _is_allowed_return_origin(return_origin):
        slog_warning(f"[OIDC] ignoring unsafe post-logout return origin: {return_origin!r}")
        return False
    if _normalize_origin(return_origin) == _normalize_origin(current_origin):
        return False

    logout_return_url = f"{return_origin.rstrip('/')}/"
    slog_warning(
        f"[OIDC] returning post-logout browser to original host | current={current_origin!r} | "
        f"return={return_origin!r}"
    )
    st.markdown(
        f'<meta http-equiv="refresh" content="0; url={logout_return_url}">',
        unsafe_allow_html=True,
    )
    st.stop()
    return True

def _exchange_cache_path(code: str) -> str:
    h = hashlib.sha256(code.encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"_oidc_xch_{h}.json")

def _write_exchange_file_cache(code: str, data: dict) -> None:
    try:
        path = _exchange_cache_path(code)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def _read_exchange_file_cache(code: str) -> Optional[Dict[str, Any]]:
    try:
        path = _exchange_cache_path(code)
        if os.path.exists(path):
            if (datetime.now().timestamp() - os.path.getmtime(path)) > 300:
                try: os.unlink(path)
                except OSError: pass
                return None
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    return None

def _build_authorize_url(force_login: bool = False) -> str:
    start = perf_counter()
    nonce = _random_urlsafe(24)
    cv    = _random_urlsafe(32)
    state_payload = {
        "nonce": nonce,
        "cv": cv,
        "ru": OIDC_REDIRECT_URI,
        "ts": int(datetime.now(timezone.utc).timestamp()),
    }
    return_key = _stash_return_origin()
    if return_key:
        state_payload["rk"] = return_key
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": OIDC_SCOPE,
        "state": _encode_state_payload(state_payload),
        "nonce": nonce,
        "code_challenge": cv,
        "code_challenge_method": "plain",
    }
    if force_login:
        params["prompt"] = "login"
    slog_warning(f"[OIDC] authorize URL | client={OIDC_CLIENT_ID} redirect={OIDC_REDIRECT_URI}")
    url = f"{IDP_AUTHORIZE_URL}?{urlencode(params)}"
    log_timing("OIDC_BUILD_AUTHORIZE_URL", (perf_counter() - start) * 1000, f"force_login={force_login}")
    return url


def _wait_for_cookie_flush(flow: str) -> None:
    if POST_LOGIN_REDIRECT_DELAY_MS <= 0:
        return
    wait_start = perf_counter()
    sleep(POST_LOGIN_REDIRECT_DELAY_MS / 1000.0)
    log_timing(
        "AUTH_POST_LOGIN_REDIRECT_WAIT",
        (perf_counter() - wait_start) * 1000,
        f"flow={flow} configured_ms={POST_LOGIN_REDIRECT_DELAY_MS}",
    )

def _exchange_code_for_tokens(code: str, state_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    start = perf_counter()
    _cache_key = "__oidc_exchange_result"

    cached = st.session_state.get(_cache_key)
    if isinstance(cached, dict) and cached.get("_for_code") == code:
        log_timing("OIDC_TOKEN_EXCHANGE", (perf_counter() - start) * 1000, "cache=session_state")
        return {k: v for k, v in cached.items() if k != "_for_code"}
    proc = _oidc_exchange_cache.get(code)
    if isinstance(proc, dict):
        log_timing("OIDC_TOKEN_EXCHANGE", (perf_counter() - start) * 1000, "cache=process")
        return {k: v for k, v in proc.items() if k != "_for_code"}
    fc = _read_exchange_file_cache(code)
    if isinstance(fc, dict):
        log_timing("OIDC_TOKEN_EXCHANGE", (perf_counter() - start) * 1000, "cache=file")
        return fc

    lock_wait_start = perf_counter()
    if not _oidc_exchange_lock.acquire(timeout=25):
        slog_error("[OIDC] exchange lock timeout")
        st.error("Login timed out. Please try again.")
        return None
    try:
        log_timing("OIDC_EXCHANGE_LOCK_WAIT", (perf_counter() - lock_wait_start) * 1000)
        # Double-check after lock
        cached = st.session_state.get(_cache_key)
        if isinstance(cached, dict) and cached.get("_for_code") == code:
            log_timing("OIDC_TOKEN_EXCHANGE", (perf_counter() - start) * 1000, "cache=session_state_after_lock")
            return {k: v for k, v in cached.items() if k != "_for_code"}
        proc = _oidc_exchange_cache.get(code)
        if isinstance(proc, dict):
            log_timing("OIDC_TOKEN_EXCHANGE", (perf_counter() - start) * 1000, "cache=process_after_lock")
            return {k: v for k, v in proc.items() if k != "_for_code"}
        fc = _read_exchange_file_cache(code)
        if isinstance(fc, dict):
            log_timing("OIDC_TOKEN_EXCHANGE", (perf_counter() - start) * 1000, "cache=file_after_lock")
            return fc

        cv = state_payload.get("cv")
        redirect_uri = state_payload.get("ru") or OIDC_REDIRECT_URI
        if not cv:
            slog_error("[OIDC] missing code_verifier")
            st.error("Missing PKCE verifier. Please try again.")
            return None

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": OIDC_CLIENT_ID,
            "code_verifier": cv,
        }
        if OIDC_CLIENT_SECRET:
            form["client_secret"] = OIDC_CLIENT_SECRET

        slog_warning(f"[OIDC] token exchange → {IDP_TOKEN_URL} | code={code[:12]}...")
        try:
            http_start = perf_counter()
            resp = requests.post(IDP_TOKEN_URL,
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"},
                                 data=form, timeout=20, allow_redirects=False)
            log_timing("OIDC_TOKEN_HTTP", (perf_counter() - http_start) * 1000, f"status={resp.status_code}")
            slog_warning(f"[OIDC] token exchange status={resp.status_code}")

            if resp.status_code != 200:
                try: payload = resp.json()
                except Exception: payload = {"raw": resp.text[:500]}
                if isinstance(payload, dict) and payload.get("error") == "invalid_grant":
                    for retry_delay in OIDC_EXCHANGE_CACHE_RETRY_DELAYS:
                        fc = _read_exchange_file_cache(code)
                        if isinstance(fc, dict): return fc
                        proc = _oidc_exchange_cache.get(code)
                        if isinstance(proc, dict): return {k: v for k, v in proc.items() if k != "_for_code"}
                        sleep(retry_delay)
                slog_error(f"[OIDC] token exchange FAILED | {payload}")
                st.error(f"Token exchange failed: {payload}")
                return None

            data = resp.json()
            if not isinstance(data, dict):
                slog_error("[OIDC] token payload not a dict")
                return None
            _write_exchange_file_cache(code, data)
            _oidc_exchange_cache[code] = {**data, "_for_code": code}
            st.session_state[_cache_key] = {**data, "_for_code": code}
            slog_warning(f"[OIDC] token exchange SUCCESS | keys={list(data.keys())}")
            log_timing("OIDC_TOKEN_EXCHANGE", (perf_counter() - start) * 1000, "result=success")
            return data
        except requests.RequestException as e:
            slog_error(f"[OIDC] request error: {e}")
            st.error(f"Token request error: {e}")
            return None
    finally:
        _oidc_exchange_lock.release()

def _fetch_userinfo(access_token: str, id_token: str = "") -> Optional[Dict[str, Any]]:
    def _try(url, headers, label):
        try:
            t0 = perf_counter()
            resp = requests.get(url, headers=headers, timeout=20)
            ms = int((perf_counter() - t0) * 1000)
            slog_warning(f"[OIDC][USERINFO][{label}] HTTP {resp.status_code} ({ms}ms)")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
        except Exception as e:
            slog_warning(f"[OIDC][USERINFO][{label}] error: {e}")
        return None

    base = {"Accept": "application/json"}
    if access_token:
        r = _try(IDP_USERINFO_URL, {**base, "Authorization": f"Bearer {access_token}"}, "ACCESS_TOKEN")
        if r: return r
    if id_token:
        r = _try(IDP_USERINFO_URL, {**base, "Authorization": f"Bearer {id_token}"}, "ID_TOKEN")
        if r: return r
    return None

def _build_token_data(token_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    start = perf_counter()
    access_token = token_payload.get("access_token", "")
    id_token     = token_payload.get("id_token", "")
    if not access_token or not id_token:
        slog_error(f"[OIDC] missing tokens | keys={list(token_payload.keys())}")
        return None

    claims  = _decode_jwt_payload_unverified(id_token)
    wp_data = claims.get("data") or {}
    wp_user = (wp_data.get("wp_user") if isinstance(wp_data, dict) else {}) or {}
    wp_user_id = wp_data.get("user_id") if isinstance(wp_data, dict) else None

    userinfo: Dict[str, Any] = {}
    user_email = (
        claims.get("email")
        or claims.get("user_email")
        or wp_user.get("email")
        or wp_user.get("username")
        or ""
    )
    user_display_name = (wp_user.get("name") or wp_user.get("display") or "")
    user_nicename = (wp_user.get("username") or "")

    if not user_email or not user_display_name or not user_nicename:
        userinfo = _fetch_userinfo(access_token, id_token) or {}
        user_email = (userinfo.get("email") or userinfo.get("user_email") or user_email)
        if not user_email and isinstance(userinfo.get("wp_user"), dict):
            user_email = userinfo["wp_user"].get("email") or userinfo["wp_user"].get("username") or ""
        user_display_name = user_display_name or userinfo.get("name") or user_email
        user_nicename = user_nicename or userinfo.get("nickname") or user_email

    slog_warning(f"[OIDC] resolved email={user_email!r} wp_user_id={wp_user_id!r}")
    if not user_email:
        slog_error(f"[OIDC] could not resolve email | claims_keys={list(claims.keys())}")
        st.error("Could not determine your email from login. Please contact support.")
        return None

    if not user_display_name:
        user_display_name = user_email
    if not user_nicename:
        user_nicename = user_email

    log_timing(
        "OIDC_BUILD_TOKEN_DATA",
        (perf_counter() - start) * 1000,
        f"used_userinfo={bool(userinfo)}",
    )

    return {
        "user_email": user_email,
        "user_display_name": user_display_name,
        "user_nicename": user_nicename,
        "token": access_token,
        "id_token": id_token,
        "wp_user_id": wp_user_id,
    }


def _make_loading_overlay(message: str) -> str:
    return (
        "<style>"
        "#csr-loading-overlay {"
        "position:fixed;inset:0;z-index:999999;background:#ffffff;"
        "display:flex;flex-direction:column;align-items:center;justify-content:center;"
        "font-family:Inter,sans-serif;}"
        "#csr-loading-overlay img{width:180px;margin-bottom:32px;}"
        ".csr-spinner{width:40px;height:40px;border:3px solid #f0f0f0;"
        "border-top-color:#D62E2F;border-radius:50%;"
        "animation:csr-spin 0.8s linear infinite;}"
        "@keyframes csr-spin{to{transform:rotate(360deg);}}"
        ".csr-loading-text{margin-top:16px;color:#888;font-size:14px;letter-spacing:0.3px;}"
        "</style>"
        '<div id="csr-loading-overlay">'
        '<img src="https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net'
        '/wp-content/uploads/2023/12/coresight-logo-1.png"'
        ' onerror="this.style.display=\'none\'" />'
        '<div class="csr-spinner"></div>'
        f'<div class="csr-loading-text">{message}</div>'
        "</div>"
    )


def _complete_oidc_login(token_data: Dict[str, Any]) -> None:
    start = perf_counter()
    _flow_id = get_or_create_auth_flow_id()
    log_timing(
        "AUTH_LOGIN_COMPLETE_START",
        0,
        f"{_auth_request_meta()} user={token_data.get('user_email', '?')}",
        level="WARNING",
    )
    user_email        = token_data.get("user_email", "")
    token             = token_data.get("token", "")
    id_token          = token_data.get("id_token", "")
    wp_user_id        = token_data.get("wp_user_id")
    user_nicename     = token_data.get("user_nicename", "")
    user_display_name = token_data.get("user_display_name", "")

    if not user_email or not user_email.lower().endswith("@coresight.com"):
        slog_error(f"[OIDC] access denied — email={user_email!r}")
        # Clear all OIDC callback state to prevent infinite loop
        for _key in [
            "__oidc_cb_code", "__oidc_cb_state", "__oidc_cb_error",
            "__oidc_cb_error_desc", "__oidc_cb_captured", "__oidc_processing_code",
            "__oidc_processing_started_at", "__completed_oidc_handoff_key",
            "__completed_oidc_handoff_token_data", "__failed_oidc_handoff_key",
            "__completed_oidc_post_logout_key"
        ]:
            st.session_state.pop(_key, None)
        st.session_state.pop("_auth_invalidated", None)
        st.error("❌ Access Denied: Only Coresight employees (@coresight.com) can access this portal. Please try again with your Coresight email.")
        st.info("Redirecting to login page in 2 seconds...")
        sleep(2)
        st.switch_page("pages/login.py")
        st.stop()

    login_start = perf_counter()
    result = login_user(user_email=user_email, user_nicename=user_nicename,
                        user_display_name=user_display_name, token=token,
                        id_token=id_token, wp_user_id=wp_user_id)
    log_timing(
        "OIDC_LOGIN_USER",
        (perf_counter() - login_start) * 1000,
        f"{_auth_request_meta()} user={user_email} success={result.success} "
        f"session_id={(result.session_id or '')[:8]}",
    )
    if result.success:
        slog_warning(
            f"[OIDC] login complete | auth_flow_id={_flow_id} | user={user_email} → cookie handoff"
        )
        st.session_state.pop("__logout_guard", None)
        st.session_state.pop("__logout_oidc_redirect_started", None)
        st.session_state.pop("_auth_invalidated", None)
        for _key in [
            "__oidc_cb_code", "__oidc_cb_state", "__oidc_cb_error",
            "__oidc_cb_error_desc", "__oidc_cb_captured", "__oidc_processing_code",
            "__oidc_processing_started_at", "__completed_oidc_handoff_key",
            "__completed_oidc_handoff_token_data", "__failed_oidc_handoff_key",
            "__completed_oidc_post_logout_key"
        ]:
            st.session_state.pop(_key, None)
        log_timing("OIDC_COMPLETE_LOGIN", (perf_counter() - start) * 1000, f"user={user_email}")
        render_auth_cookie_handoff_redirect(
            target_path="/home",
            message="Completing sign-in…",
        )
        st.stop()
    else:
        slog_error(f"[OIDC] login_user failed | {result.error_message}")
        st.error(result.error_message or "Login failed. Please try again.")


# =============================================================================
# JWT UTILITIES  (production only)
# =============================================================================

class _SimpleLogger:
    def error(self, msg, *a, **kw):
        try: log_error(msg)
        except Exception: pass

_log = _SimpleLogger()

def _exc() -> str:
    try: return "".join(traceback.format_exc(limit=2))
    except Exception: return ""

def _as_str(x) -> str:
    return "" if x is None else (x if isinstance(x, str) else str(x))

def sanitize_username(u) -> str:
    s = _as_str(u)
    return s.strip()

def sanitize_password(p) -> str:
    if p is None: return ""
    if isinstance(p, bytes): return p.decode("utf-8", errors="replace")
    return str(p)

def _has_quote(s: str) -> bool:
    return isinstance(s, str) and ("'" in s or '"' in s)

def _no_trailing_slash(url: str) -> str:
    return (url[:-1] if url.endswith("/") else url) if url else url

def _wpjson_base(api_url: str):
    if not api_url: return None
    url   = _no_trailing_slash(api_url)
    lower = url.lower()
    tok   = "/jwt-auth/v1/token"
    if lower.endswith(tok):
        base = url[:-len(tok)]
        return _no_trailing_slash(base + "/wp-json") if "/wp-json" not in base.lower() else _no_trailing_slash(base)
    if "/wp-json" in lower:
        return _no_trailing_slash(url.split("/wp-json")[0] + "/wp-json")
    return _no_trailing_slash(url + "/wp-json")

def _auth_endpoint():
    base = _wpjson_base(AUTH_URL_PAID) or _wpjson_base(AUTH_URL)
    return f"{base}/jwt-auth/v1/token" if base else None

def _build_wire_json(payload: dict) -> Tuple[str, str]:
    raw  = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    wire = raw.replace('\\"', '\\u0022').replace("'", "\\u0027")
    return raw, wire

def _escape_for_unslash(value: str) -> str:
    v = (value or "").replace("\\", "\\\\")
    return v.replace('"', '\\"').replace("'", "\\'")

def _send_json_wire(url, payload, timeout=12):
    try:
        _, wire = _build_wire_json(payload)
        resp = requests.post(url, data=wire,
                             headers={"Accept": "application/json",
                                      "Content-Type": "application/json; charset=utf-8"},
                             timeout=timeout, allow_redirects=False)
        return resp, "json-wire"
    except Exception as e:
        log_structured_error(e, page="login", component="_send_json_wire", operation="HTTP_POST")
        return None, "json-wire"

def _send_json_raw(url, payload, timeout=12):
    try:
        resp = requests.post(url, json=payload,
                             headers={"Accept": "application/json"},
                             timeout=timeout, allow_redirects=False)
        return resp, "json-raw"
    except Exception as e:
        log_structured_error(e, page="login", component="_send_json_raw", operation="HTTP_POST")
        return None, "json-raw"

def _send_form(url, payload, timeout=12):
    try:
        resp = requests.post(url, data=payload,
                             headers={"Accept": "application/json",
                                      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                             timeout=timeout, allow_redirects=False)
        return resp, "form"
    except Exception as e:
        log_structured_error(e, page="login", component="_send_form", operation="HTTP_POST")
        return None, "form"

def authenticate_user(username, password):
    token_url = _auth_endpoint()
    if not token_url:
        return None
    username = sanitize_username(_as_str(username))
    password = sanitize_password(_as_str(password))
    if not username or not password:
        st.error("Please enter both email and password.")
        return None

    payload = {"username": username, "password": password}

    def _ok(resp, mode):
        if resp is not None and resp.status_code == 200:
            try: return resp.json()
            except Exception: pass
        return None

    r = _ok(*_send_json_wire(token_url, payload))
    if r: return r
    r = _ok(*_send_json_raw(token_url, payload))
    if r: return r
    r = _ok(*_send_form(token_url, payload))
    if r: return r

    if _has_quote(password):
        p2 = {"username": username, "password": _escape_for_unslash(password)}
        for fn in (_send_json_wire, _send_json_raw, _send_form):
            r = _ok(*fn(token_url, p2))
            if r: return r
    return None


# =============================================================================
# MODULE-LEVEL: OIDC EARLY CALLBACK CAPTURE  (staging / local only)
# =============================================================================

if IS_OIDC_ENV:
    if not st.session_state.get("__oidc_cb_captured"):
        _cb_qp    = _get_query_params()
        _cb_code  = _cb_qp.get("code")
        _cb_error = _cb_qp.get("error")
        if _cb_code or _cb_error:
            _flow_id = get_or_create_auth_flow_id()
            st.session_state["__oidc_cb_code"]      = _cb_code
            st.session_state["__oidc_cb_state"]     = _cb_qp.get("state")
            st.session_state["__oidc_cb_error"]     = _cb_error
            st.session_state["__oidc_cb_error_desc"]= _cb_qp.get("error_description")
            st.session_state["__oidc_cb_captured"]  = True
            log_timing(
                "AUTH_LOGIN_CALLBACK_CAPTURED",
                0,
                f"{_auth_request_meta()} code={'YES' if _cb_code else 'NO'} error={_cb_error or 'NO'}",
                level="WARNING",
            )
            slog_warning(
                f"[OIDC] callback captured | auth_flow_id={_flow_id} | "
                f"code={'YES' if _cb_code else 'NO'} | error={_cb_error or 'NO'}"
            )

    slog_warning(f"[LOGIN] OIDC mode | IDP={IDP_AUTHORIZE_URL} | CLIENT={OIDC_CLIENT_ID} | REDIRECT={OIDC_REDIRECT_URI}")

# =============================================================================
# MODULE-LEVEL: HIDE SIDEBAR
# =============================================================================

try:
    hide_sidebar()
except Exception as e:
    log_structured_error(e, page="login", component="module_init", operation="HIDE_SIDEBAR")

# =============================================================================
# MODULE-LEVEL: OIDC HOST HANDOFF  (staging / local only)
# =============================================================================

if IS_OIDC_ENV:
    _process_post_logout_return()
    _process_oidc_handoff()

# Cookie handoff failure surfaced by main.py after login redirect
_auth_err_qp = (_get_query_params().get("auth_error") or "").strip()
if _auth_err_qp == "cookie_write_failed":
    st.session_state["_oidc_login_error"] = (
        "Sign-in cookie could not be saved. Please try again."
    )

# =============================================================================
# MODULE-LEVEL: AUTH CHECK (redirect to home if already logged in)
# =============================================================================

try:
    if IS_OIDC_ENV:
        st.session_state.pop("__logout_oidc_redirect_started", None)
        # OIDC flow: use logout guard to block auto-restore after logout
        _just_logged_out = st.session_state.pop("__just_logged_out", False)
        _logout_guard    = st.session_state.get("__logout_guard", False)
        if _just_logged_out:
            st.session_state["__logout_guard"] = True
            _logout_guard = True
            slog_warning("[LOGIN] __logout_guard set")
        if not _logout_guard and not st.session_state.get("_auth_invalidated"):
            # ── FAST RESTORE: st.context.cookies sync read with timing + overlay ──
            import urllib.parse as _urlparse
            _t_restore_start = perf_counter()
            _fast_restore_data = None
            _fast_restore_skip_reason = ""
            try:
                _fast_raw = st.context.cookies.get("auth_session")
                _t_cookie_ms = (perf_counter() - _t_restore_start) * 1000
                log_timing("FAST_RESTORE_cookie_read", _t_cookie_ms,
                           f"present={'YES' if _fast_raw else 'NO'}", level="WARNING")
                slog_warning(
                    f"[FAST_RESTORE] Cookie read | present={'YES' if _fast_raw else 'NO'} | "
                    f"elapsed={_t_cookie_ms:.1f}ms"
                )
                if _fast_raw:
                    _decoded = _urlparse.unquote(_fast_raw) if isinstance(_fast_raw, str) else _fast_raw
                    if isinstance(_decoded, str):
                        _s = _decoded.strip()
                        if len(_s) >= 2 and _s[0] == _s[-1] and _s[0] in ("'", '"'):
                            _s = _s[1:-1].strip()
                        _parsed = json.loads(_s) if _s else {}
                    else:
                        _parsed = _decoded
                    if isinstance(_parsed, dict) and _parsed.get("user_email") and _parsed.get("session_id"):
                        _fast_restore_data = _parsed
                    else:
                        _fast_restore_skip_reason = "cookie present but missing user_email or session_id"
                else:
                    _fast_restore_skip_reason = "no auth_session cookie in request headers"
            except Exception as _fe:
                _t_cookie_ms = (perf_counter() - _t_restore_start) * 1000
                _preview = ""
                try:
                    if isinstance(_fast_raw, str) and _fast_raw:
                        _preview = (_fast_raw[:40] + ("…" if len(_fast_raw) > 40 else "")).replace("\n", "\\n")
                except Exception:
                    _preview = ""
                _fast_restore_skip_reason = f"exception: {type(_fe).__name__}:{_fe} raw_preview={_preview!r}"
                log_structured_error(_fe, page="login", component="FAST_RESTORE", operation="cookie_read")

            _t_restore_ms = (perf_counter() - _t_restore_start) * 1000

            if _fast_restore_data:
                # Cookie valid — show overlay THEN redirect (no login page flash)
                st.markdown(
                    _make_loading_overlay("Loading your workspace…"),
                    unsafe_allow_html=True
                )
                st.session_state.auth_data = _fast_restore_data
                st.session_state.authenticated = True
                slog_warning(
                    f"[FAST_RESTORE] ✅ REDIRECT → home.py | "
                    f"user={_fast_restore_data.get('user_email')!r} | "
                    f"total_elapsed={_t_restore_ms:.1f}ms"
                )
                log_timing("FAST_RESTORE_total", _t_restore_ms,
                           f"result=REDIRECT user={_fast_restore_data.get('user_email')!r}", level="WARNING")
                st.switch_page("pages/home.py")
                st.stop()
            else:
                slog_warning(
                    f"[FAST_RESTORE] ⛔ no valid cookie | reason={_fast_restore_skip_reason!r} | "
                    f"total_elapsed={_t_restore_ms:.1f}ms"
                )
                log_timing("FAST_RESTORE_total", _t_restore_ms,
                           f"result=NO_COOKIE reason={_fast_restore_skip_reason!r}", level="WARNING")
                # Session_state without HTTP cookie — must complete browser handoff
                if is_authenticated():
                    slog_warning(
                        "[LOGIN] session_state auth without HTTP cookie — running cookie handoff"
                    )
                    render_auth_cookie_handoff_redirect(
                        target_path="/home",
                        message="Completing sign-in…",
                    )
                    st.stop()
    else:
        # Production flow: handle invalidation + cookie removal, then check auth
        if st.session_state.get("_auth_invalidated"):
            try:
                from streamlit_cookies_controller import CookieController as _CC
                from core.auth_manager import COOKIE_NAME
                _cc = _CC(key="_logout_cc")
                _domain = get_current_domain()
                _cc.remove(COOKIE_NAME, path="/", domain=_domain if _domain else None)
            except Exception:
                pass
        else:
            # Fast path: st.context.cookies (sync) with timing + overlay
            import urllib.parse as _urlparse
            _t_restore_start = perf_counter()
            _auth_ok = False
            _fast_data = None
            try:
                _raw = st.context.cookies.get("auth_session")
                _t_cookie_ms = (perf_counter() - _t_restore_start) * 1000
                log_timing("FAST_RESTORE_cookie_read", _t_cookie_ms,
                           f"present={'YES' if _raw else 'NO'}", level="WARNING")
                if _raw:
                    _decoded = _urlparse.unquote(_raw) if isinstance(_raw, str) else _raw
                    _data = json.loads(_decoded) if isinstance(_decoded, str) else _decoded
                    if _data.get("user_email") and _data.get("session_id"):
                        st.session_state.auth_data    = _data
                        st.session_state.authenticated = True
                        _auth_ok = True
                        _fast_data = _data
            except Exception:
                pass
            _t_restore_ms = (perf_counter() - _t_restore_start) * 1000
            if _auth_ok:
                log_timing("FAST_RESTORE_total", _t_restore_ms,
                           f"result=REDIRECT user={_fast_data.get('user_email')!r}", level="WARNING")
                st.markdown(
                    _make_loading_overlay("Loading your workspace…"),
                    unsafe_allow_html=True
                )
                st.switch_page("pages/home.py")
                st.stop()
            elif is_authenticated():
                render_auth_cookie_handoff_redirect(
                    target_path="/home",
                    message="Completing sign-in…",
                )
                st.stop()
except Exception as e:
    log_structured_error(e, page="login", component="module_init", operation="CHECK_EXISTING_AUTH")


# =============================================================================
# OIDC CALLBACK SPINNER
# When Stage3 redirects back with ?code=, show overlay immediately while
# token exchange + DB write happen (~2–5s). Without this, user sees blank page.
# =============================================================================
if IS_OIDC_ENV and st.session_state.get("__oidc_cb_captured"):
    st.markdown(
        _make_loading_overlay("Completing sign-in…"),
        unsafe_allow_html=True
    )
    slog_warning("[CALLBACK_SPINNER] Showing overlay — OIDC code exchange in progress")


# =============================================================================
# MODULE-LEVEL: OIDC CALLBACK PROCESSING  (staging / local only)
# =============================================================================

if IS_OIDC_ENV:
    _cb_code      = st.session_state.get("__oidc_cb_code")
    _cb_state     = st.session_state.get("__oidc_cb_state")
    _cb_error     = st.session_state.get("__oidc_cb_error")
    _cb_error_desc= st.session_state.get("__oidc_cb_error_desc")

    if _cb_error:
        slog_error(f"[OIDC] callback error={_cb_error}")
        st.error(f"Login failed: {_cb_error_desc or _cb_error}")
        for _k in ["__oidc_cb_code", "__oidc_cb_state", "__oidc_cb_error",
                   "__oidc_cb_error_desc", "__oidc_cb_captured"]:
            st.session_state.pop(_k, None)

    elif _cb_code:
        _completed = st.session_state.get("__completed_oidc_code")
        _processing = st.session_state.get("__oidc_processing_code")
        _cached_state_payload = _decode_state_payload(_cb_state or "") if _cb_state else None
        if _completed == _cb_code:
            slog_warning("[OIDC] duplicate callback — code already processed")
            _cached_td = st.session_state.get("__completed_oidc_token_data")
            if _cached_td:
                if _cached_state_payload:
                    _maybe_redirect_oidc_handoff(_cached_td, _cached_state_payload)
                _complete_oidc_login(_cached_td)
        elif _processing == _cb_code:
            slog_warning("[OIDC] callback already in progress")
            _cached_td = st.session_state.get("__completed_oidc_token_data")
            if _cached_td:
                if _cached_state_payload:
                    _maybe_redirect_oidc_handoff(_cached_td, _cached_state_payload)
                _complete_oidc_login(_cached_td)
            _cached_payload = st.session_state.get("__oidc_exchange_result")
            if not isinstance(_cached_payload, dict):
                _cached_payload = _oidc_exchange_cache.get(_cb_code)
            if not isinstance(_cached_payload, dict):
                _cached_payload = _read_exchange_file_cache(_cb_code)
            if isinstance(_cached_payload, dict):
                _token_data = _build_token_data(_cached_payload)
                if _token_data:
                    st.session_state["__completed_oidc_code"] = _cb_code
                    st.session_state["__completed_oidc_token_data"] = _token_data
                    if _cached_state_payload:
                        _maybe_redirect_oidc_handoff(_token_data, _cached_state_payload)
                    _complete_oidc_login(_token_data)
            _processing_started = float(st.session_state.get("__oidc_processing_started_at", 0) or 0)
            _processing_age = datetime.now(timezone.utc).timestamp() - _processing_started
            if _processing_started and _processing_age > 8:
                slog_warning("[OIDC] processing marker stale — clearing and retrying")
                st.session_state.pop("__oidc_processing_code", None)
                st.session_state.pop("__oidc_processing_started_at", None)
                st.rerun()
            sleep(0.2)
            st.rerun()
        else:
            get_or_create_auth_flow_id()
            slog_warning(
                f"[OIDC] processing callback | auth_flow_id={get_auth_flow_id()} | "
                f"code={_cb_code[:12]}..."
            )
            log_timing(
                "AUTH_LOGIN_CALLBACK_PROCESSING",
                0,
                f"{_auth_request_meta()} code_prefix={_cb_code[:12]}",
                level="WARNING",
            )
            st.session_state["__oidc_processing_code"] = _cb_code
            st.session_state["__oidc_processing_started_at"] = datetime.now(timezone.utc).timestamp()
            callback_start = perf_counter()
            try:
                _state_payload = _decode_state_payload(_cb_state or "")
                if not _state_payload:
                    slog_error("[OIDC] invalid/expired state")
                    st.error("Invalid login state. Please try again.")
                else:
                    _t_exchange_start = perf_counter()
                    _token_payload = _exchange_code_for_tokens(_cb_code, _state_payload)
                    log_timing("CALLBACK_token_exchange", (perf_counter() - _t_exchange_start) * 1000,
                               f"success={'YES' if _token_payload else 'NO'}", level="WARNING")
                    if _token_payload:
                        _t_build_start = perf_counter()
                        _token_data = _build_token_data(_token_payload)
                        log_timing("CALLBACK_build_token_data", (perf_counter() - _t_build_start) * 1000,
                                   f"success={'YES' if _token_data else 'NO'}", level="WARNING")
                        if _token_data:
                            st.session_state["__completed_oidc_code"]       = _cb_code
                            st.session_state["__completed_oidc_token_data"] = _token_data
                            if _maybe_redirect_oidc_handoff(_token_data, _state_payload):
                                log_timing(
                                    "OIDC_CALLBACK_TOTAL",
                                    (perf_counter() - callback_start) * 1000,
                                    "result=handoff",
                                )
                            log_timing("OIDC_CALLBACK_TOTAL", (perf_counter() - callback_start) * 1000, "result=success")
                            _complete_oidc_login(_token_data)
            finally:
                if st.session_state.get("__oidc_processing_code") == _cb_code and st.session_state.get("__completed_oidc_code") != _cb_code:
                    st.session_state.pop("__oidc_processing_code", None)
                    st.session_state.pop("__oidc_processing_started_at", None)


# =============================================================================
# HTML COMPONENT LOADER
# =============================================================================

def load_html_component(filename: str) -> str:
    try:
        with open(APP_DIR / "components" / filename, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


# =============================================================================
# MAIN UI
# =============================================================================

def main():
    layout_css = load_html_component("login_layout.css")
    if layout_css:
        if not layout_css.strip().startswith("<style>"):
            layout_css = f"<style>{layout_css}</style>"
        st.markdown(layout_css, unsafe_allow_html=True)

    header_html = load_html_component("login_header.html")
    if header_html:
        st.markdown(header_html, unsafe_allow_html=True)

    st.markdown('<div class="content-wrapper login-content">', unsafe_allow_html=True)

    _, center_col, _ = st.columns([2, 5, 2])

    with center_col:
        st.markdown("<div style='height: 56px;'></div>", unsafe_allow_html=True)
        st.markdown(
            "<h4 style='margin-top:0;margin-bottom:8px;font-size:24px;'>Welcome to the Coresight Research</h4>",
            unsafe_allow_html=True)
        st.markdown(
            "<h1 style='font-size:40px;margin-top:0;'>Coresight Market Data Portal</h1>",
            unsafe_allow_html=True)

        if IS_OIDC_ENV:
            # ── OIDC UI ───────────────────────────────────────────────────────
            st.markdown("""
                <style>
                [data-testid="stHeaderActionElements"] { display: none !important; }
                div.stButton { width: 100% !important; }
                div.stButton > button:first-child {
                    background-color: #d32f2f;
                    color: white;
                    width: 100% !important;
                    min-width: 100% !important;
                    max-width: 100% !important;
                    height: 3em;
                    font-size: 18px;
                    border-radius: 4px;
                    border: none;
                    display: block !important;
                }
                div.stButton > button:first-child:hover {
                    background-color: #b71c1c;
                    color: white;
                }
                .oidc-login-form {
                    margin: 0;
                    width: 100%;
                }
                .oidc-login-submit {
                    align-items: center;
                    background-color: #b71c1c;
                    border: none;
                    border-radius: 4px;
                    color: white !important;
                    cursor: pointer;
                    display: flex;
                    font-size: 18px;
                    height: 3em;
                    justify-content: center;
                    width: 100%;
                }
                .oidc-login-submit {
                    background-color: #d32f2f;
                }
                .oidc-login-submit:hover {
                    background-color: #b71c1c;
                }
                </style>""", unsafe_allow_html=True)

            st.markdown(
                """
                **Coresight Research Premium members** may use their existing login credentials
                to access a complimentary trial of the **Coresight Market Data Portal**.

                <a href="https://coresight.com/research/"
                   target="_blank"
                   style="text-decoration: none; color: #d6262f; font-weight: 600;">
                   Learn more: Coresight Market Data Portal Overview >
                </a>
                """,
                unsafe_allow_html=True)

            # Pre-build authorize URL — JS navigates directly to Stage3 (no server round trip).
            _md_force_prompt = bool(st.session_state.pop("__force_prompt_login", False))
            _md_sso_url = _build_authorize_url(force_login=_md_force_prompt)

            st.markdown(
                "<style>"
                "#csr-sso-overlay{"
                "display:none;position:fixed;inset:0;z-index:999999;background:#fff;"
                "flex-direction:column;align-items:center;justify-content:center;"
                "font-family:Inter,sans-serif;}"
                "#csr-sso-overlay img{width:180px;margin-bottom:32px;}"
                ".csr-sso-spin{width:40px;height:40px;border:3px solid #f0f0f0;"
                "border-top-color:#D62E2F;border-radius:50%;"
                "animation:csr-sso-anim 0.8s linear infinite;}"
                "@keyframes csr-sso-anim{to{transform:rotate(360deg);}}"
                ".csr-sso-text{margin-top:16px;color:#888;font-size:14px;letter-spacing:0.3px;}"
                ".oidc-login-submit{"
                "align-items:center;background-color:#d32f2f;border:none;border-radius:4px;"
                "color:white!important;cursor:pointer;display:flex;font-size:18px;height:3em;"
                "justify-content:center;width:100%;}"
                ".oidc-login-submit:hover{background-color:#b71c1c;}"
                "div[data-testid='stCustomComponentV1']{display:none!important;}"
                "</style>"
                '<div id="csr-sso-overlay">'
                '<img src="https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net'
                '/wp-content/uploads/2023/12/coresight-logo-1.png"'
                ' onerror="this.style.display=\'none\'" />'
                '<div class="csr-sso-spin"></div>'
                '<div class="csr-sso-text">Redirecting to SSO…</div>'
                "</div>"
                '<button class="oidc-login-submit" type="button" id="csr-sso-btn">Sign in with Coresight</button>',
                unsafe_allow_html=True,
            )
            _st_components.html(
                f"""<script>
(function(){{
  var url = {json.dumps(_md_sso_url)};
  var doc = window.parent.document;
  function doNav() {{
    /* Inject nav script into parent context — bypasses iframe sandbox restriction */
    var s = doc.createElement('script');
    s.textContent = 'window.location.href=' + JSON.stringify(url) + ';';
    doc.head.appendChild(s);
    s.parentNode && s.parentNode.removeChild(s);
  }}
  function attach() {{
    var btn = doc.getElementById('csr-sso-btn');
    if (!btn) {{ setTimeout(attach, 50); return; }}
    btn.addEventListener('click', function() {{
      var ov = doc.getElementById('csr-sso-overlay');
      if (ov) {{ ov.style.display = 'flex'; }}
      setTimeout(doNav, 35);
    }});
  }}
  attach();
}})();
</script>""",
                height=0,
            )

            if st.session_state.get("_oidc_login_error"):
                st.error(st.session_state.pop("_oidc_login_error"))

            st.markdown(
                """
                <div style='margin-top:20px;margin-bottom:40px'>
                    Not a Premium member? Please
                    <a href="https://coresight.com/contact/" target="_blank"
                       style="text-decoration:none; color:#d6262f;">Contact Us</a>
                    to request trial access.
                </div>
                """,
                unsafe_allow_html=True)

        else:
            # ── JWT UI (production) ───────────────────────────────────────────
            st.markdown("""
                <style>
                [data-testid="stHeaderActionElements"]{display:none!important;visibility:hidden!important}
                div.stButton{width:100%!important}
                div[data-testid="stVerticalBlock"] div[data-testid="stButton"]>button,
                div[data-testid="stVerticalBlock"] button[kind="secondary"],
                div.stButton > button:first-child{
                    background-color:#d32f2f!important;color:white!important;
                    width:100%!important;min-width:100%!important;max-width:100%!important;
                    height:44px!important;font-size:16px!important;
                    border-radius:4px!important;border:none!important;
                    font-family:'Inter',sans-serif!important;font-weight:600!important;
                    margin-top:8px!important;}
                div[data-testid="stVerticalBlock"] div[data-testid="stButton"]>button:hover{
                    background-color:#b71c1c!important;}
                div[data-testid="stTextInput"]>div>div>input{
                    border-radius:4px!important;border:1px solid #ccc!important;
                    padding:10px 12px!important;font-size:16px!important;
                    height:48px!important;font-family:'Inter',sans-serif!important;}
                div[data-testid="stTextInput"] label{
                    font-size:14px!important;color:#323232!important;
                    margin-bottom:6px!important;font-family:'Inter',sans-serif!important;
                    font-weight:500!important;}
                </style>""", unsafe_allow_html=True)

            if "is_authenticating" not in st.session_state:
                st.session_state.is_authenticating = False
            if "auth_error" not in st.session_state:
                st.session_state.auth_error = None

            username = st.text_input("Email")
            password = st.text_input("Password", type="password")

            btn_label  = "Authenticating..." if st.session_state.is_authenticating else "Log in"
            login_btn  = st.button(btn_label, key="login_button",
                                   disabled=st.session_state.is_authenticating,
                                   use_container_width=True)

            if st.session_state.auth_error:
                st.error(st.session_state.auth_error)
                st.session_state.auth_error = None

            if st.session_state.is_authenticating:
                try:
                    token_data = authenticate_user(username, password)
                    if token_data:
                        user_email = token_data.get("user_email", "")
                        token      = token_data.get("token", "")
                        if not (user_email and token):
                            st.session_state.is_authenticating = False
                            st.session_state.auth_error = "Invalid response from server."
                            st.rerun()
                        if not user_email.lower().endswith("@coresight.com"):
                            st.session_state.is_authenticating = False
                            st.session_state.auth_error = "Access restricted to Coresight employees only."
                            st.rerun()
                        result = login_user(
                            user_email=user_email,
                            user_nicename=token_data.get("user_nicename"),
                            user_display_name=token_data.get("user_display_name"),
                            token=token)
                        if result.success:
                            st.session_state.is_authenticating = False
                            render_auth_cookie_handoff_redirect(
                                target_path="/home",
                                message="Completing sign-in…",
                            )
                            st.stop()
                        else:
                            st.session_state.is_authenticating = False
                            st.session_state.auth_error = result.error_message or "Login failed."
                            st.rerun()
                    else:
                        st.session_state.is_authenticating = False
                        st.session_state.auth_error = "Incorrect username or password."
                        st.rerun()
                except Exception as e:
                    log_structured_error(e, page="login", component="main", operation="JWT_AUTH")
                    st.session_state.is_authenticating = False
                    st.session_state.auth_error = "Something went wrong. Please try again."
                    st.rerun()

            if login_btn and not st.session_state.is_authenticating:
                st.session_state.is_authenticating = True
                st.rerun()

            st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)
            st.markdown(
                """
                <div style='margin-top:20px;margin-bottom:40px'>
                    Not a Premium member? Please
                    <a href="https://coresight.com/contact/" target="_blank"
                       style="text-decoration:none; color:#d6262f;">Contact Us</a>
                    to request trial access.
                </div>
                """,
                unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)

    footer_html = load_html_component("login_footer.html")
    if footer_html:
        st.markdown(footer_html, unsafe_allow_html=True)


try:
    main()
except Exception as e:
    log_structured_error(e, page="login", component="main", operation="PAGE_RENDER")
    st.error("Something went wrong. Please try again.")
