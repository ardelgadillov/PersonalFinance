# Running Personal Finance as a Home Assistant add-on

This guide installs the app as a **local add-on**. That means Home Assistant takes the files from
this project, builds them into its own little container, and runs the app for you — with the web
page showing up right inside the Home Assistant sidebar. Your data (database, imports) is kept in a
private folder called `/data` that survives restarts, reboots and updates.

The steps below assume you have **never installed a Home Assistant add-on before**. Follow them in
order and you'll be fine. Each step says exactly where to click.

> **"Add-ons" is now called "Apps".**
> In Home Assistant **2026.2** (February 2026) the **Add-ons** section was renamed **Apps**. It's
> only a name change — local add-ons still install and work exactly the same way. In this guide,
> wherever you see "Add-on Store" / "Add-ons", your menu may say **"App Store" / "Apps"**; they are
> the same thing. The one place it matters is the **Samba share**, which was renamed from `addons`
> to **`local_apps`** (see Step 1).

> **Do you have the right kind of Home Assistant?**
> This only works on **Home Assistant OS** or **Home Assistant Supervised** — the versions that
> have an **App Store / Add-on Store** (look in **Settings → Apps** (or **Add-ons**); if you see a
> store button, you're good). If you installed the "Home Assistant Container" (a plain Docker image)
> there is no store, and you should skip to the **Plain Docker** section at the very bottom instead.

> **Quick answer to a common question: do I need a `.env` file here?**
> **No.** The `.env` file is only for running the app on your own PC. In Home Assistant you type
> the same settings (SimpleFIN URL, timezone, etc.) into the add-on's **Configuration** tab in
> **Step 3** — the add-on turns those into what the app needs automatically. You never create a
> `.env` file on the Home Assistant machine.

---

## Step 1 — Copy the project files into Home Assistant

Home Assistant looks for local add-ons in its **local apps** folder. You need to put this whole
project inside it, in a subfolder named exactly `personalfinance`.

When you're done, the file `config.yaml` from this project must end up **directly** in that
subfolder — over the network share: `local_apps\personalfinance\config.yaml`; from a terminal:
`/addons/personalfinance/config.yaml` (the terminal path is still `/addons`). That path is what
makes Home Assistant notice the add-on, so double-check it before moving on.

> **Two names, one folder.** The network (Samba) share used to be called `addons` and is now
> `local_apps` (the old `addons` share still works but is deprecated). Inside the terminal the path
> is unchanged — still `/addons`. So you clone into `/addons` over SSH, and you see it as
> `local_apps` over the network. Do **not** create an extra nested folder (a common mistake is
> ending up with `local_apps\personalfinance\PersonalFinance\config.yaml` — one level too deep).

Pick **one** of the two ways below. If you're not comfortable with a command line, use Option A.

### Option A — Using Samba (drag-and-drop over the network, no commands)

1. In Home Assistant, go to **Settings → Apps → App Store** (older versions: **Add-ons → Add-on Store**).
2. Find **Samba share** in the list, click it, click **Install**, then **Start** it.
   (Open its **Configuration** tab first if it asks you to set a username/password.)
3. On your Windows PC, open **File Explorer** and type this into the address bar at the top, then
   press Enter: `\\homeassistant\local_apps`
   (If that doesn't open, replace `homeassistant` with your Home Assistant's IP address, for
   example `\\192.168.1.50\local_apps`. You can find the IP in **Settings → System → Network**.
   On older Home Assistant the share is named `addons` instead.)
4. Inside that shared `local_apps` folder, create a **new folder** named `personalfinance`.
5. Copy **every file and folder from this project** into that new `personalfinance` folder — the
   whole thing, including `config.yaml`, `Dockerfile`, the `finance` folder, and so on.
   When finished you should see `local_apps\personalfinance\config.yaml` over the network share —
   with `config.yaml` directly inside, not inside a second nested folder.

### Option B — Using the command line (clones the latest code from GitHub)

1. In the **App Store / Add-on Store**, install **Advanced SSH & Web Terminal** and **Start** it,
   then open its **Terminal** (the "Open Web UI" button, or the **Terminal** item in the sidebar).
2. Type these commands. Make sure the target folder does **not** already exist first, so you don't
   get a double-nested clone:
   ```bash
   cd /addons
   rm -rf personalfinance            # only if a leftover/empty folder is there
   git clone https://github.com/ardelgadillov/PersonalFinance.git personalfinance
   ls personalfinance/config.yaml    # must print the path, not "No such file"
   ```
3. Later, when you want the newest version, run:
   ```bash
   cd /addons/personalfinance && git pull
   ```

> **Note:** the **"Add repository"** button in the store does **not** work for this project. That
> button is only for *repositories that bundle several add-ons* (they need a `repository.yaml` at
> the repo root). This project is a single add-on, so adding its GitHub URL there gives
> *"not a valid repository"* — that's expected. Use the local install above instead.

---

## Step 2 — Install the add-on

1. Go to **Settings → Apps → App Store** (older versions: **Add-ons → Add-on Store**).
2. In the **top-right corner**, click the **⋮** (three-dots menu) → **Check for updates**. Then
   **refresh the browser page** (press F5).
3. Scroll down. You should now see a section called **Local apps** (older versions: **Local add-ons**)
   with **Personal Finance** inside it. Click it, then click **Install**.
   - The **first install takes several minutes** — Home Assistant is compiling the Python
     environment. This is normal; let it finish. (If you want to watch, the **Log** tab shows
     progress.)

> **Don't see a "Local apps" section, even after Check for updates + F5?** Work through these:
> 1. **Confirm the path.** The file must be exactly at `/addons/personalfinance/config.yaml`
>    (over the share: `local_apps\personalfinance\config.yaml`) — **not** `.../PersonalFinance/...`
>    inside a second nested folder. In the SSH terminal: `ls /addons/personalfinance/config.yaml`.
> 2. **Force a full rescan** from the SSH terminal (more reliable than the menu button):
>    ```bash
>    ha addons reload
>    ```
>    then refresh the browser.
> 3. **Check the Supervisor log.** A mistake in `config.yaml` makes Home Assistant skip the add-on
>    *silently* — no error in the store, just nothing shown. Look at **Settings → System → Logs**,
>    choose **Supervisor** from the dropdown (or run `ha supervisor logs` in the terminal), and
>    search for `personalfinance`. The reason it was rejected will be there.

---

## Step 3 — Configure it (this is where your settings go — no `.env` needed)

Open the add-on and click its **Configuration** tab. You'll see a few fields. Fill them in:

- **`simplefin_access_url`** — your SimpleFIN Bridge access URL. This is the one piece of secret
  information the app needs to download your accounts. Paste it here.
  *(If you only want to import an Empower CSV and don't use SimpleFIN yet, you can leave this empty
  and the app will just run in import-only mode.)*
- **`timezone`** — your local timezone, for example `America/New_York` or `Europe/Madrid`.
- **`daily_sync_hour`** — the hour of the day (a number from 0 to 23, in your local time) when the
  app should automatically fetch new data once per day. For example, `3` means 3:00 AM. It's once a
  day on purpose: SimpleFIN only allows about 24 requests per day.
- **`sync_max_age_hours`** — when the add-on starts up, only sync if the last sync was older than
  this many hours. The default (`12`) is fine; you can leave it.

Click **Save**.

That's it — you do **not** create or edit any `.env` file. These boxes are the Home Assistant
equivalent of `.env`.

---

## Step 4 — Start it and open the app

1. Go to the **Info** tab and click **Start**.
2. Click the **Log** tab and watch for these two lines, which mean it started correctly:
   - `Personal Finance running at http://0.0.0.0:8099`
   - `Serving behind Home Assistant ingress at /api/hassio_ingress/...`
3. Click **Open Web UI**. You'll also now find a **Finance** item in the left sidebar of Home
   Assistant that opens the app any time.

---

## Step 5 — Load your data

- **SimpleFIN:** click the **Sync** button inside the app (top of the page), or just wait for the
  automatic daily sync at the hour you set.
- **Empower history:** in the app go to **Settings → Empower import**, upload your Empower CSV
  file, then link the accounts. (The main `README` / `CLAUDE.md` explains the linking step.)

---

## Updating to a newer version later

1. Get the new files into `/addons/personalfinance` (over the share: `local_apps\personalfinance`):
   - If you used **Samba**: copy the new files over the old ones.
   - If you used **SSH**: pull the latest code and have Home Assistant rescan the folder:
     ```bash
     cd /addons/personalfinance && git pull
     ha addons reload
     ```
2. In the **App Store / Add-on Store**, click **⋮ → Check for updates**, open **Personal Finance**,
   and click **Rebuild**.

Your `/data` (database and imports) is left untouched by a rebuild, so you won't lose anything.

---

## Troubleshooting

- **The page is blank, or styles/buttons don't load inside the sidebar.** The ingress path wasn't
  picked up. Open the **Log** tab and check that it shows the `Serving behind ... ingress` line.
  Make sure `hassio_api: true` is present in `config.yaml`. As a fallback you can expose a direct
  port: add a `ports:` entry to `config.yaml` (for example `"8099/tcp": 8099`), rebuild, and open
  `http://<your-HA-IP>:8099` in a browser.
- **The build fails while compiling `numpy` or `pandas`, or with `apt-get: not found`.** The base
  image must be Debian (`python:3.13-slim`), not Alpine. On Home Assistant **2026.2+** the new
  **Apps** builder ignores `build.yaml` and tries to build on the Alpine `ghcr.io/home-assistant/base`
  image — which has no `apt-get` and would compile numpy/pandas from source. The `Dockerfile` guards
  against this by pinning `FROM python:3.13-slim` itself, so make sure you have the latest `Dockerfile`
  from this project (pull the newest files and **Rebuild**).
- **No "Local apps" / "Local add-ons" section appears.** Usually one of: the files aren't exactly at
  `/addons/personalfinance/config.yaml` (check for an extra nested folder); the store hasn't
  rescanned (run `ha addons reload` in the SSH terminal, then refresh); `config.yaml` has an error so
  Supervisor skipped it silently (check `ha supervisor logs` for `personalfinance`); or you're on a
  Container-only install with no store (see Plain Docker below).
- **"Not a valid repository" when adding the GitHub URL.** The **Add repository** button is only for
  multi-add-on repositories (those have a `repository.yaml` at the root). This is a single add-on, so
  that's expected — install it locally (Step 1) instead.

---

## Plain Docker (only if you don't have the Add-on Store)

If you're on "Home Assistant Container" or just want to run it as a normal Docker container, there's
no add-on store and no Configuration tab — so here you pass the settings as `-e` environment
variables on the command line instead:

```bash
docker build -t personalfinance .
docker run -d --name personalfinance -p 8050:8099 \
  -e SIMPLEFIN_ACCESS_URL="<your-simplefin-url>" \
  -e FINANCE_TIMEZONE="America/New_York" \
  -e FINANCE_DAILY_SYNC_HOUR=3 \
  -v /path/on/host/finance-data:/data \
  personalfinance
```

Then open `http://<host>:8050`. (There's no ingress here, so the app serves at the root path.)
