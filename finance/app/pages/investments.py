"""Investments page: portfolio value over time, allocation (click to filter), performance vs a benchmark,
holdings with gain/loss, holding history."""
from __future__ import annotations

import datetime as dt

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from finance import benchmarks as bm
from finance import investments as inv
from finance import networth as nw
from finance import services
from finance import transactions as tx
from finance.app.charts import CONFIG, base_layout, colors, empty_figure, glow, rgba
from finance.app.components import grid, money_col, panel
from finance.config import get_settings

PERIODS = ["30d", "90d", "ytd", "12m", "last_year", "24m", "all", "custom"]
PERSIST = {"persistence": True, "persistence_type": "local"}
PCT_SIGNED = {"function": "params.value == null ? '' : d3.format('+.1%')(params.value)"}
PCT = {"function": "params.value == null ? '' : d3.format('.1%')(params.value)"}
SIGN_CLASS = {"amount-neg": "params.value < 0", "amount-pos": "params.value > 0"}
DOLLARS = {"function": "params.value == null ? '' : d3.format('$,.0f')(params.value)"}
WRAP = {"wrapHeaderText": True, "autoHeaderHeight": True}
NUM = {"function": "params.value == null ? '' : d3.format(',.4~f')(params.value)"}
ALLOC_HEIGHT = 310
HIDDEN = {"display": "none"}


def _money(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    if v < 0:
        return f"-${-v:,.0f}"
    return f"{'+' if signed and v > 0 else ''}${v:,.0f}"


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1%}"


def _stat(label: str, value: str, hint: str | None = None, cls: str = "") -> html.Div:
    return html.Div([html.Div(label, className="stat-label"), html.Div(value, className=f"stat-value {cls}"),
                     html.Div(hint, className="stat-hint") if hint else None], className="stat-chip")


def _sign_cls(v) -> str:
    return "" if v is None else "amount-neg" if v < 0 else "amount-pos"


def _graph(id_: str, height: int) -> dcc.Graph:
    return dcc.Graph(id=id_, config=CONFIG, style={"height": f"{height}px"})


def _pct_col(field: str, header: str, signed: bool = True, **kw) -> dict:
    return {"field": field, "headerName": header, "valueFormatter": PCT_SIGNED if signed else PCT,
            "cellClassRules": SIGN_CLASS if signed else {}, "type": "rightAligned",
            "filter": "agNumberColumnFilter", **kw}


def layout() -> html.Div:
    accounts = services.read(inv.investment_accounts)
    filters = dbc.Row([
        dbc.Col(dbc.Select(id="inv-period", value="12m", size="sm", **PERSIST,
                           options=[{"label": tx.PERIODS[k], "value": k} for k in PERIODS]), lg=2, md=4),
        dbc.Col(dcc.DatePickerRange(id="inv-dates", display_format="YYYY-MM-DD", clearable=True, **PERSIST),
                id="inv-dates-col", width="auto"),
        dbc.Col(dcc.Dropdown(id="inv-accounts", multi=True, placeholder="All investment accounts", **PERSIST,
                             options=[{"label": a["label"] + (f" ({a['institution']})" if a["institution"] else ""),
                                       "value": a["id"]} for a in accounts]), lg=4, md=8),
        dbc.Col(html.Div([html.I(className="bi bi-funnel me-1"), html.Span(id="inv-focus-label"),
                          dbc.Button([html.I(className="bi bi-x-lg me-1"), "Clear"], id="inv-focus-clear",
                                     color="link", size="sm", className="py-0 ms-1")],
                         id="inv-focus-chip", className="focus-chip", style=HIDDEN), width="auto"),
    ], className="g-2 mb-3 align-items-center")
    holding_cols = [
        {"field": "symbol", "headerName": "Holding", "minWidth": 100, "tooltipField": "description",
         "headerTooltip": "Hover a symbol for the full name"},
        {"field": "account", "headerName": "Account", "minWidth": 120, "tooltipField": "account"},
        {"field": "shares", "headerName": "Shares", "type": "rightAligned", "valueFormatter": NUM,
         "filter": "agNumberColumnFilter", "minWidth": 105},
        money_col("price", "Price", minWidth=90, cellClassRules={}),
        money_col("market_value", "Value", minWidth=105, cellClassRules={}, valueFormatter=DOLLARS),
        money_col("cost_basis", "Cost basis", minWidth=110, cellClassRules={}, valueFormatter=DOLLARS, **WRAP,
                  headerTooltip="What you paid for the shares you still hold"),
        money_col("gain", "Gain", minWidth=95, cellClassRules=SIGN_CLASS, valueFormatter=DOLLARS,
                  headerTooltip="Value minus cost basis (not sold yet, so unrealized)"),
        _pct_col("gain_pct", "Gain %", minWidth=90),
        _pct_col("weight", "Weight", signed=False, minWidth=90),
        _pct_col("price_change", "Price change", minWidth=125, **WRAP,
                 headerTooltip="Change in price per share over the period (ignores buys and sells)"),
    ]
    benchmark = dcc.Dropdown(id="inv-benchmark", value=bm.DEFAULT, clearable=False, searchable=False,
                             options=[{"label": v, "value": k} for k, v in bm.BENCHMARKS.items()],
                             style={"minWidth": "210px"}, **PERSIST)
    return html.Div([
        dcc.Store(id="inv-focus"),
        filters,
        html.Div(id="inv-kpis", className="stat-strip mb-3"),
        panel(html.Span("Portfolio value over time", id="inv-history-title"), _graph("inv-history", 360),
              subtitle=html.Span(id="inv-history-subtitle")),
        dbc.Row([
            dbc.Col(panel("Allocation",
                          dbc.RadioItems(id="inv-alloc-by", inline=True, value="symbol", className="mb-1", **PERSIST,
                                         options=[{"label": "By holding", "value": "symbol"},
                                                  {"label": "By account", "value": "account"}]),
                          _graph("inv-allocation", ALLOC_HEIGHT),
                          subtitle="Share of market value at the end of the period. Click a bar to filter."), lg=5),
            dbc.Col(panel("Performance", html.Div(id="inv-perf-stats", className="stat-strip compact mb-2"),
                          _graph("inv-performance", 300), actions=benchmark,
                          subtitle=("Cumulative price return from the start of the period. Deposits and buys "
                                    "are not counted as gains; dividends are not included.")), lg=7),
        ], className="g-3"),
        panel("Holdings",
              grid("inv-holdings", holding_cols, [], height=380,
                   dashGridOptions={"rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                     "enableClickSelection": True}}),
              subtitle=("Latest snapshot on or before the end of the period. Click a holding to see its history "
                        "and performance.")),
        panel(html.Span("Holding history", id="inv-holding-title"), _graph("inv-holding", 320),
              subtitle=html.Span(id="inv-holding-subtitle")),
    ])


def _range(period, start, end):
    if period == "custom":
        return (dt.date.fromisoformat(start) if start else None, dt.date.fromisoformat(end) if end else None)
    return tx.period_range(period or "12m")


def _pick(selected, rows) -> dict | None:
    """The clicked holding if it is still listed, else the largest one."""
    ids = {r["id"] for r in rows or []}
    if selected and selected[0]["id"] in ids:
        return selected[0]
    return rows[0] if rows else None


# --------------------------------------------------------------------------- figures
def history_figure(values: pd.DataFrame, cost: pd.Series | None, accounts: list[dict], theme,
                   empty_text: str = "No investment history yet: it starts with the first sync") -> go.Figure:
    c = colors(theme)
    if values.empty or "total" not in values or not values["total"].any():
        return empty_figure(theme, empty_text, height=360)
    slot = {a["label"]: a["slot"] for a in accounts}
    traces = []
    for name in [col for col in values.columns if col != "total"]:
        color = c["series"][slot.get(name, inv.MAX_ACCOUNT_SERIES)]
        traces.append(go.Scatter(x=values.index, y=values[name], name=name, mode="lines", stackgroup="value",
                                 line={"width": 1, "color": c["surface"]}, fillcolor=rgba(color, 0.85),
                                 hovertemplate="%{y:$,.0f}<extra>" + name + "</extra>"))
    if cost is not None:
        traces.append(go.Scatter(x=cost.index, y=cost.values, name="Cost basis", mode="lines",
                                 line={"color": c["text"], "width": 2, "dash": "dash"},
                                 hovertemplate="%{y:$,.0f}<extra>Cost basis</extra>"))
    fig = go.Figure(traces)
    layout = base_layout(theme, height=360, hovermode="x unified")
    layout["legend"]["traceorder"] = "normal"
    fig.update_layout(**layout)
    fig.update_xaxes(hoverformat="%a %b %d, %Y")
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    glow(fig, theme)  # cost-basis line plus the cumulative edges of the stacked value bands
    return fig


def allocation_figure(alloc: pd.DataFrame, theme, focus: str | None = None) -> go.Figure:
    """Fixed height (the graph container does not resize), so few bars just get more room around them."""
    c = colors(theme)
    if alloc.empty:
        return empty_figure(theme, "No holdings in this selection", height=ALLOC_HEIGHT)
    fills = [c["accent"] if focus is None or k == focus else rgba(c["accent"], 0.3) for k in alloc["key"]]
    fig = go.Figure(go.Bar(
        x=alloc["value"], y=alloc["label"], orientation="h", marker={"color": fills, "cornerradius": 4},
        customdata=list(zip(alloc["share"], alloc["key"].fillna(""))),
        hovertemplate="<b>%{y}</b><br>%{x:$,.0f} (%{customdata[0]:.1%})<br>Click to filter<extra></extra>"))
    for label, v, share in zip(alloc["label"], alloc["value"], alloc["share"]):
        fig.add_annotation(x=v, y=label, text=f"{share:.0%}", showarrow=False, xanchor="left", xshift=6,
                           font={"color": c["muted"], "size": 12})
    n = len(alloc)
    fig.update_layout(**base_layout(theme, height=ALLOC_HEIGHT, showlegend=False,
                                    bargap=0.3 if n >= 5 else 0.55 if n >= 3 else 0.7,
                                    margin={"l": 8, "r": 48, "t": 8, "b": 8}))
    fig.update_yaxes(autorange="reversed", showgrid=False, zeroline=False, tickfont={"color": c["text"]})
    fig.update_xaxes(showgrid=True, tickprefix="$", tickformat="~s", zeroline=True)
    glow(fig, theme)
    return fig


def performance_figure(series: list[tuple[str, pd.Series, str, str]], theme) -> go.Figure:
    """series: (name, cumulative return, color, dash)."""
    if not series:
        return empty_figure(theme, "No priced holdings in this selection", height=300)
    fig = go.Figure([go.Scatter(x=s.index, y=s.values, name=name, mode="lines",
                                line={"color": color, "width": 2, "dash": dash},
                                hovertemplate="%{y:+.1%}<extra>" + name + "</extra>")
                     for name, s, color, dash in series])
    layout = base_layout(theme, height=300, hovermode="x unified")
    layout["legend"]["traceorder"] = "normal"
    fig.update_layout(**layout)
    fig.update_xaxes(hoverformat="%a %b %d, %Y")
    fig.update_yaxes(tickformat="+.0%")
    glow(fig, theme)
    return fig


def holding_figure(hist: pd.DataFrame | None, color: str, theme) -> go.Figure:
    c = colors(theme)
    if hist is None or hist.empty:
        return empty_figure(theme, "Click a holding in the table above", height=320)
    traces = [go.Scatter(x=hist["date"], y=hist["market_value"], name="Value", mode="lines",
                         line={"color": color, "width": 2}, customdata=hist["price"],
                         hovertemplate="%{y:$,.0f} (price %{customdata:$,.2f})<extra>Value</extra>")]
    if hist["cost_basis"].notna().any():
        traces.append(go.Scatter(x=hist["date"], y=hist["cost_basis"], name="Cost basis", mode="lines",
                                 line={"color": c["text"], "width": 2, "dash": "dash"},
                                 hovertemplate="%{y:$,.0f}<extra>Cost basis</extra>"))
    fig = go.Figure(traces)
    fig.update_layout(**base_layout(theme, height=320, hovermode="x unified"))
    fig.update_xaxes(hoverformat="%a %b %d, %Y")
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    glow(fig, theme, underfill=True)
    return fig


# --------------------------------------------------------------------------- callbacks
@callback(Output("inv-kpis", "children"), Output("inv-history", "figure"), Output("inv-history-title", "children"),
          Output("inv-history-subtitle", "children"), Output("inv-allocation", "figure"),
          Output("inv-holdings", "rowData"), Output("inv-dates-col", "style"), Output("inv-focus-chip", "style"),
          Output("inv-focus-label", "children"),
          Input("inv-period", "value"), Input("inv-dates", "start_date"), Input("inv-dates", "end_date"),
          Input("inv-accounts", "value"), Input("inv-alloc-by", "value"), Input("inv-focus", "data"),
          Input("theme", "data"), Input("sync-version", "data"))
def update(period, start, end, accounts, alloc_by, focus, theme, _version):
    accts, daily, holdings = services.read(inv.load, accounts or None)
    lo, hi = _range(period, start, end)
    clipped = nw.clip(daily, lo, hi)
    all_rows = inv.holding_rows(holdings, accts, lo, hi)
    by = alloc_by or "symbol"
    alloc_fig = allocation_figure(inv.allocation(all_rows, by), theme, focus if by == "symbol" else None)
    if focus:
        h = inv.focus_holdings(holdings, focus)
        values = inv.holding_values(h, clipped.index, accts)
        rows = inv.holding_rows(h, accts, lo, hi)
        title = f"{focus} value over time"
        subtitle = f"Market value of {focus}, stacked by account; the dashed line is its cost basis."
    else:
        h, rows = holdings, all_rows
        values = inv.value_by_account(clipped, accts)
        title = "Portfolio value over time"
        subtitle = ("Account balances (including uninvested cash), stacked by account; the dashed line is the "
                    "holdings' total cost basis." + (f" History starts at the first sync "
                                                      f"({daily.index.min():%b %d, %Y})." if not daily.empty else ""))
    if daily.empty:
        subtitle = "History starts with the first sync: one balance and holdings snapshot per account per day."
    cost = inv.cost_daily(h, clipped.index) if not clipped.empty else None
    k = inv.kpis(values, rows)
    pct = f"{k['change_pct']:+.1%} " if k["change_pct"] is not None else ""
    kpis = [
        _stat(f"{focus} value" if focus else "Portfolio value", _money(k["value"]),
              f"as of {k['end']:%b %d, %Y}" if k["end"] else None),
        _stat("Cost basis", _money(k["cost_basis"]), "what you paid for what you hold"),
        _stat("Unrealized gain", _money(k["gain"], signed=True),
              f"{k['gain_pct']:+.1%} on cost" if k["gain_pct"] is not None else None, cls=_sign_cls(k["gain"])),
        _stat("Change", _money(k["change"], signed=True),
              (pct + f"since {k['start']:%b %d, %Y}, incl. deposits") if k["start"] else None,
              cls=_sign_cls(k["change"])),
    ]
    empty_text = f"No {focus} holdings in this selection" if focus else None
    hist_fig = history_figure(values, cost, accts, theme, *([empty_text] if empty_text else []))
    return (kpis, hist_fig, title, subtitle, alloc_fig, rows,
            {} if period == "custom" else HIDDEN, {} if focus else HIDDEN, f"Filtered to {focus}" if focus else "")


@callback(Output("inv-focus", "data"), Output("inv-accounts", "value"),
          Input("inv-allocation", "clickData"), Input("inv-focus-clear", "n_clicks"),
          State("inv-alloc-by", "value"), State("inv-focus", "data"), prevent_initial_call=True)
def click_allocation(click, _clear, alloc_by, focus):
    if ctx.triggered_id == "inv-focus-clear":
        return None, no_update
    try:
        key = click["points"][0]["customdata"][1]
    except (TypeError, KeyError, IndexError):
        return no_update, no_update
    if not key:  # the folded "Other (n)" bar
        return no_update, no_update
    if alloc_by == "account":
        return None, [key]
    return (None if key == focus else key), no_update


@callback(Output("inv-performance", "figure"), Output("inv-perf-stats", "children"),
          Input("inv-holdings", "selectedRows"), Input("inv-holdings", "rowData"), Input("inv-focus", "data"),
          Input("inv-benchmark", "value"), Input("inv-period", "value"), Input("inv-dates", "start_date"),
          Input("inv-dates", "end_date"), Input("inv-accounts", "value"), Input("theme", "data"),
          Input("sync-version", "data"))
def update_performance(selected, rows, focus, benchmark, period, start, end, accounts, theme, _version):
    c = colors(theme)
    accts, daily, holdings = services.read(inv.load, accounts or None)
    idx = nw.clip(daily, *_range(period, start, end)).index
    if not len(idx):
        return performance_figure([], theme), None
    series, stats = [], []
    port = inv.return_index(holdings, idx)
    if port is not None:
        series.append(("Portfolio", port, c["text"], "solid"))
    row = _pick(selected, rows)
    if focus:
        name, h = focus, inv.focus_holdings(holdings, focus)
    elif row is not None:
        name = inv.symbol_of(row)
        h = holdings[(holdings["account_id"] == row["account_id"]) & (holdings["holding_id"] == row["holding_id"])]
    else:
        name, h = None, holdings.iloc[0:0]
    holding = inv.return_index(h, idx) if name else None
    if holding is not None:  # colored like its account (the first one, for a symbol held in several)
        slots = [a["slot"] for a in services.read(inv.investment_accounts, list(h["account_id"].unique()))]
        series.append((name, holding, c["series"][min(slots, default=0)], "solid"))
    symbol = benchmark or bm.DEFAULT
    label = bm.BENCHMARKS.get(symbol, symbol)
    prices, error = services.write(bm.load, symbol, idx[0].date(), idx[-1].date(), get_settings().demo)
    bench = bm.return_index(prices, idx)
    if bench is not None:
        series.append((label, bench, c["muted"], "dash"))

    def last(s):
        return float(s.iloc[-1]) if s is not None else None

    if port is not None:
        stats.append(_stat("Portfolio", _pct(last(port)), cls=_sign_cls(last(port))))
    if holding is not None:
        stats.append(_stat(name, _pct(last(holding)), cls=_sign_cls(last(holding))))
    if bench is not None:
        stats.append(_stat(label, _pct(last(bench)), cls=_sign_cls(last(bench))))
        if port is not None:
            diff = last(port) - last(bench)
            stats.append(_stat("Difference", f"{diff * 100:+.1f} pts", cls=_sign_cls(diff)))
    elif error:
        stats.append(html.Div([html.I(className="bi bi-exclamation-triangle me-1"),
                               f"{label} unavailable: {error}"], className="text-muted small"))
    return performance_figure(series, theme), stats


@callback(Output("inv-holding", "figure"), Output("inv-holding-title", "children"),
          Output("inv-holding-subtitle", "children"),
          Input("inv-holdings", "selectedRows"), Input("inv-holdings", "rowData"), Input("inv-period", "value"),
          Input("inv-dates", "start_date"), Input("inv-dates", "end_date"), Input("theme", "data"),
          Input("sync-version", "data"))
def update_holding(selected, rows, period, start, end, theme, _version):
    row = _pick(selected, rows)
    if row is None:
        return holding_figure(None, "", theme), "Holding history", None
    _, _, holdings = services.read(inv.load, [row["account_id"]])
    lo, hi = _range(period, start, end)
    hist = inv.holding_history(holdings, row["account_id"], row["holding_id"])
    if lo:
        hist = hist[hist["date"] >= pd.Timestamp(lo)]
    if hi:
        hist = hist[hist["date"] <= pd.Timestamp(hi)]
    all_accts = {a["id"]: a for a in services.read(inv.investment_accounts, [row["account_id"]])}
    slot = all_accts.get(row["account_id"], {}).get("slot", 0)
    title = f"{inv.symbol_of(row)} in {row['account']}"
    picked = selected and selected[0]["id"] == row["id"]
    subtitle = ("Market value and cost basis on each snapshot day."
                + ("" if picked else " Showing the largest holding; click another in the table."))
    return holding_figure(hist, colors(theme)["series"][slot], theme), title, subtitle
