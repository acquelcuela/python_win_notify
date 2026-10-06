"""Near-real-time Nikkei / TOPIX readings from Yahoo! JAPAN pages (2026-10-06).

- fetch_yahoo_top_indices(): Nikkei average and the actual TOPIX index from the
  Yahoo!ファイナンス top page (server-rendered, so a plain HTTP GET is enough).
- fetch_offhours_nikkei(): the "日経時間外" (24h Nikkei) level as people post it
  on X, read through Yahoo!リアルタイム検索. Used at 06:45 instead of the CME
  dollar futures (NKD=F) as the overnight market signal.

The X posts are usually shared from a quote app as
"【🇯🇵日経時間外 】 0.21％ 70,096 （ 149）", where the up/down arrow is an image
and is lost in text. So only the level (70,096) is used, and the change is
computed against the previous Nikkei close the caller passes in. Several of
the newest posts are combined (median) so one stale or mistyped post can't
move the result.

Both pages are personal low-frequency reads (a few times a day); nothing here
needs Chrome or Claude.
"""
from __future__ import annotations

import html
import json
import re
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), "JST")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT_SECONDS = 20

YAHOO_FINANCE_TOP_URL = "https://finance.yahoo.co.jp/"
REALTIME_SEARCH_URL = "https://search.yahoo.co.jp/realtime/search?p={query}"
OFFHOURS_QUERY = "日経時間外"

# Only posts this recent count, and at most this many of the newest are used.
OFFHOURS_MAX_AGE_MINUTES = 180
OFFHOURS_POSTS_TO_USE = 5
# A level further than this from the previous close is treated as a typo or
# an unrelated number and ignored.
OFFHOURS_MAX_DEVIATION_PCT = 8.0

_OFFHOURS_LEVEL_RE = re.compile(r"日経時間外[^0-9]{0,20}?(?:[0-9.]+\s*[％%]\s*)?([0-9]{2},[0-9]{3}(?:\.[0-9]+)?)")
_TOP_INDEX_RE = r"_cl_link:{key};_cl_position:[0-9]+"


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", "replace")


def _to_float(text: str) -> float:
    return float(text.replace(",", "").replace("+", ""))


def fetch_yahoo_top_indices() -> dict:
    """{"nikkei": {"value", "change", "change_pct"}, "topix": {...}} as shown on
    the Yahoo!ファイナンス top page. Before 09:00 / on holidays this is the
    previous session's close and that session's change."""
    page = _get(YAHOO_FINANCE_TOP_URL)
    result = {}
    for name, key in (("nikkei", "nikkei"), ("topix", "topix")):
        # Each market item is a link label followed by its value and change;
        # read from this item's link up to the next item's link.
        match = re.search(_TOP_INDEX_RE.format(key=key), page)
        if not match:
            raise ValueError(f"{name} not found on the Yahoo!ファイナンス top page")
        next_item = page.find("_cl_link:", match.end())
        block = page[match.start():next_item if next_item > 0 else match.end() + 2000]
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", block.split('">', 1)[-1])))
        numbers = re.findall(r"[+-]?[0-9][0-9,]*\.?[0-9]*", text)
        if len(numbers) < 2:
            raise ValueError(f"could not read {name} value/change from: {text[:80]}")
        value, change = _to_float(numbers[0]), _to_float(numbers[1])
        previous = value - change
        result[name] = {
            "value": value,
            "change": change,
            "change_pct": round(change / previous * 100, 2) if previous else None,
        }
    return result


def fetch_offhours_nikkei(previous_close: float, now: datetime | None = None) -> dict:
    """Latest "日経時間外" level from X posts and its change vs previous_close.

    Raises ValueError when no recent, plausible post is found - callers fall
    back to another source.
    """
    now = now or datetime.now(JST)
    page = _get(REALTIME_SEARCH_URL.format(query=urllib.parse.quote(OFFHOURS_QUERY)))
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        raise ValueError("Yahoo!リアルタイム検索: page data not found")
    data = json.loads(match.group(1))
    entries = (((data.get("props") or {}).get("pageProps") or {}).get("pageData") or {}).get("timeline", {}).get("entry") or []

    cutoff = now - timedelta(minutes=OFFHOURS_MAX_AGE_MINUTES)
    readings = []
    for entry in entries:
        try:
            posted_at = datetime.fromtimestamp(int(entry.get("createdAt")), JST)
        except (TypeError, ValueError):
            continue
        if posted_at < cutoff:
            continue
        text = str(entry.get("displayText") or "").replace("\tSTART\t", "").replace("\tEND\t", "")
        level_match = _OFFHOURS_LEVEL_RE.search(text)
        if not level_match:
            continue
        level = _to_float(level_match.group(1))
        if abs(level / previous_close - 1) * 100 > OFFHOURS_MAX_DEVIATION_PCT:
            continue
        readings.append((posted_at, level))

    if not readings:
        raise ValueError(f"no 日経時間外 post in the last {OFFHOURS_MAX_AGE_MINUTES} minutes")
    readings.sort(reverse=True)
    newest = readings[:OFFHOURS_POSTS_TO_USE]
    level = statistics.median(level for _, level in newest)
    return {
        "source": "yahoo_realtime_search",
        "label": "日経時間外(Xの投稿・Yahoo!リアルタイム検索)",
        "value": round(level, 2),
        "previous_close": round(previous_close, 2),
        "change": round(level - previous_close, 2),
        "change_pct": round((level / previous_close - 1) * 100, 2),
        "posts_used": len(newest),
        "latest_post_at": newest[0][0].isoformat(),
    }
