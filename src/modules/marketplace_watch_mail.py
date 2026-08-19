import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
PLATFORM_LABELS = {"yahoo_auction": "ヤフオク"}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _item_card(item: dict) -> str:
    title = html.escape(item.get("title") or "-")
    url = item.get("url") or ""
    price = item.get("price")
    price_text = f"{price:,}円" if isinstance(price, int) else "-"
    platform_label = PLATFORM_LABELS.get(item.get("platform"), item.get("platform") or "-")
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f"""
    <div style="margin-top:8px;padding:8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">
      <span style="font-weight:bold;">{link}</span>
      <div style="color:#6b7280;font-size:12px;margin-top:2px;">{html.escape(platform_label)} / {price_text}</div>
    </div>
    """


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "marketplace_watch_mail.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    data = _load_json(root / "output" / "marketplace_watch.json")
    if not data or data.get("status") != "ok":
        result = {
            "module": "marketplace_watch_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "marketplace_watch output is not available or not ok.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[marketplace_watch_mail] skipped: marketplace_watch status is not ok")
        return

    results = data.get("results") or []
    sections = ""
    total_new = 0
    for entry in results:
        keyword = entry.get("keyword") or "-"
        new_items = entry.get("new_items") or []
        total_new += len(new_items)
        if new_items:
            cards = "".join(_item_card(item) for item in new_items)
        else:
            cards = '<div style="color:#6b7280;">新着出品はありませんでした。</div>'
        sections += f'<h3 style="margin-top:16px;">{html.escape(keyword)}</h3>{cards}'
    if not sections:
        sections = '<div style="color:#6b7280;">監視中のキーワードがありません。</div>'

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>フリマ新着監視(ヤフオク)</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点</div>
        {sections}
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
    result = {
        "module": "marketplace_watch_mail",
        "generated_at": generated_at,
        "status": "ok",
        "new_item_count": total_new,
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[marketplace_watch_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] フリマ新着監視 {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[marketplace_watch_mail] sent %d new item(s)", total_new)
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[marketplace_watch_mail] mail send failed: %s", exc)
        return

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
