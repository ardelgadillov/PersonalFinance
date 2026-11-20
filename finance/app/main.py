"""Dash application shell: left sidebar, top bar with sync, routing, light/dark theme."""
from __future__ import annotations

import logging
import os

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, ctx, dcc, html, no_update

from finance import addon, services
from finance.app import theme
from finance.background import sync_manager
from finance.config import get_settings

log = logging.getLogger(__name__)

# path, label, Bootstrap icon
NAV = [
    ("/", "Net worth", "bi-graph-up"),
    ("/cashflow", "Cash flow", "bi-arrow-left-right"),
    ("/investments", "Investments", "bi-pie-chart"),
    ("/optimizer", "Optimizer", "bi-sliders"),
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
            # get_relative_path prefixes the href with Dash's requests_pathname_prefix so links work
            # both locally ("/cashflow") and behind the Home Assistant ingress path.
            dbc.NavLink([html.I(className=f"bi {icon}"), html.Span(label)],
                        href=dash.get_relative_path(path), active="exact")
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
    from finance.app.pages import cashflow as cashflow_page, investments as investments_page  # noqa: F401
    from finance.app.pages import networth as networth_page, optimizer as optimizer_page  # noqa: F401
    from finance.app.pages import settings as settings_page, transactions as transactions_page  # noqa: F401

    services.engine_for()  # create / migrate the database before the first request
    # Behind Home Assistant ingress the browser reaches us under a dynamic path prefix; Dash
    # serves routes at '/' (HA strips the prefix) but must emit asset/callback URLs under it.
    prefix = addon.ingress_prefix() if addon.in_addon() else None
    path_kwargs = {"routes_pathname_prefix": "/", "requests_pathname_prefix": prefix} if prefix else {}
    app = dash.Dash(
        __name__,
        title="Personal Finance" + (" (demo)" if get_settings().demo else ""),
        external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
        suppress_callback_exceptions=True,
        update_title=None,
        **path_kwargs,
    )
    # Theme colour variables are generated from finance/app/theme.py and injected here, so
    # assets/style.css only references var(--app-*). The block loads after {%css%} (the
    # assets) so the variable definitions are in place for the structural rules.
    app.index_string = """<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style id="app-theme">__THEME_CSS__</style>
    </head>
    <body>
        {%app_entry%}
        <footer>{%config%}{%scripts%}{%renderer%}</footer>
    </body>
</html>""".replace("__THEME_CSS__", theme.css())
    app.layout = html.Div([
        dcc.Location(id="url"),
        dcc.Store(id="sync-version", data=sync_manager.state.version),
        dcc.Store(id="theme", data=theme.DEFAULT_THEME),
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

    def _route_key(path: str | None) -> str:
        """Browser pathname -> app route. Behind HA ingress the pathname carries the ingress prefix
        (Dash's requests_pathname_prefix); strip it so routes match '/', '/cashflow', ... as usual."""
        prefix = app.config.requests_pathname_prefix or "/"
        p = path or "/"
        if prefix != "/":
            if p == prefix.rstrip("/"):  # ingress root without the trailing slash
                return "/"
            if p.startswith(prefix):
                p = "/" + p[len(prefix):]
        return p or "/"

    @app.callback(Output("page", "children"), Output("page-title", "children"), Input("url", "pathname"))
    def route(path):
        path = _route_key(path)
        title = TITLES.get(path, "Not found")
        if path == "/":
            return networth_page.layout(), title
        if path == "/settings":
            return settings_page.layout(), title
        if path == "/cashflow":
            return cashflow_page.layout(), title
        if path == "/transactions":
            return transactions_page.layout(), title
        if path == "/investments":
            return investments_page.layout(), title
        if path == "/optimizer":
            return optimizer_page.layout(), title
        return dbc.Alert(["Page not found. ", dcc.Link("Go to Net worth", href=dash.get_relative_path("/"))],
                         color="secondary"), title

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
    from finance.logging_setup import configure_logging
    configure_logging()
    app = create_app()
    if startup_sync:
        if sync_manager.start_if_stale():
            log.warning("Last sync is old: started a background sync")
    settings = get_settings()
    # Daily background sync (used by the HA add-on; FINANCE_DAILY_SYNC_HOUR is set from its options).
    hour = os.getenv("FINANCE_DAILY_SYNC_HOUR", "").strip()
    if hour:
        sync_manager.schedule_daily(int(hour), settings=settings)
    # Fund prices for the optimizer: one batched download now (so the cache is ready by the time the
    # optimizer tab is opened) and again daily at FINANCE_PRICE_REFRESH_HOUR (04:00 by default).
    if not settings.demo:
        sync_manager.warm_prices(settings)
        sync_manager.schedule_daily_prices(int(os.getenv("FINANCE_PRICE_REFRESH_HOUR", "4")), settings=settings)
    banner = (f"Personal Finance running at http://{host}:{port}  (data: {settings.data_dir}"
              f"{', DEMO' if settings.demo else ''})")
    print(banner)
    log.info(banner)
    if debug:
        # Flask/werkzeug dev server: live tracebacks for development (no reloader - it would
        # start two background syncs). Prints the "development server" warning; that is expected.
        app.run(host=host, port=port, debug=True, use_reloader=False)
    else:
        # Production serving (incl. the HA add-on): waitress is a small, multi-threaded, pure-Python
        # WSGI server - no compiled deps, so it runs the same on Windows and all HA architectures.
        from waitress import serve
        # A page can fire several callbacks at once (the optimizer fires three); more worker threads than
        # waitress's default of 4 keep one slow page from starving requests for the others.
        serve(app.server, host=host, port=port, threads=int(os.getenv("FINANCE_SERVER_THREADS", "8")))
