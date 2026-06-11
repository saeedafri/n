"""
Local Storage Manager - Syncs session state with browser local storage
This module provides utilities to persist session state across page refreshes using streamlit-local-storage
"""
import streamlit as st
import json
import logging
import streamlit.components.v1 as components

try:
    from streamlit_local_storage import LocalStorage
except ImportError:
    LocalStorage = None

# Import server logger for error logging only
try:
    from utils.server_logger import log_error, log_exception, log_structured_error
    SERVER_LOGGER_AVAILABLE = True
except ImportError:
    SERVER_LOGGER_AVAILABLE = False
    log_error = logging.getLogger(__name__).error               # type: ignore[misc]
    def log_exception(msg): pass                                # type: ignore[misc]
    def log_structured_error(exc, **kwargs): pass               # type: ignore[misc]

logger = logging.getLogger(__name__)

class LocalStorageManager:
    """Manages syncing between Streamlit session state and browser local storage"""

    STORAGE_KEY = "sip_app_state"  # Single key for entire app

    def __init__(self, page_name=None):
        """Initialize local storage manager

        Args:
            page_name: Page name to identify which page data to manage (e.g., 'market_data', 'home')
        """
        try:
            if LocalStorage is None:
                self.local_storage = None
                self.page_name = page_name
                logger.warning("streamlit-local-storage not installed; browser persistence disabled")
                return
            # Initialize with a unique key to avoid conflicts
            self.local_storage = LocalStorage(key=f"sip_local_storage_{page_name or 'default'}")
            self.page_name = page_name
        except Exception as e:
            log_structured_error(
                e,
                page="local_storage_manager",
                component="LocalStorageManager.__init__",
                operation="init_storage",
                context=f"page_name={page_name}",
            )
            self.local_storage = None
            self.page_name = page_name

    # Session-state key for caching the full all-pages blob (avoids blocking getItem on saves)
    _SS_CACHE_KEY = "_ls_all_pages_raw_cache"

    def load_from_local_storage(self):
        """Load state from local storage and sync to session state for this page"""

        try:
            if self.local_storage is None:
                return False
            # Get stored data from local storage using getItem
            stored_data = self.local_storage.getItem(self.STORAGE_KEY)

            if stored_data and stored_data != "null" and stored_data != "":
                # Parse JSON data if it's a string
                if isinstance(stored_data, str):
                    try:
                        all_pages_state = json.loads(stored_data)
                    except json.JSONDecodeError:
                        pass
                        return False
                else:
                    all_pages_state = stored_data

                # Cache raw blob so save_to_local_storage can avoid a second getItem()
                if isinstance(all_pages_state, dict):
                    st.session_state[self._SS_CACHE_KEY] = all_pages_state

                # Check if this page has data
                if isinstance(all_pages_state, dict) and self.page_name in all_pages_state:
                    page_data = all_pages_state[self.page_name]

                    # page_data structure: { "base": {...}, "compare_retailers": {...}, "compare_sectors": {...}, "selected_tab_<page>": "..." }
                    if isinstance(page_data, dict):
                        loaded_count = 0
                        for key, value in page_data.items():
                            if key.startswith('selected_tab_'):
                                # Load tab selection
                                st.session_state[key] = value
                                loaded_count += 1
                            elif key.startswith('selected_ticker_'):
                                # Load ticker selection
                                st.session_state[key] = value
                                loaded_count += 1
                            elif key.startswith('date_range_'):
                                # Skip date_range keys — dates are derived from query params and defaults.
                                # Loading stale dates from localStorage corrupts the date widget state
                                # on page refresh (causes a date_sync mismatch loop).
                                pass
                            elif key.startswith('sort_order_'):
                                # Load sort order (handle both tab-specific and shared)
                                st.session_state[key] = value
                                loaded_count += 1

                            elif key == 'conversion_mode':
                                # Load conversion mode
                                st.session_state['conversion_mode'] = value
                                loaded_count += 1

                            elif key == 'units':
                                # Load units
                                st.session_state['units'] = value
                                loaded_count += 1

                            elif key in ('conversion_mode', 'units'):
                                # Load conversion mode and units preferences
                                st.session_state[key] = value
                                loaded_count += 1
                            elif key == 'target_currency':
                                # Do NOT load target_currency from localStorage — it is
                                # per-company and managed via the `to_curr` URL query param.
                                # Loading from localStorage causes stale values from a
                                # previous company/session to override the correct default
                                # on every Render 2 (React localStorage component mount).
                                pass
                            elif isinstance(value, dict):
                                # Load tab filters (tab_name is the key like "base", "compare_retailers")
                                tab_name = key
                                session_key = f"filters_{self.page_name}_{tab_name}"

                                # Deserialize all filter values
                                deserialized_filters = {k: self._deserialize_value(v) for k, v in value.items()}

                                # Store in session state
                                st.session_state[session_key] = deserialized_filters
                                loaded_count += len(deserialized_filters)

                        # Log what was loaded
                        date_range_keys = [k for k in page_data.keys() if 'date_range' in k]
                        sort_order_keys = [k for k in page_data.keys() if 'sort_order' in k]
                        ticker_key = f"selected_ticker_{self.page_name}"
                        ticker_value = page_data.get(ticker_key, 'N/A')
                        conversion_mode = page_data.get('conversion_mode', 'N/A')
                        units = page_data.get('units', 'N/A')

                        return True

                    return False

                return False

            return False
        except Exception as e:
            log_error(f"[LOCALSTORAGE_LOAD] Failed to load for page={self.page_name}: {e}")
            return False

    def save_to_local_storage(self, page_name=None):
        """Save all filter data for this page to local storage in structured format

        Args:
            page_name: Override page name (optional, uses self.page_name if not provided)
        """
        page = page_name or self.page_name


        try:
            # Use session-state cache to avoid a blocking getItem() round-trip.
            # The cache is populated by load_from_local_storage() on Render 2
            # (when the React localStorage component mounts and sends data).
            # Falls back to empty dict on first render (safe — setItem will create the key).
            all_pages_state = dict(st.session_state.get(self._SS_CACHE_KEY) or {})

            if not isinstance(all_pages_state, dict):
                all_pages_state = {}

            # Collect all filter data for this page from session state
            # Structure: { "base": {...}, "compare_retailers": {...}, "compare_sectors": {...}, "selected_tab_<page>": "..." }
            page_data = {}

            # Look for tab selection state
            tab_selection_key = f"selected_tab_{page}"
            if tab_selection_key in st.session_state:
                page_data[tab_selection_key] = st.session_state[tab_selection_key]

            # Look for ticker selection state
            ticker_selection_key = f"selected_ticker_{page}"
            if ticker_selection_key in st.session_state:
                page_data[ticker_selection_key] = st.session_state[ticker_selection_key]

            # date_range_* keys are intentionally NOT saved to localStorage.
            # Dates are derived from query params and defaults on each page load.
            # Saving stale dates caused a mismatch loop on page refresh.

            # Look for all tab-specific sort orders
            for key in st.session_state.keys():  # type: ignore
                if key.startswith(f'sort_order_{page}_'):  # type: ignore  # e.g., sort_order_market_data_income_statement
                    page_data[key] = st.session_state[key]

            # Look for shared sort order (across all tabs)
            shared_sort_key = f"sort_order_{page}_shared"
            if shared_sort_key in st.session_state:
                page_data[shared_sort_key] = st.session_state[shared_sort_key]

            # date_range keys (general + shared) intentionally NOT saved — see load_from_local_storage.

            # Look for general sort order state (legacy support)
            sort_order_key = f"sort_order_{page}"
            if sort_order_key in st.session_state:
                page_data[sort_order_key] = st.session_state[sort_order_key]

            # Save conversion mode and units preferences
            # NOTE: target_currency is intentionally NOT saved to localStorage — it is
            # per-company and persisted via the `to_curr` URL query param instead.
            if 'conversion_mode' in st.session_state:
                page_data['conversion_mode'] = st.session_state['conversion_mode']
            if 'units' in st.session_state:
                page_data['units'] = st.session_state['units']

            # Look for all filters_<page>_<tab> keys in session state
            filter_prefix = f"filters_{page}_"

            for key in st.session_state.keys():  # type: ignore
                if key.startswith(filter_prefix):  # type: ignore
                    # Extract tab name from key (e.g., "filters_market_data_income_statement" -> "income_statement")
                    tab_name = key[len(filter_prefix):]  # type: ignore

                    # Get the filter data for this tab
                    tab_filters = st.session_state[key]

                    if isinstance(tab_filters, dict) and self._is_serializable(tab_filters):
                        # Serialize each filter value
                        serialized_filters = {k: self._serialize_value(v) for k, v in tab_filters.items()}
                        page_data[tab_name] = serialized_filters

            if page_data:
                # Update the page data in the overall structure
                all_pages_state[page] = page_data

                # Convert to JSON and save
                json_data = json.dumps(all_pages_state)

                # Keep session-state cache in sync so subsequent saves in same run are consistent
                st.session_state[self._SS_CACHE_KEY] = all_pages_state

                if self.local_storage is not None:
                    self.local_storage.setItem(self.STORAGE_KEY, json_data)

                # Also save using JavaScript as fallback
                save_to_local_storage_js(self.STORAGE_KEY, all_pages_state)
                return True

        except Exception as e:
            log_error(f"[LOCALSTORAGE_SAVE] Failed to save for page={page}: {e}")
            return False

    def clear_local_storage(self, page_name=None, tab_name=None):
        """Clear local storage for specific page/tab or entire storage

        Args:
            page_name: If provided, only clear this page's data
            tab_name: If provided (with page_name), only clear this specific tab's data
        """
        try:
            if self.local_storage is None:
                return False
            if page_name:
                all_pages_state = dict(st.session_state.get(self._SS_CACHE_KEY) or {})
                if isinstance(all_pages_state, dict) and page_name in all_pages_state:
                    if tab_name:
                        if tab_name in all_pages_state[page_name]:
                            del all_pages_state[page_name][tab_name]
                    else:
                        del all_pages_state[page_name]
                    json_data = json.dumps(all_pages_state)
                    self.local_storage.setItem(self.STORAGE_KEY, json_data)
                    st.session_state[self._SS_CACHE_KEY] = all_pages_state
                    return True
            else:
                # Clear entire storage
                self.local_storage.removeItem(self.STORAGE_KEY)  # type: ignore
                return True
        except Exception:
            return False

    def delete_keys(self, page_name, keys_to_delete):
        """Delete specific keys from browser localStorage for a page

        Args:
            page_name: Page name to delete keys from
            keys_to_delete: List of keys to delete

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.local_storage is None:
                return False
            all_pages_state = dict(st.session_state.get(self._SS_CACHE_KEY) or {})
            if isinstance(all_pages_state, dict) and page_name in all_pages_state:
                page_data = all_pages_state[page_name]
                deleted_count = 0
                for key in keys_to_delete:
                    if key in page_data:
                        del page_data[key]
                        deleted_count += 1

                if deleted_count > 0:
                    json_data = json.dumps(all_pages_state)
                    self.local_storage.setItem(self.STORAGE_KEY, json_data)
                    st.session_state[self._SS_CACHE_KEY] = all_pages_state

                return True
            return False
        except Exception as e:
            log_error(f"[LOCALSTORAGE_DELETE] Failed to delete keys: {e}")
            return False

    def _serialize_value(self, value):
        """Serialize a value for storage"""
        try:
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            elif isinstance(value, (list, dict)):
                return value  # JSON serializable
            else:
                return str(value)  # Convert to string as fallback
        except Exception as e:
            log_structured_error(
                e,
                page="local_storage_manager",
                component="LocalStorageManager._serialize_value",
                operation="serialize",
            )
            return None

    def _deserialize_value(self, value):
        """Deserialize a value from storage"""
        try:
            return value  # For now, return as-is. Add custom deserialization if needed.
        except Exception as e:
            log_structured_error(
                e,
                page="local_storage_manager",
                component="LocalStorageManager._deserialize_value",
                operation="deserialize",
            )
            return None

    def _is_serializable(self, obj):
        """Check if an object is JSON serializable"""
        try:
            json.dumps(obj)
            return True
        except (TypeError, ValueError):
            return False


def save_to_local_storage_js(key, data):
    """Fallback JavaScript function to save data to localStorage"""
    try:
        js_code = f"""
        <script>
        try {{
            localStorage.setItem('{key}', JSON.stringify({json.dumps(data)}));
        }} catch (e) {{
            console.error('Failed to save to localStorage:', e);
        }}
        </script>
        """
        components.html(js_code, height=0, width=0)
    except Exception as e:
        log_structured_error(
            e,
            page="local_storage_manager",
            component="save_to_local_storage_js",
            operation="js_fallback_save",
            context=f"key={key}",
        )


# Convenience functions for Market Data page
def sync_market_data_state():
    """Sync market data page state between session and local storage"""
    try:
        manager = LocalStorageManager("market_data")

        # Load from local storage on page load
        manager.load_from_local_storage()

        # Set up automatic saving when state changes
        # This should be called after any state change in the UI
        return manager
    except Exception as e:
        log_structured_error(
            e,
            page="local_storage_manager",
            component="sync_market_data_state",
            operation="sync_state",
        )
        return None


def save_market_data_state():
    """Save current market data state to local storage"""
    try:
        manager = LocalStorageManager("market_data")
        return manager.save_to_local_storage()
    except Exception as e:
        log_structured_error(
            e,
            page="local_storage_manager",
            component="save_market_data_state",
            operation="save_state",
        )
        return False


def get_persistent_state(key, default=None):
    """Get a value from session state, with fallback to local storage"""
    try:
        if key in st.session_state:
            return st.session_state[key]

        # Try to load from local storage
        manager = LocalStorageManager("market_data")
        manager.load_from_local_storage()

        return st.session_state.get(key, default)
    except Exception as e:
        log_structured_error(
            e,
            page="local_storage_manager",
            component="get_persistent_state",
            operation="get_state",
            context=f"key={key}",
        )
        return default


def set_persistent_state(key, value):
    """Set a value in session state and save to local storage"""
    try:
        st.session_state[key] = value
        save_market_data_state()
    except Exception as e:
        log_structured_error(
            e,
            page="local_storage_manager",
            component="set_persistent_state",
            operation="set_state",
            context=f"key={key}",
        )


# =============================================================================
# Convenience functions for Earnings Calls page
# =============================================================================

_EC_KEYS = ("ec_company", "ec_year", "ec_quarter")


def load_earnings_calls_state():
    """Load earnings calls filter state from local storage into session state.

    Returns True if values were loaded, False otherwise.
    """
    manager = LocalStorageManager("earnings_calls")
    try:
        if manager.local_storage is None:
            return False
        # Use session-state cache populated by load_from_local_storage() to avoid blocking getItem()
        all_pages = st.session_state.get(LocalStorageManager._SS_CACHE_KEY)
        if not all_pages:
            # Cache not yet primed — attempt a one-time getItem() read (only happens on first render)
            stored_data = manager.local_storage.getItem(manager.STORAGE_KEY)
            if stored_data and stored_data != "null" and stored_data != "":
                all_pages = json.loads(stored_data) if isinstance(stored_data, str) else stored_data
                if isinstance(all_pages, dict):
                    st.session_state[LocalStorageManager._SS_CACHE_KEY] = all_pages
        if isinstance(all_pages, dict) and "earnings_calls" in all_pages:
            page_data = all_pages["earnings_calls"]
            if isinstance(page_data, dict):
                for key in _EC_KEYS:
                    if key in page_data and key not in st.session_state:
                        st.session_state[key] = page_data[key]
                return True
    except Exception:
        pass
    return False


def save_earnings_calls_state():
    """Save current earnings calls filter state to local storage."""
    manager = LocalStorageManager("earnings_calls")
    try:
        if manager.local_storage is None:
            return False
        all_pages = dict(st.session_state.get(LocalStorageManager._SS_CACHE_KEY) or {})

        page_data = {}
        for key in _EC_KEYS:
            if key in st.session_state:
                page_data[key] = st.session_state[key]

        if page_data:
            all_pages["earnings_calls"] = page_data
            json_data = json.dumps(all_pages)
            manager.local_storage.setItem(manager.STORAGE_KEY, json_data)
            st.session_state[LocalStorageManager._SS_CACHE_KEY] = all_pages
            save_to_local_storage_js(manager.STORAGE_KEY, all_pages)
            return True
    except Exception:
        pass
    return False
