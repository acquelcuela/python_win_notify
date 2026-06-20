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
