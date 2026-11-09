"""Shared UI helpers."""
from __future__ import annotations

import dash_ag_grid as dag
import dash_bootstrap_components as dbc
from dash import html

MONEY = {"function": "params.value == null ? '' : d3.format('$,.2f')(params.value)"}
NEG_CLASS = {"amount-neg": "params.value < 0"}


def money_col(field: str, header: str, **kw) -> dict:
    return {"field": field, "headerName": header, "valueFormatter": MONEY, "cellClassRules": NEG_CLASS,
            "type": "rightAligned", "filter": "agNumberColumnFilter", **kw}


def editable(col: dict) -> dict:
    """Mark a column as editable (accent header + hover underline)."""
    return {**col, "editable": True, "headerClass": "editable-col", "cellClass": "cell-editable",
            "headerTooltip": "Click a cell to edit"}


def grid(id_: str, column_defs: list[dict], row_data: list[dict], height: int = 450,
         row_id: str | None = "params.data.id", **kw) -> dag.AgGrid:
    options = {"animateRows": False, "stopEditingWhenCellsLoseFocus": True,
               "singleClickEdit": True, "tooltipShowDelay": 300}
    options.update(kw.pop("dashGridOptions", {}))
    extra = {"getRowId": row_id} if row_id else {}
    return dag.AgGrid(
        id=id_,
        columnDefs=column_defs,
        rowData=row_data,
        defaultColDef={"sortable": True, "resizable": True, "filter": True, "minWidth": 90},
        columnSize="responsiveSizeToFit",
        dashGridOptions=options,
        style={"height": f"{height}px", "width": "100%"},
        **extra,
        **kw,
    )


def panel(title: str | None, *children, subtitle: str | None = None, actions=None) -> html.Div:
    head = None
    if title or actions:
        head = html.Div([
            html.Div([html.Div(title, className="panel-title"),
                      html.Div(subtitle, className="panel-subtitle") if subtitle else None],
                     className="me-auto"),
            html.Div(actions) if actions else None,
        ], className="d-flex align-items-start gap-2")
    return html.Div([head, *children], className="panel")


def message(id_: str) -> html.Div:
    """Placeholder for a status alert filled by callbacks."""
    return html.Div(id=id_)


def alert(text, color: str = "success", details: list[str] | None = None, duration: int | None = 8000):
    body = [html.Div(text, className="fw-semibold")]
    if details:
        body.append(html.Ul([html.Li(d) for d in details], className="mb-0 mt-1 small"))
    return dbc.Alert(body, color=color, dismissable=True, duration=duration, className="py-2")
