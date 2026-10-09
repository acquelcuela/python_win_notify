from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.sumo_banzuke import auto_basho_code
from modules.sumo_news_mail import _load_json


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
    # module skips instead of re-sending. sumo_basho_final_mail writes it
    # only once the mail has actually gone out (or there are no Gmail
    # settings to send with at all), so a transient SMTP failure gets
    # retried the next day rather than being silently given up on.
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

    # sumo_basho_final_mail reads this and the archive, sends the mail, and
    # only then writes the marker - so a failed send is retried the next day.
    result = {
        "module": "sumo_basho_final",
        "generated_at": generated_at,
        "status": "ok",
        "code": code,
        "title": title,
        "final_day_no": final_day_no,
        "archive_path": str(archive_path),
        "marker_path": str(marker_path),
        "reason": f"archived final results for {code} to {archive_path.name}.",
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
