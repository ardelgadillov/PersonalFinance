"""Plotly figure styling shared by the pages: one look for light and dark themes.

Colors follow the data's job (validated categorical slots; both modes selected, not flipped):
income = slot 1 (blue), spending = slot 2 (orange), savings = slot 3 (aqua).
Text, grid and axes use the same neutrals as the CSS `--app-*` variables.
"""
from __future__ import annotations

import plotly.graph_objects as go

THEMES = {
    "light": {
        "text": "#111827", "muted": "#6b7280", "grid": "#eef0f3", "axis": "#d1d5db", "surface": "#ffffff",
        "income": "#2a78d6", "spending": "#eb6834", "savings": "#1baf7a", "neutral": "#9ca3af",
        "accent": "#6366f1",
    },
    "dark": {
        "text": "#e5e7eb", "muted": "#9ca3af", "grid": "#232838", "axis": "#3a4152", "surface": "#171a23",
        "income": "#3987e5", "spending": "#d95926", "savings": "#199e70", "neutral": "#6b7280",
        "accent": "#818cf8",
    },
}
FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
CONFIG = {"displayModeBar": False, "responsive": True}


def colors(theme: str | None) -> dict:
    return THEMES.get(theme or "light", THEMES["light"])


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def base_layout(theme: str | None, height: int = 340, **kw) -> dict:
    c = colors(theme)
    axis = {"gridcolor": c["grid"], "linecolor": c["axis"], "zerolinecolor": c["axis"], "tickcolor": c["axis"],
            "tickfont": {"color": c["muted"], "size": 12}, "title": {"font": {"color": c["muted"]}}}
    layout = {
        "template": "none",  # no default bar outlines/colorway; everything is set explicitly below
        "height": height,
        "margin": {"l": 8, "r": 8, "t": 8, "b": 8},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": FONT, "color": c["text"], "size": 13},
        "xaxis": {**axis, "showgrid": False, "automargin": True},
        "yaxis": {**axis, "showgrid": True, "automargin": True, "zeroline": True},
        "hoverlabel": {"bgcolor": c["surface"], "bordercolor": c["axis"], "font": {"color": c["text"], "family": FONT}},
        "legend": {"orientation": "h", "x": 0, "y": 1.02, "yanchor": "bottom", "font": {"color": c["text"]},
                   "bgcolor": "rgba(0,0,0,0)"},
        "showlegend": True,
    }
    layout.update(kw)
    return layout


def empty_figure(theme: str | None, text: str = "No data for this selection", height: int = 300) -> go.Figure:
    c = colors(theme)
    fig = go.Figure()
    fig.update_layout(**base_layout(theme, height=height, showlegend=False))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.add_annotation(text=text, showarrow=False, font={"color": c["muted"], "size": 14},
                       xref="paper", yref="paper", x=0.5, y=0.5)
    return fig
