import html
import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
# idxtag=00001 is Nikkei's own "日経平均＋銘柄入替" tag on their newsroom -
# pre-filtered by Nikkei themselves to just Nikkei 225 constituent-change
# announcements, out of the dozens of unrelated index releases (JPX日経400,
# 日経気候変動指数, etc.) that share the same newsroom feed.
NEWSROOM_URL = "https://indexes.nikkei.co.jp/nkave/newsroom?evt=10016&idxtag=00001&year="
BASE_URL = "https://indexes.nikkei.co.jp"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

SEEN_STATE_PATH = Path("state") / "nikkei_constituents_seen.json"
SEEN_URLS_RETENTION_COUNT = 200

_ITEM_RE = re.compile(
    r'<li class="news-item">\s*<p class="date">([^<]*)</p>.*?href="([^"]+)"[^>]*>\s*(.*?)\s*</a>',
    re.S,
)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _fetch_announcements() -> list[dict]:
    request = urllib.request.Request(NEWSROOM_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")

    items = []
    for date_text, href, title in _ITEM_RE.findall(body):
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        items.append({"date": date_text.strip(), "title": _strip_tags(title), "url": url})
    return items


def _load_seen(root: Path) -> set[str]:
    path = root / SEEN_STATE_PATH
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("seen_urls") or []) if isinstance(data, dict) else set()
    except json.JSONDecodeError:
        return set()


def _save_seen(root: Path, seen_urls: set[str]) -> None:
    path = root / SEEN_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    trimmed = sorted(seen_urls)[-SEEN_URLS_RETENTION_COUNT:]
    path.write_text(json.dumps({"seen_urls": trimmed}, ensure_ascii=False, indent=2), encoding="utf-8")


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "nikkei_constituents.json"
    generated_at = datetime.now(JST).isoformat()

    try:
        announcements = _fetch_announcements()
    except Exception as exc:
        payload = {
            "module": "nikkei_constituents",
            "generated_at": generated_at,
            "status": "error",
            "error": str(exc),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[nikkei_constituents] fetch failed: %s", exc)
        return

    if not announcements:
        payload = {
            "module": "nikkei_constituents",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No announcements found on the newsroom page.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[nikkei_constituents] skipped: no announcements found")
        return

    seen = _load_seen(root)
    is_first_run = not seen
    new_items = [a for a in announcements if a["url"] not in seen]
    _save_seen(root, seen | {a["url"] for a in announcements})

    # On the very first run there's no baseline to diff against - every
    # historical announcement on the page would otherwise look "new", which
    # would dump years of past constituent-change history into one alert.
    if is_first_run:
        payload = {
            "module": "nikkei_constituents",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "First run: baseline recorded, nothing to compare against yet.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[nikkei_constituents] first run: recorded %d announcement(s) as baseline", len(announcements))
        return

    if new_items:
        payload = {
            "module": "nikkei_constituents",
            "generated_at": generated_at,
            "status": "ok",
            "data": new_items,
        }
        logging.info("[nikkei_constituents] found %d new announcement(s)", len(new_items))
    else:
        payload = {
            "module": "nikkei_constituents",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No new announcements.",
        }
        logging.info("[nikkei_constituents] no new announcements")

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
