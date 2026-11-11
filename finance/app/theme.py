"""Single source of truth for every colour in the app.

To add or restyle a theme, edit ``THEMES`` below and nothing else: the CSS custom
properties used across the UI (``assets/style.css`` only *references* them as
``var(--app-*)``) and the Plotly chart palette (``charts.py``) are both generated from
these values. ``main.py`` injects :func:`css` into the page ``<head>``.

Each theme has two groups of tokens:
  ``ui``    - semantic UI colours, emitted as ``--app-<name>`` CSS variables.
  ``chart`` - colours for Plotly figures, consumed by ``charts.colors(theme)``.

Light is the default (emitted on ``:root``); every theme is also emitted under
``[data-bs-theme="<name>"]`` so the dark-mode switch (and any future theme) can select it.
Chart colours follow the data's job (validated categorical slots; both modes selected, not
flipped): income = blue, spending = orange, savings = aqua. Keep that mapping when editing.
"""
from __future__ import annotations

DEFAULT_THEME = "light"

# --- edit colours here; add a new key to add a whole theme -----------------------------
THEMES: dict[str, dict] = {
    "light": {
        "ui": {
            "bg": "#f5f6fa", "surface": "#ffffff", "surface-2": "#f9fafb", "border": "#e5e7eb",
            "text": "#111827", "muted": "#6b7280",
            "accent": "#6366f1", "accent-hover": "#4f46e5", "accent-text": "#4338ca",
            "on-accent": "#ffffff",
            "positive": "#059669", "negative": "#dc2626",
            "warning-soft": "rgba(245, 158, 11, 0.14)", "demo-text": "#b45309",
            "shadow": "0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06)",
        },
        "chart": {
            # Neutrals (text/grid/axis/surface) stay light so they read on a white canvas;
            # the data hues below are shared with the dark theme by request.
            "text": "#111827", "muted": "#6b7280", "grid": "#eef0f3", "axis": "#d1d5db", "surface": "#ffffff",
            "income": "#3fb4ff", "spending": "#ff9e45", "savings": "#1fe6d4", "neutral": "#5c6370",
            "accent": "#c84bff",
            "nw_cash": "#3fb4ff", "nw_investments": "#1fe6d4", "nw_other": "#ffd166", "nw_credit": "#ff9e45",
            "nw_loans": "#c84bff",
            "series": ["#3fb4ff", "#ff9e45", "#1fe6d4", "#ffd166", "#ff5370", "#4dff8f", "#c84bff", "#ff6bb0"],
        },
    },
    # One Dark (Atom) inspired, dialled up to an electric/neon palette on a deep base.
    "dark": {
        "ui": {
            "bg": "#1b1f27", "surface": "#21262f", "surface-2": "#272d38", "border": "#394153",
            "text": "#c3cad6", "muted": "#8b93a7",
            "accent": "#c84bff", "accent-hover": "#d980ff", "accent-text": "#dd90ff",
            "on-accent": "#15181f",
            "positive": "#2ff08a", "negative": "#ff5370",
            "warning-soft": "rgba(255, 209, 102, 0.18)", "demo-text": "#ffd166",
            "shadow": "0 1px 2px rgba(0, 0, 0, 0.45)",
        },
        "chart": {
            "text": "#c3cad6", "muted": "#8b93a7", "grid": "#2b313d", "axis": "#394153", "surface": "#21262f",
            "income": "#3fb4ff", "spending": "#ff9e45", "savings": "#1fe6d4", "neutral": "#5c6370",
            "accent": "#c84bff",
            "nw_cash": "#3fb4ff", "nw_investments": "#1fe6d4", "nw_other": "#ffd166", "nw_credit": "#ff9e45",
            "nw_loans": "#c84bff",
            "series": ["#3fb4ff", "#ff9e45", "#1fe6d4", "#ffd166", "#ff5370", "#4dff8f", "#c84bff", "#ff6bb0"],
        },
    },
}
# ---------------------------------------------------------------------------------------


def chart_themes() -> dict[str, dict]:
    """Chart palette per theme, for ``charts.THEMES`` / ``charts.colors``."""
    return {name: t["chart"] for name, t in THEMES.items()}


def _rgb(hex_color: str) -> str:
    """'#rrggbb' -> 'r, g, b' (for rgba() in CSS)."""
    h = hex_color.lstrip("#")
    return ", ".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def _block(selector: str, ui: dict) -> str:
    lines = [f"  --app-{name}: {value};" for name, value in ui.items()]
    # Derived helpers so a theme only needs to declare its accent once.
    lines.append(f"  --app-accent-rgb: {_rgb(ui['accent'])};")
    lines.append("  --app-accent-soft: rgba(var(--app-accent-rgb), 0.14);")
    return f"{selector} {{\n" + "\n".join(lines) + "\n}"


def css() -> str:
    """Generate the CSS custom-property blocks for every theme.

    Light lands on ``:root`` (the default) and on ``[data-bs-theme="light"]``; each theme
    also lands on ``[data-bs-theme="<name>"]`` so the theme switch can select it.
    """
    blocks = []
    for name, theme in THEMES.items():
        selector = f':root, [data-bs-theme="{name}"]' if name == DEFAULT_THEME else f'[data-bs-theme="{name}"]'
        blocks.append(_block(selector, theme["ui"]))
    return "/* Generated from finance/app/theme.py - do not hand-edit. */\n" + "\n".join(blocks) + "\n"
