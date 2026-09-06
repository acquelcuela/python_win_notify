import html
import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
BASE_URL = "https://www.oricon.co.jp/release/album/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
PAGE_OFFSETS = list(range(0, 2))  # unmarked page (this week) + p/1/
PAGE_FETCH_INTERVAL_SECONDS = 2.0
DEFAULT_SCHEDULE_DAYS = [10, 20, 30]
CONFIG_PATH = Path("config.json")

_SECTION_RE = re.compile(
    r'<h4 class="ttl-b"><span>([^<]*)</span></h4>.*?<ul>(.*?)</ul>\s*</div>\s*<!-- /\.block-relese-list -->',
    re.S,
)
_ITEM_RE = re.compile(r'<li class="jsc-block-ranking-detail">(.*?)</li>\s*</ul>', re.S)
_ITEM_SPLIT_RE = re.compile(r'<li class="jsc-block-ranking-detail">')
_DETAIL_URL_RE = re.compile(r'<a href="(/prof/[^"]+/products/[^"]+/)">')
_TITLE_RE = re.compile(r'<h5 class="title">(.*?)</h5>', re.S)
_ARTIST_RE = re.compile(r'<h6 class="artist">(.*?)</h6>', re.S)
_DATE_RE = re.compile(r'<td class="cell-date">(.*?)</td>', re.S)
_PRICE_RE = re.compile(r'<td class="cell-price">(.*?)</td>', re.S)
_COMPANY_RE = re.compile(r'<td class="cell-company"[^>]*>(.*?)</td>', re.S)
_HEADER_DATE_RE = re.compile(r"(\d+)年(\d+)月(\d+)日")


def _load_config(root: Path) -> dict:
    config_path = root / CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return config.get("oricon_album", {}) if isinstance(config, dict) else {}


def _is_scheduled_today(config: dict, now: datetime) -> bool:
    schedule_days = config.get("schedule_days") or DEFAULT_SCHEDULE_DAYS
    return now.day in {int(d) for d in schedule_days}


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _page_url(offset: int) -> str:
    return BASE_URL if offset == 0 else f"{BASE_URL}p/{offset}/"


def _fetch_page(offset: int) -> str:
    url = _page_url(offset)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return raw.decode("shift_jis", errors="replace")


def _parse_header_date(header_text: str) -> str | None:
    match = _HEADER_DATE_RE.search(header_text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_items(list_html: str, release_date: str | None) -> list[dict]:
    items = []
    blocks = _ITEM_SPLIT_RE.split(list_html)[1:]  # first chunk is before the first <li>
    for block in blocks:
        title_match = _TITLE_RE.search(block)
        if not title_match:
            continue
        artist_match = _ARTIST_RE.search(block)
        date_match = _DATE_RE.search(block)
        price_match = _PRICE_RE.search(block)
        company_match = _COMPANY_RE.search(block)
        url_match = _DETAIL_URL_RE.search(block)
        items.append(
            {
                "title": _strip_tags(title_match.group(1)),
                "artist": _strip_tags(artist_match.group(1)) if artist_match else None,
                "release_date": release_date or (_strip_tags(date_match.group(1)) if date_match else None),
                "price": _strip_tags(price_match.group(1)) if price_match else None,
                "company": _strip_tags(company_match.group(1)) if company_match else None,
                "url": f"https://www.oricon.co.jp{url_match.group(1)}" if url_match else None,
            }
        )
    return items


def _parse_page(page_html: str) -> list[dict]:
    items = []
    for header_text, list_html in _SECTION_RE.findall(page_html):
        release_date = _parse_header_date(_strip_tags(header_text))
        items.extend(_parse_items(list_html, release_date))
    return items


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for item in items:
        key = item.get("url") or (item.get("title"), item.get("artist"), item.get("release_date"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "oricon_album.json"
    now = datetime.now(JST)
    generated_at = now.isoformat()

    config = _load_config(root)
    if not _is_scheduled_today(config, now):
        payload = {
            "module": "oricon_album",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": f"Not a scheduled day (schedule_days={config.get('schedule_days', DEFAULT_SCHEDULE_DAYS)}).",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[oricon_album] skipped: not a scheduled day")
        return

    all_items = []
    warnings = []
    for index, offset in enumerate(PAGE_OFFSETS):
        if index > 0:
            time.sleep(PAGE_FETCH_INTERVAL_SECONDS)
        try:
            page_html = _fetch_page(offset)
            all_items.extend(_parse_page(page_html))
        except Exception as exc:
            warnings.append(f"page offset {offset}: {exc}")
            logging.error("[oricon_album] fetch failed for page offset %d: %s", offset, exc)

    items = _dedupe(all_items)
    items.sort(key=lambda item: item.get("release_date") or "")

    if items:
        payload = {
            "module": "oricon_album",
            "generated_at": generated_at,
            "status": "ok",
            "data": items,
        }
        if warnings:
            payload["warnings"] = warnings
        logging.info("[oricon_album] collected %d album(s) across %d page(s)", len(items), len(PAGE_OFFSETS))
    else:
        payload = {
            "module": "oricon_album",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "; ".join(warnings) if warnings else "No album release items found.",
        }
        logging.info("[oricon_album] no items found")

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
