"""Nightly read of ANA Mobile's remaining data (22:00, user request 2026-11-23).

The member page (https://anamobile.ana.co.jp/member) needs the ANA Mileage
Club login, so it is read through the user's own logged-in Chrome with one
short `claude -p --chrome` run (same pattern as login_check's Grok check).
Only the data figures are read; no setting (高速通信/5G ON/OFF, data charge,
plan change) is touched.

Writes output/ana_mobile_data.json and appends to state/ana_mobile_data_history.json
so ana_mobile_data_mail can show how much was used since the previous day.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.grok_web import ALLOWED_TOOLS, _find_claude_binary, _kill_tree

JST = timezone(timedelta(hours=9), "JST")
TIMEOUT_SECONDS = 300
HISTORY_KEEP_DAYS = 120
MEMBER_URL = "https://anamobile.ana.co.jp/member"

SCHEMA = {
    "type": "object",
    "properties": {
        "logged_in": {"type": "boolean"},
        "total_remaining_gb": {"type": ["number", "null"]},
        "plan_gb": {"type": ["number", "null"]},
        "by_expiry": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "gb": {"type": "number"}},
                "required": ["label", "gb"],
            },
        },
        "high_speed": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
    },
    "required": ["logged_in", "total_remaining_gb"],
}

PROMPT = (
    "claude-in-chromeでChromeを操作し、新しいタブで " + MEMBER_URL + " を開いてください(ANAモバイルのマイページ)。\n"
    "画面は読み込みに数秒かかるので、表示されるまで待ってください。\n"
    "「データ残容量」の欄が見えない場合は、左のメニューの「ご利用明細」を押してください。\n"
    "「有効期限ごとのデータ残容量」を押すと、今月・翌月・2か月後などの期限ごとの残量が開きます。\n"
    "読み取るもの:\n"
    "- total_remaining_gb: 「データ合計」のGB数(例: データ合計1.51GB → 1.51)\n"
    "- by_expiry: 有効期限ごとの残量を表示順に [{label: '今月', gb: 0.0}, ...]\n"
    "- plan_gb: ご契約内容の「データ容量」(例: 10GB → 10)\n"
    "- high_speed: 「高速通信：ON」なら 'ON'、OFFなら 'OFF'\n"
    "見るだけにしてください。高速通信や5GのON/OFF、データチャージ、容量変更、同意ボタンなどは絶対に押さないでください。"
    "ログインもしないでください。\n"
    "ログイン画面が出た・セッション切れのときは logged_in=false、total_remaining_gb=null とし、error に状況を短く書いてください。\n"
    "氏名・電話番号・お客様番号は出力に含めないでください。\n"
    "最後に、開いたタブを閉じてから、指定のJSON Schemaの形式で出力してください。"
)


def fetch(claude_bin: str, cwd: Path) -> dict:
    cmd = [claude_bin, "-p", "--chrome", "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--allowedTools", ALLOWED_TOOLS]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", cwd=str(cwd))
    except OSError as exc:
        return {"ok": False, "error": f"claude CLI を起動できませんでした: {exc}"}
    try:
        stdout, stderr = proc.communicate(PROMPT, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        return {"ok": False, "error": f"{TIMEOUT_SECONDS}秒以内にマイページを読み取れませんでした(Chromeの接続を確認してください)。"}
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"読み取りに失敗しました(終了コード {proc.returncode}): {(stderr or stdout)[:500]}"}
    output = envelope.get("structured_output")
    if envelope.get("is_error") or not isinstance(output, dict):
        return {"ok": False, "error": f"読み取りに失敗しました: {str(envelope.get('result'))[:500]}",
                "cost_usd": envelope.get("total_cost_usd")}
    total = output.get("total_remaining_gb")
    ok = output.get("logged_in") is True and isinstance(total, (int, float))
    return {
        "ok": ok,
        "logged_in": output.get("logged_in") is True,
        "total_remaining_gb": total,
        "plan_gb": output.get("plan_gb"),
        "by_expiry": output.get("by_expiry") or [],
        "high_speed": output.get("high_speed"),
        "error": None if ok else (output.get("error") or "データ残容量を読み取れませんでした。"),
        "cost_usd": envelope.get("total_cost_usd"),
    }


def _update_history(path: Path, now: datetime, total: float) -> list[dict]:
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        history = []
    today = now.strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date") != today]
    history.append({"date": today, "total_remaining_gb": total, "at": now.isoformat()})
    cutoff = (now - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    history = sorted((h for h in history if h.get("date", "") >= cutoff), key=lambda h: h["date"])
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return history


def run(root: Path) -> None:
    now = datetime.now(JST)
    output_path = root / "output" / "ana_mobile_data.json"
    output_path.parent.mkdir(exist_ok=True)
    claude_bin = _find_claude_binary()
    result = fetch(claude_bin, root) if claude_bin else {"ok": False, "error": "claude CLI が見つかりません。"}
    previous = None
    if result.get("ok"):
        history = _update_history(root / "state" / "ana_mobile_data_history.json", now, result["total_remaining_gb"])
        earlier = [h for h in history if h["date"] < now.strftime("%Y-%m-%d")]
        previous = earlier[-1] if earlier else None
    payload = {
        "module": "ana_mobile_data",
        "generated_at": now.isoformat(),
        "status": "ok" if result.get("ok") else "problem",
        "data": result,
        "previous": previous,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[ana_mobile_data] ok=%s total=%sGB plan=%sGB%s", result.get("ok"), result.get("total_remaining_gb"),
                 result.get("plan_gb"), f" ({result.get('error')})" if result.get("error") else "")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run(Path(__file__).resolve().parents[1])
