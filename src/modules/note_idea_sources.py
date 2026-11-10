"""Extra topic sources for note_article_ideas (added 2026-11-10):

- TDnet dividend disclosures (増配 / 減配 / 配当方針の変更 ...)
- headlines and movers this repo already collects (market_news, news_movers)
- a month-by-month calendar of dividend-investor events
- "refresh" reminders: when every company in a top-PV comparison article
  has published new earnings, suggest republishing it with fresh numbers

stock_dividend / stock_watchlist outputs are deliberately not used: they
reflect the user's own trading list, and articles must not reveal holdings.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")

# Unofficial but long-running JSON mirror of TDnet (checked 2026-11-10:
# ~760 disclosures on a normal weekday, one request per date).
TDNET_DATE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{date}.json?limit=5000"
TDNET_COMPANY_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json?limit=40"

# Lower rank = shown first. Cuts and policy changes make the strongest
# 本当か / 個別レビュー material; plain 増配 is common in results season.
_TDNET_RANKS = [
    (0, ("減配", "無配", "配当方針", "株主還元方針", "株主還元の強化", "累進配当", "DOE", "配当倍増")),
    (1, ("増配", "記念配当")),
    (2, ("配当予想の修正",)),
]
_TDNET_EXCLUDE = ("収益分配", "配当金受領", "株式分割に伴う")

REFRESH_STATE_PATH = Path("state") / "note_refresh_notified.json"

DEFAULT_SEASON_EVENTS = [
    {"months": [1], "event": "新しいNISA枠が使えるようになる。年初の入金計画・配当目標の立て直し"},
    {"months": [2, 3], "event": "確定申告(配当控除・総合課税・外国税額控除の選び方)"},
    {"months": [3], "event": "3月末の権利確定。高配当株の権利取りと権利落ち"},
    {"months": [4, 5], "event": "本決算の発表が集中。来期の配当予想、増配・減配の発表"},
    {"months": [6], "event": "株主総会と期末配当の入金。夏のボーナスの使い道"},
    {"months": [7, 8], "event": "第1四半期決算。配当予想の早期修正"},
    {"months": [9], "event": "9月末の中間配当の権利確定"},
    {"months": [10, 11], "event": "中間決算の発表が集中。配当予想の修正、中間配当の入金"},
    {"months": [12], "event": "年末の損出し(損益通算)、NISA枠の使い切り、冬のボーナス、来年の計画"},
]


def _get_json(url: str, timeout: int = 30) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _tdnet_items(payload: dict) -> list[dict]:
    return [item.get("Tdnet") or {} for item in (payload.get("items") or []) if isinstance(item, dict)]


def _tdnet_rank(title: str) -> int | None:
    if any(word in title for word in _TDNET_EXCLUDE):
        return None
    for rank, words in _TDNET_RANKS:
        if any(word in title for word in words):
            return rank
    return None


def collect_tdnet_dividend_news(days: int, limit: int) -> tuple[list[dict], list[str]]:
    """Dividend-related disclosures from today and the previous days-1
    days (the batch runs at 22:00, so yesterday covers anything filed after
    the previous run)."""
    warnings: list[str] = []
    found: list[dict] = []
    seen = set()
    today = datetime.now(JST).date()
    for offset in range(days):
        date_label = (today - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            items = _tdnet_items(_get_json(TDNET_DATE_URL.format(date=date_label)))
        except Exception as exc:
            warnings.append(f"tdnet {date_label}: {exc}")
            logging.error("[note_idea_sources] TDnet fetch failed for %s: %s", date_label, exc)
            continue
        for item in items:
            title = str(item.get("title") or "").strip()
            rank = _tdnet_rank(title)
            if rank is None:
                continue
            code = str(item.get("company_code") or "")[:4]
            key = (code, title)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "rank": rank,
                    "code": code,
                    "name": str(item.get("company_name") or "").strip(),
                    "title": title,
                    "pubdate": str(item.get("pubdate") or ""),
                }
            )
    # Newest first within each rank (sort is stable).
    found.sort(key=lambda d: d["pubdate"], reverse=True)
    found.sort(key=lambda d: d["rank"])
    return found[:limit], warnings


def _is_fresh(payload: dict | None, max_age_hours: int) -> bool:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    try:
        generated = datetime.fromisoformat(str(payload.get("generated_at")))
    except ValueError:
        return False
    return datetime.now(JST) - generated <= timedelta(hours=max_age_hours)


def _load_output(root: Path, name: str) -> dict | None:
    path = root / "output" / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def collect_market_context(root: Path, max_age_hours: int, headline_limit: int) -> dict:
    """Reuses what market_news / news_movers already fetched (they run at
    midday and 23:00, so the 22:00 idea run sees the midday snapshot)."""
    headlines: list[str] = []
    news = _load_output(root, "market_news")
    if _is_fresh(news, max_age_hours):
        seen = set()
        for entry in news.get("data") or []:
            title = str(entry.get("title") or "").strip()
            if title and title not in seen:
                seen.add(title)
                headlines.append(title)

    movers: list[dict] = []
    movers_payload = _load_output(root, "news_movers")
    if _is_fresh(movers_payload, max_age_hours):
        for entry in movers_payload.get("data") or []:
            change = entry.get("change_pct")
            movers.append(
                {
                    "name": str(entry.get("name") or ""),
                    "code": str(entry.get("code") or ""),
                    "change_pct": round(float(change), 1) if isinstance(change, (int, float)) else None,
                    "headline": str((entry.get("matched_titles") or [""])[0]),
                }
            )
    return {"headlines": headlines[:headline_limit], "movers": movers}


def season_events(config_events: list | None, now: datetime | None = None) -> list[str]:
    """Events for this month and next month, so an article can be written
    and published before the event rather than after it."""
    now = now or datetime.now(JST)
    this_month = now.month
    next_month = this_month % 12 + 1
    events = config_events if isinstance(config_events, list) and config_events else DEFAULT_SEASON_EVENTS
    results = []
    for event in events:
        months = event.get("months") or []
        text = str(event.get("event") or "").strip()
        if not text:
            continue
        if this_month in months:
            results.append(f"{this_month}月: {text}")
        elif next_month in months:
            results.append(f"{next_month}月(来月): {text}")
    return results


def _latest_earnings(code: str) -> dict | None:
    items = _tdnet_items(_get_json(TDNET_COMPANY_URL.format(code=code)))
    for item in items:  # newest first
        title = str(item.get("title") or "")
        if "決算短信" in title and "訂正" not in title:
            return {"code": code, "title": title, "pubdate": str(item.get("pubdate") or "")}
    return None


def _load_refresh_state(root: Path) -> dict:
    state_path = root / REFRESH_STATE_PATH
    try:
        notified = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except json.JSONDecodeError:
        notified = {}
    return notified if isinstance(notified, dict) else {}


def mark_refresh_notified(root: Path, candidates: list[dict]) -> None:
    """Called only after the idea memo was produced, so a failed run
    doesn't swallow the reminder."""
    if not candidates:
        return
    notified = _load_refresh_state(root)
    for candidate in candidates:
        notified[candidate["title"]] = candidate["ready_date"]
    state_path = root / REFRESH_STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(notified, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_refresh_candidates(root: Path, targets: list | None, window_days: int) -> tuple[list[dict], list[str]]:
    """A comparison article is ready to refresh once every company in it
    has filed a 決算短信 within window_days. Each article is suggested once
    per earnings round: the newest filing date is remembered in
    state/note_refresh_notified.json (see mark_refresh_notified), and the
    article comes back only after a later round of filings."""
    warnings: list[str] = []
    if not isinstance(targets, list) or not targets:
        return [], warnings

    notified = _load_refresh_state(root)
    cutoff = (datetime.now(JST) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    candidates = []
    for target in targets:
        title = str(target.get("title") or "").strip()
        codes = [str(c).strip() for c in (target.get("codes") or []) if str(c).strip()]
        if not title or not codes:
            continue
        filings = []
        try:
            for code in codes:
                latest = _latest_earnings(code)
                if latest:
                    filings.append(latest)
        except Exception as exc:
            warnings.append(f"refresh {title}: {exc}")
            logging.error("[note_idea_sources] earnings lookup failed for %s: %s", title, exc)
            continue
        if len(filings) < len(codes) or any(f["pubdate"][:10] < cutoff for f in filings):
            continue
        ready_date = max(f["pubdate"][:10] for f in filings)
        if notified.get(title, "") >= ready_date:
            continue
        candidates.append(
            {
                "title": title,
                "ready_date": ready_date,
                "filings": [f"{f['code']} {f['pubdate'][:10]} {f['title']}" for f in filings],
            }
        )
    return candidates, warnings
