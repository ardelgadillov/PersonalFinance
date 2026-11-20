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
6. ✅ **Net worth page**: KPIs (net worth, assets, debts, change), stacked area by group (cash, investments, other assets above zero; credit cards, loans below) with net worth line, change by month, composition, accounts table with per-account history; balances carried over missed sync days until the account stops being seen; liabilities always count as debt
7. ✅ **Investments page**: KPIs (portfolio value, cost basis, unrealized gain, change), value over time stacked by account with a cost basis line, allocation by holding or account (click a holding to filter the page to it, an account to filter by it), performance vs a benchmark (S&P 500 by default; portfolio, selected holding, benchmark as time-weighted price returns), holdings table (gain, weight, price change that ignores deposits), per-holding history
7b. ✅ **Optimizer page**: per-account fund menu (CSV upload, paste, add current holdings), Markowitz max Sharpe / min variance / frontier slider with a cap per fund and risk-free rate, current mix on the frontier, in-sample backtest and walk-forward (out-of-sample) test vs the current mix
8. ✅ **Home Assistant add-on**: root `Dockerfile` / `config.yaml` / `build.yaml` / `run.sh` (local add-on,
   Debian `python:3.13-slim` base), `finance/addon.py` reads `/data/options.json` into the env and the
   ingress base path from the Supervisor API (becomes Dash `requests_pathname_prefix`; routes stay at `/`),
   `/data` volume via `FINANCE_DATA_DIR`, daily sync via `SyncManager.schedule_daily` (APScheduler,
   `FINANCE_DAILY_SYNC_HOUR`). Install steps in `INSTALL_HOMEASSISTANT.md`. Image build + ingress still to be
   verified in a live HA instance (no Docker/Supervisor available in dev).

# My test/changes
1. ✅ Manual accounts: added balance-only accounts you maintain by hand (Settings → Accounts → "Add a
   manual account": name, type, balance, in-net-worth). Edit the Balance cell to record a new value for today;
   select the row and Delete to remove it. They flow into net worth (liability types count as debt), carry
   their last balance forward, and never auto-disconnect. (`services.create_manual_account` /
   `update_manual_balance` / `delete_manual_account`; `source="manual"`.)
2. ✅ Cash flow comparison line: year-to-date now compares against the SAME calendar months one year earlier
   (was an equal-length window shifted by the YTD length, so January lined up with last February). Whole-year
   periods (12m/24m/last year) keep the prior equal-length window, which is already calendar-aligned.
   (`cashflow.py::_prev_cumnet`.)
3. ✅ Optimizer "Ticker performance" -> "Performance vs market": instead of one clicked ticker, the chart now
   overlays the account's portfolio (its current mix, or the max-Sharpe mix when there is no current one), the
   clicked ticker (optional), and a selectable market benchmark (S&P 500 default; VTI / QQQ / VT / AGG), all as
   cumulative total return over the optimizer's in-sample window on the same dividend-adjusted basis. The
   Investments page performance chart was left as-is (it already narrows to the selected period; it can only
   start at the first sync, since balance/holding history begins there). (`optimizer.py::ticker_performance`.)

# Homeassistant test/changes
1. Is it possible to add an icon that shows in the app in homeassistant?
2. I also noticed that the app in homeassistant is also a little bit slower, what are the possible reasons for that?
3. initial page shows Page Not found. It shows Go to Net worth in http://homeassistant.local:8123/local_personalfinance
4. In the cellphone, the KPIs on top use half of the screen in all different tabs. I like the main KPIs but I would like
   if they don't use so much space
5. Networth page. I think Change by month and Account history charts are not needed. When selecting an account on the table
   the networth overtime chart should show only that account
6. Cashflow page.
   - I do not like the dots in the Net so far line. Show the line without the dots. 
   - the transaction table should show all transactions related to cash flow (cash accounts and credit cards), 
     not contributions or investments
   - The category trend chart is not needed. Instead, when selecting a category in the spending by category use the chart
     on top to show the bars for that category
7. Optimization page. Sometimes there is an error when opening the optimization page. I have put the log in local_personalfinance_2026-11-20T15-11-03.155Z.log
   that shows the error

## Addressed 2026-11-20
1. ✅ Add-on icon: added `icon.png` + `logo.png` at the repo root (HA auto-detects them for the store/add-on
   page); the sidebar entry already uses `panel_icon: mdi:finance`. Replace the two PNGs with your own art any
   time. (Answer: the sidebar icon was always configurable; the card/logo just needed the image files.)
2. ℹ️ Slower in HA: most likely (a) ingress proxying every asset/callback through the Supervisor, (b) slower
   host storage (SD card / small VM) making SQLite writes slow, and (c) the DB-lock stalls from item 7. The
   item-7 fix removes the stalls; faster storage for the data dir is the main lever otherwise.
3. ✅ "Page not found" on open: behind ingress `dcc.Location` reports the path *with* the ingress prefix, so the
   router never matched `/`. Routing now strips the request prefix (`main._route_key`) and nav/links use
   `get_relative_path`. Needs confirming in a live HA instance.
4. ✅ Mobile KPIs: compact two-column chips (smaller padding/fonts, sub-hints hidden) under 860px in `style.css`.
5. ✅ Net worth page: removed "Change by month" and "Account history"; "Composition" is now full width; clicking
   an account in the table shows only that account in the top chart, with a "Show all accounts" button to revert.
6. ✅ Cash flow page: Net line drawn without dots; the Transactions table is scoped to cash-flow transactions
   (transfers/contributions and investment-type accounts excluded unless an account is explicitly selected);
   "Category trend" removed — clicking a category now shows it in the top chart, Clear filter restores the view.
7. ✅ Optimizer "database is locked": added `PRAGMA busy_timeout=30000` so a competing writer waits, and moved
   the fund-price network fetches out of the write transaction so the SQLite write lock is held only briefly.
