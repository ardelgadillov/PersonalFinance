"""Cash flow page: income vs spending by month, spending by category, category trend, Sankey, merchants."""
from __future__ import annotations

import datetime as dt

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dcc, html, no_update

from finance import cashflow as cf
from finance import services
from finance import transactions as tx
from finance.app.charts import CONFIG, base_layout, colors, empty_figure, glow, rgba
from finance.app.components import grid, money_col, panel

PERIODS = ["month", "12m", "ytd", "last_year", "24m", "all", "custom"]
PERSIST = {"persistence": True, "persistence_type": "local"}


def _money(v: float | None) -> str:
    if v is None:
        return "-"
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


def _stat(label: str, value: str, hint: str | None = None, cls: str = "") -> html.Div:
    return html.Div([html.Div(label, className="stat-label"), html.Div(value, className=f"stat-value {cls}"),
                     html.Div(hint, className="stat-hint") if hint else None], className="stat-chip")


def _graph(id_: str, height: int) -> dcc.Graph:
    return dcc.Graph(id=id_, config=CONFIG, style={"height": f"{height}px"})


def layout() -> html.Div:
    accounts = services.account_choices()
    filters = dbc.Row([
        dbc.Col(dbc.Select(id="cf-period", value="12m", size="sm", **PERSIST,
                           options=[{"label": tx.PERIODS[k], "value": k} for k in PERIODS]), lg=2, md=4),
        dbc.Col(dcc.DatePickerRange(id="cf-dates", display_format="YYYY-MM-DD", clearable=True, **PERSIST),
                id="cf-dates-col", width="auto"),
        dbc.Col(dcc.Dropdown(id="cf-accounts", multi=True, placeholder="All visible accounts", **PERSIST,
                             options=[{"label": a["label"], "value": a["id"]} for a in accounts]), lg=4, md=8),
    ], className="g-2 mb-3 align-items-center")
    merchant_cols = [
        {"field": "merchant", "headerName": "Merchant", "minWidth": 200, "tooltipField": "example"},
        {"field": "category", "headerName": "Category", "minWidth": 150},
        money_col("total", "Total", minWidth=120, cellClassRules={}),
        {"field": "transactions", "headerName": "Transactions", "type": "rightAligned", "maxWidth": 140},
        money_col("average", "Average", minWidth=110, cellClassRules={}),
        {"field": "last", "headerName": "Last", "maxWidth": 120},
    ]
    return html.Div([
        filters,
        html.Div(id="cf-kpis", className="stat-strip mb-3"),
        panel(html.Span("Income and spending by month", id="cf-monthly-title"), _graph("cf-monthly", 340),
              subtitle=html.Span(id="cf-monthly-subtitle")),
        dbc.Row([
            dbc.Col(panel("Spending by category", html.Div(_graph("cf-categories", 420), className="scroll-y"),
                          subtitle="Click a bar to see its monthly trend."), lg=6),
            dbc.Col(panel("Category trend",
                          dcc.Dropdown(id="cf-trend-cat", clearable=False, className="mb-2", **PERSIST),
                          _graph("cf-trend", 360)), lg=6),
        ], className="g-3"),
        panel("Where the money goes", _graph("cf-sankey", 460),
              subtitle="Income sources on the left, spending categories on the right."),
        panel("Top merchants", grid("cf-merchants", merchant_cols, [], height=420, row_id="params.data.merchant"),
              subtitle="Spending grouped by merchant (bank descriptions with reference numbers removed)."),
    ])


def _range(period, start, end):
    if period == "custom":
        return (dt.date.fromisoformat(start) if start else None, dt.date.fromisoformat(end) if end else None)
    return tx.period_range(period or "12m")


def _load(period, start, end, accounts):
    lo, hi = _range(period, start, end)
    df = services.read(cf.load_flows, lo, hi, accounts or None)
    return df, cf.month_range(lo, hi, df)


# --------------------------------------------------------------------------- figures
def monthly_figure(m, theme) -> go.Figure:
    c = colors(theme)
    if m.empty or not (m["income"].any() or m["spending"].any()):
        return empty_figure(theme, height=340)
    rate = [f"{r:.0%}" if r is not None else "n/a" for r in m["savings_rate"]]
    fig = go.Figure([
        go.Bar(x=m["month"], y=m["income"], name="Income", marker={"color": c["income"], "cornerradius": 4},
               hovertemplate="%{y:$,.0f}<extra>Income</extra>"),
        go.Bar(x=m["month"], y=m["spending"], name="Spending", marker={"color": c["spending"], "cornerradius": 4},
               hovertemplate="%{y:$,.0f}<extra>Spending</extra>"),
        go.Scatter(x=m["month"], y=m["net"], name="Net", mode="lines+markers", customdata=rate,
                   line={"color": c["text"], "width": 2}, marker={"size": 8, "color": c["text"],
                                                                 "line": {"color": c["surface"], "width": 2}},
                   hovertemplate="%{y:$,.0f} (savings rate %{customdata})<extra>Net</extra>"),
    ])
    fig.update_layout(**base_layout(theme, height=340, barmode="group", bargap=0.3, bargroupgap=0.06,
                                    hovermode="x unified"))
    fig.update_xaxes(tickformat="%b %Y", dtick="M1" if len(m) <= 14 else "M3", hoverformat="%B %Y")
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    glow(fig, theme)  # bloom on the income/spending bars and the Net line
    return fig


def category_figure(cats, theme) -> go.Figure:
    c = colors(theme)
    cats = cats[cats["total"] > 0]
    if cats.empty:
        return empty_figure(theme, height=420)
    labels = [f"${t:,.0f}  {s:.0%}" for t, s in zip(cats["total"], cats["share"])]
    fig = go.Figure(go.Bar(
        x=cats["total"], y=cats["category"], orientation="h", text=labels, textposition="outside",
        textfont={"color": c["muted"], "size": 12}, cliponaxis=False,
        marker={"color": c["spending"], "cornerradius": 4},
        customdata=list(zip(cats["monthly_avg"], cats["transactions"])),
        hovertemplate="<b>%{y}</b><br>%{x:$,.0f} total<br>%{customdata[0]:$,.0f} per month"
                      "<br>%{customdata[1]} transactions<extra></extra>",
    ))
    height = max(260, 30 * len(cats) + 40)
    fig.update_layout(**base_layout(theme, height=height, showlegend=False, bargap=0.25,
                                    margin={"l": 8, "r": 90, "t": 8, "b": 8}))
    fig.update_yaxes(autorange="reversed", showgrid=False, zeroline=False, tickfont={"color": c["text"]})
    fig.update_xaxes(showgrid=True, tickprefix="$", tickformat="~s", zeroline=True)
    glow(fig, theme)
    return fig


def trend_figure(trend, category: str | None, kind: str, theme, daily: bool = False) -> go.Figure:
    c = colors(theme)
    if not category or trend.empty or not trend["amount"].any():
        return empty_figure(theme, "Choose a category", height=360)
    color = c["income"] if kind == "income" else c["spending"]
    avg = float(trend["amount"].mean())
    fig = go.Figure(go.Bar(x=trend["month"], y=trend["amount"], name=category,
                           marker={"color": color, "cornerradius": 4},
                           hovertemplate=("%{x|%a %b %d}" if daily else "%{x|%B %Y}")
                           + "<br>%{y:$,.0f}<extra></extra>"))
    fig.add_hline(y=avg, line={"color": c["muted"], "width": 2, "dash": "dash"},
                  annotation={"text": f"{'Daily average' if daily else 'Average'} {_money(avg)}", "font": {"color": c["muted"]},
                              "xanchor": "left", "x": 0, "yanchor": "bottom"})
    fig.update_layout(**base_layout(theme, height=360, showlegend=False, bargap=0.3))
    if daily:
        fig.update_xaxes(tickformat="%b %d", dtick=7 * 86400000, tickangle=0)
    else:
        step = 1 if len(trend) <= 6 else 2 if len(trend) <= 14 else 3 if len(trend) <= 30 else 6
        fig.update_xaxes(tickformat="%b '%y", dtick=f"M{step}", tickangle=0)
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    glow(fig, theme)
    return fig


def daily_figure(cur, prev, today: dt.date, month_end: dt.date, theme) -> go.Figure:
    """This month by day: income/spending bars, net so far, and last month's net so far (grey)."""
    c = colors(theme)
    so_far = cur[cur["day"] <= pd.Timestamp(today)]
    bars = so_far.replace({"income": {0.0: None}, "spending": {0.0: None}})  # no stubs on empty days
    # last month's running net placed on the same day numbers of this month
    prev = prev[prev["day_of_month"] <= month_end.day]
    prev_x = [pd.Timestamp(today.replace(day=int(d))) for d in prev["day_of_month"]]
    prev_label = f"Net so far, {prev['day'].iloc[0]:%B}" if len(prev) else "Net so far, last month"
    fig = go.Figure([
        go.Bar(x=bars["day"], y=bars["income"], name="Income", marker={"color": c["income"], "cornerradius": 4},
               hovertemplate="%{y:$,.0f}<extra>Income</extra>"),
        go.Bar(x=bars["day"], y=bars["spending"], name="Spending",
               marker={"color": c["spending"], "cornerradius": 4}, hovertemplate="%{y:$,.0f}<extra>Spending</extra>"),
        go.Scatter(x=prev_x, y=prev["cum_net"], name=prev_label, mode="lines",
                   line={"color": c["neutral"], "width": 2},
                   hovertemplate="%{y:$,.0f}<extra>" + prev_label + "</extra>"),
        go.Scatter(x=so_far["day"], y=so_far["cum_net"], name=f"Net so far, {today:%B}", mode="lines+markers",
                   line={"color": c["text"], "width": 2},
                   marker={"size": 8, "color": c["text"], "line": {"color": c["surface"], "width": 2}},
                   hovertemplate="%{y:$,.0f}<extra>Net so far</extra>"),
    ])
    fig.update_layout(**base_layout(theme, height=340, barmode="group", bargap=0.25, bargroupgap=0.06,
                                    hovermode="x unified"))
    fig.update_xaxes(range=[pd.Timestamp(today.replace(day=1)) - pd.Timedelta(hours=12),
                            pd.Timestamp(month_end) + pd.Timedelta(hours=12)],
                     tickformat="%b %d", dtick=2 * 86400000, hoverformat="%a %b %d")
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    glow(fig, theme)  # bloom on the income/spending bars and the running-net lines
    return fig


def sankey_figure(data: dict, theme) -> go.Figure:
    c = colors(theme)
    if not data["labels"]:
        return empty_figure(theme, height=460)
    node_color = {"income": c["income"], "expense": c["spending"], "savings": c["savings"],
                  "deficit": c["neutral"], "hub": c["accent"]}
    nodes = [node_color[k] for k in data["kinds"]]
    links = [rgba(nodes[s] if data["kinds"][s] != "hub" else nodes[t], 0.35)
             for s, t in zip(data["source"], data["target"])]
    totals = [0.0] * len(data["labels"])
    for s, t, v in zip(data["source"], data["target"], data["value"]):
        totals[t if data["kinds"][t] != "hub" else s] += v
    totals[0] = sum(v for t, v in zip(data["target"], data["value"]) if t == 0)
    labels = [f"{lbl}  {_money(v)}" for lbl, v in zip(data["labels"], totals)]
    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node={"label": labels, "color": nodes, "pad": 14, "thickness": 14, "line": {"width": 0},
              "hovertemplate": "%{label}<extra></extra>"},
        link={"source": data["source"], "target": data["target"], "value": data["value"], "color": links,
              "hovertemplate": "%{source.label} to %{target.label}<br>%{value:$,.0f}<extra></extra>"},
        textfont={"color": c["text"], "family": "Inter, sans-serif", "size": 12},
    ))
    fig.update_layout(**base_layout(theme, height=460, showlegend=False))
    return fig


# --------------------------------------------------------------------------- callbacks
@callback(Output("cf-kpis", "children"), Output("cf-monthly", "figure"), Output("cf-categories", "figure"),
          Output("cf-categories", "style"), Output("cf-sankey", "figure"), Output("cf-merchants", "rowData"),
          Output("cf-trend-cat", "options"), Output("cf-trend-cat", "value"), Output("cf-dates-col", "style"),
          Output("cf-monthly-title", "children"), Output("cf-monthly-subtitle", "children"),
          Input("cf-period", "value"), Input("cf-dates", "start_date"), Input("cf-dates", "end_date"),
          Input("cf-accounts", "value"), Input("theme", "data"), Input("sync-version", "data"),
          State("cf-trend-cat", "value"))
def update(period, start, end, accounts, theme, _version, trend_cat):
    df, months = _load(period, start, end, accounts)
    k = cf.kpis(df, months)
    rate = k["savings_rate"]
    kpis = [
        _stat("Income", _money(k["income"])),
        _stat("Spending", _money(k["spending"])),
        _stat("Net savings", _money(k["net"]), cls="amount-neg" if k["net"] < 0 else "amount-pos"),
        _stat("Savings rate", f"{rate:.0%}" if rate is not None else "-", "of income"),
        _stat("Average spending", _money(k["avg_spending"]), f"per month over {k['months']} month(s)"),
    ]
    if period == "month":
        today = dt.date.today()
        first, month_end = cf.month_bounds(today)
        kpis[-1] = _stat("Average spending", _money(k["spending"] / today.day), f"per day, {today.day} day(s) so far")
        prev_lo, prev_hi = cf.previous_month(today)
        prev = cf.daily(services.read(cf.load_flows, prev_lo, prev_hi, accounts or None), prev_lo, prev_hi)
        main_fig = daily_figure(cf.daily(df, first, month_end), prev, today, month_end, theme)
        title = "Income and spending by day"
        subtitle = ("Transfers between your accounts are excluded. The lines are the running net (income minus "
                    "spending) this month and, in grey, last month on the same day.")
    else:
        main_fig = monthly_figure(cf.monthly(df, months), theme)
        title = "Income and spending by month"
        subtitle = "Transfers between your accounts are excluded. Hover a month for its savings rate."
    spend = cf.by_category(df, "expense", len(months))
    income = cf.by_category(df, "income", len(months))
    cat_fig = category_figure(spend, theme)
    options = ([{"label": f"{c} (spending)", "value": c} for c in spend.loc[spend["total"] > 0, "category"]]
               + [{"label": f"{c} (income)", "value": c} for c in income.loc[income["total"] > 0, "category"]])
    values = [o["value"] for o in options]
    if trend_cat not in values:
        trend_cat = values[0] if values else None
    return (kpis, main_fig, cat_fig,
            {"height": f"{cat_fig.layout.height}px"}, sankey_figure(cf.sankey(df), theme),
            cf.top_merchants(df), options, trend_cat, {} if period == "custom" else {"display": "none"},
            title, subtitle)


@callback(Output("cf-trend", "figure"),
          Input("cf-trend-cat", "value"), Input("cf-period", "value"), Input("cf-dates", "start_date"),
          Input("cf-dates", "end_date"), Input("cf-accounts", "value"), Input("theme", "data"),
          Input("sync-version", "data"))
def update_trend(category, period, start, end, accounts, theme, _version):
    df, months = _load(period, start, end, accounts)
    if not category or df.empty:
        return trend_figure(df, None, "expense", theme)
    kinds = df.loc[df["category"] == category, "kind"]
    kind = kinds.iloc[0] if len(kinds) else "expense"
    if period == "month":
        first, month_end = cf.month_bounds(dt.date.today())
        return trend_figure(cf.category_daily(df, category, first, dt.date.today()), category, kind, theme,
                            daily=True)
    return trend_figure(cf.category_trend(df, category, months), category, kind, theme)


@callback(Output("cf-trend-cat", "value", allow_duplicate=True), Input("cf-categories", "clickData"),
          prevent_initial_call=True)
def pick_category(click):
    try:
        return click["points"][0]["y"]
    except (TypeError, KeyError, IndexError):
        return no_update
