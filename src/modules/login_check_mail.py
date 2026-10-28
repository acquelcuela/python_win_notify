"""Mails the result of login_check, only when Claude or Grok has a problem."""
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail

JST = timezone(timedelta(hours=9), "JST")
FIXES = {
    "claude": "このPCでターミナルを開き、claude を起動して /login でログインしてください。",
    "grok": "このPCのChromeで https://grok.com を開いてログインしてください"
            "(Claude in Chrome の拡張機能がつながっていること、つながっているブラウザが1つだけであることも確認してください)。",
}
USED_BY = {
    "claude": "売買考察(18:00)、レポートの要約、相撲・noteのバッチなど",
    "grok": "Xトレンドの取得(06:30・22:30)、ラジオのゲスト確認(11:00)",
}


def run(root: Path) -> None:
    now = datetime.now(JST)
    try:
        data = json.loads((root / "output" / "login_check.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.info("[login_check_mail] skipped: no login_check.json")
        return
    if not str(data.get("generated_at", "")).startswith(now.strftime("%Y-%m-%d")):
        logging.info("[login_check_mail] skipped: login_check.json is not from today")
        return
    problems = [name for name in ("claude", "grok") if (data.get(name) or {}).get("ok") is not True]
    if not problems:
        logging.info("[login_check_mail] all logged in; no mail")
        return
    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        logging.warning("[login_check_mail] mail skipped: missing Gmail settings")
        return
    label = {"claude": "Claude", "grok": "Grok"}
    blocks = ""
    for name in problems:
        info = data.get(name) or {}
        blocks += (
            f'<div style="margin:12px 0;padding:8px 12px;border-left:6px solid #b91c1c;background:#fef2f2;">'
            f'<div style="font-weight:bold;">⚠ {label[name]}</div>'
            f'<div>{html.escape(str(info.get("error") or "確認できませんでした"))}</div>'
            f'<div style="margin-top:4px;">直し方: {html.escape(FIXES[name])}</div>'
            f'<div style="color:#6b7280;font-size:12px;">使っているバッチ: {html.escape(USED_BY[name])}</div></div>'
        )
    body = (
        "<html><body style=\"font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;\">"
        f"<h2>ログインの確認({now:%Y-%m-%d %H:%M})</h2>{blocks}</body></html>"
    )
    subject = f"⚠ ログアウトを検知: {'・'.join(label[p] for p in problems)}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[login_check_mail] sent: %s", subject)
    except Exception as exc:
        logging.error("[login_check_mail] mail send failed: %s", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
