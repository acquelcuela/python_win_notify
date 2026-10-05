import html
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
CLI_TIMEOUT_SECONDS = 120

# `claude -p "/usage"` answers locally with the same text the interactive
# /usage screen shows, e.g.
#   Current session: 15% used · resets Oct 2, 7:40pm (Asia/Tokyo)
#   Current week (all models): 17% used · resets Oct 7, 6am (Asia/Tokyo)
USAGE_LINE_PATTERNS = {
    "session": re.compile(r"Current session:\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets\s*(.+))?"),
    "week": re.compile(r"Current week \(all models\):\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets\s*(.+))?"),
}
LABELS = {
    "session": "現在のセッション",
    "week": "週次(全モデル)",
}


def _find_claude_cli() -> str | None:
    # Task Scheduler runs may not have the npm global bin on PATH.
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path(os.getenv("APPDATA", "")) / "npm" / "claude.cmd"
    return str(fallback) if fallback.exists() else None


def _fetch_usage_text(root: Path) -> str:
    cli = _find_claude_cli()
    if not cli:
        raise RuntimeError("claude CLI not found on PATH or in %APPDATA%\\npm.")
    completed = subprocess.run(
        [cli, "-p", "/usage", "--no-session-persistence"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CLI_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"claude exited {completed.returncode}: {completed.stderr.strip()[:300]}")
    return completed.stdout


def _parse_usage(text: str) -> dict:
    result = {}
    for key, pattern in USAGE_LINE_PATTERNS.items():
        match = pattern.search(text)
        if match:
            result[key] = {
                "used_pct": float(match.group(1)),
                "resets": (match.group(2) or "").strip() or None,
            }
    return result


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "claude_usage.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    try:
        usage = _parse_usage(_fetch_usage_text(root))
        error = None if usage else "Could not find usage lines in `claude -p /usage` output."
    except Exception as exc:
        usage = {}
        error = str(exc)

    result = {
        "module": "claude_usage",
        "generated_at": generated_at,
        "status": "ok" if not error else "error",
        "usage": usage,
    }
    if error:
        result["error"] = error
        logging.error("[claude_usage] %s", error)
    else:
        logging.info(
            "[claude_usage] %s",
            ", ".join(f"{key} {value['used_pct']:.0f}%" for key, value in usage.items()),
        )
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for key, label in LABELS.items():
        value = usage.get(key)
        if not value:
            continue
        used_pct = value["used_pct"]
        color = "#b91c1c" if used_pct >= 80 else ("#ca8a04" if used_pct >= 50 else "#047857")
        rows.append(
            "<tr>"
            f'<td style="padding:4px 10px;font-weight:bold;">{label}</td>'
            f'<td style="padding:4px 10px;color:{color};font-weight:bold;">{used_pct:.0f}%</td>'
            f'<td style="padding:4px 10px;">{html.escape(value["resets"] or "-")}</td>'
            "</tr>"
        )
    table_rows = (
        "".join(rows)
        if rows
        else f'<tr><td colspan="3" style="padding:8px;">取得に失敗しました: {html.escape(error or "")}</td></tr>'
    )

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>Claude 使用状況</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点(claude /usage より)</div>
        <table style="margin-top:8px;border-collapse:collapse;">
          <thead>
            <tr style="background:#f1f5f9;">
              <th style="padding:4px 10px;text-align:left;">区分</th>
              <th style="padding:4px 10px;text-align:left;">使用率</th>
              <th style="padding:4px 10px;text-align:left;">リセット</th>
            </tr>
          </thead>
          <tbody>
            {table_rows}
          </tbody>
        </table>
      </body>
    </html>
    """

    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    missing = [
        name
        for name, value in [
            ("GMAIL_ADDRESS", gmail_address),
            ("GMAIL_APP_PASSWORD", app_password),
            ("MAIL_TO", mail_to),
        ]
        if not value
    ]
    if missing:
        logging.warning("[claude_usage] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"Claude 使用状況 {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[claude_usage] sent mail")
    except Exception as exc:
        logging.error("[claude_usage] mail send failed: %s", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
