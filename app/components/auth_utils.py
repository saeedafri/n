import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from time import sleep

import streamlit as st
from dotenv import load_dotenv
from streamlit_cookies_controller import CookieController
from core.database import init_database
import pandas as pd

# -------------------------------------------------------------------------
# Setup
# -------------------------------------------------------------------------
load_dotenv()
logger = logging.getLogger(__name__)

try:
    from utils.server_logger import log_structured_error, log_error
except ImportError:
    def log_structured_error(exc, **kw): pass  # type: ignore
    def log_error(msg, **kw): pass  # type: ignore

# -------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------
COOKIE_EXPIRY_DAYS = 30
MAX_COOKIE_RETRIES = 2  # Increased retries
RETRY_DELAY = 0.3       # Reduced delay

# -------------------------------------------------------------------------
# Cookie Controller Helpers
# -------------------------------------------------------------------------
def get_current_domain(default: Optional[str] = None) -> str:
    """Best-effort current host → cookie domain (normalized)."""
    try:
        host = st.context.headers.get("host", "")

        if not host:
            try:
                from streamlit.web.server.websocket_headers import _get_websocket_headers
                ws = _get_websocket_headers() or {}
                host = ws.get("host") or ws.get("Host") or ""
            except ImportError:
                pass

        # Extract only the domain part (ignore port if present)
        domain = host.split(":", 1)[0].strip().lower()

        # --- Normalize allowed domains ---
        if domain == "localhost":
            return "localhost"
        elif domain == "0.0.0.0":
            return "0.0.0.0"
        elif domain.endswith(".coresight.com"):
            return ".coresight.com"

        # Fallback
        return default or ""
    except Exception:
        return default or ""

def get_cookie_controller() -> CookieController:
    """Get cookie controller instance."""
    return CookieController(key="auth_cookies")

def _read_auth_cookie() -> Dict[str, Any]:
    """Read and parse auth cookie from cookie controller - FIXED VERSION."""
    try:
        controller = get_cookie_controller()

        # Look for "auth_data" cookie
        raw_data = controller.get("auth_data")

        if not raw_data:
            return {}

        # Parse the cookie value
        try:
            if isinstance(raw_data, str):
                data = json.loads(raw_data)
            else:
                data = raw_data  # if already dict

            # Validate required fields
            required_fields = ["user_email", "token", "session_id"]
            if all(data.get(field) for field in required_fields):
                return data
            else:
                return {}

        except json.JSONDecodeError as e:
            log_structured_error(e, page="auth_utils", component="_read_auth_cookie", operation="PARSE_JSON")
            return {}
    except Exception as e:
        log_structured_error(e, page="auth_utils", component="_read_auth_cookie", operation="READ_COOKIE")

    return {}



def _write_auth_cookie(data: Dict[str, Any]) -> bool:
    """Write auth cookie and return success status."""
    try:
        if not data or not data.get("session_id"):
            log_error("Cannot write cookie: missing session_id")
            return False

        payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        sleep(2)
        controller = get_cookie_controller()

        expires_dt = datetime.now(timezone.utc) + timedelta(days=COOKIE_EXPIRY_DAYS)
        domain = get_current_domain()

        session_id = data.get("session_id")
        cookie_key = "auth_data"

        # Set cookie with appropriate domain
        cookie_params = {
            'expires': expires_dt,
            'path': '/',
            'domain': domain,
        }


        controller.set(cookie_key, payload, **cookie_params)

        # Store auth data in session state for immediate access
        st.session_state.auth_data = {
            "user_email": data.get("user_email"),
            "token": data.get("token"),
            "session_id": session_id,
            "membership_id": data.get("membership_id"),
            "membership_type": data.get("membership_type"),
            "cookie_set_at": datetime.now(timezone.utc).isoformat()
        }

        # Mark session as restored immediately
        st.session_state.session_restored = True

        return True

    except Exception as e:
        log_structured_error(e, page="auth_utils", component="_write_auth_cookie", operation="WRITE_COOKIE")
        return False

# -------------------------------------------------------------------------
# Session State Management - FIXED FOR PAGE REFRESH
# -------------------------------------------------------------------------
def _initialize_session_state():
    """Initialize session state with required keys."""
    if "session_initialized" not in st.session_state:
        st.session_state.session_initialized = True
        st.session_state.cookie_retry_count = 0
        st.session_state.session_restored = False

def restore_session_from_cookie() -> bool:
    """Restore session from cookie - FIXED for page refresh."""
    _initialize_session_state()

    # If already restored in this session, return true
    if st.session_state.get("session_restored") and st.session_state.get("auth_data"):
        return True

    # Try to read from cookie
    cookie_data = _read_auth_cookie()
    if not cookie_data:
        st.session_state.session_restored = True
        return False

    try:
        user_email = cookie_data.get("user_email")
        token = cookie_data.get("token")
        session_id = cookie_data.get("session_id")

        if user_email and token and session_id:
            # Store in session state
            st.session_state.auth_data = {
                "user_email": user_email,
                "token": token,
                "session_id": session_id,
                "membership_id": cookie_data.get("membership_id"),
                "membership_type": cookie_data.get("membership_type"),
                "restored_from_cookie": True,
                "restored_at": datetime.now(timezone.utc).isoformat()
            }
            st.session_state.session_restored = True
            return True
        else:
            pass

    except Exception as e:
        log_structured_error(e, page="auth_utils", component="restore_session_from_cookie", operation="RESTORE_SESSION")

    st.session_state.session_restored = True
    return False


def _check_cookie_with_retry(max_retries: int = MAX_COOKIE_RETRIES, retry_delay: float = RETRY_DELAY) -> bool:
    """Check for auth cookie with retry logic - FIXED."""
    _initialize_session_state()

    # First immediate check
    if restore_session_from_cookie():
        st.session_state.cookie_retry_count = 0
        return True

    # Retry logic with progress indicator
    if st.session_state.cookie_retry_count < max_retries:
        st.session_state.cookie_retry_count += 1

        # Rerun to try again
        st.rerun()
    else:
        # Max retries reached
        st.session_state.cookie_retry_count = 0
        return False

# -------------------------------------------------------------------------
# Authentication State Helpers
# -------------------------------------------------------------------------
def get_current_user() -> Optional[str]:
    """Get current logged in user email."""
    auth_data = st.session_state.get("auth_data", {})
    return auth_data.get("user_email")


def get_current_session_id() -> Optional[str]:
    """Get current session ID."""
    auth_data = st.session_state.get("auth_data", {})
    return auth_data.get("session_id")


def get_current_token() -> Optional[str]:
    """Get current authentication token."""
    auth_data = st.session_state.get("auth_data", {})
    return auth_data.get("token")


def get_user_membership() -> Tuple[Optional[str], Optional[str]]:
    """Get user membership ID and type."""
    auth_data = st.session_state.get("auth_data", {})
    return auth_data.get("membership_id"), auth_data.get("membership_type")


def is_authenticated() -> bool:
    """Check if user is authenticated - FIXED for immediate response."""
    auth_data = st.session_state.get("auth_data", {})

    # Quick check for required fields
    has_auth = bool(
        auth_data.get("user_email") and
        auth_data.get("token") and
        auth_data.get("session_id")
    )

    return has_auth

# -------------------------------------------------------------------------
# Authentication Flow Functions - FIXED FOR PAGE REFRESH
# -------------------------------------------------------------------------
def require_auth(redirect_to: str = "login.py") -> str:
    """
    Require authentication for protected pages - FIXED VERSION.
    Handles page refresh properly.
    """
    _initialize_session_state()

    # First, check if we're already authenticated in session state
    # Skip delay entirely when session is already established (hot path)
    if is_authenticated():
        session_id = get_current_session_id()
        return session_id or ""

    # Only delay when we need to read cookies (cold path — first load / refresh)
    sleep(1)

    # If not authenticated, try to restore from cookie with retry
    if not _check_cookie_with_retry():
        # Clear any partial auth data
        if "auth_data" in st.session_state:
            del st.session_state.auth_data

        st.switch_page(redirect_to)
        return ""

    # If we get here, cookie restoration was successful
    session_id = get_current_session_id() or ""

    return session_id


def logout() -> None:
    """Logout user - complete cleanup of session and cookies."""
    # Clear cookies first
    _clear_all_auth_cookies()

    # Clear session state
    _clean_session_state()

    sleep(2)
    st.switch_page("login.py")


def _clear_all_auth_cookies():
    """Clear all auth-related cookies."""
    try:
        controller = get_cookie_controller()
        cookie_key = "auth_data"


        # Get cookie
        cbb = controller.get(cookie_key)
        if cbb is None:
            st.rerun()

        past = datetime.now(timezone.utc) - timedelta(days=1)
        domain = get_current_domain()

        cookie_params = {
            "expires": past,
            "path": "/",
            'domain': domain,
        }

        # Expire cookie
        controller.set(cookie_key, "", **cookie_params)
    except Exception as e:
        log_structured_error(e, page="auth_utils", component="_clear_all_auth_cookies", operation="CLEAR_COOKIES")


def _clean_session_state() -> None:
    """Clean session state while preserving essential items."""
    essential_keys = ["cookie_controller", "session_initialized"]

    new_session_state = {}
    for key in essential_keys:
        if key in st.session_state:
            new_session_state[key] = st.session_state[key]

    # Clear all session state
    st.session_state.clear()

    # Restore essentials
    for key, value in new_session_state.items():
        st.session_state[key] = value

    # Reset flags
    st.session_state.session_restored = False
    st.session_state.cookie_retry_count = 0


# -------------------------------------------------------------------------
# Database Operations
# -------------------------------------------------------------------------
def create_db_connection():
    """Create and return database connection."""
    try:
        return mysql.connector.connect(
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", 3306)),
            ssl_ca=os.getenv("SSL_CA"),
        )
    except Exception as err:
        log_structured_error(err, page="auth_utils", component="create_db_connection", operation="DB_CONNECT")
        return None


def login_user(
    user_email: str,
    user_nicename: Optional[str] = None,
    user_display_name: Optional[str] = None,
    token: str = "",
    membership_id: Optional[str] = None,
    membership_type: Optional[str] = None,
) -> str:
    """
    Login user and set up session - FIXED for immediate session state.
    """
    # Validate required fields
    if not user_email or not token:
        log_error("Login failed: missing user_email or token")
        return ""

    # Clear any existing session first
    if is_authenticated():
        logout()

    # Generate unique session ID
    session_id = int(datetime.now(timezone.utc).timestamp())

    # Store in database (non-blocking)
    try:
        conn = init_database()
        if conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_login_sessions
                    (user_email, user_nicename, user_display_name, token, session_id, login_at, last_activity, membership_id, membership_type)
                VALUES (%s, %s, %s, %s, %s, NOW(), NOW(), %s, %s)
                ON DUPLICATE KEY UPDATE
                    token = VALUES(token),
                    session_id = VALUES(session_id),
                    login_at = NOW(),
                    last_activity = NOW(),
                    membership_id = VALUES(membership_id),
                    membership_type = VALUES(membership_type)
                """,
                (user_email, user_nicename, user_display_name, token, session_id, membership_id, membership_type),
            )
            conn.commit()
            conn.close()
    except Exception as err:
        log_structured_error(err, page="auth_utils", component="login_user", operation="DB_INSERT")
        # Continue even if DB fails - we still want to set the cookie

    # Prepare cookie data
    cookie_data = {
        "user_email": user_email,
        "token": token,
        "session_id": session_id,
        "membership_id": membership_id,
        "membership_type": membership_type,
    }

    # Write cookie AND set session state immediately
    if not _write_auth_cookie(cookie_data):
        log_error("Failed to write auth cookie during login")
        return ""

    # Session state is already set in _write_auth_cookie, just mark as restored
    st.session_state.session_restored = True

    return session_id

# -------------------------------------------------------------------------
# Quick Auth Check for Immediate Use
# -------------------------------------------------------------------------
def quick_auth_check() -> bool:
    """
    Quick authentication check that doesn't trigger redirects.
    Useful for components that need to know auth status without interrupting flow.
    """
    _initialize_session_state()

    # First check session state
    if is_authenticated():
        return True

    # Quick cookie check without retries
    try:
        cookie_data = _read_auth_cookie()
        if cookie_data and cookie_data.get("user_email") and cookie_data.get("token"):
            # Restore immediately without retry logic
            st.session_state.auth_data = {
                "user_email": cookie_data.get("user_email"),
                "token": cookie_data.get("token"),
                "session_id": cookie_data.get("session_id"),
                "membership_id": cookie_data.get("membership_id"),
                "membership_type": cookie_data.get("membership_type"),
                "quick_restored": True
            }
            st.session_state.session_restored = True
            return True
    except Exception as e:
        log_structured_error(e, page="auth_utils", component="quick_auth_check", operation="READ_COOKIE")

    return False

def make_json_safe(val):
    if isinstance(val, pd.Timestamp):
        return val.isoformat()
    if isinstance(val, list):
        return [make_json_safe(x) for x in val]
    if isinstance(val, dict):
        return {k: make_json_safe(v) for k, v in val.items()}
    return val

# -------------------------------------------------------------------------
# Debug Utilities
# -------------------------------------------------------------------------
def debug_auth_state() -> None:
    """Display debug information about authentication state."""
    if st.sidebar.checkbox("🔧 Show Auth Debug", False):
        st.sidebar.subheader("Authentication Debug")

        st.sidebar.write("**Status:**")
        st.sidebar.write(f"- Authenticated: `{is_authenticated()}`")
        st.sidebar.write(f"- Current User: `{get_current_user()}`")
        st.sidebar.write(f"- Session ID: `{get_current_session_id()}`")
        st.sidebar.write(f"- Session Restored: `{st.session_state.get('session_restored', False)}`")
        st.sidebar.write(f"- Retry Count: `{st.session_state.get('cookie_retry_count', 0)}`")

        auth_data = st.session_state.get("auth_data", {})
        st.sidebar.write("**Auth Data:**")
        st.sidebar.json(auth_data)

        # Cookie info
        try:
            cookie_data = _read_auth_cookie()
            st.sidebar.write("**Cookie Data:**")
            st.sidebar.json(cookie_data)
        except Exception as e:
            st.sidebar.write(f"Cookie error: {e}")

        if st.sidebar.button("Force Logout (Debug)"):
            logout()

        st.sidebar.markdown("---")
