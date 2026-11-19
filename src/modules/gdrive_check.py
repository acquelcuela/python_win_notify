"""Checks that the Google Drive for desktop virtual drive (G: by default) is
mounted, and mails the result.

Background (2026-11-19): a Drive auto-update replaced its filesystem driver,
which unmounted G:. The old GoogleDriveFS.exe kept running without remounting,
so the app looked alive while G: was gone. Fix was to quit GoogleDriveFS.exe
and start the newest version. This check catches that state.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from modules.local_config import load_config
from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_DRIVE = "G:"
LIST_TIMEOUT_SECONDS = 60


def _drivefs_processes() -> list[str]:
    """Returns the exe paths of running GoogleDriveFS.exe processes."""
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-Process GoogleDriveFS -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        logging.warning("[gdrive_check] process lookup failed: %s", exc)
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _list_root(root_path: Path) -> tuple[list[str] | None, str]:
    """Lists the drive root in a child process so a hung drive can't block the batch."""
    script = (
        "import json,os,sys;"
        "print(json.dumps(sorted(os.listdir(sys.argv[1])), ensure_ascii=False))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, str(root_path)],
            capture_output=True,
            timeout=LIST_TIMEOUT_SECONDS,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return None, f"{root_path} の一覧取得が{LIST_TIMEOUT_SECONDS}秒で終わりませんでした(応答なし)"
    except Exception as exc:
        return None, f"一覧取得に失敗: {exc}"
    if completed.returncode != 0:
        err = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return None, err[-1] if err else f"exit code {completed.returncode}"
    return json.loads(completed.stdout.decode("utf-8")), ""


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "gdrive_check.json"
    now = datetime.now(JST)

    config = load_config(root).get("gdrive_check", {})
    drive = str(config.get("drive") or DEFAULT_DRIVE).rstrip("\\/")
    root_path = Path(drive + "\\")

    processes = _drivefs_processes()
    entries: list[str] | None = None
    reason = ""
    if not root_path.exists():
        reason = f"{drive} ドライブがありません"
    else:
        entries, reason = _list_root(root_path)

    ok = entries is not None and len(entries) > 0
    if entries is not None and not entries:
        reason = f"{drive} は見えますが中身が空です"

    result = {
        "module": "gdrive_check",
        "generated_at": now.isoformat(),
        "status": "ok" if ok else "ng",
        "drive": drive,
        "reason": reason,
        "entries": entries or [],
        "drivefs_processes": processes,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if ok:
        logging.info("[gdrive_check] %s OK (%s)", drive, ", ".join(entries))
    else:
        logging.error("[gdrive_check] %s NG: %s", drive, reason)

    if processes:
        versions = sorted({Path(p).parent.name for p in processes})
        process_text = f"起動中 ({len(processes)}プロセス / バージョン {', '.join(versions)})"
    else:
        process_text = "起動していません"

    if ok:
        status_html = f'<div style="font-weight:bold;color:#047857;">OK: {escape(drive)} はつながっています</div>'
        detail_html = f'<div style="margin-top:4px;">中身: {escape(" / ".join(entries))}</div>'
        hint_html = ""
    else:
        status_html = f'<div style="font-weight:bold;color:#b91c1c;">NG: {escape(reason)}</div>'
        detail_html = ""
        hint_html = """
            <div style="margin-top:12px;font-size:13px;">
              直し方: タスクトレイの Google Drive を終了して起動し直す。<br>
              だめなら PowerShell で <code>Stop-Process -Name GoogleDriveFS -Force</code> のあと、
              <code>C:\\Program Files\\Google\\Drive File Stream\\</code> の一番新しいバージョンの GoogleDriveFS.exe を起動。
              それでもだめなら PC を再起動。
            </div>
        """

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>Google Drive ({escape(drive)}) 確認</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点</div>
        <div style="margin-top:8px;padding:10px;background:#f8fafc;border-radius:8px;">
          {status_html}
          {detail_html}
          <div style="margin-top:4px;">GoogleDriveFS: {escape(process_text)}</div>
        </div>
        {hint_html}
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
        logging.warning("[gdrive_check] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    mark = "OK" if ok else "【NG】"
    subject = f"Google Drive {drive} 確認 {mark} {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[gdrive_check] sent mail")
    except Exception as exc:
        logging.error("[gdrive_check] mail send failed: %s", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
