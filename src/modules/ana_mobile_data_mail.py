"""Mails ANA Mobile's remaining data every night (result of ana_mobile_data)."""
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail

JST = timezone(timedelta(hours=9), "JST")
FONT = "font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;"


def _gb(value) -> str:
    return f"{value:.2f}GB" if isinstance(value, (int, float)) else "-"


def _build(data: dict, previous: dict | None, now: datetime) -> tuple[str, str]:
    info = data.get("data") or {}
    if not info.get("ok"):
        body = (
            f'<div style="padding:8px 12px;border-left:6px solid #b91c1c;background:#fef2f2;">'
            f'<div style="font-weight:bold;">⚠ データ残量を読み取れませんでした</div>'
            f'<div>{html.escape(str(info.get("error") or "不明なエラー"))}</div>'
            f'<div style="margin-top:4px;">直し方: このPCのChromeで {html.escape("https://anamobile.ana.co.jp/member")} '
            f'を開き、ログインし直してください(Claude in Chrome の拡張機能がつながっていることも確認してください)。</div></div>'
        )
        return "⚠ ANAモバイル: データ残量を読み取れませんでした", body

    total = info["total_remaining_gb"]
    rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;">{html.escape(str(b.get("label")))}</td>'
        f'<td style="text-align:right;">{_gb(b.get("gb"))}</td></tr>'
        for b in info.get("by_expiry") or []
    )
    change = ""
    if previous and isinstance(previous.get("total_remaining_gb"), (int, float)):
        used = previous["total_remaining_gb"] - total
        if used >= 0:
            change = f"{previous['date']} から {used:.2f}GB 使いました。"
        else:
            change = f"{previous['date']} から {-used:.2f}GB 増えました(月替わりの追加またはチャージ)。"
    this_month = next((b.get("gb") for b in info.get("by_expiry") or [] if b.get("label") == "今月"), None)
    expire_note = ""
    if isinstance(this_month, (int, float)) and this_month > 0:
        expire_note = f'<p style="color:#b45309;">今月末に失効する分: {_gb(this_month)}</p>'
    speed = info.get("high_speed")
    speed_note = f'<p style="color:#b91c1c;">高速通信が {html.escape(speed)} になっています。</p>' if speed == "OFF" else ""
    body = (
        f'<p style="font-size:20px;margin:4px 0;">残り <b>{_gb(total)}</b>'
        f'<span style="color:#6b7280;font-size:14px;">(契約 {_gb(info.get("plan_gb"))}/月)</span></p>'
        f'<p>{html.escape(change)}</p>{expire_note}{speed_note}'
        f'<table style="border-collapse:collapse;margin-top:8px;"><tr><th colspan="2" style="text-align:left;">有効期限ごと</th></tr>'
        f'{rows}</table>'
    )
    return f"ANAモバイル残量 {_gb(total)}", body


def run(root: Path) -> None:
    now = datetime.now(JST)
    try:
        data = json.loads((root / "output" / "ana_mobile_data.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.info("[ana_mobile_data_mail] skipped: no ana_mobile_data.json")
        return
    if not str(data.get("generated_at", "")).startswith(now.strftime("%Y-%m-%d")):
        logging.info("[ana_mobile_data_mail] skipped: ana_mobile_data.json is not from today")
        return
    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        logging.warning("[ana_mobile_data_mail] mail skipped: missing Gmail settings")
        return
    subject, inner = _build(data, data.get("previous"), now)
    body = f'<html><body style="{FONT}"><h2>ANAモバイル データ残量({now:%Y-%m-%d %H:%M})</h2>{inner}</body></html>'
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[ana_mobile_data_mail] sent: %s", subject)
    except Exception as exc:
        logging.error("[ana_mobile_data_mail] mail send failed: %s", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
