"""Optimizer page: Markowitz optimal mixes of an account's fund menu (401k, HSA, ...) vs the current mix."""
from __future__ import annotations

import base64
import datetime as dt
import threading
from collections import OrderedDict

import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from finance import benchmarks as bm
from finance import fundmenu as fm
from finance import investments as inv
from finance import optimizer as op
from finance import services
from finance.app.charts import CONFIG, base_layout, colors, empty_figure
from finance.app.components import alert, editable, grid, panel
from finance.config import get_settings

PERSIST = {"persistence": True, "persistence_type": "local"}
HISTORY_YEARS = 16  # enough for a 10-year window plus walk-forward training
PCT = {"function": "params.value == null ? '' : d3.format('.1%')(params.value)"}
PCT_SIGNED = {"function": "params.value == null ? '' : d3.format('+.1%')(params.value)"}
NUM2 = {"function": "params.value == null ? '' : d3.format('.2f')(params.value)"}
# series identity on every chart of this page (validated categorical slots 1-4)
CURRENT, MAX_SHARPE, MIN_VAR, SELECTED = "Current mix", "Max Sharpe", "Min variance", "Selected"
SLOT = {CURRENT: 0, MAX_SHARPE: 1, MIN_VAR: 2, SELECTED: 3}


def _pct_col(field, header, signed=False, **kw):
    return {"field": field, "headerName": header, "valueFormatter": PCT_SIGNED if signed else PCT,
            "type": "rightAligned", **kw}


def _graph(id_: str, height: int) -> dcc.Graph:
    return dcc.Graph(id=id_, config=CONFIG, style={"height": f"{height}px"})


def _label(text, control, width=None):
    return dbc.Col(html.Div([html.Div(text, className="stat-label mb-1"), control]), width=width or "auto")


def layout() -> html.Div:
    accounts = services.read(inv.investment_accounts)
    default = next((a["id"] for a in accounts if a["type"] == "retirement"), accounts[0]["id"] if accounts else None)
    settings_row = dbc.Row([
        _label("Account", dbc.Select(id="opt-account", value=default, size="sm", **PERSIST, options=[
            {"label": a["label"] + (f" ({a['institution']})" if a["institution"] else ""), "value": a["id"]}
            for a in accounts]), width=3),
        _label("History used", dbc.Select(id="opt-lookback", value="5", size="sm", **PERSIST, options=[
            {"label": f"Last {y} year{'s' if y > 1 else ''}", "value": str(y)} for y in (1, 3, 5, 10)])),
        _label("Max per fund", dbc.InputGroup([dbc.Input(id="opt-cap", type="number", value=40, min=5, max=100,
                                                         step=5, debounce=True, **PERSIST),
                                               dbc.InputGroupText("%")], size="sm", style={"width": "120px"})),
        _label("Risk-free rate", dbc.InputGroup([dbc.Input(id="opt-rf", type="number", value=0, min=0, max=10,
                                                           step=0.25, debounce=True, **PERSIST),
                                                 dbc.InputGroupText("%")], size="sm", style={"width": "120px"})),
        _label("Walk-forward training", dbc.Select(id="opt-train", value="3", size="sm", **PERSIST, options=[
            {"label": f"{y} year{'s' if y > 1 else ''}", "value": str(y)} for y in (1, 2, 3, 5)])),
    ], className="g-3 mb-3 align-items-end")
    menu_cols = [
        {"field": "symbol", "headerName": "Ticker", "maxWidth": 120},
        editable({"field": "name", "headerName": "Name", "minWidth": 140, "tooltipField": "name"}),
        _pct_col("current", "Held", minWidth=85, maxWidth=110,
                 headerTooltip="Share of the account's latest holdings"),
    ]
    menu = panel(
        "Fund menu",
        dcc.Upload(id="opt-upload", className="upload-zone mb-2", accept=".csv,.txt", children=html.Div([
            html.I(className="bi bi-upload"), "Drop a CSV with a Ticker column (name optional), or click to choose"])),
        dbc.InputGroup([
            dbc.Textarea(id="opt-paste", placeholder="Or paste tickers: VFIAX, VBTLX ... (one fund per line may "
                                                     "include a name)", rows=2, style={"resize": "vertical"}),
            dbc.Button("Add", id="opt-add", color="primary", outline=True),
        ], size="sm", className="mb-2"),
        grid("opt-menu", menu_cols, [], height=260,
             dashGridOptions={"rowSelection": {"mode": "multiRow", "checkboxes": True, "headerCheckbox": True,
                                               "enableClickSelection": False}}),
        html.Div([
            dbc.Button([html.I(className="bi bi-plus-lg me-1"), "Add current holdings"], id="opt-add-holdings",
                       size="sm", color="secondary", outline=True),
            dbc.Button([html.I(className="bi bi-trash me-1"), "Remove selected"], id="opt-remove", size="sm",
                       color="danger", outline=True),
        ], className="d-flex gap-2 mt-2"),
        html.Div(id="opt-menu-msg", className="mt-2"),
        subtitle="The funds your plan lets you choose. Funds without a ticker cannot be priced and are skipped.",
    )
    frontier = panel(
        "Efficient frontier", _graph("opt-frontier", 400),
        html.Div([html.Div(id="opt-target-label", className="small text-muted mb-1"),
                  dcc.Slider(id="opt-target", min=0, max=1, step=0.0005, value=None, marks=None,
                             allow_direct_input=False, updatemode="mouseup")],
                 className="px-4 pb-2"),
        subtitle=("Expected annual return vs volatility from the history used (log returns, annualized). Drag "
                  "the slider to pick a portfolio on the frontier."),
    )
    stats_cols = [
        {"field": "portfolio", "headerName": "Portfolio", "minWidth": 150},
        _pct_col("ret", "Expected return", headerTooltip="Mean daily log return x 252 over the history used"),
        _pct_col("vol", "Volatility", headerTooltip="Annualized standard deviation of daily log returns"),
        {"field": "sharpe", "headerName": "Sharpe", "valueFormatter": NUM2, "type": "rightAligned"},
        _pct_col("cagr", "Realized CAGR", headerTooltip="Compound annual growth of the mix over the history used"),
        _pct_col("max_dd", "Max drawdown", signed=True),
    ]
    weight_cols = [
        {"field": "symbol", "headerName": "Ticker", "minWidth": 100, "tooltipField": "name",
         "headerTooltip": "Hover a ticker for the fund name"},
        _pct_col("current", "Current", minWidth=90), _pct_col("max_sharpe", MAX_SHARPE, minWidth=110),
        _pct_col("min_var", MIN_VAR, minWidth=115), _pct_col("selected", "Selected", minWidth=90),
    ]
    walk_cols = [
        {"field": "strategy", "headerName": "Strategy", "minWidth": 250},
        _pct_col("cagr", "CAGR"), _pct_col("vol", "Volatility"),
        {"field": "sharpe", "headerName": "Sharpe", "valueFormatter": NUM2, "type": "rightAligned"},
        _pct_col("max_dd", "Max drawdown", signed=True),
        _pct_col("vs_current", "CAGR vs current", signed=True),
    ]
    return html.Div([
        dcc.Store(id="opt-refresh", data=0),
        settings_row,
        dbc.Row([dbc.Col(menu, lg=5), dbc.Col(frontier, lg=7)], className="g-3"),
        html.Div(id="opt-msg"),
        dbc.Row([
            dbc.Col(panel("Weights", grid("opt-weights", weight_cols, [], height=330, row_id="params.data.symbol"),
                          subtitle="Long only, each fund capped at the max per fund. Click a ticker for its chart."),
                    lg=7),
            dbc.Col(panel("Current vs selected", _graph("opt-weights-chart", 330)), lg=5),
        ], className="g-3"),
        dcc.Store(id="opt-ticker-sym"),
        panel("Performance vs market", _graph("opt-ticker", 320),
              subtitle=html.Span(id="opt-ticker-subtitle"),
              actions=dcc.Dropdown(id="opt-bench", value=bm.DEFAULT, clearable=False, searchable=False,
                                   options=[{"label": v, "value": k} for k, v in bm.BENCHMARKS.items()],
                                   style={"minWidth": "210px"}, **PERSIST)),
        panel("Portfolios", grid("opt-stats", stats_cols, [], height=300, row_id="params.data.portfolio"),
              subtitle=html.Span(id="opt-stats-subtitle")),
        panel("In-sample backtest", _graph("opt-backtest", 340), subtitle=html.Span(id="opt-backtest-subtitle")),
        panel("Walk-forward test (out of sample)", _graph("opt-walk", 340),
              grid("opt-walk-stats", walk_cols, [], height=255, row_id="params.data.strategy"),
              subtitle=html.Span(id="opt-walk-subtitle")),
    ])


# --------------------------------------------------------------------------- data
# The optimizer page fires three callbacks at once (analysis, selected mix, ticker chart), all needing the
# same heavy Markowitz + walk-forward computation. A plain lru_cache does not dedupe *concurrent* calls, so
# the first open would run the (CPU-heavy, GIL-holding) optimization up to three times in parallel and stall
# the whole app. This bounded cache computes each key exactly once and makes the other callers wait for and
# reuse that result, keeping the server responsive.
_CACHE_MAX = 8
_analysis_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_analysis_cache_lock = threading.Lock()
_analysis_key_locks: dict[tuple, threading.Lock] = {}


def _analysis(db_url: str, account: str, lookback: int, cap: float, rf: float, train: int, refresh, version,
              day: dt.date):
    """(Analysis or None, info dict), computed once per key and shared by all three page callbacks."""
    key = (db_url, account, lookback, cap, rf, train, refresh, version, day)
    with _analysis_cache_lock:
        if key in _analysis_cache:
            _analysis_cache.move_to_end(key)
            return _analysis_cache[key]
        key_lock = _analysis_key_locks.setdefault(key, threading.Lock())
    with key_lock:  # only one caller computes this key; others wait here, then hit the cache below
        with _analysis_cache_lock:
            if key in _analysis_cache:
                _analysis_cache.move_to_end(key)
                return _analysis_cache[key]
        result = _compute_analysis(account, lookback, cap, rf, train)
        with _analysis_cache_lock:
            _analysis_cache[key] = result
            _analysis_cache.move_to_end(key)
            while len(_analysis_cache) > _CACHE_MAX:
                _analysis_cache.popitem(last=False)
            _analysis_key_locks.pop(key, None)
        return result


def _analysis_cache_clear() -> None:
    with _analysis_cache_lock:
        _analysis_cache.clear()
        _analysis_key_locks.clear()


_analysis.cache_clear = _analysis_cache_clear  # keep the lru_cache-style API used by the tests


def _compute_analysis(account: str, lookback: int, cap: float, rf: float, train: int):
    items = services.read(fm.menu, account)
    symbols = [i["symbol"] for i in items]
    info = {"names": {i["symbol"]: i["name"] for i in items}, "errors": {}, "outside": [], "symbols": symbols,
            "excluded": []}
    if len(symbols) < 2:
        return None, info
    today = dt.date.today()
    # Read prices from the cache the startup/daily warm fills; only a ticker that was just added to the menu
    # (nothing cached yet) is downloaded here, so opening the tab normally fetches nothing.
    prices, errors = services.write(bm.fund_prices_cached, symbols,
                                    get_settings().demo, today - dt.timedelta(days=365 * HISTORY_YEARS))
    info["errors"] = errors
    have = [sym for sym in symbols if sym in prices.columns and prices[sym].notna().any()]
    if len(have) < 2:
        return None, info
    # Drop funds with too little history so one barely-launched fund does not block the whole analysis;
    # keep everything if dropping would leave fewer than two funds (better a short window than no result).
    kept, excluded = op.select_funds(prices[have])
    if kept.shape[1] >= 2:
        info["excluded"] = excluded
    else:
        kept = prices[have]
    priced = list(kept.columns)
    mix, outside = services.read(fm.current_mix, account, priced)
    info["outside"] = outside
    if len(priced) < 2:
        return None, info
    return op.analyze(kept, lookback, rf, cap, train, mix), info


def _get(account, lookback, cap, rf, train, refresh, version):
    if not account:
        return None, {"names": {}, "errors": {}, "outside": [], "symbols": []}
    cap = float(cap or 40) / 100
    rf = float(rf or 0) / 100
    return _analysis(get_settings().db_url, account, int(lookback or 5), cap, rf, int(train or 3), refresh, version,
                     dt.date.today())


def _portfolios(a: op.Analysis, target: float | None) -> dict[str, op.Portfolio]:
    out = {}
    if a.current is not None:
        out[CURRENT] = a.current
    out[MAX_SHARPE] = a.max_sharpe
    out[MIN_VAR] = a.min_variance
    t = a.max_sharpe.ret if target is None else target
    out[SELECTED] = op.frontier_point(a.mu, a.cov, t, a.rf, a.cap)
    return out


# --------------------------------------------------------------------------- figures
def frontier_figure(a: op.Analysis, ports: dict[str, op.Portfolio], theme) -> go.Figure:
    c = colors(theme)
    vols = np.sqrt(np.diag(a.cov))
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=vols, y=a.mu, mode="markers+text", name="Funds", text=a.symbols,
                             textposition="top center", textfont={"color": c["muted"], "size": 11},
                             marker={"color": c["neutral"], "size": 9},
                             hovertemplate="<b>%{text}</b><br>Return %{y:.1%}<br>Volatility %{x:.1%}<extra></extra>"))
    fig.add_trace(go.Scatter(x=[p.vol for p in a.frontier], y=[p.ret for p in a.frontier], mode="lines",
                             name="Efficient frontier", line={"color": c["text"], "width": 2},
                             hovertemplate="Return %{y:.1%}<br>Volatility %{x:.1%}<extra>Frontier</extra>"))
    symbols = {CURRENT: "circle", MAX_SHARPE: "star", MIN_VAR: "diamond", SELECTED: "square"}
    for name, p in ports.items():
        fig.add_trace(go.Scatter(
            x=[p.vol], y=[p.ret], mode="markers", name=name,
            marker={"color": c["series"][SLOT[name]], "size": 16 if name == MAX_SHARPE else 13,
                    "symbol": symbols[name], "line": {"color": c["surface"], "width": 2}},
            customdata=[p.sharpe if p.sharpe is not None else float("nan")],
            hovertemplate=(f"<b>{name}</b><br>Return %{{y:.1%}}<br>Volatility %{{x:.1%}}"
                           "<br>Sharpe %{customdata:.2f}<extra></extra>")))
    fig.update_layout(**base_layout(theme, height=400, hovermode="closest"))
    fig.update_xaxes(title={"text": "Volatility (annual)"}, tickformat=".0%", showgrid=True, zeroline=False)
    fig.update_yaxes(title={"text": "Expected return (annual)"}, tickformat=".0%", zeroline=False)
    return fig


def weights_figure(a: op.Analysis, ports: dict[str, op.Portfolio], theme) -> go.Figure:
    c = colors(theme)
    names = [n for n in (CURRENT, SELECTED) if n in ports]
    order = np.argsort(-ports[SELECTED].weights)
    syms = [a.symbols[i] for i in order]
    fig = go.Figure([go.Bar(y=syms, x=ports[n].weights[order], name=n, orientation="h",
                            marker={"color": c["series"][SLOT[n]], "cornerradius": 3},
                            hovertemplate="<b>%{y}</b><br>%{x:.1%}<extra>" + n + "</extra>") for n in names])
    fig.update_layout(**base_layout(theme, height=330, barmode="group", bargap=0.25, bargroupgap=0.08))
    fig.update_yaxes(autorange="reversed", showgrid=False, zeroline=False, tickfont={"color": c["text"]})
    fig.update_xaxes(tickformat=".0%", showgrid=True, rangemode="tozero")
    return fig


def lines_figure(series: list[tuple[str, pd.Series, str, str]], theme, height=340, vlines=()) -> go.Figure:
    if not series:
        return empty_figure(theme, height=height)
    c = colors(theme)
    fig = go.Figure([go.Scatter(x=s.index, y=s.values, name=n, mode="lines", line={"color": col, "width": 2,
                                                                                    "dash": dash},
                                hovertemplate="%{y:+.1%}<extra>" + n + "</extra>") for n, s, col, dash in series])
    for x in vlines:
        fig.add_vline(x=pd.Timestamp(x), line={"color": c["axis"], "width": 1, "dash": "dot"})
    layout = base_layout(theme, height=height, hovermode="x unified")
    layout["legend"]["traceorder"] = "normal"
    fig.update_layout(**layout)
    fig.update_xaxes(hoverformat="%b %d, %Y")
    fig.update_yaxes(tickformat="+.0%")
    return fig


def _history_label(days: int) -> str:
    if days <= 0:
        return "no price history"
    if days < 252:
        return f"only {max(1, round(days / 21))} month(s) of history"
    return f"only {days / 252:.1f} year(s) of history"


def _messages(a: op.Analysis | None, info: dict) -> list:
    out = []
    if info["errors"]:
        out.append(alert("Some funds have no price history and were left out", "warning",
                         [msg for msg in info["errors"].values()], duration=None))
    if info.get("excluded"):
        out.append(alert("Some funds were left out: too little price history to optimize (need at least 1 year)",
                         "warning",
                         [f"{info['names'].get(sym) or sym} ({sym}): {_history_label(days)}"
                          for sym, days in info["excluded"]], duration=None))
    if info["outside"]:
        out.append(alert("Holdings that are not in the fund menu are left out of the current mix", "secondary",
                         [f"{sym}: {share:.1%} of the account" for sym, share in info["outside"]], duration=None))
    if a is not None and a.limiter:
        out.append(alert(f"The history used starts {a.start:%b %d, %Y}: {a.limiter} has no prices before that",
                         "secondary", duration=None))
    if a is not None and a.current is None:
        out.append(alert("No current mix: the account's latest holdings do not include any fund of the menu",
                         "secondary", duration=None))
    return out


# --------------------------------------------------------------------------- callbacks
SETTINGS = [Input("opt-account", "value"), Input("opt-lookback", "value"), Input("opt-cap", "value"),
            Input("opt-rf", "value"), Input("opt-train", "value"), Input("opt-refresh", "data"),
            Input("sync-version", "data")]


@callback(Output("opt-menu", "rowData"), Input("opt-account", "value"), Input("opt-refresh", "data"))
def menu_rows(account, _refresh):
    if not account:
        return []
    items = services.read(fm.menu, account)
    mix, _ = services.read(fm.current_mix, account, [i["symbol"] for i in items])
    return [{**i, "id": str(i["id"]), "current": mix.get(i["symbol"])} for i in items]  # AG Grid ids are strings


@callback(Output("opt-menu-msg", "children"), Output("opt-refresh", "data"), Output("opt-paste", "value"),
          Input("opt-upload", "contents"), Input("opt-add", "n_clicks"), Input("opt-add-holdings", "n_clicks"),
          Input("opt-remove", "n_clicks"), Input("opt-menu", "cellValueChanged"),
          State("opt-upload", "filename"), State("opt-paste", "value"), State("opt-account", "value"),
          State("opt-menu", "selectedRows"), State("opt-refresh", "data"), prevent_initial_call=True)
def edit_menu(contents, _add, _holdings, _remove, changed, filename, pasted, account, selected, refresh):
    trigger = ctx.triggered_id
    if not account:
        return alert("Choose an account first.", "warning"), no_update, no_update
    try:
        if trigger in ("opt-upload", "opt-add"):
            if trigger == "opt-upload":
                if not contents:
                    return no_update, no_update, no_update
                try:
                    text = base64.b64decode(contents.split(",", 1)[1]).decode("utf-8-sig", errors="replace")
                except (IndexError, ValueError):
                    return alert("Could not read the file.", "danger"), no_update, no_update
                source = filename or "the file"
            else:
                text, source = pasted or "", "the pasted text"
            parsed = op.parse_fund_list(text)
            if not parsed.funds:
                return alert(f"No tickers found in {source}.", "warning", parsed.skipped[:10]), no_update, no_update
            added, existing = services.write(fm.add_funds, account, parsed.funds)
            details = ([f"Skipped (no ticker): {', '.join(parsed.skipped[:10])}"] if parsed.skipped else [])
            msg = alert(f"Added {added} fund(s) from {source}" + (f"; {existing} already in the menu" if existing
                                                                 else "") + ".", "success", details)
            return msg, (refresh or 0) + 1, "" if trigger == "opt-add" else no_update
        if trigger == "opt-add-holdings":
            added, existing = services.write(fm.add_current_holdings, account)
            return alert(f"Added {added} holding(s) to the menu.", "success"), (refresh or 0) + 1, no_update
        if trigger == "opt-remove":
            ids = [int(r["id"]) for r in selected or []]
            if not ids:
                return alert("Select funds with the checkboxes first.", "secondary"), no_update, no_update
            n = services.write(fm.remove, ids)
            return alert(f"Removed {n} fund(s).", "success"), (refresh or 0) + 1, no_update
        if trigger == "opt-menu" and changed:
            change = changed[0]
            services.write(fm.rename, int(change["data"]["id"]), change.get("value"))
            return alert("Name saved.", "success", duration=3000), no_update, no_update
    except fm.FundMenuError as exc:
        return alert(str(exc), "danger"), no_update, no_update
    return no_update, no_update, no_update


@callback(Output("opt-msg", "children"), Output("opt-target", "min"), Output("opt-target", "max"),
          Output("opt-target", "value"), Output("opt-target", "marks"), Output("opt-walk", "figure"),
          Output("opt-walk-stats", "rowData"), Output("opt-walk-subtitle", "children"),
          *SETTINGS, Input("theme", "data"))
def run_analysis(account, lookback, cap, rf, train, refresh, version, theme):
    a, info = _get(account, lookback, cap, rf, train, refresh, version)
    c = colors(theme)
    if a is None:
        usable = [s for s in info["symbols"] if s not in info["errors"]]
        if len(info["symbols"]) < 2:
            need = "Add at least two funds with a ticker to the fund menu."
        elif len(usable) < 2:
            need = ("The fund prices have not been downloaded yet (see above) - this can happen right after "
                    "startup or when the provider rate-limits us. They refresh automatically; try again shortly.")
        else:
            need = "The funds do not share enough overlapping price history to run the optimization."
        msg = _messages(None, info) + [alert(need, "secondary", duration=None)]
        return msg, 0, 1, None, None, empty_figure(theme, height=340), [], None
    lo, hi = a.min_variance.ret, op.max_return(a.mu, a.cap)
    marks = {float(lo): {"label": "Min variance"}, float(hi): {"label": "Max return"}}
    if a.max_sharpe.ret - lo > (hi - lo) * 0.15 and hi - a.max_sharpe.ret > (hi - lo) * 0.15:
        marks[float(a.max_sharpe.ret)] = {"label": "Max Sharpe"}
    walk, rows, subtitle = empty_figure(theme, "Not enough history for a walk-forward test", height=340), [], None
    if a.walk is not None:
        colors_by = {MAX_SHARPE: c["series"][1], MIN_VAR: c["series"][2], CURRENT: c["series"][0],
                     "Equal weight": c["muted"]}
        series = [(n, op.cumulative(r), colors_by.get(n, c["muted"]), "dash" if n == "Equal weight" else "solid")
                  for n, r in a.walk.returns.items()]
        walk = lines_figure(series, theme, vlines=[p["start"] for p in a.walk.periods[1:]])
        perf = {n: op.performance(r, a.rf) for n, r in a.walk.returns.items()}
        base = perf.get(CURRENT, {}).get("cagr")
        rows = [{"strategy": n + (" (re-optimized yearly)" if n in (MAX_SHARPE, MIN_VAR) else ""),
                 "cagr": p["cagr"], "vol": p["vol"], "sharpe": p["sharpe"], "max_dd": p["max_drawdown"],
                 "vs_current": (p["cagr"] - base) if base is not None and p["cagr"] is not None and n != CURRENT
                 else None} for n, p in perf.items()]
        first, last = a.walk.periods[0]["start"], a.walk.periods[-1]["end"]
        subtitle = (f"Every 12 months from {first:%b %Y} to {last:%b %Y}, the optimal mixes are re-computed from "
                    f"the previous {int(train or 3)} year(s) only and held for the next 12 months (dotted lines), "
                    "so the test never uses future prices. The current mix is held unchanged.")
    return (_messages(a, info), float(lo), float(hi), float(a.max_sharpe.ret), marks, walk, rows, subtitle)


@callback(Output("opt-frontier", "figure"), Output("opt-target-label", "children"), Output("opt-stats", "rowData"),
          Output("opt-stats-subtitle", "children"), Output("opt-weights", "rowData"),
          Output("opt-weights-chart", "figure"), Output("opt-backtest", "figure"),
          Output("opt-backtest-subtitle", "children"),
          Input("opt-target", "value"), *SETTINGS, Input("theme", "data"))
def update_selected(target, account, lookback, cap, rf, train, refresh, version, theme):
    a, info = _get(account, lookback, cap, rf, train, refresh, version)
    if a is None:
        empty = empty_figure(theme, "Add at least two funds with a ticker", height=400)
        return empty, None, [], None, [], empty_figure(theme, height=330), empty_figure(theme, height=340), None
    c = colors(theme)
    ports = _portfolios(a, target)
    sel = ports[SELECTED]
    label = (f"Selected: expected return {sel.ret:.1%}, volatility {sel.vol:.1%}"
             + (f", Sharpe {sel.sharpe:.2f}" if sel.sharpe is not None else ""))
    daily = {n: op.mix_returns(a.in_sample_returns, p.weights) for n, p in ports.items()}
    daily["Equal weight"] = op.mix_returns(a.in_sample_returns, np.full(len(a.symbols), 1 / len(a.symbols)))
    stats = []
    for n, r in daily.items():
        p = ports.get(n) or op.evaluate(np.full(len(a.symbols), 1 / len(a.symbols)), a.mu, a.cov, a.rf)
        perf = op.performance(r, a.rf)
        stats.append({"portfolio": n, "ret": p.ret, "vol": p.vol, "sharpe": p.sharpe, "cagr": perf["cagr"],
                      "max_dd": perf["max_drawdown"]})
    years = (a.end - a.start).days / 365.25
    stats_sub = (f"In sample: {a.start:%b %d, %Y} to {a.end:%b %d, %Y} ({years:.1f} years). Sharpe = (expected "
                 f"return - {a.rf:.2%}) / volatility.")
    names = info["names"]
    weights = sorted([{"symbol": s, "name": names.get(s, ""),
                       "current": float(ports[CURRENT].weights[i]) if CURRENT in ports else None,
                       "max_sharpe": float(a.max_sharpe.weights[i]), "min_var": float(a.min_variance.weights[i]),
                       "selected": float(sel.weights[i])} for i, s in enumerate(a.symbols)],
                     key=lambda r: -r["max_sharpe"])
    series = [(n, op.cumulative(r), c["series"][SLOT[n]] if n in SLOT else c["muted"],
               "dash" if n == "Equal weight" else "solid") for n, r in daily.items()]
    backtest_sub = ("Each mix held at constant weights (rebalanced daily) over the same history the optimizer saw, "
                    "so the optimal mixes look better here than they would have in real time; see the walk-forward "
                    "test below.")
    return (frontier_figure(a, ports, theme), label, stats, stats_sub, weights, weights_figure(a, ports, theme),
            lines_figure(series, theme), backtest_sub)


@callback(Output("opt-ticker-sym", "data"), Input("opt-weights", "cellClicked"), prevent_initial_call=True)
def pick_ticker(click):
    # the Weights grid uses the ticker as its row id, so a clicked cell carries it directly
    if not click or not click.get("rowId"):
        return no_update
    return click["rowId"]


@callback(Output("opt-ticker", "figure"), Output("opt-ticker-subtitle", "children"),
          Input("opt-ticker-sym", "data"), Input("opt-bench", "value"), *SETTINGS, Input("theme", "data"))
def ticker_performance(symbol, bench, account, lookback, cap, rf, train, refresh, version, theme):
    """Cumulative total return over the history used: the account's portfolio, a clicked ticker and a market
    benchmark, all on the same dividend-adjusted basis so they are comparable."""
    a, _info = _get(account, lookback, cap, rf, train, refresh, version)
    c = colors(theme)
    if a is None:
        return empty_figure(theme, "Add at least two priced funds to compare performance", height=320), None
    idx = a.in_sample_returns.index

    series = []
    # The portfolio line: the account's current mix, or the max-Sharpe mix when there is no current one.
    if a.current is not None:
        port_name, port_w = "Current mix", a.current.weights
    else:
        port_name, port_w = "Max Sharpe mix", a.max_sharpe.weights
    port = op.cumulative(op.mix_returns(a.in_sample_returns, port_w))
    series.append((port_name, port, c["series"][SLOT[CURRENT]], "solid"))

    # The clicked ticker (if any and priced), on the same window.
    tkr = op.cumulative(a.in_sample_returns[symbol]) if symbol and symbol in a.symbols else None
    if tkr is not None:
        series.append((symbol, tkr, c["series"][SLOT[SELECTED]], "solid"))

    # The market benchmark (dividend-adjusted closes, like the funds), aligned to the same window.
    bench = bench or bm.DEFAULT
    blabel = bm.BENCHMARKS.get(bench, bench)
    bprices, _berr = services.write(bm.fund_prices_cached, [bench], get_settings().demo, a.start.date())
    bench_ri = bm.return_index(bprices[bench].dropna(), idx) if bench in bprices.columns else None
    if bench_ri is not None:
        series.append((blabel, bench_ri, c["muted"], "dash"))

    fig = lines_figure(series, theme, height=320)

    def _tot(s):
        return f"{float(s.iloc[-1]):+.1%}" if s is not None and len(s) else "n/a"

    parts = [f"{port_name} {_tot(port)}"]
    if tkr is not None:
        parts.append(f"{symbol} {_tot(tkr)}")
    if bench_ri is not None:
        parts.append(f"{blabel} {_tot(bench_ri)}")
    sub = (f"Cumulative total return (dividends included), {a.start:%b %Y} to {a.end:%b %Y}. "
           + "  ".join(parts)
           + ("" if symbol in a.symbols else "  Click a ticker in the Weights table to add it."))
    return fig, sub
