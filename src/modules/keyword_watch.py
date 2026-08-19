import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.market_news import _dedupe, _fetch_google_news


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_DAY_OF_MONTH = 1
DEFAULT_LOOKBACK_DAYS = 31
DEFAULT_MAX_ITEMS = 20

SEEN_LOG_PATH = Path("state") / "keyword_watch_seen.json"
SEEN_LOG_RETENTION_DAYS = 100


CONFIG_PATH = Path("keyword_watch_config.json")


def _load_config(root: Path) -> dict:
    config_path = root / CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[keyword_watch] %s is invalid; using defaults.", CONFIG_PATH)
        return {}
    return config if isinstance(config, dict) else {}


def _is_scheduled_today(config: dict, now: datetime) -> bool:
    """main.py's batch_schedule only understands time-of-day + weekday, so
    the "run once a month" condition lives here instead: every scheduled
    trigger checks in, and only actually runs on the configured day."""
    day_of_month = int(config.get("day_of_month", DEFAULT_DAY_OF_MONTH))
    return now.day == day_of_month


def _load_seen(root: Path) -> list[dict]:
    path = root / SEEN_LOG_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _save_seen(root: Path, records: list[dict]) -> None:
    path = root / SEEN_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _seen_key(item: dict) -> str:
    return item.get("url") or item.get("title") or ""


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "keyword_watch.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    config = _load_config(root)

    if not _is_scheduled_today(config, now):
        payload = {
            "module": "keyword_watch",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": f"Not the scheduled day of month (day_of_month={config.get('day_of_month', DEFAULT_DAY_OF_MONTH)}).",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[keyword_watch] skipped: not the scheduled day of month")
        return

    queries = [str(q).strip() for q in (config.get("queries") or []) if str(q).strip()]
    if not queries:
        payload = {
            "module": "keyword_watch",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No queries configured.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[keyword_watch] skipped: no queries configured")
        return

    lookback_days = int(config.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    max_items = int(config.get("max_items", DEFAULT_MAX_ITEMS))
    cutoff = now - timedelta(days=lookback_days)

    all_items = []
    warnings = []
    for query in queries:
        try:
            all_items.extend(_fetch_google_news(query, query))
        except Exception as exc:
            warnings.append(f"{query}: {exc}")
            logging.error("[keyword_watch] fetch failed for query '%s': %s", query, exc)

    recent_items = []
    for item in _dedupe(all_items):
        published_text = item.get("published_at")
        if published_text:
            try:
                published_at = datetime.fromisoformat(published_text)
                if published_at < cutoff:
                    continue
            except ValueError:
                pass
        recent_items.append(item)
    recent_items.sort(key=lambda item: item.get("published_at") or "", reverse=True)

    # Cross-month dedup: only notify about articles not already sent in a
    # previous run, so a story that stays in the lookback window across two
    # monthly runs doesn't get repeated.
    seen_records = _load_seen(root)
    seen_keys = {r.get("key") for r in seen_records}
    new_items = [item for item in recent_items if _seen_key(item) not in seen_keys][:max_items]

    today_label = now.strftime("%Y-%m-%d")
    for item in new_items:
        key = _seen_key(item)
        if key:
            seen_records.append({"key": key, "logged_date": today_label})
    cutoff_label = (now - timedelta(days=SEEN_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    seen_records = [r for r in seen_records if r.get("logged_date", "9999-99-99") >= cutoff_label]
    _save_seen(root, seen_records)

    payload = {
        "module": "keyword_watch",
        "generated_at": generated_at,
        "status": "ok",
        "query_count": len(queries),
        "queries": queries,
        "data": new_items,
    }
    if warnings:
        payload["warnings"] = warnings
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[keyword_watch] collected %d new item(s) across %d quer(y/ies)", len(new_items), len(queries))


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
