"""Ask Grok (grok.com web UI) a question by driving this PC's Chrome through a
nested `claude -p --chrome` session, and save the result as JSON.

Shared helper, not a scheduled module - import ask_grok() from other modules,
or run it from the command line:

    python -m modules.grok_web "question" --out output/grok_answer.json [--schema schema.json] [--caller name]
    python -m modules.grok_web --usage

Every call is logged in state/grok_web_usage.json, and calls are skipped
(status "skipped") without touching Chrome when they would exceed the limits
in config.json's grok_web section - so several batches can share one Grok
free-tier budget:
  - window_limit / window_hours: free Grok is a rolling quota (roughly 10-20
    messages per 2 hours), so at most window_limit calls in any window_hours.
  - daily_limit: optional cap per JST day (null = none).
  - if Grok itself says the limit was hit, all calls pause for window_hours.

Requirements: Chrome running with the Claude in Chrome extension connected,
grok.com already logged in, and (for Task Scheduler) a task that runs only
while the user is logged on, so it shares the desktop session with Chrome.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9), "JST")

# Page load + typing + waiting for Grok to finish answering. A simple question
# took ~45-60s in testing; long research-style questions take much longer.
DEFAULT_TIMEOUT_SECONDS = 600

# Only the Chrome tools are pre-approved - a scheduled run has nobody to
# answer a permission prompt, but there's no need to skip permissions
# wholesale (--dangerously-skip-permissions) for this.
ALLOWED_TOOLS = "mcp__claude-in-chrome"

ROOT = Path(__file__).resolve().parents[1]
USAGE_PATH = ROOT / "state" / "grok_web_usage.json"
USAGE_KEEP_DAYS = 30

DEFAULT_LIMITS = {"window_limit": 10, "window_hours": 2, "daily_limit": None}


def _now() -> datetime:
    return datetime.now(JST)


def _today() -> str:
    return _now().date().isoformat()


def _load_usage() -> dict:
    try:
        usage = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"days": {}}
    except (json.JSONDecodeError, OSError):
        logging.warning("[grok_web] unreadable usage file ignored: %s", USAGE_PATH)
        return {"days": {}}
    if not isinstance(usage, dict) or not isinstance(usage.get("days"), dict):
        return {"days": {}}
    return usage


def _save_usage(usage: dict) -> None:
    days = usage["days"]
    for day in sorted(days)[:-USAGE_KEEP_DAYS]:
        del days[day]
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")


def get_limits() -> dict:
    """config.json's grok_web limits merged over DEFAULT_LIMITS."""
    limits = dict(DEFAULT_LIMITS)
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return limits
    section = config.get("grok_web") or {}
    for key in limits:
        if key in section and (section[key] is None or isinstance(section[key], (int, float))):
            limits[key] = section[key]
    return limits


def get_today_calls() -> list[dict]:
    """Today's (JST) Grok calls, oldest first."""
    return list(_load_usage()["days"].get(_today(), []))


def get_today_call_count() -> int:
    """How many times Grok was called today (JST), including running/failed ones."""
    return len(get_today_calls())


def _calls_since(usage: dict, since: datetime) -> list[datetime]:
    # A window can span midnight, so look at yesterday's log too.
    starts = []
    for day in sorted(usage["days"])[-2:]:
        for call in usage["days"][day]:
            try:
                started = datetime.fromisoformat(call["started_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if started > since:
                starts.append(started)
    return sorted(starts)


def get_window_call_count(hours: float | None = None) -> int:
    """Calls in the last `hours` (default: config's window_hours)."""
    hours = get_limits()["window_hours"] if hours is None else hours
    return len(_calls_since(_load_usage(), _now() - timedelta(hours=hours)))


def get_usage_status() -> dict:
    """Current counts against every limit, and whether a call may go out now.

    `next_available_at` is when the next call will be allowed (None = now).
    """
    limits = get_limits()
    usage = _load_usage()
    now = _now()
    window = timedelta(hours=limits["window_hours"])
    window_starts = _calls_since(usage, now - window)
    today_count = len(usage["days"].get(_today(), []))

    blocked_until: list[datetime] = []
    reasons: list[str] = []

    if limits["window_limit"] is not None and len(window_starts) >= limits["window_limit"]:
        # The oldest call that has to age out before we're under the limit again.
        free_at = window_starts[len(window_starts) - limits["window_limit"]] + window
        blocked_until.append(free_at)
        reasons.append(
            f"rolling limit reached ({len(window_starts)}/{limits['window_limit']} calls "
            f"in the last {limits['window_hours']}h)"
        )
    if limits["daily_limit"] is not None and today_count >= limits["daily_limit"]:
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), JST)
        blocked_until.append(tomorrow)
        reasons.append(f"daily limit reached ({today_count}/{limits['daily_limit']} calls today)")
    paused = usage.get("rate_limited_until")
    if paused:
        try:
            paused_until = datetime.fromisoformat(paused)
        except ValueError:
            paused_until = None
        if paused_until and paused_until > now:
            blocked_until.append(paused_until)
            reasons.append("Grok reported its usage limit was hit")

    return {
        "now": now.isoformat(),
        "limits": limits,
        "window_calls": len(window_starts),
        "today_calls": today_count,
        "rate_limited_until": paused,
        "can_call": not reasons,
        "reason": "; ".join(reasons) or None,
        "next_available_at": max(blocked_until).isoformat() if blocked_until else None,
    }


def _record_call_start(caller: str | None, question: str) -> tuple[str, int]:
    # Recorded before the result is known, so a run killed mid-way (watchdog,
    # reboot) still counts against the budget - Grok may already have answered.
    usage = _load_usage()
    day = _today()
    calls = usage["days"].setdefault(day, [])
    calls.append({
        "started_at": _now().isoformat(),
        "caller": caller,
        "question": question[:200],
        "status": "running",
    })
    _save_usage(usage)
    return day, len(calls) - 1


def _record_call_end(day: str, index: int, result: dict) -> None:
    usage = _load_usage()
    calls = usage["days"].get(day, [])
    if index < len(calls):
        calls[index]["status"] = result["status"]
        calls[index]["elapsed_seconds"] = result["elapsed_seconds"]
        if result["error"]:
            calls[index]["error"] = str(result["error"])[:300]
    if result.get("rate_limited"):
        hours = get_limits()["window_hours"]
        usage["rate_limited_until"] = (_now() + timedelta(hours=hours)).isoformat()
    _save_usage(usage)


def _find_claude_binary() -> str | None:
    return shutil.which("claude") or shutil.which("claude.cmd")


def _envelope_schema(data_schema: dict | None) -> dict:
    properties = {
        "logged_in": {"type": "boolean"},
        "rate_limited": {"type": "boolean"},
        "answer_text": {"type": "string"},
        "error": {"type": "string"},
    }
    if data_schema is not None:
        properties["data"] = data_schema
    return {"type": "object", "properties": properties, "required": ["logged_in"]}


def _build_prompt(question: str, data_schema: dict | None) -> str:
    data_instruction = (
        "さらに、Grokの回答内容を data フィールドに、そのJSON Schemaの形式で整形して入れてください。"
        "Grok自身がJSONで答えていればそれをそのまま使い、テキストで答えていれば内容をSchemaに合わせてください。\n"
        if data_schema is not None
        else ""
    )
    return (
        "claude-in-chromeでChromeを操作し、新しいタブで https://grok.com を開いてください。\n"
        "ログイン済みかどうかを確認し、未ログイン/セッション切れの場合は操作を試みず、"
        "logged_in=false とし、error に状況を書いて出力してください。\n"
        "利用規約への同意・年齢確認・個人情報の入力などを求められた場合も、代わりに操作せず、"
        "error にその状況を書いて出力してください。\n"
        "Grokが利用上限・回数制限に達した旨のメッセージ(例: 上限に達しました、しばらく待ってから、"
        "アップグレードしてください 等)を表示して回答しなかった場合は、rate_limited=true とし、"
        "error にそのメッセージを書いて出力してください。\n\n"
        "ログイン済みなら、新しい会話で以下の質問をそのまま送信し、Grokの回答が完了するまで待ってから"
        "回答を読み取ってください:\n\n"
        f"--- Grokへの質問(ここから) ---\n{question}\n--- (ここまで) ---\n\n"
        "Grokの回答本文は answer_text にそのまま入れてください。\n"
        f"{data_instruction}"
        "最後に、開いたタブを閉じてから、指定のJSON Schemaの形式で出力してください。"
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    # claude resolves to claude.cmd on Windows, so proc is cmd.exe and the
    # real node process is its child - proc.kill() alone would orphan it.
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=30,
        )
    except Exception:
        proc.kill()


def ask_grok(
    question: str,
    output_path: Path | str,
    *,
    schema: dict | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: Path | str | None = None,
    caller: str | None = None,
    enforce_limits: bool = True,
) -> dict:
    """Asks Grok `question` and writes the result to `output_path` as JSON.

    Always writes the file (status "ok", "error" or "skipped") and returns the
    same dict. With `schema`, Grok's answer is also reshaped into `data`
    matching it. `caller` is recorded in the usage log. Unless
    `enforce_limits` is False, a call that would exceed config.json's
    grok_web limits (see get_usage_status) is skipped without opening Chrome.
    """
    usage_slot: tuple[str, int] | None = None
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(JST)
    start = time.monotonic()

    result: dict = {
        "generated_at": started_at.isoformat(),
        "status": "error",
        "question": question,
        "logged_in": None,
        "answer_text": None,
        "data": None,
        "error": None,
        "elapsed_seconds": None,
        "claude": None,
        "caller": caller,
        "rate_limited": False,
        "usage": None,
    }

    def _finish(**updates) -> dict:
        result.update(updates)
        result["elapsed_seconds"] = round(time.monotonic() - start, 1)
        if usage_slot is not None:
            _record_call_end(*usage_slot, result)
        result["usage"] = get_usage_status()
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if result["status"] == "ok":
            logging.info("[grok_web] answered in %.1fs -> %s", result["elapsed_seconds"], output_path)
        elif result["status"] == "skipped":
            logging.warning("[grok_web] %s", result["error"])
        else:
            logging.error("[grok_web] %s", result["error"])
        return result

    if enforce_limits:
        status = get_usage_status()
        if not status["can_call"]:
            return _finish(
                status="skipped",
                error=f"{status['reason']}; not called. Next call allowed at {status['next_available_at']}.",
            )

    claude_bin = _find_claude_binary()
    if not claude_bin:
        return _finish(error="claude CLI was not found on PATH.")

    # The prompt goes in via stdin, not as an argument: claude is claude.cmd
    # on Windows, and cmd.exe cuts a batch-file argument off at the first
    # newline - silently dropping the rest of the prompt and every flag after
    # it (--chrome, --output-format, ...).
    cmd = [
        claude_bin,
        "-p",
        "--chrome",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(_envelope_schema(schema)),
        "--allowedTools",
        ALLOWED_TOOLS,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(cwd) if cwd else None,
        )
    except Exception as exc:
        return _finish(error=f"failed to launch claude CLI: {exc}")
    usage_slot = _record_call_start(caller, question)

    try:
        stdout, stderr = proc.communicate(_build_prompt(question, schema), timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        return _finish(error=f"claude CLI timed out after {timeout_seconds}s.")

    if proc.returncode != 0:
        return _finish(error=f"claude CLI exited {proc.returncode}: {(stderr or stdout)[:2000]}")

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _finish(error=f"claude CLI output was not JSON: {exc}: {stdout[:2000]}")

    result["claude"] = {
        "num_turns": envelope.get("num_turns"),
        "total_cost_usd": envelope.get("total_cost_usd"),
        "session_id": envelope.get("session_id"),
    }
    if envelope.get("is_error"):
        return _finish(error=f"claude CLI reported an error: {str(envelope.get('result'))[:2000]}")

    output = envelope.get("structured_output")
    if not isinstance(output, dict):
        return _finish(error=f"claude CLI returned no structured output: {str(envelope.get('result'))[:2000]}")

    updates = {
        "rate_limited": output.get("rate_limited") is True,
        "logged_in": output.get("logged_in"),
        "answer_text": output.get("answer_text"),
        "data": output.get("data"),
    }
    if updates["rate_limited"]:
        return _finish(**updates, error=output.get("error") or "Grok's usage limit was hit.")
    if output.get("logged_in") is not True:
        return _finish(**updates, error=output.get("error") or "not logged in to grok.com.")
    if not output.get("answer_text") and output.get("data") is None:
        return _finish(**updates, error=output.get("error") or "Grok's answer could not be read.")
    return _finish(**updates, status="ok", error=output.get("error"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask Grok via Chrome and save the result as JSON.")
    parser.add_argument("question", nargs="?", help="question to send to Grok")
    parser.add_argument("--out", help="output JSON path")
    parser.add_argument("--schema", help="JSON Schema file for the structured `data` field")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--caller", help="name recorded in the daily usage log")
    parser.add_argument("--usage", action="store_true", help="print current usage against the limits and exit")
    args = parser.parse_args()

    if args.usage:
        print(json.dumps({
            **get_usage_status(),
            "calls": get_today_calls(),
        }, ensure_ascii=False, indent=2))
        return 0
    if not args.question or not args.out:
        parser.error("question and --out are required (or use --usage)")

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8")) if args.schema else None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = ask_grok(args.question, args.out, schema=schema, timeout_seconds=args.timeout, caller=args.caller)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
