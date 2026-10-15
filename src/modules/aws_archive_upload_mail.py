"""Daily status mail for the Deep Archive upload, built from aws_archive_check
(05:30): tonight's job result (or that it is still sending), what is still
waiting to be sent, what is done, and the `aws s3 ls` of the bucket."""
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail

JST = timezone(timedelta(hours=9), "JST")
STATUS_LABELS = {
    "uploaded": ("✅ 送信しました", "#047857"),
    "already_uploaded": ("☑ AWSに送信済みだったので完了に移動しました", "#2563eb"),
    "error": ("❌ 送信できませんでした", "#b91c1c"),
    "interrupted": ("❌ 送信が途中で止まっていました", "#b91c1c"),
    "running": ("⏳ まだ送信中です", "#b45309"),
    "starting": ("⏳ まだ送信中です", "#b45309"),
}


def _gb(size) -> str:
    return f"{size / 1024 ** 3:.2f} GB" if isinstance(size, int) else "-"


def _table(rows: list[list[str]], header: list[str]) -> str:
    th = "".join(f'<th style="text-align:left;padding:4px 8px;background:#f1f5f9;">{html.escape(h)}</th>' for h in header)
    trs = "".join(
        "<tr>" + "".join(f'<td style="padding:4px 8px;border-top:1px solid #e5e7eb;">{c}</td>' for c in row) + "</tr>"
        for row in rows
    )
    return f'<table style="border-collapse:collapse;font-size:13px;"><tr>{th}</tr>{trs}</table>'


def _body(data: dict, now: datetime) -> tuple[str, str]:
    job = data.get("job") or {}
    result = job.get("result") or {}
    queue = data.get("queue") or []
    done = data.get("done") or []
    listing = data.get("listing") or {}
    bad, too_big = set(), set(data.get("too_big") or [])

    if job.get("file"):
        status = job.get("status")
        label, color = STATUS_LABELS.get(status, (str(status), "#334155"))
        speed = f" / {result['seconds'] // 60}分{result['seconds'] % 60}秒・{result['mib_per_second']} MiB/秒" if result.get("seconds") else ""
        times = f"開始 {job.get('kicked_at', '')[11:16]}" + (f" → 終了 {job['finished_at'][11:16]}" if job.get("finished_at") else "")
        today = (f'<div style="font-weight:bold;color:{color};">{label}</div>'
                 f'<div>{html.escape(job.get("key") or job["file"])}({_gb(job.get("size"))}{speed})</div>'
                 f'<div style="color:#6b7280;font-size:12px;">{times}</div>')
        if job.get("error"):
            today += f'<div style="color:#b91c1c;">{html.escape(job["error"])}</div>'
        headline = {"uploaded": "送信", "already_uploaded": "送信済み", "running": "送信中", "starting": "送信中"}.get(status, "失敗")
        headline = f"{job['file']} {headline}"
    else:
        today = '<div style="color:#6b7280;">送れるファイルがなかったので、今日は送信していません。</div>'
        headline = "送信なし"
    if data.get("error"):
        today += f'<div style="color:#b91c1c;">{html.escape(data["error"])}</div>'
    warnings = "".join(f'<div style="color:#b45309;">⚠ {html.escape(w)}</div>' for w in data.get("warnings") or [])

    sendable = [q for q in queue if q["name"] not in too_big]
    def note(name: str) -> str:
        return "名前を直す必要あり" if name in bad else "大きすぎ(手動で)" if name in too_big else ""
    queue_rows = [[str(i), html.escape(f"{q['prefix']}/{q['name']}"), _gb(q["size"]), note(q["name"])]
                  for i, q in enumerate(queue, start=1)]
    queue_html = (_table(queue_rows, ["順番", "ファイル", "大きさ", "備考"]) if queue
                  else '<div style="color:#6b7280;">送信予定のファイルはありません。</div>')
    done_rows = [[html.escape(f"{d['prefix']}/{d['name']}"), _gb(d["size"]), d["moved_at"][:10]] for d in done]
    done_html = _table(done_rows, ["ファイル", "大きさ", "完了日"]) if done else '<div style="color:#6b7280;">まだありません。</div>'

    summary = (f"送信予定 {len(queue)}件({_gb(sum(q['size'] for q in queue))})"
               f" / 完了 {len(done)}件({_gb(sum(d['size'] for d in done))})")
    if sendable:
        summary += f" / あと{len(sendable)}晩で送り終わる見込み"
    aws_line = (f"AWS上: {listing.get('object_count')}ファイル・{listing.get('total_size_text')}"
                + (f"(保存料金 月約{data['monthly_cost_yen']}円)" if data.get("monthly_cost_yen") is not None else "")
                if listing.get("ok") else "AWSの一覧を取得できませんでした")
    filelist = f'<div style="color:#6b7280;font-size:12px;">一覧の保存先: {html.escape(data["filelist_path"])}</div>' if data.get("filelist_path") else ""

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>AWS Deep Archive 送信状況</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / バケット {html.escape(str(data.get('bucket')))}</div>
        <h3>今日の送信</h3>{today}{warnings}
        <div style="margin-top:8px;font-weight:bold;">{html.escape(summary)}</div>
        <div style="font-weight:bold;">{html.escape(aws_line)}</div>
        <h3>送信予定(次に送る順)</h3>{queue_html}
        <h3>完了</h3>{done_html}
        <h3>AWSのファイル一覧(aws s3 ls)</h3>
        <pre style="background:#f8fafc;border:1px solid #e5e7eb;padding:8px;font-size:12px;white-space:pre-wrap;">{html.escape(listing.get('text') or '-')}</pre>
        {filelist}
      </body>
    </html>
    """
    return body, headline


def run(root: Path) -> None:
    now = datetime.now(JST)
    output_path = root / "output" / "aws_archive_upload_mail.json"
    source = root / "output" / "aws_archive_check.json"
    result = {"module": "aws_archive_upload_mail", "generated_at": now.isoformat(), "status": "ok"}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if not data or data.get("generated_at", "")[:10] != now.strftime("%Y-%m-%d"):
        result.update(status="skipped", reason="aws_archive_check output is missing or not from today.")
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[aws_archive_upload_mail] skipped: no output from today")
        return

    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        result.update(status="error", reason="Missing Gmail settings")
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.warning("[aws_archive_upload_mail] mail skipped: missing Gmail settings")
        return

    body, headline = _body(data, now)
    mark = "❌ " if data.get("status") == "error" else ""
    subject = f"{mark}AWSアーカイブ {now.strftime('%Y-%m-%d')}({headline} / 残り{len(data.get('queue') or [])}件)"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[aws_archive_upload_mail] sent: %s", subject)
    except Exception as exc:
        result.update(status="error", reason=str(exc))
        logging.error("[aws_archive_upload_mail] mail send failed: %s", exc)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
