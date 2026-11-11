# Running Personal Finance as a Home Assistant add-on

This installs the app as a **local add-on**: Home Assistant builds it from the files in this
repository and runs it in its own container, with the web UI in the HA sidebar (via *ingress*).
Your data lives in the add-on's persistent `/data` volume, so it survives restarts and updates.

> You need a **Home Assistant OS** or **Supervised** install (the add-on store / Supervisor).
> A plain "Home Assistant Container" (Docker-only) install has no add-on store — in that case
> run the app as a normal container instead (see *Plain Docker* at the bottom).

---

## 1. Get the files onto the add-ons share

Home Assistant builds local add-ons from the `addons` folder on the host. Put this repo in a
subfolder there called `personalfinance`. Two easy ways:

### Option A — Samba (no command line)
1. In HA: **Settings → Add-ons → Add-on Store**, install **Samba share**, start it.
2. On your PC, open the network share `\\homeassistant\addons` (or `\\<HA-IP>\addons`).
3. Create a folder `personalfinance` and copy **all files from this repository** into it
   (so you end up with `addons/personalfinance/config.yaml`, `Dockerfile`, `finance/`, etc.).

### Option B — SSH / Terminal (clone straight from GitHub)
1. Install the **Advanced SSH & Web Terminal** add-on and start it.
2. In its terminal:
   ```bash
   cd /addons
   git clone https://github.com/ardelgadillov/PersonalFinance.git personalfinance
   ```
   To update later: `cd /addons/personalfinance && git pull`.

Either way, the key requirement is: **`config.yaml` must sit at `/addons/personalfinance/config.yaml`.**

## 2. Install the add-on
1. **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ → Check for updates**, then reload the page.
3. A **Local add-ons** section now lists **Personal Finance** — open it and click **Install**.
   (The first build takes a few minutes while it compiles the Python environment.)

## 3. Configure it
On the add-on's **Configuration** tab:
- `simplefin_access_url` — your SimpleFIN Bridge access URL (the same value that goes in `.env`
  locally). Leave empty to run read-only / import-only.
- `timezone` — e.g. `America/New_York`.
- `daily_sync_hour` — hour (0–23, local time) for the once-a-day automatic sync. SimpleFIN
  allows only ~24 requests/day, so once daily is deliberate.
- `sync_max_age_hours` — on start-up, sync only if the last sync is older than this (default 12).

Click **Save**.

## 4. Start and open
1. **Info** tab → **Start**.
2. Check the **Log** tab — you should see `Personal Finance running at http://0.0.0.0:8099`
   and, if ingress is working, `Serving behind Home Assistant ingress at /api/hassio_ingress/...`.
3. Click **Open Web UI** (or use the **Finance** item in the sidebar).

## 5. Load your data
- **SimpleFIN**: use the **Sync** button in the app, or wait for the daily sync.
- **Empower history**: **Settings → Empower import** in the app, upload your Empower CSV, then
  link the accounts (see the main README / `CLAUDE.md`).

---

## Updating
Pull the new files into `/addons/personalfinance` (git pull, or recopy over Samba), then in the
add-on store **⋮ → Check for updates**, open the add-on and click **Rebuild**. Your `/data`
(database, imports) is untouched.

## Troubleshooting
- **Blank page / assets 404 behind the sidebar** — the ingress path wasn't picked up. Confirm
  `hassio_api: true` is in `config.yaml` and the log shows the `Serving behind ... ingress` line.
  If not, the app still works on a direct port: add `ports:` to `config.yaml` (e.g.
  `"8099/tcp": 8099`) and open `http://<HA-IP>:8099`.
- **Build fails compiling numpy/pandas** — make sure `build.yaml` still points at
  `python:3.13-slim` (Debian); an Alpine base would try to compile those from source.
- **No "Local add-ons" section** — the files aren't at `/addons/personalfinance/config.yaml`,
  or you're on a Container-only install without the Supervisor.

## Plain Docker (no Supervisor)
If you don't have the add-on store, run it directly:
```bash
docker build -t personalfinance .
docker run -d --name personalfinance -p 8050:8099 \
  -e SIMPLEFIN_ACCESS_URL="<your-simplefin-url>" \
  -e FINANCE_TIMEZONE="America/New_York" \
  -e FINANCE_DAILY_SYNC_HOUR=3 \
  -v /path/on/host/finance-data:/data \
  personalfinance
```
Then open `http://<host>:8050`. (No ingress here, so it serves at the root path.)
