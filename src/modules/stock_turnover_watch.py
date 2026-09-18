import html
import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


JST = timezone(timedelta(hours=9), "JST")
RANKING_URL = "https://kabutan.jp/warning/trading_value_ranking"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

DEFAULT_TOP_N = 40
DEFAULT_MIN_CHANGE_PCT = 3.0

# Plain-text, user-maintained skip list (one ticker code per line, "#"
# comments and blank lines ignored) - mirrors post_x_magazine's exclude
# file pattern. Without this, a large-mover the user has already looked at
# and decided NOT to add would keep reappearing every single day for as
# long as it stays in the top-N by trading value, with no way to silence it
# short of adding it to the watchlist.
DISMISSED_PATH = Path("state") / "stock_turnover_watch_dismissed.txt"

# Daily snapshot archive so past new-entrant candidates can be looked back
# on (output/stock_turnover_watch.json itself is overwritten every run) -
# mirrors sumo_news's own history archive.
HISTORY_DIR_NAME = "history"
HISTORY_RETENTION_DAYS = 40

# The ranking page's stock table has a stable, server-rendered structure
# (code -> name -> market -> gaiyou/chart icon cells -> price -> blank cell
# -> change -> change% -> trading value -> PER -> PBR -> yield). Matching
# that fixed shape directly is more robust than trying to key off column
# headers, since the header row's colspans don't line up 1:1 with body <td>s.
_ROW_RE = re.compile(
    r'<td class="tac"><a href="/stock/\?code=([^"]+)">.*?</a></td>\s*'
    r'<th scope="row" class="tal">([^<]*)</th>\s*'
    r'<td class="tac">([^<]*)</td>\s*'
    r'<td class="gaiyou_icon">.*?</td>\s*'
    r'<td class="chart_icon">.*?</td>\s*'
    r'<td>([\d,]+)</td>\s*'
    r'<td></td>\s*'
    r'<td class="w61"><span class="(?:up|down)">([^<]*)</span></td>\s*'
    r'<td class="w50"><span class="(?:up|down)">([^<]*)</span>%</td>\s*'
    r'<td>([\d,]+)</td>',
    re.S,
)


def _fetch_ranking() -> list[dict]:
    request = urllib.request.Request(RANKING_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")

    items = []
    for match in _ROW_RE.finditer(body):
        code, name, market, price_text, change_text, change_pct_text, trading_value_text = match.groups()
        try:
            change_pct = float(change_pct_text)
        except ValueError:
            change_pct = None
        items.append(
            {
                "ticker": f"{code}.T",
                "code": code,
                "name": html.unescape(name).strip(),
                "market": html.unescape(market).strip(),
                "price": price_text.replace(",", ""),
                "change": change_text,
                "change_pct": change_pct,
                "trading_value": trading_value_text.replace(",", ""),
            }
        )
    return items


def _load_config(root: Path) -> dict:
    path = root / "config.json"
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("[stock_turnover_watch] config.json is invalid; default settings used.")
        return {}
    return config.get("stock_turnover_watch", {}) if isinstance(config, dict) else {}


def _watchlist_tickers(root: Path) -> set[str]:
    config_path = root / "config.json"
    if not config_path.exists():
        return set()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    raw_targets = config.get("watchlist", {}).get("tickers", [])
    return {
        str(t.get("ticker", "")).strip()
        for t in raw_targets
        if isinstance(t, dict) and t.get("ticker")
    }


def _dismissed_tickers(root: Path) -> set[str]:
    path = root / DISMISSED_PATH
    if not path.exists():
        return set()
    dismissed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.strip()
        if not code or code.startswith("#"):
            continue
        # Accept either a bare code ("9984") or a full ticker ("9984.T") in
        # the file, so the user doesn't need to remember the exact format.
        dismissed.add(code if code.endswith(".T") else f"{code}.T")
    return dismissed


def _archive_history(root: Path, payload: dict) -> None:
    history_dir = root / "output" / HISTORY_DIR_NAME
    history_dir.mkdir(parents=True, exist_ok=True)
    generated_at = payload.get("generated_at") or datetime.now(JST).isoformat()
    try:
        date_label = datetime.fromisoformat(str(generated_at)).astimezone(JST).strftime("%Y%m%d")
    except (TypeError, ValueError):
        date_label = datetime.now(JST).strftime("%Y%m%d")
    history_path = history_dir / f"stock_turnover_watch_{date_label}.json"
    history_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cutoff = datetime.now(JST) - timedelta(days=HISTORY_RETENTION_DAYS)
    for existing in history_dir.glob("stock_turnover_watch_*.json"):
        try:
            file_date = datetime.strptime(existing.stem.split("_")[-1], "%Y%m%d").replace(tzinfo=JST)
        except ValueError:
            continue
        if file_date < cutoff:
            existing.unlink(missing_ok=True)


def run(root: Path) -> None:
    output_dir = root / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "stock_turnover_watch.json"
    generated_at = datetime.now(JST).isoformat()

    config = _load_config(root)
    top_n = int(config.get("top_n", DEFAULT_TOP_N))
    min_change_pct = float(config.get("min_change_pct", DEFAULT_MIN_CHANGE_PCT))

    try:
        ranking = _fetch_ranking()
    except Exception as exc:
        payload = {
            "module": "stock_turnover_watch",
            "generated_at": generated_at,
            "status": "error",
            "error": str(exc),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.error("[stock_turnover_watch] fetch failed: %s", exc)
        return

    if not ranking:
        payload = {
            "module": "stock_turnover_watch",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No rows parsed from the ranking page.",
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[stock_turnover_watch] skipped: no rows parsed")
        return

    watchlist_tickers = _watchlist_tickers(root)
    dismissed_tickers = _dismissed_tickers(root)
    # Already-watched tickers are excluded here - this module is only for
    # surfacing NEW candidates to manually review, not for re-flagging
    # names already on the watchlist (stock_range's own candidate scoring
    # already covers those). Dismissed tickers (state/stock_turnover_watch_
    # dismissed.txt) are ones the user has already looked at and chose not
    # to add - without this they'd keep reappearing every day they stay in
    # the top-N by trading value.
    candidates = [
        item
        for item in ranking[:top_n]
        if item["ticker"] not in watchlist_tickers
        and item["ticker"] not in dismissed_tickers
        and item.get("change_pct") is not None
        and abs(item["change_pct"]) >= min_change_pct
    ]

    if candidates:
        payload = {
            "module": "stock_turnover_watch",
            "generated_at": generated_at,
            "status": "ok",
            "top_n": top_n,
            "min_change_pct": min_change_pct,
            "data": candidates,
        }
        logging.info("[stock_turnover_watch] found %d new-entrant candidate(s) among top %d by trading value", len(candidates), top_n)
    else:
        payload = {
            "module": "stock_turnover_watch",
            "generated_at": generated_at,
            "status": "skipped",
            "reason": "No non-watchlist large-move candidates in today's top trading-value ranking.",
        }
        logging.info("[stock_turnover_watch] no new-entrant candidates today")

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Archive every day the ranking was actually fetched (whether or not it
    # produced candidates), so "any days with none" is still answerable -
    # but not a fetch-failure or unparseable-page day, which isn't real data.
    _archive_history(root, payload)


if __name__ == "__main__":
    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    run(root)
