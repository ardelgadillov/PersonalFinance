"""Net worth page: KPIs, assets and debts over time by group, monthly change, composition, accounts."""
from __future__ import annotations

import datetime as dt

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, callback, dcc, html

from finance import networth as nw
from finance import services
from finance import transactions as tx
from finance.app.charts import CONFIG, base_layout, colors, empty_figure, glow, rgba
from finance.app.components import grid, money_col, panel

PERIODS = ["30d", "90d", "ytd", "12m", "last_year", "24m", "all", "custom"]
PERSIST = {"persistence": True, "persistence_type": "local"}


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
        {"field": "group", "headerName": "Group", "minWidth": 130},
        {"field": "account", "headerName": "Account", "minWidth": 200},
        {"field": "institution", "headerName": "Institution", "minWidth": 150},
        money_col("balance", "Balance", minWidth=120),
        money_col("change", "Change in period", minWidth=130),
        {"field": "updated", "headerName": "Last balance", "maxWidth": 140,
         "headerTooltip": "Date of the latest balance snapshot (one per sync day)"},
    ]
    return html.Div([
        dbc.Alert(["Some accounts have no type yet, so they are grouped by the sign of their balance. ",
                   dcc.Link("Set types in Settings", href="/settings")],
                  color="warning", className="py-2") if untyped else None,
        filters,
        html.Div(id="nw-kpis", className="stat-strip mb-3"),
        panel("Net worth over time", _graph("nw-history", 380), subtitle=html.Span(id="nw-history-subtitle")),
        dbc.Row([
            dbc.Col(panel("Change by month", _graph("nw-monthly", 300),
                          subtitle="Net worth at month end compared with the previous month end."), lg=7),
            dbc.Col(panel("Composition", _graph("nw-composition", 300),
                          subtitle="Balance by group at the end of the period."), lg=5),
        ], className="g-3"),
        panel("Accounts",
              grid("nw-accounts-grid", account_cols, [], height=360,
                   dashGridOptions={"rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                     "enableClickSelection": True}}),
              subtitle=("Click an account to see its balance history. Choose which accounts count in Settings "
                        "(Include in net worth, Hidden).")),
        panel(html.Span("Account history", id="nw-account-title"), _graph("nw-account", 300)),
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


def monthly_figure(m: pd.DataFrame, theme) -> go.Figure:
    c = colors(theme)
    if m.empty:
        return empty_figure(theme, height=300)
    bar_colors = [c["savings"] if v >= 0 else c["spending"] for v in m["change"]]
    fig = go.Figure(go.Bar(
        x=m["month"], y=m["change"], marker={"color": bar_colors, "cornerradius": 4},
        customdata=m["net_worth"], name="Change",
        hovertemplate="%{x|%B %Y}<br>%{y:+$,.0f}<br>Net worth %{customdata:$,.0f}<extra></extra>"))
    fig.update_layout(**base_layout(theme, height=300, showlegend=False, bargap=0.3))
    step = 1 if len(m) <= 8 else 2 if len(m) <= 16 else 3 if len(m) <= 30 else 6
    fig.update_xaxes(tickformat="%b '%y", dtick=f"M{step}", tickangle=0)
    fig.update_yaxes(tickprefix="$", tickformat="~s")
    glow(fig, theme)
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
@callback(Output("nw-kpis", "children"), Output("nw-history", "figure"), Output("nw-history-subtitle", "children"),
          Output("nw-monthly", "figure"), Output("nw-composition", "figure"), Output("nw-accounts-grid", "rowData"),
          Output("nw-dates-col", "style"),
          Input("nw-period", "value"), Input("nw-dates", "start_date"), Input("nw-dates", "end_date"),
          Input("nw-accounts", "value"), Input("theme", "data"), Input("sync-version", "data"))
def update(period, start, end, accounts, theme, _version):
    daily, accts, full = _load(period, start, end, accounts)
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
    if full.empty:
        subtitle = "History starts with the first sync: one balance snapshot per account per day."
    else:
        subtitle = (f"Debts are shown below zero; the line is net worth. History starts at the first sync "
                    f"({full.index.min():%b %d, %Y}).")
    return (kpis, history_figure(groups, theme), subtitle, monthly_figure(nw.monthly_change(groups), theme),
            composition_figure(groups, theme), nw.account_rows(daily, accts),
            {} if period == "custom" else {"display": "none"})


@callback(Output("nw-account", "figure"), Output("nw-account-title", "children"),
          Input("nw-accounts-grid", "selectedRows"), Input("nw-accounts-grid", "rowData"),
          Input("nw-period", "value"), Input("nw-dates", "start_date"), Input("nw-dates", "end_date"),
          Input("theme", "data"), Input("sync-version", "data"))
def update_account(selected, rows, period, start, end, theme, _version):
    ids = {r["id"] for r in rows or []}
    row = selected[0] if selected and selected[0]["id"] in ids else (rows[0] if rows else None)
    if row is None:
        return account_figure(None, "", "cash", theme), "Account history"
    daily, _ = services.read(nw.load, [row["id"]])
    daily = nw.clip(daily, *_range(period, start, end))
    series = daily[row["id"]] if row["id"] in daily else None
    title = f"{row['account']}" + (f" ({row['institution']})" if row.get("institution") else "")
    if not selected:
        title += " (click another account in the table)"
    return account_figure(series, row["account"], row.get("group_key") or "cash", theme), title
