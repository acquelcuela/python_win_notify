import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.gemini_pricing import GeminiUsageTracker

JST = timezone(timedelta(hours=9), "JST")
DEFAULT_MODEL = "gemini-3.1-flash-lite"
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[ai_summary] invalid JSON ignored: %s", path)
        return None


def _load_config(root: Path) -> dict:
    config = _load_json(root / "config.json")
    return config if isinstance(config, dict) else {}


def _japan_watchlist(root: Path) -> dict | None:
    watchlist = _load_json(root / "output" / "stock_watchlist.json")
    if not watchlist or not watchlist.get("data"):
        return watchlist
    return {
        **watchlist,
        "data": [
            item for item in watchlist["data"]
            if item.get("market") == "japan"
        ],
    }


def _news_payload(root: Path) -> dict | None:
    news = _load_json(root / "output" / "market_news.json")
    if not news or not news.get("data"):
        return news
    return {
        **news,
        "data": [
            {
                "title": item.get("title"),
                "source": item.get("source"),
                "source_group": item.get("source_group"),
                "published_at": item.get("published_at"),
                "query": item.get("query"),
            }
            for item in news["data"]
        ],
    }


def _market_data_payload(root: Path) -> dict:
    return {
        "market": _load_json(root / "output" / "stock_nikkei.json"),
        "japan_watchlist": _japan_watchlist(root),
    }


def _market_news_payload(root: Path) -> dict:
    return {
        "news": _news_payload(root),
    }


def _json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_market_prompt(payload: dict) -> str:
    return f"""
あなたは日本株の前場メモを書くアシスタントです。
以下のJSONだけを根拠に、「ユーザー指定銘柄と市場データからの考察」を作ってください。

対象:
- 日経平均
- TOPIX連動ETF 1306.T
- 日経225先物と日経平均の差
- japan_watchlist に含まれる日本株のみ

前提:
- 日経225先物は夜間の動きを含みやすい参考データです。
- AI考察では、通常の日本株市場比較は日経平均とTOPIX連動ETFを使ってください。
- 日経225先物は、日経平均と比べた先物側の強弱を見るための補助情報として扱ってください。
- TOPIXはTOPIX連動ETF 1306.T を代理指標として使っています。
- TOPIX連動ETFは前営業日の日中取引データであり、夜間取引の結果ではありません。
- 日経225先物とTOPIX連動ETFを直接比較しないでください。
- 投資助言ではなく、状況整理として書いてください。
- 米国株、RYLD、SDIV、ニュース見出しには触れないでください。

出力条件:
- 日本語。
- 3〜5行。
- 上昇/下落が目立つ指定銘柄を具体名で触れる。
- 断定しすぎない。
- HTMLタグは使わない。

JSON:
{_json_text(payload)}
""".strip()


def _build_news_prompt(payload: dict) -> str:
    return f"""
あなたは日本株ニュースの要点メモを書くアシスタントです。
以下のニュース見出しJSONだけを根拠に、「ニュースから見える日本株の動向」を作ってください。

対象:
- 日本株に関するニュース全般
- セクター、業種、テーマ、材料株
- 上がった/下がった/動いた銘柄やテーマ

禁止:
- URLやリンク案内は出さない。
- ユーザー指定銘柄データや日経225先物/TOPIXの数値には触れない。
- 米国株、RYLD、SDIVには触れない。

出力条件:
- 日本語。
- 3〜5行。
- ニュース見出しから読める範囲で、セクターや銘柄の動向を整理する。
- 日経平均やTOPIXの見出しだけに寄せず、個別材料・セクター・レーティングも拾う。
- 不明なことは推測しすぎない。
- HTMLタグは使わない。

JSON:
{_json_text(payload)}
""".strip()


def _call_gemini(api_key: str, model: str, prompt: str) -> tuple[str, dict]:
    """Returns (text, usage_metadata); see modules/gemini_pricing.py."""
    url = API_URL_TEMPLATE.format(model=model)
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc

    candidates = result.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini API returned no candidates.")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    if not text:
        raise RuntimeError("Gemini API returned empty text.")
    return text, (result.get("usageMetadata") or {})


def _build_skipped_payload(generated_at: str) -> dict:
    return {
        "module": "ai_summary",
        "generated_at": generated_at,
        "status": "skipped",
        "reason": "GEMINI_API_KEY is not set.",
        "data": None,
    }


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "ai_summary.json"
    generated_at = datetime.now(JST).isoformat()
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        output_path.write_text(
            json.dumps(_build_skipped_payload(generated_at), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logging.info("[ai_summary] skipped: GEMINI_API_KEY is not set")
        return

    config = _load_config(root).get("ai_summary", {})
    model = str(config.get("model") or DEFAULT_MODEL)
    summaries = {}
    errors = {}
    usage_tracker = GeminiUsageTracker(model)

    tasks = {
        "market_data": _build_market_prompt(_market_data_payload(root)),
        "news": _build_news_prompt(_market_news_payload(root)),
    }

    for key, prompt in tasks.items():
        try:
            text, usage = _call_gemini(api_key=api_key, model=model, prompt=prompt)
            summaries[key] = text
            usage_tracker.add(usage)
            logging.info("[ai_summary] generated %s with %s", key, model)
        except Exception as exc:
            errors[key] = str(exc)
            logging.error("[ai_summary] %s failed: %s", key, exc)

    if summaries:
        payload = {
            "module": "ai_summary",
            "generated_at": generated_at,
            "status": "ok",
            "model": model,
            "data": summaries,
            "gemini_cost_jpy": round(usage_tracker.cost_jpy, 3),
            "gemini_call_count": usage_tracker.call_count,
        }
        if errors:
            payload["warnings"] = errors
    else:
        payload = {
            "module": "ai_summary",
            "generated_at": generated_at,
            "status": "error",
            "model": model,
            "error": errors,
            "data": None,
        }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
