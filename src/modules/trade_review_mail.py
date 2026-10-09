"""trade_review_mail: output/trade_review.json を読んで、売買考察をメールで送る。

ほかのモジュールと同じ「データ(trade_review) → JSON → メール(このモジュール)」の形。
メールの見た目(上げは緑・下げは赤、時系列の行は表)はここで作るので、見た目を直したときは
Claude を呼び直さずに `python -m modules.trade_review_mail --resend` で同じ考察を再送できる。

  status ok     → 考察(mail_text)を色付き・表形式で送る
  status error  → 【失敗】メール
  status skipped/送信済み(mailed_at あり) → 何もしない(--resend なら送信済みでも送る)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.llm_client import send_failure_mail
from modules.mail_gmail import send_html_mail

JST = timezone(timedelta(hours=9), "JST")


# Mail-only styling (the .txt note stays plain for logs and for Claude to read
# back): same up/down colors as the report (green up, red down).
UP_COLOR, DOWN_COLOR, FLAT_COLOR = "#047857", "#b91c1c", "#64748b"
_PCT_RE = re.compile(r"([+\-−±])(\d+(?:\.\d+)?)%")
_LABEL_RE = re.compile(r"^(\s*)(想定|結果|考察|訂正|見ておく点)(:|:)")


def _color_pcts(line: str) -> str:
    def repl(match: re.Match) -> str:
        sign, number = match.group(1), match.group(2)
        if sign == "±" or float(number) == 0:
            color = FLAT_COLOR
        else:
            color = UP_COLOR if sign == "+" else DOWN_COLOR
        return f'<b style="color:{color};">{sign}{number}%</b>'

    return _PCT_RE.sub(repl, line)


_SEGMENT_RE = re.compile(r"^(.*?)\s*([+\-−±]\d+(?:\.\d+)?%)\s*(.*)$")
_CELL = 'style="padding:3px 8px;border:1px solid #e2e8f0;text-align:center;white-space:nowrap;"'


def _parse_series(raw: str):
    """'結果: 寄り付きの値の飛び -2.0% / 寄り→10時 -1.3% / ...' -> (row label, [(column, value)], note).
    Only lines with 3+ "label value%" segments are treated as a time series."""
    stripped = raw.strip().lstrip("-・ ").strip()
    prefix, sep, rest = stripped.partition(":")
    if not sep:
        prefix, sep, rest = stripped.partition(":")
    if not sep:
        prefix, rest = "", stripped
    segments = [s.strip() for s in rest.split(" / ")]
    if len(segments) < 3:
        return None
    cells, note = [], ""
    for segment in segments:
        match = _SEGMENT_RE.match(segment)
        if not match or not match.group(1):
            return None
        cells.append((match.group(1).strip(), match.group(2)))
        if match.group(3):
            note = (note + " " + match.group(3)).strip()
    return prefix.strip(), cells, note


def _columns_key(series) -> list[str]:
    # "前日比(15:25)" and "前日比(15:20)" are the same column
    return [re.sub(r"\(.*?\)|（.*?）", "", c) for c, _ in series[1]]


_PAREN_RE = re.compile(r"\((.*?)\)|（(.*?)）")


def _series_table(rows: list[tuple[str, list, str]]) -> str:
    # Column headers drop any "(...)" part; a row's own "(...)" (e.g. the times in
    # "よかった買い方(9:15→10:00)") is shown inside its cell instead.
    columns = [_PAREN_RE.sub("", c) for c, _ in rows[0][1]]
    has_label = any(label and label not in ("結果",) for label, _, _ in rows)
    head = "".join(f'<th {_CELL} bgcolor="#f1f5f9">{html.escape(c)}</th>' for c in columns)
    body = ""
    for label, cells, _ in rows:
        label_cell = f'<th {_CELL} bgcolor="#f8fafc">{html.escape(label)}</th>' if has_label else ""
        tds = ""
        for column, value in cells:
            extra = "".join(a or b for a, b in _PAREN_RE.findall(column))
            small = f'<div style="font-size:11px;color:#64748b;">{html.escape(extra)}</div>' if extra else ""
            tds += f"<td {_CELL}>{_color_pcts(html.escape(value))}{small}</td>"
        body += "<tr>" + label_cell + tds + "</tr>"
    notes = "".join(f'<div style="font-size:12px;color:#64748b;">※{html.escape(n)}</div>' for _, _, n in rows if n)
    corner = f'<th {_CELL} bgcolor="#f1f5f9"></th>' if has_label else ""
    return (
        '<div style="overflow-x:auto;margin:4px 0 6px;">'
        f'<table style="border-collapse:collapse;font-size:13px;"><tr>{corner}{head}</tr>{body}</table>{notes}</div>'
    )


def _to_html(text: str) -> str:
    rows = []
    pending: list = []  # consecutive time-series lines with the same columns -> one table

    def flush() -> None:
        if pending:
            rows.append(_series_table(pending))
            pending.clear()

    for raw in text.splitlines():
        series = _parse_series(raw)
        if series:
            if pending and _columns_key(pending[0]) != _columns_key(series):
                flush()
            pending.append(series)
            continue
        flush()
        line = html.escape(raw)
        stripped = raw.strip()
        if stripped.startswith("■"):
            rows.append(
                f'<div style="margin:18px 0 6px;padding:6px 10px;background:#111827;color:#ffffff;'
                f'font-weight:bold;border-radius:4px;">{html.escape(stripped)}</div>'
            )
            continue
        if stripped.startswith("◆"):
            color = "#047857" if "◎" in stripped else ("#b45309" if "⚠" in stripped else "#1d4ed8")
            rows.append(
                f'<div style="margin:14px 0 4px;padding:6px 10px;border-left:6px solid {color};'
                f'background:#f8fafc;font-size:15px;font-weight:bold;">{_color_pcts(html.escape(stripped))}</div>'
            )
            continue
        if set(stripped) <= {"="} and stripped:
            continue
        line = _color_pcts(line)
        line = _LABEL_RE.sub(
            lambda m: f'{m.group(1)}<span style="display:inline-block;min-width:3em;font-weight:bold;color:#334155;">{m.group(2)}</span>{m.group(3)}',
            line,
        )
        line = line.replace("✅", '<span style="color:#047857;">✅</span>').replace("❌", '<span style="color:#b91c1c;">❌</span>')
        rows.append(f'<div style="margin:2px 0;padding-left:{(len(raw) - len(raw.lstrip())) * 6}px;">{line or "&nbsp;"}</div>')
    flush()
    if rows and not rows[0].startswith("<div style=\"margin:18px"):
        rows[0] = rows[0].replace('<div style="margin:2px 0;', '<div style="margin:2px 0;font-size:18px;font-weight:bold;', 1)
    return "".join(rows)


def run(root: Path, resend: bool = False) -> None:
    path = root / "output" / "trade_review.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.info("[trade_review_mail] skipped: no trade_review.json")
        return
    if payload.get("mailed_at") and not resend:
        logging.info("[trade_review_mail] skipped: already mailed at %s", payload["mailed_at"])
        return
    status = payload.get("status")
    if status == "error":
        send_failure_mail(payload.get("title") or "売買考察", str(payload.get("error")), {"provider": "claude"})
    elif status == "ok" and payload.get("mail_text"):
        gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
        app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
        mail_to = os.getenv("MAIL_TO", "").strip()
        if not (gmail_address and app_password and mail_to):
            logging.warning("[trade_review_mail] mail skipped: missing Gmail settings")
            return
        body = (
            "<html><body style=\"font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;"
            'font-size:14px;line-height:1.6;max-width:760px;">'
            f"{_to_html(payload['mail_text'])}"
            "</body></html>"
        )
        try:
            send_html_mail(gmail_address, app_password, mail_to, payload.get("title") or "売買考察", body)
        except Exception as exc:
            logging.error("[trade_review_mail] mail send failed: %s", exc)
            return
    else:
        logging.info("[trade_review_mail] skipped: trade_review status is %s", status)
        return
    payload["mailed_at"] = datetime.now(JST).isoformat()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[trade_review_mail] sent (%s)", status)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mail the latest trade review (output/trade_review.json).")
    parser.add_argument("--resend", action="store_true", help="send even if it was already mailed")
    args = parser.parse_args()
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(root, resend=args.resend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
