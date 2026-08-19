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


def _end_time_text(end_time: str | None) -> str:
    if not end_time:
        return "-"
    try:
        dt = datetime.fromisoformat(end_time)
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "-"


def _target_diff_text(price: int | None, target_price: int | None) -> str:
    # Deliberately plain/muted, not colored or bolded: this is informational
    # context (e.g. an accessory-only listing priced far below the target
    # is obviously a different item, not a deal), not a "cheap!" alert.
    if price is None or target_price is None:
        return ""
    diff = price - target_price
    sign = "+" if diff >= 0 else ""
    return f" (目標{target_price:,}円比 {sign}{diff:,}円)"


NEAR_TARGET_RATIO = 0.35


def _is_near_target(price: int | None, target_price: int | None) -> bool:
    if price is None or target_price is None or target_price <= 0:
        return False
    return abs(price - target_price) <= target_price * NEAR_TARGET_RATIO


def _item_card(item: dict, target_price: int | None) -> str:
    title = html.escape(item.get("title") or "-")
    url = item.get("url") or ""
    platform_label = PLATFORM_LABELS.get(item.get("platform"), item.get("platform") or "-")
    price = item.get("price")
    price_text = f"{price:,}円" if isinstance(price, int) else "-"
    diff_text = _target_diff_text(price, target_price)
    buy_now_price = item.get("buy_now_price")
    buy_now_text = f" / 即決{buy_now_price:,}円" if isinstance(buy_now_price, int) else ""
    shipping_text = html.escape(item.get("shipping") or "-")
    bid_count = item.get("bid_count")
    bid_text = f"{html.escape(bid_count)}件" if bid_count else "-"
    end_time_text = _end_time_text(item.get("end_time"))
    # is_store is inferred from a Yahoo-Shopping-catalog-sync flag, not a
    # confirmed seller-type badge from Yahoo - see marketplace_watch.py.
    store_badge = (
        '<span style="background:#fde047;color:#713f12;font-weight:bold;font-size:11px;'
        'padding:2px 6px;border-radius:4px;margin-right:6px;">🏷 ストア出品</span>'
        if item.get("is_store")
        else ""
    )
    near_target = _is_near_target(price, target_price)
    card_style = (
        "margin-top:8px;padding:8px;border-radius:6px;background:#eff6ff;border:2px solid #2563eb;"
        if near_target
        else "margin-top:8px;padding:8px;border-radius:6px;background:#ffffff;border:1px solid #e5e7eb;"
    )
    near_target_badge = (
        '<span style="background:#2563eb;color:#ffffff;font-weight:bold;font-size:11px;'
        'padding:2px 6px;border-radius:4px;margin-right:6px;">⭐ 目標近辺</span>'
        if near_target
        else ""
    )
    link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
    return f"""
    <div style="{card_style}">
      <div>{near_target_badge}{store_badge}</div>
      <span style="font-weight:bold;">{link}</span>
      <span style="color:#6b7280;font-size:12px;">({html.escape(platform_label)})</span>
      <div style="color:#334155;font-size:14px;font-weight:bold;margin-top:2px;">現在{price_text}{buy_now_text}<span style="color:#6b7280;font-weight:normal;">{diff_text}</span></div>
      <div style="color:#6b7280;font-size:12px;">送料: {shipping_text} / 入札件数: {bid_text}</div>
      <div style="color:#6b7280;font-size:12px;">終了日時: {end_time_text}</div>
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
        target_price = entry.get("target_price")
        new_items = entry.get("new_items") or []
        total_new += len(new_items)
        if new_items:
            # Near-target items first (still stable-ordered within each
            # group), so the ones worth a closer look aren't buried below
            # accessory-only or off-target listings.
            sorted_items = sorted(
                new_items,
                key=lambda item: not _is_near_target(item.get("price"), target_price),
            )
            cards = "".join(_item_card(item, target_price) for item in sorted_items)
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
