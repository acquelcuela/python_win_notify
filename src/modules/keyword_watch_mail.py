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


def _news_card(item: dict) -> str:
    title = html.escape(item.get("title") or "-")
    url = item.get("url") or ""
    source = html.escape(item.get("source") or "")
    published_at = item.get("published_at")
    time_text = "-"
    if published_at:
        try:
            dt = datetime.fromisoformat(published_at)
            time_text = dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f"""
    <div style="margin-top:8px;padding:8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">
      <span style="font-weight:bold;">{link}</span>
      <div style="color:#6b7280;font-size:12px;margin-top:2px;">{source} / {time_text}</div>
    </div>
    """


def _group_by_query(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        query = item.get("query") or "-"
        if query not in groups:
            groups[query] = []
            order.append(query)
        groups[query].append(item)
    return [(query, groups[query]) for query in order]


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "keyword_watch_mail.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    data = _load_json(root / "output" / "keyword_watch.json")
    if not data or data.get("status") != "ok":
        # keyword_watch also writes status="skipped" on the ~29 non-trigger
        # days of the month - that's not "ran and found nothing", so stay
        # silent here rather than sending a misleading "no news" mail every
        # single day.
        result = {
            "module": "keyword_watch_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "keyword_watch did not run today (not the scheduled day, no queries, or no output).",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[keyword_watch_mail] skipped: keyword_watch status is not ok")
        return

    items = data.get("data") or []
    grouped = dict(_group_by_query(items))
    # queries wasn't recorded in older keyword_watch.json output - fall back
    # to only the queries that actually had hits in that case.
    queries = data.get("queries") or list(grouped.keys())

    sections = ""
    for query in queries:
        group = grouped.get(query) or []
        if group:
            cards = "".join(_news_card(item) for item in group)
        else:
            cards = '<div style="color:#6b7280;">該当記事なし</div>'
        sections += f'<h3 style="margin-top:16px;">{html.escape(query)}</h3>{cards}'
    if not sections:
        sections = '<div style="color:#6b7280;">今回は新着の関連記事がありませんでした。</div>'

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>キーワード月次検索</h2>
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
        "module": "keyword_watch_mail",
        "generated_at": generated_at,
        "status": "ok",
        "item_count": len(items),
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[keyword_watch_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] キーワード月次検索 {now.strftime('%Y-%m')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[keyword_watch_mail] sent %d item(s)", len(items))
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[keyword_watch_mail] mail send failed: %s", exc)
        return

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
