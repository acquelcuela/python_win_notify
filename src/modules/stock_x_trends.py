from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_MODEL = "grok-4.3"
GROK_API_URL = "https://api.x.ai/v1/responses"
MAX_COMMON_KEYWORDS = 10
MAX_FINDINGS = 8


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[stock_x_trends] invalid JSON ignored: %s", path)
        return None


def _load_config(root: Path) -> dict:
    payload = _load_json(root / "config.json")
    return payload if isinstance(payload, dict) else {}


def _module_config(root: Path) -> dict:
    config = _load_config(root)
    payload = config.get("stock_x_trends", {})
    return payload if isinstance(payload, dict) else {}


def _market_context(root: Path) -> str:
    market_news = _load_json(root / "output" / "market_news.json") or {}
    nikkei = _load_json(root / "output" / "stock_nikkei.json") or {}
    parts: list[str] = []

    if isinstance(nikkei, dict) and nikkei.get("data"):
        data = nikkei["data"]
        indices = data.get("indices") or {"nikkei_futures": data}
        items = []
        for key in ("nikkei_average", "topix", "nikkei_futures"):
            item = indices.get(key)
            if isinstance(item, dict) and item.get("label"):
                change = item.get("change")
                change_pct = item.get("change_pct")
                change_text = "-"
                if change is not None:
                    sign = "+" if change >= 0 else ""
                    change_text = f"{sign}{float(change):,.2f} ({sign}{float(change_pct):.2f}%)"
                items.append(f"{item['label']}: {item.get('current', '-')}, {change_text}")
        if items:
            parts.append("市場データ: " + " / ".join(items))

    if isinstance(market_news, dict) and market_news.get("data"):
        titles = [str(item.get("title") or "").strip() for item in market_news["data"][:10]]
        titles = [title for title in titles if title]
        if titles:
            parts.append("ニュース見出し: " + " / ".join(titles[:8]))

    return "\n".join(parts)


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
    raise ValueError("Grok response did not contain valid JSON.")


def _build_prompt(focus: str, search_terms: list[str], context: str) -> str:
    search_line = " / ".join(search_terms)
    return f"""
日本株に関するX上の投稿を調査してください。
出力は JSON のみです。説明文やコードフェンスは不要です。
朝の寄り付き前から前場中の投稿を優先してください。

調査方針:
- 日本株に関係する投稿だけを対象にする
- 似た表現はまとめる
- 一般的な相場ワードは common_keywords に入れる
- 具体的な銘柄、材料、イベント、需給の変化、決算、レーティング、テーマ変化は discovery_findings に入れる
- discovery_findings は銘柄名・銘柄コード・理由を優先する
- sentiment は strong_positive / positive / neutral / negative のいずれか
- common_keywords は 5〜10件
- discovery_findings は 5〜8件

今回の重点:
{focus}

参考にする検索語:
{search_line}

JSON形式:
{{
  "common_keywords": ["...", "..."],
  "discovery_findings": [
    {{
      "ticker": "銘柄コードまたは空文字",
      "name": "銘柄名またはテーマ名",
      "reason": "なぜ注目されているかを一文で",
      "sentiment": "strong_positive | positive | neutral | negative",
      "source": "X上の投稿要約または見出し",
      "detail": "補足があれば短く"
    }}
  ]
}}

補足メモ:
{context}
""".strip()


def _call_grok(api_key: str, model: str, prompt: str, max_tokens: int) -> dict:
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": "Return only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": max_tokens,
        "tools": [{"type": "x_search"}],
    }
    request = urllib.request.Request(
        GROK_API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Grok API HTTP {exc.code}: {detail}") from exc

    output = payload.get("output") or []
    text = payload.get("output_text") or ""
    if not text and output:
        for item in output:
            for part in item.get("content", []) or []:
                text += str(part.get("text") or part.get("output_text") or "")
    if not text:
        raise RuntimeError("Grok API returned empty content.")
    return _extract_json(text)


def _normalize_payload(data: dict) -> dict:
    keywords: list[str] = []
    source_keywords = data.get("common_keywords") or data.get("trending_keywords") or []
    for item in source_keywords:
        value = str(item).strip()
        if value and value not in keywords:
            keywords.append(value)
    keywords = keywords[:MAX_COMMON_KEYWORDS]

    stock_findings: list[dict] = []
    theme_findings: list[dict] = []
    source_findings = data.get("discovery_findings") or data.get("notable_posts") or []
    for item in source_findings:
        if not isinstance(item, dict):
            continue
        finding = {
            "ticker": str(item.get("ticker") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "sentiment": str(item.get("sentiment") or "neutral").strip(),
            "source": str(item.get("source") or "").strip(),
            "detail": str(item.get("detail") or "").strip(),
        }
        if finding["ticker"]:
            stock_findings.append(finding)
        elif finding["name"] or finding["reason"]:
            theme_findings.append(finding)

    return {
        "common_keywords": keywords,
        "stock_findings": stock_findings[:MAX_FINDINGS],
        "theme_findings": theme_findings[:MAX_FINDINGS],
        "discovery_findings": theme_findings[:MAX_FINDINGS],
        "trending_keywords": keywords,
        "notable_posts": theme_findings[:5],
    }


def _merge_payload(base: dict, extra: dict) -> dict:
    merged_keywords: list[str] = []
    for source in (
        base.get("common_keywords") or [],
        extra.get("common_keywords") or [],
    ):
        for value in source:
            if value and value not in merged_keywords:
                merged_keywords.append(value)

    def _merge_items(sources: list[list[dict]]) -> list[dict]:
        merged_items: list[dict] = []
        seen_keys: set[tuple[str, str]] = set()
        for source in sources:
            for item in source:
                key = (str(item.get("ticker") or "").strip(), str(item.get("name") or "").strip())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged_items.append(item)
        return merged_items

    merged_stock_findings = _merge_items(
        [base.get("stock_findings") or [], extra.get("stock_findings") or []]
    )
    merged_theme_findings = _merge_items(
        [base.get("theme_findings") or [], extra.get("theme_findings") or []]
    )

    return {
        "common_keywords": merged_keywords[:MAX_COMMON_KEYWORDS],
        "stock_findings": merged_stock_findings[:MAX_FINDINGS],
        "theme_findings": merged_theme_findings[:MAX_FINDINGS],
        "discovery_findings": merged_theme_findings[:MAX_FINDINGS],
        "trending_keywords": merged_keywords[:MAX_COMMON_KEYWORDS],
        "notable_posts": merged_theme_findings[:5],
    }


def _needs_more_passes(payload: dict) -> bool:
    findings = (payload.get("stock_findings") or []) + (payload.get("theme_findings") or [])
    if len(findings) < 4:
        return True
    specific_count = 0
    for item in findings:
        ticker = str(item.get("ticker") or "").strip()
        name = str(item.get("name") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if (ticker or name) and len(reason) >= 12:
            specific_count += 1
    return specific_count < 3


def _search_passes() -> list[tuple[str, list[str], str]]:
    return [
        (
            "broad",
            ["急騰", "爆上げ", "仕掛け", "上がりそう"],
            "今日のXで急騰や仕掛けとして話題になっている日本株を、朝の寄り付き前から前場中を優先して広く拾う。",
        ),
        (
            "momentum",
            ["材料出た", "IR", "決算", "上方修正"],
            "今日のXで材料、IR、決算、上方修正をきっかけに話題になっている日本株を、銘柄名・材料の内容・盛り上がり度合いで拾う。",
        ),
        (
            "catalyst",
            ["出来高急増", "板が厚い", "仕込み時", "次の主役"],
            "今日のXで出来高急増、板の厚さ、仕込み時、次の主役として言及されている低位株・小型株を、短期トレーダーの投稿優先で拾う。",
        ),
    ]


def _run_grok_searches(api_key: str, model: str, max_tokens: int, context: str) -> tuple[dict, list[dict]]:
    passes_used: list[dict] = []
    merged: dict | None = None

    for index, (name, search_terms, focus) in enumerate(_search_passes(), start=1):
        prompt = _build_prompt(focus, search_terms, context)
        data = _normalize_payload(_call_grok(api_key, model, prompt, max_tokens))
        passes_used.append(
            {
                "name": name,
                "search_terms": search_terms,
                "common_keywords": len(data["common_keywords"]),
                "discovery_findings": len(data["discovery_findings"]),
            }
        )

        if merged is None:
            merged = data
            if not _needs_more_passes(merged):
                break
            continue

        merged = _merge_payload(merged, data)
        if not _needs_more_passes(merged):
            break

        # Run only the next pass when the current result is still weak.
        if index >= 2 and not _needs_more_passes(merged):
            break

    if merged is None:
        raise RuntimeError("Grok search returned no payload.")
    return merged, passes_used


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_x_trends.json"
    generated_at = datetime.now(JST).isoformat()

    config = _module_config(root)
    enabled = bool(config.get("enabled", False))
    model = str(config.get("model") or DEFAULT_MODEL)
    max_tokens = int(config.get("max_tokens", 1000))
    api_key = os.getenv("GROK_API_KEY", "").strip()
    run_times = [
        str(value).strip()
        for value in config.get("run_times", ["07:00"])
        if str(value).strip()
    ] if isinstance(config.get("run_times", ["07:00"]), list) else ["07:00"]
    schedule_key = os.getenv("BATCH_SCHEDULE_KEY", "").strip()

    if run_times and schedule_key and schedule_key not in run_times:
        # Only search once (in the early morning by default); later batch
        # runs skip without touching output_path, so report_html keeps
        # showing this morning's results instead of nothing.
        logging.info(
            "[stock_x_trends] skipped: schedule %s not in run_times %s (keeping existing output)",
            schedule_key,
            run_times,
        )
        return

    if not enabled:
        payload = {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "stock_x_trends is disabled in config.json.",
            "data": None,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_x_trends] skipped: disabled in config.json")
        return

    if not api_key:
        payload = {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "GROK_API_KEY is not set.",
            "data": None,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_x_trends] skipped: GROK_API_KEY is not set")
        return

    context = _market_context(root)

    try:
        data, passes_used = _run_grok_searches(api_key, model, max_tokens, context)
        payload = {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "ok",
            "model": model,
            "search_passes": passes_used,
            "data": data,
        }
        logging.info(
            "[stock_x_trends] collected %s common keywords, %s stock findings and %s theme findings using %s pass(es)",
            len(data["common_keywords"]),
            len(data["stock_findings"]),
            len(data["theme_findings"]),
            len(passes_used),
        )
    except Exception as exc:
        payload = {
            "module": "stock_x_trends",
            "generated_at": generated_at,
            "status": "error",
            "model": model,
            "error": str(exc),
            "data": None,
        }
        logging.error("[stock_x_trends] failed: %s", exc)

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
