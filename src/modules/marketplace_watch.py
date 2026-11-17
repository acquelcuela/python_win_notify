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

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


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
    """Yahoo Auction has no public keyword-search API. Its search page used
    to be server-rendered <li class="Product"> cards with data-auction-*
    attributes, but around 2026-10-14 it moved to a Next.js page
    (/search/search/<keyword>/0, the old URL 301-redirects there) and those
    cards disappeared - the parser then silently returned 0 items for a
    month. The listings are now read from the page's __NEXT_DATA__ JSON
    (props.pageProps.initialState.search.items.listing.items). If that
    JSON or path is missing, this raises so the run shows a warning
    instead of quietly reporting nothing. Mercari is not covered - see
    keyword_and_marketplace_watch_spec_20260819.md."""
    options = options or {}
    query = {"p": keyword}
    # Optional per keyword (2026-10-11): price range passed to Yahoo's own
    # search and newest-first order, so the 50 listings on the first page
    # are the latest ones in range rather than "recommended". The redirect
    # keeps s1/o1 (as select=22) but drops min/max, so the price range uses
    # aucminprice/aucmaxprice (checked 2026-11-13).
    if isinstance(options.get("min_price"), (int, float)):
        query["aucminprice"] = int(options["min_price"])
    if isinstance(options.get("max_price"), (int, float)):
        query["aucmaxprice"] = int(options["max_price"])
    if options.get("newest_first"):
        query.update({"s1": "new", "o1": "d"})
    params = urllib.parse.urlencode(query)
    url = f"{YAHOO_AUCTION_SEARCH_URL}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")

    next_data = _NEXT_DATA_RE.search(body)
    if not next_data:
        raise ValueError("Yahoo search page has no __NEXT_DATA__ (page format changed?)")
    try:
        search = json.loads(next_data.group(1))["props"]["pageProps"]["initialState"]["search"]
        listing = search["items"]["listing"]["items"] or []
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Yahoo search data not where expected (page format changed?): {exc!r}") from exc

    items = []
    seen_ids_in_page = set()
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        auction_id = str(entry.get("auctionId") or "").strip()
        if not auction_id or auction_id in seen_ids_in_page:
            continue
        seen_ids_in_page.add(auction_id)

        buynow = entry.get("buyNowPrice")
        price = entry.get("price")
        fee = entry.get("currentPriceShippingFee")
        if entry.get("isFreeShipping"):
            shipping = "送料無料"
        elif isinstance(fee, (int, float)) and fee > 0:
            shipping = f"送料 {int(fee):,}円"
        else:
            shipping = None
        bid_count = entry.get("bidCount")
        seller = entry.get("seller") if isinstance(entry.get("seller"), dict) else {}

        items.append(
            {
                "platform": "yahoo_auction",
                "id": auction_id,
                "title": html.unescape(str(entry.get("title") or "")).strip(),
                # The new page carries an explicit store-seller flag
                # (seller.isStore), replacing the old isshoppingitem proxy.
                "is_store": bool(seller.get("isStore")),
                "price": int(price) if isinstance(price, (int, float)) else None,
                "buy_now_price": int(buynow) if isinstance(buynow, (int, float)) and buynow > 0 else None,
                "shipping": shipping,
                "end_time": entry.get("endTime"),
                "bid_count": str(bid_count) if bid_count is not None else None,
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
