# NightlyBatchNotify Specification

## Overview

NightlyBatchNotify is a Windows scheduled batch that collects Nikkei 225 futures
data, generates an HTML report, and sends it by Gmail.

The current implementation does not use Docker. It runs with Windows Python in
`src\.venv`.

## Runtime Model

The task scheduler starts `run.bat` every 15 minutes.

`main.py` then decides whether to actually process the batch:

- Active windows are configured by `config.json`.
- The allowed delay is configured by `config.json`.
- Starts outside the active windows are logged and skipped.
- Completed schedule windows are recorded to prevent duplicate sends.

Recommended `config.json` settings:

```json
{
  "batch_schedule": ["00:00", "07:00"],
  "batch_window_minutes": 14
}
```

With this setting:

- `00:00-00:14` runs the midnight batch.
- `07:00-07:14` runs the morning batch.
- Other 15-minute starts only log a skip.

## File Layout

```text
src\
  main.py
  requirements.txt
  setup_windows.bat
  run.bat
  README.md
  config.json
  .env.example
  .env                  # local only, ignored by git
  .venv\                # local only, ignored by git
  .cache\               # local only, ignored by git
  logs\                 # local only, ignored by git
  output\               # local only, ignored by git
  state\                # local only, ignored by git
  modules\
    __init__.py
    stock_nikkei.py
    stock_watchlist.py
    stock_dividend.py
    report_html.py
    mail_gmail.py
  scheduler\
    install_scheduled_task.bat
    install_scheduled_task.ps1
    uninstall_scheduled_task.bat
    uninstall_scheduled_task.ps1
    check_scheduled_task.bat
    check_scheduled_task.ps1
  docs\
    specification.md
    progress_notes.md
```

## Core Files

`main.py`

Batch runner. It loads `.env`, checks the schedule window, prevents duplicate
runs, acquires a lock file, and executes enabled modules in order.

`requirements.txt`

Python dependencies:

- `yfinance`
- `python-dotenv`

`setup_windows.bat`

Creates `src\.venv` and installs dependencies from `requirements.txt`.

`run.bat`

Entry point for Windows Task Scheduler. It writes a task-kick log and then runs
`main.py` with the virtual environment Python.

## Modules

`modules\stock_nikkei.py`

Fetches Nikkei 225 futures data from Yahoo Finance with `yfinance`.

Output:

```text
output\stock_nikkei.json
```

`modules\report_html.py`

Reads module output files and generates the HTML report.

Output:

```text
output\report.html
```

`modules\stock_watchlist.py`

Fetches configured watchlist tickers with yfinance and calculates each ticker's
latest daily change.

Output:

```text
output\stock_watchlist.json
```

`modules\stock_dividend.py`

Tracks RYLD and SDIV dividend timing with yfinance where possible. It estimates
the action phase from the next ex-dividend date and degrades to `unknown` when
the date is not available.

Output:

```text
output\stock_dividend.json
```

`modules\mail_gmail.py`

Sends `output\report.html` through Gmail SMTP over SSL.

Required `.env` keys:

```env
GMAIL_ADDRESS=...
GMAIL_APP_PASSWORD=...
MAIL_TO=...
```

The Gmail password must be a Google app password, not the normal account
password.

## Module Order

`main.py` runs modules in this order:

```text
stock_nikkei
stock_watchlist
stock_sector
stock_dividend
report_html
mail_gmail
```

Currently implemented:

- `stock_nikkei`
- `stock_watchlist`
- `stock_dividend`
- `report_html`
- `mail_gmail`

Phase 2 placeholders:

- `stock_sector`

Unimplemented modules are skipped if enabled accidentally.

## Configuration

Runtime settings are stored in `config.json`. This file is safe to edit with AI
and commit to git.

Current structure:

```json
{
  "batch_schedule": ["00:00", "07:00"],
  "batch_window_minutes": 14,
  "modules": {
    "stock_nikkei": true,
    "stock_watchlist": true,
    "stock_sector": false,
    "stock_dividend": true,
    "report_html": true,
    "mail_gmail": true
  },
  "watchlist": {
    "tickers": [
      {"ticker": "7203.T", "name": "Toyota Motor"},
      {"ticker": "9983.T", "name": "Fast Retailing"},
      {"ticker": "8035.T", "name": "Tokyo Electron"},
      {"ticker": "1570.T", "name": "Nikkei 225 Leveraged ETF"},
      {"ticker": "AAPL", "name": "Apple"},
      {"ticker": "NVDA", "name": "NVIDIA"}
    ]
  },
  "dividend_schedule": {
    "targets": {
      "RYLD": {
        "ex_dividend_date": "2026-06-22",
        "date_confidence": "estimated_from_recent_history"
      },
      "SDIV": {
        "ex_dividend_date": "2026-07-02",
        "date_confidence": "estimated_from_recent_history"
      }
    }
  }
}
```

Secrets are stored in `.env`.

Use `.env.example` as the template for `.env`.

`.env` keys:

```env
GMAIL_ADDRESS=your@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
MAIL_TO=your@gmail.com
```

`.env` is ignored by git because it contains secrets.

## Logging

There are two daily log files.

Task-kick log:

```text
logs\task_runner_YYYYMMDD.log
```

This records every `run.bat` start and finish, even when the batch skips because
it is outside the active window.

Batch log:

```text
logs\batch_YYYYMMDD.log
```

This records Python-side processing details, module starts/completions, skips,
errors, and duplicate-run prevention.

## Duplicate Run Prevention

Completed schedule windows are recorded in:

```text
state\run_history.json
```

The run key format is:

```text
YYYY-MM-DD HH:mm
```

Example:

```text
2026-06-19 07:00
```

If the same schedule window is reached again, `main.py` skips it.

Overlapping execution is prevented by:

```text
state\batch.lock
```

If a stale lock is older than 30 minutes, it is removed automatically.

`--force` skips schedule and run-history checks for manual testing.

## Scheduler Scripts

Register task:

```text
scheduler\install_scheduled_task.bat
```

Remove task:

```text
scheduler\uninstall_scheduled_task.bat
```

Check task:

```text
scheduler\check_scheduled_task.bat
```

The install script creates a task named:

```text
NightlyBatchNotify
```

The task runs every 15 minutes and calls:

```text
src\run.bat
```

The task is registered with one daily calendar trigger. It starts at `00:00`,
repeats every 15 minutes for one day, and repeats again the next day. Python
still decides whether the batch should process or skip based on `config.json`.

## Manual Commands

Create or update the virtual environment:

```cmd
cd /d "C:\Users\user\OneDrive - LIFEWORK\data@OneDrive\kaeru\ai_other_cli_2026\ai_agent_win_notify_cron\src"
setup_windows.bat
```

Manual test without schedule checks:

```cmd
.venv\Scripts\python.exe main.py --force
```

Manual scheduler-style run:

```cmd
run.bat
```

Check registered task from PowerShell:

```powershell
Get-ScheduledTask -TaskName NightlyBatchNotify
Get-ScheduledTaskInfo -TaskName NightlyBatchNotify
```

## Git Policy

The repository `.gitignore` excludes runtime and local-secret files:

- `src\.env`
- `src\.venv\`
- `src\.cache\`
- `src\logs\`
- `src\output\`
- `src\state\`
- Python `__pycache__`

Commit source files, scripts, docs, `config.json`, and `.env.example`.
