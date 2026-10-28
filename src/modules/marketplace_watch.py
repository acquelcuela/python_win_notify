import html
import json
import logging
import re
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
YAHOO_AUCTION_SEARCH_URL = "https://auctions.yahoo.co.jp/search/search"
RAKUMA_SEARCH_URL = "https://fril.jp/s"
DEFAULT_PLATFORMS = ("yahoo_auction",)
SUPPORTED_PLATFORMS = ("yahoo_auction", "rakuma")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
DEFAULT_MAX_ITEMS = 30

SEEN_LOG_PATH = Path("state") / "marketplace_watch_seen.json"
SEEN_LOG_RETENTION_DAYS = 180

_ITEM_BLOCK_RE = re.compile(r'<li class="Product">(.*?)</li>', re.S)
_ATTR_RE = {
    "id": re.compile(r'data-auction-id="([^"]*)"'),
    "title": re.compile(r'data-auction-title="([^"]*)"'),
    "price": re.compile(r'data-auction-price="([^"]*)"'),
    "endtime": re.compile(r'data-auction-endtime="([^"]*)"'),
    "buynowprice": re.compile(r'data-auction-buynowprice="([^"]*)"'),
    "isfreeshipping": re.compile(r'data-auction-isfreeshipping="([^"]*)"'),
    "isshoppingitem": re.compile(r'data-auction-isshoppingitem="([^"]*)"'),
}
_POSTAGE_RE = re.compile(r'<p class="Product__postage">\s*([^<]*?)\s*</p>', re.S)
_BID_COUNT_RE = re.compile(r'<dd class="Product__bid">([^<]*)</dd>')


def _normalize_keywords(raw_keywords) -> list[dict]:
    """Accepts both a plain keyword string and {"keyword": ..., "target_price":
    ...} - target_price is informational only (shown as a plain diff in the
    mail, not a filter or an alert), so keeping the plain-string form
    working means older config doesn't need to be migrated."""
    keywords = []
    for entry in raw_keywords or []:
        if isinstance(entry, str):
            keyword = entry.strip()
            if keyword:
                keywords.append({"keyword": keyword, "target_price": None, "platforms": list(DEFAULT_PLATFORMS), "exclude": []})
        elif isinstance(entry, dict):
            if entry.get("enabled") is False:  # paused, settings kept (e.g. Fire HD 10, 2026-10-18)
                continue
            keyword = str(entry.get("keyword") or "").strip()
            if not keyword:
                continue
            target_price = entry.get("target_price")
            platforms = [p for p in (entry.get("platforms") or DEFAULT_PLATFORMS) if p in SUPPORTED_PLATFORMS]
            keywords.append(
                {
                    "keyword": keyword,
                    "target_price": int(target_price) if isinstance(target_price, (int, float)) else None,
                    "platforms": platforms or list(DEFAULT_PLATFORMS),
                    "exclude": [str(w) for w in entry.get("exclude") or [] if str(w).strip()],
                    "tiers": [t for t in entry.get("tiers") or [] if isinstance(t, dict) and t.get("patterns")],
                    "unknown_tier": entry.get("unknown_tier") if isinstance(entry.get("unknown_tier"), dict) else None,
                    "min_price": entry.get("min_price") if isinstance(entry.get("min_price"), (int, float)) else None,
                    "max_price": entry.get("max_price") if isinstance(entry.get("max_price"), (int, float)) else None,
                    "newest_first": bool(entry.get("newest_first")),
                    "store_only": bool(entry.get("store_only")),
                }
            )
    return keywords


CONFIG_PATH = Path("marketplace_watch_config.json")


def _load_config(root: Path) -> dict:
    config_path = root / CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[marketplace_watch] %s is invalid; using defaults.", CONFIG_PATH)
        return {}
    return config if isinstance(config, dict) else {}


def _fetch_yahoo_auction(keyword: str, options: dict | None = None) -> list[dict]:
    """Yahoo Auction has no public keyword-search API, and its old
    search-result RSS feed no longer returns RSS (checked 2026-08-19: the
    rss=1 param just returns the normal HTML page now). The search-results
    page is still server-rendered HTML though, and each listing carries
    data-auction-* attributes that Yahoo's own JS uses for click tracking -
    pulling those is far less fragile than scraping visible DOM text, so
    that's the approach here instead of a true API call. Mercari was
    evaluated too but its search page ships empty (client-side rendered
    after a JS/CAPTCHA challenge) with no equivalent, so it's not covered
    here - see keyword_and_marketplace_watch_spec_20260819.md."""
    options = options or {}
    query = {"p": keyword}
    # Optional per keyword (2026-10-11): price range passed to Yahoo's own
    # search (min/max) and newest-first order, so the 50 listings on the
    # first page are the latest ones in range rather than "recommended".
    if isinstance(options.get("min_price"), (int, float)):
        query["min"] = int(options["min_price"])
    if isinstance(options.get("max_price"), (int, float)):
        query["max"] = int(options["max_price"])
    if options.get("newest_first"):
        query.update({"s1": "new", "o1": "d"})
    params = urllib.parse.urlencode(query)
    url = f"{YAHOO_AUCTION_SEARCH_URL}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")

    items = []
    seen_ids_in_page = set()
    for match in _ITEM_BLOCK_RE.finditer(body):
        block = match.group(1)
        id_match = _ATTR_RE["id"].search(block)
        if not id_match:
            continue
        auction_id = id_match.group(1).strip()
        if not auction_id or auction_id in seen_ids_in_page:
            continue
        seen_ids_in_page.add(auction_id)

        title_match = _ATTR_RE["title"].search(block)
        price_match = _ATTR_RE["price"].search(block)
        price_text = price_match.group(1) if price_match else None

        end_time = None
        endtime_match = _ATTR_RE["endtime"].search(block)
        if endtime_match and endtime_match.group(1).isdigit():
            end_time = datetime.fromtimestamp(int(endtime_match.group(1)), tz=JST).isoformat()

        buynow_price = None
        buynow_match = _ATTR_RE["buynowprice"].search(block)
        if buynow_match and buynow_match.group(1).isdigit() and int(buynow_match.group(1)) > 0:
            buynow_price = int(buynow_match.group(1))

        postage_match = _POSTAGE_RE.search(block)
        shipping = html.unescape(postage_match.group(1)).strip() if postage_match else ""
        if not shipping:
            isfreeshipping_match = _ATTR_RE["isfreeshipping"].search(block)
            if isfreeshipping_match and isfreeshipping_match.group(1) == "1":
                shipping = "送料無料"
        bid_count_match = _BID_COUNT_RE.search(block)

        # Yahoo Auction's item cards carry no explicit "store seller" badge
        # in this search-result view - data-auction-isshoppingitem (this
        # listing is also synced to Yahoo Shopping's catalog) is the closest
        # available proxy for a business/store seller, but it's inferred,
        # not a confirmed store flag straight from Yahoo.
        isshopping_match = _ATTR_RE["isshoppingitem"].search(block)
        is_store = bool(isshopping_match and isshopping_match.group(1) == "1")

        items.append(
            {
                "platform": "yahoo_auction",
                "id": auction_id,
                "title": html.unescape(title_match.group(1)).strip() if title_match else "",
                "is_store": is_store,
                "price": int(price_text) if price_text and price_text.isdigit() else None,
                "buy_now_price": buynow_price,
                "shipping": shipping or None,
                "end_time": end_time,
                "bid_count": bid_count_match.group(1).strip() if bid_count_match else None,
                "url": f"https://auctions.yahoo.co.jp/jp/auction/{auction_id}",
            }
        )
    return items


def _fetch_rakuma(keyword: str, options: dict | None = None) -> list[dict]:
    """Rakuma's search page is server-rendered: each result is a
    <div class="item"> block whose link carries the item name/price/id in
    data-rat-* attributes. Newest first, on-sale only (transaction=selling).
    Its keyword search is loose (any word can match - "Fire HD" also returns
    HDMI cables), so the caller keeps only titles containing every word."""
    params = urllib.parse.urlencode({"query": keyword, "order": "desc", "sort": "created_at", "transaction": "selling"})
    request = urllib.request.Request(f"{RAKUMA_SEARCH_URL}?{params}", headers={"User-Agent": USER_AGENT, "Accept-Language": "ja"})
    with urllib.request.urlopen(request, timeout=20) as response:
        page = response.read().decode("utf-8", errors="replace")
    items = []
    for block in page.split('<div class="item">')[1:]:
        url = re.search(r'href="(https://item\.fril\.jp/[0-9a-f]+)"', block)
        name = re.search(r'data-rat-item_name="([^"]*)"', block)
        price = re.search(r'data-rat-price="(\d+)"', block)
        if not (url and name):
            continue
        items.append(
            {
                "platform": "rakuma",
                "id": url.group(1).rsplit("/", 1)[-1],
                "title": html.unescape(name.group(1)).strip(),
                "is_store": False,
                "price": int(price.group(1)) if price else None,
                "buy_now_price": int(price.group(1)) if price else None,
                "shipping": None,
                "end_time": None,
                "bid_count": None,
                "url": url.group(1),
            }
        )
    return items


def _title_matches(title: str, keyword: str, exclude: list[str]) -> bool:
    normalized = unicodedata.normalize("NFKC", title).lower()
    if any(unicodedata.normalize("NFKC", w).lower() in normalized for w in exclude):
        return False
    return all(unicodedata.normalize("NFKC", w).lower() in normalized for w in keyword.split())


FETCHERS = {"yahoo_auction": _fetch_yahoo_auction, "rakuma": _fetch_rakuma}


def _judge(item: dict, entry: dict, keyword: str, platform: str) -> dict:
    """One fetched listing with whether it passed this keyword's filters and,
    if not, the first reason it was dropped - kept for the run log."""
    title = item.get("title") or ""
    price = item.get("buy_now_price") or item.get("price")
    record = {**item, "kept": False, "reason": None}
    # Exclude words apply everywhere; the all-words title check only on
    # Rakuma, whose search matches loosely (Yahoo's search is already AND).
    if not _title_matches(title, keyword if platform == "rakuma" else "", entry["exclude"]):
        record["reason"] = "除外語/キーワード不一致"
    elif entry["store_only"] and not item.get("is_store"):
        # is_store is inferred (Yahoo-Shopping-synced listing), not an official badge.
        record["reason"] = "ストア以外"
    elif (entry["min_price"] is not None or entry["max_price"] is not None) and not _in_price_range(
        price, entry["min_price"], entry["max_price"]
    ):
        # Same price rule as the tiers: buy-it-now price when there is one.
        record["reason"] = "価格範囲外"
    else:
        tier = _assign_tier(item, entry["tiers"], entry["unknown_tier"]) if entry["tiers"] else {}
        if tier is None:
            record["reason"] = "優先度の条件外(世代/メモリ不明・上限価格超え)"
        else:
            record.update(tier)
            record["kept"] = True
    return record


# Every run's full result (all fetched listings, kept or dropped with the
# reason, and which were new) for later analysis of prices and filters
# (user request 2026-10-11). marketplace_watch.json itself is overwritten.
RUN_LOG_DIR = Path("output") / "history" / "marketplace_watch"
RUN_LOG_RETENTION_DAYS = 120


def _archive_run(root: Path, now: datetime, run_log: dict) -> None:
    try:
        directory = root / RUN_LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"marketplace_watch_{now:%Y%m%d_%H%M}.json").write_text(
            json.dumps(run_log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cutoff = (now - timedelta(days=RUN_LOG_RETENTION_DAYS)).strftime("%Y%m%d")
        for path in directory.glob("marketplace_watch_*.json"):
            if path.stem[len("marketplace_watch_"):][:8] < cutoff:
                path.unlink()
    except OSError as exc:
        logging.warning("[marketplace_watch] run log archive failed: %s", exc)


def _in_price_range(price, min_price, max_price) -> bool:
    if not isinstance(price, int):
        return False
    return (min_price is None or price >= min_price) and (max_price is None or price <= max_price)


def _assign_tier(item: dict, tiers: list[dict], unknown_tier: dict | None) -> dict | None:
    """Optional per-keyword buying priorities (2026-10-11, e.g. Fire HD 10 by
    generation). The first tier whose patterns appear in the title wins; an
    item over that tier's max_price is dropped. Titles matching no tier fall
    into unknown_tier if configured, otherwise they are dropped.
    The price compared is the buy-it-now price when there is one (Yahoo's
    current bid can still rise), else the listed price."""
    normalized = unicodedata.normalize("NFKC", item.get("title") or "").lower()
    price = item.get("buy_now_price") or item.get("price")

    def _within(tier: dict) -> bool:
        limit = tier.get("max_price")
        return not (isinstance(limit, (int, float)) and isinstance(price, int) and price > limit)

    def _hit(tier: dict) -> bool:
        # "regex": true lets a tier use patterns like (?<!\d)8\s*gb, so "8GB"
        # memory doesn't match the "8GB" inside "SSD 128GB".
        if tier.get("regex"):
            return any(re.search(str(p), normalized) for p in tier["patterns"])
        return any(unicodedata.normalize("NFKC", str(p)).lower() in normalized for p in tier["patterns"])

    for rank, tier in enumerate(tiers):
        if _hit(tier):
            return {"tier": tier.get("label"), "tier_rank": rank} if _within(tier) else None
    if unknown_tier and _within(unknown_tier):
        return {"tier": unknown_tier.get("label") or "世代不明", "tier_rank": len(tiers)}
    return None


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


def _seen_key(keyword: str, item: dict) -> str:
    return f"{keyword}::{item.get('platform')}::{item.get('id')}"


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "marketplace_watch.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    config = _load_config(root)
    keywords = _normalize_keywords(config.get("keywords"))
    max_items = int(config.get("max_items", DEFAULT_MAX_ITEMS))

    if not keywords:
        payload = {
            "module": "marketplace_watch",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No keywords configured.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[marketplace_watch] skipped: no keywords configured")
        return

    seen_records = _load_seen(root)
    seen_keys = {r.get("key") for r in seen_records}
    today_label = now.strftime("%Y-%m-%d")

    results_by_keyword = []
    run_log: list[dict] = []
    warnings = []
    for entry in keywords:
        keyword = entry["keyword"]
        items = []
        log_records: list[dict] = []
        for platform in entry["platforms"]:
            try:
                fetched = FETCHERS[platform](keyword, entry)
            except Exception as exc:
                warnings.append(f"{keyword} ({platform}): {exc}")
                logging.error("[marketplace_watch] %s fetch failed for keyword '%s': %s", platform, keyword, exc)
                continue
            kept = []
            for item in fetched:
                record = _judge(item, entry, keyword, platform)
                log_records.append(record)
                if record["kept"]:
                    kept.append({k: v for k, v in record.items() if k not in ("kept", "reason")})
            fetched = kept
            items.extend(fetched[:max_items])

        new_items = []
        for item in items:
            key = _seen_key(keyword, item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            seen_records.append({"key": key, "logged_date": today_label})
            new_items.append(item)

        results_by_keyword.append(
            {"keyword": keyword, "target_price": entry["target_price"], "new_items": new_items}
        )
        new_ids = {(i.get("platform"), i.get("id")) for i in new_items}
        for record in log_records:
            record["is_new"] = (record.get("platform"), record.get("id")) in new_ids
        run_log.append({"keyword": keyword, "settings": {k: v for k, v in entry.items() if k != "keyword"}, "items": log_records})

    cutoff_label = (now - timedelta(days=SEEN_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
    seen_records = [r for r in seen_records if r.get("logged_date", "9999-99-99") >= cutoff_label]
    _save_seen(root, seen_records)

    total_new = sum(len(r["new_items"]) for r in results_by_keyword)
    payload = {
        "module": "marketplace_watch",
        "generated_at": generated_at,
        "status": "ok",
        "results": results_by_keyword,
    }
    if warnings:
        payload["warnings"] = warnings
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _archive_run(root, now, {"generated_at": generated_at, "warnings": warnings, "keywords": run_log})
    logging.info(
        "[marketplace_watch] found %d new item(s) across %d keyword(s)", total_new, len(keywords)
    )


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
