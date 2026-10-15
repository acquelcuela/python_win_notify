"""Sends one file a night from the "to send" folders to S3 Glacier Deep Archive
(user request 2026-10-14, spec: inbox/aws_deep_archive_backup_requirements_spec_20261014.md).

How a night goes (kick + background job + later check, 2026-10-15 - an upload
of a 4.4GB DVD takes ~100 min at the measured 0.75MiB/s, longer than the
batch should wait):
- 01:00 run() (this module): picks the first file in folder order, then name
  order, writes state/aws_archive_job.json and starts `python -m
  modules.aws_archive_upload --job` as a separate background process. The
  batch returns right away. If the previous job is still running, nothing new
  is started.
- job_main(): `aws s3 cp <file> s3://<bucket>/<prefix>/<name> --storage-class
  DEEP_ARCHIVE` (AWS CLI, no GUI tools, per the spec), checks the object size
  with `aws s3 ls`, moves the file to done_dir/<queue folder name>/ and
  records the result in the state file and state/aws_archive_upload_log.json.
  It times out at upload_timeout_seconds (12600s: before the 05:00 router reset).
- 05:30 aws_archive_check + aws_archive_upload_mail: report the job's state,
  the send/done folders and `aws s3 ls`.

No overwrites: an object with the same key is never re-sent. If it is already
there with the same size, the file is just moved to done.

Bucket and local paths are personal, so they live in config.local.json.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.local_config import load_config

JST = timezone(timedelta(hours=9), "JST")
DEFAULT_AWS_CLI = r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"
# Spec 3.4: never send OS junk files.
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
HISTORY_PATH = Path("state") / "aws_archive_upload_log.json"
# Deep Archive storage price (us-east-1) and the yen rate used in the spec.
PRICE_USD_PER_GB_MONTH = 0.00099
YEN_PER_USD = 160


def _settings(root: Path) -> dict:
    return load_config(root).get("aws_archive_upload") or {}


def _aws(cli: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cli, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )


def _remote_size(cli: str, bucket: str, key: str) -> int | None:
    """Size of s3://bucket/key, or None if there is no such object."""
    proc = _aws(cli, ["s3", "ls", f"s3://{bucket}/{key}"], 120)
    name = key.rsplit("/", 1)[-1]
    for line in proc.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[3] == name:
            return int(parts[2])
    return None


def _queue(folders: list[dict]) -> tuple[list[dict], list[dict]]:
    """(files that can be sent, files skipped because of their name). The
    spec's lowercase-only name rule was dropped at the user's request
    (2026-10-15), so names are sent as they are and the second list is empty."""
    ready, bad_names = [], []
    for folder in folders:
        directory = Path(folder["queue"])
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            if not path.is_file() or path.name.startswith("._") or path.name.lower() in JUNK_NAMES:
                continue
            entry = {"prefix": folder["prefix"], "path": str(path), "name": path.name, "size": path.stat().st_size}
            ready.append(entry)
    return ready, bad_names


def _done_files(folders: list[dict], done_dir: Path) -> list[dict]:
    files = []
    for folder in folders:
        directory = done_dir / Path(folder["queue"]).name
        if directory.is_dir():
            files += [
                {"prefix": folder["prefix"], "name": p.name, "size": p.stat().st_size,
                 "moved_at": datetime.fromtimestamp(p.stat().st_mtime, JST).isoformat()}
                for p in sorted(directory.iterdir(), key=lambda p: p.name) if p.is_file()
            ]
    return files


def _move_to_done(item: dict, done_dir: Path) -> str:
    source = Path(item["path"])
    target_dir = done_dir / source.parent.name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    shutil.move(str(source), str(target))
    return str(target)


def _upload_one(cli: str, bucket: str, item: dict, settings: dict, done_dir: Path) -> dict:
    key = f"{item['prefix']}/{item['name']}"
    result = {"file": item["name"], "key": key, "size": item["size"], "status": "error"}
    existing = _remote_size(cli, bucket, key)
    if existing is not None:
        if existing == item["size"]:
            result.update(status="already_uploaded", done_path=_move_to_done(item, done_dir))
        else:
            result["error"] = (f"同じ名前のファイルがAWSに別の大きさ({existing:,}バイト)で既にあります。"
                               "上書きはしないので、名前を変えてください。")
        return result

    started = time.monotonic()
    try:
        proc = _aws(cli, ["s3", "cp", item["path"], f"s3://{bucket}/{key}",
                          "--storage-class", settings.get("storage_class") or "DEEP_ARCHIVE", "--only-show-errors"],
                    int(settings.get("upload_timeout_seconds") or 12600))
    except subprocess.TimeoutExpired:
        result["error"] = "送信が時間内に終わりませんでした(途中の断片は7日後にAWS側で自動削除されます)。明日やり直します。"
        return result
    seconds = time.monotonic() - started
    result["seconds"] = round(seconds)
    result["mib_per_second"] = round(item["size"] / 1048576 / seconds, 2) if seconds > 0 else None
    if proc.returncode != 0:
        result["error"] = f"aws s3 cp が失敗しました(終了コード {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:800]}"
        return result
    uploaded = _remote_size(cli, bucket, key)
    if uploaded != item["size"]:
        result["error"] = f"送信後の確認で大きさが合いません(手元 {item['size']:,} / AWS {uploaded})。ファイルは送信予定に残します。"
        return result
    result.update(status="uploaded", done_path=_move_to_done(item, done_dir))
    return result


def _bucket_listing(cli: str, bucket: str) -> dict:
    proc = _aws(cli, ["s3", "ls", f"s3://{bucket}/", "--recursive", "--human-readable", "--summarize"], 300)
    total = re.search(r"Total Size:\s*([\d.]+)\s*(\w+)", proc.stdout)
    count = re.search(r"Total Objects:\s*(\d+)", proc.stdout)
    raw = _aws(cli, ["s3", "ls", f"s3://{bucket}/", "--recursive", "--summarize"], 300).stdout
    total_bytes = re.search(r"Total Size:\s*(\d+)", raw)
    return {
        "ok": proc.returncode == 0,
        "text": proc.stdout.strip() or proc.stderr.strip(),
        "object_count": int(count.group(1)) if count else None,
        "total_size_text": f"{total.group(1)} {total.group(2)}" if total else None,
        "total_bytes": int(total_bytes.group(1)) if total_bytes else None,
    }


def _save_filelist(directory: str | None, listing: dict, now: datetime) -> str | None:
    if not directory or not listing["ok"]:
        return None
    path = Path(directory) / f"aws_s3_deep_archive_filelist_{now:%Y%m%d}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(listing["text"] + "\n", encoding="utf-8")
    return str(path)


JOB_STATE_PATH = Path("state") / "aws_archive_job.json"
JOB_LOG_DIR = Path("logs")
STILL_ACTIVE = 259
# Windows process creation flags: run the job on its own, outside the batch's
# console and (if the scheduler's job object allows it) outside its job object,
# so the batch can exit right away and Task Scheduler's time limit no longer
# has to cover an upload of 1-2 hours (user idea 2026-10-15).
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def load_job_state(root: Path) -> dict | None:
    """The last job's state; a "running" job whose process is gone becomes
    "interrupted" (its file is still in send/, so the next kick retries it)."""
    path = root / JOB_STATE_PATH
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if state.get("status") in ("starting", "running") and not _pid_alive(state.get("pid")):
        state["status"] = "interrupted"
        state["error"] = "送信のプロセスが途中で終わっていました(PCの再起動など)。ファイルは送信予定に残っているので、次の晩にやり直します。"
    return state


def _save_job_state(root: Path, state: dict) -> None:
    path = root / JOB_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_history(root: Path, entry: dict) -> None:
    path = root / HISTORY_PATH
    try:
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except json.JSONDecodeError:
        history = []
    history.append(entry)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def _config_problem(settings: dict) -> str | None:
    folders = [f for f in settings.get("folders") or [] if f.get("prefix") and f.get("queue")]
    if not settings.get("bucket") or not folders or not settings.get("done_dir"):
        return "config.local.json の aws_archive_upload に bucket / folders / done_dir がありません。"
    return None


def sendable_queue(settings: dict) -> tuple[list[dict], list[dict]]:
    """(files in send order that fit the daily limit, files over the limit)."""
    folders = [f for f in settings.get("folders") or [] if f.get("prefix") and f.get("queue")]
    max_bytes = int(float(settings.get("max_file_gb") or 5) * 1024 ** 3)
    ready, _ = _queue(folders)
    return [i for i in ready if i["size"] <= max_bytes], [i for i in ready if i["size"] > max_bytes]


def run(root: Path) -> None:
    """01:00 kick: picks tonight's file and starts the upload as a separate
    background process, then returns. aws_archive_check reports later."""
    now = datetime.now(JST)
    settings = _settings(root)
    problem = _config_problem(settings)
    if problem:
        _save_job_state(root, {"status": "error", "kicked_at": now.isoformat(), "error": problem})
        logging.error("[aws_archive_upload] %s", problem)
        return
    previous = load_job_state(root)
    if previous and previous.get("status") in ("starting", "running"):
        logging.warning("[aws_archive_upload] previous upload (%s) is still running; not starting another", previous.get("file"))
        return
    sendable, _too_big = sendable_queue(settings)
    if not sendable:
        _save_job_state(root, {"status": "idle", "kicked_at": now.isoformat(), "file": None})
        logging.info("[aws_archive_upload] nothing to send")
        return

    item = sendable[0]
    state = {"status": "starting", "kicked_at": now.isoformat(), "file": item["name"],
             "key": f"{item['prefix']}/{item['name']}", "size": item["size"], "item": item, "pid": None}
    _save_job_state(root, state)
    log_path = root / JOB_LOG_DIR / f"aws_archive_job_{now:%Y%m%d}.log"
    log_path.parent.mkdir(exist_ok=True)
    command = [sys.executable, "-m", "modules.aws_archive_upload", "--job"]
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    with log_path.open("a", encoding="utf-8") as log_file:
        try:
            proc = subprocess.Popen(command, cwd=str(root), stdin=subprocess.DEVNULL, stdout=log_file,
                                    stderr=subprocess.STDOUT, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
                                    close_fds=True)
        except OSError:
            # The scheduler's job object doesn't allow breakaway: start it
            # detached anyway (it then lives as long as Windows lets the job).
            proc = subprocess.Popen(command, cwd=str(root), stdin=subprocess.DEVNULL, stdout=log_file,
                                    stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    state.update(status="running", pid=proc.pid)
    _save_job_state(root, state)
    logging.info("[aws_archive_upload] started upload job pid=%s for %s (%.2f GB)", proc.pid, state["key"], item["size"] / 1024 ** 3)


def job_main(root: Path) -> None:
    """The background upload job started by run()."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = json.loads((root / JOB_STATE_PATH).read_text(encoding="utf-8"))
    state.update(status="running", pid=os.getpid(), started_at=datetime.now(JST).isoformat())
    _save_job_state(root, state)
    settings = _settings(root)
    cli = settings.get("aws_cli") or DEFAULT_AWS_CLI
    item = state["item"]
    logging.info("[aws_archive_job] sending %s", state["key"])
    try:
        result = _upload_one(cli, settings["bucket"], item, settings, Path(settings["done_dir"]))
    except Exception as exc:  # keep the state file honest whatever happens
        result = {"file": item["name"], "key": state["key"], "size": item["size"], "status": "error",
                  "error": f"送信中に予期しないエラー: {exc}"}
    finished = datetime.now(JST).isoformat()
    state.update(status=result["status"], finished_at=finished, result=result, error=result.get("error"))
    _save_job_state(root, state)
    _append_history(root, {"at": state["kicked_at"], "finished_at": finished, **result})
    logging.info("[aws_archive_job] %s: %s %s", state["key"], result["status"], result.get("error") or "")


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[1]
    if "--job" in sys.argv:
        job_main(project_root)
    else:
        logging.basicConfig(level=logging.INFO)
        run(project_root)
