"""
Local storage utility for persisting user state across sessions.
Uses streamlit-local-storage for client-side persistence.
"""
import json
import logging
from typing import Any, Optional, Dict, Callable
from dataclasses import dataclass, asdict
from datetime import datetime, date
from enum import Enum

import streamlit as st
from utils.server_logger import log_structured_error, log_error

logger = logging.getLogger(__name__)


class StorageKey(Enum):
    """Enumeration of local storage keys for type safety."""
    SEC_FILING = "sec_filing"
    START_DATE = "start_date"
    END_DATE = "end_date"
    USER_PREFERENCES = "user_preferences"
    CHART_SETTINGS = "chart_settings"
    FILTER_STATE = "filter_state"
    # Market Data page keys
    MARKETDATA_SELECTED_SOURCE = "marketdata_selected_source"
    MARKETDATA_SELECTED_COMPANY = "marketdata_selected_company"
    MARKETDATA_SELECTED_TAB = "marketdata_selected_tab"
    MARKETDATA_DATE_START = "marketdata_date_start"
    MARKETDATA_DATE_END = "marketdata_date_end"
    # Global company selection (used across all pages)
    COMPANY_SELECTED = "company_selected"


@dataclass
class UserFilterState:
    """User's filter selections."""
    sec_filing: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        try:
            return asdict(self)
        except Exception as e:
            log_structured_error(e, page="", component="UserFilterState", operation="to_dict")
            return {}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserFilterState':
        try:
            return cls(**data)
        except Exception as e:
            log_structured_error(e, page="", component="UserFilterState", operation="from_dict")
            return cls()


class LocalStorageManager:
    """
    Global utility for managing local storage operations.
    Provides safe get/set/update operations with type validation.
    """

    def __init__(self):
        self._cache: Dict[str, Any] = {}
        self._initialized = False

    def _get_from_st_state(self, key: str) -> Optional[Any]:
        """Retrieve value from streamlit session state."""
        try:
            storage_key = f"ls_{key}"
            if storage_key in st.session_state:
                return st.session_state[storage_key]
            return None
        except Exception as e:
            log_structured_error(e, page="local_storage", component="_get_from_st_state", operation="reading_session_state")
            return None

    def _set_to_st_state(self, key: str, value: Any) -> None:
        """Store value in streamlit session state."""
        try:
            storage_key = f"ls_{key}"
            st.session_state[storage_key] = value
            self._cache[key] = value
        except Exception as e:
            log_structured_error(e, page="local_storage", component="_set_to_st_state", operation="writing_session_state")

    def get(
        self,
        key: StorageKey | str,
        default: Any = None,
        validate_type: Optional[type] = None
    ) -> Any:
        """
        Safely retrieve data from local storage.

        Args:
            key: Storage key (enum or string)
            default: Default value if key not found
            validate_type: Optional type to validate against

        Returns:
            Stored value or default
        """
        key_str = key.value if isinstance(key, StorageKey) else key

        try:
            # Check cache first
            if key_str in self._cache:
                value = self._cache[key_str]
            else:
                value = self._get_from_st_state(key_str)
                if value is None:
                    return default
                self._cache[key_str] = value

            # Type validation
            if validate_type is not None and value is not None:
                if not isinstance(value, validate_type):
                    return default

            return value

        except Exception as e:
            logger.error(f"Error retrieving local storage key {key_str}: {e}")
            return default

    def set(
        self,
        key: StorageKey | str,
        value: Any,
        serializer: Optional[Callable[[Any], str]] = None
    ) -> bool:
        """
        Safely store data in local storage.

        Args:
            key: Storage key (enum or string)
            value: Value to store
            serializer: Optional custom serializer

        Returns:
            True if successful, False otherwise
        """
        key_str = key.value if isinstance(key, StorageKey) else key

        try:
            # Serialize if needed
            if serializer is not None:
                value = serializer(value)
            elif isinstance(value, (datetime, date)):
                value = value.isoformat()
            elif isinstance(value, (dict, list)):
                value = json.dumps(value)

            self._set_to_st_state(key_str, value)
            return True

        except Exception as e:
            logger.error(f"Error storing local storage key {key_str}: {e}")
            return False

    def update(
        self,
        key: StorageKey | str,
        updater: Callable[[Any], Any],
        default: Any = None
    ) -> bool:
        """
        Update existing value using a transformer function.

        Args:
            key: Storage key
            updater: Function that receives current value and returns new value
            default: Default value if key doesn't exist

        Returns:
            True if successful, False otherwise
        """
        try:
            current = self.get(key, default)
            new_value = updater(current)
            return self.set(key, new_value)
        except Exception as e:
            log_structured_error(e, page="local_storage", component="update", operation="updating_storage_value")
            return False

    def delete(self, key: StorageKey | str) -> bool:
        """
        Remove a key from local storage.

        Args:
            key: Storage key to remove

        Returns:
            True if successful, False otherwise
        """
        key_str = key.value if isinstance(key, StorageKey) else key

        try:
            storage_key = f"ls_{key_str}"
            had_in_session = storage_key in st.session_state
            had_in_cache = key_str in self._cache

            if storage_key in st.session_state:
                del st.session_state[storage_key]
            if key_str in self._cache:
                del self._cache[key_str]

            return True
        except Exception as e:
            logger.error(f"Error deleting local storage key {key_str}: {e}")
            return False

    def clear(self) -> bool:
        """Clear all local storage data."""
        try:
            keys_to_remove = [k for k in st.session_state.keys() if k.startswith("ls_")]
            for key in keys_to_remove:
                del st.session_state[key]
            self._cache.clear()
            return True
        except Exception as e:
            logger.error(f"Error clearing local storage: {e}")
            return False

    def get_filter_state(self) -> UserFilterState:
        """Get user's filter state from local storage."""
        try:
            data = self.get(StorageKey.FILTER_STATE, {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    data = {}
            return UserFilterState.from_dict(data)
        except Exception as e:
            log_structured_error(e, page="local_storage", component="get_filter_state", operation="retrieving_filter_state")
            return UserFilterState()

    def save_filter_state(self, state: UserFilterState) -> bool:
        """Save user's filter state to local storage."""
        try:
            return self.set(StorageKey.FILTER_STATE, state.to_dict())
        except Exception as e:
            log_structured_error(e, page="local_storage", component="save_filter_state", operation="saving_filter_state")
            return False

    def sync_to_session_state(self) -> None:
        """
        Sync local storage values to streamlit session state.
        Call this at app initialization.
        """
        try:
            filter_state = self.get_filter_state()

            # Map to session state with standard keys
            st.session_state.sec_filing = filter_state.sec_filing
            st.session_state.start_date = filter_state.start_date
            st.session_state.end_date = filter_state.end_date
        except Exception as e:
            log_structured_error(e, page="local_storage", component="sync_to_session_state", operation="syncing_to_session_state")

    def sync_from_session_state(self) -> bool:
        """
        Save current session state to local storage.
        Call this when filters change.
        """
        try:
            state = UserFilterState(
                sec_filing=st.session_state.get("sec_filing"),
                start_date=st.session_state.get("start_date"),
                end_date=st.session_state.get("end_date"),
            )
            return self.save_filter_state(state)
        except Exception as e:
            log_structured_error(e, page="local_storage", component="sync_from_session_state", operation="syncing_from_session_state")
            return False


# Global local storage instance
local_storage = LocalStorageManager()


# Convenience functions for common operations
def get_sec_filing() -> Optional[str]:
    """Get selected SEC filing from storage."""
    try:
        return local_storage.get(StorageKey.SEC_FILING)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="get_sec_filing", operation="getting_sec_filing")
        return None


def set_sec_filing(filing: str) -> bool:
    """Save selected SEC filing to storage."""
    try:
        return local_storage.set(StorageKey.SEC_FILING, filing)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="set_sec_filing", operation="setting_sec_filing")
        return False


def get_date_range() -> tuple[Optional[str], Optional[str]]:
    """Get date range from storage."""
    try:
        start = local_storage.get(StorageKey.START_DATE)
        end = local_storage.get(StorageKey.END_DATE)
        return start, end
    except Exception as e:
        log_structured_error(e, page="local_storage", component="get_date_range", operation="getting_date_range")
        return None, None


def set_date_range(start: str, end: str) -> bool:
    """Save date range to storage."""
    try:
        success_start = local_storage.set(StorageKey.START_DATE, start)
        success_end = local_storage.set(StorageKey.END_DATE, end)
        return success_start and success_end
    except Exception as e:
        log_structured_error(e, page="local_storage", component="set_date_range", operation="setting_date_range")
        return False


def init_local_storage():
    """Initialize local storage on app startup."""
    try:
        local_storage.sync_to_session_state()
    except Exception as e:
        log_structured_error(e, page="local_storage", component="init_local_storage", operation="initializing_local_storage")


# Market Data page convenience functions
def get_marketdata_source() -> Optional[str]:
    """Get selected data source for market data."""
    try:
        return local_storage.get(StorageKey.MARKETDATA_SELECTED_SOURCE)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="get_marketdata_source", operation="getting_marketdata_source")
        return None


def set_marketdata_source(source: str) -> bool:
    """Save selected data source for market data."""
    try:
        return local_storage.set(StorageKey.MARKETDATA_SELECTED_SOURCE, source)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="set_marketdata_source", operation="setting_marketdata_source")
        return False


def get_marketdata_company() -> Optional[str]:
    """Get selected company ticker for market data."""
    try:
        return local_storage.get(StorageKey.MARKETDATA_SELECTED_COMPANY)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="get_marketdata_company", operation="getting_marketdata_company")
        return None


def set_marketdata_company(ticker: str) -> bool:
    """Save selected company ticker for market data."""
    try:
        return local_storage.set(StorageKey.MARKETDATA_SELECTED_COMPANY, ticker)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="set_marketdata_company", operation="setting_marketdata_company")
        return False


def get_marketdata_tab() -> Optional[str]:
    """Get selected tab for market data."""
    try:
        return local_storage.get(StorageKey.MARKETDATA_SELECTED_TAB, "company_profile")
    except Exception as e:
        log_structured_error(e, page="local_storage", component="get_marketdata_tab", operation="getting_marketdata_tab")
        return None


def set_marketdata_tab(tab: str) -> bool:
    """Save selected tab for market data."""
    try:
        return local_storage.set(StorageKey.MARKETDATA_SELECTED_TAB, tab)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="set_marketdata_tab", operation="setting_marketdata_tab")
        return False


def get_marketdata_date_range() -> tuple[Optional[str], Optional[str]]:
    """Get date range for market data."""
    try:
        start = local_storage.get(StorageKey.MARKETDATA_DATE_START)
        end = local_storage.get(StorageKey.MARKETDATA_DATE_END)
        return start, end
    except Exception as e:
        log_structured_error(e, page="local_storage", component="get_marketdata_date_range", operation="getting_marketdata_date_range")
        return None, None


def set_marketdata_date_range(start: str, end: str) -> bool:
    """Save date range for market data."""
    try:
        success_start = local_storage.set(StorageKey.MARKETDATA_DATE_START, start)
        success_end = local_storage.set(StorageKey.MARKETDATA_DATE_END, end)
        return success_start and success_end
    except Exception as e:
        log_structured_error(e, page="local_storage", component="set_marketdata_date_range", operation="setting_marketdata_date_range")
        return False


# Global company selection functions (used across all pages)
def get_selected_company() -> Optional[str]:
    """Get the globally selected company ticker (used across all pages)."""
    try:
        return local_storage.get(StorageKey.COMPANY_SELECTED)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="get_selected_company", operation="getting_selected_company")
        return None


def set_selected_company(ticker: str) -> bool:
    """Save the globally selected company ticker (used across all pages)."""
    try:
        # Also update marketdata company for backward compatibility
        set_marketdata_company(ticker)
        return local_storage.set(StorageKey.COMPANY_SELECTED, ticker)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="set_selected_company", operation="setting_selected_company")
        return False


def clear_selected_company() -> bool:
    """Clear the selected company."""
    try:
        return local_storage.delete(StorageKey.COMPANY_SELECTED)
    except Exception as e:
        log_structured_error(e, page="local_storage", component="clear_selected_company", operation="clearing_selected_company")
        return False
