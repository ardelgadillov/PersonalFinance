# Personal Finance (Dash + SimpleFIN)

Personal finance tracker: syncs accounts, balances, holdings and transactions from SimpleFIN Bridge,
imports historical transactions from Empower (Personal Capital) CSV exports, and shows cash flow,
investments and net worth in a Plotly Dash web app. Roadmap, decisions and milestone status: `PLAN.md`.

## Environment & commands (Windows)
- Python env: conda env `personalfinance` (PyCharm interpreter). Plain `pip` + `requirements.txt`; no uv, no `.venv`.
  Run tools as `python ...` with that env (full path: `C:/Users/AndresDelgadillo/miniconda3/envs/personalfinance/python.exe`).
- Tests: `python -m pytest -q -p no:dash` (the dash pytest plugin emits noise; always pass `-p no:dash`).
- Demo data (never touches real data or SimpleFIN): `python -m finance demo --reset`, then add `--demo` to any
  command, e.g. `python -m finance --demo app`, `--demo sync`, `--demo import-empower data/demo/import/empower_transactions.csv`.
- Web app: `python -m finance app [--port 8050] [--no-sync] [--debug]` -> http://127.0.0.1:8050
- Other CLI: `sync [--full]`, `status`, `import-empower FILE`, `empower-links [--auto|--set NAME ID]`, `categories`.

## Layout
- `finance/config.py` settings from `.env` (`SIMPLEFIN_ACCESS_URL`, `FINANCE_TIMEZONE`, `FINANCE_DATA_DIR`, `FINANCE_DEMO`).
- `finance/models.py` SQLAlchemy models; `finance/db.py` engine, `init_db` (create tables, add missing columns, seed categories).
- `finance/simplefin/client.py` API client; `finance/sync.py` windowed sync + daily balance/holding snapshots.
- `finance/importers/empower.py` CSV import: staging table `empower_rows` -> account linking -> dedupe vs SimpleFIN -> learned rules.
- `finance/categories.py` category CRUD + Empower mapping; `finance/categorize.py` rule engine.
- `finance/transactions.py` listing/filters, manual edits, rules CRUD + preview; `finance/transfers.py` transfer pair
  suggestions (opposite amounts, different accounts, <= 3 days), confirm / dismiss (`transfer_dismissals`) / unpair.
- `finance/services.py` data access for the UI (no Dash imports; unit-tested). `services.read(fn, ...)` /
  `services.write(fn, ...)` run a session-based function (write commits). `finance/background.py` threaded sync manager.
- `finance/app/` Dash UI: `main.py` (sidebar shell, routing, sync polling, theme), `pages/*.py`, `components.py`, `assets/style.css`.
- `finance/demo.py` deterministic simulated household + fake SimpleFIN client + fake Empower CSV.
- `tests/` pytest; `tests/conftest.py` seeds categories from the frozen `tests/fixtures/categories.csv`.

## Data rules (keep these invariants)
- Amounts: negative = money out (SimpleFIN convention); Empower amounts are sign-detected on import.
- Category precedence: `manual` > `empower` > `rule`. Syncs/imports/recategorization must never overwrite manual edits.
- SimpleFIN only gives *current* balances/holdings; history comes from daily snapshots (`balance_snapshots`, `holding_snapshots`).
- Transfer pairs: both rows' `transfer_pair_id` point at each other; `clean_pairs` drops links whose partner is gone.
- Rules only touch transactions with `category_source` NULL/`rule`; learned rules (`origin=empower`) are rebuilt on
  each Empower refresh, so users override them with their own rules (priority 100 < ~990), never by editing.
- Empower rows for a linked account are used only before the first SimpleFIN transaction (cutoff); matches are not duplicated.
- Categories live in the DB and are managed in Settings -> Categories. `finance/default_categories.csv` only seeds a NEW database.
- No migration tool: `_add_missing_columns` in `db.py` adds new nullable columns; avoid renames/drops of columns.

## SimpleFIN limits
- ~24 requests/day per connection; <= 45 days per request. Normal sync = 1 request; full/initial sync <= 12.
- Do not run real syncs casually while testing; use demo mode or the public demo URL
  `https://demo:demo@beta-bridge.simplefin.org/simplefin` (its data is random on every request).

## UI conventions (user preferences)
- Left sidebar navigation, light/dark toggle (Bootstrap `data-bs-theme` + AG Grid `data-ag-theme-mode`), indigo accent.
- No emojis anywhere in the UI (titles, headers, labels). Bootstrap Icons (`bi-*`) are fine.
- Tables: dash-ag-grid 35 via `components.grid()`; mark editable columns with `components.editable()`; content in `components.panel()`.
- Colors only via CSS variables in `assets/style.css` (`--app-*`) so both themes work. Dash 4 dcc components are
  themed through their `--Dash-*` variables (mapped to `--app-*` on `html:root`).
- Everything should be configurable in the app, not via external files.

## Gotchas
- Components referenced as `State`/`Input` in a callback must always exist in the page layout (e.g. modal contents),
  otherwise Dash silently skips the callback.
- Tabs rendered by a callback: every Input/State of a callback must live in the same tab (or at page level).
  Shared state across tabs goes in page-level `dcc.Store`s (e.g. `txn-refresh`, `txn-selection`).
- Callbacks with `allow_duplicate=True` get a hash suffix in their output id; HTTP probes must read ids from `/_dash-dependencies`.
- When editing files with Python/heredoc scripts in Git Bash, `"\n"` inside replacement strings can become real newlines
  and break string literals; prefer the Edit tool for such lines. Always import-check (`python -c "import finance.cli"`).
- Never read or print `.env` (contains the SimpleFIN access URL with credentials). `.env` and `data/` are gitignored;
  PyCharm may auto-stage new files, so check `git status` before committing.
- Run the app with `use_reloader=False` (already in `run()`): the reloader would start two background syncs.
