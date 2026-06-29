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
  "batch_schedule": ["07:00", "09:30", "12:15", "22:45"],
  "batch_window_minutes": 14
}
```

With this setting:

- `07:00-07:14` checks the final overnight futures result before the Japanese
  market opens.
- `09:30-09:44` checks early Tokyo-market news and opening-session movement.
- `12:15-12:29` checks morning-session Japanese stock news and movement.
- `22:45-22:59` checks evening news and futures shortly after the US market
  opens during daylight saving time.
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

Fetches market overview data from Yahoo Finance with `yfinance`.

Current targets:

- Nikkei 225 futures: `NKD=F`
- Nikkei average: `^N225`
- TOPIX proxy: TOPIX-linked ETF `1306.T`

The normal market comparison uses Nikkei average versus TOPIX proxy. Nikkei 225
futures are treated as supplemental context and compared with the Nikkei average,
because futures can include overnight movement.

Output:

```text
output\stock_nikkei.json
```

`modules\report_html.py`

Reads module output files and generates the HTML report.

The HTML mail is optimized for smartphone mail apps. Main display rules:

- Show `日経平均 / TOPIX 市場概況` first.
- In the market section, show cards in this order: Nikkei average, Nikkei 225
  futures, TOPIX-linked ETF.
- Show AI summaries after the market numbers.
- Show `news_movers` matches as `ニュースに出た銘柄`, including both rising
  and falling stocks.
- Show watchlist cards in two columns, with previous-day, two-trading-day, and
  three-trading-day changes colored green/red.

Output:

```text
output\report.html
```

`modules\stock_watchlist.py`

Fetches configured watchlist tickers with yfinance and calculates each ticker's
latest daily change. The module requests a 10-day daily history per ticker and
derives these values from the same response:

- Previous trading-day change
- Two-trading-day change
- Three-trading-day change

The additional multi-day values do not add more yfinance calls.

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

`modules\market_news.py`

Fetches Japanese stock-related headlines through Google News RSS search groups.

Output:

```text
output\market_news.json
```

`modules\news_movers.py`

Matches `output\market_news.json` headlines against `src\data\data_j.csv`
company names and `src\data\data_j_aliases.json` aliases, converts the CSV to
`src\.cache\listed_companies.json` at runtime, fetches matched ticker prices
with yfinance, and writes matched movers.

`src\data\data_j.csv` is a committed project dictionary file. The converted
`src\.cache\listed_companies.json` file is runtime cache and is not committed.
`src\data\data_j_aliases.json` is a committed local supplement for abbreviations.
Matching intentionally uses simple company-name or alias containment without
spelling-variation correction. Very short company names are skipped by
`news_movers.min_name_length`, and a basic text-boundary check reduces false
matches inside longer company names.

Output:

```text
output\news_movers.json
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
market_news
news_movers
ai_summary
report_html
mail_gmail
```

Currently implemented:

- `stock_nikkei`
- `stock_watchlist`
- `stock_dividend`
- `market_news`
- `news_movers`
- `ai_summary`
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
  "batch_schedule": ["07:00", "09:30", "12:15", "22:45"],
  "batch_window_minutes": 14,
  "modules": {
    "stock_nikkei": true,
    "stock_watchlist": true,
    "stock_sector": false,
    "stock_dividend": true,
    "market_news": true,
    "news_movers": true,
    "ai_summary": true,
    "report_html": true,
    "mail_gmail": true
  },
  "ai_summary": {
    "provider": "gemini",
    "model": "gemini-3.1-flash-lite"
  },
  "market_news": {
    "sources": [
      {
        "name": "Google News",
        "queries": ["日本株 ニュース"]
      },
      {
        "name": "株探",
        "queries": ["site:kabutan.jp 日本株 ニュース"]
      },
      {
        "name": "Reuters",
        "queries": ["site:jp.reuters.com 日本株 東京市場"]
      }
    ],
    "queries": [
      "日本株 ニュース",
      "東京株式市場 ニュース",
      "東証 銘柄 材料",
      "日本株 セクター 業種",
      "日本株 レーティング 目標株価"
    ],
    "max_items": 24,
    "per_source_limit": 8,
    "lookback_hours": 18,
    "exclude_title_keywords": [
      "米国株個別",
      "米国株",
      "ダウ先物",
      "ＮＹ株",
      "NY株"
    ]
  },
  "news_movers": {
    "data_file": "data/data_j.csv",
    "alias_file": "data/data_j_aliases.json",
    "max_tickers": 12,
    "min_name_length": 4
  },
  "watchlist": {
    "tickers": [
      {"ticker": "8035.T", "name": "東京エレクトロン"},
      {"ticker": "1570.T", "name": "NEXT FUNDS 日経平均レバレッジ"},
      {"ticker": "3436.T", "name": "SUMCO"},
      {"ticker": "200A.T", "name": "NF日経半導体株"},
      {"ticker": "7762.T", "name": "シチズン時計"},
      {"ticker": "6976.T", "name": "太陽誘電"},
      {"ticker": "5016.T", "name": "JX金属"},
      {"ticker": "5401.T", "name": "日本製鉄"},
      {"ticker": "6613.T", "name": "QDレーザ"},
      {"ticker": "7013.T", "name": "IHI"},
      {"ticker": "5803.T", "name": "フジクラ"},
      {"ticker": "AAPL", "name": "アップル"},
      {"ticker": "NVDA", "name": "エヌビディア"}
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
GEMINI_API_KEY=your_gemini_api_key
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
