# Personal Finance

A self-hosted personal finance tracker. It syncs accounts, balances, holdings and transactions
from [SimpleFIN Bridge](https://beta-bridge.simplefin.org/), imports historical transactions from
Empower (Personal Capital) CSV exports, and shows cash flow, investments and net worth in a
[Plotly Dash](https://dash.plotly.com/) web app.

Everything runs locally against a SQLite database; it can also be packaged as a
[Home Assistant add-on](INSTALL_HOMEASSISTANT.md).

## Features

- **Net worth** — assets and debts over time (stacked areas with a net-worth line), monthly change,
  composition, and a per-account history. Balances are carried over days with no sync.
- **Cash flow** — income vs. spending by month with a net line, spending by category (click through
  to a monthly trend), a Sankey flow diagram, and top merchants. Transfers are excluded.
- **Investments** — portfolio value over time stacked by account with a cost-basis line, allocation
  by holding or account, performance vs. a benchmark (S&P 500 by default), and per-holding history.
- **Optimizer** — per-account fund menus and a Markowitz optimizer (max Sharpe / min variance /
  efficient frontier) with a cap per fund, plus an in-sample and walk-forward backtest vs. your
  current mix.
- **Transactions** — a filterable, editable list with regex/keyword rules (live preview), learned
  rules from Empower, manual per-transaction overrides, and automatic transfer-pair matching.
- **Settings** — manage accounts, categories, Empower import/links, and view the sync log.
- Light/dark theme (indigo accent, color-blind-safe chart palette), no external config files —
  everything is configured in the app.

## Requirements

- Windows with the conda environment **`personalfinance`** (the PyCharm interpreter).
  Dependencies are plain `pip` + `requirements.txt` (no uv, no `.venv`).
- A SimpleFIN Bridge access URL (optional — you can run import-only or in demo mode).

Full interpreter path:
`C:/Users/AndresDelgadillo/miniconda3/envs/personalfinance/python.exe`

## Setup

1. Install dependencies into the conda env:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a `.env` file in the project root (it is gitignored — never commit it):
   ```ini
   SIMPLEFIN_ACCESS_URL=https://<user>:<pass>@bridge.simplefin.org/simplefin
   FINANCE_TIMEZONE=America/New_York
   # FINANCE_DATA_DIR=./data        # where finance.db and snapshots live (default: ./data)
   ```
3. Start the app:
   ```bash
   python -m finance app
   ```
   Then open <http://127.0.0.1:8050>.

## Command line

```bash
python -m finance app [--port 8050] [--no-sync] [--debug]   # web app
python -m finance sync [--full]                             # download from SimpleFIN
python -m finance status                                    # accounts and last sync
python -m finance import-empower FILE                       # import an Empower CSV
python -m finance empower-links [--auto | --set NAME ID]    # link Empower <-> SimpleFIN accounts
python -m finance categories                                # list categories
```

> SimpleFIN Bridge allows only ~24 requests/day per connection (<= 45 days each). A normal sync is
> one request; a full/initial sync is up to 12. Avoid running real syncs casually while testing —
> use demo mode instead.

## Demo mode

Demo mode builds a deterministic fake household in `data/demo/` and never touches your real data or
SimpleFIN:

```bash
python -m finance demo --reset                 # build/rebuild the demo database
python -m finance --demo app                   # run the app on demo data
python -m finance --demo sync                  # fake sync
python -m finance --demo import-empower data/demo/import/empower_transactions.csv
```

## How the data works

- **Amounts**: negative = money out (SimpleFIN convention). Empower amounts are sign-detected on import.
- **History**: SimpleFIN only returns *current* balances/holdings, so net-worth and investment history
  is built from daily snapshots that start at your first sync. Past transactions come from the Empower
  CSV (used only before each account's first SimpleFIN transaction). There is no balance back-fill.
- **Categories**: live in the database and are managed in **Settings → Categories**. Precedence is
  `manual` > `empower` > `rule` — syncs, imports and recategorization never overwrite a manual edit.
- **Transfers**: opposite-amount pairs across your own accounts within 3 days are suggested; you
  confirm them, and they are excluded from cash flow.

## Home Assistant add-on

The repo doubles as a local Home Assistant add-on (Docker + ingress + a daily sync). See
[INSTALL_HOMEASSISTANT.md](INSTALL_HOMEASSISTANT.md) for the step-by-step install.

## Testing

```bash
python -m pytest -q -p no:dash
```
(The `dash` pytest plugin is noisy; `-p no:dash` silences it.)

## Project layout

See [CLAUDE.md](CLAUDE.md) for a detailed module map and invariants, and [PLAN.md](PLAN.md) for the
roadmap and design decisions. In short:

- `finance/` — core logic: `config`, `models`, `db`, `sync`, `simplefin/`, `importers/`,
  `categories`, `categorize`, `transactions`, `transfers`, `cashflow`, `networth`, `investments`,
  `optimizer`, `fundmenu`, `benchmarks`, `services`, `background`, `demo`, `addon`, `cli`.
- `finance/app/` — the Dash UI: `main` (shell/routing), `pages/`, `components`, `charts`, `theme`,
  `assets/`.
- `tests/` — pytest suite.
