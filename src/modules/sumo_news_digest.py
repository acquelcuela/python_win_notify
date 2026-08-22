from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.ai_summary import _call_gemini
from modules.gemini_pricing import GeminiUsageTracker
from modules.mail_gmail import send_html_mail
from modules.sumo_news import HISTORY_DIR_NAME, _dedupe_stories


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_DIGEST_DAYS = [10, 20, 30]
DEFAULT_LOOKBACK_DAYS = 10
DEFAULT_MODEL = "gemini-3.1-flash-lite"
CONFIG_PATH = Path("sumo_news_digest_config.json")


def _load_config(root: Path) -> dict:
    config_path = root / CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[sumo_news_digest] %s is invalid; using defaults.", CONFIG_PATH)
        return {}
    return config if isinstance(config, dict) else {}


def _is_scheduled_today(config: dict, now: datetime) -> bool:
    digest_days = config.get("digest_days") or DEFAULT_DIGEST_DAYS
    return now.day in {int(d) for d in digest_days}


def _collect_history_items(root: Path, lookback_days: int, now: datetime) -> list[dict]:
    history_dir = root / "output" / HISTORY_DIR_NAME
    if not history_dir.exists():
        return []

    cutoff_date = (now - timedelta(days=lookback_days)).date()
    all_items = []
    for path in sorted(history_dir.glob("sumo_news_*.json")):
        date_str = path.stem.split("_")[-1]
        try:
            file_date = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            continue
        if file_date < cutoff_date:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        all_items.extend(payload.get("data") or [])

    all_items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return _dedupe_stories(all_items)


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        payload = json.loads(match.group(0))
        if isinstance(payload, dict):
            return payload
    raise ValueError("Gemini response did not contain valid JSON.")


def _build_prompt(items: list[dict], lookback_days: int) -> str:
    lines = []
    for index, item in enumerate(items[:80]):
        wrestler = item.get("wrestler") or "その他"
        lines.append(f"[{index}] ({wrestler}) {item.get('title', '')}")
    items_text = "\n".join(lines) or "(直近の記事なし)"

    return f"""
以下は直近{lookback_days}日間の大相撲関連ニュースの見出し一覧です(力士名でタグ付け済み、
先頭の[番号]は記事のインデックス)。これを読んで、この期間の大相撲界のできごとを
ニュース記事のように「見出し」と「本文」のセットでまとめてください。

まとめの方針:
- 話題ごとに1トピックとしてまとめる(同じできごとを報じている記事は1トピックに統合する)
- 重要度の高い話題(番付・怪我・引退・優勝争いなど)を優先する
- 5〜10トピック程度
- 見出しは15〜25文字程度、本文は2〜4文程度
- 淡々とした事実ベースの記述にする(誇張しない)
- 各トピックについて、内容の根拠にした記事のインデックス番号を1つ選んで source_index に入れる
  (同じ話題を報じる記事が複数あれば、代表的な1つだけを選ぶ)

見出し一覧:
{items_text}

出力はJSONのみ。説明文やコードフェンスは不要です。

JSON形式:
{{
  "topics": [
    {{
      "headline": "見出し",
      "body": "本文(2〜4文)",
      "source_index": 0
    }}
  ]
}}
""".strip()


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "sumo_news_digest.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    config = _load_config(root)

    if not _is_scheduled_today(config, now):
        payload = {
            "module": "sumo_news_digest",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": f"Not a scheduled digest day (digest_days={config.get('digest_days', DEFAULT_DIGEST_DAYS)}).",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[sumo_news_digest] skipped: not a scheduled digest day")
        return

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        payload = {
            "module": "sumo_news_digest",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "GEMINI_API_KEY is not set.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[sumo_news_digest] skipped: GEMINI_API_KEY is not set")
        return

    lookback_days = int(config.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    model = str(config.get("model") or DEFAULT_MODEL)

    items = _collect_history_items(root, lookback_days, now)
    if not items:
        payload = {
            "module": "sumo_news_digest",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No archived sumo_news history found for the lookback window.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[sumo_news_digest] skipped: no history items")
        return

    prompt = _build_prompt(items, lookback_days)
    usage_tracker = GeminiUsageTracker(model)
    try:
        text, usage = _call_gemini(api_key=api_key, model=model, prompt=prompt)
        usage_tracker.add(usage)
        data = _extract_json(text)
        raw_topics = data.get("topics") or []
    except Exception as exc:
        payload = {
            "module": "sumo_news_digest",
            "generated_at": generated_at,
            "status": "error",
            "error": str(exc),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[sumo_news_digest] Gemini call failed: %s", exc)
        return

    # The URL is resolved from our own fetched data by index, never trusted
    # to the model's output - an LLM has no reliable way to know the real
    # URL for a headline it didn't originate, so asking it to emit one
    # directly risks a plausible-looking but wrong link.
    topics = []
    for topic in raw_topics:
        headline = str(topic.get("headline") or "").strip()
        body_text = str(topic.get("body") or "").strip()
        if not headline or not body_text:
            continue
        source_index = topic.get("source_index")
        source_item = items[source_index] if isinstance(source_index, int) and 0 <= source_index < len(items) else None
        topics.append(
            {
                "headline": headline,
                "body": body_text,
                "source_title": source_item.get("title") if source_item else None,
                "source_url": source_item.get("url") if source_item else None,
            }
        )

    payload = {
        "module": "sumo_news_digest",
        "generated_at": generated_at,
        "status": "ok",
        "model": model,
        "cost_jpy": round(usage_tracker.cost_jpy, 2),
        "lookback_days": lookback_days,
        "item_count": len(items),
        "topics": topics,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[sumo_news_digest] summarized %d item(s) into %d topic(s) over %d day(s)", len(items), len(topics), lookback_days)

    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not (gmail_address and app_password and mail_to):
        logging.warning("[sumo_news_digest] mail skipped: missing Gmail settings")
        return

    def _topic_card(topic: dict) -> str:
        headline = html.escape(topic["headline"])
        body_text = html.escape(topic["body"])
        url = topic.get("source_url")
        link_html = (
            f'<div style="margin-top:6px;"><a href="{html.escape(url)}" target="_blank" rel="noopener" style="font-size:12px;color:#2563eb;">情報元</a></div>'
            if url
            else ""
        )
        return f"""
        <div style="margin-top:10px;padding:10px;background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;">
          <div style="font-weight:bold;font-size:15px;">{headline}</div>
          <div style="color:#334155;font-size:13px;margin-top:4px;line-height:1.6;">{body_text}</div>
          {link_html}
        </div>
        """

    topics_html = "".join(_topic_card(t) for t in topics) or '<div style="color:#6b7280;">要約できる話題がありませんでした。</div>'
    body = f"""
    <html>
      <body style="font-family:'Hiragino Sans','Yu Gothic',sans-serif;color:#0f172a;">
        <h2>大相撲ニュース まとめ({lookback_days}日分)</h2>
        <div style="color:#6b7280;font-size:12px;">{now.strftime('%Y-%m-%d %H:%M')} JST時点 / 記事{len(items)}件から{len(topics)}トピックに要約</div>
        {topics_html}
      </body>
    </html>
    """
    subject = f"[NightlyBatchNotify] 大相撲ニュース まとめ {now.strftime('%Y-%m-%d')}"
    try:
        send_html_mail(gmail_address, app_password, mail_to, subject, body)
        logging.info("[sumo_news_digest] sent digest mail")
    except Exception as exc:
        logging.error("[sumo_news_digest] mail send failed: %s", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
