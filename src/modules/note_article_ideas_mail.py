import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _idea_card(idea: dict) -> str:
    title = html.escape(idea.get("title") or "-")
    angle = html.escape(idea.get("angle") or "-")
    category = html.escape(idea.get("category") or "-")
    inspired_by = html.escape(idea.get("inspired_by") or "-")
    return f"""
    <div style="margin-top:10px;padding:10px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;">
      <div style="font-weight:bold;font-size:15px;">{title}</div>
      <div style="color:#334155;font-size:13px;margin-top:4px;">{angle}</div>
      <div style="color:#6b7280;font-size:12px;margin-top:6px;">カテゴリ: {category} / 参考: {inspired_by}</div>
    </div>
    """


def _trend_seed_row(seed: dict) -> str:
    title = html.escape(seed.get("title") or "-")
    url = seed.get("url") or ""
    like_count = seed.get("like_count")
    like_text = f"いいね{like_count:,}" if isinstance(like_count, int) else "-"
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f'<div style="font-size:12px;color:#6b7280;margin-top:3px;">{link}({like_text})</div>'


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "note_article_ideas_mail.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    data = _load_json(root / "output" / "note_article_ideas.json")
    if not data or data.get("status") != "ok":
        result = {
            "module": "note_article_ideas_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "note_article_ideas output is not available or not ok.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[note_article_ideas_mail] skipped: note_article_ideas status is not ok")
        return

    ideas = data.get("ideas") or []
    ideas_html = "".join(_idea_card(idea) for idea in ideas) or '<div style="color:#6b7280;">本日は提案なしでした。</div>'

    trend_seeds = (data.get("trend_seeds") or [])[:8]
    seeds_html = "".join(_trend_seed_row(seed) for seed in trend_seeds)

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>note記事 構想メモ</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点</div>
        {ideas_html}
        <h3 style="margin-top:20px;">参考: 今伸びている記事(note「投資」トピック)</h3>
        {seeds_html}
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
        "module": "note_article_ideas_mail",
        "generated_at": generated_at,
        "status": "ok",
        "idea_count": len(ideas),
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[note_article_ideas_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] note記事 構想メモ {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[note_article_ideas_mail] sent %d idea(s)", len(ideas))
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[note_article_ideas_mail] mail send failed: %s", exc)
        return

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
