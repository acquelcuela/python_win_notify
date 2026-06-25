import json
import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_QUERIES = [
    "日本株 前場 日経平均 TOPIX",
    "東京株式 前引け 日経平均",
    "東証 前場 セクター 業種別",
    "日本株 材料株 前場 上昇 下落",
    "東京市場 前場 値上がり 値下がり 銘柄",
]
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[market_news] config.json is invalid; default news settings used.")
        return {}


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(JST)


def _fetch_google_news(query: str, source_group: str) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": "ja",
            "gl": "JP",
            "ceid": "JP:ja",
        }
    )
    url = f"{GOOGLE_NEWS_RSS_URL}?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "NightlyBatchNotify/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()

    root = ET.fromstring(body)
    items = []
    for item in root.findall("./channel/item"):
        source = item.find("source")
        published_at = _parse_datetime(item.findtext("pubDate", ""))
        items.append(
            {
                "query": query,
                "source_group": source_group,
                "collector": "google_news",
                "title": item.findtext("title", "").strip(),
                "url": item.findtext("link", "").strip(),
                "source": source.text.strip() if source is not None and source.text else "",
                "published_at": published_at.isoformat() if published_at else None,
            }
        )
    return items


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for item in items:
        key = item.get("title") or item.get("url")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _is_excluded(item: dict, exclude_title_keywords: list[str]) -> bool:
    title = item.get("title") or ""
    return any(keyword and keyword in title for keyword in exclude_title_keywords)


def _news_sources(config: dict) -> list[dict]:
    sources = config.get("sources")
    if isinstance(sources, list) and sources:
        normalized = []
        for index, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                continue
            queries = source.get("queries") or []
            if not isinstance(queries, list):
                continue
            normalized.append(
                {
                    "name": str(source.get("name") or f"source_{index}"),
                    "queries": [str(query) for query in queries if str(query).strip()],
                }
            )
        if normalized:
            return normalized

    queries = config.get("queries") or DEFAULT_QUERIES
    return [
        {
            "name": "Google News",
            "queries": [str(query) for query in queries if str(query).strip()],
        }
    ]


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "market_news.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root).get("market_news", {})
    sources = _news_sources(config)
    max_items = int(config.get("max_items", 8))
    per_source_limit = int(config.get("per_source_limit", max_items))
    lookback_hours = int(config.get("lookback_hours", 18))
    exclude_title_keywords = [
        str(keyword)
        for keyword in config.get("exclude_title_keywords", [])
        if str(keyword).strip()
    ]
    cutoff = datetime.now(JST) - timedelta(hours=lookback_hours)

    all_items = []
    warnings = []
    for source in sources:
        source_items = []
        source_name = source["name"]
        for query in source["queries"]:
            try:
                source_items.extend(_fetch_google_news(query, source_name))
            except Exception as exc:
                warnings.append(f"{source_name} / {query}: {exc}")
                logging.error(
                    "[market_news] fetch failed for source '%s' query '%s': %s",
                    source_name,
                    query,
                    exc,
                )
        source_items = [
            item for item in _dedupe(source_items)
            if not _is_excluded(item, exclude_title_keywords)
        ]
        source_items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
        all_items.extend(source_items[:per_source_limit])

    recent_items = []
    for item in _dedupe(all_items):
        published_text = item.get("published_at")
        if not published_text:
            recent_items.append(item)
            continue
        try:
            published_at = datetime.fromisoformat(published_text)
        except ValueError:
            recent_items.append(item)
            continue
        if published_at >= cutoff:
            recent_items.append(item)

    recent_items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    recent_items = recent_items[:max_items]

    if recent_items:
        payload = {
            "module": "market_news",
            "generated_at": generated_at,
            "status": "ok",
            "source_count": len(sources),
            "data": recent_items,
        }
        if warnings:
            payload["warnings"] = warnings
        logging.info("[market_news] collected %s news items", len(recent_items))
    else:
        payload = {
            "module": "market_news",
            "generated_at": generated_at,
            "status": "error",
            "error": "; ".join(warnings) if warnings else "No recent market news returned.",
            "data": None,
        }
        logging.error("[market_news] no recent news items")

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
