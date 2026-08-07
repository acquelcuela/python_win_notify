from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail
from modules.stock_range_mail import _yahoo_finance_link


JST = timezone(timedelta(hours=9), "JST")


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _result_row(record: dict) -> str:
    hit = record.get("hit")
    verdict_text, verdict_color = ("的中", "#047857") if hit else ("不発", "#b91c1c")
    actual_pct = record.get("actual_change_pct")
    actual_text = f"{actual_pct:+.2f}%" if actual_pct is not None else "-"
    reasons = " / ".join(record.get("reasons") or [])
    return f"""
    <div style="margin-top:6px;padding:8px;background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;">
      <strong>{html.escape(record.get("name", ""))}</strong>
      <span style="color:#6b7280;font-size:12px;"> {_yahoo_finance_link(record.get("ticker", "-"))}</span>
      <span style="float:right;font-weight:bold;color:{verdict_color};">{verdict_text}</span>
      <div style="color:#6b7280;font-size:12px;clear:both;">スコア{record.get("score")}点 / 本日{actual_text} / {html.escape(reasons)}</div>
    </div>
    """


def _section(title: str, group: list[dict]) -> str:
    if not group:
        return ""
    rows = "".join(_result_row(r) for r in group)
    return f'<h4 style="margin:12px 0 4px;">{html.escape(title)}</h4>{rows}'


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_range_eval_mail.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    data = _load_json(root / "output" / "stock_range_eval.json")
    if not data or data.get("status") != "ok":
        result = {
            "module": "stock_range_eval_mail",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "stock_range_eval output is not available.",
        }
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_range_eval_mail] skipped: stock_range_eval output is not available")
        return

    results = data.get("results") or []
    momentum = [r for r in results if r.get("type") == "momentum"]
    reversal = [r for r in results if r.get("type") == "reversal"]
    hit_count = data.get("hit_count", 0)
    evaluated_count = data.get("evaluated_count", len(results))
    skipped_count = data.get("skipped_count", 0)
    skipped_note = (
        f'<div style="color:#6b7280;font-size:12px;">{skipped_count}件は本日終値が未取得のため未評価です(米国株など)。</div>'
        if skipped_count
        else ""
    )

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>30日レンジ位置 本日の的中結果</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / 本日06:45に出したモメンタム型・リバーサル型の候補が、本日の値動きでプラスになったかを評価しています。</div>
        <div style="margin-top:8px;font-weight:bold;">本日 {hit_count}/{evaluated_count} 的中</div>
        {skipped_note}
        {_section("モメンタム型", momentum)}
        {_section("リバーサル型", reversal)}
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
        "module": "stock_range_eval_mail",
        "generated_at": generated_at,
        "status": "ok",
        "evaluated_count": evaluated_count,
        "hit_count": hit_count,
    }
    if missing:
        result["status"] = "error"
        result["reason"] = "Missing Gmail settings: " + ", ".join(missing)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[stock_range_eval_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
        return

    subject = f"[NightlyBatchNotify] 30日レンジ候補 本日の結果 {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[stock_range_eval_mail] sent result mail for %d candidates (%d hits)", evaluated_count, hit_count)
    except Exception as exc:
        result["status"] = "error"
        result["reason"] = str(exc)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[stock_range_eval_mail] mail send failed: %s", exc)
        return

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
