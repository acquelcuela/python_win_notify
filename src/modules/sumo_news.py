import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.market_news import _dedupe, _fetch_google_news


JST = timezone(timedelta(hours=9), "JST")
DEFAULT_QUERIES = [
    "大相撲 ニュース",
    "大相撲 取組",
    "大相撲 番付",
]


def _normalized_story_key(item: dict) -> str:
    """Google News RSS syndicates the same story from many outlets, each
    with title formatted as "Headline - Outlet Name" and sometimes
    full-width vs half-width digits in the headline itself (e.g. "4100人"
    vs "４１００人"). Plain title/url dedup (market_news._dedupe) treats
    those as different stories. This strips the trailing outlet suffix
    and normalizes digit width so syndicated copies collapse to one key."""
    title = item.get("title") or ""
    source = (item.get("source") or "").strip()
    if source and title.endswith(f" - {source}"):
        title = title[: -(len(source) + 3)]
    else:
        title = re.sub(r"\s-\s[^-]+$", "", title)
    return unicodedata.normalize("NFKC", title).strip()


def _dedupe_stories(items: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for item in items:
        key = _normalized_story_key(item) or item.get("url")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _tag_roster_wrestler(item: dict, roster: list[dict]) -> None:
    """Tags an item with the first roster entry whose name is found in its
    title, so general-query articles (not fetched via a per-wrestler query)
    can still be grouped by wrestler in the mail. roster is a manually
    maintained list (config.json sumo_news.roster) rather than an official
    banzuke feed - no public API/CSV for the current banzuke was found, and
    the official site's banzuke table is JS-rendered, so it needs periodic
    manual updates (promotions/demotions/retirements) supplied by the user.
    peak_rank is the highest rank the wrestler is known to have reached,
    not their current rank - it's a static label, not live-tracked."""
    if item.get("wrestler"):
        return
    title = item.get("title") or ""
    for entry in roster:
        name = entry.get("name")
        if name and name in title:
            item["wrestler"] = name
            item["wrestler_peak_rank"] = entry.get("peak_rank")
            return


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[sumo_news] config.json is invalid; default sumo news settings used.")
        return {}


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "sumo_news.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root).get("sumo_news", {})
    queries = [str(q) for q in (config.get("queries") or DEFAULT_QUERIES) if str(q).strip()]
    wrestlers = [str(w).strip() for w in (config.get("wrestlers") or []) if str(w).strip()]
    roster = [
        {"name": str(entry.get("name", "")).strip(), "peak_rank": entry.get("peak_rank")}
        for entry in (config.get("roster") or [])
        if isinstance(entry, dict) and str(entry.get("name", "")).strip()
    ]
    peak_rank_by_name = {entry["name"]: entry["peak_rank"] for entry in roster}
    max_items = int(config.get("max_items", 15))
    lookback_hours = int(config.get("lookback_hours", 14))
    cutoff = datetime.now(JST) - timedelta(hours=lookback_hours)

    all_items = []
    warnings = []

    for query in queries:
        try:
            all_items.extend(_fetch_google_news(query, "general"))
        except Exception as exc:
            warnings.append(f"general / {query}: {exc}")
            logging.error("[sumo_news] fetch failed for query '%s': %s", query, exc)

    for wrestler in wrestlers:
        try:
            items = _fetch_google_news(f"大相撲 {wrestler}", "wrestler")
            for item in items:
                item["wrestler"] = wrestler
                item["wrestler_peak_rank"] = peak_rank_by_name.get(wrestler)
            all_items.extend(items)
        except Exception as exc:
            warnings.append(f"wrestler:{wrestler}: {exc}")
            logging.error("[sumo_news] fetch failed for wrestler '%s': %s", wrestler, exc)

    for item in all_items:
        _tag_roster_wrestler(item, roster)

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
    recent_items = _dedupe_stories(recent_items)[:max_items]

    if recent_items:
        payload = {
            "module": "sumo_news",
            "generated_at": generated_at,
            "status": "ok",
            "data": recent_items,
        }
        if warnings:
            payload["warnings"] = warnings
        logging.info("[sumo_news] collected %d news items", len(recent_items))
    else:
        payload = {
            "module": "sumo_news",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "; ".join(warnings) if warnings else "No recent sumo news found.",
        }
        logging.info("[sumo_news] no recent news items")

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
