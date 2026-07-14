# Deploying on Another Machine

> **New to this project?** Start with `../README.md` for a file overview and folder structure.
>
> **Looking for day-to-day run commands** (single vs. multi-portfolio, paper vs. live)?
> See `RUNNING.md`. This file covers one-time installation/setup only.

## Files needed (copy this whole set to the new machine)

Since the Epic 17-18 restructure, this is an installable package — copy the whole repo (or at
minimum the items below), not individual flat files:
- `pyproject.toml` (package metadata/dependencies)
- `src/momentum_trading/` (the whole package: `core/`, `backtest/`, `execution/`, `risk/`,
  `interfaces/`, `daily_runner.py` — see `README.md`'s folder structure for what's where)
- `Dockerfile`
- `docker-compose.yml`
- `requirements.txt`
- `requirements-dev.txt` (adds pytest, for verifying the install — see below)
- `config.example.yaml` (copy to `config.yaml` and edit — do NOT commit `config.yaml` if it
  ever contains anything sensitive; keep secrets in env vars instead, per below)
- `tests/` (the test suite — see `docs/TESTING.md`)
- `docs/RUNNING.md` (day-to-day run commands, not needed for install itself but useful to have
  on the machine)
- `docs/TESTING.md` (how to run/interpret the test suite)

`risk_monitor.py` (`src/momentum_trading/risk/risk_monitor.py`, independent risk oversight —
see note below on true segregation) and `interfaces/notifications.py`/`email_commands.py`
(categorized email notifications + monthly report / optional remote commands — see
`EMAIL_REPORTING.md`/`EMAIL_COMMANDS.md`) are all included automatically as part of `src/`.

## One-time config setup (both platforms)

```bash
cp config.example.yaml config.yaml
# edit config.yaml: set your real portfolios, tickers, custom_weights, risk overrides

cp .env.example .env
# edit .env: API keys, SMTP/IMAP credentials, IBKR host/port, cron schedule -- only fill in
# the blocks for features you're actually using (see comments in .env.example for what's
# required vs. optional). Docker only reads .env; native installs use user env vars instead
# (see the platform-specific sections below).
```

Both `config.yaml` and `.env` are gitignored — never commit either if they contain anything
real. The `.env` examples throughout the rest of this file are shown inline for context on
each platform, but `.env.example` is the authoritative, up-to-date list of every variable the
app reads.

## Verifying the install (recommended before configuring anything else)

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

230 tests should pass cleanly. This only confirms code mechanics work on this machine
(dependencies installed correctly, no environment mismatch) — see `TESTING.md` for what the
suite does and doesn't validate, and how to interpret a failure if one occurs.

## Email alerting setup (both platforms — required for daily_runner.py alerts to work)

`daily_runner.py` sends email alerts on failures via SMTP. If unconfigured, alerts fall back
to a visible ERROR log line instead of failing silently — but you should set this up for real
unattended operation. `SMTP_PROVIDER` selects the auth mechanism; the rest of the required
variables depend on which provider you pick:

### Gmail (`SMTP_PROVIDER=gmail`, the default if unset)

```
SMTP_PROVIDER=gmail
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=youraddress@gmail.com
SMTP_PASS=your_app_password        # Gmail: use an App Password, not your real password
ALERT_TO_EMAIL=youraddress@gmail.com   # can be the same or different address
```

### Outlook.com / Hotmail / Microsoft 365 (`SMTP_PROVIDER=outlook`): OAuth2 required instead of SMTP_PASS

Microsoft has disabled basic authentication (username+password, including App Passwords) for
SMTP AUTH on these accounts. `server.login(user, password)` fails with:

```
535, b'5.7.139 Authentication unsuccessful, basic authentication is disabled. [...]'
```

no matter what's in `SMTP_PASS`. Set `SMTP_PROVIDER=outlook` to use OAuth2 (XOAUTH2) instead —
`core/smtp_auth.py` then requires `MS_OAUTH_CLIENT_ID` and ignores `SMTP_PASS` entirely (it isn't
read at all on this path, so there's no point setting it):

1. **Register an app** at [portal.azure.com](https://portal.azure.com) → *Azure Active
   Directory* → *App registrations* → *New registration*.
   - Name: anything, e.g. `momentum-trading-smtp`.
   - Supported account types: **"Personal Microsoft accounts only"** (for outlook.com/hotmail.com)
     or the multi-tenant option if this is a work/school account.
   - Redirect URI: leave blank — the device-code flow used here doesn't need one.
2. **Add the API permission**: *API permissions* → *Add a permission* → *APIs my organization
   uses* → search **"Office 365 Exchange Online"** → *Delegated permissions* → check **`SMTP.Send`**
   → *Add permissions*. (Personal accounts don't need admin consent for this.)
3. **Enable public client flows**: *Authentication* → under *Advanced settings*, set
   **"Allow public client flows"** to **Yes** → *Save*. (Required for the device-code flow;
   there's no client secret since this is a public client.)
4. **Copy the Application (client) ID** from the app's *Overview* page into `.env`:
   ```
   SMTP_PROVIDER=outlook
   SMTP_HOST=smtp-mail.outlook.com
   SMTP_PORT=587
   SMTP_USER=youraddress@outlook.com
   ALERT_TO_EMAIL=youraddress@outlook.com
   MS_OAUTH_CLIENT_ID=your-application-client-id-guid
   # MS_OAUTH_TENANT=consumers   # default; only override for a work/school tenant
   ```
5. **First run is interactive, once**: the first `send_alert_email()`/notification call logs a
   line like `To sign in, use a web browser to open https://microsoft.com/devicelogin and enter
   the code XXXXXXXX` — complete that in any browser, signed in as `SMTP_USER`. The resulting
   refresh token is cached in `data/ms_oauth_token_cache.json`, so subsequent runs (including the
   scheduled cron/Task Scheduler job) re-authenticate silently — no browser needed again unless
   that cache file is deleted or the token is revoked.

## Email-commanded remote actions setup (optional, opt-in — see EMAIL_COMMANDS.md)

Only needed if you want to PAUSE/RESUME/LIQUIDATE/etc. the bot via email. Skip this section if
you don't need remote commands. **Read `EMAIL_COMMANDS.md`'s security model before enabling.**

```
IMAP_HOST=imap.gmail.com
IMAP_USER=your-dedicated-bot-inbox@gmail.com   # recommend a DEDICATED inbox, not your primary email
IMAP_PASS=your_app_password
TRUSTED_SENDER_EMAIL=trader@yourdomain.com     # only commands from this exact address are ever parsed
```

---

## Linux / Mac

### Steps

1. **Install Docker** — Docker Desktop on Mac, `docker.io`/`docker-ce` on Linux.

2. **Set API keys** (if using FMP/EODHD) in a `.env` file next to `docker-compose.yml`:
   ```
   FMP_API_KEY=your_key_here
   EODHD_API_KEY=your_key_here
   SMTP_PROVIDER=gmail
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=youraddress@gmail.com
   SMTP_PASS=your_app_password
   ALERT_TO_EMAIL=youraddress@gmail.com
   # Outlook.com/Hotmail/Microsoft 365 instead -- see the OAuth2 section above:
   # SMTP_PROVIDER=outlook
   # MS_OAUTH_CLIENT_ID=your-application-client-id-guid

   # Cron schedule (Epic 20) -- optional, standard 5-field cron syntax, evaluated in
   # the container's TZ (America/New_York). Omit to use the defaults shown here.
   # DAILY_RUNNER_CRON=35 9 * * 1-5      # daily-runner: 9:35am ET, weekdays
   # RISK_MONITOR_CRON=0 9-16 * * 1-5    # risk_monitor.py: hourly, 9am-4pm ET, weekdays

   # Which portfolios get automated risk_monitor.py coverage (Epic 22) -- space-separated,
   # must match config.yaml's portfolios: key. config.yaml itself supports any number of
   # portfolios for daily-runner automatically; this is the one place you also need to
   # update if you add portfolios beyond the default single one.
   # RISK_MONITOR_PORTFOLIOS=portfolio1 portfolio2
   ```

3. **TWS/IB Gateway must run on the HOST machine**, not inside the container — IBKR's API
   isn't meant to be containerized itself. Start TWS/Gateway on the host, logged into paper
   or live as appropriate, with API access enabled (Configure > API > Settings > Enable
   ActiveX and Socket Clients).

4. **Build and start:**
   ```bash
   docker compose up -d --build
   ```

5. **Verify the cron schedule is running inside the container:**
   ```bash
   docker exec -it momentum-signal crontab -l
   docker logs -f momentum-signal
   ```
   To change the schedule later, edit `DAILY_RUNNER_CRON`/`RISK_MONITOR_CRON` in `.env` and run
   `docker compose up -d` — this only recreates the container (which regenerates the crontab
   from those env vars on start), it does **not** require `--build`.

6. **Check logs land in the mounted volume** (survives container restarts/rebuilds):
   ```bash
   ls ./logs/
   ls ./data/    # live_trades_log*.csv
   ```

7. **Test manually before trusting the schedule:**
   ```bash
   docker exec -it momentum-signal daily-runner --force-rebalance
   ```

---

## Windows

Two supported paths. **Path A (Docker Desktop)** matches the Linux/Mac setup exactly and is
recommended for consistency. **Path B (native Task Scheduler)** skips Docker entirely if you'd
rather run Python directly on Windows.

### Path A: Docker Desktop (recommended — same container as Linux/Mac)

1. **Install Docker Desktop for Windows** (requires WSL2 backend — Docker Desktop installer
   prompts for this automatically). Enable WSL2 integration during setup.

2. **Set API keys** in a `.env` file next to `docker-compose.yml` (same format as above).

3. **TWS/IB Gateway runs natively on Windows** (the host), not inside the container. Start it
   as usual, enable API access (Configure > API > Settings > Enable ActiveX and Socket Clients),
   and make sure "Allow connections from localhost only" is unchecked if Docker Desktop's
   internal networking needs to reach it — `docker-compose.yml` already defaults
   `IBKR_HOST=host.docker.internal`, which Docker Desktop resolves to the Windows host
   automatically (no `network_mode: host` override needed on Windows, unlike Linux).

4. **Build and start**, from PowerShell or Command Prompt in the project folder:
   ```powershell
   docker compose up -d --build
   ```

5. **Verify and test**, same commands as Linux/Mac:
   ```powershell
   docker exec -it momentum-signal crontab -l
   docker logs -f momentum-signal
   docker exec -it momentum-signal daily-runner --force-rebalance
   ```

6. **Logs/data** land in `.\logs\` and `.\data\` in the project folder (Docker Desktop maps
   the volumes to real Windows paths automatically).

### Path B: Native Windows, no Docker (Task Scheduler + venv)

1. **Install Python 3.12+** from python.org (check "Add python.exe to PATH" during install) —
   matches `pyproject.toml`'s `requires-python = ">=3.12"`.

2. **Create a virtual environment and install dependencies**, in PowerShell:
   ```powershell
   cd C:\path\to\momentum-trading
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   If PowerShell blocks the activation script, run once as Administrator:
   `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

3. **Set API keys as user environment variables** (persist across sessions):
   ```powershell
   [System.Environment]::SetEnvironmentVariable("FMP_API_KEY", "your_key_here", "User")
   [System.Environment]::SetEnvironmentVariable("EODHD_API_KEY", "your_key_here", "User")
   [System.Environment]::SetEnvironmentVariable("SMTP_PROVIDER", "gmail", "User")
   [System.Environment]::SetEnvironmentVariable("SMTP_HOST", "smtp.gmail.com", "User")
   [System.Environment]::SetEnvironmentVariable("SMTP_PORT", "587", "User")
   [System.Environment]::SetEnvironmentVariable("SMTP_USER", "youraddress@gmail.com", "User")
   [System.Environment]::SetEnvironmentVariable("SMTP_PASS", "your_app_password", "User")
   [System.Environment]::SetEnvironmentVariable("ALERT_TO_EMAIL", "youraddress@gmail.com", "User")
   # Outlook.com/Hotmail/Microsoft 365 instead -- see the OAuth2 section above:
   # [System.Environment]::SetEnvironmentVariable("SMTP_PROVIDER", "outlook", "User")
   # [System.Environment]::SetEnvironmentVariable("MS_OAUTH_CLIENT_ID", "your-application-client-id-guid", "User")
   ```
   Close and reopen PowerShell after setting these for them to take effect.

4. **TWS/IB Gateway runs on the same machine**, listening on `localhost` — no container
   networking translation needed, `127.0.0.1` works directly.

5. **Test manually first:**
   ```powershell
   .venv\Scripts\daily-runner.exe --force-rebalance
   ```

6. **Register the daily scheduled task** (run once, as Administrator):
   ```powershell
   schtasks /Create /TN "MomentumDailyRunner" /TR "C:\path\to\momentum-trading\.venv\Scripts\daily-runner.exe" /SC DAILY /ST 09:35 /RU SYSTEM
   ```
   Dry-run is the default with no arguments — there is no `--dry-run` flag (passing one is an
   error); add `--live` (and `--confirm-live-trading` for port 7496) only when you're ready to
   place real orders, per "Going live for real" below.
   Or via GUI: Task Scheduler → Create Task → General tab: run whether user is logged on or
   not → Triggers: New, Daily, 9:35 AM → Actions: New, Program/script =
   `C:\path\to\.venv\Scripts\daily-runner.exe`, Arguments = (leave blank for dry-run),
   Start in = `C:\path\to\momentum-trading`.

7. **Verify the task and check logs:**
   ```powershell
   schtasks /Query /TN "MomentumDailyRunner" /V /FO LIST
   Get-Content .\logs\daily_*.log -Tail 50
   ```
   (`daily_runner.py` writes its own dated log file per run, same as the cron example —
   redirect stdout/stderr explicitly in the scheduled task's arguments if you want a single
   combined log instead: append `>> logs\daily.log 2>&1` won't work directly in
   `schtasks /TR` — wrap the call in a `.bat` file instead if you need shell redirection:
   ```bat
   @echo off
   cd /d C:\path\to\momentum-trading
   .venv\Scripts\daily-runner.exe >> logs\daily_%date:~-4,4%%date:~-10,2%%date:~-7,2%.log 2>&1
   ```
   then point `schtasks /TR` at the `.bat` file instead of `python.exe` directly.)

### Path A vs. Path B — which to choose

- **Path A (Docker)** if you want identical behavior to your Linux/Mac deployment, easier
  dependency management, and you're already comfortable with Docker Desktop.
- **Path B (native)** if you want the simplest possible setup with no virtualization overhead,
  or Docker Desktop's WSL2 requirement is a blocker on that machine (e.g. Windows Home
  editions, or corporate policy restrictions on virtualization).

---

## Why containerize at all, vs. plain cron/Task Scheduler directly on the new machine

- **No "works on my machine" risk** — the container pins the exact Python version and
  dependency versions (`requirements.txt`), so you're not debugging a pandas/numpy version
  mismatch on the new box (the same class of bug that broke `fill_method='pad'` earlier).
- **Portable across OS** — the same container runs identically whether the new machine is
  Linux, Mac, or Windows with Docker Desktop; you don't maintain separate cron and Task
  Scheduler configs.
- **Isolated and restartable** — `restart: unless-stopped` means a machine reboot brings the
  scheduler back automatically; logs/trade history persist via the mounted volumes regardless.

## Independent risk oversight (risk_monitor.py)

`risk_monitor.py` is a deliberately separate, read-only script that watches trade logs and can
halt trading (via a flag file `daily_runner.py` respects) but cannot itself place orders --
segregation of duties, same principle real trading desks use so a bug in the trading logic
doesn't also blind the thing watching for it.

The `Dockerfile`'s default cron schedule runs it in the **same container** as `daily_runner.py`
for convenience -- this is a compromise, not full segregation. For genuine independence (a
monitor that keeps working even if the trading container itself is compromised or hangs), run
`risk_monitor.py` in a **separate container or separate host**, pointed at the same `data/`
volume (read-only mount recommended).

**Multi-portfolio coverage (Epic 22):** `config.yaml` supports any number of portfolios, and
`daily_runner.py` automatically rebalances all of them -- but `risk_monitor.py`'s Docker cron
entries are controlled separately, by `RISK_MONITOR_PORTFOLIOS` (space-separated names) in
`.env`. The default only covers `portfolio1`; if you add more portfolios to `config.yaml`,
add their names to `RISK_MONITOR_PORTFOLIOS` too, or they'll silently get no automated risk
oversight even though they're trading normally.

Manual/one-off invocation:

```bash
python -m momentum_trading.risk.risk_monitor --portfolio portfolio1 --max-loss-pct 0.25
```
`--initial-capital` is optional here — it defaults to `config.yaml`'s
`portfolios.portfolio1.total_value` if omitted; pass it explicitly to override.

Run this on its own schedule (e.g. hourly during market hours), independent of `daily_runner.py`'s
own cron entry. If it detects the loss threshold breached, it writes the same halt flag file
`daily_runner.py` checks -- clear it after review with:

```bash
daily-runner --resume-trading portfolio1
```

## Autostart on Reboot (Epic 11)

**Docker (Linux/Mac/Windows Docker Desktop):** already handled — `docker-compose.yml`'s
`restart: unless-stopped` (see above) automatically restarts the container, and its internal
cron, whenever the host machine reboots. No additional setup needed; this IS your autostart
mechanism if you're using the Docker path.

**Native Windows (no Docker):** the `schtasks` example in Section 2 (native install) schedules
a *daily time-based* trigger, which does not by itself guarantee the task exists/fires after a
reboot if the trigger time already passed. Add an explicit "At startup" trigger alongside the
daily one:
```powershell
schtasks /Create /TN "MomentumDailyRunnerStartup" /TR "C:\path\to\.venv\Scripts\daily-runner.exe" /SC ONSTART /RU SYSTEM
```
Or via GUI: Task Scheduler → your existing task → Triggers → New → "At startup" (in addition
to the existing daily time trigger) — the script's own `is_rebalance_day()` self-gating means
an extra startup-triggered run on a non-rebalance day is harmless (just checks and exits).

**Native Linux (no Docker), e.g. a bare-metal or VM host that reboots unpredictably:** prefer a
systemd service over cron, since systemd handles restart-on-failure and boot-time start more
robustly:
```ini
# /etc/systemd/system/momentum-runner.service
[Unit]
Description=Momentum Trading Daily Runner
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/momentum-trading
ExecStart=/path/to/.venv/bin/daily-runner
User=youruser
```
```ini
# /etc/systemd/system/momentum-runner.timer
[Unit]
Description=Run momentum-runner daily

[Timer]
OnCalendar=*-*-* 09:35:00
Persistent=true

[Install]
WantedBy=timers.target
```
```bash
sudo systemctl enable --now momentum-runner.timer
```
`Persistent=true` means a missed run (machine was off at 9:35am) fires as soon as the machine
is back up, which plain cron does not do automatically.

## Going live for real (not dry-run)

Dry-run is the default with no arguments at all — there is no `--dry-run` flag to remove.
Going live means *adding* `--live` (and `--confirm-live-trading` for port 7496).

**Docker path:** edit the cron line baked into the `Dockerfile` (or override via `docker exec`
for a one-off run) to add `--live` — and if trading real money on port 7496, also
`--confirm-live-trading`. Rebuild the image after changing the Dockerfile.

**Native Windows path:** edit the `schtasks /TR` argument (or the `.bat` file) the same way —
add `--live` (and `--confirm-live-trading` for port 7496) — then re-run the `schtasks /Create`
command, or edit the task directly in Task Scheduler's GUI.

