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

# Only show headlines not already shown in an earlier run (user request
# 2026-10-02 - the 5 weekday reports kept repeating the same 18h of news).
# The seen log does the dedup; the lookback still stretches back to the
# previous run (plus a margin for RSS indexing lag) so Monday 07:00 picks up
# the weekend since Friday 23:00 instead of only the last 18h.
SEEN_LOG_PATH = Path("state") / "market_news_seen.json"
SEEN_LOG_RETENTION_DAYS = 7
LAST_RUN_MARGIN_HOURS = 1
MAX_LOOKBACK_HOURS = 96


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


def _load_seen(root: Path) -> dict:
    path = root / SEEN_LOG_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_seen(root: Path, seen: dict) -> None:
    path = root / SEEN_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def _seen_key(item: dict) -> str:
    return item.get("title") or item.get("url") or ""


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "market_news.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

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
    cutoff = now - timedelta(hours=lookback_hours)
    seen = _load_seen(root)
    last_run_text = seen.get("last_run_at")
    if last_run_text:
        try:
            since_last_run = datetime.fromisoformat(last_run_text) - timedelta(hours=LAST_RUN_MARGIN_HOURS)
            cutoff = min(cutoff, max(since_last_run, now - timedelta(hours=MAX_LOOKBACK_HOURS)))
        except ValueError:
            pass
    seen_items = seen.get("items") if isinstance(seen.get("items"), dict) else {}

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
    fetched_count = 0
    for item in _dedupe(all_items):
        fetched_count += 1
        if _seen_key(item) in seen_items:
            continue
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

    seen_cutoff = (now - timedelta(days=SEEN_LOG_RETENTION_DAYS)).isoformat()
    seen_items = {key: at for key, at in seen_items.items() if at >= seen_cutoff}
    for item in recent_items:
        key = _seen_key(item)
        if key:
            seen_items[key] = generated_at
    _save_seen(root, {"last_run_at": generated_at, "items": seen_items})

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
    elif fetched_count:
        # Fetch worked, everything was already shown in an earlier run - not
        # an error, just nothing new since then.
        payload = {
            "module": "market_news",
            "generated_at": generated_at,
            "status": "ok",
            "source_count": len(sources),
            "data": [],
            "note": "No news newer than what earlier runs already showed.",
        }
        logging.info("[market_news] no new news items since the previous run")
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
