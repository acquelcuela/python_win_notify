from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail
from modules.sumo_banzuke import auto_basho_code
from modules.sumo_news_mail import _hoshitori_table, _id_color_map, _leaderboard, _load_json, _ranked_groups


JST = timezone(timedelta(hours=9), "JST")


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[sumo_basho_final] config.json is invalid.")
        return {}


def _skip(output_path: Path, generated_at: str, reason: str) -> None:
    result = {
        "module": "sumo_basho_final",
        "generated_at": generated_at,
        "status": "skipped",
        "reason": reason,
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "sumo_basho_final.json"
    generated_at = datetime.now(JST).isoformat()

    manual_code = str((_load_config(root).get("sumo_basho") or {}).get("code") or "").strip()
    code = manual_code or auto_basho_code(datetime.now(JST).date())

    # This marker is the only thing that makes the mail/archive a one-time
    # event - once it exists for this basho code, every later run this
    # module skips instead of re-sending. It's only written once the mail
    # has actually gone out (or there are no Gmail settings to send with at
    # all), so a transient SMTP failure gets retried the next day rather
    # than being silently given up on.
    marker_path = root / "state" / f"sumo_basho_final_{code}.json"
    if marker_path.exists():
        _skip(output_path, generated_at, f"already reported for {code} ({marker_path.name} exists).")
        return

    banzuke = _load_json(root / "state" / f"sumo_banzuke_{code}.json")
    hoshitori = _load_json(root / "state" / f"sumo_hoshitori_{code}.json")
    if not banzuke or not hoshitori:
        _skip(output_path, generated_at, "banzuke/hoshitori cache not available yet - run those modules first.")
        return

    start_date = datetime.strptime(banzuke["start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(banzuke["end_date"], "%Y-%m-%d").date()
    final_day_no = (end_date - start_date).days + 1

    days_done = set(hoshitori.get("days_done") or [])
    if final_day_no not in days_done:
        _skip(
            output_path,
            generated_at,
            f"final day ({final_day_no}) results not fetched yet (days_done={sorted(days_done)}).",
        )
        return

    results = hoshitori.get("results") or {}
    title = banzuke.get("title") or code

    # Archive the full banzuke + hoshitori snapshot under output/history/,
    # separate from state/'s per-code cache files - state/ is working data
    # that a future setup could reasonably clear, while output/history/ is
    # this project's established "keep forever" location for past results.
    history_dir = root / "output" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    archive_path = history_dir / f"sumo_basho_final_{code}.json"
    archive_payload = {
        "code": code,
        "title": title,
        "start_date": banzuke["start_date"],
        "end_date": banzuke["end_date"],
        "final_day_no": final_day_no,
        "banzuke": banzuke,
        "hoshitori": hoshitori,
        "archived_at": generated_at,
    }
    archive_path.write_text(json.dumps(archive_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    divisions = (
        (banzuke.get("makuuchi") or [], "幕内"),
        (banzuke.get("juryo") or [], "十両"),
        (banzuke.get("makushita") or [], "幕下"),
    )
    sections = ""
    for entries, label in divisions:
        if not entries:
            continue
        groups, top_records, worst_records = _ranked_groups(entries, results, final_day_no)
        id_colors = _id_color_map(groups, top_records, worst_records)
        sections += _leaderboard(entries, results, final_day_no, label)
        sections += _hoshitori_table(entries, results, final_day_no, label, id_colors)

    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>{html.escape(title)} 最終成績</h2>
        <div style="color:#6b7280;font-size:12px;">千秋楽(全{final_day_no}日目)終了時点</div>
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

    mail_sent = False
    if missing:
        logging.warning("[sumo_basho_final] mail skipped: missing Gmail settings: %s", ", ".join(missing))
    else:
        subject = f"{title} 最終成績"
        try:
            send_html_mail(gmail_address, app_password, mail_to, subject, body)
            mail_sent = True
            logging.info("[sumo_basho_final] sent final results mail for %s", code)
        except Exception as exc:
            logging.error("[sumo_basho_final] mail send failed: %s", exc)

    if mail_sent or missing:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"code": code, "reported_at": generated_at, "mail_sent": mail_sent}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    result = {
        "module": "sumo_basho_final",
        "generated_at": generated_at,
        "status": "ok",
        "reason": f"archived final results for {code} to {archive_path.name}; mail_sent={mail_sent}.",
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
