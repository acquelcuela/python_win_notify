"""Save note.com drafts from instruction files by driving this PC's Chrome
through a nested `claude -p --chrome` session (same approach as grok_web).

Drafts only (user request 2026-10-02): Claude enters the title and body and
saves the draft. Paid settings, images, hashtags and the actual publish are
left to the user - the result mail lists what each instruction file asked for
so they can be finished by hand.

Spends otherwise-unused Claude quota: runs only when the weekly limit resets
within `run_within_hours_before_weekly_reset` hours and the current
session/week usage is below the configured ceilings, and only if there are
instruction files in note_drafts/queue/. Usage is read before and after every
draft (via `claude -p /usage`) so the cost per post can be monitored.

    python -m modules.note_draft_post            # same checks as the batch
    python -m modules.note_draft_post --force    # skip the weekly-reset timing check
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.claude_usage import _fetch_usage_text, _parse_usage
from modules.grok_web import _find_claude_binary, _kill_tree
from modules.mail_gmail import send_html_mail


JST = timezone(timedelta(hours=9), "JST")
DRAFTS_DIR_NAME = "note_drafts"
RUN_LOG_PATH = Path("state") / "note_draft_runs.json"
ALLOWED_TOOLS = "mcp__claude-in-chrome"

DEFAULT_SETTINGS = {
    "run_within_hours_before_weekly_reset": 24,
    "max_week_used_pct": 90,
    "max_session_used_pct": 80,
    "max_posts_per_run": 3,
    "timeout_seconds_per_post": 900,
}

# Shown in the mail as a to-do list for finishing the draft by hand.
MANUAL_FIELD_LABELS = {
    "price": "価格",
    "paid_line": "有料ライン",
    "hashtags": "ハッシュタグ",
    "magazine": "マガジン",
    "images": "画像",
    "memo": "メモ",
}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}
_RESET_RE = re.compile(r"([A-Za-z]{3})\w*\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "logged_in": {"type": "boolean"},
        "draft_saved": {"type": "boolean"},
        "draft_url": {"type": "string"},
        "error": {"type": "string"},
    },
    "required": ["logged_in", "draft_saved"],
}


def _load_settings(root: Path) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return settings
    section = config.get("note_draft_post") or {}
    for key in settings:
        if isinstance(section.get(key), (int, float)):
            settings[key] = section[key]
    return settings


def _parse_reset(text: str | None, now: datetime) -> datetime | None:
    """'Oct 7, 6am (Asia/Tokyo)' -> the next such moment in JST."""
    match = _RESET_RE.search(text or "")
    if not match:
        return None
    month = MONTHS.get(match.group(1)[:3].lower())
    if not month:
        return None
    hour = int(match.group(3)) % 12 + (12 if match.group(5).lower() == "pm" else 0)
    candidate = datetime(now.year, month, int(match.group(2)), hour, int(match.group(4) or 0), tzinfo=JST)
    if candidate < now - timedelta(days=1):
        candidate = candidate.replace(year=now.year + 1)
    return candidate


def _read_usage(root: Path) -> dict:
    try:
        return _parse_usage(_fetch_usage_text(root))
    except Exception as exc:
        logging.warning("[note_draft_post] usage check failed: %s", exc)
        return {}


def _usage_pct(usage: dict, key: str) -> float | None:
    return (usage.get(key) or {}).get("used_pct")


def _usage_block_reason(usage: dict, settings: dict) -> str | None:
    week, session = _usage_pct(usage, "week"), _usage_pct(usage, "session")
    if week is None or session is None:
        return "Claude usage could not be read."
    if week >= settings["max_week_used_pct"]:
        return f"weekly usage {week:.0f}% >= {settings['max_week_used_pct']}%."
    if session >= settings["max_session_used_pct"]:
        return f"session usage {session:.0f}% >= {settings['max_session_used_pct']}%."
    return None


def _parse_instruction(path: Path) -> tuple[dict, str]:
    """Front matter (simple `key: value` lines between --- markers) + body."""
    text = path.read_text(encoding="utf-8-sig")
    meta: dict = {}
    body = text
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            for line in parts[0].splitlines()[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            body = parts[1].lstrip("-").lstrip("\r\n")
    return meta, body.strip()


def _build_prompt(title: str, body: str) -> str:
    return (
        "claude-in-chromeでChromeを操作し、新しいタブで https://note.com を開いてください。\n"
        "ログイン済みかどうかを確認し、未ログイン/セッション切れの場合は操作を試みず、"
        "logged_in=false とし、error に状況を書いて出力してください。\n\n"
        "ログイン済みなら、テキスト記事の新規作成画面を開き、以下のタイトルと本文を入力して"
        "「下書き保存」してください。\n"
        "- 本文中の Markdown の見出し(## など)・箇条書き・太字は、noteエディタの対応する書式にしてください。\n"
        "- 本文の文言は一字一句変えないでください。要約・補足・言い換えは禁止です。\n"
        "- **絶対に公開しないでください。**「公開に進む」「投稿する」などの公開系ボタンは押さないこと。"
        "価格・有料ライン・ハッシュタグ・画像・マガジンの設定も行わないでください(ユーザーが後で行います)。\n"
        "- 想定外の画面(規約同意・確認ダイアログ・エラー等)が出たら、無理に進めず error に状況を書いてください。\n\n"
        f"--- タイトル(ここから) ---\n{title}\n--- (ここまで) ---\n\n"
        f"--- 本文(ここから) ---\n{body}\n--- (ここまで) ---\n\n"
        "下書き保存できたら、その下書きの編集画面のURLを draft_url に入れ、draft_saved=true としてください。"
        "保存できなかった場合は draft_saved=false とし、error に理由を書いてください。\n"
        "最後に、開いたタブを閉じてから、指定のJSON Schemaの形式で出力してください。"
    )


def _save_draft(title: str, body: str, root: Path, timeout_seconds: int) -> dict:
    claude_bin = _find_claude_binary()
    if not claude_bin:
        return {"ok": False, "error": "claude CLI was not found on PATH."}
    # Prompt via stdin - see grok_web: cmd.exe truncates batch-file args at newlines.
    cmd = [
        claude_bin, "-p", "--chrome",
        "--output-format", "json",
        "--json-schema", json.dumps(RESULT_SCHEMA),
        "--allowedTools", ALLOWED_TOOLS,
        "--no-session-persistence",
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(root),
        )
    except Exception as exc:
        return {"ok": False, "error": f"failed to launch claude CLI: {exc}"}
    try:
        stdout, stderr = proc.communicate(_build_prompt(title, body), timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        return {"ok": False, "error": f"claude CLI timed out after {timeout_seconds}s."}
    if proc.returncode != 0:
        return {"ok": False, "error": f"claude CLI exited {proc.returncode}: {(stderr or stdout)[:1000]}"}
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"claude CLI output was not JSON: {exc}: {stdout[:1000]}"}
    claude_info = {"num_turns": envelope.get("num_turns"), "total_cost_usd": envelope.get("total_cost_usd")}
    output = envelope.get("structured_output")
    if envelope.get("is_error") or not isinstance(output, dict):
        return {"ok": False, "claude": claude_info, "error": f"claude reported: {str(envelope.get('result'))[:1000]}"}
    if output.get("logged_in") is not True:
        return {"ok": False, "claude": claude_info, "error": output.get("error") or "not logged in to note.com."}
    if output.get("draft_saved") is not True:
        return {"ok": False, "claude": claude_info, "error": output.get("error") or "draft was not saved."}
    return {"ok": True, "claude": claude_info, "draft_url": output.get("draft_url"), "error": output.get("error")}


def _append_run_log(root: Path, entry: dict) -> None:
    path = root / RUN_LOG_PATH
    try:
        runs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        runs = []
    runs.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(runs[-200:], ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt_delta(before: dict, after: dict, key: str) -> str:
    b, a = _usage_pct(before, key), _usage_pct(after, key)
    if b is None or a is None:
        return "-"
    return f"{b:.0f}% → {a:.0f}% (+{a - b:.0f})"


def _send_mail(results: list[dict], now: datetime) -> None:
    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        logging.warning("[note_draft_post] mail skipped: missing Gmail settings")
        return

    cards = []
    for r in results:
        ok = r["status"] == "ok"
        url = r.get("draft_url")
        link = f'<a href="{html.escape(url)}">{html.escape(url)}</a>' if url else "-"
        todo = "".join(
            f"<li>{label}: {html.escape(r['meta'][key])}</li>"
            for key, label in MANUAL_FIELD_LABELS.items()
            if r["meta"].get(key)
        )
        cards.append(f"""
        <div style="border:1px solid {'#6ee7b7' if ok else '#fca5a5'};border-radius:8px;padding:10px;margin-top:10px;">
          <div style="font-weight:bold;">{'✅ 下書き保存' if ok else '❌ 失敗'}: {html.escape(r['title'])}</div>
          <div style="font-size:12px;color:#6b7280;">{html.escape(r['file'])} / {r['elapsed_seconds']:.0f}秒</div>
          <div>下書き: {link}</div>
          {f'<div style="color:#b91c1c;">エラー: {html.escape(r["error"])}</div>' if r.get("error") else ''}
          <div style="margin-top:6px;font-size:13px;">消費: セッション {_fmt_delta(r['usage_before'], r['usage_after'], 'session')} /
            週次 {_fmt_delta(r['usage_before'], r['usage_after'], 'week')}</div>
          {f'<div style="margin-top:6px;font-size:13px;">手動で仕上げる項目:<ul>{todo}</ul></div>' if ok and todo else ''}
        </div>
        """)
    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>note 下書き自動保存</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST</div>
        {''.join(cards)}
      </body>
    </html>
    """
    ok_count = sum(1 for r in results if r["status"] == "ok")
    subject = f"note下書き {ok_count}/{len(results)}件 {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[note_draft_post] sent mail")
    except Exception as exc:
        logging.error("[note_draft_post] mail send failed: %s", exc)


def run(root: Path, force: bool = False) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "note_draft_post.json"
    now = datetime.now(JST)
    settings = _load_settings(root)

    def _write(status: str, **extra) -> None:
        payload = {"module": "note_draft_post", "generated_at": now.isoformat(), "status": status, **extra}
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    drafts_dir = root / DRAFTS_DIR_NAME
    queue_dir = drafts_dir / "queue"
    # "_"-prefixed files (e.g. _template.md) are never posted.
    queue = sorted(p for p in queue_dir.glob("*.md") if not p.name.startswith("_")) if queue_dir.exists() else []
    if not queue:
        _write("skipped", reason="No instruction files in note_drafts/queue.")
        logging.info("[note_draft_post] skipped: queue is empty")
        return

    usage = _read_usage(root)
    if not force:
        reset_at = _parse_reset((usage.get("week") or {}).get("resets"), now)
        if reset_at is None:
            _write("skipped", reason="Weekly reset time could not be read.", usage=usage)
            logging.warning("[note_draft_post] skipped: weekly reset time unknown")
            return
        hours_left = (reset_at - now).total_seconds() / 3600
        if hours_left > settings["run_within_hours_before_weekly_reset"]:
            _write("skipped", reason=f"Weekly reset is {hours_left:.1f}h away.", usage=usage)
            logging.info("[note_draft_post] skipped: weekly reset is %.1fh away", hours_left)
            return

    results = []
    for path in queue[: int(settings["max_posts_per_run"])]:
        block = _usage_block_reason(usage, settings)
        if block:
            logging.info("[note_draft_post] stopping: %s", block)
            break
        meta, body = _parse_instruction(path)
        title = meta.get("title") or path.stem
        start = time.monotonic()
        if not body:
            outcome = {"ok": False, "error": "instruction file has no body."}
        else:
            outcome = _save_draft(title, body, root, int(settings["timeout_seconds_per_post"]))
        usage_after = _read_usage(root)
        result = {
            "file": path.name,
            "title": title,
            "meta": meta,
            "status": "ok" if outcome["ok"] else "error",
            "draft_url": outcome.get("draft_url"),
            "error": outcome.get("error"),
            "claude": outcome.get("claude"),
            "elapsed_seconds": time.monotonic() - start,
            "usage_before": usage,
            "usage_after": usage_after,
            "finished_at": datetime.now(JST).isoformat(),
        }
        results.append(result)
        _append_run_log(root, result)
        # Failed files go to failed/ rather than staying queued, so a
        # half-entered draft isn't silently created again next week.
        dest_dir = drafts_dir / ("done" if outcome["ok"] else "failed")
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / f"{now.strftime('%Y%m%d')}_{path.name}"))
        logging.info("[note_draft_post] %s: %s", path.name, result["status"])
        usage = usage_after

    if not results:
        _write("skipped", reason=_usage_block_reason(usage, settings), usage=usage)
        return
    _write("ok", results=results)
    _send_mail(results, now)


def main() -> int:
    parser = argparse.ArgumentParser(description="Save note.com drafts from note_drafts/queue.")
    parser.add_argument("--force", action="store_true", help="skip the weekly-reset timing check")
    args = parser.parse_args()
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(root, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
