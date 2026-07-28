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


def _load_x_trend_hits(root: Path) -> dict[str, dict]:
    """Maps ticker code (e.g. "6857") to today's X-trend finding, so watchlist
    cards can show whether that ticker was also buzzing on X today - the
    correlation is just this same-day cross-reference, no separate history
    needs to be kept for it."""
    payload = _load_json(root / "output" / "stock_x_trends.json")
    if not payload or payload.get("status") != "ok" or not payload.get("data"):
        return {}
    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=JST)
        if generated_at.astimezone(JST).date() != datetime.now(JST).date():
            return {}
    except (TypeError, ValueError):
        return {}

    hits: dict[str, dict] = {}
    for finding in payload["data"].get("stock_findings") or []:
        code = str(finding.get("ticker") or "").strip()
        if code:
            hits[code] = finding
    return hits


def _x_trend_finding(item: dict, x_trend_hits: dict[str, dict]) -> dict | None:
    code = str(item.get("ticker") or "").split(".")[0]
    return x_trend_hits.get(code)


def _x_trend_note(item: dict, x_trend_hits: dict[str, dict]) -> str:
    finding = _x_trend_finding(item, x_trend_hits)
    if not finding:
        return ""
    reason = html.escape(str(finding.get("reason") or "").strip())
    sentiment = html.escape(str(finding.get("sentiment") or "").strip())
    return f"""
    <div style="margin-top:6px;padding:6px 8px;background:#fff7ed;border:1px solid #fdba74;border-radius:6px;color:#9a3412;font-size:12px;">
      🔥 今日Xで話題({sentiment}): {reason}
    </div>
    """


_SENTIMENT_BONUS = {"strong_positive": 30, "positive": 20}


def _momentum_score(item: dict, x_trend_hits: dict[str, dict]) -> tuple[int, list[str]]:
    """Rule-based signal, not a prediction: recent uptrend + strength within
    the 30-day range + same-day positive X buzz. Purely mechanical scoring
    from data already computed elsewhere - no extra fetches."""
    range_info = item.get("range_30d") or {}
    score = 0
    reasons: list[str] = []

    if range_info.get("trend") == "up":
        score += 40
        reasons.append("直近5日上昇トレンド")
        change_pct = float(range_info.get("trend_change_pct") or 0)
        bonus = min(max(change_pct, 0) * 2, 20)
        if bonus:
            score += bonus
            reasons.append(f"上昇幅+{change_pct:.1f}%")

    position_pct = range_info.get("position_pct")
    if position_pct is not None and position_pct >= 70:
        score += 20
        reasons.append(f"30日レンジ上位({position_pct:.0f}%)")

    finding = _x_trend_finding(item, x_trend_hits)
    sentiment = str((finding or {}).get("sentiment") or "").strip()
    bonus = _SENTIMENT_BONUS.get(sentiment)
    if bonus:
        score += bonus
        reasons.append(f"当日Xで{sentiment}に話題")

    return min(round(score), 100), reasons


def _reversal_score(item: dict, x_trend_hits: dict[str, dict]) -> tuple[int, list[str]]:
    """Rule-based signal, not a prediction: deep in the 30-day low range,
    no longer actively falling, plus same-day positive X buzz."""
    range_info = item.get("range_30d") or {}
    score = 0
    reasons: list[str] = []

    position_pct = range_info.get("position_pct")
    if position_pct is not None and position_pct <= 20:
        score += 40
        reasons.append(f"30日安値圏(位置{position_pct:.0f}%)")
        score += min((20 - position_pct) * 2, 20)

    if range_info.get("trend") and range_info.get("trend") != "down":
        score += 10
        reasons.append("下落一服")

    finding = _x_trend_finding(item, x_trend_hits)
    sentiment = str((finding or {}).get("sentiment") or "").strip()
    bonus = _SENTIMENT_BONUS.get(sentiment)
    if bonus:
        score += bonus
        reasons.append(f"当日Xで{sentiment}に話題")

    return min(round(score), 100), reasons


def _candidate_rows(candidates: list[tuple[int, dict, list[str]]], empty_text: str) -> str:
    if not candidates:
        return f'<div style="color:#6b7280;font-size:12px;">{html.escape(empty_text)}</div>'
    rows = ""
    for score, item, reasons in candidates[:5]:
        rows += f"""
        <div style="margin-top:6px;padding:8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">
          <strong>{html.escape(item.get("name", ""))}</strong>
          <span style="color:#6b7280;font-size:12px;"> {_yahoo_finance_link(item.get("ticker", "-"))}</span>
          <span style="float:right;font-weight:bold;">{score}点</span>
          <div style="color:#6b7280;font-size:12px;clear:both;">{html.escape(" / ".join(reasons))}</div>
        </div>
        """
    return rows


def _score_candidates(items: list[dict], x_trend_hits: dict[str, dict]) -> tuple[list, list]:
    momentum = sorted(
        ((s, item, r) for item in items for s, r in [_momentum_score(item, x_trend_hits)] if s >= 40),
        key=lambda entry: entry[0],
        reverse=True,
    )
    reversal = sorted(
        ((s, item, r) for item in items for s, r in [_reversal_score(item, x_trend_hits)] if s >= 40),
        key=lambda entry: entry[0],
        reverse=True,
    )
    return momentum, reversal


PREDICTIONS_LOG_PATH = Path("state") / "stock_range_predictions.json"
PREDICTION_LOG_RETENTION_DAYS = 90


def _load_predictions_log(root: Path) -> list[dict]:
    path = root / PREDICTIONS_LOG_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _save_predictions_log(root: Path, records: list[dict]) -> None:
    path = root / PREDICTIONS_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _evaluate_predictions(root: Path, today_label: str, items_by_ticker: dict[str, dict]) -> list[dict]:
    """Marks previously-logged candidates as hit/miss using the next fresh
    daily change_pct we see for that ticker - i.e. the price move that
    happened after the candidate was flagged. Same-day entries are left
    alone so a candidate never gets evaluated against the data that
    produced it."""
    records = _load_predictions_log(root)
    changed = False
    for record in records:
        if record.get("evaluated") or record.get("logged_date", "") >= today_label:
            continue
        item = items_by_ticker.get(record.get("ticker"))
        change_pct = item.get("change_pct") if item else None
        if change_pct is None:
            continue
        record["evaluated"] = True
        record["evaluated_date"] = today_label
        record["actual_change_pct"] = change_pct
        record["hit"] = bool(change_pct > 0)
        changed = True

    cutoff_label = (datetime.now(JST) - timedelta(days=PREDICTION_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    trimmed = [r for r in records if r.get("logged_date", "9999-99-99") >= cutoff_label]
    if changed or len(trimmed) != len(records):
        _save_predictions_log(root, trimmed)
    return trimmed


def _append_predictions(root: Path, records: list[dict], today_label: str, momentum: list, reversal: list) -> None:
    # stock_range runs twice a day (06:45 and 21:30); skip tickers already
    # logged today under the same candidate type so a second run doesn't
    # double-count the same day's hit-rate stats.
    already_logged = {
        (r.get("type"), r.get("ticker"))
        for r in records
        if r.get("logged_date") == today_label
    }
    for kind, candidates in (("momentum", momentum), ("reversal", reversal)):
        for score, item, reasons in candidates:
            key = (kind, item.get("ticker"))
            if key in already_logged:
                continue
            records.append(
                {
                    "logged_date": today_label,
                    "type": kind,
                    "ticker": item.get("ticker"),
                    "name": item.get("name"),
                    "score": score,
                    "reasons": reasons,
                    "evaluated": False,
                    "hit": None,
                    "actual_change_pct": None,
                }
            )
    _save_predictions_log(root, records)


def _hit_rate_summary(records: list[dict]) -> str:
    def _stats(kind: str) -> tuple[int, int]:
        evaluated = [r for r in records if r.get("type") == kind and r.get("evaluated")]
        hits = sum(1 for r in evaluated if r.get("hit"))
        return hits, len(evaluated)

    m_hits, m_total = _stats("momentum")
    r_hits, r_total = _stats("reversal")
    m_text = f"{m_hits}/{m_total}({m_hits / m_total * 100:.0f}%)" if m_total else "データ蓄積中"
    r_text = f"{r_hits}/{r_total}({r_hits / r_total * 100:.0f}%)" if r_total else "データ蓄積中"
    return f'<div style="color:#6b7280;font-size:12px;margin-top:8px;">これまでの的中率(翌営業日の実際の値動きがプラスだったか): モメンタム型 {html.escape(m_text)} / リバーサル型 {html.escape(r_text)}</div>'


def _prediction_section(items: list[dict], x_trend_hits: dict[str, dict], momentum: list, reversal: list, hit_rate_html: str) -> str:
    return f"""
    <div style="margin-top:16px;padding:12px;background:#f8fafc;border-radius:8px;">
      <h3 style="margin-top:0;">翌営業日 上昇候補(機械的スコアリング・投資助言ではありません)</h3>
      <div style="color:#6b7280;font-size:12px;margin-bottom:8px;">30日レンジ位置・直近5営業日のトレンド・当日Xの話題を組み合わせた参考指標です。的中を保証するものではありません。</div>
      <h4 style="margin:0 0 4px;">モメンタム型(上昇継続を期待)</h4>
      {_candidate_rows(momentum, "該当銘柄なし")}
      <h4 style="margin:12px 0 4px;">リバーサル型(反発を期待)</h4>
      {_candidate_rows(reversal, "該当銘柄なし")}
      {hit_rate_html}
    </div>
    """


def _range_card(item: dict, x_trend_hits: dict[str, dict] | None = None) -> str:
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
      {_x_trend_note(item, x_trend_hits or {})}
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

    x_trend_hits = _load_x_trend_hits(root)

    def _market_block(title: str, market_items: list[dict]) -> str:
        if not market_items:
            return ""
        cards = "".join(_range_card(item, x_trend_hits) for item in market_items)
        return f'<h3 style="margin-top:18px;">{html.escape(title)}</h3>{cards}'

    sections = _market_block("日本株", japan_items) + _market_block("米国株", us_items) + _market_block("その他", other_items)

    now = datetime.now(JST)
    today_label = now.strftime("%Y-%m-%d")
    items_by_ticker = {item.get("ticker"): item for item in items}
    prediction_records = _evaluate_predictions(root, today_label, items_by_ticker)
    momentum, reversal = _score_candidates(items, x_trend_hits)
    _append_predictions(root, prediction_records, today_label, momentum, reversal)
    hit_rate_html = _hit_rate_summary(prediction_records)
    prediction_section = _prediction_section(items, x_trend_hits, momentum, reversal, hit_rate_html)

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>30日レンジ位置</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / 直近30営業日の値動きレンジの中で、現在値がどの位置にあるかを表示します。</div>
        {prediction_section}
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
