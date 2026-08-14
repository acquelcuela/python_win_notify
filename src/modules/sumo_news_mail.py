import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
OTHER_LABEL = "その他"
RANK_ORDER = ["横綱", "大関", "関脇", "小結", "前頭", "十両"]


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
            time_text = dt.astimezone(JST).strftime("%m/%d %H:%M")
        except ValueError:
            pass
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f"""
    <div style="margin-top:8px;padding:8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">
      <span style="font-weight:bold;">{link}</span>
      <div style="color:#6b7280;font-size:12px;margin-top:2px;">{source} / {time_text}</div>
    </div>
    """


def _group_by_wrestler(items: list[dict]) -> list[tuple[str, str | None, list[dict]]]:
    """Groups items by wrestler, ordered by peak rank (横綱 first, 十両
    last, unranked/unrecognized wrestlers after that), with untagged items
    collected last under OTHER_LABEL. Within a rank tier, groups keep the
    order wrestlers first appeared in (items are already sorted
    newest-first)."""
    groups: dict[str, list[dict]] = {}
    peak_ranks: dict[str, str | None] = {}
    order: list[str] = []
    other: list[dict] = []
    for item in items:
        wrestler = item.get("wrestler")
        if not wrestler:
            other.append(item)
            continue
        if wrestler not in groups:
            groups[wrestler] = []
            peak_ranks[wrestler] = item.get("wrestler_peak_rank")
            order.append(wrestler)
        groups[wrestler].append(item)

    def _rank_sort_key(name: str) -> int:
        rank = peak_ranks.get(name)
        return RANK_ORDER.index(rank) if rank in RANK_ORDER else len(RANK_ORDER)

    order.sort(key=_rank_sort_key)
    result = [(name, peak_ranks[name], groups[name]) for name in order]
    if other:
        result.append((OTHER_LABEL, None, other))
    return result


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "sumo_news_mail.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    data = _load_json(root / "output" / "sumo_news.json")
    if not data or data.get("status") != "ok" or not data.get("data"):
        result = {
            "module": "sumo_news_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "sumo_news output is not available.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[sumo_news_mail] skipped: sumo_news output is not available")
        return

    items = data["data"]
    sections = ""
    for wrestler, peak_rank, group in _group_by_wrestler(items):
        cards = "".join(_news_card(item) for item in group)
        rank_badge = (
            f'<span style="color:#6b7280;font-size:12px;font-weight:normal;">(最高位: {html.escape(peak_rank)})</span>'
            if peak_rank
            else ""
        )
        sections += f'<h3 style="margin-top:16px;">{html.escape(wrestler)} {rank_badge}</h3>{cards}'

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>大相撲ニュース</h2>
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
        "module": "sumo_news_mail",
        "generated_at": generated_at,
        "status": "ok",
        "item_count": len(items),
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[sumo_news_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] 大相撲ニュース {now.strftime('%Y-%m-%d %H:%M')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[sumo_news_mail] sent %d news items", len(items))
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[sumo_news_mail] mail send failed: %s", exc)
        return

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
