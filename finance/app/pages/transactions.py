"""Transactions page: filterable editable list, transfer matching and categorization rules."""
from __future__ import annotations

import datetime as dt
import time

import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from finance import services
from finance import transactions as tx
from finance import transfers as tr
from finance.app.components import alert, editable, grid, message, money_col, panel
from finance.categorize import description_key

KEEP = "(keep current)"
ANY_ACCOUNT = ""
MULTI_SELECT = {"mode": "multiRow", "checkboxes": True, "headerCheckbox": True, "enableClickSelection": False}
PERSIST = {"persistence": True, "persistence_type": "session"}


def _bump() -> float:
    return time.time()


def layout() -> html.Div:
    return html.Div([
        dcc.Store(id="txn-refresh", data=0),
        dcc.Store(id="txn-selection", data=[]),
        message("txn-msg"),
        dbc.Tabs(id="txn-tabs", active_tab="list", className="mb-4", children=[
            dbc.Tab(label="Transactions", tab_id="list"),
            dbc.Tab(label="Transfers", tab_id="transfers"),
            dbc.Tab(label="Rules", tab_id="rules"),
        ]),
        html.Div(id="txn-content"),
        rule_modal(),
    ])


@callback(Output("txn-content", "children"), Output("txn-selection", "data"),
          Input("txn-tabs", "active_tab"), Input("sync-version", "data"))
def render_tab(tab: str, _version):
    page = {"list": list_tab, "transfers": transfers_tab, "rules": rules_tab}.get(tab, list_tab)
    return page(), []


# --------------------------------------------------------------------------- list
def _list_cols(names: list[str]) -> list[dict]:
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
        editable({"field": "tags", "headerName": "Tags", "minWidth": 110}),
        editable({"field": "note", "headerName": "Note", "minWidth": 130, "tooltipField": "note"}),
        {"field": "flags", "headerName": "Status", "maxWidth": 130},
        {"field": "source", "headerName": "Source", "maxWidth": 110},
    ]


def list_tab() -> html.Div:
    cats = services.category_choices()
    accounts = services.account_choices()
    names = [c["name"] for c in cats]
    filters = dbc.Row([
        dbc.Col(dbc.Input(id="txn-search", type="search", debounce=True, size="sm",
                          placeholder="Search text, tags, notes or amount", **PERSIST), lg=3),
        dbc.Col(dbc.Select(id="txn-period", value="90d", size="sm",
                           options=[{"label": v, "value": k} for k, v in tx.PERIODS.items()], **PERSIST), lg=2),
        dbc.Col(dcc.DatePickerRange(id="txn-dates", display_format="YYYY-MM-DD", clearable=True,
                                    **PERSIST), id="txn-dates-col", width="auto"),
        dbc.Col(dcc.Dropdown(id="txn-accounts", multi=True, placeholder="All accounts",
                             options=[{"label": a["label"], "value": a["id"]} for a in accounts], **PERSIST), lg=3),
        dbc.Col(dcc.Dropdown(id="txn-categories", multi=True, placeholder="All categories",
                             options=[{"label": "Uncategorized", "value": tx.UNCATEGORIZED}]
                             + [{"label": c["name"], "value": c["id"]} for c in cats], **PERSIST), lg=3),
    ], className="g-2 mb-3 align-items-center")
    actions = html.Div([
        html.Span(id="txn-selected", className="text-muted small"),
        dcc.Dropdown(id="txn-bulk-cat", placeholder="Category for selected", style={"minWidth": "210px"},
                     options=[{"label": c["name"], "value": c["id"]} for c in cats]),
        dbc.Button("Apply", id="txn-bulk-apply", color="primary", size="sm"),
        dbc.Button("Reset to automatic", id="txn-bulk-reset", color="secondary", outline=True, size="sm"),
        dbc.Button([html.I(className="bi bi-funnel me-1"), "Create rule"], id="txn-rule-open",
                   color="primary", outline=True, size="sm"),
    ], className="d-flex align-items-center gap-2 flex-wrap")
    return html.Div([
        filters,
        html.Div(id="txn-summary", className="stat-strip mb-3"),
        panel("Transactions", actions,
              html.Div(className="mb-2"),
              grid("txn-grid", _list_cols(names), [], height=620, dashGridOptions={"rowSelection": MULTI_SELECT}),
              subtitle="Edit category, tags and notes directly in the table. A category you set by hand is never "
                       "changed by syncs, imports or rules. Clear the category to return it to automatic."),
    ])


def _stat(label: str, value: str, cls: str = "") -> html.Div:
    return html.Div([html.Div(label, className="stat-label"), html.Div(value, className=f"stat-value {cls}")],
                    className="stat-chip")


def _money(v: float) -> str:
    return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"


@callback(Output("txn-grid", "rowData"), Output("txn-summary", "children"), Output("txn-dates-col", "style"),
          Input("txn-search", "value"), Input("txn-period", "value"), Input("txn-dates", "start_date"),
          Input("txn-dates", "end_date"), Input("txn-accounts", "value"), Input("txn-categories", "value"),
          Input("txn-refresh", "data"))
def load_transactions(search, period, start, end, account_ids, category_ids, _refresh):
    period = period or "90d"
    if period == "custom":
        lo, hi = (dt.date.fromisoformat(start) if start else None,
                  dt.date.fromisoformat(end) if end else None)
    else:
        lo, hi = tx.period_range(period)
    f = tx.TxnFilter(start=lo, end=hi, account_ids=account_ids or [], category_ids=category_ids or [],
                     search=search)
    rows = services.read(tx.query, f)
    sm = tx.summarize(rows)
    summary = [
        _stat("Transactions", f"{sm['count']:,}"),
        _stat("Income", _money(sm["income"]), "amount-pos"),
        _stat("Spending", _money(sm["spending"])),
        _stat("Net", _money(sm["net"]), "amount-neg" if sm["net"] < 0 else "amount-pos"),
        _stat("Uncategorized", f"{sm['uncategorized']:,}", "text-warning" if sm["uncategorized"] else ""),
    ]
    return rows, summary, ({} if period == "custom" else {"display": "none"})


@callback(Output("txn-selected", "children"), Output("txn-selection", "data", allow_duplicate=True),
          Input("txn-grid", "selectedRows"), prevent_initial_call=True)
def show_selection(rows):
    rows = rows or []
    return (f"{len(rows):,} selected" if rows else "Select rows with the checkboxes"), rows


@callback(Output("txn-msg", "children", allow_duplicate=True), Output("txn-grid", "rowTransaction"),
          Input("txn-grid", "cellValueChanged"), prevent_initial_call=True)
def save_cell(changes):
    if not changes:
        return no_update, no_update
    ids_by_name = {c["name"]: c["id"] for c in services.category_choices()}
    done, ids = [], []
    try:
        for ch in changes:
            tid, col, value = ch["data"]["id"], ch["colId"], ch["value"]
            if col == "category":
                if value:
                    services.write(tx.set_category, [tid], ids_by_name[value])
                    done.append(f"category set to {value}")
                else:
                    services.write(tx.reset_category, [tid])
                    done.append("category returned to automatic")
            else:
                services.write(tx.update_text, tid, col, value)
                done.append(f"{col} saved")
            ids.append(tid)
    except (tx.TransactionError, KeyError) as exc:
        return alert(f"Not saved: {exc}", "danger"), no_update
    return alert("Saved: " + ", ".join(dict.fromkeys(done)) + ".", duration=3000), \
        {"update": services.read(tx.get_rows, ids)}


@callback(Output("txn-msg", "children", allow_duplicate=True), Output("txn-refresh", "data", allow_duplicate=True),
          Input("txn-bulk-apply", "n_clicks"), Input("txn-bulk-reset", "n_clicks"),
          State("txn-selection", "data"), State("txn-bulk-cat", "value"), prevent_initial_call=True)
def bulk_edit(_apply, _reset, rows, category_id):
    if not ctx.triggered_id or not ctx.triggered[0]["value"]:
        return no_update, no_update
    ids = [r["id"] for r in rows or []]
    if not ids:
        return alert("Select transactions first (checkboxes at the start of each row).", "info", duration=4000), \
            no_update
    if ctx.triggered_id == "txn-bulk-apply":
        if not category_id:
            return alert("Choose a category to apply.", "info", duration=4000), no_update
        n = services.write(tx.set_category, ids, category_id)
        return alert(f"Category set on {n:,} transaction(s)."), _bump()
    n = services.write(tx.reset_category, ids)
    return alert(f"{n:,} transaction(s) returned to automatic categorization."
                 if n else "None of the selected transactions had a manual category."), _bump()


# --------------------------------------------------------------------------- rule modal (shared)
def rule_modal() -> dbc.Modal:
    return dbc.Modal(id="rule-modal", is_open=False, size="lg", children=[
        dbc.ModalHeader(dbc.ModalTitle("New rule")),
        dbc.ModalBody([
            dbc.Row([
                dbc.Col([dbc.Label("Text to match", className="small fw-semibold"),
                         dbc.Input(id="rule-pattern", size="sm", debounce=True)], md=6),
                dbc.Col([dbc.Label("In field", className="small fw-semibold"),
                         dbc.Select(id="rule-field", size="sm", value="description",
                                    options=[{"label": f.title(), "value": f} for f in tx.RULE_FIELDS])], md=3),
                dbc.Col([dbc.Label("Match", className="small fw-semibold"),
                         dbc.Select(id="rule-match", size="sm", value="contains",
                                    options=[{"label": m.title(), "value": m} for m in tx.MATCH_TYPES])], md=3),
            ], className="g-2 mb-2"),
            dbc.Row([
                dbc.Col([dbc.Label("Category", className="small fw-semibold"),
                         dcc.Dropdown(id="rule-category", placeholder="Choose a category")], md=6),
                dbc.Col([dbc.Label("Only for account", className="small fw-semibold"),
                         dcc.Dropdown(id="rule-account", placeholder="Any account")], md=6),
            ], className="g-2 mb-3"),
            html.Div(id="rule-preview", className="small"),
            html.Div(id="rule-error"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id="rule-cancel", color="secondary", outline=True, size="sm"),
            dbc.Button("Save rule", id="rule-save", color="primary", size="sm"),
        ]),
    ])


@callback(Output("rule-modal", "is_open"), Output("rule-pattern", "value"), Output("rule-category", "options"),
          Output("rule-category", "value"), Output("rule-account", "options"), Output("rule-account", "value"),
          Output("rule-error", "children"),
          Input("txn-rule-open", "n_clicks"), Input("rule-cancel", "n_clicks"), State("txn-selection", "data"),
          prevent_initial_call=True)
def open_rule_modal(opened, _cancel, selected):
    if ctx.triggered_id == "rule-cancel" or not opened:
        return (False,) + (no_update,) * 6
    cats = services.category_choices()
    accounts = services.account_choices()
    row = (selected or [None])[0]
    pattern = (description_key(row["description"]) or row["description"]) if row else ""
    cat_id = next((c["id"] for c in cats if row and c["name"] == row["category"]), None)
    return (True, pattern, [{"label": c["name"], "value": c["id"]} for c in cats], cat_id,
            [{"label": a["label"], "value": a["id"]} for a in accounts], None, None)


@callback(Output("rule-preview", "children"),
          Input("rule-pattern", "value"), Input("rule-field", "value"), Input("rule-match", "value"),
          Input("rule-category", "value"), Input("rule-account", "value"), Input("rule-modal", "is_open"),
          prevent_initial_call=True)
def preview_rule(pattern, field, match, category_id, account_id, is_open):
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


@callback(Output("rule-modal", "is_open", allow_duplicate=True), Output("rule-error", "children", allow_duplicate=True),
          Output("txn-msg", "children", allow_duplicate=True), Output("txn-refresh", "data", allow_duplicate=True),
          Input("rule-save", "n_clicks"),
          State("rule-pattern", "value"), State("rule-field", "value"), State("rule-match", "value"),
          State("rule-category", "value"), State("rule-account", "value"), prevent_initial_call=True)
def save_rule(clicks, pattern, field, match, category_id, account_id):
    if not clicks:
        return (no_update,) * 4
    try:
        rule, changed = services.write(tx.add_rule, pattern, category_id, field, match, account_id)
    except tx.TransactionError as exc:
        return no_update, alert(str(exc), "danger", duration=None), no_update, no_update
    return False, None, alert(f"Rule '{rule.pattern}' saved; {changed:,} transaction(s) recategorized."), _bump()


# --------------------------------------------------------------------------- transfers
def _pair_cols(names: list[str]) -> list[dict]:
    return [
        {"field": "date", "headerName": "Date", "maxWidth": 120, "minWidth": 110},
        money_col("amount", "Amount", maxWidth=130, minWidth=110, cellClassRules={}),
        {"field": "out_account", "headerName": "From", "minWidth": 140},
        {"field": "in_account", "headerName": "To", "minWidth": 140},
        {"field": "out_description", "headerName": "Outgoing description", "minWidth": 200,
         "tooltipField": "out_description"},
        {"field": "in_description", "headerName": "Incoming description", "minWidth": 200,
         "tooltipField": "in_description"},
        {"field": "days", "headerName": "Days apart", "maxWidth": 110, "type": "rightAligned"},
        editable({"field": "category", "headerName": "Category for both", "minWidth": 170,
                  "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": [KEEP] + names}}),
    ]


def _paired_cols() -> list[dict]:
    return [
        {"field": "date", "headerName": "Date", "maxWidth": 120, "minWidth": 110},
        money_col("amount", "Amount", maxWidth=130, minWidth=110, cellClassRules={}),
        {"field": "from_account", "headerName": "From", "minWidth": 140},
        {"field": "to_account", "headerName": "To", "minWidth": 140},
        {"field": "from_description", "headerName": "Outgoing description", "minWidth": 200,
         "tooltipField": "from_description"},
        {"field": "to_description", "headerName": "Incoming description", "minWidth": 200,
         "tooltipField": "to_description"},
        {"field": "category", "headerName": "Category", "minWidth": 150},
    ]


def _suggestion_rows() -> list[dict]:
    names = {c["id"]: c["name"] for c in services.category_choices()}
    return [{
        "id": p.key, "out_id": p.out_id, "in_id": p.in_id, "date": p.date.isoformat(), "amount": p.amount,
        "out_account": p.out_account, "in_account": p.in_account, "out_description": p.out_description,
        "in_description": p.in_description, "days": p.days, "category": names.get(p.category_id, KEEP),
    } for p in services.read(tr.suggest_pairs)]


def transfers_tab() -> html.Div:
    names = [c["name"] for c in services.category_choices()]
    suggestions = _suggestion_rows()
    sugg_actions = html.Div([
        dbc.Button("Confirm selected", id="pairs-confirm", color="primary", size="sm"),
        dbc.Button("Confirm all", id="pairs-confirm-all", color="primary", outline=True, size="sm"),
        dbc.Button("Not a transfer", id="pairs-dismiss", color="secondary", outline=True, size="sm"),
    ], className="d-flex gap-2")
    return html.Div([
        dcc.Store(id="pairs-overrides", data={}),
        panel("Suggested transfers",
              grid("pairs-grid", _pair_cols(names), suggestions, height=420,
                   dashGridOptions={"rowSelection": MULTI_SELECT}),
              html.Div(id="pairs-count", children=f"{len(suggestions):,} suggestion(s).",
                       className="text-muted small mt-2"),
              subtitle=f"Money leaving one account and the same amount arriving in another within "
                       f"{tr.MATCH_DAYS} days. Confirming links both sides and sets the chosen category on both "
                       f"(transfer categories are excluded from cash flow). Choose {KEEP} to only link them.",
              actions=sugg_actions),
        panel("Matched transfers",
              grid("paired-grid", _paired_cols(), services.read(tr.paired_rows), height=360,
                   dashGridOptions={"rowSelection": MULTI_SELECT}),
              subtitle="Unpairing keeps the categories; change them in the Transactions tab if needed.",
              actions=dbc.Button("Unpair selected", id="pairs-unpair", color="secondary", outline=True,
                                 size="sm")),
    ])


@callback(Output("pairs-overrides", "data"), Input("pairs-grid", "cellValueChanged"),
          State("pairs-overrides", "data"), prevent_initial_call=True)
def remember_pair_category(changes, overrides):
    overrides = dict(overrides or {})
    for ch in changes or []:
        overrides[ch["data"]["id"]] = ch["value"]
    return overrides


@callback(Output("txn-msg", "children", allow_duplicate=True),
          Output("pairs-grid", "rowData"), Output("paired-grid", "rowData"), Output("pairs-count", "children"),
          Output("pairs-overrides", "data", allow_duplicate=True),
          Input("pairs-confirm", "n_clicks"), Input("pairs-confirm-all", "n_clicks"),
          Input("pairs-dismiss", "n_clicks"), Input("pairs-unpair", "n_clicks"),
          State("pairs-grid", "selectedRows"), State("pairs-grid", "rowData"), State("paired-grid", "selectedRows"),
          State("pairs-overrides", "data"), prevent_initial_call=True)
def change_pairs(_c, _ca, _d, _u, selected, all_rows, paired_selected, overrides):
    trigger = ctx.triggered_id
    if not trigger or not ctx.triggered[0]["value"]:
        return (no_update,) * 5
    overrides = overrides or {}
    ids_by_name = {c["name"]: c["id"] for c in services.category_choices()}
    rows = all_rows if trigger == "pairs-confirm-all" else (selected or [])
    errors: list[str] = []
    if trigger == "pairs-unpair":
        for r in paired_selected or []:
            services.write(tr.unpair, r["id"])
        msg = f"Unpaired {len(paired_selected or []):,} transfer(s)."
        if not paired_selected:
            msg = "Select matched transfers to unpair first."
    elif not rows:
        msg = "Select suggestions first (checkboxes at the start of each row)."
    elif trigger == "pairs-dismiss":
        for r in rows:
            services.write(tr.dismiss_pair, r["out_id"], r["in_id"])
        msg = f"{len(rows):,} suggestion(s) marked as not a transfer."
    else:
        done = 0
        for r in rows:
            name = overrides.get(r["id"], r.get("category"))
            try:
                services.write(tr.confirm_pair, r["out_id"], r["in_id"], ids_by_name.get(name))
                done += 1
            except tr.TransferError as exc:
                errors.append(f"{r['date']} {r['amount']:,.2f}: {exc}")
        msg = f"Confirmed {done:,} transfer(s)."
    suggestions = _suggestion_rows()
    return (alert(msg, "danger" if errors else "success", errors or None),
            suggestions, services.read(tr.paired_rows), f"{len(suggestions):,} suggestion(s).", {})


# --------------------------------------------------------------------------- rules
def _rule_cols(names: list[str], account_labels: list[str]) -> list[dict]:
    user_only = {"function": "params.data.origin === 'You'"}

    def ed(col: dict) -> dict:
        return {**editable(col), "editable": user_only}

    return [
        ed({"field": "pattern", "headerName": "Text to match", "minWidth": 220}),
        ed({"field": "field", "headerName": "Field", "maxWidth": 130, "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": tx.RULE_FIELDS}}),
        ed({"field": "match_type", "headerName": "Match", "maxWidth": 120, "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": tx.MATCH_TYPES}}),
        ed({"field": "category", "headerName": "Category", "minWidth": 170, "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": names}}),
        ed({"field": "account", "headerName": "Only for account", "minWidth": 160,
            "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": [ANY_ACCOUNT] + account_labels}}),
        ed({"field": "priority", "headerName": "Priority", "maxWidth": 110, "cellDataType": "number",
            "type": "rightAligned", "headerTooltip": "Lower numbers run first; the first matching rule wins"}),
        {"field": "uses", "headerName": "Transactions", "maxWidth": 130, "type": "rightAligned",
         "headerTooltip": "Transactions this rule currently categorizes"},
        {"field": "origin", "headerName": "Created by", "maxWidth": 120},
    ]


def rules_tab() -> html.Div:
    names = [c["name"] for c in services.category_choices()]
    labels = [a["label"] for a in services.account_choices()]
    actions = html.Div([
        dbc.Switch(id="rules-show-learned", label="Show learned rules", value=True, className="mb-0 me-2",
                   **PERSIST),
        dbc.Button([html.I(className="bi bi-plus-lg me-1"), "Add rule"], id="txn-rule-open", color="primary",
                   size="sm"),
        dbc.Button([html.I(className="bi bi-trash me-1"), "Delete selected"], id="rules-delete", color="danger",
                   outline=True, size="sm"),
    ], className="d-flex align-items-center gap-2")
    return html.Div([
        panel("Categorization rules",
              grid("rules-grid", _rule_cols(names, labels), [], height=520,
                   dashGridOptions={"rowSelection": MULTI_SELECT}),
              subtitle="Rules categorize transactions that you have not categorized by hand and that have no "
                       "Empower category. Learned rules come from your Empower history and are rebuilt on each "
                       "import; to override one, add your own rule (yours run first).",
              actions=actions),
    ])


@callback(Output("rules-grid", "rowData"), Input("rules-show-learned", "value"), Input("txn-refresh", "data"))
def load_rules(show_learned, _refresh):
    return services.read(tx.rule_rows, include_learned=bool(show_learned))


@callback(Output("txn-msg", "children", allow_duplicate=True), Output("txn-refresh", "data", allow_duplicate=True),
          Input("rules-grid", "cellValueChanged"), Input("rules-delete", "n_clicks"),
          State("rules-grid", "selectedRows"), prevent_initial_call=True)
def change_rules(changes, delete_clicks, selected):
    try:
        if ctx.triggered_id == "rules-delete":
            if not delete_clicks:
                return no_update, no_update
            ids = [r["id"] for r in selected or [] if r["origin"] == "You"]
            if not ids:
                return alert("Select your rules to delete first (learned rules cannot be deleted).", "info",
                             duration=4000), no_update
            n, changed = services.write(tx.delete_rules, ids)
            return alert(f"Deleted {n} rule(s); {changed:,} transaction(s) recategorized."), _bump()
        cats = {c["name"]: c["id"] for c in services.category_choices()}
        accounts = {a["label"]: a["id"] for a in services.account_choices()}
        changed = 0
        for ch in changes or []:
            col, value = ch["colId"], ch["value"]
            if col == "category":
                col, value = "category_id", cats.get(value)
            elif col == "account":
                col, value = "account_id", accounts.get(value)
            changed += services.write(tx.update_rule, ch["data"]["id"], col, value)
        return alert(f"Rule saved; {changed:,} transaction(s) recategorized.", duration=4000), _bump()
    except tx.TransactionError as exc:
        return alert(f"Not saved: {exc}", "danger"), _bump()
