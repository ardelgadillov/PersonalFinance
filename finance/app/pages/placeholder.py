"""Temporary pages for views built in later milestones."""
from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dcc, html

from finance import services


def _stat(label: str, value) -> dbc.Col:
    return dbc.Col(html.Div([html.Div(label, className="stat-label"),
                             html.Div(value, className="stat-value")], className="panel"), md=3)


def layout(milestone: int, what: str) -> html.Div:
    ov = services.overview()
    untyped = services.untyped_account_count()
    span = (f"{ov['first_date']:%b %Y} – {ov['last_date']:%b %Y}" if ov["first_date"] else "–")
    return html.Div([
        dbc.Alert(["Some accounts have no type yet. ", dcc.Link("Set them in Settings", href="/settings")],
                  color="warning", className="py-2") if untyped else None,
        dbc.Row([
            _stat("Accounts", f"{ov['accounts']}"),
            _stat("Transactions", f"{ov['transactions']:,}"),
            _stat("History", span),
            _stat("Uncategorized", f"{ov['uncategorized']:,}"),
        ], className="g-3"),
        html.Div([html.Div("Coming next", className="panel-title"),
                  html.Div(f"Milestone {milestone}: {what}", className="text-muted")], className="panel"),
    ])


PAGES = {
    "/": (6, "assets vs. liabilities over time, stacked by account type, with KPI cards."),
    "/investments": (7, "holdings with gain/loss, allocation, portfolio value over time."),
}
