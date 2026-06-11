"""
Configuration module for environment-based settings.
Supports local development and production deployments.

Key fix:
- If SSL is enabled but SSL_CA is missing at import time (common in Streamlit),
  we late-bind the CA path by calling core.ssl_setup.ensure_ca_cert() inside
  DatabaseConfig.connect_args.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from enum import Enum
from dotenv import load_dotenv

# Load environment variables from .env file in development
load_dotenv()

# Import error logger only
try:
    from utils.server_logger import log_structured_error, log_error, log_exception
except ImportError:
    def log_error(msg):
        pass
    def log_structured_error(exc, **kwargs):
        pass
    def log_exception(msg):
        pass




class Environment(Enum):
    """Application environments."""
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass
class DatabaseConfig:
    """Database connection configuration."""
    host: str
    port: int
    database: str
    user: str
    password: str

    # Azure MySQL Optimized Pool Settings
    # - pool_recycle: 3600s (1hr) avoids Azure SSL re-handshake (4s penalty!)
    # - pool_size: 20 base connections for 60 concurrent users
    # - max_overflow: 40 for burst traffic
    pool_size: int = 20
    max_overflow: int = 40
    pool_timeout: int = 30
    pool_recycle: int = 3600     # CRITICAL: 1hr to avoid 4s SSL reconnect

    ssl_enabled: bool = False
    ssl_ca: Optional[str] = None

    @property
    def connection_string(self) -> str:
        """
        Generate MySQL connection string for SQLAlchemy.
        NOTE: Do NOT print/log this string (contains password).
        """
        try:
            # If your DB name can contain special chars, URL-encode it. Keeping simple here.
            return f"mysql+pymysql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"
        except Exception as e:
            log_structured_error(e, page="config", component="connection_string", operation="build_connection_string")
            raise

    @property
    def connect_args(self) -> dict:
        """
        Return SSL connect_args for SQLAlchemy if SSL is enabled.
        IMPORTANT: Late-bind CA path here to avoid Streamlit import-order issues.

        If ssl_enabled=True and ssl_ca is missing, we call ensure_ca_cert() which:
        - downloads the CA if needed
        - sets os.environ["SSL_CA"]
        - returns the absolute path
        """
        try:
            if not self.ssl_enabled:
                return {}

            # 1. First check if SSL_CA was set in environment
            ca_from_env = os.getenv("SSL_CA", "").strip()
            if ca_from_env:
                self.ssl_ca = ca_from_env
                return {"ssl": {"ca": ca_from_env}}

            # 2. Check if we already have a cached value
            ca_path = (self.ssl_ca or "").strip() if isinstance(self.ssl_ca, str) else None
            if ca_path:
                return {"ssl": {"ca": ca_path}}

            # 3. Check for bundled CA certificate next to config.py's parent dirs
            #    app/core/config.py -> parent.parent = app/ -> cert at app/DigiCert...
            #    Also check one level up (repo root) as fallback.
            current_file = Path(__file__).resolve()
            app_dir = current_file.parent.parent  # app/core/ -> app/
            search_dirs = [app_dir, app_dir.parent]  # app/, then repo root

            bundled_ca = None
            for search_dir in search_dirs:
                candidate = search_dir / "DigiCertGlobalRootG2.crt.pem"
                if candidate.exists():
                    bundled_ca = candidate
                    break

            if bundled_ca is not None:
                ca_path = str(bundled_ca)
                self.ssl_ca = ca_path
                return {"ssl": {"ca": ca_path}}

            # 4. Late-bind via ensure_ca_cert() as last resort
            try:
                from core.ssl_setup import ensure_ca_cert  # local import to avoid circulars
                ca_path = ensure_ca_cert()
                self.ssl_ca = ca_path  # update cached config instance
            except Exception:
                ca_path = None

            if ca_path:
                return {"ssl": {"ca": ca_path}}

            # If still None after all attempts, FAIL LOUDLY -- do NOT silently connect without SSL
            raise RuntimeError(
                "SSL is enabled (ENABLE_SSL=true) but no CA certificate could be found or downloaded. "
                "Set SSL_CA env var, place DigiCertGlobalRootG2.crt.pem next to the app, or fix network access."
            )
        except RuntimeError:
            raise
        except Exception as e:
            log_structured_error(e, page="config", component="connect_args", operation="resolve_ssl_ca")
            raise


@dataclass
class AppConfig:
    """Application configuration."""
    env: Environment
    debug: bool
    database: DatabaseConfig
    secret_key: str
    session_timeout: int = 3600

    # Feature flags
    enable_caching: bool = True
    cache_ttl: int = 300

    # Pagination defaults
    default_page_size: int = 20
    max_page_size: int = 100

    # Navigation mode: 'new' = open in new tab, 'same' = load in same tab
    navigation_mode: str = "same"


def _as_bool(val: str, default: bool = False) -> bool:
    try:
        if val is None:
            return default
        return str(val).strip().lower() in ("1", "true", "yes", "y", "on")
    except Exception as e:
        log_structured_error(e, page="config", component="_as_bool", operation="parse_bool_value")
        return default


def load_config() -> AppConfig:
    """
    Load configuration from environment variables.
    Falls back to sensible defaults for local development.
    """
    try:
        env_str = os.getenv("APP_ENV", "local").strip().lower()
        try:
            env = Environment(env_str)
        except Exception:
            env = Environment.LOCAL

        pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
        max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))

        # SSL is only considered in staging/production in this template
        if env in (Environment.STAGING, Environment.PRODUCTION):
            # STG/Prod: SSL required by default (Azure MySQL requires secure transport)
            ssl_enabled = _as_bool(os.getenv("ENABLE_SSL", "true"), default=True)

            # If SSL enabled, read SSL_CA from env *if present*.
            # If it's missing, DatabaseConfig.connect_args will late-bind it.
            ssl_ca = os.getenv("SSL_CA") if ssl_enabled else None

            # Prefer STG_DB_* for staging, PROD_DB_* for prod; fall back to DB_*.
            if env == Environment.STAGING:
                host = os.getenv("STG_DB_HOST", os.getenv("DB_HOST", "localhost"))
                port = int(os.getenv("STG_DB_PORT", os.getenv("DB_PORT", "3306")))
                database = os.getenv("STG_DB_NAME", os.getenv("DB_NAME", "secfiling"))
                user = os.getenv("STG_DB_USER", os.getenv("DB_USER", "root"))
                password = os.getenv("STG_DB_PASSWORD", os.getenv("DB_PASSWORD", ""))
            else:  # PRODUCTION
                host = os.getenv("PROD_DB_HOST", os.getenv("DB_HOST", "localhost"))
                port = int(os.getenv("PROD_DB_PORT", os.getenv("DB_PORT", "3306")))
                database = os.getenv("PROD_DB_NAME", os.getenv("DB_NAME", "secfiling"))
                user = os.getenv("PROD_DB_USER", os.getenv("DB_USER", "root"))
                password = os.getenv("PROD_DB_PASSWORD", os.getenv("DB_PASSWORD", ""))

            db_config = DatabaseConfig(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                pool_size=pool_size,
                max_overflow=max_overflow,
                ssl_enabled=ssl_enabled,
                ssl_ca=ssl_ca,
            )
        else:
            # Local: default SSL off unless you explicitly enable it
            ssl_enabled = _as_bool(os.getenv("ENABLE_SSL", "false"), default=False)
            ssl_ca = os.getenv("SSL_CA") if ssl_enabled else None

            db_config = DatabaseConfig(
                host=os.getenv("DB_HOST", "localhost"),
                port=int(os.getenv("DB_PORT", "3306")),
                database=os.getenv("DB_NAME", "secfiling"),
                user=os.getenv("DB_USER", "root"),
                password=os.getenv("DB_PASSWORD", "admin"),
                pool_size=pool_size,
                max_overflow=max_overflow,
                ssl_enabled=ssl_enabled,
                ssl_ca=ssl_ca,
            )

        debug = _as_bool(os.getenv("DEBUG", "true"), default=True)
        if env == Environment.PRODUCTION:
            debug = _as_bool(os.getenv("DEBUG", "false"), default=False)

        secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
        if secret_key == "dev-secret-key-change-in-production" and env != Environment.LOCAL:
            log_error(f"[CONFIG] WARNING: Using default SECRET_KEY in {env.value} environment! Set SECRET_KEY env var.")

        def _safe_int(env_var: str, default: int) -> int:
            raw = os.getenv(env_var, str(default))
            try:
                return int(raw)
            except (ValueError, TypeError):
                log_error(f"[CONFIG] Invalid integer for {env_var}={raw!r}, using default {default}")
                return default

        cfg = AppConfig(
            env=env,
            debug=debug,
            database=db_config,
            secret_key=secret_key,
            session_timeout=_safe_int("SESSION_TIMEOUT", 3600),
            enable_caching=_as_bool(os.getenv("ENABLE_CACHING", "true"), default=True),
            cache_ttl=_safe_int("CACHE_TTL", 300),
            default_page_size=_safe_int("DEFAULT_PAGE_SIZE", 20),
            max_page_size=_safe_int("MAX_PAGE_SIZE", 100),
            navigation_mode=os.getenv("NAVIGATION_MODE", "same").strip().lower(),
        )

        return cfg
    except Exception as e:
        log_structured_error(e, page="config", component="load_config", operation="load_app_configuration")
        raise


# Global configuration instance (import-safe)
config = load_config()
