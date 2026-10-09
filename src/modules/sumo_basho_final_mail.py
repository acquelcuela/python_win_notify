"""sumo_basho_final_mail: output/sumo_basho_final.json と場所の保存データを読んで、最終成績をメールする。

データ(sumo_basho_final)→ JSON → メール(このモジュール)の形(2026-10-09 に分離)。
送信できたら(または Gmail の設定自体がなければ)state/sumo_basho_final_<code>.json の
マーカーを書き、以後その場所は二度と送らない。送信に失敗したらマーカーを書かないので翌日また送る。
"""
from __future__ import annotations

import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.mail_gmail import send_html_mail
from modules.sumo_news_mail import _hoshitori_table, _id_color_map, _leaderboard, _load_json, _ranked_groups

JST = timezone(timedelta(hours=9), "JST")


def run(root: Path) -> None:
    payload = _load_json(root / "output" / "sumo_basho_final.json")
    if not payload or payload.get("status") != "ok":
        logging.info("[sumo_basho_final_mail] skipped: no final results to send")
        return
    code = payload.get("code")
    marker_path = Path(payload.get("marker_path") or root / "state" / f"sumo_basho_final_{code}.json")
    if marker_path.exists():
        logging.info("[sumo_basho_final_mail] skipped: already reported for %s", code)
        return
    archive = _load_json(Path(payload.get("archive_path") or ""))
    if not archive:
        logging.error("[sumo_basho_final_mail] archive not found: %s", payload.get("archive_path"))
        return
    banzuke = archive["banzuke"]
    results = (archive.get("hoshitori") or {}).get("results") or {}
    final_day_no = archive["final_day_no"]
    title = archive.get("title") or code
    generated_at = datetime.now(JST).isoformat()

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
        logging.warning("[sumo_basho_final_mail] mail skipped: missing Gmail settings: %s", ", ".join(missing))
    else:
        subject = f"{title} 最終成績"
        try:
            send_html_mail(gmail_address, app_password, mail_to, subject, body)
            mail_sent = True
            logging.info("[sumo_basho_final_mail] sent final results mail for %s", code)
        except Exception as exc:
            logging.error("[sumo_basho_final_mail] mail send failed: %s", exc)

    if mail_sent or missing:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"code": code, "reported_at": generated_at, "mail_sent": mail_sent}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )



if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(root)
