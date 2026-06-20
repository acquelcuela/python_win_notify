# NightlyBatchNotify Progress Notes

## Goal

Build `nightly_batch_notify_overview` under `src`.

The intended operation is:

- Windows Task Scheduler starts the batch at fixed times.
- The batch checks the schedule window itself.
- Phase 1 collects Nikkei 225 futures data, generates an HTML report, and sends it by Gmail.

## Initial Docker Direction

The first implementation used Docker Compose:

- `docker compose run --rm batch`
- Temporary container per scheduled run
- `python:3.11-slim` Linux image

This matched the original overview, but the PC environment was not suitable for Linux containers.

## Docker Investigation

Docker CLI and Compose were installed, and Docker Engine service was running.

Important findings:

- Docker API access from the Codex session was denied.
- In the user's `cmd`, Docker worked.
- `docker info --format "{{.OSType}}"` returned:

```text
windows
```

This means the local Docker Engine is configured for Windows containers, not Linux containers.

The Linux image failed with:

```text
no matching manifest for windows(10.0.19045)/amd64
```

## Windows Container Attempt

The Dockerfile was temporarily changed to use Windows containers.

Tried approaches:

- Official Python Windows image:

```dockerfile
FROM python:3.13-windowsservercore-ltsc2022
```

- Windows Server Core base image with Python installed during build:

```dockerfile
FROM mcr.microsoft.com/windows/servercore:ltsc2019
```

Reason for abandoning this path:

- Windows containers have strong host OS build dependencies.
- The host was detected as `windows(10.0.19045)/amd64`.
- Official Python Windows images did not match this environment cleanly.
- Windows Server Core images are heavy and less portable.
- This reduced the main benefit of Docker: reproducible execution across environments.

Conclusion:

Windows containers are not a good fit for this batch on this PC.

## Final Direction

The project was switched to:

```text
Windows Python + src\.venv
```

Reasons:

- No Docker daemon dependency.
- No Windows container OS-version dependency.
- Python dependencies are isolated inside `src\.venv`.
- Task Scheduler can call a simple `.bat` file.
- This is easier to operate on this PC.

## Current File State

Docker files were removed from `src`:

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`

Current important files:

- `main.py`
- `requirements.txt`
- `.env`
- `.env.example`
- `run.bat`
- `setup_windows.bat`
- `run_windows_python.bat`
- `modules/stock_nikkei.py`
- `modules/report_html.py`
- `modules/mail_gmail.py`

## Current Operation

Initial setup:

```cmd
cd /d "C:\Users\user\OneDrive - LIFEWORK\data@OneDrive\kaeru\ai_other_cli_2026\ai_agent_win_notify_cron\src"
setup_windows.bat
```

Manual forced test:

```cmd
.venv\Scripts\python.exe main.py --force
```

Scheduled execution:

```text
C:\Users\user\OneDrive - LIFEWORK\data@OneDrive\kaeru\ai_other_cli_2026\ai_agent_win_notify_cron\src\run.bat
```

`run.bat` expects `src\.venv` to exist.

## Environment File

`.env` was created for local testing.

Current safe test setting:

```json
"modules": {
  "mail_gmail": false
}
```

This prevents accidental Gmail sending during tests.

Current mail delivery setting:

```json
"modules": {
  "mail_gmail": true
}
```

Gmail credentials remain in `.env`.

## Verification Done

Dependency installation succeeded:

- `yfinance`
- `python-dotenv`
- `pandas`
- related dependencies

Forced run completed:

```cmd
.venv\Scripts\python.exe main.py --force
```

The batch completed and generated:

- `output/report.html`
- `logs/batch.log`

## Remaining Issue

`yfinance` cannot currently reach Yahoo:

```text
Failed to connect to fc.yahoo.com port 443
```

This appears to be a network, firewall, proxy, or endpoint access issue rather than a Python dependency issue.

The batch still completes:

- `stock_nikkei.json` records the fetch error.
- `report_html` still generates a report.
- `mail_gmail` is currently disabled.

## Fix Already Applied

`yfinance` tried to write cache files under:

```text
C:\Users\user\AppData\Local\py-yfinance
```

That caused access-denied warnings.

The code was changed to use:

```text
src\.cache\yfinance
```

This keeps runtime cache inside the project directory.

## Completed Setup Notes

Yahoo Finance access, Gmail delivery, and scheduled-task kicks were verified
during setup. Current non-secret settings now live in `config.json`.

## Configuration Split

Settings were later split to make AI-assisted edits safer:

- `config.json` stores non-secret runtime settings such as schedules and module flags.
- `.env` stores only Gmail credentials and addresses.

This keeps secrets out of git while allowing normal behavior changes to be
committed and reviewed.

## Intentional Spec Changes After Initial Draft

The original markdown drafts used `.env` `MODULE_*` values for module ON/OFF
settings. After implementation, this was changed intentionally:

- Module flags are now stored in `config.json`.
- `.env` is kept for secrets only, especially Gmail credentials.
- This makes normal behavior changes reviewable in git and safer for AI-assisted
  edits because secrets are not mixed with runtime switches.

The original drafts also mentioned a `06:30` run time. After scheduler setup and
manual operation checks, the active windows were changed to:

- `00:00-00:14`
- `07:00-07:14`

The task itself still starts every 15 minutes. `main.py` decides whether the
current start is inside an active window. This keeps Task Scheduler simple while
preventing duplicate sends through `state/run_history.json`.

`stock_nikkei` was implemented first, but Yahoo Finance / yfinance can return no
data or fail depending on network conditions, Yahoo endpoint behavior, ticker
availability, or local firewall/proxy state. This is treated as an expected
runtime failure mode:

- `stock_nikkei.json` is still written with `status=error`.
- `report_html` still generates a report and shows the fetch failure.
- The batch can continue to Gmail/report generation instead of crashing on a
  market data outage.

## Current Next Module

The next useful data module is `stock_dividend`.

Purpose:

- Track RYLD and SDIV dividend timing.
- Estimate the current action phase from the next ex-dividend date.
- Add the result to the HTML report so the mail says whether the period is
  `buy_window`, `buy_now`, `hold`, `sell_start`, `sell_now`, `neutral`, or
  `unknown`.

The first version should use yfinance when possible and degrade to `unknown`
when the next ex-dividend date is not available.

## Dividend Schedule Fallback

After the first mail test, yfinance could not connect to Yahoo Finance from the
batch environment. Gmail delivery and HTML generation were verified, but market
data retrieval was not reliable enough to be the only source for RYLD/SDIV
timing.

The dividend module was therefore changed to use a configured schedule first:

- `config.json` `dividend_schedule.targets` stores the next ex-dividend date.
- The batch still attempts yfinance for price/yield details.
- If yfinance fails, the configured date is still used to calculate the phase.
- The report shows the date source and confidence so estimated dates do not look
  like silently verified market data.

Current seed dates were researched from public pages:

- Global X fund pages confirm monthly distributions for RYLD and SDIV.
- Dividend.com history showed RYLD distributions through `2026-05-18`.
- Dividend.com history showed SDIV distributions through `2026-06-03`.

The configured future dates should be reviewed and updated before each target
month once Global X or another reliable source publishes the next distribution
calendar.

## Watchlist Module

`stock_watchlist` was added after the dividend timing module.

Purpose:

- Track configured tickers from `config.json` `watchlist.tickers`.
- Fetch the latest daily OHLCV data with yfinance.
- Calculate the change and percentage change from the previous trading day.
- Sort the report by `change_pct` descending so the strongest names appear
  first.
- Continue when individual tickers fail and show those failures as warnings in
  the HTML report.
- Display watchlist results separately for Japanese stocks and US stocks.
- Prefer configured Japanese/Katakana names over yfinance English names so the
  report is easier to scan in mail.

The first configured list is:

- `7203.T`
- `9983.T`
- `8035.T`
- `1570.T`
- `AAPL`
- `NVDA`
