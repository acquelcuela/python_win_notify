"""Morning status check for the nightly Deep Archive upload (2026-10-15).

aws_archive_upload (01:00) only starts the upload as a background job; this
module runs a few hours later, reads the job's state, lists the bucket
(`aws s3 ls --recursive`, also saved to filelist_dir) and the send/done
folders, and writes output/aws_archive_check.json for aws_archive_upload_mail.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from modules.aws_archive_upload import (
    DEFAULT_AWS_CLI, JST, PRICE_USD_PER_GB_MONTH, YEN_PER_USD, HISTORY_PATH,
    _bucket_listing, _config_problem, _done_files, _queue, _save_filelist, _settings,
    load_job_state, sendable_queue,
)


def run(root: Path) -> None:
    now = datetime.now(JST)
    output_path = root / "output" / "aws_archive_check.json"
    output_path.parent.mkdir(exist_ok=True)
    settings = _settings(root)
    payload = {"module": "aws_archive_check", "generated_at": now.isoformat(), "status": "ok",
               "bucket": settings.get("bucket"), "job": load_job_state(root), "warnings": []}
    problem = _config_problem(settings)
    if problem:
        payload.update(status="error", error=problem)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[aws_archive_check] %s", problem)
        return

    folders = [f for f in settings["folders"] if f.get("prefix") and f.get("queue")]
    missing = [f["queue"] for f in folders if not Path(f["queue"]).is_dir()]
    if missing:
        payload["warnings"].append("送信予定のフォルダが見つかりません: " + ", ".join(missing))
    job = payload["job"] or {}
    if job.get("status") in ("error", "interrupted"):
        payload["status"] = "error"
    # A job kicked on an earlier night means tonight's kick didn't start one.
    if job.get("kicked_at", "")[:10] not in (now.strftime("%Y-%m-%d"), ""):
        payload["warnings"].append(f"今夜(01:00)の送信は始まっていません(最後の起動: {job.get('kicked_at', '')[:16]})。")

    sendable, too_big = sendable_queue(settings)
    if too_big:
        payload["warnings"].append(f"1日の上限({settings.get('max_file_gb') or 5}GB)を超えるため送らないファイル: {len(too_big)}件")
    try:
        listing = _bucket_listing(settings.get("aws_cli") or DEFAULT_AWS_CLI, settings["bucket"])
    except (OSError, subprocess.SubprocessError) as exc:
        listing = {"ok": False, "text": f"AWS CLI を実行できませんでした: {exc}", "object_count": None,
                   "total_size_text": None, "total_bytes": None}
    payload["listing"] = listing
    if listing.get("total_bytes") is not None:
        payload["monthly_cost_yen"] = round(listing["total_bytes"] / 1024 ** 3 * PRICE_USD_PER_GB_MONTH * YEN_PER_USD, 1)
    payload["filelist_path"] = _save_filelist(settings.get("filelist_dir"), listing, now)
    ready, _ = _queue(folders)
    payload["queue"] = ready
    payload["sendable_count"] = len(sendable)
    payload["too_big"] = [i["name"] for i in too_big]
    payload["done"] = _done_files(folders, Path(settings["done_dir"]))
    try:
        payload["history"] = json.loads((root / HISTORY_PATH).read_text(encoding="utf-8"))[-30:]
    except (OSError, json.JSONDecodeError):
        payload["history"] = []
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[aws_archive_check] job=%s file=%s, AWS %s objects", job.get("status"), job.get("file"), listing.get("object_count"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(Path(__file__).resolve().parents[1])
