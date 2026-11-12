"""Plotly figure styling shared by the pages: one look for light and dark themes.

Colors follow the data's job (validated categorical slots; both modes selected, not flipped):
income = slot 1 (blue), spending = slot 2 (orange), savings = slot 3 (aqua).
Net worth groups (`nw_*`, stacking order yellow/aqua/blue | orange/violet validated adjacent-pairs in both
modes): cash blue, investments aqua, other assets yellow, credit cards orange, loans violet.
Generic entities (e.g. investment accounts) use `series`: the 8 categorical slots in fixed order, validated
adjacent-pairs in both modes; an entity keeps its slot when filters change.
Text, grid and axes use the same neutrals as the CSS `--app-*` variables.
"""
from __future__ import annotations

import plotly.graph_objects as go

from finance.app.theme import chart_themes

# Per-theme chart palette; the single source of truth lives in finance/app/theme.py.
THEMES = chart_themes()
FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
CONFIG = {"displayModeBar": False, "responsive": True}

# Toggle the mplcyberpunk-style neon bloom on lines/areas/bars (see `glow` below).
# Set to True to turn the effect back on; when False, `glow()` leaves figures untouched
# so lines keep their plain, thinner stroke. No UI control - this is the single switch.
GLOW_ENABLED = False


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


def _fade(strength: float, i: int, layers: int) -> float:
    """Opacity for glow pass ``i`` (1..layers): the widest pass is the faintest."""
    return strength * (1 - (i - 1) / max(layers, 1))


def _bar_color(marker) -> str | list | None:
    """A usable border colour for a bar's glow: its single colour, or the per-bar list."""
    mc = marker.color
    if isinstance(mc, str):
        return mc
    if isinstance(mc, (list, tuple)) and mc and all(isinstance(v, str) for v in mc):
        return list(mc)
    return None  # numeric/colorscale bars have no single neon to echo; skip them


def glow(fig: go.Figure, theme: str | None = None, *, layers: int = 4, spread: float = 4.0,
         strength: float = 0.18, underfill: bool = False) -> go.Figure:
    """Add an mplcyberpunk-style neon bloom to a figure's traces, in place.

    Plotly has no blur/shadow primitive, so for each trace we draw a few progressively
    wider, fainter copies in the same colour - the widest is the faintest - which fakes the
    glow. It covers all three mark types:

    * **lines** - the bloom sits *under* the line; ``underfill=True`` also drops a
      translucent vertical gradient from the line to y=0.
    * **stacked areas** (``lines`` traces with a ``stackgroup``) - the bloom traces the
      band's cumulative top edge and is drawn *over* the fills so it stays visible.
    * **bars** - widening transparent-fill copies with a coloured border bloom *under* the
      bars (per-bar colours supported); the original keeps its slot in grouped charts.

    Disabled by default via ``GLOW_ENABLED`` (set at the top of this module); when off the
    figure is returned unchanged, so the whole effect is kept but inert.
    """
    if not GLOW_ENABLED:
        return fig
    under: list = []   # drawn beneath the originals (line/bar bloom, underfill)
    over: list = []    # drawn above the originals (area edges, so fills don't hide them)
    stack_cum: dict[str, list[float]] = {}
    for idx, tr in enumerate(fig.data):
        if tr.type == "scatter" and "lines" in (tr.mode or ""):
            color = tr.line.color
            common = {"x": tr.x, "mode": "lines", "hoverinfo": "skip", "showlegend": False,
                      "legendgroup": tr.legendgroup, "xaxis": tr.xaxis, "yaxis": tr.yaxis}
            if tr.stackgroup:  # cumulative top edge of this band within its stack
                y = [0.0 if v is None or v != v else float(v) for v in (tr.y if tr.y is not None else [])]
                prev = stack_cum.get(tr.stackgroup)
                cum = [(prev[i] if prev else 0.0) + y[i] for i in range(len(y))]
                stack_cum[tr.stackgroup] = cum
                if not color:
                    continue
                a_spread = min(spread, 3.0)  # keep the band edge glow from bleeding into neighbours
                for i in range(layers, 0, -1):
                    over.append(go.Scatter(**common, y=cum, opacity=_fade(strength, i, layers),
                                           line={"color": color, "width": 1 + i * a_spread}))
                over.append(go.Scatter(**common, y=cum, line={"color": color, "width": 1.5}))  # crisp edge
            elif color:
                base_w = tr.line.width or 2
                if underfill:
                    under.append(go.Scatter(**common, y=tr.y, line={"width": 0, "color": color}, fill="tozeroy",
                                            fillgradient={"type": "vertical",
                                                          "colorscale": [[0.0, rgba(color, 0.0)],
                                                                         [1.0, rgba(color, 0.22)]]}))
                for i in range(layers, 0, -1):
                    under.append(go.Scatter(**common, y=tr.y, opacity=_fade(strength, i, layers),
                                            line={"color": color, "width": base_w + i * spread}))
        elif tr.type == "bar":
            color = _bar_color(tr.marker)
            if color is None:
                continue
            if not tr.offsetgroup:  # pin the slot so adding overlay bars can't shift a grouped chart
                tr.offsetgroup = f"glow{idx}"
            common = {"x": tr.x, "y": tr.y, "base": tr.base, "width": tr.width,
                      "orientation": tr.orientation or "v", "offsetgroup": tr.offsetgroup,
                      "alignmentgroup": tr.alignmentgroup, "hoverinfo": "skip", "showlegend": False,
                      "xaxis": tr.xaxis, "yaxis": tr.yaxis}
            for i in range(layers, 0, -1):
                under.append(go.Bar(**common, opacity=_fade(strength, i, layers),
                                    marker={"color": "rgba(0,0,0,0)", "cornerradius": tr.marker.cornerradius,
                                            "line": {"color": color, "width": i * spread * 0.6}}))
    if under or over:
        n = len(fig.data)
        fig.add_traces(under + over)
        added = list(fig.data[n:])
        fig.data = tuple(added[:len(under)] + list(fig.data[:n]) + added[len(under):])
    return fig


glow_lines = glow  # backwards-compatible alias: glow now covers lines, areas and bars


def empty_figure(theme: str | None, text: str = "No data for this selection", height: int = 300) -> go.Figure:
    c = colors(theme)
    fig = go.Figure()
    fig.update_layout(**base_layout(theme, height=height, showlegend=False))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.add_annotation(text=text, showarrow=False, font={"color": c["muted"], "size": 14},
                       xref="paper", yref="paper", x=0.5, y=0.5)
    return fig
