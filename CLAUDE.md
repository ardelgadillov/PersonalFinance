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
- `finance/cashflow.py` pandas cash flow analytics (monthly, by category, trend, merchants, Sankey).
- `finance/networth.py` net worth from `balance_snapshots` (daily, by group, monthly change, account rows).
- `finance/investments.py` holdings from `holding_snapshots` (gain, weight, price change, allocation, cost basis,
  time-weighted return). `finance/benchmarks.py` benchmark closes (via the `yfinance` library, cached in
  `benchmark_prices`; fake deterministic series in demo mode; also dividend-adjusted fund closes in `fund_prices`).
- `finance/optimizer.py` Markowitz (mirrors the QuiraData notebook: log returns x 252, SLSQP, long only) + cap per
  fund, frontier point, walk-forward test, fund-list parsing. `finance/fundmenu.py` per-account fund menus
  (`fund_menu`) and the current mix from the latest holdings. Page: `finance/app/pages/optimizer.py`.
- `finance/services.py` data access for the UI (no Dash imports; unit-tested). `services.read(fn, ...)` /
  `services.write(fn, ...)` run a session-based function (write commits). `finance/background.py` threaded sync manager.
- `finance/app/` Dash UI: `main.py` (sidebar shell, routing, sync polling, theme), `pages/*.py`, `components.py`,
  `charts.py` (Plotly theme: `base_layout(theme)`, `colors(theme)`), `assets/style.css`.
- `finance/demo.py` deterministic simulated household + fake SimpleFIN client + fake Empower CSV.
- `finance/addon.py` Home Assistant add-on glue (active only when `SUPERVISOR_TOKEN` is set): reads
  `/data/options.json` into env vars, and this add-on's ingress base path from the Supervisor API (becomes
  Dash `requests_pathname_prefix`; routes stay at `/`). Root `Dockerfile`/`config.yaml`/`build.yaml`/`run.sh`
  package it as a local HA add-on (`python:3.13-slim` base); `SyncManager.schedule_daily` runs the daily sync
  (`FINANCE_DAILY_SYNC_HOUR`). Install steps: `INSTALL_HOMEASSISTANT.md`. No Docker in dev - build/ingress
  verified only in a live HA instance.
- `tests/` pytest; `tests/conftest.py` seeds categories from the frozen `tests/fixtures/categories.csv`.

## Data rules (keep these invariants)
- Amounts: negative = money out (SimpleFIN convention); Empower amounts are sign-detected on import.
- Category precedence: `manual` > `empower` > `rule`. Syncs/imports/recategorization must never overwrite manual edits.
- SimpleFIN only gives *current* balances/holdings; history comes from daily snapshots (`balance_snapshots`, `holding_snapshots`).
- Cash flow excludes transfer-group categories, confirmed transfer pairs and hidden accounts (unless selected);
  spending = -(expense amounts), so refunds reduce spending; uncategorized counts by sign.
- Net worth: accounts with `include_in_networth` and not hidden (unless selected); balances carried forward
  over missing days up to the account's `last_seen`; liability types count as -abs(balance); untyped accounts
  are an asset or a debt by the sign of their latest balance.
- Investments: accounts of an investment type or with holding snapshots (hidden unless selected); value over time
  = account balances (incl. cash); holdings = latest snapshot <= end of range per account; gain only where a cost
  basis is reported; account colors come from `charts.THEMES[...]['series']` by a slot fixed across filters.
  Returns are price returns (no dividends) chained on the prior day's shares, so deposits/buys are not gains;
  benchmarks use the same basis. A benchmark is re-fetched at most every 6 h and only when the cache lacks the range.
- Optimizer: in-sample = last N years of the funds' common history; walk-forward re-optimizes every 12 months
  on the trailing training window only (never future prices); the current mix only counts holdings that are in
  the fund menu. Funds without a ticker cannot be priced and are skipped.
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
- Charts: figures are built server-side with `Input("theme", "data")` so they re-render on the theme toggle.
  Data colors are fixed by meaning (income blue, spending orange, savings aqua; validated for color-blind safety
  in both modes) and live in `charts.THEMES`. One y-axis per chart; legend for 2+ series.
- All colors are defined once in `finance/app/theme.py` (`THEMES`: per-theme `ui` + `chart` tokens). To add or
  restyle a theme, edit that dict only. `theme.css()` generates the `--app-*` CSS variable blocks (injected into
  the page `<head>` by `main.py`), and `charts.THEMES` is derived from the same source. `assets/style.css` is
  structural only and just references `var(--app-*)`; never hardcode a color there or in a page. The dark theme is
  One Dark (Atom) inspired. Dash 4 dcc components are themed through their `--Dash-*` variables (mapped to `--app-*`).
- Everything should be configurable in the app, not via external files.

## Gotchas
- Components referenced as `State`/`Input` in a callback must always exist in the page layout (e.g. modal contents),
  otherwise Dash silently skips the callback.
- Tabs rendered by a callback: every Input/State of a callback must live in the same tab (or at page level).
  Shared state across tabs goes in page-level `dcc.Store`s (e.g. `txn-refresh`, `txn-selection`).
- A callback Output whose id is missing from the layout makes Dash stop ALL callbacks on the page (blank charts,
  dead filters). Never test a Dash component's truthiness (`if comp:`): an empty component has `len() == 0`;
  use `is not None`. `tests/test_app.py::test_every_callback_id_exists_in_its_view` guards this for every page/tab.
- Debugging the real browser: headless Edge can load localhost, e.g. `msedge --headless=new --enable-logging=stderr
  --screenshot=out.png http://127.0.0.1:8050/cashflow` (console errors appear in stderr as `CONSOLE`).
- Callbacks with `allow_duplicate=True` get a hash suffix in their output id; HTTP probes must read ids from `/_dash-dependencies`.
- When editing files with Python/heredoc scripts in Git Bash, `"\n"` inside replacement strings can become real newlines
  and break string literals; prefer the Edit tool for such lines. Always import-check (`python -c "import finance.cli"`).
- Never read or print `.env` (contains the SimpleFIN access URL with credentials). `.env` and `data/` are gitignored;
  PyCharm may auto-stage new files, so check `git status` before committing.
- Git workflow: commit straight to `main` (no feature branches) and push when asked. This solo project has no
  PR/review step; still run the tests before committing.
- Outbound HTTPS from Python on this machine goes through a TLS-inspecting proxy: Python 3.13's VERIFY_X509_STRICT
  (the `requests`/`ssl` default) rejects its CA. `benchmarks.py` now fetches via `yfinance`, which uses `curl_cffi`
  and verifies against the OS trust store, so it works behind the proxy without that workaround (and its browser
  impersonation avoids Yahoo's rate-limiting that a plain `requests.Session` hits). The SimpleFIN client still uses
  `requests`; if it ever needs the strict flag dropped, re-add a system-trust adapter there.
- Run the app with `use_reloader=False` (already in `run()`): the reloader would start two background syncs.
