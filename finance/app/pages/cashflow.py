"""Cash flow page: income vs spending by month, spending by category, category trend, Sankey, merchants."""
from __future__ import annotations

import datetime as dt
import time

import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from finance import cashflow as cf
from finance import services
from finance import transactions as tx
from finance.app.charts import CONFIG, base_layout, colors, empty_figure, glow, rgba
from finance.app.components import alert, editable, grid, message, money_col, panel
from finance.categorize import description_key

PERIODS = ["month", "last_month", "12m", "ytd", "last_year", "24m", "all", "custom"]
PERSIST = {"persistence": True, "persistence_type": "local"}
DAILY_PERIODS = {"month", "last_month"}  # a single month is shown day by day


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
    names = [c["name"] for c in services.category_choices()]
    txn_actions = html.Div([
        html.Span(id="cf-txn-filter-label", className="text-muted small me-auto"),
        dbc.Button([html.I(className="bi bi-x-lg me-1"), "Clear filter"], id="cf-txn-clear",
                   color="secondary", outline=True, size="sm"),
        dbc.Button([html.I(className="bi bi-funnel me-1"), "Create rule"], id="cf-rule-open",
                   color="primary", outline=True, size="sm"),
    ], className="d-flex align-items-center gap-2 flex-wrap")
    return html.Div([
        dcc.Store(id="cf-txn-cat"),
        dcc.Store(id="cf-txn-selection", data=[]),
        dcc.Store(id="cf-txn-refresh", data=0),
        message("cf-txn-msg"),
        filters,
        html.Div(id="cf-kpis", className="stat-strip mb-3"),
        panel(html.Span("Income and spending by month", id="cf-monthly-title"), _graph("cf-monthly", 340),
              subtitle=html.Span(id="cf-monthly-subtitle")),
        panel("Where the money goes", _graph("cf-sankey", 460),
              subtitle="Income sources on the left, spending categories on the right. Click a category to filter "
                       "the transactions below."),
        dbc.Row([
            dbc.Col(panel("Spending by category", html.Div(_graph("cf-categories", 420), className="scroll-y"),
                          subtitle="Click a bar to see its trend and filter the transactions below."), lg=6),
            dbc.Col(panel("Category trend",
                          dcc.Dropdown(id="cf-trend-cat", clearable=False, className="mb-2", **PERSIST),
                          _graph("cf-trend", 360)), lg=6),
        ], className="g-3"),
        panel("Transactions", txn_actions, html.Div(className="mb-2"),
              grid("cf-txn-grid", _txn_cols(names), [], height=480,
                   dashGridOptions={"rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                     "enableClickSelection": True}}),
              subtitle="All transactions in the period (or the selected category). Edit a category in place, or "
                       "select a row and Create rule. Manual edits are never changed by syncs or rules."),
        _rule_modal(),
    ])


def _txn_cols(names: list[str]) -> list[dict]:
    return [
        {"field": "date", "headerName": "Date", "maxWidth": 120, "minWidth": 110},
        {"field": "account", "headerName": "Account", "minWidth": 140, "tooltipField": "account"},
        {"field": "description", "headerName": "Description", "minWidth": 240, "flex": 2,
         "tooltipField": "description"},
        money_col("amount", "Amount", maxWidth=130, minWidth=110),
        editable({"field": "category", "headerName": "Category", "minWidth": 160,
                  "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": [""] + names},
                  "cellClassRules": {"cell-missing": "!params.value"}}),
        {"field": "how", "headerName": "Set by", "maxWidth": 100,
         "headerTooltip": "Manual (your edit) beats Empower, which beats Rule"},
        {"field": "flags", "headerName": "Status", "maxWidth": 130},
    ]


def _rule_modal() -> dbc.Modal:
    return dbc.Modal(id="cf-rule-modal", is_open=False, size="lg", children=[
        dbc.ModalHeader(dbc.ModalTitle("New rule")),
        dbc.ModalBody([
            dbc.Row([
                dbc.Col([dbc.Label("Text to match", className="small fw-semibold"),
                         dbc.Input(id="cf-rule-pattern", size="sm", debounce=True)], md=6),
                dbc.Col([dbc.Label("In field", className="small fw-semibold"),
                         dbc.Select(id="cf-rule-field", size="sm", value="description",
                                    options=[{"label": f.title(), "value": f} for f in tx.RULE_FIELDS])], md=3),
                dbc.Col([dbc.Label("Match", className="small fw-semibold"),
                         dbc.Select(id="cf-rule-match", size="sm", value="contains",
                                    options=[{"label": m.title(), "value": m} for m in tx.MATCH_TYPES])], md=3),
            ], className="g-2 mb-2"),
            dbc.Row([
                dbc.Col([dbc.Label("Category", className="small fw-semibold"),
                         dcc.Dropdown(id="cf-rule-category", placeholder="Choose a category")], md=6),
                dbc.Col([dbc.Label("Only for account", className="small fw-semibold"),
                         dcc.Dropdown(id="cf-rule-account", placeholder="Any account")], md=6),
            ], className="g-2 mb-3"),
            html.Div(id="cf-rule-preview", className="small"),
            html.Div(id="cf-rule-error"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id="cf-rule-cancel", color="secondary", outline=True, size="sm"),
            dbc.Button("Save rule", id="cf-rule-save", color="primary", size="sm"),
        ]),
    ])


def _range(period, start, end):
    if period == "custom":
        return (dt.date.fromisoformat(start) if start else None, dt.date.fromisoformat(end) if end else None)
    return tx.period_range(period or "12m")


def _load(period, start, end, accounts):
    lo, hi = _range(period, start, end)
    df = services.read(cf.load_flows, lo, hi, accounts or None)
    return df, cf.month_range(lo, hi, df)


def _prev_cumnet(period, months, accounts):
    """Running net of the equivalent period just before `months`, aligned by position (None when not applicable)."""
    if period == "all" or len(months) == 0:
        return None
    span = len(months)
    prev_end = months[0] - pd.offsets.MonthBegin(1)       # first day of the previous month
    prev_start = months[0] - pd.offsets.MonthBegin(span)  # first day of the month `span` months earlier
    prev_months = pd.date_range(prev_start, prev_end, freq="MS")
    lo, hi = prev_start.date(), (prev_end + pd.offsets.MonthEnd(0)).date()
    prev_df = services.read(cf.load_flows, lo, hi, accounts or None)
    return cf.monthly(prev_df, prev_months)["net"].cumsum()


# --------------------------------------------------------------------------- figures
def monthly_figure(m, prev_cum, theme) -> go.Figure:
    """Income/spending bars per month; the Net line is the running (cumulative) net for the period, with the
    previous equivalent period's running net overlaid in grey for comparison."""
    c = colors(theme)
    if m.empty or not (m["income"].any() or m["spending"].any()):
        return empty_figure(theme, height=340)
    cum = m["net"].cumsum()
    rate = [f"{r:.0%}" if r is not None else "n/a" for r in m["savings_rate"]]
    traces = [
        go.Bar(x=m["month"], y=m["income"], name="Income", marker={"color": c["income"], "cornerradius": 4},
               hovertemplate="%{y:$,.0f}<extra>Income</extra>"),
        go.Bar(x=m["month"], y=m["spending"], name="Spending", marker={"color": c["spending"], "cornerradius": 4},
               hovertemplate="%{y:$,.0f}<extra>Spending</extra>"),
    ]
    if prev_cum is not None and len(prev_cum):  # last period's running net on the same positions, in grey
        k = min(len(prev_cum), len(m))
        traces.append(go.Scatter(x=list(m["month"].iloc[:k]), y=list(prev_cum[:k]), name="Net, last period",
                                 mode="lines", line={"color": rgba(c["neutral"], 0.4), "width": 2},
                                 hovertemplate="%{y:$,.0f}<extra>Net, last period</extra>"))
    traces.append(go.Scatter(
        x=m["month"], y=cum, name="Net (cumulative)", mode="lines+markers", customdata=list(zip(m["net"], rate)),
        line={"color": c["text"], "width": 2},
        marker={"size": 8, "color": c["text"], "line": {"color": c["surface"], "width": 2}},
        hovertemplate="%{y:$,.0f} running<br>%{customdata[0]:$,.0f} this month "
                      "(savings rate %{customdata[1]})<extra>Net</extra>"))
    fig = go.Figure(traces)
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
                   line={"color": rgba(c["neutral"], 0.4), "width": 2},
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
          Output("cf-categories", "style"), Output("cf-sankey", "figure"),
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
    if period in DAILY_PERIODS:
        anchor = dt.date.today() if period == "month" else (dt.date.today().replace(day=1) - dt.timedelta(days=1))
        first, month_end = cf.month_bounds(anchor)
        cutoff = dt.date.today() if period == "month" else month_end  # a full month when it is last month
        if period == "month":
            kpis[-1] = _stat("Average spending", _money(k["spending"] / anchor.day),
                             f"per day, {anchor.day} day(s) so far")
        prev_lo, prev_hi = cf.previous_month(anchor)
        prev = cf.daily(services.read(cf.load_flows, prev_lo, prev_hi, accounts or None), prev_lo, prev_hi)
        main_fig = daily_figure(cf.daily(df, first, month_end), prev, cutoff, month_end, theme)
        title = "Income and spending by day"
        subtitle = ("Transfers between your accounts are excluded. The lines are the running net (income minus "
                    "spending) this month and, in grey, the previous month on the same day.")
    else:
        prev_cum = _prev_cumnet(period, months, accounts)
        main_fig = monthly_figure(cf.monthly(df, months), prev_cum, theme)
        title = "Income and spending by month"
        subtitle = ("Transfers excluded. The line is the running (cumulative) net; the grey line is the previous "
                    "equivalent period for comparison.")
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
            options, trend_cat, {} if period == "custom" else {"display": "none"},
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
    if period in DAILY_PERIODS:
        anchor = dt.date.today() if period == "month" else (dt.date.today().replace(day=1) - dt.timedelta(days=1))
        first, month_end = cf.month_bounds(anchor)
        cutoff = dt.date.today() if period == "month" else month_end
        return trend_figure(cf.category_daily(df, category, first, cutoff), category, kind, theme, daily=True)
    return trend_figure(cf.category_trend(df, category, months), category, kind, theme)


@callback(Output("cf-trend-cat", "value", allow_duplicate=True), Input("cf-categories", "clickData"),
          prevent_initial_call=True)
def pick_category(click):
    try:
        return click["points"][0]["y"]
    except (TypeError, KeyError, IndexError):
        return no_update


# --------------------------------------------------------------------------- transactions table (filter + edit)
@callback(Output("cf-txn-cat", "data"),
          Input("cf-categories", "clickData"), Input("cf-sankey", "clickData"), Input("cf-txn-clear", "n_clicks"),
          prevent_initial_call=True)
def set_txn_filter(cat_click, sankey_click, _clear):
    trig = ctx.triggered_id
    if trig == "cf-txn-clear":
        return None
    valid = {c["name"] for c in services.category_choices()} | {cf.UNCATEGORIZED}
    try:
        if trig == "cf-categories":
            label = cat_click["points"][0]["y"]
        elif trig == "cf-sankey":
            label = sankey_click["points"][0]["label"].split("  ")[0].strip()  # drop the "  $amount" suffix
        else:
            return no_update
    except (TypeError, KeyError, IndexError):
        return no_update
    return label if label in valid else no_update


@callback(Output("cf-txn-grid", "rowData"), Output("cf-txn-filter-label", "children"),
          Input("cf-period", "value"), Input("cf-dates", "start_date"), Input("cf-dates", "end_date"),
          Input("cf-accounts", "value"), Input("cf-txn-cat", "data"), Input("cf-txn-refresh", "data"),
          Input("sync-version", "data"))
def load_cf_txns(period, start, end, accounts, category, _refresh, _version):
    lo, hi = _range(period, start, end)
    if category == cf.UNCATEGORIZED:
        cat_ids = [tx.UNCATEGORIZED]
    elif category:
        ids = {c["name"]: c["id"] for c in services.category_choices()}
        cat_ids = [ids[category]] if category in ids else []
    else:
        cat_ids = []
    f = tx.TxnFilter(start=lo, end=hi, account_ids=accounts or [], category_ids=cat_ids)
    rows = services.read(tx.query, f)
    label = (f"Filtered to {category} ({len(rows):,})" if category
             else f"All transactions in the period ({len(rows):,})")
    return rows, label


@callback(Output("cf-txn-msg", "children", allow_duplicate=True), Output("cf-txn-grid", "rowTransaction"),
          Input("cf-txn-grid", "cellValueChanged"), prevent_initial_call=True)
def cf_save_cell(changes):
    if not changes:
        return no_update, no_update
    ids_by_name = {c["name"]: c["id"] for c in services.category_choices()}
    done, ids = [], []
    try:
        for ch in changes:
            if ch["colId"] != "category":
                continue
            tid, value = ch["data"]["id"], ch["value"]
            if value:
                services.write(tx.set_category, [tid], ids_by_name[value])
                done.append(f"category set to {value}")
            else:
                services.write(tx.reset_category, [tid])
                done.append("category returned to automatic")
            ids.append(tid)
    except (tx.TransactionError, KeyError) as exc:
        return alert(f"Not saved: {exc}", "danger"), no_update
    if not ids:
        return no_update, no_update
    return alert("Saved: " + ", ".join(dict.fromkeys(done)) + ".", duration=3000), \
        {"update": services.read(tx.get_rows, ids)}


@callback(Output("cf-txn-selection", "data"), Input("cf-txn-grid", "selectedRows"), prevent_initial_call=True)
def cf_remember_selection(rows):
    return rows or []


# --------------------------------------------------------------------------- rule modal (cash-flow copy)
@callback(Output("cf-rule-modal", "is_open"), Output("cf-rule-pattern", "value"),
          Output("cf-rule-category", "options"), Output("cf-rule-category", "value"),
          Output("cf-rule-account", "options"), Output("cf-rule-account", "value"), Output("cf-rule-error", "children"),
          Input("cf-rule-open", "n_clicks"), Input("cf-rule-cancel", "n_clicks"),
          State("cf-txn-selection", "data"), prevent_initial_call=True)
def cf_open_rule(opened, _cancel, selected):
    if ctx.triggered_id == "cf-rule-cancel" or not opened:
        return (False,) + (no_update,) * 6
    cats = services.category_choices()
    accounts = services.account_choices()
    row = (selected or [None])[0]
    pattern = (description_key(row["description"]) or row["description"]) if row else ""
    cat_id = next((c["id"] for c in cats if row and c["name"] == row["category"]), None)
    return (True, pattern, [{"label": c["name"], "value": c["id"]} for c in cats], cat_id,
            [{"label": a["label"], "value": a["id"]} for a in accounts], None, None)


@callback(Output("cf-rule-preview", "children"),
          Input("cf-rule-pattern", "value"), Input("cf-rule-field", "value"), Input("cf-rule-match", "value"),
          Input("cf-rule-category", "value"), Input("cf-rule-account", "value"), Input("cf-rule-modal", "is_open"),
          prevent_initial_call=True)
def cf_preview_rule(pattern, field, match, category_id, account_id, is_open):
    if not is_open or not (pattern or "").strip():
        return html.Div("Type the text to match, e.g. a merchant name.", className="text-muted")
    p = services.read(tx.preview_rule, pattern, field, match, category_id, account_id)
    keep = []
    if p.keep_empower:
        keep.append(f"{p.keep_empower:,} keep their Empower category")
    if p.keep_manual:
        keep.append(f"{p.keep_manual:,} keep your manual category")
    return html.Div([
        html.Div([html.Strong(f"Matches {p.matches:,} transaction(s)"),
                  f": {p.will_change:,} would be recategorized" + (f"; {'; '.join(keep)}" if keep else "") + "."]),
        html.Ul([html.Li(html.Code(sm)) for sm in p.samples], className="mb-0 mt-1") if p.samples else None,
    ])


@callback(Output("cf-rule-modal", "is_open", allow_duplicate=True),
          Output("cf-rule-error", "children", allow_duplicate=True),
          Output("cf-txn-msg", "children", allow_duplicate=True),
          Output("cf-txn-refresh", "data", allow_duplicate=True),
          Input("cf-rule-save", "n_clicks"),
          State("cf-rule-pattern", "value"), State("cf-rule-field", "value"), State("cf-rule-match", "value"),
          State("cf-rule-category", "value"), State("cf-rule-account", "value"), prevent_initial_call=True)
def cf_save_rule(clicks, pattern, field, match, category_id, account_id):
    if not clicks:
        return (no_update,) * 4
    try:
        rule, changed = services.write(tx.add_rule, pattern, category_id, field, match, account_id)
    except tx.TransactionError as exc:
        return no_update, alert(str(exc), "danger", duration=None), no_update, no_update
    return False, None, alert(f"Rule '{rule.pattern}' saved; {changed:,} transaction(s) recategorized."), time.time()
