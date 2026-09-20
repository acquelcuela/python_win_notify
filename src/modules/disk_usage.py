import ctypes
import html
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")


def _list_drive_letters() -> list[str]:
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{chr(ord('A') + i)}:\\" for i in range(26) if bitmask & (1 << i)]


def _fmt_gb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GB"


def _collect_disk_stats() -> list[dict]:
    stats = []
    for drive in _list_drive_letters():
        try:
            usage = shutil.disk_usage(drive)
        except OSError:
            # Unmounted/empty removable drives (e.g. an empty card reader slot).
            continue
        free_pct = (usage.free / usage.total * 100) if usage.total else 0.0
        stats.append(
            {
                "drive": drive,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "free_pct": free_pct,
            }
        )
    return stats


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "disk_usage.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    stats = _collect_disk_stats()

    result = {
        "module": "disk_usage",
        "generated_at": generated_at,
        "status": "ok",
        "drives": stats,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info(
        "[disk_usage] %s",
        ", ".join(f"{s['drive']} {s['free_pct']:.1f}% free" for s in stats) or "no drives found",
    )

    rows = []
    for s in stats:
        free_pct = s["free_pct"]
        color = "#b91c1c" if free_pct < 10 else ("#ca8a04" if free_pct < 20 else "#047857")
        rows.append(
            "<tr>"
            f'<td style="padding:4px 10px;font-weight:bold;">{html.escape(s["drive"])}</td>'
            f'<td style="padding:4px 10px;">{_fmt_gb(s["total_bytes"])}</td>'
            f'<td style="padding:4px 10px;">{_fmt_gb(s["used_bytes"])}</td>'
            f'<td style="padding:4px 10px;">{_fmt_gb(s["free_bytes"])}</td>'
            f'<td style="padding:4px 10px;color:{color};font-weight:bold;">{free_pct:.1f}%</td>'
            "</tr>"
        )
    table_rows = "".join(rows) if rows else '<tr><td colspan="5" style="padding:8px;">ドライブが見つかりませんでした</td></tr>'

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>ディスク容量チェック</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点</div>
        <table style="margin-top:8px;border-collapse:collapse;">
          <thead>
            <tr style="background:#f1f5f9;">
              <th style="padding:4px 10px;text-align:left;">ドライブ</th>
              <th style="padding:4px 10px;text-align:left;">総容量</th>
              <th style="padding:4px 10px;text-align:left;">使用量</th>
              <th style="padding:4px 10px;text-align:left;">空き容量</th>
              <th style="padding:4px 10px;text-align:left;">空き率</th>
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
        logging.warning("[disk_usage] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"ディスク容量チェック {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[disk_usage] sent mail")
    except Exception as exc:
        logging.error("[disk_usage] mail send failed: %s", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
