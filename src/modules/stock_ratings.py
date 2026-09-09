import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.market_news import _dedupe, _fetch_google_news


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_LOOKBACK_HOURS = 30
DEFAULT_MAX_ITEMS_PER_TICKER = 3

# Google News RSS ranks by loose relevance, not strict keyword matching, so
# a per-ticker query pulls in articles about unrelated companies (e.g. the
# "伊藤忠商事" query surfacing a 三井物産 rating article) or generic
# stock-explainer pieces that never mention a rating at all. Requiring the
# ticker's own name AND an actual rating-related word in the title (rather
# than trusting the query match) was added 2026-09-09 after a spot check
# found several such false positives on first use.
RATING_KEYWORDS = ("レーティング", "目標株価", "格上げ", "格下げ", "アナリスト")


def _is_relevant(item: dict, name: str) -> bool:
    title = item.get("title") or ""
    return name in title and any(keyword in title for keyword in RATING_KEYWORDS)


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[stock_ratings] config.json is invalid; default settings used.")
        return {}
    return config.get("stock_ratings", {}) if isinstance(config, dict) else {}


def _watchlist_targets(root: Path) -> list[dict]:
    config_path = root / "config.json"
    if not config_path.exists():
        return []
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    raw_targets = config.get("watchlist", {}).get("tickers", [])
    targets = []
    for item in raw_targets:
        if isinstance(item, dict):
            ticker = str(item.get("ticker", "")).strip()
            name = str(item.get("name") or ticker).strip()
            if ticker and name:
                targets.append({"ticker": ticker, "name": name})
    return targets


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_ratings.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root)
    lookback_hours = int(config.get("lookback_hours", DEFAULT_LOOKBACK_HOURS))
    max_items_per_ticker = int(config.get("max_items_per_ticker", DEFAULT_MAX_ITEMS_PER_TICKER))
    cutoff = datetime.now(JST) - timedelta(hours=lookback_hours)

    targets = _watchlist_targets(root)
    if not targets:
        payload = {
            "module": "stock_ratings",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No watchlist tickers configured.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_ratings] skipped: no watchlist tickers configured")
        return

    results = []
    warnings = []
    for target in targets:
        # A single combined query (not two separate "レーティング" /
        # "目標株価" calls) halves the request count against Google News's
        # RSS endpoint - Google's own relevance ranking already surfaces
        # articles matching either term.
        query = f"{target['name']} レーティング 目標株価"
        try:
            items = _fetch_google_news(query, "rating")
        except Exception as exc:
            warnings.append(f"{target['name']}: {exc}")
            logging.warning("[stock_ratings] fetch failed for '%s': %s", target["name"], exc)
            continue

        recent_items = []
        for item in _dedupe(items):
            if not _is_relevant(item, target["name"]):
                continue
            published_text = item.get("published_at")
            if not published_text:
                continue
            try:
                published_at = datetime.fromisoformat(published_text)
            except ValueError:
                continue
            if published_at >= cutoff:
                recent_items.append(item)

        recent_items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
        recent_items = recent_items[:max_items_per_ticker]
        if recent_items:
            results.append({"ticker": target["ticker"], "name": target["name"], "items": recent_items})

    if results:
        payload = {
            "module": "stock_ratings",
            "generated_at": generated_at,
            "status": "ok",
            "data": results,
        }
        if warnings:
            payload["warnings"] = warnings
        logging.info("[stock_ratings] found rating-related news for %d/%d ticker(s)", len(results), len(targets))
    else:
        payload = {
            "module": "stock_ratings",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "; ".join(warnings) if warnings else "No recent rating-related news found.",
        }
        logging.info("[stock_ratings] no recent rating-related news found")

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
