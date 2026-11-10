"""Dash application shell: left sidebar, top bar with sync, routing, light/dark theme."""
from __future__ import annotations

import logging

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, ctx, dcc, html, no_update

from finance import services
from finance.background import sync_manager
from finance.config import get_settings

log = logging.getLogger(__name__)

# path, label, Bootstrap icon
NAV = [
    ("/", "Net worth", "bi-graph-up"),
    ("/cashflow", "Cash flow", "bi-arrow-left-right"),
    ("/investments", "Investments", "bi-pie-chart"),
    ("/transactions", "Transactions", "bi-list-ul"),
    ("/settings", "Settings", "bi-gear"),
]
TITLES = {path: label for path, label, _ in NAV}

POLL_FAST_MS = 1000
POLL_SLOW_MS = 15000

THEME_JS = """
function(dark) {
    const mode = dark ? 'dark' : 'light';
    document.documentElement.setAttribute('data-bs-theme', mode);
    document.documentElement.setAttribute('data-ag-theme-mode', mode);
    return mode;
}
"""


def sidebar() -> html.Aside:
    settings = get_settings()
    return html.Aside([
        html.Div([html.Div("PF", className="brand-mark"), html.Span("Personal Finance")], className="brand"),
        dbc.Nav([
            dbc.NavLink([html.I(className=f"bi {icon}"), html.Span(label)], href=path, active="exact")
            for path, label, icon in NAV
        ], vertical=True, className="side-nav"),
        html.Div([
            html.Div("Demo data", className="demo-pill") if settings.demo else None,
            dbc.Switch(id="theme-switch", label="Dark mode", value=False, persistence=True,
                       persistence_type="local", className="mb-0"),
        ], className="sidebar-footer"),
    ], className="sidebar")


def topbar() -> html.Div:
    return html.Div([
        html.H1(id="page-title", className="page-title me-auto"),
        html.Span(id="sync-status", className="sync-status"),
        dbc.Button(id="sync-btn", color="primary", size="sm", children=[
            html.I(className="bi bi-arrow-repeat me-1"), "Sync"]),
    ], className="topbar")


def config_banner():
    settings = get_settings()
    if settings.demo or settings.simplefin_access_url:
        return None
    return dbc.Alert(["SimpleFIN is not configured: add ", html.Code("SIMPLEFIN_ACCESS_URL"),
                      " to your .env file (see .env.example), or start the app with ",
                      html.Code("python -m finance --demo app"), " to explore fake data."],
                     color="warning", className="py-2")


def create_app() -> dash.Dash:
    from finance.app.pages import cashflow as cashflow_page, networth as networth_page, placeholder  # noqa: F401
    from finance.app.pages import settings as settings_page, transactions as transactions_page  # noqa: F401

    services.engine_for()  # create / migrate the database before the first request
    app = dash.Dash(
        __name__,
        title="Personal Finance" + (" (demo)" if get_settings().demo else ""),
        external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
        suppress_callback_exceptions=True,
        update_title=None,
    )
    app.layout = html.Div([
        dcc.Location(id="url"),
        dcc.Store(id="sync-version", data=sync_manager.state.version),
        dcc.Store(id="theme", data="light"),
        dcc.Interval(id="sync-poll", interval=POLL_SLOW_MS),
        sidebar(),
        html.Div([
            topbar(),
            html.Div([
                config_banner(),
                html.Div(id="sync-toast-area"),
                html.Div(id="page"),
            ], className="content"),
        ], className="main"),
    ])

    app.clientside_callback(THEME_JS, Output("theme", "data"), Input("theme-switch", "value"))

    @app.callback(Output("page", "children"), Output("page-title", "children"), Input("url", "pathname"))
    def route(path):
        title = TITLES.get(path, "Not found")
        if path == "/":
            return networth_page.layout(), title
        if path == "/settings":
            return settings_page.layout(), title
        if path == "/cashflow":
            return cashflow_page.layout(), title
        if path == "/transactions":
            return transactions_page.layout(), title
        if path in placeholder.PAGES:
            return placeholder.layout(*placeholder.PAGES[path]), title
        return dbc.Alert(["Page not found. ", dcc.Link("Go to Net worth", href="/")], color="secondary"), title

    @app.callback(
        Output("sync-status", "children"), Output("sync-btn", "children"), Output("sync-btn", "disabled"),
        Output("sync-poll", "interval"), Output("sync-version", "data"), Output("sync-toast-area", "children"),
        Input("sync-btn", "n_clicks"), Input("sync-poll", "n_intervals"), State("sync-version", "data"),
    )
    def sync_status(_clicks, _n, seen_version):
        toast = no_update
        if ctx.triggered_id == "sync-btn":
            if not sync_manager.start(trigger="manual"):
                ok, why = sync_manager.can_sync()
                toast = dbc.Alert(why or "A sync is already running.", color="warning", duration=5000,
                                  dismissable=True, className="py-2")
        st = sync_manager.state
        if st.running:
            label = "Startup sync in progress" if st.trigger == "startup" else "Syncing with SimpleFIN"
            return (label, [dbc.Spinner(size="sm", spinner_class_name="me-2"), "Syncing"], True,
                    POLL_FAST_MS, no_update, toast)

        last = services.last_sync(ok_only=True)
        status = f"Last sync {services.humanize_age(last.finished_at)}" if last else "Never synced"
        new_version = no_update
        if st.version != seen_version:
            new_version = st.version
            if st.result is not None:
                r = st.result
                color = {"ok": "success", "partial": "warning"}.get(r.status, "danger")
                text = (f"Sync {r.status}: {r.transactions_new} new and {r.transactions_updated} updated "
                        f"transaction(s) across {r.accounts} account(s).")
                toast = dbc.Alert([html.Div(text)] + [html.Div(e, className="small") for e in r.errors],
                                  color=color, dismissable=True, duration=None if r.errors else 6000,
                                  className="py-2")
        return (status, [html.I(className="bi bi-arrow-repeat me-1"), "Sync"], False, POLL_SLOW_MS,
                new_version, toast)

    return app


def run(host: str = "127.0.0.1", port: int = 8050, debug: bool = False, startup_sync: bool = True) -> None:
    app = create_app()
    if startup_sync:
        if sync_manager.start_if_stale():
            log.warning("Last sync is old: started a background sync")
    settings = get_settings()
    print(f"Personal Finance running at http://{host}:{port}  (data: {settings.data_dir}"
          f"{', DEMO' if settings.demo else ''})")
    app.run(host=host, port=port, debug=debug, use_reloader=False)
