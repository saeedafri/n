"""
Reusable layout components for consistent page structure.
"""
import streamlit as st
from typing import Optional, List, Callable, Any
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from components.styles import COLORS, TYPOGRAPHY, SPACING, BORDER_RADIUS, SHADOWS
from utils.server_logger import log_structured_error


# def render_header(title: str, subtitle: Optional[str] = None):
#     """Render page header with consistent styling."""
#     st.markdown(f"""
#     <div style="
#         margin-bottom: {SPACING['space_8']};
#         padding-bottom: {SPACING['space_6']};
#         border-bottom: 1px solid {COLORS['gray_200']};
#     ">
#         <h1 style="
#             font-size: {TYPOGRAPHY['text_3xl']};
#             font-weight: {TYPOGRAPHY['font_bold']};
#             color: {COLORS['gray_900']};
#             margin: 0 0 {SPACING['space_2']} 0;
#         ">{title}</h1>
#         {f'<p style="font-size: {TYPOGRAPHY["text_lg"]}; color: {COLORS["gray_600"]}; margin: 0;">{subtitle}</p>' if subtitle else ''}
#     </div>
#     """, unsafe_allow_html=True)


def render_card(
    content: str,
    title: Optional[str] = None,
    padding: str = SPACING["space_6"],
    shadow: str = SHADOWS["shadow"],
    border_radius: str = BORDER_RADIUS["rounded_xl"],
    background: str = COLORS["white"]
):
    """Render a styled card component."""
    try:
        title_html = f"""
        <div style="
            font-size: {TYPOGRAPHY['text_lg']};
            font-weight: {TYPOGRAPHY['font_semibold']};
            color: {COLORS['gray_900']};
            margin-bottom: {SPACING['space_4']};
            padding-bottom: {SPACING['space_3']};
            border-bottom: 1px solid {COLORS['gray_200']};
        ">{title}</div>
        """ if title else ""

        st.markdown(f"""
        <div style="
            background: {background};
            border-radius: {border_radius};
            padding: {padding};
            box-shadow: {shadow};
            border: 1px solid {COLORS['gray_200']};
            margin-bottom: {SPACING['space_4']};
        ">
            {title_html}
            <div style="color: {COLORS['gray_700']}; line-height: {TYPOGRAPHY['leading_relaxed']};">
                {content}
            </div>
        </div>
        """, unsafe_allow_html=True)
    except Exception as e:
        log_structured_error(e, page="layout", component="render_card", operation="rendering card component")
        st.error("Failed to render card.")


def render_metric_card(
    label: str,
    value: str,
    change: Optional[str] = None,
    change_positive: bool = True,
    icon: Optional[str] = None,
    help_text: Optional[str] = None
):
    """Render a metric card with value and optional change indicator."""
    # Build the change indicator HTML
    change_html = ""
    if change:
        change_color = COLORS["success"] if change_positive else COLORS["danger"]
        arrow = "↑" if change_positive else "↓"
        change_html = f'<span style="color: {change_color}; font-weight: 600; font-size: 0.875rem; margin-left: 8px;">{arrow} {change}</span>'

    icon_html = f"{icon} " if icon else ""
    help_html = f'<div style="font-size: 0.75rem; color: {COLORS["gray_500"]}; margin-top: 8px;">{help_text}</div>' if help_text else ""

    # Use columns for layout to avoid HTML escaping issues
    with st.container():
        st.markdown(f"""
        <div style="
            background: {COLORS['white']};
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border: 1px solid {COLORS['gray_200']};
            height: 100%;
        ">
            <div style="
                font-size: 0.875rem;
                color: {COLORS['gray_600']};
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 8px;
            ">{icon_html}{label}</div>
            <div>
                <span style="
                    font-size: 1.75rem;
                    font-weight: 700;
                    color: {COLORS['gray_900']};
                ">{value}</span>
                {change_html}
            </div>
            {help_html}
        </div>
        """, unsafe_allow_html=True)


def render_divider():
    """Render a horizontal divider."""
    st.markdown(f"""
    <div style="
        height: 1px;
        background: {COLORS['gray_200']};
        margin: 24px 0;
    "></div>
    """, unsafe_allow_html=True)


def render_badge(
    text: str,
    variant: str = "primary",
    icon: Optional[str] = None
):
    """Render a badge with variant styling."""
    variant_styles = {
        "primary": (COLORS["primary_light"], COLORS["primary"]),
        "success": (COLORS["success_light"], COLORS["success"]),
        "warning": (COLORS["warning_light"], "#856404"),
        "danger": (COLORS["danger_light"], COLORS["danger"]),
        "info": (COLORS["info_light"], COLORS["info"]),
    }

    bg_color, text_color = variant_styles.get(variant, variant_styles["primary"])
    icon_html = f"{icon} " if icon else ""

    st.markdown(f"""
    <span style="
        display: inline-flex;
        align-items: center;
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        background: {bg_color};
        color: {text_color};
    ">{icon_html}{text}</span>
    """, unsafe_allow_html=True)


def render_empty_state(
    title: str,
    description: str,
    icon: str = "📊",
    action_label: Optional[str] = None,
    action_callback: Optional[Callable] = None
):
    """Render an empty state with optional action."""
    st.markdown(f"""
    <div style="
        text-align: center;
        padding: 64px 32px;
    ">
        <div style="
            font-size: 4rem;
            margin-bottom: 16px;
        ">{icon}</div>
        <h3 style="
            font-size: 1.25rem;
            font-weight: 600;
            color: {COLORS['gray_900']};
            margin: 0 0 8px 0;
        ">{title}</h3>
        <p style="
            font-size: 1rem;
            color: {COLORS['gray_600']};
            margin: 0;
            max-width: 400px;
            margin-left: auto;
            margin-right: auto;
        ">{description}</p>
    </div>
    """, unsafe_allow_html=True)

    if action_label and action_callback:
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button(action_label, type="primary", width="stretch"):
                action_callback()


def render_loading_skeleton(height: str = "200px"):
    """Render a loading skeleton placeholder."""
    st.markdown(f"""
    <div style="
        background: {COLORS['gray_100']};
        border-radius: 12px;
        height: {height};
        animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    "></div>
    <style>
    @keyframes pulse {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: .5; }}
    }}
    </style>
    """, unsafe_allow_html=True)


def render_tabs(tabs_config: List[tuple[str, str, Callable]], default_tab: int = 0):
    """
    Render custom tabs with content.

    Args:
        tabs_config: List of (tab_id, tab_label, content_renderer) tuples
        default_tab: Index of default active tab
    """
    tab_labels = [label for _, label, _ in tabs_config]
    tabs = st.tabs(tab_labels)

    for i, (tab_id, _, content_renderer) in enumerate(tabs_config):
        with tabs[i]:
            content_renderer()


def render_filter_bar(
    filters: List[tuple[str, str, Any]],
    on_change: Optional[Callable] = None
):
    """
    Render a filter bar with multiple filter inputs.

    Args:
        filters: List of (filter_id, filter_label, filter_component) tuples
        on_change: Callback when any filter changes
    """
    st.markdown(f"""
    <div style="
        background: {COLORS['white']};
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        border: 1px solid {COLORS['gray_200']};
        margin-bottom: 24px;
    ">
        <div style="
            font-size: 0.875rem;
            font-weight: 600;
            color: {COLORS['gray_700']};
            margin-bottom: 16px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        ">Filters</div>
    </div>
    """, unsafe_allow_html=True)

    cols = st.columns(len(filters))
    for i, (filter_id, label, default_value) in enumerate(filters):
        with cols[i]:
            st.text_input(label, value=default_value, key=f"filter_{filter_id}")


def render_pagination(
    current_page: int,
    total_pages: int,
    on_page_change: Callable[[int], None]
):
    """Render pagination controls."""
    col1, col2, col3 = st.columns([1, 2, 1])

    with col1:
        st.button(
            "← Previous",
            disabled=current_page <= 1,
            on_click=lambda: on_page_change(current_page - 1),
            width="stretch"
        )

    with col2:
        st.markdown(f"""
        <div style="
            text-align: center;
            padding: 8px;
            font-size: 0.875rem;
            color: {COLORS['gray_600']};
        ">Page {current_page} of {total_pages}</div>
        """, unsafe_allow_html=True)

    with col3:
        st.button(
            "Next →",
            disabled=current_page >= total_pages,
            on_click=lambda: on_page_change(current_page + 1),
            width="stretch"
        )
