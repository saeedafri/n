"""
Screening Page — Coresight Data Portal
======================================
Capital IQ–style progressive company screening.

Architecture:
  - Auth / layout follow newsroom.py conventions exactly
  - All DB access delegated to data.screening_service
  - All metric / table mappings live in data.screening_config
  - Criteria are applied progressively: Add Criteria triggers immediate
    background filtering; Show Results merely renders the pre-computed set
  - Session state keys are prefixed with "scr_" to avoid collisions

This iteration implements: Companies flow only.
  • Industry Classifications
  • Geographic Locations
  • Financial Information (Income Statement, Balance Sheet, Cash Flow)
"""

import json
import hashlib
import os
import re
import textwrap
import threading
import time
import streamlit as st
import pandas as pd
from datetime import date, timedelta
from io import BytesIO
from typing import Dict, List, Optional
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

try:
    from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode, JsCode
    HAS_AGGRID = True
except Exception:
    HAS_AGGRID = False
    JsCode = None  # type: ignore[misc, assignment]

_GRID_PIN_COLUMN_PRIORITY = (
    "Company Name",
    "Company Name(s)",
    "Company",
    "Executive Name",
    "Key Development Headline",
    "Ticker",
)
_GRID_LINK_LABEL = "View Source ↗"
_GRID_LINK_RENDERER = None
if HAS_AGGRID:
    # Class renderer — builds DOM <a> nodes; never returns HTML strings (those render as escaped text).
    _GRID_LINK_RENDERER = JsCode(f"""
    class SourceLinkCellRenderer {{
      init(params) {{
        const wrap = document.createElement('span');
        let href = '';

        if (params.data && params.data._source_url != null) {{
          href = String(params.data._source_url).trim();
        }}
        if (!href && params.value != null) {{
          const raw = String(params.value).trim();
          const htmlMatch = raw.match(/href=["']([^"']+)["']/i);
          href = htmlMatch && htmlMatch[1] ? htmlMatch[1] : raw;
        }}

        if (href && (href.startsWith('http') || href.startsWith('/'))) {{
          const link = document.createElement('a');
          link.innerText = '{_GRID_LINK_LABEL}';
          link.setAttribute('href', href);
          link.setAttribute('target', '_blank');
          link.setAttribute('rel', 'noopener noreferrer');
          link.style.color = '#0066CC';
          link.style.textDecoration = 'none';
          link.style.fontWeight = '500';
          wrap.appendChild(link);
        }}

        this.eGui = wrap;
      }}
      getGui() {{
        return this.eGui;
      }}
      refresh() {{
        return false;
      }}
    }}
    """)

# Excel-style distinct-values column filter (AG Grid Community custom filter).
# Lives inside the existing column-menu funnel: search box + "(Select All)" +
# a scrollable checkbox list of the column's distinct values. Auto-applies on
# toggle (no Apply button), 100% client-side. Registered once as every grid's
# default_column filter so all screening grids inherit it.
# NOTE: JsCode strips JS comments and collapses whitespace — keep this comment-free.
_SUPPRESS_FILTER_RERUN = None
if HAS_AGGRID:
    # Excel never reloads the sheet while you tick boxes; this grid used to reload
    # the whole page on EVERY tick. st_aggrid hands each `filterChanged` straight to
    # Streamlit, which reruns the script and REMOUNTS the component — destroying the
    # open filter popup, so the user had to reopen it after every single checkbox.
    # Measured: one click on (Select All) = 1 rerun, grid remounted, popup gone.
    #
    # st_aggrid calls this gate before returning anything. Returning false while a
    # filter popup is open keeps the toggle purely client-side (AG Grid filters in
    # the browser, instantly), so ticking N values costs ZERO reruns. The filter
    # class flushes once from afterGuiDetached() when the popup closes, and by then
    # this flag is false — so Python still learns the final model for the Excel
    # export, in exactly one rerun instead of one per click.
    _SUPPRESS_FILTER_RERUN = JsCode("""
    function shouldGridReturn(p) {
      try {
        var ev = p ? p.streamlitRerunEventTriggerName : null;
        if (ev === 'filterChanged') {
          if (window.__agFilterFlush === true) {
            window.__agFilterFlush = false;
            return true;
          }
          return false;
        }
      } catch (e) {}
      return true;
    }
    """)

_DISTINCT_VALUES_FILTER = None
if HAS_AGGRID:
    _DISTINCT_VALUES_FILTER = JsCode("""
    class DistinctValuesFilter {
      init(params) {
        this.params = params;
        this.field = (params.colDef && params.colDef.field)
          ? params.colDef.field
          : params.column.getColId();
        this.selected = null;
        this.eGui = document.createElement('div');
        this.eGui.style.cssText = 'font-family:Inter,Arial,sans-serif;font-size:13px;min-width:230px;max-width:340px;padding:8px;';
        var search = document.createElement('input');
        search.type = 'text';
        search.placeholder = 'Search';
        search.style.cssText = 'width:100%;box-sizing:border-box;padding:5px 8px;margin-bottom:6px;border:1px solid #d0d0d0;border-radius:4px;font-size:13px;';
        this.search = search;
        var selAllWrap = document.createElement('label');
        selAllWrap.style.cssText = 'display:flex;align-items:center;gap:7px;padding:4px 2px;font-weight:600;cursor:pointer;border-bottom:1px solid #eee;margin-bottom:4px;';
        var selAll = document.createElement('input');
        selAll.type = 'checkbox';
        selAll.checked = true;
        this.selAll = selAll;
        var selAllTxt = document.createElement('span');
        selAllTxt.textContent = '(Select All)';
        selAllWrap.appendChild(selAll);
        selAllWrap.appendChild(selAllTxt);
        var list = document.createElement('div');
        list.style.cssText = 'max-height:230px;overflow-y:auto;';
        this.list = list;
        this.eGui.appendChild(search);
        this.eGui.appendChild(selAllWrap);
        this.eGui.appendChild(list);
        this.values = [];
        this.buildValues();
        this.render('');
        this.syncSelAll();
        var self = this;
        search.addEventListener('input', function() { self.render(search.value); });
        selAll.addEventListener('change', function() { self.onSelectAll(selAll.checked); });
      }
      buildValues() {
        var self = this;
        var seen = {};
        var order = [];
        var fp = (this.params.colDef && this.params.colDef.filterParams)
          ? this.params.colDef.filterParams : null;
        var preset = (fp && fp.values && fp.values.length) ? fp.values : null;
        if (preset) {
          preset.forEach(function(v) {
            v = (v === null || v === undefined) ? '' : String(v);
            if (!seen[v]) { seen[v] = true; order.push(v); }
          });
        }
        var api = this.params.api;
        if (api && api.forEachLeafNode) {
          api.forEachLeafNode(function(node) {
            var v = (node && node.data) ? node.data[self.field] : '';
            v = (v === null || v === undefined) ? '' : String(v);
            if (!seen[v]) { seen[v] = true; order.push(v); }
          });
        }
        order.sort(function(a, b) {
          return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
        });
        this.values = order;
        var pre = (fp && fp.preselected) ? fp.preselected : null;
        this.selected = pre ? pre.slice() : null;
      }
      render(term) {
        var self = this;
        var t = (term || '').toLowerCase();
        this.list.innerHTML = '';
        var cur = this.currentSet();
        this.values.forEach(function(v) {
          if (t && v.toLowerCase().indexOf(t) === -1) { return; }
          var row = document.createElement('label');
          row.style.cssText = 'display:flex;align-items:center;gap:7px;padding:3px 2px;cursor:pointer;';
          var cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = cur[v] === true;
          cb.addEventListener('change', function() { self.onToggle(v, cb.checked); });
          var span = document.createElement('span');
          span.textContent = (v === '') ? '(Blanks)' : v;
          span.style.cssText = 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
          row.appendChild(cb);
          row.appendChild(span);
          self.list.appendChild(row);
        });
      }
      currentSet() {
        var m = {};
        if (this.selected === null) {
          this.values.forEach(function(v) { m[v] = true; });
        } else {
          var s = this.selected;
          for (var i = 0; i < s.length; i++) { m[s[i]] = true; }
        }
        return m;
      }
      onToggle(v, on) {
        var m = this.currentSet();
        if (on) { m[v] = true; } else { delete m[v]; }
        this.selected = Object.keys(m);
        this.syncSelAll();
        this.params.filterChangedCallback();
      }
      onSelectAll(on) {
        this.selected = on ? null : [];
        this.render(this.search.value);
        this.params.filterChangedCallback();
      }
      selectedCount() {
        return (this.selected === null) ? this.values.length : this.selected.length;
      }
      syncSelAll() {
        var n = this.selectedCount();
        this.selAll.checked = (n === this.values.length);
        this.selAll.indeterminate = (n > 0 && n < this.values.length);
      }
      isFilterActive() {
        return (this.selected !== null) && (this.selected.length < this.values.length);
      }
      doesFilterPass(p) {
        if (!this.isFilterActive()) { return true; }
        var v = (p.data) ? p.data[this.field] : '';
        v = (v === null || v === undefined) ? '' : String(v);
        return this.currentSet()[v] === true;
      }
      getModel() {
        if (!this.isFilterActive()) { return null; }
        return { values: this.selected.slice() };
      }
      setModel(m) {
        this.selected = (m && m.values) ? m.values.slice() : null;
        if (this.list) { this.render(this.search ? this.search.value : ''); this.syncSelAll(); }
      }
      getGui() { return this.eGui; }
      afterGuiAttached() {
        window.__agFilterPopupOpen = true;
        this.openSnapshot = JSON.stringify(this.selected);
        if (this.search) { this.search.focus(); }
      }
      afterGuiDetached() {
        window.__agFilterPopupOpen = false;
        var now = JSON.stringify(this.selected);
        if (now !== this.openSnapshot) {
          this.openSnapshot = now;
          window.__agFilterFlush = true;
          this.params.filterChangedCallback();
        }
      }
    }
    """)

CORESIGHT_LOGO_URL = (
    "https://production-wordpress-cdn-dpa0g9bzd7b3h7gy.z03.azurefd.net"
    "/wp-content/uploads/2023/12/coresight-logo-1.png"
)

# =============================================================================
# AUTH — must be first executable statement
# =============================================================================
from core.auth_manager import require_auth
# require_auth(page="screening")

# =============================================================================
# SCREENING IMPORTS
# =============================================================================
from data.screening_config import (
    OPERATORS,
    STATEMENT_CONFIG,
    TIMEFRAMES,
    PERIOD_TYPES,
    PERIOD_TYPE_LABELS,
    QUARTERS,
    TRAILING_QUARTERS_OPTIONS,
    TRAILING_QUARTERS_DEFAULT,
    TRAILING_QUARTERS_STMTS,
    SCREENING_YEARS,
    FORWARD_SCREENING_YEARS,
    FORWARD_LOOKING_STMTS,
    KEYDEV_CATEGORIES,
    KEYDEV_CATEGORIES_ALL,
    KEYDEV_LABEL_TO_KEY,
    KEYDEV_TIMEFRAMES,
    TABULAR_MARKET_DATA_STMTS,
    SEGMENT_STATEMENT_TYPES,
    get_metric_labels,
    get_metric_info,
)
from data.screening_service import (
    get_all_industries,
    get_all_countries,
    get_segment_member_options,
    read_segment_member_options_cache,
    get_segment_values_cache_status,
    populate_segment_member_cache_from_presets,
    rebuild_segment_values_cache_async,
    get_segment_cache_build_state,
    SEGMENT_CACHE_UNAVAILABLE_MESSAGE,
    recompute_working_set,
    criteria_stack_fingerprint,
    build_industry_summary,
    build_geography_summary,
    build_financial_summary,
    build_keydevs_summary,
    build_biz_segments_summary,
    build_geo_segments_summary,
    build_additional_summary,
    get_base_company_universe,
    get_all_companies_universe,
    get_keydevs_events_by_industry,
    KEYDEV_PER_INDUSTRY,
    apply_industry_criterion,
    ALL_TICKERS,
    filter_universe_to_members,
    keep_working_set_companies,
    merge_company_columns,
    KeydevsQueryError,
    FILTERING_CRITERION_TYPES,
    get_keydevs_events_for_tickers,
    get_keydevs_events_count,
    keydev_subtype_domain,
    keydev_industry_domain,
    fetch_all_keydevs_events,
    keydevs_period_display_label,
    resolve_keydevs_event_window,
)
from data.saved_criteria_service import (
    save_criteria        as svc_save_criteria,
    get_user_criteria    as svc_get_criteria,
    update_criteria      as svc_update_criteria,
    delete_criteria      as svc_delete_criteria,
    grant_access         as svc_grant_criteria_access,
    revoke_access        as svc_revoke_criteria_access,
    get_access_grants    as svc_get_criteria_grants,
    ensure_tables        as svc_ensure_criteria_tables,
)
from data.watchlist_service import (
    create_watchlist               as _real_wl_create,
    get_user_watchlists            as _real_wl_get_all,
    get_watchlist_companies        as _real_wl_get_companies,
    get_watchlist_tickers          as _real_wl_get_tickers,
    add_companies_to_watchlist     as _real_wl_add_companies,
    remove_company_from_watchlist  as _real_wl_remove_company,
    replace_watchlist_companies    as wl_replace_companies,
    update_watchlist               as _real_wl_update,
    delete_watchlist               as _real_wl_delete,
    grant_watchlist_access         as wl_grant_access,
    revoke_watchlist_access        as wl_revoke_access,
    get_watchlist_access_grants    as wl_get_grants,
    ensure_tables                  as wl_ensure_tables,
)

# ---------------------------------------------------------------------------
# In-memory fallback so the UI works even when the DB is unreachable (local
# dev without VPN).  The real DB functions are tried first; only if they fail
# because the DB is down do we fall back to the in-memory store.
# Remove this block once the DB is stable / always reachable.
# ---------------------------------------------------------------------------

class _InMemoryWatchlistStore:
    """Thread-safe in-memory watchlist store for local dev fallback.

    Data is kept in st.session_state so it survives Streamlit reruns
    (the module is re-executed each rerun, but session_state persists).
    """
    _db_ok: bool | None = None  # class-level so it survives instances

    @staticmethod
    def _store():
        """Return the persistent dict from session_state."""
        if "_mem_wl_data" not in st.session_state:
            st.session_state["_mem_wl_data"] = {
                "watchlists": {},  # id -> dict
                "companies": {},   # id -> list[dict]
                "next_id": 1,
            }
        return st.session_state["_mem_wl_data"]

    def _check_db(self):
        if _InMemoryWatchlistStore._db_ok is not None:
            return _InMemoryWatchlistStore._db_ok
        try:
            from core.database import db_manager
            result = db_manager.execute_query("SELECT 1 AS ok")
            _InMemoryWatchlistStore._db_ok = bool(result)
        except Exception:
            _InMemoryWatchlistStore._db_ok = False
        return _InMemoryWatchlistStore._db_ok

    def create(self, name, user_email, description=""):
        if self._check_db():
            return _real_wl_create(name, user_email, description)
        s = self._store()
        wid = s["next_id"]; s["next_id"] += 1
        s["watchlists"][wid] = dict(
            id=wid, name=name, description=description or "",
            created_by=user_email, is_owner=True, can_edit=True,
            can_delete=True, company_count=0,
        )
        s["companies"][wid] = []
        return wid

    def get_all(self, user_email):
        if self._check_db():
            return _real_wl_get_all(user_email)
        s = self._store()
        return [
            {**w,
             "company_count": len(s["companies"].get(w["id"], [])),
             "is_owner": w["created_by"] == user_email,
             "can_edit": w["created_by"] == user_email,
             "can_delete": w["created_by"] == user_email}
            for w in s["watchlists"].values()
        ]

    def get_companies(self, wl_id):
        if self._check_db():
            return _real_wl_get_companies(wl_id)
        return list(self._store()["companies"].get(wl_id, []))

    def get_tickers(self, wl_id):
        if self._check_db():
            return _real_wl_get_tickers(wl_id)
        return {c["ticker"] for c in self._store()["companies"].get(wl_id, [])}

    def add_companies(self, wl_id, companies, user_email):
        if self._check_db():
            return _real_wl_add_companies(wl_id, companies, user_email)
        s = self._store()
        # Composite identity (ticker, name) — shared tickers (JD/LULU/TSCO) are
        # two distinct companies, so dedup on ticker alone would drop one.
        existing = {(c["ticker"], c.get("company_name") or "")
                    for c in s["companies"].get(wl_id, [])}
        added = 0
        for c in companies:
            key = (c["ticker"], c.get("company_name") or "")
            if key not in existing:
                s["companies"].setdefault(wl_id, []).append(c)
                existing.add(key)
                added += 1
        return added

    def remove_company(self, wl_id, ticker, user_email, company_name=None):
        if self._check_db():
            return _real_wl_remove_company(wl_id, ticker, user_email, company_name)
        s = self._store()
        before = len(s["companies"].get(wl_id, []))
        s["companies"][wl_id] = [
            c for c in s["companies"].get(wl_id, [])
            if not (c["ticker"] == ticker
                    and (company_name is None or (c.get("company_name") or "") == company_name))
        ]
        return len(s["companies"][wl_id]) < before

    def update(self, wl_id, user_email, **kwargs):
        if self._check_db():
            return _real_wl_update(wl_id, user_email, **kwargs)
        wl = self._store()["watchlists"].get(wl_id)
        if not wl:
            return False
        for k, v in kwargs.items():
            if v is not None and k in wl:
                wl[k] = v
        return True

    def delete(self, wl_id, user_email):
        if self._check_db():
            return _real_wl_delete(wl_id, user_email)
        s = self._store()
        if wl_id in s["watchlists"]:
            del s["watchlists"][wl_id]
            s["companies"].pop(wl_id, None)
            return True
        return False

_mem_wl = _InMemoryWatchlistStore()
wl_create         = _mem_wl.create
wl_get_all        = _mem_wl.get_all
wl_get_companies  = _mem_wl.get_companies
wl_get_tickers    = _mem_wl.get_tickers
wl_add_companies  = _mem_wl.add_companies
wl_remove_company = _mem_wl.remove_company
wl_update         = _mem_wl.update
wl_delete         = _mem_wl.delete


def _set_active_watchlist_scope(wl_id):
    """Store the active watchlist's scope in session state.

    Sets THREE coordinated keys (always together):
      • scr_watchlist_tickers — set[str], used by segment-options/back-compat
      • scr_watchlist_members — list[(ticker, company_name)], the exact identity
        used to filter the universe (shared tickers JD/LULU/TSCO need the name)
      • scr_watchlist_count   — int, the true company count (matches the universe
        rows kept), so the UI never shows a collapsed ticker-set length.
    """
    cos = wl_get_companies(wl_id)
    members = [(c["ticker"], c.get("company_name") or "") for c in cos]
    tickers = {t for t, _ in members}
    try:
        count = len(filter_universe_to_members(get_base_company_universe(), members))
    except Exception:
        count = len(members)
    st.session_state.scr_watchlist_tickers = tickers
    st.session_state.scr_watchlist_members = members
    st.session_state.scr_watchlist_count   = count
    return tickers, members, count


def _clear_active_watchlist_scope():
    """Clear all three watchlist-scope keys together."""
    st.session_state.scr_watchlist_tickers = None
    st.session_state.scr_watchlist_members = None
    st.session_state.scr_watchlist_count   = None
from core.auth_manager import get_current_user, get_auth_data
from data.portal_users_service import (
    get_all_portal_users     as pu_get_all,
    get_portal_user_by_email as pu_get_by_email,
    ensure_tables            as pu_ensure_tables,
)
from components.styles import hide_sidebar, set_page_layout, render_styles
from components.navigation import render_header, render_coresight_footer

try:
    from utils.server_logger import (
        new_rerun_id, PageLoadTracker, log_render_complete,
        log_structured_error, log_error, log_warning, log_timing, log_info,
    )
except ImportError:
    def new_rerun_id(*a, **kw): return ''
    PageLoadTracker = None
    def log_structured_error(*a, **kw): pass
    def log_error(*a, **kw): pass
    def log_warning(*a, **kw): pass
    def log_timing(*a, **kw): pass
    def log_info(*a, **kw): pass
    def log_render_complete(*a, **kw): pass

hide_sidebar()
# =============================================================================
# CONSTANTS
# =============================================================================

_SCREEN_FOR_OPTIONS = [
    "Companies",
    "Key Devs",
    "Equities",
    "Fixed Income",
    "People",
    "Transactions",
    "Projects/Portfolios",
]

_CRITERIA_OPTIONS = [
    ("Industry Classifications",       "industry",   True),
    ("Geographic Locations",           "geography",  True),
    ("Financial Information",          "financial",  True),
    ("Key Developments by Category",   "keydevs",    True),
]

# Friendly label for "Screen For" → page subtitle
_SCREEN_TITLES = {
    "Companies":          "Company Screening",
    "Equities":           "Equity Screening",
    "Fixed Income":       "Fixed Income Screening",
    "Key Devs":           "Key Developments Screening",
    "People":             "People Screening",
    "Transactions":       "Transaction Screening",
    "Projects/Portfolios":"Project/Portfolio Screening",
}

# =============================================================================
# SESSION STATE INITIALIZER
# =============================================================================

def _init_state():
    """Initialize all screening session state keys (idempotent)."""
    try:
        defaults = {
            "scr_screen_for":       "Companies",
            "scr_active_criteria":  [],        # list of criterion dicts
            "scr_working_df":       None,      # pd.DataFrame | None
            "scr_debug_trace":      [],        # list of debug dicts
            "scr_active_form":      None,      # "industry" | "geography" | "financial" | None
            "scr_show_results":     False,
            "scr_prefill":          None,      # pre-fill dict when editing a criterion
            "scr_prefill_idx":      None,      # index of criterion being edited
            # Financial form sub-state (live outside form so options are dynamic)
            "scr_fin_stmt":         list(STATEMENT_CONFIG.keys())[0],
            "scr_fin_metric":       None,
            "scr_fin_operator":     "Greater Than",
            "scr_fin_timeframe":    "Latest",
            "scr_geo_available":    None,      # None = not checked yet; True/False
            "scr_criterion_cache":  {},        # criterion-level result cache (see screening_service)
            # ── Watchlist ──
            "scr_active_watchlist_id":   None,   # int | None — active watchlist filter
            "scr_active_watchlist_name": None,   # str | None — display name
            "scr_watchlist_tickers":     None,   # set | None — tickers in active watchlist
            "scr_watchlist_members":     None,   # list[(ticker,name)] | None — exact identity
            "scr_watchlist_count":       None,   # int | None — true company count (composite)
            # ── DB bootstrap flag ──
            "scr_db_tables_ready":       False,
            "scr_results_loading":       False,
            "scr_results_pending_paint": False,
            "scr_show_results_requested": False,
            "scr_results_error":         None,
            "scr_criteria_fingerprint":    None,
            "scr_last_computed_fingerprint": None,
        }
        for key, val in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = val
    except Exception as e:
        log_structured_error(e, page="screening", component="_init_state", operation="initialize_session_state")


# =============================================================================
# HELPERS
# =============================================================================

def _reset_criteria():
    """Clear all active criteria, working set, and criterion cache."""
    try:
        st.session_state.scr_active_criteria = []
        st.session_state.scr_working_df = None
        st.session_state.scr_debug_trace = []
        st.session_state.scr_show_results = False
        st.session_state.scr_active_form = None
        st.session_state.scr_criterion_cache = {}
        st.session_state.scr_results_error = None
        st.session_state.scr_results_loading = False
        st.session_state.scr_show_results_requested = False
        st.session_state.scr_last_computed_fingerprint = None
    except Exception as e:
        log_structured_error(e, page="screening", component="_reset_criteria", operation="clear_criteria")


def _sync_criteria_fingerprint():
    """Update session fingerprint from active criteria + watchlist scope."""
    try:
        st.session_state.scr_criteria_fingerprint = criteria_stack_fingerprint(
            st.session_state.get("scr_active_criteria", []),
            st.session_state.get("scr_watchlist_tickers"),
            allowed_members=st.session_state.get("scr_watchlist_members"),
        )
    except Exception as e:
        log_structured_error(
            e, page="screening", component="_sync_criteria_fingerprint", operation="fingerprint",
        )


def _needs_results_recompute() -> bool:
    """True when Show Results must re-run the criteria pipeline."""
    if st.session_state.get("scr_working_df") is None:
        return True
    return (
        st.session_state.get("scr_criteria_fingerprint")
        != st.session_state.get("scr_last_computed_fingerprint")
    )


def _render_coresight_loading_overlay(message: str, subtext: str = ""):
    """Full-viewport branded loading overlay. Returns the placeholder holding it.

    CALLERS MUST CLEAR IT (`slot.empty()`) once the work is done. The overlay is
    `position: fixed` across the whole viewport, so one left behind keeps swallowing
    every click — Playwright reports it plainly: ".scr-loading-overlay … subtree
    intercepts pointer events" — while the finished results sit visible underneath.
    It used to be emitted with a bare `st.markdown` that nothing ever removed; the
    only reason this was not obvious before is that the wrapper's `cs-rise`
    animation left it at opacity:0, so it was an INVISIBLE click-blocker. That is
    the "I press the button and nothing happens" symptom.
    """
    sub_html = (
        f"<p class='scr-ov-sub'>{subtext}</p>" if subtext else ""
    )
    overlay_html = textwrap.dedent(
        f"""
        <div class="scr-loading-overlay" role="status" aria-live="polite">
          <div class="scr-loading-card">
            <img src="{CORESIGHT_LOGO_URL}" alt="Coresight" class="scr-loading-logo">
            <div class="scr-loading-spinner" aria-hidden="true"></div>
            <p class="scr-ov-msg">{message}</p>
            {sub_html}
          </div>
        </div>
        """
    ).strip()
    slot = st.empty()
    slot.markdown(overlay_html, unsafe_allow_html=True)
    return slot


def _narrowing_criteria(criteria: List[dict]) -> bool:
    """True when a criterion actually drops companies (Industry / Geography).

    Only those narrow the working set — financial, key-dev and segment criteria
    annotate it (see FILTERING_CRITERION_TYPES). When none is present the working
    set is still the whole universe, which is what lets the event query skip its
    ticker IN list.
    """
    return any(c.get("type") in FILTERING_CRITERION_TYPES for c in criteria)


def _all_companies_on() -> bool:
    """True in Key Devs mode, which always covers every ticker that has events.

    Key-dev screening is about the events themselves, and bounding it to the ~464
    Coresight companies hid 9,741 of the 21,034 M&A Activity events (46%). So this
    mode always screens the full ~4,350-ticker event universe — no opt-in.

    Every other mode stays on the curated Coresight master.

    Industry no longer narrows a Key Devs screen back to Coresight coverage: the
    SEC and non-SEC masters now carry `primary_industry_coresight` too, so
    get_all_companies_universe() gives 96% of event tickers a real industry and the
    criterion filters the whole event universe rather than the 439 it can name.

    Geography filters this same wide universe, but on thinner data: `country` comes
    from the Alpha Vantage listing and is populated for 58% of event tickers versus
    96% for industry. A Country criterion therefore drops the 42% that have no
    country at all — a coverage gap in that column, not a Coresight-only universe.
    """
    return st.session_state.get("scr_screen_for") == "Key Devs"


def _screening_universe():
    """Base universe honouring the all-companies toggle."""
    return get_all_companies_universe() if _all_companies_on() else get_base_company_universe()


def _trigger_recompute():
    """Re-run the full criteria pipeline and store working_df + trace."""
    criteria = st.session_state.get("scr_active_criteria", [])
    wl_tickers = st.session_state.get("scr_watchlist_tickers")
    wl_members = st.session_state.get("scr_watchlist_members")
    # Branded loading overlay during the (multi-second, uncached) criteria pipeline.
    # EVERY apply/edit/remove path routes through here, so this single guard replaces
    # the blank-no-spinner screen the criterion-apply path used to show while the
    # additional-data / keydevs queries ran.
    #
    # This used to skip the Show Results path ("it renders its own overlay first").
    # It does not: that overlay lives near the end of the script, i.e. AFTER this
    # work has already run, so it never painted while anything was loading. Screen
    # recording of a cold run: 34 seconds from the click with ZERO pixel change —
    # and Streamlit's own "Running" indicator is off (hideTopBar=true in
    # .streamlit/config.toml), so there was no feedback of any kind. Rendering here,
    # BEFORE recompute_working_set, is what the user actually sees.
    # The Show Results path renders its own overlay (scr_results_loading, below), so
    # this one must stay out of its way: the cards are position:fixed and two of
    # them stack into a visible double card. That is what this guard is for.
    overlay_slot = None
    if criteria and not st.session_state.get("scr_results_loading"):
        try:
            overlay_slot = _render_coresight_loading_overlay(
                "Applying criteria…", "Screening companies against your filters."
            )
        except Exception:
            pass
    try:
        cache = st.session_state.get("scr_criterion_cache", {})
        log_info(
            f"[TIMING] SHOW_RESULTS_RECOMPUTE_START | criteria={len(criteria)} "
            f"watchlist={len(wl_members) if wl_members else (len(wl_tickers) if wl_tickers else 'all')}"
        )
        t_rec = time.perf_counter()
        df, trace = recompute_working_set(
            criteria,
            criterion_cache=cache,
            allowed_tickers=wl_tickers,
            allowed_members=wl_members,
            # Key Devs mode renders one row per EVENT, so the per-company key-dev
            # detail column is never displayed there — don't pay to build it.
            keydev_details=(st.session_state.get("scr_screen_for") != "Key Devs"),
            all_companies=_all_companies_on(),
        )
        ms_rec = (time.perf_counter() - t_rec) * 1000
        log_timing(
            "SHOW_RESULTS_RECOMPUTE_TOTAL",
            ms_rec,
            f"criteria={len(criteria)} rows={len(df)}",
        )

        st.session_state.scr_working_df = df
        st.session_state.scr_debug_trace = trace
        st.session_state.scr_criterion_cache = cache
        st.session_state.scr_last_computed_fingerprint = (
            st.session_state.get("scr_criteria_fingerprint")
        )
        st.session_state.pop("scr_results_error", None)
    except Exception as e:
        crit_meta = []
        for c in criteria:
            if c.get("hidden"):
                continue
            crit_meta.append({
                "type": c.get("type"),
                "statement": c.get("statement"),
                "metric": c.get("metric_label"),
                "period": c.get("timeframe") or c.get("year"),
            })
        log_structured_error(
            e,
            page="screening",
            component="_trigger_recompute",
            operation="recompute_working_set",
            criteria_count=len(criteria),
            criteria_summary=crit_meta,
            watchlist_size=len(wl_tickers) if wl_tickers else None,
        )
        st.session_state.scr_results_error = str(e)
        raise
    finally:
        # Always, even on the error path — an orphaned overlay blocks every click.
        if overlay_slot is not None:
            try:
                overlay_slot.empty()
            except Exception:
                pass


def _add_criterion(criterion: dict):
    """Append a criterion to the stack and recompute."""
    try:
        st.session_state.scr_active_criteria.append(criterion)
        _trigger_recompute()
        st.session_state.scr_active_form = None
        st.session_state.scr_show_results = False
    except Exception as e:
        log_structured_error(e, page="screening", component="_add_criterion", operation="add_criterion")
        st.error("Something went wrong. Please try again.")


def _remove_criterion(idx: int):
    """Remove criterion at index and recompute."""
    try:
        removed = st.session_state.scr_active_criteria.pop(idx)
        _trigger_recompute()
        st.session_state.scr_show_results = False
    except Exception as e:
        log_structured_error(e, page="screening", component="_remove_criterion", operation="remove_criterion")
        st.error("Something went wrong. Please try again.")


def _start_edit_criterion(idx: int):
    """Load an existing criterion into the form for editing."""
    try:
        criterion = st.session_state.scr_active_criteria[idx]
        # Remove it from stack first (will be re-added on save)
        st.session_state.scr_active_criteria.pop(idx)
        _trigger_recompute()
        st.session_state.scr_prefill = criterion
        st.session_state.scr_prefill_idx = idx
        st.session_state.scr_active_form = criterion.get("type")
    except Exception as e:
        log_structured_error(e, page="screening", component="_start_edit_criterion", operation="edit_criterion")
        st.error("Something went wrong. Please try again.")


# These DDLs are idempotent (CREATE TABLE IF NOT EXISTS) and identical for every
# user, so bootstrapping is a PROCESS-level concern, not a per-session one. Gating
# on st.session_state made every NEW browser session pay ~5 table round-trips on
# its first screening landing (painful over a high-latency link / VPN).
#
# NOTE: Streamlit re-executes a page file on every rerun, so this module global
# resets each time and does NOT actually make the work once-per-process — the
# real guards live in the imported data modules (`*_service.ensure_tables`,
# `ensure_segment_*_cache_table`), which are cached in sys.modules and therefore
# do persist. Keep this flag only as a cheap same-run short-circuit; never rely
# on it to skip a round trip across reruns.
_DB_TABLES_READY = False


def _ensure_db_tables():
    """Bootstrap saved-criteria, watchlist, and portal-users tables once per server process."""
    global _DB_TABLES_READY
    if not _DB_TABLES_READY:
        try:
            svc_ensure_criteria_tables()
            wl_ensure_tables()
            pu_ensure_tables()
            from data.screening_service import (
                ensure_segment_member_cache_table,
                ensure_segment_values_cache_table,
                populate_segment_member_cache_from_presets,
                read_segment_member_options_cache,
            )
            ensure_segment_member_cache_table()
            ensure_segment_values_cache_table()
            # Populate member cache from presets so segment dropdowns load instantly.
            # Only runs if the cache is empty (first startup or after a rebuild).
            if not read_segment_member_options_cache("business"):
                populate_segment_member_cache_from_presets()
            _DB_TABLES_READY = True
            st.session_state.scr_db_tables_ready = True
        except Exception as exc:
            log_structured_error(exc, page="screening", component="_ensure_db_tables",
                                 operation="bootstrap_tables")


# ---------------------------------------------------------------------------
# Per-user Streamlit cache wrappers (30s TTL — fast, near-instant re-read)
# We cache at this layer so multiple reruns in the same 30s window reuse the
# last DB result without re-querying.  Any write operation MUST call .clear()
# on these functions to invalidate immediately.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30, show_spinner=False)
def _cached_user_criteria(user_email: str):
    """Cached wrapper for svc_get_criteria — keyed per user_email."""
    return svc_get_criteria(user_email)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_user_watchlists(user_email: str):
    """Cached wrapper for wl_get_all — keyed per user_email."""
    return wl_get_all(user_email)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_portal_users(exclude_email: str = ""):
    """Cached wrapper for portal users list (5 min TTL — rarely changes)."""
    return pu_get_all(exclude_email or None)


def _invalidate_criteria_cache():
    """Call after any criteria write to force next render to re-query."""
    try:
        _cached_user_criteria.clear()
    except Exception:
        pass


def _invalidate_watchlist_cache():
    """Call after any watchlist write to force next render to re-query."""
    try:
        _cached_user_watchlists.clear()
    except Exception:
        pass


def _queue_toast(msg: str, icon: str = "✅"):
    """Queue a toast to display after the next rerun.

    st.toast() called before st.rerun() is discarded because the rerun
    aborts the current render cycle.  Instead, stash the message in
    session state and call _flush_pending_toasts() early in main().
    """
    pending = st.session_state.get("_pending_toasts") or []
    pending.append((msg, icon))
    st.session_state["_pending_toasts"] = pending


def _flush_pending_toasts():
    """Display and clear any queued toast messages."""
    pending = st.session_state.pop("_pending_toasts", None)
    if pending:
        for msg, icon in pending:
            st.toast(msg, icon=icon)


def _current_user_email() -> str:
    """Return the authenticated user's email (empty string if unavailable)."""
    try:
        auth = get_auth_data()
        email = (auth or {}).get("user_email", "") or ""
        if email:
            return email
        if (
            os.getenv("APP_ENV", "").upper() == "LOCAL"
            and os.getenv("DEBUG", "").lower() in ("true", "1", "yes")
        ):
            return os.getenv("LOCAL_TEST_USER_EMAIL", "").strip()
        return ""
    except Exception:
        return ""


def _working_count() -> int:
    """Return row count of current working set (or -1 if not computed)."""
    try:
        df = st.session_state.get("scr_working_df")
        if df is None:
            return -1
        return len(df)
    except Exception as e:
        log_structured_error(e, page="screening", component="_working_count", operation="get_working_count")
        return -1


def _company_display_name(row) -> str:
    """Compact CIQ-style company label: Name (EXCH:TICKER)."""
    nm = row.get("company_name") or ""
    tk = row.get("ticker") or ""
    exch = row.get("exchange") or ""
    if nm and exch and tk:
        return f"{nm} ({exch}:{tk})"
    if nm and tk:
        return f"{nm} ({tk})"
    return nm or tk or ""


def _format_screening_cell_value(val, unit: str = "") -> str:
    """Format a numeric screening result for HTML table display."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        fv = float(val)
    except (TypeError, ValueError):
        return str(val) if val not in ("", "None", "nan") else "N/A"
    if unit == "%":
        return f"{fv:,.2f}%"
    if unit == "$mm":
        return f"{fv:,.2f}"
    if unit == "$":
        return f"{fv:,.2f}"
    if unit == "x":
        return f"{fv:,.2f}x" if fv >= 0.5 else f"{fv:,.2f}"
    return f"{fv:,.2f}"


def _criterion_unit(criteria: List[dict], display_col: str) -> str:
    for c in criteria:
        if not c.get("metric_info"):
            continue
        if (c.get("display_col") == display_col
                or display_col in (c.get("quarter_cols") or [])
                or display_col in (c.get("year_cols") or [])):
            return c["metric_info"].get("unit", "") or ""
    return ""


def _is_segment_result_col(criteria: List[dict], display_col: str) -> bool:
    for c in criteria:
        if c.get("display_col") == display_col and c.get("statement") in SEGMENT_STATEMENT_TYPES:
            return True
    return False


def _build_results_display_df(
    df: pd.DataFrame,
    criteria: List[dict],
    metric_cols: List[str],
) -> pd.DataFrame:
    """Visible results only: Company Name + selected metric columns."""
    display_df = pd.DataFrame()
    display_df["Company Name"] = df.apply(_company_display_name, axis=1)

    for col in metric_cols:
        if col not in df.columns:
            continue
        if _is_segment_result_col(criteria, col):
            display_df[col] = df[col].apply(
                lambda v: "N/A" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
            )
        else:
            unit = _criterion_unit(criteria, col)
            display_df[col] = df[col].apply(
                lambda v, u=unit: _format_screening_cell_value(v, u)
            )

    return display_df


def _criteria_display_cols(
    criteria: List[dict],
    available,
    include_keydevs: bool = True,
) -> List[str]:
    """Ordered result columns contributed by the active criteria.

    One definition for every mode, so Companies / Key Devs / People cannot drift
    on which criterion is visible — Key Devs used to show none of them.
    Industry and Geography are non-filtering selectors: they annotate the working
    set rather than narrowing it, so their column IS the whole result of adding
    the criterion and must be shown.

    `include_keydevs` is False in Key Devs mode, where every row already IS a key
    development and a per-company summary column repeated down the grid is noise.
    """
    cols = []
    if any(c.get("type") == "industry" for c in criteria):
        cols.append("Industry")
    if any(c.get("type") == "geography" for c in criteria):
        cols.append("Country")
    cols += _get_financial_metric_cols(criteria)
    if include_keydevs:
        cols += _get_keydevs_cols(criteria)
    cols += _get_biz_segments_cols(criteria)
    cols += _get_geo_segments_cols(criteria)
    cols += _get_additional_cols(criteria)

    available = set(available)
    seen = set()
    return [c for c in cols
            if c in available and not (c in seen or seen.add(c))]


def _get_financial_metric_cols(criteria: List[dict]) -> List[str]:
    """Return list of financial metric column names currently in active criteria."""
    try:
        cols = []
        seen = set()
        for c in criteria:
            if c.get("type") != "financial":
                continue
            # Quarterly multi-column criteria (Last N Quarters / Quarter Range)
            # contribute one column per quarter (stamped on quarter_cols).
            if c.get("quarter_cols"):
                for qc in c["quarter_cols"]:
                    if qc not in seen:
                        cols.append(qc)
                        seen.add(qc)
                continue
            # Year-range criterion contributes one column per fiscal year.
            if c.get("year_cols"):
                for yc in c["year_cols"]:
                    if yc not in seen:
                        cols.append(yc)
                        seen.add(yc)
                continue
            if c.get("display_col"):
                col = c["display_col"]
                if col not in seen:
                    cols.append(col)
                    seen.add(col)
        return cols
    except Exception as e:
        log_structured_error(e, page="screening", component="_get_financial_metric_cols", operation="get_metric_cols")
        return []


def _get_keydevs_cols(criteria: List[dict]) -> List[str]:
    """Return list of key developments result column names."""
    try:
        cols = []
        for c in criteria:
            if c.get("type") == "keydevs" and c.get("display_col"):
                cols.append(c["display_col"])
        return cols
    except Exception as e:
        log_structured_error(e, page="screening", component="_get_keydevs_cols", operation="get_keydevs_cols")
        return []


def _get_biz_segments_cols(criteria: List[dict]) -> List[str]:
    """Return list of business segments result column names."""
    try:
        return [c["display_col"] for c in criteria
                if c.get("type") == "biz_segments" and c.get("display_col")]
    except Exception as e:
        log_structured_error(e, page="screening", component="_get_biz_segments_cols", operation="get_cols")
        return []


def _get_geo_segments_cols(criteria: List[dict]) -> List[str]:
    """Return list of geographic segments result column names."""
    try:
        return [c["display_col"] for c in criteria
                if c.get("type") == "geo_segments" and c.get("display_col")]
    except Exception as e:
        log_structured_error(e, page="screening", component="_get_geo_segments_cols", operation="get_cols")
        return []


def _get_additional_cols(criteria: List[dict]) -> List[str]:
    """Return list of additional data result column names."""
    try:
        return [c["display_col"] for c in criteria
                if c.get("type") == "additional" and c.get("display_col")]
    except Exception as e:
        log_structured_error(e, page="screening", component="_get_additional_cols", operation="get_cols")
        return []


# =============================================================================
# CSS
# =============================================================================

_SCREENING_CSS = """
<style>
/* ── Screen-For radio row ── */
/* Remove grey background/border from the Screen For radio container */
div[data-testid="stRadio"] {
  background: transparent !important;
  border: none !important;
  padding: 0 !important;
}
div[data-testid="stRadio"] > div {
  background: transparent !important;
  border: none !important;
}
/* Inline label for the radio group */
div[data-testid="stRadio"] > label {
  font-size: 0.85rem !important;
  font-weight: 600 !important;
  color: #495057 !important;
  margin-bottom: 6px !important;
}
/* Keep radio options on one horizontal line */
div[data-testid="stRadio"] [role="radiogroup"] {
  flex-wrap: nowrap !important;
  gap: 24px !important;
}
/* ── Criteria palette cards ── */
.criteria-palette-title {
  font-size: 0.85rem;
  color: #6c757d;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  margin-bottom: 8px;
}
/* ── Active criterion card ── */
.criterion-card {
  background: #ffffff;
  border: 1px solid #e9ecef;
  border-left: 4px solid #d62e2f;
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 8px;
}
.criterion-card-num {
  color: #6c757d;
  font-size: 0.78rem;
  font-weight: 600;
}
.criterion-card-summary {
  font-size: 0.9rem;
  color: #212529;
  font-weight: 500;
}
.criterion-card-count {
  font-size: 0.78rem;
  color: #28a745;
  font-weight: 600;
}
/* ── Show Results loading overlay ── */
.scr-loading-overlay {
  position: fixed;
  inset: 0;
  z-index: 99990;
  background: rgba(242, 242, 242, 0.92);
  display: flex;
  align-items: center;
  justify-content: center;
}
/* styles.py animates EVERY element-container / stMarkdownContainer / horizontal
   column with `cs-rise` — fill-mode `both`, which both starts at opacity:0 AND
   applies `transform: translateY(10px)`. That breaks this overlay two ways:
     1. the rerun replaces the element before the animation plays, so the wrapper
        stays at opacity:0 and the overlay is in the DOM but invisible;
     2. a live transform makes that ancestor the CONTAINING BLOCK for a
        position:fixed child, so `inset:0` resolves against the wrapper instead of
        the viewport. `_trigger_recompute` renders "Applying criteria…" from inside
        an st.columns() block, so the card was laid out inside a ~240px column —
        squeezed against the left edge, one character per line.
   Enumerating test-ids missed the column wrapper, so match EVERY div ancestor of
   the overlay instead. Deliberately NOT forcing `opacity:1` here: `animation:none`
   already restores the natural opacity, and leaving opacity alone lets Streamlit
   dim and drop the stale overlay normally instead of pinning it on screen. */
div:has(.scr-loading-overlay) {
  animation: none !important;
  will-change: auto !important;
  transform: none !important;
  filter: none !important;
  perspective: none !important;
}
.scr-loading-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 32px 44px;
  background: #fff;
  border-radius: 14px;
  box-shadow: 0 6px 40px rgba(0, 0, 0, 0.10);
  max-width: 420px;
  text-align: center;
}
.scr-loading-logo {
  width: 144px;
  height: auto;
  display: block;
}
.scr-loading-spinner {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: 3px solid rgba(214, 46, 47, 0.12);
  border-top-color: #d62e2f;
  animation: scr-spin 0.7s linear infinite;
}
@keyframes scr-spin {
  to { transform: rotate(360deg); }
}
/* ── Results grid: mask the AG Grid iframe bootstrap (~1s) ──
   The component iframe reserves its height immediately but paints its rows a
   beat later, flashing an empty grey box. We layer a white fill + centred
   spinner BEHIND the iframe; once AG Grid paints its (opaque white) grid on
   top, both are naturally covered — no JS timing needed. */
[class*="st-key-scr_grid_shell"] {
  position: relative;
  min-height: 520px;
}
[class*="st-key-scr_grid_shell"]::before {   /* white fill replaces the grey flash */
  content: "";
  position: absolute;
  inset: 0;
  background: #ffffff;
  z-index: 0;
}
[class*="st-key-scr_grid_shell"]::after {    /* centred spinner during load */
  content: "";
  position: absolute;
  top: 232px;
  left: 50%;
  width: 34px;
  height: 34px;
  margin-left: -17px;
  border-radius: 50%;
  border: 3px solid rgba(214, 46, 47, 0.12);
  border-top-color: #d62e2f;
  animation: scr-spin 0.7s linear infinite;
  z-index: 1;
}
[class*="st-key-scr_grid_shell"] iframe[title="st_aggrid.AgGrid.agGrid"] {
  position: relative;                   /* sits above fill + spinner once painted */
  z-index: 2;
}
.scr-ov-msg {
  font-size: 0.95rem;
  font-weight: 600;
  color: #212529;
  margin: 0;
}
.scr-ov-sub {
  font-size: 0.82rem;
  color: #6c757d;
  margin: 0;
  line-height: 1.4;
}
/* ── Criterion expandable details ── */
.criterion-details {
  margin-top: 8px;
  border-top: 1px solid #e9ecef;
}
.criterion-details summary {
  font-size: 0.76rem;
  color: #6c757d;
  cursor: pointer;
  padding: 6px 0 2px 0;
  user-select: none;
  font-weight: 500;
  list-style: none;
}
.criterion-details summary::-webkit-details-marker { display: none; }
.criterion-details summary::before {
  content: '\25B6';
  font-size: 0.6rem;
  margin-right: 6px;
  display: inline-block;
  transition: transform 0.15s ease;
}
.criterion-details[open] summary::before {
  transform: rotate(90deg);
}
.criterion-details-body {
  max-height: 140px;
  overflow-y: auto;
  padding: 6px 0 2px 0;
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.criterion-details-body::-webkit-scrollbar {
  width: 4px;
}
.criterion-details-body::-webkit-scrollbar-thumb {
  background: #ced4da;
  border-radius: 4px;
}
.criterion-pill {
  display: inline-block;
  background: #f0f2f5;
  color: #495057;
  font-size: 0.74rem;
  padding: 3px 10px;
  border-radius: 12px;
  border: 1px solid #dee2e6;
  white-space: nowrap;
}
.criterion-detail-kv {
  font-size: 0.78rem;
  color: #495057;
  padding: 2px 0;
}
.criterion-detail-kv strong {
  color: #343a40;
}
/* ── Status bar ── */
.screening-status {
  font-size: 0.82rem;
  color: #6c757d;
  padding: 6px 0;
}
.screening-status strong {
  color: #d62e2f;
}
/* ── Coming soon badge ── */
.coming-soon-badge {
  font-size: 0.7rem;
  background: #e9ecef;
  color: #6c757d;
  border-radius: 4px;
  padding: 1px 6px;
  margin-left: 6px;
  vertical-align: middle;
}
/* ── Results header ── */
.results-header {
  font-size: 0.85rem;
  color: #495057;
  margin-bottom: 8px;
  padding: 6px 0;
}
/* ── Excel download button (EVERY screening tab) ──
   Branded red-outline button, right-aligned to the results grid's right edge.

   These rules live in the page stylesheet, not next to the button, because
   _render_excel_js_download used to emit them itself with st.markdown. That works
   in Companies and People, whose button sits in a plain st.columns block — but the
   Key Devs button is rendered `with _dl_slot:` where `_dl_slot = _dl_col.empty()`,
   and an st.empty() holds exactly ONE element: the st.container that follows
   REPLACED the <style>, so Key Devs silently got an unstyled grey button while the
   other tabs got the red one. That is the "Excel button looks different" report.
   Emitting them once per page instead makes every tab identical by construction.

   st.container(key=...) renders as a COLUMN flex block, so the horizontal axis is
   align-items, not justify-content; Streamlit's own emotion class on the same
   element sets align-items:start, hence !important. */
div[class*="st-key-xlbtn-"] {
  display: flex;
  align-items: flex-end !important;
  justify-content: center;
  min-height: 52px;
}
div[class*="st-key-xlbtn-"] button {
  background: transparent; border: 1px solid #D62E2F; color: #D62E2F;
  border-radius: 4px; padding: 6px 10px; font-size: 13px; font-weight: 500;
  letter-spacing: 0.01em; white-space: nowrap; width: auto; min-height: 0;
  transition: background .15s, color .15s;
}
div[class*="st-key-xlbtn-"] button p { font-size: 13px; font-weight: 500; margin: 0; }
div[class*="st-key-xlbtn-"] button:hover { background: #D62E2F; color: #fff; }
div[class*="st-key-xlbtn-"] button:hover p { color: #fff; }
div[class*="st-key-xlbtn-"] button:active { opacity: .85; }
.screening-results-card {
  background: #ffffff;
  border: 1px solid #e3e6ea;
  border-radius: 10px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  padding: 16px 18px 12px 18px;
  margin-top: 8px;
}
.screening-results-title {
  font-size: 1.05rem;
  font-weight: 700;
  color: #2d2a29;
  margin: 0 0 4px 0;
}
.screening-results-meta {
  font-size: 0.8rem;
  color: #6c757d;
  margin: 0 0 12px 0;
}
.screening-results-meta strong {
  color: #d62e2f;
}
.screening-criteria-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
}
.screening-criteria-chip {
  background: #f8f9fa;
  border: 1px solid #dee2e6;
  border-radius: 16px;
  padding: 5px 12px;
  font-size: 0.76rem;
  color: #495057;
}
.screening-results-table-wrap {
  overflow-x: auto;
  max-width: 100%;
}
.screening-results-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
}
.screening-results-table thead th {
  position: sticky;
  top: 0;
  background: #f8f9fa;
  border-bottom: 2px solid #dee2e6;
  padding: 10px 12px;
  text-align: left;
  z-index: 2;
}
.screening-results-table thead th.num {
  text-align: right;
}
.screening-results-table thead th .col-sub {
  display: block;
  font-size: 0.68rem;
  font-weight: 400;
  color: #868e96;
  margin-top: 2px;
}
.screening-results-table tbody td {
  padding: 8px 12px;
  border-bottom: 1px solid #f0f2f5;
  vertical-align: top;
}
.screening-results-table tbody td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.screening-results-table tbody td.company-cell {
  position: sticky;
  left: 0;
  background: #fff;
  z-index: 1;
  min-width: 200px;
}
.screening-results-table tbody tr:hover td {
  background: #fafbfc;
}
.screening-results-table tbody tr:hover td.company-cell {
  background: #fafbfc;
}
.screening-results-table .company-name {
  font-weight: 600;
  color: #212529;
}
.screening-results-table .company-sub {
  font-size: 0.72rem;
  color: #868e96;
}
/* ── Form section label ── */
.form-section-label {
  font-size: 0.85rem;
  color: #495057;
  font-weight: 600;
  margin-bottom: 4px;
}
/* Remove excessive Streamlit padding in columns */
div[data-testid="column"] {
  padding-left: 6px !important;
  padding-right: 6px !important;
}

/* ── Coresight Button Styling ── */

/* Primary buttons — Coresight red fill */
button[data-testid="baseButton-primary"],
button[data-testid="stFormSubmitButton"][kind="primary"] {
  background-color: #d62e2f !important;
  color: #ffffff !important;
  border: 1px solid #d62e2f !important;
  border-radius: 6px !important;
  font-weight: 600 !important;
  transition: background-color 0.15s ease, box-shadow 0.15s ease !important;
}
button[data-testid="baseButton-primary"]:hover,
button[data-testid="stFormSubmitButton"][kind="primary"]:hover {
  background-color: #b52526 !important;
  border-color: #b52526 !important;
  box-shadow: 0 2px 6px rgba(214,46,47,0.30) !important;
}

/* Secondary buttons — white with Coresight red outline */
button[data-testid="baseButton-secondary"],
button[data-testid="stFormSubmitButton"][kind="secondary"] {
  background-color: #ffffff !important;
  color: #d62e2f !important;
  border: 1px solid #d62e2f !important;
  border-radius: 6px !important;
  font-weight: 500 !important;
  transition: background-color 0.15s ease !important;
}
button[data-testid="baseButton-secondary"]:hover,
button[data-testid="stFormSubmitButton"][kind="secondary"]:hover {
  background-color: #fff0f0 !important;
}

/* Default (no type) buttons — same as secondary */
button[data-testid="baseButton-borderless"],
button[data-testid="stBaseButton-borderless"] {
  background-color: #ffffff !important;
  color: #d62e2f !important;
  border: 1px solid #d62e2f !important;
  border-radius: 6px !important;
  font-weight: 500 !important;
}

/* Disabled buttons */
button[disabled] {
  background-color: #f8f9fa !important;
  color: #adb5bd !important;
  border-color: #dee2e6 !important;
  cursor: not-allowed !important;
}

/* ── Watchlist bar — compact single-row ── */
.watchlist-bar-title {
  font-size: 0.9rem;
  font-weight: 700;
  color: #212529;
  margin: 0;
  line-height: 38px;   /* vertically centres next to the selectbox */
}
.watchlist-active-label {
  font-size: 0.76rem;
  color: #d62e2f;
  font-weight: 600;
  margin: 2px 0 0 0;
}
/* "Edit / Manage" button — compact, grey */
.st-key-scr_wl_manage_btn button {
  background-color: #f0f0f0 !important;
  color: #212529 !important;
  border: 1px solid #dddddd !important;
  border-radius: 6px !important;
  font-weight: 600 !important;
  font-size: 0.78rem !important;
  height: auto !important;
  min-height: unset !important;
  padding: 4px 12px !important;
  white-space: nowrap !important;
}
.st-key-scr_wl_manage_btn button:hover {
  background-color: #e0e0e0 !important;
}
/* Watchlist selectbox in bar — tight padding */
.st-key-scr_wl_bar_select [data-baseweb="select"] {
  border-radius: 6px !important;
}
/* ── Dialog: Create/Edit Watchlist ── */
.dlg-section-header {
  font-size: 1rem;
  font-weight: 700;
  color: #212529;
  margin: 12px 0 8px 0;
}
.filter-grey-card {
  background: #f2f2f2;
  border-radius: 8px;
  padding: 16px;
  margin: 8px 0 12px 0;
}
.filter-col-label {
  font-size: 0.83rem;
  color: #495057;
  font-weight: 500;
  margin-bottom: 4px !important;
}
.filter-divider {
  width: 1px;
  background: #d0d0d0;
  align-self: stretch;
}
/* Multiselect tags → red pills */
.st-key-wl_add_co_ms span[data-baseweb="tag"],
.st-key-wl_add_sec_ms span[data-baseweb="tag"] {
  background-color: #d62e2f !important;
  border-color: #d62e2f !important;
  border-radius: 4px !important;
}
.st-key-wl_add_co_ms [data-baseweb="tag"] span,
.st-key-wl_add_sec_ms [data-baseweb="tag"] span {
  color: #ffffff !important;
}
.st-key-wl_add_co_ms [data-baseweb="tag"] [data-testid="delete"],
.st-key-wl_add_sec_ms [data-baseweb="tag"] [data-testid="delete"] {
  color: #ffffff !important;
}
/* Make dialog selector look clean */
.wl-selector-row { margin-bottom: 8px; }
.wl-create-link {
  color: #d62e2f;
  font-weight: 600;
  font-size: 0.95rem;
  cursor: pointer;
  text-decoration: none;
}
/* "Aggregate Financials +" label */
.agg-fin-label {
  font-size: 0.95rem;
  font-weight: 700;
  color: #212529;
  padding: 10px 0;
  border-top: 1px solid #e9ecef;
  margin-top: 8px;
}
/* ── Saved Screenings button — red, compact, same height as palette btns ── */
.st-key-scr_pal_browse_saved button {
  background-color: #d62e2f !important;
  color: #ffffff !important;
  border: 1px solid #d62e2f !important;
  font-size: 0.78rem !important;
  font-weight: 600 !important;
  height: auto !important;
  min-height: unset !important;
  padding: 4px 14px !important;
  white-space: nowrap !important;
}
.st-key-scr_pal_browse_saved button:hover {
  background-color: #b52526 !important;
  border-color: #b52526 !important;
}

/* ── Companies table in watchlist dialog ── */
.co-table-wrap {
  border: 1px solid #e9ecef;
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 12px;
}
/* Scrollable container for company rows (> 5 companies) */
.co-table-scroll-wrap {
  max-height: 280px;
  overflow-y: auto;
  border: 1px solid #e9ecef;
  border-radius: 0 0 6px 6px;
  border-top: none;
}
.co-table-header {
  background: #f8f9fa;
  padding: 7px 10px;
  font-size: 0.78rem;
  font-weight: 700;
  color: #6c757d;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid #e9ecef;
}

/* ── Delete row button — small red icon button ── */
[class*="st-key-wl_del_row"] button {
  background-color: transparent !important;
  color: #dc3545 !important;
  border: 1px solid #dc3545 !important;
  border-radius: 4px !important;
  padding: 1px 6px !important;
  height: 24px !important;
  min-height: 24px !important;
  font-size: 0.72rem !important;
  font-weight: 600 !important;
  line-height: 1 !important;
}
[class*="st-key-wl_del_row"] button:hover {
  background-color: #dc3545 !important;
  color: #ffffff !important;
}

/* ── Char counter ── */
.char-counter {
  font-size: 0.71rem;
  color: #6c757d;
  text-align: right;
  margin-top: -10px;
  margin-bottom: 6px;
}
.char-counter-warn { color: #dc3545; }

/* Access grant list inside dialogs */
.grant-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid #f0f0f0;
  font-size: 0.82rem;
}
.grant-badge {
  font-size: 0.7rem;
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 600;
}
.grant-badge-edit   { background: #fff3cd; color: #856404; }
.grant-badge-delete { background: #f8d7da; color: #721c24; }
.grant-badge-view   { background: #d1ecf1; color: #0c5460; }
/* Criteria card in saved criteria dialog */
.saved-criteria-card {
  background: #fff;
  border: 1px solid #e9ecef;
  border-left: 4px solid #d62e2f;
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 10px;
}
.saved-criteria-name {
  font-weight: 600;
  font-size: 0.92rem;
  color: #212529;
}
.saved-criteria-meta {
  font-size: 0.75rem;
  color: #6c757d;
  margin-top: 2px;
}
</style>
"""


# =============================================================================
# DIALOGS — Saved Criteria
# =============================================================================


def _render_criteria_detail_panel(cid: int, cr: dict, user_email: str,
                                  editable: bool = False):
    """Render detailed filter view inside a Saved Screenings card.

    Shows the *actual* items in each filter (e.g. the 13 industries, not just
    "13 selected").  When ``editable`` is True (owner), the user may:
      • remove individual items from industry / geography / keydevs filters
      • remove an entire filter
      • add new criteria (any of the 4 types)
      • rename / re-describe the saved screening
    When ``editable`` is False (non-owner), the panel is read-only.
    """
    saved_filters: list = cr.get("criteria_list", [])
    wk_key = f"_scr_edit_wk_{cid}"

    # ── working copy of filters for owner editing ──
    if editable and wk_key not in st.session_state:
        import copy
        st.session_state[wk_key] = copy.deepcopy(saved_filters)

    working = st.session_state.get(wk_key, saved_filters) if editable else saved_filters

    with st.container(border=True):
        # ── Name / Description edit row (owner only) ──
        if editable:
            new_name = st.text_input("Name", value=cr["name"],
                                     key=f"_scr_edn_{cid}", max_chars=100)
            new_desc = st.text_area("Description",
                                    value=cr.get("description") or "",
                                    key=f"_scr_edd_{cid}", height=50)

        st.markdown(f"**Saved Filters** ({len(working)})")

        if not working:
            st.info("No filters saved in this criteria.")

        # ── Render each filter in detail ──
        for fi, filt in enumerate(working):
            ftype = filt.get("type", "unknown")

            # ── header row with optional remove-filter button ──
            type_label = {
                "industry":     "Industry Classifications",
                "geography":    "Geographic Locations",
                "financial":    "Financial Information",
                "keydevs":      "Key Developments",
                "biz_segments": "Business Segments",
                "geo_segments": "Geographic Segments",
                "additional":   "Additional Data",
            }.get(ftype, ftype.upper())

            hdr_cols = st.columns([7, 1]) if editable else [st.container()]
            with hdr_cols[0]:
                st.markdown(
                    f'<div style="background:#e9ecef;border-left:3px solid #d62e2f;'
                    f'border-radius:4px;padding:6px 10px;margin:6px 0 2px 0;'
                    f'font-size:0.85rem;font-weight:600;">'
                    f'#{fi+1} &nbsp;·&nbsp; {type_label}</div>',
                    unsafe_allow_html=True,
                )
            if editable:
                with hdr_cols[1]:
                    if st.button("🗑️", key=f"_scr_rf_{cid}_{fi}",
                                 help="Remove this entire filter"):
                        working.pop(fi)
                        st.session_state[wk_key] = working
                        st.rerun()

            # ── detail rendering per type ──
            if ftype == "industry":
                _render_industry_detail(cid, fi, filt, working, editable, wk_key)
            elif ftype == "geography":
                _render_geography_detail(cid, fi, filt, working, editable, wk_key)
            elif ftype == "financial":
                _render_financial_detail(cid, fi, filt, editable)
            elif ftype == "keydevs":
                _render_keydevs_detail(cid, fi, filt, working, editable, wk_key)
            else:
                st.caption(filt.get("summary", "—"))

        # ── Add new criteria (owner only) ──
        if editable:
            st.markdown("")
            _render_add_new_criteria_inline(cid, working, wk_key)

        # ── Action buttons ──
        st.markdown("")
        if editable:
            bc1, bc2, bc3, _ = st.columns([1.2, 1.2, 1.5, 4.1])
            with bc1:
                if st.button("Save", key=f"_scr_esv_{cid}",
                             type="primary", width="stretch"):
                    final_name = st.session_state.get(f"_scr_edn_{cid}", cr["name"]).strip()
                    final_desc = st.session_state.get(f"_scr_edd_{cid}", cr.get("description") or "").strip()
                    if not final_name:
                        st.error("Name is required.")
                    elif not working:
                        st.error("At least one filter is required.")
                    else:
                        ok = svc_update_criteria(cid, user_email,
                                                 name=final_name,
                                                 description=final_desc,
                                                 criteria_list=working)
                        if ok:
                            _invalidate_criteria_cache()
                            st.session_state.pop(f"scr_dlg_editmode_{cid}", None)
                            st.session_state.pop(wk_key, None)
                            st.success(f"**{final_name}** updated successfully.")
                            st.rerun()
                        else:
                            st.error("Update failed.")
            with bc2:
                if st.button("Cancel", key=f"_scr_ecn_{cid}", width="stretch"):
                    st.session_state.pop(f"scr_dlg_editmode_{cid}", None)
                    st.session_state.pop(wk_key, None)
                    st.rerun()
            with bc3:
                if st.button("Load & Edit", key=f"_scr_ele_{cid}", width="stretch"):
                    st.session_state.scr_active_criteria = list(working)
                    st.session_state.scr_criterion_cache = {}
                    st.session_state.scr_working_df = None
                    st.session_state.scr_show_results = False
                    st.session_state.scr_active_form = None
                    st.session_state["_scr_editing_criteria_id"] = cid
                    _trigger_recompute()
                    st.session_state.pop(f"scr_dlg_editmode_{cid}", None)
                    st.session_state.pop(wk_key, None)
                    st.session_state["scr_saved_dlg_open"] = False
                    st.toast(f"Loaded **{cr['name']}** — modify then Save Current Criteria to update.")
                    st.rerun()
        else:
            if st.button("Close", key=f"_scr_evc_{cid}"):
                st.session_state[f"scr_dlg_viewmode_{cid}"] = False
                st.rerun()


# ── Detail renderers per criterion type ──────────────────────────────────────

def _render_industry_detail(cid, fi, filt, working, editable, wk_key):
    """Show individual industry names; owner can remove/add."""
    industries = filt.get("industries", [])
    if not industries:
        st.caption("No industries selected.")
        return

    # Show each industry as a chip/tag row
    for idx, ind in enumerate(industries):
        if editable:
            ic1, ic2 = st.columns([8, 1])
            with ic1:
                st.markdown(
                    f'<span style="display:inline-block;background:#f0f2f6;border:1px solid #ddd;'
                    f'border-radius:12px;padding:2px 10px;margin:1px 2px;font-size:0.8rem;">'
                    f'{ind}</span>',
                    unsafe_allow_html=True,
                )
            with ic2:
                if st.button("✕", key=f"_scr_ri_{cid}_{fi}_{idx}",
                             help=f"Remove {ind}"):
                    industries.pop(idx)
                    if industries:
                        filt["industries"] = industries
                        filt["summary"] = build_industry_summary(industries)
                    else:
                        working.pop(fi)
                    st.session_state[wk_key] = working
                    st.rerun()
        else:
            st.markdown(
                f'<span style="display:inline-block;background:#f0f2f6;border:1px solid #ddd;'
                f'border-radius:12px;padding:2px 10px;margin:1px 2px;font-size:0.8rem;">'
                f'{ind}</span>',
                unsafe_allow_html=True,
            )

    # Owner: add new industries
    if editable:
        all_industries = get_all_industries()
        available = [i for i in all_industries if i not in industries]
        if available:
            add_key = f"_scr_ai_{cid}_{fi}"
            new_inds = st.multiselect(
                "Add industries", options=available,
                key=add_key, placeholder="Select industries to add…",
            )
            if new_inds and st.button("Add", key=f"_scr_aib_{cid}_{fi}"):
                filt["industries"].extend(new_inds)
                filt["summary"] = build_industry_summary(filt["industries"])
                st.session_state[wk_key] = working
                st.rerun()


def _render_geography_detail(cid, fi, filt, working, editable, wk_key):
    """Show individual country names; owner can remove/add."""
    countries = filt.get("countries", [])
    if not countries:
        st.caption("No countries selected.")
        return

    for idx, country in enumerate(countries):
        if editable:
            gc1, gc2 = st.columns([8, 1])
            with gc1:
                st.markdown(
                    f'<span style="display:inline-block;background:#f0f2f6;border:1px solid #ddd;'
                    f'border-radius:12px;padding:2px 10px;margin:1px 2px;font-size:0.8rem;">'
                    f'{country}</span>',
                    unsafe_allow_html=True,
                )
            with gc2:
                if st.button("✕", key=f"_scr_rg_{cid}_{fi}_{idx}",
                             help=f"Remove {country}"):
                    countries.pop(idx)
                    if countries:
                        filt["countries"] = countries
                        filt["summary"] = build_geography_summary(countries)
                    else:
                        working.pop(fi)
                    st.session_state[wk_key] = working
                    st.rerun()
        else:
            st.markdown(
                f'<span style="display:inline-block;background:#f0f2f6;border:1px solid #ddd;'
                f'border-radius:12px;padding:2px 10px;margin:1px 2px;font-size:0.8rem;">'
                f'{country}</span>',
                unsafe_allow_html=True,
            )

    if editable:
        all_countries = get_all_countries()
        available = [c for c in all_countries if c not in countries]
        if available:
            add_key = f"_scr_ag_{cid}_{fi}"
            new_countries = st.multiselect(
                "Add countries", options=available,
                key=add_key, placeholder="Select countries to add…",
            )
            if new_countries and st.button("Add", key=f"_scr_agb_{cid}_{fi}"):
                filt["countries"].extend(new_countries)
                filt["summary"] = build_geography_summary(filt["countries"])
                st.session_state[wk_key] = working
                st.rerun()


def _render_financial_detail(cid, fi, filt, editable):
    """Show financial filter parameters (read-only display; owner can remove the whole filter)."""
    stmt = filt.get("statement", "—")
    metric = filt.get("metric_label", filt.get("metric_info", {}).get("label", "—"))
    operator = filt.get("operator", "—")
    val1 = filt.get("value1", "—")
    val2 = filt.get("value2")
    tf = filt.get("timeframe", "—")

    val_display = f"{val1}"
    if operator == "between" and val2 is not None:
        val_display = f"{val1} — {val2}"

    detail_html = (
        f'<div style="background:#f8f9fa;padding:6px 12px;border-radius:4px;'
        f'font-size:0.82rem;margin:2px 0 4px 0;">'
        f'<strong>Statement:</strong> {stmt}<br>'
        f'<strong>Metric:</strong> {metric}<br>'
        f'<strong>Condition:</strong> {operator} {val_display}<br>'
        f'<strong>Timeframe:</strong> {tf}'
        f'</div>'
    )
    st.markdown(detail_html, unsafe_allow_html=True)


def _keydevs_default_dates(prefill: Optional[dict]) -> tuple:
    """Default start/end for Key Developments date-range picker."""
    today = date.today()
    default_end = today
    default_start = today - timedelta(days=30)
    if prefill and prefill.get("date_filter_mode") == "date_range":
        try:
            if prefill.get("start_date"):
                default_start = date.fromisoformat(str(prefill["start_date"])[:10])
            if prefill.get("end_date"):
                default_end = date.fromisoformat(str(prefill["end_date"])[:10])
        except ValueError:
            pass
    return default_start, default_end


def _keydevs_mode_label_from_prefill(prefill: Optional[dict]) -> str:
    if prefill and prefill.get("date_filter_mode") == "date_range":
        return "Date range"
    return "Time frame"


def _keydevs_mode_from_session(key_prefix: str) -> str:
    label = st.session_state.get(f"{key_prefix}_date_mode", "Time frame")
    return "date_range" if label == "Date range" else "timeframe"


def _render_keydevs_period_radio(prefill: Optional[dict], key_prefix: str):
    """Period radio — must render outside st.form so toggling updates immediately."""
    mode_key = f"{key_prefix}_date_mode"
    if mode_key not in st.session_state:
        st.session_state[mode_key] = _keydevs_mode_label_from_prefill(prefill)
    st.radio(
        "Period",
        options=["Time frame", "Date range"],
        horizontal=True,
        key=mode_key,
    )


def _collect_keydevs_date_fields(prefill: Optional[dict], key_prefix: str) -> dict:
    """Timeframe selectbox or start/end date inputs (reads period mode from session)."""
    is_edit = bool(prefill)
    date_filter_mode = _keydevs_mode_from_session(key_prefix)
    timeframe_label = None
    days = None
    start_date_val = None
    end_date_val = None

    if date_filter_mode == "timeframe":
        tf_labels = list(KEYDEV_TIMEFRAMES.keys())
        default_tf_idx = (
            tf_labels.index(prefill["timeframe_label"])
            if is_edit
            and prefill.get("timeframe_label") in tf_labels
            and prefill.get("date_filter_mode", "timeframe") == "timeframe"
            else 1
        )
        timeframe_label = st.selectbox(
            "Timeframe",
            options=tf_labels,
            index=default_tf_idx,
            key=f"{key_prefix}_timeframe",
        )
        days = KEYDEV_TIMEFRAMES[timeframe_label]
    else:
        default_start, default_end = _keydevs_default_dates(prefill)
        dc1, dc2 = st.columns(2)
        with dc1:
            start_date_val = st.date_input(
                "Start date",
                value=default_start,
                key=f"{key_prefix}_start_date",
            )
        with dc2:
            end_date_val = st.date_input(
                "End date",
                value=default_end,
                key=f"{key_prefix}_end_date",
            )

    return {
        "date_filter_mode": date_filter_mode,
        "timeframe_label": timeframe_label,
        "days": days,
        "start_date": start_date_val.isoformat() if start_date_val else None,
        "end_date": end_date_val.isoformat() if end_date_val else None,
    }


def _render_keydevs_date_filter(prefill: Optional[dict], key_prefix: str):
    """Radio + inputs for contexts without st.form (e.g. inline add)."""
    _render_keydevs_period_radio(prefill, key_prefix)
    return _collect_keydevs_date_fields(prefill, key_prefix)


def _apply_keydevs_date_fields(criterion: dict, date_fields: dict) -> Optional[str]:
    """Merge date filter fields into criterion; return error message or None."""
    mode = date_fields["date_filter_mode"]
    criterion["date_filter_mode"] = mode

    if mode == "timeframe":
        criterion["timeframe_label"] = date_fields["timeframe_label"]
        criterion["days"] = date_fields["days"]
        criterion.pop("start_date", None)
        criterion.pop("end_date", None)
        return None

    start_s = date_fields.get("start_date")
    end_s = date_fields.get("end_date")
    if not start_s or not end_s:
        return "Please select both start and end dates."
    if start_s > end_s:
        return "Start date must be on or before end date."
    criterion["start_date"] = start_s
    criterion["end_date"] = end_s
    criterion["timeframe_label"] = f"{start_s} to {end_s}"
    criterion["days"] = None
    return None


def _keydevs_display_col(date_fields: dict) -> str:
    if date_fields["date_filter_mode"] == "date_range":
        return (
            f"Key Developments by Category - "
            f"[{date_fields['start_date']} to {date_fields['end_date']}]"
        )
    return f"Key Developments by Category - [{date_fields['timeframe_label']}]"


def _keydevs_summary_from_fields(
    category_labels: List[str], date_fields: dict,
) -> str:
    return build_keydevs_summary(
        category_labels,
        date_fields.get("timeframe_label") or "",
        date_filter_mode=date_fields["date_filter_mode"],
        start_date=date_fields.get("start_date"),
        end_date=date_fields.get("end_date"),
    )


def _render_keydevs_detail(cid, fi, filt, working, editable, wk_key):
    """Show key-dev categories; owner can remove individual categories / add new."""
    cat_labels = filt.get("category_labels", [])
    categories = filt.get("categories", [])
    period_label = keydevs_period_display_label(filt) or "—"

    if filt.get("date_filter_mode") == "date_range":
        period_html = f"<strong>Date range:</strong> {period_label}"
    else:
        days = filt.get("days", "—")
        period_html = f"<strong>Timeframe:</strong> {period_label} ({days} days)"

    st.markdown(
        f'<div style="font-size:0.82rem;color:#495057;margin:2px 0;">'
        f'{period_html}</div>',
        unsafe_allow_html=True,
    )

    for idx, label in enumerate(cat_labels):
        if editable:
            kc1, kc2 = st.columns([8, 1])
            with kc1:
                st.markdown(
                    f'<span style="display:inline-block;background:#f0f2f6;border:1px solid #ddd;'
                    f'border-radius:12px;padding:2px 10px;margin:1px 2px;font-size:0.8rem;">'
                    f'{label}</span>',
                    unsafe_allow_html=True,
                )
            with kc2:
                if st.button("✕", key=f"_scr_rk_{cid}_{fi}_{idx}",
                             help=f"Remove {label}"):
                    cat_labels.pop(idx)
                    if idx < len(categories):
                        categories.pop(idx)
                    if cat_labels:
                        filt["category_labels"] = cat_labels
                        filt["categories"] = categories
                        filt["summary"] = build_keydevs_summary(
                            cat_labels,
                            filt.get("timeframe_label", ""),
                            date_filter_mode=filt.get("date_filter_mode", "timeframe"),
                            start_date=filt.get("start_date"),
                            end_date=filt.get("end_date"),
                        )
                    else:
                        working.pop(fi)
                    st.session_state[wk_key] = working
                    st.rerun()
        else:
            st.markdown(
                f'<span style="display:inline-block;background:#f0f2f6;border:1px solid #ddd;'
                f'border-radius:12px;padding:2px 10px;margin:1px 2px;font-size:0.8rem;">'
                f'{label}</span>',
                unsafe_allow_html=True,
            )

    if editable:
        all_cat_labels = list(KEYDEV_CATEGORIES_ALL.keys())
        available = [c for c in all_cat_labels if c not in cat_labels]
        if available:
            add_key = f"_scr_ak_{cid}_{fi}"
            new_cats = st.multiselect(
                "Add categories", options=available,
                key=add_key, placeholder="Select categories to add…",
            )
            if new_cats and st.button("Add", key=f"_scr_akb_{cid}_{fi}"):
                for lbl in new_cats:
                    filt["category_labels"].append(lbl)
                    filt["categories"].append(KEYDEV_CATEGORIES_ALL[lbl])
                filt["summary"] = build_keydevs_summary(
                    filt["category_labels"],
                    filt.get("timeframe_label", ""),
                    date_filter_mode=filt.get("date_filter_mode", "timeframe"),
                    start_date=filt.get("start_date"),
                    end_date=filt.get("end_date"),
                )
                st.session_state[wk_key] = working
                st.rerun()


# ── Add New Criteria inline (owner edit panel) ──────────────────────────────

def _render_add_new_criteria_inline(cid, working, wk_key):
    """Let owner add a brand-new criteria type from within the detail panel."""
    add_type_key = f"_scr_addtype_{cid}"
    show_form_key = f"_scr_addform_{cid}"

    st.markdown("---")
    st.markdown("**➕ Add New Criteria**")
    type_options = ["Industry", "Geography", "Financial", "Key Developments"]
    ac1, ac2 = st.columns([3, 1])
    with ac1:
        sel_type = st.selectbox(
            "Criteria type", options=type_options,
            key=add_type_key, label_visibility="collapsed",
        )
    with ac2:
        if st.button("Add", key=f"_scr_addbtn_{cid}", width="stretch"):
            st.session_state[show_form_key] = sel_type
            st.rerun()

    active_form = st.session_state.get(show_form_key)
    if active_form == "Industry":
        _inline_add_industry(cid, working, wk_key, show_form_key)
    elif active_form == "Geography":
        _inline_add_geography(cid, working, wk_key, show_form_key)
    elif active_form == "Financial":
        _inline_add_financial(cid, working, wk_key, show_form_key)
    elif active_form == "Key Developments":
        _inline_add_keydevs(cid, working, wk_key, show_form_key)


def _inline_add_industry(cid, working, wk_key, show_form_key):
    """Inline form to add a new Industry criteria."""
    all_industries = get_all_industries()
    # Exclude already-selected industries across all industry filters
    existing = set()
    for f in working:
        if f.get("type") == "industry":
            existing.update(f.get("industries", []))
    available = [i for i in all_industries if i not in existing]

    if not available:
        st.info("All industries are already included in existing filters.")
        return

    with st.container(border=True):
        st.markdown("**New Industry Filter**")
        selected = st.multiselect(
            "Select industries", options=available,
            key=f"_scr_ni_{cid}", placeholder="Select industries…",
        )
        fc1, fc2 = st.columns([1, 1])
        with fc1:
            if st.button("Add Filter", key=f"_scr_nib_{cid}",
                         type="primary", width="stretch"):
                if not selected:
                    st.error("Select at least one industry.")
                else:
                    new_crit = {
                        "type": "industry",
                        "industries": selected,
                        "summary": build_industry_summary(selected),
                    }
                    working.append(new_crit)
                    st.session_state[wk_key] = working
                    st.session_state.pop(show_form_key, None)
                    st.rerun()
        with fc2:
            if st.button("Cancel", key=f"_scr_nic_{cid}", width="stretch"):
                st.session_state.pop(show_form_key, None)
                st.rerun()


def _inline_add_geography(cid, working, wk_key, show_form_key):
    """Inline form to add a new Geography criteria."""
    all_countries = get_all_countries()
    existing = set()
    for f in working:
        if f.get("type") == "geography":
            existing.update(f.get("countries", []))
    available = [c for c in all_countries if c not in existing]

    if not available:
        st.info("All countries are already included in existing filters.")
        return

    with st.container(border=True):
        st.markdown("**New Geography Filter**")
        selected = st.multiselect(
            "Select countries", options=available,
            key=f"_scr_ng_{cid}", placeholder="Select countries…",
        )
        fc1, fc2 = st.columns([1, 1])
        with fc1:
            if st.button("Add Filter", key=f"_scr_ngb_{cid}",
                         type="primary", width="stretch"):
                if not selected:
                    st.error("Select at least one country.")
                else:
                    new_crit = {
                        "type": "geography",
                        "countries": selected,
                        "summary": build_geography_summary(selected),
                    }
                    working.append(new_crit)
                    st.session_state[wk_key] = working
                    st.session_state.pop(show_form_key, None)
                    st.rerun()
        with fc2:
            if st.button("Cancel", key=f"_scr_ngc_{cid}", width="stretch"):
                st.session_state.pop(show_form_key, None)
                st.rerun()


def _inline_add_financial(cid, working, wk_key, show_form_key):
    """Inline form to add a new Financial criteria."""
    with st.container(border=True):
        st.markdown("**New Financial Filter**")

        stmt_options = list(STATEMENT_CONFIG.keys())
        stmt = st.selectbox("Statement Type", options=stmt_options,
                            key=f"_scr_nfs_{cid}")
        metrics = get_metric_labels(stmt)
        metric_label = st.selectbox("Metric", options=metrics,
                                    key=f"_scr_nfm_{cid}")
        op_options = list(OPERATORS.keys())
        operator = st.selectbox("Operator", options=op_options,
                                key=f"_scr_nfo_{cid}")
        vc1, vc2 = st.columns(2)
        with vc1:
            val1 = st.number_input("Value", key=f"_scr_nfv1_{cid}",
                                   value=0.0, format="%.2f")
        with vc2:
            val2 = None
            if operator == "between":
                val2 = st.number_input("Value 2", key=f"_scr_nfv2_{cid}",
                                       value=0.0, format="%.2f")

        period_options = list(PERIOD_TYPES.keys())
        period_type = st.selectbox("Period Type", options=period_options,
                                   key=f"_scr_nfp_{cid}")

        quarter_val = None
        if period_type == "Quarterly":
            quarter_val = st.selectbox("Quarter", options=QUARTERS,
                                       key=f"_scr_nfq_{cid}")

        year_options = list(SCREENING_YEARS.keys())
        year_label = st.selectbox("Year", options=year_options,
                                  key=f"_scr_nfy_{cid}")

        fc1, fc2 = st.columns([1, 1])
        with fc1:
            if st.button("Add Filter", key=f"_scr_nfb_{cid}",
                         type="primary", width="stretch"):
                metric_info = get_metric_info(stmt, metric_label)
                timeframe_parts = []
                if period_type == "Quarterly" and quarter_val:
                    timeframe_parts.append(quarter_val)
                else:
                    timeframe_parts.append(period_type[:2])
                timeframe_parts.append(year_label)
                timeframe = " ".join(timeframe_parts)

                display_col = f"{metric_label} (Millions) [{timeframe}]"

                new_crit = {
                    "type": "financial",
                    "statement": stmt,
                    "metric_info": metric_info,
                    "metric_label": metric_label,
                    "operator": operator,
                    "value1": val1,
                    "value2": val2,
                    "timeframe": timeframe,
                    "period_type": period_type,
                    "quarter": quarter_val,
                    "year": year_label,
                    "display_col": display_col,
                    "summary": build_financial_summary(
                        stmt, metric_label, operator, val1, val2, timeframe),
                }
                working.append(new_crit)
                st.session_state[wk_key] = working
                st.session_state.pop(show_form_key, None)
                st.rerun()
        with fc2:
            if st.button("Cancel", key=f"_scr_nfc_{cid}", width="stretch"):
                st.session_state.pop(show_form_key, None)
                st.rerun()


def _inline_add_keydevs(cid, working, wk_key, show_form_key):
    """Inline form to add a new Key Developments criteria."""
    with st.container(border=True):
        st.markdown("**New Key Developments Filter**")
        cat_labels = list(KEYDEV_CATEGORIES_ALL.keys())
        existing = set()
        for f in working:
            if f.get("type") == "keydevs":
                existing.update(f.get("category_labels", []))
        available = [c for c in cat_labels if c not in existing]

        if not available:
            st.info("All key dev categories are already included.")
            return

        selected_labels = st.multiselect(
            "Select Categories", options=available,
            key=f"_scr_nk_{cid}", placeholder="Select categories…",
        )
        date_fields = _render_keydevs_date_filter(None, f"_scr_nk_{cid}")

        fc1, fc2 = st.columns([1, 1])
        with fc1:
            if st.button("Add Filter", key=f"_scr_nkb_{cid}",
                         type="primary", width="stretch"):
                if not selected_labels:
                    st.error("Select at least one category.")
                else:
                    cat_keys = [KEYDEV_CATEGORIES_ALL[lb] for lb in selected_labels]
                    new_crit = {
                        "type": "keydevs",
                        "categories": cat_keys,
                        "category_labels": selected_labels,
                        "show_headline": True,   # always show event details
                    }
                    err = _apply_keydevs_date_fields(new_crit, date_fields)
                    if err:
                        st.error(err)
                        return
                    display_col = _keydevs_display_col(date_fields)
                    new_crit["summary"] = _keydevs_summary_from_fields(
                        selected_labels, date_fields,
                    )
                    # Always attach a results column for a key-dev criterion so the
                    # grid never silently drops it ("nothing is coming"). It carries
                    # the latest matched events (date · type · headline) — see
                    # apply_keydevs_criterion.
                    new_crit["display_col"] = display_col
                    working.append(new_crit)
                    st.session_state[wk_key] = working
                    st.session_state.pop(show_form_key, None)
                    st.rerun()
        with fc2:
            if st.button("Cancel", key=f"_scr_nkc_{cid}", width="stretch"):
                st.session_state.pop(show_form_key, None)
                st.rerun()


def _on_saved_dialog_dismiss():
    """Clear the open-flag on built-in X / Escape / click-outside dismissal —
    same fix as the watchlist dialog: prevents the modal from re-opening on the
    next flag-preserving interaction. See _on_watchlist_dialog_dismiss."""
    st.session_state["scr_saved_dlg_open"] = False


@st.dialog("Saved Screenings", width="large", on_dismiss=_on_saved_dialog_dismiss)
def _dialog_browse_saved_criteria():
    """Modal dialog: view, load, edit, delete saved criteria + access management."""
    # NOTE: Do NOT set scr_saved_dlg_open=True here. The flag is already True
    # from the triggering button click. Setting it here prevents proper cleanup
    # when the user dismisses via the X button (Streamlit's built-in close).

    user_email = _current_user_email()
    if not user_email:
        st.error("Not authenticated.")
        return

    criteria_list = _cached_user_criteria(user_email)

    if not criteria_list:
        st.info("No saved criteria yet. Use **Save Current Criteria** to save your first set.")
        if st.button("Close", key="scr_dlg_browse_close"):
            st.session_state["scr_saved_dlg_open"] = False
            st.rerun()
        return

    # ── Iterate over saved criteria cards ──
    for cr in criteria_list:
        cid        = cr["id"]
        is_owner   = bool(cr.get("is_owner"))
        can_edit   = bool(cr.get("can_edit"))
        can_delete = bool(cr.get("can_delete"))
        creator    = cr["created_by"]
        updated    = str(cr.get("updated_at", ""))[:10]

        with st.container():
            st.markdown(
                f"""
                <div class="saved-criteria-card">
                  <div class="saved-criteria-name">{cr['name']}</div>
                  <div class="saved-criteria-meta">
                    {"Owner" if is_owner else f"Created by {creator}"} &nbsp;·&nbsp;
                    {len(cr.get('criteria_list', []))} filter(s) &nbsp;·&nbsp;
                    Last updated {updated}
                  </div>
                  {"<div style='font-size:0.78rem;color:#495057;margin-top:4px;'>"
                   + (cr.get('description') or '') + "</div>"
                   if cr.get('description') else ""}
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Action buttons (Load / Edit name+desc / Delete)
            btn_cols = st.columns([1.2, 1.2, 1.2, 4.4])
            with btn_cols[0]:
                if st.button("Load", key=f"scr_dlg_load_{cid}", type="primary",
                             width="stretch", help="Apply this criteria set to the screening"):
                    # Apply: replace active criteria and recompute
                    st.session_state.scr_active_criteria  = list(cr["criteria_list"])
                    st.session_state.scr_criterion_cache  = {}
                    st.session_state.scr_working_df       = None
                    st.session_state.scr_show_results     = False
                    st.session_state.scr_active_form      = None
                    _trigger_recompute()
                    st.session_state["scr_saved_dlg_open"] = False
                    st.rerun()

            with btn_cols[1]:
                edit_key = f"scr_dlg_editmode_{cid}"
                if can_edit:
                    if st.button("Edit", key=f"scr_dlg_editbtn_{cid}", width="stretch",
                                 help="Edit name, description and criteria"):
                        st.session_state[edit_key] = not st.session_state.get(edit_key, False)
                        st.rerun()
                else:
                    # Non-owners get a View button to inspect filters
                    view_key = f"scr_dlg_viewmode_{cid}"
                    if st.button("View", key=f"scr_dlg_viewbtn_{cid}", width="stretch",
                                 help="View saved filters (read-only)"):
                        st.session_state[view_key] = not st.session_state.get(view_key, False)
                        st.rerun()

            with btn_cols[2]:
                if can_delete:
                    if st.button("Delete", key=f"scr_dlg_del_{cid}", width="stretch",
                                 type="secondary", help="Delete this saved criteria"):
                        st.session_state[f"scr_dlg_confirm_del_{cid}"] = True
                        st.rerun()

            # ── Detailed edit / view panel (owner=edit, non-owner=read-only) ──
            _show_edit = st.session_state.get(f"scr_dlg_editmode_{cid}")
            _show_view = (not can_edit) and st.session_state.get(f"scr_dlg_viewmode_{cid}")
            if _show_edit or _show_view:
                _render_criteria_detail_panel(cid, cr, user_email,
                                             editable=_show_edit)

            # Delete confirmation
            if st.session_state.get(f"scr_dlg_confirm_del_{cid}"):
                st.warning(f"Delete **{cr['name']}**? This cannot be undone.")
                c1, c2, _ = st.columns([1, 1, 5])
                with c1:
                    if st.button("Confirm", key=f"scr_dlg_cfm_{cid}",
                                 type="primary", width="stretch"):
                        ok = svc_delete_criteria(cid, user_email)
                        st.session_state.pop(f"scr_dlg_confirm_del_{cid}", None)
                        if ok:
                            _invalidate_criteria_cache()
                            st.rerun()
                        else:
                            st.error("Delete failed — check permissions.")
                with c2:
                    if st.button("Cancel", key=f"scr_dlg_cfm_cancel_{cid}", width="stretch"):
                        st.session_state.pop(f"scr_dlg_confirm_del_{cid}", None)
                        st.rerun()

            # Access management (owner only) — temporarily hidden
            if False and is_owner:  # re-enable when access management UI is finalized
                with st.expander(f"Manage Access — {cr['name']}", expanded=False):
                    grants = svc_get_criteria_grants(cid, user_email)
                    if grants:
                        for g in grants:
                            perms = []
                            if g["can_edit"]:   perms.append("Edit")
                            if g["can_delete"]: perms.append("Delete")
                            perm_str = ", ".join(perms) if perms else "View only"
                            col_email, col_perm, col_rev = st.columns([3, 2, 1])
                            with col_email:
                                st.markdown(f"**{g['user_email']}**")
                            with col_perm:
                                st.caption(perm_str)
                            with col_rev:
                                if st.button("Revoke", key=f"scr_rev_{cid}_{g['user_email']}",
                                             width="stretch"):
                                    svc_revoke_criteria_access(cid, g["user_email"], user_email)
                                    _invalidate_criteria_cache()
                                    st.rerun()
                    else:
                        st.caption("No shared access yet.")

                    portal_users = _cached_portal_users(user_email)
                    user_labels  = [u["label"] for u in portal_users]
                    already_granted = {g["user_email"] for g in grants}
                    avail_users = [u for u in portal_users if u["email"] not in already_granted]
                    avail_labels = [u["label"] for u in avail_users]

                    if avail_labels:
                        with st.form(f"scr_grant_form_{cid}"):
                            st.caption("Select one or more team members:")
                            sel_labels = st.multiselect(
                                "Grant access to",
                                avail_labels,
                                key=f"scr_ge_{cid}",
                                placeholder="Select people…",
                            )
                            gc2, gc3, gc4 = st.columns([1, 1, 2])
                            with gc2:
                                allow_edit = st.checkbox("Can Edit", key=f"scr_ge_edit_{cid}")
                            with gc3:
                                allow_del  = st.checkbox("Can Delete", key=f"scr_ge_del_{cid}")
                            with gc4:
                                if st.form_submit_button("Grant Access", type="primary",
                                                         width="stretch",
                                                         disabled=not sel_labels):
                                    sel_users = [u for u in avail_users
                                                 if u["label"] in sel_labels]
                                    failed = []
                                    for sel_user in sel_users:
                                        ok = svc_grant_criteria_access(
                                            cid, sel_user["email"],
                                            user_email, allow_edit, allow_del
                                        )
                                        if ok:
                                            pass
                                        else:
                                            failed.append(sel_user["email"])
                                    _invalidate_criteria_cache()
                                    if failed:
                                        st.error(f"Grant failed for: {', '.join(failed)}")
                                    st.rerun()
                    else:
                        st.caption("All portal users already have access.")

        st.divider()

    if st.button("Close", key="scr_dlg_browse_close_bottom"):
        st.session_state["scr_saved_dlg_open"] = False
        st.rerun()


@st.dialog("Save Current Criteria", width="small")
def _dialog_save_criteria():
    """Modal dialog: name + description → persist current criteria to DB."""
    user_email = _current_user_email()
    criteria   = st.session_state.get("scr_active_criteria", [])

    if not criteria:
        st.warning("No criteria to save. Add at least one filter first.")
        if st.button("Close", key="scr_dlg_save_close"):
            st.rerun()
        return

    # Check if we're editing an existing saved criteria (from Load & Edit)
    editing_id = st.session_state.get("_scr_editing_criteria_id")
    editing_name = ""
    if editing_id:
        existing = _cached_user_criteria(user_email)
        for ec in existing:
            if ec["id"] == editing_id:
                editing_name = ec["name"]
                break

    if editing_id and editing_name:
        st.info(f"Updating **{editing_name}** with **{len(criteria)} filter(s)**.")
    else:
        st.markdown(f"Saving **{len(criteria)} filter(s)** as a named set.")

    with st.form("scr_save_criteria_form"):
        name = st.text_input("Name *", max_chars=100,
                             value=editing_name if editing_id else "",
                             placeholder="e.g. Large-Cap US Retailers",
                             key="scr_save_name")
        desc = st.text_area("Description (optional)", height=70,
                            placeholder="Brief note about this criteria set",
                            key="scr_save_desc")

        if editing_id and editing_name:
            c1, c2, c3, _ = st.columns([1.5, 1.5, 1.5, 3.5])
            with c1:
                update_btn = st.form_submit_button("Update", type="primary",
                                                    width="stretch")
            with c2:
                save_new = st.form_submit_button("Save New", width="stretch")
            with c3:
                cancelled = st.form_submit_button("Cancel", width="stretch")
            submitted = False  # not used in editing mode
        else:
            update_btn = False
            save_new = False
            c1, c2, _ = st.columns([1.5, 1.5, 5])
            with c1:
                submitted = st.form_submit_button("Save", type="primary", width="stretch")
            with c2:
                cancelled = st.form_submit_button("Cancel", width="stretch")

        if cancelled:
            st.session_state.pop("_scr_editing_criteria_id", None)
            st.rerun()

        # Update existing criteria
        if update_btn and editing_id:
            if not name.strip():
                st.error("Name is required.")
            else:
                ok = svc_update_criteria(
                    editing_id, user_email,
                    name=name.strip(),
                    description=desc.strip(),
                    criteria_list=criteria,
                )
                if ok:
                    _invalidate_criteria_cache()
                    st.session_state.pop("_scr_editing_criteria_id", None)
                    st.success(f"**{name.strip()}** updated successfully.")
                    st.rerun()
                else:
                    st.error("Update failed. Please try again.")

        # Save as new (either the primary Save or "Save New" when editing)
        do_save_new = save_new if editing_id else submitted
        if do_save_new:
            if not name.strip():
                st.error("Name is required.")
            else:
                new_id = svc_save_criteria(
                    name=name.strip(),
                    criteria_list=criteria,
                    user_email=user_email,
                    description=desc.strip(),
                )
                if new_id:
                    _invalidate_criteria_cache()
                    st.session_state.pop("_scr_editing_criteria_id", None)
                    st.success(f"Criteria saved as **{name.strip()}**.")
                    st.rerun()
                else:
                    st.error("Save failed. Please try again.")


# =============================================================================
# DIALOGS — Watchlist Manager
# =============================================================================

def _on_watchlist_dialog_dismiss():
    """Clear the open-flag when the dialog is dismissed via the built-in
    X / Escape / click-outside gestures.

    Without this, Streamlit's default on_dismiss='ignore' leaves wl_dlg_open
    True after a built-in dismiss (no rerun fires), so the NEXT interaction
    that does not itself reset the flag re-opens the dialog — the "popup keeps
    reappearing" bug. The callback resets the gating state so dismissal sticks.
    """
    st.session_state["wl_dlg_open"] = False
    st.session_state.pop("wl_dlg_mode", None)


@st.dialog("Create/Edit Watchlist", width="large", on_dismiss=_on_watchlist_dialog_dismiss)
def _dialog_watchlist_manager():
    """Watchlist manager — Create mode or Edit mode.

    The wl_dlg_open flag is set to True by the triggering button and stays
    True across reruns. Only Cancel / Close / after-delete set it to False.
    Do NOT unconditionally set it to True here — that prevents proper cleanup
    when the user dismisses via the X button.
    """

    user_email = _current_user_email()
    if not user_email:
        st.error("Not authenticated.")
        return

    # ── Load company universe (cached) ──
    try:
        base_df = get_base_company_universe()
        all_companies = base_df.to_dict("records")
    except Exception:
        all_companies = []
    all_sectors = sorted({c.get("sector", "") for c in all_companies if c.get("sector")})

    mode = st.session_state.get("wl_dlg_mode", "edit")

    # ═══════════════════════════════════════════════════════════════════
    # CREATE MODE  — shown when "Create New +" is clicked from the bar
    # ═══════════════════════════════════════════════════════════════════
    if mode == "create":
        st.markdown("#### New Watchlist")

        nm = st.text_input(
            "Name *",
            max_chars=100,
            placeholder="e.g. Client List 1",
            key="wl_create_name",
        )
        _nm_len = len(st.session_state.get("wl_create_name", "") or "")
        _nm_cls = "char-counter-warn" if _nm_len >= 90 else ""
        st.markdown(
            f'<p class="char-counter {_nm_cls}">{_nm_len}/100 characters</p>',
            unsafe_allow_html=True,
        )

        desc_new = st.text_input(
            "List Description",
            placeholder="Optional description",
            key="wl_create_desc",
        )

        # ── Grey card: add companies / sectors (optional at create time) ──
        st.markdown(
            '<div class="filter-grey-card">'
            '<div class="dlg-section-header">Add Companies (optional)</div>',
            unsafe_allow_html=True,
        )
        left_col, right_col = st.columns(2)
        co_options_all = [f"{c['company_name']} ({c['ticker']})" for c in all_companies]

        with left_col:
            st.markdown('<p class="filter-col-label">Search Companies</p>',
                        unsafe_allow_html=True)
            sel_cos_new = st.multiselect(
                "Companies",
                co_options_all,
                key="wl_create_co_ms",
                placeholder="eg., Albertsons",
                label_visibility="collapsed",
            )

        with right_col:
            st.markdown('<p class="filter-col-label">Search Sectors</p>',
                        unsafe_allow_html=True)
            sel_sec_new = st.multiselect(
                "Sectors",
                all_sectors,
                key="wl_create_sec_ms",
                placeholder="eg., Grocery",
                label_visibility="collapsed",
            )
        st.markdown('</div>', unsafe_allow_html=True)

        # Red pills for create-mode multiselects
        st.markdown("""<style>
            .st-key-wl_create_co_ms span[data-baseweb="tag"],
            .st-key-wl_create_sec_ms span[data-baseweb="tag"] {
                background-color: #d62e2f !important;
                border-color: #d62e2f !important;
                border-radius: 4px !important;
            }
            .st-key-wl_create_co_ms [data-baseweb="tag"] span,
            .st-key-wl_create_sec_ms [data-baseweb="tag"] span,
            .st-key-wl_create_co_ms [data-baseweb="tag"] button,
            .st-key-wl_create_sec_ms [data-baseweb="tag"] button {
                color: #ffffff !important;
            }
        </style>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        btn_l, btn_r, _ = st.columns([1.5, 2, 6.5])

        with btn_l:
            if st.button("Cancel", key="wl_create_cancel", width="stretch"):
                st.session_state["wl_dlg_open"] = False
                st.session_state.pop("wl_dlg_mode", None)
                st.rerun()

        with btn_r:
            if st.button("Create Watchlist", key="wl_create_submit",
                         type="primary", width="stretch"):
                if not nm.strip():
                    st.error("Name is required.")
                else:
                    new_id = wl_create(
                        nm.strip(), user_email,
                        description=desc_new.strip() if desc_new else "",
                    )
                    if new_id:
                        if sel_cos_new:
                            # Exact map by full "Name (TICKER)" display string so a
                            # shared ticker (TSCO=Tractor Supply/Tesco) doesn't add
                            # the wrong company or both.
                            _opt_map_new = {
                                f"{c['company_name']} ({c['ticker']})": c
                                for c in all_companies
                            }
                            wl_add_companies(new_id,
                                             [_opt_map_new[s] for s in sel_cos_new
                                              if s in _opt_map_new],
                                             user_email)
                        if sel_sec_new:
                            wl_add_companies(new_id,
                                             [c for c in all_companies
                                              if c.get("sector") in sel_sec_new],
                                             user_email)
                        _invalidate_watchlist_cache()
                        # Analytics: watchlist created (discrete click, fires once).
                        try:
                            from utils.server_logger import track_action
                            track_action("watchlist_create", page="screening",
                                         companies=len(sel_cos_new or []),
                                         sectors=len(sel_sec_new or []))
                        except Exception:
                            pass
                        # Auto-select the new watchlist on the main page
                        st.session_state.scr_active_watchlist_id   = new_id
                        st.session_state.scr_active_watchlist_name = nm.strip()
                        _set_active_watchlist_scope(new_id)
                        st.session_state.scr_criterion_cache       = {}
                        st.session_state.scr_working_df            = None
                        st.session_state.scr_show_results          = False
                        # Also sync bar selectbox
                        # Switch to edit mode showing the new watchlist
                        st.session_state["wl_dlg_mode"]        = "edit"
                        st.session_state["_wl_dlg_preselect"]  = nm.strip()
                        st.session_state.pop("wl_dlg_select", None)
                        # Clean up create-mode keys
                        for _k in ("wl_create_name", "wl_create_desc",
                                   "wl_create_co_ms", "wl_create_sec_ms"):
                            st.session_state.pop(_k, None)
                        _queue_toast(f"'{nm.strip()}' created and selected!", icon="✅")
                        st.rerun()
                    else:
                        st.error("Failed to create watchlist. Please try again.")
        return

    # ═══════════════════════════════════════════════════════════════════
    # EDIT MODE  — dropdown + description + companies + save/delete
    # ═══════════════════════════════════════════════════════════════════
    watchlists = _cached_user_watchlists(user_email)
    wl_names   = [w["name"] for w in watchlists]

    if not watchlists:
        st.info("No watchlists yet. Click 'Create New +' to add your first watchlist.")
        if st.button("Create New  +", key="wl_edit_create_first",
                     type="primary", width="stretch"):
            st.session_state["wl_dlg_mode"] = "create"
            st.rerun()
        return

    # ── ROW 1: Watchlist selector  +  "Create New +" ──
    sel_col, create_col = st.columns([4, 1.5])

    with sel_col:
        # Use a separate key _wl_dlg_preselect (not the widget key) to avoid
        # the "set via Session State API + default value" Streamlit warning.
        _preselect = st.session_state.get("_wl_dlg_preselect")
        _idx = wl_names.index(_preselect) if _preselect and _preselect in wl_names else 0
        selected_name = st.selectbox(
            "Watchlist",
            options=wl_names,
            index=_idx,
            key="wl_dlg_select",
            label_visibility="collapsed",
        )
        selected_wl = watchlists[wl_names.index(selected_name)]

    with create_col:
        if st.button("Create New  +", key="wl_dlg_create_link",
                     type="secondary", width="stretch"):
            st.session_state["wl_dlg_mode"] = "create"
            st.rerun()

    wl_id      = selected_wl["id"]
    can_edit   = bool(selected_wl.get("can_edit"))
    can_delete = bool(selected_wl.get("can_delete"))

    # ── List Description ──
    if can_edit:
        desc_val = st.text_input(
            "List Description",
            value=selected_wl.get("description") or "",
            placeholder="Lorem ipsum dolor sit amet",
            key=f"wl_dlg_desc_{wl_id}",
        )
    else:
        desc_val = selected_wl.get("description") or ""
        if desc_val:
            st.caption(desc_val)

    # ── Grey card: Filter by Company/Sector ──
    current_cos     = wl_get_companies(wl_id)
    current_tickers = {c["ticker"] for c in current_cos}

    st.markdown(
        '<div class="filter-grey-card">'
        '<div class="dlg-section-header">Filter by Company/Sector  —</div>',
        unsafe_allow_html=True,
    )

    left_col, right_col = st.columns(2)
    # Exclude already-added companies by composite identity (ticker + name), NOT
    # ticker alone — JD / LULU / TSCO are each shared by two different companies
    # on different exchanges (e.g. TSCO = Tractor Supply AND Tesco PLC). Filtering
    # on ticker alone wrongly hides the sibling company from the add dropdown.
    current_keys = {(c["ticker"], (c.get("company_name") or "")) for c in current_cos}
    available  = [
        c for c in all_companies
        if (c["ticker"], (c.get("company_name") or "")) not in current_keys
    ]
    co_options = [f"{c['company_name']} ({c['ticker']})" for c in available]

    with left_col:
        st.markdown('<p class="filter-col-label">Search Companies</p>',
                    unsafe_allow_html=True)
        sel_cos = st.multiselect(
            "Companies",
            co_options,
            key=f"wl_add_co_ms_{wl_id}",
            placeholder="eg., Albertsons",
            label_visibility="collapsed",
            disabled=not can_edit,
        )

    with right_col:
        st.markdown('<p class="filter-col-label">Search Sectors</p>',
                    unsafe_allow_html=True)
        sel_sectors = st.multiselect(
            "Sectors",
            all_sectors,
            key=f"wl_add_sec_ms_{wl_id}",
            placeholder="eg., Grocery",
            label_visibility="collapsed",
            disabled=not can_edit,
        )

    st.markdown('</div>', unsafe_allow_html=True)

    # Red pills for edit-mode multiselects
    st.markdown(
        f"""<style>
        .st-key-wl_add_co_ms_{wl_id} span[data-baseweb="tag"],
        .st-key-wl_add_sec_ms_{wl_id} span[data-baseweb="tag"] {{
            background-color: #d62e2f !important;
            border-color: #d62e2f !important;
            border-radius: 4px !important;
        }}
        .st-key-wl_add_co_ms_{wl_id} [data-baseweb="tag"] span,
        .st-key-wl_add_sec_ms_{wl_id} [data-baseweb="tag"] span,
        .st-key-wl_add_co_ms_{wl_id} [data-baseweb="tag"] button,
        .st-key-wl_add_sec_ms_{wl_id} [data-baseweb="tag"] button {{
            color: #ffffff !important;
        }}
        </style>""",
        unsafe_allow_html=True,
    )

    # ── Companies table with per-row ✕ delete — scrollable when > 5 ──
    if current_cos:
        st.markdown(
            f'<div style="margin:12px 0 4px 0;font-size:0.88rem;font-weight:600;'
            f'color:#212529;">Companies in this watchlist ({len(current_cos)})</div>',
            unsafe_allow_html=True,
        )
        col_ratios = [1.5, 3.5, 3, 2, 0.8] if can_edit else [1.5, 3.5, 3, 2]
        # Fixed header (always visible)
        hc = st.columns(col_ratios)
        for _h, _lbl in zip(hc, ["Ticker", "Company", "Sector", "Country", ""]):
            _h.markdown(f'<div class="co-table-header">{_lbl}</div>',
                        unsafe_allow_html=True)

        _deleted_ticker  = None
        _deleted_co_name = None

        # Scrollable when > 5 companies (height must be a positive int, never None)
        if len(current_cos) > 5:
            _scroll_h = min(320, len(current_cos) * 44)
            _ctx = st.container(height=_scroll_h, border=False)
        else:
            _ctx = st.container(border=False)

        with _ctx:
            # enumerate → unique widget key per row; ticker alone is NOT unique
            # (JD/LULU/TSCO siblings would collide and raise DuplicateWidgetID).
            for _ridx, co in enumerate(current_cos):
                rc = st.columns(col_ratios)
                rc[0].markdown(
                    f'<div style="padding:5px 8px;font-size:0.83rem;font-weight:600;">'
                    f'{co["ticker"]}</div>', unsafe_allow_html=True)
                rc[1].markdown(
                    f'<div style="padding:5px 8px;font-size:0.83rem;">'
                    f'{co["company_name"]}</div>', unsafe_allow_html=True)
                rc[2].markdown(
                    f'<div style="padding:5px 8px;font-size:0.83rem;color:#6c757d;">'
                    f'{co.get("sector") or ""}</div>', unsafe_allow_html=True)
                rc[3].markdown(
                    f'<div style="padding:5px 8px;font-size:0.83rem;color:#6c757d;">'
                    f'{co.get("country") or ""}</div>', unsafe_allow_html=True)
                if can_edit:
                    with rc[4]:
                        if st.button("✕", key=f"wl_del_row_{wl_id}_{_ridx}_{co['ticker']}",
                                     help=f"Remove {co['company_name']}"):
                            _deleted_ticker  = co["ticker"]
                            _deleted_co_name = co["company_name"]

        if _deleted_ticker:
            wl_remove_company(wl_id, _deleted_ticker, user_email, _deleted_co_name)
            _invalidate_watchlist_cache()
            # Keep watchlist active if it still exists after removal
            _queue_toast(f"Removed {_deleted_co_name} from '{selected_wl['name']}'", icon="🗑️")
            st.rerun()

    # ── Action buttons ──
    st.markdown("<br>", unsafe_allow_html=True)
    if can_edit:
        btn_l, btn_r, _, del_col = st.columns([1.5, 1.5, 4, 1.5])
    else:
        btn_l, _, del_col = st.columns([1.5, 5.5, 1.5])

    with btn_l:
        _close_label = "Cancel" if can_edit else "Close"
        if st.button(_close_label, key="wl_dlg_cancel", width="stretch"):
            st.session_state["wl_dlg_open"] = False
            st.rerun()

    if can_edit:
        with btn_r:
            if st.button("Save Changes", key="wl_dlg_save",
                         type="primary", width="stretch"):
                _any_change = False
                _msgs = []
                if can_edit:
                    _cur_desc = selected_wl.get("description") or ""
                    if desc_val.strip() != _cur_desc.strip():
                        wl_update(wl_id, user_email, description=desc_val.strip())
                        _any_change = True
                        _msgs.append("description updated")
                if sel_cos and can_edit:
                    # Map each selection back to its exact company record by the
                    # full "Name (TICKER)" display string — NOT by parsing the
                    # ticker, which would match both companies that share a ticker
                    # (e.g. picking "Tesco PLC (TSCO)" would also pull in Tractor
                    # Supply (TSCO)). `available` keys are unique per company.
                    _opt_map = {f"{c['company_name']} ({c['ticker']})": c for c in available}
                    to_add = [_opt_map[s] for s in sel_cos if s in _opt_map]
                    n = wl_add_companies(wl_id, to_add, user_email)
                    if n > 0:
                        _any_change = True
                        _msgs.append(f"{n} companies added")
                if sel_sectors and can_edit:
                    # `available` already excludes companies already in the list
                    # (by composite ticker+name key), so no separate ticker dedup.
                    to_add = [c for c in available if c.get("sector") in sel_sectors]
                    n = wl_add_companies(wl_id, to_add, user_email)
                    if n > 0:
                        _any_change = True
                        _msgs.append(f"{n} sector companies added")

                if _any_change:
                    _invalidate_watchlist_cache()
                    # Auto-select this watchlist after saving
                    st.session_state.scr_active_watchlist_id   = wl_id
                    st.session_state.scr_active_watchlist_name = selected_wl["name"]
                    _set_active_watchlist_scope(wl_id)
                    st.session_state.scr_criterion_cache       = {}
                    st.session_state.scr_working_df            = None
                    st.session_state.scr_show_results          = False
                    _queue_toast(f"'{selected_wl['name']}' saved — {', '.join(_msgs)}", icon="✅")
                    st.session_state["wl_dlg_open"] = False
                else:
                    _queue_toast("No changes to save", icon="ℹ️")
                    st.session_state["wl_dlg_open"] = False
                st.rerun()

    with del_col:
        if can_delete:
            if st.button("Delete", key=f"wl_del_{wl_id}",
                         type="secondary", width="stretch"):
                st.session_state[f"wl_confirm_del_{wl_id}"] = True

    if st.session_state.get(f"wl_confirm_del_{wl_id}"):
        st.warning(f"Permanently delete **{selected_wl['name']}**?")
        dc1, dc2, _ = st.columns([1.2, 1.2, 5])
        with dc1:
            if st.button("Yes, Delete", key=f"wl_cfm_del_{wl_id}", type="primary"):
                _del_name = selected_wl["name"]
                ok = wl_delete(wl_id, user_email)
                st.session_state.pop(f"wl_confirm_del_{wl_id}", None)
                if ok:
                    _invalidate_watchlist_cache()
                    # Clear if this was the active watchlist
                    if st.session_state.get("scr_active_watchlist_id") == wl_id:
                        st.session_state.scr_active_watchlist_id   = None
                        st.session_state.scr_active_watchlist_name = None
                        _clear_active_watchlist_scope()
                        st.session_state.scr_criterion_cache       = {}
                        st.session_state.scr_working_df            = None
                        st.session_state.scr_show_results          = False
                    _queue_toast(f"Watchlist '{_del_name}' deleted", icon="🗑️")
                    st.session_state["wl_dlg_open"] = False
                    st.rerun()
                else:
                    st.error("Delete failed.")
        with dc2:
            if st.button("Keep it", key=f"wl_cfm_cancel_{wl_id}"):
                st.session_state.pop(f"wl_confirm_del_{wl_id}", None)


# =============================================================================
# WATCHLIST BAR  (shown above criteria palette)
# =============================================================================

def _render_watchlist_bar():
    """Compact watchlist bar — single row: title | selectbox | Edit/Manage button.

    One watchlist at a time. No tiles. Selecting from dropdown activates it.
    """
    try:
        user_email = _current_user_email()
        if not user_email:
            return

        watchlists = _cached_user_watchlists(user_email)
        active_id  = st.session_state.get("scr_active_watchlist_id")

        # Backfill composite scope for any session that activated a watchlist
        # before scr_watchlist_members existed (id set but members missing).
        if active_id and st.session_state.get("scr_watchlist_members") is None:
            _set_active_watchlist_scope(active_id)

        st.markdown("---")

        title_col, select_col, manage_col = st.columns([2, 8, 1.2])

        with title_col:
            st.markdown('<p class="watchlist-bar-title">Watchlists</p>',
                        unsafe_allow_html=True)

        with select_col:
            if watchlists:
                wl_options = ["— None —"] + [w["name"] for w in watchlists]
                cur_name   = st.session_state.get("scr_active_watchlist_name")
                cur_idx    = wl_options.index(cur_name) if cur_name in wl_options else 0

                sel = st.selectbox(
                    "watchlist",
                    options=wl_options,
                    index=cur_idx,
                    key="scr_wl_bar_select",
                    label_visibility="collapsed",
                )

                # Detect change and apply
                if sel == "— None —":
                    if active_id is not None:
                        st.session_state.scr_active_watchlist_id   = None
                        st.session_state.scr_active_watchlist_name = None
                        _clear_active_watchlist_scope()
                        st.session_state.scr_criterion_cache       = {}
                        st.session_state.scr_working_df            = None
                        st.session_state.scr_show_results          = False
                        # Close any stale dialog (e.g. X-dismissed but flag still True)
                        st.session_state["scr_saved_dlg_open"] = False
                        st.session_state["wl_dlg_open"] = False
                        _queue_toast("Watchlist cleared", icon="🔄")
                        st.rerun()
                else:
                    chosen = next((w for w in watchlists if w["name"] == sel), None)
                    if chosen and chosen["id"] != active_id:
                        st.session_state.scr_active_watchlist_id   = chosen["id"]
                        st.session_state.scr_active_watchlist_name = chosen["name"]
                        _, _, _wl_count = _set_active_watchlist_scope(chosen["id"])
                        st.session_state.scr_criterion_cache       = {}
                        st.session_state.scr_working_df            = None
                        st.session_state.scr_show_results          = False
                        # Close any stale dialog (e.g. X-dismissed but flag still True)
                        st.session_state["scr_saved_dlg_open"] = False
                        st.session_state["wl_dlg_open"] = False
                        _queue_toast(f"Watchlist '{chosen['name']}' activated — "
                                     f"{_wl_count} companies", icon="📋")
                        if st.session_state.scr_active_criteria:
                            _trigger_recompute()
                        st.rerun()
            else:
                st.caption("No watchlists yet — click **Edit / Manage** to create one.")

        with manage_col:
            if st.button(
                "Edit / Manage",
                key="scr_wl_manage_btn",
                width="stretch",
                help="Create or edit watchlists",
            ):
                st.session_state["wl_dlg_open"] = True
                st.session_state["scr_saved_dlg_open"] = False  # close other dialog
                st.session_state["wl_dlg_mode"] = "create" if not watchlists else "edit"
                st.rerun()

        # Active indicator (compact, one line)
        if active_id:
            active_name = st.session_state.get("scr_active_watchlist_name", "")
            # True company count (composite identity), NOT len(ticker set) which
            # collapses shared-ticker siblings (e.g. Tesco + Tractor Supply = 1).
            count = st.session_state.get("scr_watchlist_count")
            if count is None:
                count = len(st.session_state.get("scr_watchlist_members")
                            or st.session_state.get("scr_watchlist_tickers") or [])
            st.markdown(
                f'<p class="watchlist-active-label">'
                f'Filtering by: <strong>{active_name}</strong> ({count} companies)</p>',
                unsafe_allow_html=True,
            )

    except Exception as e:
        log_structured_error(e, page="screening", component="_render_watchlist_bar",
                             operation="render_watchlist_bar")


# =============================================================================
# SAVED CRITERIA CONTROLS  (shown above Show Results button)
# =============================================================================

def _render_saved_criteria_controls():
    """Render 'Save Criteria' button above Show Results (browse moved to palette row)."""
    try:
        criteria = st.session_state.get("scr_active_criteria", [])
        has_criteria = len(criteria) > 0
        if not has_criteria:
            return  # nothing to show when no criteria exist
        col_save, col_space = st.columns([1.5, 8.5])
        with col_save:
            if st.button(
                "Save Criteria",
                key="scr_save_current",
                type="secondary",
                width="stretch",
                help="Save the current criteria set for future use",
            ):
                _dialog_save_criteria()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_saved_criteria_controls",
                             operation="render_controls")


# =============================================================================
# SECTION RENDERERS
# =============================================================================

def _render_page_header():
    """Render page-level Coresight header + page title block."""
    try:
        render_styles()
        st.set_page_config(page_title="Screening", layout="wide")
        set_page_layout(
            header_full_width=True,
            footer_full_width=True,
            body_padding="0 20px",
            max_content_width="1440px",
            remove_top_padding=True,
            footer_at_bottom=True
        )
        st.markdown(_SCREENING_CSS, unsafe_allow_html=True)

        render_header(full_width=True, current_page="screening")

        screen_for = st.session_state.get("scr_screen_for", "Companies")
        page_title = _SCREEN_TITLES.get(screen_for, "Screening")

        st.markdown(f"""
        <div style="margin: 24px 0 8px 0;">
            <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 24px; color: #d62e2f; letter-spacing: 1px;">CORESIGHT MARKET DATA</div>
            <div style="font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 28px; color: #323232;">{page_title}</div>
        </div>
        """, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_page_header", operation="render_header")
        st.error("Something went wrong. Please try again.")


def _render_screen_for():
    """Render horizontal Screen For radio. Only Companies is active."""
    try:
        prev = st.session_state.get("scr_screen_for", "Companies")
        default_idx = _SCREEN_FOR_OPTIONS.index(prev) if prev in _SCREEN_FOR_OPTIONS else 0

        selected = st.radio(
            "Screen For",
            options=_SCREEN_FOR_OPTIONS,
            index=default_idx,
            horizontal=True,
            key="scr_screen_for_radio",
        )

        # Keep session state in sync; reset criteria when switching away from Companies
        if selected != prev:
            st.session_state.scr_screen_for = selected
            # The tab the user is on is the single most useful thing to know when
            # reading a slow trace back: "Companies, applied filters, switched to
            # Key Devs, froze" was reconstructed by hand from a screenshot because
            # nothing in the log said a switch had happened.
            log_warning(f"[SCREEN_FOR] {prev} -> {selected}")
            _reset_criteria()
            st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_screen_for", operation="render_radio")
        st.error("Something went wrong. Please try again.")


def _render_criteria_palette():
    """Render criteria selection palette (only for Companies mode)."""
    try:
        st.markdown("---")

        # Title row: "Add Criteria" label + "Saved Criteria" browse button at right
        pal_title_col, pal_saved_col = st.columns([8.5, 1.2])
        with pal_title_col:
            st.markdown('<p class="criteria-palette-title">Add Criteria</p>',
                        unsafe_allow_html=True)
        with pal_saved_col:
            if st.button(
                "Saved Screenings",
                key="scr_pal_browse_saved",
                type="secondary",
                width="stretch",
                help="Browse and load a previously saved criteria set",
            ):
                st.session_state["scr_saved_dlg_open"] = True
                st.session_state["wl_dlg_open"] = False  # close other dialog
                st.rerun()

        cols = st.columns(len(_CRITERIA_OPTIONS))
        for i, (label, ctype, available) in enumerate(_CRITERIA_OPTIONS):
            with cols[i]:
                if available:
                    active = st.session_state.scr_active_form == ctype
                    btn_type = "primary" if active else "secondary"
                    if st.button(
                        label,
                        key=f"scr_pal_{ctype}",
                        type=btn_type,
                        width="stretch",
                    ):
                        if st.session_state.scr_active_form == ctype:
                            # Toggle off
                            st.session_state.scr_active_form = None
                            st.session_state.scr_prefill = None
                            st.session_state.scr_prefill_idx = None
                        else:
                            st.session_state.scr_active_form = ctype
                            if st.session_state.scr_prefill and \
                                    st.session_state.scr_prefill.get("type") != ctype:
                                st.session_state.scr_prefill = None
                                st.session_state.scr_prefill_idx = None
                        # Close any stale dialog left by X-dismiss
                        st.session_state["scr_saved_dlg_open"] = False
                        st.session_state["wl_dlg_open"] = False
                        st.rerun()
                else:
                    st.button(
                        label,
                        key=f"scr_pal_{ctype}",
                        width="stretch",
                        disabled=True,
                        help="Coming soon",
                    )
                    st.markdown(
                        '<span class="coming-soon-badge">Coming soon</span>',
                        unsafe_allow_html=True,
                    )
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_criteria_palette", operation="render_palette")
        st.error("Something went wrong. Please try again.")


def _render_industry_form():
    """Render the Industry Classifications criterion form."""
    try:
        prefill = st.session_state.get("scr_prefill")
        is_edit  = prefill and prefill.get("type") == "industry"

        with st.expander("Industry Classifications", expanded=True):
            industries = get_all_industries()

            if not industries:
                st.warning("No industry data available.")
                return

            default_selected = prefill.get("industries", []) if is_edit else []

            with st.form("scr_industry_form", clear_on_submit=True):
                selected = st.multiselect(
                    f"Select industries — all {len(industries)} available, type to search",
                    options=industries,
                    default=default_selected,
                    # The dropdown is virtualised: it renders only ~10 of the list at a
                    # time, so the full taxonomy looks like a short list and industries
                    # that ARE offered (Transportation, Semiconductors, Utilities …)
                    # read as missing. The count in the label and this help text say
                    # plainly that the rest are there and reachable by typing.
                    help=(f"All {len(industries)} Coresight industries are listed — scroll or "
                          f"type to find one. Filters companies to those in the selected "
                          f"industries, across the full key-development universe, not just "
                          f"Coresight-covered names."),
                    key="scr_ind_multiselect",
                )

                col_add, col_cancel, space = st.columns([1.5, 2, 6.5])
                with col_add:
                    submitted = st.form_submit_button(
                        "Add Criteria",
                        type="primary",
                        width="stretch",
                    )
                with col_cancel:
                    cancelled = st.form_submit_button(
                        "Cancel",
                        width="content",
                    )

                if cancelled:
                    st.session_state.scr_active_form = None
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()

                if submitted:
                    if not selected:
                        st.error("Please select at least one industry.")
                        return
                    criterion = {
                        "type":       "industry",
                        "industries": selected,
                        "summary":    build_industry_summary(selected),
                    }
                    _add_criterion(criterion)
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_industry_form", operation="render_industry_form")
        st.error("Something went wrong. Please try again.")


def _render_geography_form():
    """Render the Geographic Locations criterion form."""
    try:
        prefill = st.session_state.get("scr_prefill")
        is_edit  = prefill and prefill.get("type") == "geography"

        with st.expander("Geographic Locations", expanded=True):
            countries = get_all_countries()

            if not countries:
                st.info(
                    "Geographic filtering is not available — the `country_of_incorporation` "
                    "column was not found in `coreiq_companies`. "
                    "Please verify the schema."
                )
                st.session_state.scr_active_form = None
                return

            default_selected = prefill.get("countries", []) if is_edit else []

            with st.form("scr_geography_form", clear_on_submit=True):
                selected = st.multiselect(
                    "Select countries",
                    options=countries,
                    default=default_selected,
                    help="Filter companies by country of incorporation.",
                    key="scr_geo_multiselect",
                )

                col_add, col_cancel, space = st.columns([1.5, 2, 6.5])
                with col_add:
                    submitted = st.form_submit_button(
                        "Add Criteria",
                        type="primary",
                        width="stretch",
                    )
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel")

                if cancelled:
                    st.session_state.scr_active_form = None
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()

                if submitted:
                    if not selected:
                        st.error("Please select at least one country.")
                        return

                    criterion = {
                        "type":      "geography",
                        "countries": selected,
                        "summary":   build_geography_summary(selected),
                    }
                    _add_criterion(criterion)
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_geography_form", operation="render_geography_form")
        st.error("Something went wrong. Please try again.")


def _financial_universe_tickers() -> tuple:
    """Tickers for segment member options: watchlist if active, else full universe."""
    wl = st.session_state.get("scr_watchlist_tickers")
    if wl:
        return tuple(sorted(wl))
    try:
        df = get_base_company_universe()
        return tuple(sorted(df["ticker"].dropna().unique().tolist()))
    except Exception:
        return tuple()


def _render_segment_values_cache_status(segment_type: str) -> None:
    """Small health caption for segment screening cache."""
    try:
        status = get_segment_values_cache_status()
        rows_key = (
            "geographical_rows_count"
            if segment_type == "geographical"
            else "business_rows_count"
        )
        selected_rows = int(status.get(rows_key) or 0)
        updated = status.get("last_updated_timestamp") or "unknown"
        if status.get("table_exists") and selected_rows > 0:
            st.caption(f"🟢 Segment values cache ready · updated {updated}")
        else:
            st.warning(
                "Segment values cache is empty. Build it before running full-universe segment screening."
            )
    except Exception as exc:
        log_structured_error(
            exc,
            page="screening",
            component="_render_segment_values_cache_status",
            operation="cache_health",
        )
        st.warning(
            "Segment values cache is empty. Build it before running full-universe segment screening."
        )


def _segment_options_cache_key(
    segment_type: str,
    period_type: str,
    year: str,
    watchlist_exact: bool,
) -> str:
    wl = st.session_state.get("scr_watchlist_tickers")
    wl_key = "wl_" + str(len(wl)) if wl else "all"
    scope = "exact" if watchlist_exact else "global"
    return f"scr_segment_options_{segment_type}_{period_type}_{year}_{wl_key}_{scope}"


def _render_segment_members_step(
    segment_type: str,
    period_type: str,
    year: str,
    default_seg_members: List[str],
    is_edit: bool,
) -> List[str]:
    """Step 3: optional segment members (auto-loaded from cache, no button)."""
    st.caption("Optional — leave blank to include all segments.")

    # Members are pre-loaded from the global cache (presets + nightly rebuild),
    # so the dropdown is always populated instantly with no button click.
    member_opts: List[tuple] = read_segment_member_options_cache(segment_type) or []

    member_names = [m for m, _cnt in member_opts]
    member_counts = {m: cnt for m, cnt in member_opts}

    def _member_label(name: str) -> str:
        cnt = member_counts.get(name, 0)
        return f"{name} ({cnt} companies)" if cnt else name

    # For geo segments: normalize any saved raw alias to its canonical label so
    # criteria stored before canonicalization (e.g. "U.S.") still pre-populate.
    if segment_type == "geographical" and default_seg_members:
        from data.segment_aliases import canonicalize_geo_label
        default_seg_members = list(dict.fromkeys(
            canonicalize_geo_label(m) for m in default_seg_members
        ))

    valid_defaults = [m for m in default_seg_members if m in member_names]
    _ms_kwargs = dict(
        label="Segment members",
        options=member_names,
        format_func=_member_label,
        key="scr_fin_segment_members",
        placeholder="All segments — leave empty for any member…",
        help="Leave empty = All Segments.",
    )
    if is_edit and valid_defaults:
        _ms_kwargs["default"] = valid_defaults
    return st.multiselect(**_ms_kwargs)


def _submit_financial_criterion(
    stmt: str,
    metric_info: Optional[dict],
    metric_label: str,
    operator: str,
    val1: float,
    val2: float,
    unit: str,
    period_type: str,
    quarter: Optional[str],
    year_sel: str,
    *,
    num_quarters: Optional[int] = None,
    year_range: Optional[List[int]] = None,
    quarter_range: Optional[dict] = None,
    is_segment_stmt: bool = False,
    segment_type: Optional[str] = None,
    selected_segments: Optional[List[str]] = None,
    add_credit_ratings: bool = False,
    add_store_counts: bool = False,
) -> bool:
    """Validate, append financial criterion (+ optional additional data), and recompute."""
    if metric_info is None:
        st.error("Metric not found. Please re-select.")
        return False

    is_trailing = (period_type == "TQ")
    is_year_range = bool(year_range)
    is_qrange = bool(quarter_range)

    # Quarter range keeps a real operator/value (gates the latest in-range
    # quarter), so it is validated like a normal criterion — not display-only.
    if not is_trailing and not is_year_range and operator == "Between":
        lo, hi = min(val1, val2), max(val1, val2)
        if lo == hi:
            st.error("Between: Min and Max values must differ.")
            return False
        val1, val2 = lo, hi

    if is_trailing:
        n = int(num_quarters or TRAILING_QUARTERS_DEFAULT)
        timeframe = f"Last {n} Quarters"
    elif is_year_range:
        _ys = sorted(int(y) for y in year_range)
        timeframe = f"FY {_ys[0]}–{_ys[-1]}"
    elif is_qrange:
        _qp = "FQ" if period_type == "FQ" else "Q"
        timeframe = (f"{_qp}{quarter_range['from_q']} {quarter_range['from_y']}–"
                     f"{_qp}{quarter_range['to_q']} {quarter_range['to_y']}")
    elif period_type in ("CQ", "FQ") and quarter:
        q_num = quarter.replace("Q", "")
        if year_sel == "Latest":
            timeframe = f"{period_type}{q_num} Latest"
        else:
            timeframe = f"{period_type}{q_num} {year_sel}"
    else:
        timeframe = "FY Latest" if year_sel == "Latest" else f"FY {year_sel}"

    if is_trailing:
        display_col = f"{metric_label} — Last {int(num_quarters or TRAILING_QUARTERS_DEFAULT)} Quarters"
    elif is_year_range:
        display_col = f"{metric_label} ({unit}) — {timeframe}"
    elif is_qrange:
        display_col = f"{metric_label} ({unit}) — {timeframe}"
    elif is_segment_stmt:
        display_col = f"{stmt} | {metric_label} ({unit}) | {timeframe}"
    else:
        display_col = f"{metric_label} ({unit}) [{timeframe}]"

    if is_trailing:
        summary = f"{stmt} / {metric_label}: {timeframe} (all quarterly values)"
    elif is_year_range:
        summary = f"{stmt} / {metric_label}: {timeframe} (year-by-year columns)"
    elif is_qrange:
        summary = (f"{stmt} / {metric_label}: {timeframe} (quarter columns; "
                   f"{operator} {val1} on latest quarter)")
    else:
        summary = build_financial_summary(
            stmt, metric_label, operator, val1, val2, timeframe, unit=unit,
        )

    criterion = {
        "type":         "financial",
        "statement":    stmt,
        "metric_info":  metric_info,
        "metric_label": metric_label,
        "operator":     operator,
        "value1":       val1,
        "value2":       val2,
        "timeframe":    timeframe,
        "period_type":  period_type,
        "quarter":      quarter,
        "year":         year_sel,
        "display_col":  display_col,
        "summary":      summary,
    }
    if is_trailing:
        criterion["num_quarters"] = int(num_quarters or TRAILING_QUARTERS_DEFAULT)
    if is_year_range:
        criterion["year_range"] = sorted(int(y) for y in year_range)
    if is_qrange:
        criterion["quarter_range"] = dict(quarter_range)

    if is_segment_stmt:
        criterion["segment_type"] = segment_type or "business"
        criterion["selected_segments"] = list(selected_segments or [])

    parent_idx = len(st.session_state.scr_active_criteria)
    st.session_state.scr_active_criteria.append(criterion)

    show_additional_data = stmt in {"Income Statement", "Balance Sheet", "Cash Flow"}
    if show_additional_data and add_credit_ratings:
        st.session_state.scr_active_criteria.append({
            "type":        "additional",
            "data_type":   "credit_ratings",
            "show_col":    True,
            "display_col": "S&P Rating",
            "summary":     build_additional_summary("credit_ratings"),
            "hidden":      True,
            "parent_idx":  parent_idx,
        })

    if show_additional_data and add_store_counts:
        st.session_state.scr_active_criteria.append({
            "type":        "additional",
            "data_type":   "store_counts",
            "show_col":    True,
            "display_col": "Store Count",
            "summary":     build_additional_summary("store_counts"),
            "hidden":      True,
            "parent_idx":  parent_idx,
        })

    # Recompute immediately so the criterion card shows "X companies matched".
    # Segment cache makes this fast; if the cache is missing it raises and we
    # fall back to deferring the compute to the Show Results click.
    try:
        _trigger_recompute()
    except Exception:
        # Cache unavailable or query failed — defer to Show Results
        st.session_state.scr_working_df = None
        st.session_state.scr_criterion_cache = {}
        st.session_state.scr_last_computed_fingerprint = None
        st.session_state.pop("scr_results_error", None)
    st.session_state.scr_active_form = None
    st.session_state.scr_show_results = False
    st.session_state.scr_prefill = None
    st.session_state.scr_prefill_idx = None
    st.rerun()
    return True


def _render_financial_form():
    """Render the Financial Information criterion form.

    Normal / Key Stats / Ratios: Steps 1–4 (statement, metric, period, operator+value).
    Business / Geographical Segments: Steps 1–5 (+ segment members).
    Additional Data only for Income Statement, Balance Sheet, Cash Flow.
    """
    try:
        prefill = st.session_state.get("scr_prefill")
        is_edit  = prefill and prefill.get("type") == "financial"

        with st.expander("Financial Information", expanded=True):
            stmt_options = list(STATEMENT_CONFIG.keys())

            if is_edit:
                default_stmt        = prefill.get("statement", stmt_options[0])
                default_metric      = prefill.get("metric_info", {}).get("label")
                default_op          = prefill.get("operator", "Greater Than")
                default_v1          = float(prefill.get("value1", 0.0))
                default_v2          = float(prefill.get("value2", 0.0))
                default_period_type = prefill.get("period_type", "FY")
                default_quarter     = prefill.get("quarter", "Q1")
                default_year_val    = prefill.get("year")
                default_num_quarters = int(prefill.get("num_quarters") or TRAILING_QUARTERS_DEFAULT)
                default_year_range  = prefill.get("year_range")
                default_quarter_range = prefill.get("quarter_range")
                default_seg_members = list(prefill.get("selected_segments") or [])
            else:
                default_stmt        = st.session_state.get("scr_fin_stmt", stmt_options[0])
                default_metric      = st.session_state.get("scr_fin_metric")
                default_op          = st.session_state.get("scr_fin_operator", "Greater Than")
                default_v1 = default_v2 = 0.0
                default_period_type = "FY"
                default_quarter     = "Q1"
                default_year_val    = None
                default_num_quarters = TRAILING_QUARTERS_DEFAULT
                default_year_range  = None
                default_quarter_range = None
                default_seg_members = []

            if default_stmt not in stmt_options:
                default_stmt = stmt_options[0]

            # ── Step 1 — Select Statement Type ──────────────────────────────
            st.markdown('<p class="form-section-label">Step 1 — Select Statement Type</p>',
                        unsafe_allow_html=True)
            stmt = st.selectbox(
                "Statement Type", options=stmt_options,
                index=stmt_options.index(default_stmt),
                key="scr_fin_stmt_sel", label_visibility="collapsed",
            )
            st.session_state.scr_fin_stmt = stmt

            is_segment_stmt = stmt in SEGMENT_STATEMENT_TYPES
            is_tabular_stmt = stmt in TABULAR_MARKET_DATA_STMTS
            show_additional_data = stmt in {"Income Statement", "Balance Sheet", "Cash Flow"}
            stmt_cfg = STATEMENT_CONFIG.get(stmt, {})
            segment_type = stmt_cfg.get("segment_type")

            _metric_step_label = (
                "Step 2 — Select Segment Metric" if is_segment_stmt
                else "Step 2 — Select Metric"
            )
            st.markdown(
                f'<p class="form-section-label" style="margin-top:12px;">{_metric_step_label}</p>',
                unsafe_allow_html=True,
            )
            metric_labels = get_metric_labels(stmt)
            if default_metric not in metric_labels:
                default_metric = metric_labels[0] if metric_labels else None
            metric_default_idx = metric_labels.index(default_metric) if default_metric in metric_labels else 0
            metric_label = st.selectbox(
                "Metric", options=metric_labels, index=metric_default_idx,
                key="scr_fin_metric_sel", label_visibility="collapsed",
            )
            metric_info = get_metric_info(stmt, metric_label)
            unit = metric_info.get("unit", "$mm") if metric_info else "$mm"

            _period_type_for_opts = st.session_state.get("scr_fin_period_type_sel", default_period_type)
            _year_for_opts = st.session_state.get("scr_fin_year_sel", default_year_val or "Latest")

            selected_segments: List[str] = []
            if is_segment_stmt and segment_type:
                _members_step = (
                    "Step 3 — Select Business Segment Members (optional)"
                    if segment_type == "business"
                    else "Step 3 — Select Geographical Segment Members (optional)"
                )
                st.markdown(
                    f'<p class="form-section-label" style="margin-top:12px;">{_members_step}</p>',
                    unsafe_allow_html=True,
                )
                selected_segments = _render_segment_members_step(
                    segment_type,
                    _period_type_for_opts,
                    _year_for_opts if _year_for_opts else "Latest",
                    default_seg_members,
                    is_edit,
                )

            if is_segment_stmt:
                _period_step = "Step 4"
                _op_step = "Step 5"
            else:
                _period_step = "Step 3"
                _op_step = "Step 4"

            st.markdown(
                f'<p class="form-section-label" style="margin-top:12px;">{_period_step} — Set Period Type &amp; Year</p>',
                unsafe_allow_html=True,
            )
            # Trailing-quarters (TQ) is offered only for statements with
            # quarterly SEC/YF tables (Income Statement, Balance Sheet, Cash Flow).
            period_options = list(PERIOD_TYPES)
            if stmt in TRAILING_QUARTERS_STMTS:
                period_options = period_options + ["TQ"]
            if default_period_type not in period_options:
                default_period_type = "FY"
            # If a prior selection (e.g. "TQ") is no longer valid for this
            # statement, clear the widget key so the selectbox resets cleanly.
            if st.session_state.get("scr_fin_period_type_sel") not in period_options:
                st.session_state.pop("scr_fin_period_type_sel", None)
            pt_idx = period_options.index(default_period_type)
            period_type = st.selectbox(
                "Period Type", options=period_options, index=pt_idx,
                format_func=lambda p: PERIOD_TYPE_LABELS.get(p, p),
                key="scr_fin_period_type_sel",
            )
            # Forward-looking statements (Estimates / Forecasting) offer future
            # fiscal years (forecasts run several years ahead); all others use the
            # historical range.
            _years = FORWARD_SCREENING_YEARS if stmt in FORWARD_LOOKING_STMTS else SCREENING_YEARS
            year_options = ["Latest"] + _years
            is_trailing = (period_type == "TQ")
            is_quarterly = (period_type in ("CQ", "FQ"))
            num_quarters = None
            year_range = None
            quarter_range = None
            # Year-range (display-only year columns) supported only for the core
            # statements that route through apply_financial_criterion.
            _allow_year_range = (
                stmt in {"Income Statement", "Balance Sheet", "Cash Flow"}
                and not is_segment_stmt
            )
            if is_trailing:
                # Display-only: pick how many trailing quarters to show as columns.
                quarter = None
                year_sel = "Latest"
                nq_idx = (TRAILING_QUARTERS_OPTIONS.index(default_num_quarters)
                          if default_num_quarters in TRAILING_QUARTERS_OPTIONS else 0)
                num_quarters = st.selectbox(
                    "Number of Quarters", options=TRAILING_QUARTERS_OPTIONS, index=nq_idx,
                    key="scr_fin_num_quarters_sel",
                    help="Shows the last N quarterly values as separate columns "
                         "(display-only — no value filter is applied).",
                )
            elif is_quarterly:
                # CQ/FQ each offer a Single quarter or a Quarter range. Range mode
                # shows one column per quarter in [from, to] (CQ → calendar
                # quarters, FQ → fiscal quarters); the value filter (Step 4) gates
                # which companies' quarter values are shown, on the latest quarter
                # in the window.
                _q_label = "Calendar quarters" if period_type == "CQ" else "Fiscal quarters"
                _qmode = st.radio(
                    "Quarter selection", ["Single quarter", "Quarter range"],
                    horizontal=True, key="scr_fin_quarter_mode",
                    index=(1 if default_quarter_range else 0),
                    help=f"{_q_label}. Quarter range shows the metric for each "
                         "quarter side by side; the value filter applies to the "
                         "latest quarter in the range.",
                )
                if _qmode == "Quarter range":
                    quarter = None
                    year_sel = "Latest"
                    _qs = list(QUARTERS)
                    _qr_years = [int(y) for y in _years]
                    _dqr = default_quarter_range or {}
                    def _q_idx(qv, fallback):
                        try:
                            return _qs.index(f"Q{int(qv)}")
                        except Exception:
                            return fallback
                    _qc1, _qc2, _qc3, _qc4 = st.columns(4)
                    with _qc1:
                        _fq = st.selectbox("From quarter", options=_qs,
                                           index=_q_idx(_dqr.get("from_q"), 0),
                                           key="scr_fin_qr_from_q")
                    with _qc2:
                        _fy = st.selectbox(
                            "From year", options=_qr_years,
                            index=(_qr_years.index(int(_dqr["from_y"]))
                                   if _dqr.get("from_y") in _qr_years
                                   else min(2, len(_qr_years) - 1)),
                            key="scr_fin_qr_from_y")
                    with _qc3:
                        _tq = st.selectbox("To quarter", options=_qs,
                                           index=_q_idx(_dqr.get("to_q"), len(_qs) - 1),
                                           key="scr_fin_qr_to_q")
                    with _qc4:
                        _ty = st.selectbox(
                            "To year", options=_qr_years,
                            index=(_qr_years.index(int(_dqr["to_y"]))
                                   if _dqr.get("to_y") in _qr_years else 0),
                            key="scr_fin_qr_to_y")
                    quarter_range = {
                        "from_q": int(str(_fq).replace("Q", "")), "from_y": int(_fy),
                        "to_q": int(str(_tq).replace("Q", "")), "to_y": int(_ty),
                    }
                else:
                    col_q, col_y = st.columns(2)
                    with col_q:
                        q_idx = QUARTERS.index(default_quarter) if default_quarter in QUARTERS else 0
                        quarter = st.selectbox("Quarter", options=QUARTERS, index=q_idx,
                                               key="scr_fin_quarter_sel")
                    with col_y:
                        yr_idx = year_options.index(default_year_val) if default_year_val in year_options else 0
                        year_sel = st.selectbox("Year", options=year_options, index=yr_idx,
                                                key="scr_fin_year_sel")
            else:
                quarter = None
                _ymode = "Single year"
                if _allow_year_range:
                    _ymode = st.radio(
                        "Year selection", ["Single year", "Year range"],
                        horizontal=True, key="scr_fin_year_mode",
                        index=(1 if default_year_range else 0),
                        help="Year range shows the metric for each year side by side "
                             "(display-only — no value filter is applied).",
                    )
                if _ymode == "Year range":
                    _rng_years = [int(y) for y in _years]   # numeric years, no "Latest"
                    _dr = [int(y) for y in (default_year_range or [])]
                    _from_idx = (_rng_years.index(_dr[0]) if _dr and _dr[0] in _rng_years
                                 else min(4, len(_rng_years) - 1))
                    _to_idx = (_rng_years.index(_dr[-1]) if _dr and _dr[-1] in _rng_years
                               else 0)
                    _cf, _ct = st.columns(2)
                    with _cf:
                        _yfrom = st.selectbox(
                            "From year", options=_rng_years, index=_from_idx,
                            key="scr_fin_year_from",
                        )
                    with _ct:
                        _yto = st.selectbox(
                            "To year", options=_rng_years, index=_to_idx,
                            key="scr_fin_year_to",
                        )
                    _lo, _hi = sorted([int(_yfrom), int(_yto)])
                    year_range = list(range(_lo, _hi + 1))
                    year_sel = "Latest"     # unused in range mode
                else:
                    yr_idx = year_options.index(default_year_val) if default_year_val in year_options else 0
                    year_sel = st.selectbox("Year", options=year_options, index=yr_idx,
                                            key="scr_fin_year_sel")

            if is_trailing:
                # Display-only mode: no operator/value step.
                st.caption(
                    f"Last {num_quarters} quarters of {metric_label} will be shown "
                    "as separate columns. No value filter is applied."
                )
                operator = "Greater Than"
            elif year_range:
                # Display-only mode: one column per fiscal year, no value filter.
                st.caption(
                    f"{metric_label} for FY {year_range[0]}–{year_range[-1]} will be "
                    "shown as separate year columns. No value filter is applied."
                )
                operator = "Greater Than"
            else:
                if quarter_range:
                    _qlbl = "calendar" if period_type == "CQ" else "fiscal"
                    _qp = "FQ" if period_type == "FQ" else "Q"
                    st.caption(
                        f"{metric_label} from {_qp}{quarter_range['from_q']} {quarter_range['from_y']} "
                        f"to {_qp}{quarter_range['to_q']} {quarter_range['to_y']} will be shown as "
                        f"separate {_qlbl}-quarter columns. The filter below applies to the "
                        "latest quarter in the range."
                    )
                st.markdown(
                    f'<p class="form-section-label" style="margin-top:12px;">{_op_step} — Set Operator &amp; Value</p>',
                    unsafe_allow_html=True,
                )
                op_idx = OPERATORS.index(default_op) if default_op in OPERATORS else 0
                operator = st.selectbox(
                    "Operator", options=OPERATORS, index=op_idx, key="scr_fin_op_sel",
                )

            add_credit_ratings = False
            add_store_counts = False

            with st.form(
                "scr_financial_form",
                clear_on_submit=True,
                border=False,
            ):
                if is_trailing or year_range:
                    # Display-only: no value inputs.
                    val1 = 0.0
                    val2 = 0.0
                elif operator == "Between":
                    c1, c2 = st.columns(2)
                    with c1:
                        val1 = st.number_input(
                            f"Min Value ({unit})", value=default_v1,
                            format="%.2f", key="scr_fin_val1",
                        )
                    with c2:
                        val2 = st.number_input(
                            f"Max Value ({unit})", value=max(default_v2, default_v1),
                            format="%.2f", key="scr_fin_val2",
                        )
                else:
                    val1 = st.number_input(
                        f"Value ({unit})", value=default_v1,
                        format="%.2f", key="scr_fin_val1",
                    )
                    val2 = 0.0

                # Additional Data sits AFTER the value filter, before the actions.
                if show_additional_data:
                    st.markdown("---")
                    st.markdown("**Additional Data** *(optional)*")
                    _col_cr, _col_sc = st.columns(2)
                    with _col_cr:
                        add_credit_ratings = st.checkbox(
                            "Credit Ratings", value=False, key="scr_fin_add_credit",
                            help="Show latest S&P rating per company.",
                        )
                    with _col_sc:
                        add_store_counts = st.checkbox(
                            "Store Counts", value=False, key="scr_fin_add_stores",
                            help="Show latest total store count per company.",
                        )

                col_add, col_cancel, _space = st.columns([1.5, 2, 6.5])
                with col_add:
                    submitted = st.form_submit_button(
                        "Add Criteria", type="primary", width="stretch",
                    )
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel")

                if cancelled:
                    st.session_state.scr_active_form = None
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()

                if submitted:
                    _seg_selected = (
                        st.session_state.get("scr_fin_segment_members") or selected_segments
                        if is_segment_stmt else []
                    )
                    _submit_financial_criterion(
                        stmt,
                        metric_info,
                        metric_label,
                        operator,
                        val1,
                        val2,
                        unit,
                        period_type,
                        quarter,
                        year_sel,
                        num_quarters=num_quarters,
                        year_range=year_range,
                        quarter_range=quarter_range,
                        is_segment_stmt=is_segment_stmt,
                        segment_type=segment_type,
                        selected_segments=_seg_selected,
                        add_credit_ratings=add_credit_ratings,
                        add_store_counts=add_store_counts,
                    )
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_financial_form", operation="render_financial_form")
        st.error("Something went wrong. Please try again.")


def _render_keydevs_form():
    """Render the Key Developments by Category criterion form.

    Original form restored with 'Additional Data' as an optional add-on section.
    Users can select key-dev categories AND additional data simultaneously.
    """
    try:
        prefill = st.session_state.get("scr_prefill")
        is_edit = prefill and prefill.get("type") == "keydevs"
        prefill_for_dates = prefill if is_edit else None
        cat_labels = list(KEYDEV_CATEGORIES_ALL.keys())

        with st.expander("Key Developments by Category", expanded=True):
            _render_keydevs_period_radio(prefill_for_dates, "scr_kd")
            with st.form("scr_keydevs_form", clear_on_submit=True):
                default_cats = prefill.get("category_labels", []) if is_edit else []
                selected_labels = st.multiselect(
                    "Select Categories",
                    options=cat_labels,
                    default=default_cats,
                    help="Filter companies to those with key development events in the selected categories.",
                    key="scr_kd_categories",
                )
                date_fields = _collect_keydevs_date_fields(prefill_for_dates, "scr_kd")

                # ── Additional Data add-on ───────────────────────────────────
                st.markdown("---")
                st.markdown("**Additional Data** *(optional — select alongside or instead of categories above)*")
                col_cr, col_sc = st.columns(2)
                with col_cr:
                    add_credit_ratings = st.checkbox(
                        "Credit Ratings",
                        value=False,
                        key="scr_kd_add_credit",
                        help="Also add a Credit Ratings criterion showing the latest S&P rating per company.",
                    )
                with col_sc:
                    add_store_counts = st.checkbox(
                        "Store Counts",
                        value=False,
                        key="scr_kd_add_stores",
                        help="Also add a Store Counts criterion showing the latest total store count per company.",
                    )

                col_add, col_cancel, space = st.columns([1.5, 2, 6.5])
                with col_add:
                    submitted = st.form_submit_button("Add Criteria", type="primary", width="stretch")
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel", width="content")

                if cancelled:
                    st.session_state.scr_active_form = None
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()

                if submitted:
                    if not selected_labels and not add_credit_ratings and not add_store_counts:
                        st.error("Please select at least one category or additional data option.")
                        return

                    # Add key-dev criterion if categories were selected
                    if selected_labels:
                        cat_keys = [KEYDEV_CATEGORIES_ALL[lb] for lb in selected_labels]
                        criterion = {
                            "type":            "keydevs",
                            "categories":      cat_keys,
                            "category_labels": selected_labels,
                            "show_headline":   True,   # always show event details
                        }
                        err = _apply_keydevs_date_fields(criterion, date_fields)
                        if err:
                            st.error(err)
                            return
                        criterion["summary"] = _keydevs_summary_from_fields(
                            selected_labels, date_fields)
                        # Always attach a results column (see the other key-dev form
                        # and apply_keydevs_criterion) so the grid is never blank.
                        criterion["display_col"] = _keydevs_display_col(date_fields)
                        _add_criterion(criterion)

                    # Add additional data criteria if checked
                    if add_credit_ratings:
                        _add_criterion({
                            "type":        "additional",
                            "data_type":   "credit_ratings",
                            "show_col":    True,
                            "display_col": "S&P Rating",
                            "summary":     build_additional_summary("credit_ratings"),
                        })
                    if add_store_counts:
                        _add_criterion({
                            "type":        "additional",
                            "data_type":   "store_counts",
                            "show_col":    True,
                            "display_col": "Store Count",
                            "summary":     build_additional_summary("store_counts"),
                        })

                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_keydevs_form", operation="render_keydevs_form")
        st.error("Something went wrong. Please try again.")


def _render_biz_segments_form():  # kept for edit-prefill dispatch via _render_criterion_form
    """Render the Business Segments criterion form."""
    try:
        from utils.constants import SEGMENT_METRIC_GROUPS
        prefill = st.session_state.get("scr_prefill")
        is_edit = prefill and prefill.get("type") == "biz_segments"
        pf = prefill if is_edit else {}

        metric_options = list(SEGMENT_METRIC_GROUPS.keys())

        with st.expander("Business Segments", expanded=True):
            with st.form("scr_biz_segments_form", clear_on_submit=True):
                default_metric = pf.get("metric", "Revenues")
                if default_metric not in metric_options:
                    default_metric = metric_options[0]
                metric = st.selectbox(
                    "Metric",
                    options=metric_options,
                    index=metric_options.index(default_metric),
                    key="scr_biz_seg_metric",
                    help="Select the financial metric to use for filtering and display.",
                )

                filter_enabled = st.checkbox(
                    "Filter by value (optional)",
                    value=pf.get("filter_enabled", False),
                    key="scr_biz_seg_filter_enabled",
                    help="Check to filter companies by a minimum/maximum metric value.",
                )

                operator = None
                value1 = 0.0
                value2 = 0.0
                if filter_enabled:
                    col_op, col_v1, col_v2 = st.columns([2, 2, 2])
                    with col_op:
                        operator = st.selectbox(
                            "Operator", options=OPERATORS,
                            index=OPERATORS.index(pf.get("operator", "Greater Than")) if pf.get("operator") in OPERATORS else 0,
                            key="scr_biz_seg_operator",
                        )
                    with col_v1:
                        value1 = st.number_input(
                            "Value ($mm)", value=float(pf.get("value1", 0) or 0),
                            key="scr_biz_seg_value1", min_value=0.0, step=100.0,
                        )
                    with col_v2:
                        if operator == "Between":
                            value2 = st.number_input(
                                "Value 2 ($mm)", value=float(pf.get("value2", 0) or 0),
                                key="scr_biz_seg_value2", min_value=0.0, step=100.0,
                            )

                show_col = st.checkbox(
                    "Show latest total in results",
                    value=pf.get("show_col", True),
                    key="scr_biz_seg_show_col",
                )

                col_add, col_cancel, space = st.columns([1.5, 2, 6.5])
                with col_add:
                    submitted = st.form_submit_button("Add Criteria", type="primary", width="stretch")
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel", width="content")

                if cancelled:
                    st.session_state.scr_active_form = None
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()

                if submitted:
                    op_final = operator if filter_enabled else "Greater Than"
                    display_col = f"Biz Seg {metric} ($mm)" if show_col else None
                    criterion = {
                        "type":           "biz_segments",
                        "metric":         metric,
                        "filter_enabled": filter_enabled,
                        "operator":       op_final,
                        "value1":         value1,
                        "value2":         value2 if op_final == "Between" else 0.0,
                        "show_col":       show_col,
                    }
                    if display_col:
                        criterion["display_col"] = display_col
                    criterion["summary"] = build_biz_segments_summary(
                        metric, filter_enabled, op_final, value1, value2,
                    )
                    _add_criterion(criterion)
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_biz_segments_form", operation="render_form")
        st.error("Something went wrong. Please try again.")


def _render_geo_segments_form():
    """Render the Geographic Segments criterion form."""
    try:
        from utils.constants import SEGMENT_METRIC_GROUPS
        prefill = st.session_state.get("scr_prefill")
        is_edit = prefill and prefill.get("type") == "geo_segments"
        pf = prefill if is_edit else {}

        metric_options = list(SEGMENT_METRIC_GROUPS.keys())

        with st.expander("Geographic Segments", expanded=True):
            with st.form("scr_geo_segments_form", clear_on_submit=True):
                default_metric = pf.get("metric", "Revenues")
                if default_metric not in metric_options:
                    default_metric = metric_options[0]
                metric = st.selectbox(
                    "Metric",
                    options=metric_options,
                    index=metric_options.index(default_metric),
                    key="scr_geo_seg_metric",
                    help="Select the financial metric to use for filtering and display.",
                )

                filter_enabled = st.checkbox(
                    "Filter by value (optional)",
                    value=pf.get("filter_enabled", False),
                    key="scr_geo_seg_filter_enabled",
                )

                operator = None
                value1 = 0.0
                value2 = 0.0
                if filter_enabled:
                    col_op, col_v1, col_v2 = st.columns([2, 2, 2])
                    with col_op:
                        operator = st.selectbox(
                            "Operator", options=OPERATORS,
                            index=OPERATORS.index(pf.get("operator", "Greater Than")) if pf.get("operator") in OPERATORS else 0,
                            key="scr_geo_seg_operator",
                        )
                    with col_v1:
                        value1 = st.number_input(
                            "Value ($mm)", value=float(pf.get("value1", 0) or 0),
                            key="scr_geo_seg_value1", min_value=0.0, step=100.0,
                        )
                    with col_v2:
                        if operator == "Between":
                            value2 = st.number_input(
                                "Value 2 ($mm)", value=float(pf.get("value2", 0) or 0),
                                key="scr_geo_seg_value2", min_value=0.0, step=100.0,
                            )

                show_col = st.checkbox(
                    "Show latest total in results",
                    value=pf.get("show_col", True),
                    key="scr_geo_seg_show_col",
                )

                col_add, col_cancel, space = st.columns([1.5, 2, 6.5])
                with col_add:
                    submitted = st.form_submit_button("Add Criteria", type="primary", width="stretch")
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel", width="content")

                if cancelled:
                    st.session_state.scr_active_form = None
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()

                if submitted:
                    op_final = operator if filter_enabled else "Greater Than"
                    display_col = f"Geo Seg {metric} ($mm)" if show_col else None
                    criterion = {
                        "type":           "geo_segments",
                        "metric":         metric,
                        "filter_enabled": filter_enabled,
                        "operator":       op_final,
                        "value1":         value1,
                        "value2":         value2 if op_final == "Between" else 0.0,
                        "show_col":       show_col,
                    }
                    if display_col:
                        criterion["display_col"] = display_col
                    criterion["summary"] = build_geo_segments_summary(
                        metric, filter_enabled, op_final, value1, value2,
                    )
                    _add_criterion(criterion)
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_geo_segments_form", operation="render_form")
        st.error("Something went wrong. Please try again.")


def _render_additional_form():
    """Render the Additional Data (credit ratings / store counts) criterion form."""
    try:
        prefill = st.session_state.get("scr_prefill")
        is_edit = prefill and prefill.get("type") == "additional"
        pf = prefill if is_edit else {}

        data_type_options = ["credit_ratings", "store_counts"]
        data_type_labels  = {"credit_ratings": "Credit Ratings", "store_counts": "Store Counts"}

        with st.expander("Additional Data", expanded=True):
            with st.form("scr_additional_form", clear_on_submit=True):
                default_dt = pf.get("data_type", "credit_ratings")
                if default_dt not in data_type_options:
                    default_dt = "credit_ratings"
                data_type = st.radio(
                    "Data Type",
                    options=data_type_options,
                    format_func=lambda x: data_type_labels[x],
                    index=data_type_options.index(default_dt),
                    key="scr_additional_data_type",
                    horizontal=True,
                )

                show_col = st.checkbox(
                    "Show latest value in results",
                    value=pf.get("show_col", True),
                    key="scr_additional_show_col",
                )

                col_add, col_cancel, space = st.columns([1.5, 2, 6.5])
                with col_add:
                    submitted = st.form_submit_button("Add Criteria", type="primary", width="stretch")
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel", width="content")

                if cancelled:
                    st.session_state.scr_active_form = None
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()

                if submitted:
                    if data_type == "credit_ratings":
                        display_col = "S&P Rating" if show_col else None
                    else:
                        display_col = "Store Count" if show_col else None

                    criterion = {
                        "type":      "additional",
                        "data_type": data_type,
                        "show_col":  show_col,
                        "summary":   build_additional_summary(data_type),
                    }
                    if display_col:
                        criterion["display_col"] = display_col
                    _add_criterion(criterion)
                    st.session_state.scr_prefill = None
                    st.session_state.scr_prefill_idx = None
                    st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_additional_form", operation="render_form")
        st.error("Something went wrong. Please try again.")


def _render_criterion_form():
    """Dispatch to the appropriate form based on scr_active_form."""
    try:
        atype = st.session_state.get("scr_active_form")
        if atype == "industry":
            _render_industry_form()
        elif atype == "geography":
            _render_geography_form()
        elif atype == "financial":
            _render_financial_form()
        elif atype in ("keydevs", "biz_segments", "geo_segments", "additional"):
            # All segment / additional sub-types live inside the unified keydevs form
            _render_keydevs_form()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_criterion_form", operation="dispatch_form")
        st.error("Something went wrong. Please try again.")


def _build_criterion_details_html(criterion: dict) -> str:
    """Build expandable HTML details for a criterion showing selected values."""
    ctype = criterion.get("type", "")
    pills_html = ""

    if ctype == "geography":
        items = criterion.get("countries", [])
        if items:
            pills = "".join(f"<span class='criterion-pill'>{c}</span>" for c in items)
            pills_html = (
                f"<details class='criterion-details'>\n"
                f"  <summary>View selected countries ({len(items)})</summary>\n"
                f"  <div class='criterion-details-body'>{pills}</div>\n"
                f"</details>"
            )

    elif ctype == "industry":
        items = criterion.get("industries", [])
        if items:
            pills = "".join(f"<span class='criterion-pill'>{ind}</span>" for ind in items)
            pills_html = (
                f"<details class='criterion-details'>\n"
                f"  <summary>View selected industries ({len(items)})</summary>\n"
                f"  <div class='criterion-details-body'>{pills}</div>\n"
                f"</details>"
            )

    elif ctype == "financial":
        stmt = criterion.get("statement", "")
        parts = []
        parts.append(
            f"<span class='criterion-detail-kv'><strong>Statement:</strong> {stmt}</span>"
        )
        parts.append(
            f"<span class='criterion-detail-kv'><strong>Metric:</strong> "
            f"{criterion.get('metric_label', '')}</span>"
        )
        if stmt in SEGMENT_STATEMENT_TYPES:
            selected = criterion.get("selected_segments") or []
            if selected:
                seg_str = ", ".join(selected)
            else:
                seg_str = "All Segments"
            parts.append(
                f"<span class='criterion-detail-kv'><strong>Segment Members:</strong> {seg_str}</span>"
            )
        else:
            geo = criterion.get("geo_countries", [])
            biz = criterion.get("biz_segments", [])
            if geo:
                geo_str = ", ".join(geo[:3]) + ("…" if len(geo) > 3 else "")
                parts.append(
                    f"<span class='criterion-detail-kv'><strong>Geo Regions:</strong> {geo_str}</span>"
                )
            if biz:
                biz_str = ", ".join(biz[:3]) + ("…" if len(biz) > 3 else "")
                parts.append(
                    f"<span class='criterion-detail-kv'><strong>Biz Segments:</strong> {biz_str}</span>"
                )
        parts.append(
            f"<span class='criterion-detail-kv'><strong>Period:</strong> "
            f"{criterion.get('timeframe', '')}</span>"
        )
        op = criterion.get("operator", "")
        v1 = criterion.get("value1", "")
        v2 = criterion.get("value2", "")
        unit_lbl = (
            criterion.get("metric_info", {}).get("unit", "$mm")
            if criterion.get("metric_info")
            else "$mm"
        )
        if op == "Between":
            parts.append(
                f"<span class='criterion-detail-kv'><strong>Condition:</strong> "
                f"{op} {v1} – {v2} ({unit_lbl})</span>"
            )
        else:
            parts.append(
                f"<span class='criterion-detail-kv'><strong>Condition:</strong> "
                f"{op} {v1} ({unit_lbl})</span>"
            )
        detail_rows = "<br>".join(parts)
        pills_html = (
            "<details class='criterion-details'>"
            "<summary>View filter details</summary>"
            "<div class='criterion-details-body' style='flex-direction:column;gap:2px;'>"
            f"{detail_rows}"
            "</div></details>"
        )

    elif ctype == "keydevs":
        cats = criterion.get("category_labels", [])
        period = keydevs_period_display_label(criterion)
        if cats:
            pills = "".join(f"<span class='criterion-pill'>{cat}</span>" for cat in cats)
            if criterion.get("date_filter_mode") == "date_range":
                period_label = "Date range"
            else:
                period_label = "Timeframe"
            tf_html = (
                f"<div style='margin-top:6px;'>"
                f"<span class='criterion-detail-kv'><strong>{period_label}:</strong> {period}</span>"
                f"</div>"
                if period else ""
            )
            pills_html = (
                f"<details class='criterion-details'>\n"
                f"  <summary>View selected categories ({len(cats)})</summary>\n"
                f"  <div class='criterion-details-body'>{pills}</div>\n"
                f"  {tf_html}\n"
                f"</details>"
            )

    elif ctype in ("biz_segments", "geo_segments"):
        metric   = criterion.get("metric", "Revenues")
        countries = criterion.get("countries", [])
        segments  = criterion.get("segments", [])
        year      = criterion.get("year", "Latest")
        detail_items = [f"<strong>Metric:</strong> {metric}", f"<strong>Year:</strong> {year}"]
        if countries:
            detail_items.append(f"<strong>Regions:</strong> {', '.join(countries[:3])}{'…' if len(countries) > 3 else ''}")
        if segments:
            detail_items.append(f"<strong>Segments:</strong> {', '.join(segments[:3])}{'…' if len(segments) > 3 else ''}")
        if criterion.get("filter_enabled"):
            op = criterion.get("operator", "")
            v1 = criterion.get("value1", "")
            detail_items.append(f"<strong>Filter:</strong> {op} ${v1}mm")
        detail_html = "<br>".join(f"<span class='criterion-detail-kv'>{d}</span>" for d in detail_items)
        pills_html = (
            f'<div style="margin-top:6px;padding:4px 0;">{detail_html}</div>'
        )

    elif ctype == "additional":
        dt_label = {"credit_ratings": "Credit Ratings", "store_counts": "Store Counts"}.get(
            criterion.get("data_type", ""), criterion.get("data_type", "")
        )
        pills_html = (
            f"<details class='criterion-details'>\n"
            f"  <summary>View filter details</summary>\n"
            f"  <div class='criterion-details-body' style='flex-direction:column;gap:2px;'>"
            f"  <span class='criterion-detail-kv'><strong>Data Type:</strong> {dt_label}</span>"
            f"  </div>\n"
            f"</details>"
        )

    return pills_html


def _render_active_criteria():
    """Render the active criteria stack with count, summary, Edit, Remove.

    Criteria with hidden=True (geo_segments, biz_segments, additional added via
    the Financial form) are not shown as separate cards — their details appear
    inside the parent financial criterion's 'View filter details'.
    """
    try:
        criteria = st.session_state.get("scr_active_criteria", [])
        trace    = st.session_state.get("scr_debug_trace", [])

        if not criteria:
            return

        # Only count and render visible criteria
        visible = [(i, c) for i, c in enumerate(criteria) if not c.get("hidden")]

        if not visible:
            return

        st.markdown("---")
        st.markdown(
            f"<p class='criteria-palette-title'>Active Criteria "
            f"<span style='font-weight:400; text-transform:none; font-size:0.8rem;'>"
            f"({len(visible)} applied)</span></p>",
            unsafe_allow_html=True,
        )

        # Build trace lookup by criterion idx
        trace_by_idx = {t.get("criterion_idx", i): t for i, t in enumerate(trace)}

        card_num = 0
        for i, criterion in visible:
            card_num += 1
            ctype    = criterion.get("type", "")
            summary  = criterion.get("summary", "")
            dbg      = trace_by_idx.get(i, {})
            rows_out = dbg.get("rows_out")
            details_html = _build_criterion_details_html(criterion)
            # Industry / Geography narrow the set, so rows_out IS the match count.
            # Financial / Key-Dev / segment criteria deliberately keep every company
            # (a non-reporting company must stay, showing N/A), so rows_out is always
            # the whole universe — reporting "431 companies matched" for a Key-Dev
            # category that only 213 companies have any event in was just wrong.
            # For those, report what the criterion actually found.
            with_data = dbg.get("with_data")
            if rows_out is None:
                count_html = ""
            elif ctype in FILTERING_CRITERION_TYPES:
                count_html = (f"<br><span class='criterion-card-count'>"
                              f"{rows_out} companies matched</span>")
            else:
                found = rows_out if with_data is None else with_data
                count_html = (f"<br><span class='criterion-card-count'>"
                              f"{found} of {rows_out} companies have data</span>")
            card_html = textwrap.dedent(
                f"""
                <div class="criterion-card">
                  <span class="criterion-card-num">#{card_num} · {ctype.upper()}</span>
                  <br>
                  <span class="criterion-card-summary">{summary}</span>
                  {count_html}
                  {details_html}
                </div>
                """
            ).strip()
            with st.container():
                st.markdown(card_html, unsafe_allow_html=True)

                col_edit, col_remove, col_space = st.columns([1, 1, 5])
                with col_edit:
                    if st.button("Edit", key=f"scr_edit_{i}", width="stretch"):
                        _start_edit_criterion(i)
                        st.rerun()
                with col_remove:
                    if st.button("Remove", key=f"scr_remove_{i}",
                                 width="stretch", type="secondary"):
                        # Remove this visible criterion AND any hidden siblings linked to it
                        to_remove = [i]
                        for j, c in enumerate(criteria):
                            if c.get("hidden") and c.get("parent_idx") == i:
                                to_remove.append(j)
                        for j in sorted(to_remove, reverse=True):
                            try:
                                st.session_state.scr_active_criteria.pop(j)
                            except IndexError:
                                pass
                        _trigger_recompute()
                        st.session_state.scr_show_results = False
                        st.rerun()
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_active_criteria", operation="render_criteria")
        st.error("Something went wrong. Please try again.")


def _render_status_bar():
    """Render subtle status line showing current working set size."""
    try:
        criteria  = st.session_state.get("scr_active_criteria", [])
        count     = _working_count()

        if not criteria:
            # Show base universe size (or watchlist size) as context
            try:
                wl_tickers = st.session_state.get("scr_watchlist_tickers")
                wl_name    = st.session_state.get("scr_active_watchlist_name")
                if wl_tickers:
                    # True company count (composite identity), not len(ticker set).
                    total = st.session_state.get("scr_watchlist_count")
                    if total is None:
                        total = len(st.session_state.get("scr_watchlist_members") or wl_tickers)
                    wl_note = f" (watchlist: <strong>{wl_name}</strong>)"
                else:
                    base = _screening_universe()
                    total = len(base)
                    wl_note = (" (all companies with key developments)"
                               if _all_companies_on() else "")
            except Exception as _exc:
                log_structured_error(_exc, page="screening", component="_render_status_bar", operation="GET_BASE_UNIVERSE")
                total = "?"
                wl_note = ""
            st.markdown(
                f"<p class='screening-status'>Universe: <strong>{total}</strong> companies "
                f"available{wl_note}. Add criteria to narrow results.</p>",
                unsafe_allow_html=True,
            )
        elif count == -1:
            st.markdown(
                "<p class='screening-status'>Computing working set…</p>",
                unsafe_allow_html=True,
            )
        elif count == 0:
            st.markdown(
                "<p class='screening-status' style='color:#dc3545;'>"
                "⚠ No companies match all current criteria.</p>",
                unsafe_allow_html=True,
            )
        else:
            last = criteria[-1].get("summary", "")
            st.markdown(
                f"<p class='screening-status'>Working set: <strong>{count}</strong> companies "
                f"— last filter: <em>{last}</em></p>",
                unsafe_allow_html=True,
            )
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_status_bar", operation="render_status_bar")


def _render_keydevs_results():
    """Render Key Devs mode: event-per-row results table (CIQ Key Dev Screening)."""
    try:
        st.markdown("---")
        criteria = st.session_state.get("scr_active_criteria", [])

        # ── Save Criteria button (only when criteria exist) ──
        _render_saved_criteria_controls()

        wl_active = bool(st.session_state.get("scr_active_watchlist_id"))
        col_btn, col_clear, col_space = st.columns([1, 0.8, 8.2])

        with col_btn:
            show_clicked = st.button(
                "Show Results",
                type="primary",
                width="stretch",
                disabled=(len(criteria) == 0 and not wl_active),
                key="scr_keydevs_show",
            )
            if show_clicked:
                if criteria:
                    # Same guard Companies mode uses. `is None` alone recomputed only
                    # on the very first run, so editing or removing a criterion left a
                    # stale working set and the results were built for the wrong
                    # companies — one of the "sometimes it works" cases.
                    _sync_criteria_fingerprint()
                    if _needs_results_recompute():
                        _trigger_recompute()
                else:
                    wl_members = st.session_state.get("scr_watchlist_members")
                    if wl_members:
                        st.session_state.scr_working_df = filter_universe_to_members(
                            get_base_company_universe(), wl_members
                        )
                st.session_state.scr_show_results = True

        with col_clear:
            if st.button("Clear All", width="stretch",
                         disabled=(len(criteria) == 0 and not wl_active),
                         key="scr_keydevs_clear"):
                _reset_criteria()
                st.rerun()

        if not st.session_state.get("scr_show_results"):
            return

        df: Optional[pd.DataFrame] = st.session_state.get("scr_working_df")

        if df is None or len(df) == 0:
            st.info("No companies match the current criteria. Try relaxing a filter.")
            return

        # Criterion columns to carry onto the event rows. Resolved here because the
        # Excel button below is built before the grid and must export the same set.
        _kd_criteria_cols = _criteria_display_cols(
            criteria, df.columns, include_keydevs=False)

        # Gather keydevs criterion params (categories + days) for event query.
        # STICKY branded loader: stays up through the (uncached) event query AND until
        # the AgGrid results grid actually paints client-side — previously the spinner
        # vanished the moment Python returned, leaving "2000 events found" over a BLANK
        # grid (Image #18). The JS self-removes once the grid has real height.
        from components.loading import render_sticky_loader
        keydev_criteria = [c for c in criteria if c.get("type") == "keydevs"]

        # Resolve query params: tickers + categories + date window.
        # With the all-companies toggle on and no company-narrowing criterion, the
        # working set already IS every event ticker — send the sentinel so the
        # query drops its IN list entirely instead of naming all 4,327 tickers.
        _tickers = (ALL_TICKERS if _all_companies_on() and not _narrowing_criteria(criteria)
                    else tuple(df["ticker"].values))
        if not keydev_criteria:
            # No Key Dev criterion — all categories, default 1-year window.
            _cats = tuple(KEYDEV_CATEGORIES_ALL.values())
            _window = {"days": 365}
        else:
            _cats = tuple(sorted({c for kc in keydev_criteria for c in kc.get("categories", [])}))
            _window = resolve_keydevs_event_window(keydev_criteria)

        # Date-windowed keyset pagination (newest-first, "Load older"). Replaces the
        # old LIMIT 2000 that silently truncated "All History" to the newest ~17 days.
        # Only _KD_PAGE rows live in memory per page; the honest total is shown up front.
        _KD_PAGE = 500
        # An UNBOUNDED screen (no Industry / Geography criterion, so _tickers is the
        # ALL_TICKERS sentinel) gets a per-industry page instead of a flat newest-500.
        # A flat page is whatever happens to be busy: on M&A / All History it carried
        # 36 of 69 industries, so the Industry column filter could only ever find 6 of
        # the 610 rows that really match Restaurants + Coffee & Beverage. Once the user
        # DOES pick an industry the universe is already narrow and the plain
        # newest-first page over those tickers is exactly right — so this only applies
        # while nothing has narrowed the screen.
        _kd_by_industry = (_tickers is ALL_TICKERS or _tickers == ALL_TICKERS)

        # Picking industries in the RESULTS GRID's Industry column must dig as deep as
        # picking them in the criterion form — the user does not care which control
        # they used. Without this the column filter could only sift the per-industry
        # page and showed ~16 rows of the 610 that match Restaurants + Coffee.
        #
        # The selection is readable here because closing the filter popup flushes the
        # model to Streamlit (see shouldGridReturn / afterGuiDetached), so on THIS run
        # the grid's incoming value already carries it — before any fetch is decided.
        _kd_grid_industries: tuple = ()
        if _kd_by_industry:
            _gm = _incoming_grid_filter("keydevs_results_grid")
            if _gm is None:
                _gm = st.session_state.get("_grid_filter_raw_keydevs_results_grid") or {}
            _spec = _gm.get("Industry") if isinstance(_gm, dict) else None
            if isinstance(_spec, dict) and isinstance(_spec.get("values"), list):
                _kd_grid_industries = tuple(sorted(
                    str(v) for v in _spec["values"] if str(v).strip()))

        # A grid industry selection narrows the universe exactly like the criterion
        # does, so the stratified page gives way to the plain newest-500 over those
        # tickers — which is the whole point: every one of the 500 is now on-industry.
        if _kd_grid_industries:
            _ind_rows, _ = apply_industry_criterion(list(_kd_grid_industries), df)
            _ind_tickers = tuple(sorted({str(t) for t in _ind_rows["ticker"] if str(t).strip()}))
            if _ind_tickers:
                _tickers = _ind_tickers
                _kd_by_industry = False

        _kd_sig = (_tickers, _cats, _window.get("days"),
                   _window.get("start_date"), _window.get("end_date"),
                   _kd_by_industry, _kd_grid_industries)
        # Publish the signature LAST, and only together with the rows it describes.
        # It used to be stamped BEFORE the two queries, which take ~9s together on a
        # broad screen (count 4s + first page 5s). Any rerun in that window — and
        # Streamlit reruns on every widget interaction — left the signature pointing
        # at rows that were never written, so the next run treated it as a cache HIT,
        # skipped the fetch, and reported "No key development events found" for a
        # query that really has 428. That is the intermittent empty grid.
        # `kd_df not in session_state` is also a miss, so a half-finished run heals.
        if (st.session_state.get("kd_sig") != _kd_sig
                or "kd_df" not in st.session_state):
            render_sticky_loader("Loading Key Developments")
            try:
                # The count and the first page are independent, and on this DB a
                # query costs ONE VPN round-trip almost regardless of what it
                # returns (measured: COUNT 258ms, 500-row page 294ms, a 3-column
                # 500-row page 280ms — the ~250ms Azure RTT dominates). Run in
                # sequence that is 552ms; overlapped it is one round-trip.
                # Folding both into a single `COUNT(*) OVER ()` query was tried and
                # is much worse (2,190ms) — the window function forces MySQL to
                # materialize all 20k rows instead of stopping at LIMIT 500.
                _count_result: dict = {}

                def _fetch_count():
                    try:
                        _count_result["value"] = get_keydevs_events_count(
                            _tickers, _cats, days=_window.get("days"),
                            start_date=_window.get("start_date"),
                            end_date=_window.get("end_date"),
                        )
                    except BaseException as exc:      # re-raised on the main thread
                        _count_result["error"] = exc

                def _warm_subtype_domain():
                    # Both grid filter domains are needed only when the grid renders,
                    # below — fetch them here so their 462ms / 275ms land inside the
                    # page query's round-trip instead of after it. Cached for an
                    # hour, so this costs nothing again.
                    try:
                        keydev_subtype_domain(_cats)
                    except Exception:
                        pass                       # falls back to the loaded values
                    try:
                        keydev_industry_domain()
                    except Exception:
                        pass

                # NO second overlay here. render_sticky_loader("Loading Key
                # Developments") above already covers this exact block, and both
                # cards are position:fixed and centred, so adding one stacked a
                # visible card-inside-a-card (reported from STG with a screenshot).
                # One loader per wait — this is the one.
                _workers = [threading.Thread(target=_fetch_count, daemon=True),
                            threading.Thread(target=_warm_subtype_domain, daemon=True)]
                for _w in _workers:
                    # The cached fetches read st.cache_data, so a worker needs the
                    # script run context or it runs uncached and warns.
                    add_script_run_ctx(_w, get_script_run_ctx())
                    _w.start()

                if _kd_by_industry:
                    _df0, _more0 = get_keydevs_events_by_industry(
                        _cats, days=_window.get("days"),
                        start_date=_window.get("start_date"),
                        end_date=_window.get("end_date"),
                        per_industry=KEYDEV_PER_INDUSTRY, rn_from=0,
                    )
                    _cur0 = KEYDEV_PER_INDUSTRY if _more0 else None
                else:
                    _df0, _cur0 = get_keydevs_events_for_tickers(
                        _tickers, _cats, days=_window.get("days"),
                        start_date=_window.get("start_date"), end_date=_window.get("end_date"),
                        limit=_KD_PAGE,
                    )
                for _w in _workers:
                    _w.join()
                if "error" in _count_result:
                    raise _count_result["error"]
                _kd_count = _count_result.get("value", 0)
            except KeydevsQueryError as _kd_exc:
                # A failed query is NOT an empty result. Leave the cache untouched so
                # the next click retries instead of serving a poisoned empty answer.
                log_structured_error(_kd_exc, page="screening",
                                     component="_render_keydevs_results",
                                     operation="fetch_keydev_events",
                                     context=f"tickers={len(_tickers)} cats={len(_cats)}")
                st.session_state.pop("kd_sig", None)
                st.warning(
                    "This screen was too large for the database to return in time. "
                    "Narrow it — a shorter timeframe, fewer categories, or an "
                    "Industry / Geographic Locations criterion — then press "
                    "**Show Results** again."
                )
                return
            st.session_state["kd_total"] = _kd_count
            st.session_state["kd_df"] = _df0
            st.session_state["kd_cursor"] = _cur0
            st.session_state["kd_sig"] = _kd_sig

        events_df = st.session_state.get("kd_df", pd.DataFrame())
        _kd_total = int(st.session_state.get("kd_total", len(events_df)))
        _kd_cursor = st.session_state.get("kd_cursor")

        if events_df.empty:
            st.info("No key development events found for the matched companies in the selected timeframe.")
            return

        # Header row with Excel download — honest total, newest-first.
        # Both are rendered into placeholders and FILLED IN AFTER the grid below,
        # because what the Excel button must export depends on the column filters
        # the user has set in the grid — which only exist once the grid has run.
        _shown = len(events_df)
        _hdr_col, _dl_col = st.columns([9, 1.5])
        _hdr_slot = _hdr_col.empty()
        _dl_slot = _dl_col.empty()

        # Make relative internal URLs absolute so LinkColumn works in any environment
        if "Source Reference" in events_df.columns:
            try:
                from urllib.parse import urlparse
                _current_url = getattr(st.context, "url", None) or ""
                _parsed = urlparse(_current_url)
                _base = f"{_parsed.scheme}://{_parsed.netloc}" if _parsed.netloc else ""
                if _base:
                    events_df = events_df.copy()
                    events_df["Source Reference"] = events_df["Source Reference"].apply(
                        lambda v: (_base + v) if isinstance(v, str) and v.startswith("/") else v
                    )
            except Exception:
                pass

        grid_df = _prepare_keydevs_grid_df(events_df)

        # Hidden identity columns so checkbox-selected event rows can be saved as
        # a watchlist (the watchlist stores the companies behind the events).
        # Ticker is parsed from the "Company Name(s)" label: "Name (EXCH:TICKER)".
        _kd_hidden = [c for c in ("_source_url", "_event_id") if c in grid_df.columns]
        try:
            if "Company Name(s)" in grid_df.columns:
                grid_df = grid_df.copy()
                grid_df["Ticker"] = grid_df["Company Name(s)"].apply(keydev_label_ticker)
                grid_df["_CompanyName"] = grid_df["Company Name(s)"].apply(keydev_label_name)
                _kd_hidden += ["Ticker", "_CompanyName"]
        except Exception:
            pass

        # Columns the user's own criteria contributed (Industry, Country, financial
        # metrics, segments, additional data). They are computed per COMPANY on the
        # working set, so they have to be joined onto these per-EVENT rows — adding
        # an Industry criterion here used to change nothing visible at all. They
        # inherit the Excel-style distinct-values header filter from the grid's
        # configure_default_column, like every other column.
        # One event per company, not one per company that happens to share the
        # ticker — see keep_working_set_companies.
        grid_df = keep_working_set_companies(grid_df, df)
        grid_df = merge_company_columns(grid_df, df, _kd_criteria_cols,
                                        after="Company Name(s)")

        _kd_resp = _render_filterable_results_grid(
            grid_df,
            key="keydevs_results_grid",
            empty_message="No key development events found for the matched companies.",
            pinned_column="Company Name(s)",
            link_columns=["Source Reference"] if "Source Reference" in grid_df.columns else None,
            hidden_columns=_kd_hidden or None,
            enable_selection=True,
            # The subtype and industry lists must cover the whole result, not the
            # loaded page — the Excel export honours these filters, so a missing
            # value would drop rows the user never deselected.
            filter_domains={
                "Key Developments by Type": keydev_subtype_domain(_cats),
                "Industry": keydev_industry_domain(),
            },
        )

        # ── Header + Excel, now that the grid's column filters are known ──
        # The Excel button used to be rendered ABOVE the grid and re-queried the
        # whole result set, so filtering the grid and hitting Excel still handed
        # back every row — the filter was invisible to it. It is filled in here
        # instead, and the filter is re-applied to the FULL fetched set (not just
        # the loaded page), so the file holds every row the filter matches.
        _kd_filter_model = grid_filter_model(_kd_resp)
        # Count the matches HERE rather than trusting `_kd_resp.data`. The component
        # only returns a payload when it is allowed to (see shouldGridReturn), so its
        # rows describe the page as it was when the user last closed the filter popup
        # — one interaction stale. That is how the banner came to read "16 matching
        # rows out of the 500 loaded" over a grid that was correctly showing all 500.
        # Re-applying the model to the frame we just rendered cannot drift.
        _kd_filtered_rows = (len(apply_grid_filter_model(events_df, _kd_filter_model))
                             if _kd_filter_model else 0)

        with _hdr_slot:
            _hdr_txt = f"<strong>{_kd_total:,}</strong> key development events found"
            if _kd_grid_industries:
                _hdr_txt += (f" <span style='color:#6b7280;font-weight:400'>"
                             f"· {', '.join(_kd_grid_industries)}"
                             f"{' · showing newest ' + format(_shown, ',') if _kd_total > _shown else ''}"
                             f"</span>")
            elif _kd_total > _shown:
                if _kd_by_industry:
                    _n_inds = (events_df["Industry"].replace("", pd.NA).nunique(dropna=True)
                               if "Industry" in events_df.columns else 0)
                    _hdr_txt += (f" <span style='color:#6b7280;font-weight:400'>"
                                 f"· showing the newest few from each of "
                                 f"{_n_inds} industries ({_shown:,} rows) — add an "
                                 f"Industry criterion to see everything for one</span>")
                else:
                    _hdr_txt += (f" <span style='color:#6b7280;font-weight:400'>"
                                 f"· showing newest {_shown:,}</span>")
            if _kd_filter_model:
                # The column filter is a CLIENT-side filter: it can only sift the rows
                # the grid actually holds, which is the newest `_shown` page — not the
                # whole result. Measured: filtering Industry to Restaurants + Coffee on
                # the all-history M&A screen shows 6 rows when 610 events really match,
                # because 500 of 21,610 events were loaded. The old wording ("6 of 500
                # shown rows") was technically true but read as "6 matches", so say
                # outright that rows beyond the loaded page were never examined.
                # With a grid industry selection the rows were fetched FOR those
                # industries, so "filtered on Industry" adds nothing — the green line
                # below already states the real totals. Only mention other columns.
                _other = sorted(c for c in _kd_filter_model if c != "Industry"
                                or not _kd_grid_industries)
                if _other:
                    _cols = ", ".join(_other)
                    _hdr_txt += (f"<br><span style='color:#C8102E;font-weight:500;font-size:13px'>"
                                 f"Filtered on {_cols} · {_kd_filtered_rows:,} matching rows"
                                 f" out of the {_shown:,} loaded</span>")
                if _kd_grid_industries:
                    # The Industry selection re-queried the DB for exactly these
                    # industries, so every loaded row is on-industry and `_kd_total`
                    # is already their true total — nothing was skipped.
                    _hdr_txt += (
                        f"<br><span style='color:#1a7f37;font-weight:600;font-size:13px'>"
                        f"Loaded straight from the database for "
                        f"{', '.join(_kd_grid_industries)} — "
                        f"{_shown:,} of {_kd_total:,} matching events"
                        + ("; download Excel for all of them."
                           if _kd_total > _shown else ", i.e. all of them.")
                        + "</span>")
                elif _kd_total > _shown:
                    _how = ("the newest few per industry loaded so far"
                            if _kd_by_industry else
                            f"the {_shown:,} events loaded so far")
                    _fix = ("add an Industry criterion to load every event for that "
                            "industry" if _kd_by_industry else
                            "use “Load more events” to widen it")
                    _hdr_txt += (
                        f"<br><span style='color:#8f0b1f;font-weight:600;font-size:13px'>"
                        f"⚠ This filter only searched {_how} ({_shown:,} of {_kd_total:,} "
                        f"events) — {_fix}, or download Excel, which applies this filter "
                        f"to all {_kd_total:,}.</span>")
            st.markdown(f"<p class='results-header'>{_hdr_txt}</p>", unsafe_allow_html=True)

        with _dl_slot:
            # The workbook is built ON CLICK, never while the user waits for the
            # grid: measured on STG for the 146k-event all-history screen, the grid
            # needs 4.0s (count 1.1s + first 500-row page 2.8s) but an eager export
            # build added 336s on top (fetch 323s + xlsx 13s) — 99% of the wait,
            # spent on a file most users never ask for. st.download_button accepts a
            # callable and defers it to click time, off the event loop.
            from datetime import datetime as _kd_dt
            _render_excel_js_download(
                lambda: _build_full_keydevs_workbook(
                    _tickers, _cats, _window, len(criteria),
                    working_df=df, criteria_cols=_kd_criteria_cols,
                    filter_model=_kd_filter_model),
                f"KeyDev_Screening_{_kd_dt.now().strftime('%Y%m%d_%H%M')}.xlsx",
                label="Excel",
            )

        # Load-more link — small red text (not a full-width button), placed ABOVE
        # the "Save as watchlist" panel. Keyset seek (no OFFSET scan); accumulates
        # in session, one page per click, every historical event stays reachable.
        if _kd_cursor is not None and _shown < _kd_total:
            st.markdown(
                "<style>"
                "[class*='st-key-kd_load_older'] button{background:transparent!important;"
                "border:none!important;box-shadow:none!important;color:#C8102E!important;"
                "font-size:13px!important;font-weight:600!important;padding:2px 4px!important;"
                "min-height:0!important;height:auto!important;width:auto!important;}"
                "[class*='st-key-kd_load_older'] button:hover{color:#8f0b1f!important;"
                "text-decoration:underline!important;background:transparent!important;}"
                "</style>",
                unsafe_allow_html=True,
            )
            if st.button("⬇ Load more events", key="kd_load_older"):
                try:
                    if _kd_by_industry:
                        # The cursor is a row-number depth, not a date: each click
                        # takes the NEXT slice of every industry, so the page keeps
                        # covering all of them instead of drifting into whichever
                        # industry happens to have the older events.
                        _df_next, _more_next = get_keydevs_events_by_industry(
                            _cats, days=_window.get("days"),
                            start_date=_window.get("start_date"),
                            end_date=_window.get("end_date"),
                            per_industry=KEYDEV_PER_INDUSTRY, rn_from=int(_kd_cursor),
                        )
                        _cur_next = (int(_kd_cursor) + KEYDEV_PER_INDUSTRY) if _more_next else None
                    else:
                        _df_next, _cur_next = get_keydevs_events_for_tickers(
                            _tickers, _cats, days=_window.get("days"),
                            start_date=_window.get("start_date"), end_date=_window.get("end_date"),
                            limit=_KD_PAGE, before_date=_kd_cursor[0], before_id=_kd_cursor[1],
                        )
                except KeydevsQueryError:
                    st.warning("Could not load more events just now — please try again.")
                    return
                if not _df_next.empty:
                    st.session_state["kd_df"] = pd.concat(
                        [events_df, _df_next], ignore_index=True)
                st.session_state["kd_cursor"] = _cur_next
                st.rerun()

        _render_save_as_watchlist_panel(_kd_resp)
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_keydevs_results", operation="render_keydevs_results")
        st.error("Something went wrong. Please try again.")


# =============================================================================
# EXCEL DOWNLOAD HELPERS
# =============================================================================

# ── One-step Excel download ──────────────────────────────────────────────────
# Building the workbook is cheap (~30-75ms even for ~900 rows), so it is built
# inline and memoised by a signature of the current result set — the first
# render of a given result set pays the build once; later reruns (sorting,
# selecting, etc.) hit the cache. A single JS download button is rendered; the
# download itself is client-side, so clicking it never triggers a rerun.
_EXCEL_BUILD_CACHE: Dict[str, bytes] = {}   # sig -> workbook bytes
_EXCEL_BUILD_MAX = 8


def _render_excel_download(sig: str, builder) -> None:
    """Render the single Excel download button, building (once) on demand.

    `builder` is a zero-arg callable returning the workbook bytes.
    """
    from datetime import datetime

    data = _EXCEL_BUILD_CACHE.get(sig)
    if not isinstance(data, (bytes, bytearray)):
        t_xl = time.perf_counter()
        try:
            data = builder() or b""
        except Exception as exc:
            log_structured_error(exc, page="screening",
                                 component="_render_excel_download", operation="build_excel")
            data = b""
        # Bound the cache to the few most-recent result sets.
        if len(_EXCEL_BUILD_CACHE) >= _EXCEL_BUILD_MAX:
            for old in list(_EXCEL_BUILD_CACHE.keys())[: len(_EXCEL_BUILD_CACHE) - _EXCEL_BUILD_MAX + 1]:
                _EXCEL_BUILD_CACHE.pop(old, None)
        _EXCEL_BUILD_CACHE[sig] = data
        log_timing("SCREENING_RESULTS_EXCEL_BUILD",
                   (time.perf_counter() - t_xl) * 1000, f"bytes={len(data)}")

    if data:
        _ts = datetime.now().strftime("%Y%m%d_%H%M")
        _render_excel_js_download(
            data, f"Screening_Results_{_ts}.xlsx", label="Excel",
        )
    else:
        st.caption("Excel unavailable — check logs.")


_EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

def _render_excel_js_download(excel_bytes, filename: str, label: str = "Excel") -> None:
    """Branded Excel download button served over HTTP, not the websocket.

    The workbook is handed to Streamlit's media file manager (``st.download_button``),
    so the ForwardMsg carries only a ``/media/...`` URL and the browser fetches the
    bytes over plain HTTP.

    It used to base64-inline the whole workbook into a ``components.html`` iframe.
    That put the entire file inside ONE websocket message, which scales with the
    result set (~316 B/row xlsx → ~421 B/row after base64: ~7.9 MB at 19k key-dev
    rows, ~66 MB for the full 156k-event universe). Anything that large is
    truncated in transit by the marketdata-stg proxy, and the browser's protobuf
    decoder then fails with `RangeError: index out of range: 99 + 7909686 > 348066`,
    surfaced as the "Connection error" modal. A URL is a few dozen bytes at any
    result size, so the message can no longer outgrow the transport.
    """
    try:
        # `excel_bytes` may be ready-made bytes OR a zero-arg callable returning them.
        # A callable is handed straight to st.download_button, which defers it to click
        # time and runs it off the event loop — use that for workbooks expensive enough
        # that building them up-front would stall the page (see the key-devs export).
        if callable(excel_bytes):
            size_part = "deferred"
        else:
            if not excel_bytes:
                return
            size_part = str(len(excel_bytes))
        # Stable per-call key so the CSS can target it and the widget survives reruns.
        btn_key = "xlbtn-" + hashlib.md5(
            f"{filename}|{label}|{size_part}".encode()).hexdigest()[:12]
        with st.container(key=btn_key):
            st.download_button(
                label=label,
                data=excel_bytes,
                file_name=filename,
                mime=_EXCEL_MIME,
                key=f"dl_{btn_key}",
                icon=":material/table:",
                on_click="ignore",   # downloading must not trigger a rerun
                width="content",
            )
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_excel_js_download", operation="render_excel_download_button")


def _build_full_keydevs_workbook(tickers, categories, window, criteria_count,
                                 working_df=None, criteria_cols=None,
                                 filter_model=None) -> bytes:
    """Assemble the COMPLETE key-dev export for one query. Called on click only.

    ``working_df`` / ``criteria_cols`` carry the same criterion columns the grid
    shows (Industry, Country, financial metrics, …) into the workbook, so the
    file a user downloads matches the table they were looking at.

    Passed to ``st.download_button`` as a callable so Streamlit defers it until the
    user actually clicks Excel, and runs it in a worker thread. Building it eagerly
    while the results page rendered was 99% of the "Show Results" wait (336s of 340s
    on the 146k-event all-history screen) and produced a file most users never open.

    The set is assembled with the SAME keyset pagination the grid uses (many fast,
    index-served seeks) — NOT one giant ``LIMIT = total`` query. A single 100k+ row
    statement exceeds the 120s read_timeout on the read-only connection and comes
    back EMPTY, which was the old "download only gives some records" bug. Each
    10k-row page stays well under the timeout, so the workbook always holds the
    complete set, newest-first.
    """
    page_size = 10000
    runaway_limit = 500000   # safety backstop far above any real dataset
    frames: list = []
    cursor = None
    row_count = 0
    try:
        while True:
            page, cursor = get_keydevs_events_for_tickers(
                tickers, categories, days=window.get("days"),
                start_date=window.get("start_date"), end_date=window.get("end_date"),
                limit=page_size,
                before_date=(cursor[0] if cursor else None),
                before_id=(cursor[1] if cursor else None),
            )
            if page is None or page.empty:
                break
            frames.append(page)
            row_count += len(page)
            if cursor is None or row_count >= runaway_limit:
                if row_count >= runaway_limit:
                    log_warning(f"[KEYDEVS_EXPORT] runaway guard hit at {row_count} rows")
                break
        full = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if full.empty:
            return b""
        if working_df is not None and "Company Name(s)" in full.columns:
            full["Ticker"] = full["Company Name(s)"].apply(keydev_label_ticker)
            full["_CompanyName"] = full["Company Name(s)"].apply(keydev_label_name)
            # Dedupe ALWAYS, criterion columns or not. Gating it on `criteria_cols`
            # meant a screen with no Industry/Geography criterion exported the
            # shared-ticker duplicates: 10,773 rows for 10,708 events, because
            # JD/LULU/TSCO events are listed once per same-ticker company.
            full = keep_working_set_companies(full, working_df)
            if criteria_cols:
                full = merge_company_columns(full, working_df, criteria_cols,
                                             after="Company Name(s)")
            full = full.drop(columns=["Ticker", "_CompanyName"])
        full = full.drop(columns=[c for c in ("_event_id",) if c in full.columns])
        # The user's grid column filters, re-applied to the COMPLETE set. Done last
        # so the criterion columns merged above (Industry, Country, …) are filterable
        # too — those are added after the fetch and would otherwise be invisible here.
        if filter_model:
            before = len(full)
            filtered = apply_grid_filter_model(full, filter_model)
            log_warning(f"[KEYDEVS_EXPORT] grid filter applied: {before} → {len(filtered)} rows "
                        f"(columns: {sorted(filter_model)})")
            # An empty result used to `return b""`, which downloads a 0-BYTE .xlsx —
            # a file Excel refuses to open, indistinguishable from a crash. Keep the
            # filtered frame (its columns survive), so the workbook below is a valid
            # header-only file the user can actually open and read as "no matches".
            full = filtered
        return _build_keydevs_excel_fast(full, criteria_count,
                                        title="Coresight Key Developments")
    except Exception as exc:
        log_structured_error(exc, page="screening", component="keydevs_full_export",
                             operation="build_full_excel")
        return b""


def _build_keydevs_excel_fast(
    df: pd.DataFrame,
    criteria_count: int,
    title: str = "Coresight Key Developments",
) -> bytes:
    """Fast, low-RAM Excel for the FULL key-dev export (can be 100k+ rows).

    Uses openpyxl write_only (streaming) mode + itertuples, and skips the
    per-cell borders/fills of _build_screening_excel — those make the styled
    builder minutes-slow and RAM-heavy at scale. Branded title + bold header +
    plain streamed data rows keeps a 140k-row export to a few seconds.
    """
    try:
        import io
        from datetime import datetime
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font, PatternFill

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Key Developments")
        cols = list(df.columns)

        t = WriteOnlyCell(ws, value=title)
        t.font = Font(name="Inter", size=14, bold=True, color="D62E2F")
        ws.append([t])
        now = datetime.now().strftime("%B %d, %Y %I:%M %p")
        sub = WriteOnlyCell(
            ws, value=f"{len(df):,} events  |  {criteria_count} criteria  |  Generated {now}")
        sub.font = Font(name="Inter", size=10, color="6B6B6B")
        ws.append([sub])
        ws.append([])

        hdr = []
        for c in cols:
            hc = WriteOnlyCell(ws, value=str(c))
            hc.font = Font(name="Inter", size=10, bold=True, color="2D2A29")
            hc.fill = PatternFill(start_color="F0F0F0", end_color="F0F0F0", fill_type="solid")
            hdr.append(hc)
        ws.append(hdr)

        for row in df.itertuples(index=False, name=None):
            ws.append([
                "" if (v is None or (isinstance(v, float) and pd.isna(v))) else v
                for v in row
            ])

        buf = io.BytesIO()
        wb.save(buf)
        data = buf.getvalue()
        buf.close()
        return data
    except Exception as e:
        log_structured_error(e, page="screening", component="_build_keydevs_excel_fast", operation="build_excel_fast")
        return b""


# NOTE (19-Jul): the background-thread + `@st.fragment(run_every=2)` full-export
# widget that used to live here was REMOVED. run_every=2 fired a client rerun every
# 2s on the screening results page; over a high-latency link those reruns piled up
# until the app stopped responding and Azure crash-looped the STG container. The
# Excel button is now a plain synchronous branded download of the shown events (see
# the `_dl_col` block in _render_keydevs_results). Do NOT reintroduce run_every here.


def _build_screening_excel(
    df: pd.DataFrame,
    criteria_count: int,
    title: str = "Coresight Screening Results",
    subtitle_prefix: str | None = None,
) -> bytes:
    """Build a branded Excel workbook from the screening results DataFrame."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from datetime import datetime

        wb = Workbook()
        ws = wb.active
        ws.title = "Screening Results"

        # Brand colours
        RED = "D62E2F"
        DARK = "2D2A29"
        LGRAY = "E0E0E0"
        ALT_ROW = "F9F9F9"
        HDR_BG = "F0F0F0"

        thin_border = Border(
            top=Side(style="thin", color=LGRAY),
            bottom=Side(style="thin", color=LGRAY),
            left=Side(style="thin", color=LGRAY),
            right=Side(style="thin", color=LGRAY),
        )

        # ── Title row ──
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(df.columns), 3))
        title_cell = ws.cell(row=1, column=1, value=title)
        title_cell.font = Font(name="Inter", size=14, bold=True, color=RED)
        title_cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[1].height = 30

        # ── Subtitle row ──
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(len(df.columns), 3))
        now_str = datetime.now().strftime("%B %d, %Y %I:%M %p")
        _prefix = subtitle_prefix if subtitle_prefix else f"{len(df)} companies"
        subtitle = f"{_prefix}  |  {criteria_count} criteria  |  Generated {now_str}"
        sub_cell = ws.cell(row=2, column=1, value=subtitle)
        sub_cell.font = Font(name="Inter", size=10, color="6B6B6B")
        sub_cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[2].height = 22

        # ── Blank row ──
        start_row = 4

        # ── Header row ──
        hdr_font = Font(name="Inter", size=10, bold=True, color=DARK)
        hdr_fill = PatternFill(start_color=HDR_BG, end_color=HDR_BG, fill_type="solid")
        hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[start_row].height = 28

        for c_idx, col_name in enumerate(df.columns, 1):
            cell = ws.cell(row=start_row, column=c_idx, value=col_name)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = hdr_align
            cell.border = thin_border

        # ── Data rows ──
        data_font = Font(name="Inter", size=10, color=DARK)
        alt_fill = PatternFill(start_color=ALT_ROW, end_color=ALT_ROW, fill_type="solid")
        num_font = Font(name="Inter", size=10, color=DARK)
        num_fmt = "#,##0.00"

        for r_idx, (_, row) in enumerate(df.iterrows(), start_row + 1):
            for c_idx, col_name in enumerate(df.columns, 1):
                val = row[col_name]
                try:
                    if pd.isna(val):
                        val = ""
                except (ValueError, TypeError):
                    pass  # Series or other non-scalar; keep as-is
                cell = ws.cell(row=r_idx, column=c_idx, value=val)
                cell.font = data_font
                cell.border = thin_border

                # Alternate row shading
                if (r_idx - start_row) % 2 == 0:
                    cell.fill = alt_fill

                # Number formatting for non-string columns
                if isinstance(val, (int, float)):
                    cell.font = num_font
                    cell.number_format = num_fmt
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                else:
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

        # ── Auto-fit column widths ──
        for c_idx, col_name in enumerate(df.columns, 1):
            max_len = len(str(col_name)) + 2
            for r_idx in range(start_row + 1, start_row + 1 + min(len(df), 100)):
                cell_val = ws.cell(row=r_idx, column=c_idx).value
                if cell_val is not None:
                    max_len = max(max_len, len(str(cell_val)))
            col_letter = get_column_letter(c_idx)
            ws.column_dimensions[col_letter].width = min(max_len + 3, 50)

        # Company Name column wider
        if len(df.columns) > 0:
            ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width, 40)

        # ── Freeze panes (header row) ──
        ws.freeze_panes = f"A{start_row + 1}"

        # ── Auto-filter ──
        last_col = get_column_letter(len(df.columns))
        ws.auto_filter.ref = f"A{start_row}:{last_col}{start_row + len(df)}"

        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except Exception as e:
        log_structured_error(e, page="screening", component="_build_screening_excel", operation="build_excel")
        return b""


def _render_people_results():
    """Render People Screening results — executive compensation data.

    Uses the existing company working set (filtered by any active Industry /
    Geography / Financial criteria) to determine the ticker universe, then
    fetches ALL years of compensation data from both SEC and YF sources.
    """
    try:
        st.markdown("---")
        criteria   = st.session_state.get("scr_active_criteria", [])
        wl_active  = bool(st.session_state.get("scr_active_watchlist_id"))

        col_btn, col_clear, col_space = st.columns([1, 0.8, 8.2])
        with col_btn:
            show_clicked = st.button(
                "Show Results",
                type="primary",
                width="stretch",
                key="scr_people_show_btn",
                disabled=(len(criteria) == 0 and not wl_active),
            )
            if show_clicked:
                if criteria:
                    # Same guard Companies mode uses. `is None` alone recomputed only
                    # on the very first run, so editing or removing a criterion left a
                    # stale working set and the results were built for the wrong
                    # companies — one of the "sometimes it works" cases.
                    _sync_criteria_fingerprint()
                    if _needs_results_recompute():
                        _trigger_recompute()
                else:
                    wl_members = st.session_state.get("scr_watchlist_members")
                    if wl_members:
                        st.session_state.scr_working_df = filter_universe_to_members(
                            get_base_company_universe(), wl_members
                        )
                st.session_state.scr_show_results = True

        with col_clear:
            if st.button("Clear All", width="stretch",
                         key="scr_people_clear_btn",
                         disabled=(len(criteria) == 0 and not wl_active)):
                _reset_criteria()
                st.rerun()

        if not st.session_state.get("scr_show_results"):
            return

        company_df: Optional[pd.DataFrame] = st.session_state.get("scr_working_df")

        if company_df is None or len(company_df) == 0:
            st.info("No companies match the current criteria. Try relaxing a filter.")
            return

        tickers_in_scope = list(company_df["ticker"].unique())

        # Build a lookup: ticker → {company_name, sector, country}
        company_meta = {
            row["ticker"]: row
            for _, row in company_df.iterrows()
        }

        # Detect which criteria types are active (for column visibility).
        # Industry is no longer gated on its criterion — it is always shown.
        has_geography = any(c.get("type") == "geography" for c in criteria)

        # Fetch SEC and YF data in parallel
        from data.repository import ExecutiveCompensationRepository as _ECR
        from data.repository import CompanyRepository as _CR
        from concurrent.futures import ThreadPoolExecutor

        # Split tickers by source using cached companies map
        _cmap = _CR.get_companies_map()
        _sec_tickers = tuple(t for t in tickers_in_scope if _cmap.get(t, {}).get("source") == "SEC")
        _yf_tickers  = tuple(t for t in tickers_in_scope if _cmap.get(t, {}).get("source") == "YFinance")

        with ThreadPoolExecutor(max_workers=2) as _ppl:
            _f_sec = _ppl.submit(_ECR.get_sec_all_compensation, _sec_tickers)
            _f_yf  = _ppl.submit(_ECR.get_yf_all_compensation,  _yf_tickers)
            _sec_rows = []
            _yf_rows  = []
            try:
                _sec_rows = _f_sec.result()
            except Exception as _e:
                log_error(f"[PEOPLE] SEC compensation fetch failed: {_e}")
            try:
                _yf_rows = _f_yf.result()
            except Exception as _e:
                log_error(f"[PEOPLE] YF compensation fetch failed: {_e}")

        # Build the unified people dataframe
        import pandas as pd
        records = []

        # SEC rows — have salary / bonus / stock_awards / total_compensation
        for r in _sec_rows:
            tk = r.get("ticker", "")
            meta = company_meta.get(tk, {})
            co_name = meta.get("company_name", tk)
            rec = {
                "Company":           co_name,
                "Ticker":            tk,
                "Executive Name":    r.get("executive_name", ""),
                "Position":          r.get("position", ""),
                "Year":              r.get("compensation_year", ""),
                "Salary":            r.get("salary"),
                "Bonus":             r.get("bonus"),
                "Stock Awards":      r.get("stock_awards"),
                "Total Compensation": r.get("total_compensation"),
                "Total Pay":         None,
                "_source":           "SEC",
            }
            # Always populate — the Industry label is wanted on every People row,
            # not only when an Industry criterion happens to be active.
            rec["Industry"] = meta.get("sector", "")
            if has_geography:
                rec["Country"] = meta.get("country", "")
            records.append(rec)

        # YF rows — only have total_pay
        for r in _yf_rows:
            tk = r.get("ticker", "")
            meta = company_meta.get(tk, {})
            co_name = meta.get("company_name", tk)
            rec = {
                "Company":           co_name,
                "Ticker":            tk,
                "Executive Name":    r.get("executive_name", ""),
                "Position":          r.get("position", ""),
                "Year":              r.get("compensation_year", ""),
                "Salary":            None,
                "Bonus":             None,
                "Stock Awards":      None,
                "Total Compensation": None,
                "Total Pay":         r.get("total_pay"),
                "_source":           "YF",
            }
            # Always populate — the Industry label is wanted on every People row,
            # not only when an Industry criterion happens to be active.
            rec["Industry"] = meta.get("sector", "")
            if has_geography:
                rec["Country"] = meta.get("country", "")
            records.append(rec)

        if not records:
            st.info("No executive compensation data found for the selected companies.")
            return

        people_df = pd.DataFrame(records)

        # Drop _source helper col
        people_df.drop(columns=["_source"], inplace=True, errors="ignore")

        # Decide which money columns have any real data and keep only those
        _money_cols = ["Salary", "Bonus", "Stock Awards", "Total Compensation", "Total Pay"]
        _visible_money = [c for c in _money_cols if people_df[c].notna().any()]

        # Always-shown columns
        _base_cols = ["Company", "Ticker", "Executive Name", "Position", "Year"]
        # Optional criteria-driven columns
        _criteria_cols = []
        # Industry is ALWAYS shown (it used to be called "Sector" and appear only
        # with an active Industry criterion). The value is the curated
        # `primary_industry_coresight` carried on the working set — the same field
        # the Key Devs and Companies grids show — so one name means one thing in
        # every mode. Country stays criterion-driven: it has no equivalent backfill.
        if "Industry" in people_df.columns:
            _criteria_cols.append("Industry")
        if has_geography and "Country" in people_df.columns:
            _criteria_cols.append("Country")

        all_display_cols = _base_cols + _criteria_cols + _visible_money
        display_df = people_df[[c for c in all_display_cols if c in people_df.columns]].copy()

        # Format money columns — only rows with actual values (repository guarantees
        # that every row has at least one non-None money value in its source bucket).
        for _mc in _visible_money:
            if _mc in display_df.columns:
                display_df[_mc] = display_df[_mc].apply(
                    lambda v: f"${v:,.0f}" if (v is not None and not pd.isna(v)) else ""
                )

        # Fill None/NaN text cells with empty string (clean, no em-dash clutter)
        for _tc in ["Executive Name", "Position", "Year"] + _criteria_cols:
            if _tc in display_df.columns:
                display_df[_tc] = display_df[_tc].fillna("").astype(str).str.strip()

        # Sort: Company → Year desc (numeric sort on year string) → Total desc
        display_df["_yr_sort"] = pd.to_numeric(display_df["Year"], errors="coerce").fillna(0)
        _total_col = "Total Compensation" if "Total Compensation" in display_df.columns else (
                     "Total Pay" if "Total Pay" in display_df.columns else None)
        if _total_col:
            display_df["_pay_sort"] = display_df[_total_col].apply(
                lambda v: float(v.replace("$", "").replace(",", "")) if isinstance(v, str) and v else 0.0
            )
            display_df.sort_values(["Company", "_yr_sort", "_pay_sort"],
                                   ascending=[True, False, False], inplace=True)
            display_df.drop(columns=["_yr_sort", "_pay_sort"], inplace=True)
        else:
            display_df.sort_values(["Company", "_yr_sort"],
                                   ascending=[True, False], inplace=True)
            display_df.drop(columns=["_yr_sort"], inplace=True)

        # Year filter — People tab only (standalone row above table)
        _all_years = sorted(
            [y for y in display_df["Year"].unique() if y and str(y).strip() not in ("", "nan")],
            reverse=True,
        )
        _year_options = ["All Years"] + [str(y) for y in _all_years]
        _yf_col, _ = st.columns([2, 8])
        with _yf_col:
            _selected_year = st.selectbox(
                "Filter by Year",
                options=_year_options,
                key="scr_people_year_filter",
            )

        # Apply year filter before display
        filtered_df = display_df.copy()
        if _selected_year != "All Years":
            filtered_df = filtered_df[filtered_df["Year"].astype(str) == _selected_year]

        _hdr_col, _dl_col = st.columns([8, 2])
        with _hdr_col:
            st.markdown(
                f"<p class='results-header'>"
                f"<strong>{len(filtered_df)}</strong> executive records across "
                f"<strong>{filtered_df['Ticker'].nunique() if 'Ticker' in filtered_df.columns else len(tickers_in_scope)}</strong> companies</p>",
                unsafe_allow_html=True,
            )
        with _dl_col:
            try:
                from io import BytesIO
                import openpyxl
                from openpyxl.styles import PatternFill, Font, Alignment
                _wb = openpyxl.Workbook()
                _ws = _wb.active
                _ws.title = "People"
                _red_fill  = PatternFill("solid", fgColor="D62E2F")
                _bold_white = Font(bold=True, color="FFFFFF")
                for ci, col_name in enumerate(filtered_df.columns, 1):
                    cell = _ws.cell(row=1, column=ci, value=col_name)
                    cell.fill = _red_fill
                    cell.font = _bold_white
                    cell.alignment = Alignment(horizontal="center")
                for ri, row_data in enumerate(filtered_df.itertuples(index=False), 2):
                    for ci, val in enumerate(row_data, 1):
                        _ws.cell(row=ri, column=ci, value=val)
                for col in _ws.columns:
                    max_len = max((len(str(c.value or "")) for c in col), default=10)
                    _ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)
                _buf = BytesIO()
                _wb.save(_buf)
                _xl_bytes = _buf.getvalue()
                from datetime import datetime
                _ts = datetime.now().strftime("%Y%m%d_%H%M")
                _render_excel_js_download(
                    _xl_bytes,
                    f"People_Screening_{_ts}.xlsx",
                    label="Excel",
                )
            except Exception as _xl_exc:
                log_structured_error(_xl_exc, page="screening",
                                     component="_render_people_results",
                                     operation="excel_export")

        _render_filterable_results_grid(
            filtered_df,
            key="people_results_grid",
            empty_message="No executive compensation records found.",
            pinned_column="Executive Name",
        )

    except Exception as e:
        log_structured_error(e, page="screening", component="_render_people_results",
                             operation="render_people_results")
        st.error("Something went wrong. Please try again.")


def _segment_prefix(seg_type: str) -> str:
    """Return 'Geographic' or 'Business' for column naming."""
    return "Geographic" if seg_type == "geographical" else "Business"


def _segment_metric_label(sc: dict) -> str:
    """Extract the metric label for a segment criterion (e.g. 'Revenues')."""
    mi = sc.get("metric_info") or {}
    return mi.get("label") or sc.get("metric_label") or "Revenues"


def _build_segment_pivot(
    df: pd.DataFrame,
    seg_criteria_group: List[dict],
    seg_type: str,
):
    """Build a pivoted DataFrame for one segment type.

    One row per (company, fiscal_year, segment_name). One value column per metric,
    so multiple metrics on the same segment type become separate columns:
        Company Name | Fiscal Year | <prefix> Segment | <prefix> Revenues ($mm) | <prefix> Assets ($mm)
    Returns (DataFrame, value_columns_list).
    """
    prefix  = _segment_prefix(seg_type)
    seg_col = f"{prefix} Segment"

    # acc[(company, year, segment_name)] = {val_col: value}
    acc: dict = {}
    order: list = []          # preserve insertion order of keys
    val_cols: list = []       # preserve metric column order
    # Track companies with NO segment data so we can still show them as N/A rows
    companies_with_data: set = set()
    all_companies: list = []  # (company, year) in df order

    def _safe_raw(v):
        """Return a dict from a possibly-NaN/non-dict __raw cell."""
        if isinstance(v, dict):
            return v
        return {}

    # Year label for this criterion group (used for N/A rows). All criteria in a
    # group share the same year selection; fall back to "Latest".
    group_year = "Latest"
    for sc in seg_criteria_group:
        _y = sc.get("year")
        if _y:
            group_year = str(_y)
            break

    all_company_set: set = set()
    for sc in seg_criteria_group:
        dc         = sc.get("display_col", "")
        metric_lbl = _segment_metric_label(sc)
        val_col    = f"{prefix} {metric_lbl} ($mm)"
        if val_col not in val_cols:
            val_cols.append(val_col)

        for _, row in df.iterrows():
            exch = row.get("exchange", "")
            tk   = row.get("ticker", "")
            nm   = row.get("company_name", "")
            company = f"{nm} ({exch}:{tk})" if exch else f"{nm} ({tk})"
            raw = _safe_raw(row.get(f"{dc}__raw"))
            _yr_cell = row.get(f"{dc}__year")
            # Guard against NaN/None → use the group year label
            if _yr_cell is None or (isinstance(_yr_cell, float) and pd.isna(_yr_cell)):
                yr = group_year
            else:
                yr = str(_yr_cell)

            if company not in all_company_set:
                all_company_set.add(company)
                all_companies.append((company, yr))

            if raw:
                companies_with_data.add(company)
                for seg_name, val in raw.items():
                    key = (company, yr, seg_name)
                    if key not in acc:
                        acc[key] = {}
                        order.append(key)
                    acc[key][val_col] = val

    rows = []
    for (company, yr, seg_name) in order:
        rec = {"Company Name": company, "Fiscal Year": yr, seg_col: seg_name}
        for vc in val_cols:
            v = acc[(company, yr, seg_name)].get(vc)
            rec[vc] = round(v, 2) if isinstance(v, (int, float)) and v is not None else None
        rows.append(rec)

    # Add N/A rows for companies that matched the universe but have no segment data
    for (company, yr) in all_companies:
        if company not in companies_with_data:
            rec = {"Company Name": company, "Fiscal Year": group_year, seg_col: "N/A"}
            for vc in val_cols:
                rec[vc] = None
            rows.append(rec)

    if not rows:
        return None, val_cols

    pivot = pd.DataFrame(rows)
    pivot = pivot.sort_values("Company Name").reset_index(drop=True)
    for vc in val_cols:
        pivot[vc] = pd.to_numeric(pivot[vc], errors="coerce")
    return pivot, val_cols


def _group_seg_criteria_by_type(seg_criteria: List[dict]) -> "dict":
    """Group segment criteria by segment_type, preserving order."""
    groups: dict = {}
    for sc in seg_criteria:
        dc = sc.get("display_col", "")
        st_type = sc.get("segment_type") or ("geographical" if "Geo" in dc else "business")
        groups.setdefault(st_type, []).append(sc)
    return groups


def _render_segment_expanded_table(
    df: pd.DataFrame,
    criteria: List[dict],
    seg_criteria: List[dict],
) -> None:
    """Render one pivoted table per segment type.

    Multiple metrics on the same segment type become separate value columns.
    Business and Geographical types render as two separate titled tables.
    """
    try:
        groups = _group_seg_criteria_by_type(seg_criteria)
        multiple = len(groups) > 1

        for seg_type, group in groups.items():
            prefix = _segment_prefix(seg_type)
            pivot, val_cols = _build_segment_pivot(df, group, seg_type)
            if pivot is None or pivot.empty:
                continue
            if multiple:
                st.markdown(f"**{prefix} Segments**")
            # Format value columns for display: numbers → "1,234.56", missing → "N/A"
            disp = pivot.copy()
            for vc in val_cols:
                disp[vc] = disp[vc].apply(
                    lambda v: f"{v:,.2f}" if isinstance(v, (int, float)) and pd.notna(v) else "N/A"
                )
            _render_filterable_results_grid(
                disp,
                key=f"segment_{seg_type}_results_grid",
                empty_message="No segment data found.",
                pinned_column="Company Name",
            )
    except Exception as exc:
        log_structured_error(exc, page="screening", component="_render_segment_expanded_table",
                             operation="render_expanded")
        st.error("Could not render expanded segment table.")


def _build_segment_excel(
    df: pd.DataFrame,
    criteria: List[dict],
    seg_criteria: List[dict],
) -> bytes:
    """Build Excel from the pivoted segment tables.

    One worksheet per segment type (Business / Geographical). Each sheet:
      Company Name | Fiscal Year | <prefix> Segment | <metric1> ($mm) | <metric2> ($mm) ...
    Company Name + Fiscal Year cells are merged across each company's segment rows.
    Multiple metrics on the same segment type become separate value columns.
    """
    try:
        from io import BytesIO
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        HDR_BG  = "B31B1B"
        DARK    = "1F2937"
        WHITE   = "FFFFFF"
        ALT_ROW = "FFF5F5"
        thin = Side(border_style="thin", color="D1D5DB")
        thin_border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_font  = Font(name="Inter", size=10, bold=True, color=WHITE)
        hdr_fill  = PatternFill(start_color=HDR_BG, end_color=HDR_BG, fill_type="solid")
        hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
        data_font = Font(name="Inter", size=10, color=DARK)
        num_font  = Font(name="Inter", size=10, color=DARK)

        wb = Workbook()
        first_sheet = True
        groups = _group_seg_criteria_by_type(seg_criteria)

        for seg_type, group in groups.items():
            prefix = _segment_prefix(seg_type)
            pivot, val_cols = _build_segment_pivot(df, group, seg_type)
            if pivot is None or pivot.empty:
                continue

            sheet_title = f"{prefix} Segments"[:31]
            if first_sheet:
                ws = wb.active
                ws.title = sheet_title
                first_sheet = False
            else:
                ws = wb.create_sheet(sheet_title)

            seg_col = f"{prefix} Segment"
            headers = ["Company Name", "Fiscal Year", seg_col] + val_cols
            ws.row_dimensions[1].height = 28
            for ci, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=ci, value=h)
                cell.font = hdr_font; cell.fill = hdr_fill
                cell.alignment = hdr_align; cell.border = thin_border

            # Data rows + merge company / fiscal-year cells
            prev_company = None
            company_start_row = 2
            n = len(pivot)

            for ri in range(n):
                row = pivot.iloc[ri]
                excel_row = ri + 2
                company = row["Company Name"]

                if company != prev_company:
                    if prev_company is not None and (excel_row - company_start_row) > 1:
                        ws.merge_cells(start_row=company_start_row, start_column=1,
                                       end_row=excel_row - 1, end_column=1)
                        ws.merge_cells(start_row=company_start_row, start_column=2,
                                       end_row=excel_row - 1, end_column=2)
                    company_start_row = excel_row
                    prev_company = company

                alt_fill = PatternFill(start_color=ALT_ROW, end_color=ALT_ROW, fill_type="solid") \
                    if (excel_row % 2 == 0) else None

                values = [company, row["Fiscal Year"], row[seg_col]] + [row[vc] for vc in val_cols]
                for ci, v in enumerate(values, 1):
                    # NaN → blank
                    if isinstance(v, float) and pd.isna(v):
                        v = None
                    cell = ws.cell(row=excel_row, column=ci, value=v)
                    cell.font = data_font; cell.border = thin_border
                    if alt_fill: cell.fill = alt_fill
                    if isinstance(v, (int, float)) and v is not None:
                        cell.font = num_font
                        cell.number_format = "#,##0.00"
                        cell.alignment = Alignment(horizontal="right", vertical="center")
                    else:
                        cell.alignment = Alignment(horizontal="left", vertical="center",
                                                   wrap_text=(ci == 1))

            # Merge last company group
            last_row = n + 1
            if last_row > company_start_row:
                ws.merge_cells(start_row=company_start_row, start_column=1,
                               end_row=last_row, end_column=1)
                ws.merge_cells(start_row=company_start_row, start_column=2,
                               end_row=last_row, end_column=2)

            # Column widths
            widths = [45, 12, 40] + [18] * len(val_cols)
            for ci, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(ci)].width = w
            ws.freeze_panes = "A2"

        if first_sheet:
            # No data at all
            ws = wb.active
            ws.title = "Segment Results"
            ws.append(["No segment data."])

        buf = BytesIO(); wb.save(buf); return buf.getvalue()

    except Exception as exc:
        log_structured_error(exc, page="screening", component="_build_segment_excel",
                             operation="build_excel")
        return b""


def _extract_source_url(value) -> str:
    """Normalize Source Reference values to a bare URL (handles legacy HTML anchors)."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("<"):
        match = re.search(r"""href\s*=\s*["']([^"']+)["']""", text, re.IGNORECASE)
        return match.group(1).strip() if match else ""
    return text


def keydev_label_ticker(label: str) -> str:
    """Ticker out of a key-dev "Company Name(s)" label: "Name (EXCH:TICKER)".

    The events frame carries the company as one display string, so both the
    watchlist save and the criterion-column join recover the ticker from it.
    """
    s = str(label or "")
    if "(" in s and s.rstrip().endswith(")"):
        return s[s.rfind("(") + 1:s.rfind(")")].split(":")[-1].strip()
    return ""


def keydev_label_name(label: str) -> str:
    """Company name out of the same label, without the "(EXCH:TICKER)" suffix."""
    s = str(label or "")
    return s[:s.rfind("(")].strip() if "(" in s else s.strip()


def _prepare_keydevs_grid_df(events_df: pd.DataFrame) -> pd.DataFrame:
    """Prepare Key Devs grid data: clean URLs + hidden _source_url for link renderer."""
    if events_df is None or events_df.empty or "Source Reference" not in events_df.columns:
        return events_df
    grid_df = events_df.copy()
    grid_df["_source_url"] = grid_df["Source Reference"].apply(_extract_source_url)
    grid_df["Source Reference"] = grid_df["_source_url"]
    return grid_df


def _apply_grid_column_overrides(
    grid_options: dict,
    *,
    link_columns: Optional[List[str]] = None,
    hidden_columns: Optional[List[str]] = None,
) -> None:
    """Ensure link renderers and hidden columns survive GridOptionsBuilder serialization."""
    link_cols = set(link_columns or [])
    hidden_cols = set(hidden_columns or [])
    for col_def in grid_options.get("columnDefs", []):
        field = col_def.get("field") or col_def.get("headerName")
        if field in hidden_cols:
            col_def["hide"] = True
            col_def["suppressColumnsToolPanel"] = True
        if field in link_cols and _GRID_LINK_RENDERER is not None:
            col_def["cellRenderer"] = _GRID_LINK_RENDERER


def _resolve_grid_pin_column(display_df: pd.DataFrame, pinned_column: Optional[str] = None) -> Optional[str]:
    """Pick the left-pinned column for a screening results grid."""
    if pinned_column and pinned_column in display_df.columns:
        return pinned_column
    for col in _GRID_PIN_COLUMN_PRIORITY:
        if col in display_df.columns:
            return col
    return None


def _render_filterable_results_grid(
    display_df: pd.DataFrame,
    *,
    key: str = "screening_results_grid",
    empty_message: str = "No matching results found.",
    pinned_column: Optional[str] = None,
    link_columns: Optional[List[str]] = None,
    hidden_columns: Optional[List[str]] = None,
    height: int = 520,
    enable_selection: bool = False,
    filter_domains: Optional[Dict[str, List[str]]] = None,
):
    """Render screening results with AG Grid column-menu text filters.

    ``filter_domains`` publishes the COMPLETE set of values for a column, for the
    columns where we know it. Without it the header filter can only offer what the
    loaded page contains — on a 500-row page of a 20,753-row result that hid two
    real subtypes, and since the Excel export now honours the filter, an incomplete
    domain quietly dropped those rows from the download.

    When enable_selection=True, adds a left checkbox column + header select-all and
    returns the AgGrid response (use .selected_rows); otherwise returns None.
    """
    if display_df is None or display_df.empty:
        st.info(empty_message)
        return

    if not HAS_AGGRID:
        st.warning(
            "Interactive column filtering requires streamlit-aggrid. "
            "Falling back to the standard table."
        )
        st.dataframe(display_df, width="stretch", hide_index=True)
        return

    # Monetary columns hold values in $ millions — show ONE legend above the grid
    # instead of repeating "($mm)" in every column header (headers stripped below).
    if any("($mm)" in str(c) for c in display_df.columns):
        st.caption("All monetary figures are in **$ millions (mm)**.")

    pin_col = _resolve_grid_pin_column(display_df, pinned_column)
    link_cols = set(link_columns or [])

    # Excel-style distinct-values filter (custom AG Grid Community component) when
    # available; falls back to the built-in text "contains" filter if JsCode is not.
    _grid_filter = _DISTINCT_VALUES_FILTER if _DISTINCT_VALUES_FILTER is not None else "agTextColumnFilter"

    gb = GridOptionsBuilder.from_dataframe(display_df)
    gb.configure_default_column(
        sortable=True,
        # Distinct-values dropdown (search + "(Select All)" + per-value checkboxes)
        # in the per-column header funnel — Excel-like. Auto-applies on toggle,
        # client-side. The search box subsumes the old "contains" behavior.
        filter=_grid_filter,
        resizable=True,
        # NO floating filter row — filtering stays behind the per-column header
        # funnel (the community filter button, provided by ColumnFilterModule).
        floatingFilter=False,
        # NOTE: do NOT set `menuTabs` here. On AG Grid v34 Community (streamlit-aggrid
        # 1.2.1) `menuTabs` requires the enterprise ColumnMenuModule, so it is ignored
        # and logs error #200 ~4x per grid render. The funnel filter button and
        # header-click ASC/DESC sort are both community features and work without it.
        # Hover ANY cell to read its full (untruncated) value — e.g. the long
        # Summary column. Native browser tooltip (enableBrowserTooltips below).
        tooltipValueGetter=JsCode("function(p){return p.value;}"),
    )
    if pin_col:
        gb.configure_column(
            pin_col,
            filter=_grid_filter,
            sortable=True,
            resizable=True,
            pinned="left",
        )
    hidden_cols = set(hidden_columns or [])
    for hidden_col in hidden_cols:
        if hidden_col in display_df.columns:
            gb.configure_column(
                hidden_col,
                hide=True,
                suppressColumnsToolPanel=True,
            )
    for link_col in link_cols:
        if link_col in display_df.columns and _GRID_LINK_RENDERER is not None:
            gb.configure_column(
                link_col,
                filter=_grid_filter,
                sortable=True,
                resizable=True,
                cellRenderer=_GRID_LINK_RENDERER,
            )
    if enable_selection:
        gb.configure_selection(
            selection_mode="multiple",
            use_checkbox=True,
            header_checkbox=True,
        )
    grid_options = gb.build()
    # configure_selection puts the checkbox on columnDefs[0], but the identity
    # column ("Company Name(s)") is pinned LEFT — so the pinned name column ends
    # up leftmost while the checkbox stays on whatever was column 0 (e.g. "Key
    # Developments By Date"). Move the checkbox onto the pinned column so the
    # selection box sits in the first column the user reads (matches Company
    # Screening). STG 03-Jul: keydev checkbox was showing in column 2.
    if enable_selection and pin_col:
        for _cd in grid_options.get("columnDefs", []):
            _cd.pop("checkboxSelection", None)
            _cd.pop("headerCheckboxSelection", None)
        for _cd in grid_options.get("columnDefs", []):
            if _cd.get("field") == pin_col or _cd.get("headerName") == pin_col:
                _cd["checkboxSelection"] = True
                _cd["headerCheckboxSelection"] = True
                break
    # ── Read / copy long cells (e.g. Summary) ───────────────────────────────
    # AG Grid cells are not editable here, so clicking does nothing by design.
    # enableBrowserTooltips → hover a cell to read its FULL value in a tooltip.
    # enableCellTextSelection + ensureDomOrder → select text in a cell with the
    # mouse and copy it (Cmd/Ctrl+C). Together these answer "how do I see/copy
    # the whole Summary" without an extra dialog.
    grid_options["enableBrowserTooltips"] = True
    grid_options["tooltipShowDelay"] = 200
    grid_options["enableCellTextSelection"] = True
    grid_options["ensureDomOrder"] = True
    # Double-click ANY cell → copy its FULL (untruncated) value to the clipboard,
    # with a brief toast. Deterministic across AG Grid versions and independent of
    # the ellipsis truncation, so an analyst can grab a whole headline / summary /
    # any column — not just the visible part. (Drag-select above still works too.)
    grid_options["onCellDoubleClicked"] = JsCode(
        "function(e){try{"
        "var v=(e&&e.value!=null)?String(e.value):'';"
        "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(v);}"
        "else{var ta=document.createElement('textarea');ta.value=v;document.body.appendChild(ta);"
        "ta.select();document.execCommand('copy');document.body.removeChild(ta);}"
        "var t=document.createElement('div');t.textContent='Copied';"
        "t.style.cssText='position:fixed;bottom:16px;left:50%;transform:translateX(-50%);"
        "background:#2D2A29;color:#fff;padding:6px 14px;border-radius:6px;font-size:13px;"
        "z-index:99999;font-family:Inter,Arial,sans-serif;';"
        "document.body.appendChild(t);setTimeout(function(){t.remove();},1100);"
        "}catch(err){}}"
    )
    _apply_grid_column_overrides(
        grid_options,
        link_columns=list(link_cols),
        hidden_columns=list(hidden_cols),
    )

    # Publish the full value domain for the columns we know it for. The filter
    # unions it with whatever the loaded page holds, so a value can never become
    # unlistable just because it is missing from the domain.
    for _col, _values in (filter_domains or {}).items():
        if not _values:
            continue
        for _cd in grid_options.get("columnDefs", []):
            if _cd.get("field") == _col:
                _cd.setdefault("filterParams", {})["values"] = list(_values)
                break

    # Display-only: strip "($mm)" from headers (the unit is shown once in the
    # legend above). Underlying column keys — and the Excel export — keep the unit.
    for _cd in grid_options.get("columnDefs", []):
        _f = str(_cd.get("field", ""))
        if "($mm)" in _f:
            _cd["headerName"] = _f.replace("($mm)", "").replace("  ", " ").strip()
        # Header-fit floor so auto-size can never truncate the (stripped) label.
        if not _cd.get("hide"):
            _hn = str(_cd.get("headerName") or _f)
            _cd["minWidth"] = max(120, len(_hn) * 8 + 44)
    # Close the column-filter popup on a click ANYWHERE outside the grid, not just
    # inside the table. The grid lives in its own iframe; AG Grid already closes the
    # popup on clicks within the iframe, but clicks elsewhere on the Streamlit page
    # happen in the PARENT document and never reach the iframe, so the popup would
    # stay open. onGridReady wires a capture-phase mousedown listener on the parent
    # (and top) document — those events fire ONLY for clicks outside this iframe — and
    # calls the public api.hideColumnFilter(). Wired once per grid iframe.
    #
    # ── Keep the header filter after the user touches it ────────────────────
    # `GridUpdateMode.FILTERING_CHANGED` round-trips every toggle to Streamlit, which
    # REMOUNTS this component. The rebuilt DistinctValuesFilter used to start with
    # `selected = null`, so the funnel forgot what the user had ticked the moment they
    # ticked it — and the export then disagreed with the grid.
    #
    # The restore needs BOTH halves, because a column filter has two very different
    # states to recover:
    #
    #   1. `filterParams.preselected` — what the popup shows as ticked when the user
    #      REOPENS it. Read by our own buildValues(), so it works whenever AG Grid
    #      gets around to instantiating the filter (it does so lazily, on first open).
    #
    #   2. `api.setFilterModel()` — what the GRID is actually filtered by. This is the
    #      half that was missing, and it is why the rows looked wrong after a rerun:
    #      with the popup closed there is no filter instance, so nothing read
    #      `preselected` and the grid re-rendered UNFILTERED while Python still
    #      reported the filtered count. Measured: banner said "6 of 500 shown rows"
    #      while the grid displayed 13 unrelated industries.
    #
    # It goes in onFirstDataRendered, not onGridReady: at grid-ready there are no rows
    # yet and the call was measured doing nothing. setFilterModel also FORCES the
    # filter to be created, which is exactly what defeats the lazy instantiation.
    # The filterChanged it raises is swallowed by shouldGridReturn (only an explicit
    # afterGuiDetached flush passes), so restoring cannot loop into another rerun.
    #
    # The selection is read from the component's own incoming value
    # (`st.session_state[key]`), which already holds the interaction that caused this
    # rerun — so the filter is restored on the SAME run the user toggled it, not one
    # run late.
    _incoming = _incoming_grid_filter(key)
    _restore_model = (_incoming if _incoming is not None
                      else (st.session_state.get(f"_grid_filter_raw_{key}") or {}))
    if _restore_model:
        for _cd in grid_options.get("columnDefs", []):
            _spec = _restore_model.get(_cd.get("field"))
            if isinstance(_spec, dict) and isinstance(_spec.get("values"), list):
                _cd.setdefault("filterParams", {})["preselected"] = list(_spec["values"])

    # Auto-size each column to its content, clamped to the minWidth above so the
    # full header always fits; the grid scrolls horizontally past the viewport.
    # Same hook re-applies the saved filter (see the two halves above).
    _restore_js = json.dumps({
        _col: {"values": list(_spec["values"])}
        for _col, _spec in _restore_model.items()
        if isinstance(_spec, dict) and isinstance(_spec.get("values"), list)
    })
    grid_options["onFirstDataRendered"] = JsCode(
        "function(p){var a=p.api;"
        "if(a&&a.autoSizeAllColumns){a.autoSizeAllColumns(false);}"
        "else if(p.columnApi&&p.columnApi.autoSizeAllColumns){p.columnApi.autoSizeAllColumns(false);}"
        "try{var m=" + _restore_js + ";"
        "if(a&&a.setFilterModel&&m&&Object.keys(m).length){a.setFilterModel(m);}}catch(e){}}"
    )

    # Close an open column-menu when the user clicks anywhere in the parent
    # (and top) document — those events fire ONLY for clicks outside this iframe — and
    # calls the public api.hideColumnFilter(). Wired once per grid iframe.
    # The first click outside an OPEN filter popup only closes it — it does not also
    # activate whatever was clicked. That is exactly how an Excel dropdown behaves,
    # and here it is also load-bearing for correctness: closing the popup is what
    # flushes the filter to Python (afterGuiDetached), and that flush travels over
    # the websocket. Clicking "Excel" straight from an open popup therefore raced the
    # flush and exported UNFILTERED — measured: 21,548 rows downloaded when only 610
    # matched. Swallowing that first click removes the race entirely; the user clicks
    # Excel again and the model is already in.
    #
    # The listener lives on the PARENT document (clicks outside this iframe never
    # reach it) and is wired ONCE per parent, with the live api/window republished on
    # every mount — the old per-iframe guard re-added a listener on each remount and
    # left every previous one holding a dead api.
    grid_options["onGridReady"] = JsCode(
        "function(params){try{"
        "var docs=[];"
        "try{if(window.parent&&window.parent!==window){docs.push(window.parent.document);}}catch(e){}"
        "try{if(window.top&&window.top!==window&&window.top!==window.parent){docs.push(window.top.document);}}catch(e){}"
        "var gw=window;"
        "docs.forEach(function(d){try{"
        "var w=d.defaultView;"
        "w.__agLiveApi=params.api; w.__agLiveWin=gw;"
        "if(w.__agOutsideCloseWired){return;}"
        "w.__agOutsideCloseWired=true;"
        "d.addEventListener('mousedown',function(ev){try{"
        "var api=w.__agLiveApi, fw=w.__agLiveWin;"
        "var wasOpen=!!(fw&&fw.__agFilterPopupOpen===true);"
        "if(api&&api.hideColumnFilter){api.hideColumnFilter();}"
        "if(wasOpen){"
        "ev.preventDefault(); ev.stopPropagation();"
        "var eat=function(e2){e2.preventDefault(); e2.stopPropagation();"
        "d.removeEventListener('click',eat,true);};"
        "d.addEventListener('click',eat,true);"
        "}"
        "}catch(e){}},true);"
        "}catch(e){}});"
        "}catch(err){}}"
    )

    _update_mode = (
        GridUpdateMode.SELECTION_CHANGED
        | GridUpdateMode.FILTERING_CHANGED
        | GridUpdateMode.SORTING_CHANGED
    ) if enable_selection else (
        GridUpdateMode.FILTERING_CHANGED | GridUpdateMode.SORTING_CHANGED
    )
    # ── Every cell selectable + copyable (ALL screening grids use this helper) ──
    # enableCellTextSelection (set above) is version-fragile — some AG Grid builds
    # stop honoring it, leaving cells unselectable. custom_css injects INTO the grid
    # iframe and forces user-select:text on every cell, so an analyst can drag-select
    # and Cmd/Ctrl+C the FULL value of ANY column (even when visually truncated) —
    # not just Company/Key-Dev. Pairs with enableBrowserTooltips (hover to read the
    # full value). This is deterministic regardless of the AG Grid version deployed.
    _selectable_css = {
        ".ag-cell": {
            "user-select": "text !important",
            "-webkit-user-select": "text !important",
            "-moz-user-select": "text !important",
            "-ms-user-select": "text !important",
            "cursor": "text !important",
        },
        ".ag-cell-value": {
            "user-select": "text !important",
            "-webkit-user-select": "text !important",
        },
        ".ag-cell-value span, .ag-cell span, .ag-cell a": {
            "user-select": "text !important",
            "-webkit-user-select": "text !important",
        },
    }
    # Keyed shell lets CSS mask the AG Grid iframe's grey bootstrap flash
    # (white fill + spinner behind the iframe — see [class*="st-key-scr_grid_shell"]).
    # Key is per-grid so segment mode (several grids in one run) never collides.
    with st.container(key=f"scr_grid_shell_{key}"):
        grid_response = AgGrid(
            display_df,
            gridOptions=grid_options,
            key=key,
            width="100%",
            height=height,
            fit_columns_on_grid_load=False,
            allow_unsafe_jscode=True,
            show_search=False,
            show_toolbar=False,
            show_download_button=False,
            update_mode=_update_mode,
            should_grid_return=_SUPPRESS_FILTER_RERUN,
            data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
            theme="streamlit",
            custom_css=_selectable_css,
        )
    # Remember the filter this grid is carrying so the next mount can restore it
    # (see the onGridReady note above). Stored RAW — the AG Grid shape
    # {col: {"values": [...]}} that `setFilterModel` expects — not the parsed
    # {col: set} that `grid_filter_model` hands the export.
    try:
        st.session_state[f"_grid_filter_raw_{key}"] = _raw_grid_filter_model(grid_response)
    except Exception:
        pass
    return grid_response if enable_selection else None


def _extract_raw_filter_model(state) -> dict:
    """Pull ``{col: {"values": [...]}}`` out of an AG Grid state dict.

    The shape has moved between AG Grid versions (``state.filter.filterModel`` vs a
    flat ``filterModel``), so both are accepted, and anything we cannot read is
    dropped rather than guessed at.
    """
    if not isinstance(state, dict):
        return {}
    raw = None
    filt = state.get("filter")
    if isinstance(filt, dict):
        raw = filt.get("filterModel")
    if raw is None:
        raw = state.get("filterModel")
    if not isinstance(raw, dict):
        return {}
    return {c: sp for c, sp in raw.items()
            if isinstance(sp, dict) and isinstance(sp.get("values"), list)}


def _incoming_grid_filter(key: str) -> Optional[dict]:
    """The filter the grid is arriving WITH, read before it is re-rendered.

    Streamlit puts a component's current value in ``st.session_state[key]``, so on the
    rerun caused by a filter toggle this already holds that toggle. Reading it here —
    rather than from the AgGrid return value, which only lands after the grid has been
    built — is what lets the selection be restored on the SAME run the user made it.

    Returns ``None`` when the component has not reported yet, which is NOT the same as
    ``{}`` — that means it reported and there is no filter, i.e. the user just cleared
    it. Collapsing the two (``incoming or persisted``) made clearing impossible: the
    empty model fell through to the previously persisted one and the old filter came
    straight back. That is how ticking (Select All) left the grid stuck on the last
    industry instead of returning to the per-industry page.
    """
    val = st.session_state.get(key)
    if val is None:
        return None
    state = None
    try:
        state = val.grid_state
    except Exception:
        if isinstance(val, dict):
            state = val.get("grid_state") or val.get("gridState")
    return _extract_raw_filter_model(state)


def _raw_grid_filter_model(grid_resp) -> dict:
    """The grid's filter model in AG Grid's OWN shape, read from the AgGrid return.

    Stored after each render as the fallback restore source; `_incoming_grid_filter`
    is preferred because it is available before the grid is rebuilt.
    """
    if grid_resp is None:
        return {}
    try:
        return _extract_raw_filter_model(grid_resp.grid_state or {})
    except Exception:
        return {}


def grid_filter_model(grid_resp) -> dict:
    """The grid's active column filters as ``{column: {allowed display values}}``.

    The header funnel is our own DistinctValuesFilter, whose ``getModel()`` returns
    ``{"values": [...]}`` per filtered column and ``null`` when that column is not
    filtered — so an empty dict here means "nothing is filtered", never "unknown".

    AG Grid exposes it through the grid state; the shape has moved between versions
    (``state.filter.filterModel`` vs a flat ``filterModel``), so both are accepted.
    Anything unrecognised is skipped rather than guessed at — a filter we cannot
    read must not silently narrow an export.
    """
    if grid_resp is None:
        return {}
    try:
        state = grid_resp.grid_state or {}
    except Exception:
        return {}

    raw = None
    if isinstance(state, dict):
        filt = state.get("filter")
        if isinstance(filt, dict):
            raw = filt.get("filterModel")
        if raw is None:
            raw = state.get("filterModel")
    if not isinstance(raw, dict) or not raw:
        return {}

    model: dict = {}
    for col, spec in raw.items():
        values = spec.get("values") if isinstance(spec, dict) else None
        if isinstance(values, list):
            model[col] = {"" if v is None else str(v) for v in values}
    return model


def apply_grid_filter_model(df: pd.DataFrame, model: dict) -> pd.DataFrame:
    """Re-apply the grid's column filters to a DataFrame, server-side.

    The grid holds only the loaded page, so filtering there and exporting the
    result would hand back a slice of 500 rows when the filter really matches
    thousands. Running the same model over the FULL fetched set instead gives the
    user every row their filter matches. Values are compared as the display
    strings the grid itself filtered on, so the two can never disagree.
    """
    if df is None or df.empty or not model:
        return df
    keep = pd.Series(True, index=df.index)
    for col, allowed in model.items():
        if col not in df.columns:
            continue
        keep &= df[col].fillna("").astype(str).isin(allowed)
    return df[keep].reset_index(drop=True)


def _normalize_selected_rows(grid_resp) -> List[dict]:
    """Extract selected rows (list of dicts) from an AgGrid response, version-safe."""
    if grid_resp is None:
        return []
    sel = None
    try:
        sel = grid_resp.selected_rows
    except Exception:
        try:
            sel = grid_resp["selected_rows"]
        except Exception:
            sel = None
    if sel is None:
        return []
    if isinstance(sel, pd.DataFrame):
        return [] if sel.empty else sel.to_dict("records")
    if isinstance(sel, list):
        return sel
    return []


def _render_save_as_watchlist_panel(grid_resp) -> None:
    """Save the checkbox-selected result companies as a new watchlist.

    Reuses the real watchlist save path (create_watchlist + add_companies_to_watchlist).
    Selection comes from the AG Grid checkboxes; nothing else in the results flow changes.
    """
    try:
        sel = _normalize_selected_rows(grid_resp)
        with st.expander(f"Save selection as Watchlist  ·  {len(sel)} selected", expanded=False):
            with st.form("scr_save_wl_form", clear_on_submit=False):
                c1, c2, c3 = st.columns([3, 5, 2])
                with c1:
                    name = st.text_input("Watchlist name", key="scr_wl_save_name", max_chars=100)
                with c2:
                    desc = st.text_input("Description (optional)", key="scr_wl_save_desc")
                with c3:
                    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                    submitted = st.form_submit_button("Save as Watchlist", type="primary", width="stretch")
            if submitted:
                if not sel:
                    st.warning("Select at least one company using the checkboxes on the left.")
                elif not (name or "").strip():
                    st.warning("Please enter a watchlist name.")
                else:
                    email = _current_user_email()
                    new_id = _real_wl_create(name.strip(), email, (desc or "").strip())
                    if not new_id:
                        st.error("Could not create the watchlist. Please try again.")
                    else:
                        # Dedupe by ticker — Key Devs selections can include
                        # several event rows for the same company.
                        companies = []
                        _seen_tk = set()
                        for r in sel:
                            tk = (r.get("Ticker") or "").strip()
                            if not tk or tk.upper() in _seen_tk:
                                continue
                            _seen_tk.add(tk.upper())
                            companies.append({
                                "ticker":       tk,
                                "company_name": (r.get("_CompanyName") or r.get("Company Name") or "").strip(),
                                "sector":       (r.get("_Sector") or "").strip(),
                                "country":      (r.get("_Country") or "").strip(),
                            })
                        added = _real_wl_add_companies(new_id, companies, email)
                        st.success(
                            f"Saved watchlist '{name.strip()}' with {added} "
                            f"compan{'y' if added == 1 else 'ies'}."
                        )
    except Exception as e:
        log_structured_error(e, page="screening",
                             component="_render_save_as_watchlist_panel", operation="save_watchlist")
        st.error("Could not save the watchlist. Please check logs.")


def _render_results():
    """Render Show Results button and results table."""
    try:
        st.markdown("---")
        criteria = st.session_state.get("scr_active_criteria", [])
        wl_active = bool(st.session_state.get("scr_active_watchlist_id"))
        _sync_criteria_fingerprint()

        _render_saved_criteria_controls()

        col_btn, col_clear, col_space = st.columns([1, 0.8, 8.2])

        with col_btn:
            show_clicked = st.button(
                "Show Results",
                type="primary",
                width="stretch",
                disabled=(len(criteria) == 0 and not wl_active),
            )
            if show_clicked:
                log_timing("SHOW_RESULTS_CLICK", 0, f"criteria={len(criteria)}")
                _rerun_n = st.session_state.get("_rerun_count_screening", 0)
                log_warning(
                    f"[SHOW_RESULTS] criteria={len(criteria)} "
                    f"page_rerun#={_rerun_n} watchlist={wl_active}"
                )
                st.session_state.scr_results_loading = True
                st.session_state.scr_results_pending_paint = True
                st.session_state.scr_show_results_requested = True
                st.session_state.pop("scr_results_error", None)
                st.rerun()

        with col_clear:
            if st.button("Clear All", width="stretch",
                         disabled=(len(criteria) == 0 and not wl_active)):
                _reset_criteria()
                st.rerun()

        if st.session_state.get("scr_results_loading"):
            _has_segment_criterion = any(
                c.get("statement") in SEGMENT_STATEMENT_TYPES
                for c in criteria
                if c.get("type") == "financial"
            )
            _render_coresight_loading_overlay(
                "Loading screening results...",
                (
                    "Using the segment values cache; missing cache fails fast."
                    if _has_segment_criterion
                    else "Preparing your results."
                ),
            )
            if st.session_state.get("scr_results_pending_paint"):
                st.session_state.scr_results_pending_paint = False
                time.sleep(0.6)
                st.rerun()
            try:
                if criteria and _needs_results_recompute():
                    _trigger_recompute()
                elif not criteria:
                    wl_members = st.session_state.get("scr_watchlist_members")
                    if wl_members:
                        st.session_state.scr_working_df = filter_universe_to_members(
                            get_base_company_universe(), wl_members
                        )
                        st.session_state.scr_last_computed_fingerprint = (
                            st.session_state.get("scr_criteria_fingerprint")
                        )
                st.session_state.scr_show_results = True
            except Exception as exc:
                log_structured_error(
                    exc,
                    page="screening",
                    component="_render_results",
                    operation="show_results_recompute",
                )
                st.session_state.scr_results_error = str(exc)
                st.session_state.scr_show_results = False
            finally:
                st.session_state.scr_results_loading = False
                st.session_state.scr_show_results_requested = False
                _final_rerun = st.session_state.get("_rerun_count_screening", 0)
                log_timing(
                    "SHOW_RESULTS_RERUN_TOTAL",
                    0.0,
                    f"page_rerun#={_final_rerun} criteria={len(criteria)}",
                )
                st.rerun()

        if st.session_state.get("scr_results_error"):
            _err = str(st.session_state.scr_results_error)
            if SEGMENT_CACHE_UNAVAILABLE_MESSAGE in _err:
                st.warning(SEGMENT_CACHE_UNAVAILABLE_MESSAGE)
            else:
                st.error("Could not load screening results. Please check logs.")
            if os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"):
                with st.expander("Technical details"):
                    st.code(_err)
            return

        if not st.session_state.get("scr_show_results"):
            return

        t_render_total = time.perf_counter()
        df: Optional[pd.DataFrame] = st.session_state.get("scr_working_df")

        if df is None or len(df) == 0:
            st.info("No companies match the current criteria. Try relaxing a filter.")
            return

        seen_cols: set = set()
        dedup_cols = []
        for col in df.columns:
            if col not in seen_cols:
                dedup_cols.append(col)
                seen_cols.add(col)
        df = df[dedup_cols]

        financial_cols  = _get_financial_metric_cols(criteria)
        keydevs_cols    = _get_keydevs_cols(criteria)
        biz_seg_cols    = _get_biz_segments_cols(criteria)
        geo_seg_cols    = _get_geo_segments_cols(criteria)
        additional_cols = _get_additional_cols(criteria)
        metric_cols = _criteria_display_cols(criteria, df.columns)

        for _crit in criteria:
            _dcol = _crit.get("display_col")
            if _dcol and _dcol not in df.columns:
                df[_dcol] = None
                if _dcol in metric_cols:
                    pass
                elif _dcol in (financial_cols + keydevs_cols + biz_seg_cols + geo_seg_cols + additional_cols):
                    metric_cols.append(_dcol)

        if "company_name" in df.columns:
            df = df.sort_values("company_name")

        t_df_build = time.perf_counter()
        display_df = _build_results_display_df(df, criteria, metric_cols)
        log_timing(
            "SCREENING_RESULTS_DISPLAY_DF_BUILD",
            (time.perf_counter() - t_df_build) * 1000,
            f"rows={len(display_df)} cols={len(display_df.columns)}",
        )

        # Determine if we have segment criteria with raw dict data
        seg_criteria = [
            c for c in criteria
            if c.get("type") == "financial" and c.get("statement") in SEGMENT_STATEMENT_TYPES
        ]
        has_segment_results = bool(seg_criteria) and any(
            f"{c['display_col']}__raw" in df.columns
            for c in seg_criteria if c.get("display_col")
        )

        _hdr_col, _dl_col = st.columns([8, 2])
        with _hdr_col:
            st.markdown(
                f"<p class='results-header'>"
                f"<strong>{len(display_df)}</strong> companies matched your criteria</p>",
                unsafe_allow_html=True,
            )
        with _dl_col:
            # One-step download: build the workbook in a background thread keyed
            # by a signature of the current result set, then surface the download
            # button the moment it is ready — the page never blocks on it.
            import hashlib as _hashlib
            _sig_src = "|".join([
                str(len(display_df)),
                ",".join(map(str, display_df.columns)),
                "seg" if has_segment_results else "flat",
                ",".join(
                    f"{c.get('display_col')}:{c.get('operator')}:{c.get('value1')}:{c.get('value2')}"
                    for c in criteria
                ),
            ])
            _sig = _hashlib.md5(_sig_src.encode("utf-8")).hexdigest()
            if has_segment_results:
                _builder = lambda: _build_segment_excel(df, criteria, seg_criteria)
            else:
                _builder = lambda: _build_screening_excel(display_df, len(criteria))
            _render_excel_download(_sig, _builder)

        # Analytics: a screen with real criteria was executed → record run + result
        # count (best-effort, deduped on the result-set signature so re-renders of the
        # same result set don't repeat).
        try:
            if criteria:
                from utils.server_logger import track_action
                track_action("screen_run", page="screening",
                             results=len(display_df), criteria_count=len(criteria),
                             segment=has_segment_results, dedupe_key=_sig)
        except Exception:
            pass

        t_table = time.perf_counter()
        if has_segment_results:
            _render_segment_expanded_table(df, criteria, seg_criteria)
        else:
            # Build a selection-enabled copy carrying hidden identity columns so the
            # checkbox-selected rows can be saved as a watchlist (E3). The original
            # display_df (used for Excel + matched count) is untouched.
            _sel_df = display_df.copy()
            try:
                _sel_df["Ticker"] = df["ticker"].to_numpy()
                _sel_df["_CompanyName"] = (
                    df["company_name"].to_numpy() if "company_name" in df.columns
                    else _sel_df["Company Name"].to_numpy()
                )
                _sel_df["_Sector"] = df["sector"].to_numpy() if "sector" in df.columns else ""
                _sel_df["_Country"] = df["country"].to_numpy() if "country" in df.columns else ""
                _hidden = ["Ticker", "_CompanyName", "_Sector", "_Country"]
            except Exception:
                _sel_df, _hidden = display_df.copy(), []
            _grid_resp = _render_filterable_results_grid(
                _sel_df,
                key="companies_results_grid",
                empty_message="No matching companies found.",
                pinned_column="Company Name",
                hidden_columns=_hidden,
                enable_selection=True,
            )
            _render_save_as_watchlist_panel(_grid_resp)
        log_timing(
            "SCREENING_RESULTS_TABLE_RENDER",
            (time.perf_counter() - t_table) * 1000,
            f"rows={len(display_df)}",
        )
        log_timing(
            "RESULTS_RENDER_TOTAL",
            (time.perf_counter() - t_render_total) * 1000,
            f"rows={len(display_df)} metric_cols={len(metric_cols)}",
        )
    except Exception as e:
        log_structured_error(e, page="screening", component="_render_results", operation="render_results")
        st.session_state.scr_results_error = str(e)
        st.error("Could not load screening results. Please check logs.")
        if os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"):
            with st.expander("Technical details"):
                st.code(str(e))


# =============================================================================
# MAIN PAGE ENTRYPOINT
# =============================================================================

def main():
    try:
        t0 = time.perf_counter()
        new_rerun_id("screening")
        _tracker = PageLoadTracker("screening") if PageLoadTracker else None

        # ── Initialize session state ──
        _init_state()

        # ── Flush any pending toast notifications (queued before last rerun) ──
        _flush_pending_toasts()

        # ── Bootstrap DB tables (once per server lifetime) ──
        _ensure_db_tables()

        # ── Page header ──
        _render_page_header()

        # ── Screen For selector ──
        _render_screen_for()

        # ── Companies flow ──
        screen_for = st.session_state.get("scr_screen_for", "Companies")

        if screen_for in ("Companies", "Key Devs", "People"):

            # ── Watchlist bar (above criteria palette, not for People) ──
            if screen_for != "People":
                _render_watchlist_bar()
            else:
                # People mode: still allow watchlist-based filtering
                _render_watchlist_bar()

            # Open ONE dialog at a time (Streamlit allows only one)
            if st.session_state.get("wl_dlg_open"):
                _dialog_watchlist_manager()
            elif st.session_state.get("scr_saved_dlg_open"):
                _dialog_browse_saved_criteria()

            # Criteria palette (shared: Companies / Key Devs / People)
            # People uses only Industry + Geography criteria (financial optional)
            _render_criteria_palette()

            # Active criterion configuration form (if a palette button was clicked)
            if st.session_state.get("scr_active_form"):
                _render_criterion_form()

            # Status bar
            _render_status_bar()

            # Active criteria stack
            _render_active_criteria()

            # Analytics: screening writes NOTHING to the URL, so the auto page_view had
            # zero filter data. Capture the mode + the active criteria (type + summary).
            try:
                from utils.server_logger import log_filters_if_changed
                _acrit = st.session_state.get("scr_active_criteria", []) or []
                log_filters_if_changed(
                    "screening",
                    screen_for=screen_for,
                    criteria_count=len(_acrit),
                    criteria=[
                        {"type": _c.get("type"),
                         "summary": (_c.get("summary") or _c.get("display_col")
                                     or _c.get("statement") or _c.get("metric_label"))}
                        for _c in _acrit if isinstance(_c, dict) and not _c.get("hidden")
                    ] or None,
                    watchlist=st.session_state.get("scr_active_watchlist_name") or None,
                )
            except Exception:
                pass

            # Results — mode-specific rendering
            if screen_for == "Companies":
                _render_results()
            elif screen_for == "Key Devs":
                _render_keydevs_results()
            else:  # People
                _render_people_results()

        else:
            st.info(
                f"**{screen_for} Screening** is coming soon. "
                "Select Companies or People to start screening."
            )

        # ── Footer ──
        render_coresight_footer(full_width=True, stick_to_bottom=True)
        if _tracker:
            _tracker.finish()
        log_render_complete("screening", time.perf_counter() - t0)

    except Exception as e:
        log_structured_error(e, page="screening", component="main", operation="main")
        st.error("Something went wrong. Please refresh.")


main()
