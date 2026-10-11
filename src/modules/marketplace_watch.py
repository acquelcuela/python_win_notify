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


def _fetch_yahoo_auction(keyword: str) -> list[dict]:
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
    params = urllib.parse.urlencode({"p": keyword})
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


def _fetch_rakuma(keyword: str) -> list[dict]:
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

    for rank, tier in enumerate(tiers):
        if any(unicodedata.normalize("NFKC", str(p)).lower() in normalized for p in tier["patterns"]):
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
    warnings = []
    for entry in keywords:
        keyword = entry["keyword"]
        items = []
        for platform in entry["platforms"]:
            try:
                fetched = FETCHERS[platform](keyword)
            except Exception as exc:
                warnings.append(f"{keyword} ({platform}): {exc}")
                logging.error("[marketplace_watch] %s fetch failed for keyword '%s': %s", platform, keyword, exc)
                continue
            # Exclude words apply everywhere; the all-words title check only on
            # Rakuma, whose search matches loosely (Yahoo's search is already AND).
            fetched = [
                item for item in fetched
                if _title_matches(item.get("title") or "", keyword if platform == "rakuma" else "", entry["exclude"])
            ]
            if entry["tiers"]:
                tiered = []
                for item in fetched:
                    tier = _assign_tier(item, entry["tiers"], entry["unknown_tier"])
                    if tier:
                        tiered.append({**item, **tier})
                fetched = tiered
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
    logging.info(
        "[marketplace_watch] found %d new item(s) across %d keyword(s)", total_new, len(keywords)
    )


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
