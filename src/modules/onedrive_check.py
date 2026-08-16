import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_TARGET_DIR = r"C:\Users\user\OneDrive - LIFEWORK\send@OneDrive2027"


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[onedrive_check] config.json is invalid; default onedrive_check settings used.")
        return {}


def _fmt_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "onedrive_check.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    config = _load_config(root).get("onedrive_check", {})
    target_dir = Path(config.get("path") or DEFAULT_TARGET_DIR)

    if not target_dir.exists():
        result = {
            "module": "onedrive_check",
            "generated_at": generated_at,
            "status": "error",
            "reason": f"Directory not found: {target_dir}",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[onedrive_check] directory not found: %s", target_dir)
        body = f"""
        <html>
          <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
            <h2>OneDriveフォルダ確認</h2>
            <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点</div>
            <div style="margin-top:8px;color:#b91c1c;font-weight:bold;">フォルダが見つかりませんでした: {target_dir}</div>
          </body>
        </html>
        """
    else:
        files = [p for p in target_dir.iterdir() if p.is_file()]
        file_count = len(files)
        total_bytes = sum(p.stat().st_size for p in files)
        has_files = file_count > 0

        result = {
            "module": "onedrive_check",
            "generated_at": generated_at,
            "status": "ok",
            "path": str(target_dir),
            "has_files": has_files,
            "file_count": file_count,
            "total_bytes": total_bytes,
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[onedrive_check] %d files, %s total", file_count, _fmt_size(total_bytes))

        presence_text = "ファイルあり" if has_files else "ファイルなし"
        presence_color = "#047857" if has_files else "#6b7280"
        body = f"""
        <html>
          <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
            <h2>OneDriveフォルダ確認</h2>
            <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / {target_dir}</div>
            <div style="margin-top:8px;padding:10px;background:#f8fafc;border-radius:8px;">
              <div style="font-weight:bold;color:{presence_color};">{presence_text}</div>
              <div style="margin-top:4px;">ファイル数: {file_count}件</div>
              <div style="margin-top:4px;">合計データ量: {_fmt_size(total_bytes)}</div>
            </div>
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
        logging.warning("[onedrive_check] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] OneDriveフォルダ確認 {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[onedrive_check] sent mail")
    except Exception as exc:
        logging.error("[onedrive_check] mail send failed: %s", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
