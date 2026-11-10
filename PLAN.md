# Personal Finance Dash App — Plan

Track cash flow, investments and net worth from SimpleFIN Bridge data, with
historical transactions imported from Empower (Personal Capital).

## Decisions

| Area | Choice |
|---|---|
| Runtime | Local first; later packaged as a **Home Assistant add-on** (Docker + ingress) |
| Stack | Dash + dash-bootstrap-components + dash-ag-grid; left sidebar layout, light/dark toggle, indigo accent, no emojis, SQLAlchemy + pandas, Plotly; plain `pip` + `requirements.txt`, installed into the conda env `personalfinance` (PyCharm interpreter) |
| Storage | SQLite `data/finance.db`; raw SimpleFIN JSON archived in `data/raw/` |
| Credentials | `SIMPLEFIN_ACCESS_URL` in `.env` (gitignored). Access URL already claimed. |
| Sync | Manual **Sync** button + on startup if last sync > 12h. Daily APScheduler job in the HA add-on. |
| Categories | Managed in the app (Settings → Categories: add, rename, group, parent, order, delete with move, Empower mapping). `finance/default_categories.csv` only seeds a new database. Regex/keyword rules + learned rules + manual per-transaction override (manual wins) |
| Transfers | `Transfer` category excluded from cash flow + auto-match +/- pairs across own accounts within ±3 days (suggested, user confirms) |
| Accounts | User sets type once in UI (checking, savings, credit card, brokerage, retirement, loan, mortgage, crypto) + hidden / include-in-net-worth flags |
| Currency | USD only |
| Auth | None (HA ingress handles it later) |
| Testing | `pytest` + demo mode: `python -m finance demo` builds `data/demo/` from a deterministic simulation (fake SimpleFIN client, 1 year of snapshots, fake Empower CSV); `--demo` flag on any command uses it |
| History | Net worth / balances / holdings history starts at first SimpleFIN sync (daily snapshots). Past transactions come from Empower CSV. No balance back-fill. |

## SimpleFIN

- `GET {access_url}/accounts?start-date=<epoch>&end-date=<epoch>&pending=1`
- Response: `errors[]`, `accounts[]` with `org`, `id`, `name`, `currency`, `balance`,
  `available-balance`, `balance-date`, `transactions[]` (`id`, `posted`, `amount`,
  `description`, `payee`, `memo`, `transacted_at`, `pending`), `holdings[]`
  (`id`, `created`, `symbol`, `description`, `shares`, `cost_basis`,
  `market_value`, `purchase_price`, `currency`).
- Limits: 45 days per request recommended, ~24 requests/day.
- First sync walks back in 45-day windows (Bridge recommendation), up to 12 requests as far as the bank returns data;
  subsequent syncs pull the last 14 days and upsert by transaction id
  (pending → posted handled).
- Every sync writes one balance snapshot and one holdings snapshot per account per day.
- SimpleFIN `errors[]` are stored in `sync_log` and shown in the UI.

## Empower import (transactions only)

- Source: Empower Transactions page → CSV export with columns
  `Date, Account, Description, Category, Tags, Amount`.
- Command: `python -m finance import-empower <file.csv>` (also an upload box in Settings). Idempotent.
- Amount sign auto-detected (expense categories should come out negative, matching SimpleFIN).
- **Account linking:** each Empower account name is linked once to a SimpleFIN
  account in the UI. Unlinked/closed accounts become history-only accounts.
- **Overlap handling:** per linked account, a cutoff = first SimpleFIN transaction
  date. Empower rows before the cutoff are kept, SimpleFIN rows from the cutoff on.
- **Categories:** Empower categories mapped to app categories; payee → category
  history used to auto-generate rules for new SimpleFIN transactions.
- Tags stored as transaction tags.
- Every row has a `source` (`simplefin` / `empower`).

## Data model (SQLite)

- `accounts`: id, source, org_name, name, display_name, type, hidden, include_in_networth, linked_empower_name
- `balance_snapshots`: (account_id, date) → balance, available_balance
- `holding_snapshots`: (account_id, holding_id, date) → symbol, description, shares, cost_basis, market_value, purchase_price
- `transactions`: id, account_id, source, posted, transacted_at, amount, description, payee, memo, pending, category_id, category_source (rule/manual/empower), tags, transfer_pair_id, note
- `categories`: id, name, group (income / expense / transfer)
- `category_map`: empower_category → category_id
- `rules`: id, field, pattern, match_type, category_id, priority
- `sync_log`: id, started_at, finished_at, status, errors_json, raw_path

## Project layout

```
requirements.txt   .env   .gitignore   data/ (gitignored)
finance/                     # run with `python -m finance ...`
  config.py  db.py  models.py  cli.py
  simplefin/client.py        # fetch + parse
  sync.py                    # windows, upserts, snapshots
  importers/empower.py
  categorize.py  transactions.py  transfers.py  cashflow.py  scheduler.py
  app/
    app.py  layout.py
    pages/networth.py  cashflow.py  investments.py  transactions.py  settings.py
tests/                       # fixture JSON/CSV, no real data
```

## Pages

1. **Net worth**: KPIs (net worth, assets, liabilities, change), stacked area by type/account, date range
2. **Cash flow**: monthly income vs expense, savings rate, spending by category + trend, Sankey, top payees
3. **Investments**: holdings table with gain/loss, allocation by symbol/account, portfolio value over time
4. **Transactions**: filterable editable table, category dropdown, tags, transfer suggestions
5. **Settings**: account types & flags, Empower account linking, categories & rules editor ("apply to existing"), Sync button, sync log, Empower CSV upload

## Milestones

1. ✅ **Foundation**: requirements.txt, `.gitignore`, config, DB models, SimpleFIN client, `finance sync` CLI, tests with fixtures
2. ✅ **Empower import**: CSV importer, account linking, category mapping, rule generation
3. ✅ **Settings page + Sync button + sync on startup**
4. ✅ **Transactions page**: filterable editable list (category, tags, notes; bulk set/reset), rules editor with live preview, transfer pair suggestions (confirm / not a transfer / unpair)
5. ✅ **Cash flow page**: KPIs (income, spending, net, savings rate, avg monthly spending), income vs spending by month with net line, spending by category (click for monthly trend), Sankey, top merchants; period + account filters
6. **Net worth page**
7. **Investments page**
8. **Home Assistant add-on**: Dockerfile, `config.yaml`, ingress path prefix, `/data` volume, daily scheduler
