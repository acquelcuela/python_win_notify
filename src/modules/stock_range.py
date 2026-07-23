from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fmt_decimal(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{digits}f}"


def _fmt_change(change, change_pct) -> tuple[str, str]:
    if change is None:
        return "-", "#334155"
    sign = "+" if change >= 0 else ""
    color = "#047857" if change >= 0 else "#b91c1c"
    return f"{sign}{float(change):,.2f} ({sign}{float(change_pct):.2f}%)", color


def _yahoo_finance_link(ticker: str) -> str:
    ticker = str(ticker or "").strip()
    if not ticker or ticker == "-":
        return html.escape(ticker or "-")
    import urllib.parse

    url = f"https://finance.yahoo.co.jp/quote/{urllib.parse.quote(ticker)}"
    return f'<a href="{url}" target="_blank" rel="noopener">{html.escape(ticker)}</a>'


def _position_label(position_pct) -> str:
    if position_pct is None:
        return "-"
    if position_pct >= 80:
        return "高値圏"
    if position_pct <= 20:
        return "安値圏"
    return "中間"


_TREND_LABELS = {
    "up": ("上昇中", "#047857"),
    "down": ("下降中", "#b91c1c"),
    "flat": ("横ばい", "#334155"),
    "unknown": ("-", "#6b7280"),
}


def _trend_text(range_info: dict) -> tuple[str, str]:
    trend = range_info.get("trend", "unknown")
    label, color = _TREND_LABELS.get(trend, _TREND_LABELS["unknown"])
    days = range_info.get("trend_days")
    trend_change_pct = range_info.get("trend_change_pct")
    if not days or trend_change_pct is None:
        return "-", color
    sign = "+" if trend_change_pct >= 0 else ""
    return f"直近{days}営業日: {label}（{sign}{trend_change_pct:.2f}%）", color


def _range_card(item: dict) -> str:
    range_info = item.get("range_30d")
    if not range_info:
        return ""
    change_text, change_color = _fmt_change(
        range_info.get("change_since_start"), range_info.get("change_pct_since_start", 0)
    )
    prev_change_text, prev_change_color = _fmt_change(item.get("change"), item.get("change_pct", 0))
    position_pct = range_info.get("position_pct")
    position_pct_clamped = max(0.0, min(100.0, float(position_pct))) if position_pct is not None else 0.0
    position_label = _position_label(position_pct)
    distance_from_low_pct = range_info.get("distance_from_low_pct")
    distance_from_high_pct = range_info.get("distance_from_high_pct")
    distance_text = "-"
    if distance_from_low_pct is not None and distance_from_high_pct is not None:
        distance_text = f"安値比: +{distance_from_low_pct:.2f}% / 高値比: {distance_from_high_pct:.2f}%"
    trend_text, trend_color = _trend_text(range_info)
    return f"""
    <div style="margin-top:10px;padding:10px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;">
      <div style="margin-bottom:4px;">
        <strong>{html.escape(item.get("name", ""))}</strong>
        <span style="color:#6b7280;font-size:12px;">{_yahoo_finance_link(item.get("ticker", "-"))}</span>
      </div>
      <div style="color:#6b7280;font-size:12px;">{html.escape(str(range_info.get("start_date", "-")))}（{html.escape(str(range_info.get("trading_days", "-")))}営業日前）: {_fmt_decimal(range_info.get("start_price"))} → 現在: {_fmt_decimal(item.get("close"))}</div>
      <div style="color:{change_color};font-size:13px;font-weight:bold;">{change_text}(30日前比)</div>
      <div style="color:{prev_change_color};font-size:13px;font-weight:bold;">{prev_change_text}(前日比)</div>
      <div style="color:#6b7280;font-size:12px;">30日高値: {_fmt_decimal(range_info.get("high_price"))}（{html.escape(str(range_info.get("high_date", "-")))}） / 30日安値: {_fmt_decimal(range_info.get("low_price"))}（{html.escape(str(range_info.get("low_date", "-")))}）</div>
      <div style="background:#e5e7eb;border-radius:4px;height:8px;width:100%;margin-top:6px;">
        <div style="background:#2563eb;border-radius:4px;height:8px;width:{position_pct_clamped}%;"></div>
      </div>
      <div style="color:#6b7280;font-size:12px;">現在位置: レンジの{html.escape(str(position_pct))}%地点（{position_label}） / {html.escape(distance_text)}</div>
      <div style="color:{trend_color};font-size:12px;font-weight:bold;">{html.escape(trend_text)}</div>
    </div>
    """


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_range.json"
    generated_at = datetime.now(JST).isoformat()

    payload = _load_json(root / "output" / "stock_watchlist.json")
    if not payload or payload.get("status") != "ok" or not payload.get("data"):
        result = {
            "module": "stock_range",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "stock_watchlist output is not available.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_range] skipped: stock_watchlist output is not available")
        return

    items = [item for item in payload["data"] if item.get("range_30d")]
    items.sort(key=lambda item: item["range_30d"].get("trend_change_pct") or 0, reverse=True)
    if not items:
        result = {
            "module": "stock_range",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No range_30d data found in stock_watchlist output.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_range] skipped: no range_30d data")
        return

    japan_items = [item for item in items if item.get("market") == "japan"]
    us_items = [item for item in items if item.get("market") == "us"]
    other_items = [item for item in items if item.get("market") not in {"japan", "us"}]

    def _market_block(title: str, market_items: list[dict]) -> str:
        if not market_items:
            return ""
        cards = "".join(_range_card(item) for item in market_items)
        return f'<h3 style="margin-top:18px;">{html.escape(title)}</h3>{cards}'

    sections = _market_block("日本株", japan_items) + _market_block("米国株", us_items) + _market_block("その他", other_items)

    now = datetime.now(JST)
    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>30日レンジ位置</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / 直近30営業日の値動きレンジの中で、現在値がどの位置にあるかを表示します。</div>
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
        "module": "stock_range",
        "generated_at": generated_at,
        "status": "ok",
        "ticker_count": len(items),
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[stock_range] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] 30日レンジ位置 {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[stock_range] sent range report for %d tickers", len(items))
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[stock_range] mail send failed: %s", exc)
        return

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
