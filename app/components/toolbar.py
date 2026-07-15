# """
# Navigation Toolbar Component
# Based on Figma design node 20862-187662
# """
# import streamlit as st


# def inject_toolbar(active_page: str = "Company Profile") -> None:
#     """
#     Inject a sticky navigation toolbar for the Market Data section.

#     Args:
#         active_page: The currently active page name.
#                      Options: "Company Profile", "Key Stats", "Income Statement", "Balance Sheet", "Cash Flow"
#     """

#     # Define pages and their corresponding URLs
#     # Unified navigation - all on single port
#     pages = [
#         ("Company Profile", "/company_profile"),
#         ("Key Stats", "/market_data?tab=key_stats"),
#         ("Income Statement", "/market_data?tab=income_statement"),
#         ("Balance Sheet", "/market_data?tab=balance_sheet"),
#         ("Cash Flow", "/market_data?tab=cash_flow"),
#     ]

#     # Generate toolbar HTML
#     toolbar_html = f"""
#     <style>
#         /* Toolbar Container - Sticky, full width with 110px side padding */
#         .toolbar-container {{
#             background-color: #FFFFFF;
#             padding: 32px 110px 1px 110px;
#             position: sticky;
#             top: 0;
#             z-index: 1000;
#             border-bottom: 1px solid #CBCACA;
#             display: flex;
#             flex-direction: column;
#             align-items: flex-start;
#             gap: 8px;
#             width: 100%;
#             box-sizing: border-box;
#         }}

#         /* Links Container - left aligned, matches Figma spec */
#         .toolbar-links {{
#             display: flex;
#             gap: 105px;
#             align-items: flex-start;
#             width: 100%;
#             max-width: 1220px;
#         }}

#         /* Individual Link */
#         .toolbar-link {{
#             font-family: 'Roboto', sans-serif;
#             font-weight: 600;
#             font-size: 18px;
#             color: #2D2A29 !important;
#             text-decoration: none !important;
#             position: relative;
#             padding-bottom: 2px;
#             white-space: nowrap;
#         }}

#         /* Active Link - Red color */
#         .toolbar-link.active {{
#             color: #D62E2F !important;
#         }}

#         /* Active Link - Red underline */
#         .toolbar-link.active::after {{
#             content: '';
#             position: absolute;
#             bottom: -8px;
#             left: 0;
#             width: 100%;
#             height: 4px;
#             background-color: #D62E2F;
#         }}

#         /* Hover effect */
#         .toolbar-link:hover {{
#             color: #D62E2F !important;
#             text-decoration: none !important;
#         }}

#         /* Bottom border line (gray for inactive) */
#         .toolbar-border {{
#             height: 4px;
#             width: 100%;
#             max-width: 1220px;
#             background-color: transparent;
#             margin-top: -4px;
#         }}
#     </style>

#     <div class="toolbar-container">
#         <div class="toolbar-links">
#             {''.join([
#                 f'<a href="{url}" target="_self" class="toolbar-link {"active" if name == active_page else ""}">{name}</a>'
#                 for name, url in pages
#             ])}
#         </div>
#         <div class="toolbar-border"></div>
#     </div>
#     """

#     st.markdown(toolbar_html, unsafe_allow_html=True)


# # Example usage
# if __name__ == "__main__":
#     st.set_page_config(layout="wide")

#     # Test the toolbar with different active pages
#     inject_toolbar(active_page="Key Stats")

#     # Add some content to demonstrate sticky behavior
#     for i in range(50):
#         st.write(f"Content line {i + 1}")

import streamlit as st
from .styles import render_styles
from utils.local_storage import set_marketdata_tab

try:
    from utils.server_logger import log_structured_error, timed_render
except ImportError:
    def log_structured_error(exc, **kw): pass  # type: ignore

def render_tabs(selected_tab: str) -> str:
    with timed_render("TOOLBAR_TABS"):
        return _render_tabs_inner(selected_tab)


def _render_tabs_inner(selected_tab: str) -> str:
    try:
        tabs = ["company_profile", "key_stats", "income_statement", "balance_sheet", "cash_flow", "ratios", "estimates", "forecasting", "segment_data", "ratings"]
        tab_labels = ["Company Profile", "Key Stats", "Income Statement", "Balance Sheet", "Cash Flow", "Ratios", "Estimates", "Forecasting", "Segment", "Additional Data"]
        # Tabs that show a "Testing in Progress" label above
        _dev_in_progress_tabs = {"forecasting"}

        _col_widths = [1.2, 0.8, 1.2,1, 0.8, 1, 1, 1, 1, 1]

        # ── Row 1: "Testing in Progress" labels above dev tabs ──
        label_cols = st.columns(_col_widths)
        _dev_label_html = '<div style="text-align:center;color:#d62e2f;font-size:9px;font-weight:700;font-family:\'Roboto\',sans-serif;letter-spacing:0.3px;margin-bottom:-14px;white-space:nowrap;">Testing in Progress</div>'
        for i, tab_key in enumerate(tabs):
            with label_cols[i]:
                if tab_key in _dev_in_progress_tabs:
                    st.markdown(_dev_label_html, unsafe_allow_html=True)
                else:
                    # Empty placeholder to keep alignment
                    st.markdown('<div style="height:1px;margin-bottom:-14px;"></div>', unsafe_allow_html=True)

        # ── Row 2: Tab buttons / active indicators ──
        cols = st.columns(_col_widths)

        def _make_tab_click(tab_key):
            """Return an on_click callback that sets query_params and local storage."""
            def _on_click():
                st.query_params["tab"] = tab_key
                set_marketdata_tab(tab_key)
            return _on_click

        for i, (tab_key, label) in enumerate(zip(tabs, tab_labels)):
            with cols[i]:
                is_active = selected_tab == tab_key

                if is_active:
                    st.markdown(f"""
                    <div style="
                        color: #d62e2f;
                        font-weight: 600;
                        font-size: 16px;
                        border-bottom: 2px solid #d62e2f;
                        text-align: center;
                        height: 42px;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        white-space: nowrap;
                    ">
                    {label}
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.button(
                        label,
                        key=f"tab_{tab_key}",
                        type="tertiary",
                        width="stretch",
                        on_click=_make_tab_click(tab_key),
                    )

        st.markdown("""
            <style>
                .tabs-hr-wrapper {
                    margin-top: -1.5rem;
                }
                /* Force inactive dev tab buttons to dark text (prevent Streamlit state color leak) */
                [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(9) button[kind="tertiary"],
                [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(10) button[kind="tertiary"] {
                    color: rgb(26, 26, 46) !important;
                    border-color: transparent !important;
                }
                /* No-wrap for Additional Data button */
                [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(10) button p {
                    white-space: nowrap !important;
                }
            </style>
            <div class="tabs-hr-wrapper">
                <hr style="border: none; border-top: 2px solid #dee2e6; margin: 0;">
            </div>
        """, unsafe_allow_html=True)

        return selected_tab
    except Exception as e:
        log_structured_error(e, page="toolbar", component="render_tabs", operation="RENDER_TABS")
        return selected_tab
