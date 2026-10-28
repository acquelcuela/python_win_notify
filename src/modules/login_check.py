"""Morning check that Claude and Grok are still logged in (06:00, user request
2026-10-28), so a logout is noticed before the day's batches fail on it.

- Claude: `claude auth status` (free, no usage) -> loggedIn / subscriptionType.
- Grok: one short `claude -p --chrome` run that only opens grok.com and reports
  whether it is logged in - no question is sent to Grok, so it doesn't count
  against Grok's quota (it does use a little Claude usage). Skipped when
  Claude itself is logged out. Also catches Chrome / extension problems.

Writes output/login_check.json; login_check_mail sends a mail only when
something is wrong.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.grok_web import ALLOWED_TOOLS, _find_claude_binary, _kill_tree

JST = timezone(timedelta(hours=9), "JST")
GROK_TIMEOUT_SECONDS = 300
GROK_SCHEMA = {
    "type": "object",
    "properties": {"logged_in": {"type": "boolean"}, "error": {"type": "string"}},
    "required": ["logged_in"],
}
GROK_PROMPT = (
    "claude-in-chromeでChromeを操作し、新しいタブで https://grok.com を開いてください。\n"
    "ページが表示されたら、ログイン済みかどうかだけを確認してください。"
    "Grokに質問は送らないでください。ログインやその他の操作もしないでください。\n"
    "ログイン済み(会話の入力欄が使える状態で、ログインやサインアップのボタンが出ていない)なら logged_in=true、"
    "未ログイン・セッション切れなら logged_in=false とし、error に見えた状況を短く書いてください。\n"
    "最後に、開いたタブを閉じてから、指定のJSON Schemaの形式で出力してください。"
)


def check_claude(claude_bin: str) -> dict:
    try:
        proc = subprocess.run([claude_bin, "auth", "status"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        status = json.loads(proc.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"claude auth status を実行できませんでした: {exc}"}
    logged_in = status.get("loggedIn") is True
    return {
        "ok": logged_in,
        "logged_in": logged_in,
        "plan": status.get("subscriptionType"),
        "auth_method": status.get("authMethod"),
        "error": None if logged_in else "Claudeからログアウトしています(claude auth status: loggedIn=false)。",
    }


def check_grok(claude_bin: str, cwd: Path) -> dict:
    cmd = [claude_bin, "-p", "--chrome", "--output-format", "json",
           "--json-schema", json.dumps(GROK_SCHEMA), "--allowedTools", ALLOWED_TOOLS]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", cwd=str(cwd))
    except OSError as exc:
        return {"ok": False, "error": f"claude CLI を起動できませんでした: {exc}"}
    try:
        stdout, stderr = proc.communicate(GROK_PROMPT, timeout=GROK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        return {"ok": False, "error": f"{GROK_TIMEOUT_SECONDS}秒以内に grok.com の確認が終わりませんでした(Chromeの接続を確認してください)。"}
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"確認に失敗しました(終了コード {proc.returncode}): {(stderr or stdout)[:500]}"}
    output = envelope.get("structured_output")
    if envelope.get("is_error") or not isinstance(output, dict):
        return {"ok": False, "error": f"確認に失敗しました: {str(envelope.get('result'))[:500]}",
                "cost_usd": envelope.get("total_cost_usd")}
    logged_in = output.get("logged_in") is True
    return {
        "ok": logged_in,
        "logged_in": logged_in,
        "error": None if logged_in else (output.get("error") or "grok.com からログアウトしています。"),
        "cost_usd": envelope.get("total_cost_usd"),
    }


def run(root: Path) -> None:
    now = datetime.now(JST)
    output_path = root / "output" / "login_check.json"
    output_path.parent.mkdir(exist_ok=True)
    claude_bin = _find_claude_binary()
    if not claude_bin:
        claude = {"ok": False, "error": "claude CLI が見つかりません。"}
    else:
        claude = check_claude(claude_bin)
    if claude.get("ok"):
        grok = check_grok(claude_bin, root)
    else:
        grok = {"ok": None, "error": "Claudeにログインしていないため、Grokは確認できませんでした。"}
    payload = {
        "module": "login_check",
        "generated_at": now.isoformat(),
        "status": "ok" if claude.get("ok") and grok.get("ok") else "problem",
        "claude": claude,
        "grok": grok,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[login_check] claude=%s(%s) grok=%s%s", claude.get("ok"), claude.get("plan"), grok.get("ok"),
                 f" ({grok.get('error')})" if grok.get("error") else "")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(Path(__file__).resolve().parents[1])
