"""sumo_news_digest_mail: output/sumo_news_digest.json を読んで、相撲ニュースまとめをメールする。

データ(sumo_news_digest)→ JSON → メール(このモジュール)の形(2026-10-09 に分離)。
  status ok    → まとめのメール
  status error → 【失敗】メール
  それ以外 / 今日の実行でない / 送信済み(mailed_at)→ 何もしない(--resend なら送信済みでも送る)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.llm_client import send_failure_mail
from modules.mail_gmail import send_html_mail

JST = timezone(timedelta(hours=9), "JST")


def run(root: Path, resend: bool = False) -> None:
    path = root / "output" / "sumo_news_digest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.info("[sumo_news_digest_mail] skipped: no sumo_news_digest.json")
        return
    now = datetime.now(JST)
    if not str(payload.get("generated_at", "")).startswith(now.strftime("%Y-%m-%d")):
        logging.info("[sumo_news_digest_mail] skipped: not generated today")
        return
    if payload.get("mailed_at") and not resend:
        logging.info("[sumo_news_digest_mail] skipped: already mailed")
        return
    status = payload.get("status")
    if status == "error":
        send_failure_mail("相撲ニュースまとめ", str(payload.get("error")), {"provider": payload.get("provider", "claude")})
    elif status == "ok":
        if not _send_digest(payload, now):
            return
    else:
        logging.info("[sumo_news_digest_mail] skipped: sumo_news_digest status is %s", status)
        return
    payload["mailed_at"] = datetime.now(JST).isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _send_digest(payload: dict, now: datetime) -> bool:
    topics = payload.get("topics") or []
    lookback_days = payload.get("lookback_days")
    items = [None] * int(payload.get("item_count") or 0)
    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        logging.warning("[sumo_news_digest_mail] mail skipped: missing Gmail settings")
        return False

    def _topic_card(topic: dict) -> str:
        headline = html.escape(topic["headline"])
        body_text = html.escape(topic["body"])
        url = topic.get("source_url")
        link_html = (
            f'<div style="margin-top:6px;"><a href="{html.escape(url)}" target="_blank" rel="noopener" style="font-size:12px;color:#2563eb;">情報元</a></div>'
            if url
            else ""
        )
        return f"""
        <div style="margin-top:10px;padding:10px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;">
          <div style="font-weight:bold;font-size:15px;">{headline}</div>
          <div style="color:#334155;font-size:13px;margin-top:4px;line-height:1.6;">{body_text}</div>
          {link_html}
        </div>
        """

    topics_html = "".join(_topic_card(t) for t in topics) or '<div style="color:#6b7280;">要約できる話題がありませんでした。</div>'
    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>大相撲ニュース まとめ({lookback_days}日分)</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / 記事{len(items)}件から{len(topics)}トピックに要約</div>
        {topics_html}
      </body>
    </html>
    """
    subject = f"大相撲ニュース まとめ {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[sumo_news_digest_mail] sent digest mail")
        return True
    except Exception as exc:
        logging.error("[sumo_news_digest_mail] mail send failed: %s", exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Mail the sumo news digest (output/sumo_news_digest.json).")
    parser.add_argument("--resend", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(root, resend=args.resend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
