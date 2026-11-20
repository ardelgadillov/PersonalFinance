"""Net worth page: KPIs, assets and debts over time by group, composition, accounts.

Selecting an account in the accounts table swaps the "Net worth over time" chart to that account's
balance history; "Show all accounts" (or clearing the selection) returns to the stacked total.
"""
from __future__ import annotations

import datetime as dt

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, callback, ctx, dcc, get_relative_path, html
from dash.exceptions import PreventUpdate

from finance import networth as nw
from finance import services
from finance import transactions as tx
from finance.app.charts import CONFIG, base_layout, colors, empty_figure, glow, rgba
from finance.app.components import grid, money_col, panel

PERIODS = ["30d", "90d", "ytd", "12m", "last_year", "24m", "all", "custom"]
PERSIST = {"persistence": True, "persistence_type": "local"}
GROUP_KEYS = [g[0] for g in nw.GROUPS]
GROUP_LABELS = {g[0]: g[1] for g in nw.GROUPS}


def _money(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    if v < 0:
        return f"-${-v:,.0f}"
    return f"{'+' if signed and v > 0 else ''}${v:,.0f}"


def _stat(label: str, value: str, hint: str | None = None, cls: str = "") -> html.Div:
    return html.Div([html.Div(label, className="stat-label"), html.Div(value, className=f"stat-value {cls}"),
                     html.Div(hint, className="stat-hint") if hint else None], className="stat-chip")


def _graph(id_: str, height: int) -> dcc.Graph:
    return dcc.Graph(id=id_, config=CONFIG, style={"height": f"{height}px"})


def layout() -> html.Div:
    accounts = services.account_choices()
    untyped = services.untyped_account_count()
    filters = dbc.Row([
        dbc.Col(dbc.Select(id="nw-period", value="12m", size="sm", **PERSIST,
                           options=[{"label": tx.PERIODS[k], "value": k} for k in PERIODS]), lg=2, md=4),
        dbc.Col(dcc.DatePickerRange(id="nw-dates", display_format="YYYY-MM-DD", clearable=True, **PERSIST),
                id="nw-dates-col", width="auto"),
        dbc.Col(dcc.Dropdown(id="nw-accounts", multi=True, placeholder="All accounts included in net worth",
                             **PERSIST, options=[{"label": a["label"], "value": a["id"]} for a in accounts]),
                lg=4, md=8),
    ], className="g-2 mb-3 align-items-center")
    account_cols = [
        {"field": "account", "headerName": "Account", "minWidth": 140, "flex": 2, "tooltipField": "account"},
        {"field": "institution", "headerName": "Institution", "minWidth": 110, "flex": 1,
         "tooltipField": "institution"},
        money_col("balance", "Balance", minWidth=105),
        money_col("change", "Change", minWidth=95,
                  headerTooltip="Change over the period; last balance snapshot date is the tooltip on the account"),
    ]
    return html.Div([
        dbc.Alert(["Some accounts have no type yet, so they are grouped by the sign of their balance. ",
                   dcc.Link("Set types in Settings", href=get_relative_path("/settings"))],
                  color="warning", className="py-2") if untyped else None,
        filters,
        html.Div(id="nw-kpis", className="stat-strip mb-3"),
        dbc.Row([
            dbc.Col(panel("Net worth over time", _graph("nw-history", 420),
                          subtitle=html.Span(id="nw-history-subtitle"),
                          actions=dbc.Button([html.I(className="bi bi-arrow-left me-1"), "Show all accounts"],
                                             id="nw-history-clear", color="secondary", outline=True, size="sm",
                                             style={"display": "none"})), lg=6),
            dbc.Col(_accounts_section(account_cols), lg=6),
        ], className="g-3"),
        panel("Composition", _graph("nw-composition", 300),
              subtitle="Balance by group at the end of the period."),
        dcc.Store(id="nw-account-sel"),
    ])


def _accounts_section(cols: list[dict]) -> html.Div:
    """One account list per group, shown below a shared heading; empty groups hide at runtime."""
    panels = []
    for key in GROUP_KEYS:
        panels.append(html.Div([
            html.Div(html.Span(GROUP_LABELS[key], id=f"nw-acctitle-{key}"), className="panel-title mb-1"),
            grid(f"nw-grid-{key}", cols, [], height=None,
                 dashGridOptions={"domLayout": "autoHeight",
                                  "rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                   "enableClickSelection": True}}),
        ], id=f"nw-accpanel-{key}", className="mb-3", style={"display": "none"}))
    return html.Div([
        html.Div("Accounts", className="panel-title mb-2"),
        *panels,
        html.Div("Click an account to see its balance history. Choose which accounts count in Settings "
                 "(Include in net worth, Hidden).", className="panel-subtitle"),
    ])


def _range(period, start, end):
    if period == "custom":
        return (dt.date.fromisoformat(start) if start else None, dt.date.fromisoformat(end) if end else None)
    return tx.period_range(period or "12m")


def _load(period, start, end, accounts):
    daily, accts = services.read(nw.load, accounts or None)
    lo, hi = _range(period, start, end)
    return nw.clip(daily, lo, hi), accts, daily


# --------------------------------------------------------------------------- figures
def history_figure(groups: pd.DataFrame, theme) -> go.Figure:
    c = colors(theme)
    if groups.empty:
        return empty_figure(theme, "No balance history yet: it starts with the first sync", height=380)
    traces = []
    for rank, (key, label, liability) in enumerate(nw.GROUPS, start=2):
        if key not in groups:
            continue
        color = c[f"nw_{key}"]
        traces.append(go.Scatter(
            x=groups.index, y=groups[key], name=label, mode="lines",
            stackgroup="debts" if liability else "assets",
            line={"width": 1, "color": c["surface"]}, fillcolor=rgba(color, 0.85), legendrank=rank,
            hovertemplate="%{y:$,.0f}<extra>" + label + "</extra>"))
    traces.append(go.Scatter(
        x=groups.index, y=groups["net_worth"], name="Net worth", mode="lines", legendrank=1,
        line={"color": c["text"], "width": 2}, hovertemplate="%{y:$,.0f}<extra>Net worth</extra>"))
    fig = go.Figure(traces)
    layout = base_layout(theme, height=380, hovermode="x unified")
    layout["legend"]["traceorder"] = "normal"  # stacked traces would otherwise list in reverse
    fig.update_layout(**layout)
    fig.update_xaxes(hoverformat="%a %b %d, %Y")
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    glow(fig, theme)  # bloom on the net-worth line and the cumulative edges of the stacked bands
    return fig


def composition_figure(groups: pd.DataFrame, theme) -> go.Figure:
    c = colors(theme)
    if groups.empty:
        return empty_figure(theme, height=300)
    last = groups.iloc[-1]
    rows = [(label, float(last[key]), c[f"nw_{key}"]) for key, label, _ in nw.GROUPS
            if key in groups and abs(float(last[key])) >= 0.5]
    if not rows:
        return empty_figure(theme, height=300)
    labels, values, fills = zip(*rows)
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h", marker={"color": list(fills), "cornerradius": 4},
        hovertemplate="<b>%{y}</b><br>%{x:$,.0f}<extra></extra>"))
    for label, v in zip(labels, values):  # value labels right of the bar (debts: right of zero)
        fig.add_annotation(x=max(v, 0), y=label, text=_money(v), showarrow=False, xanchor="left", xshift=6,
                           font={"color": c["muted"], "size": 12})
    fig.update_layout(**base_layout(theme, height=300, showlegend=False, bargap=0.3,
                                    margin={"l": 8, "r": 70, "t": 8, "b": 8}))
    fig.update_yaxes(autorange="reversed", showgrid=False, zeroline=False, tickfont={"color": c["text"]})
    fig.update_xaxes(showgrid=True, tickprefix="$", tickformat="~s", zeroline=True)
    glow(fig, theme)
    return fig


def account_figure(series: pd.Series | None, label: str, group: str, theme) -> go.Figure:
    c = colors(theme)
    if series is None or series.empty:
        return empty_figure(theme, "No account in this selection", height=300)
    fig = go.Figure(go.Scatter(x=series.index, y=series.values, name=label, mode="lines",
                               line={"color": c[f"nw_{group}"], "width": 2},
                               hovertemplate="%{x|%a %b %d, %Y}<br>%{y:$,.0f}<extra></extra>"))
    fig.update_layout(**base_layout(theme, height=300, showlegend=False, hovermode="x"))
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    glow(fig, theme, underfill=True)
    return fig


# --------------------------------------------------------------------------- callbacks
@callback(
    [Output("nw-kpis", "children"), Output("nw-composition", "figure"), Output("nw-dates-col", "style")]
    + [Output(f"nw-grid-{g}", "rowData") for g in GROUP_KEYS]
    + [Output(f"nw-acctitle-{g}", "children") for g in GROUP_KEYS]
    + [Output(f"nw-accpanel-{g}", "style") for g in GROUP_KEYS],
    [Input("nw-period", "value"), Input("nw-dates", "start_date"), Input("nw-dates", "end_date"),
     Input("nw-accounts", "value"), Input("theme", "data"), Input("sync-version", "data")])
def update(period, start, end, accounts, theme, _version):
    daily, accts, _full = _load(period, start, end, accounts)
    groups = nw.by_group(daily, accts)
    k = nw.kpis(groups)
    if k["start"]:
        span = f"since {k['start']:%b %d, %Y}"
        pct = f"{k['change_pct']:+.1%} " if k["change_pct"] is not None else ""
        change_hint = pct + span
    else:
        change_hint = None
    kpis = [
        _stat("Net worth", _money(k["net_worth"]), f"as of {k['end']:%b %d, %Y}" if k["end"] else None),
        _stat("Assets", _money(k["assets"])),
        _stat("Debts", _money(k["liabilities"]), "credit cards, loans, mortgage"),
        _stat("Change", _money(k["change"], signed=True), change_hint,
              cls="" if k["change"] is None else "amount-neg" if k["change"] < 0 else "amount-pos"),
    ]
    rows = nw.account_rows(daily, accts)
    by: dict[str, list] = {g: [] for g in GROUP_KEYS}
    for r in rows:
        key = r.get("group_key") if r.get("group_key") in by else "other"
        by[key].append(r)
    row_data = [by[g] for g in GROUP_KEYS]
    titles, styles = [], []
    for g in GROUP_KEYS:
        grp = by.get(g, [])
        total = sum(r["balance"] for r in grp if r.get("balance") is not None)
        titles.append(f"{GROUP_LABELS[g]}  {_money(total)}" if grp else GROUP_LABELS[g])
        styles.append({} if grp else {"display": "none"})
    return ([kpis, composition_figure(groups, theme), {} if period == "custom" else {"display": "none"}]
            + row_data + titles + styles)


@callback(Output("nw-history", "figure"), Output("nw-history-subtitle", "children"),
          Output("nw-history-clear", "style"),
          Input("nw-account-sel", "data"),
          Input("nw-period", "value"), Input("nw-dates", "start_date"), Input("nw-dates", "end_date"),
          Input("nw-accounts", "value"), Input("theme", "data"), Input("sync-version", "data"))
def update_history(sel, period, start, end, accounts, theme, _version):
    """Main chart: the full stacked net worth, or (when an account is selected) that one account."""
    if sel:
        daily, _ = services.read(nw.load, [sel["id"]])
        daily = nw.clip(daily, *_range(period, start, end))
        series = daily[sel["id"]] if sel["id"] in daily else None
        name = f"{sel['account']}" + (f" ({sel['institution']})" if sel.get("institution") else "")
        subtitle = html.Span([html.Strong(name),
                              " balance history. Use “Show all accounts” to return to total net worth."])
        return account_figure(series, sel["account"], sel.get("group_key") or "cash", theme), subtitle, {}
    daily, accts, full = _load(period, start, end, accounts)
    groups = nw.by_group(daily, accts)
    if full.empty:
        subtitle = "History starts with the first sync: one balance snapshot per account per day."
    else:
        subtitle = (f"Debts are shown below zero; the line is net worth. History starts at the first sync "
                    f"({full.index.min():%b %d, %Y}).")
    return history_figure(groups, theme), subtitle, {"display": "none"}


@callback(Output("nw-account-sel", "data"),
          [Input(f"nw-grid-{g}", "selectedRows") for g in GROUP_KEYS],
          prevent_initial_call=True)
def pick_account(*selections):
    trig = ctx.triggered_id
    if not isinstance(trig, str) or not trig.startswith("nw-grid-"):
        raise PreventUpdate
    sel = selections[GROUP_KEYS.index(trig[len("nw-grid-"):])]
    return sel[0] if sel else None


@callback([Output(f"nw-grid-{g}", "selectedRows") for g in GROUP_KEYS],
          Input("nw-history-clear", "n_clicks"), prevent_initial_call=True)
def clear_account(_n):
    """'Show all accounts' deselects every account grid, which reverts the chart via pick_account."""
    return [[] for _ in GROUP_KEYS]
