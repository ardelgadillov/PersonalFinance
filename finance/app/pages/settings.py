"""Settings page: accounts, Empower import & links, categories, sync log."""
from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from finance import services
from finance.app.components import alert, editable, grid, message, money_col, panel
from finance.background import sync_manager
from finance.categories import CategoryError
from finance.models import ACCOUNT_TYPES, CATEGORY_GROUPS

TYPE_LABELS = {
    "checking": "Checking", "savings": "Savings", "credit_card": "Credit card", "brokerage": "Brokerage",
    "retirement": "Retirement", "loan": "Loan", "mortgage": "Mortgage", "crypto": "Crypto", "other": "Other",
}
UNCATEGORIZED = "(leave uncategorized)"


def layout() -> html.Div:
    return html.Div([
        dbc.Tabs(id="settings-tabs", active_tab="accounts", className="mb-4", children=[
            dbc.Tab(label="Accounts", tab_id="accounts"),
            dbc.Tab(label="Categories", tab_id="categories"),
            dbc.Tab(label="Empower import", tab_id="empower"),
            dbc.Tab(label="Sync log", tab_id="synclog"),
        ]),
        html.Div(id="settings-content"),
    ])


@callback(Output("settings-content", "children"),
          Input("settings-tabs", "active_tab"), Input("sync-version", "data"))
def render_tab(tab: str, _version):
    return {"accounts": accounts_tab, "empower": empower_tab,
            "categories": categories_tab, "synclog": synclog_tab}.get(tab, accounts_tab)()


# --------------------------------------------------------------------------- accounts
IS_MANUAL = "params.data.source === 'Manual'"


def _add_account_form() -> dbc.Row:
    """Create a balance-only account you maintain by hand (e.g. a property, a car loan, a cash stash)."""
    return dbc.Row([
        dbc.Col(dbc.Input(id="acct-add-name", placeholder="Account name", size="sm"), md=3),
        dbc.Col(dbc.Select(id="acct-add-type", size="sm", value="other",
                           options=[{"label": TYPE_LABELS[t], "value": t} for t in ACCOUNT_TYPES]), md=2),
        dbc.Col(dbc.Input(id="acct-add-balance", placeholder="Balance (e.g. 1500 or -200)", size="sm"), md=3),
        dbc.Col(dbc.Checkbox(id="acct-add-networth", label="In net worth", value=True, className="small"),
                md="auto", className="d-flex align-items-center"),
        dbc.Col(dbc.Button([html.I(className="bi bi-plus-lg me-1"), "Add account"], id="acct-add-btn",
                           color="primary", size="sm"), md="auto"),
    ], className="g-2 mb-3 align-items-center")


def accounts_tab() -> html.Div:
    rows = services.account_rows()
    for r in rows:
        r["type"] = TYPE_LABELS.get(r["type"], "")
    untyped = sum(1 for r in rows if not r["type"] and not r["hidden"])
    cols = [
        {"field": "institution", "headerName": "Institution", "minWidth": 140},
        {"field": "name", "headerName": "Account (from bank)", "minWidth": 170},
        editable({"field": "display_name", "headerName": "Display name", "minWidth": 150}),
        editable({"field": "type", "headerName": "Type", "cellEditor": "agSelectCellEditor",
                  "cellEditorParams": {"values": [""] + [TYPE_LABELS[t] for t in ACCOUNT_TYPES]},
                  "cellClassRules": {"cell-missing": "!params.value"}, "minWidth": 120}),
        editable({"field": "include_in_networth", "headerName": "In net worth", "cellDataType": "boolean",
                  "maxWidth": 130}),
        editable({"field": "hidden", "headerName": "Hidden", "cellDataType": "boolean", "maxWidth": 110}),
        # Balance is editable only for manual accounts (synced balances come from the bank).
        money_col("balance", "Balance", minWidth=120, editable={"function": IS_MANUAL},
                  cellClassRules={"amount-neg": "params.value < 0", "cell-editable": IS_MANUAL},
                  headerTooltip="Manual accounts: click to set today's balance"),
        {"field": "balance_date", "headerName": "Balance as of", "minWidth": 140},
        {"field": "transactions", "headerName": "Transactions", "type": "rightAligned", "maxWidth": 130},
        {"field": "since", "headerName": "Since", "maxWidth": 120},
        {"field": "source", "headerName": "Source", "minWidth": 130},
    ]
    body = grid("accounts-grid", cols, rows, height=430,
                dashGridOptions={"rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                  "enableClickSelection": True}}) if rows else dbc.Alert(
        "No accounts yet. Click Sync to download your accounts from SimpleFIN, or add a manual account below.",
        color="info", className="mb-0")
    delete_btn = dbc.Button([html.I(className="bi bi-trash me-1"), "Delete selected manual account"],
                            id="acct-delete-btn", color="danger", outline=True, size="sm")
    return html.Div([
        dbc.Alert(f"{untyped} account(s) have no type yet (highlighted). The type decides whether an account "
                  "counts as an asset or a liability and whether it appears in Investments.",
                  color="warning", className="py-2") if untyped else None,
        message("accounts-msg"),
        panel("Add a manual account", _add_account_form(),
              subtitle="A balance-only account you keep up to date yourself (property, vehicle, loan, cash). "
                       "It has no transactions; edit its Balance cell to record a new value for today."),
        panel("Accounts", body,
              subtitle="Columns with an indigo header are editable. Changes save immediately. Select a manual "
                       "account and use Delete to remove it.",
              actions=delete_btn),
        _delete_account_modal(),
    ])


def _delete_account_modal() -> dbc.Modal:
    return dbc.Modal(id="acct-delete-modal", is_open=False, children=[
        dbc.ModalHeader(dbc.ModalTitle("Delete account")),
        dbc.ModalBody([html.Div(id="acct-delete-body"), dcc.Store(id="acct-delete-id")]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id="acct-delete-cancel", color="secondary", outline=True, size="sm"),
            dbc.Button("Delete", id="acct-delete-confirm", color="danger", size="sm"),
        ]),
    ])


@callback(Output("accounts-msg", "children"), Output("accounts-grid", "rowData", allow_duplicate=True),
          Input("accounts-grid", "cellValueChanged"), prevent_initial_call=True)
def save_account_edit(changes):
    if not changes:
        return no_update, no_update
    reverse = {v: k for k, v in TYPE_LABELS.items()}
    saved, refresh = [], False
    try:
        for ch in changes:
            field, value = ch["colId"], ch["value"]
            if field == "balance":
                saved.append(services.update_manual_balance(ch["data"]["id"], value))
                refresh = True
                continue
            if field == "type":
                value = reverse.get(value, value) or None
            services.update_account(ch["data"]["id"], field, value)
            saved.append(f"Saved {ch['data'].get('display_name') or ch['data']['name']}")
    except ValueError as exc:
        return alert(f"Not saved: {exc}", "danger"), _accounts_rowdata()
    return alert("; ".join(saved), duration=3000), (_accounts_rowdata() if refresh else no_update)


@callback(Output("acct-add-name", "value"), Output("accounts-msg", "children", allow_duplicate=True),
          Output("accounts-grid", "rowData", allow_duplicate=True),
          Input("acct-add-btn", "n_clicks"),
          State("acct-add-name", "value"), State("acct-add-type", "value"), State("acct-add-balance", "value"),
          State("acct-add-networth", "value"), prevent_initial_call=True)
def add_manual_account(_clicks, name, type_, balance, in_networth):
    try:
        msg = services.create_manual_account(name, type_, balance, bool(in_networth))
    except ValueError as exc:
        return no_update, alert(f"Not added: {exc}", "danger"), no_update
    return "", alert(msg), _accounts_rowdata()


@callback(Output("acct-delete-modal", "is_open"), Output("acct-delete-body", "children"),
          Output("acct-delete-id", "data"), Output("accounts-msg", "children", allow_duplicate=True),
          Input("acct-delete-btn", "n_clicks"), Input("acct-delete-cancel", "n_clicks"),
          State("accounts-grid", "selectedRows"), prevent_initial_call=True)
def open_delete_account(_open, _cancel, selected):
    if ctx.triggered_id == "acct-delete-cancel":
        return False, no_update, no_update, no_update
    row = (selected or [None])[0]
    if row is None:
        return False, no_update, no_update, alert("Select an account row first.", "info", duration=4000)
    if row.get("source") != "Manual":
        return False, no_update, no_update, alert(
            "Only manual accounts can be deleted. Hide a synced account with the Hidden toggle instead.",
            "warning", duration=5000)
    body = html.P(["Delete ", html.Strong(row.get("display_name") or row["name"]),
                   " and its balance history? This cannot be undone."])
    return True, body, row["id"], no_update


@callback(Output("acct-delete-modal", "is_open", allow_duplicate=True),
          Output("accounts-msg", "children", allow_duplicate=True),
          Output("accounts-grid", "rowData", allow_duplicate=True),
          Input("acct-delete-confirm", "n_clicks"), State("acct-delete-id", "data"), prevent_initial_call=True)
def confirm_delete_account(_clicks, account_id):
    try:
        msg = services.delete_manual_account(account_id)
    except ValueError as exc:
        return False, alert(f"Not deleted: {exc}", "danger"), no_update
    return False, alert(msg), _accounts_rowdata()


def _accounts_rowdata() -> list[dict]:
    rows = services.account_rows()
    for r in rows:
        r["type"] = TYPE_LABELS.get(r["type"], "")
    return rows


# --------------------------------------------------------------------------- categories
def _category_cols() -> list[dict]:
    return [
        editable({"field": "category", "headerName": "Category", "minWidth": 170}),
        editable({"field": "group", "headerName": "Group", "cellEditor": "agSelectCellEditor",
                  "cellEditorParams": {"values": CATEGORY_GROUPS}, "maxWidth": 130}),
        editable({"field": "parent", "headerName": "Parent", "maxWidth": 160}),
        editable({"field": "sort_order", "headerName": "Order", "cellDataType": "number", "maxWidth": 100,
                  "type": "rightAligned"}),
        {"field": "transactions", "headerName": "Transactions", "type": "rightAligned", "maxWidth": 130},
        {"field": "empower_categories", "headerName": "Empower categories", "minWidth": 220,
         "tooltipField": "empower_categories"},
    ]


def _mapping_cols(category_names: list[str]) -> list[dict]:
    return [
        {"field": "empower_category", "headerName": "Empower category", "minWidth": 200},
        {"field": "rows", "headerName": "Rows in your data", "type": "rightAligned", "maxWidth": 160},
        editable({"field": "category", "headerName": "App category", "minWidth": 200,
                  "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": [""] + category_names},
                  "cellClassRules": {"cell-missing": "!params.value && params.data.rows > 0"}}),
    ]


def _category_names(rows: list[dict]) -> list[str]:
    return [r["category"] for r in rows]


def categories_tab() -> html.Div:
    cats = services.category_rows()
    mapping = services.empower_category_rows()
    uncategorized = services.uncategorized_count()
    add_form = dbc.Row([
        dbc.Col(dbc.Input(id="cat-add-name", placeholder="New category name", size="sm"), md=4),
        dbc.Col(dbc.Select(id="cat-add-group", value="expense", size="sm",
                           options=[{"label": g.title(), "value": g} for g in CATEGORY_GROUPS]), md=2),
        dbc.Col(dbc.Input(id="cat-add-parent", placeholder="Parent (optional)", size="sm"), md=3),
        dbc.Col(dbc.Button([html.I(className="bi bi-plus-lg me-1"), "Add category"], id="cat-add-btn",
                           color="primary", size="sm"), md="auto"),
    ], className="g-2 mb-3")
    delete_btn = dbc.Button([html.I(className="bi bi-trash me-1"), "Delete selected"], id="cat-delete-btn",
                            color="danger", outline=True, size="sm")
    return html.Div([
        message("cats-msg"),
        panel("Categories",
              add_form,
              grid("cats-grid", _category_cols(), cats, height=470,
                   dashGridOptions={"rowSelection": {"mode": "singleRow", "checkboxes": True,
                                                     "enableClickSelection": False}}),
              html.Div(f"{uncategorized:,} transaction(s) are uncategorized.", className="text-muted small mt-2"),
              subtitle="Groups: income, expense, transfer (transfers are excluded from cash flow). "
                       "Parent is an optional label to group categories in charts. Order sets the display order.",
              actions=delete_btn),
        panel("Empower category mapping",
              grid("map-grid", _mapping_cols(_category_names(cats)), mapping, height=380,
                   row_id="params.data.empower_category"),
              subtitle="Which app category each Empower category becomes. Changing a mapping recategorizes "
                       "the imported transactions (your manual edits are kept)."),
        dbc.Modal(id="cat-delete-modal", is_open=False, children=[
            dbc.ModalHeader(dbc.ModalTitle("Delete category")),
            dbc.ModalBody([
                html.Div(id="cat-delete-body"),
                dbc.Label("Move its manual edits, rules and Empower mappings to", className="small fw-semibold"),
                dcc.Dropdown(id="cat-delete-move", options=[UNCATEGORIZED], value=UNCATEGORIZED,
                             clearable=False),
                html.Div("Automatically assigned transactions are recategorized from rules and Empower "
                         "mappings.", className="text-muted small mt-2"),
                dcc.Store(id="cat-delete-id"),
            ]),
            dbc.ModalFooter([
                dbc.Button("Cancel", id="cat-delete-cancel", color="secondary", outline=True, size="sm"),
                dbc.Button("Delete", id="cat-delete-confirm", color="danger", size="sm"),
            ]),
        ]),
    ])


@callback(Output("cat-delete-modal", "is_open"), Output("cat-delete-body", "children"),
          Output("cat-delete-move", "options"), Output("cat-delete-move", "value"), Output("cat-delete-id", "data"),
          Output("cats-msg", "children", allow_duplicate=True),
          Input("cat-delete-btn", "n_clicks"), Input("cat-delete-cancel", "n_clicks"),
          State("cats-grid", "selectedRows"), prevent_initial_call=True)
def open_delete(_open, _cancel, selected):
    if ctx.triggered_id == "cat-delete-cancel":
        return False, no_update, no_update, no_update, no_update, no_update
    if not selected:
        return (False, no_update, no_update, no_update, no_update,
                alert("Select a category first (checkbox at the start of the row).", "info", duration=4000))
    row = selected[0]
    usage = services.category_usage(row["id"])
    others = [r["category"] for r in services.category_rows() if r["id"] != row["id"]]
    details = [f"{usage.transactions:,} transaction(s) use it ({usage.manual:,} assigned manually)."]
    if usage.empower_categories:
        details.append("Empower categories mapped to it: " + ", ".join(usage.empower_categories) + ".")
    if usage.user_rules:
        details.append(f"{usage.user_rules} of your rules use it.")
    body = html.Div([
        html.P(["Delete ", html.Strong(row["category"]), "?"]),
        html.Ul([html.Li(d) for d in details], className="small"),
    ])
    return True, body, [UNCATEGORIZED] + others, UNCATEGORIZED, row["id"], no_update


@callback(
    Output("cats-msg", "children"), Output("cats-grid", "rowData"), Output("map-grid", "rowData"),
    Output("map-grid", "columnDefs"), Output("cat-add-name", "value"),
    Output("cat-delete-modal", "is_open", allow_duplicate=True),
    Input("cat-add-btn", "n_clicks"), Input("cats-grid", "cellValueChanged"),
    Input("cat-delete-confirm", "n_clicks"), Input("map-grid", "cellValueChanged"),
    State("cat-add-name", "value"), State("cat-add-group", "value"), State("cat-add-parent", "value"),
    State("cat-delete-id", "data"), State("cat-delete-move", "value"),
    prevent_initial_call=True,
)
def change_categories(_add, cat_changes, _confirm, map_changes, name, group, parent, delete_id, move_to):
    trigger = ctx.triggered_id
    clear_name, close_modal = no_update, no_update
    try:
        if trigger == "cat-add-btn":
            msg = services.add_category(name, group, parent)
            clear_name = ""
        elif trigger == "cats-grid":
            msgs = []
            for ch in cat_changes or []:
                field = {"category": "name"}.get(ch["colId"], ch["colId"])
                msgs.append(services.edit_category(ch["data"]["id"], field, ch["value"]))
            msg = " ".join(msgs)
        elif trigger == "cat-delete-confirm":
            msg = services.delete_category(delete_id, None if move_to == UNCATEGORIZED else move_to)
            close_modal = False
        elif trigger == "map-grid":
            msg = " ".join(services.map_empower_category(ch["data"]["empower_category"], ch["value"] or None)
                           for ch in map_changes or [])
        else:
            return (no_update,) * 6
        result = alert(msg, duration=5000)
    except CategoryError as exc:
        result = alert(f"Not saved: {exc}", "danger")
    cats = services.category_rows()
    return (result, cats, services.empower_category_rows(), _mapping_cols(_category_names(cats)),
            clear_name, close_modal)


# --------------------------------------------------------------------------- Empower
def _links_grid(rows, options):
    cols = [
        {"field": "empower_name", "headerName": "Empower account", "minWidth": 230},
        editable({"field": "linked_to", "headerName": "Linked to", "minWidth": 240,
                  "cellEditor": "agSelectCellEditor",
                  "cellEditorParams": {"values": [o["label"] for o in options]}}),
        {"field": "suggestion", "headerName": "Suggested match", "minWidth": 220},
        {"field": "rows", "headerName": "Rows", "type": "rightAligned", "maxWidth": 90},
        {"field": "first", "headerName": "First", "maxWidth": 120},
        {"field": "last", "headerName": "Last", "maxWidth": 120},
    ]
    return grid("links-grid", cols, rows, height=320, row_id="params.data.empower_name")


def empower_tab() -> html.Div:
    rows, options = services.empower_link_rows()
    upload = dcc.Loading(dcc.Upload(
        id="empower-upload", multiple=False, accept=".csv", className="upload-zone",
        children=html.Div([html.I(className="bi bi-upload"),
                           "Drag and drop the Empower CSV here, or ", html.A("browse")]),
    ))
    autolink_btn = dbc.Button("Auto-link confident matches", id="autolink-btn", size="sm", color="primary",
                              outline=True)
    return html.Div([
        message("empower-msg"),
        panel("Import Empower transactions", upload,
              subtitle="In Empower: Transactions, date range All, Download CSV "
                       "(Date, Account, Description, Category, Tags, Amount). Re-importing is safe."),
        panel("Account links",
              html.Div(_links_grid(rows, options) if rows else dbc.Alert(
                  "No Empower data imported yet.", color="info", className="mb-0"), id="links-container"),
              subtitle="Linked: Empower supplies history before SimpleFIN coverage starts. "
                       "History only: kept as a separate account (e.g. closed accounts).",
              actions=autolink_btn),
    ])


@callback(Output("empower-msg", "children"), Output("links-container", "children", allow_duplicate=True),
          Input("empower-upload", "contents"), State("empower-upload", "filename"),
          prevent_initial_call=True)
def upload_empower(contents, filename):
    if not contents:
        return no_update, no_update
    res = services.import_empower_upload(contents, filename)
    rows, options = services.empower_link_rows()
    msg = alert(res.message, "success" if res.ok else "danger", res.details, duration=None)
    return msg, _links_grid(rows, options) if rows else no_update


@callback(Output("empower-msg", "children", allow_duplicate=True),
          Output("links-container", "children", allow_duplicate=True),
          Input("links-grid", "cellValueChanged"), prevent_initial_call=True)
def change_link(changes):
    if not changes:
        return no_update, no_update
    try:
        msgs = [services.set_empower_link(ch["data"]["empower_name"], ch["value"]) for ch in changes]
    except ValueError as exc:
        return alert(f"Not saved: {exc}", "danger"), no_update
    rows, options = services.empower_link_rows()
    return alert(" ".join(msgs)), _links_grid(rows, options)


@callback(Output("empower-msg", "children", allow_duplicate=True),
          Output("links-container", "children", allow_duplicate=True),
          Input("autolink-btn", "n_clicks"), prevent_initial_call=True)
def autolink(_):
    msg = services.auto_link_empower()
    rows, options = services.empower_link_rows()
    return alert(msg), _links_grid(rows, options) if rows else no_update


# --------------------------------------------------------------------------- sync log
def synclog_tab() -> html.Div:
    rows = services.sync_log_rows()
    cols = [
        {"field": "started", "headerName": "Started", "minWidth": 150},
        {"field": "mode", "headerName": "Mode", "maxWidth": 120},
        {"field": "status", "headerName": "Status", "maxWidth": 110,
         "cellClassRules": {"amount-neg": "params.value === 'error'"}},
        {"field": "duration_s", "headerName": "Seconds", "type": "rightAligned", "maxWidth": 110},
        {"field": "requests", "headerName": "Requests", "type": "rightAligned", "maxWidth": 110},
        {"field": "new", "headerName": "New", "type": "rightAligned", "maxWidth": 100},
        {"field": "updated", "headerName": "Updated", "type": "rightAligned", "maxWidth": 110},
        {"field": "errors", "headerName": "Messages from SimpleFIN", "minWidth": 280, "tooltipField": "errors"},
    ]
    full_btn = dcc.ConfirmDialogProvider(
        dbc.Button("Full history re-sync", size="sm", color="primary", outline=True),
        id="full-sync-confirm",
        message="Re-download all available history from SimpleFIN? This uses up to 12 of your ~24 daily "
                "requests.")
    return html.Div([
        message("synclog-msg"),
        panel("Sync log",
              grid("synclog-grid", cols, rows, height=420) if rows else dbc.Alert(
                  "No syncs yet.", color="info", className="mb-0"),
              subtitle="A normal sync (Sync button) uses 1 request and downloads everything since the last "
                       "sync. Full history re-sync walks back ~18 months in 45-day steps (up to 12 requests): "
                       "only needed after adding a new bank in SimpleFIN or to fill a gap. "
                       "SimpleFIN Bridge allows about 24 requests per day.",
              actions=full_btn),
    ])


@callback(Output("synclog-msg", "children"), Input("full-sync-confirm", "submit_n_clicks"),
          prevent_initial_call=True)
def full_sync(_):
    if sync_manager.start(full=True):
        return alert("Full sync started.", "info", duration=4000)
    ok, why = sync_manager.can_sync()
    return alert(why if not ok else "A sync is already running.", "warning")
